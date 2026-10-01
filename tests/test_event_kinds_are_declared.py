"""Every event kind the code writes is one ``Event.kind`` can read back.

``events.emit`` accepts ``EventKind | str`` on purpose (a module may land before its kind
is declared), but ``Event.kind`` is strict, so a raw string that is not in the enum is
written happily and then blows up every ``Event``-typed reader that meets it. MEASURED on
the live box 2026-09-22: four undeclared kinds, 9,147 rows, and ``events.recent()`` raised
on the first one it hit.
"""

from __future__ import annotations

import re
from pathlib import Path

from kaiba.core import events as ev
from kaiba.core.schemas import EventKind

ROOT = Path(__file__).resolve().parents[1] / "kaiba"
KNOWN = {k.value for k in EventKind}

#: ``EVENT_FOO = "a.b"`` raw constants, ``EVENT_FOO = EventKind.X.value`` enum-spelled ones,
#: and ``emit("a.b", ...)`` / ``emit_once("a.b", ...)`` literals. The word boundary keeps
#: ``self._emit("heartbeat", ...)`` (a watchdog sub-event name, not a kind) out of it.
RAW_CONST = re.compile(r'^EVENT_[A-Z_]+\s*=\s*"([a-z0-9_.]+)"', re.M)
ENUM_CONST = re.compile(r'^EVENT_[A-Z_]+\s*=\s*EventKind.([A-Z0-9_]+)\.value', re.M)
LITERAL = re.compile(r'(?<!\w)emit(?:_once)?\(\s*"([a-z0-9_.]+)"')  # `_emit(` is not `emit(`; `ev.emit(` is


def _scan() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    raw: dict[str, list[str]] = {}
    by_member: dict[str, list[str]] = {}
    for path in ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = str(path.relative_to(ROOT.parent))
        for pat in (RAW_CONST, LITERAL):
            for m in pat.finditer(text):
                raw.setdefault(m.group(1), []).append(rel)
        for m in ENUM_CONST.finditer(text):
            by_member.setdefault(m.group(1), []).append(rel)
    return raw, by_member


def test_every_kind_the_code_writes_is_declared():
    raw, by_member = _scan()
    assert raw or by_member, "the scan found nothing; the regexes are broken"
    undeclared = {k: v for k, v in raw.items() if k not in KNOWN}
    assert not undeclared, f"raw kinds no Event-typed reader can load: {undeclared}"
    missing = {k: v for k, v in by_member.items() if not hasattr(EventKind, k)}
    assert not missing, f"enum-spelled constants naming no member: {missing}"


def test_the_scan_catches_a_raw_kind():
    """The regexes must still see the shape this test exists to forbid, and not the
    watchdog's ``self._emit("heartbeat", ...)`` sub-event names."""
    assert RAW_CONST.search('EVENT_X = "scan.tier1"\n').group(1) == "scan.tier1"
    assert LITERAL.search('    emit("creators.backfill", {})').group(1) == "creators.backfill"
    assert LITERAL.search('    ev.emit("creators.backfill", {})').group(1) == "creators.backfill"
    assert LITERAL.search('    self._emit("heartbeat", {})') is None
    assert ENUM_CONST.search("EVENT_SCANNED = EventKind.SCAN_TIER1.value").group(1) == "SCAN_TIER1"


def test_the_four_kinds_found_on_the_box_read_back(tmp_db):
    for kind in (EventKind.SCAN_TIER1, EventKind.TRIAGE_VERDICT, EventKind.TRIAGE_BACKPRESSURE, EventKind.CREATORS_BACKFILL):
        ev.emit(kind.value, {"probe": kind.value}, conn=tmp_db)  # written as the raw string, as before
    got = ev.recent(limit=10, conn=tmp_db)
    assert {e.kind for e in got} >= {EventKind.SCAN_TIER1, EventKind.TRIAGE_VERDICT,
                                     EventKind.TRIAGE_BACKPRESSURE, EventKind.CREATORS_BACKFILL}
