"""Compare causal entry and exit policies on retained paper-position marks."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import mean

DELAY = 12_000
FILL_WINDOW = 18_000
MAX_GAP = 30_000
ENTRY_CUTOFF = 90_000
EXIT_AT = 180_000
POLICIES = ("immediate_hold", "immediate_stop", "reclaim_hold", "reclaim_stop")


@dataclass(frozen=True)
class Mark:
    seen: int
    price: float
    liquidity: float


def marks(rows: list[dict], opened: int) -> list[Mark]:
    """Use the first identified pool and only prices observable in real time."""
    result: list[Mark] = []
    pool = None
    last_observed = -1
    for row in sorted(rows, key=lambda x: (x["upd"], x["obs"])):
        observed, seen = row["obs"], row["upd"]
        venue_pool = row.get("venue"), row.get("pool")
        if (
            not all(venue_pool)
            or not opened <= observed <= seen <= opened + EXIT_AT + DELAY + FILL_WINDOW
            or seen - observed > 15_000
            or observed <= last_observed
            or (pool is not None and venue_pool != pool)
        ):
            continue
        try:
            price, liquidity = float(row["px"]), float(row["liq"])
        except (KeyError, ValueError, TypeError):
            continue
        if not all(math.isfinite(v) and v > 0 for v in (price, liquidity)):
            continue
        pool = venue_pool
        last_observed = observed
        result.append(Mark(seen, price, liquidity))
    return result


def fill(tape: list[Mark], trigger: int) -> Mark | None:
    return next((m for m in tape if trigger + DELAY <= m.seen <= trigger + DELAY + FILL_WINDOW), None)


def reclaim(tape: list[Mark], first: Mark, cutoff: int) -> tuple[str, Mark | None]:
    low = first.price
    last = first.seen
    for mark in tape:
        if mark.seen <= first.seen:
            continue
        if mark.seen - last > MAX_GAP:
            break
        if mark.seen > cutoff:
            return "cash", None
        last = mark.seen
        low = min(low, mark.price)
        if low <= first.price * 0.88 and mark.price >= low * 1.05:
            buy = fill(tape, mark.seen)
            return ("filled", buy) if buy else ("unknown", None)
    return ("cash", None) if last == cutoff else ("unknown", None)


def impact(amount_usd: float, liquidity: float) -> float:
    return amount_usd / (liquidity + amount_usd)


def liquidation_return(buy: Mark, sell: Mark, stake_usd: float, cost: float) -> float:
    gross = stake_usd * (1 - cost - impact(stake_usd, buy.liquidity)) * sell.price / buy.price
    if gross <= 0:
        return -1.0
    net = gross * (1 - cost - impact(gross, sell.liquidity))
    return max(-1.0, net / stake_usd - 1)


def close(
    tape: list[Mark], buy: Mark, deadline: int, stake_usd: float, cost: float, early: bool,
) -> float | None:
    breach = None
    last = buy.seen
    for mark in tape:
        if mark.seen <= buy.seen:
            continue
        if mark.seen - last > MAX_GAP:
            return None
        if mark.seen >= deadline:
            break
        last = mark.seen
        if not early:
            continue
        if liquidation_return(buy, mark, stake_usd, cost) > -0.12:
            breach = None
        elif breach is None:
            breach = mark.seen
        elif mark.seen - breach >= 10_000:
            sell = fill(tape, mark.seen)
            return liquidation_return(buy, sell, stake_usd, cost) if sell else None
    sell = fill(tape, deadline)
    if sell and 0 <= deadline - last <= MAX_GAP:
        return liquidation_return(buy, sell, stake_usd, cost)
    return None


def evaluate(item: dict, tape: list[Mark], stake: float, bps: int) -> dict[str, tuple[str, float | None]]:
    opened = item["opened"]
    deadline = opened + EXIT_AT
    first = fill(tape, opened)
    fx = item["fx"]
    result = dict.fromkeys(POLICIES, ("unknown", None))
    if first is None or not math.isfinite(fx) or fx <= 0:
        return result
    status, delayed = reclaim(tape, first, opened + ENTRY_CUTOFF)
    for name, buy in (("immediate", first), ("reclaim", delayed)):
        for exit_name in ("hold", "stop"):
            key = f"{name}_{exit_name}"
            if buy is None:
                if status == "cash":
                    result[key] = ("cash", 0.0)
                continue
            pnl = close(tape, buy, deadline, stake * fx, bps / 10_000, exit_name == "stop")
            if pnl is not None:
                result[key] = ("filled", pnl)
    return result


def summarize(rows: list[dict]) -> dict:
    common = [r for r in rows if all(r[p][1] is not None for p in POLICIES)]
    cut = len(common) * 3 // 5

    def average(group: list[dict], policy: str) -> float | None:
        return mean(r[policy][1] for r in group) if group else None

    return {
        "opportunities": len(rows),
        "common_complete": len(common),
        "early_n": cut,
        "late_n": len(common) - cut,
        "policies": {
            p: {
                "priced": sum(r[p][0] == "filled" for r in rows),
                "cash": sum(r[p][0] == "cash" for r in rows),
                "unknown": sum(r[p][0] == "unknown" for r in rows),
                "common_mean": average(common, p),
                "early_mean": average(common[:cut], p),
                "late_mean": average(common[cut:], p),
                "unknown_at_full_loss": mean(-1.0 if r[p][1] is None else r[p][1] for r in rows)
                if rows else None,
            }
            for p in POLICIES
        },
    }


def run(items: list[dict]) -> dict:
    ordered = sorted(items, key=lambda item: (item["opened"], item["position"]))
    first_by_mint = {}
    for item in ordered:
        first_by_mint.setdefault(item["mint"], item)
    population = list(first_by_mint.values())
    tapes = [marks(item["tape"], item["opened"]) for item in population]
    return {
        "paper_positions": len(items),
        "unique_mints": len(population),
        "no_pool_marks": sum(not tape for tape in tapes),
        "scenarios": {
            f"{stake}sol_{bps}bps_per_side": summarize(
                [evaluate(item, tape, stake, bps) for item, tape in zip(population, tapes, strict=True)]
            )
            for stake in (0.1, 0.3)
            for bps in (275, 375)
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Local retained replay bundle")
    args = parser.parse_args()
    print(json.dumps(run(json.loads(args.input.read_bytes())), allow_nan=False, indent=2))


if __name__ == "__main__":
    main()
