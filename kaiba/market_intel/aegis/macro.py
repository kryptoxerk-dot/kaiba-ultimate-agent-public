"""Cross-asset and macro context for the Aegis Terminal: global markets, US macro and crypto macro.

Keyless public data only. Fetching is thin; parsing lives in pure functions over already-fetched
payloads. Each feed fails alone and is reported in ``feeds`` as ``{"name", "ok", "error", "count"}``;
a collector raises only when every feed failed, so ``run_collector`` keeps the last good data stale.
Observation dates are ``YYYY-MM-DD`` calendar days; instants are ISO UTC (``utc_iso``). Every
``history`` is ``[[date_or_iso, value], ...]`` oldest first, for sparklines.

``markets`` (300 s, Yahoo v8 chart, range=1mo interval=1d)::

    {"quotes": [{"symbol", "name", "group", "price", "prev_close", "prev_close_basis", "change",
                 "change_pct", "currency", "market_state", "time", "closes": [float, ...]}],
     "groups": [{"key", "title"}], "feeds": [...]}

  ``group`` is one of us_equities, volatility, rates_fx, commodities, crypto_equities. ``closes``
  holds the last <= 30 daily closes, nulls dropped. With range=1mo, ``chartPreviousClose`` is the
  close *before the month window*, not yesterday's, so ``prev_close`` comes from, in order:
  ``previousClose``, ``fulldayPrice - fulldayChange`` (Yahoo's own day reference, which handles
  futures rolls), the last bar of an earlier exchange-local day, then ``chartPreviousClose``.
  ``prev_close_basis`` names the one used (previousClose | fullday | bars | chartPreviousClose).
  ``market_state`` is null when Yahoo omits it (the chart endpoint usually does).

``macro_fred`` (21600 s, FRED fredgraph.csv from ~3 years back, plus Treasury Fiscal Data DTS)::

    {"series": [{"id", "name", "group", "freq", "transform", "units", "date", "value", "prev_date",
                 "prev", "change", "level", "level_date", "level_units", "history"}],
     "net_liquidity": {"date", "value", "walcl_bn", "walcl_date", "tga_bn", "rrp_bn", "rrp_date",
                       "chg_1w", "chg_1w_from", "chg_4w", "chg_4w_from", "history", "rule"} | null,
     "feeds": [...]}

  ``transform`` is level | yoy (% vs the same month a year earlier) | mom (change vs the prior
  month, PAYEMS in thousands). ``level`` is the latest raw observation (scaled to ``level_units``).
  ``group`` is rates | inflation | labor | liquidity. History keeps <= 60 points. ``series`` also
  carries ``TGA`` (Treasury General Account closing balance, USD bn) from the DTS feed.
  Net liquidity (USD bn) = WALCL - TGA - RRP, evaluated on each TGA business day d: TGA is the
  closing balance on d (millions / 1000); WALCL is the latest weekly Wednesday level dated <= d
  and at most 13 days old (millions / 1000); RRP is the latest RRPONTSYD dated <= d and at most
  7 days old (already billions). Days missing either are dropped. ``chg_1w``/``chg_4w`` compare
  against the latest row dated <= d - 7 / d - 28 days (that row's date is ``chg_*_from``).

``crypto_macro`` (600 s, CoinGecko global, Alternative.me Fear & Greed, DefiLlama stablecoins)::

    {"global": {"total_mcap_usd", "mcap_change_24h_pct", "total_volume_usd", "btc_dominance",
                "eth_dominance", "updated_at"} | null,
     "fear_greed": {"value", "classification", "time", "prev", "change", "history"} | null,
     "stablecoins": {"time", "total_usd", "chg_1d_usd", "chg_1d_pct", "chg_7d_usd", "chg_7d_pct",
                     "chg_30d_usd", "chg_30d_pct", "history"} | null,
     "feeds": [...]}

  Dominance and change values are percents. ``stablecoins.total_usd`` is DefiLlama's
  ``totalCirculatingUSD.peggedUSD`` (USD-pegged supply); the newest DefiLlama day is in progress.
  Its N-day change compares against the latest day at least N days older; history is the last 30.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from typing import Any, NamedTuple
from urllib.parse import quote

from .base import Collector, Http, num, utc_iso

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
TGA_URL = ("https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/dts/"
           "operating_cash_balance?sort=-record_date&page[size]=60"
           "&filter=account_type:eq:Treasury%20General%20Account%20(TGA)%20Closing%20Balance")
COINGECKO_GLOBAL = "https://api.coingecko.com/api/v3/global"
FNG_URL = "https://api.alternative.me/fng/?limit=30"
STABLECOINS_URL = "https://stablecoins.llama.fi/stablecoincharts/all"

GROUPS = (("us_equities", "US equities"), ("volatility", "Volatility"), ("rates_fx", "Rates and FX"),
          ("commodities", "Commodities"), ("crypto_equities", "Crypto equities"))
MARKETS: tuple[tuple[str, str, str], ...] = (
    ("^GSPC", "S&P 500", "us_equities"), ("^NDX", "Nasdaq 100", "us_equities"),
    ("^DJI", "Dow", "us_equities"), ("^RUT", "Russell 2000", "us_equities"),
    ("^VIX", "VIX", "volatility"),
    ("DX-Y.NYB", "DXY", "rates_fx"), ("^TNX", "US 10Y yield", "rates_fx"),
    ("JPY=X", "USD/JPY", "rates_fx"), ("EURUSD=X", "EUR/USD", "rates_fx"), ("IDR=X", "USD/IDR", "rates_fx"),
    ("GC=F", "Gold", "commodities"), ("CL=F", "WTI crude", "commodities"),
    ("MSTR", "Strategy", "crypto_equities"), ("COIN", "Coinbase", "crypto_equities"),
    ("IBIT", "iShares Bitcoin Trust", "crypto_equities"),
)
SPARK_CLOSES = 30


class SeriesSpec(NamedTuple):
    id: str
    name: str
    group: str
    freq: str
    transform: str      # level | yoy | mom
    units: str          # units of "value" after the transform
    level_units: str    # units of "level" (the raw observation times scale)
    scale: float = 1.0


FRED_SERIES: tuple[SeriesSpec, ...] = (
    SeriesSpec("DFF", "Effective fed funds", "rates", "daily", "level", "%", "%"),
    SeriesSpec("DGS2", "US 2Y Treasury", "rates", "daily", "level", "%", "%"),
    SeriesSpec("DGS10", "US 10Y Treasury", "rates", "daily", "level", "%", "%"),
    SeriesSpec("T10Y2Y", "10Y minus 2Y", "rates", "daily", "level", "pp", "pp"),
    SeriesSpec("CPIAUCSL", "CPI", "inflation", "monthly", "yoy", "% YoY", "index 1982-84=100"),
    SeriesSpec("CPILFESL", "Core CPI", "inflation", "monthly", "yoy", "% YoY", "index 1982-84=100"),
    SeriesSpec("PCEPILFE", "Core PCE prices", "inflation", "monthly", "yoy", "% YoY", "index 2017=100"),
    SeriesSpec("UNRATE", "Unemployment rate", "labor", "monthly", "level", "%", "%"),
    SeriesSpec("PAYEMS", "Nonfarm payrolls", "labor", "monthly", "mom", "k jobs m/m", "k jobs"),
    SeriesSpec("WALCL", "Fed balance sheet", "liquidity", "weekly", "level", "USD bn", "USD bn", 0.001),
    SeriesSpec("RRPONTSYD", "Overnight reverse repo", "liquidity", "daily", "level", "USD bn", "USD bn"),
    SeriesSpec("M2SL", "M2 money supply", "liquidity", "monthly", "yoy", "% YoY", "USD bn"),
)
TGA_SPEC = SeriesSpec("TGA", "Treasury General Account", "liquidity", "daily", "level", "USD bn", "USD bn", 0.001)
FRED_LOOKBACK_DAYS = 3 * 365 + 1
HISTORY = 60
WALCL_MAX_AGE_D = 13          # one missed weekly H.4.1 print is tolerated, two are not
RRP_MAX_AGE_D = 7
NET_LIQ_RULE = ("WALCL - TGA - RRP in USD bn on each TGA day d: TGA closing balance on d; WALCL = latest "
                f"weekly level dated <= d (<= {WALCL_MAX_AGE_D} d old); RRP = latest RRPONTSYD dated <= d "
                f"(<= {RRP_MAX_AGE_D} d old).")

Point = tuple[str, float]


# ------------------------------------------------------------------ shared
def _r(x: float | None, nd: int = 6) -> float | None:
    return None if x is None else round(x, nd)


def _pct(new: float | None, old: float | None) -> float | None:
    return None if new is None or not old else (new / old - 1.0) * 100.0


def _gather(jobs: list[tuple[str, Callable[[], tuple[Any, int]]]],
            workers: int = 4) -> list[tuple[Any, dict[str, Any]]]:
    """Run ``(name, fn)`` jobs (``fn`` returns ``(value, count)``) with per-feed failure isolation."""
    def one(job: tuple[str, Callable[[], tuple[Any, int]]]) -> tuple[Any, dict[str, Any]]:
        name, fn = job
        try:
            value, count = fn()
        except Exception as e:  # noqa: BLE001 - one dead feed must not blank the panel
            return None, {"name": name, "ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}", "count": 0}
        return value, {"name": name, "ok": True, "error": None, "count": count}

    if not jobs:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs))), thread_name_prefix="intel-macro") as pool:
        return list(pool.map(one, jobs))


def _require_any(feeds: list[dict[str, Any]]) -> None:
    if feeds and not any(f["ok"] for f in feeds):
        raise RuntimeError("all feeds failed: " + "; ".join(f"{f['name']}: {f['error']}" for f in feeds[:3]))


# ------------------------------------------------------------------ markets (Yahoo)
def yahoo_url(symbol: str) -> str:
    return f"{YAHOO_CHART}{quote(symbol, safe='')}?range=1mo&interval=1d"


def _local_day(ts: float, gmtoffset: float) -> str:
    return datetime.fromtimestamp(ts + gmtoffset, UTC).date().isoformat()


def yahoo_prev_close(meta: dict[str, Any], bars: list[tuple[int, float]]) -> tuple[float | None, str | None]:
    pc = num(meta.get("previousClose"))
    if pc:
        return pc, "previousClose"
    fp, fc = num(meta.get("fulldayPrice")), num(meta.get("fulldayChange"))
    if fp is not None and fc is not None and fp - fc > 0:
        return fp - fc, "fullday"
    rmt, off = num(meta.get("regularMarketTime")), num(meta.get("gmtoffset")) or 0.0
    if rmt is not None:
        today = _local_day(rmt, off)
        earlier = [c for t, c in bars if _local_day(t, off) < today]
        if earlier:
            return earlier[-1], "bars"
    cpc = num(meta.get("chartPreviousClose"))
    return (cpc, "chartPreviousClose") if cpc else (None, None)


def parse_yahoo_chart(doc: Any, symbol: str, name: str, group: str) -> dict[str, Any]:
    chart = doc.get("chart") if isinstance(doc, dict) else None
    if not isinstance(chart, dict):
        raise TypeError("yahoo: no chart object")
    err = chart.get("error")
    if err:
        raise ValueError(f"yahoo: {err.get('code')}: {err.get('description')}" if isinstance(err, dict) else f"yahoo: {err}")
    res = (chart.get("result") or [None])[0]
    if not isinstance(res, dict):
        raise TypeError("yahoo: empty result")
    meta = res.get("meta") or {}
    price = num(meta.get("regularMarketPrice"))
    if price is None:
        raise ValueError("yahoo: no regularMarketPrice")
    quotes = (res.get("indicators") or {}).get("quote") or [{}]
    closes = (quotes[0] or {}).get("close") or []
    bars = [(int(t), c) for t, c in ((t, num(c)) for t, c in zip(res.get("timestamp") or [], closes)) if c is not None]
    prev, basis = yahoo_prev_close(meta, bars)
    change = None if prev is None else price - prev
    return {"symbol": symbol, "name": name, "group": group, "price": price, "prev_close": _r(prev),
            "prev_close_basis": basis, "change": _r(change), "change_pct": _r(_pct(price, prev), 4),
            "currency": meta.get("currency"), "market_state": meta.get("marketState"),
            "time": utc_iso(num(meta.get("regularMarketTime"))),
            "closes": [_r(c) for _, c in bars[-SPARK_CLOSES:]]}


def collect_markets(http: Http) -> dict[str, Any]:
    def job(sym: str, name: str, group: str) -> Callable[[], tuple[Any, int]]:
        def fn() -> tuple[Any, int]:
            q = parse_yahoo_chart(http.get_json(yahoo_url(sym)), sym, name, group)
            return q, len(q["closes"])
        return fn

    results = _gather([(sym, job(sym, name, group)) for sym, name, group in MARKETS])
    feeds = [f for _, f in results]
    _require_any(feeds)
    return {"quotes": [q for q, _ in results if q is not None],
            "groups": [{"key": k, "title": t} for k, t in GROUPS], "feeds": feeds}


# ------------------------------------------------------------------ US macro (FRED + Treasury)
def fred_url(series_id: str, cosd: str) -> str:
    return f"{FRED_CSV}?id={series_id}&cosd={cosd}"


def parse_fred_csv(text: str, series_id: str) -> list[Point]:
    """``observation_date,<ID>`` rows to ascending ``(date, value)``; ``.`` and empty values are skipped."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    head = [h.strip().strip('"') for h in lines[0].split(",")] if lines else []
    if len(head) < 2 or head[1].upper() != series_id.upper():
        raise ValueError(f"fred {series_id}: unexpected header {(lines[0] if lines else '')[:80]!r}")
    out = []
    for ln in lines[1:]:
        d, _, v = ln.partition(",")
        v = v.strip().strip('"')
        x = None if v == "." else num(v)
        if x is not None and len(d) == 10 and d[4] == "-" and d[7] == "-":
            out.append((d, x))
    return sorted(out)


def parse_tga(doc: Any) -> list[Point]:
    """DTS operating cash balance rows to ascending ``(date, USD millions)`` TGA closing balances.

    For the "TGA Closing Balance" row the Fiscal Data API leaves ``close_today_bal`` as "null" and
    puts the day's closing balance in ``open_today_bal``; a numeric ``close_today_bal`` wins."""
    rows = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise TypeError("treasury: no data list")
    out: dict[str, float] = {}
    for r in rows:
        if "Closing Balance" not in str(r.get("account_type", "Closing Balance")):
            continue
        v = num(r.get("close_today_bal"))
        v = num(r.get("open_today_bal")) if v is None else v
        d = str(r.get("record_date") or "")
        if v is not None and len(d) == 10:
            out[d] = v
    if not out:
        raise ValueError("treasury: no TGA closing balances")
    return sorted(out.items())


def yoy(points: list[Point]) -> list[Point]:
    """Percent change versus the observation dated exactly one year earlier (gaps stay gaps)."""
    by = dict(points)
    out = []
    for d, v in points:
        base = by.get(f"{int(d[:4]) - 1:04d}{d[4:]}")
        if base:
            out.append((d, (v / base - 1.0) * 100.0))
    return out


def _prior_month(d: str) -> str:
    y, m = int(d[:4]), int(d[5:7])
    y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return f"{y:04d}-{m:02d}{d[7:]}"


def monthly_change(points: list[Point]) -> list[Point]:
    """Change versus the observation dated exactly one month earlier (a missing month breaks the pair)."""
    by = dict(points)
    return [(d, v - by[_prior_month(d)]) for d, v in points if _prior_month(d) in by]


def summarize_series(spec: SeriesSpec, raw: list[Point], history: int = HISTORY) -> dict[str, Any]:
    scaled = [(d, v * spec.scale) for d, v in raw]
    pts = yoy(scaled) if spec.transform == "yoy" else monthly_change(scaled) if spec.transform == "mom" else scaled
    if not pts:
        raise ValueError(f"{spec.id}: no usable observations")
    (d, v), prev = pts[-1], (pts[-2] if len(pts) > 1 else None)
    return {"id": spec.id, "name": spec.name, "group": spec.group, "freq": spec.freq, "transform": spec.transform,
            "units": spec.units, "date": d, "value": _r(v, 4), "prev_date": prev[0] if prev else None,
            "prev": _r(prev[1], 4) if prev else None, "change": _r(v - prev[1], 4) if prev else None,
            "level": _r(scaled[-1][1], 4), "level_date": scaled[-1][0], "level_units": spec.level_units,
            "history": [[pd, _r(pv, 4)] for pd, pv in pts[-history:]]}


def asof(points: list[Point], day: str, max_age_days: int) -> Point | None:
    """Latest point dated on or before ``day`` and at most ``max_age_days`` older (``points`` ascending)."""
    i = bisect_right(points, (day, float("inf"))) - 1
    if i < 0:
        return None
    d, v = points[i]
    return (d, v) if (date.fromisoformat(day) - date.fromisoformat(d)).days <= max_age_days else None


def net_liquidity(walcl_m: list[Point], tga_m: list[Point], rrp_bn: list[Point],
                  history: int = HISTORY) -> dict[str, Any] | None:
    """Fed net liquidity in USD bn on TGA days; see the module docstring for the as-of rules."""
    rows = []
    for d, tga in tga_m:
        w, r = asof(walcl_m, d, WALCL_MAX_AGE_D), asof(rrp_bn, d, RRP_MAX_AGE_D)
        if w is not None and r is not None:
            rows.append((d, w[1] / 1000 - tga / 1000 - r[1], w, tga / 1000, r))
    if not rows:
        return None
    d, value, w, tga_bn, r = rows[-1]

    def change(days: int) -> tuple[float | None, str | None]:
        target = (date.fromisoformat(d) - timedelta(days=days)).isoformat()
        base = [x for x in rows if x[0] <= target]
        return (value - base[-1][1], base[-1][0]) if base else (None, None)

    (c1, f1), (c4, f4) = change(7), change(28)
    return {"date": d, "value": _r(value, 3), "walcl_bn": _r(w[1] / 1000, 3), "walcl_date": w[0],
            "tga_bn": _r(tga_bn, 3), "rrp_bn": _r(r[1], 3), "rrp_date": r[0],
            "chg_1w": _r(c1, 3), "chg_1w_from": f1, "chg_4w": _r(c4, 3), "chg_4w_from": f4,
            "history": [[x[0], _r(x[1], 3)] for x in rows[-history:]], "rule": NET_LIQ_RULE}


def collect_fred(http: Http, today: date | None = None) -> dict[str, Any]:
    cosd = ((today or datetime.now(UTC).date()) - timedelta(days=FRED_LOOKBACK_DAYS)).isoformat()

    def job(spec: SeriesSpec, fetch: Callable[[], list[Point]]) -> Callable[[], tuple[Any, int]]:
        def fn() -> tuple[Any, int]:
            raw = fetch()
            return (raw, summarize_series(spec, raw)), len(raw)
        return fn

    jobs = [(s.id, job(s, lambda s=s: parse_fred_csv(http.get_text(fred_url(s.id, cosd)), s.id))) for s in FRED_SERIES]
    jobs.append((TGA_SPEC.id, job(TGA_SPEC, lambda: parse_tga(http.get_json(TGA_URL)))))
    results = _gather(jobs)
    feeds = [f for _, f in results]
    _require_any(feeds)
    got = {f["name"]: v for v, f in results if v is not None}
    liq = None
    if all(k in got for k in ("WALCL", "TGA", "RRPONTSYD")):
        liq = net_liquidity(got["WALCL"][0], got["TGA"][0], got["RRPONTSYD"][0])
    return {"series": [got[n][1] for n in [s.id for s in FRED_SERIES] + [TGA_SPEC.id] if n in got],
            "net_liquidity": liq, "feeds": feeds}


# ------------------------------------------------------------------ crypto macro
def parse_coingecko_global(doc: Any) -> dict[str, Any]:
    d = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(d, dict):
        raise TypeError("coingecko: no data object")
    mcap = num((d.get("total_market_cap") or {}).get("usd"))
    if mcap is None:
        raise ValueError("coingecko: no total_market_cap.usd")
    dom = d.get("market_cap_percentage") or {}
    return {"total_mcap_usd": mcap, "mcap_change_24h_pct": _r(num(d.get("market_cap_change_percentage_24h_usd")), 4),
            "total_volume_usd": num((d.get("total_volume") or {}).get("usd")),
            "btc_dominance": _r(num(dom.get("btc")), 4), "eth_dominance": _r(num(dom.get("eth")), 4),
            "updated_at": utc_iso(num(d.get("updated_at")))}


def parse_fng(doc: Any) -> dict[str, Any]:
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
        raise TypeError("fear_greed: no data list")
    err = (doc.get("metadata") or {}).get("error")
    if err:
        raise ValueError(f"fear_greed: {err}")
    pts = sorted((int(ts), int(v), str(r.get("value_classification") or "")) for r in doc["data"]
                 if isinstance(r, dict) and (ts := num(r.get("timestamp"))) is not None
                 and (v := num(r.get("value"))) is not None)
    if not pts:
        raise ValueError("fear_greed: no points")
    ts, v, cls = pts[-1]
    prev = pts[-2][1] if len(pts) > 1 else None
    return {"value": v, "classification": cls, "time": utc_iso(ts), "prev": prev,
            "change": None if prev is None else v - prev, "history": [[utc_iso(t), x] for t, x, _ in pts]}


def parse_stablecoins(doc: Any, tail: int = 64, history: int = 30) -> dict[str, Any]:
    """DefiLlama's daily ``stablecoincharts/all`` list; only the last ``tail`` rows are read."""
    if not isinstance(doc, list):
        raise TypeError("stablecoins: expected a list")
    pts = sorted((int(ts), v) for r in doc[-tail:] if isinstance(r, dict)
                 and (ts := num(r.get("date"))) is not None
                 and (v := num((r.get("totalCirculatingUSD") or {}).get("peggedUSD"))) is not None)
    if not pts:
        raise ValueError("stablecoins: no peggedUSD points")
    ts, v = pts[-1]
    out: dict[str, Any] = {"time": utc_iso(ts), "total_usd": _r(v, 2)}
    for label, days in (("1d", 1), ("7d", 7), ("30d", 30)):
        base = [p for p in pts if p[0] <= ts - days * 86400]
        b = base[-1][1] if base else None
        out[f"chg_{label}_usd"] = None if b is None else _r(v - b, 2)
        out[f"chg_{label}_pct"] = _r(_pct(v, b), 4)
    out["history"] = [[utc_iso(t), _r(x, 2)] for t, x in pts[-history:]]
    return out


def collect_crypto_macro(http: Http) -> dict[str, Any]:
    def job(url: str, parse: Callable[[Any], dict[str, Any]]) -> Callable[[], tuple[Any, int]]:
        def fn() -> tuple[Any, int]:
            v = parse(http.get_json(url))
            return v, len(v["history"]) if "history" in v else 1
        return fn

    names = ("global", "fear_greed", "stablecoins")
    results = _gather([("coingecko_global", job(COINGECKO_GLOBAL, parse_coingecko_global)),
                       ("fear_greed", job(FNG_URL, parse_fng)),
                       ("defillama_stablecoins", job(STABLECOINS_URL, parse_stablecoins))])
    feeds = [f for _, f in results]
    _require_any(feeds)
    return {**{n: v for n, (v, _) in zip(names, results)}, "feeds": feeds}


COLLECTORS: list[Collector] = [
    Collector("markets", "Global markets", 300, collect_markets, ("Yahoo Finance chart API",)),
    Collector("macro_fred", "US macro and Fed liquidity", 21600, collect_fred,
              ("FRED, Federal Reserve Bank of St. Louis", "US Treasury Fiscal Data (Daily Treasury Statement)")),
    Collector("crypto_macro", "Crypto macro", 600, collect_crypto_macro,
              ("CoinGecko", "Alternative.me Fear & Greed", "DefiLlama stablecoins")),
]
