"""Crypto prices from Binance's public market-data mirror (data-api.binance.vision; api.binance.com is
refused from the owner's PC).

* Collector ``tickers`` (every 60 s): 24-hour stats for the watchlist. Payload ``{"tickers":
  [{"symbol", "base", "price", "change_pct", "high", "low", "quote_volume", "time"}]}``. The page
  also streams live trades from ``wss://data-stream.binance.vision``; this is the fallback and the
  first paint.
* ``klines(symbol, interval, limit)``: candles for the terminal's chart, cached for 20 s. This is
  spot data. The strategy trades USD-M perps, whose daily candles come from the archive.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from typing import Any

from .base import Collector, Http, LiveHttp, num, utc_iso

API = "https://data-api.binance.vision/api/v3"
WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT",
             "LTCUSDT", "SUIUSDT", "TONUSDT", "TRXUSDT", "DOTUSDT", "NEARUSDT", "APTUSDT"]
INTERVALS = {"15m", "1h", "4h", "1d", "1w"}
_KCACHE: dict[tuple[str, str, int], tuple[float, list[list[float]]]] = {}
_KLOCK = threading.Lock()


def parse_tickers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        sym = str(r.get("symbol", ""))
        out.append({"symbol": sym, "base": sym.removesuffix("USDT"), "price": num(r.get("lastPrice")),
                    "change_pct": num(r.get("priceChangePercent")), "high": num(r.get("highPrice")),
                    "low": num(r.get("lowPrice")), "quote_volume": num(r.get("quoteVolume")),
                    "time": utc_iso(float(r["closeTime"]) / 1000) if r.get("closeTime") else None})
    order = {s: i for i, s in enumerate(WATCHLIST)}
    return sorted(out, key=lambda t: order.get(t["symbol"], 999))


def collect(http: Http) -> dict[str, Any]:
    symbols = urllib.parse.quote(json.dumps(WATCHLIST, separators=(",", ":")))
    rows = http.get_json(f"{API}/ticker/24hr?symbols={symbols}")
    return {"tickers": parse_tickers(rows), "source": "Binance spot (data-api.binance.vision)"}


def klines(symbol: str, interval: str = "1h", limit: int = 300, http: Http | None = None) -> list[list[float]]:
    """[[open_ms, o, h, l, c, quote_volume], ...] oldest first."""
    symbol = symbol.upper()
    if not re.fullmatch(r"[A-Z0-9]{5,20}", symbol) or interval not in INTERVALS:
        raise ValueError("bad symbol or interval")
    limit = max(10, min(int(limit), 1000))
    key = (symbol, interval, limit)
    with _KLOCK:
        hit = _KCACHE.get(key)
        if hit and time.time() - hit[0] < 20:
            return hit[1]
    own = http is None
    http = http or LiveHttp(timeout=10)
    try:
        rows = http.get_json(f"{API}/klines?symbol={symbol}&interval={interval}&limit={limit}")
    finally:
        if own:
            http.close()  # type: ignore[union-attr]
    out = [[float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[7])] for r in rows]
    with _KLOCK:
        _KCACHE[key] = (time.time(), out)
    return out


COLLECTORS = [Collector("tickers", "Crypto prices", 60, collect, (API,))]
