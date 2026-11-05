"""Tweet-launch position upkeep: token registry, entry repair, sells we did not make.

MEASURED 2026-10-07: $IMAGINE's buy was booked before its token was in ``tokens``, so the
position had no entry price, no stop, paged ``protection_blind`` every minute and halted
entries on every chain; it was then sold from the GMGN app and our ledger never saw it.
"""

from __future__ import annotations

import types
from decimal import Decimal

import pytest

from kaiba.core.db import fetch_one
from kaiba.core.schemas import Chain, Lane, OrderState, Side
from kaiba.execution import accounting, fills, protection
from kaiba.execution import engine as _engine  # noqa: F401 - binds the real protection.arm before any patch
from kaiba.execution import tweet_launch as tl
from kaiba.execution import watchdog as wd
from tests.test_accounting import NOW, ONE_SOL, make_order, persist

MINT = "Lnch1111111111111111111111111111111111pump"
QTY = 50_000_000_000_000
WALLET = "Wa11et11111111111111111111111111111111111"


def _launch_row(conn, launch_id="tl:1:sol", symbol="IMAGINE", name="Imagine", dex="pump"):
    conn.execute(
        "INSERT INTO tweet_launches (launch_id, tweet_id, author, chain, dex, mode, verdict, name, symbol, "
        "decided_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (launch_id, "1", "elonmusk", "sol", dex, "live", "launch", name, symbol, NOW),
    )


def _open(conn, *, price_usd=None, token=MINT, opened_ms=NOW):
    buy = make_order(order_id="ord:tl:buy", amount_in=ONE_SOL, filled_out=QTY, token=token)
    buy = buy.model_copy(update={"lane": Lane.TWEET_LAUNCH, "created_ms": opened_ms, "updated_ms": opened_ms})
    persist(conn, buy)
    pos = accounting.apply_fill(buy, conn, price_usd=price_usd, ts=opened_ms)
    assert pos is not None
    return pos.position_id


# ---------------------------------------------------------------- register_token


def test_register_token_writes_the_row_when_the_chain_read_fails(tmp_db, monkeypatch):
    monkeypatch.setattr(fills, "token_decimals", lambda *a, **k: (None, "unavailable", "rpc down"))
    _launch_row(tmp_db)
    assert tl.register_token(tmp_db, "tl:1:sol", Chain.SOL, MINT) == 6
    row = fetch_one(tmp_db, "SELECT decimals, symbol, name, launchpad, meta_json FROM tokens WHERE address=?", (MINT,))
    assert row["decimals"] == 6 and row["symbol"] == "IMAGINE" and row["launchpad"] == "pump"
    assert "launchpad_standard" in row["meta_json"]


def test_register_token_fills_a_row_that_exists_without_decimals(tmp_db, monkeypatch):
    monkeypatch.setattr(fills, "token_decimals", lambda *a, **k: (None, "unavailable", "rpc down"))
    tmp_db.execute("INSERT INTO tokens (chain, address, first_seen_ms) VALUES ('bsc', '0xabc', 1)")
    assert tl.register_token(tmp_db, "tl:none", Chain.BSC, "0xabc") == 18
    assert fetch_one(tmp_db, "SELECT decimals FROM tokens WHERE address='0xabc'")["decimals"] == 18


def test_register_token_prefers_the_verified_chain_read(tmp_db, monkeypatch):
    def verified(chain, token, conn, register):
        assert register is True
        conn.execute("INSERT INTO tokens (chain, address, decimals, first_seen_ms, meta_json) VALUES (?,?,?,?,?)",
                     (chain.value, token, 9, 1, '{"decimals_source": "verified_onchain"}'))
        return 9, "verified_onchain", None

    monkeypatch.setattr(fills, "token_decimals", verified)
    _launch_row(tmp_db)
    assert tl.register_token(tmp_db, "tl:1:sol", Chain.SOL, MINT) == 9
    row = fetch_one(tmp_db, "SELECT decimals, symbol FROM tokens WHERE address=?", (MINT,))
    assert row["decimals"] == 9 and row["symbol"] == "IMAGINE"


# ---------------------------------------------------------------- repair_entries


@pytest.fixture
def armed(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(protection, "arm", lambda pid, conn=None: calls.append(pid) or True)
    return calls


def _derive_stub(monkeypatch, *, usable: bool, price="0.00000002"):
    def derive(order, **kw):
        assert kw["register_token"] is True
        return types.SimpleNamespace(usable_for_stop=usable, price_usd=Decimal(price) if usable else None,
                                     basis="fill_ratio" if usable else "unavailable", notes=("stub",))

    monkeypatch.setattr(fills, "derive", derive)
    monkeypatch.setattr(fills, "record", lambda conn, fp: None)


def test_repair_gives_a_blind_launch_position_its_entry_and_arms_it(tmp_db, monkeypatch, armed):
    pid = _open(tmp_db)
    armed.clear()
    assert fetch_one(tmp_db, "SELECT entry_price_usd FROM positions WHERE position_id=?", (pid,))["entry_price_usd"] is None
    _derive_stub(monkeypatch, usable=True)
    assert tl.repair_entries(tmp_db) == [pid]
    row = fetch_one(tmp_db, "SELECT entry_price_usd, peak_price_usd FROM positions WHERE position_id=?", (pid,))
    assert Decimal(row["entry_price_usd"]) == Decimal("0.00000002")
    assert Decimal(row["peak_price_usd"]) == Decimal("0.00000002")
    assert armed == [pid]
    assert tl.repair_entries(tmp_db) == []          # idempotent: priced now


def test_repair_writes_nothing_without_a_measured_price(tmp_db, monkeypatch, armed):
    pid = _open(tmp_db)
    armed.clear()
    _derive_stub(monkeypatch, usable=False)
    assert tl.repair_entries(tmp_db) == []
    assert fetch_one(tmp_db, "SELECT entry_price_usd FROM positions WHERE position_id=?", (pid,))["entry_price_usd"] is None
    assert armed == []


def test_repair_leaves_priced_positions_alone(tmp_db, monkeypatch, armed):
    _open(tmp_db, price_usd=Decimal("0.5"))
    armed.clear()
    _derive_stub(monkeypatch, usable=True)
    assert tl.repair_entries(tmp_db) == []
    assert armed == []


# ---------------------------------------------------------------- external_sells


@pytest.fixture
def wallet(monkeypatch):
    monkeypatch.setattr(wd, "exit_wallet_for", lambda chain: WALLET)


def _sell(sig="SiG1", qty=QTY, native=1_450_000_000, ts=NOW + 90_000):
    return tl.ChainSell(sig, ts, qty, native)


def test_an_empty_wallet_books_the_onchain_sale_and_closes_the_position(tmp_db, wallet):
    pid = _open(tmp_db, price_usd=Decimal("0.00000002"))
    t0 = NOW + 5 * 60_000
    seen = []

    def find(w, token, since):
        seen.append((w, token, since))
        return [_sell()]

    out = tl.external_sells(tmp_db, now_ms=t0, balance=lambda *a: 0, find_sells=find)
    assert out == [(pid, "empty_once")] and seen == []      # one empty read is not enough
    out = tl.external_sells(tmp_db, now_ms=t0 + tl.EMPTY_CONFIRM_MS - 1, balance=lambda *a: 0, find_sells=find)
    assert out == [(pid, "empty_waiting")] and seen == []   # nor two inside the window
    out = tl.external_sells(tmp_db, now_ms=t0 + tl.EMPTY_CONFIRM_MS, balance=lambda *a: 0, find_sells=find)
    assert out == [(pid, "booked:1")]
    assert seen == [(WALLET, MINT, NOW)]
    pos = fetch_one(tmp_db, "SELECT closed_ms, proceeds_native, qty FROM positions WHERE position_id=?", (pid,))
    assert pos["closed_ms"] is not None and int(pos["proceeds_native"]) == 1_450_000_000 and int(pos["qty"]) == 0
    order = fetch_one(tmp_db, "SELECT provider, side, state FROM orders WHERE tx_hash='SiG1'")
    assert (order["provider"], order["side"], order["state"]) == ("external", Side.SELL.value, "filled")
    # A later pass finds nothing open and books nothing twice.
    assert tl.external_sells(tmp_db, now_ms=t0 + 10 * tl.EMPTY_CONFIRM_MS, balance=lambda *a: 0,
                             find_sells=find) == []


def test_a_wallet_that_still_holds_tokens_resets_the_empty_mark(tmp_db, wallet):
    pid = _open(tmp_db, price_usd=Decimal("1"))
    t0 = NOW + 5 * 60_000
    no = lambda *a: pytest.fail("must not look for sells")  # noqa: E731
    assert tl.external_sells(tmp_db, now_ms=t0, balance=lambda *a: 0, find_sells=no) == [(pid, "empty_once")]
    assert tl.external_sells(tmp_db, now_ms=t0 + 30_000, balance=lambda *a: QTY, find_sells=no) == []
    # The streak restarted: a later empty read is again only the first one.
    assert tl.external_sells(tmp_db, now_ms=t0 + 120_000, balance=lambda *a: 0, find_sells=no) == [(pid, "empty_once")]
    # An unreadable balance is never "empty".
    assert tl.external_sells(tmp_db, now_ms=t0 + 300_000, balance=lambda *a: None, find_sells=no) == []


def test_our_own_ladder_sell_is_not_booked_twice(tmp_db, wallet):
    pid = _open(tmp_db, price_usd=Decimal("1"))
    own = make_order(Side.SELL, order_id="ord:own:sell", decision_id=None, amount_in=QTY * 3 // 10, filled_out=ONE_SOL,
                     token=MINT)
    own = own.model_copy(update={"lane": Lane.TWEET_LAUNCH})
    persist(tmp_db, own)
    accounting.apply_fill(own, tmp_db, filled_in=QTY * 3 // 10, ts=NOW + 60_000)
    t0 = NOW + 5 * 60_000
    sells = [_sell("OWN", qty=QTY * 3 // 10, ts=NOW + 60_000),
             _sell("EXT", qty=QTY * 7 // 10, native=700_000_000)]
    tl.external_sells(tmp_db, now_ms=t0, balance=lambda *a: 0, find_sells=lambda *a: sells)
    out = tl.external_sells(tmp_db, now_ms=t0 + tl.EMPTY_CONFIRM_MS, balance=lambda *a: 0,
                            find_sells=lambda *a: sells)
    assert out == [(pid, "booked:1")]
    assert fetch_one(tmp_db, "SELECT 1 FROM orders WHERE tx_hash='OWN'") is None
    assert fetch_one(tmp_db, "SELECT 1 FROM orders WHERE tx_hash='EXT'") is not None


def test_an_order_in_flight_blocks_the_check(tmp_db, wallet):
    _open(tmp_db, price_usd=Decimal("1"))
    pending = make_order(Side.SELL, order_id="ord:pending", decision_id=None, amount_in=QTY, filled_out=None,
                         token=MINT)
    persist(tmp_db, pending.model_copy(update={"state": OrderState.SUBMITTED}))
    no = lambda *a: pytest.fail("must not read the wallet")  # noqa: E731
    assert tl.external_sells(tmp_db, now_ms=NOW + 10 * 60_000, balance=no, find_sells=no) == []


def test_an_unfound_sale_alerts_once_an_hour_and_keeps_the_position_open(tmp_db, wallet):
    from kaiba.core.db import fetch_all

    pid = _open(tmp_db, price_usd=Decimal("1"))
    t0 = NOW + 5 * 60_000
    for dt in (0, tl.EMPTY_CONFIRM_MS, 2 * tl.EMPTY_CONFIRM_MS):
        tl.external_sells(tmp_db, now_ms=t0 + dt, balance=lambda *a: 0, find_sells=lambda *a: [])
    alerts = [r for r in fetch_all(tmp_db, "SELECT payload FROM events") if "tweet_launch_position_empty"
              in r["payload"]]
    assert len(alerts) == 1
    assert fetch_one(tmp_db, "SELECT closed_ms FROM positions WHERE position_id=?", (pid,))["closed_ms"] is None


def test_a_sale_already_on_our_books_by_signature_is_not_booked_again(tmp_db, wallet):
    pid = _open(tmp_db, price_usd=Decimal("1"))
    known = make_order(Side.SELL, order_id="ord:known", decision_id=None, amount_in=QTY // 4, filled_out=ONE_SOL,
                       token=MINT).model_copy(update={"lane": Lane.TWEET_LAUNCH, "tx_hash": "KNOWN"})
    persist(tmp_db, known)
    accounting.apply_fill(known, tmp_db, filled_in=QTY // 4, ts=NOW + 60_000)
    # The chain reports a slightly different amount (a transfer tax, say): only the
    # signature identifies it as ours.
    sells = [_sell("KNOWN", qty=QTY // 4 + QTY // 10, ts=NOW + 60_000), _sell("EXT", qty=QTY * 3 // 4)]
    t0 = NOW + 5 * 60_000
    tl.external_sells(tmp_db, now_ms=t0, balance=lambda *a: 0, find_sells=lambda *a: sells)
    out = tl.external_sells(tmp_db, now_ms=t0 + tl.EMPTY_CONFIRM_MS, balance=lambda *a: 0,
                            find_sells=lambda *a: sells)
    assert out == [(pid, "booked:1")]
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM orders WHERE tx_hash='KNOWN'")["n"] == 1
