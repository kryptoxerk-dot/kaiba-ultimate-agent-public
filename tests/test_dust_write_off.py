"""An unsellable dust remainder is written off; a real position never is.

MEASURED 2026-09-23 on the live box. ``pos_ca35d86b03667b5e7a08fd24``:

    bought   11,778,922,406 units for 77,777,786 lamports
    sold     11,661,133,181 units for 50,423,532 lamports   (a 99% exit)
    left        117,789,225 units  = EXACTLY 1.0% of the entry

The wallet reported none of that 1%. Six attempts to sell it were refused by the venue with
HTTP 400. The watchdog then retried once a minute for hours -- 29 failures in 30 minutes --
and every one of those attempts was work inside a protection tick that was already over its
5 s budget, which kept ``protection_overrun`` armed and HALTED ENTRIES ON EVERY CHAIN.

One dust line stopped all trading. The position had been economically finished for hours.

The dangerous version of this fix closes any position whose balance reads zero. That would
book a total loss on a token that was merely moved, or on a balance read that lied. So the
fraction guard is the property that matters most here, and it is tested hardest.
"""

from __future__ import annotations

import pytest

from kaiba.core.schemas import Chain, Lane, LaneMode
from kaiba.execution import accounting, watchdog as wd
from tests.test_watchdog import make_position

TOKEN = "runupS9JpTdB169taDKykxa2y8fqxghqeJYCqr9cAk3"


def position_with(conn, *, qty: int, qty_total: int, cost: int = 77_777_786,
                  proceeds: int = 50_423_532):
    make_position(conn, token=TOKEN, chain=Chain.SOL, mode=LaneMode.LIVE,
                  lane=Lane.SM_TRENCHES, qty=qty)
    conn.execute(
        "UPDATE positions SET qty_total=?, cost_native=?, proceeds_native=? WHERE token=?",
        (qty_total, cost, proceeds, TOKEN),
    )
    # The submitter refuses on unknown decimals BEFORE it ever reads a balance, so a
    # registry row is what lets these tests reach the wallet check at all.
    conn.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, decimals, first_seen_ms, meta_json) "
        "VALUES (?,?,?,?,'{}')",
        (Chain.SOL.value, TOKEN, 6, 1),
    )
    conn.commit()
    return wd.open_positions(conn)[0]


# ------------------------------------------------------------------ the write-off


def test_a_dust_remainder_is_written_off(tmp_db):
    """THE REGRESSION: retried once a minute for hours and halted every chain."""
    position = position_with(tmp_db, qty=117_789_225, qty_total=11_778_922_406)
    closed = accounting.write_off_dust(position.position_id, tmp_db)
    assert closed is not None and closed.closed_ms is not None
    assert closed.qty == 0
    assert closed.exit_reason == "dust_written_off"


def test_the_write_off_invents_no_sell_and_no_proceeds(tmp_db):
    """Proceeds stay exactly what the fills really returned."""
    position = position_with(tmp_db, qty=117_789_225, qty_total=11_778_922_406)
    closed = accounting.write_off_dust(position.position_id, tmp_db)
    assert closed.proceeds_native == 50_423_532
    orders = tmp_db.execute(
        "SELECT COUNT(*) AS n FROM orders WHERE token=?", (TOKEN,)
    ).fetchone()["n"]
    assert orders == 0, "a sell was fabricated to close the position"


def test_the_full_cost_basis_is_booked_when_the_dust_is_worthless(tmp_db):
    """-0.026576 SOL becomes -0.027354: the dust is booked, not left pending forever."""
    position = position_with(tmp_db, qty=117_789_225, qty_total=11_778_922_406)
    closed = accounting.write_off_dust(position.position_id, tmp_db)
    assert closed.realized_native == 50_423_532 - 77_777_786


def test_writing_off_twice_is_harmless(tmp_db):
    position = position_with(tmp_db, qty=117_789_225, qty_total=11_778_922_406)
    first = accounting.write_off_dust(position.position_id, tmp_db)
    again = accounting.write_off_dust(position.position_id, tmp_db)
    assert again.closed_ms == first.closed_ms


# ------------------------------------------------------------------ the guard that matters


@pytest.mark.parametrize("qty,total", [
    (5_000_000_000, 11_778_922_406),   # 42% left -- not dust
    (11_778_922_406, 11_778_922_406),  # nothing sold at all
    (300_000_000, 11_778_922_406),     # 2.5%, just over the line
])
def test_a_remainder_too_large_to_be_dust_is_never_written_off(tmp_db, qty, total):
    """THE GUARD: a whole position reading zero means something is WRONG, not finished.

    Closing it would book a total loss on evidence that cannot support one -- the tokens
    may have been moved, or the balance read may simply be lying.
    """
    position = position_with(tmp_db, qty=qty, qty_total=total)
    state = wd.load_state(tmp_db, position)
    state.exit_attempts = 99
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    assert dog._write_off_dust(position, state) is False
    assert wd.open_positions(tmp_db), "a non-dust position was closed"


def test_the_first_zero_read_never_writes_anything_off(tmp_db):
    """One read is not a fact about the chain; `exit_attempts` is persisted for this."""
    position = position_with(tmp_db, qty=117_789_225, qty_total=11_778_922_406)
    state = wd.load_state(tmp_db, position)
    state.exit_attempts = 1
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    assert dog._write_off_dust(position, state) is False
    assert wd.open_positions(tmp_db)


def test_after_enough_attempts_the_dust_does_close(tmp_db):
    position = position_with(tmp_db, qty=117_789_225, qty_total=11_778_922_406)
    state = wd.load_state(tmp_db, position)
    state.exit_attempts = wd.DUST_MIN_ATTEMPTS
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    assert dog._write_off_dust(position, state) is True
    assert wd.open_positions(tmp_db) == []


# ------------------------------------------------------------------ the seam


def test_an_empty_wallet_is_flagged_structurally_not_by_prose():
    """The handler must not have to match a sentence to know what happened."""
    outcome = wd.ExitOutcome(False, None, None, "wallet holds none of X", wallet_empty=True)
    assert outcome.wallet_empty is True
    assert wd.ExitOutcome(False, None, None, "anything else").wallet_empty is False


def test_a_balance_we_could_not_read_is_not_an_empty_wallet(tmp_db, monkeypatch):
    """"We could not look" and "there is nothing there" must never be the same answer."""
    monkeypatch.setattr(wd, "exit_wallet_for", lambda chain: "W")
    monkeypatch.setattr(wd, "wallet_token_units", lambda *a, **k: None)
    position = position_with(tmp_db, qty=117_789_225, qty_total=11_778_922_406)
    sender = wd.DefaultExitSubmitter(tmp_db, wd.NullPriceSource())
    quote = wd.PriceQuote(price_usd=None)
    outcome = sender.submit_exit(position, __import__("decimal").Decimal(100),
                                 quote=quote, reason="stop_loss")
    assert outcome.wallet_empty is False, "an unreadable balance was read as an empty one"


def test_a_zero_balance_read_really_does_set_the_flag(tmp_db, monkeypatch):
    """THE SURVIVOR: the flag was only ever constructed by hand, never driven.

    Setting ``wallet_empty=False`` in the submitter left the suite green, so nothing
    connected the real refusal to the write-off that depends on it -- the dust would have
    gone back to retrying once a minute forever with every test still passing.
    """
    from decimal import Decimal

    monkeypatch.setattr(wd, "exit_wallet_for", lambda chain: "W")
    monkeypatch.setattr(wd, "wallet_token_units", lambda *a, **k: 0)
    position = position_with(tmp_db, qty=117_789_225, qty_total=11_778_922_406)
    sender = wd.DefaultExitSubmitter(tmp_db, wd.NullPriceSource())
    outcome = sender.submit_exit(
        position, Decimal(100), quote=wd.PriceQuote(price_usd=None), reason="stop_loss"
    )
    assert outcome.ok is False
    assert outcome.wallet_empty is True, "an empty wallet no longer flags itself"


def test_a_partial_balance_clamps_instead_of_flagging_empty(tmp_db, monkeypatch):
    """The neighbouring case: some tokens left is a smaller sell, not a write-off."""
    from decimal import Decimal

    monkeypatch.setattr(wd, "exit_wallet_for", lambda chain: "W")
    monkeypatch.setattr(wd, "wallet_token_units", lambda *a, **k: 50_000)
    position = position_with(tmp_db, qty=117_789_225, qty_total=11_778_922_406)
    sender = wd.DefaultExitSubmitter(tmp_db, wd.NullPriceSource())
    outcome = sender.submit_exit(
        position, Decimal(100), quote=wd.PriceQuote(price_usd=None), reason="stop_loss"
    )
    assert outcome.wallet_empty is False
