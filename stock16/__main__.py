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
    for name in ('setup','init','serve','backup','status','check-storage'): sub.add_parser(name)
    collect=sub.add_parser('collect');collect.add_argument('--only',nargs='+');collect.add_argument('--config',type=Path)
    retention=sub.add_parser('retention');retention.add_argument('--apply',action='store_true')
    ingest=sub.add_parser('import');ingest.add_argument('file',type=Path);ingest.add_argument('--config',type=Path,required=True);ingest.add_argument('--collector',required=True,choices=['nps','nbim','krx-etf','gic','temasek','dart'])
    restore=sub.add_parser('restore');restore.add_argument('file',type=Path);restore.add_argument('--target',type=Path,required=True)
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
        results=run_collectors(repo,args.only,config_path=args.config)
        print(json.dumps(results,ensure_ascii=False,indent=2))
        if any(r['status']=='error' for r in results): raise SystemExit(1)
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
