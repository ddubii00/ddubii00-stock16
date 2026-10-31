import json
import sqlite3
import fcntl
from contextlib import contextmanager
from pathlib import Path
from .settings import ROOT, data_dir


class Repository:
    """SQLite boundary: collectors and HTTP never open their own connections."""
    def __init__(self, root=None):
        self.root = Path(root or data_dir())
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'stock16.sqlite3'

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=30000')
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        existing = self.path.exists() and self.path.stat().st_size > 0
        with (self.root / '.migrations.lock').open('a') as lock, self.connect() as c:
            fcntl.flock(lock, fcntl.LOCK_EX)
            from .migrations import migrate, LATEST_VERSION
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='schema_versions'").fetchone():
                version=c.execute('SELECT max(version) FROM schema_versions').fetchone()[0]
                if version and version>LATEST_VERSION:
                    raise RuntimeError('Database version is newer than this application')
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript((Path(__file__).with_name('schema.sql')).read_text())
            migrate(c, self.root, existing=existing)
            config = json.loads((ROOT / 'config/watchlists.json').read_text())
            for i in config['institutions']:
                c.execute('INSERT OR IGNORE INTO institutions VALUES(:id,:name,:kind,:country,:scope,:cadence,:source_url)', i)
                c.execute('INSERT OR IGNORE INTO funds VALUES(?,?,?,?,?,?)',
                          (i['id'],i['id'],i['name'],i['kind'],None,i['source_url']))
            for f in config['etfs']:
                c.execute('INSERT OR IGNORE INTO funds VALUES(?,?,?,?,?,?)',
                          (f['id'],f.get('institution_id','kr-etf'),f['name'],'ETF',f['code'],f['source_url']))
                c.execute('INSERT OR IGNORE INTO etf_watchlist VALUES(?,?)', (f['id'],int(f.get('enabled',True))))
                c.execute('''INSERT INTO etf_profiles VALUES(?,?,?,?,?,?,?) ON CONFLICT(fund_id) DO UPDATE SET
                  style=excluded.style,listing_country=excluded.listing_country,manager=excluded.manager,
                  strategy=excluded.strategy,evidence_url=excluded.evidence_url,verified_on=excluded.verified_on''',
                  (f['id'],f.get('style','unknown'),f.get('listing_country','KR'),f.get('manager'),
                   f.get('strategy',f.get('category')),f.get('evidence_url',f['source_url']),f.get('verified_on')))
            for f in config.get('funds',[]):
                c.execute('INSERT OR IGNORE INTO funds VALUES(:id,:institution_id,:name,:kind,:code,:source_url)',f)
            for g in config['gurus']:
                c.execute('INSERT OR IGNORE INTO institutions VALUES(?,?,?,?,?,?,?)',
                          (g['id'],g['name'],'US Guru','US','13F disclosed long positions','quarterly',g['source_url']))
                c.execute('INSERT OR IGNORE INTO funds VALUES(?,?,?,?,?,?)',
                          (g['id'],g['id'],g['name'],'13F',None,g['source_url']))
                c.execute('INSERT OR IGNORE INTO guru_watchlist VALUES(?,?,NULL,1,?)',
                          (g['id'],g['cik'],'Medium'))

    def rows(self, sql, args=()):
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql,args)]

    def execute(self, sql, args=()):
        with self.connect() as c:
            return c.execute(sql,args).lastrowid
