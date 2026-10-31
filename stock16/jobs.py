"""Bounded SQLite queue. HTTP enqueues only; a separate worker owns execution."""
import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from .collectors import COLLECTORS,run_collectors,next_scheduled
from .model import now

ALLOWED=(*COLLECTORS,'all')
ACTIVE=('queued','running')


@contextmanager
def collector_lock(repo,*,blocking=True):
    with (repo.root/'collector.lock').open('a') as lock:
        acquired=False
        try:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                acquired=True
            except BlockingIOError: pass
            yield acquired
        finally:
            if acquired: fcntl.flock(lock,fcntl.LOCK_UN)


def enqueue(repo,collector):
    if not isinstance(collector,str) or collector not in ALLOWED: raise ValueError('Invalid collector')
    with repo.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        conflict=c.execute("SELECT * FROM collection_jobs WHERE status IN ('queued','running') AND (collector=? OR collector='all' OR ?='all') ORDER BY id LIMIT 1",(collector,collector)).fetchone()
        if conflict: return decode(dict(conflict)),False
        identifier=c.execute("INSERT INTO collection_jobs(collector,requested_at,status,message,requested_by) VALUES(?,?,'queued',?,'web')",(collector,now(),'요청됨 · 자동 수집 실행 중이면 종료 후 실행됩니다.')).lastrowid
        row=dict(c.execute('SELECT * FROM collection_jobs WHERE id=?',(identifier,)).fetchone())
    return decode(row),True


def decode(row):
    row['results']=json.loads(row.pop('results_json'))
    return row


def get_job(repo,identifier):
    rows=repo.rows('SELECT * FROM collection_jobs WHERE id=?',(identifier,))
    return decode(rows[0]) if rows else None


def listing(repo):
    jobs=[decode(r) for r in repo.rows('SELECT * FROM collection_jobs ORDER BY id DESC LIMIT 100')]
    collectors=[]
    for name in COLLECTORS:
        rows=repo.rows('SELECT * FROM data_fetch_runs WHERE collector=? ORDER BY id DESC LIMIT 1',(name,))
        success=repo.rows("SELECT max(finished_at) value FROM data_fetch_runs WHERE collector=? AND status IN ('success','no_new_data')",(name,))[0]['value']
        pending=next((j for j in jobs if j['status'] in ACTIVE and j['collector'] in (name,'all')),None)
        collectors.append(dict(collector=name,last_run=rows[0] if rows else None,last_success=success,active_job=pending,next_scheduled=next_scheduled(name)))
    with collector_lock(repo,blocking=False) as free:
        busy=not free
    return dict(jobs=jobs,collectors=collectors,collector_busy=busy)


def aggregate(results):
    statuses={r['status'] for r in results}
    if statuses=={'needs_configuration'}: return 'needs_configuration'
    if statuses=={'error'}: return 'error'
    if statuses & {'partial','error','needs_configuration','needs_review'}: return 'partial'
    return 'success' if any(r['new_items'] for r in results) else 'no_new_data'


def work_once(repo,*,runner=None):
    """Claim only while holding the same flock as scheduled/CLI collection."""
    if not repo.rows("SELECT id FROM collection_jobs WHERE status IN ('queued','running') LIMIT 1"):
        return False
    with collector_lock(repo,blocking=False) as available:
        if not available: return False
        # No live collector can own a running job now. Recover an interrupted job
        # without retrying its potentially already committed snapshots.
        with repo.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            orphaned=[r[0] for r in c.execute("SELECT id FROM collection_jobs WHERE status='running'")]
            for identifier in orphaned:
                c.execute("UPDATE data_fetch_runs SET status='error',finished_at=?,message=? WHERE job_id=? AND status='running'",(now(),'worker 중단 · 저장된 자료 보존',identifier))
            c.execute("UPDATE collection_jobs SET status='error',finished_at=?,message=? WHERE status='running'",(now(),'worker 중단 · 저장된 자료 보존 · 필요 시 다시 요청하세요.'))
            row=c.execute("SELECT * FROM collection_jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if row is None: return False
            job=dict(row)
            c.execute("UPDATE collection_jobs SET status='running',started_at=?,attempt_count=attempt_count+1,message=? WHERE id=?",(now(),'수집 중',job['id']))
        results=[]
        names=COLLECTORS if job['collector']=='all' else (job['collector'],)
        runner=runner or run_collectors
        for name in names:
            try:
                outcome=runner(repo,[name],origin='web',job_id=job['id'],requested_at=job['requested_at'])
                result=outcome[0]
                # Registry supplies sanitized messages and fixed structural fields.
            except Exception as error:
                result=dict(collector=name,status='error',new_items=0,message='수집 실패 · '+type(error).__name__+' · 설정 검토 필요')
            results.append(result)
            repo.execute('UPDATE collection_jobs SET results_json=?,new_items=? WHERE id=?',(json.dumps(results,ensure_ascii=False),sum(r['new_items'] for r in results),job['id']))
        status=aggregate(results)
        message=(results[0]['message'] if len(results)==1 else f"수집기 {len(results)}개 확인 · 신규 {sum(r['new_items'] for r in results)}건 · 상세 결과 확인")
        repo.execute('UPDATE collection_jobs SET status=?,finished_at=?,message=? WHERE id=?',(status,now(),message,job['id']))
    return True


def worker(repo,*,once=False,poll_interval=3):
    if not 2<=poll_interval<=5: raise ValueError('Poll interval must be 2..5 seconds')
    # A process lifetime lock prevents one idle worker from recovering another's job.
    with (repo.root/'collector-worker.lock').open('a') as lease:
        try: fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('Collector worker already running')
        while True:
            worked=work_once(repo)
            if once: return worked
            if not worked: time.sleep(poll_interval)


def cleanup(repo,days=None):
    days=int(days if days is not None else os.environ.get('STOCK16_JOB_RETENTION_DAYS','90'))
    if not 1<=days<=3650: raise ValueError('Invalid job retention days')
    cutoff=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
    with repo.connect() as c:
        return c.execute("DELETE FROM collection_jobs WHERE finished_at<? AND status NOT IN ('queued','running')",(cutoff,)).rowcount
