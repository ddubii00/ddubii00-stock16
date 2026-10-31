"""Active issuer data contracts; synthetic responses, no live network."""
from datetime import date
from io import BytesIO
import json
import pytest
from openpyxl import Workbook
from stock16.collectors.active_etf import TimeAdapter,KoactAdapter,ArkAdapter,JpmorganAdapter,pdf_position
from stock16.collectors.krx_etf import KRXETFCollector
from stock16.collectors.base import NeedsConfiguration
from stock16.etfs import catalog,in_focus
from stock16.model import import_snapshot
from stock16.storage import store_document,raw_retention
from stock16.analytics import Analytics

class Reply:
    def __init__(self,payload,media='application/json'): self.payload,self.media=payload,media;self.urls=[]
    def fetch(self,url,types):
        self.urls.append(url)
        body=self.payload if isinstance(self.payload,bytes) else json.dumps(self.payload).encode()
        return body,self.media,url


def test_active_default_universe_preserves_passive(repo):
    active=catalog(repo,'active')
    assert len(active)==13 and {r['listing_country'] for r in active}=={'KR','US'}
    assert all(r['enabled'] and r['manager'] and r['evidence_url'] for r in active)
    assert len(catalog(repo,'passive'))==13
    assert all(not r['enabled'] for r in catalog(repo,'passive'))
    assert 'etf-069500' not in in_focus(repo)


def test_disabled_state_survives_reinitialization(repo):
    repo.execute("UPDATE etf_watchlist SET enabled=0 WHERE fund_id='etf-jgro'")
    repo.initialize()
    assert 'etf-jgro' not in in_focus(repo)


def test_passive_history_still_readable_but_outside_dashboard(repo,ingest,stock):
    ingest('2026-10-30',[stock]);ingest('2026-10-31',[{**stock,'shares':'120'}])
    assert Analytics(repo).holdings('etf-069500')['rows'][0]['shares']=='120'
    assert not Analytics(repo).consensus(minimum=1,mode='held',etf_scope='active')
    assert Analytics(repo).consensus(minimum=1,mode='held',etf_scope='all')


def test_time_full_workbook_and_currency_units(repo):
    w=Workbook();ws=w.active
    ws.append(['종목코드','종목명','수량','평가금액(원)','비중(%)'])
    ws.append(['005930','TEST 삼성',100,1000,20]);ws.append(['PLTR US EQUITY','TEST PLTR',2.5,200,4]);ws.append([None,'현금',200,200,4])
    stream=BytesIO();w.save(stream)
    a=TimeAdapter(repo,Reply(stream.getvalue(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),{})
    r=a.fetch_day({'code':'385720'},date(2026,10,30),'11')
    assert r['disclosure_scope']=='full' and r['quantity_type']=='pdf_per_cu_quantity'
    assert len(r['rows'])==3 and r['rows'][1]['shares']=='2.5'
    assert r['rows'][2]['shares'] is None and r['deduplicate_state']


def test_time_product_identity_mismatch(repo):
    a=TimeAdapter(repo,Reply(b'<span class="prdNum"><span>123456</span>','text/html'),{})
    with pytest.raises(NeedsConfiguration):a.prepare({'code':'385720','source_url':'https://www.timeetf.co.kr/m11_view.php?idx=11'})


def test_koact_foreign_equity_quantity_and_date_validation(repo):
    payload={'pdf':{'gijunYMD':'20261030','totalCnt':1,'list':[dict(itmNo='PLTR US Equity',secNm='TEST PLTR',applyQ='12.5',ratio='10',evalA='1000')]}}
    a=KoactAdapter(repo,Reply(payload),{})
    assert a.fetch_day({},date(2026,10,30),'2ETFQ1')['rows'][0]['shares']=='12.5'
    assert a.fetch_day({},date(2026,10,31),'2ETFQ1') is None
    with pytest.raises(ValueError):a.fetch_day({},date(2026,10,29),'2ETFQ1')
    payload['pdf']['totalCnt']=2
    with pytest.raises(ValueError):a.fetch_day({},date(2026,10,30),'2ETFQ1')


def jpm_payload():
    return {'fundData':{'numberOfHoldings':1,'dailyHoldingsAll':{'effectiveDate':'2026-10-30','data':[
      dict(securityId='67066G104',securityDescription='TEST NVIDIA',securityTicker='NVDA',securityType='DOMESTIC COMMON STOCK',shares=100,marketValue=1000,netAssetValuePercent=99,navDate='2026-10-30',currencyCode='USD'),
      dict(securityId='CASH',securityDescription='TEST Cash',securityType='CURRENCIES',shares=10,marketValue=10,netAssetValuePercent=1,navDate='2026-10-30',currencyCode='USD')]}}}


def test_jpmorgan_full_latest_only_and_no_cusip_guess(repo):
    transport=Reply(jpm_payload());a=JpmorganAdapter(repo,transport,{})
    result=a.fetch_latest({'code':'JGRO'},'46654Q609',date(2026,10,31))
    assert result['quantity_type']=='fund_reported_quantity' and result['disclosure_scope']=='full'
    assert result['rows'][0]['shares']==100 and result['rows'][1]['shares'] is None
    assert 'cusip' not in result['rows'][0] and a.latest_only
    assert result['as_of_date']=='2026-10-30'


@pytest.mark.parametrize('fault',['future','mixed_dates','short','identifier'])
def test_jpmorgan_rejects_invalid_full_data(repo,fault):
    payload=jpm_payload()
    if fault=='mixed_dates':payload['fundData']['dailyHoldingsAll']['data'][0]['navDate']='2026-10-29'
    elif fault=='short':payload['fundData']['numberOfHoldings']=10
    elif fault=='identifier':del payload['fundData']['dailyHoldingsAll']['data'][0]['securityId']
    with pytest.raises(ValueError):JpmorganAdapter(repo,Reply(payload),{}).fetch_latest({},'46654Q609',date(2026,10,29) if fault=='future' else date(2026,10,31))


def test_jpmorgan_catalogue_ticker_identity(repo):
    a=JpmorganAdapter(repo,Reply([dict(ticker='JGRO',fundType='etf',baseCurrencyCode='USD',cusip='46654Q609')]),{})
    assert a.prepare({'code':'JGRO'})=='46654Q609'
    with pytest.raises(NeedsConfiguration):a.prepare({'code':'JAVA'})


def test_ark_partial_no_html_archive_no_private_identifier_guess(repo):
    html=b'<p>As of 10/30/2026</p><table><tr><th>Ticker</th><th>Name</th><th>Market Value</th><th>Weight (%)</th></tr><tr><td>TSLA</td><td>TEST Tesla</td><td>$1,000</td><td>10%</td></tr><tr><td>-</td><td>TEST Private</td><td>$100</td><td>1%</td></tr></table>'
    transport=Reply(html,'text/html');r=ArkAdapter(repo,transport,{}).fetch_latest({'code':'ARKK'},'1',date(2026,10,31))
    assert r['disclosure_scope']=='partial' and r['quantity_type']=='unavailable'
    assert r['rows'][0]['shares'] is None and len(r['rows'])==1
    assert r['media_type']=='application/json' and '식별자 미공개 1개' in r['notice']
    assert not any('assets.ark-funds.com' in u for u in transport.urls)
    assert len(json.loads(r['body'])['rows'])==2


def test_same_state_new_workbook_bytes_does_not_create_revision(repo,stock):
    meta=dict(fund_id='etf-385720',as_of_date='2026-10-30',published_at=None,source_url='https://www.timeetf.co.kr/TEST',source_type='time-etf',currency='KRW',disclosure_scope='full',quantity_type='pdf_per_cu_quantity',deduplicate_state=True)
    for raw,expected in [(b'zip-time1',True),(b'zip-time2',False)]:
        sha=store_document(repo,raw,'application/json',meta)
        assert import_snapshot(repo,meta,[stock],sha)[1] is expected
    assert len(repo.rows('SELECT * FROM portfolio_snapshots'))==1
    assert len(repo.rows('SELECT * FROM source_observations'))==2


def test_active_raw_retention_uses_etf_kind(repo,stock):
    for day in ('2024-01-02','2024-01-31'):
        meta=dict(fund_id='etf-jgro',as_of_date=day,published_at=None,source_url='https://am.jpmorgan.com/TEST',source_type='jpmorgan-etf',currency='USD',disclosure_scope='full',quantity_type='fund_reported_quantity')
        sha=store_document(repo,day.encode(),'application/json',meta);import_snapshot(repo,meta,[stock],sha)
    assert raw_retention(repo,date(2026,10,31),apply=True)['files']==1
    assert len(repo.rows('SELECT * FROM portfolio_snapshots'))==2


def test_latest_only_collector_does_not_repeat_as_backfill(repo,monkeypatch):
    repo.execute("UPDATE etf_watchlist SET enabled=(fund_id='etf-jgro')")
    calls=[]
    class Adapter:
        latest_only=True
        def prepare(self,fund): return 'reviewed'
        def fetch_latest(self,fund,context,today):
            calls.append(today)
            return dict(body=b'TEST',media_type='application/json',source_url='https://am.jpmorgan.com/TEST',as_of_date='2026-10-30',published_at=None,rows=[dict(name='TEST',cusip='67066G104',shares='100',weight='5')],currency='USD',disclosure_scope='full',quantity_type='fund_reported_quantity',source_type='jpmorgan-etf')
    monkeypatch.setattr('stock16.collectors.etf_adapters.make_adapters',lambda *a:{'jpmorgan':Adapter()})
    c=KRXETFCollector(repo,None,{})
    assert c.run(today=date(2026,10,31))==1 and len(calls)==1
    assert c.details['new_snapshots']==1 and c.details['checked_funds']==1 and c.details['errors']==0


def test_same_manager_etfs_not_independent_institutions(repo,ingest,stock):
    for fund in ('etf-385720','etf-495060'):
        ingest('2026-09-30',[stock],fund=fund);ingest('2026-10-30',[{**stock,'shares':'110'}],fund=fund)
    r=Analytics(repo).consensus('1m',2,etf_scope='active')[0]
    assert r['institution_count']==0 and r['etf_count']==2 and r['etf_manager_count']==1


def test_negative_official_us_cash_weight_only(repo):
    meta=dict(fund_id='etf-java',as_of_date='2026-10-30',published_at=None,source_url='https://am.jpmorgan.com/TEST',source_type='jpmorgan-etf',currency='USD',disclosure_scope='full',quantity_type='fund_reported_quantity')
    cash=dict(name='TEST Cash',source_security_id='CASH',instrument='CURRENCIES',shares=None,weight='-0.02',market_value='-100')
    sha=store_document(repo,b'TEST signed cash','application/json',meta)
    assert import_snapshot(repo,meta,[cash],sha)[1]
    assert Analytics(repo).holdings('etf-java')['rows'][0]['weight']=='-0.02'
    for changes in [dict(instrument='equity'),dict(shares='-1'),dict(shares='1'),dict(weight='-101')]:
        body=json.dumps(changes).encode();sha=store_document(repo,body,'application/json',meta)
        with pytest.raises(ValueError):import_snapshot(repo,meta,[{**cash,**changes}],sha)
