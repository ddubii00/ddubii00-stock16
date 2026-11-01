"""Approved KRX whole-market API; historical Nasdaq needs a reviewed full feed."""
import json
import os
import re
from datetime import date,timedelta
from ..markets import FIRST_WEEK,MARKETS,closed_week,weeks_between,MarketAnalytics,import_market
from ..source_state import record_status
from .base import NeedsConfiguration
from .transport import Transport,FetchError

KRX_HOST='data-dbg.krx.co.kr'
KRX_ENDPOINTS={'KOSPI':'stk_bydd_trd','KOSDAQ':'ksq_bydd_trd'}


def parse_krx(payload,market,day):
    if 'OutBlock_1' not in payload:
        raise NeedsConfiguration('KRX 공식 응답 오류 · 인증키/서비스 승인 확인 필요')
    rows=payload['OutBlock_1']
    if not isinstance(rows,list):raise ValueError('KRX response contract changed')
    if not rows:return []
    if len(rows)<500:raise ValueError('Truncated/sample KRX universe rejected')
    result=[]
    for row in rows:
        rawday=str(row['BAS_DD']).replace('/','').replace('-','')
        code=row['ISU_CD']
        if rawday!=day.strftime('%Y%m%d') or row['MKT_NM']!=market or not re.fullmatch(r'[0-9][0-9A-Z]{5}',code):
            raise ValueError('KRX date/market/security identity mismatch')
        # These two official stock endpoints are separate from ETF/ETN endpoints.
        # Instrument type is supplied explicitly for file imports; no name guessing.
        result.append(dict(name=row['ISU_NM'],ticker=code,kr_code=code,exchange=market,
          instrument='STOCK',market_cap=row['MKTCAP'],country='KR'))
    return result


class MarketRankCollector:
    def __init__(self,repo,transport,config):self.repo,self.transport,self.config=repo,transport,config
    def run(self,*,start=FIRST_WEEK,end=None,budget=None,markets=MARKETS):
        from . import next_scheduled
        if any(m not in MARKETS for m in markets):raise ValueError('Unknown market')
        self.warnings=[];self.details=dict(checked_weeks=0,new_snapshots=0,errors=0)
        count=0;configured=0
        key=os.environ.get('STOCK16_KRX_API_KEY')
        # The header is scoped to this official host and is recalculated on redirects.
        client=self.transport if not isinstance(self.transport,Transport) else Transport(credentials={KRX_HOST:{'AUTH_KEY':key}}) if key else None
        bound=budget if budget is not None else int(os.environ.get('STOCK16_MARKET_BACKFILL_WEEKS','52'))
        if not 1<=bound<=1000:raise ValueError('Market backfill bound must be 1..1000 weeks')
        if end and any(date.fromisoformat(end)>closed_week(market) for market in markets):
            raise ValueError('Future/incomplete week rejected')
        if not key and any(m in KRX_ENDPOINTS for m in markets):
            from .marcap import MarcapArchive
            public=MarcapArchive(self.repo,self.transport)
            try:
                count=public.run(start=start,end=end or closed_week('KOSPI').isoformat(),budget=bound,markets=[m for m in markets if m in KRX_ENDPOINTS])
                self.details=public.details;self.warnings=public.warnings
            except Exception as error:
                self.warnings=['공개 한국 아카이브 접근/검증 실패 · '+type(error).__name__]
                self.details['errors']+=1
                for market in markets:
                    if market in KRX_ENDPOINTS:
                        record_status(self.repo,'fund','market-'+market.lower(),'market-ranks','market-ranks','error',self.warnings[0],'https://github.com/FinanceData/marcap',next_scheduled('market-ranks'))
            if 'NASDAQ' in markets:
                message='NASDAQ 당시 전종목 시가총액 공개 자료 미확보 · 검증 파일 연결/import 필요 (ETF 제외)'
                self.warnings.append(message)
                record_status(self.repo,'fund','market-nasdaq','market-ranks','market-ranks','needs_configuration',message,'https://www.nasdaq.com/',next_scheduled('market-ranks'))
            return count
        for market in markets:
            source='https://openapi.krx.co.kr/' if market!='NASDAQ' else 'https://www.nasdaq.com/'
            added=0;failures=[];status='no_new_data';message='신규 주간 시가총액 자료 없음'
            try:
                if market=='NASDAQ':
                    raise NeedsConfiguration('NASDAQ 과거 전종목 시가총액·당시 상장시장·영구 식별자 자료 연결 또는 검증 파일 import 필요 (ETF 제외)')
                if not key:raise NeedsConfiguration('STOCK16_KRX_API_KEY 및 유가증권/코스닥 일별매매정보 서비스 승인 필요')
                configured+=1
                target=end or closed_week(market).isoformat()
                if date.fromisoformat(target)>closed_week(market):raise ValueError('Future/incomplete week rejected')
                weeks=weeks_between(start,target)
                stored={r['week_end'] for r in self.repo.rows('SELECT DISTINCT week_end FROM market_snapshots WHERE market=?',(market,))}
                # Prioritize the current complete week, then oldest missing history.
                wanted=[target]+[w for w in weeks if w not in stored and w!=target]
                for week in wanted[:bound]:
                    self.details['checked_weeks']+=1
                    try:
                        found=False
                        for offset in range(5):
                            day=date.fromisoformat(week)-timedelta(days=offset)
                            url='https://'+KRX_HOST+'/svc/apis/sto/'+KRX_ENDPOINTS[market]+'?basDd='+day.strftime('%Y%m%d')
                            body,media,final=client.fetch(url,{'application/json'})
                            rows=parse_krx(json.loads(body),market,day)
                            if not rows:continue
                            meta=dict(market=market,as_of_date=day.isoformat(),currency='KRW',source_url=final,source_type='krx-market-ranks',universe_complete=True,expected_count=len(rows),
                              universe_definition='KRX 공식 주식 API 전종목 · 우선주/DR 포함 · ETF/ETN 별도 API 제외 · 주권 종목 단위')
                            added+=int(import_market(self.repo,meta,rows,body,media)[1]);found=True;break
                        if not found:failures.append(week+' · 같은 주 거래자료 미제공')
                    except NeedsConfiguration:raise
                    except FetchError as error:
                        if error.status_code in (401,403):raise NeedsConfiguration('KRX 인증키/서비스 승인 또는 공식 접근 정책 확인 필요') from None
                        failures.append(week+' · '+type(error).__name__)
                    except Exception as error:failures.append(week+' · '+type(error).__name__)
                status='partial' if failures else 'success' if added else 'no_new_data'
                message=f'주간 snapshot {added}건 저장 · 누락/실패 {len(failures)}건'
            except NeedsConfiguration as error:status='needs_configuration';message=str(error)
            except Exception as error:status='error';message='주간 순위 수집 실패 · '+type(error).__name__
            record_status(self.repo,'fund','market-'+market.lower(),'market-ranks','market-ranks',status,message,source,next_scheduled('market-ranks'))
            if failures or status in ('needs_configuration','error'):
                self.warnings.append(market+' · '+message)
            self.details['errors']+=len(failures)+int(status=='error')
            count+=added
        self.details['new_snapshots']=count
        if not configured:raise NeedsConfiguration(' · '.join(self.warnings))
        return count
