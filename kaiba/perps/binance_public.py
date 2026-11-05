"""Binance USD-M public market data for the perps paper book: daily klines and funding (no key, no account).

Only the keyless market endpoints of ``fapi.binance.com`` are used. MEASURED 2026-11-03: the Kaiba
box reaches them (HTTP 200, 0.17 s); the owner's PC times out. Calls are few (two per symbol per day)
and bounded; nothing here can place an order, read an account or move funds.
"""
from __future__ import annotations

import math
from typing import Any

from kaiba.perps.d1_breakout import DAY_MS, Bar

FAPI = "https://fapi.binance.com"
TIMEOUT_S = 15.0
MAX_FUNDING_PAGES = 5


class DataRefused(Exception):
    """The response cannot be trusted as a daily series (gap, bad price, wrong shape)."""


def _get(http: Any, path: str, params: dict) -> Any:
    r = http.get(FAPI + path, params=params, timeout=TIMEOUT_S)
    r.raise_for_status()
    return r.json()


def parse_klines(rows: Any, now_ms: int) -> tuple[list[Bar], float | None]:
    """``(closed daily bars, open of the day in progress or None)``. Refuses gaps and bad rows."""
    if not isinstance(rows, list):
        raise DataRefused("klines payload is not a list")
    closed: list[Bar] = []
    next_open = None
    prev_t = None
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            raise DataRefused("kline row shape")
        t, close_t = int(row[0]), int(row[6])
        o, h, lo, c = (float(row[1]), float(row[2]), float(row[3]), float(row[4]))
        if t % DAY_MS or close_t != t + DAY_MS - 1:
            raise DataRefused("kline is not a UTC day")
        if not all(math.isfinite(x) and x > 0 for x in (o, h, lo, c)) or not lo <= min(o, c) <= max(o, c) <= h:
            raise DataRefused("kline prices")
        if prev_t is not None and t != prev_t + DAY_MS:
            raise DataRefused("kline gap")
        prev_t = t
        if close_t < now_ms:
            closed.append(Bar(float(t), o, h, lo, c))
        elif t <= now_ms:
            next_open = o
    return closed, next_open


def daily(symbol: str, limit: int, http: Any, now_ms: int) -> tuple[list[Bar], float | None]:
    limit = max(1, min(1000, int(limit)))
    return parse_klines(_get(http, "/fapi/v1/klines", {"symbol": symbol, "interval": "1d", "limit": limit}), now_ms)


def parse_funding(rows: Any) -> tuple[list[float], list[float]]:
    if not isinstance(rows, list):
        raise DataRefused("funding payload is not a list")
    out = []
    for row in rows:
        try:
            t, r = int(row["fundingTime"]), float(row["fundingRate"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DataRefused("funding row shape") from exc
        if not math.isfinite(r) or abs(r) > 0.05:
            raise DataRefused("funding rate out of range")
        out.append((t, r))
    out.sort()
    return [float(t) for t, _ in out], [r for _, r in out]


def funding(symbol: str, start_ms: int, http: Any, now_ms: int) -> tuple[list[float], list[float]]:
    """Every funding print from ``start_ms`` to now (paged, at most MAX_FUNDING_PAGES x 1000)."""
    ts: list[float] = []
    rs: list[float] = []
    cursor = int(start_ms)
    for _ in range(MAX_FUNDING_PAGES):
        rows = _get(http, "/fapi/v1/fundingRate", {"symbol": symbol, "startTime": cursor, "limit": 1000})
        t, r = parse_funding(rows)
        new = [(a, b) for a, b in zip(t, r, strict=True) if not ts or a > ts[-1]]
        ts += [a for a, _ in new]
        rs += [b for _, b in new]
        if len(rows) < 1000 or not new or ts[-1] >= now_ms:
            return ts, rs
        cursor = int(ts[-1]) + 1
    raise DataRefused("funding history longer than the page bound")
