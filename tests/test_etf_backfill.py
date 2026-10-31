import json
from datetime import date,timedelta
import pytest
from stock16.collectors.krx_etf import KRXETFCollector,planned_days
from stock16.collectors.etf_adapters import provider_for
from stock16.collectors.transport import FetchError
from stock16.analytics import Analytics


class OfficialPDF:
    """Synthetic official-shape responses, no external requests."""
    def __init__(self):
        self.dates=[]
        self.quantity='100'
        self.holidays=set()
        self.failures=set()

    def fetch(self,url,types):
        if '/main/product.do' in url:
            return json.dumps([dict(stkTicker='069500',fId='2ETF01',fNm='TEST KODEX')]).encode(),'application/json',url
        selected=url.split('gijunYMD=')[1].replace('.','-')
        self.dates.append(selected)
        if selected in self.failures: raise FetchError('TEST outage',status_code=503)
        if selected in self.holidays:
            return b'{"pdf":{"list":[],"totalCnt":0}}','application/json',url
        payload={'pdf':{'gijunYMD':selected.replace('-',''),'totalCnt':1,'list':[
          dict(itmNo='005930',secNm='TEST stock',applyQ=self.quantity,evalA='1000',ratio='10')]}}
        return json.dumps(payload).encode(),'application/json',url


def only_one(repo):
    repo.execute("UPDATE etf_watchlist SET enabled=1 WHERE fund_id='etf-069500'")
    repo.execute("UPDATE etf_watchlist SET enabled=0 WHERE fund_id<>'etf-069500'")


def test_three_day_outage_backfills_and_replays(repo,monkeypatch):
    only_one(repo)
    transport=OfficialPDF()
    collector=KRXETFCollector(repo,transport,{'kodex':{'069500':'2ETF01'}})
    monkeypatch.setenv('STOCK16_ETF_BACKFILL_DAYS','1')
    assert collector.run(today=date(2026,10,5))==1
    monkeypatch.setenv('STOCK16_ETF_BACKFILL_DAYS','30')
    transport.dates.clear()
    assert collector.run(today=date(2026,10,8))==3
    assert {'2026-10-06','2026-10-07','2026-10-08'}<=set(transport.dates)
    assert len(repo.rows('SELECT DISTINCT as_of_date FROM portfolio_snapshots'))==4
    assert collector.run(today=date(2026,10,8))==0
    assert len(repo.rows('SELECT * FROM source_documents'))==4
    assert len(repo.rows('SELECT * FROM holding_sets'))==1


def test_initial_collection_has_calendar_bound_and_skips_weekends(repo):
    today=date(2026,10,30)
    days=planned_days(repo,'etf-069500',today,30)
    assert min(days)>=today-timedelta(days=29)
    assert max(days)==today and all(day.weekday()<5 for day in days)
    assert len(days)<=22


def test_corrected_etf_revision_and_quantity_semantics(repo,monkeypatch):
    only_one(repo);monkeypatch.setenv('STOCK16_ETF_BACKFILL_DAYS','1')
    transport=OfficialPDF();collector=KRXETFCollector(repo,transport,{})
    assert collector.run(today=date(2026,10,5))==1
    transport.quantity='120'
    assert collector.run(today=date(2026,10,5))==1
    assert collector.run(today=date(2026,10,5))==0
    assert len(repo.rows('SELECT * FROM portfolio_snapshots'))==2
    holdings=Analytics(repo).holdings('etf-069500')
    assert holdings['snapshot']['quantity_type']=='pdf_per_cu_quantity'
    assert holdings['rows'][0]['quantity']=='120'
    assert len(repo.rows('SELECT * FROM source_documents'))==2


def test_missing_interior_day_retried_after_later_success(repo,monkeypatch):
    only_one(repo);monkeypatch.setenv('STOCK16_ETF_BACKFILL_DAYS','1')
    transport=OfficialPDF();collector=KRXETFCollector(repo,transport,{})
    collector.run(today=date(2026,10,5))
    monkeypatch.setenv('STOCK16_ETF_BACKFILL_DAYS','30')
    transport.failures={'2026-10-07'}
    collector.run(today=date(2026,10,8))
    assert not repo.rows("SELECT id FROM portfolio_snapshots WHERE as_of_date='2026-10-07'")
    transport.failures.clear()
    assert collector.run(today=date(2026,10,8))==1


def test_official_empty_holiday_skips_without_deletion(repo,monkeypatch):
    only_one(repo);monkeypatch.setenv('STOCK16_ETF_BACKFILL_DAYS','1')
    transport=OfficialPDF();collector=KRXETFCollector(repo,transport,{})
    collector.run(today=date(2026,10,5))
    transport.holidays={'2026-10-06'}
    monkeypatch.setenv('STOCK16_ETF_BACKFILL_DAYS','30')
    assert collector.run(today=date(2026,10,6))==0
    assert not collector.warnings
    assert len(repo.rows('SELECT * FROM portfolio_snapshots'))==1


@pytest.mark.parametrize('brand,host',[('tiger','www.tigeretf.com'),('rise','www.riseetf.co.kr'),
 ('ace','www.aceetf.co.kr'),('sol','www.soletf.co.kr'),('plus','www.plusetf.co.kr')])
def test_manager_routing_reviewed_contract_and_no_unverified_endpoint(repo,monkeypatch,brand,host):
    only_one(repo);monkeypatch.setenv('STOCK16_ETF_BACKFILL_DAYS','1')
    repo.execute('UPDATE funds SET source_url=? WHERE id=?',('https://'+host+'/','etf-069500'))
    assert provider_for('https://'+host+'/')==brand
    class File:
        def fetch(self,url,types):
            return b'code,name,quantity\n005930,TEST stock,12\n','text/csv',url
    collector=KRXETFCollector(repo,File(),{})
    assert collector.run(today=date(2026,10,5))==0
    assert repo.rows('SELECT status FROM source_status')[0]['status']=='needs_configuration'
    config={'providers':{brand:{'069500':dict(reviewed=True,date_parameter_verified=True,
      url_template='https://'+host+'/TEST-FIXTURE?date={date:%Y%m%d}',format='csv',
      mapping={'kr_code':'code','name':'name','shares':'quantity'},disclosure_scope='full')}}}
    assert KRXETFCollector(repo,File(),config).run(today=date(2026,10,5))==1
    assert Analytics(repo).holdings('etf-069500')['rows'][0]['quantity_type']=='pdf_per_cu_quantity'


def test_provider_host_matching_is_exact():
    assert provider_for('https://www.samsungfund.com.evil.example/') is None
