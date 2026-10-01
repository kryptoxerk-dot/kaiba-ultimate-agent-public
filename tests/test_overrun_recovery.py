"""A failsafe that cannot recover is an outage.

MEASURED 2026-09-22: the protection-overrun failsafe fired correctly on three consecutive
ticks of 6.1 / 14.6 / 18.5 s against a 5 s interval, and then entries stayed halted for
40 MINUTES across sol, bsc and robinhood while the watchdog was back to 0.8-2.1 s ticks.
17 decisions were skipped citing `risk_halt`, zero entries were made, and nothing was
wrong any more. The halt was right; having no way back was not.

The safety property under test is the prefix match: an automatic resume may lift ONLY the
halt this mechanism set. An operator halt, a daily-loss-stop halt, or anything set by
another subsystem must survive any amount of protection health.
"""

from __future__ import annotations

from types import SimpleNamespace

from kaiba.core.schemas import Chain, Lane
from kaiba.execution import watchdog as wd
from kaiba.execution.risk import RiskGate
from tests.test_watchdog import (  # noqa: F401 - fixtures used by name
    TOKEN, FakeSource, RecordingSubmitter, events_named, make_position, risk_file,
)

SOL, LANE = Chain.SOL, Lane.CONFLUENCE_5


#: Pinned, not read from `config/risk.yaml`. MEASURED 2026-09-24: the shipped
#: `poll_interval_s` moved 5 -> 12, so the 12,000 ms "chronic" tick these tests use
#: stopped being >=2x the interval and the halt they assert simply never fired. The rule
#: under test is the recovery prefix match, not whatever the operator has the poll set to.
INTERVAL_S = 5


def _cfg(dog) -> wd.ProtectionConfig:
    return dog.config().model_copy(update={"poll_interval_s": INTERVAL_S})


def _report(duration_ms: int) -> wd.TickReport:
    r = wd.TickReport()
    r.duration_ms = duration_ms
    r.checked = 3
    return r


def _dog(tmp_db) -> wd.Watchdog:
    return wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "1.0"}), submitter=RecordingSubmitter())


def _halted(conn) -> tuple[int, str | None]:
    row = conn.execute("SELECT halted, halt_reason FROM risk_state ORDER BY day_key DESC LIMIT 1").fetchone()
    return (int(row["halted"]), row["halt_reason"]) if row else (0, None)


# ------------------------------------------------------------------ recovery


def test_sustained_health_lifts_the_halt_this_watchdog_set(tmp_db, risk_file):
    dog = _dog(tmp_db)
    cfg = _cfg(dog)
    for _ in range(wd.OVERRUN_HALT_TICKS):
        dog._note_overrun(_report(12_000), cfg)
    assert _halted(tmp_db)[0] == 1, "the failsafe should have halted entries"

    for _ in range(wd.OVERRUN_RECOVER_TICKS):
        dog._note_overrun(_report(900), cfg)

    assert _halted(tmp_db) == (0, None), "sustained health must lift its own halt"
    assert events_named(tmp_db, "overrun_halt_cleared")


def test_a_brief_recovery_is_not_enough(tmp_db, risk_file):
    """One good tick after a storm proves nothing; the halt holds."""
    dog = _dog(tmp_db)
    cfg = _cfg(dog)
    for _ in range(wd.OVERRUN_HALT_TICKS):
        dog._note_overrun(_report(12_000), cfg)
    for _ in range(wd.OVERRUN_RECOVER_TICKS - 1):
        dog._note_overrun(_report(900), cfg)
    assert _halted(tmp_db)[0] == 1, "recovery fired too early"


def test_an_isolated_relapse_delays_recovery_without_cancelling_it(tmp_db, risk_file):
    """Amended 2026-09-22 after this rule caused an outage on the live box.

    It used to assert that a single relapse RESTARTS the count. Measured against the
    watchdog's own heartbeats that afternoon: nine ticks in ten ran 2.4-3.4 s against a
    5 s budget while about one a minute spiked to 15-16 s, always with ``consecutive: 1``,
    never the chronic run that halts. Under the old rule the counter was zeroed every
    eight to fourteen ticks and the entry halt on all three chains could never lift, while
    protection was healthy by its own definition.

    The halt fires on a CHRONIC condition, so only a chronic one may cancel recovery. An
    isolated spike costs ``OVERRUN_SPIKE_PENALTY`` ticks of progress -- it is still a late
    stop and must not be free -- and the next test pins that a chronic relapse still
    wipes it.
    """
    dog = _dog(tmp_db)
    cfg = _cfg(dog)
    for _ in range(wd.OVERRUN_HALT_TICKS):
        dog._note_overrun(_report(12_000), cfg)

    # Just short of recovery, then one isolated spike: it must still be halted.
    for _ in range(wd.OVERRUN_RECOVER_TICKS - 1):
        dog._note_overrun(_report(900), cfg)
    dog._note_overrun(_report(12_000), cfg)
    assert _halted(tmp_db)[0] == 1, "a spike must delay recovery"

    # ...and the delay must be finite: more clean ticks than the spike cost, and it lifts.
    for _ in range(wd.OVERRUN_SPIKE_PENALTY + 1):
        dog._note_overrun(_report(900), cfg)
    assert _halted(tmp_db)[0] == 0, "an isolated spike must not make the halt unliftable"


def test_a_chronic_relapse_still_restarts_the_recovery_count(tmp_db, risk_file):
    """The condition that halts is still the condition that cancels recovery outright."""
    dog = _dog(tmp_db)
    cfg = _cfg(dog)
    for _ in range(wd.OVERRUN_HALT_TICKS):
        dog._note_overrun(_report(12_000), cfg)
    for _ in range(wd.OVERRUN_RECOVER_TICKS - 2):
        dog._note_overrun(_report(900), cfg)
    for _ in range(wd.OVERRUN_HALT_TICKS):           # a CHRONIC relapse
        dog._note_overrun(_report(12_000), cfg)
    for _ in range(wd.OVERRUN_RECOVER_TICKS - 2):
        dog._note_overrun(_report(900), cfg)
    assert _halted(tmp_db)[0] == 1, "a chronic relapse must restart the count"


# ------------------------------------------------------------------ the safety property


def test_an_operator_halt_is_never_lifted(tmp_db, risk_file):
    """The whole reason the resume is prefix-scoped."""
    RiskGate().halt("operator: stand down", tmp_db)
    dog = _dog(tmp_db)
    cfg = _cfg(dog)
    for _ in range(wd.OVERRUN_RECOVER_TICKS * 3):
        dog._note_overrun(_report(500), cfg)
    assert _halted(tmp_db) == (1, "operator: stand down")


def test_a_daily_loss_stop_halt_is_never_lifted(tmp_db, risk_file):
    RiskGate().halt("daily_loss_stop", tmp_db)
    dog = _dog(tmp_db)
    cfg = _cfg(dog)
    for _ in range(wd.OVERRUN_RECOVER_TICKS * 3):
        dog._note_overrun(_report(500), cfg)
    assert _halted(tmp_db)[1] == "daily_loss_stop"


def test_the_gate_helper_refuses_a_non_matching_reason(tmp_db):
    g = RiskGate()
    g.halt("something_else:12345", tmp_db)
    assert g.resume_if_reason_starts_with("protection_overrun:", tmp_db) is False
    assert _halted(tmp_db)[0] == 1
    assert g.resume_if_reason_starts_with("something_else:", tmp_db) is True
    assert _halted(tmp_db)[0] == 0


def test_the_helper_is_a_no_op_when_nothing_is_halted(tmp_db):
    assert RiskGate().resume_if_reason_starts_with("protection_overrun:", tmp_db) is False
