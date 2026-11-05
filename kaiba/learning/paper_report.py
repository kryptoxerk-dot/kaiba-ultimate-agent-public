"""The owner's daily paper report, and the rule that says when a strategy is ready for live.

Owner, 2026-10-13: "Make sure the agent paper trades and report to me daily its progress and
let me know when it's confident to actually live trade."

One Telegram message a day, in this order:

1. **Is it paper trading?** Paper entries and exits per chain in the last 24 h. A chain that
   traded on paper in the previous 7 days and opened nothing in the last 24 h is flagged
   loudly: a silent zero is the failure this repository keeps finding (components that return
   nothing instead of an error).
2. **The scoreboard.** Every strategy (lane x chain) over a rolling :data:`WINDOW_DAYS` days,
   scored twice against the SAME pass line, the one declared on 2026-10-08 in
   :mod:`kaiba.learning.sprint` (n >= 40, >= 15 in each half, mean > 0 in both halves, 90%
   bootstrap lower bound > 0):

   * **paper** -- the closed paper positions themselves, net of the paper broker's modelled
     fees and impact. Rows whose outcome was booked rather than measured
     (:mod:`kaiba.learning.fabricated`) are left out and COUNTED, so the reader sees what
     was taken out.
   * **replay** -- the daily signal-audit replay (the live exit ladder over the swap tape),
     read from ``kv sprint:results``, written by the scorecard job.

   Two independent measurements, because each has failed alone: on 2026-10-13 the paper record
   said launch-snipe/sol -68.9% (exits refused, positions written off at -100%) while the
   replay said -7.1%.
3. **Readiness.** A strategy is READY FOR YOUR DECISION only when BOTH measurements pass on
   :data:`READY_DAYS` consecutive daily reports. The first time it gets there the message opens
   with it; if it later fails either, the message says it is no longer ready. Nothing here arms
   anything: going live stays the owner's decision, at minimum size.

4. **Strategy search** (PROFIT-2, :mod:`kaiba.learning.strategy_search`, added 2026-10-13):
   how many candidates the daily factory tried, how many survived its corrected holdout, and
   the FORWARD record (signals after selection only) of the tracked survivors. A search
   candidate is ready for the owner's decision under the same rule: a forward PASS on
   :data:`READY_DAYS` consecutive daily reports (the factory's forward PASS already includes
   the correction for how many candidates it tracks).

Not used, on purpose: conviction score, smart-wallet count and dossier grade. All three were
MEASURED anti-calibrated on live fills (AGENTS.md, 2026-09-24). "Confident" here means a pass
line declared in advance, held on two measurements for three days, and nothing else.
"""

from __future__ import annotations

import calendar
import json
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from kaiba.core.schemas import Chain
from kaiba.execution import paper_costs
from kaiba.learning import fabricated, sprint, time_exit_trial

DAY_MS = 86_400_000
#: Rolling window, both measurements. 12 days: the replay reads the swap tape from 30 min
#: before to 6 h after each signal, and swaps retention keeps 14 days (floor 13).
WINDOW_DAYS = 12
#: Consecutive daily reports on which BOTH measurements must pass. INVENTED: three days rules
#: out a one-day spike without making a real edge wait a week.
READY_DAYS = 3
#: OWNER 2026-10-20: keep paper trading, gather good wallets (runner hunt -> confluence-5), and
#: decide on this day: nothing ready for his decision by then -> automated trading stops.
DECISION_DAY = "2026-11-03"
KV_STATE = "paper_report:state"
KV_LAST = "paper_report:last"
#: Telegram refuses messages over 4,096 characters.
MAX_CHARS = 3_900
NATIVE_DECIMALS = {"sol": 9, "bsc": 18, "robinhood": 18, "eth": 18, "base": 18}
NATIVE_SYMBOL = {"sol": "SOL", "bsc": "BNB", "robinhood": "ETH", "eth": "ETH", "base": "ETH"}
#: Every chain the owner named (2026-10-21: Robinhood, Solana, BNB, Ethereum, Base). Listed even when
#: silent, so a chain that never trades shows as a zero rather than as a missing line.
REPORT_CHAINS: tuple[str, ...] = ("sol", "bsc", "robinhood", "eth", "base")


@dataclass
class Activity:
    chain: str
    opened_24h: int = 0
    closed_24h: int = 0
    opened_prior_7d: int = 0
    #: Over the scoreboard window (:data:`WINDOW_DAYS`), on the shared rule (:class:`PaperFilter`):
    #: closed paper positions, how many the scoreboard COUNTS (honest: sol by date, every other chain
    #: only with an honest pool/curve tag), and the mean return of the counted ones only. 2026-10-23
    #: (design step 8): per chain, so eth / base show apart from the chains that already trade.
    closed_window: int = 0
    counted_window: int = 0
    counted_mean: float | None = None

    @property
    def silent(self) -> bool:
        return self.opened_24h == 0 and self.opened_prior_7d > 0

    @property
    def honest_share(self) -> float | None:
        return self.counted_window / self.closed_window if self.closed_window else None


@dataclass
class PaperScore:
    score: sprint.Score
    excluded: int = 0


@dataclass
class Readiness:
    ready: list[str] = field(default_factory=list)
    newly_ready: list[str] = field(default_factory=list)
    no_longer_ready: list[str] = field(default_factory=list)
    streaks: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------------------
# reads (short, one query each)
# --------------------------------------------------------------------------------------


def activity(conn: sqlite3.Connection, now_ms: int, chains: Iterable[str] = REPORT_CHAINS) -> list[Activity]:
    out = {c: Activity(c) for c in chains}
    for chain, opened, closed, prior in conn.execute(
        "SELECT chain, "
        "SUM(opened_ms >= ?), SUM(closed_ms IS NOT NULL AND closed_ms >= ?), "
        "SUM(opened_ms < ? AND opened_ms >= ?) "
        "FROM positions WHERE mode = 'shadow' AND opened_ms >= ? GROUP BY chain",
        (now_ms - DAY_MS, now_ms - DAY_MS, now_ms - DAY_MS, now_ms - 8 * DAY_MS, now_ms - 8 * DAY_MS),
    ):
        a = out.setdefault(str(chain), Activity(str(chain)))
        a.opened_24h, a.closed_24h, a.opened_prior_7d = int(opened or 0), int(closed or 0), int(prior or 0)
    return list(out.values())


def with_honesty(conn: sqlite3.Connection, now_ms: int, acts: Sequence[Activity],
                 rows: Sequence[tuple[str, str, int, float | None, str | None, str]],
                 days: int = WINDOW_DAYS) -> list[Activity]:
    """``acts`` with each chain's window counts (:attr:`Activity.closed_window`, ``counted_window``,
    ``counted_mean``) filled in. ``rows`` are the scoreboard's own rows (:func:`scoreboard`, i.e.
    already through :class:`PaperFilter`), so "counted" here is exactly what the pass line counts;
    a booked write-off or an unpriceable row is not counted, as in :func:`paper_scores`."""
    import dataclasses

    closed: dict[str, int] = {}
    for chain, n in conn.execute(
        "SELECT chain, COUNT(*) FROM positions WHERE mode = 'shadow' AND closed_ms IS NOT NULL AND closed_ms >= ? "
        "GROUP BY chain", (now_ms - days * DAY_MS,),
    ):
        closed[str(chain)] = int(n or 0)
    counted: dict[str, list[float]] = {}
    for _lane, chain, _closed, ret, reason, mode in rows:
        if fabricated.is_fabricated_outcome(reason, mode) or ret is None:
            continue
        counted.setdefault(str(chain), []).append(max(-1.0, min(sprint.OUTCOME_CAP, ret)))
    out: list[Activity] = []
    known = {a.chain for a in acts}
    for a in [*acts, *(Activity(c) for c in sorted(set(closed) - known))]:
        vals = counted.get(a.chain, [])
        out.append(dataclasses.replace(a, closed_window=closed.get(a.chain, 0), counted_window=len(vals),
                                       counted_mean=(sum(vals) / len(vals)) if vals else None))
    return out


#: 2026-10-18: until this moment the paper broker converted USD<->native with
#: ``paper.DEFAULT_NATIVE_USD`` (SOL $200 against a real $103.93; see
#: ``viability.paper_native_usd``). A position whose exit changed price basis (curve <-> DEX)
#: booked a fiction (~-58% on flat launch-snipe trades); a CURVE-priced entry got a USD entry
#: price ~2x real, so protection stopped it out on a phantom -49% within seconds; and a
#: position open ACROSS the fix bought at one SOL price and sold at another. All three kinds
#: are left out of the scoreboard and counted. Same-basis DEX trades before the fix are
#: unaffected (the placeholder cancels in their return, and their USD prices are real) and stay.
PAPER_NATIVE_FIX_MS = 1792322000000

#: 2026-10-19: from here sol paper fills and protection's marks are priced from the token's own
#: PumpSwap pool or pump.fun bonding curve, read on-chain at the moment of the fill
#: (kaiba/execution/pumpswap_price.py), and every chain's paper fills pay the measured live costs
#: (kaiba/execution/paper_costs.py). Paper trades opened earlier are left out of the scoreboard:
#: their sol prices came from a feed that manufactured profits (box-evidence.md Q1) or from curve
#: snapshots up to 60 s old, and their costs were about half a live fill's. They stay in the
#: database for research. Moved on 10-19 as the curve-side fixes landed (01:39 -> 02:10 -> 04:00 UTC;
#: the last because paper curve exits sold without our own buy in the curve until 03:4x, see
#: pumpswap_price.with_holding).
PAPER_HONEST_MS = 1792382400000

#: 2026-10-21: from here a sol paper pool entry pays the market's drift NET of the pool fee
#: (paper_costs.ENTRY_LATENCY_BPS, 88 bps) instead of a figure that already contained the fee
#: (paper_costs.SUPERSEDED_ENTRY_LATENCY_BPS, 220). The broker charges the PumpSwap fee and our
#: impact itself, so earlier pool entries paid both twice: 132 bps, ~1.3 pp per entry, against them
#: (docs/research/claude-profit-ways-2026-10-21/VERIFICATION.md section 4 row 1). Unlike the trades
#: before :data:`PAPER_HONEST_MS` they STAY on the scoreboard -- their prices are real and the error
#: is one known constant, in the pessimistic direction -- and the report says how many there are.
#: The box's kaiba-engine restart that loaded the fix: 2026-10-21 03:19:43 UTC. MEASURED on the box's
#: ``paper_entry_priced`` events: the last sol entry at 320 bps was 03:07:43, the first at 188 bps
#: 03:22:35, none in between, so any instant in that gap classifies every trade the same.
PAPER_COST_FIX_MS = 1792552783000


def _tagged_closed(conn: sqlite3.Connection, now_ms: int, days: int) -> tuple[list[tuple[str, str]],
                                                                               dict[str, dict[str, Any]]]:
    """Closed paper positions in the window on every chain NOT honest by date
    (``paper_costs.HONEST_BY_DATE_CHAINS``, i.e. everything but sol), with their tags."""
    by_date = tuple(sorted(c.value for c in paper_costs.HONEST_BY_DATE_CHAINS))
    rows = conn.execute(
        f"SELECT position_id, chain FROM positions WHERE mode = 'shadow' AND closed_ms IS NOT NULL "
        f"AND closed_ms >= ? AND chain NOT IN ({','.join('?' * len(by_date))})",
        (now_ms - days * DAY_MS, *by_date),
    ).fetchall()
    rows = [(str(pid), str(chain)) for pid, chain in rows]
    bases = paper_costs.read_bases(conn, [pid for pid, _ in rows])
    # 2026-10-23 (hA_): a position with more FILLED orders than its tag accounts for does not count
    # (``paper_costs.UNTAGGED_LEGS``): before the merge fix every top-up rewrote the tag from scratch, so
    # a row whose first buy was dossier-priced read honest (MEASURED: one robinhood row counted at +62%).
    paper_costs.flag_untagged_legs(bases, paper_costs.filled_order_counts(conn, list(bases)))
    return rows, bases


def not_pool_priced(conn: sqlite3.Connection, now_ms: int, days: int = WINDOW_DAYS) -> tuple[set[str], dict[str, int]]:
    """Closed paper positions OFF SOL the pass line must NOT count, and how many per chain: the
    ENTRY-stage exclusions.

    2026-10-23 (step 3 follow-up): default-deny. Only sol is honest by date (since
    :data:`PAPER_HONEST_MS`); every other chain -- robinhood, bsc, and also eth/base (the manual
    lane), which nothing tags and nothing prices from a pool -- counts only with its own tag
    (``paper_costs.counts_on_scoreboard``). It used to filter robinhood and bsc only, so an eth or
    base row counted by its date alone.

    2026-10-21 (design step 3): :data:`PAPER_HONEST_MS` is a SOL cut. It certified robinhood and
    bsc rows as honest by date alone, while MEASURED 89 of 89 robinhood and 45 of 45 bsc entries
    after it were priced off the dossier (a median +5% above the tape) and most exits off
    DexScreener. A row on those chains counts only when its own ``paper_costs`` tag says its ENTRY
    was filled from the pool or the curve (``paper_costs.entry_counts``). A row with no tag --
    opened before the tag existed, or a tag that could not be written -- does not count. The trades
    keep running and stay in the database; they are only left off the scoreboard, and counted.

    2026-10-23 (step 3 review, blocking #5): the EXIT is no longer part of this test. Requiring
    every exit leg to be pool-filled dropped exactly the rows whose pool could not pay -- drained
    pools, the worst losers -- so the exclusion depended on the outcome. Those rows now stay, booked
    at the bound, and :func:`exit_not_pool_filled` reports them."""
    rows, bases = _tagged_closed(conn, now_ms, days)
    out: set[str] = set()
    counts: dict[str, int] = {}
    for pid, chain in rows:
        if not paper_costs.counts_on_scoreboard(chain, bases.get(pid)):
            out.add(pid)
            counts[chain] = counts.get(chain, 0) + 1
    return out, counts


def exit_not_pool_filled(conn: sqlite3.Connection, now_ms: int, days: int = WINDOW_DAYS) -> set[str]:
    """Closed paper positions off sol that ARE counted (pool- or curve-priced entry) but whose
    exit was not all filled from the pool or curve (``paper_costs.exit_degraded``): a leg booked at
    zero proceeds because the pool could not pay, at the deciding mark because the pool could not be
    read, or with an unknown tax. :func:`exit_stage` and :func:`hold_exit_stage` turn them into the
    report's count, booked mean and PASS hold."""
    rows, bases = _tagged_closed(conn, now_ms, days)
    return {pid for pid, _chain in rows if paper_costs.exit_degraded(bases.get(pid))}


#: A strategy whose counted rows include more than this share of exit-stage rows (an exit not
#: filled from the pool, :func:`exit_not_pool_filled`) does not get PASS: the verdict reads HOLD
#: until the share falls. INVENTED: one in ten is "a small share"; above it, the pass leans on
#: exits this module did not price at our size. Withheld whichever way those rows were booked --
#: the zero-proceeds bound is pessimistic, the mark is not, and the reader cannot tell a pass
#: carried by either apart from one carried by real pool fills.
EXIT_STAGE_MAX_SHARE = 0.10


def exit_stage(rows: Sequence[tuple[str, str, int, float | None, str | None, str]]
               ) -> dict[tuple[str, str], tuple[int, float | None]]:
    """``(lane, chain) -> (count, booked mean)`` of exit-stage rows, on :func:`paper_scores`' basis
    (booked write-offs and unpriceable rows are not counted there, so not here either)."""
    groups: dict[tuple[str, str], list[float]] = {}
    for lane, chain, _closed, ret, reason, mode in rows:
        if fabricated.is_fabricated_outcome(reason, mode) or ret is None:
            continue
        groups.setdefault((lane, chain), []).append(max(-1.0, min(sprint.OUTCOME_CAP, ret)))
    return {key: (len(v), sum(v) / len(v) if v else None) for key, v in groups.items()}


def hold_exit_stage(papers: Sequence[PaperScore], stage: Mapping[tuple[str, str], tuple[int, float | None]],
                    *, max_share: float = EXIT_STAGE_MAX_SHARE) -> list[PaperScore]:
    """``papers`` with PASS turned into HOLD where exit-stage rows exceed ``max_share`` of n."""
    import dataclasses

    out: list[PaperScore] = []
    for p in papers:
        s = p.score
        k, mean = stage.get((s.lane, s.chain), (0, None))
        if s.verdict == "PASS" and s.n and k / s.n > max_share:
            why = (f"exit stage: {k} of {s.n} exits not filled from the pool (booked mean {_pct(mean)}), "
                   f"over {int(max_share * 100)}%")
            p = dataclasses.replace(p, score=dataclasses.replace(s, verdict="HOLD", why=why))
        out.append(p)
    return out


def sol_fee_counted_twice(conn: sqlite3.Connection, now_ms: int, days: int = WINDOW_DAYS) -> int:
    """Closed sol paper trades the scoreboard still counts whose POOL-priced entry paid the pool
    fee twice: opened in [:data:`PAPER_HONEST_MS`, :data:`PAPER_COST_FIX_MS`), closed inside the
    window, not a fabricated write-off, and not a curve-priced buy (the curve path never paid the
    drift charge). No early return on the window start: the window is set by ``closed_ms``, so a
    trade opened before the fix and closed after it stays on the scoreboard and is named."""
    start = now_ms - days * DAY_MS
    row = conn.execute(
        "SELECT count(*) FROM positions p WHERE p.mode = 'shadow' AND p.chain = 'sol' "
        "AND p.closed_ms IS NOT NULL AND p.closed_ms >= ? AND p.opened_ms >= ? AND p.opened_ms < ? "
        "AND CAST(p.cost_native AS REAL) > 0 AND COALESCE(p.exit_reason, '') NOT LIKE 'abandoned_%' "
        "AND NOT EXISTS (SELECT 1 FROM position_orders po JOIN order_events oe ON oe.order_id = po.order_id "
        "WHERE po.position_id = p.position_id AND oe.detail LIKE '%\"basis\":\"curve\"%' "
        "AND oe.detail LIKE '%\"side\":\"buy\"%')",
        (start, PAPER_HONEST_MS, PAPER_COST_FIX_MS),
    ).fetchone()
    return int(row[0] or 0)


def native_bug_positions(conn: sqlite3.Connection, now_ms: int, days: int = WINDOW_DAYS) -> set[str]:
    """Paper positions the native-price placeholder distorted (see :data:`PAPER_NATIVE_FIX_MS`)."""
    start = now_ms - days * DAY_MS
    if start >= PAPER_NATIVE_FIX_MS:
        return set()
    from kaiba.ops.retention import id_boundary

    out = {str(pid) for (pid,) in conn.execute(
        "SELECT position_id FROM positions WHERE mode = 'shadow' AND opened_ms < ? AND closed_ms >= ?",
        (PAPER_NATIVE_FIX_MS, PAPER_NATIVE_FIX_MS),
    )}
    lo = id_boundary(conn, "order_events", "ts_ms", start)
    # Exits that switched basis, AND curve-priced entries: a curve buy's USD entry price was
    # the curve price x the $200 placeholder (~2x real), so protection, comparing real USD
    # quotes with it, saw an instant ~-49% and stopped the position out within seconds.
    orders = [str(o) for (o,) in conn.execute(
        "SELECT order_id FROM order_events WHERE id > ? AND ts_ms < ? AND (detail LIKE '%basis_changed%' "
        "OR (detail LIKE '%\"basis\":\"curve\"%' AND detail LIKE '%\"side\":\"buy\"%'))",
        (int(lo or 0), PAPER_NATIVE_FIX_MS),
    )]
    for i in range(0, len(orders), 500):
        chunk = orders[i:i + 500]
        out |= {str(pid) for (pid,) in conn.execute(
            f"SELECT position_id FROM position_orders WHERE order_id IN ({','.join('?' * len(chunk))})", chunk,
        )}
    return out


# --------------------------------------------------------------------------------------
# which paper rows count: ONE rule for the report, the console and the agent
# --------------------------------------------------------------------------------------

#: Why the scoreboard leaves a closed paper position out (:meth:`PaperFilter.reason`, and
#: :func:`outcome_exclusion` for the two outcome-level reasons).
BEFORE_HONEST = "before_honest_cutoff"
NOT_POOL_PRICED = "not_pool_priced"
NATIVE_PRICE_BUG = "native_price_bug"
FABRICATED_OUTCOME = "fabricated_outcome"
UNPRICEABLE = "unpriceable"


@dataclass(frozen=True)
class PaperFilter:
    """Which closed paper positions every paper scoreboard leaves out, and why.

    2026-10-23 (step 3 follow-up): ONE rule, built once by :func:`paper_filter` and applied by this
    report (:func:`scoreboard`), the operator console (``dashboard.trading``: its trade summary,
    scoreboard and trial table) and the agent's ``kaiba_performance`` tool (what the Hermes "Paper
    results" chat prompt reads), so they cannot disagree. Before it the console's trade summary said
    it applied "the same exclusions as the paper report" and applied none of the step 3 ones: every
    dossier-priced robinhood/bsc row, every pre-cutoff row, counted there.

    Default-deny off sol: a position on any other chain counts only when it is in
    :attr:`counted_off_sol` -- tagged, inside the window, with a pool- or curve-priced entry
    (``paper_costs.counts_on_scoreboard``). A position this filter never looked at does not count.
    """

    native_bug: frozenset[str] = frozenset()
    counted_off_sol: frozenset[str] = frozenset()
    #: The off-sol positions in the window that were looked at and do not count (for the counts).
    not_pool_priced: frozenset[str] = frozenset()
    not_pool_priced_counts: Mapping[str, int] = field(default_factory=dict)
    since_ms: int = PAPER_HONEST_MS

    def off_sol_denied(self, position_id: Any, chain: Any) -> bool:
        if str(chain) in {c.value for c in paper_costs.HONEST_BY_DATE_CHAINS}:
            return False
        return str(position_id) not in self.counted_off_sol

    def reason(self, position_id: Any, chain: Any, opened_ms: Any) -> str | None:
        """Why this position is off every scoreboard (position-level), or ``None``."""
        if opened_ms is None or int(opened_ms) < self.since_ms:
            return BEFORE_HONEST
        if self.off_sol_denied(position_id, chain):
            return NOT_POOL_PRICED
        if str(position_id) in self.native_bug:
            return NATIVE_PRICE_BUG
        return None


def outcome_exclusion(ret: float | None, exit_reason: str | None, mode: str = "shadow") -> str | None:
    """The outcome-level exclusions :func:`paper_scores` applies (and counts as ``excluded``)."""
    if fabricated.is_fabricated_outcome(exit_reason, mode):
        return FABRICATED_OUTCOME
    if ret is None:
        return UNPRICEABLE
    return None


def paper_filter(conn: sqlite3.Connection, now_ms: int, days: int = WINDOW_DAYS) -> PaperFilter:
    """The :class:`PaperFilter` for closed paper positions in the last ``days``."""
    rows, bases = _tagged_closed(conn, now_ms, days)
    counted: set[str] = set()
    left_out: set[str] = set()
    counts: dict[str, int] = {}
    for pid, chain in rows:
        if paper_costs.counts_on_scoreboard(chain, bases.get(pid)):
            counted.add(pid)
        else:
            left_out.add(pid)
            counts[chain] = counts.get(chain, 0) + 1
    return PaperFilter(native_bug=frozenset(native_bug_positions(conn, now_ms, days)),
                       counted_off_sol=frozenset(counted), not_pool_priced=frozenset(left_out),
                       not_pool_priced_counts=counts)


def paper_rows(conn: sqlite3.Connection, now_ms: int, days: int = WINDOW_DAYS, *,
               skip: set[str] | None = None,
               skipped: dict[tuple[str, str], int] | None = None,
               trials: Mapping[str, time_exit_trial.Trial] | None = None,
               since_ms: int | None = None,
               not_honest: set[str] | None = None,
               only: set[str] | None = None,
               flt: PaperFilter | None = None,
               ) -> list[tuple[str, str, int, float | None, str | None, str]]:
    """(lane, chain, closed_ms, return or None, exit_reason, mode) for closed paper positions.

    Positions in ``skip`` are left out and counted per (lane, chain) into ``skipped``. Positions in
    ``not_honest`` (:func:`not_pool_priced`) are left out silently here; the caller counts them. A
    position in a time-exit trial's TREATMENT arm is reported under the trial's label, so the lane's
    own row keeps measuring the incumbent exit (kaiba/learning/time_exit_trial.py). ``only``
    restricts the result to those positions, after every other rule (:func:`exit_stage`).

    ``flt`` (:class:`PaperFilter`) applies the shared rule instead -- the date cut, default-deny
    off sol, the native-price placeholder -- with the same counting into ``skipped``; it is what
    :func:`scoreboard` passes. ``skip``/``since_ms``/``not_honest`` are ANDed with it."""
    trials = time_exit_trial.load_trials() if trials is None else trials
    rows = []
    for pid, lane, chain, closed, cost, realized, reason, mode, opened in conn.execute(
        "SELECT position_id, lane, chain, closed_ms, cost_native, realized_native, exit_reason, mode, opened_ms "
        "FROM positions WHERE mode = 'shadow' AND closed_ms IS NOT NULL AND closed_ms >= ?",
        (now_ms - days * DAY_MS,),
    ):
        why = flt.reason(pid, chain, opened) if flt is not None else None
        if why == BEFORE_HONEST or (since_ms is not None and (opened is None or int(opened) < since_ms)):
            if skipped is not None:
                skipped[("__before_honest__", "")] = skipped.get(("__before_honest__", ""), 0) + 1
            continue
        if why == NOT_POOL_PRICED or (not_honest and str(pid) in not_honest):
            continue
        lane = time_exit_trial.label_for(trials, str(lane), str(pid), int(opened) if opened is not None else None)
        if why == NATIVE_PRICE_BUG or (skip and str(pid) in skip):
            if skipped is not None:
                skipped[(str(lane), str(chain))] = skipped.get((str(lane), str(chain)), 0) + 1
            continue
        if only is not None and str(pid) not in only:
            continue
        try:
            c, r = float(cost), float(realized)  # TEXT columns: never compare them as strings
        except (TypeError, ValueError):
            c, r = 0.0, 0.0
        ret = (r / c) if c > 0 else None
        rows.append((str(lane), str(chain), int(closed), ret, reason, str(mode)))
    return rows


def paper_scores(rows: Sequence[tuple[str, str, int, float | None, str | None, str]], now_ms: int,
                 days: int = WINDOW_DAYS) -> list[PaperScore]:
    start = now_ms - days * DAY_MS
    groups: dict[tuple[str, str], list[tuple[int, float]]] = {}
    excluded: dict[tuple[str, str], int] = {}
    for lane, chain, closed, ret, reason, mode in rows:
        key = (lane, chain)
        groups.setdefault(key, [])
        if fabricated.is_fabricated_outcome(reason, mode) or ret is None:
            excluded[key] = excluded.get(key, 0) + 1
            continue
        groups[key].append((closed, max(-1.0, min(sprint.OUTCOME_CAP, ret))))
    return [PaperScore(sprint.score(lane, chain, g, start, now_ms), excluded.get((lane, chain), 0))
            for (lane, chain), g in sorted(groups.items())]


def no_depth_fills(conn: sqlite3.Connection, now_ms: int, days: int = WINDOW_DAYS) -> int:
    """Paper exits filled at the pessimistic no-depth bound (PROFIT-1a) inside the window."""
    from kaiba.ops.retention import id_boundary

    lo = id_boundary(conn, "order_events", "ts_ms", now_ms - days * DAY_MS)
    row = conn.execute(
        "SELECT count(*) FROM order_events WHERE id > ? AND detail LIKE '%worst_allowed_no_depth%'",
        (int(lo or 0),),
    ).fetchone()
    return int(row[0] or 0)


def replay_results(conn: sqlite3.Connection) -> tuple[dict[tuple[str, str], dict[str, Any]], int | None]:
    row = conn.execute("SELECT value, updated_ms FROM kv WHERE key = ?", (sprint.KV_RESULTS,)).fetchone()
    if row is None:
        return {}, None
    try:
        items = json.loads(row[0])
    except (TypeError, ValueError):
        return {}, int(row[1] or 0)
    return {(str(i["lane"]), str(i["chain"])): i for i in items if isinstance(i, dict)}, int(row[1] or 0)


def live_leftovers(conn: sqlite3.Connection) -> dict[str, tuple[int, float]]:
    """Open LIVE positions (money still in tokens) per chain: (count, cost in native units)."""
    out: dict[str, tuple[int, float]] = {}
    for chain, cost in conn.execute("SELECT chain, cost_native FROM positions WHERE mode = 'live' AND closed_ms IS NULL"):
        n, total = out.get(str(chain), (0, 0.0))
        try:
            c = float(cost) / 10 ** NATIVE_DECIMALS.get(str(chain), 18)
        except (TypeError, ValueError):
            c = 0.0
        out[str(chain)] = (n + 1, total + c)
    return out


# --------------------------------------------------------------------------------------
# readiness
# --------------------------------------------------------------------------------------


def _key(lane: str, chain: str) -> str:
    return f"{lane}/{chain}"


#: Replay results older than this at report time are stale: they cannot count toward a streak.
REPLAY_MAX_AGE_MS = 36 * 3_600_000


def _previous_day(day: str) -> str:
    import datetime as _dt

    return (_dt.date.fromisoformat(day) - _dt.timedelta(days=1)).isoformat()


def update_readiness(state: Mapping[str, Any], papers: Sequence[PaperScore],
                     replay: Mapping[tuple[str, str], Mapping[str, Any]], day: str, *,
                     replay_ms: int | None, now_ms: int) -> tuple[dict[str, Any], Readiness]:
    """Advance each strategy's streak at most once per UTC ``day`` (``YYYY-MM-DD``).

    The streak counts CALENDAR-CONSECUTIVE report days on which paper AND replay both PASS on
    NEW evidence. Reaching :data:`READY_DAYS` makes a strategy ready.

    Tightened 2026-10-14 (Codex reproduction, docs/research/codex-learning-loop-audit-20261014.md:
    one unchanged PASS reused on Oct 1, 5 and 14 became READY). A day counts only when:

    * the replay results are fresh (written within :data:`REPLAY_MAX_AGE_MS`) and newer than
      the ones the previous counted day used;
    * the paper score changed since that day (n or mean): an identical score is the same
      evidence read again, not another day of it;
    * the previous counted day was YESTERDAY. A gap restarts the streak at 1.

    Anything else resets it to zero, with the reason kept in the state.
    """
    prev: dict[str, Any] = dict(state.get("strategies") or {})
    new_state: dict[str, Any] = {"strategies": {}}
    out = Readiness()
    fresh = replay_ms is not None and now_ms - int(replay_ms) <= REPLAY_MAX_AGE_MS
    for p in papers:
        s = p.score
        key = _key(s.lane, s.chain)
        r = replay.get((s.lane, s.chain)) or {}
        both = s.verdict == "PASS" and r.get("verdict") == "PASS"
        rec = dict(prev.get(key) or {"streak": 0, "day": None, "announced": False})
        paper_fp = [s.n, None if s.mean is None else round(float(s.mean), 9)]
        if rec.get("day") != day:
            newer_replay = fresh and int(replay_ms or 0) > int(rec.get("replay_ms") or 0)
            new_paper = rec.get("paper_fp") != paper_fp
            if not both:
                rec["streak"], rec["why"] = 0, "not both PASS"
            elif not fresh:
                rec["streak"], rec["why"] = 0, "replay results stale or missing"
            elif not (newer_replay and new_paper):
                rec["streak"], rec["why"] = 0, "no new evidence since the last counted day"
            elif rec.get("day") == _previous_day(day) and int(rec.get("streak") or 0) > 0:
                rec["streak"], rec["why"] = int(rec["streak"]) + 1, "consecutive"
            else:
                rec["streak"], rec["why"] = 1, "streak (re)started"
            rec["day"] = day
            rec["replay_ms"] = int(replay_ms) if replay_ms is not None else None
            rec["paper_fp"] = paper_fp
        elif not both:
            rec["streak"], rec["why"] = 0, "not both PASS"
        ready = int(rec["streak"]) >= READY_DAYS
        if ready:
            out.ready.append(key)
            if not rec.get("announced"):
                out.newly_ready.append(key)
                rec["announced"] = True
        elif rec.get("announced"):
            out.no_longer_ready.append(key)
            rec["announced"] = False
        out.streaks[key] = int(rec["streak"])
        new_state["strategies"][key] = rec
    return new_state, out


# --------------------------------------------------------------------------------------
# the message
# --------------------------------------------------------------------------------------


def _day_ms(day: str) -> int:
    """Midnight UTC of ``YYYY-MM-DD`` in epoch milliseconds."""
    return int(calendar.timegm(time.strptime(day, "%Y-%m-%d")) * 1000)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:+.1f}%"


#: Forward rows shown in the strategy-search section; labels are cut to this many characters,
#: so the section stays well inside Telegram's limit whatever the factory tracks.
SEARCH_ROWS = 5
SEARCH_LABEL_CHARS = 70
#: 2026-11-03 (SEARCH-SURVIVOR-GUARD-CLAUDE-20261103): written by scheduler.job_strategy_search. The
#: factory's forward PASSes rested on the tokens the replay could score (29 launch-snipe/sol
#: candidates at +24..32% while their rule averaged -18.8% on every paper position, n=829), so a
#: candidate is ready only when the guard record for the LATEST search run clears it.
KV_SEARCH_GUARD = "strategy_search:survivor_guard"


def search_ready(c: Mapping[str, Any], search: Mapping[str, Any] | None) -> bool:
    """READY_DAYS forward passes running AND the latest run's survivor guard cleared this candidate.
    No guard record (before the first guarded run, or a run whose guard failed) means not ready."""
    guard = (search or {}).get("guard") or {}
    g = (guard.get("candidates") or {}).get(str(c.get("candidate_id")))
    return int(c.get("fwd_streak") or 0) >= READY_DAYS and bool(g) and g.get("verdict") == "PASS"


def search_lines(search: Mapping[str, Any] | None) -> list[str]:
    """The short "Strategy search" section; nothing before the factory's first run."""
    if not search:
        return []
    day = time.strftime("%Y-%m-%d", time.gmtime(int(search["ts_ms"]) / 1000))
    method = str(search.get("method") or "bh").upper()
    stopped = f" (run stopped early: {search['stopped']})" if search.get("stopped") else ""
    lines = ["", f"Strategy search {day} ({method} q={search.get('fdr_q')} on the holdout; "
                 "only FORWARD results count):",
             f"  tried {search['tried']}, to holdout {search['selected']}, survivors "
             f"{search['survivors']}; tracking {search['tracked']} forward{stopped}"]
    ready = []
    for c in list(search.get("candidates") or [])[:SEARCH_ROWS]:
        streak = int(c.get("fwd_streak") or 0)
        since = time.strftime("%m-%d", time.gmtime(int(c["selected_ms"]) / 1000))
        label = str(c["label"])[:SEARCH_LABEL_CHARS]
        verdict = str(c.get("fwd_verdict") or "PENDING")
        lines.append(f"  {verdict:7s} {label}: fwd n={c.get('fwd_n') or 0} {_pct(c.get('fwd_mean'))} "
                     f"low90 {_pct(c.get('fwd_lower'))} since {since}"
                     + (f" | {streak}/{READY_DAYS}" if streak else ""))
        if search_ready(c, search):
            ready.append(label)
    guard = search.get("guard")
    if guard is None:
        lines.append("  Survivor check: not recorded for this search run, so no search candidate counts as ready.")
    else:
        turned = [g for g in (guard.get("candidates") or {}).values()
                  if g.get("verdict_before") == "PASS" and g.get("verdict") != "PASS"]
        if turned:
            lines.append(f"  Survivor check: {len(turned)} forward PASS turned {turned[0].get('verdict')}: "
                         f"{str(turned[0].get('why') or '')[:160]}")
    lines.append("  Search ready for your decision: " + ("; ".join(ready) if ready else
                 f"none (needs a forward pass {READY_DAYS} reports running that survives the survivor check)"))
    return lines


#: Where :mod:`kaiba.learning.twin_exits` writes its latest paired exit comparison.
TWIN_REPORT = "learning/twin_exits/latest.json"

#: 2026-10-18 (docs/research/claude-profit-research-20261018/box-evidence.md, Q1): paper fills and
#: protection's marks are ~95% DexScreener, which lags the pool (median ~45 s) and keeps serving
#: the pump.fun CURVE pair for ~30 s after migration. migration-fade entries were therefore often
#: booked far below the real pool price: entries priced within band of the chain average -17.8%
#: (n=94); the 17% booked >26% below it average +104.8%. Priced from the pool, a 60 s exit loses
#: -16.1%/trade (n=89). Until fills and marks are pool-priced, a paper or twin "pass" on a fast
#: lane is not evidence -- the report says so every time it shows one.
#: 2026-10-27: the line above described the record BEFORE :data:`PAPER_HONEST_MS`. The report now
#: counts sol rows only from that cut (pool/curve-priced on chain) and off-sol rows only with their
#: own fill tag, so the caveat says what the counted rows are and what a pass on them means. The
#: robinhood STOP DECISION still reads DexScreener marks (memory: onchain_vs_feed INSUFFICIENT);
#: the fills themselves are on the curve or pool.
PRICE_FEED_CAVEAT = ("CAUTION: only COUNTED rows are evidence. sol paper fills and marks are priced from the "
                     "token's own pool or curve on chain since 2026-10-19 04:00 UTC; robinhood/bsc/eth/base rows "
                     "count only when their own fill tag says the entry filled from the pool or curve at our size "
                     "(robinhood stop decisions still read DexScreener marks; the fills do not). Uncounted rows are "
                     "research only: priced from DexScreener quotes (~45 s lag, old curve price after migration) "
                     "these lanes manufactured profits. A paper or twin pass on counted rows means the lane lost "
                     "less or broke even on paper; it is evidence of live profit only after the pass line holds "
                     "3 days and replay agrees.")


def _diff_lower(treat: Sequence[float], control: Sequence[float], *, q: float = 0.05,
                resamples: int = 2000, seed: int = 11) -> float | None:
    """Bootstrap lower bound of mean(treat) - mean(control), arms resampled independently."""
    if len(treat) < 2 or len(control) < 2:
        return None
    import random
    import statistics

    rng = random.Random(seed)
    diffs = sorted(statistics.fmean(rng.choices(treat, k=len(treat)))
                   - statistics.fmean(rng.choices(control, k=len(control))) for _ in range(resamples))
    return diffs[int(q * resamples)]


def trial_stats(conn: sqlite3.Connection, now_ms: int,
                trials: Mapping[str, time_exit_trial.Trial] | None = None,
                skip: set[str] | None = None, flt: PaperFilter | None = None) -> list[dict[str, Any]]:
    """Per trial and chain: the two arms' closed paper positions, side by side.

    Verdict PASS needs >= sprint.MIN_N closes in EACH arm, the treated arm's own 90% lower bound
    above zero (it must make money, not just lose less) and the difference's lower bound above
    zero (it must beat the incumbent it runs alongside). With ``flt`` an off-sol position counts
    only when the shared rule counts it (:meth:`PaperFilter.off_sol_denied`); the trial's own
    window replaces the date cut."""
    import statistics

    trials = time_exit_trial.load_trials() if trials is None else trials
    out: list[dict[str, Any]] = []
    for lane, trial in sorted(trials.items()):
        if trial.since_ms <= 0:
            continue
        arms: dict[str, dict[str, list[float]]] = {}
        for pid, chain, cost, realized, reason, mode in conn.execute(
            "SELECT position_id, chain, cost_native, realized_native, exit_reason, mode FROM positions "
            "WHERE lane = ? AND mode = 'shadow' AND closed_ms IS NOT NULL AND opened_ms >= ? AND opened_ms < ?",
            (lane, trial.since_ms, trial.until_ms or now_ms + 1),
        ):
            if fabricated.is_fabricated_outcome(reason, mode) or (skip and str(pid) in skip):
                continue
            if flt is not None and flt.off_sol_denied(pid, chain):
                continue
            try:
                c, r = float(cost), float(realized)  # TEXT columns: never compare them as strings
            except (TypeError, ValueError):
                continue
            if c <= 0:
                continue
            which = time_exit_trial.arm(str(pid)) if trial.split == "half" else time_exit_trial.TREAT
            arms.setdefault(str(chain), {time_exit_trial.TREAT: [], time_exit_trial.CONTROL: []})[which].append(
                max(-1.0, min(sprint.OUTCOME_CAP, r / c)))
        for chain, by_arm in sorted(arms.items()):
            t, c = by_arm[time_exit_trial.TREAT], by_arm[time_exit_trial.CONTROL]
            t_low, d_low = sprint.lower_bound(t), _diff_lower(t, c)
            if len(t) < sprint.MIN_N or len(c) < sprint.MIN_N:
                verdict = "PENDING"
            elif t_low is not None and t_low > 0 and d_low is not None and d_low > 0:
                verdict = "PASS"
            else:
                verdict = "FAIL"
            out.append({
                "lane": lane, "chain": chain, "hold_s": trial.hold_s, "since_ms": trial.since_ms,
                "until_ms": trial.until_ms, "n_treat": len(t), "n_control": len(c),
                "mean_treat": statistics.fmean(t) if t else None,
                "mean_control": statistics.fmean(c) if c else None,
                "treat_low90": t_low, "diff_low90": d_low,
                "diff": (statistics.fmean(t) - statistics.fmean(c)) if t and c else None,
                "verdict": verdict,
            })
    return out


def trial_lines(stats: Sequence[Mapping[str, Any]]) -> list[str]:
    if not stats:
        return []
    out = ["", "Exit trial (randomized on paper: half of new positions exit at the fixed time, half keep "
           f"today's exit; pass = n>={sprint.MIN_N} per arm, timed arm low90 > 0 AND beats today's exit):"]
    for r in stats:
        since = time.strftime("%m-%d %H:%M", time.gmtime(int(r["since_ms"]) / 1000))
        out.append(f"  {r['verdict']:7} {r['lane']}/{r['chain']} exit {r['hold_s']}s since {since}: "
                   f"n={r['n_treat']} {_pct(r['mean_treat'])} (low90 {_pct(r['treat_low90'])}) vs today's exit "
                   f"n={r['n_control']} {_pct(r['mean_control'])} | diff {_pct(r['diff'])} (low90 {_pct(r['diff_low90'])})")
    return out


def twin_report(data_dir: str | None = None) -> dict[str, Any] | None:
    """The latest twin-exit report, or ``None`` when it has not run (never an error)."""
    import os
    from pathlib import Path

    base = Path(data_dir or os.environ.get("KAIBA_DATA_DIR") or Path.home() / "kaiba" / "data")
    try:
        return json.loads((base / TWIN_REPORT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def twin_lines(twin: Mapping[str, Any] | None, now_ms: int) -> list[str]:
    """Exit challengers paired against the incumbent on the same paper positions (2026-10-16).

    Only rows with pairs; PASS needs the pass line AND a positive paired lower bound."""
    if not twin:
        return []
    rows = [r for r in twin.get("rows") or [] if r.get("pairs")]
    age_h = (now_ms - int(twin.get("at_ms") or now_ms)) / 3_600_000
    head = (f"Exit twins ({twin.get('window_days')} d, paired on the same paper positions"
            + (f", {age_h:.0f} h old" if age_h >= 6 else "") + "):")
    if not rows:
        return ["", head + " no paired positions yet (the quote tape started 2026-10-16)"]
    pct = lambda x: "n/a" if x is None else f"{100 * x:+.1f}%"  # noqa: E731
    out = ["", head, "  " + PRICE_FEED_CAVEAT]
    for r in sorted(rows, key=lambda r: (r["verdict"] != "PASS", -(r.get("paired_diff_mean") or -9)))[:8]:
        out.append(f"  {r['verdict']:7} {r['lane']}/{r['chain']} {r['challenger']}: n={r['pairs']} "
                   f"{pct(r.get('challenger_mean'))} vs {pct(r.get('incumbent_model_mean'))} now "
                   f"(paired {pct(r.get('paired_diff_mean'))}, low90 {pct(r.get('paired_diff_lower90'))})"
                   + (f" | +{r.get('stress_bps')}bps/leg: {'holds' if r.get('survives_stress') else 'FAILS'} "
                      f"{pct(r.get('challenger_stress_mean'))}" if r.get("stress_bps") else ""))
    return out


def render(*, now_ms: int, acts: Sequence[Activity], papers: Sequence[PaperScore],
           replay: Mapping[tuple[str, str], Mapping[str, Any]], replay_ms: int | None,
           ready: Readiness, no_depth: int, leftovers: Mapping[str, tuple[int, float]],
           search: Mapping[str, Any] | None = None, twin: Mapping[str, Any] | None = None,
           skipped: Mapping[tuple[str, str], int] | None = None,
           trial: Sequence[Mapping[str, Any]] | None = None, sol_fee_twice: int = 0,
           not_honest: Mapping[str, int] | None = None,
           exit_stage_rows: Mapping[tuple[str, str], tuple[int, float | None]] | None = None) -> str:
    day = time.strftime("%Y-%m-%d", time.gmtime(now_ms / 1000))
    lines: list[str] = []
    newly = [str(c["label"])[:SEARCH_LABEL_CHARS] for c in (search or {}).get("candidates") or []
             if int(c.get("fwd_streak") or 0) == READY_DAYS and search_ready(c, search)]
    if newly:
        lines += [f"SEARCH CANDIDATE READY FOR YOUR DECISION: {'; '.join(newly)}",
                  f"Passed FORWARD (only signals after it was found) for {READY_DAYS} reports in a "
                  "row. Nothing is armed; going live (minimum size) is your call.", ""]
    if ready.newly_ready:
        lines += [f"READY FOR YOUR DECISION: {', '.join(ready.newly_ready)}",
                  f"Passed on paper AND replay for {READY_DAYS} days in a row. Nothing is armed;"
                  " going live (minimum size) is your call.", ""]
    if ready.no_longer_ready:
        lines += [f"NO LONGER READY: {', '.join(ready.no_longer_ready)} (failed paper or replay today)", ""]
    lines.append(f"Kaiba daily paper report {day} - live buying OFF")
    left = (_day_ms(DECISION_DAY) - now_ms) // 86_400_000 + 1
    if left > 0:
        lines.append(f"Decision day {DECISION_DAY}: {left} day(s) left. Wallet test: confluence-5 follows the "
                     "frozen runner-hunt wallets (only buys after each freeze count).")
    else:
        lines.append(f"DECISION DAY {DECISION_DAY} reached: if nothing below is ready, automated trading stops.")
    lines.append("")
    lines.append("Paper trading, last 24 h:")
    if any(a.closed_window or a.counted_window for a in acts):
        lines.append(f"  ({WINDOW_DAYS}d: closed paper trades, how many the scoreboard counts as honest, "
                     "and the mean of the counted ones only)")
    for a in acts:
        flag = "  <-- NOTHING TODAY, check it" if a.silent else ""
        window = ""
        if a.closed_window or a.counted_window:
            share = a.honest_share
            window = (f" | {WINDOW_DAYS}d: {a.closed_window} closed, {a.counted_window} counted"
                      f" ({f'{100 * share:.0f}%' if share is not None else 'n/a'} honest)"
                      f", mean {_pct(a.counted_mean)}")
        lines.append(f"  {a.chain}: {a.opened_24h} opened, {a.closed_24h} closed{window}{flag}")
    lines.append("")
    lines.append(PRICE_FEED_CAVEAT)
    lines.append(f"Scoreboard, last {WINDOW_DAYS} days (pass = n>={sprint.MIN_N}, both halves > 0, "
                 "90% lower bound > 0):")
    order = sorted(papers, key=lambda p: (p.score.verdict != "PASS", -(p.score.n)))
    for p in order:
        s = p.score
        r = replay.get((s.lane, s.chain))
        rtxt = f"replay {r['verdict']} {_pct(r.get('mean'))} n={r.get('n')}" if r else "replay n/a"
        streak = ready.streaks.get(_key(s.lane, s.chain), 0)
        ex = f", {p.excluded} write-offs left out" if p.excluded else ""
        k, kmean = (exit_stage_rows or {}).get((s.lane, s.chain), (0, None))
        ex += f", {k} exits not pool-filled (mean {_pct(kmean)})" if k else ""
        hold = f" | HOLD: {s.why}" if s.verdict == "HOLD" else ""
        lines.append(f"  {s.verdict:7s} {s.lane}/{s.chain}: paper n={s.n} {_pct(s.mean)} "
                     f"(old {_pct(s.mean_old)}, new {_pct(s.mean_new)}, low90 {_pct(s.lower)}{ex}) | {rtxt}"
                     + (f" | {streak}/{READY_DAYS} days" if streak else "") + hold)
    shown = {(p.score.lane, p.score.chain) for p in papers}
    for (lane, chain), r in sorted(replay.items()):
        if (lane, chain) not in shown:
            lines.append(f"  replay only {lane}/{chain}: {r['verdict']} {_pct(r.get('mean'))} n={r.get('n')}"
                         " (no paper lane of its own yet)")
    lines.append("")
    if ready.ready:
        lines.append(f"Ready for your decision: {', '.join(ready.ready)}")
    else:
        lines.append(f"Ready for live: none yet (needs paper AND replay passing {READY_DAYS} days running)")
    lines += search_lines(search)
    lines += twin_lines(twin, now_ms)
    lines += trial_lines(trial or [])
    notes = []
    skipped = dict(skipped or {})
    before = skipped.pop(("__before_honest__", ""), 0)
    if before:
        since = time.strftime("%m-%d %H:%M", time.gmtime(PAPER_HONEST_MS / 1000))
        notes.append(f"scoreboard counts paper trades opened since {since} UTC (sol priced from the pool, live "
                     f"costs charged); {before} earlier trades left out")
    if sol_fee_twice:
        extra = paper_costs.overcharge_before_fix_bps(Chain.SOL)
        since = time.strftime("%m-%d %H:%M", time.gmtime(PAPER_COST_FIX_MS / 1000))
        notes.append(f"{sol_fee_twice} sol paper trades opened before {since} UTC paid {extra} bps (~{extra / 100:.1f} pp)"
                     " more per pool-priced entry than later ones (the PumpSwap fee was counted twice); "
                     "they stay on the scoreboard")
    if skipped:
        worst = ", ".join(f"{ln}/{ch} {n}" for (ln, ch), n in sorted(skipped.items(), key=lambda kv: -kv[1])[:3])
        notes.append(f"{sum(skipped.values())} paper trades distorted by the native-price bug (fixed 2026-10-18) "
                     f"left out: {worst}")
    if not_honest:
        per = ", ".join(f"{chain} {n}" for chain, n in sorted(not_honest.items()))
        notes.append(f"entry stage: {sum(not_honest.values())} paper trades off sol left out of the "
                     f"scoreboard because nothing shows their entry was priced from the pool or curve with "
                     f"its costs known (dossier price, no tag, an unmeasured token tax, an unmeasured "
                     f"landing drift, or a placeholder native price): {per}")
    if exit_stage_rows:
        per = ", ".join(f"{ln}/{ch} {k} (mean {_pct(m)})" for (ln, ch), (k, m) in sorted(exit_stage_rows.items()))
        notes.append(f"exit stage: {sum(k for k, _ in exit_stage_rows.values())} paper trades off sol with a "
                     f"pool/curve entry are ON the scoreboard although an exit was not filled from the pool "
                     f"(zero proceeds where the pool could not pay, else the deciding mark): {per}; PASS is "
                     f"withheld (HOLD) where they are over {int(EXIT_STAGE_MAX_SHARE * 100)}% of a strategy's rows")
    if no_depth:
        notes.append(f"{no_depth} paper exits filled at the pessimistic 15% no-depth bound")
    if replay_ms is None:
        notes.append("replay results not written yet")
    elif now_ms - replay_ms > 36 * 3_600_000:
        notes.append(f"replay results are {(now_ms - replay_ms) // 3_600_000} h old")
    for chain, (n, cost) in sorted(leftovers.items()):
        notes.append(f"{n} live position(s) on {chain} still open from before the pause "
                     f"(cost {cost:.3f} {NATIVE_SYMBOL.get(chain, chain)})")
    if notes:
        lines.append("")
        lines += [f"Note: {n}" for n in notes]
    text = "\n".join(lines)
    if len(text) > MAX_CHARS:
        text = text[: MAX_CHARS - 20].rsplit("\n", 1)[0] + "\n... (truncated)"
    return text


# --------------------------------------------------------------------------------------
# one run
# --------------------------------------------------------------------------------------


@dataclass
class Scoreboard:
    """The paper scoreboard as every reader shows it (:func:`scoreboard`)."""

    papers: list[PaperScore]  # per strategy, PASS already held where exit-stage rows lean on it
    rows: list[tuple[str, str, int, float | None, str | None, str]]  # what was scored
    skipped: dict[tuple[str, str], int]  # before the cut, and the native-price placeholder
    filter: PaperFilter
    exit_stage: dict[tuple[str, str], tuple[int, float | None]]


def scoreboard(conn: sqlite3.Connection, now_ms: int, *,
               trials: Mapping[str, time_exit_trial.Trial] | None = None) -> Scoreboard:
    """THE paper scoreboard: the daily report, the console's scoreboard and the agent's tool all
    call this, so a strategy's n, mean and verdict are one computation (:class:`PaperFilter`)."""
    trials = time_exit_trial.load_trials() if trials is None else trials
    flt = paper_filter(conn, now_ms)
    skipped: dict[tuple[str, str], int] = {}
    rows = paper_rows(conn, now_ms, skipped=skipped, trials=trials, flt=flt)
    papers = paper_scores(rows, now_ms)
    # Exit-stage rows are ON the scoreboard (counted in `papers`); this is their count and booked
    # mean per strategy, on the same rows and rules, and the PASS they may not carry.
    stage = exit_stage(paper_rows(conn, now_ms, trials=trials, flt=flt, only=exit_not_pool_filled(conn, now_ms)))
    return Scoreboard(hold_exit_stage(papers, stage), rows, skipped, flt, stage)


def trial_board(conn: sqlite3.Connection, now_ms: int,
                trials: Mapping[str, time_exit_trial.Trial] | None = None,
                flt: PaperFilter | None = None) -> list[dict[str, Any]]:
    """The time-exit trial table under the shared rule (default-deny off sol), for every reader."""
    return trial_stats(conn, now_ms, trials, flt=flt if flt is not None else paper_filter(conn, now_ms))


def run(conn: sqlite3.Connection, now_ms: int) -> dict[str, Any]:
    trials = time_exit_trial.load_trials()
    board = scoreboard(conn, now_ms, trials=trials)
    acts = with_honesty(conn, now_ms, activity(conn, now_ms), board.rows)
    papers, skipped, stage = board.papers, board.skipped, board.exit_stage
    replay, replay_ms = replay_results(conn)
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (KV_STATE,)).fetchone()
    try:
        state = json.loads(row[0]) if row else {}
    except (TypeError, ValueError):
        state = {}
    day = time.strftime("%Y-%m-%d", time.gmtime(now_ms / 1000))
    new_state, ready = update_readiness(state, papers, replay, day, replay_ms=replay_ms, now_ms=now_ms)
    from kaiba.learning import strategy_search

    search = strategy_search.report_view(conn, limit=SEARCH_ROWS)
    if search is not None:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (KV_SEARCH_GUARD,)).fetchone()
        try:
            guard = json.loads(row[0]) if row else None
        except (TypeError, ValueError):
            guard = None
        search["guard"] = guard if isinstance(guard, dict) and guard.get("run_id") == search.get("run_id") else None
    text = render(now_ms=now_ms, acts=acts, papers=papers, replay=replay, replay_ms=replay_ms,
                  ready=ready, no_depth=no_depth_fills(conn, now_ms), leftovers=live_leftovers(conn),
                  search=search, twin=twin_report(), skipped=skipped,
                  trial=trial_board(conn, now_ms, trials, flt=board.filter),
                  sol_fee_twice=sol_fee_counted_twice(conn, now_ms),
                  not_honest=dict(board.filter.not_pool_priced_counts), exit_stage_rows=stage)
    for key, value in ((KV_STATE, json.dumps(new_state)), (KV_LAST, text)):
        conn.execute("INSERT OR REPLACE INTO kv (key, value, updated_ms) VALUES (?,?,?)", (key, value, now_ms))
    return {"text": text, "ready": ready.ready, "newly_ready": ready.newly_ready,
            "no_longer_ready": ready.no_longer_ready, "silent_chains": [a.chain for a in acts if a.silent],
            "strategies": len(papers), "readiness": asdict(ready),
            "search_ready": [str(c["label"]) for c in (search or {}).get("candidates") or []
                             if search_ready(c, search)]}


__all__ = ["Activity", "PaperFilter", "PaperScore", "Readiness", "Scoreboard", "activity", "with_honesty",
           "REPORT_CHAINS", "exit_not_pool_filled",
           "exit_stage", "hold_exit_stage", "not_pool_priced", "outcome_exclusion", "paper_filter", "paper_rows",
           "paper_scores", "scoreboard", "trial_board",
           "replay_results", "update_readiness", "render", "search_lines", "run", "WINDOW_DAYS",
           "READY_DAYS"]
