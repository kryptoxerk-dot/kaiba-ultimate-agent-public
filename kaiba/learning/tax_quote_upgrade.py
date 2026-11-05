"""Read-only additive provenance for the prospective tax representation repair.

Owned by AGENT-TAX-QUOTE-INTEGRATION. No installation, backup execution, SQL,
network, evidence initialization or new financial authority occurs here.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import os
from pathlib import Path

from kaiba.learning import research_service_upgrade as service

VERSION = "tax-quote-upgrade/1"
COMMIT_VERSION = "tax-quote-commit/1"
POLICY = "closed-source28-tax-hooks/1"
POINTER = "tax_quote_upgrade"
DIRECTORY = "tax-quote-upgrade"
CHANGED = ("kaiba/learning/allocation_runtime.py", "kaiba/learning/autolearn.py")
ADDITIONS = ("kaiba/learning/tax_quote.py", "kaiba/learning/tax_quote_upgrade.py")
SOURCE28 = {**service.SOURCE26,
    CHANGED[0]: "b3c07f64c2fc3cf39d7fe74ea706d717fc57ef1ced68ad748634dfbb2f10c8fa",
    CHANGED[1]: "fb673cff34d51b2c1e337065003900674b570b1d105444565c7269bf8398ac63",
    "kaiba/learning/cooperative_reader.py": "5a6c6d767d588845bbc85aaa0bea164f52b1fb5d1fb9c6271dd3223d78f9fa42",
    "kaiba/learning/research_service_upgrade.py": "80b4a2f5f0c5f379f7149cf3c8dd27274ef595ba30dd3492e54497f77a728494"}
TAX_SOURCE = "70f93099f7bf77e1f3ae83e3f1d33e817c63b298931529cccab2c95ce804da94"
MAX_JSON, MAX_SOURCE = service.MAX_JSON, service.MAX_SOURCE
_encoded, _hash = service._encoded, service._hash


def _require(ok, reason):
    if not ok:
        raise ValueError("tax_quote_upgrade:" + reason)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _workspace_root():
    return Path(__file__).resolve().parents[2]


def _node(text):
    return ast.parse(text).body[0]


def _dump(node):
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def _position(body, text):
    wanted = _dump(_node(text))
    found = [i for i, node in enumerate(body) if _dump(node) == wanted]
    _require(len(found) == 1, "exact_hook_missing_or_duplicated")
    return found[0]


def _remove(body, text):
    del body[_position(body, text)]


def _definition(tree, qualified):
    body = tree.body
    for name in qualified.split("."):
        found = [n for n in body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == name]
        _require(len(found) == 1, "definition_missing_or_duplicated")
        result = found[0]
        body = result.body
    return result


def _inverse_worker(tree):
    once = _definition(tree, "AutoLearner._once")
    latch = [n for n in once.body if isinstance(n, ast.Assign)
             and _dump(n.targets[0]) == _dump(ast.Name(id="allocation_latched", ctx=ast.Store()))]
    _require(len(latch) == 1 and isinstance(latch[0].value, ast.BoolOp), "worker_latch_shape")
    wanted = _dump(ast.parse('"tax_quote_upgrade" in state', mode="eval").body)
    values = latch[0].value.values
    found = [i for i, n in enumerate(values) if _dump(n) == wanted]
    _require(len(found) == 1, "worker_tax_latch_required")
    del values[found[0]]
    tick = _definition(once, "new_paper_tick")
    phases = [n for n in tick.body if isinstance(n, ast.With)
              and _dump(n.items[0].context_expr) == _dump(ast.parse('phase("captured_quotes")', mode="eval").body)]
    _require(len(phases) == 1, "quote_phase_shape")
    phase = phases[0]
    _require(len(phase.body) == 3, "quote_phase_scope")
    selection = 'if allocated is not None and getattr(allocated, "tax_quote_upgrade", None) is not None:\n' \
                '    from kaiba.learning.tax_quote import quote_evidence as quote_source'
    _require(_position(phase.body, "quote_source = quote_evidence") == 0
             and _position(phase.body, selection) == 1, "quote_selection_order")
    _remove(phase.body, "quote_source = quote_evidence")
    _remove(phase.body, selection)
    loop = phase.body[0]
    _require(isinstance(loop, ast.For) and len(loop.body) == 2, "quote_loop_shape")
    call = loop.body[0]
    _require(isinstance(call, ast.Assign) and isinstance(call.value, ast.Call)
             and isinstance(call.value.func, ast.Name) and call.value.func.id == "quote_source", "quote_call_shape")
    call.value.func.id = "quote_evidence"


def _inverse_runtime(tree):
    version = _definition(tree, "source_versions")
    leaves = [n for n in ast.walk(version) if isinstance(n, ast.Tuple)
              and any(isinstance(e, ast.Constant) and e.value == "learning/allocation_runtime.py" for e in n.elts)]
    _require(len(leaves) == 1, "runtime_source_tuple")
    for name in ("learning/tax_quote.py", "learning/tax_quote_upgrade.py"):
        found = [i for i, n in enumerate(leaves[0].elts) if isinstance(n, ast.Constant) and n.value == name]
        _require(len(found) == 1, "runtime_source_leaf_required")
        del leaves[0].elts[found[0]]
    init = _definition(tree, "Context.__init__")
    _require(_position(init.body, "self.tax_quote_upgrade = None") == _position(init.body,
        'self.service_upgrade = copy.deepcopy(parent.get("paper_service_upgrade"))') + 1, "tax_context_init_order")
    _remove(init.body, "self.tax_quote_upgrade = None")
    loader = _definition(tree, "_load_context")
    stale = '_require(supplied.get(tax_upgrade.POINTER) == disk.get(tax_upgrade.POINTER), "supplied_tax_upgrade_stale")'
    capture = 'tax = tax_upgrade.validate_upgrade(root, disk, {"metadata.json": metadata_raw, ' \
        '"authority.json": authority_raw, "commit-receipt.json": completion_raw, ' \
        '"receipt.json": receipt_raw, "new-books.json": seal_raw}, now_ms=now_ms)'
    binding = 'if tax is not None:\n' \
        '    context.tax_quote_upgrade = copy.deepcopy(tax)\n' \
        '    context.snapshot_token = _hash({"base_authority": context.snapshot_token, "tax_quote_upgrade": tax})'
    old_stale = _position(loader.body, '_require(supplied.get(upgrade.POINTER) == disk.get(upgrade.POINTER), "supplied_service_upgrade_stale")')
    service_nodes = [i for i, n in enumerate(loader.body) if isinstance(n, ast.Assign)
                     and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "service"]
    context_nodes = [i for i, n in enumerate(loader.body) if isinstance(n, ast.Assign)
                     and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "context"]
    _require(_position(loader.body, stale) == old_stale + 1
             and _position(loader.body, capture) == old_stale + 2 and service_nodes == [old_stale + 3], "tax_validation_order")
    _require(len(context_nodes) == 1 and _position(loader.body, binding) == context_nodes[0] + 1
             and isinstance(loader.body[context_nodes[0] + 2], ast.If)
             and _dump(loader.body[context_nodes[0] + 2].test) == _dump(ast.Name(id="_full_guard", ctx=ast.Load())),
             "tax_context_binding_order")
    _remove(loader.body, "from kaiba.learning import tax_quote_upgrade as tax_upgrade")
    _remove(loader.body, stale)
    _remove(loader.body, capture)
    _remove(loader.body, binding)
    present = [n for n in loader.body if isinstance(n, ast.Assign)
               and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "present"]
    _require(len(present) == 1 and isinstance(present[0].value, ast.BoolOp), "runtime_presence_shape")
    values = present[0].value.values
    for name in ("supplied", "disk"):
        wanted = _dump(ast.parse("tax_upgrade.POINTER in " + name, mode="eval").body)
        found = [i for i, n in enumerate(values) if _dump(n) == wanted]
        _require(len(found) == 1, "runtime_presence_required")
        del values[found[0]]
    assignments = [n for n in loader.body if isinstance(n, ast.Assign)
                   and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "service"]
    _require(len(assignments) == 1 and isinstance(assignments[0].value, ast.IfExp), "service_bridge_shape")
    value = assignments[0].value
    wanted = ast.parse('tax["service_upgrade"] if tax is not None else None', mode="eval").body
    _require(_dump(value.test) == _dump(wanted.test) and _dump(value.body) == _dump(wanted.body), "service_bridge_shape")
    assignments[0].value = value.orelse


def _old_sources(old_sources):
    _require(isinstance(old_sources, dict) and set(old_sources) == set(CHANGED), "old_source_inventory")
    for name, raw in old_sources.items():
        _require(isinstance(raw, bytes) and len(raw) <= MAX_SOURCE and _sha(raw) == SOURCE28[name], "old_source_raw_hash")
        _require(service._ast_signature(raw, name) == service.AST_BASELINES[name], "old_service_AST_history")
    return {name: _sha(raw) for name, raw in old_sources.items()}


def current_sources(old_sources):
    """Exact new graph plus whole-program inverse AST; never execute backups."""
    _old_sources(old_sources)
    root, result = _workspace_root(), {}
    for name in (*SOURCE28, *ADDITIONS):
        raw = service._read(root / name, MAX_SOURCE)
        result[name] = _sha(raw.replace(b"\r\n", b"\n"))
        if name in CHANGED:
            tree = ast.parse(raw.decode("utf-8-sig"))
            (_inverse_runtime if name == CHANGED[0] else _inverse_worker)(tree)
            _require(_dump(tree) == _dump(ast.parse(old_sources[name].decode("utf-8-sig"))), "unclaimed_AST_change:" + name)
        elif name in SOURCE28:
            _require(result[name] == SOURCE28[name], "frozen_source_changed:" + name)
    _require(result[ADDITIONS[0]] == TAX_SOURCE, "tax_adapter_changed")
    for name, wanted in service.MODEL9.items():
        _require(_sha(service._read(root / "kaiba" / name, MAX_SOURCE)) == wanted, "raw_model9_changed")
    return result


def _historical_service(parent, artifacts, raw, *, now_ms):
    _require(isinstance(raw, bytes) and len(raw) <= MAX_JSON, "service_receipt_cap")
    receipt = service._object(raw)
    original = service._original(parent, artifacts)
    expected = {"version": service.VERSION, "prepared_ms": receipt.get("prepared_ms"), "original": original,
        "old_sources": service.SOURCE26, "new_sources": SOURCE28, "raw_model9": service.MODEL9,
        "ast_policy": service.AST_POLICY, "ast_baselines": service.AST_BASELINES,
        "original_artifacts_rewritten": False, "evidence_reset": False, "funding_allowed": False}
    expected["receipt_sha256"] = _hash(expected)
    _require(receipt == expected, "historical_service_receipt")
    _require(all(receipt[n] is False for n in ("original_artifacts_rewritten", "evidence_reset", "funding_allowed")),
             "historical_service_authority_claim")
    _require(parent.get(service.POINTER) == service.pointer_for(raw), "historical_service_pointer")
    _require(type(receipt["prepared_ms"]) is int and type(now_ms) is int
             and original["activation_ms"] <= receipt["prepared_ms"] <= now_ms, "historical_service_clock")
    return original, {"receipt_file_sha256": _sha(raw), "original_runtime_sources": service.original_runtime_sources(),
                      "source_graph_sha256": _hash(SOURCE28)}


def pointer_for(receipt_raw):
    receipt = service._object(receipt_raw)
    return {"version": VERSION, "receipt_file_sha256": _sha(receipt_raw), "receipt_sha256": receipt["receipt_sha256"],
        "source_graph_sha256": _hash(receipt["new_sources"]), "activation_ms": receipt["original"]["activation_ms"],
        "funding_allowed": False}


def prepare_upgrade(parent, artifacts, *, service_receipt_raw, old_sources, prepared_ms):
    """Pure preparation recipe; caller alone owns locks, CAS and publication."""
    _require(type(prepared_ms) is int and prepared_ms > 0, "integer_clock_required")
    _require(POINTER not in parent, "already_latched")
    original, historical = _historical_service(parent, artifacts, service_receipt_raw, now_ms=prepared_ms)
    receipt = {"version": VERSION, "prepared_ms": prepared_ms, "original": original,
        "service_upgrade": {"pointer": copy.deepcopy(parent[service.POINTER]),
                            "receipt_file_sha256": historical["receipt_file_sha256"]},
        "old_sources": dict(SOURCE28), "new_sources": current_sources(old_sources),
        "old_changed_files": _old_sources(old_sources), "raw_model9": dict(service.MODEL9), "ast_policy": POLICY,
        "original_artifacts_rewritten": False, "evidence_reset": False, "funding_allowed": False}
    receipt["receipt_sha256"] = _hash(receipt)
    raw = _encoded(receipt) + b"\n"
    _require(len(raw) <= MAX_JSON, "receipt_cap")
    return {"receipt": receipt, "receipt_raw": raw, "pointer": pointer_for(raw)}


def completion_for(receipt_raw, *, visible_ms, completed_ms):
    receipt = service._object(receipt_raw)
    _require(type(visible_ms) is int and type(completed_ms) is int
             and type(receipt["prepared_ms"]) is int
             and receipt["prepared_ms"] <= visible_ms <= completed_ms, "completion_clock")
    return {"version": COMMIT_VERSION, "receipt_file_sha256": _sha(receipt_raw),
        "receipt_sha256": receipt["receipt_sha256"], "source_graph_sha256": _hash(receipt["new_sources"]),
        "activation_ms": receipt["original"]["activation_ms"], "prepared_ms": receipt["prepared_ms"],
        "visible_ms": visible_ms, "completed_ms": completed_ms}


def validate_upgrade(state_dir, parent, artifacts, *, now_ms):
    """Both latches absent is legacy; either present requires the entire chain."""
    directory = Path(state_dir) / "research-allocation" / DIRECTORY
    if POINTER not in parent and not os.path.lexists(directory):
        return None
    _require(type(now_ms) is int and now_ms > 0, "integer_clock_required")
    _require(POINTER in parent and directory.is_dir() and not directory.is_symlink(), "upgrade_latch_missing_or_conflicting")
    raw = service._read(directory / "receipt.json")
    receipt = service._object(raw)
    _require(parent[POINTER] == pointer_for(raw), "parent_upgrade_binding")
    _require(all(receipt.get(n) is False for n in ("original_artifacts_rewritten", "evidence_reset", "funding_allowed")),
             "upgrade_authority_claim")
    old_sources = {name: service._read(directory / "source28" / name, MAX_SOURCE) for name in CHANGED}
    service_raw = service._read(Path(state_dir) / "research-allocation" / service.DIRECTORY / "receipt.json")
    # Rebuild the pure receipt without its already-latched parent field. This
    # checks all fields/seals, exact original/service bytes and current sources.
    unlatched = {k: v for k, v in parent.items() if k != POINTER}
    expected = prepare_upgrade(unlatched, artifacts, service_receipt_raw=service_raw,
                               old_sources=old_sources, prepared_ms=receipt["prepared_ms"])
    _require(receipt == expected["receipt"], "receipt_or_source_graph_changed")
    _require(receipt["prepared_ms"] <= now_ms, "upgrade_not_yet_prepared")
    completion_raw = service._read(directory / "commit-receipt.json", 8192)
    completion = service._object(completion_raw)
    _require(completion == completion_for(raw, visible_ms=completion.get("visible_ms"),
             completed_ms=completion.get("completed_ms")) and completion["completed_ms"] <= now_ms,
             "completion_required")
    _, historical = _historical_service(parent, artifacts, service_raw, now_ms=now_ms)
    return {"receipt_file_sha256": _sha(raw), "completion_file_sha256": _sha(completion_raw),
        "source_graph_sha256": _hash(receipt["new_sources"]), "original_runtime_sources": service.original_runtime_sources(),
        "service_upgrade": historical}


__all__ = ["prepare_upgrade", "pointer_for", "completion_for", "validate_upgrade", "current_sources", "VERSION"]
