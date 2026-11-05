"""Source28 history plus the real allocation/ledger guard, entirely offline."""
# Codex AGENT-LESSON-TIMING: historical source28/30 inputs remain fixed DATA.
from __future__ import annotations

import ast
import copy
import hashlib
from pathlib import Path

import pytest

from kaiba.learning import allocation_runtime as runtime
from kaiba.learning import research_service_upgrade as service
from kaiba.learning import tax_quote_upgrade as upgrade
from tests.test_allocation_runtime import context, packed, snapshot
from tests.test_research_service_upgrade import (
    append_upgrade,
    historical_source_bytes,
    originals,
    provision_service_runtime,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("external transport forbidden"))


def append_tax_upgrade(value):
    plan = upgrade.prepare_upgrade(value["parent"], originals(value),
        service_receipt_raw=value["plan"]["receipt_raw"], old_sources=value["old_sources"],
        prepared_ms=value["clock"]["ms"])
    own = value["runtime_dir"] / upgrade.DIRECTORY
    own.mkdir()
    for name, raw in value["old_sources"].items():
        target = own / "source28" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    (own / "receipt.json").write_bytes(plan["receipt_raw"])
    commit = upgrade.completion_for(plan["receipt_raw"], visible_ms=value["clock"]["ms"],
                                    completed_ms=value["clock"]["ms"])
    (own / "commit-receipt.json").write_bytes(packed(commit))
    value["parent"][upgrade.POINTER] = plan["pointer"]
    (value["root"] / "state.json").write_bytes(packed(value["parent"]))
    return {**value, "tax_dir": own, "tax_plan": plan, "tax_completion": commit}


def provision_tax_runtime(tmp_path, monkeypatch, *, install=True):
    """Explicit disposable installer; old source bytes are parsed, never executed.

    The two exact source28 snapshots are closed stage fixtures, not live files.
    Seal historical service state BEFORE exposing the new operational graph.
    """
    value = provision_service_runtime(tmp_path, monkeypatch, install=False)
    workspace = Path(upgrade.__file__).resolve().parents[2]
    old_sources = historical_source_bytes(28)
    historical30 = historical_source_bytes(30)
    candidate = {"kaiba/learning/allocation_runtime.py": historical30["kaiba/learning/allocation_runtime.py"],
                 "kaiba/learning/autolearn.py": historical_source_bytes(31)["kaiba/learning/autolearn.py"]}
    for name, raw in old_sources.items():
        assert hashlib.sha256(raw).hexdigest() == upgrade.SOURCE28[name]
        (value["code"] / name).write_bytes(raw)
    value = append_upgrade(value)
    assert value["plan"]["receipt"]["new_sources"] == upgrade.SOURCE28
    for name, raw in candidate.items():
        (value["code"] / name).write_bytes(raw)
    for name in upgrade.ADDITIONS:
        (value["code"] / name).write_bytes((workspace / name).read_bytes().replace(b"\r\n", b"\n"))
    monkeypatch.setattr(upgrade, "_workspace_root", lambda: value["code"])
    value = {**value, "old_sources": old_sources}
    return append_tax_upgrade(value) if install else value


@pytest.fixture
def tax_runtime(tmp_path, monkeypatch):
    return provision_tax_runtime(tmp_path, monkeypatch)


def test_explicit_preparation_readonly_preserves_original_activation_five_service_and_family(tmp_path, monkeypatch):
    value = provision_tax_runtime(tmp_path, monkeypatch, install=False)
    before = snapshot(value["root"])
    original = originals(value)
    old_pointer = copy.deepcopy(value["parent"][runtime.POINTER])
    service_pointer = copy.deepcopy(value["parent"][service.POINTER])
    plan = upgrade.prepare_upgrade(value["parent"], original, service_receipt_raw=value["plan"]["receipt_raw"],
        old_sources=value["old_sources"], prepared_ms=value["clock"]["ms"])
    assert snapshot(value["root"]) == before
    assert len(plan["receipt"]["new_sources"]) == 30 and len(plan["receipt"]["old_sources"]) == 28
    assert plan["receipt"]["original"]["lifetime_family_size"] == 22
    assert plan["receipt"]["original"]["active_lease_count"] == 12
    value = append_tax_upgrade(value)
    before = snapshot(value["root"])
    ctx = context(value)
    assert ctx.tax_quote_upgrade["service_upgrade"]["original_runtime_sources"] == service.original_runtime_sources()
    assert ctx.service_upgrade == service_pointer
    assert ctx.tax_quote_upgrade["receipt_file_sha256"] == hashlib.sha256(plan["receipt_raw"]).hexdigest()
    assert ctx.revalidate() == ctx.snapshot_token
    assert ctx.authority["activation_ms"] == old_pointer["activation_ms"]
    assert len(ctx.leased_pairs()) == 12 and len(ctx.retired_history()) == 10
    assert sum(len(m["hypotheses"]) for m in ctx.manifests()) == 22
    assert originals(value) == original and value["parent"][runtime.POINTER] == old_pointer
    assert value["parent"][service.POINTER] == service_pointer and snapshot(value["root"]) == before


@pytest.mark.parametrize("failure", ["parent", "directory", "receipt", "completion", "backup", "file", "completion_future"])
def test_either_latch_requires_complete_exact_chain_without_repair(tax_runtime, failure):
    value = tax_runtime
    if failure == "parent":
        value["parent"].pop(upgrade.POINTER)
        (value["root"] / "state.json").write_bytes(packed(value["parent"]))
    elif failure == "directory":
        value["tax_dir"].rename(value["tax_dir"].with_name("saved-tax-fixture"))
    elif failure in ("receipt", "completion"):
        (value["tax_dir"] / ("receipt.json" if failure == "receipt" else "commit-receipt.json")).unlink()
    elif failure == "backup":
        (value["tax_dir"] / "source28" / upgrade.CHANGED[0]).unlink()
    elif failure == "file":
        value["tax_dir"].rename(value["tax_dir"].with_name("saved-tax-fixture"))
        value["tax_dir"].write_bytes(b"not a directory")
    else:
        commit = copy.deepcopy(value["tax_completion"])
        commit["completed_ms"] = value["clock"]["ms"] + 1
        (value["tax_dir"] / "commit-receipt.json").write_bytes(packed(commit))
    before = snapshot(value["root"])
    with pytest.raises(ValueError):
        context(value)
    assert snapshot(value["root"]) == before


def test_both_lost_tax_latches_cannot_fallback_to_old_service_on_new_sources(tax_runtime):
    value = tax_runtime
    value["parent"].pop(upgrade.POINTER)
    (value["root"] / "state.json").write_bytes(packed(value["parent"]))
    value["tax_dir"].rename(value["tax_dir"].with_name("saved-tax-fixture"))
    with pytest.raises(ValueError, match="current_source_graph_changed"):
        context(value)


@pytest.mark.parametrize("name", service.ORIGINAL_FILES)
def test_original_five_raw_artifacts_cannot_be_reformatted(tax_runtime, name):
    target = tax_runtime["runtime_dir"] / name
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(ValueError):
        context(tax_runtime)


@pytest.mark.parametrize("failure", ["service_pointer", "service_receipt", "backup_bytes", "adapter", "raw9"])
def test_historical_service_and_model_sources_remain_authenticated(tax_runtime, failure):
    value = tax_runtime
    if failure == "service_pointer":
        value["parent"].pop(service.POINTER)
        (value["root"] / "state.json").write_bytes(packed(value["parent"]))
    elif failure == "service_receipt":
        target = value["upgrade_dir"] / "receipt.json"
        target.write_bytes(target.read_bytes() + b"\n")
    elif failure == "backup_bytes":
        target = value["tax_dir"] / "source28" / upgrade.CHANGED[1]
        target.write_bytes(target.read_bytes() + b"\n")
    elif failure == "adapter":
        target = value["code"] / upgrade.ADDITIONS[0]
        target.write_bytes(target.read_bytes() + b"\n# unreviewed\n")
    else:
        target = value["code"] / "kaiba/execution/paper.py"
        target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(ValueError):
        context(value)


@pytest.mark.parametrize("change", ["worker_guard", "runtime_guard", "signature", "mixed_old_worker", "mixed_old_runtime"])
def test_even_new_receipt_cannot_authorize_unrelated_AST_or_mixed_prefix(tax_runtime, change):
    value = tax_runtime
    worker = value["code"] / upgrade.CHANGED[1]
    backend = value["code"] / upgrade.CHANGED[0]
    if change == "worker_guard":
        raw = worker.read_bytes()
        modified = raw.replace(b"storage >= 256 * 1024 * 1024", b"storage >= 512 * 1024 * 1024", 1)
        assert modified != raw
        worker.write_bytes(modified)
    elif change == "runtime_guard":
        raw = backend.read_bytes()
        modified = raw.replace(b"    if _full_guard:\n", b"    if False:\n", 1)
        assert modified != raw
        backend.write_bytes(modified)
    elif change == "signature":
        raw = backend.read_bytes()
        modified = raw.replace(b"def load_context(state_dir, *,", b"def load_context(state_dir, *, skip_guard=False,", 1)
        assert modified != raw
        backend.write_bytes(modified)
    else:
        name = upgrade.CHANGED[1] if change == "mixed_old_worker" else upgrade.CHANGED[0]
        (value["code"] / name).write_bytes(value["old_sources"][name])
    parent = {k: v for k, v in value["parent"].items() if k != upgrade.POINTER}
    with pytest.raises(ValueError):
        upgrade.prepare_upgrade(parent, originals(value), service_receipt_raw=value["plan"]["receipt_raw"],
            old_sources=value["old_sources"], prepared_ms=value["clock"]["ms"])


def test_changed_commit_changes_snapshot_and_stale_supplied_pointer_refuses(tax_runtime):
    value = tax_runtime
    ctx = context(value)
    supplied = copy.deepcopy(value["parent"])
    supplied.pop(upgrade.POINTER)
    with pytest.raises(ValueError, match="supplied_tax_upgrade_stale"):
        context(value, state=supplied)
    path = value["tax_dir"] / "commit-receipt.json"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="snapshot"):
        ctx._revalidate_bindings()


@pytest.mark.parametrize("kind", ["runtime", "worker"])
def test_required_new_hooks_cannot_be_relocated_for_a_new_receipt(tax_runtime, kind):
    value = tax_runtime
    name = upgrade.CHANGED[0] if kind == "runtime" else upgrade.CHANGED[1]
    target = value["code"] / name
    tree = ast.parse(target.read_text(encoding="utf-8"))
    if kind == "runtime":
        loader = upgrade._definition(tree, "_load_context")
        index = upgrade._position(loader.body,
            '_require(supplied.get(tax_upgrade.POINTER) == disk.get(tax_upgrade.POINTER), "supplied_tax_upgrade_stale")')
        loader.body.append(loader.body.pop(index))  # after return: would never validate supplied pointer
    else:
        once = upgrade._definition(tree, "AutoLearner._once")
        tick = upgrade._definition(once, "new_paper_tick")
        phase = next(n for n in tick.body if isinstance(n, ast.With)
            and upgrade._dump(n.items[0].context_expr) == upgrade._dump(ast.parse('phase("captured_quotes")', mode="eval").body))
        phase.body[0], phase.body[1] = phase.body[1], phase.body[0]  # would overwrite adapter selection
    target.write_text(ast.unparse(tree) + "\n", encoding="utf-8")
    parent = {k: v for k, v in value["parent"].items() if k != upgrade.POINTER}
    with pytest.raises(ValueError, match="tax_validation_order|quote_selection_order"):
        upgrade.prepare_upgrade(parent, originals(value), service_receipt_raw=value["plan"]["receipt_raw"],
            old_sources=value["old_sources"], prepared_ms=value["clock"]["ms"])


def test_public_and_each_bound_route_retain_real_full_inventory_guard(tax_runtime):
    value = tax_runtime
    ctx = context(value)
    path = Path(next(p["book_path"] for p in ctx.leased_pairs() if p["lease"] == "admission"))
    path.unlink()
    for invoke in (lambda: context(value),
        lambda: ctx.runner().run_candidates(value["new"], [], {}, now_ms=value["clock"]["ms"]),
        lambda: ctx.runner().expire_pending(value["new"], now_ms=value["clock"]["ms"]),
        lambda: ctx.runner().watched_tokens(value["new"], now_ms=value["clock"]["ms"]),
        lambda: ctx.assess({}, now_ms=value["clock"]["ms"])):
        with pytest.raises(ValueError, match="initialized_new_book_missing_no_genesis"):
            invoke()
    assert not path.exists()


def test_removed_closed_AST_control_is_detected(tax_runtime, monkeypatch):
    value = tax_runtime
    target = value["code"] / upgrade.CHANGED[1]
    raw = target.read_bytes()
    target.write_bytes(raw.replace(b"storage >= 256 * 1024 * 1024", b"storage >= 512 * 1024 * 1024", 1))
    with pytest.raises(ValueError, match="unclaimed_AST_change"):
        upgrade.current_sources(value["old_sources"])
    inverse = upgrade._inverse_worker
    baseline = upgrade._dump(ast.parse(value["old_sources"][upgrade.CHANGED[1]].decode()))
    original_dump = upgrade._dump
    with monkeypatch.context() as scope:
        scope.setattr(upgrade, "_inverse_worker", lambda tree: inverse(tree))
        # Remove only the final whole-worker AST comparison, not setup/inputs.
        scope.setattr(upgrade, "_dump", lambda node: baseline if isinstance(node, ast.Module)
                      and any(isinstance(n, ast.ClassDef) and n.name == "AutoLearner" for n in node.body)
                      else original_dump(node))
        result = upgrade.current_sources(value["old_sources"])
    assert result[upgrade.CHANGED[1]] != value["tax_plan"]["receipt"]["new_sources"][upgrade.CHANGED[1]]


def test_no_artifacts_legacy_probe_is_readonly(tmp_path):
    root = tmp_path / "absent"
    assert upgrade.validate_upgrade(root, {}, {}, now_ms=1) is None
    assert not root.exists()


def test_completion_does_not_replace_original_activation_and_rejects_boolean_clock(tax_runtime):
    raw = tax_runtime["tax_plan"]["receipt_raw"]
    now = tax_runtime["clock"]["ms"]
    completion = upgrade.completion_for(raw, visible_ms=now + 1, completed_ms=now + 2)
    assert completion["activation_ms"] == tax_runtime["activation"] < now
    for options in ({"visible_ms": True, "completed_ms": now},
                    {"visible_ms": now, "completed_ms": now - 1}):
        with pytest.raises(ValueError, match="completion_clock"):
            upgrade.completion_for(raw, **options)
