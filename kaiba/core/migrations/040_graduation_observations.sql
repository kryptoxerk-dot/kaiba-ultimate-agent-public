-- Graduation observer (kaiba/learning/graduation_observer.py). PAPER ONLY: records what the
-- new pool of every graduation would have paid a buyer ~30 s after graduation and what that
-- holding was worth at +5 / +15 / +60 min and +6 h, read on chain from the pool itself (never
-- from wallet feeds), so the three pre-declared graduation rules (GRADUATION_HYPOTHESES in that
-- module) get forward data that does not depend on which tokens our tracked wallets later trade.
-- Nothing reads this table to size, gate or place anything.
--
-- Money columns are TEXT holding integers in base units (lamports / wei / token atoms). CAST
-- before comparing: TEXT compares as strings.
CREATE TABLE IF NOT EXISTS graduation_observations (
    obs_id            TEXT PRIMARY KEY,           -- 'grd_' + digest(chain, token): one row per graduation
    chain             TEXT NOT NULL,
    token             TEXT NOT NULL,
    venue             TEXT NOT NULL,              -- pumpswap | pons_v4 | flap_pancake_v2
    source            TEXT NOT NULL,              -- event:token.migrated | tokens.migrated_ms
    event_id          INTEGER,                    -- events.id for sol / robinhood
    signature         TEXT,                       -- the graduation tx, when the source names it
    graduated_ms      INTEGER NOT NULL,           -- best-known graduation time (graduated_basis says which clock)
    graduated_basis   TEXT NOT NULL,              -- block_time | ingest_clock | provider_timestamp
    source_seen_ms    INTEGER,                    -- when our ingest wrote it (event ts); NULL for tokens rows
    detected_ms       INTEGER NOT NULL,           -- when THIS observer first saw it
    detect_latency_ms INTEGER,                    -- detected_ms - graduated_ms
    sampled           INTEGER NOT NULL DEFAULT 1, -- 0 = outside the deterministic sample; never read from chain
    quote_asset       TEXT,                       -- 'native' or the quote token's address
    quote_is_native   INTEGER,
    pool              TEXT,                       -- sol pool account | v4 pool id | Pancake V2 pair
    pool_meta_json    TEXT NOT NULL DEFAULT '{}', -- vaults / PoolKey, hook fee, tick range / token0, taxes
    curve_json        TEXT NOT NULL DEFAULT '{}', -- curve stats at graduation
    entry_due_ms      INTEGER NOT NULL,           -- graduated_ms + 30 s
    entry_ms          INTEGER,
    entry_offset_ms   INTEGER,                    -- entry_ms - graduated_ms
    entry_quote_in    TEXT,                       -- paper buy size, quote base units (fees inside)
    entry_tokens      TEXT,                       -- tokens that buy gets, after pool fee / hook fee / buy tax
    entry_shadow_quote TEXT,                      -- quote that reached the pool (put back in at every mark)
    entry_price       TEXT,                       -- spot quote-per-token-atom at entry (Decimal)
    entry_reserves_json TEXT NOT NULL DEFAULT '{}',
    entry_basis       TEXT,
    marks_json        TEXT NOT NULL DEFAULT '{}', -- horizon_s -> {at_ms, value, spot_value, reserves, note | missed}
    next_due_ms       INTEGER,                    -- the next moment this row needs a read
    attempts          INTEGER NOT NULL DEFAULT 0,
    rpc_calls         INTEGER NOT NULL DEFAULT 0, -- JSON-RPC calls spent on this row
    status            TEXT NOT NULL,              -- pending | resolved | open | done | late | unresolved | unsampled
    note              TEXT,
    created_ms        INTEGER NOT NULL,
    updated_ms        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_graduation_observations_chain ON graduation_observations(chain, detected_ms);
CREATE INDEX IF NOT EXISTS idx_graduation_observations_due ON graduation_observations(status, next_due_ms);
