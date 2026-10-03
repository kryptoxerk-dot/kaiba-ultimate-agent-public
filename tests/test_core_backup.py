"""The core-state backup (kaiba/ops/core_backup.py).

Each test is the failure it prevents:

* a writer is never blocked by the backup (protection must not see ``database is locked``):
  a writer with ``busy_timeout=0`` commits in the middle of the snapshot;
* the copy is ONE instant: a row committed mid-snapshot is in no table of the copy;
* only the core tables are copied (no tape), decisions only for their window;
* the restore drill catches a corrupted file, a missing row and a rewritten journal;
* rotation keeps the newest N and touches nothing that is not ours;
* the dry run and the refusals write nothing.
"""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from kaiba.core import journal
from kaiba.core.schemas import now_ms
from kaiba.ops import core_backup as CB

ROOT = Path(__file__).resolve().parents[1]
DAY = 86_400_000


def _seed(conn) -> int:
    now = now_ms()
    for i in range(5):
        conn.execute(
            "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms) VALUES (?,?,?,?,?,?)",
            (f"pos{i}", "sol", f"TOK{i}", "sm-trenches", "live", now - i * 1000),
        )
    for i, age_days in enumerate((1, 2, 45, 60)):
        conn.execute(
            "INSERT INTO decisions (decision_id, ts_ms, lane, mode, chain, token, action) VALUES (?,?,?,?,?,?,?)",
            (f"d{i}", now - age_days * DAY, "sm-trenches", "live", "sol", "TOK", "enter"),
        )
    for i in range(50):
        conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?)", (f"k{i}", "v" * 50, now))
    conn.execute(
        "INSERT INTO wallet_scores (chain, address, score, grade, evidence_weight, archetype, model_version, "
        "scored_at_ms) VALUES ('sol','W1',41.0,'B',0.8,'trader','kaiba-wallet-v1',?)", (now,),
    )
    for i in range(30):  # tape: must NOT be copied
        conn.execute(
            "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, source) VALUES (?,?,?,?,?,?,?)",
            ("sol", f"tx{i}", now, "W1", "TOK", "buy", "test"),
        )
    for i in range(3):
        journal.append("observation", f"lesson {i}", conn=conn)
    return now


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _gunzip(gz: Path, out: Path) -> Path:
    with gzip.open(gz, "rb") as src, open(out, "wb") as dst:
        dst.write(src.read())
    return out


def test_a_backup_copies_the_core_tables_only_and_passes_its_own_drill(tmp_db, tmp_path):
    now = _seed(tmp_db)
    out = tmp_path / "backups"
    manifest = CB.run_backup(tmp_path / "kaiba.db", out, now_ms=now, min_free_bytes=0)

    gz = out / manifest["file"]
    assert gz.exists() and (out / gz.name.replace(".db.gz", ".json")).exists()
    assert manifest["quick_check"] == "ok"
    assert manifest["journal_chain"] == {"present": True, "ok": True, "error": None, "rows": 3}
    assert manifest["tables"]["positions"]["rows"] == 5
    assert manifest["tables"]["kv"]["rows"] == 50
    assert manifest["tables"]["decisions"]["rows"] == 2, "decisions are kept for 30 days only"
    assert manifest["missing_tables"] == []
    assert CB._sha256(gz) == manifest["gz_sha256"]
    assert not list(out.glob(".core-*")), "no partial left behind"

    raw = _gunzip(gz, tmp_path / "restored.db")
    names = _tables(raw)
    assert "swaps" not in names and "events" not in names and "wallet_score_history" not in names
    assert {t.name for t in CB.CORE_TABLES} <= names
    conn = sqlite3.connect(raw)
    try:
        idx = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' "
                                         "AND name NOT LIKE 'sqlite_autoindex%'")]
        assert idx == [], "indexes are not copied; their DDL is"
    finally:
        conn.close()
    assert any("CREATE INDEX" in s for s in manifest["indexes"]["decisions"])

    drill = CB.verify_backup(gz)
    assert drill["ok"] is True, drill
    assert drill["tables_checked"] == len(CB.CORE_TABLES)
    assert drill["journal_chain"]["ok"] is True
    if os.name != "nt":
        assert (gz.stat().st_mode & 0o777) == 0o600
        assert (out.stat().st_mode & 0o777) == 0o700


def test_a_writer_is_never_blocked_and_the_copy_is_one_instant(tmp_db, tmp_path):
    now = _seed(tmp_db)
    src = tmp_path / "kaiba.db"
    writer = sqlite3.connect(src, isolation_level=None)
    writer.execute("PRAGMA busy_timeout=0")  # a writer that would fail on ANY lock wait
    committed: list[str] = []

    def mid_snapshot(table: str) -> None:
        if table != "kv":
            return
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO kv (key, value, updated_ms) VALUES ('late', 'x', ?)", (now,))
        writer.execute(
            "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms) "
            "VALUES ('late', 'sol', 'T', 'l', 'live', ?)", (now,),
        )
        writer.execute("COMMIT")
        committed.append(table)

    tables = (CB.CoreTable("kv", "x"), CB.CoreTable("positions", "x"))
    try:
        snap = CB.snapshot(src, tmp_path / "copy.db", now_ms=now, tables=tables, between_tables=mid_snapshot)
    finally:
        writer.close()
    assert committed == ["kv"], "the writer committed while the snapshot was open"
    assert snap.tables["kv"]["rows"] == 50
    assert snap.tables["positions"]["rows"] == 5, "a row committed mid-snapshot leaked into the copy"
    live = tmp_db.execute("SELECT count(*) FROM positions").fetchone()[0]
    assert live == 6, "the write really landed in the live database"


def test_the_snapshot_is_aborted_past_its_time_cap_and_leaves_nothing(tmp_db, tmp_path):
    now = _seed(tmp_db)
    for i in range(3000):
        tmp_db.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?)", (f"bulk{i}", "v", now))
    out = tmp_path / "backups"
    with pytest.raises(sqlite3.OperationalError):
        CB.run_backup(tmp_path / "kaiba.db", out, now_ms=now, min_free_bytes=0, max_snapshot_s=0)
    assert [p.name for p in out.iterdir()] == [], list(out.iterdir())
    # and the live database is untouched and writable
    tmp_db.execute("INSERT INTO kv (key, value, updated_ms) VALUES ('after', 'v', 1)")


def test_the_drill_catches_corruption_a_missing_row_and_a_rewritten_journal(tmp_db, tmp_path):
    now = _seed(tmp_db)
    out = tmp_path / "backups"
    manifest = CB.run_backup(tmp_path / "kaiba.db", out, now_ms=now, min_free_bytes=0)
    gz = out / manifest["file"]

    # 1. a flipped byte in the gzip
    data = bytearray(gz.read_bytes())
    data[len(data) // 2] ^= 0xFF
    bad = out / manifest["file"]
    good = gz.read_bytes()
    bad.write_bytes(bytes(data))
    assert CB.verify_backup(bad)["ok"] is False
    gz.write_bytes(good)
    assert CB.verify_backup(gz)["ok"] is True

    # 1b. a file that still decompresses to the same bytes but is not the file the manifest
    # names (here: the gzip header's mtime rewritten) -- the artefact itself is checked
    swapped = bytearray(good)
    swapped[4:8] = b""
    gz.write_bytes(bytes(swapped))
    report = CB.verify_backup(gz)
    assert report["ok"] is False and report["checks"]["gz_sha256"] is False
    assert report["checks"].get("raw_sha256", True) is True
    gz.write_bytes(good)

    # 2. a row missing from the copy (verify a raw .db: no manifest, the internal meta table)
    raw = _gunzip(gz, tmp_path / "missing.db")
    c = sqlite3.connect(raw)
    c.execute("DELETE FROM positions WHERE position_id = 'pos0'")
    c.commit()
    c.close()
    report = CB.verify_backup(raw)
    assert report["ok"] is False and report["checks"].get("rows:positions") is False

    # 3. a journal entry rewritten after the fact
    raw2 = _gunzip(gz, tmp_path / "rewritten.db")
    c = sqlite3.connect(raw2)
    c.execute("UPDATE journal SET body = 'nothing happened' WHERE seq = 2")
    c.commit()
    c.close()
    report = CB.verify_backup(raw2)
    assert report["ok"] is False and report["journal_chain"]["ok"] is False


def test_rotation_keeps_the_newest_n_and_nothing_else_is_touched(tmp_path):
    out = tmp_path / "b"
    out.mkdir()
    for day in ("20260920", "20260921", "20260922", "20260923", "20260924"):
        (out / f"core-{day}.db.gz").write_bytes(b"x")
        (out / f"core-{day}.json").write_text("{}")
    for keepme in ("notes.txt", "core-latest.db.gz", "kaiba.db"):
        (out / keepme).write_text("mine")
    removed = CB.prune_old(out, keep=3)
    assert sorted(removed) == ["core-20260920.db.gz", "core-20260921.db.gz"]
    left = sorted(p.name for p in out.iterdir())
    assert left == sorted([
        "core-20260922.db.gz", "core-20260922.json", "core-20260923.db.gz", "core-20260923.json",
        "core-20260924.db.gz", "core-20260924.json", "notes.txt", "core-latest.db.gz", "kaiba.db",
    ])


def test_refusals_write_nothing(tmp_db, tmp_path):
    now = _seed(tmp_db)
    out = tmp_path / "backups"
    with pytest.raises(CB.BackupRefused):
        CB.run_backup(tmp_path / "kaiba.db", out, now_ms=now, min_free_bytes=10**18)
    assert list(out.iterdir()) == []
    lock = out / ".core-backup.lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "host": __import__("socket").gethostname(),
                                "ts": time.time()}))
    with pytest.raises(CB.BackupRefused):
        CB.run_backup(tmp_path / "kaiba.db", out, now_ms=now, min_free_bytes=0)
    assert sorted(p.name for p in out.iterdir()) == [".core-backup.lock"]
    lock.write_text(json.dumps({"pid": os.getpid(), "host": "elsewhere", "ts": time.time() - 3 * 3600}))
    CB.run_backup(tmp_path / "kaiba.db", out, now_ms=now, min_free_bytes=0)  # stale lock reclaimed


def test_the_dry_run_reports_and_writes_nothing(tmp_db, tmp_path):
    _seed(tmp_db)
    out = tmp_path / "backups"
    report = CB.dry_run(tmp_path / "kaiba.db", out)
    assert not out.exists()
    assert report["tables"]["positions"]["rows"] == 5
    assert report["tables"]["decisions"]["rows"] == 2
    assert "swaps" not in report["tables"]
    assert report["est_raw_mb"] >= report["est_gz_mb"] >= 0
    assert report["tables"]["kv"]["avg_row_bytes"] > 50 and report["tables"]["kv"]["gzip_ratio"] < 1


def test_the_cli_backs_up_verifies_and_records_last_json(tmp_db, tmp_path, capsys):
    _seed(tmp_db)
    out = tmp_path / "backups"
    assert CB.main(["--db", str(tmp_path / "kaiba.db"), "--out-dir", str(out), "--min-free-gb", "0"]) == 0
    printed = json.loads(capsys.readouterr().out)
    last = json.loads((out / "last.json").read_text(encoding="utf-8"))
    assert last["status"] == "ok" and last["file"] == printed["file"]
    assert CB.main(["--verify", str(out / printed["file"])]) == 0


def test_the_unit_runs_at_idle_priority_with_no_credentials():
    unit = (ROOT / "deploy" / "systemd" / "user" / "kaiba-core-backup.service").read_text(encoding="utf-8")
    timer = (ROOT / "deploy" / "systemd" / "user" / "kaiba-core-backup.timer").read_text(encoding="utf-8")
    lines = {ln.split("=", 1)[0]: ln.split("=", 1)[1] for ln in unit.splitlines() if "=" in ln
             and not ln.startswith("#")}
    assert lines["Nice"] == "19" and lines["IOSchedulingClass"] == "idle"
    assert "EnvironmentFile" not in lines, "a backup needs no credential and must never see one"
    assert "-m kaiba.ops.core_backup" in lines["ExecStart"] and "data/backups" in lines["ExecStart"]
    assert lines["UMask"] == "0077"
    assert "OnCalendar=" in timer and "Unit=kaiba-core-backup.service" in timer
