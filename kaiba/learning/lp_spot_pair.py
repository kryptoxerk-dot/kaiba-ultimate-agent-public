"""Compare a modeled SOL-only DLMM range with one dip buy and holding SOL."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from base64 import b64decode
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

from kaiba.execution.lp_model import (
    CLOSE_TXS,
    OPEN_TXS,
    SWAP_TXS,
    TX_FEE_LAMPORTS,
    Arm,
    PoolState,
    holdings,
    land,
    plan_bins,
    quote_sell,
)
from kaiba.execution.lp_tx import NATIVE_MINT, quote_buy, total_fee_rate
from kaiba.learning.dlmm_state import bin_array_address, decode_bins, decode_pool, public_key
from kaiba.learning.lp_token_transfer import Fee, MintTransfers, epoch_schedule, mint_transfers

VERSION = "lp-spot-pair/1"
CAPTURE_VERSION = "lp-spot-capture/1"
MAX_BYTES = 2_000_000
TICKETS = (100_000_000, 300_000_000)
HORIZON_MS = 180_000
DELAY_MS = 3_000
MAX_GAP_MS = 22_000
ARM = Arm("paired_20bin", 20, 180, 0, 0, 0)


class Frame(NamedTuple):
    state: PoolState
    fee: float
    identity: tuple
    mint: MintTransfers | None = None
    token_fee: Fee = Fee()


def positive_int(value):
    return type(value) is int and 0 < value < 2**63


def decode_frame(pool, receipt, *, bootstrap=False):
    request, response = receipt["request"], receipt["response"]
    keys, options = request["params"]
    if (
        request["jsonrpc"] != "2.0" or response["jsonrpc"] != "2.0"
        or request["method"] != "getMultipleAccounts" or "error" in response
        or not positive_int(request["id"]) or type(response["id"]) is not int
        or request["id"] != response["id"]
        or not isinstance(keys, list) or len(keys) not in ((1,) if bootstrap else (6, 7)) or keys[0] != pool
        or len(set(keys)) != len(keys) or options["encoding"] != "base64"
        or options["commitment"] != "confirmed"
    ):
        raise ValueError("invalid_frame_envelope")
    for key in keys:
        public_key(key)
    result = response["result"]
    accounts, slot = result["value"], result["context"]["slot"]
    start, end = receipt["started_ms"], receipt["received_ms"]
    min_slot = options.get("minContextSlot", 0) if bootstrap else options["minContextSlot"]
    if (
        not isinstance(accounts, list) or len(accounts) != len(keys)
        or not positive_int(slot) or type(min_slot) is not int
        or not (0 if bootstrap else 1) <= min_slot <= slot
        or not positive_int(start) or not positive_int(end) or not 0 <= end - start <= 7_000
    ):
        raise ValueError("invalid_frame_clock_or_accounts")
    head = decode_pool(accounts[0])
    mint = None
    if len(keys) == 7:
        if head["token_program_flags"] != [1, 0] or keys[-1] != head["mints"][0]:
            raise ValueError("mint_account_identity_mismatch")
        mint = mint_transfers(keys[-1], accounts[-1])
    fee = total_fee_rate(b64decode(accounts[0]["data"][0], validate=True), head["bin_step"])
    bins = {}
    for key, account in zip(keys[1:6], accounts[1:6], strict=True):
        if account is None:
            continue
        array = decode_bins(account)
        if array["pool"] != pool or bin_array_address(pool, array["index"]) != key:
            raise ValueError("invalid_bin_identity")
        bins.update({row["id"]: row for row in array["bins"]})
    state = PoolState(slot, end, head["active_bin"], head["bin_step"], bins)
    identity = (
        tuple(keys), tuple(head["mints"]), tuple(head["token_program_flags"]),
        head["status"], head["collect_fee_mode"], head["rewards_present"], head["bin_step"],
    )
    return Frame(state, fee, identity, mint)


def pool_basis_problem(frame, *, allow_token2022_bootstrap=False):
    _, mints, programs, status, fee_mode, rewards, _ = frame.identity
    if mints[1] != NATIVE_MINT or mints[0] == NATIVE_MINT:
        return "sol_quote_required"
    if programs not in ((0, 0), (1, 0)) or (
        programs == (1, 0) and frame.mint is None and not allow_token2022_bootstrap
    ):
        return "token_program_unmodeled"
    if status:
        return "pool_disabled"
    if fee_mode:
        return "pool_fee_mode_unmodeled"
    if rewards:
        return "pool_rewards_unmodeled"
    return frame.mint.problem if frame.mint else None


def contiguous(state, direction):
    """A route cannot cross a bin for which the snapshot lacks an account."""
    bins = {}
    current = state.active
    while current in state.bins:
        bins[current] = state.bins[current]
        current += direction
    return replace(state, bins=bins)


def sell_all(atoms, frame, extra_fee):
    if atoms == 0:
        return 0
    state = contiguous(frame.state, -1)
    if not state.bins:
        return None
    proceeds, left = quote_sell(atoms, state, frame.fee + extra_fee)
    return math.floor(proceeds) if left < 1e-6 and proceeds >= 1 else None


def lp_leg(frames, ticket, extra_fee, tx_fee):
    start = frames[0].state
    plan = plan_bins(ARM, start, ticket)
    landing = next(
        (frame.state for frame in frames[1:-1] if frame.state.at_ms >= start.at_ms + DELAY_MS),
        None,
    )
    if landing is None:
        return "unpriced_landing_frame", None, None
    if not land(plan, landing):
        return "paid_miss", -tx_fee, (0, 0)
    terminal = frames[-1]
    required = set(range(plan.lo, plan.hi + 1))
    if not required.issubset(landing.bins) or not required.issubset(terminal.state.bins):
        return "unpriced_position_range", None, None
    atoms, cash = holdings(plan, terminal.state)
    gross = math.floor(atoms)
    withdrawal_fee = terminal.token_fee.charge(gross)
    received = gross - withdrawal_fee
    sweep_fee = terminal.token_fee.charge(received)
    net_input = received - sweep_fee
    if gross and not net_input:
        return "unpriced_exit", None, None
    proceeds = sell_all(net_input, terminal, extra_fee)
    if proceeds is None:
        return "unpriced_exit", None, None
    charge = tx_fee * (OPEN_TXS + CLOSE_TXS + (SWAP_TXS if gross else 0))
    return "priced_before_fee_income", math.floor(cash) + proceeds - ticket - charge, (withdrawal_fee, sweep_fee)


def spot_leg(frames, ticket, extra_fee, tx_fee):
    start, terminal = frames[0].state, frames[-1]
    cutoff = start.at_ms + HORIZON_MS - DELAY_MS
    touched = next(
        (frame.state.at_ms for frame in frames[1:-1]
         if frame.state.at_ms <= cutoff and frame.state.active < start.active),
        None,
    )
    if touched is None:
        return "no_sampled_trigger_cash", 0, (0, 0)
    entry = next(
        (frame for frame in frames[:-1] if frame.state.at_ms >= touched + DELAY_MS),
        None,
    )
    if entry is None:
        return "unpriced_entry_frame", None, None
    state = contiguous(entry.state, 1)
    if not state.bins:
        return "unpriced_entry", None, None
    atoms, left, _ = quote_buy(ticket, state, entry.fee + extra_fee)
    if left >= 1e-6 or atoms < 1:
        return "unpriced_entry", None, None
    gross = math.floor(atoms)
    buy_fee = entry.token_fee.charge(gross)
    received = gross - buy_fee
    if not received:
        return "unpriced_entry", None, None
    sell_fee = terminal.token_fee.charge(received)
    if received == sell_fee:
        return "unpriced_exit", None, None
    proceeds = sell_all(received - sell_fee, terminal, extra_fee)
    if proceeds is None:
        return "unpriced_exit", None, None
    return "priced", proceeds - ticket - 2 * SWAP_TXS * tx_fee, (buy_fee, sell_fee)


def paired_result(frames, ticket, extra_fee, tx_fee):
    lp_status, lp_net, lp_fees = lp_leg(frames, ticket, extra_fee, tx_fee)
    spot_status, spot_net, spot_fees = spot_leg(frames, ticket, extra_fee, tx_fee)
    priced = lp_net is not None and spot_net is not None
    return {
        "lp_status": lp_status, "spot_status": spot_status,
        "lp_before_fee_income_lamports": lp_net, "spot_net_lamports": spot_net,
        "lp_withdrawal_and_sweep_transfer_fee_atoms": lp_fees,
        "spot_buy_and_sell_transfer_fee_atoms": spot_fees,
        "cash_net_lamports": 0, "priced_comparison": priced,
        "lp_minus_spot_before_fee_income_lamports": lp_net - spot_net if priced else None,
        "net_fee_income_to_match_best_lamports": (
            max(0, max(spot_net, 0) - lp_net) if priced and lp_status != "paid_miss" else None
        ),
    }


def evaluate(capture):
    if capture["version"] != CAPTURE_VERSION or not isinstance(capture["frames"], list):
        raise ValueError("invalid_capture_scope")
    receipts, pool = capture["frames"], capture["pool"]
    requests = capture["requests"]
    if not positive_int(requests) or not len(receipts) < requests <= 15 or len(receipts) > 14:
        raise ValueError("invalid_request_count")
    public_key(pool)
    report = {
        "version": VERSION, "basis": "sampled_reserve_model",
        "frames_received": len(receipts), "requests": requests,
        "financial_execution": False, "promotion_authority": False,
    }
    screening = capture.get("screening")
    if screening is not None:
        if requests != 1 or receipts or screening["request"]["id"] != 1:
            raise ValueError("invalid_screening_receipt")
        problem = pool_basis_problem(decode_frame(pool, screening, bootstrap=True))
        if problem is None:
            raise ValueError("screening_does_not_refuse_pool")
        return {**report, "status": "unsupported_pool_basis", "basis_problem": problem, "by_ticket": {}}
    if not receipts:
        return {**report, "status": "no_frames", "by_ticket": {}}
    schedule_receipt = capture.get("epoch_schedule")
    schedule = epoch_schedule(schedule_receipt) if schedule_receipt is not None else None
    first_id = 3 if schedule else 2
    if len(receipts) + first_id - 1 > requests:
        raise ValueError("invalid_request_count")
    if schedule and (
        not positive_int(schedule_receipt["started_ms"]) or not positive_int(schedule_receipt["received_ms"])
        or not 0 <= schedule_receipt["received_ms"] - schedule_receipt["started_ms"] <= 7_000
        or schedule_receipt["received_ms"] > receipts[0]["started_ms"]
    ):
        raise ValueError("invalid_epoch_schedule_clock")
    frames = []
    first = previous = None
    for request_id, receipt in enumerate(receipts, first_id):
        frame = decode_frame(pool, receipt)
        if receipt["request"]["id"] != request_id:
            raise ValueError("nonconsecutive_request_ids")
        if previous and (
            frame.state.slot <= previous.state.slot
            or receipt["started_ms"] < previous.state.at_ms
            or frame.state.at_ms <= previous.state.at_ms
        ):
            raise ValueError("noncausal_frame")
        first = first or frame
        if frame.identity != first.identity:
            return {**report, "status": "pool_basis_changed", "by_ticket": {}}
        if previous and frame.state.at_ms - previous.state.at_ms > MAX_GAP_MS:
            return {**report, "status": "frame_gap", "by_ticket": {}}
        problem = pool_basis_problem(frame)
        if problem:
            return {**report, "status": "unsupported_pool_basis", "basis_problem": problem, "by_ticket": {}}
        if frame.mint:
            if schedule is None:
                raise ValueError("missing_epoch_schedule")
            frame = frame._replace(token_fee=frame.mint.fee_at(schedule.at_slot(frame.state.slot)))
        elif schedule:
            raise ValueError("unexpected_epoch_schedule")
        previous = frame
        frames.append(frame)
        if frame.state.at_ms - first.state.at_ms >= HORIZON_MS:
            break
    if frames[-1].state.at_ms - first.state.at_ms < HORIZON_MS:
        return {**report, "status": "exit_frame_missing", "by_ticket": {}}
    if any(frame.state.active not in frame.state.bins for frame in frames):
        return {**report, "status": "unpriced_frame_coverage", "by_ticket": {}}
    results = {
        str(ticket): {
            "base": paired_result(frames, ticket, 0, TX_FEE_LAMPORTS),
            "stress": paired_result(frames, ticket, 0.005, 2 * TX_FEE_LAMPORTS),
        }
        for ticket in TICKETS
    }
    active = first.state.active
    planned_bins = range(active - ARM.width, active)
    observed_growth = None
    if all(all(bid in frame.state.bins for bid in planned_bins) for frame in frames):
        observed_growth = sum(
            len({tuple(frame.state.bins[bid]["fee_growth"]) for frame in frames}) > 1
            for bid in planned_bins
        )
    return {
        **report, "status": "evaluated", "sampled_horizon_ms": frames[-1].state.at_ms - first.state.at_ms,
        "sampled_active_bin_change": [
            min(frame.state.active for frame in frames) - active,
            max(frame.state.active for frame in frames) - active,
        ],
        "planned_bins_with_observed_fee_growth_change": observed_growth,
        "token_transfer_evidence": {
            "program": "token2022" if frames[0].mint else "classic_spl",
            "observed_hook_states": sorted({frame.mint.hook for frame in frames if frame.mint}),
            "observed_fee_bps": sorted({frame.token_fee.bps for frame in frames}),
            "observed_fee_caps_atoms": sorted({frame.token_fee.maximum_atoms for frame in frames}),
            "observed_extensions": sorted({kind for frame in frames if frame.mint for kind in frame.mint.extensions}),
        },
        "by_ticket": results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    args = parser.parse_args(argv)
    try:
        with args.capture.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("capture_too_large")
        report = evaluate(json.loads(raw))
    except (OSError, ValueError, KeyError, TypeError, IndexError, RecursionError, OverflowError):
        print(json.dumps({"version": VERSION, "status": "invalid_capture", "financial_execution": False}))
        return 2
    print(json.dumps({**report, "capture_sha256": hashlib.sha256(raw).hexdigest()}, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
