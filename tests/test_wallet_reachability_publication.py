"""Codex WALLET-REACHABILITY: real publication boundary, disposable files only.

The projector, manifest/snapshot validation and lock ownership are boundary doubles.
Actual _project precommit/read/CAS and publish write/fsync/link/replace execute.
These tests exercise publication ordering, not full Context or original acquisition.
"""
from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from kaiba.learning import wallet_reachability_worker as worker


@pytest.fixture(autouse=True)
def no_transport(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("publication fixture attempted DB/network/process operation")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def encoded(value):
    return worker.original._encoded(value) + b"\n"


def replace_file(path, raw):
    replacement = path.with_name(path.name + ".fixture-replacement")
    replacement.write_bytes(raw)
    os.replace(replacement, path)


@pytest.fixture
def projection_case(tmp_path, monkeypatch):
    root = tmp_path / "learning"
    own = root / worker.DIRECTORY
    own.mkdir(parents=True)
    parent = {"last_wallet_review_ms": 10, "wallet_review_state": {}}
    state_path = root / "state.json"
    state_path.write_bytes(encoded(parent))
    declaration = {"genesis_sha256": "a" * 64, "fixture_policy": 1}
    declaration_path = own / "declaration.json"
    declaration_path.write_bytes(encoded(declaration))
    store = {"updated_ms": 10, "store_sha256": "b" * 64, "engine_state": {},
             "diagnostics": [], "resolutions": [], "quota": {"attempts": []}}
    captured = {"state.json": (state_path.read_bytes(), parent),
                "wallet-maintenance/journal.json": (None, None),
                "latest-wallet-review.json": (None, None)}
    obj = object.__new__(worker.ReachabilityMaintenance)
    obj.root, obj.reachability_root, obj.clock = root, own, lambda: 0.02
    lock = {"owned": True}

    def manifest(now):
        return worker.read(declaration_path, 128 * 1024)

    def snapshot(now, manifest_value):
        return captured, parent, store

    def owns_lock():
        worker.require(lock["owned"], "fixture_lock_changed")

    obj._manifest, obj._snapshot, obj._owns_lock = manifest, snapshot, owns_lock
    monkeypatch.setattr(worker.projector, "project", lambda *args, **kwargs: {
        "status": "unknown", "fixture": True, "copy_qualified": False})
    return SimpleNamespace(obj=obj, root=root, own=own, parent=parent, state=state_path,
        declaration=declaration_path, lock=lock, output=own / "report.json",
        stage=own / "report.json.partial", result={"status": "not_due", "source_queries": 0})


def project(case, now=20):
    return case.obj._project(case.result, now)


def seed_prior(case, monkeypatch):
    # Setup ONLY: Windows cannot rename a stage while its identity FD is held.
    # Populate the valid prior envelope using actual _project, then restore the
    # real publisher before every injected boundary fault below.
    def fixture_publication(path, value, **kwargs):
        path.write_bytes(encoded(value))

    with monkeypatch.context() as setup_patch:
        setup_patch.setattr(worker, "publish", fixture_publication)
        assert project(case)["status"] == "published"
    return case.output.read_bytes()


def after_stage_fsync(case, monkeypatch, mutate):
    actual, events = worker.os.fsync, []

    def fsync(fd):
        actual(fd)
        if case.stage.exists():
            staged, opened = case.stage.stat(), os.fstat(fd)
            if (staged.st_dev, staged.st_ino) == (opened.st_dev, opened.st_ino):
                events.append("stage_fsynced")
                mutate()

    monkeypatch.setattr(worker.os, "fsync", fsync)
    return events


@pytest.mark.skipif(os.name == "nt", reason="production POSIX publisher holds the stage FD across rename")
def test_actual_publication_is_complete_and_unchanged_evidence_does_not_rewrite(projection_case, monkeypatch):
    c = projection_case
    assert project(c)["status"] == "published"
    raw = c.output.read_bytes()
    value = worker.read(c.output, worker.MAX_REPORT_BYTES)[1]
    assert value["original_refresh_complete"] is False
    assert value["original_retained_review_complete"] is False
    assert value["observation_scope"] == "retained_unlinked_snapshot"
    assert value["extra_source_queries"] == value["provider_calls"] == 0
    assert not c.stage.exists()

    def forbidden_fsync(fd):
        pytest.fail("unchanged report must not be staged or republished")

    monkeypatch.setattr(worker.os, "fsync", forbidden_fsync)
    assert project(c, now=30)["status"] == "unchanged"
    assert c.output.read_bytes() == raw


@pytest.mark.parametrize("prior", [False, True])
@pytest.mark.parametrize("fault", ["input", "manifest", "lock"])
def test_post_fsync_input_manifest_or_lock_change_blocks_publication(projection_case, monkeypatch, prior, fault):
    c = projection_case
    if prior:
        old = seed_prior(c, monkeypatch)
        c.result = {"status": "deferred", "source_queries": 0}  # Force a new report generation.
    else:
        old = None

    def mutate():
        if fault == "input":
            c.state.write_bytes(encoded({**c.parent, "fixture_generation": 2}))
        elif fault == "manifest":
            c.declaration.write_bytes(encoded({"genesis_sha256": "a" * 64, "fixture_policy": 2}))
        else:
            c.lock["owned"] = False

    events = after_stage_fsync(c, monkeypatch, mutate)
    with pytest.raises(ValueError, match="input_changed|manifest_changed|fixture_lock_changed"):
        project(c, now=30)
    assert events == ["stage_fsynced"]
    assert c.stage.is_file(), "failed staged publication remains for explicit inspection"
    assert (c.output.read_bytes() if c.output.exists() else None) == old


@pytest.mark.parametrize("fault", ["appeared", "same_bytes_new_inode", "changed_bytes"])
def test_post_fsync_foreign_prior_report_is_preserved(projection_case, monkeypatch, fault):
    c = projection_case
    if fault != "appeared":
        old = seed_prior(c, monkeypatch)
        c.result = {"status": "deferred", "source_queries": 0}
    else:
        old = None
    replacement = old if fault == "same_bytes_new_inode" else encoded({"foreign_fixture": True})

    def mutate():
        replace_file(c.output, replacement)

    events = after_stage_fsync(c, monkeypatch, mutate)
    with pytest.raises(ValueError, match="prior_report_appeared|prior_report_changed"):
        project(c, now=30)
    assert events == ["stage_fsynced"]
    assert c.output.read_bytes() == replacement
    assert c.stage.exists()


@pytest.mark.parametrize("fault", ["bytes", "extra_link"])
def test_post_fsync_staged_bytes_or_link_change_blocks_replace(projection_case, monkeypatch, fault):
    c = projection_case
    old = seed_prior(c, monkeypatch)
    c.result = {"status": "deferred", "source_queries": 0}
    extra = c.own / "fixture-extra-link"

    def mutate():
        if fault == "bytes":
            c.stage.write_bytes(encoded({"foreign_fixture": True}))
        else:
            os.link(c.stage, extra)

    events = after_stage_fsync(c, monkeypatch, mutate)
    with pytest.raises(ValueError, match="publication_stage_changed|regular_single_link"):
        project(c, now=30)
    assert events == ["stage_fsynced"]
    assert c.output.read_bytes() == old
    assert c.stage.exists()
    if fault == "extra_link":
        assert extra.read_bytes() == c.stage.read_bytes()


def test_crash_after_stage_fsync_keeps_original_and_never_reclaims_partial(projection_case, monkeypatch):
    c = projection_case
    old = seed_prior(c, monkeypatch)
    c.result = {"status": "deferred", "source_queries": 0}

    class FixtureCrash(BaseException):
        pass

    def crash():
        raise FixtureCrash()

    events = after_stage_fsync(c, monkeypatch, crash)
    with pytest.raises(FixtureCrash):
        project(c, now=30)
    assert events == ["stage_fsynced"]
    partial = c.stage.read_bytes()
    assert partial and c.output.read_bytes() == old
    with pytest.raises(FileExistsError):
        project(c, now=40)
    assert c.stage.read_bytes() == partial and c.output.read_bytes() == old


def test_precommit_failure_before_staging_creates_no_output(projection_case, monkeypatch):
    c = projection_case

    def changed_projector(*args, **kwargs):
        c.state.write_bytes(encoded({**c.parent, "fixture_generation": 2}))
        return {"status": "unknown", "fixture": True}

    monkeypatch.setattr(worker.projector, "project", changed_projector)
    with pytest.raises(ValueError, match="input_changed"):
        project(c)
    assert not c.output.exists() and not c.stage.exists()


@pytest.mark.skipif(os.name == "nt", reason="production POSIX publisher holds the stage FD across rename")
def test_publication_callback_runs_after_stage_fsync(tmp_path):
    path = tmp_path / "projection.json"
    value = {"fixture": True}
    seen = []

    def precommit():
        staged = path.with_name(path.name + ".partial")
        seen.append(staged.read_bytes())
        assert not path.exists()

    worker.publish(path, value, precommit=precommit)
    assert seen == [encoded(value)]
    assert path.read_bytes() == encoded(value)


@pytest.mark.skipif(os.name != "posix", reason="exclusive publication holds its stage FD through link and unlink")
def test_exclusive_publication_keeps_only_exact_single_link_destination(tmp_path, monkeypatch):
    path = tmp_path / "declaration.json"
    stage = path.with_name(path.name + ".partial")
    value = {"fixture": "A"}
    actual_sync, boundaries = worker.sync, []

    def inspect_directory_sync(directory):
        actual_sync(directory)
        if stage.exists():
            current, staged = path.stat(), stage.stat()
            assert (current.st_dev, current.st_ino) == (staged.st_dev, staged.st_ino)
            assert current.st_nlink == staged.st_nlink == 2
            boundaries.append("linked_pair")
        else:
            assert path.stat().st_nlink == 1
            boundaries.append("single_destination")

    monkeypatch.setattr(worker, "sync", inspect_directory_sync)
    worker.publish(path, value, exclusive=True)
    assert boundaries == ["linked_pair", "single_destination"]
    assert path.read_bytes() == encoded(value)
    assert path.stat().st_nlink == 1 and not stage.exists()


@pytest.mark.skipif(os.name != "posix", reason="exclusive publication holds its stage FD through link and unlink")
@pytest.mark.parametrize("fault", ["stage_inode", "destination_inode", "extra_link", "bytes"])
def test_exclusive_publication_rechecks_both_links_after_directory_fsync(tmp_path, monkeypatch, fault):
    path = tmp_path / "declaration.json"
    stage = path.with_name(path.name + ".partial")
    extra = tmp_path / "foreign-hardlink"
    raw, foreign = encoded({"fixture": "A"}), encoded({"fixture": "B"})
    actual_sync, mutations, original_identity = worker.sync, [], []

    def mutate_after_directory_fsync(directory):
        actual_sync(directory)
        if mutations:
            return
        staged, destination = stage.stat(), path.stat()
        assert (staged.st_dev, staged.st_ino) == (destination.st_dev, destination.st_ino)
        assert staged.st_nlink == destination.st_nlink == 2
        original_identity.append((staged.st_dev, staged.st_ino))
        if fault == "stage_inode":
            replace_file(stage, foreign)
        elif fault == "destination_inode":
            replace_file(path, foreign)
        elif fault == "extra_link":
            os.link(stage, extra)
        else:
            stage.write_bytes(foreign)  # Same inode and byte length; both links change.
        mutations.append(fault)

    monkeypatch.setattr(worker, "sync", mutate_after_directory_fsync)
    with pytest.raises(ValueError):
        worker.publish(path, {"fixture": "A"}, exclusive=True)
    assert mutations == [fault]
    assert stage.is_file() and path.is_file(), "refusal must never unlink either changed publication name"
    assert stage.read_bytes() == (foreign if fault in {"stage_inode", "bytes"} else raw)
    assert path.read_bytes() == (foreign if fault in {"destination_inode", "bytes"} else raw)
    if fault == "stage_inode":
        assert (path.stat().st_dev, path.stat().st_ino) == original_identity[0]
        assert (stage.stat().st_dev, stage.stat().st_ino) != original_identity[0]
    elif fault == "destination_inode":
        assert (stage.stat().st_dev, stage.stat().st_ino) == original_identity[0]
        assert (path.stat().st_dev, path.stat().st_ino) != original_identity[0]
    elif fault == "extra_link":
        assert extra.read_bytes() == raw
        assert stage.stat().st_nlink == path.stat().st_nlink == extra.stat().st_nlink == 3
