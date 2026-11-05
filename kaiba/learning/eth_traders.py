"""Grade Ethereum (Stockereum) traders on what they REALIZED, from our own tape.

Owner 2026-10-09: "gather wallets that made huge gains, top trader grade A B wallets". Input is the
``swaps`` rows ``ingest/stockereum.py`` writes (chain ``eth``): every buy and sell through the
platform's factory and router, in wei and token atoms.

Realized only: a token's sells are matched against the proportional share of its buy cost
(``cost x min(1, sold / bought)``); tokens never sold count for nothing, so an unrealized paper
gain cannot make anyone a top trader. Grades, DECLARED before any result was seen:

* **A**: realized >= 1.0 ETH, >= 5 closed round trips, >= 55% of them won, and the single best
  trade is under 60% of total profit (one lucky hit is not a trader);
* **B**: realized >= 0.25 ETH, >= 3 round trips, >= 50% won.

Creators (``kv stockereum:creator:<token>``) are labelled ``dev``: their gains include the dev buy
and fees, which a copier cannot get. Graded wallets are written to ``wallets`` (chain ``eth``) with
cohort ``research`` -- a list to study and forward-test, NEVER a copy list: on sol, last week's
winners were not the next week's (rank correlation +0.05, 2026-10-07/08).
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

CHAIN = "eth"
SOURCE = "stockereum:events"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
WEI = 10**18
GRADE_A = {"min_eth": 1.0, "min_trips": 5, "min_win": 0.55, "max_best_share": 0.60}
GRADE_B = {"min_eth": 0.25, "min_trips": 3, "min_win": 0.50}


@dataclass
class TraderStats:
    wallet: str
    realized_eth: float = 0.0
    cost_eth: float = 0.0
    trips: int = 0
    wins: int = 0
    tokens: int = 0
    best_eth: float = 0.0
    median_hold_s: float | None = None
    is_dev: bool = False
    grade: str | None = None
    holds: list[float] = field(default_factory=list, repr=False)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trips if self.trips else 0.0


def grade_of(s: TraderStats) -> str | None:
    if (s.realized_eth >= GRADE_A["min_eth"] and s.trips >= GRADE_A["min_trips"]
            and s.win_rate >= GRADE_A["min_win"] and s.best_eth < GRADE_A["max_best_share"] * s.realized_eth):
        return "A"
    if s.realized_eth >= GRADE_B["min_eth"] and s.trips >= GRADE_B["min_trips"] and s.win_rate >= GRADE_B["min_win"]:
        return "B"
    return None


def compute(rows: list[tuple[str, str, str, str, str, int]], devs: set[str]) -> dict[str, TraderStats]:
    """``rows`` = (wallet, token, side, amount_token, amount_native, ts_ms)."""
    per: dict[tuple[str, str], list[Any]] = defaultdict(lambda: [0, 0, 0, 0, None, None])
    for wallet, token, side, atoms, wei, ts in rows:
        try:
            a, w = int(atoms), int(wei)
        except (TypeError, ValueError):
            continue
        if a <= 0 or w < 0:
            continue
        p = per[(wallet, token)]
        if side == "buy":
            p[0] += a
            p[1] += w
            p[4] = ts if p[4] is None else min(p[4], ts)
        elif side == "sell":
            p[2] += a
            p[3] += w
            p[5] = ts if p[5] is None else min(p[5], ts)
    out: dict[str, TraderStats] = {}
    for (wallet, _token), (ba, bw, sa, sw, tb, ts) in per.items():
        s = out.setdefault(wallet, TraderStats(wallet, is_dev=wallet in devs))
        s.tokens += 1
        if ba <= 0 or sa <= 0:
            continue
        cost = bw * min(1.0, sa / ba) / WEI
        proceeds = sw * min(1.0, ba / sa) / WEI
        pnl = proceeds - cost
        s.realized_eth += pnl
        s.cost_eth += cost
        s.trips += 1
        s.wins += pnl > 0
        s.best_eth = max(s.best_eth, pnl)
        if tb is not None and ts is not None and ts >= tb:
            s.holds.append((ts - tb) / 1000)
    for s in out.values():
        if s.holds:
            h = sorted(s.holds)
            s.median_hold_s = h[len(h) // 2]
        s.grade = grade_of(s)
    return out


def run(conn: Any, *, write: bool = True, top: int = 25) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT wallet, token, side, amount_token, amount_native, ts_ms FROM swaps "
        "WHERE chain = ? AND source = ?", (CHAIN, SOURCE)).fetchall()
    # ONLY WETH-QUOTED launches are priced in ETH. MEASURED 2026-10-09, first run: stock-,
    # USDC- and memecoin-quoted pools (PEPE, SHIB, NVDAon ...) report the router amount in the
    # QUOTE token's units, which read as up to 219,971 "ETH". A token whose launch (and so
    # quote) we never saw is left out rather than guessed.
    devs: set[str] = set()
    eth_quoted: set[str] = set()
    for k, v in conn.execute("SELECT key, value FROM kv WHERE key LIKE 'stockereum:creator:%'"):
        try:
            d = json.loads(v)
        except (ValueError, TypeError):
            continue
        devs.add(str(d.get("creator")))
        if str(d.get("quote", "")).lower() == WETH:
            eth_quoted.add(str(k).split(":", 2)[2])
    priced = [tuple(r) for r in rows if r[1] in eth_quoted]
    stats = compute(priced, devs)
    graded = sorted((s for s in stats.values() if s.grade), key=lambda s: -s.realized_eth)
    huge = sorted(stats.values(), key=lambda s: -s.realized_eth)[:top]
    if write and graded:
        now = int(time.time() * 1000)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for s in graded:
                row = conn.execute("SELECT meta_json, cohort FROM wallets WHERE chain = ? AND address = ?",
                                   (CHAIN, s.wallet)).fetchone()
                meta = json.loads(row[0]) if row and row[0] else {}
                meta["eth_trader"] = {k: v for k, v in asdict(s).items() if k not in ("holds", "wallet")} | {
                    "win_rate": round(s.win_rate, 3), "scored_ms": now, "source": SOURCE}
                if row:
                    conn.execute("UPDATE wallets SET meta_json = ?, last_seen_ms = ? WHERE chain = ? AND address = ?",
                                 (json.dumps(meta), now, CHAIN, s.wallet))
                else:
                    conn.execute("INSERT INTO wallets (chain, address, source, cohort, meta_json, first_seen_ms, last_seen_ms) "
                                 "VALUES (?,?,?,?,?,?,?)", (CHAIN, s.wallet, "eth_traders", "research", json.dumps(meta), now, now))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {
        "trades": len(rows), "eth_quoted_trades": len(priced), "eth_quoted_tokens": len(eth_quoted), "wallets": len(stats), "devs": len(devs),
        "grade_A": sum(1 for s in graded if s.grade == "A"), "grade_B": sum(1 for s in graded if s.grade == "B"),
        "graded": [(s.wallet, s.grade, round(s.realized_eth, 3), s.trips, round(s.win_rate, 2), s.is_dev) for s in graded[:top]],
        "huge": [(s.wallet, round(s.realized_eth, 3), s.trips, round(s.win_rate, 2), s.is_dev, s.grade) for s in huge],
    }


__all__ = ["TraderStats", "compute", "grade_of", "run", "GRADE_A", "GRADE_B"]
