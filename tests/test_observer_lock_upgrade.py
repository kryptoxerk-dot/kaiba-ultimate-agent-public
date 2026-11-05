"""Atomic publication faults plus the genuine budget/admission/capture path."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from kaiba.learning import observer_lock_upgrade as upgrade
from kaiba.learning import observer_upgrade as original
from kaiba.learning import prospective_quotes as base
from tests.test_observer_upgrade import install
from tests.test_prospective_quotes import at, read, write
from tests.test_prospective_quotes import setup as observer_setup  # noqa: F401


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("external transport forbidden"))


@pytest.fixture
def locked_upgrade(request):
    s = install(request.getfixturevalue("observer_setup"))
    s.collector = upgrade.LockSafeQuoteCollector(s.research, s.root / "prospective-quotes",
        learning_root=s.root, clock=lambda: s.clock["ms"] / 1000, provider=s.provider)
    recipe = upgrade.prepare_manifest(s.collector.manifest_path.read_bytes(), prepared_ms=s.clock["ms"])
    write(s.collector.lock_manifest_path, recipe)
    return s


def artifacts(s):
    return {p: p.read_bytes() for p in (s.collector.state_path, s.collector.ledger_path, s.collector.manifest_path)}


def test_recipe_is_pure_and_binds_exact_original_and_own_sources(locked_upgrade):
    s = locked_upgrade
    before = artifacts(s)
    raw = s.collector.manifest_path.read_bytes()
    result = upgrade.prepare_manifest(raw, prepared_ms=s.clock["ms"])
    assert result == read(s.collector.lock_manifest_path)
    assert result["version"] == upgrade.VERSION and result["policy"] == "atomiclock/1"
    assert result["original_manifest_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["original_source_bindings"] == read(s.collector.manifest_path)["source_bindings"]
    assert result["source_bindings"] == {"learning/observer_lock_upgrade.py":
        hashlib.sha256(Path(upgrade.__file__).read_bytes()).hexdigest()}
    assert result["funding_allowed"] is result["lock_reclaim"] is False
    assert len(json.dumps(result).encode()) < upgrade.MAX_MANIFEST_BYTES
    assert artifacts(s) == before and not s.provider.calls


def test_complete_original_pid_is_published_after_fsync_before_original_work(locked_upgrade, monkeypatch):
    s = locked_upgrade
    actual_link, actual_fsync = os.link, os.fsync
    events = []

    def fsync(fd):
        events.append("fsync")
        return actual_fsync(fd)

    def link(src, dst, **kwargs):
        if Path(dst).name != "worker.lock":
            return actual_link(src, dst, **kwargs)
        assert "fsync" in events
        assert not Path(dst).exists()
        assert read(Path(src)) == {"pid": os.getpid()}
        assert Path(src).stat().st_nlink == 1
        events.append("link")
        return actual_link(src, dst, **kwargs)

    inherited = original.BudgetedQuoteCollector._once

    def once(collector):
        lock = collector.state_dir / "worker.lock"
        assert read(lock) == {"pid": os.getpid()}
        assert lock.stat().st_nlink == 1
        assert not (collector.state_dir / upgrade.STAGE_NAME).exists()
        collector._owns_lock()
        events.append("original")
        return inherited(collector)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(original.BudgetedQuoteCollector, "_once", once)
    result = at(s)
    assert result["status"] == "captured" and result["provider_attempts"] == 1
    assert len(s.provider.calls) == 1
    assert events.index("fsync") < events.index("link") < events.index("original")
    assert result["attempt"]["cache"]["original_capture"]["status"] == "retained"
    assert read(s.collector.state_path)["days"] == {str(s.clock["ms"] // base.DAY_MS): 1}
    assert not (s.collector.state_dir / "worker.lock").exists()
    assert not (s.collector.state_dir / upgrade.STAGE_NAME).exists()


@pytest.mark.parametrize("payload", [b"", b"invalid", b'{}', b'{"pid":999999999}', b'{"pid":1}'])
@pytest.mark.parametrize("name", ["worker.lock", upgrade.STAGE_NAME])
def test_any_existing_lock_or_single_orphan_is_never_reclaimed(locked_upgrade, monkeypatch, payload, name):
    s = locked_upgrade
    path = s.collector.state_dir / name
    path.write_bytes(payload)
    before, inode = artifacts(s), path.stat().st_ino
    monkeypatch.setattr(base, "_pid_alive", lambda *a: pytest.fail("no PID reclamation"))
    monkeypatch.setattr(original.BudgetedQuoteCollector, "_once", lambda *a: pytest.fail("no work while locked"))
    result = at(s)
    assert result["status"] == "locked" and result["provider_attempts"] == 0
    assert path.read_bytes() == payload and path.stat().st_ino == inode
    assert artifacts(s) == before and not s.provider.calls
    assert len(list(s.collector.state_dir.glob(upgrade.STAGE_NAME))) <= 1


@pytest.mark.parametrize("change", ["missing", "own_source", "original_hash", "old_sources", "future", "bool_clock", "policy", "funding"])
def test_missing_or_foreign_declaration_refuses_without_admission(locked_upgrade, change):
    s = locked_upgrade
    before = artifacts(s)
    path = s.collector.lock_manifest_path
    value = read(path)
    if change == "missing":
        path.unlink()
    else:
        if change == "own_source":
            value["source_bindings"]["learning/observer_lock_upgrade.py"] = "0" * 64
        elif change == "original_hash":
            value["original_manifest_sha256"] = "0" * 64
        elif change == "old_sources":
            value["original_source_bindings"]["learning/observer_upgrade.py"] = "0" * 64
        elif change == "future":
            value["prepared_ms"] = s.clock["ms"] + 999_999
        elif change == "bool_clock":
            value["prepared_ms"] = True
        elif change == "policy":
            value["policy"] = "reclaim/1"
        else:
            value["funding_allowed"] = True
        write(path, value)
    assert at(s)["status"] == "partial"
    assert artifacts(s) == before and not s.provider.calls
    assert not (s.collector.state_dir / "worker.lock").exists()
    assert not (s.collector.state_dir / upgrade.STAGE_NAME).exists()


def test_original_manifest_raw_change_is_not_normalized_away(locked_upgrade):
    s = locked_upgrade
    with s.collector.manifest_path.open("ab") as stream:
        stream.write(b"\n")
    before = artifacts(s)
    assert at(s)["status"] == "partial"
    assert artifacts(s) == before and not s.provider.calls


@pytest.mark.parametrize("raw", [b'{"version":NaN}', b'{"version":1,"version":2}', b"x" * 32769],
                         ids=["nonfinite", "duplicate", "over_cap"])
def test_bad_bounded_manifest_json_refuses(locked_upgrade, raw):
    s = locked_upgrade
    s.collector.lock_manifest_path.write_bytes(raw)
    before = artifacts(s)
    assert at(s)["status"] == "partial"
    assert artifacts(s) == before and not s.provider.calls


@pytest.mark.parametrize("point", ["file_fsync", "link", "directory_fsync"])
def test_publication_fault_never_runs_original_or_leaves_empty_lock(locked_upgrade, monkeypatch, point):
    s = locked_upgrade
    before = artifacts(s)
    monkeypatch.setattr(original.BudgetedQuoteCollector, "_once", lambda *a: pytest.fail("no unproved publication"))

    def fail(*a, **k):
        raise OSError("fixture publication failure")

    if point == "file_fsync":
        monkeypatch.setattr(os, "fsync", fail)
    elif point == "link":
        monkeypatch.setattr(os, "link", fail)
    else:
        monkeypatch.setattr(upgrade, "_fsync_directory", fail)
    assert at(s)["status"] == "partial"
    lock = s.collector.state_dir / "worker.lock"
    assert not lock.exists() or read(lock) == {"pid": os.getpid()}
    assert artifacts(s) == before and not s.provider.calls


def test_link_race_never_overwrites_competing_lock(locked_upgrade, monkeypatch):
    s = locked_upgrade
    before = artifacts(s)

    def competing(src, dst, **kwargs):
        Path(dst).write_bytes(b"foreign")
        raise FileExistsError("fixture race")

    monkeypatch.setattr(os, "link", competing)
    result = at(s)
    assert result["status"] == "locked"
    assert (s.collector.state_dir / "worker.lock").read_bytes() == b"foreign"
    assert not (s.collector.state_dir / upgrade.STAGE_NAME).exists()
    assert artifacts(s) == before and not s.provider.calls


@pytest.mark.parametrize("same_bytes", [False, True])
def test_cleanup_does_not_unlink_foreign_replacement_even_with_same_pid_bytes(locked_upgrade, monkeypatch, same_bytes):
    s = locked_upgrade
    replacement = None

    def replace(collector):
        nonlocal replacement
        lock = collector.state_dir / "worker.lock"
        payload = lock.read_bytes() if same_bytes else b"foreign"
        lock.unlink()
        lock.write_bytes(payload)
        replacement = lock.stat().st_ino
        assert not collector._matches(lock, {1})
        return {"status": "fixture", "provider_attempts": 0, "funding_allowed": False}

    monkeypatch.setattr(original.BudgetedQuoteCollector, "_once", replace)
    assert at(s)["status"] == "fixture"
    lock = s.collector.state_dir / "worker.lock"
    assert lock.exists() and lock.stat().st_ino == replacement
    assert not s.provider.calls


def test_unexpected_additional_hardlink_refuses_original_work(locked_upgrade, monkeypatch):
    s = locked_upgrade
    extra = s.collector.state_dir / "foreign-hardlink"

    def extra_link(collector):
        os.link(collector.state_dir / "worker.lock", extra)
        with pytest.raises(ValueError, match="worker_lock_changed"):
            collector._owns_lock()
        return {"status": "fixture", "provider_attempts": 0, "funding_allowed": False}

    monkeypatch.setattr(original.BudgetedQuoteCollector, "_once", extra_link)
    assert at(s)["status"] == "fixture"
    assert extra.exists() and (s.collector.state_dir / "worker.lock").exists()
    assert not s.provider.calls


def test_reentrant_collector_cannot_dispatch_or_touch_counters(locked_upgrade, monkeypatch):
    s = locked_upgrade
    inherited = original.BudgetedQuoteCollector._once

    def recurse(collector):
        contender = upgrade.LockSafeQuoteCollector(s.research, collector.state_dir,
            learning_root=s.root, clock=collector.clock, provider=s.provider)
        before = artifacts(s)
        assert contender.once()["status"] == "locked"
        assert artifacts(s) == before
        return inherited(collector)

    monkeypatch.setattr(original.BudgetedQuoteCollector, "_once", recurse)
    assert at(s)["status"] == "captured"
    assert len(s.provider.calls) == 1
    assert read(s.collector.state_path)["days"][str(s.clock["ms"] // base.DAY_MS)] == 1


def test_same_instance_reentry_keeps_outer_identity_fd_and_cleanup(locked_upgrade, monkeypatch):
    s = locked_upgrade
    inherited = original.BudgetedQuoteCollector._once
    held_fd = None

    def recurse(collector):
        nonlocal held_fd
        before = (collector._lock_identity, collector._lock_raw, collector._lock_fd,
                  copy_manifest := json.dumps(collector._lock_manifest, sort_keys=True))
        held_fd = collector._lock_fd
        before_files = artifacts(s)
        assert collector.once()["status"] == "locked"
        assert (collector._lock_identity, collector._lock_raw, collector._lock_fd,
                json.dumps(collector._lock_manifest, sort_keys=True)) == before
        assert copy_manifest and artifacts(s) == before_files
        collector._owns_lock()
        return inherited(collector)

    monkeypatch.setattr(original.BudgetedQuoteCollector, "_once", recurse)
    assert at(s)["status"] == "captured" and len(s.provider.calls) == 1
    assert not (s.collector.state_dir / "worker.lock").exists()
    assert not s.collector._once_active and s.collector._lock_fd is None
    if held_fd is not None:
        with pytest.raises(OSError):
            os.fstat(held_fd)


def test_threaded_same_instance_contender_cannot_reset_active_publication(locked_upgrade, monkeypatch):
    s = locked_upgrade
    entered, release = threading.Event(), threading.Event()
    results = []

    def work(collector):
        entered.set()
        assert release.wait(5)
        collector._owns_lock()
        return {"status": "fixture", "provider_attempts": 0, "funding_allowed": False}

    def outer():
        try:
            results.append(s.collector.once())
        except BaseException as exc:
            results.append(exc)

    monkeypatch.setattr(original.BudgetedQuoteCollector, "_once", work)
    thread = threading.Thread(target=outer)
    thread.start()
    try:
        assert entered.wait(5)
        before = s.collector._lock_identity, s.collector._lock_raw, s.collector._lock_fd
        assert s.collector.once()["status"] == "locked"
        assert (s.collector._lock_identity, s.collector._lock_raw, s.collector._lock_fd) == before
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and results[0]["status"] == "fixture"
    assert not (s.collector.state_dir / "worker.lock").exists() and not s.provider.calls


@pytest.mark.parametrize("change", ["same_bytes_lock", "own_manifest"])
def test_final_real_provider_boundary_rechecks_lock_and_declaration_after_metadata(locked_upgrade, monkeypatch, change):
    s = locked_upgrade
    in_boundary = False
    original_boundary = original.BudgetedQuoteCollector._token_info
    original_targets = original.BudgetedQuoteCollector._load_targets

    def boundary(collector, *args, **kwargs):
        nonlocal in_boundary
        in_boundary = True
        try:
            return original_boundary(collector, *args, **kwargs)
        finally:
            in_boundary = False

    def targets(collector, now):
        result = original_targets(collector, now)
        if in_boundary:
            if change == "same_bytes_lock":
                lock = collector.state_dir / "worker.lock"
                raw = lock.read_bytes()
                lock.unlink()
                lock.write_bytes(raw)
            else:
                manifest = read(collector.lock_manifest_path)
                manifest["source_bindings"]["learning/observer_lock_upgrade.py"] = "0" * 64
                write(collector.lock_manifest_path, manifest)
        return result

    monkeypatch.setattr(original.BudgetedQuoteCollector, "_token_info", boundary)
    monkeypatch.setattr(original.BudgetedQuoteCollector, "_load_targets", targets)
    # Rebind the original typed shim exactly as the real constructor does.
    s.collector.provider.token_info = s.collector._token_info
    result = at(s)
    # The unmodified base conservatively labels an exception at the provider
    # boundary unknown; this guard proves no typed wrapper invocation occurred.
    assert result["status"] == "unknown" and result["provider_attempts"] == 0
    assert not s.provider.calls
    day = str(s.clock["ms"] // base.DAY_MS)
    assert read(s.collector.state_path)["days"][day] == 1
    assert read(s.collector.ledger_path)["days"][day]["observed_total"] == 1
    if change == "same_bytes_lock":
        assert (s.collector.state_dir / "worker.lock").exists()  # Foreign replacement survives.


def test_unchanged_one_hundred_daily_attempt_cap_and_history_are_not_refilled(locked_upgrade):
    s = locked_upgrade
    state = read(s.collector.state_path)
    day = str(s.clock["ms"] // base.DAY_MS)
    state["days"][day] = 100
    state["updated_ms"] = s.clock["ms"]
    # Explicit fixture admission history, using the original installer only.
    plan = original.prepare_upgrade(state, None, now_ms=s.clock["ms"])
    write(s.collector.state_path, state)
    write(s.collector.ledger_path, plan["ledger"])
    before = artifacts(s)
    result = at(s)
    assert result["provider_attempts"] == 0 and not s.provider.calls
    assert read(s.collector.state_path)["days"][day] == 100
    after = read(s.collector.state_path)
    assert {k: v for k, v in after.items() if k != "updated_ms"} == {
        k: v for k, v in state.items() if k != "updated_ms"}
    assert after["updated_ms"] >= state["updated_ms"]  # Original idle completion clock.
    assert s.collector.manifest_path.read_bytes() == before[s.collector.manifest_path]
    assert read(s.collector.ledger_path)["days"][day]["observed_total"] == 100


def test_interrupted_inherited_admission_is_not_refunded(locked_upgrade, monkeypatch):
    s = locked_upgrade

    def stop(*args, **kwargs):
        raise KeyboardInterrupt("fixture after original debit")

    monkeypatch.setattr(s.provider, "token_info", stop)
    with pytest.raises(KeyboardInterrupt):
        at(s)
    day = str(s.clock["ms"] // base.DAY_MS)
    assert read(s.collector.state_path)["days"][day] == 1
    assert read(s.collector.ledger_path)["days"][day]["observed_total"] == 1
    assert not (s.collector.state_dir / "worker.lock").exists()


def test_prepublication_orphan_is_bounded_and_does_not_authorize_replay(locked_upgrade):
    s = locked_upgrade
    stage = s.collector.state_dir / upgrade.STAGE_NAME
    stage.write_bytes(b"")  # Simulated process death before complete publication.
    before = artifacts(s)
    assert at(s)["status"] == "locked"
    assert not (s.collector.state_dir / "worker.lock").exists()
    assert stage.read_bytes() == b"" and artifacts(s) == before and not s.provider.calls


@pytest.mark.parametrize("name", ["worker.lock", upgrade.STAGE_NAME])
def test_foreign_symlink_is_locked_and_never_followed(locked_upgrade, name):
    s = locked_upgrade
    target = s.root / "foreign-file"
    target.write_bytes(b"foreign")
    path = s.collector.state_dir / name
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("host cannot create symlinks")
    before = artifacts(s)
    assert at(s)["status"] == "locked"
    assert path.is_symlink() and target.read_bytes() == b"foreign"
    assert artifacts(s) == before and not s.provider.calls
