from datetime import date,timedelta
import gzip
import sqlite3
from stock16.storage import raw_retention,backup,storage_status,store_document


def test_retention_month_end_dedup_and_history(repo,ingest,stock):
    ingest('2024-01-02',[stock],raw=b'old-midmonth')
    ingest('2024-01-31',[stock],raw=b'old-monthend')
    ingest('2026-10-01',[stock],raw=b'recent')
    before=repo.rows('SELECT count(*) n FROM portfolio_snapshots')[0]['n']
    result=raw_retention(repo,date(2026,10,31),apply=True)
    assert result['files']==1
    assert len(list((repo.root/'raw').rglob('*.gz')))==2
    assert repo.rows('SELECT count(*) n FROM portfolio_snapshots')[0]['n']==before
    assert len(repo.rows('SELECT * FROM source_documents WHERE purged_at IS NOT NULL'))==1


def test_shared_blob_not_deleted(repo,ingest,stock):
    ingest('2024-01-02',[stock],raw=b'shared')
    ingest('2024-01-31',[stock],raw=b'other')
    ingest('2024-02-29',[stock],raw=b'shared')
    assert raw_retention(repo,date(2026,10,31),apply=True)['files']==0


def test_retention_calendar_leap_boundary(repo):
    from stock16.storage import months_before
    assert months_before(date(2024,2,29),12)==date(2023,2,28)


def test_backup_gfs_and_integrity(repo,ingest,stock,tmp_path,monkeypatch):
    # Preserve and verify the original explicitly configured 7/4/6 policy.
    for kind,count in [('DAILY',7),('WEEKLY',4),('MONTHLY',6)]:
        monkeypatch.setenv('STOCK16_BACKUP_'+kind,str(count))
    ingest('2026-01-01',[stock])
    for d in range(50): backup(repo,date(2026,1,1)+timedelta(days=d))
    root=repo.root/'backups'
    assert len(list(root.glob('daily-*')))==7
    assert len(list(root.glob('weekly-*')))==4
    assert len(list(root.glob('monthly-*')))==2
    latest=sorted(root.glob('daily-*'))[-1]
    target=tmp_path/'restored.db';target.write_bytes(gzip.decompress(latest.read_bytes()))
    with sqlite3.connect(target) as c:
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert c.execute('SELECT count(*) FROM portfolio_snapshots').fetchone()[0]==1


def test_storage_usage(repo):
    s=storage_status(repo)
    assert s['db_bytes']>0 and s['total_bytes']>=s['db_bytes']
    assert s['thresholds']==[70,80,90]


def test_threshold_notify_once(repo,monkeypatch):
    from stock16 import storage
    monkeypatch.setenv('STOCK16_ALERT_WEBHOOK','https://example.com/webhook')
    monkeypatch.setattr(storage,'storage_status',lambda r:dict(warning_level=80,disk_used_pct=82))
    calls=[]
    class Response:
        def raise_for_status(self): pass
    def send(*a,**k): calls.append(k['json']);return Response()
    storage.check_storage_alert(repo,send);storage.check_storage_alert(repo,send)
    assert len(calls)==1


def test_html_not_archived(repo):
    import pytest
    with pytest.raises(ValueError): store_document(repo,b'html','text/html',{})
