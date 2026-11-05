"""The trial registry: how many parameter configurations a lane has actually run under.

The deflated Sharpe ratio deflates a result by the expected maximum of *N* attempts at
nothing. That makes *N* load-bearing, and it is the number people fudge without meaning
to, because the honest count includes every threshold they nudged and re-ran, not only
the configurations they formally wrote down.

Before this module, `gates._trial_count` read the `experiments` table. That counts
declared experiments. A lane whose thresholds were adjusted twenty times in
`config/risk.yaml` and formally proposed twice deflated as though it had been tried
twice, which inflates every downstream number in the flattering direction.

So this records the configuration at the moment a decision is made, keyed on a digest of
the parameters themselves. Nobody has to remember anything, and nobody can quietly reset
the count: the table is append-only, and a configuration that was retired still counts,
because it was still an attempt.

**This makes promotion harder, not easier, and that is the point.** If the honest trial
count means a lane can no longer clear its gate, the lane was never clearing it — we were
just measuring with the wrong denominator.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump
from kaiba.core.schemas import Lane, digest, now_ms

log = logging.getLogger(__name__)

#: In-process memo so the hot path does not hit SQLite on every decision. Only the
#: "already registered this configuration" case is memoised; the counter still updates.
_SEEN: set[str] = set()


def trial_id(lane: Lane | str, params: dict[str, Any] | None) -> str:
    """Stable identity for one (lane, configuration) pair."""
    name = lane.value if isinstance(lane, Lane) else str(lane)
    return "trial_" + digest({"lane": name, "params": params or {}})[:24]


def register(
    lane: Lane | str,
    params: dict[str, Any] | None,
    conn: sqlite3.Connection | None = None,
    *,
    source: str = "decision",
    count_decision: bool = True,
) -> str:
    """Record that this configuration was run. Returns its trial id. Never raises.

    Called on the decision path, so it must be cheap and it must never be able to stop a
    decision being made: a registry write failing is a measurement problem, and losing
    the decision would be a trading problem.
    """
    tid = trial_id(lane, params)
    name = lane.value if isinstance(lane, Lane) else str(lane)
    ts = now_ms()
    try:
        c = conn or get_conn()
        c.execute(
            "INSERT INTO lane_trials (trial_id, lane, params_json, first_seen_ms, "
            " last_seen_ms, decisions, source) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(trial_id) DO UPDATE SET last_seen_ms=excluded.last_seen_ms, "
            "  decisions=lane_trials.decisions + excluded.decisions",
            (tid, name, jdump(params or {}), ts, ts, 1 if count_decision else 0, source),
        )
        _SEEN.add(tid)
    except Exception as exc:  # noqa: BLE001 - a measurement must never break a decision
        # Deliberately broad. The narrow sqlite3.Error missed a closed or substituted
        # connection, and the one thing this function must guarantee is that it cannot
        # be the reason a trade did not happen.
        log.debug("trial registry write failed for %s: %s", name, exc)
    return tid


def trial_count(lane: Lane | str, conn: sqlite3.Connection | None = None) -> int:
    """Distinct configurations this lane has ever run under."""
    name = lane.value if isinstance(lane, Lane) else str(lane)
    try:
        row = fetch_one(
            conn or get_conn(),
            "SELECT COUNT(*) AS n FROM lane_trials WHERE lane = ?",
            (name,),
        )
    except Exception:  # noqa: BLE001 - an unreadable registry counts as no trials
        return 0
    return int(row["n"]) if row else 0


def trials(lane: Lane | str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Every configuration for a lane, oldest first. The audit trail for a deflation."""
    name = lane.value if isinstance(lane, Lane) else str(lane)
    try:
        return fetch_all(
            conn or get_conn(),
            "SELECT trial_id, params_json, first_seen_ms, last_seen_ms, decisions, source "
            "FROM lane_trials WHERE lane = ? ORDER BY first_seen_ms ASC",
            (name,),
        )
    except Exception:  # noqa: BLE001
        return []


def reset_memo() -> None:
    """Forget the in-process memo. For tests; the table is never reset."""
    _SEEN.clear()
