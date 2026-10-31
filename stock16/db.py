import json
import sqlite3
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
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript((Path(__file__).with_name('schema.sql')).read_text())
            config = json.loads((ROOT / 'config/watchlists.json').read_text())
            for i in config['institutions']:
                c.execute('INSERT OR IGNORE INTO institutions VALUES(:id,:name,:kind,:country,:scope,:cadence,:source_url)', i)
                c.execute('INSERT OR IGNORE INTO funds VALUES(?,?,?,?,?,?)',
                          (i['id'],i['id'],i['name'],i['kind'],None,i['source_url']))
            for f in config['etfs']:
                c.execute('INSERT OR IGNORE INTO funds VALUES(?,?,?,?,?,?)',
                          (f['id'],'kr-etf',f['name'],'ETF',f['code'],f['source_url']))
                c.execute('INSERT OR IGNORE INTO etf_watchlist VALUES(?,1)', (f['id'],))
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
