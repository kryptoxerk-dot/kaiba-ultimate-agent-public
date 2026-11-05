"""Pure, descriptive wallet study over supplied tape; never an admission decision.

The caller owns extraction and unit normalization. ``amount_token`` must be integer
inventory units and ``amount_native`` integer native atoms, or integer micro-dollars
when ``metadata.money_axis`` is ``usd_micro``. No USD imputation occurs here. Observed
swap cashflows omit fees, failed transactions and unseen inventory movements, so every
profit is gross, history coverage is partial, and copier profitability stays UNPROVEN.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from kaiba.core.schemas import EVM_CHAINS, Chain, is_quote_asset
from kaiba.intelligence.pnl import DUST_BPS, MATH, Episode, ratio, reconstruct


def _atoms(value: Any) -> int | None:
    """Reject fractional/nonfinite/negative atoms instead of truncating provider units."""
    if value is None or isinstance(value, (bool, float)):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        return None
    return int(number)


def _usd(value: Any) -> Decimal | None:
    if value is None or isinstance(value, (bool, float)):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number >= 0 else None


def _json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    return value


def _median(values: Sequence[int | Decimal]) -> Decimal | None:
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return Decimal(ordered[middle])
    return MATH.divide(Decimal(ordered[middle - 1]) + Decimal(ordered[middle]), Decimal(2))


def _address(value: Any, chain: Chain) -> str:
    text = str(value or "").strip()
    return text.lower() if chain in EVM_CHAINS else text


def _association(
    episodes: list[Episode], rows: list[dict[str, Any]], as_of_ms: int
) -> tuple[list[list[dict[str, Any]]], set[int]]:
    """Associate nonoverlapping timestamp intervals; never reuse a boundary row.

    Without instruction indexes, a same-timestamp close and reopen has no observable
    ordering. Both affected episode details and profitability eligibility are withheld.
    """
    by_token: dict[str, list[int]] = defaultdict(list)
    for index, episode in enumerate(episodes):
        by_token[episode.token].append(index)
    associated: list[list[dict[str, Any]]] = [[] for _ in episodes]
    ambiguous: set[int] = set()
    for row in rows:
        candidates = [
            index
            for index in by_token[row["token"]]
            if episodes[index].opened_ms
            <= row["ts_ms"]
            <= (episodes[index].closed_ms if episodes[index].closed_ms is not None else as_of_ms)
        ]
        if len(candidates) == 1:
            associated[candidates[0]].append(row)
        elif len(candidates) > 1:
            ambiguous.update(candidates)
    return associated, ambiguous


def _detail(episode: Episode, rows: list[dict[str, Any]], *, ambiguous: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "detail_evaluated": not ambiguous,
        "association_issue": "ambiguous_timestamp_boundary" if ambiguous else None,
        "first_sell_ms": None,
        "first_sell_hold_s": None,
        "first_sell_was_partial": None,
        "first_trim_hold_s": None,
        "buy_after_first_sell": None,
        "same_timestamp_order_ambiguous": None,
        "trades": [],
    }
    if ambiguous:
        return result
    by_time: dict[int, set[str]] = defaultdict(set)
    for row in rows:
        by_time[row["ts_ms"]].add(row["side"])
    order_ambiguous = any(len(sides) > 1 for sides in by_time.values())
    result["same_timestamp_order_ambiguous"] = order_ambiguous
    sells = [row for row in rows if row["side"] == "sell"]
    if sells:
        first = sells[0]
        first_ms = first["ts_ms"]
        result["first_sell_ms"] = first_ms
        result["first_sell_hold_s"] = max(0, (first_ms - episode.opened_ms) // 1000)
        buys = [row for row in rows if row["side"] == "buy"]
        after = any(row["ts_ms"] > first_ms for row in buys)
        tied = any(row["ts_ms"] == first_ms for row in buys)
        result["buy_after_first_sell"] = True if after else (None if tied else False)
        if not order_ambiguous and not set(episode.contamination) - {
            "buy_without_native_amount",
            "sell_without_native_amount",
        }:
            inventory = sum(row["amount_token"] for row in buys if row["ts_ms"] < first_ms)
            result["first_sell_was_partial"] = first["amount_token"] < inventory
            if result["first_sell_was_partial"]:
                result["first_trim_hold_s"] = result["first_sell_hold_s"]
    result["trades"] = [
        {
            "id": row.get("id"),
            "ts_ms": row["ts_ms"],
            "side": row["side"],
            "amount_token_units": None if row["amount_token"] is None else str(row["amount_token"]),
            "amount_money_units": None if row["amount_native"] is None else str(row["amount_native"]),
            "usd_value_unadjusted": row["usd_value"],
            "tx": row.get("tx"),
            "source": row.get("source"),
        }
        for row in rows
    ]
    return result


def _profit_metrics(records: list[tuple[str, str, int, int | Decimal, int | Decimal]]) -> dict[str, Any]:
    """Gross result plus concentration by token, on exactly the supplied covered subset."""
    if not records:
        return {
            "episodes": 0,
            "cost": None,
            "proceeds": None,
            "gross_profit": None,
            "size_weighted_gross_roi": None,
            "mean_episode_gross_roi": None,
            "wins": 0,
            "gross_win_rate": None,
            "profitable_utc_days": 0,
            "observed_close_utc_days": 0,
            "gross_profit_by_close_utc_day": {},
            "positive_profit_token_count": 0,
            "top1_positive_token_profit_share": None,
            "top3_positive_token_profit_share": None,
            "top3_positive_tokens": [],
            "leave_top3_positive_tokens_out_gross_profit": None,
            "leave_top3_positive_tokens_out_size_weighted_roi": None,
            "leave_top3_positive_tokens_out_episodes": 0,
        }
    cost = sum(record[3] for record in records)
    proceeds = sum(record[4] for record in records)
    profits = [record[4] - record[3] for record in records]
    returns = [ratio(p, r[3]) for p, r in zip(profits, records, strict=True) if r[3] > 0]
    by_token: dict[str, int | Decimal] = defaultdict(int)
    by_day: dict[str, int | Decimal] = defaultdict(int)
    for record, profit in zip(records, profits, strict=True):
        by_token[record[1]] += profit
        day = datetime.fromtimestamp(record[2] / 1000, UTC).date().isoformat()
        by_day[day] += profit
    positive = sorted(((t, p) for t, p in by_token.items() if p > 0), key=lambda x: (-x[1], x[0]))
    positive_total = sum(p for _, p in positive)
    top3 = {token for token, _ in positive[:3]}
    retained = [record for record in records if record[1] not in top3]
    retained_cost = sum(record[3] for record in retained)
    retained_profit = sum(record[4] - record[3] for record in retained)
    return {
        "episodes": len(records),
        "cost": str(cost),
        "proceeds": str(proceeds),
        "gross_profit": str(proceeds - cost),
        "size_weighted_gross_roi": ratio(proceeds - cost, cost),
        "mean_episode_gross_roi": ratio(sum(returns, Decimal(0)), len(returns)),
        "wins": sum(p > 0 for p in profits),
        "gross_win_rate": ratio(sum(p > 0 for p in profits), len(records)),
        "profitable_utc_days": sum(p > 0 for p in by_day.values()),
        "observed_close_utc_days": len(by_day),
        "gross_profit_by_close_utc_day": {day: str(p) for day, p in sorted(by_day.items())},
        "positive_profit_token_count": len(positive),
        "top1_positive_token_profit_share": ratio(sum(p for _, p in positive[:1]), positive_total),
        "top3_positive_token_profit_share": ratio(sum(p for _, p in positive[:3]), positive_total),
        "top3_positive_tokens": [token for token, _ in positive[:3]],
        "leave_top3_positive_tokens_out_gross_profit": str(retained_profit) if retained else None,
        "leave_top3_positive_tokens_out_size_weighted_roi": ratio(retained_profit, retained_cost),
        "leave_top3_positive_tokens_out_episodes": len(retained),
    }


def analyze_wallet(
    rows: list[dict], *, chain: str, wallet: str, as_of_ms: int, metadata: dict | None = None
) -> dict:
    """Describe one wallet's observed inventory episodes without I/O or side effects.

    This accepts caller-normalized rows, not a provider response with unknown UI units.
    ``metadata.coverage.money_axis`` is also accepted for grade normalizer receipts.
    Original USD coverage and every caller window/normalization flag are propagated;
    fees, unseen transfers, history completeness and copier net returns remain unknown.
    """
    selected_chain = Chain(chain)
    selected_wallet = _address(wallet, selected_chain)
    cutoff = _atoms(as_of_ms)
    if cutoff is None:
        raise ValueError("as_of_ms must be a nonnegative integer timestamp")
    meta = dict(metadata or {})
    axis = meta.get("money_axis", (meta.get("coverage") or {}).get("money_axis", "native_atomic"))
    if axis == "native":
        axis = "native_atomic"
    if axis not in {"native_atomic", "usd_micro"}:
        raise ValueError("money_axis must be native_atomic/native or usd_micro")
    excluded: Counter[str] = Counter()
    invalid: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    scoped: list[dict[str, Any]] = []
    for raw in rows:
        if str(raw.get("chain") or "") != selected_chain.value:
            excluded["other_or_missing_chain"] += 1
            continue
        if _address(raw.get("wallet"), selected_chain) != selected_wallet:
            excluded["other_or_missing_wallet"] += 1
            continue
        ts = _atoms(raw.get("ts_ms"))
        if ts is None:
            excluded["invalid_timestamp"] += 1
            continue
        if ts > cutoff:
            excluded["after_as_of"] += 1
            continue
        token = _address(raw.get("token"), selected_chain)
        if not token:
            excluded["missing_token"] += 1
            continue
        if is_quote_asset(selected_chain, token):
            excluded["quote_asset"] += 1
            continue
        row = dict(raw, token=token, ts_ms=ts, side=str(raw.get("side") or "").strip().lower())
        for field in ("amount_token", "amount_native"):
            value = _atoms(raw.get(field))
            if field == "amount_token" and value == 0 and row["side"] in {"buy", "sell"}:
                value = None
            if value is None:
                (missing if raw.get(field) is None or raw.get(field) == "" else invalid)[field] += 1
            row[field] = value
        value_usd = _usd(raw.get("usd_value"))
        if value_usd is None:
            (missing if raw.get("usd_value") is None or raw.get("usd_value") == "" else invalid)[
                "usd_value"
            ] += 1
        # An explicitly imputed USD amount is not original known-USD evidence.
        if raw.get("usd_value_imputed") or raw.get("usd_imputed"):
            value_usd = None
            invalid["imputed_usd_not_original_evidence"] += 1
        row["usd_value"] = value_usd
        scoped.append(row)
    scoped.sort(key=lambda row: row["ts_ms"])  # preserve supplied order on timestamp ties
    seen: dict[tuple, dict] = {}
    kept: list[dict] = []
    duplicates = conflicts = rows_without_tx = 0
    conflict_tokens: set[str] = set()
    tx_legs: dict[tuple, set[int]] = defaultdict(set)
    for row in scoped:
        tx = str(row.get("tx") or "").strip()
        if not tx:
            rows_without_tx += 1
        qty = row["amount_token"]
        strong_key = (selected_chain.value, tx, selected_wallet, row["token"], row["side"], qty)
        if tx and qty is not None:
            tx_legs[strong_key[:-1]].add(qty)
            previous = seen.get(strong_key)
            if previous is not None:
                if (previous["amount_native"], previous["usd_value"], previous["ts_ms"]) == (
                    row["amount_native"],
                    row["usd_value"],
                    row["ts_ms"],
                ):
                    duplicates += 1
                    continue
                conflicts += 1
                conflict_tokens.add(row["token"])
            else:
                seen[strong_key] = row
        kept.append(row)
    with localcontext(MATH):
        episodes = reconstruct(kept, as_of_ms=cutoff)
        associated, ambiguous = _association(episodes, kept, cutoff)
        token_timestamp_sides: dict[tuple[str, int], set[str]] = defaultdict(set)
        for row in kept:
            token_timestamp_sides[(row["token"], row["ts_ms"])].add(row["side"])
        order_ambiguous_tokens = {
            token for (token, _), sides in token_timestamp_sides.items() if len(sides) > 1
        }
        details = [
            _detail(e, r, ambiguous=i in ambiguous)
            for i, (e, r) in enumerate(zip(episodes, associated, strict=True))
        ]
        clean_indices = [
            i
            for i, e in enumerate(episodes)
            if not set(e.contamination) - {"buy_without_native_amount", "sell_without_native_amount"}
            and i not in ambiguous
            and e.token not in conflict_tokens
            and e.token not in order_ambiguous_tokens
        ]
        structural_closed_indices = [i for i in clean_indices if episodes[i].closed]
        native_clean_indices = [i for i in clean_indices if not episodes[i].contaminated]
        closed_indices = [i for i in native_clean_indices if episodes[i].closed]
        usd_indices = [
            i
            for i in structural_closed_indices
            if episodes[i].cost_usd is not None and episodes[i].proceeds_usd is not None
        ]
        money_records = [
            (
                str(i),
                episodes[i].token,
                episodes[i].closed_ms,
                episodes[i].cost_native,
                episodes[i].proceeds_native,
            )
            for i in closed_indices
        ]
        usd_records = [
            (str(i), episodes[i].token, episodes[i].closed_ms, episodes[i].cost_usd, episodes[i].proceeds_usd)
            for i in usd_indices
        ]
        gross_money = _profit_metrics(money_records)
        gross_usd = _profit_metrics(usd_records)
        gross_usd["covered_closed_episodes"] = len(usd_indices)
        gross_usd["eligible_closed_episodes"] = len(structural_closed_indices)
        gross_usd["coverage_fraction"] = ratio(len(usd_indices), len(structural_closed_indices))
        gross_usd["complete_closed_profit_usd"] = (
            gross_usd["gross_profit"]
            if structural_closed_indices and len(usd_indices) == len(structural_closed_indices)
            else None
        )
        open_clean = [episodes[i] for i in native_clean_indices if not episodes[i].closed]
        open_usd_clean = [
            episodes[i]
            for i in clean_indices
            if not episodes[i].closed
            and episodes[i].cost_usd is not None
            and episodes[i].proceeds_usd is not None
        ]
        clean_with_buys = [i for i in clean_indices if episodes[i].buys]
        clean_with_sells = [i for i in clean_indices if episodes[i].sells]
        after_evaluated = [i for i in clean_with_sells if details[i]["buy_after_first_sell"] is not None]
        buy_usd = [r["usd_value"] for r in kept if r["side"] == "buy" and r["usd_value"] is not None]
        observed_token_timings: list[dict[str, Any]] = []
        by_token: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in kept:
            by_token[row["token"]].append(row)
        for token, token_rows in sorted(by_token.items()):
            buys = [r for r in token_rows if r["side"] == "buy"]
            if not buys:
                continue
            first_buy_ms = buys[0]["ts_ms"]
            sells_after = [r for r in token_rows if r["side"] == "sell" and r["ts_ms"] >= first_buy_ms]
            first_sell_ms = sells_after[0]["ts_ms"] if sells_after else None
            observed_token_timings.append(
                {
                    "token": token,
                    "first_observed_buy_ms": first_buy_ms,
                    "first_observed_sell_after_buy_ms": first_sell_ms,
                    "observed_first_buy_to_first_sell_s": None
                    if first_sell_ms is None
                    else (first_sell_ms - first_buy_ms) // 1000,
                }
            )
        behavior = {
            "eligible_episodes": len(clean_indices),
            "episodes_with_buys": len(clean_with_buys),
            "episodes_with_sells": len(clean_with_sells),
            "episodes_with_adds": sum(episodes[i].buys > 1 for i in clean_with_buys),
            "adds_episode_share": ratio(
                sum(episodes[i].buys > 1 for i in clean_with_buys), len(clean_with_buys)
            ),
            "episodes_with_multiple_sells": sum(episodes[i].sells > 1 for i in clean_with_sells),
            "multiple_sell_episode_share": ratio(
                sum(episodes[i].sells > 1 for i in clean_with_sells), len(clean_with_sells)
            ),
            "buy_after_first_sell_evaluated_episodes": len(after_evaluated),
            "buy_after_first_sell_episodes": sum(details[i]["buy_after_first_sell"] for i in after_evaluated),
            "buy_after_first_sell_share": ratio(
                sum(details[i]["buy_after_first_sell"] for i in after_evaluated), len(after_evaluated)
            ),
            "median_first_sell_hold_s": _median([details[i]["first_sell_hold_s"] for i in clean_with_sells]),
            "median_first_trim_hold_s": _median(
                [
                    details[i]["first_trim_hold_s"]
                    for i in clean_with_sells
                    if details[i]["first_trim_hold_s"] is not None
                ]
            ),
            "median_full_close_hold_s": _median([episodes[i].hold_s for i in structural_closed_indices]),
            "median_observed_buy_usd_unadjusted": _median(buy_usd),
            "buy_usd_coverage_rows": len(buy_usd),
            "buy_rows": sum(r["side"] == "buy" for r in kept),
            "open_unreturned_capital_money_units": str(
                sum(max(0, e.cost_native - e.proceeds_native) for e in open_clean)
            ),
            "open_observed_net_cashflow_money_units": str(
                sum(e.proceeds_native - e.cost_native for e in open_clean)
            ),
            "open_cashflow_covered_episodes": len(open_clean),
            "observed_buy_events": sum(r["side"] == "buy" for r in kept),
            "observed_sell_events": sum(r["side"] == "sell" for r in kept),
            "observed_token_first_buy_first_sell_timings": observed_token_timings,
            "median_observed_first_buy_to_first_sell_s": _median(
                [
                    timing["observed_first_buy_to_first_sell_s"]
                    for timing in observed_token_timings
                    if timing["observed_first_buy_to_first_sell_s"] is not None
                ]
            ),
            "open_complete_usd_cashflow_episodes": len(open_usd_clean),
            "open_unreturned_capital_usd_known_subset": str(
                sum(
                    (max(Decimal(0), e.cost_usd - e.proceeds_usd) for e in open_usd_clean),
                    Decimal(0),
                )
            )
            if open_usd_clean
            else None,
        }
        episode_rows = []
        for i, episode in enumerate(episodes):
            row = {
                "episode_id": str(i),
                "token": episode.token,
                "opened_ms": episode.opened_ms,
                "closed_ms": episode.closed_ms,
                "state": "closed" if episode.closed else "open",
                "buys": episode.buys,
                "sells": episode.sells,
                "contaminated": episode.contaminated,
                "contamination": episode.contamination,
                "profit_eligible": i in closed_indices or i in usd_indices,
                "money_axis_profit_eligible": i in closed_indices,
                "usd_profit_eligible": i in usd_indices,
                "strong_identity_conflict": episode.token in conflict_tokens,
                "same_timestamp_token_profit_unevaluated": episode.token in order_ambiguous_tokens,
                "cost_money_units": str(episode.cost_native),
                "proceeds_money_units": str(episode.proceeds_native),
                "gross_closed_profit_money_units": str(episode.realized_pnl_native)
                if i in closed_indices
                else None,
                "gross_closed_roi_money_axis": episode.roi if i in closed_indices else None,
                "cost_usd_unadjusted": episode.cost_usd,
                "proceeds_usd_unadjusted": episode.proceeds_usd,
                "gross_closed_profit_usd": episode.realized_pnl_usd if i in usd_indices else None,
                "qty_bought_units": str(episode.qty_bought),
                "qty_sold_units": str(episode.qty_sold),
                "leftover_qty_units": str(episode.leftover_qty),
                "full_close_hold_s": episode.hold_s if episode.closed else None,
                "observed_open_age_s": episode.hold_s if not episode.closed else None,
                **details[i],
            }
            episode_rows.append(row)
        result = {
            "schema_version": 1,
            "chain": selected_chain.value,
            "wallet": selected_wallet,
            "as_of_ms": cutoff,
            "money_axis": axis,
            "inventory_close_dust_bps": DUST_BPS,
            "metadata": meta,
            "profitability_status": "UNPROVEN",
            "live_copy_approved": False,
            "coverage": {
                "input_rows": len(rows),
                "scoped_rows_before_dedup": len(scoped),
                "included_rows": len(kept),
                "excluded_rows_by_reason": dict(excluded),
                "missing_fields_rows": dict(missing),
                "invalid_fields_rows": dict(invalid),
                "rows_dropped_strong_identity_duplicate": duplicates,
                "strong_identity_conflicting_rows_retained": conflicts,
                "tx_token_side_groups_with_distinct_quantities": sum(len(q) > 1 for q in tx_legs.values()),
                "rows_without_tx_identity": rows_without_tx,
                "partial_history": True,
                "fees_coverage": "unknown",
                "failed_transaction_costs_coverage": "unknown",
                "unobserved_transfers_coverage": "unknown",
                "original_usd_not_imputed": True,
                "same_timestamp_boundary_ambiguous_episodes": len(ambiguous),
                "strong_identity_conflict_tokens": sorted(conflict_tokens),
                "same_timestamp_order_ambiguous_tokens": sorted(order_ambiguous_tokens),
            },
            "episode_counts": {
                "all": len(episodes),
                "closed": sum(e.closed for e in episodes),
                "open": sum(not e.closed for e in episodes),
                "contaminated": sum(e.contaminated for e in episodes),
                "eligible_clean_closed": len(closed_indices),
                "structurally_eligible_closed": len(structural_closed_indices),
                "known_usd_clean_closed": len(usd_indices),
                "unmatched_sell_episodes": sum("sell_without_buy" in e.contamination for e in episodes),
            },
            "gross_money_axis": gross_money,
            "gross_native_atomic": gross_money if axis == "native_atomic" else None,
            "gross_usd_known_subset": gross_usd,
            "behavior": behavior,
            "episodes": episode_rows,
            "limitations": [
                "Gross observed swap cashflows exclude fees and failed-transaction costs; net profit is unavailable.",
                "Watched-token tape cannot establish complete wallet history or rule out unseen transfers and losses.",
                "Open unreturned capital is observed cashflow, not an unrealized loss or current marked value.",
                "USD totals cover only original known-USD closed episodes; coverage and imputation receipts are separate.",
                "No transaction leg index exists: exact identity dedup is a stated heuristic and may collapse identical legs.",
                "Same-timestamp close/reopen boundaries are unevaluated; no row is assigned to two episode examples.",
                "Inventory closes use the existing reconstruction dust tolerance; a close need not mean exactly zero atoms.",
                "Behavior describes an observed address, not human identity, skill or achievable follower returns.",
            ],
        }
        return _json(result)
