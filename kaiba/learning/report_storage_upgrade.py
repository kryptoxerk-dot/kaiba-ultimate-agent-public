"""Authenticate a persisted-report projection without rewriting experiment history.

REPORT-STORAGE-UPGRADE-20261029: SOURCE34 is immutable historical data. Only the
terminal writer hook and this runtime binding may differ; MODEL9 stays exact.
"""
from __future__ import annotations

import ast
import copy
import os
from pathlib import Path

from kaiba.learning import journal_storage_upgrade as storage

lesson, configured, tax, service = storage.lesson, storage.configured, storage.tax, storage.service
VERSION = "report-storage-upgrade/1"
COMMIT_VERSION = "report-storage-commit/1"
POLICY = "closed-source34-terminal-report-projection/1"
POINTER = "report_storage_upgrade"
DIRECTORY = "report-storage-upgrade"
CHANGED = storage.CHANGED
ADDITIONS = ("kaiba/learning/report_storage_upgrade.py", "kaiba/learning/research_report.py")
SOURCE34 = {**storage.SOURCE32,
    CHANGED[0]: "bdc5825a6c3d73868fe2e1c56b3fe47807069a30e5f8461f453882ba20a9c250",
    CHANGED[1]: storage.WORKER_SOURCE,
    storage.ADDITIONS[0]: "17612f63fed712f3e62e7296b1e28726eb9b19289c48aae313678d57c7fb8d29",
    storage.ADDITIONS[1]: storage.OUTBOX_SOURCE}
REPORT_SOURCE = "c3b4dc9668d7f0f9d236eb8fb0d581fd4100036799d86c2390f52094c96c40c5"
MAX_JSON, MAX_SOURCE = storage.MAX_JSON, storage.MAX_SOURCE
FALSE_FLAGS = storage.FALSE_FLAGS
_encoded, _hash, _sha = storage._encoded, storage._hash, storage._sha
_dump, _definition, _position, _remove = storage._dump, storage._definition, storage._position, storage._remove


def _require(ok, reason):
    if not ok:
        raise ValueError("report_storage_upgrade:" + reason)


def _workspace_root():
    return Path(__file__).resolve().parents[2]


def _inverse_worker(tree):
    writer = _definition(tree, "atomic_json")
    hook = ('if path.name == "latest-report.json" and isinstance(value, Mapping) and "campaigns" in value:\n'
            '    from kaiba.learning.research_report import compact_report\n'
            '    value = compact_report(value)')
    encode = 'text = json.dumps(value, indent=2, sort_keys=True, default=str, allow_nan=False)'
    _require(_position(writer.body, hook) + 1 == _position(writer.body, encode), "terminal_writer_hook_required")
    _remove(writer.body, hook)


def _inverse_runtime(tree):
    version = _definition(tree, "source_versions")
    leaves = [n for n in ast.walk(version) if isinstance(n, ast.Tuple)
              and any(isinstance(e, ast.Constant) and e.value == "learning/allocation_runtime.py" for e in n.elts)]
    _require(len(leaves) == 1, "runtime_source_tuple")
    for target in (n.removeprefix("kaiba/") for n in ADDITIONS):
        found = [i for i, n in enumerate(leaves[0].elts) if isinstance(n, ast.Constant) and n.value == target]
        _require(len(found) == 1, "runtime_source_leaf_required")
        del leaves[0].elts[found[0]]
    init = _definition(tree, "Context.__init__")
    _require(_position(init.body, "self.report_storage_upgrade = None") ==
             _position(init.body, "self.journal_storage_upgrade = None") + 1, "context_init_order")
    _remove(init.body, "self.report_storage_upgrade = None")
    loader = _definition(tree, "_load_context")
    stale = '_require(supplied.get(report_upgrade.POINTER) == disk.get(report_upgrade.POINTER), "supplied_report_storage_upgrade_stale")'
    capture = 'report = report_upgrade.validate_upgrade(root, disk, {"metadata.json": metadata_raw, "authority.json": authority_raw, "commit-receipt.json": completion_raw, "receipt.json": receipt_raw, "new-books.json": seal_raw}, now_ms=now_ms)'
    old_stale = '_require(supplied.get(storage_upgrade.POINTER) == disk.get(storage_upgrade.POINTER) and supplied.get(storage_upgrade.MIGRATION_POINTER) == disk.get(storage_upgrade.MIGRATION_POINTER), "supplied_journal_storage_upgrade_stale")'
    prior = _position(loader.body, old_stale)
    _require(_position(loader.body, stale) == prior + 1 and _position(loader.body, capture) == prior + 2,
             "report_validation_order")
    assignments = [n for n in loader.body if isinstance(n, ast.Assign)
                   and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "storage"]
    _require(len(assignments) == 1 and isinstance(assignments[0].value, ast.IfExp)
             and loader.body.index(assignments[0]) == prior + 3, "storage_bridge_order_or_shape")
    value = assignments[0].value
    wanted = ast.parse('report["storage_upgrade"] if report is not None else None', mode="eval").body
    _require(_dump(value.test) == _dump(wanted.test) and _dump(value.body) == _dump(wanted.body), "storage_bridge_shape")
    assignments[0].value = value.orelse
    binding = 'if report is not None:\n    context.report_storage_upgrade = copy.deepcopy(report)\n    context.snapshot_token = _hash({"base_authority": context.snapshot_token, "report_storage_upgrade": report})'
    old_binding = 'if storage is not None:\n    context.journal_storage_upgrade = copy.deepcopy(storage)\n    context.snapshot_token = _hash({"base_authority": context.snapshot_token, "journal_storage_upgrade": storage})'
    _require(_position(loader.body, binding) == _position(loader.body, old_binding) + 1, "report_context_binding_order")
    following = loader.body[_position(loader.body, binding) + 1]
    _require(isinstance(following, ast.If) and _dump(following.test) == _dump(ast.Name(id="_full_guard", ctx=ast.Load())),
             "report_binding_before_guard")
    for statement in ("from kaiba.learning import report_storage_upgrade as report_upgrade", stale, capture, binding):
        _remove(loader.body, statement)
    present = [n for n in loader.body if isinstance(n, ast.Assign)
               and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "present"]
    _require(len(present) == 1 and isinstance(present[0].value, ast.BoolOp), "runtime_presence_shape")
    for name in ("supplied", "disk"):
        wanted = _dump(ast.parse("report_upgrade.POINTER in " + name, mode="eval").body)
        found = [i for i, n in enumerate(present[0].value.values) if _dump(n) == wanted]
        _require(len(found) == 1, "runtime_presence_required")
        del present[0].value.values[found[0]]


def _old_sources(old_sources):
    _require(isinstance(old_sources, dict) and set(old_sources) == set(CHANGED), "old_source_inventory")
    for name, raw in old_sources.items():
        _require(isinstance(raw, bytes) and len(raw) <= MAX_SOURCE and _sha(raw) == SOURCE34[name], "old_source_raw_hash")
    return {name: _sha(raw) for name, raw in old_sources.items()}


def current_sources(old_sources):
    _old_sources(old_sources)
    result, root = {}, _workspace_root()
    for name in (*SOURCE34, *ADDITIONS):
        raw = service._read(root / name, MAX_SOURCE)
        result[name] = _sha(raw.replace(b"\r\n", b"\n"))
        if name in CHANGED:
            tree = ast.parse(raw.decode("utf-8-sig"))
            (_inverse_runtime if name == CHANGED[0] else _inverse_worker)(tree)
            _require(_dump(tree) == _dump(ast.parse(old_sources[name].decode("utf-8-sig"))), "unclaimed_AST_change:" + name)
        elif name == ADDITIONS[1]:
            _require(result[name] == REPORT_SOURCE, "reviewed_report_source_changed")
        elif name in SOURCE34:
            _require(result[name] == SOURCE34[name], "frozen_source_changed:" + name)
    for name, wanted in service.MODEL9.items():
        _require(_sha(service._read(root / "kaiba" / name, MAX_SOURCE)) == wanted, "raw_model9_changed")
    return result


def historical_storage(state_dir, parent, artifacts, *, now_ms):
    """Reconstruct the exact old receipt from data; never execute archived code."""
    own = Path(state_dir) / "research-allocation"
    directory = own / storage.DIRECTORY
    raw = service._read(directory / "receipt.json")
    receipt = service._object(raw)
    stamp = receipt.get("prepared_ms")
    _require(type(stamp) is int and type(now_ms) is int and 0 < stamp <= now_ms, "historical_storage_clock")
    def read_old(module, name="receipt.json", cap=MAX_JSON):
        return service._read(own / module.DIRECTORY / name, cap)
    for module, source_dir in ((tax, "source28"), (configured, "source30"), (lesson, "source31"), (storage, "source32")):
        module._old_sources({name: read_old(module, source_dir + "/" + name, MAX_SOURCE) for name in module.CHANGED})
    original, historical = storage._historical_lesson(parent, artifacts,
        read_old(service), read_old(tax), read_old(tax, "commit-receipt.json", 8192),
        read_old(configured), read_old(configured, "commit-receipt.json", 8192),
        read_old(lesson), read_old(lesson, "commit-receipt.json", 8192), now_ms=stamp)
    from kaiba.learning import journal_outbox as outbox
    caps = (MAX_JSON, outbox.MAX_ARCHIVE, outbox.MAX_STATE_INPUT, outbox.MAX_ECONOMIC_INPUT)
    migration = {name: service._read(directory / "migration" / name, cap)
                 for name, cap in zip(storage.MIGRATION_FILES, caps, strict=True)}
    expected = {"version": storage.VERSION, "prepared_ms": stamp, "original": original,
        "lesson_upgrade": {"pointer": copy.deepcopy(parent[lesson.POINTER]),
                           "receipt_file_sha256": historical["receipt_file_sha256"],
                           "completion_file_sha256": historical["completion_file_sha256"]},
        "migration": storage._migration(parent, migration, prepared_ms=stamp, helper_source_sha256=storage.OUTBOX_SOURCE),
        "old_sources": dict(storage.SOURCE32), "new_sources": dict(SOURCE34),
        "old_changed_files": {name: storage.SOURCE32[name] for name in storage.CHANGED},
        "raw_model9": dict(service.MODEL9), "ast_policy": storage.POLICY,
        **dict.fromkeys(storage.FALSE_FLAGS, False)}
    expected["receipt_sha256"] = _hash(expected)
    _require(receipt == expected and parent.get(storage.POINTER) == storage.pointer_for(raw), "historical_storage_receipt_or_pointer")
    completed_raw = service._read(directory / "commit-receipt.json", 8192)
    completed = service._object(completed_raw)
    _require(completed == storage.completion_for(raw, visible_ms=completed.get("visible_ms"), completed_ms=completed.get("completed_ms"))
             and completed["completed_ms"] <= now_ms, "historical_storage_completion")
    return original, {"receipt_file_sha256": _sha(raw), "completion_file_sha256": _sha(completed_raw),
        "source_graph_sha256": _hash(SOURCE34), "original_runtime_sources": service.original_runtime_sources(),
        "lesson_upgrade": historical}


def prepare_upgrade(state_dir, parent, artifacts, *, old_sources, prepared_ms):
    _require(type(prepared_ms) is int and prepared_ms > 0, "integer_clock_required")
    _require(POINTER not in parent, "already_latched")
    original, historical = historical_storage(state_dir, parent, artifacts, now_ms=prepared_ms)
    sources = current_sources(old_sources)
    receipt = {"version": VERSION, "prepared_ms": prepared_ms, "original": original,
        "storage_upgrade": {"pointer": copy.deepcopy(parent[storage.POINTER]),
                            "receipt_file_sha256": historical["receipt_file_sha256"],
                            "completion_file_sha256": historical["completion_file_sha256"]},
        "old_sources": dict(SOURCE34), "new_sources": sources, "old_changed_files": _old_sources(old_sources),
        "raw_model9": dict(service.MODEL9), "ast_policy": POLICY, **dict.fromkeys(FALSE_FLAGS, False)}
    receipt["receipt_sha256"] = _hash(receipt)
    raw = _encoded(receipt) + b"\n"
    _require(len(raw) <= MAX_JSON, "receipt_cap")
    return {"receipt": receipt, "receipt_raw": raw, "pointer": pointer_for(raw)}


def pointer_for(receipt_raw):
    receipt = service._object(receipt_raw)
    return {"version": VERSION, "receipt_file_sha256": _sha(receipt_raw), "receipt_sha256": receipt["receipt_sha256"],
        "source_graph_sha256": _hash(receipt["new_sources"]), "activation_ms": receipt["original"]["activation_ms"],
        "funding_allowed": False}


def completion_for(receipt_raw, *, visible_ms, completed_ms):
    receipt = service._object(receipt_raw)
    _require(type(visible_ms) is int and type(completed_ms) is int and type(receipt["prepared_ms"]) is int
             and receipt["prepared_ms"] <= visible_ms <= completed_ms, "completion_clock")
    return {"version": COMMIT_VERSION, "receipt_file_sha256": _sha(receipt_raw), "receipt_sha256": receipt["receipt_sha256"],
        "source_graph_sha256": _hash(receipt["new_sources"]), "activation_ms": receipt["original"]["activation_ms"],
        "prepared_ms": receipt["prepared_ms"], "visible_ms": visible_ms, "completed_ms": completed_ms}


def validate_upgrade(state_dir, parent, artifacts, *, now_ms):
    directory = Path(state_dir) / "research-allocation" / DIRECTORY
    if POINTER not in parent and not os.path.lexists(directory):
        return None
    _require(type(now_ms) is int and now_ms > 0, "integer_clock_required")
    _require(POINTER in parent and directory.is_dir() and not directory.is_symlink(), "upgrade_latch_missing_or_conflicting")
    raw = service._read(directory / "receipt.json")
    receipt = service._object(raw)
    _require(parent[POINTER] == pointer_for(raw), "parent_upgrade_binding")
    old_sources = {name: service._read(directory / "source34" / name, MAX_SOURCE) for name in CHANGED}
    expected = prepare_upgrade(state_dir, {k: v for k, v in parent.items() if k != POINTER}, artifacts,
                               old_sources=old_sources, prepared_ms=receipt["prepared_ms"])
    _require(receipt == expected["receipt"] and receipt["prepared_ms"] <= now_ms, "receipt_or_source_graph_changed")
    completed_raw = service._read(directory / "commit-receipt.json", 8192)
    completed = service._object(completed_raw)
    _require(completed == completion_for(raw, visible_ms=completed.get("visible_ms"), completed_ms=completed.get("completed_ms"))
             and completed["completed_ms"] <= now_ms, "completion_required")
    _, historical = historical_storage(state_dir, parent, artifacts, now_ms=now_ms)
    return {"receipt_file_sha256": _sha(raw), "completion_file_sha256": _sha(completed_raw),
        "source_graph_sha256": _hash(receipt["new_sources"]), "original_runtime_sources": service.original_runtime_sources(),
        "storage_upgrade": historical}
