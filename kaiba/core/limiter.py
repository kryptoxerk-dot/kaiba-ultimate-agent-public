"""Shared provider rate limiter.

Every outbound provider call goes through here. The state lives in SQLite rather than in
process memory because the ingest services, the CLI and the dashboard all call the same
providers with the same key, and GMGN in particular locks the whole IP when an account
exceeds its allowance.

Design points, learned from the prior repo's provider governor:

* Capacity is charged **at reservation**, never refunded. An error still cost the provider
  a request, so pretending otherwise is how you get IP-banned.
* A 429 opens a cooldown for the *endpoint family*, honouring ``x-ratelimit-reset`` when
  present, and raises a blanket penalty level that doubles the minimum interval.
* Priorities exist so that exits and unresolved orders keep working while discovery and
  research starve.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum

from kaiba.core.config import get_risk
from kaiba.core.db import get_conn, tx
from kaiba.core.redact import redact_text
from kaiba.core.schemas import now_ms

log = logging.getLogger(__name__)


#: Credit an EXIT may spend below zero, in weight units. Two swaps' worth on the gmgn
#: budget (swap=10). This is the reserved floor that keeps a stop-loss reachable when
#: entries have drained the bucket -- and only Priority.EXIT may spend it, so discovery
#: and entries cannot borrow against it. INVENTED as a size; MEASURED as a need: six
#: gmgn swaps drain the bucket in 1.92 s and the seventh is refused, exits included.
EXIT_OVERDRAFT_WEIGHT = 20


class Priority(IntEnum):
    """Lower runs first when capacity is scarce."""

    EXIT = 0
    UNRESOLVED = 1
    POSITION = 2
    ENTRY = 3
    DISCOVERY = 4
    RESEARCH = 5


class RateLimited(Exception):
    """Raised when the limiter refuses. ``retry_after_s`` is a hint, not a promise."""

    def __init__(self, provider: str, reason: str, retry_after_s: float = 1.0) -> None:
        super().__init__(f"{provider}: {reason} (retry in {retry_after_s:.1f}s)")
        self.provider = provider
        self.reason = reason
        self.retry_after_s = retry_after_s


#: How long :func:`reserve` may WAIT OUT its own minimum-interval refusal before raising,
#: by priority. Priorities not listed (ENTRY, DISCOVERY, RESEARCH) never wait here.
#:
#: MEASURED 2026-10-01 on the box, 24 h: 883 of 1,967 `protection_blind` events on
#: robinhood (45%) read "rate limited: dexscreener: minimum interval", on five tokens
#: DexScreener prices fine (they were never blind for any other reason). 75% of all
#: dexscreener calls land inside protection's own ticks, which cover 9% of the clock: the
#: prefetch workers price the book concurrently and each EXIT read after the first is
#: refused by the 275 ms gap the previous one just opened. The refusal said "retry in
#: 0.2s" and the read was abandoned for the 12 s tick. The same shape refused
#: copy_manager's POSITION-priority gmgn holdings read 21 times in 24 h ("retry in
#: 0.0-0.5s"), and two live robinhood sells in 7.6 days (gmgn, "retry in 0.0-0.1s").
#:
#: Waiting out the interval is not bypassing it: the call still goes no sooner than the
#: provider's spacing allows. What this changes is only that a sub-second gap no longer
#: costs a whole tick. Bounded, so a penalty-escalated interval after a real 429 (doubled
#: per level) still refuses at once -- the provider's own back-pressure is never overridden.
#: The EXIT bound is INVENTED as a size; it admits ~5 serialised dexscreener EXIT reads
#: (275 ms apart) inside a 12 s protection tick whose p90 is 974 ms.
INTERVAL_WAIT_S: dict[Priority, float] = {
    Priority.EXIT: 1.5,
    Priority.UNRESOLVED: 1.0,
    Priority.POSITION: 1.0,
}

#: Refusals our limiter makes BEFORE anything reaches the provider, which clear on their
#: own within seconds. A provider's own 429 is a different reason string and never in here.
LOCAL_TRANSIENT_REASONS: frozenset[str] = frozenset(
    {"minimum interval", "max inflight", "bucket exhausted"}
)

_REFUSAL_TEXT = re.compile(
    r"RateLimited: (?P<provider>[^:\s]+): (?P<reason>[^()]+?) \(retry in (?P<retry>[0-9.]+)s\)"
)


def local_refusal_retry_s(text: str | None) -> float | None:
    """``retry_after_s`` when ``text`` reports one of OUR transient refusals, else ``None``.

    ``text`` is a :class:`RateLimited` rendered as ``f"{type(exc).__name__}: {exc}"``,
    which is how the exit submitter flattens every failure into ``ExitOutcome.detail``.
    ``None`` for anything else, including a provider's own 429 ("provider returned 429")
    and our cooldowns: those must keep their full backoff. The format is pinned by a
    round-trip test against :class:`RateLimited` itself, so a change to the message breaks
    a test rather than silently turning every limiter refusal back into a venue failure.
    """
    if not text:
        return None
    # Anchored: the class name leads the detail when the submit itself raised. The same
    # words quoted somewhere inside a venue's error text are not our refusal.
    found = _REFUSAL_TEXT.match(text.strip())
    if found is None or found.group("reason").strip() not in LOCAL_TRANSIENT_REASONS:
        return None
    try:
        return max(0.0, float(found.group("retry")))
    except ValueError:
        return None


#: Seams so a test can drive the wait against a fake clock. Production uses the real ones.
_sleep = time.sleep
_monotonic = time.monotonic


@dataclass(frozen=True)
class Limits:
    min_interval_ms: int = 1000
    capacity: int = 10
    refill_per_s: float = 1.0
    max_inflight: int = 2
    ban_floor_s: int = 60
    ban_ceiling_s: int = 3600
    daily_credit_cap: int | None = None
    weights: dict[str, int] | None = None

    def weight_for(self, endpoint: str) -> int:
        """Charge by endpoint, falling back to its bare name, then to the default.

        The fallback matters: the contract asks for ``family.name`` endpoint strings, so
        the executor reserves ``trade.swap`` while the weight table is keyed ``swap``.
        Without this, a swap was charged weight 1 instead of 10 — the system's single most
        expensive call was billed as its cheapest, which is how a real 429 arrives while
        the limiter still believes it has headroom.
        """
        w = self.weights or {}
        if endpoint in w:
            return int(w[endpoint])
        name = endpoint.rsplit(".", 1)[-1]
        return int(w.get(name, w.get("default", 1)))


DEFAULTS: dict[str, Limits] = {
    # GMGN Free allows weight 5; quote and swap cost 10 each, so execution needs Plus.
    # Capacity is set to that allowance rather than above it. The earlier 20/2.0 let a
    # recorded session sail past a real ``HTTP 429 RATE_LIMIT_EXCEEDED`` without the
    # limiter objecting once, which is worse than useless: it is a budget that reports
    # headroom it does not have. Raise both numbers when the account is on Plus.
    "gmgn": Limits(min_interval_ms=1200, capacity=10, refill_per_s=0.2, max_inflight=1,
                   weights={"quote": 10, "swap": 10, "default": 1}),
    "helius": Limits(min_interval_ms=100, capacity=50, refill_per_s=10.0, max_inflight=4,
                     daily_credit_cap=900_000),
    "dexscreener": Limits(min_interval_ms=1100, capacity=60, refill_per_s=1.0, max_inflight=2),
    "solanatracker": Limits(min_interval_ms=350, capacity=3, refill_per_s=3.0, max_inflight=2),
    "rugcheck": Limits(min_interval_ms=500, capacity=10, refill_per_s=2.0, max_inflight=2),
    "goplus": Limits(min_interval_ms=500, capacity=30, refill_per_s=2.0, max_inflight=2),
    "jupiter": Limits(min_interval_ms=1100, capacity=5, refill_per_s=1.0, max_inflight=2),
    "pumpportal": Limits(min_interval_ms=200, capacity=20, refill_per_s=5.0, max_inflight=2),
    "birdeye": Limits(min_interval_ms=1000, capacity=10, refill_per_s=1.0, max_inflight=2),
    "bitquery": Limits(min_interval_ms=700, capacity=10, refill_per_s=1.5, max_inflight=2),
    "coingecko": Limits(min_interval_ms=2500, capacity=10, refill_per_s=0.4, max_inflight=1),
    "alchemy": Limits(min_interval_ms=100, capacity=25, refill_per_s=10.0, max_inflight=4),
    "rpc": Limits(min_interval_ms=50, capacity=40, refill_per_s=20.0, max_inflight=6),
}


def limits_for(provider: str) -> Limits:
    """Config overrides win over the built-in defaults."""
    base = DEFAULTS.get(provider, Limits())
    cfg = (get_risk().provider_budgets or {}).get(provider)
    if not cfg:
        return base
    return Limits(
        min_interval_ms=int(cfg.get("min_interval_ms", base.min_interval_ms)),
        capacity=int(cfg.get("capacity", base.capacity)),
        refill_per_s=float(cfg.get("refill_per_s", base.refill_per_s)),
        max_inflight=int(cfg.get("max_inflight", base.max_inflight)),
        ban_floor_s=int(cfg.get("ban_floor_s", base.ban_floor_s)),
        ban_ceiling_s=int(cfg.get("ban_ceiling_s", base.ban_ceiling_s)),
        daily_credit_cap=cfg.get("daily_credit_cap", base.daily_credit_cap),
        weights=cfg.get("weights", base.weights),
    )


def _day_key() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


#: An inflight slot whose last call is older than this belongs to a process that died
#: before it could release it. Persisted state plus a hard kill equals a permanent
#: deadlock otherwise -- see the reclamation in `reserve`. 120 s is comfortably longer
#: than any provider call this tree makes (the shared HTTP timeout is 20 s) and short
#: enough that a stall is measured in minutes rather than discovered by hand.
INFLIGHT_STALE_MS = 120_000


def _state(conn: sqlite3.Connection, provider: str) -> dict:
    row = conn.execute("SELECT * FROM provider_state WHERE provider=?", (provider,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT OR IGNORE INTO provider_state "
            "(provider, credit_milli, last_refill_ms, last_call_ms, inflight, banned_until_ms, "
            " penalty_level, spent_today, day_key) VALUES (?,?,?,?,?,?,?,?,?)",
            (provider, limits_for(provider).capacity * 1000, now_ms(), 0, 0, 0, 0, 0, _day_key()),
        )
        row = conn.execute("SELECT * FROM provider_state WHERE provider=?", (provider,)).fetchone()
    return dict(row)


def _family(endpoint: str) -> str:
    return endpoint.split(".", 1)[0].split("/", 1)[0] or "default"


def family_banned_until(conn: sqlite3.Connection, provider: str, endpoint: str) -> int:
    row = conn.execute(
        "SELECT banned_until_ms FROM provider_family_bans WHERE provider=? AND family=?",
        (provider, _family(endpoint)),
    ).fetchone()
    return int(row["banned_until_ms"]) if row else 0


#: How long an in-flight count may sit AT OR ABOVE a provider's cap, never once freeing a
#: slot, before it is treated as a leak rather than load. An EXIT bypasses the cap deliberately, so a brief excess is
#: legitimate; a lasting one is not, because `reserve` refuses above the cap.
#:
#: 120 s is INVENTED as a duration; the need is MEASURED. It is 12x the 10 s HTTP timeout
#: in `providers/_http.py`, so no real call can still be running when it expires, and it
#: is short enough that a leak costs minutes rather than the days these counts had
#: actually been accumulating for.
INFLIGHT_LEASE_S = 120


def _reclaim_leaked_slots(
    conn: sqlite3.Connection, provider: str, lim: Limits, state: dict, ts: int
) -> int:
    """Return `inflight`, having reset it if it has been over the cap past the lease.

    See ``migrations/030_slot_lease.sql`` for why the decrement cannot simply be made
    reliable. This is the backstop that keeps a lost one from being permanent.
    """
    inflight = int(state.get("inflight") or 0)
    cap = int(lim.max_inflight)
    # The lease runs while the count is AT or over the cap, not only over it. MEASURED
    # 2026-09-29: dexscreener sat at exactly 2 of 2 for as long as anyone looked, after
    # its holders were killed (an OOM and two restarts). Nothing reclaimed it: the stale
    # rule never fires because the watchdog's EXIT reads bypass the cap and refresh
    # `last_call_ms` every 12 s, and the over-cap lease never started because 2 is not
    # over 2. Every non-EXIT caller was refused "max inflight"; native_price, which every
    # entry size needs, timed out on every run. A count that never once drops below the
    # cap in a full lease -- when no call this tree makes lasts over 20 s -- is held by
    # the dead, so the whole count is reclaimed, not just the excess.
    if inflight < cap:
        if state.get("inflight_over_since_ms") is not None:
            conn.execute("UPDATE provider_state SET inflight_over_since_ms=NULL WHERE provider=?",
                         (provider,))
        return inflight
    since = state.get("inflight_over_since_ms")
    if since is None:
        conn.execute("UPDATE provider_state SET inflight_over_since_ms=? WHERE provider=?",
                     (ts, provider))
        return inflight
    if ts - int(since) < INFLIGHT_LEASE_S * 1000:
        return inflight
    log.warning(
        "%s held %d slots against a cap of %d without freeing one for over %ds; "
        "reclaiming them as leaked by a dead holder",
        provider, inflight, cap, INFLIGHT_LEASE_S,
    )
    conn.execute("UPDATE provider_state SET inflight=0, inflight_over_since_ms=NULL "
                 "WHERE provider=?", (provider,))
    return 0


def _accounting_conn(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    """The connection the limiter does its OWN bookkeeping on. Never a shared one.

    Provider slots are GLOBAL state, not part of any caller's unit of work, and treating
    them as part of one caused two separate production failures on 2026-09-22:

    * **The caller's rollback re-took the slot.** The decrement joined whatever
      transaction the caller had open, so any later failure in that transaction silently
      undid it. Nothing logged: from here the release had succeeded.
    * **Savepoints do not isolate threads.** The watchdog prices positions across eight
      workers on ONE handle. Two workers interleaving SAVEPOINT/RELEASE on that handle
      unwind each other's frames -- 41 `no such savepoint` errors in twelve minutes, each
      one a lost decrement. pumpfun ended at 68 slots held against a maximum of 2, and
      rpc at 27 against 6, which throttles the whole agent to a standstill.

    ``db.get_conn`` is thread-local, so each thread does its bookkeeping on its own
    connection and none of it can be rolled back by anyone else. Under test it is
    monkeypatched to the test's own connection, which is single-threaded, so the
    behaviour there is unchanged.
    """
    try:
        from kaiba.core.db import get_conn as _thread_conn

        return _thread_conn()
    except Exception:  # noqa: BLE001 - a caller's connection beats no connection at all
        if conn is None:
            raise
        return conn


def reserve(
    provider: str,
    endpoint: str,
    priority: Priority = Priority.RESEARCH,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Charge capacity for one call. Raises :class:`RateLimited` if we must not call.

    Returns the weight charged, which the caller passes back to :func:`release`.

    A minimum-interval refusal at a priority in :data:`INTERVAL_WAIT_S` is waited out, up
    to that bound, instead of raised -- see the measurement there. The sleep happens
    BETWEEN attempts, never inside one: each attempt is its own short transaction, so a
    waiting caller holds no lock. A caller that is already inside a transaction on the
    accounting connection is never made to wait, because sleeping there would hold ITS
    write lock against every other service for the length of the wait.
    """
    c = _accounting_conn(conn)
    budget = INTERVAL_WAIT_S.get(priority, 0.0)
    # Read as a hint, not a lock decision: on the thread-local connection production uses
    # it is this thread's own state. On a connection shared across threads either answer
    # is safe -- True only skips the wait (the old behaviour), and a wait holds nothing.
    if budget <= 0 or getattr(c, "in_transaction", False):
        return _reserve_once(c, provider, endpoint, priority)
    deadline = _monotonic() + budget
    while True:
        try:
            return _reserve_once(c, provider, endpoint, priority)
        except RateLimited as exc:
            if exc.reason != "minimum interval":
                raise
            remaining = deadline - _monotonic()
            # Sleeping into a refusal we can already see coming only adds latency to it.
            if remaining <= 0 or exc.retry_after_s > remaining:
                raise
            # +1 ms because `now_ms` is whole milliseconds: waking exactly on the boundary
            # can still read one millisecond short of the interval.
            _sleep(min(remaining, max(exc.retry_after_s, 0.0) + 0.001))
            log.debug("%s %s: %s waiting %.0f ms out for the minimum interval",
                      provider, endpoint, getattr(priority, "name", priority),
                      exc.retry_after_s * 1000)


def _reserve_once(
    c: sqlite3.Connection,
    provider: str,
    endpoint: str,
    priority: Priority,
) -> int:
    """One reservation attempt in one transaction. Raises :class:`RateLimited` on refusal."""
    lim = limits_for(provider)
    weight = lim.weight_for(endpoint)
    ts = now_ms()

    # `tx` nests (SAVEPOINT when the caller already holds a transaction). Before
    # 2026-09-22 this was a bare BEGIN IMMEDIATE, which raised for any caller that was
    # already in one -- and the paired bare ROLLBACK then discarded THEIR writes.
    with tx(c):
        st = _state(c, provider)
        # Reclaim first, so a stuck count cannot refuse the very call that would clear it.
        st = dict(st)
        st["inflight"] = _reclaim_leaked_slots(c, provider, lim, st, ts)

        if st["banned_until_ms"] > ts:
            raise RateLimited(provider, "provider cooldown", (st["banned_until_ms"] - ts) / 1000)

        fam_until = family_banned_until(c, provider, endpoint)
        if fam_until > ts:
            if priority is Priority.EXIT:
                # An exit during a family cooldown is exactly when an exit matters most.
                # The cooldown is OUR guess about the provider's state; a stop-loss that
                # our own guess refuses is an unbounded loss. Let it try: a real 429 from
                # the provider is a better answer than a self-imposed one. Found 2026-09-21:
                # one entry-burst 429 banned the `trade` family -- which covers trade.swap
                # (every sell) AND trade.query_order (every reconcile) -- for 60-3600 s.
                log.warning("%s: EXIT bypassing %s cooldown (%.0fs left)",
                            provider, _family(endpoint), (fam_until - ts) / 1000)
            else:
                raise RateLimited(provider, f"{_family(endpoint)} cooldown", (fam_until - ts) / 1000)

        # daily credit cap
        day = _day_key()
        spent = st["spent_today"] if st["day_key"] == day else 0
        if lim.daily_credit_cap is not None and spent + weight > lim.daily_credit_cap:
            raise RateLimited(provider, "daily credit cap", 3600)

        # minimum interval, doubled per penalty level (capped)
        interval = lim.min_interval_ms * (2 ** min(st["penalty_level"], 8))
        # high-priority work gets a shorter floor so exits are never starved by discovery
        if priority <= Priority.POSITION:
            interval = max(50, interval // 4)
        since = ts - st["last_call_ms"]
        if st["last_call_ms"] and since < interval:
            raise RateLimited(provider, "minimum interval", (interval - since) / 1000)

        if st["inflight"] >= lim.max_inflight:
            # `inflight` lives in SQLite so the six services share one count. The cost is
            # that a process killed mid-request never runs its `release`, and the leaked
            # slot outlives the process, the restart and the machine reboot -- a provider
            # at max_inflight with every holder dead is deadlocked permanently.
            #
            # This happened on 2026-09-21: a hard restart left `pumpfun` and `dexscreener`
            # at inflight=2, and pump.fun -- the keyless source behind launches, curve
            # reserves and the trade tape -- served zero requests afterwards. The tape is
            # a hot-window resource, so that was coverage lost for good, and it looked
            # exactly like a provider outage from the outside.
            #
            # A slot whose last call is older than any call could possibly still be
            # running belongs to a dead holder. Reclaim it rather than waiting for a
            # human to notice a silent stall.
            stale_after = max(INFLIGHT_STALE_MS, lim.min_interval_ms * 10)
            if st["last_call_ms"] and (ts - st["last_call_ms"]) > stale_after:
                log.warning(
                    "%s: reclaiming %d leaked inflight slot(s); last call was %.0fs ago",
                    provider,
                    st["inflight"],
                    (ts - st["last_call_ms"]) / 1000,
                )
                c.execute("UPDATE provider_state SET inflight=0 WHERE provider=?", (provider,))
                st["inflight"] = 0
            elif priority is Priority.EXIT:
                # Every inflight slot may be an entry. An exit does not wait behind them.
                log.warning("%s: EXIT bypassing max_inflight (%d in flight)", provider, st["inflight"])
            else:
                raise RateLimited(provider, "max inflight", 0.5)

        # leaky bucket, in milli-units to avoid float drift
        elapsed_s = max(0.0, (ts - st["last_refill_ms"]) / 1000.0)
        credit = min(
            lim.capacity * 1000,
            int(st["credit_milli"] + elapsed_s * lim.refill_per_s * 1000),
        )
        cost = weight * 1000
        if weight > lim.capacity:
            # A bucket smaller than the call can never fill enough, so "retry later" is a
            # lie: this never clears. It is a misconfiguration or a plan the account does
            # not have, and it must read as one. Setting gmgn capacity to the Free
            # allowance of 5 while a swap costs 10 made every live entry and every live
            # exit permanently unreservable, reported as a transient bucket exhaustion.
            raise RateLimited(
                provider,
                f"{endpoint} costs {weight} but the {provider} budget caps at "
                f"{lim.capacity}; raise provider_budgets.{provider}.capacity in "
                f"config/risk.yaml, or the account needs a higher plan",
                0.0,
            )
        if credit < cost:
            # The reserved floor: only an EXIT may run the bucket below zero, and only to
            # -EXIT_OVERDRAFT_WEIGHT. Entries and discovery see exhaustion here as before,
            # which is what keeps the overdraft available for the exit that needs it.
            floor = -EXIT_OVERDRAFT_WEIGHT * 1000 if priority is Priority.EXIT else 0
            if credit - cost < floor:
                deficit = (cost - credit) / 1000.0
                raise RateLimited(provider, "bucket exhausted", max(0.2, deficit / max(lim.refill_per_s, 0.1)))
            log.warning("%s: EXIT drawing on the reserved overdraft (credit %.1f -> %.1f)",
                        provider, credit / 1000, (credit - cost) / 1000)

        c.execute(
            "UPDATE provider_state SET credit_milli=?, last_refill_ms=?, last_call_ms=?, "
            "inflight=inflight+1, spent_today=?, day_key=? WHERE provider=?",
            (credit - cost, ts, ts, spent + weight, day, provider),
        )
    return weight


def release(
    provider: str,
    endpoint: str,
    status: str = "ok",
    latency_ms: int | None = None,
    retry_after_s: float | None = None,
    detail: str | None = None,
    weight: int = 1,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Record the outcome. ``status='rate_limited'`` opens a family cooldown.

    A note on how much that cooldown actually achieves. Some providers rate limit per
    route, but GMGN's 429 is **IP-wide** ("IP rate limit exceeded"), so banning one
    endpoint family under-reacts: every other family is equally throttled and this
    function has not touched them. The real global back-pressure is ``penalty_level``,
    incremented just below, which doubles ``min_interval_ms`` for the whole provider.
    That is the intended design — a family ban is the precise part and the penalty level
    is the blunt part — but it is not obvious from the code, so do not reach for a wider
    family ban when the answer is that the penalty level should have climbed faster.

    ``detail`` is redacted here rather than at the call site. ``guarded`` builds it from
    the raw exception, and an httpx error message carries the full request URL, so a
    provider whose key travels in the query string wrote that key straight into
    ``provider_calls.detail`` and ``provider_family_bans.reason``. Doing it here means a
    caller passing its own detail cannot leak either.
    """
    c = _accounting_conn(conn)
    ts = now_ms()
    detail = redact_text(detail) if detail else detail

    # THE SLOT COMES BACK FIRST, in its own nested transaction, before anything that can
    # fail. Until 2026-09-22 the decrement shared a transaction with the penalty
    # bookkeeping and the `provider_calls` insert, so any error anywhere in this function
    # left `inflight` permanently one higher. pumpfun allows 2 concurrent calls; three
    # such failures throttled the provider to a standstill until the process restarted,
    # and the only clue was a warning about a rate limiter.
    #
    # Releasing a slot and recording why are separate concerns and must not share a fate.
    try:
        with tx(c):
            c.execute(
                "UPDATE provider_state SET inflight=MAX(0, inflight-1) WHERE provider=?",
                (provider,),
            )
    except sqlite3.Error as exc:
        log.error("limiter could not release the %s slot: %s", provider, exc)

    try:
        with tx(c):
            lim = limits_for(provider)
            if status == "rate_limited":
                st = _state(c, provider)
                level = min(st["penalty_level"] + 1, 12)
                cool = retry_after_s if retry_after_s else min(
                    lim.ban_ceiling_s, lim.ban_floor_s * (2 ** min(level - 1, 6))
                )
                c.execute(
                    "INSERT INTO provider_family_bans (provider, family, banned_until_ms, reason) "
                    "VALUES (?,?,?,?) ON CONFLICT(provider, family) DO UPDATE SET "
                    "banned_until_ms=excluded.banned_until_ms, reason=excluded.reason",
                    (provider, _family(endpoint), ts + int(cool * 1000), detail or "429"),
                )
                c.execute(
                    "UPDATE provider_state SET penalty_level=? WHERE provider=?", (level, provider)
                )
                log.warning("%s %s rate limited; family cooldown %.0fs", provider, endpoint, cool)
            elif status == "ok":
                c.execute(
                    "UPDATE provider_state SET penalty_level=MAX(0, penalty_level-1) "
                    "WHERE provider=?",
                    (provider,),
                )
            # Every call is recorded, whatever its status -- this insert sits OUTSIDE the
            # status branches. It briefly did not, during the 2026-09-22 nesting fix, which
            # would have hidden every rate-limited call from `provider_calls` and from the
            # spend accounting that reads it.
            c.execute(
                "INSERT INTO provider_calls (provider, endpoint, weight, ts_ms, latency_ms, "
                "status, detail) VALUES (?,?,?,?,?,?,?)",
                (provider, endpoint, weight, ts, latency_ms, status, (detail or "")[:500]),
            )
    except sqlite3.Error as exc:
        # The slot is already back. This is the bookkeeping only, so say so -- the old
        # message read as though the release itself had failed, which sent the operator
        # looking in the wrong place.
        log.warning("limiter bookkeeping failed for %s (slot already released): %s", provider, exc)


@contextmanager
def guarded(
    provider: str,
    endpoint: str,
    priority: Priority = Priority.RESEARCH,
    conn: sqlite3.Connection | None = None,
) -> Iterator[None]:
    """Reserve, run, release. Use this around every provider request."""
    weight = reserve(provider, endpoint, priority, conn=conn)
    started = time.monotonic()
    status = "ok"
    detail: str | None = None
    retry_after: float | None = None
    try:
        yield
    except RateLimited:
        raise
    except Exception as exc:  # noqa: BLE001 — classified below
        status = "error"
        detail = f"{type(exc).__name__}: {exc}"
        retry_after = getattr(exc, "retry_after_s", None)
        if getattr(exc, "status_code", None) == 429 or "429" in str(exc):
            status = "rate_limited"
        raise
    finally:
        release(
            provider,
            endpoint,
            status=status,
            latency_ms=int((time.monotonic() - started) * 1000),
            retry_after_s=retry_after,
            detail=detail,
            weight=weight,
            conn=conn,
        )


def wait_for(
    provider: str,
    endpoint: str,
    priority: Priority = Priority.RESEARCH,
    timeout_s: float = 30.0,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Block until a reservation succeeds or ``timeout_s`` elapses."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return reserve(provider, endpoint, priority, conn=conn)
        except RateLimited as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(exc.retry_after_s, remaining, 5.0))


def status(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Snapshot for the dashboard's provider meters."""
    c = conn or get_conn()
    ts = now_ms()
    out: list[dict] = []
    for row in c.execute("SELECT * FROM provider_state"):
        st = dict(row)
        lim = limits_for(st["provider"])
        elapsed_s = max(0.0, (ts - st["last_refill_ms"]) / 1000.0)
        credit = min(lim.capacity * 1000, int(st["credit_milli"] + elapsed_s * lim.refill_per_s * 1000))
        bans = [
            dict(b)
            for b in c.execute(
                "SELECT family, banned_until_ms FROM provider_family_bans "
                "WHERE provider=? AND banned_until_ms > ?",
                (st["provider"], ts),
            )
        ]
        out.append(
            {
                "provider": st["provider"],
                "credit": round(credit / 1000, 2),
                "capacity": lim.capacity,
                "inflight": st["inflight"],
                "penalty_level": st["penalty_level"],
                "spent_today": st["spent_today"],
                "daily_cap": lim.daily_credit_cap,
                "banned": st["banned_until_ms"] > ts,
                "family_bans": bans,
            }
        )
    return out


def reset(provider: str | None = None, conn: sqlite3.Connection | None = None) -> None:
    """Clear cooldowns. Operator action; not exposed to the model."""
    c = conn or get_conn()
    if provider:
        c.execute("DELETE FROM provider_family_bans WHERE provider=?", (provider,))
        c.execute(
            "UPDATE provider_state SET banned_until_ms=0, penalty_level=0, inflight=0 WHERE provider=?",
            (provider,),
        )
    else:
        c.execute("DELETE FROM provider_family_bans")
        c.execute("UPDATE provider_state SET banned_until_ms=0, penalty_level=0, inflight=0")
