"""Additive configured-receipt provenance; no install, dispatch or history reset.

Owned by AGENT-CONFIGURED-RECEIPTS. Historical programs are parsed as data;
normal consumers validate a previously published chain and never initialize it.
"""
from __future__ import annotations

import ast
import copy
import os
from pathlib import Path

from kaiba.learning import tax_quote_upgrade as tax

service = tax.service
VERSION = "configured-receipts-upgrade/1"
COMMIT_VERSION = "configured-receipts-commit/1"
POLICY = "closed-source30-configured-receipts-hooks/1"
POINTER = "configured_receipts_upgrade"
DIRECTORY = "configured-receipts-upgrade"
CHANGED = ("kaiba/learning/allocation_runtime.py", "kaiba/learning/transaction_receipts.py")
ADDITIONS = ("kaiba/learning/configured_receipts_upgrade.py",)
SOURCE30 = {**tax.SOURCE28,
    CHANGED[0]: "8a975dc9789dfb824a809e8216a71b0d4f651b4c54d7e6bc03ecb2c9db125afa",
    "kaiba/learning/autolearn.py": "dabd04959624f35670f9a7a210773f305001fa366e996750d4ab510731d3b462",
    "kaiba/learning/tax_quote.py": tax.TAX_SOURCE,
    "kaiba/learning/tax_quote_upgrade.py": "328cbb98d1b947ab229ddb4a9d50cab819a60398eef4c03d1e7adade690de2aa"}
# Reviewed configured-receipt leaf; a different source graph must refuse.
RECEIPTS_SOURCE = "b24f653acb680723c39ded9e97372b98bef47c8199f1fdee223006708e80fb57"
MAX_JSON, MAX_SOURCE = tax.MAX_JSON, tax.MAX_SOURCE
_encoded, _hash, _sha = tax._encoded, tax._hash, tax._sha
_dump, _definition, _position, _remove = tax._dump, tax._definition, tax._position, tax._remove
FALSE_FLAGS = ("original_artifacts_rewritten", "evidence_reset", "receipt_history_reset", "funding_allowed")


def _require(ok, reason):
    if not ok:
        raise ValueError("configured_receipts_upgrade:" + reason)


def _workspace_root():
    return Path(__file__).resolve().parents[2]


def _inverse_runtime(tree):
    version = _definition(tree, "source_versions")
    leaves = [n for n in ast.walk(version) if isinstance(n, ast.Tuple)
              and any(isinstance(e, ast.Constant) and e.value == "learning/allocation_runtime.py" for e in n.elts)]
    _require(len(leaves) == 1, "runtime_source_tuple")
    target = "learning/configured_receipts_upgrade.py"
    found = [i for i, n in enumerate(leaves[0].elts) if isinstance(n, ast.Constant) and n.value == target]
    _require(len(found) == 1, "runtime_source_leaf_required")
    del leaves[0].elts[found[0]]
    init = _definition(tree, "Context.__init__")
    _require(_position(init.body, "self.configured_receipts_upgrade = None") ==
             _position(init.body, "self.tax_quote_upgrade = None") + 1, "context_init_order")
    _remove(init.body, "self.configured_receipts_upgrade = None")
    loader = _definition(tree, "_load_context")
    stale = '_require(supplied.get(configured_upgrade.POINTER) == disk.get(configured_upgrade.POINTER), "supplied_configured_receipts_upgrade_stale")'
    capture = 'configured = configured_upgrade.validate_upgrade(root, disk, {"metadata.json": metadata_raw, "authority.json": authority_raw, "commit-receipt.json": completion_raw, "receipt.json": receipt_raw, "new-books.json": seal_raw}, now_ms=now_ms)'
    binding = 'if configured is not None:\n    context.configured_receipts_upgrade = copy.deepcopy(configured)\n    context.snapshot_token = _hash({"base_authority": context.snapshot_token, "configured_receipts_upgrade": configured})'
    old_stale = _position(loader.body, '_require(supplied.get(tax_upgrade.POINTER) == disk.get(tax_upgrade.POINTER), "supplied_tax_upgrade_stale")')
    _require(_position(loader.body, stale) == old_stale + 1 and _position(loader.body, capture) == old_stale + 2,
             "configured_validation_order")
    assignments = [n for n in loader.body if isinstance(n, ast.Assign)
                   and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "tax"]
    _require(len(assignments) == 1 and isinstance(assignments[0].value, ast.IfExp)
             and loader.body.index(assignments[0]) == old_stale + 3, "tax_bridge_order_or_shape")
    value = assignments[0].value
    wanted = ast.parse('configured["tax_upgrade"] if configured is not None else None', mode="eval").body
    _require(_dump(value.test) == _dump(wanted.test) and _dump(value.body) == _dump(wanted.body), "tax_bridge_shape")
    assignments[0].value = value.orelse
    tax_binding = 'if tax is not None:\n    context.tax_quote_upgrade = copy.deepcopy(tax)\n    context.snapshot_token = _hash({"base_authority": context.snapshot_token, "tax_quote_upgrade": tax})'
    _require(_position(loader.body, binding) == _position(loader.body, tax_binding) + 1, "configured_context_binding_order")
    following = loader.body[_position(loader.body, binding) + 1]
    _require(isinstance(following, ast.If) and _dump(following.test) == _dump(ast.Name(id="_full_guard", ctx=ast.Load())),
             "configured_binding_before_guard")
    for statement in ("from kaiba.learning import configured_receipts_upgrade as configured_upgrade", stale, capture, binding):
        _remove(loader.body, statement)
    present = [n for n in loader.body if isinstance(n, ast.Assign)
               and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "present"]
    _require(len(present) == 1 and isinstance(present[0].value, ast.BoolOp), "runtime_presence_shape")
    values = present[0].value.values
    for name in ("supplied", "disk"):
        wanted = _dump(ast.parse("configured_upgrade.POINTER in " + name, mode="eval").body)
        found = [i for i, node in enumerate(values) if _dump(node) == wanted]
        _require(len(found) == 1, "runtime_presence_required")
        del values[found[0]]


def _old_sources(old_sources):
    _require(isinstance(old_sources, dict) and set(old_sources) == set(CHANGED), "old_source_inventory")
    for name, raw in old_sources.items():
        _require(isinstance(raw, bytes) and len(raw) <= MAX_SOURCE and _sha(raw) == SOURCE30[name], "old_source_raw_hash")
    return {name: _sha(raw) for name, raw in old_sources.items()}


def current_sources(old_sources):
    """Verify the one runtime delta and exact reviewed configured receipt leaf."""
    _old_sources(old_sources)
    _require(isinstance(RECEIPTS_SOURCE, str) and len(RECEIPTS_SOURCE) == 64, "reviewed_receipt_source_pending")
    result, root = {}, _workspace_root()
    for name in (*SOURCE30, *ADDITIONS):
        raw = service._read(root / name, MAX_SOURCE)
        result[name] = _sha(raw.replace(b"\r\n", b"\n"))
        if name == CHANGED[0]:
            tree = ast.parse(raw.decode("utf-8-sig"))
            _inverse_runtime(tree)
            _require(_dump(tree) == _dump(ast.parse(old_sources[name].decode("utf-8-sig"))), "unclaimed_runtime_AST_change")
        elif name == CHANGED[1]:
            _require(result[name] == RECEIPTS_SOURCE, "configured_receipt_source_changed")
        elif name in SOURCE30:
            _require(result[name] == SOURCE30[name], "frozen_source_changed:" + name)
    for name, wanted in service.MODEL9.items():
        _require(_sha(service._read(root / "kaiba" / name, MAX_SOURCE)) == wanted, "raw_model9_changed")
    return result


def _historical_tax(parent, artifacts, service_raw, tax_raw, completion_raw, *, now_ms):
    _require(isinstance(tax_raw, bytes) and len(tax_raw) <= MAX_JSON
             and isinstance(completion_raw, bytes) and len(completion_raw) <= 8192, "historical_tax_cap")
    receipt = service._object(tax_raw)
    prepared = receipt.get("prepared_ms")
    _require(type(prepared) is int and type(now_ms) is int and 0 < prepared <= now_ms, "historical_tax_clock")
    original, historical_service = tax._historical_service(parent, artifacts, service_raw, now_ms=prepared)
    _require(original["activation_ms"] <= prepared, "historical_tax_clock")
    expected = {"version": tax.VERSION, "prepared_ms": prepared, "original": original,
        "service_upgrade": {"pointer": copy.deepcopy(parent[service.POINTER]),
                            "receipt_file_sha256": historical_service["receipt_file_sha256"]},
        "old_sources": dict(tax.SOURCE28), "new_sources": dict(SOURCE30),
        "old_changed_files": {name: tax.SOURCE28[name] for name in tax.CHANGED},
        "raw_model9": dict(service.MODEL9), "ast_policy": tax.POLICY,
        "original_artifacts_rewritten": False, "evidence_reset": False, "funding_allowed": False}
    expected["receipt_sha256"] = _hash(expected)
    _require(receipt == expected and all(receipt.get(n) is False for n in tax_flags()), "historical_tax_receipt")
    _require(parent.get(tax.POINTER) == tax.pointer_for(tax_raw), "historical_tax_pointer")
    completion = service._object(completion_raw)
    _require(completion == tax.completion_for(tax_raw, visible_ms=completion.get("visible_ms"), completed_ms=completion.get("completed_ms"))
             and type(completion["completed_ms"]) is int and completion["completed_ms"] <= now_ms,
             "historical_tax_completion")
    return original, {"receipt_file_sha256": _sha(tax_raw), "completion_file_sha256": _sha(completion_raw),
        "source_graph_sha256": _hash(SOURCE30), "original_runtime_sources": service.original_runtime_sources(),
        "service_upgrade": historical_service}


def tax_flags():
    return ("original_artifacts_rewritten", "evidence_reset", "funding_allowed")


def pointer_for(receipt_raw):
    receipt = service._object(receipt_raw)
    return {"version": VERSION, "receipt_file_sha256": _sha(receipt_raw), "receipt_sha256": receipt["receipt_sha256"],
        "source_graph_sha256": _hash(receipt["new_sources"]), "activation_ms": receipt["original"]["activation_ms"],
        "funding_allowed": False}


def prepare_upgrade(parent, artifacts, *, service_receipt_raw, tax_receipt_raw, tax_completion_raw, old_sources, prepared_ms):
    """Explicit owner recipe; publishes nothing and never initializes a worker."""
    _require(type(prepared_ms) is int and prepared_ms > 0, "integer_clock_required")
    _require(POINTER not in parent, "already_latched")
    original, historical = _historical_tax(parent, artifacts, service_receipt_raw, tax_receipt_raw, tax_completion_raw, now_ms=prepared_ms)
    receipt = {"version": VERSION, "prepared_ms": prepared_ms, "original": original,
        "tax_upgrade": {"pointer": copy.deepcopy(parent[tax.POINTER]),
                        "receipt_file_sha256": historical["receipt_file_sha256"],
                        "completion_file_sha256": historical["completion_file_sha256"]},
        "service_upgrade": {"pointer": copy.deepcopy(parent[service.POINTER]),
                            "receipt_file_sha256": historical["service_upgrade"]["receipt_file_sha256"]},
        "old_sources": dict(SOURCE30), "new_sources": current_sources(old_sources),
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
    """Either activation latch requires all immutable history and completion."""
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
    # Authenticate prior backups as DATA; never import or execute them.
    tax._old_sources({name: service._read(own / tax.DIRECTORY / "source28" / name, MAX_SOURCE) for name in tax.CHANGED})
    old_sources = {name: service._read(directory / "source30" / name, MAX_SOURCE) for name in CHANGED}
    expected = prepare_upgrade({k: v for k, v in parent.items() if k != POINTER}, artifacts,
        service_receipt_raw=service_raw, tax_receipt_raw=tax_raw, tax_completion_raw=tax_completion,
        old_sources=old_sources, prepared_ms=receipt["prepared_ms"])
    _require(receipt == expected["receipt"] and receipt["prepared_ms"] <= now_ms, "receipt_or_source_graph_changed")
    completion_raw = service._read(directory / "commit-receipt.json", 8192)
    completion = service._object(completion_raw)
    _require(completion == completion_for(raw, visible_ms=completion.get("visible_ms"), completed_ms=completion.get("completed_ms"))
             and completion["completed_ms"] <= now_ms, "completion_required")
    _, historical = _historical_tax(parent, artifacts, service_raw, tax_raw, tax_completion, now_ms=now_ms)
    return {"receipt_file_sha256": _sha(raw), "completion_file_sha256": _sha(completion_raw),
        "source_graph_sha256": _hash(receipt["new_sources"]), "original_runtime_sources": service.original_runtime_sources(),
        "tax_upgrade": historical, "service_upgrade": historical["service_upgrade"]}


__all__ = ["prepare_upgrade", "pointer_for", "completion_for", "validate_upgrade", "current_sources", "VERSION"]
