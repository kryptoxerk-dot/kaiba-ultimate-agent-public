"""The frozen D1 breakout_target strategy for Binance USD-M perpetuals (PERPS-D1-PAPER-CLAUDE-20261103).

Source: AEGIS OMNI ``research/prop_perps_2026_10`` (lead, 2026-10-09), the only positive
out-of-sample result in either Kaiba or Aegis: BTC/ETH 127 trades at +0.41R (out-of-sample
2023-01..2026-09: 72 trades, +0.38R); the same frozen settings on 8 coins the fit never saw, +0.165R
over 501 trades. Pure Python, no numpy, with the research's own order of operations (cumulative-sum
SMA, Wilder ATR) so a replay of the research period reproduces its trade list exactly
(``tests/test_perps_d1.py``).

Rules (FROZEN; a change is a new strategy, never an edit):

* daily bars on UTC days; signal on a bar's close, filled at the NEXT bar's open;
* long when the close breaks the prior 20-day high AND is above the 200-day SMA; short when it breaks
  the prior 20-day low AND is below it;
* stop 2.5 x ATR(14) from the entry; target 2R; exit at the next open after 30 daily bars;
* one position per symbol; when a bar touches both, the stop is assumed first; a gap through the stop
  fills at that bar's open;
* costs: 7 bps a side (5 taker + 2 slippage, Binance USD-M VIP0) plus the real 8-hourly funding paid
  between entry and exit (longs pay positive rates).

Results are in R: multiples of the risk taken at entry (the stop distance), net of costs.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

NAN = float("nan")
DAY_MS = 86_400_000
N_IN = 20
ATR_N = 14
ATR_K = 2.5
TARGET_R = 2.0
MAX_BARS = 30
SMA_DAYS = 200
COST_BPS = 7.0
PARAMS = {"n_in": N_IN, "atr_n": ATR_N, "atr_k": ATR_K, "target_r": TARGET_R, "max_bars": MAX_BARS,
          "sma_days": SMA_DAYS, "cost_bps_per_side": COST_BPS, "funding": "real 8-hourly, (entry, exit]"}


@dataclass(frozen=True)
class Bar:
    t: float  # open time, ms, UTC day boundary
    o: float
    h: float
    l: float  # noqa: E741 - the research's own field name
    c: float


@dataclass(frozen=True)
class Trade:
    symbol: str
    side: int
    entry_t: float
    exit_t: float
    entry: float
    exit: float
    stop: float
    r: float
    gross_r: float
    cost_r: float
    reason: str


@dataclass(frozen=True)
class OpenPosition:
    symbol: str
    side: int
    entry_t: float
    entry: float
    stop: float
    target: float
    signal_t: float
    bars_held: int


# ------------------------------------------------------------------ indicators (research order of operations)


def atr(bars: Sequence[Bar], n: int = ATR_N) -> list[float]:
    c = [b.c for b in bars]
    pc = [c[0], *c[:-1]] if c else []
    tr = [max(b.h - b.l, max(abs(b.h - p), abs(b.l - p))) for b, p in zip(bars, pc, strict=True)]
    out = [NAN] * len(tr)
    if len(tr) > n:
        acc = 0.0
        for x in tr[:n]:
            acc += x
        out[n - 1] = acc / n
        for i in range(n, len(tr)):
            out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def sma(x: Sequence[float], n: int) -> list[float]:
    out = [NAN] * len(x)
    cs = [0.0]
    for v in x:
        cs.append(cs[-1] + v)
    for i in range(n - 1, len(x)):
        out[i] = (cs[i + 1] - cs[i + 1 - n]) / n
    return out


def rolling_max(x: Sequence[float], n: int) -> list[float]:
    return [NAN] * min(n - 1, len(x)) + [max(x[i - n + 1:i + 1]) for i in range(n - 1, len(x))]


def rolling_min(x: Sequence[float], n: int) -> list[float]:
    return [NAN] * min(n - 1, len(x)) + [min(x[i - n + 1:i + 1]) for i in range(n - 1, len(x))]


class Signals:
    """The signal of ``grid.make("breakout_target", b, frozen params)``."""

    def __init__(self, bars: Sequence[Bar]):
        self.bars = bars
        self.hi = rolling_max([b.h for b in bars], N_IN)
        self.lo = rolling_min([b.l for b in bars], N_IN)
        self.a = atr(bars, ATR_N)
        self.trend = sma([b.c for b in bars], SMA_DAYS)

    def _ok(self, side: int, i: int) -> bool:
        if math.isnan(self.trend[i]):
            return False
        return (self.bars[i].c > self.trend[i]) if side > 0 else (self.bars[i].c < self.trend[i])

    def at(self, i: int) -> tuple[int, float] | None:
        """``(side, stop distance)`` on bar ``i``'s close, or ``None``."""
        if i < 1 or math.isnan(self.hi[i - 1]) or math.isnan(self.a[i]):
            return None
        c = self.bars[i].c
        for side in (1, -1):
            brk = (c > self.hi[i - 1]) if side > 0 else (c < self.lo[i - 1])
            if brk and self._ok(side, i):
                return side, ATR_K * self.a[i]
        return None


def funding_paid(fund_t: Sequence[float], fund_r: Sequence[float], t0: float, t1: float, side: int) -> float:
    """Funding paid as a fraction of notional over (t0, t1] (positive = a cost)."""
    i, j = bisect_right(fund_t, t0), bisect_right(fund_t, t1)
    return math.fsum(fund_r[i:j]) * side


def _exit_scan(bars: Sequence[Bar], k: int, side: int, entry: float, stop: float, target: float,
               next_open: float | None) -> tuple[int, float, str] | None:
    """The research's exit loop from entry bar ``k``: ``(exit bar index, price, reason)`` or ``None``
    while unresolved. ``next_open`` is the open of the bar after the last closed one (known once that
    bar has started); index ``len(bars)`` stands for it."""
    n = len(bars)
    j = k
    while j < n:
        b = bars[j]
        hit_stop = (b.l <= stop) if side > 0 else (b.h >= stop)
        hit_tgt = (b.h >= target) if side > 0 else (b.l <= target)
        if hit_stop:
            gap = (b.o < stop) if side > 0 else (b.o > stop)
            return j, (b.o if gap and j > k else stop), "stop"
        if hit_tgt:
            return j, target, "target"
        if j - k + 1 >= MAX_BARS:
            if j + 1 < n:
                return j + 1, bars[j + 1].o, "time"
            if next_open is not None:
                return n, next_open, "time"
        j += 1
    return None


def _trade(symbol: str, bars: Sequence[Bar], fund_t, fund_r, side, k, j, entry, exit_px, stop, dist, reason,
           exit_t: float | None = None) -> Trade:
    exit_t = bars[j].t if exit_t is None else exit_t
    gross = (exit_px - entry) * side
    cost = 2 * COST_BPS / 1e4 * entry + funding_paid(fund_t, fund_r, bars[k].t, exit_t, side) * entry
    return Trade(symbol, side, bars[k].t, exit_t, entry, exit_px, stop, (gross - cost) / dist, gross / dist,
                 cost / dist, reason)


def replay(symbol: str, bars: Sequence[Bar], fund_t: Sequence[float], fund_r: Sequence[float],
           *, first_signal_close_ms: float = -math.inf, next_open: float | None = None
           ) -> tuple[list[Trade], OpenPosition | None]:
    """Every trade the frozen rules take on ``bars`` (closed daily bars, oldest first).

    With the defaults this is exactly the research's ``backtest.run``. For the forward ledger,
    ``first_signal_close_ms`` admits only signals whose bar CLOSES at or after it, and ``next_open``
    (the open of the day in progress) lets the last closed bar's signal enter and a 30-bar time exit
    fill. Returns the closed trades and the position still open, if any."""
    sig = Signals(bars)
    trades: list[Trade] = []
    n = len(bars)
    i = 0
    while i < n:
        if i == n - 1 and next_open is None:
            break
        s = sig.at(i) if bars[i].t + DAY_MS >= first_signal_close_ms else None
        if not s:
            i += 1
            continue
        side, dist = s
        if not dist or dist <= 0 or math.isnan(dist):
            i += 1
            continue
        k = i + 1
        entry = bars[k].o if k < n else next_open
        stop = entry - side * dist
        target = entry + side * dist * TARGET_R
        if k >= n:  # entered at today's open; nothing has traded against it yet
            return trades, OpenPosition(symbol, side, bars[i].t + DAY_MS, entry, stop, target, bars[i].t, 0)
        hit = _exit_scan(bars, k, side, entry, stop, target, next_open)
        if hit is None:
            return trades, OpenPosition(symbol, side, bars[k].t, entry, stop, target, bars[i].t, n - k)
        j, exit_px, reason = hit
        if j >= n:  # a time exit at today's open
            trades.append(_trade(symbol, bars, fund_t, fund_r, side, k, j, entry, exit_px, stop, dist, reason,
                                 exit_t=bars[n - 1].t + DAY_MS))
            return trades, None
        trades.append(_trade(symbol, bars, fund_t, fund_r, side, k, j, entry, exit_px, stop, dist, reason))
        i = j  # flat again from the exit bar
    return trades, None
