-- GMGN cohort labels, rolled up out of `wallet.trade` events (kaiba/intelligence/feed_tags.py).
--
-- The labels GMGN attaches to a feed trade (`smart_degen`, `kol`, `wash_trader` ...) were
-- stored ONLY in the `wallet.trade` event payload: `swaps` has no tags column. So no event
-- could ever be deleted without losing them, and every reader that wanted them scanned the
-- events table -- naming.py's LIKE over every wallet.trade event is why `wallet_naming`
-- has timed out on every run since 2026-09-24. MEASURED 2026-10-02 on the live box:
-- 336,028 wallet.trade events in 24 h at ~680 B of payload each; 15% of them are GMGN feed
-- rows, the rest robinhood/tracker trades that carry no label at all.
--
-- One row per (chain, address, tag, source). tag '' is the MEMBERSHIP row: "this wallet
-- appeared on this source", with n = feed events, which the readers need for feed
-- membership and per-feed counts. first_ms / last_ms are event (bus) times, so last_ms
-- only moves forward and doubles as a change cursor (idx_wallet_feed_tags_last).
-- wallet_name: the first non-empty `wallet_name` a feed row carried (discover.py reads it).
--
-- Additive and idempotent: nothing reads this table until the backfill has completed and
-- an exact parity check has passed (feed_tags.table_ready).
CREATE TABLE IF NOT EXISTS wallet_feed_tags (
  chain        TEXT    NOT NULL,
  address      TEXT    NOT NULL,
  tag          TEXT    NOT NULL,
  source       TEXT    NOT NULL,
  first_ms     INTEGER NOT NULL,
  last_ms      INTEGER NOT NULL,
  n            INTEGER NOT NULL,
  wallet_name  TEXT,
  PRIMARY KEY (chain, address, tag, source)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_wallet_feed_tags_last ON wallet_feed_tags(last_ms);
