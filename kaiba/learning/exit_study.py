"""Did we sell at the right time? Measured against our own tape, not against a theory.

Every live round trip this agent has ever completed exited on a stop and lost money. The
standing explanation was that ``bounds.max_slippage_bps = 2500`` lets the exit fill into a
falling book, so three of four ``sm-trenches`` fills realised worse than the declared -30%.
This module exists to check that explanation, and it does not survive contact with the
order rows.

**What this module measures, and why each piece is shaped the way it is.**

*The exit gap, decomposed.* ``kaiba.execution.watchdog._min_out`` computes the sell floor
as ``qty * mark_price / native_price * (1 - max_slippage_bps/10_000)``. That makes
``min_out / (1 - slip)`` an exact, recorded, after-the-fact readout of **the watchdog's own
valuation of the position at the instant it pressed send** — the same ``PriceQuote`` object
that decided the breach. So every exit gives three numbers that need no reconstruction:
the declared stop, the mark at send, and the realised fill. The difference between the
first two is evaluation lag; between the second and third is the fill. See
:func:`exit_gaps`. This is the load-bearing trick in the module and it is why nothing here
has to guess at slippage.

*Counterfactual policies, replayed on the tape.* ``position_marks`` cannot do this job:
measured 2026-09-22 on the live box it holds **88 rows for 72 positions, zero of them for
any of the six live positions**, and where two marks exist at all the median gap between
them is 115.9 s against a declared ``protection.poll_interval_s: 5``. It is an occasional
snapshot, not a price series. So :func:`load_paths` builds the path from ``swaps`` — third
party prints, independent of our own broker — and every policy in :data:`POLICIES` is
replayed against that same path so the comparison is internally consistent.

*Censoring is a first-class outcome, never a return.* Our ingestion watches a token while
we hold it and stops shortly after we close. Measured: the tape runs a median 368 s past
the close, and only 3 of 57 replayable positions have a full 60 minutes of forward prices.
A policy that would still be holding when the prints stop has **no measured outcome**, and
:class:`PolicyResult` says ``censored=True`` rather than pretending the last print was an
exit. :class:`PolicySummary` carries the censored count next to the mean so a reader cannot
miss it. A hold-to-+60min "control" over this tape is 95% fabrication and is reported as
such.

*In-sample and out-of-sample, always paired.* :class:`PolicyReport` has no way to express
an in-sample number on its own — the field is a pair. The split is by time, at the median
open, and the whole corpus spans about 15.5 hours, which is one session and one regime.
That is stated in :attr:`ExitStudy.caveats` on every run because it is the single largest
reason to distrust anything below.

**Provenance.** Every constant here carries MEASURED or INVENTED in
:data:`CONSTANT_PROVENANCE`, and ``tests/test_exit_study.py`` fails if one does not.

This module reads. It never writes, never touches config, and never changes lane
behaviour. It is evidence for the operator, not a control loop.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from kaiba.learning import gates

# --------------------------------------------------------------------------------------
# constants, each with its provenance
# --------------------------------------------------------------------------------------

#: One leg of a GMGN round trip, as a fraction. MEASURED: GMGN's router takes 1% of every
#: swap, confirmed on the live fills. Gas on Solana adds ~0.00005 SOL per leg, which on a
#: 0.056 SOL position is a further ~0.09%; ATA rent of 0.00204 SOL is recoverable only if
#: the exit closes the account and is deliberately NOT charged here, because whether it is
#: recovered is unresolved and charging an unresolved cost would make every policy look
#: worse than the evidence supports.
LEG_COST_PCT = 1.09

#: Round trip, both legs, as a fraction of notional. DERIVED from :data:`LEG_COST_PCT`.
#: Applied multiplicatively in :func:`_net`, so a policy that exits at the entry price
#: returns this much less than zero.
ROUND_TRIP_COST_PCT = 2.18

#: The stop the operator declared, in percent. MEASURED from ``config/risk.yaml``
#: ``protection.stop_loss_bps: 3000`` on 2026-09-22. Read as a number, not enforced here.
DECLARED_STOP_PCT = -30.0

#: The emergency exit, in percent. MEASURED from ``config/risk.yaml``
#: ``protection.emergency_loss_bps: 5000``. It matters here because two of the six live
#: exits closed with ``exit_reason='emergency_loss'``, and scoring those against the -30%
#: stop would charge them an evaluation gap they were never asked to meet.
DECLARED_EMERGENCY_PCT = -50.0

#: Exit reason prefix to the threshold it was actually judged against. STRUCTURAL: it
#: mirrors the precedence in ``kaiba.execution.protection`` (rug > emergency > stop). A
#: rug exit has no declared price threshold at all — the trigger is a liquidity drop — so
#: it maps to ``None`` and is reported without an evaluation gap rather than with a
#: made-up one.
EXIT_REASON_THRESHOLD_PCT: dict[str, float | None] = {
    "rug": None,
    "emergency_loss": DECLARED_EMERGENCY_PCT,
    "stop_loss": DECLARED_STOP_PCT,
    "trailing_stop": None,
}

#: Exit tolerance the sell orders actually carried. MEASURED from the ``orders`` rows:
#: all six live sells and all 88 shadow sells recorded their own ``slippage_bps``, and the
#: live ones were 2500. Used to invert ``min_out`` back into the watchdog's mark.
DECLARED_EXIT_SLIPPAGE_BPS = 2500

#: Declared watchdog cadence, seconds. MEASURED from ``config/risk.yaml``
#: ``protection.poll_interval_s: 5``. Used only as the retry interval in
#: :func:`slippage_band_scenarios`; it is what the config says, not what was observed.
DECLARED_POLL_INTERVAL_S = 5

#: Minimum tape prints inside the replay window before a position is replayable at all.
#: INVENTED. Three is the fewest that can show a direction and a reversal. It is not
#: derived from anything; a different number would change which positions qualify, and
#: :attr:`ExitStudy.eligibility` reports how many were dropped so the choice is visible.
MIN_PATH_POINTS = 3

#: How far past the open the replay looks, milliseconds. INVENTED as "one hour", to match
#: the hold-to-+60min control the operator asked for. MEASURED consequence: only 3 of 57
#: replayable positions have prints that far out, so the horizon mostly binds as censoring.
REPLAY_HORIZON_MS = 3_600_000

#: A tape print older than this is not allowed to stand in for "the price now" when a time
#: stop or a horizon fires. INVENTED at 120 s. Without it a sparse tape lets a time-30s
#: policy silently exit at a print four minutes late and book it as a 30-second trade.
MAX_PRINT_STALENESS_MS = 120_000

#: Blocks for the CSCV procedure in :func:`gates.pbo_cscv`. INVENTED at 8: the default the
#: gate ships with, and the largest even split that leaves >=7 positions per block at n=57.
CSCV_SPLITS = 8

CONSTANT_PROVENANCE: dict[str, str] = {
    "LEG_COST_PCT": (
        "MEASURED - GMGN takes 1% per leg (confirmed on the live fills); +0.09% gas on a "
        "0.056 SOL position. ATA rent 0.00204 SOL is UNRESOLVED and deliberately excluded."
    ),
    "ROUND_TRIP_COST_PCT": "DERIVED - two legs of LEG_COST_PCT.",
    "DECLARED_STOP_PCT": "MEASURED - config/risk.yaml protection.stop_loss_bps: 3000, read 2026-09-22.",
    "DECLARED_EMERGENCY_PCT": (
        "MEASURED - config/risk.yaml protection.emergency_loss_bps: 5000, read 2026-09-22."
    ),
    "EXIT_REASON_THRESHOLD_PCT": (
        "STRUCTURAL - mirrors the precedence in kaiba.execution.protection (rug > emergency > "
        "stop > ladder). A rug or trailing exit has no declared price level, so it maps to None."
    ),
    "DECLARED_EXIT_SLIPPAGE_BPS": "MEASURED - orders.slippage_bps on all six live sells.",
    "DECLARED_POLL_INTERVAL_S": "MEASURED - config/risk.yaml protection.poll_interval_s: 5.",
    "MIN_PATH_POINTS": "INVENTED - three prints is the fewest that can show a reversal. Settled by nothing.",
    "REPLAY_HORIZON_MS": "INVENTED - one hour, to match the requested hold-to-+60min control.",
    "MAX_PRINT_STALENESS_MS": "INVENTED - 120 s; stops a sparse tape from faking a punctual exit.",
    "CSCV_SPLITS": "INVENTED - gates.DEFAULT_SPLITS, the largest even split leaving >=7 rows per block.",
    "FIRST_TP_RUNG_PCT": (
        "MEASURED - config/risk.yaml protection.tp_ladder first rung is [2.0, 50], i.e. sell 50% at "
        "a 2.0x multiple = +100% gain. Read 2026-09-22."
    ),
    "WORST_MEASURED_NON_FILL_PP": (
        "MEASURED - pos_live_3d59670ebf16: sell ord:267e1f3860c2747f1124 was refused before send, the "
        "position rode 814 s further down, and the eventual fill was 48.7pp below the refused mark."
    ),
}

#: The first rung of the shipped take-profit ladder, in percent gain. See provenance above.
FIRST_TP_RUNG_PCT = 100.0


# --------------------------------------------------------------------------------------
# the path
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PricePath:
    """A position's forward price history, expressed as a gain multiple on entry.

    ``points`` is ``(offset_ms_from_open, gain)`` where ``gain`` is
    ``tape_price / entry_price`` — so ``1.0`` is flat and ``0.7`` is the declared stop.
    Working in multiples rather than USD keeps the SOL/USD leg out of the arithmetic; over
    a sub-hour hold that leg moves well under 1% while these tokens move tens of percent,
    but it is one fewer thing to be wrong about.

    ``last_point_ms`` is the censoring horizon. Anything a policy would do after it is not
    measured, and callers must not treat the final point as an exit price.
    """

    position_id: str
    token: str
    lane: str
    mode: str
    opened_ms: int
    closed_ms: int
    points: tuple[tuple[int, float], ...]
    #: Offset of the last print, milliseconds from open. The end of measurable time.
    last_point_ms: int
    #: Realised fraction of cost recovered, ``proceeds_native / cost_native``. ``None``
    #: when either side is missing. For live rows this is money that actually moved.
    realised_gross: float | None

    def at(self, offset_ms: int) -> tuple[int, float] | None:
        """Latest print at or before ``offset_ms``, or ``None`` if it would be too stale."""
        latest: tuple[int, float] | None = None
        for t, g in self.points:
            if t <= offset_ms:
                latest = (t, g)
            else:
                break
        if latest is None:
            return None
        if offset_ms - latest[0] > MAX_PRINT_STALENESS_MS:
            return None
        return latest

    def peak_before(self, offset_ms: int) -> float | None:
        vals = [g for t, g in self.points if t <= offset_ms]
        return max(vals) if vals else None


def _dec(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d


def load_paths(
    conn: sqlite3.Connection,
    *,
    horizon_ms: int = REPLAY_HORIZON_MS,
    min_points: int = MIN_PATH_POINTS,
) -> tuple[list[PricePath], dict[str, int]]:
    """Every closed position that can be replayed, plus a census of the ones that cannot.

    The census is the point. Dropping unreplayable positions silently is how a study of
    "our exits" quietly becomes a study of "our exits on tokens that kept trading", which
    is a different and much more flattering question. The returned counts let the caller
    state the selection out loud, and :class:`ExitStudy` does.
    """
    census = {"closed": 0, "no_entry_price": 0, "too_few_prints": 0, "replayable": 0}
    out: list[PricePath] = []
    rows = conn.execute(
        "SELECT position_id, chain, token, lane, mode, opened_ms, closed_ms, "
        "       entry_price_usd, cost_native, proceeds_native "
        "FROM positions WHERE closed_ms IS NOT NULL ORDER BY opened_ms"
    ).fetchall()
    for row in rows:
        census["closed"] += 1
        entry = _dec(row["entry_price_usd"])
        if entry is None or entry <= 0:
            census["no_entry_price"] += 1
            continue
        opened = int(row["opened_ms"])
        # chain=? lets idx_swaps_token (chain, token, ts_ms) seek; without it every
        # position walked the whole swaps table (MEASURED 2026-09-29: 744-894 s a run).
        prints = conn.execute(
            "SELECT ts_ms, price_usd FROM swaps "
            "WHERE chain=? AND token=? AND price_usd IS NOT NULL AND ts_ms>=? AND ts_ms<=? ORDER BY ts_ms",
            (row["chain"], row["token"], opened, opened + horizon_ms),
        ).fetchall()
        points: list[tuple[int, float]] = []
        for p in prints:
            px = _dec(p["price_usd"])
            if px is None or px <= 0:
                continue
            points.append((int(p["ts_ms"]) - opened, float(px / entry)))
        if len(points) < min_points:
            census["too_few_prints"] += 1
            continue
        cost = _dec(row["cost_native"])
        proceeds = _dec(row["proceeds_native"])
        realised = float(proceeds / cost) if cost and cost > 0 and proceeds is not None else None
        census["replayable"] += 1
        out.append(
            PricePath(
                position_id=row["position_id"],
                token=row["token"],
                lane=row["lane"],
                mode=row["mode"],
                opened_ms=opened,
                closed_ms=int(row["closed_ms"]),
                points=tuple(points),
                last_point_ms=points[-1][0],
                realised_gross=realised,
            )
        )
    return out, census


def load_decision_paths(
    conn: sqlite3.Connection,
    *,
    actions: Sequence[str] = ("enter",),
    horizon_ms: int = REPLAY_HORIZON_MS,
    min_points: int = MIN_PATH_POINTS,
    max_anchor_lag_ms: int = MAX_PRINT_STALENESS_MS,
) -> tuple[list[PricePath], dict[str, int]]:
    """Replayable paths for tokens we *decided* on, whether or not a fill followed.

    This exists because the binding constraint on every number in this module is n, and
    six live round trips will not become sixty by waiting. Measured on the live box
    2026-09-22: 87 ``enter`` decisions and 934 ``skip`` decisions, of which 677 tokens
    already carry twenty or more priced tape prints. That is one to two orders of
    magnitude more evidence than the position table holds, available now, for free.

    Two rules keep it honest.

    *No look-ahead in the anchor.* The entry price is the **first tape print at or after
    the decision timestamp**, not a dossier price. ``token_dossiers`` keeps one row per
    token with the latest ``built_at_ms``, so anchoring on it would price a decision with
    information that did not exist yet. A print that lands more than
    ``max_anchor_lag_ms`` after the decision is refused rather than stretched.

    *A decision is not a fill.* The anchor is a stranger's trade at our size-free price,
    so these paths carry none of our own entry impact and none of the latency between
    deciding and filling — both of which the live book shows are real. Returns from this
    cohort are therefore an **upper bound** on what we would have achieved, and they are
    for comparing exit policies against each other, not for forecasting P&L.

    Passing ``actions=("skip",)`` answers a different question again: what an exit policy
    would do on tokens our entry filter rejected. That cohort is not ours and must be
    labelled separately, never pooled with the ``enter`` one.
    """
    census = {"decisions": 0, "no_anchor": 0, "too_few_prints": 0, "replayable": 0}
    out: list[PricePath] = []
    placeholders = ",".join("?" * len(actions))
    rows = conn.execute(
        f"SELECT decision_id, chain, token, lane, mode, ts_ms FROM decisions WHERE action IN ({placeholders}) "
        "ORDER BY ts_ms",
        tuple(actions),
    ).fetchall()
    for row in rows:
        census["decisions"] += 1
        t0 = int(row["ts_ms"])
        prints = conn.execute(
            "SELECT ts_ms, price_usd FROM swaps WHERE chain=? AND token=? AND price_usd IS NOT NULL "
            "AND ts_ms>=? AND ts_ms<=? ORDER BY ts_ms",
            (row["chain"], row["token"], t0, t0 + horizon_ms),
        ).fetchall()
        anchor: Decimal | None = None
        points: list[tuple[int, float]] = []
        for p in prints:
            px = _dec(p["price_usd"])
            if px is None or px <= 0:
                continue
            if anchor is None:
                if int(p["ts_ms"]) - t0 > max_anchor_lag_ms:
                    break
                anchor = px
            points.append((int(p["ts_ms"]) - t0, float(px / anchor)))
        if anchor is None:
            census["no_anchor"] += 1
            continue
        if len(points) < min_points:
            census["too_few_prints"] += 1
            continue
        census["replayable"] += 1
        out.append(
            PricePath(
                position_id=row["decision_id"],
                token=row["token"],
                lane=row["lane"],
                mode=row["mode"],
                opened_ms=t0,
                closed_ms=t0 + points[-1][0],
                points=tuple(points),
                last_point_ms=points[-1][0],
                realised_gross=None,
            )
        )
    return out, census


# --------------------------------------------------------------------------------------
# policies
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyResult:
    """What one exit policy would have done to one position.

    ``censored`` is not a detail. It means the tape ran out before the policy fired, so
    ``net_return_pct`` is what you would have got *if you had been forced to sell at the
    last print we happen to hold*, which is not the policy's outcome and must not be
    averaged in as though it were. Every summary counts these separately.
    """

    net_return_pct: float
    exit_offset_ms: int
    reason: str
    censored: bool


def _net(gain: float, cost_pct: float = ROUND_TRIP_COST_PCT) -> float:
    """Gross gain multiple to net percentage return, both legs charged."""
    return (gain * (1.0 - cost_pct / 100.0) - 1.0) * 100.0


def _terminal(path: PricePath, cost_pct: float, reason: str) -> PolicyResult:
    t, g = path.points[-1]
    return PolicyResult(_net(g, cost_pct), t, reason, censored=True)


def fixed_stop(path: PricePath, stop_pct: float, *, cost_pct: float = ROUND_TRIP_COST_PCT) -> PolicyResult:
    """Sell on the first print at or below ``stop_pct``.

    The fill is the breaching print itself. That is optimistic by exactly the amount of
    evaluation lag the live book actually shows (:func:`exit_gaps` measures it), so a stop
    replayed here will look better than the same stop did in production. Said plainly
    rather than corrected for, because correcting for it with n=6 would be modelling.
    """
    for t, g in path.points:
        if (g - 1.0) * 100.0 <= stop_pct:
            return PolicyResult(_net(g, cost_pct), t, f"stop{stop_pct:g}", censored=False)
    return _terminal(path, cost_pct, f"stop{stop_pct:g}")


def time_stop(path: PricePath, seconds: float, *, cost_pct: float = ROUND_TRIP_COST_PCT) -> PolicyResult:
    """Sell at the first print strictly after ``seconds``, regardless of price.

    Refuses a print that is more than :data:`MAX_PRINT_STALENESS_MS` late, because on a
    sparse tape the "first print after 30 s" can be four minutes old and booking it as a
    30-second trade is the cheapest way to manufacture an edge in this whole module.
    """
    target = int(seconds * 1000)
    for t, g in path.points:
        if t > target:
            if t - target > MAX_PRINT_STALENESS_MS:
                return _terminal(path, cost_pct, f"time{seconds:g}s:stale")
            return PolicyResult(_net(g, cost_pct), t, f"time{seconds:g}s", censored=False)
    return _terminal(path, cost_pct, f"time{seconds:g}s")


def trailing_stop(
    path: PricePath, drop_pct: float, *, cost_pct: float = ROUND_TRIP_COST_PCT
) -> PolicyResult:
    """Sell when price falls ``drop_pct`` below the running peak, peak seeded at entry."""
    peak = 1.0
    for t, g in path.points:
        peak = max(peak, g)
        if g <= peak * (1.0 + drop_pct / 100.0):
            return PolicyResult(_net(g, cost_pct), t, f"trail{drop_pct:g}", censored=False)
    return _terminal(path, cost_pct, f"trail{drop_pct:g}")


def take_profit_ladder(
    path: PricePath,
    rungs: Sequence[tuple[float, float]],
    *,
    stop_pct: float = DECLARED_STOP_PCT,
    cost_pct: float = ROUND_TRIP_COST_PCT,
) -> PolicyResult:
    """Scale out on ``(gain_pct, fraction)`` rungs, with the remainder on a stop.

    Each rung sells its fraction of the ORIGINAL size at the first print at or above its
    gain. Cost is charged once on the blended proceeds, which slightly flatters a ladder
    against a single exit (three partial sells pay three lots of gas). With gas at ~0.09%
    of position that understates the ladder's cost by roughly 0.2pp; the ladders lose by
    far more than that, so the conclusion does not turn on it.
    """
    remaining = 1.0
    accrued = 0.0
    fired = [False] * len(rungs)
    for t, g in path.points:
        for i, (gain_pct, frac) in enumerate(rungs):
            if not fired[i] and (g - 1.0) * 100.0 >= gain_pct and remaining > 0:
                take = min(frac, remaining)
                accrued += take * g
                remaining -= take
                fired[i] = True
        if remaining <= 1e-9:
            return PolicyResult(_net(accrued, cost_pct), t, "ladder:complete", censored=False)
        if (g - 1.0) * 100.0 <= stop_pct:
            accrued += remaining * g
            return PolicyResult(_net(accrued, cost_pct), t, "ladder:stop", censored=False)
    accrued += remaining * path.points[-1][1]
    return PolicyResult(_net(accrued, cost_pct), path.points[-1][0], "ladder", censored=True)


def hold_to(path: PricePath, seconds: float, *, cost_pct: float = ROUND_TRIP_COST_PCT) -> PolicyResult:
    """Do nothing until ``seconds``, then sell. Censored whenever the tape ends first."""
    target = int(seconds * 1000)
    if path.last_point_ms < target:
        return _terminal(path, cost_pct, f"hold{seconds:g}s")
    mark = path.at(target)
    if mark is None:
        return _terminal(path, cost_pct, f"hold{seconds:g}s:stale")
    return PolicyResult(_net(mark[1], cost_pct), mark[0], f"hold{seconds:g}s", censored=False)


Policy = Callable[[PricePath], PolicyResult]

#: The comparison set. Deliberately includes the incumbent (``stop-30``) and the
#: do-nothing control, because a study that only compares candidates to each other cannot
#: say whether any of them beats leaving the thing alone.
POLICIES: dict[str, Policy] = {
    "stop-10": lambda p: fixed_stop(p, -10.0),
    "stop-15": lambda p: fixed_stop(p, -15.0),
    "stop-20": lambda p: fixed_stop(p, -20.0),
    "stop-30(incumbent)": lambda p: fixed_stop(p, -30.0),
    "time-30s": lambda p: time_stop(p, 30),
    "time-60s": lambda p: time_stop(p, 60),
    "time-180s": lambda p: time_stop(p, 180),
    "trail-10": lambda p: trailing_stop(p, -10.0),
    "trail-20": lambda p: trailing_stop(p, -20.0),
    "trail-30": lambda p: trailing_stop(p, -30.0),
    "tp-25/50/100": lambda p: take_profit_ladder(p, [(25.0, 1 / 3), (50.0, 1 / 3), (100.0, 1 / 3)]),
    "tp-25-all": lambda p: take_profit_ladder(p, [(25.0, 1.0)]),
    "hold-10min": lambda p: hold_to(p, 600),
    "hold-60min(control)": lambda p: hold_to(p, 3600),
}


# --------------------------------------------------------------------------------------
# summaries, always in pairs
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicySummary:
    n: int
    censored: int
    mean_pct: float | None
    median_pct: float | None
    win_rate_pct: float | None
    worst_pct: float | None
    best_pct: float | None

    @property
    def uncensored(self) -> int:
        return self.n - self.censored


def summarise(results: Iterable[PolicyResult]) -> PolicySummary:
    rows = list(results)
    if not rows:
        return PolicySummary(0, 0, None, None, None, None, None)
    vals = [r.net_return_pct for r in rows]
    return PolicySummary(
        n=len(rows),
        censored=sum(1 for r in rows if r.censored),
        mean_pct=statistics.mean(vals),
        median_pct=statistics.median(vals),
        win_rate_pct=100.0 * sum(1 for v in vals if v > 0) / len(vals),
        worst_pct=min(vals),
        best_pct=max(vals),
    )


@dataclass(frozen=True)
class PolicyReport:
    """One policy's numbers. There is no way to hold an in-sample figure alone.

    The repository rule is that an in-sample number without its out-of-sample twin is not
    a finding. This dataclass enforces it structurally: ``in_sample`` and ``out_of_sample``
    are both required, so a caller cannot construct half a result and report it.
    """

    name: str
    overall: PolicySummary
    in_sample: PolicySummary
    out_of_sample: PolicySummary
    live_only: PolicySummary
    #: Deflated Sharpe over the whole series, deflated by the number of policies tried.
    dsr_overall: float | None
    dsr_out_of_sample: float | None
    dsr_notes: tuple[str, ...]

    @property
    def replicated(self) -> bool:
        """True only when the out-of-sample half is positive net of costs AND the DSR
        clears 0.95. Both, because a positive mean on 28 observations is nothing."""
        oos = self.out_of_sample.mean_pct
        return bool(oos is not None and oos > 0 and (self.dsr_out_of_sample or 0.0) >= 0.95)


# --------------------------------------------------------------------------------------
# the exit gap, decomposed
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExitGap:
    """Declared stop versus mark-at-send versus realised fill, for one exit.

    Where a price threshold applies at all, the three components sum by construction to
    the whole miss:

        ``realised - declared_stop = evaluation_gap_pp + fill_gap_pp``

    A rug exit or a trailing exit has no declared price level to miss, so
    ``threshold_applies`` is ``False``, the evaluation term is zero and the identity does
    not hold. That is the correct behaviour: scoring a liquidity-triggered exit against
    the -30% stop would invent a gap the exit was never asked to close.

    ``evaluation_gap_pp`` is how far past the stop the position had already fallen *on the
    watchdog's own mark* by the time it pressed send. ``fill_gap_pp`` is what the fill did
    relative to that mark. ``fill_over_mark`` is the same thing as a ratio, which is the
    form the slippage band is written in, so :func:`slippage_band_scenarios` can use it
    directly.

    A negative ``fill_gap_pp`` is the "fills into a falling book" story. A positive one
    means the mark was too low and we exited a position that was not as dead as we thought.
    """

    position_id: str
    lane: str
    mode: str
    order_id: str
    #: The threshold this exit was actually judged against, chosen from the position's own
    #: ``exit_reason`` via :data:`EXIT_REASON_THRESHOLD_PCT`. ``None`` for a rug or a
    #: trailing exit, which have no declared price level, and in that case
    #: ``evaluation_gap_pp`` is ``0.0`` and means "not applicable", not "no gap".
    declared_stop_pct: float
    exit_reason: str
    threshold_applies: bool
    mark_at_send_pct: float
    realised_pct: float
    evaluation_gap_pp: float
    fill_gap_pp: float
    fill_over_mark: float
    send_lag_s: float
    submit_lag_s: float
    #: Sell orders on this position that never filled, and why. One of these cost 48.7pp.
    failed_attempts: tuple[tuple[str, str], ...]


def exit_gaps(conn: sqlite3.Connection, *, mode: str | None = "live") -> list[ExitGap]:
    """Decompose every closed position's exit. ``mode=None`` for shadow rows too.

    Inverting ``min_out`` back through the order's own ``slippage_bps`` recovers the mark
    the watchdog held at send. That inversion is exact only because
    ``watchdog._min_out`` and ``executor.build_order`` are required to write the *same*
    band into both the floor and the order row — the engine's docstring calls disagreeing
    there a lie, and this function is the thing that would notice if they ever did.
    """
    out: list[ExitGap] = []
    where = "WHERE closed_ms IS NOT NULL" + ("" if mode is None else " AND mode=?")
    args: tuple[object, ...] = () if mode is None else (mode,)
    for row in conn.execute(
        "SELECT position_id, token, lane, mode, opened_ms, closed_ms, cost_native, proceeds_native, "
        "       exit_reason "
        f"FROM positions {where} ORDER BY opened_ms",
        args,
    ).fetchall():
        cost = _dec(row["cost_native"])
        proceeds = _dec(row["proceeds_native"])
        if not cost or cost <= 0 or proceeds is None:
            continue
        sells = conn.execute(
            "SELECT order_id, min_out, filled_out, slippage_bps, state, error, created_ms, updated_ms "
            "FROM orders WHERE token=? AND side='sell' AND mode=? AND created_ms>=? AND created_ms<=? "
            "ORDER BY created_ms",
            (row["token"], row["mode"], row["opened_ms"], int(row["closed_ms"]) + 5_000),
        ).fetchall()
        filled = [s for s in sells if s["state"] == "filled" and s["filled_out"] is not None]
        if not filled:
            continue
        last = filled[-1]
        band = int(last["slippage_bps"] or DECLARED_EXIT_SLIPPAGE_BPS)
        min_out = _dec(last["min_out"])
        if min_out is None or min_out <= 0 or not 0 <= band < 10_000:
            continue
        mark = min_out / (Decimal(10_000 - band) / Decimal(10_000))
        realised_pct = float(proceeds / cost - 1) * 100.0
        mark_pct = float(mark / cost - 1) * 100.0
        failures = tuple(
            (s["order_id"], (s["error"] or "")[:200]) for s in sells if s["state"] != "filled"
        )
        reason = str(row["exit_reason"] or "")
        threshold = next(
            (v for k, v in EXIT_REASON_THRESHOLD_PCT.items() if reason.startswith(k)),
            DECLARED_STOP_PCT,
        )
        out.append(
            ExitGap(
                position_id=row["position_id"],
                lane=row["lane"],
                mode=row["mode"],
                order_id=last["order_id"],
                declared_stop_pct=DECLARED_STOP_PCT if threshold is None else threshold,
                exit_reason=reason,
                threshold_applies=threshold is not None,
                mark_at_send_pct=mark_pct,
                realised_pct=realised_pct,
                evaluation_gap_pp=0.0 if threshold is None else mark_pct - threshold,
                fill_gap_pp=realised_pct - mark_pct,
                fill_over_mark=float(_dec(last["filled_out"]) / mark),  # type: ignore[arg-type]
                send_lag_s=(int(last["created_ms"]) - int(row["opened_ms"])) / 1000.0,
                submit_lag_s=(int(last["updated_ms"]) - int(last["created_ms"])) / 1000.0,
                failed_attempts=failures,
            )
        )
    return out


@dataclass(frozen=True)
class GapVerdict:
    """Which component dominates, and whether n can support saying so."""

    n: int
    mean_evaluation_gap_pp: float | None
    mean_fill_gap_pp: float | None
    mean_fill_over_mark: float | None
    fill_over_mark_ci95: tuple[float, float] | None
    dominant: str
    #: True only when the bootstrap CI on ``fill_over_mark`` excludes 1.0, i.e. when the
    #: tape can actually distinguish "the fill was bad" from "the fill was noise".
    fill_effect_separable: bool


def gap_verdict(gaps: Sequence[ExitGap], *, draws: int = 20_000, seed: int = 7) -> GapVerdict:
    """Bootstrap the fill component, because with six exits the mean is nearly meaningless.

    The bootstrap is over positions, not over fills, so a position with two sell attempts
    still counts once. ``fill_effect_separable`` is the whole answer to "which dominates":
    if the interval straddles 1.0 the honest reading is that the fill component cannot be
    told apart from zero, and whatever the point estimate says, evaluation lag is the only
    component we can see.
    """
    if not gaps:
        return GapVerdict(0, None, None, None, None, "no data", False)
    ratios = [g.fill_over_mark for g in gaps]
    with_threshold = [g for g in gaps if g.threshold_applies]
    ev = statistics.mean(g.evaluation_gap_pp for g in with_threshold) if with_threshold else 0.0
    fg = statistics.mean(g.fill_gap_pp for g in gaps)
    ci: tuple[float, float] | None = None
    if len(ratios) >= 3:
        rng = _Lcg(seed)
        means = sorted(
            statistics.mean(ratios[rng.below(len(ratios))] for _ in ratios) for _ in range(draws)
        )
        lo = means[int(0.025 * draws)]
        hi = means[min(draws - 1, int(0.975 * draws))]
        ci = (lo, hi)
    separable = bool(ci and (ci[0] > 1.0 or ci[1] < 1.0))
    dominant = "evaluation" if abs(ev) >= abs(fg) else "fill"
    if not separable:
        dominant += " (fill component not separable from noise at this n)"
    return GapVerdict(len(gaps), ev, fg, statistics.mean(ratios), ci, dominant, separable)


class _Lcg:
    """A tiny deterministic PRNG, so a bootstrap reported to the operator on Monday is the
    same one they can reproduce on Tuesday without pinning the interpreter's Mersenne
    state. Numerical recipes constants; adequate for resampling six numbers."""

    def __init__(self, seed: int) -> None:
        self.state = (seed * 6_364_136_223_846_793_005 + 1_442_695_040_888_963_407) & ((1 << 64) - 1)

    def below(self, n: int) -> int:
        self.state = (self.state * 6_364_136_223_846_793_005 + 1_442_695_040_888_963_407) & ((1 << 64) - 1)
        return (self.state >> 33) % n


# --------------------------------------------------------------------------------------
# favourable / adverse excursion
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExcursionReport:
    """How far up our positions went before they died — the number that decides whether a
    take-profit or a better stop is worth more.

    Two independent sources, because they disagree and the disagreement matters.
    ``peak_*`` comes from ``positions.peak_price_usd``, which the watchdog maintained.
    ``tape_*`` comes from third-party prints inside the hold. ``marks_usable`` is the
    ``position_marks`` answer and is reported so nobody proposes it again: it is zero for
    live positions.
    """

    n: int
    marks_rows: int
    marks_for_live: int
    peak_mfe_median_pct: float | None
    peak_mfe_mean_pct: float | None
    tape_mfe_median_pct: float | None
    tape_mae_median_pct: float | None
    reached_25pct: int
    reached_50pct: int
    reached_100pct: int
    #: The first rung of the shipped ``protection.tp_ladder``, in percent gain.
    first_tp_rung_pct: float
    rung_reachable: int


def excursions(
    conn: sqlite3.Connection, *, mode: str | None = "live", first_tp_rung_pct: float = FIRST_TP_RUNG_PCT
) -> ExcursionReport:
    """Favourable excursion, measured twice and compared against the shipped TP ladder."""
    where = "WHERE closed_ms IS NOT NULL AND entry_price_usd IS NOT NULL"
    args: tuple[object, ...] = ()
    if mode is not None:
        where += " AND mode=?"
        args = (mode,)
    peaks: list[float] = []
    tape_mfe: list[float] = []
    tape_mae: list[float] = []
    n = 0
    for row in conn.execute(
        f"SELECT position_id, chain, token, mode, opened_ms, closed_ms, entry_price_usd, peak_price_usd "
        f"FROM positions {where} ORDER BY opened_ms",
        args,
    ).fetchall():
        entry = _dec(row["entry_price_usd"])
        if entry is None or entry <= 0:
            continue
        n += 1
        peak = _dec(row["peak_price_usd"])
        if peak is not None and peak > 0:
            peaks.append(float(peak / entry - 1) * 100.0)
        hold = [
            float(_dec(p["price_usd"]) / entry - 1) * 100.0  # type: ignore[operator]
            for p in conn.execute(
                "SELECT price_usd FROM swaps WHERE chain=? AND token=? AND price_usd IS NOT NULL "
                "AND ts_ms>=? AND ts_ms<=?",
                (row["chain"], row["token"], row["opened_ms"], row["closed_ms"]),
            ).fetchall()
            if _dec(p["price_usd"])
        ]
        if hold:
            tape_mfe.append(max(hold))
            tape_mae.append(min(hold))
    marks_rows = conn.execute("SELECT COUNT(*) FROM position_marks").fetchone()[0]
    marks_live = conn.execute(
        "SELECT COUNT(*) FROM position_marks m JOIN positions p ON p.position_id=m.position_id "
        "WHERE p.mode='live'"
    ).fetchone()[0]
    return ExcursionReport(
        n=n,
        marks_rows=int(marks_rows),
        marks_for_live=int(marks_live),
        peak_mfe_median_pct=statistics.median(peaks) if peaks else None,
        peak_mfe_mean_pct=statistics.mean(peaks) if peaks else None,
        tape_mfe_median_pct=statistics.median(tape_mfe) if tape_mfe else None,
        tape_mae_median_pct=statistics.median(tape_mae) if tape_mae else None,
        reached_25pct=sum(1 for v in peaks if v >= 25.0),
        reached_50pct=sum(1 for v in peaks if v >= 50.0),
        reached_100pct=sum(1 for v in peaks if v >= 100.0),
        first_tp_rung_pct=first_tp_rung_pct,
        rung_reachable=sum(1 for v in peaks if v >= first_tp_rung_pct),
    )


# --------------------------------------------------------------------------------------
# is the exit slippage band too wide?
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BandScenario:
    """What a candidate exit tolerance would have done, WITH the non-fill branch priced.

    A tighter band does not buy a better price. It refuses this fill and tries again at
    the next tick, on a token that is usually still falling. So the scenario has to model
    the refusal, and this one does it the only way the data allows: by looking up what the
    tape said one retry cycle later and filling there.

    ``retry_optimistic`` is set because that model gives the retry a fill at the tape mid
    with no impact at all — strictly kinder to the tighter band than reality. If a band
    still loses under a model built to flatter it, it loses.

    ``never_filled_risk`` is the branch this cannot price: the retry is refused too, and
    again, and the position rides down. We have exactly one observation of an exit that
    did not go out — a confirmation-gate refusal, not a slippage refusal — and it cost
    48.7 points. That number is carried here so the tail is never waved away.
    """

    band_bps: int
    exits_considered: int
    exits_rejected: int
    mean_realised_pct: float
    mean_modelled_pct: float
    delta_pp: float
    retry_optimistic: bool
    never_filled_risk_pp: float
    unpriceable_retries: int
    #: Per-position ``(position_id, realised_pct, modelled_pct)`` for every exit this band
    #: would have refused. Two positions with opposite signs is not a result, and the only
    #: way a reader can see that is if the rows are here rather than only their mean.
    rejected_detail: tuple[tuple[str, float, float], ...] = ()


def slippage_band_scenarios(
    conn: sqlite3.Connection,
    gaps: Sequence[ExitGap],
    paths: Sequence[PricePath],
    *,
    bands_bps: Sequence[int] = (2500, 2000, 1500, 1000, 500),
    retry_delay_s: float = 2 * DECLARED_POLL_INTERVAL_S,
) -> list[BandScenario]:
    """Price each candidate ``bounds.max_slippage_bps`` on the exits we actually sent.

    ``retry_delay_s`` defaults to two poll intervals: one tick to notice the refusal and
    one to re-send. That is the config's own cadence, not an observed one — the observed
    submit latency on the live sells was 4.1 to 9.8 s for ``sm-trenches``, which is the
    same order, and 128.9 s once.

    The retry is priced as a *relative* move on the tape applied to the realised return,
    not as an absolute tape return. The realised figure is ``proceeds/cost`` in lamports —
    it carries our actual entry, our actual fees and our actual impact. The tape carries a
    dossier-priced entry and someone else's size. Multiplying the realised return by the
    tape's ratio between the two moments keeps our basis and borrows only the thing the
    tape is good for: how much the price moved while we waited.
    """
    by_pos = {p.position_id: p for p in paths}
    worst_known_non_fill_pp = 48.7  # MEASURED: pos_live_3d59670ebf16, mark -42.8% -> fill -91.5%
    out: list[BandScenario] = []
    for band in bands_bps:
        floor = 1.0 - band / 10_000.0
        realised: list[float] = []
        modelled: list[float] = []
        detail: list[tuple[str, float, float]] = []
        rejected = 0
        unpriceable = 0
        for gap in gaps:
            realised.append(gap.realised_pct)
            if gap.fill_over_mark >= floor:
                modelled.append(gap.realised_pct)
                continue
            rejected += 1
            path = by_pos.get(gap.position_id)
            fill_at = int((gap.send_lag_s + gap.submit_lag_s) * 1000)
            retry_at = fill_at + int(retry_delay_s * 1000)
            at_fill = path.at(fill_at) if path else None
            at_retry = path.at(retry_at) if path else None
            if at_fill is None or at_retry is None or at_fill[1] <= 0 or at_fill[0] == at_retry[0]:
                # Same print on both sides means the tape cannot resolve a retry_delay_s
                # gap here. Reporting a drift of exactly 1.0 would dress "we do not know"
                # up as "nothing happened", which is the flattering version.
                unpriceable += 1
                modelled.append(gap.realised_pct)
                continue
            drift = at_retry[1] / at_fill[1]
            # No extra impact charged on the retry: deliberately generous to the tighter band.
            new_pct = ((1.0 + gap.realised_pct / 100.0) * drift - 1.0) * 100.0
            modelled.append(new_pct)
            detail.append((gap.position_id, gap.realised_pct, new_pct))
        if not realised:
            continue
        mr = statistics.mean(realised)
        mm = statistics.mean(modelled)
        out.append(
            BandScenario(
                band_bps=band,
                exits_considered=len(realised),
                exits_rejected=rejected,
                mean_realised_pct=mr,
                mean_modelled_pct=mm,
                delta_pp=mm - mr,
                retry_optimistic=True,
                never_filled_risk_pp=worst_known_non_fill_pp,
                unpriceable_retries=unpriceable,
                rejected_detail=tuple(detail),
            )
        )
    return out


# --------------------------------------------------------------------------------------
# the study
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExitStudy:
    split_ms: int
    eligibility: dict[str, int]
    n_in_sample: int
    n_out_of_sample: int
    n_live: int
    span_hours: float
    policies: tuple[PolicyReport, ...]
    pbo: float | None
    best_in_sample: str | None
    best_in_sample_oos_mean_pct: float | None
    gaps: tuple[ExitGap, ...]
    verdict: GapVerdict
    excursions_live: ExcursionReport
    excursions_shadow: ExcursionReport
    bands: tuple[BandScenario, ...]
    caveats: tuple[str, ...]
    #: Cross-check: the shadow record's own PnL against the same policy replayed on the
    #: independent tape. A large gap means the shadow record is not evidence.
    shadow_record_vs_tape_pp: float | None = None

    @property
    def any_policy_replicated(self) -> bool:
        return any(p.replicated for p in self.policies)


def _sharpe(values: Sequence[float]) -> float:
    s = gates._sharpe_raw(list(values))
    return 0.0 if s is None else s


def study(
    conn: sqlite3.Connection,
    *,
    policies: dict[str, Policy] | None = None,
    horizon_ms: int = REPLAY_HORIZON_MS,
) -> ExitStudy:
    """Run the whole thing. Reads only; returns evidence, recommends nothing."""
    pol = policies or POLICIES
    paths, census = load_paths(conn, horizon_ms=horizon_ms)
    caveats: list[str] = []
    if not paths:
        empty = PolicySummary(0, 0, None, None, None, None, None)
        return ExitStudy(
            split_ms=0,
            eligibility=census,
            n_in_sample=0,
            n_out_of_sample=0,
            n_live=0,
            span_hours=0.0,
            policies=tuple(
                PolicyReport(name, empty, empty, empty, empty, None, None, ("no data",)) for name in pol
            ),
            pbo=None,
            best_in_sample=None,
            best_in_sample_oos_mean_pct=None,
            gaps=(),
            verdict=gap_verdict([]),
            excursions_live=excursions(conn, mode="live"),
            excursions_shadow=excursions(conn, mode="shadow"),
            bands=(),
            caveats=("no replayable positions",),
        )

    opens = [p.opened_ms for p in paths]
    split = int(statistics.median(opens))
    span_hours = (max(opens) - min(opens)) / 3_600_000.0
    names = list(pol)
    results: dict[str, list[PolicyResult]] = {n: [pol[n](p) for p in paths] for n in names}
    matrix = [[results[n][i].net_return_pct for n in names] for i in range(len(paths))]

    is_idx = [i for i, p in enumerate(paths) if p.opened_ms <= split]
    oos_idx = [i for i, p in enumerate(paths) if p.opened_ms > split]
    live_idx = [i for i, p in enumerate(paths) if p.mode == "live"]

    trial_sharpes = [_sharpe([row[k] for row in matrix]) for k in range(len(names))]
    oos_sharpes = [_sharpe([matrix[i][k] for i in oos_idx]) for k in range(len(names))]

    reports: list[PolicyReport] = []
    for k, name in enumerate(names):
        series = [row[k] for row in matrix]
        dsr, notes = gates.deflated_sharpe(series, trials=len(names), trial_sharpes=trial_sharpes)
        dsr_o, notes_o = gates.deflated_sharpe(
            [matrix[i][k] for i in oos_idx], trials=len(names), trial_sharpes=oos_sharpes
        )
        reports.append(
            PolicyReport(
                name=name,
                overall=summarise(results[name]),
                in_sample=summarise([results[name][i] for i in is_idx]),
                out_of_sample=summarise([results[name][i] for i in oos_idx]),
                live_only=summarise([results[name][i] for i in live_idx]),
                dsr_overall=dsr,
                dsr_out_of_sample=dsr_o,
                dsr_notes=tuple(dict.fromkeys([*notes, *notes_o])),
            )
        )

    pbo = gates.pbo_cscv(matrix, splits=CSCV_SPLITS)
    best = None
    best_oos = None
    if is_idx:
        bk = max(range(len(names)), key=lambda k: statistics.mean(matrix[i][k] for i in is_idx))
        best = names[bk]
        best_oos = statistics.mean(matrix[i][bk] for i in oos_idx) if oos_idx else None

    gaps = exit_gaps(conn, mode="live")
    verdict = gap_verdict(gaps)
    bands = slippage_band_scenarios(conn, gaps, paths)

    shadow_gap = None
    shadow_paths = [p for p in paths if p.mode == "shadow"]
    if shadow_paths:
        tape = statistics.mean(fixed_stop(p, DECLARED_STOP_PCT).net_return_pct for p in shadow_paths)
        rec = [
            r[0]
            for r in conn.execute(
                "SELECT pnl_pct FROM trades WHERE mode='shadow' AND position_id IN "
                f"({','.join('?' * len(shadow_paths))})",
                [p.position_id for p in shadow_paths],
            ).fetchall()
        ]
        if rec:
            shadow_gap = statistics.mean(rec) - tape

    caveats.append(
        f"The whole corpus spans {span_hours:.1f} hours. A time split inside one session is not a "
        "held-out regime; it is the same tape cut in two."
    )
    caveats.append(
        f"{census['too_few_prints']} of {census['closed']} closed positions had fewer than "
        f"{MIN_PATH_POINTS} tape prints and were dropped. Tokens stop printing when they die, so "
        "the surviving sample is biased towards tokens that kept trading."
    )
    censored_control = next(
        (r.overall.censored for r in reports if r.name == "hold-60min(control)"), None
    )
    if censored_control:
        caveats.append(
            f"The hold-to-+60min control is censored on {censored_control} of {len(paths)} positions: "
            "our ingestion stops shortly after we close, so the control is mostly not measured."
        )
    if shadow_gap is not None:
        caveats.append(
            f"The shadow record is {shadow_gap:+.1f}pp away from the same stop replayed on the "
            "independent tape. Shadow rows are a broker model, not evidence."
        )
    if verdict.n and not verdict.fill_effect_separable:
        caveats.append(
            f"With n={verdict.n} live exits the bootstrap interval on fill-versus-mark straddles 1.0. "
            "No statement about exit slippage is separable from noise."
        )
    return ExitStudy(
        split_ms=split,
        eligibility=census,
        n_in_sample=len(is_idx),
        n_out_of_sample=len(oos_idx),
        n_live=len(live_idx),
        span_hours=span_hours,
        policies=tuple(reports),
        pbo=pbo,
        best_in_sample=best,
        best_in_sample_oos_mean_pct=best_oos,
        gaps=tuple(gaps),
        verdict=verdict,
        excursions_live=excursions(conn, mode="live"),
        excursions_shadow=excursions(conn, mode="shadow"),
        bands=tuple(bands),
        caveats=tuple(caveats),
        shadow_record_vs_tape_pp=shadow_gap,
    )


def _pct(value: float | None, width: int = 8) -> str:
    return "n/a".rjust(width) if value is None else f"{value:+{width}.1f}"


def render(s: ExitStudy) -> str:
    """A report an operator can read in one screen without a notebook."""
    lines: list[str] = []
    lines.append("EXIT STUDY - when to sell, measured on our own tape")
    lines.append("=" * 104)
    lines.append(
        f"replayable {s.eligibility['replayable']}/{s.eligibility['closed']} closed positions "
        f"(dropped: {s.eligibility['no_entry_price']} no entry price, "
        f"{s.eligibility['too_few_prints']} too few tape prints)"
    )
    lines.append(
        f"time split: in-sample n={s.n_in_sample}, out-of-sample n={s.n_out_of_sample}, "
        f"live n={s.n_live}, corpus span {s.span_hours:.1f} h"
    )
    lines.append(f"round-trip cost charged: {ROUND_TRIP_COST_PCT:.2f}% ({CONSTANT_PROVENANCE['LEG_COST_PCT']})")
    lines.append("")
    lines.append("1. THE EXIT GAP  (declared stop -> watchdog mark at send -> realised fill)")
    lines.append(
        f"   {'position':24s} {'lane':14s} {'trigger':14s} {'thresh':>7s} {'mark@send':>10s} "
        f"{'realised':>9s} {'eval gap':>9s} {'fill gap':>9s} {'fill/mark':>9s} {'submit':>7s}"
    )
    for g in s.gaps:
        lines.append(
            f"   {g.position_id[:24]:24s} {g.lane[:14]:14s} {g.exit_reason[:14]:14s} "
            f"{(f'{g.declared_stop_pct:+.0f}%' if g.threshold_applies else 'n/a'):>7s} "
            f"{_pct(g.mark_at_send_pct, 10)} "
            f"{_pct(g.realised_pct, 9)} "
            f"{(_pct(g.evaluation_gap_pp, 9) if g.threshold_applies else 'n/a'.rjust(9))} "
            f"{_pct(g.fill_gap_pp, 9)} {g.fill_over_mark:9.3f} {g.submit_lag_s:6.1f}s"
        )
        for oid, err in g.failed_attempts:
            lines.append(f"      ! sell {oid} never went out: {err[:80]}")
    v = s.verdict
    lines.append(
        f"   verdict (n={v.n}): mean evaluation gap {_pct(v.mean_evaluation_gap_pp, 6)}pp, "
        f"mean fill gap {_pct(v.mean_fill_gap_pp, 6)}pp, mean fill/mark "
        f"{'n/a' if v.mean_fill_over_mark is None else f'{v.mean_fill_over_mark:.3f}'} "
        f"CI95 {'n/a' if not v.fill_over_mark_ci95 else f'[{v.fill_over_mark_ci95[0]:.3f}, {v.fill_over_mark_ci95[1]:.3f}]'}"
    )
    lines.append(f"   dominant component: {v.dominant}")
    for lane in sorted({g.lane for g in s.gaps}):
        sub = gap_verdict([g for g in s.gaps if g.lane == lane])
        lines.append(
            f"     {lane:16s} n={sub.n}  eval {_pct(sub.mean_evaluation_gap_pp, 6)}pp  "
            f"fill {_pct(sub.mean_fill_gap_pp, 6)}pp  "
            f"fill/mark {'n/a' if sub.mean_fill_over_mark is None else f'{sub.mean_fill_over_mark:.3f}'}"
        )
    lines.append("")
    lines.append("2. COUNTERFACTUAL EXIT POLICIES  (net of cost; cens = censored, no measured outcome)")
    lines.append(
        f"   {'policy':22s} {'n':>3s} {'cens':>4s} {'mean':>8s} {'med':>8s} {'win%':>5s} {'worst':>8s} "
        f"| {'IS mean':>8s} {'OOS mean':>8s} {'OOS med':>8s} {'DSRoos':>6s} | {'LIVE':>8s}"
    )
    for r in s.policies:
        lines.append(
            f"   {r.name:22s} {r.overall.n:3d} {r.overall.censored:4d} {_pct(r.overall.mean_pct)} "
            f"{_pct(r.overall.median_pct)} {'' if r.overall.win_rate_pct is None else f'{r.overall.win_rate_pct:5.0f}'} "
            f"{_pct(r.overall.worst_pct)} | {_pct(r.in_sample.mean_pct)} {_pct(r.out_of_sample.mean_pct)} "
            f"{_pct(r.out_of_sample.median_pct)} "
            f"{'   n/a' if r.dsr_out_of_sample is None else f'{r.dsr_out_of_sample:6.3f}'} | "
            f"{_pct(r.live_only.mean_pct)}"
        )
    lines.append(
        f"   best in-sample: {s.best_in_sample} -> out-of-sample mean "
        f"{_pct(s.best_in_sample_oos_mean_pct)}%   PBO/CSCV = "
        f"{'n/a' if s.pbo is None else f'{s.pbo:.3f}'}"
    )
    lines.append(f"   any policy replicated (OOS>0 and DSR>=0.95): {s.any_policy_replicated}")
    lines.append("")
    lines.append("3. FAVOURABLE EXCURSION  (does a take-profit have anything to take?)")
    for label, e in (("live", s.excursions_live), ("shadow", s.excursions_shadow)):
        lines.append(
            f"   {label:7s} n={e.n:3d}  peak MFE median {_pct(e.peak_mfe_median_pct, 7)}%  "
            f">=+25%: {e.reached_25pct}/{e.n}  >=+50%: {e.reached_50pct}/{e.n}  "
            f">=+100%: {e.reached_100pct}/{e.n}  shipped first TP rung "
            f"{e.first_tp_rung_pct:.0f}% reached by {e.rung_reachable}/{e.n}"
        )
    lines.append(
        f"   position_marks: {s.excursions_live.marks_rows} rows in total, "
        f"{s.excursions_live.marks_for_live} of them for live positions."
    )
    lines.append("")
    lines.append("4. IS THE EXIT SLIPPAGE BAND TOO WIDE?  (retry priced, non-fill tail flagged)")
    lines.append(
        f"   {'band':>6s} {'rejected':>9s} {'realised':>9s} {'modelled':>9s} {'delta':>8s} "
        f"{'unpriceable':>12s}"
    )
    for b in s.bands:
        lines.append(
            f"   {b.band_bps:6d} {b.exits_rejected:4d}/{b.exits_considered:<4d} "
            f"{_pct(b.mean_realised_pct, 9)} {_pct(b.mean_modelled_pct, 9)} {_pct(b.delta_pp, 8)} "
            f"{b.unpriceable_retries:12d}"
        )
    for b in s.bands:
        for pid, was, now in b.rejected_detail:
            lines.append(f"      {b.band_bps}bps would refuse {pid[:24]}: {was:+.1f}% -> {now:+.1f}%")
    if s.bands:
        lines.append(
            f"   retry modelled at the tape mid with zero impact (generous to the tighter band). "
            f"Worst measured non-fill cost {s.bands[0].never_filled_risk_pp:.1f}pp and is NOT in the deltas."
        )
    lines.append("")
    lines.append("CAVEATS")
    for c in s.caveats:
        lines.append(f"   - {c}")
    return "\n".join(lines)


def main(db_path: str | None = None) -> int:  # pragma: no cover - operator entry point
    from kaiba.core.db import get_conn

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) if db_path else get_conn()
    conn.row_factory = sqlite3.Row
    print(render(study(conn)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
