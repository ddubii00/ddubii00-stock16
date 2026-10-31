"""Transactional, versioned upgrades of existing SQLite databases."""
import gzip
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

LATEST_VERSION = 2

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
    try:
        connection.execute('BEGIN IMMEDIATE')
        for statement in V2 if steps is None else steps:
            connection.execute(statement)
        if connection.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Migration foreign key validation failed')
        connection.execute('INSERT INTO schema_versions VALUES(?)', (LATEST_VERSION,))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return saved
