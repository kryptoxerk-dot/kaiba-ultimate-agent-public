"""The database guard stops AD-HOC readers that pin the WAL (2026-10-04).

An analysis script run over ssh held one read transaction for 87 minutes, 355,415 frames
behind the log head; the checkpoint was refused and the WAL reached 4.4 GB before the owner
was paged. Warning was not enough. The guard now sends SIGTERM, then SIGKILL, to a reader
that is far behind -- but only one OUTSIDE a ``kaiba-*.service`` unit, so trading,
protection, ingest, the sniper, the backup and the dashboard are never touched.
"""

from __future__ import annotations

import signal
import sqlite3
from collections import namedtuple
from pathlib import Path

from kaiba.ops import db_guard

GIB = db_guard.GIB
Usage = namedtuple("Usage", "total used free")
SERVICE = "/user.slice/user-1000.slice/user@1000.service/app.slice/kaiba-engine.service"
SNIPE = "/user.slice/user-1000.slice/user@1000.service/app.slice/kaiba-snipe.service"
BACKUP = "/user.slice/user-1000.slice/user@1000.service/app.slice/kaiba-core-backup.service"
DASHBOARD = "/system.slice/kaiba-dashboard.service"
SSH = "/user.slice/user-1000.slice/session-156025.scope"
HERMES = "/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-gateway-kaiba-operator.service"


def _roomy(_path):
    return Usage(100 * GIB, 10 * GIB, 90 * GIB)


def test_kaiba_units_are_protected_and_ad_hoc_readers_are_not():
    for cg in (SERVICE, SNIPE, BACKUP, DASHBOARD):
        assert db_guard.is_protected(cg), cg
    for cg in (SSH, HERMES):
        assert not db_guard.is_protected(cg), cg


def test_an_unreadable_cgroup_is_protected():
    assert db_guard.is_protected(None)
    assert db_guard.is_protected("")


def test_selection_takes_only_far_behind_ad_hoc_readers():
    readers = [
        {"pid": 100, "cmd": "python lookalike_sol.py", "frames_behind": 355_415},
        {"pid": 101, "cmd": "python -m kaiba ops run", "frames_behind": 400_000},
        {"pid": 102, "cmd": "python early_tape_followup.py", "frames_behind": 130},
        {"pid": 103, "cmd": "this guard", "frames_behind": 900_000},
        {"pid": 1, "cmd": "init", "frames_behind": 900_000},
    ]
    cgroups = {100: SSH, 101: SERVICE, 102: HERMES, 103: SSH, 1: SSH}
    chosen = db_guard.select_evictions(readers, frames_behind=100_000, own_pid=103,
                                       cgroup_of=cgroups.get)
    assert [r["pid"] for r in chosen] == [100]


def test_evict_terms_then_kills_a_reader_that_ignores_term():
    calls: list[tuple[int, int]] = []
    slept: list[float] = []
    done = db_guard.evict(
        [{"pid": 100, "cmd": "python stuck.py", "frames_behind": 355_415}],
        grace_s=10.0, kill=lambda p, s: calls.append((p, s)), alive=lambda p: True,
        cmdline=lambda p: "python stuck.py", sleep=slept.append,
    )
    assert calls == [(100, signal.SIGTERM), (100, db_guard._SIGKILL)]
    assert slept == [10.0]
    assert done[0]["signals"] == ["TERM", "KILL"]


def test_evict_does_not_kill_a_reader_that_exited_on_term():
    calls: list[tuple[int, int]] = []
    db_guard.evict(
        [{"pid": 100, "cmd": "python polite.py", "frames_behind": 200_000}],
        kill=lambda p, s: calls.append((p, s)), alive=lambda p: False,
        cmdline=lambda p: "?", sleep=lambda s: None,
    )
    assert calls == [(100, signal.SIGTERM)]


def test_evict_never_kills_a_reused_pid():
    """Same pid, different command line after the grace period: not our process any more."""
    calls: list[tuple[int, int]] = []
    db_guard.evict(
        [{"pid": 100, "cmd": "python stuck.py", "frames_behind": 200_000}],
        kill=lambda p, s: calls.append((p, s)), alive=lambda p: True,
        cmdline=lambda p: "/usr/sbin/sshd", sleep=lambda s: None,
    )
    assert calls == [(100, signal.SIGTERM)]


def test_a_vanished_target_is_recorded_and_not_waited_for():
    def gone(p, s):
        raise ProcessLookupError

    slept: list[float] = []
    done = db_guard.evict([{"pid": 100, "cmd": "x", "frames_behind": 200_000}],
                          kill=gone, alive=lambda p: False, cmdline=lambda p: "x", sleep=slept.append)
    assert done[0]["signals"] == ["gone"] and slept == []


def _pinned(path: Path) -> tuple[sqlite3.Connection, sqlite3.Connection]:
    writer = sqlite3.connect(str(path), isolation_level=None)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE t (x BLOB)")
    for _ in range(200):
        writer.execute("INSERT INTO t VALUES (randomblob(2000))")
    reader = sqlite3.connect(str(path), isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM t").fetchone()
    for _ in range(50):
        writer.execute("INSERT INTO t VALUES (randomblob(2000))")
    return writer, reader


def _run(tmp_path, path, *, cgroup, evict_wal_bytes=1, sent=None, kills=None):
    sent = [] if sent is None else sent
    kills = [] if kills is None else kills
    fake_reader = [{"pid": 4242, "slots": [1], "cmd": "python lookalike_sol.py", "frames_behind": 355_415}]
    report = db_guard.run(
        path, wal_truncate_bytes=1, wal_alert_bytes=1, busy_ms=50,
        state_path=tmp_path / "s.json", disk_usage=_roomy,
        notify=lambda t: sent.append(t) or True,
        evict_wal_bytes=evict_wal_bytes, evict_frames_behind=100_000,
        cgroup_of=lambda pid: cgroup, kill=lambda p, s: kills.append((p, s)),
        alive=lambda p: False, sleep=lambda s: None,
        readers_of=lambda db, hdr: list(fake_reader),
    )
    return report, sent, kills


def test_run_stops_an_ad_hoc_pinner_and_says_so(tmp_path):
    path = tmp_path / "k.db"
    writer, reader = _pinned(path)
    report, sent, kills = _run(tmp_path, path, cgroup=SSH)
    assert kills == [(4242, signal.SIGTERM)]
    assert report.evicted and report.evicted[0]["pid"] == 4242
    assert sent and "STOPPED ad-hoc reader pid 4242" in sent[0]
    reader.execute("COMMIT")
    reader.close()
    writer.close()


def test_run_never_stops_a_kaiba_service(tmp_path):
    path = tmp_path / "k.db"
    writer, reader = _pinned(path)
    report, sent, kills = _run(tmp_path, path, cgroup=SERVICE)
    assert kills == [] and report.evicted == []
    assert sent and "STOPPED" not in sent[0]  # still paged as a pinned WAL
    reader.execute("COMMIT")
    reader.close()
    writer.close()


def test_run_does_not_evict_below_the_wal_threshold(tmp_path):
    path = tmp_path / "k.db"
    writer, reader = _pinned(path)
    report, _sent, kills = _run(tmp_path, path, cgroup=SSH, evict_wal_bytes=100 * GIB)
    assert kills == [] and report.evicted == []
    reader.execute("COMMIT")
    reader.close()
    writer.close()


def test_eviction_is_announced_even_when_the_level_is_unchanged(tmp_path):
    """A second eviction on a WARN that already paged would otherwise be silent."""
    path = tmp_path / "k.db"
    writer, reader = _pinned(path)
    sent: list[str] = []
    _run(tmp_path, path, cgroup=SSH, sent=sent)
    _run(tmp_path, path, cgroup=SSH, sent=sent)
    assert sum("STOPPED" in s for s in sent) == 2
    reader.execute("COMMIT")
    reader.close()
    writer.close()


# --------------------------------------------------------------------------------------
# second valve: restart a RESTARTABLE kaiba service that keeps the log from resetting
# --------------------------------------------------------------------------------------

SCAN = "/user.slice/user-1000.slice/user@1000.service/app.slice/kaiba-scan.service"
OPS = "/user.slice/user-1000.slice/user@1000.service/app.slice/kaiba-ops.service"
PROTECTION = "/user.slice/user-1000.slice/user@1000.service/app.slice/kaiba-protection.service"


def test_unit_of_reads_the_service_name():
    assert db_guard.unit_of(SCAN) == "kaiba-scan.service"
    assert db_guard.unit_of(SSH) is None
    assert db_guard.unit_of(None) is None


def test_restart_picks_the_most_behind_restartable_unit_only():
    readers = [
        {"pid": 10, "frames_behind": 27_955},   # ops
        {"pid": 11, "frames_behind": 43_341},   # scan
        {"pid": 12, "frames_behind": 900_000},  # engine: never restarted by the guard
        {"pid": 13, "frames_behind": 900_000},  # protection: never
    ]
    cg = {10: OPS, 11: SCAN, 12: SERVICE, 13: PROTECTION}.get
    pick = db_guard.select_restart(readers, frames_behind=10_000, cgroup_of=cg,
                                   last_restart_ms={}, now_ms=10**12, cooldown_s=1800)
    assert pick["unit"] == "kaiba-scan.service" and pick["pid"] == 11


def test_restart_respects_threshold_and_cooldown():
    readers = [{"pid": 11, "frames_behind": 43_341}]
    cg = {11: SCAN}.get
    assert db_guard.select_restart(readers, frames_behind=50_000, cgroup_of=cg,
                                   last_restart_ms={}, now_ms=10**12, cooldown_s=1800) is None
    recent = {"kaiba-scan.service": 10**12 - 60_000}
    assert db_guard.select_restart(readers, frames_behind=10_000, cgroup_of=cg,
                                   last_restart_ms=recent, now_ms=10**12, cooldown_s=1800) is None


def _restart_run(tmp_path, path, *, cgroups, inflight=0, sent=None, restarts=None, now_ms=10**12):
    sent = [] if sent is None else sent
    restarts = [] if restarts is None else restarts
    readers = [{"pid": 11, "slots": [1], "cmd": "python -m kaiba scan run", "frames_behind": 43_341}]
    return db_guard.run(
        path, wal_truncate_bytes=1, wal_alert_bytes=1, busy_ms=50,
        state_path=tmp_path / "s.json", disk_usage=_roomy, now_ms=now_ms,
        notify=lambda t: sent.append(t) or True,
        cgroup_of=cgroups.get, kill=lambda p, s: None, alive=lambda p: False, sleep=lambda s: None,
        readers_of=lambda db, hdr: list(readers),
        restart_wal_bytes=1, restart_frames_behind=10_000, restart_cooldown_s=1800,
        restart_unit=lambda u: restarts.append(u) or True, orders_in_flight=lambda p: inflight,
    ), sent, restarts


def test_run_restarts_a_pinning_scan_and_announces_it(tmp_path):
    path = tmp_path / "k.db"
    writer, reader = _pinned(path)
    report, sent, restarts = _restart_run(tmp_path, path, cgroups={11: SCAN})
    assert restarts == ["kaiba-scan.service"]
    assert report.restarted and report.restarted["unit"] == "kaiba-scan.service"
    assert sent and "RESTARTED kaiba-scan.service" in sent[0]
    reader.execute("COMMIT")
    reader.close()
    writer.close()


def test_run_never_restarts_with_an_order_in_flight(tmp_path):
    path = tmp_path / "k.db"
    writer, reader = _pinned(path)
    report, _sent, restarts = _restart_run(tmp_path, path, cgroups={11: SCAN}, inflight=1)
    assert restarts == [] and report.restarted is None
    assert any("orders in flight" in r for r in report.reasons)
    reader.execute("COMMIT")
    reader.close()
    writer.close()


def test_run_never_restarts_when_the_order_check_fails(tmp_path):
    path = tmp_path / "k.db"
    writer, reader = _pinned(path)
    report, _sent, restarts = _restart_run(tmp_path, path, cgroups={11: SCAN}, inflight=None)
    assert restarts == [] and report.restarted is None
    reader.execute("COMMIT")
    reader.close()
    writer.close()


def test_run_never_restarts_a_money_service(tmp_path):
    path = tmp_path / "k.db"
    writer, reader = _pinned(path)
    report, _sent, restarts = _restart_run(tmp_path, path, cgroups={11: PROTECTION})
    assert restarts == [] and report.restarted is None
    reader.execute("COMMIT")
    reader.close()
    writer.close()


def test_the_cooldown_survives_between_passes(tmp_path):
    path = tmp_path / "k.db"
    writer, reader = _pinned(path)
    restarts: list[str] = []
    _restart_run(tmp_path, path, cgroups={11: SCAN}, restarts=restarts, now_ms=10**12)
    _restart_run(tmp_path, path, cgroups={11: SCAN}, restarts=restarts, now_ms=10**12 + 60_000)
    assert restarts == ["kaiba-scan.service"]
    reader.execute("COMMIT")
    reader.close()
    writer.close()


def test_a_restart_is_announced_even_when_the_level_is_unchanged(tmp_path):
    """Second restart, after the cooldown but inside the hourly repeat window: still paged."""
    path = tmp_path / "k.db"
    writer, reader = _pinned(path)
    sent: list[str] = []
    restarts: list[str] = []
    _restart_run(tmp_path, path, cgroups={11: SCAN}, sent=sent, restarts=restarts, now_ms=10**12)
    _restart_run(tmp_path, path, cgroups={11: SCAN}, sent=sent, restarts=restarts,
                 now_ms=10**12 + 1_801_000)
    assert restarts == ["kaiba-scan.service", "kaiba-scan.service"]
    assert sum("RESTARTED" in s for s in sent) == 2
    reader.execute("COMMIT")
    reader.close()
    writer.close()
