"""Tier 1: the expensive pass that turns a promoted launch into evaluated lanes.

This module is the missing joint. Before it existed, ``kaiba/ingest/pumpportal.py`` wrote
tokens and screened them (tier 0), ``kaiba/execution/lanes.py`` held eight evaluators and
``kaiba/execution/engine.py`` turned ``signals`` rows into decisions — and **nothing ever
constructed a** :class:`~kaiba.execution.lanes.LaneContext`. There was not one call to
``evaluate_and_record`` anywhere outside ``lanes.py``, so no lane had ever looked at the
market and every row in ``signals`` came from a manual agent-intent test. The strategy
layer was disconnected from the feed at exactly one point, and this is that point.

The pass, per token:

1. **Dossier** — ``kaiba.intelligence.dyor.scan_token``, reused from ``token_dossiers``
   when ``engine.load_dossier`` says it is still fresh. Measured at 7.25 s warm and 8.7 s
   cold, it is ~95% of the cost of a tier-1 pass and the entire reason tier 0 exists.
2. **Curve** — pump.fun's free ``coins/{mint}`` route, which carries the reserves.
3. **Trade tape** — the mint's own trades, collected and its coverage recorded. See below;
   this is the only moment in the system where a tape can reliably be had at all.
4. **Recent buys** — the ``swaps`` table.
5. **Token meta** — the ``tokens`` table. **Caller** — ``caller_calls``.
6. ``lanes.evaluate_and_record(ctx, conn)``, then the engine takes it from ``signals``.

## The tape is captured here because here is the only place it exists

The pump.fun trade route serves a mint's trade history only while that mint is trading.
Measured on 2026-09-20 against ``frontend-api-v3.pump.fun/trades/{chain_id}/{mint}``:

* bonding-curve mints idle **0-12 minutes**: HTTP 200, 11 of 11;
* bonding-curve mints idle **48-170 minutes**: HTTP 503 with
  ``degraded_lanes: ["trade_api.list_trades"]``, 37 of 37;
* graduated mints: 200 regardless of idleness.

It is per mint and persistent, not transient load — one mint returned 503 on 6 of 6
controlled retries in the same minute another returned 200 on 4 of 4 — and it is specific
to the trade lane, because ``/coins/{mint}`` kept answering for the identical mints. A
backfill over the 435 tokens already in the database attempted 78 and recovered **2**.

The consequence is structural and worth stating plainly: **tape coverage is a
going-forward property and cannot be acquired later.** A launch that passes through tier 1
without its tape captured is a permanent hole in bundle share, curve velocity and
confluence for that token, forever. That is why :func:`_observe_flow` both collects the
trades and writes a coverage verdict to ``token_tape``, and why the verdict is counted in
:class:`ScanStats` — a pass that captured nothing is a token lost, and it should be as
visible as a pass that fired no lane.

Capturing costs no extra request: the collection was already happening inside
``token_flow.observe``, and recording the verdict is a handful of local SQLite statements.
Measured on 2026-09-20: the capture itself is **0.33 ms mean / 0.50 ms p95** over 40 calls
against the live database, and an interleaved A/B of 20 real tier-1 passes put the pass at
13.35 s with capture and 13.18 s without — a +171 ms difference with a standard error of
351 ms (95% CI -517 to +859 ms), which is provider variance and not the change: the
dossier alone ranged 7.1-9.7 s across the same 20 passes. The capture is roughly 0.002% of
a pass.

## Throughput, which is the design constraint and not a footnote

pump.fun launches **14 tokens a minute**. Measured on this machine on 2026-09-20 over a
364-second live run against the real feed: **11.7 seconds per pass, of which 9.7 is the
dossier**, sustaining **5.1 tokens a minute — 307 an hour, 36% of the launch rate**. That
is with no GMGN credential, so the dossier runs on GoPlus, RugCheck and dedup only; with
GMGN Plus it gets slower, not faster, because there is one more provider to wait on.

We cannot scan the market; we can only scan a *selection* of it. That is why the queue in
``triage.py`` is a ranking device rather than a buffer, and why this module never grows a
backlog of its own: it pops the best available work, scans it, drops what it could not
reach, and pops again. A backlog would only guarantee that the head of it is stale by the
time a worker gets there.

:func:`measure_rate` reports what was actually achieved rather than what was hoped for.

## Rules this module holds to, each of which cost something to learn

* **A lane that cannot be evaluated produces no signal.** If the curve fetch fails, the
  context carries ``curve=None`` and ``curve_velocity`` stays silent. Nothing here
  substitutes a zero, a default or a "reasonable" guess to let a lane run; the lanes
  already fail closed on unknown evidence and papering over the gap would convert
  "we don't know" into "it's fine" (``docs/CONTRACT.md`` rule 2).
* **A derived number is only supplied when its derivation is sound.** ``sol_per_swap`` is
  the published predictor, but dividing curve SOL by *our* swap count is nonsense unless
  we have been watching the token since launch — three rows out of four hundred would
  overstate per-swap volume by two orders of magnitude and fire the lane on garbage. So
  the swap count is supplied only when our coverage provably starts at launch, and the
  weaker per-minute basis is used otherwise, labelled as such.
* **One bad token never stops the loop.** Every pass is wrapped; the failure is recorded
  as a :class:`ScanResult` and on the event bus, and the worker takes the next item.
* **Every outbound call goes through** ``kaiba/providers/_http.py`` **with**
  ``wait_for_slot_s`` **set**, because a tier-1 pass makes many provider calls per logical
  operation and the non-waiting default silently drops everything after the first.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Any

from kaiba.core.db import connect, fetch_all, fetch_one, get_conn, jdump, jload
from kaiba.core.events import emit
from kaiba.core.limiter import Priority
from kaiba.core.schemas import (
    Chain,
    EventKind,
    Grade,
    Lane,
    Receipt,
    Signal,
    Token,
    is_quote_asset,
    now_ms,
)
from kaiba.execution import lanes as lanes_mod
from kaiba.execution import triage as triage_mod
from kaiba.execution.engine import DOSSIER_MAX_AGE_S, load_dossier
from kaiba.execution.lanes import LaneContext
from kaiba.providers._http import get_json

log = logging.getLogger(__name__)

PARAMS_VERSION = "scanner-v1"

#: Event-bus kind, declared in ``schemas.EventKind`` since 2026-09-22 (it was a raw string
#: no ``Event``-typed reader could load). Kept as ``.value`` so comparisons to rows stay str.
EVENT_SCANNED = EventKind.SCAN_TIER1.value
EVENT_SCAN_FAILED = EventKind.SCAN_FAILED.value

PROVIDER = "pumpfun"
CURVE_URL = "https://frontend-api-v3.pump.fun/coins/{mint}"
CURVE_ENDPOINT = "coins.detail"
USER_AGENT = "Mozilla/5.0 (compatible; kaiba/0.1; +https://pump.fun)"

LAMPORTS_PER_SOL = Decimal(1_000_000_000)

# --------------------------------------------------------------------------------------
# pump.fun bonding-curve constants — and the one that is not a constant any more
#
# Measured on mainnet on 2026-09-20 against a live sample, then corrected by a live tier-1
# run that rejected 3 of 30 launches for violating the "constants" below:
#
# * ``virtual_token_reserves - real_token_reserves`` was **exactly** 279,900,000,000,000 on
#   every SOL-quoted coin observed, with no exceptions. This is the real invariant: the
#   tokens held back for the post-graduation pool. Everything else is derived from it.
# * **The starting point is per launch.** The classic curve starts at 30 virtual SOL with
#   793,100,000,000,000 real tokens and graduates at 85 SOL, and plenty of coins still do.
#   Others in the same minute started at 0.107 SOL and 1,066,700,000,000,000 real tokens
#   (graduating at 0.41 SOL), or at 40.6 SOL. Creators now choose a starting market cap.
#   Hard-coding 793.1e12 as the progress denominator rejects the high-cap launches
#   outright and, worse, silently *overstates* progress on the low-cap ones — pushing
#   tokens into ``curve-velocity``'s 30-70% band that are nowhere near it.
#
# So both the initial SOL and the initial token reserves are derived per token from the
# constant-product invariant (see :func:`curve_from_payload`). The derivation reproduces
# exactly 30 SOL, exactly 793.1e12 and exactly 85 SOL on a classic curve, which is what
# makes it trustworthy on a curve that is not classic. ``docs/PLAN-TO-RUNNING-AGENT.md``
# §3.5 asked for exactly this and understated how much it mattered.
# --------------------------------------------------------------------------------------

#: The only genuine constant: ``virtual - real`` tokens, held back for the pool.
RESERVED_TOKEN_ATOMS = 279_900_000_000_000

#: The classic launch's starting reserves. Reference values for the tests and for reading
#: a derived number against — never a divisor. See the note above.
CLASSIC_VIRTUAL_TOKEN_ATOMS = 1_073_000_000_000_000
CLASSIC_REAL_TOKEN_ATOMS = CLASSIC_VIRTUAL_TOKEN_ATOMS - RESERVED_TOKEN_ATOMS  # 793.1e12
CLASSIC_VIRTUAL_SOL_LAMPORTS = 30_000_000_000
CLASSIC_GRADUATION_SOL = Decimal(85)

#: pump.fun now runs curves quoted in things that are not SOL (USDC and other mints appear
#: in the live sample). On those, every ``*_sol_*`` field is denominated in the quote token
#: and ``quote_decimals`` is not 9, so a SOL-denominated threshold like curve-velocity's
#: 0.18 SOL per swap would be compared against a different unit. We refuse to build a curve
#: for them rather than silently mixing units.
SOL_QUOTE_MINTS: frozenset[str] = frozenset(
    {
        "11111111111111111111111111111111",
        "So11111111111111111111111111111111111111112",
    }
)


# --------------------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScanConfig:
    """Tier-1 knobs. Every number is annotated with where it came from."""

    #: Reuse a stored dossier newer than this. **Half** ``engine.DOSSIER_MAX_AGE_S``, not
    #: all of it: the engine refuses an entry whose dossier is over 300 s old at *decide*
    #: time, which is strictly later than scan time. Reusing a 299-second-old dossier
    #: produces a signal that is already dead on arrival, and the live run on 2026-09-20
    #: did exactly that once ("dossier is 483s old (budget 300s)"). Half the budget leaves
    #: the engine 150 s of headroom to catch up, at the cost of one extra scan.
    dossier_max_age_s: int = DOSSIER_MAX_AGE_S // 2

    #: Scan tokens that smart-money wallets are buying right now, at any token age.
    #: Without this the scanner is a pure new-launch sampler and ``sm_trenches`` -- which
    #: needs three smart wallets to have already accumulated -- can never see a candidate
    #: that satisfies it. MEASURED 2026-09-22: 97% of scanned tokens were 0-3 minutes old
    #: and 6 of 7 qualifying smart-flow tokens were never evaluated.
    smart_flow: bool = True
    #: The look-back for "is smart money buying this now". Must match ``sm_trenches``'s
    #: own ``window_s`` so the feeder and the lane agree on what "now" means.
    #:
    #: 1800 s, not 300. MEASURED 2026-09-24 on the live box: at 300 s there were ZERO
    #: sol tokens with three or more tagged smart buyers -- the feeder's only job is
    #: to find sm-trenches candidates and its window admitted none. At 1800 s, 13.
    #: Over six hours 221 tokens across the three chains reached the bar while the
    #: lane made 26 decisions. Confluence ACCUMULATES over ~30 minutes; a five-minute
    #: slice measures a burst, which is a different thing and a rarer one.
    #:
    #: This costs no extra provider call and does not widen the tick: ``batch`` and
    #: the scan cadence are unchanged, so only the COMPOSITION of each batch moves,
    #: away from new launches and toward the tokens the live lane can actually act
    #: on. ``recent_buys_window_s`` is already 1800, so the rows are loaded anyway.
    smart_flow_window_s: int = 1800
    #: The most of one batch smart flow may take, as a fraction. The remainder is left to
    #: the launch sources, so arming this cannot starve ``pons-robinhood`` (robinhood) or
    #: the migration window.
    #:
    #: RAISED 0.5 -> 0.8 on 2026-09-22, because the split was paying for lanes that cannot
    #: trade. Smart flow is the ONLY source that can produce an ``sm-trenches`` candidate,
    #: and ``sm-trenches`` was the only live lane: every other lane -- migration-fade,
    #: pons-robinhood, confluence-5, trusted-copy, kol-fade, manual -- was in SHADOW.
    #: MEASURED over two hours: 68 of 186 signals (37%) went to migration-fade, which
    #: could not act on any of them, while half of every batch was reserved for the
    #: sources that feed it.
    #:
    #: This buys frequency at ZERO provider cost, which is the point. Raising scanner
    #: CONCURRENCY the same day lifted dossier throughput 1.8x and simultaneously drained
    #: the ``robinhood-rpc`` bucket to -19,699, breaking the Pons curve read and leaving
    #: two live positions with no evaluable stop. Reallocating a fixed batch cannot do
    #: that: the same number of tokens is scanned, against the same providers.
    #:
    #: The remaining 0.2 is deliberate and not roundable to zero. The shadow lanes are how
    #: the learning studies get their sample, and a lane starved of candidates stops
    #: producing the evidence that would promote it.
    smart_flow_share: float = 0.8
    #: Distinct smart buyers a token needs before it is worth a pass. ``None`` reads
    #: ``sm_trenches``'s ``min_smart_degen`` so the feeder cannot drift from the lane.
    smart_flow_min_wallets: int | None = None
    #: Rows the smart-flow query may consider. Bounded like every other query here.
    smart_flow_limit: int = 200
    #: How long the smart-wallet SET is reused before it is read again. Reading it is two
    #: full table scans (775k wallets, 399k scores on the box) and it only changes when the
    #: cohort and grading jobs run -- every half hour at best -- but it was re-read on every
    #: batch, about once a second. See ``_smart_flow_work``.
    smart_set_ttl_s: float = 300.0

    #: Scan tokens that >= ``min_entities`` PROVEN wallets (``kaiba.learning.proven``) are
    #: buying, whenever ``confluence-5`` counts that cohort (``wallet_source: proven`` in
    #: risk.yaml). Off in effect otherwise: the feeder reads the lane's own params and
    #: returns nothing unless the lane asks for proven wallets.
    #:
    #: WHY a feeder of its own, MEASURED 2026-10-02 on the box: of the 19 robinhood tokens
    #: where five B-graded wallets net-bought within 120 s over seven days, 3 were scanned
    #: within +-120 s and NONE within 30 s of qualifying. The lane only sees what a work
    #: source offers, and no source was offering the tokens its own wallets were buying.
    proven_flow: bool = True
    #: The most of one batch proven flow may take. A few qualifying tokens a day per chain
    #: are expected, so this almost never binds; it exists so a misconfigured threshold
    #: cannot crowd out the live lane's feeder.
    proven_flow_max: int = 4
    #: The feeder's query runs at most this often. One short range seek per proven wallet
    #: per chain, so it is cheap; the floor stops it running on every one-second batch.
    proven_flow_min_interval_s: float = 5.0
    #: Rows the proven-flow query may return per chain.
    proven_flow_limit: int = 50

    #: How far back ``recent_buys`` reaches. 1800 s is the widest window any lane asks for
    #: (``pons-robinhood``'s ``max_age_s``); the narrower lanes filter inside it themselves.
    recent_buys_window_s: int = 1800
    #: Hard cap on rows loaded per token, so one heavily-traded mint cannot cost the pass
    #: its latency budget. INVENTED; 4000 rows is far above anything the swaps table holds.
    recent_buys_limit: int = 4000

    #: A swap count is only usable as the ``sol_per_swap`` denominator when our coverage of
    #: the token starts at its launch. INVENTED grace: one minute between the token's
    #: creation and our first recorded swap still counts as "we were there".
    swap_coverage_grace_s: int = 60
    #: Never derive a per-minute velocity from less than one minute of life. MEASURED
    #: requirement, not a preference: a 5-second-old token with 0.2 SOL in the curve
    #: extrapolates to 2.4 SOL/min, which is an artefact of the denominator.
    min_age_for_per_minute_s: int = 60

    curve_ttl_s: float = 5.0
    #: Required by ``docs/CONTRACT.md``: a tier-1 pass makes many provider calls, so the
    #: non-waiting default would silently drop this one whenever the dossier's own calls
    #: had just spent the limiter's capacity.
    curve_wait_for_slot_s: float = 15.0
    curve_timeout_s: float = 12.0
    curve_retries: int = 2

    #: Parallel tier-1 workers. Default 1 — see the module docstring; concurrency is
    #: measured before it is trusted, and the providers behind the dossier have
    #: ``max_inflight`` of 1-4 each, so the second worker mostly waits on the limiter.
    workers: int = 1
    #: Items drained per :func:`run_once`.
    batch: int = 8
    #: Sleep when the queue is empty. The launch feed produces 14/min, so an idle second
    #: costs nothing and a busy-wait costs a core.
    idle_sleep_s: float = 1.0
    #: Persist a backpressure sample this often. Per-token would be 7 writes an hour of
    #: nearly identical rows.
    backpressure_every_s: float = 60.0

    #: Fall back to ``triage_decisions`` when the in-process queue is empty.
    #:
    #: This is not an optimisation, it is what makes tier 1 a *service*.
    #: ``triage.get_queue()`` is a process-wide singleton living in memory, so the queue
    #: that ``kaiba ingest run`` fills is not the queue that ``kaiba scan run`` drains —
    #: as separate systemd units they would never exchange a single token. The durable
    #: record of tier 0 is the table, so that is what a separate process reads.
    db_queue: bool = True
    #: Ignore a tier-0 verdict older than this. DERIVED from the lane windows: the widest
    #: any lane looks back is ``pons-robinhood``'s 1800 s, and a launch we screened five
    #: minutes ago and never reached is not the opportunity it was.
    db_queue_max_age_s: int = 300
    #: Candidates considered per cycle before ranking. We scan the best few and **drop**
    #: the rest, because a 60:1 arrival ratio means a backlog is a lie about capacity.
    db_queue_window: int = 200
    #: Admit ``defer`` verdicts when there is spare capacity, matching ``TriageQueue``'s
    #: default: idle tier-1 capacity is better spent on the best of a boring minute.
    db_queue_admit_defers: bool = True

    #: Do not re-offer a token tier 1 already scanned this recently. Defaults to the
    #: dossier freshness budget, inside which a re-scan reuses the same dossier, lands in
    #: the same signal bucket and therefore cannot produce anything new — it would only
    #: occupy a slot. Explicit ``--token`` requests bypass this; the operator asked.
    rescan_cooldown_s: int = DOSSIER_MAX_AGE_S

    #: Also scan tokens that just migrated. ``migration-fade`` is the only lane whose
    #: window (180 s post-migration) is both reachable at our latency and independent of
    #: the wallet grades we do not have yet, and tier 0 only ever enqueues *launches*, so
    #: without this the one lane that can fire is never offered a candidate.
    include_migrations: bool = True
    #: Ignore a migration older than this when catching up, rather than scanning a queue
    #: of stale ones after a restart. DERIVED from ``migration-fade``'s own
    #: ``sell_within_s`` default of 180 s: past it the lane cannot fire anyway.
    migration_max_age_s: int = 180


DEFAULT_CONFIG = ScanConfig()

MIGRATION_WATERMARK_KEY = "scanner.migration_watermark"
TRIAGE_WATERMARK_KEY = "scanner.triage_watermark"


# --------------------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScanResult:
    """One tier-1 pass, whatever happened. A failure is a result, not an exception."""

    chain: Chain
    token: str
    source: str = "queue"  # queue | token | migration
    ok: bool = True
    elapsed_ms: int = 0
    dossier_ms: int = 0
    dossier_grade: Grade | None = None
    dossier_reused: bool = False
    dossier_blockers: tuple[str, ...] = ()
    dossier_unknowns: int = 0
    curve_ok: bool = False
    curve_note: str | None = None
    velocity_basis: str | None = None
    #: What this scan's trade collection established about the tape: ``complete``,
    #: ``partial``, ``unavailable``, or ``None`` when no collection was attempted.
    #: ``complete`` is the only value that means we hold every trade back to the launch —
    #: see ``kaiba.ingest.tape``. It is on the result because the hot window makes this a
    #: number worth watching per pass: a scan that captured nothing is a token lost.
    tape_coverage: str | None = None
    recent_buys: int = 0
    signals: tuple[Signal, ...] = ()
    error: str | None = None
    ts_ms: int = 0

    @property
    def lanes_fired(self) -> tuple[str, ...]:
        return tuple(s.lane.value for s in self.signals)

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain": self.chain.value,
            "token": self.token,
            "source": self.source,
            "ok": self.ok,
            "elapsed_ms": self.elapsed_ms,
            "dossier_ms": self.dossier_ms,
            "dossier_grade": self.dossier_grade.value if self.dossier_grade else None,
            "dossier_reused": self.dossier_reused,
            "dossier_blockers": list(self.dossier_blockers),
            "dossier_unknowns": self.dossier_unknowns,
            "curve_ok": self.curve_ok,
            "curve_note": self.curve_note,
            "velocity_basis": self.velocity_basis,
            "tape_coverage": self.tape_coverage,
            "recent_buys": self.recent_buys,
            "signals": [
                {"lane": s.lane.value, "strength": s.strength, "signal_id": s.signal_id}
                for s in self.signals
            ],
            "lanes_fired": list(self.lanes_fired),
            "error": self.error,
            "ts_ms": self.ts_ms,
        }


class ScanStats:
    """Running totals for the live report. Thread-safe; workers share one."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = time.monotonic()
        self.scanned = 0
        self.failed = 0
        self.signals = 0
        self.dossiers_built = 0
        self.dossiers_reused = 0
        self.curve_ok = 0
        self.curve_missing = 0
        self.total_scan_s = 0.0
        self.total_dossier_s = 0.0
        self.per_lane: dict[str, int] = {}
        self.grades: dict[str, int] = {}
        self.blockers: dict[str, int] = {}
        self.errors: dict[str, int] = {}
        self.velocity_basis: dict[str, int] = {}
        #: Coverage verdicts per pass. `tape_complete` is the number that decides
        #: whether bundle share, curve velocity and confluence have anything to read.
        self.tape_coverage: dict[str, int] = {}

    def record(self, result: ScanResult) -> None:
        with self._lock:
            if result.ok:
                self.scanned += 1
            else:
                self.failed += 1
                key = (result.error or "unknown").split(":", 1)[0]
                self.errors[key] = self.errors.get(key, 0) + 1
            self.total_scan_s += result.elapsed_ms / 1000.0
            self.total_dossier_s += result.dossier_ms / 1000.0
            if result.dossier_reused:
                self.dossiers_reused += 1
            elif result.dossier_ms:
                self.dossiers_built += 1
            if result.curve_ok:
                self.curve_ok += 1
            elif result.ok:
                self.curve_missing += 1
            if result.dossier_grade is not None:
                g = result.dossier_grade.value
                self.grades[g] = self.grades.get(g, 0) + 1
            for blocker in result.dossier_blockers:
                self.blockers[blocker] = self.blockers.get(blocker, 0) + 1
            if result.velocity_basis:
                b = result.velocity_basis
                self.velocity_basis[b] = self.velocity_basis.get(b, 0) + 1
            key = result.tape_coverage or ("none" if result.ok else "not_attempted")
            self.tape_coverage[key] = self.tape_coverage.get(key, 0) + 1
            for signal in result.signals:
                self.signals += 1
                lane = signal.lane.value
                self.per_lane[lane] = self.per_lane.get(lane, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            wall_s = max(time.monotonic() - self.started_at, 1e-6)
            passes = self.scanned + self.failed
            return {
                "wall_s": round(wall_s, 1),
                "scanned": self.scanned,
                "failed": self.failed,
                "signals": self.signals,
                "lanes_fired": dict(sorted(self.per_lane.items())),
                "dossiers_built": self.dossiers_built,
                "dossiers_reused": self.dossiers_reused,
                "curve_ok": self.curve_ok,
                "curve_missing": self.curve_missing,
                "grades": dict(sorted(self.grades.items())),
                "blockers": dict(sorted(self.blockers.items())),
                "velocity_basis": dict(sorted(self.velocity_basis.items())),
                "tape_coverage": dict(sorted(self.tape_coverage.items())),
                "tape_complete": self.tape_coverage.get("complete", 0),
                "errors": dict(sorted(self.errors.items())),
                "mean_scan_s": round(self.total_scan_s / passes, 3) if passes else None,
                "mean_dossier_s": round(self.total_dossier_s / passes, 3) if passes else None,
                "tokens_per_min": round(passes * 60.0 / wall_s, 2),
                "tokens_per_hour": round(passes * 3600.0 / wall_s, 1),
            }


def measure_rate(stats: ScanStats, launches_per_min: float = 14.0) -> dict[str, Any]:
    """The sustainable scan rate, next to the rate the market produces.

    Reported as a *fraction of the market seen* rather than as a throughput number on its
    own, because 7 tokens an hour sounds like a capacity problem and "0.9% of launches"
    correctly sounds like a selection problem.
    """
    snap = stats.as_dict()
    per_min = float(snap["tokens_per_min"])
    return {
        **snap,
        "launches_per_min": launches_per_min,
        "market_coverage_pct": round(100.0 * per_min / launches_per_min, 2) if launches_per_min else None,
        "backlog_ratio": round(launches_per_min / per_min, 1) if per_min > 0 else None,
    }


# --------------------------------------------------------------------------------------
# curve
# --------------------------------------------------------------------------------------


def _int(value: Any) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def fetch_curve_payload(
    mint: str,
    conn: sqlite3.Connection | None = None,
    *,
    config: ScanConfig = DEFAULT_CONFIG,
    priority: Priority = Priority.DISCOVERY,
) -> tuple[dict[str, Any] | None, Receipt]:
    """pump.fun's free coin route. Never raises; a dead provider is ``(None, UNAVAILABLE)``."""
    got = get_json(
        PROVIDER,
        CURVE_ENDPOINT,
        CURVE_URL.format(mint=mint),
        headers={"user-agent": USER_AGENT, "accept": "*/*"},
        ttl_s=config.curve_ttl_s,
        priority=priority,
        wait_for_slot_s=config.curve_wait_for_slot_s,
        timeout_s=config.curve_timeout_s,
        retries=config.curve_retries,
        conn=conn,
    )
    if not got.ok or not isinstance(got.data, dict):
        return None, got.receipt
    return got.data, got.receipt


def _observe_flow(
    chain: Chain,
    token: str,
    curve: dict[str, Any],
    conn: Any,
    now: int,
    fallback_basis: str,
) -> tuple[dict[str, Any], str, str | None]:
    """Fold per-token trade flow into the curve, and **record what it proved about coverage**.

    Returns ``(curve, velocity_basis, tape_coverage)``.

    ## Why the coverage record is written *here* and nowhere else

    The pump.fun trade route serves a mint's tape only while that mint is trading.
    Measured on 2026-09-20: a bonding-curve mint idle 12 minutes still answered (11 of 11),
    one idle 48 minutes returned 503 ``degraded_lanes: ["trade_api.list_trades"]`` (37 of
    37), per mint and persistent under controlled retry, while ``/coins/{mint}`` kept
    answering for the very same mints. A backfill pass over the 435 tokens we already knew
    attempted 78 and recovered **2**; the rest had been idle for hours and are gone for
    good. See ``kaiba.ingest.tape.HOT_WINDOW_ANSWERED_MAX_IDLE_MIN``.

    So tape coverage is a going-forward property. Tier-1 scan time is the moment the token
    is seconds old and the route will answer, which makes this function the only place in
    the system where a complete tape is reliably obtainable — and every launch that passes
    through here without its coverage being written down is a permanent hole.

    The collection itself was already happening: ``token_flow.observe`` walks up to
    ``FlowConfig.scan_max_pages`` pages of trades and writes them to ``swaps``. What was
    missing is that nothing recorded whether that walk *finished*, so the evidence existed
    and no consumer could tell a complete tape from a fragment. ``record_scan_capture``
    makes no provider call and adds no latency — it reads the flow outcome ``observe``
    already put in the curve dict and writes one row.

    ## The geometry guard, which predates this

    ``graduation_sol`` is derived from the constant-product invariant, and on one live
    token it collapsed 38.5 -> 11.9 -> 1.19 -> 0.114 SOL across four snapshots ninety
    seconds apart as ``k`` fell 345x, while the token-reserve invariant held exactly. Five
    of six other tokens were stable to the digit. Since the curve-velocity floor is
    ``graduation_sol * 0.0021``, the floor moved 345x with it — the lane would have been
    judging the same behaviour against a threshold three orders of magnitude apart from
    one minute to the next. A target that moves that much is not a target, so it is
    dropped rather than used.
    """
    try:
        from kaiba.ingest import token_flow

        observed, basis = token_flow.observe(chain, token, curve, conn, at_ms=now)
        if observed:
            curve, fallback_basis = observed, basis or fallback_basis
    except Exception as exc:  # noqa: BLE001 - flow is an enrichment, not a precondition
        log.debug("token flow unavailable for %s: %s", token[:16], exc)

    tape_coverage = _record_tape(chain, token, curve, conn, now)
    curve = _drop_unstable_graduation(chain, token, curve, conn)
    return curve, fallback_basis, tape_coverage


def _record_tape(
    chain: Chain, token: str, curve: Mapping[str, Any], conn: Any, now: int
) -> str | None:
    """Persist the coverage verdict for this scan's collection. Never raises.

    Bookkeeping must not be able to cost a scan. If this fails the tape rows are still in
    ``swaps`` and the token simply reads as unproved, which is the fail-closed direction:
    a consumer that needs completeness will decline rather than act on a fragment.
    """
    try:
        from kaiba.ingest import tape as tape_mod

        record = tape_mod.record_scan_capture(chain, token, curve, conn, at_ms=now)
    except Exception as exc:  # noqa: BLE001 - coverage bookkeeping is never load-bearing
        log.debug("tape capture unavailable for %s: %s", token[:16], exc)
        return None
    return record.coverage if record is not None else None


#: How far the derived graduation target may move between observations before we stop
#: trusting it. Generous, because real drift from fees is small and the failure we are
#: catching moved by 345x.
GRADUATION_DRIFT_MAX = Decimal("1.5")


def _drop_unstable_graduation(
    chain: Chain, token: str, curve: dict[str, Any], conn: Any
) -> dict[str, Any]:
    """Remove ``graduation_sol`` when history says it is not stable for this token."""
    target = curve.get("graduation_sol")
    if target is None or conn is None:
        return curve
    try:
        from kaiba.ingest.token_flow import latest_snapshot

        prior = latest_snapshot(chain, token, conn)
    except Exception:  # noqa: BLE001 - no history is not instability
        return curve
    previous = (prior or {}).get("graduation_sol") if isinstance(prior, dict) else None
    if previous in (None, "", 0):
        return curve
    try:
        now_t, was_t = Decimal(str(target)), Decimal(str(previous))
        if was_t <= 0 or now_t <= 0:
            return curve
        ratio = max(now_t / was_t, was_t / now_t)
    except (ArithmeticError, InvalidOperation, TypeError, ValueError):
        return curve
    if ratio <= GRADUATION_DRIFT_MAX:
        return curve
    out = dict(curve)
    out["graduation_sol"] = None
    out["graduation_note"] = (
        f"dropped: target moved {ratio:.1f}x since the last observation "
        f"({was_t} -> {now_t} SOL); a target that moves that much is not a target"
    )
    log.info("curve geometry unstable for %s: target moved %.1fx", token[:16], ratio)
    return out


def curve_from_payload(
    payload: Mapping[str, Any],
    *,
    at_ms: int | None = None,
    swaps: int | None = None,
    swaps_basis: str | None = None,
    config: ScanConfig = DEFAULT_CONFIG,
) -> tuple[dict[str, Any] | None, str]:
    """Map a pump.fun coin payload to the dict ``curve_velocity`` reads.

    Pure, so the arithmetic is testable without a network. Returns ``(curve, note)`` and
    ``(None, reason)`` when the payload cannot honestly produce one — a graduated curve, a
    non-SOL quote, or reserves we could not read. There is no partial curve: a dict with
    ``progress_pct`` present and the velocity fields missing would let the lane run on half
    the evidence it asked for.
    """
    now = at_ms if at_ms is not None else now_ms()

    quote_mint = payload.get("quote_mint")
    quote_decimals = _int(payload.get("quote_decimals"))
    if quote_mint is not None and str(quote_mint) not in SOL_QUOTE_MINTS:
        return None, f"non_sol_quote:{str(quote_mint)[:12]}"
    if quote_decimals is not None and quote_decimals != 9:
        # A SOL-looking quote mint with 6 decimals is not lamports; refuse the unit mix.
        return None, f"unexpected_quote_decimals:{quote_decimals}"

    if bool(payload.get("complete")):
        return None, "curve_complete"

    virtual_token = _int(payload.get("virtual_token_reserves"))
    real_token = _int(payload.get("real_token_reserves"))
    virtual_sol = _int(payload.get("virtual_sol_reserves"))
    real_sol = _int(payload.get("real_sol_reserves"))
    if virtual_token is None or real_token is None or virtual_sol is None or real_sol is None:
        return None, "reserves_unreadable"
    if virtual_token <= 0 or virtual_sol <= 0 or real_token < 0:
        return None, "reserves_nonpositive"
    if real_sol < 0 or real_sol >= virtual_sol:
        # ``virtual_sol - real_sol`` is this curve's starting point; a non-positive one
        # means the payload does not describe a bonding curve we understand.
        return None, "sol_reserves_inconsistent"

    # Derive this curve's own geometry from the constant-product invariant rather than
    # assuming the classic 30 / 793.1e12 / 85 triple. On a classic curve this returns
    # exactly those three numbers; on the low- and high-cap launches that now make up
    # about a tenth of the feed it returns theirs.
    try:
        k = Decimal(virtual_sol) * Decimal(virtual_token)
        virtual_sol_initial = Decimal(virtual_sol - real_sol)
        virtual_token_initial = k / virtual_sol_initial
        real_token_initial = virtual_token_initial - Decimal(RESERVED_TOKEN_ATOMS)
        if real_token_initial <= 0:
            return None, "derived_initial_reserves_nonpositive"
        graduation_sol = (
            k / Decimal(RESERVED_TOKEN_ATOMS) - virtual_sol_initial
        ) / LAMPORTS_PER_SOL
        progress = (real_token_initial - Decimal(real_token)) / real_token_initial * 100
    except (ArithmeticError, DivisionByZero, InvalidOperation):
        return None, "curve_geometry_unreadable"
    if progress < -1 or progress > 101:
        # Small negatives are fee drift on a curve that has barely traded; a large one
        # means the invariant does not hold here and nothing derived from it is usable.
        return None, "derived_progress_out_of_range"
    progress = max(Decimal(0), min(Decimal(100), progress))
    if graduation_sol <= 0:
        return None, "derived_graduation_nonpositive"

    sol_in_curve = Decimal(real_sol) / LAMPORTS_PER_SOL

    created_ms = _int(payload.get("created_timestamp"))
    if created_ms is not None and created_ms < 10_000_000_000:
        created_ms *= 1000  # some routes answer in seconds
    age_s = (now - created_ms) / 1000.0 if created_ms else None

    sol_per_min: Decimal | None = None
    if age_s is not None and age_s >= config.min_age_for_per_minute_s:
        sol_per_min = sol_in_curve / (Decimal(str(age_s)) / Decimal(60))

    curve: dict[str, Any] = {
        "progress_pct": progress,
        "progress_basis": "real_token_reserves_vs_derived_initial",
        "sol_in_curve": sol_in_curve,
        "sol_in_curve_lamports": real_sol,
        "sol_per_min": sol_per_min,
        "swaps": swaps,
        "swaps_basis": swaps_basis,
        "graduation_sol": graduation_sol,
        # The share of the *SOL* target raised. Not what the lane bands on — token
        # progress is — but the number that shows how differently two launches with the
        # same 46% token progress are placed when one graduates at 85 SOL and the other
        # at 0.41. A velocity threshold in absolute SOL cannot be right for both.
        "sol_raised_pct_of_graduation": (
            sol_in_curve / graduation_sol * 100 if graduation_sol > 0 else None
        ),
        "virtual_sol_initial": virtual_sol_initial / LAMPORTS_PER_SOL,
        "real_token_initial": int(real_token_initial),
        "classic_curve": abs(virtual_sol_initial - CLASSIC_VIRTUAL_SOL_LAMPORTS)
        < Decimal(10_000_000),
        "virtual_sol_reserves": virtual_sol,
        "virtual_token_reserves": virtual_token,
        "real_token_reserves": real_token,
        "created_ms": created_ms,
        "age_s": round(age_s, 1) if age_s is not None else None,
        "last_trade_ms": _int(payload.get("last_trade_timestamp")),
        "market_cap_usd": _dec(payload.get("usd_market_cap")),
        "source": PROVIDER,
        "observed_ms": now,
    }
    if swaps is None:
        curve.pop("swaps")  # absent, not zero: the lane must not divide by it
    basis = "sol_per_swap" if swaps else ("sol_per_min" if sol_per_min is not None else "none")
    return curve, basis


def swap_count_if_covered(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection,
    *,
    created_ms: int | None,
    config: ScanConfig = DEFAULT_CONFIG,
) -> tuple[int | None, str]:
    """Our swap count for this token, **only** if our coverage starts at its launch.

    ``sol_per_swap`` is the published graduation predictor and per-minute is the weaker
    fallback, so there is a standing temptation to divide curve SOL by however many swap
    rows we happen to hold. That is wrong by orders of magnitude: three observed swaps out
    of four hundred real ones turns 0.19 SOL per swap into 25, clearing a 0.18 floor by a
    factor of 130 on a token that is pacing exactly at the population average. The rule is
    therefore coverage, not presence.
    """
    try:
        row = fetch_one(
            conn,
            "SELECT COUNT(*) AS n, MIN(ts_ms) AS first_ms FROM swaps WHERE chain=? AND token=?",
            (chain.value, token),
        )
    except sqlite3.Error as exc:
        log.warning("scanner: swap count unreadable for %s (%s)", token[:12], exc)
        return None, "swaps_unreadable"
    n = _int(row["n"]) if row else 0
    if not n:
        return None, "no_swap_rows"
    if created_ms is None:
        return None, "creation_time_unknown"
    first_ms = _int(row["first_ms"]) if row else None
    if first_ms is None:
        return None, "no_swap_rows"
    lag_s = (first_ms - int(created_ms)) / 1000.0
    if lag_s > config.swap_coverage_grace_s:
        return None, f"coverage_starts_{lag_s:.0f}s_after_launch"
    return n, "covered_from_launch"


# --------------------------------------------------------------------------------------
# context inputs
# --------------------------------------------------------------------------------------


def load_token_meta(chain: Chain, token: str, conn: sqlite3.Connection) -> Token | None:
    """The ``tokens`` row as a :class:`~kaiba.core.schemas.Token`, or ``None``."""
    try:
        row = fetch_one(
            conn, "SELECT * FROM tokens WHERE chain=? AND address=?", (chain.value, token)
        )
    except sqlite3.Error as exc:
        log.warning("scanner: tokens row unreadable for %s (%s)", token[:12], exc)
        return None
    if not row:
        return None
    try:
        return Token(
            address=row["address"],
            chain=Chain(row["chain"]),
            symbol=row["symbol"],
            name=row["name"],
            decimals=row["decimals"],
            creator=row["creator"],
            created_ms=row["created_ms"],
            launchpad=row["launchpad"],
            pool=row["pool"],
            migrated_ms=row["migrated_ms"],
            meta=jload(row["meta_json"], {}) or {},
        )
    except Exception as exc:  # noqa: BLE001 - a malformed row is a missing row, not a crash
        log.warning("scanner: tokens row unusable for %s (%s)", token[:12], exc)
        return None


def load_recent_buys(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection,
    *,
    since_ms: int,
    limit: int = DEFAULT_CONFIG.recent_buys_limit,
) -> list[dict[str, Any]]:
    """Swaps on this token inside the window, oldest first.

    Both sides are loaded, not just buys: ``lanes._net_buyers`` nets sells against buys, so
    handing it only the buys would report conviction from a wallet that had already exited.
    """
    try:
        rows = fetch_all(
            conn,
            "SELECT * FROM swaps WHERE chain=? AND token=? AND ts_ms >= ? "
            "ORDER BY ts_ms DESC LIMIT ?",
            (chain.value, token, int(since_ms), int(limit)),
        )
    except sqlite3.Error as exc:
        log.warning("scanner: swaps unreadable for %s (%s)", token[:12], exc)
        return []
    return list(reversed(rows))


def load_caller(chain: Chain, token: str, conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Newest call on this token joined to that caller's measured reputation.

    ``kol_fade`` will run this query itself when ``ctx.caller`` is ``None``, but the point
    of :class:`LaneContext` is that it is a *snapshot*: assembling it here means a replay
    of the stored context reproduces the decision, rather than re-reading a table that has
    moved on. ``caller_calls`` is empty today, so this returns ``None`` in practice.
    """
    try:
        return fetch_one(
            conn,
            "SELECT c.platform, c.caller_id, c.display_name, c.calls, c.expectancy, c.mode, "
            "       cc.ts_ms AS call_ms "
            "FROM caller_calls cc JOIN callers c "
            "  ON c.platform = cc.platform AND c.caller_id = cc.caller_id "
            "WHERE cc.chain=? AND cc.token=? ORDER BY cc.ts_ms DESC LIMIT 1",
            (chain.value, token),
        )
    except sqlite3.Error as exc:
        log.debug("scanner: caller lookup failed for %s (%s)", token[:12], exc)
        return None


def _dossier_for(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection,
    *,
    config: ScanConfig,
    at_ms: int,
    force: bool = False,
) -> tuple[Any, bool, int]:
    """``(dossier, reused, elapsed_ms)``. The expensive step, skipped when it can be."""
    if not force:
        existing = load_dossier(chain, token, conn)
        if existing is not None:
            age_s = (at_ms - existing.built_at_ms) / 1000.0
            if 0 <= age_s <= config.dossier_max_age_s:
                return existing, True, 0
    started = time.perf_counter()
    from kaiba.intelligence.dyor import scan_token as dyor_scan

    dossier = dyor_scan(token, chain, conn=conn)
    return dossier, False, int((time.perf_counter() - started) * 1000)


def build_context(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection,
    *,
    config: ScanConfig = DEFAULT_CONFIG,
    at_ms: int | None = None,
    force_dossier: bool = False,
    extras: dict[str, Any] | None = None,
) -> tuple[LaneContext, ScanResult]:
    """Assemble everything the lanes are allowed to look at, once, for one token.

    Returns the context plus a partially filled :class:`ScanResult` describing what the
    assembly actually managed to find, so the caller can report "the curve was missing"
    rather than only "no lane fired".
    """
    now = at_ms if at_ms is not None else now_ms()
    token_meta = load_token_meta(chain, token, conn)

    dossier, reused, dossier_ms = _dossier_for(
        chain, token, conn, config=config, at_ms=now, force=force_dossier
    )

    buys = load_recent_buys(
        chain,
        token,
        conn,
        since_ms=now - config.recent_buys_window_s * 1000,
        limit=config.recent_buys_limit,
    )

    curve: dict[str, Any] | None = None
    curve_note: str | None = None
    velocity_basis: str | None = None
    tape_coverage: str | None = None
    if chain is Chain.SOL and (token_meta is None or token_meta.migrated_ms is None):
        launchpad = (token_meta.launchpad if token_meta else None) or ""
        if launchpad and "pump" not in launchpad.lower():
            curve_note = f"not_a_pumpfun_launch:{launchpad[:24]}"
        else:
            payload, receipt = fetch_curve_payload(token, conn, config=config)
            if payload is None:
                curve_note = f"curve_unavailable:{(receipt.note or receipt.basis.value)[:80]}"
            else:
                created_ms = _int(payload.get("created_timestamp")) or (
                    token_meta.created_ms if token_meta else None
                )
                swaps, swaps_basis = swap_count_if_covered(
                    chain, token, conn, created_ms=created_ms, config=config
                )
                curve, note = curve_from_payload(
                    payload, at_ms=now, swaps=swaps, swaps_basis=swaps_basis, config=config
                )
                if curve is None:
                    curve_note = note
                else:
                    velocity_basis = note
                    # Snapshot the observation and take the stronger velocity where the
                    # collector has walked the token's full trade history: one snapshot
                    # then yields sol_in_curve / total_trades directly, which beats a
                    # delta between two. It also cross-checks the derived geometry
                    # against history and refuses a curve whose target is drifting.
                    curve, velocity_basis, tape_coverage = _observe_flow(
                        chain, token, curve, conn, now, velocity_basis
                    )
    elif chain is Chain.SOL:
        curve_note = "already_migrated"
    else:
        curve_note = f"no_curve_source_for_{chain.value}"

    ctx = LaneContext(
        chain=chain,
        token=token,
        now_ms=now,
        conn=conn,
        dossier=dossier,
        recent_buys=buys,
        token_meta=token_meta,
        curve=curve,
        caller=load_caller(chain, token, conn),
        params={},
        extras=dict(extras or {}),
    )
    partial = ScanResult(
        chain=chain,
        token=token,
        dossier_ms=dossier_ms,
        dossier_grade=dossier.grade if dossier is not None else None,
        dossier_reused=reused,
        dossier_blockers=tuple(
            b.value if hasattr(b, "value") else str(b) for b in (dossier.blockers if dossier else [])
        ),
        dossier_unknowns=len(dossier.unknowns) if dossier is not None else 0,
        curve_ok=curve is not None,
        curve_note=curve_note,
        velocity_basis=velocity_basis,
        tape_coverage=tape_coverage,
        recent_buys=len(buys),
        ts_ms=now,
    )
    return ctx, partial


# --------------------------------------------------------------------------------------
# the pass
# --------------------------------------------------------------------------------------


def scan(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection | None = None,
    *,
    config: ScanConfig = DEFAULT_CONFIG,
    source: str = "token",
    force_dossier: bool = False,
    extras: dict[str, Any] | None = None,
    at_ms: int | None = None,
) -> ScanResult:
    """One full tier-1 pass. **Never raises** — a failure comes back as a result.

    That is not defensive habit: this is called from a loop that must outlive any single
    token, and the audit's own lesson is that a silent ``except`` is worse than none. So
    the failure is recorded on the result, counted in :class:`ScanStats` and emitted, and
    only then swallowed.
    """
    c = conn or get_conn()
    started = time.perf_counter()
    RECENT.mark(chain, token)
    try:
        ctx, partial = build_context(
            chain, token, c, config=config, at_ms=at_ms, force_dossier=force_dossier, extras=extras
        )
        signals = lanes_mod.evaluate_and_record(ctx, c)
    except Exception as exc:  # noqa: BLE001 - one bad token must never stop the loop
        elapsed = int((time.perf_counter() - started) * 1000)
        detail = f"{type(exc).__name__}: {exc}"[:300]
        log.exception("scanner: tier-1 pass failed for %s:%s", chain.value, token[:16])
        _emit(
            EVENT_SCAN_FAILED,
            {"token": token, "source": source, "error": detail, "elapsed_ms": elapsed},
            chain=chain,
            subject=token,
            level="warn",
            conn=c,
        )
        return ScanResult(
            chain=chain,
            token=token,
            source=source,
            ok=False,
            elapsed_ms=elapsed,
            error=detail,
            ts_ms=now_ms(),
        )

    elapsed = int((time.perf_counter() - started) * 1000)
    result = ScanResult(
        chain=partial.chain,
        token=partial.token,
        source=source,
        ok=True,
        elapsed_ms=elapsed,
        dossier_ms=partial.dossier_ms,
        dossier_grade=partial.dossier_grade,
        dossier_reused=partial.dossier_reused,
        dossier_blockers=partial.dossier_blockers,
        dossier_unknowns=partial.dossier_unknowns,
        curve_ok=partial.curve_ok,
        curve_note=partial.curve_note,
        velocity_basis=partial.velocity_basis,
        tape_coverage=partial.tape_coverage,
        recent_buys=partial.recent_buys,
        signals=tuple(signals),
        ts_ms=partial.ts_ms,
    )
    _emit(
        EVENT_SCANNED,
        {
            "token": token,
            "source": source,
            "elapsed_ms": elapsed,
            "dossier_ms": partial.dossier_ms,
            "dossier_reused": partial.dossier_reused,
            "grade": partial.dossier_grade.value if partial.dossier_grade else None,
            "curve_ok": partial.curve_ok,
            "curve_note": partial.curve_note,
            "velocity_basis": partial.velocity_basis,
            "tape_coverage": partial.tape_coverage,
            "recent_buys": partial.recent_buys,
            "lanes_fired": list(result.lanes_fired),
            "params_version": PARAMS_VERSION,
        },
        chain=chain,
        subject=token,
        level="info" if signals else "debug",
        conn=c,
    )
    return result


def _emit(kind: str, payload: dict[str, Any], **kw: Any) -> None:
    """Telemetry must never be the reason a scan fails."""
    try:
        emit(kind, payload, **kw)
    except Exception as exc:  # noqa: BLE001
        log.debug("scanner: could not emit %s (%s)", kind, type(exc).__name__)


# --------------------------------------------------------------------------------------
# work sources
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkItem:
    chain: Chain
    token: str
    source: str
    score: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict)


class _RecentlyScanned:
    """TTL set of tokens tier 1 has just looked at.

    Three sources feed :func:`next_work` and two of them can name the same token: when
    ingest and tier 1 share a process, a launch sits in the in-memory queue *and* in
    ``triage_decisions``. Without this the token is scanned twice and the second pass
    cannot produce anything the first did not — same dossier, same signal bucket — while
    occupying one of the ~7 slots an hour we have.
    """

    def __init__(self, max_entries: int = 4096) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()
        self._max = max_entries

    def mark(self, chain: Chain, token: str) -> None:
        with self._lock:
            if len(self._seen) >= self._max:
                self._seen.clear()  # bounded and blunt; correctness does not depend on it
            self._seen[f"{chain.value}:{token}"] = time.monotonic()

    def is_recent(self, chain: Chain, token: str, ttl_s: float) -> bool:
        if ttl_s <= 0:
            return False
        with self._lock:
            at = self._seen.get(f"{chain.value}:{token}")
        return at is not None and (time.monotonic() - at) < ttl_s

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


RECENT = _RecentlyScanned()


def _queue_work(queue: triage_mod.TriageQueue, n: int) -> list[WorkItem]:
    out: list[WorkItem] = []
    for decision in queue.pop_batch(n):
        if decision.token:
            out.append(
                WorkItem(chain=decision.chain, token=decision.token, source="queue", score=decision.score)
            )
    return out


def _migration_work(
    conn: sqlite3.Connection, n: int, *, config: ScanConfig
) -> list[WorkItem]:
    """Tokens that migrated since the watermark, newest-usable first.

    Read off the event bus rather than ``tokens.migrated_ms`` so the watermark is a
    monotonic event id: a restart resumes instead of re-scanning, and a re-stamped token
    row cannot resurrect an old migration.
    """
    if not config.include_migrations or n <= 0:
        return []
    after_id = _watermark(conn, MIGRATION_WATERMARK_KEY)
    try:
        rows = fetch_all(
            conn,
            "SELECT id, chain, subject, payload, ts_ms FROM events "
            "WHERE kind=? AND id > ? ORDER BY id ASC LIMIT ?",
            ("token.migrated", after_id, int(n)),
        )
    except sqlite3.Error as exc:
        log.warning("scanner: migration source unreadable (%s)", exc)
        return []

    now = now_ms()
    out: list[WorkItem] = []
    highest = after_id
    for row in rows:
        highest = max(highest, int(row["id"]))
        token = row["subject"]
        if not token:
            continue
        payload = jload(row["payload"], {}) or {}
        migrated_ms = _int(payload.get("migrated_ms")) or _int(row["ts_ms"]) or now
        if (now - migrated_ms) / 1000.0 > config.migration_max_age_s:
            continue  # past migration-fade's own window; scanning it cannot produce a signal
        try:
            chain = Chain(row["chain"] or Chain.SOL.value)
        except ValueError:
            chain = Chain.SOL
        out.append(
            WorkItem(
                chain=chain,
                token=token,
                source="migration",
                extras={"migration_ms": migrated_ms, "migration": payload},
            )
        )
    if highest > after_id:
        _set_migration_watermark(conn, highest)
    return out


def _watermark(conn: sqlite3.Connection, key: str) -> int:
    try:
        row = fetch_one(conn, "SELECT value FROM kv WHERE key=?", (key,))
    except sqlite3.Error as exc:
        log.warning("scanner: watermark %s unreadable (%s)", key, exc)
        return 0
    if not row:
        return 0
    return int((jload(row["value"], {}) or {}).get("row_id", 0) or 0)


def _set_watermark(conn: sqlite3.Connection, key: str, row_id: int) -> None:
    from kaiba.core.db import jdump, upsert

    try:
        upsert(
            conn,
            "kv",
            {"key": key, "value": jdump({"row_id": int(row_id)}), "updated_ms": now_ms()},
            ["key"],
        )
    except sqlite3.Error as exc:
        log.warning("scanner: could not store the watermark %s (%s)", key, exc)


def _set_migration_watermark(conn: sqlite3.Connection, event_id: int) -> None:
    _set_watermark(conn, MIGRATION_WATERMARK_KEY, event_id)


def _db_queue_work(conn: sqlite3.Connection, n: int, *, config: ScanConfig) -> list[WorkItem]:
    """Unscanned tier-0 verdicts from ``triage_decisions``, best first.

    Same doctrine as :class:`~kaiba.execution.triage.TriageQueue`, made durable: read the
    candidates that have arrived since the watermark, rank ``promote`` above ``defer`` and
    then by score, scan the best ``n`` — and **advance the watermark past all of them**.
    Dropping the rest is the honest behaviour at a 60:1 arrival ratio; carrying them
    forward would build a backlog whose head is half an hour stale by the time it is
    reached, which is the failure the bounded queue exists to prevent.
    """
    if not config.db_queue or n <= 0:
        return []
    after_id = _watermark(conn, TRIAGE_WATERMARK_KEY)
    cutoff = now_ms() - config.db_queue_max_age_s * 1000
    try:
        rows = fetch_all(
            conn,
            "SELECT id, chain, token, verdict, score, ts_ms FROM triage_decisions "
            "WHERE id > ? AND verdict != 'reject' AND token != '' "
            "ORDER BY id ASC LIMIT ?",
            (after_id, int(config.db_queue_window)),
        )
    except sqlite3.Error as exc:
        log.warning("scanner: triage_decisions unreadable (%s)", exc)
        return []
    if not rows:
        return []

    highest = max(int(r["id"]) for r in rows)
    fresh = [r for r in rows if int(r["ts_ms"] or 0) >= cutoff]
    if not config.db_queue_admit_defers:
        fresh = [r for r in fresh if str(r["verdict"]) == triage_mod.Verdict.PROMOTE.value]
    ranked = sorted(
        fresh,
        key=lambda r: (
            0 if str(r["verdict"]) == triage_mod.Verdict.PROMOTE.value else 1,
            -float(r["score"] or 0.0),
            int(r["id"]),
        ),
    )
    # Bucket by chain, then take one from each in turn -- the SAME shape as
    # `_smart_flow_work`, deliberately, because this is the same bug in a second feeder.
    #
    # MEASURED 2026-09-23, `token.scanned` per 30 minutes: a burst of robinhood launches
    # filled this queue and, ranked in one pool by (verdict, score, id), took the whole
    # budget -- robinhood 40 -> 319 while sol fell 149 -> 30. Sol produced most of the
    # signals, so signal output went from 19-32 per 30 minutes to ONE. Nothing was wrong
    # with the lanes; they were never offered a sol token.
    #
    # `_smart_flow_work` had this exact fault (robinhood won 0 of 528 qualifying rows) and
    # was fixed on 2026-09-22; this feeder was left ranking one pool. Ordering still
    # decides WHICH token from a chain is scanned; this only decides that every chain with
    # queued work is scanned at all. Same slice size, same passes, same providers.
    by_chain: dict[Chain, list[WorkItem]] = {}
    seen: set[str] = set()
    for row in ranked:
        token = str(row["token"])
        if not token or token in seen:
            continue
        seen.add(token)
        try:
            chain = Chain(row["chain"] or Chain.SOL.value)
        except ValueError:
            chain = Chain.SOL
        by_chain.setdefault(chain, []).append(
            WorkItem(chain=chain, token=token, source="triage_db", score=float(row["score"] or 0.0))
        )

    # Chains taken in descending order of their BEST candidate, so the strongest chain
    # still leads; the round-robin only stops it taking every slot.
    order = sorted(by_chain, key=lambda ch: -(by_chain[ch][0].score if by_chain[ch] else 0))
    out: list[WorkItem] = []
    rank = 0
    while len(out) < int(n) and any(len(by_chain[ch]) > rank for ch in order):
        for ch in order:
            if len(out) >= int(n):
                break
            bucket = by_chain[ch]
            if len(bucket) <= rank:
                continue
            out.append(bucket[rank])
        rank += 1
    _set_watermark(conn, TRIAGE_WATERMARK_KEY, highest)
    return out


def _smart_flow_min_wallets(config: ScanConfig) -> int:
    """The lane's own threshold unless the operator pinned one."""
    if config.smart_flow_min_wallets is not None:
        return max(1, int(config.smart_flow_min_wallets))
    try:
        # Through the lane's own accessor, the way engine.py reads params, so an operator
        # override in risk.yaml moves the feeder and the lane together.
        params = lanes_mod.LaneContext(chain=Chain.SOL, token="").lane_params(Lane.SM_TRENCHES)
        return max(1, int(params.get("min_smart_degen", 3)))
    except Exception:  # noqa: BLE001 - a missing default is not a reason to stop scanning
        return 3


#: The wallet tags that make a buyer "smart" -- taken from the lane rather than respelled,
#: so the feeder and ``lanes.sm_trenches`` can never disagree about who counts.
#: ``db path -> (expires_monotonic, {chain: [address, ...]})``. Keyed by the database file
#: so two databases in one process (the test suite) never share a set.
_SMART_SET_CACHE: dict[str, tuple[float, dict[str, list[str]]]] = {}
_SMART_SET_LOCK = threading.Lock()


def _db_key(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        path = str(row[2] or "") if row else ""
    except sqlite3.Error:
        path = ""
    return path or f"conn:{id(conn)}"


def _smart_set(
    conn: sqlite3.Connection, tags: Sequence[str], *, ttl_s: float
) -> dict[str, list[str]]:
    """Smart wallets by chain: a smart tag on ``wallets`` or a smart ``wallet_scores`` archetype.

    The same two routes ``lanes.sm_trenches`` uses. Cached for ``ttl_s`` per database.
    """
    key = _db_key(conn)
    now = time.monotonic()
    with _SMART_SET_LOCK:
        hit = _SMART_SET_CACHE.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    tag_clause = " OR ".join("w.tags_json LIKE ?" for _ in tags)
    rows = fetch_all(
        conn,
        f"SELECT w.chain AS chain, w.address AS address FROM wallets w WHERE {tag_clause} "
        "UNION "
        "SELECT sc.chain, sc.address FROM wallet_scores sc "
        "WHERE sc.archetype IN ('smart_money','top_trader')",
        tuple(f'%"{t}"%' for t in tags),
    )
    by_chain: dict[str, list[str]] = {}
    for r in rows:
        by_chain.setdefault(str(r["chain"]), []).append(str(r["address"]))
    with _SMART_SET_LOCK:
        _SMART_SET_CACHE[key] = (now + max(0.0, ttl_s), by_chain)
    return by_chain


def _smart_tag_values() -> list[str]:
    try:
        return sorted({t.value if hasattr(t, "value") else str(t) for t in lanes_mod.SMART_TAGS})
    except Exception:  # noqa: BLE001
        return ["pump_smart", "renowned", "smart_money", "top_trader"]


#: How a smart-flow token's ``tokens`` row is marked, so it is never mistaken for a
#: listener-written one and is never overwritten by this path on a later sighting.
SMART_FLOW_TOKEN_SOURCE = "smart_flow"


def _register_smart_flow_token(
    conn: sqlite3.Connection, chain: Chain, token: str, *, discovered_by: str = "smart_flow"
) -> bool:
    """Put a discovered token in the registry, if nothing else already has. Returns whether
    a row was written.

    ``fills.token_decimals`` reads the chain for a token's decimals ONLY when that token
    already has a ``tokens`` row -- its docstring says "every engine-traded token is in it
    by construction", and before smart flow that was true, because every candidate came
    from a launch listener which registers as it ingests.

    Smart flow broke that invariant: it surfaces tokens straight from the ``swaps`` tape,
    which no listener necessarily saw. MEASURED 2026-09-22 within minutes of arming it,
    two LIVE sm-trenches entries were abandoned with ``token_decimals_unavailable`` --
    "token not in the tokens registry; decimals not fetched" -- so the discovery worked,
    the sizing worked, and the order was never written.

    The fix belongs here rather than in the engine: the engine's refusal to floor a live
    order on an unverified decimals is correct and must stay (a decimals wrong by six
    turns a correct min_out into one a million times too small). What was missing is that
    a new discovery route has to honour the registry invariant the older ones do.

    Deliberately minimal and non-destructive: only the facts we actually have. No
    ``created_ms`` -- the earliest swap we hold is when we first SAW it trade, which is
    not its creation, and writing one would be inventing a launch time that the
    migration and launch-age gates would then read as fact.
    """
    existing = fetch_one(
        conn, "SELECT 1 FROM tokens WHERE chain = ? AND address = ?", (chain.value, token)
    )
    if existing is not None:
        return False
    first_seen = fetch_one(
        conn,
        "SELECT MIN(ts_ms) AS first FROM swaps WHERE chain = ? AND token = ?",
        (chain.value, token),
    )
    seen_ms = int((first_seen or {}).get("first") or now_ms())
    try:
        conn.execute(
            "INSERT OR IGNORE INTO tokens (chain, address, first_seen_ms, meta_json) "
            "VALUES (?,?,?,?)",
            (chain.value, token, seen_ms,
             jdump({"source": SMART_FLOW_TOKEN_SOURCE, "discovered_by": discovered_by,
                    "note": f"seen in the swaps tape being bought ({discovered_by}); "
                            "not observed at launch, so created_ms is deliberately unset"})),
        )
        conn.commit()
    except sqlite3.Error as exc:
        log.warning("could not register smart-flow token %s: %s", token[:16], exc)
        return False
    return True


def _smart_flow_work(conn: sqlite3.Connection, n: int, *, config: ScanConfig) -> list[WorkItem]:
    """Tokens smart-money wallets are buying inside the window, at any token age.

    The same two routes ``lanes.sm_trenches`` uses to decide a wallet is smart: a tag on
    the ``wallets`` row (written only after the cohort screen -- see
    ``tracker.seed_from_cohorts``), or a ``wallet_scores`` archetype. Raw buys are counted
    rather than net buyers, which can overcount by a wallet that also sold: that is the
    right bias for a FEEDER, because the lane re-derives net buyers itself and a token we
    scanned and refused costs one pass, while a token we never looked at costs the trade.

    Quote assets are dropped here as well as in ``engine.decide``: WSOL was the top-ranked
    candidate the day the cohort landed, and a pass spent on it is a pass not spent on a
    real one.
    """
    if not config.smart_flow or n <= 0:
        return []
    tags = _smart_tag_values()
    if not tags:
        return []
    min_wallets = _smart_flow_min_wallets(config)
    cutoff = now_ms() - int(config.smart_flow_window_s) * 1000
    try:
        smart = _smart_set(conn, tags, ttl_s=float(config.smart_set_ttl_s))
    except sqlite3.Error as exc:
        log.warning("scanner: smart-wallet set unreadable (%s)", exc)
        return []
    # MEASURED 2026-09-29: the old single query joined the whole smart set to the whole of
    # ``swaps`` and planned as a walk of all 7.48M ``idx_swaps_token`` entries, plus full
    # scans of ``wallets`` and ``wallet_scores``, on nearly every batch. It kept the scanner
    # inside one long read almost continuously -- one of the snapshots that stopped SQLite
    # resetting the WAL. Driving the join from the smart set (CROSS JOIN fixes the order)
    # makes it one short range seek per smart wallet over the look-back window.
    sql = (
        "SELECT s.token AS token, COUNT(DISTINCT s.wallet) AS nw, MAX(s.ts_ms) AS last_ms "
        "FROM json_each(?) AS j CROSS JOIN swaps AS s INDEXED BY idx_swaps_wallet "
        "WHERE s.chain = ? AND s.wallet = j.value AND s.ts_ms >= ? "
        "AND s.side = 'buy' AND s.token != '' "
        "GROUP BY s.token HAVING nw >= ? ORDER BY nw DESC, last_ms DESC LIMIT ?"
    )
    rows: list[dict[str, Any]] = []
    try:
        for chain_value, addresses in smart.items():
            if not addresses:
                continue
            for r in fetch_all(conn, sql, (
                jdump(addresses), chain_value, cutoff, int(min_wallets), int(config.smart_flow_limit),
            )):
                rows.append({"chain": chain_value, **r})
    except sqlite3.Error as exc:
        log.warning("scanner: smart-flow query unreadable (%s)", exc)
        return []
    # Same global order and bound the single query had.
    rows.sort(key=lambda r: (-int(r["nw"] or 0), -int(r["last_ms"] or 0)))
    rows = rows[: int(config.smart_flow_limit)]

    # Bucket by chain, then take one from each in turn. The SQL still orders by `nw`, so
    # ordering decides WHICH token from a chain is offered; this decides that every chain
    # with qualifying work is offered something at all.
    #
    # WHY, MEASURED on the live box 2026-09-22 over 3 hours of the real feeder: ranking
    # every chain in one pool by `nw` gave robinhood 0 slots out of 528 qualifying rows --
    # never one -- while bsc took 93 and sol 51. Robinhood's smart-buyer counts top out at
    # 6 where bsc reaches 26, so a smaller chain loses every comparison by construction,
    # however good its candidates are. It made zero decisions in that window.
    #
    # The comparison was never meaningful: `nw` counts wallets from each chain's own
    # cohort, and those cohorts differ in size and in how much of each chain we observe.
    # Across chains it measures the chain, not the candidate.
    #
    # Costs nothing: same slice size, same passes, same providers. A chain with no work
    # simply contributes no bucket, so one busy chain still fills the whole slice.
    by_chain: dict[Chain, list[WorkItem]] = {}
    for row in rows:
        token = str(row["token"] or "")
        if not token:
            continue
        try:
            chain = Chain(row["chain"] or Chain.SOL.value)
        except ValueError:
            continue
        if is_quote_asset(chain, token):
            continue
        by_chain.setdefault(chain, []).append(
            WorkItem(
                chain=chain,
                token=token,
                source="smart_flow",
                score=float(row["nw"] or 0),
                extras={"smart_buyers": int(row["nw"] or 0), "last_buy_ms": int(row["last_ms"] or 0)},
            )
        )

    # Chains taken in descending order of their BEST candidate, so the strongest chain
    # still leads; the round-robin only stops it taking every slot.
    order = sorted(by_chain, key=lambda ch: -(by_chain[ch][0].score if by_chain[ch] else 0))
    out: list[WorkItem] = []
    rank = 0
    while len(out) < n and any(len(by_chain[ch]) > rank for ch in order):
        for ch in order:
            if len(out) >= n:
                break
            bucket = by_chain[ch]
            if len(bucket) <= rank:
                continue
            item = bucket[rank]
            # Honour the registry invariant the engine relies on before offering the token.
            _register_smart_flow_token(conn, item.chain, item.token)
            out.append(item)
        rank += 1
    return out


#: Per database: when the proven-flow query may next run, and what it last offered.
#: ``(db, chain, token) -> (distinct proven buyers offered, monotonic time)``. A token is
#: re-offered only when MORE proven wallets are buying it than when it was last offered:
#: that is new evidence, and the only thing a re-scan inside the cooldown could act on.
_PROVEN_NEXT_AT: dict[str, float] = {}
_PROVEN_OFFERED: dict[tuple[str, str, str], tuple[int, float]] = {}
_PROVEN_LOCK = threading.Lock()
#: Forget an offer after this long; by then the lane's window has long closed.
_PROVEN_OFFER_TTL_S = 7_200.0


def _proven_flow_work(conn: sqlite3.Connection, n: int, *, config: ScanConfig) -> list[WorkItem]:
    """Tokens that at least ``min_entities`` proven wallets are buying inside the window.

    Driven entirely by ``confluence-5``'s own params, read the way the lane reads them, so
    the feeder and the lane cannot disagree: nothing is returned unless ``wallet_source``
    is ``proven``; the window, the threshold and the age gate are the lane's, per chain;
    and the cohort is the one the lane will count (``proven.proven_members``, same age
    limit). Raw buyers are counted, not net buyers -- the lane re-derives that itself, and
    for a feeder a token scanned and refused costs one pass while one never offered costs
    the signal.

    A token whose newest proven buy is already older than the lane's age gate is skipped:
    the lane would refuse it, so the pass would be spent for nothing.
    """
    if not config.proven_flow or n <= 0:
        return []
    db = _db_key(conn)
    mono = time.monotonic()
    with _PROVEN_LOCK:
        if _PROVEN_NEXT_AT.get(db, 0.0) > mono:
            return []
        _PROVEN_NEXT_AT[db] = mono + max(0.0, float(config.proven_flow_min_interval_s))
        if len(_PROVEN_OFFERED) > 5_000:
            for k in [k for k, (_, at) in _PROVEN_OFFERED.items() if mono - at > _PROVEN_OFFER_TTL_S]:
                _PROVEN_OFFERED.pop(k, None)
    try:
        from kaiba.core.config import get_risk
        from kaiba.learning.proven import proven_members

        params = LaneContext(chain=Chain.SOL, token="").lane_params(Lane.CONFLUENCE_5)
        if str(params.get("wallet_source") or "grade").strip().lower() != "proven":
            return []
        chains = list(get_risk().lane(Lane.CONFLUENCE_5).chains or [])
    except Exception as exc:  # noqa: BLE001 - a feeder must never take the scanner down
        log.warning("scanner: proven-flow config unreadable (%s)", exc)
        return []

    now = now_ms()
    sql = (
        "SELECT s.token AS token, COUNT(DISTINCT s.wallet) AS nw, MAX(s.ts_ms) AS last_ms "
        "FROM json_each(?) AS j CROSS JOIN swaps AS s INDEXED BY idx_swaps_wallet "
        "WHERE s.chain = ? AND s.wallet = j.value AND s.ts_ms >= ? "
        "AND s.side = 'buy' AND s.token != '' "
        "GROUP BY s.token HAVING nw >= ? ORDER BY last_ms DESC LIMIT ?"
    )
    out: list[WorkItem] = []
    for chain in chains:
        if len(out) >= n:
            break
        try:
            cohort = proven_members(
                conn, chain, max_age_s=float(params.get("proven_max_cohort_age_s", 259_200)), at_ms=now
            )
            if cohort is None or not cohort.members:
                continue
            window_s = int(lanes_mod._per_chain(params, "window_s", chain, 120))
            min_n = max(1, int(lanes_mod._per_chain(params, "min_entities", chain, 5)))
            max_age_s = int(lanes_mod._per_chain(params, "max_signal_age_s", chain, 30))
            rows = fetch_all(conn, sql, (
                jdump(sorted(cohort.members)), chain.value, now - window_s * 1000, min_n,
                int(config.proven_flow_limit),
            ))
        except (sqlite3.Error, ValueError, TypeError) as exc:
            log.warning("scanner: proven-flow query failed on %s (%s)", chain.value, exc)
            continue
        for row in rows:
            token = str(row["token"] or "")
            nw, last_ms = int(row["nw"] or 0), int(row["last_ms"] or 0)
            if not token or is_quote_asset(chain, token):
                continue
            if now - last_ms > max_age_s * 1000:
                continue  # the lane's age gate would refuse it
            key = (db, chain.value, token)
            with _PROVEN_LOCK:
                offered = _PROVEN_OFFERED.get(key)
                if offered is not None and offered[0] >= nw:
                    continue  # nothing new since the last offer
                _PROVEN_OFFERED[key] = (nw, mono)
            _register_smart_flow_token(conn, chain, token, discovered_by="proven_flow")
            out.append(WorkItem(
                chain=chain, token=token, source="proven_flow", score=float(nw),
                extras={"proven_buyers": nw, "last_buy_ms": last_ms, "cohort_id": cohort.cohort_id},
            ))
            if len(out) >= n:
                break
    return out


def next_work(
    conn: sqlite3.Connection,
    n: int,
    *,
    queue: triage_mod.TriageQueue | None = None,
    config: ScanConfig = DEFAULT_CONFIG,
) -> list[WorkItem]:
    """Up to ``n`` candidates, in priority order across all four sources.

    Migrations first, because ``migration-fade``'s window is 180 s and closing. Then the
    in-process queue, which only has anything when ingest and tier 1 share a process. Then
    ``triage_decisions``, which is the same work made durable and is the only source that
    works when the two run as separate services.
    """
    q = queue if queue is not None else triage_mod.get_queue()
    work = _migration_work(conn, n, config=config)
    # Proven flow next. Rare by construction (a few tokens a day per chain) and its lane's
    # age gate is tight, so it goes ahead of the smart-flow slice, capped.
    if len(work) < n and config.proven_flow:
        work.extend(_proven_flow_work(conn, min(n - len(work), int(config.proven_flow_max)), config=config))
    # Smart flow next, and capped at a share of the batch. Its window (300 s) is nearly as
    # tight as a migration's, and it is the only source that can produce an sm-trenches
    # candidate -- but the launch sources below it feed pons-robinhood and the migration
    # lanes, so it takes a slice, never the batch.
    if len(work) < n and config.smart_flow:
        room = n - len(work)
        share = max(1, int(n * float(config.smart_flow_share)))
        work.extend(_smart_flow_work(conn, min(room, share), config=config))
    if len(work) < n:
        work.extend(_queue_work(q, n - len(work)))
    if len(work) < n:
        work.extend(_db_queue_work(conn, n - len(work), config=config))

    out: list[WorkItem] = []
    seen: set[tuple[str, str]] = set()
    for item in work:
        key = (item.chain.value, item.token)
        if key in seen:
            continue
        # A migration is always worth the pass: it is a new fact about the token, and it
        # is the one lane whose window we can actually hit.
        # Proven flow too: it only offers a token when MORE proven wallets are buying it
        # than at its last offer, and its lane's age gate is shorter than the cooldown --
        # waiting the cooldown out would be the same as never looking.
        if item.source not in ("migration", "proven_flow") and RECENT.is_recent(
            item.chain, item.token, config.rescan_cooldown_s
        ):
            continue
        seen.add(key)
        out.append(item)
    return out[:n]


# --------------------------------------------------------------------------------------
# service entry points
# --------------------------------------------------------------------------------------


def run_once(
    conn: sqlite3.Connection | None = None,
    *,
    limit: int | None = None,
    config: ScanConfig = DEFAULT_CONFIG,
    queue: triage_mod.TriageQueue | None = None,
    stats: ScanStats | None = None,
    tokens: Sequence[tuple[Chain, str]] | None = None,
) -> list[ScanResult]:
    """Drain up to ``limit`` candidates through tier 1 and return what happened.

    ``tokens`` bypasses the queue entirely, which is how ``kaiba scan once --token MINT``
    asks "what would the lanes say about this?" without waiting for the feed.
    """
    c = conn or get_conn()
    n = int(limit if limit is not None else config.batch)
    if tokens is not None:
        work = [WorkItem(chain=ch, token=tok, source="token") for ch, tok in tokens]
    else:
        work = next_work(c, n, queue=queue, config=config)
    if not work:
        return []

    results: list[ScanResult] = []
    if config.workers <= 1 or len(work) == 1:
        for item in work:
            results.append(
                scan(item.chain, item.token, c, config=config, source=item.source, extras=item.extras)
            )
    else:
        results.extend(_scan_parallel(work, config=config))
    if stats is not None:
        for r in results:
            stats.record(r)
    return results


def _scan_parallel(work: Sequence[WorkItem], *, config: ScanConfig) -> list[ScanResult]:
    """One sqlite connection per worker.

    ``db.connect`` opens with ``check_same_thread=False`` and autocommit, so sharing one
    handle across threads would *appear* to work and would interleave statements on a
    single cursor-less connection. A connection each is cheap and removes the question.
    """

    def _one(item: WorkItem) -> ScanResult:
        worker_conn = connect()
        try:
            return scan(
                item.chain,
                item.token,
                worker_conn,
                config=config,
                source=item.source,
                extras=item.extras,
            )
        finally:
            worker_conn.close()

    with ThreadPoolExecutor(max_workers=config.workers, thread_name_prefix="kaiba-tier1") as pool:
        return list(pool.map(_one, work))


def run_loop(
    conn: sqlite3.Connection | None = None,
    *,
    config: ScanConfig = DEFAULT_CONFIG,
    queue: triage_mod.TriageQueue | None = None,
    stop: threading.Event | None = None,
    max_seconds: float | None = None,
    max_tokens: int | None = None,
    on_result: Callable[[ScanResult], None] | None = None,
    stats: ScanStats | None = None,
) -> ScanStats:
    """Run tier 1 continuously. This is the service ``kaiba scan run`` starts.

    ``max_seconds`` and ``max_tokens`` exist so a measurement run is a normal call rather
    than a special mode — the live numbers in the work log were produced by this function,
    not by a script that approximates it.
    """
    c = conn or get_conn()
    counters = stats if stats is not None else ScanStats()
    started = time.monotonic()
    last_backpressure = 0.0
    q = queue if queue is not None else triage_mod.get_queue()

    while True:
        if stop is not None and stop.is_set():
            break
        if max_seconds is not None and (time.monotonic() - started) >= max_seconds:
            break
        if max_tokens is not None and (counters.scanned + counters.failed) >= max_tokens:
            break

        remaining = None
        if max_tokens is not None:
            remaining = max_tokens - (counters.scanned + counters.failed)
        batch = config.batch if remaining is None else max(1, min(config.batch, remaining))

        results = run_once(c, limit=batch, config=config, queue=q, stats=counters)
        for r in results:
            if on_result is not None:
                try:
                    on_result(r)
                except Exception as exc:  # noqa: BLE001 - a reporter must not stop the loop
                    log.debug("scanner: on_result raised (%s)", type(exc).__name__)

        now = time.monotonic()
        if now - last_backpressure >= config.backpressure_every_s:
            last_backpressure = now
            try:
                triage_mod.snapshot_backpressure(q, c)
            except Exception as exc:  # noqa: BLE001
                log.debug("scanner: backpressure snapshot failed (%s)", type(exc).__name__)

        if not results:
            if stop is not None:
                stop.wait(config.idle_sleep_s)
            else:
                time.sleep(config.idle_sleep_s)
    return counters


def scan_tokens(
    tokens: Iterable[str],
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    config: ScanConfig = DEFAULT_CONFIG,
    stats: ScanStats | None = None,
) -> list[ScanResult]:
    """Tier 1 over an explicit list. The operator's "what about these?" entry point."""
    pairs = [(chain, t) for t in tokens if t]
    if not pairs:
        return []
    return run_once(conn, config=config, stats=stats, tokens=pairs)


def lane_silence_report(results: Sequence[ScanResult]) -> dict[str, Any]:
    """Why nothing fired, in the terms someone would ask the question.

    Zero signals is the expected outcome today and it is a *diagnosis*, not a number:
    ``curve-velocity`` needs a bundler share that is GMGN-Plus-only, ``confluence-5`` and
    ``sm-trenches`` need graded wallets and no wallet has a grade yet, ``kol-fade`` needs
    ``caller_calls`` rows. This aggregates what the pass actually observed so the reason is
    read off the run rather than guessed at afterwards.
    """
    total = len(results)
    ok = [r for r in results if r.ok]
    fired = [r for r in ok if r.signals]
    curve_notes: dict[str, int] = {}
    blockers: dict[str, int] = {}
    grades: dict[str, int] = {}
    for r in ok:
        if not r.curve_ok and r.curve_note:
            key = r.curve_note.split(":", 1)[0]
            curve_notes[key] = curve_notes.get(key, 0) + 1
        for b in r.dossier_blockers:
            blockers[b] = blockers.get(b, 0) + 1
        if r.dossier_grade is not None:
            grades[r.dossier_grade.value] = grades.get(r.dossier_grade.value, 0) + 1
    return {
        "scanned": total,
        "ok": len(ok),
        "failed": total - len(ok),
        "with_signal": len(fired),
        "signals": sum(len(r.signals) for r in ok),
        "lanes": sorted({lane for r in ok for lane in r.lanes_fired}),
        "curve_ok": sum(1 for r in ok if r.curve_ok),
        "curve_missing_reasons": dict(sorted(curve_notes.items())),
        "dossier_grades": dict(sorted(grades.items())),
        "dossier_blockers": dict(sorted(blockers.items())),
        "with_recent_buys": sum(1 for r in ok if r.recent_buys),
        "lanes_available": [lane.value for lane in Lane if lane is not Lane.MANUAL],
    }


__all__ = [
    "CURVE_URL",
    "DEFAULT_CONFIG",
    "EVENT_SCANNED",
    "EVENT_SCAN_FAILED",
    "CLASSIC_GRADUATION_SOL",
    "CLASSIC_REAL_TOKEN_ATOMS",
    "CLASSIC_VIRTUAL_SOL_LAMPORTS",
    "CLASSIC_VIRTUAL_TOKEN_ATOMS",
    "MIGRATION_WATERMARK_KEY",
    "PARAMS_VERSION",
    "RECENT",
    "RESERVED_TOKEN_ATOMS",
    "SOL_QUOTE_MINTS",
    "TRIAGE_WATERMARK_KEY",
    "ScanConfig",
    "ScanResult",
    "ScanStats",
    "WorkItem",
    "build_context",
    "curve_from_payload",
    "fetch_curve_payload",
    "lane_silence_report",
    "load_caller",
    "load_recent_buys",
    "load_token_meta",
    "measure_rate",
    "next_work",
    "run_loop",
    "run_once",
    "scan",
    "scan_tokens",
    "swap_count_if_covered",
]
