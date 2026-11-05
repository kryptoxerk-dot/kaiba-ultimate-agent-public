"""Actual 22-book lesson upgrade; historical source programs are never executed."""
from __future__ import annotations

import ast
import copy
import hashlib
import inspect
from pathlib import Path

import pytest

from kaiba.learning import lesson_timing_upgrade as upgrade
from tests.test_allocation_runtime import context, packed, snapshot
from tests.test_configured_receipts_upgrade import provision_configured_runtime, source31_sources
from tests.test_research_service_upgrade import originals


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("external transport forbidden"))


def recipe_arguments(value):
    return {"service_receipt_raw": (value["upgrade_dir"] / "receipt.json").read_bytes(),
        "tax_receipt_raw": (value["tax_dir"] / "receipt.json").read_bytes(),
        "tax_completion_raw": (value["tax_dir"] / "commit-receipt.json").read_bytes(),
        "configured_receipt_raw": (value["cfg_dir"] / "receipt.json").read_bytes(),
        "configured_completion_raw": (value["cfg_dir"] / "commit-receipt.json").read_bytes(),
        "old_sources": value["historical_sources31"], "prepared_ms": value["clock"]["ms"]}


def append_lesson_upgrade(value):
    plan = upgrade.prepare_upgrade(value["parent"], originals(value), **recipe_arguments(value))
    own = value["runtime_dir"] / upgrade.DIRECTORY
    own.mkdir()
    for name, raw in value["historical_sources31"].items():
        target = own / "source31" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    (own / "receipt.json").write_bytes(plan["receipt_raw"])
    completion = upgrade.completion_for(plan["receipt_raw"], visible_ms=value["clock"]["ms"],
                                        completed_ms=value["clock"]["ms"])
    (own / "commit-receipt.json").write_bytes(packed(completion))
    value["parent"][upgrade.POINTER] = plan["pointer"]
    (value["root"] / "state.json").write_bytes(packed(value["parent"]))
    return {**value, "lesson_dir": own, "lesson_plan": plan, "lesson_completion": completion}


def provision_lesson_runtime(tmp_path, monkeypatch, *, install=True):
    """Seal fixed historical31, then exact32, independently of later worker edits."""
    historical32 = source32_sources()
    workspace = Path(upgrade.__file__).resolve().parents[2]
    source_paths = {(workspace / name).resolve(): raw for name, raw in historical32.items()}
    read_bytes = Path.read_bytes

    def staged_read(path):
        return source_paths[path.resolve()] if path.resolve() in source_paths else read_bytes(path)

    with monkeypatch.context() as staging:
        staging.setattr(Path, "read_bytes", staged_read)
        value = provision_configured_runtime(tmp_path, monkeypatch)
    assert value["cfg_plan"]["receipt"]["new_sources"] == upgrade.SOURCE31
    assert value["historical_sources31"] == source31_sources()
    for name in (*upgrade.CHANGED, *upgrade.ADDITIONS):
        target = value["code"] / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(historical32[name] if name in historical32
                           else (workspace / name).read_bytes().replace(b"\r\n", b"\n"))
    monkeypatch.setattr(upgrade, "_workspace_root", lambda: value["code"])
    return append_lesson_upgrade(value) if install else value


def source32_sources():
    """Hash-bound historical source is fixture DATA, never imported or executed."""
    import json

    base = Path(__file__).parent / "fixtures" / "journal_storage_upgrade"
    wanted = {upgrade.CHANGED[0]: "c3c33e855e9b1ace3a530bccd80608cdf2c79a5c0a59ba98fba2056bb3a605de",
              upgrade.CHANGED[1]: upgrade.WORKER_SOURCE,
              **{name: upgrade.SOURCE31[name] for name in ("kaiba/intelligence/wallet_campaign.py",
                 "kaiba/intelligence/wallet_candidates.py", "kaiba/learning/autolearn_wallets.py")}}
    assert json.loads((base / "manifest.json").read_bytes()) == {"version": "source32-fixtures/1", "sources": wanted}
    rows = {name: (base / "source32" / (name + ".txt")).read_bytes() for name in wanted}
    assert {name: hashlib.sha256(raw).hexdigest() for name, raw in rows.items()} == wanted
    return rows


@pytest.fixture
def lesson(tmp_path, monkeypatch):
    return provision_lesson_runtime(tmp_path, monkeypatch)


def test_source31_fixture_and_whole_runtime_inverse():
    old = source31_sources()
    assert upgrade._old_sources(old) == {n: upgrade.SOURCE31[n] for n in upgrade.CHANGED}
    tree = ast.parse(source32_sources()[upgrade.CHANGED[0]].decode())
    upgrade._inverse_runtime(tree)
    assert upgrade._dump(tree) == upgrade._dump(ast.parse(old[upgrade.CHANGED[0]].decode()))
    assert len(upgrade.SOURCE31) == 31


def test_genuine_context_preserves_all_prior_history_original_five_and_family(tmp_path, monkeypatch):
    value = provision_lesson_runtime(tmp_path, monkeypatch, install=False)
    before, original = snapshot(value["root"]), originals(value)
    plan = upgrade.prepare_upgrade(value["parent"], original, **recipe_arguments(value))
    assert snapshot(value["root"]) == before
    assert len(plan["receipt"]["new_sources"]) == 32
    value = append_lesson_upgrade(value)
    after = snapshot(value["root"])
    ctx = context(value)
    assert ctx.lesson_timing_upgrade["configured_upgrade"] == ctx.configured_receipts_upgrade
    assert ctx.configured_receipts_upgrade["tax_upgrade"] == ctx.tax_quote_upgrade
    assert ctx.tax_quote_upgrade["service_upgrade"]["original_runtime_sources"] == upgrade.service.original_runtime_sources()
    assert ctx.revalidate() == ctx.snapshot_token
    assert len(ctx.leased_pairs()) == 12 and len(ctx.retired_history()) == 10
    assert sum(len(m["hypotheses"]) for m in ctx.manifests()) == 22
    assert ctx.authority["activation_ms"] == value["activation"]
    assert originals(value) == original and snapshot(value["root"]) == after
    assert all(after[name] == digest for name, digest in before.items() if name != "state.json")
    assert (value["root"] / "worker.lock").exists()


def test_missing_latches_artifacts_and_prior_backups_refuse_without_writes(lesson):
    value = lesson
    paths = [value["lesson_dir"] / "receipt.json", value["lesson_dir"] / "commit-receipt.json",
        value["lesson_dir"] / "source31" / upgrade.CHANGED[0],
        value["cfg_dir"] / "source30" / upgrade.configured.CHANGED[1],
        value["tax_dir"] / "source28" / upgrade.tax.CHANGED[0],
        value["cfg_dir"] / "commit-receipt.json", value["tax_dir"] / "commit-receipt.json",
        value["upgrade_dir"] / "receipt.json", value["runtime_dir"] / "metadata.json"]
    for path in paths:
        raw = path.read_bytes()
        try:
            path.unlink()
            before = snapshot(value["root"])
            with pytest.raises((ValueError, KeyError)):
                context(value)
            assert snapshot(value["root"]) == before and not path.exists()
        finally:
            path.write_bytes(raw)
    parent_raw = (value["root"] / "state.json").read_bytes()
    try:
        parent = {k: v for k, v in value["parent"].items() if k != upgrade.POINTER}
        (value["root"] / "state.json").write_bytes(packed(parent))
        before = snapshot(value["root"])
        with pytest.raises(ValueError, match="latch"):
            context(value)
        assert snapshot(value["root"]) == before
    finally:
        (value["root"] / "state.json").write_bytes(parent_raw)
    saved = value["lesson_dir"].with_name("saved-lesson-fixture")
    value["lesson_dir"].rename(saved)
    try:
        with pytest.raises(ValueError, match="latch"):
            context(value)
    finally:
        saved.rename(value["lesson_dir"])
    assert context(value).lesson_timing_upgrade is not None


def test_foreign_sources_signature_and_relocated_binding_cannot_be_resealed(lesson):
    value = lesson
    runtime = value["code"] / upgrade.CHANGED[0]
    worker = value["code"] / upgrade.CHANGED[1]
    leaf = value["code"] / "kaiba/learning/transaction_receipts.py"
    raw9 = value["code"] / "kaiba/execution/paper.py"
    binding = ('    if lesson is not None:\n        context.lesson_timing_upgrade = copy.deepcopy(lesson)\n'
               '        context.snapshot_token = _hash({"base_authority": context.snapshot_token, "lesson_timing_upgrade": lesson})\n')
    cases = [(runtime, lambda raw: raw.replace(b"if _full_guard:", b"if False:", 1)),
        (runtime, lambda raw: raw.replace(b"max_active_arms=12", b"max_active_arms=18", 1)),
        (runtime, lambda raw: raw.replace(binding.encode(), b"", 1).replace(b"    context.leased_pairs()\n", binding.encode() + b"    context.leased_pairs()\n", 1)),
        (worker, lambda raw: raw + b"\n# unreviewed worker\n"),
        (leaf, lambda raw: raw + b"\n# foreign receipt leaf\n"),
        (raw9, lambda raw: raw + b"\n")]
    for path, mutate in cases:
        raw = path.read_bytes()
        try:
            changed = mutate(raw)
            assert changed != raw
            path.write_bytes(changed)
            with pytest.raises(ValueError):
                upgrade.prepare_upgrade({k: v for k, v in value["parent"].items() if k != upgrade.POINTER},
                                        originals(value), **recipe_arguments(value))
        finally:
            path.write_bytes(raw)


def test_completion_binding_changes_context_and_future_commit_refuses(lesson):
    value = lesson
    ctx = context(value)
    path = value["lesson_dir"] / "commit-receipt.json"
    raw = path.read_bytes()
    path.write_bytes(raw + b"\n")
    with pytest.raises(ValueError, match="snapshot"):
        ctx.revalidate()
    path.write_bytes(packed({**value["lesson_completion"], "completed_ms": value["clock"]["ms"] + 1}))
    with pytest.raises(ValueError, match="completion_required"):
        context(value)
    path.write_bytes(raw)
    assert ctx.revalidate() == ctx.snapshot_token
    with pytest.raises(ValueError, match="completion_clock"):
        upgrade.completion_for(value["lesson_plan"]["receipt_raw"], visible_ms=True, completed_ms=value["clock"]["ms"])


def test_underlying_real_retired_guard_remains_on_every_bound_route(lesson):
    value = lesson
    ctx = context(value)
    path = value["books"] / next(r["book"] for r in value["receipt"]["legacy_books"] if r["lease"] == "retired")
    raw = path.read_bytes()
    path.write_bytes(raw + b"\n")
    try:
        # Private bindings intentionally avoid book I/O; each next operation still guards.
        assert ctx._revalidate_bindings() == ctx.snapshot_token
        runner = ctx.runner()
        calls = (lambda: runner.run_candidates(value["new"], [], {}, now_ms=value["clock"]["ms"]),
                 lambda: runner.expire_pending(value["manifest"], now_ms=value["clock"]["ms"]),
                 lambda: runner.watched_tokens(value["manifest"], now_ms=value["clock"]["ms"]))
        before = snapshot(value["books"])
        for call in calls:
            with pytest.raises(ValueError):
                call()
            assert snapshot(value["books"]) == before
    finally:
        path.write_bytes(raw)


def test_future_historical_completion_has_meaningful_removed_guard(lesson):
    value = lesson
    arguments = recipe_arguments(value)
    changed = {**value["cfg_completion"], "completed_ms": value["clock"]["ms"] + 1}
    arguments["configured_completion_raw"] = packed(changed)
    args = (value["parent"], originals(value), arguments["service_receipt_raw"], arguments["tax_receipt_raw"],
            arguments["tax_completion_raw"], arguments["configured_receipt_raw"], arguments["configured_completion_raw"])
    with pytest.raises(ValueError, match="historical_configured_completion"):
        upgrade._historical_configured(*args, now_ms=value["clock"]["ms"])
    source = inspect.getsource(upgrade._historical_configured)
    needle = 'and completion["completed_ms"] <= now_ms'
    assert source.count(needle) == 1
    scope = dict(upgrade._historical_configured.__globals__)
    exec(compile(source.replace(needle, "and True", 1), "removed-historical-commit-guard", "exec"), scope)
    assert scope["_historical_configured"](*args, now_ms=value["clock"]["ms"])[1]["completion_file_sha256"] == hashlib.sha256(arguments["configured_completion_raw"]).hexdigest()


def test_old_sources_strict_raw_and_pending_pin_fail_closed(lesson, monkeypatch):
    changed = copy.deepcopy(lesson["historical_sources31"])
    changed[upgrade.CHANGED[0]] += b"\n"
    with pytest.raises(ValueError, match="old_source_raw_hash"):
        upgrade.current_sources(changed)
    before = snapshot(lesson["root"])
    monkeypatch.setattr(upgrade, "WORKER_SOURCE", None)
    with pytest.raises(ValueError, match="reviewed_worker_source_pending"):
        context(lesson)
    assert snapshot(lesson["root"]) == before
