"""Arithmetic, conservation and failure controls for offline LP accounting; no providers."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from kaiba.learning.lp_inventory_replay import VERSION, ReplayError, load_trace, replay


def ev(ident, at, kind, prices=("10", "100"), active=0, **kwargs):
    return {"id": ident, "at_ms": at, "kind": kind, "prices_usd": list(prices), "active_bin": active,
            "cost_quote": {"network": "0", "rent_spent": "0", "other": "0"}, **kwargs}


def checkpoint(at, wallet, lp=("0", "0"), pending=("0", "0"), locked="0", prices=("10", "100")):
    return ev("end", at, "checkpoint", prices=prices, terminal=True,
              expected={"wallet": list(wallet), "lp": list(lp), "pending_fees": list(pending), "locked_quote": locked})


def trace(events):
    return {"schema": VERSION, "quote_asset": "SOL", "policy": {"id": "predeclared_fixture", "declared_ms": 0},
            "evidence": {"kind": "observed", "source_sha256": "a" * 64,
                         "inventory_complete": True, "fees_complete": True, "costs_complete": True},
            "initial": {"at_ms": 0, "wallet": ["10", "2"], "prices_usd": ["10", "100"]}, "events": events}


def test_positive_fees_can_lose_against_sol_cash_and_the_starting_basket():
    # Deposit $200 and keep $100 idle. Pool swaps 1 SOL for 20 tokens before a price collapse.
    # The $10 fee does not offset $170 of inventory/mark losses plus $1 of costs.
    t = trace([
        ev("open", 0, "deposit", bin=0, amount=["10", "1"]),
        ev("swap", 1000, "inventory_swap", bin=0, delta=["20", "-1"], prices=("5", "100")),
        ev("fee", 2000, "fee", amount=["0", "0.1"], prices=("5", "100")),
        ev("withdraw", 3000, "withdraw", bin=0, amount=["30", "0"], prices=("1", "100"),
           cost_quote={"network": "0.01", "rent_spent": "0", "other": "0"}),
        ev("claim", 3000, "claim", amount=["0", "0.1"], prices=("1", "100")),
        checkpoint(4000, ["30", "1.09"], prices=("1", "100")),
    ])
    out = replay(t)
    assert Decimal(out["fees_at_accrual_usd"]) == 10
    assert Decimal(out["ending_usd_known_costs"]) == 139
    assert Decimal(out["net_pnl_usd"]) == -161
    assert Decimal(out["inventory_and_mark_change_usd"]) == -170
    # It loses against original basket ($210), SOL ($300) and USD ($300),
    # but beats an all-token benchmark ($30). Never collapse these different benchmarks.
    assert Decimal(out["vs_initial_basket_usd_known_costs"]) == -71
    assert Decimal(out["vs_all_quote_usd_known_costs"]) == -161
    assert Decimal(out["vs_all_token_usd_known_costs"]) == 109
    assert out["strategy_promotable"] is False and out["chain_verified"] is False


def test_claim_does_not_duplicate_fees_and_partial_withdrawal_conserves_inventory():
    t = trace([ev("open", 0, "deposit", bin=-1, amount=["10", "1"], active=-1),
               ev("fee", 1000, "fee", amount=["1", "0.1"]),
               ev("half", 2000, "withdraw", bin=-1, amount=["5", "0.5"]),
               ev("claim", 2000, "claim", amount=["1", "0.1"]),
               checkpoint(3000, ["6", "1.6"], lp=("5", "0.5"))])
    out = replay(t)
    assert Decimal(out["net_pnl_usd"]) == 20
    assert Decimal(out["inventory_and_mark_change_usd"]) == 0
    assert out["ending"]["pending_fees"] == ["0", "0.0"]


def test_refundable_rent_is_locked_capital_not_a_cost_or_profit():
    t = trace([ev("rent", 0, "rent_lock", amount_quote="0.1"),
               ev("return", 86_400_000, "rent_refund", amount_quote="0.1"),
               checkpoint(86_400_001, ["10", "2"])])
    out = replay(t)
    assert Decimal(out["capital_usd_days"]["locked_rent"]) == 10
    assert Decimal(out["net_pnl_usd"]) == 0
    assert out["costs_usd"]["rent_spent"] == "0"


def test_nonrefundable_rent_and_network_cost_are_charged_once():
    t = trace([ev("cost", 0, "mark", cost_quote={"network": "0.01", "rent_spent": "0.07", "other": "0"}),
               checkpoint(1000, ["10", "1.92"])])
    out = replay(t)
    assert Decimal(out["net_pnl_usd"]) == -8
    assert Decimal(out["inventory_and_mark_change_usd"]) == 0


def test_out_of_range_and_idle_capital_are_separate():
    t = trace([ev("open", 0, "deposit", bin=0, amount=["10", "1"], active=0),
               ev("move", 86_400_000, "mark", active=1),
               checkpoint(172_800_000, ["0", "1"], lp=("10", "1"))])
    out = replay(t)
    assert out["capital_usd_days"]["lp"] == "400"
    assert out["capital_usd_days"]["wallet"] == "200"
    assert out["capital_usd_days"]["active_bin"] == "200"
    assert out["active_share_of_lp_capital_time"] == "0.5"


@pytest.mark.parametrize("gap", ["inventory_complete", "fees_complete", "costs_complete", "unknown", "modeled", "late"])
def test_missing_evidence_withholds_net_but_preserves_known_accounting(gap):
    t = trace([checkpoint(1000, ["10", "2"])])
    if gap in t["evidence"]:
        t["evidence"][gap] = False
    elif gap == "unknown":
        t["events"][0]["cost_quote"] = None
    elif gap == "modeled":
        t["evidence"]["kind"] = "modeled"
    else:
        t["policy"]["declared_ms"] = 1
    out = replay(t)
    assert out["accounting_reconciled"] is True
    assert out["net_available"] is False and out["evidence_gaps"]
    assert out["net_pnl_usd"] is None and out["net_return"] is None
    assert Decimal(out["pnl_usd_known_costs"]) == 0


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1", "1e999999", "0e-999999", True, 0.1, None])
def test_invalid_quantities_are_refused(bad):
    t = trace([checkpoint(1000, ["10", "2"])])
    t["initial"]["wallet"][0] = bad
    with pytest.raises(ReplayError):
        replay(t)


def test_duplicate_receipt_out_of_order_and_unreconciled_close_are_refused():
    t = trace([ev("same", 10, "mark"), checkpoint(1000, ["10", "2"])])
    for mutation in ("duplicate", "time", "balance", "unfinished", "post_close"):
        bad = copy.deepcopy(t)
        if mutation == "duplicate":
            bad["events"][-1]["id"] = "same"
        elif mutation == "time":
            bad["events"][-1]["at_ms"] = 9
        elif mutation == "balance":
            bad["events"][-1]["expected"]["wallet"][0] = "11"
        elif mutation == "unfinished":
            bad["events"][-1]["terminal"] = False
        else:
            bad["events"].append(ev("extra", 2000, "mark"))
        with pytest.raises(ReplayError):
            replay(bad)


@pytest.mark.parametrize("kind,payload", [
    ("withdraw", {"bin": 0, "amount": ["1", "0"]}),
    ("claim", {"amount": ["1", "0"]}),
    ("rent_refund", {"amount_quote": "0.1"}),
    ("inventory_swap", {"bin": 0, "delta": ["1", "-1"]}),
    ("wallet_swap", {"delta": ["1", "1"]}),
])
def test_no_inventory_can_be_created_by_withdrawing_claiming_refunding_or_bad_swaps(kind, payload):
    t = trace([ev("bad", 0, kind, **payload), checkpoint(1000, ["10", "2"])])
    with pytest.raises(ReplayError):
        replay(t)


def test_observed_rebalance_costs_can_make_an_otherwise_flat_path_lose():
    t = trace([ev("open", 0, "deposit", bin=0, amount=["10", "1"]),
               ev("remove", 100, "withdraw", bin=0, amount=["10", "1"],
                  cost_quote={"network": "0.01", "rent_spent": "0", "other": "0"}),
               ev("reallocate", 100, "wallet_swap", delta=["-5", "0.49"]),
               ev("reopen", 100, "deposit", bin=1, amount=["5", "1.49"], active=1),
               checkpoint(1000, ["0", "0.99"], lp=("5", "1.49"))])
    out = replay(t)
    assert Decimal(out["net_pnl_usd"]) == -2
    assert Decimal(out["swap_value_change_at_event_marks_usd"]) == -1
    assert Decimal(out["costs_usd"]["network"]) == 1


def test_sol_price_changes_are_in_benchmarks():
    t = trace([checkpoint(1000, ["10", "2"], prices=("10", "50"))])
    out = replay(t)
    assert Decimal(out["net_pnl_usd"]) == -100
    assert Decimal(out["vs_initial_basket_usd_known_costs"]) == 0
    assert Decimal(out["vs_all_quote_usd_known_costs"]) == 50


def test_cli_and_byte_bounded_duplicate_key_loader(tmp_path: Path):
    p = tmp_path / "trace.json"
    p.write_text(json.dumps(trace([checkpoint(1000, ["10", "2"])])), encoding="utf-8")
    run = subprocess.run([sys.executable, "-m", "kaiba.learning.lp_inventory_replay", str(p)],
                         capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["accounting_reconciled"] is True
    p.write_text('{"a": 1, "a": 2}', encoding="utf-8")
    with pytest.raises(ReplayError):
        load_trace(p)
    p.write_bytes(b" " * 4_000_001)
    with pytest.raises(ReplayError, match="byte budget"):
        load_trace(p)
