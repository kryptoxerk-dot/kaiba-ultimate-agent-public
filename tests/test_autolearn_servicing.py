"""Same-worker cooperative polls use actual allocated books and normal entry gates."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from kaiba.core import journal
from kaiba.learning import autolearn, cooperative_reader
from kaiba.learning.autolearn_io import SourceDeferred
from tests.test_allocation_runtime import packed
from tests.test_autolearn_allocation import canonical_quote
from tests.test_research_service_upgrade import originals, provision_service_runtime

REGISTRY = "SELECT * FROM wallets WHERE chain=? AND address IN (?)"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("external dispatch forbidden"))


def worker(tmp_path, tmp_db, monkeypatch):
    value = provision_service_runtime(tmp_path, monkeypatch)
    (value["root"] / "worker.lock").unlink()
    path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
    learner = autolearn.AutoLearner(path, value["root"], learning_root=value["learning"],
        clock=lambda: value["clock"]["ms"] / 1000)
    old = cooperative_reader.PaperService
    services = []

    class ClockedService(old):
        def __init__(self, callback, **kwargs):
            kwargs.update(monotonic_clock=lambda: value["clock"]["ms"] / 1000,
                          wall_clock=lambda: value["clock"]["ms"] / 1000)
            super().__init__(callback, **kwargs)
            services.append(self)

    monkeypatch.setattr(cooperative_reader, "PaperService", ClockedService)
    return value, learner, services


@pytest.mark.parametrize("fresh_quote", [True, False])
def test_decision_published_after_first_poll_is_serviced_inside_real_long_review(
        tmp_path, tmp_db, monkeypatch, fresh_quote):
    value, learner, services = worker(tmp_path, tmp_db, monkeypatch)
    artifact_bytes = originals(value)
    signal = value["activation"] + 1
    value["clock"]["ms"] = signal + 40_000
    canonical_quote(tmp_db, value)
    published = False
    original_query_calls = []

    class PublicationReader(autolearn.PollingReader):
        def _read(self, sql, args, *, dictionaries):
            nonlocal published
            rows = super()._read(sql, args, dictionaries=dictionaries)
            if sql == REGISTRY:
                original_query_calls.append((sql, tuple(args)))
            if "FROM decisions WHERE rowid>?" in sql and not published:
                assert not rows
                value["clock"]["ms"] = signal + 41_725
                tmp_db.execute("INSERT INTO signals(signal_id,lane,chain,token,created_ms,payload_json) "
                    "VALUES(?,?,?,?,?,?)", ("servicing-signal", "sm-trenches", "robinhood", value["token"], signal - 1, "{}"))
                tmp_db.execute("INSERT INTO decisions(decision_id,ts_ms,lane,mode,chain,token,action,size_base_units,signals_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?)", ("servicing-entry", signal, "sm-trenches", "shadow", "robinhood",
                        value["token"], "enter", "1", '["servicing-signal"]'))
                published = True
            return rows

    learner.reader_factory = PublicationReader
    inside_review = {"yes": False}
    old_flush = learner._flush_lessons

    def flush(*args, **kwargs):
        assert not inside_review["yes"], "callback must never flush/ACK economic revisions"
        return old_flush(*args, **kwargs)

    monkeypatch.setattr(learner, "_flush_lessons", flush)
    old_capture = __import__("kaiba.learning.economic_work", fromlist=["capture_work"]).capture_work
    captures = []

    def capture(reader, *args, **kwargs):
        assert not inside_review["yes"]
        assert not isinstance(reader, cooperative_reader.CooperativeReader)
        captures.append(True)
        return old_capture(reader, *args, **kwargs)

    monkeypatch.setattr("kaiba.learning.economic_work.capture_work", capture)
    preserved = {"round": 17, "selection": ["all-members"], "baselines": {"fixed": "a" * 64},
                 "acquisition": {"admitted": ["original-attempt"], "counter": 9}}
    parent = json.loads(learner.state_path.read_bytes())
    parent["wallet_review_state"] = copy.deepcopy(preserved)
    learner.state_path.write_bytes(packed(parent))
    callback_observations = []

    def review(reader, state, report, now):
        inside_review["yes"] = True
        try:
            assert (value["root"] / "worker.lock").exists()
            assert report["new_opportunities"] == 0
            for i, advance in enumerate([16_000] * 15 + [13_000]):
                value["clock"]["ms"] += advance
                if fresh_quote:
                    canonical_quote(tmp_db, value)
                else:
                    tmp_db.execute("DELETE FROM token_dossiers")
                    tmp_db.execute("DELETE FROM native_prices")
                state["wallet_review_state"]["progress"] = i + 1
                reader.rows(REGISTRY, ("robinhood", value["token"]))
                if i == 0:
                    saved = json.loads(learner.state_path.read_bytes())
                    assert saved["decision_cursor"] > 0
                    assert saved["wallet_review_state"]["progress"] == 1
                    callback_observations.append(copy.deepcopy(report["campaigns"]))
            state["wallet_review_state"]["round"] = 18
            report["wallet_review"] = {"observed_ms": now, "status": "fixture-review-completed"}
        finally:
            inside_review["yes"] = False

    monkeypatch.setattr(learner, "_scheduled_reviews", review)
    result = learner.once()
    assert result["status"] in ("complete", "partial"), result
    assert published and callback_observations and result["new_opportunities"] == 1
    assert result["paper_tick_count"] >= 15
    assert result["allocation_assessment"]["family_size"] == 22
    assert len(result["paper_ticks"]) <= 32 and len(result["phases"]) <= 128
    assert original_query_calls == [(REGISTRY, ("robinhood", value["token"]))] * 16
    first = next(c for c in callback_observations[0] if c["campaign_id"] == value["new"]["campaign_id"])
    rows = [r for r in first["paper"]["assessment_rows"] if r["chain"] == "robinhood"]
    expected = sum(h["spec"]["chain"] == "robinhood" for h in value["new"]["hypotheses"])
    assert len(rows) == expected
    if fresh_quote:
        assert all(r["status"] == "open" for r in rows)
        assert all(30_000 <= r["candidate_received_ms"] - signal <= 120_000 for r in rows)
    else:
        assert all(r["status"] == "pending" for r in rows)
        assert all(r["reason"] == "pool_price_or_depth_missing_or_stale" for r in rows)
    saved = json.loads(learner.state_path.read_bytes())
    expected_state = {**preserved, "round": 18, "progress": 16}
    assert saved["wallet_review_state"] == expected_state
    assert saved["research_allocation"] == parent["research_allocation"]
    assert saved["paper_service_upgrade"] == parent["paper_service_upgrade"]
    assert originals(value) == artifact_bytes
    assert captures == [True] and len(services) == 1
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert tmp_db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    assert journal.verify(tmp_db) == (True, None)


def test_only_new_upgrade_latch_refuses_without_rewriting_parent(tmp_path, tmp_db):
    root = tmp_path / "learning/autolearn"
    root.mkdir(parents=True)
    parent = {"version": autolearn.VERSION, "pending_lessons": [], "paper_service_upgrade": {"invalid": True}}
    original = packed(parent)
    (root / "state.json").write_bytes(original)
    db_path = tmp_db.execute("PRAGMA database_list").fetchone()[2]
    learner = autolearn.AutoLearner(db_path, root, learning_root=root.parent)
    result = learner.once()
    assert result["status"] == "partial" and result["source_queries"]["count"] == 0
    assert (root / "state.json").read_bytes() == original
    assert not (root / "paper").exists()


def test_existing_source_pause_is_not_extended_by_ordinary_timer_polls(tmp_path, tmp_db, monkeypatch):
    value, learner, services = worker(tmp_path, tmp_db, monkeypatch)
    fail = {"once": True}

    class OnceWalReader(autolearn.PollingReader):
        def guard(self):
            if fail["once"]:
                fail["once"] = False
                self.wal_waits += 1
                raise SourceDeferred("wal_over_limit:1600000000")
            return super().guard()

    learner.reader_factory = OnceWalReader
    started = value["clock"]["ms"]
    failed = learner.once()
    assert failed["status"] == "deferred"
    frontier = json.loads(learner.state_path.read_bytes())["retry_after_ms"]
    assert frontier == started + 60_000 and len(services) == 1
    value["clock"]["ms"] = started + 15_000
    paused = learner.once()
    assert paused["deferred"] == "source_retry_backoff" and paused["source_queries"]["count"] == 0
    assert json.loads(learner.state_path.read_bytes())["retry_after_ms"] == frontier
    assert len(services) == 1, "expected durable backoff must not create a new failed callback"
    value["clock"]["ms"] = frontier
    resumed = learner.once()
    assert resumed["status"] in ("complete", "partial"), resumed
    assert resumed["source_queries"]["count"] > 0 and len(services) == 2
    assert json.loads(learner.state_path.read_bytes())["retry_after_ms"] == 0


def test_swallowed_review_pause_is_durable_and_keeps_its_first_frontier(tmp_path, tmp_db, monkeypatch):
    value, learner, services = worker(tmp_path, tmp_db, monkeypatch)
    artifacts = originals(value)
    captured = []

    class WalReader(autolearn.PollingReader):
        def q(self, sql, args=()):
            if sql == "SELECT 7":
                raise SourceDeferred("wal_over_limit:1600000000")
            return super().q(sql, args)

    learner.reader_factory = WalReader

    def review(reader, state, report, now):
        state["wallet_review_state"] = {"round": 19, "selection": ["original-members"], "progress": 5}
        state["unknown_namespace"] = {"counter": 13, "receipt": "preserved"}
        value["clock"]["ms"] += 1_000
        failure_ms = value["clock"]["ms"]
        with pytest.raises(SourceDeferred, match="wal_over_limit"):
            reader.q("SELECT 7")  # Default-denied callback boundary; actual tuple failure.
        durable = json.loads(learner.state_path.read_bytes())
        assert durable == state, "pause must be durable before the optional review continues"
        assert durable["retry_after_ms"] == failure_ms + 60_000
        captured.append(durable["retry_after_ms"])
        value["clock"]["ms"] += 20_000
        with pytest.raises(SourceDeferred, match="wal_source_pause"):
            reader.guard()
        assert json.loads(learner.state_path.read_bytes())["retry_after_ms"] == captured[0]

    monkeypatch.setattr(learner, "_scheduled_reviews", review)
    result = learner.once()
    durable = json.loads(learner.state_path.read_bytes())
    assert captured and result["status"] == "deferred"
    assert durable["retry_after_ms"] == captured[0], "outer failure handling must not renew this pause"
    assert durable["unknown_namespace"] == {"counter": 13, "receipt": "preserved"}
    assert durable["wallet_review_state"]["round"] == 19
    assert len(services) == 1 and services[0].snapshot()["counts"]["serviced"] == 1
    assert originals(value) == artifacts


@pytest.mark.parametrize("conflict", [False, True])
def test_paper_checkpoint_purely_deduplicates_pending_ids_and_refuses_body_conflicts(
        tmp_path, tmp_db, monkeypatch, conflict):
    value, learner, _ = worker(tmp_path, tmp_db, monkeypatch)
    parent = json.loads(learner.state_path.read_bytes())
    lesson = {"evidence_id": "pending-servicing-fixture", "lane": None,
              "text": "Fixture research observation, proposed only.", "body": '{"funding_allowed":false}'}
    parent["pending_lessons"] = [lesson, {**lesson, "body": "{}"} if conflict else copy.deepcopy(lesson)]
    learner.state_path.write_bytes(packed(parent))
    checkpoint_counts = []
    original = autolearn.atomic_json

    def observe(path, packet):
        if path == learner.state_path:
            checkpoint_counts.append(sum(r["evidence_id"] == lesson["evidence_id"] for r in packet["pending_lessons"]))
        return original(path, packet)

    monkeypatch.setattr(autolearn, "atomic_json", observe)
    result = learner.once()
    if conflict:
        assert result["status"] == "partial" and result["input_error"] == "pending_lesson_identity_conflict"
        assert tmp_db.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == 0
    else:
        assert result["status"] in ("complete", "partial"), result
        assert checkpoint_counts[0] == 1
        assert tmp_db.execute("SELECT COUNT(*) FROM journal WHERE subject=?",
                              ("autolearn:" + lesson["evidence_id"],)).fetchone()[0] == 1
        assert journal.verify(tmp_db) == (True, None)
