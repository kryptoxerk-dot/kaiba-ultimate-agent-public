-- 017: the evidence behind a live entry price.
--
-- `positions.entry_price_usd` is the number the exit watchdog compares against a live quote
-- and the number the hard stop is cut from. Until this migration it was the pre-trade
-- dossier quote: a price from *before* the trade, excluding the slippage actually paid,
-- which on a curve snipe is routinely 10%+. A too-low entry is a too-low stop.
--
-- Three tables, and why they are tables rather than columns on `orders` / `positions`:
--
--   1. `native_prices` is a TIME SERIES keyed by (chain, ts_ms), never a "latest" cell. A
--      fill is priced in USD with the sample nearest to *its own* moment, and the distance
--      to that sample is reported. Stamping today's SOL price on last week's fill would be
--      a number nobody can stand behind, so it is refused rather than approximated.
--   2. `fill_prices` records, per order, what the fill itself established: the exact
--      native/token ratio from the two legs, which decimals it was read with and where they
--      came from, which native sample converted it and how far away that sample was. This
--      is provenance next to the number, so a stop can be audited back to the fill.
--   3. `fill_reconciliations` records what the CHAIN says the fill was, side by side with
--      what the venue claimed, plus the delta. Neither row overwrites the other: a venue
--      reporting one fill and settling another is exactly the discrepancy that must stay
--      visible.
--
-- Money is TEXT base-unit integers and USD is TEXT Decimal, per docs/CONTRACT.md §1.

CREATE TABLE IF NOT EXISTS native_prices (
  chain         TEXT NOT NULL,
  ts_ms         INTEGER NOT NULL,           -- when the provider answered, not when we read it
  price_usd     TEXT NOT NULL,              -- Decimal as text; never REAL
  source        TEXT NOT NULL,              -- provider that reported it
  pair          TEXT,                       -- pool it was read from, for audit
  liquidity_usd TEXT,                       -- depth of that pool, Decimal as text
  receipt_json  TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (chain, ts_ms)
);
CREATE INDEX IF NOT EXISTS idx_native_prices_chain_ts ON native_prices(chain, ts_ms DESC);

CREATE TABLE IF NOT EXISTS fill_prices (
  order_id                  TEXT PRIMARY KEY,
  chain                     TEXT NOT NULL,
  token                     TEXT NOT NULL,
  side                      TEXT NOT NULL,
  fill_ts_ms                INTEGER NOT NULL, -- the instant the USD leg was looked up at
  fill_ts_basis             TEXT NOT NULL,    -- block_time | order_updated | caller
  native_atoms              TEXT NOT NULL,    -- native base units on the native leg
  token_atoms               TEXT NOT NULL,    -- token base units on the token leg
  fee_native                TEXT,             -- venue/tx fee in native base units, when reported
  token_decimals            INTEGER,          -- NULL when unknown; the per-token price is then NULL
  decimals_basis            TEXT NOT NULL,    -- verified_onchain | tokens_row | caller | unavailable
  price_native_per_token    TEXT,             -- Decimal: native units per whole token, fee excluded
  price_native_all_in       TEXT,             -- Decimal: the same with fee_native included
  price_usd                 TEXT,             -- Decimal; NULL unless a contemporaneous sample existed
  native_usd                TEXT,             -- the sample used, Decimal
  native_sample_ts_ms       INTEGER,
  native_sample_distance_ms INTEGER,
  basis                     TEXT NOT NULL,    -- fill_ratio | fill_ratio_native_only | unavailable
  computed_ms               INTEGER NOT NULL,
  notes_json                TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_fill_prices_token ON fill_prices(chain, token, fill_ts_ms);

CREATE TABLE IF NOT EXISTS fill_reconciliations (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id               TEXT,               -- NULL when reconciling a bare signature
  chain                  TEXT NOT NULL,
  signature              TEXT NOT NULL,
  wallet                 TEXT NOT NULL,
  token                  TEXT NOT NULL,
  side                   TEXT,
  slot                   INTEGER,
  block_time_ms          INTEGER,
  claimed_native         TEXT,               -- what the order / venue row says moved
  claimed_tokens         TEXT,
  onchain_native         TEXT,               -- the venue leg: native that entered or left the pool
  onchain_native_all_in  TEXT,               -- the wallet's own native delta, fees and rent included
  onchain_tx_fee         TEXT,
  onchain_tokens         TEXT,
  delta_native           TEXT,               -- onchain - claimed
  delta_tokens           TEXT,
  delta_native_bps       INTEGER,
  verdict                TEXT NOT NULL,      -- agree | disagree | unavailable
  detail                 TEXT NOT NULL DEFAULT '',
  receipt_json           TEXT NOT NULL DEFAULT '{}',
  checked_ms             INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fill_recon_order ON fill_reconciliations(order_id, checked_ms DESC);
CREATE INDEX IF NOT EXISTS idx_fill_recon_sig ON fill_reconciliations(chain, signature);
