"""Standalone servicing controls; no production state, network or providers."""
from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from kaiba.learning.autolearn_io import SourceDeferred
from kaiba.learning.cooperative_reader import CooperativeReader, PaperService


class Clock:
    def __init__(self):
        self.mono, self.wall = 0.0, 1_800_000_000.0

    def advance(self, seconds):
        self.mono += seconds
        self.wall += seconds


class Reader:
    db_path = "fixture-only"
    query_seconds = 3

    def __init__(self):
        self.calls, self.guards, self.receipts = [], 0, []
        self.answer = [{"value": "012345678901234567890"}]
        self.error = self.guard_error = None

    @property
    def queries(self):
        return len(self.calls)

    def guard(self):
        self.guards += 1
        if self.guard_error:
            raise self.guard_error
        return 0

    def rows(self, sql, args=()):
        self.calls.append(("rows", sql, args))
        if self.error:
            raise self.error
        return self.answer

    def q(self, sql, args=()):
        self.calls.append(("q", sql, args))
        if self.error:
            raise self.error
        return [("012345678901234567890",)]


def setup(callback=None, **kwargs):
    clock, reader, calls = Clock(), Reader(), []
    service = PaperService(callback or (lambda: calls.append(clock.mono)),
        monotonic_clock=lambda: clock.mono, wall_clock=lambda: clock.wall, **kwargs)
    return clock, reader, calls, service


@pytest.mark.parametrize("sql", [
    "SELECT * FROM swaps WHERE chain=?", "SELECT value FROM kv WHERE key='retention:swaps:horizon_ms'",
    "SELECT * FROM positions", "SELECT * FROM wallets", "unrecognized operation",
])
def test_default_predicate_never_yields_even_when_due(sql):
    _, bare, callbacks, service = setup()
    reader = CooperativeReader(bare, service)
    assert reader.rows(sql, ("sol",)) is bare.answer
    assert callbacks == [] and bare.calls == [("rows", sql, ("sol",))]


def test_explicit_safe_boundary_delegates_exact_sql_args_result_once():
    clock, bare, callbacks, service = setup()
    seen = []

    def safe(op, sql, args):
        seen.append((op, sql, args))
        return sql == "registry fixture"

    reader = CooperativeReader(bare, service, safe_predicate=safe)
    answer = reader.rows("registry fixture", ["sol", 7])
    assert answer is bare.answer
    assert seen == bare.calls == [("rows", "registry fixture", ("sol", 7))]
    assert callbacks == [0.0] and bare.guards >= 2
    clock.advance(14)
    reader.q("registry fixture", (8,))
    assert callbacks == [0.0]
    clock.advance(1)
    reader.rows("registry fixture")
    assert callbacks == [0.0, 15.0]
    assert service.snapshot()["counts"]["serviced"] == 2


def test_success_frontier_is_callback_completion_and_no_after_query_callback():
    clock, bare, callbacks, service = setup()

    def paper():
        callbacks.append(clock.mono)
        clock.advance(30)

    service.callback = paper
    reader = CooperativeReader(bare, service, safe_predicate=lambda *_: True)
    reader.rows("registry")
    assert callbacks == [0] and service.last_receipt["elapsed_ms"] == 30_000
    clock.advance(14)
    reader.rows("registry")
    assert callbacks == [0]
    clock.advance(1)
    reader.rows("registry")
    assert callbacks == [0, 45]


def test_long_source_query_never_triggers_an_after_query_callback():
    clock, bare, callbacks, service = setup()
    original = bare.rows

    def slow(sql, args):
        clock.advance(60)
        return original(sql, args)

    bare.rows = slow
    reader = CooperativeReader(bare, service, safe_predicate=lambda *_: True)
    reader.rows("registry")
    assert callbacks == [0] and bare.queries == 1
    reader.rows("registry")
    assert callbacks == [0, 60] and bare.queries == 2


def test_callback_or_nested_dispatch_inside_active_source_is_refused():
    _, bare, callbacks, service = setup()
    reader = CooperativeReader(bare, service, safe_predicate=lambda *_: True)
    original = bare.rows
    refused = []

    def active(sql, args):
        for action in (lambda: service.checkpoint("inside SQL", force=True), lambda: reader.q("nested")):
            with pytest.raises(SourceDeferred, match="active_source_dispatch_reentry"):
                action()
            refused.append(True)
        return original(sql, args)

    bare.rows = active
    reader.rows("registry")
    assert callbacks == [0] and bare.queries == 1 and len(refused) == 2
    assert not service.snapshot()["source_dispatch_active"]


def test_actual_guard_runs_again_after_callback_before_sql():
    _, bare, _, service = setup()
    service.callback = lambda: setattr(bare, "guard_error", SourceDeferred("wal_over_limit:1500000001"))
    reader = CooperativeReader(bare, service, safe_predicate=lambda *_: True)
    with pytest.raises(SourceDeferred, match="wal_over_limit"):
        reader.rows("registry")
    assert bare.calls == [] and service.snapshot()["counts"]["wal_refusals"] == 1


@pytest.mark.parametrize("failed_at_guard", [True, False])
def test_wal_failure_stops_all_dispatch_and_cannot_be_shortened_by_wall_jump(failed_at_guard):
    clock, bare, callbacks, service = setup()
    error = SourceDeferred("wallet_registry_deferred:wal_over_limit:1500000001")
    if failed_at_guard:
        bare.guard_error = error
    else:
        bare.error = error
    reader = CooperativeReader(bare, service)
    with pytest.raises(SourceDeferred, match="wal_over_limit"):
        reader.rows("registry")
    prior = bare.queries
    bare.error = bare.guard_error = None
    clock.wall += 3600
    for sql in ("SELECT * FROM swaps", "unknown"):
        with pytest.raises(SourceDeferred, match="wal_source_pause"):
            reader.rows(sql)
    assert bare.queries == prior and callbacks == []
    clock.mono = 59.999
    with pytest.raises(SourceDeferred, match="wal_source_pause"):
        reader.q("registry")
    clock.mono = 60
    reader.q("registry")
    assert bare.queries == prior + 1


def test_backward_wall_clock_is_not_rewritten_or_used_to_shorten_quiet():
    clock, bare, _, service = setup()
    service.note_source_failure(SourceDeferred("wal_over_limit:2000000000"))
    clock.mono += 60
    clock.wall -= 5
    reader = CooperativeReader(bare, service)
    with pytest.raises(SourceDeferred, match="wal_source_pause"):
        reader.rows("unknown")
    clock.wall += 65
    reader.rows("unknown")
    service.callback = lambda: setattr(clock, "wall", clock.wall - 10)
    receipt = service.checkpoint("explicit safe phase")
    assert receipt["wall_elapsed_ms"] == -10_000
    assert receipt["elapsed_ms"] == 0 and receipt["wall_clock_regressed"] is True


@pytest.mark.parametrize("result", [False, {"status": "deferred"}, {"status": "locked"}, {"status": "failed"},
                                   {"status": "partial"}, {"status": "running"}, {"status": "unavailable"}])
def test_callback_refusal_never_dispatches_or_marks_success(result):
    _, bare, _, service = setup(lambda: result)
    reader = CooperativeReader(bare, service, safe_predicate=lambda *_: True)
    with pytest.raises(SourceDeferred, match="did_not_complete"):
        reader.rows("registry")
    assert bare.calls == [] and service.snapshot()["counts"]["serviced"] == 0
    assert service.last_receipt["status"] == "failed"
    service.callback = lambda: None
    reader.rows("registry")
    assert bare.queries == 1 and service.snapshot()["counts"]["serviced"] == 1


def test_raised_callback_failure_propagates_without_consuming_original_query():
    _, bare, _, service = setup()
    original = RuntimeError("paper fixture failed")

    def fail():
        raise original

    service.callback = fail
    reader = CooperativeReader(bare, service, safe_predicate=lambda *_: True)
    with pytest.raises(RuntimeError) as found:
        reader.rows("registry")
    assert found.value is original and bare.calls == []
    assert service.snapshot()["counts"]["serviced"] == 0 and not service.snapshot()["busy"]


def test_callback_wal_failure_is_sticky_and_original_sql_is_not_sent():
    clock, bare, _, service = setup()

    def fail():
        raise SourceDeferred("wal_over_limit:3000000000")

    service.callback = fail
    reader = CooperativeReader(bare, service, safe_predicate=lambda *_: True)
    with pytest.raises(SourceDeferred, match="wal_over_limit"):
        reader.rows("registry")
    service.callback = lambda: None
    clock.advance(59)
    with pytest.raises(SourceDeferred, match="wal_source_pause"):
        reader.rows("unknown")
    assert bare.calls == []
    clock.advance(1)
    reader.rows("registry")
    assert bare.queries == 1


@pytest.mark.parametrize("reenter_reader", [True, False])
def test_paper_callback_cannot_reenter_service_or_wrapped_reader(reenter_reader):
    _, bare, _, service = setup()
    reader = CooperativeReader(bare, service, safe_predicate=lambda *_: True)
    service.callback = (lambda: reader.rows("nested")) if reenter_reader else (lambda: service.checkpoint("nested"))
    with pytest.raises(SourceDeferred, match="reentry"):
        reader.rows("registry")
    assert bare.calls == [] and service.snapshot()["counts"]["serviced"] == 0


def test_foreign_thread_cannot_query_or_service_and_owner_remains_usable():
    _, bare, callbacks, service = setup()
    reader = CooperativeReader(bare, service, safe_predicate=lambda *_: True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(reader.rows, "foreign")
        with pytest.raises(SourceDeferred, match="foreign_thread"):
            future.result()
    assert callbacks == [] and bare.calls == []
    reader.rows("owner")
    assert callbacks == [0] and bare.queries == 1


def test_predicate_failure_or_implicit_truthy_admission_does_not_dispatch():
    _, bare, _, service = setup()
    reader = CooperativeReader(bare, service, safe_predicate=lambda *_: 1)
    with pytest.raises(ValueError, match="explicitly return bool"):
        reader.rows("registry")
    assert bare.calls == []


def test_guard_and_original_query_failures_are_not_retried_or_replayed():
    _, bare, _, service = setup()
    reader = CooperativeReader(bare, service)
    error = SourceDeferred("source_worker_wall_timeout")
    bare.error = error
    with pytest.raises(SourceDeferred) as found:
        reader.rows("registry", ("unchanged",))
    assert found.value is error and bare.calls == [("rows", "registry", ("unchanged",))]
    bare.error = None
    reader.rows("registry", ("unchanged",))
    assert bare.queries == 2


def test_nonfinite_and_regressed_monotonic_clock_refuse_dispatch():
    clock, bare, _, service = setup()
    reader = CooperativeReader(bare, service)
    reader.rows("first")
    clock.advance(5)
    reader.rows("second")
    clock.mono = 4
    with pytest.raises(SourceDeferred, match="monotonic_clock_regressed"):
        reader.rows("third")
    clock.mono = float("nan")
    with pytest.raises(SourceDeferred, match="invalid_clock"):
        reader.rows("fourth")
    assert bare.queries == 2


def test_between_checkpoint_wall_regression_remains_visible():
    clock, _, _, service = setup()
    first = service.checkpoint("first")
    clock.mono += 16
    clock.wall -= 10
    later = service.checkpoint("later")
    assert later["started_ms"] == first["completed_ms"] - 10_000
    assert later["wall_clock_regressed"] is True
    assert service.snapshot()["wall_clock_regressions"] == 1


def test_wal_with_invalid_clock_cannot_quietly_recover_without_a_frontier():
    clock, bare, _, service = setup()
    clock.mono = float("nan")
    with pytest.raises(SourceDeferred, match="invalid_clock"):
        service.note_source_failure(SourceDeferred("wal_over_limit:2000000000"))
    clock.mono = 0
    reader = CooperativeReader(bare, service)
    with pytest.raises(SourceDeferred, match="wal_quiet_clock_unknown"):
        reader.rows("unknown")
    assert bare.calls == [] and service.snapshot()["wal_quiet_clock_unknown"] is True


def test_real_disposable_sqlite_read_preserves_text_and_source_bytes(tmp_path):
    path = tmp_path / "fixture.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE facts(value TEXT)")
        conn.execute("INSERT INTO facts VALUES (?)", ("0012345678901234567890",))
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    class LocalReader(Reader):
        def rows(self, sql, args=()):
            self.calls.append(("rows", sql, args))
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                return [dict(row) for row in conn.execute(sql, args)]

    _, _, callbacks, service = setup()
    bare = LocalReader()
    reader = CooperativeReader(bare, service, safe_predicate=lambda op, sql, args: sql == "SELECT value FROM facts")
    assert reader.rows("SELECT value FROM facts") == [{"value": "0012345678901234567890"}]
    assert callbacks == [0] and bare.queries == 1
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_removed_pause_control_is_detected_after_real_wal_refusal(monkeypatch):
    _, bare, _, service = setup()
    reader = CooperativeReader(bare, service)
    service.note_source_failure(SourceDeferred("wal_over_limit:2000000000"))
    with pytest.raises(SourceDeferred, match="wal_source_pause"):
        reader.rows("original")
    assert bare.calls == []
    monkeypatch.setattr(service, "before_source", lambda: None)
    reader.rows("original")
    assert bare.queries == 1  # Removed control admits exactly the unsafe dispatch.


def test_removed_reentry_control_exposes_nested_callback(monkeypatch):
    _, _, _, service = setup()
    calls = []

    def callback():
        calls.append(len(calls))
        if len(calls) == 1:
            service.checkpoint("nested", force=True)

    service.callback = callback
    with pytest.raises(SourceDeferred, match="reentry"):
        service.checkpoint("baseline")
    assert len(calls) == 1
    calls.clear()
    monkeypatch.setattr(service, "_owner_check", lambda **_: None)
    service.checkpoint("removed guard")
    assert len(calls) == 2  # Baseline's exact unsafe nested action is now observable.
