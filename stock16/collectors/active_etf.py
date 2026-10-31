"""Verified active-ETF issuer adapters. No trade inference or access bypass."""
import json
import re
from datetime import datetime
from urllib.parse import parse_qs,urlsplit,quote
from .base import NeedsConfiguration,TABLE_TYPES
from .etf_adapters import domestic_position
from .parsers import table
from .nps import clean


def pdf_position(code,name,quantity,weight,value):
    code=str(code or '').strip()
    if re.fullmatch(r'[0-9][0-9A-Z]{5}',code):
        return domestic_position(code,name,quantity,weight,value)
    # The issuer's explicitly labelled EQUITY identifier supplies the instrument
    # class, but does not establish a global ISIN/CUSIP match from ticker alone.
    equity=re.fullmatch(r'(.+?) ([A-Z]{2}) EQUITY',code,re.I)
    from ..model import number
    item=dict(name=name,source_security_id=code.upper() or 'CASH:KRW',
      shares=number(quantity) if equity else None,market_value=number(value),weight=number(weight),
      currency='KRW',instrument='equity' if equity else 'source_item')
    if equity: item['ticker']=equity[1]
    return item


class TimeAdapter:
    name='time'

    def __init__(self,repo,transport,config):
        self.repo,self.transport,self.config=repo,transport,config

    def prepare(self,fund):
        product=parse_qs(urlsplit(fund['source_url']).query).get('idx',[''])[0]
        if not re.fullmatch(r'\d{1,4}',product):
            raise NeedsConfiguration('TIME 공식 상세페이지 idx 등록 필요')
        body,_,_=self.transport.fetch('https://www.timeetf.co.kr/m11_view.php?idx='+product,{'text/html'})
        match=re.search(r'class="prdNum">\s*<span>([0-9A-Z]{6})</span>',body.decode('utf-8'))
        if not match or match[1]!=fund['code']:
            raise NeedsConfiguration('TIME 상품번호/거래소 코드 일치 확인 필요')
        return product

    def fetch_day(self,fund,day,product):
        # Official form pdfDate and Excel export were checked on distinct dates
        # and a holiday. The workbook contains all rows, not the top-ten preview.
        url='https://www.timeetf.co.kr/pdf_excel.php?idx='+product+'&pdfDate='+day.isoformat()
        body,media,final=self.transport.fetch(url,TABLE_TYPES)
        try: records=table(body,'xlsx')
        except ValueError as error:
            if str(error)=='Empty or malformed table': return None
            raise
        required={'종목코드','종목명','수량','평가금액(원)','비중(%)'}
        if any(set(r)!=required for r in records): raise ValueError('TIME workbook columns changed')
        rows=[pdf_position(r['종목코드'],r['종목명'],r['수량'],r['비중(%)'],r['평가금액(원)']) for r in records]
        return dict(body=body,media_type=media,source_url=final,as_of_date=day.isoformat(),rows=rows,
          published_at=None,disclosure_scope='full',quantity_type='pdf_per_cu_quantity',
          currency='KRW',deduplicate_state=True,source_type='time-etf')


class KoactAdapter:
    name='koact'

    def __init__(self,repo,transport,config):
        self.repo,self.transport,self.config=repo,transport,config

    def prepare(self,fund):
        product=parse_qs(urlsplit(fund['source_url']).query).get('id',[''])[0]
        if not re.fullmatch(r'2ETF[A-Z0-9]{2}',product):
            raise NeedsConfiguration('KoAct 공식 상세페이지 상품 ID 등록 필요')
        body,_,_=self.transport.fetch('https://www.samsungactive.co.kr/api/v1/product/etf/'+product+'.do',{'application/json'})
        info=json.loads(body)['info']['product']
        if info['stkTicker']!=fund['code'] or info['fId']!=product:
            raise NeedsConfiguration('KoAct 상품번호/거래소 코드 일치 확인 필요')
        self.repo.execute('UPDATE funds SET name=? WHERE id=?',(info['fNm'],fund['id']))
        return product

    def fetch_day(self,fund,day,product):
        from .krx_etf import parse_pdf
        url='https://www.samsungactive.co.kr/api/v1/product/etf-pdf/'+product+'.do?gijunYMD='+day.strftime('%Y.%m.%d')
        body,media,final=self.transport.fetch(url,{'application/json'})
        payload=json.loads(body)
        if not payload.get('pdf') or not payload['pdf'].get('list'): return None
        asof,rows=parse_pdf(payload)
        # Reviewed issuer codes explicitly mark overseas equity instruments.
        rows=[pdf_position(r['itmNo'],r['secNm'],r.get('applyQ'),r.get('ratio'),r.get('evalA'))
              if re.fullmatch(r'.+? [A-Z]{2} EQUITY',r['itmNo'],re.I) else row
              for r,row in zip(payload['pdf']['list'],rows)]
        if asof<day.isoformat(): return None
        if asof!=day.isoformat(): raise ValueError('KoAct official PDF date mismatch')
        return dict(body=body,media_type=media,source_url=final,as_of_date=asof,rows=rows,
          published_at=None,disclosure_scope='full',quantity_type='pdf_per_cu_quantity',currency='KRW',source_type='koact-etf')


class ArkAdapter:
    """Allowed official top-ten data, explicitly partial; full CSV is restricted."""
    latest_only=True
    name='ark'
    def __init__(self,repo,transport,config):
        self.repo,self.transport,self.config=repo,transport,config

    def prepare(self,fund):
        code=fund['code']
        if not re.fullmatch(r'ARK[KQWGFX]',code): raise NeedsConfiguration('검증된 ARK 액티브 ETF ticker 필요')
        body,_,_=self.transport.fetch('https://www.ark-funds.com/funds/'+code.lower(),{'text/html'})
        match=re.search(r'/api/fund/overview/(\d+)',body.decode('utf-8'))
        if not match: raise NeedsConfiguration('ARK 공식 상품 ID 검토 필요')
        return match[1]

    def fetch_latest(self,fund,product,today):
        settings=dict(Heading='Top 10 Holdings',PdfLinkText='Full Holdings PDF',CsvLinkText='Full Holdings CSV',
                      Link=dict(Style='',Href='',Aria='',Target='',Text=''))
        url='https://www.ark-funds.com/api/fund/holdings/'+product+'?fundHoldingData='+quote(json.dumps(settings,separators=(',',':')))
        body,_,final=self.transport.fetch(url,{'text/html'})
        text=body.decode('utf-8')
        date_match=re.search(r'As of\s+(\d{1,2}/\d{1,2}/\d{4})',text)
        if not date_match: raise ValueError('ARK holdings basis date missing')
        asof=datetime.strptime(date_match[1],'%m/%d/%Y').date()
        if asof>today: raise ValueError('ARK holdings date is in the future')
        records=[[clean(cell) for cell in re.findall(r'<t[hd]\b[^>]*>(.*?)</t[hd]>',tr,re.S)]
                 for tr in re.findall(r'<tr\b[^>]*>(.*?)</tr>',text,re.S)]
        if not records or records[0]!=['Ticker','Name','Market Value','Weight (%)']:
            raise ValueError('ARK official holdings columns changed')
        if not 1<=len(records)-1<=10: raise ValueError('ARK top-ten scope changed')
        from ..model import number
        rows=[]
        unresolved=0
        for cells in records[1:]:
            if len(cells)!=4: raise ValueError('Malformed ARK holdings row')
            ticker,name,value,weight=cells
            if ticker in ('-','—',''):
                unresolved+=1
                continue
            rows.append(dict(name=name,source_security_id=ticker,ticker=ticker,
              shares=None,market_value=number(value.replace('$','')),weight=number(weight.rstrip('%')),
              currency='USD',instrument='reported_security'))
        if not rows: raise ValueError('No identifiable ARK holdings')
        extracted=json.dumps(dict(as_of_date=asof.isoformat(),columns=records[0],rows=records[1:]),
                             sort_keys=True,ensure_ascii=False).encode()
        return dict(body=extracted,media_type='application/json',source_url=final,as_of_date=asof.isoformat(),
          rows=rows,published_at=None,disclosure_scope='partial',quantity_type='unavailable',currency='USD',
          source_type='ark-etf',notice='공식 상위 10개 관측 · 수량 미공개 · 전체 CSV는 수동 import 필요'+
          (f' · 식별자 미공개 {unresolved}개 분석 제외' if unresolved else ''))


class JpmorganAdapter:
    """Official latest full daily holdings; never invent historical snapshots."""
    name='jpmorgan'
    latest_only=True

    def __init__(self,repo,transport,config):
        self.repo,self.transport,self.config=repo,transport,config

    def prepare(self,fund):
        url='https://am.jpmorgan.com/FundsMarketingHandler/autocomplete?country=us&role=adv&language=en&userLoggedIn=false'
        body,_,_=self.transport.fetch(url,{'application/json'})
        matches=[r for r in json.loads(body) if r.get('fundType')=='etf' and r.get('ticker')==fund['code'] and r.get('baseCurrencyCode')=='USD']
        if len(matches)!=1 or not re.fullmatch(r'[A-Z0-9]{9}',matches[0].get('cusip','')):
            raise NeedsConfiguration('JPMorgan 공식 ETF ticker / CUSIP 매핑 검토 필요')
        return matches[0]['cusip']

    def fetch_latest(self,fund,cusip,today):
        url='https://am.jpmorgan.com/FundsMarketingHandler/product-data?cusip='+cusip+'&country=us&role=adv&language=en'
        body,media,final=self.transport.fetch(url,{'application/json'})
        data=json.loads(body)['fundData']
        full=data['dailyHoldingsAll']
        asof=full['effectiveDate']
        basis=datetime.strptime(asof,'%Y-%m-%d').date()
        if basis>today or not full['data']: raise ValueError('JPMorgan holdings basis missing or future')
        # Cash can be an additional row beyond the published number of holdings.
        expected=data.get('numberOfHoldings')
        if expected and len(full['data'])<int(expected): raise ValueError('Incomplete JPMorgan holdings')
        rows=[]
        for row in full['data']:
            if row.get('navDate')!=asof: raise ValueError('Mixed JPMorgan holdings dates')
            if not row.get('securityId') or not row.get('securityDescription'): raise ValueError('Missing holdings identifier')
            equity=row.get('securityType') in ('DOMESTIC COMMON STOCK','FOREIGN COMMON STOCK','ADR','COMMON STOCK')
            item=dict(name=row['securityDescription'],source_security_id=str(row['securityId']),
              ticker=row.get('securityTicker') or None,shares=row.get('shares') if equity else None,
              market_value=row.get('marketValue'),weight=row.get('netAssetValuePercent'),
              currency=row.get('currencyCode') or 'USD',instrument='equity' if equity else row.get('securityType','source_item'))
            # Only explicitly identified global IDs may merge across providers.
            if row.get('securityIsin'): item['isin']=row['securityIsin']
            if row.get('securityCusip'): item['cusip']=row['securityCusip']
            rows.append(item)
        return dict(body=body,media_type=media,source_url=final,as_of_date=asof,rows=rows,
          published_at=None,disclosure_scope='full',quantity_type='fund_reported_quantity',currency='USD',
          source_type='jpmorgan-etf',deduplicate_state=True)
