"""Trade-tape coverage: which mints we hold every trade for, and how we know.

On 2026-09-20 five separate pieces of analysis each concluded nothing, and all five traced
to one cause. We held 435 known tokens and a per-token trade tape for a handful of them;
everything else had either no swap rows at all or a scattering deposited by the *wallet*
backfill, which walks a wallet's history and leaves an arbitrary subset of any given mint's
trades behind. Bundle share was measurable on 10 of 435. `confluence-5` had never fired.
`curve-velocity` scored 40 tokens and pinned 33 of them at the floor.

This module does two things, and the second matters more than the first.

**It pulls the tape.** :func:`run` walks the tokens we know about through
``kaiba.ingest.token_flow.collect_trades`` — the same fetch, the same parser, the same
pagination that the scanner already uses, deliberately not a second copy of any of it —
and records what it got.

**It makes completeness provable.** ``swaps.source`` says which provider spoke. It does not
say whether what we hold is all of it, and those are different questions with different
answers per mint. The distinction is not academic: another agent accepted *"our earliest
swap row sits at the launch"* as proof of coverage, which is true of a complete tape and
also true of a wallet-walk that happens to hold one row at the create slot and nothing else
from that slot. The bundle share of a one-transaction tape is 0% by construction — a
confident zero on a mint nobody looked at — and it would have opened `curve-velocity` on 15
tokens that had never been examined, because a `max_bundler_pct` gate fails closed on an
unknown and wide open on a zero.

So :func:`is_complete` is the only question a consumer should ask, it is answered from
``token_tape`` rather than inferred from ``swaps``, and it says no unless all of the
following were recorded together:

* the rows came from a **per-token route** (`pumpfun:trades`), never a wallet-walk —
  migration 025 enforces this with a CHECK, so it is not a convention anyone can forget;
* the walk **terminated** (the trade route returned a short page: `end_of_history`), which
  is direct evidence rather than a timestamp heuristic;
* it reached back **past the token's creation**;
* and the ``created_ms`` it was proved against still matches the one on the token today.

A partial tape is therefore indistinguishable from no tape to anything that needs
completeness. That is the whole design. `partial` exists so the job knows what to resume
and an operator knows what went wrong, not so a consumer can take a chance on it.

Two data defects are repaired here as well, and they have very different outcomes.

``swaps.is_create_tx`` **is now a fact, not an inference.** Consumers had been falling back
to "the creator's earliest buy", which happens to work on pump.fun because create and
dev-buy are one transaction and is a guess anywhere else. It does not need to be a guess:
``tokens.meta_json`` carries the launchpad's own ``signature`` for the creation transaction
on 425 of 435 tokens, so :func:`repair_create_flags` marks the row whose ``tx`` equals it
and marks nothing otherwise. Where a creator made no initial buy there is no create swap to
mark, and the absence is correct rather than missing.

``swaps.fee_payer`` **cannot be filled from this source, and the honest answer is to say
so.** The pump.fun trade record carries ``trader.address`` and no signer: the full field set
is ordinalKey, blockId, txIndex, eventIndex, blockTimeMs, txId, legIndex, late, isBackfill,
side, kind, venue, pool, trader, baseAmount, quoteAmount, quote, priceUsd, priceQuote,
quotePriceUsd, valueUsd, valueNative — verified against live pages from five mints and two
recorded fixtures on 2026-09-20. Assuming the trader is the fee payer would be wrong in
exactly the case entity resolution cares about, because a bundler pays for wallets that are
not it, so the guess would manufacture the clusters it was meant to discover.
:func:`repair_fee_payer` therefore only copies a fee payer across when some *other* row for
the same transaction already carries one — a verifiable identity, not an inference — and
:data:`FEE_PAYER_NOTE` records what would actually fix it: the signature is on every row,
and account index 0 of the transaction message is the fee payer, so one RPC
``getTransaction`` per distinct signature resolves it exactly.

## Complete is not the same as deep, and we spent a month proving it

Measured on ``data/kaiba.db`` on 2026-09-21: **4,071 tapes marked complete, median span
41 seconds, 17 reaching five minutes.** A published result (arXiv 2608.20271, Solana, 6.4M
tokens) says rug pulls are predictable from the **first five minutes** of trading
microstructure with no contract features at all, and 35% of our closed paper trades exited
to the rug monitor. Seventeen samples cannot test that.

Three separate mechanisms produced that shape, and none of them was the market:

* :func:`candidates` **structurally excluded every proved-complete tape**, so a tape could
  be finished and never improved. A tier-1 capture necessarily stops seconds after launch
  — the trades that make the first five minutes have not happened yet — so "complete" was
  being earned at a depth of about forty seconds and then frozen there by the predicate
  that was supposed to protect it.
* **Nothing advanced the watermark.** 4,043 of 4,071 rows had ``updated_ms ==
  first_seen_ms``: written once, never touched. ``kaiba.ops.scheduler``'s ``job_token_flow``
  had gone on writing trades straight into ``swaps`` without telling this table, so
  ``newest_ms`` sat a median of 283 s behind reality on 1,205 rows and nothing could
  distinguish a stale tape from a fresh one. See :func:`reconcile`.
* **Depth was being scored as last-trade-minus-first-trade.** A mint that traded for forty
  seconds and went quiet scores 40 s by that rule forever, however many times we look —
  and a five-minute silence after a forty-second burst is the loudest microstructure
  signal there is. Coverage depth is ``covered_to_ms - created_ms``, and
  :func:`observed_to_ms` is what lets a terminated walk advance ``covered_to_ms`` to the
  moment it looked rather than to the last row it happened to find.

The fix is one scheduled return visit per mint, timed by :func:`next_deepen_attempt` to
land just after the five-minute mark so a single page spans the gap, and ordered by
:func:`candidates` so perishable tapes are served before merely-new ones. **A mint 70
minutes old with a 40-second tape is worth more than an unseen launch**, because the first
becomes unreachable forever inside the hot window and the second will not.

And the guard that matters more than the gain: **a tape that grows at the END must stay
honest about its START.** ``covered_from_ms``, ``proof`` and ``coverage`` are never
written by any of the deepening paths. :func:`reconcile` only ever moves
``covered_to_ms``; :func:`collect_token` refuses to write ``partial`` over a standing
proof unless it was actually topping up and actually failed to reach the watermark, which
is the only observation that constitutes evidence of a hole. The bundle detector and the
confluence scorer read the start of the tape, and nothing here can move it.

Pacing. This job does not set its own rate and must not: it goes through
``kaiba.core.limiter`` like everything else, and whatever ``limits_for("pumpfun")`` returns
is the budget. Verified by running it on 2026-09-21: ``Limits(min_interval_ms=1000,
capacity=10, refill_per_s=1.0, max_inflight=2)`` — the module default, because
``config/risk.yaml`` has **no** ``pumpfun`` entry under ``provider_budgets``. That is worth
stating plainly rather than assuming, because a tighter budget (1400 ms / capacity 5 /
0.7 per s / 1 inflight) was described as having been applied after we were rate-limited off
the host earlier that day, and it is not in the tree. Whichever lands, nothing in this file
needs to change and nothing in this file may raise it; the deepening work above is
deliberately *fewer, deeper* tapes rather than more requests, and :class:`RunReport`
measures the rate actually sustained from the limiter's own ledger so the claim stays
checkable.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, get_conn
from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, EventKind, now_ms
from kaiba.ingest.token_flow import (
    PROVIDER as TRADE_PROVIDER,
)
from kaiba.ingest.token_flow import (
    SOURCE as TRADE_SOURCE,
)
from kaiba.ingest.token_flow import (
    FlowConfig,
    FlowResult,
    collect_trades,
)

log = logging.getLogger(__name__)

#: Definition version for ``token_tape.model``. Bump when the meaning of ``coverage``
#: changes, so a stricter later rule can find rows proved under the looser earlier one
#: instead of silently inheriting them.
MODEL_ID = "tape/v1"

#: The wallet-walk source. Present for contrast: rows carrying it can never make a tape
#: complete, and migration 025 refuses to store a row that claims otherwise.
WALLET_SOURCE = "helius:backfill"

COMPLETE = "complete"
PARTIAL = "partial"
UNAVAILABLE = "unavailable"

ROUTE_TRADES = "pumpfun:trades"

#: StonkFun's tape, served by Raydium LaunchLab's index. A second *per-token* route, so it
#: proves completeness on the same terms as pump.fun's -- unlike `WALLET_SOURCE`, which
#: walks a wallet and can leave one row in a slot while holding none of the rest of it.
#:
#: It differs in one way that matters operationally: it has no hot window. 70 of 70 pools
#: answered after up to 14 days idle, where pump.fun 503s permanently after a few hours.
#: So StonkFun coverage is backfillable and pump.fun coverage is not.
ROUTE_LAUNCHLAB = "raydium:launchlab"

#: Every route that walks one token's own trades. A route outside this set cannot prove
#: completeness, whatever its coverage says.
PER_TOKEN_ROUTES = (ROUTE_TRADES, ROUTE_LAUNCHLAB)

#: Launchpads the pump.fun trade route cannot serve, excluded from `candidates`. Kept as
#: data rather than a literal in the query so adding a third venue is one edit here.
_EXCLUDED_LAUNCHPADS: tuple[str, ...] = ("stonkfun",)
_EXCLUDED_LAUNCHPADS_SQL = ",".join("?" for _ in _EXCLUDED_LAUNCHPADS)
ROUTE_WALLET = "helius:backfill"
ROUTE_MIXED = "mixed"
ROUTE_NONE = "none"

#: ``collect_trades`` reason strings this module interprets. ``end_of_history`` is the only
#: one that means the walk *terminated*; everything else means it stopped for a reason of
#: ours (a budget, a watermark) or of the provider's. Treating ``reached_watermark`` as
#: proof of completeness is the mistake this constant exists to make visible.
REASON_END_OF_HISTORY = "end_of_history"
REASON_WATERMARK = "reached_watermark"
REASON_UNAVAILABLE = "unavailable"

#: ``collect_trades`` reasons that mean **we could not look**, as opposed to looking and
#: seeing something that contradicts an earlier proof.
#:
#: This distinction had to be learned the hard way and it cost real data. A proved-complete
#: tape was re-scanned 8-10 minutes later, hit the hot-window 503, and the refusal was
#: written over the proof as ``coverage='unavailable'``. ``complete_tokens`` went from 12
#: to 7 inside two minutes. Because the tape is a hot-window resource the proof could not
#: then be re-earned, and because ``seed_from_snapshots`` used to skip any token that held
#: a record, the still-valid ``curve_snapshots`` evidence could not be re-adopted either.
#: The hot window had turned from a collection limit into a data-destruction mechanism.
#:
#: **A failed observation is not an observation.** A 503 says nothing whatsoever about what
#: we already saw while the endpoint was answering, so it must never weaken a claim. Only
#: evidence that actually arrived and contradicts completeness — a top-up that found more
#: trades than it could bridge, a walk that ran out of budget with pages in hand — is
#: allowed to demote a record.
NON_OBSERVATIONS: frozenset[str] = frozenset(
    {
        "unavailable",
        "partial_provider_error",
        "malformed_page",
        "no_usable_cursor",
        "not_attempted",
    }
)

#: **The hot window.** The pump.fun trade route does not serve a mint's tape indefinitely.
#: Measured on 2026-09-20 against ``frontend-api-v3.pump.fun/trades/{chain_id}/{mint}``:
#:
#: ===========================================  ==================================
#: mint state                                   result
#: ===========================================  ==================================
#: bonding curve, last trade 0.0-12.1 min ago   HTTP 200, 11 of 11 mints
#: bonding curve, last trade 48.3-169.7 min      HTTP 503, 37 of 37 mints
#: graduated (``complete: true``)                HTTP 200 regardless of idleness
#: ===========================================  ==================================
#:
#: The 503 body is ``{"statusCode":503,...,"degraded_lanes":["trade_api.list_trades"]}``.
#: It is **per mint and persistent**, not transient load: one mint answered 503 on 6 of 6
#: controlled retries in the same minute another answered 200 on 4 of 4, alternating. It is
#: also specific to the trade lane — ``/coins/{mint}`` returns 200 for the same mints,
#: carrying reserves, creator and ``created_timestamp``, so the mint is known and only its
#: tape is gone.
#:
#: The boundary is bracketed, not pinned: somewhere between 12 and 48 minutes of trade
#: inactivity. Nothing here depends on the exact value, and no code should start depending
#: on one — the safe reading is that a mint's tape is obtainable *while it is trading* and
#: unobtainable an hour after it stops.
#:
#: **What this means for the system.** Tape coverage is a going-forward property. It cannot
#: be backfilled: a pass over the 435 tokens known on 2026-09-20 attempted 78 of them and
#: recovered 2, because the rest had been idle for hours. Every launch we do not capture
#: while it is hot is a permanent hole, which is why the capture belongs at tier-1 scan
#: time (``kaiba.execution.scanner``) and not in a nightly job.
HOT_WINDOW_ANSWERED_MAX_IDLE_MIN = 12.1
HOT_WINDOW_REFUSED_MIN_IDLE_MIN = 48.3

#: Re-measured 2026-09-21 on quiet mints: 6 of 6 returned HTTP 503 at 81-162 minutes since
#: their last trade. It does not narrow the 12.1/48.3 bracket above, it confirms the far
#: side of it, and it is recorded separately rather than folded in because averaging two
#: independent measurements into one number is how a bracket turns into a false precision.
HOT_WINDOW_REFUSED_2026_09_21_MIN_IDLE_MIN = 81.0

#: The depth buckets :func:`depth_histogram` reports, in milliseconds past launch. Not
#: thresholds — nothing branches on them. 300 s is the one that matters (see
#: :data:`TapeConfig.deep_enough_ms`); 60 s and 120 s are there so a pass that moved the
#: population without yet clearing the bar still shows it moved.
DEPTH_BUCKETS_MS: tuple[int, ...] = (60_000, 120_000, 300_000)

#: What would actually populate ``fee_payer``. Stated as data rather than prose in a
#: docstring so an operator report can quote it verbatim.
FEE_PAYER_NOTE = (
    "The pump.fun trade record has no signer field; it reports trader.address only. The fee "
    "payer is account index 0 of the transaction message, so one Solana RPC getTransaction "
    "(or Helius equivalent) per DISTINCT signature resolves it exactly. Cost scales with "
    "distinct signatures, not swap rows. Deriving it from trader.address would be wrong in "
    "precisely the bundled case entity resolution exists to detect."
)


# --------------------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Knob:
    """Where one tunable came from. ``basis`` is the only field worth arguing about.

    * ``MEASURED``    — a number this repository observed, with the observation quoted.
    * ``PUBLISHED``   — a number from a cited paper, used as-is rather than re-derived.
    * ``REQUIREMENT`` — not a tuning choice at all: the shape of the question we are
      trying to answer. Changing it changes what is being measured, not how well.
    * ``INVENTED``    — someone picked it. Says so, and says what would settle it.
    """

    basis: str
    evidence: str


#: Provenance for every field of :class:`TapeConfig`, keyed by field name.
#:
#: ``tests/test_tape.py::test_every_config_knob_has_provenance`` asserts this covers the
#: dataclass exactly, in both directions, so a knob cannot be added without saying where
#: its value came from and a stale entry cannot outlive the field it describes. The test
#: is the point: a threshold with no provenance is a number someone will later defend as
#: if it had been measured.
PROVENANCE: dict[str, Knob] = {
    "walk_pages": Knob(
        "MEASURED",
        "A median graduating pump.fun token sees ~457 trades at 100 per page, so 8 pages "
        "covers one end to end and 24 covers a mint an order of magnitude busier.",
    ),
    "topup_pages": Knob(
        "MEASURED",
        "Re-measured 2026-09-21 on data/kaiba.db: across the 695 proved tapes whose trades "
        "reach 300 s past launch, the rows lying between the stored watermark and "
        "launch+300 s number 1 at p50, 36 at p90 and 200 at the maximum -- 2 pages. Zero "
        "of 695 need more than 3.",
    ),
    "budget_s": Knob(
        "INVENTED",
        "A wall-clock stop so a resumable job cannot run away. Nothing depends on the "
        "value; the pass continues on the next run.",
    ),
    "max_tokens": Knob(
        "INVENTED", "Belt and braces with budget_s. Same reasoning: the job is resumable."
    ),
    "pause_between_tokens_s": Knob(
        "INVENTED",
        "Deliberately 0: the limiter paces requests, and a second sleep on top of it "
        "would make the measured request rate a statement about this constant rather "
        "than about the provider budget.",
    ),
    "backoff_s": Knob(
        "MEASURED",
        "Motivated by the hot window: a mint idle ~48 min returned 503 on 37 of 37, so a "
        "mint that has aged out will not come back and re-asking is noise on someone "
        "else's infrastructure. The ladder itself is a choice; the shape is not.",
    ),
    "max_attempts": Knob("INVENTED", "Retirement point. Nothing measured says 5."),
    "partial_retry_s": Knob(
        "MEASURED",
        "90 s sits inside the 12.1-minute floor of the measured hot window: a partial tape "
        "belongs to a mint that just answered, so finishing it is urgent in a way that "
        "retrying a refusal never is.",
    ),
    "pumpfun_decimals": Knob(
        "MEASURED", "6 on every pump.fun payload observed. Applied only where the launchpad says so."
    ),
    "pumpfun_launchpads": Knob(
        "MEASURED", "The launchpad strings actually seen in tokens.launchpad for pump.fun."
    ),
    "deep_enough_ms": Knob(
        "REQUIREMENT",
        "arXiv 2608.20271 (Solana, 6.4M tokens) reports rug pulls predictable from the "
        "FIRST FIVE MINUTES of trading microstructure with no contract features. 300 s is "
        "therefore the horizon of the model we intend to fit, not a tuning choice: lower "
        "it and we are no longer testing that finding.",
    ),
    "deepen_settle_ms": Knob(
        "INVENTED",
        "Slack after launch+deep_enough_ms before the deepening top-up is offered, so the "
        "window has provably elapsed rather than nearly elapsed. Sized against a "
        "measurement: at p90 a token sees 159 trades in its first 300 s (0.53/s), so 30 s "
        "adds ~16 rows -- comfortably inside the single page the top-up already costs. "
        "What would settle it is measuring how often a top-up lands short of 300 s.",
    ),
    "deepen_max_idle_ms": Knob(
        "MEASURED",
        "The PROVED-ANSWERING edge of the hot window, HOT_WINDOW_ANSWERED_MAX_IDLE_MIN "
        "(11 of 11 mints answered at 0.0-12.1 min idle), not the 12.1-48.3 bracket's "
        "middle and not the refused edge. A first capture is worth attempting on a "
        "maybe, because there is no second chance; a deepening re-ask is not, because a "
        "503 buys nothing and still spends a request on the provider we were rate-limited "
        "off on 2026-09-21.",
    ),
}


@dataclass(frozen=True, slots=True)
class TapeConfig:
    """Every number is annotated with where it came from -- see :data:`PROVENANCE`."""

    #: Pages one full walk may spend. MEASURED against the population: a median graduating
    #: pump.fun token sees ~457 trades, so 8 pages covers one end to end, and 24 covers a
    #: mint an order of magnitude busier. Above this the token is recorded ``partial`` and
    #: retried later rather than allowed to eat the whole pass.
    walk_pages: int = 24

    #: Pages a top-up of an already-complete tape may spend before the unreached watermark
    #: has to be called a gap. Three pages is 300 trades of new activity between runs; a
    #: mint busier than that gets demoted to ``partial`` and re-walked, which is the
    #: fail-closed answer.
    topup_pages: int = 3

    #: Wall-clock budget for one :func:`run`. The job is resumable, so a short budget run
    #: often is strictly better than a long one run rarely.
    budget_s: float = 600.0

    #: Tokens one :func:`run` may attempt. Belt and braces with ``budget_s``.
    max_tokens: int = 500

    #: Extra pause between tokens, on top of the limiter's own 1 req/s floor. INVENTED,
    #: and deliberately non-zero: the limiter paces requests, not logical operations, and
    #: a generous free endpoint is worth being visibly gentle with.
    pause_between_tokens_s: float = 0.0

    #: Backoff after an ``unavailable`` attempt, in seconds, by attempt count. MEASURED
    #: motivation: the trade route serves a hot window (a bonding-curve mint idle ~12 min
    #: still answered, one idle ~48 min returned 503), so a mint that has aged out will not
    #: come back and re-asking is pure noise on someone else's infrastructure. The last
    #: entry repeats for every further attempt.
    backoff_s: tuple[int, ...] = (600, 2_400, 9_600, 86_400)

    #: Attempts after which a token stops being offered at all by :func:`candidates`
    #: unless the caller asks for retired rows. INVENTED.
    max_attempts: int = 5

    #: Retry delay for a *partial* tape, as opposed to an unavailable one. Deliberately far
    #: shorter than the first entry of :attr:`backoff_s`, and the reason is the hot window
    #: above: a partial tape belongs to a mint that answered, so it is still inside the
    #: window *now* and will not be in an hour. Finishing it is urgent in a way that
    #: retrying a refusal never is. 90 s is comfortably inside the 12-minute floor of the
    #: measured window while leaving the scanner's own traffic room to breathe.
    partial_retry_s: int = 90

    #: pump.fun mints are 6 decimals on every payload observed. Applied only when the
    #: launchpad says pump.fun; a mint from anywhere else with unknown decimals is refused
    #: without spending a request, because a wrong exponent is a silent factor-of-1000
    #: error in every ``amount_token`` and it looks entirely plausible.
    pumpfun_decimals: int = 6

    #: Launchpad strings for which :attr:`pumpfun_decimals` may be assumed.
    pumpfun_launchpads: frozenset[str] = frozenset({"pump.fun", "pumpfun", "pump"})

    #: How far past launch a tape must provably reach before it is deep enough to model.
    #: REQUIREMENT, not a knob -- see :data:`PROVENANCE`. Five minutes is the horizon the
    #: published rug-pull result is stated over, so a shorter tape does not test it.
    deep_enough_ms: int = 300_000

    #: Slack added to ``created_ms + deep_enough_ms`` before a shallow-but-complete tape is
    #: re-offered for deepening. One return visit, timed so a single page spans the gap.
    deepen_settle_ms: int = 30_000

    #: Longest a mint may have been idle and still be re-asked *for deepening*. The proved
    #: answering edge of the hot window, deliberately not the bracket. See
    #: :data:`PROVENANCE` for why a deepening re-ask gets a stricter rule than a capture.
    deepen_max_idle_ms: int = int(HOT_WINDOW_ANSWERED_MAX_IDLE_MIN * 60_000)

    def backoff_for(self, attempts: int) -> int:
        idx = max(0, min(attempts, len(self.backoff_s)) - 1)
        return self.backoff_s[idx]

    def flow_config(self, base: FlowConfig | None = None) -> FlowConfig:
        """A :class:`FlowConfig` with this job's page budget, everything else untouched.

        The point of going through ``token_flow``'s own config object rather than passing
        loose arguments is that the endpoint, the pacing, the retry count and the cursor
        validation stay defined in exactly one place.
        """
        src = base if base is not None else FlowConfig()
        return FlowConfig(
            page_limit=src.page_limit,
            max_pages=self.walk_pages,
            scan_max_pages=src.scan_max_pages,
            wait_for_slot_s=src.wait_for_slot_s,
            timeout_s=src.timeout_s,
            retries=src.retries,
            ttl_s=src.ttl_s,
            min_trades_for_rate=src.min_trades_for_rate,
            min_interval_s=src.min_interval_s,
            coverage_grace_s=src.coverage_grace_s,
            flow_ratio_min=src.flow_ratio_min,
            flow_ratio_max=src.flow_ratio_max,
            flow_check_floor_lamports=src.flow_check_floor_lamports,
            snapshot_keep_per_token=src.snapshot_keep_per_token,
            snapshot_max_age_s=src.snapshot_max_age_s,
            prune_every=src.prune_every,
        )


DEFAULT_CONFIG = TapeConfig()


# --------------------------------------------------------------------------------------
# parsed shapes
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TapeRecord:
    """One ``token_tape`` row.

    ``proved`` is the only property a consumer needing completeness should read, and it
    re-checks the invariants rather than trusting ``coverage`` alone. The database already
    enforces them, but this class is also constructed in memory before any write, and a
    record that has not been stored yet has had nothing checked.
    """

    chain: Chain
    token: str
    coverage: str
    route: str
    reason: str
    model: str = MODEL_ID
    proof: str | None = None
    covered_from_ms: int | None = None
    covered_to_ms: int | None = None
    created_ms: int | None = None
    oldest_ms: int | None = None
    newest_ms: int | None = None
    swaps_route: int | None = None
    swaps_total: int | None = None
    pages: int | None = None
    create_tx: str | None = None
    create_tx_basis: str | None = None
    attempts: int = 0
    last_attempt_ms: int | None = None
    next_attempt_ms: int | None = None

    @property
    def proved(self) -> bool:
        """Complete, by a per-token route, provably back to a known launch."""
        return (
            self.coverage == COMPLETE
            and self.route in PER_TOKEN_ROUTES
            and self.proof is not None
            and self.covered_from_ms is not None
            and self.created_ms is not None
            and self.covered_from_ms <= self.created_ms
        )

    @property
    def depth_ms(self) -> int | None:
        """How far past launch the coverage claim reaches. ``None`` when we cannot say.

        This is *coverage* depth, not the gap between the first and last trade we hold,
        and the difference is the whole reason the number is worth having. A mint that
        traded three times in its first ten seconds and then went silent has a five-minute
        tape once we have asked at launch+300 s and been told there is nothing newer --
        five minutes of proven silence is a microstructure observation, arguably the
        loudest one there is. Measuring last-trade-minus-first-trade would score that mint
        at ten seconds and quietly drop exactly the population the rug model is about.

        ``None`` rather than 0 on a missing input, per the evidence rules: an unproved
        record, an unknown launch or an unknown watermark is a depth we have not
        established, and it must not sort or compare as a shallow one.
        """
        if not self.proved or self.created_ms is None or self.covered_to_ms is None:
            return None
        return self.covered_to_ms - self.created_ms

    def deep_enough(self, config: TapeConfig | None = None) -> bool:
        """Does the coverage claim already span the modelling horizon? Fails closed."""
        cfg = config if config is not None else DEFAULT_CONFIG
        depth = self.depth_ms
        return depth is not None and depth >= cfg.deep_enough_ms

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain": self.chain.value,
            "token": self.token,
            "coverage": self.coverage,
            "route": self.route,
            "proof": self.proof,
            "reason": self.reason,
            "covered_from_ms": self.covered_from_ms,
            "covered_to_ms": self.covered_to_ms,
            "created_ms": self.created_ms,
            "swaps_route": self.swaps_route,
            "swaps_total": self.swaps_total,
            "pages": self.pages,
            "create_tx": self.create_tx,
            "proved": self.proved,
            "depth_ms": self.depth_ms,
        }


@dataclass(slots=True)
class RunReport:
    """What one :func:`run` did, in numbers an operator can check against the database."""

    attempted: int = 0
    completed: int = 0
    partial: int = 0
    unavailable: int = 0
    skipped: int = 0
    rows_written: int = 0
    pages: int = 0
    requests: int = 0
    rate_limited: int = 0
    create_flags_set: int = 0
    fee_payers_set: int = 0
    elapsed_s: float = 0.0
    complete_before: int = 0
    complete_after: int = 0
    #: Tokens whose coverage claim reached each of :data:`DEPTH_BUCKETS_MS`, before and
    #: after the pass. Completeness counts alone cannot show a deepening pass working:
    #: every token here was already `complete` when the pass started.
    depth_before: dict[str, int] = field(default_factory=dict)
    depth_after: dict[str, int] = field(default_factory=dict)
    #: Tapes that were already proved complete and were re-offered to be made deeper.
    deepen_attempted: int = 0
    #: ...of which came back with a strictly later watermark.
    deepen_advanced: int = 0
    #: Watermarks advanced with no provider call at all, from curve_snapshots evidence.
    reconciled: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def page_rate(self) -> float:
        """Pages of trade data obtained per second."""
        return (self.pages / self.elapsed_s) if self.elapsed_s > 0 else 0.0

    @property
    def request_rate(self) -> float:
        """Requests actually put on the wire per second, including the ones that failed.

        Counted from ``provider_calls``, the limiter's own ledger, rather than from pages
        obtained. The two differ by a lot and only one of them is what the endpoint feels:
        a mint outside the route's hot window costs its full retry budget and yields no
        page at all, so a pass over dead mints has a page rate near zero and a request rate
        near the ceiling. Reporting the page rate as "the rate we sustain" would understate
        our load on a free endpoint by roughly 3x, which is exactly the number not to get
        wrong when the whole plan rests on not being the reason it stops.
        """
        return (self.requests / self.elapsed_s) if self.elapsed_s > 0 else 0.0

    def note(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "completed": self.completed,
            "partial": self.partial,
            "unavailable": self.unavailable,
            "skipped": self.skipped,
            "rows_written": self.rows_written,
            "pages": self.pages,
            "requests": self.requests,
            "rate_limited": self.rate_limited,
            "create_flags_set": self.create_flags_set,
            "fee_payers_set": self.fee_payers_set,
            "elapsed_s": round(self.elapsed_s, 2),
            "request_rate_per_s": round(self.request_rate, 3),
            "page_rate_per_s": round(self.page_rate, 3),
            "complete_before": self.complete_before,
            "complete_after": self.complete_after,
            "depth_before": dict(self.depth_before),
            "depth_after": dict(self.depth_after),
            "deepen_attempted": self.deepen_attempted,
            "deepen_advanced": self.deepen_advanced,
            "reconciled": self.reconciled,
            "reasons": dict(sorted(self.reasons.items(), key=lambda kv: -kv[1])),
        }


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _conn(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    return conn if conn is not None else get_conn()


def _meta(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def create_signature(meta_json: Any) -> str | None:
    """The launchpad's own signature for the creation transaction, if it told us one.

    pump.fun's create event carries it and ``kaiba.ingest`` stores the event verbatim in
    ``tokens.meta_json``. This is the difference between knowing which transaction created
    a token and inferring it from whoever bought first.
    """
    sig = _meta(meta_json).get("signature")
    if not isinstance(sig, str):
        return None
    sig = sig.strip()
    # Solana signatures are base58 of 64 bytes: 86-88 characters. Anything else in this
    # field is not a signature and must not be matched against swaps.tx.
    return sig if 80 <= len(sig) <= 90 else None


# --------------------------------------------------------------------------------------
# reading coverage — the only questions a consumer should ask
# --------------------------------------------------------------------------------------


def record_of(chain: Chain, token: str, conn: sqlite3.Connection | None = None) -> TapeRecord | None:
    """The stored coverage record for a mint, or ``None`` if we have never assessed it."""
    c = _conn(conn)
    try:
        row = fetch_one(c, "SELECT * FROM token_tape WHERE chain=? AND token=?", (chain.value, token))
    except sqlite3.Error as exc:
        log.warning("tape: coverage read failed for %s (%s)", token[:12], exc)
        return None
    if row is None:
        return None
    return TapeRecord(
        chain=chain,
        token=token,
        coverage=str(row["coverage"]),
        route=str(row["route"]),
        reason=str(row["reason"]),
        model=str(row["model"]),
        proof=row["proof"],
        covered_from_ms=_int(row["covered_from_ms"]),
        covered_to_ms=_int(row["covered_to_ms"]),
        created_ms=_int(row["created_ms"]),
        oldest_ms=_int(row["oldest_ms"]),
        newest_ms=_int(row["newest_ms"]),
        swaps_route=_int(row["swaps_route"]),
        swaps_total=_int(row["swaps_total"]),
        pages=_int(row["pages"]),
        create_tx=row["create_tx"],
        create_tx_basis=row["create_tx_basis"],
        attempts=_int(row["attempts"]) or 0,
        last_attempt_ms=_int(row["last_attempt_ms"]),
        next_attempt_ms=_int(row["next_attempt_ms"]),
    )


def completeness(
    chain: Chain, token: str, conn: sqlite3.Connection | None = None
) -> tuple[bool, str]:
    """``(complete, reason)``. The reason is populated on success as well as on refusal.

    Fails closed on every path: no record, a partial record, a wallet-walk record, a proof
    made against a ``created_ms`` that has since changed, or a database that will not
    answer all return ``False``. There is deliberately no argument that relaxes this.
    """
    c = _conn(conn)
    rec = record_of(chain, token, c)
    if rec is None:
        return False, "no_tape_record"
    if not rec.proved:
        return False, f"{rec.coverage}:{rec.reason}"

    # Note 3 of migration 025: a proof is only as good as the launch time it was measured
    # against, so it is re-checked against the token's current one rather than trusted.
    #
    # The test is ``covered_from_ms <= created_ms``, not ``created_ms`` being unchanged.
    # Exact equality was the first attempt and it was wrong in both directions. It refused
    # a perfectly good proof over a sub-second difference — pump.fun floors
    # ``created_timestamp`` to the second, so a scan-captured row held ...028000 against a
    # token row's ...029249 and a valid proof silently evaporated over 1,249 ms. And it
    # was not testing the thing that matters: a launch time that moved *later* leaves our
    # coverage reaching even further back than claimed, which cannot invalidate anything.
    # Only a launch time that moved *earlier than where our coverage starts* can, and that
    # is what this compares.
    try:
        row = fetch_one(
            c, "SELECT created_ms FROM tokens WHERE chain=? AND address=?", (chain.value, token)
        )
    except sqlite3.Error as exc:
        log.warning("tape: token read failed for %s (%s)", token[:12], exc)
        return False, "tokens_unreadable"
    current = _int(row["created_ms"]) if row else None
    if current is None:
        return False, "creation_time_unknown"
    if rec.covered_from_ms is None or rec.covered_from_ms > current:
        # Keep the precise reason for tape callers and the historic marker consumed by
        # confluence/reporting integrations. Both describe the same fail-closed condition:
        # the launch moved earlier than the coverage proof.
        return False, (
            f"launch_moved_before_coverage:{rec.covered_from_ms}>{current};created_ms_moved"
        )
    return True, rec.proof or "complete"


def is_complete(chain: Chain, token: str, conn: sqlite3.Connection | None = None) -> bool:
    """Do we hold every trade this mint has ever had? The gate for anything that needs it."""
    return completeness(chain, token, conn)[0]


def complete_tokens(
    chain: Chain = Chain.SOL, conn: sqlite3.Connection | None = None
) -> list[str]:
    """Every mint whose tape is proved complete back to launch, re-checked against ``tokens``."""
    c = _conn(conn)
    try:
        rows = fetch_all(
            c,
            "SELECT tt.token AS token FROM token_tape tt "
            "JOIN tokens tk ON tk.chain = tt.chain AND tk.address = tt.token "
            "WHERE tt.chain=? AND tt.coverage=? AND tt.route=? AND tt.proof IS NOT NULL "
            "  AND tt.covered_from_ms IS NOT NULL AND tk.created_ms IS NOT NULL "
            "  AND tt.covered_from_ms <= tk.created_ms "
            "ORDER BY tt.token",
            (chain.value, COMPLETE, ROUTE_TRADES),
        )
    except sqlite3.Error as exc:
        log.warning("tape: complete-token listing failed (%s)", exc)
        return []
    return [str(r["token"]) for r in rows]


#: The one predicate that says a coverage claim is real, as SQL, so the reader and the
#: candidate query cannot drift apart. It is `TapeRecord.proved` plus the re-check against
#: the token's current launch time that `completeness` performs, and it is a format string
#: only in the table aliases -- nothing interpolates a value.
_PROVED_SQL = (
    "({tt}.coverage = 'complete' AND {tt}.route = 'pumpfun:trades' "
    " AND {tt}.proof IS NOT NULL AND {tt}.covered_from_ms IS NOT NULL "
    " AND {tk}.created_ms IS NOT NULL AND {tt}.covered_from_ms <= {tk}.created_ms)"
)


def depth_histogram(
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    buckets: Sequence[int] = DEPTH_BUCKETS_MS,
) -> dict[str, int]:
    """How many proved tapes reach each depth past launch. ``{"60s": n, "120s": n, ...}``.

    The number this whole module is now judged on, and it is deliberately read straight
    off the database so an operator can reproduce it with one SELECT. Only rows that pass
    the same proof test :func:`complete_tokens` applies are counted: a deep *partial* tape
    is not a deep tape, because nobody can say what is missing from the middle of it.
    """
    c = _conn(conn)
    out: dict[str, int] = {f"{b // 1000}s": 0 for b in buckets}
    sql = (
        "SELECT COUNT(*) AS n FROM token_tape tt "
        "JOIN tokens tk ON tk.chain = tt.chain AND tk.address = tt.token "
        "WHERE tt.chain=? AND " + _PROVED_SQL.format(tt="tt", tk="tk") + " "
        "  AND tt.covered_to_ms IS NOT NULL AND tt.covered_to_ms - tk.created_ms >= ?"
    )
    for bucket in buckets:
        try:
            row = fetch_one(c, sql, (chain.value, int(bucket)))
        except sqlite3.Error as exc:
            log.warning("tape: depth histogram failed (%s)", exc)
            return out
        out[f"{bucket // 1000}s"] = (_int(row["n"]) if row else 0) or 0
    return out


def coverage_summary(
    chain: Chain = Chain.SOL, conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    """Counts an operator can read at a glance and check against the database by hand."""
    c = _conn(conn)
    out: dict[str, Any] = {
        "tokens_known": 0,
        "complete": 0,
        "partial": 0,
        "unavailable": 0,
        "unassessed": 0,
        "swaps_per_token_route": 0,
        "swaps_wallet_walk": 0,
        "depth": depth_histogram(chain, c),
    }
    try:
        row = fetch_one(c, "SELECT COUNT(*) AS n FROM tokens WHERE chain=?", (chain.value,))
        out["tokens_known"] = _int(row["n"]) if row else 0
        out["complete"] = len(complete_tokens(chain, c))
        for r in fetch_all(
            c,
            "SELECT coverage, COUNT(*) AS n FROM token_tape WHERE chain=? GROUP BY coverage",
            (chain.value,),
        ):
            key = str(r["coverage"])
            if key in (PARTIAL, UNAVAILABLE):
                out[key] = _int(r["n"]) or 0
        assessed = fetch_one(
            c, "SELECT COUNT(*) AS n FROM token_tape WHERE chain=?", (chain.value,)
        )
        out["unassessed"] = max(0, out["tokens_known"] - (_int(assessed["n"]) if assessed else 0))
        for r in fetch_all(
            c, "SELECT source, COUNT(*) AS n FROM swaps WHERE chain=? GROUP BY source", (chain.value,)
        ):
            if str(r["source"]) == TRADE_SOURCE:
                out["swaps_per_token_route"] = _int(r["n"]) or 0
            elif str(r["source"]) == WALLET_SOURCE:
                out["swaps_wallet_walk"] = _int(r["n"]) or 0
    except sqlite3.Error as exc:
        log.warning("tape: coverage summary failed (%s)", exc)
    return out


# --------------------------------------------------------------------------------------
# writing coverage
# --------------------------------------------------------------------------------------


_UPSERT = (
    "INSERT INTO token_tape "
    "(chain, token, model, coverage, route, proof, reason, covered_from_ms, covered_to_ms, "
    " created_ms, oldest_ms, newest_ms, swaps_route, swaps_total, pages, create_tx, "
    " create_tx_basis, attempts, last_attempt_ms, next_attempt_ms, first_seen_ms, updated_ms) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT(chain, token) DO UPDATE SET "
    " model=excluded.model, coverage=excluded.coverage, route=excluded.route, "
    " proof=excluded.proof, reason=excluded.reason, covered_from_ms=excluded.covered_from_ms, "
    " covered_to_ms=excluded.covered_to_ms, created_ms=excluded.created_ms, "
    " oldest_ms=excluded.oldest_ms, newest_ms=excluded.newest_ms, "
    " swaps_route=excluded.swaps_route, swaps_total=excluded.swaps_total, "
    " pages=excluded.pages, create_tx=excluded.create_tx, "
    " create_tx_basis=excluded.create_tx_basis, attempts=excluded.attempts, "
    " last_attempt_ms=excluded.last_attempt_ms, next_attempt_ms=excluded.next_attempt_ms, "
    " updated_ms=excluded.updated_ms"
)


def store(record: TapeRecord, conn: sqlite3.Connection | None = None, *, at_ms: int | None = None) -> bool:
    """Persist one record. Idempotent on ``(chain, token)``; returns whether it was written.

    A record that claims completeness it cannot support is rejected by migration 025's
    CHECK constraints. That rejection is logged and swallowed rather than raised: a bad
    coverage claim must not be stored, and it also must not take down a long collection
    pass. The token simply stays unproved, which is the fail-closed answer.
    """
    c = _conn(conn)
    now = at_ms if at_ms is not None else now_ms()
    try:
        c.execute(
            _UPSERT,
            (
                record.chain.value,
                record.token,
                record.model,
                record.coverage,
                record.route,
                record.proof,
                record.reason,
                record.covered_from_ms,
                record.covered_to_ms,
                record.created_ms,
                record.oldest_ms,
                record.newest_ms,
                record.swaps_route,
                record.swaps_total,
                record.pages,
                record.create_tx,
                record.create_tx_basis,
                record.attempts,
                record.last_attempt_ms if record.last_attempt_ms is not None else now,
                record.next_attempt_ms,
                now,
                now,
            ),
        )
    except sqlite3.Error as exc:
        log.warning("tape: refusing coverage row for %s (%s)", record.token[:12], exc)
        return False
    return True


def seed_from_snapshots(
    chain: Chain = Chain.SOL, conn: sqlite3.Connection | None = None, *, at_ms: int | None = None
) -> int:
    """Adopt coverage that ``token_flow.observe`` already proved, before this table existed.

    ``curve_snapshots.coverage_from_ms`` is only ever written from a ``collect_trades``
    result, and ``collect_trades`` only lets it reach back to or past ``created_ms`` when
    the walk hit ``end_of_history``. A snapshot whose ``coverage_from_ms <= created_ms`` is
    therefore the same evidence this module records, recorded earlier and in another table
    — worth adopting rather than re-fetching.

    Nothing weaker is adopted. A snapshot with ``coverage_from_ms`` after the token's
    creation means the walk stopped early, and it is left for the job to re-attempt.

    **It re-adopts.** The first version skipped any token that already held a row, which
    seemed conservative and was the opposite: when five proved rows were overwritten with
    ``unavailable`` by failed re-scans, the perfectly good snapshot evidence sat there and
    could never be read back, so the damage was permanent. A record that is *weaker* than
    the evidence in ``curve_snapshots`` is a record to replace, not a reason to stay quiet.
    A record that already holds a standing proof is left exactly as it is.
    """
    c = _conn(conn)
    now = at_ms if at_ms is not None else now_ms()
    try:
        rows = fetch_all(
            c,
            "SELECT cs.token AS token, MIN(cs.coverage_from_ms) AS covered_from, "
            "       MAX(cs.observed_ms) AS covered_to, tk.created_ms AS created_ms "
            "FROM curve_snapshots cs "
            "JOIN tokens tk ON tk.chain = cs.chain AND tk.address = cs.token "
            "WHERE cs.chain=? AND cs.coverage_from_ms IS NOT NULL AND tk.created_ms IS NOT NULL "
            "GROUP BY cs.chain, cs.token "
            "HAVING MIN(cs.coverage_from_ms) <= tk.created_ms",
            (chain.value,),
        )
    except sqlite3.Error as exc:
        log.warning("tape: snapshot seed query failed (%s)", exc)
        return 0

    seeded = 0
    for row in rows:
        token = str(row["token"])
        created = _int(row["created_ms"])
        existing = record_of(chain, token, c)
        if _proof_still_holds(existing, created):
            continue  # already proved; the snapshot adds nothing
        counts = _swap_counts(chain, token, c)
        record = TapeRecord(
            chain=chain,
            token=token,
            coverage=COMPLETE,
            route=ROUTE_TRADES,
            proof="curve_snapshot_end_of_history",
            reason=(
                "re-adopted over a weaker record from curve_snapshots.coverage_from_ms"
                if existing is not None
                else "adopted from curve_snapshots.coverage_from_ms proved at or before launch"
            ),
            covered_from_ms=_int(row["covered_from"]),
            covered_to_ms=max(_int(row["covered_to"]) or 0, counts["newest_ms"] or 0) or None,
            created_ms=created,
            oldest_ms=counts["oldest_ms"],
            newest_ms=counts["newest_ms"],
            swaps_route=counts["route"],
            swaps_total=counts["total"],
            create_tx=existing.create_tx if existing else None,
            create_tx_basis=existing.create_tx_basis if existing else None,
            attempts=existing.attempts if existing else 0,
            last_attempt_ms=now,
        )
        if store(record, c, at_ms=now):
            seeded += 1
    return seeded


def advance_watermark(
    watermark: int | None, observations: Sequence[tuple[int | None, int | None]]
) -> tuple[int | None, int]:
    """Walk a chain of ``(observed_ms, coverage_from_ms)`` forward. Pure.

    Returns ``(watermark, steps)``. Each observation is one ``collect_trades`` walk that
    someone else already did and recorded in ``curve_snapshots``: it fetched this mint's
    newest trades at ``observed_ms`` and paged back as far as ``coverage_from_ms``. So it
    extends our coverage to ``observed_ms`` **if and only if it reached back to at or
    before where our coverage already ended** — otherwise there is a hole between the two
    and a hole is not coverage, however complete each side of it is.

    Three ways an observation is refused, and all three are absences rather than
    contradictions, so they break the chain without weakening what came before it:

    * ``coverage_from_ms IS NULL`` — the walk was not run, or could not be read. A
      snapshot recorded with ``collect=False`` looks exactly like one whose walk failed,
      and neither says anything about trades.
    * ``coverage_from_ms > watermark`` — the walk stopped short. This is the page-budget
      case and it is the one that matters: it is precisely how a gap gets created.
    * ``observed_ms <= watermark`` — older than what we hold. Nothing to add.

    A later observation can still re-join the chain after an earlier one broke it, which
    is why this is a walk rather than a fold over the maximum: the walk that broke the
    chain wrote nothing to the watermark, so a subsequent deeper walk is measured against
    the same unchanged watermark and bridges it if it reaches.
    """
    if watermark is None:
        return None, 0
    steps = 0
    for observed_ms, coverage_from_ms in observations:
        if observed_ms is None or observed_ms <= watermark:
            continue
        if coverage_from_ms is None or coverage_from_ms > watermark:
            continue
        watermark = observed_ms
        steps += 1
    return watermark, steps


@dataclass(slots=True)
class ReconcileReport:
    """What one :func:`reconcile` pass changed. No provider call is made."""

    rows_seen: int = 0
    rows_written: int = 0
    counts_refreshed: int = 0
    watermarks_advanced: int = 0
    watermark_steps: int = 0
    depth_before: dict[str, int] = field(default_factory=dict)
    depth_after: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows_seen": self.rows_seen,
            "rows_written": self.rows_written,
            "counts_refreshed": self.counts_refreshed,
            "watermarks_advanced": self.watermarks_advanced,
            "watermark_steps": self.watermark_steps,
            "depth_before": dict(self.depth_before),
            "depth_after": dict(self.depth_after),
        }


def reconcile(
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    at_ms: int | None = None,
    config: TapeConfig = DEFAULT_CONFIG,
) -> ReconcileReport:
    """Re-read what the database already knows into ``token_tape``. **Makes no request.**

    Two things were provably wrong on ``data/kaiba.db`` on 2026-09-21, and both of them
    are bookkeeping rather than collection:

    1. **4,043 of 4,071 complete rows had ``updated_ms == first_seen_ms``.** They were
       written once and never touched again. Meanwhile ``kaiba.ops.scheduler``'s
       ``job_token_flow`` had gone on calling ``token_flow.collect_trades`` directly and
       writing rows straight into ``swaps`` without telling this table anything, so
       ``newest_ms`` and ``swaps_total`` described a mint as it had been at first sight.
       1,205 rows held a ``newest_ms`` a median of 283 s behind the newest trade actually
       stored. Nothing could tell a stale tape from a fresh one because nothing was
       writing the field that would have said.

    2. **The coverage watermark was frozen with it.** ``curve_snapshots`` carries, for
       every scan, the ``coverage_from_ms`` of the walk that scan ran. Chained through
       :func:`advance_watermark` that is evidence of contiguous coverage out to a later
       observation, and it was sitting unread: 3,035 of 4,074 proved rows could have their
       watermark advanced from data already on disk, for zero requests.

    Row counts are refreshed unconditionally -- they are a ``COUNT``/``MIN``/``MAX`` over
    ``swaps``, a restatement of fact, not a claim. ``covered_to_ms`` is advanced only for
    rows whose proof currently stands and only as far as the chain of observations
    supports, and ``covered_from_ms``, ``proof`` and ``coverage`` are never touched: a
    tape may grow at the end without its claim about its start moving by a millisecond.
    """
    c = _conn(conn)
    now = at_ms if at_ms is not None else now_ms()
    report = ReconcileReport(depth_before=depth_histogram(chain, c))

    observations: dict[str, list[tuple[int | None, int | None]]] = {}
    try:
        for row in fetch_all(
            c,
            "SELECT token, observed_ms, coverage_from_ms FROM curve_snapshots "
            "WHERE chain=? ORDER BY token, observed_ms",
            (chain.value,),
        ):
            observations.setdefault(str(row["token"]), []).append(
                (_int(row["observed_ms"]), _int(row["coverage_from_ms"]))
            )
        measured = {
            str(r["token"]): r
            for r in fetch_all(
                c,
                "SELECT token, COUNT(*) AS n, MIN(ts_ms) AS lo, MAX(ts_ms) AS hi, "
                " SUM(CASE WHEN source=? THEN 1 ELSE 0 END) AS route "
                "FROM swaps WHERE chain=? GROUP BY token",
                (TRADE_SOURCE, chain.value),
            )
        }
        rows = fetch_all(
            c,
            "SELECT tt.token AS token, tk.created_ms AS created_ms FROM token_tape tt "
            "JOIN tokens tk ON tk.chain = tt.chain AND tk.address = tt.token "
            "WHERE tt.chain=?",
            (chain.value,),
        )
    except sqlite3.Error as exc:
        log.warning("tape: reconcile query failed (%s)", exc)
        return report

    for row in rows:
        token = str(row["token"])
        record = record_of(chain, token, c)
        if record is None:
            continue
        report.rows_seen += 1
        seen = measured.get(token)
        oldest = _int(seen["lo"]) if seen else None
        newest = _int(seen["hi"]) if seen else None
        total = (_int(seen["n"]) if seen else 0) or 0
        route_rows = (_int(seen["route"]) if seen else 0) or 0
        counts_moved = (
            record.oldest_ms != oldest
            or record.newest_ms != newest
            or record.swaps_total != total
            or record.swaps_route != route_rows
        )

        covered_to = record.covered_to_ms
        next_attempt = record.next_attempt_ms
        steps = 0
        proved_now = _proof_still_holds(record, _int(row["created_ms"]))
        if proved_now:
            covered_to, steps = advance_watermark(record.covered_to_ms, observations.get(token, ()))
        # A watermark may only ever move forward. `advance_watermark` cannot move it back,
        # but the max() says so at the call site too, because a silently receding
        # watermark would make the next top-up re-walk ground it already holds.
        if covered_to is not None and record.covered_to_ms is not None:
            covered_to = max(covered_to, record.covered_to_ms)
        advanced = covered_to is not None and covered_to != record.covered_to_ms
        if proved_now:
            # Only a standing proof gets its timer rewritten here. A partial or refused
            # row is inside somebody's backoff ladder and reconciliation is not evidence
            # that the ladder was wrong.
            next_attempt = next_deepen_attempt(_int(row["created_ms"]), covered_to, config)

        if not counts_moved and not advanced and next_attempt == record.next_attempt_ms:
            continue
        refreshed = replace(
            record,
            oldest_ms=oldest,
            newest_ms=newest,
            swaps_total=total,
            swaps_route=route_rows,
            covered_to_ms=covered_to,
            next_attempt_ms=next_attempt,
        )
        if not store(refreshed, c, at_ms=now):
            continue
        report.rows_written += 1
        report.counts_refreshed += int(counts_moved)
        report.watermarks_advanced += int(advanced)
        report.watermark_steps += steps
    report.depth_after = depth_histogram(chain, c)
    return report


def _swap_counts(chain: Chain, token: str, conn: sqlite3.Connection) -> dict[str, int | None]:
    """Row counts and the time span we hold for a mint, split by how they were obtained."""
    out: dict[str, int | None] = {"total": 0, "route": 0, "wallet": 0, "oldest_ms": None, "newest_ms": None}
    try:
        row = fetch_one(
            conn,
            "SELECT COUNT(*) AS n, MIN(ts_ms) AS lo, MAX(ts_ms) AS hi, "
            " SUM(CASE WHEN source=? THEN 1 ELSE 0 END) AS route, "
            " SUM(CASE WHEN source=? THEN 1 ELSE 0 END) AS wallet "
            "FROM swaps WHERE chain=? AND token=?",
            (TRADE_SOURCE, WALLET_SOURCE, chain.value, token),
        )
    except sqlite3.Error as exc:
        log.warning("tape: swap counts unreadable for %s (%s)", token[:12], exc)
        return out
    if row is None:
        return out
    out["total"] = _int(row["n"]) or 0
    out["route"] = _int(row["route"]) or 0
    out["wallet"] = _int(row["wallet"]) or 0
    out["oldest_ms"] = _int(row["lo"])
    out["newest_ms"] = _int(row["hi"])
    return out


@dataclass(frozen=True, slots=True)
class _Verdict:
    coverage: str
    proof: str | None
    reason: str
    covered_from_ms: int | None
    retry: bool


def next_deepen_attempt(
    created_ms: int | None,
    covered_to_ms: int | None,
    config: TapeConfig = DEFAULT_CONFIG,
) -> int | None:
    """When a proved tape should be asked again *to make it deeper*. Pure.

    ``None`` means never: either the claim already spans the modelling horizon, or we do
    not know the launch time and so cannot say how deep it is, and in both cases a return
    visit would be a request spent to learn nothing.

    Otherwise it is **one** scheduled visit, at ``launch + deep_enough_ms + settle``. That
    timing is the whole budget argument. A tape captured at tier-1 scan time necessarily
    stops seconds after launch -- the trades that make the first five minutes have not
    happened yet -- so depth cannot come from spending more pages at capture, only from
    coming back. Coming back *once*, after the window has closed, costs one page at p50
    and p90 (measured: the rows between the stored watermark and launch+300 s number 1 at
    p50 and 36 at p90, against a 100-row page). Coming back repeatedly would cost a
    request per visit and buy the same tape.

    A time already in the past is returned as-is rather than clamped to ``now``: the row
    is due, and saying so with the timestamp it was due at keeps the arithmetic legible
    in the table.
    """
    if created_ms is None or covered_to_ms is None:
        return None
    if covered_to_ms - created_ms >= config.deep_enough_ms:
        return None
    return created_ms + config.deep_enough_ms + config.deepen_settle_ms


def _proof_still_holds(prior: TapeRecord | None, created_ms: int | None) -> bool:
    """Does an earlier proof still cover this token's launch?

    Same relaxed comparison as :func:`completeness`, and for the same reason: a launch
    time that moved later cannot invalidate coverage that already reached back past it,
    and a sub-second rounding difference is not a correction.
    """
    return (
        prior is not None
        and prior.proved
        and created_ms is not None
        and prior.covered_from_ms is not None
        and prior.covered_from_ms <= created_ms
    )


def record_failed_attempt(
    prior: TapeRecord,
    conn: sqlite3.Connection | None = None,
    *,
    flow_reason: str,
    now: int,
    config: TapeConfig = DEFAULT_CONFIG,
) -> TapeRecord:
    """Note that an observation failed, **without touching what an earlier one proved**.

    The attempt counter and the retry timer move; ``coverage``, ``proof``,
    ``covered_from_ms`` and ``created_ms`` do not. See :data:`NON_OBSERVATIONS` for the
    incident that made this a named function rather than an inline branch.
    """
    c = _conn(conn)
    base = prior.reason.split(" | failed attempt:", 1)[0]
    refreshed = replace(
        prior,
        reason=f"{base} | failed attempt: {flow_reason}"[:400],
        attempts=prior.attempts + 1,
        last_attempt_ms=now,
        # Every failure gets a timer, including one on a preserved proof.
        #
        # This used to be `None if prior.coverage == COMPLETE`, on the reasoning that a
        # proved tape "stays out of the candidate list on its own merits, so it needs no
        # timer". That reasoning died the moment `candidates` learned to re-offer a
        # complete-but-shallow tape for deepening: NULL means *eligible now*, so a mint
        # that had just refused us would be re-asked on every single pass, at the one
        # provider whose budget we are least able to spend and on the one route that
        # answers 503 persistently rather than transiently. A deepening refusal is exactly
        # the case that must back off.
        next_attempt_ms=now + config.backoff_for(prior.attempts + 1) * 1000,
    )
    store(refreshed, c, at_ms=now)
    return refreshed


def _classify(
    *,
    flow_reason: str,
    pages: int | None,
    created_ms: int | None,
    oldest_ms: int | None,
    prior: TapeRecord | None,
    topping_up: bool,
) -> _Verdict:
    """Turn a ``collect_trades`` outcome into a coverage verdict. Pure.

    One function, two callers — the standalone job and the tier-1 scanner — because the
    whole value of ``token_tape`` is that "complete" means the same thing however the rows
    arrived. Two code paths reaching that word independently is how the scanner ends up
    with a laxer definition than the job, and the laxer one wins by writing last.

    ``end_of_history`` is the only ``collect_trades`` reason that means the walk
    *terminated*. ``reached_watermark`` means it stopped where we told it to, which proves
    completeness only when the rows on the far side of that watermark were already proved.
    """
    where = f" in {pages} page(s)" if pages else ""

    if flow_reason == REASON_UNAVAILABLE:
        return _Verdict(UNAVAILABLE, None, "provider_unavailable", None, True)

    if flow_reason == REASON_END_OF_HISTORY:
        if created_ms is None:
            # The walk did terminate, so we hold every trade. We just cannot tie that to a
            # launch, and "complete back to launch" is the property the lanes gate on.
            return _Verdict(PARTIAL, None, "walk_terminated_but_creation_time_unknown", None, False)
        covered_from = created_ms if oldest_ms is None else min(created_ms, oldest_ms)
        return _Verdict(COMPLETE, REASON_END_OF_HISTORY, f"walked to end of history{where}",
                        covered_from, False)

    if topping_up and prior is not None:
        if flow_reason == REASON_WATERMARK:
            return _Verdict(COMPLETE, "topup_contiguous",
                            f"topped up contiguously from watermark{where}",
                            prior.covered_from_ms, False)
        # The watermark was not reached, so rows between it and what we just wrote are
        # missing. A gap is not a complete tape, however complete each side of it is.
        return _Verdict(PARTIAL, None, f"topup_gap:{flow_reason}", None, True)

    return _Verdict(PARTIAL, None, f"walk_incomplete:{flow_reason}", None, True)


def observed_to_ms(coverage: str, observed_at_ms: int | None, newest_ms: int | None) -> int | None:
    """The newest moment coverage reaches, given how the walk ended. Pure.

    A terminated or bridged walk fetched this mint's newest page **at** ``observed_at_ms``
    and found nothing after the rows it wrote, so coverage runs to that instant and not
    merely to the last trade in it. The distinction is not pedantry, it is most of the
    depth we were missing: a mint that traded for forty seconds and then stopped still has
    a five-minute tape the moment we ask at launch+300 s and are told there is nothing
    newer. Scoring it by its last trade calls that mint 40 s deep forever, however many
    times we look, and the mints that go quiet early are the ones the rug literature is
    about.

    ``coverage`` gates it deliberately. On a ``partial`` verdict there is a hole somewhere
    and the observation time would be a claim across it, so the answer falls back to the
    newest row we hold, which claims nothing.
    """
    if coverage != COMPLETE:
        return newest_ms
    if observed_at_ms is None:
        return newest_ms
    if newest_ms is None:
        return observed_at_ms
    return max(observed_at_ms, newest_ms)


def _flow_observed_at(flow: FlowResult | None, call_started_ms: int) -> int | None:
    """The latest instant this walk can honestly claim to have looked at. Never later.

    Two independent ceilings, and the answer is the lower of them, because each guards a
    different way of accidentally claiming the present:

    * **the earliest receipt** — ``_http`` hands back a cached page carrying its
      *original* fetch time, so reading the clock instead would let a cache hit advance a
      watermark to now. ``FlowConfig.ttl_s`` is 0.0 for the trade route today, which is a
      reason this has never bitten and not a reason to depend on it.
    * **when the call started** — taken before any request went out, so it is earlier
      than every page in the walk. It is also the only ceiling a caller can control, which
      is what makes the behaviour testable on a fixed clock instead of on wall time.
    """
    stamps = [call_started_ms]
    if flow is not None:
        stamps.extend(r.observed_at_ms for r in flow.receipts if r.observed_at_ms)
    return min(stamps) if stamps else None


def _route_for(counts: dict[str, int | None]) -> str:
    route = counts["route"] or 0
    wallet = counts["wallet"] or 0
    if route and wallet:
        return ROUTE_MIXED
    if route:
        return ROUTE_TRADES
    if wallet:
        return ROUTE_WALLET
    return ROUTE_NONE


# --------------------------------------------------------------------------------------
# the two data repairs
# --------------------------------------------------------------------------------------


def repair_create_flags(
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    tokens: Iterable[str] | None = None,
) -> tuple[int, int]:
    """Set ``swaps.is_create_tx`` from the launchpad's own creation signature.

    Returns ``(rows_set, tokens_with_signature)``.

    This is a fact, not the usual inference. Consumers had been reaching for "the
    creator's earliest buy", which is only equivalent where create and dev-buy are one
    transaction — true on pump.fun, a guess on any other launchpad, and a guess that fails
    silently rather than loudly. ``tokens.meta_json.signature`` is the transaction the
    launchpad said created the mint, so the rule is an equality on ``swaps.tx`` and nothing
    is marked when the creator made no initial buy, which is correct: there is no create
    swap to mark, and inventing one would put a phantom row at the head of every launch.

    Idempotent, and it never clears a flag another collector set.
    """
    c = _conn(conn)
    sql = "SELECT address, meta_json FROM tokens WHERE chain=?"
    params: list[Any] = [chain.value]
    wanted = list(tokens) if tokens is not None else None
    if wanted is not None:
        if not wanted:
            return 0, 0
        sql += f" AND address IN ({','.join('?' * len(wanted))})"
        params.extend(wanted)
    try:
        rows = fetch_all(c, sql, tuple(params))
    except sqlite3.Error as exc:
        log.warning("tape: create-flag scan failed (%s)", exc)
        return 0, 0

    marked = 0
    with_sig = 0
    for row in rows:
        sig = create_signature(row["meta_json"])
        if sig is None:
            continue
        with_sig += 1
        try:
            marked += (
                c.execute(
                    "UPDATE swaps SET is_create_tx=1 "
                    "WHERE chain=? AND token=? AND tx=? AND is_create_tx=0",
                    (chain.value, str(row["address"]), sig),
                ).rowcount
                or 0
            )
        except sqlite3.Error as exc:
            log.warning("tape: create-flag update failed for %s (%s)", str(row["address"])[:12], exc)
    return marked, with_sig


def repair_fee_payer(
    chain: Chain = Chain.SOL, conn: sqlite3.Connection | None = None
) -> tuple[int, int]:
    """Copy a fee payer onto rows of the same transaction that are missing one.

    Returns ``(rows_set, rows_still_missing)``.

    This is the whole of what is recoverable without a new provider call, and it is small
    by construction: it only fires where two collectors saw the same signature and exactly
    one of them reported a fee payer. The pump.fun trade route reports none at all, so the
    overlap with the Helius wallet backfill is the only bridge, and the two collections
    barely intersect.

    What it deliberately does **not** do is set ``fee_payer = wallet``. On a bonding-curve
    trade the trader usually is the signer, so the guess would be right most of the time
    and wrong exactly where it matters: a bundler pays for wallets that are not it, and the
    shared-fee-payer rule exists to find precisely those. A rule that is right on the easy
    cases and wrong on the interesting ones is worse than no rule, because the clusters it
    produces look real. See :data:`FEE_PAYER_NOTE` for what would actually fix it.
    """
    c = _conn(conn)
    copied = 0
    try:
        donors = fetch_all(
            c,
            "SELECT s.tx AS tx, MIN(s.fee_payer) AS fee_payer FROM swaps s "
            "WHERE s.chain=? AND s.fee_payer IS NOT NULL "
            "  AND EXISTS (SELECT 1 FROM swaps t WHERE t.chain=s.chain AND t.tx=s.tx "
            "              AND t.fee_payer IS NULL) "
            "GROUP BY s.tx "
            "HAVING COUNT(DISTINCT s.fee_payer) = 1",
            (chain.value,),
        )
        for row in donors:
            copied += (
                c.execute(
                    "UPDATE swaps SET fee_payer=? WHERE chain=? AND tx=? AND fee_payer IS NULL",
                    (str(row["fee_payer"]), chain.value, str(row["tx"])),
                ).rowcount
                or 0
            )
        left = fetch_one(
            c,
            "SELECT COUNT(*) AS n FROM swaps WHERE chain=? AND fee_payer IS NULL",
            (chain.value,),
        )
    except sqlite3.Error as exc:
        log.warning("tape: fee-payer repair failed (%s)", exc)
        return copied, -1
    return copied, (_int(left["n"]) if left else 0) or 0


# --------------------------------------------------------------------------------------
# collecting one token
# --------------------------------------------------------------------------------------


def _decimals_for(row: dict[str, Any], config: TapeConfig) -> tuple[int | None, str]:
    """``(decimals, basis)``; ``(None, reason)`` when they cannot be established for free.

    An assumed exponent is a silent factor-of-a-thousand error in every ``amount_token``,
    and it produces a wallet that looks like it bought a thousand times its actual size —
    the kind of wrong number that survives review because it is the right shape. The
    assumption is allowed only where it has been measured to hold.
    """
    known = _int(row.get("decimals"))
    if known is not None and 0 <= known <= 18:
        return known, "tokens.decimals"
    launchpad = str(row.get("launchpad") or "").lower()
    if launchpad in config.pumpfun_launchpads:
        return config.pumpfun_decimals, "pumpfun_default"
    return None, f"decimals_unknown_for_launchpad:{launchpad or 'none'}"


def collect_token(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection | None = None,
    *,
    config: TapeConfig = DEFAULT_CONFIG,
    flow_config: FlowConfig | None = None,
    priority: Priority = Priority.RESEARCH,
    at_ms: int | None = None,
) -> tuple[TapeRecord, FlowResult | None]:
    """Pull one mint's tape and record what that establishes. Never raises.

    Three modes, chosen from what we already hold, so a repeated run is cheap:

    * **Top-up.** The tape is already proved complete: walk back only as far as the stored
      watermark. One page is the usual cost. If the walk does *not* reach the watermark
      inside :attr:`TapeConfig.topup_pages`, there is now a hole between the rows we had and
      the rows we just wrote, so the record is demoted to ``partial``. An unreached
      watermark is a gap, and a gap is not a complete tape.
    * **Full walk.** Everything else: page backwards until the route returns a short page.
    * **Refusal without a request.** A mint whose decimals cannot be established is
      recorded ``unavailable`` before any network call, because the amounts would be
      unusable even if the fetch succeeded.
    """
    c = _conn(conn)
    now = at_ms if at_ms is not None else now_ms()
    prior = record_of(chain, token, c)
    attempts = (prior.attempts if prior else 0) + 1

    try:
        token_row = fetch_one(
            c,
            "SELECT created_ms, decimals, launchpad, meta_json FROM tokens WHERE chain=? AND address=?",
            (chain.value, token),
        )
    except sqlite3.Error as exc:
        log.warning("tape: token row unreadable for %s (%s)", token[:12], exc)
        token_row = None
    token_row = token_row or {}
    created_ms = _int(token_row.get("created_ms"))
    create_tx = create_signature(token_row.get("meta_json"))
    decimals, decimals_basis = _decimals_for(token_row, config)

    def _finish(
        coverage: str,
        route: str,
        reason: str,
        *,
        proof: str | None = None,
        covered_from: int | None = None,
        covered_to: int | None = None,
        pages: int | None = None,
        retry: bool,
    ) -> TapeRecord:
        counts = _swap_counts(chain, token, c)
        # A complete row gets one scheduled return visit if -- and only if -- its claim
        # does not yet span the modelling horizon. It used to get NULL unconditionally,
        # on the reasoning that `candidates` excluded it anyway; that is what made a
        # complete tape permanently undeepenable, because NULL also means "eligible now"
        # and the exclusion was the only thing holding it back. Anything else that is not
        # worth retrying soon still gets the longest backoff rather than NULL, since a
        # permanent refusal with no timestamp would be re-offered on every pass forever.
        if coverage == COMPLETE:
            next_attempt = next_deepen_attempt(created_ms, covered_to, config)
        elif coverage == PARTIAL and retry:
            # A partial tape belongs to a mint that just answered, so it is inside the hot
            # window now and will not be in an hour. Finishing it is urgent in a way that
            # retrying a refusal is not. See :data:`HOT_WINDOW_ANSWERED_MAX_IDLE_MIN`.
            next_attempt = now + config.partial_retry_s * 1000
        elif retry:
            next_attempt = now + config.backoff_for(attempts) * 1000
        else:
            next_attempt = now + config.backoff_s[-1] * 1000
        record = TapeRecord(
            chain=chain,
            token=token,
            coverage=coverage,
            route=route,
            proof=proof,
            reason=reason,
            covered_from_ms=covered_from,
            covered_to_ms=covered_to,
            created_ms=created_ms,
            oldest_ms=counts["oldest_ms"],
            newest_ms=counts["newest_ms"],
            swaps_route=counts["route"],
            swaps_total=counts["total"],
            pages=pages,
            create_tx=create_tx,
            create_tx_basis="launchpad_create_event" if create_tx else None,
            attempts=attempts,
            last_attempt_ms=now,
            next_attempt_ms=next_attempt,
        )
        if not store(record, c, at_ms=now):
            # The CHECK constraints refused the claim. Record the weaker truth instead of
            # leaving the token looking unassessed, and never retry into the same refusal.
            fallback = TapeRecord(
                chain=chain,
                token=token,
                coverage=PARTIAL,
                route=_route_for(counts),
                reason=f"coverage_claim_rejected:{reason}"[:400],
                covered_to_ms=covered_to,
                created_ms=created_ms,
                oldest_ms=counts["oldest_ms"],
                newest_ms=counts["newest_ms"],
                swaps_route=counts["route"],
                swaps_total=counts["total"],
                pages=pages,
                create_tx=create_tx,
                create_tx_basis="launchpad_create_event" if create_tx else None,
                attempts=attempts,
                last_attempt_ms=now,
                next_attempt_ms=now + config.backoff_for(attempts) * 1000,
            )
            store(fallback, c, at_ms=now)
            return fallback
        return record

    if chain is not Chain.SOL:
        return _finish(UNAVAILABLE, ROUTE_NONE, f"no_trade_source_for_{chain.value}", retry=False), None
    if decimals is None:
        return _finish(UNAVAILABLE, _route_for(_swap_counts(chain, token, c)), decimals_basis,
                       retry=False), None

    topping_up = bool(_proof_still_holds(prior, created_ms)
                      and prior is not None and prior.covered_to_ms is not None)
    watermark = prior.covered_to_ms if (topping_up and prior is not None) else None
    budget = config.topup_pages if topping_up else config.walk_pages

    flow = collect_trades(
        chain,
        token,
        c,
        since_ms=watermark,
        created_ms=created_ms,
        decimals=decimals,
        max_pages=budget,
        config=config.flow_config(flow_config),
        priority=priority,
    )

    if create_tx is not None:
        repair_create_flags(chain, c, tokens=[token])

    # A refusal must never be written over a proof earned when the endpoint was answering.
    if flow.reason in NON_OBSERVATIONS and prior is not None and prior.proved:
        return record_failed_attempt(
            prior, c, flow_reason=flow.reason, now=now, config=config
        ), flow

    newest = max(flow.newest_ms or 0, (prior.covered_to_ms if prior else 0) or 0) or None
    verdict = _classify(
        flow_reason=flow.reason,
        pages=flow.pages,
        created_ms=created_ms,
        oldest_ms=flow.coverage_from_ms if flow.coverage_from_ms is not None else flow.oldest_ms,
        prior=prior,
        topping_up=topping_up,
    )

    # A walk that was not a top-up cannot demote a standing proof.
    #
    # Only a top-up produces evidence of a *hole*: it starts from a stored watermark and
    # fails to reach it, so the rows it just wrote provably do not join the rows we had.
    # A walk with no watermark to aim at -- a proved row whose `covered_to_ms` is NULL, or
    # one re-walked from scratch -- that runs out of page budget has observed nothing
    # about completeness at all. Writing `partial` on it would be the 2026-09-20 incident
    # in a new costume: a downgrade justified by the absence of evidence, on a hot-window
    # resource where the proof cannot be earned a second time.
    if (
        verdict.coverage != COMPLETE
        and not topping_up
        and prior is not None
        and prior.proved
        and _proof_still_holds(prior, created_ms)
    ):
        return record_failed_attempt(
            prior, c, flow_reason=f"rewalk_did_not_reach_proof:{flow.reason}", now=now, config=config
        ), flow

    covered_to = observed_to_ms(verdict.coverage, _flow_observed_at(flow, now), newest)
    # Same rule as the scan path: we walked the per-token route, so that is the route,
    # even on a mint whose entire history turned out to be empty.
    return _finish(
        verdict.coverage,
        ROUTE_TRADES,
        verdict.reason,
        proof=verdict.proof,
        covered_from=verdict.covered_from_ms,
        covered_to=covered_to,
        pages=flow.pages,
        retry=verdict.retry,
    ), flow


# --------------------------------------------------------------------------------------
# capture at scan time — the only moment a tape is obtainable
# --------------------------------------------------------------------------------------


#: Keys :func:`token_flow.observe` writes into the curve dict, which is how the scanner's
#: already-completed collection reports itself. Reading them is what lets the scan record
#: coverage without making a single extra request.
FLOW_REASON_KEY = "flow_reason"
FLOW_PAGES_KEY = "flow_pages"


def record_scan_capture(
    chain: Chain,
    token: str,
    curve: Mapping[str, Any] | None,
    conn: sqlite3.Connection | None = None,
    *,
    created_ms: int | None = None,
    at_ms: int | None = None,
    config: TapeConfig = DEFAULT_CONFIG,
) -> TapeRecord | None:
    """Record what a tier-1 scan's trade collection established. **Makes no provider call.**

    This is the important half of the whole module, because of what the hot window means:
    a mint's tape is obtainable while it is trading and gone an hour after it stops, so
    tier-1 scan time — when the token is seconds old and the trade lane will answer — is
    the only moment we reliably have. ``kaiba.execution.scanner`` already collects the
    trades there via ``token_flow.observe``; what it did not do was write down whether the
    collection *finished*, so the evidence existed and no consumer could find it.

    Everything here is read from the curve dict ``observe`` already returned and from rows
    already in the database. Cost is a handful of local SQLite statements — microseconds
    against a ~7.6 s pass — and it adds no latency to the scan and no load to the endpoint.

    It classifies through the same :func:`_classify` as the standalone job, so a tape
    captured by the scanner and a tape captured by :func:`collect_token` mean exactly the
    same thing by the word ``complete``. Returns ``None`` when the scan did not attempt a
    collection at all, which is different from attempting one and failing.
    """
    if curve is None:
        return None
    flow_reason = curve.get(FLOW_REASON_KEY)
    if not isinstance(flow_reason, str) or not flow_reason:
        return None  # the scan did not run a collection; say nothing rather than guess

    c = _conn(conn)
    now = at_ms if at_ms is not None else now_ms()
    prior = record_of(chain, token, c)

    # `tokens.created_ms` is the authority, not the curve payload's `created_timestamp`.
    # pump.fun floors that field to the second, so a proof stored against it carried
    # ...028000 where the token row held ...029249 and a later completeness check read the
    # 1,249 ms difference as the launch time having moved. One source of truth for the
    # number, and the number is the one `completeness` compares against.
    if created_ms is None:
        try:
            row = fetch_one(
                c, "SELECT created_ms FROM tokens WHERE chain=? AND address=?", (chain.value, token)
            )
        except sqlite3.Error as exc:
            log.debug("tape: token row unreadable during scan capture for %s (%s)", token[:12], exc)
            row = None
        created_ms = _int(row["created_ms"]) if row else None
    if created_ms is None:
        created_ms = _int(curve.get("created_ms"))

    if flow_reason in NON_OBSERVATIONS and prior is not None and prior.proved:
        # The scan could not read the tape. That is silence, not contradiction.
        return record_failed_attempt(prior, c, flow_reason=flow_reason, now=now, config=config)

    counts = _swap_counts(chain, token, c)
    # ``observe`` tops up from the previous snapshot rather than from our watermark, so a
    # re-scan of a proved token legitimately stops at ``reached_watermark``. That is still
    # contiguous: the previous snapshot is the moment we last proved coverage, and the walk
    # reached back past it. The prior record must still be proved against the *same*
    # creation time, or the proof it would inherit was made about a different claim.
    topping_up = _proof_still_holds(prior, created_ms)

    verdict = _classify(
        flow_reason=flow_reason,
        pages=_int(curve.get(FLOW_PAGES_KEY)),
        created_ms=created_ms,
        oldest_ms=counts["oldest_ms"],
        prior=prior,
        topping_up=topping_up,
    )

    create_tx: str | None = None
    try:
        row = fetch_one(
            c, "SELECT meta_json FROM tokens WHERE chain=? AND address=?", (chain.value, token)
        )
        create_tx = create_signature(row["meta_json"]) if row else None
    except sqlite3.Error as exc:
        log.debug("tape: create signature unreadable for %s (%s)", token[:12], exc)
    if create_tx is not None:
        repair_create_flags(chain, c, tokens=[token])

    # `observe` stamps the curve with the wall clock it started at, *before* it runs the
    # collection, so this is never later than the moment the newest page was fetched.
    covered_to = observed_to_ms(
        verdict.coverage,
        _int(curve.get("observed_ms")),
        max(counts["newest_ms"] or 0, (prior.covered_to_ms if prior else 0) or 0) or None,
    )

    attempts = (prior.attempts if prior else 0) + 1
    if verdict.coverage == COMPLETE:
        next_attempt = next_deepen_attempt(created_ms, covered_to, config)
    elif verdict.coverage == PARTIAL and verdict.retry:
        next_attempt = now + config.partial_retry_s * 1000
    elif verdict.retry:
        next_attempt = now + config.backoff_for(attempts) * 1000
    else:
        next_attempt = now + config.backoff_s[-1] * 1000

    record = TapeRecord(
        chain=chain,
        token=token,
        coverage=verdict.coverage,
        # The per-token route is what ran, whatever it returned. `route` records how the
        # collection was *made*, not an inventory of which sources happen to have left
        # rows on this mint — that is `swaps_route` / `swaps_total`. Deriving it from row
        # counts instead meant a terminated walk that found no trades was filed as
        # `route='none'` and its (valid) completeness claim was rejected by the CHECK, and
        # a mint that also held wallet-walk rows was filed as `mixed` and rejected too.
        route=ROUTE_TRADES,
        proof=verdict.proof,
        reason=f"scan: {verdict.reason}"[:400],
        covered_from_ms=verdict.covered_from_ms,
        covered_to_ms=covered_to,
        created_ms=created_ms,
        oldest_ms=counts["oldest_ms"],
        newest_ms=counts["newest_ms"],
        swaps_route=counts["route"],
        swaps_total=counts["total"],
        pages=_int(curve.get(FLOW_PAGES_KEY)),
        create_tx=create_tx,
        create_tx_basis="launchpad_create_event" if create_tx else None,
        attempts=attempts,
        last_attempt_ms=now,
        next_attempt_ms=next_attempt,
    )
    if not store(record, c, at_ms=now):
        # The CHECK constraints refused the claim. A scan must never end up with a weaker
        # record than it started with, so a prior proof is left standing untouched.
        if prior is not None and prior.proved:
            return prior
        downgraded = TapeRecord(
            chain=chain,
            token=token,
            coverage=PARTIAL,
            route=_route_for(counts),
            reason=f"scan: coverage_claim_rejected:{verdict.reason}"[:400],
            created_ms=created_ms,
            oldest_ms=counts["oldest_ms"],
            newest_ms=counts["newest_ms"],
            swaps_route=counts["route"],
            swaps_total=counts["total"],
            attempts=attempts,
            last_attempt_ms=now,
            next_attempt_ms=now + config.partial_retry_s * 1000,
        )
        store(downgraded, c, at_ms=now)
        return downgraded
    return record


# --------------------------------------------------------------------------------------
# the job
# --------------------------------------------------------------------------------------


#: A mint's last sign of life, for hot-window arithmetic. ``newest_ms`` is the newest
#: trade row we hold, ``covered_to_ms`` the last moment we looked and saw none newer, and
#: ``created_ms`` the fallback for a mint we have never collected. The route's window is
#: measured against *trade inactivity*, not age, which is why this is a MAX over evidence
#: of activity rather than anything derived from the launch timestamp alone.
_LAST_ACTIVITY_SQL = (
    "MAX(COALESCE(tt.newest_ms, 0), COALESCE(tt.covered_to_ms, 0), COALESCE(tk.created_ms, 0))"
)

#: A proved tape whose claim does not yet span the modelling horizon. NULL watermark
#: counts as shallow for reading, but see `_DEEPENABLE_SQL` for why it is not re-offered.
_SHALLOW_SQL = "(tt.covered_to_ms IS NULL OR tt.covered_to_ms - tk.created_ms < ?)"

#: The re-offer predicate: proved, shallow, has a watermark to top up from, and the mint
#: has traded recently enough that the route will still answer. Every clause is load-bearing
#: and each one is defended in :func:`candidates`.
_DEEPENABLE_SQL = (
    "(" + _PROVED_SQL.format(tt="tt", tk="tk") + " AND " + _SHALLOW_SQL
    + " AND tt.covered_to_ms IS NOT NULL"
    + " AND " + _LAST_ACTIVITY_SQL + " >= ?)"
)


def candidates(
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    limit: int = 500,
    config: TapeConfig = DEFAULT_CONFIG,
    at_ms: int | None = None,
    include_retired: bool = False,
) -> list[str]:
    """Mints worth spending a pump.fun request on, most perishable first.

    Two populations, and the second one did not exist before 2026-09-21.

    **Capture.** A mint with no proved-complete tape, exactly as before.

    **Deepen.** A mint whose tape *is* proved complete and is still too shallow to model.
    This is the case the old predicate made structurally impossible: it excluded every
    proved row, so a tape could be finished and never improved, and on ``data/kaiba.db``
    that produced 4,071 complete tapes with a median span of 41 seconds and 17 reaching
    five minutes. The tape was not shallow because the mints stopped trading -- on 57
    mints we did keep polling, trading was still running at 1.72 swaps/min in the 300-600 s
    bucket. It was shallow because we stopped looking. A tier-1 capture *cannot* be deep:
    it happens seconds after launch, and the trades that make the first five minutes have
    not happened yet.

    Four clauses decide whether a proved tape is re-offered, and each is a refusal of a
    cheaper answer:

    * **shallow** — ``covered_to_ms - created_ms < deep_enough_ms`` (300 s, the horizon of
      the published result this data is for; see :data:`PROVENANCE`). Depth is measured
      against the *coverage claim*, not against the first and last trade we hold, because
      a mint that goes quiet at 40 s and is then confirmed quiet at 300 s has a genuine
      five-minute tape and is precisely the population a rug model cares about.
    * **has a watermark** — ``covered_to_ms IS NOT NULL``. Without one, ``collect_token``
      cannot top up; it re-walks from scratch, and a re-walk that exhausts its page budget
      would write ``partial`` over a standing proof on no evidence at all. 17 rows are in
      this state and they are left alone deliberately. (:func:`collect_token` refuses the
      demotion too, belt and braces, because this predicate is not the only caller.)
    * **still answering** — last activity within :attr:`TapeConfig.deepen_max_idle_ms`,
      the **proved-answering** edge of the hot window (11 of 11 mints answered at 0.0-12.1
      min idle; 37 of 37 refused at 48.3-169.7 min; 6 of 6 refused at 81-162 min on
      2026-09-21). A first capture is worth a maybe because there is no second chance. A
      deepening re-ask is not: a 503 buys nothing and still spends a request on the
      provider we were rate-limited off on 2026-09-21.
    * **due** — ``next_attempt_ms``, which for a proved-but-shallow row is one scheduled
      visit at launch+300 s+settle rather than "whenever a pass happens to run". See
      :func:`next_deepen_attempt`.

    **Ordering is by deadline, not by novelty.** The old rule was newest launch first,
    which sorts by how likely a mint is to answer. That is the right proxy while every
    candidate is an unseen launch and the wrong one the moment some candidates are
    perishable: a mint 70 minutes old whose last trade was two minutes ago holds a
    40-second tape that becomes unreachable *forever* in ten minutes, while an unseen
    launch minted thirty seconds ago will still be there on the next pass. So deepenable
    mints come first, ordered by soonest expiry, and everything else keeps the old
    newest-first order. The deepenable set is naturally small -- it is bounded by mints
    that traded inside the last twelve minutes -- so this cannot starve capture; on the
    live database it was 233 of 4,074 proved rows.
    """
    c = _conn(conn)
    now = at_ms if at_ms is not None else now_ms()
    proved = _PROVED_SQL.format(tt="tt", tk="tk")
    idle_floor = now - int(config.deepen_max_idle_ms)
    deepen_sql = _DEEPENABLE_SQL
    sql = (
        "SELECT tk.address AS address, " + deepen_sql + " AS deepenable, "
        + _LAST_ACTIVITY_SQL + " AS last_activity_ms "
        "FROM tokens tk "
        "LEFT JOIN token_tape tt ON tt.chain = tk.chain AND tt.token = tk.address "
        "WHERE tk.chain=? "
        "  AND (NOT " + proved + " OR " + deepen_sql + ") "
        "  AND (tt.next_attempt_ms IS NULL OR tt.next_attempt_ms <= ?) "
        # Only mints this route can actually serve. Without it every StonkFun mint is
        # offered to the pump.fun collector: measured on a database holding 1,200 of them,
        # `candidates` returned 1,200 of 1,200, and `_decimals_for` answers for them too,
        # so each one would spend a real pump.fun request on a 404. That is the provider
        # we were rate-limited off on 2026-09-21 and whose budget is now deliberately
        # tight, and it is a hot-window route -- a request wasted here is launch coverage
        # for some other mint that we can never get back.
        "  AND (tk.launchpad IS NULL OR tk.launchpad NOT IN (" + _EXCLUDED_LAUNCHPADS_SQL + ")) "
    )
    deep_params = [int(config.deep_enough_ms), idle_floor]
    params: list[Any] = [
        *deep_params,          # SELECT ... AS deepenable
        chain.value,
        *deep_params,          # WHERE ... OR <deepenable>
        now,
        *_EXCLUDED_LAUNCHPADS,
    ]
    if not include_retired:
        sql += "  AND (tt.attempts IS NULL OR tt.attempts < ?) "
        params.append(int(config.max_attempts))
    sql += (
        "ORDER BY deepenable DESC, "
        "         CASE WHEN deepenable THEN last_activity_ms END ASC, "
        "         tk.created_ms IS NULL, tk.created_ms DESC LIMIT ?"
    )
    params.append(max(1, int(limit)))
    try:
        rows = fetch_all(c, sql, tuple(params))
    except sqlite3.Error as exc:
        log.warning("tape: candidate query failed (%s)", exc)
        return []
    return [str(r["address"]) for r in rows]


def deepenable(
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    limit: int = 500,
    config: TapeConfig = DEFAULT_CONFIG,
    at_ms: int | None = None,
    include_retired: bool = False,
) -> list[str]:
    """Exactly the proved-but-shallow mints :func:`candidates` would re-offer, same order.

    Split out so an operator can count the perishable population without running a pass,
    and so a test can assert the two queries agree rather than trusting that they do.

    The due and retirement clauses are here because the first version left them out and a
    live pass caught it inside two minutes: 14 mints that had *just* been deepened and
    were sitting on their next scheduled visit were still being reported as perishable.
    Nothing was collected twice -- ``candidates`` was right and this was the query that
    lied -- but a reporting function that overstates the work outstanding is how a budget
    argument gets made on a number that was never true.
    """
    c = _conn(conn)
    now = at_ms if at_ms is not None else now_ms()
    sql = (
        "SELECT tk.address AS address, " + _LAST_ACTIVITY_SQL + " AS last_activity_ms "
        "FROM tokens tk "
        "JOIN token_tape tt ON tt.chain = tk.chain AND tt.token = tk.address "
        "WHERE tk.chain=? AND " + _DEEPENABLE_SQL + " "
        "  AND (tt.next_attempt_ms IS NULL OR tt.next_attempt_ms <= ?) "
        "  AND (tk.launchpad IS NULL OR tk.launchpad NOT IN (" + _EXCLUDED_LAUNCHPADS_SQL + ")) "
    )
    params: list[Any] = [
        chain.value,
        int(config.deep_enough_ms),
        now - int(config.deepen_max_idle_ms),
        now,
        *_EXCLUDED_LAUNCHPADS,
    ]
    if not include_retired:
        sql += "  AND (tt.attempts IS NULL OR tt.attempts < ?) "
        params.append(int(config.max_attempts))
    sql += "ORDER BY last_activity_ms ASC LIMIT ?"
    params.append(max(1, int(limit)))
    try:
        rows = fetch_all(c, sql, tuple(params))
    except sqlite3.Error as exc:
        log.warning("tape: deepenable query failed (%s)", exc)
        return []
    return [str(r["address"]) for r in rows]


def run(
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    config: TapeConfig = DEFAULT_CONFIG,
    flow_config: FlowConfig | None = None,
    priority: Priority = Priority.RESEARCH,
    limit: int | None = None,
    budget_s: float | None = None,
    seed: bool = True,
    repair: bool = True,
    tokens: Sequence[str] | None = None,
) -> RunReport:
    """One coverage pass. Safe to run repeatedly; resumable; never raises.

    Stops on the first of the token limit or the wall-clock budget, leaving everything it
    has not reached eligible for the next run. Nothing it re-runs re-fetches a tape it
    already holds in full: a proved-complete token is topped up from its watermark, usually
    in a single page, and a token in backoff is not asked at all.
    """
    c = _conn(conn)
    report = RunReport()
    # perf_counter, not monotonic: on Windows CPython 3.12 monotonic() is GetTickCount64 at
    # ~15.6 ms resolution, so a short pass measured 0.0 s elapsed and reported an infinite
    # request rate as 0.0. The rate is one of the numbers this job exists to report.
    deadline = time.perf_counter() + float(budget_s if budget_s is not None else config.budget_s)

    if seed:
        seeded = seed_from_snapshots(chain, c)
        if seeded:
            log.info("tape: adopted %d proved tape(s) from curve_snapshots", seeded)
            report.note(f"seeded_from_snapshots:{seeded}")
        # Before the depth baseline is taken, because this costs nothing and changes what
        # the baseline is: it is the difference between measuring the tape and measuring
        # how long ago we last wrote down what the tape was.
        rec = reconcile(chain, c, config=config)
        report.reconciled = rec.watermarks_advanced
        if rec.rows_written:
            report.note(f"reconciled_rows:{rec.rows_written}")
            report.note(f"watermarks_advanced:{rec.watermarks_advanced}")

    report.complete_before = len(complete_tokens(chain, c))
    report.depth_before = depth_histogram(chain, c)

    if repair:
        marked, with_sig = repair_create_flags(chain, c)
        report.create_flags_set += marked
        report.note(f"tokens_with_create_signature:{with_sig}")
        copied, still_missing = repair_fee_payer(chain, c)
        report.fee_payers_set += copied
        report.note(f"fee_payer_still_null:{still_missing}")

    todo = (
        list(tokens)
        if tokens is not None
        else candidates(chain, c, limit=int(limit or config.max_tokens), config=config)
    )
    started = time.perf_counter()
    ledger_from_ms = now_ms()

    for token in todo[: int(limit or config.max_tokens)]:
        if time.perf_counter() >= deadline:
            report.note("budget_exhausted")
            break
        # Read before, not after: a deepening attempt is one that started from a standing
        # proof, and after `collect_token` has run the record no longer says what it
        # started as. Counting it afterwards would silently drop every deepen that failed.
        before = record_of(chain, token, c)
        was_deepen = before is not None and before.proved and not before.deep_enough(config)
        record, flow = collect_token(
            chain, token, c, config=config, flow_config=flow_config, priority=priority
        )
        if was_deepen:
            report.deepen_attempted += 1
            if (
                before is not None
                and record.covered_to_ms is not None
                and (before.covered_to_ms or 0) < record.covered_to_ms
            ):
                report.deepen_advanced += 1
        report.attempted += 1
        report.pages += (flow.pages if flow else 0)
        report.rows_written += (flow.rows_written if flow else 0)
        if record.coverage == COMPLETE:
            report.completed += 1
        elif record.coverage == PARTIAL:
            report.partial += 1
        else:
            report.unavailable += 1
        report.note(record.reason.split(":")[0][:60])
        if config.pause_between_tokens_s > 0:
            time.sleep(config.pause_between_tokens_s)

    report.elapsed_s = time.perf_counter() - started
    report.requests, report.rate_limited = _provider_calls_since(c, ledger_from_ms)
    report.complete_after = len(complete_tokens(chain, c))
    report.depth_after = depth_histogram(chain, c)
    report.skipped = max(0, len(todo) - report.attempted)

    _emit_summary(chain, report, c)
    return report


def _provider_calls_since(conn: sqlite3.Connection, since_ms: int) -> tuple[int, int]:
    """``(requests, rate_limited)`` the limiter recorded for this provider since ``since_ms``.

    The limiter charges and logs every outbound call, including the ones that came back
    503, so this is the only honest account of what we put on the wire. It over-counts if
    another process is hitting the same provider concurrently, which is the safe direction
    to be wrong in when the number is being used to argue we are being polite.
    """
    try:
        row = fetch_one(
            conn,
            "SELECT COUNT(*) AS n, SUM(CASE WHEN status='rate_limited' THEN 1 ELSE 0 END) AS limited "
            "FROM provider_calls WHERE provider=? AND ts_ms >= ?",
            (TRADE_PROVIDER, int(since_ms)),
        )
    except sqlite3.Error as exc:
        log.debug("tape: provider call ledger unreadable (%s)", exc)
        return 0, 0
    if row is None:
        return 0, 0
    return (_int(row["n"]) or 0), (_int(row["limited"]) or 0)


def _emit_summary(chain: Chain, report: RunReport, conn: sqlite3.Connection) -> None:
    """Telemetry must never break a collection pass."""
    try:
        from kaiba.core import events as ev

        ev.emit(
            EventKind.SYSTEM,
            {"job": "ingest.tape", **report.as_dict()},
            chain=chain,
            subject="ingest.tape",
            conn=conn,
        )
    except Exception as exc:  # noqa: BLE001 - telemetry is not load-bearing
        log.debug("tape: could not record run summary (%s)", exc)


__all__ = [
    "COMPLETE",
    "DEFAULT_CONFIG",
    "DEPTH_BUCKETS_MS",
    "FEE_PAYER_NOTE",
    "MODEL_ID",
    "PARTIAL",
    "PROVENANCE",
    "ROUTE_MIXED",
    "ROUTE_NONE",
    "ROUTE_TRADES",
    "ROUTE_WALLET",
    "UNAVAILABLE",
    "WALLET_SOURCE",
    "Knob",
    "ReconcileReport",
    "RunReport",
    "TapeConfig",
    "TapeRecord",
    "FLOW_PAGES_KEY",
    "FLOW_REASON_KEY",
    "HOT_WINDOW_ANSWERED_MAX_IDLE_MIN",
    "HOT_WINDOW_REFUSED_2026_09_21_MIN_IDLE_MIN",
    "HOT_WINDOW_REFUSED_MIN_IDLE_MIN",
    "advance_watermark",
    "candidates",
    "collect_token",
    "complete_tokens",
    "completeness",
    "coverage_summary",
    "create_signature",
    "deepenable",
    "depth_histogram",
    "is_complete",
    "next_deepen_attempt",
    "observed_to_ms",
    "reconcile",
    "record_of",
    "record_scan_capture",
    "repair_create_flags",
    "repair_fee_payer",
    "run",
    "seed_from_snapshots",
    "store",
]
