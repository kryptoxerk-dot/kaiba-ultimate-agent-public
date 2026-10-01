"""Hash-chained learning journal.

Append-only by construction: every entry hashes the previous one, so a rewritten history
is detectable with :func:`verify`. The reflection job writes lessons here; nothing ever
updates or deletes a row.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable
from typing import Any, Literal

from kaiba.core.db import fetch_all, get_conn, jdump, jload
from kaiba.core.schemas import now_ms

JournalKind = Literal["observation", "lesson", "experiment", "change", "outcome", "correction"]

GENESIS = "0" * 64
MAX_BODY = 8192


def _hash(seq: int, ts_ms: int, kind: str, subject: str | None, body: str, prev: str) -> str:
    blob = f"{seq}|{ts_ms}|{kind}|{subject or ''}|{body}|{prev}"
    return hashlib.sha256(blob.encode()).hexdigest()


def append(
    kind: JournalKind,
    body: str,
    *,
    subject: str | None = None,
    refs: Iterable[str] | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Add one entry and return it. Body is capped so a runaway model cannot flood it."""
    c = conn or get_conn()
    body = body.strip()[:MAX_BODY]
    if not body:
        raise ValueError("journal body is empty")
    c.execute("BEGIN IMMEDIATE")
    try:
        row = c.execute("SELECT seq, entry_hash FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
        prev_hash = row["entry_hash"] if row else GENESIS
        seq = (row["seq"] + 1) if row else 1
        ts = now_ms()
        entry_hash = _hash(seq, ts, kind, subject, body, prev_hash)
        c.execute(
            "INSERT INTO journal (seq, ts_ms, kind, subject, body, refs_json, prev_hash, entry_hash) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (seq, ts, kind, subject, body, jdump(list(refs or [])), prev_hash, entry_hash),
        )
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    return {
        "seq": seq, "ts_ms": ts, "kind": kind, "subject": subject,
        "body": body, "refs": list(refs or []), "entry_hash": entry_hash,
    }


def read(
    limit: int = 20,
    kind: JournalKind | None = None,
    subject: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    c = conn or get_conn()
    sql = "SELECT * FROM journal"
    where, params = [], []
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if subject:
        where.append("subject = ?")
        params.append(subject)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY seq DESC LIMIT ?"
    params.append(limit)
    rows = fetch_all(c, sql, params)
    for r in rows:
        r["refs"] = jload(r.pop("refs_json"), [])
    return rows


def verify(conn: sqlite3.Connection | None = None) -> tuple[bool, str | None]:
    """Recompute the chain. Returns ``(ok, first_bad_description)``."""
    c = conn or get_conn()
    prev = GENESIS
    for r in c.execute("SELECT * FROM journal ORDER BY seq ASC"):
        expect = _hash(r["seq"], r["ts_ms"], r["kind"], r["subject"], r["body"], prev)
        if r["prev_hash"] != prev:
            return False, f"seq {r['seq']}: prev_hash mismatch"
        if r["entry_hash"] != expect:
            return False, f"seq {r['seq']}: entry_hash mismatch"
        prev = r["entry_hash"]
    return True, None


def stats(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    c = conn or get_conn()
    rows = fetch_all(c, "SELECT kind, COUNT(*) AS n FROM journal GROUP BY kind")
    total = c.execute("SELECT COUNT(*) AS n FROM journal").fetchone()["n"]
    ok, err = verify(c)
    return {"total": total, "by_kind": {r["kind"]: r["n"] for r in rows}, "chain_ok": ok, "error": err}
