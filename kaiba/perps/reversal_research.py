"""Offline intraday perpetuals candidate lab; no provider, account or order imports.

These are NEW research hypotheses, not a faithful port of JJ's unseen backend.
Input: one linear symbol/venue, closed UTC 1m bars, timestamped bid/ask/mark quotes,
explicit fees/slippage and settled funding. Quotes model market fills, not proof
of executability. No result grants live authority or changes the frozen D1 book.

python -m kaiba.perps.reversal_research --catalog
python -m kaiba.perps.reversal_research --input captured.json --output result.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from bisect import bisect_right
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

VERSION = "perps-reversal-research/1"
MINUTE = 60_000
ARMS = ("LF", "LF_CASCADE", "LF_FLOW", "VWAP_RECLAIM", "SWEEP_RECLAIM", "TREND_PULLBACK")


@dataclass(frozen=True)
class Policy:
    liquidation_usd: float = 10_000
    cascade_usd: float = 250_000
    min_stop_bps: float = 20
    stop_buffer_bps: float = 5
    target_r: float = 2
    breakeven_r: float = .5
    hold_ms: int = 60 * MINUTE
    entry_delay_ms: int = 5_000
    signal_ttl_ms: int = 120_000
    max_quote_age_ms: int = 2_000
    max_quote_gap_ms: int = 2_000
    initial_cash: float = 1_000
    notional: float = 100


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _num(value, name, minimum=0, strict=False):
    _require(type(value) in (int, float) and math.isfinite(value), "invalid_" + name)
    _require(value > minimum if strict else value >= minimum, "invalid_" + name)
    return value


def _clock(value):
    _require(type(value) is int and value >= 0, "invalid_clock")
    return value


def validate(data, policy=Policy()):
    _require(isinstance(data, dict) and data.get("schema") == VERSION, "schema")
    for key in ("symbol", "venue", "source"):
        _require(isinstance(data.get(key), str) and 0 < len(data[key]) <= 128, key)
    _require(data.get("contract_type") == "linear" and data.get("quote_asset") in
             ("USD", "USDT", "USDC"), "linear_quote_contract_required")
    _require(type(data.get("funding_complete")) is bool, "funding_completeness_required")
    for key in ("fee_bps_per_side", "slippage_bps_per_side"):
        _num(data.get(key), key)
        _require(data[key] <= 100, "cost_bound")
    for key in ("bars", "quotes", "funding"):
        _require(isinstance(data.get(key), list), key)
    _require(len(data["bars"]) <= 100_000 and len(data["quotes"]) <= 500_000
             and len(data["funding"]) <= 10_000, "row_bound")
    last_t, last_a = -1, -1
    for b in data["bars"]:
        t, a = _clock(b["t"]), _clock(b["available_ms"])
        _require(t % MINUTE == 0 and t > last_t and a >= t + MINUTE and a >= last_a, "bar_clock")
        for key in ("o", "h", "l", "c"):
            _num(b[key], key, strict=True)
        _require(b["l"] <= min(b["o"], b["c"]) <= max(b["o"], b["c"]) <= b["h"], "bar_prices")
        _num(b["volume"], "volume")
        _require(b.get("liq_basis") in ("observed", "snapshot", "proxy", "unknown"), "liq_basis")
        for key in ("long_liq_usd", "short_liq_usd", "buy_usd", "sell_usd"):
            if b.get(key) is not None:
                _num(b[key], key)
        last_t, last_a = t, a
    last_t, last_a = -1, -1
    for q in data["quotes"]:
        t, a = _clock(q["t"]), _clock(q["available_ms"])
        _require(t > last_t and a >= t and a >= last_a, "quote_clock")
        for key in ("bid", "ask", "mark"):
            _num(q[key], key, strict=True)
        _require(q["bid"] <= q["ask"], "crossed_quote")
        last_t, last_a = t, a
    times = []
    for f in data["funding"]:
        times.append(_clock(f["t"]))
        _num(f["mark"], "funding_mark", strict=True)
        _require(type(f["rate"]) in (int, float) and math.isfinite(f["rate"])
                 and abs(f["rate"]) <= 1, "funding_rate")
    _require(times == sorted(set(times)), "funding_clock")
    for name, value in asdict(policy).items():
        _num(value, name, strict=name not in ("entry_delay_ms",))
    _require(policy.notional <= policy.initial_cash and policy.breakeven_r < policy.target_r, "policy")
    return data


def _net(bar):
    if bar["liq_basis"] != "observed" or any(bar.get(k) is None for k in ("long_liq_usd", "short_liq_usd")):
        return None
    return bar["long_liq_usd"] - bar["short_liq_usd"]


def _history(bars, i, n):
    xs = bars[max(0, i-n):i]
    if len(xs) != n or any(b["available_ms"] > bars[i]["available_ms"] for b in xs):
        return []
    if any(b["t"] != bars[i]["t"] - (n-j)*MINUTE for j, b in enumerate(xs)):
        return []
    return xs


def _signal(arm, b, side, low, high):
    return {"arm": arm, "bar_ms": b["t"], "available_ms": b["available_ms"],
            "side": side, "low": low, "high": high}


def signals(data, policy=Policy()):
    """Causal fixed candidates; all six count as trials, not an optimizer."""
    validate(data, policy)
    bars, out = data["bars"], []
    cascade = None
    rejected = Counter()
    for i, b in enumerate(bars):
        net = _net(b)
        side = (1 if net > 0 else -1) if net is not None and abs(net) >= policy.liquidation_usd else 0
        if side:
            out.append(_signal("LF", b, side, b["l"], b["h"]))
            buys, sells = b.get("buy_usd"), b.get("sell_usd")
            if buys is None or sells is None:
                rejected["LF_FLOW:unknown_flow"] += 1
            elif side * (buys-sells) > 0 and side * (b["c"]-b["o"]) > 0:
                out.append(_signal("LF_FLOW", b, side, b["l"], b["h"]))
        if net is None:
            rejected["liquidation_input_unqualified"] += 1
            cascade = None
        else:
            recent = _history(bars, i, min(i, 10)) if i else []
            # A gap/unknown print coverage cannot prove isolation or exhaustion.
            known = len(recent) == 10 and all(_net(x) is not None for x in recent)
            if not known:
                cascade = None
                rejected["LF_CASCADE:incomplete_lookback"] += 1
            else:
                if cascade and b["t"]-cascade["last"] > 15*MINUTE:
                    cascade = None
                if cascade:
                    s = cascade["side"]
                    same = s*net >= policy.liquidation_usd
                    cascade["low"] = min(cascade["low"], b["l"])
                    cascade["high"] = max(cascade["high"], b["h"])
                    reversal = s*(b["c"]-b["o"]) > 0
                    if reversal and (not same or abs(net) < cascade["size"]):
                        out.append(_signal("LF_CASCADE", b, s, cascade["low"], cascade["high"]))
                        cascade = None
                    elif same:
                        cascade.update(last=b["t"], size=abs(net))
                if side and cascade is None and not any(x["arm"] == "LF_CASCADE" and x["bar_ms"] == b["t"] for x in out[-3:]):
                    same5 = [x for x in recent[-5:] if side*_net(x) >= policy.liquidation_usd]
                    amount = sum(max(0, side*_net(x)) for x in recent[-9:]) + abs(net)
                    if same5 or amount >= policy.cascade_usd:
                        group = recent[-5:] + [b]
                        cascade = {"side": side, "last": b["t"], "size": abs(net),
                                   "low": min(x["l"] for x in group), "high": max(x["h"] for x in group)}
                    else:
                        out.append(_signal("LF_CASCADE", b, side, b["l"], b["h"]))
        hist = _history(bars, i, 20)
        if not hist:
            continue
        lo, hi = min(x["l"] for x in hist), max(x["h"] for x in hist)
        for s, hit in ((1, b["l"] < lo and b["c"] > lo), (-1, b["h"] > hi and b["c"] < hi)):
            if hit:
                out.append(_signal("SWEEP_RECLAIM", b, s, b["l"], b["h"]))
        vol = sum(x["volume"] for x in hist)
        if vol:
            vwap = sum((x["h"]+x["l"]+x["c"])/3*x["volume"] for x in hist)/vol
            sd = statistics.pstdev(x["c"] for x in hist)
            if sd:
                for s in (1, -1):
                    if s*(hist[-1]["c"]-vwap) < -sd and s*(b["c"]-vwap) > 0:
                        out.append(_signal("VWAP_RECLAIM", b, s, min(b["l"], hist[-1]["l"]), max(b["h"], hist[-1]["h"])))
        slow = _history(bars, i, 60)
        if slow:
            # Fixed SMA20/SMA60 comparator, separate from the frozen D1 strategy.
            fast = statistics.fmean(x["c"] for x in hist)
            trend = statistics.fmean(x["c"] for x in slow)
            if fast > trend and b["l"] <= fast < b["c"] and b["c"] > b["o"]:
                out.append(_signal("TREND_PULLBACK", b, 1, b["l"], b["h"]))
            if fast < trend and b["h"] >= fast > b["c"] and b["c"] < b["o"]:
                out.append(_signal("TREND_PULLBACK", b, -1, b["l"], b["h"]))
    return sorted(out, key=lambda x: (x["available_ms"], x["bar_ms"], x["arm"])), dict(rejected)


def _book(arm, candidates, quotes, funding, data, policy, stress):
    cash, pos, index, last_q = policy.initial_cash, None, 0, None
    trades, skips = [], Counter()
    unknown = 0
    peak, max_dd = cash, 0.0
    fee = data["fee_bps_per_side"] * stress / 10_000
    slip = data["slippage_bps_per_side"] * stress / 10_000
    funding_times, prefix = [], [0.0]
    for f in funding:
        funding_times.append(f["t"])
        prefix.append(prefix[-1]+f["mark"]*f["rate"])

    def funding_cash(position, at_ms):
        lo = bisect_right(funding_times, position["entry_ms"])
        hi = bisect_right(funding_times, at_ms)
        return position["side"]*position["qty"]*(prefix[hi]-prefix[lo])

    for q in quotes:
        fresh = q["available_ms"]-q["t"] <= policy.max_quote_age_ms
        if pos and (not fresh or (last_q is not None and q["t"]-last_q > policy.max_quote_gap_ms)):
            pos["gap"] = True
        last_q = q["t"]
        if not fresh:
            skips["stale_quote"] += 1
            continue
        closed_here = False
        if pos:
            s = pos["side"]
            reason = ("stop" if s*(q["mark"]-pos["stop"]) <= 0 else
                      "target" if s*(q["mark"]-pos["target"]) >= 0 else
                      "time" if q["t"]-pos["entry_ms"] >= policy.hold_ms else None)
            if reason:
                exit_px = (q["bid"] if s == 1 else q["ask"])*(1-s*slip)
                paid = funding_cash(pos, q["t"])
                exit_fee = pos["qty"]*exit_px*fee
                pnl = s*pos["qty"]*(exit_px-pos["entry"])-pos["entry_fee"]-exit_fee-paid
                cash += policy.notional + pnl + pos["entry_fee"]
                issue = pos["gap"] or not data["funding_complete"]
                unknown += int(issue)
                trades.append({"arm": arm, "side": s, "signal_ms": pos["signal_ms"],
                               "entry_ms": pos["entry_ms"], "exit_ms": q["t"], "entry": pos["entry"],
                               "exit": exit_px, "initial_stop": pos["initial_stop"], "reason": reason,
                               "modeled_pnl": pnl, "modeled_r": pnl/pos["risk_quote"],
                               "fees": pos["entry_fee"]+exit_fee, "funding_paid": paid,
                               "cashflow_unknown": issue})
                pos, closed_here = None, True
            elif s*(q["mark"]-pos["entry"]) >= policy.breakeven_r*pos["distance"]:
                pos["stop"] = pos["entry"]
        while index < len(candidates) and candidates[index]["available_ms"]+policy.entry_delay_ms <= q["available_ms"]:
            sig = candidates[index]
            if q["t"] < sig["available_ms"]+policy.entry_delay_ms:
                break
            index += 1
            if q["t"]-sig["available_ms"] > policy.signal_ttl_ms:
                skips["expired_signal"] += 1
                continue
            if pos or closed_here:
                skips["position_busy"] += 1
                continue
            s = sig["side"]
            entry = (q["ask"] if s == 1 else q["bid"])*(1+s*slip)
            boundary = sig["low"]*(1-policy.stop_buffer_bps/10_000) if s == 1 else sig["high"]*(1+policy.stop_buffer_bps/10_000)
            dist = max(s*(entry-boundary), entry*policy.min_stop_bps/10_000)
            entry_fee = policy.notional*fee
            if cash < policy.notional+entry_fee:
                skips["cash_unavailable"] += 1
                continue
            cash -= policy.notional+entry_fee
            pos = {"side": s, "entry": entry, "entry_ms": q["t"], "signal_ms": sig["available_ms"],
                   "distance": dist, "stop": entry-s*dist, "initial_stop": entry-s*dist,
                   "target": entry+s*policy.target_r*dist, "qty": policy.notional/entry,
                   "risk_quote": policy.notional/entry*dist, "entry_fee": entry_fee, "gap": False}
        # Mark-to-market with market liquidation costs; never a historical close-only curve.
        equity = cash
        if pos:
            s = pos["side"]
            px = (q["bid"] if s == 1 else q["ask"])*(1-s*slip)
            paid = funding_cash(pos, q["t"])
            equity += policy.notional + s*pos["qty"]*(px-pos["entry"])-pos["qty"]*px*fee-paid
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak-equity)/peak)
    skips["no_quote_after_signal"] += len(candidates)-index
    complete = not unknown and pos is None and data["funding_complete"]
    return {"candidates": len(candidates), "closed": len(trades), "open": int(pos is not None),
            "unknown_closed": unknown, "skips": dict(skips), "trades": trades,
            "modeled_closed_pnl": sum(t["modeled_pnl"] for t in trades),
            "net_pnl": cash-policy.initial_cash if complete and trades else None,
            "modeled_max_drawdown": max_dd if quotes else None,
            "economic_coverage": "complete_under_model" if complete and trades else "incomplete_or_empty"}


def run(data, policy=Policy()):
    validate(data, policy)
    candidates, gaps = signals(data, policy)
    results = {}
    for arm in ARMS:
        selected = [s for s in candidates if s["arm"] == arm]
        results[arm] = {"base": _book(arm, selected, data["quotes"], data["funding"], data, policy, 1),
                        "cost_stress_2x": _book(arm, selected, data["quotes"], data["funding"], data, policy, 2)}
    return {"schema": VERSION, "mode": "offline_research", "execution_allowed": False,
            "profitability_proven": False, "promotable": False, "verdict": "UNPROVEN",
            "symbol": data["symbol"], "venue": data["venue"], "source": data["source"],
            "contract_type": data["contract_type"], "quote_asset": data["quote_asset"],
            "cost_model": {"fee_bps_per_side": data["fee_bps_per_side"],
                           "slippage_bps_per_side": data["slippage_bps_per_side"],
                           "funding_complete": data["funding_complete"],
                           "stress": "2x fees and slippage; actual funding unchanged"},
            "input_counts": {k: len(data[k]) for k in ("bars", "quotes", "funding")},
            "input_sha256": hashlib.sha256(json.dumps(data, sort_keys=True, allow_nan=False).encode()).hexdigest(),
            "policy": asdict(policy), "trial_count": len(ARMS), "input_gaps": gaps,
            "results": results, "limitations": [
                "Caller-supplied provenance/completeness is not independently authenticated.",
                "Quotes model market fills without venue depth/queue/partial-fill proof.",
                "One fully collateralized fixed-notional position per arm; no leverage/liquidation model.",
                "Arms are independent alternative books, not additive allocated portfolio returns.",
                "Sampled mark paths cannot prove intratick stop ordering; feed gaps withhold net totals.",
                "No data download, live collector, account, order, ranking promotion or D1 change."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", action="store_true")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.catalog:
        print(json.dumps({"schema": VERSION, "arms": ARMS, "policy": asdict(Policy()),
                          "required": ["symbol", "venue", "source", "contract_type", "quote_asset", "bars", "quotes", "funding",
                                       "funding_complete", "fee_bps_per_side", "slippage_bps_per_side"],
                          "execution_allowed": False}, indent=2))
        return 0
    if args.input is None:
        parser.error("--input or --catalog required")
    _require(args.input.stat().st_size <= 64*1024*1024, "file_bound")
    report = run(json.loads(args.input.read_text(encoding="utf-8")))
    encoded = json.dumps(report, indent=2, allow_nan=False)
    if args.output:
        with args.output.open("x", encoding="utf-8") as output:
            output.write(encoded + "\n")
    else:
        print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
