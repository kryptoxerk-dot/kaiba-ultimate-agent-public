"""Real readonly review capture, independent population caps, and unavailable sections."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from kaiba.learning import autolearn_review as capture
from kaiba.learning.agent_review import build_review
from kaiba.learning.autolearn_io import DAY_MS, WAL_LIMIT, GuardedReader, SourceDeferred

NOW = 1_800_000_000_000


def source_path(conn):
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


def position(conn, pid, *, closed=NOW - 1000, mode="live"):
    conn.execute(
        "INSERT INTO positions(position_id,chain,token,lane,mode,opened_ms,closed_ms,qty,qty_total,cost_native,proceeds_native,realized_native) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, "sol", "token", "sm-trenches", mode, closed - 10_000, closed, "0", "100", "100", "90", "-10"),
    )


def order(conn, oid, *, mode="live", state="filled", costs=False):
    conn.execute(
        "INSERT INTO orders(order_id,chain,token,side,lane,mode,input_token,output_token,amount_in,min_out,slippage_bps,state,provider,created_ms,updated_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (oid, "sol", "token", "buy", "sm-trenches", mode, "SOL", "token", "100", "1", 1500, state, "gmgn", NOW - 1000, NOW - 1000),
    )
    if costs:
        conn.execute("INSERT INTO fill_costs(order_id,reconciled) VALUES (?,1)", (oid,))


def paper_book(root, name, cid):
    path = root / "paper" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE candidate_meta(key TEXT PRIMARY KEY,value TEXT);"
            "CREATE TABLE candidate_entries(signal_id TEXT PRIMARY KEY,payload TEXT,status TEXT,reason TEXT,"
            "decision_id TEXT,position_id TEXT,quote_ms INTEGER,latency_ms INTEGER);"
            "CREATE TABLE orders(state TEXT);CREATE TABLE positions(closed_ms INTEGER);"
        )
        conn.execute("INSERT INTO candidate_meta VALUES ('identity',?)", (json.dumps({
            "campaign_id": cid, "hypothesis_id": name, "spec": {"chain": "sol", "lane": "sm-trenches"}}),))
        conn.execute("INSERT INTO candidate_entries(signal_id,payload,status,reason) VALUES (?,?,?,?)",
                     ("old", json.dumps({"signal_ms": NOW - 100_000, "token": "public-token"}), "rejected", "latency_unreachable"))
    return path


def report_for(*paths):
    return {"campaigns": [{"campaign_id": "active", "paper": {"arms": [{"book_path": str(p)} for p in paths]}}]}


def test_live_period_is_capped_explicitly_without_counting_shadow_or_ancient_positions(tmp_db, tmp_path, monkeypatch):
    position(tmp_db, "new")
    position(tmp_db, "older", closed=NOW - 2000)
    position(tmp_db, "shadow", mode="shadow")
    position(tmp_db, "ancient", closed=NOW - 13 * DAY_MS)
    monkeypatch.setattr(capture, "POSITION_LIMIT", 1)
    reader = GuardedReader(source_path(tmp_db))
    before = tmp_db.total_changes
    result = capture.collect_review(reader, tmp_path / "research", {}, now_ms=NOW)
    assert [p["position_id"] for p in result["closed_live"]] == ["new"]
    assert result["scope"]["start_ms"] == NOW - 12 * DAY_MS
    assert result["scope"]["row_limit"] == 1 and result["scope"]["truncated"] is True
    assert result["scope"]["complete"] is False and result["scope"]["total_matching"] is None
    assert result["scope"]["total_matching_lower_bound"] == 2
    assert tmp_db.total_changes == before
    assert max(r["duration_s"] for r in reader.receipts) < 3


def test_cost_and_ops_denominators_are_recent_rowid_windows_not_entire_history(tmp_db, tmp_path, monkeypatch):
    for i in range(1, 7):
        order(tmp_db, str(i), mode="shadow" if i == 5 else "live", costs=i in (1, 4))
    for i in range(4):
        tmp_db.execute("INSERT INTO ops_runs(job,started_ms,status) VALUES (?,?,?)", ("job-" + str(i), NOW - 1000 + i, "ok"))
    monkeypatch.setattr(capture, "ORDER_WINDOW", 3)
    monkeypatch.setattr(capture, "OPS_WINDOW", 2)
    result = capture.collect_review(GuardedReader(source_path(tmp_db)), tmp_path / "research", {}, now_ms=NOW)
    assert result["cost_coverage"] == [{"chain": "sol", "mode": "live", "filled_orders": 2,
                                        "fill_cost_orders": 1, "reconciled_orders": 1}]
    assert result["cost_coverage_scope"]["rowid_min"] == 4 and result["cost_coverage_scope"]["rowid_max"] == 6
    assert result["cost_coverage_scope"]["entire_period_population"] is False
    assert {r["job"] for r in result["ops_runs"]} == {"job-2", "job-3"}
    review = build_review(result, now_ms=NOW)
    assert review["fill_cost_coverage"][0]["fill_cost_fraction"] == .5
    assert review["economic_profit_verified"] is False


def test_oversized_source_wal_stops_all_canonical_queries_and_reports_unknown(tmp_db, tmp_path, monkeypatch):
    reader = GuardedReader(source_path(tmp_db))
    monkeypatch.setattr(reader, "_wal_bytes", lambda: WAL_LIMIT + 1)
    result = capture.collect_review(reader, tmp_path / "research", {}, now_ms=NOW)
    assert reader.queries == 0 and result["source_deferred"]
    assert result["closed_live"] is None and result["cost_coverage"] is None and result["ops_runs"] is None
    assert result["section_status"]["closed_live"]["status"] == "unknown"
    assert result["section_status"]["cost_coverage"]["reason"] == "source_admission_previously_deferred"
    assert result["scope"]["complete"] is False and result["scope"]["truncated"] is None
    assert "restore_review_evidence_capture" in {r["code"] for r in build_review(result, now_ms=NOW)["repair_actions"]}


def test_nonwal_query_failure_keeps_that_section_unknown_but_allows_other_bounded_sections(tmp_db, tmp_path, monkeypatch):
    reader = GuardedReader(source_path(tmp_db))
    original = reader.q

    def fail_cost(sql, args=()):
        if "FROM orders" in sql:
            raise SourceDeferred("source_query_unavailable:OperationalError")
        return original(sql, args)

    monkeypatch.setattr(reader, "q", fail_cost)
    result = capture.collect_review(reader, tmp_path / "research", {}, now_ms=NOW)
    assert result["closed_live"] == [] and result["cost_coverage"] is None
    assert result["section_status"]["cost_coverage"]["status"] == "unknown"
    assert result["section_status"]["ops_runs"]["status"] == "captured"
    assert result["ops_runs"] == [] and result["source_deferred"]


def test_active_books_are_read_without_migration_and_retired_twentyfour_book_directory_cannot_hide_them(tmp_db, tmp_path):
    root = tmp_path / "research"
    active = [paper_book(root, f"candidate-z-active-{i}.db", "active") for i in range(12)]
    retired = [paper_book(root, f"candidate-a-retired-{i}.db", "retired") for i in range(12)]
    (root / "latest-wallet-review.json").write_text(json.dumps({"coverage": {"unique_chain_addresses": 4}}), encoding="utf-8")
    (root / "latest-discovery.json").write_text(json.dumps({"candidate_manifest": {"tried": 840, "holdout_survivors": 0}}), encoding="utf-8")
    before = {p: p.read_bytes() for p in active + retired}
    result = capture.collect_review(GuardedReader(source_path(tmp_db)), root, report_for(*active), now_ms=NOW)
    assert len(result["books"]) == 12 and len(result["retired_books"]) == 12
    assert {b["identity"]["campaign_id"] for b in result["books"]} == {"active"}
    assert result["section_status"]["paper_books"]["status"] == "captured"
    assert all(b["latest"][0]["registered_ms"] is None for b in result["books"])
    assert all(p.read_bytes() == content for p, content in before.items())
    assert result["wallet_review"]["coverage"]["unique_chain_addresses"] == 4
    assert build_review(result, now_ms=NOW)["retired_book_count"] == 12


def test_paper_book_alias_to_source_is_rejected_before_reading_as_research(tmp_db, tmp_path):
    root = tmp_path / "research"
    path = root / "paper" / "candidate-alias.db"
    path.parent.mkdir(parents=True)
    os.link(source_path(tmp_db), path)
    result = capture.collect_review(GuardedReader(source_path(tmp_db)), root, report_for(path), now_ms=NOW)
    assert result["books"] == [] and result["section_status"]["paper_books"]["status"] == "partial"
    assert result["section_status"]["paper_books"]["errors"][0]["reason"] == "research_book_path_refused"
    assert build_review(result, now_ms=NOW)["paper_learning"]["status"] == "SOURCE_UNAVAILABLE"


def test_corrupt_owned_json_is_unknown_and_never_erases_last_report_state(tmp_db, tmp_path):
    root = tmp_path / "research"
    root.mkdir()
    path = root / "latest-wallet-review.json"
    path.write_text("{broken", encoding="utf-8")
    result = capture.collect_review(GuardedReader(source_path(tmp_db)), root, {}, now_ms=NOW)
    assert result["wallet_review"] is None and result["section_status"]["wallet_review"]["status"] == "unknown"
    assert path.read_text(encoding="utf-8") == "{broken"


def test_reader_cannot_silently_widen_query_time_budget(tmp_db, tmp_path):
    with pytest.raises(ValueError, match="<=3s"):
        capture.collect_review(GuardedReader(source_path(tmp_db), query_seconds=4), tmp_path, {}, now_ms=NOW)


def test_retired_books_are_not_opened_while_collecting_active_books(tmp_db, tmp_path, monkeypatch):
    original = capture._book
    calls = []

    def observe(path, **kwargs):
        calls.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(capture, "_book", observe)
    root = tmp_path / "research"
    active = paper_book(root, "candidate-active.db", "active")
    retired = paper_book(root, "candidate-retired.db", "retired")
    capture.collect_review(GuardedReader(source_path(tmp_db)), root, report_for(active), now_ms=NOW)
    assert calls == [active] and retired not in calls


def test_missing_active_book_directory_does_not_report_an_empty_observed_population(tmp_db, tmp_path):
    root = tmp_path / "research"
    path = root / "paper" / "candidate-required.db"
    result = capture.collect_review(GuardedReader(source_path(tmp_db)), root, report_for(path), now_ms=NOW)
    assert result["section_status"]["paper_books"]["status"] == "partial"
    assert result["section_status"]["paper_books"]["errors"][0]["reason"] == "active_research_book_directory_missing"
    assert build_review(result, now_ms=NOW)["paper_learning"]["status"] == "SOURCE_UNAVAILABLE"
