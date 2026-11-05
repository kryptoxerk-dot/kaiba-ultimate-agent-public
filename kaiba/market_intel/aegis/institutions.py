"""Institutional positioning: US spot ETF flows, CME futures positioning (CFTC COT) and public-company
treasuries. Keyless public data only; fetching is thin and every parser is a pure function.

Output schemas (the ``data`` field of each cache document). Each multi-feed collector also carries
``feeds: [{"name", "ok", "error", "count"}]``; a failed asset/contract/coin keeps its slot with
``"ok": False, "error": str`` and is left out of the verdict. If every feed fails the collector raises,
so ``run_collector`` keeps the last good data and marks it stale.

``etf_flows`` (10800 s; one Farside page per asset per run, two only when the first fails)::

    {"attribution": "Data: Farside Investors (farside.co.uk)", "units": "US$m", "verdict": str,
     "assets": {"BTC" | "ETH": {
        "asset", "ok", "error", "source_url", "coverage": "all_data" | "month_to_date",
        "funds": [ticker, ...],                                   # Farside column order
        "rows": [{"date": "YYYY-MM-DD", "total": float|None, "flows": {ticker: float|None}}],  # last 30, oldest first
        "latest": {"date", "total", "partial": bool, "missing": [ticker]},  # missing = '-' today, reported yesterday
        "sum_5d", "sum_20d", "mtd", "mtd_days", "cumulative_total",      # US$m; cumulative = Farside "Total" row
        "streak": {"days": int, "sign": "inflow" | "outflow" | "flat"},
        "top_flows": [{"ticker", "flow"}],                        # latest day, largest |flow| first, max 5
        "verdict": str}}}

``cot`` (43200 s; CFTC Traders in Financial Futures, futures only)::

    {"note": str, "verdict": str,
     "contracts": {"btc" | "micro_btc" | "eth" | "micro_eth": {
        "key", "label", "code", "name", "coin", "coin_per_contract", "contract_units", "units_check": bool,
        "ok", "error", "verdict",
        "reports": [{"report_date": "YYYY-MM-DD", "open_interest", "oi_change",   # last 12, newest first
                     "groups": {"asset_mgr" | "lev_funds" | "dealer" | "other_rept" | "nonrept": {
                        "long", "short", "net", "net_change", "net_pct_oi", "net_coins", "net_change_coins"}}}]}},
     "by_coin": {"BTC" | "ETH": {"report_date", "contracts": [key], "groups": {group: {"net_coins", "net_change_coins"}}}}}

``treasuries`` (43200 s; CoinGecko, one history entry per coin per UTC day in ``treasuries_history.json``)::

    {"attribution": "Data: CoinGecko", "verdict": str,
     "coins": {"bitcoin" | "ethereum": {
        "coin", "symbol", "ok", "error", "total_holdings", "total_value_usd", "market_cap_dominance",
        "implied_price", "company_count",
        "companies": [{"rank", "name", "symbol", "country", "holdings", "pct_supply", "current_value_usd",
                       "entry_value_usd": float|None, "avg_cost": float|None, "unrealized_usd": float|None}],  # top 15
        "changes": {"prev" | "7d" | "30d": {"from_date", "days", "holdings", "change", "change_pct"} | None},
        "history_note": str | None,        # "not enough history yet ..." while a window is missing
        "verdict": str}}}
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode

from .base import Collector, Http, _write_atomic, cache_dir, num, utc_iso

FARSIDE_ATTRIBUTION = "Data: Farside Investors (farside.co.uk)"
# The /btc/ and /eth/ pages hold only the current month; the "all daily data" pages they link to hold the
# full history in the same table layout, so they come first and the month pages are the fallback.
FARSIDE = {
    "BTC": ("https://farside.co.uk/bitcoin-etf-flow-all-data/", "https://farside.co.uk/btc/"),
    "ETH": ("https://farside.co.uk/ethereum-etf-flow-all-data/", "https://farside.co.uk/eth/"),
}
CFTC_TFF = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
# (key, label, CFTC contract market code, coin, coins per contract, market_and_exchange_names on 2026-10-20).
# Queried by code: the names carry odd spacing ("MICRO ETHER  - ...") and have been renamed before.
COT_CONTRACTS = (
    ("btc", "CME BTC", "133741", "BTC", 5.0, "BITCOIN - CHICAGO MERCANTILE EXCHANGE"),
    ("micro_btc", "CME Micro BTC", "133742", "BTC", 0.1, "MICRO BITCOIN - CHICAGO MERCANTILE EXCHANGE"),
    ("eth", "CME ETH", "146021", "ETH", 50.0, "ETHER CASH SETTLED - CHICAGO MERCANTILE EXCHANGE"),
    ("micro_eth", "CME Micro ETH", "146022", "ETH", 0.1, "MICRO ETHER  - CHICAGO MERCANTILE EXCHANGE"),
)
# group -> (long field, short field, long change field, short change field, plain-English name)
COT_GROUPS = {
    "asset_mgr": ("asset_mgr_positions_long", "asset_mgr_positions_short",
                  "change_in_asset_mgr_long", "change_in_asset_mgr_short", "asset managers"),
    "lev_funds": ("lev_money_positions_long", "lev_money_positions_short",
                  "change_in_lev_money_long", "change_in_lev_money_short", "leveraged funds"),
    "dealer": ("dealer_positions_long_all", "dealer_positions_short_all",
               "change_in_dealer_long_all", "change_in_dealer_short_all", "dealers"),
    "other_rept": ("other_rept_positions_long", "other_rept_positions_short",
                   "change_in_other_rept_long", "change_in_other_rept_short", "other reportables"),
    "nonrept": ("nonrept_positions_long_all", "nonrept_positions_short_all",
                "change_in_nonrept_long_all", "change_in_nonrept_short_all", "non-reportables"),
}
COT_NOTE = ("CFTC Commitments of Traders (Traders in Financial Futures, futures only): positions are as of "
            "Tuesday and published Friday; net = long - short, w/w from CFTC's change fields.")
COINGECKO = "https://api.coingecko.com/api/v3/companies/public_treasury/{coin}"
TREASURY_COINS = (("bitcoin", "BTC"), ("ethereum", "ETH"))
HISTORY_FILE = "treasuries_history.json"
HISTORY_KEEP = 400

_MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTHS = {m.lower(): i for i, m in enumerate(_MON, 1)}
_WD = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_DATE = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3})[A-Za-z]*\s+(\d{4})$")
_TICKER = re.compile(r"^[A-Z][A-Z0-9]{1,5}$")


# ------------------------------------------------------------------ formatting
def fmt_musd(m: float | None) -> str:
    """US$ millions as '+$412.3M' / '-$1.21B'."""
    if m is None:
        return "n/a"
    sign = "+" if m > 0 else "-" if m < 0 else ""
    a = abs(m)
    return f"{sign}${a / 1000:.2f}B" if a >= 1000 else f"{sign}${a:.1f}M"


def _signed(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+,.0f}"


def _r(v: float | None, nd: int = 1) -> float | None:
    return None if v is None else round(v, nd)


def _day(d: date) -> str:
    return f"{d.day} {_MON[d.month - 1]}"


# ------------------------------------------------------------------ Farside ETF flows
def parse_farside_date(text: str) -> date | None:
    m = _DATE.match(text.strip())
    if not m or m.group(2).lower() not in _MONTHS:
        return None
    try:
        return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
    except ValueError:
        return None


class _TableRows(HTMLParser):
    """Cell texts of every row of every ``<table class="etf">`` (tolerates unclosed cells and rows)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._depth = 0                     # table nesting depth inside an etf table
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def _end_cell(self) -> None:
        if self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
        self._cell = None

    def _end_row(self) -> None:
        self._end_cell()
        if self._row is not None:
            self.rows.append(self._row)
        self._row = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            if self._depth or "etf" in (dict(attrs).get("class") or "").split():
                self._depth += 1
        elif self._depth == 1 and tag == "tr":
            self._end_row()
            self._row = []
        elif self._depth == 1 and tag in ("td", "th") and self._row is not None:
            self._end_cell()
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if not self._depth:
            return
        if tag == "table":
            if self._depth == 1:
                self._end_row()
            self._depth -= 1
        elif self._depth == 1 and tag == "tr":
            self._end_row()
        elif self._depth == 1 and tag in ("td", "th"):
            self._end_cell()

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def parse_farside(html_text: str) -> dict[str, Any]:
    """The Farside flow table: ``{"funds", "rows" (oldest first, one per date), "cumulative"}``.

    Data rows are rows whose first cell is a date ('17 Oct 2026'); summary rows (Total, Average, Maximum,
    Minimum, Fee, Seed) never qualify. '(12.5)' is -12.5 and '-' is None. A dated row where every fund is
    '-' (a market holiday placeholder) is dropped. Raises ``ValueError`` when no flow table is found."""
    p = _TableRows()
    p.feed(html_text)
    p.close()
    first_data = next((i for i, r in enumerate(p.rows) if r and parse_farside_date(r[0])), None)
    if first_data is None:
        raise ValueError("no Farside flow table with dated rows found")
    head = p.rows[:first_data]
    tick_row = max(head, key=lambda r: sum(1 for c in r[1:] if _TICKER.match(c)), default=[])
    total_idx = next((r.index("Total") for r in head if "Total" in r[1:]), None)
    width = max(len(r) for r in p.rows[first_data:])
    if total_idx is None:
        total_idx = width - 1
    funds = [(i, c) for i, c in enumerate(tick_row) if i and i != total_idx and _TICKER.match(c)]
    if not funds:
        raise ValueError("Farside header has no fund tickers")
    by_date: dict[date, dict[str, Any]] = {}
    cumulative: dict[str, float | None] = {}
    for r in p.rows[first_data:]:
        if not r:
            continue
        d = parse_farside_date(r[0])
        if d is None:
            if r[0].strip().lower() == "total":
                cumulative = {t: num(r[i]) if i < len(r) else None for i, t in funds}
                cumulative["Total"] = num(r[total_idx]) if total_idx < len(r) else None
            continue
        flows = {t: (num(r[i]) if i < len(r) else None) for i, t in funds}
        vals = [v for v in flows.values() if v is not None]
        if not vals:
            continue
        total = num(r[total_idx]) if total_idx < len(r) else None
        by_date[d] = {"date": d.isoformat(), "total": total if total is not None else round(sum(vals), 1),
                      "flows": flows}
    if not by_date:
        raise ValueError("Farside table has no rows with data")
    return {"funds": [t for _, t in funds], "rows": [by_date[d] for d in sorted(by_date)],
            "cumulative": cumulative}


def _sum(rows: list[dict[str, Any]]) -> float | None:
    vals = [r["total"] for r in rows if r.get("total") is not None]
    return round(sum(vals), 1) if vals else None


def flow_streak(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Consecutive latest days with the same sign of the total (a zero or missing total ends it)."""
    sign, days = "flat", 0
    for r in reversed(rows):
        t = r.get("total")
        s = "inflow" if t and t > 0 else "outflow" if t and t < 0 else "flat"
        if s == "flat" or (days and s != sign):
            break
        sign, days = s, days + 1
    return {"days": days, "sign": sign}


def summarize_flows(asset: str, table: dict[str, Any], keep: int = 30) -> dict[str, Any]:
    rows, funds = table["rows"], table["funds"]
    latest = rows[-1]
    prev = rows[-2] if len(rows) > 1 else None
    missing = [t for t in funds if latest["flows"].get(t) is None and prev and prev["flows"].get(t) is not None]
    month = latest["date"][:7]
    mtd_rows = [r for r in rows if r["date"][:7] == month]
    streak = flow_streak(rows)
    top = sorted(((t, v) for t, v in latest["flows"].items() if v), key=lambda kv: -abs(kv[1]))[:5]
    d = date.fromisoformat(latest["date"])
    when = f"on {_day(d)}" + (f" ({', '.join(missing)} not yet reported)" if missing else "")
    run = f"{streak['days']}-day {streak['sign']} streak" if streak["days"] else "no streak (flat day)"
    out = {
        "asset": asset, "funds": funds, "rows": rows[-keep:],
        "latest": {"date": latest["date"], "total": latest["total"], "partial": bool(missing), "missing": missing},
        "sum_5d": _sum(rows[-5:]), "sum_20d": _sum(rows[-20:]), "mtd": _sum(mtd_rows), "mtd_days": len(mtd_rows),
        "cumulative_total": table.get("cumulative", {}).get("Total"),
        "streak": streak, "top_flows": [{"ticker": t, "flow": v} for t, v in top],
    }
    out["verdict"] = (f"{asset} ETFs: {fmt_musd(latest['total'])} {when}, "
                      f"5-day {fmt_musd(out['sum_5d'])}, {run}")
    return out


def _farside_asset(http: Http, asset: str) -> dict[str, Any]:
    all_url, month_url = FARSIDE[asset]
    try:
        table, url, coverage = parse_farside(http.get_text(all_url)), all_url, "all_data"
    except Exception as first:  # noqa: BLE001 - fall back to the month page, keep the reason
        try:
            table, url, coverage = parse_farside(http.get_text(month_url)), month_url, "month_to_date"
        except Exception as second:
            raise RuntimeError(f"all-data page: {type(first).__name__}: {str(first)[:150]}; "
                               f"month page: {type(second).__name__}: {str(second)[:150]}") from second
    return {"ok": True, "error": None, "source_url": url, "coverage": coverage, **summarize_flows(asset, table)}


def collect_etf_flows(http: Http) -> dict[str, Any]:
    assets: dict[str, Any] = {}
    feeds = []
    for asset in FARSIDE:
        try:
            a = _farside_asset(http, asset)
            feeds.append({"name": f"farside_{asset.lower()}", "ok": True, "error": None, "count": len(a["rows"])})
        except Exception as e:  # noqa: BLE001 - one asset failing never blanks the other
            err = f"{type(e).__name__}: {str(e)[:300]}"
            a = {"asset": asset, "ok": False, "error": err}
            feeds.append({"name": f"farside_{asset.lower()}", "ok": False, "error": err, "count": 0})
        assets[asset] = a
    if not any(f["ok"] for f in feeds):
        raise RuntimeError("; ".join(f"{f['name']}: {f['error']}" for f in feeds))
    return {"attribution": FARSIDE_ATTRIBUTION, "units": "US$m", "feeds": feeds, "assets": assets,
            "verdict": "; ".join(a["verdict"] for a in assets.values() if a.get("ok"))}


# ------------------------------------------------------------------ CFTC COT (TFF)
def cot_url(code: str, limit: int = 12) -> str:
    return CFTC_TFF + "?" + urlencode({"$where": f"cftc_contract_market_code='{code}'",
                                       "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": str(limit)})


def cot_report(row: dict[str, Any], coin_per_contract: float) -> dict[str, Any]:
    oi = num(row.get("open_interest_all"))
    groups = {}
    for g, (lf, sf, clf, csf, _name) in COT_GROUPS.items():
        lo, sh, cl, cs = (num(row.get(f)) for f in (lf, sf, clf, csf))
        net = None if lo is None or sh is None else lo - sh
        chg = None if cl is None or cs is None else cl - cs
        groups[g] = {"long": lo, "short": sh, "net": net, "net_change": chg,
                     "net_pct_oi": round(net / oi * 100, 2) if net is not None and oi else None,
                     "net_coins": _r(net * coin_per_contract if net is not None else None, 2),
                     "net_change_coins": _r(chg * coin_per_contract if chg is not None else None, 2)}
    return {"report_date": str(row.get("report_date_as_yyyy_mm_dd") or "")[:10], "open_interest": oi,
            "oi_change": num(row.get("change_in_open_interest_all")), "groups": groups}


def _units_number(text: str) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", (text or "").replace(",", ""))
    return float(m.group(0)) if m else None


def _position_phrase(name: str, g: dict[str, Any], coin: str) -> str:
    net = g["net"]
    if net is None:
        return f"{name} n/a"
    side = "long" if net > 0 else "short" if net < 0 else "flat"
    return (f"{name} net {side} {abs(net):,.0f} contracts ({_signed(g['net_change'])} w/w; "
            f"{abs(g['net_coins'] or 0):,.0f} {coin})")


def summarize_cot(spec: tuple, rows: list[dict[str, Any]], keep: int = 12) -> dict[str, Any]:
    key, label, code, coin, size, name = spec
    rows = sorted((r for r in rows if r.get("report_date_as_yyyy_mm_dd")),
                  key=lambda r: r["report_date_as_yyyy_mm_dd"], reverse=True)[:keep]
    if not rows:
        raise ValueError(f"no COT rows for {label} ({code})")
    units = str(rows[0].get("contract_units") or "")
    reports = [cot_report(r, size) for r in rows]
    latest = reports[0]
    d = date.fromisoformat(latest["report_date"])
    verdict = (f"{label}: " + "; ".join(_position_phrase(COT_GROUPS[g][4], latest["groups"][g], coin)
                                         for g in ("asset_mgr", "lev_funds"))
               + f" (as of {_WD[d.weekday()]} {_day(d)})")
    return {"key": key, "label": label, "code": code, "name": rows[0].get("market_and_exchange_names") or name,
            "coin": coin, "coin_per_contract": size, "contract_units": units,
            "units_check": _units_number(units) == size, "reports": reports, "verdict": verdict}


def cot_by_coin(contracts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Net coin exposure per group summed over a coin's contracts that share the latest report date."""
    out: dict[str, Any] = {}
    for coin in sorted({c["coin"] for c in contracts.values() if c.get("ok")}):
        cs = [c for c in contracts.values() if c.get("ok") and c["coin"] == coin]
        day = max(c["reports"][0]["report_date"] for c in cs)
        cs = [c for c in cs if c["reports"][0]["report_date"] == day]
        groups = {}
        for g in COT_GROUPS:
            nets = [c["reports"][0]["groups"][g]["net_coins"] for c in cs]
            chgs = [c["reports"][0]["groups"][g]["net_change_coins"] for c in cs]
            groups[g] = {"net_coins": _r(sum(v for v in nets if v is not None), 2),
                         "net_change_coins": _r(sum(v for v in chgs if v is not None), 2)}
        out[coin] = {"report_date": day, "contracts": [c["key"] for c in cs], "groups": groups}
    return out


def collect_cot(http: Http) -> dict[str, Any]:
    contracts: dict[str, Any] = {}
    feeds = []
    for spec in COT_CONTRACTS:
        key, label, code, coin, size, name = spec
        try:
            rows = http.get_json(cot_url(code))
            if not isinstance(rows, list):
                raise TypeError(f"unexpected CFTC response: {type(rows).__name__}")
            c = {"ok": True, "error": None, **summarize_cot(spec, rows)}
            feeds.append({"name": f"cftc_{key}", "ok": True, "error": None, "count": len(c["reports"])})
        except Exception as e:  # noqa: BLE001 - one contract failing never blanks the others
            err = f"{type(e).__name__}: {str(e)[:300]}"
            c = {"key": key, "label": label, "code": code, "name": name, "coin": coin, "coin_per_contract": size,
                 "ok": False, "error": err, "reports": []}
            feeds.append({"name": f"cftc_{key}", "ok": False, "error": err, "count": 0})
        contracts[key] = c
    if not any(f["ok"] for f in feeds):
        raise RuntimeError("; ".join(f"{f['name']}: {f['error']}" for f in feeds))
    main = [contracts[k]["verdict"] for k in ("btc", "eth") if contracts[k].get("ok")]
    return {"note": COT_NOTE, "feeds": feeds, "contracts": contracts, "by_coin": cot_by_coin(contracts),
            "verdict": " | ".join(main or [c["verdict"] for c in contracts.values() if c.get("ok")])}


# ------------------------------------------------------------------ CoinGecko public treasuries
def summarize_treasury(coin: str, symbol: str, doc: dict[str, Any], top: int = 15) -> dict[str, Any]:
    if not isinstance(doc, dict) or not isinstance(doc.get("companies"), list):
        raise TypeError("unexpected CoinGecko treasury response")
    hold, value = num(doc.get("total_holdings")), num(doc.get("total_value_usd"))
    comps = sorted((c for c in doc["companies"] if isinstance(c, dict)),
                   key=lambda c: -(num(c.get("total_holdings")) or 0))
    rows = []
    for i, c in enumerate(comps[:top], 1):
        h, cur = num(c.get("total_holdings")), num(c.get("total_current_value_usd"))
        entry = num(c.get("total_entry_value_usd")) or None       # CoinGecko reports unknown cost as 0
        rows.append({"rank": i, "name": c.get("name"), "symbol": c.get("symbol"), "country": c.get("country"),
                     "holdings": h, "pct_supply": num(c.get("percentage_of_total_supply")),
                     "current_value_usd": _r(cur, 0), "entry_value_usd": _r(entry, 0),
                     "avg_cost": _r(entry / h, 2) if entry and h else None,
                     "unrealized_usd": _r(cur - entry, 0) if entry and cur is not None else None})
    return {"coin": coin, "symbol": symbol, "total_holdings": hold, "total_value_usd": _r(value, 0),
            "market_cap_dominance": num(doc.get("market_cap_dominance")),
            "implied_price": _r(value / hold, 2) if value and hold else None,
            "company_count": len(comps), "companies": rows}


def update_history(entries: list[dict[str, Any]], day: date, total_holdings: float | None,
                   total_value_usd: float | None = None, keep: int = HISTORY_KEEP) -> list[dict[str, Any]]:
    """Return the history with today's snapshot (one entry per UTC day; a same-day rerun replaces it)."""
    out = [e for e in entries if isinstance(e, dict) and e.get("date") and e["date"] != day.isoformat()]
    out.append({"date": day.isoformat(), "total_holdings": total_holdings, "total_value_usd": total_value_usd})
    return sorted(out, key=lambda e: e["date"])[-keep:]


def history_changes(entries: list[dict[str, Any]], day: date, current: float | None) -> dict[str, Any]:
    """Change vs the previous snapshot and over about 7 and 30 days (``None`` until history exists)."""
    past = sorted((e for e in entries if e.get("date", "") < day.isoformat()
                   and e.get("total_holdings") is not None), key=lambda e: e["date"])

    def change(e: dict[str, Any] | None) -> dict[str, Any] | None:
        if e is None or current is None:
            return None
        h = float(e["total_holdings"])
        return {"from_date": e["date"], "days": (day - date.fromisoformat(e["date"])).days, "holdings": h,
                "change": round(current - h, 4), "change_pct": round((current - h) / h * 100, 3) if h else None}

    def window(n: int, slack: int) -> dict[str, Any] | None:
        target = (day - timedelta(days=n)).isoformat()
        older = [e for e in past if e["date"] <= target]
        e = older[-1] if older else None
        return change(e) if e and (day - date.fromisoformat(e["date"])).days <= n + slack else None

    out = {"prev": change(past[-1] if past else None), "7d": window(7, 2), "30d": window(30, 7)}
    missing = [k for k, v in out.items() if v is None]
    first = past[0]["date"] if past else day.isoformat()
    note = f"not enough history yet for {', '.join(missing)} (first snapshot {first})" if missing else None
    return {"changes": out, "history_note": note}


def _treasury_verdict(t: dict[str, Any]) -> str:
    lead = t["companies"][0] if t["companies"] else None
    s = (f"{t['company_count']} public companies hold {t['total_holdings'] or 0:,.0f} {t['symbol']} "
         f"(${(t['total_value_usd'] or 0) / 1e9:,.1f}B)")
    prev = t["changes"].get("prev")
    if prev:
        s += f", {prev['change']:+,.0f} {t['symbol']} since {prev['from_date']}"
    if lead:
        s += f"; largest {lead['name']} {lead['holdings'] or 0:,.0f}"
    return s


def collect_treasuries(http: Http, today: date | None = None) -> dict[str, Any]:
    day = today or datetime.now(UTC).date()
    path = cache_dir() / HISTORY_FILE
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
        history = history if isinstance(history, dict) else {}
    except (OSError, ValueError):
        history = {}
    coins: dict[str, Any] = {}
    feeds = []
    for coin, symbol in TREASURY_COINS:
        try:
            t = summarize_treasury(coin, symbol, http.get_json(COINGECKO.format(coin=coin)))
            entries = history.get(coin) if isinstance(history.get(coin), list) else []
            t.update(history_changes(entries, day, t["total_holdings"]))
            history[coin] = update_history(entries, day, t["total_holdings"], t["total_value_usd"])
            t.update({"ok": True, "error": None, "verdict": _treasury_verdict(t)})
            feeds.append({"name": f"coingecko_{coin}", "ok": True, "error": None, "count": len(t["companies"])})
        except Exception as e:  # noqa: BLE001 - one coin failing never blanks the other
            err = f"{type(e).__name__}: {str(e)[:300]}"
            t = {"coin": coin, "symbol": symbol, "ok": False, "error": err}
            feeds.append({"name": f"coingecko_{coin}", "ok": False, "error": err, "count": 0})
        coins[coin] = t
    if not any(f["ok"] for f in feeds):
        raise RuntimeError("; ".join(f"{f['name']}: {f['error']}" for f in feeds))
    history["updated_at"] = utc_iso(datetime.now(UTC))
    _write_atomic(path, history)
    return {"attribution": "Data: CoinGecko", "feeds": feeds, "coins": coins,
            "verdict": "; ".join(c["verdict"] for c in coins.values() if c.get("ok"))}


COLLECTORS: list[Collector] = [
    Collector("etf_flows", "Spot ETF flows (BTC, ETH)", 10800, collect_etf_flows,
              tuple(u for pair in FARSIDE.values() for u in pair)),
    Collector("cot", "CME positioning (CFTC COT)", 43200, collect_cot, (CFTC_TFF,)),
    Collector("treasuries", "Public company treasuries", 43200, collect_treasuries,
              tuple(COINGECKO.format(coin=c) for c, _ in TREASURY_COINS)),
]
