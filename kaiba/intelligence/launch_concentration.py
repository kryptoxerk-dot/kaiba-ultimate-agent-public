"""True launch concentration, including the laundered kind, measured from the trade *sequence*.

``kaiba/intelligence/concentration.py`` already exists and owns a different measurement:
bundle-adjusted **holder-snapshot** concentration — who holds what *now*, collapsed into
funding-graph entities. It is a snapshot tool and nothing in it is wrong. It simply cannot
see the thing this module exists for, so this is a new module rather than an extension of
that one, and the two are deliberately not merged: one divides a holder list, the other
walks a tape.

The evasion, and why every snapshot tool reports zero on it
-----------------------------------------------------------

The operator's friend launched ``GIVE``
(``12qeY9vz1uZHZjWtQPuRfmJtMidPXg9mU1CY4Mpkygiv``, sol, pump.fun, 2026-09-18 17:01:18Z)
and described the method: buy with a few wallets, **sell**, then re-buy the same supply
with a dozen wallets that share no funding edge. Bubblemaps sees nothing, because there is
no edge to see. Our own ``kaiba/intelligence/bundles.py`` sees nothing either, and for a
structural reason it states itself: it groups **contiguous ``block_index`` runs inside one
slot** and requires ``>= 2`` distinct **entities**. A relay is neither contiguous — the
re-buys land seconds or minutes later, in different blocks — nor entity-linked. Both
defences are defeated by construction.

What the evidence actually looks like on GIVE, measured by the lead on the live box on
2026-09-22 from ``gmgn-cli token traders`` (99 traders with a first-buy timestamp) and
re-derived to the atom by this module's pure functions in
``tests/test_launch_concentration.py``:

======================================  =========================================
largest wave, first 34 s                 42-43 wallets, **59.418%** of supply
GMGN's own ``bundler`` maker tag         31 wallets, **50.550%** of supply
exact co-timed cohorts                   17 wallets, **7.372%**, in 6 cohorts
sell -> re-buy relay, e.g. at t+12 s     **4.743%** exits, **21.799%** re-bought in 120 s
wallets fully exited                     92 of 99, aggregate trader PnL -$342.71
**what every snapshot metric says today**  top-10 holder rate **0**, 7 holders, zero volume
======================================  =========================================

The operator guessed 30%. The launch wave was ~59%, and his tooling said zero. The
evidence exists **only in the trade sequence**, which is why this module takes a sequence
and not a holder list.

What is measured, and what each component is blind to
------------------------------------------------------

Five numbers, each separately reported and separately falsifiable, because they fail in
different directions and a caller that gets one blended number cannot tell which one moved:

``launch_wave_pct``
    Supply bought by wallets whose **first** buy falls within ``W`` seconds of the token's
    first trade, as a **curve** over :data:`WAVE_WINDOWS_S`. Not one number: the operator
    buys at t+1 s and what is knowable then is a different quantity from what the launch
    turns out to have been by t+60 s. Blind to a wave that is deliberately spread past the
    widest window; :attr:`WavePoint.in_window_atoms` is the strictly-truncated companion
    that an observer standing at t0+W could have computed.

``cotimed_pct``
    Supply held by wallets sharing an identical entry second **and** an identical exit
    second with at least one other wallet. This is the one a funding graph cannot see and
    the one a launderer cannot avoid without giving up synchronised exits. Blind until
    exits exist: on a token that has not been dumped yet it is legitimately 0 with a
    reason saying so, which is **not** the same as unavailable.

``relay_pct``
    Supply that exits and is re-bought within :data:`RELAY_WINDOW_S` seconds, matched
    FIFO so no atom is counted twice, reported as events and not only as a total.
    Blind to a relay slower than the window, and *loud* on an ordinary busy tape where
    every buy happens to follow some sell — which is why it is a supply number that feeds
    the headline through a deliberately narrow wallet set (see
    :func:`relay_takeover_wallets`).

``vendor_bundler_pct``
    GMGN's own ``bundler`` maker tag. A **cross-check from an opaque vendor**: never the
    primary measure, never blended into ours, never on the decision path. It is here so a
    disagreement is visible, and the two disagree on GIVE by 8.9 percentage points.

``headline_pct``
    The single number sizing consumes. See :func:`headline` for the combination rule and
    the two properties it is chosen to have.

Everything is a **wallet** count, not an entity count, and that is a design decision
rather than a gap. ``entity_members`` has rows on sol and none on bsc or robinhood, so an
entity count degrades to an address count on those chains — but here it would not help on
any chain, because the whole point of the evasion is that the wallets share no funding
edge. Collapsing on a graph that cannot see them would change nothing and would invite the
reader to believe it had.

Missing evidence is ``None`` and ``UNAVAILABLE``, never 0
----------------------------------------------------------

A token we cannot measure is not a token with 0% bundling. ``basis`` is
``measured`` or ``unavailable``, ``reason`` is populated on **both**, and ``coverage`` says
what fraction of the launch's token flow our tape actually accounts for. Three gates must
pass before a number is emitted, and each refusal names itself:

1. **Anchored.** The tape must be proved back to the launch —
   ``token_tape.coverage='complete'``, or ``curve_snapshots.coverage_from_ms <=
   tokens.created_ms``. A tape that starts late understates every component here, and
   understating is the direction that makes a size *larger*.
2. **Reconciled.** The tape's net token outflow must agree with the bonding curve's own
   reserve movement. This is the ``coverage`` number and it is an exact test, not a
   plausibility one: on 400 complete-tape sol mints it is exact to the atom on 271 of
   them. It is what catches a tape whose rows are all present and whose *amounts* are
   wrong — 18 of those 400 disagree by more than 10%, one of them by 6,696x, and every one
   of them would otherwise have produced a confident three-digit percentage.
3. **Ledger closes.** Per-wallet cumulative sells must not materially exceed cumulative
   buys, and the all-wallet peak net holding must not exceed supply. A tape that violates
   this cannot support a per-wallet attribution at all, which is exactly what
   :func:`headline` is.

Where our tape is thin the answer is ``UNAVAILABLE`` with the coverage figure attached.
It is never patched from the vendor: :func:`measure` does not call GMGN, and the vendor
cross-check is an argument the caller passes in.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, get_conn
from kaiba.core.schemas import Chain, EvidenceBasis, Measure, Receipt, Side, now_ms
from kaiba.intelligence.bundles import (
    PUMPFUN_CURVE_INVARIANT_ATOMS,
    PUMPFUN_LAUNCH_CURVE_ATOMS,
    SupplyBasis,
    launch_coverage_proved,
    resolve_supply,
)
from kaiba.intelligence.hubs import safe_normalize

log = logging.getLogger(__name__)

#: Bump when any definition below changes, so a stored or logged number says which
#: definition produced it.
MODEL_ID = "kaiba-launch-concentration-v1"

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)

#: A launch is a fixed historical event and the tape only deepens, so a day-old answer
#: about it is still the right answer.
FRESHNESS_BUDGET_S = 86_400


# --------------------------------------------------------------------------------------
# thresholds, and where every one of them came from
# --------------------------------------------------------------------------------------

#: The ``W`` grid for the launch-wave curve, in seconds from the token's first trade.
WAVE_WINDOWS_S: tuple[int, ...] = (1, 5, 15, 30, 60)

#: Which point of that curve :func:`headline` unions over.
HEADLINE_WAVE_WINDOW_S = 60

#: Seconds within which an exit that is re-bought counts as one relay.
RELAY_WINDOW_S = 120

#: Wallets needed before an identical (entry second, exit second) pair is a cohort.
MIN_COTIMED_COHORT = 2

#: Below this reconciliation agreement the measurement is refused outright.
MIN_COVERAGE = Decimal("0.98")

#: Tolerance around ``curve_snapshots.observed_ms`` when reconciling, in milliseconds.
RECONCILE_TOLERANCE_MS = 15_000

#: Share of supply by which per-wallet sells may exceed per-wallet buys before the tape's
#: wallet-level accounting is treated as unsound.
MAX_OVERSOLD_FRACTION = Decimal("0.02")

#: Trade rows needed before any of this is worth computing.
MIN_TRADE_EVENTS = 10

THRESHOLD_PROVENANCE: dict[str, str] = {
    "WAVE_WINDOWS_S": (
        "MEASURED anchor, INVENTED grid. GIVE's launch wave spans 34 s and holds 59.418% "
        "of supply across 42-43 wallets (n=99 traders, gmgn-cli token traders, "
        "2026-09-22), so the grid has to reach past 34 s or it cannot see that launch at "
        "all; 60 is the smallest round number that does. 1 s is not a guess either - it "
        "is the operator's stated entry latency, and the whole reason the answer is a "
        "curve rather than a number is that he needs to know what is knowable at t+1 s "
        "separately from what the launch turns out to have been. 5, 15 and 30 are "
        "INVENTED filler between those two measured ends and have never been swept "
        "against outcomes. Widening the grid can only raise every point (the cohort and "
        "its buying are both monotone in W), so a too-narrow grid understates, which is "
        "the direction that makes a size larger."
    ),
    "HEADLINE_WAVE_WINDOW_S": (
        "INVENTED, and deliberately the widest point of the grid. The headline is the "
        "number sizing consumes, so it takes the most complete view of the launch rather "
        "than the earliest. Note the measured consequence, reported rather than hidden: "
        "on 298 measurable sol mints the W=60 cohort is the *entire* observed wallet set "
        "in the median case, because a pump.fun mint's whole trading life is often "
        "shorter than 60 s. On that population the headline is therefore close to peak "
        "curve outflow and does NOT discriminate; the discriminating components are the "
        "W=1 and W=5 points, cotimed_pct and relay_pct. Lowering this to 5 would make the "
        "headline discriminate and would also make it miss a wave that took 34 s, which "
        "is the wave we have actually seen."
    ),
    "RELAY_WINDOW_S": (
        "MEASURED on one launch, INVENTED as a default. On GIVE the largest relay is at "
        "t+12 s: 4.743% of supply exits and 21.799% is re-bought inside 120 s. Both "
        "figures reproduce exactly at R=120 and are pinned by test. One launch is one "
        "launch: no distribution of relay latencies has been measured, and the correct "
        "value is whatever covers the latency a launderer can afford, which nobody here "
        "has established. Raising it merges unrelated churn into relays and inflates "
        "relay_pct; lowering it below the true latency silently returns a small number, "
        "which is the dangerous direction."
    ),
    "MIN_COTIMED_COHORT": (
        "DEFINITIONAL, not a threshold. One wallet cannot share a second with itself."
    ),
    "MIN_COVERAGE": (
        "MEASURED distribution, INVENTED cut. On 400 sol mints with "
        "token_tape.coverage='complete' and >=40 swaps, the tape's net token outflow "
        "reconciles against the bonding curve's own reserve movement exactly to the atom "
        "on 271, within 0.5% on 33, within 2% on 26, within 10% on 16, and disagrees by "
        "more than 10% on 18 (worst case 6,696x). The distribution is bimodal - exact, or "
        "broken - so any cut inside the empty middle gives nearly the same answer and "
        "0.98 is chosen there. It admits 314 of 400 before the ledger gate. Raising it to "
        "1.0 would refuse the 33 mints whose only error is a snapshot clock skew."
    ),
    "RECONCILE_TOLERANCE_MS": (
        "MEASURED. curve_snapshots.observed_ms is OUR read clock, not the chain's, so the "
        "reserve figure describes a moment our timestamp only approximates. Cutting the "
        "tape exactly at observed_ms reconciles 138 of 400 mints; searching for the best "
        "cut within +/-15 s reconciles 304. 15 s is the smallest value tried that "
        "captured the whole improvement, and the search takes a minimum over candidate "
        "cuts, so a wider tolerance can only find a better match - it cannot manufacture "
        "one, because the tape's cumulative outflow is monotone in the cut only where the "
        "tape is buys, and a spurious exact match would have to hit the curve's figure to "
        "the atom by accident."
    ),
    "MAX_OVERSOLD_FRACTION": (
        "MEASURED distribution, INVENTED cut. A wallet that sells more than our tape saw "
        "it buy acquired the difference somewhere we did not look - a transfer in, or a "
        "missing row - and a tape full of those cannot support a per-wallet attribution, "
        "which is what headline_pct is. Over 417 complete-tape sol mints with >=40 swaps, "
        "the oversold share of supply is exactly ZERO on 269 (64.5%), 1.70% at p75, "
        "19.62% at p90 and 191.70% at worst. Admitted counts by cut: 0% -> 269, 0.5% -> "
        "291, 1% -> 302, 2% -> 315, 5% -> 329, 10% -> 354. 2% is chosen on the shoulder "
        "and is a guess: nothing establishes how much transfer-in noise a launch-share "
        "estimate can absorb. Lowering it to 0 would refuse 46 mints whose only fault is "
        "a rounding-scale transfer; raising it to 10% admits mints that are 20% fiction. "
        "The hard guard beside it is not a tolerance at all: the all-wallet peak net "
        "holding must not exceed supply, which is an identity."
    ),
    "MIN_TRADE_EVENTS": (
        "INVENTED. Below roughly ten trades there is no sequence to read and every "
        "component is dominated by a single wallet. Nothing has been measured about where "
        "the real floor is."
    ),
    "PUMPFUN_LAUNCH_CURVE_ATOMS": (
        "MEASURED upstream and imported, not redefined: kaiba.intelligence.bundles records "
        "that every curve_snapshots row in this database reports exactly "
        "793,100,000,000,000 real token atoms at launch. Used here only as the *start* of "
        "the reconciliation interval, and only on a mint whose curve invariant has already "
        "been checked for exact equality."
    ),
    "PUMPFUN_CURVE_INVARIANT_ATOMS": (
        "MEASURED upstream and imported, not redefined: 015_curve_snapshots.sql names "
        "virtual_token_reserves - real_token_reserves = 279,900,000,000,000 as the one "
        "constant of the pump.fun curve. Used here as an equality test and never as an "
        "assumption - a mint whose reserves do not satisfy it exactly has no known launch "
        "reserve, so there is nothing to reconcile the tape against and the measurement is "
        "refused rather than estimated."
    ),
}


# --------------------------------------------------------------------------------------
# vocabulary
# --------------------------------------------------------------------------------------


class Basis(StrEnum):
    """Whether this report carries numbers or a refusal."""

    MEASURED = "measured"
    UNAVAILABLE = "unavailable"


class Gate(StrEnum):
    """Which precondition failed, so refusals are countable by cause.

    ``ANCHORED`` and ``RECONCILED`` are statements about our collection.
    ``LEDGER`` is a statement about the tape's internal consistency. ``THIN`` is a
    statement about the token. They are different facts and a sweep that lumps them
    together cannot tell a collection bug from a quiet mint.
    """

    NONE = "none"
    THIN = "thin"
    ANCHORED = "anchored"
    RECONCILED = "reconciled"
    LEDGER = "ledger"
    SUPPLY = "supply"


@dataclass(frozen=True, slots=True)
class TradeEvent:
    """One trade, at **second** resolution, in base units.

    Seconds rather than milliseconds because :func:`cotimed_cohorts` tests for an
    *identical* instant and equality at millisecond resolution would find nothing. Our
    sol tape already arrives at second resolution (every ``swaps.ts_ms`` for a
    ``pumpfun:trades`` row ends in ``000``), so this loses nothing there; on a tape with
    finer timestamps it is a deliberate quantisation and :func:`from_rows` performs it.
    """

    wallet: str
    ts_s: int
    side: Side
    atoms: int
    tx: str = ""

    @property
    def is_buy(self) -> bool:
        return self.side is Side.BUY


@dataclass(frozen=True, slots=True)
class WavePoint:
    """One point of the launch-wave curve."""

    window_s: int
    wallets: tuple[str, ...]
    #: Every atom the cohort ever bought, over the whole observed tape. This is the figure
    #: that reproduces GIVE's 59.418% at W=34, and it is **retrospective**: the same
    #: cohort re-measured an hour later can only have bought more.
    #:
    #: It is cumulative *buying*, so it can legitimately exceed 100% of supply when the
    #: same supply is bought, sold and bought again — which is precisely what a relay
    #: does, and it is measured above 100% on real mints. That is why the wave is a
    #: component and not the headline: :func:`headline` is the bounded one.
    bought_atoms: int
    #: Atoms the cohort bought strictly inside ``[t0, t0 + window_s]``. What an observer
    #: standing at ``t0 + window_s`` could have computed. Equal to all buying in the
    #: window, because a wallet buying inside it is in the cohort by definition.
    in_window_atoms: int
    pct: Measure
    in_window_pct: Measure

    @property
    def wallet_count(self) -> int:
        return len(self.wallets)


@dataclass(frozen=True, slots=True)
class CotimedCohort:
    """Wallets that entered on the same second and left on the same second.

    Seconds are in whatever frame the events were in. On a report built by :func:`measure`
    that is seconds since the token's first trade, because :func:`rebase_to_first_trade`
    runs first.
    """

    entry_s: int
    exit_s: int
    wallets: tuple[str, ...]
    atoms: int

    @property
    def size(self) -> int:
        return len(self.wallets)


@dataclass(frozen=True, slots=True)
class RelayEvent:
    """One second at which supply exited and was re-bought inside the relay window.

    Three amounts, and they are not interchangeable:

    * ``exit_atoms`` — everything sold at ``exit_s``.
    * ``rebuy_atoms`` — everything bought in ``(exit_s, exit_s + R]``, whoever bought it.
      These two are the pair the operator reads: on GIVE, ``t+12`` is 4.743% out and
      21.799% back in.
    * ``matched_atoms`` — the FIFO-matched overlap, which is the only one that can be
      summed across events without counting an atom twice, and the only one
      :func:`relay_total` adds up.
    """

    exit_s: int
    exit_atoms: int
    rebuy_atoms: int
    matched_atoms: int
    sellers: tuple[str, ...]
    buyers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VendorCrossCheck:
    """GMGN's ``bundler`` maker tag, kept at arm's length.

    Passed in by the caller, never fetched here. ``measure`` writes it into the report and
    into no other field: nothing in :func:`headline` reads it, and no threshold anywhere in
    this module compares against it.
    """

    provider: str
    tag: str
    wallets: int
    atoms: int
    traders_seen: int
    note: str | None = None


@dataclass(frozen=True)
class LaunchConcentrationReport:
    """One measurement object per ``(chain, token)``. Components are independent."""

    chain: Chain
    token: str
    model_id: str
    basis: Basis
    #: Always populated, on success as well as on refusal. An empty explanation is a bug.
    reason: str
    gate: Gate = Gate.NONE
    #: Fraction in 0..1 of the launch's token flow our tape accounts for, against the
    #: bonding curve's own reserve movement. ``None`` when it could not be established --
    #: which is itself a refusal, never a low number.
    coverage: Measure = field(default_factory=lambda: Measure.unknown(FRESHNESS_BUDGET_S))
    supply_atoms: int | None = None
    supply_basis: SupplyBasis = SupplyBasis.UNKNOWN

    first_trade_ms: int | None = None
    events_seen: int = 0
    wallets_seen: int = 0
    reconciled_to_s: int | None = None

    wave: tuple[WavePoint, ...] = ()
    cotimed: tuple[CotimedCohort, ...] = ()
    relays: tuple[RelayEvent, ...] = ()
    vendor: VendorCrossCheck | None = None

    launch_wave_pct: Measure = field(default_factory=lambda: Measure.unknown(FRESHNESS_BUDGET_S))
    cotimed_pct: Measure = field(default_factory=lambda: Measure.unknown(FRESHNESS_BUDGET_S))
    relay_pct: Measure = field(default_factory=lambda: Measure.unknown(FRESHNESS_BUDGET_S))
    vendor_bundler_pct: Measure = field(
        default_factory=lambda: Measure.unknown(FRESHNESS_BUDGET_S)
    )
    headline_pct: Measure = field(default_factory=lambda: Measure.unknown(FRESHNESS_BUDGET_S))

    headline_wallets: tuple[str, ...] = ()
    headline_peak_s: int | None = None
    #: Component names with no number, so a caller can list what it did not get.
    unknowns: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def measured(self) -> bool:
        return self.basis is Basis.MEASURED

    def wave_at(self, window_s: int) -> WavePoint | None:
        for point in self.wave:
            if point.window_s == window_s:
                return point
        return None

    def wave_curve(self) -> tuple[tuple[int, Decimal | None], ...]:
        """``((W, pct), ...)`` — the shape the operator asked to see."""
        return tuple((p.window_s, p.pct.value) for p in self.wave)

    def summary(self) -> str:
        if not self.measured:
            return f"launch concentration unavailable ({self.gate.value}): {self.reason}"
        curve = " ".join(
            f"W{p.window_s}={'n/a' if p.pct.value is None else f'{p.pct.value:.2f}%'}"
            for p in self.wave
        )
        head = "n/a" if self.headline_pct.value is None else f"{self.headline_pct.value:.2f}%"
        cot = "n/a" if self.cotimed_pct.value is None else f"{self.cotimed_pct.value:.2f}%"
        rel = "n/a" if self.relay_pct.value is None else f"{self.relay_pct.value:.2f}%"
        return (
            f"headline {head} | wave {curve} | cotimed {cot} over "
            f"{sum(c.size for c in self.cotimed)} wallets in {len(self.cotimed)} cohorts | "
            f"relay {rel} over {len(self.relays)} events | coverage "
            f"{'n/a' if self.coverage.value is None else f'{self.coverage.value:.4f}'}"
        )


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------


def _conn(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    return conn if conn is not None else get_conn()


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _receipt(endpoint: str, note: str | None = None) -> Receipt:
    return Receipt(
        provider="kaiba.intelligence",
        endpoint=endpoint,
        observed_at_ms=now_ms(),
        basis=EvidenceBasis.DERIVED,
        note=note,
    )


def _measure(value: Decimal | None, endpoint: str, note: str | None = None) -> Measure:
    """A number with provenance, or an explicit unknown. Never a 0 standing in for one."""
    if value is None:
        return Measure.unknown(FRESHNESS_BUDGET_S)
    return Measure(
        value=value,
        basis=EvidenceBasis.DERIVED,
        receipt=_receipt(endpoint, note),
        freshness_budget_s=FRESHNESS_BUDGET_S,
    )


def _pct(part: int, whole: int | None) -> Decimal | None:
    if whole is None or whole <= 0:
        return None
    return Decimal(part) / Decimal(whole) * HUNDRED


# --------------------------------------------------------------------------------------
# pure components: a sequence in, a component out. No database, no clock, no vendor.
# --------------------------------------------------------------------------------------


def normalise_events(events: Iterable[TradeEvent]) -> tuple[TradeEvent, ...]:
    """Sort into the one order every component below assumes.

    Within a second, **sells come before buys**. A relay is a sell followed by a re-buy and
    the two legs routinely land in the same second; ordering buys first would make the
    re-buy arrive before the exit it consumes and would silently lose the largest relays.
    On GIVE that ordering choice alone moves ``relay_pct``.
    """
    return tuple(
        sorted(events, key=lambda e: (e.ts_s, 1 if e.is_buy else 0, e.wallet, e.tx))
    )


def first_trade_second(events: Sequence[TradeEvent]) -> int | None:
    return events[0].ts_s if events else None


def rebase_to_first_trade(events: Sequence[TradeEvent]) -> tuple[TradeEvent, ...]:
    """Shift every timestamp so the token's first trade is second 0.

    Every second on a :class:`LaunchConcentrationReport` — a cohort's entry and exit, a
    relay's exit, the instant of the headline peak — is a second *since the launch*, which
    is the only frame the operator reads ("t+12"). The pure components deliberately do not
    rebase on their own: they work in whatever frame they are handed, so a caller can run
    them over a slice of a tape without the numbers silently changing meaning.
    :func:`measure` rebases once, after the reconciliation (which must compare against an
    absolute ``curve_snapshots.observed_ms``) and before the components.
    """
    ordered = normalise_events(events)
    if not ordered:
        return ()
    t0 = ordered[0].ts_s
    if t0 == 0:
        return ordered
    return tuple(
        TradeEvent(e.wallet, e.ts_s - t0, e.side, e.atoms, e.tx) for e in ordered
    )


def _wallet_ledgers(
    events: Sequence[TradeEvent],
) -> tuple[dict[str, int], dict[str, int], dict[str, int], dict[str, int]]:
    """``(first_buy_s, last_sell_s, bought_atoms, sold_atoms)`` keyed by wallet."""
    first_buy: dict[str, int] = {}
    last_sell: dict[str, int] = {}
    bought: dict[str, int] = {}
    sold: dict[str, int] = {}
    for event in events:
        if event.is_buy:
            first_buy.setdefault(event.wallet, event.ts_s)
            bought[event.wallet] = bought.get(event.wallet, 0) + event.atoms
        else:
            last_sell[event.wallet] = event.ts_s
            sold[event.wallet] = sold.get(event.wallet, 0) + event.atoms
    return first_buy, last_sell, bought, sold


def launch_wave(
    events: Sequence[TradeEvent],
    supply_atoms: int | None,
    windows_s: Sequence[int] = WAVE_WINDOWS_S,
) -> tuple[WavePoint, ...]:
    """The wave curve: one point per ``W``, both the retrospective and the truncated share.

    A wallet joins the ``W`` cohort when its **first** buy lands in ``[t0, t0+W]``. That
    "first" is what makes the cohort mean something: it separates the wallets that started
    the launch from the ones that arrived later and merely traded through it.

    Monotone in ``W`` by construction — the cohort grows and so does each member's counted
    buying — which is the property the GIVE check leans on: W=34 is 59.418%, so W=60 must
    be at least that, and it is 62.810%.
    """
    if not events:
        return ()
    t0 = events[0].ts_s
    first_buy, _, bought, _ = _wallet_ledgers(events)
    points: list[WavePoint] = []
    for window in sorted({int(w) for w in windows_s}):
        cohort = tuple(
            sorted(w for w, ts in first_buy.items() if 0 <= ts - t0 <= window)
        )
        member = set(cohort)
        lifetime = sum(bought[w] for w in cohort)
        in_window = sum(
            e.atoms for e in events if e.is_buy and e.ts_s - t0 <= window and e.wallet in member
        )
        note = f"{MODEL_ID}; first-buy window {window}s from the token's first trade"
        points.append(
            WavePoint(
                window_s=window,
                wallets=cohort,
                bought_atoms=lifetime,
                in_window_atoms=in_window,
                pct=_measure(_pct(lifetime, supply_atoms), "launch_concentration.wave", note),
                in_window_pct=_measure(
                    _pct(in_window, supply_atoms), "launch_concentration.wave_in_window", note
                ),
            )
        )
    return tuple(points)


def cotimed_cohorts(
    events: Sequence[TradeEvent], *, min_size: int = MIN_COTIMED_COHORT
) -> tuple[CotimedCohort, ...]:
    """Wallets sharing an identical entry second **and** an identical exit second.

    Entry is the wallet's first buy, exit is its last sell. Both must exist: a wallet still
    holding has no exit second and cannot be co-timed with anything, which is why this
    component is structurally blind on a token nobody has dumped yet.

    Two wallets that happen to buy in the same busy second are not a cohort here — they
    must *also* leave together. That conjunction is what makes the coincidence expensive:
    a launderer can spread funding, spread blocks and spread entry, but a synchronised
    exit is the behaviour he is actually coordinating.
    """
    first_buy, last_sell, bought, _ = _wallet_ledgers(events)
    groups: dict[tuple[int, int], list[str]] = {}
    for wallet, entry in first_buy.items():
        exit_s = last_sell.get(wallet)
        if exit_s is None:
            continue
        groups.setdefault((entry, exit_s), []).append(wallet)
    out = [
        CotimedCohort(
            entry_s=entry,
            exit_s=exit_s,
            wallets=tuple(sorted(members)),
            atoms=sum(bought.get(w, 0) for w in members),
        )
        for (entry, exit_s), members in groups.items()
        if len(members) >= min_size
    ]
    out.sort(key=lambda c: (-c.atoms, c.entry_s, c.exit_s))
    return tuple(out)


def cotimed_wallets(cohorts: Sequence[CotimedCohort]) -> frozenset[str]:
    return frozenset(w for c in cohorts for w in c.wallets)


def cotimed_atoms(cohorts: Sequence[CotimedCohort]) -> int:
    """Sum over cohorts. Disjoint by construction: a wallet has one entry and one exit."""
    return sum(c.atoms for c in cohorts)


def relay_events(
    events: Sequence[TradeEvent], *, window_s: int = RELAY_WINDOW_S
) -> tuple[RelayEvent, ...]:
    """Sell -> re-buy relays, matched FIFO so no atom is counted twice.

    The matching is a queue of exited atoms. A buy consumes the oldest unconsumed exit that
    is still inside the window; an exit older than the window is dropped unmatched. Each
    sold atom can be matched at most once and each bought atom can consume at most one sold
    atom, so :func:`relay_total` is a sum over a partition and cannot double-count — which
    is the only reason it can be added to anything.

    ``exit_atoms`` and ``rebuy_atoms`` on the event are deliberately **not** the matched
    figure. They are the two gross quantities the operator reads off a launch — how much
    left, how much came back — and on GIVE the t+12 event reproduces the lead's pair
    exactly at 4.743% and 21.799%.
    """
    if window_s < 0:
        raise ValueError("relay window must not be negative")
    ordered = normalise_events(events)
    exits_at: dict[int, int] = {}
    buys_at: dict[int, int] = {}
    for event in ordered:
        bucket = buys_at if event.is_buy else exits_at
        bucket[event.ts_s] = bucket.get(event.ts_s, 0) + event.atoms

    queue: deque[list[Any]] = deque()  # [exit_s, wallet, remaining]
    matched: dict[int, int] = {}
    sellers: dict[int, set[str]] = {}
    buyers: dict[int, set[str]] = {}
    for event in ordered:
        if not event.is_buy:
            queue.append([event.ts_s, event.wallet, event.atoms])
            continue
        remaining = event.atoms
        while remaining > 0 and queue:
            while queue and queue[0][0] < event.ts_s - window_s:
                queue.popleft()
            if not queue:
                break
            head = queue[0]
            take = min(remaining, head[2])
            head[2] -= take
            remaining -= take
            matched[head[0]] = matched.get(head[0], 0) + take
            sellers.setdefault(head[0], set()).add(head[1])
            buyers.setdefault(head[0], set()).add(event.wallet)
            if head[2] <= 0:
                queue.popleft()

    out = [
        RelayEvent(
            exit_s=exit_s,
            exit_atoms=exits_at.get(exit_s, 0),
            rebuy_atoms=sum(a for ts, a in buys_at.items() if exit_s < ts <= exit_s + window_s),
            matched_atoms=amount,
            sellers=tuple(sorted(sellers.get(exit_s, ()))),
            buyers=tuple(sorted(buyers.get(exit_s, ()))),
        )
        for exit_s, amount in sorted(matched.items())
        if amount > 0
    ]
    return tuple(out)


def relay_total(relays: Sequence[RelayEvent]) -> int:
    """Matched atoms only. Summing ``exit_atoms`` here would double-count overlapping windows."""
    return sum(r.matched_atoms for r in relays)


def relay_takeover_wallets(
    events: Sequence[TradeEvent], *, window_s: int = RELAY_WINDOW_S
) -> frozenset[str]:
    """The narrow relay wallet set: a **fresh** wallet taking supply off an exiting one.

    :func:`relay_events` is promiscuous on purpose — on any busy tape nearly every buy
    follows some sell inside two minutes, and that is the correct behaviour for a *supply*
    measure. It is the wrong behaviour for a *wallet* set, because a set that contains
    everybody adds nothing to a union. So the set is restricted to matches where the buyer
    is making its **first observed buy** and is not the wallet that just sold: a wallet
    appearing for the first time to absorb supply that has just left is the laundering
    fingerprint, and a market maker round-tripping its own inventory is not.

    Known limitation, stated because it bit the validation: on a tape reconstructed from
    per-wallet vendor aggregates every wallet has exactly one buy, so every buy is a first
    buy and this restriction does nothing. It discriminates only on a real per-trade tape.
    """
    ordered = normalise_events(events)
    queue: deque[list[Any]] = deque()
    seen_buyer: set[str] = set()
    out: set[str] = set()
    for event in ordered:
        if not event.is_buy:
            queue.append([event.ts_s, event.wallet, event.atoms])
            continue
        fresh = event.wallet not in seen_buyer
        seen_buyer.add(event.wallet)
        remaining = event.atoms
        while remaining > 0 and queue:
            while queue and queue[0][0] < event.ts_s - window_s:
                queue.popleft()
            if not queue:
                break
            head = queue[0]
            take = min(remaining, head[2])
            head[2] -= take
            remaining -= take
            if fresh and head[1] != event.wallet:
                out.add(head[1])
                out.add(event.wallet)
            if head[2] <= 0:
                queue.popleft()
    return frozenset(out)


def peak_concurrent_atoms(
    events: Sequence[TradeEvent], wallets: Iterable[str]
) -> tuple[int, int | None]:
    """``(peak atoms, second it happened)`` for the net holding of ``wallets`` together.

    Each wallet's net position is cumulative buys minus cumulative sells, floored at zero,
    so a wallet that sells supply our tape never saw it acquire cannot drive the total
    negative and cannot be given a phantom short. The floor is also what the ledger gate
    exists to keep honest: it can only ever *raise* the total, so a tape with large
    unexplained sells inflates this number, and :func:`ledger_closes` refuses those tapes
    rather than letting the inflation through.
    """
    member = frozenset(wallets)
    if not member:
        return 0, None
    net: dict[str, int] = {}
    total = 0
    best = 0
    best_at: int | None = None
    for event in normalise_events(events):
        if event.wallet not in member:
            continue
        held = net.get(event.wallet, 0)
        delta = event.atoms if event.is_buy else -min(event.atoms, held)
        net[event.wallet] = held + delta
        total += delta
        if total > best:
            best, best_at = total, event.ts_s
    return best, best_at


def oversold_atoms(events: Sequence[TradeEvent]) -> int:
    """Atoms sold beyond what our tape saw the seller buy, summed over wallets."""
    _, _, bought, sold = _wallet_ledgers(events)
    return sum(max(0, amount - bought.get(wallet, 0)) for wallet, amount in sold.items())


def ledger_closes(
    events: Sequence[TradeEvent],
    supply_atoms: int,
    *,
    max_oversold_fraction: Decimal = MAX_OVERSOLD_FRACTION,
) -> tuple[bool, str]:
    """Can this tape support a per-wallet attribution at all? ``(ok, reason)``.

    Two tests, and the second is an identity rather than a tolerance:

    1. Sells beyond observed buys must stay under ``max_oversold_fraction`` of supply.
    2. The **all-wallet** peak net holding must not exceed supply. Every wallet's net is
       floored at zero and their sum is the supply that has left the curve, so a total
       above supply is not a large number — it is an impossible one, and it means the
       amounts on these rows are not what they claim to be.

    Test 2 is what makes :func:`headline`'s 100% bound a proof rather than a hope: the
    headline sums a subset of exactly this quantity, so ``headline <= all-wallet peak <=
    supply`` whenever this returns ``True``.
    """
    if supply_atoms <= 0:
        return False, "no supply denominator, so no ledger to close"
    over = oversold_atoms(events)
    limit = int(Decimal(supply_atoms) * max_oversold_fraction)
    if over > limit:
        share = Decimal(over) / Decimal(supply_atoms) * HUNDRED
        return False, (
            f"wallets sold {share:.2f}% of supply more than our tape saw them buy, above the "
            f"{max_oversold_fraction * HUNDRED:.0f}% bound; the per-wallet ledger is not "
            "sound enough to attribute supply to wallets"
        )
    everyone = {e.wallet for e in events}
    peak, _ = peak_concurrent_atoms(events, everyone)
    if peak > supply_atoms:
        share = Decimal(peak) / Decimal(supply_atoms) * HUNDRED
        return False, (
            f"all wallets together peak at {share:.2f}% of supply held at one instant, which "
            "is impossible; the trade amounts on this tape are wrong, not merely incomplete"
        )
    return True, "per-wallet ledger closes against supply"


def headline(
    events: Sequence[TradeEvent],
    supply_atoms: int | None,
    *,
    wave_points: Sequence[WavePoint] = (),
    cohorts: Sequence[CotimedCohort] = (),
    relay_window_s: int = RELAY_WINDOW_S,
    wave_window_s: int = HEADLINE_WAVE_WINDOW_S,
) -> tuple[Measure, tuple[str, ...], int | None]:
    """The one number sizing consumes. ``(measure, union wallets, second of the peak)``.

    **The combination rule, and why it is not a max and not a sum.**

    A *max* over the three components throws away the case the operator actually faces: a
    launch that is 25% plain wave, 20% co-timed and 20% relayed is not a 25% launch, and
    reporting the largest component would say it was. A *sum* is worse — every component
    here is defined over wallets, the components overlap heavily by construction (a
    co-timed cohort that entered in the first second is also in the wave), and adding them
    counts those wallets two and three times.

    So the combination is a **union over the wallet sets**, and the number is a property of
    that union rather than a sum of the components' numbers::

        U = wave(wave_window_s) u cotimed u relay-takeover
        headline = max over t of ( sum over w in U of net_w(t) ) / supply

    Two properties, both of which are the reason for this shape:

    *Cannot double-count a wallet.* ``U`` is a set and the inner sum runs over it once, so
    a wallet listed by all three components contributes exactly one term at each instant.
    This is structural: there is no arithmetic path by which a wallet appears twice.

    *Cannot exceed 100%.* Each ``net_w(t)`` is floored at zero, so
    ``sum_{w in U} net_w(t) <= sum_{all w} net_w(t)``, and that all-wallet sum is the supply
    that has left the curve, which cannot exceed supply. :func:`ledger_closes` checks the
    right-hand side against the actual tape and refuses the measurement when it fails, so
    the bound is enforced rather than assumed. A caller that reaches this function with a
    tape that violates it gets a number above 100 and no clamp, deliberately: silently
    clamping would turn a broken tape into a plausible-looking answer.

    What this shape costs, measured rather than guessed: on 298 measurable sol mints the
    W=60 cohort is the entire observed wallet set at the median, so the union is everybody
    and the headline collapses towards peak curve outflow. See
    ``THRESHOLD_PROVENANCE['HEADLINE_WAVE_WINDOW_S']``. The headline is still the right
    number to *size* on — it is the supply that was, at one moment, in flagged hands — but
    it is not the number to *screen* on, and the components are reported separately so a
    caller can screen on those.
    """
    point = next((p for p in wave_points if p.window_s == wave_window_s), None)
    wave_set: frozenset[str] = frozenset(point.wallets) if point is not None else frozenset()
    union = wave_set | cotimed_wallets(cohorts) | relay_takeover_wallets(
        events, window_s=relay_window_s
    )
    peak, at = peak_concurrent_atoms(events, union)
    note = (
        f"{MODEL_ID}; union of wave(W={wave_window_s}s), cotimed and relay-takeover wallet "
        f"sets ({len(union)} wallets), scored as their peak simultaneous net holding"
    )
    return (
        _measure(_pct(peak, supply_atoms), "launch_concentration.headline", note),
        tuple(sorted(union)),
        at,
    )


def vendor_share(
    cross_check: VendorCrossCheck | None, supply_atoms: int | None
) -> Measure:
    """The vendor's own number, divided by our denominator and kept out of everything else."""
    if cross_check is None:
        return Measure.unknown(FRESHNESS_BUDGET_S)
    value = _pct(cross_check.atoms, supply_atoms)
    if value is None:
        return Measure.unknown(FRESHNESS_BUDGET_S)
    return Measure(
        value=value,
        basis=EvidenceBasis.PROVIDER_REPORTED,
        receipt=Receipt(
            provider=cross_check.provider,
            endpoint=f"token.traders tag={cross_check.tag}",
            observed_at_ms=now_ms(),
            basis=EvidenceBasis.PROVIDER_REPORTED,
            note=(
                f"cross-check only, never on the decision path; {cross_check.wallets} of "
                f"{cross_check.traders_seen} traders carry the tag"
            ),
        ),
        freshness_budget_s=FRESHNESS_BUDGET_S,
    )


# --------------------------------------------------------------------------------------
# reading our own tape
# --------------------------------------------------------------------------------------


def from_rows(rows: Iterable[Mapping[str, Any]]) -> tuple[TradeEvent, ...]:
    """Turn ``swaps`` rows into events, dropping what cannot be read rather than guessing.

    A row with no parseable ``amount_token`` is dropped: it carries no supply and inventing
    one would be a fabricated number. Dropped rows are why :func:`measure` reconciles the
    surviving ones against the curve instead of trusting the row count.
    """
    out: list[TradeEvent] = []
    for row in rows:
        atoms = _int_or_none(row.get("amount_token"))
        ts_ms = _int_or_none(row.get("ts_ms"))
        wallet = row.get("wallet")
        if atoms is None or atoms <= 0 or ts_ms is None or not wallet:
            continue
        side = Side.SELL if str(row.get("side", "")).lower() == "sell" else Side.BUY
        out.append(
            TradeEvent(
                wallet=str(wallet),
                ts_s=ts_ms // 1000,
                side=side,
                atoms=atoms,
                tx=str(row.get("tx") or ""),
            )
        )
    return normalise_events(out)


def _load_events(chain: Chain, token: str, conn: sqlite3.Connection) -> tuple[TradeEvent, ...]:
    rows = fetch_all(
        conn,
        "SELECT tx, ts_ms, wallet, side, amount_token FROM swaps "
        "WHERE chain=? AND token=? ORDER BY ts_ms, tx",
        (chain.value, token),
    )
    return from_rows(dict(r) for r in rows)


def tape_anchored(
    chain: Chain, token: str, conn: sqlite3.Connection, *, created_ms: int | None
) -> tuple[bool, str]:
    """Is our tape proved complete back to the launch? ``(ok, reason)``.

    Two independent proofs, either of which is enough. ``token_tape.coverage='complete'``
    is the collector's own record and its migration's CHECK constraints already forbid it
    being set from a wallet walk. ``curve_snapshots.coverage_from_ms <= created_ms`` is the
    older proof, duplicated in :func:`kaiba.intelligence.bundles.launch_coverage_proved`,
    which is imported here rather than copied — ``kaiba.ingest.token_flow``'s is
    authoritative if the three ever disagree.
    """
    try:
        row = fetch_one(
            conn,
            "SELECT coverage, route, proof, covered_from_ms, created_ms FROM token_tape "
            "WHERE chain=? AND token=?",
            (chain.value, token),
        )
    except sqlite3.Error as exc:
        log.warning("launch_concentration: token_tape unreadable for %s (%s)", token[:12], exc)
        row = None
    if row is not None and str(row["coverage"]) == "complete":
        return True, (
            f"token_tape proves this mint's trades complete back to its launch via "
            f"{row['route']}"
        )
    if launch_coverage_proved(chain, token, conn, created_ms=created_ms):
        return True, "a trade-route walk terminated at or before this mint's creation"
    state = "no token_tape row" if row is None else f"token_tape says {row['coverage']}"
    return False, (
        f"tape is not proved complete back to launch ({state}); every component here is a "
        "share of the launch and a tape that starts late understates all of them"
    )


def reconcile_coverage(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection,
    events: Sequence[TradeEvent],
    *,
    tolerance_ms: int = RECONCILE_TOLERANCE_MS,
) -> tuple[Decimal | None, int | None, str]:
    """``(coverage 0..1, second reached, reason)`` against the curve's own reserve movement.

    This is the coverage number, and it is an **exact** test rather than a plausibility
    one. The bonding curve reports how many token atoms it still holds; subtract that from
    the launch reserve and you have how many atoms left the curve, measured by the curve
    itself. Our tape's cumulative buys minus sells over the same interval must equal that
    figure. It does, to the atom, on 271 of 400 complete-tape sol mints.

    What it catches that a row count cannot: a tape with every row present and the wrong
    *amounts* on them. 18 of those 400 disagree by more than 10%, one by 6,696x, and each
    would otherwise have produced a confident three-digit launch-wave percentage.

    ``observed_ms`` is our read clock, not the chain's, so the cut is searched within
    ``tolerance_ms`` and the best agreement wins. The search cannot manufacture a match:
    it would have to hit the curve's atom-exact figure by accident.
    """
    try:
        snap = fetch_one(
            conn,
            "SELECT observed_ms, real_token_atoms, virtual_token_atoms FROM curve_snapshots "
            "WHERE chain=? AND token=? AND real_token_atoms IS NOT NULL "
            "ORDER BY observed_ms ASC LIMIT 1",
            (chain.value, token),
        )
    except sqlite3.Error as exc:
        log.warning("launch_concentration: curve snapshot unreadable %s (%s)", token[:12], exc)
        return None, None, "curve snapshots unreadable, so coverage cannot be established"
    if snap is None:
        return None, None, "no curve snapshot, so the tape cannot be reconciled against anything"
    real = _int_or_none(snap["real_token_atoms"])
    virtual = _int_or_none(snap["virtual_token_atoms"])
    observed_ms = _int_or_none(snap["observed_ms"])
    if real is None or virtual is None or observed_ms is None:
        return None, None, "curve snapshot is missing a reserve or a timestamp"
    if virtual - real != PUMPFUN_CURVE_INVARIANT_ATOMS:
        return None, None, (
            "curve invariant does not hold on this mint, so its launch reserve is unknown "
            "and there is nothing to reconcile the tape against"
        )
    outflow = PUMPFUN_LAUNCH_CURVE_ATOMS - real
    if outflow <= 0:
        return None, None, (
            "the curve had not net-sold any tokens by the only snapshot we hold, so the "
            "reconciliation has a zero denominator"
        )

    low = (observed_ms - tolerance_ms) // 1000
    high = (observed_ms + tolerance_ms) // 1000
    running = 0
    best: Decimal | None = None
    best_at: int | None = None
    for event in normalise_events(events):
        running += event.atoms if event.is_buy else -event.atoms
        if low <= event.ts_s <= high:
            error = abs(Decimal(running) / Decimal(outflow) - ONE)
            if best is None or error < best:
                best, best_at = error, event.ts_s
    if best is None:
        return None, None, (
            "no trade lands within the reconciliation window around the curve snapshot, so "
            "the tape and the curve cannot be compared at a common instant"
        )
    coverage = ONE - best
    if coverage < ZERO:
        coverage = ZERO
    return coverage, best_at, (
        f"tape net outflow agrees with the curve's own reserve movement to "
        f"{coverage * HUNDRED:.4f}% at t+{(best_at or 0) - (events[0].ts_s if events else 0)}s"
    )


def _unavailable(
    chain: Chain,
    token: str,
    gate: Gate,
    reason: str,
    *,
    coverage: Measure | None = None,
    events_seen: int = 0,
    wallets_seen: int = 0,
    first_trade_ms: int | None = None,
    notes: Sequence[str] = (),
) -> LaunchConcentrationReport:
    return LaunchConcentrationReport(
        chain=chain,
        token=token,
        model_id=MODEL_ID,
        basis=Basis.UNAVAILABLE,
        reason=reason,
        gate=gate,
        coverage=coverage or Measure.unknown(FRESHNESS_BUDGET_S),
        events_seen=events_seen,
        wallets_seen=wallets_seen,
        first_trade_ms=first_trade_ms,
        unknowns=(
            "launch_wave_pct",
            "cotimed_pct",
            "relay_pct",
            "headline_pct",
        ),
        notes=tuple(notes),
    )


def measure(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection | None = None,
    *,
    total_supply_atoms: int | None = None,
    windows_s: Sequence[int] = WAVE_WINDOWS_S,
    relay_window_s: int = RELAY_WINDOW_S,
    vendor: VendorCrossCheck | None = None,
) -> LaunchConcentrationReport:
    """Measure one ``(chain, token)`` from **our** ``swaps`` table, or refuse with a reason.

    Nothing here calls a provider. ``vendor`` is a cross-check the caller has already
    obtained and wants recorded beside our numbers; it is written into the report and read
    by nothing that computes one. That separation is the point: this must run at decision
    time on a token one second old, and a vendor round trip on the decision path would both
    cost a second and make the answer depend on an opaque tag.
    """
    c = _conn(conn)
    token = safe_normalize(token, chain)
    notes: list[str] = []
    if chain is not Chain.SOL:
        notes.append(
            f"chain is {chain.value}: the reconciliation gate is written against the "
            "pump.fun bonding curve and will refuse every non-sol mint until an equivalent "
            "curve reading exists for that chain"
        )

    row = fetch_one(
        c, "SELECT created_ms FROM tokens WHERE chain=? AND address=?", (chain.value, token)
    )
    created_ms = _int_or_none(row["created_ms"]) if row else None

    events = _load_events(chain, token, c)
    wallets_seen = len({e.wallet for e in events})
    first_ms = events[0].ts_s * 1000 if events else None
    if len(events) < MIN_TRADE_EVENTS:
        detail = (
            "this mint has no swap rows at all: nobody has pulled its tape, which is a "
            "fact about our collection and says nothing whatever about the token"
            if not events
            else f"only {len(events)} usable trade rows, below the {MIN_TRADE_EVENTS} needed "
            "before there is a sequence to read"
        )
        return _unavailable(
            chain, token, Gate.THIN,
            f"{detail}; a component computed from this would be a statement about our "
            "collection rather than about the token",
            events_seen=len(events), wallets_seen=wallets_seen, first_trade_ms=first_ms,
            notes=notes,
        )

    anchored, anchor_reason = tape_anchored(chain, token, c, created_ms=created_ms)
    if not anchored:
        return _unavailable(
            chain, token, Gate.ANCHORED, anchor_reason,
            events_seen=len(events), wallets_seen=wallets_seen, first_trade_ms=first_ms,
            notes=notes,
        )

    supply, supply_basis, supply_note = resolve_supply(
        chain, token, c, total_supply_atoms=total_supply_atoms, created_ms=created_ms
    )
    if supply_note:
        notes.append(supply_note)
    if supply is None or supply <= 0:
        return _unavailable(
            chain, token, Gate.SUPPLY,
            "no supply denominator could be established, so every percentage here would be "
            "a ratio with an invented bottom",
            events_seen=len(events), wallets_seen=wallets_seen, first_trade_ms=first_ms,
            notes=notes,
        )

    coverage_value, reconciled_at, coverage_reason = reconcile_coverage(chain, token, c, events)
    coverage_measure = _measure(
        coverage_value, "launch_concentration.coverage", f"{MODEL_ID}; {coverage_reason}"
    )
    if coverage_value is None:
        return _unavailable(
            chain, token, Gate.RECONCILED, coverage_reason,
            events_seen=len(events), wallets_seen=wallets_seen, first_trade_ms=first_ms,
            notes=notes,
        )
    if coverage_value < MIN_COVERAGE:
        return _unavailable(
            chain, token, Gate.RECONCILED,
            f"tape covers only {coverage_value * HUNDRED:.2f}% of the curve's own token "
            f"outflow, below the {MIN_COVERAGE * HUNDRED:.0f}% floor; a launch share read "
            "off this tape would be understated by an unknown amount",
            coverage=coverage_measure,
            events_seen=len(events), wallets_seen=wallets_seen, first_trade_ms=first_ms,
            notes=notes,
        )

    ledger_ok, ledger_reason = ledger_closes(events, supply)
    if not ledger_ok:
        return _unavailable(
            chain, token, Gate.LEDGER, ledger_reason,
            coverage=coverage_measure,
            events_seen=len(events), wallets_seen=wallets_seen, first_trade_ms=first_ms,
            notes=notes,
        )

    t0 = events[0].ts_s
    # From here on every second is a second since the launch, because that is the frame
    # the operator reads and the only one in which "t+12" means anything.
    relative = rebase_to_first_trade(events)
    wave = launch_wave(relative, supply, windows_s)
    cohorts = cotimed_cohorts(relative)
    relays = relay_events(relative, window_s=relay_window_s)
    head, union, peak_at = headline(
        relative, supply, wave_points=wave, cohorts=cohorts, relay_window_s=relay_window_s
    )

    endpoint_note = f"{MODEL_ID}; supply basis {supply_basis.value}; {anchor_reason}"
    unknowns: list[str] = []
    if not cohorts:
        exited = sum(1 for e in events if not e.is_buy)
        notes.append(
            "no co-timed cohort: "
            + (
                "no wallet has exited yet, so the component is structurally blind rather "
                "than negative"
                if exited == 0
                else f"{exited} sells observed and no two wallets share both an entry and an "
                "exit second"
            )
        )
    if not relays:
        notes.append(
            f"no sell->rebuy relay inside {relay_window_s}s: measured and absent, not unmeasured"
        )
    notes.append(
        "counts are ADDRESSES, not entities, by design: the evasion this measures is "
        "specifically wallets with no shared funding edge, so collapsing on the entity "
        "graph would change nothing and would invite the reader to believe it had"
    )
    if vendor is not None:
        notes.append(
            f"vendor cross-check from {vendor.provider} recorded but not used: it is an "
            "opaque tag and nothing here reads it"
        )

    return LaunchConcentrationReport(
        chain=chain,
        token=token,
        model_id=MODEL_ID,
        basis=Basis.MEASURED,
        reason=f"{anchor_reason}; {coverage_reason}; {ledger_reason}",
        gate=Gate.NONE,
        coverage=coverage_measure,
        supply_atoms=supply,
        supply_basis=supply_basis,
        first_trade_ms=first_ms,
        events_seen=len(events),
        wallets_seen=wallets_seen,
        reconciled_to_s=None if reconciled_at is None else reconciled_at - t0,
        wave=wave,
        cotimed=cohorts,
        relays=relays,
        vendor=vendor,
        launch_wave_pct=(
            wave[-1].pct if wave else Measure.unknown(FRESHNESS_BUDGET_S)
        ),
        cotimed_pct=_measure(
            _pct(cotimed_atoms(cohorts), supply), "launch_concentration.cotimed", endpoint_note
        ),
        relay_pct=_measure(
            _pct(relay_total(relays), supply), "launch_concentration.relay", endpoint_note
        ),
        vendor_bundler_pct=vendor_share(vendor, supply),
        headline_pct=head,
        headline_wallets=union,
        headline_peak_s=peak_at,  # already relative: the components ran on a rebased tape
        unknowns=tuple(unknowns),
        notes=tuple(notes),
    )


__all__ = [
    "HEADLINE_WAVE_WINDOW_S",
    "MAX_OVERSOLD_FRACTION",
    "MIN_COTIMED_COHORT",
    "MIN_COVERAGE",
    "MIN_TRADE_EVENTS",
    "MODEL_ID",
    "RECONCILE_TOLERANCE_MS",
    "RELAY_WINDOW_S",
    "THRESHOLD_PROVENANCE",
    "WAVE_WINDOWS_S",
    "Basis",
    "CotimedCohort",
    "Gate",
    "LaunchConcentrationReport",
    "RelayEvent",
    "TradeEvent",
    "VendorCrossCheck",
    "WavePoint",
    "cotimed_atoms",
    "cotimed_cohorts",
    "cotimed_wallets",
    "first_trade_second",
    "from_rows",
    "headline",
    "launch_wave",
    "ledger_closes",
    "measure",
    "normalise_events",
    "oversold_atoms",
    "peak_concurrent_atoms",
    "rebase_to_first_trade",
    "reconcile_coverage",
    "relay_events",
    "relay_takeover_wallets",
    "relay_total",
    "tape_anchored",
    "vendor_share",
]
