"""Evaluate retained Solana route quote pairs without collecting or placing orders."""
from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import NamedTuple

VERSION = "sol-route-cost/1"
H4_SIZE = 50_000_000
SIZES = (H4_SIZE, 100_000_000, 300_000_000)
MAX_ATTEMPTS = 200
MAX_PAIR_MS = 2_000
DAY_MS = 86_400_000
MAX_FILE_BYTES = 512 * 1024
SHA = re.compile(r"[0-9a-f]{64}")


class Pair(NamedTuple):
    token: str
    observed_ms: int
    gmgn_cost_lamports: int
    direct_cost_lamports: int


def _integer(value: object, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise ValueError("invalid_integer")
    return value


def _hash(value: object) -> str:
    if not isinstance(value, str) or SHA.fullmatch(value) is None:
        raise ValueError("invalid_source_identity")
    return value


def _leg_pair(route: dict, *, token: str, size: int, attempt: int, end: int) -> tuple[int, int, int, str, str]:
    if route["basis"] not in ("provider_quote", "curve_model") or route["token_sha256"] != token:
        raise ValueError("route_identity_mismatch")
    buy, sell = _integer(route["buy_ms"], 1), _integer(route["sell_ms"], 1)
    if not attempt <= buy <= sell <= end:
        raise ValueError("route_clock_mismatch")
    if (_integer(route["buy_lamports"], 1) != size or
            _integer(route["bought_atoms"], 1) != _integer(route["sold_atoms"], 1)):
        raise ValueError("route_size_mismatch")
    external = _integer(route["outside_cost_lamports"])
    cost = size + external - _integer(route["sell_lamports"], 1)
    return cost, buy, sell, route["basis"], _hash(route["source_sha256"])


def _summary(pairs: list[Pair], *, size: int, evaluate_h4: bool) -> dict:
    groups: dict[str, list[float]] = defaultdict(list)
    savings = []
    for pair in pairs:
        saving = (pair.gmgn_cost_lamports - pair.direct_cost_lamports) * 10_000 / size
        groups[pair.token].append(saving)
        savings.append(saving)
    days = len({pair.observed_ms // DAY_MS for pair in pairs})
    point = median(savings) if savings else None
    lower = None
    verdict = "PENDING" if evaluate_h4 else "DIAGNOSTIC"
    enough = len(pairs) >= 40 and days >= 3 and len(groups) >= 2
    if evaluate_h4 and enough:
        rng = random.Random(20261104)
        clusters = [groups[token] for token in sorted(groups)]
        values = sorted(median([v for group in rng.choices(clusters, k=len(clusters)) for v in group])
                        for _ in range(2_000))
        lower = values[199]
        verdict = "PASS_QUOTE_COST_LINE" if point >= 150 and lower > 0 else "FAIL_QUOTE_COST_LINE"
    return {"pairs": len(pairs), "utc_days": days, "distinct_tokens": len(groups),
            "gmgn_median_cost_bps": round(median(pair.gmgn_cost_lamports for pair in pairs) * 10_000 / size, 4)
            if pairs else None,
            "direct_median_cost_bps": round(median(pair.direct_cost_lamports for pair in pairs) * 10_000 / size, 4)
            if pairs else None,
            "median_saving_bps": round(point, 4) if point is not None else None,
            "one_sided_90_lower_bps": round(lower, 4) if lower is not None else None,
            "h4_verdict": verdict}


def evaluate(capture: dict) -> dict:
    if capture["version"] != VERSION:
        raise ValueError("unsupported_capture_version")
    start, end = _integer(capture["registered_ms"], 1), _integer(capture["as_of_ms"], 1)
    attempts = capture["attempts"]
    if start > end or not isinstance(attempts, list) or len(attempts) > MAX_ATTEMPTS:
        raise ValueError("invalid_study_scope")
    counts = {size: Counter(attempted=0, unpaired=0, missing_gmgn=0, missing_direct=0) for size in SIZES}
    quotes = {size: [] for size in SIZES}
    models = {size: [] for size in SIZES}
    seen_attempts = set()
    seen_sources = set()
    for row in attempts:
        size = _integer(row["size_lamports"], 1)
        token, at = _hash(row["token_sha256"]), _integer(row["attempt_ms"], 1)
        identity = (size, token, at)
        if size not in counts or not start <= at <= end or identity in seen_attempts:
            raise ValueError("attempt_scope_mismatch")
        seen_attempts.add(identity)
        counter = counts[size]
        counter["attempted"] += 1
        gmgn, direct = row["gmgn"], row["direct"]
        if gmgn is None or direct is None:
            counter["missing_gmgn"] += gmgn is None
            counter["missing_direct"] += direct is None
            counter["unpaired"] += 1
            continue
        g_cost, g_buy, g_sell, g_basis, g_source = _leg_pair(gmgn, token=token, size=size, attempt=at, end=end)
        d_cost, d_buy, d_sell, d_basis, d_source = _leg_pair(direct, token=token, size=size, attempt=at, end=end)
        sources = {(size, g_source), (size, d_source)}
        if g_basis != "provider_quote" or len(sources) != 2 or seen_sources & sources:
            raise ValueError("paired_source_mismatch")
        seen_sources.update(sources)
        if max(g_sell, d_sell) - min(g_buy, d_buy) > MAX_PAIR_MS:
            raise ValueError("pair_skew_exceeded")
        pairs = quotes if d_basis == "provider_quote" else models
        pairs[size].append(Pair(token, min(g_buy, d_buy), g_cost, d_cost))
    return {"version": VERSION, "registered_ms": start, "as_of_ms": end, "financial_execution": False,
            "promotion_authority": False, "funded_pnl_verified": False,
            "by_size": {str(size): {"coverage": dict(counts[size]),
                                    "paired_quotes": _summary(quotes[size], size=size, evaluate_h4=size == H4_SIZE),
                                    "curve_model_pairs": _summary(models[size], size=size, evaluate_h4=False)}
                        for size in SIZES}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    args = parser.parse_args(argv)
    try:
        with args.capture.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("capture_too_large")
        report = evaluate(json.loads(raw))
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        print(json.dumps({"version": VERSION, "status": "invalid_capture", "financial_execution": False}))
        return 2
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
