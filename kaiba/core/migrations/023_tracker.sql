-- Live wallet tracking: a watchlist that can be audited, and observations that are not signals.
--
-- Why these tables exist, and why they are shaped the way they are.
--
--   1. `wallets.cohort` is a single nullable TEXT column. It can say a wallet is
--      "tracked"; it cannot say who decided that, when, on what evidence, or what screen
--      the wallet passed on the way in. A membership nobody can explain is the
--      leaderboard problem rebuilt in-house (docs/research/13 A2: "the source of our
--      wallet universe is an advertisement"). So membership here carries its reason, its
--      source, its author and its admission evidence, and every change is appended to a
--      log that is never updated in place.
--
--   2. There is deliberately **no `trusted` tier**. `tracker_watchlist.tier` is
--      CHECK-constrained to `observe` and `candidate`. A wallet cannot be promoted to
--      trusted by anything in this subsystem, by an operator command, or by a later
--      migration that forgets why — the database refuses the row. Only measured forward
--      performance against a matched control (`kaiba/learning/validation.py`) could
--      justify trust, and nothing here measures that.
--
--   3. A detection is an **observation, not a signal**. `tracker_detections` records that
--      a watched address traded, with the two timestamps needed to say how late we were.
--      It does not carry a score, a confidence or a recommendation, and no lane reads it
--      as a trigger. The research is blunt that copying is negative for the copier at
--      published latencies (13 A3: four of five selectors produce positive leader returns
--      and negative copier returns at *zero* latency), so an instrument that quietly
--      became a trigger would contradict the evidence in the same repository.
--
--   4. Latency is stored **per detection**, not rolled up like `ingest_latency`. The
--      question is whether `trusted-copy`'s configured `max_copy_delay_s: 20` is
--      reachable, and that is a question about the tail, not the median. Detections are
--      rare enough (a watchlist is tens of wallets, not a firehose) that per-sample rows
--      are affordable where they would not be for launches.
--
--   5. `tracker_polls` is the honesty ledger. Every poll records what it cost and what it
--      caught, so "we detected N buys" can always be divided by "we spent M credits and
--      made P requests" without asking a provider. A tracker whose recall is never
--      measured against its own effort is indistinguishable from one that is not working.

-- One row per address ever considered. Membership is a status, not a deletion: a wallet
-- that leaves keeps its history so the decision can be re-read later.
CREATE TABLE IF NOT EXISTS tracker_watchlist (
  chain            TEXT    NOT NULL,
  address          TEXT    NOT NULL,

  -- observe   : we record what it does. Nothing downstream may size on it.
  -- candidate : same, plus it is in the forward-tracking cohort being measured.
  -- There is no third value. See note 2 above before adding one.
  tier             TEXT    NOT NULL CHECK (tier IN ('observe', 'candidate')),
  status           TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'removed')),

  -- Auditability: all four are required and none has a usable default.
  reason           TEXT    NOT NULL,          -- the sentence a human would want to read
  source           TEXT    NOT NULL,          -- grade:<model> | discovery:<run_id> | operator | import:<file>
  added_by         TEXT    NOT NULL,          -- the actor, not the module
  added_ms         INTEGER NOT NULL,

  removed_ms       INTEGER,
  removed_reason   TEXT,

  -- What the wallet looked like at admission, frozen. A later regrade does not rewrite
  -- the reason it was let in.
  grade_at_add     TEXT,
  score_at_add     REAL,

  -- The admission screen's findings: buy share, transaction failure rate, sample sizes,
  -- and an EvidenceBasis per field. UNAVAILABLE is a real value here and it blocks
  -- admission — unknown is not average.
  screen_json      TEXT    NOT NULL DEFAULT '{}',

  last_checked_ms  INTEGER,
  -- Highest signature this address has been polled up to, so a poll only asks for what
  -- it has not seen. NULL means the next poll establishes a baseline and emits nothing.
  cursor_sig       TEXT,
  cursor_ms        INTEGER,

  meta_json        TEXT    NOT NULL DEFAULT '{}',
  PRIMARY KEY (chain, address)
);
CREATE INDEX IF NOT EXISTS idx_tracker_watchlist_active
  ON tracker_watchlist(chain, status, tier);
CREATE INDEX IF NOT EXISTS idx_tracker_watchlist_checked
  ON tracker_watchlist(status, last_checked_ms);

-- Append-only. Never UPDATE, never DELETE. This is the answer to "who put this wallet
-- here and why", including for wallets that were refused.
CREATE TABLE IF NOT EXISTS tracker_watchlist_log (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  chain        TEXT    NOT NULL,
  address      TEXT    NOT NULL,
  ts_ms        INTEGER NOT NULL,
  action       TEXT    NOT NULL,   -- admitted | refused | removed | retiered | rescreened
  actor        TEXT    NOT NULL,
  detail       TEXT    NOT NULL DEFAULT '',
  payload_json TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tracker_wl_log ON tracker_watchlist_log(chain, address, ts_ms);
CREATE INDEX IF NOT EXISTS idx_tracker_wl_log_action ON tracker_watchlist_log(action, ts_ms DESC);

-- One row per observed trade by a watched address.
--
-- `block_ms` is when the chain says it happened; `detected_ms` is when this process first
-- held it. `lag_ms` is the difference and is the only number that answers whether
-- `trusted-copy` is viable. It is NULL, never 0, when the route did not supply a block
-- time — a missing latency is not a fast one.
CREATE TABLE IF NOT EXISTS tracker_detections (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  chain         TEXT    NOT NULL,
  wallet        TEXT    NOT NULL,
  token         TEXT    NOT NULL,
  side          TEXT    NOT NULL CHECK (side IN ('buy', 'sell')),
  tx            TEXT    NOT NULL,
  slot          INTEGER,
  block_ms      INTEGER,
  detected_ms   INTEGER NOT NULL,
  lag_ms        INTEGER,
  usd_value     TEXT,                      -- Decimal as text; money never touches a float
  amount_native TEXT,                      -- lamports as text
  route         TEXT    NOT NULL,          -- pumpfun:trades | helius:signatures
  entity_id     TEXT,                      -- resolved at detection time, NULL when unclustered
  UNIQUE (chain, tx, wallet, token, side)
);
CREATE INDEX IF NOT EXISTS idx_tracker_det_token ON tracker_detections(chain, token, block_ms);
CREATE INDEX IF NOT EXISTS idx_tracker_det_wallet ON tracker_detections(chain, wallet, detected_ms DESC);
CREATE INDEX IF NOT EXISTS idx_tracker_det_lag ON tracker_detections(route, lag_ms);

-- Every window the confluence scan evaluated, including the ones that did not qualify.
--
-- Recording the misses is the point. "confluence-5 never fired" is only informative if we
-- know how close it came and over how much tape, and the negative result is the finding
-- we expect (8 wallets at C or better out of 425 graded).
CREATE TABLE IF NOT EXISTS tracker_windows (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  chain            TEXT    NOT NULL,
  token            TEXT    NOT NULL,
  window_start_ms  INTEGER NOT NULL,
  window_end_ms    INTEGER NOT NULL,
  buyers           INTEGER NOT NULL,       -- distinct addresses
  entities         INTEGER NOT NULL,       -- distinct operators; this is the one that counts
  watched_buyers   INTEGER NOT NULL DEFAULT 0,
  min_usd          TEXT,                   -- the floor applied, as text
  qualifying       INTEGER NOT NULL DEFAULT 0,
  scanned_ms       INTEGER NOT NULL,
  detail_json      TEXT    NOT NULL DEFAULT '{}',
  UNIQUE (chain, token, window_start_ms)
);
CREATE INDEX IF NOT EXISTS idx_tracker_windows_qual
  ON tracker_windows(chain, qualifying, window_end_ms DESC);
CREATE INDEX IF NOT EXISTS idx_tracker_windows_entities
  ON tracker_windows(chain, entities DESC);

-- What each poll cost and what it caught. `credits` is 0 for a free route, which is a
-- measured 0 and not a missing value; `ok` distinguishes a poll that found nothing from
-- a poll that failed.
CREATE TABLE IF NOT EXISTS tracker_polls (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  chain        TEXT    NOT NULL,
  route        TEXT    NOT NULL,
  subject      TEXT    NOT NULL,           -- the wallet or mint polled
  started_ms   INTEGER NOT NULL,
  finished_ms  INTEGER NOT NULL,
  rtt_ms       INTEGER NOT NULL,
  credits      INTEGER NOT NULL DEFAULT 0,
  ok           INTEGER NOT NULL DEFAULT 0,
  rows_seen    INTEGER NOT NULL DEFAULT 0,
  detections   INTEGER NOT NULL DEFAULT 0,
  note         TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tracker_polls_route ON tracker_polls(route, started_ms DESC);
