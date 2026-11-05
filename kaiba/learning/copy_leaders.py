"""Pick the sol wallets the live copy lane follows: profitable fast traders, from our own tape.

OWNER 2026-10-07: "Copy trade on live" -- after the backtest of the same day (journal, subject
``wallets``): of 4,000 active sol tape wallets, 19 were profitable scalpers over a week
(>= 8 closed round trips, profitable, >= 55% of round trips won); their median hold is 13 s.
Copying their buys within 10 s and exiting at 60 s was +6.5% GROSS and -0.2% net of 3.25% per
leg; at 30 s it was -3.3% net. So the lane copies them fast and leaves at a fixed time
(``trusted-copy`` param ``max_hold_s``, enforced by the ops job ``copy_time_exit``).

WHERE THE LIST LIVES. A chosen wallet gets ``wallets.cohort = 'trusted_copy'`` on SOL, which is
what ``lanes.trusted_copy`` copies, what ``ingest/wallet_stream_sol`` subscribes to in real
time (block -> push p50 1.2 s) and what ``scanner._copy_flow_work`` feeds the scanner from.
Every row this module writes carries ``meta_json.copy_leader.auto = true``; only such rows are
ever demoted, so a wallet the owner put in ``trusted_copy`` by hand is never touched. SOL ONLY:
on robinhood ``trusted_copy`` is the owner's curated list (``lanes.TRUSTED_COPY_CHAINS``) and
this module refuses any other chain.

READS follow the AGENTS.md database rule: one short read-only query at a time through
``signal_audit.Reader`` (WAL checked), per wallet by ``idx_swaps_wallet``, never a scan.
"""

from __future__ import annotations

import collections
import statistics
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from kaiba.core.db import jdump, jload
from kaiba.core.schemas import now_ms

CHAIN = "sol"
COHORT = "trusted_copy"
#: Demoted auto leaders go back to this cohort (the screened-but-unused default).
DEMOTED_COHORT = "tracked"
WINDOW_DAYS = 7
MIN_TRIPS = 8
MIN_WIN = 0.55
POOL = 4000
MAX_LEADERS = 25
MAX_ROWS_PER_WALLET = 20_000


@dataclass(frozen=True)
class Leader:
    wallet: str
    ret: float          # realised return on cost over the window (tape usd values)
    win: float          # share of closed round trips that made money
    trips: int
    hold_s: float       # median first-buy -> first-sell, seconds


def _i(x: Any) -> int | None:
    try:
        return int(str(x))
    except (TypeError, ValueError):
        return None


def _f(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def score_wallet(rows: list[tuple[Any, ...]], lo: int, hi: int) -> Leader | None:
    """``rows`` = (token, side, amount_token, usd_value, ts_ms). None if not a qualifying scalper."""
    tk: dict[str, list[Any]] = {}
    for tok, side, a, u, ts in rows:
        a, u, ts = _i(a), _f(u), _i(ts)
        if not a or a <= 0 or u is None or not 0 < u < 5e6 or ts is None or not lo <= ts < hi:
            continue
        t = tk.setdefault(str(tok), [None, 0.0, 0, None, 0.0, 0])
        if side == "buy":
            t[0] = ts if t[0] is None else min(t[0], ts)
            t[1] += u
            t[2] += a
        elif side == "sell":
            t[3] = ts if t[3] is None else min(t[3], ts)
            t[4] += u
            t[5] += a
    closed = [v for v in tk.values() if v[0] is not None and v[3] is not None and v[3] > v[0] and v[2] > 0 and v[5] > 0]
    if len(closed) < MIN_TRIPS:
        return None
    pnl = cost = 0.0
    wins = 0
    for v in closed:
        c = v[1] * min(1.0, v[5] / v[2])
        p = v[4] * min(1.0, v[2] / v[5])
        pnl += p - c
        cost += c
        wins += p > c
    if cost <= 0 or pnl <= 0 or wins / len(closed) < MIN_WIN:
        return None
    hold = statistics.median((v[3] - v[0]) / 1000.0 for v in closed)
    return Leader("", pnl / cost, wins / len(closed), len(closed), hold)


def select_leaders(
    q: Callable[[str, tuple[Any, ...]], list[tuple[Any, ...]]],
    *,
    now: int | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] = time.time,
) -> tuple[list[Leader], dict[str, Any]]:
    """The best :data:`MAX_LEADERS` qualifying sol wallets over the last :data:`WINDOW_DAYS`."""
    now = now if now is not None else now_ms()
    lo = now - WINDOW_DAYS * 86_400_000
    active = collections.Counter(
        str(w) for (w,) in q("SELECT wallet FROM seed_buyers WHERE chain = ?", (CHAIN,)))
    pool = [w for w, n in active.most_common(POOL) if n >= 3]
    found: list[Leader] = []
    scanned = 0
    for w in pool:
        if deadline is not None and clock() >= deadline:
            break
        rows = q(
            "SELECT token, side, amount_token, usd_value, ts_ms FROM swaps INDEXED BY idx_swaps_wallet "
            "WHERE chain = ? AND wallet = ? AND ts_ms >= ? ORDER BY ts_ms LIMIT ?",
            (CHAIN, w, lo, MAX_ROWS_PER_WALLET),
        )
        scanned += 1
        got = score_wallet(rows, lo, now)
        if got is not None:
            found.append(Leader(w, got.ret, got.win, got.trips, got.hold_s))
    found.sort(key=lambda x: (-x.ret, -x.trips))
    return found[:MAX_LEADERS], {"pool": len(pool), "scanned": scanned, "qualified": len(found),
                                 "complete": scanned == len(pool)}


def apply_leaders(w: Any, leaders: list[Leader], *, chain: str = CHAIN, now: int | None = None,
                  complete: bool = True) -> dict[str, int]:
    """Put ``leaders`` in the sol ``trusted_copy`` cohort; demote auto leaders no longer chosen.

    A pass cut short by its deadline (``complete`` False) only ADDS: demoting on a partial
    view would drop wallets merely because their turn had not come.
    """
    if chain != CHAIN:
        raise ValueError(f"copy_leaders writes {CHAIN} only; {chain} trusted_copy is the owner's list")
    now = now if now is not None else now_ms()
    chosen = {x.wallet: x for x in leaders}
    out = {"added": 0, "kept": 0, "demoted": 0, "skipped_owner": 0}
    w.execute("BEGIN IMMEDIATE")
    try:
        current = {
            str(a): (str(c or ""), jload(m, {}) or {})
            for a, c, m in w.execute(
                "SELECT address, cohort, meta_json FROM wallets WHERE chain = ? AND address IN "
                f"({','.join('?' * len(chosen)) or 'NULL'}) OR (chain = ? AND cohort = ?)",
                (chain, *chosen, chain, COHORT),
            )
        }
        for addr, (cohort, meta) in current.items():
            auto = isinstance(meta, dict) and bool((meta.get("copy_leader") or {}).get("auto"))
            if addr not in chosen and cohort == COHORT and auto:
                meta["copy_leader"] = {**meta.get("copy_leader", {}), "demoted_ms": now}
                w.execute("UPDATE wallets SET cohort = ?, meta_json = ? WHERE chain = ? AND address = ?",
                          (DEMOTED_COHORT if complete else COHORT, jdump(meta), chain, addr))
                out["demoted"] += 1 if complete else 0
        for addr, x in chosen.items():
            cohort, meta = current.get(addr, (None, {}))
            meta = meta if isinstance(meta, dict) else {}
            if cohort == COHORT and not (meta.get("copy_leader") or {}).get("auto"):
                out["skipped_owner"] += 1
                continue
            meta["copy_leader"] = {"auto": True, "picked_ms": now, **{k: v for k, v in asdict(x).items() if k != "wallet"}}
            if addr in current:
                w.execute("UPDATE wallets SET cohort = ?, meta_json = ? WHERE chain = ? AND address = ?",
                          (COHORT, jdump(meta), chain, addr))
                out["kept" if cohort == COHORT else "added"] += 1
            else:
                w.execute(
                    "INSERT INTO wallets (chain, address, source, cohort, meta_json, first_seen_ms, last_seen_ms) "
                    "VALUES (?,?,?,?,?,?,?)", (chain, addr, "copy_leaders", COHORT, jdump(meta), now, now))
                out["added"] += 1
        w.execute("COMMIT")
    except Exception:
        w.execute("ROLLBACK")
        raise
    return out


__all__ = ["Leader", "score_wallet", "select_leaders", "apply_leaders", "CHAIN", "COHORT"]
