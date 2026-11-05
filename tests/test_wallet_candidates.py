"""Runner nominations cannot become trusted or copy-profitable through labels."""

from __future__ import annotations

import builtins
import copy
import json
import sqlite3
import subprocess

import pytest

from kaiba.intelligence.wallet_candidates import MAX_INPUT_ROWS, VERSION, review_candidates
from kaiba.learning.wallet_behavior_study import analyze_wallet

NOW = 1_800_000_000_000
DAY = 86_400_000
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz123456789"


def sol(index=0):
    return "A" * 31 + ALPHABET[index]


def evm(index=1):
    return f"0x{index:040x}"


def export(chain="sol", addresses=None, *, at=NOW, source="runner_hunt", **kwargs):
    addresses = addresses or [sol() if chain == "sol" else evm()]
    return {"chain": chain, "source": source, "path": f"exports/kaiba-{chain}-snipers-early-runners.txt",
            "sha256": "a" * 64, "observed_ms": at,
            "rows": [{"address": address, "name": "TOP $999k 99%WR", "groups": ["default"]}
                     for address in addresses], **kwargs}


def profile(chain="sol", address=None, *, at=NOW, source="ht_stats", **data):
    address = address or (sol() if chain == "sol" else evm())
    actual = {"_at": at // 1000, "rp": 25_000, "ret": .4, "buy": 50, "tokens": 15,
              "win": .5, "hold": 600, "created": 0, "two_x": 5}
    if source == "p30":
        actual = {"_at": at // 1000, "realized_profit": 25_000, "buy": 50}
    actual.update(data)
    return {"chain": chain, "address": address, "source": source, "data": actual}


def packet():
    return {"exports": [export()], "profiles": [profile()], "history": [], "entities": [],
            "exclusions": [], "behavior_reports": [], "copy_evidence": [], "controls": []}


def review(source=None, **kwargs):
    return review_candidates(source or packet(), now_ms=NOW, **kwargs)


def candidate(result, *, chain="sol", address=None):
    address = address or (sol() if chain == "sol" else evm())
    return next(row for row in result["candidates"] if row["chain"] == chain and row["address"] == address)


def test_actual_export_and_cache_shapes_are_claims_and_only_queue_observations():
    result = review()
    row = candidate(result)
    assert row["roles"] == ["PROVIDER_POSITIVE"]
    assert row["profiles"][0]["profit_usd_claim"] == "25000"
    assert row["profiles"][0]["at_ms"] == NOW
    assert row["copy_status"] == "COPY_UNPROVEN"
    assert result["copy_qualified_count"] == 0 and result["live_copy_approved"] is False
    assert result["funding_allowed"] is False
    assert result["queue_policy"]["unit"] == "proposed_observation_slots"
    assert result["queue_policy"]["provider_budget_increase"] == 0
    assert result["observation_queue"][0]["provider_calls_authorized"] == 0
    assert result["intake_records"] == [{"chain": "sol", "address": sol(), "name": "",
                                          "source": "runner_observation_nomination", "cohort": "research"}]
    json.dumps(result, allow_nan=False)


def test_export_top_grade_and_pnl_name_alone_never_create_quality_evidence():
    source = packet()
    source["profiles"] = []
    result = review(source)
    row = candidate(result)
    assert row["roles"] == [] and row["copy_status"] == "COPY_UNPROVEN"
    assert result["coverage"]["provider_positive_count"] == 0
    assert "refresh_provider_arithmetic" in row["required_evidence"]


def test_export_mtime_does_not_refresh_old_or_missing_provider_timestamp():
    source = packet()
    source["profiles"][0]["data"]["_at"] = (NOW - 3 * DAY) // 1000
    result = review(source)
    assert candidate(result)["roles"] == []
    assert "provider_observation_stale_missing_or_future" in candidate(result)["profiles"][0]["reasons"]
    source["profiles"][0]["data"].pop("_at")
    assert candidate(review(source))["roles"] == []


@pytest.mark.parametrize("changes,reason", [
    ({"_at": NOW // 1000 + 1}, "provider_observation_stale_missing_or_future"),
    ({"_at": "not-a-clock"}, "provider_observation_stale_missing_or_future"),
    ({"missing": True}, "provider_reply_missing"),
    ({"rp": None}, "provider_profit_unknown"),
    ({"rp": "NaN"}, "provider_profit_unknown"),
    ({"buy": None}, "provider_activity_unknown"),
    ({"buy": True}, "provider_activity_unknown"),
    ({"buy": 0}, "provider_no_recent_buys"),
    ({"wallet_address": sol(1)}, "provider_wallet_identity_mismatch"),
    ({"chain": "bsc"}, "provider_chain_identity_mismatch"),
])
def test_unknown_malformed_dormant_and_wrong_identity_provider_rows_are_not_positive(changes, reason):
    source = packet()
    source["profiles"][0]["data"].update(changes)
    row = candidate(review(source))
    assert row["roles"] == []
    assert reason in row["profiles"][0]["reasons"]
    assert row["copy_status"] == "COPY_UNPROVEN"


def test_p30_uses_exact_live_cache_keys_without_a_second_grade_rubric():
    source = packet()
    source["profiles"] = [profile(source="p30", realized_profit="12.5", buy=1)]
    row = candidate(review(source))
    assert row["roles"] == ["PROVIDER_POSITIVE"]
    assert row["profiles"][0]["profit_usd_claim"] == "12.5"
    assert row["profiles"][0]["win_rate_claim"] is None
    assert row["profiles"][0]["return_claim"] is None
    assert "grade" not in row and "score" not in row


def test_latest_observation_supersedes_old_profit_but_does_not_erase_harmful_history():
    source = packet()
    source["profiles"] = [profile(at=NOW - DAY, rp=999_999), profile(rp=-100)]
    source["history"] = [{"chain": "sol", "address": sol(), "tags_json": '["gmgn:scammer"]'}]
    row = candidate(review(source))
    assert row["profiles"][0]["profit_usd_claim"] == "-100"
    assert row["roles"] == []
    assert "existing_hard_quarantine_tag:scammer" in row["exclusions"]
    assert not row["observation_eligible"]


def test_same_clock_conflicting_provider_records_cannot_win_by_input_order():
    source = packet()
    source["profiles"].append(profile(rp=-100))
    first = review(source)
    source["profiles"].reverse()
    second = review(source)
    assert candidate(first)["roles"] == candidate(second)["roles"] == []
    assert "conflicting_provider_profile_same_timestamp" in candidate(first)["warnings"]


def test_fresh_different_provider_windows_keep_disagreement_explicit():
    source = packet()
    source["profiles"].append(profile(source="p30", realized_profit=-100))
    row = candidate(review(source))
    assert row["roles"] == []
    assert "fresh_provider_windows_disagree" in row["warnings"]


def test_chain_dedupe_preserves_solana_case_and_separates_same_evm_address_by_chain():
    address = "0x" + "ab" * 20
    upper = "0x" + "AB" * 20
    source = {"exports": [export(addresses=[sol(), sol().lower()]),
                           export("bsc", [address, upper]), export("robinhood", [address])]}
    result = review(source)
    keys = {(row["chain"], row["address"]) for row in result["candidates"]}
    assert keys == {("sol", sol()), ("sol", sol().lower()), ("bsc", address), ("robinhood", address)}
    assert result["coverage"]["duplicate_nomination_rows"] == 1


@pytest.mark.parametrize("tag", ["gmgn:scammer", "wash_trader", "gmgn:sandwich_bot", "gmgn:bundler", "dev", "creator", "dev_team"])
def test_existing_hard_and_runner_source_exclusions_are_explicit_and_not_silently_removed(tag):
    source = packet()
    source["history"] = [{"chain": "sol", "address": sol(), "tags_json": json.dumps([tag])}]
    result = review(source)
    row = candidate(result)
    assert row["exclusions"] and not row["observation_eligible"]
    assert result["observation_queue"] == [] and result["intake_records"] == []
    assert result["coverage"]["excluded_count"] == 1
    assert row["gates"]["authenticity"] == "EXCLUDED"


@pytest.mark.parametrize("fields,reason", [
    ({"cohort": "blacklist"}, "existing_blacklist_preserved"),
    ({"cohort": "trusted_copy"}, "existing_trusted_copy_preserved"),
    ({"source": "owner-curated"}, "owner_or_agent_wallet"),
    ({"is_owner": True}, "owner_or_agent_wallet"),
    ({"cluster_status": "hub"}, "hub_or_cluster_quarantine"),
    ({"tags_json": "not-json"}, "invalid_historical_risk_tags"),
    ({"is_suspicious": True}, "runner_suspicious_flag"),
    ({"history_transfer_in_amount": "1"}, "runner_transfer_in_flag"),
])
def test_risk_history_is_preserved_and_existing_registry_rows_are_never_intake_updates(fields, reason):
    source = packet()
    source["history"] = [{"chain": "sol", "address": sol(), **fields}]
    result = review(source)
    assert reason in candidate(result)["exclusions"]
    assert result["intake_records"] == [] and result["observation_queue"] == []


def test_clean_existing_registry_nominee_can_be_observed_but_is_not_reimported():
    source = packet()
    source["history"] = [{"chain": "sol", "address": sol(), "cohort": "research", "tags_json": "[]", "grade": "A"}]
    result = review(source)
    assert len(result["observation_queue"]) == 1
    assert result["intake_records"] == []
    assert "stored_grade_is_not_copy_profit_evidence" in candidate(result)["warnings"]


def test_global_owner_exclusion_covers_same_evm_key_on_multiple_chains():
    source = {"exports": [export("bsc"), export("robinhood")],
              "exclusions": [{"address": evm(), "reason": "owned_address"}]}
    result = review(source)
    assert result["observation_queue"] == []
    assert all("supplied_exclusion:owned_address" in row["exclusions"] for row in result["candidates"])


def test_sniper_teacher_is_not_discarded_or_misrepresented_as_a_reachable_copy():
    source = packet()
    source["exports"][0]["rows"][0]["groups"] = ["kaiba-sniper", "kaiba-toptier-TOP"]
    source["history"] = [{"chain": "sol", "address": sol(), "tags_json": '["sniper"]'}]
    row = candidate(review(source))
    assert row["observation_eligible"]
    assert "sniper_copy_reachability_unproven" in row["copy_blockers"]
    assert row["gates"]["reachability"] == "SNIPER_REQUIRES_DELAYED_COPY_TEST"
    assert row["copy_status"] == "COPY_UNPROVEN"


def _behavior_rows(*, old=False, carry=False):
    times = [1000, 2000] if old else [NOW - 2000, NOW - 1000]
    rows = [{"chain": "sol", "wallet": sol(), "token": "token", "ts_ms": times[0], "side": "buy",
             "amount_token": "100", "amount_native": "1000", "usd_value": "100", "tx": "buy", "source": "fixture:atomic"},
            {"chain": "sol", "wallet": sol(), "token": "token", "ts_ms": times[1], "side": "sell",
             "amount_token": "100", "amount_native": "900", "usd_value": "90", "tx": "sell", "source": "fixture:atomic"}]
    return rows[1:] if carry else rows


def test_real_inventory_aware_analyzer_can_supply_a_losing_teacher_without_copy_approval():
    report = analyze_wallet(_behavior_rows(), chain="sol", wallet=sol(), as_of_ms=NOW)
    assert report["gross_money_axis"]["gross_profit"] == "-100"
    source = packet()
    source["behavior_reports"] = [report]
    row = candidate(review(source))
    assert "BEHAVIOR_TEACHER" in row["roles"]
    assert row["copy_status"] == "COPY_UNPROVEN"
    assert row["behavior_evidence"][0]["coverage"]["fees_coverage"] == "unknown"
    assert row["behavior_evidence"][0]["coverage"]["partial_history"] is True


@pytest.mark.parametrize("old,carry", [(True, False), (False, True)])
def test_retimestamped_old_episodes_and_unmatched_carry_sells_are_not_current_teachers(old, carry):
    source = packet()
    source["behavior_reports"] = [analyze_wallet(_behavior_rows(old=old, carry=carry), chain="sol", wallet=sol(), as_of_ms=NOW)]
    row = candidate(review(source))
    assert "BEHAVIOR_TEACHER" not in row["roles"]
    assert row["copy_status"] == "COPY_UNPROVEN"


def test_existing_copy_and_gross_followthrough_labels_never_override_missing_follower_economics():
    source = packet()
    source["copy_evidence"] = [{"chain": "sol", "address": sol(), "source": "proven.py",
                                "status": "PASS", "as_of_ms": NOW, "live_copy_approved": True,
                                "mean_out": .8, "cost_complete": False, "inventory_complete": False}]
    row = candidate(review(source))
    assert row["copy_status"] == "COPY_UNPROVEN"
    assert row["copy_evidence"][0]["status_claim"] == "PASS"
    assert "follower_fees_failed_attempts_inventory_and_reachability_not_proven" in row["copy_blockers"]


def test_fair_queue_respects_budget_entity_caps_and_cross_chain_dependence():
    source = {"exports": [export("sol", [sol(i) for i in range(4)]),
                           export("bsc", [evm(i) for i in range(1, 5)]),
                           export("robinhood", [evm(i) for i in range(1, 5)])],
              "entities": [{"chain": chain, "address": address, "entity_id": "same-operator"}
                           for chain, address in (("sol", sol()), ("bsc", evm()), ("robinhood", evm()))]}
    result = review(source, budget=6, exploration_slots=6)
    queue = result["observation_queue"]
    assert len(queue) == 6
    assert {row["chain"] for row in queue} == {"sol", "bsc", "robinhood"}
    assert sum(row["entity_key"] == "entity:same-operator" for row in queue) <= 1
    assert all(not row["independence_proven"] for row in result["candidates"])


def test_explicit_exploration_slots_preserve_controls_and_remaining_slots_follow_evidence():
    source = packet()
    source["controls"] = [{"chain": "sol", "address": sol(1), "source": "declared-control", "observed_ms": NOW},
                           {"chain": "sol", "address": sol(2), "source": "declared-control", "observed_ms": NOW}]
    result = review(source, budget=2, exploration_slots=1)
    assert [row["selection_arm"] for row in result["observation_queue"]] == ["exploration", "evidence_followup"]
    assert result["queue_policy"]["exploration_queued"] == 1
    assert result["queue_policy"]["matched_losing_controls_verified"] is False


def test_persistent_queue_rotation_does_not_propose_the_same_wallet_forever():
    source = {"exports": [export(addresses=[sol(), sol(1)])]}
    first = review(source, budget=1, exploration_slots=1)
    source["prior_state"] = first["next_state"]
    second = review(source, budget=1, exploration_slots=1)
    assert first["observation_queue"][0]["address"] != second["observation_queue"][0]["address"]
    assert second["next_state"]["version"] == VERSION
    assert sum(second["next_state"]["selection_counts"].values()) == 2


def test_projection_is_pure_deterministic_and_does_not_turn_huge_pnl_into_priority_score():
    source = {"exports": [export(addresses=[sol(), sol(1)])],
              "profiles": [profile(address=sol(), rp=1), profile(address=sol(1), rp=10**50)]}
    original = copy.deepcopy(source)
    first = review(source)
    assert source == original and first == review(source)
    source["profiles"][0]["data"]["rp"], source["profiles"][1]["data"]["rp"] = 10**50, 1
    assert [row["address"] for row in review(source)["observation_queue"]] == [row["address"] for row in first["observation_queue"]]


def test_review_has_no_files_database_or_provider_transport_side_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the wallet adapter may not perform transport or persistence")
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    result = review()
    assert len(result["observation_queue"]) == 1
    assert result["observation_queue"][0]["provider_calls_authorized"] == 0


def test_zero_budget_and_unknown_inputs_produce_no_fake_calls_or_quality():
    result = review(budget=0)
    assert result["observation_queue"] == [] and result["intake_records"] == []
    assert review_candidates({}, now_ms=NOW)["coverage"]["unique_chain_addresses"] == 0


@pytest.mark.parametrize("bad", [-1, True, 1.5, 201])
def test_invalid_observation_budgets_are_rejected(bad):
    with pytest.raises(ValueError, match="budget"):
        review(budget=bad)


@pytest.mark.parametrize("stamp", [True, 0, -1, 1.5, "1800000000000"])
def test_invalid_review_frontier_cannot_relabel_recency(stamp):
    with pytest.raises(ValueError, match="review time"):
        review_candidates(packet(), now_ms=stamp)


def test_oversized_packets_and_malformed_exclusion_containers_cannot_silently_bypass_limits():
    with pytest.raises(ValueError, match="20000"):
        review_candidates({"history": [{"chain": "sol", "address": sol()}] * (MAX_INPUT_ROWS + 1)}, now_ms=NOW)
    with pytest.raises(ValueError, match="exclusions"):
        review({"exports": [export()], "exclusions": {"address": sol(), "reason": "owner"}})


def test_invalid_chain_and_row_chain_conflicts_remain_in_denominators_and_reasons():
    source = {"exports": [export("unsupported", [sol()]), export()]}
    source["exports"][1]["rows"][0]["chain"] = "bsc"
    result = review(source)
    assert result["coverage"]["invalid_identity_rows"] == 1
    assert "export_row_chain_conflict" in candidate(result)["exclusions"]
    assert result["observation_queue"] == []


def test_source_negative_label_and_unknown_tags_are_not_cleared_by_a_clean_later_export():
    source = packet()
    source["exports"][0]["rows"][0]["name"] = "DEV SCAMMER"
    source["exports"].append(export())
    result = review(source)
    assert "negative_operator_label" in candidate(result)["exclusions"]
    assert result["observation_queue"] == []
