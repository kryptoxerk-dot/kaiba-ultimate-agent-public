-- Venue-side standing orders: the protection that outlives our process.
--
-- Layer 2 (kaiba/execution/protection.py) is pure and correct, but it lives in memory and
-- in `positions`. If this process dies while a position is open, the ladder stops being
-- evaluated and the position has no stop at all. `kaiba/execution/standing.py` mirrors the
-- price-triggered part of that ladder onto GMGN as strategy orders; this is where the
-- mirror is recorded.
--
-- Why a table rather than `positions.protection_ids_json`:
--
--   1. **Ownership.** Exactly one party may own a given rung's quantity at a time. A JSON
--      blob of ids cannot express "the venue holds tp1, we hold tp2", and without that the
--      in-process watchdog and the venue both sell the same tokens. `owner` is that record
--      and the partial unique index below makes two live claims on one rung impossible.
--   2. **The ambiguous placement.** A create whose result we could not read is UNKNOWN,
--      never "not placed". It needs a durable row written *before* the venue call, exactly
--      like `orders` does for a swap, so a crash mid-placement leaves evidence instead of
--      an invitation to place it again.
--   3. **Cheap reconciliation.** `intent_digest` and `trigger_price_usd` are what let a
--      reconcile pass decide that a live venue order still matches the ladder and needs no
--      write. A GMGN write costs weight 10 against a Free capacity of 10 refilling at
--      0.2/s, so "leave it alone" has to be a decision the schema can support.
--
-- Prices are TEXT because USD is Decimal and a float must never touch money.

CREATE TABLE IF NOT EXISTS standing_orders (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id       TEXT NOT NULL,
  chain             TEXT NOT NULL,
  token             TEXT NOT NULL,
  -- which rung of the ladder this mirrors: tp1 / tp2 / tp3 / stop. One row per rung.
  tag               TEXT NOT NULL,
  -- what protection.to_gmgn_condition_orders rendered (profit_stop / loss_stop / ...)
  order_type        TEXT NOT NULL,
  -- what we actually sent: gmgn-cli `order strategy create --sub-order-type`
  sub_order_type    TEXT NOT NULL,
  -- absolute trigger, in USD (our unit) and in the unit the venue was given
  trigger_price_usd TEXT,
  trigger_price     TEXT,
  price_unit        TEXT,
  sell_ratio        TEXT,
  -- digest of the rendered intent; a live row whose digest still matches needs no write
  intent_digest     TEXT NOT NULL,
  -- placing | live | unknown | cancelled | failed | superseded | gone
  state             TEXT NOT NULL,
  -- venue | watchdog | contested. `contested` means a cancel we could not confirm: both
  -- sides may believe they own the quantity, and that is an operator-visible incident.
  owner             TEXT NOT NULL,
  provider          TEXT NOT NULL DEFAULT 'gmgn',
  provider_order_id TEXT,
  placed_ms         INTEGER,
  last_seen_ms      INTEGER,
  settled_ms        INTEGER,
  attempts          INTEGER NOT NULL DEFAULT 0,
  detail            TEXT,
  created_ms        INTEGER NOT NULL,
  updated_ms        INTEGER NOT NULL
);

-- The double-sell invariant, enforced by the database rather than by care: a position's
-- rung may have at most one row that is placing, live or unknown. A replace must settle
-- the old row first, which is the schema-level form of "cancel before replace".
CREATE UNIQUE INDEX IF NOT EXISTS ux_standing_live
  ON standing_orders(position_id, tag)
  WHERE state IN ('placing', 'live', 'unknown');

CREATE INDEX IF NOT EXISTS idx_standing_position ON standing_orders(position_id, updated_ms DESC);
CREATE INDEX IF NOT EXISTS idx_standing_open ON standing_orders(state, chain);
CREATE INDEX IF NOT EXISTS idx_standing_provider_order ON standing_orders(provider_order_id);

-- Per-position scheduling and the honest reason a position is not venue-protected.
-- `unprotected_reason` is what the dashboard and `kaiba probe` should read: it is NULL
-- only when a live venue stop actually exists, never merely because the feature is on.
CREATE TABLE IF NOT EXISTS standing_sync (
  position_id        TEXT PRIMARY KEY,
  last_sync_ms       INTEGER,
  last_write_ms      INTEGER,
  writes             INTEGER NOT NULL DEFAULT 0,
  unprotected_reason TEXT,
  detail             TEXT,
  updated_ms         INTEGER NOT NULL
);
