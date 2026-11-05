"""Receipt/quantity controls using the unchanged fixed-study and tape fixtures."""
from __future__ import annotations

import copy
import json

import pytest

from kaiba.learning import wallet_reachability as reach
from tests.test_wallet_observation_tape import T0, WALLET, page_receipt, raw, receipt, setup, slot, tape


def observe(rows, *, end=20_000, capture=30_000, covered=True, wallet=WALLET):
    _, _, state = setup()
    observed = tape.ingest(state, wallet, rows,
        receipt(T0 + 1, T0 + end, capture=T0 + capture), now_ms=T0 + capture)
    assert observed["status"] == "committed", observed
    state = observed["state"]
    if covered:
        observed = tape.ingest(state, wallet, [],
            receipt(T0 + end + 1, T0 + 50_000, capture=T0 + 60_000), now_ms=T0 + 60_000)
        assert observed["status"] == "committed", observed
        state = observed["state"]
    return state


def trade(rowid, offset, *, side="buy", qty="100", **kwargs):
    return raw(rowid, T0 + offset, token="NEW", side=side, qty=qty, **kwargs)


def project(state, *, diagnostics=(), now=T0 + 100_000):
    return reach.project(state, now_ms=now, diagnostics=list(diagnostics) if diagnostics is not None else None)


def member(report, wallet=WALLET):
    return next(w for w in report["wallets"] if (w["chain"], w["wallet"]) == wallet)


def example(report):
    return member(report)["examples"][0]


def test_partial_sale_before_capture_is_quantity_evidence_not_a_long_tail_copy_pass():
    state = observe([trade(10_001, 2000), trade(10_002, 12_000, side="sell", qty="90")])
    original = copy.deepcopy(state)
    result = project(state)
    measured = example(result)
    assert measured["status"] == "observed_retrospective_quantity"
    assert measured["sold_fraction"] == "0.9"
    assert measured["observed_residual_units"] == "10"
    assert measured["first_retained_opening_raw_capture_ms"] == T0 + 30_000
    assert measured["retrospective_adjudication_capture_ms"] == T0 + 60_000
    assert member(result)["open_episode_count"] == 1
    assert member(result)["partially_sold_count"] == 1
    assert member(result)["baseline_or_carry_episode_count"] == 1
    assert len(result["wallets"]) == result["fixed_wallet_denominator"] == 6
    assert result["canonical_arrival"] == result["follower_execution"] == result["profitability_status"] == "UNPROVEN"
    assert result["copy_qualified"] is result["funding_allowed"] is False
    assert state == original


@pytest.mark.parametrize(("sold", "fraction", "counter"), [
    ("0", "0", "no_observed_sale_count"), ("100", "1", "fully_sold_count")])
def test_positive_quantity_controls_keep_no_sale_and_fully_sold_separate(sold, fraction, counter):
    rows = [trade(10_001, 2000)]
    if sold != "0":
        rows.append(trade(10_002, 12_000, side="sell", qty=sold))
    result = project(observe(rows))
    assert example(result)["sold_fraction"] == fraction
    assert member(result)[counter] == member(result)["quantity_evaluated_count"] == 1


def test_later_captured_sale_can_adjudicate_event_before_opening_capture_retrospectively():
    state = observe([trade(10_001, 2000)], end=5000, capture=30_000, covered=False)
    added = tape.ingest(state, WALLET, [trade(10_002, 12_000, side="sell", qty="90")],
        receipt(T0 + 5001, T0 + 50_000, capture=T0 + 60_000), now_ms=T0 + 60_000)
    assert added["status"] == "committed"
    result = project(added["state"])
    assert example(result)["sold_fraction"] == "0.9"
    assert example(result)["first_retained_opening_raw_capture_ms"] == T0 + 30_000
    assert example(result)["retrospective_adjudication_capture_ms"] == T0 + 60_000


def test_complete_frontier_not_latest_capture_or_requested_upper_is_required():
    state = observe([trade(10_001, 2000)], covered=False)
    result = project(state)
    assert member(result)["quantity_evaluated_count"] == 0
    assert "completed_coverage_before_capture" in example(result)["reasons"]
    page = tape.ingest_page(state, WALLET, [trade(10_002, 25_000, side="sell")],
        page_receipt(T0 + 20_001, T0 + 90_000, [None], limit=1, capture=T0 + 95_000), now_ms=T0 + 95_000)
    assert page["status"] == "committed", page
    result = project(page["state"])
    assert slot(page["state"])["latest_as_of_ms"] > T0 + 30_000
    assert member(result)["quantity_evaluated_count"] == 0
    assert "capture_interval_has_open_page" in example(result)["reasons"]


def test_query_gap_through_capture_is_unknown_even_after_completed_frontier_advances():
    state = observe([trade(10_001, 2000)], covered=False)
    added = tape.ingest(state, WALLET, [],
        receipt(T0 + 40_000, T0 + 50_000, capture=T0 + 60_000), now_ms=T0 + 60_000)
    assert added["status"] == "committed"
    result = project(added["state"])
    assert "capture_interval_has_gap" in example(result)["reasons"]
    assert example(result)["sold_fraction"] is None


@pytest.mark.parametrize("diagnostics", [None, [{"reason": "unresolved_source_admission", "wallet": list(WALLET)}],
                                       [{"reason": "unknown_global"}]])
def test_missing_or_relevant_transport_diagnostics_never_grant_coverage(diagnostics):
    result = project(observe([trade(10_001, 2000)]), diagnostics=diagnostics)
    assert member(result)["quantity_evaluated_count"] == 0
    assert example(result)["sold_fraction"] is None


def test_other_wallet_diagnostic_preserves_this_wallets_known_component():
    other = next(w for w in tape.MEMBERS if w != WALLET)
    result = project(observe([trade(10_001, 2000)]), diagnostics=[{"wallet": list(other), "reason": "unavailable"}])
    assert example(result)["sold_fraction"] == "0"


@pytest.mark.parametrize("kind", ["seal", "capture", "receipt", "future", "source"])
def test_original_integrity_receipt_and_clock_guards_are_exercised(kind):
    state = observe([trade(10_001, 2000)])
    if kind == "seal":
        state["state_sha256"] = "0" * 64
    elif kind == "capture":
        slot(state)["rows"][-1]["first_captured_ms"] += 1
        state = tape._seal(state)
    elif kind == "receipt":
        slot(state)["receipts"][-1]["packet_raw_sha256"] = "0" * 64
        state = tape._seal(state)
    elif kind == "source":
        state["source_versions"]["intelligence/grade.py"] = "0" * 64
        state = tape._seal(state)
    result = project(state, now=T0 + 20_000 if kind == "future" else T0 + 100_000)
    assert result["status"] == "unknown"
    assert len(result["wallets"]) == 6
    assert all(w["quantity_evaluated_count"] == 0 for w in result["wallets"])


def test_earliest_retained_duplicate_opening_clock_is_preserved_but_quantity_is_ambiguous():
    first = trade(10_001, 2000)
    state = observe([first], end=5000, capture=10_000, covered=False)
    duplicate = {**first, "id": 10_002}
    added = tape.ingest(state, WALLET, [first, duplicate],
        receipt(T0 + 1, T0 + 50_000, capture=T0 + 60_000), now_ms=T0 + 60_000)
    assert added["status"] == "committed", added
    result = project(added["state"])
    assert example(result)["first_retained_opening_raw_capture_ms"] == T0 + 10_000
    assert example(result)["opening_raw_sha256"] == reach._hash(first)
    assert example(result)["bought_units_through_capture"] is None
    assert "identical_transaction_leg_ambiguity" in example(result)["reasons"]


def test_distinct_opening_transactions_use_earliest_retained_capture_not_latest():
    first = trade(10_001, 2000)
    state = observe([first], end=5000, capture=10_000, covered=False)
    second = trade(10_002, 2000)
    added = tape.ingest(state, WALLET, [first, second],
        receipt(T0 + 1, T0 + 50_000, capture=T0 + 60_000), now_ms=T0 + 60_000)
    assert added["status"] == "committed"
    result = project(added["state"])
    assert example(result)["first_retained_opening_raw_capture_ms"] == T0 + 10_000
    assert example(result)["bought_units_through_capture"] == "200"


def test_identical_transaction_sells_cannot_be_certified_as_one_exact_leg():
    sell = trade(10_002, 12_000, side="sell", qty="45")
    result = project(observe([trade(10_001, 2000), sell, {**sell, "id": 10_003}]))
    assert member(result)["quantity_evaluated_count"] == 0
    assert "identical_transaction_leg_ambiguity" in example(result)["reasons"]
    assert example(result)["sold_fraction"] is None


def test_distinct_quantities_in_same_transaction_side_require_a_leg_identity():
    first = trade(10_002, 12_000, side="sell", qty="45")
    second = {**first, "id": 10_003, "amount_token": "55"}
    result = project(observe([trade(10_001, 2000), first, second]))
    assert member(result)["quantity_evaluated_count"] == 0
    assert "transaction_leg_identity_ambiguous" in example(result)["reasons"]


@pytest.mark.parametrize("variant", ["case", "whitespace"])
def test_canonical_evm_token_cannot_mix_human_and_base_quantity_units(variant):
    wallet = next(w for w in tape.MEMBERS if w[0] == "bsc")
    token = "0x" + "Aa" * 20
    buy = raw(10_001, T0 + 2000, wallet=wallet, token=token, qty="100", source="robinhood")
    sell_token = token.lower() if variant == "case" else " " + token + " "
    sell = raw(10_002, T0 + 12_000, wallet=wallet, token=sell_token, side="sell",
               qty="0.0000001", source="gmgn:smartmoney")
    result = project(observe([buy, sell], wallet=wallet))
    selected = member(result, wallet)
    assert selected["quantity_evaluated_count"] == 0
    assert "canonical_token_quantity_basis_mixture" in selected["examples"][0]["reasons"]


@pytest.mark.parametrize("case", ["same_time", "missing_qty", "missing_tx", "transfer", "mixed_units"])
def test_ambiguous_identity_units_and_movements_stay_unknown(case):
    buy, sell = trade(10_001, 2000), trade(10_002, 12_000, side="sell", qty="90")
    if case == "same_time":
        sell["ts_ms"] = buy["ts_ms"]
    elif case == "missing_qty":
        buy["amount_token"] = None
    elif case == "missing_tx":
        buy["tx"] = None
    elif case == "transfer":
        sell["side"] = "transfer_out"
    else:
        sell["source"] = "gmgn:smartmoney"
    result = project(observe([buy, sell]))
    assert member(result)["quantity_evaluated_count"] == 0
    assert example(result)["sold_fraction"] is None


def test_multiple_adds_use_buys_through_capture_only_and_do_not_pool_token_units():
    rows = [trade(10_001, 2000), trade(10_002, 3000, qty="100"),
            trade(10_003, 12_000, side="sell", qty="150")]
    result = project(observe(rows))
    assert example(result)["bought_units_through_capture"] == "200"
    assert example(result)["sold_units_through_capture"] == "150"
    assert example(result)["sold_fraction"] == "0.75"
    assert "sold_units" not in result


def test_missing_money_preserves_known_quantity_component_without_promoting_economics():
    rows = [trade(10_001, 2000, money=None), trade(10_002, 12_000, side="sell", qty="90", money=None)]
    result = project(observe(rows))
    measured = example(result)
    assert measured["sold_fraction"] == "0.9"
    assert measured["observed_residual_units"] == "10"
    assert measured["money_components_status"] == "unknown"
    assert set(measured["episode_contamination"]) == {"buy_without_native_amount", "sell_without_native_amount"}
    assert result["profitability_status"] == result["complete_custody"] == result["follower_execution"] == "UNPROVEN"
    assert result["funding_allowed"] is False


def test_close_then_rebuy_before_capture_reports_separate_episode_quantities_not_wallet_balance():
    rows = [trade(10_001, 2000), trade(10_002, 3000, side="sell"), trade(10_003, 4000)]
    result = project(observe(rows))
    episodes = member(result)["examples"]
    assert [e["observed_residual_units"] for e in episodes] == ["0", "100"]
    assert [e["sold_fraction"] for e in episodes] == ["1", "0"]
    assert result["policy"]["quantity_scope"] == "one_observed_episode_not_current_wallet_balance"


def test_dust_close_is_not_zero_observed_residual():
    rows = [trade(10_001, 2000, qty="10000"), trade(10_002, 12_000, side="sell", qty="9999")]
    result = project(observe(rows))
    assert example(result)["state"] == "closed"
    assert example(result)["observed_residual_units"] == "1"
    assert member(result)["fully_sold_count"] == 0


def test_projected_clock_does_not_refresh_stable_evidence_identity_or_data_clock():
    state = observe([trade(10_001, 2000)])
    first, later = project(state), project(state, now=T0 + 200_000)
    assert first["evidence_id"] == later["evidence_id"]
    assert member(first)["latest_actual_capture_ms"] == member(later)["latest_actual_capture_ms"] == T0 + 60_000
    assert first["projected_ms"] != later["projected_ms"]


def test_capacity_is_bounded_before_normalizer_expansion(monkeypatch):
    state = observe([trade(10_001, 2000)])
    monkeypatch.setattr(reach, "MAX_NODES", 8)
    result = project(state)
    assert result["status"] == "unknown"
    assert result["reason"] == "structure_cap"


def test_original_association_work_is_bounded_without_silent_row_truncation(monkeypatch):
    state = observe([trade(10_001, 2000), trade(10_002, 3000, side="sell")])
    monkeypatch.setattr(reach, "MAX_ASSOCIATION_WORK", 1)
    result = project(state)
    assert member(result)["reasons"] == ["analysis_work_cap"]
    assert member(result)["quantity_evaluated_count"] == 0
    assert len(result["wallets"]) == 6


def test_duplicate_buys_cannot_hide_actual_analyzer_matrix_from_capacity_guard(monkeypatch):
    rows = []
    for index in range(200):
        buy = trade(20_000 + index * 3, 2000 + index * 40)
        rows.extend([buy, {**buy, "id": buy["id"] + 1},
                     trade(buy["id"] + 2, buy["ts_ms"] - T0 + 10, side="sell")])
    state = observe(rows)
    monkeypatch.setattr(reach, "MAX_ASSOCIATION_WORK", 100_000)
    result = project(state)
    assert member(result)["reasons"] == ["analysis_work_cap"]
    assert member(result)["association_work"] > 100_000


def test_unseeded_reference_is_retained_not_dropped_from_denominator():
    fixed_state, _, _ = setup()
    initialized = tape.initialize(fixed_state, now_ms=T0 + 1000)
    result = project(initialized["state"])
    assert len(result["wallets"]) == 6
    assert all(w["reasons"] == ["baseline_unseeded"] for w in result["wallets"])


def test_report_is_bounded_and_does_not_copy_raw_trades():
    result = project(observe([trade(10_001, 2000)]))
    assert len(json.dumps(result).encode()) < reach.MAX_REPORT_BYTES
    assert "trades" not in example(result)
    assert example(result)["opening_capture_receipt_sha256"] == slot(observe([trade(10_001, 2000)]))["receipts"][1]["chain_sha256"]


def test_later_add_after_opening_capture_cannot_inflate_capture_denominator():
    state = observe([trade(10_001, 2000)], covered=False)
    added = tape.ingest(state, WALLET, [trade(10_002, 40_000, qty="900")],
        receipt(T0 + 20_001, T0 + 50_000, capture=T0 + 60_000), now_ms=T0 + 60_000)
    result = project(added["state"])
    assert example(result)["bought_units_through_capture"] == "100"


def test_later_open_page_does_not_erase_earlier_covered_quantity():
    state = observe([trade(10_001, 2000)])
    row = trade(10_002, 70_000, side="sell")
    added = tape.ingest_page(state, WALLET, [row],
        page_receipt(T0 + 50_001, T0 + 90_000, [row], limit=1, capture=T0 + 95_000), now_ms=T0 + 95_000)
    assert added["status"] == "committed"
    result = project(added["state"])
    assert example(result)["sold_fraction"] == "0"
    assert member(result)["page_window_status"] == "partial_window"


def test_human_token_scale_preserves_fraction_without_claiming_native_cashflow():
    rows = [trade(10_001, 2000, qty="1.5", source="gmgn:smartmoney"),
            trade(10_002, 12_000, side="sell", qty="0.75", source="gmgn:smartmoney")]
    result = project(observe(rows))
    assert example(result)["sold_fraction"] == "0.5"
    assert member(result)["normalization"]["money_axis"] == "usd_micro"
    assert result["profitability_status"] == "UNPROVEN"


def test_more_episodes_than_examples_remain_in_all_counts():
    rows = [trade(10_000 + 2 * i + leg, 2000 + i * 200 + leg * 100,
                  side="buy" if not leg else "sell") for i in range(30) for leg in range(2)]
    result = project(observe(rows))
    selected = member(result)
    assert selected["post_selection_episode_count"] == selected["quantity_evaluated_count"] == selected["fully_sold_count"] == 30
    assert len(selected["examples"]) == 20
    assert selected["examples_omitted"] == 10


@pytest.mark.parametrize("clock", [True, 0, -1, float("nan"), float("inf")])
def test_bad_analysis_clock_never_promotes_flags(clock):
    state = observe([trade(10_001, 2000)])
    assert reach.project(state, now_ms=clock, diagnostics=[])["status"] == "unknown"


def test_unknown_numeric_expansion_is_reported_before_calling_original_normalizer():
    state = observe([trade(10_001, 2000, qty="1e999999")])
    result = project(state)
    assert member(result)["reasons"] == ["numeric_expansion_cap"]
    assert member(result)["retained_rows"] == 3
    assert len(result["wallets"]) == 6


def test_full_six_by_ten_thousand_retained_rows_have_bounded_output():
    _, _, state = setup()
    for index, wallet in enumerate(tape.MEMBERS):
        saved = slot(state, wallet)
        for start in range(0, 9998, 1000):
            rows = [raw((index + 1) * 100_000 + i, T0 + 2000 + i,
                        wallet=wallet, token="FORWARD", qty="100") for i in range(start, min(start + 1000, 9998))]
            end = rows[-1]["ts_ms"]
            beginning = T0 + 1 if start == 0 else T0 + 2000 + start
            assert tape._append(saved, rows, receipt(beginning, end, capture=end + 1), selection=T0)
    state = tape._seal(state)
    result = project(state)
    assert result["status"] == "retrospective_partial_research_evidence"
    assert len(result["wallets"]) == 6
    assert all(w["retained_rows"] == 10_000 and w["post_selection_episode_count"] == 1
               and w["quantity_evaluated_count"] == 1 for w in result["wallets"])
    assert len(json.dumps(result).encode()) < reach.MAX_REPORT_BYTES
