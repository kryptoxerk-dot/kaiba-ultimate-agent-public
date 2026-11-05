"""Actual disposable books and shared read-only allocation authority."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from kaiba.learning import allocation_runtime as runtime
from tests.test_research_allocation import rig


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("external transport forbidden"))


def packed(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def provision_runtime(value):
    """Explicit test installer. Runtime itself never creates these artifacts."""
    root = value["books"].parent
    parent = {"version": "autolearn/2", "campaign_files": [value["manifest"]["campaign_id"] + ".json"],
        "assessment": copy.deepcopy(value["receipt"]["legacy_assessments"]), "pending_lessons": [],
        "selected_ms": value["t0"], "decision_cursor": 0, "source_initialized": False,
        "last_wallet_review_ms": value["clock"]["ms"], "last_agent_review_ms": value["clock"]["ms"]}
    own = root / runtime.DIRECTORY
    own.mkdir()
    receipt_raw = packed(value["receipt"])
    authority_raw = value["authority"].read_bytes()
    seal_raw = value["kwargs"]["new_books_seal_path"].read_bytes()
    (own / "receipt.json").write_bytes(receipt_raw)
    (own / "authority.json").write_bytes(authority_raw)
    (own / "new-books.json").write_bytes(seal_raw)
    metadata = runtime.metadata_for(receipt_raw, authority_raw, seal_raw, parent_state=parent)
    raw = packed(metadata)
    (own / "metadata.json").write_bytes(raw)
    activation = json.loads(authority_raw)["activation_ms"]
    (own / "commit-receipt.json").write_bytes(packed({"version": "allocation-commit/1",
        "authority_sha256": hashlib.sha256(authority_raw).hexdigest(),
        "metadata_sha256": hashlib.sha256(raw).hexdigest(), "activation_ms": activation,
        "visible_ms": activation - 2, "completed_ms": activation - 1}))
    parent[runtime.POINTER] = runtime.pointer_for(raw)
    (root / "state.json").write_bytes(packed(parent))
    return {**value, "root": root, "parent": parent, "runtime_dir": own}


def context(value, **extra):
    return runtime.load_context(value["root"], learning_root=value["learning"],
        now_ms=value["clock"]["ms"], clock=lambda: value["clock"]["ms"] / 1000, **extra)


def snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def test_absent_latches_preserve_no_allocation_and_create_nothing(tmp_path):
    root = tmp_path / "learning" / "autolearn"
    assert runtime.load_context(root, learning_root=root.parent, now_ms=1) is None
    assert not root.exists()


def test_actual_twelve_leases_ten_retired_twenty_two_declarations_are_read_only(tmp_path):
    value = provision_runtime(rig(tmp_path))
    before = snapshot(value["root"])
    ctx = context(value)
    pairs = ctx.leased_pairs()
    assert len(pairs) == 12 and sum(p["lease"] == "drain_only" for p in pairs) == 2
    assert len(ctx.retired_history()) == 10 and sum(len(m["hypotheses"]) for m in ctx.manifests()) == 22
    assert all(isinstance(p["book_path"], str) and Path(p["book_path"]).is_file() for p in pairs)
    assert ctx.revalidate() == ctx.snapshot_token
    assert snapshot(value["root"]) == before


@pytest.mark.parametrize("latch", ["directory", "pointer"])
def test_either_latch_alone_refuses_without_repair_or_genesis(tmp_path, latch):
    root = tmp_path / "learning/autolearn"
    root.mkdir(parents=True)
    if latch == "directory":
        (root / runtime.DIRECTORY).mkdir()
        (root / "state.json").write_bytes(packed({}))
    else:
        (root / "state.json").write_bytes(packed({runtime.POINTER: {}}))
    before = snapshot(root)
    with pytest.raises(ValueError):
        runtime.load_context(root, learning_root=root.parent, now_ms=1)
    assert snapshot(root) == before


def test_snapshot_token_detects_changed_authority_before_reuse(tmp_path):
    value = provision_runtime(rig(tmp_path))
    ctx = context(value)
    authority = value["runtime_dir"] / "authority.json"
    authority.write_bytes(authority.read_bytes() + b"\n")
    before = snapshot(value["root"])
    with pytest.raises(ValueError, match="metadata"):
        ctx.revalidate()
    assert snapshot(value["root"]) == before


def test_missing_new_book_never_recreated_by_context_or_runner(tmp_path):
    value = provision_runtime(rig(tmp_path))
    ctx = context(value)
    path = Path(next(p["book_path"] for p in ctx.leased_pairs() if p["lease"] == "admission"))
    path.unlink()
    with pytest.raises(ValueError):
        context(value)
    assert not path.exists()


def test_returned_runner_rechecks_parent_pointer_before_every_public_route(tmp_path):
    value = provision_runtime(rig(tmp_path))
    bound = context(value).runner()
    parent = copy.deepcopy(value["parent"])
    parent.pop(runtime.POINTER)
    (value["root"] / "state.json").write_bytes(packed(parent))
    before = snapshot(value["books"])
    for call in (lambda: bound.run_candidates(value["new"], [], {}, now_ms=value["clock"]["ms"]),
                 lambda: bound.expire_pending(value["manifest"], now_ms=value["clock"]["ms"]),
                 lambda: bound.watched_tokens(value["new"])):
        with pytest.raises(ValueError, match="parent_pointer"):
            call()
    assert snapshot(value["books"]) == before


def test_parent_history_sources_envelope_and_provisional_authority_fail_closed(tmp_path, monkeypatch):
    value = provision_runtime(rig(tmp_path))
    for options in ({"max_active_arms": 18}, {"max_stake_usd": "31"}, {"max_open_per_arm": 2}):
        with pytest.raises(ValueError, match="envelope"):
            context(value, **options)
    altered = copy.deepcopy(value["parent"])
    altered["campaign_files"] = []
    with pytest.raises(ValueError, match="stale"):
        context(value, state=altered)
    original = runtime.source_versions
    with monkeypatch.context() as scope:
        scope.setattr(runtime, "source_versions", lambda: {**original(), "foreign": "0" * 64})
        with pytest.raises(ValueError, match="metadata"):
            context(value)
    authority = value["runtime_dir"] / "authority.json"
    authority.unlink()
    with pytest.raises(ValueError, match="missing_owned_artifact"):
        context(value)
    assert not authority.exists()


@pytest.mark.parametrize("failure", ["missing", "late", "unknown", "foreign_authority", "boolean", "future"])
def test_restart_never_admits_without_exact_timely_commit_proof(tmp_path, failure):
    value = provision_runtime(rig(tmp_path))
    ctx = context(value)
    proof_path = value["runtime_dir"] / "commit-receipt.json"
    original = proof_path.read_bytes()
    proof = json.loads(original)
    if failure == "missing":
        proof_path.unlink()
    else:
        if failure == "late":
            proof["completed_ms"] = proof["activation_ms"]
        elif failure == "unknown":
            proof.pop("visible_ms")
        elif failure == "foreign_authority":
            proof["authority_sha256"] = "0" * 64
        elif failure == "boolean":
            proof["visible_ms"] = True
        else:
            value["clock"]["ms"] = proof["completed_ms"] - 1
        proof_path.write_bytes(packed(proof))
    before = snapshot(value["root"])
    calls = [lambda: context(value),
                 lambda: ctx.runner().expire_pending(value["manifest"], now_ms=value["clock"]["ms"]),
                 lambda: ctx.runner().run_candidates(value["new"], [], {}, now_ms=value["clock"]["ms"]),
                 lambda: ctx.assess({}, now_ms=value["clock"]["ms"])]
    if failure != "future":
        calls.append(lambda: ctx.runner().watched_tokens(value["new"]))
    for call in calls:
        with pytest.raises(ValueError, match="commit|owned_artifact"):
            call()
    assert snapshot(value["root"]) == before
    proof_path.write_bytes(original)
    value["clock"]["ms"] = max(value["clock"]["ms"], proof["activation_ms"])
    assert context(value).snapshot_token == ctx.snapshot_token
