"""Replay an isolated LP paper arm with fixed capital and reserved costs."""
from __future__ import annotations

import argparse
import heapq
import json
import sqlite3
import time
from collections import Counter
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path

VERSION = "finite-lp-paper/1"
LAMPORTS = 1_000_000_000
BANKROLL = 3 * LAMPORTS
TICKET = LAMPORTS
RESERVED = TICKET + 400_000_000
MAX_ROWS = 3_000
MAX_DB_BYTES = 2 * 1024 * 1024


def _native(value: object) -> int:
    if type(value) not in (float, int):
        raise ValueError("missing_or_invalid_paper_cashflow")
    sol = Decimal(str(value))
    if not sol.is_finite() or abs(sol) > 100:
        raise ValueError("invalid_paper_cashflow")
    return int((sol * LAMPORTS).to_integral_value(rounding=ROUND_FLOOR))


def replay(rows: list[dict], *, as_of_ms: int) -> dict:
    """Reserve at each plan and release only on an earlier priced terminal row."""
    if type(as_of_ms) is not int or as_of_ms <= 0 or len(rows) > MAX_ROWS:
        raise ValueError("invalid_replay_scope")
    available = BANKROLL
    unsettled: list[tuple[int, int, int]] = []
    counts = Counter()
    pnl = all_pnl = peak_locked = 0
    seen = set()

    def settle_before(cutoff: int) -> None:
        nonlocal available, pnl
        while unsettled and unsettled[0][0] < cutoff:
            _, _, net = heapq.heappop(unsettled)
            available += RESERVED + net
            pnl += net
            counts["settled"] += 1

    for row in sorted(rows, key=lambda r: (r["planned_ms"], r["id"])):
        ident, planned = row["id"], row["planned_ms"]
        terminal, status = row["at_ms"], row["arm_status"]
        if type(ident) is not int or ident in seen or type(planned) is not int or not 0 < planned <= as_of_ms:
            raise ValueError("invalid_plan_identity_or_clock")
        seen.add(ident)
        if status not in (None, "closed", "missed", "stale") or (status is None) != (terminal is None):
            raise ValueError("unsupported_terminal_status")
        if terminal is not None and (type(terminal) is not int or not planned <= terminal <= as_of_ms):
            raise ValueError("invalid_terminal_clock")
        net = _native(row["net_sol"]) if status in ("closed", "missed") else None
        if net is not None:
            if net < -RESERVED or (status == "missed" and net > 0):
                raise ValueError("cashflow_exceeds_declared_reserve_or_missed_credit")
            tx, rent = _native(row["tx_sol"]), _native(row["rent_sol"])
            if min(tx, rent) < 0 or tx + rent > RESERVED - TICKET:
                raise ValueError("paper_costs_exceed_reserve")
            all_pnl += net
            counts["all_priced"] += 1
            counts["all_closed"] += status == "closed"
            if status == "closed" and row["exit_reason"] == "idle_above":
                counts["idle_above_closed"] += 1
                counts["idle_above_zero_fee"] += row["fee_pct"] == 0
        settle_before(planned)
        if available < RESERVED:
            counts["cash_skipped"] += 1
            continue
        available -= RESERVED
        counts["admitted"] += 1
        if net is not None:
            heapq.heappush(unsettled, (terminal, ident, net))
            counts["admitted_missed"] += status == "missed"
        elif status == "stale" or row["observation_status"] != "open":
            counts["unpriced_reserved"] += 1
        else:
            counts["pending_reserved"] += 1
        peak_locked = max(peak_locked, len(unsettled) + counts["unpriced_reserved"] + counts["pending_reserved"])

    settle_before(as_of_ms + 1)
    reserved = RESERVED * (counts["unpriced_reserved"] + counts["pending_reserved"])
    assert available + reserved == BANKROLL + pnl
    return {"version": VERSION, "as_of_ms": as_of_ms, "bankroll_lamports": BANKROLL,
            "ticket_lamports": TICKET, "reserved_per_plan_lamports": RESERVED,
            "opportunities": len(rows), "admitted": counts["admitted"], "cash_skipped": counts["cash_skipped"],
            "settled": counts["settled"], "admitted_missed": counts["admitted_missed"],
            "pending_reserved": counts["pending_reserved"], "unpriced_reserved": counts["unpriced_reserved"],
            "peak_concurrent_reservations": peak_locked, "free_cash_lamports": available,
            "still_reserved_lamports": reserved, "settled_pnl_lamports": pnl if counts["settled"] else None,
            "unconstrained_priced": counts["all_priced"],
            "unconstrained_closed": counts["all_closed"],
            "unconstrained_idle_above_closed": counts["idle_above_closed"],
            "unconstrained_idle_above_zero_fee": counts["idle_above_zero_fee"],
            "unconstrained_pnl_lamports": all_pnl if counts["all_priced"] else None,
            "financial_execution": False, "promotion_authority": False}


def read_arm(path: Path, arm: str, *, since_ms: int, as_of_ms: int) -> list[dict]:
    if path.name != "lp_paper.db" or path.resolve() != path.absolute() or not path.is_file():
        raise ValueError("expected_isolated_paper_db")
    wal = path.with_name(path.name + "-wal")
    if path.stat().st_size > MAX_DB_BYTES or (wal.exists() and wal.stat().st_size > MAX_DB_BYTES):
        raise ValueError("paper_db_size_bound")
    if not 0 < since_ms <= as_of_ms or as_of_ms - since_ms > 13 * 86_400_000:
        raise ValueError("invalid_read_window")
    with_timeout = time.monotonic() + 3
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.15, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.set_progress_handler(lambda: int(time.monotonic() > with_timeout), 500)
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT o.id,o.planned_ms,o.status observation_status,r.status arm_status,r.at_ms,r.net_sol, "
            "r.tx_sol,r.rent_sol,r.exit_reason,r.fee_pct "
            "FROM observations o INDEXED BY idx_obs_time LEFT JOIN arm_results r "
            "ON r.obs_id=o.id AND r.arm=? AND r.at_ms<=? "
            "WHERE o.version='lp-paper/1' AND o.planned_ms BETWEEN ? AND ? "
            "ORDER BY o.planned_ms,o.id LIMIT ?",
            (arm, as_of_ms, since_ms, as_of_ms, MAX_ROWS + 1),
        ).fetchall()
    finally:
        conn.close()
    if len(rows) > MAX_ROWS:
        raise ValueError("paper_row_bound")
    return [dict(row) for row in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("arm", choices=("r20_60", "w30_6h", "b40_6h", "f40_6h", "d2x_24h"))
    parser.add_argument("--since-ms", type=int, required=True)
    args = parser.parse_args(argv)
    now = time.time_ns() // 1_000_000
    try:
        rows = read_arm(args.database.absolute(), args.arm, since_ms=args.since_ms, as_of_ms=now)
        report = replay(rows, as_of_ms=now)
    except (OSError, ValueError, InvalidOperation, sqlite3.Error):
        print(json.dumps({"version": VERSION, "status": "unavailable", "financial_execution": False}))
        return 2
    print(json.dumps({"arm": args.arm, **report}, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
