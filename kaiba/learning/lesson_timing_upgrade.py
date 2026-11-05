"""Additive timing-qualified lesson provenance; no publication or credit writes.

Owned by AGENT-LESSON-TIMING-INTEGRATION. Historical programs are authenticated
as data. Existing study, source, receipt and completion artifacts stay intact.
"""
from __future__ import annotations

import ast
import copy
import os
from pathlib import Path

from kaiba.learning import configured_receipts_upgrade as configured

tax, service = configured.tax, configured.service
VERSION = "lesson-timing-upgrade/1"
COMMIT_VERSION = "lesson-timing-commit/1"
POLICY = "closed-source31-lesson-timing-hooks/1"
POINTER = "lesson_timing_upgrade"
DIRECTORY = "lesson-timing-upgrade"
CHANGED = ("kaiba/learning/allocation_runtime.py", "kaiba/learning/autolearn.py")
ADDITIONS = ("kaiba/learning/lesson_timing_upgrade.py",)
SOURCE31 = {**configured.SOURCE30,
    CHANGED[0]: "1a55423e8bd07f59fbcdb84f46daa7aa50fcaa27482e5901782ef489f3fccf1e",
    "kaiba/learning/transaction_receipts.py": configured.RECEIPTS_SOURCE,
    "kaiba/learning/configured_receipts_upgrade.py": "7b518213a6ca640844519d4a341bd2bd70ab8113dccb4e5dd3b727ea485dfcd9"}
# Reviewed whole worker: timing qualification and ephemeral pending-outbox checks.
WORKER_SOURCE = "f71b0a04b19bfb08798d7adde4f068556ecb30c7f0d1d62fc24627821583abce"
MAX_JSON, MAX_SOURCE = configured.MAX_JSON, configured.MAX_SOURCE
_encoded, _hash, _sha = configured._encoded, configured._hash, configured._sha
_dump, _definition, _position, _remove = configured._dump, configured._definition, configured._position, configured._remove
FALSE_FLAGS = ("original_artifacts_rewritten", "evidence_reset", "receipt_history_reset", "funding_allowed")


def _require(ok, reason):
    if not ok:
        raise ValueError("lesson_timing_upgrade:" + reason)


def _workspace_root():
    return Path(__file__).resolve().parents[2]


def _inverse_runtime(tree):
    version = _definition(tree, "source_versions")
    leaves = [n for n in ast.walk(version) if isinstance(n, ast.Tuple)
              and any(isinstance(e, ast.Constant) and e.value == "learning/allocation_runtime.py" for e in n.elts)]
    _require(len(leaves) == 1, "runtime_source_tuple")
    found = [i for i, n in enumerate(leaves[0].elts)
             if isinstance(n, ast.Constant) and n.value == "learning/lesson_timing_upgrade.py"]
    _require(len(found) == 1, "runtime_source_leaf_required")
    del leaves[0].elts[found[0]]
    init = _definition(tree, "Context.__init__")
    _require(_position(init.body, "self.lesson_timing_upgrade = None") ==
             _position(init.body, "self.configured_receipts_upgrade = None") + 1, "context_init_order")
    _remove(init.body, "self.lesson_timing_upgrade = None")
    loader = _definition(tree, "_load_context")
    stale = '_require(supplied.get(lesson_upgrade.POINTER) == disk.get(lesson_upgrade.POINTER), "supplied_lesson_timing_upgrade_stale")'
    capture = 'lesson = lesson_upgrade.validate_upgrade(root, disk, {"metadata.json": metadata_raw, "authority.json": authority_raw, "commit-receipt.json": completion_raw, "receipt.json": receipt_raw, "new-books.json": seal_raw}, now_ms=now_ms)'
    binding = 'if lesson is not None:\n    context.lesson_timing_upgrade = copy.deepcopy(lesson)\n    context.snapshot_token = _hash({"base_authority": context.snapshot_token, "lesson_timing_upgrade": lesson})'
    old_stale = _position(loader.body, '_require(supplied.get(configured_upgrade.POINTER) == disk.get(configured_upgrade.POINTER), "supplied_configured_receipts_upgrade_stale")')
    _require(_position(loader.body, stale) == old_stale + 1
             and _position(loader.body, capture) == old_stale + 2, "lesson_validation_order")
    assignments = [n for n in loader.body if isinstance(n, ast.Assign)
                   and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "configured"]
    _require(len(assignments) == 1 and isinstance(assignments[0].value, ast.IfExp)
             and loader.body.index(assignments[0]) == old_stale + 3, "configured_bridge_order_or_shape")
    value = assignments[0].value
    wanted = ast.parse('lesson["configured_upgrade"] if lesson is not None else None', mode="eval").body
    _require(_dump(value.test) == _dump(wanted.test) and _dump(value.body) == _dump(wanted.body), "configured_bridge_shape")
    assignments[0].value = value.orelse
    old_binding = 'if configured is not None:\n    context.configured_receipts_upgrade = copy.deepcopy(configured)\n    context.snapshot_token = _hash({"base_authority": context.snapshot_token, "configured_receipts_upgrade": configured})'
    _require(_position(loader.body, binding) == _position(loader.body, old_binding) + 1, "lesson_context_binding_order")
    following = loader.body[_position(loader.body, binding) + 1]
    _require(isinstance(following, ast.If) and _dump(following.test) == _dump(ast.Name(id="_full_guard", ctx=ast.Load())),
             "lesson_binding_before_guard")
    for statement in ("from kaiba.learning import lesson_timing_upgrade as lesson_upgrade", stale, capture, binding):
        _remove(loader.body, statement)
    present = [n for n in loader.body if isinstance(n, ast.Assign)
               and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "present"]
    _require(len(present) == 1 and isinstance(present[0].value, ast.BoolOp), "runtime_presence_shape")
    values = present[0].value.values
    for name in ("supplied", "disk"):
        wanted = _dump(ast.parse("lesson_upgrade.POINTER in " + name, mode="eval").body)
        found = [i for i, node in enumerate(values) if _dump(node) == wanted]
        _require(len(found) == 1, "runtime_presence_required")
        del values[found[0]]


def _old_sources(old_sources):
    _require(isinstance(old_sources, dict) and set(old_sources) == set(CHANGED), "old_source_inventory")
    for name, raw in old_sources.items():
        _require(isinstance(raw, bytes) and len(raw) <= MAX_SOURCE and _sha(raw) == SOURCE31[name], "old_source_raw_hash")
    return {name: _sha(raw) for name, raw in old_sources.items()}


def current_sources(old_sources):
    """Require the reviewed whole worker and the exact additive runtime delta."""
    _old_sources(old_sources)
    _require(isinstance(WORKER_SOURCE, str) and len(WORKER_SOURCE) == 64, "reviewed_worker_source_pending")
    result, root = {}, _workspace_root()
    for name in (*SOURCE31, *ADDITIONS):
        raw = service._read(root / name, MAX_SOURCE)
        result[name] = _sha(raw.replace(b"\r\n", b"\n"))
        if name == CHANGED[0]:
            tree = ast.parse(raw.decode("utf-8-sig"))
            _inverse_runtime(tree)
            _require(_dump(tree) == _dump(ast.parse(old_sources[name].decode("utf-8-sig"))), "unclaimed_runtime_AST_change")
        elif name == CHANGED[1]:
            _require(result[name] == WORKER_SOURCE, "reviewed_worker_source_changed")
        elif name in SOURCE31:
            _require(result[name] == SOURCE31[name], "frozen_source_changed:" + name)
    for name, wanted in service.MODEL9.items():
        _require(_sha(service._read(root / "kaiba" / name, MAX_SOURCE)) == wanted, "raw_model9_changed")
    return result


def _historical_configured(parent, artifacts, service_raw, tax_raw, tax_completion_raw,
                           configured_raw, configured_completion_raw, *, now_ms):
    _require(isinstance(configured_raw, bytes) and len(configured_raw) <= MAX_JSON
             and isinstance(configured_completion_raw, bytes) and len(configured_completion_raw) <= 8192,
             "historical_configured_cap")
    receipt = service._object(configured_raw)
    prepared = receipt.get("prepared_ms")
    _require(type(prepared) is int and type(now_ms) is int and 0 < prepared <= now_ms, "historical_configured_clock")
    original, historical_tax = configured._historical_tax(parent, artifacts, service_raw, tax_raw,
                                                          tax_completion_raw, now_ms=prepared)
    expected = {"version": configured.VERSION, "prepared_ms": prepared, "original": original,
        "tax_upgrade": {"pointer": copy.deepcopy(parent[tax.POINTER]),
                        "receipt_file_sha256": historical_tax["receipt_file_sha256"],
                        "completion_file_sha256": historical_tax["completion_file_sha256"]},
        "service_upgrade": {"pointer": copy.deepcopy(parent[service.POINTER]),
                            "receipt_file_sha256": historical_tax["service_upgrade"]["receipt_file_sha256"]},
        "old_sources": dict(configured.SOURCE30), "new_sources": dict(SOURCE31),
        "old_changed_files": {name: configured.SOURCE30[name] for name in configured.CHANGED},
        "raw_model9": dict(service.MODEL9), "ast_policy": configured.POLICY,
        **dict.fromkeys(configured.FALSE_FLAGS, False)}
    expected["receipt_sha256"] = _hash(expected)
    _require(receipt == expected and all(receipt.get(n) is False for n in configured.FALSE_FLAGS), "historical_configured_receipt")
    _require(parent.get(configured.POINTER) == configured.pointer_for(configured_raw), "historical_configured_pointer")
    completion = service._object(configured_completion_raw)
    _require(completion == configured.completion_for(configured_raw, visible_ms=completion.get("visible_ms"), completed_ms=completion.get("completed_ms"))
             and type(completion["completed_ms"]) is int and completion["completed_ms"] <= now_ms,
             "historical_configured_completion")
    return original, {"receipt_file_sha256": _sha(configured_raw), "completion_file_sha256": _sha(configured_completion_raw),
        "source_graph_sha256": _hash(SOURCE31), "original_runtime_sources": service.original_runtime_sources(),
        "tax_upgrade": historical_tax, "service_upgrade": historical_tax["service_upgrade"]}


def pointer_for(receipt_raw):
    receipt = service._object(receipt_raw)
    return {"version": VERSION, "receipt_file_sha256": _sha(receipt_raw), "receipt_sha256": receipt["receipt_sha256"],
        "source_graph_sha256": _hash(receipt["new_sources"]), "activation_ms": receipt["original"]["activation_ms"],
        "funding_allowed": False}


def prepare_upgrade(parent, artifacts, *, service_receipt_raw, tax_receipt_raw, tax_completion_raw,
                    configured_receipt_raw, configured_completion_raw, old_sources, prepared_ms):
    """Explicit owner recipe; does not publish, initialize or qualify a lesson."""
    _require(type(prepared_ms) is int and prepared_ms > 0, "integer_clock_required")
    _require(POINTER not in parent, "already_latched")
    original, historical = _historical_configured(parent, artifacts, service_receipt_raw, tax_receipt_raw,
        tax_completion_raw, configured_receipt_raw, configured_completion_raw, now_ms=prepared_ms)
    receipt = {"version": VERSION, "prepared_ms": prepared_ms, "original": original,
        "configured_upgrade": {"pointer": copy.deepcopy(parent[configured.POINTER]),
                               "receipt_file_sha256": historical["receipt_file_sha256"],
                               "completion_file_sha256": historical["completion_file_sha256"]},
        "tax_upgrade": {"pointer": copy.deepcopy(parent[tax.POINTER]),
                        "receipt_file_sha256": historical["tax_upgrade"]["receipt_file_sha256"],
                        "completion_file_sha256": historical["tax_upgrade"]["completion_file_sha256"]},
        "service_upgrade": {"pointer": copy.deepcopy(parent[service.POINTER]),
                            "receipt_file_sha256": historical["service_upgrade"]["receipt_file_sha256"]},
        "old_sources": dict(SOURCE31), "new_sources": current_sources(old_sources),
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
    """Either latch requires the complete original chain and recorded completion."""
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
    tax._old_sources({name: service._read(own / tax.DIRECTORY / "source28" / name, MAX_SOURCE) for name in tax.CHANGED})
    configured._old_sources({name: service._read(own / configured.DIRECTORY / "source30" / name, MAX_SOURCE) for name in configured.CHANGED})
    old_sources = {name: service._read(directory / "source31" / name, MAX_SOURCE) for name in CHANGED}
    expected = prepare_upgrade({k: v for k, v in parent.items() if k != POINTER}, artifacts,
        service_receipt_raw=service_raw, tax_receipt_raw=tax_raw, tax_completion_raw=tax_completion,
        configured_receipt_raw=configured_raw, configured_completion_raw=configured_completion,
        old_sources=old_sources, prepared_ms=receipt["prepared_ms"])
    _require(receipt == expected["receipt"] and receipt["prepared_ms"] <= now_ms, "receipt_or_source_graph_changed")
    completion_raw = service._read(directory / "commit-receipt.json", 8192)
    completion = service._object(completion_raw)
    _require(completion == completion_for(raw, visible_ms=completion.get("visible_ms"), completed_ms=completion.get("completed_ms"))
             and completion["completed_ms"] <= now_ms, "completion_required")
    _, historical = _historical_configured(parent, artifacts, service_raw, tax_raw, tax_completion,
                                           configured_raw, configured_completion, now_ms=now_ms)
    return {"receipt_file_sha256": _sha(raw), "completion_file_sha256": _sha(completion_raw),
        "source_graph_sha256": _hash(receipt["new_sources"]), "original_runtime_sources": service.original_runtime_sources(),
        "configured_upgrade": historical, "tax_upgrade": historical["tax_upgrade"], "service_upgrade": historical["service_upgrade"]}


__all__ = ["prepare_upgrade", "pointer_for", "completion_for", "validate_upgrade", "current_sources", "VERSION"]
