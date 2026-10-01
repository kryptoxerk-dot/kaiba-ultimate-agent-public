-- The trial registry: every distinct parameter configuration a lane has actually run
-- under, recorded automatically at decision time.
--
-- Why this exists. The deflated Sharpe ratio asks "how many attempts did you make before
-- this one looked good?", and deflates the result by the expected maximum of that many
-- attempts at nothing. Get the number wrong and the statistic is worthless in the
-- flattering direction. Until now `_trial_count` read `experiments`, which only contains
-- configurations somebody remembered to file. A lane whose thresholds were hand-tuned
-- twenty times and formally proposed twice deflated as if it had been tried twice.
--
-- So this counts what was *run*, not what was *declared*. Append-only: a configuration
-- that is retired still counts against the deflation, because it was still an attempt.

CREATE TABLE IF NOT EXISTS lane_trials (
  trial_id      TEXT PRIMARY KEY,        -- digest(lane, params) — the configuration's identity
  lane          TEXT NOT NULL,
  params_json   TEXT NOT NULL,
  first_seen_ms INTEGER NOT NULL,
  last_seen_ms  INTEGER NOT NULL,
  decisions     INTEGER NOT NULL DEFAULT 0,
  source        TEXT NOT NULL DEFAULT 'decision'  -- decision | experiment | manual
);

CREATE INDEX IF NOT EXISTS lane_trials_lane ON lane_trials(lane, first_seen_ms);
