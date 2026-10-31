PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_versions(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_versions VALUES(1);
CREATE TABLE IF NOT EXISTS institutions(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
 country TEXT, scope TEXT NOT NULL, cadence TEXT, source_url TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS funds(
 id TEXT PRIMARY KEY, institution_id TEXT NOT NULL REFERENCES institutions(id),
 name TEXT NOT NULL, kind TEXT NOT NULL, code TEXT, source_url TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS etf_watchlist(
 fund_id TEXT PRIMARY KEY REFERENCES funds(id), enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS guru_watchlist(
 institution_id TEXT PRIMARY KEY REFERENCES institutions(id), cik TEXT, corp_code TEXT,
 enabled INTEGER NOT NULL DEFAULT 1, disclosure_level TEXT NOT NULL DEFAULT 'Medium');
CREATE TABLE IF NOT EXISTS issuers(
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, cik TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS securities(
 id INTEGER PRIMARY KEY, issuer_id INTEGER REFERENCES issuers(id),
 identity_key TEXT NOT NULL UNIQUE, name TEXT NOT NULL, isin TEXT, cusip TEXT,
 kr_code TEXT, ticker TEXT, exchange TEXT, country TEXT, sector TEXT,
 instrument TEXT NOT NULL DEFAULT 'equity', review_status TEXT NOT NULL DEFAULT 'verified');
CREATE TABLE IF NOT EXISTS security_identifiers(
 scheme TEXT NOT NULL, value TEXT NOT NULL, security_id INTEGER NOT NULL REFERENCES securities(id),
 PRIMARY KEY(scheme,value));
CREATE TABLE IF NOT EXISTS security_aliases(
 security_id INTEGER REFERENCES securities(id), ticker TEXT, exchange TEXT, valid_from TEXT,
 valid_to TEXT, source_url TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS corporate_actions(
 security_id INTEGER REFERENCES securities(id), effective_date TEXT, ratio TEXT NOT NULL,
 source_url TEXT NOT NULL, PRIMARY KEY(security_id,effective_date));
CREATE TABLE IF NOT EXISTS source_documents(
 checksum TEXT PRIMARY KEY, media_type TEXT NOT NULL, raw_path TEXT,
 original_bytes INTEGER NOT NULL, compressed_bytes INTEGER NOT NULL,
 created_at TEXT NOT NULL, purged_at TEXT);
CREATE TABLE IF NOT EXISTS source_observations(
 id INTEGER PRIMARY KEY, checksum TEXT REFERENCES source_documents(checksum),
 fund_id TEXT REFERENCES funds(id), as_of_date TEXT NOT NULL, published_at TEXT,
 fetched_at TEXT NOT NULL, source_url TEXT NOT NULL, source_type TEXT NOT NULL,
 UNIQUE(checksum,fund_id,as_of_date,published_at,source_url));
CREATE TABLE IF NOT EXISTS holding_sets(id INTEGER PRIMARY KEY,hash TEXT NOT NULL UNIQUE, count INTEGER NOT NULL);
-- Numeric values use canonical Decimal strings, avoiding floating point hash drift.
CREATE TABLE IF NOT EXISTS holding_values(
 id INTEGER PRIMARY KEY,hash BLOB NOT NULL UNIQUE, security_id INTEGER NOT NULL REFERENCES securities(id),
 position_key TEXT NOT NULL DEFAULT '', shares TEXT, market_value TEXT, weight TEXT,
 ownership_pct TEXT, currency TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS holding_members(
 set_id INTEGER NOT NULL REFERENCES holding_sets(id), value_id INTEGER NOT NULL REFERENCES holding_values(id),
 PRIMARY KEY(set_id,value_id)) WITHOUT ROWID;
CREATE VIEW IF NOT EXISTS holdings AS SELECT s.hash set_hash,v.hash value_hash
 FROM holding_members m JOIN holding_sets s ON s.id=m.set_id JOIN holding_values v ON v.id=m.value_id;
CREATE TABLE IF NOT EXISTS portfolio_snapshots(
 id INTEGER PRIMARY KEY, fund_id TEXT NOT NULL REFERENCES funds(id),
 institution_id TEXT NOT NULL REFERENCES institutions(id), as_of_date TEXT NOT NULL,
 published_at TEXT, fetched_at TEXT NOT NULL, source_url TEXT NOT NULL,
 source_type TEXT NOT NULL, checksum TEXT NOT NULL REFERENCES source_documents(checksum),
 currency TEXT NOT NULL, data_quality TEXT NOT NULL, disclosure_scope TEXT NOT NULL,
 set_hash TEXT NOT NULL REFERENCES holding_sets(hash),
 UNIQUE(fund_id,as_of_date,published_at,checksum));
CREATE INDEX IF NOT EXISTS snapshot_dates ON portfolio_snapshots(fund_id,as_of_date,published_at);
CREATE UNIQUE INDEX IF NOT EXISTS snapshot_identity ON portfolio_snapshots(fund_id,as_of_date,coalesce(published_at,''),checksum);
CREATE UNIQUE INDEX IF NOT EXISTS observation_identity ON source_observations(checksum,fund_id,as_of_date,coalesce(published_at,''),source_url);
CREATE VIEW IF NOT EXISTS effective_snapshots AS
 SELECT * FROM (SELECT p.*,row_number() OVER(
 PARTITION BY fund_id,as_of_date ORDER BY published_at DESC,id DESC) AS revision_rank
 FROM portfolio_snapshots p) WHERE revision_rank=1;
CREATE TABLE IF NOT EXISTS holding_changes(
 snapshot_id INTEGER NOT NULL REFERENCES portfolio_snapshots(id),
 previous_snapshot_id INTEGER REFERENCES portfolio_snapshots(id),
 security_id INTEGER NOT NULL REFERENCES securities(id), position_key TEXT NOT NULL,
 status TEXT NOT NULL, shares_delta TEXT, shares_delta_pct TEXT,
 market_value_delta TEXT, portfolio_weight_delta_pp TEXT,
 raw_shares_delta TEXT, corporate_action TEXT,
 PRIMARY KEY(snapshot_id,security_id,position_key));
CREATE TABLE IF NOT EXISTS asset_allocation_snapshots(
 id INTEGER PRIMARY KEY, institution_id TEXT NOT NULL REFERENCES institutions(id),
 as_of_date TEXT NOT NULL,published_at TEXT NOT NULL,fetched_at TEXT NOT NULL,
 original_category TEXT NOT NULL,standard_category TEXT NOT NULL,dimension TEXT NOT NULL DEFAULT 'asset',
 weight TEXT,target_weight TEXT,market_value TEXT,currency TEXT NOT NULL,
 source_url TEXT NOT NULL,checksum TEXT NOT NULL REFERENCES source_documents(checksum),
 data_quality TEXT NOT NULL DEFAULT 'Official',
 UNIQUE(institution_id,as_of_date,published_at,original_category,dimension,checksum));
CREATE TABLE IF NOT EXISTS disclosures(
 id INTEGER PRIMARY KEY,institution_id TEXT NOT NULL REFERENCES institutions(id),
 external_id TEXT NOT NULL,title TEXT NOT NULL,published_at TEXT NOT NULL,as_of_date TEXT,
 fetched_at TEXT NOT NULL,kind TEXT NOT NULL,importance INTEGER NOT NULL DEFAULT 0,
 summary TEXT,source_url TEXT NOT NULL,checksum TEXT,
 UNIQUE(institution_id,external_id));
CREATE TABLE IF NOT EXISTS data_fetch_runs(
 id INTEGER PRIMARY KEY,collector TEXT NOT NULL,started_at TEXT NOT NULL,
 finished_at TEXT,status TEXT NOT NULL,message TEXT,new_items INTEGER NOT NULL DEFAULT 0,
 next_scheduled TEXT);
CREATE TABLE IF NOT EXISTS collector_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts(key TEXT PRIMARY KEY,window_start INTEGER NOT NULL,count INTEGER NOT NULL);
