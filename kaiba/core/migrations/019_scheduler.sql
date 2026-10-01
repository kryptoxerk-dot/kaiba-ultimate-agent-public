-- Maintenance scheduler state (kaiba/ops/scheduler.py), Phase 7 "Operate".
--
-- Every piece of intelligence the agent depends on (creator history, wallet swap history,
-- wallet grades, early-alpha sources, per-token trade flow, native price samples, the
-- journal hash chain, the validation power report) was built with a CLI entry point and
-- no scheduler, so each went stale unless someone typed a command. This is where the
-- scheduler keeps the answer to the one question that matters for such a service: is
-- maintenance actually happening? A job that silently stopped is the failure mode this
-- project keeps meeting, and a job whose last success is only in a process's memory
-- cannot be told apart from one that never ran once the process restarts.
--
-- `ops_jobs` is one row per configured job, and it is what makes a restart idempotent:
-- `next_due_ms` survives the process, so booting does not re-run everything. `ops_runs` is
-- the per-execution history the dashboard and `kaiba ops status --runs` read; it is pruned
-- by the scheduler's own `ops_prune` job. `ops_quota` is the per-UTC-day ledger behind the
-- "wallets per day" budget: a job that grades wallets reads today's row before spending,
-- so a restart inside the day cannot reset the count and double-spend the month.

CREATE TABLE IF NOT EXISTS ops_jobs (
  name                 TEXT    PRIMARY KEY,
  enabled              INTEGER NOT NULL DEFAULT 1,
  interval_s           INTEGER NOT NULL,
  timeout_s            INTEGER NOT NULL,
  spends_helius        INTEGER NOT NULL DEFAULT 0,
  next_due_ms          INTEGER,                    -- NULL until the scheduler has planned it
  last_run_ms          INTEGER,
  last_success_ms      INTEGER,
  last_error_ms        INTEGER,
  last_status          TEXT,                       -- ok | error | timeout | skipped | running
  last_error           TEXT,                       -- redacted, capped
  last_duration_ms     INTEGER,
  last_result_json     TEXT    NOT NULL DEFAULT '{}',
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  runs                 INTEGER NOT NULL DEFAULT 0,
  failures             INTEGER NOT NULL DEFAULT 0,
  credits_total        INTEGER NOT NULL DEFAULT 0, -- Helius credits attributed to this job, all time
  updated_ms           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ops_runs (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  job          TEXT    NOT NULL,
  started_ms   INTEGER NOT NULL,
  finished_ms  INTEGER,
  status       TEXT    NOT NULL,                   -- running | ok | error | timeout | skipped
  duration_ms  INTEGER,
  credits      INTEGER NOT NULL DEFAULT 0,
  result_json  TEXT    NOT NULL DEFAULT '{}',
  error        TEXT,
  host         TEXT,
  pid          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ops_runs_job ON ops_runs(job, started_ms DESC);
CREATE INDEX IF NOT EXISTS idx_ops_runs_started ON ops_runs(started_ms);

CREATE TABLE IF NOT EXISTS ops_quota (
  job        TEXT    NOT NULL,
  period     TEXT    NOT NULL,                     -- 'YYYY-MM-DD' (UTC)
  units      INTEGER NOT NULL DEFAULT 0,           -- what the job counts: wallets, tokens, ...
  credits    INTEGER NOT NULL DEFAULT 0,
  updated_ms INTEGER NOT NULL,
  PRIMARY KEY (job, period)
);
