"""Consume runner nominations as research evidence, never as trading authority.

Codex AGENT-UPGRADE-20261015: additive pure adapter. ``review_candidates`` accepts
bounded packets extracted by the caller; no files, database, providers or rosters
are accessed here. Existing identity, adverse-tag and operator-label rules are
reused. No scores, profit thresholds, promotions or provider budgets are created.

Packet lists:
* exports: {chain, source, path, sha256, observed_ms, rows:[actual import rows]}
* profiles: {chain,address,source:'p30'|'ht_stats',data:actual cache row}. ``_at``
  is seconds; an explicit wrapper ``at_ms`` is allowed only when _at is absent.
* history: current/historical wallet/score rows, including tags_json/cohort/source.
* entities: {chain,address,entity_id,status,basis}; supplied identities are retained.
* exclusions: {chain,address,reason}; omitted chain means all compatible chains.
* behavior_reports: existing wallet_behavior_study/wallet_followthrough reports.
* copy_evidence: existing copy/proven/forward receipts, retained as research only.
* controls: explicit additional research nominees (not invented matched controls).
* prior_state: last returned next_state; coverage: caller extraction limitations.

Queue budgets are *proposed observation slots*, not provider credits or performed
calls. Existing gather_credit_room/backfill budgets must authorize collection.
Positive vendor PnL, gross leader episodes and the proven.py print model cannot
establish economically complete follower profit. COPY_UNPROVEN is deliberate.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict, deque
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core.schemas import HARD_QUARANTINE_TAGS
from kaiba.intelligence import grade
from kaiba.intelligence.import_wallets import parse_label
from kaiba.intelligence.wallet_campaign import MAX_EVIDENCE_AGE_MS, key
from kaiba.intelligence.wallet_campaign import RESEARCH_CHAINS as CHAINS

VERSION = "wallet-candidates/1"
MAX_INPUT_ROWS = 20_000
MAX_QUEUE = 200
# Exact adverse vocabulary declared by the lead's runner_hunt/toptier exporters.
# These are research/copy-source exclusions; this module never changes a live gate.
RUNNER_EXCLUDED_TAGS = frozenset({"dev", "creator", "dev_team", "bundler", "rat_trader", "dex_bot", "sandwich_bot"})
_PROFILE_SOURCES = frozenset({"p30", "ht_stats"})


def _mapping(value: Any) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[dict]:
    return [_mapping(row) for row in value] if isinstance(value, (list, tuple)) else []


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value))
        return int(n) if n.is_finite() and n == n.to_integral_value() else None
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return None


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value))
        return n if n.is_finite() else None
    except (ValueError, TypeError, InvalidOperation):
        return None


def _json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if _number(value) is not None else None
    return str(value)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(_json(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _identity(row: dict) -> tuple[str, str] | None:
    return key(row.get("chain"), row.get("address", row.get("wallet")))


def _fresh(stamp: Any, now: int) -> bool:
    at = _integer(stamp)
    return at is not None and at > 0 and 0 <= now - at <= MAX_EVIDENCE_AGE_MS


def _tags(row: dict) -> tuple[list[str], bool]:
    raw = row.get("tags", row.get("tags_json", []))
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return [], False
    if not isinstance(raw, (list, tuple)):
        return [], False
    return [str(item) for item in raw], True


def _adverse_flags(row: dict) -> set[str]:
    reasons = set()
    if any(row.get(field) is True or type(row.get(field)) is int and row[field] == 1
           for field in ("is_suspicious", "susp")):
        reasons.add("runner_suspicious_flag")
    transferred = _number(row.get("history_transfer_in_amount"))
    if row.get("tin") is True or transferred is not None and transferred > 0:
        reasons.add("runner_transfer_in_flag")
    return reasons


def _profile(row: dict, now: int) -> dict:
    data = _mapping(row.get("data"))
    seconds = _integer(data.get("_at"))
    stamp = seconds * 1000 if "_at" in data and seconds is not None else (
        None if "_at" in data else _integer(row.get("at_ms")))
    source = row.get("source")
    profit = _number(data.get("rp") if source == "ht_stats" else data.get("realized_profit"))
    buys = _integer(data.get("buy", data.get("b30")))
    reasons = []
    if source not in _PROFILE_SOURCES:
        reasons.append("unsupported_provider_profile_source")
    if data.get("wallet_address") is not None and _identity({"chain": row.get("chain"), "address": data["wallet_address"]}) != _identity(row):
        reasons.append("provider_wallet_identity_mismatch")
    if data.get("chain") is not None and data["chain"] != row.get("chain"):
        reasons.append("provider_chain_identity_mismatch")
    if data.get("missing"):
        reasons.append("provider_reply_missing")
    if not _fresh(stamp, now):
        reasons.append("provider_observation_stale_missing_or_future")
    if profit is None:
        reasons.append("provider_profit_unknown")
    if buys is None or buys < 0:
        reasons.append("provider_activity_unknown")
    if buys == 0:
        reasons.append("provider_no_recent_buys")
    return {"source": source, "at_ms": stamp, "basis": "provider_reported_30d",
            "profit_usd_claim": str(profit) if profit is not None else None,
            "buy_count_claim": buys, "return_claim": _json(data.get("ret")),
            "win_rate_claim": _json(data.get("win", data.get("wr"))),
            "holding_s_claim": _json(data.get("hold")), "tokens_claim": _json(data.get("tokens")),
            "creator_count_claim": _json(data.get("created")),
            "usable": not reasons, "reasons": reasons, "row_hash": _hash(row)}


def _behavior(report: dict, identity: tuple[str, str], now: int) -> dict:
    stamp = _integer(report.get("as_of_ms"))
    result = {"as_of_ms": stamp, "fresh": _fresh(stamp, now), "inspectable_episodes": 0,
              "copy_approved": False, "basis": "existing_inventory_aware_leader_analyzer",
              "coverage": _json(report.get("coverage", {})), "report_hash": _hash(report)}
    if _identity(report) != identity or not result["fresh"]:
        return result
    for episode in _rows(report.get("episodes")):
        closed = _integer(episode.get("closed_ms"))
        opened = _integer(episode.get("opened_ms"))
        if (episode.get("state") == "closed" and episode.get("profit_eligible") is True
                and not episode.get("contaminated") and not episode.get("strong_identity_conflict")
                and opened is not None and closed is not None and 0 < opened <= closed <= stamp
                and now - closed <= MAX_EVIDENCE_AGE_MS):
            result["inspectable_episodes"] += 1
    # Teaching an inspectable losing episode is useful too. This is not a profit grade.
    result["median_first_sell_hold_s"] = _json(_mapping(report.get("behavior")).get("median_first_sell_hold_s"))
    return result


def _fair_pick(pool: list[dict], count: int, used: set[tuple], entities: Counter, *,
               max_per_entity: int, round_no: int, counts: dict) -> list[dict]:
    queues = defaultdict(deque)
    for row in sorted(pool, key=lambda row: (counts.get(row["candidate_key"], 0),
                                             _hash([VERSION, row["candidate_key"], round_no]))):
        queues[row["chain"]].append(row)
    chains = sorted(queues)
    if chains:
        offset = round_no % len(chains)
        chains = chains[offset:] + chains[:offset]
    chosen = []
    while len(chosen) < count and any(queues.values()):
        for chain in chains:
            while queues[chain]:
                row = queues[chain].popleft()
                identity = row["chain"], row["address"]
                if identity in used or entities[row["entity_key"]] >= max_per_entity:
                    continue
                used.add(identity)
                entities[row["entity_key"]] += 1
                chosen.append(row)
                break
            if len(chosen) >= count:
                break
    return chosen


def review_candidates(packet: Mapping[str, Any], *, now_ms: int, budget: int = 20,
                      exploration_slots: int = 5, max_per_entity: int = 1) -> dict[str, Any]:
    """Normalize bounded nominations, explain exclusions, and propose fair observations."""
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms <= 0:
        raise ValueError("review time must be a positive integer in milliseconds")
    if isinstance(budget, bool) or not isinstance(budget, int) or not 0 <= budget <= MAX_QUEUE:
        raise ValueError("observation-slot budget must be an integer between 0 and 200")
    if (isinstance(exploration_slots, bool) or not isinstance(exploration_slots, int) or exploration_slots < 0
            or isinstance(max_per_entity, bool) or not isinstance(max_per_entity, int) or max_per_entity < 1):
        raise ValueError("invalid exploration or entity capacity")
    packet = _mapping(packet)
    for field in ("exports", "profiles", "history", "entities", "exclusions", "behavior_reports", "copy_evidence", "controls"):
        if field in packet and not isinstance(packet[field], (list, tuple)):
            raise ValueError(f"{field} must be a bounded list, never an implicit empty answer")
    lists = {field: _rows(packet.get(field)) for field in (
        "exports", "profiles", "history", "entities", "exclusions", "behavior_reports", "copy_evidence", "controls")}
    input_rows = (sum(len(rows) for rows in lists.values())
                  + sum(len(_rows(row.get("rows"))) for row in lists["exports"])
                  + sum(len(_rows(row.get("episodes"))) for row in lists["behavior_reports"]))
    if input_rows > MAX_INPUT_ROWS:
        raise ValueError("wallet evidence packet exceeds 20000 rows; extract a declared bounded subset")
    candidates: dict[tuple, dict] = {}
    invalid = []
    duplicate_rows = 0
    def ensure(row, nomination=None):
        nonlocal duplicate_rows
        identity = _identity(row)
        if identity is None:
            invalid.append({"chain": row.get("chain"), "address": row.get("address", row.get("wallet")),
                            "reason": "invalid_or_unsupported_chain_address"})
            return None
        if identity in candidates and nomination is not None:
            duplicate_rows += 1
        result = candidates.setdefault(identity, {"chain": identity[0], "address": identity[1],
            "candidate_key": identity[0] + ":" + identity[1], "nominations": [], "labels": [],
            "raw_tags": set(), "exclusions": set(), "warnings": set(), "registry_present": False,
            "profiles": [], "behavior_evidence": [], "copy_evidence": [], "roles": [],
            "entity_key": "solo:" + identity[0] + ":" + identity[1], "entity_basis": "no_observed_linkage",
            "independence_proven": False, "declared_control": False})
        if nomination is not None:
            result["nominations"].append(nomination)
        return result

    for export in lists["exports"]:
        receipt = {field: export.get(field) for field in ("source", "path", "sha256", "observed_ms")}
        receipt["fresh"] = _fresh(receipt["observed_ms"], now_ms)
        receipt["provenance_complete"] = bool(receipt["source"] and receipt["path"] and
                                             isinstance(receipt["sha256"], str) and len(receipt["sha256"]) == 64)
        for raw in _rows(export.get("rows")):
            row = {**raw, "chain": export.get("chain")}
            candidate = ensure(row, receipt)
            if candidate is None:
                continue
            if raw.get("chain") is not None and raw["chain"] != row["chain"]:
                candidate["exclusions"].add("export_row_chain_conflict")
            label = str(raw.get("name") or "")[:280]
            if label:
                candidate["labels"].append(label)
                if parse_label(label).negative:
                    candidate["exclusions"].add("negative_operator_label")
            tags, valid = _tags(raw)
            candidate["raw_tags"].update(tags)
            candidate["exclusions"].update(_adverse_flags(raw))
            if not valid:
                candidate["exclusions"].add("invalid_risk_tags")
            groups = raw.get("groups")
            groups = groups if isinstance(groups, (list, tuple)) else []
            if any("sniper" in group.lower() for group in groups if isinstance(group, str)):
                candidate["warnings"].add("sniper_nomination_requires_reachability_test")
            if not receipt["fresh"] or not receipt["provenance_complete"]:
                candidate["warnings"].add("export_stale_or_provenance_incomplete")
    for row in lists["controls"]:
        candidate = ensure(row, {"source": row.get("source", "declared_research_control"),
                                  "observed_ms": row.get("observed_ms"), "basis": "caller_declared_control"})
        if candidate is not None:
            candidate["declared_control"] = True
    for row in lists["history"]:
        candidate = ensure(row)
        if candidate is None:
            continue
        candidate["registry_present"] = True
        tags, valid = _tags(row)
        candidate["raw_tags"].update(tags)
        candidate["exclusions"].update(_adverse_flags(row))
        if not valid:
            candidate["exclusions"].add("invalid_historical_risk_tags")
        if row.get("cohort") in {"blacklist", "trusted_copy"}:
            candidate["exclusions"].add("existing_" + row["cohort"] + "_preserved")
        if row.get("is_owner") is True or str(row.get("source", "")).startswith(("owner", "agent")):
            candidate["exclusions"].add("owner_or_agent_wallet")
        if row.get("hub_address") or row.get("cluster_status") in {"hub", "quarantined"}:
            candidate["exclusions"].add("hub_or_cluster_quarantine")
        candidate["warnings"].add("stored_grade_is_not_copy_profit_evidence")
    for row in lists["profiles"]:
        candidate = ensure(row)
        if candidate is not None:
            candidate["profiles"].append(_profile(row, now_ms))
            tags, _ = _tags(_mapping(row.get("data")))
            candidate["raw_tags"].update(tags)
            candidate["exclusions"].update(_adverse_flags(_mapping(row.get("data"))))
            if (_number(_mapping(row.get("data")).get("created")) or 0) > 0:
                candidate["exclusions"].add("runner_creator_profile_exclusion")
    for row in lists["entities"]:
        candidate = ensure(row)
        if candidate is None:
            continue
        if row.get("status") in {"hub", "quarantined"}:
            candidate["exclusions"].add("hub_or_cluster_quarantine")
        entity = row.get("entity_id")
        if isinstance(entity, str) and entity:
            entity_key = "entity:" + entity
            if candidate["entity_basis"] != "no_observed_linkage" and candidate["entity_key"] != entity_key:
                candidate["exclusions"].add("conflicting_entity_evidence")
            candidate["entity_key"] = entity_key
            candidate["entity_basis"] = row.get("basis", "supplied_existing_entity_graph")
    for row in lists["behavior_reports"]:
        candidate = ensure(row)
        if candidate is not None:
            candidate["behavior_evidence"].append(_behavior(row, (candidate["chain"], candidate["address"]), now_ms))
    for row in lists["copy_evidence"]:
        candidate = ensure(row)
        if candidate is not None:
            candidate["copy_evidence"].append({"source": row.get("source", row.get("model_version")),
                "status_claim": row.get("status", row.get("profitability_status")),
                "observed_ms": row.get("as_of_ms", row.get("frozen_ms")), "evidence_hash": _hash(row),
                "basis": "existing_research_receipt_not_economic_copy_approval"})
    exclusions_by_identity = defaultdict(set)
    for row in lists["exclusions"]:
        chains = CHAINS if row.get("chain") in (None, "*") else [row.get("chain")]
        for chain in chains:
            identity = key(chain, row.get("address"))
            if identity is not None:
                exclusions_by_identity[identity].add("supplied_exclusion:" + str(row.get("reason", "unspecified")))
    for identity, candidate in candidates.items():
        candidate["exclusions"].update(exclusions_by_identity[identity])

    for candidate in candidates.values():
        raw_tags = sorted(candidate["raw_tags"])
        for tag in grade._tags_from(raw_tags):
            if tag in HARD_QUARANTINE_TAGS:
                candidate["exclusions"].add("existing_hard_quarantine_tag:" + tag.value)
        for raw_tag in raw_tags:
            tag = raw_tag.removeprefix(grade.VENDOR_TAG_PREFIX)
            if tag in RUNNER_EXCLUDED_TAGS:
                candidate["exclusions"].add("runner_source_adverse_tag:" + raw_tag)
            if tag == "sniper":
                candidate["warnings"].add("sniper_nomination_requires_reachability_test")
        profiles = candidate["profiles"]
        latest = {}
        for profile in profiles:
            source = profile["source"]
            previous = latest.get(source)
            stamp = profile["at_ms"] or -1
            if previous and stamp == (previous["at_ms"] or -1) and profile["row_hash"] != previous["row_hash"]:
                candidate["warnings"].add("conflicting_provider_profile_same_timestamp")
            if previous is None or stamp > (previous["at_ms"] or -1):
                latest[source] = profile
        candidate["profiles"] = [latest[source] for source in sorted(latest, key=str)]
        usable = [profile for profile in candidate["profiles"] if profile["usable"]]
        positive = [profile for profile in usable if Decimal(profile["profit_usd_claim"]) > 0]
        negative = [profile for profile in usable if Decimal(profile["profit_usd_claim"]) < 0]
        if positive and not negative and "conflicting_provider_profile_same_timestamp" not in candidate["warnings"]:
            candidate["roles"].append("PROVIDER_POSITIVE")
        if positive and negative:
            candidate["warnings"].add("fresh_provider_windows_disagree")
        if any(evidence["inspectable_episodes"] for evidence in candidate["behavior_evidence"]):
            candidate["roles"].append("BEHAVIOR_TEACHER")
        candidate["copy_status"] = "COPY_UNPROVEN"
        candidate["gates"] = {
            "authenticity": "EXCLUDED" if candidate["exclusions"] else "UNPROVEN",
            "recency": "OBSERVED_RECENT_PROVIDER_ARITHMETIC" if usable else "UNPROVEN",
            "reachability": "SNIPER_REQUIRES_DELAYED_COPY_TEST"
            if "sniper_nomination_requires_reachability_test" in candidate["warnings"] else "UNPROVEN",
            "survivability": "EXCLUDED" if candidate["exclusions"] else "UNPROVEN",
        }
        candidate["copy_blockers"] = ["no_existing_economically_complete_prospective_wallet_passline",
                                      "follower_fees_failed_attempts_inventory_and_reachability_not_proven"]
        if "sniper_nomination_requires_reachability_test" in candidate["warnings"]:
            candidate["copy_blockers"].append("sniper_copy_reachability_unproven")
        if candidate["exclusions"]:
            candidate["copy_blockers"].extend(sorted(candidate["exclusions"]))
        candidate["required_evidence"] = (["refresh_provider_arithmetic"] if not usable else []) + [
            "inventory_aware_leader_behavior", "prospective_delayed_follower_economics_with_controls"]
        candidate["observation_eligible"] = not candidate["exclusions"] and bool(candidate["nominations"])
        candidate["raw_tags"] = raw_tags
        candidate["exclusions"] = sorted(candidate["exclusions"])
        candidate["warnings"] = sorted(candidate["warnings"])
        candidate["labels"] = sorted(set(candidate["labels"]))
        candidate["nominations"] = sorted({_hash(row): row for row in candidate["nominations"]}.values(), key=_hash)

    prior = _mapping(packet.get("prior_state"))
    prior_counts = _mapping(prior.get("selection_counts")) if prior.get("version") == VERSION else {}
    counts = {candidate["candidate_key"]: max(0, _integer(prior_counts.get(candidate["candidate_key"])) or 0)
              for candidate in candidates.values()}
    round_no = max(0, _integer(prior.get("round")) or 0) if prior.get("version") == VERSION else 0
    eligible = [row for row in candidates.values() if row["observation_eligible"]]
    exploration = [row for row in eligible if row["declared_control"] or not row["roles"]]
    used, entities = set(), Counter()
    reserved = _fair_pick(exploration, min(budget, exploration_slots), used, entities,
                          max_per_entity=max_per_entity, round_no=round_no, counts=counts)
    focused = [row for row in eligible if row["roles"] and not row["declared_control"]]
    remaining = _fair_pick(focused, budget - len(reserved), used, entities,
                           max_per_entity=max_per_entity, round_no=round_no, counts=counts)
    remaining += _fair_pick(eligible, budget - len(reserved) - len(remaining), used, entities,
                            max_per_entity=max_per_entity, round_no=round_no, counts=counts)
    queue = []
    for candidate in [*reserved, *remaining]:
        counts[candidate["candidate_key"]] += 1
        queue.append({"chain": candidate["chain"], "address": candidate["address"],
            "entity_key": candidate["entity_key"], "roles": candidate["roles"], "copy_status": "COPY_UNPROVEN",
            "selection_arm": "exploration" if candidate["declared_control"] or not candidate["roles"] else "evidence_followup",
            "route": "existing_budgeted_history_observation", "required_evidence": candidate["required_evidence"],
            "evidence_id": "wallet-observe:" + _hash([VERSION, now_ms, candidate["candidate_key"], candidate["nominations"]]),
            "provider_calls_authorized": 0, "roster_change_authorized": False})
    selected_keys = {(row["chain"], row["address"]) for row in queue}
    intake = [{"chain": row["chain"], "address": row["address"], "name": "",
               "source": "runner_observation_nomination", "cohort": "research"}
              for identity, row in candidates.items() if identity in selected_keys and not row["registry_present"]]
    rows = sorted(candidates.values(), key=lambda row: (row["chain"], row["address"]))
    return _json({"version": VERSION, "observed_ms": now_ms, "packet_hash": _hash(packet),
        "funding_allowed": False, "live_copy_approved": False, "copy_qualified_count": 0,
        "candidates": rows, "invalid_rows": invalid, "observation_queue": queue, "intake_records": intake,
        "coverage": {"input_rows": input_rows, "unique_chain_addresses": len(rows),
                     "duplicate_nomination_rows": duplicate_rows, "invalid_identity_rows": len(invalid),
                     "excluded_count": sum(bool(row["exclusions"]) for row in rows),
                     "provider_positive_count": sum("PROVIDER_POSITIVE" in row["roles"] for row in rows),
                     "behavior_teacher_count": sum("BEHAVIOR_TEACHER" in row["roles"] for row in rows),
                     "by_chain": dict(Counter(row["chain"] for row in rows)),
                     "exclusions_by_reason": dict(Counter(reason for row in rows for reason in row["exclusions"])),
                     "source_coverage": packet.get("coverage", {}),
                     "absence_of_entity_link_is_not_independence": True},
        "queue_policy": {"budget": budget, "unit": "proposed_observation_slots", "queued": len(queue),
                         "exploration_slots_requested": exploration_slots,
                         "exploration_queued": sum(row["selection_arm"] == "exploration" for row in queue),
                         "matched_losing_controls_verified": False, "max_per_entity": max_per_entity,
                         "selection_is_not_a_profit_score": True, "provider_budget_increase": 0},
        "next_state": {"version": VERSION, "round": round_no + 1, "selection_counts": counts},
        "limitations": ["Provider arithmetic and export labels are nominations, not independently measured trader or copy profit.",
                        "Leader behavior may teach useful rules without being reachable or profitable to copy.",
                        "Existing proven.py omits gas/slippage; gross followthrough omits follower execution economics.",
                        "Intake must recheck absence under a write lock and preserve existing cohorts, sources and rosters."]})
