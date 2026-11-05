"""Calendar: FOMC meetings, this week's economic releases and fixed crypto market events.

Collector ``calendar`` (every hour). Payload::

    {"events": [{"title", "kind", "country", "impact", "ts", "when", "forecast", "previous", "source", "note"}],
     "fomc": {"meetings": [{"year", "start", "end", "decision_ts", "decision", "sep", "minutes_released"}],
              "next": {... one meeting ..., "days_until"} | None},
     "feeds": [{"name", "ok", "error", "count"}]}

* ``kind``: econ (ForexFactory this-week feed: every country, all impacts; the UI filters),
  fomc (Federal Reserve calendar page: statement 2:00 pm ET on the second day, press conference
  2:30 pm), expiry (Deribit options, the last Friday of each month at 08:00 UTC, quarterly in Mar/Jun/
  Sep/Dec; CME bitcoin futures, the last Friday at 4:00 pm London) and funding (Binance USD-M,
  every 8 hours from 00:00 UTC).
* US Eastern and London times are converted with their DST rules written out here, because this
  Windows Python has no tz database.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .base import Collector, Http, parse_when, utc_iso

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                       "september", "october", "november", "december"], 1)}


# ------------------------------------------------------------------ time zones without tzdata
def _nth_sunday(y: int, m: int, n: int) -> date:
    d = date(y, m, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(y: int, m: int, weekday: int) -> date:
    d = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def eastern_to_utc(d: date, hh: int, mm: int) -> datetime:
    """US Eastern wall time to UTC (EDT from the 2nd Sunday of March to the 1st Sunday of November)."""
    edt = _nth_sunday(d.year, 3, 2) <= d < _nth_sunday(d.year, 11, 1)
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=UTC) + timedelta(hours=4 if edt else 5)


def london_to_utc(d: date, hh: int, mm: int) -> datetime:
    """UK wall time to UTC (BST from the last Sunday of March to the last Sunday of October)."""
    bst = _last_weekday(d.year, 3, 6) <= d < _last_weekday(d.year, 10, 6)
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=UTC) - timedelta(hours=1 if bst else 0)


# ------------------------------------------------------------------ FOMC
def parse_fomc(page: str) -> list[dict[str, Any]]:
    """Meetings from the Fed's calendar page. Handles 'Apr/May' + '30-1', '*' (projections) and
    notes such as '(unscheduled)'; a notation vote has no press conference and is skipped."""
    out = []
    heads = list(re.finditer(r"(\d{4}) FOMC Meetings", page))
    for k, h in enumerate(heads):
        year = int(h.group(1))
        sec = page[h.end(): heads[k + 1].start() if k + 1 < len(heads) else len(page)]
        rows = re.split(r'<div class="[^"]*\brow fomc-meeting\b', sec)[1:]
        for row in rows:
            mo = re.search(r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>', row)
            dt = re.search(r'fomc-meeting__date[^>]*>([^<]+)<', row)
            if not mo or not dt:
                continue
            months = [MONTHS.get(_full(x.strip())) for x in mo.group(1).split("/")]
            raw = dt.group(1).strip()
            if "notation" in raw.lower():
                continue
            days = [int(x) for x in re.findall(r"\d+", raw.split("(")[0])]
            if not days or not all(months):
                continue
            m1, m2 = months[0], months[-1]
            d1, d2 = days[0], days[-1]
            start = date(year, m1, d1)
            end = date(year + (m2 < m1), m2, d2) if len(months) > 1 else date(year, m1, d2)
            released = re.search(r"Released ([A-Z][a-z]+ \d{1,2}, \d{4})", row)
            decision = eastern_to_utc(end, 14, 0)
            out.append({"year": year, "start": start.isoformat(), "end": end.isoformat(),
                        "decision_ts": decision.timestamp(), "decision": utc_iso(decision),
                        "press_conference": utc_iso(decision + timedelta(minutes=30)),
                        "sep": "*" in raw, "unscheduled": "unscheduled" in row.lower(),
                        "minutes_released": released.group(1) if released else None})
    out.sort(key=lambda m: m["decision_ts"])
    return out


def _full(token: str) -> str:
    t = token.lower()
    for name in MONTHS:
        if name.startswith(t[:3]):
            return name
    return t


def next_meeting(meetings: list[dict[str, Any]], now: float) -> dict[str, Any] | None:
    for m in meetings:
        if m["decision_ts"] + 3600 >= now:                      # stays "next" until an hour after the decision
            return dict(m, days_until=round((m["decision_ts"] - now) / 86400, 2))
    return None


# ------------------------------------------------------------------ economic calendar
def parse_ff(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        when = parse_when(r.get("date"))
        if when is None:
            continue
        out.append({"title": str(r.get("title", "")).strip(), "kind": "econ", "country": r.get("country", ""),
                    "impact": r.get("impact", ""), "ts": when.timestamp(), "when": utc_iso(when),
                    "forecast": r.get("forecast") or None, "previous": r.get("previous") or None,
                    "source": "ForexFactory", "note": None})
    return out


# ------------------------------------------------------------------ fixed crypto events
def crypto_events(now: float, days: int = 75) -> list[dict[str, Any]]:
    out = []
    t0 = datetime.fromtimestamp(now, UTC)
    y, m = t0.year, t0.month
    for _ in range(3):
        fri = _last_weekday(y, m, 4)
        quarterly = m in (3, 6, 9, 12)
        deribit = datetime(fri.year, fri.month, fri.day, 8, 0, tzinfo=UTC)
        cme = london_to_utc(fri, 16, 0)
        out.append({"title": f"Deribit {'quarterly' if quarterly else 'monthly'} BTC/ETH options expiry", "kind": "expiry",
                    "country": "CRYPTO", "impact": "High" if quarterly else "Medium", "ts": deribit.timestamp(),
                    "when": utc_iso(deribit), "forecast": None, "previous": None, "source": "computed",
                    "note": "last Friday of the month, 08:00 UTC"})
        out.append({"title": "CME bitcoin futures last trading day", "kind": "expiry", "country": "CRYPTO",
                    "impact": "Medium" if quarterly else "Low", "ts": cme.timestamp(), "when": utc_iso(cme),
                    "forecast": None, "previous": None, "source": "computed", "note": "last Friday, 4:00 pm London"})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    nxt = (int(now) // 28800 + 1) * 28800
    out.append({"title": "Binance USD-M funding", "kind": "funding", "country": "CRYPTO", "impact": "Low", "ts": float(nxt),
                "when": utc_iso(nxt), "forecast": None, "previous": None, "source": "computed",
                "note": "every 8 hours: 00:00, 08:00, 16:00 UTC"})
    return [e for e in out if now - 86400 <= e["ts"] <= now + days * 86400]


def collect(http: Http, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    feeds, events, meetings = [], [], []
    try:
        meetings = parse_fomc(http.get_text(FOMC_URL))
        if not meetings:
            raise ValueError("no meetings found on the Fed calendar page (layout changed?)")
        feeds.append({"name": "Federal Reserve FOMC calendar", "ok": True, "error": None, "count": len(meetings)})
    except Exception as e:  # noqa: BLE001
        feeds.append({"name": "Federal Reserve FOMC calendar", "ok": False, "error": f"{type(e).__name__}: {e}"[:200],
                      "count": 0})
    try:
        econ = parse_ff(http.get_json(FF_URL))
        events += econ
        feeds.append({"name": "ForexFactory this week", "ok": True, "error": None, "count": len(econ)})
    except Exception as e:  # noqa: BLE001
        feeds.append({"name": "ForexFactory this week", "ok": False, "error": f"{type(e).__name__}: {e}"[:200], "count": 0})
    if not meetings and not any(f["ok"] for f in feeds):
        raise RuntimeError("calendar sources all failed")
    for mt in meetings:
        if now - 86400 * 2 <= mt["decision_ts"] <= now + 86400 * 120:
            events.append({"title": f"FOMC rate decision{' + projections (SEP)' if mt['sep'] else ''}", "kind": "fomc",
                           "country": "USD", "impact": "High", "ts": mt["decision_ts"], "when": mt["decision"],
                           "forecast": None, "previous": None, "source": "Federal Reserve",
                           "note": f"meeting {mt['start']} to {mt['end']}; statement 2:00 pm ET, press conference 2:30 pm ET"})
    events += crypto_events(now)
    events.sort(key=lambda e: e["ts"])
    return {"events": events, "fomc": {"meetings": meetings, "next": next_meeting(meetings, now)}, "feeds": feeds}


COLLECTORS = [Collector("calendar", "Calendar", 3600, collect, (FOMC_URL, FF_URL))]
