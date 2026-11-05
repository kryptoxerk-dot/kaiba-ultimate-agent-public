"""Pure follow-through of fixed wallet identities on combined normalized history.

Inventory is reconstructed before separating time periods. A sale of an old bag is
never a new post-selection trade, and an add to that bag never assumes a zero opening
balance. Results describe covered gross leader cashflows, not net follower profits.
The caller retains fixed manifests, original normalization receipts and known gaps.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from kaiba.core.schemas import EVM_CHAINS, Chain
from kaiba.intelligence.pnl import MATH, ratio
from kaiba.learning.wallet_behavior_study import analyze_wallet

_MONEY_ONLY_FLAGS = {"buy_without_native_amount", "sell_without_native_amount"}


def _ms(value: Any) -> int:
    if isinstance(value, (bool, float)):
        raise ValueError("timestamps must be nonnegative integer milliseconds")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("timestamps must be nonnegative integer milliseconds") from None
    if not parsed.is_finite() or parsed < 0 or parsed != parsed.to_integral_value():
        raise ValueError("timestamps must be nonnegative integer milliseconds")
    return int(parsed)


def _serialize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(v) for v in value]
    return value


def _median(values: Sequence[int]) -> Decimal | None:
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return Decimal(ordered[middle])
    return ratio(ordered[middle - 1] + ordered[middle], 2)


def _gross(records: list[tuple[str, int | Decimal, int | Decimal]]) -> dict:
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
            "top3_positive_tokens": [],
            "top3_positive_token_profit_share": None,
            "leave_top3_positive_tokens_out_gross_profit": None,
        }
    cost = sum(r[1] for r in records)
    proceeds = sum(r[2] for r in records)
    profits = [r[2] - r[1] for r in records]
    returns = [ratio(p, r[1]) for p, r in zip(profits, records, strict=True) if r[1] > 0]
    token_profit: dict[str, int | Decimal] = defaultdict(int)
    for record, profit in zip(records, profits, strict=True):
        token_profit[record[0]] += profit
    positive = sorted(((t, p) for t, p in token_profit.items() if p > 0), key=lambda x: (-x[1], x[0]))
    top3 = {t for t, _ in positive[:3]}
    retained = [r for r in records if r[0] not in top3]
    return {
        "episodes": len(records),
        "cost": str(cost),
        "proceeds": str(proceeds),
        "gross_profit": str(proceeds - cost),
        "size_weighted_gross_roi": ratio(proceeds - cost, cost),
        "mean_episode_gross_roi": ratio(sum(returns, Decimal(0)), len(returns)),
        "wins": sum(p > 0 for p in profits),
        "gross_win_rate": ratio(sum(p > 0 for p in profits), len(records)),
        "top3_positive_tokens": [t for t, _ in positive[:3]],
        "top3_positive_token_profit_share": ratio(
            sum(p for _, p in positive[:3]), sum(p for _, p in positive)
        ),
        "leave_top3_positive_tokens_out_gross_profit": str(sum(r[2] - r[1] for r in retained))
        if retained
        else None,
    }


def _eligible(episode: dict) -> bool:
    return (
        not set(episode["contamination"]) - _MONEY_ONLY_FLAGS
        and episode["detail_evaluated"]
        and not episode["strong_identity_conflict"]
        and not episode["same_timestamp_token_profit_unevaluated"]
        and not episode.get("intersects_unobserved_interval", False)
    )


def _period(episodes: list[dict], axis: str) -> dict:
    structural = [e for e in episodes if _eligible(e)]
    closed = [e for e in structural if e["closed_ms"] is not None]
    money = [e for e in closed if e["money_axis_profit_eligible"]]
    usd = [e for e in closed if e["usd_profit_eligible"]]
    gross_money = _gross(
        [(e["token"], int(e["cost_money_units"]), int(e["proceeds_money_units"])) for e in money]
    )
    gross_usd = _gross(
        [(e["token"], Decimal(e["cost_usd_unadjusted"]), Decimal(e["proceeds_usd_unadjusted"])) for e in usd]
    )
    gross_usd.update(
        {
            "eligible_closed_episodes": len(closed),
            "covered_closed_episodes": len(usd),
            "coverage_fraction": ratio(len(usd), len(closed)),
            "complete_eligible_closed_profit_usd": gross_usd["gross_profit"]
            if closed and len(usd) == len(closed)
            else None,
        }
    )
    native_open = [e for e in structural if e["closed_ms"] is None and not e["contaminated"]]
    usd_open = [
        e
        for e in structural
        if e["closed_ms"] is None
        and e["cost_usd_unadjusted"] is not None
        and e["proceeds_usd_unadjusted"] is not None
    ]
    with_sales = [e for e in structural if e["first_sell_hold_s"] is not None]
    return {
        "counts": {
            "all": len(episodes),
            "closed": sum(e["closed_ms"] is not None for e in episodes),
            "open": sum(e["closed_ms"] is None for e in episodes),
            "contaminated": sum(e["contaminated"] for e in episodes),
            "structurally_eligible_closed": len(closed),
            "money_axis_eligible_closed": len(money),
            "original_usd_eligible_closed": len(usd),
            "unmatched_sell": sum("sell_without_buy" in e["contamination"] for e in episodes),
            "intersects_unobserved_interval": sum(
                e.get("intersects_unobserved_interval", False) for e in episodes
            ),
            "ambiguous_or_conflicting": sum(
                not e["detail_evaluated"]
                or e["same_timestamp_token_profit_unevaluated"]
                or e["strong_identity_conflict"]
                for e in episodes
            ),
        },
        "gross_money_axis": gross_money,
        "gross_native_atomic": gross_money if axis == "native_atomic" else None,
        "gross_usd_known_subset": gross_usd,
        "behavior": {
            "eligible_episodes": len(structural),
            "episodes_with_adds": sum(e["buys"] > 1 for e in structural),
            "episodes_with_multiple_sales": sum(e["sells"] > 1 for e in structural),
            "episodes_with_partial_first_sale": sum(e["first_sell_was_partial"] is True for e in structural),
            "episodes_bought_after_first_sale": sum(e["buy_after_first_sell"] is True for e in structural),
            "first_sale_timing_episodes": len(with_sales),
            "median_first_sale_hold_s": _median([e["first_sell_hold_s"] for e in with_sales]),
            "median_first_trim_hold_s": _median(
                [e["first_trim_hold_s"] for e in structural if e["first_trim_hold_s"] is not None]
            ),
            "full_close_timing_episodes": len(closed),
            "median_full_close_hold_s": _median([e["full_close_hold_s"] for e in closed]),
            "open_money_cashflow_covered_episodes": len(native_open),
            "open_unreturned_capital_money_units": str(
                sum(max(0, int(e["cost_money_units"]) - int(e["proceeds_money_units"])) for e in native_open)
            )
            if native_open
            else None,
            "open_original_usd_cashflow_covered_episodes": len(usd_open),
            "open_unreturned_capital_usd_known_subset": str(
                sum(
                    (
                        max(
                            Decimal(0),
                            Decimal(e["cost_usd_unadjusted"]) - Decimal(e["proceeds_usd_unadjusted"]),
                        )
                        for e in usd_open
                    ),
                    Decimal(0),
                )
            )
            if usd_open
            else None,
        },
        "episode_ids": [e["followthrough_episode_id"] for e in episodes],
    }


def _cashflows_after(episodes: list[dict], cutoff: int) -> dict:
    """Post-cutoff cashflow is not whole-episode PnL or a mark of the old opening bag."""
    rows = [
        r
        for e in episodes
        if _eligible(e)
        for r in e["trades"]
        if r["ts_ms"] > cutoff and r["side"] in {"buy", "sell"}
    ]
    money_known = [r for r in rows if r["amount_money_units"] is not None]
    usd_known = [r for r in rows if r["usd_value_unadjusted"] is not None]
    money_net = sum((1 if r["side"] == "sell" else -1) * int(r["amount_money_units"]) for r in money_known)
    usd_net = sum(
        ((1 if r["side"] == "sell" else -1) * Decimal(r["usd_value_unadjusted"]) for r in usd_known),
        Decimal(0),
    )
    return {
        "observed_swap_rows": len(rows),
        "money_covered_rows": len(money_known),
        "original_usd_covered_rows": len(usd_known),
        "net_cashflow_money_units_known_subset": str(money_net) if money_known else None,
        "net_cashflow_usd_known_subset": str(usd_net) if usd_known else None,
        "meaning": "receipts minus payments after selection; excludes opening inventory value and is not profit",
    }


def analyze_followthrough(
    rows: list[dict],
    *,
    chain: str,
    wallet: str,
    selection_ms: int,
    as_of_ms: int,
    metadata: dict | None = None,
) -> dict:
    """Study a fixed identity using its combined pre/post normalized tape, without I/O.

    ``metadata.unobserved_intervals`` may contain inclusive ``start_ms/end_ms`` gaps,
    optionally scoped by ``token``. Every episode intersecting one is excluded. Retain
    baseline/post-selection query receipts and caps in metadata; a gap cannot be inferred
    simply because the last observed activity is old. No result approves live copying.
    """
    selection, as_of = _ms(selection_ms), _ms(as_of_ms)
    if selection > as_of:
        raise ValueError("selection_ms must not exceed as_of_ms")
    selected_chain = Chain(chain)
    meta = dict(metadata or {})
    gaps = []
    for gap in meta.get("unobserved_intervals", []):
        start, end = _ms(gap["start_ms"]), _ms(gap["end_ms"])
        if start > end:
            raise ValueError("unobserved interval start_ms must not exceed end_ms")
        token = gap.get("token")
        if token is not None:
            if not isinstance(token, str) or not token.strip():
                raise ValueError("unobserved interval token must be a nonempty string or None")
            token = token.strip()
            if selected_chain in EVM_CHAINS:
                token = token.lower()
        gaps.append({"start_ms": start, "end_ms": end, "token": token})
    with localcontext(MATH):
        baseline = analyze_wallet(rows, chain=chain, wallet=wallet, as_of_ms=selection, metadata=meta)
        combined = analyze_wallet(rows, chain=chain, wallet=wallet, as_of_ms=as_of, metadata=meta)

        def tagged(profile: dict, end_ms: int, prefix: str) -> list[dict]:
            out = []
            for original in profile["episodes"]:
                episode = dict(original, followthrough_episode_id=f"{prefix}:{original['episode_id']}")
                end = episode["closed_ms"] if episode["closed_ms"] is not None else end_ms
                episode["intersects_unobserved_interval"] = any(
                    episode["opened_ms"] <= gap["end_ms"]
                    and end >= gap["start_ms"]
                    and (gap["token"] is None or gap["token"] == episode["token"])
                    for gap in gaps
                )
                out.append(episode)
            return out

        before = tagged(baseline, selection, "baseline")
        after = tagged(combined, as_of, "combined")
        buckets: dict[str, list[dict]] = {
            name: []
            for name in (
                "closed_before_selection",
                "new_closed_after_selection",
                "new_open_after_selection",
                "preexisting_closed_after_selection",
                "preexisting_open_as_of",
            )
        }
        for episode in after:
            closed = episode["closed_ms"]
            if closed is not None and closed <= selection:
                category = "closed_before_selection"
            elif episode["opened_ms"] > selection:
                category = "new_closed_after_selection" if closed is not None else "new_open_after_selection"
            else:
                category = (
                    "preexisting_closed_after_selection" if closed is not None else "preexisting_open_as_of"
                )
            episode["selection_category"] = category
            buckets[category].append(episode)
        new = buckets["new_closed_after_selection"] + buckets["new_open_after_selection"]
        overlap = buckets["preexisting_closed_after_selection"] + buckets["preexisting_open_as_of"]
        base_period = _period(before, combined["money_axis"])
        new_period = _period(new, combined["money_axis"])
        prior_receipt = meta.get("baseline", meta.get("pre_selection", {})) or {}
        post_receipt = meta.get("post_selection", {}) or {}
        caps = [receipt.get("row_cap_truncated") for receipt in (meta, prior_receipt, post_receipt)]
        flags = ["partial_watched_history", "net_costs_unavailable", "unseen_transfers_not_ruled_out"]
        if any(value is True for value in caps):
            flags.append("row_cap_truncated")
        missing_query = any(
            receipt.get("query_missing") is True or receipt.get("query_failed") is True
            for receipt in (meta, prior_receipt, post_receipt)
        )
        if missing_query:
            flags.append("query_observations_missing")
        if gaps:
            flags.append("declared_unobserved_intervals")
        frozen_axis = prior_receipt.get("money_axis", meta.get("baseline_money_axis"))
        if frozen_axis == "native":
            frozen_axis = "native_atomic"
        axis_changed = frozen_axis is not None and frozen_axis != combined["money_axis"]
        if axis_changed:
            flags.append("baseline_normalization_axis_changed")
        old_roi = base_period["gross_money_axis"]["size_weighted_gross_roi"]
        new_roi = new_period["gross_money_axis"]["size_weighted_gross_roi"]
        difference = (
            None
            if axis_changed
            or missing_query
            or any(value is True for value in caps)
            or old_roi is None
            or new_roi is None
            else new_roi - old_roi
        )
        patterns = Counter()
        for episode in new + overlap:
            if not _eligible(episode) or not any(r["ts_ms"] > selection for r in episode["trades"]):
                continue
            if episode["buys"] > 1:
                patterns["added_inventory"] += 1
            if episode["first_sell_was_partial"] is True:
                patterns["partial_first_sale"] += 1
            if episode["sells"] > 1:
                patterns["multiple_sales"] += 1
            if episode["buy_after_first_sell"] is True:
                patterns["bought_after_first_sale"] += 1
        return _serialize(
            {
                "schema_version": 1,
                "chain": combined["chain"],
                "wallet": combined["wallet"],
                "selection_ms": selection,
                "as_of_ms": as_of,
                "money_axis": combined["money_axis"],
                "metadata": meta,
                "profitability_status": "UNPROVEN",
                "live_copy_approved": False,
                "evidence_flags": flags,
                "baseline_normalization_axis_changed": axis_changed,
                "coverage": {
                    "baseline": baseline["coverage"],
                    "combined": combined["coverage"],
                    "unobserved_intervals": gaps,
                },
                "baseline": base_period,
                "post_selection_new_entries": new_period,
                "preexisting_inventory_followthrough": _period(overlap, combined["money_axis"]),
                "post_selection_observed_cashflows": _cashflows_after(new + overlap, selection),
                "accounting_breakdown": {
                    name: _period(group, combined["money_axis"]) for name, group in buckets.items()
                },
                "baseline_open_episode_count": sum(e["closed_ms"] is None for e in before),
                "named_observed_patterns": dict(patterns),
                "comparison": {
                    "new_minus_baseline_size_weighted_gross_roi": difference,
                    "basis": "covered gross leader episodes only; new entries exclude pre-selection bags; no follower or net-profit verdict",
                },
                "episodes": after,
                "limitations": [
                    "Selection fixes identity and cutoff; this function does not verify how identities were selected.",
                    "No zero starting inventory is imposed at selection; old bags and their additions remain in overlap accounting.",
                    "Known gaps exclude crossing episodes. Caps and unknown history prevent a complete-wallet profit claim.",
                    "Open cashflow and post-cutoff receipts omit opening inventory valuation and are not unrealized or realized portfolio profit.",
                    "Original USD and integer money-axis coverage are independent; synthetic USDmicro is never native coin profit.",
                    "Fees, taxes, latency, executable impact, failed attempts and follower execution are not established by leader tape.",
                    "The baseline uses supplied combined normalization; preserve the originally frozen receipt to detect basis changes.",
                ],
            }
        )


def summarize_cohort(profiles: list[dict]) -> dict:
    """Aggregate fixed-identity dossiers by chain/axis; no cross-currency native sum."""
    keys = [(p["chain"], p["wallet"]) for p in profiles]
    if len(set(keys)) != len(keys):
        raise ValueError("cohort contains duplicate chain/wallet identities")
    windows = {(p["selection_ms"], p["as_of_ms"]) for p in profiles}
    if len(windows) > 1:
        raise ValueError("cohort profiles must have one fixed selection/as_of window")
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for profile in profiles:
        groups[(profile["chain"], profile["money_axis"])].append(profile)
    with localcontext(MATH):
        summaries = []
        for (chain, axis), members in sorted(groups.items()):
            periods = {}
            for period in ("baseline", "post_selection_new_entries", "preexisting_inventory_followthrough"):
                for currency, field in (
                    ("money_axis", "gross_money_axis"),
                    ("original_usd", "gross_usd_known_subset"),
                ):
                    covered = [
                        p[period][field] for p in members if p[period][field]["gross_profit"] is not None
                    ]
                    # Integer axes remain exact even above Decimal's ratio precision.
                    number = int if currency == "money_axis" else Decimal
                    cost = sum((number(s["cost"]) for s in covered), number(0))
                    profit = sum((number(s["gross_profit"]) for s in covered), number(0))
                    periods[f"{period}:{currency}"] = {
                        "wallets_with_closed_result": len(covered),
                        "closed_covered_episodes": sum(s["episodes"] for s in covered),
                        "gross_profit_known_subset": str(profit) if covered else None,
                        "gross_cost_known_subset": str(cost) if covered else None,
                        "size_weighted_gross_roi_known_subset": ratio(profit, cost) if covered else None,
                        "positive_gross_wallets": sum(Decimal(s["gross_profit"]) > 0 for s in covered),
                    }
            summaries.append(
                {"chain": chain, "money_axis": axis, "wallets": len(members), "periods": periods}
            )
        return _serialize(
            {
                "wallets": len(profiles),
                "window": list(next(iter(windows))) if windows else None,
                "groups": summaries,
                "profitability_status": "UNPROVEN",
                "live_copy_approved": False,
                "basis": "fixed supplied identities; gross covered leader subsets; missing/open wallets never become zero returns",
            }
        )
