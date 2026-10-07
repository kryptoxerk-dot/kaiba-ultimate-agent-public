"""Every live open and close reaches the operator exactly once.

The operator asked three times for this: "i want the agent to notify each time they open
trade / which token they trade how much they buy and / their reasoning". So the content is
pinned here as much as the delivery -- a notification that says a trade happened but not
what or why is not what was asked for.

The delivery rules that matter, and why each one is a rule rather than a detail:

* **Exactly once.** A duplicate costs trust in the feed; a miss costs the whole point.
* **A dead chat never blocks a trade.** This runs as a poller over the order log precisely
  so a Telegram outage cannot raise inside a fill or stall a protection tick -- on this box
  a slow tick halts entries on every chain.
* **Shadow trades are not announced.** They are paper. Announcing them would make the feed
  a lie about what the money did.
* **A cold start does not dump history.** A first run after an outage announces the recent
  past only.
* **An undeliverable message is dropped, not retried forever**, or it wedges every later
  trade behind it.
"""

from __future__ import annotations

import json
import time

import pytest

from kaiba.core.schemas import Chain
from kaiba.ops import trade_notify as N

#: Recent, because the notifier deliberately ignores anything older than
#: COLD_START_LOOKBACK_MS on a first run. Tests that care about that use explicit times.
NOW = int(time.time() * 1000)
T0 = NOW - 120_000
TOKEN = "8JVtwRnDiDmjV2pSRTmqzPtXZyNdgUkpijrufgYJRARA"


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    """Pin the notifier's clock to NOW.

    NOW is taken at import (collection) time. In a full-suite run the tests here execute
    ~15+ minutes later, by which time every fixture row was older than
    COLD_START_LOOKBACK_MS and the notifier correctly ignored it: 24 tests failed in the
    suite and passed alone. ``time.time`` is the only clock trade_notify reads.
    """
    from types import SimpleNamespace

    monkeypatch.setattr(N, "time", SimpleNamespace(time=lambda: NOW / 1000))


def put_token(conn, symbol: str = "KAPI", chain: Chain = Chain.SOL, address: str = TOKEN) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, symbol, name, decimals, first_seen_ms) "
        "VALUES (?,?,?,?,?,?)",
        (chain.value, address, symbol, symbol, 9, T0),
    )
    conn.commit()


def put_order(
    conn,
    *,
    order_id: str = "ord:1",
    side: str = "buy",
    state: str = "filled",
    mode: str = "live",
    chain: Chain = Chain.SOL,
    amount_in: str = "49865460",
    decision_id: str | None = "dec:1",
    tx_hash: str | None = "5xTxHash",
    updated_ms: int | None = None,
    token: str = TOKEN,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO orders (order_id, decision_id, chain, token, side, lane, mode, "
        "input_token, output_token, amount_in, min_out, slippage_bps, state, provider, "
        "tx_hash, created_ms, updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (order_id, decision_id, chain.value, token, side, "sm-trenches", mode,
         "So11111111111111111111111111111111111111111", token, amount_in, "1", 2500,
         state, "gmgn", tx_hash, T0, updated_ms if updated_ms is not None else NOW - 60_000),
    )
    conn.commit()


def put_decision(conn, thesis: str, grade: str = "B", decision_id: str = "dec:1") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO decisions (decision_id, ts_ms, lane, mode, chain, token, "
        "action, thesis, dossier_grade) VALUES (?,?,?,?,?,?,?,?,?)",
        (decision_id, T0, "sm-trenches", "live", Chain.SOL.value, TOKEN, "enter", thesis, grade),
    )
    conn.commit()


@pytest.fixture
def sent(monkeypatch):
    box: list[str] = []
    monkeypatch.setattr(N, "send", lambda text, **kw: (box.append(text), True)[1])
    monkeypatch.setattr(N, "credentials", lambda: ("tok", "chat"))
    return box


# ------------------------------------------------------------------ what the message says


def test_an_open_names_the_token_the_size_and_the_reason(tmp_db, sent):
    put_token(tmp_db)
    put_decision(tmp_db, "3 smart wallets in the trenches preset (min 3); 3 independent entities")
    put_order(tmp_db)
    N.once(tmp_db)
    assert len(sent) == 1
    text = sent[0]
    assert "$KAPI" in text
    assert TOKEN in text, "the operator must be able to copy the mint"
    assert "0.04986546" in text or "0.049865" in text, text   # 49865460 lamports
    assert "SOL" in text
    assert "3 smart wallets in the trenches preset" in text, "the recorded thesis, verbatim"
    assert "sm-trenches" in text


def test_the_thesis_is_never_reconstructed_when_absent(tmp_db, sent):
    """No decision row means no 'why' line, not an invented one."""
    put_token(tmp_db)
    put_order(tmp_db, decision_id=None)
    N.once(tmp_db)
    assert "why" not in sent[0]


def test_a_close_reports_the_realised_result(tmp_db, sent):
    put_token(tmp_db)
    tmp_db.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, "
        "qty, qty_total, cost_native, proceeds_native, realized_native, entry_price_usd, "
        "peak_price_usd, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("pos:1", Chain.SOL.value, TOKEN, "sm-trenches", "live", T0, T0 + 600_000,
         "0", "100", "11913816749897435", "45197084098095487", "33283267348198052",
         "0.0000200682", "0.0001756842", "trailing_stop"),
    )
    tmp_db.commit()
    put_order(tmp_db, side="sell", order_id="ord:s", decision_id=None)
    N.once(tmp_db)
    text = sent[0]
    assert "SELL" in text
    assert "trailing_stop" in text
    assert "8.75x" in text, f"peak multiple missing: {text}"
    assert "10 min" in text
    assert "+" in text and "%" in text, "the realised percentage must be there"


def test_a_close_without_a_position_still_announces(tmp_db, sent):
    """Never stay silent about a real sell just because the ledger is behind."""
    put_token(tmp_db)
    put_order(tmp_db, side="sell", order_id="ord:s", decision_id=None)
    N.once(tmp_db)
    assert len(sent) == 1 and "SELL" in sent[0] and TOKEN in sent[0]


def test_an_unknown_symbol_falls_back_to_the_address(tmp_db, sent):
    put_order(tmp_db)
    N.once(tmp_db)
    assert TOKEN[:10] in sent[0]


# ------------------------------------------------------------------ delivery rules


def test_each_trade_is_announced_exactly_once(tmp_db, sent):
    put_token(tmp_db)
    put_order(tmp_db)
    N.once(tmp_db)
    N.once(tmp_db)
    N.once(tmp_db)
    assert len(sent) == 1


def test_shadow_trades_are_not_announced(tmp_db, sent):
    put_token(tmp_db)
    put_order(tmp_db, mode="shadow")
    N.once(tmp_db)
    assert sent == []


@pytest.mark.parametrize("state", ["failed", "unknown", "submitted", "reserved"])
def test_only_filled_orders_are_announced(tmp_db, sent, state):
    put_token(tmp_db)
    put_order(tmp_db, state=state)
    N.once(tmp_db)
    assert sent == []


def test_a_cold_start_does_not_dump_history(tmp_db, sent):
    """A first run after an outage must not paste hours of trades into the chat."""
    import time as _t

    now = int(_t.time() * 1000)
    put_token(tmp_db)
    put_order(tmp_db, order_id="ord:old", updated_ms=now - 6 * 3600_000)
    put_order(tmp_db, order_id="ord:new", updated_ms=now - 30_000)
    N.once(tmp_db)
    assert len(sent) == 1
    assert "ord:old" not in str(sent)


def test_an_undeliverable_message_is_dropped_not_retried_forever(tmp_db, monkeypatch):
    attempts: list[str] = []
    monkeypatch.setattr(N, "send", lambda text, **kw: (attempts.append(text), False)[1])
    monkeypatch.setattr(N, "credentials", lambda: ("tok", "chat"))
    put_token(tmp_db)
    put_order(tmp_db)
    for _ in range(N.MAX_ATTEMPTS + 3):
        N.once(tmp_db)
    assert len(attempts) == N.MAX_ATTEMPTS, len(attempts)


def test_a_failure_does_not_skip_the_next_trade(tmp_db, monkeypatch):
    """Order matters: a stuck message must not let a later one jump ahead and advance
    the cursor past it."""
    outcomes = iter([False, True, True])
    seen: list[str] = []

    def flaky(text, **kw):
        seen.append(text)
        return next(outcomes, True)

    monkeypatch.setattr(N, "send", flaky)
    monkeypatch.setattr(N, "credentials", lambda: ("tok", "chat"))
    put_token(tmp_db)
    put_order(tmp_db, order_id="ord:a", updated_ms=NOW - 60_000)
    put_order(tmp_db, order_id="ord:b", updated_ms=NOW - 50_000)
    N.once(tmp_db)
    assert len(seen) == 1, "a failed send must stop the pass, not skip ahead"
    N.once(tmp_db)
    assert len(seen) == 3, "both trades are delivered on the next pass"


def test_no_credentials_never_raises_and_never_marks_delivered(tmp_db, monkeypatch):
    monkeypatch.setattr(N, "credentials", lambda: (None, None))
    put_token(tmp_db)
    put_order(tmp_db)
    got = N.once(tmp_db)
    assert got["sent"] == 0 and got["failed"] == 1
    monkeypatch.setattr(N, "credentials", lambda: ("tok", "chat"))
    box: list[str] = []
    monkeypatch.setattr(N, "send", lambda text, **kw: (box.append(text), True)[1])
    N.once(tmp_db)
    assert len(box) == 1, "the trade is announced once the bot is configured"


def test_a_render_failure_does_not_stop_the_pass(tmp_db, monkeypatch):
    box: list[str] = []
    calls = {"n": 0}

    def sometimes(row, conn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("bad row")
        return "ok"

    monkeypatch.setattr(N, "render", sometimes)
    monkeypatch.setattr(N, "send", lambda text, **kw: (box.append(text), True)[1])
    monkeypatch.setattr(N, "credentials", lambda: ("tok", "chat"))
    put_token(tmp_db)
    put_order(tmp_db, order_id="ord:a", updated_ms=NOW - 60_000)
    put_order(tmp_db, order_id="ord:b", updated_ms=NOW - 50_000)
    N.once(tmp_db)
    assert box == ["ok"]


def test_send_never_raises_on_a_dead_network(monkeypatch):
    def boom(*a, **k):
        raise OSError("no route to host")

    monkeypatch.setattr(N.urllib.request, "urlopen", boom)
    assert N.send("hi", token="t", chat_id="c") is False


def test_the_token_is_never_placed_in_a_url_query(monkeypatch):
    """The bot token goes in the PATH; anything in a query string leaks to logs."""
    captured: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok":true}'

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = request.data
        return _Resp()

    monkeypatch.setattr(N.urllib.request, "urlopen", fake_urlopen)
    assert N.send("hi", token="SECRET", chat_id="123") is True
    assert "?" not in captured["url"], captured["url"]
    assert b"SECRET" not in (captured["body"] or b"")


# ------------------------------------------------------------------ alerts and digests


def journal(conn, kind: str, body: str, ts_ms: int | None = None) -> None:
    """Through the real API: the journal is a hash-chained log, not a plain table."""
    from kaiba.core import journal as J

    J.append(kind, body, conn=conn)
    conn.commit()


def test_a_decision_is_delivered_as_its_own_alert(tmp_db, sent):
    journal(tmp_db, "change", "entries paused: negative live expectancy")
    N.once(tmp_db)
    assert any("CHANGE" in t and "entries paused" in t for t in sent), sent


@pytest.mark.parametrize("kind", ["change", "lesson", "correction"])
def test_every_alert_kind_is_delivered(tmp_db, sent, kind):
    journal(tmp_db, kind, f"a {kind} worth knowing")
    N.once(tmp_db)
    assert any(kind.upper() in t for t in sent), sent


def test_an_alert_is_delivered_once(tmp_db, sent):
    journal(tmp_db, "change", "lane armed")
    N.once(tmp_db)
    N.once(tmp_db)
    N.once(tmp_db)
    assert sum(1 for t in sent if "lane armed" in t) == 1


def test_observations_are_digested_not_sent_one_by_one(tmp_db, sent):
    """340 observations a day, one message each, would bury the trades."""
    for i in range(40):
        journal(tmp_db, "observation", f"radar sweep {i}")
    N.once(tmp_db)                      # first pass only arms the digest clock
    assert not any("radar sweep" in t for t in sent), "no per-observation messages"
    N.once(tmp_db)
    # force the digest due
    tmp_db.execute("UPDATE notify_cursor SET last_ms=? WHERE name='digest_reports'",
                   (NOW - (N.DIGEST_INTERVAL_S + 60) * 1000,))
    tmp_db.commit()
    N.once(tmp_db)
    digests = [t for t in sent if t.startswith("REPORTS")]
    assert len(digests) == 1, sent
    assert "40 entries" in digests[0], digests[0]


def test_wallets_are_counted_not_listed(tmp_db):
    """92,268 wallet rows in 24h. A digest counts them; it must never list them."""
    for i in range(500):
        tmp_db.execute(
            "INSERT OR REPLACE INTO wallets (chain, address, first_seen_ms, last_seen_ms) "
            "VALUES (?,?,?,?)",
            ("sol", f"wallet{i}", NOW - 10_000, NOW - 10_000),
        )
    tmp_db.commit()
    text = N.intelligence_digest(tmp_db, since_ms=NOW - 3_600_000)
    assert "500" in text
    assert "wallet100" not in text, "the digest listed individual wallets"
    assert len(text) < 2000, "a digest must stay readable"


def test_the_digest_names_the_graded_wallets(tmp_db):
    """182 graded wallets carry a judgement; those are worth naming."""
    tmp_db.execute(
        "INSERT OR REPLACE INTO wallet_scores (chain, address, score, grade, "
        "evidence_weight, archetype, win_rate, scored_at_ms, model_version) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("sol", "62EbtFbmJATT94Y6Uguf6a1kdaJuZWAu8ycqbg3xKfQg", 91.0, "A", 1.0,
         "sniper", 0.62, NOW, "v1"),
    )
    tmp_db.commit()
    text = N.intelligence_digest(tmp_db, since_ms=NOW - 3_600_000)
    assert "A " in text and "sniper" in text and "62%" in text, text


def test_a_failing_digest_never_stops_the_trade_feed(tmp_db, sent, monkeypatch):
    monkeypatch.setattr(
        N, "intelligence_digest",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    put_token(tmp_db)
    put_order(tmp_db)
    N.once(tmp_db)
    assert any("BUY" in t for t in sent), "the trade must still go out"


# ------------------------------------------------------------------ found by the operator


def test_two_fills_in_the_same_millisecond_are_both_announced(tmp_db, sent):
    """FOUND 2026-09-22 by the operator agent reviewing this module.

    The cursor advanced to the highest delivered ``updated_ms`` and the query used a
    strict ``>``, so a second fill carrying that same millisecond was excluded for good.
    Latent rather than live -- 0 shared timestamps in a measured 24h -- but a burst of
    simultaneous fills is exactly when the operator most needs the feed.
    """
    put_token(tmp_db)
    stamp = NOW - 60_000
    # The bug needs the cursor to ADVANCE to `stamp` first, so the second fill must arrive
    # after a pass that already delivered the first. Inserting both up front puts them in
    # one batch, where a strict `>` never bites -- that version of this test passed against
    # the bug it was written for.
    put_order(tmp_db, order_id="ord:same_a", updated_ms=stamp)
    N.once(tmp_db)
    assert len(sent) == 1, sent
    cursor = tmp_db.execute(
        "SELECT last_ms FROM notify_cursor WHERE name='trades'").fetchone()["last_ms"]
    assert int(cursor) == stamp, "the cursor must sit exactly on the delivered timestamp"

    put_order(tmp_db, order_id="ord:same_b", updated_ms=stamp)
    N.once(tmp_db)
    assert len(sent) == 2, f"the second fill in the same millisecond was skipped: {sent}"


def test_a_paper_fill_is_never_announced_as_real(tmp_db, sent):
    """Defence in depth: paper fills carry mode='shadow' today, but the provider is the
    fact that makes them fiction, and a future path could forget the mode."""
    put_token(tmp_db)
    tmp_db.execute(
        "INSERT OR REPLACE INTO orders (order_id, chain, token, side, lane, mode, "
        "input_token, output_token, amount_in, min_out, slippage_bps, state, provider, "
        "created_ms, updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ord:paper", Chain.SOL.value, TOKEN, "buy", "sm-trenches", "live",
         "So11111111111111111111111111111111111111111", TOKEN, "1", "1", 2500,
         "filled", "paper", T0, NOW - 60_000),
    )
    tmp_db.commit()
    N.once(tmp_db)
    assert sent == [], sent


def test_a_real_fill_beside_a_paper_one_still_goes_out(tmp_db, sent):
    """The paper filter must not swallow the live trade next to it."""
    put_token(tmp_db)
    stamp = NOW - 60_000
    tmp_db.execute(
        "INSERT OR REPLACE INTO orders (order_id, chain, token, side, lane, mode, "
        "input_token, output_token, amount_in, min_out, slippage_bps, state, provider, "
        "created_ms, updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ord:paper", Chain.SOL.value, TOKEN, "buy", "sm-trenches", "live",
         "So11111111111111111111111111111111111111111", TOKEN, "1", "1", 2500,
         "filled", "paper", T0, stamp),
    )
    tmp_db.commit()
    put_order(tmp_db, order_id="ord:real", updated_ms=stamp)
    N.once(tmp_db)
    assert len(sent) == 1 and "BUY" in sent[0]


# ------------------------------------------------------------------ the public channel


@pytest.fixture
def channel(monkeypatch):
    """Capture (chat_id, text) so the two destinations can be told apart."""
    box: list[tuple[str, str]] = []
    monkeypatch.setattr(N, "credentials", lambda: ("tok", "private"))
    monkeypatch.setattr(N, "channel_id", lambda: "-1001701614508")
    monkeypatch.setattr(N, "send", lambda text, **kw: (box.append((kw.get("chat_id"), text)), True)[1])
    return box


def test_a_buy_reaches_both_the_private_chat_and_the_channel(tmp_db, channel):
    put_token(tmp_db)
    put_decision(tmp_db, "4 smart wallets in the trenches preset (min 3)")
    put_order(tmp_db)
    N.once(tmp_db)
    chats = [c for c, _ in channel]
    assert "private" in chats and "-1001701614508" in chats, chats


def test_the_channel_never_carries_a_transaction_hash(tmp_db, channel):
    """The owner asked for the reasoning, not the receipt. A public tx points at the
    wallet that placed it."""
    put_token(tmp_db)
    put_order(tmp_db, tx_hash="5xREALTXHASH")
    N.once(tmp_db)
    public = [t for c, t in channel if c == "-1001701614508"]
    assert public and all("5xREALTXHASH" not in t for t in public), public
    private = [t for c, t in channel if c == "private"]
    assert any("5xREALTXHASH" in t for t in private), "the private feed keeps the tx"


def test_a_close_is_never_posted_to_the_channel(tmp_db, channel):
    """The owner's instruction: this channel is signal for readers, not a disclosure of
    the book. A public "SELL +48%" reveals that the position existed AND what it was
    worth, and hands a reader an exit they could not have taken at our price."""
    put_token(tmp_db)
    put_order(tmp_db, side="sell", order_id="ord:s", decision_id=None, tx_hash="5xSELLHASH")
    N.once(tmp_db)
    public = [t for c, t in channel if c == "-1001701614508"]
    assert public == [], public
    private = [t for c, t in channel if c == "private"]
    assert any("SELL" in t for t in private), "the owner still gets the close privately"


def test_the_card_never_reveals_the_size(tmp_db, channel):
    """Explicit owner instruction: do not let readers see the sizing."""
    put_token(tmp_db)
    put_decision(tmp_db, "3 smart wallets in the trenches preset (min 3)")
    put_order(tmp_db, amount_in="49865460")
    N.once(tmp_db)
    card = next(t for c, t in channel if c == "-1001701614508")
    for leak in ("49865460", "0.0498", "size", "lane"):
        assert leak not in card, f"{leak!r} leaked the book into the public channel"
    private = next(t for c, t in channel if c == "private")
    assert "size" in private, "the owner's own feed still carries it"


def test_the_card_carries_the_scan_and_the_reason(tmp_db, channel):
    put_token(tmp_db)
    put_decision(tmp_db, "4 smart wallets in the trenches preset (min 3)")
    put_order(tmp_db)
    tmp_db.execute(
        "INSERT OR REPLACE INTO token_dossiers (chain, address, built_at_ms, score, grade, "
        "blockers_json, warnings_json, unknowns_json, dossier_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (Chain.SOL.value, TOKEN, NOW, 61.0, "B", "[]", '["lp_not_burned"]', "[]",
         json.dumps({
             "liquidity_usd": {"value": "31106.5", "basis": "provider_reported"},
             "top10_pct": {"value": "18.5", "basis": "provider_reported"},
             "market_cap_usd": {"value": "124042.1", "basis": "derived"},
             "insider_pct": {"value": None, "basis": "unavailable"},
         })),
    )
    tmp_db.commit()
    N.once(tmp_db)
    card = next(t for c, t in channel if c == "-1001701614508")
    assert "$KAPI" in card
    assert "$124.0K" in card and "$31.1K" in card
    assert "Top10: 18.5%" in card
    assert "4 smart wallets in the trenches preset" in card, "the reason must travel"
    assert "lp_not_burned" in card
    assert TOKEN in card, "the contract address must be copyable"


def test_an_unavailable_field_reads_as_a_dash_not_zero(tmp_db, channel):
    """A dossier that could not establish insider share has not established it is 0%."""
    put_token(tmp_db)
    put_order(tmp_db)
    tmp_db.execute(
        "INSERT OR REPLACE INTO token_dossiers (chain, address, built_at_ms, score, grade, "
        "blockers_json, warnings_json, unknowns_json, dossier_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (Chain.SOL.value, TOKEN, NOW, 40.0, "C", "[]", "[]", "[]",
         json.dumps({"insider_pct": {"value": None, "basis": "unavailable"}})),
    )
    tmp_db.commit()
    N.once(tmp_db)
    card = next(t for c, t in channel if c == "-1001701614508")
    assert "Insider: -" in card, card
    assert "Insider: 0.0%" not in card
    assert "not measured" in card


def test_no_channel_configured_still_delivers_privately(tmp_db, monkeypatch):
    box: list[tuple[str, str]] = []
    monkeypatch.setattr(N, "credentials", lambda: ("tok", "private"))
    monkeypatch.setattr(N, "channel_id", lambda: None)
    monkeypatch.setattr(N, "send", lambda text, **kw: (box.append((kw.get("chat_id"), text)), True)[1])
    put_token(tmp_db)
    put_order(tmp_db)
    N.once(tmp_db)
    assert [c for c, _ in box] == ["private"]


def test_a_failing_channel_does_not_break_the_private_record(tmp_db, monkeypatch):
    """The operator's own copy is the one that has to be reliable."""
    monkeypatch.setattr(N, "credentials", lambda: ("tok", "private"))
    monkeypatch.setattr(N, "channel_id", lambda: "-100999")
    sent: list[str] = []

    def picky(text, **kw):
        if kw.get("chat_id") == "-100999":
            raise RuntimeError("channel is down")
        sent.append(text)
        return True

    monkeypatch.setattr(N, "send", picky)
    put_token(tmp_db)
    put_order(tmp_db)
    N.once(tmp_db)
    assert len(sent) == 1, "the private message must still be delivered and recorded"
    row = tmp_db.execute("SELECT delivered FROM notify_sent WHERE order_id='ord:1'").fetchone()
    assert row and int(row["delivered"]) == 1


def test_a_value_carried_on_an_unavailable_basis_is_still_not_a_number(tmp_db, channel):
    """The basis is the authority, not the presence of a value.

    A provider that answers `0` with `basis: unavailable` is saying "I could not check",
    and a card that prints "Insider 0.0%" from it tells the operator the token is clean on
    a field nobody measured. `value: None` alone does not exercise this -- `_dec(None)` is
    None whether or not the basis is consulted -- which is why this case is separate.
    """
    put_token(tmp_db)
    put_order(tmp_db)
    tmp_db.execute(
        "INSERT OR REPLACE INTO token_dossiers (chain, address, built_at_ms, score, grade, "
        "blockers_json, warnings_json, unknowns_json, dossier_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (Chain.SOL.value, TOKEN, NOW, 40.0, "C", "[]", "[]", "[]",
         json.dumps({
             "insider_pct": {"value": "0", "basis": "unavailable"},
             "sniper_pct": {"value": "0", "basis": "provider_reported"},
         })),
    )
    tmp_db.commit()
    N.once(tmp_db)
    card = next(t for c, t in channel if c == "-1001701614508")
    assert "Insider: -" in card, card
    assert "Snipers: 0.0%" in card, "a MEASURED zero is a real answer and must show as 0.0%"


def test_the_card_takes_the_first_clause_of_a_long_thesis(tmp_db, channel):
    """sm-trenches' thesis carries 400+ chars of standing explanation. The card takes the
    part that differs between trades; the rest lives in the journal."""
    put_token(tmp_db)
    put_decision(
        tmp_db,
        "3 smart wallets in the trenches preset (min 3); 3 independent entities (min 2); "
        "rug ratio UNAVAILABLE (ceiling 0.3): not a refusal and not strength; the rug "
        "defence is the dossier blockers -- honeypot, mint/freeze authority, "
        "dev_concentration, cluster_concentration, already_rugged -> QUARANTINED",
    )
    put_order(tmp_db)
    N.once(tmp_db)
    card = next(t for c, t in channel if c == "-1001701614508")
    why = next(line for line in card.split(chr(10)) if "Why:" in line)
    assert "3 smart wallets in the trenches preset (min 3)" in why
    assert "QUARANTINED" not in why, why
    assert len(why) <= N.THESIS_HEADLINE_CHARS + 10, len(why)


def test_a_short_thesis_is_left_alone(tmp_db, channel):
    put_token(tmp_db)
    put_decision(tmp_db, "migration 18.2s ago")
    put_order(tmp_db)
    N.once(tmp_db)
    card = next(t for c, t in channel if c == "-1001701614508")
    assert "Why: migration 18.2s ago" in card
