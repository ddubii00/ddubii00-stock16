"""Official manager fallback for KRX PDF. No OTP/session-policy circumvention."""
import json
import re
from datetime import datetime,timedelta
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
        domestic=bool(re.fullmatch(r'\d{6}',code))
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


class KRXETFCollector:
    def __init__(self,repo,transport,config): self.repo,self.transport,self.config=repo,transport,config

    def run(self):
        today=datetime.now(ZoneInfo('Asia/Seoul')).date()
        settings=self.config.get('kodex',{})
        if not settings: raise NeedsConfiguration('공식 ETF 다운로드 매핑 설정 필요')
        count=0
        errors=[]
        self.warnings=[]
        catalogue,_,_=self.transport.fetch('https://www.samsungfund.com/api/v1/kodex/main/product.do?ordrColm=LIST_D&ordrSort=DESC&pageNo=1&srchTerm=w&pageRows=500',{'application/json'})
        products={p['stkTicker']:p for p in json.loads(catalogue)}
        for fund in self.repo.rows('SELECT f.* FROM funds f JOIN etf_watchlist w ON w.fund_id=f.id WHERE w.enabled=1'):
            product=products.get(fund['code']) if 'samsungfund.com' in fund['source_url'] else None
            fid=product['fId'] if product else None
            if not fid:
                self.warnings.append(fund['name']+' 공식 다운로드 설정 필요')
                continue
            if fund['code'] in settings and settings[fund['code']]!=fid:
                raise ValueError('Official ETF code/product ID mismatch; review configuration')
            self.repo.execute('UPDATE funds SET name=? WHERE id=?',(product['fNm'],fund['id']))
            for offset in range(1,-1,-1):
                day=today-timedelta(days=offset)
                if day.weekday()>=5: continue
                url='https://www.samsungfund.com/api/v1/kodex/product-pdf/'+fid+'.do?gijunYMD='+day.strftime('%Y.%m.%d')
                try:
                    body,media,final=self.transport.fetch(url,{'application/json'})
                    asof,rows=parse_pdf(json.loads(body))
                    if asof>today.isoformat(): continue
                    # Official API exposes the basis date, not a publication timestamp.
                    # NULL preserves that distinction instead of substituting fetch date.
                    meta=dict(fund_id=fund['id'],as_of_date=asof,published_at=None,source_url=final,
                       source_type='krx-etf',currency='KRW',disclosure_scope='full',data_quality='Official-derived')
                    sha=store_document(self.repo,body,media,meta)
                    count+=int(import_snapshot(self.repo,meta,rows,sha)[1])
                except Exception as e: errors.append(fund['code']+':'+type(e).__name__)
        self.warnings.extend(errors)
        return count
