"""Decompose a closed LP paper result and calculate a static holding hurdle."""
from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

VERSION = "lp-paper-payoff/1"
LAMPORTAMOUNT = Decimal("0.000000001")


def _number(value: object) -> Decimal:
    if type(value) not in (str, int, float) or len(str(value)) > 80:
        raise ValueError("missing_or_invalid_number")
    amount = Decimal(str(value))
    if not amount.is_finite() or abs(amount) > 10**9 or abs(amount.as_tuple().exponent) > 80:
        raise ValueError("invalid_number")
    return amount


def assess(row: dict) -> dict:
    if row["arm_status"] != "closed" or type(row["reopened"]) is not int or row["reopened"] != 0:
        raise ValueError("requires_closed_non_reopened_arm")
    ticket, token_fraction = _number(row["ticket_sol"]), _number(row["starting_token_fraction"])
    if ticket <= 0 or not 0 <= token_fraction <= 1:
        raise ValueError("invalid_starting_basket")
    net = _number(row["net_sol"])
    fee, inventory, exit_cost = (_number(row[key]) * ticket / 100
                                 for key in ("fee_pct", "inventory_pct", "exit_cost_pct"))
    tx, rent = _number(row["tx_sol"]), _number(row["rent_sol"])
    if min(fee, exit_cost, tx, rent) < 0:
        raise ValueError("negative_fee_or_cost")
    charges = exit_cost + tx + rent
    if abs(net - (inventory + fee - charges)) > LAMPORTAMOUNT:
        raise ValueError("paper_components_do_not_reconcile")
    return {
        "version": VERSION, "basis": "recorded_lp_paper_model", "financial_execution": False,
        "profit_proven": False, "net_sol": str(net), "gross_fee_mark_sol": str(fee),
        "inventory_change_sol": str(inventory), "exit_network_and_rent_sol": str(charges),
        "net_with_flat_inventory_sol": str(fee - charges),
        "net_without_gross_fee_mark_sol": str(net - fee),
        "static_hold_matching_token_return_pct": str(net * 100 / (ticket * token_fraction))
        if token_fraction else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    args = parser.parse_args(argv)
    try:
        with args.capture.open("rb") as stream:
            data = stream.read(16 * 1024 + 1)
        if len(data) > 16 * 1024:
            raise ValueError("capture_too_large")
        report = assess(json.loads(data))
    except (OSError, ValueError, InvalidOperation, TypeError, KeyError, RecursionError):
        print(json.dumps({"version": VERSION, "status": "unavailable", "financial_execution": False}))
        return 2
    print(json.dumps(report, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
