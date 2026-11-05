"""Bounded operating reviews must expose failed learning and uncertain economics."""

from __future__ import annotations

import copy
import json
from decimal import Decimal

import pytest

from kaiba.learning import agent_review as ar

NOW = 1_800_000_000_000


def position(pid, pnl, *, cost=100, chain="sol", mode="live", lane="sm-trenches", closed=NOW - 1000, **extra):
    return {"position_id": pid, "chain": chain, "token": "token-" + pid, "mode": mode, "lane": lane,
            "opened_ms": closed - 10_000, "closed_ms": closed, "cost_native": str(cost),
            "proceeds_native": str(cost + pnl), "realized_native": str(pnl), "qty": "0", **extra}


def book(chain="sol", *, n=20, reason="latency_unreachable", orders=0, closed=0):
    return {"identity": {"spec": {"chain": chain, "lane": "sm-trenches"}},
            "entries": [{"status": "rejected", "reason": reason, "n": n}],
            "order_count": orders, "closed": closed, "latest": [], "positions": []}


def sample(chain="sol", *, usable=False, decimals=None, age=120_000):
    measure = {"value": "1", "basis": "provider_reported", "receipt": {"observed_at_ms": NOW - age}}
    return {"chain": chain, "token_row": [{"decimals": decimals}], "usable_quote": {"priced": True} if usable else None,
            "dossier_fields": {"price_usd": measure, "liquidity_usd": copy.deepcopy(measure)}}


def test_ledger_ev_profit_factor_and_win_rate_stay_separate_by_chain_mode_and_lane():
    packet = {"closed_positions": [position("a", 20), position("b", -10), position("c", 10),
                                    position("bsc", -99, chain="bsc"), position("paper", 500, mode="shadow"),
                                    position("other", -20, lane="trusted-copy")],
              "scope": {"start_ms": NOW - 86_400_000 * ar.sprint.ROLLING_DAYS, "end_ms": NOW,
                        "complete": True, "truncated": False, "total_matching": 6, "row_limit": 20}}
    result = ar.build_review(packet, now_ms=NOW)
    rows = {(r["chain"], r["mode"], r["lane"]): r for r in result["ledger"]["by_chain_mode_lane"]}
    sol = rows[("sol", "live", "sm-trenches")]
    assert sol["position_count"] == sol["return_denominator_count"] == 3
    assert sol["ledger_pnl_native"] == "20" and sol["ledger_profit_factor"] == "3"
    assert sol["ledger_win_rate"] == pytest.approx(2 / 3)
    assert Decimal(sol["mean_ledger_return_pct"]) == pytest.approx(Decimal(20) / 3)
    assert Decimal(sol["mean_ledger_pnl_native"]) == pytest.approx(Decimal(20) / 3)
    assert rows[("bsc", "live", "sm-trenches")]["ledger_pnl_native"] == "-99"
    assert rows[("sol", "shadow", "sm-trenches")]["ledger_pnl_native"] == "500"
    assert result["ledger"]["native_results_combined_across_chains"] is False
    assert "ledger_pnl_native" not in result  # there is no cross-chain total


def test_cap_is_only_sensitivity_and_does_not_rewrite_actual_ledger_or_create_economic_profit():
    result = ar.build_review({"closed_positions": [position("winner", 1000), position("loser", -100)]}, now_ms=NOW)
    row = result["ledger"]["by_chain_mode"][0]
    assert row["ledger_pnl_native"] == "900"
    assert Decimal(row["mean_ledger_return_pct"]) == 450
    assert row["capped_return_sensitivity"]["mean_pct"] == 100
    assert row["capped_return_sensitivity"]["n"] == row["return_denominator_count"] == 2
    assert row["economic_pnl_native"] is None and row["economic_profit_verified"] is False
    assert result["economic_profit_verified"] is False and result["funding_allowed"] is False


def test_period_row_limit_missing_denominators_and_future_rows_are_explicit():
    rows = [position("good", 10), position("no-cost", -50, cost=0),
            position("future", 1000, closed=NOW + 1), position("old", -99, closed=NOW - 200_000)]
    result = ar.build_review({"closed_positions": rows,
                             "scope": {"start_ms": NOW - 100_000, "end_ms": NOW, "row_limit": 4,
                                       "truncated": True, "total_matching": 1000}}, now_ms=NOW)
    scope = result["ledger"]["period"]
    group = result["ledger"]["by_chain_mode"][0]
    assert scope["rows_received"] == 4 and scope["rows_retained"] == 2
    assert scope["complete"] is False and scope["row_limit"] == 4 and scope["total_matching"] == 1000
    assert group["ledger_pnl_count"] == 2 and group["return_denominator_count"] == 1
    assert group["ledger_pnl_native"] == "-40" and Decimal(group["mean_ledger_return_pct"]) == 10
    assert result["ledger"]["excluded_counts"]["outside_requested_period"] == 2


def test_no_denominator_does_not_turn_unknown_into_zero_percent_or_perfect_profit_factor():
    row = position("missing", -5)
    row.pop("cost_native")
    group = ar.build_review({"closed_positions": [row]}, now_ms=NOW)["ledger"]["by_chain_mode"][0]
    assert group["mean_ledger_return_pct"] is None and group["return_denominator_count"] == 0
    assert group["ledger_pnl_native"] == "-5" and group["ledger_profit_factor"] == "0"
    winner = ar.build_review({"closed_positions": [position("up", 1)]}, now_ms=NOW)["ledger"]["by_chain_mode"][0]
    assert winner["ledger_profit_factor"] is None and winner["profit_factor_basis"] == "no_losses_in_subset"


def test_duplicates_and_conflicts_cannot_select_a_favorable_row():
    a, b = position("same", 10), position("conflict", -10)
    altered = {**b, "realized_native": "1000000"}
    result = ar.build_review({"closed_positions": [a, dict(a), b, altered]}, now_ms=NOW)
    assert result["ledger"]["by_chain_mode"][0]["ledger_pnl_native"] == "10"
    assert result["ledger"]["excluded_counts"]["exact_duplicate"] == 1
    assert result["ledger"]["excluded_counts"]["conflicting_position"] == 1


def test_fabricated_shadow_closes_are_counted_as_excluded_and_live_writeoffs_remain():
    data = [position("paper", -100, mode="shadow", exit_reason="abandoned_unpriceable"),
            position("live", -100, exit_reason="abandoned_unpriceable")]
    result = ar.build_review({"closed_positions": data}, now_ms=NOW)
    assert result["ledger"]["excluded_counts"]["fabricated_paper_outcome"] == 1
    assert result["ledger"]["by_chain_mode"][0]["mode"] == "live"
    assert result["ledger"]["by_chain_mode"][0]["ledger_pnl_native"] == "-100"


def test_explicit_closed_live_source_supplies_mode_but_generic_unknown_mode_is_retained():
    row = position("one", -10)
    row.pop("mode")
    live = ar.build_review({"closed_live": [row]}, now_ms=NOW)
    unknown = ar.build_review({"closed_positions": [row]}, now_ms=NOW)
    assert live["ledger"]["by_chain_mode"][0]["mode"] == "live"
    assert unknown["ledger"]["by_chain_mode"][0]["mode"] == "unknown"


def test_fill_cost_coverage_uses_order_denominator_and_never_certifies_economics():
    result = ar.build_review({"closed_live": [position("one", 10)], "cost_coverage": [
        {"chain": "bsc", "mode": "live", "filled_orders": 18, "fill_cost_orders": 0, "reconciled_orders": 0},
        {"chain": "robinhood", "mode": "live", "filled_orders": 92, "fill_cost_orders": 92, "reconciled_orders": 53},
        {"chain": "sol", "mode": "live", "filled_orders": 391, "fill_cost_orders": 346, "reconciled_orders": 292},
    ]}, now_ms=NOW)
    coverage = {r["chain"]: r for r in result["fill_cost_coverage"]}
    assert coverage["bsc"]["status"] == "MISSING" and coverage["bsc"]["fill_cost_fraction"] == 0
    assert coverage["robinhood"]["fill_cost_fraction"] == 1
    assert coverage["robinhood"]["reconciled_fraction"] == pytest.approx(53 / 92)
    assert coverage["sol"]["fill_cost_fraction"] == pytest.approx(346 / 391)
    assert all(r["economic_profit_verified"] is False for r in coverage.values())
    assert result["economic_profit_verified"] is False
    assert "complete_fill_cashflow_evidence" in {r["code"] for r in result["repair_actions"]}


def test_decoder_fees_are_not_subtracted_again_from_ledger():
    p = position("inclusive", 10)
    p["fees_native"] = "90000000"
    result = ar.build_review({"closed_live": [p], "cost_coverage": [
        {"chain": "sol", "mode": "live", "filled_orders": 1, "fill_cost_orders": 1,
         "reconciled_orders": 1, "total_cost_native": "90000000"}]}, now_ms=NOW)
    assert result["ledger"]["by_chain_mode"][0]["ledger_pnl_native"] == "10"


def test_running_services_and_successful_jobs_do_not_hide_broken_paper_quotes():
    packet = {"books": [book("sol", n=55), book("sol", n=55), book("bsc", n=12), book("robinhood", n=33)],
              "quote_diagnostics": [sample(c) for c in ("sol", "bsc", "robinhood")],
              "service_health": {"kaiba-protection.service": {"ActiveState": "active", "Result": "success"},
                                 "kaiba-autolearn.service": {"ActiveState": "inactive", "Result": "success"},
                                 "kaiba-autolearn.timer": {"ActiveState": "active", "Result": "success"}},
              "ops_runs": [{"job": "signal_audit", "status": "ok", "started_ms": NOW - 1000}]}
    result = ar.build_review(packet, now_ms=NOW)
    assert result["operational_health"]["status"] == "OPERATING"
    paper = result["paper_learning"]
    assert paper["status"] == "DATA_FAILURE" and paper["orders"] == 0
    assert paper["opportunity_lower_bound"] == 100 and paper["attempts_across_arms"] == 155
    assert paper["eligible_opportunities_exact"] is None
    assert paper["quote_sample"]["missing_decimals"] == 3
    assert paper["quote_sample_is_entire_historical_population"] is False
    codes = {r["code"] for r in result["repair_actions"]}
    assert {"capture_token_decimals", "capture_fresh_market_evidence", "repair_paper_capture_funnel"} <= codes


def test_actual_runtime_stale_packet_with_cached_precision_does_not_invent_missing_decimals_or_prices():
    now = 1_792_083_449_496
    runtime_packet = {"chain": "sol", "token": "observed-public-token",
        "unavailable_reason": "pool_price_or_depth_missing_or_stale",
        "diagnostics": {"chain": "sol", "token": "observed-public-token", "checked_ms": now,
            "decimals": 6, "dossier_price_ms": 1_792_083_389_449,
            "dossier_depth_ms": 1_792_083_388_586,
            "metadata": {"fetched_ms": now - 1000, "source": "captured_gmgn_info"}}}
    review = ar.build_review({"books": [book()], "quote_diagnostics": [runtime_packet]}, now_ms=now)
    quotes = review["paper_learning"]["quote_sample"]
    assert quotes["missing_decimals"] == quotes["decimals_unknown"] == 0
    assert quotes["price_usd_missing"] == quotes["liquidity_usd_missing"] == 0
    assert quotes["price_usd_unknown"] == quotes["liquidity_usd_unknown"] == 1
    assert quotes["price_usd_older_than_60s"] == quotes["liquidity_usd_older_than_60s"] == 1
    assert quotes["usable"] == 0 and review["paper_learning"]["status"] == "DATA_FAILURE"
    codes = {r["code"] for r in review["repair_actions"]}
    assert "capture_fresh_market_evidence" in codes and "capture_token_decimals" not in codes


def test_actual_usable_pool_and_curve_packets_are_counted_without_inspector_wrappers():
    base = {"chain": "sol", "token": "public-token", "decimals": 6, "quote_ts_ms": NOW - 1000,
            "price_ts_ms": NOW - 2000, "price_usd": "1", "native_usd": "150"}
    pool = {**base, "liquidity_usd": "1000000", "depth_ts_ms": NOW - 1000}
    curve = {**base, "liquidity_usd": None, "curve": {
        "virtual_sol": 31_000_000_000, "virtual_token": 1_000_000_000_000_000,
        "observed_ms": NOW - 2000}}
    refused = {**pool, "unavailable_reason": "native_price_missing_or_stale"}
    stale = {**pool, "price_ts_ms": NOW - 60_001}
    result = ar.build_review({"books": [book()], "quote_diagnostics": [pool, curve, refused, stale]}, now_ms=NOW)
    samples = result["paper_learning"]["quote_sample"]
    assert samples["sampled"] == 4 and samples["usable"] == 2 and samples["usable_unknown"] == 0
    assert samples["missing_decimals"] == samples["decimals_unknown"] == 0
    assert samples["liquidity_usd_not_required_curve"] == 1 and samples["liquidity_usd_missing"] == 0
    assert result["paper_learning"]["status"] == "NO_PAPER_EXECUTION"


def test_unknown_quote_schema_remains_unknown_while_explicit_missing_metadata_is_actionable():
    unknown = ar.build_review({"books": [book()], "quote_diagnostics": [{"chain": "sol", "token": "x"}]}, now_ms=NOW)
    samples = unknown["paper_learning"]["quote_sample"]
    assert samples["missing_decimals"] == samples["price_usd_missing"] == samples["liquidity_usd_missing"] == 0
    assert samples["decimals_unknown"] == samples["usable_unknown"] == 1
    assert unknown["paper_learning"]["status"] == "NO_PAPER_EXECUTION"
    assert "capture_token_decimals" not in {r["code"] for r in unknown["repair_actions"]}
    missing = ar.build_review({"books": [book()], "quote_diagnostics": [
        {"chain": "sol", "token": "x", "unavailable_reason": "token_decimals_missing_or_invalid", "diagnostics": {}}]}, now_ms=NOW)
    assert missing["paper_learning"]["quote_sample"]["missing_decimals"] == 1
    assert missing["paper_learning"]["status"] == "DATA_FAILURE"
    assert "capture_token_decimals" in {r["code"] for r in missing["repair_actions"]}


def test_no_eligible_opportunities_is_distinct_from_a_broken_quote_path():
    empty = ar.build_review({"paper_funnel": {"eligible_opportunities": 0, "orders": 0}}, now_ms=NOW)
    excluded = ar.build_review({"books": [book(reason="filter_not_matched")]}, now_ms=NOW)
    assert empty["paper_learning"]["status"] == excluded["paper_learning"]["status"] == "NO_ELIGIBLE_OPPORTUNITIES"
    assert "repair_paper_capture_funnel" not in {r["code"] for r in empty["repair_actions"]}
    late = ar.build_review({"paper_funnel": {"eligible_opportunities": 10, "orders": 0, "old_arrivals": 10,
                                            "quote_failures": {"arrived_after_entry_deadline": 10}}}, now_ms=NOW)
    assert late["paper_learning"]["status"] == "NO_PAPER_EXECUTION"
    assert late["paper_learning"]["old_arrivals"] == 10
    assert late["paper_learning"]["quote_sample"] == {}


def test_latest_job_results_and_timer_state_drive_operational_health():
    result = ar.build_review({"ops_runs": [
        {"job": "wallet_grade", "started_ms": NOW - 1000, "status": "failed"},
        {"job": "wallet_grade", "started_ms": NOW - 2000, "status": "ok"},
        {"job": "provider_feed", "started_ms": NOW + 1, "status": "ok"}],
        "services": "Id=kaiba-autolearn.service\nActiveState=inactive\nResult=success\n\n"
                    "Id=kaiba-autolearn.timer\nActiveState=inactive\nResult=success\n"}, now_ms=NOW)
    health = result["operational_health"]
    assert health["status"] == "DEGRADED"
    assert health["latest_failed_jobs"] == ["wallet_grade"]
    assert health["failed_services"] == ["kaiba-autolearn.timer"]
    assert result["repair_actions"][0]["priority"] == 0


def test_negative_search_is_retained_and_wallet_nomination_counts_do_not_authorize_copying():
    result = ar.build_review({"discovery_result": {"candidate_manifest": {
        "tried": 840, "holdout_survivors": 0, "observation_count": 544}},
        "wallet_review": {"copy_qualified_count": 0, "coverage": {"unique_chain_addresses": 80},
                          "queue_policy": {"queued": 20}, "candidates": ["unbounded-details-omitted"]}}, now_ms=NOW)
    assert result["discovery"] == {"tried": 840, "survivors": 0, "observations": 544, "funding_allowed": False}
    assert "retain_unsuccessful_strategy_search" in {r["code"] for r in result["repair_actions"]}
    assert "validate_wallet_followers_prospectively" in {r["code"] for r in result["repair_actions"]}
    assert "candidates" not in result["wallet_review"]
    assert result["wallet_review"]["copy_profit_verified"] is False
    assert result["wallet_promotions_authorized"] is False


def test_review_is_pure_deterministic_and_json_finite_with_large_atom_values():
    huge = 10**100
    packet = {"closed_positions": [position("large", -huge // 7, cost=huge)]}
    original = copy.deepcopy(packet)
    result = ar.build_review(packet, now_ms=NOW)
    assert packet == original and result == ar.build_review(packet, now_ms=NOW)
    assert result["ledger"]["by_chain_mode"][0]["ledger_pnl_native"] == str(-huge // 7)
    json.dumps(result, allow_nan=False)


def test_excessive_rows_invalid_periods_and_noninteger_money_do_not_create_misleading_evidence(monkeypatch):
    monkeypatch.setattr(ar, "MAX_POSITION_ROWS", 1)
    with pytest.raises(ValueError, match="row budget"):
        ar.build_review({"closed_positions": [position("a", 1), position("b", 2)]}, now_ms=NOW)
    with pytest.raises(ValueError, match="period"):
        ar.build_review({"scope": {"end_ms": NOW + 1}}, now_ms=NOW)
    with pytest.raises(ValueError, match="frontier"):
        ar.build_review({}, now_ms=True)
    p = position("float", 10)
    p["realized_native"] = 10.5
    group = ar.build_review({"closed_positions": [p]}, now_ms=NOW)["ledger"]["by_chain_mode"][0]
    assert group["ledger_pnl_native"] is None and group["mean_ledger_return_pct"] is None


def test_learning_failure_detection_removed_mutation_is_caught(monkeypatch):
    original = ar._paper

    def hides_failure(*args, **kwargs):
        return {**original(*args, **kwargs), "status": "PROSPECTIVE_EVIDENCE_NEEDS_ASSESSMENT"}

    monkeypatch.setattr(ar, "_paper", hides_failure)
    with pytest.raises(AssertionError):
        test_running_services_and_successful_jobs_do_not_hide_broken_paper_quotes()
