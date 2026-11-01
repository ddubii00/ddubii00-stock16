"""Transactional, versioned upgrades of existing SQLite databases."""
import gzip
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

LATEST_VERSION = 5

V2 = (
    "ALTER TABLE portfolio_snapshots ADD COLUMN quantity_type TEXT NOT NULL DEFAULT 'shares'",
    """UPDATE portfolio_snapshots SET quantity_type='pdf_per_cu_quantity'
       WHERE fund_id IN (SELECT id FROM funds WHERE kind='ETF')""",
    "CREATE INDEX changes_previous ON holding_changes(previous_snapshot_id)",
    "CREATE INDEX observations_fund_dates ON source_observations(fund_id,as_of_date)",
    """CREATE TABLE source_status(
       entity_type TEXT NOT NULL,entity_id TEXT NOT NULL,component TEXT NOT NULL,
       collector TEXT NOT NULL,status TEXT NOT NULL,message TEXT,last_check TEXT NOT NULL,
       last_success TEXT,next_scheduled TEXT,source_url TEXT,
       PRIMARY KEY(entity_type,entity_id,component))""",
    """CREATE TABLE history_review_queue(
       collector TEXT NOT NULL,fund_id TEXT NOT NULL,as_of_date TEXT NOT NULL,
       source_url TEXT NOT NULL,reason TEXT NOT NULL,discovered_at TEXT NOT NULL,
       PRIMARY KEY(collector,fund_id,as_of_date))""",
)

V3 = (
    """CREATE TABLE etf_profiles(
       fund_id TEXT PRIMARY KEY REFERENCES funds(id),
       style TEXT NOT NULL CHECK(style IN ('active','passive','unknown')),
       listing_country TEXT NOT NULL CHECK(listing_country IN ('KR','US')),
       manager TEXT,strategy TEXT,evidence_url TEXT,verified_on TEXT)""",
    # One-time change of the old default universe; no historical rows are removed.
    """UPDATE etf_watchlist SET enabled=0 WHERE fund_id IN (
       'etf-069500','etf-229200','etf-091160','etf-305720','etf-091180',
       'etf-091170','etf-244580','etf-379800','etf-379810','etf-390390',
       'etf-360750','etf-466920','etf-266550')""",
)

V4 = (
    """CREATE TABLE collection_jobs(
       id INTEGER PRIMARY KEY,collector TEXT NOT NULL CHECK(collector IN ('nps','nbim','krx-etf','sec-13f','gic','temasek','dart','all')),
       requested_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,
       status TEXT NOT NULL CHECK(status IN ('queued','running','success','partial','no_new_data','needs_configuration','error','cancelled')),
       message TEXT NOT NULL DEFAULT '',new_items INTEGER NOT NULL DEFAULT 0,
       requested_by TEXT NOT NULL,attempt_count INTEGER NOT NULL DEFAULT 0,
       results_json TEXT NOT NULL DEFAULT '[]')""",
    "CREATE UNIQUE INDEX collection_jobs_active ON collection_jobs(collector) WHERE status IN ('queued','running')",
    "CREATE INDEX collection_jobs_pending ON collection_jobs(status,id)",
    "CREATE INDEX collection_jobs_finished ON collection_jobs(finished_at)",
    "ALTER TABLE data_fetch_runs ADD COLUMN origin TEXT NOT NULL DEFAULT 'legacy'",
    "ALTER TABLE data_fetch_runs ADD COLUMN requested_at TEXT",
    "ALTER TABLE data_fetch_runs ADD COLUMN job_id INTEGER REFERENCES collection_jobs(id) ON DELETE SET NULL",
    "ALTER TABLE data_fetch_runs ADD COLUMN details_json TEXT",
)

V5 = (
    # Rebuild the allowlist CHECK while retaining queue IDs and run references.
    """CREATE TABLE collection_jobs_v5(
       id INTEGER PRIMARY KEY,collector TEXT NOT NULL CHECK(collector IN ('nps','nbim','krx-etf','sec-13f','gic','temasek','dart','all','market-ranks','msci-korea')),
       requested_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,
       status TEXT NOT NULL CHECK(status IN ('queued','running','success','partial','no_new_data','needs_configuration','error','cancelled')),
       message TEXT NOT NULL DEFAULT '',new_items INTEGER NOT NULL DEFAULT 0,
       requested_by TEXT NOT NULL,attempt_count INTEGER NOT NULL DEFAULT 0,
       results_json TEXT NOT NULL DEFAULT '[]')""",
    "INSERT INTO collection_jobs_v5 SELECT * FROM collection_jobs",
    "DROP TABLE collection_jobs",
    "ALTER TABLE collection_jobs_v5 RENAME TO collection_jobs",
    "CREATE UNIQUE INDEX collection_jobs_active ON collection_jobs(collector) WHERE status IN ('queued','running')",
    "CREATE INDEX collection_jobs_pending ON collection_jobs(status,id)",
    "CREATE INDEX collection_jobs_finished ON collection_jobs(finished_at)",
    """CREATE TABLE market_listings(
       id INTEGER PRIMARY KEY,security_id INTEGER NOT NULL REFERENCES securities(id),
       market TEXT NOT NULL,ticker TEXT NOT NULL,name TEXT NOT NULL,instrument TEXT NOT NULL,
       UNIQUE(security_id,market,ticker,name,instrument))""",
    """CREATE TABLE market_snapshots(
       id INTEGER PRIMARY KEY,market TEXT NOT NULL CHECK(market IN ('KOSPI','KOSDAQ','NASDAQ')),
       week_end TEXT NOT NULL,as_of_date TEXT NOT NULL,currency TEXT NOT NULL,
       universe_count INTEGER NOT NULL,excluded_count INTEGER NOT NULL DEFAULT 0,
       universe_definition TEXT NOT NULL,source_type TEXT NOT NULL,
       observation_id INTEGER NOT NULL REFERENCES source_observations(id),
       checksum TEXT NOT NULL REFERENCES source_documents(checksum),created_at TEXT NOT NULL,
       UNIQUE(market,as_of_date,checksum))""",
    "CREATE INDEX market_snapshot_weeks ON market_snapshots(market,week_end,as_of_date,id)",
    """CREATE TABLE market_ranks(
       snapshot_id INTEGER NOT NULL REFERENCES market_snapshots(id),
       security_id INTEGER NOT NULL REFERENCES securities(id),
       listing_id INTEGER NOT NULL REFERENCES market_listings(id),
       rank INTEGER NOT NULL,market_cap TEXT NOT NULL,
       PRIMARY KEY(snapshot_id,security_id),UNIQUE(snapshot_id,rank)) WITHOUT ROWID""",
    "CREATE INDEX market_ranks_security ON market_ranks(security_id,snapshot_id)",
    """CREATE TABLE msci_reviews(
       id INTEGER PRIMARY KEY,index_id TEXT NOT NULL,review_period TEXT NOT NULL,
       announced_at TEXT NOT NULL,effective_close TEXT NOT NULL,
       additions_count INTEGER NOT NULL,deletions_count INTEGER NOT NULL,
       observation_id INTEGER NOT NULL REFERENCES source_observations(id),
       checksum TEXT NOT NULL REFERENCES source_documents(checksum),
       created_at TEXT NOT NULL,UNIQUE(index_id,review_period,checksum))""",
    """CREATE TABLE msci_review_changes(
       review_id INTEGER NOT NULL REFERENCES msci_reviews(id),
       action TEXT NOT NULL CHECK(action IN ('ADDED','DELETED')),name TEXT NOT NULL,
       PRIMARY KEY(review_id,action,name)) WITHOUT ROWID""",
)

MIGRATIONS={2:V2,3:V3,4:V4,5:V5}


def migration_backup(connection, root, version):
    """Independent pre-upgrade copy, never pruned by routine GFS rotation."""
    folder = Path(root) / 'backups' / 'migrations'
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / f'before-v{version}-{time.time_ns()}.sqlite3.gz'
    with tempfile.TemporaryDirectory(dir=folder) as temporary:
        plain = Path(temporary) / 'copy.sqlite3'
        with sqlite3.connect(plain) as copy:
            connection.backup(copy)
            if copy.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Pre-migration backup integrity check failed')
        packed = Path(temporary) / 'copy.gz'
        with plain.open('rb') as source, gzip.open(packed, 'wb') as target:
            shutil.copyfileobj(source, target)
        os.replace(packed, destination)
    return destination


def migrate(connection, root, *, existing=False, steps=None):
    version = connection.execute('SELECT max(version) FROM schema_versions').fetchone()[0] or 1
    if version > LATEST_VERSION:
        raise RuntimeError('Database is newer than this application; refusing downgrade')
    if version == LATEST_VERSION:
        return None
    saved = migration_backup(connection, root, version) if existing else None
    # SQLite requires FK suspension outside the transaction for an atomic
    # referenced-table rebuild. Validate all references before commit and restore.
    connection.execute('PRAGMA foreign_keys=OFF')
    try:
        connection.execute('BEGIN IMMEDIATE')
        if steps is not None:
            for statement in steps: connection.execute(statement)
            connection.execute('INSERT INTO schema_versions VALUES(?)', (LATEST_VERSION,))
        else:
            for target in range(version+1,LATEST_VERSION+1):
                for statement in MIGRATIONS[target]: connection.execute(statement)
                connection.execute('INSERT INTO schema_versions VALUES(?)', (target,))
        if connection.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Migration foreign key validation failed')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute('PRAGMA foreign_keys=ON')
    return saved
