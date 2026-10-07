-- Which X post each new launch was made from (kaiba/ingest/tweet_sources.py): the author
-- league table behind "which accounts to follow". RECORD ONLY.
--
--   kind  tweet     the metadata twitter/website field links x.com/<author>/status/<tweet_id>
--         profile   it links a bare account (author set, tweet_id NULL)
--         none      no X link
--         unfetched the metadata JSON could not be read (via = the last error)
CREATE TABLE IF NOT EXISTS tweet_sources (
    chain            TEXT NOT NULL,
    token            TEXT NOT NULL,
    kind             TEXT NOT NULL,
    author           TEXT,                 -- lower case, no @
    tweet_id         TEXT,
    token_created_ms INTEGER,
    fetched_ms       INTEGER NOT NULL,
    via              TEXT,                 -- metadata host, or the fetch error
    name             TEXT,
    symbol           TEXT,
    launchpad        TEXT,
    PRIMARY KEY (chain, token)
);
CREATE INDEX IF NOT EXISTS idx_tweet_sources_author ON tweet_sources (author, token_created_ms);
CREATE INDEX IF NOT EXISTS idx_tweet_sources_tweet ON tweet_sources (tweet_id);
