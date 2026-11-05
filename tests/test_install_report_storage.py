"""Installer transaction boundaries and a genuine synthetic historical transition."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy import install_report_storage as installer
from kaiba.core import config
from kaiba.learning import allocation_runtime as runtime
from kaiba.learning import observer_upgrade as observer
from kaiba.learning import prospective_quotes, quote_budget
from tests.test_allocation_runtime import packed, snapshot
from tests.test_report_storage_upgrade import provision_report_runtime


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("transport forbidden"))
    # Installer prepends its disposable root. Do not leak that into later tests.
    monkeypatch.setattr(sys, "path", list(sys.path))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def bundle_for(root, proposed):
    state, obs = root / "data/learning/autolearn", root / "data/learning/prospective-quotes"
    return {"commit": "a" * 40, "sources": {
        name: {"before": sha((root / name).read_bytes()) if (root / name).exists() else None,
               "after": sha(raw), "base64": base64.b64encode(raw).decode()}
        for name, raw in proposed.items()},
        "inputs": {n: sha((state / n).read_bytes()) for n in installer.INPUTS},
        "observer": {n: sha((obs / n).read_bytes()) for n in installer.OBSERVER}}


def environment(root, monkeypatch):
    settings = SimpleNamespace(kaiba_data_dir=root / "data", db_path=root / "data/kaiba.db")
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(prospective_quotes, "get_settings", lambda: settings)


@pytest.fixture
def staged(tmp_path, monkeypatch):
    root = tmp_path / "install"
    for name in installer.CHANGED:
        write(root / name, b"ORIGINAL = True\n")
    for folder, names in (("autolearn", installer.INPUTS), ("prospective-quotes", installer.OBSERVER)):
        for name in names:
            write(root / "data/learning" / folder / name, packed({"fixture": name}))
    proposed = {name: b"REVIEWED = True\n" for name in installer.SOURCES}
    environment(root, monkeypatch)
    return root, bundle_for(root, proposed)


def test_default_preflight_checks_bytes_without_creating_locks_backups_or_sources(staged):
    root, bundle = staged
    before = snapshot(root)
    result = installer.install(root, bundle)
    assert result["status"] == "preflight_only" and result["writes"] == 0
    assert result["input_hashes_match"] is True
    assert result["full_recipe_validation"] == "performed during locked apply"
    assert snapshot(root) == before
    assert not any(root.rglob("worker.lock"))


@pytest.mark.parametrize("field", ["source", "parent", "outbox", "observer", "ledger", "payload"])
def test_stale_bundle_or_tampered_payload_refused_before_writes(staged, field):
    root, bundle = staged
    target = {"source": root / installer.CHANGED[0],
        "parent": root / "data/learning/autolearn/state.json",
        "outbox": root / "data/learning/autolearn/economic-work.json",
        "observer": root / "data/learning/prospective-quotes/state.json",
        "ledger": root / "data/learning/prospective-quotes/quote-budget-ledger.json"}
    if field == "payload":
        bundle["sources"][installer.CHANGED[0]]["base64"] = base64.b64encode(b"TAMPERED = True\n").decode()
    else:
        target[field].write_bytes(target[field].read_bytes() + b"\n")
    before = snapshot(root)
    with pytest.raises(ValueError, match="source_baseline_changed|parent_inputs_changed|observer_inputs_changed|source_payload_hash"):
        installer.install(root, bundle, apply=True)
    assert snapshot(root) == before


@pytest.mark.parametrize("folder", ["autolearn", "prospective-quotes"])
def test_each_existing_worker_lock_refuses_without_reclaiming_or_installing(staged, folder):
    root, bundle = staged
    foreign = root / "data/learning" / folder / "worker.lock"
    foreign.write_bytes(packed({"pid": 999999, "fixture": "do not reclaim"}))
    before = snapshot(root)
    with pytest.raises(FileExistsError):
        installer.install(root, bundle, apply=True)
    assert snapshot(root) == before
    assert foreign.exists()


@pytest.mark.parametrize("kind", ["data", "database"])
def test_wrong_environment_refuses_before_even_a_lock(staged, monkeypatch, kind):
    root, bundle = staged
    settings = SimpleNamespace(kaiba_data_dir=root / "data", db_path=root / "data/kaiba.db")
    if kind == "data":
        settings.kaiba_data_dir = root / "different-data"
    else:
        settings.db_path = root / "different.db"
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    before = snapshot(root)
    with pytest.raises(ValueError, match="configured_.*_mismatch"):
        installer.install(root, bundle, apply=True)
    assert snapshot(root) == before


def test_second_preflight_catches_change_between_preflight_and_locks(staged, monkeypatch):
    root, bundle = staged
    original = installer.write_once
    target = root / "data/learning/autolearn/state.json"
    changed = target.read_bytes() + b"\n"

    def race(path, raw, *args, **kwargs):
        original(path, raw, *args, **kwargs)
        if Path(path) == root / "data/learning/prospective-quotes/worker.lock":
            target.write_bytes(changed)

    monkeypatch.setattr(installer, "write_once", race)
    before = snapshot(root)
    with pytest.raises(ValueError, match="parent_inputs_changed"):
        installer.install(root, bundle, apply=True)
    before[str(target.relative_to(root))] = sha(changed)
    assert snapshot(root) == before


def test_atomic_replace_checks_late_cas_and_keeps_foreign_data(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_bytes(b"original")
    original = installer.write_once

    def race(path, raw, *args, **kwargs):
        original(path, raw, *args, **kwargs)
        target.write_bytes(b"foreign replacement")

    monkeypatch.setattr(installer, "write_once", race)
    with pytest.raises(ValueError, match="late_replace_CAS"):
        installer.replace(target, b"proposed", expected=b"original")
    assert target.read_bytes() == b"foreign replacement"
    assert target.with_name("state.json.report-storage.partial").read_bytes() == b"proposed"


def test_foreign_replacement_lock_is_not_unlinked_during_failure(staged, monkeypatch):
    root, bundle = staged
    main = root / "data/learning/autolearn/worker.lock"
    other = root / "data/learning/prospective-quotes/worker.lock"
    other.write_bytes(b'{"pid":999999}')
    original = installer.write_once

    def race(path, raw, *args, **kwargs):
        if Path(path) == other:
            main.unlink()
            main.write_bytes(b'{"pid":888888,"foreign":true}')
        return original(path, raw, *args, **kwargs)

    monkeypatch.setattr(installer, "write_once", race)
    with pytest.raises(FileExistsError):
        installer.install(root, bundle, apply=True)
    assert main.read_bytes() == b'{"pid":888888,"foreign":true}'
    assert other.read_bytes() == b'{"pid":999999}'
    assert not (main.parent / "research-allocation" / installer.DIRECTORY).exists()


def test_partial_source_install_retains_intent_and_originals_and_refuses_retry(staged, monkeypatch):
    root, bundle = staged
    state = root / "data/learning/autolearn"
    own = state / "research-allocation"
    for name in ("metadata.json", "authority.json", "commit-receipt.json", "receipt.json", "new-books.json"):
        write(own / name, packed({"fixture": name}))
    original_parent, original_outbox = (state / "state.json").read_bytes(), (state / "economic-work.json").read_bytes()
    original = installer.replace

    def interrupted(path, raw, **kwargs):
        original(path, raw, **kwargs)
        if Path(path) == root / installer.CHANGED[0]:
            raise RuntimeError("fixture interruption after source publication")

    monkeypatch.setattr(installer, "replace", interrupted)
    with pytest.raises(RuntimeError, match="fixture interruption"):
        installer.install(root, bundle, apply=True)
    directory = own / installer.DIRECTORY
    assert (directory / "install-intent.json").is_file()
    assert (directory / "state.before.json").read_bytes() == original_parent
    assert (directory / "source34" / installer.CHANGED[0]).read_bytes() == b"ORIGINAL = True\n"
    assert (root / installer.CHANGED[0]).read_bytes() == b"REVIEWED = True\n"
    assert (root / installer.CHANGED[1]).read_bytes() == b"ORIGINAL = True\n"
    assert (state / "state.json").read_bytes() == original_parent
    assert (state / "economic-work.json").read_bytes() == original_outbox
    assert not (directory / "install-complete.json").exists()
    assert not any(root.rglob("worker.lock"))
    before = snapshot(root)
    with pytest.raises(ValueError, match="existing_installation_requires_review"):
        installer.install(root, bundle, apply=True)
    assert snapshot(root) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership modes are not modeled by Windows chmod")
def test_replace_preserves_private_mode_and_sets_requested_source_mode(tmp_path):
    target = tmp_path / "state.json"
    target.write_bytes(b"original")
    target.chmod(0o600)
    previous_umask = os.umask(0o077)
    try:
        installer.replace(target, b"next", expected=b"original")
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        source = tmp_path / "source.py"
        installer.replace(source, b"REVIEWED = True\n", expected=None, mode=0o644)
        assert stat.S_IMODE(source.stat().st_mode) == 0o644
        source.chmod(0o640)
        installer.replace(source, b"REVIEWED = 2\n", expected=b"REVIEWED = True\n")
        assert stat.S_IMODE(source.stat().st_mode) == 0o640
    finally:
        os.umask(previous_umask)


def genuine_staged(tmp_path, monkeypatch):
    value = provision_report_runtime(tmp_path / "history", monkeypatch, install=False)
    root = value["code"]
    shutil.copytree(value["learning"], root / "data/learning")
    state, obs = root / "data/learning/autolearn", root / "data/learning/prospective-quotes"
    parent = json.loads((state / "state.json").read_bytes())
    parent["pending_admissions"] = [{"id": "fixture-consumed", "used": True}]
    write(state / "state.json", packed(parent))
    write(state / "economic-work.json", value["migration_artifacts"]["economic-work.original.json"])
    proposed = {name: (root / name).read_bytes() for name in installer.SOURCES}
    workspace = Path(__file__).resolve().parents[1]
    for name in observer.source_bindings():
        path = root / "kaiba" / name
        if not path.exists():
            write(path, (workspace / "kaiba" / name).read_bytes().replace(b"\r\n", b"\n"))
    monkeypatch.setattr(observer, "__file__", str(root / "kaiba/learning/observer_upgrade.py"))
    current_bindings = observer.source_bindings()
    old_bindings = {name: digest for name, digest in current_bindings.items() if "kaiba/" + name not in installer.ADDED}
    for name in installer.CHANGED:
        old_bindings[name.removeprefix("kaiba/")] = sha(value["old_sources34"][name])
    now = value["clock"]["ms"]
    day, token = str(now // prospective_quotes.DAY_MS), "0x" + "a" * 40
    target = {"kind": "pending", "chain": "bsc", "token": token, "signal_ms": now - 1000}
    attempt = {"attempt_id": quote_budget._digest([day, 19, "bsc:" + token]), "target": target,
               "admitted_ms": now, "status": "admitted"}
    observer_state = {"version": prospective_quotes.VERSION, "policy": prospective_quotes.POLICY,
        "days": {day: 19}, "tokens": {"bsc:" + token: now}, "attempts": [attempt],
        "retry_after_ms": now + 60_000, "updated_ms": now}
    plan = observer.prepare_upgrade(observer_state, value["old_context"].snapshot_token, now_ms=now)
    plan["manifest"]["source_bindings"] = old_bindings
    for name, obj in (("state.json", observer_state), ("quote-budget-ledger.json", plan["ledger"]),
                      ("observer-upgrade.json", plan["manifest"])):
        write(obs / name, packed(obj))
    for name in installer.CHANGED:
        (root / name).write_bytes(value["old_sources34"][name])
    for name in installer.ADDED:
        (root / name).unlink()
    (state / "worker.lock").unlink()
    monkeypatch.setattr(installer.time, "time", lambda: now / 1000)
    environment(root, monkeypatch)
    return root, bundle_for(root, proposed), parent, value


def test_full_install_preserves_authority_books_admissions_and_observer_ledger(tmp_path, monkeypatch):
    root, bundle, before_parent, value = genuine_staged(tmp_path, monkeypatch)
    state, obs = root / "data/learning/autolearn", root / "data/learning/prospective-quotes"
    before, original_observer = snapshot(root), {n: (obs / n).read_bytes() for n in installer.OBSERVER}
    original_state = (state / "state.json").read_bytes()
    original_outbox = (state / "economic-work.json").read_bytes()
    assert installer.install(root, bundle)["writes"] == 0
    connections, original_connect = [], sqlite3.connect

    def paper_only(database, *args, **kwargs):
        normalized = str(database).replace("\\", "/")
        assert "/data/learning/autolearn/paper/" in normalized
        assert "?mode=ro" in normalized and kwargs.get("uri") is True
        connections.append(normalized)
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", paper_only)
    result = installer.install(root, bundle, apply=True)
    assert result["status"] == "installed"
    assert result["parent_fields_preserved"] is True
    assert result["provider_calls"] == result["canonical_database_queries"] == 0
    assert result["existing_paper_books_validated_read_only"] is True and connections
    assert result["funding_allowed"] is False
    after_parent = json.loads((state / "state.json").read_bytes())
    assert {k: v for k, v in after_parent.items() if k != "report_storage_upgrade"} == before_parent
    directory = state / "research-allocation" / installer.DIRECTORY
    assert (directory / "state.before.json").read_bytes() == original_state
    assert not (directory / "migration").exists()
    assert (state / "economic-work.json").read_bytes() == original_outbox
    for name, raw in original_observer.items():
        assert (directory / "observer-original" / name).read_bytes() == raw
        if name != "observer-upgrade.json":
            assert (obs / name).read_bytes() == raw
    old_manifest, new_manifest = json.loads(original_observer["observer-upgrade.json"]), json.loads((obs / "observer-upgrade.json").read_bytes())
    assert {k for k in old_manifest if old_manifest[k] != new_manifest[k]} == {"source_bindings", "allocation_snapshot_token"}
    changed = {"data/learning/autolearn/state.json", "data/learning/prospective-quotes/observer-upgrade.json", *installer.CHANGED}
    after = snapshot(root)
    assert {name: (digest, after.get(name)) for name, digest in before.items()
            if name.replace("\\", "/") not in changed and after.get(name) != digest} == {}
    assert not (state / "worker.lock").exists() and not (obs / "worker.lock").exists()
    ctx = runtime.load_context(state, learning_root=root / "data/learning", now_ms=value["clock"]["ms"],
                               clock=lambda: value["clock"]["ms"] / 1000)
    assert ctx.report_storage_upgrade is not None and ctx.journal_storage_upgrade is not None
    assert ctx.authority["activation_ms"] == value["activation"]
    assert len(ctx.leased_pairs()) == 12 and len(ctx.retired_history()) == 10
    assert sum(len(m["hypotheses"]) for m in ctx.manifests()) == 22
    assert json.loads((obs / "quote-budget-ledger.json").read_bytes())["days"][str(value["clock"]["ms"] // prospective_quotes.DAY_MS)]["observed_total"] == 19
    before_retry = snapshot(root)
    with pytest.raises(ValueError, match="existing_installation_requires_review"):
        installer.install(root, bundle, apply=True)
    assert snapshot(root) == before_retry
