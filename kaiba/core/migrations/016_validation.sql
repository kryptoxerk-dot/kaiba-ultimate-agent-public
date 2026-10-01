-- Phase 4 validation harness: power reporting, the wallet-grading control arm, and the
-- six ordered promotion gates.
--
-- Why these tables exist rather than reusing `gate_results`.
--
--   1. `gate_results.passed` is an INTEGER. A boolean cannot express "we do not have
--      enough data to say", which is the honest answer to almost every question this
--      harness asks and will stay the honest answer for months. Recording an
--      underpowered result as passed=0 would file it next to a genuine failure, and the
--      two demand opposite responses: a failure means the strategy is wrong, an
--      underpowered result means we have not looked yet.
--   2. A passed gate **expires**. The venue's fee schedule changed on 2026-09-01 and its
--      dominant shred feed died on 2026-09-05; a verdict from before either is a verdict
--      about a different game. `expires_ms` is written with the verdict, not derived at
--      read time, so a stored pass carries its own use-by date.
--   3. The wallet cohort must be **frozen before it is tracked**. If cohort membership is
--      recomputed at read time, the "forward" test silently conditions on information
--      from after t and reproduces exactly the ex-post contamination arXiv:2602.14860
--      warns about in its own Table I. So membership is a row, written once, at t.

-- One row per validation run of any kind. Append-only; a later run never rewrites an
-- earlier verdict.
CREATE TABLE IF NOT EXISTS validation_runs (
  run_id      TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,              -- power | control | gates | falsification
  lane        TEXT,                       -- NULL means the whole book
  verdict     TEXT NOT NULL,              -- pass | fail | underpowered | blocked
  sample_n    INTEGER NOT NULL DEFAULT 0, -- closed trades (or wallet pairs) the verdict rests on
  created_ms  INTEGER NOT NULL,
  expires_ms  INTEGER,                    -- NULL only when the verdict was never a pass
  summary     TEXT NOT NULL DEFAULT '',   -- one line a human can read without the JSON
  payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_validation_runs_kind ON validation_runs(kind, created_ms DESC);
CREATE INDEX IF NOT EXISTS idx_validation_runs_lane ON validation_runs(lane, created_ms DESC);

-- One row per gate per run. The gates are strictly ordered, so `ordinal` is part of the
-- evidence: a gate that was never reached is recorded as `blocked`, which is neither a
-- pass nor a failure and must never be read as either.
CREATE TABLE IF NOT EXISTS validation_gates (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT NOT NULL,
  ordinal       INTEGER NOT NULL,         -- 0..5, the protocol's own numbering
  gate          TEXT NOT NULL,            -- data-integrity | execution-realism | ...
  verdict       TEXT NOT NULL,            -- pass | fail | underpowered | blocked
  sample_n      INTEGER NOT NULL DEFAULT 0,
  criteria_json TEXT NOT NULL DEFAULT '[]',  -- every numeric criterion, value and threshold
  notes_json    TEXT NOT NULL DEFAULT '[]',
  created_ms    INTEGER NOT NULL,
  expires_ms    INTEGER                   -- 8 weeks from the check, on a pass
);
CREATE INDEX IF NOT EXISTS idx_validation_gates_run ON validation_gates(run_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_validation_gates_gate ON validation_gates(gate, created_ms DESC);

-- The frozen cohorts for the matched-control test of wallet grading.
--
-- `arm` is graded | control. `matched_on_json` records the covariate values each member
-- was matched on, and `unmatched_json` records, per cohort, what we knew we could not
-- match on. Writing the second one down is not decoration: a matched-control study whose
-- unmatched confounders are undeclared is an uncontrolled study with extra arithmetic.
CREATE TABLE IF NOT EXISTS wallet_cohorts (
  cohort_id     TEXT NOT NULL,            -- one freeze event
  arm           TEXT NOT NULL,            -- graded | control
  chain         TEXT NOT NULL,
  address       TEXT NOT NULL,
  pair_id       INTEGER,                  -- links a graded wallet to its matched control
  frozen_ms     INTEGER NOT NULL,         -- t: the instant membership was fixed
  grade         TEXT,                     -- as it stood at t, for audit only
  covariates_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (cohort_id, arm, chain, address)
);
CREATE INDEX IF NOT EXISTS idx_wallet_cohorts_pair ON wallet_cohorts(cohort_id, pair_id);

-- Metadata for a freeze: when, on what, and what could not be matched.
CREATE TABLE IF NOT EXISTS wallet_cohort_freezes (
  cohort_id      TEXT PRIMARY KEY,
  frozen_ms      INTEGER NOT NULL,
  chain          TEXT NOT NULL,
  graded_n       INTEGER NOT NULL DEFAULT 0,
  control_n      INTEGER NOT NULL DEFAULT 0,
  matched_on_json TEXT NOT NULL DEFAULT '[]',
  unmatched_json  TEXT NOT NULL DEFAULT '[]',
  notes_json      TEXT NOT NULL DEFAULT '[]'
);
