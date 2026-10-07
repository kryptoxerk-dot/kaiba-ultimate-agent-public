"""Does the owner's four-factor confluence predict a profitable ENTRY, net of our real exits?

The owner's ask (2026-10-04): "Good wallets, Good dips, Good volume, Good narrative. All of
that will be confluence." This module measures each factor alone, every pair, triple and the
4-way AND, on the SCANNED population (every token on the swap tape, not only the ones we
bought), as a realised return through the live exit ladder net of costs.

Every rule below exists because an earlier study in this package fooled itself:

**1. Point in time.** A decision is taken at checkpoint ``t`` (a fixed grid after the
token's first print). Every feature is read through :func:`upto` -- prints with
``ts <= t`` and nothing else -- and the entry is the first price-source print strictly after
``t + LATENCY_S``. ``tests/test_mooner_confluence.py`` fails if a feature can see a print
after ``t``. (entry_study's "more unique wallets -> 2.3x" was reverse causation.)

**2. One price source per decision.** ``swaps`` mixes feeds that disagree (6.3% of sol
same-token prints within 60 s differ by >2x). Price features and the outcome are read from
ONE source: the one :func:`kaiba.intelligence.deployer.series_source`'s rule picks over the
prints seen AT ``t`` (not over the whole tape, which would let the future choose).

**3. Realised, not reached.** The outcome is the net return of entering at ``t`` and exiting
through a float replica of :func:`kaiba.execution.protection.evaluate` driven at the box's
``protection`` block (12 s poll, -30% stop, TP ladder of remaining, trailing tiers, moon bag,
breakeven after TP1, -50% emergency, 1 h stale exit), charged :data:`LEG_COST` per leg. The
rug monitor and anti-wick need liquidity and executable quotes the tape does not carry, so
they are NOT modelled; both only ever exit EARLIER (rug) or hold a rung (anti-wick). Max
multiple within 6 h is reported beside it as the secondary.

**4. Unpriced is unknown.** A stale exit whose silence another feed contradicts (the token
printed elsewhere while our series was silent: the pump.fun tape is fetched per scan and
stops when we stop fetching) is ``unpriced_gap``, not a loss. An entry whose 6 h horizon runs
past the data, or (train) past the split, is censored/purged, never zero.

**5. Volume is stratified by print count.** More prints are more chances to print a high
(the reach-N confound). Volume flags compare buy volume against the TRAIN quantiles of
checkpoints with the SAME print-count band, so "high volume" can never just mean "busy".

**6. Wallet quality is precision on train only.** A wallet is good when, over TRAIN tokens
whose 6 h outcome closed before the split, its first buys were followed by a 2x at least
:data:`WALLET_PRECISION_LIFT` times the base rate, on at least :data:`WALLET_MIN_TOKENS`
tokens. On train it is leave-one-token-out. Count is never the measure (count is
anti-calibrated on our own fills).

**7. Every configuration is counted.** Thresholds are chosen on the earlier 60% of tokens by
launch time, and the later 40% is reported untouched. The number of configurations tried
travels with the result, and the holdout winner is deflated against it.
"""

from __future__ import annotations

import array
import bisect
import collections
import itertools
import json
import math
import os
import random
import re
import sys
import time
import zlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------------------
# constants, each with its provenance
# --------------------------------------------------------------------------------------

#: One leg's cost as a fraction. MEASURED by the lead on live fills: ~6.5% round trip.
#: Applied multiplicatively on both legs: net = (1-c)^2 * gross_multiple - 1.
LEG_COST: float = 0.0325
#: PaperBroker's floor for a sensitivity line (GMGN 1% + venue fee + 30 bps slippage floor).
LEG_COST_FLOOR: dict[str, float] = {"sol": 0.0255, "robinhood": 0.019, "bsc": 0.019}

#: From the box's config/risk.yaml ``protection.poll_interval_s`` (read 2026-10-04).
POLL_S: int = 12
#: Decision -> first eligible entry print. INVENTED: execute_planned runs every 5 s, plus submit.
LATENCY_S: int = 10
#: Positions still open after this are closed at the last price. The secondary outcome
#: (max multiple) uses the same window.
HORIZON_S: int = 6 * 3600

#: Decision grid, seconds after a token's first print (any source).
CHECKPOINTS_S: tuple[int, ...] = tuple(range(60, 1801, 60)) + tuple(range(2100, 6 * 3600 + 1, 300))
#: A checkpoint is active only if its price source printed within this long before it.
ACTIVE_S: int = 600
VOL_S: int = 300
WALLET_S: int = 1800
HEAT_S: int = 86_400
HEAT_MULTIPLE: float = 3.0

#: Print-count bands of the VOL_S window: [1,5), [5,15), [15,50), [50,inf).
PRINT_BAND_EDGES: tuple[int, ...] = (5, 15, 50)

DIP_RUNUP: tuple[float, ...] = (1.5, 2.0, 3.0)
DIP_RETRACE: tuple[float, ...] = (0.20, 0.35, 0.50)
#: A retrace deeper than this is a collapse, not a dip. INVENTED, fixed, never tuned.
DIP_MAX_RETRACE: float = 0.80
#: "Volume held": retrace-window buy USD per minute >= this share of the run window's, and
#: the retrace window's sell share of prints <= HELD_SELL_SHARE. INVENTED, fixed.
HELD_RATE: float = 0.5
HELD_SELL_SHARE: float = 0.6

WALLET_HIT_MULTIPLE: float = 2.0
WALLET_MIN_TOKENS: int = 8
WALLET_PRECISION_LIFT: float = 2.0

MIN_CELL: int = 30
TRAIN_FRAC: float = 0.6
BOOTSTRAP_DRAWS: int = 4_000
BOOTSTRAP_SEED: int = 20_261_004

SERIES_SOURCE_PRIORITY: dict[str, tuple[str, ...]] = {
    "sol": ("pumpfun:trades", "alchemy:ws", "helius:backfill", "gmgn:smartmoney", "gmgn:kol"),
    "robinhood": ("robinhood", "alchemy:ws", "gmgn:smartmoney", "gmgn:kol"),
    "bsc": ("gmgn:smartmoney", "gmgn:kol"),
}
SMART_SOURCE = "gmgn:smartmoney"

#: Sources a PRICE may be read from, per chain. MEASURED 2026-10-04 on this extract: the
#: largest simulated "winners" were single gmgn feed prints 8-23x off the on-chain tape (an RH
#: token with three gmgn prints "exited" at 23x on a stale rule). These are the on-chain
#: tapes. bsc has no on-chain tape on the box, so its prices are feed prices and every bsc
#: number carries that caveat. Wallet labels still read every source.
PRICE_SOURCES: dict[str, frozenset[str] | None] = {
    "sol": frozenset({"pumpfun:trades", "alchemy:ws", "helius:backfill"}),
    "robinhood": frozenset({"robinhood", "alchemy:ws"}),
    "bsc": None,
}

#: A series gap longer than this, with the position open, across which ANOTHER feed printed
#: the token, is a coverage hole (the pump.fun tape is paged per scan): the path inside it is
#: unknown, so the outcome is unpriced rather than jumped across.
GAP_S: int = 300
CHAIN_NAMES = ("sol", "robinhood", "bsc")

# Same tokenizer and stopwords as kaiba.learning.mooner (copied, not imported, so the
# worker processes do not pull kaiba.core into every spawn).
_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "of", "and", "on", "in", "to", "for", "is", "it", "by", "with",
    "coin", "token", "inu", "meme", "official", "community", "finance", "protocol",
})
_WORD = re.compile(r"[a-z0-9]+")


def words_of(symbol: str | None, name: str | None) -> tuple[str, ...]:
    text = " ".join(x for x in (symbol, name) if x).lower()
    return tuple(sorted({w for w in _WORD.findall(text) if len(w) > 2 and w not in _STOPWORDS}))


def series_source(counts: Mapping[str, int], chain: str) -> str | None:
    """The deployer.series_source rule: most priced prints, ties by chain priority, then name."""
    live = {str(s): int(n) for s, n in counts.items() if s is not None and int(n) > 0}
    if not live:
        return None
    order = SERIES_SOURCE_PRIORITY.get(str(chain), ())
    return min(live, key=lambda s: (-live[s], order.index(s) if s in order else len(order), s))


# --------------------------------------------------------------------------------------
# factor flags: bit layout
# --------------------------------------------------------------------------------------

VOL_FLAGS = ("V_q50", "V_q75", "V_q90", "V_accel2", "V_bsr60", "V_breadth_q75")
WAL_FLAGS = ("W_good1", "W_good2", "W_gmgnsmart1", "W_gmgnsmart2", "W_trusted1", "W_gradeAB1_LOOKAHEAD")
DIP_FLAGS = tuple(
    f"D_r{r:g}_d{int(d * 100)}{'_held' if held else ''}"
    for held in (False, True) for r in DIP_RUNUP for d in DIP_RETRACE
)
NAR_FLAGS = ("N_hits1", "N_hits3", "N_hits10", "N_rate10")
FACTORS: dict[str, tuple[str, ...]] = {
    "volume": VOL_FLAGS, "wallets": WAL_FLAGS, "dip": DIP_FLAGS, "narrative": NAR_FLAGS,
}
FLAG_NAMES: tuple[str, ...] = VOL_FLAGS + WAL_FLAGS + DIP_FLAGS + NAR_FLAGS
BIT: dict[str, int] = {name: 1 << i for i, name in enumerate(FLAG_NAMES)}
FACTOR_OF: dict[str, str] = {f: fac for fac, names in FACTORS.items() for f in names}


def rule_mask(flags: Iterable[str]) -> int:
    m = 0
    for f in flags:
        m |= BIT[f]
    return m


def rule_name(mask: int) -> str:
    if mask == 0:
        return "BASELINE_first_active"
    return "+".join(f for f in FLAG_NAMES if mask & BIT[f])


def band_of(prints: int) -> int:
    return bisect.bisect_right(PRINT_BAND_EDGES, prints)


# --------------------------------------------------------------------------------------
# point-in-time views
# --------------------------------------------------------------------------------------


@dataclass
class Series:
    """One source's priced prints for one token, oldest first, with prefix sums."""

    source: str
    ts: list[int]
    px: list[float]
    side: list[int]
    usd: list[float]
    wallet: list[int]
    cum_buy_usd: list[float] = field(default_factory=list)
    cum_buys: list[int] = field(default_factory=list)
    cum_sells: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        cu, cb, cs = [0.0], [0], [0]
        for s, u in zip(self.side, self.usd):
            cu.append(cu[-1] + (u if (s == 1 and u == u) else 0.0))
            cb.append(cb[-1] + (1 if s == 1 else 0))
            cs.append(cs[-1] + (1 if s == -1 else 0))
        self.cum_buy_usd, self.cum_buys, self.cum_sells = cu, cb, cs


def upto(ts: Sequence[int], t: int) -> int:
    """Number of prints at or before ``t``. THE lookahead guard: every feature slices by it."""
    return bisect.bisect_right(ts, t)


@dataclass
class TokenTape:
    """Every print of one token (all sources), plus its per-source priced series."""

    chain: str
    token: str
    ts: list[int]
    side: list[int]
    wallet: list[int]
    source: list[str]
    series: dict[str, Series]
    words: tuple[str, ...] = ()
    launchpad: str | None = None

    @property
    def t0(self) -> int:
        return self.ts[0]

    @classmethod
    def from_rows(
        cls, chain: str, token: str, rows: Iterable[tuple[int, float, float, int, int, str]],
        *, words: tuple[str, ...] = (), launchpad: str | None = None,
    ) -> TokenTape:
        """``rows`` = (ts_ms, price_usd or nan, usd_value or nan, side +1/-1/0, wallet_id, source)."""
        rows = sorted(rows, key=lambda r: r[0])
        allowed = PRICE_SOURCES.get(chain)
        per: dict[str, list[tuple]] = collections.defaultdict(list)
        for r in rows:
            p = r[1]
            if p == p and p > 0 and math.isfinite(p) and (allowed is None or r[5] in allowed):
                per[r[5]].append(r)
        series = {
            s: Series(s, [r[0] for r in rs], [r[1] for r in rs], [r[3] for r in rs],
                      [r[2] for r in rs], [r[4] for r in rs])
            for s, rs in per.items()
        }
        return cls(chain, token, [r[0] for r in rows], [r[3] for r in rows],
                   [r[4] for r in rows], [r[5] for r in rows], series, words, launchpad)

    def source_at(self, t: int) -> str | None:
        """The series source by priced-print count AT ``t`` (point in time)."""
        counts = {s: upto(ser.ts, t) for s, ser in self.series.items()}
        return series_source(counts, self.chain)

    def other_source_ts(self, source: str) -> list[int]:
        return [t for t, s in zip(self.ts, self.source) if s != source]


@dataclass
class Features:
    """Raw factor inputs at one checkpoint. Built from prints with ts <= t only."""

    t: int
    source: str
    price: float
    prints5: int
    buy_usd5: float
    prev_buy_usd5: float
    buyers5: int
    buys5: int
    sells5: int
    runup: float
    retrace: float
    held_rate: float | None
    retrace_sell_share: float | None
    good_buyers: int = 0
    smart_buyers: int = 0
    trusted_buyers: int = 0
    graded_buyers: int = 0
    heat_hits: int = 0
    heat_rate: float = 0.0

    @property
    def band(self) -> int:
        return band_of(self.prints5)

    @property
    def accel(self) -> float:
        if self.prev_buy_usd5 > 0:
            return self.buy_usd5 / self.prev_buy_usd5
        return math.inf if self.buy_usd5 > 0 else 0.0

    @property
    def buy_share5(self) -> float | None:
        n = self.buys5 + self.sells5
        return self.buys5 / n if n else None


@dataclass
class WalletBook:
    """Point-in-time wallet labels. ``good`` is train-only precision (see module rule 6)."""

    stats: Mapping[int, tuple[int, int]] = field(default_factory=dict)  # wallet -> (n, hits)
    threshold: float = 1.0                                              # precision needed
    own: Mapping[int, int] = field(default_factory=dict)  # this token's own event (LOO): wallet -> hit
    trusted: frozenset[int] = frozenset()
    graded: frozenset[int] = frozenset()

    def is_good(self, wallet: int) -> bool:
        st = self.stats.get(wallet)
        if st is None:
            return False
        n, hits = st
        own = self.own.get(wallet)
        if own is not None:  # leave-one-token-out on train
            n, hits = n - 1, hits - own
        return n >= WALLET_MIN_TOKENS and hits / n >= self.threshold


@dataclass
class NarrativeIndex:
    """Per word: launch times and 3x times of every token carrying it (same chain)."""

    launches: Mapping[str, list[int]]
    hits: Mapping[str, list[int]]

    @classmethod
    def build(cls, rows: Iterable[tuple[tuple[str, ...], int, int | None]]) -> NarrativeIndex:
        launches: dict[str, list[int]] = collections.defaultdict(list)
        hits: dict[str, list[int]] = collections.defaultdict(list)
        for words, t0, t3x in rows:
            for w in words:
                launches[w].append(t0)
                if t3x is not None:
                    hits[w].append(t3x)
        for v in launches.values():
            v.sort()
        for v in hits.values():
            v.sort()
        return cls(dict(launches), dict(hits))

    def heat(self, words: Sequence[str], t: int, *, self_t0: int, self_t3x: int | None) -> tuple[int, float]:
        """(max hits, max hit-rate over words with >= 10 launches) in ``[t - HEAT_S, t)``.

        A hit is a token whose 3x happened inside the window, strictly before ``t``. The
        token itself is excluded: its own run is momentum, not narrative.
        """
        lo = t - HEAT_S * 1000
        best_hits, best_rate = 0, 0.0
        for w in words:
            h = self.hits.get(w, ())
            nh = bisect.bisect_left(h, t) - bisect.bisect_left(h, lo)
            if self_t3x is not None and lo <= self_t3x < t:
                nh -= 1
            ln = self.launches.get(w, ())
            nl = bisect.bisect_left(ln, t) - bisect.bisect_left(ln, lo)
            if lo <= self_t0 < t:
                nl -= 1
            best_hits = max(best_hits, nh)
            if nl >= 10:
                best_rate = max(best_rate, nh / nl)
        return best_hits, best_rate


class _RunMax:
    """Incremental running max of one series, advanced monotonically in t."""

    __slots__ = ("ptr", "mx", "imax")

    def __init__(self) -> None:
        self.ptr, self.mx, self.imax = 0, -1.0, -1

    def advance(self, ser: Series, i1: int) -> None:
        px = ser.px
        for i in range(self.ptr, i1):
            if px[i] > self.mx:
                self.mx, self.imax = px[i], i
        self.ptr = max(self.ptr, i1)


def features_at(
    tape: TokenTape, t: int, *, source: str | None = None, runmax: _RunMax | None = None,
    wallets: WalletBook | None = None, narrative: NarrativeIndex | None = None,
    self_t3x: int | None = None,
) -> Features | None:
    """Features at ``t`` from prints with ts <= t. ``None`` if the checkpoint is not active."""
    src = source or tape.source_at(t)
    if src is None:
        return None
    ser = tape.series[src]
    i1 = upto(ser.ts, t)
    if i1 == 0 or ser.ts[i1 - 1] <= t - ACTIVE_S * 1000:
        return None
    if runmax is None:
        runmax = _RunMax()
    runmax.advance(ser, i1)
    mx, imax = runmax.mx, runmax.imax
    price = ser.px[i1 - 1]
    first = ser.px[0]
    i0 = upto(ser.ts, t - VOL_S * 1000)
    ip = upto(ser.ts, t - 2 * VOL_S * 1000)
    cu, cb, cs = ser.cum_buy_usd, ser.cum_buys, ser.cum_sells
    buyers5 = len({ser.wallet[k] for k in range(i0, i1) if ser.side[k] == 1})
    # dip: run window = first print .. running max; retrace window = after the max .. t
    run_ms = max(ser.ts[imax] - ser.ts[0], 60_000)
    ret_ms = max(t - ser.ts[imax], 60_000)
    run_rate = (cu[imax + 1] - cu[0]) / run_ms
    ret_rate = (cu[i1] - cu[imax + 1]) / ret_ms
    held_rate = (ret_rate / run_rate) if run_rate > 0 else None
    rb, rs = cb[i1] - cb[imax + 1], cs[i1] - cs[imax + 1]
    sell_share = rs / (rb + rs) if (rb + rs) else None
    f = Features(
        t=t, source=src, price=price, prints5=i1 - i0,
        buy_usd5=cu[i1] - cu[i0], prev_buy_usd5=cu[i0] - cu[ip], buyers5=buyers5,
        buys5=cb[i1] - cb[i0], sells5=cs[i1] - cs[i0],
        runup=mx / first if first > 0 else 0.0,
        retrace=1.0 - price / mx if mx > 0 else 0.0,
        held_rate=held_rate, retrace_sell_share=sell_share,
    )
    if wallets is not None:
        a1 = upto(tape.ts, t)
        a0 = upto(tape.ts, t - WALLET_S * 1000)
        buyers: set[int] = set()
        smart: set[int] = set()
        for k in range(a0, a1):
            if tape.side[k] == 1:
                buyers.add(tape.wallet[k])
                if tape.source[k] == SMART_SOURCE:
                    smart.add(tape.wallet[k])
        f.good_buyers = sum(1 for w in buyers if wallets.is_good(w))
        f.smart_buyers = len(smart)
        f.trusted_buyers = len(buyers & wallets.trusted) if wallets.trusted else 0
        f.graded_buyers = len(buyers & wallets.graded) if wallets.graded else 0
    if narrative is not None and tape.words:
        f.heat_hits, f.heat_rate = narrative.heat(tape.words, t, self_t0=tape.t0, self_t3x=self_t3x)
    return f


@dataclass(frozen=True)
class BandQuantiles:
    """TRAIN quantiles of buy USD and buyers in one print-count band."""

    q50: float
    q75: float
    q90: float
    buyers_q75: float


def volume_flags(f: Features, bands: Mapping[int, BandQuantiles]) -> int:
    """Volume flags, each judged against checkpoints with the SAME print-count band."""
    if f.prints5 <= 0:
        return 0
    q = bands.get(f.band)
    if q is None:
        return 0
    m = 0
    v = f.buy_usd5
    if v > 0 and v >= q.q50:
        m |= BIT["V_q50"]
        if f.accel >= 2.0:
            m |= BIT["V_accel2"]
        bs = f.buy_share5
        if bs is not None and bs >= 0.6:
            m |= BIT["V_bsr60"]
    if v > 0 and v >= q.q75:
        m |= BIT["V_q75"]
    if v > 0 and v >= q.q90:
        m |= BIT["V_q90"]
    if f.buyers5 >= 1 and f.buyers5 >= q.buyers_q75:
        m |= BIT["V_breadth_q75"]
    return m


def wallet_flags(f: Features) -> int:
    m = 0
    if f.good_buyers >= 1:
        m |= BIT["W_good1"]
    if f.good_buyers >= 2:
        m |= BIT["W_good2"]
    if f.smart_buyers >= 1:
        m |= BIT["W_gmgnsmart1"]
    if f.smart_buyers >= 2:
        m |= BIT["W_gmgnsmart2"]
    if f.trusted_buyers >= 1:
        m |= BIT["W_trusted1"]
    if f.graded_buyers >= 1:
        m |= BIT["W_gradeAB1_LOOKAHEAD"]
    return m


def dip_flags(f: Features) -> int:
    m = 0
    if not (0 < f.retrace < DIP_MAX_RETRACE):
        return 0
    held = (
        f.held_rate is not None and f.held_rate >= HELD_RATE
        and f.retrace_sell_share is not None and f.retrace_sell_share <= HELD_SELL_SHARE
    )
    for r in DIP_RUNUP:
        if f.runup < r:
            continue
        for d in DIP_RETRACE:
            if f.retrace >= d:
                m |= BIT[f"D_r{r:g}_d{int(d * 100)}"]
                if held:
                    m |= BIT[f"D_r{r:g}_d{int(d * 100)}_held"]
    return m


def narrative_flags(f: Features) -> int:
    m = 0
    if f.heat_hits >= 1:
        m |= BIT["N_hits1"]
    if f.heat_hits >= 3:
        m |= BIT["N_hits3"]
    if f.heat_hits >= 10:
        m |= BIT["N_hits10"]
    if f.heat_rate >= 0.10:
        m |= BIT["N_rate10"]
    return m


def flags_of(f: Features, bands: Mapping[int, BandQuantiles]) -> int:
    return volume_flags(f, bands) | wallet_flags(f) | dip_flags(f) | narrative_flags(f)


def first_multiple_time(ts: Sequence[int], px: Sequence[float], multiple: float) -> int | None:
    """First time a series reached ``multiple`` x its first price (known at that time)."""
    if not px or px[0] <= 0:
        return None
    target = px[0] * multiple
    for t, p in zip(ts, px):
        if p >= target:
            return t
    return None


# --------------------------------------------------------------------------------------
# the exit ladder: a float replica of kaiba.execution.protection.evaluate
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LadderConfig:
    stop_loss: float = 0.30
    tp_ladder: tuple[tuple[float, float], ...] = ((2.0, 0.50), (5.0, 0.25), (10.0, 0.15))
    trailing: tuple[tuple[float, float], ...] = (
        (1.2, 0.20), (2.0, 0.30), (5.0, 0.25), (10.0, 0.20), (25.0, 0.15), (100.0, 0.10))
    breakeven_after_tp1: bool = True
    emergency_loss: float = 0.50
    stale_s: int = 3600
    moonbag_retain: float = 0.20
    moonbag_trail: float = 0.50

    @classmethod
    def from_protection(cls, block: Mapping[str, Any]) -> LadderConfig:
        """From a risk.yaml ``protection:`` block (bps and percents, as the file writes them)."""
        d = cls()
        return cls(
            stop_loss=float(block.get("stop_loss_bps", 3000)) / 10_000,
            tp_ladder=tuple((float(m), float(p) / 100) for m, p in block.get("tp_ladder", ())) or d.tp_ladder,
            trailing=tuple(sorted((float(m), float(b) / 10_000) for m, b in block.get("trailing", ()))) or d.trailing,
            breakeven_after_tp1=bool(block.get("breakeven_after_tp1", True)),
            emergency_loss=float(block.get("emergency_loss_bps", 5000)) / 10_000,
            stale_s=int(block.get("stale_no_volume_exit_s", 3600)),
            moonbag_retain=float(block.get("moonbag_retain_pct", 20)) / 100,
            moonbag_trail=float(block.get("moonbag_trail_bps", 5000)) / 10_000,
        )


class LadderState:
    __slots__ = ("entry", "peak", "stop", "done", "trail", "moonbag")

    def __init__(self, entry: float) -> None:
        self.entry, self.peak, self.stop = entry, entry, None
        self.done: set[str] = set()
        self.trail: float | None = None
        self.moonbag = False


def _trail_for(st: LadderState, peak: float, cfg: LadderConfig) -> float | None:
    m = peak / st.entry
    unlocked = None
    for mult, frac in cfg.trailing:
        if m >= mult:
            unlocked = frac
    if unlocked is None:
        return None
    return max(unlocked, cfg.moonbag_trail) if st.moonbag else unlocked


def _commit(st: LadderState, price: float, cfg: LadderConfig) -> None:
    st.peak = max(st.peak, price)
    cands = []
    if st.stop is not None:
        cands.append(st.stop)
    if not st.done:
        cands.append(st.entry * (1 - cfg.stop_loss))
    if st.done and cfg.breakeven_after_tp1:
        cands.append(st.entry)
    tb = _trail_for(st, st.peak, cfg)
    if tb is not None:
        cands.append(st.peak * (1 - tb))
    ns = max(cands) if cands else 0.0
    if st.stop is None or ns > st.stop:
        st.stop = ns
    if tb is not None:
        st.trail = tb


def ladder_step(st: LadderState, price: float, silent_s: float | None, cfg: LadderConfig) -> tuple[str, float, str]:
    """(kind, fraction of REMAINING to sell, reason). Same precedence as protection.evaluate."""
    entry = st.entry
    if price <= entry * (1 - cfg.emergency_loss):
        return "exit", 1.0, "emergency_loss"
    if not st.done and price <= entry * (1 - cfg.stop_loss):
        return "exit", 1.0, "stop_loss"
    if st.stop is not None and price <= st.stop:
        reason = "trailing_stop" if st.trail is not None else "stop_loss"
        if reason == "trailing_stop" and 0 < cfg.moonbag_retain < 1 and price > entry and not st.moonbag:
            st.moonbag = True
            st.done.add("moonbag")
            st.stop = None
            _commit(st, price, cfg)
            return "trim", 1 - cfg.moonbag_retain, "trailing_stop_moonbag"
        return "exit", 1.0, reason
    for i, (mult, frac) in enumerate(cfg.tp_ladder):
        tag = f"tp{i + 1}"
        if tag in st.done:
            continue
        if price < entry * mult:
            break
        st.done.add(tag)
        _commit(st, price, cfg)
        return "trim", frac, tag
    if cfg.stale_s > 0 and silent_s is not None and silent_s >= cfg.stale_s:
        return "exit", 1.0, "stale_no_volume"
    _commit(st, price, cfg)
    return "hold", 0.0, "hold"


@dataclass(frozen=True)
class SimResult:
    status: str          # priced | unpriced_gap | censored | no_fill
    net: float | None    # net return, fraction
    gross: float | None  # gross multiple of entry before costs
    reason: str
    entry_ms: int | None
    exit_ms: int | None
    max_multiple: float | None


def simulate(
    ser: Series, t: int, *, cfg: LadderConfig, data_end_ms: int,
    other_ts: Sequence[int] = (), leg_cost: float = LEG_COST, poll_s: int = POLL_S,
    horizon_s: int = HORIZON_S, latency_s: int = LATENCY_S,
) -> SimResult:
    """Buy at ``t + latency`` and run the ladder on a ``poll_s`` clock.

    These venues are AMMs (pump.fun curve, Pons curve, PumpSwap/Uniswap): an order fills
    against the pool's price at the moment it lands, which is the last print at or before
    that moment. So the buy fills at the last print <= ``t + latency`` (prints in
    ``(t, t + latency]`` are after the decision and may move our fill, never our features),
    and a sell decided at tick T fills at the last print <= ``T + latency``. The watchdog
    sees the LAST print at or before each tick; a wick between ticks is invisible to it, as
    it is live. A token nobody trades after we buy is not dropped: it rides to the stale
    exit and pays both legs, which is what buying a dead token costs.
    """
    ts, px = ser.ts, ser.px
    n = len(ts)
    lat = latency_s * 1000
    e_ms = t + lat
    i = bisect.bisect_right(ts, e_ms)  # prints at or before our fill
    if i == 0:
        return SimResult("no_fill", None, None, "no_price_before_fill", None, None, None)
    entry = px[i - 1]
    end_ms = e_ms + horizon_s * 1000
    if end_ms > data_end_ms:
        return SimResult("censored", None, None, "horizon_past_data", e_ms, None, None)
    j_end = bisect.bisect_right(ts, end_ms)
    max_mult = max(px[i - 1:j_end]) / entry
    st = LadderState(entry)
    remaining, proceeds = 1.0, 0.0
    # Our own buy is a trade: the stale clock starts at the fill, not at the last print.
    price, last = entry, e_ms
    j = i
    k = 1
    poll = poll_s * 1000
    stale_ms = cfg.stale_s * 1000 if cfg.stale_s > 0 else None
    k_end = -(-horizon_s * 1000 // poll)
    dirty = True
    reason = "horizon"
    exit_ms = None
    while True:
        if k > k_end:
            proceeds += remaining * price / entry
            remaining = 0.0
            exit_ms = end_ms
            break
        T = e_ms + k * poll
        new = False
        while j < n and ts[j] <= T:
            if ts[j] - last > GAP_S * 1000:
                a = bisect.bisect_right(other_ts, last)
                if a < len(other_ts) and other_ts[a] < ts[j]:
                    return SimResult("unpriced_gap", None, None, "series_hole_while_token_printed_elsewhere",
                                     e_ms, ts[j], max_mult)
            price, last = px[j], ts[j]
            j += 1
            new = True
        silent = T - last
        if new or dirty or (stale_ms is not None and silent >= stale_ms):
            kind, frac, why = ladder_step(st, price, silent / 1000.0, cfg)
            if kind != "hold":
                if why == "stale_no_volume":
                    fill = price
                    a = bisect.bisect_right(other_ts, last)
                    if a < len(other_ts) and other_ts[a] <= T:
                        return SimResult("unpriced_gap", None, None, "series_silent_but_token_printed_elsewhere",
                                         e_ms, T, max_mult)
                else:
                    # The sell lands `latency_s` after the tick, at the pool's price then.
                    f_i = bisect.bisect_right(ts, T + lat)
                    fill = px[f_i - 1] if f_i > 0 else price
                sold = remaining * frac
                proceeds += sold * fill / entry
                remaining -= sold
                if kind == "exit" or remaining <= 1e-9:
                    reason, exit_ms, remaining = why, T, 0.0
                    break
                dirty = True
            else:
                dirty = False
        if dirty:
            k += 1
            continue
        cands = [k_end + 1]
        if j < n:
            cands.append(-(-(ts[j] - e_ms) // poll))
        if stale_ms is not None:
            cands.append(-(-(last + stale_ms - e_ms) // poll))
        k = max(k + 1, min(cands))
    net = (1 - leg_cost) * (1 - leg_cost) * proceeds - 1
    return SimResult("priced", net, proceeds, reason, e_ms, exit_ms, max_mult)


# --------------------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------------------


@dataclass
class CellStats:
    n: int
    mean: float | None
    win: float | None
    lo: float | None
    hi: float | None
    p_pos: float | None
    mean_max_mult: float | None = None
    trimmed: float | None = None
    median: float | None = None

    @property
    def measured(self) -> bool:
        return self.n >= MIN_CELL

    def line(self) -> str:
        if not self.n:
            return "n=0"
        tag = "" if self.measured else "  UNMEASURED(n<%d)" % MIN_CELL
        mm = f" maxx6h {self.mean_max_mult:.2f}" if self.mean_max_mult is not None else ""
        base = f"n={self.n:5d} mean {self.mean * 100:+6.1f}% trim1% {self.trimmed * 100:+6.1f}% win {self.win * 100:4.1f}%"
        if self.lo is None or self.p_pos is None:
            return base + mm + tag
        return base + f" CI [{self.lo * 100:+.1f},{self.hi * 100:+.1f}] P>0 {self.p_pos:.3f}" + mm + tag


def bootstrap(values: Sequence[float], *, draws: int = BOOTSTRAP_DRAWS, seed: int = BOOTSTRAP_SEED,
              alpha: float = 0.05) -> tuple[float, float, float] | None:
    """Percentile CI of the mean (same method as replay.bootstrap_ci) and P(mean > 0)."""
    n = len(values)
    if n < 3:
        return None
    rng = random.Random(seed)
    vals = list(values)
    means = []
    for _ in range(draws):
        s = 0.0
        for _ in range(n):
            s += vals[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    lo = means[int(alpha / 2 * draws)]
    hi = means[min(draws - 1, int((1 - alpha / 2) * draws))]
    return lo, hi, sum(1 for m in means if m > 0) / draws


def cell_stats(nets: Sequence[float], max_mults: Sequence[float] | None = None, *, boot: bool = True) -> CellStats:
    n = len(nets)
    if not n:
        return CellStats(0, None, None, None, None, None)
    mean = sum(nets) / n
    win = sum(1 for v in nets if v > 0) / n
    b = bootstrap(nets) if boot else None
    mm = (sum(max_mults) / len(max_mults)) if max_mults else None
    return CellStats(n, mean, win, b[0] if b else None, b[1] if b else None, b[2] if b else None, mm,
                     trimmed_mean(nets), sorted(nets)[n // 2])


def trimmed_mean(values: Sequence[float], frac: float = 0.01) -> float | None:
    """Mean after dropping ``frac`` of observations at EACH end (at least one each when n >= 20).

    An edge that lives in the top 1% of trades is ten trades in a thousand: report it, but
    never select on it.
    """
    v = sorted(values)
    n = len(v)
    if not n:
        return None
    k = int(n * frac)
    if k == 0 and n >= 20:
        k = 1
    core = v[k:n - k] if n - 2 * k > 0 else v
    return sum(core) / len(core)


def sharpe(values: Sequence[float]) -> float | None:
    n = len(values)
    if n < 2:
        return None
    m = sum(values) / n
    var = sum((v - m) ** 2 for v in values) / (n - 1)
    return m / math.sqrt(var) if var > 0 else None


def _norm_ppf(p: float) -> float:
    # Acklam's rational approximation; adequate for a haircut.
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    if p < 0.02425:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > 1 - 0.02425:
        return -_norm_ppf(1 - p)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
        (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def deflated_sharpe(values: Sequence[float], trials: int, trial_sharpes: Sequence[float]) -> float | None:
    """Bailey/Lopez de Prado DSR, the same formula as gates.deflated_sharpe."""
    n = len(values)
    sr = sharpe(values)
    if sr is None or n < 3:
        return None
    m = sum(values) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))
    skew = sum(((v - m) / sd) ** 3 for v in values) / n
    kurt = sum(((v - m) / sd) ** 4 for v in values) / n
    ts = [s for s in trial_sharpes if s is not None]
    var = (sum((s - sum(ts) / len(ts)) ** 2 for s in ts) / (len(ts) - 1)) if len(ts) >= 2 else 0.0
    sr0 = 0.0
    if trials >= 2 and var > 0:
        g = 0.5772156649
        sr0 = math.sqrt(var) * ((1 - g) * _norm_ppf(1 - 1 / trials) + g * _norm_ppf(1 - 1 / (trials * math.e)))
    den = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if den <= 0:
        return None
    z = (sr - sr0) * math.sqrt(n - 1) / math.sqrt(den)
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


# --------------------------------------------------------------------------------------
# orchestration over a compact extract (see docs/research/mooner-confluence-2026-10-04.md)
# --------------------------------------------------------------------------------------


@dataclass
class Checkpoint:
    t: int
    source: str
    mask: int
    band: int


class TokenWork:
    """Per-token state kept in a worker between commands."""

    __slots__ = ("tape", "t3x", "split", "checkpoints", "sims", "rand_cp")

    def __init__(self, tape: TokenTape) -> None:
        self.tape = tape
        self.t3x: int | None = None
        self.split = ""
        self.checkpoints: list[Checkpoint] = []
        self.sims: dict[tuple[str, int], SimResult] = {}
        self.rand_cp: int | None = None


def wallet_first_buy_events(tape: TokenTape, *, until_ms: int) -> list[tuple[int, int]]:
    """(wallet, hit) for each wallet's FIRST buy, priced on the whole-tape series source.

    A hit is the series reaching WALLET_HIT_MULTIPLE x the price at the buy within
    HORIZON_S. Only events whose outcome window closes by ``until_ms`` are returned.
    """
    counts = {s: len(ser.ts) for s, ser in tape.series.items()}
    src = series_source(counts, tape.chain)
    if src is None:
        return []
    ser = tape.series[src]
    seen: set[int] = set()
    out = []
    for t, side, w in zip(tape.ts, tape.side, tape.wallet):
        if side != 1 or w in seen:
            continue
        seen.add(w)
        if t + HORIZON_S * 1000 > until_ms:
            continue
        i = upto(ser.ts, t)
        if i == 0:
            continue
        base = ser.px[i - 1]
        j = upto(ser.ts, t + HORIZON_S * 1000)
        peak = max(ser.px[i:j], default=0.0)
        out.append((w, 1 if peak >= base * WALLET_HIT_MULTIPLE else 0))
    return out


def _quantile(sorted_vals: Sequence[float], q: float) -> float:
    if not sorted_vals:
        return math.inf
    i = min(len(sorted_vals) - 1, max(0, int(q * (len(sorted_vals) - 1))))
    return sorted_vals[i]


def build_bands(samples: Mapping[int, tuple[list[float], list[float]]]) -> dict[int, BandQuantiles]:
    out = {}
    for band, (usd, buyers) in samples.items():
        u = sorted(usd)
        b = sorted(buyers)
        out[band] = BandQuantiles(_quantile(u, 0.5), _quantile(u, 0.75), _quantile(u, 0.9), _quantile(b, 0.75))
    return out


def evaluate_rules(
    works: Iterable[TokenWork], masks: Sequence[int], *, cfg: LadderConfig, data_end_ms: int,
    split_ms: Mapping[str, int], leg_cost: float = LEG_COST, random_baseline: bool = False,
) -> dict[tuple[str, str, int], dict[str, Any]]:
    """For every rule mask: first trigger per token -> simulated outcome, grouped by (chain, split)."""
    out: dict[tuple[str, str, int], dict[str, Any]] = {}

    def slot(chain: str, split: str, mask: int) -> dict[str, Any]:
        key = (chain, split, mask)
        s = out.get(key)
        if s is None:
            s = out[key] = {"net": array.array("f"), "mm": array.array("f"), "status": collections.Counter()}
        return s

    for w in works:
        cps = w.checkpoints
        if not cps:
            continue
        chain = w.tape.chain
        for mask in masks:
            if random_baseline and mask == -1:
                if w.rand_cp is None:
                    continue
                cp = cps[w.rand_cp]
            else:
                cp = next((c for c in cps if (c.mask & mask) == mask), None)
                if cp is None:
                    continue
            key = (cp.source, cp.t)
            res = w.sims.get(key)
            if res is None or leg_cost != LEG_COST:
                res = simulate(w.tape.series[cp.source], cp.t, cfg=cfg, data_end_ms=data_end_ms,
                               other_ts=_other_ts(w, cp.source), leg_cost=leg_cost)
                if leg_cost == LEG_COST:
                    w.sims[key] = res
            s = slot(chain, w.split, mask)
            status = res.status
            if status == "priced" and w.split == "train" and res.entry_ms is not None and \
                    res.entry_ms + HORIZON_S * 1000 > split_ms[chain]:
                status = "purged"
            s["status"][status] += 1
            if status == "priced":
                s["net"].append(res.net)
                s["mm"].append(res.max_multiple)
    return out


_OTHER_CACHE: dict[tuple[int, str], list[int]] = {}


def _other_ts(w: TokenWork, source: str) -> list[int]:
    key = (id(w), source)
    v = _OTHER_CACHE.get(key)
    if v is None:
        v = _OTHER_CACHE[key] = w.tape.other_source_ts(source)
    return v


# ---- worker process -------------------------------------------------------------------


def _load_shard(path: str) -> dict[str, Any]:
    with open(path, "rb") as fh:
        blob = json.loads(zlib.decompress(fh.read()))
    cols = {}
    for name, code in (("gid", "i"), ("ts", "q"), ("px", "d"), ("usd", "d"), ("sd", "b"), ("sr", "b"), ("wl", "q")):
        a = array.array(code)
        with open(path + "." + name, "rb") as fh:
            a.frombytes(zlib.decompress(fh.read()))
        cols[name] = a
    blob["cols"] = cols
    return blob


def _worker(path: str, conn: Any) -> None:
    sys.setrecursionlimit(10_000)
    shard = _load_shard(path)
    cols = shard["cols"]
    sources = shard["sources"]          # id -> name
    meta = shard["meta"]                # gid -> [chain, token, words, launchpad]
    rows_by: dict[int, list[tuple]] = collections.defaultdict(list)
    gid, ts, px, usd, sd, sr, wl = (cols[k] for k in ("gid", "ts", "px", "usd", "sd", "sr", "wl"))
    for k in range(len(gid)):
        rows_by[gid[k]].append((ts[k], px[k], usd[k], sd[k], wl[k], sources[str(sr[k])]))
    del cols, shard
    works: dict[int, TokenWork] = {}
    for g, rows in rows_by.items():
        m = meta[str(g)]
        tape = TokenTape.from_rows(m[0], m[1], rows, words=tuple(m[2]), launchpad=m[3])
        if not tape.series:
            continue
        works[g] = TokenWork(tape)
    rows_by.clear()
    cfg = LadderConfig()
    data_end = 0
    while True:
        cmd, arg = conn.recv()
        if cmd == "stop":
            conn.send(None)
            return
        if cmd == "pass1":
            split_ms, data_end, cfg = arg["split_ms"], arg["data_end"], arg["cfg"]
            t3x, events, samples = {}, [], collections.defaultdict(lambda: ([], []))
            for g, w in works.items():
                tape = w.tape
                w.split = "train" if tape.t0 < split_ms[tape.chain] else "test"
                counts = {s: len(ser.ts) for s, ser in tape.series.items()}
                src = series_source(counts, tape.chain)
                ser = tape.series[src]
                w.t3x = first_multiple_time(ser.ts, ser.px, HEAT_MULTIPLE)
                t3x[g] = w.t3x
                if w.split == "train":
                    for wallet, hit in wallet_first_buy_events(tape, until_ms=split_ms[tape.chain]):
                        events.append((tape.chain, wallet, g, hit))
                    rm: dict[str, _RunMax] = {}
                    for off in CHECKPOINTS_S:
                        t = tape.t0 + off * 1000
                        if t > split_ms[tape.chain]:
                            break
                        s = tape.source_at(t)
                        if s is None:
                            continue
                        f = features_at(tape, t, source=s, runmax=rm.setdefault(s, _RunMax()))
                        if f is None or f.prints5 == 0:
                            continue
                        smp = samples[(tape.chain, f.band)]
                        smp[0].append(f.buy_usd5)
                        smp[1].append(float(f.buyers5))
            conn.send({"t3x": t3x, "events": events, "samples": dict(samples), "n_tokens": len(works)})
        elif cmd == "features":
            bands = arg["bands"]
            nidx = {c: NarrativeIndex(v["launches"], v["hits"]) for c, v in arg["narrative"].items()}
            stats = arg["wallet_stats"]
            thr = arg["wallet_threshold"]
            trusted, graded = arg["trusted"], arg["graded"]
            census = collections.Counter()
            for g, w in works.items():
                tape = w.tape
                ch = tape.chain
                own = {}
                if w.split == "train":
                    for wallet, hit in wallet_first_buy_events(tape, until_ms=arg["split_ms"][ch]):
                        own[wallet] = hit
                book = WalletBook(stats.get(ch, {}), thr.get(ch, 1.0), own,
                                  trusted.get(ch, frozenset()), graded.get(ch, frozenset()))
                rm = {}
                for off in CHECKPOINTS_S:
                    t = tape.t0 + off * 1000
                    if t + HORIZON_S * 1000 > data_end:
                        break
                    s = tape.source_at(t)
                    if s is None:
                        continue
                    f = features_at(tape, t, source=s, runmax=rm.setdefault(s, _RunMax()), wallets=book,
                                    narrative=nidx.get(ch), self_t3x=w.t3x)
                    if f is None:
                        continue
                    w.checkpoints.append(Checkpoint(t, s, flags_of(f, bands.get(ch, {})), f.band))
                if w.checkpoints:
                    census[(ch, w.split, "tokens_active")] += 1
                    census[(ch, w.split, "checkpoints")] += len(w.checkpoints)
                    w.rand_cp = random.Random(g * 7919 + 17).randrange(len(w.checkpoints))
                    last = w.checkpoints[-1].t - tape.t0
                    if last >= 3600 * 1000:
                        census[(ch, w.split, "active_beyond_1h")] += 1
                    seen = 0
                    for c in w.checkpoints:
                        seen |= c.mask
                    for name in FLAG_NAMES:
                        if seen & BIT[name]:
                            census[(ch, w.split, "flag:" + name)] += 1
                else:
                    census[(ch, w.split, "tokens_inactive")] += 1
            conn.send(dict(census))
        elif cmd == "eval":
            res = evaluate_rules(works.values(), arg["masks"], cfg=cfg, data_end_ms=data_end,
                                 split_ms=arg["split_ms"], leg_cost=arg.get("leg_cost", LEG_COST),
                                 random_baseline=arg.get("random_baseline", False))
            conn.send({k: {"net": v["net"].tobytes(), "mm": v["mm"].tobytes(), "status": dict(v["status"])}
                       for k, v in res.items()})
        elif cmd == "calib":
            by_tok = {(w.tape.chain, w.tape.token): w for w in works.values()}
            out = []
            for chain, token, lane, mode, opened, real in arg["positions"]:
                w = by_tok.get((chain, token))
                if w is None:
                    out.append((chain, lane, mode, real, None, "token_not_on_tape"))
                    continue
                t = opened - LATENCY_S * 1000
                s_ = w.tape.source_at(t)
                if s_ is None:
                    out.append((chain, lane, mode, real, None, "no_price_source_at_entry"))
                    continue
                r = simulate(w.tape.series[s_], t, cfg=cfg, data_end_ms=data_end,
                             other_ts=_other_ts(w, s_))
                out.append((chain, lane, mode, real, r.net, r.status))
            conn.send(out)
        elif cmd == "eval_band":
            # baseline and named rules re-cut by the print-count band at the trigger
            out = collections.defaultdict(lambda: array.array("f"))
            for w in works.values():
                for mask in arg["masks"]:
                    cp = next((c for c in w.checkpoints if (c.mask & mask) == mask), None)
                    if cp is None:
                        continue
                    r = w.sims.get((cp.source, cp.t))
                    if r is None:
                        r = simulate(w.tape.series[cp.source], cp.t, cfg=cfg, data_end_ms=data_end,
                                     other_ts=_other_ts(w, cp.source))
                        w.sims[(cp.source, cp.t)] = r
                    if r.status != "priced":
                        continue
                    if w.split == "train" and r.entry_ms + HORIZON_S * 1000 > arg["split_ms"][w.tape.chain]:
                        continue
                    out[(w.tape.chain, w.split, mask, cp.band)].append(r.net)
            conn.send({k: v.tobytes() for k, v in out.items()})


__all__ = [
    "LEG_COST", "LadderConfig", "LadderState", "ladder_step", "simulate", "SimResult", "Series",
    "TokenTape", "Features", "features_at", "upto", "volume_flags", "wallet_flags", "dip_flags",
    "narrative_flags", "flags_of", "BandQuantiles", "build_bands", "band_of", "WalletBook",
    "NarrativeIndex", "words_of", "series_source", "cell_stats", "bootstrap", "deflated_sharpe",
    "rule_mask", "rule_name", "FLAG_NAMES", "FACTORS", "BIT", "wallet_first_buy_events",
    "first_multiple_time", "evaluate_rules", "TokenWork", "Checkpoint", "trimmed_mean",
    "PRICE_SOURCES",
]
