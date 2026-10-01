-- Lane / engine bookkeeping that 003_execution.sql does not cover.
--
-- Three gaps, and why each one is its own table rather than a column on an existing one:
--
-- 1. `decision_outcomes` links a decision (including the ones that never became a trade)
--    to whatever happened next. The learning loop reads decisions and needs the join
--    without re-deriving it from timestamps.
-- 2. `position_orders` links orders to positions. `orders` is keyed by decision, but an
--    exit has no decision of its own, so without this table a position's fills cannot be
--    summed. It is a link table rather than `ALTER TABLE orders ADD COLUMN position_id`
--    so that a sibling migration adding the same column cannot collide with this one.
-- 3. `position_marks` is the paper broker's mark-to-market trail: the evidence behind the
--    MAE/MFE numbers on `positions`, which a promotion decision should be able to audit.

CREATE TABLE IF NOT EXISTS decision_outcomes (
  decision_id  TEXT PRIMARY KEY,
  position_id  TEXT,
  trade_id     TEXT,
  order_id     TEXT,
  outcome      TEXT,                 -- planned | open | closed | rejected | abandoned
  pnl_native   TEXT,
  pnl_pct      REAL,
  note         TEXT,
  linked_ms    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_outcomes_pos ON decision_outcomes(position_id);
CREATE INDEX IF NOT EXISTS idx_decision_outcomes_out ON decision_outcomes(outcome, linked_ms DESC);

CREATE TABLE IF NOT EXISTS position_orders (
  position_id TEXT NOT NULL,
  order_id    TEXT NOT NULL,
  side        TEXT NOT NULL,
  ts_ms       INTEGER NOT NULL,
  PRIMARY KEY (position_id, order_id)
);
CREATE INDEX IF NOT EXISTS idx_position_orders_order ON position_orders(order_id);

CREATE TABLE IF NOT EXISTS position_marks (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id  TEXT NOT NULL,
  ts_ms        INTEGER NOT NULL,
  price_usd    TEXT NOT NULL,
  return_pct   REAL,
  mae_pct      REAL,
  mfe_pct      REAL
);
CREATE INDEX IF NOT EXISTS idx_position_marks ON position_marks(position_id, ts_ms);
