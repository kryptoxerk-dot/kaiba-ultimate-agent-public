-- Replay: what a lane would have done on a recorded episode, and the denominator it sat in.
--
-- Why this table set exists.
--
--   Our own power calculation says a directional memecoin book needs ~5,447 closed trades
--   before it can conclude anything, and 16,122 at 100 parameter trials. At 30-60 trades a
--   week that is years, against a venue that changes roughly every eight weeks. A sample
--   collected across regime boundaries is not one sample. So "trade and wait" is not a
--   sequence we can execute, and replay is the only substitute we have.
--
--   A replay is also the easiest thing in this repository to lie with. Three specific lies
--   are what these tables are shaped to make hard:
--
--   1. **Reporting a result without its denominator.** `replay_episodes` is written for
--      EVERY candidate episode, eligible or not, with the reason it was excluded. A run
--      that replayed 91 episodes out of 1,068 migrations is a different claim from one
--      that replayed 91 out of 91, and the two are indistinguishable unless the excluded
--      rows are stored next to the included ones. `eligible` is 0 far more often than 1
--      and that is the honest shape.
--
--   2. **Five hundred quiet variants and one reported winner.** `replay_runs.trial_id` is
--      the `lane_trials` id of the configuration that ran, registered BEFORE the result
--      was read. `honest_trials` records what the deflation actually used. A run whose
--      `dsr_deflated` is 0 did not deflate: fewer than two comparable trials existed, and
--      the number in `dsr` is a plain probabilistic Sharpe against zero wearing a
--      deflated Sharpe's name. The column exists so that cannot be read the other way.
--
--   3. **A price path that includes the future.** `cursor_basis` names the rule the run
--      enforced, and `t0_basis` on each episode names where the episode's clock came
--      from. `tokens.migrated_ms` is OUR wall clock, written by the ingest at the moment
--      the frame arrived, and the row is mutated in place - there is no version of it
--      that can be read "as of" anything. A run whose `t0_basis` is `event:token.migrated`
--      took its clock from a timestamped, immutable event row instead.
--
-- `replay_runs` and `replay_outcomes` are append-only: a re-run is a new `run_id`, and
-- two runs disagreeing is a finding, not a conflict to be resolved by overwriting one of
-- them. `replay_episodes` is the exception and is keyed on (chain, token, horizon): it
-- is the denominator as the database currently stands, and a token whose tape has since
-- been deepened genuinely has a new eligibility verdict.

-- One configuration, executed once, over one episode set.
CREATE TABLE IF NOT EXISTS replay_runs (
  run_id           TEXT PRIMARY KEY,
  created_ms       INTEGER NOT NULL,
  lane             TEXT    NOT NULL,
  arm              TEXT    NOT NULL,          -- long | short_mirror
  executable       INTEGER NOT NULL,          -- 0 = no venue exists for this arm
  trial_id         TEXT    NOT NULL,          -- lane_trials.trial_id, registered before reading
  params_json      TEXT    NOT NULL,          -- the EFFECTIVE merged lane params, not the overrides
  config_json      TEXT    NOT NULL,          -- the replay's own knobs
  cursor_basis     TEXT    NOT NULL,          -- how no-lookahead was enforced
  horizon_s        INTEGER NOT NULL,
  episodes_total   INTEGER NOT NULL,          -- every candidate considered
  episodes_filled  INTEGER NOT NULL,          -- reached a modelled entry AND exit
  -- Results. NULL means not computable, never 0. `_pct` columns are TEXT Decimals.
  mean_return_pct  TEXT,
  median_return_pct TEXT,
  ci_lo_pct        TEXT,
  ci_hi_pct        TEXT,
  ci_method        TEXT,
  sharpe           TEXT,
  dsr              TEXT,                      -- probability from gates.deflated_sharpe
  dsr_deflated     INTEGER NOT NULL DEFAULT 0,-- 1 only when the deflation term was non-zero
  dsr_notes_json   TEXT    NOT NULL DEFAULT '[]',
  pbo              TEXT,                      -- gates.pbo_cscv over the block matrix
  honest_trials    INTEGER,                   -- validation.honest_trials at report time
  verdict          TEXT    NOT NULL,          -- separated | cannot_separate | unevaluable
  census_json      TEXT    NOT NULL DEFAULT '{}',
  notes_json       TEXT    NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_replay_runs_lane ON replay_runs (lane, created_ms DESC);
CREATE INDEX IF NOT EXISTS idx_replay_runs_trial ON replay_runs (trial_id);

-- The denominator. One row per (episode, horizon) CONSIDERED, whether or not it was used.
--
-- `eligible = 0` rows are the point of the table. An episode excluded for `no_depth` is a
-- different kind of absence from one excluded for `too_few_swaps`, and a result whose
-- exclusions are concentrated in one reason is a result about that reason.
CREATE TABLE IF NOT EXISTS replay_episodes (
  chain            TEXT    NOT NULL,
  token            TEXT    NOT NULL,
  horizon_s        INTEGER NOT NULL,
  t0_ms            INTEGER NOT NULL,
  t0_basis         TEXT    NOT NULL,          -- event:token.migrated | swap:create_tx | ...
  swaps_in_window  INTEGER NOT NULL,
  priced_in_window INTEGER NOT NULL,          -- swaps carrying a usable price
  eligible         INTEGER NOT NULL,
  reason           TEXT    NOT NULL,          -- why eligible, or why not
  observed_ms      INTEGER NOT NULL,
  PRIMARY KEY (chain, token, horizon_s)
);

CREATE INDEX IF NOT EXISTS idx_replay_episodes_eligible ON replay_episodes (horizon_s, eligible);

-- One modelled round trip. `refused_reason` is populated when the fill model declined,
-- which is a result and not an error: a pool too thin to take our size is exactly the
-- information a paper broker that always fills would have destroyed.
CREATE TABLE IF NOT EXISTS replay_outcomes (
  run_id           TEXT    NOT NULL,
  chain            TEXT    NOT NULL,
  token            TEXT    NOT NULL,
  t0_ms            INTEGER NOT NULL,
  entry_ms         INTEGER,
  exit_ms          INTEGER,
  entry_price_usd  TEXT,
  exit_price_usd   TEXT,
  depth_lamports   INTEGER,                   -- implied one-sided pool at entry; NULL = unknown
  depth_basis      TEXT,
  gross_return_pct TEXT,                      -- mid-to-mid, no costs
  net_return_pct   TEXT,                      -- after the fill model's own costs
  cost_in_frac     TEXT,                      -- fraction of notional charged on the entry leg
  cost_out_frac    TEXT,
  fill_basis       TEXT,                      -- paper.FillBasis of the entry leg
  refused_reason   TEXT,
  signal_strength  TEXT,
  observed_ms      INTEGER NOT NULL,
  PRIMARY KEY (run_id, chain, token)
);

CREATE INDEX IF NOT EXISTS idx_replay_outcomes_run ON replay_outcomes (run_id);
