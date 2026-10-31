"""NPS official home: transient HTML, structured allocation and dated notices."""
import calendar
import html
import json
import re
from decimal import Decimal
from ..model import import_allocation,now,number
from ..storage import store_document


def clean(text):
    return ' '.join(html.unescape(re.sub('<[^>]*>',' ',text)).split())


def parse_home(body):
    text=body.decode('utf-8')
    text=re.sub(r'<!--.*?-->','',text,flags=re.S)
    section=text[text.index('class="main-portfolio"'):]
    heading=re.search(r'기금 포트폴리오\s*<span[^>]*>\s*(\d{4})년\s*(\d{1,2})월\s*말 기준',section)
    if not heading: raise ValueError('NPS as-of heading changed')
    year,month=map(int,heading.groups())
    asof=f'{year:04}-{month:02}-{calendar.monthrange(year,month)[1]:02}'
    categories={'국내주식':'domestic_equity','해외주식':'foreign_equity','국내채권':'domestic_bond','해외채권':'foreign_bond','대체투자':'alternatives','단기자금':'cash','복지 ·기타 부문':'welfare_other'}
    table=re.search(r'<caption>기금 포트폴리오 구성별 규모</caption>(.*?)</table>',section,re.S)
    if not table: raise ValueError('NPS allocation table changed')
    rows=[]
    for tr in re.findall(r'<tr\b[^>]*>(.*?)</tr>',table.group(1),re.S):
        cells=[clean(t) for t in re.findall(r'<t[hd]\b[^>]*>(.*?)</t[hd]>',tr,re.S)]
        if len(cells)!=3 or cells[0] not in categories: continue
        value=re.search(r'[\d,.]+',cells[1])[0]
        weight=re.search(r'[\d.]+',cells[2])[0]
        rows.append(dict(original_category=cells[0],standard_category=categories[cells[0]],
                         market_value=number(Decimal(value.replace(',',''))*Decimal(10)**12),weight=weight))
    if len(rows)<6: raise ValueError('Incomplete NPS allocation')
    total=re.search(r'class="pf-total"[^>]*>\s*<em>([\d,.]+)</em>',section)
    if total:
        rows.append(dict(original_category='기금 전체',standard_category='total_fund',dimension='total',
                         market_value=number(Decimal(total[1].replace(',',''))*Decimal(10)**12),weight='100'))
    plan=text[text.find('연간 기금운용 계획'):text.find('중기 자산배분 계획')]
    target_year=re.search(r'\((\d{4})년 목표 포트폴리오\)',clean(plan))
    if target_year and int(target_year[1])==year:
        plan_text=clean(plan)
        for row in rows:
            target=re.search(re.escape(row['original_category'])+r'\s+([\d.]+)\s*%',plan_text)
            if target: row['target_weight']=target[1]
    notices=[]
    for anchor in re.findall(r'<a\b[^>]*onclick="fnc_goBbsDetail\(.*?</a>',text,re.S):
        args=re.search(r"fnc_goBbsDetail\('([0-9]+)',\s*'([^']+)'\)",anchor)
        pub=re.search(r'class="notice-date">(\d{4})/(\d{2})/(\d{2})',anchor)
        title=re.search(r'<p>(.*?)</p>',anchor,re.S)
        if not args or not pub or not title: continue
        if args[2]!='offi': continue
        notices.append(dict(id=args[1],title=clean(title[1]),published_at='-'.join(pub.groups()),
         source_url='https://fund.nps.or.kr/impa/edwmpblnt/getOHEF0006M0.do?menuId=MN24000489&tmpltdataSn='+args[1]+'&hmpgBbsCd=offi'))
    matching=[n for n in notices if re.search(r'포트폴리오.*?'+str(year)+r'\.'+str(month)+r'월',n['title'])]
    if not matching: raise ValueError('NPS publication date not verified; review official notice')
    return asof,matching[0]['published_at'],rows,notices


class NPSCollector:
    def __init__(self,repo,transport,config=None):
        self.repo,self.transport,self.config=repo,transport,config or {}
    def run(self):
        from ..source_state import record_status
        from . import next_scheduled
        self.warnings=[]
        body,_,url=self.transport.fetch('https://fund.nps.or.kr/main.do',{'text/html'})
        asof,published,rows,notices=parse_home(body)
        meta=dict(fund_id='nps',as_of_date=asof,published_at=published,source_url=url,source_type='nps',currency='KRW',disclosure_scope='partial',data_quality='Official-derived')
        # Reproducible extraction includes labels and source dates, never daily HTML.
        extracted=json.dumps(dict(as_of_date=asof,published_at=published,rows=rows),sort_keys=True,ensure_ascii=False).encode()
        checksum=store_document(self.repo,extracted,'application/json',meta)
        count=import_allocation(self.repo,meta,rows,checksum)
        for n in notices:
            with self.repo.connect() as c:
                count+=c.execute('INSERT OR IGNORE INTO disclosures(institution_id,external_id,title,published_at,as_of_date,fetched_at,kind,importance,source_url) VALUES(?,?,?,?,?,?,?,?,?)',
                  ('nps',n['id'],n['title'],n['published_at'],asof if n['published_at']==published else None,now(),'기금공시',1,n['source_url'])).rowcount
        record_status(self.repo,'institution','nps','allocation','nps','success' if count else 'no_new_data',
                      '월간 배분 · 상세 종목 공개 시차 존재',url,next_scheduled('nps'))
        if self.config.get('discovery_rules') or self.config.get('datasets'):
            from .discovery import ReviewedDiscoveryCollector
            from .base import DatasetCollector,NeedsConfiguration
            try:
                parser=DatasetCollector(self.repo,self.transport,{'id':'nps',**self.config})
                items=list(self.config.get('datasets',[]))
                if self.config.get('discovery_rules'):
                    items+=ReviewedDiscoveryCollector(self.repo,self.transport,{'id':'nps',**self.config}).discover()
                extra=0
                for item in items:
                    body,media,final=parser.fetch(item)
                    extra+=parser.persist(item,body,media,final,parser.normalize(parser.parse(body,item),item))
                count+=extra
                record_status(self.repo,'institution','nps','detailed_holdings','nps','success' if extra else 'no_new_data',
                              '검증한 공식 상세 파일 · 공개 시차 존재',url,next_scheduled('nps'))
            except Exception as error:
                message=str(error) if isinstance(error,NeedsConfiguration) else '상세 공식 파일 수집 실패 · '+type(error).__name__
                record_status(self.repo,'institution','nps','detailed_holdings','nps',
                              'needs_configuration' if isinstance(error,NeedsConfiguration) else 'error',message,url,next_scheduled('nps'))
                self.warnings.append(message)
        else:
            message='공식 파일 수동 등록 필요 · 국내/해외주식·대량보유 파일 mapping 미설정'
            record_status(self.repo,'institution','nps','detailed_holdings','nps','needs_configuration',
                          message,url,next_scheduled('nps'))
            self.warnings.append(message)
        return count
