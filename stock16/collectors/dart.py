import json
import os
from datetime import date, timedelta
from urllib.parse import urlencode
from .base import NeedsConfiguration
from ..model import now


class DARTCollector:
    """Official event disclosures; event-only reports never fabricate full portfolios."""
    def __init__(self,repo,transport):
        self.repo,self.transport=repo,transport

    def run(self):
        key=os.environ.get('OPENDART_API_KEY')
        if not key: raise NeedsConfiguration('OpenDART API key 설정 필요')
        count=0
        targets=self.repo.rows('SELECT * FROM guru_watchlist WHERE enabled=1 AND corp_code IS NOT NULL')
        if not targets: raise NeedsConfiguration('DART 추적대상 corp_code 설정 필요')
        for target in targets:
            page=1
            while True:
                params=dict(crtfc_key=key,corp_code=target['corp_code'],bgn_de=(date.today()-timedelta(days=30)).strftime('%Y%m%d'),end_de=date.today().strftime('%Y%m%d'),page_no=page,page_count=100)
                body,_,_=self.transport.fetch('https://opendart.fss.or.kr/api/list.json?'+urlencode(params),{'application/json'})
                data=json.loads(body)
                if data['status']=='013': break
                if data['status']!='000': raise ValueError('DART API returned non-success status')
                for item in data['list']:
                    title=item['report_nm']
                    if not any(s in title for s in ('대량보유','특정증권','소유상황')): continue
                    url='https://dart.fss.or.kr/dsaf001/main.do?rcpNo='+item['rcept_no']
                    pub=item['rcept_dt']
                    pub=pub[:4]+'-'+pub[4:6]+'-'+pub[6:]
                    with self.repo.connect() as c:
                        count+=c.execute('INSERT OR IGNORE INTO disclosures(institution_id,external_id,title,published_at,fetched_at,kind,importance,source_url) VALUES(?,?,?,?,?,?,?,?)',
                         (target['institution_id'],item['rcept_no'],title,pub,now(),'DART',1,url)).rowcount
                if page>=data['total_page']: break
                page+=1
        return count
