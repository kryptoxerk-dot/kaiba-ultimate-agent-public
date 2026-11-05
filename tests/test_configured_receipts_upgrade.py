"""Real allocation/source30 history, with no execution of backed-up programs."""
# Codex AGENT-LESSON-TIMING: seal exact historical31 before any candidate32 overlay.
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from kaiba.learning import configured_receipts_upgrade as upgrade
from kaiba.learning import tax_quote_upgrade as tax
from tests.test_allocation_runtime import context, packed, snapshot
from tests.test_research_service_upgrade import historical_source_bytes, originals
from tests.test_tax_quote_upgrade import provision_tax_runtime


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *a, **kw: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("external transport forbidden"))


def old_sources30():
    result = historical_source_bytes(30)
    assert {n: hashlib.sha256(raw).hexdigest() for n, raw in result.items()} == {
        name: upgrade.SOURCE30[name] for name in upgrade.CHANGED}
    return result


def source31_sources():
    return historical_source_bytes(31)


def append_configured_upgrade(value):
    own = value["runtime_dir"] / upgrade.DIRECTORY
    plan = upgrade.prepare_upgrade(value["parent"], originals(value),
        service_receipt_raw=value["plan"]["receipt_raw"], tax_receipt_raw=value["tax_plan"]["receipt_raw"],
        tax_completion_raw=(value["tax_dir"] / "commit-receipt.json").read_bytes(),
        old_sources=value["old_sources30"], prepared_ms=value["clock"]["ms"])
    own.mkdir()
    for name, raw in value["old_sources30"].items():
        target = own / "source30" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    (own / "receipt.json").write_bytes(plan["receipt_raw"])
    completion = upgrade.completion_for(plan["receipt_raw"], visible_ms=value["clock"]["ms"], completed_ms=value["clock"]["ms"])
    (own / "commit-receipt.json").write_bytes(packed(completion))
    value["parent"][upgrade.POINTER] = plan["pointer"]
    (value["root"] / "state.json").write_bytes(packed(value["parent"]))
    return {**value, "cfg_dir": own, "cfg_plan": plan, "cfg_completion": completion}


def provision_configured_runtime(tmp_path, monkeypatch, *, install=True):
    """Seal historic30 then exact31; a later fixture may explicitly overlay candidate32."""
    value = provision_tax_runtime(tmp_path, monkeypatch)
    old = old_sources30()
    historical31 = source31_sources()
    workspace = Path(upgrade.__file__).resolve().parents[2]
    assert value["tax_plan"]["receipt"]["new_sources"] == upgrade.SOURCE30
    for name, raw in historical31.items():
        (value["code"] / name).write_bytes(raw)
    receipt_source = (workspace / upgrade.CHANGED[1]).read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(receipt_source).hexdigest() == upgrade.RECEIPTS_SOURCE
    (value["code"] / upgrade.CHANGED[1]).write_bytes(receipt_source)
    for name in upgrade.ADDITIONS:
        (value["code"] / name).write_bytes((workspace / name).read_bytes().replace(b"\r\n", b"\n"))
    monkeypatch.setattr(upgrade, "_workspace_root", lambda: value["code"])
    value = {**value, "old_sources30": old, "historical_sources31": historical31}
    return append_configured_upgrade(value) if install else value


@pytest.fixture
def configured(tmp_path, monkeypatch):
    return provision_configured_runtime(tmp_path, monkeypatch)


def test_original_source30_fixture_and_exact_runtime_inverse():
    old = old_sources30()
    tree = ast.parse(source31_sources()[upgrade.CHANGED[0]].decode())
    upgrade._inverse_runtime(tree)
    assert upgrade._dump(tree) == upgrade._dump(ast.parse(old[upgrade.CHANGED[0]].decode()))
    assert len(upgrade.SOURCE30) == 30


def test_genuine_context_readonly_preserves_entire_prior_chain(tmp_path, monkeypatch):
    value = provision_configured_runtime(tmp_path, monkeypatch, install=False)
    before = snapshot(value["root"])
    original = originals(value)
    historical = {name: (value["tax_dir"] / name).read_bytes() for name in ("receipt.json", "commit-receipt.json")}
    plan = upgrade.prepare_upgrade(value["parent"], original, service_receipt_raw=value["plan"]["receipt_raw"],
        tax_receipt_raw=historical["receipt.json"], tax_completion_raw=historical["commit-receipt.json"],
        old_sources=value["old_sources30"], prepared_ms=value["clock"]["ms"])
    assert snapshot(value["root"]) == before and len(plan["receipt"]["new_sources"]) == 31
    value = append_configured_upgrade(value)
    before = snapshot(value["root"])
    ctx = context(value)
    assert ctx.configured_receipts_upgrade["tax_upgrade"] == ctx.tax_quote_upgrade
    assert ctx.tax_quote_upgrade["service_upgrade"]["original_runtime_sources"] == upgrade.service.original_runtime_sources()
    assert ctx.revalidate() == ctx.snapshot_token
    assert len(ctx.leased_pairs()) == 12 and len(ctx.retired_history()) == 10
    assert sum(len(m["hypotheses"]) for m in ctx.manifests()) == 22
    assert ctx.authority["activation_ms"] == value["parent"]["research_allocation"]["activation_ms"]
    assert originals(value) == original and snapshot(value["root"]) == before
    assert all((value["tax_dir"] / name).read_bytes() == raw for name, raw in historical.items())


@pytest.mark.parametrize("failure", ["pointer", "directory", "receipt", "completion", "source30_backup", "source28_backup", "tax_completion", "service_receipt", "future_completion"])
def test_either_latch_requires_exact_complete_history_no_reset(configured, failure):
    value = configured
    if failure == "pointer":
        value["parent"].pop(upgrade.POINTER)
        (value["root"] / "state.json").write_bytes(packed(value["parent"]))
    elif failure == "directory":
        value["cfg_dir"].rename(value["cfg_dir"].with_name("saved-configured-fixture"))
    elif failure in ("receipt", "completion"):
        (value["cfg_dir"] / ("receipt.json" if failure == "receipt" else "commit-receipt.json")).unlink()
    elif failure == "source30_backup":
        path = value["cfg_dir"] / "source30" / upgrade.CHANGED[1]
        path.write_bytes(path.read_bytes() + b"\n")
    elif failure == "source28_backup":
        (value["tax_dir"] / "source28" / tax.CHANGED[0]).unlink()
    elif failure == "tax_completion":
        (value["tax_dir"] / "commit-receipt.json").unlink()
    elif failure == "service_receipt":
        path = value["upgrade_dir"] / "receipt.json"
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        completion = {**value["cfg_completion"], "completed_ms": value["clock"]["ms"] + 1}
        (value["cfg_dir"] / "commit-receipt.json").write_bytes(packed(completion))
    before = snapshot(value["root"])
    with pytest.raises((ValueError, KeyError)):
        context(value)
    assert snapshot(value["root"]) == before


@pytest.mark.parametrize("failure", ["original", "worker", "receipt_leaf", "raw9", "runtime_guard", "signature", "relocation"])
def test_even_resealing_cannot_authorize_foreign_history_or_sources(configured, failure):
    value = configured
    if failure == "original":
        path = value["runtime_dir"] / "metadata.json"
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        name = {"worker": "kaiba/learning/autolearn.py", "receipt_leaf": upgrade.CHANGED[1],
                "raw9": "kaiba/execution/paper.py"}.get(failure, upgrade.CHANGED[0])
        path = value["code"] / name
        raw = path.read_bytes()
        if failure == "runtime_guard":
            raw = raw.replace(b"if _full_guard:", b"if False:", 1)
        elif failure == "signature":
            raw = raw.replace(b"max_active_arms=12", b"max_active_arms=18", 1)
        elif failure == "relocation":
            text = raw.decode()
            binding = '    if configured is not None:\n        context.configured_receipts_upgrade = copy.deepcopy(configured)\n        context.snapshot_token = _hash({"base_authority": context.snapshot_token, "configured_receipts_upgrade": configured})\n'
            assert text.count(binding) == 1
            text = text.replace(binding, "", 1).replace("    context.leased_pairs()\n", binding + "    context.leased_pairs()\n", 1)
            raw = text.encode()
        else:
            raw += b"\n# foreign\n"
        path.write_bytes(raw)
    with pytest.raises(ValueError):
        upgrade.prepare_upgrade({k: v for k, v in value["parent"].items() if k != upgrade.POINTER}, originals(value),
            service_receipt_raw=value["plan"]["receipt_raw"], tax_receipt_raw=value["tax_plan"]["receipt_raw"],
            tax_completion_raw=(value["tax_dir"] / "commit-receipt.json").read_bytes(),
            old_sources=value["old_sources30"], prepared_ms=value["clock"]["ms"])


def test_both_new_latches_lost_cannot_fallback_to_frozen_tax_on_new_graph(configured):
    value = configured
    value["parent"].pop(upgrade.POINTER)
    (value["root"] / "state.json").write_bytes(packed(value["parent"]))
    value["cfg_dir"].rename(value["cfg_dir"].with_name("saved-configured-fixture"))
    with pytest.raises(ValueError):
        context(value)


def test_missing_reviewed_receipt_pin_refuses_without_writing(configured, monkeypatch):
    before = snapshot(configured["root"])
    monkeypatch.setattr(upgrade, "RECEIPTS_SOURCE", None)
    with pytest.raises(ValueError, match="reviewed_receipt_source_pending"):
        context(configured)
    assert snapshot(configured["root"]) == before


def test_historical_tax_completion_guard_has_meaningful_removed_control(configured, monkeypatch):
    value = configured
    tax_raw = value["tax_plan"]["receipt_raw"]
    changed = {**value["tax_completion"], "completed_ms": value["clock"]["ms"] + 1}
    completion_raw = packed(changed)
    arguments = (value["parent"], originals(value), value["plan"]["receipt_raw"], tax_raw, completion_raw)
    with pytest.raises(ValueError, match="historical_tax_completion"):
        upgrade._historical_tax(*arguments, now_ms=value["clock"]["ms"])
    import inspect
    source = inspect.getsource(upgrade._historical_tax)
    needle = 'and completion["completed_ms"] <= now_ms'
    assert source.count(needle) == 1
    scope = dict(upgrade._historical_tax.__globals__)
    exec(compile(source.replace(needle, "and True", 1), "removed-historical-completion-guard", "exec"), scope)
    assert scope["_historical_tax"](*arguments, now_ms=value["clock"]["ms"])[1]["completion_file_sha256"] == hashlib.sha256(completion_raw).hexdigest()


def test_new_commit_gate_has_meaningful_removed_control(configured, monkeypatch):
    value = configured
    completion = {**value["cfg_completion"], "completed_ms": value["clock"]["ms"] + 1}
    (value["cfg_dir"] / "commit-receipt.json").write_bytes(packed(completion))
    with pytest.raises(ValueError, match="completion_required"):
        context(value)
    import inspect
    source = inspect.getsource(upgrade.validate_upgrade)
    needle = 'and completion["completed_ms"] <= now_ms'
    assert source.count(needle) == 1
    scope = dict(upgrade.validate_upgrade.__globals__)
    exec(compile(source.replace(needle, "and True", 1), "removed-configured-completion-guard", "exec"), scope)
    monkeypatch.setattr(upgrade, "validate_upgrade", scope["validate_upgrade"])
    assert context(value).configured_receipts_upgrade is not None
