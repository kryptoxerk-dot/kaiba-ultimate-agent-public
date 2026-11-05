"""History coverage/cash-flow checks, independent of the public RPC and live databases."""

import base64
import copy
from decimal import Decimal

import base58
import pytest

from kaiba.learning.dlmm_history import _marked_hold_comparison, reconcile
from kaiba.learning.dlmm_state import DecodeError
from tests.test_dlmm_actions import KEYS, POSITION, SIGNATURE, WSOL, transaction
from tests.test_dlmm_state import fixture


def receipt(method, params, result, clock=1100):
    return {"started_ms": clock, "received_ms": clock + 1,
            "request": {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            "response": {"jsonrpc": "2.0", "id": 1, "result": result}}


def history():
    capture = fixture(pending=0, growth=0)
    pool = capture["response"]["result"]["value"][0]
    raw = bytearray(base64.b64decode(pool["data"][0]))
    raw[120:152] = base58.b58decode(WSOL)
    pool["data"][0] = base64.b64encode(raw).decode()
    capture["response"]["result"]["value"][1]["lamports"] = 500
    row = {"signature": SIGNATURE, "slot": 90, "blockTime": 900, "confirmationStatus": "finalized", "err": None}
    options = {"commitment": "finalized", "limit": 100, "minContextSlot": 100}
    page = receipt("getSignaturesForAddress", [POSITION, options], [row])
    terminal = receipt("getSignaturesForAddress", [POSITION, {**options, "before": SIGNATURE}], [], clock=1200)
    tx = receipt("getTransaction", [SIGNATURE, {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}], transaction())
    return capture, [page, terminal], [tx]


def test_birth_to_snapshot_provider_index_coverage_and_known_costs():
    out = reconcile(*history())
    assert out["provider_index_coverage_reconciled"] and out["gaps"] == []
    assert out["deposits_atoms"] == [100, 200]
    assert out["observed_owner_network_fee_lamports"] == 5
    assert out["observed_other_native_outflows_lamports"] == 11
    assert out["position_account_funding_lamports"] == 500
    comparison = out["marked_hold_comparison"]
    assert Decimal(comparison["hold_contributed_assets_value_y_atoms"]) == 300
    assert Decimal(comparison["lp_and_distributions_value_y_atoms_gross"]) == 28
    assert Decimal(comparison["lp_minus_hold_after_network_fees_y_atoms"]) == -277
    assert Decimal(comparison["lp_minus_hold_after_all_observed_native_debits_y_atoms"]) == -288
    assert {key: Decimal(value) for key, value in comparison["fee_hurdle_at_mark"].items()} == {
        "principal_and_withdrawals_minus_hold_y_atoms": Decimal(-272),
        "gross_fees_needed_after_network_y_atoms": Decimal(277),
        "additional_fees_needed_after_network_y_atoms": Decimal(277),
        "additional_fees_needed_after_all_observed_debits_y_atoms": Decimal(288),
    }
    assert out["net_pnl"] is None and not out["strategy_promotable"] and not out["chain_verified"]


def test_more_contributed_capital_is_not_profit():
    before = reconcile(*history())
    capture, pages, txs = history()
    txs[0]["response"]["result"] = transaction(y=300)
    after = reconcile(capture, pages, txs)
    a, b = before["marked_hold_comparison"], after["marked_hold_comparison"]
    assert Decimal(b["hold_contributed_assets_value_y_atoms"]) - Decimal(a["hold_contributed_assets_value_y_atoms"]) == 100
    assert Decimal(b["gross_lp_minus_hold_y_atoms"]) - Decimal(a["gross_lp_minus_hold_y_atoms"]) == -100


@pytest.mark.parametrize("missing", ["terminal", "receipt", "null_transaction", "creation", "claimed_counter", "funding"])
def test_missing_evidence_never_becomes_complete_or_a_marked_result(missing):
    capture, pages, txs = history()
    if missing == "terminal":
        pages.pop()
    elif missing == "receipt":
        txs.clear()
    elif missing == "null_transaction":
        txs[0]["response"]["result"] = None
    elif missing == "creation":
        txs[0]["response"]["result"]["meta"]["innerInstructions"][0]["instructions"].pop(0)
    elif missing == "claimed_counter":
        replacement = fixture(claimed=1)["response"]["result"]["value"][1]
        replacement["lamports"] = 500
        capture["response"]["result"]["value"][1] = replacement
    else:
        capture["response"]["result"]["value"][1]["lamports"] = 501
    out = reconcile(capture, pages, txs)
    assert out["gaps"] and not out["provider_index_coverage_reconciled"]
    assert out["marked_hold_comparison"]["status"] == "unavailable"
    assert "fee_hurdle_at_mark" not in out["marked_hold_comparison"]


def test_fee_hurdle_counts_claims_and_pending_once_and_clamps_surplus():
    state = {"pool": {"mints": ["token", WSOL], "active_bin": 0},
             "pool_bins": [{"id": 0, "price_q64": 2**63}],
             "principal_atoms": [80, 20], "fee_atoms_gross": [8, 12]}
    out = _marked_hold_comparison(state, [100, 30], [0, 10], [4, 0], 5, 7, True)
    hurdle = {key: Decimal(value) for key, value in out["fee_hurdle_at_mark"].items()}
    assert Decimal(out["gross_fee_value_y_atoms"]) == 18
    assert hurdle["principal_and_withdrawals_minus_hold_y_atoms"] == -10
    assert hurdle["gross_fees_needed_after_network_y_atoms"] == 15
    assert hurdle["additional_fees_needed_after_network_y_atoms"] == 0
    assert hurdle["additional_fees_needed_after_all_observed_debits_y_atoms"] == 4


@pytest.mark.parametrize("mutation", [
    lambda c, p, t: p[0]["response"].update(id=2),
    lambda c, p, t: p[1]["request"]["params"][1].pop("before"),
    lambda c, p, t: p[0]["request"]["params"][1].update(minContextSlot=99),
    lambda c, p, t: p[0]["request"]["params"][1].update(commitment="processed"),
    lambda c, p, t: p[0]["request"]["params"][1].update(until=SIGNATURE),
    lambda c, p, t: p[0]["response"]["result"].append(copy.deepcopy(p[0]["response"]["result"][0])),
    lambda c, p, t: t[0]["response"]["result"].update(slot=89),
    lambda c, p, t: t[0]["response"]["result"].update(blockTime=899),
    lambda c, p, t: t[0]["request"]["params"][1].update(maxSupportedTransactionVersion=False),
    lambda c, p, t: t.append(copy.deepcopy(t[0])),
    lambda c, p, t: p[0].update(started_ms=999),
])
def test_wrong_identity_scope_order_or_clocks_are_refused(mutation):
    c, p, t = history()
    mutation(c, p, t)
    with pytest.raises((DecodeError, ValueError)):
        reconcile(c, p, t)


def test_same_slot_order_requires_additional_evidence():
    c, pages, t = history()
    row = copy.deepcopy(pages[0]["response"]["result"][0])
    row["signature"] = base58.b58encode(bytes([18]) * 64).decode()
    pages[0]["response"]["result"].append(row)
    with pytest.raises(DecodeError, match="slot_order"):
        reconcile(c, pages, t)


def test_failed_fee_is_included_without_counting_rolled_back_deposits():
    c, pages, txs = history()
    sig = base58.b58encode(bytes([18]) * 64).decode()
    failed = copy.deepcopy(txs[0])
    failed["request"]["params"][0] = sig
    result = failed["response"]["result"]
    result.update(slot=91, blockTime=910)
    result["transaction"]["signatures"] = [sig]
    result["meta"]["err"] = {"InstructionError": [0, "InvalidArgument"]}
    result["meta"]["postBalances"] = list(result["meta"]["preBalances"])
    result["meta"]["postBalances"][0] -= 5
    pages[0]["response"]["result"].insert(0, {"signature": sig, "slot": 91, "blockTime": 910, "confirmationStatus": "finalized", "err": result["meta"]["err"]})
    out = reconcile(c, pages, [*txs, failed])
    assert out["provider_index_coverage_reconciled"]
    assert out["deposits_atoms"] == [100, 200] and out["observed_owner_network_fee_lamports"] == 10
    assert out["failed_transactions"] == 1
    result["meta"]["postBalances"][KEYS.index(POSITION)] += 1
    assert not reconcile(c, pages, [*txs, failed])["provider_index_coverage_reconciled"]
