"""Real tape normalization, immutable selection and bounded optional evidence."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from kaiba.learning import autolearn_behavior as ab
from kaiba.learning.autolearn_io import GuardedReader, SourceDeferred

NOW = 1_800_000_000_000
DAY = 86_400_000


def wallet(index=1, chain="sol"):
    return "A" * 31 + "BCDEFGHJKLMNPQRSTUVWXYZ"[index % 22] if chain == "sol" else f"0x{index:040x}"


def queue(n=1, chain="sol"):
    return [{"chain": chain, "address": wallet(i, chain)} for i in range(n)]


def reader(conn):
    return GuardedReader(Path(conn.execute("PRAGMA database_list").fetchone()[2]))


def swap(conn, *, owner=None, chain="sol", offset=-1000, side="buy", qty="100", money="100",
         usd="100", token="TOKEN", source="helius", tx=None):
    owner = owner or wallet(0, chain)
    tx = tx or f"{owner}:{token}:{offset}:{side}"
    conn.execute("INSERT INTO swaps(chain,tx,ts_ms,wallet,token,side,amount_token,amount_native,usd_value,source) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (chain, tx, NOW + offset, owner, token, side, qty, money, usd, source))


def test_actual_normalizer_preserves_gmgn_quote_axis_and_gross_only(tmp_db):
    swap(tmp_db, chain="bsc", token="0x" + "f" * 40, offset=-2000,
         qty="1.5", money="888", usd="10", source="gmgn:smartmoney")
    swap(tmp_db, chain="bsc", token="0x" + "f" * 40, offset=-1000, side="sell",
         qty="1.5", money="999", usd="12", source="gmgn:smartmoney")
    result = ab.collect_behavior(reader(tmp_db), queue(chain="bsc"), now_ms=NOW)
    report = result["behavior_reports"][0]
    assert report["money_axis"] == "usd_micro"
    assert report["gross_money_axis"]["gross_profit"] == "2000000"
    assert report["gross_native_atomic"] is None
    assert report["coverage"]["partial_history"] is True
    assert report["metadata"]["left_censored"] is True
    assert report["profitability_status"] == "UNPROVEN" and not report["live_copy_approved"]
    assert result["acquisition"][0]["scheduled_history_eligibility"] == "unavailable"
    assert result["provider_calls"] == result["roster_changes"] == 0
    assert "trades" not in report["episodes"][0]


def test_fixed_selection_keeps_old_bags_separate_from_new_entries(tmp_db):
    swap(tmp_db, token="OLD", offset=-2000)
    first = ab.collect_behavior(reader(tmp_db), queue(), now_ms=NOW)
    frozen = deepcopy(first["next_state"]["selections"])
    swap(tmp_db, token="OLD", side="sell", offset=1000, money="50", usd="50")
    swap(tmp_db, token="NEW", offset=2000)
    swap(tmp_db, token="NEW", side="sell", offset=3000, money="150", usd="150")
    second = ab.collect_behavior(reader(tmp_db), queue(), now_ms=NOW + 5000, prior_state=first["next_state"])
    report = second["followthrough_reports"][0]
    assert second["next_state"]["selections"] == frozen
    assert report["selection_ms"] == NOW
    assert report["post_selection_new_entries"]["gross_money_axis"]["gross_profit"] == "50"
    assert report["preexisting_inventory_followthrough"]["gross_money_axis"]["gross_profit"] == "-50"
    assert report["baseline_comparison_status"] == "OBSERVED_GROSS_PARTIAL_ONLY"
    assert report["profitability_status"] == "UNPROVEN"


def test_rotates_only_selected_ten_with_index_and_both_time_bounds(tmp_db):
    class RecordingReader(GuardedReader):
        statements = []

        def rows(self, sql, args=()):
            self.statements.append((sql, args))
            return super().rows(sql, args)

    for row in queue(20):
        swap(tmp_db, owner=row["address"])
        swap(tmp_db, owner=row["address"], offset=1, side="sell")
    r = RecordingReader(reader(tmp_db).db_path)
    first = ab.collect_behavior(r, queue(20), now_ms=NOW)
    second = ab.collect_behavior(r, queue(20), now_ms=NOW, prior_state=first["next_state"])
    tape = [(sql, args) for sql, args in r.statements if "FROM swaps" in sql]
    assert len(tape) == 20 and len({args[1] for _, args in tape}) == 20
    assert all(args[2] >= NOW - 13 * DAY and args[3] == NOW and args[4] <= 1001 for _, args in tape)
    for sql, args in tape:
        plan = " ".join(str(tuple(row)) for row in tmp_db.execute("EXPLAIN QUERY PLAN " + sql, args))
        assert "SEARCH swaps USING INDEX idx_swaps_wallet" in plan
        assert "TEMP B-TREE" not in plan
    assert all(r["episode_counts"]["closed"] == 0 for r in second["behavior_reports"])
    assert first["coverage"]["attempted"] == second["coverage"]["attempted"] == 10


def test_sentinel_and_total_cap_cannot_be_reported_as_complete(tmp_db, monkeypatch):
    monkeypatch.setattr(ab, "MAX_WALLET_ROWS", 2)
    monkeypatch.setattr(ab, "MAX_TOTAL_ROWS", 4)
    for owner in (wallet(0), wallet(1), wallet(2)):
        for i in range(4):
            swap(tmp_db, owner=owner, offset=-100 - i)
    result = ab.collect_behavior(reader(tmp_db), queue(3), now_ms=NOW)
    assert result["coverage"]["rows_read"] <= 4
    assert all(r["coverage"]["query_status"] != "declared_query_complete" for r in result["behavior_reports"])
    assert all(not r["episodes"] for r in result["behavior_reports"])
    # Positive control: a declared two-row window finishes with the third-row sentinel absent.
    clean = wallet(4)
    swap(tmp_db, owner=clean, offset=-2000)
    swap(tmp_db, owner=clean, offset=-1000, side="sell")
    result = ab.collect_behavior(reader(tmp_db), [{"chain": "sol", "address": clean}], now_ms=NOW)
    assert result["receipts"][0]["query_status"] == "declared_query_complete"
    assert result["behavior_reports"][0]["episode_counts"]["closed"] == 1


def test_oversized_fields_are_suppressed_in_sql_not_normalized_as_missing_inventory(tmp_db):
    swap(tmp_db, token="T" * 100_000)
    result = ab.collect_behavior(reader(tmp_db), queue(), now_ms=NOW)
    assert result["receipts"][0]["query_status"] == "oversized_source_field"
    assert result["coverage"]["source_bytes"] < 1000
    assert not result["behavior_reports"][0]["episodes"]


def test_retention_and_13_day_ceiling_invalidate_original_baseline(tmp_db):
    swap(tmp_db, offset=-6 * DAY)
    first = ab.collect_behavior(reader(tmp_db), queue(), now_ms=NOW)
    baseline = deepcopy(next(iter(first["next_state"]["selections"].values()))["baseline"])
    tmp_db.execute("INSERT INTO kv(key,value,updated_ms) VALUES (?,?,?)",
                   ("retention:swaps:horizon_ms", str(NOW - 5 * DAY), NOW))
    second = ab.collect_behavior(reader(tmp_db), queue(), now_ms=NOW + 8 * DAY, prior_state=first["next_state"])
    assert second["receipts"][0]["window_start_ms"] == NOW - 5 * DAY
    assert next(iter(second["next_state"]["selections"].values()))["baseline"] == baseline
    assert second["followthrough_reports"][0]["baseline_comparison_status"].startswith("UNPROVEN")
    assert second["followthrough_reports"][0]["comparison"]["new_minus_baseline_size_weighted_gross_roi"] is None


def test_deferred_or_malformed_horizon_performs_no_tape_read_and_keeps_selection(tmp_db):
    class Deferred(GuardedReader):
        def rows(self, sql, args=()):
            assert "FROM swaps" not in sql
            raise SourceDeferred("wal_over_limit:2000000000")

    first = ab.collect_behavior(Deferred(reader(tmp_db).db_path), queue(), now_ms=NOW)
    frozen = deepcopy(first["next_state"]["selections"])
    assert first["receipts"][0]["query_status"].startswith("wal_over_limit")
    assert first["behavior_reports"][0]["episodes"] == []
    tmp_db.execute("INSERT INTO kv(key,value,updated_ms) VALUES (?,?,?)",
                   ("retention:swaps:horizon_ms", '"unknown"', NOW))
    again = ab.collect_behavior(reader(tmp_db), queue(), now_ms=NOW + 1000, prior_state=first["next_state"])
    assert again["next_state"]["selections"] == frozen
    assert again["receipts"][0]["query_status"] == "retention_watermark_unreadable"


def test_late_baseline_or_changed_source_never_becomes_comparable(tmp_db, monkeypatch):
    first = ab.collect_behavior(reader(tmp_db), queue(), now_ms=NOW)
    swap(tmp_db, offset=-1000)  # A historical row arriving after selection changes its evidence.
    later = ab.collect_behavior(reader(tmp_db), queue(), now_ms=NOW + 1000, prior_state=first["next_state"])
    assert later["followthrough_reports"][0]["baseline_comparison_status"].startswith("UNPROVEN")
    monkeypatch.setattr(ab, "source_versions", lambda: {"normalizer": "different-code"})
    changed = ab.collect_behavior(reader(tmp_db), queue(), now_ms=NOW + 2000, prior_state=later["next_state"])
    assert changed["next_state"]["selections"] == first["next_state"]["selections"]
    assert changed["followthrough_reports"][0]["baseline_comparison_status"].startswith("UNPROVEN")


def test_summary_examples_are_bounded_with_full_aggregate_and_no_database_writes(tmp_db):
    for i in range(30):
        swap(tmp_db, token=f"TOKEN{i}", offset=-2000 - i * 2)
        swap(tmp_db, token=f"TOKEN{i}", side="sell", offset=-1999 - i * 2, money="90", usd="90")
    changes = tmp_db.total_changes
    result = ab.collect_behavior(reader(tmp_db), queue(), now_ms=NOW)
    report = result["behavior_reports"][0]
    assert report["episode_counts"]["closed"] == 30
    assert len(report["episodes"]) == 20
    assert report["episode_examples"]["omitted"] == 10
    assert len(report["episode_examples"]["full_analysis_sha256"]) == 64
    assert result["coverage"]["summary_bytes"] <= ab.MAX_SUMMARY_BYTES
    assert len(json.dumps(result).encode()) < 1_000_000
    assert tmp_db.total_changes == changes


def test_new_quote_axis_cannot_silently_reprice_frozen_native_baseline(tmp_db):
    swap(tmp_db, offset=-2000)
    swap(tmp_db, offset=-1000, side="sell", money="120", usd="120")
    first = ab.collect_behavior(reader(tmp_db), queue(), now_ms=NOW)
    swap(tmp_db, offset=1000, token="NEW", qty="1.5", money="999", usd="10", source="gmgn:smartmoney")
    second = ab.collect_behavior(reader(tmp_db), queue(), now_ms=NOW + 2000, prior_state=first["next_state"])
    follow = second["followthrough_reports"][0]
    assert follow["money_axis"] == "usd_micro"
    assert follow["baseline_normalization_axis_changed"] is True
    assert follow["baseline_comparison_status"].startswith("UNPROVEN")
    assert follow["comparison"]["new_minus_baseline_size_weighted_gross_roi"] is None
    assert second["next_state"]["selections"] == first["next_state"]["selections"]


def test_selection_byte_capacity_preserves_old_freeze_and_marks_new_identity_unknown(tmp_db, monkeypatch):
    first = ab.collect_behavior(reader(tmp_db), queue(), now_ms=NOW)
    frozen = deepcopy(first["next_state"]["selections"])
    monkeypatch.setattr(ab, "MAX_SELECTION_BYTES", ab._selection_bytes(frozen) + 10)
    second = ab.collect_behavior(reader(tmp_db), queue(2), now_ms=NOW + 1000, prior_state=first["next_state"])
    assert second["next_state"]["selections"] == frozen
    unknown = next(r for r in second["behavior_reports"] if r["wallet"] == wallet(1))
    assert unknown["coverage"]["query_status"] == "immutable_selection_capacity"


def test_exhausted_byte_budget_does_not_issue_unbounded_tape_query(tmp_db, monkeypatch):
    monkeypatch.setattr(ab, "MAX_TOTAL_BYTES", ab.MAX_ROW_PAYLOAD_BYTES - 1)
    r = reader(tmp_db)
    result = ab.collect_behavior(r, queue(), now_ms=NOW)
    assert r.queries == 1  # Retention only; no tape query fits the proven maximum row payload.
    assert result["coverage"]["source_bytes"] == 0
    assert result["receipts"][0]["query_status"] == "round_source_budget_exhausted"
