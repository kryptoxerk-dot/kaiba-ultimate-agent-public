"""The evolving playbook: numbered rules with evidence counters.

This is the ACE pattern (ICLR 2026) rather than a rolling summary, and the difference is
the whole point. A summary is rewritten every night, so every night it loses the reason a
rule exists — context collapse, brevity bias, and an agent that keeps relearning the same
lesson. Here rules are **appended and curated**: a rule is added once, its hits and misses
accumulate against it, and when it stops earning its place it is *retired*, never deleted.
The row stays, the evidence stays, and the journal records why.

Consequences that the code enforces:

* :func:`retire` and :func:`expire_stale` change ``status`` and write a journal entry. No
  function in this module issues a DELETE.
* Counters are visible in :func:`render_for_prompt`, so the model that proposes new rules
  can see which of its previous rules actually worked. That is the feedback signal.
* Ordering is by a Laplace-smoothed hit rate, so a rule that is 1-for-1 does not outrank a
  rule that is 40-for-42.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Sequence
from typing import Any

from kaiba.core import journal
from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump, jload
from kaiba.core.schemas import Lane, digest, now_ms

log = logging.getLogger(__name__)

MAX_RULE_CHARS = 280
DEFAULT_MAX_AGE_DAYS = 30
DAY_MS = 86_400_000

STATUS_ACTIVE = "active"
STATUS_RETIRED = "retired"
STATUS_PROPOSED = "proposed"


def _lane_value(lane: Lane | str | None) -> str | None:
    if lane is None:
        return None
    return lane.value if isinstance(lane, Lane) else str(lane)


def _norm_text(text: str) -> str:
    """One line, collapsed whitespace, capped. A rule that needs a paragraph is two rules."""
    cleaned = " ".join(str(text).split())
    if not cleaned:
        raise ValueError("playbook rule text is empty")
    return cleaned[:MAX_RULE_CHARS]


def rule_id_for(text: str, lane: Lane | str | None) -> str:
    """Deterministic id from normalised text + lane, so the same rule cannot be added twice."""
    return "pb_" + digest({"text": _norm_text(text), "lane": _lane_value(lane)})[:16]


def add_rule(
    text: str,
    *,
    lane: Lane | str | None = None,
    evidence: Iterable[str] | None = None,
    status: str = STATUS_ACTIVE,
    expires_ms: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Append a rule and return its id.

    Idempotent by content: re-proposing an existing rule merges the new evidence and
    re-activates it if it had been retired (that is what "re-confirmed" means), instead of
    creating a duplicate. Returns the rule id either way.
    """
    c = conn or get_conn()
    body = _norm_text(text)
    lane_v = _lane_value(lane)
    rid = rule_id_for(body, lane_v)
    ts = now_ms()
    refs = [str(e) for e in (evidence or [])]

    existing = fetch_one(c, "SELECT * FROM playbook WHERE rule_id = ?", (rid,))
    if existing:
        merged = list(dict.fromkeys(jload(existing["evidence_json"], []) + refs))
        c.execute(
            "UPDATE playbook SET status = ?, updated_ms = ?, evidence_json = ?, expires_ms = ? "
            "WHERE rule_id = ?",
            (STATUS_ACTIVE, ts, jdump(merged), expires_ms, rid),
        )
        c.execute("DELETE FROM playbook_retirements WHERE rule_id = ?", (rid,))
        journal.append(
            "change",
            f"playbook rule re-confirmed: {body}",
            subject=rid,
            refs=refs,
            conn=c,
        )
        return rid

    c.execute(
        "INSERT INTO playbook (rule_id, created_ms, updated_ms, lane, text, hits, misses, "
        "status, evidence_json, expires_ms) VALUES (?,?,?,?,?,0,0,?,?,?)",
        (rid, ts, ts, lane_v, body, status, jdump(refs), expires_ms),
    )
    journal.append("change", f"playbook rule added: {body}", subject=rid, refs=refs, conn=c)
    return rid


def _record(rule_id: str, outcome: str, ref: str | None, conn: sqlite3.Connection | None) -> bool:
    c = conn or get_conn()
    column = "hits" if outcome == "hit" else "misses"
    cur = c.execute(
        f"UPDATE playbook SET {column} = {column} + 1, updated_ms = ? WHERE rule_id = ?",
        (now_ms(), rule_id),
    )
    if cur.rowcount == 0:
        log.warning("playbook %s for unknown rule %s", outcome, rule_id)
        return False
    c.execute(
        "INSERT INTO playbook_hits (rule_id, ts_ms, outcome, ref) VALUES (?,?,?,?)",
        (rule_id, now_ms(), outcome, ref),
    )
    return True


def record_hit(rule_id: str, *, ref: str | None = None, conn: sqlite3.Connection | None = None) -> bool:
    """The rule fired and the trade went the way the rule said it would."""
    return _record(rule_id, "hit", ref, conn)


def record_miss(rule_id: str, *, ref: str | None = None, conn: sqlite3.Connection | None = None) -> bool:
    """The rule fired and was wrong. Misses are kept: they are how a rule gets retired."""
    return _record(rule_id, "miss", ref, conn)


def retire(rule_id: str, reason: str, *, conn: sqlite3.Connection | None = None) -> bool:
    """Retire a rule. A status change plus a journal entry — the row is never deleted."""
    c = conn or get_conn()
    row = fetch_one(c, "SELECT * FROM playbook WHERE rule_id = ?", (rule_id,))
    if row is None:
        return False
    if row["status"] == STATUS_RETIRED:
        return False
    ts = now_ms()
    c.execute("UPDATE playbook SET status = ?, updated_ms = ? WHERE rule_id = ?", (STATUS_RETIRED, ts, rule_id))
    entry = journal.append(
        "change",
        f"playbook rule retired ({reason}): {row['text']}",
        subject=rule_id,
        refs=[],
        conn=c,
    )
    c.execute(
        "INSERT INTO playbook_retirements (rule_id, retired_ms, reason, journal_seq) VALUES (?,?,?,?) "
        "ON CONFLICT(rule_id) DO UPDATE SET retired_ms=excluded.retired_ms, reason=excluded.reason, "
        "journal_seq=excluded.journal_seq",
        (rule_id, ts, str(reason)[:500], entry["seq"]),
    )
    return True


def _hit_rate(row: dict[str, Any]) -> float:
    """Laplace-smoothed, so one lucky hit does not top the list."""
    hits, misses = int(row["hits"]), int(row["misses"])
    return (hits + 1) / (hits + misses + 2)


def get_rule(rule_id: str, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    c = conn or get_conn()
    row = fetch_one(c, "SELECT * FROM playbook WHERE rule_id = ?", (rule_id,))
    if row is None:
        return None
    row["evidence"] = jload(row.pop("evidence_json"), [])
    row["hit_rate"] = _hit_rate(row)
    row["last_hit_ms"] = last_hit_ms(rule_id, c)
    return row


def last_hit_ms(rule_id: str, conn: sqlite3.Connection | None = None) -> int | None:
    c = conn or get_conn()
    row = fetch_one(
        c,
        "SELECT MAX(ts_ms) AS ts FROM playbook_hits WHERE rule_id = ? AND outcome = 'hit'",
        (rule_id,),
    )
    return int(row["ts"]) if row and row["ts"] is not None else None


def active_rules(
    lane: Lane | str | None = None,
    conn: sqlite3.Connection | None = None,
    *,
    include_global: bool = True,
    now: int | None = None,
) -> list[dict[str, Any]]:
    """Active, unexpired rules sorted by hit rate.

    ``lane=None`` returns everything. A lane query returns that lane's rules plus the
    lane-agnostic ones, because a global rule applies everywhere by definition.
    """
    c = conn or get_conn()
    ts = now if now is not None else now_ms()
    sql = "SELECT * FROM playbook WHERE status = ? AND (expires_ms IS NULL OR expires_ms > ?)"
    params: list[Any] = [STATUS_ACTIVE, ts]
    lane_v = _lane_value(lane)
    if lane_v is not None:
        if include_global:
            sql += " AND (lane = ? OR lane IS NULL)"
        else:
            sql += " AND lane = ?"
        params.append(lane_v)
    rows = fetch_all(c, sql, params)
    for r in rows:
        r["evidence"] = jload(r.pop("evidence_json"), [])
        r["hit_rate"] = _hit_rate(r)
        r["last_hit_ms"] = last_hit_ms(r["rule_id"], c)
    rows.sort(key=lambda r: (-r["hit_rate"], -int(r["hits"]), int(r["created_ms"]), r["rule_id"]))
    return rows


def expire_stale(
    conn: sqlite3.Connection | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    *,
    now: int | None = None,
) -> list[str]:
    """Retire active rules that have not had a hit in ``max_age_days``.

    "No hits" is measured from the last recorded hit, falling back to when the rule was
    created or last re-confirmed. Re-confirming a rule (:func:`add_rule` with the same
    text) resets the clock, which is the "unless re-confirmed" escape hatch. Retirement is
    a status change plus a journal entry; nothing is deleted, so a rule retired for
    staleness can be cited as evidence and revived later.
    """
    c = conn or get_conn()
    ts = now if now is not None else now_ms()
    cutoff = ts - max_age_days * DAY_MS
    retired: list[str] = []
    for row in fetch_all(c, "SELECT * FROM playbook WHERE status = ?", (STATUS_ACTIVE,)):
        rid = row["rule_id"]
        expires = row["expires_ms"]
        if expires is not None and int(expires) <= ts:
            if retire(rid, f"expired at {expires}", conn=c):
                retired.append(rid)
            continue
        reference = last_hit_ms(rid, c)
        if reference is None:
            reference = max(int(row["created_ms"]), int(row["updated_ms"] or 0))
        if reference < cutoff:
            if retire(rid, f"no hit in {max_age_days}d", conn=c):
                retired.append(rid)
    return retired


def render_for_prompt(
    lane: Lane | str | None = None,
    max_chars: int = 4000,
    conn: sqlite3.Connection | None = None,
    *,
    rules: Sequence[dict[str, Any]] | None = None,
) -> str:
    """The numbered rule block injected into the reflection prompt.

    Counters are printed next to each rule on purpose: the model is being asked to curate
    its own rule set, and it cannot do that without seeing which rules are earning their
    place. Hard-capped at ``max_chars`` — a prompt block that silently grows is how the
    reflection prompt stops fitting and the whole loop degrades.
    """
    items = list(rules) if rules is not None else active_rules(lane, conn)
    if not items:
        return "PLAYBOOK: (empty — no rules have been confirmed yet)"

    header = "PLAYBOOK (hit/miss counters are measured, not claimed):"
    if max_chars <= len(header):
        return header[:max_chars]

    lines = [header]
    used = len(header)
    for i, r in enumerate(items, start=1):
        scope = r.get("lane") or "all-lanes"
        line = f"{i}. [{int(r['hits'])}h/{int(r['misses'])}m {scope}] {r['text']}"
        if used + 1 + len(line) > max_chars:
            remaining = len(items) - (i - 1)
            note = f"... {remaining} more rules omitted (char cap)"
            if used + 1 + len(note) <= max_chars:
                lines.append(note)
            break
        lines.append(line)
        used += 1 + len(line)
    return "\n".join(lines)


def stats(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    c = conn or get_conn()
    rows = fetch_all(c, "SELECT status, COUNT(*) AS n FROM playbook GROUP BY status")
    total = fetch_one(c, "SELECT COUNT(*) AS n FROM playbook") or {"n": 0}
    return {"total": int(total["n"]), "by_status": {r["status"]: int(r["n"]) for r in rows}}


__all__ = [
    "DEFAULT_MAX_AGE_DAYS",
    "MAX_RULE_CHARS",
    "STATUS_ACTIVE",
    "STATUS_PROPOSED",
    "STATUS_RETIRED",
    "active_rules",
    "add_rule",
    "expire_stale",
    "get_rule",
    "last_hit_ms",
    "record_hit",
    "record_miss",
    "render_for_prompt",
    "retire",
    "rule_id_for",
    "stats",
]
