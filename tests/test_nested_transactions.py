"""A transaction inside a transaction must nest, not explode -- and must never leak a slot.

FOUND LIVE 2026-09-22 on the box: ``limiter release failed for pumpfun: cannot start a
transaction within a transaction``, 30 times in three hours, in kaiba-scan. Two distinct
defects behind one log line, and the quiet one is the worse of the two.

1. **The slot leaks.** :func:`limiter.release` opens with ``BEGIN IMMEDIATE``. When the
   caller already holds a transaction that raises immediately, so the
   ``inflight = inflight - 1`` never runs. Every such call permanently burns one of the
   provider's ``max_inflight`` slots (pumpfun has 2). Once they are all burned the
   provider is throttled to a standstill until the process restarts -- which is exactly
   the "leaked provider slots" the operator was told about, still leaking.

2. **The caller's writes are discarded.** The failure handler calls ``ROLLBACK``
   unconditionally. There is no transaction of ours to roll back -- so it rolls back the
   CALLER's, throwing away whatever they had written and not yet committed, and the
   caller is never told. A logged warning about a rate limiter is not a plausible place
   to go looking for missing rows.

``db.tx`` has the identical shape, so this is not a limiter bug: it is every nested use
of either helper. The fix is a SAVEPOINT when a transaction is already open -- it nests,
and rolling back to it undoes our work only.
"""

from __future__ import annotations

import sqlite3

import pytest

from kaiba.core import limiter as L
from kaiba.core.db import tx


@pytest.fixture
def conn(tmp_db):
    tmp_db.execute("CREATE TABLE IF NOT EXISTS probe (k TEXT PRIMARY KEY, v TEXT)")
    tmp_db.commit()
    return tmp_db


# ------------------------------------------------------------------ db.tx nests


def test_a_nested_tx_does_not_raise(conn):
    with tx(conn):
        conn.execute("INSERT INTO probe VALUES ('outer','1')")
        with tx(conn):
            conn.execute("INSERT INTO probe VALUES ('inner','1')")
    assert {r[0] for r in conn.execute("SELECT k FROM probe")} == {"outer", "inner"}


def test_an_inner_rollback_keeps_the_outer_work(conn):
    """The whole point of a savepoint: undo ours, leave theirs."""
    with tx(conn):
        conn.execute("INSERT INTO probe VALUES ('outer','1')")
        with pytest.raises(RuntimeError):
            with tx(conn):
                conn.execute("INSERT INTO probe VALUES ('inner','1')")
                raise RuntimeError("inner fails")
        # the outer transaction is still alive and still usable
        conn.execute("INSERT INTO probe VALUES ('after','1')")
    assert {r[0] for r in conn.execute("SELECT k FROM probe")} == {"outer", "after"}


def test_an_outer_rollback_still_discards_everything(conn):
    with pytest.raises(RuntimeError):
        with tx(conn):
            conn.execute("INSERT INTO probe VALUES ('outer','1')")
            with tx(conn):
                conn.execute("INSERT INTO probe VALUES ('inner','1')")
            raise RuntimeError("outer fails")
    assert conn.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0


def test_tx_still_commits_on_its_own(conn):
    with tx(conn):
        conn.execute("INSERT INTO probe VALUES ('solo','1')")
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 1


def test_three_deep_still_nests(conn):
    with tx(conn), tx(conn), tx(conn):
        conn.execute("INSERT INTO probe VALUES ('deep','1')")
    assert conn.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 1


# ------------------------------------------------------------------ the slot never leaks


def inflight(conn, provider: str = "pumpfun") -> int:
    row = conn.execute("SELECT inflight FROM provider_state WHERE provider=?", (provider,)).fetchone()
    return int(row[0]) if row else 0


def test_release_inside_a_caller_transaction_still_frees_the_slot(conn):
    """The live bug, reproduced. A release that cannot run leaves the slot burned."""
    L.reserve("pumpfun", "test.endpoint", conn=conn)
    assert inflight(conn) == 1
    with tx(conn):                      # the caller's transaction, as kaiba-scan holds one
        conn.execute("INSERT INTO probe VALUES ('callers_write','1')")
        L.release("pumpfun", "test.endpoint", conn=conn)
        assert inflight(conn) == 0, "the slot must be released inside a caller's transaction"
    # ...and the caller's own write survived the release.
    assert conn.execute("SELECT COUNT(*) FROM probe WHERE k='callers_write'").fetchone()[0] == 1


def test_release_does_not_roll_back_the_callers_writes(conn):
    """Defect 2: an unconditional ROLLBACK silently discarded the caller's rows."""
    L.reserve("pumpfun", "test.endpoint", conn=conn)
    with tx(conn):
        conn.execute("INSERT INTO probe VALUES ('before_release','1')")
        L.release("pumpfun", "test.endpoint", status="rate_limited", retry_after_s=1.0, conn=conn)
        conn.execute("INSERT INTO probe VALUES ('after_release','1')")
    got = {r[0] for r in conn.execute("SELECT k FROM probe")}
    assert got == {"before_release", "after_release"}, got


def test_repeated_nested_releases_do_not_exhaust_the_provider(conn):
    """The failure mode that actually hurt: inflight ratchets up and never comes down."""
    for _ in range(6):
        # Clear the pacing clock between iterations. The minimum-interval rule is real and
        # tested elsewhere; here it would just stop the loop before it could demonstrate
        # the ratchet, which is what this test is about.
        conn.execute("UPDATE provider_state SET last_call_ms=0 WHERE provider='pumpfun'")
        L.reserve("pumpfun", "test.endpoint", conn=conn)
        with tx(conn):
            L.release("pumpfun", "test.endpoint", conn=conn)
    assert inflight(conn) == 0, "six reserve/release pairs must leave no slots held"


def test_reserve_inside_a_caller_transaction_works(conn):
    """`reserve` carries the same unconditional BEGIN, so it has the same defect."""
    with tx(conn):
        conn.execute("INSERT INTO probe VALUES ('k','1')")
        L.reserve("pumpfun", "test.endpoint", conn=conn)
        assert inflight(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM probe WHERE k='k'").fetchone()[0] == 1


def test_a_refused_reserve_inside_a_transaction_keeps_the_callers_work(conn):
    """A cooldown refusal must not take the caller's rows down with it."""
    conn.execute("INSERT OR REPLACE INTO provider_state (provider, banned_until_ms, "
                 "last_refill_ms, last_call_ms) VALUES (?,?,0,0)",
                 ("pumpfun", 4_000_000_000_000))
    conn.commit()
    with tx(conn):
        conn.execute("INSERT INTO probe VALUES ('survives','1')")
        with pytest.raises(L.RateLimited):
            L.reserve("pumpfun", "test.endpoint", conn=conn)
        conn.execute("INSERT INTO probe VALUES ('also_survives','1')")
    got = {r[0] for r in conn.execute("SELECT k FROM probe")}
    assert got == {"survives", "also_survives"}, got


def test_a_failed_inner_block_pops_its_savepoint(conn):
    """``ROLLBACK TO`` rewinds to a savepoint; it does NOT remove it. Only ``RELEASE`` does.

    Missed by every other test here, because the leak is invisible in the data: the rows
    are correct either way. What grows is SQLite's savepoint stack, one frame per failed
    inner block, for as long as the outer transaction lives -- and the long-lived writers
    are exactly the ones that nest (the scanner holds a transaction across a whole sweep).

    Traced through the statements actually issued, since the stack has no pragma.
    """
    issued: list[str] = []
    conn.set_trace_callback(issued.append)
    try:
        with tx(conn):
            for i in range(3):
                with pytest.raises(RuntimeError):
                    with tx(conn):
                        conn.execute(f"INSERT INTO probe VALUES ('doomed{i}','1')")
                        raise RuntimeError("inner fails")
            conn.execute("INSERT INTO probe VALUES ('kept','1')")
    finally:
        conn.set_trace_callback(None)

    rolled = [s.split()[-1] for s in issued if s.upper().startswith("ROLLBACK TO")]
    released = [s.split()[-1] for s in issued if s.upper().startswith("RELEASE")]
    assert len(rolled) == 3, issued
    assert rolled == released, "each ROLLBACK TO must be paired with a RELEASE of that savepoint"
    assert {r[0] for r in conn.execute("SELECT k FROM probe")} == {"kept"}


def test_a_successful_inner_block_pops_its_savepoint_too(conn):
    issued: list[str] = []
    conn.set_trace_callback(issued.append)
    try:
        with tx(conn):
            for i in range(3):
                with tx(conn):
                    conn.execute(f"INSERT INTO probe VALUES ('ok{i}','1')")
    finally:
        conn.set_trace_callback(None)
    saved = [s.split()[-1] for s in issued if s.upper().startswith("SAVEPOINT")]
    released = [s.split()[-1] for s in issued if s.upper().startswith("RELEASE")]
    assert len(saved) == 3 and saved == released, issued


# ------------------------------------------------------- the check-then-act race


class _RacedConnection:
    """A connection that reports ``in_transaction`` False while a transaction IS open.

    Exactly what a shared connection looks like to a thread that read the flag an instant
    before another thread issued its ``BEGIN``. Our connections are shared: ``connect``
    passes ``check_same_thread=False`` and the watchdog prices positions across eight
    workers on one handle.
    """

    def __init__(self, real):
        self._real = real

    @property
    def in_transaction(self):
        return False

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_losing_the_begin_race_falls_back_to_a_savepoint(conn):
    """MEASURED live 2026-09-22: the first version of this fix still leaked slots here.

    Predicting the BEGIN by reading a flag is a check-then-act race. This pins the
    behaviour that replaced it -- attempt the BEGIN, and take its refusal as the answer.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        with tx(_RacedConnection(conn)):
            conn.execute("INSERT INTO probe VALUES ('raced','1')")
        assert conn.in_transaction, "the enclosing transaction must survive"
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    assert conn.execute("SELECT COUNT(*) FROM probe WHERE k='raced'").fetchone()[0] == 1


def test_a_release_that_loses_the_race_still_frees_the_slot(conn):
    """The live symptom: a lost race left `inflight` permanently one higher."""
    L.reserve("pumpfun", "test.endpoint", conn=conn)
    assert inflight(conn) == 1
    conn.execute("BEGIN IMMEDIATE")
    L.release("pumpfun", "test.endpoint", conn=_RacedConnection(conn))
    conn.execute("COMMIT")
    assert inflight(conn) == 0, "a lost BEGIN race must not burn the slot"


def test_an_unrelated_operational_error_is_not_swallowed(conn):
    """Only the 'already in a transaction' refusal means 'take the savepoint path'."""
    class _Broken(_RacedConnection):
        def execute(self, sql, *a):
            if sql.startswith("BEGIN"):
                raise sqlite3.OperationalError("database is locked")
            return self._real.execute(sql, *a)

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        with tx(_Broken(conn)):
            pass


# ----------------------------------------- the slot must outlive the caller's transaction


#: A "busy" provider for these tests: its last call is recent enough that the EXISTING
#: stale-slot reclaim cannot fire (that one needs `last_call_ms` older than
#: ``INFLIGHT_STALE_MS``, 120 s), but old enough that the minimum-interval rule does not
#: refuse the reserve before it ever reaches the in-flight check. 30 s satisfies both.
BUSY_GAP_MS = 30_000


def test_a_busy_providers_leaked_slots_are_reclaimed(conn):
    """The gap in the existing reclaim: it only rescues an IDLE provider.

    `reserve` already reclaims when `last_call_ms` is older than any call could still be
    running. That fixed a hard-restart case in 2026-09-21 and it is a good rule, but it
    keys off the time of the LAST CALL -- and a provider under constant traffic refreshes
    that on every reserve. So a busy provider with leaked slots never satisfies it, and a
    busy provider is exactly the one whose slots matter.

    MEASURED 2026-09-22: pumpfun sat at 68 slots against a maximum of 2, and rpc at 27
    against 6, for hours, while both were serving hundreds of calls a minute. Every
    non-EXIT call to either was refused. The existing reclaim never fired once, because
    `last_call_ms` was never more than a second old.

    The lease keys off how long the count has been ABOVE THE CAP instead, which traffic
    cannot reset.
    """
    lim = L.limits_for("pumpfun")
    now = L.now_ms()
    over_since = now - (L.INFLIGHT_LEASE_S + 60) * 1000
    conn.execute("INSERT OR REPLACE INTO provider_state (provider, inflight, last_refill_ms, "
                 "last_call_ms, inflight_over_since_ms) VALUES ('pumpfun', ?, ?, ?, ?)",
                 (68, now, now - BUSY_GAP_MS, over_since))
    conn.commit()
    assert inflight(conn) > lim.max_inflight

    L.reserve("pumpfun", "test.endpoint", priority=L.Priority.EXIT, conn=conn)

    assert inflight(conn) <= lim.max_inflight + 1, (
        f"a busy provider's stuck slot count must still be reclaimed, got {inflight(conn)}"
    )


def test_a_brief_excess_from_an_exit_bypass_is_not_reclaimed(conn):
    """An EXIT may legitimately push past the cap. Only a PERSISTENT excess is a leak."""
    now = L.now_ms()
    conn.execute("INSERT OR REPLACE INTO provider_state (provider, inflight, last_refill_ms, "
                 "last_call_ms, inflight_over_since_ms) VALUES ('pumpfun', ?, ?, ?, ?)",
                 (5, now, now - BUSY_GAP_MS, now))
    conn.commit()
    L.reserve("pumpfun", "test.endpoint", priority=L.Priority.EXIT, conn=conn)
    assert inflight(conn) >= 5, "a fresh excess is load, not a leak, and must be left alone"


def test_the_lease_starts_counting_the_first_time_the_cap_is_passed(conn):
    """Nothing can expire until something records when the excess began."""
    now = L.now_ms()
    conn.execute("INSERT OR REPLACE INTO provider_state (provider, inflight, last_refill_ms, "
                 "last_call_ms, inflight_over_since_ms) VALUES ('pumpfun', 9, ?, ?, NULL)",
                 (now, now - BUSY_GAP_MS))
    conn.commit()
    L.reserve("pumpfun", "test.endpoint", priority=L.Priority.EXIT, conn=conn)
    stamped = conn.execute("SELECT inflight_over_since_ms FROM provider_state "
                           "WHERE provider='pumpfun'").fetchone()[0]
    assert stamped is not None, "the first over-cap sighting must start the lease clock"


def test_returning_under_the_cap_clears_the_lease(conn):
    """Otherwise a provider that recovered would be reclaimed on its next busy spell."""
    now = L.now_ms()
    # Seed bucket credit too: this reserve must actually SUCCEED to prove the lease is
    # cleared on the way through, and an empty bucket would refuse it first.
    conn.execute("INSERT OR REPLACE INTO provider_state (provider, inflight, last_refill_ms, "
                 "last_call_ms, credit_milli, inflight_over_since_ms) "
                 "VALUES ('pumpfun', 0, ?, ?, 60000, ?)",
                 (now, now - BUSY_GAP_MS, now - 10_000))
    conn.commit()
    L.reserve("pumpfun", "test.endpoint", conn=conn)
    stamped = conn.execute("SELECT inflight_over_since_ms FROM provider_state "
                           "WHERE provider='pumpfun'").fetchone()[0]
    assert stamped is None, "back under the cap means the lease clock must reset"


def test_a_provider_stuck_exactly_at_its_cap_is_reclaimed(conn):
    """MEASURED 2026-09-29: dexscreener at exactly 2 of 2 after its holders were killed.

    The over-cap lease never started (2 is not over 2) and the stale rule never fired (the
    watchdog's EXIT reads refresh `last_call_ms` every 12 s), so every non-EXIT caller --
    native_price among them -- was refused "max inflight" indefinitely.
    """
    lim = L.limits_for("pumpfun")
    now = L.now_ms()
    conn.execute("INSERT OR REPLACE INTO provider_state (provider, inflight, last_refill_ms, "
                 "last_call_ms, credit_milli, inflight_over_since_ms) VALUES ('pumpfun', ?, ?, ?, 60000, ?)",
                 (lim.max_inflight, now, now - BUSY_GAP_MS, now - (L.INFLIGHT_LEASE_S + 60) * 1000))
    conn.commit()

    L.reserve("pumpfun", "test.endpoint", conn=conn)  # normal priority: would be refused

    assert inflight(conn) == 1, "the dead holders' slots were not reclaimed"


def test_a_fresh_full_house_is_load_not_a_leak(conn):
    """At the cap inside the lease, normal callers are still refused: that is load."""
    lim = L.limits_for("pumpfun")
    now = L.now_ms()
    conn.execute("INSERT OR REPLACE INTO provider_state (provider, inflight, last_refill_ms, "
                 "last_call_ms, credit_milli, inflight_over_since_ms) VALUES ('pumpfun', ?, ?, ?, 60000, ?)",
                 (lim.max_inflight, now, now - BUSY_GAP_MS, now - 10_000))
    conn.commit()
    with pytest.raises(L.RateLimited, match="max inflight"):
        L.reserve("pumpfun", "test.endpoint", conn=conn)
    assert inflight(conn) == lim.max_inflight


def test_reaching_the_cap_starts_the_lease_clock(conn):
    """The EXIT reads that keep a leaked provider busy are what start the clock.

    A refused reserve rolls its whole transaction back, stamp included, so the clock is
    started by a reserve that goes through -- in production, the watchdog's EXIT reads.
    """
    lim = L.limits_for("pumpfun")
    now = L.now_ms()
    conn.execute("INSERT OR REPLACE INTO provider_state (provider, inflight, last_refill_ms, "
                 "last_call_ms, credit_milli, inflight_over_since_ms) VALUES ('pumpfun', ?, ?, ?, 60000, NULL)",
                 (lim.max_inflight, now, now - BUSY_GAP_MS))
    conn.commit()
    L.reserve("pumpfun", "test.endpoint", priority=L.Priority.EXIT, conn=conn)
    stamped = conn.execute("SELECT inflight_over_since_ms FROM provider_state "
                           "WHERE provider='pumpfun'").fetchone()[0]
    assert stamped is not None, "reaching the cap must start the lease clock"


def test_concurrent_releases_on_one_shared_connection_do_not_leak(conn):
    """Eight workers on one handle is the watchdog's prefetch, and it is where this broke.

    Savepoints nest but they do NOT isolate threads: two workers interleaving
    SAVEPOINT/RELEASE on one connection unwind each other's frames, which is what produced
    the `no such savepoint` errors and the lost decrements behind them.
    """
    import threading

    workers, each = 4, 5
    # Seed the counter directly rather than through `reserve`: pumpfun's max_inflight is 2,
    # so twenty real reservations would be refused by the very cap this test is about.
    conn.execute("INSERT OR REPLACE INTO provider_state (provider, inflight, last_refill_ms, "
                 "last_call_ms) VALUES ('pumpfun', ?, 0, 0)", (workers * each,))
    conn.commit()
    assert inflight(conn) == workers * each

    errors: list[Exception] = []

    def work():
        for _ in range(each):
            try:
                L.release("pumpfun", "test.endpoint", conn=conn)
            except Exception as exc:  # noqa: BLE001 - collected and asserted below
                errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], errors
    assert inflight(conn) == 0, f"{inflight(conn)} slots leaked across {workers} threads"
