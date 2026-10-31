import calendar
import fcntl
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from .model import now


@contextmanager
def storage_lock(repo):
    with (repo.root / '.storage.lock').open('a') as f:
        fcntl.flock(f,fcntl.LOCK_EX)
        yield


def store_document(repo,body,media_type,meta):
    """Hash uncompressed bytes; observations share one immutable gzip blob."""
    if media_type=='text/html':
        raise ValueError('HTML pages must be processed transiently')
    sha=hashlib.sha256(body).hexdigest()
    relative=Path('raw') / sha[:2] / (sha+'.gz')
    path=repo.root / relative
    with storage_lock(repo):
        path.parent.mkdir(parents=True,exist_ok=True)
        if not path.exists():
            fd,tmp=tempfile.mkstemp(dir=path.parent)
            try:
                with os.fdopen(fd,'wb') as f:
                    f.write(gzip.compress(body,compresslevel=6,mtime=0))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp,path)
            finally:
                if os.path.exists(tmp): os.unlink(tmp)
        with repo.connect() as c:
            c.execute('INSERT INTO source_documents VALUES(?,?,?,?,?,?,NULL) ON CONFLICT(checksum) DO UPDATE SET raw_path=excluded.raw_path,compressed_bytes=excluded.compressed_bytes,purged_at=NULL',
             (sha,media_type,str(relative),len(body),path.stat().st_size,now()))
            c.execute('INSERT OR IGNORE INTO source_observations(checksum,fund_id,as_of_date,published_at,fetched_at,source_url,source_type) VALUES(?,?,?,?,?,?,?)',
              (sha,meta['fund_id'],meta['as_of_date'],meta['published_at'],meta.get('fetched_at',now()),meta['source_url'],meta['source_type']))
    return sha


def months_before(day,months):
    m=day.year*12+day.month-1-months
    y,month=divmod(m,12)
    month+=1
    return date(y,month,min(day.day,calendar.monthrange(y,month)[1]))


def raw_retention(repo,today=None,apply=False):
    today=today or date.today()
    cutoff=months_before(today,int(os.environ.get('STOCK16_RAW_MONTHS','12'))).isoformat()
    observations=repo.rows('SELECT o.*,f.kind FROM source_observations o JOIN funds f ON f.id=o.fund_id')
    latest={}
    for o in observations:
        if o['source_type']=='krx-etf':
            key=(o['fund_id'],o['as_of_date'][:7])
            latest[key]=max(latest.get(key,''),o['as_of_date'])
    keep=set()
    for o in observations:
        # Other institutions' original files are retained. ETF monthly representative
        # is the last AVAILABLE official business-date file, not calendar-day guessing.
        if o['source_type']!='krx-etf' or o['as_of_date']>=cutoff or o['as_of_date']==latest[(o['fund_id'],o['as_of_date'][:7])]:
            keep.add(o['checksum'])
    candidates=[d for d in repo.rows('SELECT * FROM source_documents WHERE raw_path IS NOT NULL') if d['checksum'] not in keep]
    if apply:
        with storage_lock(repo):
            # Recompute under lock so a new shared observation cannot be deleted.
            fresh=raw_retention(repo,today,apply=False)
            candidates=fresh['candidates']
            for d in candidates:
                path=repo.root / d['raw_path']
                path.unlink(missing_ok=True)
                repo.execute('UPDATE source_documents SET raw_path=NULL,purged_at=? WHERE checksum=?',(now(),d['checksum']))
    return {'cutoff':cutoff,'files':len(candidates),'bytes':sum(d['compressed_bytes'] for d in candidates),'candidates':candidates,'applied':apply}


def tree_bytes(path):
    # Count allocated file size once for hard links, and exclude symlinks.
    seen=set()
    total=0
    for p in Path(path).rglob('*'):
        try:
            if not p.is_file() or p.is_symlink():
                continue
            st=p.stat()
        except FileNotFoundError:
            continue
        if st:
            key=(st.st_dev,st.st_ino)
            if key not in seen:
                total+=st.st_size
                seen.add(key)
    return total


def storage_status(repo):
    usage=shutil.disk_usage(repo.root)
    percent=round(usage.used/usage.total*100,2)
    def size(path):
        try: return path.stat().st_size
        except FileNotFoundError: return 0
    db=size(repo.path)
    wal=size(Path(str(repo.path)+'-wal'))
    shm=size(Path(str(repo.path)+'-shm'))
    crossed=[t for t in (70,80,90) if percent>=t]
    result=dict(db_bytes=db,wal_bytes=wal,shm_bytes=shm,db_with_wal_bytes=db+wal+shm,
                raw_bytes=tree_bytes(repo.root/'raw'),backup_bytes=tree_bytes(repo.root/'backups'),
                logs_bytes=tree_bytes(repo.root/'logs'),cache_bytes=tree_bytes(repo.root/'cache'),
                total_bytes=tree_bytes(repo.root),disk_total_bytes=usage.total,
                disk_used_bytes=usage.used,disk_free_bytes=usage.free,
                disk_used_pct=percent,warning_level=max(crossed,default=0),crossed_thresholds=crossed,
                thresholds=[70,80,90],measured_at=now())
    def human(value):
        for unit in ('B','KiB','MiB','GiB','TiB'):
            if value<1024 or unit=='TiB': return f'{value:.2f} {unit}'
            value/=1024
    result['human_readable']={key:human(value) for key,value in result.items() if key.endswith('_bytes')}
    counts={}
    for name in ('portfolio_snapshots','holding_values','holding_sets','source_documents'):
        counts[name]=repo.rows('SELECT count(*) count FROM '+name)[0]['count']
    counts['etf_snapshots']=repo.rows("SELECT count(*) count FROM portfolio_snapshots p JOIN funds f ON f.id=p.fund_id WHERE f.kind='ETF'")[0]['count']
    result['counts']=counts
    return result


def backup(repo,today=None):
    today=today or date.today()
    root=repo.root/'backups'
    root.mkdir(exist_ok=True)
    with storage_lock(repo):
        with tempfile.TemporaryDirectory(dir=root) as tmp:
            plain=Path(tmp)/'backup.sqlite3'
            with repo.connect() as src,sqlite3.connect(plain) as dest:
                src.backup(dest)
                if dest.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                    raise RuntimeError('Backup integrity check failed')
            packed=Path(tmp)/'backup.gz'
            with plain.open('rb') as f,gzip.open(packed,'wb',compresslevel=6) as g:
                shutil.copyfileobj(f,g)
            targets=[('daily',today.isoformat()),('weekly',today.strftime('%G-W%V')),('monthly',today.strftime('%Y-%m'))]
            final=root/('daily-'+today.isoformat()+'.sqlite3.gz')
            os.replace(packed,final)
            for period,key in targets[1:]:
                target=root/(period+'-'+key+'.sqlite3.gz')
                temp=root/(target.name+'.tmp')
                temp.unlink(missing_ok=True)
                os.link(final,temp)
                os.replace(temp,target)
            for period,default in (('daily',3),('weekly',2),('monthly',3)):
                count=max(1,int(os.environ.get('STOCK16_BACKUP_'+period.upper(),str(default))))
                for old in sorted(root.glob(period+'-*.sqlite3.gz'),reverse=True)[count:]:
                    old.unlink()
    return str(final)


def maintain_database(repo,vacuum=False):
    """Bounded checkpoint/optimizer; VACUUM is opt-in with a free-space guard."""
    if vacuum:
        required=repo.path.stat().st_size*2+64*1024*1024
        if shutil.disk_usage(repo.root).free<required:
            raise RuntimeError('Not enough free space for manual VACUUM and its backup')
        backup(repo)
    with repo.connect() as connection:
        connection.execute('PRAGMA busy_timeout=1000')
        connection.execute('PRAGMA analysis_limit=400')
        connection.execute('PRAGMA optimize')
        checkpoint=tuple(connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())
        if vacuum:
            connection.execute('VACUUM')
    return dict(checkpoint_busy=checkpoint[0],wal_pages=checkpoint[1],checkpointed_pages=checkpoint[2],vacuum=vacuum)


def check_storage_alert(repo,send=None):
    status=storage_status(repo)
    row=repo.rows("SELECT value FROM collector_state WHERE key='disk_warning'")
    previous=int(row[0]['value']) if row else 0
    level=status['warning_level']
    webhook=os.environ.get('STOCK16_ALERT_WEBHOOK')
    if level>previous:
        import logging
        logging.getLogger('stock16').warning('disk_threshold=%s used_pct=%s',level,status['disk_used_pct'])
        if webhook:
            if not webhook.startswith('https://'):
                raise ValueError('HTTPS webhook required')
            if send is None:
                import requests
                send=requests.post
            # Explicit operator-configured URL only; never taken from browser input.
            response=send(webhook,json=status,timeout=(5,10),allow_redirects=False)
            response.raise_for_status()
    repo.execute("INSERT INTO collector_state VALUES('disk_warning',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(level),))
    return status
