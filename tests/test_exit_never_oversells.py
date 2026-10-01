"""An exit sells what the WALLET holds, never what the ledger believes.

MEASURED 2026-09-22 on the live box. Two sol positions had been retrying their exits for
over an hour, every attempt rejected with ``HTTP 400`` from GMGN's swap endpoint:

    8JVtwRnDiDmjV2pSRTmqzPtXZyNdgUkpijrufgYJRARA
        wallet holds  47,876,517,476,461
        we asked for  50,932,465,400,491     (+6.4%)
    EgP1f5J9LDn1fuTMfrrgT5ZRMiLBitYdkmDs818BS44k
        wallet holds  29,203,541,102,943
        we asked for  29,498,526,366,610     (+1.0%)

``position.qty`` is our ledger's arithmetic: what the buy's ``filled_out`` said, less what
we have sold. The wallet is the truth, and on any token that takes a cut on transfer the
two diverge from the first fill onward. A 100% exit therefore asks for more than exists,
the venue refuses it, and the position can never be closed by the machine.

The cost is not just the stuck position. Each retry is a ``gmgn-cli`` subprocess inside the
protection tick, so two unsellable positions took ticks from ~4 s to 16.8 s against a 5 s
budget; that tripped ``protection_overrun``, which HALTS ENTRIES ON EVERY CHAIN. One
arithmetic mismatch stopped the whole agent from trading.

THE RULE. Read the wallet, and never ask for more than it holds. Only ever clamp DOWN:

* a balance we cannot read changes nothing -- exits must not become contingent on a second
  network call succeeding, or a provider blip becomes an unprotected position;
* a balance of zero is refused rather than sent as a zero-size order;
* clamping up is never allowed, so a stale or wrong high reading cannot invent tokens.

Selling slightly less than the ledger thinks we own costs a dust remainder. Selling more
than we own costs the entire exit, and -- through the overrun -- every entry on every
chain.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain, Lane, LaneMode, OrderState, Side
from kaiba.execution import watchdog as W
from kaiba.execution.watchdog import DefaultExitSubmitter, wallet_token_units

WALLET = "62EbtFbmJATT94Y6Uguf6a1kdaJuZWAu8ycqbg3xKfQg"
TOKEN = "8JVtwRnDiDmjV2pSRTmqzPtXZyNdgUkpijrufgYJRARA"

#: The live payload, verbatim. ``decimal: 0`` is a placeholder on every chain measured --
#: see ``parse_native_balance`` -- so the token's own decimals must come from elsewhere.
LIVE_PAYLOAD = {
    "balances": [
        {
            "wallet_address": WALLET,
            "token_address": TOKEN,
            "balance": "47876.517476461",
            "decimal": 0,
            "height": 449306003,
            "tx_index": 0,
        }
    ]
}


# ------------------------------------------------------------------ the reader


def test_the_live_payload_parses_to_base_units(monkeypatch):
    monkeypatch.setattr(W, "_token_balance_payload", lambda *a, **k: LIVE_PAYLOAD)
    assert wallet_token_units(Chain.SOL, WALLET, TOKEN, 9) == 47_876_517_476_461


def test_the_placeholder_decimal_field_is_ignored(monkeypatch):
    """``decimal: 0`` would make 47,876 tokens into 47,876 base units."""
    monkeypatch.setattr(W, "_token_balance_payload", lambda *a, **k: LIVE_PAYLOAD)
    assert wallet_token_units(Chain.SOL, WALLET, TOKEN, 6) == 47_876_517_476


def test_no_float_rounding(monkeypatch):
    """float('0.392310583560926627') * 1e18 is off by hundreds of units."""
    payload = {"balances": [{"balance": "0.392310583560926627", "height": 1}]}
    monkeypatch.setattr(W, "_token_balance_payload", lambda *a, **k: payload)
    assert wallet_token_units(Chain.SOL, WALLET, TOKEN, 18) == 392_310_583_560_926_627


def test_a_zero_with_no_height_is_unavailable(monkeypatch):
    """The measured signature of "GMGN has nothing", not of an empty wallet."""
    payload = {"balances": [{"balance": "0", "height": 0}]}
    monkeypatch.setattr(W, "_token_balance_payload", lambda *a, **k: payload)
    assert wallet_token_units(Chain.SOL, WALLET, TOKEN, 9) is None


def test_a_real_zero_is_zero(monkeypatch):
    payload = {"balances": [{"balance": "0", "height": 449306003}]}
    monkeypatch.setattr(W, "_token_balance_payload", lambda *a, **k: payload)
    assert wallet_token_units(Chain.SOL, WALLET, TOKEN, 9) == 0


@pytest.mark.parametrize("payload", [None, {}, {"balances": []}, {"balances": [{}]}, "boom"])
def test_an_unreadable_payload_is_unavailable(monkeypatch, payload):
    monkeypatch.setattr(W, "_token_balance_payload", lambda *a, **k: payload)
    assert wallet_token_units(Chain.SOL, WALLET, TOKEN, 9) is None


def test_a_raising_provider_is_unavailable_not_a_crash(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("cli died")

    monkeypatch.setattr(W, "_token_balance_payload", boom)
    assert wallet_token_units(Chain.SOL, WALLET, TOKEN, 9) is None


def test_fractional_base_units_truncate_down(monkeypatch):
    """Rounding up invents money we do not have, which is a rejected send."""
    payload = {"balances": [{"balance": "1.9999999999", "height": 1}]}
    monkeypatch.setattr(W, "_token_balance_payload", lambda *a, **k: payload)
    assert wallet_token_units(Chain.SOL, WALLET, TOKEN, 2) == 199


# ------------------------------------------------------------------ the clamp


class _Pos:
    def __init__(self, qty: int) -> None:
        self.position_id = "pos_test"
        self.chain = Chain.SOL
        self.token = TOKEN
        self.qty = qty
        self.lane = Lane.SM_TRENCHES
        self.mode = LaneMode.LIVE


def _submitter(tmp_db, monkeypatch, *, balance: int | None):
    sub = DefaultExitSubmitter(tmp_db)
    monkeypatch.setattr(W, "token_decimals", lambda conn, chain, token: 9)
    monkeypatch.setattr(W, "wallet_token_units", lambda *a, **k: balance)
    monkeypatch.setattr(W, "exit_wallet_for", lambda chain: WALLET)
    monkeypatch.setattr(DefaultExitSubmitter, "_min_out", lambda self, p, q, d, quote: 1)
    sent: dict = {}

    def fake_build(**kw):
        sent.update(kw)
        return type("O", (), {"order_id": "ord:x", **kw})()

    monkeypatch.setattr(W.executor, "build_order", fake_build)
    monkeypatch.setattr(
        W.executor, "submit",
        lambda order, conn: type("R", (), {
            "state": OrderState.SUBMITTED, "order_id": "ord:x", "detail": ""})(),
    )
    return sub, sent


def test_a_full_exit_is_clamped_to_the_wallet(tmp_db, monkeypatch):
    """THE REGRESSION: 50,932,465,400,491 asked against 47,876,517,476,461 held."""
    sub, sent = _submitter(tmp_db, monkeypatch, balance=47_876_517_476_461)
    quote = W.PriceQuote(price_usd=Decimal("1"), liquidity_usd=Decimal("1000"))
    out = sub._live(_Pos(50_932_465_400_491), Decimal(100), quote, "stop")
    assert out.ok, out.detail
    assert sent["amount_in"] == 47_876_517_476_461


def test_a_ledger_within_the_wallet_is_left_alone(tmp_db, monkeypatch):
    """The clamp only ever reduces. A smaller ask must pass through untouched."""
    sub, sent = _submitter(tmp_db, monkeypatch, balance=99_000_000_000_000)
    quote = W.PriceQuote(price_usd=Decimal("1"), liquidity_usd=Decimal("1000"))
    sub._live(_Pos(50_932_465_400_491), Decimal(100), quote, "stop")
    assert sent["amount_in"] == 50_932_465_400_491, "must never clamp UP to the balance"


def test_a_partial_exit_clamps_after_the_percentage(tmp_db, monkeypatch):
    sub, sent = _submitter(tmp_db, monkeypatch, balance=10_000)
    quote = W.PriceQuote(price_usd=Decimal("1"), liquidity_usd=Decimal("1000"))
    sub._live(_Pos(100_000), Decimal(50), quote, "tp1")
    assert sent["amount_in"] == 10_000, "50% of 100,000 is 50,000, but only 10,000 exist"


def test_an_unreadable_balance_does_not_block_the_exit(tmp_db, monkeypatch):
    """A provider blip must not turn a stop into an unprotected position."""
    sub, sent = _submitter(tmp_db, monkeypatch, balance=None)
    quote = W.PriceQuote(price_usd=Decimal("1"), liquidity_usd=Decimal("1000"))
    out = sub._live(_Pos(50_932_465_400_491), Decimal(100), quote, "stop")
    assert out.ok
    assert sent["amount_in"] == 50_932_465_400_491


def test_a_zero_balance_refuses_rather_than_sending_nothing(tmp_db, monkeypatch):
    sub, sent = _submitter(tmp_db, monkeypatch, balance=0)
    quote = W.PriceQuote(price_usd=Decimal("1"), liquidity_usd=Decimal("1000"))
    out = sub._live(_Pos(50_932_465_400_491), Decimal(100), quote, "stop")
    assert not out.ok
    assert "wallet" in out.detail.lower(), out.detail
    assert not sent, "nothing may be built for a zero-size sell"


# ------------------------------------------------------------------ the provider gate


def test_token_balance_is_an_allowed_read():
    """The clamp is silently a no-op if the provider refuses the call.

    MEASURED: the first deploy of this fix changed nothing on the live box, because
    ``gmgn_cli._ALLOWED`` did not list the endpoint and every read came back
    ``portfolio token-balance is not a read-only command this module may run``. The
    reader returned None, the clamp treated that as "unknown", and the exit went out at
    the ledger quantity exactly as before.
    """
    from kaiba.providers.gmgn_cli import _ALLOWED

    assert ("portfolio", "token-balance") in _ALLOWED


def test_a_balance_is_never_served_stale():
    """A stale HIGH balance is precisely the bug: it re-permits the over-ask."""
    from kaiba.providers.gmgn_cli import _TTL

    ttl, grace = _TTL["portfolio.token_balance"]
    assert grace == 0, "a balance must never be served past its TTL"
    assert 0 < ttl <= 15, ttl


def test_a_refused_read_is_unknown_not_a_number(monkeypatch):
    """`GmgnResult.data` is None on refusal; that must not become a quantity."""
    class _Receipt:
        note = "portfolio token-balance is not a read-only command this module may run"

    class _Result:
        data = None
        receipt = _Receipt()

    monkeypatch.setattr(W, "_token_balance_payload", lambda *a, **k: _Result().data)
    assert wallet_token_units(Chain.SOL, WALLET, TOKEN, 9) is None


def test_the_unwrapped_list_shape_also_parses(monkeypatch):
    """The provider registry unwraps this endpoint to the ``balances`` list."""
    monkeypatch.setattr(W, "_token_balance_payload", lambda *a, **k: LIVE_PAYLOAD["balances"])
    assert wallet_token_units(Chain.SOL, WALLET, TOKEN, 9) == 47_876_517_476_461
