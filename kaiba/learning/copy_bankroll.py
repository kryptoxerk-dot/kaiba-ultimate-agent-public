"""Finite cash replay over one chain's timestamped copy-trade research rows."""

from __future__ import annotations

import argparse
import heapq
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

CHAINS = ("sol", "robinhood", "elysium")
MAX_ROWS = 5_000
MAX_BYTES = 3 * 1024 * 1024
TOKEN_ID = re.compile(r"[0-9a-f]{64}")
BPS = 10_000


@dataclass(frozen=True)
class CopyObservation:
    event_id: int
    chain: str
    asset_id: str  # opaque digest of the chain and exact contract or mint, never a ticker
    signal_ms: int
    entry_ms: int | None = None
    exit_ms: int | None = None
    gross_proceeds_atoms: int | None = None  # before either leg's cost, at the replay ticket size


def _replay(
    rows: list[CopyObservation], *, bankroll: int, ticket: int, side_bps: int,
) -> tuple[dict, Counter]:
    cash = minimum = bankroll
    scheduled: list[tuple[int, int, int, str]] = []
    profits: Counter = Counter()
    counts: Counter = Counter()

    def settle_before(clock: int) -> None:
        nonlocal cash
        while scheduled and scheduled[0][0] < clock:
            _, _, proceeds, asset = heapq.heappop(scheduled)
            cash += proceeds
            profits[asset] += proceeds - ticket
            counts["settled"] += 1

    entries = sorted((r for r in rows if r.entry_ms is not None),
                     key=lambda r: (r.entry_ms, r.signal_ms, r.event_id))
    for row in entries:
        settle_before(row.entry_ms)
        if cash < ticket:
            counts["cash_skipped"] += 1
            continue
        cash -= ticket
        minimum = min(cash, minimum)
        counts["admitted"] += 1
        if row.exit_ms is None:
            counts["unresolved"] += 1
            continue
        # The all-in ticket buys less at an adverse entry price, then sells at an adverse exit.
        proceeds = row.gross_proceeds_atoms * (BPS - side_bps) // (BPS + side_bps)
        heapq.heappush(scheduled, (row.exit_ms, row.event_id, proceeds, row.asset_id))

    if scheduled:
        settle_before(max(row[0] for row in scheduled) + 1)
    realized = sum(profits.values())
    unresolved = counts["unresolved"] * ticket
    assert cash + unresolved == bankroll + realized
    return ({"side_cost_bps": side_bps, "opportunities": len(rows), "no_entry": len(rows) - len(entries),
             "admitted": counts["admitted"], "settled": counts["settled"], "cash_skipped": counts["cash_skipped"],
             "unresolved": counts["unresolved"], "unresolved_basis_atoms": unresolved,
             "free_cash_atoms": cash, "min_cash_atoms": minimum,
             "settled_pnl_atoms": realized if counts["settled"] else None}, profits)


def audit(
    rows: list[CopyObservation], *, chain: str, bankroll_atoms: int, ticket_atoms: int, as_of_ms: int,
    base_side_bps: int = 300, stress_side_bps: int = 1000,
) -> dict:
    """Compare base, stressed, and stressed-without-best-asset cash paths."""
    if (chain not in CHAINS or len(rows) > MAX_ROWS
            or any(type(x) is not int for x in (bankroll_atoms, ticket_atoms, as_of_ms, base_side_bps, stress_side_bps))
            or not 0 < ticket_atoms <= bankroll_atoms or as_of_ms <= 0
            or not 0 <= base_side_bps <= stress_side_bps < BPS):
        raise ValueError("invalid_replay_scope")
    seen = set()
    for row in rows:
        if (row.chain != chain or not isinstance(row.asset_id, str) or not TOKEN_ID.fullmatch(row.asset_id)
                or type(row.event_id) is not int or row.event_id in seen
                or type(row.signal_ms) is not int or not 0 < row.signal_ms <= as_of_ms):
            raise ValueError("invalid_observation_identity")
        seen.add(row.event_id)
        if row.entry_ms is not None and (type(row.entry_ms) is not int or not row.signal_ms < row.entry_ms <= as_of_ms):
            raise ValueError("invalid_entry_clock")
        if ((row.exit_ms is None) != (row.gross_proceeds_atoms is None)
                or (row.exit_ms is not None and (row.entry_ms is None or type(row.exit_ms) is not int
                                                or not row.entry_ms < row.exit_ms <= as_of_ms
                                                or type(row.gross_proceeds_atoms) is not int
                                                or row.gross_proceeds_atoms < 0))):
            raise ValueError("invalid_exit_evidence")

    base, _ = _replay(rows, bankroll=bankroll_atoms, ticket=ticket_atoms, side_bps=base_side_bps)
    stress, profits = _replay(rows, bankroll=bankroll_atoms, ticket=ticket_atoms, side_bps=stress_side_bps)
    best = max(profits, key=lambda token: (profits[token], token), default=None)
    joint = None
    if best is not None and profits[best] > 0:
        remaining = [row for row in rows if row.asset_id != best]
        joint, _ = _replay(remaining, bankroll=bankroll_atoms, ticket=ticket_atoms, side_bps=stress_side_bps)
        joint.update(removed_signals=len(rows) - len(remaining), removed_original_pnl_atoms=profits[best])
    return {"schema": "kaiba.copy-bankroll/1", "chain": chain, "as_of_ms": as_of_ms,
            "bankroll_atoms": bankroll_atoms, "ticket_atoms": ticket_atoms, "base": base, "stress": stress,
            "stress_without_best": joint, "financial_execution": False, "promotion_authority": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path)
    parser.add_argument("--chain", choices=CHAINS, required=True)
    parser.add_argument("--bankroll-atoms", type=int, required=True)
    parser.add_argument("--ticket-atoms", type=int, required=True)
    parser.add_argument("--as-of-ms", type=int, required=True)
    args = parser.parse_args()
    try:
        with args.observations.open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("observation_file_too_large")
        data = json.loads(raw)
        if not isinstance(data, list) or len(data) > MAX_ROWS:
            raise ValueError("invalid_observation_rows")
        report = audit([CopyObservation(**row) for row in data], chain=args.chain, bankroll_atoms=args.bankroll_atoms,
                       ticket_atoms=args.ticket_atoms, as_of_ms=args.as_of_ms)
    except (OSError, TypeError, ValueError) as exc:
        parser.exit(2, f"copy replay refused ({type(exc).__name__})\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
