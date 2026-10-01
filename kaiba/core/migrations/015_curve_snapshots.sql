-- Curve snapshots: the second observation that makes a rate possible.
--
-- `curve-velocity` is the highest-evidence lane we have (docs/EDGE-AND-VARIABLES.md §1,
-- variable #3) and on 2026-09-20 it had no input at all. The reason is arithmetic, not
-- plumbing: tier 1 scans most tokens inside their first minute, and a *rate* cannot be
-- read off a single observation. `sol_per_min` needs elapsed time and `sol_per_swap`
-- needs trades elapsed; one curve reading supplies neither. This table holds the earlier
-- observation so the next one becomes a delta.
--
-- Three design notes, each one a thing that was got wrong before it was written down.
--
-- 1. **Nothing here is a pump.fun "constant".** Creators choose a starting market cap and
--    observed graduation targets span 0.41 SOL to 115 SOL. The only invariant is
--    `virtual_token_reserves - real_token_reserves = 279,900,000,000,000`. So the raw
--    reserves are stored and every derived figure (progress, graduation target) is
--    recomputed per token from the constant-product invariant by
--    `kaiba/execution/scanner.py::curve_from_payload`. The derived columns below are a
--    convenience for querying, never an input to a later derivation.
--
-- 2. **`coverage_from_ms` is what makes a delta honest.** It is the oldest moment our
--    per-token trade collection is known to be *complete* down to, so a pair of snapshots
--    can only yield `sol_per_swap` when the newer one's coverage reaches back past the
--    older one's timestamp. Without it, dividing curve SOL by however many swap rows we
--    happen to hold overstates per-swap by ~130x (three observed trades out of four
--    hundred). NULL means "we do not know", never "from the beginning".
--
-- 3. **Money is integer base units.** Lamports and token atoms are exact; atoms exceed
--    2^53 and can exceed 2^63, so they are TEXT per docs/CONTRACT.md. Decimals are stored
--    as TEXT too, so nothing round-trips through a float.
--
-- Retention: this table is written once per tier-1 scan per token, so at 6.4 tokens/min
-- it would add ~9,200 rows a day forever. `kaiba.ingest.token_flow.prune_snapshots`
-- enforces two bounds - at most `snapshot_keep_per_token` rows per token (the newest) and
-- nothing older than `snapshot_max_age_s` - and is called opportunistically on write. The
-- per-token cap is the one that matters: a velocity delta only ever reads the previous
-- snapshot or two, so old rows are for backtesting, not for the lane.

CREATE TABLE IF NOT EXISTS curve_snapshots (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  chain                TEXT    NOT NULL,
  token                TEXT    NOT NULL,
  observed_ms          INTEGER NOT NULL,          -- OUR clock, when the payload was read
  -- Raw reserves, exactly as the curve reported them. Everything else is derivable.
  real_sol_lamports    INTEGER NOT NULL,
  virtual_sol_lamports INTEGER NOT NULL,
  real_token_atoms     TEXT    NOT NULL,
  virtual_token_atoms  TEXT    NOT NULL,
  -- Derived, for querying only. NULL when it could not be derived - never 0.
  progress_pct         TEXT,
  graduation_sol       TEXT,
  sol_in_curve         TEXT,
  -- Our own trade accounting at this instant.
  trades_seen          INTEGER,                   -- swap rows we hold for this token
  trades_basis         TEXT,                      -- how trades_seen was established
  coverage_from_ms     INTEGER,                   -- see note 2; NULL = unknown
  created_ms           INTEGER,                   -- the token's own creation time
  source               TEXT    NOT NULL DEFAULT 'pumpfun',
  -- One snapshot per token per millisecond. A retry inside the same millisecond is the
  -- same observation, not a second one, and two identical observations would make a
  -- zero-length interval look like a real one.
  UNIQUE (chain, token, observed_ms)
);
CREATE INDEX IF NOT EXISTS idx_curve_snap_token ON curve_snapshots(chain, token, observed_ms DESC);
CREATE INDEX IF NOT EXISTS idx_curve_snap_age   ON curve_snapshots(observed_ms);
