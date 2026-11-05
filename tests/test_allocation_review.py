"""Operating review separates allocated books from immutable retired history."""
from __future__ import annotations

import copy
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from kaiba.learning import autolearn_review as review
from kaiba.learning.autolearn_io import GuardedReader
from tests.test_allocation_observer import change_snapshot, context
from tests.test_allocation_runtime import packed, provision_runtime, rig, snapshot


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket", lambda *a, **kw: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("subprocess forbidden"))


def captured(value, tmp_db, report):
    path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
    return review.collect_review(GuardedReader(path), value["root"], report, now_ms=value["clock"]["ms"])


def misleading_report(value, tmp_db):
    paths = sorted(value["books"].glob("candidate-*.db"))
    paths.append(Path(tmp_db.execute("PRAGMA database_list").fetchone()[2]))
    return {"campaigns": [{"campaign_id": value["manifest"]["campaign_id"],
        "paper": {"arms": [{"book_path": str(path)} for path in paths]}}]}


@pytest.mark.parametrize("supplied_paths", [False, True])
def test_review_uses_twelve_leases_even_with_all_twenty_two_report_paths(tmp_path, tmp_db, monkeypatch, supplied_paths):
    value = provision_runtime(rig(tmp_path))
    ctx, calls = context(value), []
    report = misleading_report(value, tmp_db) if supplied_paths else {}
    before, original = snapshot(value["root"]), review._book
    def trace(path, *, now_ms):
        calls.append(path)
        return original(path, now_ms=now_ms)
    monkeypatch.setattr(review, "_book", trace)
    result = captured(value, tmp_db, report)
    expected = {Path(p["book_path"]) for p in ctx.leased_pairs()}
    pinned = {Path(p["book_path"]) for p in ctx.retired_history()}
    assert len(expected) == len(calls) == len(result["books"]) == 12 and set(calls) == expected
    assert not (set(calls) & pinned)
    assert len(result["retired_books"]) == 10
    assert {Path(r["book_path"]) for r in result["retired_books"]} == pinned
    assert all(r["classification"] == "allocation_retired_history_read_only" and r["allocated"] is False
               and r["artifacts"] for r in result["retired_books"])
    assert sum(b["open"] for b in result["books"]) == 2
    assert result["allocation_scope"]["active_leases"] == 12
    assert result["allocation_scope"]["lifetime_family_size"] == 22
    assert result["allocation_scope"]["snapshot_token"] == ctx.snapshot_token
    assert result["section_status"]["paper_books"]["status"] == "captured"
    assert snapshot(value["root"]) == before


@pytest.mark.parametrize("fault", ["parent", "metadata", "authority"])
def test_bad_allocation_is_unknown_even_when_report_paths_look_usable(tmp_path, tmp_db, monkeypatch, fault):
    value = provision_runtime(rig(tmp_path))
    report = misleading_report(value, tmp_db)
    if fault == "parent":
        parent = copy.deepcopy(value["parent"])
        parent.pop("research_allocation")
        (value["root"] / "state.json").write_bytes(packed(parent))
    elif fault == "metadata":
        path = value["runtime_dir"] / "metadata.json"
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        (value["runtime_dir"] / "authority.json").unlink()
    before = snapshot(value["root"])
    def forbidden(*args, **kwargs):
        pytest.fail("invalid authority must not open report-provided books")
    monkeypatch.setattr(review, "_book", forbidden)
    result = captured(value, tmp_db, report)
    assert result["books"] == [] and result["retired_books"] == []
    assert result["allocation_scope"]["status"] == "unknown"
    assert result["section_status"]["paper_books"]["status"] == "partial"
    assert snapshot(value["root"]) == before


def test_each_captured_book_must_match_exact_allocated_identity(tmp_path, tmp_db, monkeypatch):
    value = provision_runtime(rig(tmp_path))
    original, changed = review._book, []
    def wrong(path, *, now_ms):
        result = original(path, now_ms=now_ms)
        if not changed:
            result["identity"]["hypothesis_id"] = "different-identity-same-campaign"
            changed.append(path)
        return result
    monkeypatch.setattr(review, "_book", wrong)
    result = captured(value, tmp_db, misleading_report(value, tmp_db))
    assert len(changed) == 1 and len(result["books"]) == 11
    assert result["section_status"]["paper_books"]["status"] == "partial"
    assert any("allocated_book_frozen_identity_mismatch" in error["reason"]
               for error in result["section_status"]["paper_books"]["errors"])


@pytest.mark.parametrize("mutation", ["retired", "snapshot"])
def test_authority_or_retired_hash_change_during_capture_cannot_leave_verified_population(tmp_path, tmp_db, monkeypatch, mutation):
    value = provision_runtime(rig(tmp_path))
    ctx, original, changed = context(value), review._book, []
    retired = Path(ctx.retired_history()[0]["book_path"])
    def changing(path, *, now_ms):
        result = original(path, now_ms=now_ms)
        if not changed:
            if mutation == "retired":
                with closing(sqlite3.connect(retired)) as conn:
                    conn.execute("INSERT OR REPLACE INTO candidate_meta VALUES ('external_retired_write','1')")
                    conn.commit()
            else:
                change_snapshot(value)
            changed.append(path)
        return result
    monkeypatch.setattr(review, "_book", changing)
    result = captured(value, tmp_db, {})
    assert changed and result["books"] == [] and result["retired_books"] == []
    assert result["allocation_scope"]["status"] == "unknown"
    assert result["section_status"]["paper_books"]["status"] == "partial"
