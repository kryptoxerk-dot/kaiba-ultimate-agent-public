"""A genuine historical transition, with preserved books and adversarial guards."""
from __future__ import annotations

import ast
import copy
from pathlib import Path

import pytest

from kaiba.learning import report_storage_upgrade as upgrade
from tests.test_allocation_runtime import context, packed, snapshot
from tests.test_journal_storage_upgrade import historical_sources34, provision_storage_runtime
from tests.test_research_service_upgrade import originals


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("transport forbidden"))


def provision_report_runtime(tmp_path, monkeypatch, *, install=True):
    value = provision_storage_runtime(tmp_path, monkeypatch)
    assert value["storage_plan"]["receipt"]["new_sources"] == upgrade.SOURCE34
    value["old_context"] = context(value)
    workspace = Path(__file__).resolve().parents[1]
    for name in (*upgrade.CHANGED, *upgrade.ADDITIONS):
        target = value["code"] / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((workspace / name).read_bytes().replace(b"\r\n", b"\n"))
    monkeypatch.setattr(upgrade, "_workspace_root", lambda: value["code"])
    value["old_sources34"] = historical_sources34()
    return append_report(value) if install else value


def prepare(value):
    return upgrade.prepare_upgrade(value["root"], value["parent"], originals(value),
        old_sources=value["old_sources34"], prepared_ms=value["clock"]["ms"])


def append_report(value):
    plan = prepare(value)
    directory = value["runtime_dir"] / upgrade.DIRECTORY
    directory.mkdir()
    for name, raw in value["old_sources34"].items():
        target = directory / "source34" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    (directory / "receipt.json").write_bytes(plan["receipt_raw"])
    completed = upgrade.completion_for(plan["receipt_raw"], visible_ms=value["clock"]["ms"], completed_ms=value["clock"]["ms"])
    (directory / "commit-receipt.json").write_bytes(packed(completed))
    value["parent"][upgrade.POINTER] = plan["pointer"]
    (value["root"] / "state.json").write_bytes(packed(value["parent"]))
    return {**value, "report_dir": directory, "report_plan": plan, "report_completion": completed}


@pytest.fixture
def report(tmp_path, monkeypatch):
    return provision_report_runtime(tmp_path, monkeypatch)


def test_exact_inverse_of_both_current_files():
    old = historical_sources34()
    workspace = Path(__file__).resolve().parents[1]
    for name, inverse in zip(upgrade.CHANGED, (upgrade._inverse_runtime, upgrade._inverse_worker), strict=True):
        tree = ast.parse((workspace / name).read_text())
        inverse(tree)
        assert upgrade._dump(tree) == upgrade._dump(ast.parse(old[name].decode()))
    assert upgrade._old_sources(old) == {n: upgrade.SOURCE34[n] for n in upgrade.CHANGED}


def test_actual_context_preserves_every_original_authority_and_book(tmp_path, monkeypatch):
    value = provision_report_runtime(tmp_path, monkeypatch, install=False)
    before, parent, artifacts = snapshot(value["root"]), copy.deepcopy(value["parent"]), originals(value)
    assert len(prepare(value)["receipt"]["new_sources"]) == 36
    assert snapshot(value["root"]) == before
    value = append_report(value)
    ctx = context(value)
    assert ctx.report_storage_upgrade["storage_upgrade"] == ctx.journal_storage_upgrade == value["old_context"].journal_storage_upgrade
    assert ctx.authority == value["old_context"].authority
    assert ctx.authority["activation_ms"] == value["activation"]
    assert ctx.revalidate() == ctx.snapshot_token != value["old_context"].snapshot_token
    assert len(ctx.leased_pairs()) == 12 and len(ctx.retired_history()) == 10
    assert sum(len(m["hypotheses"]) for m in ctx.manifests()) == 22
    assert {k: v for k, v in value["parent"].items() if k != upgrade.POINTER} == parent
    assert originals(value) == artifacts
    after = snapshot(value["root"])
    assert all(after[n] == digest for n, digest in before.items() if n != "state.json")
    assert value["report_plan"]["receipt"]["raw_model9"] == upgrade.service.MODEL9


@pytest.mark.parametrize("failure", ["pointer_missing", "directory_missing", "future_commit", "uncompleted", "stale_supplied"])
def test_partial_or_stale_transition_never_falls_back(report, failure):
    value = report
    supplied = None
    if failure == "pointer_missing":
        (value["root"] / "state.json").write_bytes(packed({k: v for k, v in value["parent"].items() if k != upgrade.POINTER}))
    elif failure == "directory_missing":
        value["report_dir"].rename(value["report_dir"].with_name("saved-report"))
    elif failure == "future_commit":
        (value["report_dir"] / "commit-receipt.json").write_bytes(packed({**value["report_completion"], "completed_ms": value["clock"]["ms"] + 1}))
    elif failure == "stale_supplied":
        supplied = copy.deepcopy(value["parent"])
        supplied[upgrade.POINTER]["receipt_sha256"] = "0" * 64
    else:
        (value["report_dir"] / "commit-receipt.json").unlink()
    before = snapshot(value["root"])
    with pytest.raises(ValueError):
        context(value, state=supplied)
    assert snapshot(value["root"]) == before


def test_every_historical_backup_receipt_and_migration_is_still_required(report):
    value = report
    paths = [value["report_dir"] / "source34" / n for n in upgrade.CHANGED]
    paths += [value["storage_dir"] / n for n in ("receipt.json", "commit-receipt.json")]
    paths += [value["storage_dir"] / "migration" / n for n in upgrade.storage.MIGRATION_FILES]
    paths += [value["lesson_dir"] / "commit-receipt.json", value["tax_dir"] / "source28" / upgrade.tax.CHANGED[0]]
    for path in paths:
        raw = path.read_bytes()
        path.unlink()
        before = snapshot(value["root"])
        with pytest.raises((ValueError, KeyError)):
            context(value)
        assert snapshot(value["root"]) == before
        path.write_bytes(raw)
    assert context(value).revalidate()


@pytest.mark.parametrize("name", [*upgrade.CHANGED, upgrade.ADDITIONS[1], *upgrade.storage.ADDITIONS,
    "kaiba/intelligence/wallet_candidates.py", "kaiba/learning/economic_outcomes.py", "kaiba/execution/paper.py"])
def test_source_change_cannot_be_resealed(report, name):
    path = report["code"] / name
    path.write_bytes(path.read_bytes() + b"\nUNREVIEWED_CHANGE = True\n")
    with pytest.raises(ValueError):
        upgrade.current_sources(report["old_sources34"])


@pytest.mark.parametrize("mutation", ["guard", "envelope", "all_json", "missing_hook", "fee_model"])
def test_narrow_ast_policy_rejects_semantic_changes(report, mutation):
    name = upgrade.CHANGED[0 if mutation in ("guard", "envelope") else 1]
    path = report["code"] / name
    raw = path.read_text()
    old, new = {"guard": ("if _full_guard:", "if False:"),
        "envelope": ("max_stake_usd=\"30\"", "max_stake_usd=\"31\""),
        "all_json": ('path.name == "latest-report.json"', 'path.name != "state.json"'),
        "missing_hook": ('value = compact_report(value)', 'value = value'),
        "fee_model": ('allow_nan=False', 'allow_nan=True')}[mutation]
    assert old in raw
    path.write_text(raw.replace(old, new, 1), newline="\n")
    with pytest.raises(ValueError):
        upgrade.current_sources(report["old_sources34"])


def test_original_completion_clock_and_retired_book_guard_remain_active(report):
    value = report
    path = value["storage_dir"] / "commit-receipt.json"
    raw = path.read_bytes()
    path.write_bytes(packed({**value["storage_completion"], "completed_ms": value["clock"]["ms"] + 1}))
    with pytest.raises(ValueError, match="historical_storage_completion"):
        context(value)
    path.write_bytes(raw)
    ctx = context(value)
    book = Path(ctx.retired_history()[0]["book_path"])
    book.write_bytes(book.read_bytes() + b"\n")
    assert ctx._revalidate_bindings() == ctx.snapshot_token
    before = snapshot(value["books"])
    runner = ctx.runner()
    for call in (lambda: runner.run_candidates(value["new"], [], {}, now_ms=value["clock"]["ms"]),
                 lambda: runner.expire_pending(value["manifest"], now_ms=value["clock"]["ms"]),
                 lambda: runner.watched_tokens(value["new"], now_ms=value["clock"]["ms"])):
        with pytest.raises(ValueError):
            call()
        assert snapshot(value["books"]) == before
