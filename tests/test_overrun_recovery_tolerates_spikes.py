"""A halt that cannot be lifted is an outage, not a failsafe.

MEASURED on the live box 2026-09-22, from the watchdog's own heartbeats:

    -0.2min  duration_ms=2388     -0.5min  duration_ms=15430   <== spike
    -0.2min  duration_ms=2864     -0.8min  duration_ms=2684
    -0.3min  duration_ms=2424     -0.9min  duration_ms=4525
    -0.4min  duration_ms=2499     -1.0min  duration_ms=2430
    -0.5min  duration_ms=2788     -1.1min  duration_ms=3360

Nine of ten ticks were comfortably inside the 5000 ms budget. About once a minute one
spiked to 15-16 s, each time with ``consecutive: 1`` -- never the three-in-a-row at twice
the budget that HALTS. So protection was healthy by its own definition and the entry halt
stayed on anyway, on all three chains, because recovery demanded 20 CONSECUTIVE clean
ticks and a spike every eight to fourteen ticks reset the counter to zero every time.

The halt and the recovery were asymmetric: the halt fires on a CHRONIC condition, the
recovery was cancelled by an ISOLATED one. This pins the symmetry. An isolated spike
costs recovery progress -- it is still a late stop and should not be free -- but only a
chronic overrun, the same condition that halts, wipes it.
"""

from __future__ import annotations

import pytest

from kaiba.execution import watchdog as W


class _Gate:
    def __init__(self) -> None:
        self.halted = True
        self.resumed = 0

    def resume_if_reason_starts_with(self, prefix, conn=None):
        if self.halted:
            self.halted = False
            self.resumed += 1
            return True
        return False

    def halt(self, reason, conn=None):
        self.halted = True


class _Cfg:
    poll_interval_s = 5.0


def dog(monkeypatch, tmp_db):
    d = W.Watchdog(tmp_db)
    d.gate = _Gate()
    monkeypatch.setattr(d, "_halt_entries_on_timeout", lambda: True)
    monkeypatch.setattr(d, "_emit", lambda *a, **k: None)
    d._overruns = 0
    d._healthy_ticks = 0
    return d


def tick(d, ms: int):
    report = W.TickReport()
    report.duration_ms = ms
    report.checked = 9
    d._note_overrun(report, _Cfg())


def test_a_clean_run_still_lifts_the_halt(monkeypatch, tmp_db):
    """The control: without this the test below could pass by lifting nothing."""
    d = dog(monkeypatch, tmp_db)
    for _ in range(W.OVERRUN_RECOVER_TICKS):
        tick(d, 2400)
    assert d.gate.resumed == 1


def test_an_isolated_spike_does_not_make_the_halt_unliftable(monkeypatch, tmp_db):
    """THE LIVE BUG. Nine good ticks, one spike, repeatedly -- and it must still recover."""
    d = dog(monkeypatch, tmp_db)
    for _ in range(12):           # twelve cycles of 9 good + 1 spike = 108 ticks
        for _ in range(9):
            tick(d, 2400)
        tick(d, 15430)
    assert d.gate.resumed >= 1, (
        "a watchdog inside its budget nine ticks in ten must eventually lift its own halt"
    )


def test_a_spike_still_costs_recovery_progress(monkeypatch, tmp_db):
    """It is a late stop. It must not be free, or the recovery means nothing."""
    d = dog(monkeypatch, tmp_db)
    for _ in range(W.OVERRUN_RECOVER_TICKS - 1):
        tick(d, 2400)
    tick(d, 15430)
    assert d.gate.resumed == 0, "a spike one tick before recovery must delay it"
    assert d._healthy_ticks < W.OVERRUN_RECOVER_TICKS


def test_a_chronic_overrun_still_wipes_recovery(monkeypatch, tmp_db):
    """The condition that HALTS must also be the one that cancels recovery outright."""
    d = dog(monkeypatch, tmp_db)
    for _ in range(W.OVERRUN_RECOVER_TICKS - 1):
        tick(d, 2400)
    for _ in range(W.OVERRUN_HALT_TICKS):   # three in a row at >= 2x budget
        tick(d, 15430)
    assert d._healthy_ticks == 0, "a chronic overrun must reset recovery to zero"
    assert d.gate.resumed == 0


def test_a_chronic_overrun_still_halts(monkeypatch, tmp_db):
    """Nothing here weakens the failsafe itself."""
    d = dog(monkeypatch, tmp_db)
    d.gate.halted = False
    for _ in range(W.OVERRUN_HALT_TICKS):
        tick(d, 15430)
    assert d.gate.halted is True


def test_a_watchdog_late_as_often_as_not_never_recovers(monkeypatch, tmp_db):
    """The other side of the rule, and the reason a spike is not free.

    Recovery needs OVERRUN_RECOVER_TICKS MORE in-budget ticks than late ones. A watchdog
    alternating good and late is not keeping up, and must not be able to clear its own
    halt by outlasting a counter.
    """
    d = dog(monkeypatch, tmp_db)
    for _ in range(200):
        tick(d, 2400)
        tick(d, 15430)
    assert d.gate.resumed == 0, "a 50% late rate must never clear the halt"


def test_the_observed_live_rate_does_recover(monkeypatch, tmp_db):
    """MEASURED 2026-09-22: 9 of 52 ticks (17%) over budget, none chronic."""
    d = dog(monkeypatch, tmp_db)
    for _ in range(9):            # one cycle of the measured shape
        for _ in range(5):
            tick(d, 2600)
        tick(d, 16239)
    assert d.gate.resumed >= 1, "the measured live rate must be able to recover"
