"""Actual normalizer/episode tests for the additive fixed-wallet observation tape."""
from __future__ import annotations

import copy
import json

import pytest

from kaiba.learning import fixed_wallet_study as fs
from kaiba.learning import wallet_observation_tape as tape

T0 = 1_792_106_328_738
DAY = 86_400_000
WALLET = tape.MEMBERS[3]  # The actual 833-row comparison wallet.


def raw(rowid, ts, *, wallet=WALLET, token="TOKEN", side="buy", qty="1", money="10",
        usd="10", source="helius"):
    return {"id": rowid, "ts_ms": ts, "token": token, "side": side,
            "amount_token": qty, "amount_native": money, "usd_value": usd,
            "source": source, "tx": f"tx-{rowid}", "oversized_fields": 0,
            "chain": wallet[0], "wallet": wallet[1]}


def receipt(start, end, *, capture=None, horizon=0):
    return {"query_status": "declared_query_complete", "window_start_ms": start,
            "as_of_ms": end, "captured_ms": capture or end, "horizon_ms": horizon}


def page_receipt(start, end, rows, *, after=None, limit=1001, capture=None, horizon=0):
    return {"page_version": tape.PAGE_VERSION, "query_status": "observed_page",
            "window_start_ms": start, "requested_upper_ms": end,
            "captured_ms": capture or end, "horizon_ms": horizon,
            "after": after, "returned_limit": limit, "has_more": len(rows) == limit}


def setup(*, count=2, mixed=False, baseline_offset=-200_000, split_tokens=False):
    histories = {}
    rowid = 1
    for wallet in tape.MEMBERS:
        n = count if wallet == WALLET else 2
        history = []
        for index in range(n):
            history.append(raw(rowid, T0 + baseline_offset + index, wallet=wallet,
                               token="OTHER" if split_tokens and index >= 2 else "TOKEN",
                               side="buy" if index % 2 == 0 else "sell",
                               qty="1.5" if mixed and wallet == WALLET else "1",
                               money="999999" if mixed and wallet == WALLET else "10",
                               usd="10" if index % 2 == 0 else "11",
                               source="gmgn:smartmoney" if mixed and wallet == WALLET else "helius"))
            rowid += 1
        histories[wallet] = history

    class Reader:
        def rows(self, sql, args=()):
            if "FROM kv" in sql:
                return []
            wallet = tuple(args[:2])
            return [{k: v for k, v in row.items() if k not in ("chain", "wallet")}
                    for row in histories[wallet] if args[2] <= row["ts_ms"] <= args[3]][:args[4]]

    fixed_state = fs.collect_fixed_study(Reader(), [], now_ms=T0,
                                        study_state=fs.initial_study_state())["study_next_state"]
    packets = [{"wallet": wallet, "rows": histories[wallet],
                "receipt": receipt(T0 - 7 * DAY, T0, capture=T0 + 1000)} for wallet in tape.MEMBERS]
    result = tape.initialize(fixed_state, packets, now_ms=T0 + 1000)
    assert result["status"] == "initialized", result
    assert set(result["seed_statuses"].values()) == {"verified"}, result
    return fixed_state, histories, result["state"]


def slot(state, wallet=WALLET):
    return state["wallets"][":".join(wallet)]


def test_exact_frozen_baselines_use_actual_later_seed_clock_and_original_units():
    fixed_state, histories, state = setup(mixed=True)
    assert state["fixed_state"] == fixed_state
    assert slot(state)["seed_actual_capture_ms"] == T0 + 1000
    assert fixed_state["selections"][":".join(WALLET)]["baseline"]["captured_ms"] == T0
    assert [e["raw"] for e in slot(state)["rows"]] == histories[WALLET]
    assert slot(state)["rows"][0]["raw"]["amount_token"] == "1.5"
    report = tape.report(state, now_ms=T0 + 1000)
    profile = next(p for p in report["profiles"] if p["wallet"] == WALLET[1])
    assert profile["money_axis"] == "usd_micro"
    assert report["profitability_status"] == report["arrival_evidence"] == "UNPROVEN"
    assert report["copy_qualified"] is report["funding_allowed"] is False
    assert report["provider_calls"] == report["financial_actions"] == 0


def test_833_seed_plus_168_forward_facts_retained_beyond_old_1000_cap():
    _, histories, state = setup(count=833)
    future = [raw(10_000 + i, T0 + 2000 + i) for i in range(168)]
    result = tape.ingest(state, WALLET, future, receipt(T0 + 1, T0 + 5000), now_ms=T0 + 5000)
    assert result["status"] == "committed", result
    retained = slot(result["state"])["rows"]
    assert len(retained) == 1001
    assert [e["raw"] for e in retained[:833]] == histories[WALLET]
    assert all(e["first_captured_ms"] == T0 + 5000 for e in retained[833:])
    assert len(slot(state)["rows"]) == 833  # Input state was never mutated.


def test_full_page_and_short_tail_advance_only_actual_tuple_at_same_timestamp():
    _, _, state = setup(count=833)
    first = [raw(20_000 + i, T0 + 2000) for i in range(1001)]
    query = page_receipt(T0 + 1, T0 + 5000, first)
    result = tape.ingest_page(state, WALLET, first, query, now_ms=T0 + 5000)
    assert result["status"] == "committed", result
    partial = slot(result["state"])
    assert len(partial["rows"]) == 1834
    assert partial["page_cursor"] == [T0 + 2000, 21_000]
    assert partial["completed_through_ms"] == T0
    assert partial["open_page_window"] is not None
    report = tape.report(result["state"], now_ms=T0 + 5000)
    assert report["current_query_coverage"] == "UNPROVEN"
    assert next(c for c in report["coverage"] if c["wallet"] == list(WALLET))["page_window_status"] == "partial_window"
    profile = next(p for p in report["profiles"] if p["wallet"] == WALLET[1])
    assert "query_observations_missing" in profile["evidence_flags"]
    tail = [raw(21_001, T0 + 2000, side="sell")]
    completed = tape.ingest_page(result["state"], WALLET, tail,
        page_receipt(T0 + 1, T0 + 5000, tail, after=partial["page_cursor"], capture=T0 + 6000),
        now_ms=T0 + 6000)
    assert completed["status"] == "committed", completed
    final = slot(completed["state"])
    assert len(final["rows"]) == 1835
    assert final["page_cursor"] == [T0 + 2000, 21_001]
    assert final["completed_through_ms"] == T0 + 5000
    assert final["open_page_window"] is None


def test_full_page_requires_empty_final_query_and_duplicate_round_is_idempotent():
    _, _, state = setup()
    rows = [raw(50_000 + i, T0 + 2000) for i in range(3)]
    query = page_receipt(T0 + 1, T0 + 5000, rows, limit=3)
    first = tape.ingest_page(state, WALLET, rows, query, now_ms=T0 + 5000)
    repeated = tape.ingest_page(first["state"], WALLET, rows, query, now_ms=T0 + 5000)
    assert repeated["status"] == "unchanged"
    assert repeated["state"] == first["state"]
    empty = tape.ingest_page(first["state"], WALLET, [],
        page_receipt(T0 + 1, T0 + 5000, [], limit=3, after=[T0 + 2000, 50_002], capture=T0 + 6000),
        now_ms=T0 + 6000)
    assert empty["status"] == "committed", empty
    assert slot(empty["state"])["page_cursor"] == [T0 + 2000, 50_002]
    assert slot(empty["state"])["completed_through_ms"] == T0 + 5000


def test_horizon_jump_past_unfinished_timestamp_records_gap_but_adjacent_complete_does_not():
    _, _, state = setup()
    row = raw(60_000, T0 + 2000)
    partial = tape.ingest_page(state, WALLET, [row],
        page_receipt(T0 + 1, T0 + 5000, [row], limit=1), now_ms=T0 + 5000)["state"]
    result = tape.ingest_page(partial, WALLET, [],
        page_receipt(T0 + 2001, T0 + 5000, [], after=[T0 + 2000, 60_000],
                     capture=T0 + 6000, horizon=T0 + 2001), now_ms=T0 + 6000)
    assert result["status"] == "committed", result
    assert slot(result["state"])["unobserved_intervals"] == [
        {"start_ms": T0 + 2000, "end_ms": T0 + 2000, "token": None}]
    # Keeping the timestamp bucket and continuing by rowid does not declare a gap.
    continued = tape.ingest_page(partial, WALLET, [],
        page_receipt(T0 + 2000, T0 + 5000, [], after=[T0 + 2000, 60_000],
                     capture=T0 + 6000), now_ms=T0 + 6000)
    assert continued["status"] == "committed" and slot(continued["state"])["unobserved_intervals"] == []
    adjacent = tape.ingest_page(continued["state"], WALLET, [],
        page_receipt(T0 + 5001, T0 + 7000, [], after=[T0 + 2000, 60_000]), now_ms=T0 + 7000)
    assert adjacent["status"] == "committed" and slot(adjacent["state"])["unobserved_intervals"] == []


def test_pending_range_upper_cannot_grow_without_explicit_abandonment():
    _, _, state = setup()
    rows = [raw(65_000, T0 + 2000)]
    partial = tape.ingest_page(state, WALLET, rows,
        page_receipt(T0 + 1, T0 + 5000, rows, limit=1), now_ms=T0 + 5000)["state"]
    rejected = tape.ingest_page(partial, WALLET, [],
        page_receipt(T0 + 1, T0 + 7000, [], after=[T0 + 2000, 65_000]), now_ms=T0 + 7000)
    assert rejected["reason"] == "unfinished_page_upper_changed" and rejected["state"] == partial


@pytest.mark.parametrize("change,reason", [
    ("false_complete", "page_completion_claim_invalid"),
    ("timestamp_plus1", "page_cursor_did_not_continue"),
    ("rowid_skip", "page_cursor_did_not_continue"),
    ("upper_regressed", "observation_clock_regressed"),
])
def test_page_metadata_cannot_skip_cursor_or_invent_completion(change, reason):
    _, _, state = setup()
    rows = [raw(70_000, T0 + 2000)]
    first = tape.ingest_page(state, WALLET, rows,
        page_receipt(T0 + 1, T0 + 5000, rows, limit=1), now_ms=T0 + 5000)["state"]
    tail = [raw(70_001, T0 + 2000)]
    query = page_receipt(T0 + 1, T0 + 5000, tail, after=[T0 + 2000, 70_000], limit=1, capture=T0 + 6000)
    if change == "false_complete":
        query["has_more"] = False
    elif change == "timestamp_plus1":
        query["after"] = [T0 + 2001, 70_000]
        tail[0]["ts_ms"] = T0 + 2002
    elif change == "rowid_skip":
        query["after"] = [T0 + 2000, 70_001]
        tail[0]["id"] = 70_002
    else:
        query["requested_upper_ms"] -= 1
    rejected = tape.ingest_page(first, WALLET, tail, query, now_ms=T0 + 6000)
    assert rejected["status"] == "unknown" and reason in rejected["reason"], rejected
    assert rejected["state"] == first


def test_retained_open_inventory_survives_13_day_source_window_and_horizon_gap():
    _, histories, state = setup(count=1, baseline_offset=-7 * DAY + 1000)
    # The original 7d baseline prefix is retained even after the 13d source bound advances.
    end = T0 + 7 * DAY
    start = end - 13 * DAY
    sale = raw(90_000, end - 1000, side="sell", money="15", usd="15")
    result = tape.ingest_page(state, WALLET, [sale],
        page_receipt(start, end, [sale], horizon=start, capture=end), now_ms=end)
    assert result["status"] == "committed", result
    assert [e["raw"] for e in slot(result["state"])["rows"]][:1] == histories[WALLET]
    report = tape.report(result["state"], now_ms=end)
    profile = next(p for p in report["profiles"] if p["wallet"] == WALLET[1])
    assert profile["preexisting_inventory_followthrough"]["counts"]["closed"] == 1
    assert profile["post_selection_new_entries"]["counts"]["closed"] == 0
    assert result["state"]["fixed_state"] == state["fixed_state"]
    # Skipping an unavailable post-T0 interval is a permanent known gap.
    skipped = tape.ingest_page(state, WALLET, [sale],
        page_receipt(T0 + DAY, end, [sale], horizon=T0 + DAY, capture=end), now_ms=end)
    assert skipped["status"] == "committed", skipped
    assert slot(skipped["state"])["unobserved_intervals"] == [{"start_ms": T0 + 1, "end_ms": T0 + DAY - 1, "token": None}]
    contaminated = tape.report(skipped["state"], now_ms=end)
    profile = next(p for p in contaminated["profiles"] if p["wallet"] == WALLET[1])
    assert profile["preexisting_inventory_followthrough"]["counts"]["intersects_unobserved_interval"] == 1
    assert profile["preexisting_inventory_followthrough"]["gross_money_axis"]["gross_profit"] is None


@pytest.mark.parametrize("change,reason", [
    ("late_baseline", "observed_late_baseline_backfill"),
    ("mutation", "observed_rowid_mutation"),
    ("missing", "previously_observed_row_missing"),
    ("future_capture", "receipt_clock_order_invalid"),
    ("truncated", "query_not_complete"),
    ("oversize", "oversized_source_field"),
])
def test_bad_observations_are_unknown_atomic_and_rejection_marks_report(change, reason):
    _, histories, state = setup()
    rows = copy.deepcopy(histories[WALLET])
    query = receipt(T0 - 7 * DAY, T0 + 5000)
    if change == "late_baseline":
        rows.append(raw(100_000, T0 - 1))
    elif change == "mutation":
        rows[0]["amount_native"] = "999"
    elif change == "missing":
        rows.pop()
    elif change == "future_capture":
        query["captured_ms"] += 1
    elif change == "truncated":
        query["query_status"] = "row_cap_truncated"
    else:
        rows[0]["amount_native"] = "9" * 513
    rejected = tape.ingest(state, WALLET, rows, query, now_ms=T0 + 5000)
    assert rejected["status"] == "unknown" and reason in rejected["reason"], rejected
    assert rejected["state"] == state
    report = tape.report(state, now_ms=T0 + 5000, diagnostics=[rejected["diagnostic"]])
    assert report["current_query_coverage"] == "UNPROVEN"
    profile = next(p for p in report["profiles"] if p["wallet"] == WALLET[1])
    assert "query_observations_missing" in profile["evidence_flags"]


def test_seed_failures_are_per_wallet_and_never_create_another_selection():
    fixed_state, histories, _ = setup()
    result = tape.initialize(fixed_state, now_ms=T0 + 1000)
    assert result["status"] == "initialized"
    assert len(result["seed_statuses"]) == 6 and set(result["seed_statuses"].values()) == {"unseeded"}
    state = result["state"]
    unknown = tape.ingest_page(state, WALLET, [], page_receipt(T0 + 1, T0 + 5000, []), now_ms=T0 + 5000)
    assert unknown["reason"] == "wallet_seed_unverified" and unknown["state"] == state
    broken = copy.deepcopy(histories[WALLET])
    broken[0]["amount_native"] = "20"
    failed = tape.seed(state, WALLET, broken, receipt(T0 - 7 * DAY, T0, capture=T0 + 1000), now_ms=T0 + 1000)
    assert failed["status"] == "unknown" and failed["state"] == state
    wallet2 = tape.MEMBERS[0]
    good = tape.seed(state, wallet2, histories[wallet2], receipt(T0 - 7 * DAY, T0, capture=T0 + 1000), now_ms=T0 + 1000)
    assert good["status"] == "seeded", good
    report = tape.report(good["state"], now_ms=T0 + 1000)
    assert report["verified_seed_wallets"] == 1 and report["fixed_wallet_denominator"] == len(report["profiles"]) == 6
    assert good["state"]["fixed_state"] == fixed_state
    repeated = tape.seed(good["state"], wallet2, histories[wallet2], receipt(T0 - 7 * DAY, T0, capture=T0 + 1000), now_ms=T0 + 1000)
    assert repeated["reason"] == "wallet_seed_already_verified"
    assert repeated["state"] == good["state"]


def test_five_valid_seed_packets_survive_one_changed_baseline():
    fixed_state, histories, _ = setup()
    packets = [{"wallet": wallet, "rows": copy.deepcopy(histories[wallet]),
                "receipt": receipt(T0 - 7 * DAY, T0, capture=T0 + 1000)} for wallet in tape.MEMBERS]
    packets[3]["rows"][0]["amount_native"] = "200"
    result = tape.initialize(fixed_state, packets, now_ms=T0 + 1000)
    assert result["status"] == "initialized", result
    assert list(result["seed_statuses"].values()).count("verified") == 5
    assert slot(result["state"])["seed_status"] == "unseeded"
    assert result["state"]["fixed_state"] == fixed_state
    assert result["seed_diagnostics"][0]["reason"] == "seed_baseline_raw_hash_mismatch"


@pytest.mark.parametrize("mutation", ["missing", "state_hash", "source", "fixed_source", "crash_round"])
def test_missing_corrupt_old_source_or_half_committed_state_never_reinitializes(mutation):
    _, _, original = setup()
    state = copy.deepcopy(original)
    if mutation == "missing":
        state = None
    elif mutation == "state_hash":
        state["state_sha256"] = "0" * 64
    elif mutation == "source":
        state["source_versions"]["intelligence/grade.py"] = "0" * 64
        state = tape._seal(state)
    elif mutation == "fixed_source":
        state["fixed_state"]["manifest"]["source_versions"]["intelligence/grade.py"] = "0" * 64
        state = tape._seal(state)
    else:
        slot(state)["rows"].append({"raw": raw(99_999, T0 + 1000), "first_captured_ms": T0 + 1000})
    result = tape.ingest(state, WALLET, [], receipt(T0 + 1, T0 + 5000), now_ms=T0 + 5000)
    assert result["status"] == "unknown" and result["state"] == state
    assert tape.report(state, now_ms=T0 + 5000)["status"] == "unknown"


def test_storage_caps_reject_entire_page_without_dropping_prefix(monkeypatch):
    _, _, state = setup()
    rows = [raw(100_000 + i, T0 + 2000 + i) for i in range(3)]
    monkeypatch.setattr(tape, "MAX_WALLET_ROWS", 4)
    result = tape.ingest_page(state, WALLET, rows, page_receipt(T0 + 1, T0 + 5000, rows), now_ms=T0 + 5000)
    assert result["reason"] == "retained_wallet_row_cap" and result["state"] == state
    monkeypatch.setattr(tape, "MAX_WALLET_ROWS", 10_000)
    monkeypatch.setattr(tape, "MAX_STATE_BYTES", len(tape._encoded(state)) + 10)
    result = tape.ingest_page(state, WALLET, rows, page_receipt(T0 + 1, T0 + 5000, rows), now_ms=T0 + 5000)
    assert result["reason"] == "state_byte_cap" and result["state"] == state


def test_input_row_and_byte_caps_are_distinct_from_partial_page_cap(monkeypatch):
    _, _, state = setup()
    rows = [raw(100_000 + i, T0 + 2000 + i) for i in range(1002)]
    result = tape.ingest_page(state, WALLET, rows,
        page_receipt(T0 + 1, T0 + 5000, rows, limit=1001), now_ms=T0 + 5000)
    assert result["status"] == "unknown" and result["state"] == state
    monkeypatch.setattr(tape, "MAX_INPUT_BYTES", 100)
    rows = rows[:1]
    result = tape.ingest_page(state, WALLET, rows,
        page_receipt(T0 + 1, T0 + 5000, rows), now_ms=T0 + 5000)
    assert result["reason"] == "input_byte_cap" and result["state"] == state


def test_declared_complete_1001_sentinel_is_unknown_but_same_facts_page_are_retained():
    _, _, state = setup()
    rows = [raw(110_000 + i, T0 + 2000 + i) for i in range(1001)]
    rejected = tape.ingest(state, WALLET, rows, receipt(T0 + 1, T0 + 5000), now_ms=T0 + 5000)
    assert rejected["reason"] == "complete_window_contains_limit_sentinel" and rejected["state"] == state
    accepted = tape.ingest_page(state, WALLET, rows,
        page_receipt(T0 + 1, T0 + 5000, rows), now_ms=T0 + 5000)
    assert accepted["status"] == "committed" and len(slot(accepted["state"])["rows"]) == 1003
    assert slot(accepted["state"])["completed_through_ms"] == T0


def test_empty_complete_packet_records_coverage_not_zero_profit_and_hash_chain_detects_tamper():
    _, _, state = setup(count=1)
    result = tape.ingest(state, WALLET, [], receipt(T0 + 1, T0 + 5000), now_ms=T0 + 5000)
    assert result["status"] == "committed", result
    saved = slot(result["state"])
    assert saved["receipts"][-1]["packet_rows"] == saved["receipts"][-1]["added_rows"] == 0
    assert saved["receipts"][-1]["previous_sha256"] == saved["receipts"][0]["chain_sha256"]
    profile = next(p for p in tape.report(result["state"], now_ms=T0 + 5000)["profiles"] if p["wallet"] == WALLET[1])
    assert profile["post_selection_new_entries"]["gross_money_axis"]["gross_profit"] is None
    changed = copy.deepcopy(result["state"])
    slot(changed)["receipts"][-1]["packet_rows"] = 1
    changed = tape._seal(changed)
    assert tape.report(changed, now_ms=T0 + 5000)["reason"] == "receipt_chain_hash_mismatch"


def test_packet_rowids_reproduce_raw_hash_and_preserve_first_actual_capture():
    _, _, state = setup()
    rows = [raw(130_000, T0 + 2000)]
    result = tape.ingest_page(state, WALLET, rows,
        page_receipt(T0 + 1, T0 + 5000, rows), now_ms=T0 + 5000)["state"]
    old = slot(result)["rows"][-1]
    assert old["first_captured_ms"] == T0 + 5000
    repeated_later = tape.ingest_page(result, WALLET, rows,
        page_receipt(T0 + 1, T0 + 6000, rows), now_ms=T0 + 6000)
    assert repeated_later["status"] == "committed"
    assert slot(repeated_later["state"])["rows"][-1] == old
    saved = slot(repeated_later["state"])
    indexed = {e["raw"]["id"]: e["raw"] for e in saved["rows"]}
    assert all(tape._sha([indexed[i] for i in r["packet_rowids"]]) == r["packet_raw_sha256"] for r in saved["receipts"])
    changed = copy.deepcopy(repeated_later["state"])
    slot(changed)["rows"][-1]["first_captured_ms"] = T0 + 6000
    changed = tape._seal(changed)
    assert tape.report(changed, now_ms=T0 + 6000)["reason"] == "first_capture_receipt_conflict"


def test_global_source_rowid_cannot_be_reassigned_to_another_wallet():
    _, histories, state = setup()
    other_id = histories[tape.MEMBERS[0]][0]["id"]
    rows = [raw(other_id, T0 + 2000)]
    result = tape.ingest_page(state, WALLET, rows,
        page_receipt(T0 + 1, T0 + 5000, rows), now_ms=T0 + 5000)
    assert result["reason"] == "rowid_already_owned_by_other_wallet" and result["state"] == state


def test_report_compacts_examples_and_explicitly_bounds_public_output():
    _, _, state = setup()
    rows = [raw(100_000 + i, T0 + 2000 + i, side="buy" if i % 2 == 0 else "sell") for i in range(1000)]
    result = tape.ingest_page(state, WALLET, rows, page_receipt(T0 + 1, T0 + 5000, rows), now_ms=T0 + 5000)
    report = tape.report(result["state"], now_ms=T0 + 5000)
    assert report["status"] == "observed_partial_leader_tape", report
    assert len(json.dumps(report).encode()) < tape.MAX_REPORT_BYTES
    profile = next(p for p in report["profiles"] if p["wallet"] == WALLET[1])
    assert profile["post_selection_new_entries"]["counts"]["closed"] == 500
    assert profile["episode_examples"]["included"] == 20
    assert profile["episode_examples"]["omitted"] > 0
    assert all("trades" not in e for e in profile["episodes"])


def test_new_base_unit_token_cannot_silently_change_same_axis_frozen_baseline():
    _, _, state = setup(count=4, mixed=True, split_tokens=True)
    rows = [raw(120_000, T0 + 2000, token="TOKEN", source="helius"),
            raw(120_001, T0 + 2001, token="NEW", qty="1.5", source="gmgn:smartmoney"),
            raw(120_002, T0 + 2002, token="NEW", side="sell", qty="1.5", usd="20", source="gmgn:smartmoney")]
    result = tape.ingest_page(state, WALLET, rows,
        page_receipt(T0 + 1, T0 + 5000, rows), now_ms=T0 + 5000)
    assert result["status"] == "committed", result
    report = tape.report(result["state"], now_ms=T0 + 5000)
    profile = next(p for p in report["profiles"] if p["wallet"] == WALLET[1])
    assert profile["money_axis"] == "usd_micro"
    assert profile["baseline_normalization_axis_changed"] is False
    assert profile["baseline_frozen_normalization_status"] == "CHANGED"
    assert profile["metadata"]["baseline_normalization_matches_frozen"] is False
    assert profile["comparison"]["new_minus_baseline_size_weighted_gross_roi"] is None
    assert "query_observations_missing" in profile["evidence_flags"]
    assert result["state"]["fixed_state"] == state["fixed_state"]
