"""Official-manager routing and one normalization contract for all ETF sources."""
import json
import re
from datetime import datetime
from urllib.parse import urlsplit,parse_qs
from .base import DatasetCollector, NeedsConfiguration, TABLE_TYPES
from .transport import FetchError

PROVIDERS = {
    'kodex': ('www.samsungfund.com','m.samsungfund.com'),
    'tiger': ('www.tigeretf.com','tigeretf.com'),
    'rise': ('www.riseetf.co.kr','riseetf.co.kr'),
    'ace': ('www.aceetf.co.kr','aceetf.co.kr'),
    'sol': ('www.soletf.co.kr','soletf.co.kr'),
    'plus': ('www.plusetf.co.kr','plusetf.co.kr'),
}


def provider_for(source_url):
    host=urlsplit(source_url).hostname
    return next((name for name,hosts in PROVIDERS.items() if host in hosts),None)


def json_path(payload,path):
    for key in path.split('.'):
        payload=payload[int(key)] if isinstance(payload,list) else payload[key]
    return payload


class KodexAdapter:
    name='kodex'

    def __init__(self,repo,transport,config):
        self.repo,self.transport,self.config=repo,transport,config
        self.products=None

    def prepare(self,fund):
        if self.products is None:
            url='https://www.samsungfund.com/api/v1/kodex/main/product.do?ordrColm=LIST_D&ordrSort=DESC&pageNo=1&srchTerm=w&pageRows=500'
            body,_,_=self.transport.fetch(url,{'application/json'})
            self.products={p['stkTicker']:p for p in json.loads(body)}
        product=self.products.get(fund['code'])
        if not product:
            raise NeedsConfiguration('KODEX 공식 상품코드 확인 필요')
        expected=self.config.get('kodex',{}).get(fund['code'])
        if expected and expected!=product['fId']:
            raise ValueError('Official ETF code/product ID mismatch')
        self.repo.execute('UPDATE funds SET name=? WHERE id=?',(product['fNm'],fund['id']))
        return product['fId']

    def fetch_day(self,fund,day,product_id):
        from .krx_etf import parse_pdf
        url='https://www.samsungfund.com/api/v1/kodex/product-pdf/'+product_id+'.do?gijunYMD='+day.strftime('%Y.%m.%d')
        try:
            body,media,final=self.transport.fetch(url,{'application/json'})
        except FetchError as error:
            if error.status_code==404:
                return None
            raise
        payload=json.loads(body)
        pdf=payload.get('pdf')
        if pdf is None or (not pdf.get('list') and int(pdf.get('totalCnt') or 0)==0):
            return None
        asof,rows=parse_pdf(payload)
        # Holiday/stale responses must never be relabelled with the requested date.
        if asof<day.isoformat():
            return None
        if asof!=day.isoformat():
            raise ValueError('Official ETF basis date does not match request')
        return dict(body=body,media_type=media,source_url=final,as_of_date=asof,
                    rows=rows,published_at=None,disclosure_scope='full',quantity_type='pdf_per_cu_quantity')


class ReviewedManagerAdapter:
    """No invented endpoint/schema: enable only an operator-reviewed official mapping."""
    def __init__(self,repo,transport,config,name):
        self.repo,self.transport,self.config,self.name=repo,transport,config,name

    def prepare(self,fund):
        entry=self.config.get('providers',{}).get(self.name,{}).get(fund['code'])
        if not entry or not entry.get('reviewed') or not entry.get('mapping'):
            raise NeedsConfiguration(self.name.upper()+' 공식 데이터 자동수집 불가 / 수동 공식자료 import 필요')
        if not (entry.get('as_of_path') or entry.get('date_parameter_verified')):
            raise NeedsConfiguration('공식 기준일 검증 설정 필요')
        return entry

    def fetch_day(self,fund,day,entry):
        url=entry['url_template'].format(code=fund['code'],date=day,iso_date=day.isoformat())
        if urlsplit(url).hostname not in PROVIDERS[self.name]:
            raise ValueError('Download host does not match the selected official manager')
        try:
            body,media,final=self.transport.fetch(url,TABLE_TYPES)
        except FetchError as error:
            if error.status_code==404:
                return None
            raise
        item={**entry,'url':final}
        if entry.get('as_of_path') or entry.get('rows_path'):
            payload=json.loads(body)
            selected=json_path(payload,entry['rows_path']) if entry.get('rows_path') else payload
            if selected==[]:
                return None
            if entry.get('as_of_path'):
                raw=json_path(payload,entry['as_of_path'])
                from datetime import datetime
                asof=datetime.strptime(str(raw),entry.get('date_format','%Y-%m-%d')).date().isoformat()
                if asof<day.isoformat():
                    return None
                if asof!=day.isoformat():
                    raise ValueError('Official ETF date mismatch')
            if entry.get('rows_path'):
                parser_body=json.dumps(selected).encode()
            else:
                parser_body=body
        else:
            parser_body=body
        parser=DatasetCollector(self.repo,self.transport,{'id':'krx-etf'})
        rows=parser.normalize(parser.parse(parser_body,item),item)
        if not rows:
            return None
        return dict(body=body,media_type=media,source_url=final,as_of_date=day.isoformat(),
          rows=rows,published_at=None,disclosure_scope=entry.get('disclosure_scope','partial'),
          quantity_type=entry.get('quantity_type','pdf_per_cu_quantity'))


def domestic_position(code,name,quantity,weight,value=None,isin=None):
    """Keep non-equity/ambiguous units out of shares; preserve source identity."""
    from ..model import number
    domestic=bool(re.fullmatch(r'[0-9][0-9A-Z]{5}',code or ''))
    item=dict(name=name,shares=number(quantity) if domestic else None,
              weight=number(str(weight).rstrip('%')) if weight is not None else None,
              market_value=number(value),currency='KRW',
              instrument='equity' if domestic else 'source_item')
    if domestic: item.update(kr_code=code,ticker=code,country='KR',exchange='KRX')
    elif isin and re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}[0-9]',isin): item['isin']=isin
    else: item['source_security_id']=code
    if not code or not name: raise ValueError('Official PDF identity missing')
    return item


class SolAdapter:
    name='sol'

    def __init__(self,repo,transport,config):
        self.repo,self.transport,self.config=repo,transport,config
        self.products=None

    def prepare(self,fund):
        if self.config.get('providers',{}).get('sol',{}).get(fund['code']):
            return ReviewedManagerAdapter(self.repo,self.transport,self.config,'sol').prepare(fund)
        if self.products is None:
            body,_,_=self.transport.fetch('https://www.soletf.co.kr/ko/fund?viewType=E',{'text/html'})
            match=re.search(r'var etfResultData\s*=\s*(\[.*?\]);',body.decode('utf-8'),re.S)
            if not match: raise NeedsConfiguration('SOL 공식 상품목록 구조 검토 필요')
            self.products={r['ETF_CD6']:r for r in json.loads(match[1])}
        product=self.products.get(fund['code'])
        if not product or not re.fullmatch(r'\d{6}',product['FUND_CD']):
            raise NeedsConfiguration('SOL 거래소 코드/공식 상품번호 확인 필요')
        self.repo.execute('UPDATE funds SET name=? WHERE id=?',(product['Name'],fund['id']))
        return product['FUND_CD']

    def fetch_day(self,fund,day,product_id):
        if isinstance(product_id,dict):
            return ReviewedManagerAdapter(self.repo,self.transport,self.config,'sol').fetch_day(fund,day,product_id)
        url='https://www.soletf.co.kr/api/fund/pdfList?fund_cd='+product_id+'&work_dt='+day.strftime('%Y%m%d')
        body,media,final=self.transport.fetch(url,{'application/json'})
        payload=json.loads(body)
        if not isinstance(payload,list): raise ValueError('SOL PDF format changed')
        if not payload: return None
        dates={datetime.strptime(r['WORK_DT'],'%Y%m%d').date() for r in payload}
        if len(dates)==1 and max(dates)<day: return None
        if dates!={day} or any(r['ETF_CD6']!=fund['code'] or r['FUND_CD']!=product_id for r in payload):
            raise ValueError('SOL official PDF date/product mismatch')
        rows=[domestic_position(r['STOCK_CODE'],r['SEC_NM'],r['QTY'],r['WT_DISP'],r['PRICE']) for r in payload]
        return dict(body=body,media_type=media,source_url=final,as_of_date=day.isoformat(),
          rows=rows,published_at=None,disclosure_scope='full',quantity_type='pdf_per_cu_quantity')


class PlusAdapter:
    name='plus'

    def __init__(self,repo,transport,config):
        self.repo,self.transport,self.config=repo,transport,config

    def prepare(self,fund):
        if self.config.get('providers',{}).get('plus',{}).get(fund['code']):
            return ReviewedManagerAdapter(self.repo,self.transport,self.config,'plus').prepare(fund)
        product_id=parse_qs(urlsplit(fund['source_url']).query).get('n',[''])[0]
        if not re.fullmatch(r'\d{6}',product_id):
            raise NeedsConfiguration('PLUS 공식 상세페이지 URL (product/detail?n=상품번호) 등록 필요')
        body,_,_=self.transport.fetch('https://www.plusetf.co.kr/product/detail?n='+product_id,{'text/html'})
        text=body.decode('utf-8')
        code=re.search(r'class="summary__product-code">\s*([0-9A-Z]{6})\s*</div>',text)
        if not code or code[1]!=fund['code']:
            raise NeedsConfiguration('PLUS 공식 상품번호/거래소 코드 일치 확인 필요')
        return product_id

    def fetch_day(self,fund,day,product_id):
        if isinstance(product_id,dict):
            return ReviewedManagerAdapter(self.repo,self.transport,self.config,'plus').fetch_day(fund,day,product_id)
        base='https://www.plusetf.co.kr/api/v1/product/pdf/list?n='+product_id+'&d='+day.strftime('%Y%m%d')+'&pageSize=500'
        rows,originals=[],[]
        total=None
        for page in range(20):
            body,media,final=self.transport.fetch(base+'&page='+str(page),{'application/json'})
            payload=json.loads(body)
            expected=int(payload['totalElements'])
            if total is not None and total!=expected: raise ValueError('PLUS PDF changed during pagination')
            total=expected
            if payload['number']!=page: raise ValueError('PLUS pagination mismatch')
            rows.extend(payload['content'])
            originals.append(dict(source_url=final,body=body.decode('utf-8')))
            if payload['last']: break
        else:
            raise ValueError('PLUS PDF exceeds bounded pagination')
        if len(rows)!=total: raise ValueError('Incomplete PLUS Portfolio Deposit File')
        if not rows: return None
        dates={datetime.strptime(r['wkdate'],'%Y%m%d').date() for r in rows}
        if len(dates)==1 and max(dates)<day: return None
        if dates!={day}: raise ValueError('PLUS official PDF date mismatch')
        normalized=[domestic_position(r.get('jmCd') or r['krJmCd'],r['jmNm'],r['amount'],r['ratio'],isin=r.get('krJmCd')) for r in rows]
        # Multi-page originals remain lossless inside a compressed JSON envelope.
        raw=body if len(originals)==1 else json.dumps(dict(pages=originals),sort_keys=True,ensure_ascii=False).encode()
        return dict(body=raw,media_type=media,source_url=base+'&page=0',as_of_date=day.isoformat(),
          rows=normalized,published_at=None,disclosure_scope='full',quantity_type='pdf_per_cu_quantity')


def make_adapters(repo,transport,config):
    return {'kodex':KodexAdapter(repo,transport,config),'sol':SolAdapter(repo,transport,config),
        'plus':PlusAdapter(repo,transport,config),**{
        name:ReviewedManagerAdapter(repo,transport,config,name)
        for name in ('tiger','rise','ace')}}
