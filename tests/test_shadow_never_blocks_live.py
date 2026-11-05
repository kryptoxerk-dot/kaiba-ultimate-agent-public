"""A paper position nobody can price must never cost live protection its tick budget.

MEASURED 2026-09-23 on the live box. Three SHADOW ``migration-fade`` positions aged 20.6 h,
29.2 h and 31.6 h could not be priced by any source. Each went blind and "restored" on
every single tick -- 118 cycles in 30 minutes apiece -- and the shadow quote rotation kept
paying for two real fetches a tick, each walking the whole fallback chain to a timeout:

    ticks carrying 3 blind positions   9,637 ms
    ticks carrying 1 blind position    4,722 ms      (budget: 5,000 ms)

118 of 400 ticks went over budget, which re-armed ``protection_overrun``, which halts
entries on EVERY chain. Paper money stopped real money trading, for over a day.

``migration-fade``'s own lane parameters cap a hold at 1,200 s, so these were 60-93x past
the horizon the lane itself defines. There was no maximum age and no abandon path, so an
unpriceable paper position stayed open forever.

The dangerous version of this fix abandons any blind position. A LIVE position that cannot
be priced is money at risk we cannot see -- it must halt and shout, never be tidied away --
so that exclusion is the property tested hardest here.
"""

from __future__ import annotations

import pytest

from kaiba.core.schemas import Chain, Lane, LaneMode
from kaiba.execution import watchdog as wd
from tests.test_watchdog import make_position

TOKEN = "8J69rbLTzWWgUJziFY8jeu1111111111111111111111"


def aged_position(conn, *, mode: LaneMode, age_s: float, token: str = TOKEN,
                  position_id: str = "pos_1"):
    make_position(conn, position_id=position_id, token=token, chain=Chain.SOL, mode=mode,
                  lane=Lane.MIGRATION_FADE, qty=4_957_840_445)
    conn.execute(
        "UPDATE positions SET opened_ms=?, qty_total=?, cost_native=?, proceeds_native=0 "
        "WHERE token=?",
        (wd.now_ms() - int(age_s * 1000), 4_957_840_445, 60_000_000, token),
    )
    conn.commit()
    return next(p for p in wd.open_positions(conn) if p.token == token)


UNPRICEABLE = wd.PriceQuote.unavailable("no source could price this")


def priceable() -> wd.PriceQuote:
    """A usable quote observed NOW.

    Built per test, not at import: ``observed_ms`` defaults to the construction time, so a
    module-level quote went stale ~15 s into a full-suite run, stopped being ``usable``,
    and the "left alone" test then (correctly) saw an abandon.
    """
    quote = wd.PriceQuote(price_usd=__import__("decimal").Decimal("0.5"),
                          basis=__import__("kaiba.core.schemas", fromlist=["EvidenceBasis"])
                          .EvidenceBasis.PROVIDER_REPORTED)
    assert quote.usable  # positive control: the guard under test sees a priceable position
    return quote


# ------------------------------------------------------------------ the exclusion


def test_a_live_position_is_never_abandoned(tmp_db):
    """THE GUARD: money at risk we cannot see must halt and shout, not be tidied away."""
    position = aged_position(tmp_db, mode=LaneMode.LIVE, age_s=40 * 3600)
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    assert dog._abandon_unpriceable_shadow(position, UNPRICEABLE) is False
    assert wd.open_positions(tmp_db), "a live position was closed for being blind"


@pytest.mark.parametrize("mode", [LaneMode.LIVE, LaneMode.CANARY])
def test_only_shadow_is_eligible(tmp_db, mode):
    position = aged_position(tmp_db, mode=mode, age_s=40 * 3600)
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    assert dog._abandon_unpriceable_shadow(position, UNPRICEABLE) is False


# ------------------------------------------------------------------ the abandon


def test_an_old_unpriceable_shadow_is_abandoned(tmp_db):
    """THE REGRESSION: three of these halted every chain for over a day."""
    position = aged_position(tmp_db, mode=LaneMode.SHADOW, age_s=31 * 3600)
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    assert dog._abandon_unpriceable_shadow(position, UNPRICEABLE) is True
    row = tmp_db.execute(
        "SELECT closed_ms, exit_reason FROM positions WHERE token=?", (TOKEN,)
    ).fetchone()
    assert row["closed_ms"] is not None
    assert row["exit_reason"] == "abandoned_unpriceable"


def test_a_shadow_we_can_still_price_is_left_alone(tmp_db):
    """Age alone is not a reason: a position we can follow is doing its job."""
    position = aged_position(tmp_db, mode=LaneMode.SHADOW, age_s=40 * 3600)
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    assert dog._abandon_unpriceable_shadow(position, priceable()) is False
    assert wd.open_positions(tmp_db)


def test_a_young_blind_shadow_is_given_time(tmp_db):
    """A provider having a bad few minutes must not cost a paper position."""
    position = aged_position(tmp_db, mode=LaneMode.SHADOW, age_s=60)
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    assert dog._abandon_unpriceable_shadow(position, UNPRICEABLE) is False
    assert wd.open_positions(tmp_db)


def test_the_boundary_is_the_configured_hour(tmp_db):
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    young = aged_position(tmp_db, mode=LaneMode.SHADOW,
                          age_s=wd.SHADOW_BLIND_ABANDON_S - 30, token=TOKEN,
                          position_id="pos_young")
    assert dog._abandon_unpriceable_shadow(young, UNPRICEABLE) is False
    old = aged_position(tmp_db, mode=LaneMode.SHADOW,
                        age_s=wd.SHADOW_BLIND_ABANDON_S + 30, token="9" + TOKEN[1:],
                        position_id="pos_old")
    assert dog._abandon_unpriceable_shadow(old, UNPRICEABLE) is True


def test_the_abandon_invents_no_proceeds(tmp_db):
    """The paper record must read as 'we stopped being able to follow this'."""
    position = aged_position(tmp_db, mode=LaneMode.SHADOW, age_s=31 * 3600)
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    dog._abandon_unpriceable_shadow(position, UNPRICEABLE)
    row = tmp_db.execute(
        "SELECT proceeds_native FROM positions WHERE token=?", (TOKEN,)
    ).fetchone()
    assert int(row["proceeds_native"]) == 0


# ------------------------------------------------------------------ it is actually reached


def test_the_tick_reaches_the_abandon_path(tmp_db, monkeypatch):
    """The method existing is not enough -- the blind branch has to call it.

    Without this, the abandon works in isolation and the rotation keeps paying for a real
    fetch on the very next tick, which is the entire cost this exists to remove.
    """
    aged_position(tmp_db, mode=LaneMode.SHADOW, age_s=31 * 3600)
    monkeypatch.setattr(wd.Watchdog, "_quote", lambda self, position: UNPRICEABLE)
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    dog.tick()
    row = tmp_db.execute(
        "SELECT closed_ms, exit_reason FROM positions WHERE token=?", (TOKEN,)
    ).fetchone()
    assert row["closed_ms"] is not None, "the tick never reached the abandon path"
    assert row["exit_reason"] == "abandoned_unpriceable"


def test_an_abandoned_shadow_stops_being_checked(tmp_db, monkeypatch):
    """The point of the whole thing: it must leave the rotation."""
    aged_position(tmp_db, mode=LaneMode.SHADOW, age_s=31 * 3600)
    monkeypatch.setattr(wd.Watchdog, "_quote", lambda self, position: UNPRICEABLE)
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    dog.tick()
    assert dog.tick().checked == 0, "an abandoned position was still checked"
