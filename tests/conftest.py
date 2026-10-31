import pytest
from stock16.db import Repository
from stock16.storage import store_document
from stock16.model import import_snapshot


@pytest.fixture
def repo(tmp_path):
    r=Repository(tmp_path/'data');r.initialize();return r


@pytest.fixture
def ingest(repo):
    def call(day,rows,fund='etf-069500',published=None,scope='full',source_type='krx-etf',raw=None):
        meta=dict(fund_id=fund,as_of_date=day,published_at=published or day,
          source_url='https://www.samsungfund.com/test.csv',source_type=source_type,currency='KRW',disclosure_scope=scope)
        import json
        body=raw if raw is not None else json.dumps(rows,sort_keys=True).encode()
        checksum=store_document(repo,body,'application/json',meta)
        return import_snapshot(repo,meta,rows,checksum)
    return call


@pytest.fixture
def stock():
    return dict(name='TEST ONLY 삼성전자',kr_code='005930',shares='100',weight='10',market_value='1000')
