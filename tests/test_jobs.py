import configparser
import json
import re
import secrets
from datetime import datetime,timedelta,timezone
from pathlib import Path
import pytest
from werkzeug.security import generate_password_hash
from stock16.db import Repository
from stock16.web import create_app
from stock16.jobs import enqueue,get_job,listing,work_once,collector_lock,cleanup,COLLECTORS
from stock16.collectors import run_collectors,run_backfill

PREFIX='/stock16-7/api/'

@pytest.fixture
def app(tmp_path):
    return create_app(dict(TESTING=True,SECRET_KEY=secrets.token_hex(32),PASSWORD_HASH=generate_password_hash('fixture-password'),DATA_DIR=tmp_path/'data',SESSION_COOKIE_SECURE=True))

@pytest.fixture
def client(app):
    with app.test_client() as c:
        page=c.get('/stock16-7/login',base_url='https://localhost')
        csrf=re.search('name="csrf" value="([^"]+)"',page.text)[1]
        c.post('/stock16-7/login',data={'csrf':csrf,'password':'fixture-password'},base_url='https://localhost')
        with c.session_transaction(path='/stock16-7/',base_url='https://localhost') as s: token=s['csrf']
        c.environ_base['HTTP_X_CSRF_TOKEN']=token
        yield c


def test_unauthenticated_collect(app):
    assert app.test_client().post(PREFIX+'collect',json={'collector':'krx-etf'}).status_code==401


def test_missing_csrf(app,client):
    client.environ_base.pop('HTTP_X_CSRF_TOKEN')
    assert client.post(PREFIX+'collect',json={'collector':'nps'}).status_code==403


@pytest.mark.parametrize('body',[{},[],{'collector':'shell'},{'collector':'nps; touch /tmp/pwn'},{'collector':[]},{'collector':None}, {'collector':'nps','command':'ls'},{'collector':'nps','arguments':[]},{'collector':'nps','url':'https://evil.invalid'},{'collector':'nps','path':'/tmp/x'},{'collector':'nps','environment':{}},{'collector':'nps','shell':'ls'}])
def test_invalid_input(client,body):
    assert client.post(PREFIX+'collect',json=body).status_code==400


def test_enqueue_does_not_run_collectors(client,app,monkeypatch):
    def forbidden(*a,**k): raise AssertionError('HTTP must not execute collector')
    monkeypatch.setattr('stock16.jobs.run_collectors',forbidden)
    response=client.post(PREFIX+'collect',json={'collector':'krx-etf'})
    assert response.status_code==202
    job=response.json['job'];assert job['status']=='queued' and job['attempt_count']==0
    assert job['requested_by']=='web'
    assert client.get(PREFIX+'collection-jobs/'+str(job['id'])).json['id']==job['id']
    assert client.get(PREFIX+'collection-jobs').json['collectors'][2]['active_job']['id']==job['id']
    assert not app.extensions['repo'].rows('SELECT * FROM data_fetch_runs')


@pytest.mark.parametrize('status',['queued','running'])
def test_duplicate_conflict(client,app,status):
    job=client.post(PREFIX+'collect',json={'collector':'krx-etf'}).json['job']
    app.extensions['repo'].execute('UPDATE collection_jobs SET status=? WHERE id=?',(status,job['id']))
    response=client.post(PREFIX+'collect',json={'collector':'krx-etf'})
    assert response.status_code==409 and response.json['job']['id']==job['id']
    assert len(app.extensions['repo'].rows('SELECT * FROM collection_jobs'))==1


def test_different_collectors_and_all_conflict(repo):
    assert enqueue(repo,'nps')[1]
    assert enqueue(repo,'nbim')[1]
    assert not enqueue(repo,'all')[1]


def test_all_blocks_individual(repo):
    assert enqueue(repo,'all')[1]
    assert not enqueue(repo,'nps')[1]
    assert not enqueue(repo,'all')[1]


def test_worker_success_and_polling(client,app):
    job=client.post(PREFIX+'collect',json={'collector':'krx-etf'}).json['job']
    def runner(repo,names,**kwargs):
        assert names==['krx-etf'] and kwargs['origin']=='web' and kwargs['job_id']==job['id']
        running=client.get(PREFIX+'collection-jobs/'+str(job['id'])).json
        assert running['status']=='running' and running['attempt_count']==1
        return [dict(collector=names[0],status='success',message='새 데이터 2건',new_items=2,checked_funds=1,new_snapshots=2,revisions=0,skipped=0,errors=0,latest_as_of_date='2026-10-30')]
    assert work_once(app.extensions['repo'],runner=runner)
    done=client.get(PREFIX+'collection-jobs/'+str(job['id'])).json
    assert done['status']=='success' and done['new_items']==2
    assert done['results'][0]['checked_funds']==1
    assert datetime.fromisoformat(done['requested_at']).utcoffset()==timedelta(0)
    assert done['started_at'] and done['finished_at']
    assert client.get(PREFIX+'collection-jobs/99999').status_code==404


def test_registry_needs_configuration_and_web_origin(repo,monkeypatch):
    monkeypatch.delenv('STOCK16_SEC_USER_AGENT',raising=False)
    job,_=enqueue(repo,'sec-13f');work_once(repo)
    assert get_job(repo,job['id'])['status']=='needs_configuration'
    run=repo.rows('SELECT * FROM data_fetch_runs')[0]
    assert run['origin']=='web' and run['job_id']==job['id'] and run['requested_at']==job['requested_at']
    assert 'STOCK16_SEC_USER_AGENT' in run['message']


def test_worker_error_isolated_sanitized_and_next_job_runs(repo):
    first,_=enqueue(repo,'nps');second,_=enqueue(repo,'nbim')
    def runner(repo,names,**kw):
        if names==['nps']: raise RuntimeError('API_KEY=DO_NOT_EXPOSE')
        return [dict(collector='nbim',status='no_new_data',message='신규 데이터 없음',new_items=0)]
    assert work_once(repo,runner=runner)
    assert 'DO_NOT_EXPOSE' not in json.dumps(get_job(repo,first['id']))
    assert work_once(repo,runner=runner)
    assert get_job(repo,first['id'])['status']=='error'
    assert get_job(repo,second['id'])['status']=='no_new_data'


def test_all_failure_continues(repo):
    job,_=enqueue(repo,'all');calls=[]
    def runner(repo,names,**kw):
        calls.extend(names)
        if names==['nps']: raise ValueError('SECRET')
        return [dict(collector=names[0],status='success',new_items=1,message='새 데이터 1건')]
    work_once(repo,runner=runner)
    final=get_job(repo,job['id'])
    assert calls==list(COLLECTORS) and final['status']=='partial'
    assert final['new_items']==len(COLLECTORS)-1 and len(final['results'])==len(COLLECTORS)


def test_scheduler_flock_leaves_job_queued(repo):
    import fcntl
    job,_=enqueue(repo,'krx-etf')
    with (repo.root/'collector.lock').open('a') as timer_lock:
        fcntl.flock(timer_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert not work_once(repo,runner=lambda *a,**k:pytest.fail('lock violated'))
        assert listing(repo)['collector_busy']
        assert get_job(repo,job['id'])['status']=='queued'
    with collector_lock(repo,blocking=False) as acquired: assert acquired


def test_worker_recovery_retains_data_and_does_not_retry(repo,ingest,stock):
    ingest('2026-01-01',[stock]);job,_=enqueue(repo,'nps')
    repo.execute("UPDATE collection_jobs SET status='running',attempt_count=1 WHERE id=?",(job['id'],))
    assert not work_once(repo)
    assert get_job(repo,job['id'])['status']=='error'
    assert len(repo.rows('SELECT * FROM portfolio_snapshots'))==1


def test_cleanup_only_old_finished_metadata(repo,ingest,stock):
    ingest('2026-01-01',[stock])
    old=(datetime.now(timezone.utc)-timedelta(days=181)).isoformat()
    job,_=enqueue(repo,'nps')
    repo.execute("UPDATE collection_jobs SET status='success',finished_at=? WHERE id=?",(old,job['id']))
    run_collectors(repo,['sec-13f'],origin='web',job_id=job['id'])
    queued,_=enqueue(repo,'nps')
    repo.execute('UPDATE collection_jobs SET requested_at=? WHERE id=?',(old,queued['id']))
    recent,_=enqueue(repo,'nbim')
    repo.execute("UPDATE collection_jobs SET status='success',finished_at=? WHERE id=?",(datetime.now(timezone.utc).isoformat(),recent['id']))
    assert cleanup(repo,90)==1
    assert get_job(repo,queued['id']) and get_job(repo,recent['id'])
    assert repo.rows('SELECT * FROM data_fetch_runs')[0]['job_id'] is None
    assert len(repo.rows('SELECT * FROM portfolio_snapshots'))==1
    assert len(list((repo.root/'raw').rglob('*.gz')))==1


@pytest.mark.parametrize('origin',['cli','scheduler'])
def test_registry_origins(repo,origin,monkeypatch):
    monkeypatch.delenv('STOCK16_SEC_USER_AGENT',raising=False)
    run_collectors(repo,['sec-13f'],origin=origin)
    assert repo.rows('SELECT * FROM data_fetch_runs')[0]['origin']==origin


def test_allowlist_guard_before_any_run(repo):
    with pytest.raises(ValueError): run_collectors(repo,['nps','bad'])
    with pytest.raises(ValueError): enqueue(repo,'$(id)')
    assert not repo.rows('SELECT * FROM data_fetch_runs')


def test_worker_unit_and_install_scripts():
    root=Path(__file__).resolve().parents[1]
    unit=configparser.ConfigParser(interpolation=None);unit.read(root/'deploy/stock16-collector-worker.service')
    service=unit['Service']
    for key,val in [('User','stock16'),('Group','stock16'),('Restart','on-failure'),('NoNewPrivileges','true'),('PrivateTmp','true'),('ProtectSystem','strict'),('ProtectHome','true'),('ReadWritePaths','/var/www/stock16-7/data')]: assert service[key]==val
    assert service['ExecStart'].endswith('python -m stock16 collector-worker')
    assert unit['Install']['WantedBy']=='multi-user.target'
    install=(root/'deploy/install.sh').read_text();update=(root/'deploy/update.sh').read_text()
    assert 'sudo -u stock16 .venv/bin/python -m stock16 init' in install
    assert 'deploy/*.service deploy/*.timer' in update and 'enable stock16-collector-worker.service' in update
    assert 'git reset' not in update and 'git clean' not in update
    for name in ('stock16-daily.service','stock16-collect@.service'):
        text=(root/'deploy'/name).read_text()
        assert '/data/collector.lock' in text and '--lock-held --origin scheduler' in text


def test_ui_kst_polling_and_safe_web_code():
    root=Path(__file__).resolve().parents[1]
    js=(root/'stock16/static/app.js').read_text()
    assert "timeZone:'Asia/Seoul'" in js and '2500' in js
    assert "fetch(base+'/api/collect'" in js and 'location.reload' not in js
    web=(root/'stock16/web.py').read_text()
    assert 'run_collectors(' not in web and 'subprocess' not in web and 'os.system' not in web


def test_concurrent_requests_create_only_one_active_job(repo):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:enqueue(repo,'krx-etf'),range(4)))
    assert sum(created for job,created in results)==1
    assert len({job['id'] for job,created in results})==1


def test_backfill_origin_recorded_without_configuration(repo,monkeypatch):
    monkeypatch.delenv('STOCK16_SEC_USER_AGENT',raising=False)
    result=run_backfill(repo,'sec-13f',from_year=2020)
    assert result['status']=='needs_configuration'
    assert repo.rows('SELECT origin FROM data_fetch_runs')[0]['origin']=='backfill'
