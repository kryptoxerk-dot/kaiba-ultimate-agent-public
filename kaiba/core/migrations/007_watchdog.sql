-- Durable state for the exit watchdog (kaiba/execution/watchdog.py).
--
-- The `positions` table already carries peak/stop/tp_done, but three things the watchdog
-- needs have nowhere to live there and all three are safety-critical across a restart:
--
--   1. the in-flight exit marker. An ambiguous send (OrderState.UNKNOWN) must survive a
--      crash, or the process comes back up and sells the same position a second time;
--   2. the previous liquidity reading, which is the only input to the rug monitor that
--      cannot be re-derived from a single fresh quote;
--   3. a deferred exit request. When protection is blind we refuse to price an exit, so
--      the request waits here instead of being dropped on the floor.
--
-- One row per position. Rows are kept after the position closes so a post-mortem can see
-- what the watchdog knew; the loop only ever reads rows for positions that are open.

CREATE TABLE IF NOT EXISTS watchdog_state (
  position_id         TEXT PRIMARY KEY,
  -- mirror of ProtectionState, as TEXT because USD is Decimal and never a float
  entry_price_usd     TEXT,
  peak_price_usd      TEXT,
  stop_price_usd      TEXT,
  tp_done_json        TEXT NOT NULL DEFAULT '[]',
  activated_trail_bps INTEGER,
  -- rug monitor: last liquidity reading we actually observed (NULL = never seen)
  prev_liquidity_usd  TEXT,
  -- per-position overrides requested via kaiba_set_protection
  stop_loss_bps       INTEGER,
  trail_bps           INTEGER,
  -- exit in flight. exit_final is the hard latch for a completed 100% exit.
  exit_order_id       TEXT,
  exit_state          TEXT,
  exit_pct            TEXT,
  exit_reason         TEXT,
  exit_attempts       INTEGER NOT NULL DEFAULT 0,
  exit_retry_after_ms INTEGER,
  exit_final          INTEGER NOT NULL DEFAULT 0,
  -- an exit somebody asked for that we have not been able to price yet
  pending_pct         TEXT,
  pending_reason      TEXT,
  pending_source      TEXT,
  pending_ms          INTEGER,
  -- blindness bookkeeping, so the warning is throttled but the duration is not lost
  blind_since_ms      INTEGER,
  last_blind_warn_ms  INTEGER,
  updated_ms          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_watchdog_state_updated ON watchdog_state(updated_ms DESC);
CREATE INDEX IF NOT EXISTS idx_watchdog_state_inflight
  ON watchdog_state(exit_state) WHERE exit_state IS NOT NULL;
