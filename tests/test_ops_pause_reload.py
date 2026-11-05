"""Existing OPS reload/reap contract, with memory-only SQL and disposable YAML.

COST-PAID-RESPONSE-20261019: no scheduler/config changes or live operations.
Run completion and locks are explicit doubles; reload, eligibility, sync and
outcome publication use the actual Scheduler methods. This is not proof that a
live process is quiescent or that pausing its exit-producing jobs is safe.
"""
from __future__ import annotations

import copy
import json
import random
import socket
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from kaiba.ops import scheduler as ops


class DummyLock:
    def __init__(self):
        self.releases = 0
        self.heartbeats = 0

    def release(self):
        self.releases += 1

    def heartbeat(self):
        self.heartbeats += 1


def quotas(conn):
    return [tuple(row) for row in conn.execute("SELECT * FROM ops_quota ORDER BY job, period")]


def history(conn):
    return [tuple(row) for row in conn.execute(
        "SELECT name,last_run_ms,last_success_ms,last_error_ms,last_status,last_error,"
        "last_result_json,consecutive_failures,runs,failures,credits_total FROM ops_jobs ORDER BY name"
    )]


@pytest.fixture
def pause_env(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("reload/reap proof must not dispatch a worker or contact a provider")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(ops.core_db, "connect", forbidden)
    monkeypatch.setattr(ops.core_db, "get_conn", forbidden)
    connect = sqlite3.connect

    def memory_only(database, *args, **kwargs):
        assert database == ":memory:", "only the disposable in-memory database is allowed"
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", memory_only)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    migration = Path(ops.__file__).parents[1] / "core/migrations/019_scheduler.sql"
    conn.executescript(migration.read_text(encoding="utf-8"))
    clock = [100.0]
    jobs = {name: ops.JobSpec(name, forbidden, "synthetic local job", spends_helius=False)
            for name in ("held", "orphan", "waiting", "already_off")}
    original = ops.ScheduleConfig(
        jitter_pct=0, stagger_s=20, reload_every_s=1, max_concurrent=3,
        jobs={name: ops.JobConfig(interval_s=60, timeout_s=30, enabled=name != "already_off",
                                  marker="preserve-original-parameter") for name in jobs},
    )
    path = tmp_path / "schedule.yaml"
    original_raw = yaml.safe_dump(original.model_dump(), sort_keys=False).encode()
    path.write_bytes(original_raw)
    emitted = []

    def make(config):
        sched = ops.Scheduler(
            config, jobs=jobs, conn=conn, conn_factory=forbidden, lock_dir=tmp_path / "unused-locks",
            clock=lambda: clock[0], config_path=path, rng=random.Random(1),
        )
        sched.service_lock = DummyLock()
        # Only event-bus publication is doubled; actual ops_jobs/ops_runs writes remain.
        monkeypatch.setattr(sched, "_emit", lambda outcome, **kwargs: emitted.append(outcome))
        monkeypatch.setattr(sched, "_dispatch", forbidden)
        return sched

    sched = make(original)
    sched.sync_jobs()

    def run(name, started, status):
        cursor = conn.execute(
            "INSERT INTO ops_runs(job,started_ms,finished_ms,status,error) VALUES(?,?,?,?,?)",
            (name, started, None if status == ops.STATUS_RUNNING else 90_000, status,
             None if status == ops.STATUS_RUNNING else "original-timeout"),
        )
        conn.execute(
            "UPDATE ops_jobs SET last_status=?,last_run_ms=?,last_success_ms=50000,"
            "last_error_ms=60000,last_error='original-error',credits_total=7 WHERE name=?",
            (status, started, name),
        )
        return ops._Run(name, int(cursor.lastrowid), started, 30.0, DummyLock(), threading.Event())

    held = run("held", 100_000, ops.STATUS_RUNNING)
    orphan = run("orphan", 60_000, ops.STATUS_TIMEOUT)
    conn.execute("UPDATE ops_jobs SET consecutive_failures=2,failures=2,runs=2 WHERE name='orphan'")
    sched.running["held"], sched.orphans["orphan"] = held, orphan
    conn.execute("INSERT INTO ops_quota VALUES('held','synthetic-period',7,11,100000)")
    context = ops.JobContext("held", conn, original.jobs["held"].params, original,
                             100_000, 130_000, clock=lambda: clock[0])

    def pause():
        candidate = copy.deepcopy(original.model_dump())
        for item in candidate["jobs"].values():
            item["enabled"] = False
        path.write_text(yaml.safe_dump(candidate, sort_keys=False), encoding="utf-8")
        clock[0] += 2
        sched._maybe_reload(sched._now())
        return candidate

    def complete(run, result, credits=0):
        run.outcome = ops.RunOutcome(run.name, ops.STATUS_OK, run.started_ms, sched._now(),
                                     result=result, credits=credits)
        run.done.set()

    yield SimpleNamespace(conn=conn, clock=clock, jobs=jobs, original=original, original_raw=original_raw,
                          path=path, scheduler=sched, held=held, orphan=orphan, context=context,
                          pause=pause, complete=complete, make=make, emitted=emitted)
    conn.close()


def test_existing_reload_changes_only_enabled_fields_and_preserves_history(pause_env):
    env = pause_env
    before, before_quota = history(env.conn), quotas(env.conn)
    candidate = env.pause()
    assert env.scheduler.config.model_dump() == candidate
    restored = copy.deepcopy(candidate)
    for name, item in restored["jobs"].items():
        item["enabled"] = env.original.jobs[name].enabled
    assert restored == env.original.model_dump()
    assert all(not row[0] for row in env.conn.execute("SELECT enabled FROM ops_jobs"))
    assert history(env.conn) == before and quotas(env.conn) == before_quota


def test_reload_keeps_active_and_orphan_objects_locks_and_original_context(pause_env):
    env = pause_env
    env.pause()
    assert env.scheduler.tick(force=True) == []
    assert env.scheduler.running["held"] is env.held
    assert env.scheduler.orphans["orphan"] is env.orphan
    assert not env.held.done.is_set() and not env.orphan.done.is_set()
    assert env.held.lock.releases == env.orphan.lock.releases == 0
    assert env.held.lock.heartbeats > 0 and env.scheduler.service_lock.heartbeats > 0
    assert env.context.config is env.original and env.context.config.jobs["held"].enabled
    assert env.context.param("marker", None) == "preserve-original-parameter"
    assert not env.scheduler.config.jobs["held"].enabled
    assert not env.scheduler._stop


def test_disabled_jobs_are_not_eligible_even_with_force_and_have_positive_control(pause_env):
    env = pause_env
    assert "waiting" in env.scheduler.due_jobs(force=True)
    assert "already_off" not in env.scheduler.due_jobs(force=True)
    env.pause()
    assert env.scheduler.due_jobs(force=True) == []
    assert env.scheduler.tick(force=True) == []


def test_normal_completion_is_recorded_while_paused_without_refunding_quota(pause_env):
    env = pause_env
    env.pause()
    before_quota = quotas(env.conn)
    env.complete(env.held, {"retained": "normal-completion"}, credits=3)
    assert env.scheduler.tick(force=True) == []
    assert "held" not in env.scheduler.running and "orphan" in env.scheduler.orphans
    assert env.held.lock.releases == 1 and env.orphan.lock.releases == 0
    row = env.conn.execute("SELECT * FROM ops_jobs WHERE name='held'").fetchone()
    assert row["last_status"] == ops.STATUS_OK and row["enabled"] == 0
    assert json.loads(row["last_result_json"]) == {"retained": "normal-completion"}
    assert row["credits_total"] == 10 and row["runs"] == 1
    assert quotas(env.conn) == before_quota


def test_late_orphan_result_is_reaped_without_erasing_original_timeout_or_budget(pause_env):
    env = pause_env
    env.pause()
    before_quota = quotas(env.conn)
    env.complete(env.orphan, {"retained": "late-completion"}, credits=3)
    assert env.scheduler.tick(force=True) == []
    assert "orphan" not in env.scheduler.orphans and "held" in env.scheduler.running
    assert env.orphan.lock.releases == 1
    row = env.conn.execute("SELECT * FROM ops_runs WHERE id=?", (env.orphan.run_id,)).fetchone()
    assert row["status"] == ops.STATUS_TIMEOUT and row["error"] == "original-timeout"
    assert json.loads(row["result_json"]) == {"late": True, "retained": "late-completion"}
    job = env.conn.execute("SELECT * FROM ops_jobs WHERE name='orphan'").fetchone()
    assert (job["consecutive_failures"], job["failures"], job["runs"], job["credits_total"]) == (2, 2, 2, 7)
    assert quotas(env.conn) == before_quota


def test_timeout_while_paused_is_not_quiescence_or_lock_release(pause_env):
    env = pause_env
    env.pause()
    env.clock[0] = 131.0
    assert env.scheduler.tick(force=True) == []
    assert "held" not in env.scheduler.running
    assert env.scheduler.orphans["held"] is env.held and not env.held.done.is_set()
    assert env.held.lock.releases == 0
    row = env.conn.execute("SELECT * FROM ops_runs WHERE id=?", (env.held.run_id,)).fetchone()
    assert row["status"] == ops.STATUS_TIMEOUT
    assert env.emitted[-1].status == ops.STATUS_TIMEOUT


def test_fresh_scheduler_stays_paused_then_exact_config_restores_original_flags(pause_env):
    env = pause_env
    env.pause()
    env.complete(env.held, {"normal": True})
    env.complete(env.orphan, {"late": True})
    env.scheduler.tick(force=True)
    assert not env.scheduler.running and not env.scheduler.orphans
    before_quota = quotas(env.conn)
    fresh = env.make(ops.load_config(env.path))
    fresh.sync_jobs()
    assert fresh.tick(force=True) == []
    env.path.write_bytes(env.original_raw)
    env.clock[0] += 2
    fresh._maybe_reload(fresh._now())
    assert env.path.read_bytes() == env.original_raw and fresh.config == env.original
    flags = {row[0]: bool(row[1]) for row in env.conn.execute("SELECT name,enabled FROM ops_jobs")}
    assert flags == {name: item.enabled for name, item in env.original.jobs.items()}
    assert not flags["already_off"] and "already_off" not in fresh.due_jobs(force=True)
    assert quotas(env.conn) == before_quota


def test_invalid_reload_keeps_paused_last_good_configuration_and_history(pause_env):
    env = pause_env
    env.pause()
    paused, before, before_quota = env.scheduler.config, history(env.conn), quotas(env.conn)
    env.path.write_text("jobs: [broken", encoding="utf-8")
    env.clock[0] += 2
    env.scheduler._maybe_reload(env.scheduler._now())
    assert env.scheduler.config is paused
    assert env.scheduler.due_jobs(force=True) == []
    assert history(env.conn) == before and quotas(env.conn) == before_quota
