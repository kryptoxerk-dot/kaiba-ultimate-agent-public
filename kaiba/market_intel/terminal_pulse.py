"""The terminal pulse: wallets, trending tokens and chain volume for the unified dashboard (TERMINAL-PULSE-BACKEND-CLAUDE-20261103).

Owner, 2026-11-03: "highlight best wallets found daily, trending tokens/chains, most volume, most profitable
wallets... basically a Bloomberg terminal for shitcoins". Codex's ``dashboard/ultimate_pulse.py`` renders this
file; this module only produces it, read-only, into ``data/terminal-pulse/latest.json`` (schema 1).

Sources, and what each may honestly claim:

* **wallets**: the latest <= 5,000 ``wallet_score_history`` rows (rowid order), joined by primary key to
  ``wallet_scores`` and ``wallets``, latest per wallet. It is a LATEST-REVIEWED SAMPLE, never "the world's
  best wallets". A grade is Kaiba's evidence score, not profit; ``realized_pnl_usd`` is stored source history
  whose window is not proven (``pnl_window: "unknown"``), never "today's profit" and never a copy signal.
  Exported: the union of the 100 largest stored PnL, the 100 most recently scored and the 100 first seen
  today (WIB), <= 300 unique, each tagged with why it was selected.
* **tokens**: the EXISTING GMGN trending cache that ``kaiba.ingest.gmgn_feeds`` already keeps
  (``market trending --interval 1h --limit 50``) for five chains. No new GMGN call. GMGN's ``volume`` carries
  no window, so it is ``volume_usd`` with ``volume_window: "unknown"``; ``trend_window`` is the listing's 1h.
* **chains**: keyless DefiLlama ``/overview/dexs/{chain}``, the reported 24h DEX volume and its 1-day change
  for five fixed chains. Chain totals only (no protocol rows, so no multi-chain double count). Each chain is
  re-asked at most once an hour, so at most five requests an hour.

Every database read: a fresh ``mode=ro`` connection, ``query_only``, WAL <= 1.5 GB, a 3 s deadline, LIMIT or
primary-key lookups only, closed in ``finally``; no connection is held across a network call.

    python -m kaiba.market_intel.terminal_pulse run
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

SCHEMA = 1
HISTORY_ROWS = 5000
SELECT_EACH = 100
MAX_BYTES = 2 * 1024 * 1024
WAL_MAX_BYTES = 1_500_000_000
QUERY_DEADLINE_S = 3.0
LOOKUP_CHUNK = 400
WIB_OFFSET_MS = 7 * 3_600_000
DAY_MS = 86_400_000
TOKEN_CHAINS = ("sol", "eth", "bsc", "base", "robinhood")
TOKEN_STALE_MS = 15 * 60_000
DEX_CHAINS = ("solana", "ethereum", "base", "bsc", "arbitrum")
DEX_REFRESH_MS = 3_600_000
#: Codex review 2026-11-03: one DefiLlama refusal is provider-wide. A 403/429 stops every remaining chain
#: request in the run and persists a cooldown (Retry-After when longer, at least six hours) that later
#: processes honour; an unreadable state file cannot grant capacity, so it means the same cooldown.
DEX_REFUSED_BACKOFF_MS = 6 * 3_600_000
PROVIDER_KEY = "_provider"
DEX_URL = "https://api.llama.fi/overview/dexs/{chain}?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true"
BASIS = {
    "kaiba-wallet-tape-v1": "Kaiba swap tape (wallets we track), realized PnL over the tape it holds",
    "kaiba-wallet-gmgn-v1": "GMGN-reported wallet statistics as stored by the grader",
    "kaiba-wallet-v1": "Kaiba grader v1 (source mix as stored)",
}
LABELS = {
    "wallets": ("A latest-reviewed sample: the wallets behind the newest <= 5,000 grading rows. Not a ranking of "
                "the best wallets anywhere. Grades are Kaiba's evidence scores, not profit. Realized PnL is stored "
                "source history with an unproven window; it is not today's profit and not a signal to copy."),
    "tokens": ("GMGN's 1h trending list as Kaiba last cached it, per chain. GMGN does not say which window its "
               "volume covers, so it is labelled unknown."),
    "chains": "DefiLlama's reported 24h DEX volume for the chains listed only; chains not listed are not covered.",
}


class Deferred(Exception):
    """The database cannot be read now under the read rules (WAL too large, deadline hit)."""


# ------------------------------------------------------------------ database (read rules)


def _query(db: Path, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    wal = Path(str(db) + "-wal")
    if wal.exists() and wal.stat().st_size > WAL_MAX_BYTES:
        raise Deferred("wal_over_admission")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=1")
        deadline = time.monotonic() + QUERY_DEADLINE_S
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
        try:
            return conn.execute(sql, list(params)).fetchall()
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc):
                raise Deferred("query_deadline") from exc
            raise
    finally:
        conn.close()


def _lookup(db: Path, table: str, cols: str, keys: list[tuple[str, str]]) -> dict[tuple[str, str], sqlite3.Row]:
    """Primary-key lookups in chunks: ``(chain, address) IN (VALUES ...)``."""
    out: dict[tuple[str, str], sqlite3.Row] = {}
    for k in range(0, len(keys), LOOKUP_CHUNK):
        chunk = keys[k:k + LOOKUP_CHUNK]
        marks = ",".join("(?,?)" for _ in chunk)
        for row in _query(db, f"SELECT chain, address, {cols} FROM {table} WHERE (chain, address) IN (VALUES {marks})",
                          [x for key in chunk for x in key]):
            out[(row["chain"], row["address"])] = row
    return out


def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def wib_day(ms: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime((ms + WIB_OFFSET_MS) / 1000))


def _basis(model_version: str, receipts_json: str | None) -> str:
    base = BASIS.get(model_version, f"grader {model_version}")
    try:
        rec = json.loads(receipts_json or "[]")
    except ValueError:
        rec = []
    first = next((r for r in rec if isinstance(r, dict) and r.get("provider")), None)
    if first:
        base += f" [{first.get('provider')}/{first.get('endpoint')}/{first.get('basis')}]"
    return base


def read_wallets(db: Path, now_ms: int) -> tuple[list[dict], dict, dict]:
    hist = _query(db, "SELECT id, chain, address FROM wallet_score_history ORDER BY id DESC LIMIT ?", [HISTORY_ROWS])
    keys = list(dict.fromkeys((r["chain"], r["address"]) for r in hist))  # latest per wallet, newest first
    scores = _lookup(db, "wallet_scores", "grade, model_version, realized_pnl_usd, win_rate, closed_trades, "
                                          "median_hold_s, blockers_json, receipts_json, scored_at_ms", keys)
    meta = _lookup(db, "wallets", "name, first_seen_ms", keys)
    today = wib_day(now_ms)
    rows = []
    for key in keys:
        s = scores.get(key)
        if s is None:
            continue
        m = meta.get(key)
        try:
            blockers = [str(b) for b in json.loads(s["blockers_json"] or "[]")][:8]
        except ValueError:
            blockers = []
        win = _num(s["win_rate"])
        first_seen = int(m["first_seen_ms"]) if m is not None and m["first_seen_ms"] is not None else None
        rows.append({
            "chain": key[0], "address": key[1], "name": (m["name"] if m is not None else None),
            "first_seen_ms": first_seen, "found_wib_day": wib_day(first_seen) if first_seen else None,
            "scored_at_ms": int(s["scored_at_ms"]), "grade": s["grade"], "model_version": s["model_version"],
            "realized_pnl_usd": _num(s["realized_pnl_usd"]), "pnl_window": "unknown",
            "win_rate": win if win is not None and 0.0 <= win <= 1.0 else None,
            "closed_trades": s["closed_trades"], "median_hold_s": s["median_hold_s"], "blockers": blockers,
            "basis": _basis(s["model_version"], s["receipts_json"]),
        })
    by_pnl = sorted((r for r in rows if r["realized_pnl_usd"] is not None), key=lambda r: -r["realized_pnl_usd"])
    recent = sorted(rows, key=lambda r: -r["scored_at_ms"])
    found_today = sorted((r for r in rows if r["found_wib_day"] == today), key=lambda r: -(r["first_seen_ms"] or 0))
    picked: dict[tuple[str, str], dict] = {}
    for tag, group in (("pnl_top100", by_pnl), ("recent100", recent), ("found_today100", found_today)):
        for r in group[:SELECT_EACH]:
            entry = picked.setdefault((r["chain"], r["address"]), {**r, "selected_by": []})
            entry["selected_by"].append(tag)
    chains: dict[str, int] = {}
    for r in rows:
        chains[r["chain"]] = chains.get(r["chain"], 0) + 1
    coverage = {"history_rows_read": len(hist), "unique_wallets_in_sample": len(keys), "with_score_row": len(rows),
                "exported": len(picked), "found_today_wib": len(found_today), "wib_day": today,
                "by_chain_in_sample": chains, "selection": "union of top 100 stored realized PnL, 100 most recently "
                "scored and 100 first seen today (WIB)", "label": LABELS["wallets"]}
    health = {"status": "ok", "observed_ms": now_ms,
              "newest_scored_at_ms": max((r["scored_at_ms"] for r in rows), default=None)}
    return list(picked.values()), coverage, health


# ------------------------------------------------------------------ tokens (existing GMGN cache only)


def _trending_key(chain: str) -> str:
    from kaiba.core.schemas import Chain
    from kaiba.ingest import gmgn_feeds as gf

    group, command, flags = gf.invocation(Chain(chain), "trending")
    return "gmgn-cli " + " ".join([group, command, *gf._flags_to_argv(flags), "--raw"])


def _read_cache(key: str) -> tuple[Any, int] | None:
    from kaiba.providers import _http

    path = _http.cache_path("gmgn", key)
    try:
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            return None
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc.get("data"), int(doc["fetched_ms"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def read_tokens(now_ms: int, cache: Callable[[str], tuple[Any, int] | None] = _read_cache) -> tuple[list, dict, dict]:
    rows, health, per_chain = [], {}, {}
    for chain in TOKEN_CHAINS:
        hit = cache(_trending_key(chain))
        if hit is None:
            health[f"gmgn_trending:{chain}"] = {"status": "missing", "fetched_ms": None, "observed_ms": now_ms}
            continue
        data, fetched = hit
        listed = data if isinstance(data, list) else (data.get("rank") if isinstance(data, dict) else None)
        if not isinstance(listed, list):
            health[f"gmgn_trending:{chain}"] = {"status": "invalid", "fetched_ms": fetched, "observed_ms": now_ms}
            continue
        age = now_ms - fetched
        health[f"gmgn_trending:{chain}"] = {"status": "ok" if age <= TOKEN_STALE_MS else "stale", "fetched_ms": fetched,
                                            "observed_ms": now_ms, "age_s": round(age / 1000), "rows": len(listed)}
        per_chain[chain] = 0
        for r in listed[:50]:
            if not isinstance(r, dict) or not r.get("address"):
                continue
            created = _num(r.get("creation_timestamp"))
            rows.append({
                "chain": chain, "address": str(r["address"]), "symbol": r.get("symbol"), "name": r.get("name"),
                "rank": r.get("rank"), "observed_ms": fetched, "market_cap_usd": _num(r.get("market_cap")),
                "liquidity_usd": _num(r.get("liquidity")), "volume_usd": _num(r.get("volume")),
                "volume_window": "unknown", "volume_1h_usd": None, "volume_24h_usd": None, "trend_window": "1h",
                "price_change_1h_pct": _num(r.get("price_change_percent1h")), "swaps": r.get("swaps"),
                "buys": r.get("buys"), "sells": r.get("sells"), "holder_count": r.get("holder_count"),
                "launchpad": r.get("launchpad"), "created_ms": int(created * 1000) if created else None,
                "source": "gmgn market trending --interval 1h --limit 50 (Kaiba's cached feed)",
            })
            per_chain[chain] += 1
    coverage = {"chains": list(TOKEN_CHAINS), "rows_by_chain": per_chain, "label": LABELS["tokens"]}
    return rows, coverage, health


# ------------------------------------------------------------------ chains (DefiLlama, at most hourly)


def _refusal_until_ms(exc: BaseException, now_ms: int) -> int | None:
    """When ``exc`` is a provider refusal (HTTP 403/429), the cooldown end: Retry-After (seconds or an
    HTTP date) when it is later than the six-hour floor. ``None`` for any other failure."""
    resp = getattr(exc, "response", None)
    if getattr(resp, "status_code", None) not in (403, 429):
        return None
    until = now_ms + DEX_REFUSED_BACKOFF_MS
    raw = (getattr(resp, "headers", None) or {}).get("Retry-After")
    try:
        secs = float(raw)
        if math.isfinite(secs) and secs > 0:
            until = max(until, now_ms + int(secs * 1000))
    except (TypeError, ValueError, OverflowError):
        try:
            when = parsedate_to_datetime(str(raw or ""))
            if when.tzinfo is not None:
                until = max(until, int(when.timestamp() * 1000))
        except (TypeError, ValueError, IndexError, OverflowError):
            pass
    return until


def read_chains(now_ms: int, state_path: Path, http_get: Callable[[str], Any] | None) -> tuple[list, dict, dict]:
    state: Any = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = None
        if not isinstance(state, dict):  # unreadable state cannot grant provider capacity
            state = {PROVIDER_KEY: {"cooldown_until_ms": now_ms + DEX_REFUSED_BACKOFF_MS,
                                    "reason": "state_unreadable", "since_ms": now_ms}}
    provider = state.get(PROVIDER_KEY) if isinstance(state.get(PROVIDER_KEY), dict) else {}
    cooldown_until = int(provider.get("cooldown_until_ms") or 0)
    rows, health, requests = [], {}, 0
    for chain in DEX_CHAINS:
        prev = state.get(chain) if isinstance(state.get(chain), dict) else None
        due = prev is None or now_ms - int(prev.get("attempted_ms") or 0) >= DEX_REFRESH_MS
        cooling = cooldown_until > now_ms
        if due and http_get is not None and not cooling:
            requests += 1
            try:
                body = http_get(DEX_URL.format(chain=chain))
                total, change = _num(body.get("total24h")), _num(body.get("change_1d"))
                if total is None:
                    raise ValueError("no total24h")
                prev = {"volume_24h_usd": total, "change_24h_pct": change, "fetched_ms": now_ms,
                        "attempted_ms": now_ms, "ok": True, "error": None}
            except Exception as exc:  # noqa: BLE001 - a failed chain is reported, never invented
                prev = {**(prev or {}), "attempted_ms": now_ms, "ok": False, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
                refused_until = _refusal_until_ms(exc, now_ms)
                if refused_until is not None:  # provider-wide: no other chain is asked
                    cooldown_until = max(cooldown_until, refused_until)
                    state[PROVIDER_KEY] = {"cooldown_until_ms": cooldown_until, "since_ms": now_ms,
                                           "reason": f"HTTP {exc.response.status_code}"}  # type: ignore[attr-defined]
            state[chain] = prev
        if prev and prev.get("volume_24h_usd") is not None:
            rows.append({"chain": chain, "observed_ms": prev.get("fetched_ms"), "volume_24h_usd": prev["volume_24h_usd"],
                         "change_24h_pct": prev.get("change_24h_pct"), "source": "DefiLlama /overview/dexs (reported)"})
        status = "ok" if prev and prev.get("ok") else "error" if prev else "missing"
        if cooldown_until > now_ms and not (prev and prev.get("ok") and prev.get("attempted_ms") == now_ms):
            status = "cooldown"
        health[f"defillama:{chain}"] = {"status": status, "fetched_ms": (prev or {}).get("fetched_ms"),
                                        "observed_ms": now_ms, "error": (prev or {}).get("error"),
                                        "cooldown_until_ms": cooldown_until if cooldown_until > now_ms else None}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    os.replace(tmp, state_path)
    return rows, {"covered": list(DEX_CHAINS), "requests_this_run": requests, "label": LABELS["chains"],
                  "provider_cooldown_until_ms": cooldown_until if cooldown_until > now_ms else None}, health


# ------------------------------------------------------------------ the snapshot


def build(*, now_ms: int, db: Path, out_dir: Path, http_get: Callable[[str], Any] | None,
          cache: Callable[[str], tuple[Any, int] | None] = _read_cache) -> dict:
    health: dict[str, Any] = {}
    try:
        wallets, wcov, wh = read_wallets(db, now_ms)
        health["wallet_db"] = wh
    except (Deferred, sqlite3.Error) as exc:
        wallets, wcov = [], {"label": LABELS["wallets"]}
        health["wallet_db"] = {"status": "deferred", "observed_ms": now_ms, "error": f"{type(exc).__name__}: {exc}"[:160]}
    tokens, tcov, th = read_tokens(now_ms, cache)
    chains, ccov, ch = read_chains(now_ms, out_dir / "defillama_state.json", http_get)
    health.update(th)
    health.update(ch)
    doc = {"schema": SCHEMA, "observed_ms": now_ms, "wallets": wallets, "tokens": tokens, "chains": chains,
           "coverage": {"wallets": wcov, "tokens": tcov, "chains": ccov}, "source_health": health}
    raw = json.dumps(doc, sort_keys=True, allow_nan=False, default=str).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError(f"snapshot {len(raw)} bytes exceeds {MAX_BYTES}")
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / ".latest.json.tmp"
    tmp.write_bytes(raw)
    os.replace(tmp, out_dir / "latest.json")
    return doc


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] != ["run"]:
        print("usage: python -m kaiba.market_intel.terminal_pulse run")
        return 2
    import httpx

    from kaiba.core.config import get_settings

    settings = get_settings()
    with httpx.Client(timeout=15.0, headers={"User-Agent": "kaiba-terminal-pulse/1 (public data)"}) as client:
        def get(url: str) -> Any:
            r = client.get(url)
            r.raise_for_status()
            return r.json()

        doc = build(now_ms=int(time.time() * 1000), db=Path(settings.db_path),
                    out_dir=Path(settings.kaiba_data_dir) / "terminal-pulse", http_get=get)
    print(json.dumps({"wallets": len(doc["wallets"]), "tokens": len(doc["tokens"]), "chains": len(doc["chains"]),
                      "health": {k: v.get("status") for k, v in doc["source_health"].items()}}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
