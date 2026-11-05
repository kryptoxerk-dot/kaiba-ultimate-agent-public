"""Finite-cash, point-in-time research for the owner's $100k pullback hypothesis.

Owner: Codex, TWEET-STRATEGY-REVIEW-20261102. No execution or provider imports.
This consumes observations and *fixed-size* cashflows, not historical leader
prices. It does not invent fills, rescale price impact, or value unsold tokens.
It is a diagnostic, never a promotion gate. Input provenance remains the
exporter's responsibility. See docs/research/codex-tweet-strategy-review-20261102.md.

python -m kaiba.learning.pullback_study --input PRIVATE_EXPORT.json
"""
from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

VERSION = "pullback-study/1"
ARMS = ("baseline", "cap_100k", "cap_dip", "cap_dip_reclaim")
MAX_BYTES = 16 * 1024 * 1024
LAMPORTS = 1_000_000_000


@dataclass(frozen=True)
class Policy:
    """Declared hypotheses, not fitted parameters or production risk settings."""

    cap_usd: float = 100_000
    dip_min: float = .20
    dip_max: float = .40
    reclaim: float = .10
    lookback_ms: int = 30 * 60_000
    freshness_ms: int = 60_000
    entry_delay_ms: int = 30_000


def _require(ok, reason):
    if not ok:
        raise ValueError(reason)


def _integer(value, name, minimum=0):
    _require(type(value) is int and value >= minimum, "invalid_" + name)
    return value


def _positive(value, name):
    _require(type(value) in (float, int) and math.isfinite(value) and value > 0,
             "invalid_" + name)
    return value


def validate(data):
    """Refuse incomplete schemas, duplicated cases and inconsistent clocks."""
    _require(isinstance(data, dict) and data.get("version") == "pullback-input/1", "input_version")
    start = _integer(data.get("start_ms"), "start_ms")
    end = _integer(data.get("end_ms"), "end_ms")
    _require(end > start, "window")
    _require(data.get("chain") == "sol", "sol_only")
    _require(data.get("population") in ("all_scanned", "signals", "selected"), "population")
    obs, cases = data.get("observations"), data.get("opportunities")
    _require(isinstance(obs, list) and isinstance(cases, list), "input_lists")
    _require(len(obs) <= 100_000 and len(cases) <= 10_000, "row_limit")
    scanned = _integer(data.get("scanned_count"), "scanned_count")
    _require(scanned >= len(cases), "population_count")
    seen = set()
    for row in obs:
        _require(isinstance(row, dict), "observation_shape")
        for key in ("token", "source"):
            _require(isinstance(row.get(key), str) and 0 < len(row[key]) <= 128, "observation_identity")
        observed = _integer(row.get("observed_ms"), "observed_ms")
        available = _integer(row.get("available_ms"), "available_ms")
        _require(available >= observed, "observation_clock")
        _positive(row.get("price_usd"), "price")
        _require(row.get("cap_basis") in ("market_cap", "fdv", "unknown"), "cap_basis")
        if row.get("market_cap_usd") is not None:
            _positive(row["market_cap_usd"], "market_cap")
        key = (row["token"], row["source"], observed)
        _require(key not in seen, "duplicate_observation")
        seen.add(key)
    seen.clear()
    for row in cases:
        _require(isinstance(row, dict), "opportunity_shape")
        for key in ("id", "token"):
            _require(isinstance(row.get(key), str) and 0 < len(row[key]) <= 128, "case_identity")
        _require(row["id"] not in seen, "duplicate_opportunity")
        seen.add(row["id"])
        signal = _integer(row.get("signal_ms"), "signal_ms")
        _require(start <= signal < end, "signal_outside_window")
        _require(row.get("evidence") in ("measured", "modeled", "unknown"), "cashflow_evidence")
        entry, cost = row.get("entry_ms"), row.get("cost_lamports")
        settled, proceeds = row.get("settled_ms"), row.get("proceeds_lamports")
        if entry is None:
            _require(cost is None and settled is None and proceeds is None, "unfilled_cashflow")
        else:
            _integer(entry, "entry_ms")
            _integer(cost, "cost_lamports", 1)
            _require(entry >= signal, "entry_before_signal")
            if settled is not None:
                _integer(settled, "settled_ms")
                _require(settled > entry, "settlement_before_entry")
                _integer(proceeds, "proceeds_lamports")
            else:
                _require(proceeds is None, "proceeds_without_settlement")
    return data


def classify(observations, signal_ms, policy=Policy()):
    """Features from information available by the signal, on one source only.

    A later peak, delayed backfill, FDV, or stale quote cannot qualify a dip.
    'Prior crossed $100k' uses an actual earlier cap observation, not current
    supply multiplied backwards or the token's eventual all-time high.
    """
    flags = dict.fromkeys(ARMS, False)
    flags["baseline"] = True
    known = [r for r in observations
             if signal_ms - policy.lookback_ms <= r["observed_ms"] <= signal_ms
             and r["available_ms"] <= signal_ms]
    if not known:
        return flags, "missing_observation"
    latest_ms = max(r["observed_ms"] for r in known)
    latest = [r for r in known if r["observed_ms"] == latest_ms]
    if len(latest) != 1:
        return flags, "ambiguous_source"
    current = latest[0]
    if signal_ms - latest_ms > policy.freshness_ms:
        return flags, "stale_observation"
    if current["cap_basis"] != "market_cap" or current.get("market_cap_usd") is None:
        return flags, "market_cap_unverified"
    if current["market_cap_usd"] < policy.cap_usd:
        return flags, "below_100k"
    flags["cap_100k"] = True
    path = sorted((r for r in known if r["source"] == current["source"]),
                  key=lambda r: r["observed_ms"])
    prior = [r for r in path if r["observed_ms"] < latest_ms]
    if not any(r["cap_basis"] == "market_cap" and
               (r.get("market_cap_usd") or 0) >= policy.cap_usd for r in prior):
        return flags, "prior_crossing_unobserved"
    peak = max(prior, key=lambda r: (r["price_usd"], r["observed_ms"]))
    dip = 1 - current["price_usd"] / peak["price_usd"]
    if not policy.dip_min - 1e-12 <= dip <= policy.dip_max + 1e-12:
        return flags, "outside_dip_band"
    flags["cap_dip"] = True
    trough = min(r["price_usd"] for r in path if r["observed_ms"] > peak["observed_ms"])
    flags["cap_dip_reclaim"] = current["price_usd"] / trough >= 1 + policy.reclaim - 1e-12
    return flags, "eligible" if flags["cap_dip_reclaim"] else "no_reclaim"


def _replay(cases, eligible, arm, *, capital, reserve, end_ms, policy):
    """Single-entry per token; settlements strictly before a new fill fund it.

    Holding unresolved inventory consumes its original cash. End marks, external
    deposits and future proceeds never replenish cash. Costs and proceeds must
    include all fee cashflows. No size interpolation or automatic compounding up.
    """
    cash, high_water, drawdown = capital, capital, 0
    pending, entered = [], set()
    counts = Counter()
    realized = 0
    locked = 0

    def settle(before, inclusive=False):
        nonlocal cash, realized, locked, high_water, drawdown
        while pending and (pending[0][0] < before or (inclusive and pending[0][0] == before)):
            _ms, _id, cost, proceeds = heapq.heappop(pending)
            cash += proceeds
            locked -= cost
            realized += proceeds - cost
            counts["settled"] += 1
            equity = capital + realized  # open inventory is cost basis, NOT liquidation value
            high_water = max(high_water, equity)
            drawdown = max(drawdown, high_water - equity)

    chosen = [c for c in cases if eligible[c["id"]][arm]]
    counts["qualified"] = len(chosen)
    for c in sorted(chosen, key=lambda c: (c["entry_ms"] if c["entry_ms"] is not None else end_ms,
                                          c["signal_ms"], c["id"])):
        if c["entry_ms"] is None:
            counts["missing_entry"] += 1
            continue
        if c["entry_ms"] >= end_ms:
            counts["entry_outside_window"] += 1
            continue
        if c["entry_ms"] < c["signal_ms"] + policy.entry_delay_ms:
            counts["entry_faster_than_declared_delay"] += 1
            continue
        settle(c["entry_ms"])
        if c["token"] in entered:
            counts["repeat_token"] += 1
            continue
        cost = c["cost_lamports"]
        if cost > cash - reserve:
            counts["insufficient_cash"] += 1
            continue
        cash -= cost
        locked += cost
        entered.add(c["token"])
        counts["entered"] += 1
        counts[c["evidence"] + "_entries"] += 1
        if c["settled_ms"] is not None and c["evidence"] != "unknown":
            heapq.heappush(pending, (c["settled_ms"], c["id"], cost, c["proceeds_lamports"]))
        elif c["settled_ms"] is not None:
            counts["unverified_settlement"] += 1
    settle(end_ms, inclusive=True)
    return {"arm": arm, "starting_lamports": capital, "reserve_lamports": reserve,
            "cash_lamports": cash, "unsettled_cost_lamports": locked,
            "realized_pnl_lamports": realized, "realized_return_on_start": realized / capital,
            "realized_drawdown_lamports": drawdown, "counts": dict(sorted(counts.items())),
            "liquidation_equity_lamports": cash if locked == 0 else None,
            "equity_if_unsettled_worth_zero_lamports": cash,
            "cashflow_basis": "recorded_inputs; modeled entries remain modeled",
            "profit_proven": False}


def study(data, *, capitals=(LAMPORTS // 2, 5 * LAMPORTS), reserve=20_000_000, policy=Policy()):
    validate(data)
    _require(0 < len(capitals) <= 20, "capital_scenarios")
    _integer(reserve, "reserve")
    for capital in capitals:
        _integer(capital, "capital", 1)
        _require(reserve < capital, "reserve_exceeds_capital")
    by_token = defaultdict(list)
    for row in data["observations"]:
        by_token[row["token"]].append(row)
    eligible, reasons = {}, Counter()
    for c in data["opportunities"]:
        flags, reason = classify(by_token[c["token"]], c["signal_ms"], policy)
        eligible[c["id"]] = flags
        reasons[reason] += 1
    return {"version": VERSION, "status": "research_only", "profit_proven": False,
            "population_claim": data["population"], "population_verified": False,
            "scanned_count_claim": data["scanned_count"],
            "opportunities": len(data["opportunities"]), "observations": len(data["observations"]),
            "classification_counts": dict(sorted(reasons.items())),
            "assumptions": {**policy.__dict__, "one_entry_per_token": True,
                            "exact_recorded_size": True, "costs_and_proceeds_must_be_net": True},
            "results": [_replay(data["opportunities"], eligible, arm, capital=capital,
                                reserve=reserve, end_ms=data["end_ms"], policy=policy)
                        for capital in capitals for arm in ARMS]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--capital-sol", type=Decimal, nargs="+", default=[Decimal('.5'), Decimal('5')])
    parser.add_argument("--reserve-sol", type=Decimal, default=Decimal('.02'))
    args = parser.parse_args(argv)
    try:
        _require(args.input.stat().st_size <= MAX_BYTES, "input_size")
        with args.input.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        _require(len(raw) <= MAX_BYTES, "input_size")
        values = [*args.capital_sol, args.reserve_sol]
        _require(all(v.is_finite() and v >= 0 and v * LAMPORTS == int(v * LAMPORTS)
                     for v in values), "lamport_precision")
        report = study(json.loads(raw), capitals=tuple(int(v * LAMPORTS) for v in args.capital_sol),
                       reserve=int(args.reserve_sol * LAMPORTS))
        report["input_sha256"] = hashlib.sha256(raw).hexdigest()
    except (ValueError, TypeError, KeyError, OSError, ArithmeticError):
        # Do not echo arbitrary input strings, private paths or addresses.
        print(json.dumps({"version": VERSION, "status": "invalid_input", "profit_proven": False}))
        return 2
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
