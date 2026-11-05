-- Metered provider allowances (credits), as opposed to the request-rate bookkeeping in
-- 001_core.sql.
--
-- kaiba.core.limiter counts *requests* and enforces a daily request cap. That is the wrong
-- unit for providers that sell a monthly pool of credits where routes cost different
-- amounts: on Helius Free one DAS page costs 10 credits, a priority-fee estimate costs 1,
-- and a full-transaction history page costs 10 per 100 transactions returned. Counting
-- requests cannot tell those apart, and the failure mode is silent overage billing
-- ($5 per extra million on Helius), which is exactly the kind of cost that never shows up
-- until the invoice.
--
-- One row per (provider, route, period). `period` is a UTC window id — 'YYYY-MM' for a
-- monthly allowance — so the reset boundary is derivable and old periods stay readable as
-- history instead of being zeroed in place.
--
-- `estimated` is 1 when at least one call in the row was charged at a cost we could not
-- verify from the provider's own documentation. A budget report that cannot say which half
-- of its numbers are guesses is worse than one that admits it.

CREATE TABLE IF NOT EXISTS provider_budget (
  provider   TEXT    NOT NULL,
  route      TEXT    NOT NULL,          -- limiter endpoint string, e.g. 'das.getTokenAccounts'
  period     TEXT    NOT NULL,          -- 'YYYY-MM' (UTC) for monthly allowances
  calls      INTEGER NOT NULL DEFAULT 0,
  credits    INTEGER NOT NULL DEFAULT 0,
  denied     INTEGER NOT NULL DEFAULT 0, -- calls refused because the allowance was spent
  estimated  INTEGER NOT NULL DEFAULT 0, -- 1 = some credits here were charged at a guessed rate
  first_ms   INTEGER NOT NULL,
  last_ms    INTEGER NOT NULL,
  PRIMARY KEY (provider, route, period)
);

CREATE INDEX IF NOT EXISTS idx_provider_budget_period ON provider_budget(provider, period);
