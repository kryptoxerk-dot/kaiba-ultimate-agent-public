-- Tokens named in watched X posts (kaiba/execution/tweet_refs.py): the early-alpha
-- measurement and the vamp (clone-launch) plans. RECORD ONLY: nothing reads this table to
-- size, gate, enter or launch.
--
--   entry_price_usd   GMGN token-info price at OUR first look (seen_ms), not at the post:
--                     the price a buy-on-post rule could actually have got.
--   marks_json        {"<horizon_s>": {"price_usd", "liquidity_usd", "at_ms", "note"}}
--   vamp_json         {"<chain>": {"name", "symbol", "buy_amt_native", "argv"} | {"refused"}}
--
-- Money columns are TEXT holding Decimals. CAST before comparing.
CREATE TABLE IF NOT EXISTS tweet_refs (
    ref_id              TEXT PRIMARY KEY,          -- tr:<tweet_id>:<address|SYMBOL>
    tweet_id            TEXT NOT NULL,
    author              TEXT NOT NULL,
    kind                TEXT NOT NULL,             -- address | cashtag
    raw                 TEXT NOT NULL,
    chain               TEXT,
    token               TEXT,                      -- NULL = unresolved / ambiguous ticker
    resolve_note        TEXT,
    post_created_ms     INTEGER,
    seen_ms             INTEGER NOT NULL,
    entry_price_usd     TEXT,
    entry_liquidity_usd TEXT,
    meta_json           TEXT NOT NULL DEFAULT '{}',
    marks_json          TEXT NOT NULL DEFAULT '{}',
    marks_done          INTEGER NOT NULL DEFAULT 0,
    alpha_mode          TEXT NOT NULL DEFAULT 'shadow',
    vamp_json           TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tweet_refs_seen ON tweet_refs (seen_ms);
CREATE INDEX IF NOT EXISTS idx_tweet_refs_token ON tweet_refs (chain, token);
