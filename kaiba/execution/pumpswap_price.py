"""Price a graduated pump.fun token from its own PumpSwap pool, in one RPC call.

WHY (2026-10-18/19). Paper fills, protection's marks and the shadow tape were ~95% DexScreener
quotes, which lag the pool by ~45 s (median) and keep serving the pump.fun CURVE pair for ~30 s
after migration. Paper entries were booked far below the real price and fast lanes showed
profits that the chain did not (docs/research/claude-profit-research-20261018/box-evidence.md,
Q1). The paper ENTRY was worse still: it used the token's dossier price, built at scan time.

WHAT. The canonical pool (``graduation_observer.pumpswap_pool_address``) and its two vaults,
read together in ONE ``getMultipleAccounts``:

    price = (quote_vault + virtual_quote_reserves) / base_vault

``virtual_quote_reserves`` is the signed 128-bit field at byte 245 of the 301-byte pool account.
pump.fun's BOOST (2026-07-21) opens a pool with about +17.58 virtual SOL on 67.41 real; the
virtual part falls as the boost spends, and was MEASURED negative (-9.78 SOL on a 138.9 SOL
pool). VALIDATED 2026-10-19 against six executed PumpSwap trades' own events: vault + virtual
was within 0.0-1.3% of the executed price (the fee); vault-only was off by +5% to +154%.

Anything this cannot price -- not sol, a token still on its curve (no pool account yet), a pool
that is not the token against WSOL, an RPC failure, no fresh SOL/USD -- comes back UNAVAILABLE,
and the existing chain (curve, DexScreener, GMGN) answers instead. "No pool yet" is cached for
:data:`NO_POOL_TTL_S` so a curve position does not spend a call every protection tick.

AND THE CURVE. :class:`PumpCurveSource` (bottom of this file) prices a token that has NOT
graduated from its pump.fun bonding-curve account, virtual SOL / virtual tokens, one
``getAccountInfo``. Checked on the box 2026-10-19 against the last executed trade: +0.0% and
-1.9%. Protection asks pool, then curve, then the old chain; the paper entry asks the same two.
"""

from __future__ import annotations

import base64
import logging
import struct
import threading
import time
from decimal import Decimal
from typing import Any

from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, EvidenceBasis

log = logging.getLogger(__name__)

WSOL_MINT = "So11111111111111111111111111111111111111112"
POOL_MIN_LEN = 261
VIRTUAL_QUOTE_OFFSET = 245
TOKEN_ACCOUNT_AMOUNT_OFFSET = 64
#: pump.fun mints are 6 decimals; SOL is 9.
BASE_DECIMALS = 6
QUOTE_DECIMALS = 9
NO_POOL_TTL_S = 30.0
#: SOL/USD older than this is not used to price a stop.
NATIVE_MAX_AGE_MS = 10 * 60_000
RPC_BUCKET = "rpc"
SOURCE = "pumpswap-pool"

#: Shared by every instance: protection builds one source per prefetch worker thread, and a
#: per-instance cache would repeat the pool lookup (and the "no pool" probe) once per worker.
_VAULTS: dict[str, tuple[str, str, str]] = {}
_NO_POOL: dict[str, float] = {}
#: token -> (reserves last read, when this process first read them, when they last CHANGED).
_MOVED: dict[str, tuple[tuple[int, ...], int, int]] = {}
_LOCK = threading.Lock()


def clear_caches() -> None:
    with _LOCK:
        _VAULTS.clear()
        _NO_POOL.clear()
        _MOVED.clear()


def _note_reserves(token: str, reserves: tuple[int, ...], at_ms: int) -> None:
    with _LOCK:
        prev = _MOVED.get(token)
        if prev is None:
            _MOVED[token] = (reserves, at_ms, at_ms)
        elif prev[0] != reserves:
            _MOVED[token] = (reserves, prev[1], at_ms)
        if len(_MOVED) > 20_000:
            _MOVED.clear()


def last_moved_ms(token: str) -> int | None:
    """When this process last saw the token's pool or curve reserves change -- i.e. last saw a
    trade -- or None if it has never read them.

    Before the first change it answers the FIRST read's time, so silence is only ever claimed
    for time actually watched. Protection's ``stale_no_volume`` rule read silence only from the
    swaps tape, which holds the wallets we track; a launch nobody we track trades had no tape,
    the rule saw "unknown" and by design never fired. MEASURED 2026-10-19: five launch-snipe
    paper positions held 0.4-7.5 h that way, filling the lane's shadow cap of 5 and refusing
    every new launch-snipe entry (``shadow_open_cap:5/5``).
    """
    with _LOCK:
        got = _MOVED.get(token)
    return got[2] if got else None


def reserve_clock(token: str) -> tuple[tuple[int, ...], int, int] | None:
    """(reserves last read, this process's first read, last change), or None if never read.
    The watchdog mirrors it to ``kv`` so a restart does not start the silence over."""
    with _LOCK:
        return _MOVED.get(token)


class RpcFailed(RuntimeError):
    """The read did not happen (endpoint down, limiter refusal, malformed answer).

    Kept apart from "the account says there is no pool/curve" on purpose: only the latter is
    cached. Caching a failed read as "no pool" sent that token to DexScreener for
    :data:`NO_POOL_TTL_S`, and the switch back is a source transition protection must corroborate.
    """


def _u64(data: bytes, offset: int) -> int | None:
    if len(data) < offset + 8:
        return None
    return struct.unpack_from("<Q", data, offset)[0]


def _i128(data: bytes, offset: int) -> int | None:
    if len(data) < offset + 16:
        return None
    return int.from_bytes(data[offset:offset + 16], "little", signed=True)


def _b64(value: Any) -> bytes | None:
    try:
        data = value["data"]
        return base64.b64decode(data[0]) if isinstance(data, list) else None
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def pool_reserves(pool: bytes, base_vault: bytes, quote_vault: bytes) -> tuple[int, int, int] | None:
    """(effective quote reserve in lamports, base atoms, virtual lamports) or None: the reserves
    the PumpSwap program prices a swap against, quote vault PLUS the pool's virtual quote reserve.

    None for a pool that is not the token against WSOL, unreadable accounts, or an effective quote
    reserve at or below zero. Pure; shared with ``learning.graduation_observer`` so the research
    marks and the protection marks cannot drift apart.
    """
    from kaiba.learning.graduation_observer import decode_pumpswap_pool

    meta = decode_pumpswap_pool(pool) if len(pool) >= POOL_MIN_LEN else None
    if meta is None or meta.get("quote_mint") != WSOL_MINT:
        return None
    base = _u64(base_vault, TOKEN_ACCOUNT_AMOUNT_OFFSET)
    quote = _u64(quote_vault, TOKEN_ACCOUNT_AMOUNT_OFFSET)
    virtual = _i128(pool, VIRTUAL_QUOTE_OFFSET)
    if not base or quote is None or virtual is None:
        return None
    effective = quote + virtual
    if effective <= 0:
        return None
    return effective, base, virtual


def pool_price(pool: bytes, base_vault: bytes, quote_vault: bytes) -> tuple[Decimal, Decimal, int] | None:
    """(SOL per whole token, effective quote reserve in SOL, virtual lamports) or None."""
    reserves = pool_reserves(pool, base_vault, quote_vault)
    if reserves is None:
        return None
    effective, base, virtual = reserves
    sol = Decimal(effective) / Decimal(10**QUOTE_DECIMALS)
    tokens = Decimal(base) / Decimal(10**BASE_DECIMALS)
    return sol / tokens, sol, virtual


class PumpSwapPoolSource:
    """A :class:`kaiba.execution.watchdog.PriceSource` for graduated pump.fun tokens on sol."""

    name = SOURCE

    def __init__(self, conn: Any = None, *, priority: Priority = Priority.EXIT, timeout_s: float = 4.0,
                 url: str | None = None) -> None:
        self.conn = conn
        self.priority = priority
        self.timeout_s = timeout_s
        self.url = url

    # ------------------------------------------------------------------ plumbing

    def _rpc(self, method: str, params: list[Any], endpoint: str) -> Any | None:
        from kaiba.providers._http import post_json

        url = self.url
        if not url:
            from kaiba.core.config import get_settings

            url = get_settings().rpc_for(Chain.SOL)
        if not url:
            return None
        got = post_json(RPC_BUCKET, f"rpc.{endpoint}", url,
                        json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                        priority=self.priority, ttl_s=0.0, timeout_s=self.timeout_s, retries=0, conn=self.conn)
        data = got.data if got.ok else None
        if not isinstance(data, dict) or "result" not in data:
            return None
        return data["result"]

    def _sol_usd(self) -> Decimal | None:
        from kaiba.core.schemas import now_ms
        from kaiba.providers import native_price

        try:
            sample = native_price.latest(Chain.SOL, self.conn)
        except Exception as exc:  # noqa: BLE001 - no native price is "cannot price", not a crash
            log.debug("pumpswap_price: native price unreadable: %s", exc)
            return None
        if sample is None or now_ms() - int(sample.ts_ms) > NATIVE_MAX_AGE_MS:
            return None
        value = Decimal(str(sample.price_usd))
        return value if value > 0 else None

    def _vaults_for(self, token: str) -> tuple[str, str, str] | None:
        """(pool, base_vault, quote_vault), learned once per token; None when there is no pool."""
        from kaiba.learning.graduation_observer import decode_pumpswap_pool, pumpswap_pool_address

        with _LOCK:
            if token in _VAULTS:
                return _VAULTS[token]
            if time.monotonic() - _NO_POOL.get(token, -1e9) < NO_POOL_TTL_S:
                return None
        pool = pumpswap_pool_address(token)
        if pool is None:
            return None
        got = self._rpc("getAccountInfo", [pool, {"encoding": "base64", "commitment": "confirmed"}],
                        "pumpswap_pool")
        if not isinstance(got, dict):
            raise RpcFailed("pool account read failed")
        data = _b64(got.get("value")) if got.get("value") else None
        meta = decode_pumpswap_pool(data) if data else None
        if meta is None or meta.get("base_mint") != token or meta.get("quote_mint") != WSOL_MINT:
            with _LOCK:
                _NO_POOL[token] = time.monotonic()
                if len(_NO_POOL) > 20_000:
                    _NO_POOL.clear()
            return None
        found = (pool, meta["base_vault"], meta["quote_vault"])
        with _LOCK:
            _VAULTS[token] = found
            if len(_VAULTS) > 20_000:
                _VAULTS.clear()
        return found

    # ------------------------------------------------------------------ the source

    def quote(self, chain: Chain, token: str):
        from kaiba.core.schemas import now_ms
        from kaiba.execution.watchdog import SETTLE_NATIVE_POOL, PriceQuote

        if chain is not Chain.SOL:
            return PriceQuote.unavailable("pumpswap pools are solana-only", source=self.name)
        try:
            # First, because it is a local read: a mark that cannot be put in USD must not spend
            # the RPC calls it would take to read the pool.
            sol_usd = self._sol_usd()
            if sol_usd is None:
                return PriceQuote.unavailable("no fresh SOL/USD for a pool mark", source=self.name)
            addrs = self._vaults_for(token)
            if addrs is None:
                return PriceQuote.unavailable("no pumpswap pool (still on its curve, or another venue)",
                                              source=self.name)
            pool, base_vault, quote_vault = addrs
            got = self._rpc("getMultipleAccounts",
                            [[pool, base_vault, quote_vault], {"encoding": "base64", "commitment": "confirmed"}],
                            "pumpswap_reserves")
            values = got.get("value") if isinstance(got, dict) else None
            if not isinstance(values, list) or len(values) != 3 or not all(values):
                return PriceQuote.unavailable("pumpswap reserve read failed", source=self.name)
            raw = [_b64(v) or b"" for v in values]
            priced = pool_price(*raw)
            if priced is None:
                return PriceQuote.unavailable("pumpswap pool unpriceable", source=self.name)
            per_token_sol, depth_sol, virtual = priced
            observed = now_ms()
            # The vault balances move only on a swap; the virtual reserve is left out of the key.
            _note_reserves(token, (_u64(raw[1], TOKEN_ACCOUNT_AMOUNT_OFFSET) or 0,
                                   _u64(raw[2], TOKEN_ACCOUNT_AMOUNT_OFFSET) or 0), observed)
            return PriceQuote(
                price_usd=per_token_sol * sol_usd,
                liquidity_usd=2 * depth_sol * sol_usd,
                basis=EvidenceBasis.VERIFIED_ONCHAIN,
                observed_ms=observed,
                source=f"{self.name}:{pool[:8]}",
                note=f"virtual {Decimal(virtual) / Decimal(10**QUOTE_DECIMALS):+.3f} SOL",
                chain=Chain.SOL,
                token=token,
                pool_id=pool,
                venue="pumpswap",
                liquidity_kind="pool_reserves",
                quote_token=WSOL_MINT,
                quote_symbol="SOL",
                settlement=SETTLE_NATIVE_POOL,
            )
        except Exception as exc:  # noqa: BLE001 - a source must never raise (PriceSource protocol)
            log.debug("pumpswap_price failed for %s: %s", token[:12], exc)
            return PriceQuote.unavailable(f"pumpswap_price error: {type(exc).__name__}", source=self.name)


# --------------------------------------------------------------------------------------
# the bonding curve, for tokens that have not graduated
# --------------------------------------------------------------------------------------


def curve_price(data: bytes | None) -> tuple[Decimal, Decimal] | None:
    """(SOL per whole token, virtual SOL depth) from a pump.fun bonding-curve account, or None
    for a completed (graduated) or unreadable curve. Price = virtual_sol / virtual_token, the
    program's own constant product -- read from the account, so a Mayhem curve whose virtual
    reserves jump between trades is still priced from what the account holds right now."""
    from kaiba.learning.leader_exit import decode_curve

    curve = decode_curve(data)
    if curve is None or curve["complete"] or curve["virtual_token"] <= 0 or curve["virtual_sol"] <= 0:
        return None
    sol = Decimal(curve["virtual_sol"]) / Decimal(10**QUOTE_DECIMALS)
    tokens = Decimal(curve["virtual_token"]) / Decimal(10**BASE_DECIMALS)
    return sol / tokens, sol


class PumpCurveSource(PumpSwapPoolSource):
    """A :class:`~kaiba.execution.watchdog.PriceSource` for pump.fun tokens still on their curve.

    2026-10-19: the routed curve source asks pump.fun's web coin route first (it has answered 404
    since 10-03, per ``snipe.py``) and then snapshots up to 60 s old, so launch-snipe marks fell
    through to DexScreener's curve pair -- 53% of those prints more than 10% off the chain
    (box-evidence.md Q1). This
    reads the curve ACCOUNT over RPC instead, one ``getAccountInfo``. A completed curve, or a
    token that never had one, refuses so the next layer answers.
    """

    name = "pump-curve"

    def _cached_absent(self, token: str) -> bool:
        with _LOCK:
            return time.monotonic() - _NO_POOL.get("curve:" + token, -1e9) < NO_POOL_TTL_S

    def _remember_absent(self, token: str) -> None:
        with _LOCK:
            _NO_POOL["curve:" + token] = time.monotonic()

    def _account(self, token: str) -> tuple[str | None, bytes | None]:
        """(curve address, account bytes). Bytes are None when the account does not exist;
        :class:`RpcFailed` when the read itself did not happen."""
        from kaiba.learning.leader_exit import bonding_curve_address

        address = bonding_curve_address(token)
        if not address:
            return None, None
        got = self._rpc("getAccountInfo", [address, {"encoding": "base64", "commitment": "confirmed"}],
                        "pump_curve")
        if not isinstance(got, dict):
            raise RpcFailed("curve account read failed")
        value = got.get("value")
        return address, (_b64(value) if value else None)

    def quote_with_state(self, chain: Chain, token: str) -> tuple[Any, Any]:
        """(PriceQuote, the broker's CurveState or None) from ONE account read.

        The paper entry needs both: the quote for its receipt and the reserves for the fill,
        because ``PaperBroker.buy`` prices a curve token off a ``CurveState`` and ignores the
        price it is handed ("supplying them does not override the curve").
        """
        from kaiba.core.schemas import now_ms
        from kaiba.execution.watchdog import SETTLE_CURVE, PriceQuote
        from kaiba.learning.leader_exit import decode_curve

        if chain is not Chain.SOL:
            return PriceQuote.unavailable("pump.fun curves are solana-only", source=self.name), None
        try:
            if self._cached_absent(token):
                return PriceQuote.unavailable("no live pump.fun curve (cached)", source=self.name), None
            sol_usd = self._sol_usd()
            if sol_usd is None:
                return PriceQuote.unavailable("no fresh SOL/USD for a curve mark", source=self.name), None
            address, data = self._account(token)
            priced = curve_price(data) if data else None
            if address is None or priced is None:
                self._remember_absent(token)
                return PriceQuote.unavailable("no live pump.fun curve (graduated, or another venue)",
                                              source=self.name), None
            per_token_sol, depth_sol = priced
            observed = now_ms()
            curve = decode_curve(data)
            _note_reserves(token, (curve["virtual_sol"], curve["virtual_token"]), observed)
            quote = PriceQuote(
                price_usd=per_token_sol * sol_usd,
                liquidity_usd=2 * depth_sol * sol_usd,
                basis=EvidenceBasis.VERIFIED_ONCHAIN,
                observed_ms=observed,
                source=f"{self.name}:{address[:8]}",
                chain=Chain.SOL,
                token=token,
                pool_id=address,
                venue="pumpfun-curve",
                liquidity_kind="curve_virtual_reserves",
                quote_token=WSOL_MINT,
                quote_symbol="SOL",
                settlement=SETTLE_CURVE,
            )
            return quote, curve_state(data, observed_ms=observed)
        except Exception as exc:  # noqa: BLE001 - a source must never raise (PriceSource protocol)
            log.debug("pump curve price failed for %s: %s", token[:12], exc)
            return PriceQuote.unavailable(f"pump curve error: {type(exc).__name__}", source=self.name), None

    def quote(self, chain: Chain, token: str):
        return self.quote_with_state(chain, token)[0]

    def read_state(self, token: str) -> Any:
        """The curve as the paper broker models it, read now. None when the token has graduated,
        has no curve, or the read failed -- the broker then resolves its own, as before. Needs no
        SOL/USD: the broker converts with its own native price. Never raises."""
        from kaiba.core.schemas import now_ms

        try:
            if self._cached_absent(token):
                return None
            _address, data = self._account(token)
            state = curve_state(data, observed_ms=now_ms()) if data else None
            if state is None and curve_price(data) is None:
                self._remember_absent(token)
            return state
        except Exception as exc:  # noqa: BLE001 - a missing curve is the broker's old path, not an error
            log.debug("pump curve state read failed for %s: %s", token[:12], exc)
            return None


def curve_state(data: bytes | None, *, observed_ms: int) -> Any:
    """:class:`~kaiba.execution.curve_price.CurveState` from a curve account, or None.

    ``CurveState.build`` validates (consistent SOL reserves, the reserved-token invariant), so a
    curve the paper model would not trust is refused here too and the broker falls back to
    resolving its own. Source ``pump-curve-account`` names where the reserves came from.
    """
    from kaiba.execution.curve_price import CurveState
    from kaiba.learning.leader_exit import decode_curve

    curve = decode_curve(data)
    if curve is None or curve["complete"]:
        return None
    state, _note = CurveState.build(
        virtual_sol=curve["virtual_sol"], virtual_token=curve["virtual_token"],
        real_sol=curve["real_sol"], real_token=curve["real_token"],
        observed_ms=int(observed_ms), source="pump-curve-account",
    )
    return state


def with_holding(state: Any, tokens: int) -> Any:
    """``state`` as it would be had our paper position's ``tokens`` been bought on chain too.

    A paper buy never reached the chain, so the curve read there does not hold its SOL. Selling
    paper tokens into that curve draws on SOL it never had: MEASURED 2026-10-19, two launches
    nobody else kept buying (real SOL 8-9 lamports) were sold at -100% for positions marked at
    breakeven, and three more exits were refused (``fee_exceeds_proceeds``,
    ``curve_drained_during_latency``). A pump.fun curve's state is a function of the tokens
    outstanding alone (constant product on virtual reserves), so the chain's curve shifted by
    our remaining tokens is exactly the curve a real position would sell into -- the same
    correction ``graduation_observer.PoolState.holding`` makes for a pool.

    None when the shifted curve is not one the paper model accepts (our tokens would exceed what
    the curve has left to sell); the broker then resolves its own curve, as before.
    """
    from kaiba.execution.curve_price import CurveState

    x = int(tokens or 0)
    if state is None or x <= 0:
        return state
    virtual_token = state.virtual_token - x
    real_token = state.real_token - x
    if virtual_token <= 0 or real_token < 0:
        return None
    k = state.virtual_sol * state.virtual_token
    virtual_sol = -(-k // virtual_token)  # ceil: rounding never hands a seller extra SOL
    shifted, _note = CurveState.build(
        virtual_sol=virtual_sol, virtual_token=virtual_token,
        real_sol=state.real_sol + (virtual_sol - state.virtual_sol), real_token=real_token,
        observed_ms=state.observed_ms, source=f"{state.source}+holding",
    )
    return shifted
