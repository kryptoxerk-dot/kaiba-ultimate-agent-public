"""Indexed reverse lookup and bounded, explicit read failure handling only."""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kaiba.hunters import social_follow as sf
from kaiba.ingest import follow_dispatch

NOW = 1_800_000_000_000


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("transport forbidden"))


@pytest.fixture
def store(tmp_path):
    result = sf.Store(tmp_path / "social.db")
    result.query("INSERT INTO projects(id,handle,website,profile_json,first_ms,status,checked_ms) "
                 "VALUES ('1','synthetic','','{}',1,'verified',?)", (NOW,))
    result.query("INSERT INTO edges(source,target_id,active) VALUES ('watched','1',1)")
    return result


def tracked_connections(monkeypatch, store, faults, *, target="publish_watch"):
    """Real SQLite connections and close calls; faults target statement or commit."""
    connect = sqlite3.connect
    connections, events, clock = [], [], [10.0]
    original_time = sf.time
    monkeypatch.setattr(sf, "time", SimpleNamespace(monotonic=lambda: clock[0], time=original_time.time))
    faults = iter(faults)

    class Tracked(sqlite3.Connection):
        closed = False
        progress = None
        fail_commit = False

        def set_progress_handler(self, callback, steps):
            assert steps == 1000
            self.progress = callback
            return super().set_progress_handler(callback, steps)

        def execute(self, sql, args=()):
            events.append(("execute", sql))
            matches = sql == sf.RETRY_READS[target] if target in sf.RETRY_READS else sql.startswith(target)
            fault = next(faults, None) if matches else None
            if fault == "commit_busy":
                self.fail_commit = True
                return super().execute(sql, args)
            if fault in ("deadline", "late", "partial"):
                clock[0] += 3
            if fault == "late":
                # Complete small SELECT with fewer than 1000 VM operations:
                # explicit completion deadline must still reject its late rows.
                return super().execute(sql, args)
            if fault == "partial":
                cursor = super().execute(sql, args)
                assert cursor.fetchone() is not None
                owner = self
                class InterruptedFetch:
                    def fetchall(self):
                        assert owner.progress() == 1
                        error = sqlite3.OperationalError("interrupted after a row")
                        error.sqlite_errorcode = sqlite3.SQLITE_INTERRUPT
                        raise error
                return InterruptedFetch()
            if fault:
                if fault == "deadline":
                    assert self.progress() == 1
                codes = {"deadline": sqlite3.SQLITE_INTERRUPT, "busy": sqlite3.SQLITE_BUSY,
                         "locked": sqlite3.SQLITE_LOCKED, "external_interrupt": sqlite3.SQLITE_INTERRUPT,
                         "io": sqlite3.SQLITE_IOERR, "corrupt": sqlite3.SQLITE_CORRUPT}
                error = sqlite3.OperationalError("synthetic " + fault)
                error.sqlite_errorcode = codes[fault]
                raise error
            return super().execute(sql, args)

        def commit(self):
            super().commit()
            if self.fail_commit:
                error = sqlite3.OperationalError("synthetic commit outcome unknown")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error

        def close(self):
            super().close()
            self.closed = True
            events.append(("close", None))

    def opening(path, *args, **kwargs):
        assert all(c.closed for c in connections), "retry opened while earlier connection was alive"
        assert kwargs["timeout"] == 2
        if kwargs.get("uri"):
            assert path == store.path.resolve().as_uri() + "?mode=ro"
        events.append(("open", str(path)))
        conn = connect(path, *args, factory=Tracked, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(sf.sqlite3, "connect", opening)
    return connections, events


def test_index_preserves_results_and_removes_correlated_scan_with_drop_control(tmp_path):
    store = sf.Store(tmp_path / "cost.db")
    conn = sqlite3.connect(store.path)
    try:
        rows = [(str(i), i, NOW) for i in range(2500)]
        conn.executemany("INSERT INTO projects(id,handle,website,profile_json,first_ms,status,checked_ms) "
                         "VALUES (?,'synthetic','','{}',?,'verified',?)", rows)
        conn.executemany("INSERT INTO edges(source,target_id,active) VALUES ('watched',?,?)",
                         [(str(i), int(i % 11 != 0)) for i in range(2490)])
        conn.commit()

        def measure(sql, args):
            steps = [0]
            def progress():
                steps[0] += 1000
                return 0
            conn.set_progress_handler(progress, 1000)
            try:
                result = conn.execute(sql, args).fetchall()
            finally:
                conn.set_progress_handler(None, 0)
            plan = [r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, args)]
            return result, steps[0], plan

        for name, args in (("verify_project", (NOW + 1, NOW + 1)), ("publish_watch", (0, 20))):
            sql = sf.RETRY_READS[name]
            indexed = measure(sql, args)
            assert any("idx_edges_target_active" in line for line in indexed[2])
            conn.execute("DROP INDEX idx_edges_target_active")
            # Disable statement-cache reuse after the schema change for plan evidence.
            unindexed = measure(sql + " ", args)
            assert any("SCAN edges" in line for line in unindexed[2])
            assert indexed[0] == unindexed[0]
            assert indexed[1] * 100 < unindexed[1]
            conn.execute("CREATE INDEX idx_edges_target_active ON edges(target_id,active)")
        assert len(conn.execute("SELECT name FROM sqlite_master WHERE name='idx_edges_target_active'").fetchall()) == 1
    finally:
        conn.close()
    sf.Store(store.path)  # Existing schema/index is idempotent.


@pytest.mark.parametrize("fault", ["deadline", "busy", "locked", "late", "partial"])
def test_only_one_fresh_read_retry_after_closing_failed_connection(store, monkeypatch, fault):
    connections, events = tracked_connections(monkeypatch, store, [fault, None])
    rows = store.query(sf.RETRY_READS["publish_watch"], (0, 20), read_retry="publish_watch")
    assert [r["id"] for r in rows] == ["1"]
    assert len(connections) == 2 and all(c.closed for c in connections)
    assert [kind for kind, _ in events] == ["open", "execute", "close"] * 2


@pytest.mark.parametrize("fault,reason", [("deadline", "read_deadline"), ("busy", "read_busy"),
                                          ("locked", "read_locked"), ("late", "read_deadline")])
def test_two_failures_defer_never_empty_result(store, monkeypatch, fault, reason):
    connections, _ = tracked_connections(monkeypatch, store, [fault, fault, None])
    with pytest.raises(sf.StoreReadDeferred) as captured:
        store.query(sf.RETRY_READS["publish_watch"], (0, 20), read_retry="publish_watch")
    assert captured.value.phase == "publish_watch" and captured.value.reason == reason
    assert captured.value.attempts == 2
    assert len(connections) == 2 and all(c.closed for c in connections)


@pytest.mark.parametrize("fault", ["external_interrupt", "io", "corrupt"])
def test_unknown_or_nontransient_sqlite_errors_propagate_without_retry(store, monkeypatch, fault):
    connections, _ = tracked_connections(monkeypatch, store, [fault, None])
    with pytest.raises(sqlite3.OperationalError, match="synthetic " + fault):
        store.query(sf.RETRY_READS["publish_watch"], (0, 20), read_retry="publish_watch")
    assert len(connections) == 1 and connections[0].closed


def test_write_and_unannotated_reads_are_never_retried(store, monkeypatch):
    connections, _ = tracked_connections(monkeypatch, store, ["busy", None], target="UPDATE")
    with pytest.raises(sqlite3.OperationalError, match="synthetic busy"):
        store.query("UPDATE projects SET status='rejected'")
    assert len(connections) == 1 and connections[0].closed
    with pytest.raises(ValueError, match="invalid_read_retry"):
        store.query("UPDATE projects SET status='rejected'", read_retry="publish_watch")
    with pytest.raises(ValueError, match="invalid_read_retry"):
        store.query("SELECT * FROM projects", read_retry="publish_watch")
    assert len(connections) == 1


def test_unannotated_select_is_not_silently_opted_into_retry(store, monkeypatch):
    connections, _ = tracked_connections(monkeypatch, store, ["busy", None])
    with pytest.raises(sqlite3.OperationalError, match="synthetic busy"):
        store.query(sf.RETRY_READS["publish_watch"], (0, 20))
    assert len(connections) == 1 and connections[0].closed


def test_uncertain_write_commit_is_not_replayed(store, monkeypatch):
    connections, events = tracked_connections(monkeypatch, store, ["commit_busy", None], target="UPDATE")
    with pytest.raises(sqlite3.OperationalError, match="commit outcome unknown"):
        store.query("UPDATE projects SET checked_ms=checked_ms+1")
    assert len(connections) == 1 and connections[0].closed
    assert len([sql for kind, sql in events if kind == "execute"]) == 1
    assert store.query("SELECT checked_ms FROM projects")[0]["checked_ms"] == NOW + 1


def test_verify_retry_completes_one_fetch_and_one_update_with_no_read_held_during_fetch(store, monkeypatch):
    store.query("UPDATE projects SET status='pending',website='https://synthetic.example',profile_json=?",
                (json.dumps({"parody": False, "unavailable": False}),))
    connections, events = tracked_connections(monkeypatch, store, ["deadline", None], target="verify_project")
    fetched = []

    def fetch(url):
        assert len(connections) == 2 and all(c.closed for c in connections)
        fetched.append(url)
        return url, '<a href="https://x.com/synthetic">verified backlink</a>'

    assert store.verify_one(now=NOW + 1, fetch=fetch)["status"] == "verified"
    assert fetched == ["https://synthetic.example"]
    assert len(connections) == 3 and all(c.closed for c in connections)
    updates = [sql for kind, sql in events if kind == "execute" and sql.startswith("UPDATE projects")]
    assert len(updates) == 1
    assert store.query("SELECT status,checked_ms FROM projects") == [{"status": "verified", "checked_ms": NOW + 1}]


def test_real_sqlite_busy_read_retries_after_exclusive_writer_releases(store, monkeypatch):
    connect = sqlite3.connect
    writer = connect(store.path)
    writer.execute("BEGIN EXCLUSIVE")
    writer.execute("UPDATE projects SET handle='uncommitted'")
    connections, events = [], []

    class Reader(sqlite3.Connection):
        closed = False
        def execute(self, sql, args=()):
            try:
                return super().execute(sql, args)
            except sqlite3.OperationalError as exc:
                events.append(("sqlite_error", exc.sqlite_errorcode))
                raise
        def close(self):
            super().close()
            self.closed = True
            events.append(("close", None))
            writer.rollback()  # Release only after failed reader has really closed.

    def opening(path, **kwargs):
        assert kwargs == {"uri": True, "timeout": 2}
        assert path.endswith("?mode=ro") and all(c.closed for c in connections)
        conn = connect(path, factory=Reader, **kwargs)
        # Test-only immediate contention observation; production still passes2s.
        conn.execute("PRAGMA busy_timeout=0")
        connections.append(conn)
        return conn

    monkeypatch.setattr(sf.sqlite3, "connect", opening)
    try:
        rows = store.query(sf.RETRY_READS["publish_watch"], (0, 20), read_retry="publish_watch")
        assert rows[0]["handle"] == "synthetic"
        assert len(connections) == 2 and all(c.closed for c in connections)
        assert events == [("sqlite_error", sqlite3.SQLITE_BUSY), ("close", None), ("close", None)]
    finally:
        writer.close()


@pytest.mark.parametrize("phase", ["verify_project", "publish_watch"])
def test_maintenance_defers_once_preserving_watch_without_replaying_paid_poll(
        store, tmp_path, monkeypatch, caplog, capsys, phase):
    watch = tmp_path / "watch.json"
    watch.write_bytes(b'{"projects":[{"id":"previous","handle":"prior"}]}')
    original = watch.read_bytes()
    cfg = {"enabled": True, "db_path": str(store.path), "watch_path": str(watch),
           "verification_ttl_s": 86400, "max_projects": 20, "max_post_age_s": 30}
    actual_poll, provider_calls = sf.poll_once, []

    def paid_poll(*_args):
        def provider(handle, **_kwargs):
            provider_calls.append(handle)
            return {"status": "success", "followings": [], "has_next_page": False}
        return actual_poll(cfg, store, fetch=provider, watched={"synthetic"})

    monkeypatch.setattr(sf, "poll_once", paid_poll)
    monkeypatch.setattr(sf, "load_config", lambda: cfg)
    monkeypatch.setattr(sf, "Store", lambda *_: store)
    monkeypatch.setattr(sf.provider, "sync_rules", lambda *_: pytest.fail("sync after deferred read"))
    monkeypatch.setattr(sys, "argv", ["social_follow", "maintain"])
    connections, events = tracked_connections(monkeypatch, store, ["deadline", "deadline"], target=phase)
    with caplog.at_level(logging.WARNING):
        assert sf.main() == 75
    result = json.loads(capsys.readouterr().out)
    assert result == {"status": "deferred", "command": "maintain", "phase": phase,
                      "reason": "read_deadline", "attempts": 2, "partial_results_accepted": False}
    assert provider_calls == ["synthetic"]
    assert watch.read_bytes() == original and not watch.with_suffix(".new").exists()
    assert all(c.closed for c in connections)
    assert not any(kind == "execute" and sql.startswith("DELETE") for kind, sql in events)
    assert len(caplog.records) == 1 and caplog.records[0].exc_info is None


def test_main_does_not_turn_unknown_failure_into_named_deferral(store, monkeypatch):
    monkeypatch.setattr(sf, "Store", lambda *_: store)
    monkeypatch.setattr(sf, "load_config", lambda: {"db_path": str(store.path)})
    monkeypatch.setattr(sys, "argv", ["social_follow", "verify"])
    tracked_connections(monkeypatch, store, ["corrupt"], target="verify_project")
    with pytest.raises(sqlite3.OperationalError, match="synthetic corrupt"):
        sf.main()


def test_periodic_publisher_defers_at_original_cadence_without_stopping_receiver(tmp_path, monkeypatch, caplog):
    clock, calls, receives = [20.0], [], []
    path = tmp_path / "social.sock"
    cfg = {"socket_path": str(path), "enabled": False}

    def publish(_cfg):
        calls.append(clock[0])
        if len(calls) == 1:
            raise sf.StoreReadDeferred("publish_watch", "read_busy", 2)
        return []

    class Socket:
        closed = False
        def bind(self, name):
            Path(name).write_bytes(b"")
        def settimeout(self, value):
            assert value == 1
        def recv(self, _size):
            receives.append(clock[0])
            if len(receives) == 1:
                clock[0] = 21.0
            elif len(receives) == 2:
                clock[0] = 30.1
            else:
                raise KeyboardInterrupt
            raise TimeoutError
        def close(self):
            self.closed = True

    sock = Socket()
    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(flock=lambda *_: None, LOCK_EX=1, LOCK_NB=2))
    monkeypatch.setattr(sf, "socket", SimpleNamespace(socket=lambda *_: sock, AF_UNIX=1, SOCK_DGRAM=2))
    monkeypatch.setattr(sf, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(follow_dispatch, "configuration", lambda: cfg)
    with caplog.at_level(logging.WARNING), pytest.raises(KeyboardInterrupt):
        sf.run(cfg, SimpleNamespace(publish_watch=publish))
    assert calls == [20.0, 30.1] and len(caplog.records) == 1
    assert sock.closed and not path.exists()
