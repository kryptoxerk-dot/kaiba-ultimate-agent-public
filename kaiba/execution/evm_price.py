"""EVM bonding-curve pricing: what a pre-graduation EVM token is actually worth.

This is :mod:`kaiba.execution.curve_price` for the EVM side, and it exists for the same
reason that module exists: **a DEX pair does not exist for a token still on its curve**, so
the whole DexScreener-shaped quote stack correctly answers "unknown" for exactly the
tokens we hold first. On Solana that hole was closed by pricing from pump.fun's reserves.
On EVM it was never closed at all, and the measurement is brutal:

    DexScreener priced **1 of 18** live ``robinhood`` tokens sampled on 2026-09-21, and
    the one it did price it priced at **$1 of liquidity**.

A position the watchdog cannot price is a position with no stop. So this module is not a
nice-to-have on the EVM lanes; it is the thing that has to exist before an EVM position is
allowed to exist. It reads the venue's own reserves over ``eth_call`` and hands over to the
existing DEX quote stack the moment the token graduates — one source, both sides of
graduation, exactly as :class:`~kaiba.execution.curve_price.CurvePriceSource` does.

Why a separate module rather than more of ``curve_price.py``
------------------------------------------------------------

``curve_price.py`` is pump.fun's arithmetic: lamports, virtual reserves, the protocol's
``+ 1`` rounding, a 279.9e12 reserved-token invariant, a 125 bps flat curve fee. None of
that is true of Flap or Pons, both of which have their own published geometry and their own
integer conventions. Grafting two more venues onto that file would have produced one class
with three incompatible meanings for "reserve". ``curve_price.py`` is therefore **not
touched by this change**; :func:`venue_price_source` composes the two.

The venues, and how each one is read
------------------------------------

**BSC / Flap** — one ``eth_call`` to the portal at :data:`FLAP_PORTAL`,
``getTokenV8Safe(address)`` (:data:`SEL_GET_TOKEN_V8_SAFE`). The 18-word record was decoded
against reality on 2026-09-21, not read off an ABI: see :func:`parse_flap_record` for the
word-by-word proof. The published curve is ``(x + h)(y + r) = K`` with ``x`` = tokens
*remaining*, ``y`` = quote raised, and the spot price is ``K / (x + h)^2``. The same
record carries the whole curve (:class:`FlapCurve`), so a caller that has to price a
*fill* rather than a unit — the sizing gate — gets it without a second read of a curve
that has moved in between.

**Robinhood / Pons** — the curve reads already implemented and proven in
:mod:`kaiba.ingest.robinhood` (:data:`~kaiba.ingest.robinhood.CURVE_READS`,
:func:`~kaiba.ingest.robinhood.parse_curve_state`). This module adds pricing on top of them
and deliberately does not re-derive the curve semantics: there must be one derivation in
the tree. Spot price is the reserve ratio ``quoteReserve / curveTokenBalance``.

On the "RPC returns 403 from the VPS" report
--------------------------------------------

It did not reproduce. ``https://rpc.mainnet.chain.robinhood.com`` answered
``eth_chainId`` -> ``0x1237`` with HTTP 200 on 2026-09-21 from **both** this workstation and
the live VPS (203.0.113.10), with a bare ``httpx`` User-Agent, with a browser User-Agent,
and with browser ``Origin``/``Referer`` headers — six probes, six 200s. So a browser
User-Agent is **not** required and none is sent here; an occasional 403 is treated as what
any provider failure is treated as, an ``UNAVAILABLE`` quote. If it becomes frequent the
evidence will be in ``provider_errors``, which is the right place to discover it rather
than a hardcoded header that hides it.

What is refused, and why refusing is the point
----------------------------------------------

Every number below is either read from the chain or derived from numbers read from the
chain, and **every derivation is cross-checked against a second, independent one before it
is allowed to become a price**:

* Flap's reported price word is checked against ``K / (x + h)^2`` recomputed here from the
  curve constants and the token's own ``totalSupply``/``decimals``. Measured agreement on
  4/4 live tokens: relative error at most **8.5e-11**.
* Flap's total supply is not assumed. It is *derived* from the curve identity
  ``K = r * (h + S)`` (exact on 4/4 live tokens) and required to equal
  ``totalSupply() / 10**decimals()``. A launch config this module has not seen fails that
  check and is refused rather than mispriced.
* Flap's reported quote reserve is checked against the integral of the curve over the sold
  range. Measured agreement: relative error at most **8.6e-11**.

When a cross-check fails, or a read fails, or the quote token cannot be converted to USD,
the answer is ``EvidenceBasis.UNAVAILABLE`` with a named reason. It is never a stale number
and never a reassuring default. **A stale price is strictly worse than no price**, because
blindness is now a monitored, budgeted state (``protection.max_blind_s``) that pages and
halts entries, while a wrong price is a stop evaluated against fiction and nothing fires.

Money is integers in base units (wei, atoms). USD is ``Decimal``. No value here passes
through ``float``.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Any

from kaiba.core.limiter import Priority
from kaiba.core.schemas import EVM_CHAINS, Chain, EvidenceBasis, looks_evm, now_ms

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------------------

ZERO_ADDRESS = "0x" + "0" * 40

#: ``decimals()`` and ``totalSupply()``. Same selectors ``dyor_verify`` uses.
SEL_DECIMALS = "0x313ce567"
SEL_TOTAL_SUPPLY = "0x18160ddd"

#: ``creatorTaxBps()`` on a Pons curve: the selector ``viability.PONS_CREATOR_TAX_SELECTOR`` reads
#: (keccak, verified there 2026-09-21 against three live curves). Not imported from viability, which
#: imports the engine; ``tests/test_paper_pons_curve_take.py`` pins the two equal.
PONS_CREATOR_TAX_SELECTOR = "0xc1bb8901"

#: Flap's portal on BSC. One contract for every Flap curve, unlike Pons which deploys a
#: curve contract per token — so there is nothing to resolve before the read.
FLAP_PORTAL = "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0"

#: ``getTokenV8Safe(address)``. Returns the whole launch record in one call.
SEL_GET_TOKEN_V8_SAFE = "0x62fafcca"

#: ``status`` (word 0) while the token is still on the curve. 4 was observed on graduated
#: tokens (both of the graduated samples had a PancakeSwap v2 pair and word 14 populated
#: with an address). Anything that is not :data:`FLAP_STATUS_ON_CURVE` is handed to the DEX
#: fallback rather than guessed at — an unknown status is not an invitation to price.
FLAP_STATUS_ON_CURVE = 1

#: The rest of Flap's documented ``TokenStatus`` (0 Invalid, 1 Tradable, 2 InDuel, 3 Killed,
#: 4 DEX, 5 Staged). MEASURED 2026-10-05 from the box: graduated samples read 4 with the pool
#: word set and progress at 100%. Named so a caller can tell "graduated" from "dead".
FLAP_STATUS_KILLED = 3
FLAP_STATUS_DEX = 4

#: ``maxBuyPerOrigin(address token)`` on the portal: ``(uint16 bps, uint256 maxBuyAmount)``,
#: ``(0, 0)`` when the token has no Buy Quota. Selector verified by keccak in this tree
#: (``policy.keccak256``) and MEASURED readable on 371 of 371 fresh launches 2026-10-05.
SEL_MAX_BUY_PER_ORIGIN = "0x4b02c8e9"

#: ``quoteExactInput((address inputToken, address outputToken, uint256 inputAmount))`` on the
#: portal: the tokens a buy of ``inputAmount`` would deliver, by the venue's own arithmetic --
#: fee, the token's buy tax and any Buy Quota cap included. MEASURED 2026-10-05: 0.15 BNB in
#: returned 25.88M / 25.37M / 23.82M tokens at 1% / 3% / 8.99% tax, and exactly the 20M quota
#: on a quota token. The tuple is all static types, so it is encoded inline (three words).
SEL_QUOTE_EXACT_INPUT = "0xfc847c2b"

#: Flap's protocol fee per side, bps. MEASURED 2026-10-05 on our own 8 on-curve GMGN fills:
#: the portal's ``TokenBought``/``TokenSold`` ``fee`` word is exactly 1% of its ``eth`` word.
FLAP_PROTOCOL_FEE_BPS = 100

#: Flap's fixed-point scale for ``r``, ``h``, ``K`` and the price word. It is 1e18 because
#: those are the curve's own units, **not** because the quote token has 18 decimals: the
#: reserve word is in quote base units and is scaled separately. Both readings were
#: confirmed on a token whose quote is native BNB and on two whose quote is an ERC-20.
FLAP_FIXED_POINT = Decimal(10) ** 18

#: The same 1e18 as an ``int``. The cross-checks are ratios and are done in ``Decimal``; a
#: *fill* is money and is done in integers (:class:`FlapCurve`), so it needs the integer
#: spelling. One number in two types rather than two numbers that can drift apart.
FLAP_WAD = 10**18

#: Decimals on both sides of a curve that :class:`FlapCurve`'s fill arithmetic assumes.
#:
#: LibCurve is WAD maths: it adds ``h`` (1e18-scaled) to a token **atom** count and ``r``
#: (1e18-scaled) to a quote **base-unit** count, and those two additions only line up when
#: both sides carry 18 decimals. A curve that pairs anything else is refused rather than
#: rescaled by a rule this module has never checked against a fill.
FLAP_CURVE_DECIMALS = 18

#: 99.3% of Flap token addresses end in ``7777`` (vanity-mined by the launchpad). That is a
#: free venue fingerprint and it is recorded on the quote as a note, but it is deliberately
#: **not** a gate: gating on it would silently skip the other 0.7%, and the portal read is
#: itself the authoritative answer to "is this a Flap token".
FLAP_ADDRESS_FINGERPRINT = "7777"

#: Every vanity suffix the Flap launchpad mines (``7777`` and ``8888``). Used for ONE thing:
#: a ``TokenNotFound`` answer for an address carrying it contradicts itself, so it refuses
#: rather than routing the token to a pool (:func:`read_flap`). Never a gate on its own.
FLAP_ADDRESS_FINGERPRINTS: tuple[str, ...] = ("7777", "8888")

#: ``TokenNotFound(address)`` -- the custom error the portal's ``getTokenV8Safe`` REVERTS
#: with for a token it never launched. Selector verified by keccak in this tree
#: (``policy.keccak256``). MEASURED 2026-10-05 from the box: all 18 non-Flap bsc tokens
#: that reached an sm-trenches decision in the prior 7 days (four.meme, PancakeSwap pools,
#: geniusfun, o1_rwa) answered ``{"code": 3, "message": "execution reverted", "data":
#: "0xde6137d1" + <the token, left-padded>}``; two live Flap tokens answered a record.
#: ``json_rpc_batch`` used to flatten that revert into ``None`` -- the same value a dead
#: endpoint produces -- so every non-Flap token read as ``flap_portal_read_failed`` and was
#: refused outright (24 of 161 bsc sm-trenches decisions in those 7 days).
SEL_FLAP_TOKEN_NOT_FOUND = "0xde6137d1"

#: How far the venue's own price word may sit from this module's independent recomputation
#: of ``K / (x + h)^2`` before the quote is refused.
#:
#: MEASURED: the worst relative error across the four live Flap curves sampled on
#: 2026-09-21 was 8.5e-11 (the contract truncates; this module does not). 1e-6 is four
#: orders of magnitude of slack on that, and any misread of the record's word order is off
#: by at least nine orders — the two failure modes do not overlap.
PRICE_CROSS_CHECK_TOLERANCE = Decimal("1e-6")

#: Same idea for the reserve word against the integral of the curve over the sold range.
#: MEASURED worst case on the same four curves: 8.6e-11.
RESERVE_CROSS_CHECK_TOLERANCE = Decimal("1e-6")

#: How far Pons' constant product may drift from ``phantomQuote * launchSupply``.
#:
#: MEASURED on 18 live on-curve tokens sampled from the VPS database on 2026-09-21: the
#: worst relative gap was **9.3e-10**, which matches the "below 1e-9" figure
#: ``kaiba/ingest/robinhood.py`` recorded independently. 1e-6 is three orders of magnitude
#: of slack on that.
#:
#: This check is not decoration — it is what catches a **graduated** Pons curve. See
#: :func:`pons_price` for why that matters and what the drained curve looks like.
PONS_INVARIANT_TOLERANCE = Decimal("1e-6")

#: Canonical WETH on Ethereum mainnet. Robinhood Chain's native token is ETH and ETH's
#: price is not chain-specific, so the deep mainnet pools price it — the same reasoning
#: and the same address as ``kaiba.ingest.robinhood.WETH_MAINNET``.
WETH_MAINNET = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"

#: Wrapped native per chain, for turning a native-quoted curve into USD. These are the
#: same addresses ``kaiba.providers.native_price.WRAPPED_NATIVE`` uses, and the value is
#: the ``(chain, address)`` the DEX stack should be asked for — which for Robinhood Chain
#: is mainnet ETH, not anything on chain 4663.
#: eth and base (design step 6a [R], 2026-10-23): both are ETH, priced off mainnet WETH like
#: robinhood. Before this a base or eth native quote read "no native USD reference".
NATIVE_USD_REFERENCE: dict[Chain, tuple[Chain, str]] = {
    Chain.BSC: (Chain.BSC, "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"),
    Chain.ROBINHOOD: (Chain.ETH, WETH_MAINNET),
    Chain.ETH: (Chain.ETH, WETH_MAINNET),
    Chain.BASE: (Chain.ETH, WETH_MAINNET),
}

#: How long a quote-token USD price may be reused without re-asking the provider.
QUOTE_USD_TTL_S = 60.0

#: How old a quote-token USD price may be and still be allowed to price a position.
#:
#: 300 s, taken from ``native_price.DEFAULT_TOLERANCE_MS`` and for its reason: the stop
#: this feeds sits 30% below entry, and a five-minute move in ETH or BNB is well under 1%
#: — noise against a 30% stop. **Past this age a failed refresh returns ``None``, not the
#: last value.** That is the difference between this cache and every cache that has ever
#: silenced an alarm: it expires into blindness, which is monitored, rather than into a
#: stale number, which is not.
QUOTE_USD_MAX_AGE_S = 300.0

#: Longest a single price read will wait for limiter capacity. Shorter than the watchdog's
#: ``protection.poll_interval_s`` of 5 s on purpose: a tick that cannot get capacity should
#: report blind for that tick and let the next one try, not stack up behind the one before
#: it. Blindness is budgeted and visible; a backlog of price reads is neither.
WAIT_FOR_SLOT_S = 3.0

#: Read timeout for one batched ``eth_call``. A watchdog tick that hangs is a watchdog tick
#: that did not happen.
RPC_TIMEOUT_S = 12.0

#: ``(to, data)`` pairs in, raw hex results out, ``None`` per call that failed. Injectable
#: so every parser and every refusal below is testable without a network.
RpcBatch = Callable[[Sequence[tuple[str, str]]], list[str | None]]


@dataclass(frozen=True, slots=True)
class RpcRevert:
    """An ``eth_call`` that the CONTRACT refused, with the revert data it gave.

    Only a transport built with ``json_rpc_batch(..., keep_reverts=True)`` produces one;
    every other transport still answers ``None`` for a revert, so no existing caller sees
    a new type. It is deliberately NOT a ``str``: every parser in this module treats a
    non-string result as unreadable, so a caller that did not ask for reverts and gets one
    anyway refuses rather than decoding revert data as a return value.

    The distinction it exists for: ``None`` means "we do not know" (timeout, 429, dead
    node) and a revert means "the contract answered no". Those want opposite handling.
    """

    data: str

    @property
    def selector(self) -> str:
        return self.data[:10].lower()

    def address_arg(self, index: int = 0) -> str | None:
        """The ``index``-th 32-byte argument of the error, as an address, or ``None``."""
        body = self.data[10:]
        start = 64 * int(index)
        word = body[start : start + 64]
        if len(word) != 64:
            return None
        try:
            return "0x" + format(int(word, 16) & ((1 << 160) - 1), "040x")
        except ValueError:
            return None


# --------------------------------------------------------------------------------------
# hex helpers
# --------------------------------------------------------------------------------------


def _hex_int(value: Any) -> int | None:
    """``0x…`` -> int, or ``None``. ``0x`` (an empty return) is *not* zero."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw.startswith("0x") or len(raw) < 3:
        return None
    try:
        return int(raw, 16)
    except ValueError:
        return None


def _words(value: Any) -> list[int]:
    """An ABI return blob -> its 32-byte words as ints. Partial trailing words are dropped."""
    if not isinstance(value, str) or not value.startswith("0x"):
        return []
    body = value[2:]
    out: list[int] = []
    for start in range(0, len(body) - 63, 64):
        try:
            out.append(int(body[start : start + 64], 16))
        except ValueError:
            return out
    return out


def _word_address(word: int | None) -> str | None:
    """An address word -> ``0x…40hex``. The zero address becomes ``None``: on both venues
    a zero quote token means *the chain's native coin*, not "no quote token"."""
    if word is None:
        return None
    addr = "0x" + format(word & ((1 << 160) - 1), "040x")
    return None if addr == ZERO_ADDRESS else addr


def _call(to: str, selector: str, arg_address: str | None = None) -> tuple[str, str]:
    data = selector
    if arg_address is not None:
        data += arg_address[2:].lower().rjust(64, "0")
    return (to, data)


def _relative_gap(a: Decimal, b: Decimal) -> Decimal | None:
    """``|a-b| / max(|a|,|b|)``, or ``None`` when both are zero (nothing to compare)."""
    scale = max(abs(a), abs(b))
    if scale == 0:
        return None
    try:
        return abs(a - b) / scale
    except (InvalidOperation, DivisionByZero):
        return None


# --------------------------------------------------------------------------------------
# what a venue read produces
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VenuePrice:
    """One venue's curve, priced in its own quote token. USD conversion happens later.

    Splitting it here is deliberate: the curve read is exact integer truth from the chain,
    the USD leg is a provider opinion with its own failure mode, and conflating them is how
    a missing FX rate turns into a missing price with no way to tell which leg was absent.
    """

    #: Whole quote-token units per whole token. ``Decimal`` because it is a ratio, not money.
    price_quote_per_token: Decimal
    #: ``None`` means the chain's native coin (a zero quote-token address on both venues).
    quote_token: str | None
    #: Quote actually raised, in quote **base units**. The exact maximum extractable
    #: amount, and the same deliberately-small convention ``CurvePriceSource`` uses.
    #: ``None`` when the venue's reserve word could not be corroborated — depth unknown is
    #: a legal answer, depth zero is a lie the rug monitor would act on.
    quote_reserve_base: int | None
    #: Decimals of the quote token, read from the chain — never assumed to be 18.
    quote_decimals: int
    venue: str
    observed_ms: int
    note: str
    progress_pct: Decimal | None = None
    #: The curve's own state, for callers that need to price a *fill* rather than a unit
    #: — sizing asks "what would 0.01 BNB get", which spot cannot answer. ``None`` on a
    #: venue with no fill arithmetic here, so a consumer must handle its absence.
    curve: FlapCurve | None = None
    pool_id: str | None = None
    #: A Pons curve's own per-leg rates, read in the same batch as its reserves (:func:`read_pons`):
    #: ``feeBps()`` and ``creatorTaxBps()``. ``None`` = not read (another venue, an older reader, or
    #: the item failed) -- unknown, never zero. The paper legs charge them (``paper_costs``).
    fee_bps: int | None = None
    creator_tax_bps: int | None = None
    #: A Pons curve's own state from the same read (:class:`PonsCurve`), so a paper leg is FILLED at
    #: our size on the curve's own arithmetic rather than approximated against its reserve. ``None`` on
    #: any other venue. Rides along like :attr:`curve` (Flap); prices nothing on its own. 2026-10-23 (hB_).
    pons_curve: PonsCurve | None = None

    @property
    def quote_reserve_whole(self) -> Decimal | None:
        if self.quote_reserve_base is None:
            return None
        return Decimal(self.quote_reserve_base) / (Decimal(10) ** self.quote_decimals)


#: ``(price | None, reason)`` — the refusal reason is always named, never empty.
VenueRead = tuple[VenuePrice | None, str]


# --------------------------------------------------------------------------------------
# BSC / Flap
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FlapRecord:
    """The words of ``getTokenV8Safe`` that this module has *established the meaning of*.

    Word-by-word, from four live curves read on 2026-09-21 (two native-quoted, one USDT,
    one an ERC-20 stock token) plus two graduated ones:

    ==== ============================== =========================================
    word meaning                        how it was established
    ==== ============================== =========================================
    0    status                         1 on all four on-curve, 4 on both graduated
    1    quote raised, base units       equals the curve integral to 8.6e-11
    2    tokens sold, atoms             ``totalSupply - balanceOf(portal)``, exact
    3    price, quote-per-token * 1e18  equals ``K/(x+h)^2`` to 8.5e-11 and equals
                                        DexScreener's ``priceNative`` on all four
    5    ``r`` * 1e18                   6.14 on two, matching the published BSC ``r``
    6    ``h`` * 1e18                   107,036,752, the published ``h``
    7    ``K`` * 1e18                   6,797,205,657.28, the published ``K``, and
                                        ``K == r*(h+S)`` exactly on 4/4
    8    graduation, tokens sold        8e26 atoms = 80% of supply, the documented
                                        graduation rule
    9    quote token (0 = native)       0 on the two DexScreener calls WBNB, and the
                                        USDT/SPCXB addresses on the other two
    ==== ============================== =========================================

    Words 4 and 10-17, ESTABLISHED 2026-10-05 (read from the box against Flap's documented
    ``TokenStateV8`` field order; the record is exactly 18 words on every sample):

    ==== ============================== =========================================
    4    token version                  6 (Tax V3) on 1,167 of 1,170 launches in an hour
    10   nativeToQuoteSwapEnabled       0 on an ERC-20 curve ``quoteExactInput``
                                        refused BNB for; 1 on one it accepted
    12   buy tax, bps                   899 / 300 / 100 / 0 on samples, equal to the
                                        launch's own ``FlapTokenAsymmetricTaxSet``
    13   sell tax, bps                  as word 12 (0/100 is the commonest graduate)
    14   pool                           zero on the curve; the PancakeSwap pair after
    15   progress, 1e18 = 100%          1e18 on both graduated samples
    16   lpFeeProfile, 17 dexId         read, not used
    ==== ============================== =========================================

    Those fields are ``None`` on a record shorter than 18 words, so a caller that needs the
    tax must handle its absence (a 10-word record still prices). Word 11 (extension id) is
    kept raw.
    """

    status: int
    quote_raised_base: int
    tokens_sold_atoms: int
    price_word: int
    r_scaled: int
    h_scaled: int
    k_scaled: int
    graduation_tokens_atoms: int
    quote_token: str | None
    token_version: int | None = None
    native_to_quote_swap_enabled: int | None = None
    extension_id: int | None = None
    buy_tax_bps: int | None = None
    sell_tax_bps: int | None = None
    pool: str | None = None
    progress_wad: int | None = None
    lp_fee_profile: int | None = None
    dex_id: int | None = None

    @property
    def on_curve(self) -> bool:
        return self.status == FLAP_STATUS_ON_CURVE


#: Index of each field above inside the returned record.
_FLAP_W_STATUS = 0
_FLAP_W_QUOTE_RAISED = 1
_FLAP_W_TOKENS_SOLD = 2
_FLAP_W_PRICE = 3
_FLAP_W_R = 5
_FLAP_W_H = 6
_FLAP_W_K = 7
_FLAP_W_GRADUATION = 8
_FLAP_W_QUOTE_TOKEN = 9
_FLAP_MIN_WORDS = 10
_FLAP_W_VERSION = 4
_FLAP_W_N2Q = 10
_FLAP_W_EXTENSION = 11
_FLAP_W_BUY_TAX = 12
_FLAP_W_SELL_TAX = 13
_FLAP_W_POOL = 14
_FLAP_W_PROGRESS = 15
_FLAP_W_LP_FEE = 16
_FLAP_W_DEX_ID = 17
_FLAP_FULL_WORDS = 18


def _tax_word(word: int) -> int | None:
    """A tax word in bps, or ``None`` when it cannot be one (over 100%)."""
    return word if 0 <= word <= 10_000 else None


def flap_record(words: Sequence[int]) -> FlapRecord | None:
    """The raw words -> :class:`FlapRecord`, or ``None`` when the record is too short.

    A short record means the portal answered something this module does not understand —
    a proxy upgrade to a ``V9`` layout, say. Short is refused rather than padded. The
    fields after word 9 are filled only from a full 18-word record.
    """
    if len(words) < _FLAP_MIN_WORDS:
        return None
    full = len(words) >= _FLAP_FULL_WORDS
    return FlapRecord(
        status=words[_FLAP_W_STATUS],
        quote_raised_base=words[_FLAP_W_QUOTE_RAISED],
        tokens_sold_atoms=words[_FLAP_W_TOKENS_SOLD],
        price_word=words[_FLAP_W_PRICE],
        r_scaled=words[_FLAP_W_R],
        h_scaled=words[_FLAP_W_H],
        k_scaled=words[_FLAP_W_K],
        graduation_tokens_atoms=words[_FLAP_W_GRADUATION],
        quote_token=_word_address(words[_FLAP_W_QUOTE_TOKEN]),
        token_version=words[_FLAP_W_VERSION],
        native_to_quote_swap_enabled=words[_FLAP_W_N2Q] if full else None,
        extension_id=words[_FLAP_W_EXTENSION] if full else None,
        buy_tax_bps=_tax_word(words[_FLAP_W_BUY_TAX]) if full else None,
        sell_tax_bps=_tax_word(words[_FLAP_W_SELL_TAX]) if full else None,
        pool=_word_address(words[_FLAP_W_POOL]) if full else None,
        progress_wad=words[_FLAP_W_PROGRESS] if full else None,
        lp_fee_profile=words[_FLAP_W_LP_FEE] if full else None,
        dex_id=words[_FLAP_W_DEX_ID] if full else None,
    )


@dataclass(frozen=True, slots=True)
class FlapBuyQuota:
    """``maxBuyPerOrigin(token)``: Flap's per-``tx.origin`` Buy Quota on one token.

    THE HAZARD. A buy over the quota does not revert: the portal fills up to the quota and
    REFUNDS the rest (Flap docs; MEASURED 2026-10-05: ``quoteExactInput`` of 0.15 BNB on a
    2% quota token returned exactly the 20,000,000-token cap, ~0.114 BNB of curve). Our
    executor assumes fill-or-revert, so a capped buy would book a size it never got and a
    refund it may never see. ``(0, 0)`` means no quota. MEASURED 2026-10-05: non-zero on 0 of
    371 fresh launches; ``FlapBuyQuotaUpdated`` on 12 tokens in 72 h, all ending ``8888``,
    all 2% of supply.
    """

    bps: int
    max_buy_atoms: int

    @property
    def active(self) -> bool:
        return self.bps > 0 or self.max_buy_atoms > 0


def parse_buy_quota(raw: Any) -> FlapBuyQuota | None:
    """A ``maxBuyPerOrigin`` return -> :class:`FlapBuyQuota`; ``None`` when unreadable.

    ``None`` is not "no quota": a caller deciding whether a buy can be capped must refuse
    on it.
    """
    words = _words(raw)
    if len(words) < 2:
        return None
    return FlapBuyQuota(bps=words[0], max_buy_atoms=words[1])


def quote_exact_input_call(token: str, size_base_units: int) -> tuple[str, str]:
    """``(portal, calldata)`` for ``quoteExactInput((native, token, size))`` -- a BNB buy."""
    data = (SEL_QUOTE_EXACT_INPUT + ZERO_ADDRESS[2:].rjust(64, "0")
            + token[2:].lower().rjust(64, "0") + format(int(size_base_units), "064x"))
    return FLAP_PORTAL, data


def flap_buy_net_quote(size_base_units: int, buy_tax_bps: int) -> int:
    """The part of a BNB buy that reaches the curve: the protocol fee and the token's buy
    tax both come off the INPUT.

    DERIVED 2026-10-05 from the venue's own ``quoteExactInput`` at three tax rates on a
    fresh native curve (r=6.14, h=107,036,752, K=6,797,205,657.28): 0.15 BNB at 1% / 3% /
    8.99% tax returned 25.88M / 25.37M / 23.82M tokens, and ``tokens_out`` of
    ``0.15 * (1 - (100 + tax)/10000)`` gives 25.884M / 25.368M / 23.820M. Taking the tax
    off the tokens instead gives 23.79M at 8.99% and does not fit. Three points, one curve:
    UNVERIFIED against a fill.
    """
    keep = 10_000 - FLAP_PROTOCOL_FEE_BPS - int(buy_tax_bps)
    return max(0, int(size_base_units) * keep // 10_000)


def _div_wad_up(numerator: int, denominator: int) -> int:
    """``ceil(numerator * 1e18 / denominator)`` — LibCurve's ``divWadUp``.

    Named rather than inlined because *which way it rounds* is the whole content of it:
    it is applied to the tokens the curve keeps, so the atom it rounds up is an atom the
    buyer does not get.
    """
    return (numerator * FLAP_WAD + denominator - 1) // denominator


@dataclass(frozen=True, slots=True)
class FlapCurve:
    """A Flap curve's own state: enough to price a **fill**, not just a unit.

    :class:`VenuePrice` answers "what is one token worth". Sizing asks a different
    question — "what would 0.01 BNB actually get, and how far would it move the price" —
    and spot cannot answer it. Both come out of the one ``getTokenV8Safe`` record, so this
    rides along on the price rather than costing a second read of a curve that has moved.

    Units, all of them the venue's own: ``r``, ``h`` and ``K`` scaled by :data:`FLAP_WAD`;
    supply, sold and the graduation point in token atoms; the reserve in quote base units.
    The published curve is ``(x + h)(y + r) = K`` with ``x`` tokens remaining and ``y``
    quote raised, so it is a constant product in the shifted coordinates and
    :attr:`virtual_token_atoms` / :attr:`virtual_quote_base` are its two sides — the same
    shape ``curve_price.CurveState`` carries for pump.fun.

    **UNVERIFIED against a live fill.**
    :class:`kaiba.execution.viability.PonsCurveDepth` is the cautionary tale and it is in
    this tree: two algebraically identical spellings of *that* curve differed by one atom
    on every one of 40 replayed fills, and only the chain could say which one the contract
    used. Here the division rounds up on the side the curve keeps, which hands the atom to
    the venue and under-reports the fill — the direction that refuses rather than the one
    that authorises. What would settle it: replaying Flap buy events against the portal
    record at ``block - 1``, exactly as ``PonsCurveDepth`` was settled.
    """

    r_scaled: int
    h_scaled: int
    k_scaled: int
    total_supply_atoms: int
    tokens_sold_atoms: int
    graduation_tokens_atoms: int
    #: The corroborated reserve — the same number :attr:`VenuePrice.quote_reserve_base`
    #: carries, and ``None`` for the same reason: the integral cross-check refused it.
    #: A fill is priced against the reserve, so without it there is no fill arithmetic.
    quote_reserve_base: int | None
    token_decimals: int
    quote_decimals: int
    #: The token's own buy and sell tax, bps, from the same record (words 12 and 13).
    #: ``None`` when the record did not carry them -- unknown, never zero. Not used by the
    #: fill arithmetic below (which prices the curve leg only); the cost model reads them.
    buy_tax_bps: int | None = None
    sell_tax_bps: int | None = None

    @property
    def refusal(self) -> str | None:
        """Why a fill against this curve cannot be priced, or ``None`` when it can.

        One place for the preconditions, so :meth:`tokens_out` and
        :attr:`max_size_base_units` cannot disagree about whether this curve is usable.
        """
        if (
            self.token_decimals != FLAP_CURVE_DECIMALS
            or self.quote_decimals != FLAP_CURVE_DECIMALS
        ):
            return (
                f"flap_decimals_not_wad:token{self.token_decimals}:"
                f"quote{self.quote_decimals}"
            )
        if self.r_scaled <= 0 or self.h_scaled <= 0 or self.k_scaled <= 0:
            return "flap_curve_constants_missing"
        if self.quote_reserve_base is None or self.quote_reserve_base < 0:
            return "flap_reserve_uncorroborated"
        if self.total_supply_atoms <= 0 or not (
            0 <= self.tokens_sold_atoms <= self.total_supply_atoms
        ):
            return "flap_tokens_sold_out_of_range"
        if not (
            self.tokens_sold_atoms
            < self.graduation_tokens_atoms
            <= self.total_supply_atoms
        ):
            # Word 8 is the documented graduation rule, 8e26 atoms (:class:`FlapRecord`).
            # A zero there, or a point already behind us, leaves the fill unbounded — and
            # a fill on a curve that graduates mid-order is not a fill at the size asked.
            return f"flap_graduation_point_unusable:{self.graduation_tokens_atoms}"
        return None

    @property
    def virtual_token_atoms(self) -> int:
        """``x + h`` in atoms: the token side of the equivalent constant product."""
        return self.total_supply_atoms + self.h_scaled - self.tokens_sold_atoms

    @property
    def virtual_quote_base(self) -> int | None:
        """``y + r`` in quote base units: the quote side. ``None`` without a reserve."""
        if self.quote_reserve_base is None:
            return None
        return self.r_scaled + self.quote_reserve_base

    def tokens_out(self, size_base_units: int) -> int | None:
        """Atoms a buy of ``size_base_units`` receives, as ``estimateSupply`` computes it.

        ``None`` — never 0 — whenever the answer is not a fill at the size asked for:
        an unusable curve, a non-positive size, or an order that would carry the curve
        past its graduation point and complete it mid-fill.
        """
        if self.refusal is not None:
            return None
        quote = self.virtual_quote_base
        size = int(size_base_units)
        if quote is None or size <= 0:
            return None
        out = self.virtual_token_atoms - _div_wad_up(self.k_scaled, quote + size)
        if out <= 0:
            return None
        if self.tokens_sold_atoms + out > self.graduation_tokens_atoms:
            return None
        return out

    @property
    def max_size_base_units(self) -> int | None:
        """Largest buy the curve can still take before it graduates, in quote base units.

        Closed form rather than a search, and the derivation is one line:
        ``tokens_out(dy) <= graduation - sold`` is
        ``divWadUp(K, y + r + dy) >= supply + h - graduation``, and for positive integers
        ``ceil(n/d) >= T`` holds exactly while ``d * (T - 1) < n``. So the largest
        denominator is ``(n - 1) // (T - 1)`` and ``dy`` is what is left of it.
        """
        if self.refusal is not None:
            return None
        quote = self.virtual_quote_base
        target = self.total_supply_atoms + self.h_scaled - self.graduation_tokens_atoms
        if quote is None or target < 2:
            return None
        size = (self.k_scaled * FLAP_WAD - 1) // (target - 1) - quote
        return size if size > 0 else None

    def quote_out(self, atoms: int) -> int | None:
        """Quote base units the curve pays for ``atoms`` sold back, BEFORE the protocol fee
        and the sell tax. The mirror of :meth:`tokens_out` on the same constant product.

        Rounds the way :meth:`tokens_out` does, against the trader: the quote side the
        curve keeps is rounded up, so the seller's answer is the smaller one. Never more
        than the corroborated reserve. ``None`` -- never 0 -- when there is no fill to price:
        an unusable curve, a non-positive amount, or more tokens than the curve has sold.

        **UNVERIFIED against a live sell**, exactly as :meth:`tokens_out` is unverified
        against a live buy. It values paper positions (``snipe.mark_bsc``); nothing sizes
        or exits on it.
        """
        if self.token_decimals != FLAP_CURVE_DECIMALS or self.quote_decimals != FLAP_CURVE_DECIMALS:
            return None
        if self.r_scaled <= 0 or self.h_scaled <= 0 or self.k_scaled <= 0:
            return None
        quote = self.virtual_quote_base
        n = int(atoms)
        if quote is None or self.quote_reserve_base is None or n <= 0 or n > self.tokens_sold_atoms:
            return None
        out = quote - _div_wad_up(self.k_scaled, self.virtual_token_atoms + n)
        return max(0, min(out, self.quote_reserve_base))

    def with_buy(self, net_quote_base: int, atoms: int) -> FlapCurve:
        """This curve as it would be had a buy of ``net_quote_base`` (what reached the
        curve, after fee and tax) for ``atoms`` landed on it. For valuing a paper holding
        on the live curve: our paper buy never reached it, so a mark that ignored it would
        sell our tokens into a curve that never received our BNB."""
        from dataclasses import replace

        reserve = self.quote_reserve_base
        return replace(
            self,
            tokens_sold_atoms=self.tokens_sold_atoms + max(0, int(atoms)),
            quote_reserve_base=None if reserve is None else reserve + max(0, int(net_quote_base)),
        )

    def at_graduation(self) -> FlapCurve | None:
        """This curve bought exactly to its graduation point: the state it is in the moment
        it leaves for the DEX. A curve that graduated was, by definition, bought to there --
        the least it reached, the same reading ``snipe.curve_end_value`` gives pump.fun. The
        reserve follows from the constant product, ``(x + h)(y + r) = K``. ``None`` when the
        constants do not allow it."""
        from dataclasses import replace

        remaining = self.total_supply_atoms + self.h_scaled - self.graduation_tokens_atoms
        if remaining <= 0 or self.k_scaled <= 0 or self.graduation_tokens_atoms <= 0:
            return None
        reserve = _div_wad_up(self.k_scaled, remaining) - self.r_scaled
        if reserve < 0:
            return None
        return replace(self, tokens_sold_atoms=self.graduation_tokens_atoms, quote_reserve_base=reserve)


# --------------------------------------------------------------------------------------
# a paper Flap sale at our quantity, our holding put back (2026-10-23, hA_)
# --------------------------------------------------------------------------------------

#: The fields of :func:`flap_curve_snapshot`; the same keys ``snipe._flap_curve_snapshot`` stores.
FLAP_SNAPSHOT_KEYS: tuple[str, ...] = ("r", "h", "k", "supply", "sold", "graduation", "reserve",
                                       "token_decimals", "quote_decimals", "buy_tax_bps", "sell_tax_bps")


def flap_curve_snapshot(curve: FlapCurve) -> dict[str, Any]:
    """``curve`` as JSON-safe integers (what ``PriceQuote.venue_curve`` carries)."""
    return {"r": curve.r_scaled, "h": curve.h_scaled, "k": curve.k_scaled, "supply": curve.total_supply_atoms,
            "sold": curve.tokens_sold_atoms, "graduation": curve.graduation_tokens_atoms,
            "reserve": curve.quote_reserve_base, "token_decimals": curve.token_decimals,
            "quote_decimals": curve.quote_decimals, "buy_tax_bps": curve.buy_tax_bps,
            "sell_tax_bps": curve.sell_tax_bps}


def flap_curve_from_snapshot(snap: Any) -> FlapCurve | None:
    """The inverse of :func:`flap_curve_snapshot`; ``None`` for anything that is not one."""
    if not isinstance(snap, Mapping):
        return None
    try:
        def _opt(key: str) -> int | None:
            raw = snap.get(key)
            return None if raw is None else int(raw)

        return FlapCurve(
            r_scaled=int(snap["r"]), h_scaled=int(snap["h"]), k_scaled=int(snap["k"]),
            total_supply_atoms=int(snap["supply"]), tokens_sold_atoms=int(snap["sold"]),
            graduation_tokens_atoms=int(snap["graduation"]), quote_reserve_base=_opt("reserve"),
            token_decimals=int(snap["token_decimals"]), quote_decimals=int(snap["quote_decimals"]),
            buy_tax_bps=_opt("buy_tax_bps"), sell_tax_bps=_opt("sell_tax_bps"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def flap_with_holding(curve: FlapCurve, atoms: int) -> tuple[FlapCurve | None, str]:
    """``curve`` as it would be had our paper holding of ``atoms`` been bought on it too.

    A paper buy never reached the chain, so the curve read there does not hold its BNB: selling paper
    tokens into it draws on BNB it never had (the sol lesson of ``pumpswap_price.with_holding``,
    2026-10-19). The Flap curve's state is a function of the tokens sold alone -- ``(x + h)(y + r) = K``
    -- so shifting ``sold`` by our atoms and the reserve by the quote the curve integral says those
    atoms cost is exactly the curve a real holder would sell into. Rounded against us: the quote added
    is ``floor(K/x') - ceil(K/x)``, never more than the curve.

    A counterfactual, not a measurement: exact while everyone else's flow is the same in TOKENS; a buyer
    who spent a fixed BNB amount would have received fewer tokens had our buy landed (the Vanguard review,
    section 6 item 2, notes the same limit on its Pons ``withHolding``). No landing delay is modelled here
    either: the curve is the deciding mark's read, as on every EVM paper leg (``paper.py`` pool path).
    ``(None, why)`` when the shifted curve is not one: an unusable curve, or our holding would carry
    it past its graduation point (the token would have left the curve)."""
    n = int(atoms or 0)
    why = curve.refusal
    if why is not None:
        return None, why
    if n <= 0:
        return None, "no_holding"
    sold = curve.tokens_sold_atoms + n
    if sold >= curve.graduation_tokens_atoms or sold > curve.total_supply_atoms:
        return None, "holding_crosses_graduation"
    x_now = curve.virtual_token_atoms
    x_held = x_now - n
    if x_held <= 0 or curve.quote_reserve_base is None:
        return None, "holding_exceeds_curve"
    delta = (curve.k_scaled * FLAP_WAD) // x_held - _div_wad_up(curve.k_scaled, x_now)
    if delta < 0:
        return None, "holding_quote_negative"
    from dataclasses import replace

    return replace(curve, tokens_sold_atoms=sold, quote_reserve_base=curve.quote_reserve_base + delta), "ok"


def flap_sell_quote_out(curve: FlapCurve, held_atoms: int, sell_atoms: int) -> tuple[int | None, str]:
    """Native base units a paper sale of ``sell_atoms`` (of ``held_atoms`` held) receives from ``curve``:
    the holding put back (:func:`flap_with_holding`), this sale taken out of it, the Flap protocol fee
    (:data:`FLAP_PROTOCOL_FEE_BPS`) off the proceeds -- the mirror of :func:`flap_buy_net_quote`.
    Before GMGN's router fee and the token's sell tax, which the caller charges.

    The smaller of :meth:`FlapCurve.quote_out` on the shifted curve (bounded by its reserve word) and
    the pure curve integral ``floor(K/x') - ceil(K/(x' + sold))``: the reserve word agrees with the
    integral to 1e-6 (``parse_flap_record``) and the sale gets the lower of the two. **UNVERIFIED against
    a live Flap sell**, like :meth:`FlapCurve.quote_out`. ``(None, why)`` when there is no sale to price."""
    sell = int(sell_atoms or 0)
    if sell <= 0 or sell > int(held_atoms or 0):
        return None, "sale_not_within_holding"
    shifted, why = flap_with_holding(curve, held_atoms)
    if shifted is None:
        return None, why
    by_word = shifted.quote_out(sell)
    if by_word is None:
        return None, "flap_quote_out_refused"
    x_held = shifted.virtual_token_atoms
    by_curve = (shifted.k_scaled * FLAP_WAD) // x_held - _div_wad_up(shifted.k_scaled, x_held + sell)
    gross = max(0, min(int(by_word), int(by_curve)))
    return gross * (10_000 - FLAP_PROTOCOL_FEE_BPS) // 10_000, "ok"


def parse_flap_record(
    record: FlapRecord,
    *,
    token_decimals: int,
    token_supply_atoms: int,
    quote_decimals: int,
    observed_ms: int | None = None,
    address_note: str = "",
) -> VenueRead:
    """A Flap record plus the token's own supply -> a priced curve, or a named refusal.

    Pure arithmetic on integers, no network, so every refusal below is reachable in a test.

    The three cross-checks are the substance of this function. Each one compares a number
    the venue reported against a number derived independently here, and a disagreement
    refuses the quote:

    1. ``S = K/r - h`` (the curve identity) must equal ``totalSupply / 10**decimals``.
       This is what makes it safe *not* to hardcode a 1e9 supply: a launch config with a
       different supply fails the check instead of being priced against the wrong ``x``.
    2. The venue's price word must equal ``K / (x + h)^2``.
    3. The venue's reserve word must equal ``K * (1/(x+h) - 1/(S+h))``, the integral of the
       price over the range already sold.
    """
    if not record.on_curve:
        return None, f"flap_not_on_curve:status={record.status}"
    if record.r_scaled <= 0 or record.h_scaled <= 0 or record.k_scaled <= 0:
        return None, "flap_curve_constants_missing"
    if token_decimals < 0 or token_decimals > 36 or token_supply_atoms <= 0:
        return None, "flap_token_supply_unreadable"
    if quote_decimals < 0 or quote_decimals > 36:
        return None, "flap_quote_decimals_unreadable"

    try:
        r = Decimal(record.r_scaled) / FLAP_FIXED_POINT
        h = Decimal(record.h_scaled) / FLAP_FIXED_POINT
        k = Decimal(record.k_scaled) / FLAP_FIXED_POINT
        supply_whole = Decimal(token_supply_atoms) / (Decimal(10) ** token_decimals)
        sold_whole = Decimal(record.tokens_sold_atoms) / (Decimal(10) ** token_decimals)
        derived_supply = k / r - h
    except (InvalidOperation, DivisionByZero, ArithmeticError):
        return None, "flap_curve_geometry_unreadable"

    gap = _relative_gap(derived_supply, supply_whole)
    if gap is None or gap > PRICE_CROSS_CHECK_TOLERANCE:
        # K = r*(h+S) held exactly on 4/4 live curves. If it stops holding, this module's
        # idea of "tokens remaining" is wrong, and a wrong x is a wrong price at every size.
        return None, f"flap_supply_identity_failed:K/r-h={derived_supply} vs supply={supply_whole}"
    if sold_whole < 0 or sold_whole > supply_whole:
        return None, "flap_tokens_sold_out_of_range"

    remaining = supply_whole - sold_whole
    try:
        price = k / ((remaining + h) ** 2)
        reported = Decimal(record.price_word) / FLAP_FIXED_POINT
    except (InvalidOperation, DivisionByZero, ArithmeticError):
        return None, "flap_price_arithmetic_failed"
    if price <= 0:
        return None, "flap_priced_to_zero"

    price_gap = _relative_gap(price, reported)
    if price_gap is None or price_gap > PRICE_CROSS_CHECK_TOLERANCE:
        return None, f"flap_price_disagrees:derived={price} reported={reported}"

    # The reserve is not load-bearing for the stop, so a disagreement here costs the rug
    # monitor its depth reading rather than costing us the price. It is still checked,
    # because it is the only thing that pins the reserve word to quote *base* units.
    reserve_base: int | None = record.quote_raised_base
    reserve_note = ""
    integral: Decimal | None
    reported_reserve: Decimal | None
    try:
        integral = k * (Decimal(1) / (remaining + h) - Decimal(1) / (supply_whole + h))
        reported_reserve = Decimal(record.quote_raised_base) / (Decimal(10) ** quote_decimals)
    except (InvalidOperation, DivisionByZero, ArithmeticError):
        integral = None
        reported_reserve = None
    if integral is None or reported_reserve is None:
        reserve_base, reserve_note = None, "; reserve arithmetic failed"
    else:
        reserve_gap = _relative_gap(integral, reported_reserve)
        if reserve_gap is not None and reserve_gap > RESERVE_CROSS_CHECK_TOLERANCE:
            reserve_base = None
            reserve_note = f"; reserve disagrees with the curve integral ({reserve_gap})"

    progress: Decimal | None = None
    if record.graduation_tokens_atoms > 0:
        progress = (
            Decimal(record.tokens_sold_atoms) * 100 / Decimal(record.graduation_tokens_atoms)
        )

    note = "flap curve"
    if progress is not None:
        note += f", progress {progress:.2f}%"
    note += address_note + reserve_note
    return (
        VenuePrice(
            price_quote_per_token=price,
            quote_token=record.quote_token,
            quote_reserve_base=reserve_base,
            quote_decimals=quote_decimals,
            venue="flap",
            observed_ms=observed_ms if observed_ms is not None else now_ms(),
            note=note,
            progress_pct=progress,
            # Built from the same record and the same corroborated reserve as the price
            # above, so a caller sizing a fill and a caller reading spot are looking at
            # one observation of one curve rather than two reads of a moving one.
            curve=FlapCurve(
                r_scaled=record.r_scaled,
                h_scaled=record.h_scaled,
                k_scaled=record.k_scaled,
                total_supply_atoms=token_supply_atoms,
                tokens_sold_atoms=record.tokens_sold_atoms,
                graduation_tokens_atoms=record.graduation_tokens_atoms,
                quote_reserve_base=reserve_base,
                token_decimals=token_decimals,
                quote_decimals=quote_decimals,
                buy_tax_bps=record.buy_tax_bps,
                sell_tax_bps=record.sell_tax_bps,
            ),
        ),
        "ok",
    )


#: The refusal :func:`read_flap` gives when the portal reverted ``TokenNotFound`` for this
#: token: it was never launched on Flap. A prefix callers match on, not prose.
FLAP_NOT_A_FLAP_TOKEN = "flap_not_a_flap_token"


def flap_token_not_found(result: Any, token: str) -> bool:
    """True only for the portal's ``TokenNotFound(token)`` revert naming THIS token.

    The address argument is checked as well as the selector: a revert that names some
    other address is an answer to a different question.
    """
    if not isinstance(result, RpcRevert) or result.selector != SEL_FLAP_TOKEN_NOT_FOUND:
        return False
    named = result.address_arg(0)
    return named is not None and named == str(token or "").strip().lower()


def read_flap(token: str, rpc: RpcBatch) -> VenueRead:
    """Price one BSC token from Flap's portal. Two round trips cold, one warm.

    The second trip only ever reads the quote token's ``decimals()``, which is immutable,
    so it is cached for the life of the process and the steady-state cost of a watchdog
    tick on a Flap position is a single batched ``eth_call``.
    """
    try:
        addr = token.strip().lower()
        if not looks_evm(addr):
            return None, "not_an_evm_address"
        results = rpc(
            [
                _call(FLAP_PORTAL, SEL_GET_TOKEN_V8_SAFE, addr),
                _call(addr, SEL_DECIMALS),
                _call(addr, SEL_TOTAL_SUPPLY),
            ]
        )
    except Exception as exc:  # noqa: BLE001 - a dead RPC is blindness, not a crash
        return None, f"flap_rpc_raised:{type(exc).__name__}"
    if len(results) < 3:
        return None, "flap_rpc_short_response"
    if isinstance(results[0], RpcRevert):
        # The portal ANSWERED, by reverting. Only a TokenNotFound naming this very token is
        # the portal saying "never launched here"; any other revert is not understood and
        # refuses like a failed read.
        if not flap_token_not_found(results[0], addr):
            return None, f"flap_portal_reverted:{results[0].selector}"
        if addr.endswith(FLAP_ADDRESS_FINGERPRINTS):
            # The launchpad mines this suffix on 99.3% of its tokens. A portal that disowns
            # one is more likely a lagging node or a new portal than a coincidence, and a
            # Flap token priced off a pool is the mistake FLAP_PROBE_UNAVAILABLE prevents.
            return None, "flap_token_not_found_but_fingerprinted"
        return None, f"{FLAP_NOT_A_FLAP_TOKEN}:TokenNotFound"
    if results[0] is None:
        # Not the same fact as "the portal has no record of this token": the endpoint did
        # not answer. An operator reading a blind position needs to tell a rate limit from
        # a token that was never launched here.
        return None, "flap_portal_read_failed"
    record = flap_record(_words(results[0]))
    if record is None:
        return None, "flap_no_portal_record"
    if not record.on_curve:
        return None, f"flap_not_on_curve:status={record.status}"
    decimals = _hex_int(results[1])
    supply = _hex_int(results[2])
    if decimals is None or supply is None:
        return None, "flap_token_metadata_unreadable"

    quote_decimals = _quote_decimals(Chain.BSC, record.quote_token, rpc)
    if quote_decimals is None:
        return None, "flap_quote_token_decimals_unreadable"

    fingerprint = (
        "" if addr.endswith(FLAP_ADDRESS_FINGERPRINT) else "; address lacks the 7777 fingerprint"
    )
    return parse_flap_record(
        record,
        token_decimals=decimals,
        token_supply_atoms=supply,
        quote_decimals=quote_decimals,
        address_note=fingerprint,
    )


# --------------------------------------------------------------------------------------
# BSC / four.meme, and the "neither launchpad" case
# --------------------------------------------------------------------------------------
#
# WHY (2026-10-05). Once a ``TokenNotFound`` from the Flap portal stopped reading as a
# failed read, a non-Flap bsc token needed somewhere to go. Of the 18 non-Flap tokens that
# reached a bsc sm-trenches decision in the prior 7 days, MEASURED on chain the same day:
#
# * 7 are four.meme (``TokenManagerHelper3.getTokenInfo`` answers version 2, manager
#   :data:`FOURMEME_TOKEN_MANAGER_V2`): 3 still on the curve (1 quoted in BNB, 2 in an
#   ERC-20), 4 graduated (``liquidityAdded`` true, ``_tokenInfos.status`` 3);
# * 6 trade on PancakeSwap pools (every one has a V2 WBNB pair and V3 pools);
# * 5 (geniusfun x3, o1_rwa x2) have NO PancakeSwap V2 or V3 pool against WBNB or USDT,
#   no DexScreener pair, and the V2 router reverts on them: their venue is a launchpad
#   curve nothing in this tree can read.
#
# The four.meme curve is a constant product on VIRTUAL reserves, read off TokenManager2's
# ``_tokenInfos(token)``: ``T`` is the virtual token reserve (atoms) and ``K`` the product
# scaled so the virtual quote reserve is ``K * 1e18 // T`` wei. MEASURED on all 7 four.meme
# tokens: ``K * 1e36 // T**2`` equals the venue's own ``lastPrice`` EXACTLY (gap 0 on 7/7),
# and on the 3 on-curve tokens ``T * cost // (K*1e18//T + cost)`` reproduces the venue's own
# ``tryBuy`` ``estimatedAmount`` to within 1.6e-18, 2.0e-17 and 6.6e-17 relative. ``tryBuy``
# also shows the fee is charged ON TOP: 0.01 BNB in -> 0.0099009901 on the curve + 1% of it.
# Not yet replayed against a live four.meme fill of ours (we have none); the runtime
# ``tryBuy`` cross-check below is what stands in for that, on every read.

#: four.meme ``TokenManagerHelper3`` -- the read-only helper four.meme publishes for
#: integrators. MEASURED 2026-10-05: answers ``getTokenInfo`` for all 7 four.meme tokens and
#: all-zero words for the 11 non-four.meme ones.
FOURMEME_HELPER = "0xf251f83e40a78868fcfa3fa4599dad6494e46034"

#: four.meme ``TokenManager2`` (V2 tokens). The manager named by ``getTokenInfo`` for 7 of 7
#: four.meme tokens sampled. A token managed by anything else is refused, not guessed at.
FOURMEME_TOKEN_MANAGER_V2 = "0x5c952063c7fc8610ffdb798152d69f0b9550762b"

#: ``getTokenInfo(address)`` on the helper: (version, tokenManager, quote, lastPrice,
#: tradingFeeRate, minTradingFee, launchTime, offers, maxOffers, funds, maxFunds,
#: liquidityAdded). ``_tokenInfos(address)`` on TokenManager2: (base, quote, template,
#: totalSupply, maxOffers, maxRaising, launchTime, offers, funds, lastPrice, K, T, status).
#: ``tryBuy(address,uint256,uint256)`` on the helper: (tokenManager, quote, estimatedAmount,
#: estimatedCost, estimatedFee, amountMsgValue, amountApproval, amountFunds). All three
#: selectors keccak-verified in this tree; the layouts decoded against the live answers.
SEL_FOURMEME_GET_TOKEN_INFO = "0x1f69565f"
SEL_FOURMEME_TOKEN_INFOS = "0xe684626b"
SEL_FOURMEME_TRY_BUY = "0xe21b103a"

#: ``_tokenInfos.status`` while trading on the curve (3 on all 4 graduated samples).
FOURMEME_STATUS_TRADING = 0

#: How much BNB the in-batch ``tryBuy`` cross-check asks about: 0.01 BNB, inside every
#: configured bsc position size, so the check exercises the region sizing will use.
FOURMEME_PROBE_FUNDS_WEI = 10**16

#: Relative disagreement allowed between this module's curve arithmetic and the venue's own
#: ``tryBuy``. MEASURED worst case 6.6e-17; 1e-9 is eight orders of slack, and a misread
#: word or a different curve template is off by far more.
FOURMEME_CROSS_CHECK_TOLERANCE = Decimal("1e-9")

#: PancakeSwap V3 factory on BSC. MEASURED 2026-10-05: its ``getPool`` returned exactly the
#: pool DexScreener reports as the token's pancakeswap-v3 pair for the three tokens checked.
PANCAKE_V3_FACTORY = "0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865"

#: ``getPair(address,address)`` (V2 factory) and ``getPool(address,address,uint24)`` (V3).
SEL_GET_PAIR = "0xe6a43905"
SEL_GET_POOL = "0x1698ee82"

#: PancakeSwap V3's fee tiers, in hundredths of a bip.
PANCAKE_V3_FEE_TIERS: tuple[int, ...] = (100, 500, 2500, 10000)

#: What a bsc pool has to be paired against to count as evidence of a PancakeSwap venue:
#: WBNB and BSC-USD, the two quote assets every one of the 6 pool tokens sampled pairs with.
PANCAKE_POOL_PARTNERS: tuple[tuple[str, str], ...] = (
    ("wbnb", "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"),
    ("usdt", "0x55d398326f99059ff775485246999027b3197955"),
)


@dataclass(frozen=True, slots=True)
class FourMemeCurve:
    """A four.meme curve's state, one batch, cross-checked against the venue twice.

    Integers only. ``tokens_out`` is the arithmetic the venue's ``tryBuy`` was MEASURED to
    match; it takes the CURVE leg (what reaches the curve after the on-top fee), and a
    caller that passes the whole order instead asks about a slightly larger order, which
    over-states impact -- the direction this tree errs in everywhere.
    """

    k_scaled: int
    virtual_token_atoms: int
    funds_wei: int
    max_funds_wei: int
    fee_bps: int
    last_price_wei: int

    @property
    def virtual_quote_wei(self) -> int:
        return self.k_scaled * 10**18 // self.virtual_token_atoms

    @property
    def headroom_wei(self) -> int:
        """Quote the curve can still take before it fills and lists on a DEX."""
        return max(0, self.max_funds_wei - self.funds_wei)

    @property
    def spot_wei_per_token(self) -> int:
        """``K * 1e36 // T**2``: wei per WHOLE token. Equals ``lastPrice`` on 7 of 7."""
        return self.k_scaled * 10**36 // (self.virtual_token_atoms * self.virtual_token_atoms)

    def tokens_out(self, leg_wei: int) -> int | None:
        """Atoms a curve leg of ``leg_wei`` buys, or ``None`` past graduation / at zero."""
        leg = int(leg_wei)
        if leg <= 0 or leg > self.headroom_wei:
            return None
        x = self.virtual_quote_wei
        return self.virtual_token_atoms * leg // (x + leg)


@dataclass(frozen=True, slots=True)
class NonFlapVenue:
    """Where a bsc token that is NOT on Flap trades, as the chain says.

    ``kind`` is one of ``fourmeme_curve`` (``curve`` set), ``fourmeme_graduated``,
    ``pancake_pool`` (``pools`` names what was found) or ``no_known_venue``.
    """

    kind: str
    note: str
    curve: FourMemeCurve | None = None
    pools: tuple[str, ...] = ()


def _addr_word(word: int) -> str:
    return "0x" + format(word & ((1 << 160) - 1), "040x")


def _non_flap_calls(addr: str) -> list[tuple[str, str]]:
    """One batch: four.meme record, curve, tryBuy cross-check, then PancakeSwap pools."""
    calls = [
        _call(FOURMEME_HELPER, SEL_FOURMEME_GET_TOKEN_INFO, addr),
        _call(FOURMEME_TOKEN_MANAGER_V2, SEL_FOURMEME_TOKEN_INFOS, addr),
        (
            FOURMEME_HELPER,
            SEL_FOURMEME_TRY_BUY + addr[2:].rjust(64, "0") + format(0, "064x")
            + format(FOURMEME_PROBE_FUNDS_WEI, "064x"),
        ),
    ]
    for _label, partner in PANCAKE_POOL_PARTNERS:
        calls.append(
            (PANCAKE_V2_FACTORY, SEL_GET_PAIR + addr[2:].rjust(64, "0") + partner[2:].rjust(64, "0"))
        )
        for fee in PANCAKE_V3_FEE_TIERS:
            calls.append(
                (PANCAKE_V3_FACTORY, SEL_GET_POOL + addr[2:].rjust(64, "0")
                 + partner[2:].rjust(64, "0") + format(fee, "064x"))
            )
    return calls


def _fourmeme_curve(addr: str, info: list[int], state: list[int], try_buy: Any) -> tuple[FourMemeCurve | None, str]:
    """The cross-checked curve of an on-curve, BNB-quoted, TokenManager2 token, or a refusal."""
    if len(state) < 13:
        return None, "fourmeme_curve_unreadable"
    if _addr_word(state[0]) != addr:
        return None, "fourmeme_record_names_another_token"
    k, t = state[10], state[11]
    if k <= 0 or t <= 0:
        return None, "fourmeme_curve_constants_zero"
    if state[7] != info[7] or state[8] != info[9] or state[9] != info[3]:
        # offers / funds / lastPrice read twice in one batch must agree.
        return None, "fourmeme_record_disagrees_with_helper"
    if state[12] != FOURMEME_STATUS_TRADING:
        return None, f"fourmeme_not_trading:status={state[12]}"
    fee = info[4]
    if fee < 0 or fee >= 10_000:
        return None, f"fourmeme_fee_unusable:{fee}"
    if info[5] != 0:
        # A floor on the fee makes small orders dearer than ``fee`` bps. 0 on 7 of 7
        # sampled; anything else is a fee schedule this module does not price.
        return None, f"fourmeme_min_fee_unmodelled:{info[5]}"
    curve = FourMemeCurve(
        k_scaled=k, virtual_token_atoms=t, funds_wei=info[9], max_funds_wei=info[10],
        fee_bps=fee, last_price_wei=info[3],
    )
    gap = _relative_gap(Decimal(curve.spot_wei_per_token), Decimal(curve.last_price_wei))
    if gap is None or gap > FOURMEME_CROSS_CHECK_TOLERANCE:
        return None, f"fourmeme_price_disagrees:derived={curve.spot_wei_per_token} reported={curve.last_price_wei}"
    if curve.headroom_wei <= 0:
        return None, "fourmeme_curve_full"
    bought = _words(try_buy)
    if len(bought) < 4:
        return None, "fourmeme_try_buy_unreadable"
    venue_out, venue_leg = bought[2], bought[3]
    mine = curve.tokens_out(venue_leg)
    if mine is None or venue_out <= 0:
        return None, "fourmeme_try_buy_unreadable"
    gap = _relative_gap(Decimal(mine), Decimal(venue_out))
    if gap is None or gap > FOURMEME_CROSS_CHECK_TOLERANCE:
        return None, f"fourmeme_try_buy_disagrees:model={mine} venue={venue_out}"
    return curve, "ok"


def read_bsc_non_flap(token: str, rpc: RpcBatch) -> tuple[NonFlapVenue | None, str]:
    """Where a token the Flap portal disowned trades: a four.meme curve, a pool, or nowhere.

    One batched ``eth_call`` (13 calls). ``(None, why)`` whenever the answer would be a
    guess -- a failed read, a four.meme record that fails its cross-checks, a four.meme
    curve quoted in something other than BNB, a manager this module has not decoded.
    ``NonFlapVenue('no_known_venue')`` is NOT a refusal by itself; it is the chain
    answering that there is no four.meme record and no PancakeSwap pool, and the caller
    decides (viability refuses it). Never raises.
    """
    try:
        addr = token.strip().lower()
        if not looks_evm(addr):
            return None, "not_an_evm_address"
        calls = _non_flap_calls(addr)
        results = rpc(calls)
    except Exception as exc:  # noqa: BLE001 - a dead RPC is blindness, not a crash
        return None, f"non_flap_rpc_raised:{type(exc).__name__}"
    if not isinstance(results, list) or len(results) < len(calls):
        return None, "non_flap_rpc_short_response"
    info = _words(results[0])
    if len(info) < 12:
        # The helper answers all-zero words for a token it does not know, so an
        # unreadable answer is a failed read, never "not four.meme".
        return None, "fourmeme_helper_read_failed"
    manager = _word_address(info[1])
    if info[0] != 0 or manager is not None:
        if manager != FOURMEME_TOKEN_MANAGER_V2:
            return None, f"fourmeme_manager_unsupported:{manager}"
        if info[11] != 0:
            return NonFlapVenue("fourmeme_graduated", "fourmeme:graduated"), "ok"
        quote = _word_address(info[2])
        if quote is not None:
            return None, f"fourmeme_quote_not_native:{quote}"
        curve, why = _fourmeme_curve(addr, info, _words(results[1]), results[2])
        if curve is None:
            return None, why
        progress = Decimal(curve.funds_wei) * 100 / Decimal(curve.max_funds_wei) if curve.max_funds_wei else None
        note = f"fourmeme:curve:fee{curve.fee_bps}bps" + (f":progress{progress:.1f}%" if progress is not None else "")
        return NonFlapVenue("fourmeme_curve", note, curve=curve), "ok"

    pools: list[str] = []
    at = 3
    for label, _partner in PANCAKE_POOL_PARTNERS:
        for kind in ["v2", *[f"v3:{fee}" for fee in PANCAKE_V3_FEE_TIERS]]:
            raw = results[at]
            at += 1
            if raw is None:
                # Not knowing whether a pool exists is not knowing the venue.
                return None, f"pancake_factory_read_failed:{kind}:{label}"
            words = _words(raw)
            if not words:
                return None, f"pancake_factory_read_failed:{kind}:{label}"
            if _word_address(words[0]) is not None:
                pools.append(f"{kind}:{label}")
    if pools:
        note = f"pancake:{len(pools)}pools:{pools[0]}"
        return NonFlapVenue("pancake_pool", note, pools=tuple(pools)), "ok"
    return NonFlapVenue("no_known_venue", "no_fourmeme_record_and_no_pancake_pool"), "ok"


# --------------------------------------------------------------------------------------
# Robinhood / Pons
# --------------------------------------------------------------------------------------

#: token -> (curve address, quote token or ``None`` for native). Both are fixed at launch,
#: so this is a cache of immutable facts, not of prices, and it never expires.
_PONS_CURVE: dict[str, tuple[str, str | None]] = {}


#: Tokens the Pons factory has told us it does not know, and when it said so (ms).
#:
#: MEASURED on the box 2026-09-24. Five open robinhood positions each answered
#: ``pons_curve_unknown`` -- the factory ANSWERED and said the token was never launched
#: on Pons -- and nothing recorded that. So every position paid one factory read every
#: 12 s tick, forever, to be told the same thing. Five positions on the cold path draw
#: 0.83 reads/s against a bucket that refills at 0.60/s, so ``robinhood-rpc`` sat in
#: permanent overdraft: 17,354 ``EXIT drawing on the reserved overdraft`` and 10,727
#: ``EXIT bypassing chain cooldown`` in twelve hours. A saturated bucket stretches the
#: protection tick, an overrunning tick arms ``protection_overrun``, and that halts
#: entries on EVERY chain -- so an unremembered negative was stopping all trading.
#:
#: Caching this is safe for the same reason ``remember_pons_curve`` is: a launch record
#: is set at launch and never changes, so a token the factory disowns it will always
#: disown. The TTL is belt and braces -- if this were ever wrong it self-heals in a day,
#: and re-probing one token daily is free next to one read per position per tick.
#:
#: This never blinds a position. It skips only the PONS leg; the fallback chain still
#: runs, which is what was actually pricing these tokens anyway.
PONS_ABSENT_TTL_MS = 86_400_000

#: Process-local mirror of the persisted marker, to skip even the database read.
_PONS_ABSENT: dict[str, int] = {}


def remember_pons_absent(token: str, *, conn: Any = None, observed_ms: int | None = None) -> None:
    """Record that the factory disowned ``token``. Never raises, never guesses."""
    address = str(token or "").strip().lower()
    if not looks_evm(address):
        return
    stamp = int(observed_ms if observed_ms is not None else now_ms())
    _PONS_ABSENT[address] = stamp
    try:
        from kaiba.core.db import fetch_one as _fetch_one
        from kaiba.core.db import get_conn as _get_conn
        from kaiba.core.db import jdump as _jdump
        from kaiba.core.db import jload as _jload

        c = conn if conn is not None else _get_conn()
        row = _fetch_one(
            c, "SELECT meta_json FROM tokens WHERE chain=? AND address=?",
            (Chain.ROBINHOOD.value, address),
        )
        meta = (_jload(row["meta_json"], {}) if row else {}) or {}
        if not isinstance(meta, dict):
            meta = {}
        meta["pons_curve_absent_ms"] = stamp
        c.execute(
            "UPDATE tokens SET meta_json=? WHERE chain=? AND address=?",
            (_jdump(meta), Chain.ROBINHOOD.value, address),
        )
        c.commit()
    except Exception as exc:  # noqa: BLE001 - a failed cache write is not a pricing failure
        log.debug("could not remember pons absence (%s)", type(exc).__name__)


def pons_absent_recently(token: str, *, conn: Any = None, now_ms: int | None = None) -> bool:
    """True when the factory disowned this token inside :data:`PONS_ABSENT_TTL_MS`."""
    address = str(token or "").strip().lower()
    if not looks_evm(address):
        return False
    if now_ms is None:
        # The parameter shadows the module-level clock, so reach it under a new name.
        from kaiba.core.schemas import now_ms as _clock

        now = int(_clock())
    else:
        now = int(now_ms)
    stamp = _PONS_ABSENT.get(address)
    if stamp is None:
        try:
            from kaiba.core.db import fetch_one as _fetch_one
            from kaiba.core.db import get_conn as _get_conn
            from kaiba.core.db import jload as _jload

            row = _fetch_one(
                conn if conn is not None else _get_conn(),
                "SELECT meta_json FROM tokens WHERE chain=? AND address=?",
                (Chain.ROBINHOOD.value, address),
            )
        except Exception as exc:  # noqa: BLE001 - an unreadable registry is not an error
            log.debug("pons absence lookup failed (%s)", type(exc).__name__)
            return False
        meta = (_jload(row["meta_json"], {}) if row else {}) or {}
        if not isinstance(meta, dict):
            return False
        value = meta.get("pons_curve_absent_ms")
        if not isinstance(value, (int, float)):
            return False
        stamp = int(value)
        _PONS_ABSENT[address] = stamp
    # A stamp in the FUTURE is clock skew, not a fresh observation, and must not become
    # a permanent waiver of the factory read.
    if stamp > now:
        return False
    return (now - stamp) < PONS_ABSENT_TTL_MS

def remember_pons_curve(
    token: str, curve: str | None, quote_token: str | None, *, conn: Any = None
) -> None:
    """Persist a curve the factory just told us, so we never pay for it twice.

    ``read_pons``'s own docstring says the curve and its quote token are "set at launch and
    never change". That is what makes this safe: it caches an immutable fact rather than
    memoising a measurement.

    MEASURED 2026-09-23: the in-memory ``_PONS_CURVE`` dies with the process, the registry
    is written only by the launch watcher, and 10 of 11 open robinhood positions therefore
    had no cached curve -- paying two RPC round trips per position per tick on a bucket
    that refills at 0.6/s. ``pons_factory_read_failed`` was 235 of 446 blind events in six
    hours, each one a live position with no working stop.

    Never raises. A pricing path must not die because a cache write failed, and junk is
    never written: a bad address here would poison the registry permanently, which is worse
    than paying for the read again.
    """
    address = str(token or "").strip().lower()
    curve_address = str(curve or "").strip().lower()
    if not looks_evm(address) or not looks_evm(curve_address) or curve_address == ZERO_ADDRESS:
        return
    pair = str(quote_token or "").strip().lower()
    native = not (looks_evm(pair) and pair != ZERO_ADDRESS)
    try:
        from kaiba.core.db import fetch_one as _fetch_one
        from kaiba.core.db import get_conn as _get_conn
        from kaiba.core.db import jdump as _jdump
        from kaiba.core.db import jload as _jload

        c = conn if conn is not None else _get_conn()
        row = _fetch_one(
            c, "SELECT meta_json FROM tokens WHERE chain=? AND address=?",
            (Chain.ROBINHOOD.value, address),
        )
        meta = (_jload(row["meta_json"], {}) if row else {}) or {}
        if not isinstance(meta, dict):
            meta = {}
        meta["quote_is_native"] = bool(native)
        if not native:
            meta["pair_token"] = pair
        c.execute(
            "UPDATE tokens SET pool=?, meta_json=? WHERE chain=? AND address=?",
            (curve_address, _jdump(meta), Chain.ROBINHOOD.value, address),
        )
        c.commit()
    except Exception as exc:  # noqa: BLE001 - a failed cache write is not a pricing failure
        log.debug("could not remember pons curve (%s)", type(exc).__name__)


def _pons_curve_from_registry(token: str, *, conn: Any = None) -> tuple[str, str | None] | None:
    """The curve address and quote token from our own ``tokens`` row, or ``None``.

    Worth a database read because it removes a **network** read, and on this endpoint that
    is the difference between seeing a position and not. Measured 2026-09-21: the
    Robinhood public RPC answers a burst with HTTP 429, and one 429 puts the whole
    ``chain.*`` family into a 60-second cooldown — during a 20-token sample only 3 of 40
    calls reached the wire and 15 tokens went unpriced purely from the cold-path second
    round trip. ``kaiba.ingest.robinhood.record_new_token`` already writes both fields for
    every launch it sees, so for any token we could actually hold a position in this turns
    the cold path into the warm one.

    Never raises and never guesses: a row without a usable ``pool`` returns ``None`` and
    the factory is asked, which is the on-chain authority either way.
    """
    try:
        from kaiba.core.db import fetch_one, get_conn, jload

        row = fetch_one(
            conn if conn is not None else get_conn(),
            "SELECT pool, meta_json FROM tokens WHERE chain=? AND address=?",
            (Chain.ROBINHOOD.value, str(token or "").strip().lower()),
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable registry is not an error here
        log.debug("pons registry lookup failed (%s)", type(exc).__name__)
        return None
    if not row:
        return None
    curve = str(row.get("pool") or "").strip().lower()
    if not looks_evm(curve) or curve == ZERO_ADDRESS:
        return None
    meta = jload(row.get("meta_json"), {}) or {}
    if not isinstance(meta, Mapping):
        return None
    # ``quote_is_native`` must be present. Absent, we do not know whether the curve is
    # ETH-quoted, and the quote token decides the whole USD leg — so we fall through to
    # the factory rather than assuming the 78% case.
    if "quote_is_native" not in meta:
        return None
    if meta.get("quote_is_native"):
        return curve, None
    pair = str(meta.get("pair_token") or "").strip().lower()
    if not looks_evm(pair) or pair == ZERO_ADDRESS:
        return None
    return curve, pair

#: (chain, token) -> decimals. Also immutable.
_DECIMALS: dict[tuple[str, str], int] = {}


# --------------------------------------------------------------------------------------
# a Pons fill at our size, and a sale with our holding put back (2026-10-23, hB_)
# --------------------------------------------------------------------------------------
#
# Vanguard review (docs/research/claude-vanguard-snipe-review-20261023.md) section 6 item 2: a
# robinhood paper buy on the Pons curve was handed to the broker with no executable
# (``evm_pool_price._pons_entry``: ``curve_mark``), so the broker approximated it as a constant
# product against the curve's REAL quote reserve -- ignoring the 1.68 ETH phantom quote that prices
# the curve. MEASURED: the one robinhood launch-snipe decision of 2026-10-23 02:59 UTC
# (``ord_a7220ca6cd8683428adfa356``, 0.02808 ETH) was refused ``slippage_exceeded impact=2252bps``
# against a real reserve of 0.09602 ETH, where the curve itself charges about 2.6% (its 1% fee plus
# ~1.6% impact on 1.776 ETH of virtual quote).

#: The most a Pons buy gives up to its three takes together (``feeBps + creatorTaxBps`` + the snipe
#: tax), bps. MEASURED 2026-10-23 on the box's tape: two buys in their launch's own second on curves
#: with ``creatorTaxBps`` 200 reproduce to the atom only with a snipe tax of 9,600 = 9,900 - 100 - 200,
#: where the rung table says 9,900 (``tests/fixtures/robinhood/pons_curve_trades.json`` curve E). The
#: 2026-09 toll sample shows the same shape: 9,400 on a creator-tax-400 curve
#: (``tests/test_pons_tax_entry.MEASURED_TOLL_SAMPLE``, "9900 less the creator's pro-rata share").
PONS_MAX_TAKE_BPS = 9_900


@dataclass(frozen=True, slots=True)
class PonsCurve:
    """A Pons curve's own state: enough to price a **fill** at our size, the Pons twin of :class:`FlapCurve`.

    All integers off one :func:`read_pons` batch: ``quoteReserve`` (phantom + real), the curve's whole
    token balance (``sellableTokens + reservedTokens``), the real quote raised, the graduation threshold,
    ``feeBps``, ``creatorTaxBps`` and the snipe-tax parameters.

    **The arithmetic is the contract's, MEASURED against the chain** (2026-10-23, hB_; replayed from the
    box's tape, ``tests/test_paper_pons_exact_fill.py``): on 80 curves launched 10-20..10-23 (creator tax
    0-1,000 bps), 4,303 ``CurveBuy`` (198 of them inside the snipe window) and 3,478 ``CurveSell``
    replayed from each curve's launch state reproduce the chain's ``tokensOut`` / ``quoteOut`` to the
    atom and the wei on 79 curves (the 80th has a gap in our tape), and all 80 ``eth_simulateV1`` quotes
    of ``curve.buy`` at 0.0167 ETH (``snipe.paper_entry_rh``) equal :meth:`tokens_out` on the replayed
    state (72 at the decision block, 8 a few blocks after it -- ``latest`` had moved on). The rule:

    * buy: each take off the INPUT, floored on its own --
      ``net = in - in*fee//1e4 - in*creatorTax//1e4 - in*snipe//1e4`` -- then
      ``tokens = tokenReserve * net // (quoteReserve + net)``; the takes leave the curve
      (``quoteReserve += net``, ``tokenReserve -= tokens``);
    * sell: ``gross = n * quoteReserve // (tokenReserve + n)``, the seller receives
      ``gross - gross*fee//1e4 - gross*creatorTax//1e4`` (no snipe tax), and ``gross`` leaves the curve.

    ``viability.PonsCurveDepth`` (sizing) floors the two takes together; that differs from this by at most
    one wei of ``net`` and was only ever replayed on creator-tax-0 curves. It sizes; this fills.
    """

    quote_reserve: int
    token_reserve: int
    real_quote_reserve: int
    sellable_tokens: int
    graduation_threshold: int
    fee_bps: int
    #: ``creatorTaxBps()``; ``None`` = the item was not read -- then nothing here prices a fill.
    creator_tax_bps: int | None
    launched_at_s: int
    snipe_tax_start_bps: int
    snipe_tax_seconds: int
    token_decimals: int

    @property
    def refusal(self) -> str | None:
        """Why no fill can be priced on this curve, or ``None`` when one can."""
        if self.creator_tax_bps is None:
            return "pons_creator_tax_unread"
        if not 0 <= self.fee_bps < 10_000 or not 0 <= self.creator_tax_bps < 10_000:
            return "pons_take_out_of_range"
        if self.fee_bps + self.creator_tax_bps >= PONS_MAX_TAKE_BPS:
            return "pons_take_is_the_whole_order"
        if self.quote_reserve <= 0 or self.token_reserve <= 0 or self.sellable_tokens <= 0:
            return "pons_curve_reserves_empty"
        if not 0 <= self.real_quote_reserve < self.graduation_threshold:
            return "pons_curve_complete"
        if not 0 <= self.token_decimals <= 36:
            return "pons_decimals_unreadable"
        return None

    @property
    def take_bps(self) -> int | None:
        """``feeBps + creatorTaxBps``, the curve's own per-leg take outside the snipe window."""
        return None if self.creator_tax_bps is None else self.fee_bps + self.creator_tax_bps

    @property
    def headroom(self) -> int:
        """Quote the curve can still take before it graduates."""
        return self.graduation_threshold - self.real_quote_reserve

    def snipe_tax_bps_at(self, at_s: int) -> int:
        """The snipe tax a buy landing at unix second ``at_s`` pays: the MEASURED rung table for the one
        measured configuration (``ingest.robinhood.SNIPE_TAX_RUNGS_BPS``), the full starting toll inside
        any other configuration's window (fail closed), 0 after it -- capped so the three takes never
        exceed :data:`PONS_MAX_TAKE_BPS` together."""
        from kaiba.ingest import robinhood as rh

        if self.snipe_tax_start_bps <= 0 or self.snipe_tax_seconds <= 0:
            return 0
        elapsed = int(at_s) - int(self.launched_at_s)
        if elapsed >= self.snipe_tax_seconds:
            return 0
        if elapsed >= 0 and (self.snipe_tax_start_bps, self.snipe_tax_seconds) == rh.SNIPE_TAX_MEASURED_CONFIG:
            rung = rh.SNIPE_TAX_RUNGS_BPS.get(elapsed, self.snipe_tax_start_bps)
        else:
            rung = self.snipe_tax_start_bps
        return max(0, min(int(rung), PONS_MAX_TAKE_BPS - self.fee_bps - int(self.creator_tax_bps or 0)))

    def buy_net(self, quote_in: int, *, at_s: int) -> int | None:
        """The quote of a buy of ``quote_in`` that reaches the curve, landing at ``at_s``."""
        if self.refusal is not None:
            return None
        size = int(quote_in)
        if size <= 0:
            return None
        snipe = self.snipe_tax_bps_at(at_s)
        net = (size - size * self.fee_bps // 10_000 - size * int(self.creator_tax_bps or 0) // 10_000
               - size * snipe // 10_000)
        return net if net > 0 else None

    def tokens_out(self, quote_in: int, *, at_s: int) -> tuple[int | None, int, str]:
        """``(atoms, net quote that reached the curve, why)`` for a buy of ``quote_in`` landing at ``at_s``.

        ``atoms`` is ``None`` -- never 0 -- when this is not a fill at the size asked: an unusable curve,
        a size the takes swallow, or a buy that would carry the curve past its graduation threshold
        (the contract completes the curve mid-order, which this does not model)."""
        why = self.refusal
        if why is not None:
            return None, 0, why
        net = self.buy_net(quote_in, at_s=at_s)
        if net is None:
            return None, 0, "pons_fill_rounds_to_zero"
        if net >= self.headroom:
            return None, net, "pons_fill_crosses_graduation"
        atoms = self.token_reserve * net // (self.quote_reserve + net)
        if atoms <= 0 or atoms >= self.sellable_tokens:
            return None, net, "pons_fill_exceeds_sellable"
        return atoms, net, "ok"

    def after_buy(self, net: int, atoms: int | None = None) -> PonsCurve:
        """This curve after a buy whose ``net`` quote reached it (``atoms`` defaults to the curve's own
        answer for it): the takes have already left, so the quote side grows by exactly ``net``."""
        from dataclasses import replace

        n = max(0, int(net))
        out = (self.token_reserve * n // (self.quote_reserve + n)) if atoms is None else max(0, int(atoms))
        return replace(self, quote_reserve=self.quote_reserve + n, token_reserve=self.token_reserve - out,
                       real_quote_reserve=self.real_quote_reserve + n,
                       sellable_tokens=max(0, self.sellable_tokens - out))

    def quote_out(self, atoms: int) -> tuple[int | None, int]:
        """``(quote the seller receives, gross quote leaving the curve)`` for ``atoms`` sold back, by the
        contract's own sell arithmetic. Never more than the real quote reserve; ``None`` when the curve or
        the amount cannot be priced."""
        if self.refusal is not None:
            return None, 0
        n = int(atoms)
        if n <= 0:
            return None, 0
        gross = min(n * self.quote_reserve // (self.token_reserve + n), self.real_quote_reserve)
        out = gross - gross * self.fee_bps // 10_000 - gross * int(self.creator_tax_bps or 0) // 10_000
        return (out if out > 0 else None), gross


#: The fields of :func:`pons_curve_snapshot` (JSON-safe integers; ``creator_tax_bps`` may be null).
PONS_SNAPSHOT_KEYS: tuple[str, ...] = ("quote_reserve", "token_reserve", "real_quote_reserve", "sellable_tokens",
                                       "graduation_threshold", "fee_bps", "creator_tax_bps", "launched_at_s",
                                       "snipe_tax_start_bps", "snipe_tax_seconds", "token_decimals")


def pons_curve_snapshot(curve: PonsCurve) -> dict[str, Any]:
    """``curve`` as JSON-safe values (what ``PriceQuote.venue_curve`` carries for a Pons mark)."""
    return {"venue": "pons", **{key: getattr(curve, key) for key in PONS_SNAPSHOT_KEYS}}


def pons_curve_from_snapshot(snap: Any) -> PonsCurve | None:
    """The inverse of :func:`pons_curve_snapshot`; ``None`` for anything that is not one (a Flap snapshot
    included: it carries no ``venue: pons``)."""
    if not isinstance(snap, Mapping) or snap.get("venue") != "pons":
        return None
    try:
        values = {key: (None if snap.get(key) is None else int(snap[key])) for key in PONS_SNAPSHOT_KEYS}
        if any(values[key] is None for key in PONS_SNAPSHOT_KEYS if key != "creator_tax_bps"):
            return None
        return PonsCurve(**values)  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError):
        return None


def pons_with_holding(curve: PonsCurve, held_atoms: int, *,
                      entry_net_quote: int | None = None) -> tuple[PonsCurve | None, str]:
    """``curve`` as it would be had our paper holding of ``held_atoms`` been bought on it.

    A paper buy never reached the chain, so the curve read there lacks the ETH it would have added;
    selling paper tokens into it draws on ETH it never had (the sol lesson of
    ``pumpswap_price.with_holding``). Two counterfactuals, each exact under its own assumption about
    everyone else's flow since our entry, and the curve gets the SMALLER quote of the two, so the sale
    is valued against us either way:

    * others' flow fixed in TOKENS: the curve holds ``held_atoms`` fewer tokens, i.e. a buy of what those
      atoms cost on this curve now, ``ceil(held * q / (t - held))``;
    * others' flow fixed in QUOTE (every later buyer spent a fixed ETH amount: Vanguard's ``withHolding``,
      ``robinhood-pons.mjs:63-75``): the curve holds the quote our paper buy put in, ``entry_net_quote``
      (``None`` when the entry did not record it -- then the first alone).

    Either way the shift is a buy of that quote on the contract's own arithmetic
    (:meth:`PonsCurve.after_buy`), the tokens it takes out capped at ``held_atoms`` -- rounded against us: the
    contract's floor on a buy of ``ceil(...)`` can take an atom or two more than we hold, which would leave
    the curve dearer than our holding made it. ``(None, why)`` when there is no such curve: an unusable
    curve, no holding, more than the curve holds, or a holding that would have graduated it (the token
    would have left the curve)."""
    why = curve.refusal
    if why is not None:
        return None, why
    held = int(held_atoms or 0)
    if held <= 0:
        return None, "no_holding"
    if held >= curve.token_reserve:
        return None, "holding_exceeds_curve"
    cost_now = -(-held * curve.quote_reserve // (curve.token_reserve - held))
    delta = cost_now
    if entry_net_quote is not None and int(entry_net_quote) > 0:
        delta = min(cost_now, int(entry_net_quote))
    if delta >= curve.headroom:
        return None, "holding_crosses_graduation"
    removed = min(curve.token_reserve * delta // (curve.quote_reserve + delta), held)
    return curve.after_buy(delta, removed), "ok"


def pons_sell_quote_out(curve: PonsCurve, held_atoms: int, sell_atoms: int, *,
                        entry_net_quote: int | None = None) -> tuple[int | None, str]:
    """Native base units a paper sale of ``sell_atoms`` (of ``held_atoms`` held) receives from ``curve``:
    the holding put back (:func:`pons_with_holding`), this sale taken out of it on the contract's own sell
    arithmetic, the curve's ``feeBps`` and ``creatorTaxBps`` off the proceeds. Before GMGN's router fee,
    which the caller charges. Exact for the counterfactual curve; the counterfactual itself is a bound
    (see :func:`pons_with_holding`)."""
    sell = int(sell_atoms or 0)
    if sell <= 0 or sell > int(held_atoms or 0):
        return None, "sale_not_within_holding"
    shifted, why = pons_with_holding(curve, held_atoms, entry_net_quote=entry_net_quote)
    if shifted is None:
        return None, why
    out, _gross = shifted.quote_out(sell)
    if out is None:
        return None, "pons_sale_pays_nothing"
    return out, "ok"


def pons_price(
    state: Any,
    *,
    token_decimals: int,
    quote_decimals: int,
    quote_token: str | None,
    observed_ms: int | None = None,
    creator_tax_bps: int | None = None,
) -> VenueRead:
    """A :class:`kaiba.ingest.robinhood.CurveState` -> a priced curve, or a refusal.

    ``creator_tax_bps`` is the curve's ``creatorTaxBps()`` when the caller read it; it and the
    state's ``feeBps()`` ride on the priced curve for the paper legs, and price nothing here.

    Pons is a constant product on ``quoteReserve * curveTokenBalance``, verified in
    ``kaiba/ingest/robinhood.py`` to a relative error below 1e-9 on live curves, so spot is
    the reserve ratio and nothing here needs to re-derive it. ``curveTokenBalance`` is
    ``sellableTokens() + reservedTokens()`` — the curve's **whole** balance, which is the
    one correction that file had to make and the one this would otherwise get wrong.

    Note which reserve becomes ``liquidity``: ``realQuoteReserve``, the quote actually
    raised, not ``quoteReserve`` which includes the phantom leg. The phantom reserve sets
    the price and cannot be withdrawn; reporting it as depth would tell the rug monitor
    that a curve with 5 wei in it is 1.68 ETH deep.

    **A graduated Pons curve does not look graduated.** This is the trap in this venue and
    it cost a wrong price in the first draft of this module. ``CurveState.graduated`` asks
    whether ``realQuoteReserve >= graduationThreshold``, which is true for exactly the
    instant of the graduating trade — and graduation is atomic with that trade, so nobody
    ever observes it. Afterwards the curve contract is **drained and reset**: on both
    graduated tokens in the live database (read on-chain 2026-09-21)
    ``realQuoteReserve`` is back to 0, ``sellableTokens`` is 0, ``quoteReserve`` is back to
    the phantom-only 1.68 ETH, and ``reservedTokens`` still reports its launch parameter.
    A naive reserve ratio therefore prices every graduated token at a fixed
    5.88e-9 ETH forever, from a contract holding nothing, while the real market is a
    Uniswap v4 pool. Two things catch it here, and either alone is sufficient:

    * ``sellableTokens == 0``. The venue's own geometry makes this exact rather than
      approximate: ``reservedTokens / launchSupply == phantomQuote / (phantomQuote +
      graduationThreshold)``, so the sellable block reaches zero precisely when the raise
      reaches the threshold.
    * The constant product ``quoteReserve * curveTokenBalance == phantomQuote *
      launchSupply`` fails. Measured gap on 18 live on-curve tokens: at most 9.3e-10.
      Measured gap on both graduated ones: **0.714**, five orders of magnitude past
      :data:`PONS_INVARIANT_TOLERANCE`. The separation is not marginal.

    The invariant is the more valuable of the two, because it does not only catch
    graduation — it catches any curve state this module has not understood.
    """
    try:
        quote_reserve = int(state.quote_reserve)
        token_reserve = int(state.token_reserve)
        real_quote = int(state.real_quote_reserve)
        threshold = int(state.graduation_threshold)
        sellable = int(state.sellable_tokens)
        launch_supply = int(state.launch_supply)
        phantom = int(state.phantom_quote)
        graduated = bool(state.graduated)
    except (AttributeError, TypeError, ValueError):
        return None, "pons_curve_state_unreadable"
    if graduated or sellable <= 0:
        return None, "pons_curve_complete"
    if quote_reserve <= 0 or token_reserve <= 0:
        return None, "pons_curve_reserves_empty"
    if token_decimals < 0 or token_decimals > 36 or quote_decimals < 0 or quote_decimals > 36:
        return None, "pons_decimals_unreadable"

    invariant_gap = _relative_gap(
        Decimal(quote_reserve) * Decimal(token_reserve),
        Decimal(phantom) * Decimal(launch_supply),
    )
    if invariant_gap is None or invariant_gap > PONS_INVARIANT_TOLERANCE:
        return None, f"pons_constant_product_failed:gap={invariant_gap}"

    try:
        # (quote base units / token atoms) * atoms-per-token / quote-base-per-whole-quote.
        price = (
            Decimal(quote_reserve)
            * (Decimal(10) ** token_decimals)
            / (Decimal(token_reserve) * (Decimal(10) ** quote_decimals))
        )
    except (InvalidOperation, DivisionByZero, ArithmeticError):
        return None, "pons_price_arithmetic_failed"
    if price <= 0:
        return None, "pons_priced_to_zero"
    progress = Decimal(real_quote) * 100 / Decimal(threshold) if threshold > 0 else None
    note = "pons curve"
    if progress is not None:
        note += f", progress {progress:.2f}%"
    try:
        fee = int(state.fee_bps)
    except (AttributeError, TypeError, ValueError):
        fee = None
    if fee is not None and not 0 <= fee < 10_000:
        fee = None
    # The fill arithmetic's inputs, from this same read (2026-10-23, hB_). Only with both takes read: an
    # unknown rate prices no fill (PonsCurve.refusal), it is never taken as zero.
    pons_curve: PonsCurve | None = None
    if fee is not None:
        try:
            pons_curve = PonsCurve(
                quote_reserve=quote_reserve, token_reserve=token_reserve, real_quote_reserve=real_quote,
                sellable_tokens=sellable, graduation_threshold=threshold, fee_bps=fee,
                creator_tax_bps=creator_tax_bps if isinstance(creator_tax_bps, int) else None,
                launched_at_s=int(state.launched_at_s), snipe_tax_start_bps=int(state.snipe_tax_start_bps),
                snipe_tax_seconds=int(state.snipe_tax_seconds), token_decimals=int(token_decimals),
            )
        except (AttributeError, TypeError, ValueError):
            pons_curve = None
    return (
        VenuePrice(
            price_quote_per_token=price,
            quote_token=quote_token,
            quote_reserve_base=real_quote,
            quote_decimals=quote_decimals,
            venue="pons",
            observed_ms=observed_ms if observed_ms is not None else int(state.observed_ms),
            note=note,
            progress_pct=progress,
            pool_id=state.curve,
            fee_bps=fee,
            creator_tax_bps=creator_tax_bps,
            pons_curve=pons_curve,
        ),
        "ok",
    )


def read_pons(token: str, rpc: RpcBatch) -> VenueRead:
    """Price one Robinhood token from its Pons curve. Two round trips cold, one warm.

    The cold trip asks the factory's ``getLaunchedToken`` which curve belongs to the token
    and what it is quoted in. Both are set at launch and never change, so they are cached
    and the warm path is one batched ``eth_call`` carrying all ten curve reads plus the
    token's ``decimals()``.

    2026-10-23: the same batch also carries the curve's ``creatorTaxBps()`` (LAST, so every index
    before it is unchanged), and the priced curve reports it with ``feeBps()``
    (:attr:`VenuePrice.fee_bps` / :attr:`VenuePrice.creator_tax_bps`). One more item in one
    request: no extra round trip or limiter reservation. Optional by construction: a missing,
    failed or out-of-range item leaves ``creator_tax_bps`` ``None`` and prices the curve exactly as
    before -- protection's mark never depends on it; the paper legs that do tag themselves
    not-honest when it is unknown.
    """
    try:
        from kaiba.ingest import robinhood as rh
    except Exception as exc:  # noqa: BLE001 - an absent ingest module is blindness
        return None, f"pons_module_unavailable:{type(exc).__name__}"
    try:
        addr = token.strip().lower()
        if not looks_evm(addr):
            return None, "not_an_evm_address"
        found = _PONS_CURVE.get(addr) or _pons_curve_from_registry(addr)
        if found is None and pons_absent_recently(addr):
            # Already asked, already answered: not on Pons. Spending a read to hear it
            # again is what put this bucket into permanent overdraft. The caller falls
            # through to the other venues exactly as it did before.
            return None, "pons_curve_absent_cached"
        if found is None:
            launched = rpc([_call(rh.FACTORY_V2, rh.SELECTOR_GET_LAUNCHED_TOKEN, addr)])
            raw = launched[0] if launched else None
            if raw is None:
                # The endpoint did not answer — rate limited, down, or refused. That is a
                # different fact from "this token has no curve", and conflating the two
                # told an operator a transient 429 was a permanent absence.
                return None, "pons_factory_read_failed"
            words = _words(raw)
            if len(words) < 5:
                return None, "pons_factory_record_short"
            curve = _word_address(words[1])
            if curve is None:
                # The factory ANSWERED and disowned the token. Unlike a failed read
                # that is a permanent fact, so write it down rather than buy it again
                # on the next tick. See PONS_ABSENT_TTL_MS.
                remember_pons_absent(addr)
                return None, "pons_curve_unknown"
            found = (curve, _word_address(words[4]))
            # The factory answered. Write it down: the next process, and every tick until
            # then, must not pay for this again on a bucket that cannot afford it.
            remember_pons_curve(addr, found[0], found[1])
            _PONS_ABSENT.pop(addr, None)
        _PONS_CURVE[addr] = found
        curve_address, quote_token = found

        calls: list[tuple[str, str]] = [
            _call(curve_address, selector) for _, selector in rh.CURVE_READS
        ]
        calls.append(_call(addr, SEL_DECIMALS))
        calls.append(_call(curve_address, PONS_CREATOR_TAX_SELECTOR))
        results = rpc(calls)
    except Exception as exc:  # noqa: BLE001 - a dead RPC is blindness, not a crash
        return None, f"pons_rpc_raised:{type(exc).__name__}"
    if len(results) < len(rh.CURVE_READS) + 1:
        return None, "pons_rpc_short_response"
    if all(value is None for value in results):
        return None, "pons_curve_read_failed"

    state = rh.parse_curve_state(
        results[: len(rh.CURVE_READS)],
        curve=curve_address,
        token=addr,
        quote_is_native=quote_token is None,
    )
    if state is None:
        return None, "pons_curve_unreadable"
    decimals = _hex_int(results[len(rh.CURVE_READS)])
    if decimals is None:
        return None, "pons_token_decimals_unreadable"
    quote_decimals = _quote_decimals(Chain.ROBINHOOD, quote_token, rpc)
    if quote_decimals is None:
        return None, "pons_quote_token_decimals_unreadable"
    tax_index = len(rh.CURVE_READS) + 1
    creator_tax = _hex_int(results[tax_index]) if len(results) > tax_index else None
    if creator_tax is not None and not 0 <= creator_tax < 10_000:
        creator_tax = None  # not a rate: unknown, never a number
    return pons_price(
        state,
        token_decimals=decimals,
        quote_decimals=quote_decimals,
        quote_token=quote_token,
        creator_tax_bps=creator_tax,
    )


# --------------------------------------------------------------------------------------
# quote token -> USD
# --------------------------------------------------------------------------------------

#: Decimals of a chain's native coin. Both chains this module reads are 18; the map exists
#: so that a chain whose native is not 18 cannot be added without noticing.
NATIVE_DECIMALS: dict[Chain, int] = {Chain.BSC: 18, Chain.ROBINHOOD: 18, Chain.ETH: 18, Chain.BASE: 18}


def _quote_decimals(chain: Chain, quote_token: str | None, rpc: RpcBatch) -> int | None:
    """Decimals of the quote token, read once from the chain and then remembered.

    ``None`` quote token means the chain's native coin, which has no ``decimals()`` to
    call. An unmapped chain returns ``None`` rather than assuming 18: the quote decimals
    scale the reserve, and a wrong scale is a depth reading out by a factor of 1e12.
    """
    if quote_token is None:
        return NATIVE_DECIMALS.get(chain)
    key = (chain.value, quote_token)
    cached = _DECIMALS.get(key)
    if cached is not None:
        return cached
    try:
        results = rpc([_call(quote_token, SEL_DECIMALS)])
    except Exception as exc:  # noqa: BLE001
        log.debug("quote token decimals read failed (%s)", type(exc).__name__)
        return None
    value = _hex_int(results[0]) if results else None
    if value is None or value < 0 or value > 36:
        return None
    _DECIMALS[key] = value
    return value


class QuoteUsd:
    """USD per whole unit of a quote token, from the existing DEX quote stack.

    Cached for :data:`QUOTE_USD_TTL_S` and **hard-expired** at
    :data:`QUOTE_USD_MAX_AGE_S`. Past the hard expiry a failed refresh returns ``None``.
    That is the whole point of this class existing rather than a plain memo: the obvious
    implementation — keep the last good value and serve it when the refresh fails — is the
    one that turns a dead provider into a watchdog that is confidently, silently wrong.
    """

    def __init__(
        self,
        *,
        price_usd: Callable[[Chain, str], Decimal | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        ttl_s: float = QUOTE_USD_TTL_S,
        max_age_s: float = QUOTE_USD_MAX_AGE_S,
    ) -> None:
        self._price_usd = price_usd or _provider_price_usd
        self._clock = clock
        self.ttl_s = float(ttl_s)
        self.max_age_s = float(max_age_s)
        self._cache: dict[tuple[str, str], tuple[Decimal, float]] = {}

    def usd(self, chain: Chain, quote_token: str | None) -> tuple[Decimal | None, str]:
        """USD per whole quote unit, and a note saying where it came from or why not."""
        if quote_token is None:
            reference = NATIVE_USD_REFERENCE.get(chain)
            if reference is None:
                return None, f"no native USD reference for {chain.value}"
            ref_chain, ref_token = reference
        else:
            ref_chain, ref_token = chain, quote_token
        key = (ref_chain.value, ref_token)
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None and (now - cached[1]) < self.ttl_s:
            return cached[0], "cached"
        try:
            fresh = self._price_usd(ref_chain, ref_token)
        except Exception as exc:  # noqa: BLE001 - a dead provider is data, not a crash
            log.debug("quote USD lookup raised (%s)", type(exc).__name__)
            fresh = None
        if isinstance(fresh, Decimal) and fresh > 0:
            self._cache[key] = (fresh, now)
            return fresh, "fresh"
        if cached is not None:
            age = now - cached[1]
            if age < self.max_age_s:
                return cached[0], f"refresh failed, reusing a {age:.0f}s old rate"
            self._cache.pop(key, None)
            return None, f"no USD rate for {ref_token} on {ref_chain.value}; last was {age:.0f}s old"
        return None, f"no USD rate for {ref_token} on {ref_chain.value}"


def _provider_price_usd(chain: Chain, token: str) -> Decimal | None:
    """The P1-3 quote stack, at EXIT priority. Never raises; ``None`` is a real answer."""
    try:
        from kaiba.providers import prices
    except Exception as exc:  # noqa: BLE001 - absent provider layer is blindness
        log.debug("price provider unimportable (%s)", type(exc).__name__)
        return None
    try:
        measure = prices.price_usd(chain, token, priority=Priority.EXIT)
    except TypeError:
        measure = prices.price_usd(chain, token)
    except Exception as exc:  # noqa: BLE001
        log.debug("price_usd raised (%s)", type(exc).__name__)
        return None
    receipt = getattr(measure, "receipt", None)
    if (receipt is None or receipt.basis in {EvidenceBasis.UNAVAILABLE, EvidenceBasis.STALE}
            or not 0 < receipt.observed_at_ms <= now_ms() or measure.stale):
        return None
    value = getattr(measure, "value", None) if getattr(measure, "known", False) else None
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


# --------------------------------------------------------------------------------------
# the RPC transports
# --------------------------------------------------------------------------------------


def _rpc_url(chain: Chain) -> str | None:
    """The operator's endpoint for a chain, or the public one. Never a credentialed guess."""
    try:
        from kaiba.core.config import get_settings

        configured = (get_settings().rpc_for(chain) or "").strip()
    except Exception as exc:  # noqa: BLE001 - unreadable settings are not a crash
        log.debug("could not read rpc_for(%s): %s", chain.value, exc)
        configured = ""
    if configured:
        return configured
    try:
        from kaiba.intelligence.dyor_verify import PUBLIC_RPC

        return PUBLIC_RPC.get(chain)
    except Exception as exc:  # noqa: BLE001
        log.debug("no public RPC table available: %s", exc)
        return None


def json_rpc_batch(
    chain: Chain,
    *,
    conn: Any = None,
    priority: Priority = Priority.EXIT,
    endpoint: str = "rpc.price",
    wait_for_slot_s: float = WAIT_FOR_SLOT_S,
    keep_reverts: bool = False,
    provider: str = "rpc",
) -> RpcBatch:
    """An :data:`RpcBatch` that sends every ``eth_call`` as one JSON-RPC batch.

    ``provider`` is the limiter key (default ``rpc``, every existing caller unchanged). A caller
    that is not a stop read -- the Flap-curve paper ENTRY (``evm_pool_price.
    bsc_curve_entry_transport``) -- passes its own, so its reservations and any 429 cooldown it
    earns never land on the key protection's sol and bsc stops share.

    One HTTP request means one limiter reservation, which is what makes a per-tick price
    read affordable: the watchdog polls every ``protection.poll_interval_s`` (5 s shipped)
    for every open position, and a call per curve field would be ten reservations per
    position per tick against a bucket that refills at one a second.

    ``priority`` defaults to ``EXIT`` for the same reason ``ProviderPriceSource`` uses it —
    a read that decides whether to close a position must outrank discovery competing for
    the same budget, or a busy scan makes the watchdog blind exactly when it matters.

    ``wait_for_slot_s`` is a parameter rather than only the module constant so that a
    *last-resort* caller can bound itself tighter than a first-choice one. The default is
    unchanged for every existing caller.

    ``keep_reverts`` (default off, so every existing caller is unchanged): a call the
    contract reverted WITH revert data comes back as :class:`RpcRevert` instead of ``None``.
    A failed request, a missing item and an error carrying no hex data are still ``None``.
    """
    url = _rpc_url(chain)

    def send(calls: Sequence[tuple[str, str]]) -> list[str | None]:
        if not calls:
            return []
        if not url:
            return [None] * len(calls)
        from kaiba.providers._http import post_json

        body = [
            {
                "jsonrpc": "2.0",
                "id": index + 1,
                "method": "eth_call",
                "params": [{"to": to, "data": data}, "latest"],
            }
            for index, (to, data) in enumerate(calls)
        ]
        fetched = post_json(
            provider,
            endpoint,
            url,
            json_body=body,
            priority=priority,
            ttl_s=0.0,  # a cached price is a stale price
            wait_for_slot_s=wait_for_slot_s,
            timeout_s=RPC_TIMEOUT_S,
            conn=conn,
        )
        if not fetched.ok or not isinstance(fetched.data, list):
            return [None] * len(calls)
        by_id: dict[int, Any] = {}
        for item in fetched.data:
            if isinstance(item, Mapping) and isinstance(item.get("id"), int):
                by_id[item["id"]] = item
        out: list[str | None] = []
        for index in range(len(calls)):
            item = by_id.get(index + 1)
            if not isinstance(item, Mapping):
                out.append(None)
                continue
            error = item.get("error")
            if error is not None:
                data = error.get("data") if isinstance(error, Mapping) else None
                if (
                    keep_reverts
                    and isinstance(data, str)
                    and data.startswith("0x")
                    and len(data) >= 10
                ):
                    out.append(RpcRevert(data.lower()))  # type: ignore[arg-type]
                else:
                    out.append(None)
                continue
            result = item.get("result")
            out.append(result if isinstance(result, str) else None)
        return out

    return send


def robinhood_rpc_batch(*, conn: Any = None, priority: Priority = Priority.EXIT) -> RpcBatch:
    """An :data:`RpcBatch` over :func:`kaiba.ingest.robinhood.rpc_batch`.

    Robinhood Chain gets its own transport rather than :func:`json_rpc_batch` because that
    module already owns this endpoint's pacing: it carries the ``robinhood-rpc`` provider
    key, the batching rule that keeps a poll to one round trip, and the hard-won lesson
    (``_MAX_TS_BLOCKS_PER_POLL``) about what happens to the whole ``chain.*`` family when
    this endpoint is over-called. Two modules pacing one endpoint independently is how that
    lesson gets unlearned.
    """

    def send(calls: Sequence[tuple[str, str]]) -> list[str | None]:
        if not calls:
            return []
        from kaiba.ingest.robinhood import rpc_batch

        got = rpc_batch(
            [("eth_call", [{"to": to, "data": data}, "latest"]) for to, data in calls],
            endpoint="chain.price",
            priority=priority,
            wait_for_slot_s=WAIT_FOR_SLOT_S,
            timeout_s=RPC_TIMEOUT_S,
            conn=conn,
        )
        results = list(got.results) if got.results else [None] * len(calls)
        return [r if isinstance(r, str) else None for r in results]

    return send


#: chain -> the venue reader that knows how to price a pre-graduation token on it.
VENUE_READERS: dict[Chain, Callable[[str, RpcBatch], VenueRead]] = {
    Chain.BSC: read_flap,
    Chain.ROBINHOOD: read_pons,
}


# --------------------------------------------------------------------------------------
# the price source the watchdog consumes
# --------------------------------------------------------------------------------------


def _curve_take(priced: Any) -> int | None:
    """``feeBps() + creatorTaxBps()`` of a priced Pons curve, or ``None`` when either was not read."""
    fee, tax = getattr(priced, "fee_bps", None), getattr(priced, "creator_tax_bps", None)
    if not isinstance(fee, int) or not isinstance(tax, int) or isinstance(fee, bool) or isinstance(tax, bool):
        return None
    take = fee + tax
    return take if 0 <= fee and 0 <= tax and take < 10_000 else None


class EvmVenuePriceSource:
    """``kaiba.execution.watchdog.PriceSource`` backed by EVM launch-venue reserves.

    Pre-graduation it prices from the venue's own curve; after graduation — or on a chain
    with no reader, or a token the venue has no record of — it hands the token to
    ``fallback``, which is the existing DEX quote stack. One source, both sides of
    graduation, which is what lets ``protection.price_source`` stay a single setting.
    """

    name = "evm-venue"

    def __init__(
        self,
        conn: sqlite3.Connection | None = None,
        *,
        readers: Mapping[Chain, Callable[[str, RpcBatch], VenueRead]] | None = None,
        transports: Mapping[Chain, RpcBatch] | None = None,
        quote_usd: QuoteUsd | None = None,
        fallback: Any | None = None,
    ) -> None:
        self.conn = conn
        self.readers = dict(readers if readers is not None else VENUE_READERS)
        self._transports = dict(transports or {})
        self.quote_usd = quote_usd or QuoteUsd()
        self.fallback = fallback

    def _transport(self, chain: Chain) -> RpcBatch:
        found = self._transports.get(chain)
        if found is None:
            found = (
                robinhood_rpc_batch(conn=self.conn)
                if chain is Chain.ROBINHOOD
                else json_rpc_batch(chain, conn=self.conn)
            )
            self._transports[chain] = found
        return found

    def quote(self, chain: Chain, token: str) -> Any:
        from kaiba.execution.watchdog import PriceQuote

        def _fallback(note: str) -> Any:
            """Hand over to the DEX stack, **keeping our own reason if it fails too**.

            The first version returned the fallback's quote untouched, and a blind
            Robinhood position then reported only "no pair prices this token" — the DEX
            stack's answer, which is true of every pre-graduation token and therefore says
            nothing. The reason the *venue* refused (a drained curve, an unreadable quote
            token, a 429) is the diagnostic, and it was being thrown away at exactly the
            moment someone needed it. A usable fallback quote is still passed through
            unchanged: there is nothing to explain when there is a price.
            """
            if self.fallback is None:
                return PriceQuote.unavailable(note, self.name)
            try:
                answer = self.fallback.quote(chain, token)
            except Exception as exc:  # noqa: BLE001 - a dead fallback is blindness
                return PriceQuote.unavailable(
                    f"{note}; fallback raised {type(exc).__name__}", self.name
                )
            if getattr(answer, "usable", False):
                return answer
            return PriceQuote.unavailable(
                f"{note}; fallback: {getattr(answer, 'note', None) or 'no price'}", self.name
            )

        if chain not in EVM_CHAINS:
            return _fallback(f"{chain.value} is not an EVM chain")
        reader = self.readers.get(chain)
        if reader is None:
            return _fallback(f"no venue reader for {chain.value}")
        try:
            priced, note = reader(token, self._transport(chain))
        except Exception as exc:  # noqa: BLE001 - a reader must never take the tick down
            log.warning("venue reader for %s raised: %s", chain.value, exc)
            return _fallback(f"venue reader raised {type(exc).__name__}")
        if priced is None:
            return _fallback(f"no venue curve: {note}")

        usd_per_quote, usd_note = self.quote_usd.usd(chain, priced.quote_token)
        if usd_per_quote is None:
            # The curve is readable and the price in quote units is exact; only the FX leg
            # is missing. Refusing here rather than reporting the quote-denominated number
            # as USD is the fail-closed half of this module.
            return _fallback(f"{priced.venue} curve priced but {usd_note}")
        try:
            price_usd = priced.price_quote_per_token * usd_per_quote
        except (InvalidOperation, ArithmeticError):
            return _fallback(f"{priced.venue} USD conversion failed")
        if price_usd <= 0:
            return _fallback(f"{priced.venue} curve priced to zero in USD")

        liquidity: Decimal | None = None
        reserve_whole = priced.quote_reserve_whole
        if reserve_whole is not None:
            try:
                liquidity = reserve_whole * usd_per_quote
            except (InvalidOperation, ArithmeticError):
                liquidity = None

        return PriceQuote(
            price_usd=price_usd,
            liquidity_usd=liquidity,
            basis=EvidenceBasis.DERIVED,
            observed_ms=priced.observed_ms,
            source=f"{self.name}:{priced.venue}",
            note=f"{priced.note}; quote {priced.quote_token or 'native'} @ {usd_note}",
            chain=chain,
            token=token,
            pool_id=priced.pool_id or (FLAP_PORTAL if priced.venue == "flap" else None),
            venue=priced.venue,
            liquidity_kind="curve_quote_reserve_usd",
            # The curve's own per-leg take, read in this same batch (Pons only; None otherwise):
            # what a paper exit sold on this curve pays (watchdog.DefaultExitSubmitter._paper).
            curve_take_bps=_curve_take(priced),
            # A native-quoted Flap curve's state from this same read, so a paper exit decided on this mark
            # is SOLD on this curve at our quantity (watchdog.DefaultExitSubmitter._paper_flap_curve_fill),
            # at no extra call. Not serialised (PriceQuote.venue_curve). 2026-10-23 (hA_).
            # A native-quoted Pons curve's state likewise (2026-10-23, hB_): its paper exit is sold on the
            # curve at our quantity, our holding put back (watchdog.DefaultExitSubmitter._paper_pons_curve_fill).
            venue_curve=(flap_curve_snapshot(priced.curve)
                         if priced.venue == "flap" and priced.quote_token is None and priced.curve is not None
                         else pons_curve_snapshot(priced.pons_curve)
                         if priced.venue == "pons" and priced.quote_token is None
                         and getattr(priced, "pons_curve", None) is not None
                         else None),
        )


class ChainRoutedPriceSource:
    """One price source per chain family, behind one ``PriceSource``.

    ``protection.price_source`` is a single string and the watchdog resolves it once for
    every position on every chain. So a source that only understands EVM would blind the
    eight live Solana positions the moment it was configured, and a source that only
    understands Solana is the state this change is fixing. This routes instead.
    """

    name = "venue"

    def __init__(self, routes: Mapping[Chain, Any], *, fallback: Any | None = None) -> None:
        self.routes = dict(routes)
        self.fallback = fallback

    def quote(self, chain: Chain, token: str) -> Any:
        from kaiba.execution.watchdog import PriceQuote

        source = self.routes.get(chain) or self.fallback
        if source is None:
            return PriceQuote.unavailable(f"no price source for {chain.value}", self.name)
        try:
            return source.quote(chain, token)
        except Exception as exc:  # noqa: BLE001 - a routed source must not take the tick down
            log.warning("%s price source raised: %s", chain.value, exc)
            return PriceQuote.unavailable(
                f"{chain.value} price source raised {type(exc).__name__}", self.name
            )


def evm_price_source() -> EvmVenuePriceSource:
    """EVM venues only, with the DEX stack after graduation.

    Registered as ``PRICE_SOURCES["evm"]``. Prefer :func:`venue_price_source` in
    ``config/risk.yaml``: this one is blind on Solana by construction.
    """
    conn = _conn()
    return EvmVenuePriceSource(conn, fallback=_dex_fallback())


def venue_price_source() -> ChainRoutedPriceSource:
    """**The one to configure.** Curve reserves on every venue we can read, DEX after.

    Wiring requested (``watchdog.PRICE_SOURCES``, a file this change does not own)::

        from kaiba.execution.evm_price import evm_price_source, venue_price_source
        PRICE_SOURCES["evm"] = evm_price_source
        PRICE_SOURCES["venue"] = venue_price_source

    then ``protection.price_source: venue`` in ``config/risk.yaml``. Solana gets the
    :func:`kaiba.execution.curve_price.curve_price_source` object it has always had, with
    an executable router quote behind it (see :func:`_sol_price_source`), and BSC and
    Robinhood stop being blind.

    **BSC gets a source of its own**, because its fallback is a chain and Robinhood's is
    not. ``EvmVenuePriceSource`` carries one ``fallback``, and the two chains were sharing
    an instance; widening that instance would have handed Pons a BSC-only router it can
    never use, and put a refusal it cannot act on into every Robinhood blind reason. The
    readers, the transports and the reader table are identical — only the fallback differs.
    """
    conn = _conn()
    fallback = _dex_fallback()
    routes: dict[Chain, Any] = {}
    sol = _sol_price_source()
    if sol is not None:
        routes[Chain.SOL] = sol
    evm = EvmVenuePriceSource(conn, fallback=fallback)
    for chain in VENUE_READERS:
        routes[chain] = evm
    rh = _robinhood_after_curve(conn, fallback)
    if rh is not None:
        routes[Chain.ROBINHOOD] = rh
    bsc = _bsc_after_curve(conn, fallback)
    if bsc is not None:
        routes[Chain.BSC] = bsc
    # eth / base (design step 6d): the token's own pool and nothing that is not independent of it.
    # ALWAYS routed -- to a refusal if the leaf cannot be built -- so these chains can never fall
    # through to the DexScreener fallback below (MEASURED in the design: $435,628 of DexScreener
    # liquidity where GMGN showed $321, no pair at all for new Zora / Virtuals coins, up to 1,355x apart).
    for chain in sorted(_pool_only_chains(), key=lambda c: c.value):
        routes[chain] = _pool_only_route(conn, chain)
    return ChainRoutedPriceSource(routes, fallback=fallback)


def _pool_only_chains() -> frozenset[Chain]:
    try:
        from kaiba.execution.evm_pool_price import POOL_ONLY_CHAINS

        return POOL_ONLY_CHAINS
    except Exception as exc:  # noqa: BLE001 - the chains still never reach DexScreener (refused below)
        log.warning("pool-only chains unreadable, eth/base routed to a refusal: %s", exc)
        return frozenset({Chain.ETH, Chain.BASE})


class _RefusingSource:
    """A route that only refuses, naming why: what eth / base get if their pool leaf cannot be built."""

    name = "pool-only-unavailable"

    def __init__(self, why: str) -> None:
        self.why = why

    def quote(self, chain: Chain, token: str) -> Any:
        from kaiba.execution.watchdog import PriceQuote

        return PriceQuote.unavailable(self.why, self.name)


def _pool_only_route(conn: Any, chain: Chain) -> Any:
    """eth / base: ``evm_pool_price.PoolPriceSource`` (one ``aggregate3`` per tick for the chain's
    book, its own key and host), then the sampled Codex.io aggregate for corroboration ONLY. No
    DexScreener and no curve. The box's top-level chain (``watchdog._venue_price_source``) still
    asks GMGN after this route refuses: an aggregator on its own provider, so independent of the
    pool for ``Watchdog._validate_quote``, and the paper EXIT is filled from the pool regardless."""
    try:
        from kaiba.execution.evm_pool_price import BackupPriceSource, PoolPriceSource, codex_backup_quote
        from kaiba.execution.watchdog import FallbackPriceSource
    except Exception as exc:  # noqa: BLE001 - refuse, never DexScreener
        log.warning("no pool leaf for the %s price source: %s", chain.value, exc)
        return _RefusingSource(f"{chain.value} pool leaf unavailable: {type(exc).__name__}")
    return FallbackPriceSource(PoolPriceSource(conn, chain=chain), BackupPriceSource(codex_backup_quote),
                               name=f"{chain.value}-pool-only")


def _robinhood_after_curve(conn: Any, fallback: Any) -> Any | None:
    """Pons' curve, then the on-chain pool SAMPLE, then the DEX stack; the backup (Codex.io) is
    listed last for corroboration ONLY (``watchdog.corroboration_only``: never a tick's price).

    2026-10-21 (design step 3a [R]): MEASURED, 54 of 62 robinhood paper exits were priced off
    DexScreener. ``onchain_pool.OnchainPriceLog`` (``protection.onchain_price_log: true`` on the
    box) already reads every open robinhood position's pool once per tick on ``robinhood-rpc``,
    so this adds ZERO calls on that host: ``evm_pool_price.OnchainSampleSource`` answers from its
    newest sample, at most two poll intervals old, else refuses and DexScreener answers as before.
    ``None`` when the chain cannot be built, which leaves robinhood exactly as it was.
    """
    try:
        from kaiba.execution.evm_pool_price import BackupPriceSource, OnchainSampleSource, codex_backup_quote
        from kaiba.execution.watchdog import FallbackPriceSource
    except Exception as exc:  # noqa: BLE001 - keep the curve and the DEX stack rather than losing RH
        log.warning("no on-chain sample leaf for the robinhood price source: %s", exc)
        return None
    return EvmVenuePriceSource(
        conn,
        fallback=FallbackPriceSource(OnchainSampleSource(conn), fallback, BackupPriceSource(codex_backup_quote),
                                     name="rh-after-curve"),
    )


def _bsc_after_curve(conn: Any, fallback: Any) -> Any | None:
    """Flap's curve, then the DEX stack, then an executable Pancake V2 quote.

    ``None`` when the chain cannot be built, which leaves BSC exactly as it was rather
    than losing the two layers it already had.
    """
    router = _bsc_router_last_resort(conn)
    if router is None:
        return None
    try:
        from kaiba.execution.watchdog import FallbackPriceSource
    except Exception as exc:  # noqa: BLE001 - keep the curve rather than losing BSC
        log.warning("no fallback chain for the bsc price source: %s", exc)
        return None
    # 2026-10-21 (design step 3): the token's own WBNB pool BEFORE DexScreener. MEASURED, 19 of 19
    # bsc paper exits were priced off DexScreener. ``evm_pool_price.PoolPriceSource`` reads every
    # bsc position's pool in one aggregate3 per tick (``Watchdog._prime_sources``) on its own key
    # and host (``bsc-pool-rpc`` / ``BSC_POOL_RPC_URL``); with no host configured it refuses at once
    # and the chain is exactly what it was. The backup leaf (Codex.io's aggregate, sampled) is the
    # independent corroboration ``Watchdog._validate_quote`` needs behind a pool; it is
    # corroboration-ONLY (``watchdog.corroboration_only``), never asked for a tick's price and never
    # remembered, because one answer an hour would set a quote identity GMGN's next stop could not
    # follow (step 3 review 2026-10-23, blocking #3).
    pool = backup = None
    try:
        from kaiba.execution.evm_pool_price import BackupPriceSource, PoolPriceSource, codex_backup_quote

        # The backup is Codex.io, sampled (evm_pool_price.CODEX_BACKUP_MAX_PER_HOUR), never per tick.
        pool, backup = PoolPriceSource(conn, chain=Chain.BSC), BackupPriceSource(codex_backup_quote)
    except Exception as exc:  # noqa: BLE001 - the pool leaf is an upgrade, never a loss
        log.warning("no pool leaf for the bsc price source: %s", exc)
    return EvmVenuePriceSource(
        conn,
        fallback=FallbackPriceSource(pool, fallback, router, backup, name="bsc-after-curve"),
    )


#: How long the router last resort may wait for limiter capacity before refusing.
#:
#: MEASURED 2026-09-22: four mints quoted concurrently at ``Priority.EXIT`` the way
#: ``Watchdog._prefetch_quotes`` quotes them, on the shipped ``jupiter`` budget
#: (``min_interval_ms: 1100``, ``refill_per_s: 1.0``, ``max_inflight: 2``), took **2356 ms
#: and 2362 ms wall and priced 4 of 4** at this setting. The tick budget is
#: ``protection.poll_interval_s`` = 5000 ms.
#:
#: DERIVED from that: it must be bounded well under the tick. ``jupiter``'s own
#: ``DEFAULT_WAIT_FOR_SLOT_S`` is 10.0 s, which would park a prefetch worker for two whole
#: ticks when the limiter is cold — and ``watchdog.OVERRUN_HALT_TICKS`` halts entries on
#: **every** chain after three consecutive ticks at twice the budget. Refusing a price is
#: survivable; overrunning protection is the failure this fix must not trade for.
ROUTER_LAST_RESORT_WAIT_FOR_SLOT_S = 2.0


def _sol_price_source() -> Any | None:
    """Curve reserves, then the DEX stack, then an executable router quote. Or nothing.

    THE 2026-09-22 HOLE. The Solana route was one source: ``curve_price_source()``, which
    is the bonding curve with DexScreener behind it. Both refuse — correctly — for a
    **launch quoted in something other than SOL**. MEASURED on the live mint
    ``FnkzzU3t55RQNbjc6Jynn7LHrPEbRTnBenvHQwJCebYB``, a ``letsbonk.fun`` /
    ``raydium_launchpad`` coin whose ``quote_mint`` is
    ``Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk`` at 6 decimals:

    * ``scanner.curve_from_payload`` returns ``(None, 'non_sol_quote:Dz9mQ9NzkBcC')``.
      Right: those reserves are not lamports, and reading them as lamports would put a
      confident wrong number on a stop.
    * DexScreener returns ``pairs: null``. Right: the coin is still on its curve
      (``complete: false``), so it has no pair.

    Two correct refusals and no third layer is a live position with no stop. Jupiter routes
    it in two hops (``SOL -> BONK`` then ``BONK -> token`` on Raydium Launchlab) and prices
    it **executably** — the price a real sell would get, which is a better basis for a stop
    than any mid price — in a MEASURED 570-611 ms.

    It is deliberately LAST. The curve is ground truth pre-graduation and costs no router
    call, so a priceable position never pays for this. And it is deliberately a *router*
    rather than a second curve reader: Raydium Launchlab's geometry plus a BONK/USD
    conversion is arithmetic we have not verified against the chain, and an unverified
    price on a stop is worse than no price.

    ``with_liquidity=False`` drops the second ``/price/v3`` round trip. MEASURED: with it
    on, **1 of 4** mints lost its price entirely at this slot budget. ``PriceQuote`` says
    the rug monitor may be unavailable and the stop may not — and a position that has no
    price has no liquidity figure either, so nothing is lost that we have today.

    **The Solana route stays the** :class:`~kaiba.execution.curve_price.CurvePriceSource`
    **object itself**, with its ``fallback`` widened from one source to a chain. It is not
    wrapped, because ``tests/test_curve_price_priority.py`` pins that
    ``venue_price_source().routes[Chain.SOL].resolver`` is the curve resolver reading at
    ``Priority.EXIT`` — that is a property of this wiring worth keeping, not an accident.
    """
    curve: Any | None = None
    try:
        from kaiba.execution.curve_price import curve_price_source

        # EXIT, like every EVM venue read in this module: the venue source exists for
        # the exit watchdog, and a protection read never queues behind discovery.
        curve = curve_price_source(priority=Priority.EXIT)
    except Exception as exc:  # noqa: BLE001 - losing SOL to an import error is unacceptable
        log.warning("solana curve price source unavailable: %s", exc)
    router = _router_last_resort()
    if curve is None:
        return router
    if router is None:
        return curve
    try:
        from kaiba.execution.watchdog import FallbackPriceSource
    except Exception as exc:  # noqa: BLE001 - keep the curve rather than losing SOL
        log.warning("no fallback chain for the solana price source: %s", exc)
        return curve
    # ``CurvePriceSource.fallback`` is documented as "another PriceSource consulted when
    # there is no curve". One was the hole; this makes it a chain, in order.
    curve.fallback = FallbackPriceSource(curve.fallback, router, name="sol-after-curve")
    return curve


def _router_last_resort() -> Any | None:
    """Jupiter as the layer of last resort. ``None`` when it cannot be built.

    Solana only by construction: ``jupiter.executable_price`` refuses every other chain,
    which is why this is wired into the Solana route and nowhere else.
    """
    try:
        from kaiba.providers.jupiter import JupiterPriceSource

        return JupiterPriceSource(
            with_liquidity=False,
            priority=Priority.EXIT,
            wait_for_slot_s=ROUTER_LAST_RESORT_WAIT_FOR_SLOT_S,
        )
    except Exception as exc:  # noqa: BLE001 - an absent router is one less layer, not a crash
        log.warning("no router last resort for the solana price source: %s", exc)
        return None


# --------------------------------------------------------------------------------------
# the BSC last resort: an executable PancakeSwap V2 quote for a GRADUATED token
# --------------------------------------------------------------------------------------
#
# THE 2026-09-22 HOLE, and it is the Solana hole above with different spelling. The BSC
# route of :func:`venue_price_source` was two layers — Flap's curve, then DexScreener —
# and both refuse, correctly, for a token that has **graduated off** its curve. Measured
# over 20 minutes on the live box, one BSC position went blind on every tick:
#
#     27x  0xb4dcd30148ee  "no venue curve: flap_not_on_curve:status=4; fallback: ..."
#      3x  0x252cc0200078  same
#
# ``status=4`` is graduation (:class:`FlapRecord`). :func:`read_flap` is right to refuse —
# there is no curve left to read — and a position with no price has no stop.
#
# Where a graduated Flap token actually lives, MEASURED 2026-09-22 from
# ``https://bsc-dataseed.bnbchain.org``: of thirteen Flap addresses already in this tree,
# five report ``status=4``, and the pair in **word 14** of all five answers ``factory()``
# with :data:`PANCAKE_V2_FACTORY`. **5 of 5 graduate into PancakeSwap V2.** So the router
# that can price them is the one holding the pool, read with a plain ``eth_call`` — no API
# key, no new provider budget, no subprocess, and the transport :func:`read_flap` already
# uses. It priced 5 of 5: three through ``[WBNB, token]`` and two through
# ``[WBNB, quoteToken, token]``, where ``quoteToken`` is word 9 of the token's own record.
# Neither of those two quote tokens is WBNB or USDT, so a fixed list of intermediates
# would have missed both — the venue's own record is what finds the route.
#
# Why not the alternatives that were considered:
#
# * ``gmgn_cli`` — excluded by construction. It spawns a subprocess with a 45 s timeout,
#   against a 5000 ms protection tick.
# * a 1inch/0x-style aggregator HTTP quote — would add a keyed provider, a new limiter
#   budget and a second pacing authority for a route that is one ``eth_call`` away.
#
# It refuses rather than guesses, MEASURED in the same batch: an **on-curve** Flap token
# reverts ``PancakeLibrary: INSUFFICIENT_LIQUIDITY`` and an address with no pair reverts
# ``execution reverted: 0x``. A revert arrives through :data:`RpcBatch` as ``None``, which
# becomes a named refusal here. There is no branch that invents a number.

#: PancakeSwap V2 Router02 on BSC — the contract ``getAmountsOut`` is read from.
#:
#: MEASURED 2026-09-22: this address answered ``getAmountsOut`` for all five graduated
#: Flap tokens sampled, and reverted for the on-curve and the non-existent ones.
PANCAKE_V2_ROUTER = "0x10ed43c718714eb63d5aa57b78b54704e256024e"

#: The factory whose pairs the router above can read.
#:
#: MEASURED 2026-09-22: ``factory()`` on the pair recorded in word 14 of all five
#: graduated Flap records returned exactly this address — 5 of 5, no other factory seen.
#: It is named rather than inlined because it is the *evidence* for choosing this router:
#: a venue that starts graduating somewhere else will stop matching it, which is a
#: discoverable fact rather than a silent mispricing by a router that holds no pool.
PANCAKE_V2_FACTORY = "0xca143ce32fe78f1f7019d7d551a6402fc5350c73"

#: DEFINITIONAL: the 4-byte selector of ``getAmountsOut(uint256,address[])``.
SEL_GET_AMOUNTS_OUT = "0xd06ca61f"

#: STRUCTURAL: WBNB, taken from :data:`NATIVE_USD_REFERENCE` rather than typed again.
#: One spelling of wrapped native in this module; a second one is how two code paths come
#: to price different tokens while appearing to agree.
BSC_WRAPPED_NATIVE = NATIVE_USD_REFERENCE[Chain.BSC][1]

#: How much WBNB the executable probe buys, in wei. 5e15 = 0.005 BNB.
#:
#: DERIVED from ``jupiter.PROBE_LAMPORTS`` (0.02 SOL), which is the same order of USD, and
#: MEASURED 2026-09-22 against it: across the five live graduated pools, the price quoted
#: at this size sits at most **+0.135%** (13.5 bps) above the near-mid
#: :data:`ROUTER_REFERENCE_WEI` probe. At 1e17 wei the same pools drift up to +2.70%,
#: which is why the probe is not larger; the shipped ``stop_loss_bps`` is 3000, so 13.5
#: bps of probe distortion is noise against the thing this price has to decide.
ROUTER_PROBE_WEI = 5 * 10**15

#: A near-mid reference probe, 1e12 wei, sent in the SAME batch as the real one.
#:
#: DERIVED: it costs no extra round trip and no extra limiter reservation, and it is the
#: only way to know whether the pool is deep enough for the answer to mean anything. A
#: layer that skipped it would be guessing about depth rather than measuring it.
ROUTER_REFERENCE_WEI = 10**12

#: How far the real probe may sit above the reference before the quote is REFUSED.
#:
#: DERIVED, with MEASURED headroom. A ~$4 probe that moves the pool 20% implies roughly
#: $40 of WBNB on that side of it; a mark taken from that is not a price, and the shipped
#: ``stop_loss_bps`` is 3000, so a distortion approaching that size cannot evaluate the
#: stop it is supposed to feed. MEASURED worst case across the five live graduated pools
#: at :data:`ROUTER_PROBE_WEI`: **13.5 bps**, which is 148x below this cap. The separation
#: is not marginal. A negative excursion of the same size is refused too: a larger probe
#: cannot get a *better* price on a constant product, so if it did, the two answers did
#: not come from one pool and neither may become a stop.
ROUTER_MAX_PROBE_IMPACT_BPS = 2000

#: token -> the quote token of its Flap launch (``None`` = native, or no Flap record).
#:
#: Fixed at launch, like :data:`_PONS_CURVE`, so this is a cache of an immutable fact and
#: never expires. It is populated only when the portal actually answered: caching a failed
#: read as "no hop" would make one 429 a permanently unroutable position.
_FLAP_QUOTE_TOKEN: dict[str, str | None] = {}

#: ONE BNB/USD rate for the whole process, shared by every router instance.
#:
#: MEASURED 2026-09-22, and this is not an optimisation — it is the difference between the
#: layer working and not. ``Watchdog._prefetch_quotes`` builds a **fresh price source per
#: worker per tick** (``prefetch_source_factory``), so a per-instance
#: :class:`QuoteUsd` starts cold on every worker and every tick: N positions means N
#: identical BNB/USD lookups a tick, all against the same ``dexscreener`` budget the
#: position quotes are already spending. Quoting the five live graduated tokens
#: concurrently through ``venue_price_source()`` priced only **3-4 of 5**, and the refusal
#: was never the route — it was
#: ``router priced via wbnb but no USD rate for 0xbb4cdb9cbd36b01bd1...``, with
#: ``rate limited: dexscreener: minimum interval`` right beside it. The route had been
#: found and was being thrown away for want of an FX rate the process already had.
#: Sharing the cache made it **5 of 5 on 3 of 3 trials**.
#:
#: The rate is a property of the chain, not of a position or a worker, so one cache is
#: also the honest place to hold it. It keeps :class:`QuoteUsd`'s expiry rules exactly:
#: :data:`QUOTE_USD_TTL_S` before a refresh, :data:`QUOTE_USD_MAX_AGE_S` before it expires
#: into blindness rather than into a stale number. A dict get/set race between workers
#: costs one duplicate fetch and nothing else.
#:
#: FURTHER, and deliberately NOT taken here: :func:`kaiba.providers.native_price.at` is a
#: DB-backed sample series with the same 300 s rule, shared across *processes* as well as
#: threads. Whether its sampler runs on the live box is UNMEASURED from here, so it is
#: named as the better source rather than shipped on an assumption.
_BSC_NATIVE_USD = QuoteUsd()


def encode_amounts_out(amount_in: int, path: Sequence[str]) -> tuple[str, str]:
    """``getAmountsOut(amountIn, path)`` as a ``(to, data)`` pair for :data:`RpcBatch`.

    The dynamic ``address[]`` is tail-encoded: the head is ``amountIn`` then the offset
    ``0x40``, and the tail is the length followed by one word per hop.
    """
    data = SEL_GET_AMOUNTS_OUT + format(int(amount_in), "064x") + format(0x40, "064x")
    data += format(len(path), "064x")
    data += "".join(str(hop).strip().lower()[2:].rjust(64, "0") for hop in path)
    return (PANCAKE_V2_ROUTER, data)


def decode_amounts_out(raw: Any, *, hops: int) -> int | None:
    """The LAST amount of the returned ``uint256[]``, or ``None`` when it is not one.

    ``None`` — never 0 — for every shape that is not an amounts array of exactly the
    length the path asked for. A reply of the wrong length is not a short answer to our
    question, it is an answer to a different one, and the difference between "no route"
    and "a number I did not understand" must not be resolved by taking the number.
    """
    words = _words(raw)
    expected = int(hops) + 1
    if expected < 2 or len(words) < 2 + expected:
        return None
    if words[0] != 0x20 or words[1] != expected:
        return None
    return words[1 + expected]


def router_price(
    probe_out_atoms: int | None,
    reference_out_atoms: int | None,
    *,
    token_decimals: int,
    probe_wei: int = ROUTER_PROBE_WEI,
    reference_wei: int = ROUTER_REFERENCE_WEI,
    max_impact_bps: int = ROUTER_MAX_PROBE_IMPACT_BPS,
) -> tuple[Decimal | None, int | None, str]:
    """Two probes -> ``(native per whole token, impact bps, reason)``. Pure arithmetic.

    No network, so every refusal below is reachable in a test. ``price`` is ``None``
    whenever the answer would be a guess, and ``reason`` is always named.
    """
    if token_decimals is None or token_decimals < 0 or token_decimals > 36:
        return None, None, f"router_token_decimals_unusable:{token_decimals}"
    if probe_out_atoms is None:
        return None, None, "router_no_route"
    if int(probe_out_atoms) <= 0:
        return None, None, "router_route_returns_nothing"
    if reference_out_atoms is None or int(reference_out_atoms) <= 0:
        # The impact figure is the only depth measurement this layer has. Without it the
        # price would be reported with nothing behind it, which is the guess.
        return None, None, "router_reference_probe_failed"
    try:
        scale = Decimal(10) ** int(token_decimals)
        native = Decimal(10) ** NATIVE_DECIMALS[Chain.BSC]
        price = Decimal(int(probe_wei)) * scale / (Decimal(int(probe_out_atoms)) * native)
        reference = (
            Decimal(int(reference_wei)) * scale / (Decimal(int(reference_out_atoms)) * native)
        )
    except (InvalidOperation, DivisionByZero, ArithmeticError):
        return None, None, "router_price_arithmetic_failed"
    if price <= 0 or reference <= 0:
        return None, None, "router_priced_to_zero"
    try:
        impact_bps = int((price / reference - 1) * 10000)
    except (InvalidOperation, DivisionByZero, ArithmeticError):
        return None, None, "router_impact_arithmetic_failed"
    if abs(impact_bps) > int(max_impact_bps):
        return None, impact_bps, f"router_probe_impact_{impact_bps}bps_over_{max_impact_bps}"
    return price, impact_bps, "ok"


class PancakeRouterPriceSource:
    """``watchdog.PriceSource`` backed by an executable PancakeSwap V2 router quote.

    BSC only by construction — :data:`PANCAKE_V2_ROUTER` is a BSC contract — which is why
    this is wired into the BSC route and nowhere else.

    It is deliberately LAST. Flap's curve is ground truth before graduation and costs no
    router call, and DexScreener already prices the graduated tokens it has indexed, so a
    position that is priceable today never pays for this.

    What it deliberately does NOT report, because it did not measure it:

    * ``liquidity_usd`` — the same trade the Solana last resort makes (``with_liquidity=
      False``). ``PriceQuote`` says the rug monitor may be unavailable and the stop may
      not, and these positions have no liquidity figure today because they have no price.
    * ``executable_quote_usd`` — ``protection.py`` documents it as "the **per-unit price
      an actual sell quote would fill at**, not a notional", and this probe is a BUY
      probe. Putting the probe's notional there would hand the anti-wick rule a number
      that is not the thing it divides by ``price_usd``.

    UNVERIFIED, and stated rather than hidden: ``getAmountsOut`` is constant-product
    arithmetic on the pair's reserves plus the router's 25 bps fee. It does not model a
    **transfer tax**, so on a taxed token a realised sell differs from this mark by the
    tax. What would settle it: replaying a live Flap sell against the pair's reserves at
    ``block - 1``, exactly as :class:`kaiba.execution.viability.PonsCurveDepth` was.
    """

    name = "pancake-v2"

    def __init__(
        self,
        conn: Any = None,
        *,
        transport: RpcBatch | None = None,
        quote_usd: QuoteUsd | None = None,
        probe_wei: int = ROUTER_PROBE_WEI,
        reference_wei: int = ROUTER_REFERENCE_WEI,
        max_impact_bps: int = ROUTER_MAX_PROBE_IMPACT_BPS,
        priority: Priority = Priority.EXIT,
        wait_for_slot_s: float = ROUTER_LAST_RESORT_WAIT_FOR_SLOT_S,
    ) -> None:
        self.conn = conn
        self._transport_fn = transport
        # NOT ``QuoteUsd()``: see :data:`_BSC_NATIVE_USD`. A per-instance cache is cold on
        # every prefetch worker on every tick, and that cost 1-2 of 5 live routes.
        self.quote_usd = quote_usd or _BSC_NATIVE_USD
        self.probe_wei = int(probe_wei)
        self.reference_wei = int(reference_wei)
        self.max_impact_bps = int(max_impact_bps)
        self.priority = priority
        self.wait_for_slot_s = float(wait_for_slot_s)

    def _transport(self) -> RpcBatch:
        if self._transport_fn is None:
            self._transport_fn = json_rpc_batch(
                Chain.BSC,
                conn=self.conn,
                priority=self.priority,
                endpoint="rpc.router",
                wait_for_slot_s=self.wait_for_slot_s,
            )
        return self._transport_fn

    def _paths(self, token: str) -> list[tuple[str, list[str]]]:
        """The routes worth asking about, most direct first.

        Direct first because 3 of the 5 live graduated tokens pair with WBNB and must not
        pay for a hop probe they do not need — and because a shorter path is a strictly
        better execution than a longer one at the same price.
        """
        paths: list[tuple[str, list[str]]] = [("wbnb", [BSC_WRAPPED_NATIVE, token])]
        quote = _FLAP_QUOTE_TOKEN.get(token)
        if quote and quote != token and quote != BSC_WRAPPED_NATIVE:
            paths.append((f"wbnb>{quote}", [BSC_WRAPPED_NATIVE, quote, token]))
        return paths

    def _priced(
        self, probe: Any, reference: Any, *, hops: int, token_decimals: int
    ) -> tuple[Decimal | None, int | None, str]:
        return router_price(
            decode_amounts_out(probe, hops=hops),
            decode_amounts_out(reference, hops=hops),
            token_decimals=token_decimals,
            probe_wei=self.probe_wei,
            reference_wei=self.reference_wei,
            max_impact_bps=self.max_impact_bps,
        )

    def _route(self, token: str) -> tuple[tuple[Decimal, int | None, str] | None, str]:
        """One batched ``eth_call`` warm, at most two cold. Never a cached price."""
        rpc = self._transport()
        calls: list[tuple[str, str]] = []

        decimals = _DECIMALS.get((Chain.BSC.value, token))
        decimals_at = None
        if decimals is None:
            decimals_at = len(calls)
            calls.append(_call(token, SEL_DECIMALS))
        portal_at = None
        if token not in _FLAP_QUOTE_TOKEN:
            portal_at = len(calls)
            calls.append(_call(FLAP_PORTAL, SEL_GET_TOKEN_V8_SAFE, token))

        paths = self._paths(token)
        probes_at: dict[str, int] = {}
        for label, path in paths:
            probes_at[label] = len(calls)
            calls.append(encode_amounts_out(self.probe_wei, path))
            calls.append(encode_amounts_out(self.reference_wei, path))

        results = rpc(calls)
        if not isinstance(results, list) or len(results) < len(calls):
            return None, "router_rpc_short_response"

        if decimals_at is not None:
            decimals = _hex_int(results[decimals_at])
            if decimals is None or decimals < 0 or decimals > 36:
                # Decimals scale the price. A wrong scale is a mark out by 1e12.
                return None, "router_token_decimals_unreadable"
            _DECIMALS[(Chain.BSC.value, token)] = decimals
        if portal_at is not None and results[portal_at] is not None:
            record = flap_record(_words(results[portal_at]))
            _FLAP_QUOTE_TOKEN[token] = record.quote_token if record is not None else None

        refusals: list[str] = []
        for label, path in paths:
            at = probes_at[label]
            price, impact, reason = self._priced(
                results[at], results[at + 1], hops=len(path) - 1, token_decimals=decimals
            )
            if price is not None:
                return (price, impact, label), "ok"
            refusals.append(f"{label}={reason}")

        # The portal answer above may have just named a hop the batch could not know about
        # when it was built. One more batch, never a loop.
        tried = {label for label, _ in paths}
        for label, path in self._paths(token):
            if label in tried:
                continue
            got = rpc(
                [
                    encode_amounts_out(self.probe_wei, path),
                    encode_amounts_out(self.reference_wei, path),
                ]
            )
            if not isinstance(got, list) or len(got) < 2:
                refusals.append(f"{label}=router_rpc_short_response")
                continue
            price, impact, reason = self._priced(
                got[0], got[1], hops=len(path) - 1, token_decimals=decimals
            )
            if price is not None:
                return (price, impact, label), "ok"
            refusals.append(f"{label}={reason}")

        return None, "no pancake v2 route: " + "; ".join(refusals)

    def quote(self, chain: Chain, token: str) -> Any:
        from kaiba.execution.watchdog import PriceQuote

        def refuse(note: str) -> Any:
            return PriceQuote.unavailable(note, self.name)

        if chain is not Chain.BSC:
            return refuse(f"pancake v2 routes bsc only, not {chain.value}")
        addr = str(token or "").strip().lower()
        if not looks_evm(addr):
            return refuse("not_an_evm_address")
        try:
            found, reason = self._route(addr)
        except Exception as exc:  # noqa: BLE001 - the LAST layer must never take a tick down
            log.warning("pancake router raised for %s: %s", addr, exc)
            return refuse(f"router_raised:{type(exc).__name__}")
        if found is None:
            return refuse(reason)
        price_native, impact_bps, label = found

        usd_per_bnb, usd_note = self.quote_usd.usd(Chain.BSC, None)
        if usd_per_bnb is None:
            # The route is exact and only the FX leg is missing. Reporting the
            # BNB-denominated number as USD is the failure this refusal exists to prevent.
            return refuse(f"router priced via {label} but {usd_note}")
        try:
            price_usd = price_native * usd_per_bnb
        except (InvalidOperation, ArithmeticError):
            return refuse(f"router {label} USD conversion failed")
        if price_usd <= 0:
            return refuse(f"router {label} priced to zero in USD")

        return PriceQuote(
            price_usd=price_usd,
            liquidity_usd=None,
            executable_quote_usd=None,
            basis=EvidenceBasis.DERIVED,
            observed_ms=now_ms(),
            source=f"{self.name}:{label}",
            note=(
                f"executable at {self.probe_wei} wei of WBNB via {label}, "
                f"impact {impact_bps} bps; BNB @ {usd_note}"
            ),
        )


def _bsc_router_last_resort(conn: Any = None) -> Any | None:
    """PancakeSwap V2 as the BSC layer of last resort. ``None`` when it cannot be built."""
    try:
        return PancakeRouterPriceSource(
            conn,
            priority=Priority.EXIT,
            wait_for_slot_s=ROUTER_LAST_RESORT_WAIT_FOR_SLOT_S,
        )
    except Exception as exc:  # noqa: BLE001 - an absent router is one less layer, not a crash
        log.warning("no router last resort for the bsc price source: %s", exc)
        return None


def _conn() -> Any:
    try:
        from kaiba.core.db import get_conn

        return get_conn()
    except Exception as exc:  # noqa: BLE001 - the limiter falls back to its own connection
        log.warning("no db connection for the venue price source: %s", exc)
        return None


def _dex_fallback() -> Any | None:
    try:
        from kaiba.execution.watchdog import ProviderPriceSource

        return ProviderPriceSource()
    except Exception as exc:  # noqa: BLE001 - no fallback is blindness after graduation
        log.warning("no DEX fallback for the venue price source: %s", exc)
        return None


__all__ = [
    "BSC_WRAPPED_NATIVE",
    "FLAP_ADDRESS_FINGERPRINT",
    "FLAP_ADDRESS_FINGERPRINTS",
    "FLAP_NOT_A_FLAP_TOKEN",
    "FOURMEME_HELPER",
    "FOURMEME_TOKEN_MANAGER_V2",
    "FourMemeCurve",
    "NonFlapVenue",
    "PANCAKE_V3_FACTORY",
    "RpcRevert",
    "SEL_FLAP_TOKEN_NOT_FOUND",
    "flap_token_not_found",
    "read_bsc_non_flap",
    "FLAP_CURVE_DECIMALS",
    "FLAP_FIXED_POINT",
    "FLAP_PORTAL",
    "FLAP_PROTOCOL_FEE_BPS",
    "FLAP_STATUS_DEX",
    "FLAP_STATUS_KILLED",
    "FLAP_STATUS_ON_CURVE",
    "FLAP_WAD",
    "SEL_MAX_BUY_PER_ORIGIN",
    "SEL_QUOTE_EXACT_INPUT",
    "FlapBuyQuota",
    "flap_buy_net_quote",
    "parse_buy_quota",
    "quote_exact_input_call",
    "NATIVE_DECIMALS",
    "NATIVE_USD_REFERENCE",
    "PANCAKE_V2_FACTORY",
    "PANCAKE_V2_ROUTER",
    "PRICE_CROSS_CHECK_TOLERANCE",
    "QUOTE_USD_MAX_AGE_S",
    "QUOTE_USD_TTL_S",
    "RESERVE_CROSS_CHECK_TOLERANCE",
    "ROUTER_LAST_RESORT_WAIT_FOR_SLOT_S",
    "ROUTER_MAX_PROBE_IMPACT_BPS",
    "ROUTER_PROBE_WEI",
    "ROUTER_REFERENCE_WEI",
    "SEL_GET_AMOUNTS_OUT",
    "SEL_GET_TOKEN_V8_SAFE",
    "VENUE_READERS",
    "ChainRoutedPriceSource",
    "EvmVenuePriceSource",
    "FlapCurve",
    "FlapRecord",
    "PancakeRouterPriceSource",
    "QuoteUsd",
    "RpcBatch",
    "VenuePrice",
    "VenueRead",
    "decode_amounts_out",
    "encode_amounts_out",
    "evm_price_source",
    "flap_record",
    "json_rpc_batch",
    "parse_flap_record",
    "pons_price",
    "read_flap",
    "read_pons",
    "robinhood_rpc_batch",
    "router_price",
    "venue_price_source",
]
