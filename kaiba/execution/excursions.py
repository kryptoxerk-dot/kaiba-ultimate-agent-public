"""Sample accepted live watchdog quotes without pretending to have a complete price path.

AUDIT-INTEGRATE delegated writer. The watchdog owns quote acceptance; this module adds
no provider read or trading decision. Marks use observation time, not polling time.
Existing marks/events/kv hold the evidence, so no core migration is needed.

A quote the watchdog accepts for PROTECTION is not automatically evidence for a STUDY.
Protection deliberately accepts scope-less quotes (the curve, router and Jupiter readers
answer the (chain, token) they were asked for without stamping it) because refusing them
would blind live stops. A mark is read later, by exit and loss studies, with no memory of
what was asked -- so it needs the stricter bar in :func:`sample_rejection`. Fixed
2026-10-05: the sampler had no bar of its own and wrote marks observed before the
position opened, marks with no chain/token, marks from source ``none``/blank, and marks
for shadow/off positions the paper broker already owns.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import TYPE_CHECKING

from kaiba.core.db import fetch_one, jdump, jload, tx, upsert
from kaiba.core.events import emit
from kaiba.core.schemas import EventKind, LaneMode, Position

if TYPE_CHECKING:
    from kaiba.execution.watchdog import PriceQuote

#: The modes this sampler owns. SHADOW is marked by ``PaperBroker.mark_to_market``; OFF
#: is not traded. Sampling either here would double-write or invent a path.
LIVE_MODES = frozenset({LaneMode.LIVE, LaneMode.CANARY})

#: Source strings that name no producer (``PriceQuote.source`` defaults to "none").
_NO_SOURCE = frozenset({"", "none"})


def sample_rejection(position: Position, quote: PriceQuote) -> str | None:
    """Why ``quote`` must not become an excursion mark for ``position``; None if it may.

    Checks the sampler's own contract on top of ``quote.invalid_reason`` (basis, price,
    observation time, freshness), which is re-checked so a caller that skips the
    watchdog's validation cannot write a mark either.
    """
    if position.mode not in LIVE_MODES:
        return f"mode_not_live:{position.mode.value}"
    reason = quote.invalid_reason
    if reason is not None:
        return reason
    if quote.chain is None:
        return "scope_missing_chain"
    if quote.token is None or not str(quote.token).strip():
        return "scope_missing_token"
    if quote.chain != position.chain:
        return "scope_wrong_chain"
    if quote.token != position.token:
        return "scope_wrong_token"
    if (quote.source or "").strip().lower() in _NO_SOURCE:
        return "source_missing"
    if position.opened_ms is not None and quote.observed_ms < position.opened_ms:
        return "observed_before_open"
    return None


def record_live_quote(conn: sqlite3.Connection, position: Position, quote: PriceQuote) -> bool:
    """Record one accepted observation and its sampled (not full-path) extrema.

    Returns False, writing nothing, when :func:`sample_rejection` refuses the quote.
    """
    if sample_rejection(position, quote) is not None:
        return False
    key = f"watchdog.excursion:{position.position_id}"
    with tx(conn):
        row = fetch_one(conn, "SELECT * FROM positions WHERE position_id=?", (position.position_id,))
        entry = Decimal(row["entry_price_usd"])
        return_pct = float((quote.price_usd / entry - 1) * 100)
        mfe = max(row["mfe_pct"] or 0.0, return_pct)
        mae = min(row["mae_pct"] or 0.0, return_pct)
        conn.execute("UPDATE positions SET mfe_pct=?,mae_pct=? WHERE position_id=?",
                     (mfe, mae, position.position_id))
        mark = conn.execute(
            "INSERT INTO position_marks (position_id,ts_ms,price_usd,return_pct,mae_pct,mfe_pct) "
            "VALUES (?,?,?,?,?,?)",
            (position.position_id, quote.observed_ms, str(quote.price_usd), return_pct, mae, mfe),
        )
        memory = fetch_one(conn, "SELECT value FROM kv WHERE key=?", (key,))
        previous = jload(memory["value"]) if memory else {}
        payload = {
            "service": "watchdog", "event": "live_excursion_sample", "version": 1,
            "position_id": position.position_id, "mark_id": mark.lastrowid,
            "entry_price_usd": str(entry), "return_pct": return_pct,
            "mae_pct": mae, "mfe_pct": mfe,
            "quote_provenance": quote.model_dump(mode="json"),
            "coverage": "sampled_quotes_only", "history_complete": False,
            "sample_count": previous.get("sample_count", 0) + 1,
            "first_observed_ms": previous.get("first_observed_ms", quote.observed_ms),
            "last_observed_ms": quote.observed_ms,
        }
        upsert(conn, "kv", {"key": key, "value": jdump(payload), "updated_ms": quote.observed_ms}, ["key"])
        emit(EventKind.SYSTEM, payload, chain=position.chain, subject=position.token, conn=conn)
    position.mfe_pct, position.mae_pct = mfe, mae
    return True
