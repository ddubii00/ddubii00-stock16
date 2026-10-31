import json
from pathlib import Path
import pytest
from stock16.collectors.nbim import NBIMCollector,BASE
from stock16.collectors.sec_13f import SEC13FCollector
from stock16.collectors.transport import FetchError


class NBIMFiles:
    def __init__(self): self.urls=[]
    def fetch(self,url,types):
        self.urls.append(url)
        dates=['2026-06-30','2025-12-31','2024-12-31']
        if url.endswith('init.json'):
            payload={'data':{'details':[dict(date=d,fileName=d+'.json') for d in dates],
               'nav':[dict(a='eq',d=d,v=dict(e=100,n=1000)) for d in dates]}}
        elif 'report/' in url:
            return b'TEST official-shape CSV\n','text/csv',url
        else:
            payload={'data':[dict(id='official-TEST-id',cc='US',n='TEST issuer',at=0,a=dict(e=100,n=1000),eq=dict(s='Technology'))]}
        return json.dumps(payload).encode(),'application/json',url


def config_for(*dates):
    return {'publication_dates':{d:dict(published_at=str(int(d[:4])+1)+'-02-01',source_url='https://www.nbim.no/TEST-evidence/') for d in dates}}


def test_nbim_only_explicit_history_walks_older_datasets(repo):
    transport=NBIMFiles()
    config=config_for('2026-06-30','2025-12-31','2024-12-31')
    # A valid half-year publication date is explicitly reviewed in the fixture.
    config['publication_dates']['2026-06-30']['published_at']='2026-08-12'
    collector=NBIMCollector(repo,transport,config)
    collector.run()
    assert len(repo.rows('SELECT * FROM portfolio_snapshots'))==2
    assert not any('2024-12-31.json' in url for url in transport.urls)
    collector.run(history=True)
    assert len(repo.rows('SELECT * FROM portfolio_snapshots'))==3
    assert collector.run(history=True)==0


def test_nbim_unverified_publication_is_reviewed_not_ingested(repo):
    collector=NBIMCollector(repo,NBIMFiles(),config_for('2025-12-31'))
    collector.run(history=True)
    assert len(repo.rows('SELECT * FROM portfolio_snapshots'))==1
    assert {r['as_of_date'] for r in repo.rows('SELECT * FROM history_review_queue')}=={'2026-06-30','2024-12-31'}
    assert all(r['shares'] is None for r in repo.rows('SELECT * FROM holding_values'))


class SECFiles:
    def __init__(self): self.history_requests=0
    def fetch(self,url,types):
        def page(year,accession):
            return dict(form=['13F-HR'],accessionNumber=[accession],reportDate=[str(year)+'-12-31'],
              filingDate=[str(year+1)+'-02-14'],primaryDocument=['primary.xml'])
        if url.endswith('-submissions-001.json'):
            self.history_requests+=1
            return json.dumps(page(2015,'0001067983-16-000001')).encode(),'application/json',url
        if '/submissions/' in url:
            data=dict(cik=1067983,filings=dict(recent=page(2025,'0001067983-26-000001'),files=[dict(name='CIK0001067983-submissions-001.json',filingTo='2016-02-14')]))
            return json.dumps(data).encode(),'application/json',url
        if url.endswith('index.json'):
            return b'{"directory":{"item":[{"name":"primary.xml"},{"name":"holdings.xml"}]}}','application/json',url
        if url.endswith('primary.xml'): return b'<edgarSubmission/>','application/xml',url
        body=(Path(__file__).parent/'fixtures/sec13f.xml').read_bytes()
        return body,'application/xml',url


def test_sec_explicit_history_filter_cache_and_idempotence(repo,monkeypatch):
    monkeypatch.setenv('STOCK16_SEC_USER_AGENT','TEST fixture@example.invalid')
    repo.execute("UPDATE guru_watchlist SET enabled=0 WHERE institution_id<>'berkshire'")
    transport=SECFiles();collector=SEC13FCollector(repo,transport)
    assert collector.run()==1 and transport.history_requests==0
    assert collector.run(history=True,guru_id='berkshire',from_year=2015)==1
    assert collector.run(history=True,guru_id='berkshire',from_year=2015)==0
    assert transport.history_requests==1
    assert len(repo.rows('SELECT * FROM portfolio_snapshots'))==2
    assert len(list((repo.root/'cache').rglob('*.gz')))==1


def test_sec_backoff_never_bypasses_403(repo,monkeypatch):
    delays=[];monkeypatch.setattr('stock16.collectors.sec_13f.time.sleep',delays.append)
    class Throttled:
        calls=0
        def fetch(self,url,types):
            self.calls+=1
            if self.calls<3: raise FetchError('TEST throttled',status_code=429)
            return b'{}','application/json',url
    transport=Throttled();collector=SEC13FCollector(repo,transport)
    assert collector.fetch('https://data.sec.gov/TEST',{'application/json'})[0]==b'{}'
    assert delays==[2,4]
    class Forbidden:
        calls=0
        def fetch(self,*args):
            self.calls+=1;raise FetchError('TEST forbidden',status_code=403)
    forbidden=Forbidden()
    with pytest.raises(FetchError): SEC13FCollector(repo,forbidden).fetch('https://data.sec.gov/TEST',{'application/json'})
    assert forbidden.calls==1


def test_sec_truncated_cache_is_refetched(repo):
    import gzip,hashlib
    transport=SECFiles();collector=SEC13FCollector(repo,transport)
    url='https://data.sec.gov/submissions/CIK0001067983-submissions-001.json'
    folder=repo.root/'cache/sec-history';folder.mkdir(parents=True)
    path=folder/(hashlib.sha256(url.encode()).hexdigest()+'.json.gz')
    path.write_bytes(gzip.compress(b'{"TEST":1}')[:-5])
    assert 'form' in json.loads(collector.cached_history(url))
    assert transport.history_requests==1
