"""New launches, within a second of the block, for the ``launch-snipe`` lane.

Three chains, two transports, one record (:class:`Launch`):

* **Robinhood (Pons V2)** -- ``eth_subscribe("logs")`` over the Alchemy WebSocket, filtered
  to the factory address AND the ``TokenLaunched`` topic only. The Pons listener in
  ``ingest/robinhood.py`` polls the public RPC every 3 s (MEASURED p50 6.5 s, p90 11.0 s);
  the same push over Alchemy MEASURED 0.3-0.5 s after the block's whole-second timestamp
  from a Singapore box (2026-10-03, ~135 launches over ~5.5 h). One subscription, one
  topic: at ~25 launches an hour the notification bytes are a rounding error against the
  box's ~23M CU/month (``rpc-via-alchemy``), which is why it is the factory topic and not
  every curve. A dropped socket reconnects with backoff and BACKFILLS the gap with
  ``eth_getLogs`` over the same filter, exactly as ``alchemy_ws.stream`` does for wallets;
  the socket plumbing (session, backoff, dedupe, URL masking) is imported from there, not
  re-implemented.
* **Solana (pump.fun, LaunchLab via PumpPortal's ``pool``)** -- read from the ``tokens``
  table the PumpPortal listener already writes. PumpPortal allows ONE socket per IP
  (``pumpportal.ConnectionLock``; a second gets the IP banned for an hour), so this module
  never opens one: it tails the rows the ingest service persists, on a
  ``(first_seen_ms, address)`` watermark so two rows in one millisecond cannot skip each
  other.
* **BSC (Flap)** -- ``eth_subscribe("logs")`` over the Alchemy BSC WebSocket on the Flap
  portal, ``TokenCreated`` and ``LaunchedToDEX`` only (:data:`FLAP_FILTER`), through the
  same streamer as Pons (:func:`stream_flap`). MEASURED 2026-10-05: ~970 launches an hour
  (84-2,228, bursty), ~1.35 KB a ``TokenCreated`` push, ~54 CU each -- roughly 38M CU a
  month, so this socket is opened by ONE process, the ingest listener
  (``kaiba/ingest/flap.py``), which writes the ``tokens`` row. The snipe lane tails those
  rows (:func:`tail_bsc`) rather than paying for a second subscription. Nothing in a Flap
  event is indexed, so the filter cannot be narrowed by creator or token.

Nothing here writes, decides or trades. :func:`stream_pons` / :func:`stream_flap` yield;
:func:`tail_sol` / :func:`tail_bsc` return rows. The snipe lane decides what to do with them.

Secrets: the WebSocket URL carries the API key in its path. Every string that could carry
it goes through ``alchemy_ws.redact`` / ``mask_url`` before it is logged or returned.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kaiba.core.db import fetch_all, jload
from kaiba.core.schemas import Chain
from kaiba.execution.evm_price import FLAP_PORTAL
from kaiba.ingest import alchemy_ws as aws
from kaiba.ingest.pumpportal import LAUNCHPAD as PUMPPORTAL_LAUNCHPAD
from kaiba.ingest.robinhood import (
    FACTORY_V2,
    TOPIC_TOKEN_LAUNCHED,
    ZERO_ADDRESS,
    parse_token_launched,
)
from kaiba.ingest.robinhood import (
    LAUNCHPAD as PONS_LAUNCHPAD,
)

log = logging.getLogger(__name__)

#: The one subscription this module makes on Robinhood.
PONS_FILTER: dict[str, Any] = {"address": FACTORY_V2, "topics": [TOPIC_TOKEN_LAUNCHED]}

#: PumpPortal's ``pool`` value per venue (``pumpportal.parse_new_token`` keeps it in
#: ``meta.pool``). ``bonk`` is letsbonk.fun, which launches on Raydium LaunchLab -- the
#: venue GMGN calls ``ray_launchpad`` (``mooner-launchpad-edge``: lift 2.69 vs pump.fun 1.02).
SOL_VENUE_BY_POOL: dict[str, str] = {"pump": "pump.fun", "bonk": "launchlab"}

#: ``keccak("TokenCreated(uint256,address,uint256,address,string,string,string)")``, emitted
#: by the portal. MEASURED 2026-10-05: ONE topic (nothing indexed) and data words
#: ``ts, creator, nonce, token`` then three string offsets (name, symbol, meta URI). The
#: hash is checked against ``policy.keccak256`` in ``tests/test_snipe_bsc_flap.py``.
FLAP_TOPIC_TOKEN_CREATED = "0x504e7f360b2e5fe33cbaaae4c593bc55305328341bf79009e43e0e3b7f699603"
#: ``keccak("LaunchedToDEX(address,address,uint256,uint256)")``: graduation off the curve.
#: MEASURED 2026-10-05: emitted by the portal in 2 of 2 graduation receipts read; 120
#: graduations in 24 h. Its data layout (token, pool, amount, eth -- assumed non-indexed like
#: every other Flap event measured) is UNVERIFIED; :func:`flap_graduation_from_log` also
#: reads an indexed token if one is there.
FLAP_TOPIC_LAUNCHED_TO_DEX = "0x6e4f47630b8745b8cacbd44f42a8a33e7eea7cc08ef22fc7630f4f385784ff7d"
#: The one subscription made on BSC: the portal, creations and graduations (topic0 OR-list).
FLAP_FILTER: dict[str, Any] = {"address": FLAP_PORTAL,
                               "topics": [[FLAP_TOPIC_TOKEN_CREATED, FLAP_TOPIC_LAUNCHED_TO_DEX]]}
#: The launchpad label bsc ``tokens`` rows already carry for Flap (GMGN's own name for it).
FLAP_LAUNCHPAD = "flap"
#: ``meta.source`` on the rows the Flap listener writes. :func:`tail_bsc` hands on ONLY these:
#: GMGN's feeds also write ``launchpad='flap'`` rows, MEASURED 2026-10-05 a median 71 s after
#: creation, which is not a launch to snipe.
FLAP_SOURCE = "alchemy:ws:flap"

#: The setting naming the BSC endpoint DEDICATED to Flap sniping: the listener's WebSocket
#: and sender lookups (``ingest/flap.py``) and the snipe lane's portal reads and marks
#: (``snipe.bsc_rpc``). An Alchemy ``/v2/`` URL on its OWN app/key.
#:
#: WHY a separate setting (review 2026-10-05). ``BSC_RPC_URL`` is built from the owner's one
#: Alchemy key, which also serves protection's bsc and Robinhood price reads. The Flap feed
#: alone is ~38M CU a month and its volume is set by the chain, not by us; on a shared key an
#: exhausted quota or a throughput 429 would blind EXITS on two chains. So nothing here falls
#: back to ``BSC_RPC_URL``: unset means the feed and the bsc snipe reads stay idle. Sharing is
#: still possible, but only as a deliberate act (set this to the same URL). A second app
#: under the same Alchemy account separates throughput, not necessarily the account's monthly
#: cap -- the plan decides which, and the lead checks it before listing bsc.
#:
#: Read from the process environment, then the same ``.env`` files ``Settings`` reads
#: (``Settings`` has ``extra="ignore"``, so it drops an unknown key; a field there would be
#: the cleaner home, and ``kaiba/core/config.py`` is the lead's file).
BSC_SNIPE_RPC_ENV = "BSC_SNIPE_RPC_URL"


def bsc_snipe_rpc_url() -> str | None:
    """The dedicated BSC snipe endpoint (:data:`BSC_SNIPE_RPC_ENV`), or ``None`` when unset."""
    value = os.environ.get(BSC_SNIPE_RPC_ENV) or os.environ.get(BSC_SNIPE_RPC_ENV.lower())
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
                found = values.get(BSC_SNIPE_RPC_ENV) or found
        return found.strip() if found and found.strip() else None
    except Exception as exc:  # noqa: BLE001 - an unreadable setting is "not configured"
        log.debug("%s unreadable: %s", BSC_SNIPE_RPC_ENV, type(exc).__name__)
        return None


@dataclass(frozen=True, slots=True)
class Launch:
    """One launch, as the snipe lane needs it. Chain-agnostic on purpose."""

    chain: Chain
    token: str
    #: Where it launched: ``pons`` on Robinhood; ``pump.fun`` or ``launchlab`` on Solana;
    #: ``flap`` on BSC (whose quote token is NOT in the creation event: ``pair_token`` stays
    #: ``None`` there and the snipe lane reads the quote off the portal).
    venue: str
    #: The launchpad's own record of who launched it -- the key ``deployer_stats`` uses.
    #: On Pons this is the event's ``deployer`` (a bundle contract when one was used).
    creator: str | None
    #: Pons only: the bonding curve and the quote token (``ZERO_ADDRESS`` = ETH).
    curve: str | None = None
    pair_token: str | None = None
    graduation_threshold: int | None = None
    block: int | None = None
    #: The block's timestamp (whole seconds on Robinhood) or the source's create time.
    launched_ms: int | None = None
    tx: str | None = None
    name: str | None = None
    symbol: str | None = None
    received_ms: int = 0
    backfilled: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.chain.value}:{self.token}"

    @property
    def latency_ms(self) -> int | None:
        """Receive time minus launch time. On Robinhood it overstates by up to 1 s: block
        timestamps are whole seconds shared by ~10 blocks."""
        if self.launched_ms is None or not self.received_ms:
            return None
        return self.received_ms - self.launched_ms

    @property
    def quote_is_native(self) -> bool:
        return self.pair_token in (None, ZERO_ADDRESS)


# --------------------------------------------------------------------------------------
# Robinhood: Pons V2 launches over the Alchemy WebSocket
# --------------------------------------------------------------------------------------


def _hex_int(value: Any) -> int | None:
    try:
        return int(str(value), 16) if isinstance(value, str) else int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def launch_from_log(entry: Any, *, received_ms: int, backfilled: bool = False) -> Launch | None:
    """A ``TokenLaunched`` log (push or ``eth_getLogs``) -> :class:`Launch`, or ``None``.

    Parsing is ``robinhood.parse_token_launched``'s, so a launch reads the same here as in
    the poller. ``blockTimestamp`` is on every Alchemy Robinhood notification (MEASURED in
    ``alchemy_ws``); a backfilled ``eth_getLogs`` entry may lack it, and then the launch
    time is unknown rather than guessed.
    """
    if not isinstance(entry, Mapping) or entry.get("removed"):
        return None
    ts = _hex_int(entry.get("blockTimestamp"))
    ts_ms = ts * 1000 if ts is not None else None
    parsed = parse_token_launched(entry, ts_ms=ts_ms)
    if parsed is None:
        return None
    token, meta = parsed
    return Launch(
        chain=Chain.ROBINHOOD,
        token=token.address,
        venue=PONS_LAUNCHPAD,
        creator=meta.deployer,
        curve=meta.curve,
        pair_token=meta.pair_token,
        graduation_threshold=meta.graduation_threshold,
        block=meta.launched_block or None,
        launched_ms=ts_ms,
        tx=entry.get("transactionHash") if isinstance(entry.get("transactionHash"), str) else None,
        received_ms=received_ms,
        backfilled=backfilled,
        meta={"launch_config_id": meta.launch_config_id, "log_index": _hex_int(entry.get("logIndex"))},
    )


@dataclass(slots=True)
class LaunchFeedStats:
    connects: int = 0
    disconnects: int = 0
    launches: int = 0
    backfilled: int = 0
    duplicates: int = 0
    notification_bytes: int = 0
    get_logs_calls: int = 0
    last_launch_ms: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__slots__}  # type: ignore[attr-defined]


async def stream_pons(
    url: str,
    *,
    stop: asyncio.Event | None = None,
    connect: Callable[[str], Any] | None = None,
    stats: LaunchFeedStats | None = None,
    on_status: Callable[[dict[str, Any]], None] | None = None,
    max_attempts: int | None = None,
    backfill_max_blocks: int = 36_000,
    backfill_chunk_blocks: int = 9_000,
    idle_timeout_s: float = 900.0,
    poll_s: float = 1.0,
    dedupe_size: int = 20_000,
) -> AsyncIterator[Launch]:
    """Yield every Pons V2 launch until ``stop`` is set.

    ``url`` may be the https endpoint (converted by ``alchemy_ws.ws_url``). Reconnects on any
    failure with ``alchemy_ws.backoff_delay``; from the second connection on, backfills from
    the last launch block seen (inclusive; dedupe drops the overlap), at most
    ``backfill_max_blocks`` back (~1 h at ~10 blocks/s), reporting ``gap_truncated`` for
    anything older rather than skipping it silently. ``idle_timeout_s`` is long because
    launches can be minutes apart on a healthy socket; the WebSocket ping catches a dead
    TCP path. ``max_attempts`` bounds CONSECUTIVE failures, for tests.
    """
    async for launch in _stream_launch_logs(
        url, PONS_FILTER, launch_from_log, stop=stop, connect=connect, stats=stats, on_status=on_status,
        max_attempts=max_attempts, backfill_max_blocks=backfill_max_blocks,
        backfill_chunk_blocks=backfill_chunk_blocks, idle_timeout_s=idle_timeout_s, poll_s=poll_s,
        dedupe_size=dedupe_size,
    ):
        yield launch


async def _stream_launch_logs(
    url: str,
    log_filter: Mapping[str, Any],
    parse: Callable[..., Any],
    *,
    stop: asyncio.Event | None,
    connect: Callable[[str], Any] | None,
    stats: LaunchFeedStats | None,
    on_status: Callable[[dict[str, Any]], None] | None,
    max_attempts: int | None,
    backfill_max_blocks: int,
    backfill_chunk_blocks: int,
    idle_timeout_s: float,
    poll_s: float,
    dedupe_size: int,
    start_block: int | None = None,
) -> AsyncIterator[Any]:
    """One ``eth_subscribe("logs")`` on ``log_filter``, every log through ``parse``.

    The body :func:`stream_pons` always had, with the filter and the parser passed in so
    the Flap portal (:func:`stream_flap`) shares it rather than copying it.
    ``parse(entry, received_ms=, backfilled=)`` returns ``None`` for a log that is not one
    of ours, or an object with a ``block`` attribute (the backfill point).

    ``start_block``: a block the caller already processed up to (a persisted cursor). When
    given, the FIRST connection backfills from it exactly as a reconnect does (inclusive,
    capped at ``backfill_max_blocks``, ``gap_truncated`` reported); without it, as before,
    only a reconnect inside one process backfills. ``None`` keeps the old behaviour.
    """
    target = aws.ws_url(url)
    stop = stop or asyncio.Event()
    dial = connect or aws._default_connect  # noqa: SLF001 - shared stack, by design
    stats = stats or LaunchFeedStats()
    seen = aws._RecentKeys(dedupe_size)  # noqa: SLF001
    feed_stats = aws.FeedStats()
    last_block: int | None = int(start_block) if start_block is not None and int(start_block) > 0 else None
    attempt = 0
    log_filter = dict(log_filter)

    def _fresh(entry: Any, at_ms: int, backfilled: bool) -> Any:
        nonlocal last_block
        launch = parse(entry, received_ms=at_ms, backfilled=backfilled)
        if launch is None:
            return None
        key = f"{entry.get('transactionHash')}:{entry.get('logIndex')}"
        if not seen.add(key):
            stats.duplicates += 1
            return None
        if launch.block is not None:
            last_block = launch.block if last_block is None else max(last_block, launch.block)
        stats.launches += 1
        stats.last_launch_ms = at_ms
        if backfilled:
            stats.backfilled += 1
        return launch

    while not stop.is_set():
        if max_attempts is not None and attempt >= max_attempts:
            aws._status(on_status, "gave_up", target, attempts=attempt)  # noqa: SLF001
            break
        try:
            async with dial(target) as ws:
                session = aws._Session(ws, feed_stats, target)  # noqa: SLF001
                sub_id = await session.call("eth_subscribe", ["logs", log_filter])
                if not isinstance(sub_id, str):
                    raise aws.RpcError(f"eth_subscribe returned {sub_id!r}")
                stats.connects += 1
                attempt = 0
                aws._status(on_status, "subscribed", target, connects=stats.connects)  # noqa: SLF001

                if last_block is not None:
                    head = _hex_int(await session.call("eth_blockNumber", []))
                    if head is not None and head >= last_block:
                        start = last_block
                        if head - start > backfill_max_blocks:
                            aws._status(on_status, "gap_truncated", target, from_block=start, head=head,  # noqa: SLF001
                                        lost_blocks=head - backfill_max_blocks - start)
                            start = head - backfill_max_blocks
                        got = 0
                        step = max(1, int(backfill_chunk_blocks))
                        for lo in range(start, head + 1, step):
                            hi = min(head, lo + step - 1)
                            logs = await session.call("eth_getLogs", [{**log_filter, "fromBlock": hex(lo), "toBlock": hex(hi)}])
                            stats.get_logs_calls += 1
                            at = aws._now_ms()  # noqa: SLF001
                            for entry in logs or []:
                                launch = _fresh(entry, at, True)
                                if launch is not None:
                                    got += 1
                                    yield launch
                        aws._status(on_status, "backfilled", target, from_block=start, to_block=head, launches=got)  # noqa: SLF001

                last_frame = time.monotonic()
                while not stop.is_set():
                    item = await session.next_message(poll_s)
                    if item is None:
                        if time.monotonic() - last_frame > idle_timeout_s:
                            raise aws.StaleConnection(f"no frame for {idle_timeout_s:.0f}s")
                        continue
                    raw, at_ms = item
                    last_frame = time.monotonic()
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(msg, dict) or msg.get("method") != "eth_subscription":
                        continue
                    params = msg.get("params") or {}
                    if str(params.get("subscription")) != sub_id:
                        continue
                    stats.notification_bytes += len(raw if isinstance(raw, bytes) else raw.encode())
                    launch = _fresh(params.get("result"), at_ms, False)
                    if launch is not None:
                        yield launch
            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - every failure is a reconnect
            stats.disconnects += 1
            delay = aws.backoff_delay(attempt)
            attempt += 1
            aws._status(on_status, "disconnected", target, error=f"{type(exc).__name__}: {exc}",  # noqa: SLF001
                        reconnect_in_s=delay, attempt=attempt, last_block=last_block)
            await aws._wait(stop, delay)  # noqa: SLF001


# --------------------------------------------------------------------------------------
# BSC: Flap launches and graduations over the Alchemy WebSocket
# --------------------------------------------------------------------------------------


def _data_words(data: Any) -> list[int]:
    if not isinstance(data, str) or not data.startswith("0x"):
        return []
    body = data[2:]
    out: list[int] = []
    for i in range(0, len(body) - 63, 64):
        try:
            out.append(int(body[i:i + 64], 16))
        except ValueError:
            return out
    return out


def _addr(word: int | None) -> str | None:
    if word is None or word >> 160:
        return None  # an address word has nothing in its 12 high bytes
    return "0x" + format(word, "040x")


def _abi_string(data: str, offset_bytes: int, *, limit: int = 200) -> str | None:
    """A dynamic ``string`` out of ABI-encoded ``data`` at ``offset_bytes``. Never raises;
    a value off the end, or that is not UTF-8, is ``None``. Capped at ``limit`` characters:
    a launch's name is the deployer's free text, kept as data and nothing else."""
    try:
        body = bytes.fromhex(data[2:])
        start = int(offset_bytes)
        if start < 0 or start + 32 > len(body):
            return None
        length = int.from_bytes(body[start:start + 32], "big")
        raw = body[start + 32:start + 32 + length]
        if len(raw) != length:
            return None
        return raw.decode("utf-8")[:limit]
    except (ValueError, UnicodeDecodeError, TypeError):
        return None


def flap_launch_from_log(entry: Any, *, received_ms: int, backfilled: bool = False) -> Launch | None:
    """A Flap ``TokenCreated`` log -> :class:`Launch`, or ``None``.

    The launch time is the event's own ``ts`` word (word 0, the block's timestamp), so a
    backfilled log is timed as exactly as a pushed one. ``creator`` is the event's creator
    word: MEASURED 2026-10-05 it equals the transaction's sender on 26 of 40 launches (the
    rest are launcher contracts and Flap's VaultPortal). It is NOT always what the listener
    writes to ``tokens.creator``: when this word is a contract, every user of that launcher
    would pool into one "deployer", so ``ingest/flap.py`` resolves the transaction's sender
    and keys the row on it (the event's word kept as ``meta.event_creator``).
    """
    if not isinstance(entry, Mapping) or entry.get("removed"):
        return None
    topics = entry.get("topics")
    if not isinstance(topics, Sequence) or not topics or str(topics[0]).lower() != FLAP_TOPIC_TOKEN_CREATED:
        return None
    if str(entry.get("address") or "").lower() != FLAP_PORTAL:
        return None  # same signature from another contract is not a Flap launch
    data = entry.get("data")
    words = _data_words(data)
    if len(words) < 7:
        return None
    creator, token = _addr(words[1]), _addr(words[3])
    if token is None:
        return None
    ts = words[0]
    ts_ms = ts if ts > 10**12 else ts * 1000  # seconds on chain; tolerate a ms stamp
    tx = entry.get("transactionHash")
    return Launch(
        chain=Chain.BSC,
        token=token,
        venue=FLAP_LAUNCHPAD,
        creator=creator,
        block=_hex_int(entry.get("blockNumber")),
        launched_ms=ts_ms if ts > 0 else None,
        tx=tx if isinstance(tx, str) else None,
        name=_abi_string(data, words[4]),
        symbol=_abi_string(data, words[5]),
        received_ms=received_ms,
        backfilled=backfilled,
        meta={"log_index": _hex_int(entry.get("logIndex")), "nonce": words[2],
              "meta_uri": _abi_string(data, words[6]), "source": FLAP_SOURCE},
    )


@dataclass(frozen=True, slots=True)
class FlapGraduation:
    """A Flap token leaving its curve for a DEX pool (``LaunchedToDEX``)."""

    token: str
    pool: str | None
    block: int | None
    tx: str | None
    #: The block's timestamp when the push carried it, else when we received it.
    migrated_ms: int
    received_ms: int
    backfilled: bool = False


def flap_graduation_from_log(entry: Any, *, received_ms: int, backfilled: bool = False) -> FlapGraduation | None:
    """A ``LaunchedToDEX`` log -> :class:`FlapGraduation`, or ``None``. The token is read
    from an indexed topic when the log has one, else from data word 0 (the layout every
    other Flap event uses); the pool likewise."""
    if not isinstance(entry, Mapping) or entry.get("removed"):
        return None
    topics = entry.get("topics")
    if not isinstance(topics, Sequence) or not topics or str(topics[0]).lower() != FLAP_TOPIC_LAUNCHED_TO_DEX:
        return None
    if str(entry.get("address") or "").lower() != FLAP_PORTAL:
        return None
    words = _data_words(entry.get("data"))
    indexed = [a for a in (aws.topic_address(t) for t in list(topics)[1:]) if a]
    fields = indexed + [_addr(w) for w in words[:2]]  # indexed fields first, then the data words
    token = fields[0] if fields else None
    if token is None:
        return None
    ts = _hex_int(entry.get("blockTimestamp"))
    tx = entry.get("transactionHash")
    return FlapGraduation(token=token, pool=fields[1] if len(fields) > 1 else None,
                          block=_hex_int(entry.get("blockNumber")), tx=tx if isinstance(tx, str) else None,
                          migrated_ms=ts * 1000 if ts else received_ms, received_ms=received_ms,
                          backfilled=backfilled)


def flap_event_from_log(entry: Any, *, received_ms: int, backfilled: bool = False) -> Launch | FlapGraduation | None:
    """Either Flap event the subscription carries, parsed; ``None`` for anything else."""
    return (flap_launch_from_log(entry, received_ms=received_ms, backfilled=backfilled)
            or flap_graduation_from_log(entry, received_ms=received_ms, backfilled=backfilled))


async def stream_flap(
    url: str,
    *,
    stop: asyncio.Event | None = None,
    connect: Callable[[str], Any] | None = None,
    stats: LaunchFeedStats | None = None,
    on_status: Callable[[dict[str, Any]], None] | None = None,
    max_attempts: int | None = None,
    backfill_max_blocks: int = 40_000,
    backfill_chunk_blocks: int = 5_000,
    idle_timeout_s: float = 300.0,
    poll_s: float = 1.0,
    dedupe_size: int = 50_000,
    start_block: int | None = None,
) -> AsyncIterator[Launch | FlapGraduation]:
    """Yield every Flap launch (:class:`Launch`) and graduation (:class:`FlapGraduation`).

    The Pons streamer with BSC's numbers. MEASURED 2026-10-05: 0.45 s blocks, so the
    40,000-block backfill cap is ~5 h; a 5,000-block slice is ~37 min, ~600 launches,
    ~0.8 MB at the hour's rate (the socket allows 16 MiB). ``idle_timeout_s`` is 300 s
    because the quietest hour measured still carried 84 launches (one per ~43 s).

    ``start_block`` is the listener's persisted cursor (``ingest/flap.py``): with it the
    first connection after a restart backfills the downtime, so a graduation that landed
    while the process was down is still stamped (a held token reads as a rug otherwise).
    Downtime beyond ``backfill_max_blocks`` is still lost, and reported as ``gap_truncated``.
    """
    async for item in _stream_launch_logs(
        url, FLAP_FILTER, flap_event_from_log, stop=stop, connect=connect, stats=stats, on_status=on_status,
        max_attempts=max_attempts, backfill_max_blocks=backfill_max_blocks,
        backfill_chunk_blocks=backfill_chunk_blocks, idle_timeout_s=idle_timeout_s, poll_s=poll_s,
        dedupe_size=dedupe_size, start_block=start_block,
    ):
        yield item


@dataclass(slots=True)
class BscWatermark:
    """``created_ms`` of the newest Flap row handed out, plus what was handed out recently
    (a row is re-read for :data:`BSC_TAIL_OVERLAP_MS` and handed out once)."""

    created_ms: int
    seen: Any = field(default_factory=lambda: aws._RecentKeys(20_000))  # noqa: SLF001


#: How far back each :func:`tail_bsc` read reaches behind the watermark. Flap launch times
#: are whole block seconds and many launches share one, so a row written a moment after a
#: newer one must still be found; the overlap is re-read and deduplicated, never skipped.
BSC_TAIL_OVERLAP_MS = 10_000

#: :func:`tail_bsc`'s read. The unary ``+`` on ``chain`` and ``launchpad`` is load-bearing:
#: without it SQLite's planner picks ``idx_tokens_creator (chain=?)`` and walks every bsc
#: token ever stored, twice a second (checked with EXPLAIN QUERY PLAN; pinned by a test).
BSC_TAIL_SQL = (
    "SELECT address, symbol, name, creator, created_ms, first_seen_ms, meta_json FROM tokens "
    "WHERE created_ms >= ? AND +chain = ? AND +launchpad = ? ORDER BY created_ms ASC, address ASC LIMIT ?"
)


def bsc_launch_from_row(row: Mapping[str, Any], *, received_ms: int) -> Launch | None:
    """A ``tokens`` row the Flap listener wrote -> :class:`Launch`; ``None`` for any other
    ``launchpad='flap'`` row (GMGN's, which arrive a minute late)."""
    meta = jload(row.get("meta_json"), {}) or {}
    if meta.get("source") != FLAP_SOURCE:
        return None
    created = row.get("created_ms")
    return Launch(
        chain=Chain.BSC,
        token=str(row["address"]),
        venue=FLAP_LAUNCHPAD,
        creator=row.get("creator"),
        block=meta.get("block"),
        launched_ms=int(created) if created is not None else None,
        tx=meta.get("signature"),
        name=row.get("name"),
        symbol=row.get("symbol"),
        received_ms=received_ms,
        meta={"log_index": meta.get("log_index"), "first_seen_ms": row.get("first_seen_ms"), "source": FLAP_SOURCE},
    )


def tail_bsc(
    conn: sqlite3.Connection,
    mark: BscWatermark,
    *,
    limit: int = 500,
    now_ms: int | None = None,
) -> list[Launch]:
    """Flap launches the listener persisted since ``mark``; advances ``mark`` in place.

    Keyed on ``created_ms`` (``idx_tokens_created``), not ``first_seen_ms``, which has no
    index: ``tokens`` holds every chain's launches and a scan of it twice a second is what
    the index is for (:data:`BSC_TAIL_SQL`).
    """
    at = now_ms if now_ms is not None else aws._now_ms()  # noqa: SLF001
    rows = fetch_all(conn, BSC_TAIL_SQL,
                     (mark.created_ms - BSC_TAIL_OVERLAP_MS, Chain.BSC.value, FLAP_LAUNCHPAD, int(limit)))
    out: list[Launch] = []
    for r in rows:
        mark.created_ms = max(mark.created_ms, int(r["created_ms"]))
        if not mark.seen.add(str(r["address"])):
            continue
        launch = bsc_launch_from_row(r, received_ms=at)
        if launch is not None:
            out.append(launch)
    return out


def bsc_start_mark(*, lookback_ms: int = 0, now_ms: int | None = None) -> BscWatermark:
    """Start at "now", as :func:`sol_start_mark` does and for its reason."""
    at = now_ms if now_ms is not None else aws._now_ms()  # noqa: SLF001
    return BscWatermark(created_ms=at - max(0, int(lookback_ms)))


# --------------------------------------------------------------------------------------
# Solana: tail what the PumpPortal listener persists
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class SolWatermark:
    """``(first_seen_ms, address)`` of the last row handed out."""

    first_seen_ms: int
    address: str = ""


PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_P = 2**255 - 19
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + raw


def _b58encode(b: bytes) -> str:
    n, out = int.from_bytes(b, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


def _on_ed25519_curve(b: bytes) -> bool:
    """curve25519-dalek's ``CompressedEdwardsY::decompress`` succeeds: x^2 = (y^2-1)/(dy^2+1)
    has a root. (libsodium's ``is_valid_point`` also rejects small-order points, which is a
    different question and gives the wrong bump for some mints.)"""
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    return (x * x - x2) % _P == 0


def pump_curve_address(mint: str) -> str | None:
    """The pump.fun bonding curve of ``mint``: the program address ``["bonding-curve", mint]``
    under :data:`PUMP_PROGRAM`.

    MEASURED 2026-10-04 on 25 live creates: PumpPortal's ``bondingCurveKey`` was this address
    21 times, and the other 4 (all ``is_mayhem_mode``) named an EMPTY system account while
    this one held the curve. A 200-launch survey the same day could not read 17% of fresh
    curves from the frame's key, which is the box's 54 of ~327 'unpriced' sol observations.
    """
    import hashlib

    try:
        seed = _b58decode(mint)
    except ValueError:
        return None
    if len(seed) != 32:
        return None
    program = _b58decode(PUMP_PROGRAM)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"bonding-curve" + seed + bytes([bump]) + program + b"ProgramDerivedAddress").digest()
        if not _on_ed25519_curve(h):
            return _b58encode(h)
    return None  # pragma: no cover - a 256-bump miss does not happen


def sol_launch_from_row(row: Mapping[str, Any], *, received_ms: int) -> Launch:
    meta = jload(row.get("meta_json"), {}) or {}
    pool = str(meta.get("pool") or "pump").lower()
    created = row.get("created_ms") or meta.get("source_ms")
    # pump.fun: the curve is derived from the mint, never taken from the frame (see pump_curve_address)
    curve = (pump_curve_address(str(row["address"])) if pool == "pump" else None) or meta.get("bonding_curve")
    return Launch(
        chain=Chain.SOL,
        token=str(row["address"]),
        venue=SOL_VENUE_BY_POOL.get(pool, pool),
        creator=row.get("creator"),
        launched_ms=int(created) if created is not None else None,
        tx=meta.get("signature"),
        name=row.get("name"),
        symbol=row.get("symbol"),
        received_ms=received_ms,
        meta={
            "pool": pool,
            "bonding_curve": curve,
            "bonding_curve_frame": meta.get("bonding_curve"),
            "initial_buy_lamports": meta.get("initial_buy_lamports"),
            "initial_buy_tokens": meta.get("initial_buy_tokens"),
            "v_sol_in_curve": meta.get("v_sol_in_curve"),
            "v_tokens_in_curve": meta.get("v_tokens_in_curve"),
            "first_seen_ms": row.get("first_seen_ms"),
        },
    )


def tail_sol(
    conn: sqlite3.Connection,
    mark: SolWatermark,
    *,
    limit: int = 500,
    now_ms: int | None = None,
) -> list[Launch]:
    """Sol launches persisted since ``mark``; advances ``mark`` in place.

    Only rows the PumpPortal listener wrote (``launchpad`` = its label), oldest first. A
    row's ``received_ms`` is when THIS reader saw it, so a launch's latency includes the
    ingest service's own write delay -- the honest number for anything acting on it.
    """
    at = now_ms if now_ms is not None else aws._now_ms()  # noqa: SLF001
    rows = fetch_all(
        conn,
        "SELECT address, symbol, name, creator, created_ms, first_seen_ms, meta_json FROM tokens "
        "WHERE chain=? AND launchpad=? AND (first_seen_ms > ? OR (first_seen_ms = ? AND address > ?)) "
        "ORDER BY first_seen_ms ASC, address ASC LIMIT ?",
        (Chain.SOL.value, PUMPPORTAL_LAUNCHPAD, mark.first_seen_ms, mark.first_seen_ms, mark.address, int(limit)),
    )
    out = [sol_launch_from_row(r, received_ms=at) for r in rows]
    if rows:
        last = rows[-1]
        mark.first_seen_ms = int(last["first_seen_ms"])
        mark.address = str(last["address"])
    return out


def sol_start_mark(conn: sqlite3.Connection, *, lookback_ms: int = 0, now_ms: int | None = None) -> SolWatermark:
    """Start at "now" (less ``lookback_ms``): a launch from before the service started is
    not something to snipe, and replaying hours of rows would only burn the budgets."""
    at = now_ms if now_ms is not None else aws._now_ms()  # noqa: SLF001
    return SolWatermark(first_seen_ms=at - max(0, int(lookback_ms)))


__all__: Sequence[str] = (
    "PONS_FILTER", "SOL_VENUE_BY_POOL", "Launch", "LaunchFeedStats", "SolWatermark",
    "launch_from_log", "stream_pons", "sol_launch_from_row", "tail_sol", "sol_start_mark",
    "FLAP_FILTER", "FLAP_LAUNCHPAD", "FLAP_SOURCE", "FLAP_TOPIC_LAUNCHED_TO_DEX", "FLAP_TOPIC_TOKEN_CREATED",
    "BSC_TAIL_OVERLAP_MS", "BSC_TAIL_SQL", "BscWatermark", "FlapGraduation", "bsc_launch_from_row", "bsc_start_mark",
    "flap_event_from_log", "flap_graduation_from_log", "flap_launch_from_log", "stream_flap", "tail_bsc",
    "BSC_SNIPE_RPC_ENV", "bsc_snipe_rpc_url",
)
