"""Extract dated official announcements without retaining HTML pages."""
import html
from html.parser import HTMLParser
import json
import re
from ..model import now
from ..storage import store_document
from .nps import clean


class Metadata(HTMLParser):
    def __init__(self): super().__init__();self.values={};self.heading=False;self.title=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='meta':
            key=attrs.get('property') or attrs.get('name')
            if key: self.values[key]=attrs.get('content','')
        if tag=='h1':self.heading=True
    def handle_endtag(self,tag):
        if tag=='h1':self.heading=False
    def handle_data(self,data):
        if self.heading:self.title.append(data)


def article(body):
    parser=Metadata();text=body.decode('utf-8');parser.feed(text)
    published=parser.values.get('article:published_time')
    if not published:
        match=re.search(r'"datePublished"\s*:\s*"([^"]+)"',text)
        published=html.unescape(match[1]) if match else None
    # Pages contain navigation/banner h1 elements as well as the article heading.
    title=parser.values.get('og:title') or ' '.join(parser.title).strip()
    if not published or not title:raise ValueError('Official article date/title unavailable')
    from datetime import datetime
    datetime.fromisoformat(published.replace('Z','+00:00'))
    description=clean(parser.values.get('description') or parser.values.get('og:description') or '')
    return dict(title=title,published_at=published,summary=('원문 발췌: '+description[:400]) if description else None)


class GICCollector:
    def __init__(self,repo,transport):self.repo,self.transport=repo,transport
    def run(self):
        body,_,_=self.transport.fetch('https://www.gic.com.sg/newsroom/',{'text/html'})
        links=re.findall(r'href=["\'](https://www\.gic\.com\.sg/newsroom/(?:all|news|reports)/[^"\']+)["\']',body.decode('utf-8'))
        links=list(dict.fromkeys(links))[:8]
        if not links:raise ValueError('Official GIC newsroom layout changed')
        count=0
        for url in links:
            if self.repo.rows('SELECT id FROM disclosures WHERE institution_id=? AND external_id=?',('gic',url)):continue
            raw,_,final=self.transport.fetch(url,{'text/html'})
            data=article(raw)
            meta=dict(fund_id='gic',as_of_date='',published_at=data['published_at'],source_type='gic',source_url=final)
            # as-of is not exposed for ordinary announcements; source observations
            # retain an explicit empty marker rather than inventing portfolio dates.
            checksum=store_document(self.repo,json.dumps(data,sort_keys=True,ensure_ascii=False).encode(),'application/json',meta)
            count+=int(bool(self.repo.execute('INSERT INTO disclosures(institution_id,external_id,title,published_at,fetched_at,kind,importance,summary,source_url,checksum) VALUES(?,?,?,?,?,?,?,?,?,?)',
              ('gic',url,data['title'],data['published_at'],now(),'official announcement',1 if any(w in data['title'].lower() for w in ('invest','acquire','portfolio','raises','sale')) else 0,data['summary'],final,checksum))))
        return count
