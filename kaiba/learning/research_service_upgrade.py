"""Sealed additive operational compatibility; never installs or resets state.

Owned by Codex AGENT-PAPER-SERVICE-INTEGRATION. Original allocation artifacts
remain byte-exact. Preparation is explicit/read-only; workers only validate.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
from pathlib import Path

VERSION = "research-service-upgrade/1"
POINTER = "paper_service_upgrade"
DIRECTORY = "paper-service-upgrade"
MAX_JSON = 256 * 1024
MAX_SOURCE = 1024 * 1024
SOURCE26 = {'kaiba/intelligence/wallet_campaign.py': 'f153fa72125689693572096c759f1d99bd80d580e745e3e01980d32832870925',
 'kaiba/intelligence/wallet_candidates.py': 'bcca5cab2ff73ad95caf892666ff7dca4996f9a2695b338a6595fb47c3c7f797',
 'kaiba/learning/admission_paper.py': 'aeec3c30c4ce4bf12e09fe8e70b28a63b4650a6045626c381646195fd7373402',
 'kaiba/learning/agent_review.py': '9efb88a99b5d8506a0353b64a2fe182f955f05a67ba4c233e517df8eb0c7b266',
 'kaiba/learning/allocation_runtime.py': 'cf9324bb7ed2bddd744ef169baebe7c6f91671451b9c46ecb39ef2ff072e95d9',
 'kaiba/learning/autolearn.py': '8abb23c0a249722e1de07e39f5f31cbe47fdc9798d9ff77116bff52ce276073f',
 'kaiba/learning/autolearn_behavior.py': '072cfee33ddf19502f2b9e3e03f5ed42880dacb1ebfdaebbd460d3c32bb1a852',
 'kaiba/learning/autolearn_clock.py': '9f24f6bcd67a7627496b3ae8b71fb32af193d054732249425d3f13de1b719a8f',
 'kaiba/learning/autolearn_discovery.py': '229636e6b700a01d1d21f1b918fe80e5748519b77c1d440faf531b7bb1067d60',
 'kaiba/learning/autolearn_io.py': '6a0b6e636314b16a7bd00d1afa3ae0ff2f1031c979dac1ab12f0c19f597d9f5a',
 'kaiba/learning/autolearn_review.py': 'eb1b8cd4f4a5c0526fa0134e81dd632a72b59b008e477871ed55cdc105b5c7b1',
 'kaiba/learning/autolearn_wallets.py': 'f933eff5c388a2920594b46f82a07929c15a30b8fed6e61eee0a0aa9d1a36928',
 'kaiba/learning/candidate_paper.py': '2cf001f39753530e7bcd1ad42d1f03b901b1521548ec48aff15825a22934ad21',
 'kaiba/learning/economic_capture.py': '99c5cc1fa87710f09e68ba61a6d644fd34ec37e7f36fd0f4d425217cefa3e122',
 'kaiba/learning/economic_outcomes.py': '5b0291b026f274c611886d2008783ef0251fafbeac667bbc2b5671609aac69b0',
 'kaiba/learning/economic_work.py': 'fe12d771dd7fccade5e24b58cb5103ddf1f0fff2ccba2d94583d8f09f14575c9',
 'kaiba/learning/fixed_wallet_study.py': 'e1dc1f834fda9e5dd252fc37f693f204581948f3f216a780da573db46722a577',
 'kaiba/learning/process_reader.py': '27d87b5d5aeff76dfb113a8770b0ae1ce28b5000c21b7524fef927ca81f62e1e',
 'kaiba/learning/prospective_quotes.py': 'f48aad6173a98a6b04be415ea4035a6fa2d93187b5d3fc8f28fa9adc7b6ae087',
 'kaiba/learning/research_allocation.py': 'a29d214a82c2ddef00902fd51f1e072b07656171578103b56f6193e3a863cfc7',
 'kaiba/learning/strategy_campaign.py': 'ffc20887e08c6a781040129bd68dbcf49b4c58a216e89b2c5fe099a88a845547',
 'kaiba/learning/transaction_receipts.py': '7f2f67c9266fde66b7bdcf214c4c79d70d37b0fd43f245a97eb2b396dfee51b4',
 'kaiba/learning/wallet_behavior_study.py': '23376741cf93c28acec56b77d5b8eb86cc49c55bb88cb23150ba6f517827c966',
 'kaiba/learning/wallet_followthrough.py': 'bb66aa9d72674da5cc99f781bf293c8cac3ba838b132d02f4f4e0fbd8ed3a5d1',
 'kaiba/learning/wallet_observation_io.py': '7ca049e39bc7527e0f83fa7def3e06a7c39340cb2e1d4943e17a08dfad025f71',
 'kaiba/learning/wallet_observation_tape.py': 'c4b74bd7da6d6d0352d783d8d7cabeb905a11dc800325437d7479218a291594f'}
MODEL9 = {'execution/curve_price.py': '8131de3d63971e17fa8b10a25d99e8fda583e5021239cd339ac0828e5297830a',
 'execution/paper.py': '1cf9473900dbf010486ed3b73a13d06bbb6fba0536287309f5b5f25ae319bbf0',
 'execution/protection.py': 'c422baab7658b1c035ec147d20b8eef1b8088ea1d27bc38ba847bad5b627b531',
 'learning/autolearn_io.py': '6a0b6e636314b16a7bd00d1afa3ae0ff2f1031c979dac1ab12f0c19f597d9f5a',
 'learning/candidate_paper.py': '2cf001f39753530e7bcd1ad42d1f03b901b1521548ec48aff15825a22934ad21',
 'learning/signal_audit.py': 'de074926424fe66bdda2ecd9bba0b40fbd94f6b0fdef4e1384431175c621eb54',
 'learning/sprint.py': 'f1d65394c78243dc243a372c973b04e641c3dac3956994e357b01190099c040e',
 'learning/strategy_campaign.py': 'ffc20887e08c6a781040129bd68dbcf49b4c58a216e89b2c5fe099a88a845547',
 'learning/strategy_search.py': '777ad9750718efbe562685c748e64431c059e5320201a1af53cf1e20224974f8'}
AST_BASELINES = {'kaiba/learning/allocation_runtime.py': '563676752cccf00a1cae26e3507bfa2c784960d375bdb871a77fb67d3f7d4fdb',
 'kaiba/learning/autolearn.py': 'fa5c64fb8a85a114c5fdf7e7b5e4983e890dae7e0815c73f50f1a1cfc866982c'}
ADDITIONS = ("kaiba/learning/cooperative_reader.py", "kaiba/learning/research_service_upgrade.py")
ORIGINAL_FILES = ("metadata.json", "authority.json", "commit-receipt.json", "receipt.json", "new-books.json")
AST_POLICY = {"kaiba/learning/autolearn.py": {"body_only": ["AutoLearner._once"], "replace": [], "add": []},
    "kaiba/learning/allocation_runtime.py": {"body_only": ["source_versions", "load_context", "Context.__init__", "Context.assess",
                    "BoundRunner.run_candidates", "BoundRunner.expire_pending", "BoundRunner.watched_tokens"],
        "replace": [],
        "add": ["_load_context", "Context._revalidate_bindings"]}}


def _require(ok, reason):
    if not ok:
        raise ValueError("research_service_upgrade:" + reason)


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _workspace_root():
    return Path(__file__).resolve().parents[2]


def _read(path, cap=MAX_JSON):
    path = Path(path)
    _require(not any(p.is_symlink() for p in (path, *path.parents)) and path.is_file(), "missing_or_symlink_artifact")
    with path.open("rb") as stream:
        raw = stream.read(cap + 1)
    _require(len(raw) <= cap, "artifact_cap")
    return raw


def _object(raw):
    value = json.loads(raw)
    _require(isinstance(value, dict), "object_required")
    return value


def _ast_signature(raw, name, *, require_additions=True):
    """Mask only claimed definitions; every other AST node stays byte-identified."""
    policy = AST_POLICY[name]
    tree = ast.parse(raw.decode("utf-8-sig"))
    seen = set()

    def mask(body, prefix=""):
        result = []
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified = prefix + node.name
                _require(qualified not in seen, "duplicate_definition")
                seen.add(qualified)
                if qualified in policy["add"]:
                    continue
                if qualified in policy["replace"]:
                    result.append(ast.Pass())
                    continue
                if qualified in policy["body_only"]:
                    node.body = [ast.Pass()]
                elif isinstance(node, ast.ClassDef):
                    node.body = mask(node.body, qualified + ".")
            result.append(node)
        return result

    tree.body = mask(tree.body)
    required = policy["body_only"] + policy["replace"] + (policy["add"] if require_additions else [])
    _require(all(n in seen for n in required), "claimed_definition_missing")
    return hashlib.sha256(ast.dump(tree, annotate_fields=True, include_attributes=False).encode()).hexdigest()


def current_sources():
    root = _workspace_root()
    result = {}
    for name in (*SOURCE26, *ADDITIONS):
        raw = _read(root / name, MAX_SOURCE)
        result[name] = hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()
        if name in AST_POLICY:
            _require(_ast_signature(raw, name) == AST_BASELINES[name], "unclaimed_AST_change:" + name)
        elif name in SOURCE26:
            _require(result[name] == SOURCE26[name], "frozen_source_changed:" + name)
    for name, wanted in MODEL9.items():
        _require(hashlib.sha256(_read(root / "kaiba" / name, MAX_SOURCE)).hexdigest() == wanted, "raw_model9_changed")
    return result


def original_runtime_sources():
    """The original15 RAW metadata pins, not the external normalized26 map."""
    return {**MODEL9, **{name: SOURCE26["kaiba/" + name] for name in (
        "learning/admission_paper.py", "learning/research_allocation.py", "learning/allocation_runtime.py",
        "learning/autolearn.py", "learning/prospective_quotes.py", "learning/autolearn_review.py")}}


def _original(parent, artifacts):
    _require(set(artifacts) == set(ORIGINAL_FILES) and all(isinstance(v, bytes) for v in artifacts.values()), "original_artifacts_required")
    _require(all(len(raw) <= 2 * 1024 * 1024 for raw in artifacts.values()), "original_artifact_cap")
    meta, authority, completion, receipt, seal = (_object(artifacts[n]) for n in ORIGINAL_FILES)
    pointer = parent.get("research_allocation")
    _require(isinstance(pointer, dict) and pointer.get("metadata_file_sha256") == hashlib.sha256(artifacts["metadata.json"]).hexdigest(), "original_pointer_conflict")
    _require(meta["source_versions"] == original_runtime_sources(), "original_runtime15_conflict")
    _require(set(completion) == {"version", "authority_sha256", "metadata_sha256", "activation_ms", "visible_ms", "completed_ms"}
        and completion["version"] == "allocation-commit/1"
        and all(type(completion[n]) is int for n in ("activation_ms", "visible_ms", "completed_ms")), "original_commit_schema")
    _require(completion["metadata_sha256"] == hashlib.sha256(artifacts["metadata.json"]).hexdigest()
        and completion["authority_sha256"] == hashlib.sha256(artifacts["authority.json"]).hexdigest()
        and type(authority["activation_ms"]) is int and 0 < completion["visible_ms"] <= completion["completed_ms"] < authority["activation_ms"]
        and completion["activation_ms"] == authority["activation_ms"] == meta["activation_ms"] == pointer["activation_ms"], "original_timely_commit_conflict")
    _require(authority.get("generation") == meta.get("generation") == pointer.get("generation") == 1
        and authority.get("funding_allowed") is False and pointer.get("funding_allowed") is False, "original_generation_conflict")
    _require(meta["receipt_file_sha256"] == hashlib.sha256(artifacts["receipt.json"]).hexdigest()
        and meta["authority_file_sha256"] == hashlib.sha256(artifacts["authority.json"]).hexdigest()
        and meta["new_books_file_sha256"] == hashlib.sha256(artifacts["new-books.json"]).hexdigest()
        and authority["receipt_sha256"] == receipt["receipt_sha256"] == meta["receipt_sha256"] == pointer["receipt_sha256"]
        and authority["new_books_sha256"] == seal["seal_sha256"] == meta["new_books_sha256"] == pointer["new_books_sha256"], "original_chain_conflict")
    _require(receipt["receipt_sha256"] == _hash({k: v for k, v in receipt.items() if k != "receipt_sha256"})
        and seal["seal_sha256"] == _hash({k: v for k, v in seal.items() if k != "seal_sha256"}), "original_receipt_or_seal_hash")
    _require(receipt["envelope"] == {"max_active_arms": 12, "max_stake_usd": "30", "max_open_per_arm": 3}
        and receipt["lifetime_family_size"] == 22 and receipt["active_lease_count"] == 12, "original_family_or_envelope")
    _require(authority == {"version": "research-allocation/1", "receipt_sha256": receipt["receipt_sha256"],
        "new_books_sha256": seal["seal_sha256"], "activation_ms": meta["activation_ms"],
        "generation": 1, "funding_allowed": False}, "original_authority_schema")
    expected = {"version": "allocation-runtime/1", "source_versions": original_runtime_sources(),
        "receipt_file_sha256": hashlib.sha256(artifacts["receipt.json"]).hexdigest(),
        "authority_file_sha256": hashlib.sha256(artifacts["authority.json"]).hexdigest(),
        "new_books_file_sha256": hashlib.sha256(artifacts["new-books.json"]).hexdigest(),
        "receipt_sha256": receipt["receipt_sha256"], "new_books_sha256": seal["seal_sha256"],
        "activation_ms": authority["activation_ms"], "generation": 1, "envelope": receipt["envelope"],
        "legacy_campaign_files": list(parent.get("campaign_files", [])),
        "legacy_clock_sha256": _hash(parent.get("clock_upgrade")), "funding_allowed": False}
    _require(meta == expected and pointer == {"version": "allocation-runtime/1",
        "metadata_file_sha256": hashlib.sha256(artifacts["metadata.json"]).hexdigest(),
        "receipt_sha256": meta["receipt_sha256"], "new_books_sha256": meta["new_books_sha256"],
        "activation_ms": meta["activation_ms"], "generation": 1, "funding_allowed": False}, "original_metadata_schema_or_history")
    return {"raw_sha256": {n: hashlib.sha256(raw).hexdigest() for n, raw in artifacts.items()},
        "pointer": copy.deepcopy(pointer), "activation_ms": authority["activation_ms"], "generation": 1,
        "envelope": receipt["envelope"], "lifetime_family_size": receipt["lifetime_family_size"],
        "active_lease_count": receipt["active_lease_count"]}


def pointer_for(receipt_raw):
    receipt = _object(receipt_raw)
    return {"version": VERSION, "receipt_file_sha256": hashlib.sha256(receipt_raw).hexdigest(),
            "receipt_sha256": receipt["receipt_sha256"], "source_graph_sha256": _hash(receipt["new_sources"]),
            "activation_ms": receipt["original"]["activation_ms"], "funding_allowed": False}


def prepare_upgrade(parent, artifacts, *, prepared_ms):
    """Explicit read-only recipe. Installer owns durable CAS/lock/parent writes."""
    _require(type(prepared_ms) is int and prepared_ms > 0, "integer_clock_required")
    _require(POINTER not in parent, "already_latched_requires_original_upgrade_receipt")
    original = _original(parent, artifacts)
    _require(prepared_ms >= original["activation_ms"], "prepare_before_original_activation")
    receipt = {"version": VERSION, "prepared_ms": prepared_ms, "original": original,
        "old_sources": dict(SOURCE26), "new_sources": current_sources(), "raw_model9": dict(MODEL9),
        "ast_policy": copy.deepcopy(AST_POLICY), "ast_baselines": dict(AST_BASELINES),
        "original_artifacts_rewritten": False, "evidence_reset": False, "funding_allowed": False}
    receipt["receipt_sha256"] = _hash(receipt)
    raw = _encoded(receipt) + b"\n"
    _require(len(raw) <= MAX_JSON, "receipt_cap")
    return {"receipt": receipt, "receipt_raw": raw, "pointer": pointer_for(raw)}


def validate_upgrade(state_dir, parent, artifacts, *, now_ms):
    """Return None only when both upgrade latches are absent. Never writes."""
    directory = Path(state_dir) / "research-allocation" / DIRECTORY
    if POINTER not in parent and not os.path.lexists(directory):
        return None
    _require(POINTER in parent and directory.is_dir() and not directory.is_symlink(), "upgrade_latch_missing_or_conflicting")
    raw = _read(directory / "receipt.json")
    receipt = _object(raw)
    _require(set(receipt) == {"version", "prepared_ms", "original", "old_sources", "new_sources", "raw_model9",
        "ast_policy", "ast_baselines", "original_artifacts_rewritten", "evidence_reset", "funding_allowed", "receipt_sha256"}, "receipt_fields")
    _require(receipt["version"] == VERSION and receipt["receipt_sha256"] == _hash({k: v for k, v in receipt.items() if k != "receipt_sha256"}), "receipt_seal")
    _require(parent[POINTER] == pointer_for(raw), "parent_upgrade_binding")
    _require(receipt["original"] == _original(parent, artifacts), "original_bytes_or_activation_changed")
    _require(type(now_ms) is int and type(receipt["prepared_ms"]) is int
        and receipt["original"]["activation_ms"] <= receipt["prepared_ms"] <= now_ms, "upgrade_clock")
    _require(receipt["old_sources"] == SOURCE26 and receipt["raw_model9"] == MODEL9
        and receipt["ast_policy"] == AST_POLICY and receipt["ast_baselines"] == AST_BASELINES, "source_or_AST_policy_changed")
    _require(all(receipt[n] is False for n in ("original_artifacts_rewritten", "evidence_reset", "funding_allowed")), "upgrade_authority_claim")
    _require(receipt["new_sources"] == current_sources(), "current_source_graph_changed")
    return {"receipt_file_sha256": hashlib.sha256(raw).hexdigest(),
            "original_runtime_sources": original_runtime_sources(), "source_graph_sha256": _hash(receipt["new_sources"])}


__all__ = ["prepare_upgrade", "validate_upgrade", "pointer_for", "current_sources", "VERSION"]
