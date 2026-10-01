"""The EVM venue price source: can the watchdog see an EVM position at all?

Every fixture in this file is a **real response captured from mainnet on 2026-09-21**, not
a hand-written shape. That matters more here than usual, because the failure this module
exists to fix was not a logic bug — it was a whole class of token nobody could price, and
a test written against an invented record would have passed against an invented venue.

The record blobs are the literal ``result`` strings returned by ``eth_call``:

* :data:`FLAP_MID_RECORD` — ``0xfe59…7777``, a Flap curve 49.6% of the way to graduation,
  quoted in native BNB. DexScreener's ``priceNative`` for it at capture time was
  ``0.00000001347``; the curve arithmetic here lands on ``1.3471439594e-8``.
* :data:`FLAP_USDT_RECORD` — ``0x72ed…7777``, untouched, quoted in BSC USDT. DexScreener:
  ``0.000003466``; this module: ``3.466009590980e-6``.
* :data:`FLAP_GRADUATED_RECORD` — ``0x64a6…7777``, graduated (``status == 4``).
* :data:`PONS_LIVE` / :data:`PONS_GRADUATED` — the ten curve reads for a live Pons curve
  and for one whose token has migrated. The graduated one is the important fixture: those
  are the real post-migration values, and they do **not** say "graduated".
"""

from __future__ import annotations

from decimal import Decimal

from kaiba.core.schemas import Chain, EvidenceBasis
from kaiba.execution import evm_price as ep
from kaiba.execution.watchdog import PriceQuote

# --------------------------------------------------------------------------------------
# captured mainnet responses
# --------------------------------------------------------------------------------------

FLAP_MID_TOKEN = "0xfe59b933944b4d267a14c59020c0eb19a97d7777"
FLAP_MID_RECORD = (
    "0x"
    "0000000000000000000000000000000000000000000000000000000000000001"  # 0 status: 1 = on the curve
    "0000000000000000000000000000000000000000000000002f96b2a57eee7279"  # 1 quote raised, wei of BNB
    "0000000000000000000000000000000000000000014826998afdfa2eb04a9f3a"  # 2 tokens sold, atoms
    "0000000000000000000000000000000000000000000000000000000322f5daea"  # 3 price * 1e18, quote per whole token
    "0000000000000000000000000000000000000000000000000000000000000006"  # 4 uninterpreted
    "0000000000000000000000000000000000000000000000005535a97808e60000"  # 5 r * 1e18
    "0000000000000000000000000000000000000000005889e9f3e4c6e14f400000"  # 6 h * 1e18
    "000000000000000000000000000000000000000015f6844905cece43c3e00000"  # 7 K * 1e18
    "00000000000000000000000000000000000000000295be96e640669720000000"  # 8 graduation, tokens sold
    "0000000000000000000000000000000000000000000000000000000000000000"  # 9 quote token: 0 = native BNB
    "0000000000000000000000000000000000000000000000000000000000000000"  # 10
    "0000000000000000000000000000000000000000000000000000000000000000"  # 11
    "00000000000000000000000000000000000000000000000000000000000000c8"  # 12
    "00000000000000000000000000000000000000000000000000000000000000c8"  # 13
    "0000000000000000000000000000000000000000000000000000000000000000"  # 14
    "00000000000000000000000000000000000000000000000002f982801457012e"  # 15
    "0000000000000000000000000000000000000000000000000000000000000000"  # 16
    "0000000000000000000000000000000000000000000000000000000000000000"  # 17
)
FLAP_USDT_TOKEN = "0x72ed1e533ccf8cb4b6be18809f93fb854df47777"
FLAP_USDT_RECORD = (
    "0x"
    "0000000000000000000000000000000000000000000000000000000000000001"  # 0 status: 1 = on the curve
    "0000000000000000000000000000000000000000000000000000000000000000"  # 1 nothing raised yet
    "0000000000000000000000000000000000000000000000000000000000000000"  # 2 nothing sold yet
    "00000000000000000000000000000000000000000000000000000326fe453cc4"  # 3 price * 1e18, USDT per whole token
    "0000000000000000000000000000000000000000000000000000000000000006"  # 4
    "0000000000000000000000000000000000000000000000d0011262b3e5d40000"  # 5 r * 1e18
    "0000000000000000000000000000000000000000005889e9f3e4c6e14f400000"  # 6 h * 1e18
    "00000000000000000000000000000000000000359d0f1a33e40aefe21a400000"  # 7 K * 1e18
    "00000000000000000000000000000000000000000295be96e640669720000000"  # 8 graduation, tokens sold
    "00000000000000000000000055d398326f99059ff775485246999027b3197955"  # 9 quote token: BSC USDT
    "0000000000000000000000000000000000000000000000000000000000000001"  # 10
    "0000000000000000000000000000000000000000000000000000000000000000"  # 11
    "0000000000000000000000000000000000000000000000000000000000000064"  # 12
    "0000000000000000000000000000000000000000000000000000000000000064"  # 13
    "0000000000000000000000000000000000000000000000000000000000000000"  # 14
    "0000000000000000000000000000000000000000000000000000000000000000"  # 15
    "0000000000000000000000000000000000000000000000000000000000000000"  # 16
    "0000000000000000000000000000000000000000000000000000000000000000"  # 17
)
FLAP_GRADUATED_TOKEN = "0x64a6c4c1c558be705c66ee60cd4ad5f2cc2c7777"
FLAP_GRADUATED_RECORD = (
    "0x"
    "0000000000000000000000000000000000000000000000000000000000000004"  # 0 status: 4 = graduated
    "0000000000000000000000000000000000000000000000000000000000000000"  # 1
    "0000000000000000000000000000000000000000033b2e3c9fd0803ce8000000"  # 2
    "0000000000000000000000000000000000000000000000000000000000000000"  # 3
    "0000000000000000000000000000000000000000000000000000000000000006"  # 4
    "0000000000000000000000000000000000000000000000005535a97808e60000"  # 5
    "0000000000000000000000000000000000000000005889e9f3e4c6e14f400000"  # 6
    "000000000000000000000000000000000000000015f6844905cece43c3e00000"  # 7 K * 1e18
    "00000000000000000000000000000000000000000295be96e640669720000000"  # 8
    "0000000000000000000000000000000000000000000000000000000000000000"  # 9 quote token cleared on graduation
    "0000000000000000000000000000000000000000000000000000000000000000"  # 10
    "0000000000000000000000000000000000000000000000000000000000000000"  # 11
    "0000000000000000000000000000000000000000000000000000000000000064"  # 12
    "0000000000000000000000000000000000000000000000000000000000000064"  # 13
    "0000000000000000000000001b4577cf5e27e339c6ab4b667f0b4d1349c19735"  # 14 the PancakeSwap pair it graduated into
    "0000000000000000000000000000000000000000000000000de0b6b3a7640000"  # 15
    "0000000000000000000000000000000000000000000000000000000000000000"  # 16
    "0000000000000000000000000000000000000000000000000000000000000000"  # 17
)

#: ``decimals()`` -> 18 and ``totalSupply()`` -> 1e27, identical on all three Flap tokens.
DECIMALS_18 = "0x" + format(18, "064x")
SUPPLY_1E27 = "0x" + format(10**27, "064x")

#: Flap's ``getTokenV8Safe`` word 3 for :data:`FLAP_MID_RECORD`, in whole BNB per token.
#: DexScreener published ``0.00000001347`` for the same token at the same time.
FLAP_MID_PRICE_BNB = Decimal("1.3471439594E-8")

#: The ten :data:`kaiba.ingest.robinhood.CURVE_READS` for ``0x4f72…75b8`` — a live Pons
#: curve 0.10% of the way to graduation, quoted in native ETH.
PONS_LIVE = (
    1_684_067_151_405_559_690,  # quoteReserve      (wei)
    4_067_151_405_559_690,  # realQuoteReserve  (wei)
    711_870_637_545_950_581_426_517_373,  # sellableTokens
    285_714_285_714_285_714_285_714_285,  # reservedTokens
    4_200_000_000_000_000_000,  # graduationThreshold
    1_000_000_000_000_000_000_000_000_000,  # launchSupply
    100,  # feeBps
    1_789_983_414,  # launchedAt
    9_900,  # snipeTaxStartBps
    3,  # snipeTaxSeconds
)

#: The same ten reads for ``0xbcd1…2cf0`` **after** its token migrated to Uniswap v4.
#: Read on-chain, not imagined: the curve has been drained and reset, ``realQuoteReserve``
#: is back to zero and ``quoteReserve`` is back to the phantom-only 1.68 ETH — so
#: ``realQuoteReserve >= graduationThreshold`` is **false** for a graduated token.
PONS_GRADUATED = (
    1_680_000_000_000_000_000,
    0,
    0,
    285_714_285_714_285_714_285_714_285,
    4_200_000_000_000_000_000,
    1_000_000_000_000_000_000_000_000_000,
    100,
    1_789_988_280,
    9_900,
    3,
)

#: quoteReserve / (sellable + reserved) for :data:`PONS_LIVE`, in whole ETH per whole
#: token. Computed by hand from the two integers above, not by calling the code under test.
PONS_LIVE_PRICE_ETH = Decimal("1.688144149073354940847349515061212335993E-9")


def _word(value: int) -> str:
    return format(value, "064x")


def _pons_results(reads: tuple[int, ...], *, token_decimals: int = 18) -> list[str]:
    return ["0x" + _word(v) for v in reads] + ["0x" + _word(token_decimals)]


# --------------------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------------------


class FakeRpc:
    """An :data:`~kaiba.execution.evm_price.RpcBatch` that answers from a canned table.

    Keyed on the call's ``data`` prefix so a test can say "the portal returns this" without
    caring how many calls the reader batches or in what order.
    """

    def __init__(self, answers: dict[str, str | None], *, raises: bool = False) -> None:
        self.answers = answers
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    def __call__(self, calls):
        if self.raises:
            raise RuntimeError("rpc is down")
        self.calls.extend(calls)
        out: list[str | None] = []
        for to, data in calls:
            found = self.answers.get(data[:10]) if data[:10] in self.answers else None
            if found is None:
                found = self.answers.get(f"{to}:{data[:10]}")
            out.append(found)
        return out


class RecordingSource:
    """A price source that records what it was asked and returns a fixed quote."""

    name = "recorded"

    def __init__(self, quote: PriceQuote | None = None) -> None:
        self.quote_value = quote or PriceQuote.unavailable("fallback ran", "recorded")
        self.seen: list[tuple[Chain, str]] = []

    def quote(self, chain: Chain, token: str) -> PriceQuote:
        self.seen.append((chain, token))
        return self.quote_value


class RaisingSource:
    name = "boom"

    def quote(self, chain: Chain, token: str) -> PriceQuote:
        raise RuntimeError("this source is broken")


def _flap_rpc(record: str, *, decimals: str = DECIMALS_18, supply: str = SUPPLY_1E27) -> FakeRpc:
    return FakeRpc(
        {
            ep.SEL_GET_TOKEN_V8_SAFE: record,
            ep.SEL_DECIMALS: decimals,
            ep.SEL_TOTAL_SUPPLY: supply,
        }
    )


def _usd(rate: str | None) -> ep.QuoteUsd:
    value = Decimal(rate) if rate is not None else None
    return ep.QuoteUsd(price_usd=lambda chain, token: value)


# --------------------------------------------------------------------------------------
# Flap: the record decodes to the price the venue itself publishes
# --------------------------------------------------------------------------------------


def test_a_live_flap_curve_prices_to_the_number_the_venue_publishes():
    priced, note = ep.read_flap(FLAP_MID_TOKEN, _flap_rpc(FLAP_MID_RECORD))
    assert note == "ok"
    assert priced is not None
    # The venue's own price word, to every digit the venue prints. A wrong word index or a
    # wrong fixed-point scale cannot land here by accident.
    assert abs(priced.price_quote_per_token - FLAP_MID_PRICE_BNB) / FLAP_MID_PRICE_BNB < Decimal(
        "1e-9"
    )
    assert priced.quote_token is None  # word 9 is zero: quoted in native BNB
    assert priced.venue == "flap"
    assert priced.progress_pct is not None
    assert Decimal("49") < priced.progress_pct < Decimal("50")


def test_the_reserve_is_the_quote_actually_raised_not_the_notional_pool():
    """3.429 BNB, which is what the curve integral says was paid in.

    DexScreener reported ``$15,108`` of "liquidity" for this same token. That figure is
    curve notional; this one is what a seller could actually take out, and the rug monitor
    is the consumer.
    """
    priced, _ = ep.read_flap(FLAP_MID_TOKEN, _flap_rpc(FLAP_MID_RECORD))
    assert priced is not None
    assert priced.quote_reserve_base == 3_429_124_590_158_115_449
    assert priced.quote_decimals == 18
    whole = priced.quote_reserve_whole
    assert whole is not None
    assert Decimal("3.42") < whole < Decimal("3.43")


def test_an_erc20_quoted_flap_curve_keeps_its_own_quote_token():
    priced, note = ep.read_flap(FLAP_USDT_TOKEN, _flap_rpc(FLAP_USDT_RECORD))
    assert note == "ok"
    assert priced is not None
    assert priced.quote_token == "0x55d398326f99059ff775485246999027b3197955"
    expected = Decimal("3.46600959098e-6")  # DexScreener: 0.000003466
    assert abs(priced.price_quote_per_token - expected) / expected < Decimal("1e-9")


def test_a_graduated_flap_token_is_refused_by_name_not_priced():
    priced, note = ep.read_flap(FLAP_GRADUATED_TOKEN, _flap_rpc(FLAP_GRADUATED_RECORD))
    assert priced is None
    assert note == "flap_not_on_curve:status=4"


def test_a_token_the_portal_has_never_heard_of_is_refused():
    priced, note = ep.read_flap(FLAP_MID_TOKEN, FakeRpc({ep.SEL_GET_TOKEN_V8_SAFE: "0x"}))
    assert priced is None
    assert note == "flap_no_portal_record"


def test_a_dead_rpc_is_a_refusal_not_an_exception():
    priced, note = ep.read_flap(FLAP_MID_TOKEN, FakeRpc({}, raises=True))
    assert priced is None
    assert note.startswith("flap_rpc_raised:")

    priced, note = ep.read_flap(FLAP_MID_TOKEN, FakeRpc({}))
    assert priced is None
    assert note == "flap_portal_read_failed"


def test_a_record_shorter_than_the_fields_we_read_is_refused():
    short = "0x" + _word(1) * 4
    priced, note = ep.read_flap(FLAP_MID_TOKEN, _flap_rpc(short))
    assert priced is None
    assert note == "flap_no_portal_record"
    assert ep.flap_record([1, 2, 3]) is None


# --------------------------------------------------------------------------------------
# Flap: the cross-checks are what make the number trustworthy
# --------------------------------------------------------------------------------------


def _mid_record() -> ep.FlapRecord:
    record = ep.flap_record(ep._words(FLAP_MID_RECORD))
    assert record is not None
    return record


def test_a_price_word_that_disagrees_with_the_curve_is_refused_not_believed():
    """The single most important guard here.

    If the portal upgrades to a layout where word 3 is not the price, or the curve
    constants move, the reported number and ``K/(x+h)^2`` stop agreeing. Believing the
    reported one would give the watchdog a stop evaluated against fiction.
    """
    record = _mid_record()
    tampered = ep.FlapRecord(
        status=record.status,
        quote_raised_base=record.quote_raised_base,
        tokens_sold_atoms=record.tokens_sold_atoms,
        price_word=record.price_word * 2,  # a factor-of-two lie
        r_scaled=record.r_scaled,
        h_scaled=record.h_scaled,
        k_scaled=record.k_scaled,
        graduation_tokens_atoms=record.graduation_tokens_atoms,
        quote_token=record.quote_token,
    )
    priced, note = ep.parse_flap_record(
        tampered, token_decimals=18, token_supply_atoms=10**27, quote_decimals=18
    )
    assert priced is None
    assert note.startswith("flap_price_disagrees:")


def test_a_supply_the_curve_identity_does_not_predict_is_refused():
    """``K == r*(h+S)`` is what makes it safe not to hardcode a 1e9 supply."""
    record = _mid_record()
    priced, note = ep.parse_flap_record(
        record,
        token_decimals=18,
        token_supply_atoms=2 * 10**27,  # a launch config with twice the supply
        quote_decimals=18,
    )
    assert priced is None
    assert note.startswith("flap_supply_identity_failed:")


def test_a_reserve_that_fails_its_cross_check_costs_the_depth_not_the_price():
    """Depth is the rug monitor's input; the price is the stop's. Only one is load-bearing.

    So a reserve word this module cannot corroborate becomes ``None`` — unknown depth —
    while the price, which was corroborated twice, survives.
    """
    record = _mid_record()
    tampered = ep.FlapRecord(
        status=record.status,
        quote_raised_base=record.quote_raised_base * 1000,
        tokens_sold_atoms=record.tokens_sold_atoms,
        price_word=record.price_word,
        r_scaled=record.r_scaled,
        h_scaled=record.h_scaled,
        k_scaled=record.k_scaled,
        graduation_tokens_atoms=record.graduation_tokens_atoms,
        quote_token=record.quote_token,
    )
    priced, note = ep.parse_flap_record(
        tampered, token_decimals=18, token_supply_atoms=10**27, quote_decimals=18
    )
    assert note == "ok"
    assert priced is not None
    assert priced.quote_reserve_base is None
    assert priced.quote_reserve_whole is None
    assert "reserve disagrees" in priced.note


def test_missing_curve_constants_are_refused_rather_than_defaulted():
    record = _mid_record()
    for field in ("r_scaled", "h_scaled", "k_scaled"):
        broken = ep.FlapRecord(
            **{
                **{
                    k: getattr(record, k)
                    for k in (
                        "status",
                        "quote_raised_base",
                        "tokens_sold_atoms",
                        "price_word",
                        "r_scaled",
                        "h_scaled",
                        "k_scaled",
                        "graduation_tokens_atoms",
                        "quote_token",
                    )
                },
                field: 0,
            }
        )
        priced, note = ep.parse_flap_record(
            broken, token_decimals=18, token_supply_atoms=10**27, quote_decimals=18
        )
        assert priced is None
        assert note == "flap_curve_constants_missing"


def test_unreadable_token_metadata_is_refused_rather_than_assumed_to_be_18():
    rpc = FakeRpc({ep.SEL_GET_TOKEN_V8_SAFE: FLAP_MID_RECORD, ep.SEL_TOTAL_SUPPLY: SUPPLY_1E27})
    priced, note = ep.read_flap(FLAP_MID_TOKEN, rpc)
    assert priced is None
    assert note == "flap_token_metadata_unreadable"


def test_an_unreadable_quote_token_decimals_is_refused():
    """The quote decimals scale the reserve; guessing 18 is a depth reading out by 1e12."""
    ep._DECIMALS.clear()
    rpc = FakeRpc(
        {
            ep.SEL_GET_TOKEN_V8_SAFE: FLAP_USDT_RECORD,
            ep.SEL_DECIMALS: None,
            ep.SEL_TOTAL_SUPPLY: SUPPLY_1E27,
        }
    )
    priced, note = ep.read_flap(FLAP_USDT_TOKEN, rpc)
    assert priced is None
    assert note in {"flap_token_metadata_unreadable", "flap_quote_token_decimals_unreadable"}


def test_a_non_evm_address_never_reaches_the_network():
    rpc = FakeRpc({}, raises=True)
    priced, note = ep.read_flap("So11111111111111111111111111111111111111112", rpc)
    assert priced is None
    assert note == "not_an_evm_address"


# --------------------------------------------------------------------------------------
# Pons
# --------------------------------------------------------------------------------------


def _pons_state(reads: tuple[int, ...], *, quote_is_native: bool = True):
    from kaiba.ingest import robinhood as rh

    state = rh.parse_curve_state(
        ["0x" + _word(v) for v in reads],
        curve="0x" + "1" * 40,
        token="0x" + "2" * 40,
        quote_is_native=quote_is_native,
    )
    assert state is not None
    return state


def test_a_live_pons_curve_prices_to_the_reserve_ratio():
    priced, note = ep.pons_price(
        _pons_state(PONS_LIVE), token_decimals=18, quote_decimals=18, quote_token=None
    )
    assert note == "ok"
    assert priced is not None
    gap = abs(priced.price_quote_per_token - PONS_LIVE_PRICE_ETH) / PONS_LIVE_PRICE_ETH
    assert gap < Decimal("1e-20")
    assert priced.venue == "pons"


def test_pons_depth_is_the_real_reserve_not_the_phantom_one():
    """The phantom leg sets the price and cannot be withdrawn.

    Reporting ``quoteReserve`` as depth would tell the rug monitor this curve is 1.684 ETH
    deep when 0.004 ETH has ever been paid into it — a 414x overstatement on this fixture.
    """
    priced, _ = ep.pons_price(
        _pons_state(PONS_LIVE), token_decimals=18, quote_decimals=18, quote_token=None
    )
    assert priced is not None
    assert priced.quote_reserve_base == 4_067_151_405_559_690
    assert priced.quote_reserve_base != 1_684_067_151_405_559_690


def test_a_graduated_pons_curve_is_refused_although_it_does_not_look_graduated():
    """The trap: after migration the curve is drained and reset, so the venue's own
    ``realQuoteReserve >= graduationThreshold`` test is false again.

    Priced naively, this fixture returns a fixed 5.88e-9 ETH forever from a contract
    holding nothing, while the real market is a Uniswap v4 pool. That is the exact shape of
    "a stale price is worse than none".
    """
    state = _pons_state(PONS_GRADUATED)
    assert state.graduated is False  # the venue's own flag does not catch it
    priced, note = ep.pons_price(
        state, token_decimals=18, quote_decimals=18, quote_token=None
    )
    assert priced is None
    assert note == "pons_curve_complete"


def test_a_pons_curve_that_breaks_its_constant_product_is_refused():
    """Independent of the sellable-tokens check, and the broader of the two.

    Measured: 18 live on-curve tokens sit within 9.3e-10 of the invariant, and the two
    graduated ones are 0.714 away. Anything in between is a curve state this module has not
    understood, and an ununderstood curve does not get to price a position.
    """
    reads = list(PONS_GRADUATED)
    reads[2] = 1  # one sellable token, so the cheap check no longer fires
    priced, note = ep.pons_price(
        _pons_state(tuple(reads)), token_decimals=18, quote_decimals=18, quote_token=None
    )
    assert priced is None
    assert note.startswith("pons_constant_product_failed:")


def test_pons_scales_by_the_two_decimals_it_read_and_not_by_a_presumed_18():
    """Both legs of the ratio are base units, and neither of them is assumed.

    Every Pons token observed so far has 18 decimals and every quote token this module has
    priced has 18 too, so a hardcoded ``10**18`` passes on live data and is wrong the first
    time the venue lists a 6-decimal quote. The scale is the difference between a price and
    a price out by a factor of a trillion, which is not a rounding error — it is a stop
    that can never fire, or one that fires on the first tick.
    """
    base, _ = ep.pons_price(
        _pons_state(PONS_LIVE), token_decimals=18, quote_decimals=18, quote_token=None
    )
    fewer_token_decimals, _ = ep.pons_price(
        _pons_state(PONS_LIVE), token_decimals=6, quote_decimals=18, quote_token=None
    )
    fewer_quote_decimals, _ = ep.pons_price(
        _pons_state(PONS_LIVE), token_decimals=18, quote_decimals=6, quote_token=None
    )
    assert base is not None and fewer_token_decimals is not None
    assert fewer_quote_decimals is not None
    scale = Decimal(10) ** 12
    assert fewer_token_decimals.price_quote_per_token * scale == base.price_quote_per_token
    assert fewer_quote_decimals.price_quote_per_token == base.price_quote_per_token * scale


def test_a_chain_whose_native_decimals_we_have_not_established_is_refused():
    """``None`` quote token means "the chain's native coin", which has no ``decimals()``.

    So the only way to know its scale is a table, and a chain missing from that table must
    refuse rather than fall back on 18 — the reserve is denominated in it.
    """
    rpc = FakeRpc({}, raises=True)  # nothing may be asked over the wire for a native quote
    assert ep._quote_decimals(Chain.BSC, None, rpc) == 18
    assert ep._quote_decimals(Chain.ROBINHOOD, None, rpc) == 18
    assert ep._quote_decimals(Chain.ARC, None, rpc) is None
    assert ep._quote_decimals(Chain.BASE, None, rpc) is None


def test_a_wrong_quote_decimals_is_caught_by_the_reserve_cross_check():
    """The third cross-check earns its place here: it is the only thing pinning the
    reserve word to quote *base* units, so a decimals mistake surfaces as unknown depth
    rather than as a reserve out by six orders of magnitude."""
    record = _mid_record()
    priced, note = ep.parse_flap_record(
        record, token_decimals=18, token_supply_atoms=10**27, quote_decimals=6
    )
    assert note == "ok"
    assert priced is not None
    assert priced.quote_reserve_base is None
    assert "reserve disagrees" in priced.note


def test_the_pons_invariant_accepts_every_live_curve_it_was_measured_on():
    priced, note = ep.pons_price(
        _pons_state(PONS_LIVE), token_decimals=18, quote_decimals=18, quote_token=None
    )
    assert note == "ok" and priced is not None


def test_pons_reads_the_curve_from_the_factory_and_then_remembers_it():
    from kaiba.ingest import robinhood as rh

    ep._PONS_CURVE.clear()
    token = "0x" + "3" * 40
    curve = "0x" + "4" * 40
    launched = "0x" + _word(int(token, 16)) + _word(int(curve, 16)) + _word(0) * 2 + _word(0)

    class Rpc:
        def __init__(self) -> None:
            self.batches: list[list[tuple[str, str]]] = []

        def __call__(self, calls):
            self.batches.append(list(calls))
            if len(calls) == 1 and calls[0][1].startswith(rh.SELECTOR_GET_LAUNCHED_TOKEN):
                return [launched]
            return _pons_results(PONS_LIVE)

    rpc = Rpc()
    priced, note = ep.read_pons(token, rpc)
    assert note == "ok" and priced is not None
    assert len(rpc.batches) == 2  # cold: factory, then the curve
    assert len(rpc.batches[1]) == len(rh.CURVE_READS) + 1  # one batch, not eleven calls

    ep.read_pons(token, rpc)
    assert len(rpc.batches) == 3  # warm: the curve address is not looked up again


def test_a_rate_limited_endpoint_is_not_reported_as_a_token_with_no_curve(monkeypatch):
    """Two different facts that used to share one name.

    Measured on 2026-09-21: the Robinhood public RPC 429s under a burst and one 429 puts
    the whole ``chain.*`` family into a 60 s cooldown, so 15 of a 20-token sample never
    reached the wire. Every one of them reported ``pons_factory_record_short`` — which
    reads as "this token has no curve" and is permanent, when the truth was transient and
    self-healing. The stop depends on telling those apart.
    """
    ep._PONS_CURVE.clear()
    monkeypatch.setattr(ep, "_pons_curve_from_registry", lambda token: None)
    priced, note = ep.read_pons("0x" + "5" * 40, FakeRpc({}))
    assert priced is None
    assert note == "pons_factory_read_failed"

    priced, note = ep.read_pons("0x" + "5" * 40, FakeRpc({ep_selector_launched(): "0x" + "0" * 64}))
    assert priced is None
    assert note == "pons_factory_record_short"


def ep_selector_launched() -> str:
    from kaiba.ingest import robinhood as rh

    return rh.SELECTOR_GET_LAUNCHED_TOKEN


def test_the_flap_portal_not_answering_is_not_the_same_as_having_no_record():
    priced, note = ep.read_flap(FLAP_MID_TOKEN, FakeRpc({}))
    assert priced is None
    assert note == "flap_portal_read_failed"

    priced, note = ep.read_flap(FLAP_MID_TOKEN, _flap_rpc("0x"))
    assert priced is None
    assert note == "flap_no_portal_record"


def test_the_curve_address_comes_from_our_own_registry_before_the_network(monkeypatch):
    """One round trip instead of two, on the endpoint that rate limits us.

    ``ingest.robinhood.record_new_token`` already stores the curve and the quote token for
    every launch it sees, so for any token we could hold a position in the factory lookup
    is a network call we have already paid for once.
    """
    from kaiba.ingest import robinhood as rh

    ep._PONS_CURVE.clear()
    token = "0x" + "7" * 40
    curve = "0x" + "8" * 40
    monkeypatch.setattr(ep, "_pons_curve_from_registry", lambda t: (curve, None))

    class Rpc:
        def __init__(self) -> None:
            self.batches: list[list[tuple[str, str]]] = []

        def __call__(self, calls):
            self.batches.append(list(calls))
            assert not any(c[1].startswith(rh.SELECTOR_GET_LAUNCHED_TOKEN) for c in calls)
            return _pons_results(PONS_LIVE)

    rpc = Rpc()
    priced, note = ep.read_pons(token, rpc)
    assert note == "ok" and priced is not None
    assert len(rpc.batches) == 1
    assert rpc.batches[0][0][0] == curve


def test_a_registry_row_that_does_not_say_what_the_curve_is_quoted_in_is_not_used(tmp_db):
    """78% of Pons launches quote in native ETH. Assuming the majority case for the other
    22% would price a token in the wrong asset, so an incomplete row falls through to the
    factory instead."""
    from kaiba.core.db import jdump, upsert

    token = "0x" + "9" * 40
    curve = "0x" + "a" * 40
    upsert(
        tmp_db,
        "tokens",
        {
            "chain": Chain.ROBINHOOD.value,
            "address": token,
            "pool": curve,
            "meta_json": jdump({}),
            "first_seen_ms": 1,
        },
        ("chain", "address"),
    )
    tmp_db.commit()
    assert ep._pons_curve_from_registry(token) is None

    upsert(
        tmp_db,
        "tokens",
        {
            "chain": Chain.ROBINHOOD.value,
            "address": token,
            "pool": curve,
            "meta_json": jdump({"quote_is_native": True}),
            "first_seen_ms": 1,
        },
        ("chain", "address"),
    )
    tmp_db.commit()
    assert ep._pons_curve_from_registry(token) == (curve, None)

    # A ``pair_token`` on its own is a half-written row: ``record_new_token`` always writes
    # both keys together, so one without the other came from something else and is not
    # evidence of anything. The factory is one call away and is the authority.
    upsert(
        tmp_db,
        "tokens",
        {
            "chain": Chain.ROBINHOOD.value,
            "address": token,
            "pool": curve,
            "meta_json": jdump({"pair_token": "0x" + "c" * 40}),
            "first_seen_ms": 1,
        },
        ("chain", "address"),
    )
    tmp_db.commit()
    assert ep._pons_curve_from_registry(token) is None

    # And the 22% case, written in full, is used as written.
    upsert(
        tmp_db,
        "tokens",
        {
            "chain": Chain.ROBINHOOD.value,
            "address": token,
            "pool": curve,
            "meta_json": jdump({"quote_is_native": False, "pair_token": "0x" + "c" * 40}),
            "first_seen_ms": 1,
        },
        ("chain", "address"),
    )
    tmp_db.commit()
    assert ep._pons_curve_from_registry(token) == (curve, "0x" + "c" * 40)


def test_the_registry_lookup_never_raises_without_a_database():
    assert ep._pons_curve_from_registry("0x" + "b" * 40) is None


# --------------------------------------------------------------------------------------
# the USD leg: where a stale number would get in
# --------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_a_quote_token_rate_is_reused_inside_its_ttl_and_refetched_after():
    clock = Clock()
    calls: list[tuple[Chain, str]] = []

    def price(chain: Chain, token: str) -> Decimal:
        calls.append((chain, token))
        return Decimal("2")

    usd = ep.QuoteUsd(price_usd=price, clock=clock, ttl_s=60, max_age_s=300)
    assert usd.usd(Chain.BSC, None) == (Decimal("2"), "fresh")
    assert usd.usd(Chain.BSC, None) == (Decimal("2"), "cached")
    assert len(calls) == 1
    clock.t += 61
    assert usd.usd(Chain.BSC, None)[1] == "fresh"
    assert len(calls) == 2


def test_a_failed_refresh_expires_into_blindness_rather_than_a_stale_rate():
    """The mutation this whole class exists to make impossible.

    The obvious cache keeps the last good value and serves it when the refresh fails. That
    turns a dead provider into a watchdog that is confidently wrong — and blindness is
    monitored (``protection.max_blind_s``) while a wrong price is not. So past
    ``max_age_s`` the answer is ``None``.
    """
    clock = Clock()
    answers = [Decimal("2"), None, None, None]

    def price(chain: Chain, token: str) -> Decimal | None:
        return answers.pop(0) if answers else None

    usd = ep.QuoteUsd(price_usd=price, clock=clock, ttl_s=60, max_age_s=300)
    assert usd.usd(Chain.BSC, None)[0] == Decimal("2")

    clock.t += 100  # past the TTL, inside the hard limit: reuse, and say so
    value, note = usd.usd(Chain.BSC, None)
    assert value == Decimal("2")
    assert "refresh failed" in note

    clock.t += 300  # past the hard limit: blind
    value, note = usd.usd(Chain.BSC, None)
    assert value is None
    assert "no USD rate" in note


def test_a_provider_that_raises_is_the_same_as_one_that_answers_nothing():
    def price(chain: Chain, token: str) -> Decimal:
        raise RuntimeError("provider down")

    usd = ep.QuoteUsd(price_usd=price, clock=Clock())
    assert usd.usd(Chain.BSC, None)[0] is None


def test_a_native_quote_is_priced_from_the_chain_the_coin_actually_trades_on():
    """Robinhood Chain's native is ETH, and ETH's price is not chain-specific.

    Asking chain 4663 for an ETH price would be asking a one-year-old L2 to price the
    asset; the mainnet WETH pools are the reference, exactly as ``ingest.robinhood`` does.
    """
    seen: list[tuple[Chain, str]] = []

    def price(chain: Chain, token: str) -> Decimal:
        seen.append((chain, token))
        return Decimal("3000")

    ep.QuoteUsd(price_usd=price, clock=Clock()).usd(Chain.ROBINHOOD, None)
    assert seen == [(Chain.ETH, ep.WETH_MAINNET)]


def test_a_chain_with_no_native_reference_is_blind_rather_than_guessed():
    usd = ep.QuoteUsd(price_usd=lambda c, t: Decimal("1"), clock=Clock())
    value, note = usd.usd(Chain.ARC, None)
    assert value is None
    assert "no native USD reference" in note


# --------------------------------------------------------------------------------------
# the price source the watchdog actually calls
# --------------------------------------------------------------------------------------


def _source(rpc: ep.RpcBatch, *, usd: str | None = "600", fallback=None) -> ep.EvmVenuePriceSource:
    return ep.EvmVenuePriceSource(
        None,
        transports={Chain.BSC: rpc, Chain.ROBINHOOD: rpc},
        quote_usd=_usd(usd),
        fallback=fallback,
    )


def test_the_source_turns_a_live_flap_curve_into_a_usable_usd_quote():
    quote = _source(_flap_rpc(FLAP_MID_RECORD)).quote(Chain.BSC, FLAP_MID_TOKEN)
    assert quote.usable
    assert quote.basis is EvidenceBasis.DERIVED
    assert isinstance(quote.price_usd, Decimal)
    # The USD price is the curve price times the quote rate, and the curve price is this
    # module's own full-precision K/(x+h)^2 rather than the venue's truncated word — so it
    # agrees with the published number rather than being copied from it.
    expected = FLAP_MID_PRICE_BNB * Decimal("600")
    assert abs(quote.price_usd - expected) / expected < Decimal("1e-9")
    assert quote.liquidity_usd == Decimal("3.429124590158115449") * Decimal("600")
    assert quote.source == "evm-venue:flap"


def test_money_never_becomes_a_float_on_the_way_through():
    quote = _source(_flap_rpc(FLAP_MID_RECORD)).quote(Chain.BSC, FLAP_MID_TOKEN)
    assert type(quote.price_usd) is Decimal
    assert type(quote.liquidity_usd) is Decimal


def test_a_curve_we_can_read_but_cannot_convert_to_usd_is_blind_not_quote_denominated():
    """The fail-closed half. The price in BNB is exact and it is *not* a USD price."""
    quote = _source(_flap_rpc(FLAP_MID_RECORD), usd=None).quote(Chain.BSC, FLAP_MID_TOKEN)
    assert not quote.usable
    assert quote.basis is EvidenceBasis.UNAVAILABLE
    assert quote.price_usd is None
    assert "curve priced but" in (quote.note or "")


def test_a_graduated_token_is_handed_to_the_dex_stack_not_refused_outright():
    """One source, both sides of graduation — the property that lets one config value work."""
    after = PriceQuote(
        price_usd=Decimal("0.5"), basis=EvidenceBasis.PROVIDER_REPORTED, source="prices:dexscreener"
    )
    fallback = RecordingSource(after)
    quote = _source(_flap_rpc(FLAP_GRADUATED_RECORD), fallback=fallback).quote(
        Chain.BSC, FLAP_GRADUATED_TOKEN
    )
    assert quote is after
    assert fallback.seen == [(Chain.BSC, FLAP_GRADUATED_TOKEN)]


def test_without_a_fallback_a_graduated_token_is_blind_and_says_why():
    quote = _source(_flap_rpc(FLAP_GRADUATED_RECORD)).quote(Chain.BSC, FLAP_GRADUATED_TOKEN)
    assert not quote.usable
    assert "flap_not_on_curve:status=4" in (quote.note or "")


def test_a_chain_with_no_venue_reader_goes_straight_to_the_fallback():
    fallback = RecordingSource()
    source = ep.EvmVenuePriceSource(None, readers={}, quote_usd=_usd("1"), fallback=fallback)
    source.quote(Chain.BASE, "0x" + "6" * 40)
    assert fallback.seen == [(Chain.BASE, "0x" + "6" * 40)]


def test_a_solana_token_is_never_run_through_an_evm_reader():
    fallback = RecordingSource()
    source = _source(FakeRpc({}, raises=True), fallback=fallback)
    quote = source.quote(Chain.SOL, "So11111111111111111111111111111111111111112")
    assert fallback.seen == [(Chain.SOL, "So11111111111111111111111111111111111111112")]
    assert "is not an EVM chain" in (quote.note or "")


def test_a_blind_position_keeps_the_venues_reason_and_not_just_the_dex_stacks():
    """Two blind answers, and only one of them is a diagnosis.

    "no pair prices this token" is true of *every* pre-graduation token, so on its own it
    tells an operator nothing about why the watchdog cannot see a position. The venue's
    reason — a drained curve, an unpriceable quote token, a 429 — is the actionable half,
    and returning the fallback's quote untouched threw it away. Measured on a live
    Robinhood sample: every miss reported only the DexScreener sentence.
    """
    dex_blind = PriceQuote.unavailable("no pair prices this token (0 pair(s) returned)", "prices")
    source = _source(_flap_rpc(FLAP_GRADUATED_RECORD), fallback=RecordingSource(dex_blind))
    quote = source.quote(Chain.BSC, FLAP_GRADUATED_TOKEN)
    assert not quote.usable
    assert "flap_not_on_curve:status=4" in (quote.note or "")  # ours
    assert "no pair prices this token" in (quote.note or "")  # theirs


def test_a_usable_fallback_quote_is_passed_through_untouched():
    """Nothing to explain when there is a price, and the DEX receipt must survive intact."""
    good = PriceQuote(
        price_usd=Decimal("0.5"),
        basis=EvidenceBasis.PROVIDER_REPORTED,
        source="prices:dexscreener",
    )
    source = _source(_flap_rpc(FLAP_GRADUATED_RECORD), fallback=RecordingSource(good))
    assert source.quote(Chain.BSC, FLAP_GRADUATED_TOKEN) is good


def test_a_reader_that_raises_cannot_take_the_watchdog_tick_down():
    class Boom:
        def __call__(self, calls):
            raise RuntimeError("nope")

    source = ep.EvmVenuePriceSource(
        None,
        readers={Chain.BSC: lambda token, rpc: (_ for _ in ()).throw(RuntimeError("reader"))},
        transports={Chain.BSC: Boom()},
        quote_usd=_usd("1"),
    )
    quote = source.quote(Chain.BSC, FLAP_MID_TOKEN)
    assert not quote.usable
    assert "venue reader raised" in (quote.note or "")


def test_a_fallback_that_raises_is_blindness_with_a_note_not_a_crash():
    source = _source(_flap_rpc(FLAP_GRADUATED_RECORD), fallback=RaisingSource())
    quote = source.quote(Chain.BSC, FLAP_GRADUATED_TOKEN)
    assert not quote.usable
    assert "fallback raised" in (quote.note or "")


def test_the_quote_carries_the_instant_the_chain_was_read():
    from kaiba.core.schemas import now_ms

    before = now_ms()
    quote = _source(_flap_rpc(FLAP_MID_RECORD)).quote(Chain.BSC, FLAP_MID_TOKEN)
    assert before <= quote.observed_ms <= now_ms()


def test_the_source_satisfies_the_watchdogs_price_source_protocol():
    from kaiba.execution.watchdog import PriceSource

    assert isinstance(ep.EvmVenuePriceSource(None), PriceSource)
    assert isinstance(ep.ChainRoutedPriceSource({}), PriceSource)


# --------------------------------------------------------------------------------------
# routing: one setting must not blind the chain it was not written for
# --------------------------------------------------------------------------------------


def test_routing_sends_each_chain_to_the_source_that_understands_it():
    """``protection.price_source`` is one string for every chain.

    A source that only speaks EVM would have blinded the eight live Solana positions the
    moment it was configured. This is the test that keeps that from shipping.
    """
    solana = RecordingSource(PriceQuote(price_usd=Decimal("1"), basis=EvidenceBasis.DERIVED))
    evm = RecordingSource(PriceQuote(price_usd=Decimal("2"), basis=EvidenceBasis.DERIVED))
    routed = ep.ChainRoutedPriceSource({Chain.SOL: solana, Chain.BSC: evm})

    assert routed.quote(Chain.SOL, "So1").price_usd == Decimal("1")
    assert routed.quote(Chain.BSC, "0xabc").price_usd == Decimal("2")
    assert solana.seen == [(Chain.SOL, "So1")]
    assert evm.seen == [(Chain.BSC, "0xabc")]


def test_an_unrouted_chain_uses_the_fallback_and_an_unrouted_one_without_is_blind():
    fallback = RecordingSource()
    assert ep.ChainRoutedPriceSource({}, fallback=fallback).quote(Chain.ETH, "0xa") is (
        fallback.quote_value
    )
    blind = ep.ChainRoutedPriceSource({}).quote(Chain.ETH, "0xa")
    assert not blind.usable
    assert "no price source for eth" in (blind.note or "")


def test_a_routed_source_that_raises_is_contained():
    routed = ep.ChainRoutedPriceSource({Chain.SOL: RaisingSource()})
    quote = routed.quote(Chain.SOL, "So1")
    assert not quote.usable
    assert "raised RuntimeError" in (quote.note or "")


# --------------------------------------------------------------------------------------
# the numbers this module hardcodes are the numbers that were measured
# --------------------------------------------------------------------------------------


def test_the_tolerances_sit_between_the_measured_noise_and_the_measured_failures():
    """Both tolerances have to clear real noise and still catch the real break.

    Measured noise: 8.5e-11 (Flap price), 8.6e-11 (Flap reserve), 9.3e-10 (Pons invariant).
    Measured break: 0.714 (a graduated Pons curve). A tolerance outside this band is either
    a false alarm on every tick or a graduated curve priced as a live one.
    """
    measured_noise = Decimal("9.3e-10")
    measured_break = Decimal("0.714")
    for tolerance in (
        ep.PRICE_CROSS_CHECK_TOLERANCE,
        ep.RESERVE_CROSS_CHECK_TOLERANCE,
        ep.PONS_INVARIANT_TOLERANCE,
    ):
        assert measured_noise < tolerance < measured_break


def test_the_hard_expiry_is_not_longer_than_the_native_price_tolerance_it_was_taken_from():
    from kaiba.providers.native_price import DEFAULT_TOLERANCE_MS

    assert ep.QUOTE_USD_MAX_AGE_S * 1000 <= DEFAULT_TOLERANCE_MS
    assert ep.QUOTE_USD_TTL_S < ep.QUOTE_USD_MAX_AGE_S


def test_waiting_for_limiter_capacity_never_outlasts_a_watchdog_tick():
    """A tick that cannot get capacity must report blind, not queue behind the last one.

    The shipped ``protection.poll_interval_s`` is 5. That file is not read here on purpose
    — an operator lowering it should not fail this suite — but the invariant the constant
    was chosen against is worth pinning, because a wait longer than a tick converts a busy
    provider into a watchdog that silently runs late instead of one that reports blind.
    """
    assert 0 < ep.WAIT_FOR_SLOT_S < 5.0
    assert ep.RPC_TIMEOUT_S >= ep.WAIT_FOR_SLOT_S
