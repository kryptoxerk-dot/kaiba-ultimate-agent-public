"""Extracted PAPER tick; the integrating worker owns locks, authority and scheduling.

Codex AUTOLEARN-BUDGET, 2026-10-18. This is the transaction body extracted from
the f71b0a04 worker, not a new broker, study or cadence guarantee. The caller must
supply its current locked parent, original bound runner/Context and unchanged
guarded reader. No economic capture, journal ACK, maintenance, transport or lock
acquisition occurs here. Book commits retain the original runner's recovery
semantics; a logical tick is not one atomic transaction across all books.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from kaiba.learning.autolearn_io import SourceDeferred

VERSION = "paper-tick/1"
BASELINE_WORKER_SHA256 = "f71b0a04b19bfb08798d7adde4f068556ecb30c7f0d1d62fc24627821583abce"


@dataclass
class TickState:
    """Exception-visible counterparts of the original closure's mutable values.

    Pass the worker's existing qualification mapping, rather than a copy. Read
    these fields in the integrating caller's finally block even when a callback
    fails: earlier book commits and partial state changes must remain visible.
    """

    frontier_ms: int
    errors: list = field(default_factory=list)
    defer_maintenance: bool = False
    qualified_lessons_by_campaign: dict = field(default_factory=dict)


def run_paper_tick(
    tick_reader: Any, *, state: dict, report: dict, tick: TickState,
    paper: Any, manifests: Sequence[Mapping], allocated: Any,
    groups: Sequence[tuple[str, str]], max_stake_usd: Decimal,
    cycle_started_ms: int, state_path: Path,
    current_ms: Callable[[], int], advance_frontier: Callable[[int], Any], peek_frontier: Callable[[], int],
    phase: Callable, record_phase: Callable,
    read_opportunities: Callable, quote_source: Callable, tax_quote_source: Callable | None,
    storage_bytes: Callable[[], int], assess: Callable, lessons: Callable, atomic_json: Callable,
    monotonic_clock: Callable[[], float] = time.monotonic, checkpoint: bool = False,
) -> dict:
    """Run the original tick with explicit dependencies and shared mutable state.

    ``current_ms`` is the caller's original nondecreasing processing clock.
    ``advance_frontier`` must immediately raise that same caller frontier to the
    supplied value, without sampling a new clock. This preserves broker-frontier
    propagation before later phase/assessment callbacks. Supply the original
    base and tax adapters; the original Context condition selects between them.
    ``peek_frontier`` reads that caller frontier without sampling any clock. It
    preserves phase-finally progress even when the operation in a phase raises.
    No callback may replace the guarded reader or bypass the bound runner.
    """
    try:
        result = _run_paper_tick(tick_reader, state=state, report=report, tick=tick,
            paper=paper, manifests=manifests, allocated=allocated, groups=groups, max_stake_usd=max_stake_usd,
            cycle_started_ms=cycle_started_ms, state_path=state_path, current_ms=current_ms,
            advance_frontier=advance_frontier, phase=phase, record_phase=record_phase,
            read_opportunities=read_opportunities, quote_source=quote_source, tax_quote_source=tax_quote_source,
            storage_bytes=storage_bytes, assess=assess, lessons=lessons, atomic_json=atomic_json,
            monotonic_clock=monotonic_clock, checkpoint=checkpoint)
    finally:
        tick.frontier_ms = max(tick.frontier_ms, peek_frontier())
    result["frontier_ms"] = tick.frontier_ms
    return result


def _run_paper_tick(
    tick_reader: Any, *, state: dict, report: dict, tick: TickState,
    paper: Any, manifests: Sequence[Mapping], allocated: Any,
    groups: Sequence[tuple[str, str]], max_stake_usd: Decimal,
    cycle_started_ms: int, state_path: Path,
    current_ms: Callable[[], int], advance_frontier: Callable[[int], Any],
    phase: Callable, record_phase: Callable,
    read_opportunities: Callable, quote_source: Callable, tax_quote_source: Callable | None,
    storage_bytes: Callable[[], int], assess: Callable, lessons: Callable, atomic_json: Callable,
    monotonic_clock: Callable[[], float], checkpoint: bool,
) -> dict:
    def sampled_ms():
        value = current_ms()
        tick.frontier_ms = max(tick.frontier_ms, value)
        return value

    qualified = tick.qualified_lessons_by_campaign
    # Approvals belong to current owned-book results, never a previous tick's IDs.
    qualified.clear()
    began, cursor = sampled_ms(), int(state.get("decision_cursor", 0))
    campaigns = []
    with phase("pending_expiry"):
        report["local_expiry"] = [paper.expire_pending(m, now_ms=sampled_ms()) for m in manifests]
    if sampled_ms() < int(state.get("retry_after_ms", 0)):
        raise SourceDeferred("source_retry_backoff")
    tick_reader.guard()
    if not state.get("source_initialized"):
        boundary = tick_reader.q("SELECT COALESCE(MAX(rowid),0) FROM decisions WHERE ts_ms<=?",
                                (int(state["selected_ms"]),))
        state["decision_cursor"] = max(int(state.get("decision_cursor", 0)), int(boundary[0][0]))
        state["source_initialized"] = True
    with phase("source_opportunities"):
        source = read_opportunities(tick_reader, after_rowid=int(state.get("decision_cursor", 0)),
            selected_ms=int(state["selected_ms"]), now_ms=sampled_ms(),
            lanes=sorted({g[0] for g in groups}), chains=sorted({g[1] for g in groups}))
    ops = source["opportunities"]
    source_observed_ms = sampled_ms()
    for opportunity in ops:
        opportunity["source_observed_ms"] = source_observed_ms
    report["source_observed_ms"] = source_observed_ms
    storage = storage_bytes()
    if storage >= 256 * 1024 * 1024:
        source["skipped"].extend({"decision_id": o["decision_id"], "reason": "research_storage_budget"} for o in ops)
        ops = []  # Existing pending inventory/exits still get quotes and polling.
    report["research_storage"] = {"bytes": storage, "entry_budget_bytes": 256 * 1024 * 1024}
    queues = []
    with phase("watched_inventory"):
        for manifest in manifests:
            queues.append(list(paper.watched_tokens(manifest, limit=64)))
    watches: list[tuple[str, str]] = []
    # Existing inventory first, fairly between books; new candidates use spare slots.
    while any(queues) and len(watches) < 96:
        for queue in queues:
            if queue:
                token_key = queue.pop(0)
                if token_key not in watches:
                    watches.append(token_key)
    for opportunity in ops:
        token_key = opportunity["chain"], opportunity["token"]
        if token_key not in watches and len(watches) < 96:
            watches.append(token_key)
    quotes = {}
    with phase("captured_quotes"):
        selected_quote = quote_source
        if allocated is not None and getattr(allocated, "tax_quote_upgrade", None) is not None:
            if tax_quote_source is None:
                raise ValueError("paper_tick:required_tax_quote_source_missing")
            selected_quote = tax_quote_source
        for chain, token in watches:
            q = selected_quote(tick_reader, chain, token, now_ms=sampled_ms(), max_age_ms=60_000)
            quotes[f"{chain}:{token}"] = q
    for opportunity in ops:
        opportunity["paper_stake_usd"] = str(max_stake_usd)
        q = quotes.get(f"{opportunity['chain']}:{opportunity['token']}")
        if q and not q.get("unavailable_reason"):
            from kaiba.core.schemas import NATIVE_DECIMALS, Chain
            scale = Decimal(10) ** NATIVE_DECIMALS[Chain(opportunity["chain"])]
            opportunity["size_base_units"] = int(max_stake_usd / Decimal(q["native_usd"]) * scale)
    tick.errors = []
    family_rows = {}
    paper_began, paper_measured = sampled_ms(), monotonic_clock()
    for manifest in manifests:
        result = paper.run_candidates(manifest, ops, quotes, now_ms=sampled_ms())
        tick.frontier_ms = max(tick.frontier_ms, int(result.get("processing_frontier_ms", tick.frontier_ms)))
        advance_frontier(tick.frontier_ms)
        rows = result.get("assessment_rows", result["outcomes"])
        if allocated is None:
            assessment = assess(manifest, rows, now_ms=sampled_ms(),
                                prior_state=state["assessment"].get(manifest["campaign_id"]))
            state["assessment"][manifest["campaign_id"]] = assessment["next_state"]
        else:
            family_rows[manifest["campaign_id"]] = rows
            assessment = {}
        pending = {lesson["evidence_id"]: lesson for lesson in state["pending_lessons"]}
        current_lessons = lessons(manifest, result)
        qualified[manifest["campaign_id"]] = {}
        for lesson in current_lessons:
            previous = pending.get(lesson["evidence_id"])
            if previous is not None and previous != lesson:
                raise ValueError("paper_lesson_identity_conflict")
            qualified[manifest["campaign_id"]][lesson["evidence_id"]] = lesson
            if previous is None:
                state["pending_lessons"].append(lesson)
                pending[lesson["evidence_id"]] = lesson
        campaigns.append({"campaign_id": manifest["campaign_id"], "paper": result,
                          "assessment": {k: v for k, v in assessment.items() if k != "next_state"}})
        tick.errors.extend(result.get("errors", []))
    if allocated is not None:
        family = allocated.assess(family_rows, now_ms=sampled_ms(), prior_state=state.get("allocation_family_assessment"))
        state["allocation_family_assessment"] = family["next_state"]
        for campaign in campaigns:
            cid = campaign["campaign_id"]
            original = family["assessments"][cid]
            state["assessment"][cid] = original["next_state"]
            local = {k: v for k, v in original.items() if k != "next_state"}
            campaign["local_assessment"] = local
            joint = {a["hypothesis_id"]: a for a in family["arms"] if a["campaign_id"] == cid}
            campaign["assessment"] = {**local, "authority": "joint_lifetime_family",
                "arms": [{**a, **joint[a["hypothesis_id"]]} for a in local["arms"]],
                "status": "PAPER_EVIDENCE_READY" if any(a["status"] == "PAPER_EVIDENCE_READY" for a in joint.values()) else "UNPROVEN"}
        report["allocation_assessment"] = {k: v for k, v in family.items() if k not in ("next_state", "assessments")}
    report["campaigns"] = campaigns
    paper_ended = sampled_ms()
    record_phase({"name": "paper_poll", "started_ms": paper_began,
        "completed_ms": paper_ended, "elapsed_ms": paper_ended - paper_began,
        "measured_elapsed_ms": round((monotonic_clock() - paper_measured) * 1000),
        "status": "partial" if tick.errors else "complete"})
    if not tick.errors:
        state["decision_cursor"] = source["next_rowid"]
    report["new_opportunities"] += len(ops)
    report.setdefault("source_skips", []).extend(source["skipped"])
    report.update({"paper_errors": tick.errors,
        "captured_quote_count": sum(not q.get("unavailable_reason") for q in quotes.values()),
        "quote_diagnostics": list(quotes.values()), "candidate_population": "incumbent_decisions_only"})
    pending_entries = any(row.get("status") == "pending"
        for c in report["campaigns"] for row in c["paper"].get("assessment_rows", []))
    if pending_entries:
        state.setdefault("maintenance_deferred_since_ms", sampled_ms())
    else:
        state.pop("maintenance_deferred_since_ms", None)
    tick.defer_maintenance = pending_entries and sampled_ms() - state["maintenance_deferred_since_ms"] < 120_000
    report["maintenance"] = {"pending_entries": pending_entries, "deferred_for_pending": tick.defer_maintenance,
        "deferred_since_ms": state.get("maintenance_deferred_since_ms"), "maximum_deferral_ms": 120_000}
    ended = sampled_ms()
    report["paper_tick_count"] = report.get("paper_tick_count", 0) + 1
    ticks = report.setdefault("paper_ticks", [])
    ticks.append({"index": report["paper_tick_count"], "started_ms": began, "completed_ms": ended,
        "source_observed_ms": source_observed_ms, "source_rows": source["source_rows"],
        "new_opportunities": len(ops), "cursor_before": cursor,
        "cursor_after": int(state.get("decision_cursor", 0)), "status": "partial" if tick.errors else "complete"})
    report["paper_ticks"] = ticks[-32:]
    report["paper_ticks_scope"] = "latest32_receipts; new_opportunities is whole-cycle total"
    state["last_paper_attempt_ms"] = ended
    if not tick.errors:
        state["last_paper_service_ms"] = ended
    if checkpoint:
        pending = {}
        for lesson in state["pending_lessons"]:
            previous = pending.get(lesson["evidence_id"])
            if previous is not None and previous != lesson:
                raise ValueError("pending_lesson_identity_conflict")
            pending.setdefault(lesson["evidence_id"], lesson)
        state["pending_lessons"] = list(pending.values())
        state["last_attempt_ms"] = cycle_started_ms
        atomic_json(state_path, state)
    return {"status": "partial" if tick.errors else "complete", "frontier_ms": tick.frontier_ms,
        "errors": tick.errors, "defer_maintenance": tick.defer_maintenance,
        "qualified_lessons_by_campaign": qualified}
