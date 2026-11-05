"""Bounded, read-only bridge to Aegis' public intelligence caches.

Codex owns this additive dashboard adapter (UNIFIED-DASHBOARD-20261103).
It never imports the other project's runtime, fetches a provider, reads a trading
account, or starts its collectors. Cache freshness and observation dates are
separate evidence: refreshing a file does not refresh an old economic release.
"""

from __future__ import annotations

import json
import math
import os
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

MAX_CACHE_BYTES = 2 * 1024 * 1024
DAY_MS = 86_400_000
KAIBA_CACHE_ROOT = Path(__file__).resolve().parents[1] / "data" / "market-intel" / "intel"
AEGIS_CACHE_ROOT = Path(__file__).resolve().parents[2] / "AEGIS OMNI" / "runtime" / "terminal" / "intel"
SPECS = {
    "calendar": ("Economic calendar", 3600),
    "macro_fred": ("US macro and Fed liquidity", 21600),
    "news": ("Market news", 300),
    "etf_flows": ("Spot ETF flows", 10800),
    "btc_whale_tx": ("Large BTC mempool transfers", 120),
    "smart_money": ("Hyperliquid cohort positions", 600),
    "markets": ("Global markets", 300),
    "tickers": ("Crypto markets", 60),
}
FED_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FUND_NAMES = {
    "IBIT": "BlackRock · iShares Bitcoin Trust",
    "ETHA": "BlackRock · iShares Ethereum Trust",
    "FBTC": "Fidelity Bitcoin ETF",
    "FETH": "Fidelity Ethereum ETF",
    "GBTC": "Grayscale Bitcoin Trust",
    "ETHE": "Grayscale Ethereum Trust",
    "ARKB": "ARK 21Shares Bitcoin ETF",
    "BITB": "Bitwise Bitcoin ETF",
    "ETHW": "Bitwise Ethereum ETF",
}


def _text(value: Any, limit: int = 240) -> str:
    return str(value).strip()[:limit] if isinstance(value, (str, int, float)) else ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
            return None
        # The bridge only needs source attribution, never query credentials.
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return None


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _rows(value: Any, limit: int = 400) -> list[dict]:
    return [row for row in value[:limit] if isinstance(row, dict)] if isinstance(value, list) else []


def _instant(value: Any) -> int | None:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            return int(parsed.timestamp() * 1000)
        except (ValueError, OverflowError, OSError):
            return None
    number = _number(value)
    if number is not None and 0 < number < 253_402_300_799:
        return int(number * 1000)
    return None


def _iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, UTC).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def _observation_status(status: str, value: Any, now_ms: int, max_days: int) -> str:
    try:
        observed = datetime.strptime(str(value), "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return "unknown" if status in ("fresh", "partial") else status
    age = now_ms - int(observed.timestamp() * 1000)
    if age < -DAY_MS:
        return "unknown"
    if age > max_days * DAY_MS:
        return "stale"
    return status


def _partial(data: dict) -> bool:
    feeds = _rows(data.get("feeds"), 100)
    return any(feed.get("ok") is False for feed in feeds) or bool(
        (_number(_mapping(data.get("fetch")).get("failed")) or 0) > 0
    )


def _read(root: Path, key: str, now_ms: int) -> tuple[dict, dict]:
    title, interval = SPECS[key]
    health = {
        "key": key, "title": title, "status": "missing", "fetched_at": None,
        "age_s": None, "interval_s": interval, "error": None, "source_urls": [], "partial": False,
    }
    try:
        path = root / f"{key}.json"
        meta = path.lstat()
        if not stat.S_ISREG(meta.st_mode) or path.is_symlink():
            raise ValueError("cache must be a regular file")
        if meta.st_size > MAX_CACHE_BYTES:
            raise ValueError("cache exceeds size limit")
        with path.open("rb") as handle:
            raw = handle.read(MAX_CACHE_BYTES + 1)
        if len(raw) > MAX_CACHE_BYTES:
            raise ValueError("cache exceeds size limit")
        doc = json.loads(raw)
        if not isinstance(doc, dict) or not isinstance(doc.get("data"), dict):
            raise ValueError("cache envelope invalid")
        if doc.get("key") != key:
            raise ValueError("cache identity mismatch")
    except FileNotFoundError:
        return {}, health
    except (OSError, ValueError, RecursionError):
        health.update(status="error", error="Cache unavailable or invalid")
        return {}, health
    fetched = _instant(doc.get("fetched_ts")) or _instant(doc.get("fetched_at"))
    health["fetched_at"] = _iso(fetched)
    health["age_s"] = round((now_ms - fetched) / 1000) if fetched is not None else None
    health["source_urls"] = [url for value in doc.get("sources", [])[:12] if (url := _url(value))] \
        if isinstance(doc.get("sources"), list) else []
    data = doc["data"]
    health["partial"] = _partial(data)
    if fetched is None or fetched > now_ms + 300_000:
        health["status"] = "unknown"
    elif doc.get("ok") is not True or doc.get("stale") is True or now_ms - fetched > interval * 2000:
        health["status"] = "stale"
        if doc.get("ok") is not True:
            health["error"] = "Source refresh failed; last cached data retained"
    else:
        health["status"] = "partial" if health["partial"] else "fresh"
    return data, health


def _macro_events(data: dict, health: dict, now_ms: int) -> list[dict]:
    result = []
    for index, row in enumerate(_rows(data.get("events"), 1000)):
        title, kind = _text(row.get("title")), _text(row.get("kind"))
        when = _instant(row.get("ts")) or _instant(row.get("when"))
        if when is None or not now_ms - DAY_MS <= when <= now_ms + 90 * DAY_MS:
            continue
        country = _text(row.get("country"))
        relevant = any(word in title.lower() for word in ("cpi", "pce", "fomc", "fed ", "payroll", "unemployment", "interest", "inflation"))
        if kind not in ("fomc", "expiry", "funding") and not (country == "USD" and (relevant or row.get("impact") == "High")):
            continue
        source = _text(row.get("source"))
        result.append({
            "id": f"calendar:{index}:{when}", "title": title, "kind": kind, "country": country,
            "impact": _text(row.get("impact")), "event_at": _iso(when), "event_ms": when,
            "forecast": _text(row.get("forecast")) or None, "previous": _text(row.get("previous")) or None,
            "actual": _text(row.get("actual")) or None, "source": source,
            "source_url": FED_URL if source == "Federal Reserve" else FF_URL if source == "ForexFactory" else None,
            "status": health["status"], "note": _text(row.get("note")),
        })
    result.sort(key=lambda row: row["event_ms"])
    selected = result[:300]
    # Dense near-term calendars must not hide the next known FOMC meeting
    # from the daily compiler's separately promised 90-day lookahead.
    next_fomc = next((row for row in result if row["kind"] == "fomc" and row["event_ms"] >= now_ms), None)
    if next_fomc is not None and next_fomc not in selected:
        selected[-1] = next_fomc
        selected.sort(key=lambda row: row["event_ms"])
    return selected


def _macro_series(data: dict, health: dict, now_ms: int) -> list[dict]:
    result = []
    for row in _rows(data.get("series"), 40):
        ident = _text(row.get("id"), 30)
        if not ident.replace("_", "").isalnum():
            continue
        freq = _text(row.get("freq"))
        max_days = {"daily": 7, "weekly": 14, "monthly": 75}.get(freq, 75)
        result.append({
            "id": ident, "name": _text(row.get("name")), "value": _number(row.get("value")),
            "units": _text(row.get("units"), 40), "report_date": _text(row.get("date"), 10),
            "change": _number(row.get("change")), "group": _text(row.get("group")),
            "status": _observation_status(health["status"], row.get("date"), now_ms, max_days),
            "source_url": f"https://fred.stlouisfed.org/series/{ident}" if ident != "TGA" else "https://fiscaldata.treasury.gov/",
        })
    return result


def _news(data: dict, health: dict, now_ms: int) -> list[dict]:
    result = []
    for index, row in enumerate(_rows(data.get("items"), 500)):
        published = _instant(row.get("ts")) or _instant(row.get("published"))
        title = _text(row.get("title"))
        if not title or (published is not None and published > now_ms + 300_000):
            continue
        status = health["status"]
        if published is None:
            status = "unknown"
        elif now_ms - published > DAY_MS:
            status = "stale"
        result.append({
            "id": _text(row.get("id"), 80) or f"news:{index}", "title": title,
            "source": _text(row.get("source"), 80), "url": _url(row.get("link")),
            "published_at": _iso(published), "published_ms": published,
            "group": _text(row.get("group"), 30), "status": status, "high": row.get("high") is True,
        })
    # Aegis' news collector itself retains at most 400 items. The daily macro
    # compiler must see that full bounded set before selecting macro headlines;
    # slicing for screen density belongs to the UI, not this evidence bridge.
    return sorted(result, key=lambda row: row["published_ms"] or 0, reverse=True)[:400]


def _institutions(data: dict, health: dict, now_ms: int) -> list[dict]:
    result = []
    for asset in ("BTC", "ETH"):
        item = _mapping(_mapping(data.get("assets")).get(asset))
        latest = _mapping(item.get("latest"))
        report_date = _text(latest.get("date"), 10)
        row = next((r for r in reversed(_rows(item.get("rows"), 40)) if r.get("date") == report_date), {})
        flows = _mapping(row.get("flows"))
        expected = "IBIT" if asset == "BTC" else "ETHA"
        tickers = [expected] + [ticker for ticker in flows if isinstance(ticker, str) and ticker not in (expected, "TOTAL")]
        base_status = _observation_status(health["status"], report_date, now_ms, 7)
        if item.get("ok") is not True:
            base_status = "error" if not item else "stale"
        partial = latest.get("partial") is True or item.get("ok") is not True
        if partial and base_status == "fresh":
            base_status = "partial"
        for ticker in tickers[:16] + ["TOTAL"]:
            # The total is the provider's reported asset-wide net flow. Summing
            # a displayed subset (or missing fund values as zero) is not valid.
            raw_value = latest.get("total") if ticker == "TOTAL" else flows.get(ticker)
            value = _number(raw_value) if data.get("units") == "US$m" else None
            value_usd = value * 1_000_000 if value is not None else None
            if value_usd is not None and not math.isfinite(value_usd):
                value_usd = None
            result.append({
                "id": f"etf:{asset}:{ticker}:{report_date}", "asset": asset, "ticker": _text(ticker, 15),
                "name": f"All {asset} spot ETFs" if ticker == "TOTAL" else FUND_NAMES.get(ticker, _text(ticker, 40)), "flow_usd": value_usd,
                "direction": "unknown" if value_usd is None else "inflow" if value_usd > 0 else "outflow" if value_usd < 0 else "flat",
                "report_date": report_date or None, "status": "unknown" if value_usd is None else base_status,
                "partial": partial or value_usd is None, "source_url": _url(item.get("source_url")),
                "kind": "etf_net_flow", "note": "Daily ETF net subscriptions/redemptions; not an on-chain wallet transfer.",
            })
    return result


def _whales(txs: dict, tx_health: dict, smart: dict, smart_health: dict, now_ms: int) -> list[dict]:
    result = []
    for index, row in enumerate(_rows(txs.get("txs"), 60)):
        observed = _instant(row.get("ts")) or _instant(row.get("time"))
        status = tx_health["status"]
        if observed is None or observed > now_ms + 300_000:
            status = "unknown"
        elif now_ms - observed > DAY_MS:
            status = "stale"
        result.append({
            "id": f"btc:{index}:{_text(row.get('hash'), 64)}", "asset": "BTC", "title": "Large BTC mempool transfer",
            "amount": _number(row.get("btc")), "unit": "BTC", "amount_usd": None, "direction": "unknown",
            "observed_at": _iso(observed), "source_url": _url(row.get("url")), "status": status,
            "kind": "unconfirmed_transfer", "note": "Unconfirmed output sum; sender, recipient and exchange direction are unverified.",
        })
    positions_as_of = _mapping(smart.get("fetch")).get("positions_as_of")
    observed = _instant(positions_as_of)
    for cohort in ("smart_money", "whales"):
        payload = _mapping(_mapping(smart.get("cohorts")).get(cohort))
        for row in _rows(payload.get("coins"), 6):
            net = _number(row.get("net_usd"))
            result.append({
                "id": f"hl:{cohort}:{_text(row.get('coin'), 15)}", "asset": _text(row.get("coin"), 15),
                "title": f"Hyperliquid {'smart money' if cohort == 'smart_money' else 'whales'} positioning",
                "amount": None, "unit": "USD", "amount_usd": net,
                "direction": "unknown" if net is None else "long" if net > 0 else "short" if net < 0 else "flat",
                "observed_at": _iso(observed), "source_url": "https://app.hyperliquid.xyz/leaderboard",
                "status": smart_health["status"] if observed is not None and observed <= now_ms + 300_000 and now_ms - observed <= 1_200_000 else "stale" if observed else "unknown",
                "kind": "cohort_position", "note": "Snapshot of open perpetual positions; not a new trade, wallet inflow, or proven copy edge.",
            })
    return result[:30]


def _markets(data: dict, health: dict, now_ms: int, crypto: bool = False) -> list[dict]:
    result = []
    for row in _rows(data.get("tickers" if crypto else "quotes"), 20):
        observed = _instant(row.get("time"))
        status = health["status"]
        if observed is None or observed > now_ms + 300_000:
            status = "unknown"
        elif now_ms - observed > (300_000 if crypto else 4 * DAY_MS):
            status = "stale"
        result.append({
            "symbol": _text(row.get("symbol"), 30), "name": _text(row.get("base") if crypto else row.get("name"), 60),
            "price": _number(row.get("price")), "change_pct": _number(row.get("change_pct")),
            "observed_at": _iso(observed), "status": status,
            "source_url": "https://data-api.binance.vision" if crypto else "https://finance.yahoo.com/",
        })
    return result


def _perps(now_ms: int) -> dict:
    """An optional existing engine snapshot; never discover accounts or import engines."""
    summary = {
        "status": "unavailable", "mode": None, "venue": "Binance USD-M",
        "positions_count": None, "observed_at": None,
        "reason": "Connect an existing Aegis engine snapshot to see its current status.",
    }
    directory = os.environ.get("KAIBA_AEGIS_ENGINE_STATE_DIR")
    if not directory:
        return summary
    try:
        path = Path(directory) / "aegis_perps_runtime_snapshot.json"
        meta = path.lstat()
        if not stat.S_ISREG(meta.st_mode) or path.is_symlink() or meta.st_size > MAX_CACHE_BYTES:
            raise ValueError("invalid runtime snapshot")
        with path.open("rb") as handle:
            raw = handle.read(MAX_CACHE_BYTES + 1)
        if len(raw) > MAX_CACHE_BYTES:
            raise ValueError("runtime snapshot exceeds size limit")
        snapshot = json.loads(raw)
        if not isinstance(snapshot, dict):
            raise ValueError("runtime snapshot invalid")
    except (OSError, ValueError, RecursionError):
        summary["reason"] = "The connected Aegis engine snapshot is unavailable or invalid."
        return summary
    service = _mapping(snapshot.get("service"))
    if service.get("venue") != "binance_usdm":
        summary.update(status="unknown", reason="The connected snapshot does not identify the Binance USD-M engine.")
        return summary
    mode = service.get("deployment_profile")
    summary["mode"] = mode if mode in {"paper", "testnet", "supervised_live", "tiny_live", "live", "livepaper", "replay", "shadow"} else None
    stamp = _number(snapshot.get("ts"))
    if stamp is None or stamp <= 0 or stamp != int(stamp):
        summary.update(status="unknown", reason="The engine snapshot has no valid observation time.")
        return summary
    summary["observed_at"] = _iso(int(stamp))
    if summary["observed_at"] is None or stamp > now_ms:
        summary.update(status="unknown", reason="The engine snapshot observation time is invalid or in the future.")
        return summary
    if now_ms - stamp > 120_000 or _mapping(snapshot.get("freshness")).get("runtime_stale") is True:
        summary.update(status="stale", reason="The last engine observation is stale; current positions are unknown.")
        return summary
    count = _mapping(snapshot.get("risk")).get("open_positions")
    summary["positions_count"] = count if type(count) is int and 0 <= count <= 100_000 else None
    summary.update(status="fresh", reason="Recent read-only runtime snapshot; execution and controls stay in Aegis.")
    return summary


def build_intelligence(*, cache_root: Path | None = None, now_ms: int | None = None) -> dict:
    """Read eight fixed caches and one optional snapshot; never fetch or execute."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    configured = os.environ.get("KAIBA_AEGIS_INTEL_DIR")
    root = cache_root if cache_root is not None else Path(configured) if configured else KAIBA_CACHE_ROOT if KAIBA_CACHE_ROOT.is_dir() else AEGIS_CACHE_ROOT
    data, health = {}, {}
    for key in SPECS:
        data[key], health[key] = _read(Path(root), key, now_ms)
    statuses = [row["status"] for row in health.values()]
    fresh = sum(value == "fresh" for value in statuses)
    stale = sum(value == "stale" for value in statuses)
    missing = sum(value == "missing" for value in statuses)
    bridge_status = "unavailable" if missing == len(SPECS) else "available" if fresh == len(SPECS) else "stale" if stale + missing == len(SPECS) else "partial"
    return {
        "as_of_ms": now_ms,
        "bridge": {"status": bridge_status, "mode": "cache_only", "fresh_count": fresh, "stale_count": stale, "missing_count": missing},
        "collectors": list(health.values()),
        "macro_events": _macro_events(data["calendar"], health["calendar"], now_ms),
        "macro_series": _macro_series(data["macro_fred"], health["macro_fred"], now_ms),
        "news": _news(data["news"], health["news"], now_ms),
        "institutional_flows": _institutions(data["etf_flows"], health["etf_flows"], now_ms) if data["etf_flows"] else [],
        "whales": _whales(data["btc_whale_tx"], health["btc_whale_tx"], data["smart_money"], health["smart_money"], now_ms),
        "markets": _markets(data["tickers"], health["tickers"], now_ms, True) + _markets(data["markets"], health["markets"], now_ms),
        "perps": _perps(now_ms),
        "notes": [
            "Aegis intelligence is read from existing caches; opening this dashboard makes no provider requests.",
            "ETF net subscriptions/redemptions, unconfirmed BTC transfers and perpetual positioning are separate measures.",
            "Missing flow values and unverified exchange attribution remain unknown. All observations retain their original dates.",
        ],
    }
