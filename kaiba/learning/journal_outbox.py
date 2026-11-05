"""Stable economic journal identities and lossless historical outbox migration.

Operational evidence only: no hit/miss credits, provider admission, model change,
clock reset or journal acknowledgement. Migration is a pure installer recipe.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

VERSION = "journal-outbox/1"
MIGRATION_POINTER = "journal_outbox_migration"
PRIMARY_PREFIX = "economic-revision:"
DIAGNOSTIC_PREFIX = "economic-revision-diagnostic:"
MAX_STATE_INPUT = 4 * 1024 * 1024
MAX_STATE_OUTPUT = 2 * 1024 * 1024
MAX_ECONOMIC_INPUT = 16 * 1024 * 1024
MAX_ARCHIVE = 8 * 1024 * 1024
REVISION_FIELDS = frozenset({"position_id", "previous_source_hash", "source_hash", "status", "at_ms",
                             "chain", "mode", "lane", "raw_cost_rows", "raw_cost_rows_hash"})
HASH = re.compile(r"[0-9a-f]{64}")


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError("journal_outbox:" + reason)


def _raw_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _encoded(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("journal_outbox:invalid_json_value") from exc


def _hash(value: Any) -> str:
    return _raw_hash(_encoded(value))


def _pairs(items: list[tuple[str, Any]]) -> dict:
    out: dict = {}
    for key, value in items:
        _require(key not in out, "duplicate_json_key")
        out[key] = value
    return out


def _constant(_value: str) -> None:
    raise ValueError("journal_outbox:nonfinite_json")


def _object(raw: bytes, cap: int) -> dict:
    _require(isinstance(raw, bytes) and len(raw) <= cap, "input_byte_cap")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("journal_outbox:invalid_json") from exc
    _require(isinstance(value, dict), "object_required")
    _encoded(value)  # Includes finite-looking float overflow such as 1e999.
    return value


def encode_state(redacted_json: str) -> bytes:
    """Compact the already-redacted old writer output, preserving its parsed value.

    The caller computes this before writing/replacing anything. Other artifacts
    retain their old serializer. No truncation or cap increase is permitted.
    """
    _require(isinstance(redacted_json, str), "redacted_json_text_required")
    original = _object(redacted_json.encode(), MAX_STATE_INPUT)
    raw = _encoded(original) + b"\n"
    _require(len(raw) < MAX_STATE_OUTPUT, "state_still_at_or_above_reader_cap")
    _require(_object(raw, MAX_STATE_OUTPUT) == original, "state_value_changed")
    return raw


def _revision(revision: Mapping) -> dict:
    _require(isinstance(revision, Mapping) and set(revision) == REVISION_FIELDS, "revision_shape")
    value = copy.deepcopy(dict(revision))
    _require(isinstance(value["position_id"], str) and 0 < len(value["position_id"]) <= 512,
             "revision_position")
    _require(type(value["at_ms"]) is int and 0 < value["at_ms"] < 2**63
             and type(value["raw_cost_rows"]) is int and value["raw_cost_rows"] >= 0, "revision_numbers")
    for key in ("previous_source_hash", "source_hash", "raw_cost_rows_hash"):
        _require((value[key] is None and key != "raw_cost_rows_hash")
                 or (isinstance(value[key], str) and HASH.fullmatch(value[key]) is not None), "revision_hash")
    _require(value["status"] in {"captured", "source_missing", "packet_budget_exceeded"}, "revision_status")
    _require(value["mode"] in {None, "live", "canary"}, "revision_money_mode")
    _require(all(value[key] is None or isinstance(value[key], str) for key in ("chain", "lane")), "revision_group")
    return value


def _reasons(reasons: Sequence[str]) -> list[str]:
    _require(isinstance(reasons, (list, tuple)) and len(reasons) <= 12
             and all(isinstance(reason, str) and len(reason) <= 512 for reason in reasons), "diagnostic_reasons")
    return list(reasons)  # Preserve the observed diagnostic ordering and missing-value caveats.


def _lessons(revision: Mapping, reasons: Sequence[str]) -> tuple[dict, dict]:
    core = _revision(revision)
    eid = PRIMARY_PREFIX + _hash(core)
    primary = {"evidence_id": eid, "lane": core["lane"],
        "text": "Revisited real-money evidence changed; unresolved custody and cashflows remain unproven.",
        "body": _encoded({"type": "economic_evidence_revision", **core, "funding_allowed": False,
            "unknown_unproven": True, "diagnostic_variants": {
                "revision_evidence_id": eid, "record_type": "economic_revision_diagnostic",
                "evidence_id_prefix": DIAGNOSTIC_PREFIX, "complete": False}}).decode()}
    diagnostic_body = {"type": "economic_revision_diagnostic", "revision_evidence_id": eid,
        "unknown_reasons": _reasons(reasons), "at_ms": core["at_ms"], "funding_allowed": False,
        "unknown_unproven": True}
    diagnostic = {"evidence_id": DIAGNOSTIC_PREFIX + _hash(diagnostic_body), "lane": core["lane"],
        "text": "Economic revision diagnostic variant; missing evidence remains unknown, never profitability credit.",
        "body": _encoded(diagnostic_body).decode()}
    return primary, diagnostic


def enqueue_economic_revision(state: dict, revision: Mapping, *, unknown_reasons: Sequence[str]) -> dict:
    """Append each stable revision and diagnostic variant at most once while pending.

    An unequal existing identity still refuses. Legacy conflicts require an
    explicitly archived migration; this is not a general conflict relaxer.
    """
    pending = state.get("pending_lessons")
    _require(isinstance(pending, list), "pending_list_required")
    seen: dict[str, dict] = {}
    for lesson in pending:
        _require(isinstance(lesson, dict) and isinstance(lesson.get("evidence_id"), str), "pending_identity")
        eid = lesson["evidence_id"]
        _require(eid not in seen or seen[eid] == lesson, "pending_lesson_identity_conflict")
        seen.setdefault(eid, lesson)
    wanted = _lessons(revision, unknown_reasons)
    for lesson in wanted:
        prior = seen.get(lesson["evidence_id"])
        _require(prior is None or prior == lesson, "economic_lesson_identity_conflict")
    added = [lesson for lesson in wanted if lesson["evidence_id"] not in seen]
    pending.extend(added)
    return {"added": len(added), "revision_added": wanted[0] in added,
            "diagnostic_added": wanted[1] in added, "funding_allowed": False}


def prepare_migration(state_raw: bytes, economic_work_raw: bytes, *, prepared_ms: int) -> dict:
    """Pure recipe: exact original bytes + indexed archive + replacement + receipt.

    Installer must hold the existing worker lock, CAS both observed input hashes,
    durably preserve both originals/archive/receipt, then publish state and the
    separately reviewed runtime upgrade. No journal or outbox acknowledgement is
    performed here. The recipe state hash precedes any separate runtime pointer.
    """
    state = _object(state_raw, MAX_STATE_INPUT)
    economic = _object(economic_work_raw, MAX_ECONOMIC_INPUT)
    _require(type(prepared_ms) is int and 0 < prepared_ms < 2**63, "prepared_clock")
    _require(MIGRATION_POINTER not in state, "already_migrated")
    pending = state.get("pending_lessons")
    outbox = economic.get("revision_outbox")
    _require(isinstance(pending, list) and isinstance(outbox, dict) and 0 < len(outbox) <= 256,
             "pending_or_revision_outbox_shape")
    cores = {}
    for key, revision in outbox.items():
        core = _revision(revision)
        _require(key == _hash(core) and core["at_ms"] <= prepared_ms, "outbox_core_identity_or_clock")
        cores[PRIMARY_PREFIX + key] = core
    groups: dict[str, list[tuple[int, dict, list[str]]]] = defaultdict(list)
    observations = []
    for index, lesson in enumerate(pending):
        _require(isinstance(lesson, dict) and isinstance(lesson.get("evidence_id"), str), "pending_identity")
        eid = lesson["evidence_id"]
        if not eid.startswith(PRIMARY_PREFIX):
            continue
        _require(set(lesson) == {"evidence_id", "lane", "text", "body"}, "legacy_lesson_fields")
        _require(isinstance(lesson["body"], str), "legacy_body_text")
        body = _object(lesson["body"].encode(), 16384)
        _require(set(body) == REVISION_FIELDS | {"type", "unknown_reasons", "funding_allowed"}
                 and body["type"] == "economic_evidence_revision" and body["funding_allowed"] is False,
                 "legacy_body_shape")
        core = _revision({key: body[key] for key in REVISION_FIELDS})
        _require(eid == PRIMARY_PREFIX + _hash(core) and cores.get(eid) == core
                 and lesson["lane"] == core["lane"], "legacy_revision_outbox_conflict")
        _require(lesson["text"] == _lessons(core, body["unknown_reasons"])[0]["text"], "legacy_text_changed")
        reasons = _reasons(body["unknown_reasons"])
        groups[eid].append((index, copy.deepcopy(lesson), reasons))
        observations.append({"pending_index": index, "lesson": copy.deepcopy(lesson),
                             "raw_body_sha256": _raw_hash(lesson["body"].encode())})
    _require(set(groups) == set(cores), "outbox_pending_identity_set_mismatch")
    replacements, provenance = {}, []
    for eid, rows in groups.items():
        primary, _ = _lessons(cores[eid], rows[0][2])
        diagnostics: dict[str, dict] = {}
        variants: dict[str, dict] = {}
        for index, lesson, reasons in rows:
            diagnostic = _lessons(cores[eid], reasons)[1]
            did = diagnostic["evidence_id"]
            diagnostics.setdefault(did, diagnostic)
            variant = variants.setdefault(did, {"diagnostic_evidence_id": did,
                "unknown_reasons": reasons, "pending_indexes": [], "raw_body_sha256s": []})
            variant["pending_indexes"].append(index)
            raw_hash = _raw_hash(lesson["body"].encode())
            if raw_hash not in variant["raw_body_sha256s"]:
                variant["raw_body_sha256s"].append(raw_hash)
        replacements[eid] = [primary, *diagnostics.values()]
        provenance.append({"revision_evidence_id": eid, "revision_sha256": _hash(cores[eid]),
            "observations": len(rows), "first_pending_index": rows[0][0],
            "diagnostic_evidence_ids": list(diagnostics), "variants": list(variants.values())})
    source_hash, economic_hash = _raw_hash(state_raw), _raw_hash(economic_work_raw)
    archive = {"version": VERSION, "source_state_sha256": source_hash,
        "source_economic_work_sha256": economic_hash, "observations": observations,
        "revision_provenance": provenance, "independent_revision_count": len(groups),
        "repeated_observations_are_not_new_samples": True, "funding_allowed": False}
    archive_raw = _encoded(archive) + b"\n"
    _require(len(archive_raw) <= MAX_ARCHIVE, "archive_byte_cap")
    next_state = copy.deepcopy(state)
    next_pending, emitted = [], set()
    for lesson in pending:
        eid = lesson["evidence_id"]
        if eid not in replacements:
            next_pending.append(copy.deepcopy(lesson))
        elif eid not in emitted:
            next_pending.extend(copy.deepcopy(replacements[eid]))
            emitted.add(eid)
    checked: dict[str, dict] = {}
    for item in next_pending:
        eid = item["evidence_id"]
        _require(eid not in checked or checked[eid] == item, "remaining_pending_identity_conflict")
        checked.setdefault(eid, item)
    pointer = {"version": VERSION, "prepared_ms": prepared_ms, "source_state_sha256": source_hash,
        "source_economic_work_sha256": economic_hash, "archive_sha256": _raw_hash(archive_raw),
        "archive_file": "pending-lessons.original.json", "funding_allowed": False}
    next_state["pending_lessons"] = next_pending
    next_state[MIGRATION_POINTER] = pointer
    unchanged_before = {k: v for k, v in state.items() if k != "pending_lessons"}
    unchanged_after = {k: v for k, v in next_state.items() if k not in {"pending_lessons", MIGRATION_POINTER}}
    _require(unchanged_before == unchanged_after, "other_state_changed")
    replacement_raw = encode_state(_encoded(next_state).decode())
    diagnostic_count = sum(len(v["diagnostic_evidence_ids"]) for v in provenance)
    receipt = {**pointer, "migration_state_sha256": _raw_hash(replacement_raw),
        "original_state_bytes": len(state_raw), "migration_state_bytes": len(replacement_raw),
        "original_economic_work_bytes": len(economic_work_raw), "original_observations": len(observations),
        "independent_revision_count": len(groups), "distinct_diagnostic_variants": diagnostic_count,
        "queued_economic_records": len(groups) + diagnostic_count,
        "unrelated_pending_records": len(pending) - len(observations),
        "other_state_sha256": _hash(unchanged_before), "other_state_unchanged": True,
        "economic_outbox_unchanged": True, "original_observations_preserved": True,
        "journal_acknowledgments": 0, "profitability_credits": 0, "source_queries": 0,
        "provider_calls": 0, "archive_bytes": len(archive_raw),
        "original_state_file": "state.original.json", "original_economic_work_file": "economic-work.original.json"}
    return {"state_raw": replacement_raw, "archive_raw": archive_raw,
        "receipt_raw": _encoded(receipt) + b"\n", "receipt": receipt,
        "original_state_raw": state_raw, "original_economic_work_raw": economic_work_raw}
