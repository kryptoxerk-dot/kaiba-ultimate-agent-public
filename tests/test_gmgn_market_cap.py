"""Market cap is derived inside the GMGN adapter, where the supply units are known.

MEASURED 2026-09-22: ``market_cap_usd`` was known on **0 of 7,980** dossiers across sol,
bsc and robinhood, while ``liquidity_usd`` was known on 100%. The field exists on
``TokenDossier`` and nothing ever populated it, so any rule written against it -- "size up
above $500k", say -- would have read UNKNOWN forever and never fired once. That is the
same shape as ``creator_rug_count``: a shipped rule whose input nothing supplied.

GMGN's ``token info`` does not send a market cap. It sends the two numbers it is made of,
in the same payload::

    "circulating_supply": "1000000000",  "total_supply": "1000000000",
    "price": {"price": "0.0001240421", ...}

``flatten_payload`` already surfaces both -- ``circulating_supply`` as a top-level scalar
and ``price`` lifted out of the nested realtime block -- so the multiplication happens
where both units are GMGN's own.

WHY THIS IS NOT THE THING ``dyor`` FORBIDS. ``dyor.NUM_PROPERTIES`` deliberately omits
``total_supply`` because "providers report it in different units (UI amount vs raw atoms)
and cross-provider comparison would invent conflicts", and it adds: "Each adapter uses its
own supply internally, where the units are known." This is that. The product is offered as
one adapter's claim about market cap, not as a supply figure for anyone else to compare.

MEASURED CONTEXT, which is why the operator's ">$500k" rule could not have fired anyway:
computed over the 117 sol tokens where supply was recoverable, market cap ran p10 $6,602,
p50 $15,175, p90 $47,232. The universe this agent scans is new launches -- 97% of scanned
tokens are 0-3 minutes old -- and none of them are near $500k. Populating the field is
worth doing; a threshold above the whole distribution is not.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.providers import gmgn_cli as G


@pytest.fixture
def token_info():
    """The real shape, trimmed. Verbatim keys from a live `token info` read."""
    return {
        "address": "0x6863b3fd8caa85d6ca8f80bf10083a94de467777",
        "symbol": "KAPI",
        "circulating_supply": "1000000000",
        "total_supply": "1000000000",
        "liquidity": "31106.575578599884",
        "holder_count": 594,
        "price": {"price": "0.0001240421", "price_1m": "0.00013451066"},
    }


def _claims(body: dict) -> dict:
    """The adapter's own normaliser, driven on an already-flattened body."""
    from kaiba.core.schemas import Chain

    return G.normalize_security(body, chain=Chain.BSC)


def test_market_cap_is_supply_times_price(token_info):
    out = _claims(G.flatten_payload(token_info))
    assert out["market_cap_usd"] == Decimal("1000000000") * Decimal("0.0001240421")
    assert out["market_cap_usd"] == Decimal("124042.1000")


def test_no_supply_means_no_market_cap(token_info):
    """An absent number is never zero. A missing supply leaves the field UNKNOWN."""
    token_info.pop("circulating_supply")
    token_info.pop("total_supply")
    out = _claims(G.flatten_payload(token_info))
    assert "market_cap_usd" not in out


def test_no_price_means_no_market_cap(token_info):
    token_info.pop("price")
    out = _claims(G.flatten_payload(token_info))
    assert "market_cap_usd" not in out


def test_circulating_supply_wins_over_total(token_info):
    """Market cap is the circulating float, not the fully diluted number."""
    token_info["circulating_supply"] = "500000000"
    token_info["total_supply"] = "1000000000"
    out = _claims(G.flatten_payload(token_info))
    assert out["market_cap_usd"] == Decimal("500000000") * Decimal("0.0001240421")


def test_total_supply_is_the_fallback(token_info):
    """Many launches report only total_supply; a token with no float is still a token."""
    token_info.pop("circulating_supply")
    out = _claims(G.flatten_payload(token_info))
    assert out["market_cap_usd"] == Decimal("1000000000") * Decimal("0.0001240421")


@pytest.mark.parametrize("bad", ["0", "-1", "", "abc", None])
def test_an_unusable_supply_is_not_a_market_cap(token_info, bad):
    token_info["circulating_supply"] = bad
    token_info["total_supply"] = bad
    out = _claims(G.flatten_payload(token_info))
    assert "market_cap_usd" not in out


def test_a_zero_price_is_not_a_market_cap(token_info):
    token_info["price"] = {"price": "0"}
    out = _claims(G.flatten_payload(token_info))
    assert "market_cap_usd" not in out


def test_a_provider_supplied_market_cap_is_preferred(token_info):
    """If GMGN ever starts sending one, its own number beats our multiplication."""
    token_info["usd_market_cap"] = "999999"
    out = _claims(G.flatten_payload(token_info))
    assert out["market_cap_usd"] == Decimal("999999")


def test_the_liquidity_and_price_claims_still_work(token_info):
    """The control: adding a derived field must not disturb the ones that work today."""
    out = _claims(G.flatten_payload(token_info))
    assert out["liquidity_usd"] == Decimal("31106.575578599884")
    assert out["price_usd"] == Decimal("0.0001240421")


# ------------------------------------------------------------------ the wiring


def test_market_cap_is_a_known_dyor_property():
    """Without this the claim is dropped by `resolve` and the field stays UNKNOWN.

    This is the same miss that made the exit wallet-balance clamp a silent no-op for a
    full deploy cycle: the code was right and the layer under it refused the call.
    """
    from kaiba.intelligence.dyor import NUM_PROPERTIES

    assert "market_cap_usd" in NUM_PROPERTIES


def test_the_tolerance_is_relative_like_price():
    """Two providers pricing the same token seconds apart differ by a few percent. An
    absolute band would report a conflict on every scan."""
    from kaiba.intelligence.dyor import NUM_PROPERTIES

    assert NUM_PROPERTIES["market_cap_usd"].relative is True


def test_the_dossier_actually_carries_it():
    import inspect

    from kaiba.intelligence import dyor

    source = inspect.getsource(dyor)
    assert 'market_cap_usd=resolution.measure("market_cap_usd"' in source, (
        "build_dossier does not populate market_cap_usd"
    )


# ------------------------------------------------------------------ the short-circuit


def test_a_raw_gmgn_body_is_normalised_not_passed_through(token_info):
    """THE BUG that made every gmgn field evaporate.

    ``_unwrap_gmgn`` short-circuited on "payload shares a key with our vocabulary". Every
    raw gmgn body does: ``token security`` carries ``can_sell``, ``token info`` carries
    ``symbol``, ``name`` and ``holder_count``, and all four are our property names too. So
    the adapter was never called and everything needing translation was dropped --
    ``liquidity`` never became ``liquidity_usd``, ``top_10_holder_rate`` never became
    ``top10_pct``, and ``can_sell: 0`` arrived as an int, which ``resolve`` discards.
    """
    from kaiba.intelligence.dyor import _CONVERTED_MARKERS

    raw_info_keys = set(token_info) | {"holder_count", "symbol", "name"}
    assert not (raw_info_keys & _CONVERTED_MARKERS), (
        "a raw token-info body still trips the converted-body probe"
    )
    raw_security_keys = {"can_sell", "is_honeypot", "top_10_holder_rate", "buy_tax",
                         "renounced_mint", "creator"}
    assert not (raw_security_keys & _CONVERTED_MARKERS), (
        "a raw token-security body still trips the converted-body probe"
    )


def test_an_already_converted_body_still_short_circuits():
    """The probe must keep doing its actual job: never normalise twice."""
    from kaiba.intelligence.dyor import _CONVERTED_MARKERS

    converted = {"liquidity_usd": Decimal(1), "can_sell": True, "symbol": "KAPI"}
    assert any(k in _CONVERTED_MARKERS for k in converted)


def test_the_markers_exclude_every_name_a_provider_spells_the_same():
    """These six are what broke it. Adding any of them back reintroduces the bug."""
    from kaiba.intelligence.dyor import _CONVERTED_MARKERS

    for shared in ("symbol", "name", "creator", "holder_count", "can_sell", "rug_ratio"):
        assert shared not in _CONVERTED_MARKERS, shared


def test_unwrap_actually_normalises_a_raw_token_info_body(token_info):
    """Drive the real path. Inspecting the marker set is not the same as using it.

    An earlier version of these tests asserted only that `_CONVERTED_MARKERS` excluded the
    colliding names, and the old probe survived a mutation that put it straight back --
    because nothing exercised `_unwrap_gmgn` itself.
    """
    from kaiba.core.schemas import Chain
    from kaiba.intelligence.dyor import _unwrap_gmgn
    from kaiba.providers import gmgn_cli

    props, _receipt = _unwrap_gmgn(token_info, gmgn_cli, Chain.BSC)
    assert props.get("liquidity_usd") == Decimal("31106.575578599884"), props
    assert props.get("price_usd") == Decimal("0.0001240421"), props
    assert props.get("market_cap_usd") == Decimal("124042.1000"), props


def test_unwrap_normalises_a_raw_token_security_body():
    """`can_sell: 0` must arrive as a real bool; as an int `resolve` throws it away."""
    from kaiba.core.schemas import Chain
    from kaiba.intelligence.dyor import _unwrap_gmgn
    from kaiba.providers import gmgn_cli

    body = {"can_sell": 0, "is_honeypot": 1, "top_10_holder_rate": "0.1543",
            "buy_tax": "0.01", "sell_tax": "0.01"}
    props, _receipt = _unwrap_gmgn(body, gmgn_cli, Chain.BSC)
    assert props.get("can_sell") is False, props
    assert isinstance(props.get("can_sell"), bool), "an int here evaporates in resolve()"
    assert props.get("top10_pct") == Decimal("15.43"), props
    assert props.get("buy_tax_bps") == Decimal("100"), props


def test_unwrap_does_not_normalise_an_already_converted_body():
    from kaiba.core.schemas import Chain
    from kaiba.intelligence.dyor import _unwrap_gmgn
    from kaiba.providers import gmgn_cli

    converted = {"liquidity_usd": Decimal("5"), "can_sell": True}
    props, _receipt = _unwrap_gmgn(converted, gmgn_cli, Chain.BSC)
    assert props is converted or props == converted
