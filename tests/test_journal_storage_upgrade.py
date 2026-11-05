"""Authenticate source32 history as data; preserve every allocated PAPER authority."""
from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path

import pytest

from kaiba.learning import journal_outbox as outbox
from kaiba.learning import journal_storage_upgrade as upgrade
from kaiba.learning import observer_upgrade as observer
from kaiba.learning import prospective_quotes, quote_budget
from tests.test_allocation_runtime import context, packed, snapshot
from tests.test_journal_outbox import legacy, revision
from tests.test_lesson_timing_upgrade import provision_lesson_runtime
from tests.test_research_service_upgrade import originals

FIXTURES = Path(__file__).parent / "fixtures" / "journal_storage_upgrade"
HISTORICAL_NAMES = (*upgrade.CHANGED, "kaiba/intelligence/wallet_campaign.py",
                    "kaiba/intelligence/wallet_candidates.py", "kaiba/learning/autolearn_wallets.py")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *args, **kwargs: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: pytest.fail("transport forbidden"))


def historical_sources32():
    manifest = json.loads((FIXTURES / "manifest.json").read_bytes())
    wanted = {name: upgrade.SOURCE32[name] for name in HISTORICAL_NAMES}
    assert manifest == {"version": "source32-fixtures/1", "sources": wanted}
    values = {name: (FIXTURES / "source32" / (name + ".txt")).read_bytes() for name in wanted}
    assert {name: hashlib.sha256(raw).hexdigest() for name, raw in values.items()} == wanted
    return values


def historical_sources34():
    # REPORT-STORAGE-UPGRADE: pin the previous worker/runtime as fixture data.
    base = Path(__file__).parent / "fixtures" / "report_storage_upgrade"
    wanted = {upgrade.CHANGED[0]: "bdc5825a6c3d73868fe2e1c56b3fe47807069a30e5f8461f453882ba20a9c250",
              upgrade.CHANGED[1]: upgrade.WORKER_SOURCE}
    assert json.loads((base / "manifest.json").read_bytes()) == {"version": "source34-fixtures/1", "sources": wanted}
    values = {name: (base / "source34" / (name + ".txt")).read_bytes() for name in wanted}
    assert {name: hashlib.sha256(raw).hexdigest() for name, raw in values.items()} == wanted
    return values


def arguments(value):
    return {"service_receipt_raw": (value["upgrade_dir"] / "receipt.json").read_bytes(),
        "tax_receipt_raw": (value["tax_dir"] / "receipt.json").read_bytes(),
        "tax_completion_raw": (value["tax_dir"] / "commit-receipt.json").read_bytes(),
        "configured_receipt_raw": (value["cfg_dir"] / "receipt.json").read_bytes(),
        "configured_completion_raw": (value["cfg_dir"] / "commit-receipt.json").read_bytes(),
        "lesson_receipt_raw": (value["lesson_dir"] / "receipt.json").read_bytes(),
        "lesson_completion_raw": (value["lesson_dir"] / "commit-receipt.json").read_bytes(),
        "old_sources": value["old_sources32"], "migration_artifacts": value["migration_artifacts"],
        "prepared_ms": value["clock"]["ms"]}


def append_journal_upgrade(value):
    plan = upgrade.prepare_upgrade(value["parent"], originals(value), **arguments(value))
    directory = value["runtime_dir"] / upgrade.DIRECTORY
    directory.mkdir()
    migration = directory / "migration"
    migration.mkdir()
    for name, raw in value["migration_artifacts"].items():
        (migration / name).write_bytes(raw)
    for name, raw in value["old_sources32"].items():
        target = directory / "source32" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    (directory / "receipt.json").write_bytes(plan["receipt_raw"])
    completed = upgrade.completion_for(plan["receipt_raw"], visible_ms=value["clock"]["ms"],
                                       completed_ms=value["clock"]["ms"])
    (directory / "commit-receipt.json").write_bytes(packed(completed))
    value["parent"][upgrade.POINTER] = plan["pointer"]
    (value["root"] / "state.json").write_bytes(packed(value["parent"]))
    return {**value, "storage_dir": directory, "storage_plan": plan, "storage_completion": completed}


def provision_storage_runtime(tmp_path, monkeypatch, *, install=True):
    """Seal original32 first; no historical source is imported or executed.

    The three un-deployed wallet-intake edits in the shared workspace must not be
    silently admitted by this operational upgrade or contaminate its old fixture.
    Only exact hash-checked historical file reads are substituted during staging.
    """
    workspace = Path(upgrade.__file__).resolve().parents[2]
    historical = historical_sources32()
    current34 = historical_sources34()
    value = provision_lesson_runtime(tmp_path, monkeypatch)
    assert value["lesson_plan"]["receipt"]["new_sources"] == upgrade.SOURCE32
    for name in (*upgrade.CHANGED, *upgrade.ADDITIONS):
        target = value["code"] / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(current34[name] if name in current34 else (workspace / name).read_bytes().replace(b"\r\n", b"\n"))
    monkeypatch.setattr(upgrade, "_workspace_root", lambda: value["code"])
    core = revision()
    value["parent"]["pending_lessons"] = [legacy(core, ["fee_missing"]),
        legacy(core, ["fee_missing"], sorted_body=True), legacy(core, ["fee_missing", "custody_missing"])]
    state_raw = packed(value["parent"])
    economic_raw = packed({"revision_outbox": {outbox._hash(core): core}, "original_marker": "keep"})
    recipe = outbox.prepare_migration(state_raw, economic_raw, prepared_ms=value["clock"]["ms"])
    migrated = json.loads(recipe["state_raw"])
    value["parent"] = migrated
    (value["root"] / "state.json").write_bytes(recipe["state_raw"])
    migration_artifacts = dict(zip(upgrade.MIGRATION_FILES,
        (recipe["receipt_raw"], recipe["archive_raw"], state_raw, economic_raw), strict=True))
    value = {**value, "old_sources32": {name: historical[name] for name in upgrade.CHANGED},
             "migration_artifacts": migration_artifacts, "migration_recipe": recipe}
    return append_journal_upgrade(value) if install else value


@pytest.fixture
def storage(tmp_path, monkeypatch):
    return provision_storage_runtime(tmp_path, monkeypatch)


def test_exact_historical32_fixture_and_runtime_inverse():
    historical = historical_sources32()
    tree = ast.parse(historical_sources34()[upgrade.CHANGED[0]].decode())
    upgrade._inverse_runtime(tree)
    assert upgrade._dump(tree) == upgrade._dump(ast.parse(historical[upgrade.CHANGED[0]].decode()))
    assert len(upgrade.SOURCE32) == 32
    assert upgrade._old_sources({name: historical[name] for name in upgrade.CHANGED}) == {
        name: upgrade.SOURCE32[name] for name in upgrade.CHANGED}


def test_actual_context_preserves_prior_artifacts_books_admissions_and_family(tmp_path, monkeypatch):
    value = provision_storage_runtime(tmp_path, monkeypatch, install=False)
    state = value["parent"]
    state["pending_lessons"] = [{"evidence_id": "fixture-existing", "body": "immutable", "outcome": "miss"}]
    state["pending_admissions"] = [{"id": "fixture-spent-intent", "used": True}]
    (value["root"] / "state.json").write_bytes(packed(state))
    old_state = copy.deepcopy(state)
    before, old_artifacts = snapshot(value["root"]), originals(value)
    plan = upgrade.prepare_upgrade(state, old_artifacts, **arguments(value))
    assert snapshot(value["root"]) == before
    assert len(plan["receipt"]["new_sources"]) == 34
    assert plan["receipt"]["raw_model9"] == upgrade.service.MODEL9
    value = append_journal_upgrade(value)
    after = snapshot(value["root"])
    ctx = context(value)
    assert ctx.journal_storage_upgrade["lesson_upgrade"] == ctx.lesson_timing_upgrade
    assert ctx.lesson_timing_upgrade["configured_upgrade"] == ctx.configured_receipts_upgrade
    assert ctx.revalidate() == ctx.snapshot_token
    assert len(ctx.leased_pairs()) == 12 and len(ctx.retired_history()) == 10
    assert sum(len(m["hypotheses"]) for m in ctx.manifests()) == 22
    assert ctx.authority["activation_ms"] == value["activation"]
    assert originals(value) == old_artifacts and snapshot(value["root"]) == after
    assert {k: v for k, v in value["parent"].items() if k != upgrade.POINTER} == old_state
    assert all(after[name] == digest for name, digest in before.items() if name != "state.json")
    assert (value["root"] / "worker.lock").exists()


def test_missing_original_or_new_artifacts_refuse_without_mutation(storage):
    value = storage
    paths = [value["storage_dir"] / "receipt.json", value["storage_dir"] / "commit-receipt.json",
             value["storage_dir"] / "source32" / upgrade.CHANGED[0],
             value["lesson_dir"] / "source31" / upgrade.CHANGED[1],
             value["lesson_dir"] / "commit-receipt.json",
             value["cfg_dir"] / "source30" / upgrade.configured.CHANGED[1],
             value["tax_dir"] / "source28" / upgrade.tax.CHANGED[0],
             value["runtime_dir"] / "authority.json"]
    paths.extend(value["storage_dir"] / "migration" / name for name in upgrade.MIGRATION_FILES)
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


@pytest.mark.parametrize("failure", ["pointer_missing", "directory_missing", "future_commit", "uncompleted"])
def test_partial_install_cannot_fall_back_or_reset(storage, failure):
    value = storage
    if failure == "pointer_missing":
        changed = {k: v for k, v in value["parent"].items() if k != upgrade.POINTER}
        (value["root"] / "state.json").write_bytes(packed(changed))
    elif failure == "directory_missing":
        value["storage_dir"].rename(value["storage_dir"].with_name("saved-storage"))
    elif failure == "future_commit":
        (value["storage_dir"] / "commit-receipt.json").write_bytes(packed({
            **value["storage_completion"], "completed_ms": value["clock"]["ms"] + 1}))
    else:
        (value["storage_dir"] / "commit-receipt.json").unlink()
    before = snapshot(value["root"])
    with pytest.raises(ValueError):
        context(value)
    assert snapshot(value["root"]) == before


@pytest.mark.parametrize("path", [*upgrade.CHANGED, upgrade.ADDITIONS[1],
    "kaiba/intelligence/wallet_campaign.py", "kaiba/intelligence/wallet_candidates.py",
    "kaiba/learning/autolearn_wallets.py", "kaiba/learning/economic_work.py", "kaiba/learning/economic_outcomes.py",
    "kaiba/learning/lesson_timing_upgrade.py", "kaiba/execution/paper.py"])
def test_unreviewed_source_or_economic_change_cannot_be_resealed(storage, path):
    target = storage["code"] / path
    raw = target.read_bytes()
    target.write_bytes(raw + b"\nUNREVIEWED_FIXTURE_CHANGE = True\n")
    before = snapshot(storage["root"])
    with pytest.raises(ValueError):
        upgrade.prepare_upgrade({k: v for k, v in storage["parent"].items() if k != upgrade.POINTER},
                                originals(storage), **arguments(storage))
    assert snapshot(storage["root"]) == before


def test_runtime_guard_envelope_and_binding_location_are_exact(storage):
    target = storage["code"] / upgrade.CHANGED[0]
    raw = target.read_bytes()
    binding = ('    if storage is not None:\n        context.journal_storage_upgrade = copy.deepcopy(storage)\n'
               '        context.snapshot_token = _hash({"base_authority": context.snapshot_token, "journal_storage_upgrade": storage})\n')
    for changed in (raw.replace(b"if _full_guard:", b"if False:", 1),
                    raw.replace(b"max_active_arms=12", b"max_active_arms=18", 1),
                    raw.replace(binding.encode(), b"", 1).replace(b"    context.leased_pairs()\n", binding.encode() + b"    context.leased_pairs()\n", 1)):
        assert changed != raw
        target.write_bytes(changed)
        with pytest.raises(ValueError):
            upgrade.current_sources(storage["old_sources32"])
    target.write_bytes(raw)
    assert len(upgrade.current_sources(storage["old_sources32"])) == 34


def test_receipt_raw_identity_and_supplied_pointer_bind_existing_context(storage):
    ctx = context(storage)
    path = storage["storage_dir"] / "commit-receipt.json"
    raw = path.read_bytes()
    path.write_bytes(raw + b"\n")
    with pytest.raises(ValueError, match="snapshot"):
        ctx.revalidate()
    path.write_bytes(raw)
    supplied = copy.deepcopy(storage["parent"])
    supplied[upgrade.POINTER]["receipt_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="supplied_journal_storage_upgrade_stale"):
        context(storage, state=supplied)
    supplied = copy.deepcopy(storage["parent"])
    supplied[upgrade.MIGRATION_POINTER]["archive_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="supplied_journal_storage_upgrade_stale"):
        context(storage, state=supplied)
    assert ctx.revalidate() == ctx.snapshot_token


@pytest.mark.parametrize("name", upgrade.MIGRATION_FILES)
def test_migration_originals_archive_and_receipt_cannot_be_changed(storage, name):
    target = storage["storage_dir"] / "migration" / name
    target.write_bytes(target.read_bytes() + b"\n")
    before = snapshot(storage["root"])
    with pytest.raises(ValueError, match="migration_recipe_or_archive_changed"):
        context(storage)
    assert snapshot(storage["root"]) == before


def test_migration_preserves_diagnostic_variants_and_binds_only_stable_pointer(storage):
    receipt = storage["storage_plan"]["receipt"]["migration"]
    assert receipt["receipt"]["original_observations"] == 3
    assert receipt["receipt"]["distinct_diagnostic_variants"] == 2
    archive = json.loads(storage["migration_artifacts"]["pending-lessons.original.json"])
    assert [row["pending_index"] for row in archive["observations"]] == [0, 1, 2]
    assert [row["unknown_reasons"] for row in archive["revision_provenance"][0]["variants"]] == [
        ["fee_missing"], ["fee_missing", "custody_missing"]]
    changed = copy.deepcopy(storage["parent"])
    changed["pending_lessons"] = []  # Ordinary later acknowledgement; initial state hash is historical.
    changed["last_attempt_ms"] = storage["clock"]["ms"] + 1
    (storage["root"] / "state.json").write_bytes(packed(changed))
    assert context(storage).journal_storage_upgrade is not None
    changed[upgrade.MIGRATION_POINTER]["archive_sha256"] = "0" * 64
    (storage["root"] / "state.json").write_bytes(packed(changed))
    with pytest.raises(ValueError, match="parent_migration_binding"):
        context(storage)


def test_successful_recipe_cache_reads_every_artifact_and_never_uses_mtime(storage, monkeypatch):
    upgrade._MIGRATION_CACHE.clear()
    original, calls = outbox.prepare_migration, []

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(outbox, "prepare_migration", counted)
    ctx = context(storage)
    assert ctx.revalidate() == ctx.snapshot_token
    assert len(calls) == 1 and len(upgrade._MIGRATION_CACHE) == 1
    args = arguments(storage)
    first = upgrade._migration(storage["parent"], args["migration_artifacts"],
        prepared_ms=args["prepared_ms"], helper_source_sha256=upgrade.OUTBOX_SOURCE)
    first["pointer"]["archive_sha256"] = "0" * 64
    assert ctx.revalidate() == ctx.snapshot_token  # Returned metadata cannot mutate the cache.
    for name in upgrade.MIGRATION_FILES:
        target = storage["storage_dir"] / "migration" / name
        raw, stat = target.read_bytes(), target.stat()
        changed = bytearray(raw)
        index = raw.index(b'"') + 1
        changed[index] = ord("Z") if changed[index] != ord("Z") else ord("Y")
        target.write_bytes(changed)
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert target.stat().st_size == stat.st_size and target.stat().st_mtime_ns == stat.st_mtime_ns
        before = snapshot(storage["root"])
        with pytest.raises((ValueError, KeyError)):
            ctx.revalidate()
        assert snapshot(storage["root"]) == before
        target.write_bytes(raw)
        assert ctx.revalidate() == ctx.snapshot_token
    assert len(calls) > 1 and len(upgrade._MIGRATION_CACHE) == 1
    parent = copy.deepcopy(storage["parent"])
    parent[upgrade.MIGRATION_POINTER]["archive_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="parent_migration_binding"):
        upgrade._migration(parent, args["migration_artifacts"], prepared_ms=args["prepared_ms"],
                           helper_source_sha256=upgrade.OUTBOX_SOURCE)
    with pytest.raises(ValueError, match="migration_clock"):
        upgrade._migration(storage["parent"], args["migration_artifacts"], prepared_ms=1,
                           helper_source_sha256=upgrade.OUTBOX_SOURCE)


def test_observer_rebind_preserves_policy_state_ledger_and_original_admission(storage):
    now = storage["clock"]["ms"]
    collector = observer.BudgetedQuoteCollector(storage["root"], storage["learning"] / "prospective-quotes",
        learning_root=storage["learning"], clock=lambda: now / 1000, provider=object())
    day, token = str(now // prospective_quotes.DAY_MS), "0x" + "a" * 40
    target = {"kind": "pending", "chain": "bsc", "token": token, "signal_ms": now - 1000}
    attempt = {"attempt_id": quote_budget._digest([day, 19, "bsc:" + token]), "target": target,
               "admitted_ms": now, "status": "admitted"}
    state = {"version": prospective_quotes.VERSION, "policy": prospective_quotes.POLICY,
        "days": {day: 19}, "tokens": {"bsc:" + token: now}, "attempts": [attempt],
        "retry_after_ms": now + 60_000, "updated_ms": now}
    plan = observer.prepare_upgrade(state, "a" * 64, now_ms=now)
    old = copy.deepcopy(plan["manifest"])
    old["source_bindings"]["learning/allocation_runtime.py"] = upgrade.SOURCE32[upgrade.CHANGED[0]]
    old.pop("unused", None)
    old_raw = packed(old)
    collector.manifest_path.write_bytes(old_raw)
    collector.state_path.write_bytes(packed(state))
    collector.ledger_path.write_bytes(packed(plan["ledger"]))
    before = {path: path.read_bytes() for path in (collector.state_path, collector.ledger_path)}
    with pytest.raises(ValueError, match="declaration_or_source_changed"):
        collector._declaration()
    updated = {**copy.deepcopy(old), "source_bindings": observer.source_bindings(),
               "allocation_snapshot_token": context(storage).snapshot_token}
    assert {key for key in old if old[key] != updated[key]} == {"source_bindings", "allocation_snapshot_token"}
    archived = collector.state_dir / "observer-upgrade.original.json"
    archived.write_bytes(old_raw)
    collector.manifest_path.write_bytes(packed(updated))
    collector._manifest = collector._declaration()
    assert collector._manifest == updated
    collector._manifest = {**updated, "allocation_snapshot_token": "b" * 64}
    with pytest.raises(ValueError, match="allocation_snapshot_changed"):
        collector._load_targets(now)
    collector._manifest = updated
    _, errors = collector._load_targets(now)
    assert errors == []
    for key, value in (("funding_allowed", True), ("policy", {"changed": True})):
        collector.manifest_path.write_bytes(packed({**updated, key: value}))
        with pytest.raises(ValueError, match="declaration_or_source_changed"):
            collector._declaration()
    collector.manifest_path.write_bytes(packed(updated))
    assert collector._declaration() == updated
    assert archived.read_bytes() == old_raw
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert json.loads(collector.ledger_path.read_bytes())["days"][day]["observed_total"] == 19


def test_prior_completion_future_clock_guard_has_positive_control(storage):
    args = arguments(storage)
    changed = {**storage["lesson_completion"], "completed_ms": storage["clock"]["ms"] + 1}
    positional = (storage["parent"], originals(storage), args["service_receipt_raw"], args["tax_receipt_raw"],
                  args["tax_completion_raw"], args["configured_receipt_raw"], args["configured_completion_raw"],
                  args["lesson_receipt_raw"], packed(changed))
    with pytest.raises(ValueError, match="historical_lesson_completion"):
        upgrade._historical_lesson(*positional, now_ms=storage["clock"]["ms"])
    source = inspect.getsource(upgrade._historical_lesson)
    needle = 'and completion["completed_ms"] <= now_ms'
    assert source.count(needle) == 1
    scope = dict(upgrade._historical_lesson.__globals__)
    exec(compile(source.replace(needle, "and True", 1), "removed-historical-clock-guard", "exec"), scope)
    assert scope["_historical_lesson"](*positional, now_ms=storage["clock"]["ms"])[1]["completion_file_sha256"] == hashlib.sha256(packed(changed)).hexdigest()


def test_no_latch_is_readonly_and_pending_reviewed_hash_refuses(storage, monkeypatch, tmp_path):
    absent = tmp_path / "uncreated"
    assert upgrade.validate_upgrade(absent, {}, {}, now_ms=storage["clock"]["ms"]) is None
    assert not absent.exists()
    monkeypatch.setattr(upgrade, "WORKER_SOURCE", None)
    with pytest.raises(ValueError, match="reviewed_operational_sources_pending"):
        context(storage)


def test_retired_book_guard_remains_on_bound_routes(storage):
    value = storage
    ctx = context(value)
    path = value["books"] / next(row["book"] for row in value["receipt"]["legacy_books"] if row["lease"] == "retired")
    raw = path.read_bytes()
    path.write_bytes(raw + b"\n")
    assert ctx._revalidate_bindings() == ctx.snapshot_token
    runner = ctx.runner()
    before = snapshot(value["books"])
    for call in (lambda: runner.run_candidates(value["new"], [], {}, now_ms=value["clock"]["ms"]),
                 lambda: runner.expire_pending(value["manifest"], now_ms=value["clock"]["ms"]),
                 lambda: runner.watched_tokens(value["manifest"], now_ms=value["clock"]["ms"])):
        with pytest.raises(ValueError):
            call()
        assert snapshot(value["books"]) == before
