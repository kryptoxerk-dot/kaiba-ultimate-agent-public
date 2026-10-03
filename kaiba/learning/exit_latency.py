"""Where an exit's time goes, per chain: mark, decision, submit, confirmation.

Journal #4997 (2026-10-02): on robinhood, in 20 of 40 stop/emergency exits the tape fell
through -30% and our close landed a median 83 s after the first -30% print, against a
12 s poll. The operator's instruction was to time each stop end to end BEFORE changing any
stop parameter. This module reads that timing back.

Two sources, because one of them does not exist yet for the past:

* :func:`report` -- the ``exit_timing`` events :class:`kaiba.execution.watchdog.Watchdog`
  writes on every exit attempt that reaches the submitter, joined to ``orders`` on
  ``order_id`` for confirmation. Exact stamps, every segment this process owns.
* :func:`legacy_stop_timelines` -- what can be RECONSTRUCTED for exits before those events
  existed: the tape (``swaps``), our accepted marks (``position_marks``), the watchdog's
  ``quote_decision`` / ``exit_submitted`` events and the order row. Coarser, and only for
  stop/emergency exits where the tape crossed the threshold, but it is the only answer for
  the 30 days the journal measured.

Segments (all milliseconds; an "attempt" is one call to the submitter, an "exit" is the run
of attempts on one position that ends in a send):

======================  ===========================================================
mark_age_at_decision    decision - when the trigger mark was observed by its source
previous_mark_to_mark   trigger mark - the mark before it (poll interval, or blindness)
prev_price_held         how long the price before the trigger mark had been showing
                        (only when the trigger mark changed the price): a frozen feed
tick_to_decision        decision - tick start (prefetch + the positions ahead of it)
retry                   first decision - the sending attempt's submit start
decision_to_submit      sending attempt: decision -> submitter called
submit_call             sending attempt: submitter call, end to end
wallet_read / min_out   inside the submitter: balance read, native price for min_out
executor                inside the submitter: executor.submit (gmgn-cli)
confirm                 submit returned -> the order row reads ``filled`` (reconcile)
decision_to_fill        first decision -> order filled
mark_to_fill            trigger mark observed -> order filled
======================  ===========================================================

Read-only. Run on the box::

    nice -n 19 .venv/bin/python -m kaiba.learning.exit_latency \\
        --db /home/ubuntu/kaiba/data/kaiba.db --since-days 30 --legacy
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

#: ``exit_timing`` events carry ``dedupe_key = exit_timing:<position>:<ms>:<attempt>``; this
#: range is how they are found through ``idx_events_dedupe`` instead of by scanning every
#: ``system`` event. ``;`` is the character after ``:``.
DEDUPE_LO = "exit_timing:"
DEDUPE_HI = "exit_timing;"

SEGMENTS: tuple[str, ...] = (
    "mark_age_at_decision",
    "previous_mark_to_mark",
    "prev_price_held",
    "tick_to_decision",
    "retry",
    "decision_to_submit",
    "submit_call",
    "wallet_read",
    "min_out",
    "executor",
    "confirm",
    "decision_to_fill",
    "mark_to_fill",
)

#: Exit reasons that are a price stop. Used by the legacy reconstruction only.
STOP_PREFIXES: tuple[str, ...] = ("stop_loss", "emergency_loss")


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile, ``q`` in [0, 1]. ``None`` for no data."""
    data = sorted(float(v) for v in values)
    if not data:
        return None
    pos = (len(data) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(data) - 1)
    return data[lo] + (data[hi] - data[lo]) * (pos - lo)


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _diff(later: Any, earlier: Any) -> int | None:
    a, b = _int(later), _int(earlier)
    return None if a is None or b is None else a - b


def _rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, tuple(params))
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


# --------------------------------------------------------------------------------------
# exit_timing events
# --------------------------------------------------------------------------------------


def load_attempts(
    conn: sqlite3.Connection,
    *,
    since_ms: int | None = None,
    until_ms: int | None = None,
    chains: Iterable[str] | None = None,
    include_shadow: bool = False,
) -> list[dict[str, Any]]:
    """Every ``exit_timing`` event in range, oldest first, payload flattened onto the row."""
    sql = "SELECT id, ts_ms, chain, subject, payload FROM events WHERE dedupe_key >= ? AND dedupe_key < ?"
    params: list[Any] = [DEDUPE_LO, DEDUPE_HI]
    if since_ms is not None:
        sql += " AND ts_ms >= ?"
        params.append(int(since_ms))
    if until_ms is not None:
        sql += " AND ts_ms < ?"
        params.append(int(until_ms))
    wanted = {c.lower() for c in chains} if chains else None
    out: list[dict[str, Any]] = []
    for row in _rows(conn, sql, params):
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("event") != "exit_timing":
            continue
        if wanted is not None and str(row["chain"] or "").lower() not in wanted:
            continue
        if not include_shadow and payload.get("mode") == "shadow":
            continue
        out.append({**payload, "_id": row["id"], "_ts_ms": row["ts_ms"], "_chain": row["chain"]})
    out.sort(key=lambda a: (_int((a.get("ms") or {}).get("decided")) or 0, a["_id"]))
    return out


def exits_from_attempts(attempts: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group attempts per position into exits: a run of failures closed by a send.

    An exit that never sent (only failures so far) is kept, with no sending attempt, so a
    position that cannot be sold still shows up as the longest latency there is.
    """
    by_position: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attempt in attempts:
        by_position[str(attempt.get("position_id"))].append(attempt)
    exits: list[dict[str, Any]] = []
    for position_id, rows in by_position.items():
        run: list[dict[str, Any]] = []
        for attempt in rows:
            run.append(attempt)
            if attempt.get("outcome") in {"submitted", "ambiguous"}:
                exits.append({"position_id": position_id, "attempts": run, "sent": attempt})
                run = []
        if run:
            exits.append({"position_id": position_id, "attempts": run, "sent": None})
    return exits


def exit_segments(exit_: dict[str, Any], order: dict[str, Any] | None) -> dict[str, int | None]:
    """The segment table above for one exit. ``order`` is its ``orders`` row, if any."""
    first = exit_["attempts"][0]
    sent = exit_["sent"]
    final = sent or exit_["attempts"][-1]
    first_ms = first.get("ms") or {}
    final_ms = final.get("ms") or {}
    seg = final.get("segments_ms") or {}
    first_seg = first.get("segments_ms") or {}
    sub = final.get("submitter_ms") or {}
    mark_observed = (first.get("mark") or {}).get("observed_ms")
    filled_ms = None
    if order is not None and str(order.get("state") or "") == "filled":
        filled_ms = _int(order.get("updated_ms"))
    return {
        "mark_age_at_decision": _int(first_seg.get("mark_age_at_decision")),
        "previous_mark_to_mark": _int(first_seg.get("previous_mark_to_mark")),
        "prev_price_held": _int(first_seg.get("prev_price_held")),
        "tick_to_decision": _int(first_seg.get("tick_to_decision")),
        "retry": _diff(final_ms.get("submit_started"), first_ms.get("decided")) if sent else None,
        "decision_to_submit": _int(seg.get("decision_to_submit")),
        "submit_call": _int(seg.get("submit_call")),
        "wallet_read": _int(sub.get("wallet_read_ms")),
        "min_out": _int(sub.get("min_out_ms")),
        "executor": _int(sub.get("executor_ms")),
        "confirm": _diff(filled_ms, final_ms.get("submit_returned")) if sent else None,
        "decision_to_fill": _diff(filled_ms, first_ms.get("decided")),
        "mark_to_fill": _diff(filled_ms, mark_observed),
    }


def summarize(rows: Iterable[tuple[str, dict[str, int | None]]]) -> dict[str, dict[str, Any]]:
    """``{chain: {"exits": n, segment: {"n", "median_ms", "p90_ms"}}}``."""
    acc: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    counts: dict[str, int] = defaultdict(int)
    for chain, segments in rows:
        counts[chain] += 1
        for name, value in segments.items():
            if value is not None:
                acc[chain][name].append(int(value))
    out: dict[str, dict[str, Any]] = {}
    for chain in sorted(counts):
        table: dict[str, Any] = {"exits": counts[chain]}
        for name in SEGMENTS:
            values = acc[chain].get(name, [])
            table[name] = {
                "n": len(values),
                "median_ms": percentile(values, 0.5),
                "p90_ms": percentile(values, 0.9),
            }
        out[chain] = table
    return out


def report(
    conn: sqlite3.Connection,
    *,
    since_ms: int | None = None,
    until_ms: int | None = None,
    chains: Iterable[str] | None = None,
    include_shadow: bool = False,
    reasons: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Median/p90 of every segment per chain, from ``exit_timing`` + ``orders``.

    ``reasons`` keeps only exits whose first attempt's reason starts with one of them
    (``["stop_loss", "emergency_loss"]`` for the journal's question).
    """
    attempts = load_attempts(
        conn, since_ms=since_ms, until_ms=until_ms, chains=chains, include_shadow=include_shadow
    )
    exits = exits_from_attempts(attempts)
    if reasons:
        prefixes = tuple(reasons)
        exits = [e for e in exits if str(e["attempts"][0].get("reason") or "").startswith(prefixes)]
    order_ids = sorted({str(e["sent"]["order_id"]) for e in exits if e["sent"] and e["sent"].get("order_id")})
    orders: dict[str, dict[str, Any]] = {}
    for start in range(0, len(order_ids), 500):
        chunk = order_ids[start:start + 500]
        marks = ",".join("?" for _ in chunk)
        for row in _rows(
            conn, f"SELECT order_id, state, created_ms, updated_ms FROM orders WHERE order_id IN ({marks})", chunk
        ):
            orders[str(row["order_id"])] = row
    rows = []
    for exit_ in exits:
        order_id = exit_["sent"].get("order_id") if exit_["sent"] else None
        chain = str(exit_["attempts"][0].get("_chain") or "?")
        rows.append((chain, exit_segments(exit_, orders.get(str(order_id)) if order_id else None)))
    return {
        "source": "exit_timing",
        "attempts": len(attempts),
        "exits": len(exits),
        "unsent_exits": sum(1 for e in exits if e["sent"] is None),
        "by_chain": summarize(rows),
    }


# --------------------------------------------------------------------------------------
# reconstruction for exits that predate exit_timing
# --------------------------------------------------------------------------------------


def legacy_stop_timelines(
    conn: sqlite3.Connection,
    *,
    since_ms: int | None = None,
    chains: Iterable[str] | None = None,
    threshold_pct: float = -30.0,
) -> list[dict[str, Any]]:
    """Per closed LIVE stop/emergency exit: tape cross -> our mark -> decision -> order -> fill.

    * ``t_tape``: first swap on the tape at or below ``entry * (1 + threshold)`` after the
      position opened, excluding our own transactions (a sell of ours is not the market).
    * ``t_mark``: first accepted mark (``position_marks``) at or below the threshold, and
      ``t_prev_mark`` the mark before it. Marks were only recorded from 2026-09-30.
    * ``t_decide``: the first ``quote_decision`` for a stop/emergency reason; ``t_order``
      the ``created_ms`` of the order the first ``exit_submitted`` names (built after the
      wallet read and ``min_out``), ``t_submitted`` that event, ``t_fill`` the order row's
      ``updated_ms`` once ``filled`` (when reconcile saw it, not the block time).

    Only indexed reads: swaps by (chain, token, ts), marks by position, events by subject.
    """
    sql = (
        "SELECT position_id, chain, token, opened_ms, closed_ms, exit_reason, entry_price_usd "
        "FROM positions WHERE mode IN ('live','canary') AND closed_ms IS NOT NULL "
        "AND (exit_reason LIKE 'stop_loss%' OR exit_reason LIKE 'emergency_loss%')"
    )
    params: list[Any] = []
    if since_ms is not None:
        sql += " AND opened_ms >= ?"
        params.append(int(since_ms))
    wanted = {c.lower() for c in chains} if chains else None
    out: list[dict[str, Any]] = []
    for p in _rows(conn, sql, params):
        if wanted is not None and str(p["chain"]).lower() not in wanted:
            continue
        try:
            entry = float(p["entry_price_usd"])
        except (TypeError, ValueError):
            continue
        if entry <= 0:
            continue
        line = entry * (1 + threshold_pct / 100.0)
        chain, token, pid = p["chain"], p["token"], p["position_id"]
        ours = {
            r["tx_hash"] for r in _rows(
                conn, "SELECT tx_hash FROM orders WHERE chain=? AND token=? AND tx_hash IS NOT NULL",
                (chain, token),
            )
        }
        t_tape = None
        for s in _rows(
            conn,
            "SELECT ts_ms, tx, CAST(price_usd AS REAL) AS px FROM swaps WHERE chain=? AND token=? "
            "AND ts_ms > ? AND ts_ms <= ? ORDER BY ts_ms",
            (chain, token, p["opened_ms"], int(p["closed_ms"]) + 600_000),
        ):
            if s["tx"] in ours or s["px"] is None or s["px"] <= 0:
                continue
            if s["px"] <= line:
                t_tape = int(s["ts_ms"])
                break
        t_mark = t_prev_mark = None
        previous = None
        for m in _rows(
            conn, "SELECT ts_ms, return_pct FROM position_marks WHERE position_id=? ORDER BY ts_ms", (pid,)
        ):
            if m["return_pct"] is not None and float(m["return_pct"]) <= threshold_pct:
                t_mark, t_prev_mark = int(m["ts_ms"]), previous
                break
            previous = int(m["ts_ms"])
        t_decide = t_submitted = order_id = None
        failures = blind = 0
        for e in _rows(
            conn,
            "SELECT ts_ms, payload FROM events WHERE subject=? AND id > 0 AND ts_ms BETWEEN ? AND ? "
            "AND kind IN ('system','protection.triggered') ORDER BY id",
            (token, p["opened_ms"], int(p["closed_ms"]) + 120_000),
        ):
            try:
                d = json.loads(e["payload"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(d, dict) or d.get("position_id") != pid:
                continue
            name = d.get("event")
            is_stop = str(d.get("reason") or "").startswith(STOP_PREFIXES)
            if name == "protection_blind" and t_decide is None and (t_tape is None or e["ts_ms"] >= t_tape):
                blind += 1
            elif name == "exit_failed" and t_submitted is None:
                failures += 1
            elif name == "quote_decision" and is_stop and t_decide is None:
                t_decide = int(e["ts_ms"])
            elif name == "exit_submitted" and is_stop and t_submitted is None:
                t_submitted, order_id = int(e["ts_ms"]), d.get("order_id")
        order = None
        if order_id:
            found = _rows(conn, "SELECT created_ms, updated_ms, state FROM orders WHERE order_id=?", (order_id,))
            order = found[0] if found else None
        out.append({
            "chain": chain,
            "position_id": pid,
            "exit_reason": p["exit_reason"],
            "t_tape": t_tape,
            "t_prev_mark": t_prev_mark,
            "t_mark": t_mark,
            "t_decide": t_decide,
            "t_order": _int(order["created_ms"]) if order else None,
            "t_submitted": t_submitted,
            "t_fill": _int(order["updated_ms"]) if order and order["state"] == "filled" else None,
            "t_close": _int(p["closed_ms"]),
            "exit_failures_before_send": failures,
            "blind_events_after_tape": blind,
        })
    return out


LEGACY_SEGMENTS: tuple[tuple[str, str, str], ...] = (
    ("tape_to_close", "t_tape", "t_close"),
    ("tape_to_mark", "t_tape", "t_mark"),
    ("prev_mark_to_mark", "t_prev_mark", "t_mark"),
    ("mark_to_decision", "t_mark", "t_decide"),
    ("decision_to_order", "t_decide", "t_order"),
    ("order_to_submitted", "t_order", "t_submitted"),
    ("submitted_to_fill", "t_submitted", "t_fill"),
)


def summarize_legacy(timelines: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per chain, over exits where the tape crossed BEFORE our close (the journal's set)."""
    out: dict[str, dict[str, Any]] = {}
    for chain in sorted({t["chain"] for t in timelines}):
        late = [t for t in timelines
                if t["chain"] == chain and t["t_tape"] and t["t_close"] and t["t_close"] > t["t_tape"]]
        table: dict[str, Any] = {
            "stop_exits": sum(1 for t in timelines if t["chain"] == chain),
            "tape_crossed_before_close": len(late),
            "blind_after_tape": sum(1 for t in late if t["blind_events_after_tape"]),
            "failed_before_send": sum(1 for t in late if t["exit_failures_before_send"]),
        }
        for name, start, end in LEGACY_SEGMENTS:
            values = [t[end] - t[start] for t in late if t.get(start) and t.get(end)]
            table[name] = {
                "n": len(values),
                "median_ms": percentile(values, 0.5),
                "p90_ms": percentile(values, 0.9),
            }
        out[chain] = table
    return out


# --------------------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------------------


def _table(title: str, by_chain: dict[str, dict[str, Any]], names: Sequence[str]) -> str:
    lines = [title]
    for chain, table in by_chain.items():
        head = ", ".join(f"{k}={v}" for k, v in table.items() if not isinstance(v, dict))
        lines.append(f"  {chain}: {head}")
        for name in names:
            cell = table.get(name) or {}
            if not cell.get("n"):
                continue
            med, p90 = cell["median_ms"], cell["p90_ms"]
            lines.append(f"    {name:24s} n={cell['n']:4d}  median {med / 1000:8.1f}s  p90 {p90 / 1000:8.1f}s")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - operator entry point
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="path to kaiba.db (opened read-only)")
    parser.add_argument("--since-days", type=float, default=30.0)
    parser.add_argument("--chain", action="append", help="repeatable; default all")
    parser.add_argument("--stops-only", action="store_true", help="stop_loss/emergency_loss exits only")
    parser.add_argument("--legacy", action="store_true", help="also reconstruct pre-instrumentation stops")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=30)
    since = int(time.time() * 1000 - args.since_days * 86_400_000)
    result: dict[str, Any] = {
        "timed": report(conn, since_ms=since, chains=args.chain,
                        reasons=STOP_PREFIXES if args.stops_only else None),
    }
    if args.legacy:
        result["legacy"] = summarize_legacy(legacy_stop_timelines(conn, since_ms=since, chains=args.chain))
    if args.json:
        print(json.dumps(result, indent=1, default=str))
        return 0
    timed = result["timed"]
    print(_table(f"exit_timing: {timed['attempts']} attempts, {timed['exits']} exits "
                 f"({timed['unsent_exits']} never sent)", timed["by_chain"], SEGMENTS))
    if args.legacy:
        print(_table("reconstructed stop/emergency exits (tape crossed before close)",
                     result["legacy"], [n for n, _, _ in LEGACY_SEGMENTS]))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
