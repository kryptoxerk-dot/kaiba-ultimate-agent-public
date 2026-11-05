-- Core tables: the event bus, wallets, tokens, swaps and provider bookkeeping.

-- Append-only event bus. One write path feeds the dashboard (SSE), the nightly
-- reflection job and the trace viewer, so every consumer sees the same truth.
CREATE TABLE IF NOT EXISTS events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms        INTEGER NOT NULL,
  kind         TEXT    NOT NULL,
  level        TEXT    NOT NULL DEFAULT 'info',
  chain        TEXT,
  subject      TEXT,
  payload      TEXT    NOT NULL DEFAULT '{}',
  trace_id     TEXT,
  dedupe_key   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts    ON events(ts_ms);
CREATE INDEX IF NOT EXISTS idx_events_kind  ON events(kind, id);
CREATE INDEX IF NOT EXISTS idx_events_subj  ON events(subject, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedupe ON events(dedupe_key) WHERE dedupe_key IS NOT NULL;

-- Tracked wallets.
CREATE TABLE IF NOT EXISTS wallets (
  chain            TEXT NOT NULL,
  address          TEXT NOT NULL,
  name             TEXT,
  source           TEXT NOT NULL DEFAULT 'unknown',
  tags_json        TEXT NOT NULL DEFAULT '[]',
  first_seen_ms    INTEGER NOT NULL,
  last_seen_ms     INTEGER NOT NULL,
  first_funder     TEXT,
  first_funded_ms  INTEGER,
  twitter          TEXT,
  cohort           TEXT,                      -- tracked | trusted_copy | blacklist | research
  meta_json        TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (chain, address)
);
CREATE INDEX IF NOT EXISTS idx_wallets_cohort ON wallets(cohort);
CREATE INDEX IF NOT EXISTS idx_wallets_funder ON wallets(chain, first_funder);

-- Latest grade per wallet (history lives in wallet_score_history).
CREATE TABLE IF NOT EXISTS wallet_scores (
  chain             TEXT NOT NULL,
  address           TEXT NOT NULL,
  score             REAL NOT NULL,
  grade             TEXT NOT NULL,
  evidence_weight   REAL NOT NULL,
  archetype         TEXT NOT NULL,
  realized_pnl_usd  TEXT,
  win_rate          REAL,
  closed_trades     INTEGER,
  distinct_tokens   INTEGER,
  median_hold_s     INTEGER,
  factors_json      TEXT NOT NULL DEFAULT '[]',
  penalties_json    TEXT NOT NULL DEFAULT '[]',
  blockers_json     TEXT NOT NULL DEFAULT '[]',
  receipts_json     TEXT NOT NULL DEFAULT '[]',
  model_version     TEXT NOT NULL,
  scored_at_ms      INTEGER NOT NULL,
  PRIMARY KEY (chain, address)
);
CREATE INDEX IF NOT EXISTS idx_wallet_scores_grade ON wallet_scores(grade, score DESC);

CREATE TABLE IF NOT EXISTS wallet_score_history (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  chain         TEXT NOT NULL,
  address       TEXT NOT NULL,
  score         REAL NOT NULL,
  grade         TEXT NOT NULL,
  scored_at_ms  INTEGER NOT NULL,
  model_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wsh ON wallet_score_history(chain, address, scored_at_ms);

-- Tokens seen by any listener.
CREATE TABLE IF NOT EXISTS tokens (
  chain        TEXT NOT NULL,
  address      TEXT NOT NULL,
  symbol       TEXT,
  name         TEXT,
  decimals     INTEGER,
  creator      TEXT,
  created_ms   INTEGER,
  launchpad    TEXT,
  pool         TEXT,
  migrated_ms  INTEGER,
  first_seen_ms INTEGER NOT NULL,
  meta_json    TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (chain, address)
);
CREATE INDEX IF NOT EXISTS idx_tokens_created ON tokens(created_ms DESC);
CREATE INDEX IF NOT EXISTS idx_tokens_creator ON tokens(chain, creator);

-- Observed swaps: the raw material for grading, clustering and confluence.
CREATE TABLE IF NOT EXISTS swaps (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  chain         TEXT NOT NULL,
  tx            TEXT NOT NULL,
  slot          INTEGER,
  block_index   INTEGER,
  ts_ms         INTEGER NOT NULL,
  wallet        TEXT NOT NULL,
  token         TEXT NOT NULL,
  side          TEXT NOT NULL,              -- buy | sell
  amount_token  TEXT,                       -- token atoms, as text to keep precision
  amount_native TEXT,                       -- lamports/wei, as text
  price_usd     TEXT,
  usd_value     TEXT,
  program       TEXT,
  source        TEXT NOT NULL,              -- which provider told us
  is_create_tx  INTEGER NOT NULL DEFAULT 0,
  fee_payer     TEXT,
  UNIQUE (chain, tx, wallet, token, side, amount_token)
);
CREATE INDEX IF NOT EXISTS idx_swaps_wallet ON swaps(chain, wallet, ts_ms);
CREATE INDEX IF NOT EXISTS idx_swaps_token  ON swaps(chain, token, ts_ms);
CREATE INDEX IF NOT EXISTS idx_swaps_slot   ON swaps(chain, slot);

-- Native transfers, used for funding-source clustering.
CREATE TABLE IF NOT EXISTS transfers (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  chain         TEXT NOT NULL,
  tx            TEXT NOT NULL,
  slot          INTEGER,
  ts_ms         INTEGER NOT NULL,
  src           TEXT NOT NULL,
  dst           TEXT NOT NULL,
  amount        TEXT NOT NULL,
  is_first_inbound INTEGER NOT NULL DEFAULT 0,
  source        TEXT NOT NULL,
  UNIQUE (chain, tx, src, dst, amount)
);
CREATE INDEX IF NOT EXISTS idx_transfers_dst ON transfers(chain, dst, ts_ms);
CREATE INDEX IF NOT EXISTS idx_transfers_src ON transfers(chain, src, ts_ms);

-- Provider call accounting: every request, its weight and outcome.
CREATE TABLE IF NOT EXISTS provider_calls (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  provider    TEXT NOT NULL,
  endpoint    TEXT NOT NULL,
  weight      INTEGER NOT NULL DEFAULT 1,
  ts_ms       INTEGER NOT NULL,
  latency_ms  INTEGER,
  status      TEXT NOT NULL,                -- ok | error | rate_limited | denied
  detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_provider_calls ON provider_calls(provider, ts_ms);

-- Leaky-bucket state, shared across processes through this table.
CREATE TABLE IF NOT EXISTS provider_state (
  provider        TEXT PRIMARY KEY,
  credit_milli    INTEGER NOT NULL DEFAULT 0,
  last_refill_ms  INTEGER NOT NULL,
  last_call_ms    INTEGER NOT NULL DEFAULT 0,
  inflight        INTEGER NOT NULL DEFAULT 0,
  banned_until_ms INTEGER NOT NULL DEFAULT 0,
  penalty_level   INTEGER NOT NULL DEFAULT 0,
  spent_today     INTEGER NOT NULL DEFAULT 0,
  day_key         TEXT
);

CREATE TABLE IF NOT EXISTS provider_family_bans (
  provider        TEXT NOT NULL,
  family          TEXT NOT NULL,
  banned_until_ms INTEGER NOT NULL,
  reason          TEXT,
  PRIMARY KEY (provider, family)
);

-- Generic key/value for cursors, watermarks and small runtime state.
CREATE TABLE IF NOT EXISTS kv (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_ms INTEGER NOT NULL
);
