from decimal import Decimal
import pytest
from stock16.analytics import Analytics
from stock16.model import import_allocation,number
from stock16.storage import store_document


def test_import_duplicate_and_unchanged_dedup(repo,ingest,stock):
    first=ingest('2026-01-01',[stock]);assert first[1]
    assert ingest('2026-01-01',[stock])==(first[0],False)
    ingest('2026-01-02',[stock])
    assert len(repo.rows('SELECT * FROM portfolio_snapshots'))==2
    assert len(repo.rows('SELECT * FROM holding_sets'))==1
    assert len(repo.rows('SELECT * FROM holding_values'))==1
    assert len(repo.rows('SELECT * FROM source_documents'))==1
    assert not repo.rows('SELECT * FROM holding_changes')


def test_new_exit_and_quantity_diff(repo,ingest,stock):
    ingest('2026-01-01',[stock])
    ingest('2026-01-02',[{**stock,'shares':'120'},dict(name='TEST fixture B',kr_code='000660',shares='50',weight='5',market_value='500')])
    rs=Analytics(repo).changes('etf-069500')['rows']
    assert {r['status'] for r in rs}=={'INCREASED','NEW'}
    assert next(r for r in rs if r['status']=='INCREASED')['shares_delta_pct']=='20'
    ingest('2026-01-03',[dict(name='TEST fixture B',kr_code='000660',shares='40',weight='5',market_value='500')])
    assert {r['status'] for r in Analytics(repo).changes('etf-069500')['rows']}=={'EXITED','DECREASED'}


def test_no_shares_no_invented_buy(repo,ingest,stock):
    ingest('2026-01-01',[{**stock,'shares':None}],fund='nbim',source_type='nbim')
    ingest('2026-07-01',[{**stock,'shares':None,'weight':'12','market_value':'1200'}],fund='nbim',source_type='nbim')
    row=Analytics(repo).changes('nbim')['rows'][0]
    assert row['status']=='VALUE_OR_WEIGHT_CHANGED' and row['shares_delta'] is None
    assert row['portfolio_weight_delta_pp']=='2'


def test_price_does_not_mean_quantity_purchase(repo,ingest,stock):
    ingest('2026-01-01',[stock]);ingest('2026-01-02',[{**stock,'market_value':'2000','weight':'20'}])
    r=Analytics(repo).changes('etf-069500')['rows'][0]
    assert r['status']=='UNCHANGED' and r['shares_delta']=='0'


def test_split_evidence_adjusts_delta(repo,ingest,stock):
    ingest('2026-01-01',[stock]);sid=repo.rows('SELECT id FROM securities')[0]['id']
    repo.execute('INSERT INTO corporate_actions VALUES(?,?,?,?)',(sid,'2026-01-02','2','https://www.samsungfund.com/action'))
    ingest('2026-01-02',[{**stock,'shares':'200'}])
    r=Analytics(repo).changes('etf-069500')['rows'][0]
    assert r['status']=='UNCHANGED' and r['shares_delta']=='0' and r['raw_shares_delta']=='100'


def test_partial_disclosure_is_not_exit(repo,ingest,stock):
    ingest('2026-01-01',[stock],scope='partial')
    ingest('2026-01-02',[dict(name='TEST fixture B',kr_code='000660')],scope='partial')
    assert {r['status'] for r in Analytics(repo).changes('etf-069500')['rows']}=={'FIRST_DISCLOSED','LEFT_DISCLOSED_LIST'}


def test_late_backfill_rebuilds_temporal_diff(repo,ingest,stock):
    ingest('2026-01-01',[stock]);ingest('2026-01-03',[{**stock,'shares':'130'}]);ingest('2026-01-02',[{**stock,'shares':'120'}])
    assert Analytics(repo).changes('etf-069500')['rows'][0]['shares_delta']=='10'


def test_revisions_retained_effective_latest(repo,ingest,stock):
    ingest('2026-01-01',[stock],published='2026-01-02')
    ingest('2026-01-01',[{**stock,'shares':'110'}],published='2026-01-03')
    assert len(repo.rows('SELECT * FROM portfolio_snapshots'))==2
    assert Analytics(repo).holdings('etf-069500')['rows'][0]['shares']=='110'


def test_dates_distinct_and_no_first_purchase(repo,ingest,stock):
    ingest('2026-06-30',[stock],fund='berkshire',published='2026-08-14',source_type='sec-13f')
    h=Analytics(repo).holdings('berkshire')
    assert h['snapshot']['as_of_date']=='2026-06-30' and h['snapshot']['published_at']=='2026-08-14'
    assert not Analytics(repo).changes('berkshire')['rows']


def test_nps_allocation_diff(repo):
    for day,weight in [('2026-01-31','20'),('2026-02-28','25')]:
        m=dict(fund_id='nps',as_of_date=day,published_at='2026-04-30',currency='KRW',disclosure_scope='partial',source_url='https://fund.nps.or.kr/main.do',source_type='nps')
        sha=store_document(repo,day.encode(),'application/json',m)
        import_allocation(repo,m,[dict(original_category='국내주식',standard_category='domestic_equity',weight=weight,target_weight='22')],sha)
    row=Analytics(repo).allocation('nps')[-1]
    assert row['weight_delta_pp']=='5' and row['target_gap_pp']=='3'


def test_identity_never_name_merge(repo,ingest):
    ingest('2026-01-01',[dict(name='SAME NAME',cusip='123456789'),dict(name='SAME NAME',cusip='987654321')])
    assert len(repo.rows('SELECT * FROM securities'))==2


def test_identifier_conflict_is_atomic(repo,ingest):
    ingest('2026-01-01',[dict(name='A',cusip='123456789'),dict(name='B',isin='US9876543210')])
    with pytest.raises(ValueError,match='conflict'):
        ingest('2026-01-02',[dict(name='A',cusip='123456789',isin='US9876543210')])
    assert len(repo.rows('SELECT * FROM portfolio_snapshots'))==1


@pytest.mark.parametrize('value',['NaN','Infinity','1e100'])
def test_numeric_reject(value):
    with pytest.raises(ValueError): number(value)


def test_empty_import_not_liquidation(repo,ingest):
    with pytest.raises(ValueError,match='Empty'): ingest('2026-01-01',[])


def test_sector_consensus_keeps_asof(repo,ingest,stock):
    from datetime import date,timedelta
    first=(date.today()-timedelta(days=3)).isoformat();last=date.today().isoformat()
    for fund in ('nps','nbim','etf-069500'):
        ingest(first,[stock],fund=fund);ingest(last,[{**stock,'shares':'120'}],fund=fund)
    rows=Analytics(repo).consensus(minimum=3)
    assert rows[0]['institution_count']==2 and rows[0]['etf_count']==1
    assert all(o['as_of_date']==last for o in rows[0]['observations'])
