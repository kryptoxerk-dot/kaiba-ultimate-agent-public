"""One- and two-wallet paper comparisons on bounded, same-market swap prints."""

from __future__ import annotations

import argparse
import json
import re
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import mean, median

CHAINS = ("robinhood", "bsc", "hyperevm", "elysium", "eth", "base", "arc", "stable")
MAX_ROWS = 12_000
MAX_BYTES = 5_000_000
ADDRESS = re.compile(r"0x[0-9a-f]{40}")
DELAY_MS = 60_000
WAIT_MS = 300_000
CONFIRM_MS = 1_800_000


@dataclass(frozen=True)
class Swap:
    at_ms: int
    market: str
    token: str
    wallet: str
    tx: str
    side: str
    price: Decimal


@dataclass(frozen=True)
class Outcome:
    state: str
    gross_ratio: Decimal | None = None
    entry_ms: int | None = None
    exit_ms: int | None = None

    def net(self, side_bps: int) -> Decimal:
        return self.gross_ratio * (10_000 - side_bps) / (10_000 + side_bps) - 1


def _validated(rows: list[Swap], as_of_ms: int) -> list[Swap]:
    if len(rows) > MAX_ROWS or type(as_of_ms) is not int or as_of_ms <= 0:
        raise ValueError("paper_input_bound")
    markets: dict[str, str] = {}
    unique = {}
    for s in rows:
        if (type(s.at_ms) is not int or not 0 < s.at_ms <= as_of_ms or not ADDRESS.fullmatch(s.token)
                or not ADDRESS.fullmatch(s.wallet) or not s.tx or len(s.tx) > 128
                or not 0 < len(s.market) <= 128 or s.side not in ("buy", "sell")
                or not s.price.is_finite() or not Decimal("1e-30") <= s.price <= Decimal("1e12")):
            raise ValueError("invalid_paper_swap")
        if markets.setdefault(s.market, s.token) != s.token:
            raise ValueError("market_token_conflict")
        identity = (s.market, s.tx, s.wallet, s.side)
        earlier = unique.get(identity)
        if earlier is None or (s.at_ms, s.price) < (earlier.at_ms, earlier.price):
            unique[identity] = s
    return sorted(unique.values(), key=lambda x: (x.at_ms, x.tx, x.market, x.wallet, x.side))


def _seek(rows: list[Swap], target: int, until: int, exclude_tx: str) -> Swap | None:
    index = bisect_left(rows, target, key=lambda row: row.at_ms)
    for i in range(index, len(rows)):
        row = rows[i]
        if row.at_ms > min(target + WAIT_MS, until):
            break
        if row.tx != exclude_tx:
            return row
    return None


def _price(trigger: Swap, tape: dict, until: int, hold_ms: int) -> Outcome:
    entry_target = trigger.at_ms + DELAY_MS
    entry = _seek(tape[trigger.market, "buy"], entry_target, until, trigger.tx)
    if entry is None:
        return Outcome("censored_window" if entry_target + WAIT_MS > until else "no_entry_print")
    exit_target = entry.at_ms + hold_ms
    exit_ = _seek(tape[trigger.market, "sell"], exit_target, until, entry.tx)
    if exit_ is None:
        return Outcome("censored_window" if exit_target + WAIT_MS > until else "no_exit_print",
                       entry_ms=entry.at_ms)
    return Outcome("settled", exit_.price / entry.price, entry.at_ms, exit_.at_ms)


def _pct(value: Decimal) -> float:
    return float(round(value * 100, 5))


def _nominate(tape: dict, buys: list[Swap], start: int, split: int, hold_ms: int) -> list[dict]:
    profiles = defaultdict(list)
    seen = set()
    for buy in buys:
        key = (buy.wallet, buy.market)
        if buy.at_ms >= split or key in seen:
            continue
        seen.add(key)
        profiles[buy.wallet].append((buy, _price(buy, tape, split - 1, hold_ms)))
    midpoint = (start + split) // 2
    ranked = []
    for wallet, records in profiles.items():
        priced = [(s, o.net(1000)) for s, o in records if o.state == "settled"]
        if len(priced) < 8 or 2 * len(priced) < len(records) or len({s.token for s, _ in priced}) < 3:
            continue
        older = [p for s, p in priced if s.at_ms < midpoint]
        newer = [p for s, p in priced if s.at_ms >= midpoint]
        if not older or not newer or min(mean(older), mean(newer)) <= 0:
            continue
        by_token = defaultdict(Decimal)
        for s, profit in priced:
            by_token[s.token] += profit
        best = max(by_token, key=lambda token: (by_token[token], token))
        without = mean(p for s, p in priced if s.token != best)
        if without > 0:
            ranked.append((without, wallet, {"wallet": wallet, "settled": len(priced), "attempts": len(records),
                           "distinct_tokens": len(by_token), "stress_mean_pct": _pct(mean(older + newer)),
                           "stress_without_best_pct": _pct(without)}))
    return [result for _, _, result in sorted(ranked, key=lambda r: (-r[0], r[1]))[:6]]


def _summarize(results: dict[str, Outcome]) -> dict:
    settled = [r for r in results.values() if r.state == "settled"]
    base = [r.net(300) for r in settled]
    stress = [r.net(1000) for r in settled]
    return {"signals": len(results), "settled": len(settled),
            "no_entry": sum(r.state == "no_entry_print" for r in results.values()),
            "no_exit": sum(r.state == "no_exit_print" for r in results.values()),
            "censored": sum(r.state == "censored_window" for r in results.values()),
            "base_mean_pct": _pct(mean(base)) if base else None,
            "stress_mean_pct": _pct(mean(stress)) if stress else None,
            "stress_median_pct": _pct(median(stress)) if stress else None,
            "settled_sample": [{"market": market, "entry_ms": r.entry_ms, "exit_ms": r.exit_ms,
                                "stress_pct": _pct(r.net(1000))}
                               for market, r in results.items() if r.state == "settled"][:12]}


def _compare(tape: dict, buys: list[Swap], split: int, end: int, hold_ms: int, roster: set[str] | None) -> dict:
    first = {}
    second = {}
    first_results = {}
    second_results = {}
    for buy in buys:
        if buy.at_ms < split or (roster is not None and buy.wallet not in roster):
            continue
        lead = first.setdefault(buy.market, buy)
        if lead is buy:
            first_results[buy.market] = _price(buy, tape, end, hold_ms)
        elif (buy.market not in second and lead.wallet != buy.wallet and lead.tx != buy.tx
              and 0 < buy.at_ms - lead.at_ms <= CONFIRM_MS):
            second[buy.market] = buy
            second_results[buy.market] = _price(buy, tape, end, hold_ms)
    paired = sorted(k for k in second_results
                    if second_results[k].state == first_results[k].state == "settled")
    lift = [_pct(second_results[k].net(1000) - first_results[k].net(1000)) for k in paired]
    return {"one_wallet": _summarize(first_results), "two_wallets": _summarize(second_results),
            "paired_settled": len(paired), "paired_two_minus_one_stress_pp": mean(lift) if lift else None}


def paper(rows: list[Swap], *, chain: str, as_of_ms: int, hold_minutes: int = 30) -> dict:
    if chain not in CHAINS or hold_minutes not in (5, 30):
        raise ValueError("unknown_research_chain_or_horizon")
    swaps = _validated(rows, as_of_ms)
    if not swaps:
        return {"chain": chain, "status": "no_usable_tape", "nominations": [], "execution_ready": False}
    start, end = swaps[0].at_ms, swaps[-1].at_ms
    split = start + (end - start) * 3 // 5
    tape = defaultdict(list)
    buys = []
    for row in swaps:
        tape[row.market, row.side].append(row)
        if row.side == "buy":
            buys.append(row)
    hold_ms = hold_minutes * 60_000
    nominees = _nominate(tape, buys, start, split, hold_ms)
    return {"chain": chain, "status": "historical_print_paper", "rows": len(swaps),
            "start_ms": start, "split_ms": split, "end_ms": end, "nominations": nominees,
            "hold_minutes": hold_minutes,
            "nominated_paper": _compare(tape, buys, split, end, hold_ms, {w["wallet"] for w in nominees}),
            "all_observed_control": _compare(tape, buys, split, end, hold_ms, None),
            "cash_return_pct": 0, "execution_ready": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--chain", required=True, choices=CHAINS)
    parser.add_argument("--hold-minutes", type=int, choices=(5, 30), default=30)
    args = parser.parse_args()
    try:
        with args.input.open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("input_file_cap")
        packet = json.loads(raw, parse_float=Decimal)
        if not isinstance(packet["swaps"], list) or len(packet["swaps"]) > MAX_ROWS:
            raise ValueError("invalid_swap_rows")
        report = paper([Swap(**{**s, "price": Decimal(s["price"])}) for s in packet["swaps"]],
                       chain=args.chain, as_of_ms=packet["as_of_ms"], hold_minutes=args.hold_minutes)
    except (OSError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
        parser.exit(2, f"wallet paper input refused ({type(exc).__name__})\n")
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
