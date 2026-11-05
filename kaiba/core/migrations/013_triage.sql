-- Tier-0 triage: the cheap screen that stands between 14 launches a minute and a
-- 7.6-second safety scan that can only absorb ~8 of them (docs/AUDIT-2026-09-20.md).
--
-- Three tables, each earning its place:
--
-- 1. `triage_decisions` records EVERY tier-0 verdict, rejections included. A filter that
--    is never measured is a filter nobody can tell is broken, and this is also the only
--    way to measure exclusion precision without placing a trade: label the rejects, come
--    back in an hour, and ask what they did. Same rule as `decisions` in 003.
-- 2. `triage_fingerprints` is a local metadata-fingerprint index so "have we seen this
--    name/symbol/image before" is one indexed primary-key read rather than a table scan.
--    `kaiba/intelligence/dedup.py` owns the real copycat classifier; this is the floor
--    that keeps tier 0 working before it lands and stays useful as a fast pre-check after.
-- 3. `triage_backpressure` snapshots the tier-1 queue so "how far behind are we" survives
--    a process restart and is answerable from the dashboard rather than from a log.

CREATE TABLE IF NOT EXISTS triage_decisions (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms          INTEGER NOT NULL,
  chain          TEXT    NOT NULL,
  token          TEXT    NOT NULL,
  verdict        TEXT    NOT NULL,            -- reject | defer | promote
  score          REAL    NOT NULL DEFAULT 0,
  reasons_json   TEXT    NOT NULL DEFAULT '[]',
  -- Fields we could not read. A tier-0 reject is never allowed to rest on one of these,
  -- so this column is the audit of that rule: a rejection whose reason names an unknown
  -- is a bug, and it is queryable.
  unknowns_json  TEXT    NOT NULL DEFAULT '[]',
  factors_json   TEXT    NOT NULL DEFAULT '[]',
  creator        TEXT,
  fingerprint    TEXT,
  source         TEXT    NOT NULL DEFAULT 'pumpportal',
  launchpad      TEXT,
  event_ms       INTEGER,                     -- provider-side timestamp, when it gave one
  latency_us     INTEGER,                     -- how long tier 0 itself took
  params_version TEXT    NOT NULL DEFAULT 'triage-v1'
);
CREATE INDEX IF NOT EXISTS idx_triage_ts      ON triage_decisions(ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_triage_verdict ON triage_decisions(verdict, ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_triage_token   ON triage_decisions(chain, token, ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_triage_creator ON triage_decisions(chain, creator, ts_ms DESC);

-- One row per distinct metadata fingerprint. `hits` counts re-uses, so the first launch
-- of a fingerprint is not penalised and the twentieth is.
CREATE TABLE IF NOT EXISTS triage_fingerprints (
  chain         TEXT    NOT NULL,
  fingerprint   TEXT    NOT NULL,
  first_token   TEXT    NOT NULL,
  first_seen_ms INTEGER NOT NULL,
  last_seen_ms  INTEGER NOT NULL,
  hits          INTEGER NOT NULL DEFAULT 1,
  kind          TEXT    NOT NULL DEFAULT 'meta',  -- meta | image | name
  PRIMARY KEY (chain, fingerprint, kind)
);
CREATE INDEX IF NOT EXISTS idx_triage_fp_seen ON triage_fingerprints(chain, last_seen_ms DESC);

-- Backpressure snapshots. Written periodically by whoever owns the queue.
CREATE TABLE IF NOT EXISTS triage_backpressure (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms           INTEGER NOT NULL,
  queue_name      TEXT    NOT NULL DEFAULT 'tier1',
  depth           INTEGER NOT NULL,
  capacity        INTEGER NOT NULL,
  admitted        INTEGER NOT NULL DEFAULT 0,
  shed            INTEGER NOT NULL DEFAULT 0,
  served          INTEGER NOT NULL DEFAULT 0,
  oldest_age_ms   INTEGER,
  arrival_per_min REAL,
  service_per_min REAL,
  drain_eta_s     REAL,                        -- NULL when the queue can never drain
  saturated       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_triage_bp ON triage_backpressure(queue_name, ts_ms DESC);
