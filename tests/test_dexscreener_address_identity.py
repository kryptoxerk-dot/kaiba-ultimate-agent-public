"""SOL-ADDRESS-MATCH-20261102: token identity before quote/depth selection."""
from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain
from kaiba.providers import dexscreener as ds
from kaiba.providers import prices


def pair(*, base, quote, price="1", depth="100000", name="fixture-pool"):
    return ds.PairSnapshot(chain=Chain.SOL, chain_slug="solana", pair_address=name,
        base_address=base, quote_address=quote, price_usd=Decimal(price), liquidity_usd=Decimal(depth))


def test_case_variants_are_distinct_valid_32_byte_solana_addresses():
    # Both encodings fit 32 bytes. Case folding loses actual address information.
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    values = []
    for text in ("A"+"1"*42, "a"+"1"*42):
        number = 0
        for char in text:
            number = number*58 + alphabet.index(char)
        values.append(number.to_bytes(32, "big"))
    assert len(values[0]) == len(values[1]) == 32 and values[0] != values[1]
    assert not ds.same_address("A"+"1"*42, "a"+"1"*42)


@pytest.mark.parametrize("left,right,expected", [
    ("A"+"1"*42, "A"+"1"*42, True),
    (" A"+"1"*42+" ", "A"+"1"*42, True),
    ("a"+"1"*42, "A"+"1"*42, False),
    ("fixture", "FIXTURE", False),
    ("0x"+"ab"*20, "0x"+"AB"*20, True),
    ("0x"+"ab"*20, "0X"+"AB"*20, True),
    (" 0x"+"ab"*20+" ", "0x"+"AB"*20, True),
    ("0x"+"ab"*20, "0x"+"ac"*20, False),
])
def test_only_evm_address_identity_is_case_insensitive(left, right, expected):
    assert ds.same_address(left, right) is expected


def test_wrong_case_base_token_cannot_supply_the_requested_mint_price():
    mint = "A"+"1"*42
    wrong = pair(base="a"+"1"*42, quote="B"+"1"*42, price="1000", depth="1000000")
    correct = pair(base=mint, quote="B"+"1"*42, price="0.001", depth="50000")
    assert not wrong.prices_for(mint)
    assert prices.pick_pair([wrong], mint) is None
    assert prices.pick_pair([wrong, correct], mint) is correct
    assert correct.prices_for(mint)


def test_case_variant_quote_asset_does_not_receive_native_pool_preference():
    mint, native = "B"+"1"*42, "A"+"1"*42
    deepest = pair(base=mint, quote="C"+"1"*42, name="deepest")
    other = pair(base=mint, quote="a"+"1"*42, depth="50000", name="case-variant-quote")
    assert prices.pick_pair([deepest, other], mint, prefer_quotes=[native]) is deepest
    # The existing sufficient-depth preference still works for the exact asset.
    exact = pair(base=mint, quote=native, depth="50000", name="actual-native-quote")
    assert prices.pick_pair([deepest, exact], mint, prefer_quotes=[native]) is exact
