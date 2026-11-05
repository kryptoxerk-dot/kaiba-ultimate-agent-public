"""Pure admission policy for the additive prospective-quote observer upgrade.

The original observer remains the authority for reservations and its counters.
This bounded ledger remembers identified ordinals after its 32-attempt tail rolls
off. Unidentified admissions might all have been for any one open token, so each
open token receives that same conservative upper bound. No admission is refunded.
The caller must persist the original reservation and reconciled ledger before I/O.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any

VERSION = "quote-budget/1"
DAY_MS = 86_400_000
DAILY_ATTEMPTS = 100
OPEN_INTERVAL_MS = 600_000
OPEN_DAILY_ATTEMPTS = 6
MAX_DAYS, MAX_TOKENS, MAX_TARGETS, MAX_ATTEMPTS = 3, 201, 32, 32
MAX_BYTES = 1_000_000
POLICY = {
    "daily_attempts": DAILY_ATTEMPTS,
    "pacing_start": 10,
    "pacing_daily_rate": 100,
    "open_token_interval_ms": OPEN_INTERVAL_MS,
    "open_token_daily_attempts": OPEN_DAILY_ATTEMPTS,
    "pending_token_interval_ms": 15_000,
    "entry_min_age_ms": 30_000,
    "entry_max_age_ms": 120_000,
    "unknown_open_policy": "all_unidentified_ordinals_apply_to_every_open_token",
    "refunds": False,
}

# Frozen policy of prospective_quotes/1. Do not import its I/O-bearing collector.
OBSERVER_VERSION = "prospective_quotes/1"
OBSERVER_POLICY = {
    "daily_attempts": 100, "token_interval_ms": 15_000,
    "max_books": 18, "max_targets": 32, "calls_per_cycle": 1,
    "research_scan_seconds": 10, "priority": "RESEARCH", "ttl_s": 15,
    "stale_grace_s": 0, "timeout_s": 6, "wait_for_slot_s": 0,
}


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _int(value: Any, *, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    _require(type(value) is int and minimum <= value <= maximum, "invalid_integer_clock_or_counter")
    return value


def _bounded(value: Any, limit: int = MAX_BYTES) -> None:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("invalid_or_nonfinite_json") from exc
    _require(len(encoded.encode()) <= limit, "budget_input_size_cap")


def _digest(value: Any) -> str:
    # Exact core.schemas.digest semantics for these strictly JSON primitive inputs.
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _day(value: Any, now: int) -> str:
    _require(isinstance(value, str) and bool(re.fullmatch(r"0|[1-9][0-9]{0,15}", value)), "invalid_day_key")
    _require(int(value) <= now // DAY_MS, "future_day_counter")
    return value


def _key(chain: Any, token: Any) -> str:
    _require(isinstance(chain, str) and chain in {"sol", "bsc", "robinhood"}, "invalid_token_chain")
    pattern = r"[1-9A-HJ-NP-Za-km-z]{32,44}" if chain == "sol" else r"0x[0-9a-f]{40}"
    _require(isinstance(token, str) and bool(re.fullmatch(pattern, token)), "invalid_token_identity")
    return chain + ":" + token


def _token_key(value: Any) -> str:
    _require(isinstance(value, str) and value.count(":") == 1, "invalid_token_key")
    chain, token = value.split(":")
    return _key(chain, token)


def _target(target: Any, now: int) -> str:
    _require(type(target) is dict, "invalid_target")
    _bounded(target, 32_768)
    key = _key(target.get("chain"), target.get("token"))
    _require(isinstance(target.get("kind"), str) and target["kind"] in {"pending", "open"}, "invalid_target_kind")
    _require(_int(target.get("signal_ms"), minimum=1) <= now, "future_signal_clock")
    lease = target.get("allocation_lease")
    _require(lease is None or isinstance(lease, str) and lease in {"admission", "drain_only", "retired"},
             "invalid_allocation_lease")
    return key


def _state(state: Any, now: int) -> tuple[dict, dict, dict]:
    _require(type(state) is dict, "invalid_observer_state")
    _bounded(state)
    _require(state.get("version") == OBSERVER_VERSION and type(state.get("policy")) is dict
             and _digest(state["policy"]) == _digest(OBSERVER_POLICY),
             "observer_state_policy_changed")
    _require(_int(state.get("updated_ms")) <= now, "observer_clock_moved_backward")
    _int(state.get("retry_after_ms", 0))  # A backoff deadline may legitimately be in the future.
    days, tokens, attempts = state.get("days"), state.get("tokens"), state.get("attempts")
    _require(type(days) is dict and len(days) <= MAX_DAYS and type(tokens) is dict
             and len(tokens) <= MAX_TOKENS and type(attempts) is list and len(attempts) <= MAX_ATTEMPTS,
             "observer_state_structure_or_cap")
    for day, count in days.items():
        _day(day, now)
        _int(count, maximum=DAILY_ATTEMPTS)
    for key, stamp in tokens.items():
        _token_key(key)
        _require(_int(stamp, minimum=1) <= now, "future_token_clock")
        _require(days.get(str(stamp // DAY_MS), 0) > 0, "token_clock_without_admission_counter")
    if "last_target" in state:
        _require(state["last_target"] == "" or _token_key(state["last_target"]) == state["last_target"],
                 "invalid_last_target")
    known: dict[str, dict] = {}
    ids: set[str] = set()
    previous_stamp = 0
    previous_ordinal: dict[str, int] = {}
    for attempt in attempts:
        _require(type(attempt) is dict, "invalid_attempt")
        stamp = _int(attempt.get("admitted_ms"), minimum=1)
        _require(previous_stamp <= stamp <= now, "attempt_clock_order_or_future")
        previous_stamp = stamp
        key = _target(attempt.get("target"), stamp)
        day = str(stamp // DAY_MS)
        # The original writer prunes day counters to today/yesterday on a new
        # admission, but leaves old attempts in the tail when activity is sparse.
        _require(day in days or int(day) < now // DAY_MS - 1, "attempt_without_day_counter")
        aid = attempt.get("attempt_id")
        _require(isinstance(aid, str) and bool(re.fullmatch(r"[0-9a-f]{64}", aid)) and aid not in ids,
                 "invalid_or_duplicate_attempt_id")
        ids.add(aid)
        ordinals = [n for n in range(1, DAILY_ATTEMPTS + 1) if _digest([day, n, key]) == aid]
        _require(len(ordinals) == 1 and ordinals[0] <= days.get(day, DAILY_ATTEMPTS),
                 "attempt_ordinal_not_admitted")
        ordinal = ordinals[0]
        _require(ordinal > previous_ordinal.get(day, 0), "attempt_ordinal_order")
        previous_ordinal[day] = ordinal
        status = attempt.get("status")
        _require(isinstance(status, str) and 0 < len(status) <= 128, "invalid_attempt_status")
        if "completed_ms" in attempt:
            _require(stamp <= _int(attempt["completed_ms"], minimum=1) <= now, "attempt_completion_clock")
        if now - stamp <= DAY_MS:
            _require(tokens.get(key, 0) >= stamp, "attempt_missing_last_admission_clock")
        known.setdefault(day, {})[str(ordinal)] = {
            "token_key": key, "kind": attempt["target"]["kind"], "admitted_ms": stamp,
        }
    return days, tokens, known


def _record(total: int, known: dict) -> dict:
    counts = Counter(row["token_key"] for row in known.values() if row["kind"] == "open")
    return {"observed_total": total, "known": known, "open_counts": dict(sorted(counts.items())),
            "unknown_open": total - len(known)}


def _ledger(ledger: Any, now: int) -> dict:
    _require(type(ledger) is dict, "invalid_budget_ledger")
    _bounded(ledger)
    _require(set(ledger) == {"version", "policy", "updated_ms", "observer_updated_ms", "days", "tokens"}
             and ledger["version"] == VERSION and type(ledger["policy"]) is dict
             and _digest(ledger["policy"]) == _digest(POLICY), "budget_ledger_policy_or_shape")
    updated = _int(ledger["updated_ms"])
    _require(_int(ledger["observer_updated_ms"]) <= updated <= now, "budget_ledger_clock")
    _require(type(ledger["tokens"]) is dict and len(ledger["tokens"]) <= MAX_TOKENS
             and type(ledger["days"]) is dict and len(ledger["days"]) <= MAX_DAYS, "budget_ledger_cap")
    for key, stamp in ledger["tokens"].items():
        _token_key(key)
        _require(_int(stamp, minimum=1) <= updated, "budget_ledger_token_clock")
    for day, row in ledger["days"].items():
        _day(day, updated)
        _require(type(row) is dict and set(row) == {"observed_total", "known", "open_counts", "unknown_open"},
                 "budget_day_shape")
        total = _int(row["observed_total"], maximum=DAILY_ATTEMPTS)
        _require(type(row["known"]) is dict and len(row["known"]) <= total, "budget_known_cap")
        for ordinal, entry in row["known"].items():
            _require(isinstance(ordinal, str) and bool(re.fullmatch(r"[1-9][0-9]{0,2}", ordinal))
                     and int(ordinal) <= total, "budget_known_ordinal")
            _require(type(entry) is dict and set(entry) == {"token_key", "kind", "admitted_ms"},
                     "budget_known_shape")
            _token_key(entry["token_key"])
            _require(isinstance(entry["kind"], str) and entry["kind"] in {"open", "pending"}, "budget_known_kind")
            stamp = _int(entry["admitted_ms"], minimum=1)
            _require(stamp <= updated and str(stamp // DAY_MS) == day, "budget_known_clock")
            if updated - stamp <= DAY_MS:
                _require(ledger["tokens"].get(entry["token_key"], 0) >= stamp,
                         "budget_known_last_admission_clock")
        stamps = [entry["admitted_ms"] for _, entry in sorted(row["known"].items(), key=lambda item: int(item[0]))]
        _require(stamps == sorted(stamps), "budget_known_clock_order")
        _int(row["unknown_open"], maximum=DAILY_ATTEMPTS)
        _require(type(row["open_counts"]) is dict and len(row["open_counts"]) <= DAILY_ATTEMPTS,
                 "budget_open_count_cap")
        for key, count in row["open_counts"].items():
            _token_key(key)
            _int(count, minimum=1, maximum=DAILY_ATTEMPTS)
        _require(row == _record(total, row["known"]), "budget_day_arithmetic")
    return ledger


def reconcile_ledger(ledger_or_None: dict | None, observer_state: dict, now_ms: int) -> dict:
    """Validate monotonic authority counters and retain all known recent ordinals.

    Missing history is unknown, never assumed to be unused or refunded. This does
    not reserve anything. Persist the returned value atomically under the owner's
    existing worker lock after every original-state admission, including crashes.
    """
    now = _int(now_ms, minimum=1)
    days, tokens, observed = _state(observer_state, now)
    prior = _ledger(ledger_or_None, now) if ledger_or_None is not None else None
    if prior is not None:
        _require(observer_state["updated_ms"] >= prior["observer_updated_ms"], "observer_state_rollback")
        for day, row in prior["days"].items():
            if int(day) >= now // DAY_MS - 1:
                _require(days.get(day, 0) >= row["observed_total"], "observer_counter_rollback")
        for key, stamp in prior["tokens"].items():
            if key in tokens:
                _require(tokens[key] >= stamp, "observer_token_clock_rollback")
            elif now - stamp <= DAY_MS:
                raise ValueError("observer_token_clock_removed")
    combined = set(days) | (set(prior["days"]) if prior is not None else set())
    retained = sorted((d for d in combined if int(d) >= now // DAY_MS - 2), key=int)[-MAX_DAYS:]
    result_days = {}
    for day in retained:
        old = prior["days"].get(day) if prior is not None else None
        total = days.get(day, old["observed_total"] if old else 0)
        if old:
            _require(total >= old["observed_total"], "observer_counter_rollback")
        known = {ordinal: dict(entry) for ordinal, entry in old["known"].items()} if old else {}
        for ordinal, entry in observed.get(day, {}).items():
            _require(ordinal not in known or known[ordinal] == entry, "observer_attempt_identity_changed")
            known[ordinal] = dict(entry)
        result_days[day] = _record(total, dict(sorted(known.items(), key=lambda item: int(item[0]))))
    result = {"version": VERSION, "policy": dict(POLICY), "updated_ms": now,
              "observer_updated_ms": observer_state["updated_ms"], "days": result_days, "tokens": dict(tokens)}
    _ledger(result, now)
    return result


def paced_limit(now_ms: int) -> int:
    """Maximum cumulative admissions at this UTC instant (integer-exact)."""
    now = _int(now_ms, minimum=1)
    return min(DAILY_ATTEMPTS, 10 + 100 * (now % DAY_MS) // DAY_MS)


def _reason(target: dict, state: dict, ledger: dict, now: int) -> str | None:
    key = _target(target, now)
    if target.get("allocation_lease") in {"drain_only", "retired"}:
        return "retired_allocation_lease"
    count = state["days"].get(str(now // DAY_MS), 0)
    if count >= DAILY_ATTEMPTS:
        return "daily_research_attempt_cap"
    allowed = paced_limit(now)
    if count >= allowed:
        return "daily_research_pacing"
    if now < state.get("retry_after_ms", 0):
        return "source_retry_backoff"
    last = state["tokens"].get(key, 0)
    if target["kind"] == "pending":
        age = now - target["signal_ms"]
        if age < 30_000:
            return "pending_entry_not_ready"
        if age > 120_000:
            return "pending_entry_expired"
        if last and now - last < 15_000:
            return "pending_token_interval"
    else:
        row = ledger["days"].get(str(now // DAY_MS), _record(0, {}))
        if row["open_counts"].get(key, 0) + row["unknown_open"] >= OPEN_DAILY_ATTEMPTS:
            return "open_token_daily_attempt_cap"
        if last and now - last < OPEN_INTERVAL_MS:
            return "open_token_interval"
    return None


def admission_reason(target: dict, state: dict, ledger: dict | None, now_ms: int) -> str | None:
    """Return the current refusal reason; malformed or rollback inputs raise ValueError."""
    current = reconcile_ledger(ledger, state, now_ms)
    return _reason(target, state, current, now_ms)


def select_target(targets: list[dict], state: dict, ledger: dict | None, now_ms: int) -> dict | None:
    """Pending earliest deadline first, then open tokens least recently admitted."""
    _require(type(targets) is list and len(targets) <= MAX_TARGETS, "target_structure_or_cap")
    _bounded(targets)
    current = reconcile_ledger(ledger, state, now_ms)
    candidates = []
    for target in targets:
        if _reason(target, state, current, now_ms) is None:
            key = _key(target["chain"], target["token"])
            rank = ((0, target["signal_ms"] + 120_000, key, _digest(target)) if target["kind"] == "pending"
                    else (1, state["tokens"].get(key, 0), key, _digest(target)))
            candidates.append((rank, target))
    if not candidates:
        return None
    # Round-trip returns independent bounded JSON; callers cannot mutate input by alias.
    return json.loads(json.dumps(min(candidates, key=lambda item: item[0])[1]))
