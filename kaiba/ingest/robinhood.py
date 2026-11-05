"""Pons V2 on Robinhood Chain: the launch venue that out-earns pump.fun on fees.

``docs/EDGE-AND-VARIABLES.md`` §3a records the finding that moved this venue to the top of
the board, and ``docs/research/11-bsc-robinhood-edge-2026.md`` records what the chain
itself says about it. We have had a ``pons-robinhood`` lane in ``kaiba/execution/lanes.py``
since Phase 1 and it has never seen an event, because every ingest path we owned was
Solana. This module is that lane's missing input.

**Every address below was established by decoding transactions, not by reading a blog.**
The two addresses this task was handed as "very likely the Pons launchpad" were both wrong,
and the way they were wrong is worth keeping in view:

* ``0x9689992f5b5c09447f15906d8d11214944488341`` — 100 bytes, most-called address in a
  12-block sample — is an ERC-1967 proxy for an unrelated protocol. It emits exactly one
  event type (``0x5603e2fc…``, which is not in any Pons ABI) and nothing it does touches a
  Pons contract. Call volume identified the busiest contract on the chain, not ours.
* ``0x0f47b6850d52d3e2db8a89886bbd1a3f327f3f05`` — 10 KB, selector ``0xd04c6983`` — is a
  Pons **bonding curve**, but only one of thousands: Pons deploys a fresh curve contract
  per token, all with byte-identical 10,229-byte code. ``0xd04c6983`` is
  ``sell(uint256,uint256,address)`` on that curve. Hard-coding it would have pointed us at
  one dead memecoin forever.

The launchpad is the factory, and a factory is identified by what it *emits*, not by how
often it is called. See :data:`FACTORY_V2` for the proof chain.

Three properties of this venue shape everything below.

**There is no websocket and no filter API.** ``wss://`` on the public RPC is refused
(HTTP 400) and ``eth_newBlockFilter`` returns "method does not exist". So this is a poller,
not a subscriber, and the honest latency floor is the poll interval plus one round trip.
Blocks are ~101.5 ms, so a 2-second poll is ~20 blocks behind by construction. We measure
the lag rather than assuming it (:func:`latency_stats`).

**Graduation is atomic with the buy that triggers it.** On pump.fun, migration is a
separate transaction minutes after the curve completes, which is what makes
``migration-fade`` tradeable. Here the buy that pushes ``realQuoteReserve`` past
``graduationThreshold`` also creates the Uniswap v4 pool, seeds it, emits ``PoolGraduated``
and executes the first v4 swap — all in one transaction (proof:
``0xdfbe43f771383c6b338b40d12de207b9e499a2abe7996ee1c5a6facd510a3c8e``). There is no
window to act in. This is a finding about the venue, not a limitation of the listener, and
it is the single most important thing in this file for anyone sizing a strategy.

**The quote token is not always ETH.** 78% of launches quote in native ETH; the rest
quote in ERC-20s with their own decimals. Summing those amounts as wei is how you
overstate volume by orders of magnitude, so every curve carries its quote token and
non-ETH curves are counted separately rather than blended (:class:`CurveMeta`).

**A poll's RPC cost is capped, and that is load-bearing.** ``robinhood-rpc`` has no
``limiter.DEFAULTS`` entry, so it refills at one call a second and answers a crossing
with a 60-second cooldown on the whole ``chain.*`` family. The first design let the
block-timestamp fetch scale with the window width, and two live runs showed the spiral
that produces: the bucket empties, `chain.*` goes dark for a minute, the minute of
backlog needs even more timestamp calls, and round it goes — 9 failed polls in one run,
4 in the next, with launch detection p95 at 71 s against a p50 of 6 s.

The fix is :data:`_MAX_TS_BLOCKS_PER_POLL`: a poll costs at most three HTTP calls however
far behind it is, and when it cannot stamp everything it pulls ``to_block`` back to what
it *can* stamp rather than inventing a timestamp or dropping a log. Progress is slower
but never negative, and the spiral has no foothold.

Money is integer base units (wei, token atoms) end to end; USD is ``Decimal``. No value in
this module passes through ``float``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core.db import get_conn, jdump, upsert
from kaiba.core.events import emit, emit_once
from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, EventKind, Token, looks_evm, normalize_address, now_ms
from kaiba.ingest.pumpportal import LatencyTracker, backoff_delay
from kaiba.providers._http import post_json, redact_text

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# the chain
# --------------------------------------------------------------------------------------

#: Public RPC. Verified: ``eth_chainId`` = 0x1237 (4663), ``web3_clientVersion`` =
#: ``nitro/v3.12.0-rc.2`` (an Arbitrum Orbit chain).
RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
CHAIN = Chain.ROBINHOOD
CHAIN_ID = 4663
SOURCE = "robinhood"
#: Limiter provider key. ``limiter.DEFAULTS`` has no entry for this, so it falls back to
#: ``Limits()`` — 1000 ms minimum interval, i.e. **one HTTP call per second**. That is why
#: :func:`poll_once` puts every query for a poll into a single JSON-RPC batch: one HTTP
#: request is one reservation. See the module's report notes in the research file.
PROVIDER = "robinhood-rpc"

#: Measured 2026-09-20 over both 10,000 and 200,000 block spans: 0.1012 and 0.1015 s.
SECONDS_PER_BLOCK = Decimal("0.1015")

LAUNCHPAD = "pons"
WEI = Decimal(10) ** 18
ZERO_ADDRESS = "0x" + "0" * 40

# --------------------------------------------------------------------------------------
# the Pons contracts, each with the transaction that proved its role
# --------------------------------------------------------------------------------------

#: ``PonsV2LaunchFactory``. **This is the launchpad.**
#:
#: Proof, all reproducible against :data:`RPC_URL`:
#:   * ``0x0f502b2eb03837e58d03308d4309d47081dfdc8f4fc358d644219615b419d78a`` is a
#:     transaction *to* this address carrying exactly ``500000000000000`` wei = 0.0005 ETH,
#:     which is the documented Pons launch fee and equals this contract's own
#:     ``launchFee()`` read back over ``eth_call``. Its selector ``0xa72101af`` is
#:     ``keccak("launchToken((string,string,string,string,(string,string,string,string,
#:     string),address,uint16,bool,bytes32,bytes32),uint256,address,address[])")[:4]``.
#:     The receipt deploys a new ERC-20 and a new curve and emits ``TokenLaunched`` from
#:     this address.
#:   * ``0x28e229d228f3d228c9d45dfd1793edfbf3e0b105bf90d1e27819ff914a999f01`` is a later
#:     launch, same address, same 0.0005 ETH fee, same event.
#:   * ``eth_call`` of ``getLaunchedToken(address)`` on this contract returns a populated
#:     record (``exists=1``) for tokens named in those events, and a zero record for
#:     anything else — so this contract is the registry, not a bystander.
FACTORY_V2 = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"

#: ``PonsV2MemeHook`` — the Uniswap v4 hook that collects fees on graduated pools.
#: Proof: it emits ``PoolRegistered`` and ``HookFeeCollected`` inside the graduation
#: transaction ``0xdfbe43f771383c6b338b40d12de207b9e499a2abe7996ee1c5a6facd510a3c8e``,
#: in the same receipt as this factory's ``PoolGraduated``.
MEME_HOOK = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"

#: Uniswap v4 ``PoolManager``. Emits ``Initialize`` and ``Swap`` in that same graduation
#: receipt. Note that DefiLlama's own Pons adapter labels
#: ``0x58daec3116aae6d93017baaea7749052e8a04fa7`` as the pool manager; the chain says that
#: address is the ``PositionManager`` (it answers ``positionInfo``) and the pool manager is
#: this one (it emits the V4 ``Swap`` topic). Published sources are hints; receipts decide.
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
POSITION_MANAGER = "0x58daec3116aae6d93017baaea7749052e8a04fa7"

#: Launch fee, read from ``launchFee()`` and matched against the value of real launch
#: transactions. Integer wei, never a float.
LAUNCH_FEE_WEI = 500_000_000_000_000

# --------------------------------------------------------------------------------------
# event topics
# --------------------------------------------------------------------------------------
#
# These are keccak256 of the signatures written beside them. There is no keccak in this
# environment's dependency set (no eth-hash, no web3, and hashlib's sha3_256 is NIST SHA-3,
# whose padding differs from Ethereum's Keccak), so they are constants rather than being
# derived at import. Each one was computed with a checked Keccak implementation and then
# confirmed against real logs on chain — the test suite replays those logs, so a wrong
# constant fails a test rather than silently matching nothing.

#: ``TokenLaunched(address indexed token, address indexed curve, address indexed deployer,
#: address pairToken, uint256 launchConfigId, uint256 graduationThreshold)``
TOPIC_TOKEN_LAUNCHED = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"
#: ``PoolGraduated(address indexed token, uint256 positionId, uint256 tokenAmount,
#: uint256 pairTokenAmount)``
TOPIC_POOL_GRADUATED = "0x0a44ef75df69c534f43cd6c1aa3ef8983065fe5fe79ef9e79f6494e6f258c259"
#: ``CurveBuy(address indexed trader, address indexed recipient, uint256 quoteIn,
#: uint256 tokensOut, uint256 fee, uint256 tax)`` — emitted by the token's own curve.
TOPIC_CURVE_BUY = "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
#: ``CurveSell(address indexed trader, address indexed recipient, uint256 tokensIn,
#: uint256 quoteOut, uint256 fee, uint256 tax)``
TOPIC_CURVE_SELL = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"
#: ``HookFeeCollected(bytes32 indexed poolId, address currency, uint256 feeAmount,
#: uint256 taxAmount)`` — post-graduation Uniswap v4 fees, on :data:`MEME_HOOK`.
TOPIC_HOOK_FEE = "0xc532c43b3423e14ef72748f1c8291238829ca0af8ba9b67975ad1483485a4b4d"
#: ``PoolFeesSwept(bytes32 indexed poolId, uint256 protocolAmount, uint256 buybackAmount,
#: uint256 creatorAmount, uint256 tokensLocked)``
TOPIC_POOL_FEES_SWEPT = "0x2f3c43579b9064b6f28edcf41608f3815792d274a56afe024359703cb4ea9b30"

#: ``buy(uint256,uint256,address)`` / ``sell(uint256,uint256,address)`` on a curve. Recorded
#: for the executor's benefit; this module never sends a transaction.
SELECTOR_BUY = "0x59a87bc1"
SELECTOR_SELL = "0xd04c6983"

#: ``getLaunchedToken(address)`` on the factory: the registry lookup that ties a token to
#: its curve when we did not see the launch ourselves.
SELECTOR_GET_LAUNCHED_TOKEN = "0x3cf28b5a"

#: Zero-argument view selectors on a curve, in the order :func:`read_curve` reads them.
#: All ten ride in one JSON-RPC batch, so the last two cost nothing extra and they are the
#: two that decide whether a fast entry is even survivable — see :class:`CurveState`.
CURVE_READS: tuple[tuple[str, str], ...] = (
    ("quote_reserve", "0x9da771f4"),          # quoteReserve()      = phantom + real
    ("real_quote_reserve", "0x4f1f58fd"),     # realQuoteReserve()  = quote actually raised
    ("sellable_tokens", "0x808bcddc"),        # sellableTokens()    = balance - reserved
    ("reserved_tokens", "0x15a55347"),        # reservedTokens()
    ("graduation_threshold", "0x8b0bc501"),   # graduationThreshold()
    ("launch_supply", "0x3f7ed6b7"),          # launchSupply()
    ("fee_bps", "0x24a9d853"),                # feeBps()
    ("launched_at", "0xbf56b371"),            # launchedAt()        = unix seconds
    ("snipe_tax_start_bps", "0x50e25ac2"),    # snipeTaxStartBps()
    ("snipe_tax_seconds", "0x6783774b"),      # snipeTaxSeconds()
)

#: The one anti-sniper configuration ever read off this venue: ``snipeTaxStartBps()`` =
#: 9900 and ``snipeTaxSeconds()`` = 3, MEASURED 2026-09-21 on ``PonsV2LaunchFactory`` and
#: on 75/75 sampled curves (docs/research/REFUTATION-pons-tax-entry.md section 4).
SNIPE_TAX_MEASURED_CONFIG: tuple[int, int] = (9900, 3)

#: MEASURED: the toll in bps by whole elapsed second for that configuration. Two
#: independent methods agree exactly -- ``currentSnipeTaxBps(address)`` read at every block
#: from launch to launch+50 on a live curve, and the toll recovered from 913k real
#: ``CurveBuy`` events joined to exact block timestamps (docs/research/
#: line4-antisniper-tax-pons.md section 1; re-sampled from 1,210 early buys in
#: ``tests/test_pons_tax_entry.py::MEASURED_TOLL_SAMPLE``). The value is constant within
#: each whole second, so this is a step function on ``block.timestamp - launchedAt``, and
#: past the last rung it is zero. Convex, not linear: the linear model this replaced said
#: 6600 at t=1 s, 10.7x the measured 618, and 3300 at t=2 s against a measured 19.
#: ``lanes.PONS_SNIPE_TAX_RUNGS_BPS`` carries the same table for the entry rule;
#: ``tests/test_pons_tax_curve.py`` pins the two to each other so they cannot drift.
SNIPE_TAX_RUNGS_BPS: dict[int, int] = {0: 9900, 1: 618, 2: 19}

#: Streams, for latency bookkeeping and the ``ingest_latency`` table.
STREAM_LAUNCH = "pons_launch"
STREAM_GRADUATION = "pons_graduation"
STREAM_TRADE = "pons_trade"

_LATENCY = LatencyTracker()


def latency_stats() -> dict[str, Any]:
    """Rolling detect-lag per stream: block timestamp -> our receive time."""
    return _LATENCY.stats()


def reset_latency() -> None:
    _LATENCY.reset()


# --------------------------------------------------------------------------------------
# small pure helpers
# --------------------------------------------------------------------------------------


def _hex_int(value: Any) -> int | None:
    """Hex quantity -> int. ``None`` on anything that is not one, never 0 as a fallback."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _words(data: Any) -> list[str]:
    """ABI data payload -> 32-byte words as hex strings, or ``[]``."""
    if not isinstance(data, str) or not data.startswith("0x"):
        return []
    body = data[2:]
    return [body[i:i + 64] for i in range(0, len(body) - len(body) % 64, 64)]


def _word_int(words: Sequence[str], index: int) -> int | None:
    if index >= len(words):
        return None
    try:
        return int(words[index], 16)
    except ValueError:
        return None


def _word_address(word: str | None) -> str | None:
    """Right-most 20 bytes of a word as a lowercase 0x address."""
    if not isinstance(word, str) or len(word) < 40:
        return None
    candidate = "0x" + word[-40:]
    return candidate.lower() if looks_evm(candidate) else None


def _topic_address(topics: Sequence[Any], index: int) -> str | None:
    if index >= len(topics) or not isinstance(topics[index], str):
        return None
    return _word_address(topics[index])


def normalize(address: str) -> str:
    """Lowercase per ``docs/CONTRACT.md`` rule 4. Raises on a non-EVM address."""
    return normalize_address(address, CHAIN)


def block_seconds(blocks: int) -> Decimal:
    return Decimal(blocks) * SECONDS_PER_BLOCK


# --------------------------------------------------------------------------------------
# curve bookkeeping
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CurveMeta:
    """What ``TokenLaunched`` told us about one curve.

    A curve trade log names the curve, not the token, so without this map a ``CurveBuy``
    cannot be attributed. It also carries the quote token: amounts on a curve quoted in a
    6-decimal ERC-20 are not wei, and blending them with ETH curves is the single easiest
    way to produce a volume figure that is wrong by orders of magnitude.
    """

    curve: str
    token: str
    deployer: str
    pair_token: str
    launch_config_id: int
    graduation_threshold: int
    launched_block: int

    @property
    def quote_is_native(self) -> bool:
        return self.pair_token == ZERO_ADDRESS


class CurveIndex:
    """curve address -> :class:`CurveMeta`, learned from ``TokenLaunched``.

    Bounded: a chain that has launched tens of thousands of tokens would otherwise grow
    this without limit in a long-running process. Eviction is oldest-first by insertion,
    which is the right order because curve activity decays sharply after launch.
    """

    def __init__(self, max_entries: int = 50_000) -> None:
        self._by_curve: dict[str, CurveMeta] = {}
        self._by_token: dict[str, CurveMeta] = {}
        self._max = max_entries

    def add(self, meta: CurveMeta) -> None:
        if meta.curve in self._by_curve:
            return
        self._by_curve[meta.curve] = meta
        self._by_token[meta.token] = meta
        while len(self._by_curve) > self._max:
            evicted = self._by_curve.pop(next(iter(self._by_curve)))
            self._by_token.pop(evicted.token, None)

    def get(self, curve: str) -> CurveMeta | None:
        return self._by_curve.get(curve.lower())

    def by_token(self, token: str) -> CurveMeta | None:
        """The reverse lookup the scanner needs: it knows the token, not the curve."""
        return self._by_token.get(token.lower())

    def __len__(self) -> int:
        return len(self._by_curve)

    def __contains__(self, curve: object) -> bool:
        return isinstance(curve, str) and curve.lower() in self._by_curve


# --------------------------------------------------------------------------------------
# pure log parsers
# --------------------------------------------------------------------------------------


def parse_token_launched(entry: Any, *, ts_ms: int | None = None) -> tuple[Token, CurveMeta] | None:
    """A ``TokenLaunched`` log -> (:class:`Token`, :class:`CurveMeta`), or ``None``.

    ``None`` rather than a partial result: a launch whose curve or token we could not read
    is not a launch we can follow, and a half-filled row would be indistinguishable from a
    real one downstream.
    """
    if not isinstance(entry, Mapping):
        return None
    topics = entry.get("topics")
    if not isinstance(topics, Sequence) or len(topics) < 4:
        return None
    if str(topics[0]).lower() != TOPIC_TOKEN_LAUNCHED:
        return None
    token_addr = _topic_address(topics, 1)
    curve = _topic_address(topics, 2)
    deployer = _topic_address(topics, 3)
    if token_addr is None or curve is None or deployer is None:
        return None
    words = _words(entry.get("data"))
    pair_token = _word_address(words[0]) if words else None
    config_id = _word_int(words, 1)
    threshold = _word_int(words, 2)
    if pair_token is None or config_id is None or threshold is None:
        return None
    block = _hex_int(entry.get("blockNumber"))
    tx = entry.get("transactionHash")
    meta = CurveMeta(
        curve=curve,
        token=token_addr,
        deployer=deployer,
        pair_token=pair_token,
        launch_config_id=config_id,
        graduation_threshold=threshold,
        launched_block=block if block is not None else 0,
    )
    token = Token(
        address=token_addr,
        chain=CHAIN,
        creator=deployer,
        created_ms=ts_ms,
        launchpad=LAUNCHPAD,
        pool=curve,
        decimals=None,
        meta={
            "curve": curve,
            "pair_token": pair_token,
            "quote_is_native": pair_token == ZERO_ADDRESS,
            "launch_config_id": config_id,
            "graduation_threshold_wei": str(threshold),
            "factory": FACTORY_V2,
            "block": block,
            "signature": tx if isinstance(tx, str) else None,
            "source": SOURCE,
            "source_ms": ts_ms,
        },
    )
    return token, meta


def parse_pool_graduated(entry: Any, *, ts_ms: int | None = None) -> dict[str, Any] | None:
    """A ``PoolGraduated`` log -> a flat dict shaped like ``pumpportal.parse_migration``."""
    if not isinstance(entry, Mapping):
        return None
    topics = entry.get("topics")
    if not isinstance(topics, Sequence) or len(topics) < 2:
        return None
    if str(topics[0]).lower() != TOPIC_POOL_GRADUATED:
        return None
    token_addr = _topic_address(topics, 1)
    if token_addr is None:
        return None
    words = _words(entry.get("data"))
    tx = entry.get("transactionHash")
    return {
        "mint": token_addr,
        "chain": CHAIN,
        "pool": "uniswap-v4",
        "pool_address": POOL_MANAGER,
        "position_id": _word_int(words, 0),
        "token_amount": _word_int(words, 1),
        "pair_token_amount": _word_int(words, 2),
        "signature": tx if isinstance(tx, str) else None,
        "slot": _hex_int(entry.get("blockNumber")),
        "source_ms": ts_ms,
        "migrated_ms": ts_ms if ts_ms is not None else now_ms(),
    }


def parse_curve_trade(
    entry: Any,
    *,
    meta: CurveMeta,
    ts_ms: int | None = None,
    eth_usd: Decimal | None = None,
) -> dict[str, Any] | None:
    """A ``CurveBuy``/``CurveSell`` log -> a ``swaps``-shaped dict.

    The two events share a layout — ``(trader, recipient, amount0, amount1, fee, tax)`` —
    but the *meaning* of the amounts flips with the side: on a buy ``amount0`` is quote in
    and ``amount1`` is tokens out; on a sell ``amount0`` is tokens in and ``amount1`` is
    quote out. Both were confirmed against the fee, which is 1% (``feeBps``=100) of the
    quote leg in each direction, and against the transaction's ``value`` on a buy.

    ``usd_value`` is filled only when the curve is quoted in native ETH *and* an ETH price
    was supplied. It is otherwise ``None``, never 0 — and that matters more than it looks:
    ``lanes._net_buyers`` treats an unpriced fill as zero conviction, so an invented USD
    figure here would manufacture lane signals out of nothing.
    """
    if not isinstance(entry, Mapping):
        return None
    topics = entry.get("topics")
    if not isinstance(topics, Sequence) or len(topics) < 3:
        return None
    topic = str(topics[0]).lower()
    if topic == TOPIC_CURVE_BUY:
        side = "buy"
    elif topic == TOPIC_CURVE_SELL:
        side = "sell"
    else:
        return None
    trader = _topic_address(topics, 1)
    if trader is None:
        return None
    words = _words(entry.get("data"))
    amount0 = _word_int(words, 0)
    amount1 = _word_int(words, 1)
    fee = _word_int(words, 2)
    tax = _word_int(words, 3)
    if amount0 is None or amount1 is None:
        return None
    quote_amount, token_amount = (amount0, amount1) if side == "buy" else (amount1, amount0)
    tx = entry.get("transactionHash")
    if not isinstance(tx, str):
        return None

    usd_value: Decimal | None = None
    price_usd: Decimal | None = None
    if eth_usd is not None and meta.quote_is_native:
        usd_value = (Decimal(quote_amount) / WEI) * eth_usd
        if token_amount > 0:
            price_usd = usd_value / (Decimal(token_amount) / WEI)

    return {
        "chain": CHAIN.value,
        "tx": tx,
        "slot": _hex_int(entry.get("blockNumber")),
        "block_index": _hex_int(entry.get("logIndex")),
        "ts_ms": ts_ms if ts_ms is not None else now_ms(),
        "wallet": trader,
        "token": meta.token,
        "side": side,
        "amount_token": str(token_amount),
        "amount_native": str(quote_amount),
        "price_usd": str(price_usd) if price_usd is not None else None,
        "usd_value": str(usd_value) if usd_value is not None else None,
        "program": "pons-curve",
        "source": SOURCE,
        "curve": meta.curve,
        "quote_token": meta.pair_token,
        "fee": fee,
        "tax": tax,
        "recipient": _topic_address(topics, 2),
        "source_ms": ts_ms,
    }


# --------------------------------------------------------------------------------------
# persistence — deliberately the same shapes pumpportal.py writes
# --------------------------------------------------------------------------------------


def record_new_token(token: Token, conn: Any = None) -> bool:
    """Upsert the token and emit ``TOKEN_CREATED`` once. True if the event was new.

    Mirrors ``pumpportal.record_new_token`` field for field so tier-0 triage and tier-1
    scan need no branch for this chain: same table, same conflict target, same refusal to
    overwrite ``first_seen_ms``.
    """
    c = conn or get_conn()
    ts = now_ms()
    upsert(
        c,
        "tokens",
        {
            "chain": token.chain.value,
            "address": token.address,
            "symbol": token.symbol,
            "name": token.name,
            "decimals": token.decimals,
            "creator": token.creator,
            "created_ms": token.created_ms,
            "launchpad": token.launchpad,
            "pool": token.pool,
            "first_seen_ms": ts,
            "meta_json": jdump(token.meta),
        },
        conflict=["chain", "address"],
        update=["symbol", "name", "decimals", "creator", "created_ms", "launchpad",
                "pool", "meta_json"],
    )
    event_id = emit_once(
        EventKind.TOKEN_CREATED,
        {
            "mint": token.address,
            "symbol": token.symbol,
            "name": token.name,
            "creator": token.creator,
            "launchpad": token.launchpad,
            "created_ms": token.created_ms,
            "curve": token.meta.get("curve"),
            "pair_token": token.meta.get("pair_token"),
            "graduation_threshold_wei": token.meta.get("graduation_threshold_wei"),
            "block": token.meta.get("block"),
            "signature": token.meta.get("signature"),
            "source": SOURCE,
        },
        chain=CHAIN,
        subject=token.address,
        dedupe_key=f"{EventKind.TOKEN_CREATED.value}:{CHAIN.value}:{token.address}",
        conn=c,
    )
    return event_id is not None


def record_migration(info: Mapping[str, Any], conn: Any = None) -> bool:
    """Stamp ``tokens.migrated_ms`` and emit ``TOKEN_MIGRATED`` once per token."""
    c = conn or get_conn()
    mint = info["mint"]
    upsert(
        c,
        "tokens",
        {
            "chain": CHAIN.value,
            "address": mint,
            "migrated_ms": info["migrated_ms"],
            "launchpad": LAUNCHPAD,
            "first_seen_ms": now_ms(),
        },
        conflict=["chain", "address"],
        update=["migrated_ms"],  # never clobber what we learned at creation
    )
    event_id = emit_once(
        EventKind.TOKEN_MIGRATED,
        {
            "mint": mint,
            "pool": info.get("pool"),
            "pool_address": info.get("pool_address"),
            "position_id": info.get("position_id"),
            "token_amount": str(info["token_amount"]) if info.get("token_amount") is not None else None,
            "pair_token_amount": (
                str(info["pair_token_amount"]) if info.get("pair_token_amount") is not None else None
            ),
            "signature": info.get("signature"),
            "slot": info.get("slot"),
            "migrated_ms": info["migrated_ms"],
            "source": SOURCE,
        },
        chain=CHAIN,
        subject=mint,
        dedupe_key=f"{EventKind.TOKEN_MIGRATED.value}:{CHAIN.value}:{mint}",
        conn=c,
    )
    return event_id is not None


def record_trade(row: Mapping[str, Any], conn: Any = None) -> bool:
    """Insert one ``swaps`` row and emit ``WALLET_TRADE``. False if we already had it."""
    c = conn or get_conn()
    cur = c.execute(
        "INSERT OR IGNORE INTO swaps "
        "(chain, tx, slot, block_index, ts_ms, wallet, token, side, amount_token, "
        " amount_native, price_usd, usd_value, program, source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row["chain"], row["tx"], row["slot"], row.get("block_index"), row["ts_ms"],
            row["wallet"], row["token"], row["side"], row["amount_token"],
            row["amount_native"], row["price_usd"], row["usd_value"], row["program"],
            row["source"],
        ),
    )
    if not cur.rowcount:
        return False
    emit_once(
        EventKind.WALLET_TRADE,
        {k: v for k, v in row.items() if k not in {"source_ms"}},
        chain=CHAIN,
        subject=row["wallet"],
        dedupe_key=(
            f"{EventKind.WALLET_TRADE.value}:{SOURCE}:{row['tx']}:"
            f"{row.get('block_index')}:{row['wallet']}:{row['side']}"
        ),
        conn=c,
    )
    return True


def _screen(token: Token, conn: Any) -> None:
    """Hand a launch to tier-0 triage, exactly as ``pumpportal._screen`` does.

    Imported lazily because ``kaiba.execution`` imports back into ``kaiba.ingest``, and
    swallowed because a screening failure must never cost us the ingest row: knowing a
    token exists is worth more than knowing what we thought of it.

    A dict is passed rather than the ``Token`` because ``triage.parse_launch`` reads the
    chain out of the payload, and it must see ``robinhood`` — on ``Chain.SOL`` it would
    apply ``looks_solana`` to a 0x address and drop the mint on the floor.
    """
    try:
        from kaiba.execution.triage import screen_launch

        screen_launch(
            {
                "chain": CHAIN.value,
                "mint": token.address,
                "creator": token.creator,
                "name": token.name,
                "symbol": token.symbol,
                "launchpad": token.launchpad,
                "pool": token.pool,
                "timestamp": token.created_ms,
                "signature": token.meta.get("signature"),
                "slot": token.meta.get("block"),
                "source": SOURCE,
            },
            conn=conn,
        )
    except Exception as exc:  # noqa: BLE001 - triage is advisory, ingest is not
        log.debug("triage skipped a Pons launch (%s: %s)", type(exc).__name__, exc)


# --------------------------------------------------------------------------------------
# JSON-RPC
# --------------------------------------------------------------------------------------


@dataclass
class RpcResult:
    """One batched JSON-RPC round trip."""

    results: list[Any]
    ok: bool
    note: str | None = None
    elapsed_ms: int = 0


def rpc_batch(
    calls: Sequence[tuple[str, list[Any]]],
    *,
    endpoint: str = "chain.batch",
    priority: Priority = Priority.DISCOVERY,
    wait_for_slot_s: float = 5.0,
    timeout_s: float = 20.0,
    conn: Any = None,
) -> RpcResult:
    """Send several JSON-RPC calls as one HTTP request.

    Batching is not a micro-optimisation here, it is what makes the listener affordable:
    the limiter has no ``robinhood-rpc`` entry so it falls back to one call per second, and
    the public endpoint answers a batch of 50 happily while rejecting 100 with a JSON-RPC
    429. One poll is therefore one reservation and one round trip.

    Never raises. A failure returns ``ok=False`` and an empty result list, per the contract:
    a dead provider is data, not an exception.
    """
    if not calls:
        return RpcResult([], True)
    body = [
        {"jsonrpc": "2.0", "id": i + 1, "method": method, "params": params}
        for i, (method, params) in enumerate(calls)
    ]
    started = time.perf_counter()
    got = post_json(
        PROVIDER,
        endpoint,
        RPC_URL,
        json_body=body,
        priority=priority,
        wait_for_slot_s=wait_for_slot_s,
        timeout_s=timeout_s,
        ttl_s=0.0,
        conn=conn,
    )
    elapsed = int((time.perf_counter() - started) * 1000)
    if not got.ok or not isinstance(got.data, list):
        note = got.receipt.note or "no response"
        return RpcResult([], False, redact_text(str(note))[:200], elapsed)
    by_id: dict[int, Any] = {}
    for item in got.data:
        if isinstance(item, Mapping) and isinstance(item.get("id"), int):
            by_id[item["id"]] = item
    out: list[Any] = []
    errors: list[str] = []
    for i in range(len(calls)):
        item = by_id.get(i + 1)
        if not isinstance(item, Mapping):
            out.append(None)
            errors.append(f"{calls[i][0]}: missing")
            continue
        if item.get("error") is not None:
            out.append(None)
            errors.append(f"{calls[i][0]}: {str(item['error'])[:90]}")
            continue
        out.append(item.get("result"))
    return RpcResult(out, not errors, "; ".join(errors)[:200] or None, elapsed)


def get_logs_call(
    *, from_block: int, to_block: int, address: str | None = None,
    topics: list[Any] | None = None,
) -> tuple[str, list[Any]]:
    params: dict[str, Any] = {"fromBlock": hex(from_block), "toBlock": hex(to_block)}
    if address is not None:
        params["address"] = address
    if topics is not None:
        params["topics"] = topics
    return ("eth_getLogs", [params])


# --------------------------------------------------------------------------------------
# curve reading — the same dict keys scanner.curve_from_payload emits
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CurveState:
    """Raw integers read off one curve. Every field is a base unit, never a float.

    ``snipe_tax_start_bps`` and ``snipe_tax_seconds`` are here because they are the two
    numbers that decide whether a fast entry is survivable, and the observed values are
    brutal in the launch second and nearly nothing after it: **9900 / 618 / 19 / 0 bps at
    0 / 1 / 2 / 3+ whole seconds** (MEASURED, :data:`SNIPE_TAX_RUNGS_BPS`). A bot that
    buys in the launch second pays a 99% tax on the way in; one second later the wall is
    6.18%, two seconds later 0.19%. Any design that assumes speed is the edge on this
    venue needs to read :meth:`snipe_tax_bps_at` before it is written -- and any design
    that assumes the wall lasts three seconds needs to read it too.
    """

    curve: str
    token: str
    quote_reserve: int
    real_quote_reserve: int
    sellable_tokens: int
    reserved_tokens: int
    graduation_threshold: int
    launch_supply: int
    fee_bps: int
    launched_at_s: int
    quote_is_native: bool
    observed_ms: int
    snipe_tax_start_bps: int = 0
    snipe_tax_seconds: int = 0

    @property
    def phantom_quote(self) -> int:
        """The virtual reserve the curve starts with. Pons' analogue of virtual SOL."""
        return self.quote_reserve - self.real_quote_reserve

    @property
    def token_reserve(self) -> int:
        """Tokens still held by the curve, ``k / quoteReserve``."""
        return self.sellable_tokens + self.reserved_tokens

    @property
    def graduated(self) -> bool:
        return self.real_quote_reserve >= self.graduation_threshold > 0

    @property
    def snipe_tax_shape_measured(self) -> bool:
        """True when this curve carries the one configuration whose decay was measured."""
        return (self.snipe_tax_start_bps, self.snipe_tax_seconds) == SNIPE_TAX_MEASURED_CONFIG

    def snipe_tax_bps_at(self, at_s: int) -> int:
        """Anti-sniper tax in bps at a unix second, on the MEASURED schedule.

        The contract prices the toll off ``block.timestamp - launchedAt`` in whole seconds,
        so the schedule is a step function on integer elapsed seconds: for the
        :data:`SNIPE_TAX_MEASURED_CONFIG` every curve carries it is 9900 / 618 / 19 / 0 bps
        at 0 / 1 / 2 / 3+ s (:data:`SNIPE_TAX_RUNGS_BPS`). There is nothing to
        interpolate *between* the rungs: every block inside second ``k`` read rung ``k``,
        so a fractional instant floors to its whole second rather than sliding toward the
        next rung. A linear blend at t=1.5 s would say 318 where every block said 618, and
        the linear decay this replaced (9900 / 6600 / 3300) was 10.7x high at t=1 s.

        Anything beyond those four points is INVENTED and fails closed. Any other positive
        configuration has an unmeasured shape, so inside its window this returns the full
        starting toll -- an unknown decay must never price as a cheap one -- and zero once
        the window has passed; those two endpoints the contract does expose. A zero
        configuration is no tax. :meth:`snipe_tax_basis` says which case a reading is.
        """
        if self.snipe_tax_start_bps <= 0 or self.snipe_tax_seconds <= 0:
            return 0
        elapsed = math.floor(at_s) - self.launched_at_s
        if elapsed < 0:
            return self.snipe_tax_start_bps
        if elapsed >= self.snipe_tax_seconds:
            return 0
        if self.snipe_tax_shape_measured:
            return SNIPE_TAX_RUNGS_BPS[elapsed]
        return self.snipe_tax_start_bps

    def snipe_tax_basis(self, at_s: int) -> str:
        """Where :meth:`snipe_tax_bps_at` got its number, for the record that carries it."""
        if self.snipe_tax_start_bps <= 0 or self.snipe_tax_seconds <= 0:
            return "no_tax_configured"
        elapsed = math.floor(at_s) - self.launched_at_s
        if elapsed < 0:
            return "pre_launch_full_toll"
        if elapsed >= self.snipe_tax_seconds:
            return "window_elapsed"
        if self.snipe_tax_shape_measured:
            return "MEASURED_rungs"
        return "INVENTED_unmeasured_config_fail_closed_full_toll"


def parse_curve_state(
    values: Sequence[Any], *, curve: str, token: str, quote_is_native: bool,
    observed_ms: int | None = None,
) -> CurveState | None:
    """The eight ``eth_call`` results in :data:`CURVE_READS` order -> :class:`CurveState`.

    Returns ``None`` if any read failed. There is no partial curve: a state with the
    reserves present and the threshold missing would let a caller compute a progress figure
    against nothing.
    """
    if len(values) < len(CURVE_READS):
        return None
    ints: list[int] = []
    for raw in values[:len(CURVE_READS)]:
        parsed = _hex_int(raw) if isinstance(raw, str) and raw not in ("0x", "") else None
        if parsed is None:
            return None
        ints.append(parsed)
    (quote_reserve, real_quote, sellable, reserved, threshold, supply, fee_bps,
     launched_at, snipe_bps, snipe_s) = ints
    if quote_reserve <= 0 or supply <= 0 or threshold <= 0:
        return None
    if real_quote < 0 or real_quote > quote_reserve:
        return None
    return CurveState(
        curve=curve.lower(),
        token=token.lower(),
        quote_reserve=quote_reserve,
        real_quote_reserve=real_quote,
        sellable_tokens=sellable,
        reserved_tokens=reserved,
        graduation_threshold=threshold,
        launch_supply=supply,
        fee_bps=fee_bps,
        launched_at_s=launched_at,
        quote_is_native=quote_is_native,
        observed_ms=observed_ms if observed_ms is not None else now_ms(),
        snipe_tax_start_bps=snipe_bps,
        snipe_tax_seconds=snipe_s,
    )


def curve_from_state(
    state: CurveState,
    *,
    swaps: int | None = None,
    swaps_basis: str | None = None,
    last_trade_ms: int | None = None,
    eth_usd: Decimal | None = None,
    at_ms: int | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """:class:`CurveState` -> the dict ``scanner.curve_from_payload`` emits, plus a basis.

    The keys are deliberately the Solana ones. ``sol_in_curve`` on this chain is **ETH in
    the curve** and ``sol_in_curve_lamports`` is **wei**; the names are wrong for the venue
    and right for the consumer, which is the trade worth making while
    ``kaiba/execution/scanner.py`` and the lanes are not mine to rename. Every Pons-native
    figure is also present under its own name, so nothing has to be un-guessed later.

    Note what is *not* claimed: ``curve_velocity`` refuses any chain that is not Solana
    (``lanes.py`` line 595), so this dict cannot fire that lane today. It is built to the
    same shape so that the day the lane's chain gate opens, the input is already correct
    and already measured.

    The mechanics, verified on live curves rather than assumed:

    * ``quoteReserve = phantomQuote + realQuoteReserve``
    * ``quoteReserve * curveTokenBalance = phantomQuote * launchSupply`` — a constant
      product whose token leg is the curve's **whole** balance, not ``sellableTokens()``.
      Checked to a relative error below 1e-9 on live curves.
    * ``sellableTokens() = curveTokenBalance - reservedTokens``
    * ``reservedTokens / launchSupply = phantomQuote / (phantomQuote + graduationThreshold)``
      exactly — so ``sellableTokens`` reaches zero precisely when the raise reaches the
      threshold, and the reserved block is what seeds the Uniswap v4 pool.
    * Graduation fires when ``realQuoteReserve >= graduationThreshold``.
    """
    now = at_ms if at_ms is not None else now_ms()
    if state.graduated:
        return None, "curve_complete"
    if state.graduation_threshold <= 0:
        return None, "graduation_threshold_unreadable"

    try:
        quote_raised = Decimal(state.real_quote_reserve) / WEI
        graduation_quote = Decimal(state.graduation_threshold) / WEI
        phantom = Decimal(state.phantom_quote) / WEI
        progress = Decimal(state.real_quote_reserve) / Decimal(state.graduation_threshold) * 100
    except (ArithmeticError, InvalidOperation):
        return None, "curve_geometry_unreadable"
    if progress < 0 or progress > 100:
        return None, "derived_progress_out_of_range"

    created_ms = state.launched_at_s * 1000 if state.launched_at_s else None
    age_s = (now - created_ms) / 1000.0 if created_ms else None
    quote_per_min: Decimal | None = None
    if age_s is not None and age_s >= 30:
        quote_per_min = quote_raised / (Decimal(str(age_s)) / Decimal(60))

    market_cap_usd: Decimal | None = None
    if eth_usd is not None and state.quote_is_native and state.token_reserve > 0:
        # Spot price on a constant-product curve is reserve ratio; FDV is that times supply.
        price_quote_per_atom = Decimal(state.quote_reserve) / Decimal(state.token_reserve)
        market_cap_usd = price_quote_per_atom * Decimal(state.launch_supply) / WEI * eth_usd

    curve: dict[str, Any] = {
        # --- the shared keys, in scanner.curve_from_payload's vocabulary
        "progress_pct": progress,
        "progress_basis": "real_quote_reserve_vs_graduation_threshold",
        "sol_in_curve": quote_raised,
        "sol_in_curve_lamports": state.real_quote_reserve,
        "sol_per_min": quote_per_min,
        "swaps": swaps,
        "swaps_basis": swaps_basis,
        "graduation_sol": graduation_quote,
        "sol_raised_pct_of_graduation": progress,
        "virtual_sol_initial": phantom,
        "real_token_initial": state.launch_supply - state.reserved_tokens,
        # There is no "classic" Pons curve to compare against: the launch config is chosen
        # per launch and the observed configs already differ by three orders of magnitude
        # in the quote leg. False, not None, because the key is a flag the scanner prints.
        "classic_curve": False,
        "virtual_sol_reserves": state.quote_reserve,
        "virtual_token_reserves": state.token_reserve,
        "real_token_reserves": state.sellable_tokens,
        "created_ms": created_ms,
        "age_s": round(age_s, 1) if age_s is not None else None,
        "last_trade_ms": last_trade_ms,
        "market_cap_usd": market_cap_usd,
        "source": LAUNCHPAD,
        "observed_ms": state.observed_ms,
        # --- Pons' own vocabulary, so nothing above has to be reverse-engineered
        "venue": "pons-v2",
        "chain": CHAIN.value,
        "curve_address": state.curve,
        "token": state.token,
        "quote_is_native": state.quote_is_native,
        "quote_raised_wei": state.real_quote_reserve,
        "quote_reserve_wei": state.quote_reserve,
        "phantom_quote_wei": state.phantom_quote,
        "graduation_threshold_wei": state.graduation_threshold,
        "reserved_tokens": state.reserved_tokens,
        "launch_supply": state.launch_supply,
        "fee_bps": state.fee_bps,
        "launched_at_s": state.launched_at_s,
        "quote_per_min": quote_per_min,
        "snipe_tax_start_bps": state.snipe_tax_start_bps,
        "snipe_tax_seconds": state.snipe_tax_seconds,
        "snipe_tax_bps_now": state.snipe_tax_bps_at(now // 1000),
        "snipe_tax_basis": state.snipe_tax_basis(now // 1000),
    }
    if swaps is None:
        curve.pop("swaps")  # absent, not zero: the lane must not divide by it
    basis = "quote_per_swap" if swaps else ("quote_per_min" if quote_per_min is not None else "none")
    return curve, basis


def read_curve(
    curve: str,
    token: str,
    *,
    quote_is_native: bool = True,
    conn: Any = None,
    priority: Priority = Priority.RESEARCH,
) -> CurveState | None:
    """Read one curve's state over ``eth_call``. One HTTP round trip for all eight reads."""
    try:
        curve_n, token_n = normalize(curve), normalize(token)
    except ValueError:
        return None
    got = rpc_batch(
        [("eth_call", [{"to": curve_n, "data": selector}, "latest"]) for _, selector in CURVE_READS],
        endpoint="chain.curve",
        priority=priority,
        conn=conn,
    )
    if not got.results:
        return None
    return parse_curve_state(
        got.results, curve=curve_n, token=token_n, quote_is_native=quote_is_native
    )


def curve_for_token(
    token: str, *, conn: Any = None, index: CurveIndex | None = None
) -> tuple[dict[str, Any] | None, str]:
    """Convenience for the scanner: token address -> curve dict, resolving the curve first.

    Falls back to the factory's ``getLaunchedToken`` when the in-memory index has not seen
    the launch, which is the normal case for a token that launched before this process did.
    """
    try:
        token_n = normalize(token)
    except ValueError:
        return None, "not_an_evm_address"
    meta = index.by_token(token_n) if index is not None else None
    curve = meta.curve if meta else None
    pair_token = meta.pair_token if meta else None
    if curve is None:
        # getLaunchedToken(address) -> (token, curve, deployer, creatorFeeRecipient,
        #                               pairToken, graduationThreshold, ...)
        got = rpc_batch(
            [("eth_call", [{"to": FACTORY_V2,
                            "data": SELECTOR_GET_LAUNCHED_TOKEN + token_n[2:].rjust(64, "0")},
                           "latest"])],
            endpoint="chain.launched",
            conn=conn,
        )
        words = _words(got.results[0]) if got.results and isinstance(got.results[0], str) else []
        curve = _word_address(words[1]) if len(words) > 1 else None
        pair_token = _word_address(words[4]) if len(words) > 4 else None
        if curve is None or curve == ZERO_ADDRESS:
            return None, "curve_unknown"
    state = read_curve(curve, token_n, quote_is_native=(pair_token or ZERO_ADDRESS) == ZERO_ADDRESS,
                       conn=conn)
    if state is None:
        return None, "curve_unreadable"
    return curve_from_state(state)


# --------------------------------------------------------------------------------------
# ETH price, for the one field the lane cannot work without
# --------------------------------------------------------------------------------------

#: Canonical WETH on Ethereum mainnet. Robinhood Chain's native token is ETH
#: (``NATIVE_SYMBOL[Chain.ROBINHOOD]``), and ETH's price is not chain-specific, so the deep
#: mainnet pools are a better source than anything quoted on a one-year-old L2.
WETH_MAINNET = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"


@dataclass
class EthPrice:
    """A cached ETH/USD quote. ``None`` when unknown — never a stand-in number.

    This exists because of one line in ``lanes._net_buyers``: a fill whose USD value we
    could not establish contributes 0 and therefore fails ``min_buy_usd``. So an unpriced
    feed cannot fire ``pons-robinhood`` at all, and a *wrongly* priced one fires it on
    fiction. Cached for a minute, refreshed lazily, and it fails to ``None``.
    """

    ttl_s: float = 60.0
    _value: Decimal | None = field(default=None, init=False)
    _fetched_ms: int = field(default=0, init=False)

    def get(self, *, conn: Any = None) -> Decimal | None:
        if self._value is not None and (now_ms() - self._fetched_ms) < self.ttl_s * 1000:
            return self._value
        try:
            from kaiba.providers import prices

            measure = prices.price_usd(Chain.ETH, WETH_MAINNET, conn=conn)
            value = measure.value if measure.known else None
        except Exception as exc:  # noqa: BLE001 - pricing must never break ingestion
            log.debug("eth price lookup failed (%s: %s)", type(exc).__name__, exc)
            value = None
        if isinstance(value, Decimal) and value > 0:
            self._value = value
            self._fetched_ms = now_ms()
        return self._value


# --------------------------------------------------------------------------------------
# the poll
# --------------------------------------------------------------------------------------


@dataclass
class PollResult:
    """What one poll did. Returned so the loop and the tests can both assert on it."""

    head: int
    from_block: int
    to_block: int
    launches: int = 0
    graduations: int = 0
    trades: int = 0
    skipped_unknown_curve: int = 0
    #: Trades on curves quoted in something other than native ETH. These ARE recorded —
    #: the amounts are exact in their own token — but they carry no ``usd_value``, so a
    #: lane gating on USD will never see them. Counted separately so that silence is
    #: attributable rather than mysterious.
    non_native_quote: int = 0
    logs_seen: int = 0
    rpc_calls: int = 0
    lag_blocks: int = 0
    lag_ms: int | None = None
    elapsed_ms: int = 0
    ok: bool = True
    note: str | None = None

    @property
    def events(self) -> int:
        return self.launches + self.graduations + self.trades


@dataclass
class ListenerConfig:
    """Knobs, each with the measurement behind it."""

    #: 3 s is ~30 blocks at the measured 0.1015 s/block.
    #:
    #: The interval is set by the limiter, not the chain. ``robinhood-rpc`` has no
    #: ``limiter.DEFAULTS`` entry, so it refills at **1.0 call/s** and answers a crossing
    #: with a 60-second cooldown on the whole ``chain.*`` family. A poll costs 2 HTTP
    #: calls normally and 3 at its hard cap, so 3 s gives 0.67/s steady and 1.0/s worst
    #: case. At 2 s the steady state is exactly 1.0/s — no headroom — and two live runs
    #: duly fell into the cooldown spiral.
    #:
    #: Polling faster buys nothing anyway: graduation is atomic with its triggering buy,
    #: and the first three seconds of a launch carry a 99% tax. There is no race here.
    poll_interval_s: float = 3.0
    #: eth_getLogs answers a 5,000-block span in well under a second, but a poll that has
    #: fallen far behind should catch up in bounded steps rather than one giant query.
    max_span_blocks: int = 2_000
    #: On a cold start, how far back to learn curve->token mappings. 10,000 blocks is
    #: ~17 minutes, which covers the tokens still being actively traded.
    #:
    #: It is not larger because the warm-up runs in 5,000-block chunks and each chunk is
    #: an HTTP call against a bucket of 10 that refills at 1/s. A 36,000-block warm-up
    #: spent 8 calls back to back, started the first poll with an almost-empty bucket, and
    #: took ~2 minutes of a live run to complete before a single event was ingested.
    warm_blocks: int = 10_000
    #: Price the ETH leg so lanes that gate on USD can run at all.
    price_trades: bool = True
    budget_interval_s: float = 300.0


DEFAULT_CONFIG = ListenerConfig()


#: Block number -> unix ms, so an overlapping poll (the head race in :func:`poll_once` is
#: resolved in favour of re-reading) does not pay for the same block twice. Bounded,
#: insertion-ordered, and never read for correctness — a miss just costs a fetch.
_BLOCK_TS: dict[int, int] = {}
_BLOCK_TS_MAX = 4_000

#: Safety valve on the timestamp fallback: at most this many unstamped blocks per poll.
#:
#: **Set high on purpose.** The intuition that a tighter cap is safer is wrong here, and
#: four 11-minute live runs say so. The cooldowns come from real HTTP 429s on the public
#: endpoint (``provider_family_bans.reason`` = ``RuntimeError: 429 rate limited``), which
#: is a quota measured over minutes, not a burst limit. A tight cap does not reduce the
#: quota spent — it makes the listener crawl, so it stays behind and keeps spending:
#:
#: ===========  ==========  =============  ==================
#: Poll every   Cap         Failed polls   Launch detect p50
#: ===========  ==========  =============  ==================
#: 2 s          uncapped    9 / 117        6.1 s
#: 3 s          uncapped    **4 / 120**    **5.5 s**
#: 3 s          40 (1x)     12 / 105       6.6 s
#: 3 s          80 (2x)     24 / 82        30.8 s
#: ===========  ==========  =============  ==================
#:
#: So the cap exists only to stop a pathological backlog (a multi-hour outage) issuing a
#: hundred batches in one poll. It is not the throttle; the poll interval is.
_MAX_TS_BLOCKS_PER_POLL = 400


def log_timestamp_ms(entry: Mapping[str, Any]) -> int | None:
    """Unix ms straight off the log entry, or ``None``.

    Nitro puts ``blockTimestamp`` on ``eth_getLogs`` entries, which saves the obvious
    second call — collect the distinct block numbers, batch ``eth_getBlockByNumber`` —
    whenever it is populated.

    **It is populated only within a few blocks of the head**, which is the opposite of
    what you would guess and was established by measurement: a query over
    ``head-30..latest`` came back with 6 of 8 entries stamped, while ``head-100..head-50``
    and every older window returned ``0x0`` for all of them. The field appears to be
    served from a recent-block cache. A live poller reads near the head, so it wins most
    of the time and must not depend on it.

    ``0x0`` is treated as absent rather than as the Unix epoch. That distinction is the
    whole point: a zero here would become ``ts_ms = 0`` on a swap row, and every lane
    window would then read the trade as 56 years old.
    """
    ts = _hex_int(entry.get("blockTimestamp"))
    if ts is None or ts <= 0:
        return None
    return ts * 1000


def _block_timestamps(
    blocks: Iterable[int], *, conn: Any = None
) -> tuple[dict[int, int], int]:
    """Block number -> unix ms, in batched round trips. Returns (map, http_calls).

    Only used for logs that arrived without a ``blockTimestamp`` (see
    :func:`log_timestamp_ms`). Interpolating from the head block at 101.5 ms each would
    put an invented number into ``ts_ms``, which every window calculation in the lanes
    then treats as measured, so we pay for a real one.
    """
    wanted = sorted({b for b in blocks if isinstance(b, int) and b >= 0})
    if not wanted:
        return {}, 0
    out: dict[int, int] = {}
    for block in list(wanted):
        cached = _BLOCK_TS.get(block)
        if cached is not None:
            out[block] = cached
            wanted.remove(block)
    if not wanted:
        return out, 0
    calls = 0
    for start in range(0, len(wanted), 40):
        chunk = wanted[start:start + 40]
        got = rpc_batch(
            [("eth_getBlockByNumber", [hex(b), False]) for b in chunk],
            endpoint="chain.blocks",
            conn=conn,
        )
        calls += 1
        for block, result in zip(chunk, got.results, strict=False):
            if isinstance(result, Mapping):
                ts = _hex_int(result.get("timestamp"))
                if ts is not None:
                    out[block] = ts * 1000
                    _BLOCK_TS[block] = ts * 1000
    while len(_BLOCK_TS) > _BLOCK_TS_MAX:
        _BLOCK_TS.pop(next(iter(_BLOCK_TS)))
    return out, calls


def poll_once(
    *,
    from_block: int,
    index: CurveIndex,
    conn: Any = None,
    config: ListenerConfig = DEFAULT_CONFIG,
    eth_price: EthPrice | None = None,
    head: int | None = None,
) -> PollResult:
    """One pass: read the head, fetch the window's logs, write everything they imply.

    All three queries ride in one JSON-RPC batch and the timestamps come off the log
    entries (:func:`log_timestamp_ms`), so **a poll is one HTTP call regardless of how
    wide its window is** — which is what keeps it inside a limiter that allows one call
    per second and cools down a whole family for a minute when crossed.

    Never raises. Every failure path returns a :class:`PollResult` with ``ok=False``, which
    the loop turns into a retry rather than a crash.
    """
    started = time.perf_counter()

    # Everything for this poll goes in one JSON-RPC batch, which is one HTTP request and
    # so one limiter reservation.
    #
    # ``eth_blockNumber`` is deliberately the FIRST item and the cursor advances to
    # ``head + 1``. Nitro answers a batch in order, so the ``eth_getLogs`` that follow can
    # only ever see a block at or beyond that head. That makes the one possible race
    # re-reading a block we already handled — harmless, because every write here is
    # idempotent (``emit_once``, ``INSERT OR IGNORE``, ``upsert``). Putting the head last
    # would invert the race into *skipping* blocks, which is silent data loss.
    bounded = head is not None and (head - from_block) >= config.max_span_blocks
    if bounded:
        to_block_arg: Any = hex(from_block + config.max_span_blocks - 1)
    else:
        to_block_arg = "latest"

    def _logs(address: str | None, topics: list[Any]) -> tuple[str, list[Any]]:
        params: dict[str, Any] = {"fromBlock": hex(from_block), "toBlock": to_block_arg,
                                  "topics": topics}
        if address is not None:
            params["address"] = address
        return ("eth_getLogs", [params])

    got = rpc_batch(
        [
            ("eth_blockNumber", []),
            _logs(FACTORY_V2, [[TOPIC_TOKEN_LAUNCHED, TOPIC_POOL_GRADUATED]]),
            _logs(None, [[TOPIC_CURVE_BUY, TOPIC_CURVE_SELL]]),
        ],
        endpoint="chain.logs",
        conn=conn,
    )
    rpc_calls = 1
    if not got.results or len(got.results) < 3 or not isinstance(got.results[0], str):
        return PollResult(head or 0, from_block, from_block - 1, ok=False,
                          note=got.note or "no log response", rpc_calls=rpc_calls,
                          elapsed_ms=int((time.perf_counter() - started) * 1000))
    head = _hex_int(got.results[0]) or (head or 0)
    to_block = (from_block + config.max_span_blocks - 1) if bounded else head
    if to_block < from_block:
        return PollResult(head, from_block, from_block - 1, rpc_calls=rpc_calls,
                          elapsed_ms=int((time.perf_counter() - started) * 1000))
    factory_logs = got.results[1] if isinstance(got.results[1], list) else []
    curve_logs = got.results[2] if isinstance(got.results[2], list) else []

    result = PollResult(head, from_block, to_block, rpc_calls=rpc_calls,
                        logs_seen=len(factory_logs) + len(curve_logs))
    if got.note:
        result.ok = False
        result.note = got.note
        return result

    # Timestamps come off the log entries themselves; only blocks whose logs omitted the
    # field cost a round trip. On this RPC that set is normally empty.
    stamps: dict[int, int] = {}
    missing: set[int] = set()
    for entry in list(factory_logs) + list(curve_logs):
        if not isinstance(entry, Mapping):
            continue
        block = _hex_int(entry.get("blockNumber"))
        if block is None or block in stamps:
            continue
        stamped = log_timestamp_ms(entry)
        if stamped is not None:
            stamps[block] = stamped
        else:
            missing.add(block)
    # Bound the fallback. Its cost scales with distinct unstamped blocks, and an unbounded
    # version is what produced the cooldown spiral: a 60-second backlog needs ~15 extra
    # batches, the bucket empties, the limiter cools `chain.*` for another 60 seconds, and
    # the next backlog is bigger. Capping the fetch caps the poll at two HTTP calls
    # whatever happens. Blocks above the cap are not stamped, not invented, and not
    # dropped: `to_block` is pulled back to the last block we can honestly stamp, so they
    # are simply read again next poll.
    if missing:
        # Lowest blocks first, so whatever we can afford is the *oldest* part of the
        # window and the cursor can always move forward by at least that much.
        affordable = sorted(missing)[:_MAX_TS_BLOCKS_PER_POLL]
        fetched, ts_calls = _block_timestamps(affordable, conn=conn)
        stamps.update(fetched)
        result.rpc_calls += ts_calls

    # Advance only over a *contiguous* stamped prefix. Taking ``max(stamps)`` instead
    # would be defeated by a single stamped block at the head: the cursor would jump the
    # whole window and every log in the unstamped middle would be written with a
    # fabricated time, because a trade with no timestamp otherwise falls back to "now" —
    # which for a minute-old backlog is a minute wrong on every lane window that reads it.
    log_blocks = sorted({b for b in (set(stamps) | missing)})
    for block in log_blocks:
        if block not in stamps:
            to_block = min(to_block, block - 1)
            result.note = f"timestamp budget reached; advancing only to {to_block}"
            break

    c = conn or get_conn()
    eth_usd = None
    if config.price_trades:
        eth_usd = (eth_price or _ETH_PRICE).get(conn=c)

    # Launches first: a trade in the same window on a token launched in it can only be
    # attributed once the curve is in the index.
    for entry in factory_logs:
        if not isinstance(entry, Mapping):
            continue
        block = _hex_int(entry.get("blockNumber"))
        if block is not None and block > to_block:
            continue  # beyond this poll's honest reach; re-read next time
        ts_ms = stamps.get(block) if block is not None else None
        topic = str((entry.get("topics") or [""])[0]).lower()
        if topic == TOPIC_TOKEN_LAUNCHED:
            parsed = parse_token_launched(entry, ts_ms=ts_ms)
            if parsed is None:
                continue
            token, meta = parsed
            index.add(meta)
            _LATENCY.record(STREAM_LAUNCH, ts_ms)
            try:
                record_new_token(token, conn=c)
                result.launches += 1
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the poll
                log.warning("pons: could not record launch %s (%s)", token.address[:12], exc)
                continue
            _screen(token, c)
        elif topic == TOPIC_POOL_GRADUATED:
            info = parse_pool_graduated(entry, ts_ms=ts_ms)
            if info is None:
                continue
            _LATENCY.record(STREAM_GRADUATION, ts_ms)
            try:
                record_migration(info, conn=c)
                result.graduations += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("pons: could not record graduation (%s)", exc)

    for entry in curve_logs:
        if not isinstance(entry, Mapping):
            continue
        address = entry.get("address")
        if not isinstance(address, str):
            continue
        block = _hex_int(entry.get("blockNumber"))
        if block is not None and block > to_block:
            continue
        meta = index.get(address)
        if meta is None:
            result.skipped_unknown_curve += 1
            continue
        if not meta.quote_is_native:
            # A non-ETH quote is a different unit. Recording it is fine; pricing it as wei
            # is how a volume figure ends up orders of magnitude out, so parse_curve_trade
            # leaves usd_value None for these and we just count them.
            result.non_native_quote += 1
        ts_ms = stamps.get(block) if block is not None else None
        row = parse_curve_trade(entry, meta=meta, ts_ms=ts_ms, eth_usd=eth_usd)
        if row is None:
            continue
        _LATENCY.record(STREAM_TRADE, ts_ms)
        try:
            if record_trade(row, conn=c):
                result.trades += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("pons: could not record trade %s (%s)", row.get("tx", "?")[:18], exc)

    result.to_block = to_block
    result.lag_blocks = max(0, head - to_block)
    head_ts = stamps.get(to_block)
    if head_ts is not None:
        result.lag_ms = now_ms() - head_ts
    result.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return result


def warm_index(
    *, head: int, index: CurveIndex, conn: Any = None,
    config: ListenerConfig = DEFAULT_CONFIG,
) -> int:
    """Learn curve->token for launches just before we started. Returns curves added.

    Without this every trade in the first minutes is unattributable, because a
    ``CurveBuy`` names the curve and the curve is only tied to its token by the
    ``TokenLaunched`` event that created it.
    """
    added = 0
    span = max(0, config.warm_blocks)
    if span == 0:
        return 0
    lo = max(0, head - span)
    while lo <= head:
        hi = min(lo + 5_000 - 1, head)
        got = rpc_batch(
            [get_logs_call(from_block=lo, to_block=hi, address=FACTORY_V2,
                           topics=[[TOPIC_TOKEN_LAUNCHED]])],
            endpoint="chain.warm",
            conn=conn,
        )
        entries = got.results[0] if got.results and isinstance(got.results[0], list) else []
        for entry in entries:
            parsed = parse_token_launched(entry)
            if parsed is not None:
                before = len(index)
                index.add(parsed[1])
                added += len(index) - before
        lo = hi + 1
    return added


_ETH_PRICE = EthPrice()


def publish_latency(conn: Any = None, *, tracker: LatencyTracker | None = None) -> int | None:
    """Write the current window as an ``ingest_latency`` rollup and a PROVIDER_BUDGET event."""
    t = tracker or _LATENCY
    snap = t.stats()
    if not snap["streams"]:
        return None
    c = conn or get_conn()
    for stream, s in snap["streams"].items():
        try:
            c.execute(
                "INSERT OR IGNORE INTO ingest_latency "
                "(source, stream, window_start_ms, window_end_ms, samples, p50_ms, p95_ms, "
                " min_ms, max_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                (SOURCE, stream, s["window_start_ms"], s["window_end_ms"], s["samples"],
                 int(s["p50_ms"]), int(s["p95_ms"]), int(s["min_ms"]), int(s["max_ms"])),
            )
        except Exception as exc:  # noqa: BLE001 - bookkeeping never breaks ingestion
            log.warning("pons latency rollup write failed: %s", exc)
    return emit(
        EventKind.PROVIDER_BUDGET,
        {"provider": SOURCE, "latency": snap},
        chain=CHAIN,
        conn=c,
    )


def _note_events(count: int, conn: Any) -> None:
    """Report handled events to ``ingest_status`` so a dead feed is distinguishable."""
    if count <= 0:
        return
    try:
        from kaiba.ingest.runner import note_events

        note_events(SOURCE, count, conn)
    except Exception as exc:  # noqa: BLE001
        log.debug("could not report ingest progress: %s", exc)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    if seconds <= 0:
        return
    with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def run(
    stop: asyncio.Event | None = None,
    *,
    conn: Any = None,
    config: ListenerConfig = DEFAULT_CONFIG,
    start_block: int | None = None,
    max_polls: int | None = None,
    poll: Callable[..., PollResult] | None = None,
    index: CurveIndex | None = None,
    on_poll: Callable[[PollResult], None] | None = None,
) -> dict[str, Any]:
    """Follow Pons from the current head until ``stop``. Returns a summary.

    Shaped to be supervised by ``kaiba.ingest.runner``: it holds no lock (unlike
    PumpPortal, a second poller costs nothing but RPC), it never raises out of the loop,
    and it reports into ``ingest_status`` so silence is distinguishable from death.
    """
    stop = stop or asyncio.Event()
    poller = poll or poll_once
    idx = index if index is not None else CurveIndex()
    c = conn

    totals = {"polls": 0, "launches": 0, "graduations": 0, "trades": 0, "rpc_calls": 0,
              "errors": 0, "skipped_unknown_curve": 0, "started_ms": now_ms()}

    cursor = start_block
    if cursor is None:
        probe = rpc_batch([("eth_blockNumber", [])], endpoint="chain.head", conn=c)
        totals["rpc_calls"] += 1
        head = _hex_int(probe.results[0]) if probe.results and isinstance(probe.results[0], str) else None
        if head is None:
            log.error("pons: cannot read the chain head; not starting")
            emit(EventKind.PROVIDER_ERROR,
                 {"provider": SOURCE, "error": "chain head unreadable at startup"},
                 chain=CHAIN, level="error", conn=c)
            return totals
        warmed = warm_index(head=head, index=idx, conn=c, config=config)
        log.info("pons: warmed %d curves from the last %d blocks", warmed, config.warm_blocks)
        cursor = head + 1

    emit(
        EventKind.SYSTEM,
        {"component": "ingest.robinhood", "status": "connected", "chain": CHAIN.value,
         "chain_id": CHAIN_ID, "factory": FACTORY_V2, "from_block": cursor,
         "curves_known": len(idx), "poll_interval_s": config.poll_interval_s},
        chain=CHAIN,
        conn=c,
    )
    log.info("pons: following from block %d, %d curves known", cursor, len(idx))

    attempt = 0
    unreported = 0
    last_head: int | None = cursor - 1 if cursor else None
    next_budget = now_ms() + int(config.budget_interval_s * 1000)
    try:
        while not stop.is_set():
            if max_polls is not None and totals["polls"] >= max_polls:
                break
            # Pass the head we last saw so a poll that has fallen behind bounds its own
            # span. Without it the catch-up after an outage is one enormous getLogs, and
            # the topic-only curve query hits the RPC's result cap well before an hour of
            # blocks — which fails the poll repeatedly and never catches up.
            result = poller(from_block=cursor, index=idx, conn=c, config=config,
                            head=last_head)
            totals["polls"] += 1
            if result.head:
                last_head = result.head
            totals["rpc_calls"] += result.rpc_calls
            if on_poll is not None:
                with contextlib.suppress(Exception):
                    on_poll(result)
            if not result.ok:
                totals["errors"] += 1
                attempt += 1
                delay = backoff_delay(attempt)
                log.warning("pons: poll failed (%s); retry #%d in %.1fs", result.note, attempt, delay)
                emit(
                    EventKind.PROVIDER_ERROR,
                    {"provider": SOURCE, "error": result.note, "attempt": attempt,
                     "retry_in_s": delay, "from_block": cursor},
                    chain=CHAIN, level="warn", conn=c,
                )
                await _wait(stop, delay)
                continue
            attempt = 0
            totals["launches"] += result.launches
            totals["graduations"] += result.graduations
            totals["trades"] += result.trades
            totals["skipped_unknown_curve"] += result.skipped_unknown_curve
            unreported += result.events
            if result.to_block >= result.from_block:
                cursor = result.to_block + 1
            if unreported:
                _note_events(unreported, c)
                unreported = 0
            if now_ms() >= next_budget:
                publish_latency(conn=c)
                next_budget = now_ms() + int(config.budget_interval_s * 1000)
            # A poll that was span-limited is behind; go straight round again.
            if result.lag_blocks > config.max_span_blocks:
                continue
            await _wait(stop, config.poll_interval_s)
    finally:
        publish_latency(conn=c)
        totals["ended_ms"] = now_ms()
        totals["curves_known"] = len(idx)
        log.info("pons: stopped after %d polls (%s)", totals["polls"], totals)
    return totals


__all__ = [
    "CHAIN",
    "CHAIN_ID",
    "CURVE_READS",
    "CurveIndex",
    "CurveMeta",
    "CurveState",
    "EthPrice",
    "FACTORY_V2",
    "LAUNCHPAD",
    "LAUNCH_FEE_WEI",
    "ListenerConfig",
    "MEME_HOOK",
    "POOL_MANAGER",
    "PROVIDER",
    "PollResult",
    "RPC_URL",
    "SECONDS_PER_BLOCK",
    "SNIPE_TAX_MEASURED_CONFIG",
    "SNIPE_TAX_RUNGS_BPS",
    "SOURCE",
    "TOPIC_CURVE_BUY",
    "TOPIC_CURVE_SELL",
    "TOPIC_POOL_GRADUATED",
    "TOPIC_TOKEN_LAUNCHED",
    "curve_for_token",
    "curve_from_state",
    "latency_stats",
    "log_timestamp_ms",
    "normalize",
    "parse_curve_state",
    "parse_curve_trade",
    "parse_pool_graduated",
    "parse_token_launched",
    "poll_once",
    "publish_latency",
    "read_curve",
    "record_migration",
    "record_new_token",
    "record_trade",
    "reset_latency",
    "run",
    "warm_index",
]
