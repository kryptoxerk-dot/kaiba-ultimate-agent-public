"""Operational projection of cumulative research history; never scoring input.

The worker retains full in-memory records for assessment/lessons and returns them.
Candidate books remain authoritative. Only latest-report.json gets this projection.
No I/O, imports of execution, profitability calculation or promotion side effects.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

VERSION = "research-operational-report/1"
HISTORY_FIELDS = ("outcomes", "assessment_rows", "refusals", "excluded_by_filter", "excluded_by_scope")
EVIDENCE_FLAGS = ("evidence_complete", "cost_complete", "venue_complete", "tax_complete",
                  "commission_complete", "inventory_complete", "live_cost_verified", "no_position_created")
MAX_LABELS = 128


def _labels(rows: list, field: str) -> dict:
    counts: dict[str, int] = {}
    missing = invalid = overflow = 0
    for row in rows:
        if not isinstance(row, Mapping) or field not in row or row[field] is None:
            missing += 1
            continue
        label = row[field]
        if not isinstance(label, str) or not label or len(label) > 160:
            invalid += 1
        elif label in counts:
            counts[label] += 1
        elif len(counts) < MAX_LABELS:
            counts[label] = 1
        else:
            overflow += 1
    return {"counts": counts, "missing_rows": missing, "invalid_rows": invalid,
            "overflow_rows": overflow, "label_limit": MAX_LABELS}


def history_summary(rows: Any) -> dict:
    """Count every retained row. Unknown booleans never become False/complete.

    Different history lists overlap; their counts must not be added as unique trades.
    Bounded reason labels may overflow, but the overflow row count stays explicit.
    """
    if not isinstance(rows, list):
        return {"available": False, "reason": "history_not_a_list", "rows": None}
    flags = {name: {"true": 0, "false": 0, "unknown": 0} for name in EVIDENCE_FLAGS}
    for row in rows:
        for name in EVIDENCE_FLAGS:
            value = row.get(name) if isinstance(row, Mapping) else None
            key = "true" if value is True else "false" if value is False else "unknown"
            flags[name][key] += 1
    return {"available": True, "rows": len(rows),
            "malformed_rows": sum(not isinstance(row, Mapping) for row in rows),
            "status": _labels(rows, "status"), "reason": _labels(rows, "reason"),
            "evidence_flags": flags,
            "count_basis": "rows_in_this_list; overlaps_other_history_lists"}


def compact_report(report: Mapping[str, Any]) -> dict:
    """Remove cumulative detail only from the persisted operational projection.

    Preserve clocks, assessments, authority, diagnostics and unknown fields. This is
    not a generic truncation function. Original rows and source dictionaries are never
    mutated. Calling the function on a previously projected report is an error.
    """
    if not isinstance(report, Mapping) or "operational_projection" in report:
        raise ValueError("raw_research_report_required")
    campaigns = report.get("campaigns", [])
    if not isinstance(campaigns, list) or len(campaigns) > 128:
        raise ValueError("campaign_inventory_invalid")
    projected = []
    omitted = 0
    for campaign in campaigns:
        if not isinstance(campaign, Mapping) or not isinstance(campaign.get("paper"), Mapping):
            raise ValueError("campaign_paper_invalid")
        paper = campaign["paper"]
        view = {key: value for key, value in paper.items() if key not in HISTORY_FIELDS}
        summaries = {}
        for key in HISTORY_FIELDS:
            if key in paper:
                summaries[key] = history_summary(paper[key])
                omitted += len(paper[key]) if isinstance(paper[key], list) else 0
        if "arms" in paper:
            if not isinstance(paper["arms"], list) or len(paper["arms"]) > 128:
                raise ValueError("arm_inventory_invalid")
            arms = []
            for arm in paper["arms"]:
                if not isinstance(arm, Mapping):
                    raise ValueError("paper_arm_invalid")
                item = {key: value for key, value in arm.items() if key != "incomplete"}
                if "incomplete" in arm:
                    item["incomplete_summary"] = history_summary(arm["incomplete"])
                    omitted += len(arm["incomplete"]) if isinstance(arm["incomplete"], list) else 0
                arms.append(item)
            view["arms"] = arms
        view["history_summary"] = summaries
        view["detail_storage"] = {
            "basis": "original_candidate_books_at_report_observation; not_embedded_in_this_view",
            "references": "paper.arms[].book_path; original campaign/hypothesis identifiers preserved",
            "missing_history_is_not_zero": True,
        }
        projected.append({**campaign, "paper": view})
    return {**report, "campaigns": projected, "operational_projection": {
        "version": VERSION, "details_embedded": False, "scoring_input": False,
        "omitted_list_entries": omitted, "omitted_entries_are_not_unique_outcomes": True,
        "original_books_modified": False,
    }}
