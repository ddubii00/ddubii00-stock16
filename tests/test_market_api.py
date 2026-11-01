import json
import re
import secrets
import pytest
from werkzeug.security import generate_password_hash
from stock16.web import create_app
from stock16.markets import import_market
from stock16.collectors.msci import save_review

@pytest.fixture
def app(tmp_path):return create_app(dict(TESTING=True,SECRET_KEY=secrets.token_hex(32),PASSWORD_HASH=generate_password_hash('TEST PASSWORD'),DATA_DIR=tmp_path/'data',SESSION_COOKIE_SECURE=True))

@pytest.fixture
def client(app):
    with app.test_client() as c:
        html=c.get('/stock16-7/login',base_url='https://localhost').text
        csrf=re.search('name="csrf" value="([^"]+)"',html)[1]
        c.post('/stock16-7/login',data={'csrf':csrf,'password':'TEST PASSWORD'},base_url='https://localhost')
        with c.session_transaction(path='/stock16-7/',base_url='https://localhost') as session:token=session['csrf']
        c.environ_base['HTTP_X_CSRF_TOKEN']=token
        yield c

@pytest.mark.parametrize('path',['market-rankings','market-rankings/status','market-rankings/trace?week=2020-01-03&rank=67','msci-reviews'])
def test_new_apis_require_authentication(app,path):
    assert app.test_client().get('/stock16-7/api/'+path).status_code==401

@pytest.mark.parametrize('query',['market=INVALID','week=broken','week=','week=20200103','week=2020-01-01','week=2019-12-27'])
def test_invalid_market_query(client,query):
    assert client.get('/stock16-7/api/market-rankings?'+query).status_code==400

@pytest.mark.parametrize('rank',['bad','0','100001'])
def test_invalid_trace_rank(client,rank):
    assert client.get('/stock16-7/api/market-rankings/trace?week=2020-01-03&rank='+rank).status_code==400

def test_no_data_reports_gaps_without_fabricating_rank(client):
    result=client.get('/stock16-7/api/market-rankings').json
    assert result['snapshot'] is None and result['status']=='not_collected' and result['rows']==[]
    statuses=client.get('/stock16-7/api/market-rankings/status').json['markets']
    assert all(s['collected_weeks']==0 and s['expected_weeks']>300 for s in statuses)
    trace=client.get('/stock16-7/api/market-rankings/trace?week=2020-01-03&rank=67').json
    assert trace['status']=='baseline_not_collected' and trace['current']==[]

def test_current_rank_and_msci_data_json(client,app):
    repo=app.extensions['repo']
    row=dict(name='TEST STOCK',ticker='TEST',source_security_id='permanent-test-1',instrument='COMMON_STOCK',exchange='NASDAQ',market_cap='10')
    meta=dict(market='NASDAQ',as_of_date='2020-01-03',currency='USD',source_type='test-provider',source_url='https://www.nasdaq.com/TEST',universe_complete=True,expected_count=1,universe_definition='TEST point-in-time stocks')
    import_market(repo,meta,[row],json.dumps([row]).encode())
    ranking=client.get('/stock16-7/api/market-rankings?market=NASDAQ').json
    assert ranking['rows'][0]['rank']==1 and ranking['snapshot']['as_of_date']=='2020-01-03'
    result=dict(announced_at='2020-02-12',effective_close='2020-02-28',additions=[],deletions=[])
    save_review(repo,'2020-02',result,b'TEST','https://app2.msci.com/TEST.pdf')
    reviews=client.get('/stock16-7/api/msci-reviews?year=2020').json['reviews']
    assert reviews[0]['effective_close']=='2020-02-28' and reviews[0]['additions']==[]
    assert client.get('/stock16-7/api/msci-reviews?year=notayear').status_code==400

def test_new_collectors_queue_only(client,app):
    for name in ('market-ranks','msci-korea'):
        response=client.post('/stock16-7/api/collect',json={'collector':name})
        assert response.status_code==202 and response.json['job']['status']=='queued'
    assert not app.extensions['repo'].rows('SELECT * FROM data_fetch_runs')
