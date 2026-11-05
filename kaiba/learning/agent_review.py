"""Pure operating review of captured evidence; no provider, database, or trading calls.

Codex AGENT-UPGRADE-20261015. ``build_review(packet, now_ms=...)`` accepts bounded
closed_positions (or explicitly live ``closed_live``), scope, books/paper_funnel,
quote_diagnostics, cost_coverage, ops_runs, service_health, discovery_result, and
wallet_review. Native amounts are never pooled across chains. Ledger observations
and PaperBroker model results never become verified wallet profitability.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from kaiba.learning import sprint
from kaiba.learning.economic_outcomes import project_outcomes
from kaiba.learning.fabricated import is_fabricated_outcome

VERSION = "agent_review/1"
MAX_POSITION_ROWS = 5000
MAX_OPS_ROWS = 2000


def _integer(value: Any) -> int | None:
    if isinstance(value, (bool, float)):
        return None
    try:
        n = int(value)
        return n if Decimal(str(value)) == n else None
    except (ValueError, TypeError, InvalidOperation):
        return None


def _rows(value: Any) -> list[Mapping[str, Any]]:
    return [r for r in value if isinstance(r, Mapping)] if isinstance(value, (list, tuple)) else []


def _ratio(a: int | Decimal, b: int | Decimal, factor: int = 1) -> str | None:
    if not b:
        return None
    with localcontext() as ctx:
        ctx.prec = max(50, len(str(abs(a))) + len(str(abs(b))) + 12)
        return str(Decimal(a) * factor / Decimal(b))


def _count(row: Mapping[str, Any], key: str) -> int | None:
    n = _integer(row.get(key))
    return n if n is not None and n >= 0 else None


def _ledger(packet: Mapping[str, Any], now: int) -> dict[str, Any]:
    implicit_live = "closed_positions" not in packet and "closed_live" in packet
    supplied = _rows(packet.get("closed_positions", packet.get("closed_live", [])))
    if len(supplied) > MAX_POSITION_ROWS:
        raise ValueError("position row budget exceeded; supply an explicit bounded scope")
    scope = dict(packet.get("scope") or {})
    start = _integer(scope.get("start_ms"))
    end = _integer(scope.get("end_ms", now))
    if end is None or end > now or end <= 0 or (start is not None and not 0 < start <= end):
        raise ValueError("invalid review period")
    excluded = Counter()
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    conflicts = set()
    for value in supplied:
        row = dict(value)
        if implicit_live:
            row.setdefault("mode", "live")
        row.setdefault("mode", "unknown")
        closed = _integer(row.get("closed_ms"))
        if closed is None or closed <= 0:
            excluded["missing_close_clock"] += 1
            continue
        if closed > end or (start is not None and closed < start):
            excluded["outside_requested_period"] += 1
            continue
        if not row.get("position_id") or not row.get("chain") or not row.get("lane"):
            excluded["missing_position_identity"] += 1
            continue
        if is_fabricated_outcome(row.get("exit_reason"), row["mode"]):
            excluded["fabricated_paper_outcome"] += 1
            continue
        key = str(row["chain"]), str(row["mode"]), str(row["position_id"])
        if key in unique:
            if unique[key] != row:
                conflicts.add(key)
            else:
                excluded["exact_duplicate"] += 1
            continue
        unique[key] = row
    for key in conflicts:
        unique.pop(key, None)
    excluded["conflicting_position"] += len(conflicts)
    positions = list(unique.values())
    # Reuse the existing conservative projection. Minimal ledger packets intentionally
    # retain unknown economic costs/inventory instead of manufacturing completeness.
    projected = project_outcomes([{"position": row} for row in positions])
    indexed = {(r["chain"], r["mode"], r["position_id"]): r for r in projected["outcomes"]}

    def summarize(rows: list[dict[str, Any]], *, chain: str, mode: str, lane: str | None) -> dict[str, Any]:
        pnls, costs, returns = [], [], []
        ledger_mismatch = 0
        for row in rows:
            projection = indexed[(row["chain"], row["mode"], row["position_id"])]
            pnl, cost = _integer(projection["ledger_pnl_native"]), _integer(projection["ledger_cost_native"])
            if pnl is not None:
                pnls.append(pnl)
            if cost is not None and cost > 0:
                costs.append(cost)
                if pnl is not None:
                    returns.append(Decimal(pnl) / cost)
            proceeds = _integer(row.get("proceeds_native"))
            if pnl is not None and cost is not None and proceeds is not None and proceeds - cost != pnl:
                ledger_mismatch += 1
        gains, losses = sum(n for n in pnls if n > 0), -sum(n for n in pnls if n < 0)
        capped = [float(max(Decimal(-1), min(Decimal(str(sprint.OUTCOME_CAP)), r))) for r in returns]
        lower = sprint.lower_bound(capped)
        mean_return = sum(returns) / len(returns) if returns else None
        return {
            "chain": chain, "mode": mode, "lane": lane, "position_count": len(rows),
            "ledger_pnl_count": len(pnls), "return_denominator_count": len(returns),
            "ledger_pnl_native": str(sum(pnls)) if pnls else None,
            "ledger_cost_native_known_subset": str(sum(costs)) if costs else None,
            "mean_ledger_pnl_native": _ratio(sum(pnls), len(pnls)) if pnls else None,
            "mean_ledger_return_pct": str(mean_return * 100) if mean_return is not None else None,
            "ledger_wins": sum(n > 0 for n in pnls), "ledger_losses": sum(n < 0 for n in pnls),
            "ledger_win_rate": float(Decimal(sum(n > 0 for n in pnls)) / len(pnls)) if pnls else None,
            "ledger_profit_factor": _ratio(gains, losses) if losses else None,
            "profit_factor_basis": "no_losses_in_subset" if pnls and not losses else "ledger_native_gains_over_losses",
            "ledger_cashflow_mismatch_count": ledger_mismatch,
            "capped_return_sensitivity": {"cap_return": sprint.OUTCOME_CAP, "floor_return": -1,
                "n": len(capped), "mean_pct": sum(capped) / len(capped) * 100 if capped else None,
                "bootstrap_lower_pct": lower * 100 if lower is not None else None,
                "basis": "existing sprint cap and bootstrap; ledger sensitivity, not promotion evidence"},
            "economic_profit_verified": False, "economic_pnl_native": None,
        }

    by_chain, by_lane = defaultdict(list), defaultdict(list)
    for row in positions:
        by_chain[(row["chain"], row["mode"])].append(row)
        by_lane[(row["chain"], row["mode"], row["lane"])].append(row)
    complete = (scope.get("complete") is True and scope.get("truncated") is False
                and _count(scope, "total_matching") == len(supplied))
    clocks = [_integer(r.get("closed_ms")) for r in positions]
    return {"period": {"start_ms": start, "end_ms": end, "filter": "closed_ms", "complete": complete,
                        "row_limit": _count(scope, "row_limit"), "rows_received": len(supplied),
                        "rows_retained": len(positions), "total_matching": _count(scope, "total_matching"),
                        "truncated": scope.get("truncated"), "first_observed_close_ms": min(clocks) if clocks else None,
                        "last_observed_close_ms": max(clocks) if clocks else None,
                        "population": "captured closed positions; open inventory and full-wallet economics excluded"},
            "by_chain_mode": [summarize(v, chain=k[0], mode=k[1], lane=None) for k, v in sorted(by_chain.items())],
            "by_chain_mode_lane": [summarize(v, chain=k[0], mode=k[1], lane=k[2]) for k, v in sorted(by_lane.items())],
            "excluded_counts": dict(excluded), "native_results_combined_across_chains": False,
            "economic_projection": {k: v for k, v in projected.items() if k != "outcomes"}}


def _cost_coverage(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = []
    for row in _rows(packet.get("cost_coverage", [])):
        filled = _count(row, "filled_orders")
        measured = _count(row, "fill_cost_orders")
        reconciled = _count(row, "reconciled_orders")
        valid = filled is not None and all(n is None or n <= filled for n in (measured, reconciled))
        result.append({"chain": row.get("chain"), "mode": row.get("mode", "unknown"),
                       "period": row.get("period", packet.get("cost_coverage_scope")),
                       "denominator": "distinct filled orders, not closed positions",
                       "filled_orders": filled, "fill_cost_orders": measured, "reconciled_orders": reconciled,
                       "fill_cost_fraction": float(Decimal(measured) / filled) if valid and filled and measured is not None else None,
                       "reconciled_fraction": float(Decimal(reconciled) / filled) if valid and filled and reconciled is not None else None,
                       "status": "INVALID_COUNTS" if not valid else "NO_FILLED_ORDERS" if filled == 0
                       else "MISSING" if measured == 0 else "PARTIAL" if measured is None or measured < filled
                       else "DECODED_COVERAGE_ONLY",
                       "economic_profit_verified": False})
    return result


def _sum_counts(rows: Any, key: str, value: Any = None) -> int:
    if isinstance(rows, int) and not isinstance(rows, bool):
        return max(0, rows)
    total = 0
    for row in _rows(rows):
        if value is None or row.get(key) == value:
            total += _count(row, "n") if _count(row, "n") is not None else 1
    return total


def _quote_observation(row: Mapping[str, Any], now: int) -> Counter:
    """Describe actual worker packets and explicit inspector samples separately."""
    counts = Counter(sampled=1, usable=0, usable_unknown=0, missing_decimals=0, decimals_unknown=0)
    diagnostics = row.get("diagnostics") if isinstance(row.get("diagnostics"), Mapping) else {}
    token_rows = _rows(row.get("token_row"))
    decimal_values = [row.get("decimals"), diagnostics.get("decimals")]
    if token_rows:
        decimal_values.append(token_rows[0].get("decimals"))
    decimals_known = any((n := _integer(v)) is not None and 0 <= n <= 30 for v in decimal_values)
    unavailable = str(row.get("unavailable_reason") or "")
    if not decimals_known:
        if ("token_row" in row or "decimals" in row or "decimals" in diagnostics
                or unavailable in {"token_decimals_missing", "token_decimals_missing_or_invalid"}):
            counts["missing_decimals"] = 1
        else:
            counts["decimals_unknown"] = 1

    def positive(value):
        try:
            n = Decimal(str(value))
            return not isinstance(value, bool) and n.is_finite() and n > 0
        except (InvalidOperation, ValueError, TypeError):
            return False

    curve = row.get("curve") if isinstance(row.get("curve"), Mapping) else {}
    has_curve = bool(curve) and all(positive(curve.get(k)) for k in ("virtual_sol", "virtual_token"))
    fields = row.get("dossier_fields") if isinstance(row.get("dossier_fields"), Mapping) else {}
    clocks = {}
    for field, clock_key, diagnostic_key in (("price_usd", "price_ts_ms", "dossier_price_ms"),
                                               ("liquidity_usd", "depth_ts_ms", "dossier_depth_ms")):
        measured = fields.get(field) if isinstance(fields.get(field), Mapping) else {}
        receipt = measured.get("receipt") if isinstance(measured.get("receipt"), Mapping) else {}
        clock = next((n for v in (row.get(clock_key), diagnostics.get(diagnostic_key),
                                  receipt.get("observed_at_ms")) if (n := _integer(v)) is not None), None)
        if clock is None and field in row and positive(row[field]):
            clock = _integer(curve.get("observed_ms") if has_curve else row.get("quote_ts_ms"))
        clocks[field] = clock
        counts[field + "_missing"] += 0
        counts[field + "_unknown"] += 0
        if field == "liquidity_usd" and has_curve:
            counts[field + "_not_required_curve"] += 1
        elif field in row or field in fields:
            if not positive(row[field] if field in row else measured.get("value")):
                counts[field + "_missing"] += 1
        else:
            # Unavailable runtime packets intentionally omit values. Their absence
            # is not proof the upstream field was missing rather than stale.
            counts[field + "_unknown"] += 1
        if measured.get("basis") == "stale":
            counts[field + "_explicit_stale"] += 1
        if clock is not None and now - clock > 60_000:
            counts[field + "_older_than_60s"] += 1
        if clock is None:
            counts[field + "_clock_unknown"] += 1

    if unavailable:
        pass  # explicit source refusal proves this particular packet is unusable
    elif "usable_quote" in row:
        counts["usable"] = int(bool(row["usable_quote"]))  # inspector's explicit result
    elif any(k in row for k in ("quote_ts_ms", "price_usd", "native_usd", "liquidity_usd", "curve")):
        quote_clock = _integer(row.get("quote_ts_ms"))
        required_clocks = [clocks["price_usd"]] + ([] if has_curve else [clocks["liquidity_usd"]])
        counts["usable"] = int(decimals_known and positive(row.get("price_usd"))
            and positive(row.get("native_usd")) and (has_curve or positive(row.get("liquidity_usd")))
            and quote_clock is not None and 0 <= now - quote_clock <= 60_000
            and all(c is not None and 0 < c <= quote_clock and 0 <= now - c <= 60_000 for c in required_clocks))
    else:
        counts["usable_unknown"] = 1
    return counts


def _paper(packet: Mapping[str, Any], now: int) -> dict[str, Any]:
    books = _rows(packet.get("books", []))
    if len(books) > 128:
        raise ValueError("research book budget exceeded")
    orders, positions, arm_attempts = 0, 0, 0
    chain_lower, reasons, sampled_ids = defaultdict(int), Counter(), set()
    for book in books:
        spec = (book.get("identity") or {}).get("spec", {})
        chain, lane = str(spec.get("chain", "unknown")), str(spec.get("lane", "unknown"))
        entry_rows = _rows(book.get("entries"))
        entries = sum((_count(r, "n") or 0) for r in entry_rows if r.get("reason") not in
                      {"filter_not_matched", "outside_prospective_window"}
                      and r.get("status") not in {"excludedbyfilter", "excludedbyscope"})
        arm_attempts += entries
        chain_lower[(chain, lane)] = max(chain_lower[(chain, lane)], entries)
        orders += _count(book, "order_count") if _count(book, "order_count") is not None else _sum_counts(book.get("orders"), "state")
        positions += (_count(book, "closed") or sum(
            (_count(r, "n") if _count(r, "n") is not None else 1) for r in _rows(book.get("positions"))
            if r.get("closed") is True or r.get("status") == "closed" or r.get("closed_ms") is not None))
        for row in _rows(book.get("entries")):
            reasons[str(row.get("reason") or row.get("status") or "unknown")] += _count(row, "n") or 0
        for row in _rows(book.get("latest")):
            if row.get("signal_id"):
                sampled_ids.add((chain, str(row["signal_id"])))
    supplied = dict(packet.get("paper_funnel") or {})
    eligible = _count(supplied, "eligible_opportunities")
    orders = _count(supplied, "orders") if _count(supplied, "orders") is not None else orders
    positions = _count(supplied, "closed_positions") if _count(supplied, "closed_positions") is not None else positions
    samples = _rows(packet.get("quote_diagnostics", []))
    if len(samples) > 256:
        raise ValueError("quote diagnostic sample budget exceeded")
    quote_counts = Counter()
    for row in samples:
        quote_counts.update(_quote_observation(row, now))
    quotes = _count(supplied, "usable_quotes")
    if quotes is None and samples and (quote_counts["usable"] or not quote_counts["usable_unknown"]):
        quotes = quote_counts["usable"]
    attempts_min = max(sum(chain_lower.values()), len(sampled_ids))
    current = _count(packet.get("report") or {}, "new_opportunities") or 0
    has_opportunities = (eligible > 0 if eligible is not None else attempts_min > 0 or current > 0)
    no_opportunities = not has_opportunities
    if orders == 0 and has_opportunities and quotes == 0:
        status = "DATA_FAILURE"
    elif orders == 0 and has_opportunities:
        status = "NO_PAPER_EXECUTION"
    elif no_opportunities and orders == 0:
        status = "NO_ELIGIBLE_OPPORTUNITIES"
    elif positions == 0 and orders > 0:
        status = "COLLECTING_WITHOUT_CLOSES"
    else:
        status = "PROSPECTIVE_EVIDENCE_NEEDS_ASSESSMENT"
    if (packet.get("section_status", {}).get("paper_books", {}).get("status") in {"unknown", "partial"}
            and not supplied):
        status = "SOURCE_UNAVAILABLE"
    return {"status": status, "orders": orders, "closed_positions": positions,
            "eligible_opportunities_exact": eligible, "opportunity_lower_bound": attempts_min,
            "attempts_across_arms": arm_attempts, "sampled_distinct_signal_ids": len(sampled_ids),
            "book_count": len(books), "terminal_reason_counts_across_arms": dict(reasons),
            "quote_sample": dict(quote_counts), "quote_failures": supplied.get("quote_failures", {}),
            "old_arrivals": _count(supplied, "old_arrivals"),
            "quote_sample_is_entire_historical_population": False, "funding_allowed": False}


def _operations(packet: Mapping[str, Any], now: int) -> dict[str, Any]:
    ops = _rows(packet.get("ops_runs", []))
    if len(ops) > MAX_OPS_ROWS:
        raise ValueError("operation row budget exceeded")
    latest = {}
    for row in ops:
        at = _integer(row.get("started_ms"))
        if at is not None and at <= now and (row.get("job") not in latest or at > latest[row["job"]]["started_ms"]):
            latest[row["job"]] = {**row, "started_ms": at}
    failures = [r for r in latest.values() if str(r.get("status")).lower() in {"error", "failed", "timeout", "fail"}]
    services = packet.get("service_health")
    if not isinstance(services, Mapping):
        services = {}
        for block in str(packet.get("services") or "").split("\n\n"):
            values = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            if values.get("Id"):
                services[values["Id"]] = values
    broken = []
    daemon_names = {"kaiba-ops.service", "kaiba-scan.service", "kaiba-engine.service", "kaiba-protection.service"}
    for name, fields in services.items():
        if not isinstance(fields, Mapping):
            continue
        active, result = fields.get("ActiveState"), fields.get("Result")
        if (active == "failed" or result not in (None, "success")
                or (str(name).endswith(".timer") and active != "active")
                or (name in daemon_names and active != "active")):
            broken.append(str(name))
    return {"status": "DEGRADED" if broken or failures else "OPERATING" if services or latest else "UNKNOWN",
            "failed_services": sorted(broken), "latest_failed_jobs": sorted(str(r["job"]) for r in failures),
            "latest_job_count": len(latest), "latest_runs": sorted(latest.values(), key=lambda r: str(r.get("job"))),
            "service_observation_is_profit_evidence": False}


def build_review(packet: Mapping[str, Any], *, now_ms: int) -> dict[str, Any]:
    """Describe bounded supplied evidence and the repairs it supports, never authorize funds."""
    now = _integer(now_ms)
    if now is None or now <= 0:
        raise ValueError("review requires an explicit positive millisecond frontier")
    ledger, coverage = _ledger(packet, now), _cost_coverage(packet)
    paper, operations = _paper(packet, now), _operations(packet, now)
    discovery = packet.get("discovery_result") or packet.get("discovery") or {}
    manifest = discovery.get("candidate_manifest") or discovery
    discovery_summary = {"tried": _count(manifest, "tried"), "survivors": _count(manifest, "holdout_survivors"),
                         "observations": _count(manifest, "observation_count"), "funding_allowed": False}
    actions = []

    def action(priority: int, code: str, why: str, remedy: str, chains: Sequence[str] = ()):
        actions.append({"priority": priority, "code": code, "evidence": why, "action": remedy,
                        "chains": list(chains), "funding_allowed": False})

    if operations["status"] == "DEGRADED":
        action(0, "restore_failed_jobs", "Latest supplied service/job results contain failures.",
               "Inspect the named failed services and latest failed jobs; verify the next successful run.")
    if any(v.get("status") in {"unknown", "partial"} for v in packet.get("section_status", {}).values() if isinstance(v, Mapping)):
        action(1, "restore_review_evidence_capture", "One or more review sections could not be captured completely.",
               "Honor the source backoff, then retry the bounded unknown sections and retain their capture errors.")
    samples = paper["quote_sample"]
    if samples.get("missing_decimals"):
        action(1, "capture_token_decimals", f"{samples['missing_decimals']} of {samples.get('sampled', 0)} captured token samples lack decimals.",
               "Capture token decimals from verified existing token metadata before constructing paper quotes.")
    if any(samples.get(k, 0) for k in ("price_usd_older_than_60s", "liquidity_usd_older_than_60s")):
        action(1, "capture_fresh_market_evidence", "Captured quote samples have stale price or depth components.",
               "Record fresh source receipts, curve reserves or pool depth for prospective candidates; retain missing values.")
    if paper["status"] in {"DATA_FAILURE", "NO_PAPER_EXECUTION"}:
        action(1, "repair_paper_capture_funnel", "Eligible captured opportunities exist but no paper order was created.",
               "Separate entry arrival age from quote-component failure and verify one complete prospective paper round trip.")
    missing_costs = [r["chain"] for r in coverage if r["status"] in {"MISSING", "PARTIAL", "INVALID_COUNTS"}]
    if missing_costs or not coverage:
        action(1, "complete_fill_cashflow_evidence", "Filled-order cost coverage is incomplete or unavailable.",
               "Capture transaction costs and reconcile attributed inventory using the existing economic projection; retain unknown cashflows.",
               [str(c) for c in missing_costs])
    negative = [r for r in ledger["by_chain_mode"] if r["mode"] in {"live", "canary"}
                and r["mean_ledger_return_pct"] is not None and Decimal(r["mean_ledger_return_pct"]) < 0]
    if negative:
        action(2, "measure_negative_live_expectancy", "Captured live/canary closed subsets have negative mean ledger returns.",
               "Compare fixed prospective hypotheses after measured costs and delay; retain the existing risk envelope and pass line.",
               sorted({r["chain"] for r in negative}))
    if discovery_summary["tried"] and discovery_summary["survivors"] == 0:
        action(3, "retain_unsuccessful_strategy_search", "The supplied historical search found zero survivors.",
               "Journal the negative search and restore prospective measurements before expanding the experiment family.")
    supplied_wallet = dict(packet.get("wallet_review") or {})
    wallet = {k: supplied_wallet.get(k) for k in ("version", "observed_ms", "coverage", "queue_policy", "copy_qualified_count")
              if k in supplied_wallet}
    wallet["copy_profit_verified"] = False
    if supplied_wallet.get("coverage", {}).get("unique_chain_addresses"):
        action(2, "validate_wallet_followers_prospectively", "Wallet nominations exist without independently proved copy profitability.",
               "Observe the bounded candidate/control queue under existing provider budgets and measure delayed follower cashflows.")
    return {"version": VERSION, "at_ms": now, "ledger": ledger, "fill_cost_coverage": coverage,
            "paper_learning": paper, "operational_health": operations, "discovery": discovery_summary,
            "wallet_review": wallet, "repair_actions": sorted(actions, key=lambda a: (a["priority"], a["code"])),
            "section_status": packet.get("section_status", {}), "source_deferred": packet.get("source_deferred", False),
            "retired_book_count": len(packet.get("retired_books") or []),
            "economic_profit_verified": False, "funding_allowed": False, "wallet_promotions_authorized": False,
            "population_caveat": "Metrics describe the supplied bounded closed-position subset; they are not whole-wallet economic profit."}
