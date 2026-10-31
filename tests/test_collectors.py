from pathlib import Path
import json
import pytest
import requests
from stock16.collectors.parsers import table,sec_information_table,mapped_rows
from stock16.collectors.transport import Transport,FetchError
from stock16.collectors import run_collectors


def test_sec_units_and_aggregation():
    body=(Path(__file__).parent/'fixtures/sec13f.xml').read_bytes()
    current=sec_information_table(body,'2026-08-14')[0]
    assert current['shares']=='150' and current['market_value']=='1500' and current['weight']=='100'
    assert sec_information_table(body,'2022-11-14')[0]['market_value']=='1500000'


@pytest.mark.parametrize('body,fmt',[(b'<html>error</html>','csv'),(b'broken','xlsx'),(b'{bad','json'),(b'[]','json')])
def test_malformed(body,fmt):
    with pytest.raises(Exception): table(body,fmt)


def test_unexpected_mapping():
    with pytest.raises(ValueError,match='columns'): mapped_rows([{'Wrong':'A'}],{'name':'Company'})


def test_sec_xml_entity_protection():
    with pytest.raises(Exception):
        sec_information_table(b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><informationTable>&xxe;</informationTable>','2026-01-01')


def test_transport_timeout(monkeypatch):
    t=Transport();monkeypatch.setattr(t,'validate_url',lambda u:None)
    monkeypatch.setattr(t.http,'get',lambda *a,**k:(_ for _ in ()).throw(requests.Timeout('secret url key')))
    with pytest.raises(FetchError) as e: t.fetch('https://data.sec.gov/a',{'application/json'},robots=False)
    assert 'secret' not in str(e.value)


@pytest.mark.parametrize('url',['http://data.sec.gov/a','https://localhost/a','https://www.sec.gov@127.0.0.1/a','https://data.sec.gov:8080/a','https://evil.example/'])
def test_ssrf_rejected(url):
    with pytest.raises(FetchError): Transport().validate_url(url)


def test_collector_isolation_source_unavailable(repo,tmp_path):
    cfg=tmp_path/'sources.json';cfg.write_text(json.dumps({'gic':{'feeds':['https://www.gic.com.sg/feed/']}}))
    class Broken:
        def fetch(self,*a,**k): raise TimeoutError('upstream secret')
    results=run_collectors(repo,['gic','temasek'],transport_factory=Broken,config_path=cfg)
    assert [r['status'] for r in results]==['error','needs_configuration']
    assert 'secret' not in json.dumps(results)


def test_xlsx_roundtrip():
    import io
    from openpyxl import Workbook
    book=Workbook();book.active.append(['Name','ISIN']);book.active.append(['TEST','US1234567890']);out=io.BytesIO();book.save(out)
    assert table(out.getvalue(),'xlsx')[0]['Name']=='TEST'
