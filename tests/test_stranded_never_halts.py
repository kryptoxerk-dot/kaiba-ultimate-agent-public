"""A position the venue already reported as wallet-empty must never halt entries.

MEASURED 2026-10-01: pos_9cb9 had been deferred as `wallet_empty` (the GMGN copy trade on
the same wallet had sold the tokens). Its hourly recheck could not price the dead token,
`_check_position` returned from the unusable branch without renewing the deferral, the
position fell back onto every tick, and 300 s later `_blind_timeout` halted entries on
EVERY chain for 5 h 10 min. It was the fourth halt from that one position since 09-28.

Every test drives the real tick loop and real persistence.
"""

from __future__ import annotations

from kaiba.core.db import fetch_one, jdump
from kaiba.core.schemas import LaneMode, now_ms
from tests.test_watchdog_blind import (  # noqa: F401 - risk_file is a fixture
    RecordingSubmitter,
    SwitchableSource,
    age_blindness,
    blind_dog,
    events_named,
    halted_row,
    make_position,
    risk_file,
)
from kaiba.execution import watchdog as wd


def _stranded(conn, pid: str = "pos_blind", *, due_ms: int | None = None, reason: str = "wallet_empty"):
    conn.execute(
        "INSERT OR REPLACE INTO kv (key, value, updated_ms) VALUES (?,?,?)",
        (
            f"watchdog.stranded:{pid}",
            jdump({
                "recheck_after_ms": now_ms() - 1 if due_ms is None else due_ms,
                "attempts": 40,
                "reason": reason,
            }),
            now_ms(),
        ),
    )


def _marker(conn, pid: str = "pos_blind"):
    return fetch_one(conn, "SELECT value FROM kv WHERE key=?", (f"watchdog.stranded:{pid}",))


def test_a_wallet_empty_position_pages_but_never_halts_entries(tmp_db, risk_file):
    make_position(tmp_db, mode=LaneMode.LIVE)
    _stranded(tmp_db)                       # hourly recheck due now
    dog = blind_dog(tmp_db)

    dog.tick()                              # unpriceable recheck: the blind clock starts
    age_blindness(tmp_db, "pos_blind", 3_600)
    _stranded(tmp_db)                       # the next hourly recheck is due
    dog.tick()

    pages = events_named(tmp_db, "protection_blind_timeout")
    assert len(pages) == 1, "the operator is still told"
    assert pages[0]["venue_reported_empty"] is True
    assert pages[0]["entries_halt_requested"] is False
    assert halted_row(tmp_db) is None, "a position the venue says is empty must not brake the book"


def test_positive_control_without_venue_evidence_it_still_halts(tmp_db, risk_file):
    """Without this, the test above would pass on a watchdog that never halts at all."""
    make_position(tmp_db, mode=LaneMode.LIVE)
    dog = blind_dog(tmp_db)

    dog.tick()
    age_blindness(tmp_db, "pos_blind", 3_600)
    dog.tick()

    assert halted_row(tmp_db)["halt_reason"] == "protection_blind_timeout:pos_blind"
    assert events_named(tmp_db, "protection_blind_timeout")[0]["venue_reported_empty"] is False


def test_a_marker_for_another_reason_does_not_exempt(tmp_db, risk_file):
    make_position(tmp_db, mode=LaneMode.LIVE)
    _stranded(tmp_db, reason="something_else")
    dog = blind_dog(tmp_db)

    dog.tick()
    age_blindness(tmp_db, "pos_blind", 3_600)
    dog.tick()

    assert halted_row(tmp_db) is not None


def test_an_unpriceable_recheck_stays_on_the_hourly_cadence(tmp_db, risk_file):
    make_position(tmp_db, mode=LaneMode.LIVE)
    _stranded(tmp_db)
    dog = blind_dog(tmp_db)

    first = dog.tick()                      # due -> examined -> re-deferred
    second = dog.tick()

    assert first.checked == 1
    assert second.stranded_deferred == 1 and second.checked == 0
    due = int(__import__("json").loads(_marker(tmp_db)["value"])["recheck_after_ms"])
    assert due > now_ms() + (wd.STRANDED_RECHECK_S - 60) * 1000


def test_an_unpriceable_tick_never_creates_a_deferral(tmp_db, risk_file):
    """Deferral still originates only in the wallet_empty branch."""
    make_position(tmp_db, mode=LaneMode.LIVE)
    dog = blind_dog(tmp_db)

    dog.tick()
    dog.tick()

    assert _marker(tmp_db) is None


def test_an_accepted_sell_clears_the_marker(tmp_db, risk_file):
    """Tokens came back (e.g. bought again into the same wallet) and a sell went through:
    the wallet-empty evidence is stale and must not exempt this position any more."""
    make_position(tmp_db, mode=LaneMode.LIVE, entry="1.0")
    _stranded(tmp_db)
    submitter = RecordingSubmitter()
    dog = wd.Watchdog(tmp_db, price_source=SwitchableSource("0.6"), submitter=submitter)

    dog.tick()

    assert submitter.calls, "a 40% drawdown must trigger a sell"
    assert _marker(tmp_db) is None
