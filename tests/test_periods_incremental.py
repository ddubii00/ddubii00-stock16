import sqlite3
import gzip
from datetime import date
from pathlib import Path
import pytest
from stock16.analytics import Analytics
from stock16.model import rebuild_changes
from stock16.db import Repository
from stock16.migrations import migrate


def test_consensus_uses_month_not_previous(repo,ingest,stock):
    for fund in ('nps','etf-069500'):
        for day,shares in [('2026-07-31','80'),('2026-09-30','100'),('2026-10-29','150'),('2026-10-31','120')]:
            ingest(day,[{**stock,'shares':shares}],fund=fund)
    analytics=Analytics(repo)
    assert analytics.consensus('previous',minimum=2)==[]
    result=analytics.consensus('1m',minimum=2)[0]
    assert result['institution_count']==1 and result['etf_count']==1
    for obs in result['observations']:
        assert obs['before_as_of_date']=='2026-09-30'
        assert obs['after_as_of_date']=='2026-10-31'
        assert obs['shares_delta']=='20' and obs['shares_delta_pct']=='20'
        assert all(k in obs for k in ('fund_id','institution_id','published_at','source_url','status','portfolio_weight_delta_pp','quantity_type'))
    assert analytics.consensus('3m',minimum=2)[0]['observations'][0]['shares_delta']=='40'


def test_consensus_no_history_does_not_fallback_to_yesterday(repo,ingest,stock):
    for fund in ('nps','nbim'):
        ingest('2026-10-29',[stock],fund=fund)
        ingest('2026-10-31',[{**stock,'shares':'110'}],fund=fund)
    assert Analytics(repo).consensus('1m')==[]
    assert Analytics(repo).consensus('1m',mode='held')[0]['observations'][0]['before_as_of_date'] is None


@pytest.mark.parametrize('period,expected', [('1m','2026-09-30'),('3m','2026-07-31'),('quarter','2026-07-31'),('half','2026-04-30'),('1y','2025-10-31')])
def test_calendar_month_ends(repo,ingest,stock,period,expected):
    ingest(expected,[stock])
    ingest('2026-10-31',[{**stock,'shares':'120'}])
    result=Analytics(repo).changes('etf-069500',period)
    assert result['previous']['as_of_date']==expected


def test_incremental_equals_rebuild_all_scenarios(repo,ingest,stock,monkeypatch):
    import stock16.model as model
    actual_compare=model.compare
    calls=[]
    def counted(c,b,a):
        calls.append(a['as_of_date'])
        return actual_compare(c,b,a)
    monkeypatch.setattr(model,'compare',counted)
    events=[('2026-01-01','100','2026-01-02','full'),
            ('2026-01-03','120','2026-01-04','full'),
            ('2026-01-02','110','2026-01-03','full'),
            ('2026-01-02','115','2026-01-05','partial'),
            ('2026-01-02','112','2026-01-04','full')]
    for day,shares,published,scope in events:
        calls.clear()
        ingest(day,[{**stock,'shares':shares}],published=published,scope=scope)
        assert len(calls)<=2
        incremental=repo.rows('SELECT * FROM holding_changes ORDER BY snapshot_id,security_id')
        with repo.connect() as c: rebuild_changes(c,'etf-069500')
        assert incremental==repo.rows('SELECT * FROM holding_changes ORDER BY snapshot_id,security_id')
    calls.clear()
    ingest('2026-01-03',[{**stock,'shares':'120'}],published='2026-01-04')
    assert calls==[]


def test_incremental_new_exit_split_partial_equivalence(repo,ingest,stock):
    other=dict(name='TEST B',kr_code='000660',shares='50',weight='5')
    ingest('2026-01-01',[stock])
    sid=repo.rows('SELECT id FROM securities')[0]['id']
    repo.execute('INSERT INTO corporate_actions VALUES(?,?,?,?)',(sid,'2026-01-02','2','https://www.samsungfund.com/split'))
    for day,rows,scope in [('2026-01-03',[{**stock,'shares':'220'},other],'full'),
                           ('2026-01-02',[{**stock,'shares':'200'}],'full'),
                           ('2026-01-04',[other],'partial'),('2026-01-05',[stock],'full')]:
        ingest(day,rows,scope=scope)
        before=repo.rows('SELECT * FROM holding_changes ORDER BY snapshot_id,security_id')
        with repo.connect() as c: rebuild_changes(c,'etf-069500')
        assert before==repo.rows('SELECT * FROM holding_changes ORDER BY snapshot_id,security_id')


def old_database(folder):
    folder.mkdir()
    path=folder/'stock16.sqlite3'
    connection=sqlite3.connect(path)
    connection.executescript((Path(__file__).parents[1]/'stock16/schema.sql').read_text())
    connection.execute("INSERT INTO institutions VALUES('original','Original','Pension','KR','partial','annual','https://fund.nps.or.kr/')")
    connection.commit()
    return path,connection


def test_migration_preserves_and_backs_up_v1(tmp_path):
    folder=tmp_path/'old'
    path,c=old_database(folder);c.close()
    repo=Repository(folder);repo.initialize()
    assert repo.rows('SELECT name FROM institutions WHERE id=?',('original',))[0]['name']=='Original'
    assert repo.rows('SELECT max(version) v FROM schema_versions')[0]['v']==2
    backups=list((folder/'backups/migrations').glob('*.gz'))
    assert len(backups)==1
    restored=tmp_path/'before.db';restored.write_bytes(gzip.decompress(backups[0].read_bytes()))
    with sqlite3.connect(restored) as old:
        assert old.execute('SELECT max(version) FROM schema_versions').fetchone()[0]==1
    repo.initialize()
    assert list((folder/'backups/migrations').glob('*.gz'))==backups


def test_failed_migration_rolls_back_entire_upgrade(tmp_path):
    folder=tmp_path/'failure';path,c=old_database(folder)
    with pytest.raises(sqlite3.Error):
        migrate(c,folder,existing=True,steps=("ALTER TABLE institutions ADD COLUMN temporary TEXT",'INVALID SQL'))
    assert 'temporary' not in [r[1] for r in c.execute('PRAGMA table_info(institutions)')]
    assert c.execute('SELECT max(version) FROM schema_versions').fetchone()[0]==1
    assert c.execute('SELECT name FROM institutions').fetchone()[0]=='Original'
    c.close()
    assert len(list((folder/'backups/migrations').glob('*.gz')))==1
