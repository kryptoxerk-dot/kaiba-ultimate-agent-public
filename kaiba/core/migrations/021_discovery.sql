-- Wallet discovery from flow we observed ourselves.
--
-- Why this is not another leaderboard table.
--
--   1. The 6,236 wallets in `wallets` came from GMGN's "Smart Money" labels. GMGN defines
--      that as "wallet who often earns money" and publishes no method, no cost-basis
--      convention and no out-of-sample test; the other board, Kolscan, was bought by
--      pump.fun, which earns the fees the board advertises. So the source of our wallet
--      universe is an advertisement, and `kaiba/intelligence/discover.py` exists to
--      replace it with structural facts from our own `swaps` and `first_buyers`.
--   2. Replacing it with *our own* ranking would rebuild the same artefact. Screening
--      100,000 zero-skill wallets yields ~98 with a perfect 10-for-10 record and ~2,460
--      at "45% over 100 trades" (research doc 13, A2). So `screened_addresses` is a
--      column, not a log line: every cohort this module writes carries the size of the
--      screen that produced it, and therefore how many wallets would look that good by
--      chance. A row here cannot be read without its own deflation.
--   3. A cohort is `research`, never `trusted_copy`. Nothing in discovery may promote a
--      wallet. Only measured forward performance against the matched control in
--      `kaiba/learning/validation.py` could, and that is a separate, later decision.
--
-- The forward-tracking membership deliberately does NOT live here. A frozen discovery
-- cohort is written into `wallet_cohorts` / `wallet_cohort_freezes` (migration 016) so
-- `validation.control_arm()` measures it with the same code that measures a graded
-- cohort. A second cohort table would mean a second matched-control implementation, and
-- the whole point of the exercise is that there is only one.

-- One row per discovery run. Append-only in practice: a later run is a new run_id.
CREATE TABLE IF NOT EXISTS discovery_runs (
  run_id              TEXT PRIMARY KEY,
  chain               TEXT NOT NULL,
  run_ms              INTEGER NOT NULL,
  as_of_ms            INTEGER NOT NULL,   -- the cutoff; nothing after this was looked at
  model_version       TEXT NOT NULL,

  -- The multiple-comparisons denominator. `screened_addresses` is every wallet the run
  -- looked at, not the shortlist, because N in the deflation is the screen size.
  -- `screened_entities` is the same population collapsed to operators: five addresses
  -- from one funder are one opinion, and the honest N is somewhere between the two.
  screened_addresses  INTEGER NOT NULL DEFAULT 0,
  screened_entities   INTEGER NOT NULL DEFAULT 0,
  rejected_addresses  INTEGER NOT NULL DEFAULT 0,
  assessable          INTEGER NOT NULL DEFAULT 0,
  candidates          INTEGER NOT NULL DEFAULT 0,
  gate_cleared        INTEGER NOT NULL DEFAULT 0,

  -- The null the cohort is tested against. NULL means we could not measure a base rate
  -- from our own tape, in which case no wallet in the run may be called anything but
  -- unvalidated. It is never defaulted to 0.5.
  null_win_rate       REAL,
  null_basis          TEXT NOT NULL DEFAULT 'unavailable',
  null_pooled_trades  INTEGER NOT NULL DEFAULT 0,

  -- The sample-size gate actually applied, and what the screen implies by chance.
  min_closed_trades   INTEGER,            -- NULL when no gate could be computed
  bonferroni_alpha    REAL,
  expected_max_z      REAL,
  expected_false_positives REAL,          -- screened * alpha, uncorrected

  cohort_id           TEXT,               -- FK-by-convention into wallet_cohort_freezes
  summary             TEXT NOT NULL DEFAULT '',
  payload_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_discovery_runs_chain ON discovery_runs(chain, run_ms DESC);

-- One row per surviving candidate. `status` is never "good": it is unassessable,
-- unvalidated, or sample-gate-cleared, and the third still means only that the sample is
-- large enough to be tested, not that anything passed.
CREATE TABLE IF NOT EXISTS discovery_candidates (
  run_id              TEXT NOT NULL,
  chain               TEXT NOT NULL,
  address             TEXT NOT NULL,
  entity_key          TEXT NOT NULL,      -- entity id, observed co-entity group, or solo:<addr>
  status              TEXT NOT NULL,
  closed_trades       INTEGER NOT NULL DEFAULT 0,
  wins                INTEGER NOT NULL DEFAULT 0,
  win_rate            REAL,
  distinct_tokens     INTEGER NOT NULL DEFAULT 0,

  -- P(at least this many wins | the population base rate), and the number of zero-skill
  -- wallets in a screen this size that would match it. The second number is the one that
  -- stops a reader from believing the first.
  p_under_null        REAL,
  expected_peers      REAL,

  features_json       TEXT NOT NULL DEFAULT '{}',
  bases_json          TEXT NOT NULL DEFAULT '{}',   -- feature -> EvidenceBasis
  unknowns_json       TEXT NOT NULL DEFAULT '[]',   -- measured as nothing, not as zero
  blockers_json       TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (run_id, chain, address)
);
CREATE INDEX IF NOT EXISTS idx_discovery_candidates_status
  ON discovery_candidates(run_id, status);
CREATE INDEX IF NOT EXISTS idx_discovery_candidates_addr
  ON discovery_candidates(chain, address);

-- Every address the screen threw away and why. Kept because the rejects are the part of
-- the output with actual evidence behind them: sell-only settlement addresses and 40%+
-- transaction-failure bots are observed facts, whereas "this wallet is good" is a guess.
CREATE TABLE IF NOT EXISTS discovery_rejects (
  run_id     TEXT NOT NULL,
  chain      TEXT NOT NULL,
  address    TEXT NOT NULL,
  reason     TEXT NOT NULL,
  detail     TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, chain, address, reason)
);
CREATE INDEX IF NOT EXISTS idx_discovery_rejects_reason ON discovery_rejects(run_id, reason);
