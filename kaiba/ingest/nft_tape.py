"""Robinhood Chain NFT tape: OpenSea primary mints and Seaport sales, from the chain alone.

Keyless and read-only. Each run reads SeaDrop ``SeaDropMint`` and Seaport ``OrderFulfilled``
logs forward from a block cursor in ``kv`` and writes ``nft_mints`` / ``nft_fills``
(migration 034). It is the input of :mod:`kaiba.learning.mint_study`, which needs one thing
OpenSea's API cannot give it without a key: what someone actually PAID, as opposed to the
lowest ask.

Resumable and gap-free by construction: the cursor and the rows of a chunk commit in ONE
transaction, so a crash between them cannot skip or double-count a range, and the cursor
only ever advances past a range whose logs AND both anchor blocks were read. A run that
cannot read a range stops and leaves the cursor where it was; nothing is ever skipped to
"catch up".

Budget (the ``robinhood-rpc`` bucket is shared with LIVE stop-losses; MEASURED on the box
2026-10-02: ~1,200 HTTP calls an hour already, 4 of them 429s):

* one HTTP call for the head, then one HTTP batch per chunk: the chunk's end block and
  both log queries, plus its start block only on a cold start (a warm cursor's last
  anchor is the next chunk's start). A steady 15-minute run covers ~8,900 blocks: 2 HTTP
  calls, 4 reads.
* ``max_chunks_per_run`` (6) bounds a catch-up: 60,000 blocks (~1.7 h of chain) per run,
  so a 24 h cold-start backfill completes in ~17 runs without bursting.
* ``chunk_blocks`` (10,000, ~17 min at 0.1015 s/block): the sample's busiest stream was
  300 fills in 20,000 blocks (~650 kB), far inside the RPC's 10,000-log limit. A "matched
  by query exceeds limit" answer halves the chunk rather than failing the run.

Timestamps: ``blockTimestamp`` on a log is used when present (it is ``0x0`` away from the
head, see ``kaiba.ingest.robinhood.log_timestamp_ms``); otherwise a block's time is
interpolated between the two anchor blocks read in the same batch, at most ~17 minutes
apart, and the row says so (``ts_exact = 0``). The study's windows are hours wide, so a
minutes-wide interpolation cannot move a fill across one.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from kaiba.core.db import fetch_one, jdump, jload, tx
from kaiba.core.schemas import now_ms
from kaiba.providers import seaport_rh as sp

log = logging.getLogger(__name__)

CURSOR_KEY = "nft_tape:robinhood:cursor"
#: MEASURED 2026-09-20 over 10,000 and 200,000 block spans (kaiba.ingest.robinhood).
SECONDS_PER_BLOCK = Decimal("0.1015")
#: Errors the public RPC answers when a log query matched too much. The first is the exact
#: text MEASURED 2026-10-02 ("logs matched by query exceeds limit of 10000").
_TOO_MANY = ("exceeds limit", "too many", "query returned more than", "response size")


@dataclass(frozen=True)
class TapeConfig:
    chunk_blocks: int = 10_000
    min_chunk_blocks: int = 500
    max_chunks_per_run: int = 6
    #: Blocks left unread behind the head (~5 s). The sequencer does not reorg in practice;
    #: this only keeps us off blocks whose logs may still be settling.
    head_margin_blocks: int = 50
    #: How far back a cold start begins. INVENTED: one day buys the first decision a full
    #: 24 h evidence window instead of waiting a day for it.
    backfill_hours: float = 24.0
    #: Row retention for nft_mints / nft_fills. INVENTED: 72 h marks + their 6 h windows
    #: need under 4 days; 14 leaves room for a long outage and a re-score.
    retention_days: float = 14.0
    prune_chunk: int = 5_000
    prune_max_chunks: int = 10

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> TapeConfig:
        kw: dict[str, Any] = {}
        for f in ("chunk_blocks", "min_chunk_blocks", "max_chunks_per_run", "head_margin_blocks",
                  "prune_chunk", "prune_max_chunks"):
            if params.get(f) is not None:
                kw[f] = max(1, int(params[f]))
        for f in ("backfill_hours", "retention_days"):
            if params.get(f) is not None:
                kw[f] = max(0.0, float(params[f]))
        return cls(**kw)


@dataclass
class TapeCursor:
    """What the tape has read. ``[first_ts_ms, through_ts_ms]`` is covered with no gap."""

    next_block: int
    first_block: int
    first_ts_ms: int | None = None
    through_block: int | None = None
    through_ts_ms: int | None = None
    base_fee_wei: int | None = None
    updated_ms: int | None = None

    def covers(self, from_ms: int, to_ms: int) -> bool:
        return (self.first_ts_ms is not None and self.through_ts_ms is not None
                and self.first_ts_ms <= from_ms and self.through_ts_ms >= to_ms)


def load_cursor(conn: Any) -> TapeCursor | None:
    row = fetch_one(conn, "SELECT value FROM kv WHERE key=?", (CURSOR_KEY,))
    data = jload(row["value"]) if row else None
    if not isinstance(data, dict):
        return None
    try:
        return TapeCursor(**{k: data.get(k) for k in TapeCursor.__dataclass_fields__})
    except TypeError:
        return None


def _save_cursor(conn: Any, cursor: TapeCursor) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ms=excluded.updated_ms",
        (CURSOR_KEY, jdump(asdict(cursor)), cursor.updated_ms or now_ms()),
    )


@dataclass
class TapeResult:
    ok: bool = True
    note: str | None = None
    head: int | None = None
    from_block: int | None = None
    to_block: int | None = None
    chunks: int = 0
    rpc_calls: int = 0
    mints: int = 0
    fills: Counter = field(default_factory=Counter)      # listing / offer written
    skipped: Counter = field(default_factory=Counter)    # counter / other / undecodable
    halvings: int = 0
    behind_blocks: int | None = None
    pruned: int = 0
    cursor: TapeCursor | None = None

    def receipt(self) -> dict[str, Any]:
        c = self.cursor
        return {
            "ok": self.ok, "note": self.note, "head": self.head,
            "from_block": self.from_block, "to_block": self.to_block,
            "chunks": self.chunks, "rpc_calls": self.rpc_calls, "mints": self.mints,
            "fills": dict(self.fills), "skipped": dict(self.skipped), "halvings": self.halvings,
            "behind_blocks": self.behind_blocks, "pruned": self.pruned,
            "covered_hours": (round((c.through_ts_ms - c.first_ts_ms) / 3_600_000, 2)
                              if c and c.first_ts_ms is not None and c.through_ts_ms is not None else None),
        }


def blocks_for_hours(hours: float) -> int:
    return int(Decimal(str(hours)) * 3600 / SECONDS_PER_BLOCK)


def _block_anchor(result: Any) -> tuple[int, int | None] | None:
    """``eth_getBlockByNumber`` -> (timestamp ms, baseFeePerGas), or ``None``."""
    if not isinstance(result, Mapping):
        return None
    ts = sp.hex_int(result.get("timestamp"))
    if ts is None or ts <= 0:
        return None
    return ts * 1000, sp.hex_int(result.get("baseFeePerGas"))


def interpolate_ms(block: int, from_block: int, from_ms: int, to_block: int, to_ms: int) -> int:
    """Block time between two anchors, integer arithmetic, clamped to the anchors."""
    if to_block <= from_block:
        return from_ms
    b = min(max(block, from_block), to_block)
    return from_ms + (b - from_block) * (to_ms - from_ms) // (to_block - from_block)


def _too_many(note: str | None) -> bool:
    text = (note or "").lower()
    return any(marker in text for marker in _TOO_MANY)


def persist_chunk(
    conn: Any,
    mint_logs: Sequence[Any],
    fill_logs: Sequence[Any],
    *,
    from_block: int,
    from_ms: int,
    to_block: int,
    to_ms: int,
) -> tuple[int, Counter, Counter]:
    """Decode and INSERT OR IGNORE one chunk. Returns (mints, fills by kind, skipped)."""
    fills: Counter = Counter()
    skipped: Counter = Counter()

    def stamp(block: int, exact_ms: int | None) -> tuple[int, int]:
        if exact_ms is not None:
            return exact_ms, 1
        if block in (from_block, to_block):
            return (from_ms if block == from_block else to_ms), 1
        return interpolate_ms(block, from_block, from_ms, to_block, to_ms), 0

    mint_rows = []
    for entry in mint_logs:
        m = sp.decode_seadrop_mint(entry)
        if m is None:
            skipped["undecodable_mint"] += 1
            continue
        ts, exact = stamp(m.block, m.ts_ms)
        mint_rows.append((m.tx, m.log_index, m.block, ts, exact, m.collection, m.minter, m.payer,
                          m.fee_recipient, m.quantity, str(m.unit_price_wei), m.fee_bps, m.stage_index))
    fill_rows = []
    for entry in fill_logs:
        of = sp.decode_order_fulfilled(entry)
        if of is None:
            skipped["undecodable_fill"] += 1
            continue
        kind, sale = sp.classify(of)
        if sale is None:
            skipped[kind] += 1
            continue
        ts, exact = stamp(sale.block, sale.ts_ms)
        fills[kind] += 1
        fill_rows.append((sale.tx, sale.log_index, sale.block, ts, exact, sale.kind, sale.collection,
                          str(sale.token_id), sale.units, sale.payment_token, str(sale.gross),
                          str(sale.seller_net), str(sale.market_fee), str(sale.royalty), sale.seller,
                          sale.buyer, sale.zone))
    if mint_rows:
        conn.executemany(
            "INSERT OR IGNORE INTO nft_mints (tx, log_index, block, ts_ms, ts_exact, collection, minter, "
            "payer, fee_recipient, quantity, unit_price_wei, fee_bps, stage_index) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", mint_rows)
    if fill_rows:
        conn.executemany(
            "INSERT OR IGNORE INTO nft_fills (tx, log_index, block, ts_ms, ts_exact, kind, collection, "
            "token_id, units, payment_token, gross, seller_net, market_fee, royalty, seller, buyer, zone) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", fill_rows)
    return len(mint_rows), fills, skipped


def prune(conn: Any, *, before_ms: int, chunk: int = 5_000, max_chunks: int = 10) -> int:
    """Delete tape rows older than ``before_ms`` in bounded chunks through the ts indexes."""
    total = 0
    for table in ("nft_mints", "nft_fills"):
        for _ in range(max(1, max_chunks)):
            with tx(conn):
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE rowid IN "  # noqa: S608 - fixed table names
                    f"(SELECT rowid FROM {table} WHERE ts_ms < ? ORDER BY ts_ms LIMIT ?)",
                    (int(before_ms), int(chunk)),
                )
            total += cur.rowcount or 0
            if (cur.rowcount or 0) < chunk:
                break
    return total


def run_tape(
    conn: Any,
    *,
    rpc: sp.Rpc,
    config: TapeConfig | None = None,
    now: int | None = None,
    deadline_monotonic: float | None = None,
) -> TapeResult:
    """Advance the tape by at most ``max_chunks_per_run`` chunks. Never raises on RPC."""
    cfg = config or TapeConfig()
    ts_now = now if now is not None else now_ms()
    res = TapeResult(cursor=load_cursor(conn))

    head_got = rpc([("eth_blockNumber", [])], sp.ENDPOINT_TAPE)
    res.rpc_calls += 1
    head = sp.hex_int(head_got.results[0]) if head_got.results else None
    if head is None:
        res.ok, res.note = False, f"head unreadable: {head_got.note or 'no response'}"[:200]
        return res
    res.head = head
    safe_head = head - cfg.head_margin_blocks
    cursor = res.cursor
    if cursor is None:
        start = max(0, safe_head - blocks_for_hours(cfg.backfill_hours))
        cursor = TapeCursor(next_block=start, first_block=start)
    res.from_block = cursor.next_block
    span = cfg.chunk_blocks
    attempts = 0
    while attempts < cfg.max_chunks_per_run and cursor.next_block <= safe_head:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            res.note = "deadline"
            break
        attempts += 1
        start = cursor.next_block
        end = min(start + span - 1, safe_head)
        # A warm cursor's last anchor (its through block) is this chunk's start anchor, so
        # the batch carries three reads instead of four.
        warm = (cursor.through_block is not None and cursor.through_ts_ms is not None
                and cursor.through_block == start - 1)
        got = rpc(sp.tape_chunk_calls(start, end, with_from_anchor=not warm), sp.ENDPOINT_TAPE)
        res.rpc_calls += 1
        results = list(got.results or [])
        if len(results) < (3 if warm else 4):
            res.ok, res.note = False, f"chunk {start}-{end} unread: {got.note or 'no response'}"[:200]
            break
        to_raw, mint_logs, fill_logs = results[0], results[1], results[2]
        if (mint_logs is None or fill_logs is None) and _too_many(got.note):
            if span <= cfg.min_chunk_blocks:
                res.ok, res.note = False, f"chunk {start}-{end} too dense even at {span} blocks"
                break
            span = max(cfg.min_chunk_blocks, span // 2)
            res.halvings += 1
            continue
        anchor_to = _block_anchor(to_raw)
        if warm:
            anchor_block, anchor_from = int(cursor.through_block), (int(cursor.through_ts_ms), None)  # type: ignore[arg-type]
        else:
            anchor_block, anchor_from = start, _block_anchor(results[3])
        if anchor_from is None or anchor_to is None or not isinstance(mint_logs, list) \
                or not isinstance(fill_logs, list):
            res.ok, res.note = False, f"chunk {start}-{end} incomplete: {got.note or 'bad shape'}"[:200]
            break
        (from_ms, _), (to_ms, base_fee) = anchor_from, anchor_to
        with tx(conn):
            mints, fills, skipped = persist_chunk(
                conn, mint_logs, fill_logs, from_block=anchor_block, from_ms=from_ms, to_block=end, to_ms=to_ms,
            )
            if cursor.first_ts_ms is None:
                cursor.first_ts_ms = from_ms
                cursor.first_block = start
            cursor.next_block = end + 1
            cursor.through_block = end
            cursor.through_ts_ms = to_ms
            if base_fee is not None:
                cursor.base_fee_wei = base_fee
            cursor.updated_ms = ts_now
            _save_cursor(conn, cursor)
        res.chunks += 1
        res.mints += mints
        res.fills.update(fills)
        res.skipped.update(skipped)
        res.to_block = end
    res.cursor = cursor
    res.behind_blocks = max(0, safe_head - cursor.next_block + 1)
    if cfg.retention_days > 0:
        try:
            res.pruned = prune(conn, before_ms=ts_now - int(cfg.retention_days * 86_400_000),
                               chunk=cfg.prune_chunk, max_chunks=cfg.prune_max_chunks)
        except Exception as exc:  # noqa: BLE001 - retention failing is not a tape failure
            log.warning("nft tape prune failed: %s", exc)
    return res


__all__ = [
    "CURSOR_KEY",
    "TapeConfig",
    "TapeCursor",
    "TapeResult",
    "interpolate_ms",
    "load_cursor",
    "persist_chunk",
    "prune",
    "run_tape",
]
