-- Per-token trade tape coverage: *how* we got a token's swaps, and whether they are all of them.
--
-- Why this table exists.
--
--   On 2026-09-20 five separate pieces of work each concluded nothing, and every one of
--   them traced to the same cause: we held 24k swap rows and could not say, for any given
--   mint, whether they were the whole tape or a sliver of it. `swaps.source` records which
--   provider spoke, which is not the same question. The two collection paths we have
--   differ in kind, not in quality:
--
--     `pumpfun:trades`  walks ONE MINT's trade history backwards to its end. When that
--                       walk terminates, what we hold is every trade the mint ever had.
--     `helius:backfill` walks ONE WALLET's history. It deposits rows against whatever
--                       mints that wallet touched. For any of those mints it holds an
--                       arbitrary, unknowable subset — 3,664 rows spread over 796 mints,
--                       ~4.6 each.
--
--   Mixing them is how a confident zero gets published. A wallet-walk can leave exactly
--   one row at a mint's create slot while holding none of the rest of that slot; the
--   bundle share of a one-transaction tape is then 0% *by construction*, on a mint nobody
--   ever looked at. That false zero would have opened `curve-velocity` on 15 tokens that
--   had never been examined, because `max_bundler_pct: 20` fails closed on an unknown and
--   wide open on a zero.
--
-- Four things this schema is shaped to make impossible.
--
--   1. **A wallet-walk cannot be recorded as complete.** The CHECK below binds
--      `coverage='complete'` to `route='pumpfun:trades'`. This is not a convention a
--      future caller can forget: SQLite refuses the row. `helius:backfill` rows are
--      evidence that *a* trade happened, never evidence that we hold them all.
--
--   2. **"Complete" means complete BACK TO LAUNCH, and the proof is stored.** A complete
--      row must carry `covered_from_ms <= created_ms`, both NOT NULL. Holding every trade
--      since we started watching is a different and much weaker fact; it does not get to
--      use the same word. `proof` names the observation that established it — normally
--      `end_of_history`, the pump.fun trade route returning a short page, which is direct
--      evidence the walk terminated rather than being cut off.
--
--   3. **`created_ms` is copied in at proof time.** A proof is only as good as the launch
--      timestamp it was measured against, so `tape.completeness` re-checks the stored
--      `covered_from_ms` against `tokens.created_ms` rather than trusting the row.
--      The test is `covered_from_ms <= tokens.created_ms`, NOT that `created_ms` is
--      unchanged. Exact equality was tried first and was wrong twice over: pump.fun floors
--      `created_timestamp` to the second, so a scan-captured row held ...028000 against a
--      token row's ...029249 and a valid proof vanished over 1,249 ms; and a launch time
--      that moves *later* leaves coverage reaching further back than claimed, which cannot
--      invalidate anything. Only a launch earlier than where coverage starts can.
--
--   4. **Partial is a state, not an absence.** A token we walked halfway is recorded as
--      `partial` with its reason, not left out of the table to look like a token we never
--      tried. Consumers that need completeness must read `partial` exactly as they read a
--      missing row — that is the fail-closed contract — but the job needs the difference
--      to know what to resume, and an operator needs it to know what went wrong.
--
-- On `unavailable` and the backoff columns. The pump.fun trade route serves a hot window:
-- measured on 2026-09-20, a bonding-curve mint idle for ~12 minutes still answered and one
-- idle for ~48 minutes returned 503 `degraded_lanes: ["trade_api.list_trades"]`, while
-- graduated mints answered regardless of idleness. A mint that has fallen out of that
-- window will not come back, so `next_attempt_ms` exists to stop the job re-asking a free
-- endpoint the same dead question every run. It is politeness, and it is also the only
-- thing that makes a long, slow, resumable pass cheaper than a fast one.

CREATE TABLE IF NOT EXISTS token_tape (
  chain            TEXT    NOT NULL,
  token            TEXT    NOT NULL,

  -- Definition version. Bump when the meaning of `coverage` changes, so a later, stricter
  -- rule can find the rows that were proved under the older one instead of inheriting them.
  model            TEXT    NOT NULL,

  -- 'complete'    : every trade this mint has ever had, proved back to its launch.
  -- 'partial'     : we hold some rows. How many of the whole is UNKNOWN. Not a small
  --                 version of complete — an unquantified one.
  -- 'unavailable' : we could not look, or looked and were refused. Read `reason`.
  coverage         TEXT    NOT NULL CHECK (coverage IN ('complete', 'partial', 'unavailable')),

  -- How the collection was MADE, not an inventory of which sources left rows on this
  -- mint -- that is `swaps_route` / `swaps_total`. A per-token walk is 'pumpfun:trades'
  -- whatever it returned, including nothing. 'helius:backfill', 'mixed' and 'none'
  -- describe rows that arrived without any per-token walk, and the CHECK below is what
  -- stops them ever being called complete.
  route            TEXT    NOT NULL CHECK (route IN ('pumpfun:trades', 'helius:backfill', 'mixed', 'none')),

  -- The observation that established completeness. NULL unless coverage='complete'.
  proof            TEXT,
  -- Always populated, including on success. An empty explanation is a bug, not a pass.
  reason           TEXT    NOT NULL,

  -- The oldest moment the tape is known to be GAPLESS down to. NULL = unknown, never
  -- "from the beginning". Mirrors curve_snapshots.coverage_from_ms deliberately.
  covered_from_ms  INTEGER,
  -- The newest moment covered. A later top-up resumes from here and must reach it, or the
  -- row is demoted to 'partial': an unreached watermark is a gap.
  covered_to_ms    INTEGER,

  -- tokens.created_ms as it read when the proof was made. See note 3.
  created_ms       INTEGER,

  oldest_ms        INTEGER,                   -- oldest swap row we hold for this mint
  newest_ms        INTEGER,
  swaps_route      INTEGER,                   -- rows from the per-token route
  swaps_total      INTEGER,                   -- rows from every source
  pages            INTEGER,                   -- provider pages spent on the last attempt

  -- The token's creation transaction, when the launchpad told us its signature, and how we
  -- know. This is what sets swaps.is_create_tx, and it is a fact rather than the usual
  -- inference ("the creator's earliest buy"), which is only true where create and dev-buy
  -- are one transaction.
  create_tx        TEXT,
  create_tx_basis  TEXT,

  attempts         INTEGER NOT NULL DEFAULT 0,
  last_attempt_ms  INTEGER,
  next_attempt_ms  INTEGER,                   -- do not re-ask before this; NULL = eligible

  first_seen_ms    INTEGER NOT NULL,
  updated_ms       INTEGER NOT NULL,

  PRIMARY KEY (chain, token),

  -- Note 1: a wallet-walk can never prove completeness.
  CHECK (coverage <> 'complete' OR route = 'pumpfun:trades'),
  -- Note 2: complete means proved, and proved back to a known launch.
  CHECK (coverage <> 'complete' OR proof IS NOT NULL),
  CHECK (coverage <> 'complete' OR (covered_from_ms IS NOT NULL
                                    AND created_ms IS NOT NULL
                                    AND covered_from_ms <= created_ms))
);

CREATE INDEX IF NOT EXISTS idx_token_tape_coverage ON token_tape(coverage, chain);
CREATE INDEX IF NOT EXISTS idx_token_tape_due      ON token_tape(next_attempt_ms);

-- A proof may be contradicted. It may not be forgotten.
--
-- The CHECK constraints above make a *false* completeness claim unstorable, which turned
-- out to be only half the problem. They said nothing about replacing a *true* one, and on
-- 2026-09-20 that gap cost real data: five proved-complete rows were re-scanned 8-10
-- minutes after capture, hit the hot-window 503, and had `coverage='unavailable'` written
-- straight over the proof. `complete_tokens` fell from 12 to 7 in two minutes. Because the
-- tape is a hot-window resource, none of those proofs could be re-earned.
--
-- A 503 is an absence of evidence. It says nothing whatsoever about what we saw while the
-- endpoint was answering, so it must not be able to erase it. `complete -> partial` stays
-- allowed, because that transition comes from evidence that actually arrived and
-- contradicts completeness — a top-up that found more trades than it could bridge. It is
-- specifically `complete -> unavailable`, the downgrade-on-silence, that is forbidden.
--
-- The application enforces this too (`tape.record_failed_attempt`), and after that fix
-- this trigger should never fire. It is here because the data it protects cannot be
-- recovered if a future caller forgets, and a loud failure is much better than a silent
-- deletion of the only copy.
CREATE TRIGGER IF NOT EXISTS trg_token_tape_no_downgrade_on_silence
BEFORE UPDATE OF coverage ON token_tape
FOR EACH ROW
WHEN old.coverage = 'complete' AND new.coverage = 'unavailable'
BEGIN
  SELECT RAISE(ABORT, 'refusing to downgrade a proved tape to unavailable: a failed observation is not evidence against an earlier one');
END;
