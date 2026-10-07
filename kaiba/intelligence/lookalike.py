"""Lookalike wallets: find wallets that trade like the ones that make money, then ask the only
question that matters -- would COPYING them have paid, out of sample, at our lag and our cost?

The owner's ask (2026-10-04): "find more wallets like these" (his robinhood copy leaders) and
"gather lots of solana wallets A and B tier so solana can actually make money". Every earlier
attempt at "more good wallets" measured the wrong thing first and paid for it:

* tape grades do not predict on sol (B wallets bought into the same -40% medians as C/D);
* a vendor's A/B list scored unrealized PnL and did not reproduce under our rubric;
* copying the top copy-profit cohort lost at 60 s and 15 s and only turned positive near 5 s;
* the "proven" job's first sol pass was a dust network co-timing its own buys.

So "like these" means "like the ones that actually make money", judged by evidence, and a
wallet only counts when its edge survives (1) a time split -- chosen on the earlier part,
measured on the later part only -- and (2) being copied at a realistic lag, net of realistic
cost. Nothing here writes anywhere: the database is opened ``mode=ro`` and the lists go to
stdout or a file the caller names. Cohort or grade writes are the lead's, after review.

Method
------
1. **Co-buy discovery** (:func:`cobuy_scan`, :func:`pick_candidates`). For every seed's FIRST
   buy of a token in the EARLIER period, every other wallet's first buy of that token inside
   ``[-cobuy_before_ms, +cobuy_near_ms]`` is classified against the seed's buy:

   * ``before``  -- landed before the seed (same block counts when it is earlier in the block);
   * ``copier``  -- landed AFTER the seed by at most ``copier_ms`` (0.6 s: GMGN copy bots land
     0.2-0.4 s behind their leader, MEASURED 2026-10-03 on the owner's 0x7243);
   * ``near``    -- after the seed by more than ``copier_ms`` but within ``cobuy_near_ms``.

   A wallet whose co-buys are mostly ``copier`` is a follower and is excluded. A candidate
   needs ``before``/``near`` co-buys on at least ``min_cobuy_tokens`` DISTINCT tokens.
   Routers, contracts, exchanges and the seeds themselves are excluded (EVM: pass-through
   netting inside each transaction, ``eth_getCode``, an activity cap).
2. **Copied return** (:func:`copy_trip`). Their round trip is an episode -- open on the first
   buy, close on the first sell after it (:func:`episodes`), which is what a copier trades.
   Our entry is the market at ``buy + lag``; our exit the market at ``sell + lag``, or a 24 h
   time stop (``max_hold_ms``), or the period end (marked). On a CONTINUOUS tape (robinhood:
   every Pons curve trade, every Uniswap v4 swap) the price at ``t`` is the pool's STATE, the
   last print at or before ``t``; on a sparse tape (sol) it is the NEXT print within
   ``max_wait_ms`` (``copytrade.next_print`` parity) with the exit falling back to the last
   print (``proven._price_trips`` ``last_print`` rule), so a dump into a dead book is never
   silently dropped. ``net = exit/entry * (1-fee)^2 * (1-slip)/(1+slip) - 1``.
3. **Out of sample** (:func:`score_in`, :func:`score_out`). One split instant. A trip belongs
   to the period its BUY is in; earlier-period trips are priced with ``until = split`` so no
   print after the split is ever read for selection (a trip still open at the split is marked
   there). Selection = earlier-period copied mean at ``grade_lag_ms`` > 0 with at least
   ``min_trips_in`` priced trips.
4. **Grades, on the LATER period only** (:func:`grade`): ``A`` when the 5 s copied mean has a
   90% bootstrap interval wholly above zero with n >= 10; ``B`` when the 5 s AND the 1 s
   point estimates are positive with n >= 10; otherwise ungraded. Only SELECTED wallets are
   graded, so a grade means "chosen on the past, confirmed on the future". The owner's seeds
   are re-scored the same way -- they are not exempt.
5. **Baseline** (:func:`summarize_group`). Random wallets from the same discovery pool (they
   bought the same tokens around the same time, but fewer than ``min_cobuy_tokens`` early),
   activity-matched to the selected candidates and put through the SAME selection and
   grading. Their A/B rate is the chance rate: a list of A wallets is only news above it.

Costs (``fee_per_leg``, ``slip_per_leg``)
------------------------------------------
GMGN takes 1% per leg (``copytrade.ROUND_TRIP_FEE_BPS``). ``slip_per_leg`` defaults to 1.5%:
the venue's own fee is 1% per leg on the robinhood venues a copy lands on (Pons curve
``feeBps=100``; graduated Uniswap v4 pools initialise with ``fee=10000`` ppm) and is NOT inside
a v4 ``sqrtPriceX96`` state price; plus ~0.4% impact for a ~$100 order and ~0.1% gas (robinhood
flat cost MEASURED $0.1175/leg). Round trip ~= 5%. ``--slip 0`` gives the fee-only upper bound.

Run (read-only; robinhood also reads RPC through the kaiba limiter)::

    python -m kaiba.intelligence.lookalike --db data/kaiba.db --chain robinhood \\
        --seeds 0xabc...,0xdef... --cache /dev/shm/lookalike/cache --json out.json
    python -m kaiba.intelligence.lookalike --db data/kaiba.db --chain sol --json out.json
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import random
import sqlite3
import statistics
import sys
import time
import zlib
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Config",
    "CoBuy",
    "Leg",
    "PriceTape",
    "Trip",
    "WalletScore",
    "buyers_from_token_logs",
    "cluster_candidates",
    "cobuy_scan",
    "cotime_clusters",
    "copy_trip",
    "episodes",
    "grade",
    "legs_from_transfers",
    "net_return",
    "pick_candidates",
    "score_in",
    "score_out",
    "score_wallet",
    "summarize",
    "summarize_group",
    "token_spans",
    "trip_reads",
    "v4_index",
    "v4_prints",
]

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
ZERO = "0x" + "0" * 40
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
V4_SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
V4_INIT = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
#: Uniswap v4 PoolManager on robinhood (``kaiba.ingest.robinhood.POOL_MANAGER``).
RH_POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
#: MEASURED 2026-10-04 on the box: (last - first slot) / (last - first ts) over the newest
#: 1.5M swaps = 101.52 ms; over the oldest = 100.61 ms. Only RELATIVE block distances feed a
#: lag, so the 1% drift is immaterial.
BLOCK_MS: dict[str, float] = {"robinhood": 101.5}


@dataclass(frozen=True)
class Config:
    """Every threshold, recorded verbatim in the output."""

    lags_ms: tuple[int, ...] = (1_000, 5_000, 15_000)
    grade_lag_ms: int = 5_000
    fast_lag_ms: int = 1_000
    fee_per_leg: float = 0.01
    slip_per_leg: float = 0.015
    #: Grades need this many LATER-period copied trips (the owner's rule).
    min_n: int = 10
    #: Selection needs this many EARLIER-period copied trips at ``grade_lag_ms``.
    min_trips_in: int = 5
    ci_level: float = 0.90
    boot_draws: int = 1000
    #: Copy exits on the leader's first sell or after this long, whichever is first.
    max_hold_ms: int = DAY_MS
    #: Sparse tapes only: our fill is the first print at most this late.
    max_wait_ms: int = 120_000
    cobuy_before_ms: int = 60_000
    cobuy_near_ms: int = 3_000
    copier_ms: int = 600
    copier_share: float = 0.5
    min_cobuy_tokens: int = 3
    #: A co-buy this close to the seed's, before it, is a sibling address (one operator).
    sibling_ms: int = 150
    #: Co-timing that makes two candidates one operator (``cotime_clusters``).
    cluster_ms: int = 300
    #: More legs than this in the look-back is a bot, router or exchange, not a copy leader.
    max_legs: int = 5_000
    #: Statistics cap one trip at +500% (``proven.ProvenConfig.net_cap``): a single print on a
    #: pool nobody could sell into would otherwise certify a wallet by itself. MEASURED
    #: 2026-10-04: uncapped, robinhood pooled means read +25,000,000% on medians of -4.9%.
    net_cap: float = 5.0
    seed: int = 20_261_004

    def as_dict(self) -> dict[str, Any]:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.__dict__.items()}


# --------------------------------------------------------------------------------------
# data shapes
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Leg:
    """One side of one wallet's trade. ``t_ms`` is the chain clock; ``seq`` orders a block."""

    wallet: str
    token: str
    side: str  # "buy" | "sell"
    t_ms: float
    seq: int = 0
    ref: str = ""

    @property
    def key(self) -> tuple[float, int]:
        return (self.t_ms, self.seq)


@dataclass(frozen=True)
class Trip:
    wallet: str
    token: str
    buy_ms: float
    sell_ms: float | None


def episodes(legs: Iterable[Leg]) -> list[Trip]:
    """``copytrade.round_trips`` shape: open on a buy with nothing open, close on the first
    sell after it. Extra buys while open are ignored -- a copier entered on the first one.
    An episode still open at the end has ``sell_ms=None``."""
    by: dict[tuple[str, str], list[Leg]] = defaultdict(list)
    for leg in legs:
        by[(leg.wallet, leg.token)].append(leg)
    out: list[Trip] = []
    for (wallet, token), items in by.items():
        items.sort(key=lambda x: x.key)
        open_ms: float | None = None
        for leg in items:
            if leg.side == "buy":
                if open_ms is None:
                    open_ms = leg.t_ms
            elif leg.side == "sell" and open_ms is not None:
                out.append(Trip(wallet, token, open_ms, leg.t_ms))
                open_ms = None
        if open_ms is not None:
            out.append(Trip(wallet, token, open_ms, None))
    out.sort(key=lambda t: (t.buy_ms, t.wallet, t.token))
    return out


def first_buys(legs: Iterable[Leg]) -> dict[str, Leg]:
    """``{token: earliest buy leg}``."""
    out: dict[str, Leg] = {}
    for leg in legs:
        if leg.side == "buy" and (leg.token not in out or leg.key < out[leg.token].key):
            out[leg.token] = leg
    return out


def token_spans(
    legs: Iterable[Leg], *, pre_ms: float, post_ms: float, lo_ms: float, hi_ms: float
) -> dict[str, list[tuple[float, float]]]:
    """Merged ``[t - pre, t + post]`` intervals per token, clipped to ``[lo, hi]``: the prints
    a copy of these legs can ever read."""
    raw: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for leg in legs:
        a, b = max(lo_ms, leg.t_ms - pre_ms), min(hi_ms, leg.t_ms + post_ms)
        if a <= b:
            raw[leg.token].append((a, b))
    out: dict[str, list[tuple[float, float]]] = {}
    for token, ivs in raw.items():
        ivs.sort()
        merged = [list(ivs[0])]
        for a, b in ivs[1:]:
            if a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        out[token] = [(a, b) for a, b in merged]
    return out


# --------------------------------------------------------------------------------------
# prices
# --------------------------------------------------------------------------------------


class PriceTape:
    """``{token: [(t_ms, seq, price[, gap_prone])]}`` with one pricing rule per token.

    * continuous tokens: the STATE at ``t`` -- the last print at or before ``t``. A pool or a
      curve still quotes after its last trade, at the price that trade left. A gap-prone
      print (from a poller with outages) does not carry into a known outage window.
    * sparse tokens: the NEXT print in ``[t, t + max_wait_ms]``.

    Never reads a print later than the caller's ``until_ms``: that bound is what keeps an
    earlier-period number from seeing the later period.
    """

    def __init__(
        self,
        prints: Mapping[str, Sequence[Sequence[Any]]],
        *,
        continuous: Iterable[str] | bool = (),
        max_wait_ms: int = 120_000,
        dark: Sequence[tuple[float, float]] = (),
    ) -> None:
        self._t: dict[str, list[tuple[float, int]]] = {}
        self._p: dict[str, list[float]] = {}
        self._g: dict[str, list[bool]] = {}
        for token, rows in prints.items():
            clean = sorted(
                (float(r[0]), int(r[1]), float(r[2]), bool(r[3]) if len(r) > 3 else False)
                for r in rows
                if r[2] is not None and math.isfinite(float(r[2])) and float(r[2]) > 0
            )
            if clean:
                self._t[token] = [(r[0], r[1]) for r in clean]
                self._p[token] = [r[2] for r in clean]
                self._g[token] = [r[3] for r in clean]
        self._all_cont = continuous is True
        self._cont = set() if isinstance(continuous, bool) else set(continuous)
        self.max_wait_ms = int(max_wait_ms)
        self.dark = sorted((float(a), float(b)) for a, b in dark)

    def tokens(self) -> set[str]:
        return set(self._t)

    def is_continuous(self, token: str) -> bool:
        return self._all_cont or token in self._cont

    def _in_dark(self, last_t: float, t: float) -> bool:
        return any(a <= t <= b and last_t < a for a, b in self.dark)

    def price_at(self, token: str, t_ms: float, *, until_ms: float) -> tuple[float, float] | None:
        """``(price, print time)`` per the token's rule, or ``None``."""
        if t_ms > until_ms or token not in self._t:
            return None
        keys = self._t[token]
        if self.is_continuous(token):
            i = bisect.bisect_right(keys, (t_ms, math.inf)) - 1
            if i < 0:
                return None
            if self._g[token][i] and self._in_dark(keys[i][0], t_ms):
                return None
            return self._p[token][i], keys[i][0]
        i = bisect.bisect_left(keys, (t_ms, -math.inf))
        if i >= len(keys):
            return None
        pt = keys[i][0]
        if pt > min(t_ms + self.max_wait_ms, until_ms):
            return None
        return self._p[token][i], pt

    def last_at(self, token: str, t_ms: float, *, since_ms: float) -> tuple[float, float] | None:
        """The newest print in ``[since_ms, t_ms]``: the book as it stood (any tape)."""
        if token not in self._t:
            return None
        keys = self._t[token]
        i = bisect.bisect_right(keys, (t_ms, math.inf)) - 1
        if i < 0 or keys[i][0] < since_ms:
            return None
        return self._p[token][i], keys[i][0]


def merge_tapes(*tapes: Mapping[str, Sequence[Sequence[Any]]]) -> dict[str, list[Sequence[Any]]]:
    """Union of print maps, deduplicated on ``(t, seq)`` per token."""
    out: dict[str, dict[tuple[float, int], Sequence[Any]]] = defaultdict(dict)
    for tape in tapes:
        for token, rows in tape.items():
            for r in rows:
                out[token][(float(r[0]), int(r[1]))] = r
    return {t: list(v.values()) for t, v in out.items()}


def net_return(entry: float, exit_: float, *, fee: float, slip: float) -> float:
    """Copied round trip, net: we pay ``entry*(1+slip)``, receive ``exit*(1-slip)``, and the
    venue fee comes off each leg."""
    return (exit_ / entry) * (1.0 - fee) ** 2 * (1.0 - slip) / (1.0 + slip) - 1.0


def copy_trip(
    trip: Trip,
    tape: PriceTape,
    lag_ms: int,
    *,
    until_ms: float,
    fee: float,
    slip: float,
    max_hold_ms: int | None = DAY_MS,
) -> tuple[float | None, str]:
    """Net copied return of one trip at ``lag_ms``, and how it was priced.

    ``None`` only when we could never have BOUGHT (no entry price): an uncopyable trade is not
    a break-even one. Once bought, a trip always gets an exit: the leader's sell + lag, the
    time stop, or the period end (marked) -- falling back to the last print when the tape is
    silent there, and to flat when nothing printed after our entry at all.
    """
    entry_t = trip.buy_ms + lag_ms
    if entry_t > until_ms:
        return None, "entry_after_period"
    entry = tape.price_at(trip.token, entry_t, until_ms=until_ms)
    if entry is None:
        return None, "no_entry_price"
    due: list[tuple[float, str]] = []
    if trip.sell_ms is not None:
        due.append((trip.sell_ms + lag_ms, "leader_sell"))
    if max_hold_ms:
        due.append((trip.buy_ms + lag_ms + max_hold_ms, "time_stop"))
    exit_t, how = min(due) if due else (until_ms, "marked")
    if exit_t > until_ms:
        exit_t, how = until_ms, "marked"
    exit_t = max(exit_t, entry[1])
    exit_ = tape.price_at(trip.token, exit_t, until_ms=until_ms) if how != "marked" else None
    if exit_ is None:
        exit_ = tape.last_at(trip.token, exit_t, since_ms=entry[1])
        if how != "marked":
            how += "_last_print"
    if exit_ is None:
        exit_, how = entry, how + "_flat"
    return net_return(entry[0], exit_[0], fee=fee, slip=slip), how


# --------------------------------------------------------------------------------------
# statistics and scoring
# --------------------------------------------------------------------------------------


def summarize(
    values: Sequence[float], *, level: float = 0.90, draws: int = 1000, seed: int = 0, ci: bool = True
) -> dict[str, Any]:
    """n, mean, median, win rate and a two-sided percentile-bootstrap interval of the mean."""
    vals = [float(v) for v in values]
    n = len(vals)
    out: dict[str, Any] = {"n": n, "mean": None, "median": None, "win": None, "ci": None}
    if not n:
        return out
    out["mean"] = math.fsum(vals) / n
    out["median"] = statistics.median(vals)
    out["win"] = sum(1 for v in vals if v > 0) / n
    if ci and n >= 3:
        rng = random.Random(seed)
        means = sorted(math.fsum(rng.choices(vals, k=n)) / n for _ in range(max(200, draws)))
        a = (1.0 - level) / 2.0
        out["ci"] = (means[int(a * len(means))], means[min(len(means) - 1, int((1.0 - a) * len(means)))])
    return out


@dataclass
class WalletScore:
    wallet: str
    role: str
    legs: int = 0
    trips_in: int = 0
    trips_out: int = 0
    ins: dict[int, dict[str, Any]] = field(default_factory=dict)
    outs: dict[int, dict[str, Any]] = field(default_factory=dict)
    nets_out: dict[int, list[float]] = field(default_factory=dict, repr=False)
    #: exit-rule sensitivity (``EXIT_RULES`` other than ``state``), later period only
    outs_alt: dict[str, dict[int, dict[str, Any]]] = field(default_factory=dict)
    nets_out_alt: dict[str, dict[int, list[float]]] = field(default_factory=dict, repr=False)
    grade_alt: dict[str, str | None] = field(default_factory=dict)
    reasons_in: Counter = field(default_factory=Counter)
    reasons_out: Counter = field(default_factory=Counter)
    selected: bool = False
    grade: str | None = None
    notes: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        def fmt(s: Mapping[int, Mapping[str, Any]]) -> dict[str, Any]:
            res = {}
            for lag, st in sorted(s.items()):
                res[f"{lag // 1000}s"] = {
                    "n": st["n"],
                    "mean_pct": _pct(st["mean"]),
                    "median_pct": _pct(st["median"]),
                    "win_pct": _pct(st["win"]),
                    "ci90_pct": None if st["ci"] is None else [_pct(st["ci"][0]), _pct(st["ci"][1])],
                }
            return res

        return {
            "wallet": self.wallet,
            "role": self.role,
            "legs": self.legs,
            "trips_in": self.trips_in,
            "trips_out": self.trips_out,
            "selected": self.selected,
            "grade": self.grade,
            "in_sample": fmt(self.ins),
            "out_of_sample": fmt(self.outs),
            "exit_rule_sensitivity": {r: {"grade": self.grade_alt.get(r), **fmt(o)} for r, o in self.outs_alt.items()},
            "how_in": dict(self.reasons_in),
            "how_out": dict(self.reasons_out),
            **({"notes": self.notes} if self.notes else {}),
        }


def _pct(x: float | None) -> float | None:
    return None if x is None else round(100.0 * x, 2)


def _wallet_seed(cfg: Config, wallet: str) -> int:
    return cfg.seed ^ int(hashlib.sha1(wallet.encode()).hexdigest()[:8], 16)


#: Exits a tape actually showed: the market after the leader's sell, or at the time stop.
#: Everything else (``*_last_print``, ``marked``, ``*_flat``) prices the exit off an older
#: print -- the state on a continuous tape, but on a sparse one only the moment somebody
#: happened to be looking.
OBSERVED_EXITS: frozenset[str] = frozenset({"leader_sell", "time_stop"})

#: Exit-pricing sensitivity, from the SAME pass: ``state`` (default: the last print stands),
#: ``observed`` (unobserved exits dropped -- optimistic, the dead tokens go missing) and
#: ``loss`` (unobserved exits are -100% -- pessimistic). On robinhood's continuous tape the
#: three agree up to the dark gaps; on sol they bound the answer.
EXIT_RULES: tuple[str, ...] = ("state", "observed", "loss")


def _priced_all(trips: Sequence[Trip], tape: PriceTape, lag: int, until: float, cfg: Config,
                reasons: Counter | None) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {r: [] for r in EXIT_RULES}
    for t in trips:
        net, how = copy_trip(t, tape, lag, until_ms=until, fee=cfg.fee_per_leg, slip=cfg.slip_per_leg,
                             max_hold_ms=cfg.max_hold_ms)
        if reasons is not None:
            reasons[how] += 1
        if net is None:
            continue
        if cfg.net_cap is not None and net > cfg.net_cap:
            if reasons is not None:
                reasons["capped"] += 1
            net = cfg.net_cap
        out["state"].append(net)
        if how in OBSERVED_EXITS:
            out["observed"].append(net)
            out["loss"].append(net)
        else:
            out["loss"].append(-1.0)
    return out


def _priced(trips: Sequence[Trip], tape: PriceTape, lag: int, until: float, cfg: Config, reasons: Counter | None) -> list[float]:
    return _priced_all(trips, tape, lag, until, cfg, reasons)["state"]


def score_in(
    wallet: str, legs: Sequence[Leg], tape: PriceTape, *, lo_ms: float, split_ms: float, cfg: Config,
    role: str = "candidate",
) -> WalletScore:
    """EARLIER period only, every print bounded by ``until = split``. Sets ``selected``."""
    ws = WalletScore(wallet=wallet, role=role, legs=len(legs))
    tin = [t for t in episodes(legs) if lo_ms <= t.buy_ms < split_ms]
    ws.trips_in = len(tin)
    for lag in cfg.lags_ms:
        nets = _priced(tin, tape, lag, split_ms, cfg, ws.reasons_in if lag == cfg.grade_lag_ms else None)
        ws.ins[lag] = summarize(nets, ci=False)
    g = ws.ins[cfg.grade_lag_ms]
    ws.selected = bool(g["n"] >= cfg.min_trips_in and (g["mean"] or 0.0) > 0)
    return ws


def score_out(ws: WalletScore, legs: Sequence[Leg], tape: PriceTape, *, split_ms: float, hi_ms: float, cfg: Config) -> WalletScore:
    """LATER period: trips whose buy is at or after the split, priced up to ``hi_ms``; grades."""
    tout = [t for t in episodes(legs) if split_ms <= t.buy_ms < hi_ms]
    ws.trips_out = len(tout)
    seed = _wallet_seed(cfg, ws.wallet)
    alt: dict[str, dict[int, dict[str, Any]]] = {"observed": {}, "loss": {}}
    for lag in cfg.lags_ms:
        sets = _priced_all(tout, tape, lag, hi_ms, cfg, ws.reasons_out if lag == cfg.grade_lag_ms else None)
        ws.nets_out[lag] = sets["state"]
        ws.outs[lag] = summarize(sets["state"], level=cfg.ci_level, draws=cfg.boot_draws, seed=seed)
        for rule in alt:
            ws.nets_out_alt.setdefault(rule, {})[lag] = sets[rule]
            alt[rule][lag] = summarize(sets[rule], level=cfg.ci_level, draws=cfg.boot_draws, seed=seed)
    gradeable = ws.selected or ws.role == "seed"
    ws.grade = grade(ws.outs, cfg) if gradeable else None
    for rule, outs in alt.items():
        ws.outs_alt[rule] = outs
        ws.grade_alt[rule] = grade(outs, cfg) if gradeable else None
    return ws


def score_wallet(
    wallet: str, legs: Sequence[Leg], tape: PriceTape, *, lo_ms: float, split_ms: float, hi_ms: float,
    cfg: Config, role: str = "candidate",
) -> WalletScore:
    ws = score_in(wallet, legs, tape, lo_ms=lo_ms, split_ms=split_ms, cfg=cfg, role=role)
    return score_out(ws, legs, tape, split_ms=split_ms, hi_ms=hi_ms, cfg=cfg)


def grade(outs: Mapping[int, Mapping[str, Any]], cfg: Config) -> str | None:
    """The owner's rule, on the LATER period only."""
    g, f = outs.get(cfg.grade_lag_ms), outs.get(cfg.fast_lag_ms)
    if not g or g["n"] < cfg.min_n or g["mean"] is None:
        return None
    if g["ci"] is not None and g["ci"][0] > 0:
        return "A"
    if g["mean"] > 0 and f and f["n"] >= cfg.min_n and (f["mean"] or 0.0) > 0:
        return "B"
    return None


def summarize_group(scores: Sequence[WalletScore], cfg: Config, rule: str = "state") -> dict[str, Any]:
    """Pooled later-period copied trips of a group's SELECTED wallets, per lag, plus grade rates,
    under one exit rule (``EXIT_RULES``)."""
    sel = [s for s in scores if s.selected]
    out: dict[str, Any] = {"wallets": len(scores), "selected": len(sel), "exit_rule": rule}

    def nets(s: WalletScore, lag: int) -> list[float]:
        return s.nets_out.get(lag, []) if rule == "state" else s.nets_out_alt.get(rule, {}).get(lag, [])

    def grade_of(s: WalletScore) -> str | None:
        return s.grade if rule == "state" else s.grade_alt.get(rule)

    def n_of(s: WalletScore) -> int:
        o = s.outs if rule == "state" else s.outs_alt.get(rule, {})
        return (o.get(cfg.grade_lag_ms) or {}).get("n", 0)

    for lag in cfg.lags_ms:
        pooled = [x for s in sel for x in nets(s, lag)]
        st = summarize(pooled, level=cfg.ci_level, draws=cfg.boot_draws, seed=cfg.seed)
        out[f"pooled_{lag // 1000}s"] = {
            "trips": st["n"], "mean_pct": _pct(st["mean"]), "median_pct": _pct(st["median"]),
            "win_pct": _pct(st["win"]),
            "ci90_pct": None if st["ci"] is None else [_pct(st["ci"][0]), _pct(st["ci"][1])],
        }
    gradeable = [s for s in sel if n_of(s) >= cfg.min_n]
    out["gradeable"] = len(gradeable)
    out["A"] = sum(1 for s in sel if grade_of(s) == "A")
    out["B"] = sum(1 for s in sel if grade_of(s) == "B")
    out["ab_rate_of_gradeable"] = round((out["A"] + out["B"]) / len(gradeable), 4) if gradeable else None
    out["a_rate_of_gradeable"] = round(out["A"] / len(gradeable), 4) if gradeable else None
    return out


# --------------------------------------------------------------------------------------
# co-buy discovery
# --------------------------------------------------------------------------------------


@dataclass
class CoBuy:
    wallet: str
    before: int = 0
    near: int = 0
    copier: int = 0
    sibling: int = 0
    tokens_early: set[str] = field(default_factory=set)
    tokens_all: set[str] = field(default_factory=set)
    seeds: set[str] = field(default_factory=set)
    offsets_ms: list[float] = field(default_factory=list)

    @property
    def pairs(self) -> int:
        return self.before + self.near + self.copier

    @property
    def copier_share(self) -> float:
        return self.copier / self.pairs if self.pairs else 0.0

    def as_dict(self) -> dict[str, Any]:
        offs = sorted(self.offsets_ms)
        return {
            "early_tokens": len(self.tokens_early),
            "before": self.before,
            "near": self.near,
            "copier": self.copier,
            "copier_share": round(self.copier_share, 3),
            "seed_sibling_share": round(self.sibling / self.pairs, 3) if self.pairs else 0.0,
            "seeds": len(self.seeds),
            "median_offset_s": round(offs[len(offs) // 2] / 1000.0, 2) if offs else None,
        }


def cobuy_scan(
    seed_first_buys: Sequence[Leg],
    buyers_by_token: Mapping[str, Sequence[Leg]],
    cfg: Config,
    *,
    exclude: Iterable[str] = (),
) -> dict[str, CoBuy]:
    """Classify every other wallet's first buy around each seed first buy (module docstring).

    ``buyers_by_token``: buy legs of ANY wallet on each token. Per (wallet, seed, token) only
    that wallet's earliest buy inside the window counts, so buying twice is not two co-buys.
    """
    skip = {w.lower() for w in exclude} | {sb.wallet.lower() for sb in seed_first_buys}
    out: dict[str, CoBuy] = {}
    for sb in seed_first_buys:
        lo, hi = sb.t_ms - cfg.cobuy_before_ms, sb.t_ms + cfg.cobuy_near_ms
        firsts: dict[str, Leg] = {}
        for leg in buyers_by_token.get(sb.token, ()):
            if leg.side != "buy" or leg.wallet.lower() in skip or not lo <= leg.t_ms <= hi:
                continue
            if leg.wallet not in firsts or leg.key < firsts[leg.wallet].key:
                firsts[leg.wallet] = leg
        for wallet, leg in firsts.items():
            cb = out.setdefault(wallet, CoBuy(wallet=wallet))
            d = leg.t_ms - sb.t_ms
            cb.offsets_ms.append(d)
            cb.seeds.add(sb.wallet)
            cb.tokens_all.add(sb.token)
            if leg.key < sb.key:
                cb.before += 1
                cb.tokens_early.add(sb.token)
                if -d <= cfg.sibling_ms:
                    cb.sibling += 1
            elif d <= cfg.copier_ms:
                cb.copier += 1
            else:
                cb.near += 1
                cb.tokens_early.add(sb.token)
    return out


def cotime_clusters(
    buys: Mapping[str, Sequence[tuple[str, float]]], *, within_ms: float, min_events: int = 3, min_share: float = 0.5,
) -> dict[str, str]:
    """``{wallet: cluster id}`` -- one operator behind several addresses.

    Two wallets join when they bought the same token within ``within_ms`` of each other on
    at least ``min_events`` tokens AND on at least ``min_share`` of the smaller wallet's
    tokens (two unrelated launch snipers share a few blocks, not most of their book). The
    id is the cluster's smallest address. Same shape as ``proven.cotime_clusters``, plus the
    share condition.
    """
    parent = {w: w for w in buys}

    def find(w: str) -> str:
        while parent[w] != w:
            parent[w] = parent[parent[w]]
            w = parent[w]
        return w

    first: dict[str, dict[str, float]] = {}
    for w, items in buys.items():
        d: dict[str, float] = {}
        for token, t in items:
            d[token] = min(t, d.get(token, math.inf))
        first[w] = d
    by_token: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for w, d in first.items():
        for token, t in d.items():
            by_token[token].append((t, w))
    pairs: Counter = Counter()
    for items in by_token.values():
        items.sort()
        for i, (t, w) in enumerate(items):
            for t2, w2 in items[i + 1:]:
                if t2 - t > within_ms:
                    break
                if w2 != w:
                    pairs[(w, w2) if w < w2 else (w2, w)] += 1
    for (a, b), n in pairs.items():
        if n >= min_events and n >= min_share * min(len(first[a]), len(first[b])):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    return {w: find(w) for w in buys}


def representatives(
    cands: Sequence[CoBuy], clusters: Mapping[str, str], limit: int
) -> tuple[list[CoBuy], dict[str, list[str]]]:
    """The best-ranked member of each cluster, in rank order, up to ``limit``; and the members."""
    members: dict[str, list[str]] = defaultdict(list)
    for c in cands:
        members[clusters.get(c.wallet, c.wallet)].append(c.wallet)
    out, seen = [], set()
    for c in cands:
        cid = clusters.get(c.wallet, c.wallet)
        if cid in seen:
            continue
        seen.add(cid)
        out.append(c)
        if len(out) >= limit:
            break
    return out, dict(members)


def cluster_candidates(
    cands: Sequence[CoBuy], buyers_by_token: Mapping[str, Sequence[Leg]], cfg: Config, limit: int
) -> tuple[list[CoBuy], dict[str, str], dict[str, list[str]]]:
    """Collapse co-timed candidates to one representative per operator (rank order kept)."""
    names = {c.wallet for c in cands}
    buys: dict[str, list[tuple[str, float]]] = {w: [] for w in names}
    for token, legs in buyers_by_token.items():
        for leg in legs:
            if leg.wallet in names:
                buys[leg.wallet].append((token, leg.t_ms))
    clusters = cotime_clusters(buys, within_ms=cfg.cluster_ms)
    reps, members = representatives(cands, clusters, limit)
    return reps, clusters, members


def pick_candidates(stats: Mapping[str, CoBuy], cfg: Config) -> tuple[list[CoBuy], dict[str, int]]:
    """Followers out, thin evidence out; the rest ranked by distinct early tokens."""
    funnel = {"cobuyers": len(stats), "copiers": 0, "few_tokens": 0, "candidates": 0}
    keep: list[CoBuy] = []
    for cb in stats.values():
        if cb.copier_share >= cfg.copier_share:
            funnel["copiers"] += 1
        elif len(cb.tokens_early) < cfg.min_cobuy_tokens:
            funnel["few_tokens"] += 1
        else:
            keep.append(cb)
    keep.sort(key=lambda c: (-len(c.tokens_early), -c.before, c.wallet))
    funnel["candidates"] = len(keep)
    return keep, funnel


# --------------------------------------------------------------------------------------
# EVM on-chain parsing (pure)
# --------------------------------------------------------------------------------------


def _amount(data: Any) -> int:
    try:
        return int(str(data), 16) if data and str(data) != "0x" else 0
    except ValueError:
        return 0


def legs_from_transfers(
    wallet: str,
    logs: Iterable[Sequence[Any]],
    *,
    clock: Callable[[int], float],
    ignore_tokens: Iterable[str] = (),
) -> tuple[list[Leg], dict[str, int]]:
    """A wallet's buy/sell legs from its ERC-20 Transfer logs (in AND out, deduplicated).

    Each log is ``[block, tx_index, log_index, tx_hash, token, from, to, data]``. Per
    (transaction, token) the wallet's NET flow decides the side: in > out is a buy, out > in
    a sell, zero is a pass-through (a router hop or an arbitrage leg -- counted, never a
    trade). A mint (from the zero address) is never a buy.
    """
    w = wallet.lower()
    ign = {t.lower() for t in ignore_tokens}
    seen: set[tuple[str, int]] = set()
    flow: dict[tuple[str, str], list[Any]] = {}
    for lg in logs:
        block, txi, li, txh, token, frm, to, data = lg[:8]
        if (txh, int(li)) in seen:
            continue
        seen.add((txh, int(li)))
        token, frm, to = str(token).lower(), str(frm).lower(), str(to).lower()
        if token in ign:
            continue
        amt = _amount(data)
        rec = flow.setdefault((txh, token), [int(block), int(txi), int(li), 0, False])
        rec[2] = min(rec[2], int(li))
        if to == w and frm != w:
            rec[3] += amt
            if frm == ZERO:
                rec[4] = True
        elif frm == w and to != w:
            rec[3] -= amt
    legs: list[Leg] = []
    counts: Counter = Counter()
    for (txh, token), (block, txi, li, net, minted) in flow.items():
        if net > 0 and not minted:
            side = "buy"
        elif net < 0:
            side = "sell"
        else:
            counts["mint" if minted else "pass_through"] += 1
            continue
        counts[side] += 1
        legs.append(Leg(w, token, side, clock(block), txi * 100_000 + li, txh))
    legs.sort(key=lambda x: x.key)
    return legs, dict(counts)


def buyers_from_token_logs(
    token: str,
    logs: Iterable[Sequence[Any]],
    *,
    clock: Callable[[int], float],
    exclude: Iterable[str] = (),
) -> list[Leg]:
    """Final recipients of ``token`` per transaction: net receivers, minus the zero address
    and ``exclude`` (pool manager, curve, the token itself). A router that receives and
    forwards in the same transaction nets to zero and is not a buyer.

    Each log is ``[block, tx_index, log_index, tx_hash, from, to, data]``.
    """
    ex = {a.lower() for a in exclude} | {ZERO, token.lower()}
    by_tx: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    pos: dict[str, tuple[int, int, int]] = {}
    for lg in logs:
        block, txi, li, txh, frm, to, data = lg[:7]
        amt = _amount(data)
        by_tx[txh][str(to).lower()] += amt
        by_tx[txh][str(frm).lower()] -= amt
        p = (int(block), int(txi), int(li))
        if txh not in pos or p < pos[txh]:
            pos[txh] = p
    out: list[Leg] = []
    for txh, nets in by_tx.items():
        block, txi, li = pos[txh]
        for addr, net in nets.items():
            if net > 0 and addr not in ex:
                out.append(Leg(addr, token.lower(), "buy", clock(block), txi * 100_000 + li, txh))
    out.sort(key=lambda x: x.key)
    return out


def v4_index(init_logs: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """``{token: [{pool, quote, token_is0, fee, block}]}`` from v4 ``Initialize`` logs."""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[str] = set()
    for lg in init_logs:
        topics = lg.get("topics") or []
        if len(topics) < 4 or str(topics[0]).lower() != V4_INIT:
            continue
        pool = str(topics[1]).lower()
        if pool in seen:
            continue
        seen.add(pool)
        c0, c1 = "0x" + str(topics[2])[-40:].lower(), "0x" + str(topics[3])[-40:].lower()
        data = str(lg.get("data") or "0x")[2:]
        fee = int(data[0:64], 16) if len(data) >= 64 else None
        block = int(str(lg.get("blockNumber") or "0x0"), 16)
        out[c1].append({"pool": pool, "quote": c0, "token_is0": False, "fee": fee, "block": block})
        out[c0].append({"pool": pool, "quote": c1, "token_is0": True, "fee": fee, "block": block})
    return out


def _compact_swap(lg: Mapping[str, Any]) -> list[Any] | None:
    """A v4 ``Swap`` log -> ``[pool, block, seq, p1per0]`` (token1 per token0, raw units, from
    the post-swap ``sqrtPriceX96``), or ``None`` for a zero-amount swap."""
    topics = lg.get("topics") or []
    data = str(lg.get("data") or "0x")[2:]
    if len(topics) < 2 or len(data) < 64 * 3:
        return None
    if int(data[0:64], 16) == 0 and int(data[64:128], 16) == 0:
        return None
    sqrt_p = int(data[128:192], 16)
    if sqrt_p <= 0:
        return None
    p1per0 = (sqrt_p * sqrt_p) / float(1 << 192)
    if not (p1per0 > 0 and math.isfinite(p1per0)):
        return None
    seq = int(str(lg.get("transactionIndex") or "0x0"), 16) * 100_000 + int(str(lg.get("logIndex") or "0x0"), 16)
    return [str(topics[1]).lower(), int(str(lg.get("blockNumber")), 16), seq, p1per0]


def _int256(word: str) -> int:
    v = int(word, 16)
    return v - (1 << 256) if v >= 1 << 255 else v


def v4_prints(
    swap_logs: Iterable[Mapping[str, Any]], *, token_is0: bool, clock: Callable[[int], float]
) -> list[tuple[float, int, float]]:
    """Post-swap STATE prices (quote atoms per token atom) from v4 ``Swap`` logs.

    ``sqrtPriceX96`` is the pool's price after the swap, token1 per token0; a token that is
    currency1 is quoted as its inverse. Zero-amount swaps are skipped.
    """
    out: list[tuple[float, int, float]] = []
    seen: set[tuple[str, str]] = set()
    for lg in swap_logs:
        k = (str(lg.get("transactionHash")), str(lg.get("logIndex")))
        if k in seen:
            continue
        seen.add(k)
        data = str(lg.get("data") or "0x")[2:]
        if len(data) < 64 * 3:
            continue
        a0, a1 = _int256(data[0:64]), _int256(data[64:128])
        sqrt_p = int(data[128:192], 16)
        if sqrt_p <= 0 or (a0 == 0 and a1 == 0):
            continue
        p1per0 = (sqrt_p * sqrt_p) / float(1 << 192)
        if p1per0 <= 0 or not math.isfinite(p1per0):
            continue
        price = p1per0 if token_is0 else 1.0 / p1per0
        block = int(str(lg.get("blockNumber")), 16)
        seq = int(str(lg.get("transactionIndex") or "0x0"), 16) * 100_000 + int(str(lg.get("logIndex") or "0x0"), 16)
        out.append((clock(block), seq, price))
    return out


# --------------------------------------------------------------------------------------
# read-only database adapters
# --------------------------------------------------------------------------------------


def _cache_load(path: str) -> Any:
    """Read a cache entry: zlib-compressed JSON (``.jz``) or a legacy plain ``.json``."""
    if path.endswith(".jz"):
        with open(path, "rb") as fh:
            return json.loads(zlib.decompress(fh.read()))
    with open(path) as fh:
        return json.load(fh)


def _cache_dump(path: str, obj: Any) -> None:
    with open(path + ".tmp", "wb") as fh:
        fh.write(zlib.compress(json.dumps(obj, separators=(",", ":")).encode(), 6))
    os.replace(path + ".tmp", path)


class _Rows(list):
    """A fully fetched result: the cursor shape the adapters use, with no statement left open."""

    def fetchall(self) -> list[Any]:
        return list(self)

    def fetchone(self) -> Any:
        return self[0] if self else None


class Db:
    """Short, bounded, read-only reads of the live database.

    The box rules (lead, 2026-10-04, after a 1 h 27 min reader pinned a 4.4 GB WAL):

    * a FRESH connection per statement, closed as soon as its rows are fetched, so no read
      transaction outlives one statement (and nothing can be frozen holding one);
    * a statement still running after ``max_query_s`` is interrupted -- the caller chunks;
    * before every statement the WAL is checked, and while it is over ``wal_limit_bytes`` we
      wait instead of adding a reader to a growing WAL (over ``wal_soft_bytes``, a shorter wait);
    * after every statement we rest at least as long as it ran (``duty`` = 0.5) and never under
      ``min_pause_s``: back-to-back short readers still leave no instant with zero readers, and
      the WAL cannot restart without one. MEASURED 2026-10-04: a 20-minute run of ~1 ms reads
      with no pause grew the WAL 536 -> 869 MB at ~1 MB/s; it fell back to 536 MB the second
      the run stopped (checkpoint starvation, not a long transaction);
    * ``cache_dir`` keeps each answer (zlib JSON) so a run killed by its ``timeout`` resumes
      from where it stopped instead of re-reading the database.
    """

    def __init__(self, path: str, *, cache_dir: str | None = None, max_query_s: float = 30.0,
                 wal_limit_bytes: int = 1_500_000_000, wal_wait_s: float = 60.0,
                 wal_soft_bytes: int = 1_000_000_000, wal_soft_wait_s: float = 20.0,
                 min_pause_s: float = 0.05, duty: float = 0.5,
                 log: Callable[[str], None] = print) -> None:
        self.path, self.cache_dir, self.max_query_s = path, cache_dir, float(max_query_s)
        self.wal_limit_bytes, self.wal_wait_s, self.log = int(wal_limit_bytes), float(wal_wait_s), log
        self.wal_soft_bytes, self.wal_soft_wait_s = int(wal_soft_bytes), float(wal_soft_wait_s)
        self.min_pause_s, self.duty = float(min_pause_s), float(duty)
        self.queries = self.cached = self.interrupted = self.wal_waits = 0
        if cache_dir:
            os.makedirs(os.path.join(cache_dir, "db"), exist_ok=True)

    def _wal_gate(self) -> None:
        wal = self.path + "-wal"
        while True:
            try:
                size = os.path.getsize(wal)
            except OSError:
                return
            if size <= self.wal_soft_bytes:
                return
            self.wal_waits += 1
            wait = self.wal_wait_s if size > self.wal_limit_bytes else self.wal_soft_wait_s
            self.log(f"WAL {size / 1e9:.2f} GB > {self.wal_soft_bytes / 1e9:.2f} GB: waiting {wait:.0f}s")
            time.sleep(wait)
            if size <= self.wal_limit_bytes:
                return  # soft gate: one rest, then go on

    def _key(self, sql: str, params: Sequence[Any]) -> str | None:
        if not self.cache_dir:
            return None
        h = hashlib.sha1(json.dumps([sql, list(params)], default=str).encode()).hexdigest()
        return os.path.join(self.cache_dir, "db", h + ".jz")

    def execute(self, sql: str, params: Sequence[Any] = (), *, cache: bool = False) -> _Rows:
        """All rows of one statement. Raises ``sqlite3.OperationalError`` when interrupted."""
        path = self._key(sql, params) if cache else None
        if path and os.path.exists(path):
            self.cached += 1
            return _Rows(tuple(r) for r in _cache_load(path))
        self._wal_gate()
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=10)
        try:
            conn.execute("PRAGMA query_only=1")
            start = time.monotonic()
            conn.set_progress_handler(lambda: 1 if time.monotonic() - start > self.max_query_s else 0, 10_000)
            try:
                rows = conn.execute(sql, tuple(params)).fetchall()
            except sqlite3.OperationalError as exc:
                if "interrupt" in str(exc).lower():
                    self.interrupted += 1
                raise
        finally:
            conn.close()
            took = time.monotonic() - start if "start" in locals() else 0.0
            # duty cycle: leave the checkpointer an instant with no reader at all
            time.sleep(max(self.min_pause_s, took * self.duty / max(1e-9, 1.0 - self.duty)))
        self.queries += 1
        if path:
            _cache_dump(path, [list(r) for r in rows])
        return _Rows(rows)

    def scan_ids(self, sql: str, tail: Sequence[Any], lo_id: int, hi_id: int, *, step: int = 100_000,
                 cache: bool = True) -> Iterable[tuple[Any, ...]]:
        """Rows of ``sql`` (which starts its WHERE with ``id > ? AND id <= ?``) over ``(lo_id, hi_id]``
        in primary-key chunks, halving any chunk that runs past the statement limit."""
        a = int(lo_id)
        cur = int(step)
        while a < hi_id:
            b = min(int(hi_id), a + cur)
            try:
                rows = self.execute(sql, (a, b, *tail), cache=cache)
            except sqlite3.OperationalError as exc:
                if "interrupt" not in str(exc).lower() or cur <= 1_000:
                    raise
                cur //= 2
                continue
            yield from rows
            a = b


def split_rows(
    conn: Db, sql: str, params: Callable[[int, int], Sequence[Any]], a: int, b: int, *, min_span: int = 60_000,
    cache: bool = True,
) -> list[tuple[Any, ...]]:
    """Rows of a ``[a, b]`` time-range query; a statement interrupted for running too long is
    re-asked as two halves instead (down to ``min_span``), so no single read runs long."""
    try:
        return list(conn.execute(sql, params(a, b), cache=cache))
    except sqlite3.OperationalError as exc:
        if "interrupt" not in str(exc).lower() or b - a <= min_span:
            raise
    mid = (a + b) // 2
    return split_rows(conn, sql, params, a, mid, min_span=min_span, cache=cache) + split_rows(
        conn, sql, params, mid + 1, b, min_span=min_span, cache=cache)


def connect_ro(path: str, **kw: Any) -> Db:
    """The module's only way into a database: a :class:`Db` (short reads, WAL gate)."""
    return Db(path, **kw)


def protection_health(conn: Db) -> tuple[bool, str]:
    """Latest watchdog heartbeat: fresh and ``duration_ms < 12000``. Read-only."""
    try:
        rows = conn.execute(
            "SELECT ts_ms, payload FROM events WHERE kind='system' ORDER BY id DESC LIMIT 400"
        ).fetchall()
    except sqlite3.Error as exc:
        return False, f"events unreadable: {exc}"
    for ts, payload in rows:
        try:
            d = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if d.get("event") == "heartbeat":
            age = time.time() - ts / 1000.0
            dur = d.get("duration_ms") or 0
            return (age < 180 and dur < 12_000), f"heartbeat age {age:.0f}s duration_ms {dur}"
    return False, "no heartbeat in the last 400 system events"


class BlockClock:
    """Linear block -> ms map anchored on one tape point; only relative distances matter."""

    def __init__(self, b0: int, t0_ms: float, ms_per_block: float) -> None:
        self.b0, self.t0, self.rate = int(b0), float(t0_ms), float(ms_per_block)

    def __call__(self, block: int) -> float:
        return self.t0 + (int(block) - self.b0) * self.rate

    def block(self, t_ms: float) -> int:
        # round, not floor: block(clock(b)) must give back b exactly through float error.
        return int(round(self.b0 + (float(t_ms) - self.t0) / self.rate))


def rh_clock(conn: Db, chain: str = "robinhood", *, at_block: int | None = None) -> BlockClock:
    """Anchored on the newest slotted print -- or, given ``at_block``, on the first print at or
    after it, so every resumed invocation maps blocks to the SAME milliseconds (and so hits the
    same cache keys); rate from :data:`BLOCK_MS`."""
    if at_block is not None:
        row = conn.execute(
            "SELECT slot, ts_ms FROM swaps INDEXED BY idx_swaps_slot WHERE chain = ? AND slot >= ? ORDER BY slot LIMIT 1",
            (chain, int(at_block)), cache=True,
        ).fetchone()
        if row is not None:
            return BlockClock(int(row[0]), float(row[1]), BLOCK_MS.get(chain, 101.5))
    hi = conn.execute("SELECT max(id) FROM swaps").fetchone()[0]
    row = conn.execute(
        "SELECT slot, ts_ms FROM swaps NOT INDEXED WHERE id > ? AND chain = ? AND slot IS NOT NULL ORDER BY id DESC LIMIT 1",
        (int(hi) - 200_000, chain),
    ).fetchone()
    if row is None:
        raise RuntimeError("no slotted swaps to anchor the block clock")
    return BlockClock(int(row[0]), float(row[1]), BLOCK_MS.get(chain, 101.5))


def db_curve_prints(
    conn: Db, chain: str, token: str, lo_ms: float, hi_ms: float, *, clock: BlockClock,
    source: str = "robinhood",
) -> list[tuple[float, int, float, bool]]:
    """Pons curve prints (quote atoms per token atom) from ONE continuous source, on the block
    clock. ``ts_ms`` bounds carry a 3 h margin for the clock's drift; the slot decides."""
    margin = 3 * HOUR_MS
    lo_b, hi_b = clock.block(lo_ms), clock.block(hi_ms)
    rows = split_rows(
        conn,
        "SELECT slot, block_index, amount_native, amount_token FROM swaps INDEXED BY idx_swaps_token "
        "WHERE chain = ? AND token = ? AND ts_ms BETWEEN ? AND ? AND source = ? AND slot IS NOT NULL",
        lambda a, b: (chain, token, a, b, source), int(lo_ms - margin), int(hi_ms + margin),
    )
    out = []
    for slot, bi, an, at in rows:
        if not lo_b <= int(slot) <= hi_b:
            continue
        try:
            a_n, a_t = int(an), int(at)
        except (TypeError, ValueError):
            continue
        if a_n > 0 and a_t > 0:
            out.append((clock(int(slot)), int(bi or 0), a_n / a_t, True))
    return out


def db_dark_gaps(
    conn: Db, chain: str, lo_b: int, hi_b: int, *, clock: BlockClock, bucket_blocks: int = 6_000
) -> list[tuple[float, float]]:
    """Outages of the chain's slotted tape: ``bucket_blocks`` (~10 min) windows with no print
    at all. One index seek per bucket on ``idx_swaps_slot`` -- never a scan."""
    gaps: list[tuple[float, float]] = []
    b = lo_b
    while b < hi_b:
        row = conn.execute(
            "SELECT 1 FROM swaps INDEXED BY idx_swaps_slot WHERE chain = ? AND slot >= ? AND slot < ? LIMIT 1",
            (chain, b, b + bucket_blocks), cache=True,
        ).fetchone()
        if row is None:
            if gaps and abs(gaps[-1][1] - clock(b)) < 1.0:
                gaps[-1] = (gaps[-1][0], clock(b + bucket_blocks))
            else:
                gaps.append((clock(b), clock(b + bucket_blocks)))
        b += bucket_blocks
    return gaps


def db_sparse_prints(
    conn: Db, chain: str, token: str, spans: Sequence[tuple[float, float]], *, source: str | None = None
) -> tuple[list[tuple[float, int, float]], str | None]:
    """One swap source per token -- sources disagree on scale (``alpha_sources._series``):
    ``source`` when the caller pinned one, else the source with the most prints in ``spans``."""
    rows: list[tuple[Any, ...]] = []
    for a, b in spans:
        rows += split_rows(
            conn,
            "SELECT ts_ms, id, price_usd, source FROM swaps INDEXED BY idx_swaps_token "
            "WHERE chain = ? AND token = ? AND ts_ms BETWEEN ? AND ? AND price_usd IS NOT NULL AND price_usd != ''",
            lambda x, y: (chain, token, x, y), int(a), int(b),
        )
    if not rows:
        return [], None
    counts = Counter(r[3] for r in rows)
    best = source if source is not None else max(counts, key=lambda s: (counts[s], str(s)))
    out = []
    for ts, rid, px, src in rows:
        if src != best:
            continue
        try:
            p = float(px)
        except (TypeError, ValueError):
            continue
        if p > 0 and math.isfinite(p):
            out.append((float(ts), int(rid), p))
    return out, best


def db_last_print(
    conn: Db, chain: str, token: str, t_ms: float, *, source: str
) -> tuple[float, int, float] | None:
    """The newest print of ``source`` at or before ``t_ms`` (one index seek)."""
    for ts, rid, px in conn.execute(
        "SELECT ts_ms, id, price_usd FROM swaps INDEXED BY idx_swaps_token WHERE chain = ? AND token = ? "
        "AND ts_ms <= ? AND source = ? AND price_usd IS NOT NULL AND price_usd != '' ORDER BY ts_ms DESC LIMIT 1",
        (chain, token, int(t_ms), source), cache=True,
    ):
        try:
            p = float(px)
        except (TypeError, ValueError):
            return None
        return (float(ts), int(rid), p) if p > 0 and math.isfinite(p) else None
    return None


def thin_prints(
    rows: Sequence[tuple[float, int, float]], instants: Sequence[float], offsets: Sequence[int], max_wait_ms: int
) -> list[tuple[float, int, float]]:
    """Only the rows a sparse-tape copy can read: for each instant + offset, the first print
    inside the wait and the last print at or before it. Bounds memory to a few rows per
    instant however busy the token is."""
    rs = sorted(rows)
    keys = [(r[0], r[1]) for r in rs]
    keep: dict[tuple[float, int], tuple[float, int, float]] = {}
    for x in instants:
        for off in offsets:
            t = x + off
            i = bisect.bisect_left(keys, (t, -math.inf))
            if i < len(rs) and rs[i][0] <= t + max_wait_ms:
                keep[keys[i]] = rs[i]
            j = bisect.bisect_right(keys, (t, math.inf)) - 1
            if j >= 0:
                keep[keys[j]] = rs[j]
    return list(keep.values())


def sparse_instants(legs: Iterable[Leg], cfg: Config, *, marks: Sequence[float] = ()) -> dict[str, list[float]]:
    """``{token: instants}`` a copy of ``legs`` reads a price at, before any lag: each leg,
    each buy's time stop, and the period marks."""
    out: dict[str, list[float]] = defaultdict(list)
    for leg in legs:
        out[leg.token].append(leg.t_ms)
        if leg.side == "buy" and cfg.max_hold_ms:
            out[leg.token].append(leg.t_ms + cfg.max_hold_ms)
    for token in out:
        out[token] += [float(m) for m in marks]
        out[token] = sorted(set(out[token]))
    return dict(out)


def db_wallet_legs(
    conn: Db, chain: str, wallet: str, lo_ms: float, hi_ms: float, *,
    max_legs: int, quote_assets: Iterable[str] = (),
) -> list[Leg] | None:
    """A wallet's legs from the tape (index seek), or ``None`` past ``max_legs``."""
    q = set(quote_assets)
    rows = conn.execute(
        "SELECT token, side, ts_ms, id, tx FROM swaps INDEXED BY idx_swaps_wallet "
        "WHERE chain = ? AND wallet = ? AND ts_ms >= ? AND ts_ms < ? AND token != '' LIMIT ?",
        (chain, wallet, int(lo_ms), int(hi_ms), int(max_legs) + 1), cache=True,
    ).fetchall()
    if len(rows) > max_legs:
        return None
    seen: set[tuple[str, str, str]] = set()
    out = []
    for token, side, ts, rid, tx in rows:
        if token in q or side not in ("buy", "sell"):
            continue
        # The same trade arrives from more than one feed; one tx/token/side is one leg.
        k = (str(tx), str(token), str(side))
        if tx and k in seen:
            continue
        seen.add(k)
        out.append(Leg(wallet, token, side, float(ts), int(rid), str(tx or "")))
    out.sort(key=lambda x: x.key)
    return out


def db_token_buyers(conn: Db, chain: str, token: str, lo_ms: float, hi_ms: float) -> list[Leg]:
    rows = conn.execute(
        "SELECT wallet, ts_ms, id, tx FROM swaps INDEXED BY idx_swaps_token "
        "WHERE chain = ? AND token = ? AND ts_ms BETWEEN ? AND ? AND side = 'buy'",
        (chain, token, int(lo_ms), int(hi_ms)), cache=True,
    ).fetchall()
    return [Leg(str(w), token, "buy", float(ts), int(i), str(tx or "")) for w, ts, i, tx in rows if w]


# --------------------------------------------------------------------------------------
# paced RPC (robinhood) and GMGN, through the kaiba limiter -- read-only calls only
# --------------------------------------------------------------------------------------


class Rpc:
    """JSON-RPC reads: <= 2 calls per HTTP batch, ``gap_s`` apart, RESEARCH priority, in its
    own limiter family so a 429 cools this down and not the ingest poller. Every answer is
    cached under ``cache_dir`` so a rerun costs nothing. The URL is never printed."""

    ALLOWED = frozenset({"eth_getLogs", "eth_getCode", "eth_blockNumber"})

    def __init__(self, url: str, *, cache_dir: str | None, gap_s: float = 6.0,
                 health: Callable[[], tuple[bool, str]] | None = None, health_every: int = 25,
                 max_http: int = 5_000, log: Callable[[str], None] = print) -> None:
        self.url, self.cache_dir, self.gap_s = url, cache_dir, float(gap_s)
        self.health, self.health_every, self.max_http, self.log = health, health_every, max_http, log
        self.http = self.calls = self.cached = self.failed = self.r429 = 0
        self._last = 0.0
        self._since_health = 10**9
        if cache_dir:
            os.makedirs(os.path.join(cache_dir, "rpc"), exist_ok=True)

    def _path(self, method: str, params: Any, tag: str = "") -> str | None:
        if not self.cache_dir:
            return None
        h = hashlib.sha1(json.dumps([method, params], sort_keys=True).encode()).hexdigest()
        return os.path.join(self.cache_dir, "rpc", h + (f".{tag}" if tag else "") + ".jz")

    @staticmethod
    def _existing(path: str | None) -> str | None:
        """The cache file for ``path`` -- compressed, or a legacy plain ``.json`` -- if any."""
        if not path:
            return None
        for cand in (path, path[:-3] + ".json"):
            if os.path.exists(cand):
                return cand
        return None

    def many(
        self, calls: Sequence[tuple[str, list[Any]]], *, cache: bool = True,
        compact: Callable[[Mapping[str, Any]], Any] | None = None, tag: str = "",
    ) -> list[Any]:
        """Results in order; a refused call yields ``{"error": ...}``, a dead batch ``None``.

        ``compact`` maps each log of a list result to a smaller row BEFORE it is cached or held
        (``None`` rows are dropped); ``tag`` keeps compacted and raw caches apart."""
        out: list[Any] = [None] * len(calls)
        todo: list[int] = []
        for i, (m, p) in enumerate(calls):
            if m not in self.ALLOWED:
                raise ValueError(f"{m} is not a read this module may make")
            path = self._existing(self._path(m, p, tag)) if cache else None
            raw = self._existing(self._path(m, p)) if cache and tag and compact is not None else None
            if path:
                out[i] = _cache_load(path)
                self.cached += 1
            elif raw:
                # an earlier uncompacted answer to the same query: compact it, never refetch
                r = _cache_load(raw)
                out[i] = [c for c in (compact(x) for x in r) if c is not None] if isinstance(r, list) else r
                self.cached += 1
            else:
                todo.append(i)
        for k in range(0, len(todo), 2):
            idx = todo[k:k + 2]
            for i, r in zip(idx, self._send([calls[i] for i in idx]), strict=True):
                if compact is not None and isinstance(r, list):
                    r = [c for c in (compact(x) for x in r) if c is not None]
                out[i] = r
                path = self._path(*calls[i], tag) if cache else None
                # A size refusal is deterministic for a fixed block range: cache it too, so a
                # rerun does not re-ask the node a question it already said it will not answer.
                if path and r is not None and (not (isinstance(r, dict) and "error" in r) or _is_size_refusal(r)):
                    _cache_dump(path, r)
        return out

    def _send(self, calls: Sequence[tuple[str, list[Any]]], tries: int = 4) -> list[Any]:
        from kaiba.core.limiter import Priority  # lazy: the tests never touch the network
        from kaiba.providers._http import post_json

        if self.http >= self.max_http:
            raise RuntimeError(f"RPC budget exhausted ({self.http} HTTP requests)")
        if self.health and self._since_health >= self.health_every:
            while True:
                ok, msg = self.health()
                self.log(f"health {'ok' if ok else 'PAUSE'}: {msg} (rpc http {self.http})")
                if ok:
                    break
                time.sleep(60)
            self._since_health = 0
        for attempt in range(tries):
            wait = self.gap_s - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            body = [{"jsonrpc": "2.0", "id": i + 1, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
            got = post_json("robinhood-rpc", "research.getLogs", self.url, json_body=body,
                            priority=Priority.RESEARCH, wait_for_slot_s=30.0, timeout_s=40.0)
            self.http += 1
            self.calls += len(calls)
            self._since_health += 1
            self._last = time.time()
            if got.ok and isinstance(got.data, list):
                by_id = {r.get("id"): r for r in got.data if isinstance(r, dict)}
                res = []
                for i in range(len(calls)):
                    r = by_id.get(i + 1, {})
                    res.append({"error": r["error"]} if "error" in r else r.get("result"))
                return res
            note = str(got.receipt.note or "")
            self.failed += 1
            if "429" in note:
                self.r429 += 1
            self.log(f"rpc batch failed (attempt {attempt + 1}): {note[:120]}")
            time.sleep(120 if ("429" in note or "rate" in note.lower()) else 10 * (attempt + 1))
        return [None] * len(calls)

    def logs(self, jobs: Mapping[str, Mapping[str, Any]], *, min_span: int = 50) -> dict[str, list[dict[str, Any]]]:
        """``{tag: logs}`` for ``{tag: filter with fromBlock/toBlock}``, halving any range the
        node refuses as too large, two queries per HTTP request."""
        out: dict[str, list[dict[str, Any]]] = {t: [] for t in jobs}
        queue = [(t, int(f["fromBlock"], 16), int(f["toBlock"], 16)) for t, f in jobs.items()]
        while queue:
            chunk, queue = queue[:2], queue[2:]
            calls = [("eth_getLogs", [dict(jobs[t], fromBlock=hex(a), toBlock=hex(b))]) for t, a, b in chunk]
            for (t, a, b), r in zip(chunk, self.many(calls), strict=True):
                if isinstance(r, list):
                    out[t].extend(r)
                elif b - a >= min_span:
                    mid = (a + b) // 2
                    queue = [(t, a, mid), (t, mid + 1, b), *queue]
                else:
                    self.log(f"giving up on {t} [{a},{b}]: {str(r)[:120]}")
        return out


def _slim_transfer(lg: Mapping[str, Any]) -> list[Any]:
    """``[block, txi, li, tx, token, from, to, data]``."""
    return [int(lg["blockNumber"], 16), int(lg["transactionIndex"], 16), int(lg["logIndex"], 16),
            str(lg["transactionHash"]).lower(), str(lg["address"]).lower(),
            "0x" + lg["topics"][1][-40:].lower(), "0x" + lg["topics"][2][-40:].lower(), lg.get("data")]


def _is_size_refusal(r: Any) -> bool:
    msg = str((r.get("error") or {}).get("message", "")).lower() if isinstance(r, dict) else ""
    return any(k in msg for k in ("10000", "10k", "response size", "more than"))


def _slim_or_none(lg: Mapping[str, Any]) -> list[Any] | None:
    return _slim_transfer(lg) if len(lg.get("topics") or []) == 3 else None


def _topic(addr: str) -> str:
    return "0x" + "0" * 24 + addr.lower()[2:]


class EvmSource:
    """Robinhood on-chain reads for one run: wallet histories, co-buy windows, v4 prices."""

    def __init__(self, rpc: Rpc, clock: BlockClock, *, pool_manager: str = RH_POOL_MANAGER) -> None:
        self.rpc, self.clock, self.pm = rpc, clock, pool_manager.lower()

    def histories(
        self, wallets: Sequence[str], lo_b: int, hi_b: int, *, group: int = 6
    ) -> tuple[dict[str, list[list[Any]]], set[str], set[str]]:
        """``(logs, too_active, unavailable)``. ``logs[w]`` is every Transfer into or out of ``w``
        as ``[block, txi, li, tx, token, from, to, data]``, compacted as it arrives.

        Wallets are OR-grouped; a group the node refuses is split by WALLET, and a single wallet
        whose inflow or outflow over the whole window still overflows the node's 10k-log cap is
        a bot (over ~770 transfers a day) -- returned in ``too_active`` WITHOUT being paged in.
        A wallet whose query failed outright is ``unavailable``, never "too active".
        """
        ws = sorted({w.lower() for w in wallets})
        out: dict[str, list[list[Any]]] = {w: [] for w in ws}
        too_active: set[str] = set()
        unavailable: set[str] = set()

        def flt(direction: str, grp: Sequence[str]) -> dict[str, Any]:
            topics = [_topic(w) for w in grp]
            t = [TRANSFER, None, topics] if direction == "in" else [TRANSFER, topics]
            return {"topics": t, "fromBlock": hex(lo_b), "toBlock": hex(hi_b)}

        queue: list[tuple[str, list[str]]] = []
        for k in range(0, len(ws), group):
            queue += [("in", ws[k:k + group]), ("out", ws[k:k + group])]
        while queue:
            chunk, queue = queue[:2], queue[2:]
            res = self.rpc.many([("eth_getLogs", [flt(d, g)]) for d, g in chunk], compact=_slim_or_none, tag="slim")
            for (d, g), r in zip(chunk, res, strict=True):
                if isinstance(r, list):
                    for rec in r:
                        for w in {rec[5], rec[6]} & out.keys():
                            out[w].append(rec)
                elif r is None and len(g) == 1:
                    unavailable.add(g[0])
                elif len(g) > 1:
                    half = len(g) // 2
                    queue = [(d, g[:half]), (d, g[half:]), *queue]
                else:
                    too_active.add(g[0])
        for w in too_active | unavailable:
            out[w] = []
        return out, too_active, unavailable

    def window_logs(self, windows: Sequence[tuple[str, int, int]]) -> dict[tuple[str, int, int], list[list[Any]]]:
        """``[block, txi, li, tx, from, to, data]`` per (token, lo, hi) window."""
        jobs = {f"{t}:{a}:{b}": {"address": t, "topics": [TRANSFER], "fromBlock": hex(a), "toBlock": hex(b)}
                for t, a, b in windows}
        out = {}
        for key, logs in self.rpc.logs(jobs).items():
            t, a, b = key.split(":")
            out[(t, int(a), int(b))] = [
                [r[0], r[1], r[2], r[3], r[5], r[6], r[7]]
                for r in (_slim_transfer(lg) for lg in logs if len(lg.get("topics") or []) == 3)
            ]
        return out

    def is_contract(self, addresses: Sequence[str]) -> dict[str, bool | None]:
        """Has deployed code. An EIP-7702 delegation (``0xef0100``) is an EOA, not a contract."""
        res = self.rpc.many([("eth_getCode", [a, "latest"]) for a in addresses], cache=True)
        out: dict[str, bool | None] = {}
        for a, code in zip(addresses, res, strict=True):
            if not isinstance(code, str):
                out[a] = None
            else:
                c = code.lower()
                out[a] = not (c in ("0x", "0x0") or c.startswith("0xef0100"))
        return out

    def v4_pools(self, tokens: Sequence[str], hi_b: int, *, group: int = 80) -> dict[str, list[dict[str, Any]]]:
        """``Initialize`` logs where the token is currency1 (every native-ETH pair) or currency0."""
        toks = sorted({t.lower() for t in tokens})
        jobs: dict[str, dict[str, Any]] = {}
        for k in range(0, len(toks), group):
            grp = [_topic(t) for t in toks[k:k + group]]
            jobs[f"c1:{k}"] = {"address": self.pm, "topics": [V4_INIT, None, None, grp], "fromBlock": hex(0), "toBlock": hex(hi_b)}
            jobs[f"c0:{k}"] = {"address": self.pm, "topics": [V4_INIT, None, grp], "fromBlock": hex(0), "toBlock": hex(hi_b)}
        idx = v4_index([lg for logs in self.rpc.logs(jobs).values() for lg in logs])
        return {t: idx.get(t, []) for t in toks}

    def v4_swaps(
        self, segments: Sequence[tuple[str, int, int]], instants: Mapping[str, Sequence[int]], *,
        group: int = 40, max_group_blocks: int = 2_400_000, back: int = 300, fwd: int = 160,
        log: Callable[[str], None] = print,
    ) -> tuple[dict[str, list[list[Any]]], set[str]]:
        """``({pool: [[block, seq, p1per0], ...]}, hot pools)``.

        Cold pools: every swap over their (pool, lo, hi) segments, OR-grouped. A group the node
        refuses is split by POOL, and a single pool that is still too busy for its own span is
        HOT: it is priced only in ``[t - back, t + fwd]`` block windows around ``instants`` (the
        legs, the time stops and the period ends a copy can read). Rows are compacted before
        they are cached or held -- v4 on robinhood runs ~1.3 swaps a block chain-wide.
        """
        out: dict[str, list[list[Any]]] = defaultdict(list)
        hot: set[str] = set()
        groups: list[list[tuple[str, int, int]]] = []
        cur: list[tuple[str, int, int]] = []
        for seg in sorted(segments, key=lambda x: x[1]):
            if cur and (len({p for p, _, _ in cur} | {seg[0]}) > group
                        or max(seg[2], max(x for _, _, x in cur)) - min(x for _, x, _ in cur) > max_group_blocks):
                groups.append(cur)
                cur = []
            cur.append(seg)
        if cur:
            groups.append(cur)

        def query(pools: Sequence[str], a: int, b: int) -> tuple[str, list[Any]]:
            return ("eth_getLogs", [{"address": self.pm, "topics": [V4_SWAP, sorted(set(pools))],
                                     "fromBlock": hex(a), "toBlock": hex(b)}])

        def take(rows: Sequence[Sequence[Any]]) -> None:
            for r in rows:
                out[r[0]].append([r[1], r[2], r[3]])

        queue = [(sorted({p for p, _, _ in g}), min(a for _, a, _ in g), max(b for _, _, b in g)) for g in groups]
        while queue:
            chunk, queue = queue[:2], queue[2:]
            res = self.rpc.many([query(*c) for c in chunk], compact=_compact_swap, tag="v4c")
            for (pools, a, b), r in zip(chunk, res, strict=True):
                if isinstance(r, list):
                    take(r)
                elif len(pools) > 1:
                    half = len(pools) // 2
                    queue = [(pools[:half], a, b), (pools[half:], a, b), *queue]
                else:
                    hot.add(pools[0])
        windows: list[tuple[str, int, int]] = []
        for pool in sorted(hot):
            merged: list[list[int]] = []
            for a, b in sorted((t - back, t + fwd) for t in instants.get(pool, ())):
                if merged and a <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b])
            windows += [(pool, a, b) for a, b in merged]
        log(f"[rh] v4: {len(groups)} cold groups, {len(hot)} hot pools priced in {len(windows)} windows")
        queue = [([p], a, b) for p, a, b in windows]
        while queue:
            chunk, queue = queue[:2], queue[2:]
            res = self.rpc.many([query(*c) for c in chunk], compact=_compact_swap, tag="v4c")
            for (pools, a, b), r in zip(chunk, res, strict=True):
                if isinstance(r, list):
                    take(r)
                elif b - a > 20:
                    mid = (a + b) // 2
                    queue = [(pools, a, mid), (pools, mid + 1, b), *queue]
        for rows in out.values():
            rows.sort()
        return out, hot


class Gmgn:
    """Budgeted GMGN reads at DISCOVERY priority, >= ``gap_s`` apart, health-gated every 50.
    Annotation only: a vendor figure is never a grade input."""

    def __init__(self, *, budget: int, gap_s: float = 2.0, health: Callable[[], tuple[bool, str]] | None = None,
                 log: Callable[[str], None] = print) -> None:
        self.budget, self.gap_s, self.health, self.log = budget, gap_s, health, log
        self.calls = 0
        self._last = 0.0

    def stats(self, wallet: str, chain: str, period: str = "30d") -> dict[str, Any] | None:
        from kaiba.core.limiter import Priority
        from kaiba.providers import gmgn_cli

        if self.calls >= self.budget:
            return None
        if self.health and self.calls % 50 == 0:
            while True:
                ok, msg = self.health()
                self.log(f"health {'ok' if ok else 'PAUSE'}: {msg} (gmgn {self.calls})")
                if ok:
                    break
                time.sleep(60)
        wait = self.gap_s - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        res = gmgn_cli.portfolio_stats(wallet, chain, period=period, priority=Priority.DISCOVERY,
                                       ttl_s=0, stale_grace_s=0, wait_for_slot_s=15.0)
        self.calls += 1
        self._last = time.time()
        if not res.ok:
            self.log(f"gmgn stats {wallet[:10]} failed: {str(res.receipt.note)[:120]}")
            return None
        data = res.data[0] if isinstance(res.data, list) and res.data else res.data
        return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------------------


def _rank_key(ws: WalletScore, cfg: Config) -> tuple:
    g = ws.outs.get(cfg.grade_lag_ms) or {}
    lo = (g.get("ci") or (None, None))[0]
    return ({"A": 0, "B": 1}.get(ws.grade or "", 2), -(lo if lo is not None else -9.0), ws.wallet)


def _draw(pool: Iterable[str], n: int, seed: int) -> list[str]:
    items = sorted(set(pool))
    random.Random(seed).shuffle(items)
    return items[:n]


def _activity_matched(scores: Sequence[WalletScore], ref: Sequence[WalletScore]) -> list[WalletScore]:
    """Wallets whose earlier-period trip count lies inside the reference group's 10th-90th
    percentile band -- "similar activity"."""
    xs = sorted(r.trips_in for r in ref if r.trips_in)
    if not xs:
        return list(scores)
    lo, hi = xs[int(0.1 * (len(xs) - 1))], xs[int(0.9 * (len(xs) - 1))]
    return [s for s in scores if lo <= s.trips_in <= hi]


def rh_quote_tokens(conn: Db) -> set[str]:
    """Pons curves quoted in an ERC-20 (22% of launches): buying through one SPENDS that quote
    token, which would read as a sell of it. Never scored as a trade."""
    top = conn.execute("SELECT max(rowid) FROM tokens").fetchone()[0] or 0
    rows = conn.scan_ids(
        "SELECT json_extract(meta_json, '$.pair_token') FROM tokens NOT INDEXED WHERE rowid > ? AND rowid <= ? "
        "AND chain = 'robinhood' AND launchpad = 'pons'", (), 0, int(top), step=20_000,
    )
    return {str(r[0]).lower() for r in rows if r[0] and str(r[0]).lower() != ZERO}


def rh_pair_tokens(conn: Db, tokens: Iterable[str]) -> dict[str, str]:
    """``{token: quote}`` for Pons tokens (ZERO = native ETH); a primary-key read per token."""
    out = {}
    for t in tokens:
        row = conn.execute(
            "SELECT json_extract(meta_json, '$.pair_token') FROM tokens WHERE chain = 'robinhood' AND address = ?", (t,),
            cache=True,
        ).fetchone()
        if row and row[0]:
            out[t] = str(row[0]).lower()
    return out


def choose_pool(plist: Sequence[Mapping[str, Any]], counts: Mapping[str, int], quote: str = ZERO) -> str | None:
    """ONE v4 pool per token, so two phases can never stitch two quote units together: pools in
    the token's own quote (its Pons pair token, else native ETH) first, then the most swaps,
    then the earliest initialised."""
    if not plist:
        return None
    pref = [p for p in plist if p["quote"] == quote] or list(plist)
    pref.sort(key=lambda p: (-counts.get(p["pool"], 0), p["block"], p["pool"]))
    return pref[0]["pool"]


def trip_reads(
    trips: Iterable[Trip], cfg: Config, *, lo_ms: float, hi_ms: float, pre_ms: float = 10 * 60_000
) -> tuple[dict[str, list[tuple[float, float]]], dict[str, list[float]]]:
    """``(spans, instants)`` per token: the stretch of tape a copy of each trip can read -- from
    ``pre_ms`` before the buy to its exit (the leader's sell, the time stop, or the period end)
    plus the longest lag -- and the instants it reads a price at. Far shorter than a fixed
    24 h span per leg: most memecoin round trips last minutes."""
    pad = max(cfg.lags_ms) + 60_000
    raw: dict[str, list[tuple[float, float]]] = defaultdict(list)
    instants: dict[str, list[float]] = defaultdict(list)
    for t in trips:
        end = t.buy_ms + (cfg.max_hold_ms or 0) if cfg.max_hold_ms else hi_ms
        if t.sell_ms is not None:
            end = min(end, t.sell_ms)
        end = min(end + pad, hi_ms)
        a = max(lo_ms, t.buy_ms - pre_ms)
        if a <= end:
            raw[t.token].append((a, end))
        instants[t.token] += [t.buy_ms, end - pad if end < hi_ms else hi_ms]
    spans: dict[str, list[tuple[float, float]]] = {}
    for token, ivs in raw.items():
        ivs.sort()
        merged = [list(ivs[0])]
        for a, b in ivs[1:]:
            if a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        spans[token] = [(a, b) for a, b in merged]
    return spans, dict(instants)


def candidate_pools(plist: Sequence[Mapping[str, Any]], quote: str, *, keep: int = 3, base_asset: int = 20) -> list[Mapping[str, Any]]:
    """At most ``keep`` v4 pools per token, in its own quote (else native ETH) first, earliest
    initialised first. A token that is a currency in more than ``base_asset`` pools is a base
    asset (a stable, a wrapped native, a stock token), not a copy target: none."""
    if not plist or len(plist) > base_asset:
        return []
    pref = [x for x in plist if x["quote"] == quote] or [x for x in plist if x["quote"] == ZERO] or list(plist)
    return sorted(pref, key=lambda x: (x["block"], x["pool"]))[:keep]


def _rh_prints(
    conn: Db, src: EvmSource, clock: BlockClock, trips: Iterable[Trip], *, lo_ms: float,
    hi_ms: float, cfg: Config, chosen: dict[str, str], log: Callable[[str], None],
) -> dict[str, list[tuple[float, int, float, bool]]]:
    """Curve prints (tape) + v4 prints (RPC) for every token in ``trips``, over what a copy of
    them can read (:func:`trip_reads`). ``chosen`` pins each token's v4 pool across calls."""
    spans, instants_t = trip_reads(trips, cfg, lo_ms=lo_ms, hi_ms=hi_ms)
    prints: dict[str, list[tuple[float, int, float, bool]]] = defaultdict(list)
    for token, ivs in spans.items():
        for a, b in ivs:
            prints[token].extend(db_curve_prints(conn, "robinhood", token, a, b, clock=clock))
    log(f"[rh] curve prints for {sum(1 for v in prints.values() if v)} of {len(spans)} tokens")
    all_pools = src.v4_pools(list(spans), clock.block(hi_ms))
    quotes = rh_pair_tokens(conn, spans)
    pools = {t: candidate_pools(pl, quotes.get(t, ZERO)) for t, pl in all_pools.items()}
    base_assets = sum(1 for t, pl in all_pools.items() if pl and not pools[t])
    # A pool has no swaps before it was initialised (a Pons token is on its curve until then).
    segments = [(p["pool"], max(clock.block(a), int(p["block"])), clock.block(b)) for token, plist in pools.items()
                for p in plist if chosen.get(token) in (None, p["pool"]) for a, b in spans.get(token, [])
                if clock.block(b) >= int(p["block"])]
    lo_b, hi_b = clock.block(lo_ms), clock.block(hi_ms)
    instants = {p["pool"]: [clock.block(x) for x in instants_t.get(token, []) if lo_ms <= x <= hi_ms] + [lo_b, hi_b]
                for token, plist in pools.items() for p in plist}
    log(f"[rh] v4 pools for {sum(1 for v in pools.values() if v)} tokens ({base_assets} base assets skipped); "
        f"{len(segments)} segments")
    swaps, hot = src.v4_swaps(segments, instants, log=log)
    for token, plist in pools.items():
        if token not in chosen:
            pick = choose_pool(plist, {p: len(v) + (10**9 if p in hot else 0) for p, v in swaps.items()},
                               quotes.get(token, ZERO))
            if pick is None:
                continue
            chosen[token] = pick
        p = next((x for x in plist if x["pool"] == chosen[token]), None)
        if p is None:
            continue
        end = curve_end_of(prints[token])
        for block, seq, p1per0 in swaps.get(p["pool"], []):
            t = clock(block)
            if p1per0 > 0 and t > end and not at_bound_high(p1per0, p["token_is0"]):
                prints[token].append((t, seq, p1per0 if p["token_is0"] else 1.0 / p1per0, False))
    return prints


def at_bound_high(p1per0: float, token_is0: bool) -> bool:
    """A swap into a v4 pool with no in-range liquidity ends at the tick bound (sqrtP at
    MIN/MAX_SQRT_PRICE, token1/token0 ~1e-38 or ~1e+38; a live Pons pool sits near 1e6-1e9).
    In the direction that makes the TOKEN look astronomically valuable that print is not a
    price anyone could sell at, so it is dropped. The other bound -- a sell into a drained
    pool, the token at ~0 -- is kept: that one is the honest -100%."""
    return (p1per0 > 1e30) if token_is0 else (p1per0 < 1e-30)


def curve_end_of(rows: Sequence[Sequence[Any]]) -> float:
    """Time of the last gap-prone (curve) print; v4 prints only count after it."""
    return max((r[0] for r in rows if len(r) > 3 and r[3]), default=-math.inf)


def run_robinhood(
    conn: Db,
    seeds: Sequence[str],
    cfg: Config,
    *,
    rpc: Rpc,
    lo_ms: float | None = None,
    hi_ms: float | None = None,
    split_ms: float | None = None,
    max_candidates: int = 150,
    n_baseline: int = 150,
    ignore_tokens: Iterable[str] = (),
    blocks: tuple[int | None, int | None, int | None] = (None, None, None),
    gmgn: Gmgn | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """``blocks`` = (lo, split, hi) block numbers; each one given overrides the ms argument, so
    a rerun hits the same RPC cache keys."""
    chain = "robinhood"
    clock = rh_clock(conn, chain, at_block=None if blocks[2] is None else int(blocks[2]) - 20_000)
    if blocks[0] is not None:
        lo_ms = clock(blocks[0])
    if blocks[1] is not None:
        split_ms = clock(blocks[1])
    if blocks[2] is not None:
        hi_ms = clock(blocks[2])
    head = rpc.many([("eth_blockNumber", [])], cache=False)[0]
    head_b = int(head, 16) if isinstance(head, str) else clock.block(time.time() * 1000)
    if hi_ms is None:
        hi_ms = clock(head_b - 50)
    if lo_ms is None:
        first = conn.execute(
            "SELECT slot FROM swaps NOT INDEXED WHERE chain = ? AND slot IS NOT NULL ORDER BY id LIMIT 1", (chain,)
        ).fetchone()
        lo_ms = clock(int(first[0])) if first else hi_ms - 10 * DAY_MS
    lo_b, hi_b = clock.block(lo_ms), clock.block(hi_ms)
    seeds = [s.lower() for s in seeds]
    ignore = rh_quote_tokens(conn) | {t.lower() for t in ignore_tokens}
    src = EvmSource(rpc, clock)
    funnel: dict[str, Any] = {"ignored_quote_tokens": len(ignore)}

    def legs_for(wallets: Sequence[str]) -> tuple[dict[str, list[Leg]], dict[str, dict[str, int]]]:
        hist, busy, missing = src.histories(wallets, lo_b, hi_b)
        legs, counts = {}, {}
        for w in wallets:
            legs[w], counts[w] = legs_from_transfers(w, hist.get(w, []), clock=clock, ignore_tokens=ignore)
            if w in busy:
                counts[w] = {**counts[w], "too_active": 1}
            if w in missing:
                counts[w] = {**counts[w], "unavailable": 1}
        return legs, counts

    log(f"[rh] window blocks {lo_b}..{hi_b}; seeds {len(seeds)}")
    seed_legs, seed_counts = legs_for(seeds)
    seed_first = [leg for w in seeds for leg in first_buys(seed_legs[w]).values()]
    if split_ms is None:
        ts = sorted(leg.t_ms for leg in seed_first)
        split_ms = ts[len(ts) // 2] if ts else (lo_ms + hi_ms) / 2
    early_first = [leg for leg in seed_first if lo_ms <= leg.t_ms < split_ms]
    log(f"[rh] split at block {clock.block(split_ms)}; seed first buys {len(seed_first)} ({len(early_first)} earlier)")

    # ---- co-buy windows around each EARLIER-period seed first buy (block-rounded to 50)
    before_b = int(math.ceil(cfg.cobuy_before_ms / BLOCK_MS[chain] / 50.0)) * 50
    after_b = int(math.ceil(cfg.cobuy_near_ms / BLOCK_MS[chain] / 50.0)) * 50
    spans: dict[str, list[list[int]]] = {}
    for leg in sorted(early_first, key=lambda x: x.key):
        b = clock.block(leg.t_ms)
        lst = spans.setdefault(leg.token, [])
        if lst and b - before_b <= lst[-1][1]:
            lst[-1][1] = max(lst[-1][1], b + after_b)
        else:
            lst.append([b - before_b, b + after_b])
    windows = [(t, a, b) for t, v in spans.items() for a, b in v]
    buyers: dict[str, list[Leg]] = defaultdict(list)
    for (t, _a, _b), logs in src.window_logs(windows).items():
        buyers[t].extend(buyers_from_token_logs(t, logs, clock=clock, exclude=(src.pm,)))
    stats = cobuy_scan(early_first, buyers, cfg, exclude=seeds)
    cands, f = pick_candidates(stats, cfg)
    funnel.update({"windows": len(windows), **f})
    cands, clusters, members = cluster_candidates(cands, buyers, cfg, max_candidates)
    funnel["candidate_operators"] = len(members)
    funnel["representatives_scored"] = len(cands)
    log(f"[rh] co-buy: {json.dumps(f)}; {len(members)} operators, scoring {len(cands)} representatives")

    # ---- history-based exclusions (activity cap, routers); contracts after selection below
    cand_set = {c.wallet for c in cands}
    pool = [w for w, cb in stats.items() if cb.copier_share < cfg.copier_share and w not in cand_set]
    base_ws = _draw(pool, n_baseline, cfg.seed)
    cand_ws = [c.wallet for c in cands]
    cand_legs, cand_counts = legs_for(cand_ws)
    base_legs, base_counts = legs_for(base_ws)

    def usable(legs: Sequence[Leg], ct: Mapping[str, int]) -> str | None:
        if ct.get("unavailable"):
            return "unavailable"
        if ct.get("too_active") or len(legs) > cfg.max_legs:
            return "too_active"
        if ct.get("pass_through", 0) > ct.get("buy", 0) + ct.get("sell", 0):
            return "router"
        if not any(leg.side == "buy" for leg in legs):
            return "no_buys"
        return None

    drop: Counter = Counter()
    keep_c = []
    for w in cand_ws:
        why = usable(cand_legs[w], cand_counts[w])
        drop[why or "kept"] += 1
        if why is None:
            keep_c.append(w)
    funnel["history"] = dict(drop)
    keep_b = [w for w in base_ws if usable(base_legs[w], base_counts[w]) is None]
    funnel["baseline_drawn"], funnel["baseline_usable"] = len(base_ws), len(keep_b)

    # ---- phase 1: EARLIER-period prices only (nothing after the split is fetched or read)
    every = {**{w: seed_legs[w] for w in seeds}, **{w: cand_legs[w] for w in keep_c}, **{w: base_legs[w] for w in keep_b}}
    early_trips = [t for ls in every.values() for t in episodes(ls) if lo_ms <= t.buy_ms < split_ms]
    chosen: dict[str, str] = {}
    p_in = _rh_prints(conn, src, clock, early_trips, lo_ms=lo_ms, hi_ms=split_ms, cfg=cfg, chosen=chosen, log=log)
    dark = db_dark_gaps(conn, chain, lo_b, hi_b, clock=clock)
    tape_in = PriceTape(p_in, continuous=True, dark=dark)
    s_seed = {w: score_in(w, seed_legs[w], tape_in, lo_ms=lo_ms, split_ms=split_ms, cfg=cfg, role="seed") for w in seeds}
    s_cand = {w: score_in(w, cand_legs[w], tape_in, lo_ms=lo_ms, split_ms=split_ms, cfg=cfg) for w in keep_c}
    s_base = {w: score_in(w, base_legs[w], tape_in, lo_ms=lo_ms, split_ms=split_ms, cfg=cfg, role="baseline") for w in keep_b}

    # ---- contracts: checked only where it can change an answer (selected wallets), the same
    # way for candidates and baseline. EIP-7702 delegated EOAs are not contracts.
    picked = [w for w, s in {**s_cand, **s_base}.items() if s.selected]
    code = src.is_contract(picked)
    for w in [w for w in picked if code.get(w)]:
        (s_cand.get(w) or s_base[w]).selected = False
        (s_cand.get(w) or s_base[w]).notes["excluded"] = "contract"
    funnel["contracts_excluded_after_selection"] = sum(1 for w in picked if code.get(w))

    # ---- phase 2: LATER-period prices for the seeds and the SELECTED wallets only
    later_ws = seeds + [w for w, s in s_cand.items() if s.selected] + [w for w, s in s_base.items() if s.selected]
    late_trips = [t for w in later_ws for t in episodes(every[w]) if split_ms <= t.buy_ms < hi_ms]
    p_out = _rh_prints(conn, src, clock, late_trips, lo_ms=split_ms - HOUR_MS, hi_ms=hi_ms, cfg=cfg, chosen=chosen, log=log)
    tape = PriceTape(merge_tapes(p_in, p_out), continuous=True, dark=dark)
    for group, legs in ((s_seed, seed_legs), (s_cand, cand_legs), (s_base, base_legs)):
        for w, ws in group.items():
            if ws.role == "seed" or ws.selected:
                score_out(ws, legs[w], tape, split_ms=split_ms, hi_ms=hi_ms, cfg=cfg)
    for w, ws in s_cand.items():
        ws.notes["cobuy"] = stats[w].as_dict()
        cid = clusters.get(w, w)
        ws.notes["operator"] = {"id": cid, "size": len(members.get(cid, [w])), "members": members.get(cid, [w])[:25]}
    funnel["tokens_priced"] = len(tape.tokens())
    funnel["dark_gaps"] = len(dark)
    return _report(chain, cfg, funnel, list(s_seed.values()), list(s_cand.values()), list(s_base.values()),
                   window={"lo_ms": lo_ms, "split_ms": split_ms, "hi_ms": hi_ms, "lo_block": lo_b, "hi_block": hi_b,
                           "split_block": clock.block(split_ms)},
                   calls={"rpc_http": rpc.http, "rpc_queries": rpc.calls, "rpc_cached": rpc.cached,
                          "rpc_failed": rpc.failed, "rpc_429": rpc.r429},
                   gmgn=gmgn, extra={"seed_leg_counts": seed_counts})


def _sol_quote_assets() -> frozenset[str]:
    try:
        from kaiba.core.schemas import QUOTE_ASSETS, Chain
        return frozenset(QUOTE_ASSETS.get(Chain.SOL, frozenset()))
    except Exception:  # noqa: BLE001 - outside the tree the three canonical mints suffice
        return frozenset({"So11111111111111111111111111111111111111112",
                          "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                          "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"})


def sol_pool(conn: Db, *, rows: int = 1_500_000, max_tagged: int = 3_000, seed: int = 0,
             hi_id: int | None = None) -> dict[str, list[str]]:
    """Kaiba's existing sol candidates: the latest ``proven:sol`` members, A/B grades, and the
    GMGN smart-money / KOL wallets active in the newest ``rows`` swaps (primary-key range)."""
    out: dict[str, list[str]] = {}
    row = conn.execute(
        "SELECT cohort_id FROM wallet_cohort_freezes WHERE cohort_id LIKE 'proven:sol:%' ORDER BY frozen_ms DESC LIMIT 1"
    ).fetchone()
    out["proven"] = [r[0] for r in conn.execute(
        "SELECT address FROM wallet_cohorts WHERE cohort_id = ? AND arm = 'graded'", (row[0],))] if row else []
    out["graded_ab"] = [r[0] for r in conn.execute(
        "SELECT address FROM wallet_scores INDEXED BY idx_wallet_scores_grade WHERE grade IN ('A','B') AND chain = 'sol'")]
    hi = hi_id if hi_id is not None else conn.execute("SELECT max(id) FROM swaps").fetchone()[0]
    tagged = {r[0] for r in conn.scan_ids(
        "SELECT wallet FROM swaps NOT INDEXED WHERE id > ? AND id <= ? AND chain = 'sol' AND source IN ('gmgn:smartmoney','gmgn:kol')",
        (), int(hi) - int(rows), int(hi),
    ) if r[0]}
    out["gmgn_tagged"] = _draw(tagged, max_tagged, seed)
    return out


def run_sol(
    conn: Db,
    seeds: Sequence[str],
    cfg: Config,
    *,
    lo_ms: float | None = None,
    hi_ms: float | None = None,
    split_ms: float | None = None,
    max_candidates: int = 300,
    n_baseline: int = 300,
    top_seeds: int = 25,
    hi_id: int | None = None,
    gmgn: Gmgn | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Sol, database only. With no ``seeds`` the seeds are the best EARLIER-period copies among
    Kaiba's existing sol cohorts -- chosen without looking at the later period."""
    chain = "sol"
    q = _sol_quote_assets()
    if hi_id is None:
        hi_id = int(conn.execute("SELECT max(id) FROM swaps").fetchone()[0])
        log(f"[sol] pin --hi-id {hi_id} to resume this run from its cache")
    if hi_ms is None:
        hi_ms = float(max((r[0] or 0) for r in conn.scan_ids(
            "SELECT max(ts_ms) FROM swaps NOT INDEXED WHERE id > ? AND id <= ? AND chain = 'sol'", (),
            int(hi_id) - 200_000, int(hi_id), step=50_000)))
    if lo_ms is None:
        lo_ms = hi_ms - 10 * DAY_MS
    funnel: dict[str, Any] = {}
    pool = sol_pool(conn, seed=cfg.seed, hi_id=hi_id)
    funnel["pool"] = {k: len(v) for k, v in pool.items()}
    sources: dict[str, str] = {}
    for k in ("proven", "graded_ab", "gmgn_tagged"):
        for w in pool[k]:
            sources.setdefault(w, k)
    legs_cache: dict[str, list[Leg] | None] = {}

    def legs_of(w: str) -> list[Leg] | None:
        if w not in legs_cache:
            legs_cache[w] = db_wallet_legs(conn, chain, w, lo_ms, hi_ms, max_legs=cfg.max_legs, quote_assets=q)
        return legs_cache[w]

    pool_legs = {w: legs_of(w) for w in sources}
    if split_ms is None:
        ts = sorted(leg.t_ms for ls in pool_legs.values() for leg in (ls or []))
        split_ms = ts[len(ts) // 2] if ts else (lo_ms + hi_ms) / 2
    log(f"[sol] window {lo_ms:.0f}..{hi_ms:.0f}, split {split_ms:.0f}; pool {len(sources)}")

    prints: dict[str, dict[tuple[float, int], tuple[float, int, float]]] = defaultdict(dict)
    covered: dict[str, list[tuple[float, float]]] = defaultdict(list)
    pinned: dict[str, str | None] = {}
    price_src: Counter = Counter()
    post = max(cfg.lags_ms) + cfg.max_wait_ms

    seeked: set[tuple[str, int]] = set()

    def tape_for(legs: Iterable[Leg]) -> PriceTape:
        """Only the prints a copy of these legs can read, keeping each token on the ONE source it
        was first priced from: the next-print windows after every instant (each leg, each buy's
        time stop, the split, the end) and the last print at or before each instant (the exit
        fallback and the marks). A 24 h span per leg held millions of prints in memory."""
        instants = sparse_instants(legs, cfg, marks=(split_ms, hi_ms))
        for token, xs in instants.items():
            ivs = []
            for x in xs:
                a, b = max(lo_ms, x), min(hi_ms, x + post)
                if a <= b:
                    ivs.append((a, b))
            ivs.sort()
            merged: list[list[float]] = []
            for a, b in ivs:
                if merged and a <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b])
            todo = [(a, b) for a, b in merged if not any(ca <= a and b <= cb for ca, cb in covered[token])]
            if todo:
                pr, src = db_sparse_prints(conn, chain, token, todo, source=pinned.get(token))
                if pinned.get(token) is None and src is not None:
                    pinned[token] = src
                    price_src[src] += 1
                for row in thin_prints(pr, xs, (0, *cfg.lags_ms), cfg.max_wait_ms):
                    prints[token][(row[0], row[1])] = row
                covered[token].extend(todo)
            src = pinned.get(token)
            if src is None:
                continue
            for x in xs:
                key = (token, int(x // 1000))
                if key in seeked:
                    continue
                seeked.add(key)
                row = db_last_print(conn, chain, token, x, source=src)
                if row is not None:
                    prints[token][(row[0], row[1])] = row
        return PriceTape({t: list(v.values()) for t, v in prints.items()}, continuous=False, max_wait_ms=cfg.max_wait_ms)

    def score_group(ws: Iterable[str], role: str) -> dict[str, WalletScore]:
        legs = {w: legs_of(w) for w in ws}
        legs = {w: v for w, v in legs.items() if v}
        tape = tape_for(leg for v in legs.values() for leg in v)
        out = {}
        for w, v in legs.items():
            s = score_in(w, v, tape, lo_ms=lo_ms, split_ms=split_ms, cfg=cfg, role=role)
            if s.selected or role == "seed":
                score_out(s, v, tape, split_ms=split_ms, hi_ms=hi_ms, cfg=cfg)
            out[w] = s
        return out

    funnel["pool_too_active_or_empty"] = sum(1 for v in pool_legs.values() if not v)
    if seeds:
        seed_list = list(seeds)
        funnel["seed_basis"] = "caller"
    else:
        # Seeds chosen on the EARLIER period only: the pool's best copied means there.
        tape_early = tape_for(leg for v in pool_legs.values() if v for leg in v if leg.t_ms < split_ms)
        ranked = []
        for w, v in pool_legs.items():
            if v:
                s = score_in(w, v, tape_early, lo_ms=lo_ms, split_ms=split_ms, cfg=cfg, role="pool")
                if s.selected:
                    ranked.append(s)
        ranked.sort(key=lambda s: (-(s.ins[cfg.grade_lag_ms]["mean"] or 0.0), s.wallet))
        seed_list = [s.wallet for s in ranked[:top_seeds]]
        funnel["pool_selected"] = len(ranked)
        log(f"[sol] pool scored in sample: {len(ranked)} selected of {sum(1 for v in pool_legs.values() if v)}")
        funnel["seed_basis"] = f"top {len(seed_list)} earlier-period copied means in the pool"
    seed_first = [leg for w in seed_list for leg in first_buys(legs_of(w) or []).values() if lo_ms <= leg.t_ms < split_ms]
    buyers: dict[str, list[Leg]] = defaultdict(list)
    for leg in seed_first:
        buyers[leg.token].extend(db_token_buyers(conn, chain, leg.token, leg.t_ms - cfg.cobuy_before_ms,
                                                 leg.t_ms + cfg.cobuy_near_ms))
    stats = cobuy_scan(seed_first, buyers, cfg, exclude=seed_list)
    cands, f = pick_candidates(stats, cfg)
    funnel.update({"seed_first_buys_earlier": len(seed_first), **f})
    cands, clusters, members = cluster_candidates(cands, buyers, cfg, max_candidates)
    funnel["candidate_operators"] = len(members)
    cand_ws = [c.wallet for c in cands]
    funnel["cobuy_representatives_scored"] = len(cand_ws)
    log(f"[sol] co-buy: {json.dumps(f)}; {len(members)} operators, {len(cand_ws)} representatives")
    # Kaiba's own cohort members are candidates too: they were already somebody's pick.
    cand_ws += [w for w in sources if w not in set(seed_list) | set(cand_ws)]
    s_seed = score_group(seed_list, "seed")
    s_cand = score_group(cand_ws, "candidate")
    log(f"[sol] scored {len(s_cand)} candidates; {sum(1 for x in s_cand.values() if x.selected)} selected")
    for w, s in s_cand.items():
        if w in stats:
            s.notes["cobuy"] = stats[w].as_dict()
            cid = clusters.get(w, w)
            s.notes["operator"] = {"id": cid, "size": len(members.get(cid, [w])), "members": members.get(cid, [w])[:25]}
        s.notes["source"] = sources.get(w, "cobuy")
    taken = set(cand_ws) | set(seed_list)
    act: Counter = Counter()
    for (w,) in conn.scan_ids("SELECT wallet FROM swaps NOT INDEXED WHERE id > ? AND id <= ? AND chain = 'sol' AND token != ''",
                              (), int(hi_id) - 1_500_000, int(hi_id)):
        act[w] += 1
    base_pool = [w for w, n in act.items() if w and 4 <= n <= 3_000 and w not in taken]
    s_base = score_group(_draw(base_pool, n_baseline, cfg.seed), "baseline")
    funnel["price_sources"] = {str(k): v for k, v in price_src.items()}
    return _report(chain, cfg, funnel, list(s_seed.values()), list(s_cand.values()), list(s_base.values()),
                   window={"lo_ms": lo_ms, "split_ms": split_ms, "hi_ms": hi_ms}, calls={"rpc_http": 0}, gmgn=gmgn)


def _report(
    chain: str, cfg: Config, funnel: dict[str, Any], seeds: Sequence[WalletScore], cands: Sequence[WalletScore],
    base: Sequence[WalletScore], *, window: dict[str, Any], calls: dict[str, Any], gmgn: Gmgn | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sel = [c for c in cands if c.selected]
    graded = sorted([c for c in sel if c.grade], key=lambda s: _rank_key(s, cfg))
    funnel.update({
        "scored": len(cands),
        "eligible_in": sum(1 for c in cands if c.ins[cfg.grade_lag_ms]["n"] >= cfg.min_trips_in),
        "selected_in": len(sel),
        "gradeable_out": sum(1 for c in sel if c.outs.get(cfg.grade_lag_ms, {}).get("n", 0) >= cfg.min_n),
        "A": sum(1 for c in graded if c.grade == "A"),
        "B": sum(1 for c in graded if c.grade == "B"),
    })
    base_m = _activity_matched(base, sel) if sel else list(base)
    if gmgn is not None:
        for c in graded:
            st = gmgn.stats(c.wallet, chain)
            if st:
                c.notes["gmgn_30d"] = {k: st.get(k) for k in ("realized_profit", "pnl", "winrate", "buy", "sell",
                                                             "total_cost", "token_num") if k in st}
        calls["gmgn"] = gmgn.calls
    return {
        "chain": chain,
        "generated_ms": int(time.time() * 1000),
        "config": cfg.as_dict(),
        "window": window,
        "funnel": funnel,
        "graded": [c.as_dict() for c in graded],
        "seeds": [s.as_dict() for s in seeds],
        "candidates_group": summarize_group(cands, cfg),
        "baseline_group": summarize_group(base_m, cfg),
        "baseline_all_group": summarize_group(base, cfg),
        "exit_rule_sensitivity": {
            rule: {"candidates": summarize_group(cands, cfg, rule), "baseline": summarize_group(base_m, cfg, rule),
                   "baseline_all": summarize_group(base, cfg, rule), "seeds": summarize_group(seeds, cfg, rule)}
            for rule in EXIT_RULES
        },
        "candidates": [c.as_dict() for c in sorted(cands, key=lambda s: _rank_key(s, cfg))],
        "baseline": [b.as_dict() for b in base],
        "calls": calls,
        **(extra or {}),
    }


def render(rep: Mapping[str, Any]) -> str:
    """A short text table for the terminal."""
    lines = [f"lookalike [{rep['chain']}]  funnel: {json.dumps(rep['funnel'])}"]

    def row(d: Mapping[str, Any]) -> str:
        cells = []
        for k in ("1s", "5s", "15s"):
            s = d["out_of_sample"].get(k) or {}
            cells.append(f"{k} {s.get('mean_pct')}% n={s.get('n')} win={s.get('win_pct')}% ci={s.get('ci90_pct')}")
        in5 = d["in_sample"].get("5s", {})
        return f"  {d['wallet']} {d['grade'] or '-'} sel={d['selected']} in5s={in5.get('mean_pct')}%/n={in5.get('n')} | " + " | ".join(cells)

    lines.append("graded:")
    lines += [row(d) for d in rep["graded"]] or ["  (none)"]
    lines.append("seeds:")
    lines += [row(d) for d in rep["seeds"]]
    lines.append(f"candidates group: {json.dumps(rep['candidates_group'])}")
    lines.append(f"baseline (activity-matched): {json.dumps(rep['baseline_group'])}")
    lines.append(f"calls: {json.dumps(rep['calls'])}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Lookalike wallets, scored by out-of-sample copied return.")
    ap.add_argument("--db", required=True)
    ap.add_argument("--chain", required=True, choices=("robinhood", "sol"))
    ap.add_argument("--seeds", default="", help="comma-separated seed wallets")
    ap.add_argument("--seeds-file", default=None)
    ap.add_argument("--json", nargs="?", const="-", default=None, help="write the JSON report to a file, or - for stdout")
    ap.add_argument("--cache", default=None, help="RPC cache directory (robinhood)")
    ap.add_argument("--lo-ms", type=float, default=None)
    ap.add_argument("--hi-ms", type=float, default=None)
    ap.add_argument("--split-ms", type=float, default=None)
    ap.add_argument("--blocks", default=None, help="robinhood: LO,SPLIT,HI block numbers (any may be empty)")
    ap.add_argument("--max-candidates", type=int, default=None)
    ap.add_argument("--baseline", type=int, default=None)
    ap.add_argument("--slip", type=float, default=None, help="slippage haircut per leg (fraction)")
    ap.add_argument("--rpc-gap-s", type=float, default=6.0)
    ap.add_argument("--rpc-max-http", type=int, default=3_000)
    ap.add_argument("--gmgn-budget", type=int, default=0, help="GMGN stats calls for graded wallets (annotation only)")
    ap.add_argument("--hi-id", type=int, default=None, help="sol: pin the newest swaps id (resume a run from its cache)")
    ap.add_argument("--max-query-s", type=float, default=30.0, help="interrupt any one SQL statement after this")
    ap.add_argument("--min-pause-s", type=float, default=0.05,
                    help="rest after every SQL statement (the checkpointer needs reader-free instants)")
    args = ap.parse_args(argv)

    seeds = [s.strip() for s in args.seeds.split(",") if s.strip()]
    if args.seeds_file:
        with open(args.seeds_file) as fh:
            seeds += [s.strip() for s in fh if s.strip() and not s.startswith("#")]
    cfg = Config() if args.slip is None else Config(slip_per_leg=args.slip)
    def log(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    conn = connect_ro(args.db, cache_dir=args.cache, max_query_s=args.max_query_s, min_pause_s=args.min_pause_s, log=log)

    def health() -> tuple[bool, str]:
        return protection_health(conn)

    gm = Gmgn(budget=args.gmgn_budget, health=health, log=log) if args.gmgn_budget > 0 else None
    if args.chain == "robinhood":
        if not seeds:
            ap.error("robinhood needs --seeds")
        from kaiba.core.config import get_settings
        from kaiba.core.schemas import Chain

        rpc = Rpc(get_settings().rpc_for(Chain.ROBINHOOD), cache_dir=args.cache, gap_s=args.rpc_gap_s,
                  health=health, max_http=args.rpc_max_http, log=log)
        blocks = tuple((int(x) if x.strip() else None) for x in (args.blocks or ",,").split(","))
        rep = run_robinhood(conn, seeds, cfg, rpc=rpc, lo_ms=args.lo_ms, hi_ms=args.hi_ms, split_ms=args.split_ms,
                            blocks=blocks,  # type: ignore[arg-type]
                            max_candidates=args.max_candidates or 150, n_baseline=args.baseline or 150, gmgn=gm, log=log)
    else:
        rep = run_sol(conn, seeds, cfg, lo_ms=args.lo_ms, hi_ms=args.hi_ms, split_ms=args.split_ms, hi_id=args.hi_id,
                      max_candidates=args.max_candidates or 300, n_baseline=args.baseline or 300, gmgn=gm, log=log)
    rep.setdefault("calls", {})["db"] = {"queries": conn.queries, "cached": conn.cached,
                                          "interrupted": conn.interrupted, "wal_waits": conn.wal_waits}
    print(render(rep))
    if args.json:
        text = json.dumps(rep, indent=1, default=str)
        if args.json == "-":
            print(text)
        else:
            with open(args.json, "w") as fh:
                fh.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
