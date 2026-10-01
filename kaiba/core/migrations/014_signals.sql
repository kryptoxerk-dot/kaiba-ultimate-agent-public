-- Early-alpha detection layer: one normalised table for every pre-event signal.
--
-- These sources have nothing in common except that each one leaks a public fact days
-- before the thing it is a fact about. A certificate for `claim.<project>` is issued
-- before the claim page is announced; a Hyperliquid pre-launch market exists before the
-- token does; an exchange publishes a listing before the listing opens. Normalising them
-- into one table means the trading book asks one question ("what did we learn early, and
-- how early?") instead of five.
--
--   alpha_signals        one row per deduped signal. `first_seen_ms` is OUR clock,
--                        `event_at_ms` is the SOURCE's clock. The gap between them is our
--                        detection lag and is derivable in SQL, so it is not a column.
--                        `lead_ms` is NULL whenever the lead is not knowable - never 0,
--                        because a zero lead reads as "we were exactly on time".
--   alpha_source_health  per-source last-success, so a detector that has been failing for
--                        six hours is visible. A quiet detector and a quiet market look
--                        identical from the outside; this table is what tells them apart.
--   alpha_baseline       the asset list a diff-based source (Hyperliquid, Aevo) is
--                        compared against. Without it the first poll reports every asset
--                        that has ever existed as brand new.

CREATE TABLE IF NOT EXISTS alpha_signals (
  signal_key    TEXT PRIMARY KEY,          -- digest(source, kind, key_seed); fires once
  source        TEXT NOT NULL,             -- crtsh | binance | okx | upbit | snapshot | ...
  kind          TEXT NOT NULL,             -- cert_subdomain | venue_listing | prelaunch_market | ...
  subject       TEXT NOT NULL,             -- claim.monad.xyz | ONDO | enso.eth | org/repo
  title         TEXT,
  url           TEXT,
  chain         TEXT,
  event_at_ms   INTEGER,                   -- source's own timestamp; NULL when it publishes none
  first_seen_ms INTEGER NOT NULL,          -- when this process first saw it
  lead_ms       INTEGER,                   -- estimated warning ahead of the public event
  lead_basis    TEXT,                      -- how lead_ms was derived, or why it is NULL
  confidence    REAL,                      -- prior precision of this source/kind, 0..1
  payload_json  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_alpha_signals_seen   ON alpha_signals(first_seen_ms DESC);
CREATE INDEX IF NOT EXISTS idx_alpha_signals_source ON alpha_signals(source, first_seen_ms DESC);
CREATE INDEX IF NOT EXISTS idx_alpha_signals_kind   ON alpha_signals(kind, first_seen_ms DESC);
CREATE INDEX IF NOT EXISTS idx_alpha_signals_subject ON alpha_signals(subject);

CREATE TABLE IF NOT EXISTS alpha_source_health (
  source        TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,
  interval_s    INTEGER NOT NULL DEFAULT 0,
  last_poll_ms  INTEGER,                   -- we tried
  last_ok_ms    INTEGER,                   -- we got usable data; the number that matters
  last_count    INTEGER NOT NULL DEFAULT 0,-- items the source returned
  last_new      INTEGER NOT NULL DEFAULT 0,-- of those, how many were new to us
  total_new     INTEGER NOT NULL DEFAULT 0,
  fail_streak   INTEGER NOT NULL DEFAULT 0,
  baseline_ms   INTEGER,                   -- diff sources only: when the baseline was set
  last_error    TEXT
);

CREATE TABLE IF NOT EXISTS alpha_baseline (
  source        TEXT NOT NULL,
  item          TEXT NOT NULL,
  first_seen_ms INTEGER NOT NULL,
  PRIMARY KEY (source, item)
);
CREATE INDEX IF NOT EXISTS idx_alpha_baseline_source ON alpha_baseline(source);
