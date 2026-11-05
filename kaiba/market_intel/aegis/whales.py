"""Whale and smart-money positioning: are the biggest and the best traders long or short?

Public, keyless, read-only sources: Hyperliquid's info API and leaderboard (positions are on-chain),
Binance's public data archive (data.binance.vision) and blockchain.info's mempool. Nothing here can
place, cancel or change an order. Money is USD; a long/short ratio above 1 means more long.

``smart_money`` (600 s): open Hyperliquid perp positions of two leaderboard cohorts::

    {"definitions": {"smart_money": str, "whales": str},
     "leaderboard": {"fetched_at", "rows", "reused", "stale", "error"},
     "fetch": {"addresses", "ok", "failed", "failed_addresses": [{"address", "error"}], "positions_as_of"},
     "cohorts": {"smart_money" | "whales": {
         "n_members", "n_ok", "n_with_positions", "totals": {"long_usd", "short_usd", "net_usd", "long_share"},
         "coins": [{"coin", "long_usd", "short_usd", "net_usd", "n_long", "n_short", "long_share",
                    "avg_leverage", "verdict"}],                       # ranked by |net_usd|
         "top_positions": {"BTC" | "ETH" | "SOL": [{"addr", "address", "side", "size", "size_usd", "entry",
                    "upnl", "roe", "leverage", "lev_type", "liq_px"}]},  # the 10 largest by size_usd
         "verdicts": [str],                                            # one line each for BTC, ETH, SOL
         "members": [{"addr", "address", "name", "account_value", "month_pnl", "month_roi", "ok",
                      "perp_value", "n_positions", "net_usd"}]}}}

Cohorts (``DEFINITIONS``): *smart money* is the top 40 by month PnL among accounts with leaderboard
accountValue >= $250,000 and month ROI > 0; *whales* is the top 40 by accountValue among accounts with
month volume > 0 (without that filter idle holders, system addresses and vaults such as HLP fill most of
the top 40). An address can be in both. ``n_long``/``n_short`` count accounts, ``long_share`` is
long_usd / (long_usd + short_usd), ``avg_leverage`` is the size-weighted mean leverage. The ~40 MB
leaderboard is downloaded at most every 6 h; the derived cohorts live in ``hl_cohorts.json``.

``hl_market`` (120 s): Hyperliquid metaAndAssetCtxs for BTC, ETH, SOL and the top 15 by day volume::

    {"rows": [{"coin", "focus", "vol_rank", "max_leverage", "mark", "oracle", "mid", "premium", "funding_1h",
               "funding_ann_pct", "oi", "oi_usd", "day_ntl_vlm", "prev_day_px", "change_24h_pct"}],
     "totals": {"coins", "day_ntl_vlm", "oi_usd"}, "note"}

``oi`` is in coins and ``oi_usd`` = oi x mark; ``funding_1h`` is the hourly rate and
``funding_ann_pct`` = funding_1h x 24 x 365 x 100.

``binance_positioning`` (10800 s): Binance USD-M daily metrics archive (DELAYED: about 1-2 days)::

    {"symbols": {"BTCUSDT" | "ETHUSDT": {
         "as_of_date", "as_of_time", "lag_days",
         "latest": {"time", "oi", "oi_usd", "top_account_ls", "top_position_ls", "all_account_ls", "taker_buy_sell"},
         "days": [{"date", "rows", "oi", "oi_usd", "top_account_ls", "top_position_ls", "all_account_ls",
                   "taker_buy_sell"}],                                 # ratios are day means, oi is end of day
         "window_means": {ratio: float}, "oi_change_7d_usd", "oi_change_7d_pct", "oi_change_base_date",
         "readings": {ratio: str}}},
     "feeds": [{"name", "ok", "error", "count"}], "fetched": {sym: [date]}, "missing": {sym: [date]},
     "ratio_defs": {ratio: {"column", "label", "meaning"}}, "delayed": true, "as_of_date", "lag_days", "note"}

Each downloaded day is summarized once into ``binance_metrics/<SYM>-<date>.json``; later runs only
fetch days not yet cached (a 404 day is skipped and retried next run).

``btc_whale_tx`` (120 s): mempool transactions with total output >= 100 BTC, rolling 24 h::

    {"txs": [{"hash", "btc", "time", "ts", "url", "n_out"}],          # largest first
     "counts": {"1h": {"n", "btc"}, "24h": {"n", "btc"}},
     "snapshot": {"txs", "max_btc", "large", "new"}, "threshold_btc", "note"}
"""

from __future__ import annotations

import csv
import io
import json
import time
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .base import Collector, Http, cache_dir, num, parse_when, utc_iso

HL_INFO = "https://api.hyperliquid.xyz/info"
HL_LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
FOCUS = ("BTC", "ETH", "SOL")
COHORT_N = 40
SMART_MIN_ACCOUNT = 250_000.0
LEADERBOARD_MAX_AGE_S = 6 * 3600
MAX_CALLS = 90                 # clearinghouseState calls per run (two cohorts of 40, minus overlap)
PAUSE_S = 0.07                 # between calls; tests set it to 0
TOP_POSITIONS = 10
LABELS = {"smart_money": "smart money", "whales": "whales"}
DEFINITIONS = {
    "smart_money": f"Top {COHORT_N} Hyperliquid leaderboard accounts by 30-day (month) PnL among accounts with "
                   f"accountValue >= ${SMART_MIN_ACCOUNT:,.0f} and month ROI > 0.",
    "whales": f"Top {COHORT_N} Hyperliquid leaderboard accounts by accountValue among accounts with month volume "
              "> 0 (idle holders, system addresses and vaults such as HLP otherwise fill most of the top 40). "
              "An address can be in both cohorts.",
}

BV_METRICS = "https://data.binance.vision/data/futures/um/daily/metrics/{sym}/{sym}-metrics-{day}.zip"
BV_SYMBOLS = ("BTCUSDT", "ETHUSDT")
BV_DAYS = 10
BV_KEEP_DAYS = 30
RATIOS: tuple[tuple[str, str, str, str], ...] = (   # key, archive column, label, meaning
    ("top_account_ls", "count_toptrader_long_short_ratio", "Top-trader accounts",
     "long/short ratio of the top 20% of accounts by margin balance, counted by number of accounts"),
    ("top_position_ls", "sum_toptrader_long_short_ratio", "Top-trader positions",
     "long/short ratio of the same top 20% of accounts, weighted by position size"),
    ("all_account_ls", "count_long_short_ratio", "All accounts",
     "long/short ratio of all accounts with a position, counted by number of accounts"),
    ("taker_buy_sell", "sum_taker_long_short_vol_ratio", "Taker buy/sell volume",
     "aggressive (taker) buy volume divided by aggressive sell volume"),
)
METRIC_COLS = ("create_time", "symbol", "sum_open_interest", "sum_open_interest_value", *(r[1] for r in RATIOS))
SHARE_TEXT = {"top_account_ls": "of top-20% accounts are net long", "top_position_ls": "of top-20% position size is long",
              "all_account_ls": "of all accounts are net long", "taker_buy_sell": "of taker volume was buying"}

BTC_UNCONFIRMED = "https://blockchain.info/unconfirmed-transactions?format=json"
MEMPOOL_TX = "https://mempool.space/tx/"
WHALE_TX_MIN_BTC = 100.0
WHALE_TX_NOTE = ("Unlabelled, unconfirmed mempool transfers sampled from blockchain.info's newest ~100 transactions "
                 "per poll, so many large transfers are missed and counts are a lower bound. Total output includes "
                 "change returned to the sender. These are not confirmed exchange inflows or outflows.")


# ------------------------------------------------------------------ small helpers
def _err(e: Exception) -> str:
    return f"{type(e).__name__}: {str(e)[:160]}"


def _status(e: Exception) -> int | None:
    return getattr(getattr(e, "response", None), "status_code", None)


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


def _mean(vals: list[float | None]) -> float | None:
    v = [x for x in vals if x is not None]
    return sum(v) / len(v) if v else None


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def short_addr(a: str) -> str:
    return f"{a[:6]}…{a[-4:]}" if len(a) > 12 else a


def usd_text(x: float) -> str:
    a = abs(x)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"${a / div:.1f}{suf}"
    return f"${a:,.0f}"


# ------------------------------------------------------------------ smart_money: cohorts
def _window(row: dict[str, Any], name: str) -> dict[str, Any]:
    for item in row.get("windowPerformances") or []:
        if isinstance(item, (list, tuple)) and len(item) == 2 and item[0] == name and isinstance(item[1], dict):
            return item[1]
    return {}


def leaderboard_members(doc: Any) -> list[dict[str, Any]]:
    """Leaderboard rows -> {address, name, account_value, month_pnl, month_roi, month_vlm}; bad rows dropped."""
    out, seen = [], set()
    for r in (doc or {}).get("leaderboardRows") or []:
        addr = str(r.get("ethAddress") or "").lower()
        av = num(r.get("accountValue"))
        if not addr.startswith("0x") or av is None or addr in seen:
            continue
        seen.add(addr)
        m = _window(r, "month")
        out.append({"address": addr, "name": r.get("displayName"), "account_value": av,
                    "month_pnl": num(m.get("pnl")) or 0.0, "month_roi": num(m.get("roi")) or 0.0,
                    "month_vlm": num(m.get("vlm")) or 0.0})
    return out


def select_cohorts(members: list[dict[str, Any]], n: int = COHORT_N,
                   min_account: float = SMART_MIN_ACCOUNT) -> dict[str, list[dict[str, Any]]]:
    """The two cohorts exactly as ``DEFINITIONS`` states them (ties broken by address)."""
    smart = sorted((m for m in members if m["account_value"] >= min_account and m["month_roi"] > 0),
                   key=lambda m: (-m["month_pnl"], m["address"]))[:n]
    whales = sorted((m for m in members if m["month_vlm"] > 0),
                    key=lambda m: (-m["account_value"], m["address"]))[:n]
    return {"smart_money": smart, "whales": whales}


def cohorts_path() -> Path:
    return cache_dir() / "hl_cohorts.json"


def load_cohorts(http: Http, now: float) -> tuple[dict[str, Any], dict[str, Any]]:
    """Cohorts from ``hl_cohorts.json`` when under 6 h old, else from a fresh leaderboard download.
    A failed download falls back to an older side file, marked stale, instead of failing the run."""
    side = _load(cohorts_path())
    side = side if isinstance(side, dict) and isinstance(side.get("smart_money"), list) else None
    if side and side.get("definitions") == DEFINITIONS and 0 <= now - float(side.get("built_ts") or 0) < LEADERBOARD_MAX_AGE_S:
        return side, {"reused": True, "stale": False, "error": None}
    try:
        members = leaderboard_members(http.get_json(HL_LEADERBOARD))
        if not members:
            raise ValueError("leaderboard has no usable rows")
    except Exception as e:  # an old cohort list beats an empty panel; re-raised when there is none
        if side:
            return side, {"reused": True, "stale": True, "error": _err(e)}
        raise
    side = {"built_ts": now, "built_at": utc_iso(now), "rows": len(members), "definitions": DEFINITIONS,
            **select_cohorts(members)}
    _save(cohorts_path(), side)
    return side, {"reused": False, "stale": False, "error": None}


# ------------------------------------------------------------------ smart_money: positions
def parse_positions(state: Any) -> list[dict[str, Any]]:
    """clearinghouseState -> open positions; ``szi`` > 0 is long, positionValue is the USD size."""
    out = []
    for ap in (state or {}).get("assetPositions") or []:
        p = (ap or {}).get("position") or {}
        szi = num(p.get("szi"))
        if not p.get("coin") or not szi:
            continue
        lev = p.get("leverage") or {}
        out.append({"coin": str(p["coin"]), "side": "long" if szi > 0 else "short", "size": abs(szi),
                    "size_usd": abs(num(p.get("positionValue")) or 0.0), "entry": num(p.get("entryPx")),
                    "upnl": num(p.get("unrealizedPnl")), "roe": num(p.get("returnOnEquity")),
                    "leverage": num(lev.get("value")), "lev_type": lev.get("type"),
                    "liq_px": num(p.get("liquidationPx"))})
    return out


def fetch_states(http: Http, addresses: list[str]) -> tuple[dict[str, dict[str, Any] | None], list[dict[str, str]]]:
    """One clearinghouseState call per address (budget ``MAX_CALLS``); a failure is recorded, never raised."""
    results: dict[str, dict[str, Any] | None] = {}
    failed: list[dict[str, str]] = []
    for i, a in enumerate(addresses):
        if i >= MAX_CALLS:
            results[a] = None
            failed.append({"address": a, "error": "skipped: per-run call budget"})
            continue
        if i and PAUSE_S:
            time.sleep(PAUSE_S)
        try:
            st = http.post_json(HL_INFO, {"type": "clearinghouseState", "user": a})
            if not isinstance(st, dict) or not isinstance(st.get("assetPositions"), list):
                raise TypeError("unexpected clearinghouseState shape")
            results[a] = {"positions": parse_positions(st), "time": st.get("time"),
                          "perp_value": num((st.get("marginSummary") or {}).get("accountValue"))}
        except Exception as e:  # noqa: BLE001 - one bad address must not fail the cohort
            results[a] = None
            failed.append({"address": a, "error": _err(e)})
    return results, failed


def verdict(coin: str, label: str, row: dict[str, Any] | None) -> str:
    if not row or not (row["n_long"] or row["n_short"]):
        return f"{coin}: {label} has no open positions"
    net, nl, ns = row["net_usd"], row["n_long"], row["n_short"]
    side = "LONG" if net > 0 else "SHORT" if net < 0 else "FLAT"
    counts = f"{ns} short / {nl} long" if net < 0 else f"{nl} long / {ns} short"
    share = f", {row['long_share']:.0%} long by size" if row["long_share"] is not None else ""
    return f"{coin}: {label} net {side} {usd_text(net)} ({counts}{share})"


def aggregate(members: list[dict[str, Any]], results: dict[str, dict[str, Any] | None],
              label: str) -> dict[str, Any]:
    """One cohort's book. ``results`` maps address -> {"positions", "perp_value"}, or None if the call failed."""
    coins: dict[str, dict[str, Any]] = {}
    lev: dict[str, list[float]] = {}            # coin -> [sum(leverage x size), sum(size)]
    tops: dict[str, list[dict[str, Any]]] = {c: [] for c in FOCUS}
    rows = []
    for m in members:
        res = results.get(m["address"])
        pos = res["positions"] if res else []
        net = 0.0
        for p in pos:
            c = coins.setdefault(p["coin"], {"coin": p["coin"], "long_usd": 0.0, "short_usd": 0.0,
                                             "n_long": 0, "n_short": 0})
            c[f"{p['side']}_usd"] += p["size_usd"]
            c[f"n_{p['side']}"] += 1
            net += p["size_usd"] if p["side"] == "long" else -p["size_usd"]
            if p["leverage"] is not None and p["size_usd"] > 0:
                w = lev.setdefault(p["coin"], [0.0, 0.0])
                w[0] += p["leverage"] * p["size_usd"]
                w[1] += p["size_usd"]
            if p["coin"] in tops:
                tops[p["coin"]].append({"addr": short_addr(m["address"]), "address": m["address"],
                                        **{k: p[k] for k in ("side", "size", "size_usd", "entry", "upnl", "roe",
                                                             "leverage", "lev_type", "liq_px")}})
        rows.append({"addr": short_addr(m["address"]), "address": m["address"], "name": m.get("name"),
                     "account_value": _r(m.get("account_value")), "month_pnl": _r(m.get("month_pnl")),
                     "month_roi": _r(m.get("month_roi"), 4), "ok": res is not None,
                     "perp_value": _r(res["perp_value"]) if res else None, "n_positions": len(pos),
                     "net_usd": _r(net) if res else None})
    for c in coins.values():
        gross = c["long_usd"] + c["short_usd"]
        w = lev.get(c["coin"])
        c.update(net_usd=c["long_usd"] - c["short_usd"], long_share=_r(c["long_usd"] / gross, 4) if gross else None,
                 avg_leverage=_r(w[0] / w[1]) if w and w[1] else None)
        c["verdict"] = verdict(c["coin"], label, c)
        for k in ("long_usd", "short_usd", "net_usd"):
            c[k] = _r(c[k])
    ranked = sorted(coins.values(), key=lambda c: (-abs(c["net_usd"]), c["coin"]))
    lu, su = sum(c["long_usd"] for c in ranked), sum(c["short_usd"] for c in ranked)
    return {"n_members": len(members), "n_ok": sum(r["ok"] for r in rows),
            "n_with_positions": sum(1 for r in rows if r["n_positions"]),
            "totals": {"long_usd": _r(lu), "short_usd": _r(su), "net_usd": _r(lu - su),
                       "long_share": _r(lu / (lu + su), 4) if lu + su else None},
            "coins": ranked,
            "top_positions": {k: sorted(v, key=lambda p: -p["size_usd"])[:TOP_POSITIONS] for k, v in tops.items()},
            "verdicts": [verdict(c, label, coins.get(c)) for c in FOCUS],
            "members": rows}


def collect_smart_money(http: Http, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    side, lb = load_cohorts(http, now)
    cohorts = {k: side.get(k) or [] for k in LABELS}
    addresses = list(dict.fromkeys(m["address"] for k in LABELS for m in cohorts[k]))
    results, failed = fetch_states(http, addresses)
    ok = [r for r in results.values() if r]
    if addresses and not ok:
        raise RuntimeError(f"all {len(addresses)} clearinghouseState calls failed; first: {failed[0]['error']}")
    times = [float(r["time"]) for r in ok if num(r.get("time"))]
    return {"definitions": side.get("definitions") or DEFINITIONS,
            "leaderboard": {"fetched_at": side.get("built_at"), "rows": side.get("rows"), **lb},
            "fetch": {"addresses": len(addresses), "ok": len(ok), "failed": len(failed), "failed_addresses": failed,
                      "positions_as_of": utc_iso(max(times) / 1000) if times else None},
            "cohorts": {k: aggregate(cohorts[k], results, LABELS[k]) for k in LABELS}}


# ------------------------------------------------------------------ hl_market
def parse_market(doc: Any) -> list[dict[str, Any]]:
    """metaAndAssetCtxs ``[meta, ctxs]`` -> one row per coin; ``meta.universe[i]`` pairs with ``ctxs[i]``."""
    if not (isinstance(doc, list) and len(doc) == 2 and isinstance(doc[0], dict) and isinstance(doc[1], list)):
        raise ValueError("metaAndAssetCtxs: expected [meta, ctxs]")
    uni, ctxs = doc[0].get("universe") or [], doc[1]
    if len(uni) != len(ctxs):
        raise ValueError(f"metaAndAssetCtxs misaligned: {len(uni)} coins vs {len(ctxs)} contexts")
    rows = []
    for u, c in zip(uni, ctxs):
        mark, prev, oi, fr = (num(c.get(k)) for k in ("markPx", "prevDayPx", "openInterest", "funding"))
        rows.append({"coin": u.get("name"), "delisted": bool(u.get("isDelisted")), "max_leverage": u.get("maxLeverage"),
                     "mark": mark, "oracle": num(c.get("oraclePx")), "mid": num(c.get("midPx")),
                     "premium": num(c.get("premium")), "funding_1h": fr,
                     "funding_ann_pct": _r(fr * 24 * 365 * 100, 4) if fr is not None else None,
                     "oi": oi, "oi_usd": _r(oi * mark) if oi is not None and mark is not None else None,
                     "day_ntl_vlm": num(c.get("dayNtlVlm")) or 0.0, "prev_day_px": prev,
                     "change_24h_pct": _r((mark / prev - 1) * 100, 4) if mark and prev else None})
    return rows


def select_market(rows: list[dict[str, Any]], focus: tuple[str, ...] = FOCUS, top_n: int = 15) -> list[dict[str, Any]]:
    """Focus coins first, then the rest of the top ``top_n`` listed coins by day notional volume."""
    by_vol = sorted((r for r in rows if not r["delisted"]), key=lambda r: -r["day_ntl_vlm"])
    rank = {r["coin"]: i + 1 for i, r in enumerate(by_vol)}
    pick = [r for c in focus for r in rows if r["coin"] == c] + [r for r in by_vol[:top_n] if r["coin"] not in focus]
    return [{"coin": r["coin"], "focus": r["coin"] in focus, "vol_rank": rank.get(r["coin"]),
             **{k: v for k, v in r.items() if k not in ("coin", "delisted")}} for r in pick]


def collect_hl_market(http: Http) -> dict[str, Any]:
    rows = parse_market(http.post_json(HL_INFO, {"type": "metaAndAssetCtxs"}))
    live = [r for r in rows if not r["delisted"]]
    return {"rows": select_market(rows),
            "totals": {"coins": len(live), "day_ntl_vlm": _r(sum(r["day_ntl_vlm"] for r in live)),
                       "oi_usd": _r(sum(r["oi_usd"] or 0.0 for r in live))},
            "note": "Hyperliquid perps. funding_1h is the hourly rate; funding_ann_pct = funding_1h x 24 x 365 x 100; "
                    "oi is in coins and oi_usd = oi x mark."}


# ------------------------------------------------------------------ binance_positioning
def unzip_csv(raw: bytes, limit: int = 16 * 1024 * 1024) -> str:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise ValueError("zip holds no csv")
        if z.getinfo(names[0]).file_size > limit:
            raise ValueError("csv in zip too large")
        return z.read(names[0]).decode("utf-8", errors="replace")


def parse_metrics_csv(text: str) -> list[dict[str, Any]]:
    """Binance ``<SYM>-metrics-<date>.csv`` (5-minute rows) -> rows sorted by time, duplicates dropped."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    if not lines[0].lower().startswith("create_time"):
        lines.insert(0, ",".join(METRIC_COLS))
    out: dict[float, dict[str, Any]] = {}
    for r in csv.DictReader(lines):
        when = parse_when(r.get("create_time"))
        if when is None:
            continue
        out[when.timestamp()] = {"time": utc_iso(when), "ts": when.timestamp(), "oi": num(r.get("sum_open_interest")),
                                 "oi_usd": num(r.get("sum_open_interest_value")),
                                 **{k: num(r.get(col)) for k, col, *_ in RATIOS}}
    return [out[t] for t in sorted(out)]


def summarize_day(rows: list[dict[str, Any]], day: str) -> dict[str, Any]:
    """Day means of each ratio, end-of-day open interest and the day's last row."""
    last = rows[-1]
    return {"date": day, "rows": len(rows), "oi": last["oi"], "oi_usd": _r(last["oi_usd"]),
            **{k: _r(_mean([r[k] for r in rows]), 6) for k, *_ in RATIOS},
            "last": {k: v for k, v in last.items() if k != "ts"}}


def metrics_path(sym: str, day: str) -> Path:
    return cache_dir() / "binance_metrics" / f"{sym}-{day}.json"


def load_days(http: Http, sym: str, days: list[str]) -> tuple[dict[str, dict[str, Any]], list[str], list[str], list[str]]:
    """Per-day summaries for ``days``: cached ones are read, the rest downloaded. A 404 day is skipped."""
    got: dict[str, dict[str, Any]] = {}
    fetched, missing, errors = [], [], []
    for day in days:
        cached = _load(metrics_path(sym, day))
        if isinstance(cached, dict) and cached.get("date") == day:
            got[day] = cached
            continue
        try:
            rows = parse_metrics_csv(unzip_csv(http.get_bytes(BV_METRICS.format(sym=sym, day=day))))
            if not rows:
                raise ValueError("no rows")
        except Exception as e:  # noqa: BLE001 - a missing or bad day is skipped, not fatal
            if _status(e) == 404:
                missing.append(day)
            else:
                errors.append(f"{day}: {_err(e)}")
            continue
        got[day] = summarize_day(rows, day)
        _save(metrics_path(sym, day), got[day])
        fetched.append(day)
    return got, fetched, missing, errors


def ratio_reading(key: str, value: float | None, avg: float | None, day: str, n_days: int) -> str:
    label = next(r[2] for r in RATIOS if r[0] == key)
    if value is None:
        return f"{label}: no data"
    share = value / (1 + value)
    if key == "taker_buy_sell":
        lean = ("buyers more aggressive" if value > 1.02 else "sellers more aggressive" if value < 0.98
                else "buyers and sellers balanced")
        text = f"{label} {value:.2f} on {day} (mean of 5-min ratios): {lean}; {share:.0%} {SHARE_TEXT[key]}"
    else:
        lean = "more long than short" if value > 1 else "more short than long" if value < 1 else "evenly split"
        text = f"{label} {value:.2f} on {day}: {lean}; {share:.0%} {SHARE_TEXT[key]}"
    if avg:
        rel = value / avg - 1
        cmp = "above" if rel > 0.03 else "below" if rel < -0.03 else "in line with"
        text += f"; {cmp} the {n_days}-day average {avg:.2f}"
    return text


def positioning(sym: str, summaries: dict[str, dict[str, Any]], today: date) -> dict[str, Any] | None:
    days = sorted(summaries)
    if not days:
        return None
    last_day = days[-1]
    latest = summaries[last_day]
    as_of = date.fromisoformat(last_day)
    base_cut = (as_of - timedelta(days=7)).isoformat()
    base = next((d for d in days if d >= base_cut and d != last_day), None)
    b_oi, l_oi = (summaries[base]["oi_usd"] if base else None), latest["oi_usd"]
    window = {k: _r(_mean([summaries[d][k] for d in days]), 6) for k, *_ in RATIOS}
    return {"symbol": sym, "as_of_date": last_day, "as_of_time": latest["last"]["time"], "lag_days": (today - as_of).days,
            "latest": latest["last"],
            "days": [{k: v for k, v in summaries[d].items() if k != "last"} for d in days],
            "window_means": window,
            "oi_change_7d_usd": _r(l_oi - b_oi) if b_oi and l_oi is not None else None,
            "oi_change_7d_pct": _r((l_oi / b_oi - 1) * 100, 3) if b_oi and l_oi is not None else None,
            "oi_change_base_date": base,
            "readings": {k: ratio_reading(k, latest[k], window[k], last_day, len(days)) for k, *_ in RATIOS}}


def _prune_metrics(today: date) -> None:
    cut = (today - timedelta(days=BV_KEEP_DAYS)).isoformat()
    for p in (cache_dir() / "binance_metrics").glob("*.json"):
        if p.stem[-10:] < cut:
            p.unlink(missing_ok=True)


def collect_binance_positioning(http: Http, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now, UTC).date()
    days = [(today - timedelta(days=k)).isoformat() for k in range(BV_DAYS, 0, -1)]   # oldest .. yesterday
    out: dict[str, Any] = {"symbols": {}, "feeds": [], "fetched": {}, "missing": {}}
    for sym in BV_SYMBOLS:
        got, fetched, missing, errors = load_days(http, sym, days)
        pos = positioning(sym, got, today)
        if pos:
            out["symbols"][sym] = pos
        out["fetched"][sym], out["missing"][sym] = fetched, missing
        out["feeds"].append({"name": sym, "ok": pos is not None, "error": "; ".join(errors)[:300] or None,
                             "count": len(got)})
    if not out["symbols"]:
        raise RuntimeError("no Binance metrics day available: " + "; ".join(f["error"] or "all 404" for f in out["feeds"]))
    _prune_metrics(today)
    lags = [s["lag_days"] for s in out["symbols"].values()]
    return {**out, "ratio_defs": {k: {"column": col, "label": lab, "meaning": mean} for k, col, lab, mean in RATIOS},
            "delayed": True, "as_of_date": min(s["as_of_date"] for s in out["symbols"].values()), "lag_days": max(lags),
            "note": "DELAYED: Binance's public daily archive publishes about a day late, so this is positioning as of "
                    "as_of_date, not now. Ratios above 1 mean more long (or, for taker volume, more aggressive buying)."}


# ------------------------------------------------------------------ btc_whale_tx
def large_txs(doc: Any, now: float, min_btc: float = WHALE_TX_MIN_BTC) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Mempool snapshot -> transactions whose total output is >= ``min_btc``, plus snapshot stats."""
    txs = (doc or {}).get("txs") or []
    out, mx = [], 0.0
    for t in txs:
        h = str(t.get("hash") or "")
        outs = t.get("out") or []
        btc = sum(num(o.get("value")) or 0.0 for o in outs) / 1e8
        mx = max(mx, btc)
        if h and btc >= min_btc:
            out.append({"hash": h, "btc": round(btc, 8), "ts": num(t.get("time")) or now, "seen_ts": now,
                        "n_out": len(outs)})
    return out, {"txs": len(txs), "max_btc": round(mx, 8)}


def merge_archive(old: list[dict[str, Any]], new: list[dict[str, Any]], now: float,
                  window_s: float = 86400) -> list[dict[str, Any]]:
    """De-duplicate by hash (first sighting wins) and drop entries older than ``window_s``."""
    keep, seen = [], set()
    for r in [*old, *new]:
        h = r.get("hash")
        if not h or h in seen or float(r.get("ts") or r.get("seen_ts") or now) < now - window_s:
            continue
        seen.add(h)
        keep.append(r)
    return keep


def whale_tx_summary(archive: list[dict[str, Any]], now: float) -> dict[str, Any]:
    def win(s: float) -> dict[str, Any]:
        sel = [r for r in archive if float(r.get("ts") or 0) >= now - s]
        return {"n": len(sel), "btc": round(sum(r["btc"] for r in sel), 8)}

    rows = sorted(archive, key=lambda r: (-r["btc"], r["hash"]))
    return {"txs": [{"hash": r["hash"], "btc": r["btc"], "time": utc_iso(r["ts"]), "ts": r["ts"],
                     "url": MEMPOOL_TX + r["hash"], "n_out": r.get("n_out")} for r in rows],
            "counts": {"1h": win(3600), "24h": win(86400)}}


def whale_archive_path() -> Path:
    return cache_dir() / "btc_whale_tx_archive.jsonl"


def collect_btc_whale_tx(http: Http, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    doc = http.get_json(BTC_UNCONFIRMED)
    if not isinstance(doc, dict) or not isinstance(doc.get("txs"), list):
        raise TypeError("unexpected unconfirmed-transactions shape")
    new, snap = large_txs(doc, now)
    path = whale_archive_path()
    old = []
    if path.exists():
        for ln in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(ln)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("hash") and isinstance(row.get("btc"), (int, float)):
                old.append(row)
    known = {r["hash"] for r in old}
    archive = merge_archive(old, new, now)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in archive), encoding="utf-8")
    tmp.replace(path)
    snap.update(large=len(new), new=sum(1 for r in new if r["hash"] not in known))
    return {**whale_tx_summary(archive, now), "snapshot": snap, "threshold_btc": WHALE_TX_MIN_BTC,
            "note": WHALE_TX_NOTE}


COLLECTORS: list[Collector] = [
    Collector("smart_money", "Hyperliquid smart money and whales", 600, collect_smart_money,
              (HL_LEADERBOARD, HL_INFO)),
    Collector("hl_market", "Hyperliquid funding, OI and premium", 120, collect_hl_market, (HL_INFO,)),
    Collector("binance_positioning", "Binance USD-M positioning (daily archive, delayed)", 10800,
              collect_binance_positioning, ("https://data.binance.vision/data/futures/um/daily/metrics/",)),
    Collector("btc_whale_tx", "Large BTC mempool transfers", 120, collect_btc_whale_tx, (BTC_UNCONFIRMED,)),
]
