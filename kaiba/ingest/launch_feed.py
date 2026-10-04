"""New launches, within a second of the block, for the ``launch-snipe`` lane.

Two chains, two transports, one record (:class:`Launch`):

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

Nothing here writes, decides or trades. :func:`stream_pons` yields; :func:`tail_sol`
returns rows. The snipe lane decides what to do with them.

Secrets: the WebSocket URL carries the API key in its path. Every string that could carry
it goes through ``alchemy_ws.redact`` / ``mask_url`` before it is logged or returned.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from kaiba.core.db import fetch_all, jload
from kaiba.core.schemas import Chain
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


@dataclass(frozen=True, slots=True)
class Launch:
    """One launch, as the snipe lane needs it. Chain-agnostic on purpose."""

    chain: Chain
    token: str
    #: Where it launched: ``pons`` on Robinhood; ``pump.fun`` or ``launchlab`` on Solana.
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
    target = aws.ws_url(url)
    stop = stop or asyncio.Event()
    dial = connect or aws._default_connect  # noqa: SLF001 - shared stack, by design
    stats = stats or LaunchFeedStats()
    seen = aws._RecentKeys(dedupe_size)  # noqa: SLF001
    feed_stats = aws.FeedStats()
    last_block: int | None = None
    attempt = 0

    def _fresh(entry: Any, at_ms: int, backfilled: bool) -> Launch | None:
        nonlocal last_block
        launch = launch_from_log(entry, received_ms=at_ms, backfilled=backfilled)
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
                sub_id = await session.call("eth_subscribe", ["logs", PONS_FILTER])
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
                            logs = await session.call("eth_getLogs", [{**PONS_FILTER, "fromBlock": hex(lo), "toBlock": hex(hi)}])
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
# Solana: tail what the PumpPortal listener persists
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class SolWatermark:
    """``(first_seen_ms, address)`` of the last row handed out."""

    first_seen_ms: int
    address: str = ""


def sol_launch_from_row(row: Mapping[str, Any], *, received_ms: int) -> Launch:
    meta = jload(row.get("meta_json"), {}) or {}
    pool = str(meta.get("pool") or "pump").lower()
    created = row.get("created_ms") or meta.get("source_ms")
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
            "bonding_curve": meta.get("bonding_curve"),
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
)
