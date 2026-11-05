-- X (Twitter) narrative evidence (kaiba/providers/x_search.py, kaiba/ingest/x_narrative.py).
-- Records only. The daily signal audit (kaiba/learning/signal_audit.py) is where these
-- features have to earn a place before anything trades on them.
--
--   x_posts      every post a search returned, once (first time seen). refs_json holds the
--                cashtags and contract addresses found in the text.
--   x_token_obs  one row per (chain, token, observation): what X looked like for that
--                token's contract address at that moment. Only rows with observed_ms <= a
--                signal's time may be read as that signal's feature (no lookahead).

CREATE TABLE IF NOT EXISTS x_posts (
    post_id          TEXT PRIMARY KEY,
    first_seen_ms    INTEGER NOT NULL,
    query            TEXT,
    backend          TEXT,
    author           TEXT,
    author_followers INTEGER,
    author_verified  INTEGER,
    created_ms       INTEGER,
    text             TEXT,
    likes            INTEGER,
    reposts          INTEGER,
    replies          INTEGER,
    views            INTEGER,
    url              TEXT,
    refs_json        TEXT
);
CREATE INDEX IF NOT EXISTS idx_x_posts_seen ON x_posts (first_seen_ms);

CREATE TABLE IF NOT EXISTS x_token_obs (
    chain          TEXT NOT NULL,
    token          TEXT NOT NULL,
    observed_ms    INTEGER NOT NULL,
    backend        TEXT,
    ok             INTEGER NOT NULL,
    reason         TEXT,
    n_posts        INTEGER,
    n_authors      INTEGER,
    max_followers  INTEGER,
    sum_likes      INTEGER,
    sum_views      INTEGER,
    earliest_ms    INTEGER,
    posts_1h       INTEGER,
    PRIMARY KEY (chain, token, observed_ms)
);
CREATE INDEX IF NOT EXISTS idx_x_token_obs_token ON x_token_obs (chain, token, observed_ms);
