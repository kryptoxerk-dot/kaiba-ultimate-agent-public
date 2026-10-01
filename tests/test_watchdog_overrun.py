"""A protection tick that outruns its own interval is announced; a chronic one halts entries.

MEASURED 2026-09-21: the 5 s poll inflated silently as positions were added (24.95 s at
N=25) and nothing paged. ``TickReport.duration_ms`` was in every heartbeat; nothing read
it. Exits are never gated by this -- they are what it protects.
"""

from __future__ import annotations

from types import SimpleNamespace

from kaiba.core.schemas import LaneMode
from kaiba.execution import watchdog as wd
from tests.test_watchdog import (  # noqa: F401 - fixtures are used by name
    TOKEN, FakeSource, RecordingSubmitter, events_named, make_position, risk_file,
)


def _dog(tmp_db, halts: list[str]) -> wd.Watchdog:
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "1.0"}), submitter=RecordingSubmitter())
    # The stub must answer EVERY gate call the watchdog makes, not just the one this file
    # is about. `_check_position` calls `resume_if_reason_starts_with` on the blind-halt
    # recovery path; a SimpleNamespace without it raised AttributeError, which the tick
    # swallowed as "could not clear the protection-blind halt" and then never reached the
    # overrun bookkeeping -- so all six tests here failed for a reason none of them named.
    dog.gate = SimpleNamespace(
        halt=lambda reason, conn: halts.append(reason),
        resume_if_reason_starts_with=lambda prefix, conn: False,
    )
    return dog


def _newest(conn, name: str) -> dict:
    """``events_named`` returns newest first; be explicit rather than rely on it."""
    found = events_named(conn, name)
    assert found, f"no {name} event"
    return max(found, key=lambda e: int(e.get("consecutive") or 0))


#: These tests are about the overrun RULE, so they fix the interval themselves rather
#: than reading whatever `config/risk.yaml` currently ships. MEASURED 2026-09-24: the
#: shipped `poll_interval_s` moved 5 -> 12 and every test here failed on
#: `assert cfg.poll_interval_s == 5` -- an ambient literal, not the invariant. A rule
#: test must not break because an operator retuned a knob it does not test.
INTERVAL_S = 5


def _cfg(dog) -> wd.ProtectionConfig:
    return dog.config().model_copy(update={"poll_interval_s": INTERVAL_S})


def _report(duration_ms: int, checked: int = 4) -> wd.TickReport:
    r = wd.TickReport()
    r.duration_ms = duration_ms
    r.checked = checked
    return r


def test_a_tick_over_the_interval_is_announced_with_the_numbers(tmp_db, risk_file):
    halts: list[str] = []
    dog = _dog(tmp_db, halts)
    cfg = _cfg(dog)
    assert dog._halt_entries_on_timeout(), "the shipped switch must be on for the halt rule to apply"

    dog._note_overrun(_report(6_000), cfg)

    ev = events_named(tmp_db, "tick_overrun")
    assert ev and ev[-1]["duration_ms"] == 6_000 and ev[-1]["poll_interval_ms"] == 5_000
    assert ev[-1]["positions"] == 4 and ev[-1]["consecutive"] == 1
    assert halts == [], "one overrun is a warning, not a halt"


def test_three_chronic_overruns_halt_entries_once_and_say_so(tmp_db, risk_file):
    halts: list[str] = []
    dog = _dog(tmp_db, halts)
    cfg = _cfg(dog)
    for _ in range(wd.OVERRUN_HALT_TICKS):
        dog._note_overrun(_report(12_000), cfg)  # >= 2x the 5 s interval
    assert len(halts) >= 1 and halts[0].startswith("protection_overrun:12000ms>5000ms x3"), halts
    assert _newest(tmp_db, "tick_overrun")["halting_entries"] is True


def test_slow_but_not_doubled_ticks_never_halt(tmp_db, risk_file):
    halts: list[str] = []
    dog = _dog(tmp_db, halts)
    cfg = _cfg(dog)
    for _ in range(wd.OVERRUN_HALT_TICKS + 2):
        dog._note_overrun(_report(7_000), cfg)  # over, but under 2x
    assert halts == []
    assert _newest(tmp_db, "tick_overrun")["consecutive"] == wd.OVERRUN_HALT_TICKS + 2


def test_an_in_budget_tick_resets_the_streak(tmp_db, risk_file):
    halts: list[str] = []
    dog = _dog(tmp_db, halts)
    cfg = _cfg(dog)
    dog._note_overrun(_report(12_000), cfg)
    dog._note_overrun(_report(12_000), cfg)
    dog._note_overrun(_report(100), cfg)  # back inside the interval
    dog._note_overrun(_report(12_000), cfg)
    assert halts == [], "the streak restarted; three in a row is the rule"
    assert dog._overruns == 1


def test_the_operator_switch_off_means_no_halt_but_still_the_alarm(tmp_db, risk_file, monkeypatch):
    halts: list[str] = []
    dog = _dog(tmp_db, halts)
    monkeypatch.setattr(dog, "_halt_entries_on_timeout", lambda: False)
    cfg = _cfg(dog)
    for _ in range(wd.OVERRUN_HALT_TICKS):
        dog._note_overrun(_report(12_000), cfg)
    assert halts == []
    assert _newest(tmp_db, "tick_overrun")["halting_entries"] is False


def test_a_real_tick_measures_itself(tmp_db, risk_file, monkeypatch):
    """Through ``tick()``: the clock says the tick took 6 s; the overrun is announced."""
    make_position(tmp_db, mode=LaneMode.SHADOW)
    halts: list[str] = []
    dog = _dog(tmp_db, halts)
    # `tick()` reads the SHIPPED interval, so the overrun has to be built from it rather
    # than from a literal -- at the shipped 12 s a 6 s tick is simply a fast tick.
    interval_s = float(dog.config().poll_interval_s)
    over_s = interval_s + 1.0
    clock = [100.0]

    def fake_monotonic() -> float:
        clock[0] += over_s  # one call at tick start, one at tick end
        return clock[0]

    monkeypatch.setattr(wd.time, "monotonic", fake_monotonic)
    report = dog.tick()
    assert report.duration_ms >= interval_s * 1000
    assert events_named(tmp_db, "tick_overrun"), (
        f"a {over_s:.0f} s tick on a {interval_s:.0f} s interval must be announced"
    )
