"""Keep the vendored Aegis market-intel caches current on the Kaiba box (MARKET-INTEL-REFRESH-CLAUDE-20261103).

Owner, 2026-11-03: one dashboard for Kaiba and Aegis, with smart-wallet and whale alerts, ETF and
institution flows, and a daily macro view. The Aegis collectors (``kaiba.market_intel.aegis``, copied
byte-identical from Aegis Omni, see ``aegis/SOURCE.md``) only ever ran inside the Aegis terminal's own
process on the owner's PC, so their caches stopped on 2026-10-20. This one-shot runner, fired by
``deploy/systemd/kaiba-market-intel.timer``, runs on the box instead.

One run:

* every SELECTED collector that is due runs once, sequentially, through Aegis's own
  ``run_collector`` (a failure keeps the last good data and marks it stale, never shows it as fresh);
* "due" is Aegis's own rule (``Refresher.due``): the collector's interval after a success, a fifth of
  it (at least a minute) after a failure;
* the run stops before starting a collector past its total time budget;
* it skips everything at the disk guard's HALT band, or when the cache directory exceeds its cap.

Keyless public endpoints only. Nothing here reads a Kaiba credential or the trading database, makes a
GMGN/RPC/Alchemy call, or can trade. The cache directory comes from ``AEGIS_TERMINAL_DATA`` and the
runner refuses to run without it, so it never writes into the source tree.

    AEGIS_TERMINAL_DATA=~/kaiba/data/market-intel python -m kaiba.market_intel.refresh
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

#: Collectors the dashboard uses (Codex's dashboard/ultimate_intel.py reads calendar, macro_fred, news,
#: etf_flows, btc_whale_tx, tickers and markets) plus the cheap institution and positioning feeds.
SELECTED = ("tickers", "markets", "crypto_macro", "macro_fred", "calendar", "news", "etf_flows", "cot",
            "treasuries", "btc_whale_tx", "hl_market", "binance_positioning")
#: Left out on purpose, and why. The dashboard shows these as missing, never as fresh.
EXCLUDED = {
    "smart_money": "the Hyperliquid leaderboard is about 40 MB a fetch; the box has an OOM history",
    "x_feed": "the X API is paid; no owner token is used here",
    "networth": "the owner's own portfolio, not market data",
    "social": "not requested for the dashboard",
    "reddit": "not requested for the dashboard",
}
#: 2026-11-03 (MARKET-INTEL-EXTERNAL-KEYS-CLAUDE-20261103, for Codex DESKTOP-INTEL-SYNC-20261103): the
#: owner's PC fetches the three sources that refuse the box's IP and pushes their caches here, with
#: the source's own clocks. A key named in this variable is skipped ENTIRELY (never run, so its
#: mirrored cache is never overwritten by a box-side failure) and reported as external. Only these
#: three may be named; any other name refuses the whole run rather than silently skipping a feed.
#: Unset (the default) changes nothing.
EXTERNAL_ENV = "KAIBA_MARKET_INTEL_EXTERNAL_KEYS"
EXTERNAL_ALLOWED = frozenset({"etf_flows", "cot", "markets"})
CACHE_CAP_BYTES = 50 * 1024**2
#: MEASURED on the box's first run, 2026-11-03: farside.co.uk (etf_flows) and the CFTC (cot) answer the
#: datacenter IP 403, Yahoo (markets) 429; all three worked from a residential IP. Aegis's own retry
#: after a failure is a fifth of the interval, which for ``markets`` is a request burst every two
#: minutes into a 429. A refusal waits at least this long instead; any other failure keeps Aegis's rule.
REFUSED_BACKOFF_S = 6 * 3600
REFUSED_MARKERS = ("403 Forbidden", "429 Too Many Requests")
BUDGET_S = 240.0
MODULES = ("market", "macro", "econ_calendar", "news", "institutions", "whales")


def collectors() -> dict[str, Any]:
    """The selected collectors, by key. Raises if one is missing (a renamed key must not go silent)."""
    import importlib

    found = {}
    for name in MODULES:
        mod = importlib.import_module(f"kaiba.market_intel.aegis.{name}")
        for c in getattr(mod, "COLLECTORS", []):
            found[c.key] = c
    missing = [k for k in SELECTED if k not in found]
    if missing:
        raise RuntimeError(f"selected collectors not found: {missing}")
    return {k: found[k] for k in SELECTED}


def is_due(c: Any, doc: dict | None, now: float) -> bool:
    """Aegis's own rule (``aegis.base.Refresher.due``), without its thread pool, except that a source
    that REFUSED us (:data:`REFUSED_MARKERS`) is not asked again for :data:`REFUSED_BACKOFF_S`."""
    doc = doc or {}
    last = doc.get("attempted_ts") or doc.get("fetched_ts") or 0
    wait = c.interval_s if doc.get("ok", True) else max(60, c.interval_s // 5)
    if not doc.get("ok", True) and any(m in str(doc.get("error") or "") for m in REFUSED_MARKERS):
        wait = max(wait, REFUSED_BACKOFF_S)
    return now - float(last) >= wait


def external_keys(environ: Any = None) -> tuple[frozenset[str], str | None]:
    """``(keys, refusal)`` from :data:`EXTERNAL_ENV`: comma-separated, exact lowercase names."""
    raw = (os.environ if environ is None else environ).get(EXTERNAL_ENV, "") or ""
    names = {x.strip() for x in raw.split(",") if x.strip()}
    unknown = sorted(names - EXTERNAL_ALLOWED)
    if unknown:
        return frozenset(), f"{EXTERNAL_ENV} names keys that may not be external: {unknown}"
    return frozenset(names), None


def dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def run(*, now: float | None = None, http_factory: Callable[[], Any] | None = None,
        disk_usage: Callable[[Path], Any] = shutil.disk_usage, budget_s: float = BUDGET_S,
        clock: Callable[[], float] = time.monotonic, only: Iterable[str] | None = None) -> dict[str, Any]:
    from kaiba.market_intel.aegis import base
    from kaiba.ops import disk_guard

    if not os.environ.get("AEGIS_TERMINAL_DATA"):
        return {"status": "refused", "reason": "AEGIS_TERMINAL_DATA is not set"}
    external, refusal = external_keys()
    if refusal:
        return {"status": "refused", "reason": refusal}
    root = base.cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    use = disk_guard.usage_of(root, disk_usage)
    pct = 100.0 * use.used / max(1, use.used + use.free)
    if pct >= disk_guard.HALT_PCT:
        return {"status": "skipped", "reason": f"disk_halt:{pct:.1f}%"}
    size = dir_bytes(root)
    if size > CACHE_CAP_BYTES:
        return {"status": "skipped", "reason": f"cache_over_cap:{size}"}
    table = collectors()
    keys = [k for k in SELECTED if only is None or k in set(only)]
    started = clock()
    wall = time.time() if now is None else now
    out: dict[str, Any] = {"status": "ok", "disk_pct": round(pct, 1), "cache_bytes": size, "ran": {}, "not_due": [],
                           "budget_stopped": [], "external": sorted(k for k in keys if k in external)}
    for key in keys:
        if key in external:
            continue  # kept current by an external writer; never read, run or overwritten here
        c = table[key]
        if not is_due(c, base.read_cache(key), wall):
            out["not_due"].append(key)
            continue
        if clock() - started >= budget_s:
            out["budget_stopped"].append(key)
            continue
        http = (http_factory or (lambda: base.LiveHttp(timeout=20.0)))()
        try:
            doc = base.run_collector(c, http)
        finally:
            close = getattr(getattr(http, "_c", None), "close", None)
            if close:
                close()
        out["ran"][key] = {"ok": doc.get("ok"), "stale": doc.get("stale"), "duration_s": doc.get("duration_s"),
                           "error": (doc.get("error") or None) and str(doc["error"])[:160]}
    return out


def main() -> int:
    report = run()
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("status") in ("ok", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
