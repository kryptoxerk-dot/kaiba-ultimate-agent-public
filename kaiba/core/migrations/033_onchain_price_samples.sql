-- LOG-ONLY on-chain pool prices for live Robinhood positions (kaiba.execution.onchain_pool).
--
-- One row per position per protection tick while `protection.onchain_price_log` is on: the
-- pool's own price (one Multicall3 eth_call for the whole book) beside the quote the
-- watchdog actually decided on at that tick. Nothing reads this table to make a decision;
-- kaiba.learning.onchain_vs_feed reads it to measure whether the pool would have fired the
-- stop earlier and better than the DexScreener/GMGN mark (audit-20261001-strategy §1c, #3).
--
-- Prices are Decimal text, like every other money column in this database. Retention is
-- the writer's own: rows older than `protection.onchain_price_log_retention_days` (14) are
-- deleted hourly in bounded chunks through idx_onchain_price_samples_ts.
CREATE TABLE IF NOT EXISTS onchain_price_samples (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms                 INTEGER NOT NULL,   -- when the eth_call answered (local clock)
    position_id           TEXT    NOT NULL,
    token                 TEXT    NOT NULL,
    pool                  TEXT    NOT NULL,   -- v2/v3 pool address or v4 poolId
    pool_kind             TEXT    NOT NULL,   -- v2 | v3 | v4
    quote_token           TEXT,               -- NULL = native ETH
    price_quote           TEXT    NOT NULL,   -- token price in whole quote units
    quote_usd             TEXT,               -- USD per whole quote unit; NULL if unknown
    price_usd             TEXT,               -- NULL when quote_usd is unknown
    source                TEXT    NOT NULL,   -- e.g. onchain:v4|eth_usd:native_prices
    block_number          INTEGER,            -- L2 block (ArbSys.arbBlockNumber)
    block_ts_ms           INTEGER,
    rpc_ms                INTEGER,
    stop_price_usd        TEXT,               -- the watchdog's stop at this tick
    incumbent_price_usd   TEXT,               -- the quote protection decided on
    incumbent_source      TEXT,
    incumbent_observed_ms INTEGER,
    incumbent_age_ms      INTEGER,
    note                  TEXT
);
CREATE INDEX IF NOT EXISTS idx_onchain_price_samples_pos ON onchain_price_samples(position_id, ts_ms);
CREATE INDEX IF NOT EXISTS idx_onchain_price_samples_ts ON onchain_price_samples(ts_ms);
