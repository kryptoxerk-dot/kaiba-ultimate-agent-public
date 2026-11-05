"""Pure policy tests: durable original admissions, not provider-return success."""
from __future__ import annotations

import copy

import pytest

from kaiba.core.schemas import digest
from kaiba.learning import quote_budget as qb

DAY = 21_000
MIDNIGHT = DAY * qb.DAY_MS
NOON = MIDNIGHT + qb.DAY_MS // 2
A, B, C = "A" * 32, "B" * 32, "C" * 32


def target(token=A, kind="open", *, now=NOON, age=60_000, **extra):
    return {"chain": "sol", "token": token, "kind": kind, "signal_ms": now - age, **extra}


def empty(now=NOON):
    return {"version": qb.OBSERVER_VERSION, "policy": dict(qb.OBSERVER_POLICY), "days": {},
            "tokens": {}, "attempts": [], "updated_ms": now, "retry_after_ms": 0}


def admit(state, t, at, *, status="admitted"):
    day = str(at // qb.DAY_MS)
    ordinal = state["days"].get(day, 0) + 1
    key = t["chain"] + ":" + t["token"]
    state["days"][day] = ordinal
    state["tokens"][key] = at
    state["attempts"] = (state["attempts"] + [{"attempt_id": digest([day, ordinal, key]),
        "target": copy.deepcopy(t), "admitted_ms": at, "status": status}])[-32:]
    state["updated_ms"] = at


def legacy(count, *, now=NOON):
    state = empty(now)
    state["days"][str(now // qb.DAY_MS)] = count
    return state


def test_original_policy_and_digest_are_compatible():
    from kaiba.learning import prospective_quotes as original
    assert qb.OBSERVER_VERSION == original.VERSION
    assert qb.OBSERVER_POLICY == original.POLICY
    state = empty()
    admit(state, target(kind="pending"), NOON)
    ledger = qb.reconcile_ledger(None, state, NOON)
    row = ledger["days"][str(DAY)]
    assert row["known"]["1"]["token_key"] == "sol:" + A
    assert row["observed_total"] == 1 and row["unknown_open"] == 0 and row["open_counts"] == {}


def test_genuine_100_admissions_are_capped_even_when_every_attempt_failed():
    state, ledger = empty(), None
    for i in range(100):
        at = MIDNIGHT + 78_000_000 + i * 20_000
        t = target(kind="pending", now=at)
        assert qb.admission_reason(t, state, ledger, at) is None
        admit(state, t, at, status="unknown")
        ledger = qb.reconcile_ledger(ledger, state, at)
    assert len(state["attempts"]) == 32
    assert len(ledger["days"][str(DAY)]["known"]) == 100
    assert qb.admission_reason(target(kind="pending", now=at + 20_000), state, ledger,
                               at + 20_000) == "daily_research_attempt_cap"


def test_pacing_exact_boundary_and_midnight_new_day():
    boundary = MIDNIGHT + qb.DAY_MS // 100
    state = legacy(10, now=MIDNIGHT)
    t = target(kind="pending", now=boundary - 1)
    assert qb.admission_reason(t, state, None, boundary - 1) == "daily_research_pacing"
    assert qb.admission_reason(t, state, None, boundary) is None
    state = legacy(100, now=MIDNIGHT + qb.DAY_MS - 1)
    ledger = qb.reconcile_ledger(None, state, MIDNIGHT + qb.DAY_MS - 1)
    next_day = MIDNIGHT + qb.DAY_MS
    assert qb.admission_reason(target(kind="pending", now=next_day), state, ledger, next_day) is None
    assert qb.admission_reason(target(now=next_day), state, ledger, next_day) is None


def test_paced_limit_integer_rounding_and_final_guard_semantics():
    assert qb.paced_limit(MIDNIGHT) == 10
    assert qb.paced_limit(MIDNIGHT + qb.DAY_MS // 100 - 1) == 10
    assert qb.paced_limit(MIDNIGHT + qb.DAY_MS // 100) == 11
    assert qb.paced_limit(MIDNIGHT + 90 * qb.DAY_MS // 100) == 100
    assert qb.paced_limit(MIDNIGHT + qb.DAY_MS - 1) == 100
    assert qb.paced_limit(MIDNIGHT + qb.DAY_MS) == 10


def test_midnight_preserves_token_spacing_across_day_boundary():
    at = MIDNIGHT + qb.DAY_MS - 1_000
    state = empty(at)
    admit(state, target(now=at), at)
    ledger = qb.reconcile_ledger(None, state, at)
    now = MIDNIGHT + qb.DAY_MS + 100_000
    assert qb.admission_reason(target(now=now), state, ledger, now) == "open_token_interval"
    assert qb.admission_reason(target(B, now=now), state, ledger, now) is None


def test_crash_reservation_and_status_changes_never_refund():
    state = empty()
    admit(state, target(), NOON)
    ledger = qb.reconcile_ledger(None, state, NOON)
    state["attempts"][0]["status"] = "unavailable"
    state["attempts"][0]["completed_ms"] = NOON + 2_000
    state["updated_ms"] = NOON + 2_000
    next_ledger = qb.reconcile_ledger(ledger, state, NOON + 2_000)
    assert next_ledger["days"] == ledger["days"]
    assert qb.admission_reason(target(now=NOON + 2_000), state, next_ledger, NOON + 2_000) == "open_token_interval"


def test_known_ordinals_survive_tail_eviction_and_pending_does_not_become_open():
    state, ledger = empty(), None
    for i in range(40):
        at = NOON + i * 20_000
        t = target(B, kind="pending", now=at)
        admit(state, t, at, status="entry_window_elapsed_during_validation")
        ledger = qb.reconcile_ledger(ledger, state, at)
    row = ledger["days"][str(DAY)]
    assert len(row["known"]) == 40 and row["unknown_open"] == 0 and row["open_counts"] == {}
    assert qb.admission_reason(target(A, now=at), state, ledger, at) is None


def test_missing_legacy_ordinals_bound_every_open_token_but_not_pending():
    state = legacy(6)
    ledger = qb.reconcile_ledger(None, state, NOON)
    assert ledger["days"][str(DAY)]["unknown_open"] == 6
    for token in (A, B):
        assert qb.admission_reason(target(token), state, ledger, NOON) == "open_token_daily_attempt_cap"
    assert qb.admission_reason(target(kind="pending"), state, ledger, NOON) is None


def test_partially_retained_legacy_tail_does_not_guess_missing_admissions():
    state = legacy(5)
    admit(state, target(B, kind="pending"), NOON)
    ledger = qb.reconcile_ledger(None, state, NOON)
    row = ledger["days"][str(DAY)]
    assert row["observed_total"] == 6 and row["unknown_open"] == 5
    assert row["known"]["6"]["kind"] == "pending"
    assert qb.admission_reason(target(), state, ledger, NOON) is None


def test_six_open_admissions_per_token_and_ten_minute_boundary():
    state, ledger = empty(), None
    for i in range(6):
        at = NOON + i * qb.OPEN_INTERVAL_MS
        t = target(now=at)
        assert qb.admission_reason(t, state, ledger, at) is None
        admit(state, t, at)
        ledger = qb.reconcile_ledger(ledger, state, at)
        if i < 5:
            assert qb.admission_reason(target(now=at + qb.OPEN_INTERVAL_MS - 1), state, ledger,
                at + qb.OPEN_INTERVAL_MS - 1) == "open_token_interval"
    now = at + qb.OPEN_INTERVAL_MS
    assert qb.admission_reason(target(now=now), state, ledger, now) == "open_token_daily_attempt_cap"
    assert qb.admission_reason(target(B, now=now), state, ledger, now) is None


def test_pending_age_edges_and_original_fifteen_second_spacing():
    state = empty()
    for age, reason in ((29_999, "pending_entry_not_ready"), (30_000, None), (120_000, None),
                        (120_001, "pending_entry_expired")):
        assert qb.admission_reason(target(kind="pending", age=age), state, None, NOON) == reason
    admit(state, target(kind="pending"), NOON)
    ledger = qb.reconcile_ledger(None, state, NOON)
    assert qb.admission_reason(target(kind="pending", now=NOON + 14_999), state, ledger,
                               NOON + 14_999) == "pending_token_interval"
    assert qb.admission_reason(target(kind="pending", now=NOON + 15_000), state, ledger, NOON + 15_000) is None


def test_selection_pending_earliest_deadline_then_open_least_recent_and_stable_ties():
    state = empty()
    admit(state, target(B, now=NOON - 1_200_000), NOON - 1_200_000)
    admit(state, target(C, now=NOON - 1_000_000), NOON - 1_000_000)
    ledger = qb.reconcile_ledger(None, state, NOON)
    pending_a = target(A, "pending", age=90_000)
    pending_b = target(B, "pending", age=90_000)
    later = target(C, "pending", age=60_000)
    targets = [target(), later, pending_b, pending_a]
    assert qb.select_target(targets, state, ledger, NOON) == pending_a
    assert qb.select_target(list(reversed(targets)), state, ledger, NOON) == pending_a
    assert qb.select_target([target(C), target(B)], state, ledger, NOON)["token"] == B
    chosen = qb.select_target([pending_a], state, ledger, NOON)
    chosen["token"] = B
    assert pending_a["token"] == A


def test_drain_only_and_retired_targets_are_excluded_and_unknown_lease_rejected():
    state = empty()
    targets = [target(A, "pending", allocation_lease="drain_only"),
               target(B, allocation_lease="retired"), target(C, allocation_lease="admission")]
    assert qb.select_target(targets, state, None, NOON)["token"] == C
    assert qb.select_target(targets[:2], state, None, NOON) is None
    with pytest.raises(ValueError, match="invalid_allocation_lease"):
        qb.select_target([target(allocation_lease="typo")], state, None, NOON)


def test_admission_is_rechecked_when_counter_or_clock_changes_after_selection():
    state = legacy(9, now=MIDNIGHT)
    now = MIDNIGHT + 60_000
    chosen = qb.select_target([target(kind="pending", now=now)], state, None, now)
    ledger = qb.reconcile_ledger(None, state, now)
    assert chosen is not None
    admit(state, target(B, "pending", now=now), now)
    assert qb.admission_reason(chosen, state, ledger, now) == "daily_research_pacing"
    state = empty()
    chosen = qb.select_target([target(kind="pending", age=119_000)], state, None, NOON)
    assert qb.admission_reason(chosen, state, None, NOON + 1_001) == "pending_entry_expired"


@pytest.mark.parametrize("mutation", [
    lambda s: s.update(updated_ms=NOON + 1),
    lambda s: s.update(updated_ms=True),
    lambda s: s["days"].update({str(DAY): True}),
    lambda s: s["days"].update({str(DAY): -1}),
    lambda s: s["days"].update({str(DAY): 101}),
    lambda s: s["days"].update({str(DAY + 1): 0}),
    lambda s: s["days"].update({"01": 0}),
    lambda s: s["tokens"].update({"sol:" + A: NOON + 1}),
    lambda s: s.update(policy={**s["policy"], "daily_attempts": 101}),
    lambda s: s.update(policy={**s["policy"], "wait_for_slot_s": False}),
    lambda s: s.update(unrelated=float("nan")),
])
def test_corrupt_observer_inputs_refuse_with_positive_control(mutation):
    state = empty()
    assert qb.admission_reason(target(), state, None, NOON) is None
    mutation(state)
    with pytest.raises(ValueError):
        qb.admission_reason(target(), state, None, NOON)


@pytest.mark.parametrize("mutation", [
    lambda s: s["attempts"][0].update(attempt_id="a" * 64),
    lambda s: s["attempts"].append(copy.deepcopy(s["attempts"][0])),
    lambda s: s["attempts"][0].update(admitted_ms=NOON + 1),
    lambda s: s["attempts"][0].update(completed_ms=NOON - 1),
    lambda s: s["attempts"][0]["target"].update(kind="other"),
    lambda s: s["attempts"][0]["target"].update(signal_ms=NOON + 1),
    lambda s: s["days"].update({str(DAY): 0}),
    lambda s: s["tokens"].clear(),
])
def test_corrupt_attempts_cannot_establish_knowledge(mutation):
    state = empty()
    admit(state, target(), NOON)
    assert qb.reconcile_ledger(None, state, NOON)["days"][str(DAY)]["unknown_open"] == 0
    mutation(state)
    with pytest.raises(ValueError):
        qb.reconcile_ledger(None, state, NOON)


@pytest.mark.parametrize("mutation", [
    lambda ledger: ledger.update(version="other"),
    lambda ledger: ledger.update(updated_ms=NOON + 1),
    lambda ledger: ledger["days"][str(DAY)].update(observed_total=0),
    lambda ledger: ledger["days"][str(DAY)].update(unknown_open=True),
    lambda ledger: ledger["days"][str(DAY)]["open_counts"].update({"sol:" + A: True}),
    lambda ledger: ledger["days"][str(DAY)]["known"]["1"].update(admitted_ms=NOON + 1),
    lambda ledger: ledger["tokens"].clear(),
    lambda ledger: ledger.update(policy={**ledger["policy"], "refunds": 0}),
    lambda ledger: ledger["days"][str(DAY)]["known"].update({"101": {"token_key": "sol:" + A, "kind": "open", "admitted_ms": NOON}}),
])
def test_corrupt_ledger_refuses_instead_of_resetting(mutation):
    state = empty()
    admit(state, target(), NOON)
    ledger = qb.reconcile_ledger(None, state, NOON)
    mutation(ledger)
    with pytest.raises(ValueError):
        qb.reconcile_ledger(ledger, state, NOON)


def test_counter_timestamp_token_and_same_ordinal_rollbacks_are_refused():
    state = empty()
    admit(state, target(), NOON)
    ledger = qb.reconcile_ledger(None, state, NOON)
    for field, value in (("days", {}), ("updated_ms", NOON - 1), ("tokens", {})):
        broken = copy.deepcopy(state)
        broken[field] = value
        with pytest.raises(ValueError):
            qb.reconcile_ledger(ledger, broken, NOON + 1)
    broken = copy.deepcopy(state)
    broken["attempts"][0]["target"]["kind"] = "pending"
    with pytest.raises(ValueError, match="identity_changed"):
        qb.reconcile_ledger(ledger, broken, NOON + 1)
    broken = copy.deepcopy(state)
    broken["attempts"] = []
    broken["tokens"]["sol:" + A] = NOON - 1
    with pytest.raises(ValueError, match="token_clock_rollback"):
        qb.reconcile_ledger(ledger, broken, NOON + 1)


def test_old_days_and_token_clocks_age_out_without_a_fresh_allowance_today():
    state = empty()
    admit(state, target(), NOON)
    ledger = qb.reconcile_ledger(None, state, NOON)
    now = NOON + 3 * qb.DAY_MS
    later = empty(now)
    later["days"][str(now // qb.DAY_MS)] = 10
    result = qb.reconcile_ledger(ledger, later, now)
    assert str(DAY) not in result["days"]
    assert result["days"][str(now // qb.DAY_MS)]["unknown_open"] == 10
    assert qb.admission_reason(target(now=now), later, result, now) == "open_token_daily_attempt_cap"


def test_original_sparse_writer_prunes_day_counters_but_retains_old_attempt_tail():
    state = empty()
    admit(state, target(), NOON)
    ledger = qb.reconcile_ledger(None, state, NOON)
    later = NOON + 2 * qb.DAY_MS
    # Exact base reservation pruning: old day counter/token removed, old tail kept.
    state["days"] = {}
    state["tokens"] = {}
    admit(state, target(B, "pending", now=later), later)
    result = qb.reconcile_ledger(ledger, state, later)
    assert len(state["attempts"]) == 2
    assert result["days"][str(DAY + 2)]["observed_total"] == 1
    assert result["days"][str(DAY + 2)]["unknown_open"] == 0
    assert result["days"][str(DAY)]["observed_total"] == 1
    assert qb.admission_reason(target(C, now=later), state, result, later) is None
    # Same sparse state must also initialize safely when the upgrade first starts.
    assert qb.reconcile_ledger(None, state, later)["days"][str(DAY + 2)]["unknown_open"] == 0


def test_strict_caps_future_targets_nonfinite_and_no_input_mutation():
    state = empty()
    before = copy.deepcopy(state)
    ledger = qb.reconcile_ledger(None, state, NOON)
    qb.select_target([target()], state, ledger, NOON)
    assert state == before
    for targets in ([target()] * 33, [target(signal_ms=NOON + 1)], [target(extra=float("nan"))]):
        with pytest.raises(ValueError):
            qb.select_target(targets, state, ledger, NOON)
    with pytest.raises(ValueError):
        qb.admission_reason(target(), state, ledger, True)


def test_future_backoff_is_a_valid_deadline_and_consumes_no_extra_admission():
    state = empty()
    state["retry_after_ms"] = NOON + 30_000
    assert qb.admission_reason(target(), state, None, NOON) == "source_retry_backoff"
    assert qb.admission_reason(target(now=NOON + 30_000), state, None, NOON + 30_000) is None
    assert state["days"] == {}


@pytest.mark.parametrize("bad", [target(kind=[]), target(allocation_lease={}),
                                 target(signal_ms=True), target(chain=True), target(token="bad")])
def test_malformed_targets_raise_valueerror_not_untyped_membership_errors(bad):
    with pytest.raises(ValueError):
        qb.select_target([bad], empty(), None, NOON)


def test_evm_identity_is_canonical_and_chain_scoped():
    state = empty()
    t = target(token="0x" + "a" * 40, chain="bsc")
    admit(state, t, NOON)
    ledger = qb.reconcile_ledger(None, state, NOON)
    assert ledger["days"][str(DAY)]["open_counts"] == {"bsc:0x" + "a" * 40: 1}
    assert qb.admission_reason({**t, "chain": "robinhood"}, state, ledger, NOON) is None
    with pytest.raises(ValueError, match="invalid_token_identity"):
        qb.admission_reason({**t, "token": "0x" + "A" * 40}, state, ledger, NOON)


def test_unknown_legacy_count_combines_with_known_open_count_without_a_refund():
    state = legacy(5)
    admit(state, target(B), NOON)
    ledger = qb.reconcile_ledger(None, state, NOON)
    later = NOON + qb.OPEN_INTERVAL_MS
    assert ledger["days"][str(DAY)]["unknown_open"] == 5
    assert qb.admission_reason(target(B, now=later), state, ledger, later) == "open_token_daily_attempt_cap"
    assert qb.admission_reason(target(A, now=later), state, ledger, later) is None


def test_distinct_attempt_ids_cannot_reuse_one_daily_ordinal():
    state = empty()
    admit(state, target(), NOON)
    admit(state, target(B, now=NOON + 1), NOON + 1)
    state["attempts"][1]["attempt_id"] = digest([str(DAY), 1, "sol:" + B])
    with pytest.raises(ValueError, match="ordinal_order"):
        qb.reconcile_ledger(None, state, NOON + 1)
