"""Live wallet tracking: an auditable watchlist, two measured detection routes, and the
entity-counted confluence window.

Three lanes fail at the same place. ``confluence-5`` needs five independent entities
buying one token inside 120 seconds, ``trusted-copy`` allows 20 seconds of copy delay, and
``sm-trenches`` needs three smart-degen wallets across two entities — all live questions
that a scheduled backfill of wallet history cannot answer, because by the time the
backfill runs the window has closed. This module is the missing instrument.

**It is an instrument, not a trigger, and the distinction is load-bearing.**
``docs/research/13-validation-and-copytrading-2026.md`` Part A is unambiguous:

* Wallet-skill persistence is *not established*. The one proper out-of-sample test finds
  "at most a modest uplift" and the authors warn it may be ex-post contamination (A1).
* The top of a realised-PnL leaderboard is partly settlement plumbing — rank 1 in that
  paper's table had 1,793 trades and **zero buys** (A1).
* Copy trading is provably negative for the copier. In the only paired leader/copier
  measurement, four of five wallet selectors produce a profitable leader and a *losing*
  copier at **zero latency**, a 6–15 pp imitation penalty before any delay (A3).
* Ranking N wallets by a noisy statistic yields a ~5-sigma "best" wallet when every wallet
  in the population has zero edge (A2).

So a detection here says *a watched address acted*. It never says the address was right,
it carries no score, and nothing in this module may promote a wallet to trusted — the
``tier`` column is CHECK-constrained to ``observe`` and ``candidate`` so a later caller
cannot do it either. Only measured forward performance against a matched control
(``kaiba/learning/validation.py``) could justify trust, and nothing here measures that.

--------------------------------------------------------------------------------------
The per-token / per-wallet tension, and how it actually resolves
--------------------------------------------------------------------------------------

The brief framed a real tension: the free complete source is indexed by *mint* and the
thing we want to watch is a *wallet*. Measured on this machine on 2026-09-20, the two
routes are:

**Route T — ``frontend-api-v3.pump.fun/trades/{chain_id}/{mint}``.** Free, keyless,
complete per mint. On ten mints taken from pump.fun's own ``last_trade_timestamp``
ordering, the three genuinely hot ones returned a newest trade **1.0 s, 2.1 s and 2.1 s
old**; request RTT was 953–1,313 ms (p50 1,094 ms). Three of ten mints answered **503**,
so the route's own availability is ~70% per call and a miss is a miss, not a retry
success. Indexed by mint, so per-wallet recall equals the chance that a watched wallet
happened to trade a mint we were already polling.

**Route W — Helius ``getTransactionsForAddress``.** Indexed by address.
``transactionDetails="full"`` at ``limit=25`` cost **10 credits**, took **1,938 ms**, and
returned 215 KB containing everything needed to name the mint and the side from
``pre/postTokenBalances`` plus the owner's lamport delta. ``transactionDetails="signatures"``
at ``limit=1000`` also cost **10 credits** and carries the ``err`` field, which is the
transaction-failure-rate screen for free.

**The resolution is that these are two different questions, not one.** ``confluence-5``
asks "did five entities buy *this mint* in 120 s" — that is intrinsically per-token, and
Route T answers it completely and for nothing. ``trusted-copy`` asks "did *this wallet*
just buy" — that is intrinsically per-wallet, and only Route W answers it. Both are
implemented; neither pretends to be the other.

--------------------------------------------------------------------------------------
What Route W costs, which is the answer to the 20-second question
--------------------------------------------------------------------------------------

One poll is 10 credits, so ``N`` wallets on a ``T``-second interval costs
``N * (86400/T) * 10`` credits a day. Against the free plan's 1,000,000 a month
(we have used 9,803 — 0.98% — so effectively the whole allowance is available):

=========  ==================  =====================  ==========================
 wallets    poll interval       credits / 30 days      verdict
=========  ==================  =====================  ==========================
 **1**     **15 s**             **1,728,000**          **1.7x — one wallet already
                                                       breaks the free tier at a
                                                       cadence the lane could use**
 10         10 s                25,920,000             26x the free allowance
 10         20 s                12,960,000             13x
 10         60 s                 4,320,000             4.3x
 10         260 s                  996,923             the free-tier ceiling
 10         600 s                  432,000             comfortable
=========  ==================  =====================  ==========================

A poll interval of ``T`` gives a mean detection lag of roughly ``T/2`` and a worst case of
``T``, plus RTT and Helius's own indexing lag. RTT was MEASURED over 56 consecutive polls
on 2026-09-20: p50 **1,618 ms**, p95 **1,985 ms**, max **3,188 ms**. **Ten watched wallets
inside the free tier means a 260-second poll interval, so a p95 detection lag around 250
seconds — about twelve times ``trusted-copy``'s configured ``max_copy_delay_s: 20``.** The
first row is the blunter version of the same fact: even *one* wallet, polled at 15 s,
costs 1.73x the whole monthly allowance. Polling fast
enough for 20 seconds costs roughly 13 M credits a month for ten wallets, i.e. ~$60/month
of Helius overage at $5 per extra million, and that buys detection latency only — the
imitation penalty in A3 is charged at *zero* latency and is unaffected by paying it.

There is one affordable route to a genuinely low-latency per-wallet feed and it is already
half-built in this repository: **Helius webhooks**, which ``kaiba/providers/helius.py``
prices at 1 credit per push (``ROUTE_WEBHOOK``) and already receives, HMAC-verifies and
trims in :func:`kaiba.providers.helius.handle_webhook`. Pushes are charged only when
something happens, so ten wallets at a hundred transactions a day is ~30,000 credits a
month. It is blocked on one thing this machine does not have: a publicly reachable HTTPS
endpoint. :func:`webhook_readiness` reports that gap rather than hiding it.

--------------------------------------------------------------------------------------
Admission, and why the screen runs before the watchlist and not before the score
--------------------------------------------------------------------------------------

Known-bad shapes are rejected at ingest, because a scoring-time subtraction can be
absorbed by a large realised profit:

* **Sell-only and buy-starved addresses** — reused wholesale from
  :mod:`kaiba.intelligence.grade` (:func:`~kaiba.intelligence.grade.sell_only_rejection`,
  :func:`~kaiba.intelligence.grade.buy_share`). Not reimplemented here; a second copy of
  that rule would eventually disagree with the first.
* **Transaction failure rate** — ``docs/TRADING-METHOD.md`` §2 lists this as live in
  ``grade.py``. **It is not implemented there.** ``grep`` for ``fail`` in that module
  finds only exception handling, and ``WalletEvidence`` has no field for it. The
  documentation is ahead of the code, so the rule is implemented here, at ingest, where
  the brief asked for it. Measured on our own nine C-or-better wallets it is not a
  hypothetical: three of nine run at **35.8%, 23.3% and 23.2%** failure, reproducing the
  research's 36%/49% leaderboard finding on our own universe.
* **Hard quarantine tags** — :data:`kaiba.core.schemas.HARD_QUARANTINE_TAGS`.
* **Unknown is not good.** A wallet whose failure rate could not be measured is
  *unassessed*, and unassessed does not get in. Every screen field is a value plus an
  :class:`~kaiba.core.schemas.EvidenceBasis`, and ``UNAVAILABLE`` blocks admission.

Rate limits: Route T reuses the ``pumpfun`` limiter bucket and the ``coins`` family that
``token_flow`` and the scanner already share, so a 429 on any of them cools all of them.
Route W goes through :mod:`kaiba.providers.helius`, which checks the monthly credit ledger
before every call. Nothing here opens its own HTTP client or its own bucket.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import statistics
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kaiba.core.config import get_risk
from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump, jload
from kaiba.core.events import emit
from kaiba.core.limiter import Priority
from kaiba.core.schemas import (
    EVM_CHAINS,
    HARD_QUARANTINE_TAGS,
    Chain,
    EventKind,
    EvidenceBasis,
    Grade,
    Lane,
    Measure,
    Receipt,
    WalletTag,
    normalize_address,
    now_ms,
)
from kaiba.ingest import token_flow
from kaiba.intelligence import entity as entity_mod
from kaiba.intelligence import grade as grade_mod
from kaiba.providers import helius

log = logging.getLogger(__name__)

MODEL_ID = "kaiba-tracker-v1"

#: Route identifiers, written into ``tracker_detections.route`` and ``tracker_polls.route``.
ROUTE_PUMPFUN = "pumpfun:trades"
ROUTE_HELIUS = "helius:signatures"
#: Not a poll route of ours: the GMGN wallet feeds (:mod:`kaiba.ingest.gmgn_feeds`) already
#: report every trade of a wallet they list. A wallet admitted from those feeds is observed
#: through them and is excluded from the paid per-wallet Helius poll (see
#: :func:`pollable_addresses`); polling it again would pay 10 credits every interval for
#: trades we are already receiving.
ROUTE_GMGN_FEED = "gmgn:feed"

#: ``tracker_watchlist.source`` for a wallet admitted on a vendor cohort label:
#: ``gmgn:cohort:<label>``. It names the label and nothing else, so a reader of the
#: watchlist can never mistake it for a grade (``grade:<model>``) or for a discovery run.
COHORT_SOURCE_PREFIX = "gmgn:cohort:"

#: Admission policies, recorded in ``tracker_watchlist.meta_json["admission_policy"]`` so
#: a rescreen re-applies the rules the wallet was let in under.
ADMISSION_POLICY_STANDARD = "standard"
ADMISSION_POLICY_COHORT = "cohort"

#: Screen blocker codes a *cohort* admission may waive, and why each one is waivable:
#:
#: * ``grade_missing`` — the wallet is ungraded. Under the standard policy that is a
#:   refusal ("unassessed is not average"). Under the cohort policy the vendor label is the
#:   admission source, so the absence of our own grade is recorded, not enforced. A grade
#:   we *did* measure is never waived: our D beats their label.
#: * ``failure_rate_unmeasurable`` — the tape has no signature source for the chain (only
#:   Helius on Solana carries the ``err`` field). The gate is not skipped: the wallet enters
#:   at the lower tier with the unknown on its row, and a rate that *is* measured over the
#:   ceiling still refuses.
#: * ``failure_rate_disabled`` — the caller chose not to spend credits this run. Same
#:   treatment as unmeasurable, and the reason says which it was.
COHORT_WAIVABLE_CODES: frozenset[str] = frozenset(
    {"grade_missing", "failure_rate_unmeasurable", "failure_rate_disabled"}
)

#: The tag values ``kaiba.execution.lanes.sm_trenches`` reads out of ``wallets.tags_json``
#: (its ``SMART_TAGS``). Spelled here rather than imported so the intelligence layer does
#: not import the execution layer; ``tests/test_bsc_lane_inputs.py`` asserts the two sets
#: are identical so they cannot drift apart.
LANE_SMART_TAGS: frozenset[str] = frozenset({"smart_money", "pump_smart", "renowned", "top_trader"})

#: ``wallet_scores.archetype`` values the same lane accepts as its fallback route.
LANE_SMART_ARCHETYPES: frozenset[str] = frozenset({"smart_money", "top_trader"})

#: Provenance of every number and mapping the cohort path introduces. A test asserts that
#: each entry is labelled and that the ones nobody measured say INVENTED.
COHORT_PROVENANCE: dict[str, tuple[str, str]] = {
    "cohort_tier_measured": (
        "INVENTED",
        "candidate: a cohort wallet whose execution we measured clean joins the forward-"
        "tracking cohort so the vendor label gets tested against a control; neither tier "
        "confers trust and nothing downstream sizes on either",
    ),
    "cohort_tier_unmeasured": (
        "INVENTED",
        "observe: the lower of the two tiers for a wallet whose failure rate the tape could "
        "not compute; the gate was applied and came back unknown",
    ),
    "rescreen_max_wallets": (
        "DERIVED",
        "50 wallets x 10 credits = 500 credits/day = 15,500 per 31-day month = 1.6% of the "
        "free tier; before this bound a daily rescreen of a 700-wallet cohort would have cost "
        "217,000 credits a month",
    ),
    "smart_cohort_tag_mapping": (
        "CITED",
        "GMGN maker_info.tags smart_degen / app_smart_money / launchpad_smart -> "
        "WalletTag.SMART_MONEY, written as a derived tag on admission only; GMGN defines "
        "smart money as 'wallet who often earns money' with no published method "
        "(docs/research/13, Part A), so the label is observed, never trusted",
    ),
}

#: MEASURED 2026-09-20: ``transactionDetails="full"`` at ``limit=25`` and
#: ``transactionDetails="signatures"`` at ``limit=1000`` each moved the Helius ledger by
#: exactly 10. The route is billed "10 credits per 100 returned, rounded up, 10 minimum",
#: so any poll that returns 100 rows or fewer costs this.
HELIUS_POLL_CREDITS = 10

#: The free route costs nothing. This is a measured zero, not an unknown.
PUMPFUN_POLL_CREDITS = 0

#: Wrapped SOL. A delta in this mint is the quote leg of a swap, never the asset bought.
WSOL_MINT = "So11111111111111111111111111111111111111112"

#: pump.fun's own listing, ordered by most recent trade. This is how the free per-token
#: route gets a candidate set that is actually trading: our ``tokens`` table is ordered by
#: creation, and a probe of its eight newest rows found six with no trade route at all and
#: two whose newest trade was ~326 s old.
PUMPFUN_COINS_URL = "https://frontend-api-v3.pump.fun/coins"

#: Same limiter bucket and same cool-down family as ``token_flow`` and the scanner.
COINS_ENDPOINT = "coins.list"


class Tier(StrEnum):
    """Watchlist tiers.

    There is deliberately no ``TRUSTED``. Adding one would need a migration that widens
    the CHECK constraint in ``023_tracker.sql``, which is exactly the friction intended:
    the research says wallet-skill persistence is unestablished, so trust cannot be
    conferred by observation, only by measured forward performance against a control.
    """

    OBSERVE = "observe"
    CANDIDATE = "candidate"


#: Guard against a future edit quietly re-adding trust. Cheap, and it fails at import.
assert "trusted" not in {t.value for t in Tier}, "the tracker must never mint trust"


class Action(StrEnum):
    ADMITTED = "admitted"
    REFUSED = "refused"
    REMOVED = "removed"
    RETIERED = "retiered"
    RESCREENED = "rescreened"


# --------------------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrackerConfig:
    """Every number is annotated MEASURED, CITED or INVENTED."""

    # -- admission screen ---------------------------------------------------------------

    #: INVENTED, anchored on two published numbers. Jupiter's own sybil filter excludes
    #: above 50% as "code instead of food" (docs/EDGE-AND-VARIABLES.md §1); the research's
    #: leaderboard check found bots at 36% and 49%. 20% is stricter than both because the
    #: watchlist exists to find *judgement* and a wallet whose orders fail one in five is
    #: submitting speculatively. Our own C-or-better set splits cleanly around it:
    #: 1.6/3.3/3.7/4.0/5.8/8.0% on one side, 23.2/23.3/35.8% on the other.
    max_tx_failure_rate: float = 0.20

    #: INVENTED. A failure rate over ten signatures is noise; 50 makes a 20% threshold
    #: mean at least ten observed failures.
    min_signatures_for_failure_rate: int = 50

    #: MEASURED: 10 credits flat for up to 1000 signatures, so take all of them.
    screen_signature_limit: int = 1000

    #: Refuse any wallet whose grade is worse than this. ``UNSCORED`` is refused too —
    #: an ungraded wallet is unassessed, and unassessed is not average.
    min_grade: Grade = Grade.C

    # -- detection ----------------------------------------------------------------------

    #: MEASURED: ``limit=25`` full transactions is 10 credits and 215 KB. Raising it to
    #: 100 is the same 10 credits but ~860 KB per poll for a wallet that will almost never
    #: have produced 100 transactions between two polls.
    wallet_poll_limit: int = 25

    #: INVENTED: the free-tier ceiling for ten wallets is 260 s (see the module docstring).
    #: The default is deliberately *slower* than the lane needs, because the honest
    #: default is the affordable one and the lane's own viability is the finding.
    wallet_poll_interval_s: int = 300

    #: MEASURED: the pumpfun limiter entry does not exist, so the provider takes the
    #: default ``min_interval_ms=1000``. One mint per second is the hard pacing ceiling
    #: and ``token_flow``'s docstring records 73 consecutive pages at that rate with zero
    #: 429s. Do not raise this without raising the limiter entry first.
    token_sweep_size: int = 30

    #: MEASURED: one page of 50 trades covers ~120 s of tape on a hot mint (50 of 50
    #: trades on the three hottest mints were inside the last 120 s, which is also the
    #: warning that on the very hottest mints one page is *not* enough).
    token_page_limit: int = 100

    # -- confluence ---------------------------------------------------------------------

    #: CITED: ``config/risk.yaml`` lane ``confluence-5`` params. Read from the live config
    #: by :func:`config_from_risk`; these are the fallbacks if the lane block is absent.
    confluence_window_s: int = 120
    confluence_min_entities: int = 5
    confluence_min_buy_usd: Decimal = Decimal("50")

    #: CITED: ``config/risk.yaml`` lane ``trusted-copy`` param ``max_copy_delay_s``.
    copy_delay_budget_s: int = 20

    #: CITED: ``config/risk.yaml`` lane ``sm-trenches`` (``min_smart_degen`` and
    #: ``min_independent_entities``).
    trenches_min_wallets: int = 3
    trenches_min_entities: int = 2

    #: CITED: ``config/risk.yaml`` lane ``sm-trenches`` ``window_s``; the lane default.
    trenches_window_s: int = 300

    # -- cohort admission ---------------------------------------------------------------

    #: INVENTED (see :data:`COHORT_PROVENANCE`): tier for a cohort wallet whose failure
    #: rate was measured and cleared the ceiling.
    cohort_tier_measured: Tier = Tier.CANDIDATE

    #: INVENTED (see :data:`COHORT_PROVENANCE`): the lower tier, for a cohort wallet whose
    #: failure rate the tape could not compute.
    cohort_tier_unmeasured: Tier = Tier.OBSERVE

    #: DERIVED (see :data:`COHORT_PROVENANCE`): how many wallets one rescreen pass may
    #: pay for. Least-recently-checked first, so a large watchlist rotates through.
    rescreen_max_wallets: int = 50

    # -- bookkeeping --------------------------------------------------------------------

    #: How far back a confluence scan reads ``swaps`` by default.
    scan_lookback_s: int = 3600


DEFAULT_CONFIG = TrackerConfig()


def config_from_risk(base: TrackerConfig = DEFAULT_CONFIG) -> TrackerConfig:
    """Overlay the live lane parameters from ``config/risk.yaml`` onto ``base``.

    The lane thresholds belong to the operator, not to this module. Reading them here
    means the viability verdict is measured against what is actually configured rather
    than against a copy of it that can drift.
    """
    try:
        risk = get_risk()
    except Exception as exc:  # noqa: BLE001 - a missing config must not break tracking
        log.warning("tracker: risk config unreadable, using defaults (%s)", exc)
        return base

    conf = risk.lane(Lane.CONFLUENCE_5).params or {}
    copy = risk.lane(Lane.TRUSTED_COPY).params or {}
    trenches = risk.lane(Lane.SM_TRENCHES).params or {}

    def _int(source: Mapping[str, Any], key: str, fallback: int) -> int:
        try:
            return int(source[key])
        except (KeyError, TypeError, ValueError):
            return fallback

    min_usd = base.confluence_min_buy_usd
    if "min_buy_usd" in conf:
        try:
            min_usd = Decimal(str(conf["min_buy_usd"]))
        except (InvalidOperation, TypeError, ValueError):
            pass

    return TrackerConfig(
        max_tx_failure_rate=base.max_tx_failure_rate,
        min_signatures_for_failure_rate=base.min_signatures_for_failure_rate,
        screen_signature_limit=base.screen_signature_limit,
        min_grade=base.min_grade,
        wallet_poll_limit=base.wallet_poll_limit,
        wallet_poll_interval_s=base.wallet_poll_interval_s,
        token_sweep_size=base.token_sweep_size,
        token_page_limit=base.token_page_limit,
        confluence_window_s=_int(conf, "window_s", base.confluence_window_s),
        confluence_min_entities=_int(conf, "min_entities", base.confluence_min_entities),
        confluence_min_buy_usd=min_usd,
        copy_delay_budget_s=_int(copy, "max_copy_delay_s", base.copy_delay_budget_s),
        # The lane's keys are ``min_smart_degen`` / ``min_independent_entities``
        # (``lanes.DEFAULT_PARAMS`` and ``config/risk.yaml``). The older ``min_wallets`` /
        # ``min_entities`` spellings are kept as fallbacks; before this they were the only
        # keys read, so the file's values were never reaching this config.
        trenches_min_wallets=_int(
            trenches, "min_smart_degen", _int(trenches, "min_wallets", base.trenches_min_wallets)
        ),
        trenches_min_entities=_int(
            trenches,
            "min_independent_entities",
            _int(trenches, "min_entities", base.trenches_min_entities),
        ),
        trenches_window_s=_int(trenches, "window_s", base.trenches_window_s),
        cohort_tier_measured=base.cohort_tier_measured,
        cohort_tier_unmeasured=base.cohort_tier_unmeasured,
        rescreen_max_wallets=base.rescreen_max_wallets,
        scan_lookback_s=base.scan_lookback_s,
    )


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------


def _conn(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    return conn if conn is not None else get_conn()


def _norm(address: str, chain: Chain) -> str:
    try:
        return normalize_address(address, chain)
    except ValueError:
        return address.strip()


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile. ``None`` on an empty sample, never 0.

    Nearest-rank rather than interpolated on purpose: a p95 latency should be a latency we
    actually observed, not an average of two we did not.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(pct / 100.0 * len(ordered))
    return ordered[max(0, min(len(ordered) - 1, rank - 1))]


# --------------------------------------------------------------------------------------
# the admission screen
# --------------------------------------------------------------------------------------


class Screen(BaseModel):
    """What we established about a wallet before deciding whether to watch it.

    Every measurable carries a basis. A field that is ``None`` with an ``UNAVAILABLE``
    basis is *not measured*, which is a blocker rather than a pass — an unassessed wallet
    is unassessed, never average.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    chain: Chain
    address: str

    grade: Grade | None = None
    score: float | None = None
    grade_basis: EvidenceBasis = EvidenceBasis.UNAVAILABLE

    buy_share: Decimal | None = None
    buy_share_basis: EvidenceBasis = EvidenceBasis.UNAVAILABLE
    observed_buys: int | None = None
    observed_sells: int | None = None

    tx_failure_rate: float | None = None
    tx_failure_basis: EvidenceBasis = EvidenceBasis.UNAVAILABLE
    signatures_seen: int | None = None
    signatures_failed: int | None = None
    last_activity_ms: int | None = None

    tags: list[WalletTag] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    #: One machine-readable code per entry of ``blockers``, same order. An admission
    #: policy decides on codes; the text is for the human reading the audit log.
    blocker_codes: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    receipts: list[Receipt] = Field(default_factory=list)

    @property
    def admissible(self) -> bool:
        """A wallet is admissible only when nothing blocked it and nothing was unknown."""
        return not self.blockers and not self.unknowns

    def block(self, code: str, text: str) -> None:
        self.blockers.append(text)
        self.blocker_codes.append(code)

    def as_dict(self) -> dict[str, Any]:
        return {
            "grade": self.grade.value if self.grade else None,
            "score": self.score,
            "grade_basis": self.grade_basis.value,
            "buy_share": None if self.buy_share is None else str(self.buy_share),
            "buy_share_basis": self.buy_share_basis.value,
            "observed_buys": self.observed_buys,
            "observed_sells": self.observed_sells,
            "tx_failure_rate": self.tx_failure_rate,
            "tx_failure_basis": self.tx_failure_basis.value,
            "signatures_seen": self.signatures_seen,
            "signatures_failed": self.signatures_failed,
            "last_activity_ms": self.last_activity_ms,
            "tags": [t.value for t in self.tags],
            "blockers": list(self.blockers),
            "blocker_codes": list(self.blocker_codes),
            "unknowns": list(self.unknowns),
            "model": MODEL_ID,
        }


def measure_failure_rate(
    chain: Chain,
    address: str,
    conn: sqlite3.Connection | None = None,
    *,
    config: TrackerConfig = DEFAULT_CONFIG,
) -> tuple[float | None, dict[str, Any], Receipt]:
    """Fraction of this address's recent signatures that failed on chain.

    MEASURED cost: 10 credits flat for up to 1,000 signatures, ~1.6 s. The ``err`` field
    on each row is non-null exactly when the transaction failed, so this is a direct
    on-chain count and not a provider's summary of one.

    Returns ``(rate_or_None, detail, receipt)``. The rate is ``None`` whenever the sample
    is too thin to divide, and the receipt then carries an ``UNAVAILABLE`` basis. It is
    never 0 on a failure to measure.
    """
    if chain is not Chain.SOL:
        return None, {"reason": "failure rate is only measurable on solana here"}, Receipt(
            provider=helius.PROVIDER,
            endpoint=helius.ROUTE_TRANSACTIONS,
            basis=EvidenceBasis.UNAVAILABLE,
            note=f"unsupported chain {chain.value}",
        )

    result, receipt = helius.get_transactions_for_address(
        address,
        limit=config.screen_signature_limit,
        transaction_details="signatures",
        ttl_s=30.0,
        priority=Priority.RESEARCH,
        conn=conn,
    )
    if result is None:
        return None, {"reason": receipt.note or "helius unavailable"}, receipt

    rows = [r for r in (result.get("data") or []) if isinstance(r, dict)]
    seen = len(rows)
    failed = sum(1 for r in rows if r.get("err") is not None)
    block_times = [t for t in (_int_or_none(r.get("blockTime")) for r in rows) if t is not None]
    detail: dict[str, Any] = {
        "signatures_seen": seen,
        "signatures_failed": failed,
        "last_activity_ms": max(block_times) * 1000 if block_times else None,
    }
    if seen < config.min_signatures_for_failure_rate:
        detail["reason"] = (
            f"{seen} signatures is below the {config.min_signatures_for_failure_rate} needed "
            "to divide; a failure rate over a handful of transactions is noise"
        )
        return None, detail, Receipt(
            provider=receipt.provider,
            endpoint=receipt.endpoint,
            observed_at_ms=receipt.observed_at_ms,
            basis=EvidenceBasis.UNAVAILABLE,
            note=detail["reason"],
        )
    return failed / seen, detail, receipt


def screen_wallet(
    chain: Chain,
    address: str,
    conn: sqlite3.Connection | None = None,
    *,
    config: TrackerConfig = DEFAULT_CONFIG,
    check_failure_rate: bool = True,
) -> Screen:
    """Run every admission rule. Pure of side effects except the provider call.

    The sell-only and buy-starved rules are :mod:`kaiba.intelligence.grade`'s, called
    rather than copied. The failure-rate rule is this module's, because ``grade.py`` does
    not in fact implement it (see the module docstring).
    """
    c = _conn(conn)
    addr = _norm(address, chain)
    screen = Screen(chain=chain, address=addr)

    # Admission consumes only shape, tags and the stored score. Reconstructing
    # every historical episode (and early/creator metrics) here is unused work
    # multiplied by every cohort wallet. Preserve grade.py's raw-count precedence
    # and provider fallback, including None rather than zero for an empty tape.
    wallet = fetch_one(c, "SELECT tags_json, meta_json FROM wallets WHERE chain=? AND address=?",
                       (chain.value, addr))
    meta = jload(wallet["meta_json"], {}) if wallet else {}
    provider = {k: meta[k] for k in ("buy_count", "sell_count") if meta.get(k) is not None}
    # INDEXED BY idx_swaps_wallet: see grade.grade_address -- without statistics the
    # planner walked the whole chain's swaps for every cohort wallet.
    sides = fetch_one(c, "SELECT SUM(side='buy') AS buys, SUM(side='sell') AS sells "
                        "FROM swaps INDEXED BY idx_swaps_wallet WHERE chain=? AND wallet=?",
                      (chain.value, addr))
    evidence = grade_mod.WalletEvidence(
        address=addr, chain=chain,
        tags=grade_mod._tags_from(jload(wallet["tags_json"], []) if wallet else []),
        provider_stats=grade_mod.ProviderStats.model_validate(provider) if provider else None,
        observed_buys=int(sides["buys"]) if sides and sides["buys"] is not None else None,
        observed_sells=int(sides["sells"]) if sides and sides["sells"] is not None else None,
    )
    screen.tags = list(evidence.tags)
    screen.observed_buys = evidence.observed_buys
    screen.observed_sells = evidence.observed_sells

    # 1. grade -------------------------------------------------------------------------
    stored = grade_mod.load_score(chain, addr, c)
    if stored is None:
        screen.unknowns.append("grade")
        screen.block("grade_missing", "never graded: an unassessed wallet is unassessed, not average")
    else:
        screen.grade = stored.grade
        screen.score = stored.score
        screen.grade_basis = EvidenceBasis.DERIVED
        order = [Grade.A, Grade.B, Grade.C, Grade.D, Grade.UNSCORED, Grade.QUARANTINED]
        if stored.grade in (Grade.UNSCORED, Grade.QUARANTINED):
            screen.block(f"grade_{stored.grade.value.lower()}", f"grade {stored.grade.value}")
        elif order.index(stored.grade) > order.index(config.min_grade):
            screen.block(
                "grade_below_floor",
                f"grade {stored.grade.value} is below the {config.min_grade.value} floor",
            )

    # 2. hard quarantine tags ------------------------------------------------------------
    hard = set(screen.tags) & HARD_QUARANTINE_TAGS
    if hard:
        screen.block(
            "quarantine_tags",
            "hard quarantine tags: " + ", ".join(sorted(t.value for t in hard)),
        )

    # 3. sell-only / buy-starved — grade.py's rules, not a second copy --------------------
    share = grade_mod.buy_share(evidence)
    screen.buy_share = share
    if share is None:
        screen.buy_share_basis = EvidenceBasis.UNAVAILABLE
        screen.unknowns.append("buy_share")
        screen.block(
            "shape_unassessable",
            f"fewer than {grade_mod.SELL_ONLY_MIN_TRADES} observed swaps, so the sell-only "
            "filter cannot run; a wallet we cannot shape-check does not enter",
        )
    else:
        screen.buy_share_basis = EvidenceBasis.VERIFIED_ONCHAIN
        rejection = grade_mod.sell_only_rejection(evidence)
        if rejection:
            screen.block("sell_only", rejection)
        elif share < grade_mod.BUY_STARVED_MAX_BUY_SHARE:
            screen.block(
                "buy_starved",
                f"buy-starved: {share:.2%} buy share, under grade.py's "
                f"{grade_mod.BUY_STARVED_MAX_BUY_SHARE:.0%} floor",
            )

    # 4. transaction failure rate --------------------------------------------------------
    if not check_failure_rate:
        screen.tx_failure_basis = EvidenceBasis.UNAVAILABLE
        screen.unknowns.append("tx_failure_rate")
        screen.block(
            "failure_rate_disabled",
            "failure rate not measured (screen ran with the check disabled)",
        )
    else:
        rate, detail, receipt = measure_failure_rate(chain, addr, c, config=config)
        screen.receipts.append(receipt)
        screen.signatures_seen = detail.get("signatures_seen")
        screen.signatures_failed = detail.get("signatures_failed")
        screen.last_activity_ms = detail.get("last_activity_ms")
        if rate is None:
            screen.tx_failure_basis = EvidenceBasis.UNAVAILABLE
            screen.unknowns.append("tx_failure_rate")
            screen.block(
                "failure_rate_unmeasurable",
                f"failure rate unmeasurable: {detail.get('reason', 'no reason given')}",
            )
        else:
            screen.tx_failure_rate = rate
            screen.tx_failure_basis = EvidenceBasis.VERIFIED_ONCHAIN
            if rate > config.max_tx_failure_rate:
                screen.block(
                    "failure_rate_over_ceiling",
                    f"transaction failure rate {rate:.1%} over {screen.signatures_seen} "
                    f"signatures exceeds the {config.max_tx_failure_rate:.0%} ceiling — that is "
                    "bot execution, not judgement",
                )
    return screen


# --------------------------------------------------------------------------------------
# admission policies
# --------------------------------------------------------------------------------------


class Verdict(BaseModel):
    """What a policy decided about a screen: enter or not, at which tier, and why.

    ``blockers`` are the rules that were *enforced*; ``waived`` are the ones the policy
    chose to record rather than enforce, each with the reason. A waived rule is still on
    the row, so "we did not look" can never later read as "we looked and it was fine".
    """

    policy: str
    admissible: bool
    tier: Tier
    blockers: list[str] = Field(default_factory=list)
    blocker_codes: list[str] = Field(default_factory=list)
    waived: list[str] = Field(default_factory=list)


def standard_verdict(screen: Screen, tier: Tier = Tier.OBSERVE) -> Verdict:
    """The original rule: nothing blocked and nothing unknown, or no entry."""
    return Verdict(
        policy=ADMISSION_POLICY_STANDARD,
        admissible=screen.admissible,
        tier=tier,
        blockers=list(screen.blockers),
        blocker_codes=list(screen.blocker_codes),
    )


def cohort_verdict(screen: Screen, *, config: TrackerConfig = DEFAULT_CONFIG) -> Verdict:
    """Admission on a vendor cohort label, with the screen's shape and execution gates kept.

    What is waived and why is spelled out on :data:`COHORT_WAIVABLE_CODES`. Everything
    else — a measured bad grade, a hard quarantine tag, a sell-only or buy-starved shape,
    a shape too thin to check, and above all a *measured* failure rate over the ceiling —
    refuses exactly as it does under the standard policy. The tier answers the one question
    the failure-rate gate could not: measured clean goes to ``config.cohort_tier_measured``,
    unknown goes to the lower ``config.cohort_tier_unmeasured``.
    """
    enforced: list[tuple[str, str]] = []
    waived: list[str] = []
    for code, text in zip(screen.blocker_codes, screen.blockers, strict=True):
        if code in COHORT_WAIVABLE_CODES:
            waived.append(f"{code}: {text}")
        else:
            enforced.append((code, text))
    measured = screen.tx_failure_rate is not None
    return Verdict(
        policy=ADMISSION_POLICY_COHORT,
        admissible=not enforced,
        tier=config.cohort_tier_measured if measured else config.cohort_tier_unmeasured,
        blockers=[text for _code, text in enforced],
        blocker_codes=[code for code, _text in enforced],
        waived=waived,
    )


# --------------------------------------------------------------------------------------
# the watchlist
# --------------------------------------------------------------------------------------


class WatchlistEntry(BaseModel):
    chain: Chain
    address: str
    tier: Tier
    status: str
    reason: str
    source: str
    added_by: str
    added_ms: int
    removed_ms: int | None = None
    removed_reason: str | None = None
    grade_at_add: Grade | None = None
    score_at_add: float | None = None
    screen: dict[str, Any] = Field(default_factory=dict)
    last_checked_ms: int | None = None
    cursor_sig: str | None = None
    cursor_ms: int | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def policy(self) -> str:
        """The admission policy this row entered under. Rows older than the field are
        standard: that was the only policy when they were written."""
        return str(self.meta.get("admission_policy") or ADMISSION_POLICY_STANDARD)

    @property
    def helius_polled(self) -> bool:
        """Whether the paid per-wallet route covers this row. Legacy rows: yes."""
        flag = self.meta.get("helius_poll")
        return True if flag is None else bool(flag)


def _log(
    conn: sqlite3.Connection,
    chain: Chain,
    address: str,
    action: Action,
    actor: str,
    detail: str,
    payload: Mapping[str, Any] | None = None,
) -> None:
    """Append to the audit log. Never updates, never deletes."""
    try:
        conn.execute(
            "INSERT INTO tracker_watchlist_log (chain, address, ts_ms, action, actor, detail, "
            "payload_json) VALUES (?,?,?,?,?,?,?)",
            (chain.value, address, now_ms(), action.value, actor, detail[:500], jdump(dict(payload or {}))),
        )
    except sqlite3.Error as exc:  # an audit failure must not silently drop the decision
        log.warning("tracker: watchlist log write failed for %s: %s", address[:10], exc)


def _row_to_entry(row: Mapping[str, Any]) -> WatchlistEntry:
    raw_grade = row.get("grade_at_add")
    return WatchlistEntry(
        chain=Chain(str(row["chain"])),
        address=str(row["address"]),
        tier=Tier(str(row["tier"])),
        status=str(row["status"]),
        reason=str(row["reason"]),
        source=str(row["source"]),
        added_by=str(row["added_by"]),
        added_ms=int(row["added_ms"]),
        removed_ms=_int_or_none(row.get("removed_ms")),
        removed_reason=row.get("removed_reason"),
        grade_at_add=Grade(str(raw_grade)) if raw_grade else None,
        score_at_add=float(row["score_at_add"]) if row.get("score_at_add") is not None else None,
        screen=jload(row.get("screen_json"), {}),
        last_checked_ms=_int_or_none(row.get("last_checked_ms")),
        cursor_sig=row.get("cursor_sig"),
        cursor_ms=_int_or_none(row.get("cursor_ms")),
        meta=jload(row.get("meta_json"), {}) or {},
    )


def admit(
    chain: Chain,
    address: str,
    *,
    reason: str,
    source: str,
    added_by: str,
    tier: Tier = Tier.OBSERVE,
    conn: sqlite3.Connection | None = None,
    config: TrackerConfig = DEFAULT_CONFIG,
    screen: Screen | None = None,
    check_failure_rate: bool = True,
    verdict: Verdict | None = None,
) -> WatchlistEntry | None:
    """Put a wallet on the watchlist if it survives the screen. ``None`` if it does not.

    ``reason``, ``source`` and ``added_by`` are required and have no defaults, because a
    membership nobody can explain is the leaderboard problem rebuilt in-house. A refusal
    is logged as loudly as an admission: the rejects are the part of the output with
    actual evidence behind them.

    ``verdict`` is the policy's reading of the screen. Without one the standard policy
    applies (nothing blocked, nothing unknown). With one, the verdict's ``admissible`` and
    ``tier`` decide, and its policy and waived rules are written to ``meta_json`` so a
    later rescreen applies the same rules the wallet entered under.
    """
    c = _conn(conn)
    addr = _norm(address, chain)
    if not reason.strip() or not source.strip() or not added_by.strip():
        raise ValueError("admit() requires a non-empty reason, source and added_by")

    result = screen or screen_wallet(chain, addr, c, config=config, check_failure_rate=check_failure_rate)
    decision = verdict if verdict is not None else standard_verdict(result, tier)
    # Owner watchlist policy is distinct from grading and cannot be waived by
    # an earlier cohort screen. Read the current stored grade, not its label.
    from kaiba.intelligence.watchlist_policy import rejection as active_rejection

    active_reason = active_rejection(c, chain, addr)
    if active_reason:
        decision.admissible = False
        decision.blocker_codes.append("active_grade_policy")
        decision.blockers.append(active_reason)
    if not decision.admissible:
        _log(
            c,
            chain,
            addr,
            Action.REFUSED,
            added_by,
            "; ".join(decision.blockers)[:500] or "unassessable",
            {
                "screen": result.as_dict(),
                "requested_tier": decision.tier.value,
                "source": source,
                "policy": decision.policy,
                "waived": list(decision.waived),
            },
        )
        emit(
            EventKind.WALLET_GRADED,
            {
                "tracker": MODEL_ID,
                "decision": "refused",
                "address": addr,
                "blockers": decision.blockers,
                "unknowns": result.unknowns,
                "policy": decision.policy,
            },
            chain=chain,
            subject=addr,
            conn=c,
        )
        log.info("tracker: refused %s (%s)", addr[:10], "; ".join(decision.blockers)[:160])
        return None

    # A wallet that is already being watched keeps the reason, source and author that put
    # it there. A second nomination is corroboration, not a rewrite: "who put this wallet
    # here and why" must keep answering with the decision that actually admitted it. The
    # extra source is appended to the log so the corroboration is not lost either.
    existing = get_entry(chain, addr, c)
    if existing is not None and existing.status == "active":
        _log(
            c, chain, addr, Action.RESCREENED, added_by,
            f"already watched since {existing.added_ms} via {existing.source}; "
            f"also nominated by {source}",
            {"screen": result.as_dict(), "additional_source": source, "reason": reason.strip()},
        )
        try:
            c.execute(
                "UPDATE tracker_watchlist SET screen_json=?, last_checked_ms=? "
                "WHERE chain=? AND address=?",
                (jdump(result.as_dict()), now_ms(), chain.value, addr),
            )
        except sqlite3.Error as exc:
            log.warning("tracker: screen refresh failed for %s: %s", addr[:10], exc)
        return existing

    stamp = now_ms()
    meta = {
        "admission_policy": decision.policy,
        "waived": list(decision.waived),
        # The paid per-wallet poll is for wallets no free route reports on. A cohort
        # wallet is reported by the feed that nominated it, so it is not Helius-polled.
        "helius_poll": decision.policy == ADMISSION_POLICY_STANDARD,
        "poll_route": ROUTE_HELIUS if decision.policy == ADMISSION_POLICY_STANDARD else ROUTE_GMGN_FEED,
    }
    try:
        c.execute(
            "INSERT INTO tracker_watchlist (chain, address, tier, status, reason, source, added_by, "
            " added_ms, grade_at_add, score_at_add, screen_json, last_checked_ms, meta_json) "
            "VALUES (?,?,?,'active',?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(chain, address) DO UPDATE SET tier=excluded.tier, status='active', "
            " reason=excluded.reason, source=excluded.source, added_by=excluded.added_by, "
            " added_ms=excluded.added_ms, removed_ms=NULL, removed_reason=NULL, "
            " grade_at_add=excluded.grade_at_add, score_at_add=excluded.score_at_add, "
            " screen_json=excluded.screen_json, last_checked_ms=excluded.last_checked_ms, "
            " meta_json=excluded.meta_json",
            (
                chain.value,
                addr,
                decision.tier.value,
                reason.strip(),
                source.strip(),
                added_by.strip(),
                stamp,
                result.grade.value if result.grade else None,
                result.score,
                jdump(result.as_dict()),
                stamp,
                jdump(meta),
            ),
        )
    except sqlite3.Error as exc:
        log.warning("tracker: admit failed for %s: %s", addr[:10], exc)
        return None

    _log(c, chain, addr, Action.ADMITTED, added_by, reason.strip(),
         {"screen": result.as_dict(), "tier": decision.tier.value, "source": source,
          "policy": decision.policy, "waived": list(decision.waived)})
    emit(
        EventKind.WALLET_GRADED,
        {
            "tracker": MODEL_ID,
            "decision": "admitted",
            "address": addr,
            "tier": decision.tier.value,
            "reason": reason.strip(),
            "source": source.strip(),
            "policy": decision.policy,
            "grade": result.grade.value if result.grade else None,
            "tx_failure_rate": result.tx_failure_rate,
            # Said out loud on the bus so nothing downstream can read admission as trust.
            "note": "observation only; the tracker never promotes a wallet to trusted",
        },
        chain=chain,
        subject=addr,
        conn=c,
    )
    return get_entry(chain, addr, c)


def remove(
    chain: Chain,
    address: str,
    *,
    reason: str,
    actor: str,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Take a wallet off the watchlist. The row stays; the status changes."""
    c = _conn(conn)
    addr = _norm(address, chain)
    cur = c.execute(
        "UPDATE tracker_watchlist SET status='removed', removed_ms=?, removed_reason=? "
        "WHERE chain=? AND address=? AND status='active'",
        (now_ms(), reason.strip()[:500], chain.value, addr),
    )
    if not cur.rowcount:
        return False
    _log(c, chain, addr, Action.REMOVED, actor, reason.strip())
    return True


def rescreen(
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    config: TrackerConfig = DEFAULT_CONFIG,
    actor: str = "rescreen",
    addresses: Sequence[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Re-run the admission screen on the active watchlist and evict what now fails.

    Admission is a judgement about a moving target. A wallet that was executing cleanly
    when it entered can turn into a bot, go quiet, or start settling someone else's flow,
    and a watchlist that only ever grows is the leaderboard problem with a slower clock.
    The evidence is refreshed, the new screen is written onto the row, and a wallet that
    no longer passes leaves with the failure recorded as its removal reason.

    Each wallet is judged under the policy it was admitted under (``meta_json``): a cohort
    wallet is not evicted for being ungraded, because it never entered on a grade, but a
    measured failure rate over the ceiling evicts it exactly as it would anyone else.

    Costs 10 Helius credits per wallet on Solana, so a pass is bounded: without an explicit
    ``addresses`` list it screens at most ``limit`` (default
    ``config.rescreen_max_wallets``) wallets, least recently checked first, and reports how
    many it left for the next pass. A 700-wallet cohort rescreened daily and unbounded would
    have cost 217,000 credits a month.
    """
    c = _conn(conn)
    skipped = 0
    if addresses is not None:
        targets = list(addresses)
    else:
        cap = int(limit) if limit is not None else int(config.rescreen_max_wallets)
        rows = fetch_all(
            c,
            "SELECT address FROM tracker_watchlist WHERE chain=? AND status='active' "
            "ORDER BY COALESCE(last_checked_ms, 0) ASC, address ASC",
            (chain.value,),
        )
        everyone = [str(r["address"]) for r in rows]
        targets = everyone[: max(0, cap)]
        skipped = len(everyone) - len(targets)
    kept: list[str] = []
    evicted: list[tuple[str, str]] = []
    retiered: list[str] = []
    for addr in targets:
        entry = get_entry(chain, addr, c)
        result = screen_wallet(chain, addr, c, config=config)
        if entry is not None and entry.policy == ADMISSION_POLICY_COHORT:
            decision = cohort_verdict(result, config=config)
        else:
            decision = standard_verdict(result, entry.tier if entry else Tier.OBSERVE)
        try:
            # A rescreened row goes to the back of the chain's queue: strictly later than
            # every stamp on the chain, not merely the clock. The rotation is then a
            # property of the pass and not of the clock's resolution — two passes inside
            # one millisecond still move on to the wallets they have not yet checked.
            c.execute(
                "UPDATE tracker_watchlist SET screen_json=?, "
                " last_checked_ms=MAX(?, COALESCE((SELECT MAX(last_checked_ms) FROM "
                "  tracker_watchlist WHERE chain=?), 0) + 1) "
                "WHERE chain=? AND address=?",
                (jdump(result.as_dict()), now_ms(), chain.value, chain.value, addr),
            )
        except sqlite3.Error as exc:
            log.warning("tracker: rescreen write failed for %s: %s", addr[:10], exc)
        if decision.admissible:
            kept.append(addr)
            _log(c, chain, addr, Action.RESCREENED, actor, "still passes",
                 {"screen": result.as_dict(), "policy": decision.policy,
                  "waived": list(decision.waived)})
            # The bounded daily rescreen is the only affordable place a cohort wallet's
            # failure rate gets measured, so a wallet that entered unmeasured and now
            # measures clean moves up here. Upward only, on evidence only: an unmeasurable
            # answer never demotes.
            if (
                entry is not None
                and entry.policy == ADMISSION_POLICY_COHORT
                and result.tx_failure_rate is not None
                and entry.tier is config.cohort_tier_unmeasured
                and decision.tier is not entry.tier
            ):
                set_tier(
                    chain, addr, decision.tier, actor=actor,
                    reason=f"failure rate now measured at {result.tx_failure_rate:.1%}", conn=c,
                )
                retiered.append(addr)
            continue
        why = "; ".join(decision.blockers)[:400]
        evicted.append((addr, why))
        remove(chain, addr, reason=f"failed rescreen: {why}", actor=actor, conn=c)
    return {
        "screened": len(targets),
        "kept": kept,
        "evicted": [{"address": a, "why": w} for a, w in evicted],
        "retiered": retiered,
        "skipped_for_budget": skipped,
        "credits": len(targets) * HELIUS_POLL_CREDITS if chain is Chain.SOL else 0,
    }


def set_tier(
    chain: Chain,
    address: str,
    tier: Tier,
    *,
    actor: str,
    reason: str,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Move between ``observe`` and ``candidate``. There is no third destination."""
    c = _conn(conn)
    addr = _norm(address, chain)
    cur = c.execute(
        "UPDATE tracker_watchlist SET tier=? WHERE chain=? AND address=? AND status='active'",
        (tier.value, chain.value, addr),
    )
    if not cur.rowcount:
        return False
    _log(c, chain, addr, Action.RETIERED, actor, f"{reason} -> {tier.value}")
    return True


def get_entry(
    chain: Chain, address: str, conn: sqlite3.Connection | None = None
) -> WatchlistEntry | None:
    row = fetch_one(
        _conn(conn),
        "SELECT * FROM tracker_watchlist WHERE chain=? AND address=?",
        (chain.value, _norm(address, chain)),
    )
    return _row_to_entry(row) if row else None


def watchlist(
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    status: str | None = "active",
    tier: Tier | None = None,
) -> list[WatchlistEntry]:
    sql = "SELECT * FROM tracker_watchlist WHERE chain=?"
    params: list[Any] = [chain.value]
    if status:
        sql += " AND status=?"
        params.append(status)
    if tier:
        sql += " AND tier=?"
        params.append(tier.value)
    sql += " ORDER BY added_ms DESC"
    return [_row_to_entry(r) for r in fetch_all(_conn(conn), sql, params)]


def watched_addresses(
    chain: Chain = Chain.SOL, conn: sqlite3.Connection | None = None
) -> set[str]:
    """Just the active addresses. One query, used on every detection hot path."""
    rows = fetch_all(
        _conn(conn),
        "SELECT address FROM tracker_watchlist WHERE chain=? AND status='active'",
        (chain.value,),
    )
    from kaiba.intelligence.watchlist_policy import filter_addresses

    return filter_addresses(_conn(conn), chain, {str(r["address"]) for r in rows})


def pollable_addresses(
    chain: Chain = Chain.SOL, conn: sqlite3.Connection | None = None
) -> set[str]:
    """Active addresses the paid per-wallet route should cover.

    Everything :func:`watched_addresses` returns minus the rows whose ``meta_json`` says
    ``helius_poll: false`` — the cohort wallets, whose trades the GMGN feed that nominated
    them already delivers. Rows written before the flag existed are polled, as they always
    were. The distinction matters for money: at 10 credits a poll every 300 s, a 700-wallet
    cohort left on this route would cost ~2,000,000 credits a day, twice the month's
    free allowance before lunch.
    """
    rows = fetch_all(
        _conn(conn),
        "SELECT address, meta_json FROM tracker_watchlist WHERE chain=? AND status='active'",
        (chain.value,),
    )
    out: set[str] = set()
    for row in rows:
        meta = jload(row.get("meta_json"), {}) or {}
        flag = meta.get("helius_poll") if isinstance(meta, dict) else None
        if flag is None or bool(flag):
            out.add(str(row["address"]))
    from kaiba.intelligence.watchlist_policy import filter_addresses

    return filter_addresses(_conn(conn), chain, out)


def audit_trail(
    chain: Chain, address: str, conn: sqlite3.Connection | None = None
) -> list[dict[str, Any]]:
    """Everything ever decided about this address, oldest first."""
    rows = fetch_all(
        _conn(conn),
        "SELECT ts_ms, action, actor, detail, payload_json FROM tracker_watchlist_log "
        "WHERE chain=? AND address=? ORDER BY ts_ms, id",
        (chain.value, _norm(address, chain)),
    )
    for row in rows:
        row["payload"] = jload(row.pop("payload_json"), {})
    return rows


def seed_from_grades(
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    config: TrackerConfig = DEFAULT_CONFIG,
    added_by: str = "seed_from_grades",
    limit: int = 50,
    check_failure_rate: bool = True,
) -> tuple[list[WatchlistEntry], list[Screen]]:
    """Offer every graded wallet at or above the floor to :func:`admit`.

    Returns ``(admitted, refused_screens)``. This is a *candidate source*, not a promotion
    path: a grade says what a wallet did, the screen says whether its shape is copyable at
    all, and neither says it will be right next time.
    """
    c = _conn(conn)
    order = [Grade.A, Grade.B, Grade.C, Grade.D, Grade.UNSCORED, Grade.QUARANTINED]
    allowed = [g.value for g in order[: order.index(config.min_grade) + 1]]
    placeholders = ",".join("?" for _ in allowed)
    rows = fetch_all(
        c,
        f"SELECT address, grade, score FROM wallet_scores WHERE chain=? AND grade IN ({placeholders}) "
        "ORDER BY score DESC LIMIT ?",
        (chain.value, *allowed, int(limit)),
    )
    admitted: list[WatchlistEntry] = []
    refused: list[Screen] = []
    for row in rows:
        addr = str(row["address"])
        result = screen_wallet(chain, addr, c, config=config, check_failure_rate=check_failure_rate)
        entry = admit(
            chain,
            addr,
            reason=(
                f"graded {row['grade']} at {float(row['score']):.1f} by {grade_mod.MODEL_ID}; "
                "observing forward behaviour only"
            ),
            source=f"grade:{grade_mod.MODEL_ID}",
            added_by=added_by,
            tier=Tier.OBSERVE,
            conn=c,
            config=config,
            screen=result,
        )
        (admitted.append(entry) if entry else refused.append(result))
    return admitted, refused


def seed_from_discovery(
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    run_id: str | None = None,
    config: TrackerConfig = DEFAULT_CONFIG,
    added_by: str = "seed_from_discovery",
    limit: int = 50,
) -> tuple[list[WatchlistEntry], list[Screen]]:
    """Offer a ``discovery_candidates`` cohort to :func:`admit`.

    ``kaiba/intelligence/discover.py`` is being written by another agent and does not
    exist yet, so this reads migration 021's tables directly and imports the module only
    if it is importable. That keeps the tracker working today and picks up discovery's own
    helpers the moment they land, without either module blocking the other.
    """
    c = _conn(conn)
    try:  # noqa: SIM105 - the module is expected to be absent for now
        from kaiba.intelligence import discover  # type: ignore # noqa: F401
    except ImportError:
        log.info("tracker: kaiba.intelligence.discover not present; reading migration 021 directly")

    try:
        if run_id is None:
            row = fetch_one(
                c,
                "SELECT run_id FROM discovery_runs WHERE chain=? ORDER BY run_ms DESC LIMIT 1",
                (chain.value,),
            )
            run_id = str(row["run_id"]) if row else None
        if run_id is None:
            return [], []
        rows = fetch_all(
            c,
            "SELECT address, status, closed_trades FROM discovery_candidates "
            "WHERE run_id=? AND chain=? ORDER BY closed_trades DESC LIMIT ?",
            (run_id, chain.value, int(limit)),
        )
    except sqlite3.Error as exc:
        log.warning("tracker: discovery read failed: %s", exc)
        return [], []

    admitted: list[WatchlistEntry] = []
    refused: list[Screen] = []
    for row in rows:
        addr = str(row["address"])
        result = screen_wallet(chain, addr, c, config=config)
        entry = admit(
            chain,
            addr,
            reason=(
                f"discovery run {run_id} status={row['status']} over {row['closed_trades']} closed "
                "trades; the run's own screen size is the deflation and it is recorded on the run"
            ),
            source=f"discovery:{run_id}",
            added_by=added_by,
            tier=Tier.OBSERVE,
            conn=c,
            config=config,
            screen=result,
        )
        (admitted.append(entry) if entry else refused.append(result))
    return admitted, refused


# --------------------------------------------------------------------------------------
# vendor cohorts: feeding sm-trenches the input it reads
# --------------------------------------------------------------------------------------
#
# The input chain of ``kaiba.execution.lanes.sm_trenches``, traced 2026-09-21 (read-only):
#
#   1. ``scanner.build_context`` -> ``load_recent_buys`` -> ``SELECT * FROM swaps`` for the
#      token inside the window. ``swaps`` has no tags column, so ``row["tags"]`` is absent.
#   2. ``lanes._tags`` therefore falls back to ``wallets.tags_json`` for each net buyer and
#      intersects it with ``SMART_TAGS`` = {smart_money, pump_smart, renowned, top_trader}.
#   3. Failing that, ``wallet_scores.archetype`` in {smart_money, top_trader}.
#   4. ``len(smart) >= min_smart_degen`` (3), then ``dossier.rug_ratio`` must be *known*
#      (unknown fails closed), then ``independent_entity_count`` over ``entity_members``
#      must be >= ``min_independent_entities`` (2).
#
# Nothing in that chain reads ``tracker_watchlist``. The watchlist is the auditable
# membership — who was let in, on what screen, by whom — and ``wallets.tags_json`` is the
# lane's actual input. So a cohort admission does both: it writes the watchlist row with
# the screen and policy on it, and only then writes ``smart_money`` onto the wallet's tag
# list. A refused wallet never gets the tag, and a wallet later evicted loses it. That is
# what makes the failure-rate gate mean something to the lane and not only to the ledger.
#
# On the live database before this existed: ``wallets`` 0 rows, ``tracker_watchlist`` 0
# rows, ``wallet_scores`` 0 rows on bsc — every step of the chain was empty on bsc, and the
# lane had never fired on any chain.

#: MEASURED per screen on Solana: one ``getTransactionsForAddress`` signatures page.
COHORT_SCREEN_CREDITS_SOL = HELIUS_POLL_CREDITS


class CohortSeedReport(BaseModel):
    """What one :func:`seed_from_cohorts` pass did, with the counts a reader will ask for."""

    chain: Chain
    sources: list[str]
    dry_run: bool
    considered: int = 0
    smart_cohort: int = 0
    kol_cohort: int = 0
    feed_only: int = 0
    tags_unavailable: int = 0
    admitted: list[str] = Field(default_factory=list)
    already_watched: list[str] = Field(default_factory=list)
    retiered: list[str] = Field(default_factory=list)
    refused: list[dict[str, Any]] = Field(default_factory=list)
    removed: list[dict[str, Any]] = Field(default_factory=list)
    refusal_counts: dict[str, int] = Field(default_factory=dict)
    waived_counts: dict[str, int] = Field(default_factory=dict)
    admitted_by_tier: dict[str, int] = Field(default_factory=dict)
    failure_rate_measured: int = 0
    failure_rate_unmeasurable: int = 0
    credits_spent: int = 0
    credits_budget: int | None = None
    wallet_rows_written: int = 0
    smart_tags_written: int = 0
    smart_tags_stripped: int = 0
    watchlist_before: dict[str, int] = Field(default_factory=dict)
    watchlist_after: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def _watchlist_counts(chain: Chain, conn: sqlite3.Connection) -> dict[str, int]:
    rows = fetch_all(
        conn,
        "SELECT status, tier, COUNT(*) AS n FROM tracker_watchlist WHERE chain=? "
        "GROUP BY status, tier",
        (chain.value,),
    )
    out = {"active": 0, "removed": 0, "observe": 0, "candidate": 0}
    for row in rows:
        n = int(row["n"])
        status, tier = str(row["status"]), str(row["tier"])
        out[status] = out.get(status, 0) + n
        if status == "active":
            out[tier] = out.get(tier, 0) + n
    return out


#: The only bare tags the cohort path may ever put on ``wallets.tags_json``, and only after
#: the wallet passed the screen (the call that wrote its ``tracker_watchlist`` row). Every
#: word the vendor said is written under naming-v2's ``gmgn:`` namespace instead, so a
#: label spelled like ``kol``, ``pump_smart`` or ``smart_money`` reaches neither
#: ``lanes.SMART_TAGS`` nor ``grade.POSITIVE_REPUTATION_TAGS`` until we screened the wallet.
#: :func:`_write_wallet_tags` refuses any other ``add``.
COHORT_DERIVED_TAGS: frozenset[WalletTag] = frozenset({WalletTag.SMART_MONEY})


def _vendor_tags(cohort: Any) -> list[str]:
    """The vendor's labels in the only spelling they get in ``wallets.tags_json``:
    ``gmgn:<label>`` (:func:`kaiba.intelligence.naming.vendor_tag`, the same namespace
    naming-v2 writes). ``grade._tags_from`` routes that spelling through
    ``admit_vendor_label`` — a quarantine or penalty label still reaches the screen, a
    positive one never becomes a tag — and ``lanes._tags`` matches none of it."""
    from kaiba.intelligence.naming import vendor_tag  # lazy: naming is a sibling, not a dependency

    out: list[str] = []
    for label in cohort.gmgn_tags:
        text = str(label).strip()
        if not text:
            continue
        spelled = vendor_tag(text)
        if spelled not in out:
            out.append(spelled)
    return out


def _write_wallet_tags(
    conn: sqlite3.Connection,
    chain: Chain,
    address: str,
    cohort: Any,
    *,
    add: Iterable[WalletTag] = (),
    strip: Iterable[WalletTag] = (),
    source: str,
) -> tuple[bool, bool]:
    """Record the vendor's labels on the ``wallets`` row, namespaced, and adjust our derived tags.

    Returns ``(derived_tag_added, derived_tag_stripped)``.

    * A vendor label is written only as ``gmgn:<label>`` (:func:`_vendor_tags`), never
      bare. The raw list stays a fact in ``meta.gmgn.tags``; the tag list is what the
      lane and the grader read, and a bare label there would be read as a screen result.
    * ``add`` may name only :data:`COHORT_DERIVED_TAGS`; anything else raises, so no later
      edit can put ``kol`` or ``top_trader`` on a wallet through this helper. The caller
      adds only after admission.
    * ``strip`` removes a derived tag only if *we* derived it earlier
      (``meta.kaiba_derived_tags``) — a ``smart_money`` that a wallet import wrote from
      another source is not ours to remove.
    * A row this path wrote before the namespace (``meta.gmgn`` present without
      ``tag_prefix``) still carries the vendor's labels bare; they are moved into the
      namespace once, and the marker keeps every later pass from touching a bare tag that
      is not ours. Fail-closed on the one case the record cannot tell apart: a bare tag an
      import wrote that coincides with a label the old pass also recorded is treated as
      the leak.
    * Existing ``name``, ``source``, ``cohort`` and funding fields are never touched;
      another job owns them. Idempotent: a second call with the same inputs changes no tag.
    """
    add = list(add)
    strip = list(strip)
    illegal = sorted({t.value for t in add} - {t.value for t in COHORT_DERIVED_TAGS})
    if illegal:
        raise ValueError(
            f"the cohort path may not write bare {illegal} onto wallets.tags_json; only "
            f"{sorted(t.value for t in COHORT_DERIVED_TAGS)}, and only after the screen"
        )
    from kaiba.intelligence.naming import GMGN_TAG_PREFIX  # lazy, see _vendor_tags

    row = fetch_one(
        conn,
        "SELECT tags_json, meta_json, last_seen_ms FROM wallets WHERE chain=? AND address=?",
        (chain.value, address),
    )
    tags: list[str] = [str(t) for t in (jload(row["tags_json"], []) if row else []) or []]
    meta: dict[str, Any] = (jload(row["meta_json"], {}) if row else {}) or {}
    if not isinstance(meta, dict):
        meta = {}
    derived_before = {str(t) for t in meta.get("kaiba_derived_tags", []) or []}
    prior = meta.get("gmgn")
    prior = prior if isinstance(prior, dict) else None

    added = False
    stripped = False
    for tag in strip:
        if tag.value in derived_before and tag.value in tags:
            tags = [t for t in tags if t != tag.value]
            stripped = True
    derived_after = derived_before - {t.value for t in strip}

    # Migration, once: a row the pre-namespace code wrote carries its recorded labels bare.
    if prior is not None and prior.get("tag_prefix") != GMGN_TAG_PREFIX:
        leaked = {str(t) for t in (prior.get("tags") or [])} - derived_after
        if leaked:
            tags = [t for t in tags if t not in leaked]

    for spelled in _vendor_tags(cohort):
        if spelled not in tags:
            tags.append(spelled)
    for tag in add:
        if tag.value not in tags:
            tags.append(tag.value)
            added = True
        derived_after.add(tag.value)

    meta["kaiba_derived_tags"] = sorted(derived_after)
    meta["gmgn"] = {
        "feeds": list(cohort.sources),
        "tags": list(cohort.gmgn_tags),
        "tag_prefix": GMGN_TAG_PREFIX,
        "tags_basis": cohort.tags_basis.value,
        "name": cohort.wallet_name,
        "feed_rows": cohort.feed_rows,
        "gathered_ms": now_ms(),
    }
    if row:
        conn.execute(
            "UPDATE wallets SET tags_json=?, meta_json=?, last_seen_ms=? WHERE chain=? AND address=?",
            (
                jdump(tags),
                jdump(meta),
                max(int(row["last_seen_ms"] or 0), int(cohort.last_seen_ms)),
                chain.value,
                address,
            ),
        )
    else:
        conn.execute(
            "INSERT INTO wallets (chain, address, name, source, tags_json, first_seen_ms, "
            " last_seen_ms, cohort, meta_json) VALUES (?,?,NULL,?,?,?,?,NULL,?)",
            (
                chain.value,
                address,
                source,
                jdump(tags),
                int(cohort.first_seen_ms),
                int(cohort.last_seen_ms),
                jdump(meta),
            ),
        )
    return added, stripped


def _cohort_reason(cohort: Any, screen: Screen, decision: Verdict, chain: Chain) -> str:
    feeds = ", ".join(cohort.sources)
    tags = ", ".join(cohort.gmgn_tags[:6]) or "no labels in any feed event"
    if screen.tx_failure_rate is not None:
        rate = f"failure rate {screen.tx_failure_rate:.1%} over {screen.signatures_seen} signatures"
    elif any(w.startswith("failure_rate_disabled") for w in decision.waived):
        rate = "failure rate not measured this run (credit budget); admitted at the lower tier"
    else:
        rate = (
            f"failure rate unmeasurable on {chain.value} (no signature source here); "
            "admitted at the lower tier"
        )
    return (
        f"listed on GMGN {feeds} with labels [{tags}]: a vendor cohort label "
        f"('wallet who often earns money', no published method), not a grade; {rate}; "
        "observing only"
    )


def seed_from_cohorts(
    chain: Chain,
    conn: sqlite3.Connection | None = None,
    *,
    sources: Iterable[str] | None = None,
    config: TrackerConfig = DEFAULT_CONFIG,
    added_by: str = "seed_from_cohorts",
    limit: int | None = None,
    since_ms: int | None = None,
    check_failure_rate: bool = True,
    max_credits: int | None = None,
    dry_run: bool = False,
    write_wallet_tags: bool = True,
) -> CohortSeedReport:
    """Offer every wallet the GMGN feeds listed on ``chain`` to :func:`admit` under the
    cohort policy, and write the admitted smart-money wallets where ``sm-trenches`` reads.

    * **Source is the label, not a score.** ``tracker_watchlist.source`` is
      ``gmgn:cohort:<smart_degen|kol|feed_only>``; ``grade_at_add`` is whatever we had,
      which on bsc is nothing.
    * **The failure-rate gate is kept.** A measured rate over the ceiling refuses. Where
      the tape cannot compute one (every chain but Solana; Solana when ``max_credits`` runs
      out or ``check_failure_rate`` is off) the wallet enters at
      ``config.cohort_tier_unmeasured`` with the unknown on its row and in its reason.
    * **Vendor labels are boxed.** Every GMGN label lands on ``wallets.tags_json`` as
      ``gmgn:<label>`` (naming-v2's namespace) *before* the screen, so the screen can see
      a quarantine label and nothing can read a positive one as a result. The only bare
      tag this pass writes is ``smart_money`` (:data:`COHORT_DERIVED_TAGS`), on a
      smart-cohort wallet, after it passed; a refused or evicted wallet loses it again.
    * **Idempotent.** A wallet already watched keeps its original admission; a re-run
      corroborates it in the log, refreshes the screen, and re-tiers upward only when a
      failure rate is newly measured clean. A currently watched cohort wallet that now
      fails an *enforced* rule is removed with the reason.
    * **Per chain.** Only ``chain``'s feed rows are read and only ``chain``'s rows written.
    * ``dry_run`` screens and decides but writes nothing, so the pass can be measured on a
      live database without changing it.

    Solana screens cost :data:`COHORT_SCREEN_CREDITS_SOL` each when the failure rate is
    measured; ``max_credits`` bounds a run, and wallets past the budget are screened
    without the paid check and say so.
    """
    from kaiba.intelligence import discover  # sibling; imported here to keep the graph light

    c = _conn(conn)
    wanted = sorted({str(s) for s in (sources or discover.COHORT_FEED_SOURCES) if s})
    report = CohortSeedReport(chain=chain, sources=wanted, dry_run=dry_run, credits_budget=max_credits)
    report.watchlist_before = _watchlist_counts(chain, c)

    cohorts = discover.cohort_wallets(chain, c, sources=wanted, since_ms=since_ms, limit=limit)
    summary = discover.cohort_summary(cohorts)
    report.considered = summary["wallets"]
    report.smart_cohort = summary["smart_cohort"]
    report.kol_cohort = summary["kol_cohort"]
    report.feed_only = summary["feed_only"]
    report.tags_unavailable = summary["tags_unavailable"]
    if not cohorts:
        report.notes.append(f"no swaps rows on {chain.value} from {wanted}; nothing to offer")
        report.watchlist_after = report.watchlist_before
        return report
    if chain is not Chain.SOL:
        report.notes.append(
            f"failure rate has no signature source on {chain.value}; every admission here is "
            f"at the lower tier ({config.cohort_tier_unmeasured.value}) with the gate recorded "
            "as unmeasurable, not skipped"
        )
    elif not check_failure_rate:
        report.notes.append(
            "failure-rate check disabled for this run: no Helius spend, every admission at the "
            f"lower tier ({config.cohort_tier_unmeasured.value})"
        )

    def _bump(counter: dict[str, int], key: str) -> None:
        counter[key] = counter.get(key, 0) + 1

    spent = 0
    for cohort in cohorts:
        addr = cohort.address
        source = f"{COHORT_SOURCE_PREFIX}{cohort.cohort_label}"

        # 1. record what the vendor said, namespaced, before the screen reads the wallet's
        #    tags. Nothing bare is written here: the wallet has not been screened yet.
        if not dry_run and write_wallet_tags:
            _write_wallet_tags(c, chain, addr, cohort, source=source)
            report.wallet_rows_written += 1

        # 2. the screen, with the paid check only while the budget lasts. Off Solana the
        #    measurement answers "unmeasurable" by itself, at no cost; the budget only
        #    ever turns the check off where it would have been paid for.
        measure = check_failure_rate
        paid = chain is Chain.SOL
        if measure and paid and max_credits is not None and spent + COHORT_SCREEN_CREDITS_SOL > max_credits:
            measure = False
        screen = screen_wallet(chain, addr, c, config=config, check_failure_rate=measure)
        if measure and paid and screen.tx_failure_basis is not EvidenceBasis.UNAVAILABLE:
            spent += COHORT_SCREEN_CREDITS_SOL
        elif measure and paid and screen.signatures_seen is not None:
            spent += COHORT_SCREEN_CREDITS_SOL  # a thin page is still a paid page
        if dry_run or not write_wallet_tags:
            # The wallets row was not written, so fold the vendor's labels into the screen
            # by hand, in the same namespaced spelling the live row would carry: a
            # quarantine label must refuse here exactly as it would live, and a positive
            # one must be as invisible here as it is there.
            extra = grade_mod._tags_from(_vendor_tags(cohort))
            for tag in extra:
                if tag not in screen.tags:
                    screen.tags.append(tag)
            hard = set(screen.tags) & HARD_QUARANTINE_TAGS
            if hard and "quarantine_tags" not in screen.blocker_codes:
                screen.block(
                    "quarantine_tags",
                    "hard quarantine tags: " + ", ".join(sorted(t.value for t in hard)),
                )
        if screen.tx_failure_rate is None:
            report.failure_rate_unmeasurable += 1
        else:
            report.failure_rate_measured += 1

        # 3. the policy.
        decision = cohort_verdict(screen, config=config)
        for waived in decision.waived:
            _bump(report.waived_counts, waived.split(":", 1)[0])
        reason = _cohort_reason(cohort, screen, decision, chain)

        if dry_run:
            if decision.admissible:
                report.admitted.append(addr)
                _bump(report.admitted_by_tier, decision.tier.value)
            else:
                report.refused.append({"address": addr, "codes": decision.blocker_codes, "why": decision.blockers})
                for code in decision.blocker_codes:
                    _bump(report.refusal_counts, code)
            continue

        # 4. admit / corroborate / remove, and keep the lane's input in step with it.
        existing = get_entry(chain, addr, c)
        entry = admit(
            chain, addr, reason=reason, source=source, added_by=added_by,
            conn=c, config=config, screen=screen, verdict=decision,
        )
        derived = [WalletTag.SMART_MONEY] if cohort.smart_cohort else []
        if entry is None:
            report.refused.append({"address": addr, "codes": decision.blocker_codes, "why": decision.blockers})
            for code in decision.blocker_codes:
                _bump(report.refusal_counts, code)
            if existing is not None and existing.status == "active":
                why = "; ".join(decision.blockers)[:400]
                remove(chain, addr, reason=f"failed cohort re-screen: {why}", actor=added_by, conn=c)
                report.removed.append({"address": addr, "why": why})
            if write_wallet_tags:
                # Whatever this path derived earlier goes, whether or not the vendor still
                # calls the wallet smart: a refused or evicted wallet carries no bare tag.
                _added, stripped = _write_wallet_tags(
                    c, chain, addr, cohort, strip=COHORT_DERIVED_TAGS, source=source
                )
                report.smart_tags_stripped += 1 if stripped else 0
            continue

        if existing is not None and existing.status == "active":
            report.already_watched.append(addr)
            # Upward only, and only on new evidence: a measured-clean rate on a wallet that
            # entered unmeasured. Absence of a measurement never demotes.
            if (
                existing.policy == ADMISSION_POLICY_COHORT
                and screen.tx_failure_rate is not None
                and existing.tier is config.cohort_tier_unmeasured
                and decision.tier is not existing.tier
            ):
                set_tier(
                    chain, addr, decision.tier, actor=added_by,
                    reason=f"failure rate now measured at {screen.tx_failure_rate:.1%}", conn=c,
                )
                report.retiered.append(addr)
        else:
            report.admitted.append(addr)
            _bump(report.admitted_by_tier, entry.tier.value)
        if write_wallet_tags:
            # Admitted: the derived tag for its cohort goes on, and any derived tag its
            # cohort no longer earns comes off (a wallet the feeds stopped calling smart).
            added, stripped = _write_wallet_tags(
                c, chain, addr, cohort, add=derived,
                strip=[t for t in COHORT_DERIVED_TAGS if t not in derived], source=source,
            )
            report.smart_tags_written += 1 if added else 0
            report.smart_tags_stripped += 1 if stripped else 0

    report.credits_spent = spent
    report.watchlist_after = _watchlist_counts(chain, c) if not dry_run else report.watchlist_before
    if dry_run:
        report.notes.append("dry run: nothing written; counts are what a live pass would do")
    return report


def lane_smart_wallets(chain: Chain, conn: sqlite3.Connection | None = None) -> set[str]:
    """The addresses ``sm-trenches`` would count as smart on ``chain``, by its own two routes:
    a ``wallets.tags_json`` entry in :data:`LANE_SMART_TAGS`, or a ``wallet_scores``
    archetype in :data:`LANE_SMART_ARCHETYPES`. Read the same way the lane reads them."""
    c = _conn(conn)
    out: set[str] = set()
    for row in fetch_all(c, "SELECT address, tags_json FROM wallets WHERE chain=?", (chain.value,)):
        tags = {str(t) for t in (jload(row["tags_json"], []) or [])}
        if tags & LANE_SMART_TAGS:
            out.add(str(row["address"]))
    placeholders = ",".join("?" for _ in LANE_SMART_ARCHETYPES)
    for row in fetch_all(
        c,
        f"SELECT address FROM wallet_scores WHERE chain=? AND archetype IN ({placeholders})",
        (chain.value, *sorted(LANE_SMART_ARCHETYPES)),
    ):
        out.add(str(row["address"]))
    return out


def _stored_rug_measure(conn: sqlite3.Connection, chain: Chain, token: str) -> Measure | None:
    """The stored dossier's ``rug_ratio`` as the :class:`Measure` it was written as, or
    ``None`` when no dossier is stored at all. A dossier whose ``rug_ratio`` is missing or
    malformed reads as ``Measure.unknown()``: unknown, never a number."""
    row = fetch_one(
        conn,
        "SELECT dossier_json FROM token_dossiers WHERE chain=? AND address=?",
        (chain.value, token),
    )
    if not row:
        return None
    body = jload(row["dossier_json"], {}) or {}
    raw = body.get("rug_ratio") if isinstance(body, dict) else None
    if not isinstance(raw, dict):
        return Measure.unknown()
    try:
        return Measure.model_validate(raw)
    except (TypeError, ValueError):  # pydantic's ValidationError is a ValueError
        return Measure.unknown()


def _rug_ratio_is_evm_zero(chain: Chain, measure: Measure) -> bool:
    """GMGN's EVM placeholder: a ``rug_ratio`` of exactly 0 on an EVM chain.

    MEASURED 2026-09-21 on the live box: every ``rug_ratio`` GMGN has filled on a bsc,
    robinhood or base trenches row was exactly 0 (8/180, 8/180, 54/180; the rest null),
    while sol carries a real spread (180/180 filled, median 0.008, 23/180 at or above
    0.3) and one token moved 0 -> 0.682 within minutes. An EVM 0 is therefore "not scored
    yet", not "clean". The feed reader returns UNAVAILABLE for it; this report agrees, so
    a stored dossier that predates that rule cannot count a placeholder as coverage. A sol
    0 stays a score.
    """
    return chain in EVM_CHAINS and measure.value is not None and measure.value == 0


def _rug_ratio_stale(measure: Measure) -> bool:
    """The Measure's own verdict, as ``lanes.sm_trenches`` reads it: past its freshness
    budget, or served with basis STALE out of an expired provider cache."""
    return bool(measure.known) and (bool(measure.stale) or measure.basis is EvidenceBasis.STALE)


def _rug_ratio_known(conn: sqlite3.Connection, chain: Chain, token: str) -> bool | None:
    """``True`` known, ``False`` unknown, ``None`` no dossier at all. Never a number.

    Unknown covers an UNAVAILABLE basis, a null value, a malformed measure and -- since
    round 2 of make-bsc-fire -- an EVM 0 (:func:`_rug_ratio_is_evm_zero`). A known value
    may still be stale; :func:`_rug_ratio_stale` says so, and the lane reads a stale value
    under its ceiling as unavailable and one at or over it as a refusal.
    """
    measure = _stored_rug_measure(conn, chain, token)
    if measure is None:
        return None
    return bool(measure.known) and not _rug_ratio_is_evm_zero(chain, measure)


def trenches_input_report(
    chain: Chain,
    conn: sqlite3.Connection | None = None,
    *,
    config: TrackerConfig = DEFAULT_CONFIG,
    since_ms: int | None = None,
    limit: int = 500,
    smart_wallets: set[str] | None = None,
) -> dict[str, Any]:
    """Does ``sm-trenches`` have enough input on ``chain`` to fire on any token? Read-only.

    Walks the lane's own chain: which wallets it would count as smart (or ``smart_wallets``
    when a dry run wants to measure a set it has not written), whether three of them net
    buy one token inside ``window_s``, how many *entities* those three are, and whether the
    token's stored dossier carries a MEASURED rug ratio at or over the lane's ceiling. Each
    gate is reported on its own, because a lane that cannot fire has exactly one first
    blocker and the operator needs to know which.

    Two things are counted but do not block, and ``notes`` says so:

    * An unavailable rug ratio. Since 2026-09-21 the lane treats an UNAVAILABLE rug ratio
      as no evidence either way -- it neither refuses nor earns strength -- because GMGN
      does not fill the number on bsc, the chain the lane exists for (MEASURED: 8/180 feed
      rows, 0/4,532 dossiers). The rug defence is the dossier blockers ``engine.decide``
      refuses before sizing (honeypot, mint/freeze authority, dev_concentration,
      cluster_concentration, already_rugged -> QUARANTINED). The ``rug_ratio_known`` /
      ``_unknown`` / ``no_dossier`` counters stay so the operator can see coverage; only
      ``rug_ratio_over_max_on_qualifying`` refuses. Two readings are unknown in disguise
      and are counted on their own: an EVM 0 (``rug_ratio_evm_zero_on_qualifying``, the
      GMGN "not scored yet" placeholder the feed reader returns UNAVAILABLE for; see
      :func:`_rug_ratio_is_evm_zero`) and a value past its freshness budget
      (``rug_ratio_stale_on_qualifying``): the lane reads a stale value under its ceiling
      as unavailable and refuses one at or over it, and so does this report. Every
      qualifying row carries ``rug_ratio_basis`` in the lane's own words (``measured`` /
      ``stale`` / ``unavailable``, or ``refused``) next to ``rug_ratio_stored``, the
      number literally on the dossier row.
    * ``amount_native`` that is NULL (UNAVAILABLE: the feed could not prove the quote
      asset, and the backfill nulls the old UI-unit strings) or still a dotted UI-unit
      string. ``lanes._net_buyers`` reads both as UNAVAILABLE and runs the net-buyer test
      on ``usd_value``, so a lane pass over such a tape no longer fails.

    Honesty notes that travel with the numbers: where ``entity_members`` has no rows for
    the chain the entity count is the address count (the permissive direction, as in
    ``independent_entity_count``), and ``entity_collapse_available`` says so. The net
    buyer test here is the lane's (``buy_usd - sell_usd > 0`` inside the window), but the
    dossier the lane sees is built at scan time, so the rug counters describe what the
    stored dossier knows now, not what a future scan might fetch.
    """
    c = _conn(conn)
    smart = set(smart_wallets) if smart_wallets is not None else lane_smart_wallets(chain, c)
    window_ms = int(config.trenches_window_s) * 1000
    min_smart = int(config.trenches_min_wallets)
    min_entities = int(config.trenches_min_entities)
    entity_rows = entity_mod.entity_count(chain, c)
    # The ceiling the lane itself reads: defaults, then config/risk.yaml. Imported here
    # and not at module level so the intelligence package does not depend on execution.
    try:
        from kaiba.execution.lanes import LaneContext as _LaneContext

        max_rug = _dec(
            _LaneContext(chain=chain, token="").lane_params(Lane.SM_TRENCHES).get("max_rug_ratio")
        )
    except Exception as exc:  # noqa: BLE001 - reported as a blocker below, never a crash
        log.warning("sm-trenches lane params unreadable: %s", exc)
        max_rug = None

    out: dict[str, Any] = {
        "chain": chain.value,
        "smart_wallets_available": len(smart),
        "min_smart_degen": min_smart,
        "min_independent_entities": min_entities,
        "window_s": int(config.trenches_window_s),
        "max_rug_ratio": None if max_rug is None else str(max_rug),
        "entity_collapse_available": entity_rows > 0,
        "entities_on_chain": entity_rows,
        "tokens_scanned": 0,
        "tokens_with_enough_smart_buyers": 0,
        "tokens_qualifying_on_entities": 0,
        "tokens_lane_eligible": 0,
        "qualifying_tokens": [],
        "rug_ratio_known_on_qualifying": 0,
        "rug_ratio_unknown_on_qualifying": 0,
        "rug_ratio_evm_zero_on_qualifying": 0,
        "rug_ratio_stale_on_qualifying": 0,
        "no_dossier_on_qualifying": 0,
        "rug_ratio_over_max_on_qualifying": 0,
        "amount_native_not_base_units": 0,
        "amount_native_unavailable": 0,
        "lane_can_fire_now": False,
        "blockers": [],
        "notes": [],
    }
    if not smart:
        out["blockers"].append(
            "no wallet on this chain carries a lane smart tag or archetype; the lane's first "
            "gate (min_smart_degen) cannot be met by any tape"
        )
        return out

    floor = since_ms if since_ms is not None else now_ms() - config.scan_lookback_s * 1000
    rows = fetch_all(
        c,
        "SELECT token, wallet, ts_ms, side, usd_value, amount_native FROM swaps "
        "WHERE chain=? AND ts_ms >= ? ORDER BY token, ts_ms, id",
        (chain.value, int(floor)),
    )
    by_token: dict[str, list[dict[str, Any]]] = {}
    ui_units = 0
    unavailable = 0
    for row in rows:
        if str(row["wallet"]) in smart:
            by_token.setdefault(str(row["token"]), []).append(row)
            native = row.get("amount_native")
            if native is None or str(native).strip() == "":
                unavailable += 1  # UNAVAILABLE, never 0: the feed could not prove the quote asset
            elif not str(native).strip().lstrip("-").isdigit():
                ui_units += 1  # a UI-unit decimal in a base-units column; the backfill nulls it
    ranked = sorted(by_token.items(), key=lambda kv: (-len({r["wallet"] for r in kv[1]}), kv[0]))[: int(limit)]
    out["tokens_scanned"] = len(ranked)
    out["amount_native_not_base_units"] = ui_units
    out["amount_native_unavailable"] = unavailable

    qualifying: list[dict[str, Any]] = []
    for token, trades in ranked:
        best_smart: list[str] = []
        best_entities = 0
        for anchor in trades:
            end = int(anchor["ts_ms"])
            start = end - window_ms
            agg: dict[str, Decimal] = {}
            bought: set[str] = set()
            for row in trades:
                ts = int(row["ts_ms"])
                if ts < start or ts > end:
                    continue
                usd = _dec(row["usd_value"]) or Decimal(0)
                w = str(row["wallet"])
                if str(row["side"]).lower() == "buy":
                    agg[w] = agg.get(w, Decimal(0)) + usd
                    bought.add(w)
                else:
                    agg[w] = agg.get(w, Decimal(0)) - usd
            net = sorted(w for w in bought if agg.get(w, Decimal(0)) > 0)
            if len(net) > len(best_smart):
                best_smart = net
                best_entities = entity_mod.independent_entity_count(chain, net, c)
        if len(best_smart) < min_smart:
            continue
        out["tokens_with_enough_smart_buyers"] += 1
        if best_entities < min_entities:
            continue
        out["tokens_qualifying_on_entities"] += 1
        # The stored dossier's rug Measure, read the way ``lanes.sm_trenches`` reads the
        # live one: value, basis and age together. ``_rug_ratio_known`` is the same read
        # with the EVM-0 rule applied; it is called once more here so the two cannot drift.
        measure = _stored_rug_measure(c, chain, token)
        known = _rug_ratio_known(c, chain, token)
        stored: Decimal | None = measure.value if measure is not None else None
        evm_zero = measure is not None and _rug_ratio_is_evm_zero(chain, measure)
        stale = bool(known) and measure is not None and _rug_ratio_stale(measure)
        if known is None:
            out["no_dossier_on_qualifying"] += 1
        elif known:
            out["rug_ratio_known_on_qualifying"] += 1
            if stale:
                out["rug_ratio_stale_on_qualifying"] += 1
        else:
            out["rug_ratio_unknown_on_qualifying"] += 1
            if evm_zero:
                out["rug_ratio_evm_zero_on_qualifying"] += 1
        # The lane's rule, exactly: a value that EXISTS at or over the ceiling refuses,
        # fresh or stale (a stale bad number is still a bad number). A stale value under
        # it is read as unavailable; only a fresh known one is "measured".
        refused = stored is not None and max_rug is not None and stored >= max_rug
        if refused:
            out["rug_ratio_over_max_on_qualifying"] += 1
            basis = "refused"
        else:
            out["tokens_lane_eligible"] += 1
            basis = "measured" if known and not stale else ("stale" if stale else "unavailable")
        rug_value: Decimal | None = stored if (refused or basis == "measured") else None
        qualifying.append(
            {
                "token": token,
                "smart_net_buyers": len(best_smart),
                "entities": best_entities,
                "entities_are_addresses": entity_rows == 0 or best_entities == len(best_smart),
                "rug_ratio_known": known,
                # What the lane would read (None for unavailable, stale and the EVM 0) ...
                "rug_ratio": None if rug_value is None else str(rug_value),
                "rug_ratio_basis": basis,
                # ... and what the dossier row literally holds, so the operator sees both.
                "rug_ratio_stored": None if stored is None else str(stored),
                "rug_refused": refused,
            }
        )
    out["qualifying_tokens"] = qualifying[:40]

    if out["tokens_with_enough_smart_buyers"] == 0:
        out["blockers"].append(
            f"no token in the lookback had {min_smart} smart net buyers inside {config.trenches_window_s} s"
        )
    elif out["tokens_qualifying_on_entities"] == 0:
        out["blockers"].append(
            f"smart buyers present but never across {min_entities} independent entities"
        )
    elif out["tokens_lane_eligible"] == 0:
        out["blockers"].append(
            f"every qualifying token carries a MEASURED rug_ratio >= {max_rug}; the lane "
            "refuses a measured value at or over its ceiling, fresh or stale (an unavailable "
            "one it does not)"
        )
    if not out["entity_collapse_available"]:
        out["blockers"].append(
            "entity_members has no rows for this chain: the >=2-entities gate is counting "
            "addresses, so it cannot refuse a sybil ring here"
        )
    if max_rug is None:
        out["blockers"].append(
            "max_rug_ratio is unreadable from the lane params; the lane fails closed on a "
            "missing ceiling"
        )
    unknown_total = out["rug_ratio_unknown_on_qualifying"] + out["no_dossier_on_qualifying"]
    if unknown_total:
        out["notes"].append(
            f"rug_ratio unavailable on {unknown_total} of {out['tokens_qualifying_on_entities']} "
            f"qualifying tokens (no dossier {out['no_dossier_on_qualifying']}, dossier unknown "
            f"{out['rug_ratio_unknown_on_qualifying']}): not a lane blocker since 2026-09-21; "
            "an UNAVAILABLE rug ratio neither refuses nor earns strength. The rug defence is "
            "the dossier blockers (honeypot, mint/freeze authority, dev_concentration, "
            "cluster_concentration, already_rugged -> QUARANTINED), refused by engine.decide "
            "before any size"
        )
    if out["rug_ratio_evm_zero_on_qualifying"]:
        out["notes"].append(
            f"{out['rug_ratio_evm_zero_on_qualifying']} qualifying tokens hold a stored "
            "rug_ratio of exactly 0 on an EVM chain: GMGN's placeholder for 'not scored yet' "
            "(MEASURED 2026-09-21: every EVM value seen was 0; one token moved 0 -> 0.682 "
            "within minutes). Read as UNAVAILABLE, as the feed reader reads it: not coverage, "
            "not a blocker, not a clean score"
        )
    if out["rug_ratio_stale_on_qualifying"]:
        out["notes"].append(
            f"{out['rug_ratio_stale_on_qualifying']} qualifying tokens hold a stored rug_ratio "
            "past its freshness budget: the lane reads a stale value under the ceiling as "
            "unavailable (basis 'stale', no strength) and refuses one at or over it; the "
            "dossier is rebuilt at scan time, so a fresh read may replace the stored number"
        )
    if ui_units:
        out["notes"].append(
            f"{ui_units} smart-wallet swap rows still carry amount_native as a UI-unit decimal "
            "string in a base-units column; lanes._net_buyers reads them as UNAVAILABLE and "
            "the net-buyer test runs on usd_value, so the lane pass does not fail on them. "
            "Run kaiba.ingest.gmgn_feeds backfill-amount-native to null them"
        )
    if unavailable:
        out["notes"].append(
            f"{unavailable} smart-wallet swap rows carry amount_native NULL (UNAVAILABLE: the "
            "quote asset was not proven); not a lane blocker, the net-buyer test runs on usd_value"
        )
    # The entity gate plus a readable ceiling. Deliberately NOT "and a rug ratio is known":
    # the lane does not require one (unavailable neither refuses nor earns strength), so
    # the report must not either, or it would call a lane that can fire blocked.
    out["lane_can_fire_now"] = out["tokens_lane_eligible"] > 0 and max_rug is not None
    return out

# --------------------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Detection:
    """One observed trade by a watched address. Deliberately carries no score."""

    chain: Chain
    wallet: str
    token: str
    side: str
    tx: str
    slot: int | None
    block_ms: int | None
    detected_ms: int
    usd_value: Decimal | None
    amount_native: int | None
    route: str
    entity_id: str | None = None

    @property
    def lag_ms(self) -> int | None:
        """``None``, never 0, when the route did not supply a block time."""
        if self.block_ms is None:
            return None
        return max(0, self.detected_ms - self.block_ms)


@dataclass(frozen=True, slots=True)
class PollResult:
    route: str
    subject: str
    ok: bool
    rtt_ms: int
    credits: int
    rows_seen: int
    detections: tuple[Detection, ...] = field(default_factory=tuple)
    note: str = ""


def _record_poll(conn: sqlite3.Connection, chain: Chain, result: PollResult, started_ms: int) -> None:
    try:
        conn.execute(
            "INSERT INTO tracker_polls (chain, route, subject, started_ms, finished_ms, rtt_ms, "
            " credits, ok, rows_seen, detections, note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                chain.value,
                result.route,
                result.subject,
                started_ms,
                started_ms + result.rtt_ms,
                result.rtt_ms,
                result.credits,
                1 if result.ok else 0,
                result.rows_seen,
                len(result.detections),
                result.note[:300],
            ),
        )
    except sqlite3.Error as exc:
        log.warning("tracker: poll ledger write failed: %s", exc)


def record_detections(
    conn: sqlite3.Connection, detections: Iterable[Detection]
) -> list[Detection]:
    """Persist and announce. Returns only the ones that were new.

    The event kind is ``wallet.trade`` and not ``alpha.signal`` on purpose. A tracker
    detection is an observation; routing it onto a signal kind would put it in front of
    the lane machinery as though it were evidence that the wallet is right, which the
    research says it is not.
    """
    fresh: list[Detection] = []
    for det in detections:
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO tracker_detections (chain, wallet, token, side, tx, slot, "
                " block_ms, detected_ms, lag_ms, usd_value, amount_native, route, entity_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    det.chain.value,
                    det.wallet,
                    det.token,
                    det.side,
                    det.tx,
                    det.slot,
                    det.block_ms,
                    det.detected_ms,
                    det.lag_ms,
                    None if det.usd_value is None else str(det.usd_value),
                    None if det.amount_native is None else str(det.amount_native),
                    det.route,
                    det.entity_id,
                ),
            )
        except sqlite3.Error as exc:
            log.warning("tracker: detection insert failed for %s: %s", det.tx[:12], exc)
            continue
        if not cur.rowcount:
            continue
        fresh.append(det)
        emit(
            EventKind.WALLET_TRADE,
            {
                "tracker": MODEL_ID,
                "observation": True,
                "wallet": det.wallet,
                "token": det.token,
                "side": det.side,
                "tx": det.tx,
                "route": det.route,
                "block_ms": det.block_ms,
                "detected_ms": det.detected_ms,
                "lag_ms": det.lag_ms,
                "usd_value": None if det.usd_value is None else str(det.usd_value),
                "entity_id": det.entity_id,
                "note": (
                    "a watched wallet acted; this is not evidence the wallet is right and it is "
                    "not a buy signal"
                ),
            },
            chain=det.chain,
            subject=det.wallet,
            conn=conn,
        )
    return fresh


# -- Route T: per token, free ------------------------------------------------------------


def hot_mints(
    limit: int = 30,
    conn: sqlite3.Connection | None = None,
    *,
    priority: Priority = Priority.DISCOVERY,
) -> tuple[list[str], Receipt]:
    """Mints that traded most recently, from pump.fun's own listing.

    MEASURED 2026-09-20: our ``tokens`` table is ordered by *creation*, and of its eight
    newest rows six had no trade route at all (503) and two had last traded ~326 s ago.
    Sweeping recently-created mints therefore spends the entire free budget on dead tape.
    Ordering by ``last_trade_timestamp`` returned mints whose newest trade was 1–2 s old.
    """
    from kaiba.providers._http import get_json

    got = get_json(
        token_flow.PROVIDER,
        COINS_ENDPOINT,
        PUMPFUN_COINS_URL,
        params={
            "offset": 0,
            "limit": max(1, min(int(limit), 70)),
            "sort": "last_trade_timestamp",
            "order": "DESC",
            "includeNsfw": "true",
        },
        headers={"user-agent": token_flow.USER_AGENT, "accept": "*/*"},
        ttl_s=5.0,
        priority=priority,
        wait_for_slot_s=10.0,
        conn=conn,
    )
    if not got.ok or not isinstance(got.data, list):
        return [], got.receipt
    mints = [str(r["mint"]) for r in got.data if isinstance(r, dict) and r.get("mint")]
    return mints, got.receipt


def poll_token(
    chain: Chain,
    mint: str,
    conn: sqlite3.Connection | None = None,
    *,
    watched: set[str] | None = None,
    config: TrackerConfig = DEFAULT_CONFIG,
) -> PollResult:
    """One page of a mint's tape. Free. Writes every trade to ``swaps``; flags watched ones.

    Writing *all* of it rather than only the watched rows is deliberate: ``confluence-5``
    counts entities among *all* buyers of the mint, so filtering to the watchlist at
    ingest would make the confluence question unanswerable from our own tape.
    """
    c = _conn(conn)
    seen = watched if watched is not None else watched_addresses(chain, c)
    started = now_ms()
    t0 = time.monotonic()
    page, receipt = token_flow.fetch_trades_page(
        mint,
        config=token_flow.FlowConfig(page_limit=config.token_page_limit, ttl_s=0.0),
        priority=Priority.DISCOVERY,
        conn=c,
    )
    rtt = int((time.monotonic() - t0) * 1000)
    if page is None:
        result = PollResult(ROUTE_PUMPFUN, mint, False, rtt, PUMPFUN_POLL_CREDITS, 0,
                            note=(receipt.note or "unavailable")[:300])
        _record_poll(c, chain, result, started)
        return result

    raw = page.get("trades") or []
    # A sweep runs on mints we have never scanned, so ``tokens.decimals`` is usually
    # absent. ``token_flow`` records the same fallback as an assumption rather than a
    # fact: pump.fun mints are 6 decimals in practice, and a wrong exponent is a silent
    # factor-of-a-million error in every amount.
    decimals = token_flow.token_decimals(chain, mint, c)
    if decimals is None:
        decimals = token_flow.DEFAULT_TOKEN_DECIMALS
    rows = [
        row
        for row in (
            token_flow.parse_trade(r, mint, decimals=decimals, chain=chain)
            for r in raw
            if isinstance(r, Mapping)
        )
        if row is not None
    ]
    if rows:
        token_flow.write_trades(c, rows)

    detected = now_ms()
    entity_map = entity_mod.entity_ids_for(chain, [r.wallet for r in rows if r.wallet in seen], c)
    detections = tuple(
        Detection(
            chain=chain,
            wallet=row.wallet,
            token=mint,
            side=row.side,
            tx=row.tx,
            slot=row.slot,
            block_ms=row.ts_ms,
            detected_ms=detected,
            usd_value=row.usd_value,
            amount_native=row.amount_native,
            route=ROUTE_PUMPFUN,
            entity_id=entity_map.get(row.wallet),
        )
        for row in rows
        if row.wallet in seen
    )
    fresh = tuple(record_detections(c, detections))
    result = PollResult(ROUTE_PUMPFUN, mint, True, rtt, PUMPFUN_POLL_CREDITS, len(rows), fresh)
    _record_poll(c, chain, result, started)
    return result


def sweep_tokens(
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    mints: Sequence[str] | None = None,
    config: TrackerConfig = DEFAULT_CONFIG,
) -> list[PollResult]:
    """One pass of the free per-token route over the hottest mints.

    MEASURED over 915 consecutive polls in one 32-minute run on 2026-09-20: **2.1 s of
    wall clock per mint**, not the 1 s the limiter's minimum interval implies, and **87.5%
    availability** — 114 of those 915 polls returned 503 even after ``FlowConfig.retries``
    attempts with backoff. (``token_flow``'s own note says a retry a second later
    "succeeded every time it was observed"; over a larger sample that is not the case, and
    a miss costs several times what a hit does.)

    That pacing *is* the coverage ceiling and it is the honest limit of this route for
    per-wallet detection: about 29 mints a minute against a venue launching roughly 14 new
    ones in the same minute, with thousands already live. A sweep of 30 mints took ~62 s,
    which is why the run's realised tape lag had a p50 of 61 s against a 1.19 s floor —
    the route is nearly real time and the *cadence* is what makes detection late.
    """
    c = _conn(conn)
    targets = list(mints) if mints is not None else hot_mints(config.token_sweep_size, c)[0]
    seen = watched_addresses(chain, c)
    return [poll_token(chain, mint, c, watched=seen, config=config) for mint in targets]


# -- Route W: per wallet, 10 credits a poll ------------------------------------------------


def _swap_from_transaction(tx: Mapping[str, Any], owner: str) -> tuple[str, str, int | None] | None:
    """``(mint, side, lamport_delta)`` for the owner, or ``None`` when it was not a swap.

    Reads ``meta.pre/postTokenBalances`` filtered to the owner plus the owner's lamport
    delta. MEASURED against a live wallet on 2026-09-20: buys show a positive token delta
    with a negative lamport delta and sells the reverse; fee-claim and transfer
    transactions show no non-WSOL token delta at all and are correctly skipped.
    """
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None  # a failed transaction is not a trade

    pre: dict[str, Decimal] = {}
    post: dict[str, Decimal] = {}
    for bucket, out in ((meta.get("preTokenBalances") or [], pre), (meta.get("postTokenBalances") or [], post)):
        if not isinstance(bucket, list):
            continue
        for entry in bucket:
            if not isinstance(entry, Mapping) or entry.get("owner") != owner:
                continue
            amount = _dec(((entry.get("uiTokenAmount") or {}) or {}).get("amount"))
            mint = str(entry.get("mint") or "")
            if amount is None or not mint:
                continue
            out[mint] = out.get(mint, Decimal(0)) + amount

    deltas = {
        mint: post.get(mint, Decimal(0)) - pre.get(mint, Decimal(0))
        for mint in (set(pre) | set(post))
        if mint != WSOL_MINT
    }
    moved = {mint: delta for mint, delta in deltas.items() if delta != 0}
    if len(moved) != 1:
        return None  # zero legs is not a trade; several is a route we will not guess at

    mint, delta = next(iter(moved.items()))

    lamports = _owner_lamport_delta(tx, owner)
    return mint, ("buy" if delta > 0 else "sell"), lamports


def _owner_lamport_delta(tx: Mapping[str, Any], owner: str) -> int | None:
    """The owner's native balance change, or ``None`` when the page did not carry it.

    ``None`` rather than 0, because "we could not read the balances" and "the balance did
    not move" are different facts and only the second one is evidence.
    """
    message = (tx.get("transaction") or {}).get("message") or {}
    keys = message.get("accountKeys")
    if not isinstance(keys, list) or owner not in keys:
        return None
    idx = keys.index(owner)
    meta = tx.get("meta") or {}
    pre_list = meta.get("preBalances") or []
    post_list = meta.get("postBalances") or []
    if len(pre_list) <= idx or len(post_list) <= idx:
        return None
    pre_b, post_b = _int_or_none(pre_list[idx]), _int_or_none(post_list[idx])
    if pre_b is None or post_b is None:
        return None
    return post_b - pre_b


def poll_wallet(
    chain: Chain,
    address: str,
    conn: sqlite3.Connection | None = None,
    *,
    config: TrackerConfig = DEFAULT_CONFIG,
    baseline_only: bool = False,
) -> PollResult:
    """One Helius page for one watched address. MEASURED cost: 10 credits, ~1.9 s.

    The first poll of an address only sets the cursor and reports nothing, because
    everything it can see is history and calling month-old transactions a "detection"
    would put a fictional latency into the distribution this module exists to measure.
    """
    c = _conn(conn)
    addr = _norm(address, chain)
    entry = get_entry(chain, addr, c)
    started = now_ms()
    t0 = time.monotonic()
    result, receipt = helius.get_transactions_for_address(
        addr,
        limit=config.wallet_poll_limit,
        transaction_details="full",
        ttl_s=0.0,
        priority=Priority.DISCOVERY,
        conn=c,
    )
    rtt = int((time.monotonic() - t0) * 1000)
    detected = now_ms()

    if result is None:
        poll = PollResult(ROUTE_HELIUS, addr, False, rtt, 0, 0, note=(receipt.note or "unavailable")[:300])
        _record_poll(c, chain, poll, started)
        return poll

    rows = [r for r in (result.get("data") or []) if isinstance(r, Mapping)]
    cursor = entry.cursor_sig if entry else None
    first_poll = baseline_only or cursor is None

    detections: list[Detection] = []
    newest_sig: str | None = None
    newest_ms: int | None = None
    entity_id = entity_mod.entity_ids_for(chain, [addr], c).get(addr)
    for tx in rows:
        signature = ((tx.get("transaction") or {}).get("signatures") or [None])[0]
        if not signature:
            continue
        block_s = _int_or_none(tx.get("blockTime"))
        block_ms = block_s * 1000 if block_s is not None else None
        if newest_sig is None:
            newest_sig, newest_ms = str(signature), block_ms
        if str(signature) == cursor:
            break  # everything from here down has already been polled
        if first_poll:
            continue
        parsed = _swap_from_transaction(tx, addr)
        if parsed is None:
            continue
        mint, side, lamports = parsed
        detections.append(
            Detection(
                chain=chain,
                wallet=addr,
                token=mint,
                side=side,
                tx=str(signature),
                slot=_int_or_none(tx.get("slot")),
                block_ms=block_ms,
                detected_ms=detected,
                # Helius's raw RPC page carries no USD figure; inventing one from a price
                # lookup here would attach a number with a different observation time to
                # a trade receipt. It stays None with the amount recorded in lamports.
                usd_value=None,
                amount_native=None if lamports is None else abs(lamports),
                route=ROUTE_HELIUS,
                entity_id=entity_id,
            )
        )

    fresh = tuple(record_detections(c, detections))
    if newest_sig:
        try:
            c.execute(
                "UPDATE tracker_watchlist SET cursor_sig=?, cursor_ms=?, last_checked_ms=? "
                "WHERE chain=? AND address=?",
                (newest_sig, newest_ms, detected, chain.value, addr),
            )
        except sqlite3.Error as exc:
            log.warning("tracker: cursor update failed for %s: %s", addr[:10], exc)

    poll = PollResult(
        ROUTE_HELIUS,
        addr,
        True,
        rtt,
        HELIUS_POLL_CREDITS,
        len(rows),
        fresh,
        note="baseline" if first_poll else "",
    )
    _record_poll(c, chain, poll, started)
    return poll


def poll_wallets(
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    addresses: Sequence[str] | None = None,
    config: TrackerConfig = DEFAULT_CONFIG,
) -> list[PollResult]:
    """One pass of the paid per-wallet route over the pollable part of the watchlist.

    Without an explicit list this covers :func:`pollable_addresses`, not every active row:
    cohort wallets are reported by the feed that nominated them and are not paid for twice.
    """
    c = _conn(conn)
    targets = list(addresses) if addresses is not None else sorted(pollable_addresses(chain, c))
    return [poll_wallet(chain, addr, c, config=config) for addr in targets]


def projected_wallet_cost(
    wallets: int, poll_interval_s: float, *, days: int = 30
) -> dict[str, Any]:
    """What Route W costs at a given watchlist size and interval, and whether it fits.

    Pure arithmetic over one measured constant (10 credits a poll), exposed as a function
    because the answer to "is ``trusted-copy`` viable" is a budget question and should be
    recomputable rather than quoted from a docstring.
    """
    if wallets <= 0 or poll_interval_s <= 0:
        return {"credits": 0, "fits_free_tier": True, "expected_lag_s": None}
    polls = wallets * (86_400.0 / poll_interval_s) * days
    credits = polls * HELIUS_POLL_CREDITS
    allowance = helius.monthly_allowance()
    return {
        "wallets": wallets,
        "poll_interval_s": poll_interval_s,
        "days": days,
        "polls": int(polls),
        "credits": int(credits),
        "monthly_allowance": allowance,
        "fits_free_tier": credits <= allowance,
        "multiple_of_allowance": round(credits / allowance, 2) if allowance else None,
        # Uniform arrival within the interval, plus the measured RTT floor.
        "expected_lag_s": round(poll_interval_s / 2 + 1.9, 1),
        "worst_case_lag_s": round(poll_interval_s + 1.9, 1),
    }


def max_free_tier_wallets(poll_interval_s: float, *, days: int = 30) -> int:
    """How many wallets Route W can watch at this interval inside the free allowance."""
    if poll_interval_s <= 0:
        return 0
    per_wallet = (86_400.0 / poll_interval_s) * days * HELIUS_POLL_CREDITS
    return int(helius.monthly_allowance() // per_wallet) if per_wallet else 0


def webhook_readiness(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Whether the one affordable low-latency per-wallet route can be turned on.

    Helius webhooks are 1 credit per push and only charge when something happens, which is
    two to three orders of magnitude cheaper than polling fast enough for a 20-second
    lane. ``kaiba/providers/helius.py`` already implements the receiver and its HMAC
    check. The blocker is infrastructure, not code, and naming it is more useful than
    quietly polling slowly instead.
    """
    from kaiba.core.config import get_settings

    settings = get_settings()
    secret = bool(getattr(settings, "helius_webhook_secret", None))
    return {
        "receiver_implemented": hasattr(helius, "handle_webhook"),
        "hmac_secret_configured": secret,
        "public_https_endpoint": False,
        "credits_per_push": helius.CREDIT_COSTS[helius.ROUTE_WEBHOOK].credits,
        "push_cost_verified": helius.CREDIT_COSTS[helius.ROUTE_WEBHOOK].verified,
        "blocker": (
            "no publicly reachable HTTPS endpoint on this host. The receiver, the signature "
            "check and the credit accounting already exist; a webhook on the VPS in deploy/ is "
            "the only route to per-wallet detection inside the free tier."
        ),
        "ready": False,
    }


# --------------------------------------------------------------------------------------
# entity-counted confluence windows
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WindowResult:
    """One evaluated window. Recorded whether or not it qualified."""

    chain: Chain
    token: str
    start_ms: int
    end_ms: int
    buyers: tuple[str, ...]
    entities: tuple[str, ...]
    watched_buyers: tuple[str, ...]
    min_usd: Decimal
    qualifying: bool

    @property
    def buyer_count(self) -> int:
        return len(self.buyers)

    @property
    def entity_count(self) -> int:
        return len(self.entities)


def scan_windows(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection | None = None,
    *,
    config: TrackerConfig = DEFAULT_CONFIG,
    since_ms: int | None = None,
    persist: bool = True,
) -> list[WindowResult]:
    """Every ``window_s`` window on this token's tape, counted in entities.

    Counting **entities and not addresses** is the whole point. Five addresses funded from
    one source are one opinion (``docs/TRADING-METHOD.md`` §3, rule 1), and without the
    collapse ``confluence-5`` is spoofable by anyone with a script and 0.1 SOL.

    **This measures an upper bound on ``confluence-5``, not the lane itself.** Two caveats
    travel with every number it produces, and both point the same way:

    1. The entity graph collapses addresses only where :mod:`kaiba.intelligence.cluster`
       has found an edge. Where it has not, the entity count degenerates to the address
       count — the *permissive* direction, which can only overstate independence. Every
       :class:`WindowResult` carries its member addresses so the claim can be re-derived
       once clustering has caught up.

       This is not hypothetical. MEASURED on 2026-09-20: 740 windows were evaluated over
       307 tokens while the ``entities`` table held 174 entities covering 559 addresses,
       and **``buyers > entities`` was true in exactly zero of them**. The anti-spoofing
       guard never engaged once, because none of the clustered addresses were among the
       buyers we observed. Treat every entity count from that run as an address count.
    2. ``kaiba/execution/lanes.py::confluence_5`` additionally requires **every one of the
       qualifying buyers to be individually graded at or above** its
       ``require_wallet_grade`` (``B`` as configured) *before* it counts entities. So a
       window this function calls qualifying is necessary for the lane and nowhere near
       sufficient: the lane needs five separately graded-B buyers in it.

    Read a qualifying window here as "the loosest version of the question said yes". If
    even the loose version never says yes, the lane certainly cannot fire.
    """
    c = _conn(conn)
    floor = since_ms if since_ms is not None else now_ms() - config.scan_lookback_s * 1000
    rows = fetch_all(
        c,
        "SELECT ts_ms, wallet, usd_value FROM swaps WHERE chain=? AND token=? AND side='buy' "
        "AND ts_ms >= ? ORDER BY ts_ms",
        (chain.value, token, floor),
    )
    buys: list[tuple[int, str]] = []
    for row in rows:
        usd = _dec(row["usd_value"])
        # A buy whose USD value we do not know cannot be shown to clear the $50 floor, so
        # it does not count towards the window. Unknown is not average.
        if usd is None or usd < config.confluence_min_buy_usd:
            continue
        buys.append((int(row["ts_ms"]), str(row["wallet"])))
    if not buys:
        return []

    watched = watched_addresses(chain, c)
    # One lookup for every buyer on the tape rather than one per window: a hot mint has
    # hundreds of windows over the same few dozen addresses.
    entity_map = entity_mod.entity_ids_for(chain, [w for _t, w in buys], c)
    span = config.confluence_window_s * 1000
    results: list[WindowResult] = []
    left = 0
    for right, (ts, _wallet) in enumerate(buys):
        while buys[left][0] < ts - span:
            left += 1
        members = [w for _t, w in buys[left : right + 1]]
        unique = sorted(set(members))
        if len(unique) < 2:
            continue  # a window with one buyer is not a window, it is a trade
        keys = sorted({entity_map.get(a, f"{entity_mod.SOLO_PREFIX}{a}") for a in unique})
        results.append(
            WindowResult(
                chain=chain,
                token=token,
                start_ms=buys[left][0],
                end_ms=ts,
                buyers=tuple(unique),
                entities=tuple(keys),
                watched_buyers=tuple(a for a in unique if a in watched),
                min_usd=config.confluence_min_buy_usd,
                qualifying=len(keys) >= config.confluence_min_entities,
            )
        )

    # Keep only the widest window per start, so a hot mint does not write one row per
    # trade. "Widest" is ordered on entities first and addresses second: the entity count
    # is the claim, and the address count is what a reader needs to check it against.
    best: dict[int, WindowResult] = {}
    for window in results:
        current = best.get(window.start_ms)
        if current is None or (window.entity_count, window.buyer_count) > (
            current.entity_count,
            current.buyer_count,
        ):
            best[window.start_ms] = window
    windows = [best[k] for k in sorted(best)]

    if persist:
        stamp = now_ms()
        for window in windows:
            try:
                c.execute(
                    "INSERT OR REPLACE INTO tracker_windows (chain, token, window_start_ms, "
                    " window_end_ms, buyers, entities, watched_buyers, min_usd, qualifying, "
                    " scanned_ms, detail_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        chain.value,
                        token,
                        window.start_ms,
                        window.end_ms,
                        window.buyer_count,
                        window.entity_count,
                        len(window.watched_buyers),
                        str(window.min_usd),
                        1 if window.qualifying else 0,
                        stamp,
                        jdump(
                            {
                                "buyers": list(window.buyers)[:40],
                                "entities": list(window.entities)[:40],
                                "watched": list(window.watched_buyers)[:40],
                                "entity_table_populated": entity_mod.entity_count(chain, c) > 0,
                            }
                        ),
                    ),
                )
            except sqlite3.Error as exc:
                log.warning("tracker: window write failed for %s: %s", token[:10], exc)
        for window in windows:
            if window.qualifying:
                emit(
                    EventKind.ALPHA_SIGNAL,
                    {
                        "tracker": MODEL_ID,
                        "observation": True,
                        "token": token,
                        "entities": window.entity_count,
                        "buyers": window.buyer_count,
                        "window_s": config.confluence_window_s,
                        "note": (
                            "an entity-counted confluence window was observed; the lane decides "
                            "whether it means anything and every lane is in shadow"
                        ),
                    },
                    chain=chain,
                    subject=token,
                    conn=c,
                )
    return windows


def scan_recent_tokens(
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    config: TrackerConfig = DEFAULT_CONFIG,
    since_ms: int | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Run :func:`scan_windows` over every token with recent buy tape. No network calls.

    The report includes ``lane_reachable``, which is the question behind the question. The
    lane needs ``min_entities`` buyers that are *each* graded at its dossier floor, so if
    the whole database holds fewer graded-B wallets than that, no tape whatsoever can make
    it fire. That is an arithmetic fact about our coverage rather than a fact about the
    market, and it should be visible in the same output as the window counts so the two
    are never confused.
    """
    c = _conn(conn)
    floor = since_ms if since_ms is not None else now_ms() - config.scan_lookback_s * 1000
    rows = fetch_all(
        c,
        "SELECT token, COUNT(*) AS n FROM swaps WHERE chain=? AND side='buy' AND ts_ms >= ? "
        "GROUP BY token ORDER BY n DESC LIMIT ?",
        (chain.value, floor, int(limit)),
    )
    best = 0
    qualifying = 0
    scanned = 0
    collapsed = 0
    distribution: dict[int, int] = {}
    for row in rows:
        windows = scan_windows(chain, str(row["token"]), c, config=config, since_ms=floor)
        scanned += 1
        for window in windows:
            distribution[window.entity_count] = distribution.get(window.entity_count, 0) + 1
            best = max(best, window.entity_count)
            qualifying += 1 if window.qualifying else 0
            collapsed += 1 if window.buyer_count > window.entity_count else 0
    evaluated = sum(distribution.values())
    return {
        "tokens_scanned": scanned,
        "windows_evaluated": evaluated,
        "max_entities_in_any_window": best,
        "qualifying_windows": qualifying,
        "required_entities": config.confluence_min_entities,
        "window_s": config.confluence_window_s,
        "min_buy_usd": str(config.confluence_min_buy_usd),
        "entity_distribution": dict(sorted(distribution.items())),
        "entity_table_populated": entity_mod.entity_count(chain, c) > 0,
        # A populated entities table proves nothing on its own. This says whether the
        # collapse actually fired on the tape we scanned. Zero here means every entity
        # count below is really an address count, and independence is unverified.
        "windows_where_collapse_fired": collapsed,
        "collapse_effective": collapsed > 0,
        "lane_reachable": lane_reachability(chain, c, config=config),
    }


def lane_reachability(
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    config: TrackerConfig = DEFAULT_CONFIG,
) -> dict[str, Any]:
    """Can ``confluence-5`` and ``sm-trenches`` fire at our present grading coverage?

    Both lanes count only buyers that already clear a grade floor, so the number of
    wallets in the whole database at that grade is a hard ceiling on the number that can
    appear in any one window. When that ceiling is below the lane's entity requirement,
    the lane cannot fire on any tape at all, and no amount of tracking changes it.
    """
    c = _conn(conn)
    try:
        risk = get_risk()
        floor = str(risk.lane(Lane.CONFLUENCE_5).params.get("require_wallet_grade", "B"))
    except Exception:  # noqa: BLE001
        floor = "B"
    order = [Grade.A, Grade.B, Grade.C, Grade.D]
    try:
        allowed = [g.value for g in order[: order.index(Grade(floor)) + 1]]
    except ValueError:
        allowed = [Grade.A.value, Grade.B.value]
    placeholders = ",".join("?" for _ in allowed)
    row = fetch_one(
        c,
        f"SELECT COUNT(*) AS n FROM wallet_scores WHERE chain=? AND grade IN ({placeholders})",
        (chain.value, *allowed),
    )
    at_floor = int(row["n"]) if row else 0
    return {
        "dossier_grade_floor": floor,
        "wallets_at_or_above_floor": at_floor,
        "confluence_5_requires": config.confluence_min_entities,
        "confluence_5_reachable": at_floor >= config.confluence_min_entities,
        "sm_trenches_requires": config.trenches_min_wallets,
        "sm_trenches_reachable": at_floor >= config.trenches_min_wallets,
        "note": (
            "lanes.py::confluence_5 filters buyers by grade before counting entities, so "
            "this count is a hard ceiling on the entities any window can contribute"
        ),
    }


# --------------------------------------------------------------------------------------
# reporting: latency, cost, and the viability verdict
# --------------------------------------------------------------------------------------


def latency_report(
    conn: sqlite3.Connection | None = None,
    *,
    route: str | None = None,
    config: TrackerConfig = DEFAULT_CONFIG,
    since_ms: int | None = None,
) -> dict[str, Any]:
    """The measured on-chain-to-database distribution, and the 20-second verdict.

    Reports the whole distribution rather than the best case, because the lane's threshold
    is a ceiling and a median that clears it while the p95 does not is a lane that fails
    in exactly the cases it was built for.
    """
    c = _conn(conn)
    sql = "SELECT route, lag_ms FROM tracker_detections WHERE lag_ms IS NOT NULL"
    params: list[Any] = []
    if route:
        sql += " AND route=?"
        params.append(route)
    if since_ms is not None:
        sql += " AND detected_ms >= ?"
        params.append(since_ms)
    rows = fetch_all(c, sql, params)

    missing = fetch_one(
        c, "SELECT COUNT(*) AS n FROM tracker_detections WHERE lag_ms IS NULL", ()
    )
    by_route: dict[str, list[float]] = {}
    for row in rows:
        by_route.setdefault(str(row["route"]), []).append(float(row["lag_ms"]) / 1000.0)

    budget = float(config.copy_delay_budget_s)
    out: dict[str, Any] = {
        "copy_delay_budget_s": budget,
        "detections_without_block_time": int(missing["n"]) if missing else 0,
        "routes": {},
    }
    for name, lags in sorted(by_route.items()):
        within = sum(1 for lag in lags if lag <= budget)
        out["routes"][name] = {
            "samples": len(lags),
            "min_s": round(min(lags), 2),
            "p50_s": round(statistics.median(lags), 2),
            "p90_s": round(_percentile(lags, 90) or 0.0, 2),
            "p95_s": round(_percentile(lags, 95) or 0.0, 2),
            "max_s": round(max(lags), 2),
            "within_budget": within,
            "within_budget_pct": round(100.0 * within / len(lags), 1),
            # The verdict is stated on p95 and not on the median on purpose.
            "viable_for_trusted_copy": (_percentile(lags, 95) or float("inf")) <= budget,
        }
    if not out["routes"]:
        out["note"] = "no detection carried a block time, so no latency has been measured"
    return out


def cost_report(conn: sqlite3.Connection | None = None, *, since_ms: int | None = None) -> dict[str, Any]:
    """What tracking has cost and what it caught, per route."""
    c = _conn(conn)
    sql = (
        "SELECT route, COUNT(*) AS polls, SUM(ok) AS ok, SUM(credits) AS credits, "
        " SUM(rows_seen) AS rows_seen, SUM(detections) AS detections, "
        " AVG(rtt_ms) AS avg_rtt FROM tracker_polls"
    )
    params: list[Any] = []
    if since_ms is not None:
        sql += " WHERE started_ms >= ?"
        params.append(since_ms)
    sql += " GROUP BY route"
    routes: dict[str, Any] = {}
    for row in fetch_all(c, sql, params):
        polls = int(row["polls"])
        ok = int(row["ok"] or 0)
        detections = int(row["detections"] or 0)
        credits = int(row["credits"] or 0)
        routes[str(row["route"])] = {
            "polls": polls,
            "ok": ok,
            "failed": polls - ok,
            "availability_pct": round(100.0 * ok / polls, 1) if polls else None,
            "credits": credits,
            "rows_seen": int(row["rows_seen"] or 0),
            "detections": detections,
            "credits_per_detection": round(credits / detections, 1) if detections else None,
            "avg_rtt_ms": int(row["avg_rtt"] or 0),
        }
    return {"routes": routes, "helius_budget": helius.budget_status(conn=c)}


def status(
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    config: TrackerConfig = DEFAULT_CONFIG,
) -> dict[str, Any]:
    """One call for the dashboard and for the operator: what is watched, seen and costing."""
    c = _conn(conn)
    entries = watchlist(chain, c)
    detections = fetch_one(
        c,
        "SELECT COUNT(*) AS n, SUM(side='buy') AS buys FROM tracker_detections WHERE chain=?",
        (chain.value,),
    ) or {"n": 0, "buys": 0}
    windows = fetch_one(
        c,
        "SELECT COUNT(*) AS n, MAX(entities) AS best, SUM(qualifying) AS qual "
        "FROM tracker_windows WHERE chain=?",
        (chain.value,),
    ) or {"n": 0, "best": None, "qual": 0}
    return {
        "model": MODEL_ID,
        "watchlist_active": len(entries),
        "watchlist_by_tier": {
            tier.value: sum(1 for e in entries if e.tier is tier) for tier in Tier
        },
        "detections": int(detections["n"] or 0),
        "buys_detected": int(detections["buys"] or 0),
        "windows_evaluated": int(windows["n"] or 0),
        "max_entities_in_any_window": windows["best"],
        "qualifying_windows": int(windows["qual"] or 0),
        "entity_table_populated": entity_mod.entity_count(chain, c) > 0,
        "latency": latency_report(c, config=config),
        "cost": cost_report(c),
        "webhook": webhook_readiness(c),
        "never_promotes": "the tracker observes; only measured forward performance may promote",
    }


__all__ = [
    "ADMISSION_POLICY_COHORT",
    "ADMISSION_POLICY_STANDARD",
    "COHORT_PROVENANCE",
    "COHORT_SCREEN_CREDITS_SOL",
    "COHORT_SOURCE_PREFIX",
    "COHORT_WAIVABLE_CODES",
    "DEFAULT_CONFIG",
    "HELIUS_POLL_CREDITS",
    "LANE_SMART_ARCHETYPES",
    "LANE_SMART_TAGS",
    "MODEL_ID",
    "PUMPFUN_POLL_CREDITS",
    "ROUTE_GMGN_FEED",
    "ROUTE_HELIUS",
    "ROUTE_PUMPFUN",
    "Action",
    "CohortSeedReport",
    "Detection",
    "PollResult",
    "Screen",
    "Tier",
    "TrackerConfig",
    "Verdict",
    "WatchlistEntry",
    "WindowResult",
    "admit",
    "audit_trail",
    "cohort_verdict",
    "config_from_risk",
    "lane_smart_wallets",
    "pollable_addresses",
    "seed_from_cohorts",
    "standard_verdict",
    "trenches_input_report",
    "cost_report",
    "get_entry",
    "hot_mints",
    "lane_reachability",
    "latency_report",
    "max_free_tier_wallets",
    "measure_failure_rate",
    "poll_token",
    "poll_wallet",
    "poll_wallets",
    "projected_wallet_cost",
    "record_detections",
    "remove",
    "rescreen",
    "scan_recent_tokens",
    "scan_windows",
    "screen_wallet",
    "seed_from_discovery",
    "seed_from_grades",
    "set_tier",
    "status",
    "sweep_tokens",
    "watched_addresses",
    "watchlist",
    "webhook_readiness",
]
