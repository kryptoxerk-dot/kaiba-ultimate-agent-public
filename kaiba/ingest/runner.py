"""Supervisor for the ingest listeners.

One process, one asyncio loop, one stop event. Each listener runs in its own supervised
task so a crash in the GMGN poller cannot take the PumpPortal socket down with it — that
matters more than usual here, because losing the socket costs a reconnect and possibly an
hour-long ban, while losing a poller costs one missed page.

Restart policy is per-feed jittered exponential backoff capped at a minute (the same
schedule the PumpPortal reconnect uses), with the crash recorded as a SYSTEM event so a
flapping listener shows up in the dashboard rather than in a log nobody reads.

Status heartbeats: every 60 s the supervisor writes one SYSTEM event and refreshes the
``ingest_status`` row per feed, which is what the Phase 1 acceptance test ("24 h of
uninterrupted ingestion") is checked against.

Run it with ``python -m kaiba.ingest.runner --feeds pumpportal,gmgn,telegram``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any

from kaiba.core.config import get_settings
from kaiba.core.db import ensure_db, get_conn
from kaiba.core.events import emit
from kaiba.core.schemas import EventKind, now_ms
from kaiba.ingest import gmgn_feeds, pumpportal, rhscannerr, robinhood, telegram_calls
from kaiba.ingest.pumpportal import AlreadyRunning, backoff_delay

log = logging.getLogger(__name__)

#: name -> coroutine factory taking the shared stop event.
FeedFactory = Callable[[asyncio.Event], Awaitable[None]]

REGISTRY: dict[str, FeedFactory] = {
    "pumpportal": lambda stop: pumpportal.run(stop=stop),
    "gmgn": lambda stop: gmgn_feeds.run(stop=stop),
    "telegram": lambda stop: telegram_calls.run(stop=stop),
    # Pons V2 on Robinhood Chain. Registered but deliberately not in DEFAULT_FEEDS: it is
    # a poller against a public RPC with no limiter entry of its own, and the venue's
    # tradeability is still an open question (docs/research/11-bsc-robinhood-edge-2026.md).
    # Start it explicitly with ``--feeds pumpportal,gmgn,robinhood``.
    "robinhood": lambda stop: robinhood.run(stop=stop),
    # The RH Scanner channel's PUBLIC web preview (t.me/s/rhscannerr), read over plain
    # HTTP once a minute. Observation only: it emits `alpha.meta` and is not an execution
    # lane, so nothing it says can size or place an order by itself.
    #
    # It is not the `telegram` listener and the licensing reasoning below does not reach
    # it: no account, no API, no channel history -- the same page any browser is served.
    "rhscannerr": lambda stop: rhscannerr.run(stop=stop),
}

#: Feeds started when the operator names none. ``telegram`` is deliberately absent.
#:
#: Telegram's Content Licensing terms prohibit "scraping, indexing, harvesting,
#: aggregation or use of data ... deployment of artificial intelligence, machine learning
#: models" (see docs/research/12-early-alpha-airdrops-nfts-2026.md). A Telethon userbot
#: feeding channel text to an LLM sits inside that language. The capability stays, because
#: it is the operator's account and their call to make, but it is not switched on by a
#: default. Start it explicitly with ``--listeners pumpportal,gmgn,telegram``.
DEFAULT_FEEDS: tuple[str, ...] = ("pumpportal", "gmgn", "rhscannerr")
HEARTBEAT_S = 60.0
#: A listener that returns faster than this has not really run.
IDLE_RETURN_MS = 2_000
#: How many immediate returns before we stop retrying and call it idle.
IDLE_RETURN_LIMIT = 3


def set_status(feed: str, state: str, conn: Any = None, **fields: Any) -> None:
    """Upsert one ``ingest_status`` row. Bookkeeping only — never raises into a listener."""
    c = conn or get_conn()
    try:
        c.execute(
            "INSERT INTO ingest_status (feed, state, started_ms, last_event_ms, events_seen, "
            " restarts, last_error, updated_ms) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(feed) DO UPDATE SET state=excluded.state, "
            "  started_ms=COALESCE(excluded.started_ms, ingest_status.started_ms), "
            "  last_event_ms=COALESCE(excluded.last_event_ms, ingest_status.last_event_ms), "
            "  restarts=COALESCE(excluded.restarts, ingest_status.restarts), "
            "  events_seen=ingest_status.events_seen + excluded.events_seen, "
            "  last_error=excluded.last_error, updated_ms=excluded.updated_ms",
            (
                feed,
                state,
                fields.get("started_ms"),
                fields.get("last_event_ms"),
                int(fields.get("events_seen") or 0),
                fields.get("restarts"),
                fields.get("last_error"),
                now_ms(),
            ),
        )
    except Exception as exc:  # noqa: BLE001 - status writing is not worth an outage
        log.warning("ingest_status write failed for %s: %s", feed, exc)


def note_events(feed: str, count: int, conn: Any = None, *, last_event_ms: int | None = None) -> None:
    """Add ``count`` handled frames to a feed's running total.

    ``events_seen`` was in the INSERT column list but missing from the ON CONFLICT UPDATE,
    so it was written once at zero and never moved again. A 60-second live run ingested 23
    tokens while `kaiba status` reported a feed that had seen nothing, which is exactly the
    state where an operator cannot tell a working listener from a silently dead one.
    """
    if count <= 0:
        return
    c = conn or get_conn()
    try:
        c.execute(
            "UPDATE ingest_status SET events_seen = events_seen + ?, "
            "last_event_ms = COALESCE(?, last_event_ms), updated_ms = ? WHERE feed = ?",
            (int(count), last_event_ms or now_ms(), now_ms(), feed),
        )
    except Exception as exc:  # noqa: BLE001 - bookkeeping never breaks a listener
        log.debug("ingest_status increment failed for %s: %s", feed, exc)


async def _supervise(
    name: str,
    factory: FeedFactory,
    stop: asyncio.Event,
    *,
    conn: Any = None,
    max_restarts: int | None = None,
) -> int:
    """Run one listener until ``stop``, restarting it on crash. Returns the restart count."""
    restarts = 0
    started = now_ms()
    quick_returns = 0
    set_status(name, "starting", conn, started_ms=started, restarts=0)
    while not stop.is_set():
        attempt_ms = now_ms()
        try:
            set_status(name, "running", conn, started_ms=started, restarts=restarts)
            await factory(stop)
            if stop.is_set():
                break
            # A listener that returns immediately, repeatedly, is not crashing — it has
            # nothing to do. The telegram listener with no channels configured did exactly
            # this and was restarted once a second forever, burning CPU and filling the
            # log while looking like a healthy feed. Idle is a state, not a failure.
            if now_ms() - attempt_ms < IDLE_RETURN_MS:
                quick_returns += 1
                if quick_returns >= IDLE_RETURN_LIMIT:
                    log.warning(
                        "ingest %s: returned immediately %d times; marking idle and "
                        "leaving it down (it has nothing to do, check its configuration)",
                        name, quick_returns,
                    )
                    set_status(name, "idle", conn, restarts=restarts,
                               last_error="returned immediately; nothing configured")
                    emit(
                        EventKind.SYSTEM,
                        {"component": f"ingest.{name}", "status": "idle",
                         "error": "listener returned immediately; nothing configured"},
                        level="warn", conn=conn,
                    )
                    return restarts
            else:
                quick_returns = 0
            log.warning("ingest %s: returned before stop; restarting", name)
            reason = "returned early"
        except asyncio.CancelledError:
            raise
        except AlreadyRunning as exc:
            # A second PumpPortal socket is a one-hour ban, so this is fatal for this feed
            # and must never be retried in a loop.
            log.error("ingest %s: %s", name, exc)
            set_status(name, "disabled", conn, restarts=restarts, last_error=str(exc))
            emit(
                EventKind.SYSTEM,
                {"component": f"ingest.{name}", "status": "disabled", "error": str(exc)},
                level="error",
                conn=conn,
            )
            return restarts
        except Exception as exc:  # noqa: BLE001 - a listener crash is a restart, not an exit
            reason = f"{type(exc).__name__}: {exc}"
            log.exception("ingest %s crashed: %s", name, reason)
        if stop.is_set():
            break
        if max_restarts is not None and restarts >= max_restarts:
            log.error("ingest %s: restart budget exhausted (%d)", name, max_restarts)
            set_status(name, "crashed", conn, restarts=restarts, last_error=reason)
            break
        delay = backoff_delay(restarts)
        restarts += 1
        set_status(name, "reconnecting", conn, restarts=restarts, last_error=reason)
        emit(
            EventKind.SYSTEM,
            {"component": f"ingest.{name}", "status": "restarting", "error": reason,
             "restarts": restarts, "delay_s": delay},
            level="warn",
            conn=conn,
        )
        await _wait(stop, delay)
    set_status(name, "stopped", conn, restarts=restarts)
    return restarts


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, waking early on stop. Tests patch this."""
    if seconds <= 0:
        return
    with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def _heartbeat(
    feeds: Sequence[str], stop: asyncio.Event, interval_s: float, conn: Any = None
) -> None:
    """Status heartbeats: one SYSTEM event per interval so silence is distinguishable from death."""
    while not stop.is_set():
        emit(
            EventKind.SYSTEM,
            {
                "component": "ingest.runner",
                "status": "heartbeat",
                "feeds": list(feeds),
                "latency": pumpportal.latency_stats() if "pumpportal" in feeds else {},
                "ts_ms": now_ms(),
            },
            conn=conn,
        )
        await _wait(stop, interval_s)


async def run_all(
    feeds: Iterable[str],
    stop: asyncio.Event | None = None,
    *,
    registry: dict[str, FeedFactory] | None = None,
    heartbeat_s: float = HEARTBEAT_S,
    conn: Any = None,
    max_restarts: int | None = None,
) -> dict[str, int]:
    """Supervise the named listeners until ``stop``. Returns restarts per feed."""
    stop = stop or asyncio.Event()
    table = registry if registry is not None else REGISTRY
    wanted = [f for f in feeds]
    unknown = [f for f in wanted if f not in table]
    if unknown:
        raise KeyError(f"unknown ingest feed(s) {unknown}; known: {sorted(table)}")
    if not wanted:
        return {}

    emit(
        EventKind.SYSTEM,
        {"component": "ingest.runner", "status": "starting", "feeds": wanted},
        conn=conn,
    )
    tasks = {
        name: asyncio.ensure_future(
            _supervise(name, table[name], stop, conn=conn, max_restarts=max_restarts)
        )
        for name in wanted
    }
    beat = asyncio.ensure_future(_heartbeat(wanted, stop, heartbeat_s, conn=conn))
    try:
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    finally:
        stop.set()
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
    out: dict[str, int] = {}
    for name, result in zip(tasks, results, strict=True):
        if isinstance(result, BaseException):
            log.error("ingest %s supervisor failed: %s", name, result)
            out[name] = -1
        else:
            out[name] = result
    emit(
        EventKind.SYSTEM,
        {"component": "ingest.runner", "status": "stopped", "restarts": out},
        conn=conn,
    )
    return out


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, AttributeError, ValueError):
            # Windows ProactorEventLoop has no add_signal_handler; KeyboardInterrupt still works.
            pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kaiba.ingest.runner", description="Run the ingest listeners")
    parser.add_argument(
        "--feeds",
        default=",".join(DEFAULT_FEEDS),
        help=f"comma-separated subset of {','.join(DEFAULT_FEEDS)}",
    )
    parser.add_argument("--heartbeat-s", type=float, default=HEARTBEAT_S)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=get_settings().kaiba_log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    feeds = [f.strip() for f in args.feeds.split(",") if f.strip()]
    ensure_db()

    async def _main() -> dict[str, int]:
        stop = asyncio.Event()
        _install_signal_handlers(asyncio.get_running_loop(), stop)
        return await run_all(feeds, stop, heartbeat_s=args.heartbeat_s)

    try:
        restarts = asyncio.run(_main())
    except KeyboardInterrupt:
        log.info("ingest runner: interrupted")
        return 130
    log.info("ingest runner: finished (%s)", restarts)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
