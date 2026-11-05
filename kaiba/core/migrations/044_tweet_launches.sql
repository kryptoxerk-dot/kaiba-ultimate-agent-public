-- Tweet launcher (kaiba/execution/tweet_launch.py, kaiba/ingest/x_stream.py).
--
--   tweet_launch_tweets  every post the X stream delivered from a watched account, once.
--   tweet_launches       one row per (tweet, chain): what we would launch (or did), why, and
--                        what the market launched on the same tweet. In SHADOW nothing is sent;
--                        the row is the measurement. `competitors_json` is filled by the
--                        observer at fixed horizons after the post: other tokens in `tokens`
--                        whose symbol or name matches the planned ticker.
--
-- Money / atom columns are TEXT holding integers (lamports / wei). CAST before comparing.
CREATE TABLE IF NOT EXISTS tweet_launch_tweets (
    tweet_id         TEXT PRIMARY KEY,           -- X snowflake, kept as text
    author           TEXT NOT NULL,              -- screen name, lower case, no @
    kind             TEXT,                       -- post | reply | repost | quote | thread ...
    text             TEXT,
    media_json       TEXT NOT NULL DEFAULT '[]',
    created_ms       INTEGER,                    -- when the post was made (from the snowflake)
    received_ms      INTEGER NOT NULL,           -- when this process got it
    feed_delay_ms    INTEGER,                    -- provider's own post -> server latency
    backend          TEXT NOT NULL,              -- twitterapi_io_stream | twitterapi_io_rule | ...
    raw_json         TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tlt_author ON tweet_launch_tweets (author, created_ms);

CREATE TABLE IF NOT EXISTS tweet_launches (
    launch_id        TEXT PRIMARY KEY,           -- tl:<tweet_id>:<chain>
    tweet_id         TEXT NOT NULL,
    author           TEXT NOT NULL,
    chain            TEXT NOT NULL,
    dex              TEXT NOT NULL,              -- gmgn --dex value (pump, fourmeme, flap, pons, ...)
    mode             TEXT NOT NULL,              -- shadow | live
    verdict          TEXT NOT NULL,              -- launch | skip
    reasons_json     TEXT NOT NULL DEFAULT '[]', -- why (both verdicts)
    score            REAL,
    name             TEXT,
    symbol           TEXT,
    image_source     TEXT,                       -- tweet_media | none
    image_url        TEXT,
    buy_amt_native   TEXT,                       -- human units (e.g. "1.485") as passed to --buy-amt
    supply_pct       REAL,                       -- what that buy is modelled to take; NULL = no curve model
    buy_basis        TEXT,                       -- how buy_amt was derived
    argv_json        TEXT,                       -- the exact gmgn-cli argv (no secrets in it)
    decided_ms       INTEGER NOT NULL,
    decide_latency_ms INTEGER,                   -- decided_ms - tweet created_ms
    -- live only
    state            TEXT,                       -- planned | submitted | confirmed | failed | ambiguous
    provider_order_id TEXT,
    token            TEXT,                       -- created token (report.output_token)
    order_id         TEXT,                       -- orders.order_id handed to reconcile
    error            TEXT,
    -- the market on the same tweet (filled by the observer)
    competitors_json TEXT NOT NULL DEFAULT '{}', -- {"<horizon_s>": {"n": .., "first": {...}, "tokens": [...]}}
    observed_ms      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tl_decided ON tweet_launches (decided_ms);
CREATE INDEX IF NOT EXISTS idx_tl_tweet ON tweet_launches (tweet_id);
