"""Tracked-wallet token movements over an Alchemy WebSocket, seconds after the block.

Why this exists. Kaiba learns of a smart wallet's robinhood trade from two places today:
the GMGN ``track smartmoney`` sweep (MEASURED 2026-10-03 on the box: event minus block
time p50 44.6 s, p90 94.3 s over 1,875 rows) and the Pons curve poller in
``ingest/robinhood.py`` (p50 6.5 s, p90 11.0 s, but it sees Pons CURVE trades only, never a
graduated token on Uniswap v4). The copy-trade study found a top-wallet cohort loses at
60 s and 15 s of lag and only turns positive near 5 s. This module is the 5 s question:
``eth_subscribe("logs")`` on the ERC-20 ``Transfer`` topic, filtered to the tracked wallets
as ``to`` (tokens arriving) and as ``from`` (tokens leaving). One push per log; no polling.

What a Transfer can and cannot prove.

* A Transfer TO a wallet proves tokens arrived. It does not prove anyone paid. The
  operator's rule (``lanes.TRANSFER_IN_MARKERS``) is that a buy may be copied and a
  transfer-in may not, so every record keeps the raw movement (``side`` says only which
  way the tokens went) and :func:`fold_tx` states the BASIS on which a movement reads as a
  trade: a quote asset left the wallet in the same transaction (``paid_quote``), the
  counterparty is a known venue (``venue``), or nothing corroborates it
  (``transfer_only``). A native-ETH payment emits no Transfer log at all, so on a venue the
  caller has not named, a real native-ETH buy reads ``transfer_only``. That is the honest
  answer from logs alone, not a defect to paper over.
* A zero-amount Transfer is dropped. Address-poisoning spam emits exactly that, with a
  tracked wallet as ``from``, and it would otherwise read as a sell.
* ERC-721 shares the ``Transfer`` signature with ``tokenId`` as a fourth topic. Only the
  three-topic ERC-20 shape is decoded.
* Mints (``from`` is the zero address) and burns are kept and labelled, never read as
  trades.

Timing. Alchemy's Robinhood (Arbitrum Nitro) notifications carry ``blockTimestamp``
(MEASURED: present on every sampled log), so latency needs no second call. Block
timestamps are WHOLE SECONDS shared by ~10 blocks of ~100 ms, so ``latency_ms`` is
receive time minus the floor of the true block second: it overstates by up to 1 s and
cannot resolve below that. ``newHeads`` would not fix it (same field) and costs ~1.7 KB a
block at ~10 blocks/s.

Cost. Alchemy bills ``eth_subscribe`` 10 CU per call and every subscription notification
at 0.04 CU per byte (alchemy.com/docs/reference/compute-unit-costs, read 2026-10-03).
:class:`FeedStats` counts the bytes actually received so the estimate is measured, not
assumed. ``eth_getLogs`` (backfill) is 60 CU a call.

Disconnects. A websocket drop loses every log pushed while it was down. :func:`stream`
reconnects with capped, jittered exponential backoff, re-subscribes, and BACKFILLS the gap
with ``eth_getLogs`` from the last block it saw, over the same filters (MEASURED: a forced
drop resubscribed in 1.3 s). The subscription is
re-established before the backfill is read, so nothing falls between them, and a bounded
dedupe set stops the overlap from yielding twice. Backfilled records say so
(``backfilled=True``): their latency is the outage, not the feed.

Secrets. The endpoint URL carries the API key in its path. Nothing here logs, returns or
raises the URL unmasked: :func:`mask_url` and :func:`redact` are applied to every string
that could carry it.

This module writes nothing. :func:`stream` yields records; persisting them is the caller's
decision. It imports nothing from ``kaiba`` so it can run beside the tree, not inside it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: keccak256("Transfer(address,address,uint256)"), shared by ERC-20 and ERC-721.
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS = "0x" + "0" * 40

#: Alchemy published prices (compute-unit-costs page, read 2026-10-03).
CU_PER_SUBSCRIBE_CALL = 10
CU_PER_NOTIFICATION_BYTE = 0.04
CU_PER_GET_LOGS = 60

#: MEASURED 2026-10-03: a 5,000-entry topic OR-list was accepted by the Robinhood endpoint.
#: Chunks stay well under that so one rejected filter cannot blind every wallet at once.
DEFAULT_CHUNK_SIZE = 500

#: Directions. ``in`` = wallet is ``to`` (topic 2), ``out`` = wallet is ``from`` (topic 1).
DIRECTION_IN = "in"
DIRECTION_OUT = "out"
DIRECTIONS: tuple[str, ...] = (DIRECTION_IN, DIRECTION_OUT)

#: Record kinds. Only ``transfer`` can ever become a trade in :func:`fold_tx`.
KIND_TRANSFER = "transfer"
KIND_QUOTE = "quote"
KIND_MINT = "mint"
KIND_BURN = "burn"

#: Bases :func:`fold_tx` can state for a trade.
BASIS_PAID_QUOTE = "paid_quote"
BASIS_RECEIVED_QUOTE = "received_quote"
BASIS_VENUE = "venue"
BASIS_TRANSFER_ONLY = "transfer_only"
BASIS_MINT = "mint"

_HEX40 = re.compile(r"^0x[0-9a-f]{40}$")
_HEX64 = re.compile(r"^0x[0-9a-f]{64}$")


# --------------------------------------------------------------------------------------
# secrets
# --------------------------------------------------------------------------------------


def mask_url(url: str) -> str:
    """The endpoint with every path segment after ``/v2/`` (and any query) masked.

    A URL without ``/v2/`` keeps only scheme and host: an unknown shape could carry the key
    anywhere, and an over-masked log line costs nothing.
    """
    text = str(url or "")
    m = re.match(r"^([a-z]+://[^/?#]+)(.*)$", text, flags=re.IGNORECASE)
    if not m:
        return "***"
    host, rest = m.group(1), m.group(2)
    if "/v2/" in rest:
        return host + rest.split("/v2/", 1)[0] + "/v2/***"
    return host + ("/***" if rest else "")


def redact(text: Any, url: str | None = None) -> str:
    """``text`` with any ``/v2/<key>`` segment, and the literal key from ``url``, masked."""
    out = re.sub(r"/v2/[^\s/'\"?#]+", "/v2/***", str(text))
    if url and "/v2/" in url:
        key = url.split("/v2/", 1)[1].split("?", 1)[0].strip("/")
        if key:
            out = out.replace(key, "***")
    return out


def ws_url(http_url: str) -> str:
    """``https://…`` -> ``wss://…`` (and ``http`` -> ``ws``). Raises on anything else."""
    text = str(http_url or "").strip()
    low = text.lower()
    if low.startswith("wss://") or low.startswith("ws://"):
        return text
    if low.startswith("https://"):
        return "wss://" + text[len("https://"):]
    if low.startswith("http://"):
        return "ws://" + text[len("http://"):]
    raise ValueError(f"not an http(s)/ws(s) endpoint: {mask_url(text)}")


# --------------------------------------------------------------------------------------
# addresses and topics
# --------------------------------------------------------------------------------------


def normalize_address(address: Any) -> str | None:
    """Lowercase 0x address, or ``None`` when it is not one."""
    if not isinstance(address, str):
        return None
    low = address.strip().lower()
    return low if _HEX40.match(low) else None


def address_topic(address: str) -> str:
    """An address as a 32-byte indexed-topic word. Raises on a non-address."""
    addr = normalize_address(address)
    if addr is None:
        raise ValueError(f"not an EVM address: {address!r}")
    return "0x" + "0" * 24 + addr[2:]


def topic_address(topic: Any) -> str | None:
    """The address in an indexed topic word, or ``None`` if the word is not one.

    The 12 high bytes must be zero: a word with anything there is not an address, and
    reading its low 20 bytes anyway would attribute a movement to a wallet that never made it.
    """
    if not isinstance(topic, str):
        return None
    low = topic.strip().lower()
    if not _HEX64.match(low) or low[2:26] != "0" * 24:
        return None
    return "0x" + low[26:]


def _hex_int(value: Any) -> int | None:
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


# --------------------------------------------------------------------------------------
# subscriptions
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Subscription:
    """One ``eth_subscribe("logs")`` filter: one direction over one chunk of wallets."""

    key: str
    direction: str
    wallets: tuple[str, ...]

    def topics(self) -> list[Any]:
        words = [address_topic(w) for w in self.wallets]
        if self.direction == DIRECTION_IN:
            return [TRANSFER_TOPIC, None, words]
        if self.direction == DIRECTION_OUT:
            return [TRANSFER_TOPIC, words]
        raise ValueError(f"unknown direction {self.direction!r}")

    def subscribe_params(self) -> list[Any]:
        return ["logs", {"topics": self.topics()}]

    def logs_filter(self, from_block: int, to_block: int) -> dict[str, Any]:
        """The same filter as an ``eth_getLogs`` object, for backfilling a gap."""
        return {"fromBlock": hex(int(from_block)), "toBlock": hex(int(to_block)), "topics": self.topics()}


def normalize_wallets(wallets: Iterable[Any]) -> tuple[str, ...]:
    """Lowercased, de-duplicated, sorted; anything that is not an address is dropped."""
    return tuple(sorted({a for a in (normalize_address(w) for w in wallets) if a}))


def build_subscriptions(
    wallets: Iterable[Any],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    directions: Sequence[str] = DIRECTIONS,
) -> list[Subscription]:
    """Split the wallets into ``chunk_size`` OR-lists, one subscription per direction each."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    for d in directions:
        if d not in DIRECTIONS:
            raise ValueError(f"unknown direction {d!r}")
    ws = normalize_wallets(wallets)
    subs: list[Subscription] = []
    for i, start in enumerate(range(0, len(ws), chunk_size)):
        chunk = ws[start:start + chunk_size]
        for d in directions:
            subs.append(Subscription(key=f"{d}:{i}", direction=d, wallets=chunk))
    return subs


def rpc_request(request_id: int, method: str, params: Sequence[Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": int(request_id), "method": method, "params": list(params)}


# --------------------------------------------------------------------------------------
# decoding
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WalletTransfer:
    """One ERC-20 movement into or out of a tracked wallet, stamped on arrival.

    ``side`` is DIRECTION ONLY: ``buy`` = tokens arrived, ``sell`` = tokens left. Whether
    that was a trade is :func:`fold_tx`'s question; see the module doc.
    """

    chain: str
    wallet: str
    token: str
    side: str
    kind: str
    amount_atoms: int
    counterparty: str
    tx: str
    log_index: int
    block_number: int
    block_ts_ms: int | None
    recv_ms: int
    latency_ms: int | None
    removed: bool = False
    backfilled: bool = False
    subscription: str | None = None

    @property
    def key(self) -> tuple[str, int, str, str, bool]:
        return (self.tx, self.log_index, self.wallet, self.side, self.removed)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["amount_atoms"] = str(self.amount_atoms)  # uint256 does not survive a JSON float
        return d


def decode_transfer(
    entry: Any,
    *,
    wallets: frozenset[str] | set[str],
    recv_ms: int,
    quote_assets: frozenset[str] | set[str] = frozenset(),
    chain: str = "robinhood",
    subscription: str | None = None,
    backfilled: bool = False,
) -> list[WalletTransfer]:
    """A raw log -> zero, one or two :class:`WalletTransfer` records.

    Two when both ends are tracked wallets (one ``sell`` for the sender, one ``buy`` for the
    receiver). Empty for anything that is not a well-formed, non-zero ERC-20 Transfer
    touching a tracked wallet. Never raises on input shape.
    """
    if not isinstance(entry, Mapping):
        return []
    topics = entry.get("topics")
    if not isinstance(topics, Sequence) or isinstance(topics, str) or len(topics) != 3:
        return []
    if str(topics[0]).lower() != TRANSFER_TOPIC:
        return []
    sender = topic_address(topics[1])
    receiver = topic_address(topics[2])
    token = normalize_address(entry.get("address"))
    if sender is None or receiver is None or token is None:
        return []
    data = entry.get("data")
    if not isinstance(data, str) or not data.startswith("0x") or len(data) < 66:
        return []
    try:
        amount = int(data[2:66], 16)
    except ValueError:
        return []
    if amount <= 0:
        return []  # zero-value address-poisoning spam; never a trade
    tx = entry.get("transactionHash")
    block = _hex_int(entry.get("blockNumber"))
    log_index = _hex_int(entry.get("logIndex"))
    if not isinstance(tx, str) or block is None or log_index is None:
        return []
    ts = _hex_int(entry.get("blockTimestamp"))
    block_ts_ms = ts * 1000 if ts is not None and ts > 0 else None  # 0x0 is absent, not 1970
    latency = recv_ms - block_ts_ms if block_ts_ms is not None else None
    removed = bool(entry.get("removed", False))
    is_quote = token in quote_assets

    out: list[WalletTransfer] = []
    for wallet, side, counterparty in (
        (sender, "sell", receiver),
        (receiver, "buy", sender),
    ):
        if wallet not in wallets:
            continue
        # Quote first: a quote asset minted/burned against the wallet is a wrap/unwrap of
        # the native coin (MEASURED: WETH-style mints precede the payment leg in the same
        # tx), and it must stay visible as a QUOTE leg so fold_tx can discount it.
        if is_quote:
            kind = KIND_QUOTE
        elif counterparty == ZERO_ADDRESS:
            kind = KIND_MINT if side == "buy" else KIND_BURN
        else:
            kind = KIND_TRANSFER
        out.append(
            WalletTransfer(
                chain=chain,
                wallet=wallet,
                token=token,
                side=side,
                kind=kind,
                amount_atoms=amount,
                counterparty=counterparty,
                tx=tx.lower(),
                log_index=log_index,
                block_number=block,
                block_ts_ms=block_ts_ms,
                recv_ms=int(recv_ms),
                latency_ms=latency,
                removed=removed,
                backfilled=backfilled,
                subscription=subscription,
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class TxTrade:
    """A wallet's NET movement in one token within one transaction, with its basis."""

    chain: str
    tx: str
    wallet: str
    token: str
    side: str
    net_atoms: int
    basis: str
    quote_token: str | None
    quote_atoms: int | None
    block_number: int
    block_ts_ms: int | None
    first_recv_ms: int
    latency_ms: int | None

    @property
    def copyable(self) -> bool:
        """Only a buy that something besides the Transfer itself corroborates."""
        return self.side == "buy" and self.basis in {BASIS_PAID_QUOTE, BASIS_VENUE}


def fold_tx(
    records: Iterable[WalletTransfer],
    *,
    venues: frozenset[str] | set[str] = frozenset(),
) -> list[TxTrade]:
    """Net each wallet's legs per transaction and say on what basis each reads as a trade.

    * Quote-asset legs (``kind == quote``) are the payment, never a trade of their own. A
      quote leg against the zero address is a wrap/unwrap of the native coin and is
      ignored, so "wrap ETH, pay WETH" reads as paid and "sell for WETH, unwrap" as received.
    * A token in and out in the same tx (routing through the wallet) nets to nothing.
    * ``paid_quote`` / ``received_quote``: a quote asset moved the opposite way in the tx.
    * ``venue``: the token leg's counterparty is a caller-named venue (pool, curve, router).
    * ``mint``: the tokens came from the zero address.
    * ``transfer_only``: nothing corroborates a trade. Native-ETH buys land here unless the
      venue is named, because native value emits no log.
    Removed (reorged) records are excluded.
    """
    groups: dict[tuple[str, str], list[WalletTransfer]] = {}
    for r in records:
        if r.removed:
            continue
        groups.setdefault((r.tx, r.wallet), []).append(r)

    trades: list[TxTrade] = []
    for (tx, wallet), legs in groups.items():
        quote_net: dict[str, int] = {}
        token_net: dict[str, int] = {}
        token_legs: dict[str, list[WalletTransfer]] = {}
        for leg in legs:
            signed = leg.amount_atoms if leg.side == "buy" else -leg.amount_atoms
            if leg.kind == KIND_QUOTE:
                if leg.counterparty != ZERO_ADDRESS:  # wrap/unwrap is not a payment
                    quote_net[leg.token] = quote_net.get(leg.token, 0) + signed
            else:
                token_net[leg.token] = token_net.get(leg.token, 0) + signed
                token_legs.setdefault(leg.token, []).append(leg)
        paid = [(q, -n) for q, n in quote_net.items() if n < 0]
        received = [(q, n) for q, n in quote_net.items() if n > 0]
        for token, net in token_net.items():
            if net == 0:
                continue
            these = token_legs[token]
            side = "buy" if net > 0 else "sell"
            moving = [x for x in these if x.side == side]
            quote_token: str | None = None
            quote_atoms: int | None = None
            if any(x.kind == KIND_MINT for x in moving):
                basis = BASIS_MINT
            elif side == "buy" and paid:
                basis = BASIS_PAID_QUOTE
                quote_token, quote_atoms = max(paid, key=lambda p: p[1])
            elif side == "sell" and received:
                basis = BASIS_RECEIVED_QUOTE
                quote_token, quote_atoms = max(received, key=lambda p: p[1])
            elif any(x.counterparty in venues for x in moving):
                basis = BASIS_VENUE
            else:
                basis = BASIS_TRANSFER_ONLY
            first = min(these, key=lambda x: x.recv_ms)
            trades.append(
                TxTrade(
                    chain=first.chain,
                    tx=tx,
                    wallet=wallet,
                    token=token,
                    side=side,
                    net_atoms=abs(net),
                    basis=basis,
                    quote_token=quote_token,
                    quote_atoms=quote_atoms,
                    block_number=first.block_number,
                    block_ts_ms=first.block_ts_ms,
                    first_recv_ms=first.recv_ms,
                    latency_ms=first.latency_ms,
                )
            )
    return trades


# --------------------------------------------------------------------------------------
# bookkeeping
# --------------------------------------------------------------------------------------


@dataclass
class FeedStats:
    """What the connection cost and how it behaved. Bytes are counted as received."""

    connects: int = 0
    disconnects: int = 0
    subscribe_calls: int = 0
    get_logs_calls: int = 0
    other_calls: int = 0
    notifications: int = 0
    notification_bytes: int = 0
    records: int = 0
    duplicates: int = 0
    undecoded: int = 0
    backfilled: int = 0
    unknown_subscription: int = 0
    per_subscription: dict[str, int] = field(default_factory=dict)

    def estimated_cu(self) -> float:
        """Alchemy CU implied by what was actually received and called.

        The getLogs response bytes are not billed per byte (it is a flat 60 CU call), so
        they are not in ``notification_bytes``; ``eth_blockNumber`` is counted at 10 CU
        (the published price for it), which is a rounding error here either way.
        """
        return (
            self.subscribe_calls * CU_PER_SUBSCRIBE_CALL
            + self.notification_bytes * CU_PER_NOTIFICATION_BYTE
            + self.get_logs_calls * CU_PER_GET_LOGS
            + self.other_calls * 10
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["estimated_cu"] = round(self.estimated_cu(), 1)
        return d


class _RecentKeys:
    """Bounded insertion-ordered set, for dropping the backfill/subscription overlap."""

    def __init__(self, maxlen: int = 50_000) -> None:
        self._maxlen = maxlen
        self._keys: OrderedDict[Any, None] = OrderedDict()

    def add(self, key: Any) -> bool:
        """True if the key is new."""
        if key in self._keys:
            return False
        self._keys[key] = None
        while len(self._keys) > self._maxlen:
            self._keys.popitem(last=False)
        return True


# --------------------------------------------------------------------------------------
# reconnect schedule
# --------------------------------------------------------------------------------------


def _rand() -> float:
    """Indirection so the reconnect schedule is deterministic under test."""
    return random.random()


def backoff_delay(
    attempt: int,
    *,
    base: float = 1.0,
    cap: float = 60.0,
    jitter: float = 0.25,
    rand: Callable[[], float] | None = None,
) -> float:
    """Exponential backoff with additive-upward jitter, hard-capped at ``cap`` seconds.

    Same shape as ``pumpportal.backoff_delay`` (copied, not imported, so this module has no
    ``kaiba`` imports): the first retry stays prompt and no sleep exceeds the cap.
    """
    raw = min(cap, base * (2 ** min(max(attempt, 0), 16)))
    return round(min(cap, raw * (1.0 + jitter * (rand or _rand)())), 3)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    if seconds <= 0:
        return
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class RpcError(RuntimeError):
    """The endpoint answered a request with a JSON-RPC error."""


class StaleConnection(RuntimeError):
    """No frame at all for longer than ``idle_timeout_s``."""


def _default_connect(url: str) -> Any:
    import websockets  # lazy: importable without the extra

    # 16 MiB: a backfill eth_getLogs answer can exceed the 1 MiB default.
    return websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=16 * 2**20, max_queue=4096)


class _Session:
    """One live socket: request/response multiplexed with subscription pushes.

    A request reads frames until its own id answers; any push that arrives meanwhile is
    queued, not dropped, and handed out by :meth:`next_message` before new frames.
    """

    def __init__(self, ws: Any, stats: FeedStats, url: str, *, call_timeout_s: float = 30.0) -> None:
        self.ws = ws
        self.stats = stats
        self.url = url
        self.call_timeout_s = call_timeout_s
        self._next_id = 1
        self.queued: deque[tuple[str | bytes, int]] = deque()

    async def _recv(self, timeout: float) -> tuple[str | bytes, int]:
        raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
        return raw, _now_ms()

    async def call(self, method: str, params: Sequence[Any]) -> Any:
        rid = self._next_id
        self._next_id += 1
        await self.ws.send(json.dumps(rpc_request(rid, method, params)))
        deadline = time.monotonic() + self.call_timeout_s
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"{method} unanswered after {self.call_timeout_s}s")
            raw, at = await self._recv(left)
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                self.stats.undecoded += 1
                continue
            if isinstance(msg, dict) and msg.get("id") == rid:
                if "error" in msg:
                    raise RpcError(redact(f"{method}: {msg['error']}", self.url))
                return msg.get("result")
            self.queued.append((raw, at))

    async def next_message(self, timeout: float) -> tuple[str | bytes, int] | None:
        if self.queued:
            return self.queued.popleft()
        try:
            return await self._recv(timeout)
        except TimeoutError:
            return None


def _status(on_status: Callable[[dict[str, Any]], None] | None, event: str, url: str, **fields: Any) -> None:
    payload = {"event": event, "at_ms": _now_ms(), **{k: redact(v, url) if isinstance(v, str) else v
                                                     for k, v in fields.items()}}
    log.info("alchemy_ws %s %s", event, {k: v for k, v in payload.items() if k != "event"})
    if on_status is not None:
        try:
            on_status(payload)
        except Exception as exc:  # noqa: BLE001 - a status sink never stops the stream
            log.debug("status sink failed: %s", exc)


async def stream(
    url: str,
    wallets: Iterable[Any],
    *,
    chain: str = "robinhood",
    quote_assets: Iterable[str] = (),
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    directions: Sequence[str] = DIRECTIONS,
    stop: asyncio.Event | None = None,
    connect: Callable[[str], Any] | None = None,
    stats: FeedStats | None = None,
    on_status: Callable[[dict[str, Any]], None] | None = None,
    max_attempts: int | None = None,
    backfill_max_blocks: int = 200_000,
    backfill_chunk_blocks: int = 20_000,
    idle_timeout_s: float = 600.0,
    poll_s: float = 1.0,
    dedupe_size: int = 50_000,
    from_block: int | None = None,
    anchor_on_subscribe: bool = False,
) -> AsyncIterator[WalletTransfer]:
    """Yield :class:`WalletTransfer` records for the tracked wallets until ``stop`` is set.

    ``url`` may be the https endpoint; it is converted with :func:`ws_url`. Each connection
    subscribes every filter from :func:`build_subscriptions`, then (from the second
    connection on) backfills from the last block seen, then reads pushes. Any failure --
    dial, subscribe rejection, socket drop, ``idle_timeout_s`` with no frame at all -- is a
    reconnect after :func:`backoff_delay`. ``max_attempts`` bounds CONSECUTIVE failures
    (a successful subscribe resets it), so a dead endpoint ends the stream instead of
    retrying forever under test.

    Backfill runs from the last block that carried a tracked record (inclusive) to the head,
    in ``backfill_chunk_blocks`` slices, at most ``backfill_max_blocks`` back (~5.5 h at
    Robinhood's ~10 blocks/s); anything older is reported as ``gap_truncated``, never
    silently skipped. MEASURED 2026-10-03 on this endpoint: one ``eth_getLogs`` over
    100,000 blocks for 400 wallets answered in 2.1 s with 4,626 logs (2.9 MB), so the
    slices are for response size, not for a range cap.

    ``idle_timeout_s`` is deliberately long: a filter of a few hundred wallets can be quiet
    for minutes on a healthy socket, and the websocket ping (20 s) already catches a dead
    TCP path. The idle check catches the remaining case, a socket that pongs but whose
    subscriptions have silently gone away.

    ``from_block`` (ADDED 2026-10-04, default ``None`` = unchanged behaviour) makes the
    FIRST connection backfill from that block as well, exactly as a reconnect would. A
    caller that replaces one stream with another (a new wallet set) passes the head it
    read before stopping the old one, so the switch leaves no gap; the overlap is
    duplicates, which the caller dedupes by transaction.

    ``anchor_on_subscribe`` (ADDED 2026-10-04, default ``False`` = unchanged behaviour)
    reads the head after every successful subscribe and moves the backfill point up to
    it. Without it the backfill point is the last block that CARRIED a record, so a small,
    quiet wallet set that has produced nothing yet reconnects with no backfill at all.
    MEASURED 2026-10-04 on the box with 15 wallets: the socket went 600 s without a frame,
    ``StaleConnection`` fired, and the reconnect could not backfill the ~1.3 s it was down.
    One ``eth_blockNumber`` (10 CU) per connection closes that.
    """
    target = ws_url(url)
    stop = stop or asyncio.Event()
    dial = connect or _default_connect
    stats = stats or FeedStats()
    watch = frozenset(normalize_wallets(wallets))
    quotes = frozenset(a for a in (normalize_address(q) for q in quote_assets) if a)
    subs = build_subscriptions(watch, chunk_size=chunk_size, directions=directions)
    if not subs:
        raise ValueError("no valid wallet addresses to track")
    seen = _RecentKeys(dedupe_size)
    last_block: int | None = int(from_block) if from_block is not None else None
    attempt = 0

    def _decode(entry: Any, at_ms: int, sub_key: str | None, backfilled: bool) -> list[WalletTransfer]:
        nonlocal last_block
        recs = decode_transfer(entry, wallets=watch, recv_ms=at_ms, quote_assets=quotes,
                               chain=chain, subscription=sub_key, backfilled=backfilled)
        fresh: list[WalletTransfer] = []
        for rec in recs:
            if not seen.add(rec.key):
                stats.duplicates += 1
                continue
            fresh.append(rec)
            if not rec.removed:
                last_block = rec.block_number if last_block is None else max(last_block, rec.block_number)
        stats.records += len(fresh)
        if backfilled:
            stats.backfilled += len(fresh)
        return fresh

    while not stop.is_set():
        if max_attempts is not None and attempt >= max_attempts:
            _status(on_status, "gave_up", target, attempts=attempt)
            break
        try:
            async with dial(target) as ws:
                session = _Session(ws, stats, target)
                by_id: dict[str, Subscription] = {}
                for sub in subs:
                    sub_id = await session.call("eth_subscribe", sub.subscribe_params())
                    stats.subscribe_calls += 1
                    if not isinstance(sub_id, str):
                        raise RpcError(f"eth_subscribe returned {sub_id!r}")
                    by_id[sub_id] = sub
                stats.connects += 1
                attempt = 0
                _status(on_status, "subscribed", target, subscriptions=len(by_id),
                        wallets=len(watch), connects=stats.connects)

                head: int | None = None
                if last_block is not None:
                    head_hex = await session.call("eth_blockNumber", [])
                    stats.other_calls += 1
                    head = _hex_int(head_hex)
                    if head is not None and head >= last_block:
                        start = last_block  # inclusive: a block can hold logs we had not seen yet
                        if head - start > backfill_max_blocks:
                            _status(on_status, "gap_truncated", target, from_block=start, head=head,
                                    lost_blocks=head - backfill_max_blocks - start)
                            start = head - backfill_max_blocks
                        got = 0
                        step = max(1, int(backfill_chunk_blocks))
                        for lo in range(start, head + 1, step):
                            hi = min(head, lo + step - 1)
                            for sub in subs:
                                logs = await session.call("eth_getLogs", [sub.logs_filter(lo, hi)])
                                stats.get_logs_calls += 1
                                at = _now_ms()
                                for entry in logs or []:
                                    for rec in _decode(entry, at, sub.key, True):
                                        got += 1
                                        yield rec
                        _status(on_status, "backfilled", target, from_block=start, to_block=head,
                                blocks=head - start + 1, records=got)

                if anchor_on_subscribe:
                    if head is None:
                        head = _hex_int(await session.call("eth_blockNumber", []))
                        stats.other_calls += 1
                    if head is not None:
                        # Everything up to the head is now either backfilled or pushed.
                        last_block = head if last_block is None else max(last_block, head)

                last_frame = time.monotonic()
                while not stop.is_set():
                    item = await session.next_message(poll_s)
                    if item is None:
                        if time.monotonic() - last_frame > idle_timeout_s:
                            raise StaleConnection(f"no frame for {idle_timeout_s:.0f}s")
                        continue
                    raw, at_ms = item
                    last_frame = time.monotonic()
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        stats.undecoded += 1
                        continue
                    if not isinstance(msg, dict) or msg.get("method") != "eth_subscription":
                        continue
                    params = msg.get("params") or {}
                    sub = by_id.get(str(params.get("subscription")))
                    if sub is None:
                        stats.unknown_subscription += 1
                        continue
                    stats.notifications += 1
                    stats.notification_bytes += len(raw if isinstance(raw, bytes) else raw.encode())
                    stats.per_subscription[sub.key] = stats.per_subscription.get(sub.key, 0) + 1
                    for rec in _decode(params.get("result"), at_ms, sub.key, False):
                        yield rec
            # The read loop only exits on ``stop``; a socket closed by either side raises
            # (ConnectionClosed*) and lands in the reconnect branch below.
            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - every failure is a reconnect
            stats.disconnects += 1
            delay = backoff_delay(attempt)
            attempt += 1
            _status(on_status, "disconnected", target, error=f"{type(exc).__name__}: {exc}",
                    reconnect_in_s=delay, attempt=attempt, last_block=last_block)
            await _wait(stop, delay)
