"""Flap (BSC) launches and graduations into ``tokens``, about a second after the block.

Why this exists. The ``launch-snipe`` lane can only act on a launch the engine can see,
and the engine reads ``tokens`` (its launchpad allowlist, the depth reader, the dossier).
MEASURED 2026-10-05 on the box: every bsc ``launchpad='flap'`` row came from a GMGN feed,
1,723 in 24 h against ~23,000 launches on chain (~7%), first seen a median 71 s (p10 16 s)
after creation. Nothing could be sniped from that.

What it does. One ``eth_subscribe("logs")`` on the Flap portal over the Alchemy BSC
WebSocket (``launch_feed.stream_flap``: ``TokenCreated`` and ``LaunchedToDEX`` only), and
for each:

* a launch -> one ``tokens`` row (chain ``bsc``, launchpad ``flap``, block time,
  ``meta.source`` = ``launch_feed.FLAP_SOURCE``), the same shape
  ``robinhood.record_new_token`` writes. ``launch_feed.tail_bsc`` hands those rows to the
  snipe lane. ``tokens.creator`` is the launch's real sender when the event's creator word
  is a contract (:class:`SenderResolver`; the word is kept as ``meta.event_creator``).
* a graduation -> ``tokens.migrated_ms`` on a row that EXISTS (an UPDATE, never an
  insert: a graduation-only row would have no symbol, name, creator or birth, and GMGN can
  only fill a row it is allowed to merge into). Protection's migration grace reads exactly
  that column, and without it a graduation reads as a rug (``migration-read-as-rug``: 33
  of 45 rug exits were graduations).

Rows other writers own are not taken over (:func:`record_new_token`): a GMGN row the
listener sees later (a backfill after a restart) only has its NULL columns filled.

Restarts. The newest block written is persisted (kv :data:`CURSOR_KEY`) and the first
connection of a new process backfills from it (``stream_flap(start_block=)``), capped at
~5 h. Before that cursor existed every ``kaiba-ingest`` restart (deploy, OOM, db_guard)
silently dropped the downtime's graduations, and protection would then read a held Flap
token that graduated meanwhile as a 100% liquidity drop.

What it deliberately does NOT do:

* no ``token.created`` / ``token.migrated`` event. ~23k launches a day would roughly double
  the event rows written, and ``scanner._migration_work`` turns every ``token.migrated``
  into a tier-1 scan (GMGN budget shared with live stops). Protection reads the column.
* no ``triage.screen_launch``: 23k launches a day through tier 0 would flood it. GMGN's
  feeds still screen every Flap token THEY surface: ``gmgn_feeds.write_token`` merges
  into a listener row and screens it once (``MERGEABLE_LISTENER_SOURCES``).
* nothing unless asked, and nothing on a shared key. It idles (returns at once, so the
  runner marks it ``idle``) unless the ``launch-snipe`` lane's ``params.chains`` lists
  ``bsc`` AND ``BSC_SNIPE_RPC_URL`` (``launch_feed.BSC_SNIPE_RPC_ENV``) names an Alchemy
  ``/v2/`` endpoint -- NOT ``BSC_RPC_URL``, whose key protection's price reads use.
  MEASURED cost when on: ~54 CU a launch, ~38M CU a month at the 2026-10-05 rate, plus
  ~17 CU a launch for the sender lookup (``bsc_resolve_sender``).
* no unbounded spend. Every :data:`RECHECK_EVERY_S` it re-reads the switch, and estimates
  today's CU (notification bytes x 0.04, getLogs x 60, sender lookups); when the lane drops
  ``bsc`` or the day passes ``bsc_feed_max_cu_per_day`` it closes the socket and waits,
  reconnecting (with a cursor backfill) when the switch is back or the UTC day turns.

Secrets: the endpoint carries the API key in its path. Every string that could carry it
goes through ``alchemy_ws.redact`` / ``mask_url``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kaiba.core.db import fetch_one, get_conn, jdump, jload
from kaiba.core.events import emit
from kaiba.core.schemas import Chain, EventKind, Token, looks_evm, now_ms
from kaiba.execution.evm_price import FLAP_PORTAL
from kaiba.ingest import alchemy_ws as aws
from kaiba.ingest import launch_feed as lf

log = logging.getLogger(__name__)

FEED = "flap"
CHAIN = Chain.BSC
SOURCE = lf.FLAP_SOURCE
LAUNCHPAD = lf.FLAP_LAUNCHPAD
#: How often a ``stats`` SYSTEM event (and the ``ingest_status`` count) is written, and the
#: cursor and the day's CU estimate persisted.
STATS_EVERY_S = 300.0
#: How often the switch (the lane lists bsc) and the CU ceiling are re-checked while running.
RECHECK_EVERY_S = 120.0
#: kv: ``{"block": n}``, the newest block whose events were written. The next process
#: backfills from it.
CURSOR_KEY = "flap:last_block"
#: kv: ``{"day": "YYYY-MM-DD", "cu": x}``, today's (UTC) estimated Alchemy CU for this feed.
CU_DAY_KEY = "flap:cu_day"
#: Alchemy's published CU prices for the two sender reads (not re-measured here).
CU_GET_TX = 17
CU_GET_CODE = 26
#: The limiter bucket sender lookups are charged to: not ``rpc`` (protection's), and not
#: ``bsc-snipe-rpc`` (the snipe lane's reads), so a burst of launches starves neither.
SENDER_BUCKET = "bsc-flap-rpc"
#: An EIP-7702 delegated EOA's code: ``0xef0100 || address``. An EOA, not a launcher.
EIP7702_PREFIX = "0xef0100"
#: How long a launch's sender lookup may wait for a limiter slot before the row is written
#: with the event's creator (``creator_basis: unresolved:*``) rather than wait longer.
SENDER_WAIT_FOR_SLOT_S = 1.0


def alchemy_url() -> str | None:
    """The DEDICATED BSC endpoint (``launch_feed.bsc_snipe_rpc_url``), only if it is an
    Alchemy (``/v2/``) URL. Never ``BSC_RPC_URL``: its key serves protection's price reads
    (``launch_feed.BSC_SNIPE_RPC_ENV`` says why)."""
    url = lf.bsc_snipe_rpc_url()
    return url if url and "/v2/" in url else None


def feed_params() -> dict[str, Any]:
    """The ``launch-snipe`` lane's params (defaults overlaid). Lazily imported:
    ``kaiba.execution.snipe`` imports this package's ``launch_feed``. ``{}`` if unreadable."""
    try:
        from kaiba.execution.snipe import params

        return dict(params())
    except Exception as exc:  # noqa: BLE001 - an unreadable config is not a yes
        log.debug("flap: launch-snipe params unreadable (%s)", exc)
        return {}


def lane_wants_bsc() -> bool:
    """True only when ``launch-snipe``'s params list ``bsc`` -- the switch for this feed."""
    return Chain.BSC.value in [str(c) for c in feed_params().get("chains") or []]


def _max_cu_per_day(p: Mapping[str, Any]) -> float:
    try:
        return float(p.get("bsc_feed_max_cu_per_day") or 0)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------------------
# who launched it
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sender:
    """Who a launch belongs to. ``creator`` is what ``tokens.creator`` gets."""

    creator: str | None
    event_creator: str | None
    tx_from: str | None
    #: ``creator_is_sender`` | ``sender_of_contract_creator`` | ``creator_eoa_not_sender`` |
    #: ``tx_from_no_event_creator`` | ``unresolved:<why>`` | ``event_creator`` (not looked up)
    basis: str

    @property
    def resolved(self) -> bool:
        return not self.basis.startswith("unresolved") and self.basis != "event_creator"


def _bsc_rpc_default(calls: Sequence[tuple[str, list[Any]]]) -> list[Any] | None:
    from kaiba.execution.snipe import bsc_rpc

    return bsc_rpc(calls, endpoint="flap.sender", bucket=SENDER_BUCKET, wait_for_slot_s=SENDER_WAIT_FOR_SLOT_S,
                   timeout_s=5.0)


class SenderResolver:
    """The launch transaction's sender, and whether the event's creator is a contract.

    WHY (review 2026-10-05). ``deployer_stats`` keys a deployer's record on
    ``tokens.creator``. The Flap event's creator word is a launcher contract on ~35% of
    launches (MEASURED: 26 of 40 equal the sender; of 76 logs, the two most active
    creators returned contract bytecode), so writing it would pool every user of one
    launcher into one mega-deployer and lose each dev's own history -- exactly the record
    the snipe lane's ``fire_on_records`` and the live sizer's deployer ladder read.

    Cost-ordered: ONE ``eth_getTransactionByHash`` per launch (17 CU); only when its
    ``from`` differs from the creator word is the creator's code read (26 CU), cached per
    address, so a reused launcher costs it once. An EIP-7702 delegated EOA has code but is
    an EOA (:data:`EIP7702_PREFIX`) and keeps the event's word, as does an EOA creator
    submitted by someone else (a relayer). Any failure keeps the event's word and says so.
    """

    def __init__(self, rpc: Callable[[Sequence[tuple[str, list[Any]]]], list[Any] | None] | None = None, *,
                 cache_size: int = 20_000) -> None:
        self._rpc = rpc or _bsc_rpc_default
        self._is_contract: OrderedDict[str, bool] = OrderedDict()
        self._cache_size = max(1, int(cache_size))
        self.calls = 0
        self.cu = 0

    def _send(self, calls: Sequence[tuple[str, list[Any]]]) -> list[Any] | None:
        self.calls += len(calls)
        self.cu += sum(CU_GET_TX if m == "eth_getTransactionByHash" else CU_GET_CODE for m, _ in calls)
        return self._rpc(calls)

    def is_contract(self, address: str) -> bool | None:
        """Has non-delegation code; ``None`` when unread (never cached)."""
        addr = address.lower()
        if addr in self._is_contract:
            self._is_contract.move_to_end(addr)
            return self._is_contract[addr]
        got = self._send([("eth_getCode", [addr, "latest"])])
        code = got[0] if got else None
        if not isinstance(code, str) or not code.startswith("0x"):
            return None
        contract = len(code) > 2 and not code.lower().startswith(EIP7702_PREFIX)
        self._is_contract[addr] = contract
        while len(self._is_contract) > self._cache_size:
            self._is_contract.popitem(last=False)
        return contract

    def resolve(self, launch: lf.Launch) -> Sender:
        creator = launch.creator.lower() if launch.creator else None
        if not launch.tx:
            return Sender(creator, creator, None, "unresolved:no_tx")
        try:
            got = self._send([("eth_getTransactionByHash", [launch.tx])])
            tx = got[0] if got else None
            sender = str(tx.get("from") or "").lower() if isinstance(tx, Mapping) else ""
            if not looks_evm(sender):
                return Sender(creator, creator, None, "unresolved:no_tx_from")
            if creator is None:
                return Sender(sender, None, sender, "tx_from_no_event_creator")
            if sender == creator:
                return Sender(creator, creator, sender, "creator_is_sender")
            contract = self.is_contract(creator)
        except Exception as exc:  # noqa: BLE001 - a lookup failure never costs the row
            return Sender(creator, creator, None, f"unresolved:{type(exc).__name__}")
        if contract is None:
            return Sender(creator, creator, sender, "unresolved:creator_code_unread")
        if contract:
            return Sender(sender, creator, sender, "sender_of_contract_creator")
        return Sender(creator, creator, sender, "creator_eoa_not_sender")


def _prior_sender(conn: Any, launch: lf.Launch) -> Sender | None:
    """A resolved sender already on this token's row (a re-backfilled launch): reuse it."""
    try:
        row = fetch_one(conn, "SELECT creator, meta_json FROM tokens WHERE chain=? AND address=?",
                        (CHAIN.value, launch.token))
    except Exception:  # noqa: BLE001 - no reuse is only a lookup spent
        return None
    if row is None:
        return None
    meta = jload(row.get("meta_json"), {}) or {}
    if not isinstance(meta, dict) or meta.get("source") != SOURCE:
        return None
    prior = Sender(row.get("creator"), meta.get("event_creator"), meta.get("tx_from"),
                   str(meta.get("creator_basis") or "event_creator"))
    return prior if prior.resolved else None


# --------------------------------------------------------------------------------------
# the rows
# --------------------------------------------------------------------------------------


def token_from_launch(launch: lf.Launch, sender: Sender | None = None) -> Token:
    who = sender or Sender(launch.creator, launch.creator, None, "event_creator")
    return Token(
        address=launch.token,
        chain=CHAIN,
        symbol=launch.symbol,
        name=launch.name,
        creator=who.creator,
        created_ms=launch.launched_ms,
        launchpad=LAUNCHPAD,
        pool=None,
        decimals=None,
        meta={
            "block": launch.block,
            "signature": launch.tx,
            "log_index": launch.meta.get("log_index"),
            "nonce": launch.meta.get("nonce"),
            "meta_uri": launch.meta.get("meta_uri"),
            "portal": FLAP_PORTAL,
            "source": SOURCE,
            "source_ms": launch.launched_ms,
            "event_creator": launch.creator,
            "tx_from": who.tx_from,
            "creator_basis": who.basis,
        },
    )


def record_new_token(token: Token, conn: Any = None) -> str:
    """Write the launch's row. ``"inserted"``, ``"refreshed"`` or ``"filled"``.

    * no row -> inserted (``first_seen_ms`` now).
    * our row (``source`` = :data:`SOURCE`), or an ownerless one -> refreshed with the
      chain's facts; ``first_seen_ms``, ``decimals`` and a graduation pool are never
      clobbered, keys other writers added to ``meta`` (GMGN's ``gmgn_screened`` latch) are
      kept, and a resolved creator is not replaced by an unresolved one.
    * a row another writer owns (GMGN's, seen first: a backfill after a restart) -> only
      its NULL columns are filled and its ``source`` is kept, so GMGN keeps refreshing it.

    No event (module doc).
    """
    c = conn or get_conn()
    meta = {**dict(token.meta or {}), "source": SOURCE}
    cur = c.execute(
        "INSERT INTO tokens (chain, address, symbol, name, decimals, creator, created_ms, launchpad, pool, "
        "first_seen_ms, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(chain, address) DO NOTHING",
        (token.chain.value, token.address, token.symbol, token.name, token.decimals, token.creator,
         token.created_ms, token.launchpad, token.pool, now_ms(), jdump(meta)),
    )
    if cur.rowcount:
        return "inserted"
    existing = fetch_one(c, "SELECT creator, meta_json FROM tokens WHERE chain=? AND address=?",
                         (token.chain.value, token.address)) or {}
    old = jload(existing.get("meta_json"), {}) or {}
    old = old if isinstance(old, dict) else {}
    if str(old.get("source") or "") in ("", SOURCE):
        creator = token.creator
        new_basis = str(meta.get("creator_basis") or "")
        old_basis = str(old.get("creator_basis") or "")
        if (new_basis.startswith("unresolved") or new_basis == "event_creator") and old_basis and not (
                old_basis.startswith("unresolved") or old_basis == "event_creator"):
            creator = existing.get("creator")
            meta.update({k: old.get(k) for k in ("event_creator", "tx_from", "creator_basis")})
        c.execute(
            "UPDATE tokens SET symbol=?, name=?, creator=?, created_ms=?, launchpad=?, pool=COALESCE(pool, ?), "
            "decimals=COALESCE(decimals, ?), meta_json=? WHERE chain=? AND address=?",
            (token.symbol, token.name, creator, token.created_ms, token.launchpad, token.pool, token.decimals,
             jdump({**old, **meta}), token.chain.value, token.address),
        )
        return "refreshed"
    c.execute(
        "UPDATE tokens SET symbol=COALESCE(symbol, ?), name=COALESCE(name, ?), creator=COALESCE(creator, ?), "
        "created_ms=COALESCE(created_ms, ?), launchpad=COALESCE(launchpad, ?), meta_json=? "
        "WHERE chain=? AND address=?",
        (token.symbol, token.name, token.creator, token.created_ms, token.launchpad,
         jdump({**meta, **old}), token.chain.value, token.address),
    )
    return "filled"


def record_migration(grad: lf.FlapGraduation, conn: Any = None) -> bool:
    """Stamp ``tokens.migrated_ms`` (once) and the DEX pool on a row that exists.

    An UPDATE only: ``True`` when a row was stamped, ``False`` when there is no row for the
    token (one launched before the listener ran, that nobody registered). Inserting a row
    there would leave symbol, name, creator and birth NULL for good, and a held token always
    has a row (``fills`` registers what we buy).
    """
    c = conn or get_conn()
    cur = c.execute(
        "UPDATE tokens SET migrated_ms=COALESCE(migrated_ms, ?), pool=COALESCE(?, pool) WHERE chain=? AND address=?",
        (int(grad.migrated_ms), grad.pool, CHAIN.value, grad.token),
    )
    return bool(cur.rowcount)


# --------------------------------------------------------------------------------------
# persisted state: the cursor and today's CU
# --------------------------------------------------------------------------------------


def _kv_get(conn: Any, key: str) -> dict[str, Any]:
    try:
        row = fetch_one(conn, "SELECT value FROM kv WHERE key=?", (key,))
    except Exception:  # noqa: BLE001 - no state is the cold start
        return {}
    value = jload(row["value"], {}) if row else {}
    return value if isinstance(value, dict) else {}


def _kv_set(conn: Any, key: str, value: Mapping[str, Any]) -> None:
    try:
        conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
                     "value=excluded.value, updated_ms=excluded.updated_ms", (key, jdump(dict(value)), now_ms()))
    except Exception as exc:  # noqa: BLE001 - bookkeeping never breaks the listener
        log.debug("flap: kv %s not written: %s", key, exc)


def read_cursor(conn: Any) -> int | None:
    block = _kv_get(conn, CURSOR_KEY).get("block")
    try:
        return int(block) if block is not None and int(block) > 0 else None
    except (TypeError, ValueError):
        return None


def write_cursor(conn: Any, block: int | None) -> None:
    if block:
        _kv_set(conn, CURSOR_KEY, {"block": int(block)})


def _utc_day(at_ms: int) -> str:
    return datetime.fromtimestamp(at_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


class CuMeter:
    """Today's (UTC) estimated CU for this feed, carried across restarts in kv."""

    def __init__(self, day: str, cu: float = 0.0) -> None:
        self.day, self.cu = day, float(cu)

    @classmethod
    def load(cls, conn: Any, *, at_ms: int | None = None) -> CuMeter:
        today = _utc_day(at_ms or now_ms())
        saved = _kv_get(conn, CU_DAY_KEY)
        try:
            return cls(today, float(saved.get("cu") or 0) if saved.get("day") == today else 0.0)
        except (TypeError, ValueError):
            return cls(today)

    def add(self, cu: float, *, at_ms: int | None = None) -> float:
        today = _utc_day(at_ms or now_ms())
        if today != self.day:
            self.day, self.cu = today, 0.0
        self.cu += max(0.0, float(cu))
        return self.cu

    def save(self, conn: Any) -> None:
        _kv_set(conn, CU_DAY_KEY, {"day": self.day, "cu": round(self.cu, 1)})


# --------------------------------------------------------------------------------------
# the listener
# --------------------------------------------------------------------------------------


async def _watch(stop: asyncio.Event, session: asyncio.Event, pause_reason: Callable[[], str | None],
                 recheck_s: float) -> None:
    """End the session when ``stop`` is set or there is a reason to pause."""
    while not session.is_set():
        await aws._wait(stop, recheck_s)  # noqa: SLF001
        if stop.is_set() or pause_reason():
            session.set()


async def run(
    stop: asyncio.Event | None = None,
    *,
    url: str | None = None,
    connect: Callable[[str], Any] | None = None,
    writer: Any = None,
    enabled: Callable[[], bool] | None = None,
    on_status: Callable[[dict[str, Any]], None] | None = None,
    max_attempts: int | None = None,
    resolver: SenderResolver | None = None,
    max_cu_per_day: float | None = None,
    recheck_s: float = RECHECK_EVERY_S,
) -> dict[str, Any]:
    """Follow the Flap portal until ``stop``. Shaped for ``kaiba.ingest.runner``."""
    stop = stop or asyncio.Event()
    wants = enabled or lane_wants_bsc
    if not wants():
        log.info("flap: launch-snipe does not list bsc; idle")
        return {"idle": "launch-snipe params.chains has no bsc"}
    url = url or alchemy_url()
    if not url:
        log.warning("flap: no dedicated Alchemy endpoint for bsc (%s with /v2/); idle", lf.BSC_SNIPE_RPC_ENV)
        return {"idle": "no websocket endpoint"}
    w = writer if writer is not None else get_conn()
    p = feed_params()
    ceiling = float(max_cu_per_day) if max_cu_per_day is not None else _max_cu_per_day(p)
    if resolver is None and bool(p.get("bsc_resolve_sender", True)):
        resolver = SenderResolver()
    stats = lf.LaunchFeedStats()
    counts = {"launches": 0, "graduations": 0, "graduations_no_row": 0, "errors": 0, "pauses": 0}
    meter = CuMeter.load(w)
    state: dict[str, Any] = {"unreported": 0, "last_stats": time.monotonic(), "cu_seen": 0.0,
                             "cursor": read_cursor(w), "paused": None}
    emit(EventKind.SYSTEM, {"component": f"ingest.{FEED}", "status": "starting", "chain": CHAIN.value,
                            "endpoint": aws.mask_url(url), "cursor": state["cursor"]}, chain=CHAIN, conn=w)

    def _cu_total() -> float:
        return (stats.notification_bytes * aws.CU_PER_NOTIFICATION_BYTE + stats.get_logs_calls * aws.CU_PER_GET_LOGS
                + stats.connects * aws.CU_PER_SUBSCRIBE_CALL + (resolver.cu if resolver is not None else 0))

    def _account() -> float:
        total = _cu_total()
        today = meter.add(total - state["cu_seen"])
        state["cu_seen"] = total
        return today

    def _pause_reason() -> str | None:
        try:
            on = bool(wants())
        except Exception:  # noqa: BLE001 - an unreadable switch is off
            on = False
        if not on:
            return "lane_dropped_bsc"
        today = _account()
        if ceiling > 0 and today >= ceiling:
            return f"cu_ceiling:{int(today)}>={int(ceiling)}"
        return None

    def _report() -> None:
        state["last_stats"] = time.monotonic()
        try:
            from kaiba.ingest.runner import note_events

            note_events(FEED, state["unreported"], w)
        except Exception as exc:  # noqa: BLE001 - bookkeeping never breaks the listener
            log.debug("flap: ingest_status not updated: %s", exc)
        state["unreported"] = 0
        write_cursor(w, state["cursor"])
        today = _account()
        meter.save(w)
        emit(EventKind.SYSTEM, {"component": f"ingest.{FEED}", "status": "stats", **counts,
                                "socket": stats.as_dict(), "cursor": state["cursor"],
                                "cu_estimate_process": round(_cu_total(), 1), "cu_today": round(today, 1),
                                "cu_ceiling_per_day": ceiling,
                                "sender": ({"calls": resolver.calls, "cu": resolver.cu} if resolver is not None
                                           else "off")},
             chain=CHAIN, conn=w)

    def _status(payload: dict[str, Any]) -> None:
        if payload.get("event") == "gap_truncated":
            # Graduations in the lost blocks are not stamped: a held token among them reads
            # as a rug to protection. Loud, so the lead can stamp them by hand.
            emit(EventKind.SYSTEM, {"component": f"ingest.{FEED}", "status": "gap_truncated",
                                    **{k: v for k, v in payload.items() if k != "event"}},
                 chain=CHAIN, level="warn", conn=w)
        if on_status is not None:
            on_status(payload)

    async def _handle(item: Any) -> None:
        if isinstance(item, lf.FlapGraduation):
            if record_migration(item, w):
                counts["graduations"] += 1
            else:
                counts["graduations_no_row"] += 1
        else:
            sender: Sender | None = None
            if resolver is not None:
                sender = _prior_sender(w, item) if item.backfilled else None
                if sender is None:
                    sender = await asyncio.to_thread(resolver.resolve, item)
            record_new_token(token_from_launch(item, sender), w)
            counts["launches"] += 1

    try:
        while not stop.is_set():
            why = _pause_reason()
            if why:
                if state["paused"] is None:
                    counts["pauses"] += 1
                    emit(EventKind.SYSTEM, {"component": f"ingest.{FEED}", "status": "paused", "reason": why,
                                            "cursor": state["cursor"]}, chain=CHAIN, level="warn", conn=w)
                state["paused"] = why
                await aws._wait(stop, recheck_s)  # noqa: SLF001
                continue
            if state["paused"] is not None:
                emit(EventKind.SYSTEM, {"component": f"ingest.{FEED}", "status": "resumed",
                                        "after": state["paused"], "cursor": state["cursor"]}, chain=CHAIN, conn=w)
                state["paused"] = None
            session = asyncio.Event()
            watcher = asyncio.create_task(_watch(stop, session, _pause_reason, recheck_s))
            try:
                async for item in lf.stream_flap(url, stop=session, connect=connect, stats=stats, on_status=_status,
                                                 max_attempts=max_attempts, start_block=state["cursor"]):
                    try:
                        await _handle(item)
                        state["unreported"] += 1
                    except Exception as exc:  # noqa: BLE001 - one bad row never stops the feed
                        counts["errors"] += 1
                        log.warning("flap: write failed for %s: %s", str(getattr(item, "token", "?"))[:14],
                                    aws.redact(exc, url))
                    if item.block:
                        state["cursor"] = max(int(state["cursor"] or 0), int(item.block))
                    if time.monotonic() - state["last_stats"] > STATS_EVERY_S:
                        _report()
            finally:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
            if not session.is_set() and not stop.is_set():
                break  # the stream gave up (max_attempts): let the runner restart us
    finally:
        with contextlib.suppress(Exception):
            _report()
    return {**counts, "socket": stats.as_dict(), "cursor": state["cursor"]}


__all__: Sequence[str] = (
    "CHAIN", "CURSOR_KEY", "CU_DAY_KEY", "CuMeter", "FEED", "LAUNCHPAD", "SOURCE", "Sender", "SenderResolver",
    "alchemy_url", "feed_params", "lane_wants_bsc", "read_cursor", "record_migration", "record_new_token", "run",
    "token_from_launch", "write_cursor",
)
