-- The strategy factory (kaiba/learning/strategy_search.py, PROFIT-2, 2026-10-13). Records
-- only; nothing reads these tables to make a trading decision, and no lane, mode, size or
-- risk.yaml key is ever written from them. Arming anything stays the owner's decision.
--
-- Numbered 047 because 044-046 (tweet launcher) exist untracked in the lead's checkout.
--
--   strategy_search_runs     one row per daily search: how many candidates were TRIED on the
--                            older part of the window, how many were carried to the newer part
--                            (the holdout), and how many survived the multiple-testing
--                            correction AND the owner's pass line there.
--   strategy_candidates      every survivor, frozen at the moment it was selected: its filter
--                            (with the cut points learned on the training data), its exit
--                            policy (with the ladder numbers of that day), and `selected_ms`.
--                            The fwd_* columns summarise its FORWARD record, which is the only
--                            record that matters.
--   strategy_forward_trades  one row per (candidate, signal) for signals with t0 AFTER the
--                            candidate's selected_ms whose 6 h replay horizon has elapsed.
--                            First measurement stands (INSERT OR IGNORE): a forward trade is
--                            never revised after it is booked.
--   strategy_forward_days    the forward score per candidate per UTC day, against the same
--                            pass line (kaiba/learning/sprint.py), so "passed N reports in a
--                            row" is read from rows, not remembered.
--
-- Returns are fractions (0.10 = +10%), net of 3.25%/leg plus the chain's measured extra
-- cost (sprint.EXTRA_COST_PER_SIDE), capped at +300% per trade.

CREATE TABLE IF NOT EXISTS strategy_search_runs (
    run_id        TEXT PRIMARY KEY,
    ts_ms         INTEGER NOT NULL,
    version       TEXT NOT NULL,
    tried         INTEGER NOT NULL,
    selected      INTEGER NOT NULL,
    survivors     INTEGER NOT NULL,
    summary_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strategy_search_runs_ts ON strategy_search_runs (ts_ms);

CREATE TABLE IF NOT EXISTS strategy_candidates (
    candidate_id    TEXT PRIMARY KEY,
    lane            TEXT NOT NULL,
    chain           TEXT NOT NULL,
    label           TEXT NOT NULL,
    spec_json       TEXT NOT NULL,
    selected_ms     INTEGER NOT NULL,
    run_id          TEXT NOT NULL,
    selection_json  TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'tracking',
    fwd_n           INTEGER NOT NULL DEFAULT 0,
    fwd_mean        REAL,
    fwd_lower       REAL,
    fwd_p           REAL,
    fwd_verdict     TEXT,
    fwd_why         TEXT,
    fwd_streak      INTEGER NOT NULL DEFAULT 0,
    fwd_updated_ms  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_strategy_candidates_status ON strategy_candidates (status);

CREATE TABLE IF NOT EXISTS strategy_forward_trades (
    candidate_id  TEXT NOT NULL,
    token         TEXT NOT NULL,
    t0_ms         INTEGER NOT NULL,
    ret           REAL NOT NULL,
    recorded_ms   INTEGER NOT NULL,
    PRIMARY KEY (candidate_id, token, t0_ms)
);

CREATE TABLE IF NOT EXISTS strategy_forward_days (
    candidate_id  TEXT NOT NULL,
    day           TEXT NOT NULL,
    ts_ms         INTEGER NOT NULL,
    n             INTEGER NOT NULL,
    mean          REAL,
    mean_old      REAL,
    mean_new      REAL,
    lower         REAL,
    p             REAL,
    family        INTEGER NOT NULL,
    verdict       TEXT NOT NULL,
    why           TEXT,
    PRIMARY KEY (candidate_id, day)
);
