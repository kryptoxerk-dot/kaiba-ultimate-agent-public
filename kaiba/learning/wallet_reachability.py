"""Retrospective quantities at a retained opening leg's first research capture.

This pure projection measures the fixed research path, never canonical/feed arrival,
executable follower prices, complete custody or copy profit. No selection is made.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from kaiba.core.schemas import Chain
from kaiba.intelligence import grade
from kaiba.intelligence.pnl import ratio
from kaiba.learning import fixed_wallet_study as fixed
from kaiba.learning import wallet_behavior_study as behavior
from kaiba.learning import wallet_observation_tape as tape

VERSION = "wallet-reachability/1"
MAX_NODES = 3_000_000
MAX_DEPTH = 32
MAX_STRING = 4096
MAX_ASSOCIATION_WORK = 1_000_000
MAX_EXAMPLES = 20
MAX_REPORT_BYTES = 1024 * 1024
_CLOSED_FAILURES = frozenset({"structure_cap", "string_cap", "integer_cap", "nonfinite", "value_type",
                              "clock", "diagnostics", "report_capacity"})
_MONEY_ONLY_FLAGS = frozenset({"buy_without_native_amount", "sell_without_native_amount"})
POLICY = {"capture_scope": "first_retained_opening_raw_occurrence",
          "coverage": "completed_declared_windows_through_capture",
          "measurement": "retrospective_observed_swap_quantity_only",
          "quantity_scope": "one_observed_episode_not_current_wallet_balance",
          "canonical_arrival_proven": False, "follower_execution_proven": False,
          "funding_allowed": False}


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def source_versions():
    return {**tape.source_versions(), **fixed.source_versions(),
            "learning/wallet_reachability.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def _bounded(value):
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise ValueError("structure_cap")
        if type(item) is dict:
            if len(item) * 2 > MAX_NODES - count - len(pending):
                raise ValueError("structure_cap")
            pending.extend((part, depth + 1) for pair in item.items() for part in pair)
        elif type(item) in (list, tuple):
            if len(item) > MAX_NODES - count - len(pending):
                raise ValueError("structure_cap")
            pending.extend((part, depth + 1) for part in item)
        elif type(item) is str:
            if len(item) > MAX_STRING:
                raise ValueError("string_cap")
        elif type(item) is int:
            if item.bit_length() > 2048:
                raise ValueError("integer_cap")
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("nonfinite")
        elif item is not None and type(item) is not bool:
            raise ValueError("value_type")


def _numeric_safe(rows):
    # The original normalizer/Decimal->int path must not expand huge exponents.
    for row in rows:
        for field in ("amount_token", "amount_native", "usd_value"):
            value = row.get(field)
            if value is None or value == "":
                continue
            try:
                number = Decimal(str(value))
            except (InvalidOperation, ValueError):
                continue  # Original analyzer records ordinary missing/invalid fields.
            if not number.is_finite() or abs(number.adjusted()) > 512 or abs(number.as_tuple().exponent) > 512:
                return False
    return True


def _token(value, chain):
    return behavior._address(value, Chain(chain))


def _key(row, chain):
    return (_token(row["token"], chain), str(row.get("tx") or "").strip(),
            str(row.get("side") or "").strip().lower(), behavior._atoms(row.get("amount_token")),
            behavior._atoms(row.get("amount_native")), behavior._usd(row.get("usd_value")), row["ts_ms"])


def _diagnostics(diagnostics, wallet):
    if diagnostics is None:
        return ["transport_diagnostics_not_supplied"]
    if any(d.get("wallet") is None or tuple(d.get("wallet", ())) == wallet for d in diagnostics):
        return ["unresolved_transport_diagnostic"]
    return []


def _unknown_wallet(chain, wallet, role, reason):
    return {"chain": chain, "wallet": wallet, "role": role, "status": "unknown",
            "reasons": [reason], "post_selection_episode_count": None,
            "quantity_evaluated_count": 0, "quantity_unknown_count": None,
            "examples": [], "copy_qualified": False}


def _normalization(coverage):
    result = coverage.model_dump(mode="json")
    for field in ("sources", "feeds"):
        value = result[field]
        keys = sorted(value)
        result[field] = {k: value[k] for k in keys[:16]} if isinstance(value, dict) else keys[:16]
        result[field + "_omitted"] = max(0, len(value) - 16)
        result[field + "_sha256"] = _hash(value)
    return result


def _wallet(checked, wallet, role, diagnostics):
    chain, address = wallet
    saved = checked["wallets"][":".join(wallet)]
    if saved["seed_status"] != "verified":
        return _unknown_wallet(chain, address, role, "baseline_unseeded")
    entries = saved["rows"]
    raw = [entry["raw"] for entry in entries]
    if not _numeric_safe(raw):
        return _unknown_wallet(chain, address, role, "numeric_expansion_cap")
    normalized = grade.normalise_tape_rows(raw, Chain(chain))
    rows = normalized.rows
    unit_bases = defaultdict(set)
    for row in raw:
        unit_bases[_token(row["token"], chain)].add("human" if grade._is_human_unit_source(row.get("source")) else "base")
    mixed_quantity_tokens = {token for token, bases in unit_bases.items() if len(bases) > 1}
    # Every closed episode needs a non-buy movement, plus at most one final open
    # episode per token. This matrix upper bound survives later deduplication:
    # reconstructing duplicate buys first could incorrectly merge many episodes.
    row_counts = Counter(_token(r["token"], chain) for r in rows)
    nonbuys = Counter(_token(r["token"], chain) for r in rows
                     if str(r.get("side") or "").strip().lower() != "buy")
    work = sum(count * (nonbuys[token] + 1) for token, count in row_counts.items())
    if work > MAX_ASSOCIATION_WORK:
        return {**_unknown_wallet(chain, address, role, "analysis_work_cap"),
                "post_selection_episode_count": None, "association_work": work}
    profile = behavior.analyze_wallet(rows, chain=chain, wallet=address,
        as_of_ms=saved["latest_as_of_ms"], metadata={"money_axis": normalized.coverage.money_axis})
    selection = checked["fixed_state"]["manifest"]["selected_ms"]
    indexed = {entry["raw"]["id"]: entry for entry in entries}
    normalized_by_id = {row["id"]: row for row in rows}
    occurrences = defaultdict(list)
    transaction_legs = defaultdict(set)
    for row in rows:
        identity = _key(row, chain)
        occurrences[identity].append(indexed[row["id"]])
        transaction_legs[identity[:3]].add(identity)
    normalized_ids = {r["id"] for r in rows}
    removed_tokens = {_token(r["token"], chain) for r in raw if r["id"] not in normalized_ids}
    first_receipts = {}
    for receipt in saved["receipts"]:
        for rowid in receipt["packet_rowids"]:
            first_receipts.setdefault(rowid, receipt["chain_sha256"])
    counts, reasons, examples = Counter(), Counter(), []
    for episode in profile["episodes"]:
        if episode["opened_ms"] <= selection:
            counts["baseline_or_carry_episode_count"] += 1
            continue
        counts["post_selection_episode_count"] += 1
        counts["closed_episode_count" if episode["state"] == "closed" else "open_episode_count"] += 1
        item = {"episode_id": episode["episode_id"], "token": episode["token"],
                "opened_ms": episode["opened_ms"], "closed_ms": episode["closed_ms"],
                "state": episode["state"], "status": "unknown", "reasons": [],
                "episode_contamination": list(episode["contamination"]),
                "money_components_status": "unknown" if _MONEY_ONLY_FLAGS.intersection(episode["contamination"])
                    else "not_evaluated",
                "bought_units_through_capture": None, "sold_units_through_capture": None,
                "observed_residual_units": None, "sold_fraction": None}
        issues = _diagnostics(diagnostics, wallet)
        if set(episode["contamination"]) - _MONEY_ONLY_FLAGS:
            issues.append("contaminated_episode")
        if episode["strong_identity_conflict"]:
            issues.append("conflicting_transaction_identity")
        if any(len(transaction_legs[_key(normalized_by_id[r["id"]], chain)[:3]]) > 1
               for r in episode["trades"] if r.get("id") in normalized_by_id):
            issues.append("transaction_leg_identity_ambiguous")
        if any(len(occurrences[_key(normalized_by_id[r["id"]], chain)]) > 1
               for r in episode["trades"] if r.get("id") in normalized_by_id):
            # The retained schema has no instruction/log index. Equal tx legs
            # may be duplicate occurrences or genuine equal legs; neither an
            # exact quantity nor an exact-dedup witness follows from equality.
            issues.append("identical_transaction_leg_ambiguity")
        if any(not str(r.get("tx") or "").strip() for r in episode["trades"]):
            issues.append("transaction_identity_missing")
        if not episode["detail_evaluated"] or episode["same_timestamp_token_profit_unevaluated"]:
            issues.append("ambiguous_episode_order")
        if episode["token"] in removed_tokens:
            issues.append("normalization_removed_token_rows")
        if episode["token"] in mixed_quantity_tokens:
            issues.append("canonical_token_quantity_basis_mixture")
        opening = [r for r in episode["trades"] if r["side"] == "buy" and r["ts_ms"] == episode["opened_ms"]]
        candidates = []
        for trade in opening:
            entry = indexed.get(trade.get("id"))
            if entry is None:
                continue
            original = normalized_by_id.get(trade["id"])
            if original is not None:
                candidates.extend(occurrences[_key(original, chain)])
        if not candidates:
            issues.append("opening_raw_lineage_unknown")
        else:
            first = min(candidates, key=lambda e: (e["first_captured_ms"], e["raw"]["id"]))
            capture = first["first_captured_ms"]
            item.update(first_retained_opening_raw_capture_ms=capture,
                        opening_capture_lag_ms=capture - episode["opened_ms"],
                        opening_raw_sha256=_hash(first["raw"]),
                        opening_capture_receipt_sha256=first_receipts[first["raw"]["id"]],
                        retrospective_adjudication_capture_ms=saved["latest_capture_ms"])
            if saved["completed_through_ms"] < capture:
                issues.append("completed_coverage_before_capture")
            if any(g["start_ms"] <= capture and g["end_ms"] >= episode["opened_ms"]
                   for g in saved["unobserved_intervals"]):
                issues.append("capture_interval_has_gap")
            page = saved["open_page_window"]
            if page is not None and page["cursor"][0] <= capture and page["requested_upper_ms"] >= episode["opened_ms"]:
                issues.append("capture_interval_has_open_page")
            before = [r for r in episode["trades"] if r["ts_ms"] <= capture]
            quantities = [(r["side"], behavior._atoms(r["amount_token_units"])) for r in before]
            if any(q is None or q <= 0 or side not in {"buy", "sell"} for side, q in quantities):
                issues.append("quantity_units_unknown")
            else:
                bought = sum(q for side, q in quantities if side == "buy")
                sold = sum(q for side, q in quantities if side == "sell")
                if bought <= 0 or sold > bought:
                    issues.append("inventory_inconsistent")
                elif not issues:
                    item.update(status="observed_retrospective_quantity", bought_units_through_capture=str(bought),
                                sold_units_through_capture=str(sold), observed_residual_units=str(bought - sold),
                                sold_fraction=str(ratio(sold, bought)))
                    counts["fully_sold_count" if sold == bought else "partially_sold_count" if sold else "no_observed_sale_count"] += 1
        item["reasons"] = sorted(set(issues))
        reasons.update(item["reasons"])
        counts["quantity_evaluated_count" if not issues else "quantity_unknown_count"] += 1
        if len(examples) < MAX_EXAMPLES:
            examples.append(item)
    return {"chain": chain, "wallet": address, "role": role, "status": "observed_partial_retrospective_tape",
            **{name: counts[name] for name in ("post_selection_episode_count", "baseline_or_carry_episode_count",
                "closed_episode_count", "open_episode_count", "quantity_evaluated_count", "quantity_unknown_count",
                "fully_sold_count", "partially_sold_count", "no_observed_sale_count")},
            "unknown_reasons": dict(sorted(reasons.items())), "retained_rows": len(raw),
            "completed_through_ms": saved["completed_through_ms"], "latest_query_as_of_ms": saved["latest_as_of_ms"],
            "latest_actual_capture_ms": saved["latest_capture_ms"], "seed_actual_capture_ms": saved["seed_actual_capture_ms"],
            "raw_chain_sha256": saved["receipts"][-1]["chain_sha256"], "retained_raw_sha256": saved["retained_raw_sha256"],
            "normalization": _normalization(normalized.coverage), "normalized_rows_sha256": _hash(rows),
            "frozen_baseline": checked["fixed_state"]["selections"][":".join(wallet)]["baseline"],
            "frozen_baseline_normalization_matches": _hash(tape._baseline_rows(rows, selection))
                == checked["fixed_state"]["selections"][":".join(wallet)]["baseline"]["normalization_sha256"],
            "examples": examples, "examples_omitted": max(0, counts["post_selection_episode_count"] - len(examples)),
            "association_work": work, "copy_qualified": False}


def project(engine_state, *, now_ms, diagnostics=None):
    """Project all six frozen identities; diagnostics must come from the bound store.

    ``None`` withholds transport coverage. Even an empty supplied list cannot grant
    custody, arrival or economic completeness. The wrapper binds transport genesis,
    store/report/review hashes; none exists inside this engine-only input.
    """
    try:
        if type(now_ms) is not int or now_ms <= 0:
            raise ValueError("clock")
        _bounded(engine_state)
        _bounded(diagnostics)
        if diagnostics is not None and (type(diagnostics) not in (list, tuple) or len(diagnostics) > 128
                                       or any(type(d) is not dict for d in diagnostics)):
            raise ValueError("diagnostics")
        checked = tape._state(engine_state, now_ms)
        wallets = []
        for chain, address, role in fixed.MEMBERS:
            saved = checked["wallets"][chain + ":" + address]
            try:
                row = _wallet(checked, (chain, address), role, diagnostics)
            except (ValueError, TypeError, KeyError, OverflowError, InvalidOperation):
                row = _unknown_wallet(chain, address, role, "wallet_analysis_unproved")
            row.update(seed_status=saved["seed_status"], retained_rows=len(saved["rows"]),
                latest_actual_capture_ms=saved.get("latest_capture_ms"),
                latest_query_as_of_ms=saved.get("latest_as_of_ms"),
                completed_through_ms=saved.get("completed_through_ms"),
                page_window_status="partial_window" if saved.get("open_page_window") else "declared_windows_only",
                unobserved_interval_count=len(saved.get("unobserved_intervals", [])))
            wallets.append(row)
        result = {"version": VERSION, "status": "retrospective_partial_research_evidence", "projected_ms": now_ms,
                "policy": dict(POLICY), "engine_state_sha256": checked["state_sha256"],
                "manifest_sha256": checked["manifest_sha256"], "selection_ms": checked["fixed_state"]["manifest"]["selected_ms"],
                "engine_created_ms": checked["created_ms"], "source_versions": source_versions(),
                "diagnostics_sha256": _hash(diagnostics), "fixed_wallet_denominator": len(fixed.MEMBERS),
                "wallets": wallets, "copy_qualified": False, "profitability_status": "UNPROVEN",
                "canonical_arrival": "UNPROVEN", "complete_custody": "UNPROVEN", "follower_execution": "UNPROVEN",
                "provider_calls": 0, "source_queries": 0, "funding_allowed": False}
        # Identity ignores the analysis clock: unchanged evidence cannot look new.
        result["evidence_id"] = _hash({"version": VERSION, "policy": POLICY,
            "engine_state_sha256": checked["state_sha256"], "diagnostics_sha256": result["diagnostics_sha256"],
            "source_versions": result["source_versions"]})
        if len(_encoded(result)) > MAX_REPORT_BYTES:
            raise ValueError("report_capacity")
        return result
    except (ValueError, TypeError, KeyError, OverflowError, InvalidOperation) as exc:
        reason = "input_or_analysis_unproved"
        if type(exc) is ValueError and len(exc.args) == 1 and type(exc.args[0]) is str and exc.args[0] in _CLOSED_FAILURES:
            reason = next(code for code in _CLOSED_FAILURES if exc.args[0] == code)
        return {"version": VERSION, "status": "unknown", "reason": reason,
                "fixed_wallet_denominator": len(fixed.MEMBERS), "wallets": [
                    _unknown_wallet(c, a, role, "input_or_analysis_unproved") for c, a, role in fixed.MEMBERS],
                "copy_qualified": False, "profitability_status": "UNPROVEN", "funding_allowed": False}
