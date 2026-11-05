"""Economic outcomes must not manufacture a profitable training label.

All provider evidence here is explicit synthetic proof. The integration case uses
the real PaperBroker on an isolated pytest database, never a funded database.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal, localcontext

import pytest

from kaiba.learning.economic_outcomes import PAPER_CONVENTION, project_episode, project_outcomes


def _flow(oid, tx, kind, delta, qty, *, chain="sol", pid="p1"):
    result = {
        "order_id": oid, "tx_hash": tx, "chain": chain, "kind": kind,
        "position_id": pid, "wallet": "wallet", "token": "token",
        "economic_native_delta": str(delta), "wallet_token_delta": str(qty),
        "delta_basis": "sol_owned_accounts_observed" if chain == "sol" else "wallet_balance_observed",
        "identity_verified": True, "attribution_proven": True, "native_quote_verified": True,
        "ownership_complete": True, "transfers_classified": True, "rent_classified": True,
        "fee_inclusion": "all_inclusive", "transaction_success": kind != "failed_attempt",
    }
    if kind == "entry":
        result.update(entry_outflow_native=str(-delta), entry_outflow_basis="observed_native_wallet_outflow")
    return result


def _order(oid, tx, side, *, chain="sol", amount=1000, filled=100):
    return {"order_id": oid, "tx_hash": tx, "chain": chain, "token": "token", "side": side,
            "state": "filled", "mode": "live", "amount_in": str(amount), "filled_out": str(filled),
            "fee_native": "20"}


def packet(*, chain="sol", cost=1000, proceeds=900):
    return {
        "position": {"position_id": "p1", "chain": chain, "token": "token", "mode": "live",
                     "lane": "sm-trenches", "qty": "0", "qty_total": "100", "cost_native": str(cost),
                     "proceeds_native": str(proceeds), "realized_native": str(proceeds - cost),
                     "opened_ms": 100, "closed_ms": 200},
        "orders": [_order("buy", "tx-buy", "buy", chain=chain, amount=cost),
                   _order("sell", "tx-sell", "sell", chain=chain, amount=100, filled=proceeds)],
        "trades": [{"pnl_native": str(proceeds - cost), "fees_native": "40"}],
        "cashflows": [_flow("buy", "tx-buy", "entry", -cost, 100, chain=chain),
                      _flow("sell", "tx-sell", "exit", proceeds, -100, chain=chain)],
        "identity": {"wallet": "wallet", "chain": chain, "token": "token", "verified": True},
        "inventory": {"opening_quantity": "0", "qty_remaining": "0", "quantity_reconciled": True,
                      "transfers_complete": True},
        "coverage": dict.fromkeys(("orders_complete", "attempts_complete", "recoveries_complete",
                                   "inventory_complete"), True),
        "attempts": [], "recoveries": [],
    }


def test_positive_control_is_a_measured_loss_and_does_not_deduct_fees_twice():
    source = packet()
    source["fill_costs"] = [{"order_id": "buy", "chain": "sol", "signature": "tx-buy",
                             "wallet_native_delta": "-1000", "network_fee_native": "10",
                             "priority_fee_native": "7", "venue_fee_native": "20",
                             "router_fee_native": "10", "total_cost_native": "45.123",
                             "reconciled": 1}]
    result = project_episode(source)
    assert result["unknown_reasons"] == []
    assert result["economic_complete"]
    assert not result["model_complete"]
    assert result["economic_pnl_native"] == "-100"
    assert result["economic_return_pct"] == "-10"
    assert result["entry_outflow_native"] == "1000"
    assert result["decoded_fill_evidence"][0]["total_cost_native"] == "45.123"
    assert result["fee_inclusion"]["priority_fee_native"] == "subset_of_network_fee_never_added"


@pytest.mark.parametrize("qualifier", [{"future_timestamps": ["computed_ms"]},
                                      {"identity_mismatches": ["chain"]},
                                      {"position_identity_verified": False}])
def test_unavailable_raw_decoder_rows_do_not_become_known_current_fees(qualifier):
    source = packet()
    source["fill_costs"] = [{"order_id": "buy", "chain": "sol", "router_fee_native": "100",
        "wallet_native_delta": "-1000", "total_cost_native": "200.125", "reconciled": 1,
        "_capture_qualifications": {"available_at_frontier": "position_identity_verified" in qualifier, **qualifier}}]
    result = project_episode(source)
    decoded = result["decoded_fill_evidence"][0]
    assert not decoded["available_at_projection_frontier"]
    assert decoded["capture_qualifications"][next(iter(qualifier))] == next(iter(qualifier.values()))
    assert decoded["router_fee_native"] is None and decoded["wallet_native_delta"] is None
    assert decoded["total_cost_native"] is None
    # Independent inclusive cashflow proofs, rather than this explanatory decoder,
    # still determine economics; fee fields are never deducted twice.
    assert result["economic_pnl_native"] == "-100"
    source["fill_costs"][0].pop("_capture_qualifications")
    available = project_episode(source)["decoded_fill_evidence"][0]
    assert available["available_at_projection_frontier"] and available["router_fee_native"] == "100"


@pytest.mark.parametrize("field,reason", [
    ("identity_verified", "cashflow_wallet_or_token_unproved"),
    ("attribution_proven", "cashflow_attribution_unproved"),
    ("native_quote_verified", "cashflow_native_quote_unproved"),
    ("ownership_complete", "cashflow_transfers_or_ownership_unproved"),
    ("transfers_classified", "cashflow_transfers_or_ownership_unproved"),
    ("rent_classified", "cashflow_rent_classification_missing"),
    ("transaction_success", "cashflow_transaction_success_unproved"),
])
def test_removing_each_proof_blocks_economic_training_labels(field, reason):
    source = packet()
    source["cashflows"][0].pop(field)
    result = project_episode(source)
    assert not result["economic_complete"]
    assert result["economic_pnl_native"] is None
    assert result["economic_return_pct"] is None
    assert reason in result["unknown_reasons"]


@pytest.mark.parametrize("field", ["orders_complete", "attempts_complete", "recoveries_complete", "inventory_complete"])
def test_missing_population_coverage_is_unknown(field):
    source = packet()
    source["coverage"].pop(field)
    result = project_episode(source)
    assert result["verdict"] == "UNPROVEN"
    assert f"{field}_unproved" in result["unknown_reasons"]


@pytest.mark.parametrize("value", [True, 1000.0, "1000.0", None])
def test_money_requires_exact_integer_atoms(value):
    source = packet()
    source["cashflows"][0]["entry_outflow_native"] = value
    result = project_episode(source)
    assert result["net_pnl_native"] is None
    assert "authoritative_entry_outflow_missing" in result["unknown_reasons"]


def test_wei_above_signed_64_bit_stays_exact_and_json_serializable():
    source = packet(chain="bsc", cost=2**75, proceeds=2**75 - 123)
    result = project_episode(source)
    assert result["economic_pnl_native"] == "-123"
    assert result["entry_outflow_native"] == str(2**75)
    with localcontext() as context:
        context.prec = 50
        assert Decimal(result["net_return_pct"]) == Decimal(-123) * 100 / Decimal(2**75)
    assert json.loads(json.dumps(result))["entry_outflow_native"] == str(2**75)


def test_reconciled_decoder_rows_alone_are_not_economic_proof():
    source = packet(chain="robinhood")
    source.pop("cashflows")
    source["fill_costs"] = [{"order_id": "buy", "chain": "robinhood", "signature": "tx-buy",
                             "basis": "venue_event", "wallet_native_delta": "-1000", "reconciled": 1}]
    result = project_episode(source)
    assert result["net_pnl_native"] is None
    assert result["known_cashflow_native"] is None
    assert result["decoded_fill_evidence"][0]["delta_basis"] == "evm_gmgn_summary_plus_gas_requires_observation"
    assert not result["decoded_fill_evidence"][0]["economic_completeness_proved_by_reconciled"]


def test_gmgn_summary_cashflow_is_preserved_but_explicitly_unmeasured():
    source = packet(chain="robinhood")
    source["cashflows"][0]["delta_basis"] = "evm_gmgn_summary_plus_gas"
    result = project_episode(source)
    assert result["known_cashflow_native"] == "-100"
    assert not result["economic_complete"]
    assert "cashflow_delta_basis_unmeasured" in result["unknown_reasons"]


@pytest.mark.parametrize("chain", ["sol", "bsc", "robinhood"])
@pytest.mark.parametrize("state", ["filled", "partial"])
def test_filled_order_cannot_be_certified_by_its_failed_transaction(chain, state):
    source = packet(chain=chain)
    source["orders"][0]["state"] = state
    source["cashflows"][0] = _flow("buy", "tx-buy", "failed_attempt", -30, 0, chain=chain)
    # A separately attributed entry keeps the episode quantity and denominator
    # coherent, exposing the missing success proof for the actual FILLED order.
    source["cashflows"].append(_flow(None, "other-entry", "entry", -1000, 100, chain=chain))
    result = project_episode(source)
    assert result["known_cashflow_native"] == "-130"
    assert result["failed_attempt_cost_native_known_subset"] == "30"
    assert "filled_order_cashflow_missing" in result["unknown_reasons"]
    assert result["verdict"] == "UNPROVEN" and result["economic_pnl_native"] is None

    # Positive control: a distinct failed attempt does not disqualify a proved
    # successful entry. Its gas remains charged exactly once.
    source = packet(chain=chain)
    source["orders"][0]["state"] = state
    source["attempts"] = [{"order_id": "failed", "position_id": "p1", "chain": chain,
                           "state": "failed", "tx_hash": "failed-tx"}]
    source["cashflows"].append(_flow("failed", "failed-tx", "failed_attempt", -30, 0, chain=chain))
    result = project_episode(source)
    assert result["unknown_reasons"] == []
    assert result["verdict"] == "ECONOMIC_COMPLETE" and result["economic_pnl_native"] == "-130"


@pytest.mark.parametrize("chain,basis,entry_basis", [
    ("sol", "sol_owned_accounts_observed", "observed_native_wallet_outflow"),
    ("sol", "wallet_balance_observed", "observed_native_wallet_outflow"),
    ("bsc", "wallet_balance_observed", "observed_native_wallet_outflow"),
    ("bsc", "evm_native_trace_observed", "observed_native_trace_outflow"),
    ("robinhood", "wallet_balance_observed", "observed_native_wallet_outflow"),
    ("robinhood", "evm_native_trace_observed", "observed_native_trace_outflow"),
])
def test_chain_appropriate_measured_cashflow_provenance_remains_accepted(chain, basis, entry_basis):
    source = packet(chain=chain)
    for flow in source["cashflows"]:
        flow["delta_basis"] = basis
    source["cashflows"][0]["entry_outflow_basis"] = entry_basis
    result = project_episode(source)
    assert result["unknown_reasons"] == []
    assert result["economic_complete"] and result["economic_pnl_native"] == "-100"


@pytest.mark.parametrize("chain,basis", [("sol", "evm_native_trace_observed"),
                                         ("bsc", "sol_owned_accounts_observed"),
                                         ("robinhood", "sol_owned_accounts_observed")])
def test_chain_conflicting_native_cashflow_provenance_is_not_economic_proof(chain, basis):
    source = packet(chain=chain)
    source["cashflows"][0]["delta_basis"] = basis
    result = project_episode(source)
    assert result["known_cashflow_native"] == "-100"
    assert "cashflow_delta_basis_unmeasured" in result["unknown_reasons"]
    assert not result["economic_complete"] and result["economic_pnl_native"] is None


def test_solana_entry_denominator_cannot_claim_evm_trace_provenance():
    source = packet()
    source["cashflows"][0]["entry_outflow_basis"] = "observed_native_trace_outflow"
    result = project_episode(source)
    assert "authoritative_entry_outflow_missing" in result["unknown_reasons"]
    assert not result["economic_complete"] and result["economic_return_pct"] is None


def test_unrelated_sol_transfer_cannot_be_disguised_as_a_router_fee():
    source = packet()
    source["cashflows"][0]["unrelated_transfers"] = [{"recipient": "stranger", "lamports": "100"}]
    source["fill_costs"] = [{"reconciled": 1, "router_fee_native": "100"}]
    result = project_episode(source)
    assert not result["economic_complete"]
    assert "unrelated_or_unknown_transfers_present" in result["unknown_reasons"]


@pytest.mark.parametrize("mutate,reason", [
    (lambda p: p["cashflows"][0].update(wallet="other"), "cashflow_wallet_or_token_unproved"),
    (lambda p: p["cashflows"][0].update(tx_hash="other"), "cashflow_order_transaction_mismatch"),
    (lambda p: p["cashflows"][0].update(chain="bsc"), "cashflow_transaction_identity_missing_or_mismatched"),
    (lambda p: p["cashflows"][0].update(fee_inclusion="exclusive"), "cashflow_fee_inclusion_unproved"),
    (lambda p: p["cashflows"][0].update(wallet_token_delta="101"), "transaction_inventory_quantity_mismatch"),
    (lambda p: p["identity"].update(verified=1), "expected_trading_wallet_identity_unproved"),
])
def test_identity_inclusion_and_quantity_adversaries(mutate, reason):
    source = packet()
    mutate(source)
    result = project_episode(source)
    assert reason in result["unknown_reasons"]
    assert not result["economic_complete"]


def test_bsc_non_native_swap_route_cannot_be_counted_as_native_profit():
    source = packet(chain="bsc")
    source["cashflows"][1]["native_quote_verified"] = False
    assert "cashflow_native_quote_unproved" in project_episode(source)["unknown_reasons"]


def test_multiple_entries_and_trims_use_all_entry_outflows():
    source = packet(cost=1500, proceeds=1300)
    source["position"]["qty_total"] = "150"
    source["orders"] = [_order("b1", "t1", "buy"), _order("b2", "t2", "buy"),
                        _order("s1", "t3", "sell"), _order("s2", "t4", "sell")]
    source["cashflows"] = [_flow("b1", "t1", "entry", -1000, 100), _flow("b2", "t2", "entry", -500, 50),
                           _flow("s1", "t3", "exit", 600, -50), _flow("s2", "t4", "exit", 700, -100)]
    result = project_episode(source)
    assert result["economic_complete"]
    assert result["economic_pnl_native"] == "-200"
    assert result["entry_outflow_native"] == "1500"


def test_projection_is_pure_and_repeated_transactions_are_charged_once():
    source = packet()
    original = copy.deepcopy(source)
    first = project_episode(source)
    assert source == original
    assert first == project_episode(source)
    source["cashflows"].append(copy.deepcopy(source["cashflows"][0]))
    result = project_episode(source)
    assert result["economic_pnl_native"] == first["economic_pnl_native"]
    assert result["transaction_count"] == 2


def test_conflicting_transactions_preserve_unknown_instead_of_arbitrary_first_value():
    source = packet()
    duplicate = copy.deepcopy(source["cashflows"][0])
    duplicate["economic_native_delta"] = "-900"
    source["cashflows"].append(duplicate)
    result = project_episode(source)
    assert "conflicting_transaction_rows" in result["unknown_reasons"]
    assert result["transactions"][0]["delta_native"] is None
    assert result["economic_pnl_native"] is None


def test_two_orders_in_one_transaction_need_whole_episode_allocation():
    source = packet()
    source["orders"].append(_order("buy2", "tx-buy", "buy"))
    source["cashflows"][0]["order_ids"] = ["buy", "buy2"]
    assert "shared_order_transaction_allocation_unproved" in project_episode(source)["unknown_reasons"]
    source["cashflows"][0]["allocation"] = {
        "scope": "entire_transaction", "position_id": "p1", "proven": True,
        "order_ids": ["buy", "buy2"],
    }
    result = project_episode(source)
    assert result["economic_complete"]
    assert result["economic_pnl_native"] == "-100"


def test_refundable_rent_is_cash_lock_then_recovery_not_loss_then_profit():
    source = packet()
    source["cashflows"][0].update(cash_native_delta="-1020", rent_locked_native="20")
    recovery = _flow(None, "rent-close", "rent_recovery", 0, 0)
    recovery.pop("order_id")
    recovery.update(cash_native_delta="20", rent_recovered_native="20")
    source["recoveries"].append(recovery)
    result = project_episode(source)
    assert result["economic_pnl_native"] == "-100"
    assert result["recovery_delta_native_known_subset"] == "0"
    source["recoveries"][0]["economic_native_delta"] = "20"
    assert "rent_recovery_counted_as_profit" in project_episode(source)["unknown_reasons"]


def test_attributable_refund_changes_economics_once():
    source = packet()
    refund = _flow(None, "refund", "recovery", 30, 0)
    refund.pop("order_id")
    source["recoveries"] = [refund, copy.deepcopy(refund)]
    result = project_episode(source)
    assert result["economic_pnl_native"] == "-70"
    assert result["recovery_delta_native_known_subset"] == "30"


def test_reverted_attempt_pays_gas_and_missing_attempt_receipt_blocks_learning():
    source = packet()
    source["attempts"] = [{"order_id": "failed", "position_id": "p1", "chain": "sol",
                           "state": "failed", "tx_hash": "failed-tx"}]
    assert "failed_attempt_cost_missing" in project_episode(source)["unknown_reasons"]
    source["cashflows"].append(_flow("failed", "failed-tx", "failed_attempt", -30, 0))
    result = project_episode(source)
    assert result["economic_pnl_native"] == "-130"
    assert result["failed_attempt_cost_native_known_subset"] == "30"


@pytest.mark.parametrize("state", ["unknown", "submitted", "planned", "refused"])
def test_ambiguous_and_unproved_pre_send_attempts_are_not_free(state):
    source = packet()
    source["attempts"] = [{"attempt_id": "attempt", "position_id": "p1", "state": state}]
    result = project_episode(source)
    assert "unresolved_or_unproved_attempt" in result["unknown_reasons"]
    assert result["economic_pnl_native"] is None


def test_proved_pre_send_refusal_has_zero_chain_cost():
    source = packet()
    source["attempts"] = [{"attempt_id": "attempt", "position_id": "p1", "state": "refused",
                           "no_submission_proven": True}]
    result = project_episode(source)
    assert result["economic_complete"]
    assert result["economic_pnl_native"] == "-100"
    assert result["attempts"]["proved_no_submission_count"] == 1


def test_open_inventory_needs_an_actual_sized_net_quote_at_frontier():
    source = packet()
    source["position"].update(qty="50", closed_ms=None, realized_native="-50", proceeds_native="450")
    source["trades"] = []
    source["cashflows"][1].update(economic_native_delta="450", wallet_token_delta="-50")
    source["inventory"].update(qty_remaining="50", frontier_ms=1000)
    assert "executable_inventory_value_missing" in project_episode(source)["unknown_reasons"]
    source["inventory"]["quote"] = {"chain": "sol", "token": "token", "qty": "50", "net_native": "400",
                                     "at_ms": 1000, "executable": True, "fee_inclusion": "all_inclusive",
                                     "source": "verified-routed-size-quote"}
    result = project_episode(source)
    assert result["economic_complete"]
    assert result["known_cashflow_native"] == "-550"
    assert result["economic_pnl_native"] == "-150"
    assert result["ledger_pnl_native"] == "-50"


@pytest.mark.parametrize("changes", [{"at_ms": 1001}, {"at_ms": -5000}, {"qty": "49"},
                                      {"executable": False}, {"fee_inclusion": "exclusive"}])
def test_future_stale_or_wrong_size_quotes_cannot_pass(changes):
    source = packet()
    source["orders"] = source["orders"][:1]
    source["cashflows"] = source["cashflows"][:1]
    source["trades"] = []
    source["position"].update(qty="100", closed_ms=None, realized_native="0", proceeds_native="0")
    source["inventory"].update(qty_remaining="100", frontier_ms=1000,
                               quote={"chain": "sol", "token": "token", "qty": "100", "net_native": "900",
                                      "at_ms": 1000, "executable": True, "fee_inclusion": "all_inclusive",
                                      "source": "quote"})
    source["inventory"]["quote"].update(changes)
    assert "executable_inventory_value_missing" in project_episode(source)["unknown_reasons"]


@pytest.mark.parametrize("changes,reason", [
    ({"opening_quantity": "100"}, "opening_inventory_or_external_funding_unproved"),
    ({"airdrops": [{"qty": "100"}]}, "external_inventory_movements_present"),
    ({"external_transfers": [{"qty": "100"}]}, "external_inventory_movements_present"),
    ({"quantity_reconciled": False}, "inventory_reconciliation_missing"),
])
def test_external_and_missing_inventory_never_become_trading_profit(changes, reason):
    source = packet()
    source["inventory"].update(changes)
    assert reason in project_episode(source)["unknown_reasons"]


def test_batch_deduplicates_positions_and_does_not_add_lamports_to_wei():
    sol = packet()
    bsc = packet(chain="bsc", cost=10**20, proceeds=10**20 - 1)
    result = project_outcomes([sol, copy.deepcopy(sol), bsc])
    assert result["population_count"] == 2
    assert result["economic_complete_count"] == 2
    assert not result["native_results_combined_across_chains"]
    groups = {row["chain"]: row for row in result["by_chain_mode"]}
    assert groups["sol"]["economic_pnl_native_complete_subset"] == "-100"
    assert groups["bsc"]["economic_pnl_native_complete_subset"] == "-1"
    assert "economic_pnl_native_complete_subset" not in result


def test_batch_rejects_same_full_transaction_charged_to_two_episodes():
    first = packet()
    second = packet()
    second["position"]["position_id"] = "p2"
    for flow in second["cashflows"]:
        flow["position_id"] = "p2"
    result = project_outcomes([first, second])
    assert result["population_count"] == 2
    assert result["economic_complete_count"] == 0
    assert result["excluded_count"] == 2
    assert all("transaction_reused_across_episodes_or_allocations" in row["unknown_reasons"]
               for row in result["outcomes"])


def test_batch_accepts_proved_disjoint_transaction_allocations_and_rejects_false_sums():
    first = packet()
    second = packet(cost=500, proceeds=450)
    second["position"]["position_id"] = "p2"
    second["orders"][1]["tx_hash"] = "exit-p2"
    second["cashflows"][1]["tx_hash"] = "exit-p2"
    parts = [{"key": "part1", "position_id": "p1", "delta_native": "-1000"},
             {"key": "part2", "position_id": "p2", "delta_native": "-500"}]
    for source, key, pid in ((first, "part1", "p1"), (second, "part2", "p2")):
        for flow in source["cashflows"]:
            flow["position_id"] = pid
        source["cashflows"][0]["allocation"] = {
            "scope": "partition", "key": key, "proven": True, "position_id": pid,
            "parts": copy.deepcopy(parts), "transaction_delta_native": "-1500",
        }
    result = project_outcomes([first, second])
    assert result["economic_complete_count"] == 2
    assert result["by_chain_mode"][0]["economic_pnl_native_complete_subset"] == "-150"
    second["cashflows"][0]["allocation"]["transaction_delta_native"] = "-1499"
    result = project_outcomes([first, second])
    assert result["economic_complete_count"] == 0


def test_conflicting_episode_snapshots_are_counted_and_excluded():
    first = packet()
    second = packet(proceeds=901)
    result = project_outcomes([first, second])
    assert result["population_count"] == 1
    assert result["excluded_count"] == 1
    assert "conflicting_episode_packets" in result["outcomes"][0]["unknown_reasons"]


def test_actual_paper_broker_fee_convention_uses_the_inclusive_ledger(tmp_db):
    from kaiba.core.schemas import Action, Chain, Decision, Lane, LaneMode
    from kaiba.execution.paper import PaperBroker

    broker = PaperBroker(tmp_db, fee_bps={Chain.SOL: 100}, tip_native={Chain.SOL: 1_000_000},
                         native_usd={Chain.SOL: Decimal("100")}, slippage_floor_bps=0)
    decision = Decision(decision_id="economic_paper", ts_ms=100, lane=Lane.CONFLUENCE_5,
                        mode=LaneMode.SHADOW, chain=Chain.SOL, token="token", action=Action.ENTER,
                        thesis="fixture", confidence=0.6, size_base_units=1_000_000_000)
    buy = broker.buy(decision, price_usd=Decimal("0.001"), liquidity_usd=Decimal("1000000"),
                     now_ms=100, decimals=6)
    position = broker.open_position(Chain.SOL, "token", Lane.CONFLUENCE_5, LaneMode.SHADOW)
    assert position is not None
    sell = broker.sell(position, 100, price_usd=Decimal("0.001"), liquidity_usd=Decimal("1000000"),
                       now_ms=200, decimals=6)
    position_row = dict(tmp_db.execute("SELECT * FROM positions WHERE position_id=?", (position.position_id,)).fetchone())
    orders = [dict(row) for row in tmp_db.execute("SELECT * FROM orders WHERE order_id IN (?,?)", (buy.order_id, sell.order_id)).fetchall()]
    trades = [dict(row) for row in tmp_db.execute("SELECT * FROM trades WHERE position_id=?", (position.position_id,)).fetchall()]
    source = {"position": position_row, "orders": orders, "trades": trades,
              "coverage": packet()["coverage"], "inventory": packet()["inventory"],
              "paper_model": {"name": "kaiba.execution.paper.PaperBroker", "fee_convention": PAPER_CONVENTION,
                              "complete": True}}
    result = project_episode(source)
    assert result["unknown_reasons"] == []
    assert result["model_complete"]
    assert not result["economic_complete"]
    expected = int(position_row["proceeds_native"]) - int(position_row["cost_native"])
    assert expected < 0
    assert result["model_pnl_native"] == str(expected)
    assert result["entry_outflow_native"] == position_row["cost_native"]
    assert result["economic_pnl_native"] is None
    assert int(trades[0]["fees_native"]) > 0
    assert int(result["model_pnl_native"]) != expected - int(trades[0]["fees_native"])
    # A paper outcome cannot be relabelled as measured live profit.
    source["position"]["mode"] = "live"
    assert not project_episode(source)["model_complete"]


def test_missing_packet_is_an_excluded_record_not_an_exception_or_zero_profit():
    result = project_episode({})
    assert result["economic_pnl_native"] is None
    assert result["model_pnl_native"] is None
    assert result["verdict"] == "UNPROVEN"
    assert project_outcomes([])["population_count"] == 0
