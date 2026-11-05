"""The 7-day paper sprint (owner decision 2026-10-08: "make the agent profitable ... or we shut down").

Live buying is off for the whole sprint; every lane trades on PAPER. Each strategy (lane x chain)
is scored every day against a pass line declared HERE, before any sprint data existed:

* at least :data:`MIN_N` replayed trades since the sprint started, at least :data:`MIN_HALF` in
  each half of the elapsed window;
* mean net return per trade above zero in BOTH halves;
* the :data:`BOOT_Q` bootstrap lower bound of the pooled mean above zero.

Returns are the daily signal-audit replay (live exit ladder, 30 s entry latency) with the
chain's measured cost per side: the replay charges :data:`signal_audit.COST_PER_LEG` (3.25%)
per leg, and :data:`EXTRA_COST_PER_SIDE` adds what robinhood and bsc cost on top
(``fill_costs``: robinhood 3.7-5.7%/leg, bsc ~4%). On day 7 a PASS goes live at the chain's
minimum size (the owner arms it); no PASS -> live trading shuts down.

Strategies the replay cannot price (the tweet launcher: a shadow launch creates no token, so
there is nothing to mark) are listed as UNMEASURABLE and stay off.
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

SPRINT_DAYS = 7
MIN_N = 40
MIN_HALF = 15
BOOT_Q = 0.10            # 90% one-sided lower bound
BOOT_RESAMPLES = 2000
OUTCOME_CAP = 3.0        # same cap as the audit: one +1000% print cannot carry a strategy
LANES = ("sm-trenches", "trusted-copy", "launch-snipe")
UNMEASURABLE = {"tweet-launch": "a shadow launch creates no token, so a paper outcome cannot exist"}
#: Cost per side above the replay's 3.25%, MEASURED by fill_costs on our own fills.
EXTRA_COST_PER_SIDE = {"sol": 0.0, "robinhood": 0.0125, "bsc": 0.0075}
#: PRE-DECLARED VARIANTS (2026-10-08, before any sprint data): the only signal-audit cells that
#: made money on BOTH halves of the pre-sprint replay (audit_1791440789560). Each is scored
#: against the same pass line. Three variants on top of the base strategies is a small family;
#: a variant must still clear the 90% lower bound, so luck alone is unlikely to pass one.
#:   (label, lane, chain, feature, low inclusive, high exclusive, pre-sprint evidence)
VARIANTS: tuple[tuple[str, str, str, str, float | None, float | None, str], ...] = (
    ("buyers<50%", "launch-snipe", "sol", "buy_share_30m", None, 0.50,
     "n61 +5.6% (old +6.9%, new +4.7%)"),
    ("below-first-smart", "launch-snipe", "sol", "run_from_first_smart", None, 0.0,
     "n179 +4.5% (old +8.1%, new +1.0%)"),
    ("buyers50-75%", "sm-trenches", "bsc", "buy_share_30m", 0.50, 0.75,
     "n51 +4.0% (old +5.9%, new +1.4%)"),
)
KV_START = "sprint:start_ms"
KV_SCORECARD = "sprint:scorecard"
#: Structured scores (one dict per strategy) for kaiba.learning.paper_report, which sends the
#: owner's daily message. Added 2026-10-13.
KV_RESULTS = "sprint:results"
#: 2026-10-13: the scorecard is ROLLING over this many days. The 7-day sprint never ended in
#: code (its window slid from day 8 while "DAY 7 RESULT" repeated every day). 12 days: the
#: replay reads swaps from 30 min before to 6 h after each signal, and swaps retention keeps 14.
ROLLING_DAYS = 12


@dataclass
class Score:
    lane: str
    chain: str
    n: int
    n_old: int
    n_new: int
    mean: float | None
    mean_old: float | None
    mean_new: float | None
    lower: float | None
    verdict: str          # PASS | FAIL | PENDING
    why: str


def net(outcome: float, chain: str) -> float:
    """The replayed outcome with the chain's extra cost on both sides, capped like the audit."""
    extra = EXTRA_COST_PER_SIDE.get(chain, 0.0)
    adj = (1.0 + outcome) * (1.0 - extra) / (1.0 + extra) - 1.0
    return max(-1.0, min(OUTCOME_CAP, adj))


def lower_bound(xs: Sequence[float], *, q: float = BOOT_Q, resamples: int = BOOT_RESAMPLES,
                seed: int = 7) -> float | None:
    if len(xs) < 2:
        return None
    rng = random.Random(seed)
    n = len(xs)
    means = sorted(statistics.fmean(rng.choices(xs, k=n)) for _ in range(resamples))
    return means[int(q * resamples)]


def score(lane: str, chain: str, rows: Sequence[tuple[int, float]], start_ms: int, now_ms: int) -> Score:
    """``rows`` = (t0_ms, net return) for one strategy since the sprint started."""
    mid = start_ms + (now_ms - start_ms) // 2
    old = [r for t, r in rows if t < mid]
    new = [r for t, r in rows if t >= mid]
    allr = old + new
    mean = statistics.fmean(allr) if allr else None
    m_old = statistics.fmean(old) if old else None
    m_new = statistics.fmean(new) if new else None
    lb = lower_bound(allr)
    if len(allr) < MIN_N or len(old) < MIN_HALF or len(new) < MIN_HALF:
        verdict, why = "PENDING", f"needs n>={MIN_N} and >={MIN_HALF} per half (have {len(allr)}: {len(old)}/{len(new)})"
    elif m_old is not None and m_new is not None and m_old > 0 and m_new > 0 and lb is not None and lb > 0:
        verdict, why = "PASS", "positive on both halves and the 90% lower bound"
    else:
        bad = []
        if not (m_old and m_old > 0):
            bad.append("older half <= 0")
        if not (m_new and m_new > 0):
            bad.append("newer half <= 0")
        if not (lb is not None and lb > 0):
            bad.append("90% lower bound <= 0")
        verdict, why = "FAIL", ", ".join(bad)
    return Score(lane, chain, len(allr), len(old), len(new), mean, m_old, m_new, lb, verdict, why)


def _in_band(value: Any, low: float | None, high: float | None) -> bool:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False          # an unmeasured feature is not in any band
    return (low is None or v >= low) and (high is None or v < high)


def scorecard(cases: Sequence[Any], start_ms: int, now_ms: int) -> list[Score]:
    """One :class:`Score` per (lane, chain) since ``start_ms``, then one per :data:`VARIANTS`."""
    groups: dict[tuple[str, str], list[tuple[int, float]]] = {}
    for c in cases:
        if int(c.t0) < start_ms or c.lane not in LANES:
            continue
        row = (int(c.t0), net(float(c.outcome), c.chain))
        groups.setdefault((c.lane, c.chain), []).append(row)
        feats = getattr(c, "features", None) or {}
        for label, lane, chain, feature, low, high, _ev in VARIANTS:
            if c.lane == lane and c.chain == chain and _in_band(feats.get(feature), low, high):
                groups.setdefault((f"{lane}[{label}]", chain), []).append(row)
    return [score(lane, chain, rows, start_ms, now_ms) for (lane, chain), rows in sorted(groups.items())]


def day_of(start_ms: int, now_ms: int) -> int:
    return int((now_ms - start_ms) // 86_400_000) + 1


def results(scores: Sequence[Score]) -> list[dict[str, Any]]:
    """JSON-ready scores for ``kv sprint:results``."""
    return [{"lane": s.lane, "chain": s.chain, "n": s.n, "n_old": s.n_old, "n_new": s.n_new,
             "mean": s.mean, "mean_old": s.mean_old, "mean_new": s.mean_new, "lower": s.lower,
             "verdict": s.verdict, "why": s.why} for s in scores]


def render(scores: Sequence[Score], start_ms: int, now_ms: int) -> str:
    def pct(x: float | None) -> str:
        return "  n/a " if x is None else f"{100 * x:+5.1f}%"
    lines = [f"Kaiba replay scoreboard - rolling {ROLLING_DAYS} d (live buying OFF)"]
    for s in scores:
        lines.append(f"{s.verdict:7s} {s.lane}/{s.chain}: n={s.n} mean {pct(s.mean)} "
                     f"(old {pct(s.mean_old)}, new {pct(s.mean_new)}) low90 {pct(s.lower)}")
    for name, why in UNMEASURABLE.items():
        lines.append(f"OFF     {name}: {why}")
    passed = [f"{s.lane}/{s.chain}" for s in scores if s.verdict == "PASS"]
    lines.append("Passing now: " + (", ".join(passed) if passed else "none yet"))
    return "\n".join(lines)


__all__ = ["Score", "net", "lower_bound", "score", "scorecard", "render", "results", "day_of",
           "SPRINT_DAYS", "MIN_N", "MIN_HALF", "LANES", "KV_START", "KV_SCORECARD", "KV_RESULTS",
           "ROLLING_DAYS"]
