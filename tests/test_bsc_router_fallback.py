"""A BSC position that graduated off its Flap curve must still get a price - or refuse.

THE LIVE BUG, 2026-09-22. Measured over 20 minutes on the live box, one BSC position was
blind every tick and therefore had no working stop:

    27x  0xb4dcd30148ee  "no venue curve: flap_not_on_curve:status=4; fallback: provider..."
     3x  0x252cc0200078  same

``status=4`` is Flap's graduated state (:class:`kaiba.execution.evm_price.FlapRecord`).
``read_flap`` refuses it correctly - there is no curve left to read - and the DexScreener
fallback could not price it either. There was no third layer on the BSC route. This is
structurally the identical defect fixed on the Solana route the same day
(``tests/test_watchdog_router_fallback.py``), where the chain became
curve -> DexScreener -> executable router quote.

WHY PANCAKESWAP V2 AND NOT A ROUTER API
---------------------------------------

MEASURED 2026-09-22 against ``https://bsc-dataseed.bnbchain.org`` (the endpoint the box
uses; ``bsc.publicnode.com`` is dead from that box and is not proposed). Thirteen Flap
token addresses already present in this tree were read from Flap's portal; five came back
``status=4``, the same graduated state as the blind position:

======================================================  =====================================
graduated Flap token                                    word 14 (pair) -> ``factory()``
======================================================  =====================================
``0x5727a62145babe985a2948e5b859579687697777``           ``0x465d...04b0`` -> Pancake V2
``0x64a6c4c1c558be705c66ee60cd4ad5f2cc2c7777``           ``0x1b45...9735`` -> Pancake V2
``0xb077ada375b5e416f15e8b8f6827dadfa53a7777``           ``0xebd8...340b`` -> Pancake V2
``0xf140586d90e84ebd78ed198df4503a33b7057777``           ``0xc330...5b76`` -> Pancake V2
``0xf660cb724a25c8108c1e88ed127001fca1057777``           ``0xac90...fa89`` -> Pancake V2
======================================================  =====================================

**5 of 5 graduated Flap tokens land in PancakeSwap V2** (factory
``0xca143ce32fe78f1f7019d7d551a6402fc5350c73`` on all five). So the router that can price a
graduated Flap token is the one it graduated into, read with a plain ``eth_call`` to
``getAmountsOut`` on the V2 router - no API key, no new provider, no subprocess, and the
same ``bsc-dataseed.bnbchain.org`` transport ``read_flap`` already uses.

It priced **5 of 5**: three through ``[WBNB, token]`` directly and two through
``[WBNB, quoteToken, token]``, where ``quoteToken`` is word 9 of the token's own Flap
record. Neither of those two quote tokens is WBNB or USDT, so a hardcoded list of common
intermediates would have missed both - the venue's own record is what finds the route.

THE PRICE IS CORROBORATED BY A SOURCE THAT SHARES NO CODE WITH IT
------------------------------------------------------------------

MEASURED 2026-09-22, this layer against ``ProviderPriceSource`` (DexScreener) on the five
live graduated tokens - two completely independent paths to the same number, one from the
pair's reserves over ``eth_call`` and one from a third-party indexer:

====================  ==============  ==============  ======
token                 pancake-v2      dexscreener     gap
====================  ==============  ==============  ======
``0x5727a62145ba``    0.0000032626    0.0000032480    0.447%
``0x64a6c4c1c558``    0.0004545103    0.0004536000    0.200%
``0xb077ada375b5``    0.0001786299    0.0001778000    0.465%
``0xf140586d90e8``    0.0000346510    0.0000343800    0.782%
``0xf660cb724a25``    0.0000608941    0.0000607500    0.237%
====================  ==============  ==============  ======

Worst gap **0.78%**, which is about what the probe's own impact (~0.1%), the router's
25 bps fee and the seconds between the two reads account for. That is the check that the
decoding, the decimals and the BNB/USD leg are all right, and it is the reason this layer
is behind DexScreener rather than in front of it: where both can answer they agree, so
there is nothing to gain by spending a round trip to prefer this one.

IT REFUSES RATHER THAN GUESSES
-------------------------------

MEASURED in the same batch, and this is the property that matters more than the price:

* an **on-curve** Flap token (``0xfe59b933944b4d267a14c59020c0eb19a97d7777``, status 1)
  reverts with ``PancakeLibrary: INSUFFICIENT_LIQUIDITY``;
* an address with no pair at all (``0x...dead``) reverts with ``execution reverted: 0x``.

A revert arrives through :data:`~kaiba.execution.evm_price.RpcBatch` as ``None``, which
this layer reports as a named refusal. There is no branch in which it invents a number.

LATENCY, against the 5000 ms ``protection.poll_interval_s`` tick
-----------------------------------------------------------------

MEASURED 2026-09-22 in the shape ``Watchdog._prefetch_quotes`` uses - the five graduated
tokens above quoted **concurrently**, one worker and one transport each, at
``Priority.EXIT``, on the **shipped** ``rpc`` budget (``limiter.DEFAULTS["rpc"]``:
``min_interval_ms: 50``, ``capacity: 40``, ``refill_per_s: 20.0``, ``max_inflight: 6``;
``config/risk.yaml`` ships no override for ``rpc``):

* trial 1: **1530 ms** wall, 5 of 5 priced
* trial 2: **1239 ms** wall, 5 of 5 priced
* trial 3: **754 ms** wall, 5 of 5 priced

Worst 1530 ms against a 5000 ms budget, and comparable to the Solana router's 1396 ms for
four mints. A single batched ``eth_call`` of nine quotes measured 382-439 ms.

End to end through the whole fixed BSC route - curve, then DexScreener, then the router,
five positions concurrently, a freshly built source per worker as ``prefetch_source_factory``
builds one - MEASURED after this change:

* **1834 ms / 2524 ms / 2353 ms** wall, **5 of 5 priced on 3 of 3 trials**;
* and several of them never reached the router at all: DexScreener answered first, which
  is the last-resort ordering doing its job.

Before the shared BNB/USD cache (see :data:`~kaiba.execution.evm_price._BSC_NATIVE_USD`)
the same shape priced only **3-4 of 5**, and never because the route was missing: the
refusal read ``router priced via wbnb but no USD rate for 0xbb4cdb9cbd36b01bd1...`` next
to ``rate limited: dexscreener: minimum interval``. The route had been found and was being
discarded for want of an FX rate the process already held.

The slot wait is bounded at :data:`~kaiba.execution.evm_price.ROUTER_LAST_RESORT_WAIT_FOR_SLOT_S`
(2.0 s), the same constant and the same reasoning as the Solana last resort: contention
must produce a refusal, never a protection overrun. ``OVERRUN_HALT_TICKS`` halts entries
on **every** chain after three consecutive ticks at twice the budget.

PROBE SIZE
----------

MEASURED on all five graduated tokens across probe sizes from 1e12 to 1e17 wei. At
:data:`~kaiba.execution.evm_price.ROUTER_PROBE_WEI` (5e15 wei = 0.005 BNB, the BNB analogue
of ``jupiter.PROBE_LAMPORTS`` = 0.02 SOL) the quoted price sits at most **+0.135%** above
the near-mid 1e12 probe across the five pools - 13.5 bps, against a shipped
``stop_loss_bps`` of 3000. At 1e17 wei the same pools drift up to +2.70%, which is why the
probe is not larger.

WHAT THIS LAYER DELIBERATELY DOES NOT REPORT
---------------------------------------------

``liquidity_usd`` and ``executable_quote_usd`` are both left ``None``, and that is a
refusal too. ``protection.py`` documents ``executable_quote_usd`` as "the **per-unit price
an actual sell quote would fill at**, not a notional", and this probe is a BUY probe -
putting the probe's notional there would feed the anti-wick rule a number that is not the
thing it compares. ``PriceQuote`` says the rug monitor and the anti-wick rule may be
unavailable and the stop may not; these positions have no price at all today, so nothing
is lost.

UNMEASURED, and stated rather than hidden: ``getAmountsOut`` is constant-product
arithmetic on the pair's reserves plus the router's 25 bps fee. It does not model a
**transfer tax**, so on a taxed token the realised sell differs from this mark by the tax.
What would settle it: replaying a live Flap sell against the pair's reserves at
``block - 1``, the way ``viability.PonsCurveDepth`` was settled.
"""

from __future__ import annotations

import inspect
import re
from decimal import Decimal

import pytest

from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, EvidenceBasis
from kaiba.execution import evm_price as ep
from kaiba.execution import watchdog as wd

# ======================================================================================
# the live world, MEASURED 2026-09-22 - every byte below came back from
# https://bsc-dataseed.bnbchain.org and is replayed verbatim
# ======================================================================================

#: A graduated Flap token whose Pancake V2 pair is against WBNB directly.
NATIVE_TOKEN = "0x64a6c4c1c558be705c66ee60cd4ad5f2cc2c7777"

#: A graduated Flap token whose pair is against its own ERC-20 quote token (word 9).
ERC20_TOKEN = "0xb077ada375b5e416f15e8b8f6827dadfa53a7777"
ERC20_QUOTE = "0x4ef9d3062c7f6eba4aae4990c5036598c6eff4ec"

#: An ON-CURVE Flap token. Its probe reverts ``INSUFFICIENT_LIQUIDITY``: no pair yet.
ON_CURVE_TOKEN = "0xfe59b933944b4d267a14c59020c0eb19a97d7777"

#: ``decimals()`` -> 18, for both tokens above.
DECIMALS_18 = "0x" + format(18, "064x")

#: ``getAmountsOut`` returns for ``[WBNB, NATIVE_TOKEN]`` at 5e15 and 1e12 wei.
NATIVE_PROBE_OUT = 8646734071833394734771
NATIVE_REF_OUT = 1729543383625854726

#: ``getAmountsOut`` returns for ``[WBNB, ERC20_QUOTE, ERC20_TOKEN]`` at the same sizes.
ERC20_PROBE_OUT = 20580412278668888911948
ERC20_REF_OUT = 4119657316479381669

#: The exact 18-word Flap record for ``ERC20_TOKEN``: status 4 (graduated), word 9 the
#: quote token, word 14 the Pancake V2 pair.
ERC20_PORTAL_RECORD = "0x" + "".join(
    [
        "0000000000000000000000000000000000000000000000000000000000000004",  # 0 status
        "0000000000000000000000000000000000000000000000000000000000000000",  # 1 raised
        "0000000000000000000000000000000000000000033b2e3c9fd0803ce8000000",  # 2 sold
        "0000000000000000000000000000000000000000000000000000000000000000",  # 3 price
        "0000000000000000000000000000000000000000000000000000000000000006",  # 4
        "000000000000000000000000000000000000000000000001a9fe195d99640000",  # 5 r
        "0000000000000000000000000000000000000000005889e9e604102da7dc0000",  # 6 h
        "00000000000000000000000000000000000000006dccebb91e7cde503ddc0000",  # 7 K
        "00000000000000000000000000000000000000000295be96e640669720000000",  # 8 graduation
        "0000000000000000000000004ef9d3062c7f6eba4aae4990c5036598c6eff4ec",  # 9 quote token
        "0000000000000000000000000000000000000000000000000000000000000001",  # 10
        "0000000000000000000000000000000000000000000000000000000000000000",  # 11
        "0000000000000000000000000000000000000000000000000000000000000064",  # 12
        "0000000000000000000000000000000000000000000000000000000000000064",  # 13
        "000000000000000000000000ebd8dc168b7f401fc9a781683593cc88f9af340b",  # 14 V2 pair
        "0000000000000000000000000000000000000000000000000de0b6b3a7640000",  # 15
        "0000000000000000000000000000000000000000000000000000000000000000",  # 16
        "0000000000000000000000000000000000000000000000000000000000000000",  # 17
    ]
)

#: BNB/USD used everywhere below. A fixed number, so a price assertion is about this
#: module's arithmetic and never about what BNB happened to cost while the suite ran.
BNB_USD = Decimal("900")


def amounts_out_return(*amounts: int) -> str:
    """The ABI shape ``getAmountsOut`` actually returned: offset, length, then amounts."""
    words = [0x20, len(amounts), *amounts]
    return "0x" + "".join(format(w, "064x") for w in words)


class _Rpc:
    """An :data:`~kaiba.execution.evm_price.RpcBatch` double keyed on exact calldata.

    ``None`` is what a revert looks like coming out of ``json_rpc_batch``, so a calldata
    that is not in the table is a revert - which is the refusal path, and therefore the
    default.
    """

    def __init__(self, table: dict[tuple[str, str], str], *, raises: bool = False) -> None:
        self.table = {(to.lower(), data.lower()): v for (to, data), v in table.items()}
        self.raises = raises
        self.batches: list[list[tuple[str, str]]] = []

    def __call__(self, calls):
        calls = list(calls)
        self.batches.append(calls)
        if self.raises:
            raise RuntimeError("bsc-dataseed said no")
        return [self.table.get((to.lower(), data.lower())) for to, data in calls]

    @property
    def call_count(self) -> int:
        return sum(len(b) for b in self.batches)


def _probe(token: str, path: list[str], amount: int) -> tuple[str, str]:
    return ep.encode_amounts_out(amount, path)


def native_table() -> dict[tuple[str, str], str]:
    """Everything the chain answered for the WBNB-paired graduated token."""
    path = [ep.BSC_WRAPPED_NATIVE, NATIVE_TOKEN]
    return {
        (NATIVE_TOKEN, ep.SEL_DECIMALS): DECIMALS_18,
        _probe(NATIVE_TOKEN, path, ep.ROUTER_PROBE_WEI): amounts_out_return(
            ep.ROUTER_PROBE_WEI, NATIVE_PROBE_OUT
        ),
        _probe(NATIVE_TOKEN, path, ep.ROUTER_REFERENCE_WEI): amounts_out_return(
            ep.ROUTER_REFERENCE_WEI, NATIVE_REF_OUT
        ),
    }


def erc20_table() -> dict[tuple[str, str], str]:
    """Everything the chain answered for the ERC-20-quoted graduated token.

    The DIRECT ``[WBNB, token]`` path is absent on purpose: it reverted on the live chain.
    """
    hop = [ep.BSC_WRAPPED_NATIVE, ERC20_QUOTE, ERC20_TOKEN]
    return {
        (ERC20_TOKEN, ep.SEL_DECIMALS): DECIMALS_18,
        (
            ep.FLAP_PORTAL,
            ep.SEL_GET_TOKEN_V8_SAFE + ERC20_TOKEN[2:].rjust(64, "0"),
        ): ERC20_PORTAL_RECORD,
        _probe(ERC20_TOKEN, hop, ep.ROUTER_PROBE_WEI): amounts_out_return(
            ep.ROUTER_PROBE_WEI, 0, ERC20_PROBE_OUT
        ),
        _probe(ERC20_TOKEN, hop, ep.ROUTER_REFERENCE_WEI): amounts_out_return(
            ep.ROUTER_REFERENCE_WEI, 0, ERC20_REF_OUT
        ),
    }


def source(table=None, *, raises: bool = False, usd=BNB_USD, **kw):
    """The layer under test, with a fixed BNB/USD leg and no network."""
    rpc = _Rpc(table or {}, raises=raises)
    quote_usd = ep.QuoteUsd(price_usd=lambda chain, token: usd)
    return ep.PancakeRouterPriceSource(transport=rpc, quote_usd=quote_usd, **kw), rpc


@pytest.fixture(autouse=True)
def _clear_caches():
    """The quote-token and decimals caches are module-level and immutable-by-design."""
    ep._DECIMALS.clear()
    ep._FLAP_QUOTE_TOKEN.clear()
    yield
    ep._DECIMALS.clear()
    ep._FLAP_QUOTE_TOKEN.clear()


# ======================================================================================
# 1. REFUSALS. A wrong price on a stop is worse than no price.
# ======================================================================================


def test_a_token_with_no_route_is_refused_rather_than_priced() -> None:
    """THE property. Every path reverts, so there is nothing to price and it says so.

    This is the mutation target: a layer that answered anything here would be strictly
    worse than the blindness it replaces.
    """
    src, rpc = source({(ON_CURVE_TOKEN, ep.SEL_DECIMALS): DECIMALS_18})
    got = src.quote(Chain.BSC, ON_CURVE_TOKEN)
    assert isinstance(got, wd.PriceQuote)
    assert not got.usable
    assert got.price_usd is None
    assert got.basis is EvidenceBasis.UNAVAILABLE
    assert "no_route" in (got.note or ""), got.note
    assert rpc.call_count > 0, "it must actually have asked before refusing"


def test_the_refusal_names_every_path_it_tried() -> None:
    """An operator reading ``protection_blind`` needs to know what was asked, not just no."""
    table = {
        (ERC20_TOKEN, ep.SEL_DECIMALS): DECIMALS_18,
        (
            ep.FLAP_PORTAL,
            ep.SEL_GET_TOKEN_V8_SAFE + ERC20_TOKEN[2:].rjust(64, "0"),
        ): ERC20_PORTAL_RECORD,
    }
    src, _ = source(table)
    got = src.quote(Chain.BSC, ERC20_TOKEN)
    assert not got.usable
    note = got.note or ""
    assert ep.BSC_WRAPPED_NATIVE[:10] in note or "wbnb" in note.lower(), note
    assert ERC20_QUOTE[:10] in note, f"the quote-token hop is not named: {note}"


def test_a_route_that_returns_zero_tokens_is_refused() -> None:
    """A pair that exists but hands back nothing is not a price."""
    path = [ep.BSC_WRAPPED_NATIVE, NATIVE_TOKEN]
    src, _ = source(
        {
            (NATIVE_TOKEN, ep.SEL_DECIMALS): DECIMALS_18,
            _probe(NATIVE_TOKEN, path, ep.ROUTER_PROBE_WEI): amounts_out_return(
                ep.ROUTER_PROBE_WEI, 0
            ),
            _probe(NATIVE_TOKEN, path, ep.ROUTER_REFERENCE_WEI): amounts_out_return(
                ep.ROUTER_REFERENCE_WEI, NATIVE_REF_OUT
            ),
        }
    )
    got = src.quote(Chain.BSC, NATIVE_TOKEN)
    assert not got.usable and "nothing" in (got.note or ""), got.note


def test_a_reference_probe_that_fails_is_a_refusal_because_impact_is_unmeasurable() -> None:
    """Without the small probe there is no impact figure, and impact is the thin-pool guard.

    The reference probe rides in the SAME batch, so it costs no extra round trip and no
    extra limiter reservation - there is no efficiency argument for guessing instead.
    """
    path = [ep.BSC_WRAPPED_NATIVE, NATIVE_TOKEN]
    src, _ = source(
        {
            (NATIVE_TOKEN, ep.SEL_DECIMALS): DECIMALS_18,
            _probe(NATIVE_TOKEN, path, ep.ROUTER_PROBE_WEI): amounts_out_return(
                ep.ROUTER_PROBE_WEI, NATIVE_PROBE_OUT
            ),
        }
    )
    got = src.quote(Chain.BSC, NATIVE_TOKEN)
    assert not got.usable and "reference" in (got.note or ""), got.note


def test_a_probe_that_moves_the_pool_past_the_impact_cap_is_refused() -> None:
    """A pool a $4 probe moves 20% is not a market, and a stop priced off it is fiction."""
    path = [ep.BSC_WRAPPED_NATIVE, NATIVE_TOKEN]
    # half the tokens out for 5000x the money in -> an enormous impact
    src, _ = source(
        {
            (NATIVE_TOKEN, ep.SEL_DECIMALS): DECIMALS_18,
            _probe(NATIVE_TOKEN, path, ep.ROUTER_PROBE_WEI): amounts_out_return(
                ep.ROUTER_PROBE_WEI, NATIVE_REF_OUT * 2
            ),
            _probe(NATIVE_TOKEN, path, ep.ROUTER_REFERENCE_WEI): amounts_out_return(
                ep.ROUTER_REFERENCE_WEI, NATIVE_REF_OUT
            ),
        }
    )
    got = src.quote(Chain.BSC, NATIVE_TOKEN)
    assert not got.usable and "impact" in (got.note or ""), got.note


def test_the_impact_cap_leaves_the_measured_pools_a_wide_margin() -> None:
    """MEASURED worst impact across the five live graduated pools: 13.5 bps at this probe."""
    assert ep.ROUTER_MAX_PROBE_IMPACT_BPS >= 20 * 14, "no headroom over the measured 13.5 bps"
    assert ep.ROUTER_MAX_PROBE_IMPACT_BPS < 3000, (
        "a distortion the size of the shipped stop cannot evaluate that stop"
    )


@pytest.mark.parametrize("chain", [Chain.SOL, Chain.ROBINHOOD, Chain.ETH])
def test_a_chain_that_is_not_bsc_is_refused_without_a_call(chain) -> None:
    """Pancake V2 at this address is BSC. Everything else is a refusal, not a guess."""
    src, rpc = source(native_table())
    got = src.quote(chain, NATIVE_TOKEN)
    assert not got.usable and chain.value in (got.note or ""), got.note
    assert rpc.call_count == 0, "a chain it cannot route must not cost a round trip"


@pytest.mark.parametrize(
    "token", ["", "   ", "not-an-address", "0x1234", "FnkzzU3t55RQNbjc6Jynn7LHrPEbRTnBenvHQwJCebYB"]
)
def test_a_token_that_is_not_an_evm_address_is_refused_without_a_call(token) -> None:
    src, rpc = source(native_table())
    got = src.quote(Chain.BSC, token)
    assert not got.usable and rpc.call_count == 0


def test_a_dead_rpc_is_a_refusal_not_a_crash() -> None:
    """``PriceSource`` implementations must never raise, and this one is asked when the
    other two layers already failed - the worst possible moment to take the tick down."""
    src, _ = source(native_table(), raises=True)
    got = src.quote(Chain.BSC, NATIVE_TOKEN)
    assert not got.usable and "RuntimeError" in (got.note or ""), got.note


def test_an_rpc_that_answers_short_is_a_refusal() -> None:
    """A transport that returns fewer results than calls must not be index-matched blindly."""

    class Short:
        def __call__(self, calls):
            return []

    src = ep.PancakeRouterPriceSource(
        transport=Short(), quote_usd=ep.QuoteUsd(price_usd=lambda c, t: BNB_USD)
    )
    got = src.quote(Chain.BSC, NATIVE_TOKEN)
    assert not got.usable and "short" in (got.note or ""), got.note


def test_unreadable_token_decimals_is_a_refusal() -> None:
    """Decimals scale the price. A wrong scale is a mark out by orders of magnitude."""
    table = native_table()
    table.pop((NATIVE_TOKEN.lower(), ep.SEL_DECIMALS), None)
    table.pop((NATIVE_TOKEN, ep.SEL_DECIMALS), None)
    src, _ = source(table)
    got = src.quote(Chain.BSC, NATIVE_TOKEN)
    assert not got.usable and "decimals" in (got.note or ""), got.note


def test_no_bnb_usd_rate_is_a_refusal_not_a_bnb_denominated_number() -> None:
    """The fail-closed half. The route is exact; without the FX leg there is no USD price."""
    src, _ = source(native_table(), usd=None)
    got = src.quote(Chain.BSC, NATIVE_TOKEN)
    assert not got.usable
    assert "USD" in (got.note or "") or "usd" in (got.note or ""), got.note


def test_a_quote_of_zero_or_less_is_never_reported_as_a_price() -> None:
    src, _ = source(native_table(), usd=Decimal("0"))
    got = src.quote(Chain.BSC, NATIVE_TOKEN)
    assert not got.usable


# ======================================================================================
# 2. the pure arithmetic and the encoding, pinned against what the chain answered
# ======================================================================================


def test_get_amounts_out_is_encoded_the_way_the_chain_accepted_it() -> None:
    """MEASURED: this exact calldata returned 200 with a decodable result."""
    to, data = ep.encode_amounts_out(
        ep.ROUTER_PROBE_WEI, [ep.BSC_WRAPPED_NATIVE, NATIVE_TOKEN]
    )
    # The literal, not ``ep.PANCAKE_V2_ROUTER``: a fixture built from the constant agrees
    # with the constant whatever the constant says, which is not a test of the address.
    assert to == "0x10ed43c718714eb63d5aa57b78b54704e256024e", (
        "this is the PancakeSwap V2 Router02 that answered all five live probes"
    )
    assert data == (
        "0xd06ca61f"
        "0000000000000000000000000000000000000000000000000011c37937e08000"
        "0000000000000000000000000000000000000000000000000000000000000040"
        "0000000000000000000000000000000000000000000000000000000000000002"
        "000000000000000000000000bb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
        "00000000000000000000000064a6c4c1c558be705c66ee60cd4ad5f2cc2c7777"
    )


def test_a_three_hop_path_encodes_its_array_length_and_every_hop() -> None:
    _, data = ep.encode_amounts_out(
        ep.ROUTER_PROBE_WEI, [ep.BSC_WRAPPED_NATIVE, ERC20_QUOTE, ERC20_TOKEN]
    )
    assert data.count("0" * 24 + "bb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c") == 1
    assert ERC20_QUOTE[2:] in data and ERC20_TOKEN[2:] in data
    assert format(3, "064x") in data, "the array length must say three hops"


def test_decoding_takes_the_last_hop_not_the_first() -> None:
    """The array is ``[amountIn, ...intermediates, amountOut]``; the token is at the end."""
    raw = amounts_out_return(ep.ROUTER_PROBE_WEI, 777, ERC20_PROBE_OUT)
    assert ep.decode_amounts_out(raw, hops=2) == ERC20_PROBE_OUT
    assert ep.decode_amounts_out(amounts_out_return(1, 2), hops=1) == 2


@pytest.mark.parametrize(
    "raw",
    [None, "", "0x", "not hex", "0x1234", amounts_out_return(1), "0x" + "00" * 96],
)
def test_decoding_a_reply_that_is_not_an_amounts_array_is_none(raw) -> None:
    """``None`` is a refusal upstream. Nothing here may invent a hop that was not returned."""
    assert ep.decode_amounts_out(raw, hops=1) is None


def test_decoding_refuses_an_array_whose_length_does_not_match_the_path() -> None:
    """A two-hop answer to a three-hop question means the call was not the one we sent."""
    assert ep.decode_amounts_out(amounts_out_return(1, 2), hops=2) is None


def test_decoding_refuses_a_reply_that_is_long_enough_but_declares_the_wrong_length() -> None:
    """The word count alone is not the check. A blob can carry enough words and still be
    an answer to a different question - trailing data, a struct, a padded revert reason.
    Reading the last word of it would be taking a number because it was there."""
    words = [0x20, 2, 1, 2, ERC20_PROBE_OUT]  # says two amounts, carries three
    raw = "0x" + "".join(format(w, "064x") for w in words)
    assert ep.decode_amounts_out(raw, hops=2) is None


def test_decoding_refuses_a_reply_whose_head_is_not_an_array_offset() -> None:
    """``0x20`` is where a single dynamic return puts its tail. Anything else is not one."""
    words = [0x40, 2, 1, 2]
    raw = "0x" + "".join(format(w, "064x") for w in words)
    assert ep.decode_amounts_out(raw, hops=1) is None


def test_router_price_is_native_per_whole_token_at_the_tokens_own_decimals() -> None:
    """MEASURED on the live pool: 5e15 wei bought 8646734071833394734771 atoms."""
    price, impact, reason = ep.router_price(
        NATIVE_PROBE_OUT, NATIVE_REF_OUT, token_decimals=18
    )
    assert reason == "ok"
    assert price is not None
    # 5e15 wei / 8646734071833394734771 atoms, both sides 18 decimals.
    assert price.quantize(Decimal("1E-13")) == Decimal("5.782530E-7")
    assert impact == 1, f"MEASURED impact at this probe is 1.14 bps, got {impact}"


def test_a_token_with_six_decimals_is_not_priced_as_if_it_had_eighteen() -> None:
    six, _, _ = ep.router_price(NATIVE_PROBE_OUT, NATIVE_REF_OUT, token_decimals=6)
    eighteen, _, _ = ep.router_price(NATIVE_PROBE_OUT, NATIVE_REF_OUT, token_decimals=18)
    assert six is not None and eighteen is not None
    assert six == eighteen / (Decimal(10) ** 12)


@pytest.mark.parametrize("decimals", [-1, 37, 999])
def test_impossible_decimals_are_refused(decimals) -> None:
    price, _, reason = ep.router_price(NATIVE_PROBE_OUT, NATIVE_REF_OUT, token_decimals=decimals)
    assert price is None and reason != "ok"


@pytest.mark.parametrize("out", [None, 0, -5])
def test_router_price_refuses_a_missing_or_empty_probe(out) -> None:
    price, _, reason = ep.router_price(out, NATIVE_REF_OUT, token_decimals=18)
    assert price is None and reason != "ok"


@pytest.mark.parametrize("ref", [None, 0, -5])
def test_router_price_refuses_a_missing_or_empty_reference(ref) -> None:
    price, _, reason = ep.router_price(NATIVE_PROBE_OUT, ref, token_decimals=18)
    assert price is None and reason != "ok"


def test_a_negative_impact_that_large_is_as_wrong_as_a_positive_one() -> None:
    """A bigger probe cannot get a better price on a constant product. If it did, the two
    answers did not come from one pool, and neither of them may become a stop."""
    price, _, reason = ep.router_price(
        NATIVE_REF_OUT * 100_000, NATIVE_REF_OUT, token_decimals=18
    )
    assert price is None and "impact" in reason, reason


# ======================================================================================
# 3. it prices the thing that was blind
# ======================================================================================


def test_a_graduated_flap_token_prices_through_its_wbnb_pair() -> None:
    """The live shape: ``read_flap`` said ``status=4``, and this layer prices it anyway."""
    src, rpc = source(native_table())
    got = src.quote(Chain.BSC, NATIVE_TOKEN)
    assert got.usable, got.note
    assert got.basis is EvidenceBasis.DERIVED
    assert got.source.startswith("pancake-v2"), got.source
    # 5.78253e-7 BNB/token * $900
    assert got.price_usd.quantize(Decimal("1E-7")) == Decimal("0.0005204")
    assert "impact" in (got.note or ""), got.note
    assert len(rpc.batches) == 1, "the WBNB pair must cost ONE round trip"


def test_a_graduated_flap_token_quoted_in_an_erc20_prices_through_its_own_quote_token() -> None:
    """Word 9 of the venue's record is what finds a route no fixed intermediate list would.

    MEASURED: 2 of the 5 live graduated Flap tokens pair against a quote token that is
    neither WBNB nor USDT, and the direct ``[WBNB, token]`` path reverts for both.
    """
    src, rpc = source(erc20_table())
    got = src.quote(Chain.BSC, ERC20_TOKEN)
    assert got.usable, got.note
    assert ERC20_QUOTE[:10] in got.source, got.source
    assert got.price_usd.quantize(Decimal("1E-7")) == Decimal("0.0002187")
    sent = [data for batch in rpc.batches for _, data in batch]
    assert any(ep.SEL_GET_TOKEN_V8_SAFE in d for d in sent), "the portal must supply the hop"


def test_the_quote_token_is_remembered_so_the_warm_tick_is_one_batch() -> None:
    """The watchdog ticks every 5 s per position. A cold path every tick is a cold path."""
    src, rpc = source(erc20_table())
    assert src.quote(Chain.BSC, ERC20_TOKEN).usable
    cold = len(rpc.batches)
    rpc.batches.clear()
    assert src.quote(Chain.BSC, ERC20_TOKEN).usable
    assert len(rpc.batches) == 1, f"warm tick took {len(rpc.batches)} round trips"
    assert cold >= 1
    sent = [data for batch in rpc.batches for _, data in batch]
    assert not any(ep.SEL_GET_TOKEN_V8_SAFE in d for d in sent), "the portal was re-read"
    assert not any(d == ep.SEL_DECIMALS for d in sent), "decimals were re-read"


def test_a_portal_read_that_FAILED_is_not_remembered_as_no_hop() -> None:
    """One 429 must not make a position permanently unroutable.

    A revert and a failed read arrive identically as ``None``, so the cache is written
    only when the portal actually answered. Caching the failure would turn a transient
    endpoint problem into "this token has no quote-token hop", for the life of the
    process, for exactly the tokens that need the hop.
    """
    table = erc20_table()
    portal_key = (ep.FLAP_PORTAL, ep.SEL_GET_TOKEN_V8_SAFE + ERC20_TOKEN[2:].rjust(64, "0"))
    del table[portal_key]
    src, rpc = source(table)
    assert not src.quote(Chain.BSC, ERC20_TOKEN).usable, "the premise: the hop is unknown"
    assert ERC20_TOKEN not in ep._FLAP_QUOTE_TOKEN, "a failed portal read was cached"

    # the endpoint comes back; the very next tick must ask again, and then it prices
    rpc.table[(portal_key[0].lower(), portal_key[1].lower())] = ERC20_PORTAL_RECORD
    rpc.batches.clear()
    got = src.quote(Chain.BSC, ERC20_TOKEN)
    sent = [data for batch in rpc.batches for _, data in batch]
    assert any(ep.SEL_GET_TOKEN_V8_SAFE in d for d in sent), "the portal was never re-read"
    assert got.usable, got.note


def test_a_token_flap_has_no_record_of_is_remembered_as_having_no_hop() -> None:
    """The other half: an ANSWER of "no record" is a fact and is worth caching.

    Only a failure is not. A non-Flap BSC token degrades to "the direct WBNB pair or
    nothing", and must not re-read the portal every five seconds to learn that again.
    """
    table = native_table()
    table[
        (ep.FLAP_PORTAL, ep.SEL_GET_TOKEN_V8_SAFE + NATIVE_TOKEN[2:].rjust(64, "0"))
    ] = "0x"
    src, rpc = source(table)
    assert src.quote(Chain.BSC, NATIVE_TOKEN).usable
    assert ep._FLAP_QUOTE_TOKEN[NATIVE_TOKEN] is None
    rpc.batches.clear()
    assert src.quote(Chain.BSC, NATIVE_TOKEN).usable
    sent = [data for batch in rpc.batches for _, data in batch]
    assert not any(ep.SEL_GET_TOKEN_V8_SAFE in d for d in sent)


def test_the_direct_pair_is_tried_first_and_the_hop_is_never_sent_when_it_works() -> None:
    """Three of five live graduated tokens pair with WBNB directly; they must not pay for
    a hop probe they do not need."""
    table = native_table()
    table[
        (ep.FLAP_PORTAL, ep.SEL_GET_TOKEN_V8_SAFE + NATIVE_TOKEN[2:].rjust(64, "0"))
    ] = ERC20_PORTAL_RECORD
    src, rpc = source(table)
    assert src.quote(Chain.BSC, NATIVE_TOKEN).usable
    sent = [data for batch in rpc.batches for _, data in batch]
    assert not any(ERC20_QUOTE[2:] in d for d in sent), "a hop was probed although direct worked"


def test_liquidity_is_not_reported_because_this_layer_did_not_measure_it() -> None:
    """The Solana last resort drops the liquidity round trip for the same reason."""
    src, _ = source(native_table())
    assert src.quote(Chain.BSC, NATIVE_TOKEN).liquidity_usd is None


def test_executable_quote_usd_is_not_reported_because_the_probe_is_a_buy() -> None:
    """``protection.py``: it is "the per-unit price an actual SELL quote would fill at,
    not a notional". Putting the probe's notional there would feed the anti-wick rule a
    number that is not the thing it compares against ``price_usd``."""
    src, _ = source(native_table())
    assert src.quote(Chain.BSC, NATIVE_TOKEN).executable_quote_usd is None


def test_the_note_says_what_size_the_price_is_executable_at() -> None:
    src, _ = source(native_table())
    note = src.quote(Chain.BSC, NATIVE_TOKEN).note or ""
    assert str(ep.ROUTER_PROBE_WEI) in note, note


# ======================================================================================
# 3b. the BNB/USD leg, which is where this layer first failed for real
# ======================================================================================


def test_the_bnb_usd_rate_is_shared_across_instances_not_refetched_per_worker() -> None:
    """MEASURED 2026-09-22: a per-instance cache cost 1-2 of 5 live routes their price.

    ``_prefetch_quotes`` builds a fresh source per worker per tick, so a per-instance
    :class:`QuoteUsd` is cold every time: N positions means N identical BNB/USD lookups a
    tick against the same ``dexscreener`` budget the position quotes are spending. The
    route was found and then discarded with ``router priced via wbnb but no USD rate``.
    """
    assert ep.PancakeRouterPriceSource().quote_usd is ep._BSC_NATIVE_USD
    assert ep.PancakeRouterPriceSource().quote_usd is ep.PancakeRouterPriceSource().quote_usd


def test_the_shared_rate_still_expires_into_blindness_not_into_a_stale_number() -> None:
    """Sharing a cache must not buy reach at the cost of the hard expiry.

    That expiry is the whole reason :class:`QuoteUsd` exists rather than a plain memo.
    """
    assert ep._BSC_NATIVE_USD.max_age_s == ep.QUOTE_USD_MAX_AGE_S
    assert ep._BSC_NATIVE_USD.ttl_s == ep.QUOTE_USD_TTL_S

    clock = [1000.0]
    calls: list[int] = []

    def flaky(chain, token):
        calls.append(1)
        return Decimal("900") if len(calls) == 1 else None

    cache = ep.QuoteUsd(price_usd=flaky, clock=lambda: clock[0])
    assert cache.usd(Chain.BSC, None)[0] == Decimal("900")
    clock[0] += ep.QUOTE_USD_MAX_AGE_S + 1
    assert cache.usd(Chain.BSC, None)[0] is None, "a dead provider must expire into blindness"


def test_a_worker_that_gets_its_own_source_still_shares_the_rate(monkeypatch) -> None:
    """The prefetch factory shape: two independently built sources, one lookup."""
    calls: list[tuple] = []

    def counted(chain, token):
        calls.append((chain, token))
        return BNB_USD

    monkeypatch.setattr(ep, "_BSC_NATIVE_USD", ep.QuoteUsd(price_usd=counted))
    for _ in range(4):
        src = ep.PancakeRouterPriceSource(transport=_Rpc(native_table()))
        assert src.quote(Chain.BSC, NATIVE_TOKEN).usable
    assert len(calls) == 1, f"the BNB/USD rate was fetched {len(calls)} times for 4 workers"


# ======================================================================================
# 4. the wiring: curve -> DexScreener -> router, and nothing else moves
# ======================================================================================


def bsc_route(_tmp_db):
    return ep.venue_price_source().routes[Chain.BSC]


def test_the_bsc_route_has_a_chain_behind_the_curve_not_one_source(tmp_db) -> None:
    """The bug: ``EvmVenuePriceSource`` had exactly one fallback and it was DexScreener."""
    route = bsc_route(tmp_db)
    assert isinstance(route, ep.EvmVenuePriceSource)
    chained = route.fallback
    assert isinstance(chained, wd.FallbackPriceSource), (
        "BSC still has a single fallback; a graduated token is blind again"
    )
    names = [getattr(s, "name", type(s).__name__) for s in chained.sources]
    assert names[0] == "prices", f"the DEX stack must stay first behind the curve: {names}"
    assert names[-1] == "pancake-v2", f"the router must be LAST, not preferred: {names}"


def test_the_bsc_last_resort_is_built_with_a_bounded_slot_wait(tmp_db) -> None:
    """An unbounded wait is how a fallback becomes a protection overrun on every chain."""
    router = bsc_route(tmp_db).fallback.sources[-1]
    assert router.wait_for_slot_s == ep.ROUTER_LAST_RESORT_WAIT_FOR_SLOT_S
    assert 0 < router.wait_for_slot_s <= 2.5, "measured budget is 5000 ms a tick"


def test_the_bsc_last_resort_reads_at_exit_priority(tmp_db) -> None:
    assert bsc_route(tmp_db).fallback.sources[-1].priority is Priority.EXIT


def test_the_robinhood_route_is_untouched(tmp_db) -> None:
    """Pons is a different venue with a different graduation shape. Nothing here is its fix."""
    route = ep.venue_price_source().routes[Chain.ROBINHOOD]
    assert isinstance(route, ep.EvmVenuePriceSource)
    assert not isinstance(route.fallback, wd.FallbackPriceSource), (
        "Robinhood was given a BSC-only router it can never use"
    )


def test_the_solana_route_still_ends_in_jupiter(tmp_db) -> None:
    """``tests/test_watchdog_router_fallback.py`` owns this; asserted here so a BSC change
    that broke it fails in the BSC file too."""
    sol = ep.venue_price_source().routes[Chain.SOL]
    names = [getattr(s, "name", type(s).__name__) for s in sol.fallback.sources]
    assert names[-1] == "jupiter", names


def test_venue_still_resolves_to_a_real_source(tmp_db) -> None:
    assert not isinstance(wd.resolve_price_source("venue"), wd.NullPriceSource)


# ======================================================================================
# 5. the incident, end to end through the composed source
# ======================================================================================


class _Refusing:
    def __init__(self, name: str, note: str) -> None:
        self.name, self._note = name, note

    def quote(self, chain, token):
        return wd.PriceQuote.unavailable(self._note, self.name)


def composed(rpc, *, dex_note="provider returned no price"):
    """``EvmVenuePriceSource`` wired exactly as ``venue_price_source`` wires BSC."""
    router = ep.PancakeRouterPriceSource(
        transport=rpc, quote_usd=ep.QuoteUsd(price_usd=lambda c, t: BNB_USD)
    )
    return ep.EvmVenuePriceSource(
        None,
        readers={Chain.BSC: lambda token, transport: (None, "flap_not_on_curve:status=4")},
        transports={Chain.BSC: rpc},
        fallback=wd.FallbackPriceSource(
            _Refusing("prices", dex_note), router, name="bsc-after-curve"
        ),
    )


def test_the_incident_shape_a_graduated_token_the_curve_refuses_now_prices() -> None:
    """``flap_not_on_curve:status=4`` plus a DexScreener that cannot help - and a price."""
    rpc = _Rpc(native_table())
    got = composed(rpc).quote(Chain.BSC, NATIVE_TOKEN)
    assert got.usable, got.note
    assert got.source.startswith("pancake-v2"), got.source


def test_when_all_three_layers_refuse_the_reason_names_all_three() -> None:
    """``Watchdog._blind`` puts this straight into ``protection_blind.reason``."""
    rpc = _Rpc({(ON_CURVE_TOKEN, ep.SEL_DECIMALS): DECIMALS_18})
    got = composed(rpc).quote(Chain.BSC, ON_CURVE_TOKEN)
    assert not got.usable
    note = got.note or ""
    assert "flap_not_on_curve:status=4" in note, note
    assert "provider returned no price" in note, note
    assert "pancake-v2" in note, note


def test_a_curve_that_still_prices_never_reaches_the_router() -> None:
    """First usable wins. A pre-graduation position must not pay for a router call."""
    rpc = _Rpc(native_table())
    router = ep.PancakeRouterPriceSource(
        transport=rpc, quote_usd=ep.QuoteUsd(price_usd=lambda c, t: BNB_USD)
    )
    priced = ep.VenuePrice(
        price_quote_per_token=Decimal("0.000001"),
        quote_token=None,
        quote_reserve_base=10**18,
        quote_decimals=18,
        venue="flap",
        observed_ms=wd.now_ms(),
        note="flap curve",
    )
    src = ep.EvmVenuePriceSource(
        None,
        readers={Chain.BSC: lambda token, transport: (priced, "ok")},
        transports={Chain.BSC: rpc},
        quote_usd=ep.QuoteUsd(price_usd=lambda c, t: BNB_USD),
        fallback=wd.FallbackPriceSource(_Refusing("prices", "no"), router),
    )
    got = src.quote(Chain.BSC, NATIVE_TOKEN)
    assert got.usable and "flap" in got.source
    assert rpc.call_count == 0, "the router was called although the curve priced"


# ======================================================================================
# 6. provenance: every number above is measured, derived, or labelled
# ======================================================================================

LABELS = ("MEASURED", "DEFINITIONAL", "INVENTED", "DERIVED", "STRUCTURAL", "UNVERIFIED")


@pytest.mark.parametrize(
    "name",
    [
        "PANCAKE_V2_ROUTER",
        "PANCAKE_V2_FACTORY",
        "BSC_WRAPPED_NATIVE",
        "SEL_GET_AMOUNTS_OUT",
        "ROUTER_PROBE_WEI",
        "ROUTER_REFERENCE_WEI",
        "ROUTER_MAX_PROBE_IMPACT_BPS",
    ],
)
def test_every_new_constant_says_where_its_value_came_from(name) -> None:
    lines = inspect.getsource(ep).splitlines()
    idx = next(i for i, line in enumerate(lines) if re.match(rf"^{name}\b", line))
    comment: list[str] = []
    for line in reversed(lines[:idx]):
        if line.startswith("#"):
            comment.append(line)
        else:
            break
    text = "\n".join(comment)
    assert comment, f"{name} has no provenance comment"
    assert any(word in text for word in LABELS), f"{name} is not labelled: {text[:160]}"


def test_the_wrapped_native_address_is_the_trees_constant_not_typed_from_memory() -> None:
    """One spelling of WBNB. A second one is how two modules price different tokens."""
    assert ep.BSC_WRAPPED_NATIVE == ep.NATIVE_USD_REFERENCE[Chain.BSC][1]

    from kaiba.providers.native_price import WRAPPED_NATIVE

    assert ep.BSC_WRAPPED_NATIVE == WRAPPED_NATIVE[Chain.BSC]


def test_the_probe_is_the_bnb_analogue_of_the_shipped_solana_probe() -> None:
    """``jupiter.PROBE_LAMPORTS`` is 0.02 SOL; this is 0.005 BNB, the same order of USD."""
    from kaiba.providers.jupiter import PROBE_LAMPORTS

    assert PROBE_LAMPORTS == 20_000_000
    assert ep.ROUTER_PROBE_WEI == 5 * 10**15
    assert ep.ROUTER_REFERENCE_WEI < ep.ROUTER_PROBE_WEI, (
        "the reference probe must be the SMALLER one or the impact figure is inverted"
    )
