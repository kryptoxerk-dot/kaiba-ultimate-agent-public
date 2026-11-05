"""The public channel card must name the token, say it once, and never fail in silence.

MEASURED 2026-09-23 against the live box, rendering the three most recent live entries:

    🚀 0x8805f8...          <- every robinhood card, because `tokens.symbol` was NULL
    🟢 ROBINHOOD · ? · ...   <- and `tokens.launchpad` with it

while GMGN's ``token info`` -- the call the card ALREADY makes for price and liquidity --
carried ``symbol: "UU", name: "Unicorn", launchpad: "pons_v2"`` for that exact address.
The owner asked for "a token scan on each entry, like rick bot"; a scanner that cannot
say which token it scanned is not one.

Two more defects surfaced in the same render:

* ``0xed97b6`` filled at 02:38 and again at 02:42, and both fills would have posted the
  same card four minutes apart. A second fill of one token is our sizing, not new
  information, and this channel deliberately says nothing about sizing.
* the relay discarded :func:`send`'s return value. A bot demoted in the channel, a
  renamed channel or a rejected message all produced exactly the silence of a quiet
  hour, so the public feed could have been dead for a day with nothing saying so.
"""

from __future__ import annotations

import sqlite3

import pytest

from kaiba.core.schemas import Chain
from kaiba.ops import trade_notify as notify

TOKEN = "0x8805f82c5d2824912c66fa11914747ed19381319"
NOW = 3_000_000_000


@pytest.fixture
def gmgn(monkeypatch):
    """Stub the identity seam. ``calls`` proves whether the provider was asked at all."""
    calls: list[tuple[str, str]] = []

    def fake(chain, token):
        calls.append((chain.value, token))
        return {"symbol": "UU", "name": "Unicorn", "launchpad": "pons_v2"}

    monkeypatch.setattr(notify, "_token_identity", fake)
    return calls


def put_token(conn: sqlite3.Connection, **cols) -> None:
    keys = ["chain", "address", "first_seen_ms", *cols]
    conn.execute(
        "INSERT OR REPLACE INTO tokens (%s) VALUES (%s)"
        % (",".join(keys), ",".join("?" for _ in keys)),
        (Chain.ROBINHOOD.value, TOKEN, NOW, *cols.values()),
    )
    conn.commit()


# ------------------------------------------------------------------ the ticker


def test_a_missing_ticker_is_filled_in(tmp_db, gmgn):
    """THE REGRESSION: every robinhood card rendered as a truncated address."""
    put_token(tmp_db, symbol=None, name=None, launchpad=None)
    notify._ensure_token_identity(tmp_db, Chain.ROBINHOOD, TOKEN)
    row = tmp_db.execute(
        "SELECT symbol, name, launchpad FROM tokens WHERE address=?", (TOKEN,)
    ).fetchone()
    assert row["symbol"] == "UU"
    assert row["launchpad"] == "pons_v2"


def test_a_token_we_have_never_seen_is_inserted(tmp_db, gmgn):
    notify._ensure_token_identity(tmp_db, Chain.ROBINHOOD, TOKEN)
    row = tmp_db.execute("SELECT symbol FROM tokens WHERE address=?", (TOKEN,)).fetchone()
    assert row is not None and row["symbol"] == "UU"


def test_a_ticker_we_already_have_is_never_overwritten(tmp_db, monkeypatch):
    """``0x3122b3`` had AXON stored and NOTHING at GMGN. A blind write would delete it.

    The two sources are complementary, not ranked, so the only safe move is to fill holes.
    """
    monkeypatch.setattr(notify, "_token_identity", lambda chain, token: {})
    put_token(tmp_db, symbol="AXON", name="axon", launchpad="pons_v2")
    notify._ensure_token_identity(tmp_db, Chain.ROBINHOOD, TOKEN)
    row = tmp_db.execute("SELECT symbol FROM tokens WHERE address=?", (TOKEN,)).fetchone()
    assert row["symbol"] == "AXON"


def test_a_known_ticker_costs_no_provider_call(tmp_db, gmgn):
    """The cheap half of the rule: a hole is filled once, not re-asked on every fill."""
    put_token(tmp_db, symbol="UU", name="Unicorn", launchpad="pons_v2")
    notify._ensure_token_identity(tmp_db, Chain.ROBINHOOD, TOKEN)
    assert gmgn == [], "GMGN was asked about a token whose ticker we already had"


@pytest.mark.parametrize("reply", [{}, {"symbol": ""}, None])
def test_a_provider_that_cannot_say_leaves_the_row_alone(tmp_db, monkeypatch, reply):
    monkeypatch.setattr(notify, "_token_identity", lambda chain, token: reply or {})
    put_token(tmp_db, symbol=None, name=None, launchpad=None)
    notify._ensure_token_identity(tmp_db, Chain.ROBINHOOD, TOKEN)
    row = tmp_db.execute("SELECT symbol FROM tokens WHERE address=?", (TOKEN,)).fetchone()
    assert not (row["symbol"] or "")


def test_a_raising_provider_never_breaks_the_card(tmp_db, monkeypatch):
    """Enrichment is cosmetic. A card with an address beats no card at all."""
    def boom(chain, token):
        raise RuntimeError("cli died")

    monkeypatch.setattr(notify, "_token_identity", boom)
    notify._ensure_token_identity(tmp_db, Chain.ROBINHOOD, TOKEN)  # must not raise


def test_the_real_seam_survives_a_dead_provider(monkeypatch):
    """`_token_identity` is the thing every test above stubs, so it needs its own net."""
    def boom(*a, **k):
        raise RuntimeError("no cli")

    monkeypatch.setattr("kaiba.providers.gmgn_cli.token_info", boom)
    assert notify._token_identity(Chain.ROBINHOOD, TOKEN) == {}


# ------------------------------------------------------------------ said once


def test_a_second_fill_of_one_token_does_not_repeat_the_card(tmp_db):
    """THE REGRESSION: 0xed97b6 filled at 02:38 and 02:42 and would have posted twice."""
    notify._ensure_cursor(tmp_db)
    assert not notify._channel_recently_posted(tmp_db, Chain.ROBINHOOD, TOKEN, NOW)
    notify._mark_channel(tmp_db, Chain.ROBINHOOD, TOKEN, NOW)
    later = NOW + notify.CHANNEL_REPEAT_WINDOW_MS - 1
    assert notify._channel_recently_posted(tmp_db, Chain.ROBINHOOD, TOKEN, later)


def test_the_same_token_may_be_called_again_later(tmp_db):
    """A fresh move hours later is news again; this suppresses a burst, not a token."""
    notify._ensure_cursor(tmp_db)
    notify._mark_channel(tmp_db, Chain.ROBINHOOD, TOKEN, NOW)
    later = NOW + notify.CHANNEL_REPEAT_WINDOW_MS + 1
    assert not notify._channel_recently_posted(tmp_db, Chain.ROBINHOOD, TOKEN, later)


def test_the_window_is_per_token_not_global(tmp_db):
    """One token's turn must never mute a different token's card."""
    notify._ensure_cursor(tmp_db)
    notify._mark_channel(tmp_db, Chain.ROBINHOOD, TOKEN, NOW)
    other = "0x" + "cd" * 20
    assert not notify._channel_recently_posted(tmp_db, Chain.ROBINHOOD, other, NOW)


def test_the_window_is_per_chain(tmp_db):
    notify._ensure_cursor(tmp_db)
    notify._mark_channel(tmp_db, Chain.ROBINHOOD, TOKEN, NOW)
    assert not notify._channel_recently_posted(tmp_db, Chain.BSC, TOKEN, NOW)


def test_an_unreadable_record_posts_rather_than_stays_silent(tmp_db, monkeypatch):
    """Failing toward a duplicate is right here: this channel's job is to say things."""
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(notify, "fetch_one", boom)
    assert not notify._channel_recently_posted(tmp_db, Chain.ROBINHOOD, TOKEN, NOW)


# ------------------------------------------------------------------ never silent


def test_the_relay_reads_the_send_result():
    """THE REGRESSION: the return value was discarded, so a dead channel looked quiet."""
    import inspect

    source = inspect.getsource(notify.once)
    assert "posted = send(" in source, "the channel send result is discarded again"
    assert "channel_failed" in source, "a refused card is not counted anywhere"


def test_a_refused_card_is_counted_and_warned(tmp_db, monkeypatch, caplog):
    """The whole point: a channel that stops working must be visible somewhere."""
    import logging

    notify._ensure_cursor(tmp_db)
    monkeypatch.setattr(notify, "credentials", lambda: ("tok", "dm"))
    monkeypatch.setattr(notify, "channel_id", lambda: "-100123")
    monkeypatch.setattr(notify, "render_for_channel", lambda row, conn: "card")
    monkeypatch.setattr(notify, "render", lambda row, conn: "dm text")

    sent: list[str] = []

    def fake_send(text, *, token, chat_id):
        sent.append(str(chat_id))
        return str(chat_id) != "-100123"  # the DM works, the channel refuses

    monkeypatch.setattr(notify, "send", fake_send)

    from tests.test_accounting import make_order, persist

    order = make_order(order_id="relay_fail", filled_out=123)
    persist(tmp_db, order)
    tmp_db.execute("UPDATE orders SET updated_ms=? WHERE order_id=?", (NOW, order.order_id))
    tmp_db.execute("INSERT OR REPLACE INTO notify_cursor VALUES('trades',?,?)", (NOW - 1, NOW))
    tmp_db.commit()

    with caplog.at_level(logging.WARNING):
        out = notify.once(tmp_db)

    assert out.get("channel_failed") == 1, out
    assert "-100123" in sent, "the channel was never attempted"
    assert any("REFUSED" in r.message for r in caplog.records), "the failure was silent"


def test_a_channel_failure_does_not_disturb_the_private_record(tmp_db, monkeypatch):
    """The operator's own copy is the record; the public feed is a relay off the side."""
    notify._ensure_cursor(tmp_db)
    monkeypatch.setattr(notify, "credentials", lambda: ("tok", "dm"))
    monkeypatch.setattr(notify, "channel_id", lambda: "-100123")
    monkeypatch.setattr(notify, "render_for_channel", lambda row, conn: "card")
    monkeypatch.setattr(notify, "render", lambda row, conn: "dm text")
    monkeypatch.setattr(
        notify, "send",
        lambda text, *, token, chat_id: str(chat_id) != "-100123",
    )

    from tests.test_accounting import make_order, persist

    order = make_order(order_id="relay_fail2", filled_out=123)
    persist(tmp_db, order)
    tmp_db.execute("UPDATE orders SET updated_ms=? WHERE order_id=?", (NOW, order.order_id))
    tmp_db.execute("INSERT OR REPLACE INTO notify_cursor VALUES('trades',?,?)", (NOW - 1, NOW))
    tmp_db.commit()

    out = notify.once(tmp_db)
    assert out["sent"] == 1, out
    row = tmp_db.execute(
        "SELECT delivered FROM notify_sent WHERE order_id=?", (order.order_id,)
    ).fetchone()
    assert row["delivered"] == 1, "a public-relay failure marked the private feed undelivered"


# ------------------------------------------------- the CARD, not just the helpers
#
# Both tests below exist because a mutation survived. Deleting the backfill CALL from
# `render_scan_card`, and disabling the dedupe BRANCH in `once`, each left the suite
# green: every test above drove the helpers directly and none drove the path that uses
# them. A helper that works and is never called is the same defect as one that does not.


def _sol_entry(conn, order_id="card_1"):
    from tests.test_accounting import make_order, persist

    order = make_order(order_id=order_id, filled_out=123)
    persist(conn, order)
    conn.execute("UPDATE orders SET updated_ms=? WHERE order_id=?", (NOW, order_id))
    conn.commit()
    return conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()


def test_the_card_itself_names_the_token(tmp_db, monkeypatch):
    """THE SURVIVOR: the backfill worked and `render_scan_card` never called it."""
    row = _sol_entry(tmp_db)
    monkeypatch.setattr(
        notify, "_token_identity",
        lambda chain, token: {"symbol": "UU", "name": "Unicorn", "launchpad": "pons_v2"},
    )
    card = notify.render_scan_card(row, tmp_db)
    assert "$UU" in card, card[:200]


def test_the_card_falls_back_to_the_address_when_nothing_knows(tmp_db, monkeypatch):
    """The other direction: no ticker anywhere must still produce a card."""
    row = _sol_entry(tmp_db, order_id="card_2")
    monkeypatch.setattr(notify, "_token_identity", lambda chain, token: {})
    card = notify.render_scan_card(row, tmp_db)
    assert card and str(row["token"])[:8] in card


def test_once_posts_one_card_for_two_fills_of_a_token(tmp_db, monkeypatch):
    """THE SURVIVOR: the window was computed correctly and `once` ignored it."""
    from tests.test_accounting import make_order, persist

    notify._ensure_cursor(tmp_db)
    monkeypatch.setattr(notify, "credentials", lambda: ("tok", "dm"))
    monkeypatch.setattr(notify, "channel_id", lambda: "-100123")
    monkeypatch.setattr(notify, "render", lambda row, conn: "dm text")
    monkeypatch.setattr(notify, "render_for_channel", lambda row, conn: "card")

    to_channel: list[str] = []

    def fake_send(text, *, token, chat_id):
        if str(chat_id) == "-100123":
            to_channel.append(text)
        return True

    monkeypatch.setattr(notify, "send", fake_send)

    for i in (1, 2):  # the same token filling twice, four minutes apart on the live box
        order = make_order(order_id=f"dupe_{i}", filled_out=123)
        persist(tmp_db, order)
        tmp_db.execute(
            "UPDATE orders SET updated_ms=? WHERE order_id=?", (NOW + i, order.order_id)
        )
    tmp_db.execute("INSERT OR REPLACE INTO notify_cursor VALUES('trades',?,?)", (NOW - 1, NOW))
    tmp_db.commit()

    out = notify.once(tmp_db)
    assert out["sent"] == 2, "both fills must still reach the private feed"
    assert len(to_channel) == 1, f"the public channel got {len(to_channel)} cards for one token"
    assert out.get("channel_skipped") == 1, out
