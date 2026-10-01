-- Ingest bookkeeping: feed health and measured feed latency.
--
-- Latency is stored as rolled-up windows rather than per-message rows: at 20+ pump.fun
-- creations a second a per-sample table would dwarf every other table in the database,
-- and the only question we actually ask of it is "what is our p50/p95 detect lag this
-- hour, and is it bad enough to justify paying for a gRPC lane" (PLAN §13.15).

CREATE TABLE IF NOT EXISTS ingest_latency (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  source          TEXT    NOT NULL,          -- pumpportal | gmgn | telegram
  stream          TEXT    NOT NULL,          -- new_token | migration | trade | <feed>
  window_start_ms INTEGER NOT NULL,
  window_end_ms   INTEGER NOT NULL,
  samples         INTEGER NOT NULL,
  p50_ms          INTEGER,
  p95_ms          INTEGER,
  min_ms          INTEGER,
  max_ms          INTEGER,
  UNIQUE (source, stream, window_start_ms)
);
CREATE INDEX IF NOT EXISTS idx_ingest_latency ON ingest_latency(source, stream, window_end_ms DESC);

-- One row per listener, written by the runner's supervisor and by the listeners
-- themselves. The dashboard's "ingestion health" panel reads this; the acceptance test
-- for Phase 1 ("24 h of uninterrupted ingestion") is checked against it.
CREATE TABLE IF NOT EXISTS ingest_status (
  feed          TEXT    PRIMARY KEY,
  state         TEXT    NOT NULL,            -- starting|running|reconnecting|stopped|crashed|disabled
  started_ms    INTEGER,
  last_event_ms INTEGER,
  events_seen   INTEGER NOT NULL DEFAULT 0,
  restarts      INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT,
  updated_ms    INTEGER NOT NULL
);
