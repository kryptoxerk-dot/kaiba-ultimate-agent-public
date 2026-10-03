"""A tick that checked no live position must not halt live entries.

MEASURED 2026-10-03 12:16 CST on the box: 8 open positions, every one SHADOW, no live
position for over two hours. With no live inventory `_prefetch_quotes` gives the paper book
the whole interval by design, ticks ran 19-26 s against 12 s, and `protection_overrun`
halted entries on sol, robinhood and bsc. A halt means no live position opens, so every
later tick was paper-only again: the failsafe renewed itself on work that could not make
any stop late.
"""

from __future__ import annotations

import pytest

from kaiba.core.schemas import Chain, LaneMode
from kaiba.execution import watchdog as wd
from tests.test_overrun_recovery import _cfg, _dog, _halted
from tests.test_watchdog import (  # noqa: F401 - risk_file is a fixture used by name
    TOKEN,
    FakeSource,
    RecordingSubmitter,
    events_named,
    make_position,
    risk_file,
)

pytestmark = pytest.mark.usefixtures("risk_file")

CHRONIC_MS = 30_000  # 6x the pinned 5 s interval


def _report(duration_ms: int, live: int | None) -> wd.TickReport:
    r = wd.TickReport()
    r.duration_ms = duration_ms
    r.checked = 8
    r.live_checked = live
    return r


def test_paper_only_ticks_never_halt_entries(tmp_db):
    dog = _dog(tmp_db)
    cfg = _cfg(dog)
    for _ in range(wd.OVERRUN_HALT_TICKS * 4):
        dog._note_overrun(_report(CHRONIC_MS, live=0), cfg)
    assert _halted(tmp_db)[0] == 0
    # Still announced -- the tick WAS slow -- but as not counting toward the halt.
    seen = events_named(tmp_db, "tick_overrun")
    assert seen and all(e["halting_entries"] is False and e["live_positions"] == 0 for e in seen)


def test_the_same_ticks_with_a_live_position_still_halt(tmp_db):
    """Positive control: the failsafe is intact for the book it exists to protect."""
    dog = _dog(tmp_db)
    cfg = _cfg(dog)
    for _ in range(wd.OVERRUN_HALT_TICKS):
        dog._note_overrun(_report(CHRONIC_MS, live=1), cfg)
    halted, reason = _halted(tmp_db)
    assert halted == 1 and reason.startswith("protection_overrun:")


def test_an_uncounted_report_is_treated_as_live(tmp_db):
    """``live_checked=None`` (a caller that did not count) fails safe: it can halt."""
    dog = _dog(tmp_db)
    cfg = _cfg(dog)
    for _ in range(wd.OVERRUN_HALT_TICKS):
        dog._note_overrun(_report(CHRONIC_MS, live=None), cfg)
    assert _halted(tmp_db)[0] == 1


def test_paper_only_ticks_lift_a_halt_even_when_slow(tmp_db):
    """The 12:16 shape: halted by live-era overruns, then nothing live and slow paper ticks."""
    dog = _dog(tmp_db)
    cfg = _cfg(dog)
    for _ in range(wd.OVERRUN_HALT_TICKS):
        dog._note_overrun(_report(CHRONIC_MS, live=1), cfg)
    assert _halted(tmp_db)[0] == 1
    for _ in range(wd.OVERRUN_RECOVER_TICKS):
        dog._note_overrun(_report(CHRONIC_MS, live=0), cfg)
    assert _halted(tmp_db) == (0, None)


def test_tick_counts_live_positions(tmp_db):
    make_position(tmp_db, position_id="pos_paper", token=TOKEN, mode=LaneMode.SHADOW)
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "1.0"}), submitter=RecordingSubmitter())
    report = dog.tick()
    assert (report.checked, report.live_checked) == (1, 0)
    other = "So11111111111111111111111111111111111111113"
    make_position(tmp_db, position_id="pos_live", token=other, mode=LaneMode.LIVE, chain=Chain.SOL)
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "1.0", other: "1.0"}),
                      submitter=RecordingSubmitter())
    report = dog.tick()
    assert report.live_checked == 1
    assert report.as_payload()["live_checked"] == 1
