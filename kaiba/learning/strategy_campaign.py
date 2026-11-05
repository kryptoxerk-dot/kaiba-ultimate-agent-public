"""Frozen, prospective PAPER campaigns; no database, promotion or trading APIs.

Codex owns this additive module (AUTOLEARN-IMPLEMENT-20261014). Exploratory exits
are declared hypotheses, including when the existing search has zero survivors.
Only complete, tagged PaperBroker cashflows can support PAPER_EVIDENCE_READY;
replayed returns, a refreshed report timestamp, or a changed sample mean cannot.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kaiba.learning import signal_audit as sa
from kaiba.learning import sprint
from kaiba.learning import strategy_search as ss

CAMPAIGN_VERSION = "strategy_campaign/1"
READY_DAYS = 3
EVIDENCE_MAX_AGE_MS = ss.DAY_MS
FDR_Q = 0.10
# Only fields the prospective read_opportunities adapter can freeze from the stored
# signal payload. A replay feature with no corresponding prospective measurement
# cannot produce an executable hypothesis. Current tags/grades, delayed X, and
# wallet_track_* (whose upstream frontier omits final fill delay) stay excluded.
ENTRY_FEATURES = frozenset(sa.DOSSIER_KEYS) - ss.EXCLUDED_FEATURES.keys()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _finite_json(value: Any) -> Any:
    """An exact constant positive sample has t=inf; retain JSON without inventing t."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {k: _finite_json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_finite_json(v) for v in value]
    return value


def _numbers_hash(spec: Mapping[str, Any]) -> str:
    return _hash({**spec, "exit": {k: v for k, v in spec["exit"].items() if k != "name"}})


def source_versions() -> dict[str, str]:
    """Freeze the actual evaluator/broker sources, rather than a mutable version label."""
    root = Path(__file__).resolve().parents[1]
    paths = ("learning/strategy_campaign.py", "learning/strategy_search.py",
             "learning/sprint.py", "learning/signal_audit.py", "execution/paper.py",
             "execution/curve_price.py", "execution/protection.py", "learning/candidate_paper.py",
             "learning/autolearn_io.py")
    return {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in paths}


def exploration_policies(base_ladder: sa.ExitLadder) -> list[ss.ExitPolicy]:
    """Fixed hypotheses, NEVER a statement that these numbers are profitable."""
    return [ss.ExitPolicy("incumbent", base_ladder),
            ss.ExitPolicy("hold60s", base_ladder, 60_000),
            ss.ExitPolicy("hold5m", base_ladder, 300_000),
            ss.ExitPolicy("tp1.25all", dataclasses.replace(base_ladder, tps=((1.25, 1.0),)))]


def _campaign_id(manifest: Mapping[str, Any]) -> str:
    keys = ("version", "mode", "selected_ms", "data_cutoff_ms", "data_hash", "source_versions",
            "assessment_policy", "hypotheses")
    return "pc_" + _hash({k: manifest[k] for k in keys})[:24]


def generate_campaign(observations: Sequence[ss.Obs], base_ladder: sa.ExitLadder, *, now_ms: int,
                      max_candidates: int = 6,
                      groups: Sequence[tuple[str, str]] | None = None,
                      trained_only: bool = False) -> dict[str, Any]:
    """Search settled historical data and freeze a globally bounded paper experiment.

    ``groups`` bootstraps fixed hypotheses without fabricated observations. Fairly reserve
    incumbent/challenger pairs, then round-robin remaining exits; record omitted work.
    ``trained_only`` uses the remaining global capacity for incumbent+learned pairs,
    without repeating the fixed exploratory exits already in another campaign.
    Existing search survivors can use spare slots, but exploration is separately labelled
    and never inserted into the existing strategy_candidates table.
    """
    if max_candidates < 1:
        raise ValueError("max_candidates must be positive")
    cutoff = int(now_ms) - ss.SETTLE_MS
    requested = set(groups or ()) | {(o.lane, o.chain) for o in observations}
    safe: list[ss.Obs] = []
    omitted_features: set[str] = set()
    seen: dict[tuple[str, str, str, int], str] = {}
    for o in observations:
        if o.t0 > cutoff:
            continue
        feats = {k: v for k, v in o.features.items() if k in ENTRY_FEATURES
                 and (not isinstance(v, float) or math.isfinite(v))}
        omitted_features.update(set(o.features) - set(feats))
        raw = {k: float(v) for k, v in o.raw.items() if math.isfinite(float(v))}
        row = ss.Obs(o.lane, o.chain, o.token, int(o.t0), feats, raw)
        identity = row.lane, row.chain, row.token, row.t0
        fingerprint = _canonical(dataclasses.asdict(row))
        if identity in seen:
            if seen[identity] != fingerprint:
                raise ValueError("conflicting duplicate historical observation")
            continue
        seen[identity] = fingerprint
        safe.append(row)
    safe.sort(key=lambda o: (o.lane, o.chain, o.t0, o.token))
    policies = exploration_policies(base_ladder)
    # Also search the existing declared exit family when its paths were collected.
    search_policies = {p.key: p for p in [*ss.declared_policies(base_ladder), *policies]}
    search_result = ss.search(safe, list(search_policies.values()), ss.SearchConfig(), end_ms=cutoff)
    ordered = sorted(requested)
    if ordered:
        rotation = (int(now_ms) // ss.DAY_MS) % len(ordered)
        ordered = ordered[rotation:] + ordered[:rotation]
    capacity = max_candidates // 2 if trained_only else max(1, max_candidates // 2)
    survivors: dict[tuple[str, str], list[ss.Candidate]] = collections.defaultdict(list)
    for candidate in sorted(search_result.survivors, key=lambda c: (c.hold_p, c.candidate_id)):
        incumbent = {"lane": candidate.lane, "chain": candidate.chain, "filter": [],
                     "exit": policies[0].spec()}
        if trained_only and _numbers_hash(candidate.spec()) == _numbers_hash(incumbent):
            continue
        survivors[(candidate.lane, candidate.chain)].append(candidate)
    eligible_groups = [g for g in ordered if survivors[g]] if trained_only else ordered
    active = eligible_groups[:capacity]
    proposed: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    if trained_only:
        used_specs: set[str] = set()
        for lane, chain in active:
            baseline = {"lane": lane, "chain": chain, "filter": [], "exit": policies[0].spec()}
            proposed.append(("control", baseline, {}))
            used_specs.add(_numbers_hash(baseline))
        # Give each group its first survivor before spending capacity on a second.
        for i in range(max((len(survivors[g]) for g in active), default=0)):
            for group in active:
                if i >= len(survivors[group]):
                    continue
                candidate = survivors[group][i]
                spec = candidate.spec()
                if _numbers_hash(spec) not in used_specs:
                    proposed.append(("trained", spec, _finite_json(candidate.selection())))
                    used_specs.add(_numbers_hash(spec))
    else:
        for policy in policies:
            for lane, chain in active:
                proposed.append(("exploratory", {"lane": lane, "chain": chain, "filter": [],
                                               "exit": policy.spec()}, {}))
        used_specs = {_numbers_hash(spec) for _kind, spec, _ev in proposed}
        for candidate in sorted(search_result.survivors, key=lambda c: (c.hold_p, c.candidate_id)):
            spec = candidate.spec()
            if _numbers_hash(spec) not in used_specs:
                proposed.append(("trained", spec, _finite_json(candidate.selection())))
                used_specs.add(_numbers_hash(spec))
    hypotheses = []
    for kind, spec, selection in proposed[:max_candidates]:
        spec_hash = _hash(spec)
        hypotheses.append({"hypothesis_id": "ph_" + spec_hash[:24], "spec_hash": spec_hash,
                           "spec": spec, "kind": kind, "status": "PAPER_ONLY",
                           "selection": selection,
                           "hypothesis": "Declared experiment; profitability is unproven"})
    manifest: dict[str, Any] = {
        "version": CAMPAIGN_VERSION, "mode": "trained" if trained_only else "exploration",
        "selected_ms": int(now_ms), "data_cutoff_ms": cutoff,
        "data_hash": _hash([dataclasses.asdict(o) for o in safe]),
        "source_versions": source_versions(),
        "assessment_policy": {"fdr_q": FDR_Q, "method": "bh", "ready_days": READY_DAYS,
                              "evidence_max_age_ms": EVIDENCE_MAX_AGE_MS,
                              "min_n": sprint.MIN_N, "min_half": sprint.MIN_HALF},
        "tried": search_result.tried, "selected": len(search_result.selected),
        "holdout_survivors": len(search_result.survivors), "observation_count": len(safe),
        "search_groups": search_result.groups, "excluded_features": sorted(omitted_features),
        "groups": [list(g) for g in active],
        "omitted_groups": [list(g) for g in ordered if g not in active],
        "omitted_hypotheses": [{"kind": kind, "spec": spec}
                               for kind, spec, _ev in proposed[max_candidates:]],
        "hypotheses": hypotheses, "funding_allowed": False, "status": "FROZEN_PAPER_ONLY",
    }
    manifest["campaign_id"] = _campaign_id(manifest)
    return manifest


def matches_hypothesis(hypothesis: Mapping[str, Any], lane: str, chain: str,
                       features: Mapping[str, Any]) -> bool:
    """Apply the existing Cond contract to a point-in-time signal packet."""
    spec = hypothesis["spec"]
    return lane == spec["lane"] and chain == spec["chain"] and all(
        ss.Cond.from_spec(c).test(features) for c in spec.get("filter", [])
    )


def _integer(row: Mapping[str, Any], key: str) -> int:
    value = row[key]
    if isinstance(value, bool) or Decimal(str(value)) != Decimal(str(int(value))):
        raise ValueError(f"{key} must be an integer")
    return int(value)


def _validate_outcome(row: Mapping[str, Any], hypothesis: Mapping[str, Any], selected_ms: int,
                      now_ms: int) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Return complete entry/exit cashflow, unknown reason, or a proved zero-exposure miss."""
    try:
        if row.get("provider") != "paper" or row.get("source") != "PaperBroker":
            return None, "not an actual PaperBroker outcome", False
        if row.get("spec_hash") != hypothesis["spec_hash"]:
            return None, "spec mismatch", False
        spec = hypothesis["spec"]
        if row.get("lane") != spec["lane"] or row.get("chain") != spec["chain"]:
            return None, "group mismatch", False
        if not row.get("outcome_id") or not row.get("signal_id") or not row.get("token"):
            return None, "missing outcome/signal identity", False
        signal_ms = _integer(row, "signal_ms")
        observed_ms = _integer(row, "observed_ms")
        if not selected_ms < signal_ms <= observed_ms <= now_ms:
            return None, "non-prospective or future evidence", False
        if row.get("feature_ts_ms") is not None and _integer(row, "feature_ts_ms") > signal_ms:
            return None, "future entry features", False
        if row.get("status") in ("unfilled", "refused", "failed"):
            proved = (row.get("no_position_created") is True
                      and row.get("evidence_complete") is True
                      and row.get("cost_complete") is True
                      and row.get("inventory_complete") is True
                      and _integer(row, "entry_debit_native") == 0
                      and _integer(row, "exit_credit_native") == 0)
            return (None, None, True) if proved else (None, "unproved unfilled attempt", False)
        if row.get("status") != "closed":
            return None, "open, partial or unfillable inventory", False
        if not all(row.get(k) is True for k in ("evidence_complete", "cost_complete", "inventory_complete")):
            return None, "incomplete economics", False
        opened_ms, closed_ms = _integer(row, "opened_ms"), _integer(row, "closed_ms")
        if not signal_ms <= opened_ms <= closed_ms <= observed_ms:
            return None, "invalid cashflow chronology", False
        entry, exit_ = _integer(row, "entry_debit_native"), _integer(row, "exit_credit_native")
        if entry <= 0 or exit_ < 0:
            return None, "missing economic denominator", False
        # Both amounts include every broker fee/tip. Applying sprint.net would double-charge.
        ret = float(Decimal(exit_) / Decimal(entry) - 1)
        ret = max(-1.0, min(sprint.OUTCOME_CAP, ret))
        features = row.get("features") or {}
        if not matches_hypothesis(hypothesis, str(row["lane"]), str(row["chain"]), features):
            return None, "entry does not match frozen filter", False
        complete = {"outcome_id": str(row["outcome_id"]), "signal_id": str(row["signal_id"]),
                    "token": str(row["token"]), "signal_ms": signal_ms, "opened_ms": opened_ms,
                    "closed_ms": closed_ms, "observed_ms": observed_ms,
                    "entry_debit_native": entry, "exit_credit_native": exit_, "net_return": ret}
        return complete, None, False
    except (KeyError, TypeError, ValueError, OverflowError, InvalidOperation):
        return None, "malformed or missing economic evidence", False


def assess_campaign(manifest: Mapping[str, Any], paper_outcomes: Sequence[Mapping[str, Any]], *,
                    now_ms: int, prior_state: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Score cumulative prospective actual broker outcomes; return the next durable state.

    Readiness needs three consecutive UTC dates, strictly new complete outcome IDs
    beyond the last counted close frontier, an unchanged complete prefix, and fresh
    closes. All frozen arms enter the BH family, including empty/unknown arms (p=1).
    This is research evidence under the broker cost model; it never authorizes capital.
    Proved entry refusals with zero exposure remain visible in coverage, without
    inventing a return denominator or hiding entered, unresolved inventory.
    """
    cid, selected_ms = str(manifest["campaign_id"]), int(manifest["selected_ms"])
    day = datetime.datetime.fromtimestamp(now_ms / 1000, datetime.UTC).date()
    today, yesterday = day.isoformat(), (day - datetime.timedelta(days=1)).isoformat()
    manifest_valid = (cid == _campaign_id(manifest)
                      and manifest.get("source_versions") == source_versions()
                      and manifest.get("version") == CAMPAIGN_VERSION
                      and selected_ms <= now_ms)
    previous = dict(prior_state or {})
    if previous.get("campaign_id") != cid:
        previous = {}
    previous_arms = previous.get("arms") or {}
    grouped: dict[str, list[Mapping[str, Any]]] = collections.defaultdict(list)
    foreign = 0
    known_hypotheses = {h["hypothesis_id"] for h in manifest["hypotheses"]}
    for row in paper_outcomes:
        if row.get("campaign_id") != cid or row.get("hypothesis_id") not in known_hypotheses:
            foreign += 1
            continue
        grouped[str(row["hypothesis_id"])].append(row)
    arms = []
    for hypothesis in manifest["hypotheses"]:
        hid = hypothesis["hypothesis_id"]
        unknown: list[str] = []
        complete: dict[str, dict[str, Any]] = {}
        by_id: dict[str, str] = {}
        by_signal: dict[tuple[str, str], str] = {}
        known_unfilled = 0
        duplicate_count = 0
        for row in grouped[hid]:
            oid = str(row.get("outcome_id") or "")
            try:
                fingerprint = _canonical(row)
            except (TypeError, ValueError):
                unknown.append("non-serializable outcome")
                continue
            if oid and oid in by_id:
                if by_id[oid] == fingerprint:
                    duplicate_count += 1
                else:
                    unknown.append("conflicting duplicate outcome")
                continue
            by_id[oid] = fingerprint
            case_key = str(row.get("signal_id") or ""), str(row.get("token") or "")
            if case_key in by_signal and by_signal[case_key] != oid:
                unknown.append("same signal counted twice")
                continue
            by_signal[case_key] = oid
            valid, why, unfilled = _validate_outcome(row, hypothesis, selected_ms, now_ms)
            if valid:
                complete[oid] = valid
            elif unfilled:
                known_unfilled += 1
            else:
                unknown.append(why or "unknown outcome")
        spec = hypothesis["spec"]
        score = sprint.score(spec["lane"], spec["chain"],
                             [(r["signal_ms"], r["net_return"]) for r in complete.values()],
                             selected_ms, now_ms)
        p = ss.p_mean_positive([r["net_return"] for r in complete.values()])
        if unknown or not manifest_valid:
            p = 1.0
        arms.append({"hypothesis_id": hid, "spec_hash": hypothesis["spec_hash"],
                     "kind": hypothesis["kind"], "spec": spec, "score": dataclasses.asdict(score),
                     "p": p, "unknown_count": len(unknown), "unknown_reasons": sorted(set(unknown)),
                     "complete": complete, "known_unfilled": known_unfilled,
                     "duplicates_ignored": duplicate_count})
    # The incumbent must see the same candidate stream. A learned entry filter may
    # narrow that stream, so compare its eligible signals with the corresponding
    # incumbent subset; never compare unrelated winners with an aggregate baseline.
    incumbents = {(a["spec"]["lane"], a["spec"]["chain"]): a for a in arms
                  if a["spec"]["exit"]["name"] == "incumbent" and not a["spec"]["filter"]}
    for arm in arms:
        baseline = incumbents.get((arm["spec"]["lane"], arm["spec"]["chain"]))
        own = {(r["signal_id"], r["token"]): r["net_return"] for r in arm["complete"].values()}
        base = {} if baseline is None else {
            (r["signal_id"], r["token"]): r["net_return"] for r in baseline["complete"].values()
        }
        paired = sorted(own.keys() & base.keys())
        delta = [own[k] - base[k] for k in paired]
        arm["comparison"] = {
            "incumbent_hypothesis_id": None if baseline is None else baseline["hypothesis_id"],
            "paired_closed_count": len(paired), "candidate_only_closed_count": len(own.keys() - base.keys()),
            "mean_delta_net": sum(delta) / len(delta) if delta else None,
            "mean_incumbent_net": sum(base[k] for k in paired) / len(paired) if paired else None,
            "mean_candidate_net": sum(own[k] for k in paired) / len(paired) if paired else None,
            "complete": baseline is not None and len(paired) == len(own),
        }
        if not arm["comparison"]["complete"]:
            arm["p"] = 1.0
    significant = ss.benjamini_hochberg([a["p"] for a in arms], FDR_Q)
    state: dict[str, Any] = {"campaign_id": cid, "evaluated_ms": int(now_ms), "arms": {}}
    for arm, corrected in zip(arms, significant, strict=True):
        hid, complete = arm["hypothesis_id"], arm.pop("complete")
        prev = dict(previous_arms.get(hid) or {})
        signatures = {oid: _hash({k: v for k, v in row.items() if k != "observed_ms"})
                      for oid, row in complete.items()}
        old_ids = dict(prev.get("complete_ids") or {})
        prefix_ok = all(signatures.get(oid) == sig for oid, sig in old_ids.items())
        new_ids = set(signatures) - old_ids.keys()
        frontier = max((r["closed_ms"] for r in complete.values()), default=selected_ms)
        fresh = (bool(complete) and 0 <= now_ms - frontier <= EVIDENCE_MAX_AGE_MS
                 and frontier > selected_ms)
        monotonic = bool(new_ids) and all(complete[oid]["closed_ms"] > int(prev.get("close_frontier_ms", selected_ms))
                                         for oid in new_ids)
        comparable = bool(arm["comparison"]["complete"])
        eligible = (manifest_valid and prefix_ok and not arm["unknown_count"] and fresh and comparable
                    and arm["score"]["verdict"] == "PASS" and corrected)
        streak, counted_day = int(prev.get("streak", 0)), prev.get("counted_day")
        if not manifest_valid:
            why, streak = "manifest or evaluator version changed", 0
        elif not prefix_ok:
            why, streak = "complete evidence disappeared or changed", 0
        elif arm["unknown_count"]:
            why, streak = "incomplete prospective evidence", 0
        elif not fresh:
            why, streak = "complete cashflows are missing or stale", 0
        elif not comparable:
            why, streak = "incumbent evidence missing for eligible candidate signals", 0
        elif not eligible:
            why, streak = "net pass line or family correction did not pass", 0
        elif counted_day == today:
            why = "same date does not advance readiness"
        elif not monotonic:
            why, streak = "no strictly new complete cashflows beyond previous frontier", 0
        else:
            streak = streak + 1 if counted_day == yesterday and streak > 0 else 1
            counted_day, why = today, "new complete cashflows on consecutive date"
        status = ("PAPER_EVIDENCE_READY" if eligible and streak >= READY_DAYS else
                  "PASS" if eligible else "UNPROVEN" if
                  not manifest_valid or not prefix_ok or arm["unknown_count"] or not fresh or not comparable else
                  "UNCORRECTED" if arm["score"]["verdict"] == "PASS" and not corrected else
                  arm["score"]["verdict"])
        arm.update({"status": status, "streak": streak, "why": why, "significant": corrected,
                    "new_complete_ids": sorted(new_ids), "complete_count": len(complete),
                    "fresh": fresh, "coverage_count": len(complete) + arm["known_unfilled"] + arm["unknown_count"],
                    "economic_basis": "PaperBroker cashflows including model fees/tips",
                    "returns_conditional_on_filled": bool(arm["known_unfilled"]),
                    "funding_allowed": False})
        state["arms"][hid] = {"streak": streak, "counted_day": counted_day,
                              "complete_ids": {**old_ids, **signatures},
                              "close_frontier_ms": max(frontier, int(prev.get("close_frontier_ms", selected_ms))),
                              "evaluated_ms": int(now_ms), "why": why}
    return {"campaign_id": cid, "evaluated_ms": int(now_ms), "arms": arms, "next_state": state,
            "family_size": len(arms), "foreign_rows_ignored": foreign, "manifest_valid": manifest_valid,
            "status": "PAPER_EVIDENCE_READY" if any(a["status"] == "PAPER_EVIDENCE_READY" for a in arms)
            else "UNPROVEN", "funding_allowed": False}


__all__ = ["generate_campaign", "assess_campaign", "exploration_policies", "matches_hypothesis",
           "source_versions", "CAMPAIGN_VERSION", "ENTRY_FEATURES", "READY_DAYS"]
