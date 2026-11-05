-- Every (chain, wallet) seen on the swaps tape, folded incrementally by primary-key range
-- (kaiba/intelligence/seeds.refresh_wallet_index, cursor kv 'seeds:wallet_cursor'). Replaces a
-- per-run SELECT DISTINCT over the whole tape that made wallet_seeds time out (2026-10-06).
CREATE TABLE IF NOT EXISTS seed_wallets (
    chain   TEXT NOT NULL,
    wallet  TEXT NOT NULL,
    PRIMARY KEY (chain, wallet)
) WITHOUT ROWID;
