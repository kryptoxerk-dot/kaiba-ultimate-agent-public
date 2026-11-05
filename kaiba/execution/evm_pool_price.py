"""Pool quotes for Robinhood and BSC paper, shaped for protection. Step 3 of
``docs/research/claude-paper-every-chain-20261021/DESIGN.md``.

WHY. MEASURED on the box 2026-10-21: 89 of 89 post-cut robinhood paper entries and 45 of 45 bsc
entries were priced off the token's DOSSIER (built at scan time, mostly DexScreener), a median
+5.0% / +4.8% above the tape; 54 of 62 robinhood exits and 19 of 19 bsc exits were priced off
DexScreener. Two pre-cut paper trades booked +32,977% and +1,226% that way. This module prices the
same tokens from their own pool, at our size, and says when it cannot.

What it provides, and to whom
-----------------------------

* :func:`entry_fill` -- the paper ENTRY (``engine.paper_entry_quote``): an executable quote for
  the decision's size. v2 by the reserves formula, v3 by QuoterV2, v4 by V4Quoter with the full
  PoolKey (hooks included: the quoter simulates the swap). slot0 is a MARK, never a fill.
* :func:`exit_fill` -- the paper EXIT fill (``watchdog.DefaultExitSubmitter._paper``): the same,
  selling the position's quantity, at ``Priority.EXIT``. The deciding mark only TRIGGERS the exit.
* :class:`PoolPriceSource` -- the bsc protection leaf, after the Flap curve and BEFORE
  DexScreener (``evm_price.venue_price_source``). Its quotes carry ``pool_id``, ``venue``,
  ``liquidity_usd`` and ``liquidity_kind``, because ``Watchdog._validate_quote`` needs a pool
  identity and credible depth before it lets a new source decide a stop. It has a ``prime``
  hook (``Watchdog._prefetch_quotes`` calls it once per chain per tick) so the whole bsc book
  is read in ONE Multicall3 ``aggregate3`` per tick, whatever its size.
* :class:`OnchainSampleSource` -- the robinhood protection leaf. ZERO new RPC: it answers from
  the newest ``onchain_price_samples`` row ``onchain_pool.OnchainPriceLog`` already writes for
  the position (one ``aggregate3`` per tick on ``robinhood-rpc``, protection's own pacer), and
  refuses a sample older than two poll intervals.
* :class:`BackupPriceSource` -- an independent leaf behind the pool for corroboration ONLY
  (``_validate_quote`` refuses to let a second read of the SAME pool corroborate a stop). Never
  asked for a tick's price and never remembered as a quote identity; wired to the sampled
  Codex.io quote (:func:`codex_backup_quote`) by ``evm_price``.

Budgets, and why each is shaped this way
----------------------------------------

* **bsc** reads post under their OWN limiter key, :data:`BSC_PROVIDER` (``bsc-pool-rpc``), never
  ``rpc`` -- which every sol PumpSwap/curve stop and every ``evm_price.json_rpc_batch`` call
  spend -- and never ``dexscreener``. A separate key isolates OUR cooldown families, not the
  provider's per-IP limit, so the host is separate too: :func:`bsc_pool_rpc_url` reads
  ``BSC_POOL_RPC_URL`` and REFUSES when it is unset or names any host protection's own readers
  dial (:func:`forbidden_pool_hosts`, computed at runtime: bsc, sol and robinhood). It never falls
  back to ``rpc_for``. The Flap-curve paper ENTRY rides the same key and host when it is set
  (:func:`bsc_curve_entry_transport`).
* **robinhood** gets NO new key: that chain has one host, and ``evm_price.robinhood_rpc_batch``
  exists so ONE pacer governs it. Entry and exit fills (one call each, at the entry or exit
  rate) go through it; protection marks add no call at all (the sampler above).
* **Quote assets.** A pool is admitted only when its other side is the chain's native coin or
  its wrapped native (WBNB; WETH / native ETH on robinhood, ethereum and base). USDG, USDT,
  USDC, ZORA, VIRTUAL, flETH, tokenised stocks and every other quote asset are refused
  ``quote_asset_not_native`` until the crossing back to native is measured and charged: an exit
  sells through a second pool, and a paper fill that ignored that crossing would be optimistic
  by exactly its cost.
* **ethereum and base** (design step 6, 2026-10-23) are POOL-ONLY (:data:`POOL_ONLY_CHAINS`):
  no launch curve, no dossier fallback at the entry, no DexScreener anywhere. A token is priced
  from a Uniswap v2 pair (the 30 bps formula), a v3 pool (QuoterV2) or a v4 pool WITHOUT a
  hook (V4Quoter) against WETH / native ETH, found from the factories and the PoolManager
  (:func:`resolve_uniswap`), or it is refused with the reason named. Hooked v4 launch pools
  (Clanker, Bankr, Zora, Flaunch, Virtuals ...) and Aerodrome are refused until a V4Quoter /
  Aerodrome fill is verified against a real swap (:func:`recorded_venue_refusal`). Each chain
  reads on its OWN limiter key and host (``eth-pool-rpc`` / ``ETH_POOL_RPC_URL``,
  ``base-pool-rpc`` / ``BASE_POOL_RPC_URL``), refused when unset and when the host -- or its
  operator -- is one protection, the scanner, the snipe or the other chain already dials
  (:func:`pool_rpc_url`).

Money is integers in base units until the final ratio, which is ``Decimal``; nothing here goes
through ``float``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, EvidenceBasis, looks_evm, now_ms
from kaiba.execution import onchain_pool as op

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# chain constants
# --------------------------------------------------------------------------------------

#: Chains priced from the token's own pool, at our size, and only from it: ethereum and base
#: (design step 6, 2026-10-23). There is no launch curve reader there and no fallback -- the
#: engine refuses a paper entry the pool cannot price instead of booking the dossier, and
#: protection never routes these chains to DexScreener (``evm_price.venue_price_source``).
POOL_ONLY_CHAINS: frozenset[Chain] = frozenset({Chain.ETH, Chain.BASE})
#: Chains this module prices.
POOL_CHAINS: frozenset[Chain] = frozenset({Chain.BSC, Chain.ROBINHOOD}) | POOL_ONLY_CHAINS

MULTICALL3 = op.MULTICALL3  # canonical; VERIFIED on bsc 2026-10-21 (3,808 bytes of code)
ZERO = "0x" + "0" * 40

WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
RH_WETH = op.ROBINHOOD_WETH
WETH_ETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
WETH_BASE = "0x4200000000000000000000000000000000000006"

#: The wrapped native per chain. ``None`` (v4 currency 0x0) is the native coin itself.
WRAPPED_NATIVE: dict[Chain, str] = {Chain.BSC: WBNB, Chain.ROBINHOOD: RH_WETH, Chain.ETH: WETH_ETH,
                                    Chain.BASE: WETH_BASE}


@dataclass(frozen=True)
class UniswapDeployment:
    """Uniswap's own contracts on one chain: the only venues :func:`resolve_uniswap` reads."""

    v2_factory: str
    v3_factory: str
    v3_quoter: str  # QuoterV2
    pool_manager: str  # v4
    v4_quoter: str


#: VERIFIED from the box 2026-10-23, read-only (eth.drpc.org / base.drpc.org): ``eth_getCode`` sizes
#: v2 factory 13,859, v3 factory 24,535, QuoterV2 8,273, PoolManager 24,009, V4Quoter 5,820 bytes
#: on BOTH chains; ``QuoterV2.factory()`` returned the v3 factory and ``V4Quoter.poolManager()`` the
#: PoolManager; ``getPair(WETH, USDC)`` / ``getPool(WETH, USDC, 500)`` returned the canonical
#: pools (eth 0xb4e16d01... / 0x88e6a0c2..., base 0x88a43bbd... / 0xd0b53d92...). 0.04 ETH quoted
#: 95.600 USDC through QuoterV2 and 95.598 through V4Quoter (native ETH/USDC, 500/10, no hook) on
#: eth, 95.579 / 95.583 on base: the two quoters agree within 0.01% and with ``native_prices``
#: (ETH $2,389.67 the same minute).
UNISWAP: dict[Chain, UniswapDeployment] = {
    Chain.ETH: UniswapDeployment(
        v2_factory="0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f",
        v3_factory="0x1f98431c8ad98523631ae4a59f267346ea31f984",
        v3_quoter="0x61ffe014ba17989e743c5f6cb21bf9697530b21e",
        pool_manager="0x000000000004444c5dc75cb358380d2e3de08a90",
        v4_quoter="0x52f0e24d1c21c8a0cb1e5a5dd6198556bd9e1203",
    ),
    Chain.BASE: UniswapDeployment(
        v2_factory="0x8909dc15e40173ff4699343b6eb8132c65e18ec6",
        v3_factory="0x33128a8fc17869897dce68ed026d694621f6fdfd",
        v3_quoter="0x3d4e44eb1374240ce5f1b871ab261cd16335b76a",
        pool_manager="0x498581ff718922c3f8e6a244956af099b2652b2b",
        v4_quoter="0x0d5e0f971ed27fbff6c2837bf31316121532048d",
    ),
}
#: A Uniswap v2 pair keeps 0.30% of the input (``getAmountOut``'s 997/1000), set in the pair code.
UNISWAP_V2_FEE_BPS = 30
UNISWAP_V3_FEE_TIERS: tuple[int, ...] = (100, 500, 3000, 10000)
#: v4 pools WITHOUT a hook, at Uniswap's standard (fee, tickSpacing) pairs, against native ETH and
#: against WETH. A hookless pool's id is a pure function of its key, so it can be FOUND by reading
#: these eight state slots in one batch -- no log scan, no hint. A hooked pool cannot be found this
#: way and is not wanted: its hook's fee is not in slot0 and V4Quoter is unverified against a real
#: swap through it (design 6d).
UNISWAP_V4_NO_HOOK_TIERS: tuple[tuple[int, int], ...] = ((100, 1), (500, 10), (3000, 60), (10000, 200))

#: PancakeSwap V2 factory + its pair fee (25 bps: 0.17% LP + 0.03% treasury + 0.05% CAKE).
#: The fee is the factory's, so a pair from any other factory is refused (``v2_factory_unknown``)
#: rather than charged a guess. Address shared with ``evm_price.PANCAKE_V2_FACTORY``.
PANCAKE_V2_FACTORY = "0xca143ce32fe78f1f7019d7d551a6402fc5350c73"
PANCAKE_V2_FEE_BPS = 25
#: PancakeSwap V3 factory and QuoterV2. QuoterV2 VERIFIED deployed 2026-10-21 (eth_getCode,
#: 8,331 bytes, bsc-dataseed.bnbchain.org). Fee tiers as ``evm_price.PANCAKE_V3_FEE_TIERS``.
PANCAKE_V3_FACTORY = "0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865"
PANCAKE_V3_QUOTER = "0xb048bbc1ee6b733fffcfb9e9cef7375518e25997"
PANCAKE_V3_FEE_TIERS: tuple[int, ...] = (100, 500, 2500, 10000)

#: Robinhood Chain, from Uniswap's deployments feed (developers.uniswap.org/deployments.json,
#: generated 2026-09-22) and VERIFIED deployed from the box 2026-10-21 with one eth_getCode
#: batch: V4Quoter 6,118 bytes, QuoterV2 8,273 bytes. PoolManager / PositionManager / v3 factory
#: are ``onchain_pool``'s, which the same feed confirms.
RH_V4_QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
RH_V3_QUOTER = "0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7"
RH_POOL_MANAGER = op.POOL_MANAGER
RH_POSITION_MANAGER = op.POSITION_MANAGER
RH_V3_FACTORY = op.V3_FACTORY
RH_V3_FEE_TIERS = op.V3_FEE_TIERS

#: Selectors, each pinned against ``policy.selector_of`` in ``tests/test_evm_pool_price.py``.
SEL_GET_RESERVES = op.SEL_GET_RESERVES  # getReserves()
SEL_SLOT0 = op.SEL_SLOT0  # slot0()
SEL_LIQUIDITY = op.SEL_LIQUIDITY  # liquidity()
SEL_TOKEN0 = op.SEL_TOKEN0  # token0()
SEL_TOKEN1 = op.SEL_TOKEN1  # token1()
SEL_DECIMALS = op.SEL_DECIMALS  # decimals()
SEL_EXTSLOAD = op.SEL_EXTSLOAD  # extsload(bytes32)
SEL_POOL_KEYS = op.SEL_POOL_KEYS  # poolKeys(bytes25)
SEL_GET_POOL = op.SEL_GET_POOL  # getPool(address,address,uint24)
SEL_FACTORY = "0xc45a0155"  # factory()
SEL_FEE = "0xddca3f43"  # fee()
SEL_GET_PAIR = "0xe6a43905"  # getPair(address,address)
SEL_POOL_AND_POSITION = "0x7ba03aad"  # getPoolAndPositionInfo(uint256)
#: QuoterV2 (Uniswap and PancakeSwap share the ABI):
#: quoteExactInputSingle((address tokenIn,address tokenOut,uint256 amountIn,uint24 fee,uint160 limit))
SEL_QUOTE_V3 = "0xc6a5026a"
#: V4Quoter: quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))
SEL_QUOTE_V4 = "0xaa9d21cb"

#: v4 ``Pool.State``: slot0 at the state slot, liquidity three words later (feeGrowthGlobal0,
#: feeGrowthGlobal1, liquidity). ``StateLibrary.LIQUIDITY_OFFSET`` is 3.
V4_LIQUIDITY_OFFSET = 3

_Q96 = Decimal(2) ** 96
_U128_MAX = (1 << 128) - 1

# --------------------------------------------------------------------------------------
# budget
# --------------------------------------------------------------------------------------

#: The bsc pool reader's limiter key. Its budget is ``risk.yaml provider_budgets.bsc-pool-rpc``
#: when declared (``limiter.limits_for`` reads any name there); until then the limiter's own
#: default (1 s spacing, 2 in flight). Never ``rpc``: see the module docstring.
BSC_PROVIDER = "bsc-pool-rpc"
BSC_ENDPOINT = "rpc.pool"

#: The setting that names the bsc pool host. Read from the environment, then the two ``.env``
#: files, exactly as ``launch_feed.bsc_snipe_rpc_url`` reads its own -- no ``Settings`` field
#: exists for it (``kaiba/core/config.py`` is lead-owned; the field to add is in the docstring of
#: :func:`bsc_pool_rpc_url`).
BSC_POOL_RPC_ENV = "BSC_POOL_RPC_URL"

#: A FLOOR of the hosts the pool reader must not share: protection's bsc host on the box as of
#: 2026-10-21. The real set is computed at runtime by :func:`forbidden_pool_hosts` -- every host
#: protection's own readers dial -- because a hardcoded name stops protecting the day the box's
#: ``BSC_RPC_URL`` changes (2026-10-23, step 3 follow-up).
FORBIDDEN_BSC_HOSTS: frozenset[str] = frozenset({"bsc-rpc.publicnode.com"})
_FORBIDDEN_CACHE: dict[str, tuple[float, frozenset[str]]] = {}

#: Per-chain limiter key, endpoint and host setting of the pool reader. eth/base budgets are
#: ``risk.yaml provider_budgets.eth-pool-rpc`` / ``base-pool-rpc`` (``limiter.limits_for`` reads any
#: name there); host settings are ``Settings.eth_pool_rpc_url`` / ``base_pool_rpc_url`` (env
#: ``ETH_POOL_RPC_URL`` / ``BASE_POOL_RPC_URL``). Never ``rpc`` and never ``rpc_for``: see
#: :func:`pool_rpc_url`.
POOL_PROVIDER: dict[Chain, str] = {Chain.BSC: BSC_PROVIDER, Chain.ETH: "eth-pool-rpc", Chain.BASE: "base-pool-rpc"}
POOL_RPC_ENV: dict[Chain, str] = {Chain.BSC: BSC_POOL_RPC_ENV, Chain.ETH: "ETH_POOL_RPC_URL",
                                  Chain.BASE: "BASE_POOL_RPC_URL"}
POOL_ENDPOINT = BSC_ENDPOINT
#: The chains whose pool reads go to a host of their own (robinhood rides its one pacer instead).
POOL_HOST_CHAINS: frozenset[Chain] = frozenset({Chain.BSC}) | POOL_ONLY_CHAINS


def protection_rpc_urls() -> list[str]:
    """The URLs protection's own stop readers dial, as this process resolves them.

    * bsc: ``rpc_for(BSC)`` (``BSC_RPC_URL``; ``evm_price.json_rpc_batch`` -- the Flap curve, the
      Pancake router read) AND its public fallback ``dyor_verify.PUBLIC_RPC[BSC]``, listed whether
      or not ``BSC_RPC_URL`` is set, since an unset one is exactly when the fallback is dialed;
    * sol: ``rpc_for(SOL)`` (``SOLANA_RPC_URL``; the PumpSwap pool and curve stops) and its public
      fallback;
    * robinhood: ``rpc_for(ROBINHOOD)`` (``ROBINHOOD_RPC_URL``) and ``kaiba.ingest.robinhood.RPC_URL``,
      the one pacer every robinhood stop read goes through.

    A per-IP 429 our pool reads earned on any of these would land on a stop."""
    urls: list[str] = []
    try:
        from kaiba.core.config import get_settings

        settings = get_settings()
        urls += [settings.rpc_for(chain) or "" for chain in (Chain.BSC, Chain.SOL, Chain.ROBINHOOD)]
    except Exception as exc:  # noqa: BLE001 - what cannot be read is listed from the other sources
        log.debug("protection rpc urls: settings unreadable: %s", type(exc).__name__)
    try:
        from kaiba.intelligence.dyor_verify import PUBLIC_RPC

        urls += [PUBLIC_RPC.get(Chain.BSC) or "", PUBLIC_RPC.get(Chain.SOL) or ""]
    except Exception as exc:  # noqa: BLE001
        log.debug("protection rpc urls: no public table: %s", type(exc).__name__)
    try:
        from kaiba.ingest.robinhood import RPC_URL

        urls.append(str(RPC_URL or ""))
    except Exception as exc:  # noqa: BLE001
        log.debug("protection rpc urls: no robinhood pacer url: %s", type(exc).__name__)
    return [u for u in urls if u and u.strip()]


def forbidden_pool_hosts() -> frozenset[str]:
    """Hosts the bsc pool host may not be: :data:`FORBIDDEN_BSC_HOSTS` plus the host of every URL
    :func:`protection_rpc_urls` returns. Cached like the pool URL (:data:`_URL_TTL_S`)."""
    now = time.monotonic()
    hit = _FORBIDDEN_CACHE.get("hosts")
    if hit is not None and now - hit[0] < _URL_TTL_S:
        return hit[1]
    hosts = frozenset(FORBIDDEN_BSC_HOSTS | {_host(u) for u in protection_rpc_urls()} - {""})
    _FORBIDDEN_CACHE["hosts"] = (now, hosts)
    return hosts

RPC_TIMEOUT_S = 4.0
#: Bounded well under the 12 s tick: a pool read that cannot get a slot refuses this tick.
WAIT_FOR_SLOT_S = 2.0
ENTRY_WAIT_FOR_SLOT_S = 5.0

#: The per-tick prime (:meth:`PoolPriceSource.prime`) runs on protection's tick thread inside
#: ``Watchdog._prefetch_quotes``, before ANY position on any chain is decided -- in the shadow
#: phase that is after the live quotes but before the live decisions. So it never waits for a
#: limiter slot and gives the host about a second. 2026-10-23 (step 3 review, blocking #4):
#: at the general 2 s slot wait + 4 s timeout a slow host took the first LIVE sol decision from
#: 47 ms to 3,031 ms with only two shadow bsc positions on the book.
PRIME_WAIT_FOR_SLOT_S = 0.0
PRIME_TIMEOUT_S = 1.0

#: Per-host circuit breaker for bsc pool reads (:func:`breaker_open`). A send that fails at the
#: host (timeout, connection, HTTP error, a provider 429, an undecodable answer) opens it for
#: :data:`BREAKER_OPEN_S`, doubling on each consecutive failure up to :data:`BREAKER_MAX_OPEN_S`;
#: while open, every read refuses at once with NO network, so a dead or slow host costs one
#: bounded send per window instead of one per position per tick (the prime fails, then each
#: quote re-reads its pool alone, and nothing backed off -- blocking #4 (b)/(c)). Our own
#: limiter's transient refusals (minimum interval, max inflight, bucket) are contention, not a
#: host failure, and do not count. One success closes it. INVENTED sizes: 30 s is under three
#: 12 s ticks; 600 s caps a dead host at one probe per ten minutes.
BREAKER_OPEN_S = 30.0
BREAKER_MAX_OPEN_S = 600.0
BREAKER_REFUSAL = "bsc_pool_rpc_circuit_open"

#: How long a primed read answers ``quote`` in this process. The prime runs at the start of a
#: tick's prefetch and the quotes follow within the same tick, so this only has to cover one
#: prefetch; it must NOT carry a price into the next 12 s tick.
PRIMED_MAX_AGE_MS = 5_000

#: A pool resolution (which pool, which side) is a structural fact; cached in ``kv`` so the
#: protection process does not re-discover what the engine found at entry.
POOL_KV_PREFIX = "evm_pool:"
POOL_KV_MAX_AGE_MS = 86_400_000

#: ``onchain_price_samples`` older than this many poll intervals do not price a position.
SAMPLE_MAX_POLLS = 2
DEFAULT_POLL_INTERVAL_S = 12.0

Transport = Callable[[Sequence[tuple[str, str]]], list[str | None]]


# --------------------------------------------------------------------------------------
# small ABI helpers
# --------------------------------------------------------------------------------------


def _word(value: int) -> str:
    return format(int(value) & ((1 << 256) - 1), "064x")


def _addr(address: str | None) -> str:
    return (address or ZERO)[2:].lower().rjust(64, "0")


def _words(raw: Any) -> list[int]:
    if not isinstance(raw, str) or not raw.startswith("0x"):
        return []
    body = raw[2:]
    out: list[int] = []
    for i in range(0, len(body) - 63, 64):
        try:
            out.append(int(body[i : i + 64], 16))
        except ValueError:
            return out
    return out


def _first(raw: Any) -> int | None:
    words = _words(raw)
    return words[0] if words else None


def _address_word(raw: Any) -> str | None:
    value = _first(raw)
    if value is None:
        return None
    text = "0x" + format(value & ((1 << 160) - 1), "040x")
    return None if text == ZERO else text


def _bytes(raw: Any) -> bytes:
    if not isinstance(raw, str) or not raw.startswith("0x"):
        return b""
    try:
        return bytes.fromhex(raw[2:])
    except ValueError:
        return b""


def quote_v3_call(quoter: str, token_in: str, token_out: str, amount_in: int, fee: int) -> tuple[str, str]:
    """QuoterV2 ``quoteExactInputSingle`` with no price limit. The struct is static: inline."""
    data = SEL_QUOTE_V3 + _addr(token_in) + _addr(token_out) + _word(amount_in) + _word(fee) + _word(0)
    return quoter, data


def quote_v4_call(
    quoter: str, key: tuple[str, str, int, int, str], zero_for_one: bool, amount_in: int,
) -> tuple[str, str]:
    """V4Quoter ``quoteExactInputSingle((PoolKey, bool, uint128, bytes))``, empty hook data.

    The params tuple holds ``bytes`` so it is dynamic: one offset word (0x20), then the PoolKey
    inline (5 words), the bool, the amount, the bytes offset relative to the tuple (8 words =
    0x100), and the bytes length 0.
    """
    c0, c1, fee, spacing, hooks = key
    data = (SEL_QUOTE_V4 + _word(0x20) + _addr(c0) + _addr(c1) + _word(fee) + _word(spacing)
            + _addr(hooks) + _word(1 if zero_for_one else 0) + _word(min(int(amount_in), _U128_MAX))
            + _word(0x100) + _word(0))
    return quoter, data


def v4_liquidity_slot(pool_id: str) -> str:
    base = int(op.v4_state_slot(pool_id), 16)
    return "0x" + format(base + V4_LIQUIDITY_OFFSET, "064x")


def v4_slot0_fields(raw: Any) -> tuple[int, int, int, int] | None:
    """``extsload`` of the v4 state slot -> ``(sqrtPriceX96, tick, protocolFee, lpFee)``."""
    value = _first(raw)
    if value is None or len(_words(raw)) != 1:
        return None
    sqrt = value & ((1 << 160) - 1)
    if not sqrt:
        return None
    tick = (value >> 160) & 0xFFFFFF
    if tick >= 1 << 23:
        tick -= 1 << 24
    return sqrt, tick, (value >> 184) & 0xFFFFFF, (value >> 208) & 0xFFFFFF


def v2_amount_out(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int) -> int | None:
    """``getAmountOut`` of a constant-product pair. ``None``, never 0, for a pool that cannot fill."""
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return None
    keep = 10_000 - int(fee_bps)
    with_fee = int(amount_in) * keep
    out = with_fee * int(reserve_out) // (int(reserve_in) * 10_000 + with_fee)
    return out if out > 0 else None


def in_range_quote_reserve(sqrt_price_x96: int, liquidity: int, quote_is0: bool) -> Decimal | None:
    """The quote side's VIRTUAL reserve of the active range, in quote base units.

    ``L * sqrtP`` for token1, ``L / sqrtP`` for token0 (sqrtP = sqrtPriceX96 / 2**96). It is the
    depth a constant product of the same liquidity would show; for a full-range position it is
    the pool's reserve. Reported as ``liquidity_kind = "v3_in_range_virtual_usd"`` so nothing
    mistakes it for TVL.
    """
    if sqrt_price_x96 <= 0 or liquidity <= 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 60
        sqrt = Decimal(sqrt_price_x96) / _Q96
        return Decimal(liquidity) / sqrt if quote_is0 else Decimal(liquidity) * sqrt


# --------------------------------------------------------------------------------------
# what a pool is
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolRef:
    """One pool of ``token`` against the chain's native coin. Immutable once resolved."""

    chain: Chain
    token: str  # lower-case
    pool: str  # 20-byte pool/pair address, or the 32-byte v4 pool id
    kind: str  # v2 | v3 | v4
    venue: str  # pancake-v2 | pancake-v3 | uniswap-v3 | uniswap-v4
    token_is0: bool
    quote: str | None  # None = the native coin (v4 currency 0x0), else the wrapped native
    fee: int  # v2: bps; v3/v4: hundredths of a bip (the pool's own tier / key fee)
    tick_spacing: int | None = None
    hooks: str | None = None
    found_by: str = ""

    @property
    def currency0(self) -> str:
        quote = self.quote or ZERO
        return self.token if self.token_is0 else quote

    @property
    def currency1(self) -> str:
        quote = self.quote or ZERO
        return quote if self.token_is0 else self.token

    @property
    def v4_key(self) -> tuple[str, str, int, int, str]:
        return (self.currency0, self.currency1, int(self.fee), int(self.tick_spacing or 0), self.hooks or ZERO)

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        out["chain"] = self.chain.value
        return out

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> PoolRef | None:
        try:
            return cls(
                chain=Chain(str(raw["chain"])), token=str(raw["token"]).lower(), pool=str(raw["pool"]).lower(),
                kind=str(raw["kind"]), venue=str(raw["venue"]), token_is0=bool(raw["token_is0"]),
                quote=(str(raw["quote"]).lower() if raw.get("quote") else None), fee=int(raw["fee"]),
                tick_spacing=None if raw.get("tick_spacing") is None else int(raw["tick_spacing"]),
                hooks=(str(raw["hooks"]).lower() if raw.get("hooks") else None),
                found_by=str(raw.get("found_by") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None


def native_quote_refusal(chain: Chain, quote: str | None) -> str | None:
    """``None`` when ``quote`` is the chain's native coin or wrapped native; the refusal otherwise."""
    if quote is None or quote.lower() == ZERO:
        return None
    if quote.lower() == WRAPPED_NATIVE.get(chain, "").lower():
        return None
    return f"quote_asset_not_native:{quote.lower()}"


@dataclass(frozen=True)
class PoolMark:
    """One read of a pool's state: a MARK (spot), and the quote side's depth. Not a fill."""

    ref: PoolRef
    price_native: Decimal  # whole native per whole token
    quote_reserve_base: int | None  # native base units on the quote side (v2 real, v3/v4 in-range virtual)
    reserves: tuple[int, int] | None  # v2 only, (reserve0, reserve1)
    token_decimals: int
    observed_ms: int
    #: v4 only: the pool's protocol fee in hundredths of a bip, the higher of its two directions
    #: (slot0 packs zeroForOne in the low 12 bits, oneForZero in the next 12). Charged ON TOP of the
    #: LP fee in v4 (unlike v3, where it is a share of it). MEASURED 2026-10-23: 500 / 500 on the
    #: eth PEPE/ETH 3000/60 pool, 25 / 25 on eth ETH/USDC 100/1 (fixture pool_reads_20261023.json).
    protocol_fee_pips: int | None = None
    #: v3 / v4: ``(sqrtPriceX96, liquidity)`` as read. v2 carries ``reserves``. The raw state a swap
    #: changes, for the pool clock (:func:`pool_clock`); ``None`` where a mark was built without it.
    state: tuple[int, int] | None = None


@dataclass(frozen=True)
class Fill:
    """An executable quote at our size. ``executable_price_usd`` is USD per WHOLE token."""

    ref: PoolRef
    side: str  # buy | sell
    amount_in: int
    amount_out: int
    spot_price_usd: Decimal | None
    executable_price_usd: Decimal
    liquidity_usd: Decimal | None
    native_usd: Decimal
    basis: str  # v2_formula | quoter_v2 | v4_quoter
    observed_ms: int
    #: A BUY filled after Kaiba's landing drift (:func:`landing_drift`, fd_ 2026-10-23): its provenance, plus
    #: ``atoms_at_decision`` -- what the same size got on the decision's state, before the drift. ``None`` on a
    #: sell and on a fill built without it.
    landing: dict[str, Any] | None = None

    @property
    def source(self) -> str:
        return f"pool:{self.ref.venue}:{self.basis}"


# --------------------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------------------


_URL_CACHE: dict[str, tuple[float, str | None]] = {}
_URL_TTL_S = 60.0


def _env_or_dotenv(name: str) -> str | None:
    value = os.environ.get(name) or os.environ.get(name.lower())
    if value and value.strip():
        return value.strip()
    try:
        from dotenv import dotenv_values

        from kaiba.core.config import REPO_ROOT

        user_dir = Path(os.environ.get("KAIBA_CONFIG_DIR", Path.home() / ".config" / "kaiba"))
        found: str | None = None
        for path in (REPO_ROOT / ".env", user_dir / ".env"):  # later wins, as in Settings
            if path.is_file():
                values = {str(k).upper(): v for k, v in dotenv_values(path).items()}
                found = values.get(name) or found
        return found.strip() if found and found.strip() else None
    except Exception as exc:  # noqa: BLE001 - an unreadable setting is "not configured"
        log.debug("%s unreadable: %s", name, type(exc).__name__)
        return None


def _host(url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(url).hostname or "").lower().rstrip(".")


def bsc_pool_rpc_url() -> tuple[str | None, str]:
    """``(url, why)`` for bsc pool reads. ``None`` -- refuse -- when unset or on a forbidden host.

    The field the lead may add to ``kaiba/core/config.py`` ``Settings`` so this can be read like
    every other endpoint (this module does not edit core): ``bsc_pool_rpc_url: str = ""``
    (env ``BSC_POOL_RPC_URL``). Until then the environment and the ``.env`` files are read
    directly, as ``launch_feed.bsc_snipe_rpc_url`` does. It must never fall back to
    ``rpc_for(BSC)``: on the box that is ``bsc-rpc.publicnode.com``, protection's host.
    """
    now = time.monotonic()
    cached = _URL_CACHE.get(BSC_POOL_RPC_ENV)
    if cached is not None and now - cached[0] < _URL_TTL_S:
        url = cached[1]
    else:
        url = _env_or_dotenv(BSC_POOL_RPC_ENV)
        _URL_CACHE[BSC_POOL_RPC_ENV] = (now, url)
    if not url:
        return None, "bsc_pool_rpc_unset"
    if _host(url) in forbidden_pool_hosts():
        return None, f"bsc_pool_rpc_shares_protection_host:{_host(url)}"
    return url, "ok"


#: Second-level labels that are part of a public suffix (``co.uk``), for :func:`operator_of`.
_SECOND_LEVEL_SUFFIXES: frozenset[str] = frozenset({"co", "com", "net", "org", "gov", "edu", "ac"})


def operator_of(host: str) -> str:
    """The registrable domain of ``host`` -- who RUNS it, as far as a per-IP limit goes.

    ``ethereum-rpc.publicnode.com`` and ``bsc-rpc.publicnode.com`` are two hosts and one operator,
    and an operator's per-IP limit is commonly shared across its hosts. The last two labels, three
    under a two-letter country code with a known second level; an IP literal is its own operator."""
    import ipaddress

    text = (host or "").strip().lower().rstrip(".")
    try:
        ipaddress.ip_address(text)
        return text
    except ValueError:
        pass
    labels = [part for part in text.split(".") if part]
    if len(labels) <= 2:
        return ".".join(labels)
    if len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def shared_rpc_urls(chain: Chain) -> list[str]:
    """Every URL an eth / base pool host must not share a host OR an operator with.

    Skepticism rule (c): protection's pool reads live on a host that no discovery, scan or snipe
    load shares and that protection does not dial for another chain. So, as this process resolves
    them:

    * protection's own readers on the other chains (:func:`protection_rpc_urls`: sol, bsc and
      robinhood, their public fallbacks, the robinhood pacer) and the bsc pool host;
    * the eth / base discovery and scan readers: ``rpc_for(ETH|BASE)`` and the public table they
      fall back to (``dyor_verify.PUBLIC_RPC``: ethereum-rpc.publicnode.com, mainnet.base.org),
      the hunters' wallet reader (``watch_wallets.PUBLIC_EVM_RPC``) and the Stockereum ingest
      (``stockereum.RPCS``: tenderly, mevblocker);
    * the snipe and feed hosts (``BSC_SNIPE_RPC_URL`` / ``_WS_URL``, the sol and robinhood feed
      endpoints);
    * the OTHER pool-only chain's pool host, read raw (it is validated by its own call).

    Every source is read lazily and on its own: one that cannot be read is skipped, the rest still
    count."""
    urls: list[str] = list(protection_rpc_urls())
    for env in (BSC_POOL_RPC_ENV, "BSC_SNIPE_RPC_URL", "BSC_SNIPE_WS_URL", "SOLANA_FEED_WS_URL",
                "SOLANA_FEED_HTTP_URL", "SOLANA_FEED_ALCHEMY_URL", "ROBINHOOD_FEED_ALCHEMY_URL"):
        urls.append(_env_or_dotenv(env) or "")
    for other in POOL_ONLY_CHAINS - {chain}:
        urls.append(_env_or_dotenv(POOL_RPC_ENV[other]) or "")
    try:
        from kaiba.core.config import get_settings

        settings = get_settings()
        urls += [settings.rpc_for(Chain.ETH) or "", settings.rpc_for(Chain.BASE) or ""]
    except Exception as exc:  # noqa: BLE001 - what cannot be read is listed from the other sources
        log.debug("shared rpc urls: settings unreadable: %s", type(exc).__name__)
    try:
        from kaiba.intelligence.dyor_verify import PUBLIC_RPC

        urls += [str(u or "") for u in PUBLIC_RPC.values()]
    except Exception as exc:  # noqa: BLE001
        log.debug("shared rpc urls: no public table: %s", type(exc).__name__)
    try:
        from kaiba.hunters.watch_wallets import PUBLIC_EVM_RPC, PUBLIC_SOL_RPC

        urls += [str(u or "") for u in PUBLIC_EVM_RPC.values()] + [str(PUBLIC_SOL_RPC or "")]
    except Exception as exc:  # noqa: BLE001
        log.debug("shared rpc urls: no hunter table: %s", type(exc).__name__)
    try:
        from kaiba.ingest.stockereum import RPCS

        urls += [str(u or "") for u in RPCS]
    except Exception as exc:  # noqa: BLE001
        log.debug("shared rpc urls: no stockereum table: %s", type(exc).__name__)
    return [u for u in urls if u and u.strip()]


def _setting(name: str) -> str | None:
    """``Settings.<name>`` -- the same files :func:`_env_or_dotenv` reads, as the process loaded them."""
    try:
        from kaiba.core.config import get_settings

        value = str(getattr(get_settings(), name, "") or "").strip()
    except Exception:  # noqa: BLE001 - an unreadable setting is "not configured"
        return None
    return value or None


def pool_rpc_url(chain: Chain) -> tuple[str | None, str]:
    """``(url, why)`` for a chain's pool reads, ``None`` -- refuse -- when it may not be read.

    bsc keeps its own rule (:func:`bsc_pool_rpc_url`). eth / base (``ETH_POOL_RPC_URL`` /
    ``BASE_POOL_RPC_URL``, also ``Settings.eth_pool_rpc_url`` / ``base_pool_rpc_url``):

    * unset -> ``<chain>_pool_rpc_unset``. NEVER a fallback: ``rpc_for(eth)`` is empty on the box and
      ``evm_price._rpc_url`` would then dial ``ethereum-rpc.publicnode.com`` -- the operator protection
      reads sol and bsc on (design 6b [R]);
    * its host is one :func:`shared_rpc_urls` lists -> ``<chain>_pool_rpc_shares_host:<host>``;
    * its OPERATOR is one of theirs (:func:`operator_of`) -> ``<chain>_pool_rpc_shares_operator:<op>``.
      A second key isolates our cooldown families, not the provider's per-IP limit."""
    if chain is Chain.BSC:
        return bsc_pool_rpc_url()
    if chain not in POOL_ONLY_CHAINS:
        return None, f"pool_host_unsupported:{chain.value}"
    env = POOL_RPC_ENV[chain]
    now = time.monotonic()
    cached = _URL_CACHE.get(env)
    if cached is not None and now - cached[0] < _URL_TTL_S:
        url = cached[1]
    else:
        url = _env_or_dotenv(env) or _setting(f"{chain.value}_pool_rpc_url")
        _URL_CACHE[env] = (now, url)
    if not url:
        return None, f"{chain.value}_pool_rpc_unset"
    host = _host(url)
    if not host:
        return None, f"{chain.value}_pool_rpc_unparseable"
    hit = _FORBIDDEN_CACHE.get(chain.value)
    if hit is not None and now - hit[0] < _URL_TTL_S:
        forbidden = hit[1]
    else:
        forbidden = frozenset({_host(u) for u in shared_rpc_urls(chain)} - {""})
        _FORBIDDEN_CACHE[chain.value] = (now, forbidden)
    if host in forbidden:
        return None, f"{chain.value}_pool_rpc_shares_host:{host}"
    owner = operator_of(host)
    if owner in {operator_of(h) for h in forbidden}:
        return None, f"{chain.value}_pool_rpc_shares_operator:{owner}"
    return url, "ok"


def breaker_refusal(chain: Chain) -> str:
    """The refusal while ``chain``'s pool host breaker is open."""
    return BREAKER_REFUSAL if chain is Chain.BSC else f"{chain.value}_pool_rpc_circuit_open"


def clear_caches() -> None:
    """Forget URL, primed reads and resolutions. For tests and an operator forcing a re-read."""
    _URL_CACHE.clear()
    _FORBIDDEN_CACHE.clear()
    _GAS.clear()
    _PRIMED.clear()
    _REFS.clear()
    _UNRESOLVED.clear()
    _DECIMALS.clear()
    _SAMPLE_SWITCH.clear()
    _CODEX_SENT.clear()
    _CODEX_LAST.clear()
    _BREAKER.clear()
    with _CLOCK_LOCK:
        _MOVED.clear()


# --------------------------------------------------------------------------------------
# the per-host circuit breaker (BREAKER_OPEN_S)
# --------------------------------------------------------------------------------------

#: key -> (consecutive failures, monotonic time it stays open until). Process-local.
_BREAKER: dict[str, tuple[int, float]] = {}
_BREAKER_LOCK = threading.Lock()


def host_key(url: str) -> str:
    return f"host:{_host(url)}"


def breaker_open(key: str) -> float | None:
    """Seconds the breaker for ``key`` stays open, or ``None`` when a send may go."""
    with _BREAKER_LOCK:
        hit = _BREAKER.get(key)
    if hit is None:
        return None
    remaining = hit[1] - time.monotonic()
    return remaining if remaining > 0 else None


def breaker_record(key: str, ok: bool) -> None:
    """One send's outcome at the host. A success closes the breaker; a failure (re)opens it."""
    with _BREAKER_LOCK:
        if ok:
            _BREAKER.pop(key, None)
            return
        fails = _BREAKER.get(key, (0, 0.0))[0] + 1
        window = min(BREAKER_MAX_OPEN_S, BREAKER_OPEN_S * (2 ** min(fails - 1, 16)))
        _BREAKER[key] = (fails, time.monotonic() + window)
    log.warning("pool reads paused %.0f s on %s after %d consecutive host failure(s)", window, key, fails)


def _our_limiter_refused(fetched: Any) -> bool:
    """True when OUR limiter refused before anything was sent, for a transient reason
    (``limiter.LOCAL_TRANSIENT_REASONS``): contention for the slot, not a host failure. A
    provider ban or cooldown after a real 429 is the host's answer and is NOT this."""
    if getattr(fetched, "ok", False):
        return False
    note = str(getattr(getattr(fetched, "receipt", None), "note", "") or "")
    if not note.startswith("rate limited:"):
        return False
    try:
        from kaiba.core.limiter import LOCAL_TRANSIENT_REASONS
    except Exception:  # noqa: BLE001
        return False
    return any(f": {reason} (retry in" in note for reason in LOCAL_TRANSIENT_REASONS)


def breaker_guard(key: str, transport: Transport) -> Transport:
    """``transport`` behind the breaker for ``key``: refused at once, with no call, while it is
    open; an all-``None`` (or raising) answer counts as a host failure. For a transport whose
    failures carry no cause (an injected one); :func:`bsc_transport` records its own."""

    def send(calls: Sequence[tuple[str, str]]) -> list[str | None]:
        if not calls:
            return []
        if breaker_open(key) is not None:
            return [None] * len(calls)
        try:
            got = list(transport(calls) or [])
        except Exception as exc:  # noqa: BLE001 - a raising transport is a failed send
            log.debug("pool transport raised: %s", exc)
            breaker_record(key, False)
            return [None] * len(calls)
        breaker_record(key, len(got) >= len(calls) and any(r is not None for r in got))
        return got

    return send


def bsc_transport(
    conn: Any = None, *, priority: Priority = Priority.EXIT, wait_for_slot_s: float = WAIT_FOR_SLOT_S,
    url: str | None = None, timeout_s: float = RPC_TIMEOUT_S, keep_reverts: bool = False,
) -> Transport:
    """ONE ``eth_call`` to Multicall3 ``aggregate3`` per send, under :data:`BSC_PROVIDER`.

    ``allowFailure`` on every sub-call: one unreadable pool costs that pool, not the batch. A
    failed sub-call comes back ``None`` -- or, with ``keep_reverts``, as ``evm_price.RpcRevert``
    when it reverted WITH data (``json_rpc_batch(keep_reverts=True)``'s contract, which
    ``evm_price.read_flap`` needs to tell the portal's ``TokenNotFound`` from a failed read). No URL
    -> ``[None] * n`` and no network at all. Behind the host's circuit breaker
    (:func:`breaker_open`): open -> ``[None] * n`` with no network.
    """
    return pool_transport(Chain.BSC, conn, priority=priority, wait_for_slot_s=wait_for_slot_s, url=url,
                          timeout_s=timeout_s, keep_reverts=keep_reverts)


def pool_transport(
    chain: Chain, conn: Any = None, *, priority: Priority = Priority.EXIT, wait_for_slot_s: float = WAIT_FOR_SLOT_S,
    url: str | None = None, timeout_s: float = RPC_TIMEOUT_S, keep_reverts: bool = False,
) -> Transport:
    """:func:`bsc_transport` for any :data:`POOL_HOST_CHAINS` chain: one Multicall3 ``aggregate3``
    per send, under that chain's own key (:data:`POOL_PROVIDER`) to its own host
    (:func:`pool_rpc_url`), behind that host's breaker. No allowed URL -> ``[None] * n``, no network."""
    provider = POOL_PROVIDER[chain]

    def send(calls: Sequence[tuple[str, str]]) -> list[str | None]:
        if not calls:
            return []
        target = url
        if target is None:
            target, _why = bsc_pool_rpc_url() if chain is Chain.BSC else pool_rpc_url(chain)
        if not target:
            return [None] * len(calls)
        key = host_key(target)
        if breaker_open(key) is not None:
            return [None] * len(calls)
        from kaiba.providers._http import post_json

        body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{"to": MULTICALL3, "data": op.encode_aggregate3(calls)}, "latest"]}
        fetched = post_json(provider, POOL_ENDPOINT, target, json_body=body, priority=priority,
                            ttl_s=0.0, wait_for_slot_s=wait_for_slot_s, timeout_s=timeout_s, conn=conn)
        data = fetched.data if fetched.ok else None
        result = data.get("result") if isinstance(data, Mapping) else None
        decoded = op.decode_aggregate3(result, len(calls))
        if decoded is None:
            if not _our_limiter_refused(fetched):
                breaker_record(key, False)
            return [None] * len(calls)
        breaker_record(key, True)
        if keep_reverts:
            from kaiba.execution.evm_price import RpcRevert

            return ["0x" + blob.hex() if ok else (RpcRevert("0x" + blob.hex()) if len(blob) >= 4 else None)
                    for ok, blob in decoded]
        return ["0x" + blob.hex() if ok else None for ok, blob in decoded]

    return send


#: The limiter key of the Flap-curve paper ENTRY read when no bsc pool host is configured
#: (:func:`bsc_curve_entry_transport`). Never ``rpc``.
BSC_CURVE_ENTRY_PROVIDER = "bsc-paper-curve"


def bsc_curve_entry_transport(conn: Any = None) -> tuple[Any, str]:
    """``(rpc, limiter key)`` for the Flap-curve paper ENTRY read (``evm_price.read_flap``).

    2026-10-23 (step 3 follow-up): it was ``evm_price.json_rpc_batch(BSC, priority=ENTRY)`` --
    provider ``rpc``, which every sol PumpSwap/curve stop and protection's bsc reads also reserve
    from and are cooled down on, posting to protection's own bsc host.

    * ``BSC_POOL_RPC_URL`` set (and allowed, :func:`bsc_pool_rpc_url`): the pool reader's key
      (:data:`BSC_PROVIDER`) and host, one Multicall3 ``aggregate3``, reverts kept, behind that
      host's breaker. Nothing reaches protection's host or ``rpc``.
    * Unset -- the box today: CHOSEN, the same host as before (``rpc_for(BSC)``), under its OWN
      limiter key :data:`BSC_CURVE_ENTRY_PROVIDER`, so a 429 or a cooldown it earns lands on that
      key, never on ``rpc``, and it never takes an ``rpc`` reservation. Not refused: refusing
      would send every bsc Flap entry to the dossier, so no bsc paper row could count at all,
      while this read is the one the box already makes, at the bsc entry rate (a few an hour, one
      batch of three ``eth_call`` s plus a cached ``decimals``) -- far below any per-IP limit. What
      stays shared is only that host's per-IP limit, at that rate.
    """
    url, _why = bsc_pool_rpc_url()
    if url:
        return bsc_transport(conn, priority=Priority.ENTRY, wait_for_slot_s=ENTRY_WAIT_FOR_SLOT_S, url=url,
                             keep_reverts=True), BSC_PROVIDER
    from kaiba.execution import evm_price as ep

    return ep.json_rpc_batch(Chain.BSC, conn=conn, priority=Priority.ENTRY, keep_reverts=True,
                             provider=BSC_CURVE_ENTRY_PROVIDER), BSC_CURVE_ENTRY_PROVIDER


#: The limiter endpoint of this module's robinhood reads. Same provider (``robinhood-rpc``) and
#: same family (``chain``) as protection's Pons reads (``chain.price``), so it shares their ONE
#: pacer and their cooldowns exactly; a name of its own only so ``provider_calls`` can count what
#: the paper entry and exit fills spend on that host (design 3d: at most the entry rate).
RH_ENDPOINT = "chain.pool"


def _only_reverts(note: str | None) -> bool:
    """True when every per-item error ``rpc_batch`` reported is an on-chain revert.

    ``rpc_batch`` turns each errored item into ``None`` and joins the errors into its note
    (``"eth_call: {error}"``, each cut to 90 characters, the whole to 200). A revert is the
    chain's answer (a quoter that cannot fill reverts); anything else -- a missing item, a
    per-item rate limit -- means the batch was not answered and nothing was learned."""
    parts = [p for p in str(note or "").split("; ") if p.strip()]
    return bool(parts) and all("revert" in p.lower() for p in parts)


def robinhood_transport(conn: Any = None, *, priority: Priority = Priority.EXIT) -> Transport:
    """The ONE robinhood pacer (``kaiba.ingest.robinhood.rpc_batch``, which is also what
    ``evm_price.robinhood_rpc_batch`` wraps): one HTTP request per send.

    A batch with a per-item error that is NOT a revert comes back all ``None``: a quoter item
    lost to a per-item rate limit must read as "not read" (retry), never as the pool refusing to
    fill (booked at zero proceeds by the paper exit)."""

    def send(calls: Sequence[tuple[str, str]]) -> list[str | None]:
        if not calls:
            return []
        from kaiba.ingest.robinhood import rpc_batch

        got = rpc_batch([("eth_call", [{"to": to, "data": data}, "latest"]) for to, data in calls],
                        endpoint=RH_ENDPOINT, priority=priority, wait_for_slot_s=WAIT_FOR_SLOT_S
                        if priority <= Priority.EXIT else ENTRY_WAIT_FOR_SLOT_S, timeout_s=RPC_TIMEOUT_S,
                        conn=conn)
        if not got.results or (not got.ok and not _only_reverts(got.note)):
            return [None] * len(calls)
        return [r if isinstance(r, str) else None for r in got.results]

    return send


def transport_for(chain: Chain, conn: Any = None, *, priority: Priority) -> Transport | None:
    if chain is Chain.BSC:
        return bsc_transport(conn, priority=priority,
                             wait_for_slot_s=ENTRY_WAIT_FOR_SLOT_S if priority >= Priority.ENTRY else WAIT_FOR_SLOT_S)
    if chain is Chain.ROBINHOOD:
        return robinhood_transport(conn, priority=priority)
    if chain in POOL_ONLY_CHAINS:
        return pool_transport(chain, conn, priority=priority,
                              wait_for_slot_s=ENTRY_WAIT_FOR_SLOT_S if priority >= Priority.ENTRY else WAIT_FOR_SLOT_S)
    return None


# --------------------------------------------------------------------------------------
# gas, on the pool host (eth / base)
# --------------------------------------------------------------------------------------

#: ``(chain) -> (monotonic s, wei, note)``. One ``eth_gasPrice`` a minute per process at most.
_GAS: dict[str, tuple[float, int, str]] = {}
GAS_TTL_S = 60.0
GAS_ENDPOINT = "rpc.gas"


def gas_price_wei(chain: Chain, conn: Any = None, *, priority: Priority = Priority.ENTRY) -> tuple[int | None, str]:
    """``eth_gasPrice`` on the chain's pool host, under its pool key: ``(wei, note)`` or ``(None, why)``.

    For the eth / base venue read (``viability.read_pool_venue``) and the paper gas guard
    (``paper_costs.gas_extra_bps``). Cached :data:`GAS_TTL_S`. MEASURED from the box 2026-10-23:
    eth 62,119,446 wei (0.062 gwei; ``eth_feeHistory`` over 20 blocks: base fee 0.039-0.050 gwei,
    median tip 0.01 gwei), base 6,000,000 wei (0.006 gwei). Base's L1 data fee is NOT included:
    ``GasPriceOracle.getL1FeeUpperBound(600)`` read 2,353,243,111 wei the same minute -- 0.16% of
    a 250,000-gas leg's L2 fee, and the broker's flat 0.00002 ETH tip per leg is ~13x the L2 fee."""
    if chain not in POOL_ONLY_CHAINS:
        return None, f"gas_unsupported:{chain.value}"
    now = time.monotonic()
    hit = _GAS.get(chain.value)
    if hit is not None and now - hit[0] < GAS_TTL_S:
        return hit[1], f"{hit[2]} (cached)"
    url, why = pool_rpc_url(chain)
    if url is None:
        return None, why
    key = host_key(url)
    if breaker_open(key) is not None:
        return None, breaker_refusal(chain)
    from kaiba.providers._http import post_json

    fetched = post_json(POOL_PROVIDER[chain], GAS_ENDPOINT, url,
                        json_body={"jsonrpc": "2.0", "id": 1, "method": "eth_gasPrice", "params": []},
                        priority=priority, ttl_s=0.0, wait_for_slot_s=ENTRY_WAIT_FOR_SLOT_S,
                        timeout_s=RPC_TIMEOUT_S, conn=conn)
    data = fetched.data if fetched.ok else None
    raw = data.get("result") if isinstance(data, Mapping) else None
    try:
        wei = int(str(raw), 16) if isinstance(raw, str) and raw.startswith("0x") else None
    except ValueError:
        wei = None
    if not wei or wei <= 0:
        if not _our_limiter_refused(fetched):
            breaker_record(key, False)
        return None, "gas_price_unreadable"
    breaker_record(key, True)
    _GAS[chain.value] = (now, wei, f"gas:{_host(url)}")
    return wei, f"gas:{_host(url)}"


# --------------------------------------------------------------------------------------
# native USD
# --------------------------------------------------------------------------------------


def mark_native_usd(chain: Chain, conn: Any) -> tuple[Decimal | None, str]:
    """The native coin in USD for a protection MARK: ``native_prices`` within its 300 s tolerance."""
    try:
        from kaiba.providers.native_price import at as native_at

        got = native_at(chain, now_ms(), conn)
    except Exception as exc:  # noqa: BLE001 - a missing sampler is a missing rate
        return None, f"native_usd_unavailable:{type(exc).__name__}"
    if got.price_usd is None or got.price_usd <= 0:
        return None, "native_usd_unavailable"
    return Decimal(got.price_usd), "native_prices"


def fill_native_usd(chain: Chain, conn: Any, *, at_ms: int | None = None) -> tuple[Decimal | None, str]:
    """The native coin in USD for a paper FILL: the very rate the broker converts with
    (``viability.paper_native_usd``), so USD and native agree inside one fill. ``None`` when the
    chain is absent there (the broker would use its placeholder): refuse rather than mix rates."""
    try:
        from kaiba.execution.viability import paper_native_usd

        found = paper_native_usd(conn, at_ms=at_ms, chains=(chain,)).get(chain)
    except Exception as exc:  # noqa: BLE001
        return None, f"paper_native_usd_unavailable:{type(exc).__name__}"
    if found is None or found <= 0:
        return None, "paper_native_usd_missing"
    return Decimal(found), "paper_native_usd"


# --------------------------------------------------------------------------------------
# resolution: which pool
# --------------------------------------------------------------------------------------

_REFS: dict[tuple[str, str], tuple[int, PoolRef]] = {}
#: (chain, token) -> (retry_after_ms, why) for a resolution that FAILED. A token with no native
#: pool must not cost a discovery send on every tick: structural answers are trusted for
#: :data:`UNRESOLVED_RETRY_MS`, a failed read (the endpoint, not the token) for
#: :data:`UNREAD_RETRY_MS`.
_UNRESOLVED: dict[tuple[str, str], tuple[int, str]] = {}
UNRESOLVED_RETRY_MS = 600_000
UNREAD_RETRY_MS = 60_000
_DECIMALS: dict[tuple[str, str], int] = {}
_LOCK = threading.Lock()
#: Serialises this module's few indexed DB reads. The watchdog prices the book on several prefetch
#: workers; on the box each worker has its own thread-local connection, but a caller that hands one
#: connection to a shared source (tests do) must not interleave statements on it.
_DB_LOCK = threading.Lock()


def _kv_key(chain: Chain, token: str) -> str:
    return f"{POOL_KV_PREFIX}{chain.value}:{token.lower()}"


def cached_ref(chain: Chain, token: str, conn: Any = None) -> PoolRef | None:
    """A resolution this process or another one already made (memory, then ``kv``)."""
    key = (chain.value, token.lower())
    hit = _REFS.get(key)
    if hit is not None and now_ms() - hit[0] < POOL_KV_MAX_AGE_MS:
        return hit[1]
    if conn is None:
        return None
    try:
        from kaiba.core.db import fetch_one, jload

        with _DB_LOCK:
            row = fetch_one(conn, "SELECT value, updated_ms FROM kv WHERE key=?", (_kv_key(chain, token),))
    except Exception as exc:  # noqa: BLE001 - an unreadable cache is a cold cache
        log.debug("pool ref cache unreadable: %s", exc)
        return None
    if not row or now_ms() - int(row.get("updated_ms") or 0) > POOL_KV_MAX_AGE_MS:
        return None
    ref = PoolRef.from_json(jload(row["value"], {}))
    if ref is not None and ref.chain is chain and ref.token == token.lower():
        _REFS[key] = (int(row.get("updated_ms") or now_ms()), ref)
        return ref
    return None


def remember_ref(ref: PoolRef, conn: Any = None) -> None:
    _REFS[(ref.chain.value, ref.token)] = (now_ms(), ref)
    if conn is None:
        return
    try:
        from kaiba.core.db import jdump, upsert

        upsert(conn, "kv", {"key": _kv_key(ref.chain, ref.token), "value": jdump(ref.to_json()),
                            "updated_ms": now_ms()}, ["key"])
    except Exception as exc:  # noqa: BLE001 - a cache we cannot write costs a re-read, nothing else
        log.debug("pool ref not cached: %s", exc)


def _decimals_of(chain: Chain, token: str, conn: Any) -> int | None:
    key = (chain.value, token.lower())
    if key in _DECIMALS:
        return _DECIMALS[key]
    if conn is None:
        return None
    try:
        from kaiba.core.db import fetch_one

        row = fetch_one(conn, "SELECT decimals FROM tokens WHERE chain=? AND address=?", (chain.value, token))
        if row is None and token != token.lower():
            row = fetch_one(conn, "SELECT decimals FROM tokens WHERE chain=? AND address=?",
                            (chain.value, token.lower()))
    except Exception:  # noqa: BLE001
        return None
    if row and row.get("decimals") is not None:
        _DECIMALS[key] = int(row["decimals"])
        return _DECIMALS[key]
    return None


def bsc_discovery_calls(token: str) -> list[tuple[str, str]]:
    """``getPair(token, WBNB)`` (v2) and ``getPool(token, WBNB, fee)`` for every v3 tier, and
    ``decimals()``: one ``aggregate3``. Only WBNB: any other partner is a foreign quote."""
    calls = [(PANCAKE_V2_FACTORY, SEL_GET_PAIR + _addr(token) + _addr(WBNB))]
    calls += [(PANCAKE_V3_FACTORY, SEL_GET_POOL + _addr(token) + _addr(WBNB) + _word(fee))
              for fee in PANCAKE_V3_FEE_TIERS]
    calls.append((token, SEL_DECIMALS))
    return calls


def bsc_candidate_calls(candidates: Sequence[tuple[str, str, int]]) -> list[tuple[str, str]]:
    """Per candidate ``(kind, pool, fee)``: identity and state. v2: token0, factory, getReserves.
    v3: token0, fee, slot0, liquidity."""
    calls: list[tuple[str, str]] = []
    for kind, pool, _fee in candidates:
        if kind == "v2":
            calls += [(pool, SEL_TOKEN0), (pool, SEL_FACTORY), (pool, SEL_GET_RESERVES)]
        else:
            calls += [(pool, SEL_TOKEN0), (pool, SEL_FEE), (pool, SEL_SLOT0), (pool, SEL_LIQUIDITY)]
    return calls


def _calls_per(kind: str) -> int:
    return 3 if kind == "v2" else 4


def bsc_candidates(token: str, results: Sequence[str | None]) -> tuple[list[tuple[str, str, int]], int | None]:
    """Discovery results -> ``([(kind, pool, fee)], decimals)``. Zero addresses are no pool."""
    out: list[tuple[str, str, int]] = []
    pair = _address_word(results[0]) if results else None
    if pair:
        out.append(("v2", pair, PANCAKE_V2_FEE_BPS))
    for fee, raw in zip(PANCAKE_V3_FEE_TIERS, results[1 : 1 + len(PANCAKE_V3_FEE_TIERS)], strict=False):
        pool = _address_word(raw)
        if pool:
            out.append(("v3", pool, fee))
    decimals = _first(results[1 + len(PANCAKE_V3_FEE_TIERS)]) if len(results) > 1 + len(PANCAKE_V3_FEE_TIERS) else None
    if decimals is not None and not 0 <= decimals <= 36:
        decimals = None
    return out, decimals


def bsc_marks(token: str, candidates: Sequence[tuple[str, str, int]], results: Sequence[str | None],
              decimals: int, *, observed_ms: int | None = None) -> tuple[list[PoolMark], list[str]]:
    """Candidate identity+state results -> verified marks, and why the others were refused."""
    token = token.lower()
    marks: list[PoolMark] = []
    why: list[str] = []
    at = 0
    seen = observed_ms if observed_ms is not None else now_ms()
    for kind, pool, fee in candidates:
        chunk = list(results[at : at + _calls_per(kind)])
        at += _calls_per(kind)
        if len(chunk) < _calls_per(kind):
            why.append(f"{kind}:{pool}:short")
            continue
        t0 = _address_word(chunk[0])
        if t0 is None:
            why.append(f"{kind}:{pool}:token0_unreadable")
            continue
        token_is0 = t0 == token
        if kind == "v2":
            if (_address_word(chunk[1]) or "") != PANCAKE_V2_FACTORY:
                why.append(f"v2:{pool}:v2_factory_unknown")
                continue
            words = _words(chunk[2])
            if len(words) < 2 or words[0] <= 0 or words[1] <= 0:
                why.append(f"v2:{pool}:reserves_empty")
                continue
            r0, r1 = words[0], words[1]
            ref = PoolRef(Chain.BSC, token, pool, "v2", "pancake-v2", token_is0, WBNB, PANCAKE_V2_FEE_BPS,
                          found_by="factory_getPair")
            dec0, dec1 = (decimals, 18) if token_is0 else (18, decimals)
            price = op.price_from_reserves(r0, r1, dec0, dec1, token_is0)
            if price is None:
                why.append(f"v2:{pool}:unpriceable")
                continue
            marks.append(PoolMark(ref, price, r1 if token_is0 else r0, (r0, r1), decimals, seen))
        else:
            pool_fee = _first(chunk[1])
            if pool_fee is None or pool_fee != fee:
                why.append(f"v3:{pool}:fee_mismatch")
                continue
            sqrt = op.sqrt_price_from_v3_slot0(_bytes(chunk[2]))
            liquidity = _first(chunk[3])
            if not sqrt or not liquidity:
                # A zero-liquidity v3 pool's slot0 is whatever the last trade left (onchain_pool).
                why.append(f"v3:{pool}:no_in_range_liquidity")
                continue
            ref = PoolRef(Chain.BSC, token, pool, "v3", "pancake-v3", token_is0, WBNB, fee,
                          found_by="factory_getPool")
            dec0, dec1 = (decimals, 18) if token_is0 else (18, decimals)
            price = op.price_from_sqrt(sqrt, dec0, dec1, token_is0)
            reserve = in_range_quote_reserve(sqrt, liquidity, quote_is0=not token_is0)
            if price is None:
                why.append(f"v3:{pool}:unpriceable")
                continue
            marks.append(PoolMark(ref, price, int(reserve) if reserve is not None else None, None, decimals, seen))
    return marks, why


def deepest(marks: Sequence[PoolMark]) -> PoolMark | None:
    """The mark whose quote side holds the most native: where a market order actually trades."""
    usable = [m for m in marks if m.quote_reserve_base]
    return max(usable, key=lambda m: int(m.quote_reserve_base or 0)) if usable else None


def _rh_pons_lp_token_id(token: str, conn: Any) -> int | None:
    """The Uniswap v4 LP NFT a Pons graduation minted, from its ``token.migrated`` event.

    ``kaiba.ingest.robinhood`` records it as ``position_id`` on the event, which carries a
    ``dedupe_key`` (so retention never deletes it). ``PositionManager.getPoolAndPositionInfo``
    then returns the PoolKey in one call.
    """
    if conn is None:
        return None
    try:
        from kaiba.core.db import fetch_one, jload

        row = fetch_one(conn, "SELECT payload FROM events WHERE dedupe_key=?",
                        (f"token.migrated:robinhood:{token}",))
        if row is None and token != token.lower():
            row = fetch_one(conn, "SELECT payload FROM events WHERE dedupe_key=?",
                            (f"token.migrated:robinhood:{token.lower()}",))
    except Exception as exc:  # noqa: BLE001
        log.debug("pons migration event unreadable: %s", exc)
        return None
    payload = jload(row["payload"], {}) if row else {}
    try:
        value = int(payload.get("position_id")) if payload.get("position_id") is not None else None
    except (TypeError, ValueError):
        return None
    return value if value and value > 0 else None


def rh_ref_from_key(token: str, key: tuple[str, str, int, int, str], found_by: str) -> tuple[PoolRef | None, str]:
    """A v4 PoolKey -> a ref for ``token``, refusing a foreign quote or a key that is not the token's."""
    c0, c1, fee, spacing, hooks = (key[0].lower(), key[1].lower(), int(key[2]), int(key[3]), key[4].lower())
    token = token.lower()
    if token == c0:
        token_is0, other = True, c1
    elif token == c1:
        token_is0, other = False, c0
    else:
        return None, "token_not_in_pool_key"
    refusal = native_quote_refusal(Chain.ROBINHOOD, None if other == ZERO else other)
    if refusal:
        return None, refusal
    pool_id = op.v4_pool_id(c0, c1, fee, spacing, hooks)
    return PoolRef(Chain.ROBINHOOD, token, pool_id, "v4", "uniswap-v4", token_is0,
                   None if other == ZERO else other, fee, spacing, hooks, found_by), "ok"


def dex_pair_hint(chain: Chain, token: str) -> str | None:
    """The pool DexScreener names for ``token`` -- a HINT for which pool to read, never a price.

    Asked at ``Priority.ENTRY`` and only on the entry path (entry rate, ~1/h on robinhood), and
    only when nothing on chain named the pool. The pool is then read and verified on chain; a
    pair whose quote side is not the native coin is not returned.
    """
    try:
        from kaiba.providers import prices

        native = {ZERO, WRAPPED_NATIVE.get(chain, ZERO)}
        found = prices.quote(chain, token, priority=Priority.ENTRY, prefer_quotes=native)
    except Exception as exc:  # noqa: BLE001 - no hint is a refusal, not a crash
        log.debug("dex pair hint failed: %s", exc)
        return None
    pair = (getattr(found, "pair_address", None) or "").lower()
    quote = (getattr(found, "quote_address", None) or "").lower()
    if not pair or native_quote_refusal(chain, quote or None) is not None:
        return None
    return pair


def resolve_robinhood(token: str, transport: Transport, conn: Any = None, *,
                      pool_hint: str | None = None,
                      hint_lookup: Callable[[str], str | None] | None = None) -> tuple[PoolRef | None, str]:
    """Which native-quoted pool prices ``token`` on robinhood. One call when it can be found.

    In order: a cached resolution; the Pons graduation's LP NFT (``getPoolAndPositionInfo``);
    ``pool_hint`` (a pool id another reader holds -- the sampler's, or the incumbent quote's),
    asked as a v4 id (``poolKeys``) or a v3 address (token0/token1/fee). Unknown -> refused;
    never a guess.
    """
    token = token.lower()
    cached = cached_ref(Chain.ROBINHOOD, token, conn)
    if cached is not None:
        return cached, "cached"
    lp = _rh_pons_lp_token_id(token, conn)
    if lp is not None:
        got = transport([(RH_POSITION_MANAGER, SEL_POOL_AND_POSITION + _word(lp))])
        key = op.decode_pool_key(_bytes(got[0])) if got else None
        if key is None or key[0] == key[1] == ZERO:
            return None, "pons_lp_key_unreadable"
        ref, why = rh_ref_from_key(token, key, "pons_lp_nft")
        if ref is not None:
            remember_ref(ref, conn)
        return ref, why
    hint = (pool_hint or "").lower()
    if not hint and hint_lookup is not None:
        hint = (hint_lookup(token) or "").lower()
    if op._is_hex(hint, 66):
        b25 = bytes.fromhex(hint[2:])[:25] + bytes(7)
        got = transport([(RH_POSITION_MANAGER, SEL_POOL_KEYS + b25.hex())])
        key = op.decode_pool_key(_bytes(got[0])) if got else None
        if key is None or key[0] == key[1] == ZERO:
            # 8 of 24 traded pools have their key only in the Initialize log (onchain_pool);
            # that log scan is not affordable on the entry path.
            return None, "v4_key_not_in_position_manager"
        ref, why = rh_ref_from_key(token, key, "pool_hint")
        if ref is not None and ref.pool != hint:
            return None, "v4_key_mismatch"
        if ref is not None:
            remember_ref(ref, conn)
        return ref, why
    if op._is_hex(hint, 42):
        got = transport([(hint, SEL_TOKEN0), (hint, SEL_TOKEN1), (hint, SEL_FEE), (hint, SEL_FACTORY)])
        if not got or len(got) < 4:
            return None, "v3_hint_unreadable"
        t0, t1, fee = _address_word(got[0]), _address_word(got[1]), _first(got[2])
        if t0 is None or t1 is None or fee is None:
            return None, "v3_hint_unreadable"
        if (_address_word(got[3]) or "") != RH_V3_FACTORY:
            # QuoterV2 only quotes pools of its own factory; any other "v3" pool is unverified.
            return None, "v3_factory_unknown"
        if token not in (t0, t1):
            return None, "token_not_in_pool"
        other = t1 if token == t0 else t0
        refusal = native_quote_refusal(Chain.ROBINHOOD, other)
        if refusal:
            return None, refusal
        ref = PoolRef(Chain.ROBINHOOD, token, hint, "v3", "uniswap-v3", token == t0, other, int(fee),
                      found_by="pool_hint")
        remember_ref(ref, conn)
        return ref, "ok"
    return None, "pool_unknown"


def _unresolved(chain: Chain, token: str, why: str, retry_ms: int) -> tuple[None, str]:
    _UNRESOLVED[(chain.value, token)] = (now_ms() + int(retry_ms), why)
    return None, why


def resolve_bsc(token: str, transport: Transport, conn: Any = None) -> tuple[PoolMark | None, str]:
    """The deepest WBNB pool of ``token``: two ``aggregate3`` calls cold, none once cached."""
    token = token.lower()
    cached = cached_ref(Chain.BSC, token, conn)
    if cached is not None:
        got = read_marks(Chain.BSC, [cached], transport, conn)
        mark = got.get(token)
        return (mark, "cached") if mark is not None else (None, "cached_pool_unreadable")
    failed = _UNRESOLVED.get((Chain.BSC.value, token))
    if failed is not None and now_ms() < failed[0]:
        return None, f"{failed[1]} (cached)"
    found = transport(bsc_discovery_calls(token))
    if not found or all(r is None for r in found):
        return _unresolved(Chain.BSC, token, "bsc_pool_discovery_failed", UNREAD_RETRY_MS)
    candidates, decimals = bsc_candidates(token, found)
    if decimals is None:
        return _unresolved(Chain.BSC, token, "token_decimals_unreadable", UNREAD_RETRY_MS)
    _DECIMALS[(Chain.BSC.value, token)] = decimals
    if not candidates:
        return _unresolved(Chain.BSC, token, "no_wbnb_pool", UNRESOLVED_RETRY_MS)
    state = transport(bsc_candidate_calls(candidates))
    if not state or all(r is None for r in state):
        return _unresolved(Chain.BSC, token, "bsc_pool_state_unreadable", UNREAD_RETRY_MS)
    marks, why = bsc_marks(token, candidates, state, decimals)
    best = deepest(marks)
    if best is None:
        return _unresolved(Chain.BSC, token, "no_usable_wbnb_pool:" + ",".join(why)[:200], UNRESOLVED_RETRY_MS)
    remember_ref(best.ref, conn)
    return best, "ok"


# --------------------------------------------------------------------------------------
# ethereum and base: Uniswap pools against WETH / native ETH, nothing else
# --------------------------------------------------------------------------------------

#: Aerodrome's factories on base, VERIFIED deployed from the box 2026-10-23 (``allPoolsLength()``
#: 29,830 on the v2 PoolFactory, 3,662 on the Slipstream CL factory). Aerodrome is excluded until
#: its fill is measured (design 6d): ``venue_unverified:aerodrome``.
AERODROME_FACTORIES: frozenset[str] = frozenset({
    "0x420dd381b31aef6683db6b902084cb0ffece40da", "0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a",
})


def _canonical_v4_quote(token: str, quote: str | None) -> tuple[bool, str, str]:
    """``(token_is0, currency0, currency1)`` for a v4 key of ``token`` against ``quote``
    (``None`` = native ETH, the zero address, which always sorts first)."""
    other = (quote or ZERO).lower()
    token_is0 = int(token, 16) < int(other, 16)
    return token_is0, (token if token_is0 else other), (other if token_is0 else token)


def v4_no_hook_refs(chain: Chain, token: str) -> list[PoolRef]:
    """Every hookless v4 pool ``token`` could have against native ETH or WETH at a standard tier."""
    token = token.lower()
    out: list[PoolRef] = []
    for quote in (None, WRAPPED_NATIVE[chain]):
        token_is0, c0, c1 = _canonical_v4_quote(token, quote)
        for fee, spacing in UNISWAP_V4_NO_HOOK_TIERS:
            out.append(PoolRef(chain, token, op.v4_pool_id(c0, c1, fee, spacing, ZERO), "v4", "uniswap-v4",
                               token_is0, quote, fee, spacing, None, "v4_no_hook_tier"))
    return out


def uniswap_discovery_calls(chain: Chain, token: str) -> list[tuple[str, str]]:
    """ONE ``aggregate3``: ``getPair(token, WETH)`` (v2), ``getPool(token, WETH, fee)`` per v3 tier,
    slot0 and liquidity of every hookless v4 candidate (:func:`v4_no_hook_refs`), and ``decimals()``."""
    d = UNISWAP[chain]
    weth = WRAPPED_NATIVE[chain]
    calls = [(d.v2_factory, SEL_GET_PAIR + _addr(token) + _addr(weth))]
    calls += [(d.v3_factory, SEL_GET_POOL + _addr(token) + _addr(weth) + _word(fee)) for fee in UNISWAP_V3_FEE_TIERS]
    for ref in v4_no_hook_refs(chain, token):
        calls += mark_calls(ref)
    calls.append((token, SEL_DECIMALS))
    return calls


def uniswap_candidates(chain: Chain, token: str, results: Sequence[str | None], *, observed_ms: int | None = None,
                       ) -> tuple[list[tuple[str, str, int]], list[PoolMark], int | None]:
    """Discovery results -> ``([(kind, pool, fee)] for v2/v3, [v4 marks], decimals)``."""
    v3n = len(UNISWAP_V3_FEE_TIERS)
    refs = v4_no_hook_refs(chain, token)
    expected = 1 + v3n + 2 * len(refs) + 1
    if len(results) < expected:
        return [], [], None
    decimals = _first(results[expected - 1])
    if decimals is not None and not 0 <= decimals <= 36:
        decimals = None
    out: list[tuple[str, str, int]] = []
    pair = _address_word(results[0])
    if pair:
        out.append(("v2", pair, UNISWAP_V2_FEE_BPS))
    for fee, raw in zip(UNISWAP_V3_FEE_TIERS, results[1:1 + v3n], strict=False):
        pool = _address_word(raw)
        if pool:
            out.append(("v3", pool, fee))
    marks: list[PoolMark] = []
    if decimals is not None:
        at = 1 + v3n
        for ref in refs:
            mark = mark_from(ref, results[at:at + 2], decimals, observed_ms=observed_ms)
            at += 2
            if mark is not None and mark.quote_reserve_base:
                marks.append(mark)
    return out, marks, decimals


def uniswap_marks(chain: Chain, token: str, candidates: Sequence[tuple[str, str, int]], results: Sequence[str | None],
                  decimals: int, *, observed_ms: int | None = None) -> tuple[list[PoolMark], list[str]]:
    """v2/v3 candidate identity+state (:func:`bsc_candidate_calls` shape) -> verified marks, and why not."""
    d = UNISWAP[chain]
    weth = WRAPPED_NATIVE[chain]
    token = token.lower()
    marks: list[PoolMark] = []
    why: list[str] = []
    at = 0
    seen = observed_ms if observed_ms is not None else now_ms()
    for kind, pool, fee in candidates:
        chunk = list(results[at:at + _calls_per(kind)])
        at += _calls_per(kind)
        if len(chunk) < _calls_per(kind):
            why.append(f"{kind}:{pool}:short")
            continue
        t0 = _address_word(chunk[0])
        if t0 is None:
            why.append(f"{kind}:{pool}:token0_unreadable")
            continue
        token_is0 = t0 == token
        dec0, dec1 = (decimals, 18) if token_is0 else (18, decimals)
        if kind == "v2":
            if (_address_word(chunk[1]) or "") != d.v2_factory:
                why.append(f"v2:{pool}:v2_factory_unknown")
                continue
            words = _words(chunk[2])
            if len(words) < 2 or words[0] <= 0 or words[1] <= 0:
                why.append(f"v2:{pool}:reserves_empty")
                continue
            ref = PoolRef(chain, token, pool, "v2", "uniswap-v2", token_is0, weth, UNISWAP_V2_FEE_BPS,
                          found_by="factory_getPair")
            price = op.price_from_reserves(words[0], words[1], dec0, dec1, token_is0)
            if price is None:
                why.append(f"v2:{pool}:unpriceable")
                continue
            marks.append(PoolMark(ref, price, words[1] if token_is0 else words[0], (words[0], words[1]), decimals, seen))
        else:
            pool_fee = _first(chunk[1])
            if pool_fee is None or pool_fee != fee:
                why.append(f"v3:{pool}:fee_mismatch")
                continue
            sqrt = op.sqrt_price_from_v3_slot0(_bytes(chunk[2]))
            liquidity = _first(chunk[3])
            if not sqrt or not liquidity:
                why.append(f"v3:{pool}:no_in_range_liquidity")
                continue
            ref = PoolRef(chain, token, pool, "v3", "uniswap-v3", token_is0, weth, fee, found_by="factory_getPool")
            price = op.price_from_sqrt(sqrt, dec0, dec1, token_is0)
            reserve = in_range_quote_reserve(sqrt, liquidity, quote_is0=not token_is0)
            if price is None:
                why.append(f"v3:{pool}:unpriceable")
                continue
            marks.append(PoolMark(ref, price, int(reserve) if reserve is not None else None, None, decimals, seen,
                                  state=(int(sqrt), int(liquidity))))
    return marks, why


#: Launchpads that put their tokens in a HOOKED Uniswap v4 pool (or a venue of their own), as GMGN
#: names them in ``tokens.launchpad`` / ``meta.launchpad_platform``. MEASURED 2026-10-23 from the box
#: (``gmgn-cli market trending|trenches --chain eth|base``, 20 + 20 rows each): on base, clanker / bankr
#: / o1 trade on the PoolManager, flaunch quotes flETH, virtuals_v2 VIRTUAL, zora a creator coin or
#: ZORA, thestonks its own exchange; on eth, stockereum / programmable_custom / sender / tokenstrategy
#: trade on the PoolManager. A launchpad deploying on v4 deploys WITH its hook -- that is what a v4
#: launchpad is -- and a hook's fee is not in slot0; V4Quoter should include it, but that is
#: unverified against a real swap (design 6d gate: 10 pools, within 50 bps). Refused, not tagged.
#: The names only make the refusal say WHY: :func:`resolve_uniswap` probes hookless pools only.
GENERIC_POOL_LAUNCHPADS: tuple[str, ...] = ("pool_uniswap", "uniswap")


def recorded_venue_refusal(chain: Chain, token: str, conn: Any) -> str | None:
    """Why the venue GMGN recorded for ``token`` (``tokens.launchpad`` and ``meta``) is one this
    module must not price, or ``None`` (nothing recorded, or a plain Uniswap pool).

    * ``meta.exchange`` is an Aerodrome factory, or names Aerodrome -> ``venue_unverified:aerodrome``;
    * ``meta.exchange`` is some other contract than Uniswap's v2/v3 factory or v4 PoolManager on this
      chain -> ``venue_unverified:<launchpad or exchange>``;
    * the PoolManager with a named launchpad -> ``v4_hook_unverified:<launchpad>``;
    * ``meta.quote_address`` is not WETH / native ETH -> ``quote_asset_not_native:<address>``."""
    if conn is None or chain not in POOL_ONLY_CHAINS:
        return None
    try:
        from kaiba.core.db import fetch_one, jload

        with _DB_LOCK:
            row = fetch_one(conn, "SELECT launchpad, meta_json FROM tokens WHERE chain=? AND address=?",
                            (chain.value, token))
            if row is None and token != token.lower():
                row = fetch_one(conn, "SELECT launchpad, meta_json FROM tokens WHERE chain=? AND address=?",
                                (chain.value, token.lower()))
    except Exception as exc:  # noqa: BLE001 - no record is no refusal; the chain read still decides
        log.debug("recorded venue unreadable: %s", exc)
        return None
    if row is None:
        return None
    meta = jload(row.get("meta_json"), {}) if row.get("meta_json") else {}
    return venue_refusal_from(chain, row.get("launchpad"), meta if isinstance(meta, dict) else {})


def venue_refusal_from(chain: Chain, launchpad: Any, meta: Mapping[str, Any]) -> str | None:
    """:func:`recorded_venue_refusal` on a record already in hand (a ``tokens`` row's ``launchpad`` and
    ``meta``, or a GMGN feed row's own fields). Pure: no read."""
    if chain not in POOL_ONLY_CHAINS:
        return None
    launchpad = str(launchpad or meta.get("launchpad_platform") or "").strip().lower()
    exchange = str(meta.get("exchange") or "").strip().lower()
    quote = str(meta.get("quote_address") or "").strip().lower()
    d = UNISWAP[chain]
    if exchange in AERODROME_FACTORIES or "aerodrome" in exchange or "aerodrome" in launchpad:
        return "venue_unverified:aerodrome"
    generic = not launchpad or launchpad.startswith(GENERIC_POOL_LAUNCHPADS)
    if looks_evm(exchange) and exchange not in (d.v2_factory, d.v3_factory, d.pool_manager):
        return f"venue_unverified:{launchpad or exchange}"[:120]
    if exchange == d.pool_manager and not generic:
        return f"v4_hook_unverified:{launchpad}"[:120]
    if looks_evm(quote):
        refusal = native_quote_refusal(chain, quote)
        if refusal:
            return refusal
    return None


def resolve_uniswap(chain: Chain, token: str, transport: Transport, conn: Any = None) -> tuple[PoolMark | None, str]:
    """The WETH / native-ETH Uniswap pool of ``token`` on eth or base (v2 pair, v3 pool, or a hookless
    v4 pool) that gives the most tokens for our ticket (:func:`best_fill`, :data:`RESOLVE_QUOTE_WEI`):
    two ``aggregate3`` sends cold (discovery; then identity, state and a quote from every candidate),
    one when nothing is found, one mark read once cached (24 h, ``kv``). A recorded launch venue this
    module cannot price refuses before any read (:func:`recorded_venue_refusal`); so does a structural
    answer remembered from an earlier read."""
    token = token.lower()
    if chain not in POOL_ONLY_CHAINS:
        return None, f"pool_chain_unsupported:{chain.value}"
    cached = cached_ref(chain, token, conn)
    if cached is not None:
        got = read_marks(chain, [cached], transport, conn)
        mark = got.get(token)
        return (mark, "cached") if mark is not None else (None, "cached_pool_unreadable")
    failed = _UNRESOLVED.get((chain.value, token))
    if failed is not None and now_ms() < failed[0]:
        return None, f"{failed[1]} (cached)"
    refusal = recorded_venue_refusal(chain, token, conn)
    if refusal is not None:
        return _unresolved(chain, token, refusal, UNRESOLVED_RETRY_MS)
    found = transport(uniswap_discovery_calls(chain, token))
    if not found or all(r is None for r in found):
        return _unresolved(chain, token, "pool_discovery_failed", UNREAD_RETRY_MS)
    candidates, marks, decimals = uniswap_candidates(chain, token, found)
    if decimals is None:
        return _unresolved(chain, token, "token_decimals_unreadable", UNREAD_RETRY_MS)
    _DECIMALS[(chain.value, token)] = decimals
    why: list[str] = []
    if not candidates and not marks:
        return _unresolved(chain, token, "no_native_pool", UNRESOLVED_RETRY_MS)
    # ONE more send: every v2/v3 candidate's identity and state, and a quote AT OUR SIZE from every
    # v3 and v4 candidate (the v2 fill is the formula on the reserves read here).
    d = UNISWAP[chain]
    weth = WRAPPED_NATIVE[chain]
    calls = bsc_candidate_calls(candidates)
    v3_at = {pool: len(calls) + i for i, (pool, _fee) in enumerate((p, f) for k, p, f in candidates if k == "v3")}
    calls += [quote_v3_call(d.v3_quoter, weth, token, RESOLVE_QUOTE_WEI, fee) for k, _p, fee in candidates if k == "v3"]
    v4_at = {mk.ref.pool: len(calls) + i for i, mk in enumerate(marks)}
    calls += [_quoter_call(mk.ref, RESOLVE_QUOTE_WEI, buy=True) for mk in marks]
    state = transport(calls)
    if not state or len(state) < len(calls) or all(r is None for r in state):
        # Nothing learned about which pool our size would trade: retry soon rather than remember (for
        # 24 h) a choice made on part of the evidence.
        return _unresolved(chain, token, "pool_state_unreadable", UNREAD_RETRY_MS)
    if candidates:
        more, more_why = uniswap_marks(chain, token, candidates, state, decimals)
        marks, why = [*marks, *more], [*why, *more_why]
    outs: dict[str, int] = {}
    for mark in marks:
        ref = mark.ref
        if ref.kind == "v2" and mark.reserves is not None:
            r0, r1 = mark.reserves
            out = v2_amount_out(RESOLVE_QUOTE_WEI, r1 if ref.token_is0 else r0, r0 if ref.token_is0 else r1, ref.fee)
        else:
            at = v3_at.get(ref.pool) if ref.kind == "v3" else v4_at.get(ref.pool)
            out = _first(state[at]) if at is not None and at < len(state) else None
        if out:
            outs[ref.pool] = int(out)
    best = best_fill(marks, outs)
    if best is None:
        return _unresolved(chain, token, "no_usable_native_pool:" + ",".join(why)[:200], UNRESOLVED_RETRY_MS)
    remember_ref(best.ref, conn)
    return best, "ok"


#: The buy every eth / base candidate pool is quoted at when a token's pool is chosen: the paper
#: ticket (``risk.yaml chains.eth|base`` min = max = 0.04 ETH). A pool is chosen for what OUR size
#: gets, not for its depth figure: a tight v3 range shows a huge in-range virtual reserve with few
#: real tokens behind it. MEASURED 2026-10-23 (dry probe, 21 admitted eth tokens): picking the
#: deepest virtual reserve chose 1-bip v3 pools whose marks sat -27.8% / +8.7% / -4.3% off GMGN.
RESOLVE_QUOTE_WEI = 4 * 10**16


def best_fill(marks: Sequence[PoolMark], outs: Mapping[str, int]) -> PoolMark | None:
    """The mark whose pool gives the most tokens for :data:`RESOLVE_QUOTE_WEI` (``outs``, by pool), as a
    single-pool route would; :func:`deepest` when no candidate could be quoted."""
    quoted = [mk for mk in marks if outs.get(mk.ref.pool)]
    if quoted:
        return max(quoted, key=lambda mk: outs[mk.ref.pool])
    return deepest(marks)


def pool_fee_bps(ref: PoolRef, mark: PoolMark | None = None) -> int | None:
    """The pool's own fee per leg in bps, rounded UP: v2 bps as recorded, v3 the tier, v4 the LP fee
    PLUS the protocol fee read in slot0 (``PoolMark.protocol_fee_pips``; v4 charges
    ``protocol + lp - protocol * lp / 1e6``). ``None`` -- unknown -- for a hooked v4 pool (its hook's
    cut is not in the key) and for a v4 pool whose slot0 was not read with it."""
    if ref.kind == "v2":
        return int(ref.fee)
    if ref.kind == "v3":
        return -(-int(ref.fee) // 100)
    if ref.hooks and ref.hooks != ZERO:
        return None
    protocol = mark.protocol_fee_pips if mark is not None else None
    if protocol is None:
        return None
    lp = int(ref.fee)
    total = Decimal(protocol + lp) - Decimal(protocol * lp) / Decimal(1_000_000)
    return int((total / 100).to_integral_value(rounding="ROUND_CEILING"))


# --------------------------------------------------------------------------------------
# marks for a resolved pool
# --------------------------------------------------------------------------------------


def pool_manager_for(chain: Chain) -> str:
    """The v4 PoolManager whose state slots a v4 ref on ``chain`` is read from."""
    deployment = UNISWAP.get(chain)
    return deployment.pool_manager if deployment is not None else RH_POOL_MANAGER


def mark_calls(ref: PoolRef) -> list[tuple[str, str]]:
    if ref.kind == "v2":
        return [(ref.pool, SEL_GET_RESERVES)]
    if ref.kind == "v3":
        return [(ref.pool, SEL_SLOT0), (ref.pool, SEL_LIQUIDITY)]
    slot = op.v4_state_slot(ref.pool)
    manager = pool_manager_for(ref.chain)
    return [(manager, SEL_EXTSLOAD + slot[2:]), (manager, SEL_EXTSLOAD + v4_liquidity_slot(ref.pool)[2:])]


def mark_from(ref: PoolRef, results: Sequence[str | None], decimals: int, *, observed_ms: int | None = None
              ) -> PoolMark | None:
    seen = observed_ms if observed_ms is not None else now_ms()
    native_dec = 18
    dec0, dec1 = (decimals, native_dec) if ref.token_is0 else (native_dec, decimals)
    if ref.kind == "v2":
        words = _words(results[0]) if results else []
        if len(words) < 2 or words[0] <= 0 or words[1] <= 0:
            return None
        price = op.price_from_reserves(words[0], words[1], dec0, dec1, ref.token_is0)
        if price is None:
            return None
        return PoolMark(ref, price, words[1] if ref.token_is0 else words[0], (words[0], words[1]), decimals, seen)
    if len(results) < 2:
        return None
    protocol: int | None = None
    if ref.kind == "v3":
        sqrt = op.sqrt_price_from_v3_slot0(_bytes(results[0]))
    else:
        fields = v4_slot0_fields(results[0])
        sqrt = fields[0] if fields else None
        if fields:
            protocol = max(fields[2] & 0xFFF, (fields[2] >> 12) & 0xFFF)
    liquidity = _first(results[1])
    if not sqrt or not liquidity:
        return None
    price = op.price_from_sqrt(sqrt, dec0, dec1, ref.token_is0)
    reserve = in_range_quote_reserve(sqrt, liquidity, quote_is0=not ref.token_is0)
    if price is None:
        return None
    return PoolMark(ref, price, int(reserve) if reserve is not None else None, None, decimals, seen,
                    protocol_fee_pips=protocol, state=(int(sqrt), int(liquidity)))


def read_marks(chain: Chain, refs: Sequence[PoolRef], transport: Transport, conn: Any = None,
               ) -> dict[str, PoolMark]:
    """Marks for every ref in ONE send (one ``aggregate3`` on bsc, one batch on robinhood).

    A token whose decimals are not yet known has ``decimals()`` read in the same send.
    """
    calls: list[tuple[str, str]] = []
    plan: list[tuple[PoolRef, int, int, int | None]] = []
    for ref in refs:
        dec = _decimals_of(chain, ref.token, conn)
        start = len(calls)
        calls += mark_calls(ref)
        dec_at = None
        if dec is None:
            dec_at = len(calls)
            calls.append((ref.token, SEL_DECIMALS))
        plan.append((ref, start, len(mark_calls(ref)), dec_at))
    if not calls:
        return {}
    results = transport(calls)
    if not results or len(results) < len(calls):
        return {}
    received = now_ms()
    out: dict[str, PoolMark] = {}
    for ref, start, count, dec_at in plan:
        dec = _decimals_of(chain, ref.token, conn)
        if dec is None and dec_at is not None:
            dec = _first(results[dec_at])
            if dec is None or not 0 <= dec <= 36:
                continue
            _DECIMALS[(chain.value, ref.token)] = dec
        if dec is None:
            continue
        mark = mark_from(ref, results[start : start + count], dec, observed_ms=received)
        if mark is not None:
            out[ref.token] = mark
    return out


# --------------------------------------------------------------------------------------
# the landing: Kaiba's own decision -> landing drift, charged on every paper ENTRY (fd_)
# --------------------------------------------------------------------------------------
#
# 2026-10-23 (fd_; review of 570c5e6 / fS_). Every robinhood / bsc / eth / base paper entry priced on the
# pool or curve -- the bsc Flap curve (``_flap_entry``, ``curve_quote``), every pool fill (``entry_fill``:
# ``v2_formula`` / ``quoter_v2`` / ``v4_quoter``) and, since fS_, the Pons curve -- was an executable at our
# size on the state read AT THE DECISION. A live buy lands seconds later, after everyone else's orders in
# between. fS_ charged that for the Pons curve only; the Flap curve and every pool entry still filled at the
# decision's state and were tagged honest. Now all of them charge the token's own flow
# (``curve_price.measure_flow`` on our ``swaps`` tape, ``drift_for`` ADVERSE) over the chain's decision ->
# landing delay, as a buy that lands on the curve or pool BEFORE ours; and an entry whose flow could not be
# measured is still filled, at the decision's state, but does not count (``paper_costs.entry_drift_unmeasured``).
#
# CAVEAT, stated not hidden: off the Pons curve the tape is partial. bsc rows come from GMGN's smart-money /
# KOL feeds only (none at all for the Flap launches on the box, 2026-10-23), a graduated robinhood token's
# pool trades likewise (the ``robinhood`` source is the curve tape and stops at graduation), eth has a few
# Stockereum rows a day and base none. A flow measured on a partial tape counts -- the same reading the Pons
# rule takes -- but it can under-state the drift.

#: Decision -> landing for a paper BUY, ms, per chain: the delay ``curve_price.drift_for`` charges the
#: token's measured flow over (ADVERSE: a buy fills into a curve or pool that has risen).
#:
#: * robinhood: MEASURED 2026-10-23 on the box (read-only): the 19 live robinhood buys whose transaction is
#:   on our own tape, all of them Pons-curve fills (09-23..10-05; 8 launch-snipe, 11 sm-trenches), decision
#:   ``ts_ms`` -> the block time of the fill: median 14.6 s (p25 9.9 s, p75 56.7 s). GMGN submit -> land was
#:   0.19 s, so the delay is our own planner and quote, not the chain.
#: * bsc: MEASURED 2026-10-23 (fd_; box ``orders`` read-only, block times from bsc-dataseed): all 64 live
#:   filled bsc buys (09-24..10-08), decision ``ts_ms`` -> block time: median 10.09 s (p25 6.7 s, p75
#:   18.4 s, max 141 s); 10.1 s rounded up.
#: * eth / base: no live fill has ever gone out there, so nothing to measure. DERIVED, pessimistic: the
#:   largest measured EVM median (robinhood's, our own planner) plus one whole block of the chain (eth's
#:   12 s slot, base's 2 s block), labelled on every entry (``latency_basis``).
#:
#: The median, not the p75: the sol paper broker charges a figure below its own measured median
#: (``curve_price.DEFAULT_LATENCY_MS``, 7.6 s, called a floor there), and this is at least as strict.
ENTRY_LATENCY_MS: dict[Chain, int] = {Chain.ROBINHOOD: 14_600, Chain.BSC: 10_100,
                                      Chain.ETH: 14_600 + 12_000, Chain.BASE: 14_600 + 2_000}
ENTRY_LATENCY_BASIS: dict[Chain, str] = {
    Chain.ROBINHOOD: "measured:robinhood_live_buys_n19_median",
    Chain.BSC: "measured:bsc_live_buys_n64_median",
    Chain.ETH: "derived:robinhood_median+eth_slot_12s",
    Chain.BASE: "derived:robinhood_median+base_block_2s",
}
#: The keys of :func:`landing_drift`'s provenance (``PaperEntry.landing``, the tag's ``entry.landing``).
LANDING_FIELDS: tuple[str, ...] = ("drift_wei", "drift_basis", "flow_basis", "flow_samples", "latency_ms",
                                   "latency_basis", "decided_ms")


def landing_drift(chain: Chain, token: str, conn: Any, at_ms: int | None) -> tuple[int, dict[str, Any]]:
    """``(drift in native base units, provenance)``: what the token's own flow moves its curve or pool by
    between our decision (``at_ms``) and a live buy's landing (:data:`ENTRY_LATENCY_MS`), ADVERSE -- the
    magnitude, as a BUY that lands before ours. ``curve_price.measure_flow`` on our ``swaps`` tape, then
    ``drift_for``: the rule fS_ applies to a Pons curve entry, here for every paper entry.

    The provenance is JSON-safe and rides on the entry as ``PaperEntry.landing`` (``drift_wei``,
    ``drift_basis``, ``flow_basis``, ``flow_samples``, ``latency_ms``, ``latency_basis``, ``decided_ms``). A
    flow the tape cannot measure (fewer than ``curve_price.MIN_FLOW_SAMPLES`` trades in the window, no
    database, an unreadable table) is ``drift_basis`` ``unavailable:...`` and ZERO drift -- because nothing was
    measured, not because nothing moved -- and such an entry does not count
    (``paper_costs.entry_drift_unmeasured``). Never raises."""
    from kaiba.core.schemas import Side
    from kaiba.execution import curve_price as cp

    decided = int(at_ms if at_ms is not None else now_ms())
    latency = int(ENTRY_LATENCY_MS.get(chain, 0))
    flow = None
    if conn is not None:
        try:
            flow = cp.measure_flow(chain, token, conn, at_ms=decided)
        except Exception as exc:  # noqa: BLE001 - no flow is "drift unavailable", labelled, never a crash
            log.debug("paper entry: flow unreadable for %s %s: %s", chain.value, token[:12], exc)
    drift, basis = cp.drift_for(flow, side=Side.BUY, latency_ms=latency)
    drift = max(0, int(drift))
    return drift, {
        "drift_wei": str(drift), "drift_basis": basis, "flow_basis": getattr(flow, "basis", "unavailable"),
        "flow_samples": int(getattr(flow, "samples", 0) or 0), "latency_ms": latency,
        "latency_basis": ENTRY_LATENCY_BASIS.get(chain, "none"), "decided_ms": decided,
    }


# --------------------------------------------------------------------------------------
# fills at our size
# --------------------------------------------------------------------------------------


def _quoter_call(ref: PoolRef, amount_in: int, *, buy: bool) -> tuple[str, str] | None:
    native = WRAPPED_NATIVE[ref.chain]
    deployment = UNISWAP.get(ref.chain)
    if ref.kind == "v3":
        quoter = (PANCAKE_V3_QUOTER if ref.chain is Chain.BSC
                  else deployment.v3_quoter if deployment is not None else RH_V3_QUOTER)
        token_in, token_out = (native, ref.token) if buy else (ref.token, native)
        return quote_v3_call(quoter, token_in, token_out, amount_in, ref.fee)
    if ref.kind == "v4" and (ref.chain is Chain.ROBINHOOD or deployment is not None):
        if deployment is not None and ref.hooks and ref.hooks != ZERO:
            # eth / base: a hooked pool is refused until V4Quoter is verified against a real swap
            # through it (design 6d). Never reached through resolve_uniswap; a guard for a cached ref.
            return None
        # zeroForOne sells currency0 for currency1. Buying the token sells the native side.
        zero_for_one = (not ref.token_is0) if buy else ref.token_is0
        return quote_v4_call(RH_V4_QUOTER if deployment is None else deployment.v4_quoter, ref.v4_key,
                             zero_for_one, amount_in)
    return None


def _usd_per_token(native_atoms: int, token_atoms: int, token_decimals: int, native_usd: Decimal) -> Decimal | None:
    if native_atoms <= 0 or token_atoms <= 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 50
        native_whole = Decimal(native_atoms) / (Decimal(10) ** 18)
        token_whole = Decimal(token_atoms) / (Decimal(10) ** int(token_decimals))
        return +(native_whole * native_usd / token_whole)


#: Why a BUY cannot be priced at our size once Kaiba's landing drift (:func:`landing_drift`) landed first.
POOL_CANNOT_FILL_AFTER_DRIFT = "pool_cannot_fill_after_latency_drift"


def fill_from(ref: PoolRef, mark: PoolMark | None, quoter_result: str | None, *, side: str,
              amount_in: int, native_usd: Decimal, decimals: int | None = None,
              observed_ms: int | None = None, drift_in: int = 0,
              drift_results: Sequence[str | None] | None = None) -> tuple[Fill | None, str]:
    """A fill from what one send returned. v2 from the reserves of ``mark``; v3/v4 from the quoter.

    A BUY with ``drift_in`` > 0 (native base units, :func:`landing_drift`; fd_ 2026-10-23) is filled AFTER a buy
    of ``drift_in`` landed on the pool first: v2 in sequence on the pair's own ``getAmountOut`` (the drift's whole
    input joins the reserves, fee included, as it does on chain); v3 / v4 as ``quote(drift + ours) -
    quote(drift)`` from the two extra quoter answers in ``drift_results`` -- exact on a v3 pool, whose fee
    leaves the curve, and on a hooked v4 pool while the hook's take is proportional to the input. Either answer
    missing is no fill (:data:`POOL_CANNOT_FILL_AFTER_DRIFT`): the pool cannot say what our size gets after the
    drift. The result's ``landing`` keeps ``atoms_at_decision``, the same size's answer before the drift."""
    buy = side == "buy"
    drift = max(0, int(drift_in or 0)) if buy else 0
    if decimals is None:
        decimals = mark.token_decimals if mark is not None else None
    if decimals is None:
        return None, "token_decimals_unknown"
    at_decision = None
    if ref.kind == "v2":
        if mark is None or mark.reserves is None:
            return None, "v2_reserves_unreadable"
        r0, r1 = mark.reserves
        r_token, r_native = (r0, r1) if ref.token_is0 else (r1, r0)
        out = (v2_amount_out(amount_in, r_native, r_token, ref.fee) if buy
               else v2_amount_out(amount_in, r_token, r_native, ref.fee))
        basis = "v2_formula"
        if drift and out:
            at_decision = out
            moved = v2_amount_out(drift, r_native, r_token, ref.fee)
            if not moved or moved >= r_token:
                return None, POOL_CANNOT_FILL_AFTER_DRIFT
            out = v2_amount_out(amount_in, r_native + drift, r_token - moved, ref.fee)
            if not out:
                return None, POOL_CANNOT_FILL_AFTER_DRIFT
    else:
        out = _first(quoter_result)
        basis = "quoter_v2" if ref.kind == "v3" else "v4_quoter"
        if not out:
            return None, f"{basis}_refused"
        if drift:
            at_decision = out
            answers = [*list(drift_results or []), None, None][:2]
            first, both = _first(answers[0]), _first(answers[1])
            if first is None or both is None or both <= first:
                return None, POOL_CANNOT_FILL_AFTER_DRIFT
            out = both - first
    if not out:
        return None, "pool_cannot_fill_this_size"
    native_atoms, token_atoms = (amount_in, out) if buy else (out, amount_in)
    price = _usd_per_token(native_atoms, token_atoms, decimals, native_usd)
    if price is None or price <= 0:
        return None, "fill_priced_to_zero"
    spot = mark.price_native * native_usd if mark is not None else None
    liquidity = (Decimal(2) * Decimal(mark.quote_reserve_base) / (Decimal(10) ** 18) * native_usd
                 if mark is not None and mark.quote_reserve_base else None)
    landing = ({"atoms_at_decision": str(int(at_decision)), "drift_in_wei": str(drift)}
               if at_decision is not None else None)
    return Fill(ref, side, int(amount_in), int(out), spot, price, liquidity, native_usd, basis,
                observed_ms if observed_ms is not None else now_ms(), landing=landing), "ok"


def quote_fill(ref: PoolRef, transport: Transport, conn: Any, *, side: str, amount_in: int,
               native_usd: Decimal, drift_in: int = 0) -> tuple[Fill | None, str]:
    """ONE send: the pool's mark (and decimals, if unknown) plus the quoter at ``amount_in`` -- and, for a BUY
    after a landing drift on a v3 / v4 pool (``drift_in`` > 0, :func:`fill_from`), the quoter at ``drift_in``
    and at ``drift_in + amount_in`` in the same send."""
    calls = list(mark_calls(ref))
    count = len(calls)
    buy = side == "buy"
    drift = max(0, int(drift_in or 0)) if buy else 0
    quoter = None if ref.kind == "v2" else _quoter_call(ref, amount_in, buy=buy)
    if ref.kind != "v2" and quoter is None:
        return None, f"no_quoter:{ref.chain.value}:{ref.kind}"
    if quoter is not None:
        calls.append(quoter)
    drift_at = None
    if quoter is not None and drift:
        drift_calls = [_quoter_call(ref, drift, buy=True), _quoter_call(ref, drift + int(amount_in), buy=True)]
        if any(c is None for c in drift_calls):
            return None, f"no_quoter:{ref.chain.value}:{ref.kind}"
        drift_at = len(calls)
        calls.extend(c for c in drift_calls if c is not None)
    dec = _decimals_of(ref.chain, ref.token, conn)
    dec_at = None
    if dec is None:
        dec_at = len(calls)
        calls.append((ref.token, SEL_DECIMALS))
    results = transport(calls)
    if not results or len(results) < len(calls):
        return None, "pool_read_failed"
    if dec is None and dec_at is not None:
        dec = _first(results[dec_at])
        if dec is None or not 0 <= dec <= 36:
            return None, "token_decimals_unreadable"
        _DECIMALS[(ref.chain.value, ref.token)] = dec
    if dec is None:
        return None, "token_decimals_unreadable"
    mark = mark_from(ref, results[:count], dec)
    if all(r is None for r in results):
        return None, "pool_read_failed"
    return fill_from(ref, mark, results[count] if quoter is not None else None, side=side,
                     amount_in=amount_in, native_usd=native_usd, decimals=dec, drift_in=drift,
                     drift_results=results[drift_at:drift_at + 2] if drift_at is not None else None)


def entry_fill(chain: Chain, token: str, size_base_units: int, conn: Any, *,
               transport: Transport | None = None, pool_hint: str | None = None,
               hint_lookup: Callable[[str], str | None] | None = None,
               at_ms: int | None = None) -> tuple[Fill | None, str]:
    """What a paper BUY of ``size_base_units`` native would get, from the token's own pool.

    ``None`` with a named reason whenever it cannot be priced at our size; the caller then falls
    back to the dossier and the report marks the trade not-honest (step 8).
    """
    if chain not in POOL_CHAINS:
        return None, f"pool_chain_unsupported:{chain.value}"
    addr = (token or "").strip().lower()
    if not looks_evm(addr):
        return None, "not_an_evm_address"
    if int(size_base_units or 0) <= 0:
        return None, "no_size"
    native_usd, why = fill_native_usd(chain, conn, at_ms=at_ms)
    if native_usd is None:
        return None, why
    rpc = transport or transport_for(chain, conn, priority=Priority.ENTRY)
    if rpc is None:
        return None, "no_transport"
    if chain is Chain.BSC:
        url, why = bsc_pool_rpc_url() if transport is None else ("injected", "ok")
        if url is None:
            return None, why
        if transport is None and breaker_open(host_key(url)) is not None:
            return None, BREAKER_REFUSAL
        mark, why = resolve_bsc(addr, rpc, conn)
        if mark is None:
            return None, why
        ref = mark.ref
    elif chain in POOL_ONLY_CHAINS:
        url, why = pool_rpc_url(chain) if transport is None else ("injected", "ok")
        if url is None:
            return None, why
        if transport is None and breaker_open(host_key(url)) is not None:
            return None, breaker_refusal(chain)
        mark, why = resolve_uniswap(chain, addr, rpc, conn)
        if mark is None:
            return None, why
        ref = mark.ref
    else:
        ref, why = resolve_robinhood(addr, rpc, conn, pool_hint=pool_hint,
                                     hint_lookup=hint_lookup or (lambda t: dex_pair_hint(Chain.ROBINHOOD, t)))
        if ref is None:
            return None, why
    # 2026-10-23 (fd_): filled after Kaiba's own landing drift (:func:`landing_drift`), never at the decision's
    # pool state; an unmeasured drift is zero here and the entry does not count (paper_costs).
    drift, landing = landing_drift(chain, addr, conn, at_ms)
    fill, why = quote_fill(ref, rpc, conn, side="buy", amount_in=int(size_base_units), native_usd=native_usd,
                           drift_in=drift)
    if fill is None:
        return None, why
    return replace(fill, landing={**landing, **(fill.landing or {})}), why


def _sample_pool(position_id: str, conn: Any) -> tuple[str | None, str | None]:
    """The pool the robinhood sampler last read for ``position_id`` (indexed by position, ts)."""
    try:
        from kaiba.core.db import fetch_one

        row = fetch_one(conn, "SELECT pool, pool_kind FROM onchain_price_samples WHERE position_id=? "
                              "ORDER BY ts_ms DESC LIMIT 1", (position_id,))
    except Exception:  # noqa: BLE001 - no table, no hint
        return None, None
    return (str(row["pool"]).lower(), row.get("pool_kind")) if row and row.get("pool") else (None, None)


def exit_fill(position: Any, qty: int, conn: Any, *, qty_decimals: int, transport: Transport | None = None,
              pool_hint: str | None = None) -> tuple[Fill | None, str]:
    """What a paper SELL of ``qty`` would receive. One call at EXIT (two the first time a token's
    decimals are unknown).

    ``qty`` is in ``qty_decimals`` units -- the paper broker's (``paper._token_decimals``: the
    recorded decimals, else 18 on EVM) -- and is converted to the token's own atoms before the
    quoter sees it. The deciding mark only triggered the exit; this is the fill. ``None`` with a
    reason when the pool cannot be read: the caller then fills at the mark and tags the trade.
    """
    chain = position.chain
    if chain not in POOL_CHAINS:
        return None, f"pool_chain_unsupported:{chain.value}"
    if int(qty or 0) <= 0:
        return None, "nothing_to_sell"
    addr = str(position.token).lower()
    native_usd, why = fill_native_usd(chain, conn)
    if native_usd is None:
        return None, why
    rpc = transport or transport_for(chain, conn, priority=Priority.EXIT)
    if rpc is None:
        return None, "no_transport"
    if chain is Chain.BSC:
        url, why = bsc_pool_rpc_url() if transport is None else ("injected", "ok")
        if url is None:
            return None, why
        if transport is None and breaker_open(host_key(url)) is not None:
            return None, BREAKER_REFUSAL
        ref = cached_ref(chain, addr, conn)
        if ref is None:
            mark, why = resolve_bsc(addr, rpc, conn)
            if mark is None:
                return None, why
            ref = mark.ref
    elif chain in POOL_ONLY_CHAINS:
        url, why = pool_rpc_url(chain) if transport is None else ("injected", "ok")
        if url is None:
            return None, why
        if transport is None and breaker_open(host_key(url)) is not None:
            return None, breaker_refusal(chain)
        ref = cached_ref(chain, addr, conn)
        if ref is None:
            mark, why = resolve_uniswap(chain, addr, rpc, conn)
            if mark is None:
                return None, why
            ref = mark.ref
    else:
        hint = pool_hint or _sample_pool(str(position.position_id), conn)[0]
        ref, why = resolve_robinhood(addr, rpc, conn, pool_hint=hint)
        if ref is None:
            return None, why
    decimals = _decimals_of(chain, addr, conn)
    if decimals is None:
        got = rpc([(addr, SEL_DECIMALS)])
        decimals = _first(got[0]) if got else None
        if decimals is None or not 0 <= decimals <= 36:
            return None, "token_decimals_unreadable"
        _DECIMALS[(chain.value, addr)] = decimals
    atoms = int(qty) * 10 ** int(decimals) // 10 ** int(qty_decimals)
    if atoms <= 0:
        return None, "nothing_to_sell"
    return quote_fill(ref, rpc, conn, side="sell", amount_in=atoms, native_usd=native_usd)


#: What a paper EXIT does when :func:`exit_fill` refuses (``watchdog.DefaultExitSubmitter._paper``).
#: 2026-10-23 (step 3 review, blocking #5): every refusal used to fill at the deciding mark and drop
#: the row from the scoreboard, and the refusals are NOT outcome-blind -- a drained pool is a rug.
#:
#: * :data:`EXIT_NO_FILL` -- the pool was READ and answered that it cannot pay for this quantity:
#:   the quoter returned 0 or reverted, a v2 pair has no reserves, the fill priced to zero, or every
#:   WBNB pool found is drained. Booked at ZERO proceeds for the leg (``paper_costs.NO_FILL_BOUND``):
#:   what a sell into that pool would get.
#: * :data:`EXIT_RETRY` -- nothing was learned about the pool: the send failed, the pacer refused,
#:   the host's breaker is open, the reader raised. Asked again on the next tick before anything
#:   falls back to the mark.
#: * :data:`EXIT_AT_MARK` -- this module cannot price the token at all (no bsc pool host
#:   configured, no pool known for it, a foreign quote asset, no native USD rate for the fill --
#:   the pool was never asked): filled at the mark, labelled.
EXIT_NO_FILL = "no_fill"
EXIT_RETRY = "retry"
EXIT_AT_MARK = "mark"
_NO_FILL_HEADS: frozenset[str] = frozenset({
    "quoter_v2_refused", "v4_quoter_refused", "pool_cannot_fill_this_size", "fill_priced_to_zero",
    "v2_reserves_unreadable",
})
_DRAINED_REASONS: tuple[str, ...] = ("reserves_empty", "no_in_range_liquidity")
_RETRY_HEADS: frozenset[str] = frozenset({
    "pool_read_failed", "bsc_pool_discovery_failed", "bsc_pool_state_unreadable", "cached_pool_unreadable",
    "token_decimals_unreadable", "token_decimals_unknown", "pons_lp_key_unreadable", "v3_hint_unreadable",
    BREAKER_REFUSAL, "pool_exit_fill_raised",
    # eth / base (resolve_uniswap, pool_transport's breakers)
    "pool_discovery_failed", "pool_state_unreadable",
    *(f"{c.value}_pool_rpc_circuit_open" for c in POOL_ONLY_CHAINS),
})
#: Heads that, like bsc's ``no_usable_wbnb_pool``, list why each found pool was refused.
_NO_USABLE_HEADS: frozenset[str] = frozenset({"no_usable_wbnb_pool", "no_usable_native_pool"})


def exit_refusal_kind(why: str | None) -> str:
    """:data:`EXIT_NO_FILL`, :data:`EXIT_RETRY` or :data:`EXIT_AT_MARK` for an :func:`exit_fill`
    refusal. Unknown refusals go to the mark: the conservative bound needs evidence that the POOL
    said no, and a retry needs evidence that nothing was read."""
    text = str(why or "").strip()
    if text.endswith(" (cached)"):
        text = text[: -len(" (cached)")]
    head = text.split(" ", 1)[0].split(":", 1)[0]
    if head in _NO_FILL_HEADS:
        return EXIT_NO_FILL
    if head in _NO_USABLE_HEADS:
        reasons = [r for r in text.split(":", 1)[-1].split(",") if r.strip()]
        if reasons and all(r.strip().endswith(_DRAINED_REASONS) for r in reasons):
            return EXIT_NO_FILL
        return EXIT_AT_MARK
    if head in _RETRY_HEADS:
        return EXIT_RETRY
    return EXIT_AT_MARK


# --------------------------------------------------------------------------------------
# the paper entry: curve before graduation, pool after
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PaperEntry:
    """What ``engine.paper_entry_quote`` hands the broker for a robinhood or bsc paper buy.

    ``price_usd`` is the MARK (curve or pool spot) and ``executable_price_usd`` what our size
    actually gets per whole token; the broker charges the gap between them as impact
    (``PaperBroker.buy(executable_price_usd=)``). ``tax_in_price`` says the fill already netted
    the token's buy tax (Flap's ``flap_buy_net_quote``), so the call site must not charge it again.
    """

    source: str  # pool:<venue>:<basis> | curve:flap | curve:pons
    price_usd: Decimal
    liquidity_usd: Decimal
    executable_price_usd: Decimal | None
    fill: str  # v2_formula | quoter_v2 | v4_quoter | curve_quote | curve_mark
    buy_tax_bps: int | None = None  # the venue's own figure, when it has one (Flap record)
    sell_tax_bps: int | None = None
    tax_in_price: bool = False
    pool_id: str | None = None
    note: str = ""
    #: How the pool or curve was found (``PoolRef.found_by``; ``pons_curve`` for a Pons curve
    #: read). ``paper_costs.robinhood_pons_launch`` reads it: ``pons_lp_nft`` proves a Pons launch.
    found_by: str = ""
    #: A Pons curve entry: the curve's own per-leg take, ``feeBps() + creatorTaxBps()``, read in the
    #: same batch as the price (``evm_price.read_pons``). ``None`` = not read: the engine then
    #: charges the flat stand-in and tags the entry not honest (``paper_costs.curve_take_required``).
    curve_take_bps: int | None = None
    #: A Pons curve entry FILLED at our size (``fill`` ``curve_quote``, :func:`_pons_exact_fill`): what the
    #: curve took and gave on its own arithmetic after Kaiba's latency drift -- ``quote_in``, ``net_wei``
    #: (what reached the curve), ``atoms``, ``drift_wei`` / ``drift_basis`` / ``latency_ms``, ``snipe_bps``.
    #: JSON-safe; the engine records it on the tag so the exit can put our holding back
    #: (``evm_price.pons_with_holding``). ``None`` on every other fill. 2026-10-23 (hB_).
    curve_fill: dict[str, Any] | None = None
    #: Kaiba's decision -> landing drift on this entry (:func:`landing_drift`): what was charged before our
    #: fill and whether it was MEASURED (``drift_basis`` ``adverse:...``). The engine records it on the tag
    #: (``entry.landing``); a pool or curve entry without a measured one does not count
    #: (``paper_costs.entry_drift_unmeasured``). ``None`` on a curve MARK (no fill at our size). fd_ 2026-10-23.
    landing: dict[str, Any] | None = None


def _flap_entry(token: str, size: int, conn: Any, native_usd: Decimal, rpc: Any, *,
                at_ms: int | None = None) -> tuple[PaperEntry | None, str]:
    """The bsc Flap curve BUY of ``size`` wei, filled at our size on the curve's own arithmetic
    (``FlapCurve.tokens_out`` after ``flap_buy_net_quote``) -- on the curve as a live buy would find it when it
    LANDS: the record read at the decision plus Kaiba's own adverse landing drift (:func:`landing_drift`, bsc
    :data:`ENTRY_LATENCY_MS`) bought in first (``FlapCurve.with_buy``). The whole drift is taken as quote that
    reached the curve (none of it lost to the protocol fee or the buy tax): the pessimistic reading, the one
    fS_ takes on the Pons curve. A drift that would have carried the curve past its graduation point refuses
    (``flap_graduated_during_latency``): the token would have left the curve before our buy landed.

    2026-10-23 (fd_): it was filled at the DECISION's curve with no drift at all, since 570c5e6, and tagged
    honest. A flow the tape cannot measure is still ZERO drift here (the fill is exact at the decision's
    state), and such an entry runs but does not count (``paper_costs.entry_drift_unmeasured``)."""
    from kaiba.execution import evm_price as ep

    priced, why = ep.read_flap(token, rpc)
    if priced is None:
        return None, why
    if priced.quote_token is not None:
        return None, f"quote_asset_not_native:{priced.quote_token}"
    curve = priced.curve
    if curve is None or curve.buy_tax_bps is None:
        return None, "flap_curve_tax_unreadable"
    drift, landing = landing_drift(Chain.BSC, token, conn, at_ms)
    filled_from = curve
    if drift > 0:
        room = curve.max_size_base_units
        if room is not None and drift > room:
            return None, "flap_graduated_during_latency"
        filled_from = curve.with_buy(drift, curve.tokens_out(drift) or 0)
    net = ep.flap_buy_net_quote(size, int(curve.buy_tax_bps))
    atoms = filled_from.tokens_out(net)
    at_decision = curve.tokens_out(net) if drift > 0 else atoms
    if not atoms:
        return None, ("flap_cannot_fill_after_latency_drift" if drift > 0 and at_decision
                      else "flap_cannot_fill_this_size")
    exec_usd = _usd_per_token(size, atoms, curve.token_decimals, native_usd)
    reserve = priced.quote_reserve_whole
    if exec_usd is None or reserve is None or reserve <= 0:
        return None, "flap_fill_unpriceable"
    note = f"{priced.note}; drift {landing['drift_wei']} wei ({landing['drift_basis']}) over {landing['latency_ms']} ms"
    landing = {**landing, "atoms": str(atoms), "atoms_at_decision": str(at_decision or 0)}
    return PaperEntry(
        source="curve:flap", price_usd=priced.price_quote_per_token * native_usd,
        liquidity_usd=reserve * native_usd, executable_price_usd=exec_usd, fill="curve_quote",
        buy_tax_bps=int(curve.buy_tax_bps),
        sell_tax_bps=None if curve.sell_tax_bps is None else int(curve.sell_tax_bps),
        tax_in_price=True, pool_id=ep.FLAP_PORTAL, note=note[:200], landing=landing,
    ), "ok"


#: Decision -> landing for a robinhood Pons curve BUY, ms (:data:`ENTRY_LATENCY_MS`, where the measurement
#: is described): the delay Kaiba's own drift model (``curve_price.drift_for``, ADVERSE) is charged over on a
#: paper Pons entry.
PONS_ENTRY_LATENCY_MS = ENTRY_LATENCY_MS[Chain.ROBINHOOD]


def _pons_exact_fill(token: str, size: int, curve: Any, conn: Any, native_usd: Decimal,
                     at_ms: int | None) -> tuple[dict[str, Any] | None, Decimal | None, str]:
    """``(provenance, executable USD per whole token, why)`` for a paper buy of ``size`` wei on a Pons curve.

    The curve's own arithmetic at OUR size (``evm_price.PonsCurve.tokens_out``, MEASURED to the atom against
    4,303 chain buys and 80 ``eth_simulateV1`` quotes), on the curve as it would be when a live buy LANDS:
    the state read at the decision plus Kaiba's own adverse latency drift (``curve_price.measure_flow`` on
    this token's tape, ``drift_for`` over :data:`PONS_ENTRY_LATENCY_MS`) and the snipe tax at the landing
    second. The simulation Vanguard runs is exact at the decision's state, not at landing (review section 6
    item 2); this charges the landing. No extra call: the state is the one ``read_pons`` already read.

    ``(None, None, why)`` whenever the answer would not be a fill at our size: no curve state, an unread
    creator tax, a fill or a drift that carries the curve past graduation, or the broker's token decimals
    disagreeing with the chain's (its quantity would then be in other units).

    A flow the tape cannot measure (``drift_basis`` ``unavailable:...``: fewer than
    ``curve_price.MIN_FLOW_SAMPLES`` trades in the window, or the ``pons_trade`` tape behind) is charged
    ZERO drift here -- the fill is exact at the decision's state, not at landing, and a curve that
    graduated during the delay is not detected. Such an entry still runs, but it does not count:
    ``paper_costs.pons_drift_charged`` (fS_, 2026-10-23). The drift is :func:`landing_drift`'s, the one rule
    every paper entry now uses (fd_); its provenance is in the returned dict."""
    if curve is None:
        return None, None, "no_curve_state"
    why = curve.refusal
    if why is not None:
        return None, None, why
    if conn is not None:
        try:
            from kaiba.execution.paper import _token_decimals  # noqa: PLC0415 - the broker's own units

            broker_decimals = int(_token_decimals(Chain.ROBINHOOD, token, conn, None))
        except Exception:  # noqa: BLE001 - unknown units are no exact fill, never a guess
            return None, None, "broker_decimals_unreadable"
        if broker_decimals != int(curve.token_decimals):
            return None, None, f"broker_decimals_{broker_decimals}_vs_chain_{curve.token_decimals}"
    decided = int(at_ms if at_ms is not None else now_ms())
    drift, landing = landing_drift(Chain.ROBINHOOD, token, conn, decided)
    filled_from = curve
    if drift > 0:
        if drift >= curve.headroom:
            return None, None, "pons_graduated_during_latency"
        filled_from = curve.after_buy(drift)
    landing_s = (decided + PONS_ENTRY_LATENCY_MS) // 1000
    atoms, net, why = filled_from.tokens_out(size, at_s=landing_s)
    if atoms is None:
        return None, None, why
    exec_usd = _usd_per_token(size, atoms, curve.token_decimals, native_usd)
    if exec_usd is None or exec_usd <= 0:
        return None, None, "pons_fill_unpriceable"
    provenance = {
        **landing, "quote_in": str(size), "net_wei": str(net), "atoms": str(atoms),
        "snipe_bps": filled_from.snipe_tax_bps_at(landing_s), "take_bps": curve.take_bps,
    }
    return provenance, exec_usd, "ok"


def _pons_entry(token: str, conn: Any, native_usd: Decimal, rpc: Any, *, size: int = 0,
                at_ms: int | None = None) -> tuple[PaperEntry | None, str]:
    from kaiba.execution import evm_price as ep

    priced, why = ep.read_pons(token, rpc)
    if priced is None:
        return None, why
    if priced.quote_token is not None:
        return None, f"quote_asset_not_native:{priced.quote_token}"
    reserve = priced.quote_reserve_whole
    if reserve is None or reserve <= 0:
        return None, "pons_reserve_unreadable"
    # 2026-10-23: the curve's own take rides along (read in the same batch, no extra call); the
    # engine charges it (paper_costs.pons_curve_venue_bps).
    from kaiba.execution import paper_costs

    fee, tax = getattr(priced, "fee_bps", None), getattr(priced, "creator_tax_bps", None)
    take = paper_costs.pons_curve_take_bps(fee, tax)
    mark = priced.price_quote_per_token * native_usd
    # 2026-10-23 (hB_; Vanguard review section 6 item 2): the buy is FILLED at our size on the curve's own
    # arithmetic (``fill`` ``curve_quote``, an executable the broker uses as is). Before, no executable went
    # to the broker, which then approximated the fill as a constant product against the curve's REAL quote
    # reserve -- the 1.68 ETH phantom quote that prices the curve left out -- and refused the one robinhood
    # launch-snipe decision so far at 2,252 bps (0.02808 ETH into 0.09602 ETH of real reserve) where the
    # curve charges ~2.6%. Without the curve state the old mark remains, and the broker's record of that
    # approximation keeps it off the scoreboard (paper_costs.leg_at_size).
    fill_note = "not exact: no size" if int(size or 0) <= 0 else ""
    if int(size or 0) > 0:
        provenance, executable, fill_note = _pons_exact_fill(token, int(size), getattr(priced, "pons_curve", None),
                                                             conn, native_usd, at_ms)
        if provenance is not None and executable is not None:
            note = (f"{priced.note}; filled on the curve at our size: {provenance['atoms']} atoms for "
                    f"{provenance['quote_in']} wei (net {provenance['net_wei']}), take {take} bps, drift "
                    f"{provenance['drift_wei']} wei ({provenance['drift_basis']}) over {PONS_ENTRY_LATENCY_MS} ms")
            return PaperEntry(
                source="curve:pons", price_usd=mark, liquidity_usd=reserve * native_usd,
                executable_price_usd=executable, fill=paper_costs.CURVE_QUOTE, pool_id=priced.pool_id,
                note=note[:200], found_by="pons_curve", curve_take_bps=take, curve_fill=provenance,
                landing={key: provenance[key] for key in LANDING_FIELDS if key in provenance},
            ), "ok"
    return PaperEntry(
        source="curve:pons", price_usd=mark,
        liquidity_usd=reserve * native_usd, executable_price_usd=None, fill="curve_mark",
        pool_id=priced.pool_id,
        note=f"{priced.note}; feeBps {fee}, creatorTaxBps {tax}; mark only ({fill_note})"[:200],
        found_by="pons_curve", curve_take_bps=take,
    ), "ok"


#: Curve refusals that mean "this token is not on that curve (any more)", so the pool is the
#: right next place to look. Anything else (a failed read) refuses: a flaky node must not
#: silently move a curve token onto an unrelated pool (``viability.FLAP_PROBE_UNAVAILABLE``).
_OFF_CURVE: tuple[str, ...] = (
    "flap_not_on_curve", "flap_not_a_flap_token", "flap_no_portal_record",
    # pons_price: a complete curve, and the constant-product failure that IS how a graduated Pons
    # curve reads (evm_price.PONS_INVARIANT_TOLERANCE); a token the factory never launched.
    "pons_curve_complete", "pons_constant_product_failed", "pons_curve_unknown", "pons_curve_absent",
)


def paper_entry(chain: Chain, token: str, size_base_units: int, conn: Any, *,
                curve_rpc: Any = None, pool_rpc: Transport | None = None,
                pool_hint: str | None = None, hint_lookup: Callable[[str], str | None] | None = None,
                at_ms: int | None = None) -> tuple[PaperEntry | None, str]:
    """The paper entry for a robinhood / bsc token: its curve while it has one, its pool after.

    ``None`` with every refusal named when neither prices it at our size; the engine then falls
    back to the dossier, and the trade is tagged not-honest (``paper_costs.record_entry_basis``).
    """
    if chain not in POOL_CHAINS:
        return None, f"pool_chain_unsupported:{chain.value}"
    addr = (token or "").strip().lower()
    if not looks_evm(addr):
        return None, "not_an_evm_address"
    size = int(size_base_units or 0)
    if size <= 0:
        return None, "no_size"
    native_usd, why = fill_native_usd(chain, conn, at_ms=at_ms)
    if native_usd is None:
        return None, why
    if chain in POOL_ONLY_CHAINS:
        # No launch curve on eth / base: the token's own Uniswap pool, or a named refusal.
        fill, pool_why = entry_fill(chain, addr, size, conn, transport=pool_rpc, at_ms=at_ms)
        if fill is None:
            return None, f"pool:{pool_why}"
        if fill.spot_price_usd is None or not fill.liquidity_usd:
            return None, "pool:mark_unreadable"
        return PaperEntry(
            source=fill.source, price_usd=fill.spot_price_usd, liquidity_usd=fill.liquidity_usd,
            executable_price_usd=fill.executable_price_usd, fill=fill.basis, pool_id=fill.ref.pool,
            note=(f"{fill.ref.kind} pool via {fill.ref.found_by}, fee {fill.ref.fee} (v2 bps, v3/v4 pips), "
                  f"quote {fill.ref.quote or 'native'}")[:200],
            found_by=fill.ref.found_by, landing=fill.landing,
        ), "ok"
    if curve_rpc is None:
        from kaiba.execution import evm_price as ep

        curve_rpc = (ep.robinhood_rpc_batch(conn=conn, priority=Priority.ENTRY) if chain is Chain.ROBINHOOD
                     else bsc_curve_entry_transport(conn)[0])
    try:
        entry, curve_why = (_flap_entry(addr, size, conn, native_usd, curve_rpc, at_ms=at_ms) if chain is Chain.BSC
                            else _pons_entry(addr, conn, native_usd, curve_rpc, size=size, at_ms=at_ms))
    except Exception as exc:  # noqa: BLE001 - a curve reader never takes the entry down
        entry, curve_why = None, f"curve_reader_raised:{type(exc).__name__}"
    if entry is not None:
        return entry, "ok"
    if not any(marker in curve_why for marker in _OFF_CURVE):
        return None, f"curve:{curve_why}"
    fill, pool_why = entry_fill(chain, addr, size, conn, transport=pool_rpc, pool_hint=pool_hint,
                                hint_lookup=hint_lookup, at_ms=at_ms)
    if fill is None:
        return None, f"curve:{curve_why}; pool:{pool_why}"
    if fill.spot_price_usd is None or not fill.liquidity_usd:
        return None, f"curve:{curve_why}; pool:mark_unreadable"
    return PaperEntry(
        source=fill.source, price_usd=fill.spot_price_usd, liquidity_usd=fill.liquidity_usd,
        executable_price_usd=fill.executable_price_usd, fill=fill.basis, pool_id=fill.ref.pool,
        note=f"{fill.ref.kind} pool via {fill.ref.found_by}; off curve: {curve_why}", found_by=fill.ref.found_by,
        landing=fill.landing,
    ), "ok"


# --------------------------------------------------------------------------------------
# protection leaves
# --------------------------------------------------------------------------------------

_PRIMED: dict[tuple[str, str], PoolMark] = {}

#: ``(chain, token) -> (pool state last read, this process's first read, last CHANGE)`` on the
#: pool-only chains: their answer to the sol reserve clock (``pumpswap_price._MOVED``), read by
#: ``Watchdog._last_trade_ms`` and mirrored there to ``kv reserve_clock:<chain>:<token>``.
#:
#: 2026-10-23 review (blocking #1): on eth / base the swaps tape holds almost nothing (no wallet
#: feed; base 0 rows a day, eth ~210, Stockereum only) and a pool's mark only moves when someone
#: trades, so a position on a token that stopped trading sat between the -30% stop and TP1 for
#: good: ``stale_no_volume`` saw an unknown last trade and by design never fired, and the position
#: held a per-(lane, chain) shadow slot and one of the 25 every chain draws from. The pool this
#: module reads every protection tick IS the activity record: a swap moves sqrtPrice (v3/v4) or
#: the reserves (v2).
_MOVED: dict[tuple[str, str], tuple[tuple[int, ...], int, int]] = {}
_CLOCK_LOCK = threading.Lock()
_MOVED_MAX = 20_000


def pool_state(mark: PoolMark) -> tuple[int, ...] | None:
    """What a swap on ``mark``'s pool changes: the pool's id, then v2 ``(reserve0, reserve1)`` or v3 /
    v4 ``(sqrtPriceX96, liquidity)``. ``None`` when the mark carries neither. A different pool is a
    different state, so a re-resolution restarts the silence (later, never earlier)."""
    raw = mark.reserves if mark.ref.kind == "v2" else mark.state
    if not raw:
        return None
    try:
        return (int(mark.ref.pool, 16), *(int(x) for x in raw))
    except (TypeError, ValueError):
        return None


def note_pool_state(mark: PoolMark) -> None:
    """Record one protection read of a pool-only chain's pool (:data:`_MOVED`). Never raises."""
    if mark.ref.chain not in POOL_ONLY_CHAINS:
        return
    state = pool_state(mark)
    if state is None:
        return
    key, at = (mark.ref.chain.value, mark.ref.token.lower()), int(mark.observed_ms)
    with _CLOCK_LOCK:
        prev = _MOVED.get(key)
        if prev is None:
            _MOVED[key] = (state, at, at)
        elif prev[0] != state:
            _MOVED[key] = (state, prev[1], max(at, prev[2]))
        if len(_MOVED) > _MOVED_MAX:
            _MOVED.clear()


def pool_clock(chain: Chain, token: str) -> tuple[tuple[int, ...], int, int] | None:
    """``(state last read, this process's first read, last change)`` for ``token``'s pool, or None if
    this process has never read it. Before the first change the last change IS the first read, so
    silence is only ever claimed for time actually watched (``pumpswap_price.last_moved_ms``)."""
    with _CLOCK_LOCK:
        return _MOVED.get((chain.value, str(token or "").lower()))


def _quote_from_mark(mark: PoolMark, native_usd: Decimal, note: str) -> Any:
    from kaiba.execution.watchdog import SETTLE_NATIVE_POOL, PriceQuote

    liquidity = (Decimal(2) * Decimal(mark.quote_reserve_base) / (Decimal(10) ** 18) * native_usd
                 if mark.quote_reserve_base else None)
    return PriceQuote(
        price_usd=mark.price_native * native_usd,
        liquidity_usd=liquidity,
        basis=EvidenceBasis.VERIFIED_ONCHAIN,
        observed_ms=mark.observed_ms,
        source=f"evm-pool:{mark.ref.venue}",
        note=note,
        chain=mark.ref.chain,
        token=mark.ref.token,
        pool_id=mark.ref.pool,
        venue=mark.ref.venue,
        liquidity_kind="pool_reserve_usd" if mark.ref.kind == "v2" else "v3_in_range_virtual_usd",
        quote_token=mark.ref.quote,
        settlement=SETTLE_NATIVE_POOL,
    )


class PoolPriceSource:
    """``watchdog.PriceSource`` for bsc pools, with a per-tick :meth:`prime`.

    :meth:`prime` reads every primed token's pool in ONE ``aggregate3``; :meth:`quote` then
    answers from that read for :data:`PRIMED_MAX_AGE_MS`. Unprimed (a book of one, or a token
    first seen this tick), :meth:`quote` reads its own pool -- one call. A token with no cached
    resolution is resolved inside the prime's send only when it is cheap enough: discovery is
    two sends, so it is done by :meth:`quote` (never on the prime) and cached for every later tick.
    """

    name = "evm-pool"

    def __init__(self, conn: Any = None, *, chain: Chain = Chain.BSC, transport: Transport | None = None,
                 priority: Priority = Priority.EXIT) -> None:
        self.conn = conn
        self.chain = chain
        self._transport = transport
        self.priority = priority

    def _url(self) -> tuple[str | None, str]:
        return bsc_pool_rpc_url() if self.chain is Chain.BSC else pool_rpc_url(self.chain)

    def _breaker_key(self) -> str | None:
        if self._transport is not None:
            return f"injected:{id(self._transport)}"
        if self.chain in POOL_HOST_CHAINS:
            url = self._url()[0]
            return host_key(url) if url else None
        return None

    def _rpc(self, *, prime: bool = False) -> Transport | None:
        if self._transport is not None:
            # An injected transport says nothing about why it failed: every empty answer counts.
            return breaker_guard(f"injected:{id(self._transport)}", self._transport)
        if self.chain in POOL_HOST_CHAINS:
            url = self._url()[0]
            if url is None:
                return None
            # pool_transport records the host's breaker itself (it can tell our limiter's
            # contention from a host failure). The prime is bounded: see PRIME_TIMEOUT_S.
            return pool_transport(self.chain, self.conn, priority=self.priority, url=url,
                                  wait_for_slot_s=PRIME_WAIT_FOR_SLOT_S if prime else WAIT_FOR_SLOT_S,
                                  timeout_s=PRIME_TIMEOUT_S if prime else RPC_TIMEOUT_S)
        return transport_for(self.chain, self.conn, priority=self.priority)

    def _refusal(self) -> str | None:
        if self._transport is None and self.chain in POOL_HOST_CHAINS:
            url, why = self._url()
            if not url:
                return why
        key = self._breaker_key()
        remaining = breaker_open(key) if key else None
        if remaining is not None:
            return f"{breaker_refusal(self.chain)}:{remaining:.0f}s"
        return None

    def prime(self, chain: Chain, tokens: Sequence[str]) -> int:
        """Read the pools of ``tokens`` in one send. Returns how many were primed. Never raises.

        Bounded (:data:`PRIME_WAIT_FOR_SLOT_S`, :data:`PRIME_TIMEOUT_S`) and behind the host's
        breaker: it runs on the tick thread before any stop is decided."""
        if chain is not self.chain or not tokens:
            return 0
        try:
            if self._refusal() is not None:
                return 0
            rpc = self._rpc(prime=True)
            if rpc is None:
                return 0
            refs = [ref for ref in (cached_ref(chain, t, self.conn) for t in dict.fromkeys(str(t).lower() for t in tokens))
                    if ref is not None]
            if not refs:
                return 0
            marks = read_marks(chain, refs, rpc, self.conn)
            with _LOCK:
                for token, mark in marks.items():
                    _PRIMED[(chain.value, token)] = mark
            for mark in marks.values():
                note_pool_state(mark)
            return len(marks)
        except Exception as exc:  # noqa: BLE001 - a prime that fails leaves quote() to read alone
            log.debug("pool prime failed on %s: %s", chain.value, exc)
            return 0

    def quote(self, chain: Chain, token: str) -> Any:
        from kaiba.execution.watchdog import PriceQuote

        if chain is not self.chain:
            return PriceQuote.unavailable(f"pool reader is {self.chain.value} only", self.name)
        addr = str(token or "").strip().lower()
        if not looks_evm(addr):
            return PriceQuote.unavailable("not_an_evm_address", self.name)
        if addr in (ZERO, WRAPPED_NATIVE.get(chain, ZERO)):
            # The native coin has no pool against itself; never spend a discovery send on it.
            return PriceQuote.unavailable("native_asset_has_no_token_pool", self.name)
        refusal = self._refusal()
        if refusal is not None:
            return PriceQuote.unavailable(refusal, self.name)
        native_usd, why = mark_native_usd(chain, self.conn)
        if native_usd is None:
            return PriceQuote.unavailable(f"pool mark needs a native price: {why}", self.name)
        primed = _PRIMED.get((chain.value, addr))
        if primed is not None and 0 <= now_ms() - primed.observed_ms <= PRIMED_MAX_AGE_MS:
            return _quote_from_mark(primed, native_usd, f"{primed.ref.kind} pool, primed this tick")
        rpc = self._rpc()
        if rpc is None:
            return PriceQuote.unavailable("no pool transport", self.name)
        try:
            ref = cached_ref(chain, addr, self.conn)
            if ref is not None:
                mark = read_marks(chain, [ref], rpc, self.conn).get(addr)
                why = "cached_pool_unreadable" if mark is None else "ok"
            elif chain is Chain.BSC:
                mark, why = resolve_bsc(addr, rpc, self.conn)
            elif chain in POOL_ONLY_CHAINS:
                mark, why = resolve_uniswap(chain, addr, rpc, self.conn)
            else:
                mark, why = None, "pool_unresolved"
        except Exception as exc:  # noqa: BLE001 - a reader never takes the tick down
            return PriceQuote.unavailable(f"pool reader raised {type(exc).__name__}", self.name)
        if mark is None:
            return PriceQuote.unavailable(f"no pool mark: {why}", self.name)
        note_pool_state(mark)
        return _quote_from_mark(mark, native_usd, f"{mark.ref.kind} pool, read alone")


def _poll_interval_s() -> float:
    try:
        from kaiba.execution.protection import protection_config

        value = float(protection_config().poll_interval_s or 0)
        return value if value > 0 else DEFAULT_POLL_INTERVAL_S
    except Exception:  # noqa: BLE001
        return DEFAULT_POLL_INTERVAL_S


#: ``protection.onchain_sample_marks`` -- whether :class:`OnchainSampleSource` may decide robinhood
#: stops. OFF by default, and why: the design's own gate (step 3a [R]) is
#: ``kaiba.learning.onchain_vs_feed``, built to answer exactly this (audit-20261001 #3: "ship only if
#: the shadow stop realizes better on >=30 stops"). Run on the box 2026-10-21 with
#: ``--since-days 14 --include-shadow``: 240 closed robinhood positions, 44 with samples, 31 stop
#: exits, 14 crossed by both series -> "INSUFFICIENT: keep logging; do not switch the decision
#: source". The leaf is built and wired so flipping the key is the whole change once it passes.
#: Paper P&L does not wait for it: exits are FILLED from the pool at our size either way.
SAMPLE_MARKS_KEY = "onchain_sample_marks"
_SAMPLE_SWITCH: dict[str, tuple[float, bool]] = {}


def sample_marks_enabled() -> bool:
    now = time.monotonic()
    hit = _SAMPLE_SWITCH.get(SAMPLE_MARKS_KEY)
    if hit is not None and now - hit[0] < 30.0:
        return hit[1]
    try:
        from kaiba.core.config import get_risk

        value = bool((get_risk().protection or {}).get(SAMPLE_MARKS_KEY, False))
    except Exception:  # noqa: BLE001 - an unreadable switch is off
        value = False
    _SAMPLE_SWITCH[SAMPLE_MARKS_KEY] = (now, value)
    return value


class OnchainSampleSource:
    """``watchdog.PriceSource`` for robinhood: the sampler's newest on-chain pool price. 0 RPC.

    ``onchain_pool.OnchainPriceLog`` reads every open robinhood position's pool once per tick in
    one ``aggregate3`` on ``robinhood-rpc`` and writes ``onchain_price_samples``. This answers
    from the newest row for an open position on ``token`` when it is at most
    :data:`SAMPLE_MAX_POLLS` poll intervals old, its pool is native-quoted and its USD leg came
    from the ETH sampler. Anything else is unavailable, and the chain moves on. Only while
    :data:`SAMPLE_MARKS_KEY` is on (``enabled`` pins it for a test).
    """

    name = "onchain-sample"

    def __init__(self, conn: Any = None, *, max_age_ms: int | None = None,
                 clock_ms: Callable[[], int] = now_ms, enabled: bool | None = None) -> None:
        self.conn = conn
        self._max_age_ms = max_age_ms
        self._clock_ms = clock_ms
        self._enabled = enabled

    def max_age_ms(self) -> int:
        if self._max_age_ms is not None:
            return int(self._max_age_ms)
        return int(SAMPLE_MAX_POLLS * _poll_interval_s() * 1000)

    def _conn(self) -> Any:
        if self.conn is not None:
            return self.conn
        from kaiba.core.db import get_conn

        return get_conn()

    def quote(self, chain: Chain, token: str) -> Any:
        from kaiba.execution.watchdog import SETTLE_NATIVE_POOL, PriceQuote

        if chain is not Chain.ROBINHOOD:
            return PriceQuote.unavailable("onchain samples are robinhood only", self.name)
        if not (self._enabled if self._enabled is not None else sample_marks_enabled()):
            return PriceQuote.unavailable(f"protection.{SAMPLE_MARKS_KEY} is off", self.name)
        try:
            from kaiba.core.db import fetch_all, fetch_one

            conn = self._conn()
            best: dict[str, Any] | None = None
            with _DB_LOCK:
                ids = [r["position_id"] for r in fetch_all(
                    conn, "SELECT position_id FROM positions WHERE chain=? AND token=? AND closed_ms IS NULL",
                    (Chain.ROBINHOOD.value, token))]
                for pid in ids:
                    row = fetch_one(conn, "SELECT ts_ms, price_usd, pool, pool_kind, quote_token, source "
                                          "FROM onchain_price_samples WHERE position_id=? ORDER BY ts_ms DESC "
                                          "LIMIT 1", (pid,))
                    if row and (best is None or int(row["ts_ms"]) > int(best["ts_ms"])):
                        best = row
        except Exception as exc:  # noqa: BLE001 - no table, no sample
            return PriceQuote.unavailable(f"onchain samples unreadable: {type(exc).__name__}", self.name)
        if best is None:
            return PriceQuote.unavailable("no onchain sample for an open position", self.name)
        age = self._clock_ms() - int(best["ts_ms"])
        if age < 0 or age > self.max_age_ms():
            return PriceQuote.unavailable(f"onchain sample {age} ms old (max {self.max_age_ms()})", self.name)
        refusal = native_quote_refusal(Chain.ROBINHOOD, best.get("quote_token") or None)
        if refusal:
            return PriceQuote.unavailable(refusal, self.name)
        usd_leg = str(best.get("source") or "").split("|", 1)[-1]
        if not usd_leg.startswith("eth_usd"):
            return PriceQuote.unavailable(f"onchain sample priced via {usd_leg or 'unknown'}", self.name)
        try:
            price = Decimal(str(best["price_usd"]))
        except (InvalidOperation, TypeError, ValueError):
            return PriceQuote.unavailable("onchain sample price unparseable", self.name)
        if not price.is_finite() or price <= 0:
            return PriceQuote.unavailable("onchain sample price not positive", self.name)
        kind = str(best.get("pool_kind") or "pool")
        return PriceQuote(
            price_usd=price,
            basis=EvidenceBasis.DERIVED,
            observed_ms=int(best["ts_ms"]),
            freshness_budget_s=self.max_age_ms() / 1000,
            source=f"{self.name}:{kind}",
            note=f"onchain_pool sample, {age} ms old",
            chain=Chain.ROBINHOOD,
            token=token,
            pool_id=str(best["pool"]).lower() if best.get("pool") else None,
            venue=f"uniswap-{kind}",
            quote_token=best.get("quote_token") or None,
            settlement=SETTLE_NATIVE_POOL,
        )


#: The independent backup quote: ``(chain, token) -> PriceQuote | None``. ``None`` = unwired.
BackupQuote = Callable[[Chain, str], Any]
_BACKUP: dict[str, BackupQuote | None] = {"fn": None}


def set_backup_quote(fn: BackupQuote | None) -> None:
    """Install (or remove, with ``None``) the process-wide backup quote callable."""
    _BACKUP["fn"] = fn


# ------------------------------------------------------------------ Codex.io, sampled

#: Codex.io (``kaiba.providers.codex_io``) is the independent backup: an aggregate price across a
#: token's pools, from another operator, so it is NOT a second read of the pool a stop fired on.
#: It is quota-guarded at 10,000 CU per 30 days (8,000 CU ceiling in ``codex_io``), ~4 CU per
#: price call: ~2,000 calls a month for EVERY user of the key. That is a SAMPLED cross-check, never
#: a per-tick price. So, on top of ``codex_io``'s own ceiling:
#:
#: * at most :data:`CODEX_BACKUP_MAX_PER_HOUR` calls an hour in this process (720 a month, about
#:   2,900 CU at worst -- a bounded share of the ceiling, the rest stays for research);
#: * at most one call per token per :data:`CODEX_BACKUP_TOKEN_GAP_MS`;
#: * robinhood and bsc only, and only when a key is configured (no key: no request, no event).
#:
#: The leaf is listed LAST in the bsc and robinhood routes but is CORROBORATION-ONLY
#: (:attr:`BackupPriceSource.corroboration_only`): no ``FallbackPriceSource`` ever asks it for a
#: tick's price, and ``Watchdog._validate_quote`` asks it only after every ordinary leaf (GMGN
#: included) failed to corroborate a stop on a new quote identity. What it answers decides that
#: tick and is never remembered as the position's quote identity. 2026-10-23 (step 3 review,
#: blocking #3): as an ordinary leaf AHEAD of the box's GMGN leaf it answered one tick an hour for
#: a GMGN-priced position, became the remembered identity, and turned GMGN's next stop into an
#: uncorroborated transition -- the position went blind (a shadow one was written off).
#: 2026-10-23 (step 6): eth and base too -- Codex networks 1 and 8453 (``codex_io.NETWORK_IDS``,
#: MEASURED via getNetworks 2026-10-21). Their routes have no DexScreener and no curve, so this sampled
#: aggregate (and GMGN, the box's last leaf) are what corroborates a first-tick stop on a pool mark.
#: The hourly budget below is per process and SHARED with bsc and robinhood: it is not raised.
CODEX_BACKUP_CHAINS: frozenset[Chain] = frozenset({Chain.BSC, Chain.ROBINHOOD, Chain.ETH, Chain.BASE})
CODEX_BACKUP_MAX_PER_HOUR = 1
CODEX_BACKUP_TOKEN_GAP_MS = 30 * 60_000
#: The backup is asked on protection's tick thread (``Watchdog._validate_quote``), with a stop
#: waiting on it. 2026-10-23 (step 3 follow-up): it inherited ``codex_io``'s 15 s HTTP timeout, so
#: one slow Codex answer could hold every other position's decision for 15 s. Now bounded at
#: :data:`CODEX_BACKUP_MAX_S`: the slot wait plus httpx's connect and read phases at
#: :data:`CODEX_BACKUP_TIMEOUT_S` each (one attempt). A Codex price call answers in well under a
#: second when Codex is healthy; past this it is a refusal, and the position is blind one tick.
CODEX_BACKUP_WAIT_FOR_SLOT_S = 0.5
CODEX_BACKUP_TIMEOUT_S = 1.5
CODEX_BACKUP_MAX_S = 4.0
_CODEX_SENT: list[int] = []  # epoch ms of this process's calls in the last hour
_CODEX_LAST: dict[tuple[str, str], int] = {}
_CODEX_LOCK = threading.Lock()


def _codex_budget(chain: Chain, token: str, now: int) -> str | None:
    """``None`` and the call is booked, or why not. Atomic under one lock."""
    with _CODEX_LOCK:
        del _CODEX_SENT[: len([t for t in _CODEX_SENT if now - t >= 3_600_000])]
        last = _CODEX_LAST.get((chain.value, token))
        if last is not None and now - last < CODEX_BACKUP_TOKEN_GAP_MS:
            return "codex_backup_token_gap"
        if len(_CODEX_SENT) >= CODEX_BACKUP_MAX_PER_HOUR:
            return "codex_backup_hourly_budget_spent"
        _CODEX_SENT.append(now)
        _CODEX_LAST[(chain.value, token)] = now
        return None


def codex_backup_quote(chain: Chain, token: str) -> Any:
    """Codex.io's aggregate price for ``token`` as a ``PriceQuote``, within the sampled budget.

    ``pool_id`` is left empty on purpose: the number is an aggregate across pools, which is what
    makes it independent of the pool a stop fired on. ``observed_ms`` is when WE asked; Codex's
    own last-update timestamp is in the note.
    """
    from kaiba.execution.watchdog import SETTLE_AGGREGATOR, PriceQuote

    name = "codex-io"
    if chain not in CODEX_BACKUP_CHAINS:
        return PriceQuote.unavailable(f"codex backup is {sorted(c.value for c in CODEX_BACKUP_CHAINS)} only", name)
    addr = str(token or "").strip().lower()
    if not looks_evm(addr) or addr in (ZERO, WRAPPED_NATIVE.get(chain, ZERO)):
        return PriceQuote.unavailable("codex backup prices tokens, not the native coin", name)
    try:
        from kaiba.providers import codex_io

        if not codex_io._api_key():
            return PriceQuote.unavailable("codex_io_api_key is not configured", name)
    except Exception as exc:  # noqa: BLE001 - an absent provider is a refusal
        return PriceQuote.unavailable(f"codex_io unavailable: {type(exc).__name__}", name)
    asked = now_ms()
    refused = _codex_budget(chain, addr, asked)
    if refused is not None:
        return PriceQuote.unavailable(refused, name)
    got = codex_io.price(chain, addr, priority=Priority.POSITION, wait_for_slot_s=CODEX_BACKUP_WAIT_FOR_SLOT_S,
                         timeout_s=CODEX_BACKUP_TIMEOUT_S)
    if got is None or got.price_usd is None or got.price_usd <= 0:
        return PriceQuote.unavailable("codex-io returned no price", name)
    return PriceQuote(
        price_usd=Decimal(got.price_usd),
        liquidity_usd=Decimal(got.liquidity_usd) if got.liquidity_usd is not None and got.liquidity_usd > 0 else None,
        basis=EvidenceBasis.PROVIDER_REPORTED,
        observed_ms=asked,
        source=f"{name}:aggregate",
        note=f"codex aggregate price, last update ts={got.ts_s}, block={got.block_number}",
        chain=chain,
        token=token,
        liquidity_kind="aggregate_pooled_usd" if got.liquidity_usd else None,
        settlement=SETTLE_AGGREGATOR,
    )


class BackupPriceSource:
    """An INDEPENDENT leaf behind the pool, for ``Watchdog._validate_quote`` to corroborate a stop
    with. Never a second read of the same pool: an answer carrying the pool's own ``pool_id`` is
    skipped by the watchdog, and an answer for another chain or token is refused here.

    Corroboration ONLY (``watchdog.corroboration_only``): a ``FallbackPriceSource`` skips it, the
    settlement walk skips it, and the watchdog asks it last and never remembers its answer. It is
    sampled (about one call an hour), so as an ordinary leaf it would set a quote identity that
    nothing can follow on the next tick."""

    name = "backup"
    corroboration_only = True

    def __init__(self, fn: BackupQuote | None = None) -> None:
        self._fn = fn

    def quote(self, chain: Chain, token: str) -> Any:
        from kaiba.execution.watchdog import PriceQuote

        fn = self._fn if self._fn is not None else _BACKUP["fn"]
        if fn is None:
            return PriceQuote.unavailable("backup_unwired", self.name)
        try:
            got = fn(chain, token)
        except Exception as exc:  # noqa: BLE001 - a dead backup is a refusal
            return PriceQuote.unavailable(f"backup raised {type(exc).__name__}", self.name)
        if not isinstance(got, PriceQuote):
            return PriceQuote.unavailable("backup returned no quote", self.name)
        if (got.chain is not None and got.chain != chain) or (got.token is not None and got.token != token):
            return PriceQuote.unavailable("backup answered for another token", self.name)
        if not got.source or got.source == "none":
            got = got.model_copy(update={"source": f"{self.name}:unnamed"})
        return got.model_copy(update={"chain": chain, "token": token})


__all__ = [
    "BSC_PROVIDER",
    "BackupPriceSource",
    "Fill",
    "OnchainSampleSource",
    "PaperEntry",
    "POOL_CHAINS",
    "POOL_ONLY_CHAINS",
    "POOL_PROVIDER",
    "UNISWAP",
    "best_fill",
    "gas_price_wei",
    "note_pool_state",
    "pool_clock",
    "pool_fee_bps",
    "pool_state",
    "pool_rpc_url",
    "pool_transport",
    "recorded_venue_refusal",
    "resolve_uniswap",
    "venue_refusal_from",
    "PoolMark",
    "PoolPriceSource",
    "PoolRef",
    "bsc_pool_rpc_url",
    "bsc_transport",
    "clear_caches",
    "codex_backup_quote",
    "entry_fill",
    "exit_fill",
    "native_quote_refusal",
    "paper_entry",
    "quote_v3_call",
    "quote_v4_call",
    "read_marks",
    "set_backup_quote",
    "v2_amount_out",
]
