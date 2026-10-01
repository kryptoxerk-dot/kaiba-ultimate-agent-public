"""One wallet rubric, PnL-first, evidence-normalised.

This replaces two prior scorers that disagreed with each other:

* Kaiba Intelligence ``grade.mjs`` — eight components over 100 points, normalised by the
  evidence that was actually available, penalties for bot/wash/cadence behaviour. Its
  structural flaw was that seeds came from trending lists, so ``seed_confluence`` was
  always zero and no wallet ever reached A.
* KAIBA CORP AGENT ``kaiba-wallet-copyability-v1.0`` plus wallet-agent SCORING-V2 — the
  "actual winner" definition (a closed position that returned ≥6×), round-trip
  reconstruction, and the UNSCORED/QUARANTINED outcomes that admit when we do not know.

The unification keeps the eight-component shape, swaps provider PnL for our own
reconstruction (:mod:`kaiba.intelligence.pnl`), and keeps the honest outcomes. Two
principles drive every decision below:

1. **A component we could not measure is not a zero.** ``evidence_weight`` is the sum of
   the maximum points of the components that had data; the score is normalised over that.
   A wallet with three components cannot reach A no matter how good those three look.
2. **Grades are gated on sample size, not only on score.** 90 points off four closed
   trades is a rumour. It caps at B and says why in ``blockers``.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kaiba.core.db import ensure_db, fetch_all, fetch_one, get_conn, jdump, jload, tx, upsert
from kaiba.core.events import emit
from kaiba.core.schemas import (
    HARD_QUARANTINE_TAGS,
    SOFT_PENALTY_TAGS,
    Archetype,
    Chain,
    EventKind,
    EvidenceBasis,
    Grade,
    Receipt,
    ScoreFactor,
    WalletScore,
    WalletTag,
    normalize_address,
)
from kaiba.intelligence.pnl import MATH, ZERO, Episode, WalletPnl, ratio

log = logging.getLogger(__name__)

MODEL_ID = "kaiba-wallet-v1"
#: Grades built from our own partial tape carry their own model id so a reader of
#: ``wallet_scores`` can tell a provisional tape grade from a backfilled one.
MODEL_ID_TAPE = "kaiba-wallet-tape-v1"

#: Component name -> maximum points. Sums to 100 by construction.
COMPONENT_MAX: dict[str, float] = {
    "realized_profit": 20.0,
    "roi": 14.0,
    "win_rate": 12.0,
    "big_win_rate": 11.0,
    "early_edge": 16.0,
    "seed_confluence": 12.0,
    "breadth": 9.0,
    "reputation": 6.0,
}

#: Below this much evidence we do not have an opinion. Saying "C" would be a lie.
MIN_EVIDENCE_WEIGHT = 30.0

#: Grade A additionally needs a real sample behind it.
A_MIN_SCORE = 70.0
A_MIN_EVIDENCE_WEIGHT = 75.0
A_MIN_CLOSED_EPISODES = 10
A_MIN_DISTINCT_TOKENS = 5

B_MIN_SCORE = 40.0
C_MIN_SCORE = 20.0

#: Provider-reported profit is worth partial credit, never full credit.
PROVIDER_CREDIT = Decimal("0.6")

POSITIVE_REPUTATION_TAGS: tuple[WalletTag, ...] = (
    WalletTag.KOL,
    WalletTag.SMART_MONEY,
    WalletTag.RENOWNED,
    WalletTag.BLUECHIP_OWNER,
)

# ---------------------------------------------------------------- vendor labels
#
# GMGN labels its feed wallets (smart_degen, kol, wash_trader, top_followed ...). A label
# is a vendor's opinion with no published method; MEASURED 2026-09-21 on the live box the
# feeds had labelled 1,741 wallets, 18 of them wash_trader/sandwich_bot. Two consumers
# would take a positive label at face value if it were ever spelled in their vocabulary:
# ``POSITIVE_REPUTATION_TAGS`` above (reputation points on the backfill path) and
# ``kaiba.execution.lanes.SMART_TAGS`` plus the lane's ``wallet_scores.archetype in
# {smart_money, top_trader}`` route (a wallet counted as smart by sm-trenches). The only
# path allowed to spell ``smart_money`` on a wallet is one that screened it —
# ``tracker.seed_from_cohorts``. So:
#
#   * ``naming`` writes every GMGN label into ``wallets.tags_json`` under the
#     ``VENDOR_TAG_PREFIX`` namespace (``gmgn:smart_degen``), never bare;
#   * this module admits a vendor label as a :class:`WalletTag` only when it can *lower*
#     the wallet (``VENDOR_ADMITTED_TAGS``), on both the tags_json and the event-bus path.
#     A positive vendor label is recorded verbatim (``TapeCoverage.provider_tags``,
#     ``meta.naming.gmgn_tags``) and never becomes a tag, an archetype input or a point.

#: Prefix ``naming`` puts on a GMGN label in ``wallets.tags_json``.
VENDOR_TAG_PREFIX = "gmgn:"

#: The only vendor labels the grader turns into a tag: a hard quarantine or a soft penalty.
#: Fail-closed by construction — a wrong quarantine costs nothing, a wrong follow costs money.
VENDOR_ADMITTED_TAGS: frozenset[WalletTag] = HARD_QUARANTINE_TAGS | SOFT_PENALTY_TAGS

#: Wallets that mostly trade tokens they created are marking their own homework.
CREATOR_SELF_DEALING_MULTIPLIER = Decimal("0.45")

# ---------------------------------------------------------------- sell-only wallet filter
#
# Published pump.fun PnL leaderboards are not all traders. `docs/research/13-validation-
# and-copytrading-2026.md` and the audit's finding 3 record top-ranked wallets that
# executed **only sells** — one with 1,793 trades and zero buys. Those are settlement,
# aggregation and market-maker payout addresses: inventory arrives off-book through
# transfers, mint authority or an internal ledger, and is liquidated on-chain. Their
# realised PnL is real money and it is not a decision anyone could copy, because the
# decision that made it happen never appears on chain.
#
# This rubric would have ranked such an address highly. Every profit component keys off
# proceeds, `_frac_band` on sell-to-buy decays only above 1.5, and nothing else asks
# whether the wallet ever bought anything. So the check runs *before* scoring rather than
# as one more subtraction that a large realised profit could absorb.
#
# The rule is stated on buy *share* rather than on the sell/buy ratio because the ratio is
# undefined at zero buys, which is precisely the case that matters.
#
#   * fewer than SELL_ONLY_MIN_TRADES observations -> no opinion. One sell and no buy is a
#     thin sample, not a settlement address; that wallet is UNSCORED on evidence anyway.
#   * buy share at or below SELL_ONLY_MAX_BUY_SHARE -> refuse to score. 2% rather than a
#     literal zero so one incidental buy cannot launder an otherwise sell-only address.
#   * buy share below BUY_STARVED_MAX_BUY_SHARE -> heavy subtraction. Between 2% and 10%
#     the address may be a genuine trader that also settles somebody else's flow, so it is
#     penalised rather than refused, and the reason is on the score for a human to read.
#
# The counts come from the raw `swaps` rows rather than from reconstructed episodes.
# `pnl.summarize` excludes contaminated episodes, and an episode with no buy is flagged
# `sell_without_buy` and dropped — so exactly the wallets this filter exists to catch are
# the ones whose asymmetry the episode aggregate hides.
SELL_ONLY_MIN_TRADES = 10
SELL_ONLY_MAX_BUY_SHARE = Decimal("0.02")
BUY_STARVED_MAX_BUY_SHARE = Decimal("0.10")
BUY_STARVED_PENALTY = 40.0


# --------------------------------------------------------------------------------------
# evidence input
# --------------------------------------------------------------------------------------


class ProviderStats(BaseModel):
    """What a provider claims. Used only as a fallback, and never at full credit."""

    realized_profit_usd: Decimal | None = None
    win_rate: float | None = None
    token_num: int | None = None
    avg_hold_s: int | None = None
    buy_count: int | None = None
    sell_count: int | None = None


class EarlyMetrics(BaseModel):
    """Universe-relative early-buyer counts from ``wallet-grading/05_metrics.py``.

    ``validated_early_tokens`` = top-3 entrant on a token that drew at least ten buyers;
    ``insider_tokens`` = top-2 within 60 s; ``sniper_tokens`` = bought within five minutes.
    ``best_entry_rank`` is the best position this wallet ever took in a token's buy order.
    """

    validated_early_tokens: int = 0
    sniper_tokens: int = 0
    insider_tokens: int = 0
    best_entry_rank: int | None = None


class Reputation(BaseModel):
    twitter: str | None = None
    followers: int | None = None
    verified: bool = False
    kol: bool = False


class TapeCoverage(BaseModel):
    """What our own tape covers for one wallet — and, more importantly, what it cannot.

    ``swaps`` holds the trades we happened to observe: pump.fun trades on tokens we
    subscribed to, pons-curve trades on Robinhood, GMGN feed rows that were on screen when
    we polled. It is never a wallet's full history. A wallet that shows five wins from five
    trades here may carry two hundred unseen losers, so every grade built on this block is
    provisional and :func:`score_wallet` caps it (see ``TAPE_*``).

    ``money_axis`` says what unit the reconstructed PnL is in. On-chain sources store
    lamports/wei as text and the reconstruction is exact in native base units. GMGN feed
    rows store ``amount_native`` as the *quote* amount of whatever pair traded — MEASURED
    2026-09-21 on the live box: only ~54% of sol rows and ~25% of bsc rows were quoted in
    the native coin — so those wallets are reconstructed on their provider-reported
    ``usd_value``, held as integer micro-dollars, and say so here.
    """

    kind: str = "watched_tokens"
    rows: int = 0
    rows_raw: int = 0
    rows_dropped_duplicate: int = 0
    rows_dropped_mixed_units: int = 0
    rows_without_money: int = 0
    sources: dict[str, int] = Field(default_factory=dict)
    feeds: list[str] = Field(default_factory=list)
    distinct_tokens: int = 0
    first_ms: int | None = None
    last_ms: int | None = None
    money_axis: Literal["native", "usd_micro"] = "native"
    money_basis: EvidenceBasis = EvidenceBasis.UNAVAILABLE
    #: The tape only records swaps that executed. Failed transactions never reach it, so a
    #: failure rate cannot be measured here; it stays ``None`` + UNAVAILABLE rather than 0.
    tx_failure_rate: float | None = None
    tx_failure_basis: EvidenceBasis = EvidenceBasis.UNAVAILABLE
    #: Raw GMGN cohort labels as the provider sent them. A label, never a measurement.
    provider_tags: list[str] = Field(default_factory=list)
    provider_tag_basis: EvidenceBasis = EvidenceBasis.UNAVAILABLE

    @property
    def partial(self) -> bool:
        """Always true. Nothing in ``swaps`` can establish that a history is complete."""
        return True

    def describe(self) -> str:
        srcs = ", ".join(f"{k}={v}" for k, v in sorted(self.sources.items())) or "none"
        return (
            f"{self.rows} rows over {self.distinct_tokens} tokens from [{srcs}], money axis "
            f"{self.money_axis} ({self.money_basis.value}); only tokens we watched, unseen "
            "history cannot be ruled out"
        )


class WalletEvidence(BaseModel):
    """Everything the grader is allowed to look at.

    Optional blocks are ``None`` when nobody measured them, which is different from a
    measurement that came back empty. ``seed_confluence=None`` means "we never ran the
    seed pass"; ``seed_confluence=0`` means "we ran it and this wallet touched no seed".
    Only the second one counts as evidence.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    address: str
    chain: Chain
    tags: list[WalletTag] = Field(default_factory=list)
    pnl: WalletPnl | None = None
    provider_stats: ProviderStats | None = None
    early_metrics: EarlyMetrics | None = None
    seed_confluence: int | None = None
    reputation: Reputation | None = None
    created_token_count: int = 0
    sample_capped: bool = False
    #: Set only by :func:`build_tape_evidence`. Its presence switches on the tape caps.
    tape: TapeCoverage | None = None
    #: Raw buy/sell counts straight from `swaps`, before episode reconstruction drops
    #: contaminated rows. Only the sell-only filter reads these; see its block above.
    observed_buys: int | None = None
    observed_sells: int | None = None
    #: Distinct tokens on which at least one clean episode CLOSED. Set by the builders that
    #: replay ``swaps`` (:func:`build_evidence`, :func:`tape_evidence_from_rows`); ``None``
    #: when nobody replayed them. ``pnl.distinct_tokens`` also counts open episodes, and an
    #: open episode is a buy whose exit we have not seen. MEASURED 2026-09-22 on a read-only
    #: replay of the live tape (714,427 swaps): counting them had lifted 62 wallets from D
    #: to C (sol 44, bsc 3, robinhood 15) and the one sol tape B from 44.58 to 48.66 on 37
    #: dangling buys against 9 closed round trips. Breadth and the sample gates read this,
    #: never the open-inclusive count.
    closed_distinct_tokens: int | None = None
    #: Buy and sell rows inside those closed, clean episodes, so the breadth component's
    #: sell-to-buy band is a function of closed episodes only. ``pnl.buys``/``pnl.sells``
    #: keep counting the open ones for the ``no_sells`` penalty, where "never sells" belongs.
    closed_buys: int | None = None
    closed_sells: int | None = None

    # Signals the grader consumes but does not compute itself.
    trade_count_lifetime: int | None = None
    tokens_30d: int | None = None
    #: Address this wallet consistently trades behind — set by the clustering pass.
    lead_lag_of: str | None = None
    #: Precomputed "buys after +200%, sells at a loss, repeatedly" flag.
    fomo_flag: bool = False
    receipts: list[Receipt] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize(self) -> WalletEvidence:
        try:
            self.address = normalize_address(self.address, self.chain)
        except ValueError:
            # An address we cannot normalise is still worth grading; it just stays verbatim.
            self.address = self.address.strip()
        return self

    # -- convenience accessors, so every component reads the same fallbacks -------------

    @property
    def distinct_tokens(self) -> int | None:
        if self.pnl is not None and self.pnl.distinct_tokens:
            return self.pnl.distinct_tokens
        if self.provider_stats is not None:
            return self.provider_stats.token_num
        return None

    @property
    def sample_tokens(self) -> tuple[int | None, str]:
        """``(count, basis)`` for the sample gates and the breadth component.

        Closed round trips when the replay measured them (``closed_episodes``). Otherwise
        the touched-token count out of a hand-built PnL (``episodes_incl_open``) or the
        provider's claim (``provider_reported``), each named so a gate that refuses the
        weaker basis can say so. :func:`_tape_cap` accepts only the first.
        """
        if self.closed_distinct_tokens is not None:
            return self.closed_distinct_tokens, "closed_episodes"
        if self.pnl is not None and self.pnl.distinct_tokens:
            return self.pnl.distinct_tokens, "episodes_incl_open"
        if self.provider_stats is not None and self.provider_stats.token_num is not None:
            return self.provider_stats.token_num, "provider_reported"
        return None, "unavailable"

    @property
    def breadth_sell_to_buy(self) -> tuple[float | None, str]:
        """``(ratio, basis)`` for the breadth band: closed episodes only when replayed."""
        if self.closed_buys is not None and self.closed_sells is not None:
            r = ratio(self.closed_sells, self.closed_buys)
            return (float(r) if r is not None else None), "closed_episodes"
        if self.pnl is not None and self.pnl.sell_to_buy_ratio is not None:
            return self.pnl.sell_to_buy_ratio, "episodes_incl_open"
        return self.sell_to_buy_ratio, "provider_reported"

    @property
    def closed_episodes(self) -> int | None:
        return self.pnl.closed_episodes if self.pnl is not None else None

    @property
    def median_hold_s(self) -> int | None:
        if self.pnl is not None and self.pnl.median_hold_s is not None:
            return self.pnl.median_hold_s
        if self.provider_stats is not None:
            return self.provider_stats.avg_hold_s
        return None

    @property
    def trade_counts(self) -> tuple[int | None, int | None]:
        """(buys, sells) from our reconstruction, else from the provider."""
        if self.pnl is not None and (self.pnl.buys or self.pnl.sells):
            return self.pnl.buys, self.pnl.sells
        if self.provider_stats is not None:
            return self.provider_stats.buy_count, self.provider_stats.sell_count
        return None, None

    @property
    def lifetime_trades(self) -> int | None:
        if self.trade_count_lifetime is not None:
            return self.trade_count_lifetime
        buys, sells = self.trade_counts
        if buys is None and sells is None:
            return None
        return (buys or 0) + (sells or 0)

    @property
    def asymmetry_counts(self) -> tuple[int, int] | None:
        """``(buys, sells)`` for the sell-only filter, strongest source first.

        Raw swap counts beat the provider's claim, which beats the episode aggregate. The
        episode aggregate is last because it drops the very rows that reveal the problem.
        """
        if self.observed_buys is not None and self.observed_sells is not None:
            return int(self.observed_buys), int(self.observed_sells)
        ps = self.provider_stats
        if ps is not None and ps.buy_count is not None and ps.sell_count is not None:
            return int(ps.buy_count), int(ps.sell_count)
        buys, sells = self.trade_counts
        if buys is None or sells is None:
            return None
        return int(buys), int(sells)

    @property
    def sell_to_buy_ratio(self) -> float | None:
        if self.pnl is not None and self.pnl.sell_to_buy_ratio is not None:
            return self.pnl.sell_to_buy_ratio
        ps = self.provider_stats
        if ps is not None and ps.buy_count and ps.sell_count is not None:
            r = ratio(ps.sell_count, ps.buy_count)
            return float(r) if r is not None else None
        return None


class Penalty(BaseModel):
    """A subtraction applied after normalisation, with the reason attached."""

    name: str
    points: float
    detail: str

    def render(self) -> str:
        return f"{self.name} -{self.points:g}: {self.detail}"


# --------------------------------------------------------------------------------------
# scaling helpers (Decimal only — a score is not money, but the inputs are)
# --------------------------------------------------------------------------------------


def _dec(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _frac_linear(value: Any, lo: Any, hi: Any) -> Decimal:
    """0 at or below ``lo``, 1 at or above ``hi``, linear between."""
    v, low, high = _dec(value), _dec(lo), _dec(hi)
    if high <= low or v <= low:
        return ZERO
    if v >= high:
        return Decimal(1)
    return MATH.divide(v - low, high - low)


def _frac_log(value: Any, lo: Any, hi: Any) -> Decimal:
    """Same shape as :func:`_frac_linear` on a log axis, for quantities that span decades."""
    v, low, high = _dec(value), _dec(lo), _dec(hi)
    if low <= 0 or high <= low or v <= low:
        return ZERO
    if v >= high:
        return Decimal(1)
    return MATH.divide(MATH.ln(v) - MATH.ln(low), MATH.ln(high) - MATH.ln(low))


def _frac_band(value: Any, lo: Any, hi: Any) -> Decimal:
    """1 inside ``[lo, hi]``, ramping up from 0 below and decaying to 0 at ``2*hi``.

    Used for sell-to-buy: a wallet that never sells has no realised edge, and one that
    sells far more than it buys is unwinding inventory we never saw it acquire.
    """
    v, low, high = _dec(value), _dec(lo), _dec(hi)
    if low <= v <= high:
        return Decimal(1)
    if v < low:
        return ZERO if low <= 0 else MATH.divide(v, low)
    edge = high * 2
    if v >= edge:
        return ZERO
    return MATH.divide(edge - v, edge - high)


def _blend(parts: list[tuple[Decimal, Decimal]]) -> Decimal | None:
    """Weighted mean renormalised over the sub-parts that had data."""
    if not parts:
        return None
    total = sum((w for _, w in parts), ZERO)
    if total == 0:
        return None
    return MATH.divide(sum((f * w for f, w in parts), ZERO), total)


def _factor(name: str, fraction: Decimal, detail: str) -> ScoreFactor:
    cap = COMPONENT_MAX[name]
    return ScoreFactor(name=name, points=float(fraction * _dec(cap)), max_points=cap, detail=detail)


# --------------------------------------------------------------------------------------
# components
# --------------------------------------------------------------------------------------


def _realized_profit(ev: WalletEvidence) -> ScoreFactor | None:
    pnl = ev.pnl
    if pnl is not None and pnl.realized_pnl_usd is not None:
        usd = pnl.realized_pnl_usd
        frac = _frac_log(usd, 1_000, 2_000_000) if usd > 0 else ZERO
        detail = (
            f"basis=reconstructed realized ${usd:,.0f} over {pnl.closed_episodes} closed episodes"
        )
        return _factor("realized_profit", frac, detail)
    ps = ev.provider_stats
    if ps is not None and ps.realized_profit_usd is not None:
        usd = ps.realized_profit_usd
        frac = _frac_log(usd, 1_000, 2_000_000) if usd > 0 else ZERO
        detail = f"basis=provider_reported ${usd:,.0f} at {PROVIDER_CREDIT:.0%} credit"
        return _factor("realized_profit", frac * PROVIDER_CREDIT, detail)
    return None


def _roi(ev: WalletEvidence) -> ScoreFactor | None:
    if ev.pnl is None or ev.pnl.roi is None:
        return None
    roi = ev.pnl.roi
    return _factor("roi", _frac_linear(roi, "0.15", "1.60"), f"basis=reconstructed roi {roi:.4f}")


def _win_rate(ev: WalletEvidence) -> ScoreFactor | None:
    pnl = ev.pnl
    if pnl is not None and pnl.win_rate is not None:
        detail = f"basis=reconstructed {pnl.win_rate:.3f} over {pnl.closed_episodes} closed episodes"
        return _factor("win_rate", _frac_linear(pnl.win_rate, "0.22", "0.60"), detail)
    ps = ev.provider_stats
    if ps is not None and ps.win_rate is not None:
        detail = f"basis=provider_reported {ps.win_rate:.3f}"
        return _factor("win_rate", _frac_linear(ps.win_rate, "0.22", "0.60"), detail)
    return None


def _big_win_rate(ev: WalletEvidence) -> ScoreFactor | None:
    pnl = ev.pnl
    if pnl is None or pnl.closed_episodes <= 0:
        return None
    rate = ratio(pnl.big_wins, pnl.closed_episodes) or ZERO
    detail = f"{pnl.big_wins}/{pnl.closed_episodes} closed episodes returned >=6x"
    return _factor("big_win_rate", _frac_linear(rate, "0.01", "0.09"), detail)


def _early_edge(ev: WalletEvidence) -> ScoreFactor | None:
    em = ev.early_metrics
    if em is None:
        return None
    frac = _blend(
        [
            (_frac_log(em.validated_early_tokens, 1, 60), Decimal("0.6")),
            (_frac_linear(em.insider_tokens, 1, 15), Decimal("0.25")),
            (_frac_linear(em.sniper_tokens, 1, 50), Decimal("0.15")),
        ]
    )
    detail = (
        f"validated_early={em.validated_early_tokens} insider={em.insider_tokens} "
        f"sniper={em.sniper_tokens}"
    )
    return _factor("early_edge", frac or ZERO, detail)


def _seed_confluence(ev: WalletEvidence) -> ScoreFactor | None:
    if ev.seed_confluence is None:
        return None
    n = ev.seed_confluence
    return _factor("seed_confluence", _frac_linear(n, 1, 3), f"{n} high-quality seed tokens")


def _breadth(ev: WalletEvidence) -> ScoreFactor | None:
    """Token breadth and the sell-to-buy band, both over CLOSED round trips.

    A buy whose exit we never saw is not breadth (:attr:`WalletEvidence.sample_tokens`),
    and it does not move the band either (:attr:`WalletEvidence.breadth_sell_to_buy`):
    on the tape the band sits at 1.0 for a low ratio, so dangling buys would otherwise
    *lift* this term from 0.667 to 1.0 — MEASURED 2026-09-22 on the one sol tape B
    (7X14RGhh…): 49 buys against 12 sells on tape put the band at 1.0 and the token term
    at 39, its 9 closed round trips put them at 0.667 and 9; 48.66 -> 44.58. "Never sells"
    is the ``no_sells`` penalty's job.
    """
    parts: list[tuple[Decimal, Decimal]] = []
    bits: list[str] = []
    tokens, basis = ev.sample_tokens
    if tokens is not None:
        parts.append((_frac_log(tokens, 10, 250), Decimal("0.7")))
        bits.append(f"distinct_tokens={tokens} (basis={basis})")
    s2b, s2b_basis = ev.breadth_sell_to_buy
    if s2b is not None:
        parts.append((_frac_band(s2b, "0.15", "0.75"), Decimal("0.3")))
        bits.append(f"sell_to_buy={s2b:.3f} (basis={s2b_basis})")
    frac = _blend(parts)
    if frac is None:
        return None
    return _factor("breadth", frac, " ".join(bits))


def _reputation(ev: WalletEvidence) -> ScoreFactor | None:
    rep = ev.reputation
    if rep is None and ev.tape is not None:
        # On the tape path the only tags are GMGN cohort labels. "Smart money" there means
        # "a wallet that often earns money" with no published method (research digest,
        # 2026-09-21: a published null for predicting outcomes). They name an archetype;
        # they are not evidence of quality, so they earn nothing here.
        return None
    positive = {t for t in POSITIVE_REPUTATION_TAGS if t in ev.tags}
    if rep is not None and rep.kol:
        positive.add(WalletTag.KOL)
    if rep is None and not positive:
        return None
    cap = _dec(COMPONENT_MAX["reputation"])
    points = Decimal("1.2") * len(positive)
    if rep is not None and rep.verified:
        points += Decimal("1.5")
    followers = (rep.followers if rep is not None else None) or 0
    points += _frac_log(followers, 500, 200_000) * 2
    points = min(points, cap)
    detail = (
        f"tags={sorted(t.value for t in positive)} verified="
        f"{bool(rep and rep.verified)} followers={followers}"
    )
    # This component is expressed directly in points, so hand back the fraction of its cap.
    return _factor("reputation", MATH.divide(points, cap), detail)


_COMPONENTS = (
    _realized_profit,
    _roi,
    _win_rate,
    _big_win_rate,
    _early_edge,
    _seed_confluence,
    _breadth,
    _reputation,
)


def components_for(evidence: WalletEvidence) -> tuple[list[ScoreFactor], list[str]]:
    """Return the factors we could compute and the names of the ones we could not."""
    factors: list[ScoreFactor] = []
    missing: list[str] = []
    for fn in _COMPONENTS:
        factor = fn(evidence)
        if factor is None:
            missing.append(fn.__name__.lstrip("_"))
        else:
            factors.append(factor)
    return factors, missing


# --------------------------------------------------------------------------------------
# penalties
# --------------------------------------------------------------------------------------


def penalties_for(evidence: WalletEvidence) -> list[Penalty]:
    """Subtractive penalties, evaluated independently of the quarantine short-circuit.

    Kept public and pure so the behaviour can be tested without constructing a full score,
    and so a caller that deliberately scores a quarantined wallet still sees every reason.
    """
    ev = evidence
    tags = set(ev.tags)
    out: list[Penalty] = []

    loss = _loss_detail(ev)
    if loss is not None:
        out.append(Penalty(name="realized_pnl_negative", points=35.0, detail=loss))

    dirty = tags & {WalletTag.WASH_TRADER, WalletTag.SANDWICH_BOT, WalletTag.MEV_BOT}
    if dirty:
        detail = ", ".join(sorted(t.value for t in dirty))
        out.append(Penalty(name="wash_or_mev", points=30.0, detail=detail))
    if WalletTag.DEX_BOT in tags:
        out.append(Penalty(name="dex_bot", points=25.0, detail="tagged dex_bot"))
    if WalletTag.RAT_TRADER in tags:
        out.append(Penalty(name="rat_trader", points=15.0, detail="tagged rat_trader"))
    if WalletTag.BUNDLER in tags:
        out.append(Penalty(name="bundler", points=8.0, detail="tagged bundler"))
    if WalletTag.TRANSFER_IN in tags:
        out.append(Penalty(name="transfer_in", points=6.0, detail="tagged transfer_in"))

    buys, sells = ev.trade_counts
    if buys is not None and sells is not None and sells == 0 and buys > 3:
        out.append(
            Penalty(name="no_sells", points=12.0, detail=f"{buys} buys and no observed sell")
        )

    share = buy_share(ev)
    if share is not None and SELL_ONLY_MAX_BUY_SHARE < share < BUY_STARVED_MAX_BUY_SHARE:
        obs_buys, obs_sells = ev.asymmetry_counts or (0, 0)
        out.append(
            Penalty(
                name="buy_starved",
                points=BUY_STARVED_PENALTY,
                detail=f"{obs_buys} buys against {obs_sells} sells ({share:.2%} buy share)",
            )
        )

    hold = ev.median_hold_s
    tokens = ev.distinct_tokens
    if hold is not None and hold < 120 and tokens is not None and tokens > 50:
        out.append(
            Penalty(
                name="ultra_short_holds",
                points=10.0,
                detail=f"median hold {hold}s across {tokens} tokens",
            )
        )

    lifetime = ev.lifetime_trades
    if lifetime is not None:
        for threshold, points in ((100_000, 30.0), (25_000, 18.0), (10_000, 8.0)):
            if lifetime > threshold:
                out.append(
                    Penalty(
                        name="trade_cadence",
                        points=points,
                        detail=f"{lifetime} lifetime trades (> {threshold})",
                    )
                )
                break

    if ev.tokens_30d is not None and ev.tokens_30d > 1000:
        out.append(
            Penalty(
                name="token_churn_30d",
                points=12.0,
                detail=f"{ev.tokens_30d} distinct tokens in 30 days",
            )
        )
    return out


def _loss_detail(ev: WalletEvidence) -> str | None:
    pnl = ev.pnl
    if pnl is not None and pnl.realized_pnl_usd is not None:
        return f"reconstructed realized ${pnl.realized_pnl_usd:,.0f}" if pnl.realized_pnl_usd < 0 else None
    if pnl is not None and pnl.closed_episodes > 0 and pnl.realized_pnl_native < 0:
        return f"reconstructed realized {pnl.realized_pnl_native} base units"
    ps = ev.provider_stats
    if ps is not None and ps.realized_profit_usd is not None and ps.realized_profit_usd < 0:
        return f"provider-reported ${ps.realized_profit_usd:,.0f}"
    return None


def buy_share(evidence: WalletEvidence) -> Decimal | None:
    """Fraction of observed trades that were buys, or ``None`` on too small a sample."""
    counts = evidence.asymmetry_counts
    if counts is None:
        return None
    buys, sells = counts
    total = buys + sells
    if total < SELL_ONLY_MIN_TRADES:
        return None
    return MATH.divide(_dec(buys), _dec(total))


def sell_only_rejection(evidence: WalletEvidence) -> str | None:
    """Reason to refuse to score a sell-only address, or ``None`` to carry on.

    See the SELL_ONLY_* block at the top of this module for the evidence and the choice of
    thresholds. Returning a string here means the wallet is QUARANTINED before a single
    component is computed.
    """
    share = buy_share(evidence)
    if share is None or share > SELL_ONLY_MAX_BUY_SHARE:
        return None
    buys, sells = evidence.asymmetry_counts or (0, 0)
    return (
        f"sell-only address: {buys} buys against {sells} sells "
        f"({share:.2%} buy share, floor {SELL_ONLY_MAX_BUY_SHARE:.0%}). Inventory it never "
        "bought on-chain arrived off-book, so its profit is settlement flow rather than a "
        "strategy that could be copied."
    )


def creator_discount(evidence: WalletEvidence) -> Decimal | None:
    """Self-dealing multiplier, or ``None`` when the wallet is not a creator.

    A wallet whose created-token count is more than half of the tokens it ever traded is
    trading its own supply; whatever PnL it shows is not an edge we could copy.
    """
    tokens = evidence.distinct_tokens or 0
    if evidence.created_token_count > 0.5 * max(1, tokens):
        return CREATOR_SELF_DEALING_MULTIPLIER
    return None


# --------------------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------------------


def score_wallet(evidence: WalletEvidence) -> WalletScore:
    """Grade one wallet. Never raises on thin data — it says UNSCORED instead."""
    from kaiba.intelligence.naming import infer_archetype

    archetype = infer_archetype(evidence)
    model_id = MODEL_ID_TAPE if evidence.tape is not None else MODEL_ID
    quarantine = sorted(t.value for t in evidence.tags if t in HARD_QUARANTINE_TAGS)
    if quarantine:
        return WalletScore(
            address=evidence.address,
            chain=evidence.chain,
            score=0.0,
            grade=Grade.QUARANTINED,
            evidence_weight=0.0,
            factors=[],
            penalties=[f"quarantine: {', '.join(quarantine)}"],
            archetype=archetype,
            blockers=[f"hard quarantine tag ({', '.join(quarantine)}) — not scored"],
            model_version=model_id,
            receipts=list(evidence.receipts),
        )

    sell_only = sell_only_rejection(evidence)
    if sell_only is not None:
        return WalletScore(
            address=evidence.address,
            chain=evidence.chain,
            score=0.0,
            grade=Grade.QUARANTINED,
            evidence_weight=0.0,
            factors=[],
            penalties=[f"sell_only: {sell_only}"],
            archetype=archetype,
            blockers=[sell_only],
            model_version=model_id,
            receipts=list(evidence.receipts),
        )

    factors, missing = components_for(evidence)
    if evidence.tape is not None:
        factors, missing = apply_tape_credit(evidence, factors, missing)
    evidence_weight = sum(f.max_points for f in factors)
    raw = sum((_dec(f.points) for f in factors), ZERO)
    normalised = MATH.divide(raw * 100, _dec(evidence_weight)) if evidence_weight else ZERO

    penalties = penalties_for(evidence)
    score = normalised - sum((_dec(p.points) for p in penalties), ZERO)
    if score < 0:
        score = ZERO

    rendered = [p.render() for p in penalties]
    discount = creator_discount(evidence)
    if discount is not None:
        score *= discount
        rendered.append(
            f"creator_self_dealing x{discount}: created {evidence.created_token_count} tokens "
            f"vs {evidence.distinct_tokens or 0} traded"
        )
    score = max(ZERO, min(Decimal(100), score))

    blockers = [f"missing component: {name}" for name in missing]
    grade = _assign_grade(evidence, float(score), evidence_weight, blockers)

    pnl = evidence.pnl
    return WalletScore(
        address=evidence.address,
        chain=evidence.chain,
        score=float(round(score, 4)),
        grade=grade,
        evidence_weight=float(evidence_weight),
        factors=factors,
        penalties=rendered,
        archetype=archetype,
        blockers=blockers,
        realized_pnl_usd=pnl.realized_pnl_usd if pnl is not None else _provider_pnl(evidence),
        win_rate=pnl.win_rate if pnl is not None else _provider_win_rate(evidence),
        closed_trades=evidence.closed_episodes,
        distinct_tokens=evidence.distinct_tokens,
        median_hold_s=evidence.median_hold_s,
        model_version=model_id,
        receipts=list(evidence.receipts),
    )


def _provider_pnl(ev: WalletEvidence) -> Decimal | None:
    return ev.provider_stats.realized_profit_usd if ev.provider_stats is not None else None


def _provider_win_rate(ev: WalletEvidence) -> float | None:
    return ev.provider_stats.win_rate if ev.provider_stats is not None else None


def _assign_grade(
    ev: WalletEvidence, score: float, evidence_weight: float, blockers: list[str]
) -> Grade:
    if evidence_weight < MIN_EVIDENCE_WEIGHT:
        blockers.append(
            f"evidence_weight {evidence_weight:.1f} < {MIN_EVIDENCE_WEIGHT:.0f} — not enough "
            "measured components to form an opinion"
        )
        return _tape_cap(ev, Grade.UNSCORED, blockers)
    if score >= A_MIN_SCORE:
        gate_failures = _a_gate_failures(ev, evidence_weight)
        if not gate_failures:
            return _tape_cap(ev, Grade.A, blockers)
        blockers.extend(gate_failures)
        return _tape_cap(ev, Grade.B, blockers)
    if score >= B_MIN_SCORE:
        return _tape_cap(ev, Grade.B, blockers)
    if score >= C_MIN_SCORE:
        return _tape_cap(ev, Grade.C, blockers)
    return _tape_cap(ev, Grade.D, blockers)


def _a_gate_failures(ev: WalletEvidence, evidence_weight: float) -> list[str]:
    """Data gates that stand between a high score and an A. Each one is a capped-to-B reason."""
    out: list[str] = []
    if evidence_weight < A_MIN_EVIDENCE_WEIGHT:
        out.append(
            f"capped to B: evidence_weight {evidence_weight:.1f} < {A_MIN_EVIDENCE_WEIGHT:.0f}"
        )
    if ev.tape is not None:
        # The one gate no amount of good tape can pass. Our tape is the trades on tokens
        # we watched; an A claims we know the wallet, and we do not.
        out.append(f"capped to B: partial tape — {ev.tape.describe()}")
    closed = ev.closed_episodes
    if closed is None:
        out.append("capped to B: closed_episodes unknown (no reconstruction)")
    elif closed < A_MIN_CLOSED_EPISODES:
        out.append(f"capped to B: {closed} closed episodes < {A_MIN_CLOSED_EPISODES}")
    tokens, basis = ev.sample_tokens
    if tokens is None:
        out.append("capped to B: distinct_tokens unknown")
    elif tokens < A_MIN_DISTINCT_TOKENS:
        out.append(f"capped to B: {tokens} distinct tokens < {A_MIN_DISTINCT_TOKENS} (basis={basis})")
    if ev.sample_capped:
        out.append("capped to B: sample was truncated by a provider page limit")
    return out


# --------------------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------------------


def _write_score_row(
    score: WalletScore, c: sqlite3.Connection, *, protect_full: bool = False
) -> bool:
    """The two writes one grade makes. Caller owns the transaction.

    ``protect_full`` refuses to replace a paid full-history grade (``MODEL_ID``), decided
    by the row as it is NOW. The tape pass used to decide from a snapshot taken when the
    pass began; a sol pass runs for hours, so every wallet ``wallet_buyers`` graded in the
    meantime was overwritten (MEASURED 2026-09-29: 6 of 6 paid grades on 09-28, one B
    turned D) and then paid for again a week later. Returns whether anything was written.
    """
    row: dict[str, Any] = {
        "chain": score.chain.value,
        "address": score.address,
        "score": float(score.score),
        "grade": score.grade.value,
        "evidence_weight": float(score.evidence_weight),
        "archetype": score.archetype.value,
        "realized_pnl_usd": str(score.realized_pnl_usd) if score.realized_pnl_usd is not None else None,
        "win_rate": score.win_rate,
        "closed_trades": score.closed_trades,
        "distinct_tokens": score.distinct_tokens,
        "median_hold_s": score.median_hold_s,
        "factors_json": jdump([f.model_dump() for f in score.factors]),
        "penalties_json": jdump(score.penalties),
        "blockers_json": jdump(score.blockers),
        "receipts_json": jdump([r.model_dump() for r in score.receipts]),
        "model_version": score.model_version,
        "scored_at_ms": score.scored_at_ms,
    }
    if protect_full:
        cols = list(row)
        updates = ",".join(f"{k}=excluded.{k}" for k in cols if k not in ("chain", "address"))
        c.execute(
            f"INSERT INTO wallet_scores ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)}) "
            f"ON CONFLICT(chain, address) DO UPDATE SET {updates} "
            "WHERE wallet_scores.model_version IS NOT ?",
            [row[k] for k in cols] + [MODEL_ID],
        )
        if c.execute("SELECT changes()").fetchone()[0] == 0:
            return False
    else:
        upsert(c, "wallet_scores", row, ["chain", "address"])
    c.execute(
        "INSERT INTO wallet_score_history (chain, address, score, grade, scored_at_ms, model_version) "
        "VALUES (?,?,?,?,?,?)",
        (
            score.chain.value,
            score.address,
            float(score.score),
            score.grade.value,
            score.scored_at_ms,
            score.model_version,
        ),
    )
    return True


def store_score(score: WalletScore, conn: sqlite3.Connection | None = None) -> None:
    """Upsert the latest grade, append to history, and announce it on the bus.

    One grade, one transaction, one event. For a whole tape use :func:`store_scores`,
    which shares a transaction across many and does not announce each one.
    """
    c = conn or get_conn()
    with tx(c):
        _write_score_row(score, c)
        emit(
            EventKind.WALLET_GRADED,
            {
                "grade": score.grade.value,
                "score": score.score,
                "archetype": score.archetype.value,
            },
            chain=score.chain,
            subject=score.address,
            conn=c,
        )


#: How many wallet scores share one write transaction in :func:`store_scores`.
#:
#: MEASURED on the live box 2026-09-24. ``grade_tape`` stored through ``store_score``,
#: which opens its own transaction per wallet. Grading the robinhood tape attempts
#: 32,406 of them, each competing for the single WAL writer against the engine, the
#: scanner, ingest and the protection watchdog, all of which write continuously. The
#: measured result was ``stored: 1`` -- every other write lost the lock and raised
#: ``database is locked``, which ``store_score``'s caller logged as a warning and
#: swallowed. The job reported success having written one row.
#:
#: 500 to a transaction turns 32,406 lock acquisitions into 65. The batch is bounded
#: rather than one giant transaction because a writer that holds the lock for the
#: whole tape starves the services that keep live positions protected.
TAPE_STORE_BATCH = 500


def store_scores(
    scores: Sequence[WalletScore],
    conn: sqlite3.Connection | None = None,
    *,
    batch_size: int = TAPE_STORE_BATCH,
    announce: bool = False,
    protect_full: bool = False,
) -> tuple[int, int]:
    """Store many grades in few transactions. Returns ``(stored, failed)``.

    ``announce`` is off by default and that is deliberate: ``store_score`` emits one
    WALLET_GRADED event per wallet, which is right for grading a wallet on purpose and
    wrong for a whole-tape pass -- 32,000 events would bury every other event on the bus.

    A failed batch is COUNTED and returned, never swallowed. A caller that cannot say
    how many rows it failed to write is how ``stored: 1`` looked like success.
    """
    c = conn or get_conn()
    stored = failed = 0
    size = max(1, int(batch_size))
    for offset in range(0, len(scores), size):
        chunk = list(scores[offset : offset + size])
        try:
            written = 0
            with tx(c):
                for score in chunk:
                    wrote = (_write_score_row(score, c, protect_full=True) if protect_full
                             else _write_score_row(score, c))
                    if wrote is False:
                        continue  # a paid grade landed since the pass began; it stands
                    written += 1
                    if announce:
                        emit(
                            EventKind.WALLET_GRADED,
                            {
                                "grade": score.grade.value,
                                "score": score.score,
                                "archetype": score.archetype.value,
                            },
                            chain=score.chain,
                            subject=score.address,
                            conn=c,
                        )
            stored += written
        except sqlite3.Error as exc:
            # The whole batch rolled back, so none of it landed. Say so.
            failed += len(chunk)
            log.warning("wallet score batch of %d failed: %s", len(chunk), exc)
    return stored, failed

def load_score(chain: Chain, address: str, conn: sqlite3.Connection | None = None) -> WalletScore | None:
    """Read the latest stored grade, or ``None`` if this wallet was never graded."""
    c = conn or get_conn()
    try:
        addr = normalize_address(address, chain)
    except ValueError:
        addr = address.strip()
    row = fetch_one(
        c,
        "SELECT * FROM wallet_scores WHERE chain = ? AND address = ?",
        (chain.value, addr),
    )
    if row is None:
        return None
    return WalletScore(
        address=row["address"],
        chain=Chain(row["chain"]),
        score=row["score"],
        grade=Grade(row["grade"]),
        evidence_weight=row["evidence_weight"],
        factors=[ScoreFactor(**f) for f in jload(row["factors_json"], [])],
        penalties=list(jload(row["penalties_json"], [])),
        archetype=Archetype(row["archetype"]),
        blockers=list(jload(row["blockers_json"], [])),
        realized_pnl_usd=Decimal(row["realized_pnl_usd"]) if row["realized_pnl_usd"] else None,
        win_rate=row["win_rate"],
        closed_trades=row["closed_trades"],
        distinct_tokens=row["distinct_tokens"],
        median_hold_s=row["median_hold_s"],
        scored_at_ms=row["scored_at_ms"],
        model_version=row["model_version"],
        receipts=[Receipt(**r) for r in jload(row["receipts_json"], [])],
    )


def score_history(
    chain: Chain, address: str, limit: int = 50, conn: sqlite3.Connection | None = None
) -> list[dict[str, Any]]:
    """Grade history newest first — the input to "this wallet is decaying" checks."""
    c = conn or get_conn()
    try:
        addr = normalize_address(address, chain)
    except ValueError:
        addr = address.strip()
    return fetch_all(
        c,
        "SELECT * FROM wallet_score_history WHERE chain = ? AND address = ? "
        "ORDER BY scored_at_ms DESC, id DESC LIMIT ?",
        (chain.value, addr, limit),
    )


# --------------------------------------------------------------------------------------
# assembling evidence out of what we already observed
# --------------------------------------------------------------------------------------

_PROVIDER_FIELDS = (
    "realized_profit_usd",
    "win_rate",
    "token_num",
    "avg_hold_s",
    "buy_count",
    "sell_count",
)


def build_evidence(
    address: str, chain: Chain, conn: sqlite3.Connection | None = None
) -> WalletEvidence:
    """Gather everything the local database knows about one wallet.

    Deliberately conservative: a block stays ``None`` when the corresponding table has no
    rows for this wallet, so the grader sees "not measured" rather than "measured as zero".
    ``seed_confluence`` is always ``None`` here — the seed pass is a separate job, and
    claiming zero confluence for a wallet nobody checked is exactly the bug that made the
    old rubric grade everyone a C.
    """
    c = conn or get_conn()
    try:
        addr = normalize_address(address, chain)
    except ValueError:
        addr = address.strip()

    wallet = fetch_one(c, "SELECT * FROM wallets WHERE chain = ? AND address = ?", (chain.value, addr))
    meta: dict[str, Any] = jload(wallet["meta_json"], {}) if wallet else {}
    tags = _tags_from(jload(wallet["tags_json"], []) if wallet else [])

    swaps = fetch_all(
        c,
        "SELECT ts_ms, token, side, amount_token, amount_native, usd_value, chain FROM swaps "
        "WHERE chain = ? AND wallet = ? ORDER BY ts_ms, id",
        (chain.value, addr),
    )
    from kaiba.intelligence.pnl import reconstruct, summarize

    episodes = reconstruct(swaps) if swaps else []
    wallet_pnl = summarize(episodes) if swaps else None
    closed_tokens = closed_token_count(episodes) if swaps else None
    closed_buys, closed_sells = closed_side_counts(episodes) if swaps else (None, None)
    # Raw counts for the sell-only filter, taken before reconstruction discards the
    # contaminated episodes that a settlement address is made of.
    sides = fetch_one(
        c,
        # INDEXED BY: with no ANALYZE statistics the planner picked the (chain) prefix of
        # the swaps autoindex and walked ~5.5M sol entries for every wallet -- about two
        # minutes a wallet under load (MEASURED 2026-09-29), which is why regrade and the
        # grading half of wallet_buyers timed out every run. This index is (chain, wallet).
        "SELECT SUM(side = 'buy') AS buys, SUM(side = 'sell') AS sells "
        "FROM swaps INDEXED BY idx_swaps_wallet WHERE chain = ? AND wallet = ?",
        (chain.value, addr),
    )
    observed_buys = int(sides["buys"]) if sides and sides["buys"] is not None else None
    observed_sells = int(sides["sells"]) if sides and sides["sells"] is not None else None

    provider = {k: meta[k] for k in _PROVIDER_FIELDS if meta.get(k) is not None}
    twitter = (wallet or {}).get("twitter") or meta.get("twitter")
    rep_fields = {"followers": meta.get("followers"), "verified": meta.get("verified"), "kol": meta.get("kol")}
    reputation = (
        Reputation(twitter=twitter, **{k: v for k, v in rep_fields.items() if v is not None})
        if twitter or any(v is not None for v in rep_fields.values())
        else None
    )

    created = meta.get("created_token_count")
    if created is None:
        row = fetch_one(
            c, "SELECT COUNT(*) AS n FROM tokens WHERE chain = ? AND creator = ?", (chain.value, addr)
        )
        created = int(row["n"]) if row else 0

    return WalletEvidence(
        address=addr,
        chain=chain,
        tags=tags,
        pnl=wallet_pnl,
        provider_stats=ProviderStats.model_validate(provider) if provider else None,
        early_metrics=_early_metrics(c, chain, addr),
        seed_confluence=meta.get("seed_confluence"),
        reputation=reputation,
        created_token_count=int(created or 0),
        sample_capped=bool(meta.get("sample_capped", False)),
        observed_buys=observed_buys,
        observed_sells=observed_sells,
        closed_distinct_tokens=closed_tokens,
        closed_buys=closed_buys,
        closed_sells=closed_sells,
        trade_count_lifetime=meta.get("trade_count_lifetime"),
        tokens_30d=meta.get("tokens_30d"),
        lead_lag_of=meta.get("lead_lag_of"),
        fomo_flag=bool(meta.get("fomo_flag", False)),
    )


def _tags_from(raw: Iterable[Any]) -> list[WalletTag]:
    """``wallets.tags_json`` -> tags. A bare value was written by a screened path or the
    operator and passes; a ``gmgn:``-namespaced value is a vendor label and goes through
    :func:`admit_vendor_label`; anything else is noise, not evidence."""
    out: list[WalletTag] = []
    for item in raw:
        text = str(item).strip()
        if text.startswith(VENDOR_TAG_PREFIX):
            tag = admit_vendor_label(text[len(VENDOR_TAG_PREFIX):])
        else:
            try:
                tag = WalletTag(text)
            except ValueError:
                tag = None
        if tag is not None and tag not in out:
            out.append(tag)
    return out


def _early_metrics(conn: sqlite3.Connection, chain: Chain, address: str) -> EarlyMetrics | None:
    """Universe-relative early-buyer counts from the ``first_buyers`` table."""
    rows = fetch_all(
        conn,
        "SELECT fb.rank, fb.seconds_after_open, "
        "(SELECT COUNT(*) FROM first_buyers b2 WHERE b2.chain = fb.chain AND b2.token = fb.token) "
        "AS buyers FROM first_buyers fb WHERE fb.chain = ? AND fb.wallet = ?",
        (chain.value, address),
    )
    return _early_metrics_from_rows(rows)


def _early_metrics_from_rows(rows: Sequence[Mapping[str, Any]]) -> EarlyMetrics | None:
    """Shared by the per-wallet query above and the batch index in the tape path."""
    if not rows:
        return None
    secs = [r["seconds_after_open"] for r in rows]
    return EarlyMetrics(
        validated_early_tokens=sum(1 for r in rows if r["rank"] <= 3 and (r["buyers"] or 0) >= 10),
        sniper_tokens=sum(1 for r, s in zip(rows, secs, strict=True) if s is not None and s <= 300),
        insider_tokens=sum(
            1 for r, s in zip(rows, secs, strict=True) if r["rank"] <= 2 and s is not None and s <= 60
        ),
        best_entry_rank=min(r["rank"] for r in rows),
    )


def grade_address(
    address: str,
    chain: Chain | str = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    store: bool = True,
) -> WalletScore:
    """Assemble evidence from the local database, grade it, and store the result."""
    ch = chain if isinstance(chain, Chain) else Chain(str(chain))
    c = conn or ensure_db()
    score = score_wallet(build_evidence(address, ch, c))
    if store:
        try:
            store_score(score, c)
        except sqlite3.Error as exc:
            log.warning("store_score failed for %s: %s", score.address, exc)
    return score


def grade_wallets(
    evidence: Iterable[WalletEvidence], conn: sqlite3.Connection | None = None, *, store: bool = True
) -> list[WalletScore]:
    """Score a batch. Storage failures are logged, never raised into a gathering loop."""
    out: list[WalletScore] = []
    for ev in evidence:
        score = score_wallet(ev)
        out.append(score)
        if store:
            try:
                store_score(score, conn)
            except sqlite3.Error as exc:
                log.warning("store_score failed for %s: %s", score.address, exc)
    return out


# --------------------------------------------------------------------------------------
# grading from our own tape, at scale, without a provider
# --------------------------------------------------------------------------------------
#
# The path above assembles evidence for wallets whose history a provider backfilled: 15
# wallets a day at ~110 Helius credits each. Meanwhile `swaps` already holds every trade
# we observed on the tokens we watched — 61k Solana wallets and ~1,800 GMGN-tagged
# wallets on three chains on the live box (MEASURED 2026-09-21). This section grades from
# that tape. The reconstruction is the same (`kaiba.intelligence.pnl`); what differs is
# that the evidence carries a `TapeCoverage` block, and that block caps the grade:
#
#   * tape can never produce an A (`_a_gate_failures`);
#   * a B needs TAPE_MIN_CLOSED_FOR_B closed round trips over TAPE_MIN_TOKENS_FOR_B tokens
#     that each CLOSED a round trip (`_tape_cap`), so three winning trades cannot buy a
#     follow and twenty buys with no exit cannot widen a one-token wallet into a B;
#   * the PnL-derived components earn `tape_credit(n)` of their weight, so `evidence_weight`
#     shrinks with the sample and a thin tape lands on UNSCORED, the same way a missing
#     component would (`apply_tape_credit`).
#
# The asymmetry is deliberate: a D from a thin tape is cheap if wrong (the wallet is not
# followed), a B from a thin tape costs money. So B is gated, D is not.

#: Below this many closed round trips the tape says nothing about skill.
TAPE_MIN_CLOSED_EPISODES = 3
#: At this many closed round trips the PnL components earn their full weight.
TAPE_FULL_CREDIT_CLOSED_EPISODES = 12
#: A tape-only B needs at least this many closed episodes ...
TAPE_MIN_CLOSED_FOR_B = 8
#: ... spread over at least this many distinct tokens.
TAPE_MIN_TOKENS_FOR_B = 4
#: Provider-reported USD is held as integer micro-dollars so the money path stays integer.
TAPE_USD_MICRO = 10**6
#: Human-unit token amounts are scaled by this many decimals before reconstruction.
TAPE_TOKEN_SCALE_DECIMALS = 9
#: A wallet is flagged as worth a Helius backfill from this many closed episodes ...
TAPE_CANDIDATE_MIN_CLOSED = 2
#: ... when at least this share of them won.
TAPE_CANDIDATE_MIN_WIN_RATE = Decimal("0.5")
#: How many candidates a batch report keeps, best first.
TAPE_CANDIDATE_LIMIT = 200
#: Wallets with fewer raw rows than this are tallied, not scored: one row is not a trade.
TAPE_MIN_ROWS = 2

#: Every numeric knob above says where it came from. `tests/test_tape_grading.py` fails the
#: build on an unlabelled one, the way `bundles.py` and `clustering.py` do.
TAPE_THRESHOLD_PROVENANCE: dict[str, str] = {
    "TAPE_MIN_CLOSED_EPISODES": (
        "INVENTED. Two round trips is a coin flip; three is the smallest sample the existing "
        "rubric is willing to call a rumour. Not measured against outcomes."
    ),
    "TAPE_FULL_CREDIT_CLOSED_EPISODES": (
        "INVENTED. Chosen so that a tape-only wallet needs ~6 closed episodes before the "
        "PnL components clear MIN_EVIDENCE_WEIGHT (66 * 6/12 = 33 >= 30). Not measured."
    ),
    "TAPE_MIN_CLOSED_FOR_B": (
        "INVENTED. Sits just under A_MIN_CLOSED_EPISODES (10) because a tape B is "
        "provisional by construction and the full-history A gate stays stricter."
    ),
    "TAPE_MIN_TOKENS_FOR_B": (
        "INVENTED. One under A_MIN_DISTINCT_TOKENS (5) for the same reason. Counted over "
        "tokens with a CLOSED round trip only, so a buy whose exit we never saw is not "
        "breadth: MEASURED 2026-09-22 on the live tape, the open-inclusive count showed the "
        "one sol B 39 tokens where 9 had closed."
    ),
    "TAPE_USD_MICRO": "DEFINITIONAL. Micro-dollar is the integer unit chosen for provider USD.",
    "TAPE_TOKEN_SCALE_DECIMALS": (
        "STRUCTURAL. Episode reconstruction only compares token quantities within one "
        "(wallet, token), so any consistent scale is exact; 9 keeps 6-decimal pump.fun "
        "amounts and 18-decimal EVM amounts inside the same integer range."
    ),
    "TAPE_CANDIDATE_MIN_CLOSED": (
        "INVENTED. Two profitable closed episodes on our tape is enough to be worth 110 "
        "Helius credits to look at, not enough to be graded."
    ),
    "TAPE_CANDIDATE_MIN_WIN_RATE": (
        "INVENTED. Half the closed episodes winning; a coarse cut, not a measured edge."
    ),
    "TAPE_CANDIDATE_LIMIT": "OPERATIONAL. Report size; the ranking is what matters.",
    "TAPE_STORE_BATCH": (
        "OPERATIONAL. Rows per write transaction, not a threshold on any measurement. "
        "MEASURED 2026-09-24: one transaction per wallet asked for 32,406 write locks "
        "against the live writers and stored 1; 500 to a transaction makes it 65. "
        "Bounded rather than one transaction for the whole tape, because a writer "
        "holding the lock that long starves the services keeping live stops on time."
    ),
    "TAPE_MIN_ROWS": "DEFINITIONAL. A round trip needs a buy and a sell.",
}

#: Components whose inputs come from the reconstructed tape and therefore shrink with n.
TAPE_SCALED_COMPONENTS: frozenset[str] = frozenset(
    {"realized_profit", "roi", "win_rate", "big_win_rate", "breadth"}
)

#: `swaps.source` prefixes whose amounts are human units (decimal strings), not base units.
#: MEASURED 2026-09-21: gmgn:* rows hold `'0.147380625'`, every other source holds wei/lamports.
HUMAN_UNIT_SOURCE_PREFIXES: tuple[str, ...] = ("gmgn:",)

_TAPE_COLUMNS = "ts_ms, token, side, amount_token, amount_native, usd_value, source, tx, chain"


def _is_human_unit_source(source: Any) -> bool:
    return str(source or "").startswith(HUMAN_UNIT_SOURCE_PREFIXES)


def _dec_or_none(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _scaled_int(value: Any, decimals: int) -> int | None:
    """``text * 10**decimals`` as an int, truncated toward zero below one base unit."""
    d = _dec_or_none(value)
    if d is None:
        return None
    return int(MATH.multiply(d, Decimal(10) ** decimals))


def tape_credit(closed_episodes: int | None) -> Decimal:
    """Share of a PnL component's weight that ``n`` closed round trips have earned.

    0 below TAPE_MIN_CLOSED_EPISODES, 1 at TAPE_FULL_CREDIT_CLOSED_EPISODES, linear between.
    """
    if closed_episodes is None or closed_episodes < TAPE_MIN_CLOSED_EPISODES:
        return ZERO
    frac = MATH.divide(Decimal(closed_episodes), Decimal(TAPE_FULL_CREDIT_CLOSED_EPISODES))
    return min(Decimal(1), frac)


def apply_tape_credit(
    ev: WalletEvidence, factors: list[ScoreFactor], missing: list[str]
) -> tuple[list[ScoreFactor], list[str]]:
    """Shrink the tape-derived components to the weight their sample has earned.

    Points and ``max_points`` scale together, so the normalised score is unchanged and only
    ``evidence_weight`` moves — the existing rubric's language for "we know less". A credit
    of zero removes the component and records it as missing.
    """
    credit = tape_credit(ev.closed_episodes)
    closed = ev.closed_episodes if ev.closed_episodes is not None else 0
    kept: list[ScoreFactor] = []
    gone = list(missing)
    for f in factors:
        if f.name not in TAPE_SCALED_COMPONENTS:
            kept.append(f)
            continue
        if credit <= 0:
            gone.append(
                f"{f.name} (tape too thin: {closed} closed episodes < {TAPE_MIN_CLOSED_EPISODES})"
            )
            continue
        detail = (
            f"{f.detail or ''} tape_credit={credit:.2f} ({closed} closed episodes, "
            f"{TAPE_FULL_CREDIT_CLOSED_EPISODES} for full credit)"
        ).strip()
        kept.append(
            ScoreFactor(
                name=f.name,
                points=float(_dec(f.points) * credit),
                max_points=float(_dec(f.max_points) * credit),
                detail=detail,
            )
        )
    return kept, gone


def _tape_cap(ev: WalletEvidence, grade: Grade, blockers: list[str]) -> Grade:
    """Last word on a tape-derived grade: note the coverage, and gate B on the sample.

    Both halves of the gate count CLOSED episodes only. The token half refuses any basis
    other than ``closed_episodes`` — a token count that includes open positions is not a
    measurement of breadth, it is a list of buys we have not seen the end of.
    """
    tape = ev.tape
    if tape is None:
        return grade
    blockers.append(f"provisional: graded from partial tape — {tape.describe()}")
    if grade is not Grade.B:
        return grade
    closed = ev.closed_episodes or 0
    tokens, basis = ev.sample_tokens
    reasons: list[str] = []
    if closed < TAPE_MIN_CLOSED_FOR_B:
        reasons.append(f"{closed} closed episodes < {TAPE_MIN_CLOSED_FOR_B}")
    if basis != "closed_episodes" or tokens is None:
        reasons.append(f"tokens with a closed round trip unknown (token count basis={basis})")
    elif tokens < TAPE_MIN_TOKENS_FOR_B:
        reasons.append(f"{tokens} tokens with a closed round trip < {TAPE_MIN_TOKENS_FOR_B}")
    if not reasons:
        return grade
    blockers.append("capped to C: tape too thin for a B (" + "; ".join(reasons) + ")")
    return Grade.C


def closed_token_count(episodes: Iterable[Episode]) -> int:
    """Distinct tokens with at least one closed, clean episode: the breadth a grade may count.

    ``pnl.summarize`` counts every clean token, open positions included; that module is
    not this one's to change, so the closed-only count is taken here from the same
    episode list, using the same ``scorable`` rule the aggregate uses for its closed set.
    """
    return len({e.token for e in episodes if e.scorable})


def closed_side_counts(episodes: Iterable[Episode]) -> tuple[int, int]:
    """``(buys, sells)`` inside closed, clean episodes — the breadth band's only input."""
    closed = [e for e in episodes if e.scorable]
    return sum(e.buys for e in closed), sum(e.sells for e in closed)


@dataclass
class _NormalisedTape:
    rows: list[dict[str, Any]]
    coverage: TapeCoverage
    observed_buys: int
    observed_sells: int


def normalise_tape_rows(raw_rows: Sequence[Mapping[str, Any]], chain: Chain) -> _NormalisedTape:
    """Put one wallet's ``swaps`` rows on a single money axis and drop what cannot be used.

    * On-chain sources (pumpfun, robinhood, helius, stonkfun, token_flow) already hold base
      units as text; they pass through and the axis is ``native``.
    * GMGN feed rows hold human-unit decimals and a quote amount in an unknown token. A
      GMGN row that repeats an on-chain row (same tx/token/side) is a duplicate and is
      dropped; a GMGN row on a token we also hold in base units cannot be put on the same
      inventory scale (token decimals are unknown) and is dropped; the rest force the whole
      wallet onto the ``usd_micro`` axis, where every row's money is ``usd_value`` in
      integer micro-dollars and a row without ``usd_value`` loses its money (and so
      contaminates its episode, as ``pnl.reconstruct`` requires).

    Buy/sell counts for the sell-only filter are taken over the raw rows, before anything
    is dropped, because the drop rules are about units, not about the trader's asymmetry.
    """
    raw = list(raw_rows)
    sources: Counter[str] = Counter(str(r.get("source") or "unknown") for r in raw)
    sides: Counter[str] = Counter(str(r.get("side") or "").strip().lower() for r in raw)

    base = [r for r in raw if not _is_human_unit_source(r.get("source"))]
    human = [r for r in raw if _is_human_unit_source(r.get("source"))]
    base_keys = {(r.get("tx"), r.get("token"), str(r.get("side") or "").lower()) for r in base}
    base_tokens = {r.get("token") for r in base}

    kept_human: list[dict[str, Any]] = []
    dup = mixed = 0
    for r in human:
        key = (r.get("tx"), r.get("token"), str(r.get("side") or "").lower())
        if key in base_keys:
            dup += 1
        elif r.get("token") in base_tokens:
            mixed += 1
        else:
            kept_human.append(dict(r))

    without_money = 0
    out: list[dict[str, Any]] = []
    if not kept_human:
        axis: Literal["native", "usd_micro"] = "native"
        basis = EvidenceBasis.VERIFIED_ONCHAIN if base else EvidenceBasis.UNAVAILABLE
        out = [dict(r) for r in base]
    else:
        axis = "usd_micro"
        basis = EvidenceBasis.PROVIDER_REPORTED
        for r in [dict(r) for r in base] + kept_human:
            money = _scaled_int(r.get("usd_value"), 6)  # TAPE_USD_MICRO == 10**6
            if money is None:
                without_money += 1
            r["amount_native"] = None if money is None else str(money)
            if _is_human_unit_source(r.get("source")):
                qty = _scaled_int(r.get("amount_token"), TAPE_TOKEN_SCALE_DECIMALS)
                r["amount_token"] = None if qty is None else str(qty)
            out.append(r)
    out.sort(key=lambda r: (int(r.get("ts_ms") or 0), r.get("tx") or "", r.get("token") or ""))

    feeds = sorted({s.split(":", 1)[1] for s in sources if _is_human_unit_source(s) and ":" in s})
    ts = [int(r["ts_ms"]) for r in out if r.get("ts_ms") is not None]
    coverage = TapeCoverage(
        rows=len(out),
        rows_raw=len(raw),
        rows_dropped_duplicate=dup,
        rows_dropped_mixed_units=mixed,
        rows_without_money=without_money,
        sources=dict(sources),
        feeds=feeds,
        distinct_tokens=len({r.get("token") for r in out}),
        first_ms=min(ts) if ts else None,
        last_ms=max(ts) if ts else None,
        money_axis=axis,
        money_basis=basis,
    )
    return _NormalisedTape(
        rows=out, coverage=coverage, observed_buys=sides.get("buy", 0), observed_sells=sides.get("sell", 0)
    )


def provider_tags_from_events(
    conn: sqlite3.Connection, chain: Chain, address: str
) -> list[str]:
    """Raw GMGN cohort labels for one wallet, read back off the ``wallet.trade`` events.

    ``gmgn_feeds.write_swap`` does not store tags in ``swaps``; it emits them on the event
    bus. This is the only place on disk they exist, so if the events table is ever pruned
    the labels go with it — the returned list is then empty, which the caller records as
    UNAVAILABLE rather than "no tags".
    """
    rows = fetch_all(
        conn,
        "SELECT payload FROM events WHERE kind = ? AND chain = ? AND subject = ?",
        (EventKind.WALLET_TRADE.value, chain.value, address),
    )
    return _collect_tags(jload(r["payload"], {}) for r in rows)


def _collect_tags(payloads: Iterable[Any]) -> list[str]:
    seen: dict[str, None] = {}
    for p in payloads:
        if not isinstance(p, dict):
            continue
        for tag in p.get("tags") or []:
            t = str(tag).strip()
            if t:
                seen.setdefault(t, None)
    return list(seen)


def provider_tag_index(conn: sqlite3.Connection, chain: Chain) -> dict[str, list[str]]:
    """``address -> raw tags`` for every wallet with a tagged ``wallet.trade`` event."""
    grouped: dict[str, list[Any]] = defaultdict(list)
    for r in conn.execute(
        "SELECT subject, payload FROM events WHERE kind = ? AND chain = ?",
        (EventKind.WALLET_TRADE.value, chain.value),
    ):
        p = jload(r[1], {})
        if isinstance(p, dict) and p.get("tags"):
            grouped[str(r[0])].append(p)
    return {addr: _collect_tags(ps) for addr, ps in grouped.items()}


def admit_vendor_label(label: str) -> WalletTag | None:
    """The :class:`WalletTag` a vendor label may become, or ``None``.

    Only a label in :data:`VENDOR_ADMITTED_TAGS` — one that quarantines or penalises — is
    admitted. ``smart_degen``, ``kol``, ``top_followed`` and every other positive label
    return ``None`` here whatever they are spelled like; see the vendor-labels block above.
    """
    try:
        tag = WalletTag(str(label).strip())
    except ValueError:
        return None
    return tag if tag in VENDOR_ADMITTED_TAGS else None


def map_provider_tags(raw: Iterable[str]) -> list[WalletTag]:
    """GMGN labels off the event bus -> the tags the grader admits (disqualifying ones only)."""
    out: list[WalletTag] = []
    for item in raw:
        tag = admit_vendor_label(str(item))
        if tag is not None and tag not in out:
            out.append(tag)
    return out


def tape_evidence_from_rows(
    address: str,
    chain: Chain,
    raw_rows: Sequence[Mapping[str, Any]],
    *,
    wallet_tags: Iterable[Any] = (),
    provider_tags: Sequence[str] = (),
    early_metrics: EarlyMetrics | None = None,
    seed_confluence: int | None = None,
    created_token_count: int = 0,
    as_of_ms: int | None = None,
) -> WalletEvidence:
    from kaiba.intelligence.pnl import reconstruct, summarize

    tape = normalise_tape_rows(raw_rows, chain)
    coverage = tape.coverage
    if provider_tags:
        coverage.provider_tags = list(provider_tags)
        coverage.provider_tag_basis = EvidenceBasis.PROVIDER_REPORTED

    tags = _tags_from(wallet_tags)
    for tag in map_provider_tags(provider_tags):
        if tag not in tags:
            tags.append(tag)

    episodes = reconstruct(tape.rows, as_of_ms=as_of_ms) if tape.rows else []
    wallet_pnl = summarize(episodes) if tape.rows else None
    closed_tokens = closed_token_count(episodes) if tape.rows else None
    closed_buys, closed_sells = closed_side_counts(episodes) if tape.rows else (None, None)

    receipts = [
        Receipt(
            provider="kaiba",
            endpoint="swaps",
            basis=coverage.money_basis if tape.rows else EvidenceBasis.UNAVAILABLE,
            note=f"tape evidence: {coverage.describe()}",
        )
    ]
    if provider_tags:
        receipts.append(
            Receipt(
                provider="gmgn",
                endpoint="events:wallet.trade",
                basis=EvidenceBasis.PROVIDER_REPORTED,
                note=(
                    "cohort labels " + ",".join(provider_tags) + " — recorded, not a quality "
                    "signal; only a disqualifying label becomes a tag"
                ),
            )
        )

    return WalletEvidence(
        address=address,
        chain=chain,
        tags=tags,
        pnl=wallet_pnl,
        early_metrics=early_metrics,
        seed_confluence=seed_confluence,
        created_token_count=int(created_token_count or 0),
        observed_buys=tape.observed_buys,
        observed_sells=tape.observed_sells,
        closed_distinct_tokens=closed_tokens,
        closed_buys=closed_buys,
        closed_sells=closed_sells,
        tape=coverage,
        receipts=receipts,
    )


def build_tape_evidence(
    address: str,
    chain: Chain,
    conn: sqlite3.Connection | None = None,
    *,
    as_of_ms: int | None = None,
) -> WalletEvidence:
    """Evidence for one wallet from ``swaps`` alone — no provider call, no credits.

    Reads: the wallet's swap rows, its GMGN cohort labels off the event bus, its
    ``first_buyers`` rows, and how many tokens it created. Deliberately does *not* read
    provider stats out of ``wallets.meta_json``: that is the backfill path's evidence and
    mixing it in would hide which path a grade came from.
    """
    c = conn or get_conn()
    try:
        addr = normalize_address(address, chain)
    except ValueError:
        addr = address.strip()
    rows = fetch_all(
        c,
        f"SELECT {_TAPE_COLUMNS} FROM swaps WHERE chain = ? AND wallet = ? ORDER BY ts_ms, id",
        (chain.value, addr),
    )
    wallet = fetch_one(c, "SELECT tags_json FROM wallets WHERE chain = ? AND address = ?", (chain.value, addr))
    created = fetch_one(c, "SELECT COUNT(*) AS n FROM tokens WHERE chain = ? AND creator = ?", (chain.value, addr))
    return tape_evidence_from_rows(
        addr,
        chain,
        rows,
        wallet_tags=jload(wallet["tags_json"], []) if wallet else [],
        provider_tags=provider_tags_from_events(c, chain, addr),
        early_metrics=_early_metrics(c, chain, addr),
        created_token_count=int(created["n"]) if created else 0,
        as_of_ms=as_of_ms,
    )


def grade_from_tape(
    address: str,
    chain: Chain | str = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    store: bool = False,
) -> WalletScore:
    """Grade one wallet from the tape. Storage is opt-in: a tape grade is provisional."""
    ch = chain if isinstance(chain, Chain) else Chain(str(chain))
    c = conn or ensure_db()
    score = score_wallet(build_tape_evidence(address, ch, c))
    if store:
        try:
            store_score(score, c)
        except sqlite3.Error as exc:
            log.warning("store_score failed for %s: %s", score.address, exc)
    return score


def tape_candidate_reason(ev: WalletEvidence, score: WalletScore) -> str | None:
    """Why this wallet is worth Helius credits, or ``None``.

    A candidate is a wallet whose tape looks good and whose grade is held down by the
    tape's thinness rather than by its behaviour. The full history is what would settle it,
    and that is exactly what the credits buy. Quarantined wallets and wallets that already
    hold a full-history grade are never candidates.
    """
    if score.grade is Grade.QUARANTINED or ev.pnl is None:
        return None
    pnl = ev.pnl
    if pnl.closed_episodes < TAPE_CANDIDATE_MIN_CLOSED or pnl.realized_pnl_native <= 0:
        return None
    if pnl.win_rate is None or _dec(pnl.win_rate) < TAPE_CANDIDATE_MIN_WIN_RATE:
        return None
    unit = "base units" if (ev.tape is None or ev.tape.money_axis == "native") else "micro-USD"
    return (
        f"{pnl.closed_episodes} closed episodes, win rate {pnl.win_rate:.2f}, realized "
        f"+{pnl.realized_pnl_native} {unit} on partial tape; grade {score.grade.value}"
    )


@dataclass
class TapeRunReport:
    """What one batch pass over a chain's tape found. Every count is measured on that run."""

    chain: str
    wallets_seen: int = 0
    wallets_scored: int = 0
    wallets_too_thin: int = 0
    rows_read: int = 0
    by_grade: dict[str, int] = field(default_factory=dict)
    by_money_axis: dict[str, int] = field(default_factory=dict)
    sell_only_refused: int = 0
    quarantined_by_tag: int = 0
    capped_partial_tape: int = 0
    capped_small_n: int = 0
    b_or_better: int = 0
    tagged_wallets: int = 0
    candidates: list[dict[str, Any]] = field(default_factory=list)
    stored: int = 0
    #: Rows a batch failed to write. Non-zero means the grades are NOT in the table;
    #: see TAPE_STORE_BATCH for the lock contention this exists to make visible.
    store_failed: int = 0
    kept_full_grade: int = 0
    elapsed_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain": self.chain,
            "wallets_seen": self.wallets_seen,
            "wallets_scored": self.wallets_scored,
            "wallets_too_thin": self.wallets_too_thin,
            "rows_read": self.rows_read,
            "by_grade": dict(sorted(self.by_grade.items())),
            "by_money_axis": dict(sorted(self.by_money_axis.items())),
            "sell_only_refused": self.sell_only_refused,
            "quarantined_by_tag": self.quarantined_by_tag,
            "capped_partial_tape": self.capped_partial_tape,
            "capped_small_n": self.capped_small_n,
            "b_or_better": self.b_or_better,
            "tagged_wallets": self.tagged_wallets,
            "candidates": len(self.candidates),
            "stored": self.stored,
            "store_failed": self.store_failed,
            "kept_full_grade": self.kept_full_grade,
            "elapsed_s": round(self.elapsed_s, 2),
        }


def _early_metrics_index(conn: sqlite3.Connection, chain: Chain) -> dict[str, EarlyMetrics]:
    buyers = {
        r[0]: int(r[1])
        for r in conn.execute(
            "SELECT token, COUNT(*) FROM first_buyers WHERE chain = ? GROUP BY token", (chain.value,)
        )
    }
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in conn.execute(
        "SELECT wallet, token, rank, seconds_after_open FROM first_buyers WHERE chain = ?", (chain.value,)
    ):
        grouped[str(r[0])].append(
            {"rank": r[2], "seconds_after_open": r[3], "buyers": buyers.get(r[1], 0)}
        )
    out: dict[str, EarlyMetrics] = {}
    for wallet, rows in grouped.items():
        em = _early_metrics_from_rows(rows)
        if em is not None:
            out[wallet] = em
    return out


def _iter_wallet_rows(
    conn: sqlite3.Connection, chain: Chain, wallets: Sequence[str] | None
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """Yield ``(wallet, rows)`` groups. One ordered scan when no wallet list is given."""
    if wallets is not None:
        for w in wallets:
            rows = fetch_all(
                conn,
                f"SELECT {_TAPE_COLUMNS} FROM swaps WHERE chain = ? AND wallet = ? ORDER BY ts_ms, id",
                (chain.value, w),
            )
            yield w, rows
        return
    cur = conn.execute(
        f"SELECT wallet, {_TAPE_COLUMNS} FROM swaps WHERE chain = ? ORDER BY wallet, ts_ms, id",
        (chain.value,),
    )
    names = [d[0] for d in cur.description][1:]
    current: str | None = None
    bucket: list[dict[str, Any]] = []
    for r in cur:
        wallet = str(r[0])
        if wallet != current:
            if current is not None:
                yield current, bucket
            current, bucket = wallet, []
        bucket.append(dict(zip(names, r[1:], strict=True)))
    if current is not None:
        yield current, bucket


def grade_tape(
    conn: sqlite3.Connection | None = None,
    chain: Chain | str = Chain.SOL,
    *,
    wallets: Sequence[str] | None = None,
    min_rows: int = TAPE_MIN_ROWS,
    store: bool = False,
    overwrite_full: bool = False,
    candidate_limit: int = TAPE_CANDIDATE_LIMIT,
    as_of_ms: int | None = None,
    on_score: Any = None,
) -> TapeRunReport:
    """Grade every wallet on one chain's tape, or the given ones, and report the shape.

    Read-only unless ``store`` is set. When storing, a wallet that already holds a
    full-history grade (``model_version == MODEL_ID``) is left alone unless
    ``overwrite_full`` says otherwise — a provisional grade must not replace a measured one.
    ``on_score(evidence, score)`` is called for every scored wallet when given.
    """
    ch = chain if isinstance(chain, Chain) else Chain(str(chain))
    c = conn or ensure_db()
    started = time.perf_counter()
    report = TapeRunReport(chain=ch.value)

    # Explicit targets must bound all evidence reads, not just the swap replay.
    # The per-wallet early query still counts EVERY observed buyer of its token.
    tag_index = (provider_tag_index(c, ch) if wallets is None else
                 {w: provider_tags_from_events(c, ch, w) for w in wallets})
    early_index = (_early_metrics_index(c, ch) if wallets is None else
                   {w: _early_metrics(c, ch, w) for w in wallets})
    created_rows = (c.execute(
        "SELECT creator, COUNT(*) FROM tokens WHERE chain = ? AND creator IS NOT NULL GROUP BY creator",
        (ch.value,),
    ) if wallets is None else (
        (w, c.execute("SELECT COUNT(*) FROM tokens WHERE chain=? AND creator=?", (ch.value, w)).fetchone()[0])
        for w in wallets
    ))
    created_index = {str(r[0]): int(r[1]) for r in created_rows}
    wallet_rows = (c.execute("SELECT address, tags_json, meta_json FROM wallets WHERE chain=?", (ch.value,))
                   if wallets is None else (
                       c.execute("SELECT address, tags_json, meta_json FROM wallets WHERE chain=? AND address=?",
                                 (ch.value, w)).fetchone() for w in wallets
                   ))
    wallet_tags: dict[str, list[Any]] = {}
    # SEED CONFLUENCE, the component the rubric was built around and never received.
    #
    # `seed_confluence` is worth 12 of the grader's points and is read from
    # `wallets.meta_json`. Nothing in the tree wrote it, so it was None for every
    # wallet -- the module docstring records the predecessor failing the same way:
    # "seeds came from trending lists, so seed_confluence was always zero and no
    # wallet ever reached A". On the tape path it was not even accepted as an input.
    #
    # MEASURED 2026-09-24: of the wallets scoring 28-39, i.e. within reach of 12
    # points of the B bar at 40, 80 of 105 on sol, 48 of 55 on robinhood and 11 of 14
    # on bsc had bought at least one seed. See scripts writing `seed_confluence` into
    # `wallets.meta_json`; a None here still means 'we never ran the seed pass', which
    # is not the same as a measured zero.
    seed_index: dict[str, int] = {}
    for wallet_row in wallet_rows:
        if wallet_row is None:
            continue
        _addr, _tags, _meta = wallet_row
        wallet_tags[str(_addr)] = jload(_tags, [])
        _m = jload(_meta, {}) or {}
        if isinstance(_m, dict):
            _v = _m.get("seed_confluence")
            if isinstance(_v, int) and not isinstance(_v, bool) and _v >= 0:
                seed_index[str(_addr)] = _v
    full_rows = (c.execute(
        "SELECT address FROM wallet_scores WHERE chain = ? AND model_version = ?", (ch.value, MODEL_ID)
    ) if wallets is None else (
        c.execute("SELECT address FROM wallet_scores WHERE chain=? AND address=? AND model_version=?",
                  (ch.value, w, MODEL_ID)).fetchone() for w in wallets
    ))
    full_graded = {str(r[0]) for r in full_rows if r is not None}

    candidates: list[tuple[tuple[int, int], dict[str, Any]]] = []
    pending: list[WalletScore] = []
    # A DEDICATED WRITE CONNECTION, and the reason is not tidiness.
    #
    # ``_iter_wallet_rows`` streams one ordered scan of ``swaps`` and is still open
    # while the loop stores. On the same connection that read holds a transaction, so
    # every in-loop write failed with ``database is locked`` while the flush AFTER the
    # loop succeeded -- MEASURED 2026-09-24 on the robinhood tape: all 64 full batches
    # lost, and only the final 406-row batch, written once the cursor was exhausted,
    # landed. Batching alone did not fix it and could not have.
    #
    # A second connection writes while the first is still reading, which is exactly
    # what WAL is for. Closed in the ``finally`` below so a raising grade pass cannot
    # leak it.
    write_conn = None
    if store:
        from kaiba.core.db import connect as _connect

        write_conn = _connect()
    for address, rows in _iter_wallet_rows(c, ch, wallets):
        report.wallets_seen += 1
        report.rows_read += len(rows)
        if len(rows) < min_rows:
            report.wallets_too_thin += 1
            continue
        ev = tape_evidence_from_rows(
            address,
            ch,
            rows,
            wallet_tags=wallet_tags.get(address, []),
            provider_tags=tag_index.get(address, []),
            early_metrics=early_index.get(address),
            seed_confluence=seed_index.get(address),
            created_token_count=created_index.get(address, 0),
            as_of_ms=as_of_ms,
        )
        score = score_wallet(ev)
        report.wallets_scored += 1
        report.by_grade[score.grade.value] = report.by_grade.get(score.grade.value, 0) + 1
        axis = ev.tape.money_axis if ev.tape is not None else "native"
        report.by_money_axis[axis] = report.by_money_axis.get(axis, 0) + 1
        if ev.tape is not None and ev.tape.provider_tags:
            report.tagged_wallets += 1
        if any(p.startswith("sell_only:") for p in score.penalties):
            report.sell_only_refused += 1
        elif score.grade is Grade.QUARANTINED:
            report.quarantined_by_tag += 1
        if any(b.startswith("capped to B: partial tape") for b in score.blockers):
            report.capped_partial_tape += 1
        if any(b.startswith("capped to C: tape too thin") for b in score.blockers):
            report.capped_small_n += 1
        if score.grade in (Grade.A, Grade.B):
            report.b_or_better += 1

        if address not in full_graded:
            reason = tape_candidate_reason(ev, score)
            if reason is not None and ev.pnl is not None:
                key = (ev.pnl.closed_episodes, ev.pnl.realized_pnl_native)
                candidates.append(
                    (
                        key,
                        {
                            "address": address,
                            "grade": score.grade.value,
                            "score": score.score,
                            "closed_episodes": ev.pnl.closed_episodes,
                            "distinct_tokens": ev.pnl.distinct_tokens,
                            "closed_distinct_tokens": ev.closed_distinct_tokens,
                            "win_rate": ev.pnl.win_rate,
                            "realized_pnl": ev.pnl.realized_pnl_native,
                            "money_axis": axis,
                            "provider_tags": list(ev.tape.provider_tags) if ev.tape else [],
                            "reason": reason,
                        },
                    )
                )

        if on_score is not None:
            on_score(ev, score)
        if store:
            if address in full_graded and not overwrite_full:
                report.kept_full_grade += 1
            else:
                pending.append(score)
                if len(pending) >= TAPE_STORE_BATCH:
                    ok, bad = store_scores(pending, write_conn, protect_full=not overwrite_full)
                    report.stored += ok
                    report.store_failed += bad
                    pending.clear()

    if pending:
        ok, bad = store_scores(pending, write_conn, protect_full=not overwrite_full)
        report.stored += ok
        report.store_failed += bad
        pending.clear()

    if write_conn is not None:
        write_conn.close()

    candidates.sort(key=lambda item: item[0], reverse=True)
    report.candidates = [row for _, row in candidates[: max(0, candidate_limit)]]
    report.elapsed_s = time.perf_counter() - started
    return report


__all__ = [
    "BUY_STARVED_MAX_BUY_SHARE",
    "BUY_STARVED_PENALTY",
    "HUMAN_UNIT_SOURCE_PREFIXES",
    "MODEL_ID",
    "MODEL_ID_TAPE",
    "COMPONENT_MAX",
    "POSITIVE_REPUTATION_TAGS",
    "SELL_ONLY_MAX_BUY_SHARE",
    "SELL_ONLY_MIN_TRADES",
    "TAPE_CANDIDATE_LIMIT",
    "TAPE_STORE_BATCH",
    "store_scores",
    "TAPE_CANDIDATE_MIN_CLOSED",
    "TAPE_CANDIDATE_MIN_WIN_RATE",
    "TAPE_FULL_CREDIT_CLOSED_EPISODES",
    "TAPE_MIN_CLOSED_EPISODES",
    "TAPE_MIN_CLOSED_FOR_B",
    "TAPE_MIN_ROWS",
    "TAPE_MIN_TOKENS_FOR_B",
    "TAPE_SCALED_COMPONENTS",
    "TAPE_THRESHOLD_PROVENANCE",
    "TAPE_TOKEN_SCALE_DECIMALS",
    "TAPE_USD_MICRO",
    "VENDOR_ADMITTED_TAGS",
    "VENDOR_TAG_PREFIX",
    "EarlyMetrics",
    "Penalty",
    "ProviderStats",
    "Reputation",
    "TapeCoverage",
    "TapeRunReport",
    "WalletEvidence",
    "admit_vendor_label",
    "apply_tape_credit",
    "build_evidence",
    "build_tape_evidence",
    "buy_share",
    "closed_side_counts",
    "closed_token_count",
    "components_for",
    "creator_discount",
    "sell_only_rejection",
    "grade_address",
    "grade_from_tape",
    "grade_tape",
    "grade_wallets",
    "load_score",
    "map_provider_tags",
    "normalise_tape_rows",
    "penalties_for",
    "provider_tag_index",
    "provider_tags_from_events",
    "score_history",
    "score_wallet",
    "store_score",
    "tape_candidate_reason",
    "tape_credit",
    "tape_evidence_from_rows",
]
