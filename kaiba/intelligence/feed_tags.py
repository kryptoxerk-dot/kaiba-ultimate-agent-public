"""``wallet_feed_tags``: GMGN cohort labels rolled up out of ``wallet.trade`` events.

GMGN attaches labels (``smart_degen``, ``kol``, ``wash_trader`` ...) to every trade on its
wallet feeds. ``gmgn_feeds.write_swap`` writes the trade to ``swaps``, which has no tags
column, and the whole row -- labels included -- to a ``wallet.trade`` event. That event was
the ONLY copy, so (a) no ``wallet.trade`` event could ever be deleted, and (b) every reader
of the labels scanned the events table: ``naming.gather_facts`` ran a LIKE over every one
of them, which is why ``wallet_naming`` has not finished a run since 2026-09-24.

MEASURED on the live box 2026-10-02: 336,028 ``wallet.trade`` events in 24 h, ~680 B of
payload each plus four index entries, ~0.4 GB/day. 15.2% are GMGN feed rows; the rest are
robinhood/tracker trades that carry no label, source or feed that any reader extracts.

The table keeps, per ``(chain, address, tag, source)``: ``first_ms``, ``last_ms`` (event bus
times) and ``n`` (events). ``tag = ''`` is the MEMBERSHIP row -- "this wallet appeared on
this source" -- which the readers need for feed membership and per-feed counts.

Life cycle, in the order it has to happen on the box:

1. Migration 032 creates the table. Nothing reads it yet.
2. WRITER. ``gmgn_feeds.write_swap`` rolls each new feed event up in the same transaction
   as the event (:func:`record_event`), so an event and its contribution land together or
   not at all. Its first call stamps :data:`KV_WRITER` with that event's id: every
   ``wallet.trade`` event from that id on is the writer's.
3. BACKFILL. :func:`backfill_step` walks the ``wallet.trade`` events BELOW that id, newest
   first, in id windows. Each window's contribution and the cursor move in ONE
   transaction, so a window is counted exactly once whatever kills the run. Newest first
   is what makes "first ``wallet_name`` seen" exact: every window is older than anything
   already in the table.
4. PARITY. :func:`parity_check` samples wallets and compares, inside one read snapshot per
   wallet, the table against the rows derived from that wallet's own events -- exactly --
   and every reader's old event-based answer against its new table answer.
5. READERS switch to the table on their own once the backfill is complete and one exact
   parity check has passed (:func:`table_ready`). Until then they read events exactly as
   before, so deploying the readers early changes nothing.
6. RETENTION (``kaiba/ops/retention.py``) may delete old ``wallet.trade`` events only while
   :func:`retention_gate` allows it. Once it has deleted anything, the table is the only
   copy, :func:`table_ready` is true for good, and parity switches to containment.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kaiba.core.db import jdump, tx

log = logging.getLogger(__name__)

TABLE = "wallet_feed_tags"
EVENT_KIND = "wallet.trade"  # EventKind.WALLET_TRADE.value, spelt here so core stays the only import
MEMBERSHIP_TAG = ""
GMGN_PREFIX = "gmgn:"

#: ``{"first_event_id", "first_ms"}`` -- the first event the live writer rolled up.
KV_WRITER = "wallet_feed_tags:writer"
#: The backfill's cursor and outcome. See :func:`backfill_step`.
KV_BACKFILL = "wallet_feed_tags:backfill"
#: The latest parity result, plus the sticky ``first_exact_ok_ms``.
KV_PARITY = "wallet_feed_tags:parity"
#: Written by ``kaiba.ops.retention``: the highest event id its ``wallet.trade`` delete has
#: passed. Non-zero means events are gone and the table is the only copy.
KV_RETENTION = "retention:events:wallet.trade"

#: A parity pass over fewer wallets than this does not count toward readiness: an empty
#: sample passes vacuously. INVENTED floor.
PARITY_MIN_SAMPLE = 20
#: A sampled wallet with more events than this is skipped (and counted), so one robinhood
#: bot cannot make a parity run unbounded. INVENTED; MEASURED p99 is far below it.
PARITY_MAX_EVENTS = 20_000

_COLS = "chain, address, tag, source, first_ms, last_ms, n, wallet_name"

#: The live writer: events arrive in id order, so the name already stored is the older one.
WRITER_UPSERT_SQL = (
    f"INSERT INTO {TABLE} ({_COLS}) VALUES (?,?,?,?,?,?,?,?) "
    "ON CONFLICT(chain, address, tag, source) DO UPDATE SET "
    "first_ms = MIN(first_ms, excluded.first_ms), last_ms = MAX(last_ms, excluded.last_ms), "
    "n = n + excluded.n, wallet_name = COALESCE(wallet_name, excluded.wallet_name)"
)
#: The backfill walks newest-first, so every window it adds is OLDER than what is stored:
#: its name, when it has one, is the earlier one.
BACKFILL_UPSERT_SQL = WRITER_UPSERT_SQL.replace(
    "COALESCE(wallet_name, excluded.wallet_name)", "COALESCE(excluded.wallet_name, wallet_name)"
)

Key = tuple[str, str, str, str]  # (chain, address, tag, source)


@dataclass(frozen=True)
class TagRow:
    chain: str
    address: str
    tag: str
    source: str
    first_ms: int
    last_ms: int
    n: int
    wallet_name: str | None = None

    @property
    def key(self) -> Key:
        return (self.chain, self.address, self.tag, self.source)

    @property
    def is_gmgn(self) -> bool:
        return self.source.startswith(GMGN_PREFIX)

    def params(self) -> tuple[Any, ...]:
        return (self.chain, self.address, self.tag, self.source, self.first_ms, self.last_ms,
                self.n, self.wallet_name)


# --------------------------------------------------------------------------------------
# what one event contributes
# --------------------------------------------------------------------------------------


def _payload(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def contribution(
    chain: str | None, subject: str | None, payload: Any
) -> tuple[str, str, str, list[str], str | None] | None:
    """``(chain, address, source, tags, wallet_name)`` for one ``wallet.trade`` event, or
    ``None`` when the event carries nothing any reader extracts.

    An event counts when it is a GMGN feed row (``source`` ``gmgn:*`` or a ``feed``) or
    carries a non-empty ``tags`` list -- the union of what the four readers filter on:
    grade (any event with tags), discover (``feed``), naming (``"feed":"``) and the gather
    queue (``"source":"gmgn:``). MEASURED 2026-10-02 over 46,911 box events: every event
    with a ``feed`` has ``source == "gmgn:" + feed``, tags arrive only on those, and none
    carries a duplicate, padded or upper-case tag -- so the readers' different spellings
    (``str``, ``strip``, ``strip().lower()``) agree on every row seen.
    """
    p = _payload(payload)
    if p is None:
        return None
    ch = chain or p.get("chain")
    address = subject or p.get("wallet")
    if not ch or not address:
        return None
    raw_tags = p.get("tags")
    tags: dict[str, None] = {}
    if isinstance(raw_tags, list):
        for t in raw_tags:
            s = str(t).strip()
            if s:
                tags.setdefault(s, None)
    source, feed = p.get("source"), p.get("feed")
    if isinstance(source, str) and source:
        src = source
    elif isinstance(feed, str) and feed:
        src = f"{GMGN_PREFIX}{feed}"
    else:
        src = ""
    if not (src.startswith(GMGN_PREFIX) or (isinstance(feed, str) and feed) or tags):
        return None
    name = p.get("wallet_name")
    return str(ch), str(address), src, list(tags), (str(name) if name else None)


def rows_from_events(events: Iterable[Sequence[Any]]) -> dict[Key, TagRow]:
    """Aggregate ``(ts_ms, chain, subject, payload)`` events, given in ascending id order,
    into the rows the table should hold for them. The writer, the backfill, the parity
    check and the not-yet-ready read path all use this one function."""
    acc: dict[Key, list[Any]] = {}
    for ts_ms, chain, subject, payload in events:
        got = contribution(chain, subject, payload)
        if got is None:
            continue
        ch, address, src, tags, name = got
        ts = int(ts_ms)
        for tag in (MEMBERSHIP_TAG, *tags):
            key = (ch, address, tag, src)
            cur = acc.get(key)
            row_name = name if tag == MEMBERSHIP_TAG else None
            if cur is None:
                acc[key] = [ts, ts, 1, row_name]
            else:
                cur[0] = min(cur[0], ts)
                cur[1] = max(cur[1], ts)
                cur[2] += 1
                if cur[3] is None and row_name:
                    cur[3] = row_name
    return {k: TagRow(*k, v[0], v[1], v[2], v[3]) for k, v in acc.items()}


# --------------------------------------------------------------------------------------
# kv
# --------------------------------------------------------------------------------------


def _kv_get(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    try:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _kv_set(conn: sqlite3.Connection, key: str, value: Mapping[str, Any], now: int) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ms = excluded.updated_ms",
        (key, jdump(dict(value)), int(now)),
    )


def table_exists(conn: sqlite3.Connection) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def writer_marker(conn: sqlite3.Connection) -> dict[str, Any] | None:
    return _kv_get(conn, KV_WRITER)


def backfill_state(conn: sqlite3.Connection) -> dict[str, Any]:
    return _kv_get(conn, KV_BACKFILL) or {}


def parity_record(conn: sqlite3.Connection) -> dict[str, Any]:
    return _kv_get(conn, KV_PARITY) or {}


def deleted_through(conn: sqlite3.Connection) -> int:
    state = _kv_get(conn, KV_RETENTION) or {}
    try:
        return int(state.get("deleted_through_id") or 0)
    except (TypeError, ValueError):
        return 0


def table_ready(conn: sqlite3.Connection) -> bool:
    """Whether readers should read the table instead of the events.

    True once the backfill is complete AND an exact parity check has passed at least once
    (sticky: a later failure is reported loudly by the job, but does not send readers back
    to events). Also true, unconditionally, once retention has deleted any event: from then
    on the events are no longer a complete source and the table is the only one.
    """
    if not table_exists(conn):
        return False
    if deleted_through(conn) > 0:
        return True
    return bool(backfill_state(conn).get("complete")) and bool(parity_record(conn).get("first_exact_ok_ms"))


# --------------------------------------------------------------------------------------
# the writer
# --------------------------------------------------------------------------------------


def record_event(
    conn: sqlite3.Connection, event_id: int, *, chain: str | None, subject: str | None,
    payload: Mapping[str, Any],
) -> int:
    """Roll one freshly emitted ``wallet.trade`` event up. Call it in the event's transaction.

    Uses the event row's own ``ts_ms`` so the rollup equals what a later re-derivation from
    the event would give. Stamps :data:`KV_WRITER` on first use (INSERT OR IGNORE: the first
    id ever recorded stands). Returns the number of rows upserted.
    """
    row = conn.execute("SELECT ts_ms FROM events WHERE id = ?", (int(event_id),)).fetchone()
    if row is None:
        return 0
    ts = int(row[0])
    conn.execute(
        "INSERT OR IGNORE INTO kv (key, value, updated_ms) VALUES (?,?,?)",
        (KV_WRITER, jdump({"first_event_id": int(event_id), "first_ms": ts}), ts),
    )
    rows = rows_from_events([(ts, chain, subject, dict(payload))])
    if rows:
        conn.executemany(WRITER_UPSERT_SQL, [r.params() for r in rows.values()])
    return len(rows)


# --------------------------------------------------------------------------------------
# reading the table
# --------------------------------------------------------------------------------------

_IN_CHUNK = 400


def _to_row(r: Sequence[Any]) -> TagRow:
    return TagRow(str(r[0]), str(r[1]), str(r[2]), str(r[3]), int(r[4]), int(r[5]), int(r[6]),
                  None if r[7] is None else str(r[7]))


def wallet_rows(conn: sqlite3.Connection, chain: str, addresses: Iterable[str]) -> dict[str, list[TagRow]]:
    """Table rows for these wallets on one chain, ``address -> rows`` ordered by
    ``(first_ms, tag, source)``. Primary-key seeks; addresses with no row are absent."""
    out: dict[str, list[TagRow]] = {}
    wanted = sorted({str(a) for a in addresses if a})
    for i in range(0, len(wanted), _IN_CHUNK):
        batch = wanted[i : i + _IN_CHUNK]
        for r in conn.execute(
            f"SELECT {_COLS} FROM {TABLE} WHERE chain = ? AND address IN ({','.join('?' for _ in batch)}) "
            "ORDER BY address, first_ms, tag, source",
            (chain, *batch),
        ):
            row = _to_row(r)
            out.setdefault(row.address, []).append(row)
    return out


def chain_rows(conn: sqlite3.Connection, chain: str | None, *, gmgn_only: bool = False) -> list[TagRow]:
    """Every row on one chain, or on all of them for ``None`` (a primary-key prefix scan;
    the table is small -- one row per wallet, label and feed)."""
    where, params = ("WHERE chain = ?", (chain,)) if chain is not None else ("WHERE 1", ())
    sql = f"SELECT {_COLS} FROM {TABLE} {where}"
    if gmgn_only:
        sql += " AND source LIKE 'gmgn:%'"
    sql += " ORDER BY chain, address, first_ms, tag, source"
    return [_to_row(r) for r in conn.execute(sql, params)]


# --------------------------------------------------------------------------------------
# what each reader takes from a wallet's rows
# --------------------------------------------------------------------------------------


def provider_tags(rows: Iterable[TagRow]) -> list[str]:
    """grade: every label from any source, first seen first (``first_ms``, then the tag)."""
    first: dict[str, int] = {}
    for r in rows:
        if r.tag != MEMBERSHIP_TAG:
            first[r.tag] = min(first.get(r.tag, r.first_ms), r.first_ms)
    return [t for t, _ in sorted(first.items(), key=lambda kv: (kv[1], kv[0]))]


def feed_view(rows: Iterable[TagRow]) -> tuple[list[str], str | None, bool]:
    """discover: ``(labels, first wallet name, seen on a feed)`` over GMGN feed rows only."""
    feed = [r for r in rows if r.is_gmgn]
    seen = any(r.tag == MEMBERSHIP_TAG for r in feed)
    named = sorted((r for r in feed if r.tag == MEMBERSHIP_TAG and r.wallet_name), key=lambda r: r.first_ms)
    return provider_tags(feed), (named[0].wallet_name if named else None), seen


def naming_counts(rows: Iterable[TagRow]) -> tuple[dict[str, int], dict[str, int]]:
    """naming: ``(label -> feed events carrying it, feed -> feed events)``, labels lower-cased."""
    tags: dict[str, int] = {}
    feeds: dict[str, int] = {}
    for r in rows:
        if not r.is_gmgn:
            continue
        if r.tag == MEMBERSHIP_TAG:
            feed = r.source[len(GMGN_PREFIX):]
            if feed:
                feeds[feed] = feeds.get(feed, 0) + r.n
            continue
        label = r.tag.strip().lower()
        if label:
            tags[label] = tags.get(label, 0) + r.n
    return tags, feeds


def gather_signals(rows: Iterable[TagRow], positive: Iterable[str], negative: Iterable[str]) -> tuple[bool, int]:
    """scheduler.gather_queue: ``(on a gmgn feed, bit 1 positive label | bit 2 negative label)``."""
    pos, neg = set(positive), set(negative)
    on_feed, bits = False, 0
    for r in rows:
        if not r.is_gmgn:
            continue
        if r.tag == MEMBERSHIP_TAG:
            on_feed = True
        elif r.tag in pos:
            bits |= 1
        elif r.tag in neg:
            bits |= 2
    return on_feed, bits


# --------------------------------------------------------------------------------------
# events of one wallet (parity, and the incremental namer before the table is ready)
# --------------------------------------------------------------------------------------

#: ``+kind`` keeps the planner on idx_events_subj: idx_events_kind would walk every
#: wallet.trade event in the table. tests/test_feed_tags.py holds it to EXPLAIN.
WALLET_EVENTS_SQL = (
    "SELECT id, ts_ms, chain, subject, payload FROM events "
    "WHERE subject = ? AND +kind = 'wallet.trade' AND chain = ? ORDER BY id LIMIT ?"
)


def wallet_events(
    conn: sqlite3.Connection, chain: str, address: str, *, limit: int = PARITY_MAX_EVENTS
) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in conn.execute(WALLET_EVENTS_SQL, (address, chain, int(limit)))]


# --------------------------------------------------------------------------------------
# backfill
# --------------------------------------------------------------------------------------

#: The id-window read. idx_events_kind (kind, id) makes it a bounded range.
BACKFILL_READ_SQL = (
    "SELECT id, ts_ms, chain, subject, payload FROM events "
    "WHERE kind = 'wallet.trade' AND id >= ? AND id < ? ORDER BY id"
)


def _min_wallet_trade_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT min(id) FROM events WHERE kind = 'wallet.trade'").fetchone()
    return int(row[0]) if row and row[0] is not None else None


def backfill_step(
    conn: sqlite3.Connection,
    *,
    deadline_ms: int,
    window_ids: int = 20_000,
    sleep_s: float = 0.0,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Roll up the ``wallet.trade`` events below the writer's first id, newest window first.

    Bounded by ``deadline_ms``; resumable from :data:`KV_BACKFILL`. Each window is read
    OUTSIDE the write lock (cold pages on an IO-saturated box), then its rows and the
    cursor are written in one short transaction that first re-checks the cursor, so two
    runs can never add the same window twice. Refuses to start before the writer is live:
    without :data:`KV_WRITER` there is no id below which the events are known to be ours.
    """
    if conn.in_transaction:
        raise RuntimeError("backfill_step needs a connection with no open transaction")
    if not table_exists(conn):
        return {"phase": "no_table", "complete": False}
    writer = writer_marker(conn)
    if not writer or writer.get("first_event_id") is None:
        return {"phase": "waiting_for_writer", "complete": False,
                "note": "no wallet feed event has been rolled up by the live writer yet"}
    ceiling = int(writer["first_event_id"])
    now = int(clock() * 1000)
    state = backfill_state(conn)
    if not state:
        floor = _min_wallet_trade_id(conn)
        state = {"ceiling": ceiling, "floor": ceiling if floor is None else min(floor, ceiling),
                 "cursor": ceiling, "complete": False, "started_ms": now, "completed_ms": None,
                 "events_read": 0, "events_rolled": 0, "rows_upserted": 0, "windows": 0}
        state["complete"] = state["cursor"] <= state["floor"]
        if state["complete"]:
            state["completed_ms"] = now
        with tx(conn):
            if backfill_state(conn):
                return {"phase": "raced", "complete": False}
            _kv_set(conn, KV_BACKFILL, state, now)
    if int(state.get("ceiling", ceiling)) != ceiling:
        raise RuntimeError(
            f"{KV_WRITER} moved from {state.get('ceiling')} to {ceiling}; the backfill range is "
            "no longer known to be disjoint from the writer's. Investigate before continuing."
        )
    run = {"windows": 0, "events_read": 0, "events_rolled": 0, "rows_upserted": 0}
    window = max(1, int(window_ids))
    while not state["complete"] and int(clock() * 1000) < deadline_ms:
        hi = int(state["cursor"])
        lo = max(int(state["floor"]), hi - window)
        events = conn.execute(BACKFILL_READ_SQL, (lo, hi)).fetchall()
        rows = rows_from_events((e[1], e[2], e[3], e[4]) for e in events)
        rolled = sum(1 for e in events if contribution(e[2], e[3], e[4]) is not None)
        with tx(conn):
            current = backfill_state(conn)
            if int(current.get("cursor", -1)) != hi:
                return {"phase": "raced", **run, "cursor": current.get("cursor")}
            if rows:
                conn.executemany(BACKFILL_UPSERT_SQL, [r.params() for r in rows.values()])
            state = {**current, "cursor": lo,
                     "events_read": int(current.get("events_read", 0)) + len(events),
                     "events_rolled": int(current.get("events_rolled", 0)) + rolled,
                     "rows_upserted": int(current.get("rows_upserted", 0)) + len(rows),
                     "windows": int(current.get("windows", 0)) + 1}
            if lo <= int(state["floor"]):
                state["complete"] = True
                state["completed_ms"] = int(clock() * 1000)
            _kv_set(conn, KV_BACKFILL, state, int(clock() * 1000))
        run["windows"] += 1
        run["events_read"] += len(events)
        run["events_rolled"] += rolled
        run["rows_upserted"] += len(rows)
        if sleep_s and not state["complete"]:
            sleep(sleep_s)
    span = max(1, int(state["ceiling"]) - int(state["floor"]))
    return {
        "phase": "complete" if state["complete"] else "backfilling",
        "complete": bool(state["complete"]), **run,
        "cursor": state["cursor"], "floor": state["floor"], "ceiling": state["ceiling"],
        "done_pct": round(100.0 * (int(state["ceiling"]) - int(state["cursor"])) / span, 2),
        "events_read_total": state.get("events_read"), "rows_upserted_total": state.get("rows_upserted"),
    }


# --------------------------------------------------------------------------------------
# parity
# --------------------------------------------------------------------------------------


def _sample_wallets(
    conn: sqlite3.Connection, *, sample: int, recent_ids: int, rng: random.Random
) -> list[tuple[str, str]]:
    """Up to ``sample`` (chain, address) pairs from three places: random table rows, every
    feed wallet in the newest ``recent_ids`` events (catches a writer that is not running),
    and random older feed events (catches a backfill gap the table cannot show)."""
    picked: dict[tuple[str, str], None] = {}
    third = max(1, sample // 3)
    if table_exists(conn):
        for r in conn.execute(
            f"SELECT chain, address FROM {TABLE} WHERE tag = '' ORDER BY random() LIMIT ?", (third,)
        ):
            picked.setdefault((str(r[0]), str(r[1])), None)
    top = conn.execute("SELECT max(id) FROM events").fetchone()[0]
    floor = _min_wallet_trade_id(conn)
    if top is None or floor is None:
        return list(picked)[:sample]
    recent: list[tuple[str, str]] = []
    for r in conn.execute(
        "SELECT chain, subject, payload FROM events WHERE kind = 'wallet.trade' AND id > ? ORDER BY id DESC",
        (int(top) - int(recent_ids),),
    ):
        got = contribution(r[0], r[1], r[2])
        if got is not None:
            recent.append((got[0], got[1]))
    for key in rng.sample(recent, min(len(recent), third)) if recent else []:
        picked.setdefault(key, None)
    tries = 0
    while len(picked) < sample and tries < sample * 3:
        tries += 1
        start = rng.randint(int(floor), int(top))
        for r in conn.execute(
            "SELECT chain, subject, payload FROM events WHERE kind = 'wallet.trade' AND id >= ? "
            "ORDER BY id LIMIT 200",
            (start,),
        ):
            got = contribution(r[0], r[1], r[2])
            if got is not None:
                picked.setdefault((got[0], got[1]), None)
                break
    return list(picked)[:sample]


def _containment_ok(derived: Mapping[Key, TagRow], table: Mapping[Key, TagRow]) -> bool:
    for key, ev in derived.items():
        t = table.get(key)
        if t is None or t.n < ev.n or t.first_ms > ev.first_ms or t.last_ms < ev.last_ms:
            return False
    return True


def _legacy_gather_signals(events: Sequence[Sequence[Any]]) -> tuple[bool, int]:
    """scheduler.gather_queue's event reader, for one wallet, as it reads today."""
    from kaiba.ops.scheduler import GATHER_NEGATIVE_TAGS, GATHER_POSITIVE_TAGS  # lazy: scheduler imports us

    on_feed, bits = False, 0
    for e in events:
        text = e[4] if isinstance(e[4], str) else jdump(e[4])
        if '"source":"gmgn:' not in text:
            continue
        on_feed = True
        raw = (_payload(e[4]) or {}).get("tags") or []
        for tag in raw if isinstance(raw, list) else []:
            if tag in GATHER_POSITIVE_TAGS:
                bits |= 1
            elif tag in GATHER_NEGATIVE_TAGS:
                bits |= 2
    return on_feed, bits


def reader_mismatches(
    conn: sqlite3.Connection, chain: str, address: str, events: Sequence[Sequence[Any]],
    rows: Sequence[TagRow],
) -> list[str]:
    """Each reader's old answer (from events) against its new one (from ``rows``)."""
    from kaiba.core.schemas import Chain
    from kaiba.intelligence import discover, grade, naming
    from kaiba.ops.scheduler import GATHER_NEGATIVE_TAGS, GATHER_POSITIVE_TAGS

    try:
        ch = Chain(chain)
    except ValueError:
        return []
    out: list[str] = []
    old_grade = grade.provider_tags_from_event_rows(conn, ch, address)
    if set(old_grade) != set(provider_tags(rows)):
        out.append("grade")
    old_tags, old_names, old_seen = discover.feed_tags_from_events(conn, ch, [address])
    new_tags, new_name, new_seen = feed_view(rows)
    if (set(old_tags.get(address, [])) != set(new_tags) or old_names.get(address) != new_name
            or (address in old_seen) != new_seen):
        out.append("discover")
    if naming.gmgn_counts_from_events(conn, ch, address) != naming_counts(rows):
        out.append("naming")
    if _legacy_gather_signals(events) != gather_signals(rows, GATHER_POSITIVE_TAGS, GATHER_NEGATIVE_TAGS):
        out.append("gather")
    return out


def parity_check(
    conn: sqlite3.Connection,
    *,
    sample: int = 200,
    recent_ids: int = 5_000,
    rng: random.Random | None = None,
    record: bool = False,
    now_ms: int | None = None,
    wallets: Sequence[tuple[str, str]] | None = None,
    readers: bool = True,
    deadline_ms: int | None = None,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Table rows against the rows derived from each sampled wallet's own events.

    EXACT while no event has been deleted: same keys, same ``n``, same first/last times,
    same name. CONTAINMENT after retention has deleted any: every derived row is in the
    table with at least that ``n`` and a span that covers it. Each wallet is compared inside
    one read snapshot, so a writer committing in between cannot fake a mismatch. In exact
    mode every reader's old answer is also held against its new one.

    ``record`` writes the result to :data:`KV_PARITY`; ``first_exact_ok_ms`` is set the
    first time an exact check passes over at least :data:`PARITY_MIN_SAMPLE` wallets with
    the backfill complete, and is never cleared.

    COST, MEASURED on the box 2026-10-02: a GMGN feed wallet averages ~900 wallet.trade
    events (robinhood wallets: every on-chain trade is one), and the old readers each read
    them again, so a wallet costs ~1.8 s cold. ``deadline_ms`` stops sampling cleanly; a
    run cut short still records what it compared, and only a pass over at least
    :data:`PARITY_MIN_SAMPLE` wallets can make the table ready.
    """
    if not table_exists(conn):
        return {"ok": False, "reason": "no_table"}
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    rng = rng or random.Random()
    gone = deleted_through(conn)
    mode = "exact" if gone == 0 else "containment"
    keys = list(wallets) if wallets is not None else _sample_wallets(
        conn, sample=sample, recent_ids=recent_ids, rng=rng)
    compared = skipped_large = row_bad = 0
    by_reader: dict[str, int] = {}
    examples: list[dict[str, Any]] = []
    own_tx = not conn.in_transaction
    stopped: str | None = None
    for chain, address in keys:
        if deadline_ms is not None and int(clock() * 1000) >= deadline_ms:
            stopped = "deadline"
            break
        if own_tx:
            conn.execute("BEGIN")
        try:
            events = wallet_events(conn, chain, address, limit=PARITY_MAX_EVENTS + 1)
            table = {r.key: r for r in wallet_rows(conn, chain, [address]).get(address, [])}
            if len(events) > PARITY_MAX_EVENTS:
                skipped_large += 1
                continue
            derived = rows_from_events((e[1], e[2], e[3], e[4]) for e in events)
            ok_rows = derived == table if mode == "exact" else _containment_ok(derived, table)
            bad_readers = reader_mismatches(conn, chain, address, events, list(table.values())) \
                if (readers and mode == "exact") else []
        finally:
            if own_tx:
                conn.execute("COMMIT")
        compared += 1
        if not ok_rows:
            row_bad += 1
        for name in bad_readers:
            by_reader[name] = by_reader.get(name, 0) + 1
        if (not ok_rows or bad_readers) and len(examples) < 5:
            only_events = sorted(k[2:] for k in set(derived) - set(table))
            only_table = sorted(k[2:] for k in set(table) - set(derived))
            differ = sorted(k[2:] for k in set(derived) & set(table) if derived[k] != table[k])
            examples.append({"chain": chain, "address": address[:12], "events": len(events),
                             "only_in_events": only_events[:5], "only_in_table": only_table[:5],
                             "differ": differ[:5], "readers": bad_readers})
    ok = compared > 0 and row_bad == 0 and not by_reader
    #: enough wallets compared for the result to stand for the table (a deadline can cut a
    #: run short; a short pass is reported, but neither switches readers nor opens the gate)
    sufficient = compared >= PARITY_MIN_SAMPLE
    backfill = backfill_state(conn)
    result: dict[str, Any] = {
        "ok": ok, "sufficient": sufficient, "mode": mode, "checked_ms": now, "sampled": len(keys),
        "compared": compared,
        "row_mismatches": row_bad, "reader_mismatches": by_reader, "skipped_large": skipped_large,
        "examples": examples, "backfill_complete": bool(backfill.get("complete")),
        "deleted_through_id": gone, "stopped": stopped,
    }
    previous = parity_record(conn)
    first_ok = previous.get("first_exact_ok_ms")
    if not first_ok and ok and sufficient and mode == "exact" and result["backfill_complete"]:
        first_ok = now
    result["first_exact_ok_ms"] = first_ok
    if record:
        with tx(conn):
            _kv_set(conn, KV_PARITY, result, now)
    return result


# --------------------------------------------------------------------------------------
# the retention gate
# --------------------------------------------------------------------------------------


def retention_gate(conn: sqlite3.Connection, *, now_ms: int, parity_max_age_s: int) -> dict[str, Any]:
    """Whether ``wallet.trade`` events may be deleted, and if not, why not.

    All must hold: the table exists, the backfill is complete (so every event below the
    writer's first id was rolled up, and every event from it on was rolled up by the
    writer), an exact parity check has passed at least once, and the LATEST parity result
    passed, compared at least :data:`PARITY_MIN_SAMPLE` wallets and is younger than
    ``parity_max_age_s``.
    """
    if not table_exists(conn):
        return {"ok": False, "reason": "no_table"}
    backfill = backfill_state(conn)
    if not backfill.get("complete"):
        return {"ok": False, "reason": "backfill_incomplete", "cursor": backfill.get("cursor"),
                "floor": backfill.get("floor")}
    parity = parity_record(conn)
    if not parity.get("first_exact_ok_ms"):
        return {"ok": False, "reason": "no_exact_parity_pass"}
    if not parity.get("ok"):
        return {"ok": False, "reason": "latest_parity_failed", "checked_ms": parity.get("checked_ms")}
    if not parity.get("sufficient"):
        return {"ok": False, "reason": "latest_parity_too_small", "compared": parity.get("compared")}
    age_s = (int(now_ms) - int(parity.get("checked_ms") or 0)) / 1000.0
    if age_s > parity_max_age_s:
        return {"ok": False, "reason": "parity_stale", "age_s": round(age_s)}
    return {"ok": True, "reason": "ok", "backfill_ceiling": backfill.get("ceiling"),
            "parity_checked_ms": parity.get("checked_ms")}


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    exists = table_exists(conn)
    return {
        "table": exists,
        "rows": conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] if exists else None,
        "writer": writer_marker(conn),
        "backfill": backfill_state(conn) or None,
        "parity": parity_record(conn) or None,
        "deleted_through_id": deleted_through(conn),
        "ready": table_ready(conn),
    }


# --------------------------------------------------------------------------------------
# CLI: python -m kaiba.intelligence.feed_tags --db data/kaiba.db status|parity|backfill
# --------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kaiba.intelligence.feed_tags")
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("command", choices=("status", "parity", "backfill"))
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--record", action="store_true", help="parity: write the result to kv")
    ap.add_argument("--budget-s", type=float, default=120.0, help="backfill: wall-clock budget")
    ap.add_argument("--window", type=int, default=20_000, help="backfill: event ids per transaction")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
    writes = args.command == "backfill" or args.record
    if writes:
        conn = sqlite3.connect(str(args.db), timeout=10, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=10000")
    else:
        conn = sqlite3.connect(f"{args.db.resolve().as_uri()}?mode=ro", uri=True, timeout=10, isolation_level=None)
        conn.execute("PRAGMA query_only=1")
    try:
        if args.command == "status":
            out: dict[str, Any] = status(conn)
        elif args.command == "parity":
            out = parity_check(conn, sample=args.sample, record=args.record)
        else:
            out = backfill_step(conn, deadline_ms=int((time.time() + args.budget_s) * 1000),
                                window_ids=args.window)
    finally:
        conn.close()
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "BACKFILL_UPSERT_SQL", "KV_BACKFILL", "KV_PARITY", "KV_RETENTION", "KV_WRITER", "MEMBERSHIP_TAG",
    "TABLE", "TagRow", "WRITER_UPSERT_SQL", "backfill_state", "backfill_step", "chain_rows",
    "contribution", "deleted_through", "feed_view", "gather_signals", "naming_counts", "parity_check",
    "parity_record", "provider_tags", "record_event", "retention_gate", "rows_from_events", "status",
    "table_exists", "table_ready", "wallet_events", "wallet_rows", "writer_marker",
]
