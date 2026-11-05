"""The strategy factory, v1 (PROFIT-2, 2026-10-13): an automatic search for strategies that make
money on the replay, built so that noise is hard to pass off as an edge.

Why this module exists
----------------------

Owner, 2026-10-13: find methods that actually make money. Until today the only search was
hand-written tweaks: 10 experiments, 0 passes. A search that tries thousands of candidates will
ALWAYS find some that looked profitable on the data it was fitted to; that is arithmetic, not
an edge. So the design is three filters in series, and only the last one counts:

1. **Selection on the older part of the window (training).** Every candidate is scored on the
   older :attr:`SearchConfig.train_frac` of each lane x chain's signals. The band edges a filter
   uses are quantiles of the TRAINING data only, so nothing about the newer part leaks into
   the candidate's definition. Candidates with a positive training mean are ranked by their
   training t-statistic; at most :attr:`SearchConfig.max_selected_per_group` per group go on.
2. **Holdout on the newer part, corrected for the number carried forward.** Each selected
   candidate gets a one-sided Student-t p-value for "mean > 0" on the newer part, and the whole
   set (every group, every exit) is passed through Benjamini-Hochberg at
   :attr:`SearchConfig.fdr_q`. Because selection used only the older data, the holdout
   p-values are honest for the candidates that reached it, and the family the correction must
   cover is the number that reached it (``selected``), not everything ever tried (``tried``,
   which is reported beside it). Candidates overlap (same signals, nested bands, correlated
   exits); that is positive dependence, under which BH still controls the false discovery
   rate. ``method: by`` (Benjamini-Yekutieli, valid under ANY dependence) is the stricter
   option. A survivor must ALSO pass the owner's pass line (:func:`kaiba.learning.sprint.score`:
   n >= 40, >= 15 per half, mean > 0 in both halves, 90% bootstrap lower bound > 0) on the
   holdout, net of the chain's cost.
3. **Forward tracking: the only record that matters.** A survivor is frozen (filter cut points,
   exit-ladder numbers, ``selected_ms``) in ``strategy_candidates``. Every day it is scored ONLY
   on signals whose ``t0`` is after ``selected_ms`` and whose 6 h replay horizon has elapsed,
   against the same pass line. Tracking many candidates forward is itself a multiple test, so
   the forward verdict is PASS only when the pass line holds AND the forward p-value survives
   BH across every candidate tracked that day; a pass-line pass that does not is reported as
   ``UNCORRECTED``. The owner's daily report calls a candidate ready only after a forward PASS
   on :data:`kaiba.learning.paper_report.READY_DAYS` consecutive reports -- the same rule as
   the lane scoreboard.

What a candidate is: (lane, chain, entry filter, exit policy).

* **Entry filters.** No filter (the lane as it is); one band of one feature (tertiles and
  quartiles of the training data by default); or two such bands on different features, built
  only from the top :attr:`SearchConfig.pair_top_k` single bands by training t-statistic and
  capped at :attr:`SearchConfig.max_pairs`. Categorical features (``launchpad``) give one band
  per value seen at least ``min_train`` times in training. Features left out on purpose are in
  :data:`EXCLUDED_FEATURES` with the reason.
* **Exit policies** (:func:`declared_policies`): the live ladder (the incumbent, read from
  risk.yaml exactly as the audit reads it) and a small declared set of alternatives that are
  simulated on EXACTLY the path the audit replayed (:func:`kaiba.learning.signal_audit.price_path`,
  same single price source, same 30 s entry, same 12 s exit fill delay, same 3.25%/leg):
  tighter and looser stops, tighter trailing, take-everything at 1.5x / 2x, and time exits at
  15 / 30 / 60 minutes on top of the live ladder. Left out because the tape cannot simulate
  them faithfully: limit/pullback ENTRIES (a different entry is a different trade, not an
  exit), depth-aware slippage (swaps carry no depth), and leader-exit (the tape's wallet
  coverage after entry is not the live feed's; it is PROFIT-3's job to measure first).

What it does NOT do: it never changes a gate, a lane, a mode, a size or risk.yaml. It writes
four ``strategy_*`` tables (migration 047) and the owner reads the result in the daily report.

Database discipline (AGENTS.md DATABASE READ RULE): the tape is read once, through
:func:`kaiba.learning.signal_audit.run_audit` (one short read-only connection per query, WAL
checked), and the alternative exits are simulated in the ``on_case`` hook while each signal's
rows are in memory. All writes happen in one short transaction at the end. The search itself
stops at its deadline and records a labelled partial.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime
import hashlib
import json
import logging
import math
import sqlite3
import statistics
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from kaiba.learning import signal_audit as sa
from kaiba.learning import sprint

log = logging.getLogger(__name__)

SEARCH_VERSION = "strategy_search/2"  # /2 2026-10-14: replay price source chosen without lookahead
DAY_MS = 86_400_000
#: A signal is scored only once its whole replay has happened: entry latency + horizon + one
#: exit fill delay. A younger signal's "outcome" is a mark at whatever print came last.
SETTLE_MS = sa.LATENCY_MS + sa.HORIZON_MS + sa.POLL_MS

#: Features never used in a filter, and why. A feature that is not knowable at entry time
#: turns the holdout into a lookahead test.
EXCLUDED_FEATURES: dict[str, str] = {
    "gradeAB_now": "today's wallet grade, not the grade at signal time (lookahead)",
    "x_posts": "X is read up to 5 min after the signal; we enter at +30 s",
    "x_followers": "X is read up to 5 min after the signal; we enter at +30 s",
}


# --------------------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchConfig:
    """Knobs for one search. Every default is INVENTED unless it says otherwise; each one only
    makes the search stricter or smaller, never looser than the owner's pass line."""

    #: Band families per numeric feature: tertiles and quartiles of the TRAINING data.
    quantiles: tuple[int, ...] = (3, 4)
    #: A candidate needs this many training trades to be tried at all.
    min_train: int = 30
    #: Pairs are built from this many best single bands (by training t) ...
    pair_top_k: int = 8
    #: ... and at most this many pairs per group. Caps the family size.
    max_pairs: int = 28
    #: Categorical values per feature (most common first).
    max_categories: int = 6
    #: Carried from training to the holdout per lane x chain, best training t first.
    max_selected_per_group: int = 20
    #: Older share of each group's signals (by count) used for selection.
    train_frac: float = 0.5
    #: A group with fewer settled signals than this is not searched: the holdout could not reach
    #: the pass line's n >= 40 with 15 per half.
    min_group: int = 2 * sprint.MIN_N
    #: False discovery rate for the holdout AND the forward family.
    fdr_q: float = 0.10
    #: ``bh`` (Benjamini-Hochberg, independent or positively dependent tests) or ``by``
    #: (Benjamini-Yekutieli, any dependence; stricter by a factor of ~ln m).
    method: str = "bh"
    #: New candidates persisted per run (best holdout p first) ...
    max_new_per_run: int = 10
    #: ... and at most this many tracked at once. The forward family is corrected too, but a
    #: small family is what keeps the forward test powerful.
    max_tracked: int = 30
    #: Retire a candidate whose forward record FAILs the pass line at this many trades ...
    retire_fail_n: int = 2 * sprint.MIN_N
    #: ... or that has not passed after this many days of tracking.
    retire_after_days: int = 28

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> SearchConfig:
        """Build from a schedule.yaml job block; unknown keys are ignored, bad ones raise."""
        base = cls()
        kw: dict[str, Any] = {}
        for f in dataclasses.fields(cls):
            if params.get(f.name) is None:
                continue
            raw = params[f.name]
            cur = getattr(base, f.name)
            if isinstance(cur, tuple):
                items = raw.split(",") if isinstance(raw, str) else list(raw)
                kw[f.name] = tuple(int(x) for x in items if str(x).strip())
            elif isinstance(cur, bool):
                kw[f.name] = bool(raw)
            elif isinstance(cur, int):
                kw[f.name] = int(raw)
            elif isinstance(cur, float):
                kw[f.name] = float(raw)
            else:
                kw[f.name] = str(raw)
        cfg = dataclasses.replace(base, **kw)
        if cfg.method not in ("bh", "by"):
            raise ValueError(f"strategy_search.method must be bh or by, not {cfg.method!r}")
        if not 0 < cfg.fdr_q < 1 or not 0 < cfg.train_frac < 1:
            raise ValueError("strategy_search: fdr_q and train_frac must be in (0, 1)")
        return cfg


# --------------------------------------------------------------------------------------
# exit policies
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExitPolicy:
    """An exit ladder plus an optional maximum hold. ``name`` is a label; :attr:`key` is the
    numbers, so two names for the same numbers are one policy."""

    name: str
    ladder: sa.ExitLadder
    hold_ms: int | None = None

    def spec(self) -> dict[str, Any]:
        lad = self.ladder
        return {"name": self.name, "stop_frac": lad.stop_frac, "tps": [list(x) for x in lad.tps],
                "trailing": [list(x) for x in lad.trailing],
                "breakeven_after_tp1": lad.breakeven_after_tp1, "hold_ms": self.hold_ms}

    @property
    def key(self) -> str:
        numbers = {k: v for k, v in self.spec().items() if k != "name"}
        return json.dumps(numbers, sort_keys=True)

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> ExitPolicy:
        lad = sa.ExitLadder(
            stop_frac=float(spec["stop_frac"]),
            tps=tuple((float(m), float(f)) for m, f in spec["tps"]),
            trailing=tuple((float(m), float(t)) for m, t in spec["trailing"]),
            breakeven_after_tp1=bool(spec["breakeven_after_tp1"]),
        )
        hold = spec.get("hold_ms")
        return cls(str(spec.get("name") or "custom"), lad, None if hold is None else int(hold))


def declared_policies(base: sa.ExitLadder) -> list[ExitPolicy]:
    """The live ladder first, then the declared alternatives (all INVENTED as hypotheses; none is
    a recommendation). Duplicates of the live numbers are dropped."""
    rep = dataclasses.replace
    cands = [
        ExitPolicy("live", base),
        ExitPolicy("stop20", rep(base, stop_frac=0.20)),
        ExitPolicy("stop40", rep(base, stop_frac=0.40)),
        ExitPolicy("trail_half", rep(base, trailing=tuple((m, t / 2) for m, t in base.trailing))),
        ExitPolicy("tp_all_1.5x", rep(base, tps=((1.5, 1.0),))),
        ExitPolicy("tp_all_2x", rep(base, tps=((2.0, 1.0),))),
        ExitPolicy("hold15m", base, 15 * 60_000),
        ExitPolicy("hold30m", base, 30 * 60_000),
        ExitPolicy("hold60m", base, 60 * 60_000),
    ]
    seen: set[str] = set()
    out = []
    for p in cands:
        if p.key not in seen:
            seen.add(p.key)
            out.append(p)
    return out


def _simulate_with_hold(series: Sequence[tuple[int, float]], i0: int, entry_cost: float,
                        ladder: sa.ExitLadder, hold_ms: int) -> float:
    """:func:`signal_audit.simulate_exit` plus a time exit at ``hold_ms`` after the entry print.

    The loop is the audit's, line for line, with one added check after the staleness rule: at the
    first print at or past ``t_entry + hold_ms`` the remainder is sold at the first print
    :data:`signal_audit.POLL_MS` after the deadline (a protection tick's delay), or the last print
    when none follows. ``tests/test_strategy_search.py`` pins that with a hold beyond the horizon
    this returns exactly what ``simulate_exit`` returns.
    """
    cost = sa.COST_PER_LEG
    pos, got, peak = 1.0, 0.0, entry_cost
    done: set[float] = set()
    breakeven = False
    t_entry = series[i0][0]
    last_t = t_entry
    i = i0 + 1
    while i < len(series):
        t, p = series[i]
        if t - t_entry > sa.HORIZON_MS:
            break
        if t - last_t > sa.STALE_MS:
            return (got + pos * series[i - 1][1] * (1 - cost)) / entry_cost - 1
        if t - t_entry >= hold_ms:
            j = sa._first_at(series, i, t_entry + hold_ms + sa.POLL_MS)
            px = series[j][1] if j < len(series) else series[-1][1]
            return (got + pos * px * (1 - cost)) / entry_cost - 1
        last_t = t
        peak = max(peak, p)
        for level, frac in ladder.tps:
            if level not in done and p >= entry_cost * level:
                done.add(level)
                sell = min(frac, pos)
                j = sa._first_at(series, i, t + sa.POLL_MS)
                px = series[j][1] if j < len(series) else p
                got += sell * px * (1 - cost)
                pos -= sell
                breakeven = breakeven or ladder.breakeven_after_tp1
        stop = entry_cost * (1.0 if breakeven else 1.0 - ladder.stop_frac)
        multiple = peak / entry_cost
        for level, trail in ladder.trailing:
            if multiple >= level:
                stop = max(stop, peak * (1 - trail))
                break
        if pos > 0 and p <= stop:
            j = sa._first_at(series, i, t + sa.POLL_MS)
            px = series[j][1] if j < len(series) else p
            return (got + pos * px * (1 - cost)) / entry_cost - 1
        i += 1
    last = series[min(i, len(series)) - 1][1]
    return (got + pos * last * (1 - cost)) / entry_cost - 1


def simulate(series: Sequence[tuple[int, float]], i0: int, entry_cost: float, policy: ExitPolicy) -> float:
    """Replayed return (3.25%/leg included, chain extra NOT yet) of ``policy`` on one path."""
    if policy.hold_ms is None:
        return sa.simulate_exit(series, i0, entry_cost, policy.ladder)
    return _simulate_with_hold(series, i0, entry_cost, policy.ladder, policy.hold_ms)


class PathCollector:
    """``run_audit``'s ``on_case`` hook: simulates every policy on each replayed path while the
    swap rows are in memory, so the tape is read once."""

    def __init__(self, policies: Iterable[ExitPolicy]) -> None:
        self.policies = {p.key: p for p in policies}
        self.raw: dict[tuple[str, str, str, int], dict[str, float]] = {}

    def __call__(self, case: sa.SignalCase, rows: list[tuple[Any, ...]]) -> None:
        path = sa.price_path(rows, case.t0)
        if path is None:
            return
        series, i = path
        entry = series[i][1] * (1 + sa.COST_PER_LEG)
        self.raw[(case.lane, case.chain, case.token, int(case.t0))] = {
            k: simulate(series, i, entry, p) for k, p in self.policies.items()
        }


# --------------------------------------------------------------------------------------
# observations and filters
# --------------------------------------------------------------------------------------


@dataclass
class Obs:
    """One replayed signal: its features and the RAW replayed return under each policy key."""

    lane: str
    chain: str
    token: str
    t0: int
    features: dict[str, Any]
    raw: dict[str, float]

    def net(self, key: str) -> float | None:
        r = self.raw.get(key)
        return None if r is None else sprint.net(r, self.chain)


def observations(cases: Iterable[sa.SignalCase], raw: Mapping[tuple[str, str, str, int], Mapping[str, float]],
                 *, settled_before_ms: int) -> list[Obs]:
    """Settled cases that the collector priced. Unsettled ones are dropped, not marked."""
    out = []
    for c in cases:
        r = raw.get((c.lane, c.chain, c.token, int(c.t0)))
        if r is None or int(c.t0) > settled_before_ms:
            continue
        out.append(Obs(c.lane, c.chain, c.token, int(c.t0), dict(c.features), dict(r)))
    return out


def _sig(x: float, digits: int = 3) -> float:
    """Round to significant digits, so a cut learned on slightly different data keeps its id."""
    if x == 0 or not math.isfinite(x):
        return x
    return round(x, -int(math.floor(math.log10(abs(x)))) + digits - 1)


@dataclass(frozen=True)
class Cond:
    """``lo <= feature < hi`` (either side open), or ``feature == eq`` for a category. An
    unmeasured feature (None) is in no band."""

    feature: str
    lo: float | None = None
    hi: float | None = None
    eq: str | None = None

    def test(self, feats: Mapping[str, Any]) -> bool:
        v = feats.get(self.feature)
        if self.eq is not None:
            return v is not None and str(v) == self.eq
        x = sa._num(v)
        return x is not None and (self.lo is None or x >= self.lo) and (self.hi is None or x < self.hi)

    def spec(self) -> dict[str, Any]:
        return {"f": self.feature, "lo": self.lo, "hi": self.hi, "eq": self.eq}

    @classmethod
    def from_spec(cls, d: Mapping[str, Any]) -> Cond:
        return cls(str(d["f"]), d.get("lo"), d.get("hi"), d.get("eq"))

    def label(self) -> str:
        if self.eq is not None:
            return f"{self.feature}={self.eq}"
        if self.lo is None:
            return f"{self.feature}<{self.hi:g}"
        if self.hi is None:
            return f"{self.feature}>={self.lo:g}"
        return f"{self.feature} {self.lo:g}..{self.hi:g}"


Filter = tuple[Cond, ...]


def _matches(filt: Filter, feats: Mapping[str, Any]) -> bool:
    return all(c.test(feats) for c in filt)


def single_conditions(train: Sequence[Obs], cfg: SearchConfig) -> list[Cond]:
    """Quantile bands of each feature, learned on ``train`` ONLY. Bands with fewer than
    ``min_train`` training signals are dropped."""
    numeric: dict[str, list[float]] = collections.defaultdict(list)
    categorical: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for o in train:
        for k, v in o.features.items():
            if k in EXCLUDED_FEATURES or v is None:
                continue
            if isinstance(v, str):
                categorical[k][v] += 1
                continue
            x = sa._num(v)
            if x is not None and math.isfinite(x):
                numeric[k].append(x)
    conds: list[Cond] = []
    seen: set[Cond] = set()
    for feat in sorted(numeric):
        xs = sorted(numeric[feat])
        if len(xs) < cfg.min_train:
            continue
        for q in cfg.quantiles:
            cuts = sorted({_sig(xs[int(k * len(xs) / q)]) for k in range(1, q)})
            edges: list[float | None] = [None, *cuts, None]
            for lo, hi in zip(edges[:-1], edges[1:], strict=True):
                c = Cond(feat, lo, hi)
                if c not in seen:
                    seen.add(c)
                    conds.append(c)
    for feat in sorted(categorical):
        for value, _n in categorical[feat].most_common(cfg.max_categories):
            conds.append(Cond(feat, eq=value))
    return [c for c in conds if sum(1 for o in train if c.test(o.features)) >= cfg.min_train]


# --------------------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------------------


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the regularized incomplete beta (modified Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c if abs(1.0 + aa / c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c if abs(1.0 + aa / c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-12:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x)
    front = math.exp(ln)
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1 - x) / b


def t_sf(t: float, df: int) -> float:
    """P(T > t) for Student's t with ``df`` degrees of freedom."""
    tail = 0.5 * _betai(df / 2.0, 0.5, df / (df + t * t))
    return tail if t > 0 else 1.0 - tail


def t_stat(xs: Sequence[float]) -> float | None:
    if len(xs) < 3:
        return None
    sd = statistics.stdev(xs)
    m = statistics.fmean(xs)
    if sd == 0:
        return math.inf if m > 0 else (-math.inf if m < 0 else 0.0)
    return m / (sd / math.sqrt(len(xs)))


def p_mean_positive(xs: Sequence[float]) -> float:
    """One-sided Student-t p-value for "mean > 0". 1.0 when it cannot be computed.

    A t-test, not a bootstrap: the correction needs p-values far below 1/2000, which a 2,000-
    resample bootstrap cannot resolve. Outcomes are capped at -100%..+300% before this, which
    bounds the tails the t-test is sensitive to; n >= 40 in anything that can survive.
    """
    t = t_stat(xs)
    if t is None:
        return 1.0
    if math.isinf(t):
        return 0.0 if t > 0 else 1.0
    return t_sf(t, len(xs) - 1)


def benjamini_hochberg(pvals: Sequence[float], q: float, *, method: str = "bh") -> list[bool]:
    """Step-up FDR control: reject the k smallest p with p_(k) <= k q / (m c). c = 1 for BH,
    sum(1/i) for Benjamini-Yekutieli."""
    m = len(pvals)
    if m == 0:
        return []
    c = sum(1.0 / i for i in range(1, m + 1)) if method == "by" else 1.0
    order = sorted(range(m), key=lambda i: pvals[i])
    k = 0
    for rank, idx in enumerate(order, 1):
        if pvals[idx] <= rank * q / (m * c):
            k = rank
    out = [False] * m
    for idx in order[:k]:
        out[idx] = True
    return out


# --------------------------------------------------------------------------------------
# the search
# --------------------------------------------------------------------------------------


@dataclass
class Candidate:
    lane: str
    chain: str
    filt: Filter
    policy: ExitPolicy
    train_n: int
    train_mean: float
    train_t: float
    split_ms: int = 0
    hold_n: int = 0
    hold_mean: float | None = None
    hold_p: float = 1.0
    hold_score: sprint.Score | None = None
    significant: bool = False

    def spec(self) -> dict[str, Any]:
        return {"lane": self.lane, "chain": self.chain, "filter": [c.spec() for c in self.filt],
                "exit": self.policy.spec()}

    @property
    def candidate_id(self) -> str:
        return candidate_id(self.spec())

    @property
    def label(self) -> str:
        return label_of(self.spec())

    @property
    def survivor(self) -> bool:
        return self.significant and self.hold_score is not None and self.hold_score.verdict == "PASS"

    def selection(self) -> dict[str, Any]:
        s = self.hold_score
        return {"train_n": self.train_n, "train_mean": self.train_mean, "train_t": self.train_t,
                "split_ms": self.split_ms, "hold_n": self.hold_n, "hold_mean": self.hold_mean,
                "hold_p": self.hold_p, "hold_verdict": s.verdict if s else None,
                "hold_lower": s.lower if s else None, "significant": self.significant}


def candidate_id(spec: Mapping[str, Any]) -> str:
    return "sc_" + hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]


def label_of(spec: Mapping[str, Any]) -> str:
    conds = [Cond.from_spec(c).label() for c in spec.get("filter") or []]
    return (f"{spec['lane']}/{spec['chain']} [{' & '.join(conds) or 'all signals'}] "
            f"exit={spec['exit'].get('name')}")


@dataclass
class SearchResult:
    tried: int = 0
    selected: list[Candidate] = field(default_factory=list)
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    stopped: str | None = None

    @property
    def survivors(self) -> list[Candidate]:
        return [c for c in self.selected if c.survivor]


def _rets(members: Sequence[Obs], key: str) -> list[float]:
    return [r for r in (o.net(key) for o in members) if r is not None]


def _train_candidates(lane: str, chain: str, filters: Sequence[Filter], train: Sequence[Obs],
                      policies: Sequence[ExitPolicy], cfg: SearchConfig) -> tuple[int, list[Candidate]]:
    """Score every (filter, policy) on the training data. Returns (tried, candidates)."""
    tried = 0
    out: list[Candidate] = []
    for filt in filters:
        members = [o for o in train if _matches(filt, o.features)]
        if len(members) < cfg.min_train:
            continue
        for pol in policies:
            xs = _rets(members, pol.key)
            if len(xs) < cfg.min_train:
                continue
            tried += 1
            t = t_stat(xs)
            out.append(Candidate(lane, chain, tuple(filt), pol, len(xs), statistics.fmean(xs),
                                 t if t is not None else 0.0))
    return tried, out


def _pairs(singles: Sequence[Candidate], train: Sequence[Obs], cfg: SearchConfig) -> list[Filter]:
    best: dict[Cond, float] = {}
    for c in singles:
        (cond,) = c.filt
        best[cond] = max(best.get(cond, -math.inf), c.train_t)
    top = [c for c, _t in sorted(best.items(), key=lambda kv: -kv[1])[: cfg.pair_top_k]]
    out: list[Filter] = []
    for i, a in enumerate(top):
        for b in top[i + 1:]:
            if a.feature == b.feature:
                continue
            pair = (a, b)
            if sum(1 for o in train if _matches(pair, o.features)) >= cfg.min_train:
                out.append(pair)
            if len(out) >= cfg.max_pairs:
                return out
    return out


def search(obs: Sequence[Obs], policies: Sequence[ExitPolicy], cfg: SearchConfig, *, end_ms: int,
           deadline_ms: int | None = None, clock: Callable[[], float] = time.time) -> SearchResult:
    """Walk-forward search over every lane x chain in ``obs``. Pure: no database."""
    res = SearchResult()
    groups: dict[tuple[str, str], list[Obs]] = collections.defaultdict(list)
    for o in obs:
        groups[(o.lane, o.chain)].append(o)
    holdouts: dict[tuple[str, str], tuple[int, list[Obs]]] = {}
    for (lane, chain), group in sorted(groups.items()):
        name = f"{lane}/{chain}"
        if deadline_ms is not None and clock() * 1000 > deadline_ms:
            res.stopped = "deadline"
            break
        group.sort(key=lambda o: o.t0)
        if len(group) < cfg.min_group:
            res.groups[name] = {"n": len(group), "skipped": f"fewer than {cfg.min_group} settled signals"}
            continue
        split_ms = group[int(len(group) * cfg.train_frac)].t0
        train = [o for o in group if o.t0 < split_ms]
        hold = [o for o in group if o.t0 >= split_ms]
        conds = single_conditions(train, cfg)
        t1, singles = _train_candidates(lane, chain, [(c,) for c in conds], train, policies, cfg)
        pairs = _pairs(singles, train, cfg)
        t0_, base = _train_candidates(lane, chain, [()], train, policies, cfg)
        t2, paired = _train_candidates(lane, chain, pairs, train, policies, cfg)
        tried = t1 + t0_ + t2
        pool = [c for c in (*base, *singles, *paired) if c.train_mean > 0]
        pool.sort(key=lambda c: -c.train_t)
        chosen = pool[: cfg.max_selected_per_group]
        for c in chosen:
            c.split_ms = split_ms
        res.tried += tried
        res.selected += chosen
        holdouts[(lane, chain)] = (split_ms, hold)
        res.groups[name] = {"n": len(group), "train": len(train), "holdout": len(hold),
                            "split_ms": split_ms, "bands": len(conds), "pairs": len(pairs),
                            "tried": tried, "selected": len(chosen)}
    for c in res.selected:
        split_ms, hold = holdouts[(c.lane, c.chain)]
        rows = [(o.t0, r) for o in hold if _matches(c.filt, o.features)
                for r in (o.net(c.policy.key),) if r is not None]
        xs = [r for _t, r in rows]
        c.hold_n = len(xs)
        c.hold_mean = statistics.fmean(xs) if xs else None
        c.hold_p = p_mean_positive(xs)
        c.hold_score = sprint.score(c.lane, c.chain, rows, split_ms, end_ms)
    flags = benjamini_hochberg([c.hold_p for c in res.selected], cfg.fdr_q, method=cfg.method)
    for c, ok in zip(res.selected, flags, strict=True):
        c.significant = ok
    return res


# --------------------------------------------------------------------------------------
# forward tracking
# --------------------------------------------------------------------------------------


@dataclass
class Tracked:
    candidate_id: str
    lane: str
    chain: str
    filt: Filter
    policy: ExitPolicy
    selected_ms: int
    label: str = ""

    @classmethod
    def from_row(cls, cid: str, spec_json: str, selected_ms: int, label: str = "") -> Tracked:
        spec = json.loads(spec_json)
        return cls(cid, spec["lane"], spec["chain"], tuple(Cond.from_spec(c) for c in spec["filter"]),
                   ExitPolicy.from_spec(spec["exit"]), int(selected_ms), label)


def forward_trades(tracked: Sequence[Tracked], obs: Sequence[Obs]) -> dict[str, list[tuple[str, int, float]]]:
    """(token, t0, net return) per candidate, for signals STRICTLY after its selection."""
    out: dict[str, list[tuple[str, int, float]]] = {}
    for tr in tracked:
        rows = out.setdefault(tr.candidate_id, [])
        for o in obs:
            if o.lane != tr.lane or o.chain != tr.chain or o.t0 <= tr.selected_ms:
                continue
            if not _matches(tr.filt, o.features):
                continue
            r = o.net(tr.policy.key)
            if r is not None:
                rows.append((o.token, o.t0, r))
    return out


@dataclass
class ForwardScore:
    candidate_id: str
    score: sprint.Score
    p: float
    verdict: str
    why: str


def forward_scores(tracked: Sequence[Tracked], rows: Mapping[str, Sequence[tuple[int, float]]],
                   now_ms: int, cfg: SearchConfig) -> list[ForwardScore]:
    """Each tracked candidate on its post-selection record, against the pass line, then BH across
    the tracked family: a pass-line PASS that does not survive the correction is UNCORRECTED."""
    scores = []
    for tr in tracked:
        r = list(rows.get(tr.candidate_id) or [])
        scores.append((tr, sprint.score(tr.lane, tr.chain, r, tr.selected_ms, now_ms),
                       p_mean_positive([x for _t, x in r])))
    flags = benjamini_hochberg([p for _tr, _s, p in scores], cfg.fdr_q, method=cfg.method)
    out = []
    for (tr, s, p), ok in zip(scores, flags, strict=True):
        verdict, why = s.verdict, s.why
        if s.verdict == "PASS" and not ok:
            verdict = "UNCORRECTED"
            why = f"pass line met, but p={p:.4f} does not survive {cfg.method.upper()} across {len(scores)} tracked"
        out.append(ForwardScore(tr.candidate_id, s, p, verdict, why))
    return out


# --------------------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------------------


def _utc_day(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def load_tracked(conn: sqlite3.Connection) -> list[Tracked]:
    return [Tracked.from_row(cid, spec, sel, label) for cid, spec, sel, label in conn.execute(
        "SELECT candidate_id, spec_json, selected_ms, label FROM strategy_candidates "
        "WHERE status = 'tracking' ORDER BY selected_ms")]


def record(conn: sqlite3.Connection, res: SearchResult, fwd: Mapping[str, Sequence[tuple[str, int, float]]],
           *, now_ms: int, cfg: SearchConfig, summary: Mapping[str, Any]) -> dict[str, Any]:
    """Write the run, new survivors, forward trades and forward scores in ONE short transaction.
    Writes ``strategy_*`` tables only."""
    run_id = f"search_{now_ms}"
    day = _utc_day(now_ms)
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO strategy_search_runs (run_id, ts_ms, version, tried, selected, survivors, summary_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_id, now_ms, SEARCH_VERSION, res.tried, len(res.selected), len(res.survivors),
             json.dumps(summary, default=str)))
        conn.executemany(
            "INSERT OR IGNORE INTO strategy_forward_trades (candidate_id, token, t0_ms, ret, recorded_ms) "
            "VALUES (?,?,?,?,?)",
            [(cid, tok, t0, r, now_ms) for cid, rows in fwd.items() for tok, t0, r in rows])
        tracked = load_tracked(conn)
        rows = {tr.candidate_id: [(int(t), float(r)) for t, r in conn.execute(
            "SELECT t0_ms, ret FROM strategy_forward_trades WHERE candidate_id = ?", (tr.candidate_id,))]
            for tr in tracked}
        scores = forward_scores(tracked, rows, now_ms, cfg)
        retired = _write_forward(conn, tracked, scores, day=day, now_ms=now_ms, cfg=cfg)
        added = _add_survivors(conn, res, run_id=run_id, now_ms=now_ms, cfg=cfg)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"run_id": run_id, "added": added, "retired": retired,
            "forward": {s.candidate_id: s.verdict for s in scores}}


def _write_forward(conn: sqlite3.Connection, tracked: Sequence[Tracked], scores: Sequence[ForwardScore],
                   *, day: str, now_ms: int, cfg: SearchConfig) -> list[str]:
    retired = []
    by_id = {t.candidate_id: t for t in tracked}
    for f in scores:
        s = f.score
        conn.execute(
            "INSERT OR REPLACE INTO strategy_forward_days (candidate_id, day, ts_ms, n, mean, mean_old, "
            "mean_new, lower, p, family, verdict, why) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f.candidate_id, day, now_ms, s.n, s.mean, s.mean_old, s.mean_new, s.lower, f.p,
             len(scores), f.verdict, f.why))
        # Calendar-consecutive PASS days ending today (2026-10-14: a missing day used to be
        # skipped over, so PASSes on Oct 1, 5 and 14 counted as three in a row).
        streak, expected = 0, day
        for d, v in conn.execute("SELECT day, verdict FROM strategy_forward_days WHERE candidate_id = ? "
                                 "ORDER BY day DESC", (f.candidate_id,)):
            if v != "PASS" or str(d) != expected:
                break
            streak += 1
            expected = (datetime.date.fromisoformat(expected) - datetime.timedelta(days=1)).isoformat()
        tr = by_id[f.candidate_id]
        age_d = (now_ms - tr.selected_ms) / DAY_MS
        retire = (f.verdict == "FAIL" and s.n >= cfg.retire_fail_n) or (
            age_d >= cfg.retire_after_days and f.verdict != "PASS")
        if retire:
            retired.append(f.candidate_id)
        conn.execute(
            "UPDATE strategy_candidates SET fwd_n=?, fwd_mean=?, fwd_lower=?, fwd_p=?, fwd_verdict=?, "
            "fwd_why=?, fwd_streak=?, fwd_updated_ms=?, status=? WHERE candidate_id=?",
            (s.n, s.mean, s.lower, f.p, f.verdict, f.why, streak, now_ms,
             "retired" if retire else "tracking", f.candidate_id))
    return retired


def _add_survivors(conn: sqlite3.Connection, res: SearchResult, *, run_id: str, now_ms: int,
                   cfg: SearchConfig) -> list[str]:
    """Persist new survivors, best holdout p first, within the per-run and tracked caps. An
    already-known candidate keeps its ORIGINAL selection time (INSERT OR IGNORE)."""
    (active,) = conn.execute("SELECT count(*) FROM strategy_candidates WHERE status = 'tracking'").fetchone()
    room = max(0, min(cfg.max_new_per_run, cfg.max_tracked - int(active)))
    added = []
    for c in sorted(res.survivors, key=lambda c: c.hold_p):
        if len(added) >= room:
            break
        cur = conn.execute(
            "INSERT OR IGNORE INTO strategy_candidates (candidate_id, lane, chain, label, spec_json, "
            "selected_ms, run_id, selection_json) VALUES (?,?,?,?,?,?,?,?)",
            (c.candidate_id, c.lane, c.chain, c.label, json.dumps(c.spec(), sort_keys=True), now_ms,
             run_id, json.dumps(c.selection(), default=str)))
        if cur.rowcount:
            added.append(c.candidate_id)
    return added


# --------------------------------------------------------------------------------------
# one run
# --------------------------------------------------------------------------------------


AuditFn = Callable[..., sa.AuditRun]


def run(conn: sqlite3.Connection, reader: sa.Reader, *, base_ladder: sa.ExitLadder, cfg: SearchConfig,
        lanes: Sequence[str], chains: Sequence[str], days: int, max_signals: int, now_ms: int,
        deadline_ms: int | None, clock: Callable[[], float] = time.time,
        audit: AuditFn | None = None, tape_cache: sa.TapeCache | None = None) -> dict[str, Any]:
    """Replay the window once, search it, score every tracked candidate forward, record."""
    audit = audit or sa.run_audit
    tracked = load_tracked(conn)
    policies = declared_policies(base_ladder)
    collector = PathCollector([*policies, *(t.policy for t in tracked)])
    audit_deadline = None if deadline_ms is None else deadline_ms - 120_000
    arun = audit(reader, ladder=base_ladder, lanes=lanes, chains=chains, days=days,
                 max_signals=max_signals, now_ms=now_ms, deadline_ms=audit_deadline, clock=clock,
                 on_case=collector, **({"tape_cache": tape_cache} if tape_cache is not None else {}))
    settled = now_ms - SETTLE_MS
    obs = observations(arun.cases, collector.raw, settled_before_ms=settled)
    res = search(obs, policies, cfg, end_ms=settled, deadline_ms=deadline_ms, clock=clock)
    fwd = forward_trades(tracked, obs)
    summary = {
        "version": SEARCH_VERSION, "config": dataclasses.asdict(cfg), "days": days,
        "considered": arun.considered, "replayed": len(arun.cases), "settled": len(obs),
        "audit_stopped": arun.stopped, "search_stopped": res.stopped,
        "policies": [p.spec() for p in policies], "groups": res.groups,
        "tried": res.tried, "selected": len(res.selected),
        "survivors": [{"id": c.candidate_id, "label": c.label, **c.selection()} for c in res.survivors],
        "excluded_features": EXCLUDED_FEATURES,
    }
    written = record(conn, res, fwd, now_ms=now_ms, cfg=cfg, summary=summary)
    return {"run_id": written["run_id"], "tried": res.tried, "selected": len(res.selected),
            "survivors": len(res.survivors), "added": len(written["added"]),
            "retired": len(written["retired"]), "tracked": len(tracked),
            "forward_pass": sum(1 for v in written["forward"].values() if v == "PASS"),
            "settled": len(obs), "audit_stopped": arun.stopped, "search_stopped": res.stopped}


# --------------------------------------------------------------------------------------
# for the owner's report
# --------------------------------------------------------------------------------------


def report_view(conn: sqlite3.Connection, *, limit: int = 5) -> dict[str, Any] | None:
    """The latest run's counts and the tracked candidates with the most forward trades.
    ``None`` before the first run (or before migration 047)."""
    try:
        row = conn.execute("SELECT run_id, ts_ms, tried, selected, survivors, summary_json "
                           "FROM strategy_search_runs ORDER BY ts_ms DESC LIMIT 1").fetchone()
        if row is None:
            return None
        cands = conn.execute(
            "SELECT candidate_id, label, selected_ms, fwd_n, fwd_mean, fwd_lower, fwd_verdict, fwd_streak "
            "FROM strategy_candidates WHERE status = 'tracking' "
            "ORDER BY fwd_streak DESC, fwd_n DESC, selected_ms LIMIT ?", (int(limit),)).fetchall()
        (active,) = conn.execute("SELECT count(*) FROM strategy_candidates WHERE status = 'tracking'").fetchone()
    except sqlite3.OperationalError:
        return None
    try:
        summary = json.loads(row[5])
    except (TypeError, ValueError):
        summary = {}
    keys = ("candidate_id", "label", "selected_ms", "fwd_n", "fwd_mean", "fwd_lower", "fwd_verdict", "fwd_streak")
    return {"run_id": row[0], "ts_ms": int(row[1]), "tried": int(row[2]), "selected": int(row[3]),
            "survivors": int(row[4]), "tracked": int(active),
            "stopped": summary.get("audit_stopped") or summary.get("search_stopped"),
            "method": (summary.get("config") or {}).get("method", "bh"),
            "fdr_q": (summary.get("config") or {}).get("fdr_q"),
            "candidates": [dict(zip(keys, tuple(c), strict=True)) for c in cands]}


__all__ = ["SearchConfig", "ExitPolicy", "declared_policies", "simulate", "PathCollector", "Obs",
           "observations", "Cond", "single_conditions", "t_sf", "p_mean_positive", "benjamini_hochberg",
           "Candidate", "SearchResult", "search", "Tracked", "forward_trades", "forward_scores",
           "load_tracked", "record", "run", "report_view", "EXCLUDED_FEATURES", "SETTLE_MS"]
