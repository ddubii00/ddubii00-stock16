import argparse
import getpass
import gzip
import json
import logging
from logging.handlers import WatchedFileHandler
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
from .settings import ROOT,load_env
from .db import Repository


def setup():
    from werkzeug.security import generate_password_hash
    path=ROOT/'.env'
    if path.exists(): raise SystemExit('.env already exists; preserve it and edit settings explicitly')
    password=getpass.getpass('Initial password: ')
    if not password or password!=getpass.getpass('Confirm password: '): raise SystemExit('Password mismatch')
    body='\n'.join(['STOCK16_PASSWORD_HASH='+generate_password_hash(password),
                   'STOCK16_SECRET_KEY='+secrets.token_urlsafe(48),
                   'STOCK16_DATA_DIR='+str(ROOT/'data'),'STOCK16_BASE_PATH=/stock16-7',
                   'STOCK16_SECURE_COOKIE=1','STOCK16_PORT=3016','STOCK16_SEC_USER_AGENT=',''])
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as f: f.write(body)
    print('Server-only hash and random session key written to ignored .env (0600).')


def main():
    parser=argparse.ArgumentParser(prog='python -m stock16')
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('setup','init','serve','backup','status','check-storage','maintenance','vacuum'): sub.add_parser(name)
    collect=sub.add_parser('collect');collect.add_argument('--only',nargs='+');collect.add_argument('--config',type=Path)
    collect.add_argument('--origin',choices=['cli','scheduler'],default='cli')
    collect.add_argument('--lock-held',action='store_true',help='For systemd external flock only')
    worker=sub.add_parser('collector-worker');worker.add_argument('--once',action='store_true');worker.add_argument('--poll-interval',type=int,default=3)
    history=sub.add_parser('backfill');history.add_argument('collector',choices=['nbim','sec-13f'])
    history.add_argument('--guru');history.add_argument('--from-year',type=int);history.add_argument('--config',type=Path)
    retention=sub.add_parser('retention');retention.add_argument('--apply',action='store_true')
    ingest=sub.add_parser('import');ingest.add_argument('file',type=Path);ingest.add_argument('--config',type=Path,required=True);ingest.add_argument('--collector',required=True,choices=['nps','nbim','krx-etf','gic','temasek','dart'])
    restore=sub.add_parser('restore');restore.add_argument('file',type=Path);restore.add_argument('--target',type=Path,required=True)
    market_import=sub.add_parser('import-market');market_import.add_argument('file',type=Path);market_import.add_argument('--metadata',type=Path,required=True);market_import.add_argument('--format',choices=['csv','json'],default='csv')
    market_backfill=sub.add_parser('market-backfill');market_backfill.add_argument('--from',dest='start',default='2020-01-03');market_backfill.add_argument('--to',dest='end');market_backfill.add_argument('--markets',nargs='+',choices=['KOSPI','KOSDAQ'],default=['KOSPI','KOSDAQ'])
    args=parser.parse_args()
    if args.command=='setup': setup();return
    load_env()
    repo=Repository();repo.initialize()
    logs=repo.root/'logs';logs.mkdir(exist_ok=True)
    handler=WatchedFileHandler(logs/'app.log')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logging.getLogger('stock16').addHandler(handler)
    logging.getLogger('stock16').setLevel(logging.INFO)
    if args.command=='init': print('SQLite schema initialized.')
    elif args.command=='serve':
        from .web import create_app
        create_app().run(host='127.0.0.1',port=int(os.environ.get('STOCK16_PORT','3016')),debug=False)
    elif args.command=='collect':
        from .collectors import run_collectors
        from .jobs import collector_lock
        from contextlib import nullcontext
        with (nullcontext(True) if args.lock_held else collector_lock(repo)):
            results=run_collectors(repo,args.only,config_path=args.config,origin=args.origin)
        print(json.dumps(results,ensure_ascii=False,indent=2))
        if any(r['status']=='error' for r in results): raise SystemExit(1)
    elif args.command=='import-market':
        from .markets import import_market
        from .collectors.parsers import table
        from .jobs import collector_lock
        if args.file.stat().st_size>25*1024*1024:raise SystemExit('File size limit exceeded')
        body=args.file.read_bytes();metadata=json.loads(args.metadata.read_text())
        rows=table(body,args.format)
        with collector_lock(repo):result=import_market(repo,metadata,rows,body,'text/csv' if args.format=='csv' else 'application/json')
        print(json.dumps(dict(snapshot_id=result[0],created=result[1])))
    elif args.command=='market-backfill':
        from .collectors.market_ranks import MarketRankCollector
        from .collectors.transport import Transport
        from .jobs import collector_lock
        from .model import now
        run=repo.execute("INSERT INTO data_fetch_runs(collector,started_at,status,origin,requested_at) VALUES('market-ranks',?,'running','backfill',?)",(now(),now()))
        try:
            with collector_lock(repo):
                collector=MarketRankCollector(repo,Transport(),{})
                count=collector.run(start=args.start,end=args.end,budget=1000,markets=args.markets)
            status='partial' if collector.warnings else 'success' if count else 'no_new_data'
            message='주간 역사 snapshot '+str(count)+'건'+(' · '+'; '.join(collector.warnings) if collector.warnings else '')
        except Exception as error:
            from .collectors.base import NeedsConfiguration
            status='needs_configuration' if isinstance(error,NeedsConfiguration) else 'error'
            count=0;message=str(error) if isinstance(error,NeedsConfiguration) else '시장 backfill 실패 · '+type(error).__name__
        repo.execute('UPDATE data_fetch_runs SET finished_at=?,status=?,new_items=?,message=? WHERE id=?',(now(),status,count,message,run))
        print(json.dumps(dict(status=status,new_items=count,message=message),ensure_ascii=False))
        if status=='error':raise SystemExit(1)
    elif args.command=='collector-worker':
        from .jobs import worker
        worker(repo,once=args.once,poll_interval=args.poll_interval)
    elif args.command=='backfill':
        from .collectors import run_backfill
        from .jobs import collector_lock
        with collector_lock(repo):
            result=run_backfill(repo,args.collector,guru=args.guru,from_year=args.from_year,config_path=args.config)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if result['status']=='error': raise SystemExit(1)
    elif args.command in ('maintenance','vacuum'):
        from .storage import maintain_database
        result=maintain_database(repo,vacuum=args.command=='vacuum')
        from .jobs import cleanup
        result['jobs_deleted']=cleanup(repo)
        print(json.dumps(result,indent=2))
    elif args.command=='import':
        from .collectors.base import DatasetCollector
        from .collectors.transport import Transport
        item=json.loads(args.config.read_text())
        if args.file.stat().st_size>25*1024*1024: raise SystemExit('File size limit exceeded')
        collector=DatasetCollector(repo,Transport(),{'id':args.collector})
        rows=collector.normalize(collector.parse(args.file.read_bytes(),item),item)
        media={'csv':'text/csv','xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','json':'application/json'}[item['format']]
        print('Imported records:',collector.persist(item,args.file.read_bytes(),media,item['url'],rows))
    elif args.command=='backup':
        from .storage import backup
        print(backup(repo))
    elif args.command=='retention':
        from .storage import raw_retention
        result=raw_retention(repo,apply=args.apply);result.pop('candidates')
        if args.apply:
            from .jobs import cleanup
            result['jobs_deleted']=cleanup(repo)
            import time
            from datetime import datetime,timedelta,timezone
            repo.execute('DELETE FROM sessions WHERE expires_at<?',(int(time.time()),))
            repo.execute('DELETE FROM login_attempts WHERE window_start<?',(int(time.time())-900,))
            repo.execute('DELETE FROM data_fetch_runs WHERE finished_at<?',((datetime.now(timezone.utc)-timedelta(days=365)).isoformat(),))
        print(json.dumps(result,indent=2))
    elif args.command in ('status','check-storage'):
        from .storage import storage_status,check_storage_alert
        print(json.dumps((check_storage_alert if args.command=='check-storage' else storage_status)(repo),indent=2))
    elif args.command=='restore':
        target=args.target.resolve()
        if target.exists(): raise SystemExit('Restore target exists; refusing to overwrite')
        target.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as f:
            temporary=Path(f.name)
            try:
                with gzip.open(args.file,'rb') as g:
                    import shutil
                    shutil.copyfileobj(g,f)
                f.flush()
                with sqlite3.connect(temporary) as c:
                    if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('Corrupt backup')
                # Exclusive link prevents overwrite even if another process creates target.
                os.link(temporary,target)
            finally: temporary.unlink(missing_ok=True)
        print('Verified restored copy:',target)


if __name__=='__main__': main()
