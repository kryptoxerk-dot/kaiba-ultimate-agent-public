-- Who bought each seed token, kept incrementally (kaiba/intelligence/seeds.refresh_seed_buyers).
-- Replaces seed_counts' per-run `GROUP BY wallet ... token IN (400 seeds)`, which the planner
-- answered by walking idx_swaps_wallet for the whole chain: > 60 s per chunk, 5 chunks per
-- chain, so wallet_seeds timed out every run (2026-10-06).
CREATE TABLE IF NOT EXISTS seed_buyers (
    chain   TEXT NOT NULL,
    token   TEXT NOT NULL,
    wallet  TEXT NOT NULL,
    PRIMARY KEY (chain, token, wallet)
) WITHOUT ROWID;

-- One row per seed whose history has been (or is being) read into seed_buyers. done_ms NULL
-- means the backfill is part-way; (at_ts, at_id) is the last swap it finished, on
-- idx_swaps_token order, so a deadline-cut token resumes instead of restarting.
CREATE TABLE IF NOT EXISTS seed_buyer_tokens (
    chain    TEXT NOT NULL,
    token    TEXT NOT NULL,
    at_ts    INTEGER NOT NULL DEFAULT -1,
    at_id    INTEGER NOT NULL DEFAULT -1,
    done_ms  INTEGER,
    PRIMARY KEY (chain, token)
) WITHOUT ROWID;
