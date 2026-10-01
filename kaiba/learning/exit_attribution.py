"""Codex TRADE-2: pure descriptive audit; marks are not executable fills.

Consumes an explicit captured snapshot. Does not access the database, call a
provider, reconstruct missing quotes, change a strategy, or submit orders.
"""

import math
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from statistics import mean, median


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value))
        return n if n.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def return_pct(value, base):
    a, b = number(value), number(base)
    return float((a / b - 1) * 100) if a is not None and b is not None and b > 0 else None


def stats(values):
    values = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)))
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": round(mean(values), 6),
        "median": round(median(values), 6),
        "p90": round(values[math.ceil(len(values) * 0.9) - 1], 6),
        "min": round(values[0], 6),
        "max": round(values[-1], 6),
    }


def failure_type(detail, order_error=None):
    # The old watchdog detail cut off the venue error after the CLI banner.
    # A banner saying "confirmation required" is not proof the prompt blocked send.
    text = ((detail or "") + " " + (order_error or "")).lower()
    if "minimum interval" in text:
        return "local_minimum_interval"
    if "insufficientslippage" in text or "40003702" in text:
        return "venue_slippage"
    if "gmgn_body_slippage_out_of_range" in text:
        return "policy_slippage_ceiling"
    if "native price unavailable" in text:
        return "native_price_unavailable"
    if "min_out" in text:
        return "min_out_or_price"
    if "balance" in text or "wallet holds none" in text:
        return "balance_or_inventory"
    if "post /v1/trade/swap fail" in text or "venue answered http" in text:
        return "venue_error_unclassified"
    if "confirmation" in text:
        return "legacy_confirmation_banner_unresolved"
    return "other"


def analyze(snapshot):
    orders = {r["order_id"]: r for r in snapshot["orders"]}
    fills = {r["order_id"]: r for r in snapshot["fills"]}
    links = defaultdict(list)
    events = defaultdict(list)
    trades = defaultdict(list)
    for row in snapshot["links"]:
        links[row["position_id"]].append(row)
    for row in snapshot["events"]:
        events[row["payload"]["position_id"]].append(row)
    for row in snapshot["trades"]:
        trades[row["position_id"]].append(row)
    rows = []
    failures = []
    for p in snapshot["positions"]:
        pid = p["position_id"]
        end = p["closed_ms"] or snapshot["snapshot_ms"]
        pe = sorted(
            (e for e in events[pid] if p["opened_ms"] - 60000 <= e["ts_ms"] <= end + 1000),
            key=lambda e: (e["ts_ms"], e["id"]),
        )
        po = [orders[link["order_id"]] for link in links[pid] if link["order_id"] in orders]
        buys = [o for o in po if o["side"] == "buy" and o["state"] == "filled"]
        sells = [o for o in po if o["side"] == "sell" and o["state"] == "filled"]
        failed = [e for e in pe if e["payload"].get("event") == "exit_failed"]
        ambiguous = [e for e in pe if e["payload"].get("event") == "exit_ambiguous"]
        attempts = [
            e
            for e in pe
            if e["payload"].get("event")
            in ("quote_decision", "exit_submitted", "exit_failed", "exit_ambiguous")
        ]
        full = [e for e in attempts if (number(e["payload"].get("pct")) or 0) >= 100]
        quotes = [
            e
            for e in full
            if (number((e["payload"].get("quote_provenance") or {}).get("price_usd")) or 0) > 0
        ]
        first = full[0] if full else None
        firstq = quotes[0] if quotes else None
        gross = return_pct(p["proceeds_native"], p["cost_native"]) if p["closed_ms"] else None
        row = {
            "position_id": pid,
            "chain": p["chain"],
            "token": p["token"],
            "opened_ms": p["opened_ms"],
            "closed_ms": p["closed_ms"],
            "closed": bool(p["closed_ms"]),
            "reason": p["exit_reason"],
            "cost_native": p["cost_native"],
            "proceeds_native": p["proceeds_native"],
            "gross_native_return_pct": gross,
            "trade_rows": len(trades[pid]),
            "buys": len(buys),
            "sells": len(sells),
            "failed_exit_events": len(failed),
            "ambiguous_exit_events": len(ambiguous),
            "blind_events": sum(e["payload"].get("event") == "protection_blind" for e in pe),
            "first_full_attempt_ms": first["ts_ms"] if first else None,
            "first_full_attempt_to_close_s": (end - first["ts_ms"]) / 1000
            if first and p["closed_ms"]
            else None,
            "open_elapsed_since_attempt_s": (end - first["ts_ms"]) / 1000
            if first and not p["closed_ms"]
            else None,
            "first_full_quote_to_close_s": (end - firstq["ts_ms"]) / 1000
            if firstq and p["closed_ms"]
            else None,
            "simple_single_roundtrip": len(buys) == len(sells) == 1 and bool(p["closed_ms"]),
            "comparison_eligible": False,
            "comparison_exclusion": None,
            "first_mark_return_pct_usd": None,
            "exit_fill_return_pct_usd": None,
            "mark_to_fill_gap_pp": None,
            "first_quote_source": None,
            "first_quote_age_s": None,
            "fill_time_basis": None,
            "submitted_below_stop_pp": None,
            "trade_pnl_difference_pp": None,
        }
        if len(trades[pid]) == 1 and gross is not None:
            row["trade_pnl_difference_pp"] = float(trades[pid][0]["pnl_pct"]) - gross
        for e in failed:
            body = e["payload"]
            oid = body.get("order_id")
            order = orders.get(oid, {})
            failures.append(
                {
                    "position_id": pid,
                    "chain": p["chain"],
                    "closed": row["closed"],
                    "event_ms": e["ts_ms"],
                    "order_id": oid,
                    "type": failure_type(body.get("detail"), order.get("error")),
                    "attempts": body.get("attempts"),
                    "retry_in_ms": body.get("retry_in_ms"),
                    "order_state": order.get("state"),
                    "provider_order_id_present": bool(order.get("provider_order_id")),
                    "tx_hash_present": bool(order.get("tx_hash")),
                }
            )
        if not row["simple_single_roundtrip"]:
            row["comparison_exclusion"] = "open_or_multiple_or_missing_fills"
        elif firstq is None:
            row["comparison_exclusion"] = "missing_full_exit_quote"
        else:
            buyfill = fills.get(buys[0]["order_id"])
            sellfill = fills.get(sells[0]["order_id"])
            if (
                not buyfill
                or not sellfill
                or (number(buyfill.get("price_usd")) or 0) <= 0
                or (number(sellfill.get("price_usd")) or 0) <= 0
            ):
                row["comparison_exclusion"] = "missing_measured_fill_price"
            elif buyfill.get("basis") != "fill_ratio" or sellfill.get("basis") != "fill_ratio":
                row["comparison_exclusion"] = "fill_price_not_measured_ratio"
            elif sellfill["fill_ts_ms"] < firstq["ts_ms"]:
                row["comparison_exclusion"] = "quote_after_recorded_fill"
            elif (firstq["payload"]["quote_provenance"].get("observed_ms") or 0) > firstq["ts_ms"]:
                row["comparison_exclusion"] = "quote_observation_in_future"
            elif int(buyfill["token_atoms"]) != int(p["qty_total"]) or int(sellfill["token_atoms"]) != int(
                p["qty_total"]
            ):
                row["comparison_exclusion"] = "token_quantity_mismatch"
            elif int(buyfill["native_atoms"]) != int(p["cost_native"]) or int(
                sellfill["native_atoms"]
            ) != int(p["proceeds_native"]):
                row["comparison_exclusion"] = "native_ledger_mismatch"
            else:
                q = firstq["payload"]["quote_provenance"]
                entry = buyfill["price_usd"]
                row["comparison_eligible"] = True
                row["first_mark_return_pct_usd"] = return_pct(q["price_usd"], entry)
                row["exit_fill_return_pct_usd"] = return_pct(sellfill["price_usd"], entry)
                row["mark_to_fill_gap_pp"] = (
                    row["first_mark_return_pct_usd"] - row["exit_fill_return_pct_usd"]
                )
                row["first_quote_source"] = q.get("source")
                row["first_quote_age_s"] = (
                    (firstq["ts_ms"] - q["observed_ms"]) / 1000 if q.get("observed_ms") else None
                )
                row["fill_time_basis"] = sellfill.get("fill_ts_basis")
                submitted = next(
                    (
                        e
                        for e in pe
                        if e["payload"].get("event") == "exit_submitted"
                        and e["payload"].get("order_id") == sells[0]["order_id"]
                    ),
                    None,
                )
                if submitted:
                    body = submitted["payload"]
                    stop = body.get("stop_price_usd")
                    mark = body.get("price_usd")
                    if number(stop) is not None and number(mark) is not None:
                        row["submitted_below_stop_pp"] = float(
                            (number(stop) - number(mark)) / number(entry) * 100
                        )
        rows.append(row)
    closed = [r for r in rows if r["closed"]]
    matched = [r for r in closed if r["comparison_eligible"]]

    def group(subset):
        return {
            "n": len(subset),
            "gross_native_return_pct": stats(r["gross_native_return_pct"] for r in subset),
            "wins": sum((r["gross_native_return_pct"] or 0) > 0 for r in subset),
            "first_full_attempt_to_close_s": stats(r["first_full_attempt_to_close_s"] for r in subset),
        }

    summary = {
        "snapshot_ms": snapshot["snapshot_ms"],
        "source_hashes": snapshot["source_hashes"],
        "positions": len(rows),
        "open": len(rows) - len(closed),
        "closed": group(closed),
        "fill_fee_coverage": {
            "fills": len(snapshot["fills"]),
            "with_nonnull_fee": sum(f.get("fee_native") is not None for f in snapshot["fills"]),
        },
        "trades_table_return_pct": stats(t["pnl_pct"] for t in snapshot["trades"]),
        "closed_without_trade": [r["position_id"] for r in closed if r["trade_rows"] == 0],
        "duplicate_trade_positions": [r["position_id"] for r in rows if r["trade_rows"] > 1],
        "max_trade_pnl_difference_pp": max(
            (abs(r["trade_pnl_difference_pp"]) for r in closed if r["trade_pnl_difference_pp"] is not None),
            default=None,
        ),
        "by_chain": {
            chain: group([r for r in closed if r["chain"] == chain])
            for chain in sorted({r["chain"] for r in rows})
        },
        "by_reason": {
            reason: group([r for r in closed if (r["reason"] or "unknown").split(":")[0] == reason])
            for reason in sorted({(r["reason"] or "unknown").split(":")[0] for r in closed})
        },
        "closed_with_recorded_failure": group([r for r in closed if r["failed_exit_events"]]),
        "closed_without_recorded_failure": group([r for r in closed if not r["failed_exit_events"]]),
        "failure_types_closed": dict(Counter(f["type"] for f in failures if f["closed"])),
        "failure_types_open": dict(Counter(f["type"] for f in failures if not f["closed"])),
        "local_interval_backoff_s": stats(
            f["retry_in_ms"] / 1000
            for f in failures
            if f["type"] == "local_minimum_interval" and f["retry_in_ms"]
        ),
        "comparison_exclusions": dict(
            Counter(r["comparison_exclusion"] for r in rows if not r["comparison_eligible"])
        ),
        "matched_single_roundtrips": {
            "n": len(matched),
            "first_mark_return_pct_usd": stats(r["first_mark_return_pct_usd"] for r in matched),
            "exit_fill_return_pct_usd": stats(r["exit_fill_return_pct_usd"] for r in matched),
            "mark_to_fill_gap_pp": stats(r["mark_to_fill_gap_pp"] for r in matched),
            "positive_gap_sum_pp": sum(max(0, r["mark_to_fill_gap_pp"]) for r in matched),
            "first_full_quote_to_close_s": stats(r["first_full_quote_to_close_s"] for r in matched),
            "first_quote_age_s": stats(r["first_quote_age_s"] for r in matched),
            "fill_time_basis": dict(Counter(r["fill_time_basis"] for r in matched)),
            "quote_sources": dict(Counter(r["first_quote_source"] for r in matched)),
        },
        "matched_by_reason": {},
        "matched_by_failure": {},
        "open_positions": [r for r in rows if not r["closed"]],
    }
    for chain, data in summary["by_chain"].items():
        cohort = [r for r in closed if r["chain"] == chain]
        cost = sum(int(r["cost_native"]) for r in cohort)
        proceeds = sum(int(r["proceeds_native"]) for r in cohort)
        data["aggregate_cost_base_units"] = str(cost)
        data["aggregate_proceeds_base_units"] = str(proceeds)
        data["aggregate_realized_base_units"] = str(proceeds - cost)
        data["cost_weighted_gross_return_pct"] = return_pct(proceeds, cost)
    for field, groups in [("reason", "matched_by_reason"), ("failed_exit_events", "matched_by_failure")]:
        keys = sorted({str(r[field] if field == "reason" else bool(r[field])) for r in matched})
        for key in keys:
            subset = [r for r in matched if str(r[field] if field == "reason" else bool(r[field])) == key]
            summary[groups][key] = {
                "n": len(subset),
                "first_mark_return_pct_usd": stats(r["first_mark_return_pct_usd"] for r in subset),
                "exit_fill_return_pct_usd": stats(r["exit_fill_return_pct_usd"] for r in subset),
                "mark_to_fill_gap_pp": stats(r["mark_to_fill_gap_pp"] for r in subset),
                "delay_s": stats(r["first_full_quote_to_close_s"] for r in subset),
            }
    return rows, failures, summary
