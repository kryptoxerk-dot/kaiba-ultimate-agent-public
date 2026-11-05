"""Fixed research followups inside the existing wallet-observation budget.

The fixed and original dynamic selections have separate immutable baselines.
One existing collector reads six fixed wallets and at most four rotating nominees.
No provider, wallet-registry, roster, financial or paper-book API is called.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from kaiba.intelligence.wallet_campaign import key
from kaiba.learning import autolearn_behavior as ab
from kaiba.learning.autolearn_io import GuardedReader, SourceDeferred

VERSION = "fixed-wallet-study/1"
MAX_STUDY_BYTES = 32_768
MEMBERS = (
    ("sol", "GC69iAK53t8wBJo2Etgb5Q83UeySFp8LgbMUd9teu9kR", "slower_positive_followup"),
    ("bsc", "0x49a9b255f2ab4775f9961bfbec9201317436bd12", "positive_fast_trim_followup"),
    ("sol", "HzyQX8dWoDhsYSiQE7o17U6oYpXGYCuHpevPiZVYMWUy", "losing_open_inventory_comparison"),
    ("sol", "49JHPfVoudtzhbR4dyUqnx79BcHz1SYtFYm2Dk3LKs8N", "historical_losing_top3_fragile_comparison"),
    ("bsc", "0xe86872fb1931ebfda5526a9797b3eed54883bf39", "fast_trim_thin_margin_comparison"),
    ("bsc", "0xc518b9b8dea5149733f41911da756cfb8f694a51", "concentrated_gain_comparison"),
)
RATIONALE_SHA256 = "2fd6d0dbc06874bd5a5cfc9d786a73537b94c8bd4ccb2a48c06b0071a6128e2a"
POLICY = {"fixed_slots": 6, "dynamic_queue_slots": 14, "dynamic_reads_per_round": 4,
          "max_queue": 20, "max_wallet_reads": 10, "partial_leader_research_only": True,
          "complete_wallet_history": False, "follower_execution_known": False,
          "funding_allowed": False}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def source_versions() -> dict:
    return {**ab.source_versions(),
            "learning/fixed_wallet_study.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def _members() -> list[dict]:
    return [{"chain": c, "address": a, "role": r} for c, a, r in MEMBERS]


def _selection(chain: str, address: str, now: int) -> dict:
    return {"selection_ms": now, "window_start_ms": max(0, now - ab.INITIAL_LOOKBACK_MS),
            "selection_id": ab._hash([ab.VERSION, chain, address, now]), "baseline": None}


def initial_study_state() -> dict:
    """Explicit installer/fresh-state marker; it contains no fabricated selection clock."""
    return {"version": VERSION, "status": "awaiting_initial_capture", "source_versions": source_versions()}


def _hex(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _study(raw: Any, now: int) -> dict:
    if raw == initial_study_state():
        manifest = {"version": VERSION, "selected_ms": now, "members": _members(),
                    "source_versions": source_versions(), "policy": copy.deepcopy(POLICY),
                    "selection_rationale_sha256": RATIONALE_SHA256,
                    "rationale_is_forward_evidence": False}
        return {"version": VERSION, "manifest": manifest, "manifest_sha256": _hash(manifest),
                "round": 0, "selections": {c + ":" + a: _selection(c, a, now) for c, a, _ in MEMBERS}}
    if (not isinstance(raw, Mapping) or ab._storage_bytes(raw) > MAX_STUDY_BYTES
            or raw.get("version") != VERSION or raw.get("status") == "unknown"):
        raise ValueError("fixed_study_invalid_state")
    manifest = raw.get("manifest")
    if (not isinstance(manifest, dict) or manifest.get("version") != VERSION
            or manifest.get("members") != _members() or manifest.get("policy") != POLICY
            or manifest.get("selection_rationale_sha256") != RATIONALE_SHA256
            or manifest.get("rationale_is_forward_evidence") is not False
            or _hash(manifest) != raw.get("manifest_sha256")):
        raise ValueError("fixed_study_manifest_mismatch")
    if manifest.get("source_versions") != source_versions():
        raise ValueError("fixed_study_source_changed")
    selected = manifest.get("selected_ms")
    if type(selected) is not int or not 0 < selected <= now:
        raise ValueError("fixed_study_invalid_selection_clock")
    if type(raw.get("round")) is not int or raw["round"] < 0:
        raise ValueError("fixed_study_invalid_round")
    selections = raw.get("selections")
    expected = {c + ":" + a for c, a, _ in MEMBERS}
    if not isinstance(selections, dict) or set(selections) != expected:
        raise ValueError("fixed_study_missing_selection")
    for c, a, _ in MEMBERS:
        row = selections[c + ":" + a]
        initial = _selection(c, a, selected)
        if (not isinstance(row, dict) or any(row.get(k) != initial[k] for k in
                ("selection_ms", "window_start_ms", "selection_id")) or "baseline" not in row):
            raise ValueError("fixed_study_selection_changed")
        baseline = row["baseline"]
        if baseline is not None and (
            not isinstance(baseline, dict)
            or any(not _hex(baseline.get(k)) for k in ("raw_sha256", "normalization_sha256", "source_hash"))
            or not isinstance(baseline.get("money_axis"), str) or len(baseline["money_axis"]) > 64
            or type(baseline.get("captured_ms")) is not int or not selected <= baseline["captured_ms"] <= now
            or type(baseline.get("rows")) is not int or not 0 <= baseline["rows"] <= ab.MAX_WALLET_ROWS
            or type(baseline.get("window_start_ms")) is not int
            or not initial["window_start_ms"] <= baseline["window_start_ms"] <= now
            or type(baseline.get("horizon_ms")) is not int or not 0 <= baseline["horizon_ms"] <= now
        ):
            raise ValueError("fixed_study_invalid_baseline")
    return copy.deepcopy(dict(raw))


def _unknown(raw: Any, dynamic: dict, reason: str) -> dict:
    try:
        retain = raw is not None and isinstance(raw, Mapping) and ab._storage_bytes(raw) <= MAX_STUDY_BYTES
    except (ValueError, TypeError, OverflowError):
        retain = False
    if retain:
        retained = copy.deepcopy(dict(raw))
    else:
        fingerprint = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()
        retained = {"version": VERSION, "status": "unknown", "reason": reason,
                    "prior_state_sha256": fingerprint}
    behavior = {"version": ab.VERSION, "behavior_reports": [], "followthrough_reports": [],
                "receipts": [], "acquisition": [], "provider_calls": 0, "roster_changes": 0,
                "funding_allowed": False, "coverage": {"selected": 0, "attempted": 0, "rows_read": 0,
                    "source_bytes": 0, "partial_watched_history_only": True},
                "fixed_study": {"version": VERSION, "status": "unknown", "reason": reason,
                                "profitability_status": "UNPROVEN", "funding_allowed": False}}
    return {"behavior": behavior, "dynamic_next_state": copy.deepcopy(dynamic), "study_next_state": retained}


class _StickyReader:
    """A transient WAL refusal cannot be forgotten when a subsequent guard clears."""
    def __init__(self, reader):
        self.reader, self.wal_error = reader, None

    def rows(self, sql, args=()):
        if self.wal_error is not None:
            raise self.wal_error
        try:
            return self.reader.rows(sql, args)
        except SourceDeferred as exc:
            if "wal_over_limit:" in str(exc):
                self.wal_error = exc
            raise


def collect_fixed_study(reader: GuardedReader, queue: Sequence[Mapping[str, Any]], *, now_ms: int,
                        dynamic_state: Mapping[str, Any] | None = None,
                        study_state: Mapping[str, Any] | None = None, collector=None) -> dict:
    """Partition one bounded collector call into fixed and original dynamic namespaces.

    Membership/roles and the actual first selection clock are frozen together.
    Historical rationale is never counted as prospective performance. A deferred
    baseline stays unknown; corrupt/source-conflicting state never starts a new epoch.
    Original nomination, intake and acquisition queues are not mutated.
    """
    if type(now_ms) is not int or now_ms <= 0 or len(queue) > 20:
        raise ValueError("positive observation time and at most20 nominees required")
    dynamic = copy.deepcopy(dict(dynamic_state or {}))
    old_selections = dynamic.get("selections", {})
    old_reports = dynamic.get("reports", {})
    if (not isinstance(old_selections, dict) or not isinstance(old_reports, dict)
            or type(dynamic.get("round", 0)) is not int or dynamic.get("round", 0) < 0
            or len(old_selections) > ab.MAX_IDENTITIES
            or ab._selection_bytes(old_selections) > ab.MAX_SELECTION_BYTES
            or ab._storage_bytes(old_reports) > ab.MAX_SUMMARY_BYTES):
        raise ValueError("bounded original dynamic state required")
    try:
        if study_state is None:
            raise ValueError("fixed_study_state_missing")
        state = _study(study_state, now_ms)
        fixed_names = set(state["selections"])
        nominees, seen = [], set()
        for row in queue:
            identity = key(row.get("chain"), row.get("address"))
            if identity is not None and identity not in seen and ":".join(identity) not in fixed_names:
                nominees.append({"chain": identity[0], "address": identity[1]})
                seen.add(identity)
        offset = state["round"] * POLICY["dynamic_reads_per_round"] % len(nominees) if nominees else 0
        admitted = (nominees[offset:] + nominees[:offset])[:POLICY["dynamic_queue_slots"]]
        chosen = admitted[:POLICY["dynamic_reads_per_round"]]
        fixed_queue = [{"chain": c, "address": a} for c, a, _ in MEMBERS]
        actual_queue = fixed_queue + chosen
        expected_dynamic = {**old_selections, **{
            r["chain"] + ":" + r["address"]: old_selections.get(r["chain"] + ":" + r["address"],
                                                              _selection(r["chain"], r["address"], now_ms))
            for r in chosen}}
        if (len(expected_dynamic) + len(fixed_names) > ab.MAX_IDENTITIES
                or ab._selection_bytes(expected_dynamic) + ab._selection_bytes(state["selections"]) > ab.MAX_SELECTION_BYTES):
            raise ValueError("fixed_study_combined_selection_capacity")
        composite = {"version": ab.VERSION, "round": 0,
                     "selections": {**{n: r for n, r in expected_dynamic.items() if n not in fixed_names},
                                    **state["selections"]}, "reports": {}}
        guarded = _StickyReader(reader)
        observed = (collector or ab.collect_behavior)(guarded, actual_queue, now_ms=now_ms, prior_state=composite)
        if guarded.wal_error is not None:
            raise guarded.wal_error
        next_selections = observed["next_state"]["selections"]
        for row in chosen:
            name = row["chain"] + ":" + row["address"]
            expected_dynamic[name] = next_selections[name]
        state["selections"] = {n: next_selections[n] for n in fixed_names}
        state["round"] += 1
        if (ab._selection_bytes(expected_dynamic) + ab._selection_bytes(state["selections"]) > ab.MAX_SELECTION_BYTES
                or ab._storage_bytes(state) > MAX_STUDY_BYTES):
            raise ValueError("fixed_study_combined_selection_byte_cap")
        dynamic["selections"] = expected_dynamic
        dynamic.setdefault("version", ab.VERSION)
        dynamic.setdefault("round", 0)  # The historical dynamic scheduler was not advanced.
        reports, omitted = copy.deepcopy(old_reports), 0
        for name, row in observed["next_state"]["reports"].items():
            if name in fixed_names:
                continue  # Fixed reports are in the latest review, not duplicated in state.
            if ab._storage_bytes({**reports, name: row}) <= ab.MAX_SUMMARY_BYTES:
                reports[name] = row
            else:
                omitted += 1
        dynamic["reports"] = reports
        observed.pop("next_state")
        observed["fixed_study"] = {"version": VERSION, "status": "observing_partial_leader_tape",
            "manifest": state["manifest"], "manifest_sha256": state["manifest_sha256"],
            "round": state["round"], "observation_queue": fixed_queue + admitted,
            "queried_queue": actual_queue, "dynamic_cached_reports_omitted": omitted,
            "combined_selection_count": len(expected_dynamic) + len(fixed_names),
            "combined_selection_storage_bytes": ab._selection_bytes(expected_dynamic) + ab._selection_bytes(state["selections"]),
            "profitability_status": "UNPROVEN", "funding_allowed": False}
        return {"behavior": observed, "dynamic_next_state": dynamic, "study_next_state": state}
    except SourceDeferred:
        raise
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        return _unknown(study_state, copy.deepcopy(dict(dynamic_state or {})), str(exc))
