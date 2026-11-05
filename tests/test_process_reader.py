"""Real read-only child queries, bounded IPC and non-VM blocking cancellation."""
from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from kaiba.learning import autolearn
from kaiba.learning import process_reader as pr
from kaiba.learning.autolearn_io import WAL_LIMIT, GuardedReader, SourceDeferred


@pytest.fixture(autouse=True)
def no_pending_children():
    assert pr.ProcessReader.shutdown_all() == []
    yield
    assert pr.ProcessReader.shutdown_all() == []
    assert pr._PENDING == {}


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "source.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript("CREATE TABLE sample(id INTEGER PRIMARY KEY,amount TEXT,payload BLOB);"
                           "CREATE TABLE decisions(ts_ms INTEGER,lane TEXT);"
                           "CREATE INDEX idx_decisions_lane ON decisions(lane,ts_ms);")
        conn.execute("INSERT INTO sample VALUES (1,?,?)", (str(2**80), b"\x00\xff\x01"))
        conn.execute("INSERT INTO sample VALUES (2,?,?)", ("-9", b""))
        conn.executemany("INSERT INTO decisions(ts_ms,lane) VALUES (?,?)", [(i, "sm-trenches") for i in range(200)])
    return path


def fixture_worker(tmp_path, body):
    script = tmp_path / ("worker-" + str(len(list(tmp_path.glob("worker-*")))) + ".py")
    script.write_text(body, encoding="utf-8")
    return [sys.executable, "-I", "-B", str(script)]


def actual_worker(tmp_path, setup=""):
    root = Path(pr.__file__).resolve().parents[2]
    return fixture_worker(tmp_path,
        "import sys,runpy,sqlite3,time,os,json\n"
        + "sys.path.insert(0," + repr(str(root)) + ")\n"
        + "ns=runpy.run_path(" + repr(str(Path(pr.__file__).resolve())) + ")\n"
        + "g=ns['_worker_main'].__globals__\n" + setup
        + "raise SystemExit(ns['_worker_main']())\n")


def test_real_rows_q_text_native_bytes_integer_and_parameters_roundtrip(database):
    reader = pr.ProcessReader(database)
    expected = [{"id": 1, "amount": str(2**80), "payload": b"\x00\xff\x01"},
                {"id": 2, "amount": "-9", "payload": b""}]
    assert reader.rows("SELECT * FROM sample ORDER BY id") == expected
    assert reader.q("SELECT amount,payload FROM sample WHERE id=?", (1,)) == [(str(2**80), b"\x00\xff\x01")]
    assert reader.q("SELECT ?,?,?,?,?", (None, True, 2**60, 1.25, b"\x00\xff")) == [(None, 1, 2**60, 1.25, b"\x00\xff")]
    assert reader.queries == 3 and all(r["worker_reaped"] for r in reader.receipts)
    assert all(r["status"] == "complete" for r in reader.receipts)
    assert reader._active is None and pr._PENDING == {}


def test_short_pipe_writes_loop_until_large_parameter_request_is_complete(database, monkeypatch):
    original = pr.subprocess.Popen
    writes = []
    class ShortWriter:
        def __init__(self, stream):
            self.stream = stream
        def write(self, data):
            writes.append(len(data))
            return self.stream.write(data[:4096])
        def close(self):
            return self.stream.close()
    def spawn(*args, **kwargs):
        process = original(*args, **kwargs)
        process.stdin = ShortWriter(process.stdin)
        return process
    monkeypatch.setattr(pr.subprocess, "Popen", spawn)
    reader = pr.ProcessReader(database)
    payload = b"\x00\xff" * 50_000
    assert reader.q("SELECT length(?),?", ("T" * 200_000, payload)) == [(200_000, payload)]
    assert len(writes) > 20 and writes[-1] <= 4096 and reader.receipts[-1]["worker_reaped"]


@pytest.mark.parametrize("sql,args", [
    ("PRAGMA table_info(sample)", ()), ("PRAGMA index_list(decisions)", ()),
    ("PRAGMA index_info(idx_decisions_lane)", ()),
    ("SELECT name,type FROM pragma_table_info(?) ORDER BY cid LIMIT 129", ("sample",)),
    ("WITH a AS (SELECT amount FROM sample) SELECT * FROM a", ()),
    ("EXPLAIN QUERY PLAN SELECT * FROM sample WHERE id=?", (1,)),
])
def test_actual_metadata_cte_and_explain_parity_with_frozen_reader(database, sql, args):
    assert pr.ProcessReader(database).q(sql, args) == GuardedReader(database).q(sql, args)


def test_readonly_connection_refuses_write_and_next_query_remains_valid(database):
    reader = pr.ProcessReader(database)
    with pytest.raises(SourceDeferred, match="source_query_unavailable"):
        reader.rows("UPDATE sample SET amount='0'")
    assert reader.q("SELECT amount FROM sample WHERE id=1") == [(str(2**80),)]
    assert reader.q("PRAGMA query_only") == [(1,)]


def test_missing_source_is_not_created_and_nonfinite_result_does_not_become_null(tmp_path, database):
    missing = tmp_path / "not-created.db"
    with pytest.raises(SourceDeferred, match="source_query_unavailable"):
        pr.ProcessReader(missing).q("SELECT 1")
    assert not missing.exists()
    with pytest.raises(SourceDeferred, match="unsupported_or_oversized_source_cell"):
        pr.ProcessReader(database).q("SELECT 1e999")
    assert pr.ProcessReader(database).q("SELECT NULL") == [(None,)]


@pytest.mark.parametrize("kwargs", [{"query_seconds": 0}, {"query_seconds": 31},
                                   {"query_seconds": float("nan")}, {"max_rows": True},
                                   {"max_rows": 0}, {"max_rows": 50_001}])
def test_original_reader_limits_remain_enforced(database, kwargs):
    with pytest.raises(ValueError):
        pr.ProcessReader(database, **kwargs)


def test_request_cap_refuses_before_spawning_or_decoding_source(database, monkeypatch):
    reader = pr.ProcessReader(database)
    calls = []
    monkeypatch.setattr(reader, "_worker_command", lambda: calls.append("spawn") or [])
    with pytest.raises(SourceDeferred, match="request_cap"):
        reader.q("SELECT ?", ("X" * (pr.MAX_REQUEST_BYTES + 1),))
    assert calls == [] and reader.queries == 0


def test_expired_guard_budget_never_spawns_a_query_child(database, monkeypatch):
    reader = pr.ProcessReader(database, query_seconds=.05)
    wall, spawns = [100.0], []
    monkeypatch.setattr(pr.time, "monotonic", lambda: wall[0])
    original = reader.guard
    def slow_guard():
        size = original()
        wall[0] += 3
        return size
    monkeypatch.setattr(reader, "guard", slow_guard)
    monkeypatch.setattr(reader, "_worker_command", lambda: spawns.append("command") or [])
    with pytest.raises(SourceDeferred, match="wall_timeout_before_spawn"):
        reader.q("SELECT 1")
    assert spawns == [] and reader.queries == 0


def test_expired_os_spawn_is_reaped_before_any_sql_is_sent(database, tmp_path, monkeypatch):
    reader = pr.ProcessReader(database, query_seconds=.05)
    wall, writes = [100.0], []
    original = pr.subprocess.Popen
    monkeypatch.setattr(pr.time, "monotonic", lambda: wall[0])
    class NoSend:
        def __init__(self, stream):
            self.stream = stream
        def write(self, data):
            writes.append(len(data))
            return self.stream.write(data)
        def close(self):
            self.stream.close()
    def delayed_spawn(*args, **kwargs):
        process = original(*args, **kwargs)
        process.stdin = NoSend(process.stdin)
        wall[0] += 3
        return process
    monkeypatch.setattr(pr.subprocess, "Popen", delayed_spawn)
    with pytest.raises(SourceDeferred, match="wall_timeout_before_send"):
        reader.q("SELECT 1")
    assert writes == [] and reader._active is None and reader.receipts[-1]["worker_reaped"]


def test_polling_primary_key_transform_is_preserved(database):
    sql = "SELECT rowid FROM decisions WHERE rowid>? AND lane=? ORDER BY rowid LIMIT 10"
    reader = pr.ProcessReader(database)
    assert reader.q(sql, (50, "sm-trenches")) == [(i,) for i in range(51, 61)]
    plan = " ".join(str(r) for r in reader.q("EXPLAIN QUERY PLAN " + sql, (50, "sm-trenches")))
    assert "INTEGER PRIMARY KEY" in plan and "TEMP B-TREE" not in plan


def test_row_cap_rejects_whole_sample_and_retry_is_independent(database):
    reader = pr.ProcessReader(database, max_rows=1)
    with pytest.raises(SourceDeferred, match="source_row_cap.*no partial"):
        reader.rows("SELECT * FROM sample")
    assert reader.rows("SELECT id FROM sample WHERE id=1") == [{"id": 1}]
    assert reader.queries == 2 and all(r["worker_reaped"] for r in reader.receipts)


def test_sqlite_value_limit_prevents_giant_cell_and_does_not_return_null(database):
    reader = pr.ProcessReader(database)
    with pytest.raises(SourceDeferred, match="source_query_unavailable"):
        reader.q("SELECT zeroblob(?)", (pr.MAX_CELL_BYTES + 1,))
    assert reader.q("SELECT length(zeroblob(16))") == [(16,)]


def test_producer_json_cap_returns_deferred_not_prefix_rows(database, tmp_path):
    reader = pr.ProcessReader(database)
    reader._worker_command = lambda: actual_worker(tmp_path, "g['MAX_RESPONSE_BYTES']=4096\n")
    with pytest.raises(SourceDeferred, match="response_byte_cap.*no partial"):
        reader.rows("SELECT id,hex(zeroblob(1500)) AS value FROM sample ORDER BY id")
    assert reader._active is None


@pytest.mark.parametrize("max_rows,expected", [(2, [(1,), (2,)]), (1, None)])
def test_child_closes_connection_before_emitting_success_or_error(database, tmp_path, max_rows, expected):
    marker = tmp_path / "connection-closed"
    setup = ("original=sqlite3.connect\n"
        "class Connection:\n"
        " def __init__(self,c):self.c=c\n"
        " def __getattr__(self,k):return getattr(self.c,k)\n"
        " def close(self):\n  self.c.close()\n"
        f"  open({str(marker)!r},'w').write('closed')\n"
        "sqlite3.connect=lambda *a,**k:Connection(original(*a,**k))\n"
        "output=sys.stdout\n"
        "class Buffer:\n"
        " def write(self,value):\n"
        f"  assert __import__('pathlib').Path({str(marker)!r}).exists()\n"
        "  return output.buffer.write(value)\n"
        " def flush(self):return output.buffer.flush()\n"
        "class Output:\n buffer=Buffer()\n def flush(self):return output.flush()\n"
        "sys.stdout=Output()\n")
    reader = pr.ProcessReader(database, max_rows=max_rows)
    reader._worker_command = lambda: actual_worker(tmp_path, setup)
    if expected is None:
        with pytest.raises(SourceDeferred, match="source_row_cap"):
            reader.q("SELECT id FROM sample ORDER BY id")
    else:
        assert reader.q("SELECT id FROM sample ORDER BY id") == expected
    assert marker.exists() and reader.receipts[-1]["worker_reaped"]


@pytest.mark.parametrize("kind", ["crash", "wrong_id", "oversize", "malformed", "deep_json", "invalid_bytes"])
def test_failed_or_invalid_child_never_yields_partial_rows_and_is_reaped(database, tmp_path, monkeypatch, kind):
    reader = pr.ProcessReader(database)
    if kind == "crash":
        body, message = "raise SystemExit(7)\n", "worker_failed"
    elif kind == "oversize":
        monkeypatch.setattr(pr, "MAX_RESPONSE_BYTES", 4096)
        body, message = "import sys\nsys.stdout.buffer.write(b'x'*1000000)\nsys.stdout.flush()\n", "response_byte_cap"
    elif kind == "malformed":
        body, message = "print('not JSON')\n", "unavailable"
    elif kind == "deep_json":
        body, message = "print('['*2000+']'*2000)\n", "unavailable"
    else:
        row = "{'bytes_base64':'not valid base64'}" if kind == "invalid_bytes" else "1"
        identity = "r['request_id']" if kind == "invalid_bytes" else "'0'*32"
        body = ("import sys,json\nr=json.loads(sys.stdin.buffer.read())\n"
                + "print(json.dumps({'version':r['version'],'request_id':" + identity
                + ",'wal_bytes':0,'status':'ok','columns':['x'],'rows':[[" + row + "]]}))\n")
        message = "unavailable"
    reader._worker_command = lambda: fixture_worker(tmp_path, body)
    with pytest.raises(SourceDeferred, match=message):
        reader.rows("SELECT * FROM sample")
    assert reader._active is None and reader.receipts[-1]["worker_reaped"]
    reader._worker_command = pr.ProcessReader._worker_command.__get__(reader)
    assert reader.q("SELECT 1") == [(1,)]


def test_parent_and_fresh_child_wal_guards_preserve_sixty_second_deferral(database, tmp_path, monkeypatch):
    assert pr.WAL_LIMIT == WAL_LIMIT
    reader = pr.ProcessReader(database)
    monkeypatch.setattr(reader, "_wal_bytes", lambda: WAL_LIMIT + 1)
    with pytest.raises(SourceDeferred, match="wal_over_limit.*at least 60s"):
        reader.q("SELECT 1")
    assert reader.queries == 0 and reader.wal_waits == 1
    monkeypatch.setattr(reader, "_wal_bytes", lambda: 0)
    command = actual_worker(tmp_path, "g['_wal_size']=lambda path:1500000001\n")
    reader._worker_command = lambda: command
    with pytest.raises(SourceDeferred, match="wal_over_limit.*at least 60s"):
        reader.q("SELECT 1")
    assert reader.queries == 1 and reader.wal_waits == 2 and reader.receipts[-1]["worker_reaped"]
    reader._worker_command = pr.ProcessReader._worker_command.__get__(reader)
    assert reader.q("SELECT 1") == [(1,)]


def test_non_vm_blocking_udf_is_killed_reaped_and_releases_real_wal_lock(database, tmp_path):
    marker = tmp_path / "inside-sqlite"
    setup = ("original=sqlite3.connect\n"
        "def connect(*a,**k):\n"
        " c=original(*a,**k)\n"
        " def stall(seconds):\n"
        f"  open({str(marker)!r},'w').write(str(os.getpid()))\n"
        "  time.sleep(seconds)\n  return 1\n"
        " c.create_function('stall',1,stall)\n return c\n"
        "sqlite3.connect=connect\n")
    with closing(sqlite3.connect(database, isolation_level=None)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA busy_timeout=0")
        writer.execute("UPDATE sample SET amount=amount WHERE id=1")
        reader = pr.ProcessReader(database, query_seconds=.05)
        reader._worker_command = lambda: actual_worker(tmp_path, setup)
        failures = []
        def query():
            try:
                reader.rows("SELECT amount,stall(10) FROM sample WHERE id=1")
            except SourceDeferred as exc:
                failures.append(str(exc))
        began = time.monotonic()
        thread = threading.Thread(target=query)
        thread.start()
        while not marker.exists() and time.monotonic() - began < 1.9:
            time.sleep(.01)
        assert marker.exists(), "positive control must enter non-VM SQLite UDF"
        writer.execute("UPDATE sample SET amount='after-reader' WHERE id=2")
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 1
        thread.join(timeout=5)
        assert not thread.is_alive() and failures and "wall_timeout" in failures[0]
        assert time.monotonic() - began < 5 and reader.receipts[-1]["worker_reaped"]
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
        reader._worker_command = pr.ProcessReader._worker_command.__get__(reader)
        assert reader.q("SELECT amount FROM sample WHERE id=2") == [("after-reader",)]


@pytest.mark.parametrize("error", [KeyboardInterrupt("controlled"), SystemExit("controlled")])
def test_baseexception_cancellation_reaps_child_and_propagates_same_object(database, tmp_path, monkeypatch, error):
    marker = tmp_path / "started"
    command = fixture_worker(tmp_path, f"import time\nopen({str(marker)!r},'w').write('started')\ntime.sleep(10)\n")
    reader = pr.ProcessReader(database)
    reader._worker_command = lambda: command
    sleep = time.sleep
    injected = []
    def interrupt(seconds):
        if marker.exists() and not injected:
            injected.append(error)
            raise error
        sleep(seconds)
    monkeypatch.setattr(pr.time, "sleep", interrupt)
    with pytest.raises(type(error)) as caught:
        reader.q("SELECT 1")
    assert caught.value is error and reader._active is None
    assert injected == [error] and reader.receipts[-1]["worker_reaped"] and pr._PENDING == {}


@pytest.mark.parametrize("original,secondary", [(KeyboardInterrupt("first"), SystemExit("second")),
                                               (SystemExit("first"), KeyboardInterrupt("second"))])
def test_second_interrupt_preserves_original_and_tracks_pending_until_reaped(
    database, tmp_path, monkeypatch, original, secondary,
):
    marker = tmp_path / "started"
    command = fixture_worker(tmp_path, f"import time\nopen({str(marker)!r},'w').write('started')\ntime.sleep(10)\n")
    reader = pr.ProcessReader(database)
    reader._worker_command = lambda: command
    native_spawn, native_sleep = pr.subprocess.Popen, time.sleep
    processes, injected = [], []

    def spawn(*args, **kwargs):
        process = native_spawn(*args, **kwargs)
        native_wait, native_poll = process.wait, process.poll
        processes.append((process, native_wait, native_poll))
        # Model another signal while POSIX wait is polling, before exit is proven.
        def interrupted_wait(*args, **kwargs):
            raise secondary
        process.wait = interrupted_wait
        process.poll = lambda: None
        return process

    def interrupt_once(seconds):
        if marker.exists() and not injected:
            injected.append(original)
            raise original
        native_sleep(seconds)

    monkeypatch.setattr(pr.subprocess, "Popen", spawn)
    monkeypatch.setattr(pr.time, "sleep", interrupt_once)
    try:
        with pytest.raises(type(original)) as caught:
            reader.q("SELECT 1")
        assert caught.value is original and injected == [original]
        assert reader._active is processes[0][0] and pr._PENDING[reader._key] is reader
        assert reader.receipts[-1]["worker_reaped"] is False and reader.receipts[-1]["status"] == "deferred"
        other = pr.ProcessReader(database)
        with pytest.raises(SourceDeferred, match="cleanup_pending.*at least 60s"):
            other.q("SELECT 1")
        assert len(processes) == 1 and other.queries == 0
    finally:
        for process, native_wait, native_poll in processes:
            process.wait, process.poll = native_wait, native_poll
        assert pr.ProcessReader.shutdown_all() == []
    assert reader._active is None and pr._PENDING == {}
    monkeypatch.setattr(pr.subprocess, "Popen", native_spawn)
    assert pr.ProcessReader(database).q("SELECT 1") == [(1,)]


@pytest.mark.parametrize("error", [KeyboardInterrupt("cleanup first interrupt"), SystemExit("cleanup first interrupt")])
def test_cleanup_does_not_swallow_a_new_interrupt_without_original_cancellation(database, monkeypatch, error):
    reader = pr.ProcessReader(database)
    def interrupted():
        raise error
    monkeypatch.setattr(reader, "_reap", interrupted)
    with pytest.raises(type(error)) as caught:
        reader._cleanup()
    assert caught.value is error


def cgroup_fixture(tmp_path, *, argv=None):
    proc, cgroup = tmp_path / "proc", tmp_path / "cgroup"
    (proc / "self").mkdir(parents=True)
    (proc / "self/cgroup").write_text("0::/user.slice/research.service\n")
    group = cgroup / "user.slice/research.service"
    group.mkdir(parents=True)
    (group / "cgroup.procs").write_text("1234\n4567\n")
    (proc / "4567").mkdir()
    worker = str(Path(pr.__file__).resolve())
    (proc / "4567/cmdline").write_bytes(b"\0".join(v.encode() for v in
        (argv or ["python", "-I", "-B", worker, "--worker"])) + b"\0")
    return proc, cgroup, worker


def test_prior_generation_exact_cgroup_worker_refuses_new_source_read(tmp_path):
    proc, cgroup, worker = cgroup_fixture(tmp_path)
    with pytest.raises(SourceDeferred, match="prior_cgroup_worker_present.*at least 60s"):
        pr._check_cgroup(proc, cgroup, worker_path=worker, current_pid=1234)
    (proc / "4567/cmdline").write_bytes(b"python\0/tmp/different/process_reader.py\0--worker\0")
    pr._check_cgroup(proc, cgroup, worker_path=worker, current_pid=1234)
    (proc / "4567/cmdline").unlink()
    pr._check_cgroup(proc, cgroup, worker_path=worker, current_pid=1234)  # Real exit race.


@pytest.mark.parametrize("failure", ["hierarchy", "escape", "missing_group", "pid_cap", "argv_cap", "permission"])
def test_unknown_or_oversized_linux_cgroup_state_is_not_permission_to_spawn(tmp_path, monkeypatch, failure):
    proc, cgroup, worker = cgroup_fixture(tmp_path, argv=["timeout", "900"])
    group = cgroup / "user.slice/research.service/cgroup.procs"
    if failure == "hierarchy":
        (proc / "self/cgroup").write_text("not a unified hierarchy")
    elif failure == "escape":
        (proc / "self/cgroup").write_text("0::/../../outside\n")
    elif failure == "missing_group":
        group.unlink()
    elif failure == "pid_cap":
        group.write_text("\n".join(str(i + 1) for i in range(257)))
    elif failure == "argv_cap":
        (proc / "4567/cmdline").write_bytes(b"x" * 8193)
    else:
        original = pr._read_metadata
        def denied(path, maximum):
            if path.name == "cmdline":
                raise PermissionError("disposable fixture")
            return original(path, maximum)
        monkeypatch.setattr(pr, "_read_metadata", denied)
    with pytest.raises(SourceDeferred, match="cgroup_state_unknown.*at least 60s"):
        pr._check_cgroup(proc, cgroup, worker_path=worker, current_pid=1234)


def test_linux_cgroup_preflight_runs_before_any_query_spawn(database, monkeypatch):
    reader, calls = pr.ProcessReader(database), []
    monkeypatch.setattr(pr.sys, "platform", "linux")
    def prior_worker():
        calls.append("cgroup")
        raise SourceDeferred("source_prior_cgroup_worker_present; retry after at least 60s")
    monkeypatch.setattr(pr, "_check_cgroup", prior_worker)
    monkeypatch.setattr(reader, "_worker_command", lambda: calls.append("spawn") or [])
    with pytest.raises(SourceDeferred, match="prior_cgroup_worker_present"):
        reader.q("SELECT 1")
    assert calls == ["cgroup"] and reader.queries == 0


def test_unreapable_child_blocks_other_instances_until_actual_exit(database):
    class Pending:
        pid, stdin, stdout = 999999, None, None
        done = False
        def poll(self):
            return 0 if self.done else None
        def kill(self):
            pass
        def wait(self, timeout):
            if not self.done:
                raise subprocess.TimeoutExpired("injected-kernel-wait", timeout)
            return 0
    old, pending = pr.ProcessReader(database), Pending()
    old._active = pending
    pr._PENDING[old._key] = old
    try:
        assert pr.ProcessReader.shutdown_all() == [pending.pid]
        other = pr.ProcessReader(database)
        with pytest.raises(SourceDeferred, match="cleanup_pending.*at least 60s"):
            other.q("SELECT 1")
        assert other.queries == 0 and len(pr._PENDING) == 1
    finally:
        pending.done = True
    assert pr.ProcessReader(database).q("SELECT 1") == [(1,)]


def test_cli_factory_and_bounded_shutdown_preserve_library_default(monkeypatch, capsys, database, tmp_path):
    original = autolearn.AutoLearner
    calls = []
    class Learner:
        def __init__(self, *args, **kwargs):
            calls.append(kwargs["reader_factory"])
        def once(self):
            return {"status": "complete"}
    monkeypatch.setattr(autolearn, "AutoLearner", Learner)
    monkeypatch.setattr(pr.ProcessReader, "shutdown_all", lambda: calls.append("shutdown") or [])
    assert autolearn.main(["--once", "--db", str(database), "--state-dir", str(tmp_path / "state")]) == 0
    assert calls == [pr.ProcessReader, "shutdown"]
    assert original.__init__.__kwdefaults__["reader_factory"] is autolearn.PollingReader
    assert json.loads(capsys.readouterr().out)["status"] == "complete"
    source = Path(autolearn.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert "process_reader" not in " ".join(ast.get_source_segment(source, n) for n in tree.body
        if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name != "main")


def test_cli_shutdown_runs_on_interruption_and_preserves_exception(monkeypatch, database, tmp_path):
    calls, error = [], KeyboardInterrupt("main interrupted")
    class Learner:
        def __init__(self, *args, **kwargs):
            pass
        def once(self):
            raise error
    monkeypatch.setattr(autolearn, "AutoLearner", Learner)
    monkeypatch.setattr(pr.ProcessReader, "shutdown_all", lambda: calls.append("shutdown") or [])
    with pytest.raises(KeyboardInterrupt) as caught:
        autolearn.main(["--once", "--db", str(database), "--state-dir", str(tmp_path / "state")])
    assert caught.value is error and calls == ["shutdown"]


def test_actual_economic_capture_and_cached_work_keep_native_text_and_unknowns(tmp_db, tmp_path):
    from kaiba.learning.economic_capture import capture_positions
    from kaiba.learning.economic_work import cached_work, capture_work
    now = 1_800_000_000_000
    cost = str(2**80)
    tmp_db.execute("INSERT INTO positions(position_id,chain,token,lane,mode,opened_ms,closed_ms,qty,qty_total,"
                   "cost_native,proceeds_native,realized_native) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("p1", "sol", "token", "sm-trenches", "live", now - 1000, now - 100, "0", "100", cost,
                    str(2**80 - 1), "-1"))
    path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
    original = capture_positions(GuardedReader(path), ["p1"], now_ms=now)
    reader = pr.ProcessReader(path)
    fresh = capture_positions(reader, ["p1"], now_ms=now)
    assert fresh["packets"] == original["packets"]
    assert fresh["packets"][0]["position"]["cost_native"] == cost
    assert fresh["packets"][0]["coverage"] == original["packets"][0]["coverage"]
    work_path = tmp_path / "work.json"
    observed = capture_work(reader, work_path, now_ms=now)
    before = reader.queries
    cached = cached_work(work_path, now_ms=now + 1000, reason="interval_not_due")
    assert cached["economic"] == observed["economic"] and reader.queries == before
    assert all(r["economic_pnl_native"] is None for r in cached["economic"]["outcomes"])
    assert all(r["worker_reaped"] for r in reader.receipts)
