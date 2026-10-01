"""What the tape says about *time in the position*, and what it flatly cannot say.

Piece 3 of the confluence programme: given that we are already in a token, is holding it
longer worth anything? This module answers that from our own rows only, and its first job
is to be honest about which of the questions asked are answerable at all with the data we
hold on 2026-09-22.

The short version, before any number
------------------------------------

Three populations exist in this database and they are **not the same market**:

``POP_CURVE_LAUNCH``
    pump.fun bonding-curve tape (``swaps.source='pumpfun:trades'``) for mints whose
    ``token_tape`` row says ``coverage='complete'``. Every trade the mint had is present
    inside the covered window and the window starts at creation, so a price path here is
    real and gap-free. The catch is the window: MEASURED 2026-09-22, all 5,271 complete
    rows have ``attempts=1`` and ``covered_to_ms - covered_from_ms`` under five minutes,
    median under one. :mod:`kaiba.intelligence.confluence` already says why - "the
    pump.fun trades endpoint serves a hot window, so for most mints in this database the
    slice is all we will ever have". **This population cannot answer a question about
    +5, +20 or +60 minutes. Not badly - at all.**

``POP_SMART_MONEY``
    mints where a ``gmgn:smartmoney`` buy print exists. Anchored on the first such print,
    forward prices taken from every source we hold for that mint. This is the only
    population with an hour-long price series, and it is the universe ``sm-trenches``
    actually trades. It is also the one with the selection problem that matters:
    **a print exists only while a tracked wallet is still trading the mint.** A mint that
    goes untradeable stops printing, so "no price" is recorded as "unknown", never as
    "-100%". Every survival and every mean from this population is therefore an
    **upper bound**, and the module labels it as such rather than burying the caveat.

``POP_REALISED``
    our own closed trades - 73 of them, 10 live, at the last read on 2026-09-22 02:23 UTC,
    and the box is still trading, so :func:`realised_hold_vs_pnl` recounts on every run
    rather than carrying a number in this docstring. The only population with real fills,
    and the one where holding time is an *output of the exit rule*, not an input. See
    :func:`realised_hold_vs_pnl`, which computes the tempting table and then refuses to
    let it be read as a hold rule.

The one thing a hold study must not do
--------------------------------------

Our shadow book's realised returns rise with realised ``hold_s``. That correlation is
manufactured by the stop: a position that fell hit ``-30%`` and closed in under two
minutes, and a position that rose kept its trailing stop alive for ten. You cannot choose
to be in the long-hold bucket; the market puts you there. Reading that table as "hold
longer" is precisely how a folk rule gets a number attached to it, so
:class:`RealisedHoldTable` carries ``confounded=True`` and the reason travels with the
data structure, not in a footnote a caller can drop.

The live book settles the argument without needing the theory. MEASURED 2026-09-22 02:23
UTC: ``spearman(hold_s, pnl_pct)`` is ``+0.191`` over 63 shadow trades and ``-0.333`` over
10 live ones. Same book, same exit rule, opposite sign, both far too small to mean
anything. Whichever one a hold rule was built on, the other half of our own evidence
would have argued against it.

Provenance
----------

Every constant in :data:`PROVENANCE` is MEASURED (with the query behind it) or INVENTED
(with what would settle it). Nothing here is fitted to anything: the module measures,
it does not tune, and where it reports an in-sample number it reports the out-of-sample
twin beside it or reports neither.

Costs
-----

Net of costs or it does not count. :class:`CostModel` defaults to the MEASURED GMGN
charge of 1% per leg plus the measured flat lamport cost, and exposes the unresolved
ATA-rent question as a switch rather than picking an answer: at a 0.056 SOL ticket the
rent is 3.6% of the position, which is larger than every mean return in this study, so
whether the exit closes the account decides the sign of several of these tables.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kaiba.learning import gates

__all__ = [
    "PROVENANCE",
    "IMPUTATIONS",
    "CostModel",
    "DEFAULT_GRID_S",
    "Anchor",
    "Series",
    "HoldPoint",
    "HoldCurve",
    "SurvivalPoint",
    "FeatureResult",
    "AddToWinnerRow",
    "RealisedHoldTable",
    "Report",
    "load_series",
    "entry_price",
    "survival_sensitivity",
    "anchors_curve_launch",
    "anchors_smart_money",
    "hold_curve",
    "survival_curve",
    "entry_feature_study",
    "add_to_winner",
    "time_stop_grid",
    "realised_hold_vs_pnl",
    "trade_rate",
    "sample_needed",
    "run",
    "main",
]

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000

#: The horizons the operator asked about, plus the six realised live holds overlaid.
DEFAULT_GRID_S: tuple[int, ...] = (
    15, 30, 39, 49, 60, 90, 120, 133, 167, 180, 224, 300, 600, 900, 1200, 1216, 1800, 3600,
)

#: Our six live round trips, in seconds, for the overlay the operator asked for.
LIVE_HOLDS_S: tuple[int, ...] = (39, 49, 133, 167, 224, 1216)

PROVENANCE: dict[str, str] = {
    "fee_bps_per_leg": (
        "MEASURED - GMGN charges 1% per leg on both chains we route through; recorded in "
        "docs/TRADING-METHOD.md and reproduced by the order receipts."
    ),
    "flat_lamports_per_leg": (
        "MEASURED - non-recoverable flat cost on sol is ~0.00005 SOL per leg excluding "
        "ATA rent."
    ),
    "ata_rent_lamports": (
        "MEASURED as a number (0.00204 SOL) but UNRESOLVED as a cost: it is recoverable "
        "only if the exit closes the token account. Which of the two it is decides "
        "whether the economic floor is 0.045 SOL or far less, so CostModel exposes it as "
        "a switch and this module reports both sides rather than choosing."
    ),
    "position_lamports": (
        "MEASURED - the live sm-trenches ticket was 0.056 SOL; the flat costs are only "
        "meaningful as a share of a stated ticket."
    ),
    "DEFAULT_GRID_S": (
        "DEFINITIONAL - the horizons in the question (+5/+20/+60 min) plus the six "
        "realised live holds so they can be overlaid on the same axis."
    ),
    "alive_window_s": (
        "INVENTED - a mint counts as having a price at t if a print exists within 60 s "
        "either side. No measurement fixes this width; it trades a stricter definition "
        "against the sampling rate of the feed. What would settle it: a depth quote at a "
        "chosen instant, which we do not record. Sensitivity to 30/60/120 s is reported."
    ),
    "exit_depth_multiple": (
        "INVENTED - a mint counts as *exitable* at t if the traded USD volume inside the "
        "alive window is at least 10x our own ticket. It is a volume proxy for depth, not "
        "depth. What would settle it: a routed sell quote of our own size at that "
        "instant, which GMGN can give live and we do not store."
    ),
    "min_prints_for_path": (
        "DEFINITIONAL - a price path needs at least two prints; one print is a price, not "
        "a path."
    ),
    "winner_threshold_pct": (
        "INVENTED - 'a winner' at the check point is defined as gross return above +20%. "
        "Nothing measured picks 20%; the add-to-winner test is reported across a grid of "
        "thresholds (0/20/50%) so the answer does not hang on this one. What would "
        "settle it: a fitted threshold, which needs more closed trades than this book "
        "has ever taken and would need its own held-out half."
    ),
    "imputation_bracket": (
        "STRUCTURAL - a horizon with no print is not a return of zero and not a return "
        "of nothing. 'observed_only' drops it (optimistic), 'dead_is_zero' books -100% "
        "(pessimistic), 'last_price' marks to the last print (conventional, and wrong in "
        "a knowable direction for an abandoned mint). Nothing measured picks between "
        "them, so all three are reported and a finding must hold at BOTH ends. What "
        "would settle it: a routed sell quote at the horizon, which we do not store."
    ),
    "forward_window_halving": (
        "DEFINITIONAL - the forward price window is capped at half the horizon and must "
        "come from a print strictly after the entry. Without this a 60 s window around "
        "t0+30 s returns the entry print itself and books 0.00% for a mint that never "
        "traded again: MEASURED on the first pass, median gross was exactly 0.00% at "
        "every horizon at or below the window width, which was the artefact and not the "
        "market."
    ),
    "split_rule": (
        "STRUCTURAL - held out by time at the median anchor timestamp. Our whole sol "
        "universe was created inside roughly one 28-hour window (replay.bootstrap_ci "
        "documents the same constraint), so the two halves are hours apart, not regimes "
        "apart. An out-of-sample number here rules out in-sample fitting; it does not "
        "rule out regime dependence, and must never be read as if it did."
    ),
}


# --------------------------------------------------------------------------- costs


@dataclass(frozen=True)
class CostModel:
    """What a round trip costs, multiplicative legs and flat lamports kept separate.

    ``net = (1 - f) * (1 + gross) * (1 - f) - 1 - flat/position``

    The flat term is not a bps figure because at our ticket it is not small: 0.0001 SOL
    of gas on a 0.056 SOL position is 18 bps, and the unresolved ATA rent is 364 bps.
    """

    fee_bps_per_leg: int = 100
    flat_lamports_per_leg: int = 50_000
    position_lamports: int = 56_000_000
    ata_rent_lamports: int = 2_040_000
    ata_rent_lost: bool = False
    extra_slippage_bps_per_leg: int = 0

    @property
    def leg_fraction(self) -> float:
        return (self.fee_bps_per_leg + self.extra_slippage_bps_per_leg) / 10_000.0

    @property
    def flat_fraction(self) -> float:
        flat = 2 * self.flat_lamports_per_leg
        if self.ata_rent_lost:
            flat += self.ata_rent_lamports
        return flat / float(self.position_lamports)

    def net(self, gross: float) -> float:
        f = self.leg_fraction
        return (1.0 - f) * (1.0 + gross) * (1.0 - f) - 1.0 - self.flat_fraction

    @property
    def breakeven_gross(self) -> float:
        """The gross return a round trip must make before it has made anything."""
        f = self.leg_fraction
        return (1.0 + self.flat_fraction) / ((1.0 - f) ** 2) - 1.0

    def describe(self) -> dict[str, Any]:
        return {
            "fee_bps_per_leg": self.fee_bps_per_leg,
            "extra_slippage_bps_per_leg": self.extra_slippage_bps_per_leg,
            "flat_lamports_per_leg": self.flat_lamports_per_leg,
            "position_lamports": self.position_lamports,
            "ata_rent_lamports": self.ata_rent_lamports,
            "ata_rent_lost": self.ata_rent_lost,
            "round_trip_breakeven_gross_pct": round(100.0 * self.breakeven_gross, 4),
        }


#: Both sides of the unresolved ATA question, so no table is reported on one of them only.
COST_RENT_RECOVERED = CostModel(ata_rent_lost=False)
COST_RENT_LOST = CostModel(ata_rent_lost=True)


# ----------------------------------------------------------------------- price tape


@dataclass(frozen=True)
class Print:
    ts_ms: int
    price: float
    usd: float
    side: str
    source: str


@dataclass
class Series:
    """Every print we hold for one mint, ascending. Not necessarily gap-free.

    ``seal`` builds the timestamp index and the prefix volume sum that make lookups
    logarithmic; the loader calls it once and the study then hits ``price_at`` a few
    hundred thousand times.
    """

    chain: str
    token: str
    prints: list[Print] = field(default_factory=list)
    _ts: list[int] = field(default_factory=list, repr=False)
    _cumusd: list[float] = field(default_factory=list, repr=False)

    def seal(self) -> Series:
        self.prints.sort(key=lambda p: p.ts_ms)
        self._ts = [p.ts_ms for p in self.prints]
        acc = 0.0
        cum = [0.0]
        for p in self.prints:
            acc += p.usd
            cum.append(acc)
        self._cumusd = cum
        return self

    def first_ts(self) -> int | None:
        return self.prints[0].ts_ms if self.prints else None

    def last_ts(self) -> int | None:
        return self.prints[-1].ts_ms if self.prints else None

    def price_at(
        self, ts_ms: int, window_ms: int, *, after_ms: int | None = None
    ) -> float | None:
        """Price of the print nearest ``ts_ms`` within ``window_ms``. ``None`` otherwise.

        Nearest rather than last-before: at these sampling rates a last-before rule
        silently carries a stale price forward across a minute of nothing, which is the
        same mistake as assuming a mint that stopped printing is flat.

        ``after_ms`` excludes prints at or before an instant. A forward price must obey
        it, because without it a 60 s window around ``t0 + 30 s`` can return the entry
        print itself and book a 0.00% return for a mint that never traded again - which
        is exactly the artefact that makes a dead-mint curve look flat instead of absent.
        """
        ts = self._ts
        if not ts:
            return None
        lo = bisect_left(ts, ts_ms - window_ms)
        hi = bisect_right(ts, ts_ms + window_ms)
        best: float | None = None
        best_d = window_ms + 1
        for j in range(lo, hi):
            if after_ms is not None and ts[j] <= after_ms:
                continue
            d = abs(ts[j] - ts_ms)
            if d < best_d:
                best, best_d = self.prints[j].price, d
        return best

    def last_price_before(self, ts_ms: int) -> float | None:
        """The most recent print at or before ``ts_ms``, however stale. For mark-to-last."""
        i = bisect_right(self._ts, ts_ms)
        return self.prints[i - 1].price if i > 0 else None

    def volume_between(self, lo_ms: int, hi_ms: int) -> float:
        ts = self._ts
        if not ts:
            return 0.0
        a = bisect_left(ts, lo_ms)
        b = bisect_right(ts, hi_ms)
        return self._cumusd[b] - self._cumusd[a]

    def window(self, lo_ms: int, hi_ms: int) -> list[Print]:
        a = bisect_left(self._ts, lo_ms)
        b = bisect_right(self._ts, hi_ms)
        return self.prints[a:b]


def load_series(
    conn: sqlite3.Connection,
    *,
    chain: str = "sol",
    sources: Sequence[str] | None = None,
    tokens: Iterable[str] | None = None,
) -> dict[str, Series]:
    """Every usable print for ``chain``, grouped by mint.

    Prices are ``swaps.price_usd``. Two known hazards, both handled here rather than
    left for the caller:

    * the gmgn feeds record a swap as two rows, one per leg, and the SOL leg is stored
      under the wrapped-SOL mint. Filtering that mint out is what keeps a token series
      from silently containing the SOL price.
    * ``swaps.amount_quote`` is not a consistent unit on the gmgn feeds - MEASURED
      2026-09-22, ``price_usd / (amount_quote/amount_token)`` reads 1.0 on 5,893 rows
      and ~117.7 on hundreds of others - so a SOL-denominated price cannot be rebuilt
      from it. USD it is, which imports the SOL/USD print-to-print jitter (MEASURED at up
      to 3.7% between two consecutive native_prices reads). :func:`cross_check_sol_price`
      quantifies how much of any curve is that jitter.
    """
    sql = (
        "SELECT token, ts_ms, price_usd, usd_value, side, source FROM swaps "
        "WHERE chain=? AND token<>? AND price_usd IS NOT NULL AND price_usd<>''"
    )
    args: list[Any] = [chain, SOL_MINT]
    if sources:
        sql += f" AND source IN ({','.join('?' * len(sources))})"
        args.extend(sources)
    sql += " ORDER BY token, ts_ms"

    wanted = set(tokens) if tokens is not None else None
    out: dict[str, Series] = {}
    for token, ts_ms, px, usd, side, source in conn.execute(sql, args):
        if wanted is not None and token not in wanted:
            continue
        try:
            price = float(px)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(price) or price <= 0.0:
            continue
        try:
            usd_v = float(usd) if usd not in (None, "") else 0.0
        except (TypeError, ValueError):
            usd_v = 0.0
        s = out.get(token)
        if s is None:
            s = out[token] = Series(chain=chain, token=token)
        s.prints.append(Print(int(ts_ms), price, usd_v, str(side), str(source)))
    for s in out.values():
        s.seal()
    return out


def cross_check_sol_price(conn: sqlite3.Connection, tokens: Sequence[str]) -> dict[str, Any]:
    """How much of a USD-denominated move is SOL/USD noise rather than the mint moving.

    For pump.fun rows we hold ``amount_native`` (lamports) and ``amount_token`` (atoms),
    so ``amount_native/amount_token`` is a SOL-denominated price up to a per-mint decimals
    constant that cancels inside a ratio. Comparing the two return series on the same
    prints is the only way to know whether a +2% reading is the token or the quote asset.
    """
    q = (
        "SELECT token, ts_ms, price_usd, amount_native, amount_token FROM swaps "
        "WHERE source='pumpfun:trades' AND amount_native IS NOT NULL AND amount_token IS NOT NULL "
        "AND price_usd IS NOT NULL ORDER BY token, ts_ms"
    )
    per: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
    want = set(tokens)
    for token, ts, px, nat, tok in conn.execute(q):
        if want and token not in want:
            continue
        try:
            usd = float(px)
            sol = float(nat) / float(tok)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if usd > 0 and sol > 0 and math.isfinite(usd) and math.isfinite(sol):
            per[token].append((int(ts), usd, sol))
    diffs: list[float] = []
    for rows in per.values():
        if len(rows) < 2:
            continue
        _, u0, s0 = rows[0]
        for _, u, s in rows[1:]:
            diffs.append((u / u0 - 1.0) - (s / s0 - 1.0))
    if not diffs:
        return {"n_pairs": 0, "note": "no pump.fun rows carried both price_usd and lamports"}
    return {
        "n_pairs": len(diffs),
        "mean_usd_minus_sol_return_pct": round(100.0 * statistics.fmean(diffs), 4),
        "sd_usd_minus_sol_return_pct": round(
            100.0 * (statistics.pstdev(diffs) if len(diffs) > 1 else 0.0), 4
        ),
        "p95_abs_pct": round(
            100.0 * sorted(abs(d) for d in diffs)[int(0.95 * (len(diffs) - 1))], 4
        ),
        "reading": (
            "this is the quote-asset contamination in every USD-denominated number below: "
            "a hold-curve move smaller than the sd here is not a measurement of the mint"
        ),
    }


# ------------------------------------------------------------------------- anchors


@dataclass(frozen=True)
class Anchor:
    """One hypothetical entry. ``censor_ms`` is where our observation honestly stops."""

    chain: str
    token: str
    t0_ms: int
    population: str
    censor_ms: int | None = None
    lane: str | None = None


def anchors_curve_launch(conn: sqlite3.Connection, *, chain: str = "sol") -> list[Anchor]:
    """First bonding-curve trade of every mint whose tape is proved complete.

    ``censor_ms`` is ``token_tape.covered_to_ms``: past it we hold nothing and know
    nothing, and the difference between those two is the entire point of censoring.
    """
    rows = conn.execute(
        "SELECT tt.token, tt.covered_to_ms, MIN(s.ts_ms) FROM token_tape tt "
        "JOIN swaps s ON s.chain=tt.chain AND s.token=tt.token AND s.source='pumpfun:trades' "
        "WHERE tt.coverage='complete' AND tt.chain=? GROUP BY tt.token",
        (chain,),
    ).fetchall()
    out = []
    for token, covered_to, t0 in rows:
        if t0 is None:
            continue
        out.append(
            Anchor(
                chain=chain,
                token=token,
                t0_ms=int(t0),
                population="POP_CURVE_LAUNCH",
                censor_ms=int(covered_to) if covered_to is not None else None,
            )
        )
    out.sort(key=lambda a: a.t0_ms)
    return out


def anchors_smart_money(conn: sqlite3.Connection, *, chain: str = "sol") -> list[Anchor]:
    """First ``gmgn:smartmoney`` buy print per mint - the ``sm-trenches`` trigger.

    Censoring here is the wall clock, not a coverage row: the feed keeps producing until
    the last ingest, so an anchor is only observable out to ``max(ts_ms) - t0``.
    """
    last = conn.execute(
        "SELECT MAX(ts_ms) FROM swaps WHERE chain=?", (chain,)
    ).fetchone()[0]
    if last is None:
        return []
    rows = conn.execute(
        "SELECT token, MIN(ts_ms) FROM swaps WHERE chain=? AND source='gmgn:smartmoney' "
        "AND side='buy' AND token<>? GROUP BY token",
        (chain, SOL_MINT),
    ).fetchall()
    out = [
        Anchor(
            chain=chain,
            token=token,
            t0_ms=int(t0),
            population="POP_SMART_MONEY",
            censor_ms=int(last),
            lane="sm-trenches",
        )
        for token, t0 in rows
        if t0 is not None
    ]
    out.sort(key=lambda a: a.t0_ms)
    return out


def split_by_time(anchors: Sequence[Anchor]) -> tuple[list[Anchor], list[Anchor], int | None]:
    """Earlier half fits, later half reports. See ``PROVENANCE['split_rule']``."""
    stamps = sorted(a.t0_ms for a in anchors)
    if len(stamps) < 4:
        return list(anchors), [], None
    cut = stamps[len(stamps) // 2]
    early = [a for a in anchors if a.t0_ms < cut]
    late = [a for a in anchors if a.t0_ms >= cut]
    return early, late, cut


# --------------------------------------------------------------- the hold curve


@dataclass
class HoldPoint:
    delta_s: int
    n_eligible: int          # anchors whose observation window reaches this horizon
    n_priced: int            # of those, how many still had a price
    coverage: float | None   # n_priced / n_eligible: the survival of *observability*
    mean_gross_pct: float | None
    median_gross_pct: float | None
    win_rate_pct: float | None
    mean_net_pct: float | None
    mean_net_rent_lost_pct: float | None
    p25_gross_pct: float | None
    p75_gross_pct: float | None


@dataclass
class HoldCurve:
    population: str
    chain: str
    label: str
    imputation: str
    n_anchors: int
    points: list[HoldPoint]
    bias: str
    peak_delta_s: int | None
    peak_mean_net_pct: float | None


#: How a horizon with no print inside the window is booked.
#:
#: ``observed_only``  drop it. This is what a naive study does and it is the optimistic
#:                    bound: mints that stopped trading leave the sample instead of
#:                    leaving a loss, so the mean is conditional on continued attention.
#: ``last_price``     carry the last print forward. On ``POP_CURVE_LAUNCH`` this is not
#:                    an approximation at all: a bonding-curve price is a deterministic
#:                    function of the reserves and the reserves move only on a trade, so
#:                    with a complete tape the last print IS the curve price and this is
#:                    the correct mark. On ``POP_SMART_MONEY`` it is the conventional
#:                    mark and wrong in a knowable direction - an abandoned mint's last
#:                    print is not a price anyone would have paid.
#: ``dead_is_zero``   book -100%. The pessimistic bound: a position you cannot trade out
#:                    of is worth nothing to us.
#:
#: The truth is inside the bracket and we do not know where. A hold rule is only
#: supportable if it is positive net of costs at BOTH ends of it.
IMPUTATIONS: tuple[str, ...] = ("observed_only", "last_price", "dead_is_zero")


def entry_price(series: Series, t0_ms: int, *, tolerance_ms: int = 2_000) -> float | None:
    """The print at the anchor instant. Tight tolerance: an entry is not an estimate."""
    return series.price_at(t0_ms, tolerance_ms)


def _forward_window_ms(delta_s: int, window_ms: int) -> int:
    """Never let the forward window reach back to the entry. Half the horizon, capped."""
    return max(1, min(window_ms, delta_s * 500))


def _returns_at(
    series: Series,
    t0_ms: int,
    delta_s: int,
    *,
    window_ms: int,
    imputation: str = "observed_only",
    p0: float | None = None,
) -> float | None:
    """Gross return from the anchor to ``t0 + delta``. ``None`` means not measurable."""
    if p0 is None:
        p0 = entry_price(series, t0_ms)
    if p0 is None or p0 <= 0:
        return None
    target = t0_ms + delta_s * 1000
    p1 = series.price_at(
        target, _forward_window_ms(delta_s, window_ms), after_ms=t0_ms
    )
    if p1 is not None and p1 > 0:
        return p1 / p0 - 1.0
    if imputation == "observed_only":
        return None
    if imputation == "dead_is_zero":
        return -1.0
    if imputation == "last_price":
        last = series.last_price_before(target)
        if last is None or last <= 0:
            return None
        return last / p0 - 1.0
    raise ValueError(f"unknown imputation {imputation!r}")


def hold_curve(
    anchors: Sequence[Anchor],
    tape: Mapping[str, Series],
    *,
    grid: Sequence[int] = DEFAULT_GRID_S,
    window_s: int = 60,
    label: str = "all",
    bias: str = "",
    imputation: str = "observed_only",
    cost: CostModel = COST_RENT_RECOVERED,
    cost_rent_lost: CostModel = COST_RENT_LOST,
) -> HoldCurve:
    """Mean forward return as a function of holding time. The whole curve, not a number.

    ``n_eligible`` counts only anchors whose observation could have reached the horizon
    (coverage end for the curve tape, wall clock for the feed). Dropping that and dividing
    by all anchors is the single easiest way to turn "we stopped looking" into "it went
    to zero", or the reverse.
    """
    window_ms = window_s * 1000
    points: list[HoldPoint] = []
    for delta in grid:
        rets: list[float] = []
        eligible = 0
        for a in anchors:
            s = tape.get(a.token)
            if s is None:
                continue
            horizon = a.t0_ms + delta * 1000
            censor = a.censor_ms
            if censor is not None and horizon > censor:
                continue
            eligible += 1
            r = _returns_at(
                s, a.t0_ms, delta, window_ms=window_ms, imputation=imputation
            )
            if r is not None:
                rets.append(r)
        n = len(rets)
        if eligible == 0:
            points.append(HoldPoint(delta, 0, 0, None, None, None, None, None, None, None, None))
            continue
        if n == 0:
            points.append(
                HoldPoint(delta, eligible, 0, 0.0, None, None, None, None, None, None, None)
            )
            continue
        srt = sorted(rets)
        points.append(
            HoldPoint(
                delta_s=delta,
                n_eligible=eligible,
                n_priced=n,
                coverage=round(n / eligible, 4),
                mean_gross_pct=round(100.0 * statistics.fmean(rets), 3),
                median_gross_pct=round(100.0 * statistics.median(rets), 3),
                win_rate_pct=round(100.0 * sum(1 for r in rets if r > 0) / n, 2),
                mean_net_pct=round(100.0 * statistics.fmean([cost.net(r) for r in rets]), 3),
                mean_net_rent_lost_pct=round(
                    100.0 * statistics.fmean([cost_rent_lost.net(r) for r in rets]), 3
                ),
                p25_gross_pct=round(100.0 * srt[int(0.25 * (n - 1))], 3),
                p75_gross_pct=round(100.0 * srt[int(0.75 * (n - 1))], 3),
            )
        )
    scored = [p for p in points if p.mean_net_pct is not None and p.n_priced >= 20]
    peak = max(scored, key=lambda p: p.mean_net_pct) if scored else None
    return HoldCurve(
        population=anchors[0].population if anchors else "empty",
        chain=anchors[0].chain if anchors else "",
        label=label,
        imputation=imputation,
        n_anchors=len(anchors),
        points=points,
        bias=bias,
        peak_delta_s=peak.delta_s if peak else None,
        peak_mean_net_pct=peak.mean_net_pct if peak else None,
    )


# ---------------------------------------------------------------------- survival


@dataclass
class SurvivalPoint:
    delta_s: int
    n_eligible: int
    n_with_price: int
    n_exitable: int
    pct_with_price: float | None
    pct_exitable: float | None


def survival_curve(
    anchors: Sequence[Anchor],
    tape: Mapping[str, Series],
    *,
    grid: Sequence[int] = (300, 1200, 3600),
    window_s: int = 60,
    cost: CostModel = COST_RENT_RECOVERED,
    depth_multiple: float = 10.0,
    sol_usd: float = 118.0,
) -> list[SurvivalPoint]:
    """Is there still a price, and is there still enough traded volume to leave into?

    Two thresholds, neither of them depth. ``pct_with_price`` is "a print exists within
    the window". ``pct_exitable`` additionally requires the window's traded USD volume to
    be at least ``depth_multiple`` times our own ticket - a volume proxy, INVENTED, see
    ``PROVENANCE['exit_depth_multiple']``. A routed sell quote at that instant is what
    would settle it and we do not store one.

    A return you cannot exit into is not a return, so this is the table that decides
    whether "hold the right token" is a strategy available at this end of the market.
    """
    window_ms = window_s * 1000
    ticket_usd = cost.position_lamports / LAMPORTS * sol_usd
    need_usd = depth_multiple * ticket_usd
    out: list[SurvivalPoint] = []
    for delta in grid:
        eligible = priced = exitable = 0
        for a in anchors:
            s = tape.get(a.token)
            if s is None:
                continue
            centre = a.t0_ms + delta * 1000
            if a.censor_ms is not None and centre + window_ms > a.censor_ms:
                continue
            eligible += 1
            if s.price_at(centre, window_ms) is None:
                continue
            priced += 1
            if s.volume_between(centre - window_ms, centre + window_ms) >= need_usd:
                exitable += 1
        out.append(
            SurvivalPoint(
                delta_s=delta,
                n_eligible=eligible,
                n_with_price=priced,
                n_exitable=exitable,
                pct_with_price=round(100.0 * priced / eligible, 2) if eligible else None,
                pct_exitable=round(100.0 * exitable / eligible, 2) if eligible else None,
            )
        )
    return out


# ----------------------------------------------------- entry features and targets


def _auc(pos: Sequence[float], neg: Sequence[float]) -> float | None:
    """Mann-Whitney AUC. 0.5 is no separation. Descriptive, not a significance test."""
    if not pos or not neg:
        return None
    vals = list(pos) + list(neg)
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    r_pos = sum(ranks[: len(pos)])
    n1, n2 = len(pos), len(neg)
    return (r_pos - n1 * (n1 + 1) / 2) / (n1 * n2)


def _spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    n = len(xs)
    if n < 3 or n != len(ys):
        return None

    def rank(v: Sequence[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        out = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    rx, ry = rank(xs), rank(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def tape_features_at(series: Series, t0_ms: int) -> dict[str, float]:
    """Everything observable from the tape strictly at or before the anchor.

    Point-in-time by construction: the filter is on the print timestamp, so there is no
    row here that the agent could not have read at ``t0``. These are the cheap cousins of
    the ``confluence`` variables ``curve_velocity_sol_per_swap``,
    ``bot_dominated_early_activity`` and ``independent_entity_count`` - computed off the
    same rows, without the provider calls those specs assume.
    """
    past = series.window(-1, t0_ms)
    if len(past) < 2:
        return {}
    first, last = past[0], past[-1]
    age_s = max(1.0, (last.ts_ms - first.ts_ms) / 1000.0)
    vol = sum(p.usd for p in past)
    buys = sum(1 for p in past if p.side == "buy")
    return {
        "n_prints_before": float(len(past)),
        "age_s_at_entry": age_s,
        "prints_per_min": 60.0 * len(past) / age_s,
        "usd_volume_before": vol,
        "usd_per_print": vol / len(past),
        "buy_share": buys / len(past),
        "run_up_pct_before": 100.0 * (last.price / first.price - 1.0),
    }


@dataclass
class FeatureResult:
    feature: str
    target: str
    n_fit: int
    n_test: int
    stat_fit: float | None
    stat_test: float | None
    statistic: str
    reading: str


def entry_feature_study(
    anchors: Sequence[Anchor],
    tape: Mapping[str, Series],
    *,
    horizon_s: int = 300,
    window_s: int = 60,
    cost: CostModel = COST_RENT_RECOVERED,
    imputation: str = "observed_only",
    min_n: int = 30,
) -> list[FeatureResult]:
    """Two questions, deliberately not merged.

    **Sign.** Does the feature separate mints whose net return at ``horizon_s`` is
    positive from those whose is not? Statistic: AUC.

    **Duration.** *Among mints that were ever profitable at all*, does the feature
    predict how long the profitable window lasted? Statistic: Spearman rho against
    ``profitable_hold_s``, the last horizon on the grid at which the position was still
    above breakeven.

    Conflating the two is how hold rules get invented: a variable that picks winners tells
    you nothing about when to leave them, and a variable that predicts a long profitable
    window may pick mints that are, on average, losers. Both columns are reported for
    every feature and neither is allowed to stand for the other.
    """
    fine = [15, 30, 60, 90, 120, 180, 240, 300, 450, 600, 900, 1200, 1800, 3600]
    fine = [d for d in fine if d <= horizon_s]
    if not fine:
        fine = [horizon_s]
    window_ms = window_s * 1000
    be = cost.breakeven_gross

    rows: list[dict[str, Any]] = []
    for a in anchors:
        s = tape.get(a.token)
        if s is None:
            continue
        horizon = a.t0_ms + horizon_s * 1000
        if a.censor_ms is not None and horizon > a.censor_ms:
            continue
        feats = tape_features_at(s, a.t0_ms)
        if not feats:
            continue
        r_end = _returns_at(
            s, a.t0_ms, horizon_s, window_ms=window_ms, imputation=imputation
        )
        if r_end is None:
            continue
        profitable_until = 0
        ever = False
        for d in fine:
            r = _returns_at(s, a.t0_ms, d, window_ms=window_ms, imputation=imputation)
            if r is not None and r > be:
                profitable_until = d
                ever = True
        rows.append(
            {
                "t0": a.t0_ms,
                "feats": feats,
                "win": 1.0 if cost.net(r_end) > 0 else 0.0,
                "ever": ever,
                "dur": float(profitable_until),
            }
        )

    if len(rows) < 2 * min_n:
        return [
            FeatureResult(
                feature="(all)",
                target="(all)",
                n_fit=len(rows),
                n_test=0,
                stat_fit=None,
                stat_test=None,
                statistic="none",
                reading=(
                    f"n={len(rows)} usable anchors after point-in-time and censoring "
                    f"filters; below 2x min_n={min_n} no split is worth reporting"
                ),
            )
        ]

    rows.sort(key=lambda r: r["t0"])
    cut = len(rows) // 2
    fit, test = rows[:cut], rows[cut:]
    names = sorted({k for r in rows for k in r["feats"]})
    out: list[FeatureResult] = []
    def sign_auc(
        part: Sequence[Mapping[str, Any]], name: str
    ) -> tuple[float | None, int]:
        pos = [r["feats"][name] for r in part if name in r["feats"] and r["win"] > 0]
        neg = [r["feats"][name] for r in part if name in r["feats"] and r["win"] == 0]
        return _auc(pos, neg), len(pos) + len(neg)

    def dur_rho(
        part: Sequence[Mapping[str, Any]], name: str
    ) -> tuple[float | None, int]:
        sub = [r for r in part if r["ever"] and name in r["feats"]]
        if len(sub) < 3:
            return None, len(sub)
        return (
            _spearman([r["feats"][name] for r in sub], [r["dur"] for r in sub]),
            len(sub),
        )

    for name in names:
        a_fit, n_fit = sign_auc(fit, name)
        a_test, n_test = sign_auc(test, name)
        out.append(
            FeatureResult(
                feature=name,
                target=f"net_return_positive_at_{horizon_s}s",
                n_fit=n_fit,
                n_test=n_test,
                stat_fit=None if a_fit is None else round(a_fit, 4),
                stat_test=None if a_test is None else round(a_test, 4),
                statistic="auc",
                reading=_read_auc(a_fit, a_test),
            )
        )

        d_fit, dn_fit = dur_rho(fit, name)
        d_test, dn_test = dur_rho(test, name)
        out.append(
            FeatureResult(
                feature=name,
                target="profitable_hold_s | ever_profitable",
                n_fit=dn_fit,
                n_test=dn_test,
                stat_fit=None if d_fit is None else round(d_fit, 4),
                stat_test=None if d_test is None else round(d_test, 4),
                statistic="spearman_rho",
                reading=_read_rho(d_fit, d_test),
            )
        )
    return out


def _read_auc(fit: float | None, test: float | None) -> str:
    if fit is None or test is None:
        return "not computable on one of the halves"
    if abs(test - 0.5) < 0.05:
        return "no out-of-sample separation (|AUC-0.5| < 0.05)"
    if (fit - 0.5) * (test - 0.5) <= 0:
        return "sign flips out of sample: in-sample separation did not replicate"
    return "same direction in both halves"


def _read_rho(fit: float | None, test: float | None) -> str:
    if fit is None or test is None:
        return "not computable on one of the halves"
    if abs(test) < 0.1:
        return "no out-of-sample rank association (|rho| < 0.10)"
    if fit * test <= 0:
        return "sign flips out of sample: did not replicate"
    return "same direction in both halves"


# ------------------------------------------------------------- adding to a winner


@dataclass
class AddToWinnerRow:
    check_s: int
    to_s: int
    threshold_pct: float
    n_all: int
    n_winners: int
    mean_fwd_all_pct: float | None
    mean_fwd_winner_pct: float | None
    edge_pp: float | None
    net_fwd_winner_pct: float | None


def add_to_winner(
    anchors: Sequence[Anchor],
    tape: Mapping[str, Series],
    *,
    pairs: Sequence[tuple[int, int]] = ((60, 300), (120, 600), (300, 1200)),
    thresholds: Sequence[float] = (0.0, 0.20, 0.50),
    window_s: int = 60,
    cost: CostModel = COST_RENT_RECOVERED,
    imputation: str = "observed_only",
) -> list[AddToWinnerRow]:
    """Is a mint that is already up a better place for the *next* dollar than average?

    The added dollar pays a fresh round trip, so the test is not "is the forward return
    positive" but "is it above breakeven **and** above the forward return of the
    unconditional mint". Folk rules usually fail the second half.
    """
    window_ms = window_s * 1000
    out: list[AddToWinnerRow] = []
    for check_s, to_s in pairs:
        for thr in thresholds:
            fwd_all: list[float] = []
            fwd_win: list[float] = []
            for a in anchors:
                s = tape.get(a.token)
                if s is None:
                    continue
                if a.censor_ms is not None and a.t0_ms + to_s * 1000 > a.censor_ms:
                    continue
                r1 = _returns_at(
                    s, a.t0_ms, check_s, window_ms=window_ms, imputation=imputation
                )
                if r1 is None:
                    continue
                p1 = s.price_at(
                    a.t0_ms + check_s * 1000,
                    _forward_window_ms(check_s, window_ms),
                    after_ms=a.t0_ms,
                )
                if p1 is None or p1 <= 0:
                    continue
                t2 = a.t0_ms + to_s * 1000
                p2 = s.price_at(t2, _forward_window_ms(to_s, window_ms),
                                after_ms=a.t0_ms + check_s * 1000)
                if p2 is None or p2 <= 0:
                    if imputation == "dead_is_zero":
                        p2 = 0.0
                    elif imputation == "last_price":
                        lp = s.last_price_before(t2)
                        if lp is None or lp <= 0:
                            continue
                        p2 = lp
                    else:
                        continue
                fwd = p2 / p1 - 1.0
                fwd_all.append(fwd)
                if r1 > thr:
                    fwd_win.append(fwd)
            m_all = statistics.fmean(fwd_all) if fwd_all else None
            m_win = statistics.fmean(fwd_win) if fwd_win else None
            out.append(
                AddToWinnerRow(
                    check_s=check_s,
                    to_s=to_s,
                    threshold_pct=round(100.0 * thr, 2),
                    n_all=len(fwd_all),
                    n_winners=len(fwd_win),
                    mean_fwd_all_pct=None if m_all is None else round(100.0 * m_all, 3),
                    mean_fwd_winner_pct=None if m_win is None else round(100.0 * m_win, 3),
                    edge_pp=(
                        None if (m_all is None or m_win is None)
                        else round(100.0 * (m_win - m_all), 3)
                    ),
                    net_fwd_winner_pct=(
                        None if m_win is None else round(100.0 * cost.net(m_win), 3)
                    ),
                )
            )
    return out


# ------------------------------------------------------- the exit-study interaction


def time_stop_grid(
    anchors: Sequence[Anchor],
    tape: Mapping[str, Series],
    *,
    grid: Sequence[int] = DEFAULT_GRID_S,
    window_s: int = 60,
    cost: CostModel = COST_RENT_RECOVERED,
    imputation: str = "observed_only",
    trials: int = 1,
) -> dict[str, Any]:
    """A pure time stop at each horizon: net expectancy, and whether it survives deflation.

    This is the interaction the operator asked to be stated. If the best net expectancy
    on this grid sits at the shortest horizon, then there is no hold decision left to
    make - the exit rule has eaten it - and a hold rule would be an answer to a question
    nobody can act on.
    """
    window_ms = window_s * 1000
    rows: list[dict[str, Any]] = []
    stamped: dict[int, list[tuple[int, float]]] = {}
    for delta in grid:
        pairs: list[tuple[int, float]] = []
        for a in anchors:
            s = tape.get(a.token)
            if s is None:
                continue
            if a.censor_ms is not None and a.t0_ms + delta * 1000 > a.censor_ms:
                continue
            r = _returns_at(
                s, a.t0_ms, delta, window_ms=window_ms, imputation=imputation
            )
            if r is not None:
                pairs.append((a.t0_ms, cost.net(r)))
        stamped[delta] = pairs
        nets = [v for _, v in pairs]
        if len(nets) < 3:
            rows.append({"delta_s": delta, "n": len(nets), "mean_net_pct": None, "dsr": None})
            continue
        dsr, notes = gates.deflated_sharpe(nets, trials)
        sharpe = gates._sharpe_raw(nets)
        rows.append(
            {
                "delta_s": delta,
                "n": len(nets),
                "mean_net_pct": round(100.0 * statistics.fmean(nets), 3),
                "median_net_pct": round(100.0 * statistics.median(nets), 3),
                "sharpe_per_trade": None if sharpe is None else round(sharpe, 4),
                "deflated_sharpe": None if dsr is None else round(dsr, 4),
                "deflation_notes": notes,
            }
        )

    pbo = _pbo_over_horizons(stamped, grid)
    scored = [r for r in rows if r.get("mean_net_pct") is not None and r["n"] >= 20]
    best = max(scored, key=lambda r: r["mean_net_pct"]) if scored else None
    return {
        "imputation": imputation,
        "rows": rows,
        "best_delta_s": best["delta_s"] if best else None,
        "best_mean_net_pct": best["mean_net_pct"] if best else None,
        "pbo_cscv": pbo,
        "note": (
            "each horizon is one configuration; the deflated Sharpe is reported against "
            f"trials={trials} and the PBO is over the horizon grid treated as the "
            "configuration set, which is what gates.pbo_cscv wants"
        ),
    }


def _pbo_over_horizons(
    stamped: Mapping[int, Sequence[tuple[int, float]]],
    grid: Sequence[int],
    *,
    blocks: int = 12,
) -> float | None:
    """Horizons as configurations, calendar blocks as periods, for ``gates.pbo_cscv``.

    Each value carries its own anchor timestamp, so a horizon that lost anchors to the
    censor filter still lands in the right block. Aligning by list index instead would
    quietly shift one configuration's history against another's.
    """
    usable = [d for d in grid if len(stamped.get(d, ())) >= blocks]
    if len(usable) < 2:
        return None
    all_stamps = sorted(ts for d in usable for ts, _ in stamped[d])
    if len(all_stamps) < blocks:
        return None
    lo, hi = all_stamps[0], all_stamps[-1]
    if hi <= lo:
        return None
    width = (hi - lo) / blocks
    per: dict[int, dict[int, list[float]]] = {}
    for d in usable:
        buckets: dict[int, list[float]] = defaultdict(list)
        for ts, v in stamped[d]:
            buckets[min(blocks - 1, int((ts - lo) / width))].append(v)
        per[d] = buckets
    matrix: list[list[float]] = []
    for b in range(blocks):
        row = []
        for d in usable:
            vs = per[d].get(b)
            row.append(statistics.fmean(vs) if vs else 0.0)
        matrix.append(row)
    return gates.pbo_cscv(matrix)


# ------------------------------------------------------------- our own closed book


@dataclass
class RealisedHoldTable:
    """Realised return by realised holding time. **Confounded on purpose - read the field.**"""

    rows: list[dict[str, Any]]
    live_rows: list[dict[str, Any]]
    confounded: bool
    confound: str
    spearman_hold_vs_pnl: float | None
    n: int


REALISED_CONFOUND = (
    "hold_s is an OUTPUT of the exit rule, not an input. Every one of our closed positions "
    "left on a stop: a position that fell hit the -30% stop within two minutes and a "
    "position that rose kept its trailing stop alive for ten, so short holds are losers and "
    "long holds are winners by construction. The rank correlation below is therefore a "
    "measurement of the stop, not of the market, and it cannot be inverted into 'hold "
    "longer' - you do not get to choose which bucket you land in. The only way to break "
    "this confound is a held-out arm with a fixed time exit and no price stop."
)


def realised_hold_vs_pnl(conn: sqlite3.Connection) -> RealisedHoldTable:
    rows = conn.execute(
        "SELECT lane, mode, chain, hold_s, pnl_pct, exit_reason, opened_ms FROM trades "
        "ORDER BY opened_ms"
    ).fetchall()
    buckets = [(0, 60), (60, 120), (120, 180), (180, 300), (300, 600), (600, 10 ** 9)]
    by_bucket: list[dict[str, Any]] = []
    for lo, hi in buckets:
        sel = [r for r in rows if lo <= (r[3] or 0) < hi]
        if not sel:
            by_bucket.append({"hold_s": f"{lo}-{hi}", "n": 0})
            continue
        pnls = [float(r[4]) for r in sel]
        by_bucket.append(
            {
                "hold_s": f"{lo}-{hi if hi < 10 ** 9 else '+'}",
                "n": len(sel),
                "mean_pnl_pct": round(statistics.fmean(pnls), 2),
                "median_pnl_pct": round(statistics.median(pnls), 2),
                "win_pct": round(100.0 * sum(1 for p in pnls if p > 0) / len(pnls), 1),
                "exit_reasons": sorted({str(r[5]).split(":")[0] for r in sel}),
                "modes": sorted({str(r[1]) for r in sel}),
            }
        )
    live = [
        {
            "lane": r[0],
            "chain": r[2],
            "hold_s": r[3],
            "pnl_pct": round(float(r[4]), 2),
            "exit_reason": r[5],
        }
        for r in rows
        if r[1] == "live"
    ]
    rho = _spearman([float(r[3] or 0) for r in rows], [float(r[4]) for r in rows])
    return RealisedHoldTable(
        rows=by_bucket,
        live_rows=live,
        confounded=True,
        confound=REALISED_CONFOUND,
        spearman_hold_vs_pnl=None if rho is None else round(rho, 4),
        n=len(rows),
    )


# ------------------------------------------------------------------- sample sizing


def trade_rate(conn: sqlite3.Connection) -> dict[str, Any]:
    """Closed round trips per day, measured, so a sample size can become a date."""
    row = conn.execute(
        "SELECT COUNT(*), MIN(opened_ms), MAX(closed_ms) FROM trades"
    ).fetchone()
    n, lo, hi = row[0], row[1], row[2]
    live = conn.execute("SELECT COUNT(*) FROM trades WHERE mode='live'").fetchone()[0]
    if not n or lo is None or hi is None or hi <= lo:
        return {"n": n, "per_day": None, "note": "not enough closed trades to rate"}
    days = (hi - lo) / 86_400_000.0
    return {
        "closed_trades": n,
        "live_trades": live,
        "span_days": round(days, 3),
        "all_per_day": round(n / days, 1),
        "live_per_day": round(live / days, 1),
        "note": (
            "measured over the only window in which this book has ever traded; the live "
            "rate is the one that counts because shadow fills are optimistic"
        ),
    }


def sample_needed(
    values: Sequence[float],
    *,
    trials: int = 1,
    targets_pct: Sequence[float] = (1.0, 2.0, 5.0, 10.0),
    per_day: float | None = None,
) -> dict[str, Any]:
    """How many round trips before a mean this size is distinguishable from nothing.

    Two questions, because the honest answer to the first is usually "you cannot".

    1. *Is the observed mean provable?* Defers to
       :func:`kaiba.learning.validation.trades_needed`, which returns ``None`` for a
       non-positive mean on purpose - a sample size for a negative edge is a sentence
       that reads as progress.
    2. *How big would the sample have to be for an edge we would actually want?* For
       each hypothetical per-trade net edge in ``targets_pct``, using the spread we
       actually measured, and converted to days at the measured trade rate. This is the
       number to quote when the answer is "n is too small to say".
    """
    if len(values) < 3:
        return {"n": len(values), "needed": None, "note": "fewer than three observations"}
    from decimal import Decimal

    from kaiba.learning import validation

    mean = Decimal(str(statistics.fmean(values)))
    sd = Decimal(str(statistics.pstdev(values)))
    z = validation.significance_z(trials)
    out: dict[str, Any] = {
        "n": len(values),
        "mean_pct": round(100.0 * float(mean), 4),
        "sd_pct": round(100.0 * float(sd), 4),
        "z_for_trials": z,
        "trials": trials,
        "needed_for_observed_mean": validation.trades_needed(mean, sd, z),
        "source": "kaiba.learning.validation.trades_needed",
    }
    if out["needed_for_observed_mean"] is None:
        out["needed_note"] = (
            "the observed mean is not positive, so there is no edge to size a sample "
            "for; the table below is what a hypothetical edge would cost to prove"
        )
    rows = []
    for t in targets_pct:
        need = validation.trades_needed(Decimal(str(t / 100.0)), sd, z)
        rows.append(
            {
                "target_net_pct_per_trade": t,
                "trades_needed": need,
                "days_at_current_rate": (
                    None if (need is None or not per_day) else round(need / per_day, 1)
                ),
            }
        )
    out["to_prove_a_hypothetical_edge"] = rows
    return out


# ------------------------------------------------------------------------- report


@dataclass
class Report:
    generated_ms: int
    db: str
    cost: dict[str, Any]
    provenance: dict[str, str]
    coverage_facts: dict[str, Any]
    curves: list[dict[str, Any]]
    survival: dict[str, Any]
    features: list[dict[str, Any]]
    add_to_winner: list[dict[str, Any]]
    time_stop: dict[str, Any]
    realised: dict[str, Any]
    sample: dict[str, Any]
    verdicts: list[str]


def coverage_facts(conn: sqlite3.Connection) -> dict[str, Any]:
    """The numbers that decide which questions are answerable. Reported before any curve."""
    out: dict[str, Any] = {}
    row = conn.execute(
        "SELECT COUNT(*), SUM(attempts), MIN(covered_to_ms-covered_from_ms), "
        "MAX(covered_to_ms-covered_from_ms), AVG(covered_to_ms-covered_from_ms) "
        "FROM token_tape WHERE coverage='complete'"
    ).fetchone()
    out["complete_tape_rows"] = row[0]
    out["complete_tape_total_attempts"] = row[1]
    out["complete_tape_window_s"] = {
        "min": None if row[2] is None else row[2] / 1000.0,
        "max": None if row[3] is None else row[3] / 1000.0,
        "mean": None if row[4] is None else round(row[4] / 1000.0, 1),
    }
    out["complete_tape_zero_swap_rows"] = conn.execute(
        "SELECT COUNT(*) FROM token_tape WHERE coverage='complete' AND COALESCE(swaps_total,0)=0"
    ).fetchone()[0]
    out["swaps_by_chain"] = [
        {"chain": c, "n": n, "tokens": t, "from_ms": a, "to_ms": b}
        for c, n, t, a, b in conn.execute(
            "SELECT chain, COUNT(*), COUNT(DISTINCT token), MIN(ts_ms), MAX(ts_ms) "
            "FROM swaps GROUP BY chain"
        )
    ]
    out["live_traded_tokens_with_complete_tape"] = conn.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT t.token FROM trades t "
        "JOIN token_tape tt ON tt.chain=t.chain AND tt.token=t.token "
        "WHERE t.mode='live' AND tt.coverage='complete')"
    ).fetchone()[0]
    out["live_traded_tokens"] = conn.execute(
        "SELECT COUNT(DISTINCT token) FROM trades WHERE mode='live'"
    ).fetchone()[0]
    out["note"] = (
        "the population where a price path is provable (complete pump.fun tape) and the "
        "population we actually trade (gmgn-surfaced, mid-life) barely intersect; "
        "live_traded_tokens_with_complete_tape is the size of that intersection"
    )
    return out


BIAS_LAUNCH = (
    "SELECTION: only mints something already surfaced, so this is not the launch "
    "population and a graduation rate computed on it would be wrong. CENSORING: the "
    "pump.fun tape is a single hot-window fetch (attempts=1 on every complete row, mean "
    "window 49 s), so horizons past the fetch are NOT MEASURABLE - they are excluded by "
    "n_eligible, never booked as zero. Inside the window the path is gap-free, which is "
    "why this is the only population here whose numbers are not a selection artefact."
)

BIAS_SMART_MONEY = (
    "SELECTION, and it runs in BOTH directions, so neither a single bound nor a point "
    "estimate is honest. (a) A print exists only while a tracked wallet trades the mint, "
    "so a mint that went untradeable simply stops appearing: under imputation="
    "'observed_only' it leaves the sample instead of leaving a loss, which makes every "
    "conditional mean an UPPER BOUND and is exactly why the mean return rises as coverage "
    "collapses. (b) The same rule means a mint still liquid but abandoned by the tracked "
    "cohort reads as dead, so the survival column is a LOWER BOUND on whether a market "
    "existed. Read the three imputations as a bracket, never the middle one alone. "
    "Shadow-style optimism also applies: the two live fills of the adjacent lane lost "
    "91.5% and 66.3% against a shadow book that was near break-even."
)


def survival_sensitivity(
    anchors: Sequence[Anchor],
    tape: Mapping[str, Series],
    *,
    grid: Sequence[int],
    cost: CostModel,
    windows_s: Sequence[int] = (30, 60, 120),
) -> dict[str, list[dict[str, Any]]]:
    """The survival table at three window widths, because the width is INVENTED."""
    return {
        f"window_{w}s": [
            asdict(pt)
            for pt in survival_curve(anchors, tape, grid=grid, window_s=w, cost=cost)
        ]
        for w in windows_s
    }


def run(
    conn: sqlite3.Connection,
    *,
    chains: Sequence[str] = ("sol", "bsc", "robinhood"),
    grid: Sequence[int] = DEFAULT_GRID_S,
    window_s: int = 60,
    cost: CostModel = COST_RENT_RECOVERED,
    db_label: str = "",
) -> Report:
    facts = coverage_facts(conn)
    curves: list[dict[str, Any]] = []
    survival: dict[str, Any] = {}
    features: list[dict[str, Any]] = []
    atw: list[dict[str, Any]] = []
    time_stop: dict[str, Any] = {}
    verdicts: list[str] = []
    sample: dict[str, Any] = {}

    # ---- POP_CURVE_LAUNCH, sol only: the only gap-free paths we own.
    launch = anchors_curve_launch(conn, chain="sol")
    tape_pf = load_series(conn, chain="sol", sources=("pumpfun:trades",))
    early, late, _cut = split_by_time(launch)
    for imp in IMPUTATIONS:
        for lbl, part in (("all", launch), ("fit_earlier_half", early),
                          ("test_later_half", late)):
            if not part:
                continue
            curves.append(
                asdict(hold_curve(part, tape_pf, grid=grid, window_s=window_s,
                                  label=lbl, bias=BIAS_LAUNCH, imputation=imp, cost=cost))
            )
    survival["POP_CURVE_LAUNCH_sol"] = survival_sensitivity(
        launch, tape_pf, grid=(60, 300, 1200, 3600), cost=cost
    )
    facts["sol_price_cross_check"] = cross_check_sol_price(
        conn, [a.token for a in launch[:400]]
    )
    for imp in IMPUTATIONS:
        for lbl, part in (("all", launch), ("fit_earlier_half", early),
                          ("test_later_half", late)):
            if not part:
                continue
            time_stop[f"POP_CURVE_LAUNCH_sol::{imp}::{lbl}"] = time_stop_grid(
                part, tape_pf, grid=grid, window_s=window_s, cost=cost,
                imputation=imp, trials=_trials(conn),
            )

    # ---- POP_SMART_MONEY, per chain: the only hour-long paths, and the traded universe.
    for chain in chains:
        sm = anchors_smart_money(conn, chain=chain)
        if len(sm) < 20:
            verdicts.append(f"{chain}: only {len(sm)} smart-money anchors; no curve reported")
            continue
        tape_all = load_series(conn, chain=chain)
        early_sm, late_sm, _ = split_by_time(sm)
        for imp in IMPUTATIONS:
            for lbl, part in ((f"{chain}_all", sm), (f"{chain}_fit_earlier_half", early_sm),
                              (f"{chain}_test_later_half", late_sm)):
                if not part:
                    continue
                curves.append(
                    asdict(hold_curve(part, tape_all, grid=grid, window_s=window_s,
                                      label=lbl, bias=BIAS_SMART_MONEY, imputation=imp,
                                      cost=cost))
                )
        survival[f"POP_SMART_MONEY_{chain}"] = survival_sensitivity(
            sm, tape_all, grid=(300, 1200, 3600), cost=cost
        )
        if chain == "sol":
            for imp in IMPUTATIONS:
                features.extend(
                    {**asdict(f), "imputation": imp}
                    for f in entry_feature_study(
                        sm, tape_all, horizon_s=300, window_s=window_s, cost=cost,
                        imputation=imp,
                    )
                )
                for half, part in (("all", sm), ("fit_earlier_half", early_sm),
                                   ("test_later_half", late_sm)):
                    if not part:
                        continue
                    atw.extend(
                        {**asdict(r), "imputation": imp, "half": half}
                        for r in add_to_winner(part, tape_all, window_s=window_s,
                                               cost=cost, imputation=imp)
                    )
                for lbl, part in (("all", sm), ("fit_earlier_half", early_sm),
                                  ("test_later_half", late_sm)):
                    if not part:
                        continue
                    time_stop[f"POP_SMART_MONEY_sol::{imp}::{lbl}"] = time_stop_grid(
                        part, tape_all, grid=grid, window_s=window_s, cost=cost,
                        imputation=imp, trials=_trials(conn),
                    )
            nets = []
            for a in sm:
                s = tape_all.get(a.token)
                if s is None:
                    continue
                if a.censor_ms is not None and a.t0_ms + 300_000 > a.censor_ms:
                    continue
                r = _returns_at(s, a.t0_ms, 300, window_ms=window_s * 1000,
                                imputation="dead_is_zero")
                if r is not None:
                    nets.append(cost.net(r))
            rate = trade_rate(conn)
            sample = sample_needed(
                nets, trials=_trials(conn), per_day=rate.get("live_per_day") or None
            )
            sample["population"] = "POP_SMART_MONEY sol, 300 s time stop, dead_is_zero"
            sample["trade_rate"] = rate
            realised_pnls = [
                float(v) / 100.0
                for (v,) in conn.execute("SELECT pnl_pct FROM trades")
                if v is not None
            ]
            sample["realised_book"] = sample_needed(
                realised_pnls, trials=_trials(conn),
                per_day=rate.get("live_per_day") or None,
            )
            sample["realised_book"]["population"] = (
                "our own closed trades, live and shadow, as realised"
            )

    realised = realised_hold_vs_pnl(conn)
    return Report(
        generated_ms=int(conn.execute("SELECT MAX(ts_ms) FROM swaps").fetchone()[0] or 0),
        db=db_label,
        cost=cost.describe(),
        provenance=PROVENANCE,
        coverage_facts=facts,
        curves=curves,
        survival=survival,
        features=features,
        add_to_winner=atw,
        time_stop=time_stop,
        realised=asdict(realised),
        sample=sample,
        verdicts=verdicts,
    )


def _trials(conn: sqlite3.Connection) -> int:
    try:
        from kaiba.learning import validation

        return max(1, validation.honest_trials(conn))
    except Exception:
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--window-s", type=int, default=60)
    ap.add_argument("--chains", default="sol,bsc,robinhood")
    ap.add_argument("--rent-lost", action="store_true",
                    help="charge the 0.00204 SOL ATA rent as a cost (the unresolved side)")
    ns = ap.parse_args(list(argv) if argv is not None else sys.argv[1:])
    path = Path(ns.db)
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        rep = run(
            conn,
            chains=tuple(c for c in ns.chains.split(",") if c),
            window_s=ns.window_s,
            cost=COST_RENT_LOST if ns.rent_lost else COST_RENT_RECOVERED,
            db_label=str(path),
        )
    finally:
        conn.close()
    print(json.dumps(asdict(rep), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
