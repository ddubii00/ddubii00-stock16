import json
from urllib.parse import urljoin
from defusedxml import ElementTree as ET
from ..model import import_snapshot, import_allocation, now, validate_meta
from ..storage import store_document
from .parsers import table, mapped_rows

TABLE_TYPES={'text/csv','application/csv','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','application/json','application/octet-stream','application/vnd.ms-excel'}


class NeedsConfiguration(RuntimeError):
    pass


class DatasetCollector:
    """Reviewed official download mapping. Site-specific wrappers share this contract."""
    def __init__(self,repo,transport,config):
        self.repo,self.transport,self.config=repo,transport,config

    def discover(self):
        sources=self.config.get('datasets',[])
        if not sources:
            raise NeedsConfiguration('공식 다운로드 URL·기준일·공시일·컬럼 매핑 설정 필요')
        return sources

    def fetch(self,item):
        return self.transport.fetch(item['url'],TABLE_TYPES)

    def parse(self,body,item):
        options={k:item[k] for k in ('encoding','skip_rows','delimiter','sheet') if k in item}
        return table(body,item['format'],**options)

    def normalize(self,rows,item):
        from decimal import Decimal
        from ..model import number
        normalized=mapped_rows(rows,item['mapping'],item.get('constants'))
        # Explicit reviewed units only, e.g. NPS KRW 100-million => 100000000.
        for field,multiplier in item.get('multipliers',{}).items():
            if field not in ('shares','market_value','weight','ownership_pct','target_weight'):
                raise ValueError('Unsupported unit multiplier')
            scale=Decimal(str(multiplier))
            if not scale.is_finite() or scale<=0:
                raise ValueError('Invalid unit multiplier')
            for row in normalized:
                value=number(row.get(field))
                row[field]=number(Decimal(value)*scale) if value is not None else None
        return normalized

    def validate(self,meta,rows):
        validate_meta(meta)
        if not rows:
            raise ValueError('No rows; manual review required')

    def persist(self,item,body,media,url,rows):
        meta={**item['metadata'],'source_url':url,'source_type':self.config['id'],'fetched_at':now()}
        self.validate(meta,rows)
        sha=store_document(self.repo,body,media,meta)
        if item.get('kind')=='allocation':
            return import_allocation(self.repo,meta,rows,sha)
        return int(import_snapshot(self.repo,meta,rows,sha)[1])

    def diff(self):
        # import_snapshot recomputes adjacent periods transactionally, including backfills.
        return None

    def run(self):
        count=0
        for item in self.discover():
            body,media,url=self.fetch(item)
            rows=self.normalize(self.parse(body,item),item)
            count+=self.persist(item,body,media,url,rows)
        self.diff()
        return count


class NewsFeedCollector:
    def __init__(self,repo,transport,institution,url):
        self.repo,self.transport,self.institution,self.url=repo,transport,institution,url

    def run(self):
        from email.utils import parsedate_to_datetime
        body,_,_=self.transport.fetch(self.url,{'application/rss+xml','text/xml','application/xml','application/atom+xml'})
        root=ET.fromstring(body)
        count=0
        nodes=root.findall('.//item')+root.findall('.//{*}entry')
        for item in nodes:
            def value(name):
                n=item.find(name)
                if n is None: n=item.find('{*}'+name)
                return (n.text or '').strip() if n is not None else ''
            title=value('title')
            url=value('link')
            if not url:
                link=item.find('{*}link')
                url=link.get('href','') if link is not None else ''
            url=urljoin(self.url,url)
            self.transport.validate_url(url)
            published=value('pubDate') or value('published') or value('updated')
            if not title or not published: continue
            try:
                if ',' in published: published=parsedate_to_datetime(published).isoformat()
                else:
                    from datetime import datetime
                    datetime.fromisoformat(published.replace('Z','+00:00'))
            except ValueError: continue
            with self.repo.connect() as c:
                count+=c.execute('INSERT OR IGNORE INTO disclosures(institution_id,external_id,title,published_at,fetched_at,kind,summary,source_url) VALUES(?,?,?,?,?,?,?,?)',
                  (self.institution,value('guid') or value('id') or url,title,published,now(),'official announcement',None,url)).rowcount
        return count
