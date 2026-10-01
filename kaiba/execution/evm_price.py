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
the live VPS, with a bare ``httpx`` User-Agent, with a browser User-Agent,
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
NATIVE_USD_REFERENCE: dict[Chain, tuple[Chain, str]] = {
    Chain.BSC: (Chain.BSC, "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"),
    Chain.ROBINHOOD: (Chain.ETH, WETH_MAINNET),
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

    Words 4, 10-17 are read but not interpreted, and nothing here depends on them. They are
    left alone rather than named on a guess.
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


def flap_record(words: Sequence[int]) -> FlapRecord | None:
    """The raw words -> :class:`FlapRecord`, or ``None`` when the record is too short.

    A short record means the portal answered something this module does not understand —
    a proxy upgrade to a ``V9`` layout, say. Short is refused rather than padded.
    """
    if len(words) < _FLAP_MIN_WORDS:
        return None
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
    )


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
            ),
        ),
        "ok",
    )


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


def pons_price(
    state: Any,
    *,
    token_decimals: int,
    quote_decimals: int,
    quote_token: str | None,
    observed_ms: int | None = None,
) -> VenueRead:
    """A :class:`kaiba.ingest.robinhood.CurveState` -> a priced curve, or a refusal.

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
        ),
        "ok",
    )


def read_pons(token: str, rpc: RpcBatch) -> VenueRead:
    """Price one Robinhood token from its Pons curve. Two round trips cold, one warm.

    The cold trip asks the factory's ``getLaunchedToken`` which curve belongs to the token
    and what it is quoted in. Both are set at launch and never change, so they are cached
    and the warm path is one batched ``eth_call`` carrying all ten curve reads plus the
    token's ``decimals()``.
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
    return pons_price(
        state,
        token_decimals=decimals,
        quote_decimals=quote_decimals,
        quote_token=quote_token,
    )


# --------------------------------------------------------------------------------------
# quote token -> USD
# --------------------------------------------------------------------------------------

#: Decimals of a chain's native coin. Both chains this module reads are 18; the map exists
#: so that a chain whose native is not 18 cannot be added without noticing.
NATIVE_DECIMALS: dict[Chain, int] = {Chain.BSC: 18, Chain.ROBINHOOD: 18}


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
) -> RpcBatch:
    """An :data:`RpcBatch` that sends every ``eth_call`` as one JSON-RPC batch.

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
            "rpc",
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
            if not isinstance(item, Mapping) or item.get("error") is not None:
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
    bsc = _bsc_after_curve(conn, fallback)
    if bsc is not None:
        routes[Chain.BSC] = bsc
    return ChainRoutedPriceSource(routes, fallback=fallback)


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
    return EvmVenuePriceSource(
        conn,
        fallback=FallbackPriceSource(fallback, router, name="bsc-after-curve"),
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
    "FLAP_CURVE_DECIMALS",
    "FLAP_FIXED_POINT",
    "FLAP_PORTAL",
    "FLAP_STATUS_ON_CURVE",
    "FLAP_WAD",
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
