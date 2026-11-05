"""Additive operational chain over real disposable22-arm allocation books."""
# Codex AGENT-LESSON-TIMING: sealed historical source inputs stay DATA, never imports.
# PROFIT-ADVANCE-20261027: root-approved historical wallet fixture handoff only.
from __future__ import annotations

import copy
import hashlib
import inspect
import json
from pathlib import Path

import pytest

from kaiba.learning import allocation_runtime as runtime
from kaiba.learning import research_allocation as allocation
from kaiba.learning import research_service_upgrade as upgrade
from tests.test_allocation_runtime import context, packed, provision_runtime, snapshot
from tests.test_research_allocation import rig


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("external transport forbidden"))


def originals(value):
    return {n: (value["runtime_dir"] / n).read_bytes() for n in upgrade.ORIGINAL_FILES}


def historical_source_bytes(generation):
    """Exact closed snapshots for prior recipes; current application edits are irrelevant."""
    fixtures = Path(__file__).parent / "fixtures"
    expected = {
        28: ("tax_quote_upgrade", "source28-fixtures/1", {
            "kaiba/learning/allocation_runtime.py": "b3c07f64c2fc3cf39d7fe74ea706d717fc57ef1ced68ad748634dfbb2f10c8fa",
            "kaiba/learning/autolearn.py": "fb673cff34d51b2c1e337065003900674b570b1d105444565c7269bf8398ac63"}),
        30: ("configured_receipts_upgrade", "source30-fixtures/1", {
            "kaiba/learning/allocation_runtime.py": "8a975dc9789dfb824a809e8216a71b0d4f651b4c54d7e6bc03ecb2c9db125afa",
            "kaiba/learning/transaction_receipts.py": "7f2f67c9266fde66b7bdcf214c4c79d70d37b0fd43f245a97eb2b396dfee51b4"}),
        31: ("lesson_timing_upgrade", "source31-fixtures/1", {
            "kaiba/learning/allocation_runtime.py": "1a55423e8bd07f59fbcdb84f46daa7aa50fcaa27482e5901782ef489f3fccf1e",
            "kaiba/learning/autolearn.py": "dabd04959624f35670f9a7a210773f305001fa366e996750d4ab510731d3b462"}),
    }
    directory, version, sources = expected[generation]
    base = fixtures / directory
    assert json.loads((base / "manifest.json").read_bytes()) == {"version": version, "sources": sources}
    result = {name: (base / f"source{generation}" / (name + ".txt")).read_bytes() for name in sources}
    assert {name: hashlib.sha256(raw).hexdigest() for name, raw in result.items()} == sources
    return result


def append_upgrade(value):
    plan = upgrade.prepare_upgrade(value["parent"], originals(value), prepared_ms=value["clock"]["ms"])
    directory = value["runtime_dir"] / upgrade.DIRECTORY
    directory.mkdir()
    (directory / "receipt.json").write_bytes(plan["receipt_raw"])
    value["parent"][upgrade.POINTER] = plan["pointer"]
    (value["root"] / "state.json").write_bytes(packed(value["parent"]))
    return {**value, "upgrade_dir": directory, "plan": plan}


def historical_wallet_source_bytes():
    """Old servicing fixtures retain their exact three pre-five-chain sources."""
    names = ("kaiba/intelligence/wallet_campaign.py", "kaiba/intelligence/wallet_candidates.py",
             "kaiba/learning/autolearn_wallets.py")
    base = Path(__file__).parent / "fixtures" / "journal_storage_upgrade"
    manifest = json.loads((base / "manifest.json").read_bytes())
    assert manifest["version"] == "source32-fixtures/1"
    wanted = {name: upgrade.SOURCE26[name] for name in names}
    assert {name: manifest["sources"][name] for name in names} == wanted
    result = {name: (base / "source32" / (name + ".txt")).read_bytes() for name in names}
    assert {name: hashlib.sha256(raw).hexdigest() for name, raw in result.items()} == wanted
    return result


def provision_service_runtime(tmp_path, monkeypatch, *, install=True):
    """Explicit offline installer fixture; no local data/ snapshot dependency."""
    value = provision_runtime(rig(tmp_path / "rig"))
    # The original metadata recipe is unchanged. Seal its original15 pins FIRST;
    # all subsequent upgrade operations must preserve these exact raw artifacts.
    metadata = json.loads((value["runtime_dir"] / "metadata.json").read_bytes())
    metadata["source_versions"] = upgrade.original_runtime_sources()
    raw = packed(metadata)
    (value["runtime_dir"] / "metadata.json").write_bytes(raw)
    completion = json.loads((value["runtime_dir"] / "commit-receipt.json").read_bytes())
    completion["metadata_sha256"] = hashlib.sha256(raw).hexdigest()
    (value["runtime_dir"] / "commit-receipt.json").write_bytes(packed(completion))
    value["parent"][runtime.POINTER] = runtime.pointer_for(raw)
    (value["root"] / "state.json").write_bytes(packed(value["parent"]))
    code = tmp_path / "source-tree"
    workspace = Path(upgrade.__file__).resolve().parents[2]
    names = set(upgrade.SOURCE26) | set(upgrade.ADDITIONS) | {"kaiba/" + n for n in upgrade.MODEL9}
    historical = {**historical_source_bytes(28), **historical_wallet_source_bytes(),
                  "kaiba/learning/transaction_receipts.py": historical_source_bytes(30)["kaiba/learning/transaction_receipts.py"]}
    for name in names:
        target = code / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(historical[name] if name in historical
                           else (workspace / name).read_bytes().replace(b"\r\n", b"\n"))
    for name, wanted in upgrade.MODEL9.items():
        raw = (workspace / "kaiba" / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != wanted:
            raw = raw.replace(b"\r\n", b"\n")
        assert hashlib.sha256(raw).hexdigest() == wanted
        (code / "kaiba" / name).write_bytes(raw)
    monkeypatch.setattr(upgrade, "_workspace_root", lambda: code)
    value = {**value, "code": code}
    return append_upgrade(value) if install else value


@pytest.fixture
def old26(tmp_path, monkeypatch):
    return provision_service_runtime(tmp_path, monkeypatch, install=False)


def test_preparation_is_read_only_and_preserves_original_five_raw_artifacts(old26):
    before = snapshot(old26["root"])
    original = originals(old26)
    plan = upgrade.prepare_upgrade(old26["parent"], original, prepared_ms=old26["clock"]["ms"])
    assert len(plan["receipt"]["new_sources"]) == 28
    assert len(plan["receipt"]["old_sources"]) == 26 and len(plan["receipt"]["raw_model9"]) == 9
    assert len(upgrade.original_runtime_sources()) == 15
    assert snapshot(old26["root"]) == before and originals(old26) == original
    plan["receipt"]["old_sources"].clear()
    plan["receipt"]["ast_policy"].clear()
    assert len(upgrade.SOURCE26) == 26 and len(upgrade.AST_POLICY) == 2


def test_old_metadata_requires_explicit_additive_upgrade_and_never_rewrites(old26):
    original = originals(old26)
    original_pointer = copy.deepcopy(old26["parent"][runtime.POINTER])
    with pytest.raises(ValueError, match="metadata"):
        context(old26)
    value = append_upgrade(old26)
    before = snapshot(value["root"])
    ctx = context(value)
    assert ctx.service_upgrade == value["plan"]["pointer"]
    assert ctx.authority["activation_ms"] == original_pointer["activation_ms"]
    assert len(ctx.leased_pairs()) == 12 and len(ctx.retired_history()) == 10
    assert sum(len(m["hypotheses"]) for m in ctx.manifests()) == 22
    assert ctx.revalidate() == ctx.snapshot_token
    assert originals(value) == original and value["parent"][runtime.POINTER] == original_pointer
    assert snapshot(value["root"]) == before


@pytest.mark.parametrize("failure", ["parent_missing", "directory_missing", "receipt_missing", "directory_file", "receipt_tampered"])
def test_either_upgrade_latch_requires_exact_complete_chain(old26, failure):
    value = append_upgrade(old26)
    if failure == "parent_missing":
        parent = copy.deepcopy(value["parent"])
        parent.pop(upgrade.POINTER)
        (value["root"] / "state.json").write_bytes(packed(parent))
    elif failure == "directory_missing":
        (value["upgrade_dir"] / "receipt.json").unlink()
        value["upgrade_dir"].rmdir()
    elif failure == "receipt_missing":
        (value["upgrade_dir"] / "receipt.json").unlink()
    elif failure == "directory_file":
        (value["upgrade_dir"] / "receipt.json").unlink()
        value["upgrade_dir"].rmdir()
        value["upgrade_dir"].write_bytes(b"not a directory")
    else:
        (value["upgrade_dir"] / "receipt.json").write_bytes(value["plan"]["receipt_raw"] + b"\n")
    before = snapshot(value["root"])
    with pytest.raises(ValueError):
        context(value)
    assert snapshot(value["root"]) == before


@pytest.mark.parametrize("name", upgrade.ORIGINAL_FILES)
def test_original_raw_artifact_changes_cannot_be_hidden_by_same_json(old26, name):
    value = append_upgrade(old26)
    target = value["runtime_dir"] / name
    target.write_bytes(target.read_bytes() + b"\n")
    before = snapshot(value["root"])
    with pytest.raises(ValueError):
        context(value)
    assert snapshot(value["root"]) == before


@pytest.mark.parametrize("change", ["protected_source", "outside_once", "same_allowed_method_new_hash", "helper", "raw9"])
def test_exact_current_graph_and_closed_AST_policy_are_independent(old26, change):
    value = append_upgrade(old26)
    if change == "protected_source":
        target = value["code"] / "kaiba/learning/autolearn_wallets.py"
        target.write_bytes(target.read_bytes() + b"\n# changed\n")
    elif change in ("outside_once", "same_allowed_method_new_hash"):
        target = value["code"] / "kaiba/learning/autolearn.py"
        raw = target.read_bytes()
        if change == "outside_once":
            raw = raw.replace(b'VERSION = "autolearn/2"', b'VERSION = "changed"', 1)
        else:
            raw = raw.replace(b"    def _once(self, now: int) -> dict:\n", b"    def _once(self, now: int) -> dict:\n        pass\n", 1)
            assert upgrade._ast_signature(raw, "kaiba/learning/autolearn.py") == upgrade.AST_BASELINES["kaiba/learning/autolearn.py"]
        target.write_bytes(raw)
    elif change == "helper":
        target = value["code"] / "kaiba/learning/research_service_upgrade.py"
        target.write_bytes(target.read_bytes() + b"\n# changed\n")
    else:
        target = value["code"] / "kaiba/execution/paper.py"
        raw = target.read_bytes()
        target.write_bytes(raw.replace(b"\n", b"\r\n"))
    with pytest.raises(ValueError):
        context(value)


def test_original15_map_cannot_be_replaced_by_release26_map(old26):
    raw = originals(old26)
    metadata = json.loads(raw["metadata.json"])
    metadata["source_versions"] = dict(upgrade.SOURCE26)
    raw["metadata.json"] = packed(metadata)
    parent = copy.deepcopy(old26["parent"])
    parent[runtime.POINTER] = runtime.pointer_for(raw["metadata.json"])
    with pytest.raises(ValueError, match="original_runtime15"):
        upgrade.prepare_upgrade(parent, raw, prepared_ms=old26["clock"]["ms"])


@pytest.mark.parametrize("change", ["public_flag", "public_default", "public_revalidate"])
def test_new_seal_cannot_authorize_public_signature_or_revalidate_change(old26, change):
    target = old26["code"] / "kaiba/learning/allocation_runtime.py"
    raw = target.read_bytes()
    if change == "public_flag":
        raw = raw.replace(b"def load_context(state_dir, *, learning_root, state=None,", b"def load_context(state_dir, *, skip_guard=False, learning_root, state=None,", 1)
    elif change == "public_default":
        raw = raw.replace(b"now_ms, max_active_arms=12,", b"now_ms, max_active_arms=18,", 1)
    else:
        raw = raw.replace(b"    def revalidate(self, *, now_ms=None):\n", b"    def revalidate(self, *, now_ms=None):\n        return self.snapshot_token\n", 1)
    target.write_bytes(raw)
    with pytest.raises(ValueError, match="unclaimed_AST_change"):
        upgrade.prepare_upgrade(old26["parent"], originals(old26), prepared_ms=old26["clock"]["ms"])


def test_upgraded_snapshot_rejects_new_valid_upgrade_identity_and_stale_parent(old26):
    value = append_upgrade(old26)
    ctx = context(value)
    supplied = copy.deepcopy(value["parent"])
    supplied.pop(upgrade.POINTER)
    with pytest.raises(ValueError, match="supplied_service_upgrade"):
        context(value, state=supplied)
    unlatched = copy.deepcopy(value["parent"])
    unlatched.pop(upgrade.POINTER)
    plan = upgrade.prepare_upgrade(unlatched, originals(value), prepared_ms=value["clock"]["ms"] + 1)
    (value["upgrade_dir"] / "receipt.json").write_bytes(plan["receipt_raw"])
    value["parent"][upgrade.POINTER] = plan["pointer"]
    (value["root"] / "state.json").write_bytes(packed(value["parent"]))
    value["clock"]["ms"] += 1
    with pytest.raises(ValueError, match="snapshot"):
        ctx._revalidate_bindings()


def test_public_loader_has_no_book_guard_bypass_and_public_revalidate_stays_full(old26, monkeypatch):
    value = append_upgrade(old26)
    ctx = context(value)
    assert "_full_guard" not in inspect.signature(runtime.load_context).parameters
    counted = []
    original = allocation.AllocationRunner._guard

    def guard(self, *args, **kwargs):
        counted.append(kwargs["writes"])
        return original(self, *args, **kwargs)

    monkeypatch.setattr(allocation.AllocationRunner, "_guard", guard)
    ctx._revalidate_bindings()
    assert counted == []
    ctx.revalidate()
    assert counted == [False]
    with pytest.raises(TypeError):
        runtime.load_context(value["root"], learning_root=value["learning"], now_ms=value["clock"]["ms"], _full_guard=False)


def test_private_binding_refresh_also_refuses_before_original_activation(tmp_path):
    # A fresh unupgraded library fixture isolates the base T invariant: an
    # installed upgrade already independently refuses before prepared_ms>=T.
    value = provision_runtime(rig(tmp_path))
    ctx = context(value)
    activation = ctx.authority["activation_ms"]
    # The original timely commit completed at T-1, so this isolates the T guard
    # rather than failing its independently retained completion receipt check.
    with pytest.raises(ValueError, match="allocation_not_yet_active"):
        ctx._revalidate_bindings(now_ms=activation - 1)


@pytest.mark.parametrize("route", ["run", "expire", "watch", "assess"])
def test_each_operation_keeps_one_actual_complete_runner_guard(old26, monkeypatch, route):
    value = append_upgrade(old26)
    ctx = context(value)
    counted = []
    original = allocation.AllocationRunner._guard

    def guard(self, *args, **kwargs):
        counted.append(kwargs["writes"])
        return original(self, *args, **kwargs)

    monkeypatch.setattr(allocation.AllocationRunner, "_guard", guard)
    now = value["clock"]["ms"]
    if route == "run":
        ctx.runner().run_candidates(value["new"], [], {}, now_ms=now)
    elif route == "expire":
        ctx.runner().expire_pending(value["new"], now_ms=now)
    elif route == "watch":
        ctx.runner().watched_tokens(value["new"], now_ms=now)
    else:
        ctx.assess({}, now_ms=now)
    # The frozen watched_tokens itself calls expire_pending, retaining TWO full
    # guards. This integration removes only the extra loader validation.
    assert len(counted) == (2 if route == "watch" else 1)


@pytest.mark.parametrize("route", ["run", "expire", "watch", "assess"])
def test_intervening_archive_mutation_is_rejected_by_retained_full_guard(old26, route):
    value = append_upgrade(old26)
    ctx = context(value)
    record = next(r for r in value["receipt"]["legacy_books"] if r["lease"] == "retired")
    path = value["books"] / record["book"]
    path.write_bytes(path.read_bytes() + b"\n")
    before = snapshot(value["books"])
    assert ctx._revalidate_bindings() == ctx.snapshot_token
    now = value["clock"]["ms"]
    with pytest.raises(ValueError, match="retired_book_changed"):
        if route == "run":
            ctx.runner().run_candidates(value["new"], [], {}, now_ms=now)
        elif route == "expire":
            ctx.runner().expire_pending(value["new"], now_ms=now)
        elif route == "watch":
            ctx.runner().watched_tokens(value["new"], now_ms=now)
        else:
            ctx.assess({}, now_ms=now)
    assert snapshot(value["books"]) == before


def test_missing_allocated_book_is_not_recreated_after_binding_only_refresh(old26):
    value = append_upgrade(old26)
    ctx = context(value)
    target = Path(next(p["book_path"] for p in ctx.leased_pairs() if p["lease"] == "admission"))
    target.unlink()
    assert ctx._revalidate_bindings() == ctx.snapshot_token
    with pytest.raises(ValueError, match="initialized_new_book_missing"):
        ctx.runner().run_candidates(value["new"], [], {}, now_ms=value["clock"]["ms"])
    assert not target.exists()
