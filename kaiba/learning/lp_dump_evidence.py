"""Point-in-time evidence check for the existing after-drop LP paper setup."""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Mapping
from itertools import pairwise
from pathlib import Path

from kaiba.execution.lp_model import dump_setup

HOUR_S = 3_600
MAX_CANDLES = 73  # Existing selector requests a three-day window, including the current hour.
MIN_PRIOR_RUN_X = 3.0
MAX_CAPTURE_BYTES = 200_000


def assess(candles: object, *, as_of_s: int) -> dict:
    """Screen one retained, bound Meteora hourly response at its original decision clock.

    ``timestamp`` is the UTC bucket start in seconds. The caller binds the
    pool, timeframe, and receipt. OHLCV volume remains a total-volume proxy.
    The result gives no order, sizing, or profitability verdict.
    """
    out = {"status": "evidence_unavailable", "reason": None, "closed_candles": 0,
           "incomplete_candles": 0, "volume_basis": "total_ohlcv_volume",
           "financial_execution": False}
    if type(as_of_s) is not int or as_of_s <= 0:
        return {**out, "reason": "decision_clock_unavailable"}
    if not isinstance(candles, list) or len(candles) > MAX_CANDLES:
        return {**out, "reason": "hourly_payload_unavailable"}

    closed = {}
    seen = set()
    for raw in candles:
        t = raw.get("timestamp") if isinstance(raw, Mapping) else None
        if type(t) is not int or t <= 0 or t % HOUR_S or t > as_of_s or t in seen:
            return {**out, "reason": "hourly_clock_invalid_or_duplicate"}
        seen.add(t)
        if t + HOUR_S > as_of_s:
            out["incomplete_candles"] += 1
            continue
        try:
            fields = [raw[k] for k in ("open", "high", "low", "close", "volume")]
            if any(type(v) not in (str, int, float) for v in fields):
                raise ValueError("ohlcv_number_type")
            opening, high, low, close, volume = map(float, fields)
        except (KeyError, TypeError, ValueError, OverflowError):
            return {**out, "reason": "hourly_values_invalid"}
        if (not all(math.isfinite(v) for v in (opening, high, low, close, volume))
                or not 0 < low <= min(opening, close) <= max(opening, close) <= high or volume < 0):
            return {**out, "reason": "hourly_values_invalid"}
        closed[t] = {"high": high, "low": low, "close": close, "volume": volume}

    out["closed_candles"] = len(closed)
    if len(closed) < 6:
        return {**out, "reason": "closed_history_short"}
    clocks = sorted(closed)
    last_expected = (as_of_s // HOUR_S - 1) * HOUR_S
    if clocks[-1] != last_expected or any(b - a != HOUR_S for a, b in pairwise(clocks)):
        return {**out, "reason": "closed_history_gap_or_stale"}

    rows = [closed[t] for t in clocks]
    why, measures = dump_setup(rows, min_run_x=MIN_PRIOR_RUN_X)
    if any(not math.isfinite(value) for value in measures.values()):
        return {**out, "reason": "hourly_values_invalid"}
    out["measures"] = measures
    if why:
        return {**out, "status": "not_candidate", "reason": why}

    peak = max(range(len(rows)), key=lambda i: rows[i]["high"])
    # OHLC cannot order the peak candle's own high and low. Establish the
    # run using only lows from strictly earlier candles.
    prior_low = min((row["low"] for row in rows[:peak]), default=None)
    if prior_low is None or rows[peak]["high"] / prior_low < MIN_PRIOR_RUN_X:
        return {**out, "reason": "pre_peak_run_unproved"}
    out["prior_bar_run_x"] = rows[peak]["high"] / prior_low
    return {**out, "status": "paper_candidate", "reason": "completed_hours_and_prior_run"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--as-of-s", type=int, required=True)
    args = parser.parse_args()
    with args.capture.open("rb") as file:
        raw = file.read(MAX_CAPTURE_BYTES + 1)
    if len(raw) > MAX_CAPTURE_BYTES:
        raise ValueError("capture_size_exceeded")
    print(json.dumps(assess(json.loads(raw)["data"], as_of_s=args.as_of_s), indent=2))
