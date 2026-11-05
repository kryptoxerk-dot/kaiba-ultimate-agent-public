"""Shadow quote tape: keep every price protection already observes for open positions.

Why (2026-10-16). Codex's autolearn study compares exit hypotheses (hold 60 s, hold 5 min,
sell everything at +25%) against the incumbent exits, but in its first 14.5 h it counted 0
closes: its own entries expire, its quote budget (100 GMGN calls/day) is spent, and its cost
gate needs a venue fee nothing produces (docs/research/claude-autolearn-review-20261016.md).
Meanwhile protection prices every open paper position about every 12 s and stores only the
LATEST quote (``kv watchdog.quote:<position>:<chain>:<token>``, overwritten each tick). That
path is exactly what a paired exit comparison needs, and it is thrown away.

This module reads those records every few seconds and appends each NEW observation to a
daily JSONL file. It makes no provider call and writes nothing to the database: one short
read-only range query per poll (``kv`` primary key), under the DATABASE READ RULE (fresh
connection, WAL check, progress deadline, closed at once). :mod:`kaiba.learning.twin_exits`
replays challenger exits on this tape.

Run: ``python -m kaiba.learning.shadow_tape run`` (systemd unit ``kaiba-shadow-tape``).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

KEY_LO, KEY_HI = "watchdog.quote:", "watchdog.quote;"  # ';' sorts right after ':'
POLL_S = 4.0
KEEP_DAYS = 16
WAL_LIMIT_BYTES = 1_500_000_000
QUERY_DEADLINE_S = 5.0
VERSION = "shadow_tape/1"


def _data_dir() -> Path:
    return Path(os.environ.get("KAIBA_DATA_DIR") or Path.home() / "kaiba" / "data")


def default_db() -> Path:
    return Path(os.environ.get("KAIBA_DB_PATH") or _data_dir() / "kaiba.db")


def default_out() -> Path:
    return _data_dir() / "learning" / "shadow_tape"


def read_updates(db: Path, since_ms: int) -> list[tuple[str, str, int]] | None:
    """``(key, value, updated_ms)`` of quote records updated after ``since_ms``, or ``None``
    when the WAL is over the read rule's limit (skip this poll, never wait inside it)."""
    wal = Path(str(db) + "-wal")
    if wal.exists() and wal.stat().st_size > WAL_LIMIT_BYTES:
        return None
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    deadline = time.monotonic() + QUERY_DEADLINE_S
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        return conn.execute(
            "SELECT key, value, updated_ms FROM kv WHERE key >= ? AND key < ? AND updated_ms > ?",
            (KEY_LO, KEY_HI, int(since_ms)),
        ).fetchall()
    finally:
        conn.close()


def parse(key: str, value: str, updated_ms: int) -> dict[str, Any] | None:
    """One tape row from a quote-memory record, or ``None`` when it holds no usable price."""
    parts = key.split(":")
    if len(parts) < 4:
        return None
    try:
        record = json.loads(value)
    except (TypeError, ValueError):
        return None
    price = record.get("price") or {}
    liq = record.get("liquidity") or {}
    observed = price.get("observed_ms")
    px = price.get("price_usd")
    if not observed or px in (None, "", "0"):
        return None
    return {
        "pid": parts[1], "chain": parts[2], "token": ":".join(parts[3:]),
        "obs": int(observed), "upd": int(updated_ms),
        "px": str(px), "xq": price.get("executable_quote_usd"),
        "liq": liq.get("liquidity_usd") if liq else price.get("liquidity_usd"),
        "lk": liq.get("liquidity_kind") if liq else price.get("liquidity_kind"),
        "src": price.get("source"), "venue": price.get("venue"), "pool": price.get("pool_id"),
        "basis": price.get("basis"), "settle": price.get("settlement"),
    }


class Tape:
    """Appends new observations to ``<out>/<YYYY-MM-DD>.jsonl`` (UTC day of the observation)."""

    def __init__(self, out: Path) -> None:
        self.out = out
        self.out.mkdir(parents=True, exist_ok=True)
        self.last_obs: dict[str, int] = {}

    def add(self, rows: Iterable[dict[str, Any]]) -> int:
        by_day: dict[str, list[str]] = {}
        n = 0
        for row in rows:
            if self.last_obs.get(row["pid"]) == row["obs"]:
                continue  # same observation re-written by a later tick: not a new print
            self.last_obs[row["pid"]] = row["obs"]
            day = time.strftime("%Y-%m-%d", time.gmtime(row["obs"] / 1000))
            by_day.setdefault(day, []).append(json.dumps(row, separators=(",", ":")))
            n += 1
        for day, lines in by_day.items():
            with open(self.out / f"{day}.jsonl", "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        if len(self.last_obs) > 50_000:  # positions close; forget the oldest half
            for pid in list(self.last_obs)[:25_000]:
                del self.last_obs[pid]
        return n

    def prune(self, now_s: float | None = None, keep_days: int = KEEP_DAYS) -> int:
        cutoff = time.strftime("%Y-%m-%d", time.gmtime((now_s or time.time()) - keep_days * 86_400))
        removed = 0
        for f in self.out.glob("*.jsonl"):
            if f.stem < cutoff:
                f.unlink()
                removed += 1
        return removed


def load(out: Path, start_ms: int, end_ms: int) -> dict[str, list[dict[str, Any]]]:
    """Every tape row with ``start_ms <= obs <= end_ms``, grouped by position, time-ordered."""
    by_pid: dict[str, list[dict[str, Any]]] = {}
    d = start_ms - 86_400_000
    days = set()
    while d <= end_ms + 86_400_000:
        days.add(time.strftime("%Y-%m-%d", time.gmtime(d / 1000)))
        d += 86_400_000
    for day in sorted(days):
        f = out / f"{day}.jsonl"
        if not f.exists():
            continue
        for line in open(f, encoding="utf-8"):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if start_ms <= row["obs"] <= end_ms:
                by_pid.setdefault(row["pid"], []).append(row)
    for rows in by_pid.values():
        rows.sort(key=lambda r: r["obs"])
    return by_pid


def run(db: Path, out: Path, *, poll_s: float = POLL_S) -> None:
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("flag", True))
    tape = Tape(out)
    since = int(time.time() * 1000) - 60_000
    last_prune, written, skipped = 0.0, 0, 0
    log.info("shadow tape %s -> %s", VERSION, out)
    while not stop["flag"]:
        t0 = time.monotonic()
        try:
            got = read_updates(db, since)
            if got is None:
                skipped += 1
            else:
                rows = [r for r in (parse(k, v, u) for k, v, u in got) if r]
                written += tape.add(rows)
                if got:
                    since = max(since, max(int(u) for _k, _v, u in got) - 1)
        except sqlite3.Error as exc:
            log.warning("shadow tape read failed: %s", exc)
        if time.time() - last_prune > 3600:
            tape.prune()
            last_prune = time.time()
            log.info("shadow tape: %d observations written, %d polls skipped (WAL)", written, skipped)
        time.sleep(max(0.0, poll_s - (time.monotonic() - t0)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="record protection's quotes for open positions")
    ap.add_argument("command", choices=["run"])
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run(args.db or default_db(), args.out or default_out())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
