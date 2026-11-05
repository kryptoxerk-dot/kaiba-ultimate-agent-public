from decimal import Decimal

import pytest

from kaiba.learning.elysium_public_tape import normalize
from kaiba.learning.evm_wallet_paper import paper

TOKEN = "0x" + "a" * 40
POOL = "0x" + "b" * 64
WALLET = "0x" + "c" * 40
TX = "0x" + "d" * 64
NATIVE = "0x" + "0" * 40


def test_eleos_uses_bound_quote_and_aggregates_exact_distinct_logs():
    print_ = {"token": TOKEN, "pool": POOL, "trader": WALLET, "tx": TX, "li": 1,
              "side": 1, "ts": 1, "amount": "2", "hype": "1", "price": "99"}
    market = {"source": "eleos", "token": TOKEN, "pool": POOL,
              "trades": [print_, print_, {**print_, "li": 2, "amount": "3", "hype": "4"}]}
    swaps, coverage = normalize([market], [{"pool": POOL, "currencies": [NATIVE, TOKEN]}], 1_000)
    assert coverage["eleos"] == 1
    assert len(swaps) == 1
    assert swaps[0].side == "buy"
    assert swaps[0].price == Decimal("1")
    assert paper(swaps, chain="elysium", as_of_ms=1_000)["execution_ready"] is False


@pytest.mark.parametrize("quote", [None, "0x" + "e" * 40])
def test_eleos_omits_unbound_or_non_hype_quote(quote):
    market = {"source": "eleos", "token": TOKEN, "pool": POOL, "trades": []}
    witness = [] if quote is None else [{"pool": POOL, "currencies": [quote, TOKEN]}]
    assert normalize([market], witness, 1_000) == ([], {"unbound_quote": 1})


def _perpme(*rows):
    return {"source": "perpme", "token": TOKEN, "pool": POOL,
            "trades": {"token": TOKEN, "quote_token": {"address": NATIVE, "native": True, "decimals": 18},
                       "items": list(rows)}}


def test_perpme_normalizes_sell_and_uses_cashflow_ratio():
    row = {"tx_hash": TX, "trader": WALLET, "side": "sell", "timestamp": 1,
           "amount_token": "4", "amount_quote": "1", "price_quote": "99"}
    swaps, _ = normalize([_perpme(row, row)], [], 1_000)
    assert len(swaps) == 1
    assert (swaps[0].side, swaps[0].price, swaps[0].at_ms) == ("sell", Decimal("0.25"), 1_000)


def test_perpme_refuses_conflicting_transaction_prints():
    row = {"tx_hash": TX, "trader": WALLET, "side": "sell", "timestamp": 1,
           "amount_token": "4", "amount_quote": "1"}
    with pytest.raises(ValueError, match="conflicting_print_identity"):
        normalize([_perpme(row, {**row, "amount_quote": "2"})], [], 1_000)


def test_perpme_refuses_wrong_token_and_future_print():
    market = _perpme()
    market["trades"]["token"] = WALLET
    with pytest.raises(ValueError, match="perpme_tape_identity_mismatch"):
        normalize([market], [], 1_000)
    row = {"tx_hash": TX, "trader": WALLET, "side": "buy", "timestamp": 2,
           "amount_token": "4", "amount_quote": "1"}
    with pytest.raises(ValueError, match="print_after_capture"):
        normalize([_perpme(row)], [], 1_000)
