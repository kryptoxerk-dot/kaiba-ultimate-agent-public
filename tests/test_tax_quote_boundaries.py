"""Independent consumers of a real disposable tax-upgraded allocation."""
from __future__ import annotations

import copy
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from kaiba.learning import allocation_runtime as runtime
from kaiba.learning import autolearn
from kaiba.learning import autolearn_review as review
from kaiba.learning import prospective_quotes as quotes
from kaiba.learning import tax_quote_upgrade as upgrade
from kaiba.learning.autolearn_io import GuardedReader
from tests.test_allocation_runtime import context, packed, snapshot
from tests.test_research_service_upgrade import originals
from tests.test_tax_quote_upgrade import provision_tax_runtime


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("external dispatch forbidden"))


def observer(value, tmp_db):
    class NoDispatch:
        def token_info(self, *a, **k):
            pytest.fail("this boundary must not dispatch a provider request")

    db = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
    return quotes.ProspectiveQuoteCollector(value["root"], value["learning"] / "prospective-quotes",
        learning_root=value["learning"], db_path=db,
        clock=lambda: value["clock"]["ms"] / 1000, provider=NoDispatch())


def operating_review(value, tmp_db):
    db = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
    return review.collect_review(GuardedReader(db), value["root"], {}, now_ms=value["clock"]["ms"])


def test_observer_and_review_share_tax_bound_snapshot_and_original_family(tmp_path, tmp_db, monkeypatch):
    value = provision_tax_runtime(tmp_path, monkeypatch)
    ctx, original = context(value), originals(value)
    before = snapshot(value["root"])
    targets, errors = observer(value, tmp_db)._targets(value["clock"]["ms"])
    assert not errors and len(targets) == 1 and targets[0]["kind"] == "open"
    assert targets[0]["allocation_snapshot_token"] == ctx.snapshot_token
    captured = operating_review(value, tmp_db)
    assert captured["allocation_scope"]["snapshot_token"] == ctx.snapshot_token
    assert captured["allocation_scope"]["lifetime_family_size"] == 22
    assert len(captured["books"]) == 12 and len(captured["retired_books"]) == 10
    assert originals(value) == original and snapshot(value["root"]) == before
    assert ctx.authority["activation_ms"] == value["activation"]


@pytest.mark.parametrize("fault", ["completion", "service_receipt", "old_source"])
def test_invalid_tax_history_stops_both_consumers_before_book_scan(tmp_path, tmp_db, monkeypatch, fault):
    value = provision_tax_runtime(tmp_path, monkeypatch)
    collector = observer(value, tmp_db)
    path = (value["tax_dir"] / "commit-receipt.json" if fault == "completion" else
            value["upgrade_dir"] / "receipt.json" if fault == "service_receipt" else
            value["tax_dir"] / "source28" / upgrade.CHANGED[0])
    path.write_bytes(path.read_bytes() + b"\n") if fault != "completion" else path.unlink()
    before = snapshot(value["root"])

    def forbidden(*a, **k):
        pytest.fail("invalid authority must be rejected before a book consumer")

    monkeypatch.setattr(collector, "_book", forbidden)
    monkeypatch.setattr(review, "_book", forbidden)
    with pytest.raises(ValueError):
        collector._targets(value["clock"]["ms"])
    captured = operating_review(value, tmp_db)
    assert captured["books"] == captured["retired_books"] == []
    assert captured["allocation_scope"]["status"] == "unknown"
    assert snapshot(value["root"]) == before


def test_observer_tax_completion_change_before_admission_keeps_slot_unspent(tmp_path, tmp_db, monkeypatch):
    value = provision_tax_runtime(tmp_path, monkeypatch)
    collector, changed = observer(value, tmp_db), []
    initial = context(value).snapshot_token

    def changed_after_metadata_io(state):
        path = value["tax_dir"] / "commit-receipt.json"
        path.write_bytes(path.read_bytes() + b"\n")
        changed.append(context(value).snapshot_token)
        return {"known_refusal": False, "fixture_metadata_only": True}

    monkeypatch.setattr(collector, "_limiter_preflight", changed_after_metadata_io)
    result = collector.once()
    assert changed and changed[0] != initial
    assert result["provider_attempts"] == result["daily_admitted_attempts"] == 0
    assert result["reason"] == "eligibility_or_allocation_changed_before_admission"


@pytest.mark.parametrize("latch", ["parent_only", "artifact_only"])
def test_actual_worker_rejects_orphan_tax_install_without_parent_rewrite(tmp_path, tmp_db, latch):
    root = tmp_path / "learning/autolearn"
    root.mkdir(parents=True)
    parent = {"version": autolearn.VERSION, "assessment": {}, "pending_lessons": [],
              "preserved_unknown": {"counter": 37}}
    if latch == "parent_only":
        parent[upgrade.POINTER] = {"invalid": True}
    else:
        (root / runtime.DIRECTORY / upgrade.DIRECTORY).mkdir(parents=True)
    raw = packed(parent)
    (root / "state.json").write_bytes(raw)
    db = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
    learner = autolearn.AutoLearner(db, root, learning_root=root.parent)
    result = learner.once()
    assert result["status"] == "partial" and result["source_queries"]["count"] == 0
    assert (root / "state.json").read_bytes() == raw
    assert not (root / "paper").exists()


@pytest.mark.parametrize("route", ["public", "run", "expire", "watch", "assess"])
def test_actual_retired_history_mutation_still_blocks_every_tax_upgraded_route(tmp_path, monkeypatch, route):
    value = provision_tax_runtime(tmp_path, monkeypatch)
    ctx = context(value)
    retired = Path(ctx.retired_history()[0]["book_path"])
    with closing(sqlite3.connect(retired)) as conn:
        conn.execute("INSERT INTO candidate_meta VALUES ('external_tax_test_write','1')")
        conn.commit()
    before = snapshot(value["root"])
    calls = {
        "public": lambda: context(value),
        "run": lambda: ctx.runner().run_candidates(value["new"], [], {}, now_ms=value["clock"]["ms"]),
        "expire": lambda: ctx.runner().expire_pending(value["new"], now_ms=value["clock"]["ms"]),
        "watch": lambda: ctx.runner().watched_tokens(value["new"], now_ms=value["clock"]["ms"]),
        "assess": lambda: ctx.assess({}, now_ms=value["clock"]["ms"], prior_state=copy.deepcopy({})),
    }
    with pytest.raises(ValueError):
        calls[route]()
    assert snapshot(value["root"]) == before
