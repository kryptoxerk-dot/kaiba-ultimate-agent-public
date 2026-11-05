"""Audit saved wallet-lab timing and inventory labels without altering the frozen study.

WALLET-LAB-RECEIPTS-20261029, Codex. No I/O, provider, database or financial access.
The caller supplies the unchanged lab's pure episode/class functions. This diagnostic holds
the retained token population and pre-entry features fixed; it is not a corrected backtest.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable

BEHAVIOR = ("F4_buys", "F4_dip_add", "F4_reentry", "F6_first_sell_s", "F6_first_sell_frac",
            "F6_left_at_end", "F7_hold_s")


def _same(a, b):
    if type(a) in (int, float) and type(b) in (int, float):
        return math.isfinite(a) and math.isfinite(b) and math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-10)
    return a == b


def audit_wallet(rows: list[dict], episodes: list[dict], *, since_s: int, split_s: int, until_s: int,
                 episode_fn: Callable, classes_fn: Callable, quote_assets: set[str], max_episodes: int = 25) -> dict:
    """Return identity-free counts and same-token cutoff diagnostics, never a trade decision."""
    if not all(type(t) is int for t in (since_s, split_s, until_s)) or not since_s < split_s < until_s:
        raise ValueError("invalid_window")
    if not isinstance(rows, list) or len(rows) > 20_000 or not isinstance(episodes, list) or len(episodes) > 100:
        raise ValueError("input_budget_exceeded")
    if type(max_episodes) is not int or not 1 <= max_episodes <= 100:
        raise ValueError("episode_budget_invalid")
    grouped = {}
    for row in rows:
        if not isinstance(row, dict) or type(row.get("timestamp")) is not int or not isinstance(row.get("token"), str):
            raise ValueError("activity_identity_or_clock_invalid")
        if row["token"] not in quote_assets and since_s <= row["timestamp"] <= until_s:
            grouped.setdefault(row["token"], []).append(row)
    for values in grouped.values():
        values.sort(key=lambda r: r["timestamp"])
    first_buys = {token: next((r["timestamp"] for r in values if r.get("event_type") == "buy"), None)
                  for token, values in grouped.items()}
    prefix_tokens = {token for token, ts in first_buys.items() if ts is not None and ts < split_s}
    selected_at_split = set(sorted(prefix_tokens, key=lambda token: (-first_buys[token], token))[:max_episodes])
    original, censored, changed, full_mismatch = [], [], Counter(), Counter()
    late_activity = reopened = invalid_quantity = after_end = 0
    seen = set()
    for ep in episodes:
        if not isinstance(ep, dict) or type(ep.get("t0")) is not int or not isinstance(ep.get("token"), str):
            raise ValueError("episode_identity_or_clock_invalid")
        if ep["token"] in seen or not since_s <= ep["t0"] <= until_s:
            raise ValueError("duplicate_or_out_of_window_episode")
        seen.add(ep["token"])
        if ep.get("F7_hold_s") is not None and (ep.get("F6_left_at_end") or 0) > 0.01:
            reopened += 1
        after_end += sum(r["timestamp"] > until_s for r in rows if r["token"] == ep["token"])
        if ep["t0"] >= split_s:
            continue
        original.append(ep)
        full = [r for r in grouped.get(ep["token"], ()) if r["timestamp"] >= ep["t0"]]
        prefix = [r for r in full if r["timestamp"] < split_s]
        if not prefix or prefix[0].get("event_type") != "buy" or prefix[0]["timestamp"] != ep["t0"]:
            raise ValueError("episode_opening_not_in_activity")
        if len(prefix) != len(full):
            late_activity += 1
        if any(not _positive_quantity(r.get("token_amount")) for r in full):
            invalid_quantity += 1
        # The supplied adapter reads no candles: only these post-entry behavior fields change.
        # F1/F2/F5 remain the original stored values so the diagnostic does not fabricate history.
        reconstructed = episode_fn(full, until_s)
        at_split = episode_fn(prefix, split_s - 1)
        for field in BEHAVIOR:
            if not _same(ep.get(field), reconstructed.get(field)):
                full_mismatch[field] += 1
            if not _same(ep.get(field), at_split.get(field)):
                changed[field] += 1
        censored.append({**ep, **{field: at_split[field] for field in BEHAVIOR}})
    before, after = sorted(classes_fn(original)), sorted(classes_fn(censored))
    retained_tokens = {ep["token"] for ep in original}
    gaps = []
    if full_mismatch:
        gaps.append("stored_behavior_not_reproduced_from_bounded_activity")
    if invalid_quantity:
        gaps.append("invalid_quantity_in_profile_activity")
    if after_end:
        gaps.append("activity_after_study_end_present")
    return {"retained_episodes": len(episodes), "retained_profile_episodes": len(original),
            "profile_episodes_with_test_period_activity": late_activity,
            "changed_behavior_fields": dict(sorted(changed.items())),
            "reconstruction_mismatch_fields": dict(sorted(full_mismatch.items())),
            "invalid_quantity_profile_episodes": invalid_quantity,
            "first_exit_but_remaining_inventory_episodes": reopened,
            "profile_tokens_available_at_split": len(selected_at_split),
            "profile_tokens_omitted_from_retained_population": len(selected_at_split - retained_tokens),
            "original_classes_same_retained_tokens": before,
            "cutoff_classes_same_retained_tokens": after if not gaps else None,
            "class_changed_same_retained_tokens": before != after if not gaps else None,
            "gaps": gaps, "net_pnl": None, "strategy_promotable": False,
            "interpretation": "Timing diagnostic on retained tokens; omitted profile tokens, acquisition/price/cost gaps and frozen selection remain unresolved."}


def _positive_quantity(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return False
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (ValueError, OverflowError):
        return False
