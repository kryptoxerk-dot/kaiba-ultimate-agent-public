-- StonkFun (Raydium LaunchLab) — a second launchpad, and the three things it broke.
--
-- Why a second venue at all.
--
--   Everything this system measured up to 2026-09-21 was measured on pump.fun and then
--   described as though it were a fact about Solana. Tape coverage, bundle share, creator
--   graduation rate, the exclusion layer's quarantine rate, the wallet-discovery base
--   rate: one venue, one sample, no way to tell which findings are about memecoins and
--   which are about pump.fun's particular plumbing. On DefiLlama's 2026-09-20 reading
--   StonkFun was 27.1% of Solana launchpad curve volume and 25.0% of the fees, and we
--   held zero rows from it.
--
-- What StonkFun actually is. Every claim in this block was read off mainnet on
-- 2026-09-21, not off a docs page; `kaiba/ingest/stonkfun.py` records which is which.
--
--   * It is a FRONTEND, not a program. Its pools are owned by Raydium LaunchLab
--     (`LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj`) and carry one of two StonkFun
--     `platform_config` pubkeys in the pool account. That pubkey is the venue attribution
--     and it is on chain — not a label from an index we cannot check.
--   * It IS a bonding curve: constant product over virtual + real reserves, exactly the
--     shape pump.fun uses. 793,100,000 of a 1,000,000,000 supply sell on the curve
--     (7,556 of 7,556 pools sampled), and the remainder plus the raise seed a Raydium
--     CPMM pool at graduation.
--   * The quote asset is NOT SOL. It is whatever the creator picked: of 7,556 pools,
--     1,935 quote in STONK, 537 in ZEC, 518 in wrapped SOL, 472 in AVAX, and the tail
--     runs to 501 distinct quote mints including tokenised equities (SPYx, NVDAx) at 8
--     decimals. Wrapped SOL is 6.9% of the venue. This is the single fact that shapes
--     everything below.
--   * The base mint is Token-2022 on 7,551 of 7,556 pools, and 3,824 of them carry a
--     transfer-fee extension of 100-300 bps. The amount the curve moves and the amount
--     the wallet receives are therefore DIFFERENT numbers on half the venue.
--   * The graduation target is per-launch and spans 0.1044713 to 5,194,848,650 quote
--     units across the sample — a factor of 5e10. There is no StonkFun constant to
--     hard-code, exactly as there was none on pump.fun.
--
-- Three changes, each because a pump.fun-shaped schema cannot hold this venue.
--
-- =====================================================================================
-- 1. swaps gains a quote leg, because `amount_native` is a SOL column and 93% of this
--    venue does not trade against SOL.
-- =====================================================================================
--
-- `swaps.amount_native` means lamports on Solana and wei on an EVM chain. Every consumer
-- reads it that way: `token_flow.trades_between` sums it as lamports, `gross_flow`
-- divides it by 1e9 and calls the result SOL, `lanes.curve_velocity` compares it against
-- a SOL floor. Writing 26,516 STONK atoms or 406 SPYx atoms into that column would make
-- every one of those readers silently wrong, and wrong in the direction that produces a
-- confident number rather than an error — a token quoted in a 4-decimal asset would read
-- as 0.0000001 SOL and one quoted in a 12-decimal asset as 26,000 SOL.
--
-- So `amount_native` stays a SOL column: `kaiba/ingest/stonkfun.py` populates it only
-- when the pool's quote mint is wrapped SOL, and leaves it NULL otherwise. NULL, never 0
-- — docs/CONTRACT.md rule 2, and here the difference is a factor of infinity rather than
-- a rounding error.
--
-- The quote leg goes in its own two columns instead. `amount_quote` is TEXT like every
-- other money column so a 12-decimal quote cannot overflow 2^63, and what the text means
-- depends on `quote_mint` (amended 2026-09-22; the SQL below is unchanged):
--   * `quote_mint` set  -> base units of that asset, integer text. This is what
--     `kaiba/ingest/stonkfun.py` writes: it knows the pool's quote mint and decimals.
--   * `quote_mint` NULL -> the provider's UI Decimal text, e.g. "0.147380625", NOT base
--     units. `kaiba/ingest/gmgn_feeds.py` writes the GMGN track feeds this way because
--     the feed never names the quote asset (MEASURED 2026-09-21: 0 of 50,805 live rows
--     carried `quote_address`, and on sol the quote is often a USD stable), so no honest
--     conversion exists and inventing one was wrong by up to 1e18. The 49,347 rows
--     `backfill_amount_native` moved out of `amount_native` follow the same rule.
--   A reader that needs base units must therefore check `quote_mint IS NOT NULL` (or
--   `source NOT LIKE 'gmgn:%'`) before summing; a NULL-`quote_mint` value is a number
--   of an unnamed asset and must never be added to a base-units one.
-- Both were NULL for every row written by the collectors that existed when this migration
-- shipped, which is the correct reading: those rows have a quote leg, nobody recorded
-- which asset it was in.
--
-- Additive and nullable, so no existing INSERT changes. Every writer in the tree
-- enumerates its columns.
ALTER TABLE swaps ADD COLUMN amount_quote TEXT;
ALTER TABLE swaps ADD COLUMN quote_mint   TEXT;

-- =====================================================================================
-- 2. stonkfun_launches: the per-pool facts a trade row cannot carry.
-- =====================================================================================
--
-- A LaunchLab pool has exactly one quote mint, one curve geometry and one graduation
-- target for its whole life, so those belong to the token, not to each of its trades.
-- They are kept in their own table rather than in `tokens.meta_json` because three of
-- them are read on the hot path — `quote_decimals` to turn an API decimal into base
-- units, `base_decimals` to do the same for the token leg, and `transfer_fee_bps` to
-- explain why the two legs of a trade do not balance — and a JSON extract per trade row
-- is both slower and unindexable.
--
-- Note what is NOT here. There is no `graduated_ms`. The launch index reports
-- `finishingRate` and the pool account reports `status`, and neither is a timestamp: the
-- most we can honestly say is the moment WE first saw the curve finished, which is what
-- `graduated_seen_ms` is named for. `tokens.migrated_ms` is left NULL rather than
-- stamped with our observation time, because a consumer computing "time from launch to
-- graduation" off our polling latency would get a number that looks like a measurement.
CREATE TABLE IF NOT EXISTS stonkfun_launches (
  chain              TEXT    NOT NULL,
  token              TEXT    NOT NULL,          -- the base mint
  pool               TEXT    NOT NULL,          -- LaunchLab pool account; the trade route's key
  platform_config    TEXT    NOT NULL,          -- which StonkFun config; on-chain venue proof
  config_id          TEXT,                      -- Raydium curve config (curve type, fee rate)

  base_decimals      INTEGER NOT NULL,
  quote_mint         TEXT    NOT NULL,
  quote_symbol       TEXT,
  quote_decimals     INTEGER NOT NULL,
  -- Token-2022 transfer-fee extension on the BASE mint, in basis points. 0 on 3,732 of
  -- 7,556 sampled pools, 100 on 2,401, 300 on 1,417. Where it is non-zero the curve-side
  -- amount and the wallet-side amount differ by it, and `swaps.amount_token` holds the
  -- curve-side figure (see kaiba/ingest/stonkfun.py::parse_trade).
  transfer_fee_bps   INTEGER,

  -- Curve constants, in base units, as the launch index reports them. Stored raw; every
  -- derived figure is recomputed per token. 793,100,000,000,000 on every pool sampled,
  -- which makes it a StonkFun constant and NOT a reason to hard-code one: pump.fun looked
  -- constant too until observed graduation targets turned out to span 0.41 to 115 SOL.
  total_base_sell    TEXT,
  -- Quote base units the curve must take in to graduate. Per-launch; the sample spans
  -- ten orders of magnitude. TEXT because a 12-decimal quote exceeds 2^63.
  graduation_quote   TEXT,

  migrate_type       TEXT,                      -- 'cpmm' on 7,556 of 7,556 sampled
  is_reward_launch   INTEGER,                   -- which of the two platform configs
  -- Percent of the graduation target raised, as the index last reported it. TEXT so it
  -- does not round-trip through a float. NULL = we have not looked, never 0.
  progress_pct       TEXT,
  -- Our clock, when we first saw the curve report a finished raise. NOT the graduation
  -- time. See the note above.
  graduated_seen_ms  INTEGER,

  created_ms         INTEGER,                   -- the launch index's createAt, in ms
  observed_ms        INTEGER NOT NULL,          -- our clock, when this row was refreshed
  first_seen_ms      INTEGER NOT NULL,
  source             TEXT    NOT NULL,

  PRIMARY KEY (chain, token),
  -- One pool per token and one token per pool. A duplicate would mean we had matched a
  -- mint to the wrong curve, which is the one error that corrupts an entire tape rather
  -- than one row of it.
  UNIQUE (chain, pool)
);
CREATE INDEX IF NOT EXISTS idx_stonkfun_created ON stonkfun_launches(chain, created_ms DESC);
CREATE INDEX IF NOT EXISTS idx_stonkfun_quote   ON stonkfun_launches(chain, quote_mint);

-- =====================================================================================
-- 3. token_tape learns a second per-token route.
-- =====================================================================================
--
-- Migration 025 bound `coverage='complete'` to `route='pumpfun:trades'` so that a
-- wallet-walk could never be recorded as a complete tape. That constraint is exactly
-- right and it is kept. What it also did, because pump.fun was the only venue, was bind
-- completeness to one PROVIDER — so a StonkFun tape walked to the end of its history had
-- nowhere honest to be recorded.
--
-- The rule 025 is actually enforcing is "only a PER-TOKEN route may claim completeness",
-- and `raydium:launchlab` is a per-token route in precisely the same sense:
-- `launch-history-v1.raydium.io/trade?poolId=...` walks ONE pool's trades backwards and
-- terminates. So the route vocabulary widens by one value and the completeness CHECK
-- widens to the two per-token routes. `helius:backfill`, `mixed` and `none` remain
-- unable to claim completeness, which is the whole point of the constraint.
--
-- Nothing else changes: the proof / covered_from_ms / created_ms requirements, the
-- indexes and the no-downgrade-on-silence trigger are reproduced verbatim. Existing rows
-- are copied across unaltered, and a `pumpfun:trades` row means exactly what it meant.
--
-- One thing this migration deliberately does NOT do: `tape.TapeRecord.proved` and
-- `tape.complete_tokens` still test `route == 'pumpfun:trades'` in Python. Widening the
-- schema without widening those would be worse than useless, so `kaiba/ingest/stonkfun.py`
-- carries its own read predicate and a test that asserts it is identical to tape.py's
-- with the route substituted. The one-line change in tape.py that retires that duplicate
-- is reported to the lead rather than made here, because tape.py is not this task's file.
CREATE TABLE IF NOT EXISTS token_tape__027 (
  chain            TEXT    NOT NULL,
  token            TEXT    NOT NULL,
  model            TEXT    NOT NULL,
  coverage         TEXT    NOT NULL CHECK (coverage IN ('complete', 'partial', 'unavailable')),
  route            TEXT    NOT NULL CHECK (route IN ('pumpfun:trades', 'raydium:launchlab',
                                                     'helius:backfill', 'mixed', 'none')),
  proof            TEXT,
  reason           TEXT    NOT NULL,
  covered_from_ms  INTEGER,
  covered_to_ms    INTEGER,
  created_ms       INTEGER,
  oldest_ms        INTEGER,
  newest_ms        INTEGER,
  swaps_route      INTEGER,
  swaps_total      INTEGER,
  pages            INTEGER,
  create_tx        TEXT,
  create_tx_basis  TEXT,
  attempts         INTEGER NOT NULL DEFAULT 0,
  last_attempt_ms  INTEGER,
  next_attempt_ms  INTEGER,
  first_seen_ms    INTEGER NOT NULL,
  updated_ms       INTEGER NOT NULL,
  PRIMARY KEY (chain, token),
  -- Note 1 of migration 025, widened to the set of per-token routes. A wallet-walk still
  -- cannot prove completeness.
  CHECK (coverage <> 'complete' OR route IN ('pumpfun:trades', 'raydium:launchlab')),
  CHECK (coverage <> 'complete' OR proof IS NOT NULL),
  CHECK (coverage <> 'complete' OR (covered_from_ms IS NOT NULL
                                    AND created_ms IS NOT NULL
                                    AND covered_from_ms <= created_ms))
);

INSERT INTO token_tape__027
  (chain, token, model, coverage, route, proof, reason, covered_from_ms, covered_to_ms,
   created_ms, oldest_ms, newest_ms, swaps_route, swaps_total, pages, create_tx,
   create_tx_basis, attempts, last_attempt_ms, next_attempt_ms, first_seen_ms, updated_ms)
SELECT
   chain, token, model, coverage, route, proof, reason, covered_from_ms, covered_to_ms,
   created_ms, oldest_ms, newest_ms, swaps_route, swaps_total, pages, create_tx,
   create_tx_basis, attempts, last_attempt_ms, next_attempt_ms, first_seen_ms, updated_ms
FROM token_tape;

DROP TABLE token_tape;
ALTER TABLE token_tape__027 RENAME TO token_tape;

CREATE INDEX IF NOT EXISTS idx_token_tape_coverage ON token_tape(coverage, chain);
CREATE INDEX IF NOT EXISTS idx_token_tape_due      ON token_tape(next_attempt_ms);

-- Reproduced verbatim from migration 025. A proof may be contradicted by evidence that
-- arrived; it may not be erased by evidence that did not. `complete -> partial` stays
-- allowed because that comes from a top-up that found a gap. `complete -> unavailable`
-- is the downgrade-on-silence that cost five irrecoverable proofs on 2026-09-20.
--
-- This matters LESS for StonkFun than for pump.fun and it is kept at full strength
-- anyway. Measured 2026-09-21 over 70 pools stratified by idle time, the LaunchLab trade
-- route answered 200 on 70 of 70, including pools whose last trade was 20,077 minutes
-- (14 days) earlier — there is no hot window here, so a StonkFun proof lost to a 503
-- could in principle be re-earned. "In principle" is not a reason to allow silence to
-- delete evidence.
CREATE TRIGGER IF NOT EXISTS trg_token_tape_no_downgrade_on_silence
BEFORE UPDATE OF coverage ON token_tape
FOR EACH ROW
WHEN old.coverage = 'complete' AND new.coverage = 'unavailable'
BEGIN
  SELECT RAISE(ABORT, 'refusing to downgrade a proved tape to unavailable: a failed observation is not evidence against an earlier one');
END;
