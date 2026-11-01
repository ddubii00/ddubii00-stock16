"""Public MSCI Korea Standard review changes; not a constituents reconstruction."""
import re
from datetime import datetime
from io import BytesIO
from urllib.parse import urljoin
from pypdf import PdfReader
from ..model import now
from ..storage import store_document
from ..source_state import record_status

ARCHIVE='https://app2.msci.com/eqb/gimi/stdindex/index_review.html'
MONTHS={'Feb':2,'May':5,'Aug':8,'Nov':11}


def review_links(html):
    result={}
    for path in re.findall(r'href=[\"\']([^\"\']+)[\"\']',html,re.I):
        match=re.fullmatch(r'/eqb/gimi/stdindex/MSCI_(Feb|May|Aug|Nov)(\d{2})_STPublicList\.pdf',path)
        if match and int(match[2])>=20:
            key=f'{2000+int(match[2])}-{MONTHS[match[1]]:02d}'
            result[key]=urljoin(ARCHIVE,path)
    if not result:raise ValueError('MSCI archive contract changed')
    return result


def parse_review_text(text,period):
    lines=text.splitlines();flat=' '.join(text.split())
    if 'MSCI GLOBAL STANDARD INDEXES' not in flat or 'SUMMARY PER COUNTRY and PER REGION:' not in flat:raise ValueError('Wrong MSCI index family')
    published=re.search(r'\b(?:Geneva|London),\s*([A-Za-z]+ \d{1,2}, \d{4})',flat)
    effective=re.search(r'as of the close of\s+([A-Za-z]+ \d{1,2}, \d{4})',flat)
    if not published or not effective:raise ValueError('MSCI announcement/effective dates missing')
    announcement=datetime.strptime(published[1],'%B %d, %Y').date().isoformat()
    close=datetime.strptime(effective[1],'%B %d, %Y').date().isoformat()
    if announcement[:7]!=period or close[:7]!=period or close<announcement:raise ValueError('MSCI review date mismatch')
    if not re.search(r'^\s*[A-Z][A-Z ]{2,40}\s+\d+\s+\d+\s*$',text,re.M):
        raise ValueError('MSCI country summary unavailable')
    expected=re.search(r'^\s*KOREA\s+(\d+)\s+(\d+)\s*$',text,re.M)
    counts=tuple(map(int,expected.groups())) if expected else (0,0)
    titles=[i for i,line in enumerate(lines) if line.strip()=='MSCI KOREA INDEX']
    if not titles:
        if counts!=(0,0):raise ValueError('Korea changes missing from MSCI table')
        return dict(announced_at=announcement,effective_close=close,additions=[],deletions=[])
    if len(titles)!=1:raise ValueError('Ambiguous Korea changes section')
    index=titles[0]+1
    while index<len(lines) and not lines[index].strip():index+=1
    header=lines[index]
    if not re.fullmatch(r'\s*Additions\s+Deletions\s*',header):raise ValueError('MSCI columns changed')
    split=header.index('Deletions');added=[];deleted=[]
    for line in lines[index+1:]:
        stripped=line.strip()
        if stripped.startswith('MSCI ') and stripped.endswith('INDEX'):break
        if not stripped or stripped.startswith('Page ') or 'All rights reserved' in stripped:continue
        left,right=line[:split].strip(),line[split:].strip()
        for value,target in ((left,added),(right,deleted)):
            if value and value.lower()!='none':target.append(value)
    if (len(added),len(deleted))!=counts or len(set(added))!=len(added) or len(set(deleted))!=len(deleted):
        raise ValueError('MSCI Korea row count/column validation failed')
    return dict(announced_at=announcement,effective_close=close,additions=added,deletions=deleted)


def parse_review(body,period):
    reader=PdfReader(BytesIO(body),strict=True)
    if reader.is_encrypted or not 1<=len(reader.pages)<=50:raise ValueError('Unsupported MSCI PDF')
    pieces=[]
    for page in reader.pages:
        stream=page.get_contents()
        if stream and len(stream.get_data())>8*1024*1024:raise ValueError('PDF stream exceeds parsing budget')
        pieces.append(page.extract_text(extraction_mode='layout',layout_mode_space_vertically=False))
    return parse_review_text('\n'.join(pieces),period)


def save_review(repo,period,result,body,url):
    meta=dict(fund_id='msci-korea',as_of_date=result['effective_close'],published_at=result['announced_at'],source_url=url,source_type='msci-korea')
    sha=store_document(repo,body,'application/pdf',meta)
    with repo.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        prior=c.execute("SELECT id FROM msci_reviews WHERE index_id='MSCI_KOREA_STANDARD' AND review_period=? AND checksum=?",(period,sha)).fetchone()
        if prior:return False
        obs=c.execute('SELECT id FROM source_observations WHERE checksum=? AND fund_id=? AND as_of_date=? AND source_url=? ORDER BY id DESC LIMIT 1',(sha,'msci-korea',meta['as_of_date'],url)).fetchone()[0]
        identifier=c.execute('''INSERT INTO msci_reviews(index_id,review_period,announced_at,effective_close,additions_count,deletions_count,observation_id,checksum,created_at) VALUES('MSCI_KOREA_STANDARD',?,?,?,?,?,?,?,?)''',
          (period,result['announced_at'],result['effective_close'],len(result['additions']),len(result['deletions']),obs,sha,now())).lastrowid
        c.executemany('INSERT INTO msci_review_changes VALUES(?,?,?)',[(identifier,action,name) for key,action in (('additions','ADDED'),('deletions','DELETED')) for name in result[key]])
    return True


class MSCICollector:
    def __init__(self,repo,transport):self.repo,self.transport=repo,transport
    def run(self):
        from . import next_scheduled
        self.warnings=[];count=0;checked=0
        html,_,_=self.transport.fetch(ARCHIVE,{'text/html'})
        links=review_links(html.decode('utf8'))
        existing={r['review_period'] for r in self.repo.rows('SELECT review_period FROM msci_reviews')}
        # Retry recent two reviews for revisions; immutable earlier quarters are reused.
        recent=set(sorted(links)[-2:])
        for period,url in sorted(links.items()):
            if period in existing and period not in recent:continue
            try:
                body,_,final=self.transport.fetch(url,{'application/pdf'})
                result=parse_review(body,period);checked+=1
                count+=int(save_review(self.repo,period,result,body,final))
            except Exception as error:
                self.warnings.append(period+' · 공식 변경표 검토 필요 · '+type(error).__name__)
        self.details=dict(checked_reviews=checked,new_reviews=count,errors=len(self.warnings))
        if links:self.details['latest_review_period']=max(links)
        status='partial' if self.warnings else 'success' if count else 'no_new_data'
        record_status(self.repo,'fund','msci-korea','reviews','msci-korea',status,'MSCI Korea Standard · 분기 변경표 '+str(count)+'건 · '+str(len(self.warnings))+'건 검토 필요',ARCHIVE,next_scheduled('msci-korea'))
        return count


def reviews(repo,year=None):
    args=() if year is None else (str(year)+'%',)
    rows=repo.rows('''SELECT r.*,o.source_url,o.fetched_at FROM msci_reviews r JOIN source_observations o ON o.id=r.observation_id
      WHERE r.id=(SELECT e.id FROM msci_reviews e WHERE e.index_id=r.index_id AND e.review_period=r.review_period ORDER BY e.id DESC LIMIT 1)'''+(' AND r.review_period LIKE ?' if year is not None else '')+' ORDER BY review_period DESC',args)
    for row in rows:
        changes=repo.rows('SELECT action,name FROM msci_review_changes WHERE review_id=? ORDER BY action,name',(row['id'],))
        row['additions']=[r['name'] for r in changes if r['action']=='ADDED'];row['deletions']=[r['name'] for r in changes if r['action']=='DELETED']
    return rows
