"""Tier 0: the sub-100 ms screen that decides whether a launch deserves 7.6 seconds.

Measured on this machine on 2026-09-20 (``docs/AUDIT-2026-09-20.md``): pump.fun emits
**14 launches a minute** and a full DYOR safety scan takes **7.6 seconds**, so tier 1 can
absorb roughly **8 a minute**. Without a screen in front of it we do not see most of the
market, and we spend the provider budget on whatever happened to arrive first rather than
on whatever was worth looking at. First-come-first-served is the worst possible ordering
of a queue you cannot finish.

So: two tiers.

* **Tier 0** — this module. A decision on the event payload plus our own tables, with
  **no network calls at all**. That is not an optimisation, it is the definition: the
  moment tier 0 dials out it inherits tier 1's latency and stops being a screen.
  ``tests/test_triage.py::test_tier0_makes_no_network_calls`` patches the socket layer to
  explode and is the enforcement.
* **Tier 1** — ``kaiba.intelligence.dyor.scan_token``, fed from :class:`TriageQueue` in
  priority order rather than arrival order.

## Three verdicts, and why ``defer`` is load-bearing

``reject`` / ``defer`` / ``promote``. Most launches are neither obviously bad nor
obviously interesting, and the honest answer for those is "not now, revisit if it does
something" — not "no". Collapsing ``defer`` into ``reject`` would turn an absence of
information into an exclusion, which is how a screen quietly becomes a blindfold.

## The rule that matters most: tier 0 cannot reject on absent data

Every reject rule declares the facts it needs (:attr:`RejectRule.requires`) and is
**skipped** when any of them could not be read. "We do not know" is always ``defer``.
This is structural rather than a convention, because the failure it prevents is silent:
if PumpPortal renames ``mint`` to ``mintAddress``, a field-presence-based screen rejects
the entire market and nothing in the logs says so. With this design the same breakage
shows up as *100% defer, zero reject* in :func:`verdict_split`, which is loud.

## Where the capacity control actually lives

The verdict is evidence-driven and says nothing about how busy we are. Capacity is the
queue's job: :class:`TriageQueue` is bounded, ranks ``promote`` above ``defer`` and then
by score, sheds the worst tail instead of growing, and reports how far behind it is. So a
burst cannot flood tier 1, and — deliberately — the best-scoring ``defer`` entries are
still admitted behind the promotes so that idle tier-1 capacity is spent on the best of a
boring minute rather than wasted. Set ``admit_defers=False`` if you want a strict screen.

## Evidence, and the lack of it

Direction of travel for the exclusion rules is supported:

* Metadata / image de-duplication is the best-evidenced cheap variable we have access to:
  originals graduate at **9.20%**, copycats at **0.86%**, a 10.7x separation
  (``docs/EDGE-AND-VARIABLES.md`` §1 #1, CCS'26).
* Creator history is informative when present and uninformative when absent — a
  per-address dev-history lookup is defeated 55% of the time (§1 #4), which is precisely
  why "creator unknown" scores neutral here and never negative.
* "The dev bought his own bundle" has a **98.7%** base rate (§4 #16), so the *presence* of
  a dev buy carries no information. Only its size might, and that is untested.

**Every numeric threshold in :class:`TriageConfig` is invented unless its entry in
:data:`THRESHOLD_PROVENANCE` cites something.** ``tests/test_triage.py`` asserts that the
two stay in sync, so a new knob cannot be added without declaring where it came from.
There is already a lane full of unmarked guesses; this is not a second one.
"""

from __future__ import annotations

import bisect
import logging
import sqlite3
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from kaiba.core.db import fetch_one, get_conn, jdump, jload
from kaiba.core.events import emit
from kaiba.core.schemas import (
    EventKind,
    HARD_QUARANTINE_TAGS,
    Chain,
    Token,
    digest,
    looks_solana,
    now_ms,
)

log = logging.getLogger(__name__)

PARAMS_VERSION = "triage-v1"

#: Event-bus kinds, declared in ``schemas.EventKind`` since 2026-09-22 (they were raw strings
#: no ``Event``-typed reader could load). Kept as ``.value`` so comparisons to rows stay str.
EVENT_VERDICT = EventKind.TRIAGE_VERDICT.value
EVENT_BACKPRESSURE = EventKind.TRIAGE_BACKPRESSURE.value

COHORT_BLACKLIST = "blacklist"
COHORT_TRUSTED = "trusted_copy"
COHORT_TRACKED = "tracked"

#: What :func:`parse_launch` records when the payload names no launchpad.
#:
#: This line used to read ``"pump.fun" if chain is Chain.SOL else None``, which meant a
#: Solana token that named no launchpad -- the exact shape of a manually deployed
#: contract, and of anything a market-wide feed surfaces -- was filed under a launchpad
#: it had never touched. That is fabricated provenance: a later reader cannot tell it
#: from a real pump.fun launch, and the whole reject/defer machinery below is built on
#: never turning an absence into a claim.
#:
#: It is a sentinel, not a default. It is written ONLY into ``triage_decisions``, which
#: is the record of a decision and must say on what it was taken; ``tokens.launchpad``
#: stays NULL, because that column is the token's own provenance and we do not know it.
#: An unknown launchpad is data: the token is registered, screened, and deferred to DYOR
#: like any other, because "we have not heard of this venue" is not evidence of anything.
LAUNCHPAD_UNKNOWN = "unknown"


class Verdict(StrEnum):
    """Tier-0 outcome.

    ``DEFER`` is not a soft reject. It means "we learned nothing that justifies spending
    tier 1 on this right now"; the token stays eligible and a later event (a trade, a
    caller mention, a migration) can bring it back.
    """

    REJECT = "reject"
    DEFER = "defer"
    PROMOTE = "promote"


#: Sort rank used by the queue. Promotes always outrank defers regardless of score.
VERDICT_RANK: dict[Verdict, int] = {Verdict.REJECT: -1, Verdict.DEFER: 0, Verdict.PROMOTE: 1}


# --------------------------------------------------------------------------------------
# configuration — every number here is declared in THRESHOLD_PROVENANCE below
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TriageConfig:
    """Tier-0 knobs. Construct a copy with :func:`dataclasses.replace` to tune."""

    promote_threshold: float = 0.55
    unknown_prior: float = 0.20

    reject_min_creator_rugs: int = 2
    reject_min_image_reuses: int = 1
    reject_min_name_reuses: int = 3
    reject_min_copycat_confidence: float = 0.80

    serial_launcher_min_launches: int = 5
    soft_copycat_min_confidence: float = 0.50

    w_creator_prior_graduate: float = 0.45
    w_creator_repeat_graduate: float = 0.15
    w_creator_trusted_cohort: float = 0.30
    w_creator_tracked_cohort: float = 0.10
    w_creator_soft_rug: float = -0.25
    w_creator_serial_launcher: float = -0.20
    w_name_reused: float = -0.25
    w_copycat_suspected: float = -0.30
    w_metadata_incomplete: float = -0.05
    w_dev_buy_max: float = 0.10

    dev_buy_saturation_lamports: int = 2_000_000_000

    dedup_budget_us: int = 40_000
    latency_budget_us: int = 100_000
    latency_window: int = 4096


#: Where each :class:`TriageConfig` number came from. ``INVENTED`` means exactly that:
#: nobody has backtested it, it is a starting point, and it should be treated as a
#: hypothesis rather than a finding. The test suite asserts these keys match the
#: dataclass fields exactly, so a silently-added knob fails the build.
THRESHOLD_PROVENANCE: dict[str, str] = {
    "promote_threshold": (
        "INVENTED. Chosen so that a launch about which we know nothing lands in defer, "
        "and one distinguishing positive fact (a creator who has graduated something "
        "before) is enough to promote. No backtest."
    ),
    "unknown_prior": (
        "INVENTED. The score of a launch with no distinguishing facts. Deliberately well "
        "below promote_threshold: unknown must not read as interesting."
    ),
    "reject_min_creator_rugs": (
        "INVENTED count. Direction supported by docs/EDGE-AND-VARIABLES.md §1 #4 (creator "
        "history is informative when present). Two rather than one because a single "
        "'rugged' label in our own table has never been validated against an outcome."
    ),
    "reject_min_image_reuses": (
        "EVIDENCED direction, INVENTED count. CCS'26 via docs/EDGE-AND-VARIABLES.md §1 #1: "
        "originals graduate at 9.20%, copycats at 0.86%, a 10.7x separation. The count of "
        "1 (reject the second use of an image or metadata URI) is the strictest reading "
        "of that and is a guess."
    ),
    "reject_min_name_reuses": (
        "INVENTED. Name+symbol collision is a much weaker copycat tell than an image hash "
        "(generic names recur legitimately), so it needs more repeats before it rejects."
    ),
    "reject_min_copycat_confidence": (
        "INVENTED. Confidence floor at which kaiba.intelligence.dedup's own classification "
        "is allowed to reject on its own. The scale that module will use is not yet known "
        "to this one, so this number is a placeholder to be re-set when dedup lands."
    ),
    "serial_launcher_min_launches": (
        "INVENTED. No published study establishes a launch-count threshold. Five is a "
        "guess and it only ever produces a score penalty, never a rejection."
    ),
    "soft_copycat_min_confidence": (
        "INVENTED. The floor above which dedup's copycat call becomes a score penalty "
        "rather than nothing. Placeholder, same caveat as reject_min_copycat_confidence."
    ),
    "w_creator_prior_graduate": (
        "INVENTED weight, and the single largest one here. Direction supported by EDGE §1 "
        "#4; the size is a guess sized so that this one fact alone clears the promote bar."
    ),
    "w_creator_repeat_graduate": (
        "INVENTED weight. Extra credit for a creator with more than one graduate, on the "
        "untested assumption that two is meaningfully better evidence than one."
    ),
    "w_creator_trusted_cohort": (
        "INVENTED weight. Note the audit's warning: our cohorts derive from GMGN labels "
        "whose method is unpublished, so this is a weak input dressed as a strong one."
    ),
    "w_creator_tracked_cohort": (
        "INVENTED weight, small. 'Tracked' only means someone once added the wallet to a "
        "watch list, which is a statement about us rather than about the wallet."
    ),
    "w_creator_soft_rug": (
        "INVENTED weight, applied for prior rugs below the rejection bar. Our own 'rugged' "
        "label has never been validated against an outcome, so this is a guess on a guess."
    ),
    "w_creator_serial_launcher": (
        "INVENTED weight. Direction is the common belief that churn is adverse; no study "
        "we hold measures it, and EDGE §1 #4 notes address-level dev history is evadable."
    ),
    "w_name_reused": (
        "INVENTED weight. Direction from the CCS'26 copycat separation (EDGE §1 #1), but "
        "that study hashes images, not names, so the transfer to names is an assumption."
    ),
    "w_copycat_suspected": (
        "INVENTED weight. Direction from CCS'26 (EDGE §1 #1): copycats graduate at 0.86% "
        "against 9.20% for originals. The magnitude of the penalty is still a guess."
    ),
    "w_metadata_incomplete": (
        "INVENTED weight, deliberately tiny. Missing metadata must never approach a "
        "rejection: an absent field is far more likely to be our parser than their scam."
    ),
    "w_dev_buy_max": (
        "INVENTED and UNTESTED. EDGE §4 #16: the dev buying his own bundle has a 98.7% "
        "base rate, so presence carries no information at all. Only size might, and no "
        "study we have says it does. Kept small and first in line to be deleted."
    ),
    "dev_buy_saturation_lamports": (
        "INVENTED. 2 SOL, the point above which a larger dev buy stops moving the score. "
        "Saturating rather than linear so one whale buy cannot dominate the ordering."
    ),
    "dedup_budget_us": (
        "DERIVED from the 100 ms tier-0 requirement. Raised from 15 ms to 40 ms after a "
        "live run: classify_meta measured 26 ms and tripped the breaker, which silently "
        "switched off the best-evidenced cheap filter we have (9.20% vs 0.86% "
        "graduation) for the rest of every ingest process. Measured tier-0 median is "
        "0.61 ms, so 40 ms leaves ample headroom and 26 ms is plainly affordable. The "
        "breaker now recovers after a cooldown rather than latching, because one slow "
        "call is a hiccup and permanently losing the filter is a worse outcome than "
        "occasionally paying for a slow one."
    ),
    "latency_budget_us": (
        "MEASURED requirement, not a model parameter: tier 0 must stay under 100 ms at p99 "
        "to be worth having in front of a 7.6 s scan."
    ),
    "latency_window": (
        "Implementation detail, not a model parameter: the ring-buffer size behind the "
        "latency histogram. 4096 samples is about five minutes at the measured 14/min."
    ),
}

DEFAULT_CONFIG = TriageConfig()


# --------------------------------------------------------------------------------------
# sibling module landing in parallel: kaiba/intelligence/dedup.py
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CopycatCall:
    """Normalised view of whatever ``dedup`` decides to return."""

    available: bool
    is_copycat: bool | None = None
    confidence: float | None = None
    detail: str | None = None


#: ``dedup.classify_meta`` is documented as "the verdict for metadata we already hold",
#: with ``dedup.classify`` as its *fetching* wrapper. Measured on 2026-09-20, ``classify``
#: costs **1.9 seconds** per call because it pulls the IPFS metadata JSON and, optionally,
#: the image. That is a tier-1 price. Tier 0 binds to the local one and to nothing that
#: might dial out, which is why plain ``classify`` is deliberately absent from both lists.
_DEDUP_LOCAL_FUNCS: tuple[str, ...] = ("classify_meta",)
_DEDUP_FALLBACK_FUNCS: tuple[str, ...] = ("classify_copycat", "copycat", "lookup_copycat")

_DEDUP_PROBE_INTERVAL_S = 30.0


@dataclass
class _DedupBinding:
    fn: Callable[..., Any]
    name: str
    local: bool
    meta_cls: Any = None


#: How long a breaker trip lasts before tier 0 will try the sibling again. A latch
#: for the life of the process meant one slow call cost us the filter until restart.
DEDUP_COOLDOWN_S = 300.0
_DEDUP: dict[str, Any] = {
    "binding": None, "probed_at": 0.0, "disabled_reason": None, "disabled_at": 0.0,
    "trips": 0,
}


def reset_dedup() -> None:
    """Forget the binding and re-arm the breaker. Tests and long-lived processes use this."""
    _DEDUP.update(binding=None, probed_at=0.0, disabled_reason=None, disabled_at=0.0, trips=0)


def disable_dedup(reason: str) -> None:
    """Disconnect the sibling for a cooldown, loudly.

    Was a permanent latch. A single over-budget call then cost us the copycat filter for
    the life of the process, which on a long-running ingest service means forever, and
    silently — the log line scrolls past once and the filter is never heard from again.
    A trip is now temporary and counted, so a genuinely broken sibling still shows up as
    a rising trip count rather than one forgotten warning.
    """
    if _DEDUP["disabled_reason"] is None:
        log.warning("triage: dedup disconnected from tier 0 for %.0fs — %s",
                    DEDUP_COOLDOWN_S, reason)
    _DEDUP.update(
        binding=None, disabled_reason=reason, disabled_at=time.monotonic(),
        trips=int(_DEDUP.get("trips") or 0) + 1,
    )


def dedup_status() -> dict[str, Any]:
    binding = _DEDUP["binding"]
    return {
        "bound": None if binding is None else binding.name,
        "local": None if binding is None else binding.local,
        "disabled_reason": _DEDUP["disabled_reason"],
        "trips": int(_DEDUP.get("trips") or 0),
        "cooldown_s": DEDUP_COOLDOWN_S,
    }


#: Alias for :func:`reset_dedup`, kept because the binding cache is process-wide and any
#: other test module that monkeypatches ``dedup`` must clear it or it leaks into the next
#: file's expectations. Caching the probe is correct in production — re-running the import
#: finder on every launch at 14 a minute is real money — so the fix is to reset, not to
#: stop caching.
reset_dedup_probe = reset_dedup


def _load_dedup() -> _DedupBinding | None:
    """Bind ``dedup``'s *local* classifier, re-probing at most every 30 s.

    The module was written concurrently with this one, so this must work before it
    exists and improve once it lands. Two details are not optional:

    * **The probe is cached.** A failing import is not cached by Python, and re-running
      the finder on every launch is real money at 14 a minute.
    * **Only non-fetching entry points are eligible.** Anything unexpected is treated as
      "no opinion", never as an opinion of "bad": a half-landed sibling must not start
      rejecting the market.
    """
    if _DEDUP["disabled_reason"] is not None:
        since = time.monotonic() - float(_DEDUP.get("disabled_at") or 0.0)
        if since < DEDUP_COOLDOWN_S:
            return None
        log.info("triage: retrying dedup after %.0fs cooldown", since)
        _DEDUP.update(disabled_reason=None, disabled_at=0.0, probed_at=0.0)
    binding = _DEDUP["binding"]
    if binding is not None:
        return binding
    now = time.monotonic()
    if _DEDUP["probed_at"] and (now - _DEDUP["probed_at"]) < _DEDUP_PROBE_INTERVAL_S:
        return None
    _DEDUP["probed_at"] = now
    try:
        from kaiba.intelligence import dedup
    except ImportError:
        return None
    except Exception as exc:  # noqa: BLE001 — a broken sibling must not take tier 0 down
        log.warning("triage: kaiba.intelligence.dedup failed to import (%s)", type(exc).__name__)
        return None
    meta_cls = getattr(dedup, "TokenMeta", None)
    for name in _DEDUP_LOCAL_FUNCS:
        fn = getattr(dedup, name, None)
        if callable(fn) and meta_cls is not None:
            found = _DedupBinding(fn=fn, name=name, local=True, meta_cls=meta_cls)
            _DEDUP["binding"] = found
            return found
    for name in _DEDUP_FALLBACK_FUNCS:
        fn = getattr(dedup, name, None)
        if callable(fn):
            found = _DedupBinding(fn=fn, name=name, local=False)
            _DEDUP["binding"] = found
            return found
    return None


def _normalise_copycat(raw: Any) -> CopycatCall:
    """Accept a bool, a float, a string, a dict or an object with the obvious attributes."""
    if raw is None:
        return CopycatCall(available=False)
    if isinstance(raw, bool):
        return CopycatCall(available=True, is_copycat=raw, confidence=1.0 if raw else 0.0)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        conf = float(raw)
        return CopycatCall(available=True, is_copycat=conf >= 0.5, confidence=conf)
    if isinstance(raw, str):
        flag = raw.strip().lower() in {"copycat", "duplicate", "copy", "clone"}
        return CopycatCall(available=True, is_copycat=flag, confidence=1.0 if flag else 0.0, detail=raw)
    if isinstance(raw, tuple) and raw:
        head = _normalise_copycat(raw[0])
        if len(raw) > 1 and isinstance(raw[1], (int, float)) and not isinstance(raw[1], bool):
            return replace(head, confidence=float(raw[1]))
        return head
    getter: Callable[[str], Any]
    if isinstance(raw, Mapping):
        getter = lambda k: raw.get(k)  # noqa: E731
    else:
        getter = lambda k: getattr(raw, k, None)  # noqa: E731
    flag = getter("is_copycat")
    if flag is None:
        flag = getter("copycat")
    conf = getter("confidence")
    if conf is None:
        conf = getter("score")
    label = getter("classification") or getter("label") or getter("verdict") or getter("status")
    label = str(label) if label is not None else None
    if flag is None and label is not None:
        flag = label.strip().lower() in {"copycat", "duplicate", "copy", "clone"}
    if label is not None and label.strip().lower() in {"unknown", "unscored", "none"}:
        # The classifier says it has no opinion. That is an unknown, not an acquittal —
        # collapsing it into "not a copycat" would claim knowledge we were just denied.
        return CopycatCall(available=False, detail=label)
    if flag is None and conf is None:
        return CopycatCall(available=False)
    try:
        conf_f = float(conf) if conf is not None else None
    except (TypeError, ValueError):
        conf_f = None
    if conf_f is None and flag is not None:
        # An explicit verdict with no confidence attached is taken at face value. Picking
        # a hedge on the classifier's behalf would be inventing a number, and the
        # alternative — silently discarding an explicit "this is a copy" — is worse.
        conf_f = 1.0 if flag else 0.0
    return CopycatCall(
        available=True,
        is_copycat=bool(flag) if flag is not None else None,
        confidence=conf_f,
        detail=label,
    )


def _dedup_attempts(
    binding: _DedupBinding,
    facts: LaunchFacts,
    conn: sqlite3.Connection | None,
    register_index: bool,
) -> tuple[tuple[tuple[Any, ...], dict[str, Any]], ...]:
    if binding.local and binding.meta_cls is not None:
        meta = binding.meta_cls(
            mint=facts.mint,
            chain=facts.chain,
            name=facts.name,
            symbol=facts.symbol,
            description=facts.description,
            image_uri=facts.image_uri,
            metadata_uri=facts.metadata_uri,
            # PumpPortal's creation frames carry no timestamp (34/34 on a live sample,
            # 2026-09-20), so fall back to our receive time the way the listener already
            # does. dedup orders originals by this, and on a live feed arrival order is
            # creation order; it also starts dedup's coverage window, without which its
            # only honest verdict is UNKNOWN forever.
            created_ms=facts.event_ms or facts.observed_ms,
            creator=facts.creator,
        )
        return (
            ((meta,), {"conn": conn, "register_mint": register_index}),
            ((meta,), {"conn": conn}),
            ((meta,), {}),
        )
    return (
        ((facts.mint,), {"chain": facts.chain, "conn": conn}),
        ((facts.mint, facts.chain), {"conn": conn}),
        ((facts.mint,), {"chain": facts.chain}),
        ((facts.mint,), {}),
    )


def _ask_dedup(
    facts: LaunchFacts,
    conn: sqlite3.Connection | None,
    config: TriageConfig | None = None,
    register_index: bool = False,
) -> CopycatCall:
    """Ask the sibling, under a hard time budget.

    The budget is not defensive programming, it is the contract: tier 0 promises a
    decision in under 100 ms and cannot keep that promise while delegating to a module
    another team is still changing. ``dedup.classify`` measured **1.9 s** on 2026-09-20
    because it fetches IPFS metadata. One call over budget disconnects dedup for the rest
    of the process and says so, rather than quietly turning tier 0 into tier 1.
    """
    cfg = config or DEFAULT_CONFIG
    binding = _load_dedup()
    if binding is None or facts.mint is None:
        return CopycatCall(available=False)
    started = time.perf_counter_ns()
    result: Any = None
    called = False
    try:
        for args, kwargs in _dedup_attempts(binding, facts, conn, register_index):
            try:
                result = binding.fn(*args, **kwargs)
                called = True
                break
            except TypeError:
                continue  # wrong signature, try the next shape
    except Exception as exc:  # noqa: BLE001 — never let a sibling's bug reject a token
        log.warning("triage: dedup raised (%s: %s)", type(exc).__name__, exc)
        return CopycatCall(available=False)
    elapsed_us = (time.perf_counter_ns() - started) // 1000
    if elapsed_us > cfg.dedup_budget_us:
        disable_dedup(
            f"{binding.name} took {elapsed_us / 1000:.0f} ms against a "
            f"{cfg.dedup_budget_us / 1000:.0f} ms tier-0 budget"
        )
    if not called:
        return CopycatCall(available=False)
    return _normalise_copycat(result)


# --------------------------------------------------------------------------------------
# payload -> facts
# --------------------------------------------------------------------------------------

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)


def _first(payload: Mapping[str, Any], *keys: str) -> Any:
    for k in keys:
        v = payload.get(k)
        if v is not None and v != "":
            return v
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _norm_text(value: Any) -> str | None:
    """Casefold, strip zero-width tricks, collapse whitespace, NFKC-normalise.

    NFKC matters here: homoglyph and full-width substitutions are the cheapest way to make
    a copy of a name look distinct to a naive comparison.
    """
    s = _text(value)
    if s is None:
        return None
    s = unicodedata.normalize("NFKC", s).translate(_ZERO_WIDTH)
    s = " ".join(s.split()).casefold()
    return s or None


def _lamports(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int((Decimal(str(value)) * 1_000_000_000).to_integral_value())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _event_ms(payload: Mapping[str, Any]) -> int | None:
    raw = _first(payload, "timestamp", "blockTime", "block_time", "ts", "time", "created_ms")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value > 1e14:
        return int(value / 1000)
    if value > 1e11:
        return int(value)
    return int(value * 1000)


@dataclass(frozen=True, slots=True)
class LaunchFacts:
    """Everything tier 0 can read off the wire, plus a list of what it could not."""

    chain: Chain = Chain.SOL
    mint: str | None = None
    creator: str | None = None
    name: str | None = None
    symbol: str | None = None
    description: str | None = None
    image_uri: str | None = None
    metadata_uri: str | None = None
    dev_buy_lamports: int | None = None
    #: Provider-stated creation time. ``None`` is common: PumpPortal's ``subscribeNewToken``
    #: frames carried no timestamp at all on a 34/34 live sample (2026-09-20).
    event_ms: int | None = None
    #: When *we* saw it. Always known, never confused with the above.
    observed_ms: int = 0
    launchpad: str | None = None
    pool: str | None = None
    signature: str | None = None
    slot: int | None = None
    source: str = "pumpportal"
    unknowns: tuple[str, ...] = ()

    @property
    def name_fingerprint(self) -> str | None:
        n, s = _norm_text(self.name), _norm_text(self.symbol)
        if n is None and s is None:
            return None
        return digest({"n": n, "s": s})

    @property
    def image_fingerprint(self) -> str | None:
        ref = _norm_text(self.image_uri) or _norm_text(self.metadata_uri)
        return digest({"i": ref}) if ref else None


_FACT_FIELDS: tuple[str, ...] = (
    "mint",
    "creator",
    "name",
    "symbol",
    "description",
    "image_uri",
    "metadata_uri",
    "dev_buy_lamports",
    "event_ms",
)


def parse_launch(event: Mapping[str, Any] | Token | LaunchFacts) -> LaunchFacts:
    """Normalise a creation event. Never raises, never invents a value.

    Accepts a raw PumpPortal frame, a :class:`~kaiba.core.schemas.Token` (what
    ``kaiba.ingest.pumpportal.parse_new_token`` already produced), or a
    :class:`LaunchFacts` that has been built by hand. Anything it cannot read lands in
    ``unknowns`` rather than getting a default, because a default that reads as safe is
    the bug this whole codebase is organised against (``docs/CONTRACT.md`` rule 2).
    """
    if isinstance(event, LaunchFacts):
        return event if event.observed_ms else replace(event, observed_ms=now_ms())
    if isinstance(event, Token):
        meta = event.meta or {}
        payload: dict[str, Any] = {
            "mint": event.address,
            "chain": event.chain,
            "name": event.name,
            "symbol": event.symbol,
            "creator": event.creator,
            "timestamp": event.created_ms,
            "launchpad": event.launchpad,
            **{k: v for k, v in meta.items() if v is not None},
        }
    elif isinstance(event, Mapping):
        payload = dict(event)
    else:
        return LaunchFacts(observed_ms=now_ms(), unknowns=tuple(_FACT_FIELDS))

    chain_raw = payload.get("chain")
    chain = Chain.SOL
    if isinstance(chain_raw, Chain):
        chain = chain_raw
    elif isinstance(chain_raw, str):
        try:
            chain = Chain(chain_raw)
        except ValueError:
            chain = Chain.SOL

    mint_raw = _text(_first(payload, "mint", "mintAddress", "mint_address", "ca", "address", "token"))
    mint = mint_raw if (mint_raw and (chain is not Chain.SOL or looks_solana(mint_raw))) else None

    creator_raw = _first(payload, "traderPublicKey", "trader_public_key", "creator", "dev", "user")
    creator = _text(creator_raw)

    dev_buy = _lamports(_first(payload, "solAmount", "sol_amount"))
    if dev_buy is None:
        dev_buy = _int_or_none(_first(payload, "initial_buy_lamports", "initialBuyLamports"))

    facts = LaunchFacts(
        chain=chain,
        mint=mint,
        creator=creator,
        name=_text(payload.get("name")),
        symbol=_text(payload.get("symbol")),
        description=_text(_first(payload, "description", "desc")),
        image_uri=_text(_first(payload, "image", "imageUri", "image_uri", "imageUrl")),
        metadata_uri=_text(_first(payload, "uri", "metadataUri", "metadata_uri")),
        dev_buy_lamports=dev_buy,
        event_ms=_event_ms(payload),
        observed_ms=now_ms(),
        launchpad=_text(payload.get("launchpad")) or LAUNCHPAD_UNKNOWN,
        pool=_text(_first(payload, "bondingCurveKey", "bonding_curve", "pool")),
        signature=_text(_first(payload, "signature", "tx")),
        slot=_int_or_none(payload.get("slot")),
        source=_text(payload.get("source")) or "pumpportal",
    )
    unknowns = tuple(f for f in _FACT_FIELDS if getattr(facts, f) is None)
    return replace(facts, unknowns=unknowns)


# --------------------------------------------------------------------------------------
# what our own tables already know
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DbFacts:
    """Read-only view of what the local database knows. No network, by construction."""

    creator_known: bool = False
    creator_launches: int | None = None
    creator_graduated: int | None = None
    creator_rugged: int | None = None
    creator_cohort: str | None = None
    creator_tags: tuple[str, ...] = ()
    funder: str | None = None
    funder_cohort: str | None = None
    image_reuses: int | None = None
    name_reuses: int | None = None
    copycat: CopycatCall = field(default_factory=lambda: CopycatCall(available=False))
    unknowns: tuple[str, ...] = ()

    @property
    def creator_blacklisted(self) -> bool:
        return self.creator_cohort == COHORT_BLACKLIST

    @property
    def funder_blacklisted(self) -> bool:
        return self.funder_cohort == COHORT_BLACKLIST

    @property
    def creator_quarantine_tags(self) -> tuple[str, ...]:
        hard = {t.value for t in HARD_QUARANTINE_TAGS}
        return tuple(t for t in self.creator_tags if t in hard)


def _fingerprint_hits(conn: sqlite3.Connection, chain: Chain, fp: str | None, kind: str) -> int | None:
    if fp is None:
        return None
    row = fetch_one(
        conn,
        "SELECT hits FROM triage_fingerprints WHERE chain=? AND fingerprint=? AND kind=?",
        (chain.value, fp, kind),
    )
    return int(row["hits"]) if row else 0


def read_db_facts(
    facts: LaunchFacts,
    conn: sqlite3.Connection | None = None,
    *,
    config: TriageConfig = DEFAULT_CONFIG,
    register_index: bool = False,
) -> DbFacts:
    """At most five indexed reads. Any failure becomes an unknown, never a default.

    Note what is *not* here: no provider call, no RPC, no metadata fetch. If you are
    tempted to add one, you are writing tier 1.

    ``register_index`` is the one write this path can make, and only ``screen_launch``
    turns it on: it lets ``dedup`` record the launch's fingerprints while it classifies
    them, in the same call. Without it dedup's coverage window never starts and its only
    honest verdict is UNKNOWN forever.
    """
    unknowns: list[str] = []
    try:
        c = conn or get_conn()
    except Exception as exc:  # noqa: BLE001 — no database is a reason to defer, not to reject
        log.warning("triage: no database (%s: %s); everything defers", type(exc).__name__, exc)
        return DbFacts(unknowns=("creator_history", "creator_cohort", "funder", "fingerprints"))

    creator_known = False
    launches = graduated = rugged = None
    cohort: str | None = None
    tags: tuple[str, ...] = ()
    funder: str | None = None
    funder_cohort: str | None = None

    if facts.creator:
        try:
            row = fetch_one(
                c,
                "SELECT launches, graduated, rugged FROM creators WHERE chain=? AND address=?",
                (facts.chain.value, facts.creator),
            )
            if row:
                creator_known = True
                launches = _int_or_none(row["launches"])
                graduated = _int_or_none(row["graduated"])
                rugged = _int_or_none(row["rugged"])
            else:
                # No curated row yet. `tokens` is indexed on (chain, creator) and is the
                # only other place a prior launch is recorded. Bounded so a creator with
                # a thousand launches cannot cost us the latency budget.
                seen = fetch_one(
                    c,
                    "SELECT COUNT(*) AS n FROM (SELECT 1 FROM tokens WHERE chain=? AND creator=? "
                    "LIMIT 64)",
                    (facts.chain.value, facts.creator),
                )
                if seen is not None:
                    launches = _int_or_none(seen["n"])
                    creator_known = bool(launches)
        except sqlite3.Error as exc:
            log.warning("triage: creator history unreadable (%s)", exc)
            unknowns.append("creator_history")

        try:
            wrow = fetch_one(
                c,
                "SELECT cohort, tags_json, first_funder FROM wallets WHERE chain=? AND address=?",
                (facts.chain.value, facts.creator),
            )
            if wrow:
                cohort = _text(wrow["cohort"])
                raw_tags = jload(wrow["tags_json"], [])
                tags = tuple(str(t) for t in raw_tags) if isinstance(raw_tags, list) else ()
                funder = _text(wrow["first_funder"])
        except sqlite3.Error as exc:
            log.warning("triage: creator cohort unreadable (%s)", exc)
            unknowns.append("creator_cohort")

        if funder:
            try:
                frow = fetch_one(
                    c,
                    "SELECT cohort FROM wallets WHERE chain=? AND address=?",
                    (facts.chain.value, funder),
                )
                funder_cohort = _text(frow["cohort"]) if frow else None
            except sqlite3.Error as exc:
                log.warning("triage: funder cohort unreadable (%s)", exc)
                unknowns.append("funder_cohort")
    else:
        unknowns.append("creator")

    image_reuses = name_reuses = None
    try:
        image_reuses = _fingerprint_hits(c, facts.chain, facts.image_fingerprint, "image")
        name_reuses = _fingerprint_hits(c, facts.chain, facts.name_fingerprint, "name")
    except sqlite3.Error as exc:
        log.warning("triage: fingerprint index unreadable (%s)", exc)
        unknowns.append("fingerprints")

    copycat = _ask_dedup(facts, c, config, register_index)
    if not copycat.available:
        unknowns.append("copycat_classification")

    return DbFacts(
        creator_known=creator_known,
        creator_launches=launches,
        creator_graduated=graduated,
        creator_rugged=rugged,
        creator_cohort=cohort,
        creator_tags=tags,
        funder=funder,
        funder_cohort=funder_cohort,
        image_reuses=image_reuses,
        name_reuses=name_reuses,
        copycat=copycat,
        unknowns=tuple(unknowns),
    )


# --------------------------------------------------------------------------------------
# reject rules
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RejectRule:
    """A rule that may reject, and the facts it is not allowed to guess.

    ``requires`` is the whole point. :func:`evaluate_rejects` skips any rule whose
    required facts are unknown, so "we could not read it" can never become "no".
    """

    name: str
    requires: tuple[str, ...]
    test: Callable[[LaunchFacts, DbFacts, TriageConfig], str | None]


def _known(db: DbFacts, facts: LaunchFacts, field_name: str) -> bool:
    """Is this fact readable? Unknown means every rule that needs it stands down."""
    if field_name in db.unknowns or field_name in facts.unknowns:
        return False
    match field_name:
        case "creator":
            return facts.creator is not None
        case "creator_cohort":
            return facts.creator is not None
        case "creator_history":
            return db.creator_rugged is not None
        case "funder_cohort":
            return db.funder is not None
        case "image_fingerprint":
            return facts.image_fingerprint is not None and db.image_reuses is not None
        case "name_fingerprint":
            return facts.name_fingerprint is not None and db.name_reuses is not None
        case "copycat_classification":
            return db.copycat.available
        case _:
            return getattr(facts, field_name, None) is not None


def _r_creator_blacklisted(facts: LaunchFacts, db: DbFacts, cfg: TriageConfig) -> str | None:
    del facts, cfg
    return "creator is on our blacklist" if db.creator_blacklisted else None


def _r_creator_quarantined(facts: LaunchFacts, db: DbFacts, cfg: TriageConfig) -> str | None:
    del facts, cfg
    tags = db.creator_quarantine_tags
    return f"creator carries hard-quarantine tag(s): {','.join(sorted(tags))}" if tags else None


def _r_funder_blacklisted(facts: LaunchFacts, db: DbFacts, cfg: TriageConfig) -> str | None:
    del facts, cfg
    return f"creator was funded by blacklisted {db.funder}" if db.funder_blacklisted else None


def _r_creator_rug_history(facts: LaunchFacts, db: DbFacts, cfg: TriageConfig) -> str | None:
    del facts
    rugged = db.creator_rugged
    if rugged is not None and rugged >= cfg.reject_min_creator_rugs:
        return f"creator has {rugged} prior rug(s) (bar is {cfg.reject_min_creator_rugs})"
    return None


def _r_image_reused(facts: LaunchFacts, db: DbFacts, cfg: TriageConfig) -> str | None:
    del facts
    hits = db.image_reuses
    if hits is not None and hits >= cfg.reject_min_image_reuses:
        return f"image/metadata fingerprint already seen {hits}x (copycats graduate at 0.86%)"
    return None


def _r_name_reused(facts: LaunchFacts, db: DbFacts, cfg: TriageConfig) -> str | None:
    del facts
    hits = db.name_reuses
    if hits is not None and hits >= cfg.reject_min_name_reuses:
        return f"name+symbol already seen {hits}x (bar is {cfg.reject_min_name_reuses})"
    return None


def _r_dedup_copycat(facts: LaunchFacts, db: DbFacts, cfg: TriageConfig) -> str | None:
    del facts
    call = db.copycat
    if call.available and call.is_copycat and (call.confidence or 0.0) >= cfg.reject_min_copycat_confidence:
        return f"dedup classified as copycat (confidence {call.confidence:.2f})"
    return None


#: Ordered: the first rule that fires is the one reported, so put the most defensible and
#: most specific first. All of them are exclusions; none of them is a prediction.
REJECT_RULES: tuple[RejectRule, ...] = (
    RejectRule("creator_blacklisted", ("creator", "creator_cohort"), _r_creator_blacklisted),
    RejectRule("creator_quarantined", ("creator", "creator_cohort"), _r_creator_quarantined),
    RejectRule("funder_blacklisted", ("creator", "funder_cohort"), _r_funder_blacklisted),
    RejectRule("creator_rug_history", ("creator", "creator_history"), _r_creator_rug_history),
    RejectRule("image_reused", ("image_fingerprint",), _r_image_reused),
    RejectRule("name_reused", ("name_fingerprint",), _r_name_reused),
    RejectRule("dedup_copycat", ("copycat_classification",), _r_dedup_copycat),
)


def evaluate_rejects(
    facts: LaunchFacts, db: DbFacts, config: TriageConfig = DEFAULT_CONFIG
) -> list[tuple[str, str]]:
    """Every reject rule that both *can* run and *does* fire. Empty list is the normal case."""
    fired: list[tuple[str, str]] = []
    for rule in REJECT_RULES:
        if not all(_known(db, facts, req) for req in rule.requires):
            continue  # missing input: stand down. This is the rule that matters.
        why = rule.test(facts, db, config)
        if why:
            fired.append((rule.name, why))
    return fired


# --------------------------------------------------------------------------------------
# priority score
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Factor:
    name: str
    weight: float
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "weight": round(self.weight, 4), "detail": self.detail}


def score_launch(
    facts: LaunchFacts, db: DbFacts, config: TriageConfig = DEFAULT_CONFIG
) -> tuple[float, list[Factor]]:
    """Priority in [0, 1] for the tier-1 queue, plus the reasons for it.

    This is an *ordering*, not a probability. Nothing here has been backtested; see
    :data:`THRESHOLD_PROVENANCE`. An unknown creator scores the neutral prior rather than
    a penalty, because absence of dev history is uninformative (EDGE §1 #4), not adverse.
    """
    factors: list[Factor] = []
    score = config.unknown_prior

    grad = db.creator_graduated
    if grad:
        factors.append(Factor("creator_prior_graduate", config.w_creator_prior_graduate, f"{grad} prior"))
        if grad > 1:
            factors.append(Factor("creator_repeat_graduate", config.w_creator_repeat_graduate, f"{grad}"))
    if db.creator_cohort == COHORT_TRUSTED:
        factors.append(Factor("creator_trusted_cohort", config.w_creator_trusted_cohort, COHORT_TRUSTED))
    elif db.creator_cohort == COHORT_TRACKED:
        factors.append(Factor("creator_tracked_cohort", config.w_creator_tracked_cohort, COHORT_TRACKED))

    rugged = db.creator_rugged
    if rugged is not None and 0 < rugged < config.reject_min_creator_rugs:
        factors.append(Factor("creator_soft_rug", config.w_creator_soft_rug, f"{rugged} prior rug"))

    launches = db.creator_launches
    if (
        launches is not None
        and launches >= config.serial_launcher_min_launches
        and not db.creator_graduated
    ):
        factors.append(
            Factor("creator_serial_launcher", config.w_creator_serial_launcher, f"{launches} launches")
        )

    if db.name_reuses:
        factors.append(Factor("name_reused", config.w_name_reused, f"{db.name_reuses}x"))

    call = db.copycat
    if call.available and call.is_copycat and (call.confidence or 1.0) >= config.soft_copycat_min_confidence:
        factors.append(Factor("copycat_suspected", config.w_copycat_suspected, call.detail or "dedup"))

    if facts.name is None or facts.symbol is None or facts.metadata_uri is None:
        missing = [f for f in ("name", "symbol", "metadata_uri") if getattr(facts, f) is None]
        factors.append(Factor("metadata_incomplete", config.w_metadata_incomplete, ",".join(missing)))

    buy = facts.dev_buy_lamports
    if buy and buy > 0:
        # UNTESTED, see THRESHOLD_PROVENANCE. Saturating rather than linear so one
        # enormous dev buy cannot dominate the ordering on its own.
        frac = min(1.0, buy / max(1, config.dev_buy_saturation_lamports))
        factors.append(Factor("dev_buy_size", config.w_dev_buy_max * frac, f"{buy} lamports"))

    score += sum(f.weight for f in factors)
    return max(0.0, min(1.0, score)), factors


# --------------------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TriageDecision:
    """One tier-0 verdict, with everything needed to audit it later."""

    chain: Chain
    token: str | None
    verdict: Verdict
    score: float
    reasons: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    factors: tuple[Factor, ...] = ()
    creator: str | None = None
    fingerprint: str | None = None
    launchpad: str | None = None
    source: str = "pumpportal"
    event_ms: int | None = None
    ts_ms: int = 0
    latency_us: int = 0

    @property
    def promoted(self) -> bool:
        return self.verdict is Verdict.PROMOTE

    @property
    def rank(self) -> int:
        return VERDICT_RANK[self.verdict]

    def as_row(self) -> dict[str, Any]:
        return {
            "ts_ms": self.ts_ms or now_ms(),
            "chain": self.chain.value,
            "token": self.token or "",
            "verdict": self.verdict.value,
            "score": round(self.score, 6),
            "reasons_json": jdump(list(self.reasons)),
            "unknowns_json": jdump(list(self.unknowns)),
            "factors_json": jdump([f.as_dict() for f in self.factors]),
            "creator": self.creator,
            "fingerprint": self.fingerprint,
            "source": self.source,
            "launchpad": self.launchpad,
            "event_ms": self.event_ms,
            "latency_us": self.latency_us,
            "params_version": PARAMS_VERSION,
        }


class LatencyRecorder:
    """Ring buffer of tier-0 durations. The 100 ms p99 claim has to stay checkable in prod."""

    def __init__(self, window: int = DEFAULT_CONFIG.latency_window) -> None:
        self._samples: deque[int] = deque(maxlen=window)
        self._lock = threading.Lock()

    def record(self, micros: int) -> None:
        with self._lock:
            self._samples.append(micros)

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()

    def snapshot(self) -> list[int]:
        with self._lock:
            return sorted(self._samples)

    def percentiles(self) -> dict[str, Any]:
        ordered = self.snapshot()
        if not ordered:
            return {"samples": 0}
        return {
            "samples": len(ordered),
            "p50_us": _percentile(ordered, 0.50),
            "p95_us": _percentile(ordered, 0.95),
            "p99_us": _percentile(ordered, 0.99),
            "max_us": ordered[-1],
        }


def _percentile(ordered: Sequence[int], q: float) -> int:
    if not ordered:
        return 0
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return int(ordered[idx])


LATENCY = LatencyRecorder()


def triage(
    event: Mapping[str, Any] | Token | LaunchFacts,
    *,
    conn: sqlite3.Connection | None = None,
    config: TriageConfig = DEFAULT_CONFIG,
    record_latency: bool = True,
    register_index: bool = False,
) -> TriageDecision:
    """Tier 0. Read-only by default, no network, target p99 under 100 ms.

    Writes nothing unless ``register_index`` is set — :func:`screen_launch` is the
    side-effecting wrapper and the only caller that sets it. Keeping the decision pure is
    what makes it replayable against point-in-time data, which is the same property the
    lanes were built for.
    """
    started = time.perf_counter_ns()
    facts = parse_launch(event)
    db = read_db_facts(facts, conn, config=config, register_index=register_index)
    unknowns = tuple(dict.fromkeys(facts.unknowns + db.unknowns))

    if facts.mint is None:
        # No usable mint. This is DEFER, not REJECT, and deliberately so: the day a
        # provider renames the field, this line is the difference between a visible
        # 100%-defer anomaly and silently discarding the entire market.
        elapsed = (time.perf_counter_ns() - started) // 1000
        if record_latency:
            LATENCY.record(elapsed)
        return TriageDecision(
            chain=facts.chain,
            token=None,
            verdict=Verdict.DEFER,
            score=0.0,
            reasons=("payload_unreadable: no usable mint address",),
            unknowns=unknowns,
            creator=facts.creator,
            launchpad=facts.launchpad,
            source=facts.source,
            event_ms=facts.event_ms,
            ts_ms=now_ms(),
            latency_us=elapsed,
        )

    rejects = evaluate_rejects(facts, db, config)
    score, factors = score_launch(facts, db, config)

    if rejects:
        verdict = Verdict.REJECT
        reasons = tuple(f"{name}: {why}" for name, why in rejects)
        score = 0.0
    else:
        verdict = Verdict.PROMOTE if score >= config.promote_threshold else Verdict.DEFER
        reasons = tuple(f"{f.name}: {f.detail}" for f in factors) or ("no distinguishing signal",)

    elapsed = (time.perf_counter_ns() - started) // 1000
    if record_latency:
        LATENCY.record(elapsed)
    return TriageDecision(
        chain=facts.chain,
        token=facts.mint,
        verdict=verdict,
        score=score,
        reasons=reasons,
        unknowns=unknowns,
        factors=tuple(factors),
        creator=facts.creator,
        fingerprint=facts.image_fingerprint or facts.name_fingerprint,
        launchpad=facts.launchpad,
        source=facts.source,
        event_ms=facts.event_ms,
        ts_ms=now_ms(),
        latency_us=elapsed,
    )


# --------------------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------------------


def record_decision(
    decision: TriageDecision, conn: sqlite3.Connection | None = None
) -> int | None:
    """Persist one tier-0 verdict. Rejections included — that is the point.

    Two things depend on this table. The obvious one is that a filter nobody measures is a
    filter nobody can tell is broken. The less obvious one is that it is a labelled set:
    come back to the rejects an hour later, ask what they did, and you have exclusion
    precision without having placed a single trade.
    """
    row = decision.as_row()
    try:
        c = conn or get_conn()
        cur = c.execute(
            "INSERT INTO triage_decisions "
            "(ts_ms, chain, token, verdict, score, reasons_json, unknowns_json, factors_json, "
            " creator, fingerprint, source, launchpad, event_ms, latency_us, params_version) "
            "VALUES (:ts_ms,:chain,:token,:verdict,:score,:reasons_json,:unknowns_json,"
            ":factors_json,:creator,:fingerprint,:source,:launchpad,:event_ms,:latency_us,"
            ":params_version)",
            row,
        )
        return int(cur.lastrowid) if cur.lastrowid else None
    except sqlite3.Error as exc:
        log.warning("triage: could not record decision for %s (%s)", decision.token, exc)
        return None


def record_fingerprints(
    facts: LaunchFacts, conn: sqlite3.Connection | None = None
) -> None:
    """Index this launch's fingerprints so the *next* copy of it is cheap to spot.

    Called after the decision, never before: a token must not be its own duplicate.
    """
    pairs = (("image", facts.image_fingerprint), ("name", facts.name_fingerprint))
    ts = now_ms()
    try:
        c = conn or get_conn()
        for kind, fp in pairs:
            if fp is None or facts.mint is None:
                continue
            c.execute(
                "INSERT INTO triage_fingerprints "
                "(chain, fingerprint, first_token, first_seen_ms, last_seen_ms, hits, kind) "
                "VALUES (?,?,?,?,?,1,?) "
                "ON CONFLICT(chain, fingerprint, kind) DO UPDATE SET "
                "hits = hits + 1, last_seen_ms = excluded.last_seen_ms",
                (facts.chain.value, fp, facts.mint, ts, ts, kind),
            )
    except sqlite3.Error as exc:
        log.warning("triage: could not index fingerprints (%s)", exc)


def verdict_split(
    since_ms: int = 0, conn: sqlite3.Connection | None = None
) -> dict[str, int]:
    """Counts per verdict since ``since_ms``. The screen's own health check.

    All-defer means the payload stopped parsing. All-reject means a rule went rogue.
    Either way the shape of this dict is the alarm.
    """
    try:
        c = conn or get_conn()
        rows = c.execute(
            "SELECT verdict, COUNT(*) AS n FROM triage_decisions WHERE ts_ms >= ? GROUP BY verdict",
            (since_ms,),
        ).fetchall()
    except sqlite3.Error as exc:
        log.warning("triage: verdict split unreadable (%s)", exc)
        return {}
    out = {v.value: 0 for v in Verdict}
    for row in rows:
        out[str(row["verdict"])] = int(row["n"])
    return out


# --------------------------------------------------------------------------------------
# the bounded priority queue in front of tier 1
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueueStats:
    """How far behind we are, in the terms someone would actually ask the question."""

    depth: int
    capacity: int
    admitted: int
    shed: int
    served: int
    rejected_entry: int
    oldest_age_ms: int | None
    arrival_per_min: float
    service_per_min: float
    drain_eta_s: float | None
    saturated: bool
    best_score: float | None
    worst_score: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "depth": self.depth,
            "capacity": self.capacity,
            "admitted": self.admitted,
            "shed": self.shed,
            "served": self.served,
            "rejected_entry": self.rejected_entry,
            "oldest_age_ms": self.oldest_age_ms,
            "arrival_per_min": round(self.arrival_per_min, 2),
            "service_per_min": round(self.service_per_min, 2),
            "drain_eta_s": None if self.drain_eta_s is None else round(self.drain_eta_s, 1),
            "saturated": self.saturated,
            "best_score": self.best_score,
            "worst_score": self.worst_score,
        }


class TriageQueue:
    """Bounded, score-ordered work queue for tier 1.

    Three properties, each of which exists because the alternative is worse:

    * **Bounded.** An unbounded queue in front of a 7.6-second consumer is a memory leak
      that also serves stale work: by the time a 400-deep backlog reaches a snipe
      candidate the candidate is half an hour old. At capacity the *worst* entry is shed,
      not the newest, so a burst degrades by dropping the least interesting work.
    * **Ordered by verdict then score.** A ``promote`` always outranks a ``defer``
      whatever their scores. Within a verdict, higher score first; ties break oldest-first
      so nothing starves.
    * **Admits defers by default.** Tier 1 has ~8 scans a minute of capacity and it is
      better spent on the best of a boring minute than idle. Promotes always jump them.
      ``admit_defers=False`` turns this off and makes the screen strict.

    Thread-safe: the ingest loop offers, tier-1 workers pop.
    """

    def __init__(
        self,
        capacity: int = 256,
        *,
        name: str = "tier1",
        admit_defers: bool = True,
        admit_defer_above: float = 0.0,
        rate_window_s: float = 300.0,
    ) -> None:
        self.capacity = max(1, int(capacity))
        self.name = name
        self.admit_defers = admit_defers
        self.admit_defer_above = admit_defer_above
        self.rate_window_s = rate_window_s
        self._items: list[tuple[int, float, int, TriageDecision]] = []  # worst -> best
        self._queued: set[tuple[str, str]] = set()
        self._seq = 0
        self._admitted = 0
        self._shed = 0
        self._served = 0
        self._rejected_entry = 0
        self._arrivals: deque[float] = deque()
        self._services: deque[float] = deque()
        self._lock = threading.RLock()

    # -- admission ---------------------------------------------------------------------

    def eligible(self, decision: TriageDecision) -> bool:
        if decision.token is None or decision.verdict is Verdict.REJECT:
            return False
        if decision.verdict is Verdict.DEFER:
            return self.admit_defers and decision.score >= self.admit_defer_above
        return True

    def offer(self, decision: TriageDecision) -> bool:
        """Admit one decision. False means it was not eligible, a duplicate, or shed."""
        if not self.eligible(decision):
            with self._lock:
                self._rejected_entry += 1
            return False
        key = (decision.chain.value, decision.token or "")
        with self._lock:
            now = time.monotonic()
            self._arrivals.append(now)
            self._trim(now)
            if key in self._queued:
                return False
            self._seq += 1
            entry = (decision.rank, decision.score, -self._seq, decision)
            if len(self._items) >= self.capacity:
                worst = self._items[0]
                if entry[:3] <= worst[:3]:
                    self._shed += 1
                    return False  # the newcomer is the worst thing here; shed it
                dropped = self._items.pop(0)
                self._queued.discard((dropped[3].chain.value, dropped[3].token or ""))
                self._shed += 1
            bisect.insort(self._items, entry, key=lambda e: e[:3])
            self._queued.add(key)
            self._admitted += 1
            return True

    def offer_many(self, decisions: Iterable[TriageDecision]) -> int:
        return sum(1 for d in decisions if self.offer(d))

    # -- service -----------------------------------------------------------------------

    def pop(self) -> TriageDecision | None:
        """Best available work, or ``None``."""
        with self._lock:
            if not self._items:
                return None
            _, _, _, decision = self._items.pop()
            self._queued.discard((decision.chain.value, decision.token or ""))
            now = time.monotonic()
            self._services.append(now)
            self._served += 1
            self._trim(now)
            return decision

    def pop_batch(self, n: int) -> list[TriageDecision]:
        out: list[TriageDecision] = []
        for _ in range(max(0, n)):
            item = self.pop()
            if item is None:
                break
            out.append(item)
        return out

    def peek(self) -> TriageDecision | None:
        with self._lock:
            return self._items[-1][3] if self._items else None

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._queued.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    # -- backpressure ------------------------------------------------------------------

    def _trim(self, now: float) -> None:
        cutoff = now - self.rate_window_s
        while self._arrivals and self._arrivals[0] < cutoff:
            self._arrivals.popleft()
        while self._services and self._services[0] < cutoff:
            self._services.popleft()

    def _rate_per_min(self, stamps: deque[float], now: float) -> float:
        if len(stamps) < 2:
            return 0.0
        span = max(now - stamps[0], 1e-6)
        return len(stamps) * 60.0 / span

    def stats(self) -> QueueStats:
        """A snapshot that answers "are we behind, and by how much".

        ``drain_eta_s`` is ``None`` when tier 1 has served nothing recently, because the
        honest answer to "when will this drain" at a service rate of zero is "never", not
        a large number.
        """
        with self._lock:
            now = time.monotonic()
            self._trim(now)
            depth = len(self._items)
            arrival = self._rate_per_min(self._arrivals, now)
            service = self._rate_per_min(self._services, now)
            oldest = None
            if self._items:
                oldest_ms = min(e[3].ts_ms for e in self._items)
                oldest = max(0, now_ms() - oldest_ms)
            eta = (depth / (service / 60.0)) if service > 0 else None
            return QueueStats(
                depth=depth,
                capacity=self.capacity,
                admitted=self._admitted,
                shed=self._shed,
                served=self._served,
                rejected_entry=self._rejected_entry,
                oldest_age_ms=oldest,
                arrival_per_min=arrival,
                service_per_min=service,
                drain_eta_s=eta,
                saturated=depth >= self.capacity or (service > 0 and arrival > service),
                best_score=self._items[-1][3].score if self._items else None,
                worst_score=self._items[0][3].score if self._items else None,
            )


_QUEUE: TriageQueue | None = None
_QUEUE_LOCK = threading.Lock()


def get_queue() -> TriageQueue:
    """Process-wide default queue, so ingest and tier 1 share one without wiring."""
    global _QUEUE
    with _QUEUE_LOCK:
        if _QUEUE is None:
            _QUEUE = TriageQueue()
        return _QUEUE


def set_queue(queue: TriageQueue | None) -> None:
    """Replace the default queue. Tests use this; so would a second lane with its own budget."""
    global _QUEUE
    with _QUEUE_LOCK:
        _QUEUE = queue


def snapshot_backpressure(
    queue: TriageQueue | None = None, conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    """Persist and emit one backpressure sample. Call this on a timer, not per event."""
    q = queue or get_queue()
    stats = q.stats()
    payload = stats.as_dict()
    try:
        c = conn or get_conn()
        c.execute(
            "INSERT INTO triage_backpressure "
            "(ts_ms, queue_name, depth, capacity, admitted, shed, served, oldest_age_ms, "
            " arrival_per_min, service_per_min, drain_eta_s, saturated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                now_ms(),
                q.name,
                stats.depth,
                stats.capacity,
                stats.admitted,
                stats.shed,
                stats.served,
                stats.oldest_age_ms,
                stats.arrival_per_min,
                stats.service_per_min,
                stats.drain_eta_s,
                1 if stats.saturated else 0,
            ),
        )
    except sqlite3.Error as exc:
        log.warning("triage: could not persist backpressure (%s)", exc)
    emit(
        EVENT_BACKPRESSURE,
        payload,
        level="warn" if stats.saturated else "info",
        conn=conn,
    )
    return payload


# --------------------------------------------------------------------------------------
# the one line the ingest loop calls
# --------------------------------------------------------------------------------------


def screen_launch(
    event: Mapping[str, Any] | Token | LaunchFacts,
    *,
    conn: sqlite3.Connection | None = None,
    queue: TriageQueue | None = None,
    config: TriageConfig = DEFAULT_CONFIG,
    emit_promotions: bool = True,
) -> TriageDecision:
    """Screen one launch: decide, record, index, enqueue. **This is the ingest entry point.**

    Never raises. A listener that dies on one bad frame is worse than no listener, so
    every failure below the decision is logged and swallowed; the decision itself is
    already total by construction.
    """
    try:
        decision = triage(event, conn=conn, config=config, register_index=True)
    except Exception as exc:  # noqa: BLE001 — the screen must never take the listener down
        log.exception("triage: screen failed (%s)", type(exc).__name__)
        return TriageDecision(
            chain=Chain.SOL,
            token=None,
            verdict=Verdict.DEFER,
            score=0.0,
            reasons=(f"triage_error: {type(exc).__name__}",),
            ts_ms=now_ms(),
        )

    record_decision(decision, conn)
    try:
        record_fingerprints(parse_launch(event), conn)
    except Exception as exc:  # noqa: BLE001
        log.warning("triage: fingerprint index failed (%s)", type(exc).__name__)

    (queue or get_queue()).offer(decision)

    if emit_promotions and decision.verdict is Verdict.PROMOTE:
        # Promotions only. 20,000 rejections a day on the shared bus would drown every
        # other consumer; the rejections live in `triage_decisions`, which is where the
        # measurement work reads them from anyway.
        emit(
            EVENT_VERDICT,
            {
                "token": decision.token,
                "verdict": decision.verdict.value,
                "score": round(decision.score, 4),
                "reasons": list(decision.reasons),
                "creator": decision.creator,
                "latency_us": decision.latency_us,
            },
            chain=decision.chain,
            subject=decision.token,
            conn=conn,
        )
    return decision


def latency_report() -> dict[str, Any]:
    """Tier-0's own latency distribution, in microseconds, plus the budget verdict."""
    stats = LATENCY.percentiles()
    p99 = stats.get("p99_us")
    if p99 is not None:
        stats["within_budget"] = p99 <= DEFAULT_CONFIG.latency_budget_us
        stats["budget_us"] = DEFAULT_CONFIG.latency_budget_us
    return stats


__all__ = [
    "DEFAULT_CONFIG",
    "EVENT_BACKPRESSURE",
    "EVENT_VERDICT",
    "LAUNCHPAD_UNKNOWN",
    "PARAMS_VERSION",
    "REJECT_RULES",
    "THRESHOLD_PROVENANCE",
    "CopycatCall",
    "DbFacts",
    "Factor",
    "LaunchFacts",
    "QueueStats",
    "RejectRule",
    "TriageConfig",
    "TriageDecision",
    "TriageQueue",
    "Verdict",
    "dedup_status",
    "disable_dedup",
    "evaluate_rejects",
    "get_queue",
    "latency_report",
    "parse_launch",
    "read_db_facts",
    "record_decision",
    "record_fingerprints",
    "reset_dedup",
    "reset_dedup_probe",
    "score_launch",
    "screen_launch",
    "set_queue",
    "snapshot_backpressure",
    "triage",
    "verdict_split",
]
