-- Clustering, entities, token dossiers, caller reputation.

CREATE TABLE IF NOT EXISTS cluster_edges (
  chain         TEXT NOT NULL,
  a             TEXT NOT NULL,              -- always the lexicographically smaller address
  b             TEXT NOT NULL,
  edge_type     TEXT NOT NULL,
  confidence    REAL NOT NULL,
  observations  INTEGER NOT NULL DEFAULT 1,
  first_seen_ms INTEGER NOT NULL,
  last_seen_ms  INTEGER NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (chain, a, b, edge_type)
);
CREATE INDEX IF NOT EXISTS idx_edges_a ON cluster_edges(chain, a);
CREATE INDEX IF NOT EXISTS idx_edges_b ON cluster_edges(chain, b);

CREATE TABLE IF NOT EXISTS entities (
  entity_id   TEXT PRIMARY KEY,
  chain       TEXT NOT NULL,
  label       TEXT,
  archetype   TEXT NOT NULL DEFAULT 'trader',
  confidence  REAL NOT NULL,
  size        INTEGER NOT NULL,
  edge_types_json TEXT NOT NULL DEFAULT '[]',
  created_ms  INTEGER NOT NULL,
  updated_ms  INTEGER NOT NULL,
  version     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS entity_members (
  entity_id TEXT NOT NULL,
  chain     TEXT NOT NULL,
  address   TEXT NOT NULL,
  PRIMARY KEY (chain, address),
  FOREIGN KEY (entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_entity_members ON entity_members(entity_id);

-- Addresses that must never be clustered: CEX hot wallets, routers, pools, bridges.
CREATE TABLE IF NOT EXISTS hub_addresses (
  chain    TEXT NOT NULL,
  address  TEXT NOT NULL,
  kind     TEXT NOT NULL,                   -- cex | router | pool | bridge | disperser | program
  label    TEXT,
  source   TEXT NOT NULL,
  PRIMARY KEY (chain, address)
);

CREATE TABLE IF NOT EXISTS token_dossiers (
  chain        TEXT NOT NULL,
  address      TEXT NOT NULL,
  built_at_ms  INTEGER NOT NULL,
  score        REAL,
  grade        TEXT NOT NULL DEFAULT 'UNSCORED',
  blockers_json TEXT NOT NULL DEFAULT '[]',
  warnings_json TEXT NOT NULL DEFAULT '[]',
  unknowns_json TEXT NOT NULL DEFAULT '[]',
  dossier_json TEXT NOT NULL,
  PRIMARY KEY (chain, address)
);
CREATE INDEX IF NOT EXISTS idx_dossier_grade ON token_dossiers(grade, built_at_ms DESC);

-- First buyers per token: the input to insider/sniper/bundle detection.
CREATE TABLE IF NOT EXISTS first_buyers (
  chain       TEXT NOT NULL,
  token       TEXT NOT NULL,
  wallet      TEXT NOT NULL,
  rank        INTEGER NOT NULL,
  slot        INTEGER,
  ts_ms       INTEGER NOT NULL,
  seconds_after_open REAL,
  amount_native TEXT,
  still_holding INTEGER,
  pnl_usd     TEXT,
  source      TEXT NOT NULL,
  PRIMARY KEY (chain, token, wallet)
);
CREATE INDEX IF NOT EXISTS idx_first_buyers_token ON first_buyers(chain, token, rank);
CREATE INDEX IF NOT EXISTS idx_first_buyers_wallet ON first_buyers(chain, wallet);

-- Telegram / X callers, scored by what happened after their calls.
CREATE TABLE IF NOT EXISTS callers (
  platform     TEXT NOT NULL,               -- telegram | x
  caller_id    TEXT NOT NULL,
  display_name TEXT,
  channel      TEXT,
  calls        INTEGER NOT NULL DEFAULT 0,
  wins         INTEGER NOT NULL DEFAULT 0,
  losses       INTEGER NOT NULL DEFAULT 0,
  expectancy   REAL,
  avg_peak_x   REAL,
  last_call_ms INTEGER,
  mode         TEXT NOT NULL DEFAULT 'observe',  -- observe | follow | fade | ignore
  PRIMARY KEY (platform, caller_id)
);

CREATE TABLE IF NOT EXISTS caller_calls (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  platform    TEXT NOT NULL,
  caller_id   TEXT NOT NULL,
  chain       TEXT NOT NULL,
  token       TEXT NOT NULL,
  ts_ms       INTEGER NOT NULL,
  channel     TEXT,
  price_at_call_usd TEXT,
  peak_x      REAL,
  outcome     TEXT,                          -- pending | win | loss | rug
  UNIQUE (platform, caller_id, chain, token, ts_ms)
);
CREATE INDEX IF NOT EXISTS idx_caller_calls ON caller_calls(chain, token, ts_ms);

-- Token creators, scored by what their prior launches did.
CREATE TABLE IF NOT EXISTS creators (
  chain         TEXT NOT NULL,
  address       TEXT NOT NULL,
  launches      INTEGER NOT NULL DEFAULT 0,
  graduated     INTEGER NOT NULL DEFAULT 0,
  rugged        INTEGER NOT NULL DEFAULT 0,
  median_peak_mcap_usd TEXT,
  score         REAL,
  updated_ms    INTEGER NOT NULL,
  PRIMARY KEY (chain, address)
);
