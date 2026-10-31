import json
import os
from defusedxml import ElementTree as ET
from .base import NeedsConfiguration
from .parsers import sec_information_table
from ..model import import_snapshot, now
from ..storage import store_document


class SEC13FCollector:
    def __init__(self,repo,transport):
        self.repo,self.transport=repo,transport

    def discover(self,guru):
        cik=guru['cik'].zfill(10)
        body,_,_=self.transport.fetch('https://data.sec.gov/submissions/CIK'+cik+'.json',{'application/json'})
        data=json.loads(body)
        # Verify identity from official submissions rather than trusting a display name.
        if str(data.get('cik','')).lstrip('0')!=cik.lstrip('0'):
            raise ValueError('SEC filer identity mismatch')
        recent=data['filings']['recent']
        items=[]
        for i,form in enumerate(recent['form']):
            if form not in ('13F-HR','13F-HR/A'): continue
            accession=recent['accessionNumber'][i]
            if self.repo.rows('SELECT id FROM disclosures WHERE institution_id=? AND external_id=?',(guru['institution_id'],accession)):
                continue
            items.append(dict(accession=accession,form=form,as_of_date=recent['reportDate'][i],
                              published_at=recent['filingDate'][i],primary=recent['primaryDocument'][i]))
        return sorted(items,key=lambda x:(x['as_of_date'],x['published_at'],x['accession']))

    def fetch(self,url,types):
        return self.transport.fetch(url,types)

    def parse(self,body,filed):
        return sec_information_table(body,filed)

    def normalize(self,rows):
        return rows

    def validate(self,meta,rows):
        if not rows: raise ValueError('Empty filing requires review')

    def persist(self,guru,item):
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

    def run(self):
        ua=os.environ.get('STOCK16_SEC_USER_AGENT','')
        if '@' not in ua:
            raise NeedsConfiguration('SEC 식별용 User-Agent (이름·이메일) 설정 필요')
        self.transport.user_agent=ua
        count=0
        errors=[]
        for guru in self.repo.rows('SELECT * FROM guru_watchlist WHERE enabled=1 AND cik IS NOT NULL'):
            try:
                for item in self.discover(guru):
                    try: count+=self.persist(guru,item)
                    except Exception as e: errors.append(guru['institution_id']+': '+type(e).__name__)
            except Exception as e:
                errors.append(guru['institution_id']+': '+type(e).__name__)
        if errors:
            raise ValueError('일부 SEC filing 검토/재시도 필요: '+'; '.join(errors))
        return count
