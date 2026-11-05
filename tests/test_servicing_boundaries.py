"""Independent actual-worker servicing boundaries; no network or production state."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from kaiba.learning import allocation_runtime as runtime
from kaiba.learning import autolearn
from kaiba.learning import cooperative_reader as cooperative
from kaiba.learning import research_service_upgrade as upgrade
from kaiba.learning import wallet_observation_io as tape_io
from kaiba.learning.autolearn_io import GuardedReader, SourceDeferred
from tests.test_allocation_runtime import packed, provision_runtime
from tests.test_research_allocation import rig
from tests.test_research_service_upgrade import provision_service_runtime

REGISTRY = "SELECT * FROM wallets WHERE chain=? AND address IN (?)"
REGISTRY_ARGS = ("sol", "A" * 30 + "AB")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("provider/process dispatch forbidden"))


def upgraded_worker(tmp_path, tmp_db, monkeypatch):
    """Explicit synthetic old installation, then one additive compatibility receipt."""
    value = provision_service_runtime(tmp_path, monkeypatch)
    (value["root"] / "worker.lock").unlink()
    artifacts = {name: (value["runtime_dir"] / name).read_bytes() for name in upgrade.ORIGINAL_FILES}
    value["original_artifacts"] = artifacts
    learner = autolearn.AutoLearner(Path(tmp_db.execute("PRAGMA database_list").fetchone()[2]), value["root"],
        learning_root=value["learning"], groups=[("sm-trenches", c) for c in ("sol", "bsc", "robinhood")],
        clock=lambda: value["clock"]["ms"] / 1000)
    return value, learner


def context(value):
    return runtime.load_context(value["root"], learning_root=value["learning"],
        now_ms=value["clock"]["ms"], clock=lambda: value["clock"]["ms"] / 1000)


def direct_worker_lock(value):
    (value["root"] / "worker.lock").write_bytes(packed({"pid": os.getpid(), "started_ms": value["t0"]}))


def clocked_service(monkeypatch, value, *, callback_check=None):
    original = cooperative.PaperService
    instances = []

    class ClockedService(original):
        def __init__(self, callback, **kwargs):
            def checked():
                if callback_check:
                    callback_check()
                return callback()
            kwargs.update(monotonic_clock=lambda: value["clock"]["ms"] / 1000,
                          wall_clock=lambda: value["clock"]["ms"] / 1000)
            super().__init__(checked, **kwargs)
            instances.append(self)

    monkeypatch.setattr(cooperative, "PaperService", ClockedService)
    return instances


def assert_originals(value):
    assert {name: (value["runtime_dir"] / name).read_bytes() for name in upgrade.ORIGINAL_FILES} == value["original_artifacts"]
    parent = json.loads((value["root"] / "state.json").read_bytes())
    assert parent[runtime.POINTER] == value["parent"][runtime.POINTER]
    assert parent[upgrade.POINTER] == value["parent"][upgrade.POINTER]


def test_upgrade_pointer_alone_refuses_without_rewriting_or_bootstrapping_parent(tmp_path, tmp_db):
    root = tmp_path / "learning" / "autolearn"
    root.mkdir(parents=True)
    parent = {"version": "autolearn/2", "pending_lessons": [], "assessment": {},
              upgrade.POINTER: {"malformed_but_latched": True}, "preserved_unknown": {"counter": 17}}
    raw = packed(parent)
    (root / "state.json").write_bytes(raw)
    learner = autolearn.AutoLearner(Path(tmp_db.execute("PRAGMA database_list").fetchone()[2]), root,
        learning_root=root.parent, clock=lambda: 1_800_000_000.0)
    result = learner.once()
    assert result["status"] in {"partial", "deferred"}, result
    assert (root / "state.json").read_bytes() == raw
    assert not list(root.glob("candidate-*.db")) and not list((root / "paper").glob("*.db"))
    assert result["source_queries"]["count"] == 0


def test_private_binding_check_requires_original_activation_even_without_service_upgrade(tmp_path):
    value = provision_runtime(rig(tmp_path))
    ctx = context(value)
    assert not ctx.service_upgrade
    # This exact clock passes the original timely-commit proof. The private
    # binding path must independently enforce activation rather than rely on a
    # later caller to run the full book guard.
    with pytest.raises(ValueError):
        ctx._revalidate_bindings(now_ms=value["activation"] - 1)


@pytest.mark.parametrize("route", ["run", "expire", "watch"])
def test_private_binding_dispatch_still_rejects_actual_retired_book_mutation(tmp_path, tmp_db, monkeypatch, route):
    value, _ = upgraded_worker(tmp_path, tmp_db, monkeypatch)
    direct_worker_lock(value)
    ctx = context(value)
    assert ctx.service_upgrade
    retired = next(row for row in value["receipt"]["legacy_books"] if row["lease"] == "retired")
    path = value["books"] / retired["book"]
    path.write_bytes(path.read_bytes() + b"\n")
    before = {p.name: p.read_bytes() for p in value["books"].glob("*.db")}
    runner, manifest, now = ctx.runner(), value["new"], value["clock"]["ms"]
    with pytest.raises((ValueError, SourceDeferred), match="retired_book_changed_no_new_allowance"):
        if route == "run":
            runner.run_candidates(manifest, [], {}, now_ms=now)
        elif route == "expire":
            runner.expire_pending(manifest, now_ms=now)
        else:
            runner.watched_tokens(manifest, now_ms=now)
    assert {p.name: p.read_bytes() for p in value["books"].glob("*.db")} == before
    assert_originals(value)


@pytest.mark.parametrize("lost", ["parent", "receipt"])
def test_existing_bound_runner_does_not_fall_back_after_upgrade_latch_loss(tmp_path, tmp_db, monkeypatch, lost):
    value, _ = upgraded_worker(tmp_path, tmp_db, monkeypatch)
    direct_worker_lock(value)
    ctx = context(value)
    runner = ctx.runner()
    if lost == "parent":
        parent = json.loads((value["root"] / "state.json").read_bytes())
        parent.pop(upgrade.POINTER)
        (value["root"] / "state.json").write_bytes(packed(parent))
    else:
        (value["upgrade_dir"] / "receipt.json").unlink()
    before = {p.name: p.read_bytes() for p in value["books"].glob("*.db")}
    with pytest.raises((ValueError, SourceDeferred)):
        runner.run_candidates(value["new"], [], {}, now_ms=value["clock"]["ms"])
    assert {p.name: p.read_bytes() for p in value["books"].glob("*.db")} == before


def test_swallowed_review_WAL_still_persists_global_source_pause(tmp_path, tmp_db, monkeypatch):
    value, learner = upgraded_worker(tmp_path, tmp_db, monkeypatch)
    instances = clocked_service(monkeypatch, value)
    after_wal, review_calls, first_pause = [], [], []
    failed = {"yes": False}

    class Reader(GuardedReader):
        def rows(self, sql, args=()):
            if failed["yes"]:
                after_wal.append(sql)
                raise AssertionError("source dispatched after swallowed WAL refusal")
            if sql == REGISTRY:
                failed["yes"] = True
                raise SourceDeferred("wal_over_limit:1500000001; retry after at least 60s")
            return super().rows(sql, args)

        def q(self, sql, args=()):
            if failed["yes"]:
                after_wal.append(sql)
                raise AssertionError("tuple source dispatched after swallowed WAL refusal")
            return super().q(sql, args)

    learner.reader_factory = Reader

    def review(reader, state, report, now):
        review_calls.append(1)
        state["wallet_review_state"] = {"independent_partial_progress": 7}
        value["clock"]["ms"] += 16_000
        try:
            reader.rows(REGISTRY, REGISTRY_ARGS)
        except SourceDeferred:
            # Retain the actual first refusal frontier, then let optional work
            # take more time before its normal return. Outer handling must not
            # extend that same pause merely because it notices it later.
            first_pause.append(json.loads(learner.state_path.read_bytes())["retry_after_ms"])
            value["clock"]["ms"] += 10_000
        report["independent_swallowed_WAL"] = True

    monkeypatch.setattr(learner, "_scheduled_reviews", review)
    result = learner.once()
    parent = json.loads(learner.state_path.read_bytes())
    assert review_calls == [1] and instances and failed["yes"]
    assert result["status"] == "deferred", result
    assert len(first_pause) == 1 and parent["retry_after_ms"] == first_pause[0]
    assert first_pause[0] == value["clock"]["ms"] + 50_000
    assert parent["wallet_review_state"] == {"independent_partial_progress": 7}
    assert not after_wal
    assert_originals(value)


def test_review_WAL_pause_is_already_durable_before_catch_and_cancellation(tmp_path, tmp_db, monkeypatch):
    value, learner = upgraded_worker(tmp_path, tmp_db, monkeypatch)
    clocked_service(monkeypatch, value)
    checked = []

    class Reader(GuardedReader):
        def rows(self, sql, args=()):
            if sql == REGISTRY:
                raise SourceDeferred("wal_over_limit:1500000001; retry after at least 60s")
            return super().rows(sql, args)

    learner.reader_factory = Reader

    def review(reader, state, report, now):
        state["wallet_review_state"] = {"independent_progress_before_cancellation": 11}
        value["clock"]["ms"] += 16_000
        try:
            reader.rows(REGISTRY, REGISTRY_ARGS)
        except SourceDeferred:
            durable = json.loads(learner.state_path.read_bytes())
            assert durable.get("retry_after_ms", 0) >= value["clock"]["ms"] + 60_000, "WAL pause not durable inside review catch"
            assert durable["wallet_review_state"] == {"independent_progress_before_cancellation": 11}
            checked.append(durable["retry_after_ms"])
            value["clock"]["ms"] += 5_000
            with pytest.raises(SourceDeferred):
                reader.guard()
            assert json.loads(learner.state_path.read_bytes())["retry_after_ms"] == checked[0]
            raise SystemExit("independent_cancel_after_durable_WAL") from None
        raise AssertionError("real WAL refusal did not reach optional review")

    monkeypatch.setattr(learner, "_scheduled_reviews", review)
    with pytest.raises(SystemExit, match="independent_cancel_after_durable_WAL"):
        learner.once()
    assert len(checked) == 1
    assert json.loads(learner.state_path.read_bytes())["retry_after_ms"] == checked[0]
    assert_originals(value)


def test_source_failure_backoff_is_not_extended_by_fifteen_second_service_cycles(tmp_path, tmp_db, monkeypatch):
    value, learner = upgraded_worker(tmp_path, tmp_db, monkeypatch)
    clocked_service(monkeypatch, value)
    start = value["clock"]["ms"]
    failed, source_reads = [], []

    class Reader(GuardedReader):
        def guard(self):
            if not failed:
                failed.append(value["clock"]["ms"])
                raise SourceDeferred("source_query_unavailable:independent_one_shot")
            return super().guard()

        def rows(self, sql, args=()):
            source_reads.append(value["clock"]["ms"])
            return super().rows(sql, args)

        def q(self, sql, args=()):
            source_reads.append(value["clock"]["ms"])
            return super().q(sql, args)

    learner.reader_factory = Reader
    monkeypatch.setattr(learner, "_scheduled_reviews", lambda *args: None)
    first = learner.once()
    assert first["status"] == "deferred", first
    assert json.loads(learner.state_path.read_bytes())["retry_after_ms"] == start + 60_000
    assert failed == [start] and not source_reads
    value["clock"]["ms"] = start + 15_000
    waiting = learner.once()
    assert waiting["status"] == "deferred", waiting
    assert json.loads(learner.state_path.read_bytes())["retry_after_ms"] == start + 60_000
    assert not source_reads
    value["clock"]["ms"] = start + 60_000
    resumed = learner.once()
    assert resumed["status"] in {"complete", "partial"}, resumed
    parent = json.loads(learner.state_path.read_bytes())
    assert parent["retry_after_ms"] == 0 and source_reads
    assert all(when >= start + 60_000 for when in source_reads)
    assert parent["last_paper_service_ms"] >= start + 60_000
    assert_originals(value)


def test_real_tape_admissions_delegate_without_callback_or_extra_physical_allowance(tmp_path, tmp_db, monkeypatch):
    from tests import test_wallet_observation_io as tape_fixture

    value, learner = upgraded_worker(tmp_path, tmp_db, monkeypatch)
    monkeypatch.setattr(tape_fixture, "T0", value["clock"]["ms"])
    source, fixed, tape_now, tape_context = tape_fixture.setup(tmp_path / "tape")
    value["clock"]["ms"] = tape_now["ms"]
    tape_context["clock"] = learner.clock
    queue = [{"chain": "bsc", "address": "0x" + str(i) * 40} for i in range(1, 5)]
    for i, row in enumerate(queue):
        source.histories[(row["chain"], row["address"])] = [tape_fixture.raw(10000 + i, value["clock"]["ms"] - 2000)]
    callbacks = []

    def callback_check():
        stored = json.loads((tape_context["store_dir"] / tape_io.STORE_FILE).read_bytes())
        assert not any(row["status"] == "admitted" for row in stored["quota"]["attempts"]), "callback after durable tape admission"
        callbacks.append(value["clock"]["ms"])

    clocked_service(monkeypatch, value, callback_check=callback_check)

    class Reader(GuardedReader):
        def rows(self, sql, args=()):
            if sql in (tape_io.HORIZON_SQL, tape_io.LEGACY_SQL, tape_io.PAGE_SQL, "SELECT registry_marker"):
                answer = source.rows(sql, args)
                if sql == tape_io.HORIZON_SQL:
                    value["clock"]["ms"] += 16_000  # Make the first admitted physical read a genuinely due boundary.
                return answer
            return super().rows(sql, args)

    learner.reader_factory = Reader

    def review(reader, state, report, now):
        state["wallet_review_state"] = {"independent_partial_progress": 9}

        def original(capture, runner_root, *, now_ms, prior_state):
            before = len(callbacks)
            value["clock"]["ms"] += 16_000
            capture.rows(REGISTRY, REGISTRY_ARGS)
            assert len(callbacks) > before, "actual selected-registry positive did not service paper"
            assert state["wallet_review_state"] == {"independent_partial_progress": 9}
            return tape_fixture.review(queue)(capture, runner_root, now_ms=now_ms, prior_state=prior_state)

        tape_io.run_review_with_tape(original, reader, Path("unused"), now,
            {"fixed_study_state": fixed}, **tape_context)
        state["wallet_review_state"]["after_callback_progress"] = 10

    monkeypatch.setattr(learner, "_scheduled_reviews", review)
    result = learner.once()
    assert result["status"] in {"complete", "partial"}, result
    stored = tape_fixture.state(tape_context, {"ms": value["clock"]["ms"]})
    assert len(source.physical) == len(stored["quota"]["attempts"]) == 10
    assert stored["quota"]["lifetime_attempts"] == 10 and stored["quota"]["slot"] == 0
    assert sum(row["kind"] == "fixed" for row in stored["quota"]["attempts"]) == 6
    assert sum(row["kind"] == "dynamic" for row in stored["quota"]["attempts"]) == 4
    assert stored["engine_state"]["fixed_state"]["selections"] == fixed["selections"]
    parent = json.loads(learner.state_path.read_bytes())
    assert parent["wallet_review_state"] == {"independent_partial_progress": 9, "after_callback_progress": 10}
    assert_originals(value)


def test_callbacks_close_source_reads_and_cannot_ack_journal_or_enter_economic_capture(tmp_path, tmp_db, monkeypatch):
    from kaiba.learning import economic_work

    value, learner = upgraded_worker(tmp_path, tmp_db, monkeypatch)
    active, callbacks, captures, journal_calls = set(), [], [], []
    reviewing = {"yes": False}
    original_connect = sqlite3.connect

    class Connection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            active.add(id(self))

        def close(self):
            try:
                super().close()
            finally:
                active.discard(id(self))

    def connect(database, *args, **kwargs):
        name = str(database)
        if name == str(learner.db_path) or name.startswith(learner.db_path.resolve().as_uri()):
            kwargs["factory"] = Connection
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)

    def callback_check():
        assert not active, "paper callback began while its source connection remained open"
        callbacks.append(value["clock"]["ms"])

    clocked_service(monkeypatch, value, callback_check=callback_check)
    original_capture, original_journal = economic_work.capture_work, autolearn.persist_lessons

    def capture(reader, *args, **kwargs):
        assert not reviewing["yes"], "paper service recursively entered economic capture"
        assert not isinstance(reader, cooperative.CooperativeReader)
        captures.append(1)
        return original_capture(reader, *args, **kwargs)

    def journal(*args, **kwargs):
        assert not reviewing["yes"], "paper service acknowledged lessons inside optional review"
        journal_calls.append(1)
        return original_journal(*args, **kwargs)

    monkeypatch.setattr(economic_work, "capture_work", capture)
    monkeypatch.setattr(autolearn, "persist_lessons", journal)
    evidence_id = "independent-servicing-stable-journal"

    def review(reader, state, report, now):
        assert isinstance(reader, cooperative.CooperativeReader)
        assert reader.safe_predicate("rows", REGISTRY, REGISTRY_ARGS) is True
        for operation, sql, args in [
            ("q", REGISTRY, REGISTRY_ARGS),
            ("rows", REGISTRY + " -- same words", REGISTRY_ARGS),
            ("rows", REGISTRY + " UNION ALL SELECT * FROM swaps", REGISTRY_ARGS),
            ("rows", tape_io.HORIZON_SQL, ()),
            ("rows", tape_io.LEGACY_SQL, ("sol", REGISTRY_ARGS[1], 0, now, 1001)),
            ("rows", "SELECT * FROM positions", ()),
        ]:
            assert reader.safe_predicate(operation, sql, args) is False
        reviewing["yes"] = True
        try:
            state["pending_lessons"].append({"evidence_id": evidence_id, "lane": None,
                "text": "Independent boundary fixture.", "body": "{}"})
            for _ in range(2):
                value["clock"]["ms"] += 16_000
                before = len(callbacks)
                reader.rows(REGISTRY, REGISTRY_ARGS)
                assert len(callbacks) > before
                assert tmp_db.execute("SELECT COUNT(*) FROM journal WHERE subject=?",
                                      ("autolearn:" + evidence_id,)).fetchone()[0] == 0
            state["wallet_review_state"] = {"independent_progress_after_two_callbacks": True}
        finally:
            reviewing["yes"] = False

    monkeypatch.setattr(learner, "_scheduled_reviews", review)
    result = learner.once()
    assert result["status"] in {"complete", "partial"}, result
    assert len(callbacks) >= 3 and len(captures) == 1 and journal_calls
    assert not active
    assert tmp_db.execute("SELECT COUNT(*) FROM journal WHERE subject=?",
                          ("autolearn:" + evidence_id,)).fetchone()[0] == 1
    parent = json.loads(learner.state_path.read_bytes())
    assert parent["wallet_review_state"] == {"independent_progress_after_two_callbacks": True}
    assert not any(row["evidence_id"] == evidence_id for row in parent["pending_lessons"])
    assert result["allocation_assessment"]["family_size"] == 22
    assert_originals(value)
