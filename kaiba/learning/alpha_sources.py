"""Which alpha sources' picks went up afterwards, against an ordinary token at the same time.

The question (2026-10-03): Kaiba records picks from several "alpha" sources and nobody has
ever scored them. For every pick this module takes the time the SOURCE first published it,
prices it then and at +1 h / +6 h / +24 h off our own swap tape, records the best multiple
inside 24 h and whether it fell 50% before it doubled, and sets that against tokens Kaiba's
scanner saw on the same chain within +/-30 minutes at the same token age. It writes
nothing; it opens the database read-only and reports.

Sources it reads (and how each is recognised)
---------------------------------------------
* ``alpha.signal`` events from :mod:`kaiba.ingest.gmgn_feeds` -> ``gmgn:<feed>``
  (``signal`` / ``trending`` / ``trenches``). ``signal`` carries the source's own
  timestamp; the other two publish none, so our first sighting is the publish time.
* ``alpha.signal`` events from the tracker (payload ``tracker``) -> ``tracker:confluence``.
* ``alpha.meta`` ``record_type == "alert"`` from :mod:`kaiba.ingest.rhscannerr` ->
  ``rhscannerr:alert`` at the Telegram message time. Its ``milestone`` replies report a
  multiple of an earlier alert; they are follow-ups, never picks, and are only counted.
* ``alpha.call`` events from :mod:`kaiba.ingest.telegram_calls` -> ``call:<platform>:<room>``.
* The Hermes cron job's saved outputs, when ``--hermes-dir`` is given -> ``hermes:alpha-scan``.
  A run that answered ``[SILENT]`` made no pick. Publish time is the file's mtime.
* Hunter leads (``alpha_signals``: certificates, governance spaces, CEX listings, DefiLlama
  venues) name a domain or a ticker, not a token on a chain we can price. They are counted
  under ``unscored`` and never silently dropped.

What "price" means here, and the four ways it can be wrong
----------------------------------------------------------
Every price is a ``swaps.price_usd`` print (TEXT; converted in Python, never compared in
SQL), and a token is priced from ONE swap source, because sources disagree on scale (see
:func:`_series`). The tape is not the same on every chain, and the honest answer depends on it:

1. **Robinhood's Pons poller is continuous; nothing else is.** ``source='robinhood'`` is a
   chain-wide poller of every Pons curve, so on a token it covers, silence means the token
   did not trade and the last print carries. Anywhere else silence means "not collected",
   and a horizon is only priced when a print lands within ``max(10 min, h/4)`` of it.
2. **The poller has outages.** A gap of more than :data:`DARK_GAP_MS` between any two of its
   prints on the whole chain is an outage, not a quiet market (the 99.9th-percentile gap is
   5 s; the box has a 143-minute and an 816-minute hole). Continuity ends at the outage.
3. **Graduation ends the curve tape.** A Pons or pump.fun token that graduates stops
   printing on the curve at exactly the moment it succeeded. Carrying its last curve print
   would book a winner as flat, so a horizon after graduation is CENSORED (reason
   ``graduated``) and the graduation share is reported beside every return.
4. **Sol's tape is a one-off walk.** ``pumpfun:trades`` is filled when the scanner looks at
   a token and ends there (MEASURED: median last print 2 minutes after a scan). A sol
   horizon is priced only where ``token_tape`` proves coverage or a print lands near it, so
   most sol horizons come back censored. That is the finding, not a bug in this module.

Robustness: on a continuous on-chain tape a price is the last print (exact); on a mixed or
sparse tape it is the median of the last (up to) three prints. A barrier (2x up, -50% down)
or a peak counts only when two consecutive prints both sit beyond it (on a continuous tape
the final print is confirmed by the silence after it). One mispriced swap therefore cannot
make a token "reach 2x".

The comparison
--------------
Every pick is paired with its own cell: scanner observations (``scan.tier1``, one per
token per 30 min) on the same chain, within +/-:data:`MATCH_WINDOW_MS` of the pick, in the
same token-age band, excluding the picked token and every token that source picked. A
pick is scored only when its cell holds at least :data:`MIN_CELL` priced observations. The
difference is ``pick - mean(cell)`` per pick, so the reported difference is exactly
``pick mean - baseline mean`` over the same picks, and its 90% interval is a percentile
bootstrap over picks.

**Reach-N confound** (memory note ``reach-n-activity-confound``): a token with more prints
has more chances to print a high. Reach-2x is therefore reported twice: matched on age only,
and matched on age AND forward print-count band (:data:`ACTIVITY_EDGES`). If the first
shows a lift and the second does not, the lift was activity.

Verdict rule, fixed before any number was seen: the 24 h mean-return difference if at
least :data:`MIN_PAIRS_FOR_VERDICT` picks pair there, else 6 h, else 1 h (chosen by n, never
by the result). Interval wholly above 0 -> "beats baseline", wholly below -> "worse",
otherwise "no different". A 24 h hold is not Kaiba's exit ladder and nothing here is net of
cost (~1% per leg on GMGN); a positive difference is a lead, not a strategy.

GMGN kline was the specified fallback for picks with no tape. ``kaiba.providers.gmgn_cli``
does not allow ``("market", "kline")``, so this module makes no provider call at all and
says so in its output (:func:`kline_route`). It never goes around the wrapper: the GMGN
bucket is shared with live stop-losses.

Run on the box (read-only)::

    nice -n 10 .venv/bin/python -m kaiba.learning.alpha_sources --db data/kaiba.db \\
        --since-days 7 [--chain robinhood] [--hermes-dir PATH] [--json]
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import random
import re
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
DAY_MS = 24 * HOUR_MS

#: Forward horizons, measured from the source's publish time.
HORIZONS: tuple[tuple[str, int], ...] = (("1h", HOUR_MS), ("6h", 6 * HOUR_MS), ("24h", DAY_MS))
#: The window the best multiple and the barriers are read over.
WINDOW_MS: int = DAY_MS
#: Baseline tokens must be seen by the scanner within this of the pick (the brief: +/-30 min).
MATCH_WINDOW_MS: int = 30 * MINUTE_MS
#: The entry price is read from prints at most this old at the pick.
ENTRY_STALE_MS: int = 10 * MINUTE_MS
#: With no print before the pick, the first print this soon after it is the entry.
ENTRY_GRACE_MS: int = 2 * MINUTE_MS
#: On a non-continuous tape a horizon is priced only by a print this close before it.
HORIZON_TOLERANCE_FRACTION: float = 0.25
HORIZON_TOLERANCE_FLOOR_MS: int = 10 * MINUTE_MS
UP_MULTIPLE: float = 2.0
DOWN_MULTIPLE: float = 0.5
#: Fewest priced baseline observations a pick's cell needs before the pick is scored.
MIN_CELL: int = 3
#: Token-age bands (upper edges). Log-spaced so "10 minutes old" meets "~10 minutes old".
AGE_EDGES_MS: tuple[int, ...] = (
    2 * MINUTE_MS, 5 * MINUTE_MS, 15 * MINUTE_MS, HOUR_MS, 4 * HOUR_MS, DAY_MS, 7 * DAY_MS,
)
#: Forward print-count bands for the reach-N stratification.
ACTIVITY_EDGES: tuple[int, ...] = (10, 50, 200)
#: A gap this long in a continuous source's prints across the whole chain is an outage.
DARK_GAP_MS: int = 10 * MINUTE_MS
#: Swap sources that see every trade on the venue they cover, per chain.
CONTINUOUS_SOURCES: Mapping[str, frozenset[str]] = {"robinhood": frozenset({"robinhood"})}
#: Chains where ``token_tape`` proves a per-token window of complete coverage.
TOKEN_TAPE_CHAINS: frozenset[str] = frozenset({"sol"})
#: A token first published by a source in this much time before the window is a re-publication.
PICK_LOOKBACK_MS: int = DAY_MS
#: One baseline observation per token per this long.
SCAN_RESAMPLE_MS: int = 30 * MINUTE_MS
#: A source-reported timestamp is used only when within this of our own sighting.
MAX_SOURCE_LAG_MS: int = 6 * HOUR_MS
BOOTSTRAP_ROUNDS: int = 2000
CI_LEVEL: float = 0.90
BOOTSTRAP_SEED: int = 20261003
MIN_PAIRS_FOR_VERDICT: int = 10

KIND_SIGNAL = "alpha.signal"
KIND_META = "alpha.meta"
KIND_CALL = "alpha.call"
KIND_SCAN = "scan.tier1"
HERMES_SOURCE = "hermes:alpha-scan"

_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_B58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_LINK_CHAIN_RE = re.compile(
    r"(?:gmgn\.ai/|dexscreener\.com/)([a-z]+)/(?:token/)?(0x[0-9a-fA-F]{40})", re.IGNORECASE
)
#: EVM chains tried, in order, when a bare 0x address has to be placed on a chain.
EVM_RESOLVE_ORDER: tuple[str, ...] = ("robinhood", "bsc", "base", "eth")

METRICS: tuple[str, ...] = (
    "ret_1h", "ret_6h", "ret_24h", "gx_1h", "gx_6h", "gx_24h", "reach_up", "fell_first", "max_multiple",
)


# --------------------------------------------------------------------------------------
# data shapes
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Pick:
    """One token a source put forward, at the time the source first published it."""

    source: str
    chain: str
    token: str
    published_ms: int
    seen_ms: int
    created_ms: int | None = None


@dataclass
class Outcome:
    """What the token did after ``t``. ``None`` always means unknown, never zero."""

    entry: float | None
    entry_basis: str = "none"  # before | after | none
    returns: dict[str, float | None] = field(default_factory=dict)  # percent
    #: Same as ``returns`` except a horizon after graduation is valued at the last curve
    #: print before graduation ("sold at graduation") instead of being censored.
    grad_exit: dict[str, float | None] = field(default_factory=dict)
    censored: dict[str, str] = field(default_factory=dict)  # horizon -> reason
    max_multiple: float | None = None
    reach_up: bool | None = None
    fell_first: bool | None = None
    graduated: bool | None = None
    prints: int = 0
    window_complete: bool = False

    def metric(self, name: str) -> float | None:
        if name.startswith("ret_"):
            return self.returns.get(name[4:])
        if name.startswith("gx_"):
            return self.grad_exit.get(name[3:])
        if name == "max_multiple":
            return self.max_multiple
        value = getattr(self, name)
        return None if value is None else (1.0 if value else 0.0)


@dataclass(frozen=True)
class BaseObs:
    chain: str
    token: str
    t: int
    band: str | None
    outcome: Outcome


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------


def _jload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int | None:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _price(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) and out > 0 else None


def _token_key(token: str) -> str:
    """EVM addresses are stored lowercase; base58 is case-sensitive and left alone."""
    token = token.strip()
    return token.lower() if _EVM_RE.match(token) else token


def looks_like_token(token: Any) -> bool:
    return isinstance(token, str) and bool(_EVM_RE.match(token.strip()) or _B58_RE.match(token.strip()))


def age_band(age_ms: int | None) -> str | None:
    """The age band a token of ``age_ms`` falls in; ``None`` when the age is unknown."""
    if age_ms is None or age_ms < 0:
        return None
    for i, edge in enumerate(AGE_EDGES_MS):
        if age_ms < edge:
            return f"age{i}"
    return f"age{len(AGE_EDGES_MS)}"


def activity_band(prints: int) -> str:
    for i, edge in enumerate(ACTIVITY_EDGES):
        if prints < edge:
            return f"act{i}"
    return f"act{len(ACTIVITY_EDGES)}"


def horizon_tolerance_ms(horizon_ms: int) -> int:
    return max(HORIZON_TOLERANCE_FLOOR_MS, int(horizon_ms * HORIZON_TOLERANCE_FRACTION))


def _median_last(ts: Sequence[int], px: Sequence[float], lo: int, hi: int, k: int = 3) -> float | None:
    """Median of the last ``k`` prints with ``lo <= ts <= hi``."""
    j = bisect.bisect_right(ts, hi)
    i = max(bisect.bisect_left(ts, lo), j - k)
    if i >= j:
        return None
    return statistics.median(px[i:j])


def weighted_median(values: Sequence[float], weights: Sequence[float]) -> float | None:
    pairs = sorted((v, w) for v, w in zip(values, weights, strict=True) if w > 0)
    if not pairs:
        return None
    total = sum(w for _, w in pairs)
    acc = 0.0
    for i, (v, w) in enumerate(pairs):
        acc += w
        if acc > total / 2:
            return v
        if math.isclose(acc, total / 2) and i + 1 < len(pairs):
            return (v + pairs[i + 1][0]) / 2
    return pairs[-1][0]


def bootstrap_ci(
    values: Sequence[float], *, rounds: int = BOOTSTRAP_ROUNDS, level: float = CI_LEVEL,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float] | None:
    """Percentile bootstrap interval for the mean of ``values``."""
    n = len(values)
    if n == 0:
        return None
    if n == 1:
        return (values[0], values[0])
    rng = random.Random(seed)
    data = list(values)
    means = sorted(sum(rng.choices(data, k=n)) / n for _ in range(rounds))
    tail = (1.0 - level) / 2.0
    lo = means[int(math.floor(tail * rounds))]
    hi = means[min(rounds - 1, int(math.ceil((1.0 - tail) * rounds)) - 1)]
    return (lo, hi)


# --------------------------------------------------------------------------------------
# forward outcome
# --------------------------------------------------------------------------------------


def forward_outcome(
    ts: Sequence[int],
    px: Sequence[float],
    t: int,
    *,
    now_ms: int,
    continuous_until: int | None = None,
    migrated_ms: int | None = None,
) -> Outcome:
    """What a token whose prints are ``(ts, px)`` (sorted by time) did after ``t``.

    ``continuous_until`` is the time through which the tape is known complete (silence means
    no trade); ``None`` means silence means nothing. ``migrated_ms`` is graduation, after
    which the curve tape is blind.
    """
    if migrated_ms is not None and migrated_ms <= t:
        # Already graduated: trading moved off the curve, so the curve's silence proves nothing.
        continuous_until = None
    # An on-chain continuous tape is exact, so its last print is the price; a mixed or sparse
    # tape takes the median of its last three so one mispriced swap cannot set it.
    k = 1 if continuous_until is not None else 3
    entry = _median_last(ts, px, t - ENTRY_STALE_MS, t, k)
    start, basis = t, "before"
    if entry is None:
        j = bisect.bisect_right(ts, t)
        if j < len(ts) and ts[j] <= t + ENTRY_GRACE_MS:
            entry, start, basis = px[j], ts[j], "after"
        else:
            return Outcome(entry=None)
    out = Outcome(entry=entry, entry_basis=basis)

    cont_end = continuous_until if continuous_until is not None and continuous_until >= start else None
    if cont_end is not None:
        cont_end = min(cont_end, now_ms)
    grad = migrated_ms if migrated_ms is not None and migrated_ms > t else None

    for name, h in HORIZONS:
        horizon = t + h
        if horizon > now_ms:
            out.returns[name], out.censored[name] = None, "not_elapsed"
            out.grad_exit[name] = None
            continue
        if grad is not None and grad <= horizon:
            out.returns[name], out.censored[name] = None, "graduated"
            if cont_end is not None and cont_end >= grad:
                at_grad = _median_last(ts, px, t - ENTRY_STALE_MS, grad, k)
            else:
                at_grad = _median_last(ts, px, grad - horizon_tolerance_ms(h), grad, k)
            out.grad_exit[name] = None if at_grad is None else (at_grad / entry - 1.0) * 100.0
            continue
        if cont_end is not None and cont_end >= horizon:
            price = _median_last(ts, px, t - ENTRY_STALE_MS, horizon, k)
        else:
            price = _median_last(ts, px, horizon - horizon_tolerance_ms(h), horizon, k)
        if price is None:
            out.returns[name], out.censored[name] = None, (
                "tape_gap" if cont_end is None else "continuity_ended"
            )
            out.grad_exit[name] = None
            continue
        out.returns[name] = out.grad_exit[name] = (price / entry - 1.0) * 100.0

    window_end = t + WINDOW_MS
    if cont_end is not None:
        observed_end = min(cont_end, window_end)
        complete = observed_end >= window_end
    else:
        j = bisect.bisect_right(ts, window_end)
        observed_end = ts[j - 1] if j > 0 else start
        complete = observed_end >= window_end - horizon_tolerance_ms(WINDOW_MS)
    complete = complete and window_end <= now_ms
    if grad is not None and grad <= window_end:
        observed_end = min(observed_end, grad)
        complete = False
        out.graduated = True
    elif window_end <= now_ms:
        out.graduated = False

    a = bisect.bisect_right(ts, start)
    b = bisect.bisect_right(ts, observed_end)
    out.prints = max(0, b - a)
    up_ts = down_ts = None
    peak = entry

    def _visit(lo_px: float, hi_px: float, when: int) -> None:
        nonlocal up_ts, down_ts, peak
        peak = max(peak, lo_px)
        if up_ts is None and lo_px >= UP_MULTIPLE * entry:
            up_ts = when
        if down_ts is None and hi_px <= DOWN_MULTIPLE * entry:
            down_ts = when

    for i in range(a + 1, b):
        _visit(min(px[i - 1], px[i]), max(px[i - 1], px[i]), ts[i])
    if cont_end is not None and b > a and observed_end > ts[b - 1]:
        # On a continuous tape the last print held until the end of what we observed.
        _visit(px[b - 1], px[b - 1], ts[b - 1])

    out.window_complete = complete
    # A barrier result is only read off a window that ran its course, or one cut short by
    # graduation (the curve ending because the token succeeded). A window cut short because
    # our tape stopped says nothing: counting the hits seen before it stopped and dropping
    # the rest selects on the outcome (MEASURED: ~97% of sol, picks AND baseline, "reached 2x").
    if complete or out.graduated:
        if up_ts is not None:
            out.reach_up = True
        elif complete:
            out.reach_up = False
        if down_ts is not None and (up_ts is None or down_ts < up_ts):
            out.fell_first = True
        elif up_ts is not None or complete:
            out.fell_first = False
    out.max_multiple = peak / entry if complete else None
    return out


# --------------------------------------------------------------------------------------
# reading the database
# --------------------------------------------------------------------------------------


def connect_ro(path: str | Path) -> sqlite3.Connection:
    """Open ``path`` read-only. A missing file is an error, never a new empty database."""
    uri = f"file:{Path(path).as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=30)


def _events_of_kind(
    conn: sqlite3.Connection, kind: str, lo_ms: int, hi_ms: int, columns: str,
) -> sqlite3.Cursor:
    """Rows of one event kind with ``lo_ms <= ts_ms <= hi_ms``, oldest id first.

    Forced onto ``idx_events_kind`` so the cost is the rows of that kind, never a range of
    the whole events table, and filtered on ``ts_ms`` itself rather than on an id cut-off:
    ids are only roughly time-ordered, and an id bound silently drops late-written rows.
    """
    return conn.execute(
        f"SELECT {columns} FROM events INDEXED BY idx_events_kind "
        "WHERE kind=? AND ts_ms>=? AND ts_ms<=? ORDER BY id",
        (kind, lo_ms, hi_ms),
    )


def data_now_ms(conn: sqlite3.Connection) -> int:
    """The database's own clock: the newest event's timestamp."""
    row = conn.execute("SELECT ts_ms FROM events ORDER BY id DESC LIMIT 1").fetchone()
    return int(row[0]) if row and row[0] is not None else int(time.time() * 1000)


def _publish_time(source_ms: Any, seen_ms: int) -> int:
    """The source's own timestamp when it is plausible, else our first sighting."""
    value = _int(source_ms)
    if value is None:
        return seen_ms
    if value > 10**14:  # microseconds or nanoseconds would be a unit error; refuse it
        return seen_ms
    if value < 10**11:  # seconds
        value *= 1000
    if seen_ms - MAX_SOURCE_LAG_MS <= value <= seen_ms + MINUTE_MS:
        return value
    return seen_ms


def parse_pick(kind: str, ts_ms: int, chain: str | None, subject: str | None,
               payload: Mapping[str, Any]) -> Pick | str:
    """One event -> a :class:`Pick`, or the reason it is not one."""
    if kind == KIND_SIGNAL:
        if "tracker" in payload:
            source, token = "tracker:confluence", payload.get("token") or subject
            published = ts_ms
        elif payload.get("provider") == "gmgn":
            feed = payload.get("feed") or "unknown"
            source, token = f"gmgn:{feed}", payload.get("token") or subject
            chain = payload.get("chain") or chain
            published = _publish_time(payload.get("timestamp_ms"), ts_ms) if feed == "signal" else ts_ms
        else:
            name = f"{payload.get('source') or 'unknown'}:{payload.get('signal_kind') or 'unknown'}"
            token = subject
            if not chain or not looks_like_token(token):
                return f"not_a_token:{name}"
            source, published = name, _publish_time(payload.get("event_at_ms"), ts_ms)
    elif kind == KIND_META:
        record = payload.get("record_type")
        name = str(payload.get("source") or "unknown").removeprefix("telegram:")
        if record != "alert":
            return f"follow_up_not_pick:{name}:{record}"
        source, token = f"{name}:alert", payload.get("address") or subject
        chain = payload.get("chain") or chain
        published = _publish_time(payload.get("message_ts_ms"), ts_ms)
    elif kind == KIND_CALL:
        room = payload.get("channel") or payload.get("caller_id") or "unknown"
        source = f"call:{payload.get('platform') or 'telegram'}:{room}"
        token, chain = payload.get("token") or subject, payload.get("chain") or chain
        published = _publish_time(payload.get("called_ms"), ts_ms)
    else:
        return f"unknown_kind:{kind}"
    if not chain:
        return f"no_chain:{source}"
    if not looks_like_token(token):
        return f"not_a_token:{source}"
    created = _int(payload.get("created_ms"))
    return Pick(source=source, chain=str(chain), token=_token_key(str(token)),
                published_ms=published, seen_ms=ts_ms, created_ms=created)


def load_picks(
    conn: sqlite3.Connection, since_ms: int, until_ms: int, chains: Iterable[str] | None = None,
) -> tuple[list[Pick], Counter[str]]:
    """First publication per (source, chain, token) inside ``[since_ms, until_ms]``."""
    wanted = set(chains) if chains else None
    skipped: Counter[str] = Counter()
    first: dict[tuple[str, str, str], Pick] = {}
    for kind in (KIND_SIGNAL, KIND_META, KIND_CALL):
        cur = _events_of_kind(conn, kind, since_ms - PICK_LOOKBACK_MS, until_ms, "ts_ms, chain, subject, payload")
        for ts_ms, chain, subject, payload in cur:
            parsed = parse_pick(kind, int(ts_ms), chain, subject, _jload(payload))
            if isinstance(parsed, str):
                skipped[parsed] += 1
                continue
            if wanted is not None and parsed.chain not in wanted:
                continue
            key = (parsed.source, parsed.chain, parsed.token)
            held = first.get(key)
            if held is None or parsed.published_ms < held.published_ms:
                first[key] = parsed
    picks = []
    for pick in first.values():
        if pick.published_ms < since_ms:
            skipped[f"republished:{pick.source}"] += 1
            continue
        picks.append(pick)
    picks.sort(key=lambda p: (p.source, p.chain, p.published_ms))
    return picks, skipped


def resolve_evm_chain(conn: sqlite3.Connection, address: str, text: str = "") -> str | None:
    """Which chain a bare 0x address lives on: an explicit link first, then our tokens table."""
    for chain, addr in _LINK_CHAIN_RE.findall(text or ""):
        if addr.lower() == address.lower():
            return {"solana": "sol", "ethereum": "eth"}.get(chain.lower(), chain.lower())
    for chain in EVM_RESOLVE_ORDER:
        row = conn.execute(
            "SELECT 1 FROM tokens WHERE chain=? AND address=?", (chain, address.lower())
        ).fetchone()
        if row is not None:
            return chain
    return None


def hermes_response(text: str) -> str:
    """The model's answer section of one Hermes cron output file."""
    marker = "## Response"
    return text.split(marker, 1)[1] if marker in text else ""


def load_hermes_picks(
    conn: sqlite3.Connection, directory: str | Path, since_ms: int, until_ms: int,
    chains: Iterable[str] | None = None,
) -> tuple[list[Pick], dict[str, int]]:
    """Picks named in the Hermes alpha-scan outputs. Read-only: files are only opened for reading."""
    from kaiba.core.schemas import Chain
    from kaiba.ingest.telegram_calls import extract_addresses

    wanted = set(chains) if chains else None
    stats = {"runs": 0, "silent_runs": 0, "runs_with_text": 0, "addresses": 0, "unresolved": 0}
    first: dict[tuple[str, str], Pick] = {}
    for path in sorted(Path(directory).glob("*.md")):
        stamp = int(path.stat().st_mtime * 1000)
        if not since_ms <= stamp <= until_ms:
            continue
        stats["runs"] += 1
        answer = hermes_response(path.read_text(encoding="utf-8", errors="replace"))
        if "[SILENT]" in answer:
            stats["silent_runs"] += 1
            continue
        stats["runs_with_text"] += 1
        for chain_enum, address in extract_addresses(answer, evm_chain=Chain.ETH):
            stats["addresses"] += 1
            chain = "sol" if chain_enum is Chain.SOL else resolve_evm_chain(conn, address, answer)
            if chain is None:
                stats["unresolved"] += 1
                continue
            if wanted is not None and chain not in wanted:
                continue
            key = (chain, _token_key(address))
            if key not in first:
                first[key] = Pick(HERMES_SOURCE, chain, key[1], stamp, stamp)
    return sorted(first.values(), key=lambda p: p.published_ms), stats


def load_scans(
    conn: sqlite3.Connection, since_ms: int, until_ms: int, chains: Iterable[str] | None = None,
) -> list[tuple[str, str, int]]:
    """Scanner sightings, at most one per token per :data:`SCAN_RESAMPLE_MS`."""
    wanted = set(chains) if chains else None
    rows = []
    for ts_ms, chain, subject in _events_of_kind(conn, KIND_SCAN, since_ms, until_ms, "ts_ms, chain, subject"):
        if not chain or not subject:
            continue
        if wanted is not None and chain not in wanted:
            continue
        rows.append((int(ts_ms), str(chain), _token_key(str(subject))))
    rows.sort()
    last: dict[tuple[str, str], int] = {}
    out = []
    for ts_ms, chain, token in rows:
        prev = last.get((chain, token))
        if prev is not None and ts_ms - prev < SCAN_RESAMPLE_MS:
            continue
        last[(chain, token)] = ts_ms
        out.append((chain, token, ts_ms))
    return out


@dataclass
class ChainTape:
    """What we know about a continuous source's coverage of a whole chain."""

    chain: str
    sources: frozenset[str]
    first_ms: int | None = None
    last_ms: int | None = None
    dark: list[tuple[int, int]] = field(default_factory=list)  # (last print before, first after)

    def continuous_until(self, t: int) -> int | None:
        """Through when the tape is complete, starting at ``t``; ``None`` if ``t`` is dark."""
        if self.first_ms is None or self.last_ms is None or not self.first_ms <= t <= self.last_ms:
            return None
        for gap_start, gap_end in self.dark:
            if gap_end <= t:
                continue
            if gap_start < t:
                return None
            return gap_start
        return self.last_ms


def _slot_at_or_after(conn: sqlite3.Connection, chain: str, ms: int) -> int | None:
    """Smallest slot whose first print is at or after ``ms`` (slots are block numbers)."""
    row = conn.execute(
        "SELECT MIN(slot), MAX(slot) FROM swaps WHERE chain=? AND slot IS NOT NULL", (chain,)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    lo, hi = int(row[0]), int(row[1])
    while lo < hi:
        mid = (lo + hi) // 2
        hit = conn.execute(
            "SELECT ts_ms FROM swaps WHERE chain=? AND slot>=? ORDER BY slot LIMIT 1", (chain, mid)
        ).fetchone()
        if hit is None or hit[0] >= ms:
            hi = mid
        else:
            lo = mid + 1
    return lo


def chain_tape(conn: sqlite3.Connection, chain: str, since_ms: int, until_ms: int) -> ChainTape:
    """Scan a continuous source's prints across the chain once, recording every outage."""
    sources = CONTINUOUS_SOURCES.get(chain, frozenset())
    tape = ChainTape(chain=chain, sources=sources)
    if not sources:
        return tape
    slot = _slot_at_or_after(conn, chain, since_ms)
    if slot is None:
        return tape
    prev = None
    for ts_ms, source in conn.execute(
        "SELECT ts_ms, source FROM swaps WHERE chain=? AND slot>=? ORDER BY slot", (chain, slot)
    ):
        if source not in sources or ts_ms is None:
            continue
        ts_ms = int(ts_ms)
        if ts_ms > until_ms:
            continue
        if tape.first_ms is None:
            tape.first_ms = ts_ms
        if prev is not None and ts_ms - prev > DARK_GAP_MS:
            tape.dark.append((prev, ts_ms))
        prev = ts_ms if prev is None else max(prev, ts_ms)
    tape.last_ms = prev
    return tape


def _token_meta(conn: sqlite3.Connection, chain: str, token: str) -> tuple[int | None, int | None]:
    row = conn.execute(
        "SELECT created_ms, migrated_ms FROM tokens WHERE chain=? AND address=?", (chain, token)
    ).fetchone()
    if row is None:
        return None, None
    return _int(row[0]), _int(row[1])


def _token_tape_window(conn: sqlite3.Connection, chain: str, token: str) -> tuple[int, int] | None:
    if chain not in TOKEN_TAPE_CHAINS:
        return None
    try:
        row = conn.execute(
            "SELECT coverage, covered_from_ms, covered_to_ms FROM token_tape WHERE chain=? AND token=?",
            (chain, token),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row[0] not in ("complete", "partial") or row[1] is None or row[2] is None:
        return None
    return int(row[1]), int(row[2])


def _series(
    conn: sqlite3.Connection, chain: str, token: str, lo: int, hi: int,
) -> tuple[list[int], list[float], bool]:
    """Prints for one token in ``[lo, hi]``, from ONE swap source.

    Sources disagree on scale: MEASURED 2026-10-03 on sol, ``gmgn:smartmoney`` printed 5.93
    where ``pumpfun:trades`` printed 0.0000064 for the same token seconds apart (a decimals
    slip), and mixing them made ~95% of every population "reach 2x". So a token is priced
    from its continuous source when it has one, otherwise from whichever source holds the
    most prints for it.
    """
    rows = conn.execute(
        "SELECT ts_ms, price_usd, source FROM swaps WHERE chain=? AND token=? AND ts_ms BETWEEN ? AND ? "
        "AND price_usd IS NOT NULL ORDER BY ts_ms, id",
        (chain, token, lo, hi),
    ).fetchall()
    cont = CONTINUOUS_SOURCES.get(chain, frozenset())
    on_cont = [r for r in rows if r[2] in cont]
    if on_cont:
        use, continuous = on_cont, True
    else:
        counts = Counter(r[2] for r in rows)
        best = max(counts, key=lambda s: (counts[s], str(s))) if counts else None
        use, continuous = [r for r in rows if r[2] == best], False
    ts: list[int] = []
    px: list[float] = []
    for ts_ms, raw, _source in use:
        price = _price(raw)
        if price is not None and ts_ms is not None:
            ts.append(int(ts_ms))
            px.append(price)
    return ts, px, continuous


# --------------------------------------------------------------------------------------
# matching and scoring
# --------------------------------------------------------------------------------------


class BaselineIndex:
    """Scanner observations by (chain, age band), searchable by time."""

    def __init__(self, observations: Iterable[BaseObs]) -> None:
        cells: dict[tuple[str, str], list[BaseObs]] = defaultdict(list)
        for obs in observations:
            if obs.band is not None and obs.outcome.entry is not None:
                cells[(obs.chain, obs.band)].append(obs)
        self._cells = {}
        for key, items in cells.items():
            items.sort(key=lambda o: o.t)
            self._cells[key] = ([o.t for o in items], items)

    def match(self, chain: str, band: str | None, t: int, exclude: frozenset[str] | set[str]) -> list[BaseObs]:
        """Baseline tokens on ``chain`` in ``band`` seen within the match window of ``t``.

        Each token appears once (its sighting closest to ``t``); excluded tokens never do.
        """
        if band is None or (chain, band) not in self._cells:
            return []
        ts, items = self._cells[(chain, band)]
        i = bisect.bisect_left(ts, t - MATCH_WINDOW_MS)
        j = bisect.bisect_right(ts, t + MATCH_WINDOW_MS)
        best: dict[str, BaseObs] = {}
        for obs in items[i:j]:
            if obs.token in exclude:
                continue
            held = best.get(obs.token)
            if held is None or abs(obs.t - t) < abs(held.t - t):
                best[obs.token] = obs
        return list(best.values())


def summarize_pairs(pairs: Sequence[tuple[float, Sequence[float]]], *, seed: int = BOOTSTRAP_SEED) -> dict[str, Any]:
    """Pick vs its own cell. ``diff`` is exactly ``pick_mean - base_mean`` over these picks."""
    if not pairs:
        return {"n": 0}
    pick_vals = [pv for pv, _ in pairs]
    cell_means = [statistics.fmean(cell) for _, cell in pairs]
    diffs = [pv - cm for pv, cm in zip(pick_vals, cell_means, strict=True)]
    base_vals: list[float] = []
    base_w: list[float] = []
    for _, cell in pairs:
        base_vals.extend(cell)
        base_w.extend([1.0 / len(cell)] * len(cell))
    return {
        "n": len(pairs),
        "pick_mean": statistics.fmean(pick_vals),
        "pick_median": statistics.median(pick_vals),
        "base_mean": statistics.fmean(cell_means),
        "base_median": weighted_median(base_vals, base_w),
        "base_n": len(base_vals),
        "diff": statistics.fmean(diffs),
        "ci90": bootstrap_ci(diffs, seed=seed),
    }


def verdict(returns: Mapping[str, Mapping[str, Any]]) -> tuple[str, str]:
    """The pre-registered rule: the longest horizon with enough pairs decides."""
    for name in ("24h", "6h", "1h"):
        cell = returns.get(name) or {}
        if cell.get("n", 0) >= MIN_PAIRS_FOR_VERDICT and cell.get("ci90"):
            lo, hi = cell["ci90"]
            word = "beats baseline" if lo > 0 else "worse" if hi < 0 else "no different"
            return word, f"{name} mean return, n={cell['n']}"
    return "insufficient data", f"fewer than {MIN_PAIRS_FOR_VERDICT} matched picks at every horizon"


@dataclass
class _Scored:
    pick: Pick
    band: str | None
    outcome: Outcome | None


def score_source(
    scored: Sequence[_Scored], index: BaselineIndex, exclude: frozenset[str],
) -> dict[str, Any]:
    """Every metric for one (source, chain), paired against matched scanner cells."""
    coverage: Counter[str] = Counter()
    pairs: dict[str, list[tuple[float, list[float]]]] = {m: [] for m in METRICS}
    act_pairs: dict[str, list[tuple[float, list[float]]]] = {m: [] for m in METRICS}
    censored: dict[str, Counter[str]] = {name: Counter() for name, _ in HORIZONS}
    grad_pick: list[float] = []
    grad_base: list[float] = []
    prints_pick: list[int] = []
    prints_base: list[int] = []
    lags = []
    for item in scored:
        lags.append(item.pick.seen_ms - item.pick.published_ms)
        out = item.outcome
        if out is None or out.entry is None:
            coverage["no_price_at_pick"] += 1
            continue
        for name, _ in HORIZONS:
            if name in out.censored:
                censored[name][out.censored[name]] += 1
        if item.band is None:
            coverage["no_token_age"] += 1
            continue
        cell = index.match(item.pick.chain, item.band, item.pick.published_ms, exclude)
        if len(cell) < MIN_CELL:
            coverage["thin_baseline_cell"] += 1
            continue
        used = False
        band = activity_band(out.prints)
        same_activity = [o for o in cell if activity_band(o.outcome.prints) == band]
        for metric in METRICS:
            pv = out.metric(metric)
            if pv is None:
                continue
            values = [v for v in (o.outcome.metric(metric) for o in cell) if v is not None]
            if len(values) < MIN_CELL:
                continue
            pairs[metric].append((pv, values))
            used = True
            matched = [v for v in (o.outcome.metric(metric) for o in same_activity) if v is not None]
            if len(matched) >= MIN_CELL:
                act_pairs[metric].append((pv, matched))
        if used:
            coverage["scored"] += 1
            prints_pick.append(out.prints)
            prints_base.append(int(statistics.median(o.outcome.prints for o in cell)))
        else:
            coverage["no_forward_data"] += 1
        if out.graduated is not None:
            g = [o.outcome.graduated for o in cell if o.outcome.graduated is not None]
            if len(g) >= MIN_CELL:
                grad_pick.append(1.0 if out.graduated else 0.0)
                grad_base.append(sum(1.0 for x in g if x) / len(g))
    returns = {name: summarize_pairs(pairs[f"ret_{name}"]) for name, _ in HORIZONS}
    for name, _ in HORIZONS:
        returns[name]["censored"] = dict(censored[name])
    word, basis = verdict(returns)
    exit_at_grad = {name: summarize_pairs(pairs[f"gx_{name}"]) for name, _ in HORIZONS}
    act_returns = {name: summarize_pairs(act_pairs[f"ret_{name}"]) for name, _ in HORIZONS}
    act_exit = {name: summarize_pairs(act_pairs[f"gx_{name}"]) for name, _ in HORIZONS}
    return {
        "n_picks": len(scored),
        "coverage": dict(coverage),
        "ingest_lag_s_median": (statistics.median(lags) / 1000.0) if lags else None,
        "returns_pct": returns,
        "returns_pct_exit_at_graduation": exit_at_grad,
        "returns_pct_activity_matched": act_returns,
        "returns_pct_activity_matched_exit_at_graduation": act_exit,
        "reach_2x": summarize_pairs(pairs["reach_up"]),
        "reach_2x_activity_matched": summarize_pairs(act_pairs["reach_up"]),
        "fell_50_first": summarize_pairs(pairs["fell_first"]),
        "fell_50_first_activity_matched": summarize_pairs(act_pairs["fell_first"]),
        "max_multiple_24h": summarize_pairs(pairs["max_multiple"]),
        "graduated_24h": {
            "n": len(grad_pick),
            "pick_share": statistics.fmean(grad_pick) if grad_pick else None,
            "base_share": statistics.fmean(grad_base) if grad_base else None,
        },
        "prints_24h_median": {
            "pick": statistics.median(prints_pick) if prints_pick else None,
            "base": statistics.median(prints_base) if prints_base else None,
        },
        "verdict": word,
        "verdict_basis": basis,
        "verdict_exit_at_graduation": "{} ({})".format(*verdict(exit_at_grad)),
        "verdict_activity_matched": "{} ({})".format(*verdict(act_returns)),
        "verdict_activity_matched_exit_at_graduation": "{} ({})".format(*verdict(act_exit)),
    }


# --------------------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------------------


def _token_outcomes(
    conn: sqlite3.Connection, chain: str, token: str, times: Sequence[int], *, now_ms: int,
    tape: ChainTape | None, migrated_ms: int | None,
) -> dict[int, Outcome]:
    lo = min(times) - ENTRY_STALE_MS
    hi = max(times) + WINDOW_MS + ENTRY_GRACE_MS
    ts, px, continuous = _series(conn, chain, token, lo, hi)
    window = None if continuous else _token_tape_window(conn, chain, token)
    out: dict[int, Outcome] = {}
    for t in set(times):
        cont_until = None
        if continuous and tape is not None and ts:
            first_print = ts[0]
            until = tape.continuous_until(max(t, first_print))
            if until is not None and first_print <= t + ENTRY_GRACE_MS:
                cont_until = until
        elif window is not None and window[0] <= t:
            cont_until = window[1]
        out[t] = forward_outcome(ts, px, t, now_ms=now_ms, continuous_until=cont_until, migrated_ms=migrated_ms)
    return out


def kline_route() -> dict[str, Any]:
    """Whether the GMGN kline fallback is reachable through the provider wrapper."""
    try:
        from kaiba.providers import gmgn_cli

        allowed = ("market", "kline") in gmgn_cli._ALLOWED
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return {"available": False, "reason": f"gmgn_cli import failed: {type(exc).__name__}", "calls": 0}
    if not allowed:
        return {"available": False, "calls": 0,
                "reason": "('market', 'kline') is not in kaiba.providers.gmgn_cli._ALLOWED"}
    return {"available": True, "calls": 0,
            "reason": "allowed by the wrapper but not wired here; no response shape has been verified"}


def unscored_sources(conn: sqlite3.Connection, since_ms: int) -> dict[str, Any]:
    """Alpha feeds that name a domain or ticker, not a token we can price."""
    out: dict[str, Any] = {}
    try:
        rows = conn.execute(
            "SELECT source, kind, COUNT(*) FROM alpha_signals WHERE first_seen_ms >= ? GROUP BY source, kind",
            (since_ms,),
        ).fetchall()
        out["alpha_signals"] = {f"{s}:{k}": n for s, k, n in rows}
    except sqlite3.OperationalError:
        out["alpha_signals"] = None
    out["alpha.listing_events"] = sum(
        1 for _ in _events_of_kind(conn, "alpha.listing", since_ms, 2**62, "id")
    )
    out["reason"] = "subjects are domains, governance spaces or CEX tickers; no on-chain token to price"
    return out


def run(
    conn: sqlite3.Connection,
    *,
    since_ms: int,
    now_ms: int | None = None,
    chains: Sequence[str] | None = None,
    hermes_dir: str | Path | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    """Score every source. Read-only: only SELECTs are issued."""
    now = data_now_ms(conn) if now_ms is None else now_ms
    picks, skipped = load_picks(conn, since_ms, now, chains)
    hermes_stats = None
    if hermes_dir is not None:
        hermes_picks, hermes_stats = load_hermes_picks(conn, hermes_dir, since_ms, now, chains)
        picks.extend(hermes_picks)
    scans = load_scans(conn, since_ms - MATCH_WINDOW_MS, now, chains)

    needed: dict[tuple[str, str], set[int]] = defaultdict(set)
    for pick in picks:
        needed[(pick.chain, pick.token)].update((pick.published_ms, pick.seen_ms))
    for chain, token, t in scans:
        needed[(chain, token)].add(t)

    tapes = {
        chain: chain_tape(conn, chain, since_ms - MATCH_WINDOW_MS - ENTRY_STALE_MS, now)
        for chain in {c for c, _ in needed} if chain in CONTINUOUS_SOURCES
    }

    meta: dict[tuple[str, str], tuple[int | None, int | None]] = {}
    outcomes: dict[tuple[str, str, int], Outcome] = {}
    for done, ((chain, token), times) in enumerate(sorted(needed.items())):
        created, migrated = _token_meta(conn, chain, token)
        meta[(chain, token)] = (created, migrated)
        for t, outcome in _token_outcomes(
            conn, chain, token, sorted(times), now_ms=now, tape=tapes.get(chain), migrated_ms=migrated
        ).items():
            outcomes[(chain, token, t)] = outcome
        if progress is not None and done % 5000 == 0:
            progress(f"priced {done}/{len(needed)} tokens")

    def band_at(chain: str, token: str, t: int, fallback_created: int | None) -> str | None:
        created = meta.get((chain, token), (None, None))[0] or fallback_created
        return age_band(None if created is None else t - created)

    index = BaselineIndex(
        BaseObs(chain, token, t, band_at(chain, token, t, None), outcomes[(chain, token, t)])
        for chain, token, t in scans
    )
    by_source: dict[tuple[str, str], list[_Scored]] = defaultdict(list)
    by_sighting: dict[tuple[str, str], list[_Scored]] = defaultdict(list)
    for pick in picks:
        by_source[(pick.source, pick.chain)].append(
            _Scored(pick, band_at(pick.chain, pick.token, pick.published_ms, pick.created_ms),
                    outcomes.get((pick.chain, pick.token, pick.published_ms)))
        )
        seen = Pick(pick.source, pick.chain, pick.token, pick.seen_ms, pick.seen_ms, pick.created_ms)
        by_sighting[(pick.source, pick.chain)].append(
            _Scored(seen, band_at(pick.chain, pick.token, pick.seen_ms, pick.created_ms),
                    outcomes.get((pick.chain, pick.token, pick.seen_ms)))
        )
    picked_tokens: dict[tuple[str, str], frozenset[str]] = {
        key: frozenset(item.pick.token for item in items) for key, items in by_source.items()
    }
    rows = []
    for (source, chain), items in sorted(by_source.items()):
        row = {"source": source, "chain": chain}
        row.update(score_source(items, index, picked_tokens[(source, chain)]))
        if any(item.pick.seen_ms != item.pick.published_ms for item in items):
            # What Kaiba could have acted on: the same picks, measured from our own sighting.
            seen = score_source(by_sighting[(source, chain)], index, picked_tokens[(source, chain)])
            row["from_our_sighting"] = {
                key: seen[key] for key in ("returns_pct", "returns_pct_exit_at_graduation", "reach_2x",
                                           "fell_50_first", "verdict", "verdict_basis",
                                           "verdict_exit_at_graduation")
            }
        rows.append(row)
    if hermes_stats is not None and not any(r["source"] == HERMES_SOURCE for r in rows):
        rows.append({"source": HERMES_SOURCE, "chain": "-", "n_picks": 0, "verdict": "insufficient data",
                     "verdict_basis": "the job named no token in the window"})
    return {
        "meta": {
            "since_ms": since_ms, "now_ms": now, "chains": list(chains) if chains else "all",
            "picks": len(picks), "baseline_observations": len(scans), "tokens_priced": len(needed),
            "match_window_min": MATCH_WINDOW_MS // MINUTE_MS, "min_cell": MIN_CELL,
            "bootstrap_rounds": BOOTSTRAP_ROUNDS, "ci_level": CI_LEVEL,
        },
        "sources": rows,
        "not_picks": dict(skipped),
        "hermes": hermes_stats,
        "unscored": unscored_sources(conn, since_ms),
        "chain_tapes": {
            c: {"first_ms": t.first_ms, "last_ms": t.last_ms,
                "outages": [(a, b, round((b - a) / MINUTE_MS, 1)) for a, b in t.dark]}
            for c, t in tapes.items()
        },
        "gmgn_kline": kline_route(),
    }


# --------------------------------------------------------------------------------------
# presentation
# --------------------------------------------------------------------------------------


def _f(value: Any, spec: str = "+.1f") -> str:
    return "-" if value is None else format(value, spec)


def _ci(cell: Mapping[str, Any], spec: str = "+.1f") -> str:
    ci = cell.get("ci90")
    return "-" if not ci else f"[{format(ci[0], spec)}, {format(ci[1], spec)}]"


def render(result: Mapping[str, Any]) -> str:
    meta = result["meta"]
    lines = [
        f"alpha sources: {meta['picks']} picks, {meta['baseline_observations']} scanner observations, "
        f"{meta['tokens_priced']} tokens priced; match +/-{meta['match_window_min']} min, same chain and "
        f"age band, cell >= {meta['min_cell']}; {int(meta['ci_level'] * 100)}% bootstrap over picks",
    ]
    for row in result["sources"]:
        lines.append("")
        lines.append(f"{row['source']} [{row['chain']}]  n_picks={row.get('n_picks', 0)}  "
                     f"verdict: {row.get('verdict')} ({row.get('verdict_basis')})")
        if not row.get("returns_pct"):
            continue
        lines.append(f"  coverage {row['coverage']}  median ingest lag {_f(row.get('ingest_lag_s_median'), '.0f')} s")
        lines.append("  horizon   n   pick med  pick mean | base med  base mean |  diff mean  90% CI          censored")
        for name, _ in HORIZONS:
            c = row["returns_pct"][name]
            lines.append(
                f"  {name:>4} {c.get('n', 0):5d}  {_f(c.get('pick_median')):>8}  {_f(c.get('pick_mean')):>9} | "
                f"{_f(c.get('base_median')):>8}  {_f(c.get('base_mean')):>9} | {_f(c.get('diff')):>9}  "
                f"{_ci(c):<16} {c.get('censored', {})}"
            )
        for label, block, word in (
            ("exit at graduation", row["returns_pct_exit_at_graduation"], row["verdict_exit_at_graduation"]),
            ("activity-matched", row["returns_pct_activity_matched"], row["verdict_activity_matched"]),
            ("act-matched+grad exit", row["returns_pct_activity_matched_exit_at_graduation"],
             row["verdict_activity_matched_exit_at_graduation"]),
            ("sighting+grad exit", (row.get("from_our_sighting") or {}).get("returns_pct_exit_at_graduation"),
             (row.get("from_our_sighting") or {}).get("verdict_exit_at_graduation")),
            ("from our sighting", (row.get("from_our_sighting") or {}).get("returns_pct"),
             "{} ({})".format(row["from_our_sighting"]["verdict"], row["from_our_sighting"]["verdict_basis"])
             if row.get("from_our_sighting") else None),
        ):
            if not block:
                continue
            cells = "  ".join(
                f"{name} n={block[name].get('n', 0)} {_f(block[name].get('diff'))} {_ci(block[name])}"
                for name, _ in HORIZONS
            )
            lines.append(f"  {label:<20} diff {cells}  -> {word}")
        for label, key in (("reach 2x", "reach_2x"), ("reach 2x, activity-matched", "reach_2x_activity_matched"),
                           ("fell 50% first", "fell_50_first"),
                           ("fell 50% first, act-matched", "fell_50_first_activity_matched")):
            c = row[key]
            lines.append(
                f"  {label:<27} n={c.get('n', 0):5d}  pick {_f(c.get('pick_mean'), '.1%')}  "
                f"base {_f(c.get('base_mean'), '.1%')}  diff {_f(c.get('diff'), '+.1%')} {_ci(c, '+.1%')}"
            )
        mm = row["max_multiple_24h"]
        lines.append(f"  max multiple 24h median     n={mm.get('n', 0):5d}  pick {_f(mm.get('pick_median'), '.2f')}x  "
                     f"base {_f(mm.get('base_median'), '.2f')}x")
        g, p = row["graduated_24h"], row["prints_24h_median"]
        lines.append(f"  graduated in 24h            n={g['n']:5d}  pick {_f(g['pick_share'], '.1%')}  "
                     f"base {_f(g['base_share'], '.1%')}   prints/24h median pick {p['pick']} base {p['base']}")
    lines.append("")
    lines.append(f"not picks: {result['not_picks']}")
    if result.get("hermes") is not None:
        lines.append(f"hermes alpha scan: {result['hermes']}")
    lines.append(f"unscored: {result['unscored']}")
    lines.append(f"chain tapes: {result['chain_tapes']}")
    lines.append(f"gmgn kline: {result['gmgn_kline']}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="path to kaiba.db (opened read-only)")
    parser.add_argument("--since-days", type=float, default=7.0)
    parser.add_argument("--chain", action="append", help="repeatable; default all")
    parser.add_argument("--hermes-dir", help="Hermes alpha-scan cron output directory (read only)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    conn = connect_ro(args.db)
    try:
        now = data_now_ms(conn)
        since = int(now - args.since_days * DAY_MS)
        result = run(conn, since_ms=since, now_ms=now, chains=args.chain, hermes_dir=args.hermes_dir,
                     progress=lambda msg: print(msg, file=sys.stderr, flush=True))
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, indent=1, default=str))
    else:
        print(render(result))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
