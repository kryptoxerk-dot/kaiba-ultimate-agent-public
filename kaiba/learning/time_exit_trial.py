"""Randomized time-exit trials on PAPER positions (2026-10-18).

The exit twins (``twin_exits``) found the first variant to clear the owner's pass line:
migration-fade/sol exited at a fixed 60 s (+7.7% per trade over 103 positions, paired +10.9%
over the incumbent exit, still +5.6% at +1% extra cost per leg). That is a REPLAY: the
challenger's fill is reconstructed from recorded quotes. A trial makes it a measurement.

From ``since_ms`` on, every new paper position in a trial lane is assigned to an arm by a hash
of its position id -- half get a time exit after ``hold_s`` (the ``lane_time_exit`` ops job
asks protection for it, the same request path the copy timer and the dashboard use), half keep
the incumbent exit. Both arms fill through the real paper broker, at the same time, on the same
kind of token: the difference between them is the effect of the exit, not of the market week.

Rules every reader follows, through this module:

* the arm is :func:`arm` -- a pure function of the position id, so the job, the paper report,
  the dashboard and the twins agree without storing anything;
* a trial's TREATED positions are reported under their own label (:meth:`Trial.label`), so the
  lane's main scoreboard row stays the incumbent policy, and the twins skip them;
* the split follows the trial's DEFINITION (lane, since_ms, until_ms), not whether the job is
  enabled: switching the job off must not merge time-exited trades back into the lane's row;
* paper only. The job refuses live and canary positions; a live time exit is an owner call.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TREAT, CONTROL = "treat", "control"
JOB = "lane_time_exit"
_SALT = b"kaiba-time-exit-v1:"
MIN_HOLD_S, MAX_HOLD_S = 5, 3_600


@dataclass(frozen=True)
class Trial:
    lane: str
    hold_s: int
    since_ms: int
    until_ms: int | None = None
    split: str = "half"  # "half": 50/50 by position-id hash; "all": every eligible position

    def label(self) -> str:
        return f"{self.lane} [exit {self.hold_s}s]"

    def covers(self, opened_ms: int) -> bool:
        """Was a position opened at ``opened_ms`` eligible for this trial?"""
        if self.since_ms <= 0 or opened_ms < self.since_ms:
            return False
        return self.until_ms is None or opened_ms < self.until_ms


def arm(position_id: str) -> str:
    """TREAT or CONTROL, a pure function of the id: every reader computes the same answer."""
    return TREAT if hashlib.sha256(_SALT + str(position_id).encode()).digest()[0] % 2 == 0 else CONTROL


def trials_from(raw: Any) -> dict[str, Trial]:
    """Parse the job's ``trials`` mapping. A malformed entry is dropped and logged, never guessed."""
    out: dict[str, Trial] = {}
    if not isinstance(raw, Mapping):
        return out
    for lane, spec in raw.items():
        if not isinstance(spec, Mapping):
            continue
        try:
            hold_s = int(spec["hold_s"])
            since_ms = int(spec.get("since_ms") or 0)
            until = spec.get("until_ms")
            until_ms = int(until) if until not in (None, "", 0) else None
            split = str(spec.get("split") or "half")
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("time_exit_trial: %s ignored: %s", lane, exc)
            continue
        if not MIN_HOLD_S <= hold_s <= MAX_HOLD_S or split not in ("half", "all"):
            log.warning("time_exit_trial: %s ignored: hold_s %s / split %s out of range", lane, hold_s, split)
            continue
        out[str(lane)] = Trial(str(lane), hold_s, since_ms, until_ms, split)
    return out


def _schedule_path() -> Path:
    override = os.environ.get("KAIBA_SCHEDULE_CONFIG")
    return Path(override) if override else Path(__file__).resolve().parents[2] / "config" / "schedule.yaml"


def load_trials(path: Path | None = None) -> dict[str, Trial]:
    """Every trial DEFINED in config/schedule.yaml (enabled or not); {} when there is none."""
    import yaml

    try:
        raw = yaml.safe_load((path or _schedule_path()).read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as exc:
        log.warning("time_exit_trial: schedule unreadable: %s", exc)
        return {}
    job = ((raw.get("jobs") or {}) if isinstance(raw, Mapping) else {}).get(JOB) or {}
    return trials_from(job.get("trials") if isinstance(job, Mapping) else None)


def treated(trials: Mapping[str, Trial], lane: str, position_id: str, opened_ms: int) -> Trial | None:
    """The trial that time-exits this position, or None (control arm, out of window, no trial)."""
    trial = trials.get(str(lane))
    if trial is None or not trial.covers(int(opened_ms)):
        return None
    if trial.split == "half" and arm(position_id) != TREAT:
        return None
    return trial


def label_for(trials: Mapping[str, Trial], lane: str, position_id: str, opened_ms: int | None) -> str:
    """The scoreboard label: the lane itself, or the trial's label for a treated position."""
    if opened_ms is None:
        return str(lane)
    trial = treated(trials, lane, position_id, opened_ms)
    return trial.label() if trial else str(lane)
