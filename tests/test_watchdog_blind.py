"""The max-blind budget: what the watchdog does when it has been unable to price a
position for longer than it is willing to stay quiet about.

Before this budget existed, a position the watchdog could not price produced the same
``protection_blind`` error every 60 s forever and nothing else ever happened — no
escalation, no brake, and a heartbeat that said ``warn`` on minute one and ``warn`` on
minute four hundred. These tests pin the three things that changed:

* past ``protection.max_blind_s`` the watchdog **acts**, exactly once per blind episode,
  and the action is a page plus (for a position holding real tokens) an entry brake —
  never a sell, because a sell needs the price we do not have;
* a price that comes back resets the clock *and* re-arms the page;
* a brief outage — 17 s is the longest one the live box has ever recorded — is not
  acted on at all.

Every test here drives the real loop and real persistence. Blindness is aged by
backdating ``watchdog_state.blind_since_ms``, which is what a real outage does to it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import yaml

from kaiba.core import events as ev
from kaiba.core.config import load_risk, save_risk
from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import (
    Chain,
    EventKind,
    EvidenceBasis,
    Lane,
    LaneMode,
    OrderState,
    now_ms,
)
from kaiba.execution import watchdog as wd
from kaiba.execution.risk import RiskGate

TOKEN = "Bnd5oBSWpPpXoaWTckgXTVx9TCKkX5GyVqxcrwBzUYsB"

#: The longest blind spell ever observed on the live box (protection_restored.blind_for_s
#: was 4, 5, 5 and 17 s; n=4, read 2026-09-21). A budget that acts on this is a budget
#: that pages on provider noise.
LONGEST_REAL_TRANSIENT_S = 17


# --------------------------------------------------------------------------------------
# fixtures and doubles
# --------------------------------------------------------------------------------------


@pytest.fixture
def risk_file(tmp_path, monkeypatch):
    """An isolated risk.yaml, so a test can set protection keys without touching config/."""
    path = tmp_path / "risk.yaml"
    save_risk(load_risk(), path)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))
    return path


def set_protection(path, **keys) -> None:
    """Write keys into the ``protection:`` block of the isolated risk file."""
    raw = yaml.safe_load(path.read_text()) or {}
    block = dict(raw.get("protection") or {})
    for key, value in keys.items():
        if value is _ABSENT:
            block.pop(key, None)
        else:
            block[key] = value
    raw["protection"] = block
    path.write_text(yaml.safe_dump(raw))


class _Absent:
    pass


_ABSENT = _Absent()


def make_position(
    conn,
    *,
    position_id: str = "pos_blind",
    token: str = TOKEN,
    entry: str | None = "1.0",
    qty: int = 1_000_000,
    mode: LaneMode = LaneMode.SHADOW,
    lane: Lane = Lane.PONS_ROBINHOOD,
    chain: Chain = Chain.SOL,
) -> str:
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty, qty_total, "
        "cost_native, proceeds_native, realized_native, entry_price_usd, peak_price_usd) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            position_id, chain.value, token, lane.value, mode.value, now_ms(),
            str(qty), str(qty), "5000000", "0", "0", entry, entry,
        ),
    )
    return position_id


class SwitchableSource:
    """A price source the test can blind and un-blind between ticks."""

    name = "switchable"

    def __init__(self, price: str | None = None) -> None:
        self.price = price

    def quote(self, chain: Chain, token: str) -> wd.PriceQuote:
        if self.price is None:
            return wd.PriceQuote.unavailable("fixture is blind", source=self.name)
        return wd.PriceQuote(
            price_usd=Decimal(self.price),
            liquidity_usd=Decimal("1000000"),
            basis=EvidenceBasis.PROVIDER_REPORTED,
            source=self.name,
        )


class RecordingSubmitter:
    """Any call to this at all is a bug: a blind position must never be sold."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Decimal, str]] = []

    def submit_exit(self, position, pct, *, quote, reason):  # noqa: ANN001 - test double
        self.calls.append((position.position_id, pct, reason))
        return wd.ExitOutcome(True, OrderState.FILLED, "ord_should_not_exist", "")


class ExplodingHalt(RiskGate):
    """A risk gate whose brake is jammed."""

    def halt(self, reason, conn=None):  # noqa: ANN001 - matches RiskGate.halt
        raise RuntimeError("risk_state is locked")


def events_named(conn, name: str) -> list[dict]:
    return [
        e.payload
        for e in ev.recent(limit=500, conn=conn)
        if isinstance(e.payload, dict) and e.payload.get("event") == name
    ]


def levels_of(conn, name: str) -> list[str]:
    return [
        e.level
        for e in ev.recent(limit=500, conn=conn)
        if isinstance(e.payload, dict) and e.payload.get("event") == name
    ]


def halted_row(conn) -> dict | None:
    return fetch_one(conn, "SELECT halted, halt_reason FROM risk_state WHERE halted=1 LIMIT 1")


def age_blindness(conn, position_id: str, seconds: int) -> None:
    """Backdate the persisted blind clock, which is what a real outage does to it.

    ``last_blind_warn_ms`` is deliberately left where it is: the escalation must not be
    reachable only on the 60 s warn tick.
    """
    updated = conn.execute(
        "UPDATE watchdog_state SET blind_since_ms=? WHERE position_id=?",
        (now_ms() - seconds * 1000, position_id),
    ).rowcount
    assert updated == 1, "no watchdog_state row to age; tick once before ageing blindness"


def blind_dog(conn, *, submitter=None, **kw) -> wd.Watchdog:
    return wd.Watchdog(
        conn,
        price_source=wd.NullPriceSource(),
        submitter=submitter or RecordingSubmitter(),
        **kw,
    )


# --------------------------------------------------------------------------------------
# a brief outage is not an emergency
# --------------------------------------------------------------------------------------


def test_a_brief_blind_spell_is_not_acted_on(tmp_db, risk_file):
    """17 s is the longest blind spell the live box has ever recovered from. Acting on it
    would mean paging on ordinary provider noise."""
    make_position(tmp_db, mode=LaneMode.LIVE)
    dog = blind_dog(tmp_db)

    first = dog.tick()
    age_blindness(tmp_db, "pos_blind", LONGEST_REAL_TRANSIENT_S)
    second = dog.tick()

    assert first.blind_over_budget == 0
    assert second.blind_over_budget == 0
    assert second.longest_blind_s == LONGEST_REAL_TRANSIENT_S
    assert events_named(tmp_db, "protection_blind_timeout") == []
    assert halted_row(tmp_db) is None
    # and it is still loudly blind, which is the pre-existing behaviour
    assert events_named(tmp_db, "protection_blind")


def test_one_second_under_the_budget_is_still_only_a_warning(tmp_db, risk_file):
    """The boundary, pinned from below so an off-by-one cannot hide here."""
    make_position(tmp_db, mode=LaneMode.LIVE)
    dog = blind_dog(tmp_db, max_blind_s=600)

    dog.tick()
    age_blindness(tmp_db, "pos_blind", 599)
    report = dog.tick()

    assert report.blind_over_budget == 0
    assert events_named(tmp_db, "protection_blind_timeout") == []


def test_the_budget_is_spent_at_the_budget_not_a_tick_later(tmp_db, risk_file):
    """The boundary from above. ``max_blind_s`` is a budget, and a spent budget is spent."""
    make_position(tmp_db, mode=LaneMode.LIVE)
    dog = blind_dog(tmp_db, max_blind_s=600)

    dog.tick()
    age_blindness(tmp_db, "pos_blind", 600)
    report = dog.tick()

    assert report.blind_over_budget == 1
    assert len(events_named(tmp_db, "protection_blind_timeout")) == 1


# --------------------------------------------------------------------------------------
# past the budget: act, once
# --------------------------------------------------------------------------------------


def test_blind_past_the_budget_pages_once_not_every_tick(tmp_db, risk_file):
    make_position(tmp_db, mode=LaneMode.LIVE)
    dog = blind_dog(tmp_db)

    dog.tick()
    age_blindness(tmp_db, "pos_blind", 2_400)  # forty minutes
    reports = [dog.tick() for _ in range(4)]

    pages = events_named(tmp_db, "protection_blind_timeout")
    assert len(pages) == 1, "the page must be once per blind episode, not once per tick"
    assert levels_of(tmp_db, "protection_blind_timeout") == ["error"]
    # the *counter*, unlike the page, is the truth on every tick
    assert [r.blind_over_budget for r in reports] == [1, 1, 1, 1]
    assert all(r.longest_blind_s >= 2_400 for r in reports)

    page = pages[0]
    assert page["position_id"] == "pos_blind"
    assert page["blind_for_s"] >= 2_400
    assert page["max_blind_s"] == wd.MAX_BLIND_S_DEFAULT
    assert page["basis"] == EvidenceBasis.UNAVAILABLE.value
    assert "NO working stop loss" in page["impact"]


def test_the_page_is_not_re_attempted_on_the_bus_every_tick(tmp_db, risk_file, monkeypatch):
    """The dedupe key is what makes the page one-per-episode; the in-process latch is what
    stops a five-second loop from firing a doomed unique-index insert forever. Without it
    the observable event count is still right and the waste is invisible, so it is pinned
    here rather than left to be optimised away by someone reading only the event log."""
    attempts: list[str] = []
    real_emit = wd.ev.emit

    def counting_emit(kind, payload=None, **kw):  # noqa: ANN001 - wraps ev.emit
        key = kw.get("dedupe_key")
        if key and "blind_timeout" in key:
            attempts.append(key)
        return real_emit(kind, payload, **kw)

    monkeypatch.setattr(wd.ev, "emit", counting_emit)
    make_position(tmp_db, mode=LaneMode.LIVE)
    dog = blind_dog(tmp_db)

    dog.tick()
    age_blindness(tmp_db, "pos_blind", 2_400)
    for _ in range(6):
        dog.tick()

    assert len(attempts) == 1, f"one write attempt per blind episode, not {len(attempts)}"


def test_the_page_pulls_the_entry_brake_for_a_position_holding_real_tokens(tmp_db, risk_file):
    make_position(tmp_db, mode=LaneMode.LIVE)
    dog = blind_dog(tmp_db)

    dog.tick()
    age_blindness(tmp_db, "pos_blind", 2_400)
    dog.tick()

    row = halted_row(tmp_db)
    assert row is not None, "a live position with no working stop must stop new entries"
    assert row["halt_reason"] == "protection_blind_timeout:pos_blind"
    assert events_named(tmp_db, "protection_blind_timeout")[0]["entries_halt_requested"] is True
    # the brake rides the existing alarm channel the dashboard already filters on
    assert [e.kind for e in ev.recent(limit=200, conn=tmp_db)].count(EventKind.RISK_HALT.value) == 1


def test_the_brake_is_pulled_once_even_though_the_position_stays_blind(tmp_db, risk_file):
    make_position(tmp_db, mode=LaneMode.LIVE)
    dog = blind_dog(tmp_db)

    dog.tick()
    age_blindness(tmp_db, "pos_blind", 2_400)
    for _ in range(5):
        dog.tick()

    halts = [e for e in ev.recent(limit=300, conn=tmp_db) if e.kind == EventKind.RISK_HALT.value]
    assert len(halts) == 1, "re-halting every tick would fight an operator who resumed"


def test_a_shadow_position_pages_but_never_brakes_a_live_book(tmp_db, risk_file):
    """A shadow position never held tokens. Its blindness is a data-quality problem, and
    braking real entries over it would be the failsafe causing the outage."""
    make_position(tmp_db, mode=LaneMode.SHADOW)
    dog = blind_dog(tmp_db)

    dog.tick()
    age_blindness(tmp_db, "pos_blind", 2_400)
    dog.tick()

    page = events_named(tmp_db, "protection_blind_timeout")[0]
    assert page["real_tokens"] is False
    assert page["entries_halt_requested"] is False
    assert halted_row(tmp_db) is None


def test_a_canary_position_is_treated_as_real_tokens(tmp_db, risk_file):
    """`DefaultExitSubmitter` routes anything that is not SHADOW to the real executor, so
    anything that is not SHADOW is real money as far as this failsafe is concerned."""
    make_position(tmp_db, mode=LaneMode.CANARY)
    dog = blind_dog(tmp_db)

    dog.tick()
    age_blindness(tmp_db, "pos_blind", 2_400)
    dog.tick()

    assert events_named(tmp_db, "protection_blind_timeout")[0]["real_tokens"] is True
    assert halted_row(tmp_db) is not None


def test_the_watchdog_never_sells_a_position_it_cannot_price(tmp_db, risk_file):
    """The whole justification for paging instead of forcing an exit. A market sell with
    no min_out into a book we cannot see is not the safe option."""
    submitter = RecordingSubmitter()
    make_position(tmp_db, mode=LaneMode.LIVE)
    dog = blind_dog(tmp_db, submitter=submitter)

    dog.tick()
    age_blindness(tmp_db, "pos_blind", 86_400)  # a full day blind
    dog.tick()

    assert submitter.calls == []
    assert fetch_all(tmp_db, "SELECT * FROM orders") == []
    page = events_named(tmp_db, "protection_blind_timeout")[0]
    assert page["forced_exit"] is False
    assert "min_out" in page["forced_exit_refused"]


def test_a_position_with_no_entry_price_is_budgeted_too(tmp_db, risk_file):
    """The other blind path. A position we cannot value at all is no less unprotected."""
    make_position(tmp_db, entry=None, mode=LaneMode.LIVE)
    dog = wd.Watchdog(
        tmp_db, price_source=SwitchableSource("0.5"), submitter=RecordingSubmitter()
    )

    dog.tick()
    age_blindness(tmp_db, "pos_blind", 2_400)
    dog.tick()

    page = events_named(tmp_db, "protection_blind_timeout")[0]
    assert page["reason"] == "entry_price_unavailable"
    assert halted_row(tmp_db) is not None


def test_a_deferred_exit_request_is_named_on_the_page(tmp_db, risk_file):
    """Somebody asked us to close this and we have not been able to. That belongs on the
    page, not only in the throttled warning."""
    make_position(tmp_db, mode=LaneMode.LIVE)
    ev.emit(
        EventKind.PROTECTION_TRIGGERED,
        {"position_id": "pos_blind", "pct": "100", "reason": "agent wants out", "source": "agent"},
        conn=tmp_db,
    )
    dog = blind_dog(tmp_db)

    dog.tick()
    age_blindness(tmp_db, "pos_blind", 2_400)
    dog.tick()

    assert events_named(tmp_db, "protection_blind_timeout")[0]["deferred_exit_pct"] == "100"


# --------------------------------------------------------------------------------------
# a price resets the clock
# --------------------------------------------------------------------------------------


def test_a_price_that_comes_back_resets_the_clock(tmp_db, risk_file):
    source = SwitchableSource(None)
    make_position(tmp_db, mode=LaneMode.LIVE)
    dog = wd.Watchdog(tmp_db, price_source=source, submitter=RecordingSubmitter())

    dog.tick()
    age_blindness(tmp_db, "pos_blind", 2_400)
    source.price = "1.0"
    report = dog.tick()

    assert report.blind_over_budget == 0
    assert events_named(tmp_db, "protection_blind_timeout") == []
    assert halted_row(tmp_db) is None
    restored = events_named(tmp_db, "protection_restored")[0]
    assert restored["blind_for_s"] >= 2_400 and restored["was_over_budget"] is True
    row = fetch_one(
        tmp_db, "SELECT blind_since_ms FROM watchdog_state WHERE position_id=?", ("pos_blind",)
    )
    assert row["blind_since_ms"] is None


def test_a_recovered_position_that_goes_blind_again_is_paged_again(tmp_db, risk_file):
    """The clock resets, so the page has to re-arm with it. A second outage is a second
    emergency, not a duplicate of the first."""
    source = SwitchableSource(None)
    make_position(tmp_db, mode=LaneMode.LIVE)
    dog = wd.Watchdog(tmp_db, price_source=source, submitter=RecordingSubmitter())

    dog.tick()
    age_blindness(tmp_db, "pos_blind", 2_400)
    dog.tick()                                   # page 1
    source.price = "1.0"
    dog.tick()                                   # recovered
    source.price = None
    dog.tick()                                   # blind again, freshly
    age_blindness(tmp_db, "pos_blind", 2_400)
    dog.tick()                                   # page 2

    pages = events_named(tmp_db, "protection_blind_timeout")
    assert len(pages) == 2
    assert pages[0]["blind_since_ms"] != pages[1]["blind_since_ms"]


def test_a_restart_does_not_re_page_or_re_brake_the_same_blind_episode(tmp_db, risk_file):
    """The in-process latch cannot survive a restart; the event's dedupe key can. An
    operator who resumed after the first page must not be overruled by a bounce."""
    make_position(tmp_db, mode=LaneMode.LIVE)
    first = blind_dog(tmp_db)
    first.tick()
    age_blindness(tmp_db, "pos_blind", 2_400)
    first.tick()
    assert halted_row(tmp_db) is not None
    RiskGate().resume(tmp_db)  # the operator acknowledges and resumes

    second = blind_dog(tmp_db)  # a fresh process, same database, same blind episode
    second.tick()

    assert len(events_named(tmp_db, "protection_blind_timeout")) == 1
    assert halted_row(tmp_db) is None, "a restart must not re-halt an episode already paged"


# --------------------------------------------------------------------------------------
# the operator can tell healthy from blind-for-forty-minutes
# --------------------------------------------------------------------------------------


def test_the_heartbeat_separates_healthy_brief_and_over_budget(tmp_db, risk_file):
    source = SwitchableSource("1.0")
    make_position(tmp_db, mode=LaneMode.LIVE)
    dog = wd.Watchdog(tmp_db, price_source=source, submitter=RecordingSubmitter())

    dog.tick()
    healthy = ev.recent(limit=5, conn=tmp_db)[0]
    assert healthy.payload["event"] == "heartbeat"
    assert healthy.level == "info"
    assert healthy.payload["blind_over_budget"] == 0
    assert healthy.payload["blind_note"] is None
    assert healthy.payload["max_blind_s"] == wd.MAX_BLIND_S_DEFAULT

    source.price = None
    dog.tick()
    age_blindness(tmp_db, "pos_blind", LONGEST_REAL_TRANSIENT_S)
    dog.tick()
    brief = [e for e in ev.recent(limit=50, conn=tmp_db) if e.payload.get("event") == "heartbeat"][0]
    assert brief.level == "warn"
    assert brief.payload["blind"] == 1 and brief.payload["blind_over_budget"] == 0
    assert brief.payload["longest_blind_s"] == LONGEST_REAL_TRANSIENT_S

    age_blindness(tmp_db, "pos_blind", 2_400)
    dog.tick()
    late = [e for e in ev.recent(limit=50, conn=tmp_db) if e.payload.get("event") == "heartbeat"][0]
    assert late.level == "error", "forty minutes blind must not look like one tick blind"
    assert late.payload["blind_over_budget"] == 1
    assert late.payload["longest_blind_s"] >= 2_400
    assert "longer than" in late.payload["blind_note"]


def test_the_run_loop_totals_carry_the_budget_counters(tmp_db, risk_file):
    make_position(tmp_db, mode=LaneMode.LIVE)
    blind_dog(tmp_db).tick()
    age_blindness(tmp_db, "pos_blind", 2_400)

    totals = wd.run_watchdog(
        tmp_db,
        price_source=wd.NullPriceSource(),
        submitter=RecordingSubmitter(),
        interval_s=0.01,
        max_ticks=2,
        install_signals=False,
    )

    assert totals["blind_over_budget"] == 2
    assert totals["longest_blind_s"] >= 2_400
    started = events_named(tmp_db, "started")[0]
    assert started["max_blind_s"] == wd.MAX_BLIND_S_DEFAULT
    assert started["max_blind_halt_entries"] is True


# --------------------------------------------------------------------------------------
# the config, and what happens before it lands
# --------------------------------------------------------------------------------------


def test_the_budget_works_before_the_config_key_exists(tmp_db, risk_file):
    """The failsafe must not wait for a config edit to be deployed."""
    set_protection(risk_file, max_blind_s=_ABSENT, max_blind_halt_entries=_ABSENT)

    assert wd.configured_max_blind_s() == wd.MAX_BLIND_S_DEFAULT
    assert wd.configured_max_blind_halt_entries() is wd.MAX_BLIND_HALT_ENTRIES_DEFAULT

    make_position(tmp_db, mode=LaneMode.LIVE)
    dog = blind_dog(tmp_db)
    dog.tick()
    age_blindness(tmp_db, "pos_blind", wd.MAX_BLIND_S_DEFAULT + 1)
    dog.tick()

    assert len(events_named(tmp_db, "protection_blind_timeout")) == 1


@pytest.mark.parametrize("garbage", ["soon", "", True, [300], None])
def test_an_unreadable_budget_falls_back_to_the_failsafe_not_to_off(risk_file, garbage):
    """A typo in the risk file must not silently switch a failsafe off."""
    set_protection(risk_file, max_blind_s=garbage)
    assert wd.configured_max_blind_s() == wd.MAX_BLIND_S_DEFAULT


@pytest.mark.parametrize("garbage", ["maybe", 7, None])
def test_an_unreadable_halt_flag_falls_back_to_braking(risk_file, garbage):
    set_protection(risk_file, max_blind_halt_entries=garbage)
    assert wd.configured_max_blind_halt_entries() is True


@pytest.mark.parametrize(
    ("written", "expected"),
    [(60, 60), ("900", 900), (0, 0), (-1, -1), (False, wd.MAX_BLIND_S_DEFAULT)],
)
def test_the_budget_is_read_from_the_protection_block(risk_file, written, expected):
    set_protection(risk_file, max_blind_s=written)
    assert wd.configured_max_blind_s() == expected


def test_the_budget_can_be_switched_off_on_purpose(tmp_db, risk_file):
    """``<= 0`` is a real answer and has to be sayable, unlike a typo."""
    set_protection(risk_file, max_blind_s=0)
    make_position(tmp_db, mode=LaneMode.LIVE)
    dog = blind_dog(tmp_db)

    dog.tick()
    age_blindness(tmp_db, "pos_blind", 86_400)
    report = dog.tick()

    assert report.blind_over_budget == 0
    assert events_named(tmp_db, "protection_blind_timeout") == []
    assert halted_row(tmp_db) is None


def test_the_budget_is_re_read_every_tick(tmp_db, risk_file):
    """An operator widening or tightening the budget during an outage should not have to
    restart the service, which is how every other key in this block behaves."""
    set_protection(risk_file, max_blind_s=86_400)
    make_position(tmp_db, mode=LaneMode.LIVE)
    dog = blind_dog(tmp_db)

    dog.tick()
    age_blindness(tmp_db, "pos_blind", 1_200)
    assert dog.tick().blind_over_budget == 0

    set_protection(risk_file, max_blind_s=600)
    assert dog.tick().blind_over_budget == 1
    assert len(events_named(tmp_db, "protection_blind_timeout")) == 1


def test_an_explicit_budget_beats_the_config(tmp_db, risk_file):
    set_protection(risk_file, max_blind_s=86_400)
    make_position(tmp_db, mode=LaneMode.LIVE)
    dog = blind_dog(tmp_db, max_blind_s=30)

    dog.tick()
    age_blindness(tmp_db, "pos_blind", 60)

    assert dog.tick().blind_over_budget == 1


def test_the_brake_can_be_turned_off_without_turning_off_the_page(tmp_db, risk_file):
    set_protection(risk_file, max_blind_halt_entries=False)
    make_position(tmp_db, mode=LaneMode.LIVE)
    dog = blind_dog(tmp_db)

    dog.tick()
    age_blindness(tmp_db, "pos_blind", 2_400)
    dog.tick()

    page = events_named(tmp_db, "protection_blind_timeout")[0]
    assert page["real_tokens"] is True
    assert page["entries_halt_requested"] is False
    assert halted_row(tmp_db) is None


def test_a_brake_that_cannot_be_pulled_is_reported_not_swallowed(tmp_db, risk_file):
    make_position(tmp_db, mode=LaneMode.LIVE)
    dog = blind_dog(tmp_db, risk_gate=ExplodingHalt())

    dog.tick()
    age_blindness(tmp_db, "pos_blind", 2_400)
    report = dog.tick()

    assert report.errors == 0, "a jammed brake is an event, not an outage"
    failure = events_named(tmp_db, "blind_timeout_halt_failed")
    assert len(failure) == 1
    assert "RuntimeError" in failure[0]["error"]
    assert "halt by hand" in failure[0]["impact"]
    assert levels_of(tmp_db, "blind_timeout_halt_failed") == ["error"]
