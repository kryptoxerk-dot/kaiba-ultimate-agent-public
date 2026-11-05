"""Temporary-files-only retention, race, budget and ACK controls."""
from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import pytest

from kaiba.learning import gmgn_capture_store as module
from kaiba.learning.gmgn_capture_store import CaptureStore, CaptureStoreError


@pytest.fixture
def store(tmp_path):
    learning = tmp_path / "learning"
    learning.mkdir()
    return CaptureStore(learning / "captures", learning_root=learning)


def test_roundtrip_reference_no_path_duplicate_unchanged_and_owner_modes(store):
    raw = b'{"original":"private","price":"0.000000000000001"}'
    assert store.stats()["files"] == 0 and not store.root.exists()
    assert store.preflight(len(raw))["bytes"] == 0
    ref = store.put(raw)
    assert ref == {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    path = store.root / (ref["sha256"] + ".json")
    before = path.stat()
    assert store.get(ref) == raw and store.put(raw) == ref
    assert path.stat().st_ino == before.st_ino and path.stat().st_mtime_ns == before.st_mtime_ns
    assert store.stats()["files"] == 1 and store.stats()["bytes"] == len(raw)
    if os.name == "posix":
        assert store.root.stat().st_mode & 0o077 == path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("ref", [None, {}, {"sha256": "../secret", "bytes": 1},
    {"sha256": "a" * 64, "bytes": True}, {"sha256": "a" * 64, "bytes": 1, "filename": "foreign"}])
def test_reference_shape_refuses_before_read(store, ref):
    with patch.object(module.os, "open", side_effect=AssertionError("must not open")):
        with pytest.raises(CaptureStoreError, match="capture_reference_invalid"):
            store.get(ref)
    assert not store.root.exists()


def test_nonbytes_empty_and_oversize_refuse_without_creating_store(store):
    for raw in ("private", bytearray(b"x"), b"", b"x" * (module.MAX_CAPSULE_BYTES + 1)):
        with pytest.raises(CaptureStoreError, match="capsule_bytes_invalid"):
            store.put(raw)
    assert not store.root.exists()


def test_global_budget_counts_lock_and_temporary_publication_peak(tmp_path):
    learning = tmp_path / "learning"
    learning.mkdir()
    store = CaptureStore(learning / "bytes", learning_root=learning, max_bytes=module._LOCK_BYTES + 19)
    with pytest.raises(CaptureStoreError, match="store_byte_budget"):
        store.put(b"0123456789")  # Twenty bytes at the hardlink peak + actual lock.
    assert list(store.root.iterdir()) == []
    assert store.put(b"123456789")["bytes"] == 9
    file_store = CaptureStore(learning / "files", learning_root=learning, max_files=2)
    with pytest.raises(CaptureStoreError, match="store_file_budget"):
        file_store.put(b"x")  # Lock + partial + final would be three entries.
    assert list(file_store.root.iterdir()) == []


def test_pending_partial_counts_against_budget_and_is_never_repaired_or_deleted(store):
    raw = b"pending original"
    sha = hashlib.sha256(raw).hexdigest()
    store.root.mkdir(mode=0o700)
    partial = store.root / (sha + ".partial")
    partial.write_bytes(raw)
    partial.chmod(0o600)
    assert store.stats() == {"files": 1, "bytes": len(raw), "max_files": 512,
                             "max_bytes": 64 * 1024 * 1024, "max_capsule_bytes": 128 * 1024}
    before = partial.stat()
    with pytest.raises(CaptureStoreError, match="capsule_partial_ambiguous"):
        store.put(raw)
    assert partial.read_bytes() == raw and partial.stat().st_ino == before.st_ino
    assert not (store.root / (sha + ".json")).exists()


def test_existing_unknown_or_foreign_lock_never_reclaimed(store):
    store.root.mkdir(mode=0o700)
    lock = store.root / ".store.lock"
    lock.write_bytes(b"unknown or dead owner")
    lock.chmod(0o600)
    before = lock.read_bytes()
    with pytest.raises(CaptureStoreError, match="store_busy_or_ambiguous"):
        store.put(b"x")
    assert lock.read_bytes() == before and store.stats()["files"] == 1


def test_real_contention_has_single_exclusive_owner_no_double_publication(store, monkeypatch):
    barrier = Barrier(2)
    original = store._sync_directory
    reached = [False]

    def block_first():
        if not reached[0]:
            reached[0] = True
            barrier.wait(timeout=5)
            barrier.wait(timeout=5)
        original()

    monkeypatch.setattr(store, "_sync_directory", block_first)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(store.put, b"one")
        barrier.wait(timeout=5)
        with pytest.raises(CaptureStoreError, match="store_busy_or_ambiguous"):
            store.put(b"two")
        barrier.wait(timeout=5)
        ref = first.result(timeout=5)
    assert store.get(ref) == b"one" and store.stats()["files"] == 1


def test_hash_mutation_and_wrong_size_are_unknown_not_returned(store):
    ref = store.put(b"original")
    path = store.root / (ref["sha256"] + ".json")
    path.write_bytes(b"mutated!")  # Same length; size alone cannot detect this.
    with pytest.raises(CaptureStoreError, match="capture_hash_or_size_changed"):
        store.get(ref)
    with pytest.raises(CaptureStoreError, match="capture_hash_or_size_changed"):
        store.put(b"original")
    assert path.read_bytes() == b"mutated!"


def test_get_is_exact_file_read_without_inventory_scan(store):
    ref = store.put(b"body")
    with patch.object(module.os, "scandir", side_effect=AssertionError("get must not scan")):
        assert store.get(ref) == b"body"


def test_each_new_directory_publishes_parent_before_lock_or_capsule(tmp_path, monkeypatch):
    learning = tmp_path / "learning"
    learning.mkdir()
    store = CaptureStore(learning / "new-namespace" / "captures", learning_root=learning)
    original, synced = store._sync_parent, []

    def sync(parent, expected):
        assert not (store.root / ".store.lock").exists()
        assert (parent.stat().st_dev, parent.stat().st_ino) == expected
        synced.append(parent)
        original(parent, expected)  # Real directory fsync on POSIX; explicit nt limitation.

    monkeypatch.setattr(store, "_sync_parent", sync)
    ref = store.put(b"body")
    assert synced == [learning, learning / "new-namespace"] and store.get(ref) == b"body"


def test_readback_mutation_is_refused_before_publish(store, monkeypatch):
    original = store._read

    def changed(path, limit, **kw):
        if path.suffix == ".partial":
            path.write_bytes(b"wrong")
        return original(path, limit, **kw)

    monkeypatch.setattr(store, "_read", changed)
    with pytest.raises(CaptureStoreError, match="store_bytes_conflict"):
        store.put(b"first")
    assert not list(store.root.glob("*.json"))
    assert next(store.root.glob("*.partial")).read_bytes() == b"wrong"


def test_final_name_CAS_never_overwrites_racing_foreign_file(store, monkeypatch):
    original = module.os.link

    def race(source, destination, **kw):
        destination.write_bytes(b"foreign")
        destination.chmod(0o600)
        return original(source, destination, **kw)

    monkeypatch.setattr(module.os, "link", race)
    with pytest.raises(CaptureStoreError, match="storage_io_unknown"):
        store.put(b"our capsule")
    assert next(store.root.glob("*.json")).read_bytes() == b"foreign"


def test_failed_lock_fsync_ACK_is_consumed_never_replayed(store, monkeypatch):
    original = module.os.fsync
    failed = [False]

    def fail_once(fd):
        if not failed[0]:
            failed[0] = True
            raise OSError("private fsync ACK failure")
        original(fd)

    monkeypatch.setattr(module.os, "fsync", fail_once)
    with pytest.raises(CaptureStoreError, match="storage_io_unknown"):
        store.put(b"body")
    assert (store.root / ".store.lock").is_file()
    with pytest.raises(CaptureStoreError, match="store_busy_or_ambiguous"):
        store.put(b"body")
    assert not list(store.root.glob("*.json"))


def test_failed_publication_ACK_keeps_committed_original_immutable(store, monkeypatch):
    original = store._sync_directory
    calls = [0]

    def fail_after_publish():
        calls[0] += 1
        if calls[0] == 2:
            raise OSError("private publication ACK failure")
        original()

    monkeypatch.setattr(store, "_sync_directory", fail_after_publish)
    with pytest.raises(CaptureStoreError, match="storage_io_unknown"):
        store.put(b"body")
    final = next(store.root.glob("*.json"))
    before = final.stat()
    ref = {"sha256": hashlib.sha256(b"body").hexdigest(), "bytes": 4}
    assert store.get(ref) == b"body" and store.put(b"body") == ref
    assert final.stat().st_ino == before.st_ino and final.stat().st_mtime_ns == before.st_mtime_ns


def test_lifetime_lock_replacement_refuses_before_capsule_write_and_keeps_foreign(store, monkeypatch):
    original = store._inventory
    calls = [0]

    def steal():
        result = original()
        calls[0] += 1
        if calls[0] == 2:
            lock = store.root / ".store.lock"
            lock.unlink()
            lock.write_bytes(b"foreign lock")
            lock.chmod(0o600)
        return result

    monkeypatch.setattr(store, "_inventory", steal)
    with pytest.raises(CaptureStoreError, match="store_bytes_conflict|store_lock_changed"):
        store.put(b"body")
    assert (store.root / ".store.lock").read_bytes() == b"foreign lock"
    assert not list(store.root.glob("*.partial")) and not list(store.root.glob("*.json"))


def test_path_escape_and_foreign_entry_refused(tmp_path):
    learning = tmp_path / "learning"
    learning.mkdir()
    with pytest.raises(CaptureStoreError, match="store_path_refused"):
        CaptureStore(tmp_path / "outside", learning_root=learning)
    with pytest.raises(CaptureStoreError, match="store_path_refused"):
        CaptureStore(learning / "not-created" / ".." / ".." / "outside", learning_root=learning)
    assert not (learning / "not-created").exists() and not (tmp_path / "outside").exists()
    store = CaptureStore(learning / "captures", learning_root=learning)
    store.root.mkdir(mode=0o700)
    (store.root / "foreign.txt").write_text("private")
    with pytest.raises(CaptureStoreError, match="store_entry_refused"):
        store.put(b"body")


def test_real_directory_symlink_refused_without_touching_target(tmp_path):
    learning = tmp_path / "learning"
    learning.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = learning / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("host does not grant symlink creation")
    with pytest.raises(CaptureStoreError, match="store_path_refused"):
        CaptureStore(link / "child", learning_root=learning)
    assert not (outside / "child").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode enforcement; Windows uses inherited ACL")
def test_existing_unsafe_permissions_refused_without_chmod(store):
    store.root.mkdir(mode=0o755)
    with pytest.raises(CaptureStoreError, match="owner_or_permissions_refused"):
        store.put(b"body")
    assert store.root.stat().st_mode & 0o777 == 0o755


def test_removed_budget_and_hash_guards_are_detected(tmp_path):
    source = Path(module.__file__).read_text()
    budget = 'snapshot["bytes"] + size <= self.max_bytes'
    digest = 'hashlib.sha256(raw).hexdigest() == ref["sha256"]'
    assert source.count(budget) == source.count(digest) == 1
    learning = tmp_path / "learning"
    learning.mkdir()
    for label, changed in (("budget", source.replace(budget, "True", 1)),
                           ("hash", source.replace(digest, "True", 1))):
        ns = {"__name__": "guard_removal"}
        exec(compile(changed, label + "-removed", "exec"), ns)
        if label == "budget":
            mutant = ns["CaptureStore"](learning / label, learning_root=learning,
                                         max_bytes=module._LOCK_BYTES + 3)
            # With the guard present the actual two-byte capsule is refused at peak.
            baseline = CaptureStore(learning / "baseline", learning_root=learning,
                                    max_bytes=module._LOCK_BYTES + 3)
            with pytest.raises(CaptureStoreError, match="store_byte_budget"):
                baseline.put(b"xx")
            assert mutant.put(b"xx")["bytes"] == 2
        else:
            mutant = ns["CaptureStore"](learning / label, learning_root=learning)
            ref = mutant.put(b"first")
            (mutant.root / (ref["sha256"] + ".json")).write_bytes(b"other")
            assert mutant.get(ref) == b"other"  # Same intact negative fails under removal.
