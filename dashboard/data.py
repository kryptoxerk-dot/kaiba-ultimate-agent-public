"""Read-only views over the core, shaped for the dashboard and guarded everywhere.

Two rules drive the style in this module.

**Never fabricate a number.** A table that does not exist yet, a column a sibling module has
not started writing, or a price we have no quote source for all come back as ``None`` with
an ``unavailable`` reason attached, and the templates render "no data yet". Zero is a
measurement; ``None`` is the absence of one, and the operator has to be able to tell them
apart when the thing being reported is money.

**Never let a missing module break the page.** Phase 1 ships the dashboard before execution
and intelligence land, so every core import and every query is wrapped. A broken panel
degrades to a note; it does not take down the console the operator uses to hit the kill
switch.

Access to the database goes through ``db.get_conn`` looked up on the module at call time,
so the test fixture's monkeypatched connection is honoured.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

MAX_GRAPH_EDGES = 400
MAX_GRAPH_NODES = 250


# --------------------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------------------


def _conn() -> sqlite3.Connection | None:
    try:
        from kaiba.core import db

        return db.get_conn()
    except Exception as exc:  # pragma: no cover - only when the core itself is broken
        log.warning("dashboard: no database connection: %s", exc)
        return None


def _rows(sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    """Query, or an empty list if the table is not there yet. Never raises."""
    c = _conn()
    if c is None:
        return []
    try:
        cur = c.execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]
    except sqlite3.Error as exc:
        log.debug("dashboard query failed (%s): %s", exc, sql.split("FROM")[-1].strip()[:60])
        return []


def _one(sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    rows = _rows(sql, params)
    return rows[0] if rows else None


def _jload(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _int(raw: Any) -> int | None:
    """Base-unit integers are stored as TEXT so they survive above 2^63. Parse, don't guess."""
    if raw in (None, ""):
        return None
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


def day_start_ms(now: datetime | None = None) -> int:
    d = (now or datetime.now(UTC)).astimezone(UTC)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def day_key(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%d")


def native_symbol(chain: str | None) -> str:
    try:
        from kaiba.core.schemas import NATIVE_SYMBOL, Chain

        return NATIVE_SYMBOL.get(Chain(chain), "")
    except Exception:
        return ""


def native_amount(base_units: int | None, chain: str | None) -> float | None:
    """Base units -> a display float. Display only; money maths stays in ints elsewhere."""
    if base_units is None:
        return None
    try:
        from kaiba.core.schemas import NATIVE_DECIMALS, Chain

        decimals = NATIVE_DECIMALS.get(Chain(chain), 18)
    except Exception:
        decimals = 18
    return base_units / (10**decimals)


def short_addr(addr: str | None, head: int = 4, tail: int = 4) -> str:
    if not addr:
        return "—"
    if len(addr) <= head + tail + 1:
        return addr
    return f"{addr[:head]}…{addr[-tail:]}"


def summarise_payload(payload: dict[str, Any] | None, limit: int = 140) -> str:
    """One line for the feed row. Prefers a human field, else the first few scalars."""
    if not payload:
        return ""
    for key in ("summary", "message", "text", "thesis", "reason", "detail", "error", "note"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:limit]
    parts: list[str] = []
    for key, val in payload.items():
        if isinstance(val, (str, int, float, bool)) or val is None:
            parts.append(f"{key}={val}")
        elif isinstance(val, (list, tuple)):
            parts.append(f"{key}[{len(val)}]")
        if len(parts) >= 5:
            break
    return " · ".join(parts)[:limit]


# --------------------------------------------------------------------------------------
# risk envelope
# --------------------------------------------------------------------------------------


def risk_config() -> Any | None:
    try:
        from kaiba.core.config import get_risk

        return get_risk()
    except Exception as exc:
        log.warning("dashboard: risk config unreadable: %s", exc)
        return None


def risk_view() -> dict[str, Any]:
    """Lane modes, the envelope the agent may move inside, and today's loss against stop."""
    cfg = risk_config()
    if cfg is None:
        return {"available": False, "reason": "config/risk.yaml could not be read", "lanes": []}

    lanes = []
    for lane_key, lane_cfg in (cfg.lanes or {}).items():
        try:
            effective = cfg.effective_mode(lane_key).value
        except Exception:
            effective = "unknown"
        lanes.append(
            {
                "lane": str(getattr(lane_key, "value", lane_key)),
                "mode": str(getattr(lane_cfg.mode, "value", lane_cfg.mode)),
                "effective_mode": effective,
                "size_pct_min": lane_cfg.size_pct_min,
                "size_pct_max": lane_cfg.size_pct_max,
                "chains": [str(getattr(c, "value", c)) for c in (lane_cfg.chains or [])],
            }
        )
    lanes.sort(key=lambda row: row["lane"])

    bounds = cfg.bounds
    chains = []
    for chain_key, budget in (cfg.chains or {}).items():
        chain = str(getattr(chain_key, "value", chain_key))
        stop = budget.daily_loss_stop_base_units or None
        chains.append(
            {
                "chain": chain,
                "enabled": budget.enabled,
                "symbol": native_symbol(chain),
                "bankroll": native_amount(budget.bankroll_base_units or None, chain),
                "max_position": native_amount(budget.max_position_base_units or None, chain),
                "daily_loss_stop": native_amount(stop, chain),
                "daily_loss_stop_units": stop,
                "max_exposure_pct": budget.max_exposure_pct,
                "wallet": budget.wallet,
            }
        )
    chains.sort(key=lambda row: row["chain"])

    return {
        "available": True,
        "global_mode": str(getattr(cfg.global_mode, "value", cfg.global_mode)),
        "kill_switch": bool(cfg.kill_switch),
        "entries_paused": bool(cfg.entries_paused),
        "reduce_only": bool(cfg.reduce_only),
        "lanes": lanes,
        "chains": chains,
        "bounds": {
            "max_size_pct_bankroll": bounds.max_size_pct_bankroll,
            "max_daily_loss_pct": bounds.max_daily_loss_pct,
            "max_slippage_bps": bounds.max_slippage_bps,
            "max_concurrent_positions": bounds.max_concurrent_positions,
            "max_lane_mode": str(getattr(bounds.max_lane_mode, "value", bounds.max_lane_mode)),
            "allow_self_promotion": bounds.allow_self_promotion,
        },
        "daily_loss_pct_dial": (cfg.protection or {}).get("daily_loss_pct"),
        "today": risk_state_today(),
        "pnl_today": pnl_today(),
    }


def risk_state_today() -> dict[str, Any]:
    row = _one("SELECT * FROM risk_state WHERE day_key = ?", (day_key(),))
    if row is None:
        return {"available": False, "reason": "no risk_state row for today yet"}
    return {
        "available": True,
        "day_key": row["day_key"],
        "entries": row["entries"],
        "halted": bool(row["halted"]),
        "halt_reason": row["halt_reason"],
        "realized": _jload(row["realized_native_json"], {}),
        "updated_ms": row["updated_ms"],
    }


# --------------------------------------------------------------------------------------
# positions, trades, pnl
# --------------------------------------------------------------------------------------


def open_positions(limit: int = 100) -> list[dict[str, Any]]:
    """``positions`` rows with no ``closed_ms``.

    There is no mark price anywhere in the schema yet — quotes arrive with the execution
    layer — so ``current_price_usd`` and ``unrealized`` stay ``None`` and the template says
    so instead of printing a plausible-looking zero.
    """
    rows = _rows(
        "SELECT p.*, t.symbol AS symbol FROM positions p "
        "LEFT JOIN tokens t ON t.chain = p.chain AND t.address = p.token "
        "WHERE p.closed_ms IS NULL ORDER BY p.opened_ms DESC LIMIT ?",
        (limit,),
    )
    out = []
    for r in rows:
        chain = r["chain"]
        realized = _int(r["realized_native"])
        cost = _int(r["cost_native"])
        protection_ids = _jload(r["protection_ids_json"], [])
        tp_done = _jload(r["tp_done_json"], [])
        out.append(
            {
                "position_id": r["position_id"],
                "chain": chain,
                "symbol": r["symbol"],
                "token": r["token"],
                "token_short": short_addr(r["token"]),
                "lane": r["lane"],
                "mode": r["mode"],
                "opened_ms": r["opened_ms"],
                "qty": r["qty"],
                "entry_price_usd": r["entry_price_usd"],
                "peak_price_usd": r["peak_price_usd"],
                "stop_price_usd": r["stop_price_usd"],
                "current_price_usd": None,
                "current_unavailable": "no live quote source wired yet",
                "unrealized_native": None,
                "cost_native": native_amount(cost, chain),
                "realized_native": native_amount(realized, chain),
                "symbol_native": native_symbol(chain),
                "protected": bool(r["protected"]),
                "protection_count": len(protection_ids) if isinstance(protection_ids, list) else 0,
                "tp_done": tp_done if isinstance(tp_done, list) else [],
                "mae_pct": r["mae_pct"],
                "mfe_pct": r["mfe_pct"],
            }
        )
    return out


def pnl_today() -> dict[str, Any]:
    """Realized PnL from closed trades since 00:00 UTC, per chain, summed as integers."""
    rows = _rows(
        "SELECT chain, mode, pnl_native FROM trades WHERE closed_ms >= ?", (day_start_ms(),)
    )
    if not rows:
        return {"available": False, "reason": "no closed trades today", "by_chain": [], "trades": 0}
    totals: dict[tuple[str, str], int] = {}
    for r in rows:
        value = _int(r["pnl_native"])
        if value is None:
            continue
        totals[(r["chain"], r["mode"])] = totals.get((r["chain"], r["mode"]), 0) + value
    by_chain = [
        {
            "chain": chain,
            "mode": mode,
            "pnl_base_units": value,
            "pnl_native": native_amount(value, chain),
            "symbol": native_symbol(chain),
        }
        for (chain, mode), value in sorted(totals.items())
    ]
    return {"available": True, "by_chain": by_chain, "trades": len(rows)}


def recent_trades(limit: int = 20) -> list[dict[str, Any]]:
    rows = _rows("SELECT * FROM trades ORDER BY closed_ms DESC LIMIT ?", (limit,))
    for r in rows:
        r["pnl_native_display"] = native_amount(_int(r["pnl_native"]), r["chain"])
        r["symbol_native"] = native_symbol(r["chain"])
        r["token_short"] = short_addr(r["token"])
        r["mistakes"] = _jload(r.pop("mistakes_json", None), [])
    return rows


# --------------------------------------------------------------------------------------
# signals, decisions, tokens, opportunities
# --------------------------------------------------------------------------------------


def recent_signals(limit: int = 25) -> list[dict[str, Any]]:
    rows = _rows(
        "SELECT s.*, t.symbol AS symbol FROM signals s "
        "LEFT JOIN tokens t ON t.chain = s.chain AND t.address = s.token "
        "ORDER BY s.created_ms DESC LIMIT ?",
        (limit,),
    )
    out = []
    for r in rows:
        entities = _jload(r["entities_json"], [])
        wallets = _jload(r["wallets_json"], [])
        out.append(
            {
                "signal_id": r["signal_id"],
                "lane": r["lane"],
                "chain": r["chain"],
                "token": r["token"],
                "token_short": short_addr(r["token"]),
                "symbol": r["symbol"],
                "strength": r["strength"],
                "entity_count": len(entities) if isinstance(entities, list) else 0,
                "wallet_count": len(wallets) if isinstance(wallets, list) else 0,
                "reasons": _jload(r["reasons_json"], []),
                "window_s": r["window_s"],
                "created_ms": r["created_ms"],
            }
        )
    return out


def recent_decisions(limit: int = 25) -> list[dict[str, Any]]:
    rows = _rows("SELECT * FROM decisions ORDER BY ts_ms DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        out.append(
            {
                "decision_id": r["decision_id"],
                "ts_ms": r["ts_ms"],
                "lane": r["lane"],
                "mode": r["mode"],
                "chain": r["chain"],
                "token": r["token"],
                "token_short": short_addr(r["token"]),
                "action": r["action"],
                "thesis": r["thesis"],
                "confidence": r["confidence"],
                "dossier_grade": r["dossier_grade"],
                "size_pct_bankroll": r["size_pct_bankroll"],
                "blockers": _jload(r["blockers_json"], []),
            }
        )
    return out


def recent_tokens(limit: int = 20) -> list[dict[str, Any]]:
    rows = _rows(
        "SELECT d.chain, d.address, d.grade, d.score, d.built_at_ms, d.blockers_json, "
        "d.warnings_json, t.symbol AS symbol, t.name AS name "
        "FROM token_dossiers d LEFT JOIN tokens t ON t.chain = d.chain AND t.address = d.address "
        "ORDER BY d.built_at_ms DESC LIMIT ?",
        (limit,),
    )
    out = []
    for r in rows:
        out.append(
            {
                "chain": r["chain"],
                "address": r["address"],
                "address_short": short_addr(r["address"]),
                "symbol": r["symbol"],
                "name": r["name"],
                "grade": r["grade"],
                "score": r["score"],
                "built_at_ms": r["built_at_ms"],
                "blockers": _jload(r["blockers_json"], []),
                "warnings": _jload(r["warnings_json"], []),
            }
        )
    return out


def open_opportunities(limit: int = 10) -> list[dict[str, Any]]:
    return _rows(
        "SELECT opportunity_id, kind, name, chain, url, status, ev_score, deadline_ms "
        "FROM opportunities WHERE status = 'open' ORDER BY ev_score DESC LIMIT ?",
        (limit,),
    )


# --------------------------------------------------------------------------------------
# wallet intelligence
# --------------------------------------------------------------------------------------


def top_wallets(limit: int = 15) -> list[dict[str, Any]]:
    rows = _rows(
        "SELECT s.chain, s.address, s.score, s.grade, s.archetype, s.evidence_weight, "
        "s.realized_pnl_usd, s.win_rate, s.closed_trades, s.scored_at_ms, "
        "w.name AS name, w.cohort AS cohort, em.entity_id AS entity_id "
        "FROM wallet_scores s "
        "LEFT JOIN wallets w ON w.chain = s.chain AND w.address = s.address "
        "LEFT JOIN entity_members em ON em.chain = s.chain AND em.address = s.address "
        "ORDER BY s.score DESC LIMIT ?",
        (limit,),
    )
    for r in rows:
        r["address_short"] = short_addr(r["address"], 6, 6)
    return rows


def watchlist_summary() -> dict[str, Any]:
    """How many wallets we are tracking, and how many have actually been graded.

    ``top_wallets`` reads ``wallet_scores``, so it is empty until something has been
    graded — which needs on-chain evidence, not an operator label. Without this counter
    an operator who has just imported thousands of wallets sees a blank panel and cannot
    tell an import failure from an ungraded watchlist.
    """
    total = _rows("SELECT COUNT(*) AS n FROM wallets")
    scored = _rows("SELECT COUNT(*) AS n FROM wallet_scores")
    by_cohort = _rows(
        "SELECT cohort, COUNT(*) AS n FROM wallets GROUP BY cohort ORDER BY n DESC LIMIT 6"
    )
    by_chain = _rows(
        "SELECT chain, COUNT(*) AS n FROM wallets GROUP BY chain ORDER BY n DESC LIMIT 8"
    )
    return {
        "tracked": total[0]["n"] if total else 0,
        "graded": scored[0]["n"] if scored else 0,
        "by_cohort": by_cohort,
        "by_chain": by_chain,
    }


def entity_graph(limit_edges: int = MAX_GRAPH_EDGES) -> dict[str, Any]:
    """Nodes are wallets, edges are cluster edges. Cytoscape eats this shape directly."""
    edges = _rows(
        "SELECT chain, a, b, edge_type, confidence, observations FROM cluster_edges "
        "ORDER BY last_seen_ms DESC LIMIT ?",
        (limit_edges,),
    )
    addresses: dict[tuple[str, str], None] = {}
    for e in edges:
        addresses[(e["chain"], e["a"])] = None
        addresses[(e["chain"], e["b"])] = None

    scored = _rows(
        "SELECT chain, address, grade, archetype, score FROM wallet_scores ORDER BY score DESC LIMIT ?",
        (MAX_GRAPH_NODES,),
    )
    meta = {(r["chain"], r["address"]): r for r in scored}
    for key in list(meta):
        addresses.setdefault(key, None)

    members = {
        (r["chain"], r["address"]): r["entity_id"]
        for r in _rows("SELECT entity_id, chain, address FROM entity_members LIMIT 2000")
    }
    entities = {
        r["entity_id"]: r
        for r in _rows("SELECT entity_id, label, archetype, confidence, size FROM entities LIMIT 500")
    }

    nodes = []
    for chain, address in list(addresses)[:MAX_GRAPH_NODES]:
        info = meta.get((chain, address), {})
        entity_id = members.get((chain, address))
        entity = entities.get(entity_id or "", {})
        nodes.append(
            {
                "id": f"{chain}:{address}",
                "chain": chain,
                "address": address,
                "label": short_addr(address),
                "grade": info.get("grade"),
                "score": info.get("score"),
                "archetype": info.get("archetype") or entity.get("archetype") or "unknown",
                "entity_id": entity_id,
                "entity_label": entity.get("label"),
            }
        )
    known = {n["id"] for n in nodes}
    out_edges = [
        {
            "id": f"{e['chain']}:{e['a']}->{e['chain']}:{e['b']}:{e['edge_type']}",
            "source": f"{e['chain']}:{e['a']}",
            "target": f"{e['chain']}:{e['b']}",
            "edge_type": e["edge_type"],
            "confidence": e["confidence"],
            "observations": e["observations"],
        }
        for e in edges
        if f"{e['chain']}:{e['a']}" in known and f"{e['chain']}:{e['b']}" in known
    ]
    total_entities = _one("SELECT COUNT(*) AS n FROM entities") or {"n": 0}
    return {
        "available": bool(nodes),
        "reason": None if nodes else "no graded wallets or cluster edges yet",
        "nodes": nodes,
        "edges": out_edges,
        "entity_count": total_entities.get("n", 0),
    }


# --------------------------------------------------------------------------------------
# providers and events
# --------------------------------------------------------------------------------------


def provider_meters() -> dict[str, Any]:
    """``limiter.status()`` plus a 24h call rollup. Empty until something calls a provider."""
    meters: list[dict[str, Any]] = []
    try:
        from kaiba.core import db, limiter

        meters = list(limiter.status(conn=db.get_conn()))
    except Exception as exc:
        log.debug("dashboard: limiter status unavailable: %s", exc)
        return {"available": False, "reason": f"limiter unavailable: {exc}", "providers": []}

    since = day_start_ms()
    calls = _rows(
        "SELECT provider, status, COUNT(*) AS n FROM provider_calls WHERE ts_ms >= ? "
        "GROUP BY provider, status",
        (since,),
    )
    rollup: dict[str, dict[str, int]] = {}
    for row in calls:
        rollup.setdefault(row["provider"], {})[row["status"]] = row["n"]

    for m in meters:
        stats = rollup.get(m["provider"], {})
        m["calls_today"] = sum(stats.values())
        m["errors_today"] = stats.get("error", 0) + stats.get("rate_limited", 0)
        cap = m.get("capacity") or 0
        m["credit_pct"] = round(100 * m.get("credit", 0) / cap, 1) if cap else None
        m["healthy"] = not m.get("banned") and not m.get("family_bans")
    meters.sort(key=lambda m: m["provider"])
    return {
        "available": bool(meters),
        "reason": None if meters else "no provider has been called yet",
        "providers": meters,
        "healthy": sum(1 for m in meters if m["healthy"]),
        "total": len(meters),
    }


_LEVEL_RANK = {"debug": 0, "info": 1, "warn": 2, "error": 3}


def _event_dict(ev: Any) -> dict[str, Any]:
    payload = getattr(ev, "payload", {}) or {}
    kind = getattr(ev, "kind", "")
    chain = getattr(ev, "chain", None)
    subject = getattr(ev, "subject", None)
    return {
        "id": getattr(ev, "id", None),
        "ts_ms": getattr(ev, "ts_ms", None),
        "kind": str(getattr(kind, "value", kind)),
        "level": str(getattr(ev, "level", "info")),
        "chain": str(getattr(chain, "value", chain)) if chain else None,
        "subject": subject,
        "subject_short": short_addr(subject, 6, 6),
        "payload": payload,
        "summary": summarise_payload(payload),
        "trace_id": getattr(ev, "trace_id", None),
    }


def recent_events(limit: int = 60, kinds: list[str] | None = None) -> list[dict[str, Any]]:
    """Newest first, for the first paint of the agent stream."""
    try:
        from kaiba.core import db, events

        rows = events.recent(limit=limit, kinds=kinds, conn=db.get_conn())
    except Exception as exc:
        log.debug("dashboard: events.recent failed: %s", exc)
        return []
    return [_event_dict(e) for e in rows]


def tail_events(after_id: int = 0, limit: int = 100, kinds: list[str] | None = None) -> list[dict[str, Any]]:
    """Oldest first, ``id > after_id``. This is what the SSE loop polls."""
    try:
        from kaiba.core import db, events

        rows = events.tail(after_id=after_id, limit=limit, kinds=kinds, conn=db.get_conn())
    except Exception as exc:
        log.debug("dashboard: events.tail failed: %s", exc)
        return []
    return [_event_dict(e) for e in rows]


def latest_event_id() -> int:
    try:
        from kaiba.core import db, events

        return int(events.latest_id(conn=db.get_conn()))
    except Exception:
        return 0


def alert_count(since_ms: int | None = None) -> int:
    since = day_start_ms() if since_ms is None else since_ms
    row = _one(
        "SELECT COUNT(*) AS n FROM events WHERE ts_ms >= ? AND level IN ('warn','error')", (since,)
    )
    return int(row["n"]) if row else 0


def journal_entries(limit: int = 10) -> dict[str, Any]:
    try:
        from kaiba.core import db, journal

        rows = journal.read(limit=limit, conn=db.get_conn())
    except Exception as exc:
        log.debug("dashboard: journal unreadable: %s", exc)
        return {"available": False, "reason": f"journal unavailable: {exc}", "entries": []}
    if not rows:
        return {"available": False, "reason": "the journal is empty", "entries": []}
    return {"available": True, "reason": None, "entries": rows}


# --------------------------------------------------------------------------------------
# the metric strip
# --------------------------------------------------------------------------------------


def overview() -> dict[str, Any]:
    """Everything the top strip needs, in one call."""
    risk = risk_view()
    providers = provider_meters()
    positions = _one("SELECT COUNT(*) AS n FROM positions WHERE closed_ms IS NULL") or {"n": 0}
    by_mode = _rows(
        "SELECT mode, COUNT(*) AS n FROM positions WHERE closed_ms IS NULL GROUP BY mode"
    )
    events_today = _one("SELECT COUNT(*) AS n FROM events WHERE ts_ms >= ?", (day_start_ms(),))

    if not risk.get("available"):
        mode = "unknown"
    elif risk["kill_switch"]:
        mode = "halted"
    else:
        mode = risk["global_mode"]

    return {
        "mode": mode,
        "kill_switch": risk.get("kill_switch"),
        "entries_paused": risk.get("entries_paused"),
        "reduce_only": risk.get("reduce_only"),
        "open_positions": int(positions.get("n", 0)),
        "open_positions_by_mode": {r["mode"]: r["n"] for r in by_mode},
        "pnl_today": risk.get("pnl_today") or pnl_today(),
        "alerts_today": alert_count(),
        "events_today": int((events_today or {}).get("n", 0)),
        "providers": {
            "available": providers["available"],
            "reason": providers.get("reason"),
            "healthy": providers.get("healthy", 0),
            "total": providers.get("total", 0),
        },
        "latest_event_id": latest_event_id(),
        "generated_ms": int(datetime.now(UTC).timestamp() * 1000),
    }
