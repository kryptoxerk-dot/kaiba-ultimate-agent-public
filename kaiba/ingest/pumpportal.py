"""PumpPortal websocket: the cheapest sub-second view of Solana launches.

Three constraints shape this module.

**One connection per IP.** PumpPortal bans an IP for an hour if it sees a second websocket
from it (``docs/research/05-upgrades-beyond-the-ask.md``, "Real-time detection stack").
A second connection is therefore not a performance problem, it is an hour of blindness, so
the module refuses to start twice: :class:`ConnectionLock` takes a lock file under
``data/`` and :func:`run` raises :class:`AlreadyRunning` rather than dialling.

**Latency is the product.** Every message carries or implies a source timestamp; the gap
between that and our receive time is the only honest way to decide whether the $99/mo
Yellowstone lane or the $499 LaserStream tier is worth buying (PLAN §13.15). We keep a
rolling window per stream, expose it through :func:`latency_stats`, and periodically write
it as a ``PROVIDER_BUDGET`` event plus an ``ingest_latency`` rollup row.

**A malformed message must never kill the listener.** Parsing is pure functions returning
``None`` on anything unexpected; the consume loop logs and moves on. A listener that dies
on one bad frame is worse than no listener, because it dies silently at 03:00.

Free streams only by default. ``subscribeTokenTrade`` is metered, so it is offered for a
watchlist but only when ``settings.pumpportal_api_key`` is set.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import socket
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kaiba.core.config import get_settings
from kaiba.core.db import get_conn, jdump, upsert
from kaiba.core.events import emit, emit_once
from kaiba.core.schemas import Chain, EventKind, Token, looks_solana, now_ms

log = logging.getLogger(__name__)

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
SOURCE = "pumpportal"
LAUNCHPAD = "pump.fun"

#: Stream names used for latency bookkeeping and in the ``ingest_latency`` table.
STREAM_NEW_TOKEN = "new_token"
STREAM_MIGRATION = "migration"
STREAM_TRADE = "trade"


# --------------------------------------------------------------------------------------
# the one-connection rule
# --------------------------------------------------------------------------------------


class AlreadyRunning(RuntimeError):
    """Another PumpPortal listener holds the lock. Connecting anyway costs an hour ban."""

    def __init__(self, path: Path, holder: dict[str, Any] | None) -> None:
        who = ""
        if holder:
            who = f" (pid {holder.get('pid')} on {holder.get('host')})"
        super().__init__(f"pumpportal listener already running{who}; lock at {path}")
        self.path = path
        self.holder = holder


def _pid_alive(pid: int) -> bool:
    """POSIX-only liveness probe.

    On Windows ``os.kill(pid, 0)`` calls ``TerminateProcess`` and would actually kill the
    holder, so we never probe there and fall back to the heartbeat age instead.
    """
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class ConnectionLock:
    """Advisory lock file. Stale locks (a crashed listener) are reclaimed, live ones are not."""

    path: Path
    stale_after_s: float = 180.0
    _held: bool = field(default=False, init=False)

    def _payload(self) -> dict[str, Any]:
        ts = now_ms()
        return {"pid": os.getpid(), "host": socket.gethostname(), "started_ms": ts, "heartbeat_ms": ts}

    def read(self) -> dict[str, Any] | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return None
        try:
            holder = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return holder if isinstance(holder, dict) else None

    def is_stale(self, holder: dict[str, Any] | None) -> bool:
        if not holder:
            return True  # unreadable or truncated: treat as debris
        beat = holder.get("heartbeat_ms") or holder.get("started_ms") or 0
        if (now_ms() - int(beat)) > self.stale_after_s * 1000:
            return True
        pid, host = holder.get("pid"), holder.get("host")
        if isinstance(pid, int) and host == socket.gethostname() and not _pid_alive(pid):
            return True
        return False

    def acquire(self) -> ConnectionLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder = self.read()
                if not self.is_stale(holder):
                    raise AlreadyRunning(self.path, holder) from None
                log.warning("reclaiming stale pumpportal lock %s (holder=%s)", self.path, holder)
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._payload(), fh)
            self._held = True
            return self
        raise AlreadyRunning(self.path, self.read())

    def heartbeat(self) -> None:
        if not self._held:
            return
        holder = self.read() or self._payload()
        holder["heartbeat_ms"] = now_ms()
        try:
            self.path.write_text(json.dumps(holder), encoding="utf-8")
        except OSError as exc:  # a heartbeat failure must not kill ingestion
            log.warning("pumpportal lock heartbeat failed: %s", exc)

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            self.path.unlink()
        except (FileNotFoundError, OSError):
            pass

    def __enter__(self) -> ConnectionLock:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


def default_lock_path() -> Path:
    return get_settings().kaiba_data_dir / "pumpportal.lock"


# --------------------------------------------------------------------------------------
# latency
# --------------------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile, ``q`` in 0-100. Deterministic, no interpolation."""
    if not values:
        raise ValueError("percentile of an empty sample")
    ordered = sorted(values)
    if q <= 0:
        return float(ordered[0])
    rank = math.ceil((q / 100.0) * len(ordered))
    return float(ordered[min(max(rank, 1), len(ordered)) - 1])


@dataclass
class LatencyTracker:
    """Rolling per-stream detect-lag window.

    Samples are ``(receive_ms, latency_ms)``. Negative values are kept rather than clamped:
    a negative p50 means our clock is behind the provider's, which is itself a finding.
    """

    window: int = 500
    _samples: dict[str, deque[tuple[int, int]]] = field(default_factory=dict, init=False)

    def record(self, stream: str, source_ms: int | None, receive_ms: int | None = None) -> int | None:
        if source_ms is None:
            return None
        got = receive_ms if receive_ms is not None else now_ms()
        delta = int(got - source_ms)
        self._samples.setdefault(stream, deque(maxlen=self.window)).append((int(got), delta))
        return delta

    def reset(self) -> None:
        self._samples.clear()

    def stats(self) -> dict[str, Any]:
        streams: dict[str, dict[str, Any]] = {}
        everything: list[int] = []
        oldest: int | None = None
        newest: int | None = None
        for stream, samples in self._samples.items():
            if not samples:
                continue
            lat = [d for _, d in samples]
            everything.extend(lat)
            first, last = samples[0][0], samples[-1][0]
            oldest = first if oldest is None else min(oldest, first)
            newest = last if newest is None else max(newest, last)
            streams[stream] = _summary(lat, first, last)
        return {
            "streams": streams,
            "overall": _summary(everything, oldest or 0, newest or 0) if everything else {},
            "window": self.window,
        }


def _summary(lat: Sequence[int], first_ms: int, last_ms: int) -> dict[str, Any]:
    return {
        "samples": len(lat),
        "p50_ms": round(percentile(lat, 50), 1),
        "p95_ms": round(percentile(lat, 95), 1),
        "min_ms": min(lat),
        "max_ms": max(lat),
        "mean_ms": round(sum(lat) / len(lat), 1),
        "window_start_ms": first_ms,
        "window_end_ms": last_ms,
    }


_LATENCY = LatencyTracker()


def latency_stats() -> dict[str, Any]:
    """Rolling p50/p95 per stream. The dashboard and the budget audit both read this."""
    return _LATENCY.stats()


def reset_latency() -> None:
    _LATENCY.reset()


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
                "(source, stream, window_start_ms, window_end_ms, samples, p50_ms, p95_ms, min_ms, max_ms) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    SOURCE,
                    stream,
                    s["window_start_ms"],
                    s["window_end_ms"],
                    s["samples"],
                    int(s["p50_ms"]),
                    int(s["p95_ms"]),
                    int(s["min_ms"]),
                    int(s["max_ms"]),
                ),
            )
        except Exception as exc:  # noqa: BLE001 — bookkeeping never breaks ingestion
            log.warning("latency rollup write failed: %s", exc)
    return emit(
        EventKind.PROVIDER_BUDGET,
        {"provider": SOURCE, "latency": snap},
        chain=Chain.SOL,
        conn=c,
    )


# --------------------------------------------------------------------------------------
# pure parsers
# --------------------------------------------------------------------------------------


def _as_lamports(value: Any) -> int | None:
    """SOL float from the wire -> integer lamports. Floats never reach the database."""
    if value is None:
        return None
    try:
        return int((Decimal(str(value)) * 1_000_000_000).to_integral_value(rounding=ROUND_DOWN))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return str(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _first(msg: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = msg.get(k)
        if v is not None:
            return v
    return None


def source_timestamp_ms(msg: dict[str, Any]) -> int | None:
    """Provider-side timestamp in ms, whatever unit and field name it arrived in.

    PumpPortal has shipped ``timestamp`` in both seconds and milliseconds across versions
    and sometimes only ``blockTime`` (seconds), so the unit is inferred from magnitude
    rather than trusted.
    """
    raw = _first(msg, "timestamp", "blockTime", "block_time", "ts", "time")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value > 1e14:  # microseconds
        return int(value / 1000)
    if value > 1e11:  # already milliseconds
        return int(value)
    return int(value * 1000)  # seconds


def _valid_mint(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    mint = value.strip()
    return mint if looks_solana(mint) else None


def parse_new_token(msg: Any) -> Token | None:
    """A ``subscribeNewToken`` frame -> :class:`Token`, or ``None`` if it is not one."""
    if not isinstance(msg, dict):
        return None
    tx_type = str(msg.get("txType") or msg.get("tx_type") or "").lower()
    if tx_type and tx_type != "create":
        return None
    mint = _valid_mint(_first(msg, "mint", "mintAddress", "ca"))
    if mint is None:
        return None
    if not tx_type and not any(k in msg for k in ("uri", "name", "symbol", "bondingCurveKey")):
        return None  # not enough to call this a creation
    created = source_timestamp_ms(msg) or now_ms()
    creator = _first(msg, "traderPublicKey", "creator", "trader_public_key", "user")
    meta = {
        "uri": _first(msg, "uri", "metadataUri"),
        "signature": _first(msg, "signature", "tx"),
        "slot": _first(msg, "slot"),
        "bonding_curve": _first(msg, "bondingCurveKey", "bonding_curve"),
        "initial_buy_tokens": _as_text(_first(msg, "initialBuy", "initial_buy")),
        "initial_buy_lamports": _as_lamports(_first(msg, "solAmount", "sol_amount")),
        "v_tokens_in_curve": _as_text(_first(msg, "vTokensInBondingCurve")),
        "v_sol_in_curve": _as_text(_first(msg, "vSolInBondingCurve")),
        "market_cap_sol": _as_text(_first(msg, "marketCapSol", "market_cap_sol")),
        "pool": _first(msg, "pool") or "pump",
        "source": SOURCE,
        "source_ms": source_timestamp_ms(msg),
    }
    return Token(
        address=mint,
        chain=Chain.SOL,
        symbol=_first(msg, "symbol"),
        name=_first(msg, "name"),
        creator=str(creator) if isinstance(creator, str) else None,
        created_ms=created,
        launchpad=LAUNCHPAD,
        pool=meta["bonding_curve"] if isinstance(meta["bonding_curve"], str) else None,
        meta={k: v for k, v in meta.items() if v is not None},
    )


def parse_migration(msg: Any) -> dict[str, Any] | None:
    """A ``subscribeMigration`` frame -> a flat dict, or ``None``."""
    if not isinstance(msg, dict):
        return None
    tx_type = str(msg.get("txType") or msg.get("tx_type") or "").lower()
    if tx_type not in {"migrate", "migration", "complete"}:
        return None
    mint = _valid_mint(_first(msg, "mint", "mintAddress", "ca"))
    if mint is None:
        return None
    source_ms = source_timestamp_ms(msg)
    return {
        "mint": mint,
        "chain": Chain.SOL,
        "pool": _first(msg, "pool") or "pumpswap",
        "pool_address": _first(msg, "poolAddress", "pool_address"),
        "signature": _first(msg, "signature", "tx"),
        "slot": _first(msg, "slot"),
        "source_ms": source_ms,
        "migrated_ms": source_ms or now_ms(),
    }


def parse_trade(msg: Any) -> dict[str, Any] | None:
    """A ``subscribeTokenTrade`` frame -> a ``swaps``-shaped dict, or ``None``.

    Only used when a watchlist is subscribed, which needs a paid key.
    """
    if not isinstance(msg, dict):
        return None
    side = str(msg.get("txType") or msg.get("tx_type") or "").lower()
    if side not in {"buy", "sell"}:
        return None
    mint = _valid_mint(_first(msg, "mint", "mintAddress"))
    wallet = _first(msg, "traderPublicKey", "trader_public_key", "wallet")
    signature = _first(msg, "signature", "tx")
    if mint is None or not isinstance(wallet, str) or not isinstance(signature, str):
        return None
    source_ms = source_timestamp_ms(msg)
    return {
        "chain": Chain.SOL.value,
        "tx": signature,
        "slot": _first(msg, "slot"),
        "ts_ms": source_ms or now_ms(),
        "wallet": wallet,
        "token": mint,
        "side": side,
        "amount_token": _as_text(_first(msg, "tokenAmount", "token_amount")),
        "amount_native": str(_as_lamports(_first(msg, "solAmount", "sol_amount")) or 0),
        "price_usd": None,
        "usd_value": None,
        "program": str(_first(msg, "pool") or "pump"),
        "source": SOURCE,
        "source_ms": source_ms,
    }


# --------------------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------------------


def record_new_token(token: Token, conn: Any = None) -> bool:
    """Upsert the token and emit ``TOKEN_CREATED`` once per mint. True if it was new."""
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
            "creator": token.creator,
            "created_ms": token.created_ms,
            "launchpad": token.launchpad,
            "pool": token.pool,
            "first_seen_ms": ts,
            "meta_json": jdump(token.meta),
        },
        conflict=["chain", "address"],
        # first_seen_ms and migrated_ms are deliberately not updated: the first sighting
        # is the number the latency work cares about.
        update=["symbol", "name", "creator", "created_ms", "launchpad", "pool", "meta_json"],
    )
    event_id = emit_once(
        EventKind.TOKEN_CREATED,
        {
            "mint": token.address,
            "symbol": token.symbol,
            "name": token.name,
            "creator": token.creator,
            "uri": token.meta.get("uri"),
            "launchpad": token.launchpad,
            "created_ms": token.created_ms,
            "initial_buy_lamports": token.meta.get("initial_buy_lamports"),
            "slot": token.meta.get("slot"),
            "signature": token.meta.get("signature"),
            "source": SOURCE,
        },
        chain=Chain.SOL,
        subject=token.address,
        dedupe_key=f"{EventKind.TOKEN_CREATED.value}:{Chain.SOL.value}:{token.address}",
        conn=c,
    )
    return event_id is not None


def record_migration(info: dict[str, Any], conn: Any = None) -> bool:
    """Stamp ``tokens.migrated_ms`` and emit ``TOKEN_MIGRATED`` once per mint."""
    c = conn or get_conn()
    mint = info["mint"]
    upsert(
        c,
        "tokens",
        {
            "chain": Chain.SOL.value,
            "address": mint,
            "migrated_ms": info["migrated_ms"],
            "launchpad": LAUNCHPAD,
            "first_seen_ms": now_ms(),
        },
        conflict=["chain", "address"],
        update=["migrated_ms"],  # never clobber name/symbol/creator learned at creation
    )
    event_id = emit_once(
        EventKind.TOKEN_MIGRATED,
        {
            "mint": mint,
            "pool": info.get("pool"),
            "pool_address": info.get("pool_address"),
            "signature": info.get("signature"),
            "slot": info.get("slot"),
            "migrated_ms": info["migrated_ms"],
            "source": SOURCE,
        },
        chain=Chain.SOL,
        subject=mint,
        dedupe_key=f"{EventKind.TOKEN_MIGRATED.value}:{Chain.SOL.value}:{mint}",
        conn=c,
    )
    return event_id is not None


def record_trade(row: dict[str, Any], conn: Any = None) -> bool:
    c = conn or get_conn()
    cur = c.execute(
        "INSERT OR IGNORE INTO swaps "
        "(chain, tx, slot, ts_ms, wallet, token, side, amount_token, amount_native, "
        " price_usd, usd_value, program, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row["chain"], row["tx"], row["slot"], row["ts_ms"], row["wallet"], row["token"],
            row["side"], row["amount_token"], row["amount_native"], row["price_usd"],
            row["usd_value"], row["program"], row["source"],
        ),
    )
    if not cur.rowcount:
        return False
    emit_once(
        EventKind.WALLET_TRADE,
        {k: v for k, v in row.items() if k != "source_ms"},
        chain=Chain.SOL,
        subject=row["wallet"],
        dedupe_key=f"{EventKind.WALLET_TRADE.value}:{SOURCE}:{row['tx']}:{row['wallet']}:{row['side']}",
        conn=c,
    )
    return True


# --------------------------------------------------------------------------------------
# message dispatch
# --------------------------------------------------------------------------------------


def _screen(msg: dict[str, Any], conn: Any) -> None:
    """Hand a launch to tier-0 triage. Measured at 0.61 ms live, budget 100 ms.

    Imported lazily because ``kaiba.execution`` imports back into ``kaiba.ingest``, and
    swallowed because a screening failure must never cost us the ingest row we just
    wrote: knowing a token exists is worth more than knowing what we thought of it.

    The *raw frame* is passed rather than the parsed ``Token`` because triage reads
    fields the model drops.
    """
    try:
        from kaiba.execution.triage import screen_launch

        screen_launch(msg, conn=conn)
    except Exception as exc:  # noqa: BLE001 - triage is advisory, ingest is not
        log.debug("triage skipped a launch (%s: %s)", type(exc).__name__, exc)


def handle_message(raw: str | bytes | dict[str, Any], conn: Any = None) -> str | None:
    """Route one frame. Returns the stream it was handled as, or ``None`` if ignored.

    Never raises: a bad frame is a log line, not an outage.
    """
    msg: Any = raw
    if isinstance(raw, (str, bytes)):
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.debug("pumpportal: undecodable frame (%d bytes)", len(raw))
            return None
    if not isinstance(msg, dict):
        return None
    if "message" in msg and "txType" not in msg:
        log.info("pumpportal: %s", msg.get("message"))
        return None
    if "errors" in msg:
        emit(
            EventKind.PROVIDER_ERROR,
            {"provider": SOURCE, "errors": msg.get("errors")},
            chain=Chain.SOL,
            level="warn",
            conn=conn,
        )
        return None

    try:
        token = parse_new_token(msg)
        if token is not None:
            _LATENCY.record(STREAM_NEW_TOKEN, token.meta.get("source_ms"))
            record_new_token(token, conn=conn)
            _screen(msg, conn)
            return STREAM_NEW_TOKEN

        migration = parse_migration(msg)
        if migration is not None:
            _LATENCY.record(STREAM_MIGRATION, migration.get("source_ms"))
            record_migration(migration, conn=conn)
            return STREAM_MIGRATION

        trade = parse_trade(msg)
        if trade is not None:
            _LATENCY.record(STREAM_TRADE, trade.get("source_ms"))
            record_trade(trade, conn=conn)
            return STREAM_TRADE
    except Exception as exc:  # noqa: BLE001 — one bad frame must not stop the stream
        log.warning("pumpportal: dropped a frame (%s: %s)", type(exc).__name__, exc)
        return None
    return None


def subscribe_payloads(
    watchlist: Iterable[str] | None = None, api_key: str | None = None
) -> list[dict[str, Any]]:
    """The subscribe frames we send. Free streams always; token trades only when paid for."""
    frames: list[dict[str, Any]] = [
        {"method": "subscribeNewToken"},
        {"method": "subscribeMigration"},
    ]
    keys = [k for k in (watchlist or []) if _valid_mint(k)]
    if keys and api_key:
        frames.append({"method": "subscribeTokenTrade", "keys": keys})
    elif keys:
        log.info("pumpportal: %d watchlist mints ignored, token-trade stream is metered", len(keys))
    return frames


# --------------------------------------------------------------------------------------
# the loop
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
    """Exponential backoff with additive jitter, hard-capped at ``cap`` seconds.

    Jitter is additive-upward rather than full-random so the first retry is still prompt;
    the cap is applied after jitter so we never sleep longer than a minute.
    """
    raw = min(cap, base * (2 ** min(max(attempt, 0), 16)))
    return round(min(cap, raw * (1.0 + jitter * (rand or _rand)())), 3)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, waking early if ``stop`` is set. Tests patch this to capture the schedule."""
    if seconds <= 0:
        return
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


def _default_connect(url: str) -> Any:
    import websockets  # imported lazily so the module is importable without the extra

    return websockets.connect(url, ping_interval=20, ping_timeout=20, max_queue=2048)


async def consume(
    ws: Any,
    stop: asyncio.Event,
    *,
    conn: Any = None,
    lock: ConnectionLock | None = None,
    budget_interval_s: float = 300.0,
) -> int:
    """Read frames until the socket closes or ``stop`` is set. Returns frames handled."""
    handled = 0
    unreported = 0
    next_budget = now_ms() + int(budget_interval_s * 1000)
    next_report = now_ms() + 10_000
    async for raw in ws:
        if handle_message(raw, conn=conn) is not None:
            handled += 1
            unreported += 1
        if unreported and now_ms() >= next_report:
            _note_events(unreported, conn)
            unreported = 0
            next_report = now_ms() + 10_000
        if now_ms() >= next_budget:
            publish_latency(conn=conn)
            if lock is not None:
                lock.heartbeat()
            next_budget = now_ms() + int(budget_interval_s * 1000)
        if stop.is_set():
            break
    _note_events(unreported, conn)
    return handled


def _note_events(count: int, conn: Any) -> None:
    """Report handled frames to ``ingest_status`` so the feed's health is visible.

    Imported lazily: ``runner`` imports this module, so a top-level import is a cycle.
    """
    if count <= 0:
        return
    try:
        from kaiba.ingest.runner import note_events

        note_events("pumpportal", count, conn)
    except Exception as exc:  # noqa: BLE001 - never let bookkeeping stop the stream
        log.debug("could not report ingest progress: %s", exc)


async def run(
    stop: asyncio.Event | None = None,
    *,
    url: str = PUMPPORTAL_WS,
    connect: Callable[[str], Any] | None = None,
    lock_path: Path | None = None,
    watchlist: Iterable[str] | None = None,
    conn: Any = None,
    budget_interval_s: float = 300.0,
    max_attempts: int | None = None,
) -> None:
    """Hold the single PumpPortal connection until ``stop`` is set.

    Reconnects with jittered exponential backoff capped at 60 s, logging every attempt so
    a flapping feed is visible in the journal rather than inferred from missing tokens.
    """
    stop = stop or asyncio.Event()
    dial = connect or _default_connect
    lock = ConnectionLock(lock_path or default_lock_path()).acquire()
    api_key = get_settings().pumpportal_api_key or None
    frames = subscribe_payloads(watchlist, api_key)
    attempt = 0
    try:
        while not stop.is_set():
            if max_attempts is not None and attempt >= max_attempts:
                break
            try:
                async with dial(url) as ws:
                    for frame in frames:
                        await ws.send(json.dumps(frame))
                    log.info("pumpportal: connected, %d subscriptions", len(frames))
                    emit(
                        EventKind.SYSTEM,
                        {"component": "ingest.pumpportal", "status": "connected",
                         "subscriptions": [f["method"] for f in frames]},
                        chain=Chain.SOL,
                        conn=conn,
                    )
                    attempt = 0
                    await _race_stop(
                        consume(ws, stop, conn=conn, lock=lock, budget_interval_s=budget_interval_s),
                        stop,
                    )
            except asyncio.CancelledError:
                raise
            except AlreadyRunning:
                raise
            except Exception as exc:  # noqa: BLE001 — every dial failure is a retry
                delay = backoff_delay(attempt)
                attempt += 1
                log.warning(
                    "pumpportal: disconnected (%s: %s); reconnect #%d in %.1fs",
                    type(exc).__name__, exc, attempt, delay,
                )
                emit(
                    EventKind.PROVIDER_ERROR,
                    {"provider": SOURCE, "error": f"{type(exc).__name__}: {exc}",
                     "reconnect_in_s": delay, "attempt": attempt},
                    chain=Chain.SOL,
                    level="warn",
                    conn=conn,
                )
                await _wait(stop, delay)
                continue
            if stop.is_set():
                break
            delay = backoff_delay(attempt)
            attempt += 1
            log.warning("pumpportal: stream ended cleanly; reconnect #%d in %.1fs", attempt, delay)
            await _wait(stop, delay)
    finally:
        publish_latency(conn=conn)
        lock.release()
        log.info("pumpportal: stopped")


async def _race_stop(coro: Any, stop: asyncio.Event) -> None:
    """Run ``coro`` but return as soon as ``stop`` is set (the socket read blocks)."""
    worker = asyncio.ensure_future(coro)
    stopper = asyncio.ensure_future(stop.wait())
    try:
        done, _ = await asyncio.wait({worker, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            worker.result()  # re-raise a socket error into the retry loop
    finally:
        for task in (worker, stopper):
            if not task.done():
                task.cancel()
