"""Install the reviewed journal-only source transition. Default: read-only preflight.

The private bundle identifies committed source bytes and exact observed inputs.
No provider, canonical database, timer, risk or financial action is performed here.
Existing paper books are validated read-only through the original authority guard. Partial
installation is retained as evidence and is never silently retried or reset.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path

CHANGED = ("kaiba/learning/allocation_runtime.py", "kaiba/learning/autolearn.py")
ADDED = ("kaiba/learning/journal_storage_upgrade.py", "kaiba/learning/journal_outbox.py")
SOURCES = (*CHANGED, *ADDED)
INPUTS = ("state.json", "economic-work.json")
OBSERVER = ("state.json", "quote-budget-ledger.json", "observer-upgrade.json")
DIRECTORY = "journal-storage-upgrade"


def require(ok, why):
    if not ok:
        raise ValueError("journal_install:" + why)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def safe(path):
    path = Path(path).absolute()
    require(not any(p.is_symlink() for p in (path, *path.parents)), "symlink_refused")
    return path


def read(path, cap=2 * 1024 * 1024):
    path = safe(path)
    before = path.stat()
    require(path.is_file() and before.st_size <= cap, "file_cap_or_type")
    with path.open("rb") as stream:
        raw = stream.read(cap + 1)
    after = path.stat()
    require(len(raw) <= cap and (before.st_ino, before.st_size, before.st_mtime_ns)
            == (after.st_ino, after.st_size, after.st_mtime_ns), "changed_during_read")
    return raw


def sync_dir(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def durable_directory(path):
    """Persist each new ancestor before an immutable backup can depend on it."""
    path = safe(path)
    if path.exists():
        require(path.is_dir(), "directory_type")
        return
    durable_directory(path.parent)
    path.mkdir(mode=0o700)
    sync_dir(path)
    sync_dir(path.parent)


def write_once(path, raw, mode=0o600):
    path = safe(path)
    durable_directory(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    with os.fdopen(fd, "wb") as stream:
        if os.name != "nt":
            os.fchmod(stream.fileno(), mode)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    sync_dir(path.parent)


def replace(path, raw, *, expected, mode=None):
    path = safe(path)
    require((read(path, 16 * 1024 * 1024) if path.exists() else None) == expected, "replace_CAS")
    if mode is None:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    temporary = path.with_name(path.name + ".journal-storage.partial")
    write_once(temporary, raw, mode)
    require((read(path, 16 * 1024 * 1024) if path.exists() else None) == expected, "late_replace_CAS")
    os.replace(temporary, path)
    sync_dir(path.parent)
    require(read(path, 16 * 1024 * 1024) == raw, "replace_verification")


def preflight(root, bundle):
    root = safe(root).resolve()
    require(root.is_dir() and isinstance(bundle, dict) and set(bundle) == {"commit", "sources", "inputs", "observer"}, "bundle_shape")
    require(isinstance(bundle["commit"], str) and len(bundle["commit"]) == 40, "commit_required")
    require(set(bundle["sources"]) == set(SOURCES) and set(bundle["inputs"]) == set(INPUTS)
            and set(bundle["observer"]) == set(OBSERVER), "bundle_inventory")
    state = root / "data/learning/autolearn"
    observer = root / "data/learning/prospective-quotes"
    directory = state / "research-allocation" / DIRECTORY
    require(not os.path.lexists(directory), "existing_installation_requires_review")
    prior, proposed = {}, {}
    for name in SOURCES:
        spec = bundle["sources"][name]
        require(set(spec) == {"before", "after", "base64"}, "source_spec")
        raw = base64.b64decode(spec["base64"], validate=True)
        require(len(raw) <= 1024 * 1024 and sha(raw) == spec["after"], "source_payload_hash")
        compile(raw, name, "exec")
        path = safe(root / name)
        old = read(path, 1024 * 1024) if path.exists() else None
        require((sha(old) if old is not None else None) == spec["before"], "source_baseline_changed:" + name)
        require((name in CHANGED) == (old is not None), "source_existence")
        prior[name], proposed[name] = old, raw
    inputs = {name: read(state / name, 16 * 1024 * 1024 if name == "economic-work.json" else 4 * 1024 * 1024) for name in INPUTS}
    observation = {name: read(observer / name) for name in OBSERVER}
    require({k: sha(v) for k, v in inputs.items()} == bundle["inputs"], "parent_inputs_changed")
    require({k: sha(v) for k, v in observation.items()} == bundle["observer"], "observer_inputs_changed")
    return root, state, observer, directory, prior, proposed, inputs, observation


def validate_environment(root):
    """Resolve the observer's accounting configuration before any mutation."""
    sys.path.insert(0, str(root))
    from kaiba.core.config import get_settings

    settings = get_settings()
    require(settings.kaiba_data_dir.resolve() == root / "data", "configured_data_root_mismatch")
    require(settings.db_path.resolve() == root / "data/kaiba.db", "configured_database_mismatch")


def install(root, bundle, *, apply=False):
    values = preflight(root, bundle)
    root, state, observer, directory, prior, proposed, inputs, observation = values
    validate_environment(root)
    if not apply:
        return {"status": "preflight_only", "commit": bundle["commit"], "source_files": list(SOURCES),
                "input_hashes_match": True, "writes": 0, "full_recipe_validation": "performed during locked apply"}
    held = []
    try:
        for path, value in ((state / "worker.lock", {"pid": os.getpid(), "started_ms": int(time.time() * 1000)}),
                            (observer / "worker.lock", {"pid": os.getpid()})):
            raw = encode(value)
            write_once(path, raw)
            held.append((path, raw, path.stat().st_ino))
        values = preflight(root, bundle)
        root, state, observer, directory, prior, proposed, inputs, observation = values
        durable_directory(directory)
        own = state / "research-allocation"
        original_names = ("metadata.json", "authority.json", "commit-receipt.json", "receipt.json", "new-books.json")
        artifacts = {n: read(own / n) for n in original_names}
        for name in CHANGED:
            write_once(directory / "source32" / name, prior[name])
        for name, raw in observation.items():
            write_once(directory / "observer-original" / name, raw)
        write_once(directory / "state.before.json", inputs["state.json"])
        write_once(directory / "install-intent.json", encode({"commit": bundle["commit"], "sources": {n: sha(v) for n, v in proposed.items()},
            "inputs": bundle["inputs"], "observer": bundle["observer"], "funding_allowed": False}))
        for name in SOURCES:
            replace(root / name, proposed[name], expected=prior[name], mode=None if name in CHANGED else 0o644)
        sys.path.insert(0, str(root))
        from kaiba.learning import (
            allocation_runtime,
            journal_outbox,
            journal_storage_upgrade,
            observer_upgrade,
        )

        stamp = int(time.time() * 1000)
        migration = journal_outbox.prepare_migration(inputs["state.json"], inputs["economic-work.json"], prepared_ms=stamp)
        migration_files = dict(zip(journal_storage_upgrade.MIGRATION_FILES,
            (migration["receipt_raw"], migration["archive_raw"], inputs["state.json"], inputs["economic-work.json"]), strict=True))
        parent = json.loads(migration["state_raw"])
        def old_receipt(folder, name="receipt.json"):
            return read(own / folder / name)
        plan = journal_storage_upgrade.prepare_upgrade(parent, artifacts,
            service_receipt_raw=old_receipt("paper-service-upgrade"),
            tax_receipt_raw=old_receipt("tax-quote-upgrade"), tax_completion_raw=old_receipt("tax-quote-upgrade", "commit-receipt.json"),
            configured_receipt_raw=old_receipt("configured-receipts-upgrade"), configured_completion_raw=old_receipt("configured-receipts-upgrade", "commit-receipt.json"),
            lesson_receipt_raw=old_receipt("lesson-timing-upgrade"), lesson_completion_raw=old_receipt("lesson-timing-upgrade", "commit-receipt.json"),
            old_sources={n: prior[n] for n in CHANGED}, migration_artifacts=migration_files, prepared_ms=stamp)
        for name, raw in migration_files.items():
            write_once(directory / "migration" / name, raw)
        write_once(directory / "receipt.json", plan["receipt_raw"])
        require(read(state / "economic-work.json", 16 * 1024 * 1024) == inputs["economic-work.json"], "outbox_changed")
        parent[journal_storage_upgrade.POINTER] = plan["pointer"]
        parent_raw = journal_outbox.encode_state(encode(parent).decode())
        replace(state / "state.json", parent_raw, expected=inputs["state.json"])
        visible = int(time.time() * 1000)
        completed = journal_storage_upgrade.completion_for(plan["receipt_raw"], visible_ms=visible, completed_ms=int(time.time() * 1000))
        write_once(directory / "commit-receipt.json", encode(completed))
        ctx = allocation_runtime.load_context(state, learning_root=root / "data/learning", state=parent, now_ms=int(time.time() * 1000))
        require(ctx is not None and ctx.journal_storage_upgrade is not None, "context_not_validated")
        old_manifest = json.loads(observation["observer-upgrade.json"])
        new_manifest = dict(old_manifest)
        new_manifest["source_bindings"] = observer_upgrade.source_bindings()
        new_manifest["allocation_snapshot_token"] = ctx.snapshot_token
        old_bindings, new_bindings = old_manifest["source_bindings"], new_manifest["source_bindings"]
        require(set(new_bindings) - set(old_bindings) == {n.removeprefix("kaiba/") for n in ADDED}
                and not set(old_bindings) - set(new_bindings), "observer_source_inventory_changed")
        require({n for n in old_bindings if old_bindings[n] != new_bindings[n]}
                == {n.removeprefix("kaiba/") for n in CHANGED}, "observer_unclaimed_source_delta")
        historical = allocation_runtime._hash({"pointer": parent[allocation_runtime.POINTER], "metadata": ctx.metadata,
            "authority": ctx.authority, "completion_file_sha256": sha(artifacts["commit-receipt.json"]),
            "paper_service_upgrade": ctx.service_upgrade})
        for key in ("tax_quote_upgrade", "configured_receipts_upgrade", "lesson_timing_upgrade"):
            value = getattr(ctx, key)
            if value is not None:
                historical = allocation_runtime._hash({"base_authority": historical, key: value})
        require(old_manifest["allocation_snapshot_token"] == historical, "observer_original_context_changed")
        for name in OBSERVER:
            require(read(observer / name) == observation[name], "observer_state_or_admission_changed")
        for name, raw in artifacts.items():
            require(read(own / name) == raw, "original_authority_changed")
        replace(observer / "observer-upgrade.json", encode(new_manifest), expected=observation["observer-upgrade.json"])
        collector = observer_upgrade.BudgetedQuoteCollector(state, observer, learning_root=root / "data/learning",
            db_path=root / "data/kaiba.db", provider=object())
        require(collector._declaration() == new_manifest, "observer_declaration_failed")
        require(all(read(observer / n) == observation[n] for n in ("state.json", "quote-budget-ledger.json")), "observer_budget_changed")
        require(read(state / "economic-work.json", 16 * 1024 * 1024) == inputs["economic-work.json"], "economic_outbox_changed")
        require(all(sha(read(root / n, 1024 * 1024)) == sha(proposed[n]) for n in SOURCES), "installed_source_changed")
        report = {"status": "installed", "commit": bundle["commit"], "completed_ms": int(time.time() * 1000),
            "state_sha256": sha(parent_raw), "state_bytes": len(parent_raw), "original_observations": migration["receipt"]["original_observations"],
            "independent_revisions": migration["receipt"]["independent_revision_count"], "diagnostic_variants": migration["receipt"]["distinct_diagnostic_variants"],
            "observer_state_unchanged": True, "observer_ledger_unchanged": True, "original_authority_unchanged": True,
            "economic_outbox_unchanged": True, "provider_calls": 0, "canonical_database_queries": 0,
            "existing_paper_books_validated_read_only": True, "funding_allowed": False,
            "observer_manifest_before_sha256": sha(observation["observer-upgrade.json"]), "observer_manifest_after_sha256": sha(encode(new_manifest)),
            "source_hashes": {n: sha(v) for n, v in proposed.items()}}
        write_once(directory / "install-complete.json", encode(report))
        return report
    finally:
        for path, raw, inode in reversed(held):
            if path.exists() and path.stat().st_ino == inode and read(path, 4096) == raw:
                path.unlink()
                sync_dir(path.parent)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    report = install(args.root, json.loads(read(args.bundle, 4 * 1024 * 1024)), apply=args.apply)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
