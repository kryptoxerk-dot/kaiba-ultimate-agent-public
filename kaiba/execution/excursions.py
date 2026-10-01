"""Sample accepted live watchdog quotes without pretending to have a complete price path.

AUDIT-INTEGRATE delegated writer. The watchdog owns quote acceptance; this module adds
no provider read or trading decision. Marks use observation time, not polling time.
Existing marks/events/kv hold the evidence, so no core migration is needed.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import TYPE_CHECKING

from kaiba.core.db import fetch_one, jdump, jload, tx, upsert
from kaiba.core.events import emit
from kaiba.core.schemas import EventKind, Position

if TYPE_CHECKING:
    from kaiba.execution.watchdog import PriceQuote


def record_live_quote(conn: sqlite3.Connection, position: Position, quote: PriceQuote) -> bool:
    """Record one accepted observation and its sampled (not full-path) extrema."""
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
