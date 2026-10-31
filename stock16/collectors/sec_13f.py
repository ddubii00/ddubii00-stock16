import json
import os
import re
import time
import gzip
import hashlib
import tempfile
from pathlib import Path
from defusedxml import ElementTree as ET
from .base import NeedsConfiguration
from .parsers import sec_information_table
from ..model import import_snapshot, now
from ..storage import store_document
from ..source_state import record_status,queue_review
from .transport import FetchError


class SEC13FCollector:
    def __init__(self,repo,transport):
        self.repo,self.transport=repo,transport

    def discover(self,guru,history=False,from_year=None):
        cik=guru['cik'].zfill(10)
        body,_,_=self.fetch('https://data.sec.gov/submissions/CIK'+cik+'.json',{'application/json'})
        data=json.loads(body)
        # Verify identity from official submissions rather than trusting a display name.
        if str(data.get('cik','')).lstrip('0')!=cik.lstrip('0'):
            raise ValueError('SEC filer identity mismatch')
        pages=[data['filings']['recent']]
        if history:
            for entry in data['filings'].get('files',[]):
                if from_year and entry.get('filingTo','9999')[:4]<str(from_year):
                    continue
                filename=entry['name']
                if not re.fullmatch(re.escape('CIK'+cik)+r'-submissions-\d{3}\.json',filename):
                    raise ValueError('Invalid official SEC history filename')
                url='https://data.sec.gov/submissions/'+filename
                pages.append(json.loads(self.cached_history(url)))
        items=[]
        seen=set()
        for recent in pages:
            for i,form in enumerate(recent['form']):
                if form not in ('13F-HR','13F-HR/A'): continue
                accession=recent['accessionNumber'][i]
                if accession in seen: continue
                seen.add(accession)
                report=recent['reportDate'][i]
                if from_year and (report or recent['filingDate'][i])[:4]<str(from_year): continue
                if self.repo.rows('SELECT id FROM disclosures WHERE institution_id=? AND external_id=?',(guru['institution_id'],accession)):
                    continue
                items.append(dict(accession=accession,form=form,as_of_date=report,
                                  published_at=recent['filingDate'][i],primary=recent['primaryDocument'][i]))
        return sorted(items,key=lambda x:(x['as_of_date'],x['published_at'],x['accession']))

    def fetch(self,url,types):
        for attempt in range(3):
            try:
                return self.transport.fetch(url,types)
            except FetchError as error:
                # Fair-access backoff only for explicit throttling/transient errors.
                # 403/robots rejection is never retried or bypassed.
                if error.status_code not in (429,503) or attempt==2:
                    raise
                try: delay=float(error.retry_after)
                except (TypeError,ValueError): delay=2**(attempt+1)
                time.sleep(min(15,max(1,delay)))

    def cached_history(self,url):
        folder=self.repo.root/'cache'/'sec-history'
        folder.mkdir(parents=True,exist_ok=True)
        path=folder/(hashlib.sha256(url.encode()).hexdigest()+'.json.gz')
        if path.exists() and time.time()-path.stat().st_mtime<86400:
            try:
                with gzip.open(path,'rb') as source:
                    body=source.read(25*1024*1024+1)
                if len(body)<=25*1024*1024:
                    json.loads(body)
                    return body
            except (OSError,EOFError,ValueError):
                pass
        body,_,_=self.fetch(url,{'application/json'})
        json.loads(body)
        fd,temporary=tempfile.mkstemp(dir=folder)
        try:
            with os.fdopen(fd,'wb') as target:
                target.write(gzip.compress(body,mtime=0))
            os.replace(temporary,path)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
        # Cache is disposable, bounded and independent from historical/raw records.
        files=sorted(folder.glob('*.gz'),key=lambda p:p.stat().st_mtime,reverse=True)
        size=0
        for item in files:
            size+=item.stat().st_size
            if size>32*1024*1024 or time.time()-item.stat().st_mtime>7*86400:
                item.unlink(missing_ok=True)
        return body

    def parse(self,body,filed):
        return sec_information_table(body,filed)

    def normalize(self,rows):
        return rows

    def validate(self,meta,rows):
        if not rows: raise ValueError('Empty filing requires review')

    def persist(self,guru,item):
        if not re.fullmatch(r'\d{10}-\d{2}-\d{6}',item['accession']):
            raise ValueError('Invalid accession')
        if '..' in item['primary'] or item['primary'].startswith('/'):
            raise ValueError('Invalid SEC primary document path')
        base='https://www.sec.gov/Archives/edgar/data/'+str(int(guru['cik']))+'/'+item['accession'].replace('-','')+'/'
        index_body,_,_=self.fetch(base+'index.json',{'application/json'})
        index=json.loads(index_body)['directory']['item']
        info=None
        primary_body,_,_=self.fetch(base+item['primary'],{'text/xml','application/xml','text/plain'})
        cover=ET.fromstring(primary_body)
        def cover_text(tag):
            n=cover.find('.//{*}'+tag)
            return (n.text or '').strip() if n is not None else ''
        if item['form']=='13F-HR/A' and cover_text('amendmentType').upper()!='RESTATEMENT':
            raise ValueError('13F additive amendment requires manual merge review')
        # Reject notice/combination-only data as full portfolios.
        for f in index:
            name=f['name']
            if '/' in name or name.startswith('.') or not name.lower().endswith('.xml') or name==item['primary']: continue
            body,media,url=self.fetch(base+name,{'text/xml','application/xml','text/plain'})
            root=ET.fromstring(body)
            if root.tag.split('}')[-1]=='informationTable':
                info=(body,media,url)
                break
        if info is None: raise ValueError('SEC information table unavailable')
        rows=self.normalize(self.parse(info[0],item['published_at']))
        meta=dict(fund_id=guru['institution_id'],as_of_date=item['as_of_date'],published_at=item['published_at'],
          source_url=info[2],source_type='sec-13f',currency='USD',disclosure_scope='full',data_quality='Official-derived')
        self.validate(meta,rows)
        sha=store_document(self.repo,info[0],info[1],meta)
        added=int(import_snapshot(self.repo,meta,rows,sha)[1])
        self.repo.execute('INSERT OR IGNORE INTO disclosures(institution_id,external_id,title,published_at,as_of_date,fetched_at,kind,importance,source_url,checksum) VALUES(?,?,?,?,?,?,?,?,?,?)',
          (guru['institution_id'],item['accession'],item['form']+' · '+item['as_of_date'],item['published_at'],item['as_of_date'],now(),item['form'],1,base+item['primary'],sha))
        return added

    def diff(self):
        return None

    def run(self,history=False,guru_id=None,from_year=None):
        ua=os.environ.get('STOCK16_SEC_USER_AGENT','')
        if '@' not in ua:
            raise NeedsConfiguration('STOCK16_SEC_USER_AGENT · SEC 식별용 User-Agent (이름·이메일) 설정 필요')
        self.transport.user_agent=ua
        count=0
        self.warnings=[]
        from . import next_scheduled
        gurus=self.repo.rows('SELECT * FROM guru_watchlist WHERE enabled=1 AND cik IS NOT NULL')
        if guru_id:
            gurus=[g for g in gurus if g['institution_id']==guru_id]
            if not gurus: raise NeedsConfiguration('활성 SEC Guru ID를 확인하세요')
        for guru in gurus:
            added=0
            failed=[]
            reviewed_dates=set()
            failed_dates=set()
            try:
                for item in self.discover(guru,history,from_year):
                    try:
                        added+=self.persist(guru,item)
                        reviewed_dates.add(item['as_of_date'])
                    except Exception as e:
                        failed.append(type(e).__name__)
                        failed_dates.add(item['as_of_date'])
                        queue_review(self.repo,'sec-13f',guru['institution_id'],item['as_of_date'],
                          'https://www.sec.gov/edgar/browse/?CIK='+guru['cik'],'filing 검토 필요 · '+type(e).__name__)
            except Exception as e:
                failed.append(type(e).__name__)
            for asof in reviewed_dates-failed_dates:
                self.repo.execute('DELETE FROM history_review_queue WHERE collector=? AND fund_id=? AND as_of_date=?',
                                  ('sec-13f',guru['institution_id'],asof))
            count+=added
            status=('partial' if added else 'error') if failed else 'success' if added else 'no_new_data'
            message='분기말 기준 자료이며 현재 보유가 아닐 수 있음'
            if failed:
                message+=' · filing 검토/재시도 필요: '+','.join(sorted(set(failed)))
                self.warnings.append(guru['institution_id']+' · '+message)
            record_status(self.repo,'fund',guru['institution_id'],'holdings','sec-13f',status,message,
                          'https://www.sec.gov/edgar/browse/?CIK='+guru['cik'],next_scheduled('sec-13f'))
        return count
