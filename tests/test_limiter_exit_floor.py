"""An EXIT is never refused by our own limiter for a reason an entry created.

The bounded-loss guarantee rests on a stop-loss being SENDABLE. Found 2026-09-21: every
check in ``reserve`` except ``min_interval`` was priority-blind, and a single entry-burst
429 banned the ``trade`` family -- which covers every sell and every reconcile -- for
60-3600 s. These pin that ``Priority.EXIT`` gets through each of those gates while
``Priority.ENTRY`` is still refused by them, so the reserved capacity stays reserved.
"""

from __future__ import annotations

import pytest

from kaiba.core import limiter
from kaiba.core.limiter import EXIT_OVERDRAFT_WEIGHT, Priority, RateLimited, release, reserve

PROV = "gmgn"
SWAP = "trade.swap"


def _exhaust_as_entries(conn, clock: list[int], lim) -> None:
    for _ in range(lim.capacity // lim.weight_for(SWAP) + 2):
        clock[0] += lim.min_interval_ms + 1
        try:
            reserve(PROV, SWAP, Priority.ENTRY, conn)
            release(PROV, SWAP, "ok", conn=conn)
        except RateLimited:
            break


def test_a_family_ban_refuses_an_entry_but_not_an_exit(tmp_db, monkeypatch):
    clock = [1_000_000]
    monkeypatch.setattr(limiter, "now_ms", lambda: clock[0])
    reserve(PROV, SWAP, Priority.ENTRY, tmp_db)
    release(PROV, SWAP, "rate_limited", conn=tmp_db)  # bans the `trade` family
    clock[0] += 5_000
    with pytest.raises(RateLimited, match="cooldown"):
        reserve(PROV, SWAP, Priority.ENTRY, tmp_db)
    assert reserve(PROV, SWAP, Priority.EXIT, tmp_db) > 0


def test_max_inflight_refuses_an_entry_but_not_an_exit(tmp_db, monkeypatch):
    clock = [1_000_000]
    monkeypatch.setattr(limiter, "now_ms", lambda: clock[0])
    lim = limiter.limits_for(PROV)
    for _ in range(lim.max_inflight):  # fill every slot, never release
        clock[0] += lim.min_interval_ms + 1
        reserve(PROV, SWAP, Priority.ENTRY, tmp_db)
    clock[0] += lim.min_interval_ms + 1
    with pytest.raises(RateLimited, match="max inflight"):
        reserve(PROV, SWAP, Priority.ENTRY, tmp_db)
    assert reserve(PROV, SWAP, Priority.EXIT, tmp_db) > 0


def test_an_exhausted_bucket_refuses_an_entry_but_lets_an_exit_overdraw(tmp_db, monkeypatch):
    clock = [1_000_000]
    monkeypatch.setattr(limiter, "now_ms", lambda: clock[0])
    lim = limiter.limits_for(PROV)
    _exhaust_as_entries(tmp_db, clock, lim)
    clock[0] += lim.min_interval_ms + 1
    with pytest.raises(RateLimited, match="bucket exhausted"):
        reserve(PROV, SWAP, Priority.ENTRY, tmp_db)
    clock[0] += 60
    assert reserve(PROV, SWAP, Priority.EXIT, tmp_db) > 0, "the reserved overdraft must admit an exit"


def test_the_overdraft_is_bounded_even_for_exits(tmp_db, monkeypatch):
    """The floor is a floor. Exits cannot borrow forever."""
    clock = [1_000_000]
    monkeypatch.setattr(limiter, "now_ms", lambda: clock[0])
    lim = limiter.limits_for(PROV)
    w = lim.weight_for(SWAP)
    # EXIT's shortened interval is max(50, min_interval//4) = 62 ms on gmgn; space past it
    # so the only gate left to trip is the overdraft floor.
    step = max(50, lim.min_interval_ms // 4) + 5
    for _ in range(lim.capacity // w + EXIT_OVERDRAFT_WEIGHT // w):
        clock[0] += step
        reserve(PROV, SWAP, Priority.EXIT, tmp_db)
        release(PROV, SWAP, "ok", conn=tmp_db)
    clock[0] += step
    with pytest.raises(RateLimited, match="bucket exhausted"):
        reserve(PROV, SWAP, Priority.EXIT, tmp_db)


def test_entries_and_discovery_cannot_borrow_from_the_exit_overdraft(tmp_db, monkeypatch):
    """The whole point of reserving it."""
    clock = [1_000_000]
    monkeypatch.setattr(limiter, "now_ms", lambda: clock[0])
    lim = limiter.limits_for(PROV)
    _exhaust_as_entries(tmp_db, clock, lim)
    clock[0] += lim.min_interval_ms + 1
    with pytest.raises(RateLimited, match="bucket exhausted"):
        reserve(PROV, SWAP, Priority.ENTRY, tmp_db)
    with pytest.raises(RateLimited, match="bucket exhausted"):
        reserve(PROV, SWAP, Priority.DISCOVERY, tmp_db)
