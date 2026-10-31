"""Official manager fallback for KRX PDF. No OTP/session-policy circumvention."""
import json
import re
from datetime import date,datetime,timedelta
import os
from zoneinfo import ZoneInfo
from ..model import import_snapshot
from ..storage import store_document
from .base import NeedsConfiguration


def parse_pdf(data):
    pdf=data['pdf']
    rows=pdf['list']
    if not rows or len(rows)!=int(pdf['totalCnt']):
        raise ValueError('Incomplete Portfolio Deposit File; refusing partial snapshot')
    normalized=[]
    for row in rows:
        code=row['itmNo']
        domestic=bool(re.fullmatch(r'[0-9][0-9A-Z]{5}',code))
        isin=code if re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}\d',code) and not code.startswith('KRD') else None
        # Cash balances and derivatives are not share quantities.
        item=dict(name=row['secNm'],market_value=row.get('evalA'),weight=row.get('ratio'),
                  shares=row.get('applyQ') if domestic else None,
                  currency='KRW',instrument='equity' if domestic else 'other',country='KR' if domestic else None)
        if domestic: item.update(kr_code=code,ticker=code,exchange='KRX')
        elif isin: item['isin']=isin
        else: item['source_security_id']=code
        normalized.append(item)
    raw_date=pdf['gijunYMD']
    return raw_date[:4]+'-'+raw_date[4:6]+'-'+raw_date[6:],normalized


def planned_days(repo,fund_id,today,max_days):
    if not 1<=max_days<=3660:
        raise ValueError('ETF backfill bound must be 1..3660 calendar days')
    cutoff=today-timedelta(days=max_days-1)
    snapshots=repo.rows('SELECT DISTINCT as_of_date FROM portfolio_snapshots WHERE fund_id=?',(fund_id,))
    stored={r['as_of_date'] for r in snapshots}
    earliest=min(stored) if stored else cutoff.isoformat()
    start=max(cutoff,date.fromisoformat(earliest))
    revision_days=max(1,min(int(os.environ.get('STOCK16_ETF_REVISION_DAYS','7')),max_days))
    correction_cutoff=today-timedelta(days=revision_days-1)
    latest=max(stored) if stored else None
    days=[]
    day=start
    while day<=today:
        if day.weekday()<5 and (day.isoformat() not in stored or day>=correction_cutoff or day.isoformat()==latest):
            days.append(day)
        day+=timedelta(days=1)
    return days


class KRXETFCollector:
    def __init__(self,repo,transport,config):
        self.repo,self.transport,self.config=repo,transport,config

    def run(self,today=None):
        from .etf_adapters import make_adapters,provider_for
        from ..source_state import record_status
        from . import next_scheduled
        today=today or datetime.now(ZoneInfo('Asia/Seoul')).date()
        bound=int(os.environ.get('STOCK16_ETF_BACKFILL_DAYS','30'))
        if not 1<=bound<=3660:
            raise ValueError('ETF backfill bound must be 1..3660 calendar days')
        self.warnings=[]
        self.details=dict(checked_funds=0,new_snapshots=0,revisions=0,skipped=0,errors=0)
        count=0
        adapters=make_adapters(self.repo,self.transport,self.config)
        funds=self.repo.rows('SELECT f.* FROM funds f JOIN etf_watchlist w ON w.fund_id=f.id WHERE w.enabled=1')
        for fund in funds:
            self.details['checked_funds']+=1
            brand=provider_for(fund['source_url'])
            status='no_new_data'
            added=0
            errors=[]
            message='신규 데이터 없음 · 공식 구성종목 기준'
            try:
                if brand not in adapters:
                    raise NeedsConfiguration('공식 ETF 운용사 또는 다운로드 매핑 설정 필요')
                adapter=adapters[brand]
                context=adapter.prepare(fund)
                latest_only=getattr(adapter,'latest_only',False)
                days=[today] if latest_only else planned_days(self.repo,fund['id'],today,bound)
                notices=[]
                for day in days:
                    try:
                        result=adapter.fetch_latest(fund,context,today) if latest_only else adapter.fetch_day(fund,day,context)
                        if result is None:
                            self.details['skipped']+=1
                            continue
                        meta=dict(fund_id=fund['id'],as_of_date=result['as_of_date'],
                          published_at=result['published_at'],source_url=result['source_url'],
                          source_type=result.get('source_type','krx-etf'),currency=result.get('currency',self.config.get('currency','KRW')),
                          disclosure_scope=result['disclosure_scope'],data_quality='Official-derived',
                          quantity_type=result['quantity_type'],deduplicate_state=result.get('deduplicate_state',False))
                        sha=store_document(self.repo,result['body'],result['media_type'],meta)
                        had=bool(self.repo.rows('SELECT id FROM effective_snapshots WHERE fund_id=? AND as_of_date=?',(fund['id'],meta['as_of_date'])))
                        created=int(import_snapshot(self.repo,meta,result['rows'],sha)[1])
                        added+=created
                        self.details['revisions' if had else 'new_snapshots']+=created
                        self.details['skipped']+=int(not created)
                        self.details['latest_as_of_date']=max(self.details.get('latest_as_of_date',''),meta['as_of_date'])
                        if result.get('notice'): notices.append(result['notice'])
                    except Exception as error:
                        errors.append(day.isoformat()+':'+type(error).__name__)
                if errors:
                    status='partial' if added else 'error'
                    message='일부 영업일 수집 실패 · '+'; '.join(errors[:5])
                elif added:
                    status='success'
                    message='구성종목 snapshot '+str(added)+'건 저장'
                if notices:
                    status='partial'
                    message+=' · '+'; '.join(sorted(set(notices)))
            except NeedsConfiguration as error:
                status,message='needs_configuration',str(error)
            except Exception as error:
                status,message='error','공식 ETF 소스 확인 필요 · '+type(error).__name__
            self.details['errors']+=len(errors) or int(status in ('error','needs_configuration'))
            count+=added
            record_status(self.repo,'fund',fund['id'],'holdings','krx-etf',status,message,
                          fund['source_url'],next_scheduled('krx-etf'))
            if status in ('needs_configuration','error','partial'):
                self.warnings.append(fund['name']+' · '+message)
        return count
