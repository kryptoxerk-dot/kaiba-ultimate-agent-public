-- Learning loop bookkeeping.
--
-- Strictly additive: this migration creates only new tables and never ALTERs a table an
-- earlier migration owns, so it can land while execution is still being built.
--
-- The design rule behind these tables is that the evaluator's evidence must survive the
-- thing it evaluates. Gate verdicts, reflection inputs and playbook hit/miss history are
-- append-only logs; nothing here is ever rewritten to make a later verdict look better.

-- Append-only hit/miss log for playbook rules. `playbook.hits` / `playbook.misses` are the
-- running counters; this is the evidence trail behind them and the source of "last hit",
-- which is what staleness expiry is measured against.
CREATE TABLE IF NOT EXISTS playbook_hits (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  rule_id   TEXT NOT NULL,
  ts_ms     INTEGER NOT NULL,
  outcome   TEXT NOT NULL,              -- hit | miss
  ref       TEXT
);
CREATE INDEX IF NOT EXISTS idx_playbook_hits ON playbook_hits(rule_id, id);
CREATE INDEX IF NOT EXISTS idx_playbook_hits_outcome ON playbook_hits(outcome, ts_ms DESC);

-- Why a rule stopped being active. A retirement is a status change plus a journal entry;
-- the row is never deleted, so a retired rule can still be cited as evidence later.
CREATE TABLE IF NOT EXISTS playbook_retirements (
  rule_id     TEXT PRIMARY KEY,
  retired_ms  INTEGER NOT NULL,
  reason      TEXT NOT NULL,
  journal_seq INTEGER
);

-- Which recorded trades belong to which arm of an experiment. Written by the shadow
-- runner. The shadow gate refuses to guess when the mapping is absent: an unlabelled
-- trade stream cannot be used to promote anything.
CREATE TABLE IF NOT EXISTS experiment_trades (
  experiment_id TEXT NOT NULL,
  trade_id      TEXT NOT NULL,
  arm           TEXT NOT NULL DEFAULT 'candidate',   -- candidate | incumbent
  linked_ms     INTEGER NOT NULL,
  PRIMARY KEY (experiment_id, trade_id)
);
CREATE INDEX IF NOT EXISTS idx_experiment_trades_arm ON experiment_trades(experiment_id, arm);

-- Every gate evaluation, pass or fail, with the numbers it was decided on. Append-only:
-- a failing verdict is never overwritten by a later passing one, both are on the record.
CREATE TABLE IF NOT EXISTS gate_results (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_id TEXT NOT NULL,
  gate          TEXT NOT NULL,              -- replay | shadow
  passed        INTEGER NOT NULL,
  reasons_json  TEXT NOT NULL DEFAULT '[]',
  metrics_json  TEXT NOT NULL DEFAULT '{}',
  created_ms    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gate_results ON gate_results(experiment_id, id);

-- One row per nightly reflection: the window it covered, the pseudonym map used to mask
-- token identities before the model saw anything, and what came back. A rejected
-- reflection is kept with its rejection reason rather than dropped.
CREATE TABLE IF NOT EXISTS reflection_runs (
  run_id        TEXT PRIMARY KEY,
  created_ms    INTEGER NOT NULL,
  since_ms      INTEGER NOT NULL,
  until_ms      INTEGER NOT NULL,
  packet_digest TEXT NOT NULL,
  mask_json     TEXT NOT NULL DEFAULT '{}',
  status        TEXT NOT NULL DEFAULT 'built',   -- built | applied | rejected
  reason        TEXT,
  result_json   TEXT,
  applied_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_reflection_runs ON reflection_runs(created_ms DESC);
