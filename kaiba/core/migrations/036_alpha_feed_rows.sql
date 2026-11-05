-- SHADOW recorder for free, keyless alpha / narrative feeds (kaiba/ingest/alpha_feeds.py).
-- Nothing here gates, sizes or triggers a trade; nothing that decides reads these tables.
--
-- Question: do public feeds (Binance Web3 meme-rush / topic-rush / smart-money, GMGN
-- hot-searches) surface a token BEFORE we first see it, and does their narrative or
-- ranking carry forward-return information? Lead time is measured here (alpha_feed_first_seen
-- against tokens.first_seen_ms / snipe_observations.seen_ms); forward returns come from the
-- mooner study, joined on (chain, token_address).
--
-- Two tables:
--
--   alpha_feed_rows        one row per (feed, chain, token, dedupe window). A 1-minute poll
--                          writes a token at most once per window (default one hour), so
--                          the rank / narrative trajectory is kept at hourly grain without
--                          60 copies an hour. The only rewrite is a narrative arriving
--                          after the first sighting (Binance attaches its AI narrative a
--                          little after a token first lists): a NULL narrative is filled
--                          once, never overwritten.
--   alpha_feed_first_seen  the key output: when did THIS feed first surface THIS token.
--                          Written INSERT OR IGNORE, so the first sighting is permanent.
--                          It also keeps the first non-NULL narrative (and when it
--                          arrived), so the narrative survives alpha_feed_rows retention.
--                          Enrichment lookups (gmgn:created_tokens) are not sightings and
--                          are never written here. A token already on a list when the
--                          recorder starts is LEFT-CENSORED (first_seen_ms = start, not the
--                          feed's real first surfacing); lead_time_report excludes those.
--
-- `feed` names the list, not just the vendor: `binance:meme_rush:finalizing`,
-- `binance:topic_rush:latest`, `binance:smart_money`, `gmgn:hot_searches:5m`,
-- `gmgn:created_tokens`. EVM addresses are lowercase; Solana keeps its case.
-- `raw_json` is the vendor row with icons / translations / audit blobs dropped and a hard
-- size cap (truncation is marked `_truncated` inside the JSON, never silent).
-- Retention: alpha_feed_rows is pruned by alpha_feeds.prune_rows in bounded chunks through
-- idx_alpha_feed_rows_observed (default 14 days; MEASURED 2026-10-04 dry run: ~1.4k rows/h
-- at ~1.8 KB, ~65 MB/day with the default feeds); alpha_feed_first_seen is kept.
CREATE TABLE IF NOT EXISTS alpha_feed_rows (
    row_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    feed           TEXT    NOT NULL,
    chain          TEXT    NOT NULL,
    token_address  TEXT    NOT NULL,
    symbol         TEXT,
    name           TEXT,
    narrative      TEXT,                        -- narrative / topic text; NULL when the feed has none
    rank           INTEGER,                     -- 1-based position in the vendor's list at observation
    context        TEXT,                        -- topic id / signal id / dev wallet, per feed
    source_ts_ms   INTEGER,                     -- the vendor's own event time (launch, signal, topic)
    window_ms      INTEGER NOT NULL,            -- start of the dedupe window this row belongs to
    observed_ms    INTEGER NOT NULL,            -- when WE read it
    raw_json       TEXT    NOT NULL,
    UNIQUE (feed, chain, token_address, window_ms)
);
CREATE INDEX IF NOT EXISTS idx_alpha_feed_rows_observed ON alpha_feed_rows(observed_ms);
CREATE INDEX IF NOT EXISTS idx_alpha_feed_rows_token ON alpha_feed_rows(chain, token_address);

CREATE TABLE IF NOT EXISTS alpha_feed_first_seen (
    feed               TEXT    NOT NULL,
    chain              TEXT    NOT NULL,
    token_address      TEXT    NOT NULL,
    first_seen_ms      INTEGER NOT NULL,        -- our observation time of the feed's first surfacing
    first_rank         INTEGER,
    first_source_ts_ms INTEGER,                 -- the vendor's own event time at that sighting
    symbol             TEXT,
    narrative          TEXT,                    -- first non-NULL narrative this feed gave the token
    narrative_ms       INTEGER,                 -- when we first read that narrative
    PRIMARY KEY (feed, chain, token_address)
);
CREATE INDEX IF NOT EXISTS idx_alpha_feed_first_seen_ts ON alpha_feed_first_seen(first_seen_ms);
CREATE INDEX IF NOT EXISTS idx_alpha_feed_first_seen_token ON alpha_feed_first_seen(chain, token_address);
