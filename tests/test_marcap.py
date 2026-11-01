"""Public archive contracts: dated universes, units, integrity and deduplication."""
import gzip
import hashlib
from datetime import date,datetime
from io import BytesIO
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from stock16.collectors.marcap import MarcapArchive,archive_dates,selected_rows,normalize_archive_row
from stock16.collectors.market_ranks import MarketRankCollector
from stock16.markets import MarketAnalytics,import_market


def parquet(rows):
    output=BytesIO();pq.write_table(pa.Table.from_pylist(rows),output);return output.getvalue()


def row(day='2020-01-03',code='5930',market='KOSPI',price=55200,shares=5969782550):
    return dict(Date=datetime.fromisoformat(day),Code=code,Name='TEST archive stock',Market=market,Marcap=price*shares,Close=price,Stocks=shares,Volume=0)


def info(body,ref='a'*40):
    return dict(blob_sha=hashlib.sha1(b'blob '+str(len(body)).encode()+b'\0'+body).hexdigest(),size=len(body),ref=ref)


def test_date_selection_preserves_zero_volume_and_maps_global_board():
    body=parquet([row(),row('2020-01-02'),row(code='100001',market='KOSDAQ GLOBAL'),row(code='100002',market='KONEX')])
    assert archive_dates(body)=={'2020-01-02','2020-01-03'}
    selected=list(selected_rows(body,{'2020-01-03'}))
    assert [m for _,m in selected]==['KOSPI','KOSDAQ']
    normalized=normalize_archive_row(*selected[0])
    assert normalized['kr_code']=='005930' and normalized['market_cap']=='329531996760000'
    assert normalized['instrument']=='STOCK'


@pytest.mark.parametrize('change',[dict(Marcap=329531996),dict(Stocks=None),dict(Close=-1),dict(Code='invalid')])
def test_wrong_units_missing_values_and_bad_identity_rejected(change):
    with pytest.raises(ValueError):normalize_archive_row({**row(),**change},'KOSPI')


def test_cache_integrity_and_single_compressed_version_per_year(repo):
    class Client:
        body=b'TEST archive v1';calls=0
        def fetch(self,url,types):self.calls+=1;return self.body,'application/octet-stream',url
    client=Client();archive=MarcapArchive(repo,client)
    assert archive.body(2020,info(client.body))[0]==client.body
    assert archive.body(2020,info(client.body))[0]==client.body and client.calls==1
    client.body=b'TEST archive v2'
    archive.body(2020,info(client.body,'b'*40))
    assert len(list(archive.folder.glob('*.parquet.gz')))==1
    assert gzip.decompress((archive.folder/'2020.parquet.gz').read_bytes())==client.body
    with pytest.raises(ValueError):archive.body(2021,{**info(client.body),'blob_sha':'c'*40})
    assert not (archive.folder/'2021.parquet.gz').exists()


def test_no_key_uses_public_history_and_late_thursday_is_not_friday(repo,monkeypatch):
    monkeypatch.delenv('STOCK16_KRX_API_KEY',raising=False)
    rows=[row(day,code=str(100000+i),market=m,price=1000-i,shares=100)
          for day in ('2020-01-03','2020-01-09') for m in ('KOSPI','KOSDAQ GLOBAL') for i in range(500)]
    body=parquet(rows);metadata={**info(body), 'source_url':'https://raw.githubusercontent.com/FinanceData/marcap/TEST.parquet'}
    monkeypatch.setattr(MarcapArchive,'manifest',lambda self:{2020:info(body)})
    monkeypatch.setattr(MarcapArchive,'body',lambda self,year,details:(body,metadata))
    c=MarketRankCollector(repo,None,{})
    assert c.run(start='2020-01-03',end='2020-01-10',markets=['KOSPI','KOSDAQ'])==2
    a=MarketAnalytics(repo)
    for market in ('KOSPI','KOSDAQ'):
        assert a.snapshot(market)['week_end']=='2020-01-03'
        assert a.snapshot(market)['data_quality']=='Secondary'
        assert a.snapshot(market,'2020-01-10') is None
    assert c.run(start='2020-01-03',end='2020-01-10',markets=['KOSPI','KOSDAQ'])==0
    assert len(repo.rows('SELECT * FROM market_snapshots'))==2


def test_archive_binary_revision_does_not_duplicate_identical_rank_state(repo):
    rows=[normalize_archive_row(row(),'KOSPI')]
    meta=dict(market='KOSPI',as_of_date='2020-01-03',currency='KRW',source_type='marcap-archive',source_url='https://raw.githubusercontent.com/FinanceData/marcap/TEST.parquet',universe_complete=True,expected_count=1,universe_definition='TEST full dated universe')
    assert import_market(repo,meta,rows,b'TEST annual archive ref1')[1]
    assert not import_market(repo,meta,rows,b'TEST annual archive ref2')[1]
    assert len(repo.rows('SELECT * FROM market_snapshots'))==1
    assert len(repo.rows('SELECT * FROM source_observations'))==2


def test_weekly_next_schedule_and_future_backfill_guard(repo):
    from stock16.collectors import next_scheduled
    for name in ('market-ranks','msci-korea'):
        scheduled=datetime.fromisoformat(next_scheduled(name))
        assert scheduled.weekday()==6 and (scheduled.hour,scheduled.minute)==(9,30)
    with pytest.raises(ValueError):MarketRankCollector(repo,None,{}).run(end='2099-01-02',markets=['KOSPI'])
