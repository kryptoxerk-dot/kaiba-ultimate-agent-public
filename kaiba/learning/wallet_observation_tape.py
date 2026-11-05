"""Bounded, pure retention of a fixed study's observed raw wallet tape.

This is an additional research artifact, not a replacement for canonical swaps or
the fixed study. It never reads a database, writes a file, requests a provider, or
qualifies a wallet for trading. Event-time query coverage does not establish that
the canonical database was never backfilled. ``captured_ms`` is the caller's actual
completed-read clock, distinct from the fixed study's logical selection cutoff.

``initialize(fixed_state, packets=(), now_ms=...)`` fixes all six baseline references.
Raw seed packets may be supplied independently; ``seed`` verifies each wallet. Each
packet is ``{wallet: (chain, address), rows: [...], receipt: {...}}``. A receipt has
``query_status``, ``window_start_ms``, ``as_of_ms``, ``captured_ms``, ``horizon_ms``.
Only ``declared_query_complete`` is accepted; caps/deferred reads are unknown.
Seed snapshots must reproduce that wallet's frozen raw/normalized baseline hashes.
An unseeded slot remains unknown while other verified slots collect evidence.

``ingest(state, wallet, rows, receipt, now_ms=...)`` unions a complete supplied
window with retained rows, never drops the old prefix, and detects observed rowid
mutations/backfills. A rejection returns an unchanged copy of the prior state and
a separate diagnostic. Callers must retain those diagnostics and provide them to
``report``; a stale committed snapshot must not be presented as current coverage.
``ingest_page`` separately accepts explicit lexicographic observed pages. A full
page is unfinished coverage; only a short final page proves the declared query
finished. Its exclusive cursor is the last retained ``(ts_ms,id)``, never time+1.
No absence-of-late-arrival guarantee is implied by these pure functions.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from kaiba.core.schemas import Chain
from kaiba.intelligence import grade
from kaiba.learning import fixed_wallet_study as fixed
from kaiba.learning.wallet_followthrough import analyze_followthrough

VERSION = "wallet-observation-tape/1"
MAX_INPUT_ROWS = 1_001
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_STATE_BYTES = 32 * 1024 * 1024
MAX_WALLET_ROWS = 10_000
MAX_RECEIPTS = 10_000
MAX_REJECTION_BYTES = 8_192
MAX_REPORT_BYTES = 1_024 * 1_024
RAW_FIELDS = ("id", "ts_ms", "token", "side", "amount_token", "amount_native",
              "usd_value", "source", "tx", "oversized_fields")
RECEIPT_FIELDS = ("query_status", "window_start_ms", "as_of_ms", "captured_ms", "horizon_ms")
PAGE_VERSION = "wallet-observation-page/1"
PAGE_FIELDS = ("page_version", "query_status", "window_start_ms", "requested_upper_ms",
               "captured_ms", "horizon_ms", "after", "returned_limit", "has_more")
MEMBERS = tuple((chain, address) for chain, address, _ in fixed.MEMBERS)


class ObservationRejected(ValueError):
    """A packet cannot extend the committed research tape."""


def _encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha(value: Any) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ObservationRejected(reason)


def _integer(value: Any, *, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _identity(wallet: Any) -> tuple[str, str]:
    _require(isinstance(wallet, (tuple, list)) and len(wallet) == 2, "wallet_identity_invalid")
    identity = tuple(wallet)
    _require(identity in MEMBERS, "wallet_not_in_fixed_cohort")
    return identity


def _name(wallet: tuple[str, str]) -> str:
    return ":".join(wallet)


def source_versions() -> dict[str, str]:
    """Additional tape pins; never changes the original fixed manifest's pins."""
    root = Path(__file__).resolve().parents[1]
    names = ("learning/wallet_observation_tape.py", "intelligence/grade.py",
             "intelligence/pnl.py", "learning/wallet_behavior_study.py",
             "learning/wallet_followthrough.py")
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}


def _receipt(receipt: Any, now: int) -> dict:
    _require(isinstance(receipt, Mapping) and set(receipt) == set(RECEIPT_FIELDS),
             "receipt_fields_invalid")
    receipt = copy.deepcopy(dict(receipt))
    _require(receipt["query_status"] == "declared_query_complete", "query_not_complete")
    _require(all(_integer(receipt[k]) for k in RECEIPT_FIELDS if k != "query_status"),
             "receipt_clock_invalid")
    start, end, captured, horizon = (receipt[k] for k in RECEIPT_FIELDS[1:])
    _require(0 < start <= end <= captured <= now, "receipt_clock_order_invalid")
    _require(horizon <= start, "receipt_before_retention_horizon")
    _require(start >= captured - fixed.ab.MAX_READ_AGE_MS, "source_window_older_than_13_days")
    return receipt


def _cursor(value: Any) -> bool:
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and _integer(value[0], minimum=1) and _integer(value[1], minimum=1))


def _page_receipt(receipt: Any, now: int, count: int) -> dict:
    _require(isinstance(receipt, Mapping) and set(receipt) == set(PAGE_FIELDS), "page_receipt_fields_invalid")
    receipt = copy.deepcopy(dict(receipt))
    _require(receipt["page_version"] == PAGE_VERSION and receipt["query_status"] == "observed_page",
             "page_version_or_status_invalid")
    _require(_integer(receipt["returned_limit"], minimum=1)
             and receipt["returned_limit"] <= MAX_INPUT_ROWS
             and count <= receipt["returned_limit"], "page_limit_invalid")
    _require(type(receipt["has_more"]) is bool
             and receipt["has_more"] == (count == receipt["returned_limit"]), "page_completion_claim_invalid")
    _receipt({"query_status": "declared_query_complete", "window_start_ms": receipt["window_start_ms"],
              "as_of_ms": receipt["requested_upper_ms"], "captured_ms": receipt["captured_ms"],
              "horizon_ms": receipt["horizon_ms"]}, now)
    _require(receipt["after"] is None or _cursor(receipt["after"]), "page_cursor_invalid")
    if receipt["after"] is not None:
        receipt["after"] = list(receipt["after"])
        _require(receipt["after"][0] <= receipt["requested_upper_ms"], "page_cursor_after_upper")
    return receipt


def _page_window(receipt: dict) -> dict:
    return {"query_status": "declared_query_complete", "window_start_ms": receipt["window_start_ms"],
            "as_of_ms": receipt["requested_upper_ms"], "captured_ms": receipt["captured_ms"],
            "horizon_ms": receipt["horizon_ms"]}


def _rows(rows: Any, wallet: tuple[str, str], receipt: dict) -> list[dict]:
    _require(isinstance(rows, (list, tuple)) and len(rows) <= MAX_INPUT_ROWS, "input_row_cap")
    _require(len(_encoded(rows)) <= MAX_INPUT_BYTES, "input_byte_cap")
    out, seen = [], set()
    for original in rows:
        _require(isinstance(original, Mapping), "row_not_mapping")
        _require(set(RAW_FIELDS) <= set(original)
                 and set(original) <= set(RAW_FIELDS) | {"chain", "wallet"}, "raw_row_fields_invalid")
        row = copy.deepcopy(dict(original))
        _require(_integer(row["id"], minimum=1) and _integer(row["ts_ms"], minimum=1),
                 "row_identity_clock_invalid")
        _require(row["id"] not in seen, "duplicate_rowid_in_packet")
        seen.add(row["id"])
        _require(receipt["window_start_ms"] <= row["ts_ms"] <= receipt["as_of_ms"],
                 "row_outside_declared_window")
        _require(row["oversized_fields"] is None or type(row["oversized_fields"]) is int
                 and row["oversized_fields"] == 0, "oversized_source_field")
        for field in RAW_FIELDS[2:-1]:
            value = row[field]
            _require(value is None or type(value) in (str, int), "source_value_type_invalid")
            _require(value is None or len(str(value).encode()) <= 512, "oversized_source_field")
        _require(row.get("chain", wallet[0]) == wallet[0]
                 and row.get("wallet", wallet[1]) == wallet[1], "row_wallet_mismatch")
        row.update(chain=wallet[0], wallet=wallet[1])
        out.append(row)
    _require(out == sorted(out, key=lambda r: (r["ts_ms"], r["id"])), "query_rows_not_ordered")
    return out


def _baseline_rows(rows: list[dict], selection_ms: int) -> list[dict]:
    return [row for row in rows if row["ts_ms"] <= selection_ms]


def _anchor(rows: list[dict], baseline: dict, selection: int, wallet: tuple[str, str], receipt: dict) -> None:
    _require(receipt["window_start_ms"] == baseline["window_start_ms"], "seed_baseline_window_mismatch")
    _require(receipt["as_of_ms"] >= selection, "seed_before_selection")
    _require(baseline["captured_ms"] == selection, "seed_original_baseline_was_late")
    _require(baseline["source_hash"] == _sha(fixed.ab.source_versions()), "seed_baseline_source_conflict")
    before = _baseline_rows(rows, selection)
    _require(len(before) == baseline["rows"] and _sha(before) == baseline["raw_sha256"],
             "seed_baseline_raw_hash_mismatch")
    normalized = grade.normalise_tape_rows(before, Chain(wallet[0]))
    _require(_sha(normalized.rows) == baseline["normalization_sha256"]
             and normalized.coverage.money_axis == baseline["money_axis"],
             "seed_baseline_normalization_mismatch")


def _seal(state: dict) -> dict:
    state = copy.deepcopy(state)
    state.pop("state_sha256", None)
    state["state_sha256"] = _sha(state)
    _require(len(_encoded(state)) <= MAX_STATE_BYTES, "state_byte_cap")
    return state


def _state(state: Any, now: int) -> dict:
    _require(isinstance(state, Mapping) and len(_encoded(state)) <= MAX_STATE_BYTES, "state_missing_or_oversized")
    state = copy.deepcopy(dict(state))
    _require(state.get("version") == VERSION, "state_version_mismatch")
    expected = state.pop("state_sha256", None)
    _require(expected == _sha(state), "state_integrity_mismatch")
    state["state_sha256"] = expected
    _require(state.get("source_versions") == source_versions(), "tape_source_conflict")
    _require(_integer(state.get("created_ms"), minimum=1)
             and state["created_ms"] <= now, "state_clock_invalid")
    fixed_state = fixed._study(state.get("fixed_state"), now)
    _require("manifest" in fixed_state, "fixed_study_not_active")
    _require(all(row["baseline"] is not None for row in fixed_state["selections"].values()),
             "fixed_study_baselines_missing")
    _require(state.get("manifest_sha256") == fixed_state["manifest_sha256"], "manifest_conflict")
    wallets = state.get("wallets")
    _require(isinstance(wallets, dict) and set(wallets) == {_name(w) for w in MEMBERS}, "state_wallets_invalid")
    globally_seen = set()
    for wallet in MEMBERS:
        saved = wallets[_name(wallet)]
        _require(isinstance(saved, dict), "wallet_state_invalid")
        entries = saved.get("rows")
        _require(isinstance(entries, list) and len(entries) <= MAX_WALLET_ROWS, "wallet_state_row_cap")
        _require(isinstance(saved.get("receipts"), list) and len(saved["receipts"]) <= MAX_RECEIPTS,
                 "wallet_state_receipts_invalid")
        _require(saved.get("seed_status") in {"unseeded", "verified"}, "wallet_seed_status_invalid")
        baseline = fixed_state["selections"][_name(wallet)]["baseline"]
        _require(saved.get("seed_baseline_raw_sha256") == baseline["raw_sha256"], "stored_baseline_anchor_conflict")
        if saved["seed_status"] == "unseeded":
            _require(not entries and not saved["receipts"] and not saved.get("unobserved_intervals")
                     and saved.get("seed_actual_capture_ms") is None, "unseeded_slot_contains_facts")
            continue
        _require(bool(saved["receipts"]), "verified_seed_receipt_missing")
        seen = set()
        previous = None
        for entry in entries:
            _require(isinstance(entry, dict) and set(entry) == {"raw", "first_captured_ms"},
                     "retained_row_invalid")
            row, capture = entry["raw"], entry["first_captured_ms"]
            _require(isinstance(row, dict) and set(row) == set(RAW_FIELDS) | {"chain", "wallet"},
                     "retained_row_fields_invalid")
            _require(_integer(row["id"], minimum=1) and row["id"] not in seen,
                     "retained_rowid_invalid")
            _require(row["id"] not in globally_seen, "retained_cross_wallet_rowid_conflict")
            globally_seen.add(row["id"])
            seen.add(row["id"])
            _require((row["chain"], row["wallet"]) == wallet, "retained_wallet_conflict")
            _require(_integer(row["ts_ms"], minimum=1) and _integer(capture, minimum=1)
                     and row["ts_ms"] <= capture <= now, "retained_capture_clock_invalid")
            ordered = (row["ts_ms"], row["id"])
            _require(previous is None or previous < ordered, "retained_rows_not_ordered")
            previous = ordered
        indexed = {entry["raw"]["id"]: entry for entry in entries}
        captured_ids = set()
        for index, record in enumerate(saved["receipts"]):
            _require(isinstance(record, dict), "stored_receipt_invalid")
            body = {k: v for k, v in record.items() if k != "chain_sha256"}
            _require(record.get("chain_sha256") == _sha(body), "receipt_chain_hash_mismatch")
            _require(record.get("previous_sha256") == (saved["receipts"][index - 1]["chain_sha256"]
                     if index else None), "receipt_chain_broken")
            rowids = record.get("packet_rowids")
            _require(isinstance(rowids, list) and len(rowids) <= MAX_INPUT_ROWS
                     and all(_integer(rowid, minimum=1) and rowid in indexed for rowid in rowids)
                     and len(set(rowids)) == len(rowids) and record.get("packet_rows") == len(rowids),
                     "stored_packet_rowids_invalid")
            packet = [indexed[rowid]["raw"] for rowid in rowids]
            _require(_sha(packet) == record.get("packet_raw_sha256"), "stored_packet_raw_hash_mismatch")
            newly_seen = set(rowids) - captured_ids
            _require(record.get("added_rows") == len(newly_seen)
                     and record.get("retained_rows") == len(captured_ids | set(rowids)),
                     "stored_packet_counts_invalid")
            _require(all(indexed[rowid]["first_captured_ms"] == record["query"]["captured_ms"]
                         for rowid in newly_seen), "first_capture_receipt_conflict")
            captured_ids.update(rowids)
            if record.get("kind") == "page":
                _page_receipt(record.get("query"), now, record["packet_rows"])
            else:
                _receipt(record.get("query"), now)
        _require(captured_ids == set(indexed), "retained_row_without_capture_receipt")
        _require(saved.get("seed_actual_capture_ms") == saved["receipts"][0]["query"]["captured_ms"],
                 "seed_capture_clock_conflict")
        _require(all(e["first_captured_ms"] >= saved["seed_actual_capture_ms"] for e in entries),
                 "retained_capture_before_seed")
        _require(_sha(_baseline_rows([e["raw"] for e in entries], fixed_state["manifest"]["selected_ms"]))
                 == baseline["raw_sha256"], "retained_baseline_anchor_conflict")
        _require(saved.get("latest_capture_ms") == saved["receipts"][-1]["query"]["captured_ms"],
                 "latest_capture_conflict")
        last_query = saved["receipts"][-1]["query"]
        _require(saved.get("latest_as_of_ms") == last_query.get("as_of_ms", last_query.get("requested_upper_ms")),
                 "latest_as_of_conflict")
        _require(saved.get("page_cursor") is None or _cursor(saved["page_cursor"]), "stored_page_cursor_invalid")
        _require(_integer(saved.get("completed_through_ms"), minimum=1)
                 and saved["completed_through_ms"] <= saved["latest_as_of_ms"], "completed_clock_invalid")
        page = saved.get("open_page_window")
        _require(page is None or isinstance(page, dict) and set(page) == {"window_start_ms", "requested_upper_ms", "cursor"}
                 and _cursor(page["cursor"]) and page["cursor"] == saved["page_cursor"], "open_page_window_invalid")
        _require(isinstance(saved.get("unobserved_intervals"), list)
                 and all(isinstance(gap, dict) and set(gap) == {"start_ms", "end_ms", "token"}
                         and _integer(gap["start_ms"], minimum=1) and _integer(gap["end_ms"], minimum=1)
                         and gap["start_ms"] <= gap["end_ms"] <= saved["latest_as_of_ms"]
                         and gap["token"] is None for gap in saved["unobserved_intervals"]), "stored_gaps_invalid")
        _require(saved.get("retained_raw_sha256") == _sha([e["raw"] for e in entries]),
                 "retained_raw_hash_mismatch")
    return state


def _rejected(prior: Any, reason: str, wallet: Any = None) -> dict:
    wallet = (list(wallet) if isinstance(wallet, (tuple, list)) and len(wallet) == 2
              and all(isinstance(part, str) and len(part) <= 128 for part in wallet) else None)
    diagnostic = {"status": "unknown", "reason": reason, "wallet": wallet,
                  "committed_state_changed": False, "current_coverage_proven": False,
                  "profitability_status": "UNPROVEN", "copy_qualified": False}
    _require(len(_encoded(diagnostic)) <= MAX_REJECTION_BYTES, "rejection_diagnostic_too_large")
    return {"status": "unknown", "reason": reason, "state": copy.deepcopy(prior),
            "diagnostic": diagnostic}


def _foreign_rowids(state: dict, identity: tuple[str, str], rows: list[dict]) -> None:
    other_ids = {entry["raw"]["id"] for name, saved in state["wallets"].items()
                 if name != _name(identity) for entry in saved["rows"]}
    _require(not other_ids.intersection(row["id"] for row in rows), "rowid_already_owned_by_other_wallet")


def _append(saved: dict, rows: list[dict], receipt: dict, *, selection: int, seed: bool = False) -> bool:
    _require(len(rows) < MAX_INPUT_ROWS, "complete_window_contains_limit_sentinel")
    old = {entry["raw"]["id"]: entry for entry in saved["rows"]}
    incoming = {row["id"]: row for row in rows}
    _require(all(rowid not in old or row == old[rowid]["raw"] for rowid, row in incoming.items()),
             "observed_rowid_mutation")
    _require(all(seed or row["ts_ms"] > selection or row["id"] in old for row in rows),
             "observed_late_baseline_backfill")
    _require(all(entry["raw"]["id"] in incoming
                 for entry in old.values()
                 if receipt["window_start_ms"] <= entry["raw"]["ts_ms"] <= receipt["as_of_ms"]),
             "previously_observed_row_missing_from_complete_window")
    if saved["receipts"]:
        _require(saved.get("open_page_window") is None, "unfinished_page_window_requires_page_ingest")
        _require(receipt["captured_ms"] >= saved["latest_capture_ms"]
                 and receipt["as_of_ms"] >= saved["latest_as_of_ms"], "observation_clock_regressed")
        last = saved["receipts"][-1]
        if receipt == last["query"] and _sha(rows) == last["packet_raw_sha256"]:
            return False
    added = [row for row in rows if row["id"] not in old]
    _require(len(old) + len(added) <= MAX_WALLET_ROWS, "retained_wallet_row_cap")
    _require(len(saved["receipts"]) < MAX_RECEIPTS, "retained_receipt_cap")
    gaps = copy.deepcopy(saved["unobserved_intervals"])
    if saved["receipts"] and receipt["window_start_ms"] > saved["latest_as_of_ms"] + 1:
        gaps.append({"start_ms": saved["latest_as_of_ms"] + 1,
                     "end_ms": receipt["window_start_ms"] - 1, "token": None})
    retained = saved["rows"] + [{"raw": row, "first_captured_ms": receipt["captured_ms"]} for row in added]
    retained.sort(key=lambda entry: (entry["raw"]["ts_ms"], entry["raw"]["id"]))
    raw_hash = _sha([entry["raw"] for entry in retained])
    record = {"kind": "complete_window", "query": receipt, "packet_raw_sha256": _sha(rows),
              "packet_rowids": [row["id"] for row in rows], "packet_rows": len(rows),
              "added_rows": len(added), "retained_rows": len(retained), "retained_raw_sha256": raw_hash,
              "previous_sha256": saved["receipts"][-1]["chain_sha256"] if saved["receipts"] else None}
    record["chain_sha256"] = _sha(record)
    saved.update(rows=retained, receipts=saved["receipts"] + [record], unobserved_intervals=gaps,
                 retained_raw_sha256=raw_hash, latest_capture_ms=receipt["captured_ms"],
                 latest_as_of_ms=receipt["as_of_ms"], completed_through_ms=receipt["as_of_ms"],
                 open_page_window=None, page_cursor=saved.get("page_cursor"))
    return True


def _append_page(saved: dict, rows: list[dict], receipt: dict, selection: int) -> bool:
    old = {entry["raw"]["id"]: entry for entry in saved["rows"]}
    incoming = {row["id"]: row for row in rows}
    after = tuple(receipt["after"]) if receipt["after"] is not None else None
    _require(all(after is None or (row["ts_ms"], row["id"]) > after for row in rows), "page_row_not_after_cursor")
    _require(receipt["captured_ms"] >= saved["latest_capture_ms"]
             and receipt["requested_upper_ms"] >= saved["latest_as_of_ms"], "observation_clock_regressed")
    previous = saved["receipts"][-1]
    if previous.get("kind") == "page" and receipt == previous["query"] and _sha(rows) == previous["packet_raw_sha256"]:
        return False
    page = saved.get("open_page_window")
    expected = saved.get("page_cursor")
    if page is not None:
        _require(receipt["after"] == page["cursor"], "page_cursor_did_not_continue")
        _require(receipt["window_start_ms"] >= page["window_start_ms"], "page_window_regressed")
        _require(receipt["requested_upper_ms"] == page["requested_upper_ms"], "unfinished_page_upper_changed")
    else:
        _require(receipt["after"] is None or receipt["after"] == expected, "page_cursor_not_last_retained_tuple")
    _require(all(rowid not in old or row == old[rowid]["raw"] for rowid, row in incoming.items()),
             "observed_rowid_mutation")
    _require(all(row["ts_ms"] > selection or row["id"] in old for row in rows), "observed_late_baseline_backfill")
    last_tuple = (rows[-1]["ts_ms"], rows[-1]["id"]) if rows else after
    for entry in old.values():
        row = entry["raw"]
        point = (row["ts_ms"], row["id"])
        in_window = receipt["window_start_ms"] <= row["ts_ms"] <= receipt["requested_upper_ms"]
        in_prefix = not receipt["has_more"] or last_tuple is not None and point <= last_tuple
        if in_window and in_prefix and (after is None or point > after):
            _require(row["id"] in incoming, "previously_observed_row_missing_from_page_prefix")
    added = [row for row in rows if row["id"] not in old]
    _require(len(old) + len(added) <= MAX_WALLET_ROWS, "retained_wallet_row_cap")
    _require(len(saved["receipts"]) < MAX_RECEIPTS, "retained_receipt_cap")
    gaps = copy.deepcopy(saved["unobserved_intervals"])
    previous_through = page["cursor"][0] if page is not None else saved["completed_through_ms"]
    if (receipt["window_start_ms"] > previous_through if page is not None
            else receipt["window_start_ms"] > previous_through + 1):
        # A cursor may be inside a timestamp bucket; include that bucket conservatively.
        start = previous_through if page is not None else previous_through + 1
        gaps.append({"start_ms": start, "end_ms": receipt["window_start_ms"] - 1, "token": None})
    retained = saved["rows"] + [{"raw": row, "first_captured_ms": receipt["captured_ms"]} for row in added]
    retained.sort(key=lambda entry: (entry["raw"]["ts_ms"], entry["raw"]["id"]))
    raw_hash = _sha([entry["raw"] for entry in retained])
    record = {"kind": "page", "query": receipt, "packet_raw_sha256": _sha(rows),
              "packet_rowids": [row["id"] for row in rows], "packet_rows": len(rows),
              "added_rows": len(added), "retained_rows": len(retained), "retained_raw_sha256": raw_hash,
              "last_retained_tuple": list(last_tuple) if last_tuple is not None else None,
              "previous_sha256": saved["receipts"][-1]["chain_sha256"]}
    record["chain_sha256"] = _sha(record)
    saved.update(rows=retained, receipts=saved["receipts"] + [record], unobserved_intervals=gaps,
                 retained_raw_sha256=raw_hash, latest_capture_ms=receipt["captured_ms"],
                 latest_as_of_ms=receipt["requested_upper_ms"],
                 page_cursor=list(last_tuple) if last_tuple is not None else saved.get("page_cursor"),
                 open_page_window={"window_start_ms": receipt["window_start_ms"],
                                   "requested_upper_ms": receipt["requested_upper_ms"],
                                   "cursor": list(last_tuple)} if receipt["has_more"] else None)
    if not receipt["has_more"]:
        saved["completed_through_ms"] = receipt["requested_upper_ms"]
    return True


def initialize(fixed_state: Mapping[str, Any], packets: Sequence[Mapping[str, Any]] = (), *, now_ms: int) -> dict:
    """Explicitly bind all six existing references; verify supplied seeds independently.

    There is no implicit initialize in ``ingest``. Repeated initialization is a new
    caller decision, not an update/recovery path. Persistence must refuse replacing
    an existing artifact; no persistence API is part of this module.
    """
    try:
        _require(_integer(now_ms, minimum=1), "now_clock_invalid")
        _require(isinstance(packets, (list, tuple)) and len(packets) <= len(MEMBERS), "seed_packet_count_invalid")
        _require(len(_encoded(packets)) <= MAX_INPUT_BYTES, "initial_input_byte_cap")
        validated = fixed._study(fixed_state, now_ms)
        _require("manifest" in validated, "fixed_study_not_active")
        _require(all(row["baseline"] is not None for row in validated["selections"].values()),
                 "fixed_study_baselines_missing")
        state = {"version": VERSION, "source_versions": source_versions(), "created_ms": now_ms,
                 "fixed_state": validated, "manifest_sha256": validated["manifest_sha256"], "wallets": {
                     _name(wallet): {"seed_status": "unseeded", "rows": [], "receipts": [],
                                     "unobserved_intervals": [], "seed_actual_capture_ms": None,
                                     "seed_baseline_raw_sha256": validated["selections"][_name(wallet)]["baseline"]["raw_sha256"]}
                     for wallet in MEMBERS}}
        state = _seal(state)
        seen, diagnostics = set(), []
        for packet in packets:
            _require(isinstance(packet, Mapping) and set(packet) == {"wallet", "rows", "receipt"},
                     "seed_packet_fields_invalid")
            wallet = _identity(packet["wallet"])
            name = _name(wallet)
            _require(name not in seen, "duplicate_seed_wallet")
            seen.add(name)
            result = seed(state, wallet, packet["rows"], packet["receipt"], now_ms=now_ms)
            state = result["state"]
            if result["diagnostic"] is not None:
                diagnostics.append(result["diagnostic"])
        return {"status": "initialized", "state": state, "diagnostic": None,
                "seed_diagnostics": diagnostics,
                "seed_statuses": {name: slot["seed_status"] for name, slot in state["wallets"].items()}}
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return _rejected(None, str(exc))


def seed(state: Mapping[str, Any], wallet: tuple[str, str], rows: Sequence[Mapping[str, Any]],
         receipt: Mapping[str, Any], *, now_ms: int) -> dict:
    """Verify one original baseline without waiting for the other five raw packets."""
    try:
        _require(_integer(now_ms, minimum=1), "now_clock_invalid")
        identity = _identity(wallet)
        next_state = _state(state, now_ms)
        name = _name(identity)
        saved = next_state["wallets"][name]
        _require(saved["seed_status"] == "unseeded", "wallet_seed_already_verified")
        query = _receipt(receipt, now_ms)
        observed = _rows(rows, identity, query)
        _foreign_rowids(next_state, identity, observed)
        baseline = next_state["fixed_state"]["selections"][name]["baseline"]
        selection = next_state["fixed_state"]["manifest"]["selected_ms"]
        _anchor(observed, baseline, selection, identity, query)
        saved.update(seed_status="verified", seed_actual_capture_ms=query["captured_ms"])
        _append(saved, observed, query, selection=selection, seed=True)
        return {"status": "seeded", "state": _seal(next_state), "diagnostic": None}
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return _rejected(state, str(exc), wallet)


def ingest(state: Mapping[str, Any], wallet: tuple[str, str], rows: Sequence[Mapping[str, Any]],
           receipt: Mapping[str, Any], *, now_ms: int) -> dict:
    """Atomically union one complete packet. Invalid/capped input never adds rows."""
    try:
        _require(_integer(now_ms, minimum=1), "now_clock_invalid")
        identity = _identity(wallet)
        next_state = _state(state, now_ms)
        _require(next_state["wallets"][_name(identity)]["seed_status"] == "verified", "wallet_seed_unverified")
        query = _receipt(receipt, now_ms)
        observed = _rows(rows, identity, query)
        _foreign_rowids(next_state, identity, observed)
        changed = _append(next_state["wallets"][_name(identity)], observed, query,
                          selection=next_state["fixed_state"]["manifest"]["selected_ms"])
        return {"status": "committed" if changed else "unchanged", "state": _seal(next_state), "diagnostic": None}
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return _rejected(state, str(exc), wallet)


def ingest_page(state: Mapping[str, Any], wallet: tuple[str, str], rows: Sequence[Mapping[str, Any]],
                receipt: Mapping[str, Any], *, now_ms: int) -> dict:
    """Retain observed page facts without mistaking a full page for completeness.

    The caller must query ``(ts_ms,id) > after`` in that exact order. The module
    never invents or advances a cursor beyond the final retained tuple. Full pages
    remain partial even when the caller thinks no more rows exist; an additional
    short/empty page is required. Explicit horizon jumps retain permanent gaps.
    """
    try:
        _require(_integer(now_ms, minimum=1), "now_clock_invalid")
        identity = _identity(wallet)
        next_state = _state(state, now_ms)
        _require(next_state["wallets"][_name(identity)]["seed_status"] == "verified", "wallet_seed_unverified")
        _require(isinstance(rows, (tuple, list)), "input_rows_invalid")
        query = _page_receipt(receipt, now_ms, len(rows))
        observed = _rows(rows, identity, _page_window(query))
        _foreign_rowids(next_state, identity, observed)
        changed = _append_page(next_state["wallets"][_name(identity)], observed, query,
                               next_state["fixed_state"]["manifest"]["selected_ms"])
        return {"status": "committed" if changed else "unchanged", "state": _seal(next_state), "diagnostic": None}
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return _rejected(state, str(exc), wallet)


def report(state: Mapping[str, Any], *, now_ms: int, diagnostics: Sequence[Mapping[str, Any]] = ()) -> dict:
    """Describe retained partial leader tape; no arrival, fill or net-profit claim.

    Unknown supplied diagnostics prevent a claim of current query coverage. Source
    event timestamps cannot substitute for capture/arrival or prove absence of
    canonical backfills. Seed rows observed after T0 remain retrospectively seeded.
    """
    try:
        _require(_integer(now_ms, minimum=1), "now_clock_invalid")
        checked = _state(state, now_ms)
        _require(isinstance(diagnostics, (tuple, list)) and len(_encoded(diagnostics)) <= MAX_INPUT_BYTES,
                 "diagnostics_invalid")
        profiles, coverage = [], []
        selection = checked["fixed_state"]["manifest"]["selected_ms"]
        for wallet in MEMBERS:
            name = _name(wallet)
            saved = checked["wallets"][name]
            if saved["seed_status"] != "verified":
                coverage.append({"wallet": list(wallet), "seed_status": "unseeded",
                                 "retained_rows": 0, "current_query_coverage": "UNPROVEN"})
                profiles.append({"chain": wallet[0], "wallet": wallet[1], "status": "unseeded",
                                 "profitability_status": "UNPROVEN", "live_copy_approved": False})
                continue
            raw = [entry["raw"] for entry in saved["rows"]]
            normalized = grade.normalise_tape_rows(raw, Chain(wallet[0]))
            baseline = checked["fixed_state"]["selections"][name]["baseline"]
            reconstructed_baseline_sha256 = _sha(_baseline_rows(normalized.rows, selection))
            baseline_normalization_matches = (reconstructed_baseline_sha256 == baseline["normalization_sha256"]
                                               and normalized.coverage.money_axis == baseline["money_axis"])
            gaps = copy.deepcopy(saved["unobserved_intervals"])
            if saved["open_page_window"] is not None:
                gaps.append({"start_ms": saved["open_page_window"]["cursor"][0],
                             "end_ms": saved["latest_as_of_ms"], "token": None})
            wallet_diagnostics = [d for d in diagnostics
                                  if not isinstance(d, Mapping) or d.get("wallet") is None
                                  or tuple(d.get("wallet", ())) == wallet]
            metadata = {"money_axis": normalized.coverage.money_axis,
                        "normalization": normalized.coverage.model_dump(mode="json"),
                        "baseline": {**baseline, "query_missing": bool(wallet_diagnostics)
                                     or saved["open_page_window"] is not None or not baseline_normalization_matches},
                        "baseline_normalization_matches_frozen": baseline_normalization_matches,
                        "reconstructed_baseline_normalization_sha256": reconstructed_baseline_sha256,
                        "unobserved_intervals": gaps,
                        "seed_actual_capture_ms": saved["seed_actual_capture_ms"],
                        "canonical_backfill_absence": "UNPROVEN", "partial_history": True}
            profile = analyze_followthrough(normalized.rows, chain=wallet[0], wallet=wallet[1],
                                             selection_ms=selection, as_of_ms=saved["latest_as_of_ms"],
                                             metadata=metadata)
            profile["baseline_frozen_normalization_status"] = "MATCHED" if baseline_normalization_matches else "CHANGED"
            profiles.append(fixed.ab._compact(profile))
            coverage.append({"wallet": list(wallet), "seed_status": "verified", "retained_rows": len(raw),
                             "first_actual_capture_ms": saved["seed_actual_capture_ms"],
                             "latest_actual_capture_ms": saved["latest_capture_ms"],
                             "latest_query_as_of_ms": saved["latest_as_of_ms"],
                             "completed_through_ms": saved["completed_through_ms"],
                             "page_cursor": saved["page_cursor"],
                             "page_window_status": "partial_window" if saved["open_page_window"] else "declared_query_complete",
                             "baseline_normalization_matches_frozen": baseline_normalization_matches,
                             "packet_count": len(saved["receipts"]),
                             "unobserved_intervals": saved["unobserved_intervals"],
                             "raw_chain_sha256": saved["receipts"][-1]["chain_sha256"]})
        unknown_coverage = bool(diagnostics) or any(slot["seed_status"] != "verified"
                              or slot["open_page_window"] is not None or slot["unobserved_intervals"]
                              for slot in checked["wallets"].values())
        result = {"version": VERSION, "status": "observed_partial_leader_tape", "state_sha256": checked["state_sha256"],
                "manifest_sha256": checked["manifest_sha256"], "selection_ms": selection,
                "coverage": coverage, "profiles": profiles, "diagnostics": copy.deepcopy(list(diagnostics)),
                "verified_seed_wallets": sum(slot["seed_status"] == "verified" for slot in checked["wallets"].values()),
                "fixed_wallet_denominator": len(MEMBERS),
                "current_query_coverage": "UNPROVEN" if unknown_coverage else "declared_windows_only",
                "profitability_status": "UNPROVEN", "arrival_evidence": "UNPROVEN",
                "canonical_backfill_absence": "UNPROVEN", "complete_cashflow": "UNPROVEN",
                "follower_execution": "UNPROVEN", "copy_qualified": False, "funding_allowed": False,
                "provider_calls": 0, "financial_actions": 0}
        _require(len(_encoded(result)) <= MAX_REPORT_BYTES, "report_byte_cap")
        return result
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return {"version": VERSION, "status": "unknown", "reason": str(exc), "profiles": [],
                "profitability_status": "UNPROVEN", "copy_qualified": False, "funding_allowed": False}
