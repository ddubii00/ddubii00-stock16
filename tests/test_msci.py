import json
from pathlib import Path
import gzip
import sqlite3
import tempfile
import pytest
from stock16.collectors.msci import parse_review_text,review_links,save_review,reviews,MSCICollector
from stock16.collectors.transport import Transport
from stock16.db import Repository
from stock16.migrations import V2,V3,V4


def text_fixture(korea=True):
    header='Geneva, August 12, 2026\nMSCI GLOBAL STANDARD INDEXES\nThe following changes take place as of the close of August 31, 2026.\nSUMMARY PER COUNTRY and PER REGION:\nUSA 1 0\n'
    if not korea:return header
    return header+'KOREA 1 2\nMSCI KOREA INDEX\n'+f"{'Additions':<40}Deletions\n"+f"{'TEST ADD':<40}TEST DELETE A\n"+f"{'':<40}TEST DELETE B\n"+'MSCI INDIA INDEX\n'


def test_msci_two_columns_and_dates():
    result=parse_review_text(text_fixture(),'2026-08')
    assert result['additions']==['TEST ADD'] and result['deletions']==['TEST DELETE A','TEST DELETE B']
    assert result['announced_at']=='2026-08-12' and result['effective_close']=='2026-08-31'
    assert parse_review_text(text_fixture(False),'2026-08')['additions']==[]


def test_msci_reject_mismatched_counts_and_family():
    for bad in [text_fixture().replace('KOREA 1 2','KOREA 2 2'),text_fixture().replace('MSCI GLOBAL STANDARD INDEXES','SMALL CAP'),text_fixture().replace('August 31, 2026','July 31, 2026'),text_fixture().replace('MSCI KOREA INDEX','OTHER')]:
        with pytest.raises(ValueError):parse_review_text(bad,'2026-08')


def test_msci_public_archive_links_only():
    html='<a href="/eqb/gimi/stdindex/MSCI_Aug26_STPublicList.pdf">TEST</a><a href="/eqb/gimi/stdindex/MSCI_Feb20_STPublicList.pdf">TEST</a><a href="/eqb/gimi/stdindex/MSCI_Feb19_STPublicList.pdf">old</a><a href="https://evil.invalid/MSCI_May26_STPublicList.pdf">bad</a>'
    links=review_links(html)
    assert set(links)=={'2026-08','2020-02'} and all(u.startswith('https://app2.msci.com/') for u in links.values())
    with pytest.raises(ValueError):review_links('<a href="https://evil.invalid">x</a>')


def test_msci_changes_are_not_fake_constituent_holdings(repo):
    r=parse_review_text(text_fixture(),'2026-08')
    assert save_review(repo,'2026-08',r,b'TEST source PDF','https://app2.msci.com/TEST.pdf')
    assert not save_review(repo,'2026-08',r,b'TEST source PDF','https://app2.msci.com/TEST.pdf')
    assert reviews(repo)[0]['additions']==['TEST ADD']
    assert not repo.rows('SELECT * FROM portfolio_snapshots')
    assert not repo.rows('SELECT * FROM securities')
    doc=repo.rows('SELECT * FROM source_documents')[0]
    assert gzip.decompress((repo.root/doc['raw_path']).read_bytes())==b'TEST source PDF'


def test_msci_correction_keeps_old_revision(repo):
    r=parse_review_text(text_fixture(),'2026-08')
    save_review(repo,'2026-08',r,b'TEST v1','https://app2.msci.com/TEST.pdf')
    r['additions']=['TEST CORRECTED']
    save_review(repo,'2026-08',r,b'TEST v2','https://app2.msci.com/TEST.pdf')
    assert len(repo.rows('SELECT * FROM msci_reviews'))==2
    assert reviews(repo,2026)[0]['additions']==['TEST CORRECTED']
    assert reviews(repo,2020)==[]


def test_v4_queue_rebuild_preserves_references_and_data(tmp_path):
    folder=tmp_path/'v4';folder.mkdir();path=folder/'stock16.sqlite3'
    with sqlite3.connect(path) as c:
        c.executescript((Path(__file__).parents[1]/'stock16/schema.sql').read_text())
        for version,steps in [(2,V2),(3,V3),(4,V4)]:
            for sql in steps:c.execute(sql)
            c.execute('INSERT INTO schema_versions VALUES(?)',(version,))
        c.execute("INSERT INTO collection_jobs(id,collector,requested_at,status,requested_by) VALUES(77,'nps','2026-10-30','queued','web')")
        c.execute("INSERT INTO data_fetch_runs(collector,started_at,status,job_id,origin) VALUES('nps','2026-10-30','success',77,'web')")
    repo=Repository(folder);repo.initialize()
    assert repo.rows('SELECT job_id FROM data_fetch_runs')[0]['job_id']==77
    assert repo.rows('SELECT id FROM collection_jobs')[0]['id']==77
    assert list((folder/'backups/migrations').glob('before-v4-*.gz'))
    with repo.connect() as c:
        assert c.execute('PRAGMA foreign_keys').fetchone()[0]==1
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    repo.initialize()
    assert len(list((folder/'backups/migrations').glob('before-v4-*.gz')))==1


def test_private_krx_header_cannot_follow_redirect(monkeypatch):
    t=Transport(credentials={'data-dbg.krx.co.kr':{'AUTH_KEY':'TEST DO NOT LEAK'}})
    monkeypatch.setattr(t,'validate_url',lambda u:None)
    calls=[]
    class Response:
        def __init__(self,status,headers):self.status_code,self.headers=status,headers
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def raise_for_status(self):pass
        def iter_content(self,*a):return iter([b'{}'])
    def get(url,**kwargs):
        calls.append((url,kwargs['headers']))
        return Response(302,{'Location':'https://app2.msci.com/TEST'}) if len(calls)==1 else Response(200,{'Content-Type':'application/json'})
    monkeypatch.setattr(t.http,'get',get)
    t.fetch('https://data-dbg.krx.co.kr/TEST',{'application/json'},robots=False)
    assert calls[0][1]['AUTH_KEY']=='TEST DO NOT LEAK'
    assert 'AUTH_KEY' not in calls[1][1]
    assert not any('TEST DO NOT LEAK' in url for url,_ in calls)
