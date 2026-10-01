"""The database guard: reclaims an overgrown WAL and pages before the disk fills.

Found 2026-09-29: the box's disk hit 100% and every service wrote nothing for fourteen
hours, unannounced. The WAL was 19.8 GB because readers never let it reset, and nothing in
kaiba ever asked for a truncating checkpoint. See ``kaiba/ops/db_guard.py``.
"""

from __future__ import annotations

import sqlite3
from collections import namedtuple
from pathlib import Path

import pytest

from kaiba.core import db
from kaiba.ops import db_guard

GIB = db_guard.GIB
Usage = namedtuple("Usage", "total used free")


def _roomy(_path):
    return Usage(100 * GIB, 10 * GIB, 90 * GIB)


def _wal_db(path: Path) -> sqlite3.Connection:
    """A WAL database whose log is never checkpointed by the writer itself.

    The writer stays open for the test: closing the last connection makes SQLite checkpoint
    and delete the WAL on its own, which would pass the test without the guard doing
    anything.
    """
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE t (x BLOB)")
    for _ in range(200):
        conn.execute("INSERT INTO t VALUES (randomblob(2000))")
    return conn


def test_truncates_an_overgrown_wal(tmp_path):
    path = tmp_path / "k.db"
    writer = _wal_db(path)
    assert db_guard.wal_path(path).stat().st_size > 100_000

    report = db_guard.run(
        path, wal_truncate_bytes=1, state_path=tmp_path / "s.json",
        disk_usage=_roomy, notify=lambda _t: True,
    )

    assert report.checkpoint is not None and report.checkpoint[0] == 0
    assert db_guard.wal_path(path).stat().st_size == 0
    assert report.after.wal_bytes == 0 < report.before.wal_bytes
    writer.close()


def test_a_small_wal_is_left_alone(tmp_path):
    path = tmp_path / "k.db"
    writer = _wal_db(path)
    size = db_guard.wal_path(path).stat().st_size

    report = db_guard.run(
        path, state_path=tmp_path / "s.json", disk_usage=_roomy, notify=lambda _t: True
    )

    assert report.checkpoint is None
    assert db_guard.wal_path(path).stat().st_size == size
    writer.close()


def test_a_pinned_wal_is_reported_and_paged(tmp_path):
    path = tmp_path / "k.db"
    writer = _wal_db(path)
    reader = sqlite3.connect(str(path), isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM t").fetchone()  # holds a snapshot
    for _ in range(50):
        writer.execute("INSERT INTO t VALUES (randomblob(2000))")
    sent: list[str] = []

    report = db_guard.run(
        path, wal_truncate_bytes=1, wal_alert_bytes=1, busy_ms=50,
        state_path=tmp_path / "s.json", disk_usage=_roomy,
        notify=lambda t: sent.append(t) or True,
    )

    assert report.checkpoint is not None and report.checkpoint[0] == 1  # refused
    assert db_guard.wal_path(path).stat().st_size > 0
    assert report.level == db_guard.WARN
    assert len(sent) == 1 and "WAL" in sent[0] and "refused" in sent[0]
    reader.execute("COMMIT")
    reader.close()
    writer.close()


def test_merged_lock_ranges_name_every_read_slot():
    # /proc/locks merges adjacent ranges one process holds. Reading only the start offset
    # reported slot 1 for a process that was pinning the log through slot 2.
    inode = 1356485
    text = "\n".join([
        "1: POSIX  ADVISORY  READ 2340215 fd:02:1356485 124 125",
        "2: POSIX  ADVISORY  READ 3798220 fd:02:1356485 128 128",  # DMS byte, not a slot
        "3: POSIX  ADVISORY  WRITE 1119353 fd:02:1356485 120 120",  # the writer
        "4: POSIX  ADVISORY  READ 2925423 fd:02:1356485 127 128",
        "5: POSIX  ADVISORY  READ 999 fd:02:777 124 124",  # another file
        "6: -> POSIX  ADVISORY  READ 555 fd:02:1356485 124 124",  # a waiter
    ])

    readers = db_guard.parse_wal_readers(text, inode)

    assert readers == [
        {"pid": 2340215, "slots": [1, 2]},
        {"pid": 2925423, "slots": [4]},
    ]


def test_disk_alert_escalates_repeats_hourly_and_recovers(tmp_path):
    path = tmp_path / "k.db"
    sqlite3.connect(str(path)).close()
    state = tmp_path / "s.json"
    free = {"bytes": 2 * GIB}
    sent: list[str] = []

    def usage(_p):
        return Usage(79 * GIB, 79 * GIB - free["bytes"], free["bytes"])

    def tick(minute: int) -> db_guard.Report:
        return db_guard.run(
            path, state_path=state, disk_usage=usage,
            notify=lambda t: sent.append(t) or True, now_ms=minute * 60_000,
        )

    assert tick(0).level == db_guard.CRIT and len(sent) == 1
    assert "CRIT" in sent[0] and "Free space" in sent[0]
    tick(10)
    assert len(sent) == 1  # no repeat inside the hour
    tick(61)
    assert len(sent) == 2  # but it does repeat
    free["bytes"] = 50 * GIB
    assert tick(62).level == db_guard.OK
    assert len(sent) == 3 and "recovered" in sent[2]
    tick(63)
    assert len(sent) == 3


def test_warn_escalating_to_crit_alerts_at_once(tmp_path):
    path = tmp_path / "k.db"
    sqlite3.connect(str(path)).close()
    state = tmp_path / "s.json"
    free = {"bytes": 8 * GIB}
    sent: list[str] = []

    def tick(minute: int) -> None:
        db_guard.run(
            path, state_path=state,
            disk_usage=lambda _p: Usage(79 * GIB, 0, free["bytes"]),
            notify=lambda t: sent.append(t) or True, now_ms=minute * 60_000,
        )

    tick(0)
    free["bytes"] = 1 * GIB
    tick(5)
    assert len(sent) == 2 and "WARN" in sent[0] and "CRIT" in sent[1]


def test_an_undelivered_alert_is_retried_next_pass(tmp_path):
    path = tmp_path / "k.db"
    sqlite3.connect(str(path)).close()
    state = tmp_path / "s.json"
    attempts: list[str] = []
    low = lambda _p: Usage(79 * GIB, 78 * GIB, 1 * GIB)  # noqa: E731

    db_guard.run(path, state_path=state, disk_usage=low,
                 notify=lambda t: attempts.append(t) and False, now_ms=0)
    db_guard.run(path, state_path=state, disk_usage=low,
                 notify=lambda t: attempts.append(t) or True, now_ms=60_000)
    db_guard.run(path, state_path=state, disk_usage=low,
                 notify=lambda t: attempts.append(t) or True, now_ms=120_000)

    assert len(attempts) == 2  # failed, retried and delivered, then quiet


def test_a_missing_database_is_not_created(tmp_path):
    path = tmp_path / "absent.db"
    db_guard.run(path, wal_truncate_bytes=0, state_path=tmp_path / "s.json",
                 disk_usage=_roomy, notify=lambda _t: True)
    assert not path.exists()


def test_every_connection_caps_the_wal_file(tmp_path):
    conn = db.connect(tmp_path / "k.db")
    try:
        limit = conn.execute("PRAGMA journal_size_limit").fetchone()[0]
    finally:
        conn.close()
    assert limit == db.WAL_SIZE_LIMIT_BYTES


@pytest.mark.parametrize("free_gb,level", [(50, "ok"), (9, "warn"), (2, "crit")])
def test_assess_levels(free_gb, level):
    reading = db_guard.Reading(10 * GIB, 0, free_gb * GIB, 79 * GIB)
    got, _ = db_guard.assess(
        reading, wal_alert_bytes=db_guard.WAL_ALERT_BYTES,
        disk_warn_free=db_guard.DISK_WARN_FREE_BYTES,
        disk_crit_free=db_guard.DISK_CRIT_FREE_BYTES,
    )
    assert got == level
