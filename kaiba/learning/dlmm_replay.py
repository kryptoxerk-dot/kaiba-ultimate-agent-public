"""Replay captured DLMM balance observations without inventing interim swaps or costs.

This is the account-state input stage of the LP inventory engine. It separates gross earned
fees from claims and inventory changes. Sparse observations cannot reconstruct each swap or
prove alternative keep/narrow/rebalance/close executions. Net results remain unavailable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from kaiba.learning.dlmm_state import DecodeError, snapshot
from kaiba.learning.lp_inventory_replay import ReplayError, load_trace

MAX_CAPTURES = 64
MAX_TOTAL_BYTES = 32 * 1024 * 1024


def compare(receipts: list[dict]) -> dict:
    """Pure chronology/basis/claim reconciliation on captured raw RPC replies."""
    if not isinstance(receipts, list) or not 2 <= len(receipts) <= MAX_CAPTURES:
        raise DecodeError("two_to_64_captures_required")
    states = [snapshot(r) for r in receipts]
    first = states[0]
    intervals = []
    for before, after in zip(states, states[1:], strict=False):
        if (before["pool_address"], before["position_address"]) != (after["pool_address"], after["position_address"]):
            raise DecodeError("capture_identity_changed")
        if after["slot"] <= before["slot"] or after["received_ms"] <= before["received_ms"]:
            raise DecodeError("capture_chronology_invalid")
        if before["pool"]["mints"] != after["pool"]["mints"]:
            raise DecodeError("pool_mints_changed")
        old, new = before["position"], after["position"]
        if old["owner_fingerprint"] != new["owner_fingerprint"]:
            raise DecodeError("position_owner_changed")
        claims = [b - a for a, b in zip(old["claimed_fees"], new["claimed_fees"], strict=True)]
        if min(claims) < 0:
            raise DecodeError("claimed_counter_regressed")
        accrued = [b + c - a for a, b, c in zip(before["fee_atoms_gross"], after["fee_atoms_gross"], claims, strict=True)]
        if min(accrued) < 0:
            raise DecodeError("fee_entitlement_reconciliation_failed")
        changed_shares = [(b["id"], b["shares"]) for b in old["bins"]] != [(b["id"], b["shares"]) for b in new["bins"]]
        intervals.append({"from_slot": before["slot"], "to_slot": after["slot"],
                          "elapsed_receipt_ms": after["received_ms"] - before["received_ms"],
                          "principal_delta_atoms": [b - a for a, b in zip(before["principal_atoms"], after["principal_atoms"], strict=True)],
                          "claimed_fee_delta_atoms_gross": claims, "earned_fee_atoms_gross": accrued,
                          "shares_changed": changed_shares,
                          "position_update_clock_changed": old["updated_at"] != new["updated_at"],
                          "interpretation": "action_history_required" if changed_shares else "unchanged_endpoint_shares_only"})
    observations = [{"slot": s["slot"], "received_ms": s["received_ms"],
                     "active_bin": s["pool"]["active_bin"],
                     "in_range_at_observation": s["position"]["lower_bin"] <= s["pool"]["active_bin"] <= s["position"]["upper_bin"],
                     "principal_atoms": s["principal_atoms"], "claimable_fee_atoms_gross": s["fee_atoms_gross"]}
                    for s in states]
    return {"schema": "kaiba-dlmm-observed-replay/1", "observations": observations, "intervals": intervals,
            "identity_fingerprint": hashlib.sha256((first["pool_address"] + ":" + first["position_address"]).encode()).hexdigest(),
            "earned_fee_atoms_gross": [sum(i["earned_fee_atoms_gross"][a] for i in intervals) for a in range(2)],
            "claimed_fee_delta_atoms_gross": [sum(i["claimed_fee_delta_atoms_gross"][a] for i in intervals) for a in range(2)],
            "net_pnl": None, "net_return": None, "strategy_promotable": False, "financial_execution": False,
            "policy_comparison": {"status": "unavailable", "policies": ["keep", "narrow", "rebalance", "close"],
                                  "missing": ["ordered swap and liquidity-action tape", "causal executable rebalance/liquidation quotes",
                                              "network/rent/transfer-fee cashflows", "starting capital and external-flow reconciliation"]},
            "limitations": ["Endpoint shares can match despite intervening remove/add actions.",
                            "Gross fees include neither transfer deductions nor network costs.",
                            "Observed in-range samples do not establish continuous time in range.",
                            "Principal changes cannot be attributed to swaps alone without action receipts.",
                            "No policy winner, APR reconstruction or profitability claim is produced."]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", type=Path, nargs="+")
    args = parser.parse_args()
    try:
        if not 2 <= len(args.captures) <= MAX_CAPTURES or sum(p.stat().st_size for p in args.captures) > MAX_TOTAL_BYTES:
            raise DecodeError("capture_file_budget_exceeded")
        inputs = [load_trace(p) for p in args.captures]
        report = compare([v for v, _ in inputs])
        report["capture_sha256"] = [digest for _, digest in inputs]
    except (DecodeError, ReplayError, OSError) as exc:
        parser.exit(2, f"replay refused: {exc}\n")
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
