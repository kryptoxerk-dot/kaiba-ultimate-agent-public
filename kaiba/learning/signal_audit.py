"""The daily self-audit: replay EVERY lane signal, then ask which signal properties predict profit.

Why this module exists (2026-10-05)
-----------------------------------

The owner asked the right question after a losing week: "maybe our confluences aren't strong
enough? maybe we are chasing the wrong wallets? maybe we are not tracking KOL wallets enough?"
Each of those is answerable, and none of them is answerable from our FILLS. A rule judged on
the trades we took has already been filtered by the rule we are judging. Twice on 2026-09-24
a filter measured that way was shipped and pulled within hours (AGENTS.md).

This module answers on the SCANNED population instead:

1. **Every signal** a lane emitted over the last ``days`` is replayed. Only the first signal
   per (lane, chain, token, UTC day) is used, because the live book holds one position per
   token. Most of these signals were never traded.
2. **The replay is the live trade, not an idealised one.**
   * Entry is the first print :data:`LATENCY_MS` after the signal (decision to fill latency),
     plus one leg of cost.
   * Exit is the configured ladder: stop, TP rungs, breakeven after TP1, trailing tiers. It
     is read from ``protection`` in risk.yaml, so the audit follows the config.
   * Every exit fills at the first print :data:`POLL_MS` after the trigger, which is how a
     12 s protection tick fills.
   * Cost is :data:`COST_PER_LEG` per side.

   MEASURED on the first run, sol sm-trenches over 12 days: replay -13.1% per trade (n=400)
   against the real live -14.6% (n=73). The replay is calibrated, which is the only reason
   its cells are worth reading.
3. **Each property is scored in bands, separately on the older and the newer half** of the
   window. A band is called an ``edge`` only if it is positive in BOTH halves with at least
   :data:`MIN_HALF` signals each. A band that is merely better than baseline in both halves
   is a ``lift``; one consistently worse is a ``drag``. Everything else is ``noise``, and
   most cells are noise. That is the honest default for a 12-day memecoin tape.

What the first run found (2026-10-05, 982 signals; kept here because it is the baseline every
later run is read against):

* **Count of smart wallets is anti-calibrated** on both chains. Sol: 3 wallets -7.7%,
  4-5 -17.6%, 10+ -19.4%.
* **KOL buyers before the signal do not help.** Sol: 0 KOLs -10.7%, 1 KOL -20.4%.
* **Wallet records do not persist on sol.** Wallets that averaged above +5% on the older half
  averaged -29.2% on the newer half, against -17.2% for all newer signals.
* **Buying dips does not fix it.** Every pullback entry of 10-40% (15 or 60 minute wait)
  replayed between -11% and -16% on sol.
* **Robinhood is near break-even** (-1.7%). Signals with 2+ A/B-graded wallets did +13.3%,
  positive in both halves (n=50). BUT every one of those wallets was graded AFTER the signal,
  because ``wallet_scores`` is overwritten in place. That is lookahead until proven otherwise.

That last point is why :func:`snapshot_grades` exists. ``wallet_scores`` keeps only the
current grade, so "was this wallet A-graded when it bought?" cannot be asked of the past at
all. The daily snapshot makes it askable from the snapshot day forward. ``gradeAB_pit`` (point
in time) reads only a snapshot taken BEFORE the signal's day. ``gradeAB_now`` uses today's
grade and every cell it produces is labelled ``lookahead:``.

What it does NOT do: it never changes a gate, a size or a lane. It writes a scorecard
(``signal_audit_runs`` / ``signal_audit_cells``) that the operator agent reads every day
(``kaiba_signal_audit`` MCP tool) and that a human or the gates promote from. A gate that
retunes itself on its own output is a feedback loop nobody is reading.

Database discipline (AGENTS.md DATABASE READ RULE): every read opens its own read-only
connection and closes it, so no transaction is held across the loop. The WAL is checked every
:data:`WAL_CHECK_EVERY` queries. The run stops cleanly at its deadline with a partial,
labelled result rather than pinning the database.
"""

from __future__ import annotations

import collections
import datetime
import json
import logging
import os
import sqlite3
import statistics
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

AUDIT_VERSION = "signal_audit/1"
_EPOCH = datetime.datetime(1970, 1, 1)

#: One leg of round-trip cost. The live model's ~6.5% round trip, split evenly.
COST_PER_LEG = 0.0325
#: Protection's tick (``protection.poll_interval_s`` = 12 on the box): an exit trigger fills
#: at the first print at least this long after it.
POLL_MS = 12_000
#: Decision-to-fill latency for the entry.
LATENCY_MS = 30_000
#: Exits not hit within this horizon are marked at the last print.
HORIZON_MS = 6 * 3600_000
#: ``protection.stale_no_volume_exit_s``.
STALE_MS = 3600_000
#: Look-back read before the signal, for pre-signal features.
PRE_WINDOW_MS = 1_800_000
#: One outcome is capped at +300% so a single mooner cannot carry a cell's mean. The median
#: is reported beside it for the same reason.
OUTCOME_CAP = 3.0
#: A band with fewer signals than this prints, but gets no verdict.
MIN_CELL = 15
#: Each half needs this many before a band can be called an edge, lift or drag.
MIN_HALF = 15
#: A lift or drag must beat (or trail) that half's baseline by this much, in both halves.
LIFT_PTS = 0.05
#: X observations are made by the x_narrative recorder within minutes AFTER a signal, so an
#: X feature is read from the latest observation at or before t0 + this delay, and any cell it
#: produces is labelled ``delayed:``: acting on it means waiting for the X read before entering.
X_DELAY_MS = 5 * 60_000
#: Prints needed to replay a signal at all.
MIN_PRINTS = 10
WAL_LIMIT_BYTES = int(1.5 * 1024**3)
WAL_CHECK_EVERY = 25
QUERY_PAUSE_S = 0.03

DEFAULT_LANES: tuple[str, ...] = ("sm-trenches", "launch-snipe")
DEFAULT_CHAINS: tuple[str, ...] = ("sol", "robinhood", "bsc")


# --------------------------------------------------------------------------------------
# exit ladder
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExitLadder:
    """The live exit policy as numbers. Defaults are the box config of 2026-10-05."""

    stop_frac: float = 0.30
    tps: tuple[tuple[float, float], ...] = ((2.0, 0.50), (5.0, 0.25), (10.0, 0.15))
    #: (peak multiple, trail fraction), highest multiple first.
    trailing: tuple[tuple[float, float], ...] = (
        (100.0, 0.10), (25.0, 0.15), (10.0, 0.20), (5.0, 0.25), (2.0, 0.30), (1.2, 0.20),
    )
    breakeven_after_tp1: bool = True

    @classmethod
    def from_protection(cls, prot: Mapping[str, Any] | None) -> ExitLadder:
        """Build from ``risk.yaml``'s ``protection`` mapping; a missing key keeps the default."""
        if not prot:
            return cls()
        base = cls()
        try:
            stop = float(prot.get("stop_loss_bps", base.stop_frac * 10_000)) / 10_000
            tps = tuple(
                (float(m), float(p) / 100.0) for m, p in (prot.get("tp_ladder") or [])
            ) or base.tps
            trail = tuple(sorted(
                ((float(m), float(b) / 10_000) for m, b in (prot.get("trailing") or [])),
                reverse=True,
            )) or base.trailing
            be = bool(prot.get("breakeven_after_tp1", base.breakeven_after_tp1))
        except (TypeError, ValueError) as exc:
            log.warning("protection ladder unreadable (%s); auditing with the defaults", exc)
            return base
        return cls(stop_frac=stop, tps=tps, trailing=trail, breakeven_after_tp1=be)


def _first_at(series: Sequence[tuple[int, float]], i: int, t: int) -> int:
    while i < len(series) and series[i][0] < t:
        i += 1
    return i


def simulate_exit(
    series: Sequence[tuple[int, float]], i0: int, entry_cost: float, ladder: ExitLadder,
) -> float:
    """Return on ``entry_cost`` (price paid per unit, cost included) of holding from ``i0``.

    ``series`` is ``(ts_ms, price)`` sorted by time, from ONE price source. Trigger checks run
    on every print. A triggered sale fills at the first print :data:`POLL_MS` later, which
    charges the gap a real tick pays.
    """
    pos, got, peak = 1.0, 0.0, entry_cost
    done: set[float] = set()
    breakeven = False
    t_entry = series[i0][0]
    last_t = t_entry
    i = i0 + 1
    while i < len(series):
        t, p = series[i]
        if t - t_entry > HORIZON_MS:
            break
        if t - last_t > STALE_MS:
            return (got + pos * series[i - 1][1] * (1 - COST_PER_LEG)) / entry_cost - 1
        last_t = t
        peak = max(peak, p)
        for level, frac in ladder.tps:
            if level not in done and p >= entry_cost * level:
                done.add(level)
                sell = min(frac, pos)
                j = _first_at(series, i, t + POLL_MS)
                px = series[j][1] if j < len(series) else p
                got += sell * px * (1 - COST_PER_LEG)
                pos -= sell
                breakeven = breakeven or ladder.breakeven_after_tp1
        stop = entry_cost * (1.0 if breakeven else 1.0 - ladder.stop_frac)
        multiple = peak / entry_cost
        for level, trail in ladder.trailing:
            if multiple >= level:
                stop = max(stop, peak * (1 - trail))
                break
        if pos > 0 and p <= stop:
            j = _first_at(series, i, t + POLL_MS)
            px = series[j][1] if j < len(series) else p
            return (got + pos * px * (1 - COST_PER_LEG)) / entry_cost - 1
        i += 1
    last = series[min(i, len(series)) - 1][1]
    return (got + pos * last * (1 - COST_PER_LEG)) / entry_cost - 1


# --------------------------------------------------------------------------------------
# reading, under the database rule
# --------------------------------------------------------------------------------------


class Reader:
    """Every query on its own short read-only connection. Nothing is held across a loop."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        wal_limit: int = WAL_LIMIT_BYTES,
        sleep: Callable[[float], None] = time.sleep,
        pause_s: float = QUERY_PAUSE_S,
    ) -> None:
        self.db_path = Path(db_path)
        self.wal_limit = wal_limit
        self.sleep = sleep
        self.pause_s = pause_s
        self.queries = 0
        self.wal_waits = 0

    def _wal_bytes(self) -> int:
        try:
            return os.path.getsize(f"{self.db_path}-wal")
        except OSError:
            return 0

    def q(self, sql: str, args: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        if self.queries and self.queries % WAL_CHECK_EVERY == 0:
            waited = 0
            while self._wal_bytes() > self.wal_limit and waited < 10:
                self.wal_waits += 1
                waited += 1
                self.sleep(60)
        self.queries += 1
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=30)
        try:
            return conn.execute(sql, tuple(args)).fetchall()
        finally:
            conn.close()
            if self.pause_s:
                self.sleep(self.pause_s)


# --------------------------------------------------------------------------------------
# one signal
# --------------------------------------------------------------------------------------


@dataclass
class SignalCase:
    lane: str
    chain: str
    token: str
    t0: int
    outcome: float
    features: dict[str, Any] = field(default_factory=dict)
    wallets: tuple[str, ...] = ()


DOSSIER_KEYS: tuple[str, ...] = (
    "holder_count", "market_cap_usd", "liquidity_usd", "top10_pct", "bundler_pct",
    "token_age_s", "migrated", "launchpad",
    # 2026-10-05: smart wallets that had already sold before the signal (lanes sm_trenches).
    "smart_wallets_still_holding", "smart_wallets_sold", "smart_sold_frac_max",
)


def _is_buy(side: Any) -> bool:
    return str(side or "").strip().lower() in ("buy", "b")


def build_case(
    *,
    lane: str,
    chain: str,
    token: str,
    t0: int,
    rows: Iterable[tuple[int, Any, Any, Any, Any]],
    wallets: Sequence[str],
    payload: Mapping[str, Any],
    ladder: ExitLadder,
    kol: set[tuple[str, str]],
    trusted: set[tuple[str, str]],
    grades_now: Mapping[tuple[str, str], str],
    grades_pit: Mapping[tuple[str, str], str] | None,
    x_obs: Sequence[tuple[int, int, int]] | None = None,
) -> SignalCase | None:
    """Replay one signal from its swap rows ``(ts_ms, price, source, wallet, side)``.

    ``None`` when the tape cannot support a replay (too few prints, nothing after the fill).
    Prices come from the single source with the most priced prints. ``swaps`` mixes feeds
    whose prints disagree by >2x on 6.3% of same-token pairs, so a mixed series would invent
    highs (mooner.py rule 4). Wallet and side are read from every source, because they are
    not prices.
    """
    rows = list(rows)
    priced: list[tuple[int, float, Any]] = []
    for ts, price, source, _w, _s in rows:
        try:
            p = float(price)
        except (TypeError, ValueError):
            continue
        if p > 0:
            priced.append((int(ts), p, source))
    if len(priced) < MIN_PRINTS:
        return None
    src = collections.Counter(s for _, _, s in priced).most_common(1)[0][0]
    series = sorted((t, p) for t, p, s in priced if s == src)
    i = _first_at(series, 0, t0 + LATENCY_MS)
    if i >= len(series) - 2:
        return None
    entry = series[i][1] * (1 + COST_PER_LEG)
    outcome = simulate_exit(series, i, entry, ladder)

    sig_wallets = set(wallets)
    pre_buys = [(int(t), w) for t, _p, _s, w, side in rows if int(t) <= t0 and _is_buy(side)]
    pre_sells = [w for t, _p, _s, w, side in rows if int(t) <= t0 and not _is_buy(side) and side]
    first_smart = min((t for t, w in pre_buys if w in sig_wallets), default=None)
    pre_prices = [p for t, p in series if t <= t0]
    feats: dict[str, Any] = {
        "n_wallets": len(sig_wallets),
        "kol_pre": len({w for _, w in pre_buys if (chain, w) in kol}),
        "trusted": sum(1 for w in sig_wallets if (chain, w) in trusted),
        "gradeAB_now": sum(1 for w in sig_wallets if grades_now.get((chain, w)) in ("A", "B")),
        "gradeAB_pit": (
            None if grades_pit is None
            else sum(1 for w in sig_wallets if grades_pit.get((chain, w)) in ("A", "B"))
        ),
        "late_s": (t0 - first_smart) / 1000 if first_smart is not None else None,
        # Crowding (2026-10-05 token-selection study, 10,039 scanned tokens, honest subset with
        # prior trading): crowded tokens lost 4-10 points more per trade than uncrowded ones
        # on sol, bsc and robinhood, on both halves. Re-measured here every day.
        "buyers_30m": len({w for _, w in pre_buys if w}),
        "sellers_30m": len({w for w in pre_sells if w}),
        "buy_share_30m": (len(pre_buys) / (len(pre_buys) + len(pre_sells))) if (pre_buys or pre_sells) else None,
        "run_30m": (pre_prices[-1] / min(pre_prices) - 1) if pre_prices else None,
        "below_high_30m": (pre_prices[-1] / max(pre_prices) - 1) if pre_prices else None,
    }
    if first_smart is not None and pre_prices:
        since = [p for t, p in series if first_smart <= t <= t0]
        feats["run_from_first_smart"] = (pre_prices[-1] / since[0] - 1) if since else None
    for key in DOSSIER_KEYS:
        feats[key] = payload.get(key)
    seen = [o for o in (x_obs or ()) if o[0] <= t0 + X_DELAY_MS]
    if seen:
        _obs_ms, n_posts, max_followers = max(seen)
        feats["x_posts"], feats["x_followers"] = n_posts, max_followers
    return SignalCase(lane, chain, token, t0, outcome, feats, tuple(sorted(sig_wallets)))


# --------------------------------------------------------------------------------------
# the scorecard
# --------------------------------------------------------------------------------------


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None if v is None else float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


Band = tuple[str, Callable[[Any], bool]]


def _rng(lo: float | None, hi: float | None) -> Callable[[Any], bool]:
    def f(v: Any) -> bool:
        x = _num(v)
        return x is not None and (lo is None or x >= lo) and (hi is None or x < hi)
    return f


FEATURE_BANDS: dict[str, list[Band]] = {
    "n_wallets": [("3", _rng(3, 4)), ("4-5", _rng(4, 6)), ("6-9", _rng(6, 10)), ("10+", _rng(10, None))],
    "kol_pre": [("0", _rng(0, 1)), ("1", _rng(1, 2)), ("2-3", _rng(2, 4)), ("4+", _rng(4, None))],
    "trusted": [("0", _rng(0, 1)), ("1+", _rng(1, None))],
    "gradeAB_pit": [("0", _rng(0, 1)), ("1", _rng(1, 2)), ("2+", _rng(2, None))],
    "gradeAB_now": [("0", _rng(0, 1)), ("1", _rng(1, 2)), ("2+", _rng(2, None))],
    "buyers_30m": [("<5", _rng(None, 5)), ("5-12", _rng(5, 13)), ("13-29", _rng(13, 30)), ("30+", _rng(30, None))],
    "sellers_30m": [("0", _rng(0, 1)), ("1-2", _rng(1, 3)), ("3-11", _rng(3, 12)), ("12+", _rng(12, None))],
    "buy_share_30m": [("<50%", _rng(None, .5)), ("50-75%", _rng(.5, .75)), ("75-99%", _rng(.75, 1.0)), ("100%", _rng(1.0, None))],
    "late_s": [("<60s", _rng(None, 60)), ("1-5m", _rng(60, 300)), ("5-15m", _rng(300, 900)), ("15m+", _rng(900, None))],
    "run_30m": [("<10%", _rng(None, .1)), ("10-30%", _rng(.1, .3)), ("30-60%", _rng(.3, .6)), ("60-100%", _rng(.6, 1)), ("100%+", _rng(1, None))],
    "below_high_30m": [("<-50%", _rng(None, -.5)), ("-50..-30%", _rng(-.5, -.3)), ("-30..-15%", _rng(-.3, -.15)), ("-15..-5%", _rng(-.15, -.05)), ("at high", _rng(-.05, None))],
    "run_from_first_smart": [("<0%", _rng(None, 0)), ("0-25%", _rng(0, .25)), ("25-100%", _rng(.25, 1)), ("100%+", _rng(1, None))],
    "holder_count": [("<100", _rng(None, 100)), ("100-300", _rng(100, 300)), ("300-1000", _rng(300, 1000)), ("1000+", _rng(1000, None))],
    "market_cap_usd": [("<30k", _rng(None, 3e4)), ("30-100k", _rng(3e4, 1e5)), ("100-500k", _rng(1e5, 5e5)), ("500k+", _rng(5e5, None))],
    "liquidity_usd": [("<10k", _rng(None, 1e4)), ("10-30k", _rng(1e4, 3e4)), ("30k+", _rng(3e4, None))],
    "top10_pct": [("<20", _rng(None, 20)), ("20-35", _rng(20, 35)), ("35+", _rng(35, None))],
    "bundler_pct": [("<10", _rng(None, 10)), ("10-30", _rng(10, 30)), ("30+", _rng(30, None))],
    "token_age_s": [("<10m", _rng(None, 600)), ("10-60m", _rng(600, 3600)), ("1-6h", _rng(3600, 21600)), ("6h+", _rng(21600, None))],
    "migrated": [("no", lambda v: v is not None and not bool(v)), ("yes", lambda v: bool(v))],
    "wallet_track_hist": [("none", _rng(0, 1)), ("1+", _rng(1, None))],
    "wallet_track_best": [("<-15%", _rng(None, -.15)), ("-15..0%", _rng(-.15, 0)), ("0..+10%", _rng(0, .10)), ("+10%+", _rng(.10, None))],
    "wallet_track_good": [("0", _rng(0, 1)), ("1", _rng(1, 2)), ("2+", _rng(2, None))],
    "smart_wallets_sold": [("0", _rng(0, 1)), ("1", _rng(1, 2)), ("2-3", _rng(2, 4)), ("4+", _rng(4, None))],
    "smart_wallets_still_holding": [("0-2", _rng(None, 3)), ("3", _rng(3, 4)), ("4-5", _rng(4, 6)), ("6+", _rng(6, None))],
    "smart_sold_frac_max": [("<25%", _rng(None, .25)), ("25-50%", _rng(.25, .5)), ("50-90%", _rng(.5, .9)), ("90%+", _rng(.9, None))],
    "x_posts": [("0", _rng(0, 1)), ("1-4", _rng(1, 5)), ("5-19", _rng(5, 20)), ("20+", _rng(20, None))],
    "x_followers": [("<1k", _rng(None, 1e3)), ("1k-10k", _rng(1e3, 1e4)), ("10k-100k", _rng(1e4, 1e5)), ("100k+", _rng(1e5, None))],
}


@dataclass(frozen=True)
class Stat:
    n: int
    mean: float | None
    median: float | None
    win: float | None

    @classmethod
    def of(cls, outcomes: Sequence[float]) -> Stat:
        xs = [max(-1.0, min(x, OUTCOME_CAP)) for x in outcomes]
        if not xs:
            return cls(0, None, None, None)
        return cls(
            len(xs), sum(xs) / len(xs), statistics.median(xs), sum(x > 0 for x in xs) / len(xs)
        )


@dataclass(frozen=True)
class Cell:
    lane: str
    chain: str
    feature: str
    band: str
    all: Stat
    old: Stat
    new: Stat
    verdict: str


def verdict(old: Stat, new: Stat, base_old: Stat, base_new: Stat) -> str:
    """``edge`` / ``lift`` / ``drag`` / ``noise``. Both halves must agree for anything but noise."""
    if old.n < MIN_HALF or new.n < MIN_HALF or old.mean is None or new.mean is None:
        return "thin"
    if old.mean > 0 and new.mean > 0:
        return "edge"
    bo, bn = base_old.mean or 0.0, base_new.mean or 0.0
    if old.mean >= bo + LIFT_PTS and new.mean >= bn + LIFT_PTS:
        return "lift"
    if old.mean <= bo - LIFT_PTS and new.mean <= bn - LIFT_PTS:
        return "drag"
    return "noise"


@dataclass
class Scorecard:
    baselines: dict[tuple[str, str], tuple[Stat, Stat, Stat]] = field(default_factory=dict)
    cells: list[Cell] = field(default_factory=list)
    persistence: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)


def score(cases: Sequence[SignalCase]) -> Scorecard:
    """Band every feature per (lane, chain), split at the median signal time."""
    card = Scorecard()
    groups: dict[tuple[str, str], list[SignalCase]] = collections.defaultdict(list)
    for c in cases:
        groups[(c.lane, c.chain)].append(c)
    for (lane, chain), group in sorted(groups.items()):
        group.sort(key=lambda c: c.t0)
        mid = group[len(group) // 2].t0
        old = [c for c in group if c.t0 < mid]
        new = [c for c in group if c.t0 >= mid]
        b_all, b_old, b_new = (Stat.of([c.outcome for c in g]) for g in (group, old, new))
        card.baselines[(lane, chain)] = (b_all, b_old, b_new)
        for feature, bands in FEATURE_BANDS.items():
            for name, test in bands:
                pick = [c for c in group if test(c.features.get(feature))]
                if not pick:
                    continue
                s_old = Stat.of([c.outcome for c in pick if c.t0 < mid])
                s_new = Stat.of([c.outcome for c in pick if c.t0 >= mid])
                v = verdict(s_old, s_new, b_old, b_new)
                if feature.endswith("_now") and v not in ("thin", "noise"):
                    v = "lookahead:" + v
                elif feature.startswith("x_") and v not in ("thin", "noise"):
                    v = "delayed:" + v
                card.cells.append(Cell(lane, chain, feature, name,
                                       Stat.of([c.outcome for c in pick]), s_old, s_new, v))
        card.persistence[(lane, chain)] = wallet_persistence(old, new, b_new)
    return card


#: A wallet needs this many RESOLVED earlier signals before its track record is read.
TRACK_MIN = 3
#: A wallet whose resolved signals average at least this is "good" for wallet_track_good.
TRACK_GOOD = 0.05


def add_wallet_track(cases: Sequence[SignalCase]) -> None:
    """Give each signal the point-in-time track record of its wallets, per chain.

    A wallet's record at t0 is the mean replayed outcome of the earlier signals it joined whose
    outcome was already settled: signal time + :data:`HORIZON_MS` at or before t0. That is the
    question "were these wallets good, as far as we could know when they bought?", answered
    from our own replay and refreshed every day. It is the honest way to gather good wallets:
    MEASURED 2026-10-05, wallets that were good on the older half persisted on robinhood
    (+6.6% on newer signals vs -3.1%) and reversed on sol (-31%). The audit's two halves
    decide per chain whether the record means anything.

    Sets ``wallet_track_hist`` (wallets with a record), ``wallet_track_best`` (best record) and
    ``wallet_track_good`` (wallets at or above :data:`TRACK_GOOD`). Without a record, the
    latter two are ``None`` (unmeasured), never 0.
    """
    by_chain: dict[str, list[SignalCase]] = collections.defaultdict(list)
    for c in cases:
        by_chain[c.chain].append(c)
    for group in by_chain.values():
        group.sort(key=lambda c: c.t0)
        settled = sorted(group, key=lambda c: c.t0 + HORIZON_MS)
        stats: dict[str, list[float]] = collections.defaultdict(lambda: [0.0, 0])
        k = 0
        for c in group:
            while k < len(settled) and settled[k].t0 + HORIZON_MS <= c.t0:
                done = settled[k]
                x = max(-1.0, min(done.outcome, OUTCOME_CAP))
                for w in done.wallets:
                    st = stats[w]
                    st[0] += x
                    st[1] += 1
                k += 1
            records = [stats[w][0] / stats[w][1] for w in c.wallets if stats.get(w) and stats[w][1] >= TRACK_MIN]
            c.features["wallet_track_hist"] = len(records)
            c.features["wallet_track_best"] = max(records) if records else None
            c.features["wallet_track_good"] = sum(1 for r in records if r >= TRACK_GOOD) if records else None


def top_wallets(cases: Sequence[SignalCase], *, min_signals: int = 5, limit: int = 50) -> list[dict[str, Any]]:
    """Per chain, the wallets with the best replayed record over the window (>= ``min_signals``)."""
    acc: dict[tuple[str, str], list[float]] = collections.defaultdict(list)
    for c in cases:
        for w in c.wallets:
            acc[(c.chain, w)].append(max(-1.0, min(c.outcome, OUTCOME_CAP)))
    rows = [
        {"chain": ch, "address": w, "n": len(v), "mean": sum(v) / len(v),
         "win": sum(1 for x in v if x > 0) / len(v)}
        for (ch, w), v in acc.items() if len(v) >= min_signals
    ]
    out: list[dict[str, Any]] = []
    for ch in sorted({r["chain"] for r in rows}):
        out += sorted((r for r in rows if r["chain"] == ch), key=lambda r: -r["mean"])[:limit]
    return out


def wallet_persistence(
    old: Sequence[SignalCase], new: Sequence[SignalCase], base_new: Stat, *, min_signals: int = 5,
) -> dict[str, Any]:
    """Do wallets that did well on the older half do well on the newer half?

    MEASURED 2026-10-05 on sol: no. The 14 "good" wallets' newer signals averaged -29.2%
    against -17.2% for all newer signals. When this stays false, a wallet list built from our
    own outcomes is a list of last week's luck.
    """
    rec: dict[str, list[float]] = collections.defaultdict(list)
    for c in old:
        for w in c.wallets:
            rec[w].append(max(-1.0, min(c.outcome, OUTCOME_CAP)))
    good = {w for w, v in rec.items() if len(v) >= min_signals and sum(v) / len(v) > LIFT_PTS}
    bad = {w for w, v in rec.items() if len(v) >= min_signals and sum(v) / len(v) < -0.15}
    s_good = Stat.of([c.outcome for c in new if good & set(c.wallets)])
    s_bad = Stat.of([c.outcome for c in new if bad & set(c.wallets)])
    persists = (
        s_good.n >= MIN_HALF and s_good.mean is not None and base_new.mean is not None
        and s_good.mean >= base_new.mean + LIFT_PTS
    )
    return {
        "good_wallets": len(good), "good_new": s_good.__dict__,
        "bad_wallets": len(bad), "bad_new": s_bad.__dict__,
        "persists": persists,
    }


# --------------------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------------------


@dataclass
class AuditRun:
    cases: list[SignalCase]
    card: Scorecard
    considered: int
    no_tape: int
    stopped: str | None
    queries: int
    wal_waits: int


def _pit_grades(reader: Reader, chains: Sequence[str]) -> dict[str, dict[tuple[str, str], str]]:
    """Snapshot day -> {(chain, address): grade}. Empty until :func:`snapshot_grades` has run."""
    out: dict[str, dict[tuple[str, str], str]] = collections.defaultdict(dict)
    marks = ",".join("?" * len(chains))
    try:
        rows = reader.q(
            f"SELECT day, chain, address, grade FROM wallet_grade_snapshots "
            f"WHERE chain IN ({marks}) AND grade IN ('A','B')", chains,
        )
    except sqlite3.OperationalError:
        return {}
    for day, ch, addr, g in rows:
        out[day][(ch, addr)] = g
    return dict(out)


def _utc_day(ms: int) -> str:
    return (_EPOCH + datetime.timedelta(milliseconds=int(ms))).strftime("%Y-%m-%d")


def run_audit(
    reader: Reader,
    *,
    ladder: ExitLadder,
    lanes: Sequence[str] = DEFAULT_LANES,
    chains: Sequence[str] = DEFAULT_CHAINS,
    days: int = 12,
    max_signals: int = 2500,
    now_ms: int | None = None,
    deadline_ms: int | None = None,
    clock: Callable[[], float] = time.time,
) -> AuditRun:
    now_ms = now_ms if now_ms is not None else int(clock() * 1000)
    since = now_ms - days * 86_400_000
    lmarks, cmarks = ",".join("?" * len(lanes)), ",".join("?" * len(chains))

    kol: set[tuple[str, str]] = set()
    for ch, addr in reader.q(
        f"SELECT chain, address FROM wallet_feed_tags WHERE tag='kol' AND chain IN ({cmarks})",
        chains,
    ):
        kol.add((ch, addr))
    trusted = {
        (ch, a) for ch, a in reader.q(
            f"SELECT chain, address FROM wallets WHERE cohort='trusted_copy' AND chain IN ({cmarks})",
            chains,
        )
    }
    grades_now = {
        (ch, a): g for ch, a, g in reader.q(
            f"SELECT chain, address, grade FROM wallet_scores WHERE grade IN ('A','B') "
            f"AND chain IN ({cmarks})", chains,
        )
    }
    snapshots = _pit_grades(reader, chains)
    snap_days = sorted(snapshots)
    x_by_token: dict[tuple[str, str], list[tuple[int, int, int]]] = collections.defaultdict(list)
    try:
        for ch, tok, ms, n, f in reader.q(
            f"SELECT chain, token, observed_ms, COALESCE(n_posts,0), COALESCE(max_followers,0) "
            f"FROM x_token_obs WHERE ok=1 AND observed_ms > ? AND chain IN ({cmarks})",
            [since - 86_400_000, *chains],
        ):
            x_by_token[(ch, tok)].append((int(ms), int(n), int(f)))
    except sqlite3.OperationalError:
        pass  # table not migrated yet: no X features, which is "unmeasured", not zero

    signals = reader.q(
        f"SELECT lane, chain, token, created_ms, wallets_json, payload_json FROM signals "
        f"WHERE rowid IN (SELECT MIN(rowid) FROM signals WHERE lane IN ({lmarks}) "
        f"AND chain IN ({cmarks}) AND created_ms > ? "
        f"GROUP BY lane, chain, token, created_ms / 86400000) ORDER BY created_ms DESC LIMIT ?",
        [*lanes, *chains, since, max_signals],
    )
    cases: list[SignalCase] = []
    no_tape = 0
    stopped = None
    for lane, chain, token, t0, wj, pj in signals:
        if deadline_ms is not None and int(clock() * 1000) > deadline_ms:
            stopped = "deadline"
            break
        rows = reader.q(
            "SELECT ts_ms, price_usd, source, wallet, side FROM swaps "
            "WHERE token=? AND chain=? AND ts_ms BETWEEN ? AND ?",
            (token, chain, int(t0) - PRE_WINDOW_MS, int(t0) + HORIZON_MS),
        )
        try:
            wallets = [str(w) for w in json.loads(wj or "[]")]
        except (TypeError, ValueError):
            wallets = []
        try:
            payload = json.loads(pj) if pj and str(pj).lstrip().startswith("{") else {}
        except (TypeError, ValueError):
            payload = {}
        sday = _utc_day(int(t0))
        prior = [d for d in snap_days if d < sday]
        pit = snapshots[prior[-1]] if prior else None
        case = build_case(
            lane=lane, chain=chain, token=token, t0=int(t0), rows=rows, wallets=wallets,
            payload=payload if isinstance(payload, dict) else {}, ladder=ladder, kol=kol,
            trusted=trusted, grades_now=grades_now, grades_pit=pit,
            x_obs=x_by_token.get((chain, token)),
        )
        if case is None:
            no_tape += 1
            continue
        cases.append(case)
    add_wallet_track(cases)
    return AuditRun(cases, score(cases), len(signals), no_tape, stopped,
                    reader.queries, reader.wal_waits)


# --------------------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------------------


def record(conn: sqlite3.Connection, run: AuditRun, *, now_ms: int, days: int) -> str:
    """Write the run and its cells in one short transaction. Returns the run id."""
    run_id = f"audit_{now_ms}"
    summary = {
        "version": AUDIT_VERSION, "days": days, "considered": run.considered,
        "replayed": len(run.cases), "no_tape": run.no_tape, "stopped": run.stopped,
        "baselines": {
            f"{lane}|{chain}": {"all": a.__dict__, "old": o.__dict__, "new": n.__dict__}
            for (lane, chain), (a, o, n) in run.card.baselines.items()
        },
        "persistence": {f"{ln}|{ch}": v for (ln, ch), v in run.card.persistence.items()},
    }
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO signal_audit_runs (run_id, ts_ms, version, summary_json) VALUES (?,?,?,?)",
            (run_id, now_ms, AUDIT_VERSION, json.dumps(summary)),
        )
        conn.executemany(
            "INSERT INTO audit_wallets (run_id, chain, address, n, mean, win) VALUES (?,?,?,?,?,?)",
            [(run_id, w["chain"], w["address"], w["n"], w["mean"], w["win"]) for w in top_wallets(run.cases)],
        )
        conn.executemany(
            "INSERT INTO signal_audit_cells (run_id, lane, chain, feature, band, n, mean, median, "
            "win, old_n, old_mean, new_n, new_mean, verdict) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (run_id, c.lane, c.chain, c.feature, c.band, c.all.n, c.all.mean, c.all.median,
                 c.all.win, c.old.n, c.old.mean, c.new.n, c.new.mean, c.verdict)
                for c in run.card.cells
            ],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return run_id


def snapshot_grades(conn: sqlite3.Connection, *, now_ms: int, keep_days: int = 90) -> dict[str, int]:
    """Freeze today's A/B/C grades so a later audit can ask what a wallet WAS when it bought."""
    day = _utc_day(now_ms)
    cutoff = _utc_day(now_ms - keep_days * 86_400_000)
    conn.execute("BEGIN")
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO wallet_grade_snapshots (day, chain, address, grade, score) "
            "SELECT ?, chain, address, grade, score FROM wallet_scores WHERE grade IN ('A','B','C')",
            (day,),
        )
        inserted = cur.rowcount
        pruned = conn.execute(
            "DELETE FROM wallet_grade_snapshots WHERE day < ?", (cutoff,)
        ).rowcount
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"day": day, "inserted": int(inserted), "pruned": int(pruned)}  # type: ignore[dict-item]


def latest(conn: sqlite3.Connection, *, include_noise: bool = False) -> dict[str, Any] | None:
    """The newest scorecard, for the operator agent: baselines plus every non-noise cell."""
    row = conn.execute(
        "SELECT run_id, ts_ms, summary_json FROM signal_audit_runs ORDER BY ts_ms DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    run_id, ts_ms, summary = row[0], row[1], row[2]
    where = "" if include_noise else "AND verdict NOT IN ('noise', 'thin')"
    cells = conn.execute(
        f"SELECT lane, chain, feature, band, n, mean, median, win, old_n, old_mean, new_n, "
        f"new_mean, verdict FROM signal_audit_cells WHERE run_id=? {where} "
        f"ORDER BY lane, chain, feature, band",
        (run_id,),
    ).fetchall()
    keys = ("lane", "chain", "feature", "band", "n", "mean", "median", "win", "old_n",
            "old_mean", "new_n", "new_mean", "verdict")
    try:
        wallets = conn.execute(
            "SELECT chain, address, n, mean, win FROM audit_wallets WHERE run_id=? "
            "ORDER BY chain, mean DESC", (run_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        wallets = []
    per_chain: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for ch, addr, n, mean, win in wallets:
        if len(per_chain[ch]) < 15:
            per_chain[ch].append({"address": addr, "n": n, "mean": mean, "win": win})
    return {
        "run_id": run_id, "ts_ms": ts_ms, "summary": json.loads(summary),
        "cells": [dict(zip(keys, tuple(c), strict=True)) for c in cells],
        "top_wallets": dict(per_chain),
    }


def lines(run: AuditRun) -> list[str]:
    """Human-readable scorecard."""
    out = [f"signal audit: considered {run.considered}, replayed {len(run.cases)}, "
           f"no tape {run.no_tape}, stopped={run.stopped}"]

    def fmt(s: Stat) -> str:
        if not s.n or s.mean is None:
            return "n=0"
        return f"n={s.n} mean={100 * s.mean:+.1f}% med={100 * (s.median or 0):+.1f}% win={100 * (s.win or 0):.0f}%"

    for (lane, chain), (a, o, n) in sorted(run.card.baselines.items()):
        out.append(f"== {lane} {chain}: {fmt(a)} | old {fmt(o)} | new {fmt(n)}")
        for c in run.card.cells:
            if (c.lane, c.chain) == (lane, chain) and c.verdict not in ("noise", "thin"):
                out.append(f"   {c.verdict:16s} {c.feature}={c.band}: old {fmt(c.old)} | new {fmt(c.new)}")
        out.append(f"   wallet persistence: {run.card.persistence.get((lane, chain))}")
    return out
