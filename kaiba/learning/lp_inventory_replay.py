"""Offline LP inventory and cashflow accounting (Codex, 2026-10-29).

Replay normalized *position-level* events, never pool APR or candles as earned fees.
No providers, database, signing, orders, policy selection or production imports.
This is an accounting replay, not a DLMM swap/range counterfactual simulator.
See skills/lp-inventory-replay/SKILL.md for the deliberately narrow input contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

VERSION = "lp-inventory-replay/1"
MAX_BYTES = 4_000_000
MAX_EVENTS = 10_000
ZERO = Decimal(0)
DAY_MS = Decimal(86_400_000)
COST_KEYS = ("network", "rent_spent", "other")
ATTESTATIONS = ("inventory_complete", "fees_complete", "costs_complete")


class ReplayError(ValueError):
    """Invalid input; no partial result is published."""


def number(value: Any, field: str, *, signed: bool = False) -> Decimal:
    # Require decimal strings or integers: no binary float or nonfinite JSON numbers.
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise ReplayError(f"{field}: decimal string or integer required")
    if len(str(value)) > 96:
        raise ReplayError(f"{field}: number too long")
    try:
        out = Decimal(value)
    except InvalidOperation as exc:
        raise ReplayError(f"{field}: invalid decimal") from exc
    if not out.is_finite() or abs(out.as_tuple().exponent) > 30 or out.copy_abs() > Decimal("1e30"):
        raise ReplayError(f"{field}: nonfinite or outside numeric bounds")
    if not signed and out < 0:
        raise ReplayError(f"{field}: negative amount")
    return out


def pair(value: Any, field: str, *, signed: bool = False) -> list[Decimal]:
    if not isinstance(value, list) or len(value) != 2:
        raise ReplayError(f"{field}: [token, quote] required")
    return [number(x, field, signed=signed) for x in value]


def integer(value: Any, field: str, *, nonnegative: bool = True) -> int:
    if type(value) is not int or abs(value) > 10**15 or (nonnegative and value < 0):
        raise ReplayError(f"{field}: integer required")
    return value


def _move(source: list[Decimal], target: list[Decimal], amount: list[Decimal]) -> None:
    if any(a > s for s, a in zip(source, amount, strict=True)):
        raise ReplayError("insufficient inventory")
    for i in range(2):
        source[i] -= amount[i]
        target[i] += amount[i]


def _value(amount: list[Decimal], prices: list[Decimal]) -> Decimal:
    return sum((a * p for a, p in zip(amount, prices, strict=True)), ZERO)


def _serialized(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {k: _serialized(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialized(v) for v in value]
    return value


def replay(trace: dict[str, Any]) -> dict[str, Any]:
    """Reconcile one supplied trace; never certify provider completeness or profitability.

    Inventory changes must already belong to this position, after its bin-share allocation.
    Cost amounts are in quote units, debited once. Unknown cost records suppress net results.
    USD marks price each asset separately, including the quote asset's possible depeg.
    """
    with localcontext() as ctx:
        ctx.prec = 80
        return _serialized(_replay(trace))


def _replay(trace: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(trace, dict) or trace.get("schema") != VERSION:
        raise ReplayError("unsupported schema")
    if trace.get("quote_asset") != "SOL":
        raise ReplayError("v1 supports SOL-quoted positions only; gas and rent use this same inventory")
    evidence = trace.get("evidence")
    if not isinstance(evidence, dict):
        raise ReplayError("evidence required")
    if evidence.get("kind") not in ("observed", "modeled", "fixture"):
        raise ReplayError("evidence.kind must be observed, modeled or fixture")
    if not re.fullmatch(r"[a-f0-9]{64}", str(evidence.get("source_sha256", ""))):
        raise ReplayError("source_sha256 required")
    if any(type(evidence.get(k)) is not bool for k in ATTESTATIONS):
        raise ReplayError("explicit completeness attestations required")
    initial = trace.get("initial", {})
    if not isinstance(initial, dict):
        raise ReplayError("initial object required")
    start = integer(initial.get("at_ms"), "initial.at_ms")
    at = start
    wallet = pair(initial.get("wallet"), "initial.wallet")
    original = wallet.copy()
    prices = pair(initial.get("prices_usd"), "initial.prices_usd")
    if min(prices) <= 0:
        raise ReplayError("initial prices must be positive")
    initial_value = _value(original, prices)
    if initial_value <= 0:
        raise ReplayError("positive initial capital required")
    # These are opportunity-cost benchmarks, no hypothetical acquisition fees assumed.
    all_token = initial_value / prices[0]
    all_quote = initial_value / prices[1]
    policy = trace.get("policy")
    if not isinstance(policy, dict) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", str(policy.get("id", ""))):
        raise ReplayError("policy.id required")
    declared = integer(policy.get("declared_ms"), "policy.declared_ms")
    events = trace.get("events")
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_EVENTS:
        raise ReplayError("bounded nonempty events required")
    bins: dict[int, list[Decimal]] = {}
    pending = [ZERO, ZERO]
    locked = ZERO
    active: int | None = None
    costs = {k: ZERO for k in COST_KEYS}
    fees_usd = ZERO
    fee_units = [ZERO, ZERO]
    principal_change_usd = ZERO
    areas = {k: ZERO for k in ("lp", "active_bin", "wallet", "locked_rent")}
    unknown_costs = 0
    seen: set[str] = set()
    timeline = []
    terminal = False
    for seq, event in enumerate(events):
        if not isinstance(event, dict) or terminal:
            raise ReplayError("invalid event or event after terminal checkpoint")
        ident = event.get("id")
        if not isinstance(ident, str) or not 1 <= len(ident) <= 200 or ident in seen:
            raise ReplayError("unique event id required")
        seen.add(ident)
        now = integer(event.get("at_ms"), "event.at_ms")
        if now < at:
            raise ReplayError("events must be in causal order")
        dt = Decimal(now - at) / DAY_MS
        areas["lp"] += sum((_value(b, prices) for b in bins.values()), ZERO) * dt
        areas["active_bin"] += _value(bins.get(active, [ZERO, ZERO]), prices) * dt
        areas["wallet"] += _value(wallet, prices) * dt
        areas["locked_rent"] += locked * prices[1] * dt
        at = now
        new_prices = pair(event.get("prices_usd"), "event.prices_usd")
        if min(new_prices) <= 0:
            raise ReplayError("event prices must be positive")
        prices = new_prices
        active = integer(event.get("active_bin"), "active_bin", nonnegative=False)
        kind = event.get("kind")
        cost = event.get("cost_quote")
        if cost is None:
            unknown_costs += 1
        else:
            if not isinstance(cost, dict) or set(cost) != set(COST_KEYS):
                raise ReplayError("cost_quote requires all cost categories, or null")
            for key in COST_KEYS:
                amount = number(cost[key], "cost_quote")
                if amount > wallet[1]:
                    raise ReplayError("cost exceeds free quote inventory")
                wallet[1] -= amount
                costs[key] += amount * prices[1]
        if kind in ("deposit", "withdraw", "inventory_swap"):
            bid = integer(event.get("bin"), "bin", nonnegative=False)
            balance = bins.setdefault(bid, [ZERO, ZERO])
            if kind == "inventory_swap":
                delta = pair(event.get("delta"), "delta", signed=True)
                if delta[0] * delta[1] >= 0:
                    raise ReplayError("swap requires opposite nonzero asset deltas")
                if any(b + d < 0 for b, d in zip(balance, delta, strict=True)):
                    raise ReplayError("swap exceeds bin inventory")
                for i in range(2):
                    balance[i] += delta[i]
                principal_change_usd += _value(delta, prices)
            else:
                amount = pair(event.get("amount"), "amount")
                _move(wallet if kind == "deposit" else balance, balance if kind == "deposit" else wallet, amount)
        elif kind == "wallet_swap":
            delta = pair(event.get("delta"), "delta", signed=True)
            if delta[0] * delta[1] >= 0 or any(b + d < 0 for b, d in zip(wallet, delta, strict=True)):
                raise ReplayError("invalid wallet swap")
            for i in range(2):
                wallet[i] += delta[i]
            principal_change_usd += _value(delta, prices)
        elif kind == "fee":
            amount = pair(event.get("amount"), "amount")
            for i in range(2):
                pending[i] += amount[i]
                fee_units[i] += amount[i]
            fees_usd += _value(amount, prices)
        elif kind == "claim":
            _move(pending, wallet, pair(event.get("amount"), "amount"))
        elif kind in ("rent_lock", "rent_refund"):
            amount = number(event.get("amount_quote"), "amount_quote")
            if kind == "rent_lock":
                if amount > wallet[1]:
                    raise ReplayError("rent lock exceeds quote inventory")
                wallet[1] -= amount
                locked += amount
            else:
                if amount > locked:
                    raise ReplayError("rent refund exceeds locked rent")
                wallet[1] += amount
                locked -= amount
        elif kind == "checkpoint":
            expected = event.get("expected")
            if not isinstance(expected, dict):
                raise ReplayError("checkpoint expected balances required")
            total_bins = [sum((b[i] for b in bins.values()), ZERO) for i in range(2)]
            for key, actual in (("wallet", wallet), ("lp", total_bins), ("pending_fees", pending)):
                if pair(expected.get(key), key) != actual:
                    raise ReplayError(f"checkpoint {key} mismatch")
            if number(expected.get("locked_quote"), "locked_quote") != locked:
                raise ReplayError("checkpoint locked rent mismatch")
            terminal = event.get("terminal") is True
        elif kind != "mark":
            raise ReplayError("unknown event kind")
        total = wallet.copy()
        for i in range(2):
            total[i] += pending[i] + sum((b[i] for b in bins.values()), ZERO)
        total[1] += locked
        timeline.append({"seq": seq, "at_ms": at, "equity_usd_known_costs": _value(total, prices)})
    if not terminal or at <= start:
        raise ReplayError("terminal reconciled checkpoint after start required")
    gaps = [k for k in ATTESTATIONS if not evidence[k]]
    if unknown_costs:
        gaps.append("unknown_event_costs")
    if declared > start:
        gaps.append("policy_not_declared_before_start")
    if evidence["kind"] != "observed":
        gaps.append("not_observed_evidence")
    end_value = _value(total, prices)
    known_pnl = end_value - initial_value
    summed_costs = sum(costs.values(), ZERO)
    # Dollar valuation at accrual/cost time; residual includes subsequent fee-asset price moves.
    residual = known_pnl - fees_usd + summed_costs
    net_available = not gaps
    return {
        "schema": VERSION, "policy": policy["id"], "source_sha256": evidence["source_sha256"],
        "evidence_kind": evidence["kind"], "start_ms": start, "end_ms": at, "events": len(events),
        "accounting_reconciled": True, "net_available": net_available, "evidence_gaps": gaps,
        "chain_verified": False, "strategy_promotable": False,
        "initial_usd": initial_value, "ending_usd_known_costs": end_value,
        "pnl_usd_known_costs": known_pnl, "net_pnl_usd": known_pnl if net_available else None,
        "net_return": known_pnl / initial_value if net_available else None,
        "fees_at_accrual_usd": fees_usd, "fee_units": fee_units, "costs_usd": costs,
        "inventory_and_mark_change_usd": residual,
        "swap_value_change_at_event_marks_usd": principal_change_usd,
        "vs_initial_basket_usd_known_costs": end_value - _value(original, prices),
        "vs_all_token_usd_known_costs": end_value - all_token * prices[0],
        "vs_all_quote_usd_known_costs": end_value - all_quote * prices[1],
        "vs_usd_cash_known_costs": known_pnl,
        "capital_usd_days": areas,
        "active_share_of_lp_capital_time": areas["active_bin"] / areas["lp"] if areas["lp"] else None,
        "ending": {"wallet": wallet, "lp": [sum((b[i] for b in bins.values()), ZERO) for i in range(2)],
                   "pending_fees": pending, "locked_quote": locked},
        "timeline": timeline,
        "limitations": [
            "Input receipt completeness is an attestation, not independently verified on chain.",
            "Position-level bin inventory and fee allocations must come from decoded events; no APR reconstruction.",
            "Marks value inventory; they are not executable liquidation quotes.",
            "The cash benchmark holds constant USD; it is not a measured USDC strategy or yield.",
            "Capital time uses preceding marks and active bin, held constant between events.",
            "Inventory residual is not a causal estimate of adverse selection or loss-versus-rebalancing.",
            "Replays supplied actions only; alternative policies require their own causal, costed traces.",
        ],
    }


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReplayError("duplicate JSON key")
        result[key] = value
    return result


def load_trace(path: Path) -> tuple[dict[str, Any], str]:
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ReplayError("trace exceeds byte budget")
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, RecursionError) as exc:
        raise ReplayError("invalid trace JSON") from exc
    if not isinstance(value, dict):
        raise ReplayError("trace object required")
    return value, hashlib.sha256(raw).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path, help="bounded local normalized event trace")
    args = parser.parse_args()
    try:
        trace, digest = load_trace(args.trace)
        result = replay(trace)
        result["input_sha256"] = digest
    except (ReplayError, OSError) as exc:
        parser.exit(2, f"replay refused: {exc}\n")
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
