"""Public FinanceData KRX-derived archive. Explicitly Secondary, not KRX API."""
import gzip
import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict
from datetime import date,datetime,timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from .transport import Transport
from ..markets import closed_week,weeks_between,week_end,import_market
from ..model import number
from ..source_state import record_status

REPO='FinanceData/marcap'
MAX_ARCHIVE=32*1024*1024
COLUMNS=['Date','Code','Name','Market','Marcap','Close','Stocks']


def archive_dates(body):
    f=pq.ParquetFile(BytesIO(body))
    if not set(COLUMNS)<=set(f.schema_arrow.names) or f.metadata.num_rows>1500000:
        raise ValueError('Public archive schema/row budget changed')
    dates=set()
    for batch in f.iter_batches(columns=['Date'],batch_size=65536):
        for value in pc.unique(pc.cast(batch.column(0),pa.date32())).to_pylist():
            if value:dates.add(value.isoformat())
    return dates


def selected_rows(body,days):
    """Only week-end dates reach Python rows; halted/zero-volume stocks remain."""
    f=pq.ParquetFile(BytesIO(body));wanted=pa.array([date.fromisoformat(d) for d in days],type=pa.date32())
    for batch in f.iter_batches(columns=COLUMNS,batch_size=32768):
        filtered=batch.filter(pc.is_in(pc.cast(batch.column('Date'),pa.date32()),value_set=wanted))
        for row in filtered.to_pylist():
            market={'KOSPI':'KOSPI','KOSDAQ':'KOSDAQ','KOSDAQ GLOBAL':'KOSDAQ'}.get(row['Market'])
            if market:yield row,market


def normalize_archive_row(row,market):
    code=str(row['Code'])
    if code.isdigit():code=code.zfill(6)
    if not re.fullmatch(r'[0-9][0-9A-Z]{5}',code):raise ValueError('Archive security code changed')
    cap,price,shares=(number(row[k]) for k in ('Marcap','Close','Stocks'))
    if None in (cap,price,shares) or Decimal(cap)<0 or Decimal(price)<0 or Decimal(shares)<0:
        raise ValueError('Missing/negative archive market values')
    # README's old million-KRW description is not applied blindly. Actual files
    # are KRW: dated close × dated listed shares must validate to within 1 KRW.
    if abs(Decimal(cap)-Decimal(price)*Decimal(shares))>1:
        raise ValueError('Archive currency/unit validation failed')
    return dict(name=row['Name'],ticker=code,kr_code=code,exchange=market,instrument='STOCK',market_cap=cap,country='KR')


class MarcapArchive:
    def __init__(self,repo,transport):
        self.repo=repo
        self.transport=Transport(max_bytes=MAX_ARCHIVE) if isinstance(transport,Transport) else transport
        self.folder=repo.root/'cache'/'marcap';self.folder.mkdir(parents=True,exist_ok=True,mode=0o700)

    def manifest(self):
        body,_,_=self.transport.fetch('https://api.github.com/repos/'+REPO+'/git/ref/heads/master',{'application/json'})
        ref=json.loads(body)['object']['sha']
        if not re.fullmatch(r'[0-9a-f]{40}',ref):raise ValueError('Invalid archive revision')
        body,_,_=self.transport.fetch('https://api.github.com/repos/'+REPO+'/contents/data?ref='+ref,{'application/json'})
        files={}
        for row in json.loads(body):
            match=re.fullmatch(r'marcap-(20\d{2})\.parquet',row['name'])
            if match and int(match[1])>=2020:
                if not re.fullmatch(r'[0-9a-f]{40}',row['sha']) or not 0<row['size']<=MAX_ARCHIVE:raise ValueError('Archive file budget/identity changed')
                files[int(match[1])]=dict(blob_sha=row['sha'],ref=ref,size=row['size'])
        if not files:raise ValueError('No public archive years')
        return files

    def body(self,year,info):
        packed=self.folder/(str(year)+'.parquet.gz');metadata=self.folder/(str(year)+'.json')
        if packed.exists() and metadata.exists():
            try:
                old=json.loads(metadata.read_text())
                if old['blob_sha']==info['blob_sha']:
                    with gzip.open(packed,'rb') as stream:body=stream.read(MAX_ARCHIVE+1)
                    if len(body)<=MAX_ARCHIVE and hashlib.sha256(body).hexdigest()==old['sha256']:return body,old
            except (ValueError,OSError,KeyError):pass
        url='https://raw.githubusercontent.com/'+REPO+'/'+info['ref']+'/data/marcap-'+str(year)+'.parquet'
        body,_,final=self.transport.fetch(url,{'application/octet-stream','application/vnd.apache.parquet','binary/octet-stream'})
        actual=hashlib.sha1(b'blob '+str(len(body)).encode()+b'\0'+body).hexdigest()
        if len(body)!=info['size'] or actual!=info['blob_sha']:raise ValueError('Archive Git blob integrity mismatch')
        meta=dict(info,sha256=hashlib.sha256(body).hexdigest(),source_url=final)
        # One compressed current version per year; no daily copies or git clone.
        for target,data in ((packed,gzip.compress(body,compresslevel=6,mtime=0)),(metadata,json.dumps(meta).encode())):
            fd,temp=tempfile.mkstemp(dir=self.folder)
            try:
                with os.fdopen(fd,'wb') as f:f.write(data)
                os.replace(temp,target)
            finally:
                if os.path.exists(temp):os.unlink(temp)
        return body,meta

    def run(self,*,start,end,budget,markets):
        from . import next_scheduled
        self.warnings=[];count=0
        self.details=dict(checked_weeks=0,new_snapshots=0,errors=0,source='FinanceData/marcap · Secondary')
        files=self.manifest();current_year=min(max(files),date.fromisoformat(end).year)
        latest_body,_=self.body(current_year,files[current_year])
        global_latest=max(archive_dates(latest_body));del latest_body
        self.details['archive_latest_date']=global_latest
        target_weeks=weeks_between(start,end)
        available_end=min(date.fromisoformat(end),week_end(global_latest))
        if available_end.isoformat()>global_latest:available_end-=timedelta(days=7)
        wanted={}
        added_by_market={m:0 for m in markets}
        for market in markets:
            stored={r['week_end'] for r in self.repo.rows('SELECT DISTINCT week_end FROM market_snapshots WHERE market=?',(market,))}
            missing=[w for w in target_weeks if w not in stored and w<=available_end.isoformat()]
            latest=available_end.isoformat()
            wanted[market]=set(([latest]+[w for w in missing if w!=latest])[:budget]) if latest>=start else set()
        years=sorted({int(w[:4]) for weeks in wanted.values() for w in weeks})
        for year in years:
            if year not in files:self.warnings.append(str(year)+' · 공개 아카이브 없음');continue
            try:
                body,info=self.body(year,files[year]);dates=archive_dates(body)
                candidates={week_end(day).isoformat():day for day in sorted(dates)}
                # The first Friday may use a prior-calendar-year holiday close.
                prior=None;prior_info=None;prior_days=set()
                boundary={w for weeks in wanted.values() for w in weeks if int(w[:4])==year and date.fromisoformat(w)-timedelta(days=4)<date(year,1,1)}
                if boundary and year-1 in files:
                    prior,prior_info=self.body(year-1,files[year-1])
                    for day in sorted(archive_dates(prior)):
                        week=week_end(day).isoformat()
                        if week in boundary and day>candidates.get(week,''):candidates[week]=day
                days={candidates[w] for weeks in wanted.values() for w in weeks if int(w[:4])==year and w in candidates}
                groups=defaultdict(list);originals=defaultdict(list)
                def accumulate(content,allowed):
                    for row,market in selected_rows(content,allowed):
                        day=row['Date'].date().isoformat() if isinstance(row['Date'],datetime) else row['Date'].isoformat()
                        week=week_end(day).isoformat()
                        if week not in wanted.get(market,set()) or day!=candidates.get(week):continue
                        groups[(market,week,day)].append(normalize_archive_row(row,market))
                        originals[(market,week,day)].append({k:(v.isoformat() if hasattr(v,'isoformat') else v) for k,v in row.items()})
                accumulate(body,days & dates)
                if prior:accumulate(prior,days- dates)
                for (market,week,day),rows in sorted(groups.items()):
                    if len(rows)<500:raise ValueError('Incomplete public market date')
                    chosen_info=info if day[:4]==str(year) else prior_info
                    extracted=json.dumps(dict(format='marcap-weekly-extract-v1',archive=chosen_info,as_of_date=day,source_rows=originals[(market,week,day)]),sort_keys=True,ensure_ascii=False).encode()
                    meta=dict(market=market,as_of_date=day,currency='KRW',source_type='marcap-archive',source_url=chosen_info['source_url'],universe_complete=True,expected_count=len(rows),
                      universe_definition='Secondary · FinanceData 공개 KRX 원천 주식 아카이브 · 당시 전종목/우선주 포함 · ETF/ETN 별도 제외 · 시총 KRW 검증')
                    new=int(import_market(self.repo,meta,rows,extracted)[1]);count+=new;added_by_market[market]+=new;self.details['checked_weeks']+=1
                del body,groups,originals
            except Exception as error:
                self.details['errors']+=1;self.warnings.append(str(year)+' · 공개 아카이브 검토 필요 · '+type(error).__name__)
        self.details['new_snapshots']=count
        for market in markets:
            stored={r['week_end'] for r in self.repo.rows('SELECT DISTINCT week_end FROM market_snapshots WHERE market=?',(market,))}
            missing=[w for w in target_weeks if w not in stored]
            status='partial' if missing or self.warnings else 'success' if added_by_market[market] else 'no_new_data'
            message=f"공개 아카이브 · 신규 {added_by_market[market]}주 · 미확보 {len(missing)}주 · 원본 최신일 {global_latest} · Secondary"
            record_status(self.repo,'fund','market-'+market.lower(),'market-ranks','market-ranks',status,message,'https://github.com/'+REPO,next_scheduled('market-ranks'))
            if missing:self.warnings.append(market+' · '+message)
        return count
