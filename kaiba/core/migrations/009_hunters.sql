-- Hunters: airdrops/points, NFT mints, exchange listings.
--
-- `opportunities` already exists (003_execution.sql) and keeps its meaning: one row per
-- deduped opportunity, scored by kaiba.hunters.ev. This migration adds the columns the
-- scorer needs to be auditable from SQL alone, plus three tables 003 has no place for:
--
--   listing_events        every detected listing WITH its measured detection latency.
--                         Latency is the whole point of the listing lane — if we cannot
--                         measure it we cannot know whether the edge exists, so it gets a
--                         table and not a payload field.
--   hunter_webhook_events raw Helius NFT_MINT / CANDY_MACHINE_UPDATE deliveries. The
--                         webhook receiver writes; the hunter parses later. Keeping the
--                         raw body means a parser bug is replayable instead of lost.
--   hunter_sources        per-collector health, so a source that quietly went to zero
--                         after a layout change is visible instead of just absent.

ALTER TABLE opportunities ADD COLUMN source TEXT;
ALTER TABLE opportunities ADD COLUMN symbol TEXT;
ALTER TABLE opportunities ADD COLUMN confidence REAL;
ALTER TABLE opportunities ADD COLUMN gross_usd TEXT;
ALTER TABLE opportunities ADD COLUMN warnings_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE opportunities ADD COLUMN rationale_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE opportunities ADD COLUMN last_seen_ms INTEGER;

CREATE INDEX IF NOT EXISTS idx_opps_deadline ON opportunities(deadline_ms);

CREATE TABLE IF NOT EXISTS listing_events (
  listing_id    TEXT PRIMARY KEY,          -- digest(exchange, symbol, announced_ms)
  exchange      TEXT NOT NULL,
  symbol        TEXT NOT NULL,
  title         TEXT,
  url           TEXT,
  chain         TEXT,
  token         TEXT,                      -- resolved contract address, NULL when ambiguous
  announced_ms  INTEGER,                   -- NULL when the source publishes no timestamp
  detected_ms   INTEGER NOT NULL,
  latency_ms    INTEGER,                   -- NULL means unmeasurable, never 0
  source        TEXT NOT NULL,
  payload_json  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_listing_detected ON listing_events(detected_ms DESC);
CREATE INDEX IF NOT EXISTS idx_listing_symbol   ON listing_events(symbol, detected_ms DESC);

CREATE TABLE IF NOT EXISTS hunter_webhook_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  provider      TEXT NOT NULL,             -- helius
  event_type    TEXT NOT NULL,             -- NFT_MINT | CANDY_MACHINE_UPDATE | ...
  signature     TEXT,
  received_ms   INTEGER NOT NULL,
  processed_ms  INTEGER,
  payload_json  TEXT NOT NULL,
  UNIQUE (provider, event_type, signature)
);
CREATE INDEX IF NOT EXISTS idx_hunter_webhook_unprocessed
  ON hunter_webhook_events(processed_ms, id);

CREATE TABLE IF NOT EXISTS hunter_sources (
  name          TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,             -- airdrop | nft_mint | listing
  last_run_ms   INTEGER,
  last_ok_ms    INTEGER,
  last_count    INTEGER NOT NULL DEFAULT 0,
  fail_streak   INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT
);
