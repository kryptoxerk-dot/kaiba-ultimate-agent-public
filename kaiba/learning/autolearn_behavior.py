"""Bounded selected-wallet leader observations; no providers, grades or copy approval.

Codex AGENT-BEHAVIOR-20261015. Selection times and baseline receipts are immutable.
Watched tape is always partial, including when its declared query finishes.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from kaiba.core.schemas import Chain
from kaiba.intelligence import grade
from kaiba.intelligence.wallet_campaign import key
from kaiba.learning.autolearn_io import GuardedReader, SourceDeferred, load_json
from kaiba.learning.wallet_behavior_study import analyze_wallet
from kaiba.learning.wallet_followthrough import analyze_followthrough

VERSION = "autolearn_behavior/1"
DAY_MS = 86_400_000
INITIAL_LOOKBACK_MS = 7 * DAY_MS
MAX_READ_AGE_MS = 13 * DAY_MS
MAX_IDENTITIES = 2_000
MAX_ROUND_IDENTITIES = 10
MAX_WALLET_ROWS = 1_000
MAX_TOTAL_ROWS = 10_000
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_ROW_PAYLOAD_BYTES = 4_096
MAX_SUMMARY_BYTES = 500_000
MAX_SELECTION_BYTES = 700_000
MAX_EXAMPLES = 20
_FIELDS = ("token", "side", "amount_token", "amount_native", "usd_value", "source", "tx")


def _encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(value: Any) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _storage_bytes(value: Any) -> int:
    raw = json.dumps(value, indent=2, sort_keys=True, allow_nan=False).encode()
    # Reserve indentation when embedded in the calling worker's pretty JSON state.
    return len(raw) + 16 * raw.count(b"\n")


def _selection_bytes(selections: dict) -> int:
    reserved = {}
    for name, row in selections.items():
        baseline = row.get("baseline") or {
            "raw_sha256": "f" * 64, "money_axis": "usd_micro", "normalization_sha256": "f" * 64,
            "window_start_ms": row["window_start_ms"], "source_hash": "f" * 64,
            "horizon_ms": row["selection_ms"], "captured_ms": row["selection_ms"], "rows": 1000}
        reserved[name] = {**row, "baseline": baseline}
    return _storage_bytes(reserved)


def source_versions() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in (
        "learning/autolearn_behavior.py", "learning/autolearn_io.py", "intelligence/grade.py",
        "intelligence/pnl.py", "learning/wallet_behavior_study.py", "learning/wallet_followthrough.py")}


def _horizon(reader: GuardedReader) -> int:
    rows = reader.rows("SELECT value FROM kv WHERE key='retention:swaps:horizon_ms'")
    raw = load_json(rows[0]["value"], rows[0]["value"]) if rows else 0
    if isinstance(raw, bool) or not str(raw).strip().isdigit():
        raise ValueError("retention_watermark_unreadable")
    return int(raw)


def _unknown(chain: str, wallet: str, now: int, reason: str) -> dict:
    return {"chain": chain, "wallet": wallet, "as_of_ms": now,
            "profitability_status": "UNPROVEN", "live_copy_approved": False, "episodes": [],
            "coverage": {"partial_history": True, "query_status": reason,
                         "fees_coverage": "unknown", "unobserved_transfers_coverage": "unknown"}}


def _compact(report: dict) -> dict:
    # Keep complete aggregate counts, hashes and explicit omissions, never raw trades.
    episodes = report.get("episodes", [])
    result = {k: v for k, v in report.items() if k not in ("episodes", "behavior", "limitations")}
    result["behavior"] = {k: v for k, v in report.get("behavior", {}).items()
                          if k != "observed_token_first_buy_first_sell_timings"}
    for field in ("baseline", "post_selection_new_entries", "preexisting_inventory_followthrough"):
        if field in result:
            result[field] = {k: v for k, v in result[field].items()
                             if k not in ("episode_ids", "gross_native_atomic")}
    if "accounting_breakdown" in result:
        result["accounting_breakdown"] = {name: {k: v for k, v in period.items()
                                                 if k in ("counts", "gross_money_axis", "gross_usd_known_subset")}
                                          for name, period in result["accounting_breakdown"].items()}
    fields = ("episode_id", "token", "opened_ms", "closed_ms", "state", "profit_eligible",
              "contaminated", "contamination", "strong_identity_conflict", "selection_category",
              "buys", "sells", "leftover_qty_units", "first_sell_hold_s", "full_close_hold_s",
              "gross_closed_profit_money_units", "same_timestamp_token_profit_unevaluated")
    result["episodes"] = [{k: e[k] for k in fields if k in e} for e in episodes[-MAX_EXAMPLES:]]
    result["episode_examples"] = {"total": len(episodes), "included": len(result["episodes"]),
                                  "omitted": max(0, len(episodes) - MAX_EXAMPLES),
                                  "full_analysis_sha256": _hash(report), "selection": "last_observed_episodes"}
    result["limitations"] = ["Partial watched-token leader tape; opening inventory and unseen transfers unknown.",
                             "Gross leader cashflows omit fees, failures and achievable follower execution."]
    return result


def collect_behavior(reader: GuardedReader, queue: Sequence[Mapping[str, Any]], *, now_ms: int,
                     prior_state: Mapping[str, Any] | None = None) -> dict:
    """Observe at most ten of twenty already selected identities; never select/intake.

    Initial seven-day windows remain fixed while the 13-day/retention ceiling allows.
    A cap, source/baseline change or missing query never becomes complete evidence.
    """
    if type(now_ms) is not int or now_ms <= 0 or len(queue) > 20:
        raise ValueError("positive observation time and at most20 selected identities required")
    prior = dict(prior_state or {})
    selections = json.loads(json.dumps(prior.get("selections", {})))
    if (not isinstance(selections, dict) or len(selections) > MAX_IDENTITIES
            or _selection_bytes(selections) > MAX_SELECTION_BYTES):
        raise ValueError("bounded immutable selection state required")
    versions = source_versions()
    source_hash = _hash(versions)
    identities = []
    for row in queue:
        identity = key(row.get("chain"), row.get("address"))
        if identity is not None and identity not in identities:
            identities.append(identity)
    for chain, wallet in identities:
        name = chain + ":" + wallet
        if name not in selections and len(selections) < MAX_IDENTITIES:
            new = {"selection_ms": now_ms, "window_start_ms": max(0, now_ms - INITIAL_LOOKBACK_MS),
                   "selection_id": _hash([VERSION, chain, wallet, now_ms]), "baseline": None}
            if _selection_bytes({**selections, name: new}) <= MAX_SELECTION_BYTES:
                selections[name] = new
    round_no = max(0, int(prior.get("round", 0)))
    offset = round_no * MAX_ROUND_IDENTITIES % len(identities) if identities else 0
    chosen = (identities[offset:] + identities[:offset])[:MAX_ROUND_IDENTITIES]
    reports = {name: row for name, row in prior.get("reports", {}).items()
               if name in {c + ":" + w for c, w in identities} and row.get("source_hash") == source_hash}
    acquisition, total_rows, total_bytes = [], 0, 0
    try:
        horizon = _horizon(reader) if identities else 0
        horizon_error = None
    except (SourceDeferred, ValueError) as exc:
        horizon, horizon_error = None, str(exc)
    for chain, wallet in identities:
        name = chain + ":" + wallet
        selected = selections.get(name)
        diagnostic = {"chain": chain, "address": wallet, "provider_calls": 0, "roster_changes": 0,
                      "scheduled_history_route": "existing_SOL_wallet_tracked_quota" if chain == "sol"
                      else "no_scheduled_wallet_history_route_for_research_import",
                      "scheduled_history_eligibility": "cohort_and_quota_not_verified_here" if chain == "sol" else "unavailable",
                      "passive_tape_status": "unknown_not_read_this_round"}
        acquisition.append(diagnostic)
        if (chain, wallet) not in chosen:
            continue
        reason = horizon_error or ("immutable_selection_capacity" if selected is None else None)
        receipt = {"query_status": "unknown", "source_hash": source_hash, "horizon_ms": horizon,
                   "as_of_ms": now_ms, "partial_history": True, "complete_wallet_history": False}
        if selected is not None:
            receipt["selection_ms"] = selected["selection_ms"]
        if not reason:
            start = max(selected["window_start_ms"], now_ms - MAX_READ_AGE_MS, horizon)
            receipt["window_start_ms"] = start
            try:
                if selected["selection_ms"] > now_ms or start > now_ms:
                    raise ValueError("selection_or_retention_in_future")
                requested = min(MAX_WALLET_ROWS + 1, MAX_TOTAL_ROWS - total_rows,
                                (MAX_TOTAL_BYTES - total_bytes) // MAX_ROW_PAYLOAD_BYTES)
                if requested <= 0:
                    raise ValueError("round_source_budget_exhausted")
                projected = ",".join(f"CASE WHEN length(CAST({f} AS BLOB))<=512 THEN {f} ELSE NULL END AS {f}"
                                     for f in _FIELDS)
                oversized = " OR ".join(f"length(CAST({f} AS BLOB))>512" for f in _FIELDS)
                rows = reader.rows(f"SELECT id,ts_ms,{projected},({oversized}) AS oversized_fields "
                                   "FROM swaps INDEXED BY idx_swaps_wallet WHERE chain=? AND wallet=? "
                                   "AND ts_ms>=? AND ts_ms<=? ORDER BY ts_ms,id LIMIT ?",
                                   (chain, wallet, start, now_ms, requested))
                payload_bytes = sum(sum(len(str(v).encode()) if v is not None else 4 for v in r.values())
                                    for r in rows)
                total_rows += len(rows)
                total_bytes += payload_bytes
                receipt.update(rows_observed=len(rows), payload_bytes=payload_bytes)
                if len(rows) > MAX_WALLET_ROWS or (requested <= MAX_WALLET_ROWS and len(rows) == requested):
                    raise ValueError("row_cap_truncated")
                if total_bytes > MAX_TOTAL_BYTES:
                    raise ValueError("source_payload_byte_cap")
                if any(r.get("oversized_fields") for r in rows):
                    raise ValueError("oversized_source_field")
                raw = [{**r, "chain": chain, "wallet": wallet} for r in rows]
                normalized = grade.normalise_tape_rows(raw, Chain(chain))
                coverage = normalized.coverage.model_dump(mode="json")
                receipt.update(query_status="declared_query_complete", raw_sha256=_hash(raw),
                               normalization=coverage, normalization_sha256=_hash(normalized.rows))
                baseline_rows = [r for r in raw if r["ts_ms"] <= selected["selection_ms"]]
                baseline_tape = grade.normalise_tape_rows(baseline_rows, Chain(chain))
                observed_baseline = {"raw_sha256": _hash(baseline_rows), "money_axis": baseline_tape.coverage.money_axis,
                                     "normalization_sha256": _hash(baseline_tape.rows), "window_start_ms": start,
                                     "source_hash": source_hash, "horizon_ms": horizon,
                                     "captured_ms": now_ms, "rows": len(baseline_rows)}
                if selected["baseline"] is None:
                    selected["baseline"] = observed_baseline
                baseline = selected["baseline"]
                comparable = all(baseline[k] == observed_baseline[k] for k in
                                 ("raw_sha256", "normalization_sha256", "money_axis", "window_start_ms", "source_hash"))
                comparable = comparable and baseline["captured_ms"] == selected["selection_ms"]
                comparable = comparable and baseline["money_axis"] == coverage["money_axis"]
                metadata = {"money_axis": coverage["money_axis"], "receipt": receipt,
                            "left_censored": True, "selection_id": selected["selection_id"],
                            "baseline": {**baseline, "query_missing": not comparable},
                            "post_selection": {"window_start_ms": start, "as_of_ms": now_ms}}
                behavior = _compact(analyze_wallet(normalized.rows, chain=chain, wallet=wallet,
                                                  as_of_ms=now_ms, metadata=metadata))
                follow = _compact(analyze_followthrough(normalized.rows, chain=chain, wallet=wallet,
                                                       selection_ms=selected["selection_ms"], as_of_ms=now_ms,
                                                       metadata=metadata))
                follow["baseline_comparison_status"] = "OBSERVED_GROSS_PARTIAL_ONLY" if comparable else "UNPROVEN_BASELINE_CHANGED_OR_MISSING"
                diagnostic["passive_tape_status"] = "observed_partial_rows" if rows else "no_rows_in_declared_window"
                reports[name] = {"source_hash": source_hash, "behavior": behavior, "followthrough": follow,
                                 "receipt": receipt}
            except (SourceDeferred, ValueError, TypeError, OverflowError) as exc:
                reason = str(exc)
        if reason:
            receipt["query_status"] = reason
            diagnostic["passive_tape_status"] = "unknown:" + reason
            reports[name] = {"source_hash": source_hash, "behavior": _unknown(chain, wallet, now_ms, reason),
                             "followthrough": {"chain": chain, "wallet": wallet, "as_of_ms": now_ms,
                                               "profitability_status": "UNPROVEN", "query_status": reason},
                             "receipt": receipt}
    # Prefer fresh queried reports over cached reports when the pretty-JSON budget fills.
    cached_omitted = 0
    for name in list(reports):
        if _storage_bytes(reports) <= MAX_SUMMARY_BYTES:
            break
        if tuple(name.split(":", 1)) not in chosen:
            reports.pop(name)
            cached_omitted += 1
    # Bound output even for unusual huge integer aggregates or nested episode flags.
    if _storage_bytes(reports) > MAX_SUMMARY_BYTES:
        for row in reports.values():
            for field in ("behavior", "followthrough"):
                report = row[field]
                if "episode_examples" in report:
                    report["episodes"] = []
                    report["episode_examples"]["included"] = 0
                    report["episode_examples"]["omitted"] = report["episode_examples"]["total"]
    if _storage_bytes(reports) > MAX_SUMMARY_BYTES:
        reports = {name: {"source_hash": source_hash, "behavior": _unknown(
            *name.split(":", 1), now_ms, "summary_byte_cap"), "followthrough": {},
            "receipt": {"query_status": "summary_byte_cap"}} for name in reports}
    return {"version": VERSION, "source_versions": versions,
            "behavior_reports": [r["behavior"] for r in reports.values()],
            "followthrough_reports": [r["followthrough"] for r in reports.values()],
            "receipts": [r["receipt"] for r in reports.values()], "acquisition": acquisition,
            "coverage": {"selected": len(identities), "attempted": len(chosen), "rows_read": total_rows,
                         "source_bytes": total_bytes, "summary_bytes": _storage_bytes(reports),
                         "selection_storage_bytes": _selection_bytes(selections),
                         "cached_reports_omitted_for_budget": cached_omitted,
                         "partial_watched_history_only": True},
            "next_state": {"version": VERSION, "round": round_no + 1,
                           "selections": selections, "reports": reports},
            "provider_calls": 0, "roster_changes": 0, "funding_allowed": False}
