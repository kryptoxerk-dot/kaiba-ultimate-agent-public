"""The event bus: one append-only table, three consumers.

The dashboard streams from it over SSE, the nightly reflection job reads it, and traces
join to it by ``trace_id``. Writing an event is cheap and must never raise into a caller's
hot path, so :func:`emit` swallows storage errors after logging them.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Iterator
from typing import Any

from kaiba.core.db import fetch_all, get_conn, jdump, jload
from kaiba.core.schemas import Chain, Event, EventKind, digest, now_ms

log = logging.getLogger(__name__)


def emit(
    kind: EventKind | str,
    payload: dict[str, Any] | None = None,
    *,
    chain: Chain | str | None = None,
    subject: str | None = None,
    level: str = "info",
    trace_id: str | None = None,
    dedupe_key: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int | None:
    """Append one event. Returns its id, or ``None`` if it was a duplicate.

    ``dedupe_key`` is enforced by a unique index, so concurrent producers cannot write the
    same logical event twice (two listeners seeing one pump.fun creation, for example).
    """
    c = conn or get_conn()
    k = kind.value if isinstance(kind, EventKind) else str(kind)
    ch = chain.value if isinstance(chain, Chain) else (chain or None)
    try:
        cur = c.execute(
            "INSERT INTO events (ts_ms, kind, level, chain, subject, payload, trace_id, dedupe_key) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now_ms(), k, level, ch, subject, jdump(payload or {}), trace_id, dedupe_key),
        )
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None  # duplicate dedupe_key
    except sqlite3.Error as exc:  # storage must never break ingestion
        log.warning("event emit failed kind=%s: %s", k, exc)
        return None


def emit_once(kind: EventKind | str, payload: dict[str, Any], **kw: Any) -> int | None:
    """Emit with a dedupe key derived from the payload."""
    kw.setdefault("dedupe_key", f"{kind}:{digest(payload)}")
    return emit(kind, payload, **kw)


def _row_to_event(row: dict[str, Any]) -> Event:
    return Event(
        id=row["id"],
        ts_ms=row["ts_ms"],
        kind=row["kind"],
        level=row["level"],
        chain=row["chain"],
        subject=row["subject"],
        payload=jload(row["payload"]),
        trace_id=row["trace_id"],
        dedupe_key=row["dedupe_key"],
    )


def tail(
    after_id: int = 0,
    limit: int = 200,
    kinds: Iterable[str] | None = None,
    subject: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[Event]:
    """Events with ``id > after_id``, oldest first. This is what the SSE feed polls."""
    c = conn or get_conn()
    sql = "SELECT * FROM events WHERE id > ?"
    params: list[Any] = [after_id]
    kinds = list(kinds) if kinds else None
    if kinds:
        sql += f" AND kind IN ({','.join('?' for _ in kinds)})"
        params.extend(kinds)
    if subject:
        sql += " AND subject = ?"
        params.append(subject)
    sql += " ORDER BY id ASC LIMIT ?"
    params.append(limit)
    return [_row_to_event(r) for r in fetch_all(c, sql, params)]


def recent(
    limit: int = 100,
    kinds: Iterable[str] | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[Event]:
    """Newest first — for the dashboard's initial paint and `/events` in the MCP server."""
    c = conn or get_conn()
    sql = "SELECT * FROM events"
    params: list[Any] = []
    kinds = list(kinds) if kinds else None
    if kinds:
        sql += f" WHERE kind IN ({','.join('?' for _ in kinds)})"
        params.extend(kinds)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [_row_to_event(r) for r in fetch_all(c, sql, params)]


def latest_id(conn: sqlite3.Connection | None = None) -> int:
    c = conn or get_conn()
    row = c.execute("SELECT COALESCE(MAX(id), 0) AS m FROM events").fetchone()
    return int(row["m"])


def counts_by_kind(since_ms: int = 0, conn: sqlite3.Connection | None = None) -> dict[str, int]:
    c = conn or get_conn()
    rows = fetch_all(
        c, "SELECT kind, COUNT(*) AS n FROM events WHERE ts_ms >= ? GROUP BY kind", (since_ms,)
    )
    return {r["kind"]: r["n"] for r in rows}


def follow(
    after_id: int = 0,
    kinds: Iterable[str] | None = None,
    poll_s: float = 1.0,
    conn: sqlite3.Connection | None = None,
) -> Iterator[Event]:
    """Blocking generator over new events. Used by the CLI's ``kaiba watch``."""
    import time

    cursor = after_id
    kinds = list(kinds) if kinds else None
    while True:
        batch = tail(cursor, limit=500, kinds=kinds, conn=conn)
        if batch:
            for ev in batch:
                yield ev
                cursor = ev.id or cursor
        else:
            time.sleep(poll_s)
