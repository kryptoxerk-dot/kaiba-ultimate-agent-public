"""Small read-only projections of the three existing paper study reports."""
from __future__ import annotations

import hashlib
import json
import stat
from collections import Counter
from datetime import datetime
from pathlib import Path

MAX_REPORT_BYTES = 1024 * 1024
H5_AGE_MS = 5 * 60_000
DAILY_AGE_MS = 27 * 3_600_000
VERDICTS = {"PENDING", "INCONCLUSIVE", "PASS", "FAIL", "CONTINUE", "CONTINUE_TO_OWNER",
            "NOT_FALSIFIED", "RETIRE"}


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("invalid_count")
    return value


def _verdict(value: object) -> str:
    if not isinstance(value, str) or value not in VERDICTS:
        raise ValueError("unknown_verdict")
    return value


def _iso_ms(value: str) -> int:
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError("clock_without_timezone")
    return int(stamp.timestamp() * 1000)


def _h5(report: dict) -> dict:
    first = report["first_heartbeat_ms"]
    if first is not None and not 0 < _count(first) <= report["as_of_ms"]:
        raise ValueError("invalid_heartbeat")
    arms = {}
    for arm in ("H5", "B1", "B2"):
        source = report["arms"][arm]
        triggers, entered, terminal, unresolved = (
            _count(source[k]) for k in ("triggers", "entered", "settled", "unresolved")
        )
        if not unresolved <= terminal <= entered <= triggers:
            raise ValueError("invalid_arm_counts")
        unknown = source["recovery_unknown"]
        arms[arm] = {"triggers": triggers, "entered": entered, "priced_exits": terminal - unresolved,
                     "unresolved_terminal_marks": unresolved, "entered_without_terminal": entered - terminal,
                     "recovery_unknown_entered": _count(unknown["entered"]),
                     "recovery_unknown_pending": _count(unknown["pending"]),
                     "verdict": _verdict(source["verdict"]["verdict"])}
    gaps = report["observation_gaps"]
    return {"first_heartbeat_ms": first, "arms": arms, "gaps_over_60s": _count(gaps["over_60s"]),
            "read_deferrals": _count(report["unknown_intervals"]["read_deferrals"])}


def _d1(report: dict) -> dict:
    verdict = report["verdict"]
    closed = _count(report["pooled"]["n"])
    if closed != _count(verdict["n"]):
        raise ValueError("inconsistent_pooled_count")
    symbols = Counter()
    closed_bars = []
    for row in report["symbols"].values():
        status = row["status"]
        if status not in ("ok", "unavailable"):
            raise ValueError("unknown_symbol_status")
        symbols[status] += 1
        if status == "ok" and row["last_closed_bar"] is not None:
            closed_bar = _iso_ms(row["last_closed_bar"])
            if not 0 < closed_bar <= report["as_of_ms"]:
                raise ValueError("invalid_closed_bar_clock")
            closed_bars.append(closed_bar)
    return {"registered_ms": _iso_ms(report["registered_at"]), "paper_closes": closed,
            "min_closes": _count(verdict["min_n"]), "verdict": _verdict(verdict["verdict"]),
            "symbols_ok": symbols["ok"], "symbols_unavailable": symbols["unavailable"],
            "symbols_with_closed_bar_clock": len(closed_bars),
            "oldest_closed_bar_ms": min(closed_bars, default=None),
            "newest_closed_bar_ms": max(closed_bars, default=None)}


def _xs(report: dict) -> dict:
    books = {name: _count(report["books"][name]["weeks"]) for name in ("carry", "momentum")}
    paired = _count(report["mix_50_50"]["weeks"])
    missed = report["missed_formations"]
    if paired > min(books.values()) or not isinstance(missed, list):
        raise ValueError("invalid_week_counts")
    return {"first_formation_ms": _count(report["first_monday_ms"]), "settled_weeks_by_book": books,
            "paired_weeks": paired, "missed_formations": len(missed), "verdict": _verdict(report["verdict"])}


def _source(path: Path, version: str, age_limit: int, project, now_ms: int) -> dict:
    out = {"status": "unavailable", "max_age_ms": age_limit}
    try:
        if path.resolve() != path.absolute() or not stat.S_ISREG(path.stat().st_mode):
            return {**out, "reason": "not_regular_local_artifact"}
        with path.open("rb") as stream:
            raw = stream.read(MAX_REPORT_BYTES + 1)
        if len(raw) > MAX_REPORT_BYTES:
            return {**out, "reason": "artifact_too_large"}
        report = json.loads(raw)
        if not isinstance(report, dict) or report.get("version") != version or report.get("mode") != "paper":
            return {**out, "reason": "unsupported_report"}
        clock = _count(report["as_of_ms"])
        if clock == 0:
            return {**out, "reason": "missing_source_clock"}
        age = now_ms - clock
        out.update({"source_version": version, "as_of_ms": clock, "age_ms": age,
                    "source_sha256": hashlib.sha256(raw).hexdigest()})
        if age < 0 or age > age_limit:
            return {**out, "status": "future" if age < 0 else "stale"}
        return {**out, "status": "fresh_at_snapshot", **project(report)}
    except FileNotFoundError:
        return {**out, "reason": "artifact_missing"}
    except OSError:
        return {**out, "reason": "artifact_unreadable"}
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError, RuntimeError):
        return {**out, "reason": "invalid_report_fields"}


def snapshot(data_dir: Path, *, now_ms: int) -> dict:
    """Read only the fixed artifacts. The report carries counts, never orders or returns."""
    return {"snapshot_ms": now_ms, "mode": "paper", "promotion_authority": False,
            "h5": _source(data_dir / "learning/h5/report.json", "h5-reentry/1", H5_AGE_MS, _h5, now_ms),
            "binance_d1": _source(data_dir / "perps/report.json", "perps-d1-paper/1", DAILY_AGE_MS, _d1, now_ms),
            "binance_xs": _source(data_dir / "perps/xs/report.json", "perps-xs-paper/1", DAILY_AGE_MS, _xs, now_ms)}
