"""A stop-loss read is not abandoned over a sub-second gap in our own limiter.

MEASURED 2026-10-01 on the box, 24 h of robinhood protection:

* 883 of 1,967 ``protection_blind`` events (45%) read ``rate limited: dexscreener:
  minimum interval``, on five tokens DexScreener prices fine -- none of them was ever
  blind for any other reason. 75% of all dexscreener calls landed inside protection's own
  ticks (9% of the clock): the prefetch workers price the book concurrently, and every
  EXIT read after the first was refused by the 275 ms gap the first had just opened. The
  refusal said "retry in 0.2s"; the read was abandoned until the next 12 s tick.
* copy_manager's POSITION-priority gmgn holdings read was refused the same way 21 times
  ("retry in 0.0-0.5s"); in 17 of those a scanner token.info/token.security call had
  started within the same run.
* A robinhood stop on 2026-09-30 was refused "gmgn: minimum interval (retry in 0.1s)",
  re-sent on the next tick and closed at -42.3% against a -30% stop.

The fix waits the interval OUT, bounded, at EXIT/UNRESOLVED/POSITION. It never shortens
the interval, never waits at ENTRY or below, never waits through anything that is not a
minimum-interval refusal (a provider cooldown, our 429 penalty beyond the bound, an
exhausted bucket), and never sleeps while the caller holds a transaction.
"""

from __future__ import annotations

import pytest

from kaiba.core import limiter
from kaiba.core.limiter import (
    INTERVAL_WAIT_S,
    LOCAL_TRANSIENT_REASONS,
    Limits,
    Priority,
    RateLimited,
    local_refusal_retry_s,
    release,
    reserve,
)

PROV = "testprov"
EP = "price.token_pairs"
#: Same shape as the box's dexscreener budget: 1100 ms, so 275 ms at EXIT/POSITION.
LIM = Limits(min_interval_ms=1100, capacity=60, refill_per_s=1.0, max_inflight=2)
EXIT_GAP_MS = max(50, LIM.min_interval_ms // 4)


@pytest.fixture
def clock(monkeypatch):
    """One fake clock for the limiter's `now_ms`, its sleep and its wait deadline.

    ``slept`` records every sleep, so a test can assert that a refusal was NOT waited on.
    The sleep refuses to run more than 100 times: a wait loop that lost its deadline must
    fail the test, not hang it.
    """
    state = {"ms": 1_000_000, "slept": []}

    def sleep(seconds: float) -> None:
        state["slept"].append(seconds)
        if len(state["slept"]) > 100:
            raise AssertionError("the interval wait is not bounded")
        state["ms"] += int(round(seconds * 1000))

    monkeypatch.setattr(limiter, "now_ms", lambda: state["ms"])
    monkeypatch.setattr(limiter, "_sleep", sleep)
    monkeypatch.setattr(limiter, "_monotonic", lambda: state["ms"] / 1000.0)
    real = limiter.limits_for
    monkeypatch.setattr(limiter, "limits_for", lambda p: LIM if p == PROV else real(p))
    return state


def _take_slot(conn, priority: Priority = Priority.RESEARCH) -> None:
    reserve(PROV, EP, priority, conn)
    release(PROV, EP, "ok", conn=conn)


def _last_call(conn) -> int:
    return int(conn.execute("SELECT last_call_ms FROM provider_state WHERE provider=?",
                            (PROV,)).fetchone()[0])


# ------------------------------------------------------------------ the wait


def test_an_exit_read_waits_out_a_sub_second_interval_instead_of_failing(tmp_db, clock):
    """THE REGRESSION: the second of two concurrent EXIT reads used to be refused."""
    _take_slot(tmp_db, Priority.EXIT)
    first = _last_call(tmp_db)

    assert reserve(PROV, EP, Priority.EXIT, tmp_db) == 1

    assert clock["slept"], "the refusal must have been waited out, not raised"
    assert _last_call(tmp_db) - first >= EXIT_GAP_MS, (
        "waiting must not shorten the provider's spacing: the call went out "
        f"{_last_call(tmp_db) - first} ms after the previous one, under {EXIT_GAP_MS}"
    )


@pytest.mark.parametrize("priority", [Priority.UNRESOLVED, Priority.POSITION])
def test_position_and_unresolved_reads_wait_too(tmp_db, clock, priority):
    """copy_manager's holdings read is POSITION; reconcile is UNRESOLVED."""
    _take_slot(tmp_db)
    assert reserve(PROV, EP, priority, tmp_db) == 1
    assert clock["slept"]


@pytest.mark.parametrize("priority", [Priority.ENTRY, Priority.DISCOVERY, Priority.RESEARCH])
def test_lower_priorities_are_still_refused_at_once(tmp_db, clock, priority):
    """Only reads that protect money already at risk may wait. Entries and scans fail fast
    exactly as before -- the positive control that the interval still binds. The gap left
    is 50 ms, well inside any wait bound, so a lower priority that waited would show."""
    _take_slot(tmp_db)
    clock["ms"] += LIM.min_interval_ms - 50
    with pytest.raises(RateLimited, match="minimum interval") as caught:
        reserve(PROV, EP, priority, tmp_db)
    assert caught.value.retry_after_s == pytest.approx(0.05)
    assert clock["slept"] == []


def test_no_priority_waits_longer_than_a_higher_one():
    """A stop-loss read must never be treated worse than a lower-priority one."""
    waits = [INTERVAL_WAIT_S.get(p, 0.0) for p in sorted(Priority)]
    assert waits == sorted(waits, reverse=True), waits
    assert INTERVAL_WAIT_S.get(Priority.EXIT, 0.0) > 0


def test_a_penalty_escalated_interval_is_refused_without_sleeping(tmp_db, clock):
    """After a real 429 the penalty doubles the interval per level. That is the provider's
    own back-pressure and the wait must not sit through it: at level 4 the EXIT gap is
    4.4 s, past the bound, so the refusal is immediate and carries the true hint."""
    _take_slot(tmp_db)
    tmp_db.execute("UPDATE provider_state SET penalty_level=4 WHERE provider=?", (PROV,))
    clock["ms"] += 10
    with pytest.raises(RateLimited, match="minimum interval") as caught:
        reserve(PROV, EP, Priority.EXIT, tmp_db)
    assert caught.value.retry_after_s > INTERVAL_WAIT_S[Priority.EXIT]
    assert clock["slept"] == [], "slept into a refusal it could already see coming"


def test_an_exit_does_not_wait_through_a_provider_cooldown(tmp_db, clock):
    _take_slot(tmp_db)
    tmp_db.execute("UPDATE provider_state SET banned_until_ms=? WHERE provider=?",
                   (clock["ms"] + 500, PROV))
    with pytest.raises(RateLimited, match="provider cooldown"):
        reserve(PROV, EP, Priority.EXIT, tmp_db)
    assert clock["slept"] == []


def test_an_exit_does_not_wait_on_an_exhausted_bucket(tmp_db, clock):
    """Past the reserved overdraft the bucket refuses EXIT too. That is a budget, not a
    spacing, and it is not this mechanism's to wait out."""
    clock["ms"] += 10_000
    _take_slot(tmp_db)
    clock["ms"] += 2_000
    tmp_db.execute("UPDATE provider_state SET credit_milli=?, last_refill_ms=? WHERE provider=?",
                   (-limiter.EXIT_OVERDRAFT_WEIGHT * 1000, clock["ms"], PROV))
    with pytest.raises(RateLimited, match="bucket exhausted"):
        reserve(PROV, EP, Priority.EXIT, tmp_db)
    assert clock["slept"] == []


def test_no_wait_while_the_caller_holds_a_transaction(tmp_db, clock):
    """Sleeping inside someone's open transaction would hold THEIR write lock against
    every other service for the length of the wait."""
    _take_slot(tmp_db)
    clock["ms"] += 10
    tmp_db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(RateLimited, match="minimum interval"):
            reserve(PROV, EP, Priority.EXIT, tmp_db)
    finally:
        tmp_db.execute("ROLLBACK")
    assert clock["slept"] == []


def test_the_wait_is_bounded_when_someone_keeps_taking_the_slot(tmp_db, clock, monkeypatch):
    """Contention: every time this caller wakes, another EXIT has just gone. It must give
    up at the bound with the real refusal, not spin."""
    base_sleep = limiter._sleep

    def sleep_and_lose_the_race(seconds: float) -> None:
        base_sleep(seconds)
        tmp_db.execute("UPDATE provider_state SET last_call_ms=? WHERE provider=?",
                       (clock["ms"], PROV))

    monkeypatch.setattr(limiter, "_sleep", sleep_and_lose_the_race)
    _take_slot(tmp_db)
    started = clock["ms"]
    with pytest.raises(RateLimited, match="minimum interval"):
        reserve(PROV, EP, Priority.EXIT, tmp_db)
    waited_ms = clock["ms"] - started
    assert 0 < waited_ms <= INTERVAL_WAIT_S[Priority.EXIT] * 1000, waited_ms


def test_the_bound_fits_inside_the_tightest_protection_tick():
    """Two layers (DexScreener, then GMGN) may each wait the full bound inside one tick,
    and a tick slower than ``poll_interval_s`` counts toward the overrun halt that stops
    entries on every chain. The box runs 12 s; the model default is the tighter 5 s."""
    from kaiba.execution.protection import ProtectionConfig

    assert 2 * INTERVAL_WAIT_S[Priority.EXIT] < ProtectionConfig().poll_interval_s


# ------------------------------------------------------------------ the refusal parser


def _as_exit_detail(exc: Exception) -> str:
    """Exactly how `DefaultExitSubmitter._live` flattens a raised submit."""
    return f"{type(exc).__name__}: {exc}"[:300]


def _real_refusal(conn, clock, reason: str) -> RateLimited:
    """Have the real limiter produce each local refusal, rather than hand-building one."""
    if reason == "minimum interval":
        _take_slot(conn)
        clock["ms"] += 10
    elif reason == "max inflight":
        clock["ms"] += 10_000
        conn.execute("UPDATE provider_state SET inflight=? WHERE provider=?",
                     (LIM.max_inflight, PROV))
    elif reason == "bucket exhausted":
        clock["ms"] += 10_000
        conn.execute("UPDATE provider_state SET credit_milli=0, last_refill_ms=? "
                     "WHERE provider=?", (clock["ms"], PROV))
    with pytest.raises(RateLimited) as caught:
        reserve(PROV, EP, Priority.ENTRY, conn)
    assert caught.value.reason == reason
    return caught.value


@pytest.mark.parametrize("reason", sorted(LOCAL_TRANSIENT_REASONS))
def test_every_local_refusal_round_trips_through_the_exit_detail(tmp_db, clock, reason):
    if reason != "minimum interval":
        _take_slot(tmp_db)
    exc = _real_refusal(tmp_db, clock, reason)
    got = local_refusal_retry_s(_as_exit_detail(exc))
    assert got == pytest.approx(round(exc.retry_after_s, 1), abs=0.051), (reason, got)


@pytest.mark.parametrize("exc", [
    RateLimited("gmgn", "provider returned 429", 300),   # the VENUE's own limit
    RateLimited("gmgn", "trade cooldown", 120),          # our family ban after a 429
    RateLimited("gmgn", "provider cooldown", 60),
    RateLimited("helius", "daily credit cap", 3600),
])
def test_a_venue_limit_or_a_cooldown_is_not_a_local_refusal(exc):
    assert local_refusal_retry_s(_as_exit_detail(exc)) is None


@pytest.mark.parametrize("text", [
    None, "", "refused",
    "ExecutionRefused: gmgn-cli: venue answered HTTP 400 on /v1/trade/swap",
    "wallet holds none of 0xabc; refusing a zero-size sell",
    # our words quoted inside a venue's error are not our refusal
    "ExecutionRefused: venue said RateLimited: gmgn: minimum interval (retry in 0.1s)",
])
def test_anything_else_is_not_a_local_refusal(text):
    assert local_refusal_retry_s(text) is None


# ------------------------------------------------------------------ end to end


def test_two_concurrent_exit_price_reads_both_get_a_price(tmp_db, clock, monkeypatch):
    """The measured failure, through the real DexScreener stack: two positions priced in
    the same instant at EXIT. Before, the second came back "rate limited: dexscreener:
    minimum interval"; that position was blind for the tick."""
    import httpx

    from kaiba.providers import dexscreener as ds
    from kaiba.providers import prices
    from tests.test_dexscreener import BONK, WSOL, Recorder, fixture

    real = limiter.limits_for
    monkeypatch.setattr(limiter, "limits_for", lambda p: LIM if p == ds.PROVIDER else real(p))
    http = Recorder()
    http.add(f"/token-pairs/v1/solana/{BONK}", fixture("token_pairs_solana_bonk"))
    http.add(f"/token-pairs/v1/solana/{WSOL}", fixture("token_pairs_solana_wsol"))
    monkeypatch.setattr(httpx, "request", http)
    prices.reset_sources()
    try:
        from kaiba.core.schemas import Chain

        a = prices.quote(Chain.SOL, BONK, priority=Priority.EXIT, conn=tmp_db)
        b = prices.quote(Chain.SOL, WSOL, priority=Priority.EXIT, conn=tmp_db)
        assert a.known, a.receipt.note
        assert b.known, f"second concurrent EXIT read went blind: {b.receipt.note}"
        assert len(http.calls) == 2

        # Positive control: the same pair of reads at ENTRY still refuses the second.
        http.calls.clear()
        clock["ms"] += 60_000
        c = prices.quote(Chain.SOL, BONK, priority=Priority.ENTRY, conn=tmp_db, max_age_s=0)
        d = prices.quote(Chain.SOL, WSOL, priority=Priority.ENTRY, conn=tmp_db, max_age_s=0)
        assert c.known
        assert not d.known and "minimum interval" in (d.receipt.note or "")
    finally:
        prices.reset_sources()
