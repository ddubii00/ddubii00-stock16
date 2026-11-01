import json
from datetime import date,datetime,timedelta,timezone
import pytest
from stock16.markets import import_market,MarketAnalytics,closed_week,week_end,weeks_between
from stock16.collectors.market_ranks import parse_krx,MarketRankCollector
from stock16.collectors.base import NeedsConfiguration
from stock16.jobs import enqueue,work_once


def stockrow(i,market='KOSPI',cap=None):
    code=str(100000+i)
    return dict(name='TEST STOCK '+code,ticker=code,kr_code=code,exchange=market,instrument='COMMON_STOCK',market_cap=str(100000-i*100 if cap is None else cap))


@pytest.fixture
def market_import(repo):
    def call(day='2020-01-03',rows=None,market='KOSPI',**override):
        rows=rows if rows is not None else [stockrow(i,market) for i in range(200)]
        meta=dict(market=market,as_of_date=day,source_url='https://openapi.krx.co.kr/TEST',source_type='test-market',currency='USD' if market=='NASDAQ' else 'KRW',universe_complete=True,expected_count=len(rows),universe_definition='TEST stock universe, ETF excluded')
        meta.update(override)
        body=json.dumps(rows,sort_keys=True).encode()
        return import_market(repo,meta,rows,body)
    return call


def test_first_week_and_local_closing_boundaries():
    assert week_end('2020-01-02')==date(2020,1,3)
    assert weeks_between('2020-01-03','2020-01-17')==['2020-01-03','2020-01-10','2020-01-17']
    assert closed_week('NASDAQ',datetime(2026,10,30,19,59,tzinfo=timezone.utc))==date(2026,10,23)
    assert closed_week('NASDAQ',datetime(2026,10,30,20,1,tzinfo=timezone.utc))==date(2026,10,30)
    with pytest.raises(ValueError):weeks_between('2020-01-01','2020-01-17')


def test_exact_rank_67_can_become_180(repo,market_import):
    baseline=[stockrow(i) for i in range(200)];market_import(rows=baseline)
    current=[dict(row) for row in baseline];others=[r for r in current if r['kr_code']!='100066']
    current[66]['market_cap']=str((int(others[178]['market_cap'])+int(others[179]['market_cap']))//2)
    market_import('2026-10-30',current)
    trace=MarketAnalytics(repo).trace('KOSPI','2020-01-03',67)
    assert trace['baseline']['ticker']=='100066' and trace['current'][0]['current_rank']==180
    assert [r['rank'] for r in trace['history']]==[67,180]
    assert len(repo.rows('SELECT * FROM securities'))==200


def test_etf_etn_and_funds_cannot_enter_top100(repo,market_import):
    rows=[stockrow(i) for i in range(120)]
    rows+=[{**stockrow(900+i,cap=999999999),'instrument':kind} for i,kind in enumerate(['ETF','ETN','FUND'])]
    market_import(rows=rows)
    a=MarketAnalytics(repo);result=a.ranking('KOSPI')
    assert len(result['rows'])==100 and result['snapshot']['universe_count']==120
    assert result['snapshot']['excluded_count']==3
    assert not any(r['instrument'] in ('ETF','ETN','FUND') for r in result['rows'])
    assert len(repo.rows('SELECT * FROM securities'))==120


def test_preferred_shares_are_distinct_listed_securities(repo,market_import):
    rows=[stockrow(1),{**stockrow(2),'name':'TEST PREFERRED','instrument':'PREFERRED_STOCK'}]
    market_import(rows=rows)
    assert len(MarketAnalytics(repo).ranking('KOSPI')['rows'])==2


def test_entry_and_exit_have_full_market_ranks(repo,market_import):
    market_import()
    order=list(range(200));order.remove(150);order.insert(20,150);order.remove(90);order.insert(140,90)
    market_import('2020-01-10',[stockrow(i,cap=100000-place*100) for place,i in enumerate(order)])
    result=MarketAnalytics(repo).ranking('KOSPI')
    entry=next(r for r in result['entered'] if r['ticker']=='100150')
    exit=next(r for r in result['exited'] if r['ticker']=='100090')
    assert entry['previous_rank']==151 and entry['rank']<=100
    assert exit['rank']>100 and exit['status']=='LEFT_TOP100'


def test_missing_week_never_compared_as_adjacent(repo,market_import):
    market_import();market_import('2020-01-17')
    result=MarketAnalytics(repo).ranking('KOSPI')
    assert result['expected_previous_week']=='2020-01-10' and not result['comparison_available']
    assert result['entered']==[] and result['exited']==[]
    assert all(r['status']=='BASELINE' and r['rank_change'] is None for r in result['rows'])
    assert MarketAnalytics(repo).ranking('KOSPI','2020-01-10')['status']=='not_collected'


def test_missing_security_not_called_delisted(repo,market_import):
    market_import();market_import('2020-01-10',[stockrow(i) for i in range(1,200)])
    result=MarketAnalytics(repo).ranking('KOSPI')
    assert result['exited'][0]['status']=='NOT_IN_MARKET_UNIVERSE'
    trace=MarketAnalytics(repo).trace('KOSPI','2020-01-03',1)
    assert trace['status']=='not_in_latest_universe' and trace['current']==[]


def test_market_transfer_and_name_change_use_stable_id(repo,market_import):
    market_import(rows=[stockrow(1)],market='KOSPI')
    market_import('2026-10-30',rows=[stockrow(2)],market='KOSPI')
    market_import('2026-10-30',rows=[{**stockrow(1,'KOSDAQ'),'name':'TEST RENAMED','ticker':'NEW LABEL'}],market='KOSDAQ')
    trace=MarketAnalytics(repo).trace('KOSPI','2020-01-03',1)
    assert trace['current'][0]['current_market']=='KOSDAQ'
    assert trace['current'][0]['current_name']=='TEST RENAMED'
    assert len(trace['current'])==1 and len(trace['history'])==2
    assert len(repo.rows("SELECT * FROM securities WHERE kr_code='100001'"))==1


def test_same_file_dedup_and_corrected_revision(repo,market_import):
    assert market_import()[1]
    assert not market_import()[1]
    changed=[stockrow(i) for i in range(200)];changed[199]['market_cap']='999999'
    assert market_import(rows=changed)[1]
    assert MarketAnalytics(repo).ranking('KOSPI')['rows'][0]['ticker']=='100199'
    assert len(repo.rows('SELECT * FROM market_snapshots'))==2


@pytest.mark.parametrize('overrides',[dict(universe_complete=False),dict(expected_count=100),dict(currency='USD'),dict(universe_definition=''),dict(as_of_date='2020-01-04')])
def test_reject_incomplete_invalid_market_metadata(market_import,overrides):
    with pytest.raises(ValueError):market_import(**overrides)


@pytest.mark.parametrize('rowchanges',[dict(instrument=None),dict(instrument='UNKNOWN'),dict(market_cap='-1'),dict(market_cap=None),dict(exchange='NASDAQ'),dict(name='')])
def test_reject_bad_rows(market_import,rowchanges):
    with pytest.raises(ValueError):market_import(rows=[{**stockrow(1),**rowchanges}])


def test_ticker_only_identity_and_duplicate_ids_rejected(market_import):
    row=stockrow(1);row.pop('kr_code')
    with pytest.raises(ValueError):market_import(rows=[row])
    with pytest.raises(ValueError):market_import(rows=[stockrow(1),stockrow(1)])


def test_nasdaq_stable_source_identity_and_usd(repo,market_import):
    row=dict(name='TEST OLD',ticker='OLD',source_security_id='permanent-security-77',exchange='NASDAQ',instrument='COMMON_STOCK',market_cap='1000.25')
    market_import(rows=[row],market='NASDAQ')
    market_import('2026-10-30',rows=[{**row,'ticker':'NEW','name':'TEST NEW'}],market='NASDAQ')
    trace=MarketAnalytics(repo).trace('NASDAQ','2020-01-03',1)
    assert trace['current'][0]['current_name']=='TEST NEW'
    assert len(repo.rows('SELECT * FROM securities'))==1


def test_stale_latest_not_presented_as_current(repo,market_import):
    market_import()
    trace=MarketAnalytics(repo).trace('KOSPI','2020-01-03',67)
    assert trace['status']=='latest_data_missing'
    assert not trace['current'][0]['current_data_complete']


def test_coverage_and_missing_weeks(repo,market_import):
    market_import();s=MarketAnalytics(repo).status()[0]
    assert s['collected_weeks']==1 and s['first_week']=='2020-01-03'
    assert '2020-01-10' in s['missing_weeks'] and s['expected_weeks']>300


def test_krx_contract_rejects_sample_and_wrong_dates():
    day=date(2020,1,3)
    def payload(count=500):return {'OutBlock_1':[dict(BAS_DD='20200103',ISU_CD=str(100000+i),ISU_NM='TEST',MKT_NM='KOSPI',MKTCAP='100') for i in range(count)]}
    assert len(parse_krx(payload(),'KOSPI',day))==500
    with pytest.raises(ValueError):parse_krx(payload(3),'KOSPI',day)
    with pytest.raises(ValueError):parse_krx(payload(),'KOSDAQ',day)
    with pytest.raises(ValueError):parse_krx(payload(),'KOSPI',date(2020,1,2))
    with pytest.raises(NeedsConfiguration):parse_krx({'respMsg':'SECRET'},'KOSPI',day)


def test_krx_holiday_uses_same_week_last_trading_day(repo,monkeypatch):
    monkeypatch.setenv('STOCK16_KRX_API_KEY','TEST KEY')
    class Client:
        def fetch(self,url,types):
            day=url.rsplit('=',1)[1]
            rows=[] if day=='20200103' else [dict(BAS_DD=day,ISU_CD=str(100000+i),ISU_NM='TEST',MKT_NM='KOSPI',MKTCAP=str(100000-i)) for i in range(500)]
            return json.dumps({'OutBlock_1':rows}).encode(),'application/json',url
    c=MarketRankCollector(repo,Client(),{})
    assert c.run(start='2020-01-03',end='2020-01-03',markets=['KOSPI'])==1
    snap=MarketAnalytics(repo).snapshot('KOSPI')
    assert snap['as_of_date']=='2020-01-02' and snap['week_end']=='2020-01-03'


def test_no_credentials_does_not_fabricate_history(repo,monkeypatch):
    monkeypatch.delenv('STOCK16_KRX_API_KEY',raising=False)
    class UnavailablePublic:
        def fetch(self,*args):raise OSError('TEST unavailable')
    collector=MarketRankCollector(repo,UnavailablePublic(),{})
    assert collector.run()==0
    assert not repo.rows('SELECT * FROM market_snapshots')
    states={r['entity_id']:r['status'] for r in repo.rows("SELECT * FROM source_status WHERE component='market-ranks'")}
    assert states=={'market-kospi':'error','market-kosdaq':'error','market-nasdaq':'needs_configuration'}
    assert collector.details['errors']==1


def test_new_collectors_pass_queue_allowlist(repo):
    assert enqueue(repo,'market-ranks')[1] and enqueue(repo,'msci-korea')[1]


def test_equal_caps_are_stably_ranked(repo,market_import):
    rows=[stockrow(i,cap='10.10') for i in (2,1,3)]
    market_import(rows=rows)
    order=[r['ticker'] for r in MarketAnalytics(repo).ranking('KOSPI')['rows']]
    market_import('2020-01-10',rows=list(reversed(rows)))
    assert [r['ticker'] for r in MarketAnalytics(repo).ranking('KOSPI')['rows']]==order
