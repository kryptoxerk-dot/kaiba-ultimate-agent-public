"""Read existing decoded fill observations without upgrading their economic claims.

Pure, no database/provider writes or reads. A decision reference reconstructed
from min_out/slippage is not an executable quote; its price move includes venue
fees, impact and reference drift. None of these fields proves realized landing
slippage, complete rent/tax/custody cashflow or a causal trading edge.
Codex LOSS-COST-EVIDENCE-20261102.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core.schemas import EVM_CHAINS

SCHEMA = "kaiba.loss-cost-evidence/1"
FEE_FIELDS = ("venue_fee_native", "router_fee_native", "network_fee_native")
_EVM = {c.value for c in EVM_CHAINS}


def _decimal(value: Any, *, positive: bool = False) -> str | None:
    if type(value) not in (str, int) or (isinstance(value, str) and len(value) > 128):
        return None
    if type(value) is int and value.bit_length() > 512:
        return None
    try:
        number = Decimal(value)
    except (ValueError, InvalidOperation):
        return None
    if not number.is_finite() or (number != 0 and not -80 <= number.adjusted() <= 80):
        return None
    if positive and number <= 0:
        return None
    return format(number, "f")


def _atoms(value: Any) -> str | None:
    text = _decimal(value)
    if text is None:
        return None
    number = Decimal(text)
    if not 0 <= number < 2**256 or number != number.to_integral_value():
        return None
    return str(int(number))


def _detail(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or len(value) > 65_536:
        return {}
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _positive_time(value: Any, cutoff: int) -> bool:
    return type(value) is int and 0 < value <= cutoff


def _leg(position: dict[str, Any], order: dict[str, Any], row: dict[str, Any] | None,
         cutoff: int) -> dict[str, Any]:
    out: dict[str, Any] = {"order_id": order.get("order_id"), "side": order.get("side"),
        "source": "fill_costs", "matched_decoder_row": False, "computed_ms": None,
        "decoder_version": None, "decoder_basis": None, "signature_sha256": None,
        "native_unit": None, "wallet_bound": None, "reconciled": None,
        "decoder_reported_exec_vs_decision_bps": None, "decision_reference_basis": None,
        "exec_native_per_token": None, "decision_ref_native_per_token": None,
        "fee_net_exec_vs_decision_bps": None, "fee_net_basis": None,
        "reported_fee_atoms": dict.fromkeys((*FEE_FIELDS, "priority_fee_native")),
        "decision_to_fill_bound": False, "core_components_bound": False, "gaps": []}
    gaps = out["gaps"]
    opened, closed = position.get("opened_ms"), position.get("closed_ms")
    if not _positive_time(opened, cutoff) or not _positive_time(closed, cutoff) or closed < opened:
        gaps.append("position_clock_unavailable_or_out_of_window")
    if order.get("mode") != "live" or order.get("position_id") != position.get("position_id"):
        gaps.append("order_scope_mismatch")
    if order.get("side") not in {"buy", "sell"}:
        gaps.append("order_side_invalid")
    if any(order.get(k) != position.get(k) for k in ("chain", "token", "lane")):
        gaps.append("order_position_identity_mismatch")
    created, updated = order.get("created_ms"), order.get("updated_ms")
    if not _positive_time(created, cutoff) or not _positive_time(updated, cutoff) or updated < created:
        gaps.append("order_clock_unavailable_or_out_of_window")
    if row is None:
        gaps.append("decoder_row_unavailable")
    else:
        if any(row.get(k) != order.get(k) for k in ("order_id", "chain", "token", "side", "lane")):
            gaps.append("decoder_order_identity_mismatch")
        signature = order.get("tx_hash")
        if not isinstance(signature, str) or not signature or row.get("signature") != signature:
            gaps.append("decoder_transaction_not_bound")
        if not _positive_time(row.get("computed_ms"), cutoff):
            gaps.append("decoder_clock_unavailable_or_out_of_window")
        block_time = row.get("block_time_ms")
        if (not _positive_time(block_time, cutoff)
                or (_positive_time(row.get("computed_ms"), cutoff) and block_time > row["computed_ms"])):
            gaps.append("decoder_transaction_clock_unavailable_or_out_of_window")
    if gaps:
        return out
    detail = _detail(row.get("detail_json"))
    out.update(matched_decoder_row=True, computed_ms=row["computed_ms"],
               decoder_version=str(row.get("version") or "")[:80] or None,
               decoder_basis=str(row.get("basis") or "")[:80] or None,
               signature_sha256=hashlib.sha256(order["tx_hash"].encode()).hexdigest(),
               wallet_bound=detail.get("wallet_bound") if type(detail.get("wallet_bound")) is bool else None,
               reconciled=row.get("reconciled") == 1 if type(row.get("reconciled")) is int else None)
    expected_unit = "lamport" if order["chain"] == "sol" else "wei" if order["chain"] in _EVM else None
    out["native_unit"] = expected_unit if expected_unit and detail.get("native_unit") == expected_unit else None
    if out["native_unit"] is None:
        gaps.append("native_unit_unverified")
    if out["wallet_bound"] is not True:
        gaps.append("wallet_attribution_unverified")
    out["reported_fee_atoms"] = {k: _atoms(row.get(k)) for k in (*FEE_FIELDS, "priority_fee_native")}
    if row.get("basis") != "venue_event":
        gaps.append("priced_venue_event_unavailable")
        return out
    out["decoder_reported_exec_vs_decision_bps"] = _decimal(row.get("exec_vs_decision_bps"))
    out["exec_native_per_token"] = _decimal(row.get("exec_native_per_token"), positive=True)
    out["decision_ref_native_per_token"] = _decimal(row.get("decision_ref_native_per_token"), positive=True)
    ref_basis = row.get("decision_ref_basis")
    out["decision_reference_basis"] = ref_basis[:80] if isinstance(ref_basis, str) and ref_basis else None
    fee_net_basis = detail.get("exec_fee_net_basis")
    out["fee_net_basis"] = fee_net_basis[:80] if isinstance(fee_net_basis, str) else None
    if (order["chain"] == "sol" and out["native_unit"] == "lamport" and out["wallet_bound"] is True
            and fee_net_basis == "solana_native_venue_fee_removed"):
        out["fee_net_exec_vs_decision_bps"] = _decimal(detail.get("exec_fee_net_vs_decision_bps"))
    out["decision_to_fill_bound"] = (not gaps and out["decoder_reported_exec_vs_decision_bps"] is not None
                                    and out["exec_native_per_token"] is not None
                                    and out["decision_ref_native_per_token"] is not None
                                    and out["decision_reference_basis"] == "min_out_over_slippage_band")
    out["core_components_bound"] = (out["decision_to_fill_bound"] and out["reconciled"] is True
                                    and all(out["reported_fee_atoms"][k] is not None for k in FEE_FIELDS))
    if not out["decision_to_fill_bound"]:
        gaps.append("decision_reference_or_execution_comparison_unavailable")
    if not out["core_components_bound"]:
        gaps.append("core_decoded_components_incomplete_or_unreconciled")
    return out


def position_cost_evidence(position: dict[str, Any], orders: list[dict[str, Any]],
                           costs: dict[str, dict[str, Any]], *, cutoff_ms: int) -> dict[str, Any]:
    """Matched decoder observations for filled LIVE legs only; failed-tx costs excluded/unknown.

    Coverage flags are not full fee/cashflow qualification. Legacy loss-review
    slippage/cashflow gaps and UNPROVEN verdicts must stay independent of them.
    """
    out: dict[str, Any] = {"schema": SCHEMA, "applicable": position.get("mode") == "live",
        "scope": "filled_live_order_legs_only", "legs": [], "filled_legs": 0,
        "excluded_nonfilled_orders": 0, "matched_decoder_legs": 0,
        "decision_to_fill_bound_legs": 0, "core_components_bound_legs": 0,
        "decision_to_fill_every_filled_leg": False, "core_components_every_filled_leg": False,
        "realized_slippage_bps": None, "fee_complete_cashflow_verified": None,
        "rent_paid_atoms": None, "rent_recovered_atoms": None, "token_tax_atoms": None,
        "causal_verdict": "UNPROVEN", "gap_counts": {},
        "semantics": ["decoder_observations_not_independent_consensus_verification",
                      "decision_reference_reconstructed_from_min_out_and_slippage_envelope",
                      "decision_to_fill_includes_venue_fee_impact_and_reference_drift",
                      "fee_net_comparison_still_includes_impact_not_landing_slippage",
                      "priority_fee_is_a_subset_of_network_fee_do_not_add_twice",
                      "rent_tax_failed_transaction_and_full_custody_cashflow_unproven"]}
    if not out["applicable"]:
        return out
    filled = [o for o in orders if o.get("state") == "filled"]
    out["excluded_nonfilled_orders"] = len(orders) - len(filled)
    duplicate_ids = Counter(o.get("order_id") for o in filled)
    for order in filled:
        leg = _leg(position, order, costs.get(order.get("order_id")), cutoff_ms)
        if duplicate_ids[order.get("order_id")] > 1:
            leg["gaps"].append("duplicate_filled_order_link")
            leg["decision_to_fill_bound"] = leg["core_components_bound"] = False
        out["legs"].append(leg)
    legs = out["legs"]
    out.update(filled_legs=len(legs), matched_decoder_legs=sum(leg["matched_decoder_row"] for leg in legs),
               decision_to_fill_bound_legs=sum(leg["decision_to_fill_bound"] for leg in legs),
               core_components_bound_legs=sum(leg["core_components_bound"] for leg in legs),
               decision_to_fill_every_filled_leg=bool(legs) and all(leg["decision_to_fill_bound"] for leg in legs),
               core_components_every_filled_leg=bool(legs) and all(leg["core_components_bound"] for leg in legs),
               gap_counts=dict(Counter(g for leg in legs for g in leg["gaps"])))
    return out


def cost_coverage(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """Presence/binding counts for losing live positions, never an EV or causal estimate."""
    records = [r["decoded_fill_cost_evidence"] for r in reviews
               if r.get("mode") == "live" and "decoded_fill_cost_evidence" in r]
    return {"scope": "closed_losing_live_positions_only_not_all_trades",
            "positions": len(records),
            "positions_with_filled_legs": sum(r["filled_legs"] > 0 for r in records),
            "positions_decision_to_fill_every_filled_leg": sum(r["decision_to_fill_every_filled_leg"] for r in records),
            "positions_core_components_every_filled_leg": sum(r["core_components_every_filled_leg"] for r in records),
            **{key: sum(r[key] for r in records) for key in
               ("filled_legs", "matched_decoder_legs", "decision_to_fill_bound_legs", "core_components_bound_legs")},
            "strict_profitability_qualification_changed": False,
            "causal_verdict": "UNPROVEN"}
