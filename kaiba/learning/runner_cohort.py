"""Freeze the runner-hunt wallet lists into a cohort the confluence-5 paper lane can follow.

Owner, 2026-10-20: "keep on paper trading and gather really good wallets and see what the
agent can do after". The runner hunt (``deploy/research/runner_hunt/``, user timers
``claude-runner-*``) collects, every hour, the snipers and early buyers who made money on
tokens that really ran (GMGN trending, all-time-high market cap >= $300k), and writes GMGN
import lists to ``data/claude-runners/exports/``.

Whether those wallets are "really good" for US is a different question from whether they
were good for themselves: copying measured negative at our lag and fees every way it was
tried (memories no-proven-copy-wallets, copy-exit-early-backtest). So this module does not
claim anything. It only makes the question answerable FORWARD:

* :func:`freeze` writes the current lists as one cohort per chain,
  ``runners:<chain>:<frozen_ms>``, into the same ``wallet_cohort_freezes`` /
  ``wallet_cohorts`` tables the proven cohort uses, with each wallet's list label kept in
  ``covariates_json`` for later splits;
* :func:`runner_members` returns the newest freeze at or before a given instant, so a replay
  of an earlier moment can never count a wallet that was listed later.

The lists were chosen BECAUSE these wallets bought the runners of the last weeks, so any test
on those same weeks is circular. Only buys AFTER a wallet's freeze count, and the scoreboard
for the confluence-5 lane in the owner's daily report is that forward record.

MEASURED 2026-10-20, coverage on our own tape over 3 days: sol snipers/early list 506 of
1,176 wallets seen (26,573 trades), sol high tier 10 of 174, robinhood 94 of 843, bsc 28 of
360. The lane can only act on buys we see, so low coverage caps how often it can fire; it
does not bias what it measures.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import sys
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, jdump
from kaiba.core.schemas import Chain
from kaiba.learning.proven import TREATED_ARM, ProvenCohort

log = logging.getLogger(__name__)

COHORT_PREFIX = "runners"
#: Export file -> chain. The runner hunt names BSC "bnb" and Solana "solana".
EXPORT_CHAINS: dict[str, Chain] = {"solana": Chain.SOL, "bnb": Chain.BSC, "robinhood": Chain.ROBINHOOD}
EVM = {Chain.BSC, Chain.ROBINHOOD, Chain.ETH, Chain.BASE}


def _chain_of(filename: str) -> Chain | None:
    stem = filename.lower()
    if not stem.startswith("kaiba-"):
        return None
    head = stem.split("-")[1] if "-" in stem else ""
    return EXPORT_CHAINS.get(head)


def _norm(chain: Chain, address: str) -> str:
    a = str(address).strip()
    return a.lower() if chain in EVM else a


def read_exports(export_dir: Path) -> dict[Chain, dict[str, dict[str, Any]]]:
    """``{chain: {address: {"labels": [...], "lists": [...]}}}`` from every export list."""
    out: dict[Chain, dict[str, dict[str, Any]]] = {}
    for path in sorted(Path(export_dir).glob("kaiba-*.txt")):
        chain = _chain_of(path.name)
        if chain is None:
            continue
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("runner_cohort: cannot read %s (%s)", path.name, exc)
            continue
        for it in items if isinstance(items, list) else []:
            addr = it.get("address") if isinstance(it, dict) else None
            if not addr:
                continue
            rec = out.setdefault(chain, {}).setdefault(_norm(chain, addr), {"labels": [], "lists": []})
            if it.get("name"):
                rec["labels"].append(str(it["name"])[:80])
            rec["lists"].append(path.name)
    return out


def freeze(conn: sqlite3.Connection, chain: Chain, members: Mapping[str, Mapping[str, Any]], *,
           frozen_ms: int, source: str) -> str:
    """Write one freeze for ``chain`` in a single transaction. Returns its cohort id."""
    cohort_id = f"{COHORT_PREFIX}:{chain.value}:{int(frozen_ms)}"
    digest = hashlib.sha256("\n".join(sorted(members)).encode()).hexdigest()[:16]
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO wallet_cohort_freezes (cohort_id, frozen_ms, chain, graded_n, control_n, notes_json) "
            "VALUES (?,?,?,?,0,?)",
            (cohort_id, int(frozen_ms), chain.value, len(members),
             jdump([{"source": source, "members_sha256_16": digest}])),
        )
        conn.executemany(
            "INSERT INTO wallet_cohorts (cohort_id, arm, chain, address, frozen_ms, grade, covariates_json) "
            "VALUES (?,?,?,?,?,NULL,?)",
            [(cohort_id, TREATED_ARM, chain.value, addr, int(frozen_ms),
              jdump({"labels": rec.get("labels", [])[:3], "lists": sorted(set(rec.get("lists", [])))}))
             for addr, rec in sorted(members.items())],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return cohort_id


def latest_runner_cohort(conn: sqlite3.Connection, chain: Chain, *, at_ms: int | None = None) -> ProvenCohort | None:
    """The newest runner freeze for ``chain`` at or before ``at_ms`` (None if never frozen)."""
    sql = "SELECT cohort_id, frozen_ms FROM wallet_cohort_freezes WHERE chain = ? AND cohort_id LIKE ?"
    args: list[Any] = [chain.value, f"{COHORT_PREFIX}:{chain.value}:%"]
    if at_ms is not None:
        sql += " AND frozen_ms <= ?"
        args.append(int(at_ms))
    row = fetch_one(conn, sql + " ORDER BY frozen_ms DESC LIMIT 1", tuple(args))
    if not row:
        return None
    members = fetch_all(conn, "SELECT address FROM wallet_cohorts WHERE cohort_id = ? AND arm = ? AND chain = ?",
                        (row["cohort_id"], TREATED_ARM, chain.value))
    return ProvenCohort(cohort_id=str(row["cohort_id"]), chain=chain, frozen_ms=int(row["frozen_ms"]),
                        members=frozenset(str(m["address"]) for m in members))


_CACHE: dict[tuple[int, str], tuple[float, ProvenCohort | None]] = {}


def runner_members(conn: sqlite3.Connection, chain: Chain, *, max_age_s: float, at_ms: int | None = None,
                   ttl_s: float = 300.0) -> ProvenCohort | None:
    """What :func:`kaiba.learning.proven.proven_members` is for proven wallets: the cohort the
    lane may use now, ``None`` when never frozen or when the newest freeze is older than
    ``max_age_s`` (a stopped refresh must not leave the lane following a stale list)."""
    at = int(at_ms if at_ms is not None else time.time() * 1000)
    key = (id(conn), chain.value)
    hit = _CACHE.get(key)
    if hit is not None and hit[0] > time.monotonic():
        cohort = hit[1]
    else:
        try:
            cohort = latest_runner_cohort(conn, chain)
        except sqlite3.Error as exc:
            log.warning("runner cohort unreadable for %s (%s)", chain.value, exc)
            cohort = None
        _CACHE[key] = (time.monotonic() + ttl_s, cohort)
    if cohort is not None and cohort.frozen_ms > at:
        try:
            cohort = latest_runner_cohort(conn, chain, at_ms=at)
        except sqlite3.Error:
            cohort = None
    if cohort is None or cohort.age_s(at) > float(max_age_s):
        return None
    return cohort


def clear_cache() -> None:
    _CACHE.clear()


def refresh(conn: sqlite3.Connection, export_dir: Path, *, now_ms: int,
            chains: Iterable[Chain] = (Chain.SOL, Chain.BSC, Chain.ROBINHOOD)) -> dict[str, Any]:
    """Freeze every chain's current lists. A chain with no list is reported, not frozen empty:
    an empty freeze would read as "nobody is good", which is not what a missing file says."""
    lists = read_exports(export_dir)
    out: dict[str, Any] = {}
    for chain in chains:
        members = lists.get(chain) or {}
        if not members:
            out[chain.value] = {"skipped": "no export list"}
            continue
        prev = latest_runner_cohort(conn, chain)
        cohort_id = freeze(conn, chain, members, frozen_ms=now_ms, source=str(export_dir))
        added = len(set(members) - set(prev.members)) if prev else len(members)
        out[chain.value] = {"cohort_id": cohort_id, "members": len(members), "new_since_last": added}
    clear_cache()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kaiba.learning.runner_cohort", description=__doc__.split("\n")[0])
    ap.add_argument("--export-dir", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true", help="print list sizes; write nothing")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from kaiba.core import db as core_db
    from kaiba.core.config import get_settings

    export_dir = args.export_dir or Path(get_settings().kaiba_data_dir) / "claude-runners" / "exports"
    if args.dry_run:
        lists = read_exports(export_dir)
        sys.stdout.write(json.dumps({c.value: len(m) for c, m in lists.items()}) + "\n")
        return 0
    conn = core_db.connect()
    try:
        out = refresh(conn, export_dir, now_ms=int(time.time() * 1000))
    finally:
        conn.close()
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


__all__ = ["COHORT_PREFIX", "read_exports", "freeze", "latest_runner_cohort", "runner_members",
           "refresh", "clear_cache"]


if __name__ == "__main__":
    raise SystemExit(main())
