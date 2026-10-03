"""A sell our own limiter refused is retried on the limiter's hint, not backed off.

MEASURED 2026-09-30 on the box (robinhood 0x2aa4...6262): ``stop_loss`` decided, the sell
was refused ``RateLimited: gmgn: minimum interval (retry in 0.1s)`` -- nothing sent --
and the watchdog armed its venue-failure backoff (``retry_in_ms`` 5000, i.e. the next
12 s tick). It was re-sent 14.8 s later and closed at -42.3% against a -30% stop.

A local limiter refusal is not a venue failure: it is our own spacing, it clears in
milliseconds, and it says nothing about whether the token can be sold. So it retries on
the limiter's own hint (capped at the first rung), and it does not advance
``exit_attempts``, which drives escalation to the 30-minute ceiling and the dust
write-off. A provider's own 429 and our cooldowns keep the full backoff.

Driven through the REAL ``DefaultExitSubmitter._live``, so the detail string the tick
reads is the one production produces from a raised ``RateLimited``.
"""

from __future__ import annotations

import pytest

from kaiba.core.db import fetch_one
from kaiba.core.limiter import RateLimited
from kaiba.core.schemas import LaneMode
from kaiba.execution import watchdog as W
from tests.test_watchdog import TOKEN, FakeSource, events_named, make_position

pytest_plugins = ("tests.test_watchdog",)

WALLET = "62EbtFbmJATT94Y6Uguf6a1kdaJuZWAu8ycqbg3xKfQg"


def _live_dog(tmp_db, monkeypatch, raised: Exception):
    """A watchdog whose real live submitter reaches ``executor.submit`` and gets ``raised``."""
    monkeypatch.setattr(W, "token_decimals", lambda conn, chain, token: 9)
    monkeypatch.setattr(W, "wallet_token_units", lambda *a, **k: None)
    monkeypatch.setattr(W, "exit_wallet_for", lambda chain: WALLET)
    monkeypatch.setattr(W.DefaultExitSubmitter, "_min_out", lambda self, p, q, d, quote: 1)
    monkeypatch.setattr(W.executor, "build_order",
                        lambda **kw: type("O", (), {"order_id": "ord:x", **kw})())
    sends: list = []

    def submit(order, conn):
        sends.append(order)
        raise raised

    monkeypatch.setattr(W.executor, "submit", submit)
    make_position(tmp_db, mode=LaneMode.CANARY)
    dog = W.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "0.6"}),
                     submitter=W.DefaultExitSubmitter(tmp_db))
    return dog, sends


def _state(tmp_db) -> dict:
    return dict(fetch_one(tmp_db, "SELECT exit_attempts, exit_retry_after_ms FROM watchdog_state"))


def _rearm(tmp_db) -> None:
    """Let any backoff elapse without a clock: the watchdog reads this row every tick."""
    tmp_db.execute("UPDATE watchdog_state SET exit_retry_after_ms=NULL")


def test_a_minimum_interval_refusal_retries_on_the_limiters_hint(tmp_db, risk_file, monkeypatch):
    """THE REGRESSION: 0.1 s of our own spacing cost a 5 s backoff -- a whole 12 s tick."""
    dog, sends = _live_dog(tmp_db, monkeypatch, RateLimited("gmgn", "minimum interval", 0.1))
    dog.tick()

    assert len(sends) == 1
    failed = events_named(tmp_db, "exit_failed")[0]
    assert failed["limiter_refusal"] is True, failed
    assert failed["retry_in_ms"] == 101, failed
    assert failed["retry_in_ms"] < W.RETRY_BASE_MS


def test_a_limiter_refusal_does_not_advance_the_escalation_counter(tmp_db, risk_file, monkeypatch):
    """Five refusals in a row must leave the counter where it was: the escalation to the
    30-minute ceiling, and the dust write-off, are for a token that will not SELL."""
    dog, sends = _live_dog(tmp_db, monkeypatch, RateLimited("gmgn", "minimum interval", 0.1))
    for _ in range(5):
        dog.tick()
        _rearm(tmp_db)

    assert len(sends) == 5
    assert _state(tmp_db)["exit_attempts"] == 0
    assert all(e["retry_in_ms"] <= W.RETRY_BASE_MS for e in events_named(tmp_db, "exit_failed"))


def test_a_long_local_hint_is_capped_at_the_first_rung(tmp_db, risk_file, monkeypatch):
    """An exhausted bucket can hint 20 s; a stop must still be retried within the rung."""
    dog, _ = _live_dog(tmp_db, monkeypatch, RateLimited("gmgn", "bucket exhausted", 21.0))
    dog.tick()
    failed = events_named(tmp_db, "exit_failed")[0]
    assert failed["limiter_refusal"] is True
    assert failed["retry_in_ms"] == W.RETRY_BASE_MS


@pytest.mark.parametrize("raised", [
    RateLimited("gmgn", "provider returned 429", 300),  # the VENUE's own limit
    RateLimited("gmgn", "trade cooldown", 120),         # our family ban after a 429
])
def test_a_venue_429_or_a_cooldown_keeps_the_full_escalating_backoff(
    tmp_db, risk_file, monkeypatch, raised
):
    """Positive control: these must not be shortened, and they DO count."""
    dog, sends = _live_dog(tmp_db, monkeypatch, raised)
    for _ in range(3):
        dog.tick()
        _rearm(tmp_db)

    assert len(sends) == 3
    assert _state(tmp_db)["exit_attempts"] == 3
    got = sorted(e["retry_in_ms"] for e in events_named(tmp_db, "exit_failed"))
    assert got == [W._exit_backoff(1), W._exit_backoff(2), W._exit_backoff(3)], got
    assert not any(e["limiter_refusal"] for e in events_named(tmp_db, "exit_failed"))
