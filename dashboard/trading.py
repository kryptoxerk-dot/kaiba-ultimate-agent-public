"""Trades, paper results, gathered wallets and box health, for the operator console pages.

Same two rules as :mod:`dashboard.data`: never fabricate a number (``None`` plus a reason),
and never let one broken source take a page down. Every reader here is read-only and short,
because the database behind it is the live 40+ GB one (AGENTS.md DATABASE READ RULE):

* positions reads are bounded by ``closed_ms`` (indexed) and capped; the table is small;
* the one wide query, wallet grade counts over ~1M ``wallet_scores`` rows, uses the covering
  grade index (~2 s measured 2026-10-18), runs under a 20 s progress-handler deadline and is
  cached for 10 minutes, so a refresh storm cannot repeat it;
* files (wallet-hunt exports, twin exits, backups) are read through mtime-keyed caches.

Paper results reuse :mod:`kaiba.learning.paper_report` -- ONE filter
(``paper_report.PaperFilter``: the honest cutoff, default-deny off sol for rows whose entry was not
priced from the pool or curve, the 10-18 native-price placeholder) plus the same outcome
exclusions and +300% cap, through ``paper_report.scoreboard`` / ``paper_filter`` -- so the page,
the daily Telegram report and the agent's ``kaiba_performance`` tool cannot disagree. 2026-10-23:
the trade summary said it applied "the same exclusions" and applied only the placeholder one.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from dashboard import broker, data
from kaiba.learning import time_exit_trial

log = logging.getLogger(__name__)

DAY_MS = 86_400_000
#: Same cap as the sprint scoreboard and the paper report: one +1000% print cannot carry a lane.
OUTCOME_CAP = 3.0
GRADE_CACHE_S = 600.0
SCORE_CACHE_S = 120.0
HEALTH_CACHE_S = 15.0
QUERY_DEADLINE_S = 20.0
EXPORT_PREVIEW_ROWS = 40
CHAINS = ("sol", "bsc", "robinhood")

_cache: dict[str, tuple[float, Any]] = {}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _cached(key: str, ttl_s: float, build):
    hit = _cache.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < ttl_s:
        return hit[1]
    value = build()
    _cache[key] = (now, value)
    return value


def clear_caches() -> None:
    _cache.clear()
    _export_cache.clear()


def _data_dir() -> Path:
    try:
        from kaiba.core.config import get_settings

        return Path(get_settings().kaiba_data_dir)
    except Exception:  # pragma: no cover - settings broken means nothing else works either
        return Path("data")


def _db_path() -> Path | None:
    try:
        from kaiba.core.config import get_settings

        return Path(get_settings().db_path)
    except Exception:  # pragma: no cover
        return None


def _deadline_rows(sql: str, params: tuple = (), seconds: float = QUERY_DEADLINE_S) -> list[dict[str, Any]] | None:
    """Rows, or ``None`` if the query errored or ran past ``seconds`` (then it was interrupted)."""
    conn = data._conn()
    if conn is None:
        return None
    deadline = time.monotonic() + seconds
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.Error as exc:
        log.info("dashboard trading query skipped (%s): %s", exc, sql[:80])
        return None
    finally:
        conn.set_progress_handler(None, 0)


def _ret(cost: Any, realized: Any) -> float | None:
    """Realized return as a fraction. Both columns are TEXT base units: parse, never compare."""
    try:
        c, r = float(cost), float(realized)
    except (TypeError, ValueError):
        return None
    return (r / c) if c > 0 else None


def _stats(returns: list[float]) -> dict[str, Any]:
    if not returns:
        return {"n": 0, "mean": None, "median": None, "win_rate": None}
    capped = [max(-1.0, min(OUTCOME_CAP, x)) for x in returns]
    return {
        "n": len(returns),
        "mean": sum(capped) / len(capped),
        "median": statistics.median(returns),
        "win_rate": sum(1 for x in returns if x > 0) / len(returns),
    }


# --------------------------------------------------------------------------------------
# trades
# --------------------------------------------------------------------------------------


def _paper_filter(days: int) -> Any:
    """``paper_report.PaperFilter`` for ``days``, or ``None`` when it cannot be built. ``None`` is
    DENY, not allow: :func:`_paper_exclusion` then leaves every paper row out and says why."""

    def build() -> Any:
        conn = data._conn()
        if conn is None:
            return None
        try:
            from kaiba.learning import paper_report

            return paper_report.paper_filter(conn, _now_ms(), days)
        except Exception as exc:
            log.info("dashboard: paper filter unavailable: %s", exc)
            return None

    return _cached(f"paper_filter:{days}", SCORE_CACHE_S, build)


#: The reason a paper row is left out when the shared filter could not be built.
FILTER_UNAVAILABLE = "paper_filter_unavailable"
#: Operator-readable names of the paper report exclusion reasons (``closed_trades`` flags).
EXCLUSION_LABELS = {
    "before_honest_cutoff": "opened before the honest cutoff",
    "not_pool_priced": "entry not priced from the pool or curve",
    "native_price_bug": "placeholder price",
    "fabricated_outcome": "booked write-off",
    "unpriceable": "no cost recorded",
    FILTER_UNAVAILABLE: "paper filter unavailable",
}


def _paper_exclusion(flt: Any, row: dict[str, Any], ret: float | None) -> str | None:
    """Why the scoreboard leaves this closed PAPER row out, or ``None``: the report's own rule
    (``PaperFilter.reason`` then ``paper_report.outcome_exclusion``), never a copy of it."""
    if flt is None:
        return FILTER_UNAVAILABLE
    from kaiba.learning import paper_report

    return (flt.reason(row["position_id"], row["chain"], row["opened_ms"])
            or paper_report.outcome_exclusion(ret, row.get("exit_reason"), "shadow"))


def trade_summary(days: int = 12) -> dict[str, Any]:
    """Closed positions per mode x lane x chain over ``days``: count, capped mean, median, wins.

    Live rows also carry realized native per chain. Paper rows are counted by the paper report's
    own rule (:func:`_paper_exclusion`): every row the scoreboard leaves out is left out here too,
    and counted per reason in ``excluded_by_reason``.
    """
    since = _now_ms() - days * DAY_MS
    rows = data._rows(
        "SELECT position_id, mode, lane, chain, opened_ms, cost_native, realized_native, exit_reason FROM positions "
        "WHERE closed_ms IS NOT NULL AND closed_ms >= ?",
        (since,),
    )
    flt = _paper_filter(days)
    trials = _trials()
    groups: dict[tuple[str, str, str], list[float]] = {}
    excluded: dict[tuple[str, str, str], int] = {}
    reasons: dict[str, int] = {}
    realized: dict[str, int] = {}
    for r in rows:
        lane = str(r["lane"])
        if r["mode"] == "shadow":
            lane = time_exit_trial.label_for(trials, lane, str(r["position_id"]), r["opened_ms"])
        key = (str(r["mode"]), lane, str(r["chain"]))
        ret = _ret(r["cost_native"], r["realized_native"])
        why = _paper_exclusion(flt, r, ret) if r["mode"] == "shadow" else ("unpriceable" if ret is None else None)
        if why is not None:
            excluded[key] = excluded.get(key, 0) + 1
            reasons[why] = reasons.get(why, 0) + 1
            continue
        groups.setdefault(key, []).append(ret)
        if r["mode"] == "live":
            value = data._int(r["realized_native"])
            if value is not None:
                realized[str(r["chain"])] = realized.get(str(r["chain"]), 0) + value
    out = []
    for (mode, lane, chain), rets in sorted(groups.items()):
        out.append({"mode": mode, "lane": lane, "chain": chain, **_stats(rets),
                    "excluded": excluded.get((mode, lane, chain), 0)})
    return {
        "days": days,
        "rows": out,
        "live_realized": [
            {"chain": c, "native": data.native_amount(v, c), "symbol": data.native_symbol(c)}
            for c, v in sorted(realized.items())
        ],
        "excluded_total": sum(excluded.values()),
        "excluded_by_reason": dict(sorted(reasons.items())),
    }


def closed_trades(mode: str = "shadow", days: int = 3, limit: int = 150,
                  lane: str | None = None, chain: str | None = None) -> list[dict[str, Any]]:
    """Newest closed positions, with return, hold time and any reason to distrust the number."""
    if mode not in ("live", "shadow"):
        return []
    sql = (
        "SELECT p.position_id, p.lane, p.chain, p.mode, p.token, p.opened_ms, p.closed_ms, "
        "p.cost_native, p.proceeds_native, p.realized_native, p.exit_reason, p.mae_pct, p.mfe_pct, "
        "t.symbol AS symbol FROM positions p "
        "LEFT JOIN tokens t ON t.chain = p.chain AND t.address = p.token "
        "WHERE p.closed_ms IS NOT NULL AND p.closed_ms >= ? AND p.mode = ?"
    )
    params: list[Any] = [_now_ms() - days * DAY_MS, mode]
    if lane:
        sql += " AND p.lane = ?"
        params.append(lane)
    if chain:
        sql += " AND p.chain = ?"
        params.append(chain)
    sql += " ORDER BY p.closed_ms DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    flt = _paper_filter(max(days, 12)) if mode == "shadow" else None
    trials = _trials() if mode == "shadow" else {}
    out = []
    for r in data._rows(sql, params):
        ret = _ret(r["cost_native"], r["realized_native"])
        flags = []
        trial = time_exit_trial.treated(trials, str(r["lane"]), str(r["position_id"]), int(r["opened_ms"] or 0))
        if trial is not None:
            flags.append(f"exit trial: timed arm ({trial.hold_s}s)")
        why = _paper_exclusion(flt, r, ret) if mode == "shadow" else None
        if why is not None:
            flags.append(f"{EXCLUSION_LABELS.get(why, why)} (excluded from scores)")
        if ret is not None and ret > OUTCOME_CAP:
            flags.append("above +300% cap" + (" - foreign-pool mark?" if r["chain"] != "sol" else ""))
        out.append({
            "position_id": r["position_id"],
            "lane": r["lane"],
            "chain": r["chain"],
            "mode": r["mode"],
            "token": r["token"],
            "token_short": data.short_addr(r["token"]),
            "symbol": r["symbol"],
            "opened_ms": r["opened_ms"],
            "closed_ms": r["closed_ms"],
            "hold_s": ((r["closed_ms"] or 0) - (r["opened_ms"] or 0)) / 1000 if r["opened_ms"] else None,
            "cost_native": data.native_amount(data._int(r["cost_native"]), r["chain"]),
            "realized_native": data.native_amount(data._int(r["realized_native"]), r["chain"]),
            "symbol_native": data.native_symbol(r["chain"]),
            "return": ret,
            "exit_reason": r["exit_reason"],
            "mfe_pct": r["mfe_pct"],
            "mae_pct": r["mae_pct"],
            "flags": flags,
        })
    return out


def _trials() -> dict[str, Any]:
    return _cached("trials", SCORE_CACHE_S, time_exit_trial.load_trials)


def trial_results() -> list[dict[str, Any]]:
    """The randomized time-exit trial, arm against arm (kaiba/learning/time_exit_trial.py)."""

    def build() -> list[dict[str, Any]]:
        conn = data._conn()
        if conn is None:
            return []
        try:
            from kaiba.learning import paper_report

            return paper_report.trial_board(conn, _now_ms(), _trials())
        except Exception as exc:
            log.info("dashboard: trial stats unavailable: %s", exc)
            return []

    return _cached("trial_results", SCORE_CACHE_S, build)


def paper_scoreboard() -> dict[str, Any]:
    """The daily report's scoreboard, computed live from the same functions."""

    def build() -> dict[str, Any]:
        conn = data._conn()
        if conn is None:
            return {"available": False, "reason": "no database connection", "rows": []}
        try:
            from kaiba.learning import paper_report as pr

            board = pr.scoreboard(conn, _now_ms(), trials=_trials())
        except Exception as exc:
            return {"available": False, "reason": f"scoreboard unavailable: {type(exc).__name__}", "rows": []}
        scores, rows, skipped = board.papers, board.rows, board.skipped
        out = []
        for ps in scores:
            s = ps.score
            out.append({
                "lane": s.lane, "chain": s.chain, "n": s.n, "n_old": s.n_old, "n_new": s.n_new,
                "mean": s.mean, "mean_old": s.mean_old, "mean_new": s.mean_new, "lower": s.lower,
                "verdict": s.verdict, "why": s.why, "excluded": ps.excluded,
                "placeholder_excluded": skipped.get((s.lane, s.chain), 0),
                "exit_stage": board.exit_stage.get((s.lane, s.chain), (0, None))[0],
            })
        before = skipped.get(("__before_honest__", ""), 0)
        out.sort(key=lambda r: (r["verdict"] != "PASS", -(r["mean"] if r["mean"] is not None else -9)))
        # The same rows the verdicts use, as one running line: the sum of per-trade returns at a
        # flat stake (entries are flat by the owner directive), so +100 means one stake earned.
        rets = sorted((closed, ret) for _lane, _chain, closed, ret, _reason, _mode in rows if ret is not None)
        acc, points = 0.0, []
        for closed, ret in rets:
            acc += ret
            points.append([closed, round(acc * 100, 3)])
        if len(points) > 600:
            step = len(points) / 599
            points = [points[int(i * step)] for i in range(599)] + [points[-1]]
        total = {"n": len(rets), "wins": sum(1 for _c, r in rets if r > 0),
                 "mean": (sum(r for _c, r in rets) / len(rets)) if rets else None, "sum_pct": acc * 100}
        return {"available": True, "window_days": pr.WINDOW_DAYS, "rows": out,
                "honest_since_ms": pr.PAPER_HONEST_MS, "before_honest": before, "total": total,
                "not_pool_priced": dict(board.filter.not_pool_priced_counts),
                "curve": {"unit": "% of one stake", "source": "paper closes since the honest cutoff, flat stake",
                          "points": points}}

    return _cached("scoreboard", SCORE_CACHE_S, build)


def twin_exits() -> dict[str, Any]:
    """The paired early-exit study (``kaiba-twin-exits``), best rows first."""
    try:
        from kaiba.learning import paper_report as pr

        report = pr.twin_report()
    except Exception as exc:
        return {"available": False, "reason": f"unreadable: {type(exc).__name__}", "rows": []}
    if not report:
        return {"available": False, "reason": "no twin-exit report yet", "rows": []}
    rows = [r for r in report.get("rows") or [] if r.get("pairs")]
    rows.sort(key=lambda r: (r.get("verdict") != "PASS", -(r.get("paired_diff_mean") or -9)))
    return {
        "available": True,
        "at_ms": report.get("at_ms"),
        "positions": report.get("positions"),
        "without_tape": report.get("positions_without_tape"),
        "rows": rows[:12],
    }


def latest_paper_report() -> dict[str, Any]:
    row = data._one("SELECT value, updated_ms FROM kv WHERE key = 'paper_report:last'")
    if not row or not row.get("value"):
        return {"available": False, "reason": "the daily paper report has not run yet"}
    return {"available": True, "text": row["value"], "updated_ms": row.get("updated_ms")}


# --------------------------------------------------------------------------------------
# wallets gathered
# --------------------------------------------------------------------------------------

_export_cache: dict[str, tuple[float, int, dict[str, Any]]] = {}

_CHAIN_WORDS = (("solana", "sol"), ("robinhood", "robinhood"), ("bnb", "bsc"), ("bsc", "bsc"))
EXPORT_NAME = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


def _runner_dir() -> Path:
    return _data_dir() / "claude-runners"


def _excluded_addresses() -> set[str]:
    """The hunt's own exclusion list, applied again here: owned and blacklisted addresses never
    reach the page, even if an export were written by an older version of the hunt."""
    path = _runner_dir() / "excluded.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if isinstance(raw, dict):
        raw = list(raw.keys())
    return {str(a).lower() for a in raw if isinstance(a, str)}


def _chain_of(name: str) -> str | None:
    low = name.lower()
    for word, chain in _CHAIN_WORDS:
        if word in low:
            return chain
    return None


def _read_export(path: Path, excluded: set[str]) -> dict[str, Any]:
    st = path.stat()
    hit = _export_cache.get(path.name)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    entries: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and isinstance(item.get("address"), str):
                address = item["address"]
                if address.lower() in excluded:
                    continue
                entries.append({
                    "address": address,
                    "address_short": data.short_addr(address, 6, 6),
                    "label": str(item.get("name") or ""),
                    "emoji": str(item.get("emoji") or ""),
                    "groups": [str(g) for g in item.get("groups") or [] if g != "default"],
                })
            elif isinstance(item, str):
                if item.lower() not in excluded:
                    entries.append({"address": item, "address_short": data.short_addr(item, 6, 6),
                                    "label": "", "emoji": "", "groups": []})
    info = {
        "name": path.name,
        "chain": _chain_of(path.name),
        "count": len(entries),
        "readable": isinstance(raw, list),
        "mtime_ms": int(st.st_mtime * 1000),
        "bytes": st.st_size,
        "preview": entries[:EXPORT_PREVIEW_ROWS],
    }
    _export_cache[path.name] = (st.st_mtime, st.st_size, info)
    return info


def wallet_exports() -> dict[str, Any]:
    """Every wallet-hunt export (the files imported into GMGN), with a preview of each."""
    folder = _runner_dir() / "exports"
    if not folder.is_dir():
        return {"available": False, "reason": "no wallet-hunt exports on this machine", "files": []}
    excluded = _excluded_addresses()
    files = []
    for path in sorted(folder.iterdir()):
        if path.is_file() and EXPORT_NAME.match(path.name) and path.suffix in (".txt", ".json"):
            try:
                files.append(_read_export(path, excluded))
            except OSError:
                continue
    files.sort(key=lambda f: (f["chain"] or "~", f["name"]))
    return {"available": True, "files": files, "excluded_list_size": len(excluded)}


def export_path(name: str) -> Path | None:
    """The file behind a download link -- only a name the listing itself produced."""
    if not EXPORT_NAME.match(name or ""):
        return None
    for f in wallet_exports().get("files", []):
        if f["name"] == name:
            return _runner_dir() / "exports" / name
    return None


def hunt_status() -> list[dict[str, Any]]:
    """Each chain's last wallet-hunt run (``claude-runner-hunt``), as the hunt recorded it."""
    out = []
    for chain in CHAINS:
        path = _runner_dir() / chain / "last.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict):
            at = raw.get("at")
            raw["at_ms"] = int(at * 1000) if isinstance(at, (int, float)) else None
            raw["chain"] = chain
            out.append(raw)
    return out


def wallet_grades() -> dict[str, Any]:
    """Grade counts over every scored wallet, and the B-and-better split per chain."""

    def build() -> dict[str, Any]:
        by_grade = _deadline_rows("SELECT grade, COUNT(*) AS n FROM wallet_scores GROUP BY grade")
        if by_grade is None:
            return {"available": False, "reason": "grade count timed out or failed; it is retried in 10 min"}
        top = _deadline_rows(
            "SELECT chain, grade, COUNT(*) AS n FROM wallet_scores WHERE grade IN ('A', 'B') GROUP BY chain, grade"
        ) or []
        return {"available": True, "by_grade": by_grade, "top_by_chain": top, "at_ms": _now_ms()}

    return _cached("grades", GRADE_CACHE_S, build)


# --------------------------------------------------------------------------------------
# box health
# --------------------------------------------------------------------------------------

#: Unit name patterns the health view lists. Read-only: it shows state, it never acts.
UNIT_PATTERNS = ("kaiba-*", "hermes-gateway-*", "claude-runner-*")


def _systemctl(*args: str, timeout: float = 8.0) -> str | None:
    if not sys.platform.startswith("linux") or shutil.which("systemctl") is None:
        return None
    if broker.active():
        code, out, _err = broker.capture(["systemctl", "--user", *args], timeout)
        return None if code is None else out
    try:
        done = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout


def units() -> list[dict[str, Any]]:
    raw = _systemctl("list-units", "--all", "--plain", "--no-legend", "--no-pager", *UNIT_PATTERNS)
    if raw is None:
        return []
    out = []
    for line in raw.splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 4 and parts[0].endswith((".service", ".timer")):
            out.append({"unit": parts[0], "load": parts[1], "active": parts[2], "sub": parts[3],
                        "description": parts[4] if len(parts) > 4 else ""})
    return out


def in_flight_orders() -> int | None:
    row = data._one(
        "SELECT COUNT(*) AS n FROM orders WHERE state IN "
        "('planned','reserved','submitting','submitted','partial','unknown')"
    )
    return None if row is None else int(row["n"])


def system_health() -> dict[str, Any]:
    def build() -> dict[str, Any]:
        out: dict[str, Any] = {"at_ms": _now_ms()}
        listed = units()
        out["units"] = listed
        out["units_available"] = bool(listed)
        out["failed_units"] = [u["unit"] for u in listed if u["active"] == "failed"]
        try:
            usage = shutil.disk_usage(_data_dir())
            out["disk"] = {"used_pct": round(100 * usage.used / usage.total, 1),
                           "free_gb": round(usage.free / 2**30, 1)}
        except OSError:
            out["disk"] = None
        db_path = _db_path()
        try:
            out["wal_gb"] = round(os.path.getsize(str(db_path) + "-wal") / 2**30, 2) if db_path else None
        except OSError:
            out["wal_gb"] = None
        backups = _data_dir() / "backups"
        try:
            out["backup_last"] = json.loads((backups / "last.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out["backup_last"] = None
        try:
            newest = max(backups.glob("core-*.db.gz"), key=lambda p: p.stat().st_mtime)
            out["backup_newest"] = {"name": newest.name, "mtime_ms": int(newest.stat().st_mtime * 1000),
                                    "mb": round(newest.stat().st_size / 2**20)}
        except (OSError, ValueError):
            out["backup_newest"] = None
        out["orders_in_flight"] = in_flight_orders()
        return out

    return _cached("health", HEALTH_CACHE_S, build)
