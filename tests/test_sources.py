import json
from decimal import Decimal
import pytest
from stock16.collectors.krx_etf import parse_pdf
from stock16.collectors.nbim import normalize_holdings
from stock16.collectors.gic import article
from stock16.collectors.nps import parse_home
from stock16.model import import_snapshot
from stock16.storage import store_document
from stock16.analytics import Analytics


def test_official_pdf_cash_is_not_shares():
    data={'pdf':{'gijunYMD':'20261030','totalCnt':2,'list':[
      dict(itmNo='KRD010010001',secNm='TEST cash',applyQ='-100',evalA='-100',ratio=None),
      dict(itmNo='005930',secNm='TEST stock',applyQ='10',evalA='1000',ratio='50')]}}
    day,rows=parse_pdf(data)
    assert day=='2026-10-30' and rows[0]['shares'] is None and rows[1]['shares']=='10'
    data['pdf']['totalCnt']=3
    with pytest.raises(ValueError,match='Incomplete'): parse_pdf(data)


def test_no_publication_date_idempotent(repo):
    meta=dict(fund_id='etf-069500',as_of_date='2026-10-30',published_at=None,source_url='https://www.samsungfund.com/test',source_type='krx-etf',currency='KRW',disclosure_scope='full')
    sha=store_document(repo,b'fixture','application/json',meta)
    rows=[dict(name='TEST',kr_code='005930',shares='10')]
    assert import_snapshot(repo,meta,rows,sha)[1]
    assert not import_snapshot(repo,meta,rows,sha)[1]
    store_document(repo,b'fixture','application/json',meta)
    assert len(repo.rows('SELECT * FROM source_observations'))==1
    assert Analytics(repo).holdings('etf-069500')['snapshot']['published_at'] is None


def test_nbim_issuer_aggregate_has_no_invented_shares():
    records=[dict(id='official-id',cc='US',n='TEST issuer',at=0,a=dict(e=100),o=1.2,eq=dict(s='Technology'))]
    row=normalize_holdings(records,1000)[0]
    assert row['shares'] is None and row['weight']=='10' and row['source_security_id']=='official-id:eq'


def test_gic_extractive_metadata():
    body=b'<html><meta property="article:published_time" content="2026-10-30T12:00:00+08:00"><meta name="description" content="Official TEST announcement."><h1>TEST investment</h1></html>'
    data=article(body)
    assert data['title']=='TEST investment' and data['summary']=='원문 발췌: Official TEST announcement.'
    with pytest.raises(ValueError): article(b'<h1>No date</h1>')


def test_nps_fixture_dates_and_trillion_unit():
    cats=['국내주식','해외주식','국내채권','해외채권','대체투자','단기자금']
    html='''<div class="main-portfolio"><h3>기금 포트폴리오 <span class="date">2026년 8월 말 기준</span></h3>
    <table><caption>기금 포트폴리오 구성별 규모</caption>'''
    html+=''.join('<tr><th>'+n+'</th><td>10 조 원</td><td>10 %</td></tr>' for n in cats)
    html+='''</table><a onclick="fnc_goBbsDetail('1', 'offi')"><p>자산군별 포트폴리오 운용 현황(2026.8월)</p><span class="notice-date">2026/10/30</span></a>'''
    day,published,rows,notices=parse_home(html.encode())
    assert day=='2026-08-31' and published=='2026-10-30'
    assert rows[0]['market_value']=='10000000000000'


def test_etf_observed_business_period(repo,ingest,stock):
    days=['2026-10-19','2026-10-20','2026-10-21','2026-10-22','2026-10-23','2026-10-26']
    for idx,day in enumerate(days): ingest(day,[{**stock,'shares':str(100+idx)}])
    result=Analytics(repo).changes('etf-069500','5b')
    assert result['previous']['as_of_date']=='2026-10-19'
    assert result['rows'][0]['shares_delta']=='5'


def test_sec_quarter_diff(repo,ingest,stock):
    ingest('2026-03-31',[stock],fund='berkshire',published='2026-05-15',source_type='sec-13f')
    ingest('2026-06-30',[{**stock,'shares':'80'}],fund='berkshire',published='2026-08-14',source_type='sec-13f')
    result=Analytics(repo).changes('berkshire','quarter')
    # Calendar quarter has differing day counts; latest earlier available snapshot is explicit.
    assert result['rows'][0]['shares_delta']=='-20'


def test_sec_collector_end_to_end_and_replay(repo,monkeypatch):
    from pathlib import Path
    from stock16.collectors.sec_13f import SEC13FCollector
    monkeypatch.setenv('STOCK16_SEC_USER_AGENT','TEST fixtures fixture@example.invalid')
    repo.execute("UPDATE guru_watchlist SET enabled=0 WHERE institution_id<>'berkshire'")
    xml=(Path(__file__).parent/'fixtures/sec13f.xml').read_bytes()
    class FakeSEC:
        def fetch(self,url,types):
            if 'submissions/' in url:
                payload={'cik':1067983,'filings':{'recent':dict(form=['13F-HR','13F-HR'],accessionNumber=['0001067983-26-000002','0001067983-26-000001'],reportDate=['2026-06-30','2026-03-31'],filingDate=['2026-08-14','2026-05-15'],primaryDocument=['primary.xml','primary.xml'])}}
                return json.dumps(payload).encode(),'application/json',url
            if url.endswith('index.json'):
                return b'{"directory":{"item":[{"name":"primary.xml"},{"name":"holdings.xml"}]}}','application/json',url
            if url.endswith('primary.xml'):return b'<edgarSubmission/>','application/xml',url
            result=xml.replace(b'<sshPrnamt>100</sshPrnamt>',b'<sshPrnamt>120</sshPrnamt>') if '26000002' in url else xml
            return result,'application/xml',url
    collector=SEC13FCollector(repo,FakeSEC())
    assert collector.run()==2
    assert collector.run()==0
    assert Analytics(repo).changes('berkshire')['rows'][0]['shares_delta']=='20'


def test_normalized_history_survives_raw_expiry(repo,ingest,stock):
    from datetime import date
    from stock16.storage import raw_retention
    ingest('2024-01-02',[stock]);ingest('2024-01-31',[{**stock,'shares':'120'}])
    raw_retention(repo,date(2026,10,31),apply=True)
    assert Analytics(repo).holdings('etf-069500','2024-01-02')['rows'][0]['shares']=='100'
    assert Analytics(repo).changes('etf-069500')['rows'][0]['shares_delta']=='20'
