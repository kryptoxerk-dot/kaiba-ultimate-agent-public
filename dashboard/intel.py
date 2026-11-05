"""Token, wallet and alpha intelligence for the trading terminal (2026-10-20).

The owner asked for one screen that shows what the agent sees: the early-alpha stream, a token's
full intelligence, a wallet's record, the live book and the lanes that trade on their own. This
module is the read side of that screen. Same rules as :mod:`dashboard.data`:

* **Never fabricate a number.** Every figure carries where it came from (``basis``/``source``) or
  is ``None``; the templates print "—", never a plausible zero.
* **Never let one source take the page down.** Each reader is wrapped and returns empty on error.
* **Every query is an indexed, bounded read** (AGENTS.md DATABASE READ RULE): primary-key or
  ``(chain, token|wallet, ts)`` index lookups with a LIMIT, run under a progress-handler deadline.
  Indexes relied on, verified on the box 2026-10-20: ``tokens`` PK + ``idx_tokens_created``,
  ``token_dossiers`` PK, ``idx_triage_token``/``idx_triage_verdict``, ``creators`` PK,
  ``deployer_stats`` PK, ``token_bundles`` PK, ``idx_curve_snap_token``, ``idx_swaps_token``,
  ``idx_swaps_wallet``, ``wallet_scores`` PK, ``idx_wsh``, ``entity_members`` PK +
  ``idx_entity_members``, ``idx_position_marks``, ``idx_positions_token``, ``idx_positions_open``,
  ``idx_x_token_obs_token``, ``tweet_sources`` PK, ``idx_alpha_signals_seen``,
  ``idx_alpha_feed_first_seen_ts``/``_token``, ``idx_signals_token``, ``idx_events_kind``,
  ``idx_decisions_lane``, ``idx_native_prices_chain_ts``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from dashboard import data, trading

log = logging.getLogger(__name__)

DAY_MS = 86_400_000
CHAINS = ("sol", "bsc", "robinhood")
EVM_CHAINS = ("robinhood", "bsc")
SOL_ADDR = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
EVM_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
QUERY_S = 8.0
MAX_SERIES_POINTS = 600
NATIVE = {"sol": "SOL", "bsc": "BNB", "robinhood": "ETH", "eth": "ETH", "base": "ETH"}
NATIVE_DECIMALS = {"sol": 9, "bsc": 18, "robinhood": 18, "eth": 18, "base": 18}
#: Price chain whose samples price a chain's native asset (robinhood's native asset IS ether).
NATIVE_PRICE_CHAIN = {"sol": "sol", "bsc": "bsc", "robinhood": "eth"}

_cache: dict[str, tuple[float, Any]] = {}


# --------------------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------------------


def now_ms() -> int:
    return int(time.time() * 1000)


def _q(sql: str, params: tuple = (), seconds: float = QUERY_S) -> list[dict[str, Any]]:
    rows = trading._deadline_rows(sql, params, seconds=seconds)
    return rows or []


def _one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    rows = _q(sql, params)
    return rows[0] if rows else None


def _cached(key: str, ttl_s: float, build):
    hit = _cache.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < ttl_s:
        return hit[1]
    value = build()
    _cache[key] = (now, value)
    return value


def clear_caches() -> None:
    _cache.clear()


def _jl(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _dec(raw: Any) -> Decimal | None:
    if raw in (None, ""):
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _f(raw: Any) -> float | None:
    d = _dec(raw)
    return float(d) if d is not None else None


def native_amount(base_units: Any, chain: str) -> float | None:
    d = _dec(base_units)
    if d is None:
        return None
    return float(d / (Decimal(10) ** NATIVE_DECIMALS.get(chain, 9)))


def safe_url(url: Any) -> str | None:
    """Only http(s) links from outside sources ever become an href."""
    text = str(url or "").strip()
    return text if text.startswith(("https://", "http://")) else None


def valid_chain(chain: str | None) -> str | None:
    return chain if chain in CHAINS else None


def clean_address(chain: str | None, address: str | None) -> str | None:
    """The address in the form the database stores it, or None if it is not one."""
    text = str(address or "").strip()
    if chain == "sol":
        return text if SOL_ADDR.match(text) else None
    if chain in EVM_CHAINS:
        return text.lower() if EVM_ADDR.match(text) else None
    return None


def explorer_links(chain: str, address: str, *, kind: str = "token") -> list[tuple[str, str]]:
    """Outside links we are sure of. Robinhood has none we have verified, so it gets none."""
    if chain == "sol":
        if kind == "wallet":
            return [("Solscan", f"https://solscan.io/account/{address}"),
                    ("GMGN", f"https://gmgn.ai/sol/address/{address}")]
        links = [("DexScreener", f"https://dexscreener.com/solana/{address}"),
                 ("GMGN", f"https://gmgn.ai/sol/token/{address}"),
                 ("Solscan", f"https://solscan.io/token/{address}")]
        if address.endswith("pump"):
            links.append(("pump.fun", f"https://pump.fun/coin/{address}"))
        return links
    if chain == "bsc":
        if kind == "wallet":
            return [("BscScan", f"https://bscscan.com/address/{address}"),
                    ("GMGN", f"https://gmgn.ai/bsc/address/{address}")]
        return [("DexScreener", f"https://dexscreener.com/bsc/{address}"),
                ("GMGN", f"https://gmgn.ai/bsc/token/{address}"),
                ("BscScan", f"https://bscscan.com/token/{address}")]
    return []


def _symbol_map(pairs: set[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for chain, address in list(pairs)[:400]:
        row = _one("SELECT symbol, name, launchpad, created_ms, migrated_ms FROM tokens WHERE chain=? AND address=?",
                   (chain, address))
        if row:
            out[(chain, address)] = row
    return out


# --------------------------------------------------------------------------------------
# prices of the native assets, and the ticker
# --------------------------------------------------------------------------------------


def native_prices() -> dict[str, dict[str, Any]]:
    def build() -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for chain, src in NATIVE_PRICE_CHAIN.items():
            row = _one("SELECT price_usd, ts_ms FROM native_prices WHERE chain=? ORDER BY ts_ms DESC LIMIT 1", (src,))
            if not row:
                continue
            old = _one("SELECT price_usd FROM native_prices WHERE chain=? AND ts_ms <= ? ORDER BY ts_ms DESC LIMIT 1",
                       (src, int(row["ts_ms"]) - DAY_MS))
            px, prev = _f(row["price_usd"]), _f(old["price_usd"]) if old else None
            out[chain] = {"symbol": NATIVE[chain], "price": px, "ts_ms": int(row["ts_ms"]),
                          "change_24h": (px / prev - 1) if px and prev else None}
        return out

    return _cached("native", 20.0, build)


def native_usd(chain: str) -> float | None:
    got = native_prices().get(chain)
    return got.get("price") if got else None


def _paper_rule() -> Any:
    """``paper_report.PaperFilter`` over paper rows closed in the last day (the ticker's UTC day and the
    activity feed's six hours both fit inside it), or ``None`` when it cannot be built -- and ``None``
    is DENY: ``trading._paper_exclusion`` then counts no paper row (``paper_filter_unavailable``).

    2026-10-23 (step 3 follow-up): the header on every console page summed EVERY closed shadow position
    today into "paper today $X / n closed / w won", including the rows the scoreboard leaves out
    (dossier-priced robinhood/bsc entries, untagged eth/base, pre-cutoff, booked write-offs). It now
    applies the scoreboard's own rule, the one the report, the console scoreboard and the agent's tool
    share (``paper_report.paper_filter`` + ``outcome_exclusion``), never a copy of it."""

    def build() -> Any:
        conn = data._conn()
        if conn is None:
            return None
        try:
            from kaiba.learning import paper_report

            return paper_report.paper_filter(conn, now_ms(), days=1)
        except Exception as exc:  # noqa: BLE001 - deny, and say so through the reason
            log.info("intel: paper filter unavailable: %s", exc)
            return None

    return _cached("paper_rule", 5.0, build)


def _paper_not_counted(flt: Any, row: dict[str, Any]) -> str | None:
    """Why the scoreboard leaves this closed PAPER row out (its reason key), or ``None``."""
    return trading._paper_exclusion(flt, row, trading._ret(row.get("cost"), row.get("realized")))


def ticker() -> dict[str, Any]:
    """The bar across the top: prices, the mode switches, today's money and the book size.

    Paper money counts only the closes the paper scoreboard counts (:func:`_paper_rule`), each capped at
    the scoreboard's +300% (``trading.OUTCOME_CAP``); the rest are ``not_counted``, by reason."""

    def build() -> dict[str, Any]:
        cfg = data.risk_config()
        day = data.day_start_ms()
        closed = _q("SELECT position_id, mode, chain, opened_ms, exit_reason, CAST(cost_native AS TEXT) AS cost, "
                    "CAST(realized_native AS TEXT) AS realized FROM positions WHERE closed_ms >= ? LIMIT 5000", (day,))
        money: dict[str, dict[str, Any]] = {}
        flt = _paper_rule() if any(r["mode"] not in ("live", "canary") for r in closed) else None
        for r in closed:
            live = r["mode"] in ("live", "canary")
            bucket = "live" if live else "paper"
            m = money.setdefault(bucket, {"n": 0, "usd": 0.0, "wins": 0, "priced": True})
            realized = native_amount(r["realized"], r["chain"])
            if not live:
                m.setdefault("not_counted", 0)
                m.setdefault("not_counted_by", {})
                why = _paper_not_counted(flt, r)
                if why is not None:
                    m["not_counted"] += 1
                    label = trading.EXCLUSION_LABELS.get(why, why)
                    m["not_counted_by"][label] = m["not_counted_by"].get(label, 0) + 1
                    continue
                cost = native_amount(r["cost"], r["chain"])
                if realized is not None and cost is not None and cost > 0:
                    realized = min(realized, cost * trading.OUTCOME_CAP)
            m["n"] += 1
            if realized is not None and realized > 0:
                m["wins"] += 1
            px = native_usd(r["chain"])
            if realized is None or px is None:
                m["priced"] = False
                continue
            m["usd"] += realized * px
        open_rows = _q("SELECT mode, COUNT(*) AS n FROM positions WHERE closed_ms IS NULL GROUP BY mode")
        live_lanes: list[str] = []
        if cfg is not None:
            from kaiba.core.schemas import Lane

            for key in (cfg.lanes or {}):
                name = str(getattr(key, "value", key))
                try:
                    if cfg.effective_mode(Lane(name)).value in ("live", "canary"):
                        live_lanes.append(name)
                except Exception:  # noqa: BLE001 - a lane the enum does not know
                    continue
        return {
            "live_lanes": live_lanes,
            "prices": native_prices(),
            "global_mode": str(getattr(getattr(cfg, "global_mode", None), "value", "unknown")) if cfg else "unknown",
            "entries_paused": bool(getattr(cfg, "entries_paused", False)) if cfg else None,
            "reduce_only": bool(getattr(cfg, "reduce_only", False)) if cfg else None,
            "kill_switch": bool(getattr(cfg, "kill_switch", False)) if cfg else None,
            "today": money,
            "open": {r["mode"]: int(r["n"]) for r in open_rows},
            "at_ms": now_ms(),
        }

    return _cached("ticker", 5.0, build)


# --------------------------------------------------------------------------------------
# the book: open positions with protection's own live marks
# --------------------------------------------------------------------------------------


def _mark_of(position_id: str, chain: str, token: str) -> dict[str, Any] | None:
    row = _one("SELECT value, updated_ms FROM kv WHERE key = ?", (f"watchdog.quote:{position_id}:{chain}:{token}",))
    if not row:
        return None
    price = (_jl(row["value"], {}) or {}).get("price") or {}
    px = _dec(price.get("price_usd"))
    if px is None:
        return None
    observed = price.get("observed_ms")
    return {"price_usd": px, "source": price.get("source"), "settlement": price.get("settlement"),
            "observed_ms": int(observed) if observed else None, "updated_ms": int(row["updated_ms"] or 0)}


def book(limit: int = 60) -> dict[str, Any]:
    """Open positions marked with the price protection decided on this tick, and today's closes."""

    def build() -> dict[str, Any]:
        rows = _q("SELECT position_id, chain, token, lane, mode, opened_ms, CAST(qty AS TEXT) AS qty, "
                  "CAST(cost_native AS TEXT) AS cost, entry_price_usd, peak_price_usd, stop_price_usd, tp_done_json "
                  "FROM positions WHERE closed_ms IS NULL ORDER BY opened_ms DESC LIMIT ?", (limit,))
        names = _symbol_map({(r["chain"], r["token"]) for r in rows})
        out = []
        for r in rows:
            mark = _mark_of(r["position_id"], r["chain"], r["token"])
            entry = _dec(r["entry_price_usd"])
            ret = float(mark["price_usd"] / entry - 1) if mark and entry else None
            cost = native_amount(r["cost"], r["chain"])
            state = _one("SELECT exit_state, exit_reason, exit_attempts, pending_pct FROM watchdog_state WHERE position_id=?",
                         (r["position_id"],)) or {}
            meta = names.get((r["chain"], r["token"]), {})
            out.append({
                **r, "symbol": meta.get("symbol"), "name": meta.get("name"),
                "mark": mark, "ret": ret, "cost_native": cost, "native": NATIVE.get(r["chain"], ""),
                "value_native": cost * (1 + ret) if cost is not None and ret is not None else None,
                "tp_done": _jl(r["tp_done_json"], []), "exit_state": state.get("exit_state"),
                "exit_reason": state.get("exit_reason"), "exit_attempts": state.get("exit_attempts"),
                "paper": r["mode"] not in ("live", "canary"),
            })
        closed = _q("SELECT position_id, chain, token, lane, mode, opened_ms, closed_ms, CAST(cost_native AS TEXT) AS cost, "
                    "CAST(realized_native AS TEXT) AS realized, exit_reason FROM positions WHERE closed_ms >= ? "
                    "ORDER BY closed_ms DESC LIMIT 40", (now_ms() - DAY_MS,))
        cnames = _symbol_map({(r["chain"], r["token"]) for r in closed})
        recent = []
        for r in closed:
            cost, realized = _dec(r["cost"]), _dec(r["realized"])
            recent.append({**r, "symbol": cnames.get((r["chain"], r["token"]), {}).get("symbol"),
                           "ret": float(realized / cost) if cost and realized is not None else None,
                           "hold_s": (int(r["closed_ms"]) - int(r["opened_ms"])) / 1000,
                           "paper": r["mode"] not in ("live", "canary")})
        return {"open": out, "recent": recent, "at_ms": now_ms()}

    return _cached("book", 3.0, build)


# --------------------------------------------------------------------------------------
# the alpha stream
# --------------------------------------------------------------------------------------

STREAM_KINDS = {
    "confluence": "Tracked S/A/B wallets bought together",
    "launch": "Launch promoted by triage",
    "graduation": "Graduated to a pool",
    "signal": "Lane signal",
    "entry": "Agent entered",
    "trending": "Trending / smart-money feed",
    "listing": "Exchange listing",
    "claim": "Claim / airdrop domain",
    "governance": "Governance / repo",
}


def alpha_stream(limit: int = 80, *, kinds: tuple[str, ...] | None = None, chain: str | None = None) -> list[dict[str, Any]]:
    """Every early signal the agent collects, newest first, one shape. Cached 4 s."""
    key = f"stream:{','.join(sorted(kinds or ()))}:{chain}:{limit}"
    return _cached(key, 4.0, lambda: _build_stream(limit, kinds, chain))


def _want(kind: str, kinds: tuple[str, ...] | None) -> bool:
    return not kinds or kind in kinds


def _build_stream(limit: int, kinds: tuple[str, ...] | None, chain: str | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    per = max(20, min(120, limit))
    if _want("launch", kinds):
        for r in _q("SELECT ts_ms, chain, token, verdict, score, launchpad, reasons_json FROM triage_decisions "
                    "WHERE verdict = 'promote' ORDER BY ts_ms DESC LIMIT ?", (per,)):
            reasons = _jl(r["reasons_json"], [])
            items.append({"ts": int(r["ts_ms"]), "kind": "launch", "chain": r["chain"], "token": r["token"],
                          "title": f"{r['launchpad'] or 'launch'} promoted", "score": _f(r["score"]),
                          "detail": ", ".join(str(x) for x in reasons[:3]) if isinstance(reasons, list) else ""})
    if _want("graduation", kinds):
        for r in _q("SELECT ts_ms, chain, subject, payload FROM events WHERE kind = 'token.migrated' "
                    "ORDER BY id DESC LIMIT ?", (per,)):
            p = _jl(r["payload"], {}) or {}
            token = r["subject"] or p.get("mint") or p.get("token")
            if token:
                items.append({"ts": int(r["ts_ms"]), "kind": "graduation", "chain": r["chain"], "token": token,
                              "title": "graduated", "detail": str(p.get("pool") or p.get("venue") or "")[:60]})
    if _want("signal", kinds):
        for r in _q("SELECT created_ms, lane, chain, token, strength, reasons_json FROM signals "
                    "ORDER BY created_ms DESC LIMIT ?", (per,)):
            reasons = _jl(r["reasons_json"], [])
            items.append({"ts": int(r["created_ms"]), "kind": "signal", "chain": r["chain"], "token": r["token"],
                          "title": r["lane"], "score": _f(r["strength"]),
                          "detail": ", ".join(str(x) for x in reasons[:3]) if isinstance(reasons, list) else ""})
    if _want("entry", kinds):
        for r in _q("SELECT ts_ms, lane, mode, chain, token, confidence, thesis FROM decisions "
                    "WHERE ts_ms > ? AND action = 'enter' ORDER BY ts_ms DESC LIMIT ?", (now_ms() - 3 * DAY_MS, per)):
            items.append({"ts": int(r["ts_ms"]), "kind": "entry", "chain": r["chain"], "token": r["token"],
                          "title": f"{r['lane']} · {r['mode']}", "score": _f(r["confidence"]),
                          "detail": str(r["thesis"] or "")[:120]})
    if _want("trending", kinds):
        for r in _q("SELECT feed, chain, token_address, first_seen_ms, symbol, narrative, first_rank "
                    "FROM alpha_feed_first_seen ORDER BY first_seen_ms DESC LIMIT ?", (per,)):
            items.append({"ts": int(r["first_seen_ms"]), "kind": "trending", "chain": r["chain"],
                          "token": r["token_address"], "symbol": r["symbol"], "title": r["feed"],
                          "detail": str(r["narrative"] or (f"rank {r['first_rank']}" if r["first_rank"] else ""))[:160],
                          "rank": r["first_rank"]})
    if _want("confluence", kinds):
        # kaiba/ingest/wallet_track.py (2026-10-28): its own indexed read (idx_alpha_signals_source),
        # so a busy listing/claim source can never push a wallet confluence off the stream.
        for r in _q("SELECT subject, chain, first_seen_ms, payload_json FROM alpha_signals WHERE source = ? "
                    "ORDER BY first_seen_ms DESC LIMIT ?", ("kaiba:wallet_track", per)):
            p = _jl(r["payload_json"], {}) or {}
            tiers = " ".join(f"{t}×{n}" for t, n in (p.get("tiers") or {}).items() if n)
            usd = _f(p.get("usd"))
            sold = sum(1 for w in p.get("wallets") or [] if isinstance(w, dict) and w.get("sold"))
            items.append({"ts": int(r["first_seen_ms"]), "kind": "confluence", "chain": r["chain"], "token": r["subject"],
                          "symbol": p.get("symbol"), "title": f"{p.get('level')} tracked wallets",
                          "detail": " · ".join(x for x in (tiers, f"${usd:,.0f} bought" if usd else "",
                                                           f"{sold} already sold" if sold else "") if x),
                          "score": _f(p.get("level"))})
    kind_of = {"venue_listing": "listing", "cert_subdomain": "claim", "gov_space": "governance", "repo_commit": "governance"}
    for r in _q("SELECT source, kind, subject, title, url, chain, first_seen_ms, confidence, lead_ms "
                "FROM alpha_signals ORDER BY first_seen_ms DESC LIMIT ?", (per,)):
        if r["kind"] == "wallet_confluence":
            continue
        kind = kind_of.get(r["kind"], "governance")
        if not _want(kind, kinds):
            continue
        items.append({"ts": int(r["first_seen_ms"]), "kind": kind, "chain": r["chain"] if r["chain"] != "None" else None,
                      "token": None, "title": f"{r['source']}: {r['title'] or r['subject']}"[:140],
                      "subject": r["subject"], "url": safe_url(r["url"]), "score": _f(r["confidence"])})
    if chain:
        items = [i for i in items if i.get("chain") == chain]
    items.sort(key=lambda i: i["ts"], reverse=True)
    items = items[:limit]
    names = _symbol_map({(i["chain"], i["token"]) for i in items if i.get("token") and i.get("chain") in CHAINS})
    for i in items:
        for k in ("score", "detail", "url", "symbol", "subject", "token", "chain"):
            i.setdefault(k, None)
        meta = names.get((i.get("chain"), i.get("token")), {})
        i["symbol"] = i.get("symbol") or meta.get("symbol")
        i["linkable"] = bool(i.get("token") and valid_chain(i.get("chain")) and clean_address(i["chain"], i["token"]))
    return items


#: Stream kinds in the order a watchlist row names them (strongest evidence first).
KIND_RANK = ("confluence", "entry", "signal", "graduation", "launch", "listing", "claim", "governance", "trending")


def alpha_watch(limit: int = 60, *, kinds: tuple[str, ...] | None = None, chain: str | None = None) -> list[dict[str, Any]]:
    """The stream folded to one row per token, the way a watchlist reads.

    A token that trended on four feeds and drew a lane signal is ONE row saying so ("4 events:
    signal, trending"), not five identical lines -- the old stream was a wall of repeats. Each
    token row carries the dossier's market numbers (price, market cap, liquidity, grade) with
    the age of the dossier, so a stale number is visible as stale. Cached 5 s."""
    key = f"watch:{','.join(sorted(kinds or ()))}:{chain}:{limit}"
    return _cached(key, 5.0, lambda: _build_watch(limit, kinds, chain))


def _build_watch(limit: int, kinds: tuple[str, ...] | None, chain: str | None) -> list[dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for item in alpha_stream(400, kinds=kinds, chain=chain):
        key = (item["chain"] or "", item["token"]) if item["linkable"] else ("~", f"{item['kind']}:{item['title']}")
        row = rows.get(key)
        if row is None:
            row = {**item, "n": 0, "kinds": [], "first_ts": item["ts"], "events": []}
            rows[key] = row
            order.append(key)
        row["n"] += 1
        row["first_ts"] = min(row["first_ts"], item["ts"])
        if item["kind"] not in row["kinds"]:
            row["kinds"].append(item["kind"])
        if len(row["events"]) < 6:
            row["events"].append({"ts": item["ts"], "kind": item["kind"], "title": item["title"],
                                  "detail": item.get("detail")})
        if row.get("score") is None and item.get("score") is not None:
            row["score"] = item["score"]
        row["symbol"] = row.get("symbol") or item.get("symbol")
    folded = [rows[k] for k in order]
    if not kinds:
        # "All" is the agent's own alpha first. The trending feeds print several times a minute
        # (MEASURED 2026-10-20: 57 of the newest 60 tokens were trending-only), so they get at
        # most a third of the rows unless nothing else is there to show.
        other = [r for r in folded if r["kinds"] != ["trending"]][:limit]
        trend = [r for r in folded if r["kinds"] == ["trending"]][:max(limit // 3, limit - len(other))]
        keep = {id(r) for r in other[:limit - len(trend)] + trend}
        folded = [r for r in folded if id(r) in keep]
    out = folded[:limit]
    for row in out:
        row["kinds"].sort(key=lambda k: KIND_RANK.index(k) if k in KIND_RANK else len(KIND_RANK))
        row["lead"] = row["kinds"][0]
        row["market"] = None
    by_chain: dict[str, list[dict[str, Any]]] = {}
    for row in out:
        if row["linkable"]:
            by_chain.setdefault(row["chain"], []).append(row)
    for c, group in by_chain.items():
        addresses = [r["token"] for r in group]
        marks = ",".join("?" for _ in addresses)
        found = _q(
            "SELECT address, grade, score, built_at_ms, "
            "COALESCE(json_extract(dossier_json, '$.price_usd.value'), json_extract(dossier_json, '$.price_usd')) AS px, "
            "COALESCE(json_extract(dossier_json, '$.market_cap_usd.value'), json_extract(dossier_json, '$.market_cap_usd')) AS mcap, "
            "COALESCE(json_extract(dossier_json, '$.liquidity_usd.value'), json_extract(dossier_json, '$.liquidity_usd')) AS liq "
            f"FROM token_dossiers WHERE chain = ? AND address IN ({marks})",
            (c, *addresses),
        )
        got = {r["address"]: r for r in found}
        for row in group:
            d = got.get(row["token"])
            if d:
                row["market"] = {"price_usd": _f(d["px"]), "mcap_usd": _f(d["mcap"]), "liq_usd": _f(d["liq"]),
                                 "grade": d["grade"] if d["grade"] not in (None, "UNSCORED") else None,
                                 "score": _f(d["score"]), "built_at_ms": int(d["built_at_ms"] or 0)}
    return out


# --------------------------------------------------------------------------------------
# what the agent is doing: entries, exits, what it passed on and why, its own notes
# --------------------------------------------------------------------------------------

ACTIVITY_WINDOW_MS = 6 * 3600_000
SKIP_BUCKET_MS = 30 * 60_000
_NUMERIC = re.compile(r"^[\d.<>/_+-]+$")


def _blocker_head(raw: Any) -> str:
    """``size_not_positive:score_below_ladder:61.6`` -> ``score below ladder``: the reason, not the number."""
    parts = [p for p in str(raw or "").split(":") if p and not _NUMERIC.match(p)]
    if len(parts) >= 2 and parts[0] in ("size_not_positive", "risk", "gate"):
        parts = parts[1:]
    return (parts[0] if parts else str(raw or "unknown")).replace("_", " ")[:40]


def short_addr(address: Any) -> str:
    text = str(address or "")
    return text if len(text) <= 9 else f"{text[:4]}…{text[-4:]}"


def activity(limit: int = 40) -> dict[str, Any]:
    """The agent's last six hours as one feed: what it bought, what it sold and how that went,
    what it looked at and passed on (grouped per half hour, with the reasons), and the notes the
    operator agent wrote to the journal. Indexed reads only (``idx_decisions_ts``, the journal's
    rowid, ``closed_ms`` over a bounded window). Cached 5 s."""

    def build() -> dict[str, Any]:
        now = now_ms()
        since = now - ACTIVITY_WINDOW_MS
        feed: list[dict[str, Any]] = []
        enters = _q("SELECT ts_ms, lane, mode, chain, token, confidence, size_base_units FROM decisions "
                    "WHERE ts_ms > ? AND action = 'enter' ORDER BY ts_ms DESC LIMIT 40", (since,))
        closes = _q("SELECT position_id, chain, token, lane, mode, opened_ms, closed_ms, CAST(cost_native AS TEXT) AS cost, "
                    "CAST(realized_native AS TEXT) AS realized, exit_reason FROM positions "
                    "WHERE closed_ms >= ? ORDER BY closed_ms DESC LIMIT 40", (since,))
        # 2026-10-23 (step 3 follow-up): the sold count / win count / mean mixed live with paper and
        # counted every paper close. Paper closes are now judged by the scoreboard's own rule
        # (:func:`_paper_rule`): the summary counts only those it counts, each left-out close says why
        # in its row, and live closes are summarised apart (``counts_live``).
        flt = _paper_rule() if any(r["mode"] not in ("live", "canary") for r in closes) else None
        names = _symbol_map({(r["chain"], r["token"]) for r in enters + closes})
        for r in enters:
            sym = names.get((r["chain"], r["token"]), {}).get("symbol")
            size = native_amount(r["size_base_units"], r["chain"])
            live = r["mode"] in ("live", "canary")
            feed.append({"ts": int(r["ts_ms"]), "type": "buy", "chain": r["chain"], "token": r["token"], "symbol": sym,
                         "title": f"Bought {sym or short_addr(r['token'])}", "live": live,
                         "sub": f"{r['lane']} · {'LIVE' if live else 'paper'}"
                                + (f" · {size:.3f} {NATIVE.get(r['chain'], '')}" if size else "")})
        for r in closes:
            sym = names.get((r["chain"], r["token"]), {}).get("symbol")
            cost, realized = _dec(r["cost"]), _dec(r["realized"])
            ret = float(realized / cost) if cost and realized is not None else None
            live = r["mode"] in ("live", "canary")
            why = None if live else _paper_not_counted(flt, r)
            excluded = trading.EXCLUSION_LABELS.get(why, why) if why is not None else None
            held = (int(r["closed_ms"]) - int(r["opened_ms"])) / 1000 if r["opened_ms"] else None
            sub = f"{r['lane']} · {str(r['exit_reason'] or 'exit').replace('_', ' ')}"
            if excluded:
                sub += f" · not counted ({excluded})"
            feed.append({"ts": int(r["closed_ms"]), "type": "sell", "chain": r["chain"], "token": r["token"],
                         "symbol": sym, "title": f"Sold {sym or short_addr(r['token'])}", "ret": ret, "live": live,
                         "counted": live or why is None, "excluded": excluded, "held_s": held, "sub": sub})
        skips = _q("SELECT ts_ms, blockers_json FROM decisions WHERE ts_ms > ? AND action = 'skip' "
                   "ORDER BY ts_ms DESC LIMIT 1500", (since,))
        buckets: dict[int, dict[str, Any]] = {}
        for r in skips:
            b = buckets.setdefault(int(r["ts_ms"]) // SKIP_BUCKET_MS, {"n": 0, "ts": int(r["ts_ms"]), "why": {}})
            b["n"] += 1
            b["ts"] = max(b["ts"], int(r["ts_ms"]))
            blockers = _jl(r["blockers_json"], []) or ["unknown"]
            head = _blocker_head(blockers[0] if isinstance(blockers, list) and blockers else blockers)
            b["why"][head] = b["why"].get(head, 0) + 1
        for b in buckets.values():
            top = sorted(b["why"].items(), key=lambda kv: -kv[1])[:3]
            feed.append({"ts": b["ts"], "type": "pass", "title": f"Passed on {b['n']}",
                         "sub": " · ".join(f"{why} ×{n}" for why, n in top), "n": b["n"]})
        for j in _q("SELECT ts_ms, kind, subject, body FROM journal ORDER BY seq DESC LIMIT 12"):
            if int(j["ts_ms"] or 0) < since:
                continue
            body = str(j["body"] or "").strip()
            first = body.splitlines()[0] if body else ""
            feed.append({"ts": int(j["ts_ms"]), "type": "note", "title": first[:90] or str(j["kind"]),
                         "sub": str(j["kind"]) + (f" · {j['subject']}" if j["subject"] else ""), "body": body[:1200]})
        feed.sort(key=lambda e: e["ts"], reverse=True)
        all_sells = [e for e in feed if e["type"] == "sell"]
        # Paper: only the closes the scoreboard counts, each capped at its +300% as the scoreboard does.
        paper = [min(e["ret"], trading.OUTCOME_CAP) for e in all_sells
                 if not e["live"] and e["counted"] and e.get("ret") is not None]
        live_rets = [e["ret"] for e in all_sells if e["live"] and e.get("ret") is not None]
        not_counted: dict[str, int] = {}
        for e in all_sells:
            if not e["live"] and not e["counted"]:
                not_counted[str(e["excluded"])] = not_counted.get(str(e["excluded"]), 0) + 1
        return {"feed": feed[:limit], "at_ms": now, "window_h": ACTIVITY_WINDOW_MS // 3600_000,
                "counts": {"buys": len(enters), "passes": len(skips), "sells": len(paper),
                           "wins": sum(1 for r in paper if r > 0),
                           "mean": (sum(paper) / len(paper)) if paper else None},
                "counts_live": {"sells": len(live_rets), "wins": sum(1 for r in live_rets if r > 0),
                                "mean": (sum(live_rets) / len(live_rets)) if live_rets else None},
                "not_counted": sum(not_counted.values()), "not_counted_by": not_counted}

    return _cached(f"activity:{limit}", 5.0, build)


def source_health() -> list[dict[str, Any]]:
    return _cached("health", 60.0, _build_source_health)


def _build_source_health() -> list[dict[str, Any]]:
    rows = _q("SELECT source, kind, interval_s, last_poll_ms, last_ok_ms, last_count, last_new, total_new, fail_streak, "
              "last_error FROM alpha_source_health ORDER BY source")
    # INDEXED BY: left alone the planner walks the whole primary-key index for the GROUP BY --
    # MEASURED 9.4 s on the box 2026-10-20, against 0.04 s on the time index.
    feeds = _q("SELECT feed, chain, COUNT(*) AS n, MAX(first_seen_ms) AS last_ms FROM alpha_feed_first_seen "
               "INDEXED BY idx_alpha_feed_first_seen_ts WHERE first_seen_ms > ? GROUP BY feed, chain "
               "ORDER BY last_ms DESC", (now_ms() - DAY_MS,))
    for r in rows:
        r["last_error"] = str(r.get("last_error") or "")[:160]
    return [{**r, "row": "watcher"} for r in rows] + [{**f, "row": "feed"} for f in feeds]


# --------------------------------------------------------------------------------------
# a token
# --------------------------------------------------------------------------------------

DOSSIER_FIELDS = (
    ("price_usd", "Price", "px"), ("market_cap_usd", "Market cap", "usd"), ("liquidity_usd", "Liquidity", "usd"),
    ("volume_24h_usd", "Volume 24h", "usd"), ("holder_count", "Holders", "int"), ("top10_pct", "Top 10", "pct"),
    ("dev_pct", "Dev holds", "pct"), ("sniper_pct", "Snipers", "pct"), ("bundler_pct", "Bundlers", "pct"),
    ("insider_pct", "Insiders", "pct"), ("cluster_pct", "Clusters", "pct"), ("buy_tax_bps", "Buy tax", "bps"),
    ("sell_tax_bps", "Sell tax", "bps"), ("lp_burned_pct", "LP burned", "pct"),
)


def _field(dossier: dict[str, Any], key: str) -> dict[str, Any]:
    raw = dossier.get(key)
    if isinstance(raw, dict):
        receipt = raw.get("receipt") or {}
        return {"value": _dec(raw.get("value")), "basis": raw.get("basis"),
                "provider": receipt.get("provider") if isinstance(receipt, dict) else None,
                "observed_ms": receipt.get("observed_at_ms") if isinstance(receipt, dict) else None}
    return {"value": _dec(raw) if raw is not None and not isinstance(raw, bool) else None, "basis": None,
            "provider": None, "observed_ms": None}


def find_token_chain(address: str) -> str | None:
    """The chain a token address lives on, from our own records."""
    if SOL_ADDR.match(address):
        return "sol"
    if EVM_ADDR.match(address):
        low = address.lower()
        for chain in EVM_CHAINS:
            if _one("SELECT 1 AS x FROM tokens WHERE chain=? AND address=?", (chain, low)):
                return chain
    return None


def token_profile(chain: str, address: str) -> dict[str, Any]:
    key = f"token:{chain}:{address}"
    return _cached(key, 5.0, lambda: _build_token(chain, address))


def _build_token(chain: str, address: str) -> dict[str, Any]:
    tok = _one("SELECT symbol, name, decimals, creator, created_ms, launchpad, pool, migrated_ms, first_seen_ms "
               "FROM tokens WHERE chain=? AND address=?", (chain, address)) or {}
    out: dict[str, Any] = {"chain": chain, "address": address, "native": NATIVE.get(chain, ""),
                           "token": tok, "known": bool(tok), "links": explorer_links(chain, address)}
    drow = _one("SELECT built_at_ms, score, grade, dossier_json FROM token_dossiers WHERE chain=? AND address=?",
                (chain, address))
    if drow:
        d = _jl(drow["dossier_json"], {}) or {}
        out["dossier"] = {
            "built_at_ms": int(drow["built_at_ms"] or 0), "score": _f(drow["score"]), "grade": drow["grade"],
            "fields": [{"key": k, "label": label, "fmt": fmt, **_field(d, k)} for k, label, fmt in DOSSIER_FIELDS],
            "blockers": d.get("blockers") or [], "warnings": d.get("warnings") or [], "unknowns": d.get("unknowns") or [],
            "mint_revoked": d.get("mint_authority_revoked"), "freeze_revoked": d.get("freeze_authority_revoked"),
            "can_sell": d.get("can_sell"), "entity_count": d.get("entity_count"),
            "graded_wallets": len(d.get("graded_wallets") or []),
        }
        out["known"] = True
    out["triage"] = _q("SELECT ts_ms, verdict, score, reasons_json, launchpad FROM triage_decisions "
                       "WHERE chain=? AND token=? ORDER BY ts_ms DESC LIMIT 6", (chain, address))
    for t in out["triage"]:
        t["reasons"] = _jl(t.pop("reasons_json"), [])
    creator = tok.get("creator")
    if creator:
        out["creator"] = {
            "address": creator,
            "record": _one("SELECT launches, graduated, rugged, median_peak_mcap_usd, score FROM creators "
                           "WHERE chain=? AND address=?", (chain, creator)),
            "deployer": _one("SELECT launches, scored, runners, best_multiple FROM deployer_stats WHERE chain=? AND wallet=?",
                             (chain, creator)),
        }
    out["bundles"] = _one("SELECT computed_ms, coverage, bundled_pct, sniped_pct, bundle_groups, bundle_entities, "
                          "sniper_entities, launch_buys FROM token_bundles WHERE chain=? AND token=?", (chain, address))
    curve = _one("SELECT observed_ms, real_sol_lamports, virtual_sol_lamports, progress_pct, source FROM curve_snapshots "
                 "WHERE chain=? AND token=? ORDER BY observed_ms DESC LIMIT 1", (chain, address))
    if curve:
        curve["real_sol"] = native_amount(curve.get("real_sol_lamports"), chain)
        curve["progress"] = _f(curve.get("progress_pct"))
    out["curve"] = curve
    out["x"] = _one("SELECT observed_ms, n_posts, n_authors, max_followers, sum_likes, sum_views, posts_1h "
                    "FROM x_token_obs WHERE chain=? AND token=? ORDER BY observed_ms DESC LIMIT 1", (chain, address))
    out["tweet"] = _one("SELECT kind, author, tweet_id, fetched_ms, via FROM tweet_sources WHERE chain=? AND token=?",
                        (chain, address))
    out["feeds"] = _q("SELECT feed, first_seen_ms, first_rank, narrative FROM alpha_feed_first_seen "
                      "WHERE chain=? AND token_address=? ORDER BY first_seen_ms LIMIT 12", (chain, address))
    out["signals"] = _q("SELECT created_ms, lane, strength, reasons_json FROM signals WHERE chain=? AND token=? "
                        "ORDER BY created_ms DESC LIMIT 10", (chain, address))
    for s in out["signals"]:
        s["reasons"] = _jl(s.pop("reasons_json"), [])
    positions = _q("SELECT position_id, lane, mode, opened_ms, closed_ms, CAST(cost_native AS TEXT) AS cost, "
                   "CAST(realized_native AS TEXT) AS realized, entry_price_usd, exit_reason FROM positions "
                   "WHERE chain=? AND token=? ORDER BY opened_ms DESC LIMIT 10", (chain, address))
    for p in positions:
        cost, realized = _dec(p["cost"]), _dec(p["realized"])
        p["ret"] = float(realized / cost) if p["closed_ms"] and cost and realized is not None else None
        p["mark"] = _mark_of(p["position_id"], chain, address) if not p["closed_ms"] else None
        entry = _dec(p["entry_price_usd"])
        if p["mark"] and entry:
            p["ret"] = float(p["mark"]["price_usd"] / entry - 1)
        p["paper"] = p["mode"] not in ("live", "canary")
    out["positions"] = positions
    out["series"] = price_series(chain, address, positions)
    out["quote"] = _newest_quote(out)
    out["gmgn_chart"] = gmgn_chart_url(chain, address, tok.get("created_ms"))
    return out


#: GMGN's public price-chart embed (docs.gmgn.ai, "integrate GMGN price chart"): no key, no
#: registration, ``theme`` and ``interval`` parameters. Robinhood Chain is not a GMGN chain.
GMGN_CHART_CHAINS = {"sol": "sol", "bsc": "bsc"}


def gmgn_chart_url(chain: str, address: str, created_ms: Any = None) -> str | None:
    """The GMGN candle chart for a token, at a bar size that suits its age: a token minutes old
    has no 15-minute bars yet, a week-old one is unreadable at 1 minute."""
    slug = GMGN_CHART_CHAINS.get(chain)
    if slug is None or clean_address(chain, address) is None:
        return None
    try:
        age_ms = now_ms() - int(created_ms) if created_ms else None
    except (TypeError, ValueError):
        age_ms = None
    interval = "15" if age_ms is None else "1" if age_ms < 6 * 3600_000 else "15" if age_ms < 3 * DAY_MS else "60"
    return f"https://www.gmgn.cc/kline/{slug}/{address}?theme=dark&interval={interval}"


def _newest_quote(tok: dict[str, Any]) -> dict[str, Any] | None:
    """The price the header shows: the NEWEST one we hold, with where it came from. A dossier is
    minutes old by the time anyone looks; showing it beside a fresher chart put two prices 2x
    apart on one screen (MEASURED 2026-10-20, SNAILMAIL: dossier $0.0000469 vs protection's
    $0.0000209)."""
    found: list[dict[str, Any]] = []
    for p in tok.get("positions") or []:
        mark = p.get("mark")
        if mark and mark.get("price_usd"):
            found.append({"price": mark["price_usd"], "unit": "USD", "ts": mark.get("observed_ms") or mark.get("updated_ms"),
                          "source": "protection mark"})
    series = tok.get("series") or {}
    if series.get("points") and series.get("unit") == "USD":
        ts, px = series["points"][-1]
        found.append({"price": px, "unit": "USD", "ts": ts, "source": series.get("source")})
    d = tok.get("dossier") or {}
    for f in d.get("fields") or []:
        if f["key"] == "price_usd" and f.get("value") is not None:
            found.append({"price": f["value"], "unit": "USD", "ts": f.get("observed_ms") or d.get("built_at_ms"),
                          "source": "dossier" + (f" · {f['provider']}" if f.get("provider") else "")})
    found = [q for q in found if q["ts"]]
    return max(found, key=lambda q: int(q["ts"])) if found else None


def _downsample(points: list[list[float]]) -> list[list[float]]:
    if len(points) <= MAX_SERIES_POINTS:
        return points
    step = len(points) / MAX_SERIES_POINTS
    picked = [points[int(i * step)] for i in range(MAX_SERIES_POINTS - 1)]
    return picked + [points[-1]]


def price_series(chain: str, address: str, positions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """One honest price line: protection's marks if we held it, else the most-printed swap feed
    (``swaps`` mixes feeds that disagree, so one source only), else the curve in native units."""
    for p in (positions or [])[:5]:
        marks = _q("SELECT ts_ms, price_usd FROM position_marks WHERE position_id=? ORDER BY ts_ms DESC LIMIT 3000",
                   (p["position_id"],))
        pts = [[int(m["ts_ms"]), float(m["price_usd"])] for m in reversed(marks) if _f(m["price_usd"])]
        if len(pts) >= 3:
            return {"unit": "USD", "source": "protection marks", "points": _downsample(pts)}
    rows = _q("SELECT ts_ms, price_usd, source FROM swaps WHERE chain=? AND token=? AND ts_ms > ? "
              "ORDER BY ts_ms DESC LIMIT 6000", (chain, address, now_ms() - 14 * DAY_MS))
    by_source: dict[str, list[list[float]]] = {}
    for r in reversed(rows):
        px = _f(r["price_usd"])
        if px and px > 0:
            by_source.setdefault(str(r["source"] or "?"), []).append([int(r["ts_ms"]), px])
    if by_source:
        source, pts = max(by_source.items(), key=lambda kv: len(kv[1]))
        if len(pts) >= 2:
            return {"unit": "USD", "source": f"tracked-wallet swaps ({source})", "points": _downsample(pts)}
    snaps = _q("SELECT observed_ms, virtual_sol_lamports, virtual_token_atoms FROM curve_snapshots "
               "WHERE chain=? AND token=? ORDER BY observed_ms DESC LIMIT 2000", (chain, address))
    pts = []
    for s in reversed(snaps):
        vs, vt = _dec(s["virtual_sol_lamports"]), _dec(s["virtual_token_atoms"])
        if vs and vt:
            pts.append([int(s["observed_ms"]), float((vs / Decimal(10**9)) / (vt / Decimal(10**6)))])
    if len(pts) >= 2:
        return {"unit": NATIVE.get(chain, "native"), "source": "bonding-curve snapshots", "points": _downsample(pts)}
    return {"unit": None, "source": None, "points": []}


def launches(limit: int = 60, chain: str | None = None, window_ms: int = 6 * 3600_000) -> list[dict[str, Any]]:
    """The newest tokens we saw created, with the dossier grade and triage verdict if any."""

    def build() -> list[dict[str, Any]]:
        rows = _q("SELECT chain, address, symbol, name, launchpad, created_ms, migrated_ms FROM tokens "
                  "WHERE created_ms > ? ORDER BY created_ms DESC LIMIT ?", (now_ms() - window_ms, limit * 4))
        out = []
        for r in rows:
            if chain and r["chain"] != chain:
                continue
            d = _one("SELECT score, grade FROM token_dossiers WHERE chain=? AND address=?", (r["chain"], r["address"]))
            t = _one("SELECT verdict, score FROM triage_decisions WHERE chain=? AND token=? ORDER BY ts_ms DESC LIMIT 1",
                     (r["chain"], r["address"]))
            out.append({**r, "grade": d["grade"] if d else None, "dscore": _f(d["score"]) if d else None,
                        "verdict": t["verdict"] if t else None})
            if len(out) >= limit:
                break
        return out

    return _cached(f"launches:{chain}:{limit}", 10.0, build)


# --------------------------------------------------------------------------------------
# a wallet
# --------------------------------------------------------------------------------------


def find_wallet_chain(address: str) -> str | None:
    if SOL_ADDR.match(address):
        return "sol"
    if EVM_ADDR.match(address):
        low = address.lower()
        for chain in EVM_CHAINS:
            if _one("SELECT 1 AS x FROM wallet_scores WHERE chain=? AND address=?", (chain, low)) or \
                    _one("SELECT 1 AS x FROM wallets WHERE chain=? AND address=?", (chain, low)):
                return chain
    return None


def wallet_profile(chain: str, address: str) -> dict[str, Any]:
    return _cached(f"wallet:{chain}:{address}", 10.0, lambda: _build_wallet(chain, address))


def _build_wallet(chain: str, address: str) -> dict[str, Any]:
    w = _one("SELECT name, source, tags_json, first_seen_ms, last_seen_ms, first_funder, first_funded_ms, twitter, cohort "
             "FROM wallets WHERE chain=? AND address=?", (chain, address)) or {}
    s = _one("SELECT score, grade, evidence_weight, archetype, realized_pnl_usd, win_rate, closed_trades, distinct_tokens, "
             "median_hold_s, factors_json, penalties_json, blockers_json, model_version, scored_at_ms FROM wallet_scores "
             "WHERE chain=? AND address=?", (chain, address))
    out: dict[str, Any] = {"chain": chain, "address": address, "native": NATIVE.get(chain, ""), "wallet": w,
                           "links": explorer_links(chain, address, kind="wallet"), "known": bool(w or s)}
    if w:
        w["tags"] = _jl(w.pop("tags_json", None), [])
    if s:
        s["factors"] = [f for f in (_jl(s.pop("factors_json"), []) or []) if isinstance(f, dict)]
        s["penalties"] = _jl(s.pop("penalties_json"), []) or []
        s["blockers"] = _jl(s.pop("blockers_json"), []) or []
        for k in ("score", "evidence_weight", "realized_pnl_usd", "win_rate", "median_hold_s"):
            s[k] = _f(s.get(k))
        for f in s["factors"]:
            f["share"] = (float(f.get("points") or 0) / float(f.get("max_points") or 1)) if f.get("max_points") else None
    out["score"] = s
    out["tags"] = _q("SELECT tag, source, first_ms, last_ms, n, wallet_name FROM wallet_feed_tags "
                     "WHERE chain=? AND address=? LIMIT 20", (chain, address))
    member = _one("SELECT entity_id FROM entity_members WHERE chain=? AND address=?", (chain, address))
    if member:
        size = _one("SELECT COUNT(*) AS n FROM (SELECT 1 FROM entity_members WHERE entity_id=? LIMIT 5001)",
                    (member["entity_id"],))
        out["entity"] = {"entity_id": member["entity_id"], "size": int(size["n"]) if size else None}
    out["deployer"] = _one("SELECT launches, scored, runners, best_multiple FROM deployer_stats WHERE chain=? AND wallet=?",
                           (chain, address))
    out["creator"] = _one("SELECT launches, graduated, rugged, median_peak_mcap_usd, score FROM creators "
                          "WHERE chain=? AND address=?", (chain, address))
    out["watch"] = _one("SELECT tier, status, reason, source, added_ms FROM tracker_watchlist WHERE chain=? AND address=?",
                        (chain, address))
    out["bundles"] = _q("SELECT token, role, buys FROM token_bundle_members WHERE chain=? AND address=? LIMIT 25",
                        (chain, address))
    hist = _q("SELECT scored_at_ms, score FROM wallet_score_history WHERE chain=? AND address=? "
              "ORDER BY scored_at_ms DESC LIMIT 400", (chain, address))
    out["history"] = {"unit": "score", "source": "wallet_score_history",
                      "points": [[int(h["scored_at_ms"]), float(h["score"])] for h in reversed(hist) if _f(h["score"]) is not None]}
    trades = _q("SELECT ts_ms, token, side, CAST(amount_native AS TEXT) AS amount_native, price_usd, usd_value, source "
                "FROM swaps WHERE chain=? AND wallet=? ORDER BY ts_ms DESC LIMIT 300", (chain, address))
    per: dict[str, dict[str, Any]] = {}
    for t in trades:
        t["native_amt"] = native_amount(t["amount_native"], chain)
        agg = per.setdefault(t["token"], {"token": t["token"], "buys": 0, "sells": 0, "in": 0.0, "out": 0.0,
                                          "first": t["ts_ms"], "last": t["ts_ms"]})
        side = str(t["side"] or "")
        amount = t["native_amt"] or 0.0
        if side == "buy":
            agg["buys"] += 1
            agg["in"] += amount
        elif side:
            agg["sells"] += 1
            agg["out"] += amount
        agg["first"] = min(agg["first"], t["ts_ms"])
        agg["last"] = max(agg["last"], t["ts_ms"])
    names = _symbol_map({(chain, t) for t in per})
    for agg in per.values():
        agg["symbol"] = names.get((chain, agg["token"]), {}).get("symbol")
        agg["net"] = agg["out"] - agg["in"] if agg["sells"] and agg["buys"] else None
    out["trades"] = trades[:60]
    for t in out["trades"]:
        t["symbol"] = names.get((chain, t["token"]), {}).get("symbol")
    out["tokens"] = sorted(per.values(), key=lambda a: a["last"], reverse=True)[:40]
    closed = [a for a in per.values() if a["net"] is not None]
    out["tape"] = {"swaps": len(trades), "tokens": len(per), "closed": len(closed),
                   "net": sum(a["net"] for a in closed) if closed else None,
                   "wins": sum(1 for a in closed if a["net"] > 0),
                   "window": "the last 300 swaps on our tape (14-day retention)"}
    return out


def top_wallets(chain: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
    """A and B grades, best first, through the (grade, score) index."""

    def build() -> list[dict[str, Any]]:
        from dashboard import wallet_export

        # Our own wallets never show here either: the owner's seeds grade well on robinhood, and a
        # row on this page is one click from a copy button (same rule as the GMGN lists).
        ban = wallet_export.excluded()
        if ban is None:
            return []
        rows = _q("SELECT s.chain, s.address, s.score, s.grade, s.archetype, s.win_rate, s.closed_trades, "
                  "s.realized_pnl_usd, s.scored_at_ms, w.cohort, w.source FROM wallet_scores s "
                  "LEFT JOIN wallets w ON w.chain = s.chain AND w.address = s.address "
                  "WHERE s.grade IN ('A', 'B') ORDER BY s.grade, s.score DESC LIMIT ?", (limit * 4,), seconds=15)
        out = [r for r in rows if (not chain or r["chain"] == chain) and not wallet_export.is_ours(r, ban)]
        for r in out:
            for k in ("score", "win_rate", "realized_pnl_usd"):
                r[k] = _f(r.get(k))
        return out[:limit]

    return _cached(f"topw:{chain}:{limit}", 300.0, build)


# --------------------------------------------------------------------------------------
# lanes: what trades on its own, how, and with what record
# --------------------------------------------------------------------------------------

MODE_ORDER = ["off", "shadow", "canary", "live"]


def lane_matrix() -> dict[str, Any]:
    def build() -> dict[str, Any]:
        cfg = data.risk_config()
        if cfg is None:
            return {"available": False, "lanes": []}
        from kaiba.core.schemas import Lane

        try:
            cap = int(((cfg.protection or {}).get("shadow_max_open_per_lane")) or 0) or None
        except (TypeError, ValueError):
            cap = None
        board = trading.paper_scoreboard()
        verdicts = {(r["lane"], r["chain"]): r for r in (board.get("rows") or [])}
        open_rows = _q("SELECT lane, mode, COUNT(*) AS n FROM positions WHERE closed_ms IS NULL GROUP BY lane, mode")
        held: dict[str, dict[str, int]] = {}
        for r in open_rows:
            held.setdefault(r["lane"], {})[r["mode"]] = int(r["n"])
        since = now_ms() - DAY_MS
        lanes = []
        for key, lane_cfg in (cfg.lanes or {}).items():
            name = str(getattr(key, "value", key))
            try:
                effective = cfg.effective_mode(Lane(name)).value
            except Exception:  # noqa: BLE001 - a lane the enum does not know yet
                effective = None
            mode = str(getattr(lane_cfg.mode, "value", lane_cfg.mode))
            activity = _q("SELECT action, COUNT(*) AS n FROM decisions WHERE lane=? AND mode=? AND ts_ms > ? GROUP BY action",
                          (name, mode, since))
            chains = [str(getattr(c, "value", c)) for c in (lane_cfg.chains or [])]
            lanes.append({
                "lane": name, "mode": mode, "effective": effective, "chains": chains,
                "size_min": lane_cfg.size_pct_min, "size_max": lane_cfg.size_pct_max,
                "open": held.get(name, {}), "cap": cap,
                "activity": {r["action"]: int(r["n"]) for r in activity},
                "verdicts": [verdicts[(name, c)] for c in chains if (name, c) in verdicts],
            })
        lanes.sort(key=lambda r: (MODE_ORDER.index(r["mode"]) if r["mode"] in MODE_ORDER else 0), reverse=True)
        bounds = getattr(cfg, "bounds", None)
        return {
            "available": True, "lanes": lanes,
            "global_mode": str(getattr(cfg.global_mode, "value", cfg.global_mode)),
            "max_lane_mode": str(getattr(getattr(bounds, "max_lane_mode", None), "value", "")) if bounds else None,
            "max_size_pct": getattr(bounds, "max_size_pct_bankroll", None) if bounds else None,
            "entries_paused": cfg.entries_paused, "reduce_only": cfg.reduce_only, "kill_switch": cfg.kill_switch,
            "honest_since_ms": board.get("honest_since_ms"),
        }

    return _cached("lanes", 10.0, build)


def manual_lane_mode() -> str | None:
    cfg = data.risk_config()
    if cfg is None:
        return None
    try:
        from kaiba.core.schemas import Lane

        return cfg.effective_mode(Lane.MANUAL).value
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------------------


def resolve(query: str) -> dict[str, Any]:
    """Where a search box entry should go: a token page, a wallet page, or a list of symbols."""
    q = str(query or "").strip()
    if not q:
        return {"kind": "empty"}
    if SOL_ADDR.match(q) or EVM_ADDR.match(q):
        chain = find_token_chain(q)
        address = clean_address(chain, q) if chain else None
        if chain and address and (_one("SELECT 1 AS x FROM tokens WHERE chain=? AND address=?", (chain, address))
                                  or _one("SELECT 1 AS x FROM token_dossiers WHERE chain=? AND address=?", (chain, address))):
            return {"kind": "token", "chain": chain, "address": address}
        wchain = find_wallet_chain(q)
        if wchain:
            return {"kind": "wallet", "chain": wchain, "address": clean_address(wchain, q)}
        if chain == "sol":
            return {"kind": "token", "chain": "sol", "address": q}
        return {"kind": "unknown", "query": q}
    sym = q.lstrip("$").upper()[:20]
    rows = _q("SELECT chain, address, symbol, name, launchpad, created_ms FROM tokens WHERE created_ms > ? "
              "ORDER BY created_ms DESC LIMIT 20000", (now_ms() - 2 * DAY_MS,), seconds=10)
    hits = [r for r in rows if str(r["symbol"] or "").upper() == sym][:30]
    return {"kind": "symbol", "query": sym, "hits": hits}
