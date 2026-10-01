"""Protection reads the whole book's prices at once, so a stop is not late because the
position before it was slow.

MEASURED 2026-09-22 with sm-trenches armed and filling: quotes were fetched serially inside
the per-position loop at ~0.55 s each, so ticks ran 5,301-11,428 ms against a 5,000 ms poll
interval at only 4-5 open positions. That is both the ceiling on how many positions the agent
can hold and a reason stops fire late -- the worst live round trip (-52.8%, held 167 s) had
``blind_since`` set and sold +4.6% above the floor, into a book that had already gone.
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal

from kaiba.core.schemas import Chain, LaneMode
from kaiba.execution import watchdog as wd
from tests.test_watchdog import (  # noqa: F401 - fixtures used by name
    TOKEN, FakeSource, RecordingSubmitter, make_position, risk_file,
)


class SlowSource:
    """A price source with the live box's measured latency, and a concurrency counter."""

    name = "slow"

    def __init__(self, delay_s: float = 0.2, price: str = "1.0") -> None:
        self.delay_s = delay_s
        self.price = price
        self.calls: list[str] = []
        self._lock = threading.Lock()
        self.live = 0
        self.max_live = 0

    def quote(self, chain: Chain, token: str) -> wd.PriceQuote:
        with self._lock:
            self.calls.append(token)
            self.live += 1
            self.max_live = max(self.max_live, self.live)
        try:
            time.sleep(self.delay_s)
            return wd.PriceQuote(price_usd=Decimal(self.price), liquidity_usd=Decimal("1000000"),
                                 basis=wd.EvidenceBasis.PROVIDER_REPORTED, source=self.name)
        finally:
            with self._lock:
                self.live -= 1


def _book(conn, n: int) -> list[str]:
    toks = []
    for i in range(n):
        tok = f"TOKEN{i}pump{'x' * 30}"[:44]
        make_position(conn, position_id=f"pos_{i}", token=tok, mode=LaneMode.SHADOW)
        toks.append(tok)
    conn.commit()
    return toks


def test_the_book_is_priced_concurrently(tmp_db, risk_file):
    src = SlowSource(delay_s=0.2)
    dog = wd.Watchdog(tmp_db, price_source=src, submitter=RecordingSubmitter())
    _book(tmp_db, 6)

    started = time.monotonic()
    report = dog.tick()
    elapsed = time.monotonic() - started

    assert report.checked == 6
    assert src.max_live > 1, "prices were still fetched one at a time"
    # Serial would be >= 6 * 0.2 = 1.2 s. Concurrent at width 8 is about one delay.
    assert elapsed < 1.0, f"tick took {elapsed:.2f}s; concurrency is not helping"


def test_every_position_still_gets_its_own_price(tmp_db, risk_file):
    src = SlowSource(delay_s=0.01)
    dog = wd.Watchdog(tmp_db, price_source=src, submitter=RecordingSubmitter())
    toks = _book(tmp_db, 5)
    dog.tick()
    assert set(src.calls) >= set(toks)


def test_one_dead_quote_blinds_only_its_own_position(tmp_db, risk_file):
    """A provider that raises for one token must not cost the rest of the book its prices.

    The second assertion is the sharp one: each token is quoted EXACTLY ONCE. If a single
    raising quote aborted the pool, the tick would fall back to serial and re-quote every
    token -- correct in outcome, but it throws away the concurrency on precisely the tick
    where a provider is already misbehaving and latency matters most.
    """
    toks = _book(tmp_db, 4)
    bad = toks[1]

    class OneBad(SlowSource):
        def quote(self, chain, token):
            if token == bad:
                with self._lock:
                    self.calls.append(token)
                raise RuntimeError("provider on fire")
            return super().quote(chain, token)

    src = OneBad(delay_s=0.01)
    dog = wd.Watchdog(tmp_db, price_source=src, submitter=RecordingSubmitter())
    report = dog.tick()
    assert report.checked == 4
    assert report.blind == 1, f"expected exactly one blind position, got {report.blind}"
    for tok in toks:
        assert src.calls.count(tok) == 1, (
            f"{tok[:10]} was quoted {src.calls.count(tok)}x; one bad quote abandoned the pool"
        )


def test_the_cache_never_outlives_its_tick(tmp_db, risk_file):
    """A price parked on one tick must not decide a stop on the next."""
    src = SlowSource(delay_s=0.0)
    dog = wd.Watchdog(tmp_db, price_source=src, submitter=RecordingSubmitter())
    _book(tmp_db, 3)
    dog.tick()
    first = len(src.calls)
    assert first >= 3
    dog.tick()
    assert len(src.calls) >= first + 3, "second tick reused the first tick's prices"


def test_a_stale_price_from_an_earlier_tick_is_never_reused(tmp_db, risk_file):
    """The cache is cleared at the START of every prefetch, not merely overwritten.

    Overwriting is not enough: the prefetch returns early for a book of one, so a book
    that SHRINKS to one position would otherwise leave that position deciding its stop on
    a price parked while the book was larger. Here the price moves 1.00 -> 0.10 between
    ticks; if the stale entry survived, the survivor would be checked at the old price.
    """
    toks = _book(tmp_db, 3)
    src = SlowSource(delay_s=0.0, price="1.0")
    dog = wd.Watchdog(tmp_db, price_source=src, submitter=RecordingSubmitter())
    dog.tick()
    assert len(dog._quote_cache) == 3

    # The book shrinks to one, and the market moves hard.
    tmp_db.execute("UPDATE positions SET closed_ms=? WHERE token IN (?,?)", (wd.now_ms(), toks[1], toks[2]))
    tmp_db.commit()
    src.price = "0.10"
    src.calls.clear()

    dog.tick()

    assert dog._quote_cache == {}, "the prefetch must clear before returning early"
    assert src.calls == [toks[0]], f"the survivor was not re-priced: {src.calls}"


def test_a_single_position_skips_the_pool_entirely(tmp_db, risk_file):
    """No thread pool for a book of one; the serial path is already optimal there."""
    src = SlowSource(delay_s=0.0)
    dog = wd.Watchdog(tmp_db, price_source=src, submitter=RecordingSubmitter())
    make_position(tmp_db, mode=LaneMode.SHADOW)
    tmp_db.commit()
    dog.tick()
    assert src.max_live == 1
    assert dog._quote_cache == {}


def test_a_prefetch_that_explodes_falls_back_to_serial(tmp_db, risk_file, monkeypatch):
    """The optimisation may never be the reason a stop was not evaluated."""
    src = SlowSource(delay_s=0.0)
    dog = wd.Watchdog(tmp_db, price_source=src, submitter=RecordingSubmitter())
    _book(tmp_db, 3)

    def boom(positions):
        dog._quote_cache = {}
        raise RuntimeError("pool unavailable")

    monkeypatch.setattr(dog, "_prefetch_quotes", boom)
    try:
        report = dog.tick()
    except RuntimeError:
        raise AssertionError("a failed prefetch must not escape the tick")
    assert report.checked == 3
    assert len(src.calls) >= 3, "the serial fallback did not price the book"
