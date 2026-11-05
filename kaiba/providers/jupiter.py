"""Jupiter — an *executable* Solana quote, because a mid price flatters every paper fill.

Why this module exists at all. GMGN Free allows weight 5 per window and a quote costs 10,
so the limiter refuses a GMGN quote permanently rather than slowly (see the comment in
``kaiba/core/limiter.py`` next to the gmgn budget). Without a quote source the paper broker
measures fills against a mid price, and mid is exactly the number that hides the thing
paper trading gets wrong: what our size actually costs to trade. Jupiter answers
"if you put *this many* lamports in right now, what comes out, through which pools, at what
price impact" — free and keyless — which is a strictly better input to a simulated fill.

What was verified against the live API on 2026-09-20, not remembered:

* ``https://quote-api.jup.ag`` **no longer resolves in DNS**. Anything built against the v6
  host is dead, not slow.
* ``https://lite-api.jup.ag/swap/v1/quote`` answers 200 with **no key and no headers**.
  ``https://api.jup.ag/swap/v1/quote`` also answered keyless, but that is the metered host
  and the one that will start demanding ``x-api-key``; :func:`_base` only uses it when the
  operator has actually configured ``JUPITER_API_KEY``.
* **Jupiter routes pump.fun bonding curves directly.** A token still on its curve came back
  with a one-hop route labelled ``Pump.fun`` and a real price impact. The premise that a
  pre-graduation token "has no pair" is out of date for pump.fun specifically. The
  no-route path below is therefore *not* dead code for a different reason: brand-new mints,
  frozen mints and other launchpads' curves still come back ``TOKEN_NOT_TRADABLE``.
* **``priceImpactPct`` is not trustworthy at our size.** Measured live: a $5k-market-cap
  pump.fun curve token reported ``"0"`` impact for 0.02 SOL and ``2.77%`` for 1 SOL of the
  same token seconds later; JUP reported 4 bps at 1 SOL and ``"0"`` at 10 SOL. Jupiter
  appears to compute it against a reference route and returns an exact zero when the
  comparison degenerates, particularly on split routes. It is carried here because it is
  what the provider said, but :func:`round_trip` — quote in, quote the proceeds straight
  back out — is the number to believe, and on that same token it was 260 bps against a
  buy-side impact of 88. **Do not size against ``price_impact_bps`` alone.**
* Free-tier throughput: 60 sequential requests at 2.4 req/s drew zero 429s; a 20-way
  parallel burst started throwing 429 at roughly 11 req/s, and the block then persisted for
  the better part of a minute after the burst stopped with **no ``retry-after`` header** to
  tell us how long. The configured budget (``capacity 5``, ``refill 1.0/s``,
  ``min_interval_ms 1100``) sits about eight times under the observed ceiling, which is the
  right side to be on given that the punishment is sticky and silent.

The three outcomes this module distinguishes, because they mean different things:

``QuoteStatus.OK``
    Jupiter routed it. Every number is real and carries a receipt.
``QuoteStatus.NO_ROUTE``
    Jupiter answered, and its answer was "there is no path for this pair". A token on a
    bonding curve nothing has indexed yet, a frozen mint, a launchpad Jupiter does not
    integrate. **This is data.** It is the signal to go and ask curve-reserve pricing
    instead, and it must never be confused with an outage.
``QuoteStatus.UNAVAILABLE``
    We do not know: a 5xx, a 429, a timeout, or the limiter refusing us a slot. Retrying
    later is sensible; concluding anything about the token is not.
``QuoteStatus.BAD_REQUEST``
    We rejected the request ourselves before sending it — a malformed mint, a non-positive
    amount. That is a bug on our side and is never a statement about the token.

Known weakness, stated plainly: ``_http.request_json`` calls ``raise_for_status`` and keeps
only the exception string, so Jupiter's machine-readable 4xx body
(``{"error": "...", "errorCode": "TOKEN_NOT_TRADABLE"}``) never reaches us. NO_ROUTE is
therefore inferred from "we pre-validated every parameter, and Jupiter still answered 4xx",
not read from ``errorCode``. It also means a not-tradable token emits a ``PROVIDER_ERROR``
event, which is noise: nothing is wrong with the provider. Both are fixed by ``_http``
carrying the status code and the error body on ``Fetched``; that file is owned elsewhere so
it is a request, not a change.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from enum import StrEnum
from typing import Any

from kaiba.core.limiter import Priority
from kaiba.core.schemas import (
    SOL_NATIVE_MINT,
    Chain,
    EvidenceBasis,
    Receipt,
    looks_solana,
    now_ms,
)
from kaiba.providers._http import get_json, redact_text

log = logging.getLogger(__name__)

#: Limiter key. Matches the ``jupiter`` block in ``config/risk.yaml``.
PROVIDER = "jupiter"

#: Keyless free tier, verified live. The v6 host ``quote-api.jup.ag`` is gone from DNS.
LITE_HOST = "https://lite-api.jup.ag"
#: Metered host. Only used when ``JUPITER_API_KEY`` is configured.
PRO_HOST = "https://api.jup.ag"

QUOTE_PATH = "/swap/v1/quote"
PRICE_PATH = "/price/v3"

#: ``family.name`` for the limiter. The family (before the dot) is what a 429 cools down,
#: so the quote route and the price route are deliberately separate families: losing the
#: cheap metadata route must not also blind the route that decides a fill.
ENDPOINT_QUOTE = "quote.exact_in"
ENDPOINT_PRICE = "price.v3"

DEFAULT_SLIPPAGE_BPS = 100
MAX_SLIPPAGE_BPS = 10_000

#: Any caller that makes more than one request per logical operation must wait for limiter
#: capacity rather than silently losing every call after the first — see the warning on
#: ``_http.request_json``. A round trip is two calls and the impact ladder is three, so the
#: default here is a wait, not a refusal.
DEFAULT_WAIT_FOR_SLOT_S = 10.0

#: A quote is never cached: it is the price at a size at a slot, and a stale one is a lie
#: with a timestamp. The metadata route is cached, because decimals do not move.
DEFAULT_PRICE_TTL_S = 30.0
DECIMALS_TTL_S = 86_400.0

#: 0.02 SOL — the configured maximum position size. Probing at the size we would actually
#: trade is the entire point; probing at 1 lamport would reproduce the mid price we are
#: trying to get away from.
PROBE_LAMPORTS = 20_000_000

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

DECIMALS_TOKENS_ROW = "tokens_row"
DECIMALS_JUPITER = "jupiter"
DECIMALS_UNAVAILABLE = "unavailable"

_LAMPORTS = 9


# --------------------------------------------------------------------------------------
# parsing helpers
# --------------------------------------------------------------------------------------


def _int(value: Any) -> int | None:
    """Base units are integers. A value we cannot read is ``None``, never 0."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(Decimal(str(value)))
    except (DecimalException, ValueError, TypeError):
        return None


def _dec(value: Any) -> Decimal | None:
    """``_http`` parses JSON with ``parse_float=Decimal``; this survives the string case too."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (DecimalException, ValueError, TypeError):
        return None


_HTTP_STATUS_RE = re.compile(r"(?:Client|Server) error '(\d{3})")


def _status_from_note(note: str | None) -> int | None:
    """Recover the HTTP status from the exception string ``_http`` kept.

    Deliberately strict. A loose ``\\b[45]\\d\\d\\b`` also matches digits inside the request
    URL that ``httpx`` renders into the message, and mistaking ``amount=450...`` for a 450
    response would classify a live token as permanently unroutable.
    """
    if not note:
        return None
    match = _HTTP_STATUS_RE.search(note)
    if match:
        return int(match.group(1))
    if "rate limited" in note.lower():
        return 429
    return None


def _base() -> tuple[str, dict[str, str]]:
    """Host and headers. Keyless unless the operator configured a key.

    The key is never logged here; ``_http`` redacts what it writes and the value only ever
    travels in a header, not in the query string, so an exception string cannot carry it.
    """
    try:
        from kaiba.core.config import get_settings

        key = (get_settings().jupiter_api_key or "").strip()
    except Exception as exc:  # noqa: BLE001 - an unreadable settings file is not a reason to fail
        log.debug("could not read jupiter credentials: %s", redact_text(str(exc)))
        key = ""
    if key:
        return PRO_HOST, {"x-api-key": key}
    return LITE_HOST, {}


def _unavailable_receipt(endpoint: str, note: str) -> Receipt:
    return Receipt(
        provider=PROVIDER,
        endpoint=endpoint,
        basis=EvidenceBasis.UNAVAILABLE,
        note=redact_text(note)[:300],
    )


# --------------------------------------------------------------------------------------
# quote
# --------------------------------------------------------------------------------------


class QuoteStatus(StrEnum):
    """Why we do or do not have a quote. See the module docstring for what each implies."""

    OK = "ok"
    NO_ROUTE = "no_route"
    UNAVAILABLE = "unavailable"
    BAD_REQUEST = "bad_request"


@dataclass(frozen=True)
class RouteHop:
    """One leg of the route. ``label`` is the AMM name — ``Pump.fun`` for a bonding curve."""

    amm_key: str | None
    label: str | None
    input_mint: str | None
    output_mint: str | None
    amount_in: int | None
    amount_out: int | None
    percent: int | None = None


@dataclass(frozen=True)
class JupiterQuote:
    """One executable quote, or the explained absence of one.

    ``amount_out is None`` means we have no number. It never means zero: a zero output is
    indistinguishable from a 100% loss to anything that divides by it.
    """

    status: QuoteStatus
    chain: Chain
    input_mint: str
    output_mint: str
    amount_in: int
    slippage_bps: int
    receipt: Receipt
    amount_out: int | None = None
    min_out: int | None = None
    price_impact_pct: Decimal | None = None
    swap_usd_value: Decimal | None = None
    swap_mode: str = "ExactIn"
    route: tuple[RouteHop, ...] = ()
    context_slot: int | None = None
    http_status: int | None = None
    note: str | None = None

    # ---------------------------------------------------------------- classification

    @property
    def ok(self) -> bool:
        """True only when a real, positive output amount came back."""
        return self.status is QuoteStatus.OK and self.amount_out is not None and self.amount_out > 0

    @property
    def no_route(self) -> bool:
        """Jupiter answered and said this pair has no path. A fact about the token."""
        return self.status is QuoteStatus.NO_ROUTE

    @property
    def failed(self) -> bool:
        """We do not know. A fact about us, the network, or Jupiter — not about the token."""
        return self.status in (QuoteStatus.UNAVAILABLE, QuoteStatus.BAD_REQUEST)

    def __bool__(self) -> bool:
        return self.ok

    # ---------------------------------------------------------------- derived numbers

    @property
    def route_labels(self) -> tuple[str, ...]:
        return tuple(h.label for h in self.route if h.label)

    @property
    def route_label(self) -> str:
        return "+".join(self.route_labels) or "none"

    @property
    def on_bonding_curve(self) -> bool:
        """The route goes through a launchpad curve rather than a pool.

        Useful to the caller even though Jupiter *can* price it: a curve has a graduation
        event, and an impact number taken from one is not comparable to one from a pool.
        """
        return any(lbl.lower().startswith(("pump.fun", "bonk", "moonshot")) for lbl in self.route_labels)

    @property
    def price_impact_bps(self) -> int | None:
        """Impact in basis points, rounded. ``priceImpactPct`` is a *fraction*, not a percent."""
        if self.price_impact_pct is None:
            return None
        return int((self.price_impact_pct * Decimal(10_000)).to_integral_value())

    @property
    def rate_atoms(self) -> Decimal | None:
        """Output atoms per input atom. Exact, and needs no decimals to be correct."""
        if self.amount_out is None or self.amount_in <= 0:
            return None
        return Decimal(self.amount_out) / Decimal(self.amount_in)

    def unit_price_in_per_out(self, in_decimals: int, out_decimals: int) -> Decimal | None:
        """Whole input tokens per whole output token — e.g. SOL per memecoin.

        Both decimals are required arguments and there is no default, because guessing
        them is a factor of 10^n error in a price, and the paper broker already has one
        module that guesses 6/18 and says so.
        """
        if self.amount_out is None or self.amount_out <= 0 or self.amount_in <= 0:
            return None
        if in_decimals < 0 or out_decimals < 0:
            return None
        whole_in = Decimal(self.amount_in) / (Decimal(10) ** in_decimals)
        whole_out = Decimal(self.amount_out) / (Decimal(10) ** out_decimals)
        if whole_out == 0:
            return None
        return whole_in / whole_out

    def usd_per_output_token(self, out_decimals: int) -> Decimal | None:
        """USD per whole output token, from Jupiter's own ``swapUsdValue`` for this route."""
        if self.swap_usd_value is None or self.amount_out is None or self.amount_out <= 0:
            return None
        if out_decimals < 0:
            return None
        whole_out = Decimal(self.amount_out) / (Decimal(10) ** out_decimals)
        if whole_out == 0:
            return None
        return self.swap_usd_value / whole_out

    def usd_per_input_token(self, in_decimals: int) -> Decimal | None:
        """USD per whole input token. This is how the SOL price falls out of any quote."""
        if self.swap_usd_value is None or self.amount_in <= 0 or in_decimals < 0:
            return None
        whole_in = Decimal(self.amount_in) / (Decimal(10) ** in_decimals)
        if whole_in == 0:
            return None
        return self.swap_usd_value / whole_in


def _refused(
    status: QuoteStatus,
    input_mint: str,
    output_mint: str,
    amount_in: int,
    slippage_bps: int,
    note: str,
    *,
    http_status: int | None = None,
    chain: Chain = Chain.SOL,
) -> JupiterQuote:
    return JupiterQuote(
        status=status,
        chain=chain,
        input_mint=input_mint,
        output_mint=output_mint,
        amount_in=amount_in,
        slippage_bps=slippage_bps,
        receipt=_unavailable_receipt(ENDPOINT_QUOTE, f"{status.value}: {note}"),
        http_status=http_status,
        note=redact_text(note)[:300],
    )


def _parse_route(payload: Any) -> tuple[RouteHop, ...]:
    if not isinstance(payload, list):
        return ()
    hops: list[RouteHop] = []
    for entry in payload:
        info = entry.get("swapInfo") if isinstance(entry, dict) else None
        if not isinstance(info, dict):
            continue
        hops.append(
            RouteHop(
                amm_key=str(info.get("ammKey")) if info.get("ammKey") is not None else None,
                label=str(info.get("label")) if info.get("label") is not None else None,
                input_mint=str(info.get("inputMint")) if info.get("inputMint") is not None else None,
                output_mint=str(info.get("outputMint")) if info.get("outputMint") is not None else None,
                amount_in=_int(info.get("inAmount")),
                amount_out=_int(info.get("outAmount")),
                percent=_int(entry.get("percent")),
            )
        )
    return tuple(hops)


def quote(
    input_mint: str,
    output_mint: str,
    amount_in: int,
    *,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
    chain: Chain = Chain.SOL,
    only_direct_routes: bool = False,
    priority: Priority = Priority.RESEARCH,
    wait_for_slot_s: float = DEFAULT_WAIT_FOR_SLOT_S,
    timeout_s: float = 10.0,
    conn: Any = None,
) -> JupiterQuote:
    """An exact-in quote for ``amount_in`` base units of ``input_mint``. Never raises.

    Every parameter is validated before a request goes out, which is what lets a 4xx from
    Jupiter be read as "no route" rather than "one of us is wrong".
    """
    if chain is not Chain.SOL:
        return _refused(
            QuoteStatus.BAD_REQUEST, input_mint, output_mint, amount_in, slippage_bps,
            f"jupiter routes solana only, got {chain.value}", chain=chain,
        )
    for name, mint in (("inputMint", input_mint), ("outputMint", output_mint)):
        if not isinstance(mint, str) or not looks_solana(mint):
            return _refused(
                QuoteStatus.BAD_REQUEST, str(input_mint), str(output_mint), _int(amount_in) or 0,
                slippage_bps, f"{name} is not a solana mint",
            )
    if input_mint == output_mint:
        return _refused(
            QuoteStatus.BAD_REQUEST, input_mint, output_mint, _int(amount_in) or 0, slippage_bps,
            "input and output mint are the same",
        )
    if not isinstance(amount_in, int) or isinstance(amount_in, bool) or amount_in <= 0:
        return _refused(
            QuoteStatus.BAD_REQUEST, input_mint, output_mint, _int(amount_in) or 0, slippage_bps,
            "amount_in must be a positive integer of base units",
        )
    if not isinstance(slippage_bps, int) or not 0 <= slippage_bps <= MAX_SLIPPAGE_BPS:
        return _refused(
            QuoteStatus.BAD_REQUEST, input_mint, output_mint, amount_in, DEFAULT_SLIPPAGE_BPS,
            f"slippage_bps must be 0..{MAX_SLIPPAGE_BPS}",
        )

    host, headers = _base()
    params: dict[str, Any] = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_in),
        "slippageBps": str(slippage_bps),
    }
    if only_direct_routes:
        params["onlyDirectRoutes"] = "true"

    got = get_json(
        PROVIDER,
        ENDPOINT_QUOTE,
        f"{host}{QUOTE_PATH}",
        params=params,
        headers=headers or None,
        priority=priority,
        ttl_s=0.0,  # a quote is a price at a slot; serving a cached one would be a lie
        wait_for_slot_s=wait_for_slot_s,
        timeout_s=timeout_s,
        conn=conn,
    )

    if not got.ok:
        note = got.receipt.note or "no response"
        code = _status_from_note(note)
        # 429 and 5xx are "we do not know". A 4xx after our own validation is Jupiter
        # telling us the pair has no path — it is the TOKEN_NOT_TRADABLE body we cannot see.
        if code is not None and 400 <= code < 500 and code not in (408, 429):
            status = QuoteStatus.NO_ROUTE
        else:
            status = QuoteStatus.UNAVAILABLE
        return _refused(
            status, input_mint, output_mint, amount_in, slippage_bps, note,
            http_status=code, chain=chain,
        )

    data = got.data if isinstance(got.data, dict) else {}
    amount_out = _int(data.get("outAmount"))
    route = _parse_route(data.get("routePlan"))

    if amount_out is None:
        return _refused(
            QuoteStatus.UNAVAILABLE, input_mint, output_mint, amount_in, slippage_bps,
            "200 but no outAmount in the response", http_status=200, chain=chain,
        )
    if amount_out <= 0 or not route:
        return _refused(
            QuoteStatus.NO_ROUTE, input_mint, output_mint, amount_in, slippage_bps,
            "200 with an empty route", http_status=200, chain=chain,
        )

    return JupiterQuote(
        status=QuoteStatus.OK,
        chain=chain,
        input_mint=input_mint,
        output_mint=output_mint,
        amount_in=amount_in,
        slippage_bps=_int(data.get("slippageBps")) or slippage_bps,
        receipt=got.receipt,
        amount_out=amount_out,
        min_out=_int(data.get("otherAmountThreshold")),
        price_impact_pct=_dec(data.get("priceImpactPct")),
        swap_usd_value=_dec(data.get("swapUsdValue")),
        swap_mode=str(data.get("swapMode") or "ExactIn"),
        route=route,
        context_slot=_int(data.get("contextSlot")),
        http_status=200,
    )


def quote_buy(
    token: str,
    lamports: int = PROBE_LAMPORTS,
    **kw: Any,
) -> JupiterQuote:
    """What ``lamports`` of SOL buys of ``token`` right now, impact included."""
    return quote(SOL_NATIVE_MINT, token, lamports, **kw)


def quote_sell(token: str, token_atoms: int, **kw: Any) -> JupiterQuote:
    """What ``token_atoms`` of ``token`` fetches in lamports right now.

    The direction that matters for an exit, and the one a honeypot fails.
    """
    return quote(token, SOL_NATIVE_MINT, token_atoms, **kw)


# --------------------------------------------------------------------------------------
# token metadata: decimals, mid price, pool depth
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class JupiterTokenInfo:
    """``/price/v3`` for one mint. ``usd_price`` here is a **mid price, not executable**.

    It is carried because the depth and the decimals come with it for free, not because it
    is a substitute for a quote. Anything sizing a fill should use :func:`quote`.
    """

    mint: str
    decimals: int | None
    usd_price: Decimal | None
    liquidity_usd: Decimal | None
    receipt: Receipt
    known: bool = False


def token_info(
    mint: str,
    *,
    ttl_s: float = DEFAULT_PRICE_TTL_S,
    priority: Priority = Priority.RESEARCH,
    wait_for_slot_s: float = DEFAULT_WAIT_FOR_SLOT_S,
    conn: Any = None,
) -> JupiterTokenInfo:
    """Decimals, pool depth and Jupiter's mid price for one mint. Never raises.

    An unknown mint comes back as ``known=False`` with every field ``None`` — Jupiter
    answers 200 with an empty object for a token it cannot price, which is a clean
    "no" rather than an error, and is treated as one.
    """
    if not isinstance(mint, str) or not looks_solana(mint):
        return JupiterTokenInfo(
            mint=str(mint), decimals=None, usd_price=None, liquidity_usd=None,
            receipt=_unavailable_receipt(ENDPOINT_PRICE, "not a solana mint"),
        )
    host, headers = _base()
    got = get_json(
        PROVIDER,
        ENDPOINT_PRICE,
        f"{host}{PRICE_PATH}",
        params={"ids": mint},
        headers=headers or None,
        priority=priority,
        ttl_s=ttl_s,
        wait_for_slot_s=wait_for_slot_s,
        conn=conn,
    )
    if not got.ok:
        return JupiterTokenInfo(
            mint=mint, decimals=None, usd_price=None, liquidity_usd=None,
            receipt=got.receipt,
        )
    entry = got.data.get(mint) if isinstance(got.data, dict) else None
    if not isinstance(entry, dict):
        return JupiterTokenInfo(
            mint=mint, decimals=None, usd_price=None, liquidity_usd=None,
            receipt=_unavailable_receipt(ENDPOINT_PRICE, "jupiter does not price this mint"),
        )
    return JupiterTokenInfo(
        mint=mint,
        decimals=_int(entry.get("decimals")),
        usd_price=_dec(entry.get("usdPrice")),
        liquidity_usd=_dec(entry.get("liquidity")),
        receipt=got.receipt,
        known=True,
    )


def mint_decimals(
    mint: str,
    *,
    conn: Any = None,
    priority: Priority = Priority.RESEARCH,
    wait_for_slot_s: float = DEFAULT_WAIT_FOR_SLOT_S,
) -> tuple[int | None, str]:
    """``(decimals, basis)``. ``(None, "unavailable")`` when we do not know.

    Decimals are a fact, and a wrong one is a factor of 10^n on a price, so this never
    falls back to "probably 6 because it is Solana". The local ``tokens`` row is preferred
    because it is free and already ingested; Jupiter's own metadata is the fallback, cached
    for a day since decimals do not move.
    """
    if not isinstance(mint, str) or not looks_solana(mint):
        return None, DECIMALS_UNAVAILABLE
    try:
        from kaiba.core.db import fetch_one, get_conn

        c = conn or get_conn()
        row = fetch_one(
            c, "SELECT decimals FROM tokens WHERE chain=? AND address=?", (Chain.SOL.value, mint)
        )
        if row and row["decimals"] is not None:
            return int(row["decimals"]), DECIMALS_TOKENS_ROW
    except Exception as exc:  # noqa: BLE001 - an unreadable table is a miss, not a crash
        log.debug("tokens row lookup failed for %s: %s", mint, redact_text(str(exc)))

    info = token_info(
        mint, ttl_s=DECIMALS_TTL_S, priority=priority, wait_for_slot_s=wait_for_slot_s, conn=conn
    )
    if info.decimals is not None and info.decimals >= 0:
        return info.decimals, DECIMALS_JUPITER
    return None, DECIMALS_UNAVAILABLE


# --------------------------------------------------------------------------------------
# executable price
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutablePrice:
    """A USD price that a real route would have paid at a real size.

    ``price_usd is None`` means unknown — including the case where the quote succeeded but
    we could not establish the token's decimals, because a price derived from guessed
    decimals is worse than no price.
    """

    chain: Chain
    token: str
    price_usd: Decimal | None
    liquidity_usd: Decimal | None
    price_impact_pct: Decimal | None
    decimals: int | None
    decimals_basis: str
    probe_amount_in: int
    quote: JupiterQuote
    receipt: Receipt

    @property
    def known(self) -> bool:
        return self.price_usd is not None and self.price_usd > 0

    @property
    def no_route(self) -> bool:
        return self.quote.no_route

    @property
    def price_impact_bps(self) -> int | None:
        return self.quote.price_impact_bps


def executable_price(
    token: str,
    *,
    lamports: int = PROBE_LAMPORTS,
    with_liquidity: bool = True,
    chain: Chain = Chain.SOL,
    priority: Priority = Priority.POSITION,
    wait_for_slot_s: float = DEFAULT_WAIT_FOR_SLOT_S,
    conn: Any = None,
) -> ExecutablePrice:
    """USD price of ``token`` implied by buying ``lamports`` worth of it. Never raises.

    The price comes from Jupiter's own ``swapUsdValue`` for the route it would actually
    take, divided by the tokens that route would actually deliver — so it already contains
    the impact of our size, which is the whole reason for preferring it to a mid price.

    Asking for the native mint prices SOL itself, via the USDC leg, because the watchdog
    asks this source for the chain's own token when it computes ``min_out``.
    """
    if chain is not Chain.SOL:
        empty = _refused(
            QuoteStatus.BAD_REQUEST, SOL_NATIVE_MINT, str(token), lamports, DEFAULT_SLIPPAGE_BPS,
            f"jupiter routes solana only, got {chain.value}", chain=chain,
        )
        return ExecutablePrice(
            chain=chain, token=str(token), price_usd=None, liquidity_usd=None,
            price_impact_pct=None, decimals=None, decimals_basis=DECIMALS_UNAVAILABLE,
            probe_amount_in=lamports, quote=empty, receipt=empty.receipt,
        )

    native = isinstance(token, str) and token == SOL_NATIVE_MINT
    out_mint = USDC_MINT if native else token
    got = quote(
        SOL_NATIVE_MINT,
        out_mint,
        lamports,
        chain=chain,
        priority=priority,
        wait_for_slot_s=wait_for_slot_s,
        conn=conn,
    )

    if not got.ok:
        return ExecutablePrice(
            chain=chain, token=str(token), price_usd=None, liquidity_usd=None,
            price_impact_pct=got.price_impact_pct, decimals=None,
            decimals_basis=DECIMALS_UNAVAILABLE, probe_amount_in=lamports,
            quote=got, receipt=got.receipt,
        )

    if native:
        # SOL's own price falls straight out of the input leg; no decimals lookup needed.
        price = got.usd_per_input_token(_LAMPORTS)
        decimals, basis = _LAMPORTS, DECIMALS_JUPITER
    else:
        decimals, basis = mint_decimals(
            token, conn=conn, priority=priority, wait_for_slot_s=wait_for_slot_s
        )
        price = got.usd_per_output_token(decimals) if decimals is not None else None

    liquidity: Decimal | None = None
    if with_liquidity:
        info = token_info(
            token if not native else SOL_NATIVE_MINT,
            priority=priority,
            wait_for_slot_s=wait_for_slot_s,
            conn=conn,
        )
        liquidity = info.liquidity_usd

    receipt = got.receipt
    if price is None:
        receipt = _unavailable_receipt(
            ENDPOINT_QUOTE,
            f"routed via {got.route_label} but no usd price: decimals={basis}, "
            f"swap_usd_value={'present' if got.swap_usd_value is not None else 'absent'}",
        )

    return ExecutablePrice(
        chain=chain,
        token=str(token),
        price_usd=price,
        liquidity_usd=liquidity,
        price_impact_pct=got.price_impact_pct,
        decimals=decimals,
        decimals_basis=basis,
        probe_amount_in=lamports,
        quote=got,
        receipt=receipt,
    )


# --------------------------------------------------------------------------------------
# round trip and the impact ladder — the sizing inputs nothing else in the tree has
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RoundTrip:
    """Buy then immediately sell back. ``cost_bps`` is what a paper fill should be charged.

    A single quote's ``priceImpactPct`` is half the story: it does not include the spread,
    the second side's impact, or a launchpad's fee on each leg. Buying 0.02 SOL of a
    bonding-curve token and selling the proceeds straight back loses materially more than
    the buy-side impact alone, and that difference is exactly what a mid-price paper fill
    pretends does not exist.
    """

    token: str
    lamports_in: int
    buy: JupiterQuote
    sell: JupiterQuote | None
    lamports_out: int | None
    cost_bps: int | None

    @property
    def known(self) -> bool:
        return self.cost_bps is not None


def round_trip(
    token: str,
    lamports: int = PROBE_LAMPORTS,
    *,
    priority: Priority = Priority.RESEARCH,
    wait_for_slot_s: float = DEFAULT_WAIT_FOR_SLOT_S,
    conn: Any = None,
) -> RoundTrip:
    """Two quotes: SOL in, then the whole proceeds straight back out. Never raises."""
    buy = quote_buy(
        token, lamports, priority=priority, wait_for_slot_s=wait_for_slot_s, conn=conn
    )
    if not buy.ok or buy.amount_out is None:
        return RoundTrip(str(token), lamports, buy, None, None, None)
    sell = quote_sell(
        token, buy.amount_out, priority=priority, wait_for_slot_s=wait_for_slot_s, conn=conn
    )
    if not sell.ok or sell.amount_out is None:
        return RoundTrip(str(token), lamports, buy, sell, None, None)
    back = sell.amount_out
    cost = (Decimal(lamports - back) * Decimal(10_000)) / Decimal(lamports)
    return RoundTrip(str(token), lamports, buy, sell, back, int(cost.to_integral_value()))


def impact_ladder(
    token: str,
    sizes_lamports: Sequence[int] = (20_000_000, 1_000_000_000, 10_000_000_000),
    *,
    priority: Priority = Priority.RESEARCH,
    wait_for_slot_s: float = DEFAULT_WAIT_FOR_SLOT_S,
    conn: Any = None,
) -> list[JupiterQuote]:
    """Price impact at several sizes, so position sizing stops being a guess.

    ``wait_for_slot_s`` is non-zero by default and must stay that way: this makes one call
    per size, and the documented failure of the non-waiting default is that everything
    after the first call silently disappears — here that would look like a flat impact
    curve, which reads as "our size is free" and is the single most expensive wrong answer
    this module could give.
    """
    out: list[JupiterQuote] = []
    for size in sizes_lamports:
        out.append(
            quote_buy(
                token, int(size), priority=priority, wait_for_slot_s=wait_for_slot_s, conn=conn
            )
        )
    return out


# --------------------------------------------------------------------------------------
# price sources
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _DetachedPriceQuote:
    """Stand-in used only when ``kaiba.execution.watchdog`` cannot be imported.

    The watchdog rejects anything that is not its own ``PriceQuote``, so returning this
    makes it go blind — loudly, on its existing blind path — instead of this module raising
    into a loop that is holding open positions. It is the safe direction to fail in.
    """

    price_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    executable_quote_usd: Decimal | None = None
    basis: EvidenceBasis = EvidenceBasis.UNAVAILABLE
    observed_ms: int = 0
    source: str = PROVIDER
    note: str | None = None

    @property
    def usable(self) -> bool:
        return False


def _price_quote_cls() -> Any:
    """The watchdog's ``PriceQuote``, imported lazily.

    Lazily because ``kaiba.execution.watchdog`` imports the provider layer: a module-level
    import here would be a cycle, and because the watchdog is being written in parallel a
    hard dependency would make this provider unimportable whenever that file is mid-edit.
    """
    try:
        from kaiba.execution.watchdog import PriceQuote

        return PriceQuote
    except Exception as exc:  # noqa: BLE001 - absence is blindness, not a crash
        log.warning("watchdog PriceQuote unavailable: %s", redact_text(str(exc)))
        return None


def _as_price_quote(
    *,
    price_usd: Decimal | None,
    liquidity_usd: Decimal | None,
    executable_quote_usd: Decimal | None,
    basis: EvidenceBasis,
    source: str,
    note: str | None,
    observed_ms: int | None = None,
) -> Any:
    cls = _price_quote_cls()
    kwargs = {
        "price_usd": price_usd,
        "liquidity_usd": liquidity_usd,
        "executable_quote_usd": executable_quote_usd,
        "basis": basis,
        "source": source,
        "note": (note or "")[:300] or None,
        "observed_ms": observed_ms or now_ms(),
    }
    if cls is None:
        return _DetachedPriceQuote(**kwargs)  # type: ignore[arg-type]
    return cls(**kwargs)


class JupiterPriceSource:
    """Satisfies ``kaiba.execution.watchdog.PriceSource``: one ``quote()`` method.

    What it gives the watchdog that DexScreener cannot: ``price_usd`` is the price a real
    route would have delivered at ``probe_lamports``, and ``executable_quote_usd`` is the
    USD notional that route was actually worth. A stop evaluated against that is a stop
    evaluated against a fill.

    The failure modes stay distinguishable in the events table, because the watchdog puts
    ``quote.source`` into every ``protection_blind`` payload: ``jupiter:no_route`` means go
    ask curve pricing, ``jupiter:unavailable`` means Jupiter is down or we are throttled.

    Register it by adding to ``PRICE_SOURCES`` in ``watchdog.py``::

        "jupiter": JupiterPriceSource,

    and setting ``protection.price_source: jupiter`` in ``config/risk.yaml``.
    """

    name = PROVIDER

    def __init__(
        self,
        *,
        probe_lamports: int = PROBE_LAMPORTS,
        with_liquidity: bool = True,
        priority: Priority = Priority.EXIT,
        wait_for_slot_s: float = DEFAULT_WAIT_FOR_SLOT_S,
        conn: Any = None,
    ) -> None:
        self.probe_lamports = int(probe_lamports)
        self.with_liquidity = bool(with_liquidity)
        self.priority = priority
        self.wait_for_slot_s = float(wait_for_slot_s)
        self.conn = conn

    def quote(self, chain: Chain, token: str) -> Any:
        try:
            found = executable_price(
                token,
                lamports=self.probe_lamports,
                with_liquidity=self.with_liquidity,
                chain=chain,
                priority=self.priority,
                wait_for_slot_s=self.wait_for_slot_s,
                conn=self.conn,
            )
        except Exception as exc:  # noqa: BLE001 - "must never raise" is a promise, not a hope
            return _as_price_quote(
                price_usd=None, liquidity_usd=None, executable_quote_usd=None,
                basis=EvidenceBasis.UNAVAILABLE, source=f"{PROVIDER}:unavailable",
                note=f"jupiter raised {type(exc).__name__}",
            )

        if not found.known:
            tag = "no_route" if found.no_route else "unavailable"
            return _as_price_quote(
                price_usd=None, liquidity_usd=None, executable_quote_usd=None,
                basis=EvidenceBasis.UNAVAILABLE, source=f"{PROVIDER}:{tag}",
                note=found.receipt.note or found.quote.note or tag,
            )

        basis = found.quote.receipt.basis
        return _as_price_quote(
            price_usd=found.price_usd,
            liquidity_usd=found.liquidity_usd,
            executable_quote_usd=found.quote.swap_usd_value,
            basis=basis if isinstance(basis, EvidenceBasis) else EvidenceBasis.PROVIDER_REPORTED,
            source=f"{PROVIDER}:{found.quote.route_label}",
            note=(
                f"executable at {found.probe_amount_in} lamports, "
                f"impact {found.price_impact_bps} bps, decimals {found.decimals_basis}"
            ),
            observed_ms=found.quote.receipt.observed_at_ms,
        )


class JupiterPricesSource:
    """Satisfies ``kaiba.providers.prices.PriceSource``, for the DYOR / batch price stack.

    ``prices.py`` says in its own docstring that a second source (Jupiter) should slot in
    behind ``register_source``; this is that, and it needs no edit to ``prices.py``::

        from kaiba.providers import prices
        from kaiba.providers.jupiter import JupiterPricesSource
        prices.register_source(JupiterPricesSource())

    One quote per token — Jupiter has no batch quote route — so it is the fallback for
    tokens DexScreener could not price, not the bulk path.
    """

    name = PROVIDER

    def __init__(self, *, probe_lamports: int = PROBE_LAMPORTS, with_liquidity: bool = True) -> None:
        self.probe_lamports = int(probe_lamports)
        self.with_liquidity = bool(with_liquidity)

    def quotes(
        self,
        chain: Chain,
        tokens: Sequence[str],
        *,
        max_age_s: float = DEFAULT_PRICE_TTL_S,
        priority: Priority = Priority.POSITION,
        conn: Any = None,
    ) -> dict[str, Any]:
        from kaiba.providers.prices import Quote

        out: dict[str, Any] = {}
        for token in tokens:
            found = executable_price(
                token,
                lamports=self.probe_lamports,
                with_liquidity=self.with_liquidity,
                chain=chain,
                priority=priority,
                # Every token after the first is a separate call; without a wait the
                # limiter refuses them unread and the batch silently shrinks to one.
                wait_for_slot_s=DEFAULT_WAIT_FOR_SLOT_S,
                conn=conn,
            )
            out[token] = Quote(
                chain=chain,
                token=token,
                price_usd=found.price_usd,
                liquidity_usd=found.liquidity_usd,
                receipt=found.receipt,
                pair_label=found.quote.route_label if found.quote.ok else None,
                dex_id=(found.quote.route_labels[0] if found.quote.route_labels else None),
                pairs_considered=len(found.quote.route),
                source=self.name,
            )
        return out


__all__ = [
    "DECIMALS_JUPITER",
    "DECIMALS_TOKENS_ROW",
    "DECIMALS_UNAVAILABLE",
    "DEFAULT_SLIPPAGE_BPS",
    "ENDPOINT_PRICE",
    "ENDPOINT_QUOTE",
    "LITE_HOST",
    "PROBE_LAMPORTS",
    "PROVIDER",
    "PRO_HOST",
    "ExecutablePrice",
    "JupiterPriceSource",
    "JupiterPricesSource",
    "JupiterQuote",
    "JupiterTokenInfo",
    "QuoteStatus",
    "RouteHop",
    "RoundTrip",
    "executable_price",
    "impact_ladder",
    "mint_decimals",
    "quote",
    "quote_buy",
    "quote_sell",
    "round_trip",
    "token_info",
]
