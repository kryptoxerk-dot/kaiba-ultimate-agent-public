"""Authenticated operational journal storage upgrade; no installation or history reset.

PROFIT-ADVANCE-20261027: the prior lesson-timing chain is authenticated as data.
MODEL9, economic evidence, authority, admissions and original artifacts are immutable.
"""
from __future__ import annotations

import ast
import copy
import os
from pathlib import Path

from kaiba.learning import lesson_timing_upgrade as lesson

configured, tax, service = lesson.configured, lesson.tax, lesson.service
VERSION = "journal-storage-upgrade/1"
COMMIT_VERSION = "journal-storage-commit/1"
POLICY = "closed-source32-journal-storage-hooks/1"
POINTER = "journal_storage_upgrade"
MIGRATION_POINTER = "journal_outbox_migration"
DIRECTORY = "journal-storage-upgrade"
CHANGED = ("kaiba/learning/allocation_runtime.py", "kaiba/learning/autolearn.py")
ADDITIONS = ("kaiba/learning/journal_storage_upgrade.py", "kaiba/learning/journal_outbox.py")
SOURCE32 = {**lesson.SOURCE31,
    CHANGED[0]: "c3c33e855e9b1ace3a530bccd80608cdf2c79a5c0a59ba98fba2056bb3a605de",
    CHANGED[1]: "f71b0a04b19bfb08798d7adde4f068556ecb30c7f0d1d62fc24627821583abce",
    "kaiba/learning/lesson_timing_upgrade.py": "ddddec189ce397807ac6605ba56b6991f145605eb546a0786080322ab34d9eea"}
# Exact reviewed operational files, filled only after the worker/helper review.
WORKER_SOURCE = "816aec537a554ce9155c05ddaa0b1ac52b35159495eb1a3baa230cbe59de25c6"
OUTBOX_SOURCE = "e37f85f9ed28da185b759f091f08f99a00af6abc2be7e5449a8ae206f6d72303"
MAX_JSON, MAX_SOURCE = lesson.MAX_JSON, lesson.MAX_SOURCE
_encoded, _hash, _sha = lesson._encoded, lesson._hash, lesson._sha
_dump, _definition, _position, _remove = lesson._dump, lesson._definition, lesson._position, lesson._remove
FALSE_FLAGS = ("original_artifacts_rewritten", "evidence_reset", "receipt_history_reset", "funding_allowed")
MIGRATION_FILES = ("receipt.json", "pending-lessons.original.json", "state.original.json", "economic-work.original.json")
# One successful pure recipe only. Keys are content digests, never file metadata;
# callers still read all artifacts and authenticate the current source graph.
_MIGRATION_CACHE = {}


def _require(ok, reason):
    if not ok:
        raise ValueError("journal_storage_upgrade:" + reason)


def _workspace_root():
    return Path(__file__).resolve().parents[2]


def _inverse_runtime(tree):
    version = _definition(tree, "source_versions")
    leaves = [n for n in ast.walk(version) if isinstance(n, ast.Tuple)
              and any(isinstance(e, ast.Constant) and e.value == "learning/allocation_runtime.py" for e in n.elts)]
    _require(len(leaves) == 1, "runtime_source_tuple")
    for target in ("learning/journal_storage_upgrade.py", "learning/journal_outbox.py"):
        found = [i for i, n in enumerate(leaves[0].elts) if isinstance(n, ast.Constant) and n.value == target]
        _require(len(found) == 1, "runtime_source_leaf_required")
        del leaves[0].elts[found[0]]
    init = _definition(tree, "Context.__init__")
    _require(_position(init.body, "self.journal_storage_upgrade = None") ==
             _position(init.body, "self.lesson_timing_upgrade = None") + 1, "context_init_order")
    _remove(init.body, "self.journal_storage_upgrade = None")
    loader = _definition(tree, "_load_context")
    stale = '_require(supplied.get(storage_upgrade.POINTER) == disk.get(storage_upgrade.POINTER) and supplied.get(storage_upgrade.MIGRATION_POINTER) == disk.get(storage_upgrade.MIGRATION_POINTER), "supplied_journal_storage_upgrade_stale")'
    capture = 'storage = storage_upgrade.validate_upgrade(root, disk, {"metadata.json": metadata_raw, "authority.json": authority_raw, "commit-receipt.json": completion_raw, "receipt.json": receipt_raw, "new-books.json": seal_raw}, now_ms=now_ms)'
    binding = 'if storage is not None:\n    context.journal_storage_upgrade = copy.deepcopy(storage)\n    context.snapshot_token = _hash({"base_authority": context.snapshot_token, "journal_storage_upgrade": storage})'
    prior_stale = _position(loader.body, '_require(supplied.get(lesson_upgrade.POINTER) == disk.get(lesson_upgrade.POINTER), "supplied_lesson_timing_upgrade_stale")')
    _require(_position(loader.body, stale) == prior_stale + 1
             and _position(loader.body, capture) == prior_stale + 2, "storage_validation_order")
    assignments = [n for n in loader.body if isinstance(n, ast.Assign)
                   and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "lesson"]
    _require(len(assignments) == 1 and isinstance(assignments[0].value, ast.IfExp)
             and loader.body.index(assignments[0]) == prior_stale + 3, "lesson_bridge_order_or_shape")
    value = assignments[0].value
    wanted = ast.parse('storage["lesson_upgrade"] if storage is not None else None', mode="eval").body
    _require(_dump(value.test) == _dump(wanted.test) and _dump(value.body) == _dump(wanted.body), "lesson_bridge_shape")
    assignments[0].value = value.orelse
    old_binding = 'if lesson is not None:\n    context.lesson_timing_upgrade = copy.deepcopy(lesson)\n    context.snapshot_token = _hash({"base_authority": context.snapshot_token, "lesson_timing_upgrade": lesson})'
    _require(_position(loader.body, binding) == _position(loader.body, old_binding) + 1, "storage_context_binding_order")
    following = loader.body[_position(loader.body, binding) + 1]
    _require(isinstance(following, ast.If) and _dump(following.test) == _dump(ast.Name(id="_full_guard", ctx=ast.Load())),
             "storage_binding_before_guard")
    for statement in ("from kaiba.learning import journal_storage_upgrade as storage_upgrade", stale, capture, binding):
        _remove(loader.body, statement)
    present = [n for n in loader.body if isinstance(n, ast.Assign)
               and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "present"]
    _require(len(present) == 1 and isinstance(present[0].value, ast.BoolOp), "runtime_presence_shape")
    for name in ("supplied", "disk"):
        wanted = _dump(ast.parse("storage_upgrade.POINTER in " + name, mode="eval").body)
        found = [i for i, n in enumerate(present[0].value.values) if _dump(n) == wanted]
        _require(len(found) == 1, "runtime_presence_required")
        del present[0].value.values[found[0]]


def _old_sources(old_sources):
    _require(isinstance(old_sources, dict) and set(old_sources) == set(CHANGED), "old_source_inventory")
    for name, raw in old_sources.items():
        _require(isinstance(raw, bytes) and len(raw) <= MAX_SOURCE and _sha(raw) == SOURCE32[name], "old_source_raw_hash")
    return {name: _sha(raw) for name, raw in old_sources.items()}


def current_sources(old_sources):
    """Require the exact operational worker/helper and the narrow runtime bridge."""
    _old_sources(old_sources)
    _require(all(isinstance(pin, str) and len(pin) == 64 for pin in (WORKER_SOURCE, OUTBOX_SOURCE)),
             "reviewed_operational_sources_pending")
    result, root = {}, _workspace_root()
    for name in (*SOURCE32, *ADDITIONS):
        raw = service._read(root / name, MAX_SOURCE)
        result[name] = _sha(raw.replace(b"\r\n", b"\n"))
        if name == CHANGED[0]:
            tree = ast.parse(raw.decode("utf-8-sig"))
            _inverse_runtime(tree)
            _require(_dump(tree) == _dump(ast.parse(old_sources[name].decode("utf-8-sig"))), "unclaimed_runtime_AST_change")
        elif name == CHANGED[1]:
            _require(result[name] == WORKER_SOURCE, "reviewed_worker_source_changed")
        elif name == ADDITIONS[1]:
            _require(result[name] == OUTBOX_SOURCE, "reviewed_outbox_source_changed")
        elif name in SOURCE32:
            _require(result[name] == SOURCE32[name], "frozen_source_changed:" + name)
    for name, wanted in service.MODEL9.items():
        _require(_sha(service._read(root / "kaiba" / name, MAX_SOURCE)) == wanted, "raw_model9_changed")
    return result


def _historical_lesson(parent, artifacts, service_raw, tax_raw, tax_completion_raw,
                       configured_raw, configured_completion_raw, lesson_raw, lesson_completion_raw, *, now_ms):
    _require(isinstance(lesson_raw, bytes) and len(lesson_raw) <= MAX_JSON
             and isinstance(lesson_completion_raw, bytes) and len(lesson_completion_raw) <= 8192,
             "historical_lesson_cap")
    receipt = service._object(lesson_raw)
    prepared = receipt.get("prepared_ms")
    _require(type(prepared) is int and type(now_ms) is int and 0 < prepared <= now_ms, "historical_lesson_clock")
    original, historical = lesson._historical_configured(parent, artifacts, service_raw, tax_raw,
        tax_completion_raw, configured_raw, configured_completion_raw, now_ms=prepared)
    expected = {"version": lesson.VERSION, "prepared_ms": prepared, "original": original,
        "configured_upgrade": {"pointer": copy.deepcopy(parent[configured.POINTER]),
                               "receipt_file_sha256": historical["receipt_file_sha256"],
                               "completion_file_sha256": historical["completion_file_sha256"]},
        "tax_upgrade": {"pointer": copy.deepcopy(parent[tax.POINTER]),
                        "receipt_file_sha256": historical["tax_upgrade"]["receipt_file_sha256"],
                        "completion_file_sha256": historical["tax_upgrade"]["completion_file_sha256"]},
        "service_upgrade": {"pointer": copy.deepcopy(parent[service.POINTER]),
                            "receipt_file_sha256": historical["service_upgrade"]["receipt_file_sha256"]},
        "old_sources": dict(lesson.SOURCE31), "new_sources": dict(SOURCE32),
        "old_changed_files": {name: lesson.SOURCE31[name] for name in lesson.CHANGED},
        "raw_model9": dict(service.MODEL9), "ast_policy": lesson.POLICY,
        **dict.fromkeys(lesson.FALSE_FLAGS, False)}
    expected["receipt_sha256"] = _hash(expected)
    _require(receipt == expected and all(receipt.get(n) is False for n in lesson.FALSE_FLAGS), "historical_lesson_receipt")
    _require(parent.get(lesson.POINTER) == lesson.pointer_for(lesson_raw), "historical_lesson_pointer")
    completion = service._object(lesson_completion_raw)
    _require(completion == lesson.completion_for(lesson_raw, visible_ms=completion.get("visible_ms"), completed_ms=completion.get("completed_ms"))
             and type(completion["completed_ms"]) is int and completion["completed_ms"] <= now_ms,
             "historical_lesson_completion")
    return original, {"receipt_file_sha256": _sha(lesson_raw), "completion_file_sha256": _sha(lesson_completion_raw),
        "source_graph_sha256": _hash(SOURCE32), "original_runtime_sources": service.original_runtime_sources(),
        "configured_upgrade": historical, "tax_upgrade": historical["tax_upgrade"],
        "service_upgrade": historical["service_upgrade"]}


def pointer_for(receipt_raw):
    receipt = service._object(receipt_raw)
    return {"version": VERSION, "receipt_file_sha256": _sha(receipt_raw), "receipt_sha256": receipt["receipt_sha256"],
        "source_graph_sha256": _hash(receipt["new_sources"]), "activation_ms": receipt["original"]["activation_ms"],
        "funding_allowed": False}


def _migration(parent, artifacts, *, prepared_ms, helper_source_sha256):
    """Replay only the pure recipe over authenticated data, never historical code."""
    global _MIGRATION_CACHE

    from kaiba.learning import journal_outbox as outbox

    _require(isinstance(artifacts, dict) and set(artifacts) == set(MIGRATION_FILES), "migration_artifact_inventory")
    caps = (MAX_JSON, outbox.MAX_ARCHIVE, outbox.MAX_STATE_INPUT, outbox.MAX_ECONOMIC_INPUT)
    for name, cap in zip(MIGRATION_FILES, caps, strict=True):
        _require(isinstance(artifacts[name], bytes) and len(artifacts[name]) <= cap, "migration_artifact_cap")
    receipt = outbox._object(artifacts["receipt.json"], MAX_JSON)
    stamp = receipt.get("prepared_ms")
    _require(type(stamp) is int and 0 < stamp <= prepared_ms, "migration_clock")
    _require(helper_source_sha256 == OUTBOX_SOURCE, "migration_helper_source")
    hashes = {name: _sha(artifacts[name]) for name in MIGRATION_FILES}
    key = (helper_source_sha256, *(hashes[name] for name in MIGRATION_FILES))
    result = _MIGRATION_CACHE.get(key)
    if result is None:
        expected = outbox.prepare_migration(artifacts["state.original.json"], artifacts["economic-work.original.json"],
                                            prepared_ms=stamp)
        _require(expected["receipt_raw"] == artifacts["receipt.json"]
                 and expected["archive_raw"] == artifacts["pending-lessons.original.json"], "migration_recipe_or_archive_changed")
        replacement = outbox._object(expected["state_raw"], outbox.MAX_STATE_OUTPUT)
        result = {"pointer": replacement[outbox.MIGRATION_POINTER], "receipt": receipt, "artifact_sha256": hashes}
        _require(parent.get(outbox.MIGRATION_POINTER) == result["pointer"], "parent_migration_binding")
        _MIGRATION_CACHE = {key: copy.deepcopy(result)}
    _require(parent.get(outbox.MIGRATION_POINTER) == result["pointer"], "parent_migration_binding")
    return copy.deepcopy(result)


def prepare_upgrade(parent, artifacts, *, service_receipt_raw, tax_receipt_raw, tax_completion_raw,
                    configured_receipt_raw, configured_completion_raw, lesson_receipt_raw,
                    lesson_completion_raw, old_sources, migration_artifacts, prepared_ms):
    """Pure explicit installation recipe; no pending lessons or credits are changed."""
    _require(type(prepared_ms) is int and prepared_ms > 0, "integer_clock_required")
    _require(POINTER not in parent, "already_latched")
    original, historical = _historical_lesson(parent, artifacts, service_receipt_raw, tax_receipt_raw,
        tax_completion_raw, configured_receipt_raw, configured_completion_raw, lesson_receipt_raw,
        lesson_completion_raw, now_ms=prepared_ms)
    sources = current_sources(old_sources)
    receipt = {"version": VERSION, "prepared_ms": prepared_ms, "original": original,
        "lesson_upgrade": {"pointer": copy.deepcopy(parent[lesson.POINTER]),
                           "receipt_file_sha256": historical["receipt_file_sha256"],
                           "completion_file_sha256": historical["completion_file_sha256"]},
        "migration": _migration(parent, migration_artifacts, prepared_ms=prepared_ms,
                                 helper_source_sha256=sources[ADDITIONS[1]]),
        "old_sources": dict(SOURCE32), "new_sources": sources,
        "old_changed_files": _old_sources(old_sources), "raw_model9": dict(service.MODEL9), "ast_policy": POLICY,
        **dict.fromkeys(FALSE_FLAGS, False)}
    receipt["receipt_sha256"] = _hash(receipt)
    raw = _encoded(receipt) + b"\n"
    _require(len(raw) <= MAX_JSON, "receipt_cap")
    return {"receipt": receipt, "receipt_raw": raw, "pointer": pointer_for(raw)}


def completion_for(receipt_raw, *, visible_ms, completed_ms):
    receipt = service._object(receipt_raw)
    _require(type(visible_ms) is int and type(completed_ms) is int and type(receipt["prepared_ms"]) is int
             and receipt["prepared_ms"] <= visible_ms <= completed_ms, "completion_clock")
    return {"version": COMMIT_VERSION, "receipt_file_sha256": _sha(receipt_raw), "receipt_sha256": receipt["receipt_sha256"],
        "source_graph_sha256": _hash(receipt["new_sources"]), "activation_ms": receipt["original"]["activation_ms"],
        "prepared_ms": receipt["prepared_ms"], "visible_ms": visible_ms, "completed_ms": completed_ms}


def validate_upgrade(state_dir, parent, artifacts, *, now_ms):
    """Either latch requires the authenticated prior chain and completed new receipt."""
    directory = Path(state_dir) / "research-allocation" / DIRECTORY
    if POINTER not in parent and not os.path.lexists(directory):
        return None
    _require(type(now_ms) is int and now_ms > 0, "integer_clock_required")
    _require(POINTER in parent and directory.is_dir() and not directory.is_symlink(), "upgrade_latch_missing_or_conflicting")
    raw = service._read(directory / "receipt.json")
    receipt = service._object(raw)
    _require(parent[POINTER] == pointer_for(raw), "parent_upgrade_binding")
    _require(all(receipt.get(n) is False for n in FALSE_FLAGS), "upgrade_authority_claim")
    own = Path(state_dir) / "research-allocation"
    service_raw = service._read(own / service.DIRECTORY / "receipt.json")
    tax_raw = service._read(own / tax.DIRECTORY / "receipt.json")
    tax_completion = service._read(own / tax.DIRECTORY / "commit-receipt.json", 8192)
    configured_raw = service._read(own / configured.DIRECTORY / "receipt.json")
    configured_completion = service._read(own / configured.DIRECTORY / "commit-receipt.json", 8192)
    lesson_raw = service._read(own / lesson.DIRECTORY / "receipt.json")
    lesson_completion = service._read(own / lesson.DIRECTORY / "commit-receipt.json", 8192)
    tax._old_sources({name: service._read(own / tax.DIRECTORY / "source28" / name, MAX_SOURCE) for name in tax.CHANGED})
    configured._old_sources({name: service._read(own / configured.DIRECTORY / "source30" / name, MAX_SOURCE) for name in configured.CHANGED})
    lesson._old_sources({name: service._read(own / lesson.DIRECTORY / "source31" / name, MAX_SOURCE) for name in lesson.CHANGED})
    old_sources = {name: service._read(directory / "source32" / name, MAX_SOURCE) for name in CHANGED}
    from kaiba.learning import journal_outbox as outbox

    caps = (MAX_JSON, outbox.MAX_ARCHIVE, outbox.MAX_STATE_INPUT, outbox.MAX_ECONOMIC_INPUT)
    migration_artifacts = {name: service._read(directory / "migration" / name, cap)
                           for name, cap in zip(MIGRATION_FILES, caps, strict=True)}
    expected = prepare_upgrade({k: v for k, v in parent.items() if k != POINTER}, artifacts,
        service_receipt_raw=service_raw, tax_receipt_raw=tax_raw, tax_completion_raw=tax_completion,
        configured_receipt_raw=configured_raw, configured_completion_raw=configured_completion,
        lesson_receipt_raw=lesson_raw, lesson_completion_raw=lesson_completion,
        old_sources=old_sources, migration_artifacts=migration_artifacts, prepared_ms=receipt["prepared_ms"])
    _require(receipt == expected["receipt"] and receipt["prepared_ms"] <= now_ms, "receipt_or_source_graph_changed")
    completion_raw = service._read(directory / "commit-receipt.json", 8192)
    completion = service._object(completion_raw)
    _require(completion == completion_for(raw, visible_ms=completion.get("visible_ms"), completed_ms=completion.get("completed_ms"))
             and completion["completed_ms"] <= now_ms, "completion_required")
    _, historical = _historical_lesson(parent, artifacts, service_raw, tax_raw, tax_completion,
        configured_raw, configured_completion, lesson_raw, lesson_completion, now_ms=now_ms)
    return {"receipt_file_sha256": _sha(raw), "completion_file_sha256": _sha(completion_raw),
        "source_graph_sha256": _hash(receipt["new_sources"]), "original_runtime_sources": service.original_runtime_sources(),
        "lesson_upgrade": historical}


__all__ = ["prepare_upgrade", "pointer_for", "completion_for", "validate_upgrade", "current_sources", "VERSION"]
