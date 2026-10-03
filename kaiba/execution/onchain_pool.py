"""LOG-ONLY on-chain pool prices for Robinhood Chain positions. Changes no decision.

Why this exists
---------------

``docs/research/audit-20261001-strategy.md`` §1c: Robinhood stops realise about -35% against
a -30% stop, and the measured cause is the MARK, not the sell. Protection prices graduated
Robinhood tokens from ``prices:dexscreener`` (``provider_reported``):

* 61% of consecutive 12 s marks were identical (2,139 pairs, 40 positions);
* the last distinct price before a stop trigger had been held a median 36 s, then jumped a
  median -18.8pp into the trigger;
* the sell itself landed a median 1.8 s after the trigger.

The fix the audit proposes -- price RH positions from the pool itself -- changes the input
of every RH stop. It must not ship on a rationale (``AGENTS.md``: "Do not ship an unmeasured
filter"). This module is the measurement: on a protection tick it reads the pool state of
every open live RH position in ONE ``eth_call`` and records the on-chain price beside the
incumbent quote the watchdog actually decided on. :mod:`kaiba.learning.onchain_vs_feed`
then answers, per closed position, when each series crossed the stop and what the exit
would have been. Nothing here feeds ``protection.evaluate``; the only write is the
``onchain_price_samples`` table (plus a status event every ten minutes).

The budget rules, and why each one is shaped the way it is
----------------------------------------------------------

``robinhood-rpc`` is a public endpoint configured ``max_inflight: 1``, ``refill_per_s: 0.6``,
and live Pons curve reads ride the same bucket at ``Priority.EXIT``. So:

* **One HTTP request per tick, total.** Price reads for every resolved pool AND discovery
  reads for unresolved ones ride a single Multicall3 ``aggregate3`` -- one ``eth_call``, one
  limiter reservation, one block. The one exception is rare and still one request: a v4
  pool whose key is not in the PositionManager needs its ``Initialize`` log, and that
  ``eth_getLogs`` is sent in the same JSON-RPC batch as the tick's ``eth_call``.
* **Reserved at ``Priority.POSITION``, not ``EXIT``.** In :mod:`kaiba.core.limiter` both get
  the SAME pacing (every priority ``<= POSITION`` gets the quartered minimum interval). What
  ``EXIT`` adds is three privileges: it bypasses ``max_inflight``, bypasses a family
  cooldown, and may overdraw the bucket to ``-EXIT_OVERDRAFT_WEIGHT``. Each of those spends
  budget a live sell needs, which a log-only read must never do. ``POSITION`` is EXIT's
  cadence without EXIT's privileges, decided atomically inside the limiter's own
  transaction. (A pre-check followed by an EXIT reservation would be a check-then-act race
  that, when lost, spends exactly the overdraft this exists to leave alone.)
* **A refusal is a silent skip.** We reserve ourselves rather than going through
  ``providers._http.request_json``, which writes a ``provider_error`` event per refusal; a
  reader the bucket turns away every tick must not fill the events table.
* **Off the tick thread.** A robinhood-rpc round trip is ~350 ms p50; inline it would add
  that to every tick, and a late tick is a late stop. The tick hands a snapshot to one
  persistent daemon worker and returns; if the worker is still busy, this tick is skipped.
* **Off by default.** ``protection.onchain_price_log`` (``ProtectionConfig``) is ``False``;
  while it is off :func:`after_tick` returns before touching anything at all.

Pool resolution (once per pool, cached in memory, never per tick)
-----------------------------------------------------------------

* The pool id comes from the incumbent's own quote memory (``kv``
  ``watchdog.quote:<position>:<chain>:<token>``: the ``pool_id`` DexScreener reported).
* A 32-byte id is a Uniswap v4 pool on :data:`~kaiba.ingest.robinhood.POOL_MANAGER`. Its
  ``PoolKey`` comes from ``PositionManager.poolKeys(bytes25)`` and is VERIFIED by hashing it
  back to the id; a key the PositionManager does not hold comes from the PoolManager's
  ``Initialize`` log instead, verified the same way. State is read with ``extsload`` at the
  canonical ``_pools`` slot, so no StateView deployment is assumed. MEASURED 2026-10-02 on
  24 pools our RH positions traded: 16 keys from ``poolKeys``, 8 only in the log, all 24
  state slots non-empty.
* A 20-byte address is asked ``token0``/``token1``/``slot0()``/``getReserves()`` with
  ``allowFailure``; whichever state call answers is the pool's kind (v3 or v2). MEASURED:
  all three such pools we traded are Uniswap v3 on :data:`V3_FACTORY`.
* No pool id at all (a GMGN-priced position): the deepest v3 pool of the token against
  USDG, else WETH, via ``V3_FACTORY.getPool`` over every fee tier, chosen by in-range
  ``liquidity()``. A zero-liquidity pool is never chosen: MEASURED, their ``slot0`` prices
  are garbage (META/USDG 100 bps read 1.0e12).
* Positions the incumbent already prices from the Pons curve on chain (``evm-venue:pons``)
  are skipped: there the incumbent IS an on-chain read.

Quote currency -> USD
---------------------

* native ETH (v4 ``currency0 == 0x0``) and WETH: ``native_prices`` (Robinhood is aliased to
  ETH's samples), the same sampler ``DefaultExitSubmitter._native_usd`` falls back to. A DB
  read, no network.
* USDG: one dollar, recorded as ``usdg_peg`` (GMGN measured 1.00064 on 2026-09-22).
* Anything else -- the tokenised equities (META, GME, TSLA, SPCX ...) Pons launches are
  quoted in -- is priced through its own deepest v3 pool against USDG (else WETH), read in
  the same multicall every tick. MEASURED: META/USDG 3000 read 726.99 while META/WETH 3000
  read 0.2691 x ETH 2,698.69 = 726.3; a 0.1% spread between independent routes.

Money is integer until the final ratio, which is ``Decimal``; nothing passes through
``float``.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, LaneMode, now_ms

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# chain constants
# --------------------------------------------------------------------------------------

#: Canonical Multicall3. VERIFIED on Robinhood Chain 2026-10-02 with one ``eth_getCode``:
#: 3,808 bytes of runtime code, solc 0.8.12 metadata trailer -- the canonical deployment.
MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"

#: ArbSys precompile. Robinhood Chain is an Arbitrum Orbit chain, and on Arbitrum
#: ``block.number`` inside a contract is the PARENT chain's block number, so Multicall3's
#: ``getBlockNumber()`` would record an L1 height. ``arbBlockNumber()`` is the L2 one.
#: VERIFIED 2026-10-02: 77,759,835 against ``eth_blockNumber`` 77,756,634 minutes earlier.
ARBSYS = "0x0000000000000000000000000000000000000064"

#: Uniswap v4 PoolManager and PositionManager, as identified (with receipts) in
#: :mod:`kaiba.ingest.robinhood`. Repeated here, not imported, so this module's import
#: never pulls the ingest listener into the protection process; a test pins the two equal.
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
POSITION_MANAGER = "0x58daec3116aae6d93017baaea7749052e8a04fa7"

#: Uniswap v3 factory on Robinhood Chain. MEASURED 2026-10-02: ``factory()`` of all three
#: 20-byte pools our RH positions traded (VLADBOT/WETH, NVDA/USDG, LITANY/WETH).
V3_FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
V3_FEE_TIERS: tuple[int, ...] = (100, 500, 3000, 10000)

#: MEASURED 2026-10-02 by ``symbol()``/``decimals()`` on chain: WETH 18, USDG 6. USDG is
#: the quote leg of a quarter of RH tokens, and reading it at 18 is a 1e12 error.
ROBINHOOD_WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
ROBINHOOD_USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"

#: Uniswap v4 ``Pool.State`` mapping slot in ``PoolManager`` (``StateLibrary.POOLS_SLOT``).
V4_POOLS_SLOT = 6

#: ``Initialize(bytes32 indexed id, address indexed currency0, address indexed currency1,
#: uint24 fee, int24 tickSpacing, address hooks, uint160 sqrtPriceX96, int24 tick)``.
TOPIC_V4_INITIALIZE = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"

#: MEASURED 2026-10-02: the public RPC refuses ``eth_getLogs`` spanning more than
#: 10,000,000 blocks ("only 10000000 are allowed for this request"), ~11.7 days at 0.1015 s.
GET_LOGS_MAX_SPAN = 10_000_000
#: Windows searched backwards for a v4 ``Initialize`` before a pool is given up (~35 days).
GET_LOGS_MAX_WINDOWS = 3

#: Selectors. Each is pinned against ``kaiba.execution.policy.selector_of`` in the tests,
#: so a typo fails a test instead of silently reading the wrong function.
SEL_AGGREGATE3 = "0x82ad56cb"  # aggregate3((address,bool,bytes)[])
SEL_BLOCK_TIMESTAMP = "0x0f28c97d"  # getCurrentBlockTimestamp()
SEL_ARB_BLOCK_NUMBER = "0xa3b1b31d"  # arbBlockNumber()
SEL_SLOT0 = "0x3850c7bd"  # slot0()
SEL_GET_RESERVES = "0x0902f1ac"  # getReserves()
SEL_TOKEN0 = "0x0dfe1681"  # token0()
SEL_TOKEN1 = "0xd21220a7"  # token1()
SEL_DECIMALS = "0x313ce567"  # decimals()
SEL_LIQUIDITY = "0x1a686502"  # liquidity()
SEL_EXTSLOAD = "0x1e2eaeaf"  # extsload(bytes32)
SEL_POOL_KEYS = "0x86b6be7d"  # poolKeys(bytes25)
SEL_GET_POOL = "0x1698ee82"  # getPool(address,address,uint24)

ZERO_ADDRESS = "0x" + "0" * 40
_U160 = (1 << 160) - 1
_Q192 = Decimal(2) ** 192

# --------------------------------------------------------------------------------------
# budget and bookkeeping
# --------------------------------------------------------------------------------------

#: Limiter provider and endpoint. ``chain.`` puts us in the same family as the live curve
#: reads (``chain.price``), so a 429 cools this reader down together with everything else
#: that is non-EXIT, and a family cooldown someone else earned refuses us too.
PROVIDER = "robinhood-rpc"
ENDPOINT = "chain.onchain_log"

#: See the module docstring: EXIT's pacing without EXIT's bypasses. A test pins that this
#: is refused where EXIT would be let through, and fails if it is changed to EXIT.
RESERVE_PRIORITY = Priority.POSITION

#: Public endpoint the RH ingest already uses (``kaiba.ingest.robinhood.RPC_URL``).
RPC_URL = "https://rpc.mainnet.chain.robinhood.com"

#: One HTTP timeout. Short on purpose: while it is open we hold robinhood-rpc's only
#: non-EXIT inflight slot, and a log sample that arrives 4 s late is worth little anyway.
RPC_TIMEOUT_S = 4.0

#: Shipped defaults for the ``protection:`` keys (see ``ProtectionConfig``).
DEFAULT_MIN_INTERVAL_S = 10.0
DEFAULT_RETENTION_DAYS = 14

#: Upper bound on one multicall. Price reads come first; discovery fills what is left.
MAX_SUBCALLS = 120

#: How often the worker prunes old rows, and how many per pass (indexed by ``ts_ms``).
PRUNE_EVERY_MS = 3_600_000
PRUNE_CHUNK = 5_000
PRUNE_MAX_CHUNKS = 20

#: How often a status event is written (counts only, never prices).
STATUS_EVERY_MS = 600_000

#: A discovery read that came back undecodable this many times gives the pool up.
MAX_DISCOVERY_ATTEMPTS = 5

#: A token with no v3 pool is asked again after this long: it may graduate onto one.
FINDER_RETRY_MS = 1_800_000

TABLE = "onchain_price_samples"


# --------------------------------------------------------------------------------------
# ABI helpers
# --------------------------------------------------------------------------------------


def _word(value: int) -> bytes:
    return int(value).to_bytes(32, "big")


def _addr_word(address: str) -> bytes:
    return _word(int(address, 16))


def encode_call(selector: str, *words: bytes) -> str:
    """``selector`` followed by already-encoded 32-byte static arguments, as hex."""
    return selector + b"".join(words).hex()


def encode_aggregate3(calls: Sequence[tuple[str, str]], *, allow_failure: bool = True) -> str:
    """ABI-encode ``aggregate3(Call3[])`` for ``(target, calldata_hex)`` pairs.

    ``Call3`` is ``(address target, bool allowFailure, bytes callData)``; the ``bytes``
    member makes each tuple dynamic, so the array is a head of offsets (relative to the
    first offset word) followed by the tuples. ``allow_failure`` is ``True`` here: one
    unreadable pool must cost that pool's sample, not the whole tick's.
    """
    tuples: list[bytes] = []
    for target, data in calls:
        payload = bytes.fromhex(data[2:] if data.startswith("0x") else data)
        padded = payload + b"\x00" * ((-len(payload)) % 32)
        tuples.append(
            _addr_word(target)
            + _word(1 if allow_failure else 0)
            + _word(0x60)
            + _word(len(payload))
            + padded
        )
    offsets: list[bytes] = []
    position = 32 * len(tuples)
    for encoded in tuples:
        offsets.append(_word(position))
        position += len(encoded)
    body = _word(0x20) + _word(len(tuples)) + b"".join(offsets) + b"".join(tuples)
    return SEL_AGGREGATE3 + body.hex()


def _read_word(raw: bytes, at: int) -> int | None:
    if at < 0 or at + 32 > len(raw):
        return None
    return int.from_bytes(raw[at : at + 32], "big")


def decode_aggregate3(result: Any, expected: int) -> list[tuple[bool, bytes]] | None:
    """Decode ``Result[]`` = ``(bool success, bytes returnData)[]``. ``None`` if malformed.

    Every offset and length is bounds-checked: a truncated or hostile return must produce
    "no data", never an IndexError and never bytes read from the wrong place.
    """
    if not isinstance(result, str) or not result.startswith("0x"):
        return None
    try:
        raw = bytes.fromhex(result[2:])
    except ValueError:
        return None
    base = _read_word(raw, 0)
    if base is None:
        return None
    count = _read_word(raw, base)
    if count is None or count != expected:
        return None
    heads = base + 32
    out: list[tuple[bool, bytes]] = []
    for index in range(count):
        rel = _read_word(raw, heads + 32 * index)
        if rel is None:
            return None
        start = heads + rel
        success = _read_word(raw, start)
        data_rel = _read_word(raw, start + 32)
        if success is None or data_rel is None:
            return None
        data_at = start + data_rel
        length = _read_word(raw, data_at)
        if length is None or data_at + 32 + length > len(raw):
            return None
        out.append((success != 0, raw[data_at + 32 : data_at + 32 + length]))
    return out


def _words(blob: bytes) -> list[int]:
    return [int.from_bytes(blob[i : i + 32], "big") for i in range(0, len(blob) - 31, 32)]


def _address_of(word: int) -> str:
    return "0x" + format(word & _U160, "040x")


def _uint(blob: bytes) -> int | None:
    words = _words(blob)
    return words[0] if words else None


# --------------------------------------------------------------------------------------
# pool state -> price
# --------------------------------------------------------------------------------------


def v4_state_slot(pool_id: str) -> str:
    """``keccak256(abi.encode(poolId, POOLS_SLOT))``: where ``Pool.State.slot0`` lives."""
    from kaiba.execution.policy import keccak256  # pure stdlib keccak, already in the tree

    raw = bytes.fromhex(pool_id[2:]) + _word(V4_POOLS_SLOT)
    return "0x" + keccak256(raw).hex()


def v4_pool_id(currency0: str, currency1: str, fee: int, tick_spacing: int, hooks: str) -> str:
    """``keccak256(abi.encode(PoolKey))``. Used to VERIFY a key read back from chain."""
    from kaiba.execution.policy import keccak256

    spacing = int(tick_spacing) & ((1 << 256) - 1)  # int24, sign-extended to a word
    raw = (
        _addr_word(currency0)
        + _addr_word(currency1)
        + _word(int(fee))
        + _word(spacing)
        + _addr_word(hooks)
    )
    return "0x" + keccak256(raw).hex()


def decode_pool_key(blob: bytes) -> tuple[str, str, int, int, str] | None:
    """``poolKeys(bytes25)`` -> ``(currency0, currency1, fee, tickSpacing, hooks)``."""
    words = _words(blob)
    if len(words) < 5:
        return None
    spacing = words[3]
    if spacing >= 1 << 255:
        spacing -= 1 << 256
    return (_address_of(words[0]), _address_of(words[1]), words[2], spacing, _address_of(words[4]))


def decode_initialize_log(entry: Any) -> tuple[str, str, int, int, str] | None:
    """A v4 ``Initialize`` log -> the ``PoolKey`` it created, or ``None``."""
    if not isinstance(entry, dict):
        return None
    topics = entry.get("topics")
    data = entry.get("data")
    if not isinstance(topics, list) or len(topics) < 4 or not isinstance(data, str):
        return None
    if str(topics[0]).lower() != TOPIC_V4_INITIALIZE:
        return None
    try:
        words = _words(bytes.fromhex(data[2:]))
        c0 = _address_of(int(str(topics[2]), 16))
        c1 = _address_of(int(str(topics[3]), 16))
    except ValueError:
        return None
    if len(words) < 3:
        return None
    spacing = words[1]
    if spacing >= 1 << 255:
        spacing -= 1 << 256
    return (c0, c1, words[0], spacing, _address_of(words[2]))


def sqrt_price_from_v3_slot0(blob: bytes) -> int | None:
    """``slot0()`` word 0 is ``uint160 sqrtPriceX96``."""
    words = _words(blob)
    if not words:
        return None
    value = words[0] & _U160
    return value or None


def sqrt_price_from_v4_slot(blob: bytes) -> int | None:
    """``extsload`` of the state slot: the low 160 bits are ``sqrtPriceX96``.

    Zero means the slot is empty -- an uninitialised pool, or a wrong slot -- and is
    refused rather than read as a price of zero.
    """
    words = _words(blob)
    if len(words) != 1:
        return None
    value = words[0] & _U160
    return value or None


def reserves_from_v2(blob: bytes) -> tuple[int, int] | None:
    words = _words(blob)
    if len(words) < 2 or words[0] <= 0 or words[1] <= 0:
        return None
    return words[0], words[1]


def price_from_sqrt(sqrt_price_x96: int, dec0: int, dec1: int, token_is0: bool) -> Decimal | None:
    """Token price in whole QUOTE units from a sqrtPriceX96.

    ``sqrtPriceX96^2 / 2^192`` is currency1 base units per currency0 base unit;
    ``* 10^(dec0 - dec1)`` makes it whole units. The token's price is that ratio when the
    token is currency0 and its inverse when it is currency1.
    """
    if sqrt_price_x96 <= 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 60
        try:
            ratio = Decimal(sqrt_price_x96) * Decimal(sqrt_price_x96) / _Q192
            ratio = ratio * (Decimal(10) ** (int(dec0) - int(dec1)))
            if ratio <= 0:
                return None
            price = ratio if token_is0 else Decimal(1) / ratio
        except (InvalidOperation, ArithmeticError):
            return None
    return +price


def price_from_reserves(r0: int, r1: int, dec0: int, dec1: int, token_is0: bool) -> Decimal | None:
    """Token price in whole QUOTE units from v2 reserves (the marginal price, no impact)."""
    if r0 <= 0 or r1 <= 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 60
        whole0 = Decimal(r0) / (Decimal(10) ** int(dec0))
        whole1 = Decimal(r1) / (Decimal(10) ** int(dec1))
        price = whole1 / whole0 if token_is0 else whole0 / whole1
    return +price


def _sig(value: Decimal | None, digits: int = 14) -> str | None:
    """Compact Decimal text: ``digits`` significant figures, no exponent noise in the DB."""
    if value is None:
        return None
    with localcontext() as ctx:
        ctx.prec = digits
        return format(+value, "f") if abs(value.adjusted()) < 20 else str(+value)


def _lower(address: str | None) -> str | None:
    return address.lower() if isinstance(address, str) else None


def _is_hex(text: Any, length: int) -> bool:
    if not isinstance(text, str) or len(text) != length or not text.startswith("0x"):
        return False
    try:
        int(text, 16)
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------------------
# resolvers
# --------------------------------------------------------------------------------------


@dataclass
class PoolEntry:
    """One pool, from "we have an id" to "we can price the token from it every tick".

    ``state``: ``key`` (ask the chain what the pool is), ``logs`` (v4 key only in the
    ``Initialize`` log), ``ready``, ``dead``. Only ``ready`` produces samples.
    """

    pool: str
    token: str
    kind: str | None = None  # v2 | v3 | v4
    token_is0: bool | None = None
    quote: str | None = None  # None means the native coin (v4 currency 0x0)
    state: str = "key"
    reason: str | None = None
    attempts: int = 0
    logs_windows: int = 0

    @property
    def is_v4(self) -> bool:
        return len(self.pool) == 66

    def discovery_calls(self) -> list[tuple[str, str]]:
        if self.state != "key":
            return []
        if self.is_v4:
            b25 = bytes.fromhex(self.pool[2:])[:25] + bytes(7)
            return [(POSITION_MANAGER, SEL_POOL_KEYS + b25.hex())]
        return [
            (self.pool, SEL_TOKEN0),
            (self.pool, SEL_TOKEN1),
            (self.pool, SEL_SLOT0),
            (self.pool, SEL_GET_RESERVES),
        ]

    def _fail(self, reason: str) -> None:
        self.attempts += 1
        self.reason = reason
        if self.attempts >= MAX_DISCOVERY_ATTEMPTS:
            self.state = "dead"

    def adopt_v4_key(self, key: tuple[str, str, int, int, str]) -> None:
        if v4_pool_id(*key) != self.pool:
            self.state, self.reason = "dead", "v4_key_mismatch"
            return
        c0, c1 = key[0], key[1]
        if self.token == c0:
            self.token_is0, other = True, c1
        elif self.token == c1:
            self.token_is0, other = False, c0
        else:
            self.state, self.reason = "dead", "token_not_in_pool"
            return
        self.kind = "v4"
        self.quote = None if other == ZERO_ADDRESS else other
        self.state, self.reason = "ready", None

    def take_discovery(self, results: Sequence[tuple[bool, bytes]]) -> None:
        if self.is_v4:
            ok, blob = results[0]
            key = decode_pool_key(blob) if ok else None
            if key is None:
                self._fail("pool_keys_unreadable")
            elif key[0] == key[1] == ZERO_ADDRESS:
                # Not created through this PositionManager. MEASURED: 8 of 24 traded pools.
                self.state, self.reason = "logs", "pool_keys_empty"
            else:
                self.adopt_v4_key(key)
            return
        (ok0, b0), (ok1, b1), (ok_s, b_s), (ok_r, b_r) = results
        t0 = _address_of(_uint(b0)) if ok0 and _uint(b0) is not None else None
        t1 = _address_of(_uint(b1)) if ok1 and _uint(b1) is not None else None
        if t0 is None or t1 is None:
            self._fail("pair_tokens_unreadable")
            return
        if self.token not in (t0, t1):
            self.state, self.reason = "dead", "token_not_in_pool"
            return
        v3 = ok_s and len(b_s) >= 64 and sqrt_price_from_v3_slot0(b_s) is not None
        v2 = ok_r and len(b_r) >= 96 and reserves_from_v2(b_r) is not None
        if v3 == v2:
            self._fail("pool_kind_ambiguous" if v3 else "pool_state_unreadable")
            return
        self.kind = "v3" if v3 else "v2"
        self.token_is0 = self.token == t0
        self.quote = t1 if self.token_is0 else t0
        self.state, self.reason = "ready", None

    def logs_params(self, latest_block: int) -> dict[str, Any] | None:
        if self.state != "logs" or latest_block <= 0:
            return None
        to_block = latest_block - self.logs_windows * GET_LOGS_MAX_SPAN
        if to_block <= 0:
            return None
        from_block = max(0, to_block - GET_LOGS_MAX_SPAN + 1)
        return {
            "address": POOL_MANAGER,
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "topics": [TOPIC_V4_INITIALIZE, self.pool],
        }

    def take_logs(self, logs: Any, *, reached_genesis: bool) -> None:
        if not isinstance(logs, list):
            self._fail("initialize_logs_unreadable")
            return
        for entry in logs:
            topics = entry.get("topics") if isinstance(entry, dict) else None
            if not isinstance(topics, list) or len(topics) < 2 or str(topics[1]).lower() != self.pool:
                continue
            key = decode_initialize_log(entry)
            if key is not None:
                self.adopt_v4_key(key)
                return
        self.logs_windows += 1
        if self.logs_windows >= GET_LOGS_MAX_WINDOWS or reached_genesis:
            self.state, self.reason = "dead", "v4_initialize_not_found"

    def price_call(self) -> tuple[str, str]:
        if self.kind == "v4":
            return (POOL_MANAGER, encode_call(SEL_EXTSLOAD, bytes.fromhex(v4_state_slot(self.pool)[2:])))
        if self.kind == "v3":
            return (self.pool, SEL_SLOT0)
        return (self.pool, SEL_GET_RESERVES)

    def price_from(self, ok: bool, blob: bytes, decimals: dict[str | None, int]) -> Decimal | None:
        """The token's price in whole quote units, or ``None``. Never a guess."""
        if not ok or self.token_is0 is None:
            return None
        dt, dq = decimals.get(self.token), decimals.get(self.quote)
        if dt is None or dq is None:
            return None
        dec0, dec1 = (dt, dq) if self.token_is0 else (dq, dt)
        if self.kind == "v4":
            sqrt = sqrt_price_from_v4_slot(blob)
            return price_from_sqrt(sqrt, dec0, dec1, self.token_is0) if sqrt else None
        if self.kind == "v3":
            sqrt = sqrt_price_from_v3_slot0(blob)
            return price_from_sqrt(sqrt, dec0, dec1, self.token_is0) if sqrt else None
        reserves = reserves_from_v2(blob)
        if reserves is None:
            return None
        return price_from_reserves(reserves[0], reserves[1], dec0, dec1, self.token_is0)


@dataclass
class V3Finder:
    """The deepest Uniswap v3 pool of ``asset`` against USDG (preferred) or WETH.

    ``find`` asks the factory for every fee tier; ``depth`` reads ``slot0`` and
    ``liquidity`` of what exists; ``done`` holds the chosen pool. A pool with zero in-range
    liquidity is never chosen, because its ``slot0`` is whatever the last trade left behind.
    """

    asset: str
    state: str = "find"
    candidates: list[tuple[str, int, str]] = field(default_factory=list)  # (quote, fee, pool)
    pool: str | None = None
    quote: str | None = None
    reason: str | None = None
    retry_at_ms: int = 0
    attempts: int = 0

    def _queries(self) -> list[tuple[str, int]]:
        return [(q, fee) for q in (ROBINHOOD_USDG, ROBINHOOD_WETH) for fee in V3_FEE_TIERS]

    def discovery_calls(self, now: int) -> list[tuple[str, str]]:
        if self.state == "dead" and self.retry_at_ms and now >= self.retry_at_ms:
            self.state, self.candidates, self.reason = "find", [], None
        if self.state == "find":
            return [
                (V3_FACTORY, encode_call(SEL_GET_POOL, _addr_word(self.asset), _addr_word(q), _word(fee)))
                for q, fee in self._queries()
            ]
        if self.state == "depth":
            calls: list[tuple[str, str]] = []
            for _, _, pool in self.candidates:
                calls += [(pool, SEL_SLOT0), (pool, SEL_LIQUIDITY)]
            return calls
        return []

    def _die(self, reason: str, now: int) -> None:
        self.state, self.reason, self.retry_at_ms = "dead", reason, now + FINDER_RETRY_MS

    def take_discovery(self, results: Sequence[tuple[bool, bytes]], now: int) -> None:
        if self.state == "find":
            found: list[tuple[str, int, str]] = []
            for (q, fee), (ok, blob) in zip(self._queries(), results, strict=False):
                word = _uint(blob) if ok else None
                if word:
                    found.append((q, fee, _address_of(word)))
            if found:
                self.candidates, self.state = found, "depth"
            else:
                self._die("no_v3_pool", now)
            return
        if self.state == "depth":
            best: dict[str, tuple[int, str]] = {}
            for index, (q, _fee, pool) in enumerate(self.candidates):
                ok_s, b_s = results[2 * index]
                ok_l, b_l = results[2 * index + 1]
                liquidity = _uint(b_l) if ok_l else None
                if not (ok_s and sqrt_price_from_v3_slot0(b_s)) or not liquidity:
                    continue
                if q not in best or liquidity > best[q][0]:
                    best[q] = (liquidity, pool)
            for q in (ROBINHOOD_USDG, ROBINHOOD_WETH):
                if q in best:
                    self.pool, self.quote, self.state = best[q][1], q, "done"
                    return
            self._die("no_v3_pool_with_liquidity", now)


# --------------------------------------------------------------------------------------
# the logger
# --------------------------------------------------------------------------------------


@dataclass
class Target:
    """One open live RH position, as the tick saw it."""

    position_id: str
    token: str  # lower-case, for comparing with addresses read off the chain
    token_key: str  # as the positions/tokens/kv rows spell it
    entry_price_usd: Decimal | None
    cached: dict[str, Any] | None = None  # this tick's prefetched quote, if any


@dataclass
class Job:
    targets: list[Target]
    created_ms: int
    retention_days: int = DEFAULT_RETENTION_DAYS


@dataclass
class Stats:
    """Counts for the status event. Never prices."""

    jobs: int = 0
    calls: int = 0
    samples: int = 0
    errors: int = 0
    skips: dict[str, int] = field(default_factory=dict)
    rpc_ms: list[int] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def skip(self, reason: str) -> None:
        with self.lock:
            self.skips[reason] = self.skips.get(reason, 0) + 1

    def payload(self) -> dict[str, Any]:
        with self.lock:
            ordered = sorted(self.rpc_ms)
            p50 = ordered[len(ordered) // 2] if ordered else None
            return {
                "jobs": self.jobs,
                "calls": self.calls,
                "samples": self.samples,
                "errors": self.errors,
                "skips": dict(self.skips),
                "rpc_ms_p50": p50,
                "rpc_ms_max": ordered[-1] if ordered else None,
            }

    def reset(self) -> None:
        with self.lock:
            self.jobs = self.calls = self.samples = self.errors = 0
            self.skips = {}
            self.rpc_ms = []


class _RpcRateLimited(RuntimeError):
    """A 429 from the endpoint, shaped so ``limiter.guarded`` opens a cooldown."""

    status_code = 429


def _default_post(payload: Any, timeout_s: float) -> Any:
    """One HTTP POST. ``httpx``, because the RH RPC blocks urllib's User-Agent."""
    import httpx

    resp = httpx.post(RPC_URL, json=payload, timeout=timeout_s, follow_redirects=True)
    if resp.status_code == 429:
        raise _RpcRateLimited("429 rate limited")
    resp.raise_for_status()
    return json.loads(resp.text)


def _looks_rate_limited(item: Any) -> bool:
    error = item.get("error") if isinstance(item, dict) else None
    if not isinstance(error, dict):
        return False
    message = str(error.get("message") or "").lower()
    return error.get("code") in (429, -32005) or "rate limit" in message or "too many" in message


class OnchainPriceLog:
    """The per-process logger. One instance, one worker thread, one call per tick."""

    def __init__(
        self,
        *,
        post: Callable[[Any, float], Any] | None = None,
        conn_factory: Callable[[], Any] | None = None,
        clock_ms: Callable[[], int] = now_ms,
        inline: bool = False,
    ) -> None:
        self._post = post or _default_post
        self._conn_factory = conn_factory
        self._clock_ms = clock_ms
        self._inline = inline
        self.pools: dict[str, PoolEntry] = {}
        self.finders: dict[str, V3Finder] = {}
        self.position_pool: dict[str, str] = {}
        self.decimals: dict[str | None, int] = {None: 18, ROBINHOOD_WETH: 18, ROBINHOOD_USDG: 6}
        self.latest_block = 0
        self._looked_up: set[str] = set()
        self.stats = Stats()
        self._last_dispatch_ms = 0
        self._last_prune_ms = 0
        self._last_status_ms = 0
        self._busy = False
        self._queue: queue.Queue[Job] | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ tick side

    def on_tick(self, watchdog: Any, positions: Sequence[Any], cfg: Any) -> bool:
        """Snapshot what the tick knows and hand it to the worker. Returns whether queued.

        Runs on the protection thread, so it does no I/O at all: no database, no network,
        no config parse. Everything else happens on the worker.
        """
        now = self._clock_ms()
        try:
            gap_s = float(getattr(cfg, "onchain_price_log_min_interval_s", DEFAULT_MIN_INTERVAL_S))
        except (TypeError, ValueError):
            gap_s = DEFAULT_MIN_INTERVAL_S
        if self._last_dispatch_ms and now - self._last_dispatch_ms < max(0.0, gap_s) * 1000:
            self.stats.skip("cadence")
            return False
        cache = getattr(watchdog, "_quote_cache", None) or {}
        targets: list[Target] = []
        for position in positions:
            if getattr(position, "chain", None) is not Chain.ROBINHOOD:
                continue
            if getattr(position, "mode", None) is LaneMode.SHADOW or int(getattr(position, "qty", 0) or 0) <= 0:
                continue
            cached = cache.get((position.chain, position.token))
            snapshot = None
            if cached is not None:
                snapshot = {
                    "price_usd": None if cached.price_usd is None else str(cached.price_usd),
                    "source": cached.source,
                    "observed_ms": cached.observed_ms,
                    "usable": bool(cached.usable),
                    "pool_id": cached.pool_id,
                }
            token = str(position.token)
            targets.append(
                Target(position.position_id, token.lower(), token, position.entry_price_usd, snapshot)
            )
        if not targets:
            return False
        try:
            days = int(getattr(cfg, "onchain_price_log_retention_days", DEFAULT_RETENTION_DAYS))
        except (TypeError, ValueError):
            days = DEFAULT_RETENTION_DAYS
        job = Job(targets=targets, created_ms=now, retention_days=max(1, days))
        if self._inline:
            self._last_dispatch_ms = now
            self._safe_run(job)
            return True
        if self._busy:
            self.stats.skip("worker_busy")
            return False
        self._ensure_worker()
        try:
            assert self._queue is not None
            self._queue.put_nowait(job)
        except queue.Full:
            self.stats.skip("worker_busy")
            return False
        self._last_dispatch_ms = now
        return True

    def _ensure_worker(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._queue = queue.Queue(maxsize=1)
        self._thread = threading.Thread(target=self._loop, name="kaiba-onchain-price-log", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        assert self._queue is not None
        while True:
            job = self._queue.get()
            self._busy = True
            try:
                self._safe_run(job)
            finally:
                self._busy = False

    def _safe_run(self, job: Job) -> None:
        try:
            self.run_job(job)
        except Exception as exc:  # noqa: BLE001 - a log-only reader never takes anything down
            self.stats.errors += 1
            log.warning("onchain price log job failed: %s: %s", type(exc).__name__, exc)

    # ------------------------------------------------------------------ worker side

    def _conn(self) -> Any:
        if self._conn_factory is not None:
            return self._conn_factory()
        from kaiba.core.db import get_conn

        return get_conn()

    def run_job(self, job: Job) -> dict[str, Any]:
        """Plan, read, decode, record. Returns a small report (tests and the CLI read it)."""
        conn = self._conn()
        self.stats.jobs += 1
        plan = self._plan(conn, job)
        calls, price_at, discovery, logs_entry, logs_params = self._build(plan)
        report: dict[str, Any] = {"targets": len(job.targets), "subcalls": len(calls), "samples": 0}
        if len(calls) <= 2:
            report["skipped"] = "nothing_to_read"
            self._maybe_status(conn)
            return report
        eth_call = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": MULTICALL3, "data": encode_aggregate3(calls)}, "latest"],
        }
        payload: Any = eth_call
        if logs_params is not None:
            payload = [eth_call, {"jsonrpc": "2.0", "id": 2, "method": "eth_getLogs", "params": [logs_params]}]
        response, note, rpc_ms = self._send(payload)
        report["rpc_ms"] = rpc_ms
        if response is None:
            report["skipped"] = note
            self._maybe_status(conn)
            return report
        received_ms = self._clock_ms()
        by_id = self._by_id(response)
        results = decode_aggregate3((by_id.get(1) or {}).get("result"), len(calls))
        if logs_entry is not None and logs_params is not None:
            logs_item = by_id.get(2) or {}
            if "result" in logs_item:
                logs_entry.take_logs(logs_item["result"], reached_genesis=int(logs_params["fromBlock"], 16) == 0)
            else:
                logs_entry._fail("initialize_logs_error")
        if results is None:
            self.stats.errors += 1
            report["skipped"] = "multicall_undecodable"
            self._maybe_status(conn)
            return report
        header_block = _uint(results[0][1]) if results[0][0] else None
        header_ts = _uint(results[1][1]) if results[1][0] else None
        if header_block:
            self.latest_block = header_block
        for resolver, start, count in discovery:
            chunk = results[start : start + count]
            if isinstance(resolver, PoolEntry):
                resolver.take_discovery(chunk)
            elif isinstance(resolver, V3Finder):
                resolver.take_discovery(chunk, received_ms)
            else:  # an address whose decimals() we asked for
                value = _uint(chunk[0][1]) if chunk[0][0] else None
                if value is not None and 0 <= value <= 36:
                    self.decimals[resolver] = value
        self._adopt_finders()
        rows = self._samples(conn, plan, results, price_at, received_ms, header_block, header_ts, rpc_ms)
        if rows:
            self._write(conn, rows)
        report["samples"] = len(rows)
        self._maybe_prune(conn, job, received_ms)
        self._maybe_status(conn)
        return report

    def _by_id(self, response: Any) -> dict[int, dict[str, Any]]:
        items = response if isinstance(response, list) else [response]
        return {
            int(item["id"]): item for item in items if isinstance(item, dict) and isinstance(item.get("id"), int)
        }

    def _send(self, payload: Any) -> tuple[Any | None, str, int]:
        """One reservation, one POST, one release. A refusal is a skip; nothing was sent."""
        from kaiba.core import limiter

        started = time.monotonic()
        try:
            with limiter.guarded(PROVIDER, ENDPOINT, RESERVE_PRIORITY):
                response = self._post(payload, RPC_TIMEOUT_S)
                items = response if isinstance(response, list) else [response]
                if any(_looks_rate_limited(item) for item in items):
                    raise _RpcRateLimited("json-rpc rate limited")
        except limiter.RateLimited as exc:
            self.stats.skip(f"limiter:{exc.reason}")
            return None, f"limiter:{exc.reason}", 0
        except Exception as exc:  # noqa: BLE001 - the endpoint being down is data
            self.stats.errors += 1
            return None, f"rpc_error:{type(exc).__name__}", int((time.monotonic() - started) * 1000)
        elapsed = int((time.monotonic() - started) * 1000)
        with self.stats.lock:
            self.stats.calls += 1
            self.stats.rpc_ms.append(elapsed)
            del self.stats.rpc_ms[:-500]
        return response, "ok", elapsed

    # ------------------------------------------------------------------ planning

    def _incumbent(self, conn: Any, target: Target) -> dict[str, Any] | None:
        """The quote the watchdog DECIDED on this tick (its kv memory), else the prefetch."""
        from kaiba.core.db import fetch_one, jload

        # The key is spelled exactly as ``Watchdog._quote_memory_key`` spells it.
        key = f"watchdog.quote:{target.position_id}:{Chain.ROBINHOOD.value}:{target.token_key}"
        row = fetch_one(conn, "SELECT value FROM kv WHERE key=?", (key,))
        memory = jload(row["value"], {}) if row else {}
        price = memory.get("price") if isinstance(memory, dict) else None
        if isinstance(price, dict) and price.get("price_usd") is not None:
            return {
                "price_usd": str(price.get("price_usd")),
                "source": price.get("source"),
                "observed_ms": price.get("observed_ms"),
                "pool_id": price.get("pool_id"),
                "usable": True,
            }
        return target.cached

    def _plan(self, conn: Any, job: Job) -> list[dict[str, Any]]:
        from kaiba.core.db import fetch_one

        plan: list[dict[str, Any]] = []
        for target in job.targets:
            incumbent = self._incumbent(conn, target)
            source = str((incumbent or {}).get("source") or "")
            if source.startswith("evm-venue"):
                self.stats.skip("incumbent_onchain_curve")
                continue
            if target.token not in self.decimals and target.token not in self._looked_up:
                # Once per token, by primary key. A miss falls through to ``decimals()``
                # in the next multicall rather than re-querying every tick.
                self._looked_up.add(target.token)
                row = fetch_one(
                    conn,
                    "SELECT decimals FROM tokens WHERE chain=? AND address=?",
                    (Chain.ROBINHOOD.value, target.token_key),
                )
                if row and row["decimals"] is not None:
                    self.decimals[target.token] = int(row["decimals"])
            pool_id = _lower((incumbent or {}).get("pool_id"))
            entry = None
            if _is_hex(pool_id, 66) or _is_hex(pool_id, 42):
                entry = self.pools.get(pool_id)
                if entry is None:
                    entry = self.pools[pool_id] = PoolEntry(pool=pool_id, token=target.token)
            else:
                finder = self.finders.get(target.token)
                if finder is None:
                    finder = self.finders[target.token] = V3Finder(asset=target.token)
                if finder.state == "done" and finder.pool:
                    entry = self.pools.get(finder.pool)
            previous = self.pools.get(self.position_pool.get(target.position_id, ""))
            chosen = entry
            if entry is not None and entry.state == "ready":
                self.position_pool[target.position_id] = entry.pool
            elif previous is not None and previous.state == "ready":
                chosen = previous  # keep sampling the pool we know until the new one resolves
            stop = fetch_one(
                conn, "SELECT stop_price_usd FROM watchdog_state WHERE position_id=?", (target.position_id,)
            )
            plan.append(
                {
                    "target": target,
                    "incumbent": incumbent,
                    "entry": chosen,
                    "pending": entry,
                    "stop": stop["stop_price_usd"] if stop else None,
                }
            )
        return plan

    def _quote_ref(self, quote: str | None) -> PoolEntry | None:
        """The ready v3 pool that prices a non-ETH, non-USDG quote asset, if any."""
        if quote is None or quote in (ROBINHOOD_WETH, ROBINHOOD_USDG):
            return None
        finder = self.finders.get(quote)
        if finder is None:
            finder = self.finders[quote] = V3Finder(asset=quote)
        if finder.state == "done" and finder.pool:
            entry = self.pools.get(finder.pool)
            return entry if entry is not None and entry.state == "ready" else None
        return None

    def _adopt_finders(self) -> None:
        for finder in self.finders.values():
            if finder.state == "done" and finder.pool and finder.pool not in self.pools:
                self.pools[finder.pool] = PoolEntry(
                    pool=finder.pool,
                    token=finder.asset,
                    kind="v3",
                    token_is0=int(finder.asset, 16) < int(finder.quote or "0x0", 16),
                    quote=finder.quote,
                    state="ready",
                )

    def _build(
        self, plan: list[dict[str, Any]]
    ) -> tuple[list[tuple[str, str]], dict[str, int], list[tuple[Any, int, int]], PoolEntry | None, dict | None]:
        calls: list[tuple[str, str]] = [(ARBSYS, SEL_ARB_BLOCK_NUMBER), (MULTICALL3, SEL_BLOCK_TIMESTAMP)]
        price_at: dict[str, int] = {}

        def add_price(entry: PoolEntry) -> None:
            if entry.pool not in price_at and len(calls) < MAX_SUBCALLS:
                price_at[entry.pool] = len(calls)
                calls.append(entry.price_call())

        wanted_decimals: list[str] = []
        pending: list[PoolEntry] = []
        finders: list[V3Finder] = []
        for item in plan:
            entry: PoolEntry | None = item["entry"]
            if entry is not None and entry.state == "ready":
                add_price(entry)
                ref = self._quote_ref(entry.quote)
                if ref is not None:
                    add_price(ref)
                    wanted_decimals.append(ref.token)
                elif entry.quote not in (None, ROBINHOOD_WETH, ROBINHOOD_USDG):
                    finders.append(self.finders[entry.quote])
                wanted_decimals += [entry.token, entry.quote]
            candidate: PoolEntry | None = item["pending"]
            if candidate is not None and candidate.state in ("key", "logs"):
                pending.append(candidate)
            elif candidate is None:
                finder = self.finders.get(item["target"].token)
                if finder is not None:
                    finders.append(finder)

        discovery: list[tuple[Any, int, int]] = []
        now = self._clock_ms()

        def add_discovery(resolver: Any, sub: list[tuple[str, str]]) -> None:
            if not sub or len(calls) + len(sub) > MAX_SUBCALLS:
                return
            discovery.append((resolver, len(calls), len(sub)))
            calls.extend(sub)

        seen: set[int] = set()
        for entry in pending:
            if id(entry) not in seen:
                seen.add(id(entry))
                add_discovery(entry, entry.discovery_calls())
        for finder in finders:
            if id(finder) not in seen:
                seen.add(id(finder))
                add_discovery(finder, finder.discovery_calls(now))
        for address in dict.fromkeys(a for a in wanted_decimals if a is not None):
            if address not in self.decimals:
                add_discovery(address, [(address, SEL_DECIMALS)])

        logs_entry, logs_params = None, None
        for entry in pending:
            params = entry.logs_params(self.latest_block)
            if params is not None:
                logs_entry, logs_params = entry, params
                break
        return calls, price_at, discovery, logs_entry, logs_params

    # ------------------------------------------------------------------ samples

    def _eth_usd(self, conn: Any, ts: int) -> tuple[Decimal | None, str]:
        try:
            from kaiba.providers.native_price import at as native_price_at

            found = native_price_at(Chain.ROBINHOOD, ts, conn)
        except Exception as exc:  # noqa: BLE001 - a missing sampler is a missing rate
            return None, f"eth_usd_unavailable:{type(exc).__name__}"
        if found.price_usd is None or found.price_usd <= 0:
            return None, "eth_usd_unavailable"
        return found.price_usd, "eth_usd:native_prices"

    def _samples(
        self,
        conn: Any,
        plan: list[dict[str, Any]],
        results: list[tuple[bool, bytes]],
        price_at: dict[str, int],
        received_ms: int,
        block: int | None,
        block_ts: int | None,
        rpc_ms: int,
    ) -> list[tuple[Any, ...]]:
        rows: list[tuple[Any, ...]] = []
        eth_usd: tuple[Decimal | None, str] | None = None
        for item in plan:
            entry: PoolEntry | None = item["entry"]
            target: Target = item["target"]
            if entry is None or entry.state != "ready" or entry.pool not in price_at:
                self.stats.skip(self._why_unsampled(item))
                continue
            ok, blob = results[price_at[entry.pool]]
            price_quote = entry.price_from(ok, blob, self.decimals)
            if price_quote is None:
                self.stats.skip("price_unreadable")
                continue
            quote_usd: Decimal | None
            if entry.quote in (None, ROBINHOOD_WETH):
                eth_usd = eth_usd or self._eth_usd(conn, received_ms)
                quote_usd, usd_source = eth_usd
            elif entry.quote == ROBINHOOD_USDG:
                quote_usd, usd_source = Decimal(1), "usdg_peg"
            else:
                ref = self._quote_ref(entry.quote)
                finder = self.finders.get(entry.quote or "")
                quote_usd, usd_source = None, (
                    f"quote_unpriced:{finder.reason}" if finder is not None and finder.state == "dead"
                    else "quote_ref_pending"
                )
                if ref is not None and ref.pool in price_at:
                    ok_r, blob_r = results[price_at[ref.pool]]
                    in_ref = ref.price_from(ok_r, blob_r, self.decimals)
                    if in_ref is not None and ref.quote == ROBINHOOD_USDG:
                        quote_usd, usd_source = in_ref, f"ref:{ref.pool}:usdg_peg"
                    elif in_ref is not None:
                        eth_usd = eth_usd or self._eth_usd(conn, received_ms)
                        if eth_usd[0] is not None:
                            quote_usd, usd_source = in_ref * eth_usd[0], f"ref:{ref.pool}:{eth_usd[1]}"
                        else:
                            usd_source = f"ref:{ref.pool}:{eth_usd[1]}"
            price_usd = price_quote * quote_usd if quote_usd is not None else None
            incumbent = item["incumbent"] or {}
            observed = incumbent.get("observed_ms")
            try:
                observed = int(observed) if observed is not None else None
            except (TypeError, ValueError):
                observed = None
            rows.append(
                (
                    received_ms,
                    target.position_id,
                    target.token,
                    entry.pool,
                    entry.kind,
                    entry.quote,
                    _sig(price_quote),
                    _sig(quote_usd),
                    _sig(price_usd),
                    f"onchain:{entry.kind}|{usd_source}",
                    block,
                    block_ts * 1000 if block_ts else None,
                    rpc_ms,
                    item["stop"],
                    incumbent.get("price_usd"),
                    incumbent.get("source"),
                    observed,
                    received_ms - observed if observed else None,
                    # The prefetch is the only per-tick view (a one-position book skips it);
                    # when it says this tick was blind, the incumbent above is older news.
                    "incumbent_blind_this_tick"
                    if target.cached is not None and not target.cached.get("usable", True)
                    else None,
                )
            )
        return rows

    def _why_unsampled(self, item: dict[str, Any]) -> str:
        pending: PoolEntry | None = item["pending"]
        if pending is None:
            finder = self.finders.get(item["target"].token)
            if finder is None:
                return "no_pool"
            return f"no_pool:{finder.reason or finder.state}"
        if pending.state == "ready":
            return "pool_resolved_this_tick"  # sampled from the next tick on
        return f"pool_{pending.state}:{pending.reason}" if pending.reason else f"pool_{pending.state}"

    def _write(self, conn: Any, rows: list[tuple[Any, ...]]) -> None:
        from kaiba.core.db import tx

        with tx(conn):
            conn.executemany(
                f"INSERT INTO {TABLE} (ts_ms, position_id, token, pool, pool_kind, quote_token, "
                "price_quote, quote_usd, price_usd, source, block_number, block_ts_ms, rpc_ms, "
                "stop_price_usd, incumbent_price_usd, incumbent_source, incumbent_observed_ms, "
                "incumbent_age_ms, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        self.stats.samples += len(rows)

    def _maybe_prune(self, conn: Any, job: Job, now: int) -> int:
        if self._last_prune_ms and now - self._last_prune_ms < PRUNE_EVERY_MS:
            return 0
        self._last_prune_ms = now
        cutoff = now - int(job.retention_days) * 86_400_000
        removed = 0
        from kaiba.core.db import tx

        for _ in range(PRUNE_MAX_CHUNKS):
            with tx(conn):
                cur = conn.execute(
                    f"DELETE FROM {TABLE} WHERE id IN (SELECT id FROM {TABLE} WHERE ts_ms < ? "
                    "ORDER BY ts_ms LIMIT ?)",
                    (cutoff, PRUNE_CHUNK),
                )
            removed += max(0, cur.rowcount or 0)
            if (cur.rowcount or 0) < PRUNE_CHUNK:
                break
        return removed

    def pool_summary(self) -> dict[str, Any]:
        states: dict[str, int] = {}
        dead: dict[str, int] = {}
        for entry in self.pools.values():
            states[entry.state] = states.get(entry.state, 0) + 1
            if entry.state == "dead":
                dead[entry.reason or "?"] = dead.get(entry.reason or "?", 0) + 1
        for finder in self.finders.values():
            if finder.state == "dead":
                dead[f"finder:{finder.reason}"] = dead.get(f"finder:{finder.reason}", 0) + 1
        return {"pool_states": states, "dead_reasons": dead, "kinds": sorted({e.kind for e in self.pools.values() if e.kind})}

    def _maybe_status(self, conn: Any) -> None:
        now = self._clock_ms()
        if self._last_status_ms and now - self._last_status_ms < STATUS_EVERY_MS:
            return
        self._last_status_ms = now
        try:
            from kaiba.core import events as ev
            from kaiba.core.schemas import EventKind

            ev.emit(
                EventKind.SYSTEM,
                {
                    "service": "onchain_price_log",
                    "event": "status",
                    "window_ms": STATUS_EVERY_MS,
                    **self.stats.payload(),
                    **self.pool_summary(),
                    "latest_block": self.latest_block,
                    "note": "log-only; decisions unchanged",
                },
                chain=Chain.ROBINHOOD,
                conn=conn,
            )
            self.stats.reset()
        except Exception as exc:  # noqa: BLE001 - telemetry must never break the reader
            log.debug("onchain price log status not written: %s", exc)


_LOGGER: OnchainPriceLog | None = None
_LOGGER_LOCK = threading.Lock()


def _singleton() -> OnchainPriceLog:
    global _LOGGER
    with _LOGGER_LOCK:
        if _LOGGER is None:
            _LOGGER = OnchainPriceLog()
        return _LOGGER


def after_tick(watchdog: Any, positions: Sequence[Any], cfg: Any) -> bool:
    """The protection loop's one hook. Never raises. Returns whether a read was queued.

    With ``protection.onchain_price_log`` off -- the shipped default -- this returns before
    doing anything: no thread, no database, no network, no limiter reservation.
    """
    if not bool(getattr(cfg, "onchain_price_log", False)):
        return False
    try:
        return _singleton().on_tick(watchdog, positions, cfg)
    except Exception as exc:  # noqa: BLE001 - a log-only reader never breaks a tick
        log.debug("onchain price log hook failed: %s", exc)
        return False


__all__ = [
    "MULTICALL3",
    "OnchainPriceLog",
    "PoolEntry",
    "RESERVE_PRIORITY",
    "V3Finder",
    "after_tick",
    "decode_aggregate3",
    "encode_aggregate3",
    "price_from_reserves",
    "price_from_sqrt",
    "v4_pool_id",
    "v4_state_slot",
]
