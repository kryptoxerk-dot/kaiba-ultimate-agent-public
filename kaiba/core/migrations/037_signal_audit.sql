-- The daily self-audit (kaiba/learning/signal_audit.py). Records only; nothing reads these
-- tables to make a trading decision.
--
--   signal_audit_runs       one row per audit run: summary JSON (baselines per lane/chain,
--                           coverage, wallet persistence).
--   signal_audit_cells      one row per (run, lane, chain, feature, band): the replayed
--                           outcome of every signal in that band, on the whole window and on
--                           its older and newer halves, with a verdict (edge / lift / drag /
--                           noise / thin; `lookahead:` prefix when the feature reads today's
--                           grades).
--   wallet_grade_snapshots  A/B/C grades frozen once per UTC day. `wallet_scores` is
--                           overwritten in place, so without this no past signal can be asked
--                           what grade its wallets had WHEN they bought. That question
--                           blocked the Robinhood A/B finding of 2026-10-05.
--
-- Outcomes are fractions (0.10 = +10%), capped at +300% per signal before averaging.

CREATE TABLE IF NOT EXISTS signal_audit_runs (
    run_id        TEXT PRIMARY KEY,
    ts_ms         INTEGER NOT NULL,
    version       TEXT NOT NULL,
    summary_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signal_audit_runs_ts ON signal_audit_runs (ts_ms);

CREATE TABLE IF NOT EXISTS signal_audit_cells (
    run_id    TEXT NOT NULL,
    lane      TEXT NOT NULL,
    chain     TEXT NOT NULL,
    feature   TEXT NOT NULL,
    band      TEXT NOT NULL,
    n         INTEGER NOT NULL,
    mean      REAL,
    median    REAL,
    win       REAL,
    old_n     INTEGER NOT NULL,
    old_mean  REAL,
    new_n     INTEGER NOT NULL,
    new_mean  REAL,
    verdict   TEXT NOT NULL,
    PRIMARY KEY (run_id, lane, chain, feature, band)
);

CREATE TABLE IF NOT EXISTS wallet_grade_snapshots (
    day      TEXT NOT NULL,
    chain    TEXT NOT NULL,
    address  TEXT NOT NULL,
    grade    TEXT NOT NULL,
    score    REAL,
    PRIMARY KEY (day, chain, address)
);
CREATE INDEX IF NOT EXISTS idx_wallet_grade_snapshots_day ON wallet_grade_snapshots (day);
