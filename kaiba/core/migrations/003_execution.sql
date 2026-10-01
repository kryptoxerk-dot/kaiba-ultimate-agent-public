-- Signals, decisions, orders, positions, journal, hunters.
-- Live and shadow rows share these tables and are separated by `mode`, so promotion
-- decisions compare like with like on the same candidate stream.

CREATE TABLE IF NOT EXISTS signals (
  signal_id   TEXT PRIMARY KEY,
  lane        TEXT NOT NULL,
  chain       TEXT NOT NULL,
  token       TEXT NOT NULL,
  strength    REAL NOT NULL DEFAULT 0,
  reasons_json TEXT NOT NULL DEFAULT '[]',
  wallets_json TEXT NOT NULL DEFAULT '[]',
  entities_json TEXT NOT NULL DEFAULT '[]',
  window_s    INTEGER,
  created_ms  INTEGER NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_signals_token ON signals(chain, token, created_ms DESC);
CREATE INDEX IF NOT EXISTS idx_signals_lane  ON signals(lane, created_ms DESC);

-- Every decision, including SKIP. Skips are how we learn what we missed.
CREATE TABLE IF NOT EXISTS decisions (
  decision_id      TEXT PRIMARY KEY,
  ts_ms            INTEGER NOT NULL,
  lane             TEXT NOT NULL,
  mode             TEXT NOT NULL,
  chain            TEXT NOT NULL,
  token            TEXT NOT NULL,
  action           TEXT NOT NULL,
  thesis           TEXT,
  confidence       REAL,
  signals_json     TEXT NOT NULL DEFAULT '[]',
  dossier_grade    TEXT,
  size_base_units  INTEGER,
  size_pct_bankroll REAL,
  expected_return_pct REAL,
  invalidation     TEXT,
  regime           TEXT,
  blockers_json    TEXT NOT NULL DEFAULT '[]',
  params_version   TEXT NOT NULL DEFAULT 'v1',
  model            TEXT,
  trace_id         TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_lane ON decisions(lane, mode, ts_ms DESC);

CREATE TABLE IF NOT EXISTS orders (
  order_id      TEXT PRIMARY KEY,
  decision_id   TEXT,
  chain         TEXT NOT NULL,
  token         TEXT NOT NULL,
  side          TEXT NOT NULL,
  lane          TEXT NOT NULL,
  mode          TEXT NOT NULL,
  input_token   TEXT NOT NULL,
  output_token  TEXT NOT NULL,
  amount_in     TEXT NOT NULL,
  min_out       TEXT NOT NULL,
  slippage_bps  INTEGER NOT NULL,
  state         TEXT NOT NULL,
  provider      TEXT NOT NULL,
  provider_order_id TEXT,
  tx_hash       TEXT,
  filled_out    TEXT,
  fee_native    TEXT,
  created_ms    INTEGER NOT NULL,
  updated_ms    INTEGER NOT NULL,
  error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_state ON orders(state, updated_ms DESC);
CREATE INDEX IF NOT EXISTS idx_orders_token ON orders(chain, token);

-- Append-only order state log: an order's history is never overwritten.
CREATE TABLE IF NOT EXISTS order_events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id  TEXT NOT NULL,
  ts_ms     INTEGER NOT NULL,
  state     TEXT NOT NULL,
  detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_order_events ON order_events(order_id, id);

CREATE TABLE IF NOT EXISTS positions (
  position_id      TEXT PRIMARY KEY,
  chain            TEXT NOT NULL,
  token            TEXT NOT NULL,
  lane             TEXT NOT NULL,
  mode             TEXT NOT NULL,
  opened_ms        INTEGER NOT NULL,
  closed_ms        INTEGER,
  qty              TEXT NOT NULL DEFAULT '0',
  qty_total        TEXT NOT NULL DEFAULT '0',
  cost_native      TEXT NOT NULL DEFAULT '0',
  proceeds_native  TEXT NOT NULL DEFAULT '0',
  realized_native  TEXT NOT NULL DEFAULT '0',
  entry_price_usd  TEXT,
  peak_price_usd   TEXT,
  stop_price_usd   TEXT,
  tp_done_json     TEXT NOT NULL DEFAULT '[]',
  protected        INTEGER NOT NULL DEFAULT 0,
  protection_ids_json TEXT NOT NULL DEFAULT '[]',
  mae_pct          REAL,
  mfe_pct          REAL,
  exit_reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_open ON positions(closed_ms, mode);
CREATE INDEX IF NOT EXISTS idx_positions_token ON positions(chain, token);

CREATE TABLE IF NOT EXISTS trades (
  trade_id      TEXT PRIMARY KEY,
  position_id   TEXT NOT NULL,
  decision_id   TEXT,
  lane          TEXT NOT NULL,
  mode          TEXT NOT NULL,
  chain         TEXT NOT NULL,
  token         TEXT NOT NULL,
  opened_ms     INTEGER NOT NULL,
  closed_ms     INTEGER NOT NULL,
  hold_s        INTEGER NOT NULL,
  cost_native   TEXT NOT NULL,
  proceeds_native TEXT NOT NULL,
  pnl_native    TEXT NOT NULL,
  pnl_pct       REAL NOT NULL,
  fees_native   TEXT NOT NULL DEFAULT '0',
  slippage_bps  INTEGER,
  mae_pct       REAL,
  mfe_pct       REAL,
  exit_reason   TEXT,
  mistakes_json TEXT NOT NULL DEFAULT '[]',
  lesson        TEXT,
  params_version TEXT NOT NULL DEFAULT 'v1'
);
CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_ms DESC);
CREATE INDEX IF NOT EXISTS idx_trades_lane ON trades(lane, mode, closed_ms DESC);

-- Hash-chained learning journal. Entries are never updated or deleted.
CREATE TABLE IF NOT EXISTS journal (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms      INTEGER NOT NULL,
  kind       TEXT NOT NULL,                 -- observation | lesson | experiment | change | outcome | correction
  subject    TEXT,
  body       TEXT NOT NULL,
  refs_json  TEXT NOT NULL DEFAULT '[]',
  prev_hash  TEXT NOT NULL,
  entry_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_kind ON journal(kind, seq);

-- Playbook rules the reflection job appends to and curates (never rewrites wholesale).
CREATE TABLE IF NOT EXISTS playbook (
  rule_id      TEXT PRIMARY KEY,
  created_ms   INTEGER NOT NULL,
  updated_ms   INTEGER NOT NULL,
  lane         TEXT,
  text         TEXT NOT NULL,
  hits         INTEGER NOT NULL DEFAULT 0,
  misses       INTEGER NOT NULL DEFAULT 0,
  status       TEXT NOT NULL DEFAULT 'active',  -- active | retired | proposed
  evidence_json TEXT NOT NULL DEFAULT '[]',
  expires_ms   INTEGER
);

-- Parameter/strategy change proposals and their gate results.
CREATE TABLE IF NOT EXISTS experiments (
  experiment_id TEXT PRIMARY KEY,
  created_ms    INTEGER NOT NULL,
  lane          TEXT,
  hypothesis    TEXT NOT NULL,
  diff_json     TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'proposed', -- proposed | replay | shadow | canary | promoted | rejected
  replay_json   TEXT,
  shadow_json   TEXT,
  decided_ms    INTEGER,
  decided_by    TEXT,
  notes         TEXT
);

CREATE TABLE IF NOT EXISTS risk_state (
  day_key            TEXT PRIMARY KEY,
  realized_native_json TEXT NOT NULL DEFAULT '{}',   -- chain -> base units
  entries            INTEGER NOT NULL DEFAULT 0,
  halted             INTEGER NOT NULL DEFAULT 0,
  halt_reason        TEXT,
  updated_ms         INTEGER NOT NULL
);

-- Hunters: airdrops, NFT mints, listings.
CREATE TABLE IF NOT EXISTS opportunities (
  opportunity_id TEXT PRIMARY KEY,
  kind           TEXT NOT NULL,             -- airdrop | nft_mint | listing
  name           TEXT NOT NULL,
  chain          TEXT,
  url            TEXT,
  status         TEXT NOT NULL DEFAULT 'open',
  ev_score       REAL,
  cost_usd       TEXT,
  deadline_ms    INTEGER,
  evidence_json  TEXT NOT NULL DEFAULT '{}',
  plan_json      TEXT NOT NULL DEFAULT '{}',
  created_ms     INTEGER NOT NULL,
  updated_ms     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opps ON opportunities(kind, status, ev_score DESC);
