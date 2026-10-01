"""Replay a recorded token episode through a lane as of time T, with the future removed.

Why this module exists, in one paragraph.
-----------------------------------------

``kaiba.learning.validation`` computed, from our own closed trades, that a directional
memecoin book needs roughly 5,447 trades to conclude anything, and 16,122 once a hundred
parameter configurations are counted honestly. At 30-60 trades a week that is 1.7-3.5
years, against a venue whose rules change on something like an eight-week cycle. A sample
gathered across regime boundaries is not one sample, so it cannot be tested as one.
"Trade and wait" is therefore not a sequence this operation can execute. Replay is the
only substitute available: we hold 360,000+ swap rows and an exact bonding-curve
arithmetic that has been checked against mainnet, and a replay converts those rows into
answers about decisions we never took.

A backtester is also the single easiest thing in this repository to fool yourself with.
Four specific failures are what the design below is shaped around, and each one has a
named test that fails when the guard against it is removed.

**1. Lookahead.** The rule is "a replay at T may read only rows whose observation time is
at or before T", and stating the rule is worth nothing. :class:`PointInTime` enforces it
twice. It appends the bound to the SQL (*prevention*), and then it re-checks the time
column of every row it is about to hand back (*detection*). The second check is
unconditional and does not trust the first, so deleting the ``WHERE`` clause does not
produce a quietly optimistic backtest - it produces a :class:`LookaheadError` naming the
row and the milliseconds by which it was in the future. Tables that carry no observation
time at all are refused by name rather than read: ``tokens`` is mutated in place, so
``tokens.migrated_ms`` cannot be read "as of" anything, and ``entity_members`` has no
timestamp column, so cluster membership cannot be reconstructed for a past instant. The
lane under replay is additionally handed ``conn=None`` - it gets no database handle at
all, so the only facts it can see are the ones the point-in-time view assembled for it.

**2. Survivorship.** The denominator is written down before the result is. Every candidate
episode lands in ``replay_episodes`` with ``eligible`` and a reason, so "91 replayed" is
never reported without "out of 1,068 considered, excluded for these named reasons".
:class:`EpisodeCensus` is returned alongside every result and is part of the printed
report, not a footnote.

**3. A second fill model.** There is exactly one fill model in this repository and it is
``kaiba.execution.paper.PaperBroker``. This module does not contain a second one. It
constructs a real ``PaperBroker`` against a scratch in-memory database, feeds it
point-in-time prices and depth, and reads back the ``trades`` row the broker itself wrote.
Its refusals are kept: ``slippage_exceeded`` on a thin pool is a result, not an error, and
a replay that filled those would be measuring a venue we do not trade at.

**4. Five hundred quiet variants and one reported winner.** Every configuration is
registered in ``lane_trials`` through :func:`kaiba.learning.registry.register` *before*
its result is read, keyed on the effective merged lane parameters rather than on the
override dict, so a sweep cannot under-count itself by passing fewer keys. The report
carries the deflated Sharpe from ``gates.deflated_sharpe`` and PBO from ``gates.pbo_cscv``,
and - the part that matters - ``deflation_ok`` is False when fewer than two comparable
trials existed, because in that case the published procedure degrades to a plain
probabilistic Sharpe against zero and calling it deflated would be the lie.

On the direction question, stated before any number
---------------------------------------------------

The only lane that fires, ``migration-fade``, is titled "Fade the post-migration pump" and
emits ``{"bias": "sell"}``. ``engine.py`` hardcodes ``side=Side.BUY``, so all 44 recorded
trades were longs on a short thesis. The obvious reading is that a bug cost us the short
side. That reading is wrong, and this module says so before it reports anything:

    **A pump.fun bonding curve has no borrow and no perp, and neither does PumpSwap.
    There is no venue at which the short arm could have been executed.**

So :data:`Arm.SHORT_MIRROR` is computed and reported - the operator asked for both sides
and is entitled to see them - but it is marked ``executable=False`` and it is an *upper
bound*, not a fill: it charges the same round-trip costs the long arm actually paid and
charges nothing for borrow, funding, recall or the liquidation a memecoin short would
meet. It is the number a short would have made in a world with a venue. It is not a
number anyone could have collected.

What the lane's thesis *does* license, and what this module can therefore answer with real
fills, is the set of alternatives that are expressible at the venue: do not enter at all;
enter later, after the fade has run; or exit sooner. Those are ordinary long replays at
different ``entry_delay_s`` and ``horizon_s``, and they are what
:func:`direction_report` sweeps.

Everything is Decimal or int. No float touches money.
"""

from __future__ import annotations

import logging
import random
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from kaiba.core import db
from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump
from kaiba.core.schemas import (
    Action,
    Chain,
    Decision,
    EventKind,
    EvidenceBasis,
    Grade,
    Lane,
    LaneMode,
    OrderState,
    digest,
    now_ms,
)
from kaiba.execution import lanes, paper, viability
from kaiba.execution.curve_price import FillBasis
from kaiba.learning import gates, registry, validation

log = logging.getLogger(__name__)

BPS = Decimal(10_000)
LAMPORTS_PER_SOL = Decimal(1_000_000_000)


# ------------------------------------------------------------------------- provenance


@dataclass(frozen=True)
class Provenance:
    """Where a constant came from.

    ``tests/test_replay.py::test_every_constant_has_provenance`` parses this file and
    fails if a module-level constant is added without a row here. The pattern is
    ``kaiba.execution.viability``'s and it exists because a silently-added knob is how an
    unmeasured number becomes load-bearing: the whole value of a replay is that every
    number in it can be traced, and a threshold nobody can source is indistinguishable
    from one chosen because it made the result look better.
    """

    value: Any
    unit: str
    source: str
    note: str


# --------------------------------------------------------------------------- constants

#: Horizons reported for every run, seconds. Chosen to match the horizons the episode
#: census is cut at so a reader can never see a result without its own denominator.
DEFAULT_HORIZONS_S: tuple[int, ...] = (60, 120, 300)

#: Swaps required inside an episode's window before it is eligible.
#:
#: INVENTED, and it is a real knob. It is a floor on how many points the pool-depth solve
#: below has to work with, not a statement about the market: the solve consumes
#: consecutive pairs, so ``n`` swaps yield ``n-1`` estimates and eight is the smallest
#: number at which the inlier fraction is not dominated by one pair. Sensitivity is
#: reported: ``EpisodeCensus.by_min_swaps`` prints the count at 4, 8 and 16 so a reader
#: can see how much of the denominator this one number is deciding.
MIN_EPISODE_SWAPS = 8

#: Trailing window over which the implied pool is solved, seconds.
#:
#: INVENTED. Long enough to accumulate estimates at the ~0.2 swaps/second we observe
#: post-migration, short enough that a pool which is genuinely draining is not averaged
#: with its own healthier past.
DEPTH_WINDOW_S = 120

#: Consecutive-pair solves required before an implied pool is reported at all.
#: INVENTED; same reasoning as :data:`MIN_EPISODE_SWAPS`, applied to the trailing window
#: rather than the whole episode.
DEPTH_MIN_SOLVES = 8

#: A solve counts as an inlier when it is within this fraction of the window's median.
#:
#: INVENTED, and the weakest number in this module. It was chosen by eye from the
#: separation visible in the data: tokens whose tape is contiguous solve to a tight
#: cluster (a genuine post-graduation pool, tens to hundreds of SOL), while tokens with
#: rows missing collapse toward 0.01-0.15 SOL with 40-80% dispersion, because the price
#: move of a swap we do not hold is attributed to the SOL amount of one we do. The cut
#: is not a measurement. :func:`EpisodeCensus.depth_sensitivity` reports the eligible
#: count across a range of tolerances so the result is never read at one point.
DEPTH_INLIER_TOL = Decimal("0.20")

#: Fraction of trailing solves that must be inliers for the depth to be usable.
#: INVENTED; see :data:`DEPTH_INLIER_TOL`. This is the contiguity gate, and it is
#: evidence of consistency, never proof of completeness - a missing swap whose price move
#: was small passes it undetected. Every depth this module reports is therefore labelled
#: ``EvidenceBasis.ESTIMATED``, never ``VERIFIED_ONCHAIN``.
DEPTH_MIN_INLIER_FRAC = Decimal("0.70")

#: How stale a mid price may be at the decision instant, seconds.
#:
#: DERIVED: the live watchdog polls at 5 s and the post-migration tape prints at a median
#: of one swap every ~4.4 s, so a price older than this was not what a live loop would
#: have been looking at. A replay that prices off a two-minute-old print is measuring a
#: different system from the one we run.
PRICE_MAX_AGE_S = 30

#: How stale the last print may be when a position is marked out at its deadline.
#:
#: DERIVED, and deliberately looser than :data:`PRICE_MAX_AGE_S`. An entry is optional -
#: not trading is always available, so a stale price is a reason to stand aside. An exit
#: at a deadline is not optional, and the price a position is marked out at is simply the
#: last trade, however long ago it was. Every outcome records the age of the print it
#: exited on so a reader can filter on it; this is the ceiling, not a target.
EXIT_PRICE_MAX_AGE_S = 300

#: How often the replay's clock wakes up, seconds.
#:
#: MEASURED: ``kaiba.execution.watchdog`` ticks at 5 s, and the post-migration tape prints
#: at a median of about one swap every 4.4 s. A replay that polls faster than the live
#: loop is testing an agent we do not run.
POLL_S = 5

#: How long after t0 the replay keeps looking for an entry, seconds.
#:
#: DERIVED from the lane, not chosen: ``migration_fade`` refuses to fire once
#: ``age_s > sell_within_s``, whose shipped default is 180. Polling past the lane's own
#: window would replay a rule the lane does not implement.
ENTRY_WINDOW_S = 180

#: The venue whose swaps the pool solve is allowed to use.
#:
#: MEASURED: our sol tape carries ``program`` on every row - ``pump`` (the bonding curve,
#: 300,471 rows) and ``pump_amm`` (the post-graduation pool, 398,848). The constant-product
#: solve is the AMM's arithmetic, and a trailing window that reaches back across migration
#: would otherwise mix curve swaps into a pool that did not exist yet.
DEPTH_PROGRAM = "pump_amm"

#: How stale the SOL/USD mark may be, seconds. DERIVED: ``native_prices`` is written on a
#: ~70 s cadence by the dexscreener poller, so two intervals is the first age at which a
#: gap is a gap rather than the normal spacing.
SOL_USD_MAX_AGE_S = 180

#: Calendar block used as the row unit of the PBO matrix, seconds. DERIVED: it is the
#: longest reported horizon, which is the shortest block that does not let one episode
#: straddle two rows and correlate them.
BLOCK_S = 300

#: Bootstrap resamples for a confidence interval on the mean. Chosen for this module;
#: above ~2,000 the percentile interval moves in the fourth decimal.
BOOTSTRAP_DRAWS = 4_000

#: Fixed seed so a reported interval is reproducible from the same rows. Chosen for this
#: module: the date this file was written.
BOOTSTRAP_SEED = 20_260_921

#: Two-sided interval coverage. DEFINITIONAL: the convention the rest of the harness
#: reports at (``validation.Z_NAIVE`` is the 95% one-sided z).
CI_ALPHA = Decimal("0.05")

#: Tolerance on the reconciliation between the cost fractions this module recovers from
#: the broker's own fill receipt and the profit the broker itself booked, as a fraction of
#: notional. DERIVED: the broker rounds the fill quantity to whole token atoms, so a
#: round trip cannot reconcile below roughly one atom over the position; 0.005 is two
#: orders of magnitude above that and still tight enough to catch a mis-recovered leg.
COST_RECONCILE_TOL = Decimal("0.005")

#: Episode clock bases, best first. DEFINITIONAL. ``tokens.migrated_ms`` is deliberately
#: absent: the row is mutated in place and the value is ``now_ms()`` at ingest, so it is
#: our collector's wall clock and cannot be read as of any past instant.
T0_BASES: tuple[str, ...] = ("event:token.migrated",)

#: Time column each table's rows are observed at. A table not in here cannot be read by
#: :class:`PointInTime` at all. DEFINITIONAL: this mapping *is* the point-in-time rule.
TIME_COLUMNS: Mapping[str, str] = {
    "swaps": "ts_ms",
    "events": "ts_ms",
    "curve_snapshots": "observed_ms",
    "native_prices": "ts_ms",
    "signals": "created_ms",
    "decisions": "ts_ms",
    "orders": "created_ms",
    "trades": "closed_ms",
    "position_marks": "ts_ms",
    "triage_decisions": "ts_ms",
    "token_dossiers": "built_at_ms",
    "token_bundles": "computed_ms",
    "cluster_edges": "first_seen_ms",
    "wallet_score_history": "scored_at_ms",
}

#: Tables that carry no usable observation time, with the reason, so a refusal explains
#: itself. DEFINITIONAL, and each entry was checked against the live schema.
NO_OBSERVATION_TIME: Mapping[str, str] = {
    "tokens": (
        "the row is updated in place; migrated_ms and pool are written after the fact and "
        "migrated_ms is now_ms() at ingest, not chain time. Read migration from the "
        "token.migrated event, which is timestamped and never rewritten."
    ),
    "entity_members": (
        "no timestamp column at all: entity membership cannot be reconstructed as of a "
        "past instant, only as it stands now."
    ),
    "entities": "no timestamp column; same problem as entity_members.",
    "wallet_scores": "current state only; the history lives in wallet_score_history.",
    "positions": "mutated in place on every mark and every partial exit.",
    "token_tape": "coverage verdicts are rewritten as a tape is deepened.",
    "creators": "aggregate row, recomputed in place.",
}

PROVENANCE: Mapping[str, Provenance] = {
    "DEFAULT_HORIZONS_S": Provenance(
        DEFAULT_HORIZONS_S, "seconds", "the horizons the task requires reported up front",
        "DEFINITIONAL: the reporting contract, not a measured cut point.",
    ),
    "MIN_EPISODE_SWAPS": Provenance(
        MIN_EPISODE_SWAPS, "swaps", "chosen for this module",
        "INVENTED: floor on the number of consecutive pairs the depth solve can use. "
        "Sensitivity reported at 4/8/16 in EpisodeCensus.by_min_swaps.",
    ),
    "DEPTH_WINDOW_S": Provenance(
        DEPTH_WINDOW_S, "seconds", "chosen for this module",
        "INVENTED: ~0.2 swaps/s post-migration puts ~24 prints in the window, and a "
        "draining pool is not averaged with its healthier past.",
    ),
    "DEPTH_MIN_SOLVES": Provenance(
        DEPTH_MIN_SOLVES, "solves", "chosen for this module",
        "INVENTED: same reasoning as MIN_EPISODE_SWAPS applied to the trailing window.",
    ),
    "DEPTH_INLIER_TOL": Provenance(
        DEPTH_INLIER_TOL, "fraction of the window median", "chosen by eye from this data",
        "INVENTED and the weakest constant here: contiguous tapes cluster tightly, holed "
        "tapes collapse toward 0.01-0.15 SOL with 40-80% dispersion. Sensitivity is "
        "reported by EpisodeCensus.depth_sensitivity.",
    ),
    "DEPTH_MIN_INLIER_FRAC": Provenance(
        DEPTH_MIN_INLIER_FRAC, "fraction", "chosen by eye from this data",
        "INVENTED: the contiguity gate. Evidence of consistency, never proof of "
        "completeness, which is why every depth is reported as ESTIMATED.",
    ),
    "PRICE_MAX_AGE_S": Provenance(
        PRICE_MAX_AGE_S, "seconds", "measured: watchdog polls at 5 s; post-migration tape "
        "prints at a median of one swap per ~4.4 s",
        "DERIVED: a price older than this is not what the live loop would have seen.",
    ),
    "EXIT_PRICE_MAX_AGE_S": Provenance(
        EXIT_PRICE_MAX_AGE_S, "seconds", "the longest reported horizon",
        "DERIVED: an entry is optional and a stale price is a reason to stand aside; an "
        "exit at a deadline is not optional and is marked at the last trade. Every "
        "outcome records the age of the print it exited on.",
    ),
    "POLL_S": Provenance(
        POLL_S, "seconds", "measured: watchdog ticks at 5 s; tape prints every ~4.4 s",
        "MEASURED: a replay that polls faster than the live loop tests an agent we do "
        "not run.",
    ),
    "ENTRY_WINDOW_S": Provenance(
        ENTRY_WINDOW_S, "seconds", "lanes.DEFAULT_PARAMS[Lane.MIGRATION_FADE]['sell_within_s']",
        "DERIVED from the lane itself: migration_fade returns None once age_s exceeds "
        "sell_within_s, so polling past it would replay a rule the lane does not have.",
    ),
    "DEPTH_PROGRAM": Provenance(
        DEPTH_PROGRAM, "swaps.program value",
        "measured: 398,848 pump_amm rows and 300,471 pump rows in our sol tape",
        "MEASURED: the constant-product solve is the AMM's arithmetic, and a trailing "
        "window reaching back across migration would mix curve swaps into a pool that "
        "did not exist yet.",
    ),
    "SOL_USD_MAX_AGE_S": Provenance(
        SOL_USD_MAX_AGE_S, "seconds", "measured: native_prices lands on a ~70 s cadence",
        "DERIVED: two polling intervals, the first age at which a gap is a real gap.",
    ),
    "BLOCK_S": Provenance(
        BLOCK_S, "seconds", "the longest reported horizon",
        "DERIVED: shortest block in which one episode cannot straddle two rows.",
    ),
    "BOOTSTRAP_DRAWS": Provenance(
        BOOTSTRAP_DRAWS, "resamples", "chosen for this module",
        "DEFINITIONAL: above ~2,000 the percentile interval moves in the fourth decimal.",
    ),
    "BOOTSTRAP_SEED": Provenance(
        BOOTSTRAP_SEED, "seed", "chosen for this module",
        "DEFINITIONAL: fixed so a reported interval is reproducible from the same rows.",
    ),
    "CI_ALPHA": Provenance(
        CI_ALPHA, "probability", "the convention the rest of the harness reports at",
        "DEFINITIONAL: two-sided 95%.",
    ),
    "COST_RECONCILE_TOL": Provenance(
        COST_RECONCILE_TOL, "fraction of notional", "derived from the broker's atom rounding",
        "DERIVED: a round trip cannot reconcile below ~one token atom over the position; "
        "this is two orders of magnitude above that.",
    ),
    "T0_BASES": Provenance(
        T0_BASES, "clock bases", "measured: 1,068 of 1,068 migrated sol tokens carry a "
        "token.migrated event with its own ts_ms",
        "DEFINITIONAL: tokens.migrated_ms is excluded because it is our wall clock and "
        "the row is mutated in place.",
    ),
    "TIME_COLUMNS": Provenance(
        TIME_COLUMNS, "table -> column", "the live schema, column by column",
        "DEFINITIONAL: this mapping is the point-in-time rule.",
    ),
    "NO_OBSERVATION_TIME": Provenance(
        NO_OBSERVATION_TIME, "table -> reason", "the live schema, column by column",
        "DEFINITIONAL: a table with no observation time cannot be read as of a past "
        "instant, and saying so by name beats reading it and hoping.",
    ),
    "BPS": Provenance(BPS, "basis points per unit", "arithmetic", "DEFINITIONAL."),
    "LAMPORTS_PER_SOL": Provenance(
        LAMPORTS_PER_SOL, "lamports", "Solana", "DEFINITIONAL."
    ),
}


# ------------------------------------------------------------------------ point in time


class LookaheadError(RuntimeError):
    """A row the replay could not have seen was reachable from the replay.

    This is raised, never logged and continued. A backtest that reads one future row and
    carries on is worth less than no backtest, because it produces a number.
    """


class PointInTime:
    """A read-only view of the database frozen at ``at_ms``.

    Two independent mechanisms, deliberately. :meth:`rows` appends the time bound to the
    SQL, which is *prevention*; :meth:`_guard` then re-checks the time column of every row
    on the way out, which is *detection* and does not trust the first. The second exists
    because the first is a string that someone will eventually edit.

    A row whose time column is NULL is a leak too, and is refused: we cannot demonstrate
    it existed at ``at_ms``, and "probably fine" is the posture this class exists to
    remove.
    """

    #: Removes the SQL time bound. Exists ONLY so the mutation check in
    #: ``tests/test_replay.py`` can delete the prevention and demonstrate that the
    #: detection catches it. Setting it on a real run makes every read raise.
    _omit_time_bound: bool = False

    def __init__(self, conn: sqlite3.Connection, at_ms: int) -> None:
        self._conn = conn
        self._at_ms = int(at_ms)
        self.reads = 0

    @property
    def at_ms(self) -> int:
        return self._at_ms

    @property
    def conn(self) -> sqlite3.Connection:
        """The underlying handle. Deliberately not handed to a lane - see module docstring."""
        return self._conn

    def at(self, to_ms: int) -> PointInTime:
        """A second view at a different instant. Never mutates this one."""
        view = PointInTime(self._conn, to_ms)
        view._omit_time_bound = self._omit_time_bound
        return view

    # ---------------------------------------------------------------- reads

    def rows(
        self,
        table: str,
        *,
        where: str = "",
        params: Sequence[Any] = (),
        order_by: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Rows of ``table`` observed at or before the cursor, newest-first unless told.

        ``where`` is ANDed with the time bound. It must not reference another table: this
        view does not do joins, because a join's rows cannot be guarded column by column
        without knowing which alias each time column belongs to, and a guard with a hole
        in it is the thing we are trying not to build. Two queries instead.
        """
        tcol = self.time_column(table)
        clauses: list[str] = []
        args: list[Any] = []
        if not self._omit_time_bound:
            clauses.append(f"{tcol} <= ?")
            args.append(self._at_ms)
        if where:
            clauses.append(f"({where})")
            args.extend(params)
        sql = f"SELECT * FROM {table}"  # noqa: S608 - table name is checked above
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {order_by}" if order_by else f" ORDER BY {tcol} DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        self.reads += 1
        return self._guard(table, tcol, fetch_all(self._conn, sql, tuple(args)))

    def one(self, table: str, **kw: Any) -> dict[str, Any] | None:
        got = self.rows(table, **{**kw, "limit": 1})
        return got[0] if got else None

    @staticmethod
    def time_column(table: str) -> str:
        """The column a row of ``table`` is observed at, or a refusal naming the table."""
        if table in NO_OBSERVATION_TIME:
            raise LookaheadError(
                f"{table} cannot be read point-in-time: {NO_OBSERVATION_TIME[table]}"
            )
        try:
            return TIME_COLUMNS[table]
        except KeyError:
            raise LookaheadError(
                f"{table} has no registered observation time. Add it to "
                "replay.TIME_COLUMNS with the column its rows are observed at, or to "
                "replay.NO_OBSERVATION_TIME with the reason it has none."
            ) from None

    def _guard(
        self, table: str, tcol: str, rows: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Unconditional. This is what makes deleting the SQL bound a loud failure."""
        for row in rows:
            value = row.get(tcol)
            if value is None:
                raise LookaheadError(
                    f"{table} row has a NULL {tcol}: it cannot be shown to have existed "
                    f"at {self._at_ms}, so the replay refuses it rather than assuming"
                )
            if int(value) > self._at_ms:
                raise LookaheadError(
                    f"lookahead: {table}.{tcol}={int(value)} is {int(value) - self._at_ms}ms "
                    f"past the replay cursor {self._at_ms}. The time bound on this read is "
                    "missing or wrong."
                )
        return [dict(r) for r in rows]


def open_readonly(path: Path | str) -> sqlite3.Connection:
    """The live database, opened so a replay physically cannot write to it."""
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# ------------------------------------------------------------------------- pool depth


@dataclass(frozen=True)
class Depth:
    """The implied one-sided pool behind a fill, and how much the answer is worth."""

    lamports: int | None
    basis: EvidenceBasis
    source: str
    solves: int
    inlier_frac: Decimal | None

    @property
    def known(self) -> bool:
        return self.lamports is not None

    def usd(self, sol_usd: Decimal) -> Decimal | None:
        if self.lamports is None:
            return None
        return Decimal(self.lamports) / LAMPORTS_PER_SOL * sol_usd


UNKNOWN_DEPTH = Depth(None, EvidenceBasis.UNAVAILABLE, "not_solved", 0, None)


def implied_pool(
    view: PointInTime,
    chain: Chain,
    token: str,
    *,
    window_s: int = DEPTH_WINDOW_S,
    min_solves: int = DEPTH_MIN_SOLVES,
    inlier_tol: Decimal = DEPTH_INLIER_TOL,
    min_inlier_frac: Decimal = DEPTH_MIN_INLIER_FRAC,
    not_before_ms: int | None = None,
    program: str | None = None,
) -> Depth:
    """Solve the post-graduation pool's SOL reserve from the tape, as of the cursor.

    For a constant-product pool, a swap of ``dX`` SOL moves the reserve from ``X`` to
    ``X + dX`` and the price from ``p`` to ``p'`` with ``p'/p = ((X + dX)/X)**2``, so
    ``X = dX / (sqrt(p'/p) - 1)``. Consecutive tape rows give ``dX`` and the two prices,
    which makes this the pool depth *implied by our own rows* rather than a figure fetched
    from a provider tonight. There is no historical liquidity anywhere in the database -
    ``fill_prices`` is empty and ``token_dossiers`` has one current row per token - so
    this is the only point-in-time depth available, and without it the pool leg of the
    fill model has no second argument.

    The dispersion of the estimates is also a free tape-contiguity test: a swap we do not
    hold has its price move attributed to a swap we do, which collapses the implied pool.
    That is *evidence* of contiguity, never proof - a missing swap that barely moved the
    price passes undetected - so the basis returned is ``ESTIMATED`` and never better.

    ``not_before_ms`` and ``program`` exist because the trailing window reaches back
    across migration. A bonding-curve swap and an AMM swap are different price mechanics,
    and averaging them produces a pool depth for a pool that did not exist yet. Pass the
    migration instant and the AMM program and the solve stays inside one venue.

    Returns :data:`UNKNOWN_DEPTH` rather than a number when the gate fails. Missing is
    ``None`` with ``UNAVAILABLE``; it is never 0, because a 0 here reads as "no pool",
    which the fill model correctly treats as untradeable, and that is a different claim
    from "we could not tell".
    """
    since = view.at_ms - window_s * 1000
    if not_before_ms is not None:
        since = max(since, int(not_before_ms))
    where = ("chain=? AND token=? AND ts_ms>=? AND price_usd IS NOT NULL "
             "AND amount_native IS NOT NULL")
    params: list[Any] = [chain.value, token, since]
    if program:
        where += " AND program=?"
        params.append(program)
    rows = view.rows(
        "swaps",
        where=where,
        params=tuple(params),
        order_by="ts_ms ASC, slot ASC, block_index ASC, id ASC",
    )
    solves: list[Decimal] = []
    for prev, cur in zip(rows, rows[1:], strict=False):
        p0 = _dec(prev.get("price_usd"))
        p1 = _dec(cur.get("price_usd"))
        native = _dec(cur.get("amount_native"))
        if p0 is None or p1 is None or native is None or p0 <= 0 or p1 <= 0 or native <= 0:
            continue
        signed = native if str(cur.get("side")) == "buy" else -native
        try:
            root = (p1 / p0).sqrt()
        except (InvalidOperation, ValueError):
            continue
        if abs(root - 1) < Decimal("1e-12"):
            continue
        x = signed / (root - 1)
        if x > 0:
            solves.append(x)

    if len(solves) < min_solves:
        return Depth(None, EvidenceBasis.UNAVAILABLE, f"too_few_solves:{len(solves)}", len(solves), None)
    med = _median(solves)
    if med <= 0:
        return Depth(None, EvidenceBasis.UNAVAILABLE, "non_positive_median", len(solves), None)
    inliers = sum(1 for s in solves if abs(s - med) <= inlier_tol * med)
    frac = Decimal(inliers) / Decimal(len(solves))
    if frac < min_inlier_frac:
        return Depth(
            None, EvidenceBasis.UNAVAILABLE, f"tape_not_contiguous:{frac:.2f}", len(solves), frac
        )
    return Depth(int(med), EvidenceBasis.ESTIMATED, "cpmm_solve_from_tape", len(solves), frac)


# ---------------------------------------------------------------------------- episodes


@dataclass(frozen=True)
class Episode:
    """One recorded token episode, with the clock it is measured from."""

    chain: Chain
    token: str
    t0_ms: int
    t0_basis: str
    horizon_s: int
    swaps_in_window: int
    priced_in_window: int
    eligible: bool
    reason: str

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.chain.value, self.token, self.horizon_s)


@dataclass(frozen=True)
class EpisodeCensus:
    """The denominator, reported before any result.

    ``counts`` is horizon -> total considered. ``eligible`` is horizon -> passed. ``by_reason``
    is horizon -> reason -> count, and the reasons are the whole point: a run whose
    exclusions are 90% ``no_depth`` is a run about our tape, not about the market.
    """

    counts: Mapping[int, int]
    eligible: Mapping[int, int]
    by_reason: Mapping[int, Mapping[str, int]]
    by_min_swaps: Mapping[int, Mapping[int, int]] = field(default_factory=dict)
    t0_basis: str = ""
    universe: str = ""

    #: The bias the census cannot remove, printed with it so nobody has to remember it.
    SELECTION_WARNING = (
        "SELECTION: an episode is eligible because we HOLD tape for it. A migration "
        "excluded for no_tape_in_window is not a token that did not trade - it may be "
        "one our collector never walked. Eligibility therefore correlates with activity, "
        "and every number below is conditioned on that. The only fix is block-complete "
        "ingest, which we do not have: our swaps come from a third-party aggregator "
        "websocket with no history endpoint, so the excluded rows cannot be backfilled."
    )

    def lines(self) -> list[str]:
        """The block printed at the top of every report."""
        out = [f"episode census ({self.universe}; clock = {self.t0_basis})",
               f"  {self.SELECTION_WARNING}"]
        for h in sorted(self.counts):
            total = self.counts[h]
            ok = self.eligible.get(h, 0)
            out.append(f"  H={h:>4}s  considered {total:>5}  eligible {ok:>5}")
            for reason, n in sorted(
                self.by_reason.get(h, {}).items(), key=lambda kv: -kv[1]
            ):
                out.append(f"            {reason:<28} {n:>5}")
            sens = self.by_min_swaps.get(h)
            if sens:
                shown = "  ".join(f"min_swaps={k}: {v}" for k, v in sorted(sens.items()))
                out.append(f"            sensitivity: {shown}")
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "counts": {str(k): v for k, v in self.counts.items()},
            "eligible": {str(k): v for k, v in self.eligible.items()},
            "by_reason": {str(k): dict(v) for k, v in self.by_reason.items()},
            "by_min_swaps": {str(k): {str(a): b for a, b in v.items()}
                             for k, v in self.by_min_swaps.items()},
            "t0_basis": self.t0_basis,
            "universe": self.universe,
        }


def discover_episodes(
    conn: sqlite3.Connection,
    *,
    chain: Chain = Chain.SOL,
    horizons_s: Sequence[int] = DEFAULT_HORIZONS_S,
    min_swaps: int = MIN_EPISODE_SWAPS,
    limit: int | None = None,
) -> tuple[list[Episode], EpisodeCensus]:
    """Every migration we recorded, whether or not it can be replayed.

    The clock is the ``token.migrated`` event's own ``ts_ms``. ``tokens.migrated_ms`` is
    not used and cannot be: it is ``now_ms()`` at ingest on 1,067 of 1,068 rows (one is
    second-granular, which is consistent with coincidence), the row is mutated in place,
    and a replay keyed on it would be comparing our collector's wall clock against the
    venue's block time. The event row is timestamped and never rewritten.

    Every candidate is returned, eligible or not. Callers that filter should filter on
    ``Episode.eligible`` so the census travels with the sample.
    """
    events = fetch_all(
        conn,
        "SELECT subject, MIN(ts_ms) AS t0_ms FROM events WHERE kind=? AND chain=? "
        "AND subject IS NOT NULL GROUP BY subject ORDER BY t0_ms ASC",
        (EventKind.TOKEN_MIGRATED.value, chain.value),
    )
    if limit is not None:
        events = events[: int(limit)]

    episodes: list[Episode] = []
    counts: dict[int, int] = {}
    eligible: dict[int, int] = {}
    reasons: dict[int, dict[str, int]] = {}
    sens: dict[int, dict[int, int]] = {}
    probe_levels = (4, min_swaps, min_swaps * 2)

    for row in events:
        token = str(row["subject"])
        t0 = int(row["t0_ms"])
        for h in horizons_s:
            window_end = t0 + h * 1000
            stat = fetch_one(
                conn,
                "SELECT COUNT(*) AS n, SUM(CASE WHEN price_usd IS NOT NULL "
                "AND amount_native IS NOT NULL THEN 1 ELSE 0 END) AS priced FROM swaps "
                "WHERE chain=? AND token=? AND ts_ms>? AND ts_ms<=?",
                (chain.value, token, t0, window_end),
            )
            n = int(stat["n"] or 0) if stat else 0
            priced = int(stat["priced"] or 0) if stat else 0
            if priced >= min_swaps:
                ok, reason = True, "eligible"
            elif n == 0:
                ok, reason = False, "no_tape_in_window"
            elif priced == 0:
                ok, reason = False, "tape_has_no_prices"
            else:
                ok, reason = False, f"too_few_priced_swaps(<{min_swaps})"
            episodes.append(
                Episode(chain, token, t0, T0_BASES[0], h, n, priced, ok, reason)
            )
            counts[h] = counts.get(h, 0) + 1
            eligible[h] = eligible.get(h, 0) + (1 if ok else 0)
            reasons.setdefault(h, {})
            reasons[h][reason] = reasons[h].get(reason, 0) + 1
            bucket = sens.setdefault(h, {})
            for level in probe_levels:
                bucket[level] = bucket.get(level, 0) + (1 if priced >= level else 0)

    census = EpisodeCensus(
        counts=counts,
        eligible=eligible,
        by_reason=reasons,
        by_min_swaps=sens,
        t0_basis=T0_BASES[0],
        universe=f"{len(events)} recorded {chain.value} migrations",
    )
    return episodes, census


def record_census(
    conn: sqlite3.Connection, episodes: Sequence[Episode], *, at_ms: int | None = None
) -> int:
    """Write the denominator to ``replay_episodes``. Returns rows written."""
    ts = int(at_ms if at_ms is not None else now_ms())
    n = 0
    for ep in episodes:
        conn.execute(
            "INSERT OR REPLACE INTO replay_episodes (chain, token, horizon_s, t0_ms, "
            "t0_basis, swaps_in_window, priced_in_window, eligible, reason, observed_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ep.chain.value, ep.token, ep.horizon_s, ep.t0_ms, ep.t0_basis,
             ep.swaps_in_window, ep.priced_in_window, 1 if ep.eligible else 0, ep.reason, ts),
        )
        n += 1
    return n


# ---------------------------------------------------------------------------- the fill


class Arm(StrEnum):
    """Which side of the lane's thesis is being replayed."""

    LONG = "long"
    SHORT_MIRROR = "short_mirror"

    @property
    def executable(self) -> bool:
        """Whether a venue exists at which this arm could have been filled.

        ``SHORT_MIRROR`` is False and that is the finding, not a limitation of this code:
        a pump.fun bonding curve has no borrow and no perp, and PumpSwap has neither. The
        mirror charges the same round-trip costs the long leg actually paid and charges
        nothing for borrow, funding, recall or liquidation, so it is an UPPER BOUND on
        what a short would have returned in a world where one could be opened.
        """
        return self is Arm.LONG


class ReplayBroker:
    """``kaiba.execution.paper.PaperBroker`` against a scratch database.

    There is no second fill model here. This class owns an in-memory database so the
    broker can write the orders, positions and trades rows it needs in order to work, and
    so a replay cannot touch the live one; every price and every depth handed to it comes
    from a :class:`PointInTime` view. The numbers it returns are the broker's own, read
    back out of the ``trades`` row it wrote.
    """

    def __init__(self, *, slippage_bps: int | None = None, decimals: int = 6) -> None:
        self.scratch = sqlite3.connect(":memory:", timeout=10, isolation_level=None,
                                       check_same_thread=False)
        self.scratch.row_factory = sqlite3.Row
        self.scratch.execute("PRAGMA foreign_keys=ON")
        db.migrate(self.scratch)
        kw: dict[str, Any] = {}
        if slippage_bps is not None:
            kw["slippage_bps"] = int(slippage_bps)
        self.broker = paper.PaperBroker(self.scratch, **kw)
        self.decimals = int(decimals)

    def close(self) -> None:
        self.scratch.close()

    def _stage_token(self, chain: Chain, token: str, t0_ms: int) -> None:
        """Declare the token graduated in the scratch DB.

        ``PaperBroker.resolve_curve`` reads ``tokens.migrated_ms`` and returns
        ``(None, "graduated")`` when it is set, which sends the fill down the pool path -
        the correct one for a post-migration episode, and the one our 44 real trades took.
        Writing it here rather than reading it from the live database is also what keeps
        the live ``tokens`` row (mutated in place, therefore unreadable as of any past
        instant) out of the replay.
        """
        self.scratch.execute(
            "INSERT OR REPLACE INTO tokens (chain, address, decimals, migrated_ms, "
            "first_seen_ms) VALUES (?,?,?,?,?)",
            (chain.value, token, self.decimals, t0_ms, t0_ms),
        )

    def round_trip(
        self,
        *,
        chain: Chain,
        token: str,
        lane: Lane,
        t0_ms: int,
        entry_ms: int,
        exit_ms: int,
        size_base_units: int,
        entry_price_usd: Decimal,
        exit_price_usd: Decimal,
        entry_liquidity_usd: Decimal,
        exit_liquidity_usd: Decimal,
        entry_sol_usd: Decimal,
        exit_sol_usd: Decimal,
        exit_reason: str,
        decision_seed: str,
    ) -> dict[str, Any]:
        """One modelled buy and one modelled sell. Returns the broker's own accounting."""
        self._stage_token(chain, token, t0_ms)
        decision = Decision(
            decision_id="rpl_" + digest({"seed": decision_seed})[:24],
            ts_ms=entry_ms,
            lane=lane,
            mode=LaneMode.SHADOW,
            chain=chain,
            token=token,
            action=Action.ENTER,
            thesis="replay",
            dossier_grade=Grade.UNSCORED,
            size_base_units=int(size_base_units),
        )
        self.broker.native_usd[chain] = entry_sol_usd
        buy = self.broker.buy(
            decision,
            price_usd=entry_price_usd,
            liquidity_usd=entry_liquidity_usd,
            now_ms=entry_ms,
            decimals=self.decimals,
        )
        if buy.state is not OrderState.FILLED:
            return {"filled": False, "refused_reason": _refusal(self.scratch, buy.order_id)}

        position = self.broker.open_position(chain, token, lane, LaneMode.SHADOW)
        if position is None:
            return {"filled": False, "refused_reason": "position_not_opened"}

        self.broker.native_usd[chain] = exit_sol_usd
        sell = self.broker.sell(
            position,
            100,
            price_usd=exit_price_usd,
            liquidity_usd=exit_liquidity_usd,
            now_ms=exit_ms,
            decimals=self.decimals,
            exit_reason=exit_reason,
        )
        if sell.state is not OrderState.FILLED:
            return {"filled": False, "refused_reason": _refusal(self.scratch, sell.order_id)}

        trade = fetch_one(
            self.scratch,
            "SELECT * FROM trades WHERE position_id=? ORDER BY closed_ms DESC LIMIT 1",
            (position.position_id,),
        )
        if trade is None:
            return {"filled": False, "refused_reason": "no_trade_row"}
        entry_basis = paper.fill_basis(self.scratch, buy.order_id) or {}
        exit_basis = paper.fill_basis(self.scratch, sell.order_id) or {}
        return {
            "filled": True,
            "trade": dict(trade),
            "entry_basis": entry_basis,
            "exit_basis": exit_basis,
            "buy_order_id": buy.order_id,
            "sell_order_id": sell.order_id,
            "costs": cost_terms(
                entry_basis, exit_basis,
                size_base_units=int(size_base_units),
                tip_base_units=int(self.broker.tip_native.get(chain, 0)),
            ),
        }

    def reset(self) -> None:
        """Drop the scratch state between configurations so nothing carries over."""
        for table in ("trades", "positions", "position_orders", "orders", "order_events",
                      "position_marks", "tokens", "events", "decisions"):
            try:
                self.scratch.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed list
            except sqlite3.Error:
                pass


def _refusal(conn: sqlite3.Connection, order_id: str) -> str:
    row = fetch_one(
        conn,
        "SELECT detail FROM order_events WHERE order_id=? ORDER BY id DESC LIMIT 1",
        (order_id,),
    )
    if not row or not row["detail"]:
        return "refused"
    return str(row["detail"])[:200]


@dataclass(frozen=True)
class CostTerms:
    """What the fill model charged, split so a mirrored position can be charged the same.

    Read off the broker's own fill receipts and its own tip configuration rather than
    re-derived, because a second cost accounting that disagreed with the one inside the
    fill would make every number we already have incomparable. The three terms reproduce
    the broker's booked PnL exactly:

        ``net = (1 - c_in) * (1 + gross) * (1 - c_out) - 1 - c_tip``

    ``c_in`` is multiplicative on the way in (venue fee off the input, then impact and the
    slippage floor on the price). ``c_out`` is multiplicative on the way out. ``c_tip`` is
    the exit priority fee, which is flat and therefore not multiplicative - at our
    0.045-0.1 SOL ticket it is the largest of the three, which is the whole reason it is
    not folded into a bps figure and forgotten.
    """

    c_in: Decimal
    c_out: Decimal
    c_tip: Decimal

    @property
    def round_trip(self) -> Decimal:
        return self.c_in + self.c_out + self.c_tip


def cost_terms(
    entry_basis: Mapping[str, Any],
    exit_basis: Mapping[str, Any],
    *,
    size_base_units: int,
    tip_base_units: int,
) -> CostTerms | None:
    """Recover :class:`CostTerms` from two fill receipts. ``None`` if either is incomplete."""
    f_in = _dec(entry_basis.get("fee_bps"))
    s_in = _dec(entry_basis.get("total_slippage_bps"))
    f_out = _dec(exit_basis.get("fee_bps"))
    s_out = _dec(exit_basis.get("total_slippage_bps"))
    if f_in is None or s_in is None or f_out is None or s_out is None:
        return None
    size = Decimal(int(size_base_units))
    tip = Decimal(int(tip_base_units))
    if size <= 0 or size + tip <= 0:
        return None
    # The entry tip is inside the cost basis the broker divides by, so it belongs to
    # c_in; the exit tip is subtracted from proceeds and is flat, so it stands alone.
    entry_eff = (1 - f_in / BPS) / (1 + s_in / BPS) * size / (size + tip)
    exit_eff = (1 - s_out / BPS) * (1 - f_out / BPS)
    return CostTerms(
        c_in=1 - entry_eff,
        c_out=1 - exit_eff,
        c_tip=tip / (size + tip),
    )


# ------------------------------------------------------------------------ the replay


@dataclass(frozen=True)
class ReplayConfig:
    """Every knob one replay run turns. Frozen, hashed into the trial id.

    ``lane_overrides`` are merged on top of ``DEFAULT_PARAMS`` and ``config/risk.yaml`` by
    ``LaneContext.lane_params``; what gets registered is the EFFECTIVE merged result, not
    this dict, so a sweep that varies one key cannot register as one trial.
    """

    lane: Lane = Lane.MIGRATION_FADE
    arm: Arm = Arm.LONG
    chain: Chain = Chain.SOL
    entry_delay_s: int = 60
    entry_window_s: int = ENTRY_WINDOW_S
    poll_s: int = POLL_S
    horizon_s: int = 300
    size_base_units: int = 60_000_000
    slippage_bps: int | None = None
    price_max_age_s: int = PRICE_MAX_AGE_S
    exit_price_max_age_s: int = EXIT_PRICE_MAX_AGE_S
    depth_window_s: int = DEPTH_WINDOW_S
    depth_min_inlier_frac: Decimal = DEPTH_MIN_INLIER_FRAC
    depth_program: str | None = DEPTH_PROGRAM
    lane_overrides: tuple[tuple[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane.value,
            "arm": self.arm.value,
            "chain": self.chain.value,
            "entry_delay_s": self.entry_delay_s,
            "entry_window_s": self.entry_window_s,
            "poll_s": self.poll_s,
            "horizon_s": self.horizon_s,
            "size_base_units": self.size_base_units,
            "slippage_bps": self.slippage_bps,
            "price_max_age_s": self.price_max_age_s,
            "exit_price_max_age_s": self.exit_price_max_age_s,
            "depth_window_s": self.depth_window_s,
            "depth_min_inlier_frac": str(self.depth_min_inlier_frac),
            "depth_program": self.depth_program,
            "lane_overrides": dict(self.lane_overrides),
        }


@dataclass(frozen=True)
class EpisodeOutcome:
    """One episode replayed through one configuration."""

    token: str
    t0_ms: int
    entry_ms: int | None
    exit_ms: int | None
    entry_price_usd: Decimal | None
    exit_price_usd: Decimal | None
    depth: Depth
    gross_return: Decimal | None
    net_return: Decimal | None
    costs: CostTerms | None
    fill_basis: str | None
    signal_strength: Decimal | None
    refused_reason: str | None
    #: Seconds between the print the exit was marked at and the exit deadline. A large
    #: value means the position was marked out on a stale tape, which is achievable only
    #: in the sense that nobody else traded either.
    exit_price_age_s: int | None = None
    #: SOL/USD at entry divided by SOL/USD at exit. The tape prices tokens in USD and the
    #: book is denominated in lamports, so a move in SOL between the legs lands in the
    #: realised return whether or not the token moved. Carried explicitly rather than
    #: assumed to be 1, because assuming it is 1 is what made the cost reconciliation
    #: fail by 0.6% of notional on the first run of this module.
    fx: Decimal | None = None

    @property
    def filled(self) -> bool:
        return self.net_return is not None


@dataclass
class ArmResult:
    """One configuration's result, with its denominator and its deflation attached."""

    run_id: str
    config: ReplayConfig
    trial_id: str
    effective_params: dict[str, Any]
    outcomes: list[EpisodeOutcome]
    census: EpisodeCensus
    considered: int
    honest_trials: int | None = None
    dsr: float | None = None
    dsr_notes: list[str] = field(default_factory=list)
    deflation_ok: bool = False
    pbo: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def filled(self) -> list[EpisodeOutcome]:
        return [o for o in self.outcomes if o.filled]

    @property
    def returns(self) -> list[Decimal]:
        return [o.net_return for o in self.filled if o.net_return is not None]

    @property
    def gross(self) -> list[Decimal]:
        return [o.gross_return for o in self.filled if o.gross_return is not None]

    @property
    def n(self) -> int:
        return len(self.returns)

    @property
    def refusals(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.outcomes:
            if o.refused_reason:
                key = o.refused_reason.split(" ")[0].split(":")[0]
                out[key] = out.get(key, 0) + 1
        return out

    def mean(self) -> Decimal | None:
        return _mean(self.returns)

    def ci(self) -> tuple[Decimal, Decimal] | None:
        return bootstrap_ci(self.returns)

    def round_trip_cost(self) -> Decimal | None:
        """Mean round-trip cost as a fraction of notional, from the broker's receipts."""
        vals = [o.costs.round_trip for o in self.filled if o.costs is not None]
        return _mean(vals) if vals else None


def lane_context(
    view: PointInTime, episode: Episode, cfg: ReplayConfig
) -> lanes.LaneContext:
    """Assemble what the lane is allowed to see at the cursor, and nothing else.

    ``conn=None`` is the load-bearing line. ``LaneContext`` accepts a live connection and
    several lanes use it to run their own unbounded queries - ``_event_payload`` reads the
    newest matching event with no time filter at all, and ``_score`` reads the current
    wallet score. Handing a replay that connection would reintroduce every lookahead this
    module removes, one lane helper at a time. So the lane gets no handle, and everything
    it may consult is assembled here from the point-in-time view.

    ``migration_fade`` reads exactly ``extras["migration_ms"]`` and ``now_ms``, which is
    why it is the lane this harness can answer for today. A lane that needs a dossier will
    find ``dossier=None`` unless one was genuinely built before the cursor, which for
    migration episodes is 23 of 1,061 - the honest number, rather than a dossier built
    minutes later being read as evidence at T.
    """
    dossier_row = view.one(
        "token_dossiers",
        where="chain=? AND address=?",
        params=(cfg.chain.value, episode.token),
        order_by="built_at_ms DESC",
    )
    return lanes.LaneContext(
        chain=cfg.chain,
        token=episode.token,
        now_ms=view.at_ms,
        conn=None,
        dossier=None,
        recent_buys=[],
        token_meta=None,
        curve=None,
        caller=None,
        params=dict(cfg.lane_overrides),
        extras={
            "migration_ms": episode.t0_ms,
            "dossier_available_at_cursor": bool(dossier_row),
        },
    )


def _price_at(
    view: PointInTime, cfg: ReplayConfig, token: str, *, max_age_s: int | None = None
) -> tuple[Decimal | None, int | None]:
    age = cfg.price_max_age_s if max_age_s is None else int(max_age_s)
    row = view.one(
        "swaps",
        where="chain=? AND token=? AND price_usd IS NOT NULL AND ts_ms>=?",
        params=(cfg.chain.value, token, view.at_ms - age * 1000),
        order_by="ts_ms DESC, slot DESC, block_index DESC, id DESC",
    )
    if row is None:
        return None, None
    return _dec(row.get("price_usd")), int(row["ts_ms"])


def _sol_usd_at(view: PointInTime, chain: Chain) -> Decimal | None:
    row = view.one(
        "native_prices",
        where="chain=? AND ts_ms>=?",
        params=(chain.value, view.at_ms - SOL_USD_MAX_AGE_S * 1000),
        order_by="ts_ms DESC",
    )
    return _dec(row.get("price_usd")) if row else None


def replay_episode(
    conn: sqlite3.Connection,
    episode: Episode,
    cfg: ReplayConfig,
    broker: ReplayBroker,
) -> EpisodeOutcome:
    """Replay one episode through the lane and the fill model, as of the cursor.

    The sequence is the live one, including the polling. A running agent does not act at
    one instant chosen in advance; it wakes on a timer, looks at what has arrived, and
    acts on the first tick where the lane fires and the trade is priceable. So this walks
    a fixed grid of ``poll_s`` instants from ``t0 + entry_delay_s`` to the end of the
    lane's own window, builds a fresh :class:`PointInTime` at each one, and takes the
    first tick that clears. Nothing about the grid depends on data from later than the
    tick being evaluated - it is a wall clock, which is exactly what the live loop has.

    Fixing the entry at a single instant instead is what a naive backtest does, and on
    this data it is also badly biased: it silently discards every episode whose tape
    happened not to print in that one 30-second window, which is most of them, and the
    ones it keeps are the busiest tapes.
    """
    try:
        evaluator = lanes.LANES[cfg.lane]
    except KeyError:  # pragma: no cover - Lane.MANUAL has no evaluator
        raise ValueError(f"{cfg.lane} has no lane function to replay") from None

    start_ms = episode.t0_ms + cfg.entry_delay_s * 1000
    last_ms = episode.t0_ms + cfg.entry_window_s * 1000
    step = max(cfg.poll_s, 1) * 1000
    entry_ms = start_ms
    strength: Decimal | None = None
    depth = UNKNOWN_DEPTH
    reason = "entry_window_empty"
    view: PointInTime | None = None
    entry_px: Decimal | None = None
    entry_sol_usd: Decimal | None = None
    entry_liq: Decimal | None = None
    signal = None

    for tick in range(start_ms, last_ms + 1, step):
        probe = PointInTime(conn, tick)
        candidate = evaluator(lane_context(probe, episode, cfg))
        if candidate is None:
            reason = "lane_no_signal"
            continue
        strength = _dec(candidate.strength)
        px, _ = _price_at(probe, cfg, episode.token)
        if px is None or px <= 0:
            reason = "no_entry_price"
            continue
        usd = _sol_usd_at(probe, cfg.chain)
        if usd is None or usd <= 0:
            reason = "no_sol_usd"
            continue
        got_depth = implied_pool(
            probe, cfg.chain, episode.token,
            window_s=cfg.depth_window_s, min_inlier_frac=cfg.depth_min_inlier_frac,
            not_before_ms=episode.t0_ms, program=cfg.depth_program,
        )
        liq = got_depth.usd(usd)
        if liq is None:
            depth = got_depth
            reason = f"no_depth:{got_depth.source}"
            continue
        entry_ms, view, signal = tick, probe, candidate
        entry_px, entry_sol_usd, entry_liq, depth = px, usd, liq, got_depth
        break

    if view is None or signal is None or entry_px is None:
        return _miss(episode, entry_ms, reason, strength, depth)
    assert entry_sol_usd is not None and entry_liq is not None

    # The lane's own deadline caps the hold: migration-fade carries exit_deadline_ms in
    # its payload and says never_hold_through_migration. A replay that held past the
    # lane's own deadline would be testing a different rule from the one that fired.
    deadline = signal.payload.get("exit_deadline_ms") if isinstance(signal.payload, dict) else None
    exit_ms = entry_ms + cfg.horizon_s * 1000
    if isinstance(deadline, int) and deadline > entry_ms:
        exit_ms = min(exit_ms, deadline)

    exit_view = view.at(exit_ms)
    exit_px, exit_px_ms = _price_at(
        exit_view, cfg, episode.token, max_age_s=cfg.exit_price_max_age_s
    )
    if exit_px is None or exit_px <= 0 or exit_px_ms is None:
        return _miss(episode, entry_ms, "no_exit_price", strength, depth, exit_ms)
    exit_sol_usd = _sol_usd_at(exit_view, cfg.chain)
    if exit_sol_usd is None or exit_sol_usd <= 0:
        return _miss(episode, entry_ms, "no_exit_sol_usd", strength, depth, exit_ms)
    exit_depth = implied_pool(
        exit_view, cfg.chain, episode.token,
        window_s=cfg.depth_window_s, min_inlier_frac=cfg.depth_min_inlier_frac,
        not_before_ms=episode.t0_ms, program=cfg.depth_program,
    )
    exit_liq = exit_depth.usd(exit_sol_usd)
    if exit_liq is None:
        # Deliberately NOT falling back to the entry depth. A pool we cannot measure at
        # the exit is the case where an exit fails, and substituting the depth we had on
        # the way in would model our way out of exactly the situation that costs money.
        return _miss(
            episode, entry_ms, f"no_exit_depth:{exit_depth.source}", strength, depth, exit_ms
        )

    got = broker.round_trip(
        chain=cfg.chain,
        token=episode.token,
        lane=cfg.lane,
        t0_ms=episode.t0_ms,
        entry_ms=entry_ms,
        exit_ms=exit_ms,
        size_base_units=cfg.size_base_units,
        entry_price_usd=entry_px,
        exit_price_usd=exit_px,
        entry_liquidity_usd=entry_liq,
        exit_liquidity_usd=exit_liq,
        entry_sol_usd=entry_sol_usd,
        exit_sol_usd=exit_sol_usd,
        exit_reason="replay_horizon",
        decision_seed=f"{cfg.as_dict()}|{episode.token}|{entry_ms}",
    )
    if not got.get("filled"):
        return _miss(
            episode, entry_ms, str(got.get("refused_reason") or "refused"),
            strength, depth, exit_ms, entry_px, exit_px,
        )

    trade = got["trade"]
    cost = _dec(trade.get("cost_native"))
    pnl = _dec(trade.get("pnl_native"))
    net = (pnl / cost) if (cost and cost > 0 and pnl is not None) else None
    gross = exit_px / entry_px - 1
    costs = got.get("costs")

    fx = entry_sol_usd / exit_sol_usd
    if cfg.arm is Arm.SHORT_MIRROR:
        net = short_mirror_return(gross, costs, fx)

    return EpisodeOutcome(
        token=episode.token,
        t0_ms=episode.t0_ms,
        entry_ms=entry_ms,
        exit_ms=exit_ms,
        entry_price_usd=entry_px,
        exit_price_usd=exit_px,
        depth=depth,
        gross_return=gross,
        net_return=net,
        costs=costs,
        fill_basis=str((got.get("entry_basis") or {}).get("basis") or FillBasis.DEX.value),
        signal_strength=strength,
        refused_reason=None,
        exit_price_age_s=int((exit_ms - exit_px_ms) / 1000),
        fx=fx,
    )


def short_mirror_return(
    gross: Decimal, costs: CostTerms | None, fx: Decimal | None = None
) -> Decimal | None:
    """What a short would have returned, if a short could have been opened. It could not.

    Open by selling one unit of notional and receiving ``1 - c_out``; close by buying it
    back, which at ``(1 + gross)`` of mid costs ``(1 + gross) / (1 - c_in)`` because the
    fill model's entry efficiency is multiplicative. The flat exit tip is charged once,
    exactly as on the long. The costs are the ones the long leg actually paid, read off
    the broker's own receipts, so the two arms are charged identically and the comparison
    is not contaminated by a second cost model.

    What this deliberately does NOT charge: borrow, funding, recall risk and liquidation.
    There is no lending market for a two-hour-old pump.fun mint and no perpetual listing
    it, so those costs are not merely unmeasured - the venue at which they would be
    quoted does not exist. This number is therefore an **upper bound** on a position
    nobody could have taken, and it is reported as one.
    """
    if costs is None or costs.c_in >= 1:
        return None
    move = (1 + gross) * (fx if fx is not None else Decimal(1))
    return (1 - costs.c_out) - move / (1 - costs.c_in) - costs.c_tip


def _miss(
    episode: Episode,
    entry_ms: int,
    reason: str,
    strength: Decimal | None = None,
    depth: Depth = UNKNOWN_DEPTH,
    exit_ms: int | None = None,
    entry_px: Decimal | None = None,
    exit_px: Decimal | None = None,
) -> EpisodeOutcome:
    return EpisodeOutcome(
        token=episode.token, t0_ms=episode.t0_ms, entry_ms=entry_ms, exit_ms=exit_ms,
        entry_price_usd=entry_px, exit_price_usd=exit_px, depth=depth,
        gross_return=None, net_return=None, costs=None,
        fill_basis=None, signal_strength=strength, refused_reason=reason,
    )


def run_config(
    conn: sqlite3.Connection,
    cfg: ReplayConfig,
    episodes: Sequence[Episode],
    census: EpisodeCensus,
    *,
    registry_conn: sqlite3.Connection | None = None,
) -> ArmResult:
    """Replay every eligible episode through one configuration, registering it first.

    The registration happens BEFORE a single outcome is read, and it is keyed on the
    *effective merged* lane parameters plus this module's own knobs. A sweep of 500
    variants therefore writes 500 rows into ``lane_trials`` whether or not anyone reports
    499 of them, which is the whole mechanism by which the deflated Sharpe stays honest.
    """
    usable = [e for e in episodes if e.eligible and e.horizon_s == cfg.horizon_s]
    probe = lanes.LaneContext(
        chain=cfg.chain, token="0" * 32, now_ms=0, conn=None,
        params=dict(cfg.lane_overrides),
    )
    effective = probe.lane_params(cfg.lane)
    params = {"lane_params": effective, "replay": cfg.as_dict()}
    trial_id = register_or_refuse(cfg.lane, params, registry_conn or get_conn())
    run_id = "rpl_" + digest({"trial": trial_id, "n": len(usable), "at": now_ms()})[:20]

    broker = ReplayBroker(slippage_bps=cfg.slippage_bps)
    try:
        outcomes = [replay_episode(conn, ep, cfg, broker) for ep in usable]
    finally:
        broker.close()

    result = ArmResult(
        run_id=run_id, config=cfg, trial_id=trial_id, effective_params=effective,
        outcomes=outcomes, census=census, considered=len(usable),
    )
    _reconcile_costs(result)
    # The cost model is fitted over OUR closed trades, so it is read from the data
    # connection, not from wherever the registry happens to live.
    note = cost_model_cross_check(result, conn)
    if note:
        result.notes.append(note)
    return result


def _reconcile_costs(result: ArmResult) -> None:
    """Check the cost fractions we recovered against the profit the broker itself booked.

    ``net = (1 - c_in)(1 + gross)(1 - c_out) - 1 - c_tip`` should reproduce the broker's
    own ``pnl_native / cost_native`` on the long arm. When it does not, the cost recovery
    is wrong and every short-mirror number built on it is wrong with it, so the
    disagreement is written into ``notes`` rather than left for a reader to discover.

    ``PaperBroker`` clamps a sell's proceeds at zero (``max(0, gross - fee - tip)``), so a
    position wiped out by a collapse books exactly -100% while the multiplicative identity
    lands slightly short of it. That is a floor, not a cost-recovery error, and those
    episodes are counted separately rather than being allowed to fail the check or to be
    quietly dropped from it.
    """
    if result.config.arm is not Arm.LONG:
        return
    gaps: list[Decimal] = []
    wiped = 0
    for o in result.filled:
        if o.gross_return is None or o.net_return is None or o.costs is None:
            continue
        if o.net_return <= Decimal("-1"):
            wiped += 1
            continue
        c = o.costs
        fx = o.fx if o.fx is not None else Decimal(1)
        modelled = (1 - c.c_in) * (1 + o.gross_return) * fx * (1 - c.c_out) - 1 - c.c_tip
        gaps.append(abs(modelled - o.net_return))
    tail = (
        f" {wiped} position(s) booked a total loss, where the broker's proceeds clamp at "
        "zero and the multiplicative identity does not apply; they are excluded from the "
        "comparison and counted here instead." if wiped else ""
    )
    if not gaps:
        result.notes.append(
            f"cost reconciliation: nothing to compare.{tail}" if wiped else
            "cost reconciliation: nothing to compare."
        )
        return
    worst = max(gaps)
    if worst > COST_RECONCILE_TOL:
        result.notes.append(
            f"cost reconciliation FAILED: the fractions recovered from the broker's fill "
            f"receipts disagree with the broker's booked PnL by up to {worst:.4f} of "
            f"notional (tolerance {COST_RECONCILE_TOL}) over n={len(gaps)}. The "
            f"short-mirror arm is built on those fractions and inherits the error.{tail}"
        )
    else:
        result.notes.append(
            f"cost reconciliation OK: recovered fractions reproduce the broker's booked "
            f"PnL to within {worst:.5f} of notional over n={len(gaps)}.{tail}"
        )


class UnregisteredTrialError(RuntimeError):
    """A configuration was about to be replayed without being counted as a trial."""


def register_or_refuse(
    lane: Lane, params: Mapping[str, Any], conn: sqlite3.Connection
) -> str:
    """Register the configuration and PROVE the row landed, or refuse to run it.

    ``registry.register`` deliberately never raises: it sits on the live decision path,
    where losing a decision because a measurement failed would be the worse bug. On a
    replay the trade-off inverts. A run whose trial row was silently dropped is exactly
    the 500-variants-one-winner failure this harness exists to prevent, and it would
    produce a number rather than an error. So the write is verified by reading it back,
    and an unverifiable registration stops the run.
    """
    tid = registry.register(lane, dict(params), conn, source="replay", count_decision=True)
    try:
        row = fetch_one(conn, "SELECT trial_id FROM lane_trials WHERE trial_id=?", (tid,))
    except sqlite3.Error:
        row = None
    if row is None:
        raise UnregisteredTrialError(
            f"configuration {tid} for {lane.value} could not be written to lane_trials. "
            "Refusing to replay: an unregistered configuration does not deflate, and a "
            "result that does not deflate is worse than no result."
        )
    return tid


def cost_model_cross_check(
    result: ArmResult, conn: sqlite3.Connection | None = None
) -> str | None:
    """Compare the fill model's realised cost to the measured Theil-Sen cost model.

    ``viability.cost_model`` is a robust fit over our own closed trades: flat base units
    per leg plus proportional bps per leg. The paper broker charges its own venue fee,
    modelled impact and a slippage floor. These are two accountings of the same thing and
    they must not be added together, so this compares them instead. A large gap is a real
    finding - it means either the broker's fee schedule or the fitted model no longer
    describes what we pay - and it is reported rather than reconciled away.
    """
    model = viability.cost_model(result.config.chain, conn)
    charged = result.round_trip_cost()
    if charged is None:
        return None
    if not model.known:
        return f"measured cost model unavailable ({model.source}); nothing to compare against"
    flat = Decimal(int(model.flat_per_leg_base_units or 0))
    prop = Decimal(model.proportional_bps_per_leg or 0)
    size = Decimal(result.config.size_base_units)
    if size <= 0:
        return None
    predicted = (flat * 2 / size) + (prop * 2 / BPS)
    return (
        f"round-trip cost: fill model charged {charged * 100:.2f}% of notional; the "
        f"measured model ({model.source}, n={model.sample_trades}) predicts "
        f"{predicted * 100:.2f}% at this size. Gap {abs(charged - predicted) * 100:.2f}pp. "
        "These are two accountings of one cost and are never summed."
    )


# ------------------------------------------------------------------- deflation and CI


def block_matrix(
    results: Sequence[ArmResult], *, block_s: int = BLOCK_S
) -> list[list[float]]:
    """T calendar blocks by N configurations, for ``gates.pbo_cscv``.

    ``gates.pbo_cscv`` has never had a matrix to work on in this repository -
    ``validation``'s docstring concedes PBO is reported as unevaluable for exactly this
    reason. A replay produces one naturally: the row unit is a calendar block and the
    cell is that configuration's mean net return inside it. A block in which a
    configuration took no trade contributes 0, which is what a flat book returns.
    """
    if not results:
        return []
    stamps: set[int] = set()
    per_config: list[dict[int, list[Decimal]]] = []
    for res in results:
        buckets: dict[int, list[Decimal]] = {}
        for o in res.filled:
            if o.entry_ms is None or o.net_return is None:
                continue
            key = o.entry_ms // (block_s * 1000)
            buckets.setdefault(key, []).append(o.net_return)
            stamps.add(key)
        per_config.append(buckets)
    rows: list[list[float]] = []
    for key in sorted(stamps):
        row = []
        for buckets in per_config:
            vals = buckets.get(key)
            row.append(float(_mean(vals)) if vals else 0.0)
        rows.append(row)
    return rows


def deflate(
    conn: sqlite3.Connection,
    results: Sequence[ArmResult],
    *,
    lane: Lane,
    trials_conn: sqlite3.Connection | None = None,
) -> None:
    """Attach the honest trial count, the deflated Sharpe and PBO to every result.

    ``deflation_ok`` is the part that matters. ``gates.deflated_sharpe`` says in its own
    notes when fewer than two comparable trials existed and the deflation term collapsed
    to zero - at which point the number is a plain probabilistic Sharpe against zero, and
    reading it as a deflated Sharpe overstates the evidence by exactly the multiple-testing
    correction that was skipped. The flag makes that a failure rather than a footnote.
    """
    trials = validation.honest_trials(trials_conn or conn, lane)
    sharpes: list[float] = []
    for res in results:
        vals = [float(v) for v in res.returns]
        sr = gates._sharpe_raw(vals)
        if sr is not None:
            sharpes.append(sr)
    matrix = block_matrix(results)
    pbo = gates.pbo_cscv(matrix) if len(results) >= 2 else None
    # A long and its own mirror are not two strategies. They are one price path with the
    # sign flipped, so the in-sample winner is the out-of-sample loser by construction and
    # PBO comes back at exactly 1.0 with nothing overfitted. Reporting that as an
    # overfitting measure would be worse than reporting nothing.
    mirrored = len(results) == 2 and len({r.config.arm for r in results}) == 2 and (
        {k: v for k, v in results[0].config.as_dict().items() if k != "arm"}
        == {k: v for k, v in results[1].config.as_dict().items() if k != "arm"}
    )
    for res in results:
        res.honest_trials = trials
        dsr, notes = gates.deflated_sharpe(
            [float(v) for v in res.returns], trials,
            trial_sharpes=sharpes if len(sharpes) >= 2 else None,
        )
        res.dsr = dsr
        res.dsr_notes = list(notes)
        undeflated = any("deflation term is zero" in n for n in notes)
        res.deflation_ok = dsr is not None and not undeflated
        if dsr is None:
            res.notes.append(
                "DSR NOT COMPUTABLE: " + "; ".join(notes) + ". This is a failed gate, not "
                "a missing one - a configuration whose return series cannot carry a "
                "Sharpe cannot carry a promotion either."
            )
        elif undeflated:
            res.notes.append(
                "DSR NOT DEFLATED: fewer than two comparable trials with cross-trial "
                "variance, so this is a probabilistic Sharpe against zero. Treat the gate "
                "as failed, not as passed with a caveat."
            )
        res.pbo = None if mirrored else pbo
        if mirrored:
            res.notes.append(
                "PBO withheld: the only two configurations here are one long and its own "
                "mirror. They are the same price path with the sign flipped, so the "
                "in-sample winner is the out-of-sample loser by construction and PBO "
                "returns 1.0 whether or not anything was overfitted. Sweep several "
                "genuinely different configurations through replay.sweep() for a PBO that "
                "measures selection."
            )
        elif pbo is None:
            res.notes.append(
                "PBO unevaluable: needs at least two configurations and at least "
                f"{gates.DEFAULT_SPLITS} calendar blocks; the matrix was "
                f"{len(matrix)}x{len(matrix[0]) if matrix else 0}."
            )


def sweep(
    conn: sqlite3.Connection,
    configs: Sequence[ReplayConfig],
    episodes: Sequence[Episode],
    census: EpisodeCensus,
    *,
    lane: Lane = Lane.MIGRATION_FADE,
    registry_conn: sqlite3.Connection | None = None,
) -> list[ArmResult]:
    """Run several configurations and deflate them TOGETHER. The only supported sweep.

    Running :func:`run_config` in a loop and reporting the best result is the failure
    this whole module is built against, and it would be easy to do by accident. This is
    the path that makes it hard: every configuration is registered, the deflated Sharpe
    sees the cross-trial variance of all of them, and PBO gets the matrix it needs. The
    caller receives the whole list, not a winner.
    """
    results = [
        run_config(conn, cfg, episodes, census, registry_conn=registry_conn)
        for cfg in configs
    ]
    deflate(conn, results, lane=lane, trials_conn=registry_conn)
    if len(results) > 1:
        for res in results:
            res.notes.append(
                f"swept together with {len(results) - 1} other configuration(s); the "
                f"deflation and PBO above see all {len(results)}. Reporting one of these "
                "in isolation would undo that."
            )
    return results



def bootstrap_ci(
    values: Sequence[Decimal],
    *,
    alpha: Decimal = CI_ALPHA,
    draws: int = BOOTSTRAP_DRAWS,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[Decimal, Decimal] | None:
    """Percentile bootstrap interval for the mean. ``None`` below three observations.

    The resampling unit is the episode. That is the right unit for a cross-section of
    different tokens and the wrong unit for anything with a market-wide common factor:
    our whole sol universe was created inside one 27.9-hour window, so an interval from
    this function describes sampling error *within that regime* and says nothing about
    another one. ``validation.block_bootstrap_t`` is the day-block version and correctly
    refuses to run on two distinct days.
    """
    vals = [float(v) for v in values]
    n = len(vals)
    if n < 3:
        return None
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(max(draws, 100)):
        acc = 0.0
        for _ in range(n):
            acc += vals[rng.randrange(n)]
        means.append(acc / n)
    means.sort()
    lo_i = int(float(alpha) / 2 * len(means))
    hi_i = min(len(means) - 1, int((1 - float(alpha) / 2) * len(means)))
    return Decimal(str(means[lo_i])), Decimal(str(means[hi_i]))


# ------------------------------------------------------------------ direction report


class Verdict(StrEnum):
    SEPARATED = "separated"
    CANNOT_SEPARATE = "cannot_separate"
    UNEVALUABLE = "unevaluable"


@dataclass
class DirectionReport:
    """Long versus short on the same episodes, with the reason the answer is what it is."""

    lane: Lane
    horizon_s: int
    entry_delay_s: int
    long_arm: ArmResult
    short_arm: ArmResult
    census: EpisodeCensus
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = list(self.census.lines())
        out.append("")
        out.append(
            f"lane={self.lane.value} entry=t0+{self.entry_delay_s}s horizon={self.horizon_s}s"
        )
        for res in (self.long_arm, self.short_arm):
            mean = res.mean()
            ci = res.ci()
            tag = "EXECUTABLE" if res.config.arm.executable else "NOT EXECUTABLE (no venue)"
            out.append(f"  {res.config.arm.value:<13} n={res.n:<5} {tag}")
            if mean is None:
                out.append("                nothing filled; no mean to report")
                continue
            out.append(f"                mean net return {mean * 100:+.2f}%")
            if ci is not None:
                out.append(
                    f"                95% CI [{ci[0] * 100:+.2f}%, {ci[1] * 100:+.2f}%] "
                    f"(percentile bootstrap, episode as the unit)"
                )
            else:
                out.append("                95% CI unavailable (fewer than three fills)")
            cost = res.round_trip_cost()
            if cost is not None:
                out.append(f"                round-trip cost charged {cost * 100:.2f}% of notional")
            if res.dsr is not None:
                flag = "" if res.deflation_ok else "  <- NOT DEFLATED"
                out.append(
                    f"                DSR {res.dsr:.4f} at {res.honest_trials} honest trials{flag}"
                )
            if res.pbo is not None:
                out.append(f"                PBO {res.pbo:.3f}")
            for note in res.notes:
                out.append(f"                note: {note}")
            for reason, n in sorted(res.refusals.items(), key=lambda kv: -kv[1])[:6]:
                out.append(f"                refused {reason:<26} {n}")
        out.append("")
        out.append(f"  VERDICT: {self.verdict.value}")
        for r in self.reasons:
            out.append(f"    - {r}")
        return out


def direction_report(
    conn: sqlite3.Connection,
    *,
    lane: Lane = Lane.MIGRATION_FADE,
    entry_delay_s: int = 60,
    horizon_s: int = 300,
    size_base_units: int = 60_000_000,
    episodes: Sequence[Episode] | None = None,
    census: EpisodeCensus | None = None,
    limit: int | None = None,
    registry_conn: sqlite3.Connection | None = None,
) -> DirectionReport:
    """Replay the recorded episodes both ways and say whether the sample can tell them apart.

    The short arm is reported because the operator asked for both sides. It is marked not
    executable, and the verdict never reads "short wins": what the sample can support at
    most is a statement about the mid-to-mid drift and whether it clears the round-trip
    cost in either direction. If it does not, the honest answer is CANNOT_SEPARATE, and
    that is the answer the operator needs, because it says the wall is not going to be
    cleared by replaying harder.
    """
    if episodes is None or census is None:
        episodes, census = discover_episodes(
            conn, horizons_s=(horizon_s,), limit=limit
        )
    base = dict(
        lane=lane, entry_delay_s=entry_delay_s, horizon_s=horizon_s,
        size_base_units=size_base_units,
    )
    long_cfg = ReplayConfig(arm=Arm.LONG, **base)
    short_cfg = ReplayConfig(arm=Arm.SHORT_MIRROR, **base)
    long_res = run_config(conn, long_cfg, episodes, census, registry_conn=registry_conn)
    short_res = run_config(conn, short_cfg, episodes, census, registry_conn=registry_conn)
    deflate(conn, [long_res, short_res], lane=lane, trials_conn=registry_conn)

    verdict, reasons = _verdict(long_res, short_res)
    return DirectionReport(
        lane=lane, horizon_s=horizon_s, entry_delay_s=entry_delay_s,
        long_arm=long_res, short_arm=short_res, census=census,
        verdict=verdict, reasons=reasons,
    )


def _verdict(long_res: ArmResult, short_res: ArmResult) -> tuple[Verdict, list[str]]:
    reasons: list[str] = []
    reasons.append(
        "the short arm has no venue: a pump.fun bonding curve and PumpSwap both lack a "
        "borrow and a perp, so its number is an upper bound on a position nobody could "
        "have opened, not a fill."
    )
    if long_res.n < 3:
        reasons.append(f"only {long_res.n} episodes filled; below three there is no interval.")
        return Verdict.UNEVALUABLE, reasons

    gross = long_res.gross
    g_mean = _mean(gross)
    g_ci = bootstrap_ci(gross)
    hurdle = long_res.round_trip_cost()
    if g_mean is None or g_ci is None or hurdle is None:
        reasons.append("mid-to-mid drift or the cost hurdle could not be computed.")
        return Verdict.UNEVALUABLE, reasons

    reasons.append(
        f"mid-to-mid drift over the hold: mean {g_mean * 100:+.2f}%, 95% CI "
        f"[{g_ci[0] * 100:+.2f}%, {g_ci[1] * 100:+.2f}%], n={len(gross)}."
    )
    reasons.append(
        f"either arm must clear a round-trip cost of {hurdle * 100:.2f}% of notional "
        f"before its sign means anything."
    )
    if g_ci[0] > hurdle:
        reasons.append("the drift is above the cost hurdle: the LONG side is the one supported.")
        return Verdict.SEPARATED, reasons
    if g_ci[1] < -hurdle:
        reasons.append(
            "the drift is below the negative cost hurdle: the lane's fade thesis is "
            "supported by the price path - but see the venue line above; it is still not "
            "a position that could have been taken."
        )
        return Verdict.SEPARATED, reasons
    reasons.append(
        "the confidence interval on the drift spans the cost hurdle in both directions. "
        "This sample cannot separate the two sides, and adding replayed episodes from the "
        "same 27.9-hour window will not change that: they are one regime, and the "
        "effective sample size of a cross-section with a market-wide common factor is the "
        "number of independent time blocks, not the number of tokens."
    )
    return Verdict.CANNOT_SEPARATE, reasons


# -------------------------------------------------------------------------- persistence


def record_run(conn: sqlite3.Connection, result: ArmResult, *, verdict: Verdict) -> None:
    """Append one configuration's result.

    Plain ``INSERT``, not ``INSERT OR REPLACE``: a duplicate ``run_id`` is a bug worth
    an exception, and a re-run is a new run. Two runs disagreeing is a finding.
    """
    mean = result.mean()
    ci = result.ci()
    sharpe = gates._sharpe_raw([float(v) for v in result.returns])
    conn.execute(
        "INSERT INTO replay_runs (run_id, created_ms, lane, arm, executable, "
        "trial_id, params_json, config_json, cursor_basis, horizon_s, episodes_total, "
        "episodes_filled, mean_return_pct, median_return_pct, ci_lo_pct, ci_hi_pct, "
        "ci_method, sharpe, dsr, dsr_deflated, dsr_notes_json, pbo, honest_trials, "
        "verdict, census_json, notes_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            result.run_id, now_ms(), result.config.lane.value, result.config.arm.value,
            1 if result.config.arm.executable else 0, result.trial_id,
            jdump(result.effective_params), jdump(result.config.as_dict()),
            "PointInTime: SQL bound plus an unconditional per-row time-column check",
            result.config.horizon_s, result.considered, result.n,
            _pct(mean), _pct(_median(result.returns) if result.returns else None),
            _pct(ci[0]) if ci else None, _pct(ci[1]) if ci else None,
            "percentile_bootstrap:episode", str(sharpe) if sharpe is not None else None,
            str(result.dsr) if result.dsr is not None else None,
            1 if result.deflation_ok else 0, jdump(result.dsr_notes),
            str(result.pbo) if result.pbo is not None else None, result.honest_trials,
            verdict.value, jdump(result.census.as_dict()), jdump(result.notes),
        ),
    )
    for o in result.outcomes:
        conn.execute(
            "INSERT INTO replay_outcomes (run_id, chain, token, t0_ms, entry_ms, "
            "exit_ms, entry_price_usd, exit_price_usd, depth_lamports, depth_basis, "
            "gross_return_pct, net_return_pct, cost_in_frac, cost_out_frac, fill_basis, "
            "refused_reason, signal_strength, observed_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                result.run_id, result.config.chain.value, o.token, o.t0_ms, o.entry_ms,
                o.exit_ms, str(o.entry_price_usd) if o.entry_price_usd is not None else None,
                str(o.exit_price_usd) if o.exit_price_usd is not None else None,
                o.depth.lamports, o.depth.basis.value,
                _pct(o.gross_return), _pct(o.net_return),
                str(o.costs.c_in) if o.costs is not None else None,
                str(o.costs.c_out + o.costs.c_tip) if o.costs is not None else None,
                o.fill_basis, o.refused_reason,
                str(o.signal_strength) if o.signal_strength is not None else None,
                now_ms(),
            ),
        )


# -------------------------------------------------------------------------- arithmetic


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _mean(values: Sequence[Decimal] | None) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal(0)) / Decimal(len(values))


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _pct(value: Decimal | None) -> str | None:
    return str(value * 100) if value is not None else None


__all__ = [
    "Arm",
    "ArmResult",
    "Depth",
    "DirectionReport",
    "Episode",
    "EpisodeCensus",
    "EpisodeOutcome",
    "LookaheadError",
    "NO_OBSERVATION_TIME",
    "PROVENANCE",
    "PointInTime",
    "Provenance",
    "ReplayBroker",
    "ReplayConfig",
    "TIME_COLUMNS",
    "UnregisteredTrialError",
    "Verdict",
    "block_matrix",
    "bootstrap_ci",
    "deflate",
    "direction_report",
    "discover_episodes",
    "implied_pool",
    "lane_context",
    "CostTerms",
    "cost_model_cross_check",
    "cost_terms",
    "register_or_refuse",
    "open_readonly",
    "record_census",
    "record_run",
    "replay_episode",
    "run_config",
    "short_mirror_return",
    "sweep",
]
