"""Live quote work must not be starved by shadow quote work."""

from __future__ import annotations

import threading
import time
from decimal import Decimal

from kaiba.core.schemas import Chain, LaneMode
from kaiba.execution import watchdog as wd
from tests.test_watchdog import RecordingSubmitter, make_position, risk_file


class ShadowCanConsumeBudget:
    name = "budget-fixture"

    def __init__(self, live_token: str) -> None:
        self.live_token = live_token
        self.shadow_started = threading.Event()
        self.live_saw_shadow = False
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def quote(self, chain: Chain, token: str) -> wd.PriceQuote:
        _ = chain
        with self._lock:
            self.calls.append(token)
        if token == self.live_token:
            self.live_saw_shadow = self.shadow_started.wait(timeout=0.25)
            if self.live_saw_shadow:
                return wd.PriceQuote.unavailable("shadow quote consumed the live budget", source=self.name)
        else:
            self.shadow_started.set()
        return wd.PriceQuote(
            price_usd=Decimal("1.5"),
            liquidity_usd=Decimal("1000000"),
            basis=wd.EvidenceBasis.PROVIDER_REPORTED,
            source=self.name,
        )


def test_live_quote_finishes_before_shadow_quote_work_can_consume_budget(tmp_db, risk_file):
    shadow_token = "Shadow111111111111111111111111111111111111111"
    live_token = "Live11111111111111111111111111111111111111111"
    make_position(tmp_db, position_id="shadow", token=shadow_token, mode=LaneMode.SHADOW)
    make_position(tmp_db, position_id="live", token=live_token, mode=LaneMode.LIVE)
    tmp_db.commit()

    source = ShadowCanConsumeBudget(live_token)
    dog = wd.Watchdog(tmp_db, price_source=source, submitter=RecordingSubmitter())

    report = dog.tick()

    assert report.checked == 2
    assert report.blind == 0
    assert source.live_saw_shadow is False
    assert source.calls.count(live_token) == 1
    assert source.calls.count(shadow_token) == 1


class SlowLiveSource:
    name = "slow-live"

    def __init__(self, live_token: str, delay_s: float) -> None:
        self.live_token = live_token
        self.delay_s = delay_s
        self.calls: list[str] = []

    def quote(self, chain: Chain, token: str) -> wd.PriceQuote:
        _ = chain
        self.calls.append(token)
        if token == self.live_token:
            time.sleep(self.delay_s)
        return wd.PriceQuote(
            price_usd=Decimal("1.5"),
            liquidity_usd=Decimal("1000000"),
            basis=wd.EvidenceBasis.PROVIDER_REPORTED,
            source=self.name,
        )


def test_shadow_quotes_are_deferred_when_live_work_consumes_the_tick_budget(tmp_db, risk_file):
    shadow_tokens = [f"Shadow{i}11111111111111111111111111111111111111" for i in range(3)]
    live_token = "Live22222222222222222222222222222222222222222"
    for index, token in enumerate(shadow_tokens):
        make_position(tmp_db, position_id=f"shadow-{index}", token=token, mode=LaneMode.SHADOW)
    make_position(tmp_db, position_id="live-slow", token=live_token, mode=LaneMode.LIVE)
    tmp_db.commit()

    source = SlowLiveSource(live_token, delay_s=1.1)
    dog = wd.Watchdog(
        tmp_db,
        price_source=source,
        submitter=RecordingSubmitter(),
        cfg_provider=lambda: wd.ProtectionConfig(poll_interval_s=1),
    )

    report = dog.tick()

    assert report.checked == 4
    assert source.calls == [live_token]
    assert report.blind == 3
