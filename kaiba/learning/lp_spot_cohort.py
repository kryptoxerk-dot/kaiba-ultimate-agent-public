"""Score predeclared dip and LP windows, retaining missing opportunities."""

from __future__ import annotations

import argparse
import json
import random
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from statistics import fmean
from typing import NamedTuple

from kaiba.learning import lp_spot_pair as pair
from kaiba.learning.dlmm_state import public_key

VERSION = "lp-spot-cohort/1"
PLAN_VERSION = "lp-spot-cohort-plan/1"
DAY_MS = 86_400_000
MAX_WINDOWS = 200
MAX_PLAN_BYTES = 65_536
START_GRACE_MS = 20_000
WINDOW_MS = START_GRACE_MS + pair.HORIZON_MS + pair.MAX_GAP_MS
COST_CASES = ("base", "stress")
WINDOW_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")


class Window(NamedTuple):
    id: str
    pool: str
    scheduled_ms: int


class Observation(NamedTuple):
    window: Window
    status: str
    report: dict | None = None
    mint: str | None = None


def bounded_json(path: Path, limit: int):
    with path.open("rb") as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("input_size_bound")
    return json.loads(raw)


def windows(plan: dict) -> list[Window]:
    declared, entries = plan["declared_ms"], plan["attempts"]
    if (
        plan["version"] != PLAN_VERSION or not pair.positive_int(declared)
        or not isinstance(entries, list) or not 1 <= len(entries) <= MAX_WINDOWS
    ):
        raise ValueError("invalid_plan")
    seen = set()
    result = []
    for row in entries:
        ident, pool, at = row["id"], row["pool"], row["scheduled_ms"]
        if (
            not isinstance(ident, str) or WINDOW_ID.fullmatch(ident) is None or ident in seen
            or not pair.positive_int(at) or at <= declared
        ):
            raise ValueError("invalid_window_identity_or_clock")
        public_key(pool)
        seen.add(ident)
        result.append(Window(ident, pool, at))
    result.sort(key=lambda window: (window.scheduled_ms, window.id))
    last = {}
    for window in result:
        if window.pool in last and window.scheduled_ms < last[window.pool] + WINDOW_MS:
            raise ValueError("overlapping_pool_windows")
        last[window.pool] = window.scheduled_ms
    return result


def inspect(window: Window, root: Path, as_of: int) -> Observation:
    if as_of < window.scheduled_ms + WINDOW_MS:
        return Observation(window, "not_due")
    path = root / (window.id + ".json")
    try:
        if path.is_symlink():
            raise ValueError("symlink_capture")
        capture = bounded_json(path, pair.MAX_BYTES)
        if capture["pool"] != window.pool:
            return Observation(window, "capture_pool_mismatch")
        frames = capture["frames"]
        first = frames[0] if frames else capture.get("screening")
        last = frames[-1] if frames else first
        if first is not None and not (
            window.scheduled_ms <= first["started_ms"] <= window.scheduled_ms + START_GRACE_MS
            and last["received_ms"] <= as_of
        ):
            return Observation(window, "capture_clock_mismatch")
        report = pair.evaluate(capture)
        if report["status"] != "evaluated":
            return Observation(window, report["status"], report)
        first_frame = pair.decode_frame(window.pool, frames[0])
        return Observation(window, "evaluated", report, first_frame.identity[1][0])
    except FileNotFoundError:
        return Observation(window, "missing_capture")
    except (OSError, ValueError, KeyError, TypeError, IndexError, RecursionError, OverflowError):
        return Observation(window, "invalid_capture")


def paired_cases(observation: Observation, ticket: str) -> dict:
    if observation.status != "evaluated" or observation.report is None:
        return {}
    return observation.report["by_ticket"][ticket]


def case_summary(common: list[Observation], ticket: str, cost: str) -> dict:
    clusters = defaultdict(list)
    traded = []
    traded_mints = set()
    rows = []
    lp = 0
    misses = 0
    days = set()
    for obs in common:
        case = paired_cases(obs, ticket)[cost]
        spot = case["spot_net_lamports"]
        lp += case["lp_before_fee_income_lamports"]
        misses += case["lp_status"] == "paid_miss"
        rows.append(spot)
        clusters[obs.mint].append(spot)
        days.add(obs.window.scheduled_ms // DAY_MS)
        if case["spot_status"] == "priced":
            traded.append(spot)
            traded_mints.add(obs.mint)
    means = [fmean(clusters[mint]) for mint in sorted(clusters)]
    lower = None
    if len(traded_mints) >= 10:
        rng = random.Random(20261104)
        draws = sorted(fmean(rng.choices(means, k=len(means))) for _ in range(2000))
        lower = draws[199]
    spot_total = sum(rows)
    return {
        "paired_windows": len(rows), "utc_plan_days": len(days), "distinct_mints": len(clusters),
        "spot_trades": len(traded), "traded_mints": len(traded_mints), "lp_paid_misses": misses,
        "spot_cash_windows": len(rows) - len(traded),
        "spot_net_total_lamports": spot_total if rows else None,
        "spot_mean_per_window_lamports": fmean(rows) if rows else None,
        "spot_equal_mint_mean_lamports": fmean(means) if means else None,
        "spot_lower90_mint_mean_lamports": lower,
        "spot_mean_without_three_best_trades_lamports": (
            (spot_total - sum(sorted(traded)[-3:])) / (len(rows) - 3) if len(traded) > 3 else None
        ),
        "lp_before_fee_income_total_lamports": lp if rows else None,
        "lp_net_fee_hurdle_vs_spot_lamports": max(0, spot_total - lp) if rows else None,
        "lp_net_fee_hurdle_vs_cash_lamports": max(0, -lp) if rows else None,
    }


def screen(summary: dict, due: int) -> str:
    enough = (
        summary["spot_trades"] >= 40 and summary["traded_mints"] >= 10 and summary["utc_plan_days"] >= 7
        and 10 * summary["paired_windows"] >= 9 * due
    )
    if not enough:
        return "pending_coverage"
    if summary["spot_lower90_mint_mean_lamports"] > 0 and summary["spot_mean_without_three_best_trades_lamports"] > 0:
        return "modeled_line_met"
    return "modeled_line_failed"


def summarize_ticket(observations: list[Observation], ticket: str) -> dict:
    due = [obs for obs in observations if obs.status != "not_due"]
    common = []
    priced = Counter({case: 0 for case in COST_CASES})
    for obs in due:
        cases = paired_cases(obs, ticket)
        if not cases:
            continue
        passed = [name for name in COST_CASES if cases[name]["priced_comparison"]]
        priced.update(passed)
        if len(passed) == len(COST_CASES):
            common.append(obs)
    summary = {name: case_summary(common, ticket, name) for name in COST_CASES}
    screens = {name: screen(stats, len(due)) for name, stats in summary.items()}
    joint = (
        "pending_coverage" if "pending_coverage" in screens.values()
        else "modeled_line_failed" if "modeled_line_failed" in screens.values()
        else "modeled_line_met"
    )
    return {
        "due_windows": len(due), "common_priced_pairs": len(common),
        "unpaired_or_unpriced_windows": len(due) - len(common),
        "pairs_by_case": dict(priced), "summary_on_common_pairs": summary,
        "spot_screens_by_case": screens, "spot_joint_screen": joint,
        "lp_screen": "unresolved_fee_income",
    }


def evaluate(plan: dict, root: Path, as_of: int) -> dict:
    enrollment = windows(plan)
    if not pair.positive_int(as_of) or plan["declared_ms"] > as_of:
        raise ValueError("invalid_as_of")
    observations = [inspect(window, root, as_of) for window in enrollment]
    report = []
    programs = set()
    touches = Counter({"sampled_touch": 0, "no_sampled_touch": 0, "unknown": 0})
    for obs in observations:
        touch = "unknown"
        if obs.status == "evaluated":
            touch = "sampled_touch" if obs.report["sampled_active_bin_change"][0] < 0 else "no_sampled_touch"
            programs.add(obs.report["token_transfer_evidence"]["program"])
        if obs.status != "not_due":
            touches[touch] += 1
        report.append({
            "id": obs.window.id, "status": obs.status, "touch": touch,
            "frames_received": obs.report.get("frames_received") if obs.report else None,
            "requests": obs.report.get("requests") if obs.report else None,
            "basis_problem": obs.report.get("basis_problem") if obs.report else None,
            "token_transfer_evidence": obs.report.get("token_transfer_evidence") if obs.report else None,
        })
    mixed = len(programs) > 1
    return {
        "version": VERSION, "as_of_ms": as_of, "scheduled_windows": len(enrollment),
        "status": "mixed_token_programs" if mixed else "evaluated",
        "status_counts": dict(Counter(obs.status for obs in observations)),
        "touch_coverage": dict(touches), "windows": report,
        "by_ticket": {} if mixed else {
            str(ticket): summarize_ticket(observations, str(ticket)) for ticket in pair.TICKETS
        },
        "basis": "sampled_reserve_model_per_window", "financial_execution": False, "promotion_authority": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("capture_directory", type=Path)
    parser.add_argument("--as-of-ms", type=int, default=time.time_ns() // 1_000_000)
    args = parser.parse_args(argv)
    try:
        plan = bounded_json(args.plan, MAX_PLAN_BYTES)
        report = evaluate(plan, args.capture_directory, args.as_of_ms)
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        print(json.dumps({"version": VERSION, "status": "invalid_plan", "financial_execution": False}))
        return 2
    print(json.dumps(report, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
