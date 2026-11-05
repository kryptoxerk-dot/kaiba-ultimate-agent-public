"""Bounded GMGN quote-only experiment. All output remains paper evidence."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from statistics import mean

from kaiba.core.config import get_risk, get_settings
from kaiba.core.limiter import Priority, limits_for
from kaiba.core.schemas import Chain, EvidenceBasis
from kaiba.learning.autolearn_io import GuardedReader, SourceDeferred
from kaiba.providers import gmgn_cli

SOL = "So11111111111111111111111111111111111111112"
SIZES = (100_000_000, 300_000_000)
DAY = 86_400_000
DAILY_ATTEMPTS = 18
MAX_ROWS = 180
ENTRY_WINDOW = 60_000
TIMED_EXIT = 180_000
AFTER_CLOSE = 45_000
MIN_LATE_EXIT = 300_000
EXIT_WINDOW = 45_000
MAX_FOLLOW = 1_800_000
MIN_PAIRS = 40
DISK_FLOOR = 85
VERSION = "gmgn-forward-quotes/1"
MINT = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def utc_ms() -> int:
    return time.time_ns() // 1_000_000


def uint(value) -> int:
    if type(value) is int and 0 <= value < 2**64:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value) and int(value) < 2**64:
        return int(value)
    raise ValueError("invalid quote integer")


def bind_quote(result, input_mint: str, output_mint: str, amount: int, start: int, end: int) -> dict:
    receipt = result.receipt
    if not result or receipt.basis is not EvidenceBasis.PROVIDER_REPORTED:
        return {"status": "unavailable"}
    try:
        data = result.data
        tx = data["tx"]
        route = tx["quote"]
        out, minimum = uint(data["output_amount"]), uint(data["min_output_amount"])
        if (
            receipt.provider != "gmgn" or receipt.endpoint != "quote"
            or not receipt.request_digest or receipt.response_digest != gmgn_cli.digest(data)
            or not start <= receipt.observed_at_ms <= end <= start + 8_000
            or (data["input_token"], data["output_token"], uint(data["input_amount"])) != (
                input_mint, output_mint, amount)
            or (route["inputMint"], route["outputMint"], uint(route["inAmount"])) != (
                input_mint, output_mint, amount)
            or (uint(route["outAmount"]), uint(route["otherAmountThreshold"])) != (out, minimum)
            or route["swapMode"] != "ExactIn" or uint(route["slippageBps"]) != 100
            or not 0 < minimum <= out
        ):
            return {"status": "identity_mismatch"}
        # Retain unclassified vendor fields independently. Their names do not establish
        # whether the cost is already in the quote, held as rent, or separately charged.
        extra = {}
        for key, raw in (
            ("sol_cost", tx.get("sol_cost")),
            ("platform_fee", route.get("platformFee")),
            ("priority_fee", (tx.get("raw_tx") or {}).get("prioritizationFeeLamports")),
        ):
            extra[key] = uint(raw) if raw is not None else None
        return {
            "status": "quoted", "input": amount, "output": out, "minimum": minimum,
            "receipt": receipt.model_dump(mode="json", exclude={"note"}), **extra,
        }
    except (KeyError, TypeError, ValueError, AttributeError):
        return {"status": "invalid_payload"}


def due_stage(row: dict, at: int, closed: int | None) -> tuple[str, int] | None:
    opened = row["opened"]
    stages = [("buy", opened, ENTRY_WINDOW), ("timed", opened + TIMED_EXIT, EXIT_WINDOW)]
    if closed is not None and opened <= closed:
        stages.append(("after_close", max(opened + MIN_LATE_EXIT, closed + AFTER_CLOSE), EXIT_WINDOW))
    if at >= opened + MAX_FOLLOW:
        return None
    for stage, due, window in stages:
        if stage not in row["quotes"] and due <= at < due + window:
            return stage, due + window
    return None


def preflight(reader: GuardedReader, now: int) -> str | None:
    if reader.q("SELECT 1 FROM orders INDEXED BY idx_orders_state WHERE state IN "
                "('submitting','submitted','unknown','partial') AND chain='sol' LIMIT 1"):
        return "unresolved_order"
    if reader.q("SELECT 1 FROM provider_family_bans WHERE provider='gmgn' "
                "AND family IN ('quote','trade') AND banned_until_ms>? LIMIT 1", (now,)):
        return "provider_cooldown"
    return None


def cycle(
    state: dict, reader: GuardedReader, wallet: str, save: Callable[[], None],
    *, clock: Callable[[], int] = utc_ms, quote: Callable = gmgn_cli.order_quote,
) -> str:
    at = clock()
    day = at // DAY
    if state["day"] != day:
        if state["day"] > day:
            return "clock_reversed"
        state.update(day=day, attempts=0)
    if state["attempts"] > DAILY_ATTEMPTS - len(SIZES):
        return "daily_quote_cap"
    reason = preflight(reader, at)
    if reason:
        return reason
    rows = state["rows"]
    active = rows[-1] if rows and at < rows[-1]["opened"] + MAX_FOLLOW else None
    closed = None
    if active is not None:
        found = reader.q("SELECT closed_ms FROM positions WHERE position_id=? LIMIT 1", (active["id"],))
        if not found:
            return "position_evidence_missing"
        closed = found[0][0]
    else:
        if len(rows) >= MAX_ROWS:
            return "study_capacity_reached"
        # Capture the first fresh eligible position. The query stays on the existing close-time index.
        eligible = reader.rows(
            "SELECT position_id,token,opened_ms,closed_ms FROM positions INDEXED BY idx_positions_open "
            "WHERE (closed_ms IS NULL OR closed_ms>=?) AND opened_ms BETWEEN ? AND ? "
            "AND lane='sm-trenches' AND chain='sol' AND mode IN ('live','shadow') "
            "ORDER BY opened_ms ASC,position_id ASC LIMIT 8", (at - ENTRY_WINDOW, at - ENTRY_WINDOW, at))
        used = {row["mint"] for row in rows}
        target = next((r for r in eligible if r["token"] not in used and r["token"] != SOL
                       and MINT.fullmatch(r["token"])), None)
        if target is None:
            return "no_fresh_position"
        active = {"id": target["position_id"], "mint": target["token"],
                  "opened": target["opened_ms"], "quotes": {}}
        closed = target["closed_ms"]
        rows.append(active)
    pending = due_stage(active, at, closed)
    if pending is None:
        return "waiting_for_checkpoint"
    stage, expires = pending
    if stage == "after_close":
        active["closed"] = closed
    bought = active["quotes"].get("buy", {})
    legs = active["quotes"][stage] = {}
    for size in SIZES:
        name = str(size)
        im, om, qty = SOL, active["mint"], size
        if stage != "buy":
            buy = bought.get(name, {})
            if buy.get("status") != "quoted":
                legs[name] = {"status": "buy_missing"}
                continue
            im, om, qty = active["mint"], SOL, buy["minimum"]
        before = clock()
        if before + 8_000 >= expires or before // DAY != day:
            legs[name] = {"status": "checkpoint_expired"}
            break
        if reason := preflight(reader, before):
            legs[name] = {"status": reason}
            break
        # Commit the debit before touching the provider. A crash can lose an observation,
        # and it can never cause an automatic retry of that call.
        legs[name] = {"status": "reserved"}
        state["attempts"] += 1
        save()
        result = quote(
            input_token=im, output_token=om, amount=qty, from_address=wallet, chain=Chain.SOL,
            slippage_bps=100, priority=Priority.RESEARCH, timeout_s=5, wait_for_slot_s=1.5,
        )
        legs[name] = bind_quote(result, im, om, qty, before, clock())
        save()
        if legs[name]["status"] != "quoted":
            break
    return stage


def stress_return(size: int, buy: dict, sell: dict) -> float | None:
    """Charge all reported SOL overhead and another 100 bp per leg as a scenario."""
    if buy.get("status") != "quoted" or sell.get("status") != "quoted":
        return None
    if (buy["minimum"] != sell["input"] or buy["sol_cost"] is None or sell["sol_cost"] is None
            or buy["sol_cost"] < size or sell["sol_cost"] == 0
            or buy["receipt"]["observed_at_ms"] >= sell["receipt"]["observed_at_ms"]):
        return None
    cost = buy["sol_cost"] + sell["sol_cost"]
    reserve = (size + sell["minimum"] + 99) // 100
    return (sell["minimum"] - cost - reserve) / size


def bootstrap_lower(values: list[float], alpha: float, seed: int) -> float:
    rng = random.Random(seed)
    samples = sorted(mean(rng.choices(values, k=len(values))) for _ in range(2000))
    return samples[int(len(samples) * alpha)]


def report(state: dict) -> dict:
    sizes = {}
    for size in SIZES:
        pairs = []
        counts = {stage: 0 for stage in ("buy", "timed", "after_close")}
        for row in state["rows"]:
            legs = {k: row["quotes"].get(k, {}).get(str(size), {}) for k in counts}
            for stage in counts:
                counts[stage] += legs[stage].get("status") == "quoted"
            timed = stress_return(size, legs["buy"], legs["timed"])
            after = stress_return(size, legs["buy"], legs["after_close"])
            if timed is not None and after is not None:
                pairs.append((row["opened"], timed, after))
        pairs.sort()
        n = len(pairs)
        score = {
            "pairs": n, "quotes": counts, "timed_mean": mean(p[1] for p in pairs) if n else None,
            "after_close_mean": mean(p[2] for p in pairs) if n else None, "status": "pending",
        }
        trials = []
        for offset in range(0, n - MIN_PAIRS + 1, MIN_PAIRS):
            batch = pairs[offset:offset + MIN_PAIRS]
            block = offset // MIN_PAIRS + 1
            if len({p[0] // DAY for p in batch}) < 3:
                trials.append({"block": block, "status": "insufficient_days"})
                continue
            split = MIN_PAIRS // 2
            test = batch[split:]
            gains = [after - timed for _, timed, after in test]
            net = [after for _, _, after in test]
            # Split the one-sided error budget by size and disjoint retest block.
            alpha = 0.05 / (block * (block + 1))
            train_gain = mean(after - timed for _, timed, after in batch[:split])
            train_net = mean(after for _, _, after in batch[:split])
            lower_gain = bootstrap_lower(gains, alpha, 20261104 + block)
            lower_net = bootstrap_lower(net, alpha, 20261104 + block)
            trials.append({"block": block, "one_sided_alpha": alpha, "test_gain_lower": lower_gain,
                           "test_net_lower": lower_net, "status": "quote_stress_pass"
                           if min(train_gain, train_net, lower_gain, lower_net) > 0 else "fail"})
        score["trials"] = trials
        if trials:
            score["status"] = ("quote_stress_pass" if any(x["status"] == "quote_stress_pass" for x in trials)
                               else trials[-1]["status"])
        sizes[str(size)] = score
    return {
        "version": VERSION, "daily_attempts": state["attempts"], "targets": len(state["rows"]),
        "sizes": sizes, "live_authority": False, "fee_attribution_verified": False,
    }


def write(path: Path, data: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    os.replace(temp, path)


def once(root: Path, db: Path) -> dict:
    import fcntl

    usage = shutil.disk_usage(db.parent)
    if (usage.total - usage.free) * 100 >= usage.total * DISK_FLOOR:
        return {"version": VERSION, "status": "disk_pressure", "provider_attempts": 0}
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "locked"}
        path = root / "state.json"
        state = {"version": VERSION, "day": utc_ms() // DAY, "attempts": 0, "rows": []}
        if path.exists():
            with path.open("rb") as stream:
                blob = stream.read(1_000_001)
            if len(blob) > 1_000_000:
                raise ValueError("quote state exceeds size budget")
            state = json.loads(blob)
            if state["version"] != VERSION or len(state["rows"]) > MAX_ROWS:
                raise ValueError("quote state version or row cap")
        settings = get_settings()
        wallet = get_risk().chain_budget(Chain.SOL).wallet
        limits = limits_for("gmgn")
        if (db.resolve() != settings.db_path.resolve()
                or not settings.gmgn_api_key or not wallet or not MINT.fullmatch(wallet)
                or limits.weight_for("quote") != 10 or limits.capacity < 20):
            return {"version": VERSION, "status": "account_preflight_refused"}
        os.environ["GMGN_API_KEY"] = settings.gmgn_api_key
        os.environ.pop("GMGN_PRIVATE_KEY", None)
        try:
            status = cycle(state, GuardedReader(db, max_rows=8), wallet, lambda: write(path, state))
        except SourceDeferred:
            status = "guarded_source_deferred"
        summary = root / "latest.json"
        if status in {"buy", "timed", "after_close"} or not summary.exists():
            write(path, state)
            write(summary, {**report(state), "status": status, "at_ms": utc_ms()})
        return {"status": status, "daily_attempts": state["attempts"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--follow", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    last = None
    while True:
        result = once(args.root, args.db)
        if result != last:
            print(json.dumps(result), flush=True)
        if not args.follow:
            return
        last = result
        time.sleep(300 if result["status"] in {"disk_pressure", "daily_quote_cap", "study_capacity_reached"} else
                   60 if result["status"] in {"guarded_source_deferred", "provider_cooldown"} else 15)


if __name__ == "__main__":
    main()
