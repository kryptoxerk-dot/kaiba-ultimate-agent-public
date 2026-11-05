"""The disk guard (kaiba/ops/disk_guard.py): it ACTS before the disk fills.

Found 2026-10-12: the root disk reached 98% while db_guard paged ``crit`` and nobody acted.
Each test is written as the failure it prevents:

* dead files are removed and live ones are not -- a cache file inside its TTL, a backup
  partial young enough to be a running snapshot, a research directory still being written;
* emergency retention runs at ACT and not below it;
* the entry halt is set only at HALT, never overwrites someone else's halt, and only this
  guard's own halt is ever lifted;
* used% is df's, so the root reserve cannot hide the last gigabytes.
"""

from __future__ import annotations

import os
import time
from collections import namedtuple
from pathlib import Path

import pytest

from kaiba.ops import disk_guard as DG

GIB = DG.GIB
DU = namedtuple("DU", "total used free")
NOW = 1_800_000_000.0


def _touch(p: Path, age_s: float, size: int = 10) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    os.utime(p, (NOW - age_s, NOW - age_s))
    return p


def _disk(pct: float, total: int = 100 * GIB):
    """A disk at ``pct`` df-Use% with a 5% root reserve."""
    avail_plus_used = int(total * 0.95)
    used = int(avail_plus_used * pct / 100)
    return lambda _p: DU(total, used, avail_plus_used - used)


def _run(tmp_path: Path, pct: float, **kw):
    calls: dict[str, list] = {"retention": [], "halt": [], "lift": []}
    kw.setdefault("emergency_retention", lambda db, b: calls["retention"].append(b) or {"deleted": 7})
    kw.setdefault("halt_entries", lambda db, r: calls["halt"].append(r) or "halted")
    kw.setdefault("lift_halt", lambda db: calls["lift"].append(1) or False)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "kaiba.db").write_bytes(b"")
    rep = DG.run(data, tmp_dir=tmp_path / "tmp", disk_usage=_disk(pct), now_s=NOW,
                 state_path=tmp_path / "state.json", notify=lambda _t: True, **kw)
    return rep, calls


def test_used_pct_is_dfs_not_total():
    # 79 GB disk, 74 GB used, 2.3 GB available: df says 97%, used/total says 94%.
    u = DG.Usage(total=79 * GIB, used=74 * GIB, free=int(2.3 * GIB))
    assert round(u.used_pct) == 97


def test_cache_files_past_every_ttl_go_and_live_ones_stay(tmp_path):
    root = tmp_path / "cache"
    old = _touch(root / "gmgn" / "a.json", 4 * DG.DAY_S)
    fresh = _touch(root / "gmgn" / "b.json", 2 * DG.DAY_S)
    coin = _touch(root / "pumpfun" / "c.json", 10 * DG.DAY_S)  # 7 d TTL + 7 d grace: still servable
    coin_dead = _touch(root / "pumpfun" / "d.json", 16 * DG.DAY_S)
    n, freed, errs = DG.sweep_cache(root, NOW, dry_run=False)
    assert (n, errs) == (2, [])
    assert not old.exists() and not coin_dead.exists()
    assert fresh.exists() and coin.exists()


def test_dry_run_removes_nothing(tmp_path):
    p = _touch(tmp_path / "data" / "backups" / ".core-x.db.partial", 10 * DG.HOUR_S)
    c = _touch(tmp_path / "data" / "cache" / "gmgn" / "a.json", 9 * DG.DAY_S)
    rep = DG.run(tmp_path / "data", tmp_dir=tmp_path / "tmp", disk_usage=_disk(99), now_s=NOW,
                 state_path=tmp_path / "s.json", dry_run=True,
                 emergency_retention=lambda *_: pytest.fail("dry run ran retention"),
                 halt_entries=lambda *_: pytest.fail("dry run halted"),
                 lift_halt=lambda *_: pytest.fail("dry run lifted"))
    assert p.exists() and c.exists()
    assert rep.freed_bytes > 0 and rep.level == DG.HALT


def test_a_running_backup_partial_is_not_touched(tmp_path):
    running = _touch(tmp_path / "data" / "backups" / ".core-new.db.partial", 20 * 60)
    dead = _touch(tmp_path / "data" / "backups" / ".core-old.db.partial", 4 * DG.HOUR_S)
    finished = _touch(tmp_path / "data" / "backups" / "core-20261001.db.gz", 30 * DG.DAY_S)
    _run(tmp_path, 50)
    assert running.exists() and finished.exists() and not dead.exists()


def test_tmp_db_copies_go_after_12h_and_at_any_age_under_halt(tmp_path):
    young = _touch(tmp_path / "tmp" / "kaiba.db.copy", 60)
    old = _touch(tmp_path / "tmp" / "kaiba.db.bak-20260924.gz", 2 * DG.DAY_S)
    other = _touch(tmp_path / "tmp" / "vps_prices.db", 9 * DG.DAY_S)  # not ours: never touched
    _run(tmp_path, 50)
    assert young.exists() and not old.exists() and other.exists()
    _run(tmp_path, 95)
    assert not young.exists() and other.exists()


def test_scratch_dirs_go_only_when_nothing_in_them_changed_for_14_days(tmp_path):
    data = tmp_path / "data"
    stale = data / "codex-exit-audit-20260924"
    _touch(stale / "out.json", 20 * DG.DAY_S)
    os.utime(stale, (NOW - 20 * DG.DAY_S,) * 2)
    active = data / "codex-follow-snipe-20261007"
    _touch(active / "old.json", 20 * DG.DAY_S)
    _touch(active / "today.json", 3600)  # still being written to
    os.utime(active, (NOW - 20 * DG.DAY_S,) * 2)
    learning = data / "learning"  # not a scratch prefix: kaiba reads it
    _touch(learning / "x.json", 90 * DG.DAY_S)
    _run(tmp_path, 50)
    assert not stale.exists() and active.exists() and learning.exists()


@pytest.mark.parametrize("pct,retention,halt", [(50, False, False), (82, False, False),
                                                (86, True, False), (95, True, True)])
def test_levers_fire_at_their_lines_and_not_below(tmp_path, pct, retention, halt):
    rep, calls = _run(tmp_path, pct)
    assert bool(calls["retention"]) is retention
    assert bool(calls["halt"]) is halt
    if halt:
        assert calls["halt"][0].startswith(DG.HALT_PREFIX)


@pytest.mark.parametrize("pct,reusable,runs", [
    (86, 14 * GIB, False),               # ACT, a file full of free pages: deleting cannot move the disk
    (86, DG.REUSABLE_SKIP_BYTES, False),  # the boundary skips
    (86, DG.REUSABLE_SKIP_BYTES - 1, True),
    (86, None, True),                    # unknown free space: act, never assume
    (95, 14 * GIB, True),                # HALT always runs the emergency pass
])
def test_act_skips_emergency_retention_while_the_database_has_free_pages(tmp_path, pct, reusable, runs):
    rep, calls = _run(tmp_path, pct, db_reusable=lambda _db: reusable)
    assert bool(calls["retention"]) is runs
    assert rep.db_reusable == reusable
    if not runs:
        assert rep.retention["deleted"] == 0 and rep.retention["stopped"].startswith("skipped:")
        assert rep.level == DG.ACT  # still ACT: the page still goes out
    assert bool(calls["halt"]) is (pct >= DG.HALT_PCT)


def test_reusable_bytes_reads_the_real_freelist(tmp_path):
    import sqlite3

    db = tmp_path / "f.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (x BLOB)")
    c.executemany("INSERT INTO t VALUES (?)", [(b"x" * 4000,) for _ in range(200)])
    c.commit()
    assert DG._db_reusable_bytes(db) == 0
    c.execute("DELETE FROM t")
    c.commit()
    c.close()
    probe = sqlite3.connect(db)
    page = probe.execute("PRAGMA page_size").fetchone()[0]
    probe.close()
    assert DG._db_reusable_bytes(db) >= 150 * page
    assert DG._db_reusable_bytes(tmp_path / "missing" / "x.db") is None


def test_the_page_names_the_reusable_space(tmp_path):
    rep, _ = _run(tmp_path, 86, db_reusable=lambda _db: 14 * GIB)
    assert "reusable inside the database" in rep.alert


def test_halt_is_lifted_below_act_and_only_through_the_prefix_gate(tmp_path):
    rep, calls = _run(tmp_path, 60, lift_halt=lambda db: True)
    assert rep.halt and rep.halt.startswith("resumed")


def test_halt_entries_never_overwrites_another_halt(tmp_db, tmp_path):
    from kaiba.core.config import get_settings
    from kaiba.execution.risk import RiskGate

    db = get_settings().db_path
    RiskGate().halt("operator: owner pause", tmp_db)

    assert DG._halt_entries(db, "disk_guard: 95%").startswith("already halted: operator")
    assert DG._lift_halt(db) is False  # not ours: stays
    assert RiskGate()._state_row(tmp_db)["halt_reason"] == "operator: owner pause"


def test_own_halt_round_trip(tmp_db):
    from kaiba.core.config import get_settings
    from kaiba.execution.risk import RiskGate

    db = get_settings().db_path
    assert DG._halt_entries(db, "disk_guard: 95%") == "halted"
    assert DG._halt_entries(db, "disk_guard: 96%") == "already halted by us"
    assert DG._lift_halt(db) is True
    assert int(RiskGate()._state_row(tmp_db)["halted"]) == 0


def test_alert_on_escalation_then_hourly_then_recovery(tmp_path):
    sent: list[str] = []
    data = tmp_path / "data"
    data.mkdir()
    kw = dict(tmp_dir=tmp_path / "tmp", state_path=tmp_path / "s.json", notify=lambda t: sent.append(t) or True,
              emergency_retention=lambda *_: {"deleted": 0}, halt_entries=lambda *_: "halted",
              lift_halt=lambda *_: False)
    DG.run(data, disk_usage=_disk(81), now_s=NOW, **kw)
    DG.run(data, disk_usage=_disk(81), now_s=NOW + 60, **kw)
    DG.run(data, disk_usage=_disk(81), now_s=NOW + 3700, **kw)          # WARN: not hourly
    DG.run(data, disk_usage=_disk(81), now_s=NOW + 13 * 3600, **kw)     # ...but twice a day
    DG.run(data, disk_usage=_disk(86), now_s=NOW + 13 * 3600 + 60, **kw)  # escalation: at once
    DG.run(data, disk_usage=_disk(86), now_s=NOW + 14 * 3600 + 120, **kw)  # ACT: hourly
    DG.run(data, disk_usage=_disk(40), now_s=NOW + 14 * 3600 + 180, **kw)
    assert [t.split(" on ")[0].split("— ")[1] for t in sent] == ["WARN", "WARN", "ACT", "ACT", "recovered"]


def test_ships_in_a_systemd_unit_and_timer():
    root = Path(__file__).resolve().parents[1] / "deploy" / "systemd" / "user"
    svc = (root / "kaiba-disk-guard.service").read_text()
    timer = (root / "kaiba-disk-guard.timer").read_text()
    assert "kaiba.ops.disk_guard" in svc and "OnUnitActiveSec" in timer


def test_never_reaches_into_hermes_config_or_other_projects(tmp_path):
    home = tmp_path
    for p in (home / ".hermes" / "x", home / "vanguard-v3.0.0" / "y", home / ".config" / "kaiba" / ".env"):
        _touch(p, 365 * DG.DAY_S)
    _run(tmp_path, 99)
    assert all(p.exists() for p in (home / ".hermes" / "x", home / "vanguard-v3.0.0" / "y",
                                    home / ".config" / "kaiba" / ".env"))


def test_module_time_is_not_used_when_now_is_given(tmp_path):
    # a guard whose decisions depend on the wall clock cannot be tested; now_s must govern.
    p = _touch(tmp_path / "data" / "backups" / ".core-x.db.partial", 4 * DG.HOUR_S)
    assert time.time() != NOW
    _run(tmp_path, 50)
    assert not p.exists()
