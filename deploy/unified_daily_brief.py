"""Compile a daily macro brief from existing, bounded Aegis cache snapshots.

This command never collects data, opens the trading database, or sends messages.
Collection time, event time, release observation date and publication time remain
separate. A new compilation cannot make an old source fresh.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

JAKARTA = timezone(timedelta(hours=7), "Asia/Jakarta")
DAY_MS = 86_400_000
MAX_OUTPUT_BYTES = 512 * 1024
MAX_BRIEF_READ_BYTES = MAX_OUTPUT_BYTES
REPO = Path(__file__).resolve().parents[1]
CATEGORIES = {
    "FOMC": ("FOMC meetings and decisions", r"\bfomc\b|federal open market"),
    "FED": ("Federal Reserve news", r"\bfed\b|federal reserve|\bpowell\b"),
    "RATES": ("Interest rates", r"interest rates?|rate (?:decision|cut|hike)|federal funds|\bDFF\b|\bDGS(?:2|10)\b"),
    "CPI": ("Consumer price inflation", r"\bcpi\b|consumer price|\bCPIAUCSL\b|\bCPILFESL\b"),
    "PCE": ("Personal consumption inflation", r"\bpce\b|personal consumption|\bPCEPILFE\b"),
    "JOBS": ("Jobs and unemployment", r"\bnfp\b|payroll|unemployment|jobless|\bPAYEMS\b|\bUNRATE\b"),
}
_CATEGORY_RE = {key: re.compile(pattern, re.I) for key, (_, pattern) in CATEGORIES.items()}


def _text(value: Any, limit: int = 500) -> str:
    return str(value).replace("\x00", "")[:limit] if value is not None else ""


def _ms(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        if math.isfinite(number) and 0 <= number <= 253_402_300_799_000:
            return int(number)
    except (ValueError, TypeError, OverflowError):
        pass
    return None


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _url(value: Any) -> str | None:
    value = _text(value, 2000)
    try:
        parsed = urlsplit(value)
        if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return None


def _row(row: Any, fields: tuple[str, ...]) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    out = {}
    for key in fields:
        value = row.get(key)
        if key in ("source_url", "url"):
            out[key] = _url(value)
        elif isinstance(value, bool) or value is None:
            out[key] = value
        elif isinstance(value, (int, float)):
            out[key] = value if math.isfinite(value) else None
        elif isinstance(value, str):
            out[key] = _text(value)
    return out


def _rows(snapshot: dict[str, Any], key: str, fields: tuple[str, ...], limit: int) -> list[dict[str, Any]]:
    values = snapshot.get(key)
    if not isinstance(values, list):
        return []
    out = []
    for value in values[:1000]:
        row = _row(value, fields)
        if row is not None:
            out.append(row)
        if len(out) >= limit:
            break
    return out


def _evidence_status(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "unavailable"
    statuses = [row.get("status") for row in rows]
    if all(status == "fresh" for status in statuses):
        return "fresh"
    if any(status in ("fresh", "partial") for status in statuses):
        return "partial"
    if "stale" in statuses:
        return "stale"
    return "unknown"


def compile_brief(snapshot: dict[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    """Pure compilation, suitable for fixture tests and a daily local timer.

    Headlines cover the preceding 24 hours. Events cover the preceding 24 hours
    and next 14 days, plus the next known FOMC meeting within 90 days. Undated
    headlines are counted as unknown and never assigned a publication time.
    """
    now = int(datetime.now(UTC).timestamp() * 1000) if now_ms is None else _ms(now_ms)
    if now is None:
        raise ValueError("now_ms must be a finite nonnegative epoch in milliseconds")
    brief_date = datetime.fromtimestamp(now / 1000, JAKARTA).date().isoformat()
    news_all = _rows(snapshot, "news", (
        "id", "title", "source", "url", "published_at", "published_ms", "group", "status",
    ), 400)
    macro_news = [n for n in news_all if any(rx.search(n.get("title", "")) for rx in _CATEGORY_RE.values())]
    news = [n for n in macro_news if (published := _ms(n.get("published_ms"))) is not None
            and now - DAY_MS <= published <= now]
    news.sort(key=lambda row: row["published_ms"], reverse=True)
    news = news[:50]
    events_all = _rows(snapshot, "macro_events", (
        "id", "title", "kind", "country", "impact", "event_at", "event_ms", "forecast", "previous",
        "actual", "source", "source_url", "status",
    ), 300)
    events = [event for event in events_all if (event_ms := _ms(event.get("event_ms"))) is not None
              and now - DAY_MS <= event_ms <= now + 14 * DAY_MS
              and any(rx.search(event.get("title", "")) for rx in _CATEGORY_RE.values())]
    next_fomc = next(iter(sorted(
        (event for event in events_all if _CATEGORY_RE["FOMC"].search(event.get("title", ""))
         and (event_ms := _ms(event.get("event_ms"))) is not None and now <= event_ms <= now + 90 * DAY_MS),
        key=lambda event: event["event_ms"],
    )), None)
    if next_fomc is not None and next_fomc not in events:
        events.append(next_fomc)
    events.sort(key=lambda event: event["event_ms"])
    events = events[:80]
    series = _rows(snapshot, "macro_series", (
        "id", "name", "value", "units", "report_date", "change", "status", "source_url",
    ), 40)
    series = [row for row in series if any(rx.search(f"{row.get('id', '')} {row.get('name', '')}")
                                         for rx in _CATEGORY_RE.values())][:30]
    source_health = _rows(snapshot, "collectors", (
        "key", "title", "status", "fetched_at", "age_s", "interval_s", "error", "partial",
    ), 20)
    source_health = [source for source in source_health if source.get("key") in ("calendar", "news", "macro_fred")]
    categories = {}
    for key, (label, _) in CATEGORIES.items():
        rx = _CATEGORY_RE[key]
        news_rows = [row for row in news if rx.search(row.get("title", ""))]
        event_rows = [row for row in events if rx.search(row.get("title", ""))]
        series_rows = [row for row in series if rx.search(f"{row.get('id', '')} {row.get('name', '')}")]
        matching_news = [row["id"] for row in news_rows if row.get("id")]
        matching_events = [row["id"] for row in event_rows if row.get("id")]
        matching_series = [row["id"] for row in series_rows if row.get("id")]
        categories[key] = {
            "label": label, "news_ids": matching_news, "event_ids": matching_events, "series_ids": matching_series,
            "coverage": "available" if matching_news or matching_events or matching_series else "unavailable",
            "status": _evidence_status(news_rows + event_rows + series_rows),
        }
    observations = news + events + series
    usable = sum(row.get("status") in ("fresh", "partial") for row in observations)
    if not observations:
        status = "unavailable"
    elif not usable:
        status = "stale"
    elif (_evidence_status(observations) == "fresh"
          and all(category["status"] == "fresh" for category in categories.values())
          and all(source.get("status") == "fresh" for source in source_health) and len(source_health) == 3):
        status = "fresh" if news else "partial"
    else:
        status = "partial"
    bridge = snapshot.get("bridge") if isinstance(snapshot.get("bridge"), dict) else {}
    return {
        "schema_version": 1,
        "brief_date": brief_date,
        "timezone": "Asia/Jakarta",
        "compiled_at": _iso(now), "compiled_at_ms": now,
        "window_start_ms": now - DAY_MS, "window_end_ms": now,
        "status": status,
        "summary": f"{len(news)} cached macro headlines published in 24h; {len(events)} calendar events; {len(series)} economic series.",
        "coverage": {"current_news": sum(row.get("status") in ("fresh", "partial") for row in news),
                     "news_in_window": len(news), "events": len(events), "series": len(series),
                     "undated_macro_news": sum(_ms(row.get("published_ms")) is None for row in macro_news),
                     "bridge_status": _text(bridge.get("status") or "unavailable", 40)},
        "categories": categories, "events": events, "news": news, "series": series,
        "source_health": source_health,
        "limitations": [
            "Compiled from existing caches only; no data collection or provider calls were performed.",
            "Compilation time does not change source freshness or the underlying release observation date.",
            "A scheduled event is not a confirmed release; missing actual, forecast or previous values stay unknown.",
            "No cached headline in this window does not establish that no macro news occurred.",
            "This brief is market context; profitability and a tradable edge are unproven.",
        ],
    }


def _md(value: Any) -> str:
    return re.sub(r"[\r\n\t]+", " ", _text(value)).replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")


def render_markdown(brief: dict[str, Any]) -> str:
    """A plain, source-linked daily note; never manufactures an economic result."""
    lines = [f"# Kaiba daily macro brief — {_md(brief['brief_date'])}", "",
             f"Compiled: {_md(brief['compiled_at'])} · Timezone: Asia/Jakarta · Status: **{_md(brief['status'])}**", "",
             _md(brief["summary"]), "", "## Cached headlines published in the preceding 24 hours", ""]
    for row in brief["news"]:
        title, url = _md(row.get("title")), row.get("url")
        lines.append(f"- [{title}](<{url}>)" if url else f"- {title}")
        lines.append(f"  Source: {_md(row.get('source'))} · Published: {_md(row.get('published_at')) or 'unknown'}"
                     f" · Source status: {_md(row.get('status')) or 'unknown'}")
    if not brief["news"]:
        lines.append("No current cached macro headlines. Source collection must be checked separately.")
    lines += ["", "## Calendar and next FOMC meeting", ""]
    for row in brief["events"]:
        event_ms = _ms(row.get("event_ms"))
        local_time = datetime.fromtimestamp(event_ms / 1000, JAKARTA).strftime("%Y-%m-%d %H:%M WIB") if event_ms else "unknown"
        title, url = _md(row.get("title")), row.get("source_url")
        lines.append(f"- [{title}](<{url}>)" if url else f"- {title}")
        lines.append(f"  Scheduled: {local_time} ({_md(row.get('event_at')) or 'UTC unknown'})"
                     f" · Actual: {_md(row.get('actual')) or 'unknown'}"
                     f" · Forecast: {_md(row.get('forecast')) or 'unknown'}"
                     f" · Previous: {_md(row.get('previous')) or 'unknown'}"
                     f" · Source status: {_md(row.get('status')) or 'unknown'}")
    if not brief["events"]:
        lines.append("No cached macro events available in the briefing window; upcoming releases remain unknown.")
    lines += ["", "## Latest cached economic observations", ""]
    for row in brief["series"]:
        lines.append(f"- {_md(row.get('name') or row.get('id'))}: {_md(row.get('value')) or 'unknown'} "
                     f"{_md(row.get('units'))} · Observation date: {_md(row.get('report_date')) or 'unknown'}"
                     f" · Source status: {_md(row.get('status')) or 'unknown'}")
        if row.get("source_url"):
            lines.append(f"  Source: <{row['source_url']}>")
    if not brief["series"]:
        lines.append("No cached rate, inflation or labor observations are available.")
    lines += ["", "## Source collection health", ""]
    for source in brief["source_health"]:
        lines.append(f"- {_md(source.get('title') or source.get('key'))}: {_md(source.get('status')) or 'unknown'}"
                     f" · Collected: {_md(source.get('fetched_at')) or 'unknown'}"
                     f" · Error: {_md(source.get('error')) or 'none reported'}")
    if not brief["source_health"]:
        lines.append("Source cache collection is unavailable.")
    lines += ["", "## Coverage", ""]
    lines += [f"- {value['label']}: {value['status']}" for value in brief["categories"].values()]
    lines += ["", "## Limits", ""] + [f"- {_md(item)}" for item in brief["limitations"]]
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, content: bytes) -> None:
    if len(content) > MAX_OUTPUT_BYTES:
        raise ValueError("brief exceeds output size limit")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_brief(brief: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    """Write only four fixed/narrowly date-derived brief artifacts, atomically per file."""
    day = brief.get("brief_date")
    if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("invalid brief date")
    datetime.strptime(day, "%Y-%m-%d")
    json_body = (json.dumps(brief, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    markdown_body = render_markdown(brief).encode("utf-8")
    if max(len(json_body), len(markdown_body)) > MAX_OUTPUT_BYTES:
        raise ValueError("brief exceeds output size limit")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {"dated_json": output_dir / f"{day}.json", "dated_markdown": output_dir / f"{day}.md",
             "latest_json": output_dir / "latest.json", "latest_markdown": output_dir / "latest.md"}
    for key, path in paths.items():
        _atomic_write(path, json_body if key.endswith("json") else markdown_body)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, help="Existing Aegis intel cache directory; no collection is triggered")
    parser.add_argument("--output-dir", type=Path, default=REPO / "data" / "briefings")
    args = parser.parse_args(argv)
    from dashboard.ultimate_intel import build_intelligence

    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    snapshot = build_intelligence(cache_root=args.cache_root, now_ms=now_ms)
    brief = compile_brief(snapshot, now_ms=now_ms)
    paths = write_brief(brief, args.output_dir)
    print(json.dumps({"brief_date": brief["brief_date"], "status": brief["status"],
                      "current_news": brief["coverage"]["current_news"], "latest_json": str(paths["latest_json"]),
                      "collection": "not_run", "provider_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
