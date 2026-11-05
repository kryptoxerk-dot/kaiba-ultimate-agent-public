"""Actual bounded observer controls; no providers or financial state."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest

from kaiba.learning import autolearn_behavior as ab
from kaiba.learning import fixed_wallet_study as fs
from kaiba.learning.autolearn_io import GuardedReader, SourceDeferred

NOW = 1_800_000_000_000
DAY = 86_400_000


def dynamic_queue(n=20):
    return [{"chain": "bsc", "address": f"0x{i + 100:040x}"} for i in range(n)]


class RecordingReader(GuardedReader):
    def __init__(self, conn):
        super().__init__(Path(conn.execute("PRAGMA database_list").fetchone()[2]))
        self.calls = []

    def rows(self, sql, args=()):
        self.calls.append((sql, args))
        return super().rows(sql, args)


def swap(conn, *, offset, token, side="buy", money="100", owner=None):
    conn.execute("INSERT INTO swaps(chain,tx,ts_ms,wallet,token,side,amount_token,amount_native,usd_value,source) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)", ("sol", f"{offset}:{side}:{token}", NOW + offset,
                 owner or fs.MEMBERS[0][1], token, side, "100", money, money, "helius"))


def observe(conn, *, now=NOW, dynamic=None, study=None, queue=None, collector=None):
    return fs.collect_fixed_study(RecordingReader(conn), dynamic_queue() if queue is None else queue,
                                  now_ms=now, dynamic_state=dynamic,
                                  study_state=fs.initial_study_state() if study is None else study, collector=collector)


def test_real_observer_freezes_six_current_clocks_and_separates_old_dynamic_selection(tmp_db):
    fixed = [{"chain": c, "address": a} for c, a, _ in fs.MEMBERS]
    old = ab.collect_behavior(RecordingReader(tmp_db), fixed, now_ms=NOW - DAY)["next_state"]
    old["round"] = 17
    original = deepcopy(old)
    swap(tmp_db, offset=-2000, token="OLD")
    first = observe(tmp_db, dynamic=old)
    state = first["study_next_state"]
    assert state["manifest"]["selected_ms"] == NOW
    assert state["manifest"]["rationale_is_forward_evidence"] is False
    assert len(state["selections"]) == 6
    assert all(r["selection_ms"] == r["baseline"]["captured_ms"] == NOW for r in state["selections"].values())
    assert first["dynamic_next_state"]["round"] == 17
    for name, row in original["selections"].items():
        assert first["dynamic_next_state"]["selections"][name] == row
        assert first["dynamic_next_state"]["reports"][name] == original["reports"][name]
    assert old == original
    frozen = deepcopy(state["selections"])
    swap(tmp_db, offset=1000, token="OLD", side="sell", money="50")
    swap(tmp_db, offset=2000, token="NEW")
    swap(tmp_db, offset=3000, token="NEW", side="sell", money="150")
    later = observe(tmp_db, now=NOW + 5000, dynamic=first["dynamic_next_state"], study=state)
    assert later["study_next_state"]["selections"] == frozen
    report = next(r for r in later["behavior"]["followthrough_reports"] if r["wallet"] == fs.MEMBERS[0][1])
    assert report["post_selection_new_entries"]["gross_money_axis"]["gross_profit"] == "50"
    assert report["preexisting_inventory_followthrough"]["gross_money_axis"]["gross_profit"] == "-50"
    assert report["profitability_status"] == "UNPROVEN" and not report["live_copy_approved"]
    assert report["baseline_comparison_status"] == "OBSERVED_GROSS_PARTIAL_ONLY"


def test_one_actual_collector_fixed_six_plus_four_rotate_without_extra_reads(tmp_db):
    reader, calls = RecordingReader(tmp_db), []
    queue = dynamic_queue()
    original = deepcopy(queue)
    dynamic, study = None, fs.initial_study_state()
    for i in range(5):
        def collected(*args, **kwargs):
            calls.append(deepcopy(args[1]))
            return ab.collect_behavior(*args, **kwargs)
        out = fs.collect_fixed_study(reader, queue, now_ms=NOW + i * 900_000,
                                    dynamic_state=dynamic, study_state=study, collector=collected)
        dynamic, study = out["dynamic_next_state"], out["study_next_state"]
        assert dynamic["round"] == 0
        assert len(out["behavior"]["fixed_study"]["observation_queue"]) == 20
        assert out["behavior"]["coverage"]["attempted"] == 10
        assert len(study["selections"]) == 6 and "reports" not in study
    assert len(calls) == 5 and all(len(q) == 10 for q in calls) and queue == original
    counts = Counter(row["address"] for q in calls for row in q[6:])
    assert len(counts) == 20 and set(counts.values()) == {1}
    tape = [(sql, args) for sql, args in reader.calls if "FROM swaps" in sql]
    assert len(tape) == 50 and len(reader.calls) == 55
    fixed_counts = Counter(args[1] for _, args in tape if args[1] in {a for _, a, _ in fs.MEMBERS})
    assert set(fixed_counts.values()) == {5} and len(fixed_counts) == 6
    for sql, args in tape:
        assert args[2] >= args[3] - 13 * DAY and args[4] <= 1001
        plan = " ".join(str(tuple(r)) for r in tmp_db.execute("EXPLAIN QUERY PLAN " + sql, args))
        assert "SEARCH swaps USING INDEX idx_swaps_wallet" in plan and "TEMP B-TREE" not in plan


@pytest.mark.parametrize("change,reason", [
    ("source", "source_changed"), ("selection", "selection_changed"),
    ("missing", "missing_selection"), ("future", "invalid_selection_clock"),
    ("round", "invalid_round"), ("baseline", "invalid_baseline"),
])
def test_changed_or_corrupt_fixed_state_is_unknown_preserved_not_restarted(tmp_db, change, reason):
    first = observe(tmp_db)
    state = deepcopy(first["study_next_state"])
    name = next(iter(state["selections"]))
    if change == "source":
        state["manifest"]["source_versions"]["learning/fixed_wallet_study.py"] = "0" * 64
        state["manifest_sha256"] = fs._hash(state["manifest"])
    elif change == "selection":
        state["selections"][name]["selection_ms"] -= 1
    elif change == "missing":
        state["selections"].pop(name)
    elif change == "future":
        state["manifest"]["selected_ms"] = NOW + DAY
        state["manifest_sha256"] = fs._hash(state["manifest"])
    elif change == "round":
        state["round"] = True
    else:
        state["selections"][name]["baseline"]["captured_ms"] = NOW + DAY
    reader = RecordingReader(tmp_db)
    out = fs.collect_fixed_study(reader, dynamic_queue(), now_ms=NOW + 1000,
                                 dynamic_state=first["dynamic_next_state"], study_state=state)
    assert reason in out["behavior"]["fixed_study"]["reason"]
    assert out["study_next_state"] == state and out["dynamic_next_state"] == first["dynamic_next_state"]
    assert reader.calls == []


def test_non_wal_deferred_baseline_preserves_selection_and_never_claims_late_capture_comparable(tmp_db):
    class Deferred(RecordingReader):
        def rows(self, sql, args=()):
            if "FROM swaps" in sql and args[1] == fs.MEMBERS[0][1]:
                raise SourceDeferred("tape_query_deadline")
            return super().rows(sql, args)
    first = fs.collect_fixed_study(Deferred(tmp_db), [], now_ms=NOW, study_state=fs.initial_study_state())
    name = fs.MEMBERS[0][0] + ":" + fs.MEMBERS[0][1]
    assert first["study_next_state"]["selections"][name]["baseline"] is None
    second = observe(tmp_db, now=NOW + 900_000, dynamic=first["dynamic_next_state"],
                     study=first["study_next_state"], queue=[])
    row = second["study_next_state"]["selections"][name]
    assert row["selection_ms"] == NOW and row["baseline"]["captured_ms"] == NOW + 900_000
    report = next(r for r in second["behavior"]["followthrough_reports"] if r["wallet"] == fs.MEMBERS[0][1])
    assert report["baseline_comparison_status"].startswith("UNPROVEN")


def test_transient_wal_stays_refused_without_any_later_underlying_reads(tmp_db):
    error = SourceDeferred("wal_over_limit:1500000001; retry after at least 60s")
    class Transient(RecordingReader):
        def rows(self, sql, args=()):
            if "FROM swaps" in sql and not getattr(self, "failed", False):
                self.failed = True
                raise error
            return super().rows(sql, args)  # A later read would be allowed without the sticky guard.
    reader = Transient(tmp_db)
    with pytest.raises(SourceDeferred, match="at least 60s") as caught:
        fs.collect_fixed_study(reader, dynamic_queue(), now_ms=NOW, study_state=fs.initial_study_state())
    assert caught.value is error and len(reader.calls) == 1


def test_retention_aging_preserves_baseline_and_unknown_comparison(tmp_db):
    first = observe(tmp_db)
    original = deepcopy(first["study_next_state"]["selections"])
    tmp_db.execute("INSERT INTO kv(key,value,updated_ms) VALUES (?,?,?)",
                   ("retention:swaps:horizon_ms", str(NOW - 5 * DAY), NOW))
    second = observe(tmp_db, now=NOW + 8 * DAY, dynamic=first["dynamic_next_state"], study=first["study_next_state"])
    assert second["study_next_state"]["selections"] == original
    reports = {r["wallet"]: r for r in second["behavior"]["followthrough_reports"]}
    assert all(reports[a]["baseline_comparison_status"].startswith("UNPROVEN") for _, a, _ in fs.MEMBERS)


def test_combined_identity_capacity_is_not_increased_by_separate_fixed_namespace(tmp_db, monkeypatch):
    old = ab.collect_behavior(RecordingReader(tmp_db), dynamic_queue(3), now_ms=NOW - DAY)["next_state"]
    monkeypatch.setattr(ab, "MAX_IDENTITIES", 8)
    reader = RecordingReader(tmp_db)
    bad = fs.collect_fixed_study(reader, [], now_ms=NOW, dynamic_state=old, study_state=fs.initial_study_state())
    assert bad["behavior"]["fixed_study"]["status"] == "unknown"
    assert bad["dynamic_next_state"] == old and reader.calls == []
    assert "capacity" in bad["behavior"]["fixed_study"]["reason"]
    monkeypatch.setattr(ab, "MAX_IDENTITIES", 9)
    good = observe(tmp_db, dynamic=old, queue=[])
    assert good["behavior"]["fixed_study"]["combined_selection_count"] == 9


def test_combined_selection_byte_budget_has_refusal_and_positive_control(tmp_db, monkeypatch):
    old = ab.collect_behavior(RecordingReader(tmp_db), dynamic_queue(1), now_ms=NOW - DAY)["next_state"]
    fixed = {c + ":" + a: fs._selection(c, a, NOW) for c, a, _ in fs.MEMBERS}
    required = ab._selection_bytes(old["selections"]) + ab._selection_bytes(fixed)
    monkeypatch.setattr(ab, "MAX_SELECTION_BYTES", required - 1)
    bad = observe(tmp_db, dynamic=old, queue=[])
    assert "capacity" in bad["behavior"]["fixed_study"]["reason"]
    monkeypatch.setattr(ab, "MAX_SELECTION_BYTES", required + 500)
    good = observe(tmp_db, dynamic=old, queue=[])
    assert good["behavior"]["fixed_study"]["status"] == "observing_partial_leader_tape"
    assert good["behavior"]["fixed_study"]["combined_selection_storage_bytes"] <= required + 500


def test_dense_fixed_wallet_is_unknown_not_dropped_or_replaced(tmp_db):
    for i in range(1001):
        swap(tmp_db, offset=-i - 1, token=f"TOKEN{i}")
    out = observe(tmp_db, queue=[])
    receipt = next(r for r in out["behavior"]["receipts"] if r["selection_ms"] == NOW and r.get("rows_observed") == 1001)
    assert receipt["query_status"] == "row_cap_truncated"
    assert len(out["study_next_state"]["selections"]) == 6
    assert out["behavior"]["coverage"]["rows_read"] <= 10000
    assert out["behavior"]["coverage"]["source_bytes"] <= ab.MAX_TOTAL_BYTES
    assert out["behavior"]["coverage"]["summary_bytes"] <= ab.MAX_SUMMARY_BYTES


def test_nominee_duplicates_and_fixed_overlap_do_not_duplicate_queries(tmp_db):
    fixed = [{"chain": c, "address": a} for c, a, _ in fs.MEMBERS]
    queue = fixed + dynamic_queue(2) * 2 + [{"chain": "sol", "address": "invalid"}]
    out = observe(tmp_db, queue=queue)
    assert out["behavior"]["coverage"]["attempted"] == 8
    assert len(out["behavior"]["fixed_study"]["observation_queue"]) == 8
    with pytest.raises(ValueError, match="at most20"):
        observe(tmp_db, queue=dynamic_queue(21))


def test_lost_fixed_state_never_restarts_despite_available_dynamic_state(tmp_db):
    first = observe(tmp_db)
    reader = RecordingReader(tmp_db)
    second = fs.collect_fixed_study(reader, dynamic_queue(), now_ms=NOW + 900_000,
                                    dynamic_state=first["dynamic_next_state"], study_state=None)
    assert second["behavior"]["fixed_study"]["reason"] == "fixed_study_state_missing"
    assert second["study_next_state"]["status"] == "unknown" and reader.calls == []
    assert second["dynamic_next_state"] == first["dynamic_next_state"]
    third = fs.collect_fixed_study(reader, dynamic_queue(), now_ms=NOW + 1_800_000,
                                   dynamic_state=second["dynamic_next_state"], study_state=second["study_next_state"])
    assert third["behavior"]["fixed_study"]["status"] == "unknown" and reader.calls == []


def test_installer_marker_freezes_actual_first_capture_not_install_time(tmp_db):
    marker = fs.initial_study_state()
    result = fs.collect_fixed_study(RecordingReader(tmp_db), [], now_ms=NOW + 30_000,
                                    dynamic_state={"round": 27, "selections": {}, "reports": {}}, study_state=marker)
    assert result["study_next_state"]["manifest"]["selected_ms"] == NOW + 30_000
    assert result["dynamic_next_state"]["round"] == 27
    assert all(r["baseline"]["captured_ms"] == NOW + 30_000 for r in result["study_next_state"]["selections"].values())


def test_entirely_missing_review_state_is_unknown_without_installer_marker(tmp_db):
    reader = RecordingReader(tmp_db)
    out = fs.collect_fixed_study(reader, [], now_ms=NOW)
    assert out["behavior"]["fixed_study"]["reason"] == "fixed_study_state_missing"
    assert reader.calls == [] and "manifest" not in out["study_next_state"]


def test_nonfinite_corrupt_state_is_bounded_unknown_and_cannot_reset(tmp_db):
    reader = RecordingReader(tmp_db)
    out = fs.collect_fixed_study(reader, [], now_ms=NOW, study_state={"invalid": float("nan")})
    assert out["study_next_state"]["status"] == "unknown" and reader.calls == []
    assert len(out["study_next_state"]["prior_state_sha256"]) == 64


def test_returned_manifest_policy_cannot_mutate_process_policy(tmp_db):
    original = deepcopy(fs.POLICY)
    result = observe(tmp_db, queue=[])
    result["study_next_state"]["manifest"]["policy"]["max_wallet_reads"] = 999
    assert fs.POLICY == original
