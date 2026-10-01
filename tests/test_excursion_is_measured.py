"""MFE must be the excursion that happened, not a zero nobody updated.

THE BUG, MEASURED on the live box 2026-09-23: **236 of 269 positions carry ``mfe_pct = 0``
and exactly one is NULL** -- including a ``trailing_stop`` fill whose ``peak_price_usd``
was 0.0000283 against an entry of 0.0000115. That is a +146% excursion recorded as zero.

``accounting.open_position`` sets ``mae_pct=0.0, mfe_pct=0.0`` at creation and nothing ever
writes them again. Two separate faults live in that one line:

* the **value** is wrong, and every exit study that reads it is blind. The diagnosis of
  whether our losses are an entry problem or an exit problem runs on exactly this field;
  it returned "0.0% for every group" and said nothing at all.
* the **basis** is wrong, and that is the worse half. ``0.0`` is a measurement -- it claims
  the position never moved up. ``None`` says we did not record one. CONTRACT rule 2: a
  missing measurement is ``None``, never a zero that reads as evidence.

THE FIX. ``peak_price_usd`` is already tracked correctly by the watchdog's ratchet, so MFE
is derivable at write time and needs no new column and no extra network read. MAE is NOT
derivable -- nothing tracks a trough -- so it stays ``None`` and says so, rather than being
filled with a number that would look measured.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain, Lane, LaneMode, Position
from kaiba.execution import accounting


def position(entry=None, peak=None, **kw) -> Position:
    return Position(
        position_id=kw.get("position_id", "pos_excursion"),
        chain=Chain.SOL,
        token="tok",
        lane=Lane.SM_TRENCHES,
        mode=LaneMode.LIVE,
        opened_ms=1,
        qty=10,
        qty_total=10,
        cost_native=100,
        entry_price_usd=Decimal(str(entry)) if entry is not None else None,
        peak_price_usd=Decimal(str(peak)) if peak is not None else None,
        **{k: v for k, v in kw.items() if k != "position_id"},
    )


# ------------------------------------------------------------------ the derivation


@pytest.mark.parametrize("entry,peak,expected", [
    ("100", "246", 146.0),      # the live case: +146% recorded as 0.0
    ("100", "200", 100.0),
    ("100", "100", 0.0),        # genuinely never moved: a MEASURED zero is fine
    ("0.0000115", "0.0000283", pytest.approx(146.1, abs=0.2)),
])
def test_mfe_is_derived_from_the_peak(entry, peak, expected):
    assert accounting.excursion_pct(
        Decimal(entry), Decimal(peak)) == pytest.approx(expected, abs=0.2)


@pytest.mark.parametrize("entry,peak", [(None, "200"), ("100", None), (None, None), ("0", "5")])
def test_an_underivable_excursion_is_none_not_zero(entry, peak):
    """CONTRACT rule 2. A zero here claims the position never moved."""
    got = accounting.excursion_pct(
        Decimal(entry) if entry is not None else None,
        Decimal(peak) if peak is not None else None,
    )
    assert got is None


def test_a_peak_below_entry_is_not_a_positive_excursion():
    assert accounting.excursion_pct(Decimal("100"), Decimal("80")) == pytest.approx(-20.0)


# ------------------------------------------------------------------ it reaches the row


def test_the_stored_row_carries_the_real_excursion(tmp_db):
    """THE REGRESSION: a +146% trade was persisted as mfe_pct = 0.0."""
    accounting._save_position(tmp_db, position(entry="100", peak="246"))
    row = tmp_db.execute(
        "SELECT mfe_pct, mae_pct FROM positions WHERE position_id=?", ("pos_excursion",)
    ).fetchone()
    assert row[0] == pytest.approx(146.0, abs=0.2), (
        "the peak is tracked but the excursion is still not derived from it"
    )


def test_mae_stays_unknown_because_nothing_tracks_a_trough(tmp_db):
    """Honest absence. Inventing a MAE would be the same fault in the other direction."""
    accounting._save_position(tmp_db, position(entry="100", peak="246"))
    row = tmp_db.execute(
        "SELECT mae_pct FROM positions WHERE position_id=?", ("pos_excursion",)
    ).fetchone()
    assert row[0] is None, "mae_pct was given a value that nothing measures"


def test_a_position_with_no_peak_stores_null_not_zero(tmp_db):
    accounting._save_position(tmp_db, position(entry="100", peak=None, position_id="pos_nopeak"))
    row = tmp_db.execute(
        "SELECT mfe_pct FROM positions WHERE position_id=?", ("pos_nopeak",)).fetchone()
    assert row[0] is None


def test_a_freshly_opened_position_does_not_claim_a_measured_zero():
    """`open_position` seeded 0.0 for both. That is what put 236 zeros in the table."""
    import inspect

    source = inspect.getsource(accounting)
    assert "mae_pct=0.0" not in source, "a position is still born claiming a measured MAE"
    assert "mfe_pct=0.0" not in source, "a position is still born claiming a measured MFE"
