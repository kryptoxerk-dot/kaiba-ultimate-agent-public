"""One labelled row per entry we could have taken, and what each variable was worth on it.

Why this module exists
----------------------
:mod:`kaiba.intelligence.confluence` holds seventeen variables, every weight traced to a
published effect size, and **zero callers**. It was never tested against our own tape. This
module is the test bench: it turns the database into one row per *(chain, token, entry_ms)*
carrying (a) only what was observable strictly before ``entry_ms`` and (b) what the token
then did, net of cost. Everything else here - separations, the combined ranking, the gates -
is arithmetic on those rows.

The four things that make a number here worth reading
-----------------------------------------------------
**1. No feature may read past the entry.** Every feature read goes through
:class:`kaiba.learning.replay.PointInTime`, which appends the time bound to the SQL and then
re-checks the time column of every row on the way out. The view is opened at ``entry_ms - 1``
so our own entry swap - which is in ``swaps`` - cannot be a feature of the decision to make
it. The two derived indices (:class:`EventIndex`) bisect on the event timestamp for the same
reason. ``tests/test_outcomes.py::test_no_feature_reads_past_entry`` proves it by building a
row, then writing a contradictory future into every table and asserting not one feature
value moved.

**2. The split is by time, always.** :meth:`Dataset.time_split` cuts on ``entry_ms``.
Anything fitted - a threshold, a direction - is fitted on the earlier half only and applied
unchanged to the later half. A variable whose split rule comes from the literature is not
fitted at all, so its later-half number is a pure out-of-sample test; a variable whose
threshold we chose is marked ``fitted_median`` and its earlier-half number is worthless by
construction.

**3. Every row says how much to trust it.** :class:`SourceKind` is on every row and orders
the sources by how close they are to money that actually moved. Six live round trips are the
only ground truth this operation has. Shadow fills are optimistic - the two live fills of the
shadow lane lost 91.5% and 66.3% against a shadow mean near break-even. Decision-time rows
and tape pseudo-entries have no fill model at all: they measure what the *token* did, not
what *we* would have got.

**4. Net of cost or it is not a result.** GMGN takes 1% per leg (MEASURED), so the default
round trip is 200 bps applied multiplicatively on both legs. A +1.5% mean is a loss.

What this module does NOT do
----------------------------
It does not fit weights, it does not change a lane, it does not size anything and it writes
nothing to the database. It reports. The one number it is allowed to produce about
significance comes from :func:`kaiba.learning.gates.deflated_sharpe` and
:func:`kaiba.learning.gates.pbo_cscv`, because a hand-rolled t-test over a sample this small
is how a null gets published as an edge.

Biases that are in every number below, stated once
--------------------------------------------------
*Selection.* Our ``swaps`` table holds tokens that something already surfaced - a launch
feed, a signal, a scan. It is not the population of Solana launches, and a variable that
separates within it has not been shown to separate outside it.

*Survivorship.* A dead token stops printing. Requiring a price at +60 min keeps only the
tokens that lived an hour, which is the single easiest way to manufacture a positive mean
here. Every forward return therefore comes in two flavours - ``strict`` (a print exists at
or after the horizon; survivorship-biased upward) and ``carry`` (the last print we hold,
marked ``tape_ended``; biased downward in *time* but the honest one for "what happened") -
and the report shows both.

*Window.* MEASURED 2026-09-22: every ``events`` row on the live box spans 1789980408448 to
1790042666623, i.e. about 17.3 hours, and 8,095 of 8,729 Solana tokens in ``swaps`` print
for the first time on one day. A "time split" over that is a split inside a day and a half.
It tests nothing about a different regime, and no result here may be described as regime-
robust.
"""

from __future__ import annotations

import json
import logging
import math
import random
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from kaiba.core.schemas import Chain, EvidenceBasis
from kaiba.intelligence import confluence
from kaiba.intelligence.confluence import (
    LITERATURE_V1,
    MAPPINGS,
    Observation,
    Observations,
    WeightSet,
)
from kaiba.intelligence.deployer import series_source, single_source
from kaiba.learning import gates, replay

log = logging.getLogger(__name__)

MODEL_VERSION = "kaiba-outcomes-v1"

LAMPORTS_PER_SOL = 1_000_000_000


# --------------------------------------------------------------------------------------
# constants, each with where it came from
# --------------------------------------------------------------------------------------

#: Round-trip cost in basis points, applied as two legs of half each.
#: MEASURED 2026-09-22: GMGN takes 1% per leg on both the buy and the sell. Gas on Solana is
#: a further ~0.00005 SOL per leg (~0.09% of a 0.056 SOL position) and ATA rent of 0.00204
#: SOL is recoverable only if the exit closes the account - UNRESOLVED, so it is excluded
#: here and the true cost is therefore at least this and possibly ~0.2pp more.
COST_BPS_ROUND_TRIP = 200

#: Horizons in minutes. DEFINITIONAL: the reporting contract asked for +5/+20/+60.
DEFAULT_HORIZONS_MIN: tuple[int, ...] = (5, 20, 60)

#: A pseudo-entry is taken this long after a token's first priced print.
#: INVENTED. There is no measured "right" lag; this one is chosen because it is roughly
#: where our own live entries land (MEASURED: the six live fills priced 3.2s to 101.8s after
#: the last tape print before entry) and because a lag of zero would make the entry price
#: and the first print the same row. What would settle it: sweeping the lag and showing the
#: ranking of variables is stable across it - which :func:`sweep_pseudo_entry_lag` does.
PSEUDO_ENTRY_LAG_MS = 60_000

#: A pseudo-entry needs at least this many priced prints before it, or there is nothing to
#: compute a feature from. INVENTED, and the same caveat: swept, not justified.
MIN_PRINTS_BEFORE_PSEUDO_ENTRY = 5

#: Under EntryFill.NEXT_PRINT, refuse an entry whose next print is further away than this:
#: past it the "fill" is a different market. DEFINITIONAL, and deliberately the same order
#: as MAX_ENTRY_PRICE_AGE_MS so the window around a decision is symmetric.
MAX_FILL_LAG_MS = 60_000

#: Refuse an entry whose last pre-entry print is older than this. DEFINITIONAL: beyond it the
#: "return from entry" is mostly the move between the stale print and the entry, which is not
#: a return we could have captured.
MAX_ENTRY_PRICE_AGE_MS = 120_000

#: Cap on pre-entry swap rows fed to the wash / turnover counters, keeping the EARLIEST rows
#: because both variables are defined over the early window. OPERATIONAL: a cost guard, not a
#: measurement choice. Truncation is recorded in the observation detail.
MAX_SWAP_ROWS = 4_000

#: Bootstrap resamples for a confidence interval on a separation. DEFINITIONAL.
BOOTSTRAP_RESAMPLES = 2_000

#: Seed for every resample, so two runs of this module produce the same interval.
BOOTSTRAP_SEED = 20260922

#: Time buckets for the PBO matrix. DEFINITIONAL: gates.pbo_cscv wants T periods by N
#: configurations and needs at least ``splits`` periods.
PBO_PERIODS = 16

PROVENANCE: Mapping[str, str] = {
    "COST_BPS_ROUND_TRIP": (
        "MEASURED 2026-09-22 - GMGN charges 1% per leg. Excludes gas and the unresolved ATA "
        "rent, so it is a floor on the real cost."
    ),
    "DEFAULT_HORIZONS_MIN": "DEFINITIONAL - the horizons the task asked to be reported.",
    "MAX_FILL_LAG_MS": (
        "DEFINITIONAL - past it the next print is a different market, not our fill."
    ),
    "PSEUDO_ENTRY_LAG_MS": (
        "INVENTED - no measured basis. Settled by sweep_pseudo_entry_lag showing the variable "
        "ranking is or is not stable across the lag."
    ),
    "MIN_PRINTS_BEFORE_PSEUDO_ENTRY": (
        "INVENTED - a floor on how much tape a feature needs. Settled by the same sweep."
    ),
    "MAX_ENTRY_PRICE_AGE_MS": (
        "DEFINITIONAL - past it the measured return is mostly the gap between a stale print "
        "and the entry."
    ),
    "MAX_SWAP_ROWS": "OPERATIONAL - a cost guard. Truncation is reported, never silent.",
    "BOOTSTRAP_RESAMPLES": "DEFINITIONAL - interval width, not an effect size.",
    "PBO_PERIODS": "DEFINITIONAL - the shape gates.pbo_cscv requires.",
}


# --------------------------------------------------------------------------------------
# sources, ordered by how close each is to money that moved
# --------------------------------------------------------------------------------------


class SourceKind(StrEnum):
    """Where a row came from. This is never dropped, because the bias differs per source."""

    #: A real fill with real money. n=6 on this box, all six losses. The only ground truth.
    LIVE = "live"
    #: A paper fill. Optimistic against live by an unmeasured amount: the two live fills of
    #: the shadow lane returned -91.5% and -66.3% against a shadow mean near break-even.
    SHADOW = "shadow"
    #: An instant at which the agent actually looked at a token and said enter or skip. No
    #: fill model at all - the label is what the tape did, not what we would have got.
    DECISION = "decision"
    #: A synthetic entry on a token in the tape. Largest n, worst selection bias: the token
    #: is in the tape only because something already surfaced it.
    TAPE = "tape"


#: Most trustworthy first. Used for reporting order, never for weighting.
TRUST_ORDER: tuple[SourceKind, ...] = (
    SourceKind.LIVE,
    SourceKind.SHADOW,
    SourceKind.DECISION,
    SourceKind.TAPE,
)

class EntryFill(StrEnum):
    """Which price the entry is booked at. This choice is worth more than every variable.

    ``LAST_BEFORE`` books the last print strictly before the decision instant. It is the
    only price observable at the decision, and it is **not a fill**: MEASURED 2026-09-22,
    the very next print on a Solana tape pseudo-entry is +13.0% above it on average, so the
    convention hands every synthetic entry a 13% head start nobody could have taken.

    ``NEXT_PRINT`` books the first print at or after the decision instant - the next trade
    that actually happened. Still optimistic (no impact, no queue, unlimited size), but it
    removes the systematic head start, and it is what our live fills look like: MEASURED,
    the six live entries priced 3.2s to 101.8s after the last print before them.
    """

    LAST_BEFORE = "last_before"
    NEXT_PRINT = "next_print"


SOURCE_BIAS: Mapping[SourceKind, str] = {
    SourceKind.LIVE: "real fill, real slippage; n is tiny",
    SourceKind.SHADOW: "paper fill, optimistic by an unmeasured amount",
    SourceKind.DECISION: "real look, no fill model; tape label only",
    SourceKind.TAPE: "synthetic entry; selection bias is at its worst here",
}


# --------------------------------------------------------------------------------------
# point-in-time indices over the event log
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreationFact:
    """What a ``token.created`` event says, with the instant it was emitted."""

    ts_ms: int
    chain: str
    mint: str
    creator: str | None
    uri: str | None
    name_key: str | None
    created_ms: int | None


class EventIndex:
    """Creator history and metadata reuse, answerable as of any past instant.

    ``tokens`` and ``creators`` cannot be read point-in-time at all - both are mutated in
    place, and :data:`kaiba.learning.replay.NO_OBSERVATION_TIME` refuses them by name. The
    ``token.created`` and ``token.migrated`` events carry the same facts and are timestamped
    and never rewritten, so this index is built from those and every lookup filters on the
    event timestamp it stored. That is the same guarantee
    :class:`~kaiba.learning.replay.PointInTime` gives, applied to a payload field the SQL
    cannot index. The index is built over the whole log on purpose: a lookup that filters is
    testable, and ``tests/test_outcomes.py::test_no_feature_reads_past_entry`` writes future
    events and asserts the readings do not move.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.creations: dict[tuple[str, str], CreationFact] = {}
        self._by_creator: dict[tuple[str, str], list[tuple[int, str]]] = {}
        self._by_uri: dict[tuple[str, str], list[tuple[int, str]]] = {}
        self._by_name: dict[tuple[str, str], list[tuple[int, str]]] = {}
        self._migrations: dict[tuple[str, str], int] = {}
        self.n_created = 0
        self.n_migrated = 0

        cur = conn.execute(
            "SELECT ts_ms, chain, subject, payload FROM events WHERE kind='token.created' "
            "ORDER BY ts_ms"
        )
        for ts_ms, chain, subject, payload in cur:
            if ts_ms is None or not subject:
                continue
            data = _jload(payload)
            creator = _text(data.get("creator"))
            uri = _text(data.get("uri"))
            name = _text(data.get("name"))
            symbol = _text(data.get("symbol"))
            name_key = f"{(name or '').strip().lower()}|{(symbol or '').strip().lower()}"
            if name_key == "|":
                name_key = None
            key = (str(chain), str(subject))
            fact = CreationFact(
                ts_ms=int(ts_ms),
                chain=str(chain),
                mint=str(subject),
                creator=creator,
                uri=uri,
                name_key=name_key,
                created_ms=_int(data.get("created_ms")),
            )
            self.creations.setdefault(key, fact)
            self.n_created += 1
            if creator:
                self._by_creator.setdefault((str(chain), creator), []).append((int(ts_ms), str(subject)))
            if uri:
                self._by_uri.setdefault((str(chain), uri), []).append((int(ts_ms), str(subject)))
            if name_key:
                self._by_name.setdefault((str(chain), name_key), []).append((int(ts_ms), str(subject)))

        cur = conn.execute(
            "SELECT ts_ms, chain, subject FROM events WHERE kind='token.migrated' ORDER BY ts_ms"
        )
        for ts_ms, chain, subject in cur:
            if ts_ms is None or not subject:
                continue
            self._migrations.setdefault((str(chain), str(subject)), int(ts_ms))
            self.n_migrated += 1

    # ------------------------------------------------------------------ lookups

    def creation(self, chain: str, token: str) -> CreationFact | None:
        return self.creations.get((chain, token))

    def _earlier(
        self, table: dict[tuple[str, str], list[tuple[int, str]]], key: tuple[str, str], before_ms: int, exclude: str
    ) -> list[tuple[int, str]]:
        rows = table.get(key)
        if not rows:
            return []
        return [(ts, mint) for ts, mint in rows if ts < before_ms and mint != exclude]

    def prior_launches(self, chain: str, creator: str, before_ms: int, exclude: str) -> list[tuple[int, str]]:
        """Every mint this creator launched that we had *seen* strictly before ``before_ms``."""
        return self._earlier(self._by_creator, (chain, creator), before_ms, exclude)

    def graduated_before(self, chain: str, mints: Iterable[str], before_ms: int) -> int:
        n = 0
        for mint in mints:
            ts = self._migrations.get((chain, mint))
            if ts is not None and ts < before_ms:
                n += 1
        return n

    def reuse_before(self, chain: str, token: str, before_ms: int) -> tuple[int, int]:
        """``(uri reuses, name+symbol reuses)`` by an *earlier* mint, seen before the entry."""
        fact = self.creation(chain, token)
        if fact is None:
            return 0, 0
        by_uri = len(self._earlier(self._by_uri, (chain, fact.uri or ""), before_ms, token)) if fact.uri else 0
        by_name = (
            len(self._earlier(self._by_name, (chain, fact.name_key or ""), before_ms, token))
            if fact.name_key
            else 0
        )
        return by_uri, by_name


# --------------------------------------------------------------------------------------
# candidate variables: cheap, point-in-time, and nobody has tested them either
# --------------------------------------------------------------------------------------

#: Variables that are NOT in LITERATURE_V1. They come off the pre-entry swap stream and cost
#: nothing to compute. Every one of them is an INVENTED candidate: no published effect size
#: backs any of them, and they are here to be measured, not to be believed. Each is tested
#: with a threshold fitted on the earlier half only, which is why they carry
#: ``split="fitted_median"`` in the report and why they count toward the trial count that
#: deflates the Sharpe.
CANDIDATE_VARIABLES: tuple[str, ...] = (
    "pre_entry_swaps",
    "pre_entry_unique_wallets",
    "pre_entry_buy_share",
    "pre_entry_unique_buyers",
    "pre_entry_net_sol_inflow",
    "pre_entry_largest_buy_sol",
    "pre_entry_run_from_first_pct",
    "pre_entry_run_last_5m_pct",
    "pre_entry_drawdown_from_peak_pct",
    "pre_entry_swaps_per_min_5m",
    "token_age_at_entry_s",
    "bundled_pct_at_launch",
    "sniped_pct_at_launch",
)

CANDIDATE_PROVENANCE: Mapping[str, str] = {
    "pre_entry_swaps": "INVENTED - count of swaps before entry. Settled by its own OOS separation.",
    "pre_entry_unique_wallets": "INVENTED - distinct wallets, NOT entities. Weaker than confluence's count.",
    "pre_entry_buy_share": "INVENTED - buys / swaps before entry.",
    "pre_entry_unique_buyers": "INVENTED - distinct buy-side wallets, addresses not entities.",
    "pre_entry_net_sol_inflow": "INVENTED - buy-side minus sell-side native, in SOL.",
    "pre_entry_largest_buy_sol": "INVENTED - biggest single buy before entry, in SOL.",
    "pre_entry_run_from_first_pct": "INVENTED - price at entry over first priced print.",
    "pre_entry_run_last_5m_pct": "INVENTED - five-minute momentum into the entry.",
    "pre_entry_drawdown_from_peak_pct": "INVENTED - entry price against the pre-entry peak.",
    "pre_entry_swaps_per_min_5m": "INVENTED - trade rate over the five minutes into the entry.",
    "token_age_at_entry_s": "INVENTED - seconds from the creation event to the entry.",
    "bundled_pct_at_launch": (
        "INVENTED as a predictor - the number itself is MEASURED by kaiba-bundles-v1 and read "
        "point-in-time from token_bundles.computed_ms. It is NOT MELT's bundle-adjusted top-10 "
        "delta, which needs a holder list we do not store."
    ),
    "sniped_pct_at_launch": "INVENTED as a predictor; MEASURED reading from token_bundles.",
}


# --------------------------------------------------------------------------------------
# rows
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Forward:
    """What the tape did after an entry, at one horizon."""

    horizon_min: int
    gross_strict_pct: float | None
    net_strict_pct: float | None
    gross_carry_pct: float | None
    net_carry_pct: float | None
    #: Priced prints strictly after the entry and at or before the horizon.
    prints_in_window: int
    #: True when no print exists at or after the horizon, i.e. the tape ran out first.
    tape_ended: bool
    #: True when the tape ran out AND nothing printed inside the window either, so this
    #: horizon has no observation at all. MEASURED 2026-09-22: on this box a token's
    #: coverage end never extends past its last print (0 of 4,604 Solana tokens), so a
    #: missing forward print is ALWAYS our collector stopping and NEVER evidence that the
    #: token died. Censored rows are therefore excluded from a horizon, not scored as zero.
    censored: bool
    last_print_ms: int | None


@dataclass(frozen=True, slots=True)
class OutcomeRow:
    """One entry we could have taken, everything observable before it, and what followed."""

    chain: str
    token: str
    entry_ms: int
    source: SourceKind
    ref: str
    lane: str | None
    #: The last print strictly before the decision instant - what was observable.
    entry_price_usd: float | None
    entry_price_age_ms: int | None
    #: The price the forward returns are actually booked at, per :class:`EntryFill`.
    fill_price_usd: float | None
    fill_ms: int | None
    fill: EntryFill
    #: Gross move from the observable pre-entry price to the very NEXT print, in percent.
    #: MEASURED 2026-09-22 on this box: +13.0% on tape pseudo-entries, +3.7% on decision
    #: instants, -2.8% on shadow fills and -30.4% on the five live ones. Under
    #: ``LAST_BEFORE`` this whole number is handed to the backtest for free.
    next_print_move_pct: float | None
    observations: Mapping[str, Observation]
    mapped: Mapping[str, float | None]
    raw: Mapping[str, float | None]
    forwards: Mapping[int, Forward]
    realised_pnl_pct: float | None
    realised_exit_reason: str | None
    action: str | None
    dossier_grade: str | None
    notes: tuple[str, ...] = ()

    def net(self, horizon_min: int, *, strict: bool = False) -> float | None:
        fwd = self.forwards.get(horizon_min)
        if fwd is None:
            return None
        return fwd.net_strict_pct if strict else fwd.net_carry_pct

    def to_observations(self) -> Observations:
        """The confluence input this row was scored from, rebuilt for the scorer."""
        return Observations(
            chain=Chain(self.chain),
            token=self.token,
            as_of_ms=self.entry_ms,
            values=dict(self.observations),
            point_in_time=True,
            notes=self.notes,
        )


@dataclass(frozen=True, slots=True)
class Census:
    """The denominator, written down before any result. Survivorship guard."""

    considered: int
    kept: int
    dropped: dict[str, int] = field(default_factory=dict)

    def line(self) -> str:
        reasons = ", ".join(f"{k}={v}" for k, v in sorted(self.dropped.items())) or "none"
        return f"considered {self.considered}, kept {self.kept}, dropped: {reasons}"


@dataclass(frozen=True, slots=True)
class Dataset:
    rows: tuple[OutcomeRow, ...]
    census: Census
    cost_bps: int
    horizons_min: tuple[int, ...]
    built_from: str

    def __len__(self) -> int:
        return len(self.rows)

    def by_source(self, *sources: SourceKind) -> Dataset:
        wanted = set(sources)
        return self._like([r for r in self.rows if r.source in wanted])

    def _like(self, rows: Sequence[OutcomeRow]) -> Dataset:
        return Dataset(
            rows=tuple(rows),
            census=self.census,
            cost_bps=self.cost_bps,
            horizons_min=self.horizons_min,
            built_from=self.built_from,
        )

    def one_per_token(self) -> Dataset:
        """One row per token, keeping the most trustworthy source, earliest entry on a tie.

        A token that we traded live also has a shadow row, a decision row and a tape
        pseudo-entry, and all four carry almost the same forward return. Counting them as
        four observations inflates every n in this module and narrows every interval on a
        sample that did not get any larger. MEASURED 2026-09-22: 366 of 3,014 Solana tokens
        carry more than one row, two of them carry six.
        """
        rank = {kind: i for i, kind in enumerate(TRUST_ORDER)}
        best: dict[str, OutcomeRow] = {}
        for row in sorted(self.rows, key=lambda r: (rank.get(r.source, 99), r.entry_ms)):
            best.setdefault(f"{row.chain}:{row.token}", row)
        return self._like(sorted(best.values(), key=lambda r: r.entry_ms))

    def time_split(self, fraction: float = 0.5, *, by_token: bool = True) -> tuple[Dataset, Dataset]:
        """Earlier ``fraction`` to fit on, the rest to report on. Cut on ``entry_ms``.

        ``by_token`` assigns a token to a half by its FIRST entry and keeps every row of that
        token with it, so a token cannot be in both halves. Without it a token traded twice
        across the cut trains the threshold that is then tested on its own second entry -
        MEASURED 2026-09-22: 34 Solana tokens straddle the cut on this dataset, which is
        small and is still leakage.
        """
        if not self.rows:
            return self, self
        ordered = sorted(self.rows, key=lambda r: (r.entry_ms, r.token))
        index = max(1, min(len(ordered) - 1, int(round(len(ordered) * fraction))))
        cut_ms = ordered[index - 1].entry_ms
        if not by_token:
            return (
                self._like([r for r in ordered if r.entry_ms <= cut_ms]),
                self._like([r for r in ordered if r.entry_ms > cut_ms]),
            )
        first_seen: dict[str, int] = {}
        for row in ordered:
            key = f"{row.chain}:{row.token}"
            first_seen[key] = min(first_seen.get(key, row.entry_ms), row.entry_ms)
        early = [r for r in ordered if first_seen[f"{r.chain}:{r.token}"] <= cut_ms]
        late = [r for r in ordered if first_seen[f"{r.chain}:{r.token}"] > cut_ms]
        return self._like(early), self._like(late)

    def span_ms(self) -> tuple[int, int] | None:
        if not self.rows:
            return None
        stamps = [r.entry_ms for r in self.rows]
        return min(stamps), max(stamps)


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------


def _jload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        got = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    got = str(value).strip()
    return got or None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        got = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(got) or math.isinf(got) else got


def _price(row: Mapping[str, Any]) -> float | None:
    got = _float(row.get("price_usd"))
    return got if got is not None and got > 0.0 else None


def net_pct(entry_price: float, exit_price: float, cost_bps: int) -> float:
    """Percentage return after a fee on each leg. The fee is charged on both legs."""
    leg = (cost_bps / 2.0) / 10_000.0
    multiple = (exit_price / entry_price) * (1.0 - leg) * (1.0 - leg)
    return (multiple - 1.0) * 100.0


def _logit(p: float) -> float:
    p = min(max(p, 1e-9), 1.0 - 1e-9)
    return math.log(p / (1.0 - p))


# --------------------------------------------------------------------------------------
# features, strictly before the entry
# --------------------------------------------------------------------------------------


def observe_before_entry(
    conn: sqlite3.Connection,
    chain: str,
    token: str,
    entry_ms: int,
    *,
    index: EventIndex,
    max_swap_rows: int = MAX_SWAP_ROWS,
) -> tuple[dict[str, Observation], dict[str, float | None], list[str]]:
    """Every LITERATURE_V1 variable and every candidate, as of one millisecond before entry.

    Returns ``(observations, raw_readings, notes)``. ``observations`` is keyed by the
    confluence variable name and is what the scorer consumes; ``raw_readings`` additionally
    carries the candidates, which the scorer knows nothing about.

    The view is opened at ``entry_ms - 1``, not ``entry_ms``: our own buy lands in ``swaps``
    at the entry instant, and a feature that can see the trade it is supposed to be deciding
    is the failure mode this whole module exists to avoid.
    """
    view = replay.PointInTime(conn, entry_ms - 1)
    values: dict[str, Observation] = {}
    raw: dict[str, float | None] = {}
    notes: list[str] = []

    creation = index.creation(chain, token)
    created_ms = creation.created_ms if creation else None
    if creation is not None and creation.ts_ms >= entry_ms:
        # We only learned the token existed after the entry; treat creation as unknown.
        creation = None
        created_ms = None
        notes.append("token.created event is later than the entry, so creation facts are withheld")

    # ---------------------------------------------------------------- copycat reuse
    if creation is None:
        values["copycat_reuse"] = Observation.unavailable(
            "no token.created event held at or before the entry"
        )
    else:
        by_uri, by_name = index.reuse_before(chain, token, entry_ms)
        reuses = by_uri + by_name
        values["copycat_reuse"] = Observation(
            value=-1.0 if reuses > 0 else 1.0,
            basis=EvidenceBasis.DERIVED,
            support=float(reuses),
            proxy=True,
            detail=(
                f"metadata reuse proxy: {by_uri} earlier mint(s) share the metadata URI, "
                f"{by_name} share name+symbol. NOT the paper's IPFS image content hash, which "
                "our dedup registry resolves against its present contents and therefore cannot "
                "be replayed as of a past instant"
            ),
        )
        notes.append("copycat_reuse is a metadata-URI proxy, not the published image-hash variable")
    raw["copycat_reuse"] = values["copycat_reuse"].value

    # ---------------------------------------------------------------- creator history
    if creation is None or not creation.creator:
        values["creator_graduation_history"] = Observation.unavailable(
            "no creator recorded on a token.created event at or before the entry"
        )
    else:
        prior = index.prior_launches(chain, creation.creator, entry_ms, token)
        if not prior:
            values["creator_graduation_history"] = Observation.unavailable(
                "creator has no prior launch we had seen before this entry"
            )
        else:
            graduated = index.graduated_before(chain, [m for _, m in prior], entry_ms)
            values["creator_graduation_history"] = Observation(
                value=graduated / float(len(prior)),
                basis=EvidenceBasis.DERIVED,
                support=float(len(prior)),
                detail=(
                    f"{graduated}/{len(prior)} of this creator's previously seen launches had "
                    "migrated before the entry"
                ),
            )
    raw["creator_graduation_history"] = values["creator_graduation_history"].value

    # ---------------------------------------------------------------- swap stream
    swaps = view.rows(
        "swaps",
        where="chain=? AND token=?",
        params=(chain, token),
        order_by="ts_ms ASC, id ASC",
    )
    truncated = False
    if len(swaps) > max_swap_rows:
        swaps = swaps[:max_swap_rows]
        truncated = True

    if swaps:
        wt1, wt2, combined = confluence.wash_transactions(swaps)
        values["wash_trading"] = Observation(
            value=float(combined),
            basis=EvidenceBasis.DERIVED,
            support=float(len(swaps)),
            detail=(
                f"WT1={wt1} atomic, WT2={wt2} in a {confluence.WASH_WINDOW_MS}ms window over "
                f"{len(swaps)} pre-entry swaps" + (" (TRUNCATED)" if truncated else "")
            ),
        )
        share, in_window = confluence.early_turnover_share(swaps, created_ms)
        if share is None:
            values["bot_dominated_early_activity"] = Observation.unavailable(
                "no swaps inside the early window before the entry"
            )
        else:
            values["bot_dominated_early_activity"] = Observation(
                value=share,
                basis=EvidenceBasis.ESTIMATED,
                support=float(in_window),
                proxy=True,
                detail=f"turnover concentration over {in_window} early pre-entry swaps",
            )
    else:
        values["wash_trading"] = Observation.unavailable("no swap rows before the entry")
        values["bot_dominated_early_activity"] = Observation.unavailable("no swap rows before the entry")
    raw["wash_trading"] = values["wash_trading"].value
    raw["bot_dominated_early_activity"] = values["bot_dominated_early_activity"].value

    # ---------------------------------------------------------------- curve velocity
    values["curve_velocity_sol_per_swap"] = _velocity_before(view, chain, token, swaps, created_ms)
    raw["curve_velocity_sol_per_swap"] = values["curve_velocity_sol_per_swap"].value

    # ---------------------------------------------------------------- bundle adjusted
    values["bundle_adjusted_concentration"] = Observation.unavailable(
        "MELT's bundle-adjusted top-10 delta needs a holder list; kaiba stores none, so this "
        "variable cannot be read at all - in replay or live"
    )
    raw["bundle_adjusted_concentration"] = None

    # ---------------------------------------------------------------- dossier readings
    dossier_row = view.one(
        "token_dossiers", where="chain=? AND address=?", params=(chain, token), order_by="built_at_ms DESC"
    )
    dossier = _jload(dossier_row.get("dossier_json")) if dossier_row else {}
    dossier_names = (
        "raw_top10_concentration",
        "raw_top10_threshold",
        "dev_bought_own_bundle",
        "dev_supply_threshold",
        "bundler_exposure_threshold",
        "sniper_exposure",
        "sniper_exposure_threshold",
        "liquidity_floor",
        "freeze_authority_live",
        "wallet_pnl_grade",
    )
    if not dossier:
        for name in dossier_names:
            values[name] = Observation.unavailable("no dossier built before the entry")
            raw[name] = None
    else:
        top10, top10_basis = _measure(dossier.get("top10_pct"))
        dev, dev_basis = _measure(dossier.get("dev_pct"))
        bundler, bundler_basis = _measure(dossier.get("bundler_pct"))
        sniper, sniper_basis = _measure(dossier.get("sniper_pct"))
        liquidity, liquidity_basis = _measure(dossier.get("liquidity_usd"))
        values["raw_top10_concentration"] = Observation(top10, top10_basis, detail="top-10 %")
        values["raw_top10_threshold"] = Observation(top10, top10_basis, detail="top-10 %")
        values["dev_bought_own_bundle"] = Observation(dev, dev_basis, detail="dev supply %")
        values["dev_supply_threshold"] = Observation(dev, dev_basis, detail="dev supply %")
        values["bundler_exposure_threshold"] = Observation(bundler, bundler_basis, detail="bundler %")
        values["sniper_exposure"] = Observation(sniper, sniper_basis, detail="sniper supply %")
        values["sniper_exposure_threshold"] = Observation(sniper, sniper_basis, detail="sniper supply %")
        values["liquidity_floor"] = Observation(liquidity, liquidity_basis, detail="liquidity USD")
        frozen = dossier.get("freeze_authority_revoked")
        values["freeze_authority_live"] = (
            Observation(
                value=0.0 if frozen else 1.0,
                basis=EvidenceBasis.PROVIDER_REPORTED,
                detail="freeze authority revoked" if frozen else "freeze authority still live",
            )
            if frozen is not None
            else Observation.unavailable("freeze authority state not established")
        )
        graded = dossier.get("graded_wallets")
        values["wallet_pnl_grade"] = (
            Observation(
                value=float(len(graded)),
                basis=EvidenceBasis.DERIVED,
                detail=f"{len(graded)} graded wallets seen on this token",
            )
            if isinstance(graded, list)
            else Observation.unavailable("no graded wallets recorded")
        )
        for name in dossier_names:
            raw[name] = values[name].value
        notes.append("dossier readings were taken at scan time before the entry, so they are stale, not leaked")

    # ---------------------------------------------------------------- entity count
    values["independent_entity_count"] = Observation(
        value=None,
        basis=EvidenceBasis.UNAVAILABLE,
        support=float(len({str(r.get("wallet")) for r in swaps if r.get("wallet")})) if swaps else None,
        detail=(
            "entity_members and entities carry no timestamp, so membership cannot be "
            "reconstructed for a past instant (replay.NO_OBSERVATION_TIME). The pre-entry "
            "distinct-ADDRESS count is carried as support and is a different, weaker quantity"
        ),
    )
    raw["independent_entity_count"] = None

    # ---------------------------------------------------------------- candidates
    raw.update(_candidates(view, chain, token, swaps, entry_ms, created_ms))

    if truncated:
        notes.append(f"pre-entry swap stream truncated to the earliest {max_swap_rows} rows")
    return values, raw, notes


def _measure(payload: Any) -> tuple[float | None, EvidenceBasis]:
    """Pull a value and its basis out of a serialised ``Measure``, like confluence does."""
    if not isinstance(payload, Mapping):
        return None, EvidenceBasis.UNAVAILABLE
    value = _float(payload.get("value"))
    if value is None:
        return None, EvidenceBasis.UNAVAILABLE
    try:
        basis = EvidenceBasis(str(payload.get("basis", EvidenceBasis.UNAVAILABLE.value)))
    except ValueError:
        basis = EvidenceBasis.UNAVAILABLE
    return value, basis


def _velocity_before(
    view: replay.PointInTime,
    chain: str,
    token: str,
    swaps: Sequence[Mapping[str, Any]],
    created_ms: int | None,
) -> Observation:
    """SOL per swap, by the same two routes confluence prefers, read point-in-time."""
    from kaiba.ingest import token_flow

    snapshots = view.rows(
        "curve_snapshots",
        where="chain=? AND token=?",
        params=(chain, token),
        order_by="observed_ms DESC",
        limit=2,
    )
    if len(snapshots) >= 2:
        newest, older = snapshots[0], snapshots[1]
        lo = _int(older.get("observed_ms")) or 0
        hi = _int(newest.get("observed_ms")) or 0
        window = [r for r in swaps if lo < (_int(r.get("ts_ms")) or 0) <= hi]
        result = token_flow.velocity_between(older, newer=newest, trades=len(window))
        value = _float(result.get("sol_per_swap"))
        if value is not None:
            return Observation(
                value=value,
                basis=EvidenceBasis.DERIVED,
                support=float(len(window)),
                detail=f"delta of two pre-entry snapshots, {len(window)} trades in the interval",
            )

    # Gross inflow per buy, off our own pre-entry rows, only valid when covered from launch.
    if created_ms is None:
        return Observation.unavailable("creation time unknown, so swap coverage cannot be checked")
    if not swaps:
        return Observation.unavailable("no swap rows before the entry")
    first_ms = min((_int(r.get("ts_ms")) or 0) for r in swaps)
    lag_s = (first_ms - created_ms) / 1000.0
    if lag_s > 60:
        return Observation.unavailable(f"swap coverage starts {lag_s:.0f}s after launch")
    buys = [r for r in swaps if str(r.get("side") or "").lower() == "buy"]
    inflow = sum((_int(r.get("amount_native")) or 0) for r in buys)
    if not buys or inflow <= 0:
        return Observation.unavailable("no priced buy-side flow before the entry")
    return Observation(
        value=(inflow / LAMPORTS_PER_SOL) / float(len(buys)),
        basis=EvidenceBasis.ESTIMATED,
        support=float(len(buys)),
        proxy=True,
        detail=f"gross inflow {inflow / LAMPORTS_PER_SOL:.4f} SOL over {len(buys)} pre-entry buys",
    )


def _candidates(
    view: replay.PointInTime,
    chain: str,
    token: str,
    swaps: Sequence[Mapping[str, Any]],
    entry_ms: int,
    created_ms: int | None,
) -> dict[str, float | None]:
    """The cheap tape-derived candidates. Every one reads only ``swaps`` before the entry."""
    out: dict[str, float | None] = dict.fromkeys(CANDIDATE_VARIABLES)
    if swaps:
        buys = [r for r in swaps if str(r.get("side") or "").lower() == "buy"]
        sells = [r for r in swaps if str(r.get("side") or "").lower() == "sell"]
        wallets = {str(r.get("wallet")) for r in swaps if r.get("wallet")}
        buyers = {str(r.get("wallet")) for r in buys if r.get("wallet")}
        buy_native = sum((_int(r.get("amount_native")) or 0) for r in buys)
        sell_native = sum((_int(r.get("amount_native")) or 0) for r in sells)
        out["pre_entry_swaps"] = float(len(swaps))
        out["pre_entry_unique_wallets"] = float(len(wallets))
        out["pre_entry_unique_buyers"] = float(len(buyers))
        out["pre_entry_buy_share"] = len(buys) / float(len(swaps))
        out["pre_entry_net_sol_inflow"] = (buy_native - sell_native) / LAMPORTS_PER_SOL
        out["pre_entry_largest_buy_sol"] = (
            max((_int(r.get("amount_native")) or 0) for r in buys) / LAMPORTS_PER_SOL if buys else 0.0
        )
        priced = _pre_entry_prices(chain, swaps)
        if priced:
            last_ts, last_px = priced[-1]
            first_px = priced[0][1]
            peak = max(p for _, p in priced)
            out["pre_entry_run_from_first_pct"] = (last_px / first_px - 1.0) * 100.0
            out["pre_entry_drawdown_from_peak_pct"] = (last_px / peak - 1.0) * 100.0
            window = [(ts, p) for ts, p in priced if ts >= entry_ms - 300_000]
            if len(window) >= 2:
                out["pre_entry_run_last_5m_pct"] = (window[-1][1] / window[0][1] - 1.0) * 100.0
            recent = [r for r in swaps if (_int(r.get("ts_ms")) or 0) >= entry_ms - 300_000]
            out["pre_entry_swaps_per_min_5m"] = len(recent) / 5.0
    if created_ms is not None:
        out["token_age_at_entry_s"] = max(0.0, (entry_ms - created_ms) / 1000.0)

    bundle = view.one(
        "token_bundles", where="chain=? AND token=?", params=(chain, token), order_by="computed_ms DESC"
    )
    if bundle is not None:
        out["bundled_pct_at_launch"] = _float(bundle.get("bundled_pct"))
        out["sniped_pct_at_launch"] = _float(bundle.get("sniped_pct"))
    return out


# --------------------------------------------------------------------------------------
# forward returns
# --------------------------------------------------------------------------------------


def _priced_series(conn: sqlite3.Connection, chain: str, token: str) -> list[tuple[int, float]]:
    """The token's priced prints from ONE swap source, oldest first.

    ``swaps`` mixes feeds that disagree print by print (MEASURED 2026-10-04 on the box: 6.3%
    of sol ``gmgn:smartmoney`` / ``pumpfun:trades`` prints of one token within 60 s differ
    by more than 2x). An entry priced on one feed and an exit on another is a return made
    of two scales, so the series is the one source
    :func:`kaiba.intelligence.deployer.series_source` picks: the most priced prints.
    """
    rows = conn.execute(
        "SELECT ts_ms, price_usd, source FROM swaps WHERE chain=? AND token=? "
        "AND price_usd IS NOT NULL ORDER BY ts_ms, id",
        (chain, token),
    ).fetchall()
    priced: list[tuple[int, float, str]] = []
    for ts_ms, price, source in rows:
        value = _float(price)
        if ts_ms is None or value is None or value <= 0.0:
            continue
        priced.append((int(ts_ms), value, str(source)))
    return single_source(priced, chain)


def _pre_entry_prices(chain: str, swaps: Sequence[Mapping[str, Any]]) -> list[tuple[int, float]]:
    """Pre-entry prints from ONE source, chosen among the pre-entry rows only.

    Chosen on what was observable before the entry, so a print written after it cannot move
    which feed the pre-entry features are read from.
    """
    priced = [
        (int(r["ts_ms"]), p, str(r.get("source")))
        for r in swaps
        if (p := _price(r)) is not None and r.get("ts_ms")
    ]
    return single_source(priced, chain)


def forward_returns(
    series: Sequence[tuple[int, float]],
    entry_ms: int,
    entry_price: float,
    horizons_min: Sequence[int],
    cost_bps: int,
    *,
    after_ms: int | None = None,
) -> dict[int, Forward]:
    """What the tape did, at each horizon, in both the strict and the carry convention.

    ``strict`` requires a print at or after ``entry + horizon``. ``carry`` marks to the last
    print inside the window. Neither is right and both are carried, because the choice
    between them is the single largest bias in this module:

    *strict* keeps only tokens whose tape reaches the horizon, and a token's tape reaches
    the horizon when our collector kept pulling it. At 60 minutes that is 1,241 of 4,604
    Solana tokens (MEASURED 2026-09-22) - a 27% survival filter applied by our own polling
    schedule, not by the market.

    *carry* keeps any token that printed at all inside the window - 2,776 at 60 minutes -
    and marks the position at that last print. It understates a collapse that happened after
    the last print we hold.

    A horizon with neither is ``censored`` and returns ``None``. It is NOT scored as a zero
    or as a death: MEASURED 2026-09-22, a Solana token's coverage end never runs past its own
    last print on this box, so "no more prints" is a statement about our collector and not
    about the token.
    """
    out: dict[int, Forward] = {}
    # Horizons are measured from the decision instant; the exit may only use prints strictly
    # after the fill, so a fill and its own exit can never be the same trade.
    cursor = entry_ms if after_ms is None else after_ms
    after = [(ts, px) for ts, px in series if ts > cursor]
    last_print_ms = after[-1][0] if after else None
    for horizon in horizons_min:
        deadline = entry_ms + horizon * 60_000
        at_or_after = next((px for ts, px in after if ts >= deadline), None)
        upto = [px for ts, px in after if ts <= deadline]
        carry_px = upto[-1] if upto else None
        out[horizon] = Forward(
            horizon_min=horizon,
            gross_strict_pct=(at_or_after / entry_price - 1.0) * 100.0 if at_or_after else None,
            net_strict_pct=net_pct(entry_price, at_or_after, cost_bps) if at_or_after else None,
            gross_carry_pct=(carry_px / entry_price - 1.0) * 100.0 if carry_px else None,
            net_carry_pct=net_pct(entry_price, carry_px, cost_bps) if carry_px else None,
            prints_in_window=len(upto),
            tape_ended=at_or_after is None,
            censored=at_or_after is None and carry_px is None,
            last_print_ms=last_print_ms,
        )
    return out


# --------------------------------------------------------------------------------------
# entries
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Entry:
    chain: str
    token: str
    entry_ms: int
    source: SourceKind
    ref: str
    lane: str | None = None
    realised_pnl_pct: float | None = None
    realised_exit_reason: str | None = None
    action: str | None = None
    dossier_grade: str | None = None


def collect_entries(
    conn: sqlite3.Connection,
    *,
    sources: Sequence[SourceKind] = TRUST_ORDER,
    chains: Sequence[str] = ("sol",),
    pseudo_entry_lag_ms: int = PSEUDO_ENTRY_LAG_MS,
    min_prints_before: int = MIN_PRINTS_BEFORE_PSEUDO_ENTRY,
) -> list[Entry]:
    """Every instant we could label, from all four sources, deduplicated within a source."""
    wanted = set(sources)
    chain_set = set(chains)
    out: list[Entry] = []

    if SourceKind.LIVE in wanted or SourceKind.SHADOW in wanted:
        pnl_by_position = {
            str(r[0]): (_float(r[1]), _text(r[2]))
            for r in conn.execute("SELECT position_id, pnl_pct, exit_reason FROM trades")
        }
        for row in conn.execute(
            "SELECT position_id, chain, token, lane, mode, opened_ms, exit_reason FROM positions "
            "WHERE opened_ms IS NOT NULL"
        ):
            position_id, chain, token, lane, mode, opened_ms, exit_reason = row
            kind = SourceKind.LIVE if str(mode) == "live" else SourceKind.SHADOW
            if kind not in wanted or str(chain) not in chain_set:
                continue
            pnl, trade_exit = pnl_by_position.get(str(position_id), (None, None))
            out.append(
                Entry(
                    chain=str(chain),
                    token=str(token),
                    entry_ms=int(opened_ms),
                    source=kind,
                    ref=str(position_id),
                    lane=_text(lane),
                    realised_pnl_pct=pnl,
                    realised_exit_reason=trade_exit or _text(exit_reason),
                )
            )

    if SourceKind.DECISION in wanted:
        for row in conn.execute(
            "SELECT decision_id, ts_ms, chain, token, lane, action, dossier_grade FROM decisions "
            "WHERE ts_ms IS NOT NULL AND token IS NOT NULL"
        ):
            decision_id, ts_ms, chain, token, lane, action, grade = row
            if str(chain) not in chain_set:
                continue
            out.append(
                Entry(
                    chain=str(chain),
                    token=str(token),
                    entry_ms=int(ts_ms),
                    source=SourceKind.DECISION,
                    ref=str(decision_id),
                    lane=_text(lane),
                    action=_text(action),
                    dossier_grade=_text(grade),
                )
            )

    if SourceKind.TAPE in wanted:
        # Per source, so the pseudo-entry starts on the series it will be priced from
        # (``_priced_series``): the first print of the source with the most prints.
        per_token: dict[tuple[str, str], dict[str, tuple[int, int]]] = {}
        for chain, token, source, first_ms, prints in conn.execute(
            "SELECT chain, token, source, MIN(ts_ms), COUNT(*) FROM swaps "
            "WHERE price_usd IS NOT NULL AND CAST(price_usd AS REAL) > 0 "
            "GROUP BY chain, token, source"
        ):
            if str(chain) not in chain_set or first_ms is None:
                continue
            per_token.setdefault((str(chain), str(token)), {})[str(source)] = (
                int(first_ms), int(prints)
            )
        for (chain, token), by_source in per_token.items():
            pick = series_source({s: n for s, (_f, n) in by_source.items()}, chain)
            if pick is None:
                continue
            first_ms, prints = by_source[pick]
            if prints < min_prints_before:
                continue
            out.append(
                Entry(
                    chain=str(chain),
                    token=str(token),
                    entry_ms=int(first_ms) + int(pseudo_entry_lag_ms),
                    source=SourceKind.TAPE,
                    ref=f"tape:{token}",
                )
            )
    return out


# --------------------------------------------------------------------------------------
# dataset
# --------------------------------------------------------------------------------------


def build_dataset(
    conn: sqlite3.Connection,
    *,
    sources: Sequence[SourceKind] = TRUST_ORDER,
    chains: Sequence[str] = ("sol",),
    horizons_min: Sequence[int] = DEFAULT_HORIZONS_MIN,
    cost_bps: int = COST_BPS_ROUND_TRIP,
    pseudo_entry_lag_ms: int = PSEUDO_ENTRY_LAG_MS,
    min_prints_before: int = MIN_PRINTS_BEFORE_PSEUDO_ENTRY,
    max_entry_price_age_ms: int = MAX_ENTRY_PRICE_AGE_MS,
    max_swap_rows: int = MAX_SWAP_ROWS,
    entry_fill: EntryFill = EntryFill.NEXT_PRINT,
    max_fill_lag_ms: int = MAX_FILL_LAG_MS,
    weights: WeightSet | None = None,
    limit: int | None = None,
) -> Dataset:
    """The labelled dataset. One row per entry that survives the census.

    The census is returned with the rows and names every reason an entry was dropped, so
    "1,900 rows" is never reported without "out of 4,100 considered, dropped for these".
    """
    weight_set = weights or LITERATURE_V1
    index = EventIndex(conn)
    entries = collect_entries(
        conn,
        sources=sources,
        chains=chains,
        pseudo_entry_lag_ms=pseudo_entry_lag_ms,
        min_prints_before=min_prints_before,
    )
    entries.sort(key=lambda e: (e.entry_ms, e.token, e.source.value))
    if limit is not None:
        entries = entries[:limit]

    dropped: dict[str, int] = {}
    rows: list[OutcomeRow] = []
    series_cache: dict[tuple[str, str], list[tuple[int, float]]] = {}

    def drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    for entry in entries:
        key = (entry.chain, entry.token)
        series = series_cache.get(key)
        if series is None:
            series = _priced_series(conn, entry.chain, entry.token)
            series_cache[key] = series
        if not series:
            drop("no_priced_tape")
            continue
        before = [(ts, px) for ts, px in series if ts < entry.entry_ms]
        if not before:
            drop("no_price_before_entry")
            continue
        entry_ts, entry_price = before[-1]
        age = entry.entry_ms - entry_ts
        if age > max_entry_price_age_ms:
            drop("entry_price_stale")
            continue
        next_print = next(((ts, px) for ts, px in series if ts > entry.entry_ms), None)
        next_move = (next_print[1] / entry_price - 1.0) * 100.0 if next_print else None
        if entry_fill is EntryFill.NEXT_PRINT:
            if next_print is None:
                drop("no_next_print_to_fill_at")
                continue
            if next_print[0] - entry.entry_ms > max_fill_lag_ms:
                drop("next_print_too_late_to_be_a_fill")
                continue
            fill_ms, fill_price = next_print
        else:
            fill_ms, fill_price = entry_ts, entry_price
        forwards = forward_returns(
            series, entry.entry_ms, fill_price, horizons_min, cost_bps, after_ms=fill_ms
        )
        if all(f.censored for f in forwards.values()):
            # Our collector stopped before the first horizon. That is censoring, not a
            # result, and a row with no label at any horizon cannot be scored.
            drop("censored_before_every_horizon")
            continue

        observations, raw, notes = observe_before_entry(
            conn, entry.chain, entry.token, entry.entry_ms, index=index, max_swap_rows=max_swap_rows
        )
        mapped = map_observations(observations, weight_set)
        rows.append(
            OutcomeRow(
                chain=entry.chain,
                token=entry.token,
                entry_ms=entry.entry_ms,
                source=entry.source,
                ref=entry.ref,
                lane=entry.lane,
                entry_price_usd=entry_price,
                entry_price_age_ms=age,
                fill_price_usd=fill_price,
                fill_ms=fill_ms,
                fill=entry_fill,
                next_print_move_pct=next_move,
                observations=observations,
                mapped=mapped,
                raw=raw,
                forwards=forwards,
                realised_pnl_pct=entry.realised_pnl_pct,
                realised_exit_reason=entry.realised_exit_reason,
                action=entry.action,
                dossier_grade=entry.dossier_grade,
                notes=tuple(notes),
            )
        )

    return Dataset(
        rows=tuple(rows),
        census=Census(considered=len(entries), kept=len(rows), dropped=dropped),
        cost_bps=cost_bps,
        horizons_min=tuple(horizons_min),
        built_from=MODEL_VERSION,
    )


def map_observations(
    observations: Mapping[str, Observation], weights: WeightSet | None = None
) -> dict[str, float | None]:
    """Each LITERATURE_V1 variable's published mapping applied to its reading.

    ``None`` where the reading is missing or the mapping refuses it - which is exactly how
    the scorer treats it, so the arms below and the score agree by construction.
    """
    weight_set = weights or LITERATURE_V1
    out: dict[str, float | None] = {}
    for spec in weight_set.variables:
        obs = observations.get(spec.name)
        if obs is None or not obs.known:
            out[spec.name] = None
            continue
        fn = MAPPINGS.get(spec.mapping)
        if fn is None:
            out[spec.name] = None
            continue
        try:
            out[spec.name] = fn(obs, spec.params)
        except (TypeError, ValueError, KeyError, ZeroDivisionError):
            out[spec.name] = None
    return out


# --------------------------------------------------------------------------------------
# separation
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Arm:
    n: int
    wins: int
    mean_net_pct: float | None
    median_net_pct: float | None

    @property
    def win_rate(self) -> float | None:
        return (self.wins / self.n) if self.n else None


@dataclass(frozen=True, slots=True)
class ArmPair:
    good: Arm
    bad: Arm

    @property
    def half_separation_nats(self) -> float | None:
        """Half the log-odds separation in win rate, the unit confluence weights are in.

        Haldane-Anscombe: half a win and half a loss are added to each arm, so an arm that
        went 0-for-9 produces a finite number instead of an infinity that would read as a
        gigantic effect.
        """
        if not self.good.n or not self.bad.n:
            return None
        pg = (self.good.wins + 0.5) / (self.good.n + 1.0)
        pb = (self.bad.wins + 0.5) / (self.bad.n + 1.0)
        return (_logit(pg) - _logit(pb)) / 2.0

    @property
    def mean_gap_pct(self) -> float | None:
        if self.good.mean_net_pct is None or self.bad.mean_net_pct is None:
            return None
        return self.good.mean_net_pct - self.bad.mean_net_pct


@dataclass(frozen=True, slots=True)
class Separation:
    variable: str
    horizon_min: int
    grade: str
    split: str
    claimed_half_nats: float | None
    rule: str
    in_sample: ArmPair | None
    out_of_sample: ArmPair | None
    oos_half_nats_ci: tuple[float, float] | None
    oos_mean_gap_ci: tuple[float, float] | None
    coverage_oos: float

    @property
    def measured_half_nats(self) -> float | None:
        return self.out_of_sample.half_separation_nats if self.out_of_sample else None

    @property
    def survives(self) -> bool:
        """True only when the out-of-sample interval on the win-rate separation excludes 0."""
        ci = self.oos_half_nats_ci
        return bool(ci and (ci[0] > 0.0 or ci[1] < 0.0))

    @property
    def economically_survives(self) -> bool:
        """True only when the out-of-sample interval on the NET mean gap excludes 0."""
        ci = self.oos_mean_gap_ci
        return bool(ci and (ci[0] > 0.0 or ci[1] < 0.0))


def _arm(values: Sequence[float]) -> Arm:
    if not values:
        return Arm(n=0, wins=0, mean_net_pct=None, median_net_pct=None)
    ordered = sorted(values)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0
    return Arm(
        n=len(values),
        wins=sum(1 for v in values if v > 0.0),
        mean_net_pct=sum(values) / len(values),
        median_net_pct=median,
    )


SplitFn = Callable[[OutcomeRow], bool | None]


def _row_key(row: OutcomeRow) -> tuple[str, str, str, int]:
    """Identity of a row, so a score computed once can be looked up again."""
    return (row.source.value, row.ref, row.token, row.entry_ms)


def _published_split(name: str) -> SplitFn:
    def fn(row: OutcomeRow) -> bool | None:
        value = row.mapped.get(name)
        if value is None or value == 0.0:
            return None
        return value > 0.0

    return fn


def _fitted_split(name: str, threshold: float, higher_is_good: bool) -> SplitFn:
    def fn(row: OutcomeRow) -> bool | None:
        value = row.raw.get(name)
        if value is None:
            return None
        above = value > threshold
        return above if higher_is_good else not above

    return fn


def _fit_median_rule(
    fit_rows: Sequence[OutcomeRow], name: str, horizon: int, *, strict: bool
) -> tuple[SplitFn, str] | None:
    """Threshold and direction, learned on the earlier half ONLY, then frozen."""
    pairs = [
        (v, r)
        for r in fit_rows
        if (v := r.raw.get(name)) is not None and r.net(horizon, strict=strict) is not None
    ]
    if len(pairs) < 4:
        return None
    ordered = sorted(v for v, _ in pairs)
    mid = len(ordered) // 2
    threshold = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0
    above = [r.net(horizon, strict=strict) for v, r in pairs if v > threshold]
    below = [r.net(horizon, strict=strict) for v, r in pairs if v <= threshold]
    above = [v for v in above if v is not None]
    below = [v for v in below if v is not None]
    if not above or not below:
        return None
    higher_is_good = (sum(above) / len(above)) >= (sum(below) / len(below))
    direction = "higher" if higher_is_good else "lower"
    return (
        _fitted_split(name, threshold, higher_is_good),
        f"{direction} than {threshold:.6g} (median fitted on the earlier half)",
    )


def _pair(rows: Sequence[OutcomeRow], split: SplitFn, horizon: int, *, strict: bool) -> tuple[ArmPair, int]:
    good: list[float] = []
    bad: list[float] = []
    used = 0
    for row in rows:
        ret = row.net(horizon, strict=strict)
        if ret is None:
            continue
        side = split(row)
        if side is None:
            continue
        used += 1
        (good if side else bad).append(ret)
    return ArmPair(good=_arm(good), bad=_arm(bad)), used


def _bootstrap(
    rows: Sequence[OutcomeRow],
    split: SplitFn,
    horizon: int,
    *,
    strict: bool,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    """Percentile intervals for the win-rate separation and the net mean gap.

    A bootstrap is a description of how wide this sample is, not a significance test. The
    only significance statements in this module come from :mod:`kaiba.learning.gates`.
    """
    usable = [
        (side, ret)
        for row in rows
        if (ret := row.net(horizon, strict=strict)) is not None and (side := split(row)) is not None
    ]
    if len(usable) < 8:
        return None, None
    rng = random.Random(BOOTSTRAP_SEED)
    seps: list[float] = []
    gaps: list[float] = []
    n = len(usable)
    for _ in range(resamples):
        sample = [usable[rng.randrange(n)] for _ in range(n)]
        good = [r for s, r in sample if s]
        bad = [r for s, r in sample if not s]
        if not good or not bad:
            continue
        pg = (sum(1 for v in good if v > 0) + 0.5) / (len(good) + 1.0)
        pb = (sum(1 for v in bad if v > 0) + 0.5) / (len(bad) + 1.0)
        seps.append((_logit(pg) - _logit(pb)) / 2.0)
        gaps.append(sum(good) / len(good) - sum(bad) / len(bad))
    if len(seps) < resamples // 4:
        return None, None

    def interval(values: list[float]) -> tuple[float, float]:
        values.sort()
        lo = values[int(0.05 * (len(values) - 1))]
        hi = values[int(0.95 * (len(values) - 1))]
        return lo, hi

    return interval(seps), interval(gaps)


def measure_variable(
    fit: Dataset,
    oos: Dataset,
    name: str,
    *,
    horizon_min: int,
    strict: bool = False,
    weights: WeightSet | None = None,
) -> Separation | None:
    """One variable's separation, in-sample and out-of-sample, on a time split.

    A LITERATURE_V1 variable with a published mapping is split on the sign of that mapping -
    nothing is fitted, so the out-of-sample number is a clean test of the paper's claim. Any
    other variable (including the report-only ones, whose weight is deliberately zero) gets a
    median threshold and a direction fitted on the earlier half and frozen.
    """
    weight_set = weights or LITERATURE_V1
    spec = weight_set.by_name().get(name)
    if spec is not None and spec.mapping != "report_only":
        split: SplitFn = _published_split(name)
        rule = f"sign of the published '{spec.mapping}' mapping"
        split_kind = "published_mapping"
        grade = str(spec.grade)
        claimed = spec.weight_nats if spec.weight_nats > 0 else None
    else:
        fitted = _fit_median_rule(fit.rows, name, horizon_min, strict=strict)
        if fitted is None:
            return None
        split, rule = fitted
        split_kind = "fitted_median"
        grade = str(spec.grade) if spec is not None else "candidate"
        claimed = None

    in_pair, _ = _pair(fit.rows, split, horizon_min, strict=strict)
    oos_pair, used = _pair(oos.rows, split, horizon_min, strict=strict)
    sep_ci, gap_ci = _bootstrap(oos.rows, split, horizon_min, strict=strict)
    labelled = sum(1 for r in oos.rows if r.net(horizon_min, strict=strict) is not None)
    return Separation(
        variable=name,
        horizon_min=horizon_min,
        grade=grade,
        split=split_kind,
        claimed_half_nats=claimed,
        rule=rule,
        in_sample=in_pair,
        out_of_sample=oos_pair,
        oos_half_nats_ci=sep_ci,
        oos_mean_gap_ci=gap_ci,
        coverage_oos=(used / labelled) if labelled else 0.0,
    )


def rank_variables(
    fit: Dataset,
    oos: Dataset,
    *,
    horizon_min: int,
    strict: bool = False,
    weights: WeightSet | None = None,
    include_candidates: bool = True,
) -> list[Separation]:
    """Every variable, ranked by the size of its OUT-OF-SAMPLE win-rate separation."""
    weight_set = weights or LITERATURE_V1
    names = [spec.name for spec in weight_set.variables]
    if include_candidates:
        names += list(CANDIDATE_VARIABLES)
    out: list[Separation] = []
    for name in names:
        got = measure_variable(fit, oos, name, horizon_min=horizon_min, strict=strict, weights=weight_set)
        if got is not None:
            out.append(got)
    out.sort(key=lambda s: abs(s.measured_half_nats or 0.0), reverse=True)
    return out


# --------------------------------------------------------------------------------------
# the combination
# --------------------------------------------------------------------------------------


def score_rows(rows: Sequence[OutcomeRow], weights: WeightSet | None = None) -> list[tuple[OutcomeRow, float, float]]:
    """``(row, evidenced nats, all-variables nats)`` from the real confluence scorer."""
    weight_set = weights or LITERATURE_V1
    out: list[tuple[OutcomeRow, float, float]] = []
    for row in rows:
        result = confluence.score(row.to_observations(), weight_set)
        out.append((row, result.score_evidenced_nats, result.score_all_nats))
    return out


def _spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Rank correlation. Descriptive only - the significance comes from the gates."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx = gates._ranks(list(xs))  # noqa: SLF001 - one ranking rule in this repo, not two
    ry = gates._ranks(list(ys))  # noqa: SLF001
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx <= 0 or dy <= 0:
        return None
    return num / (dx * dy)


@dataclass(frozen=True, slots=True)
class CombinedResult:
    horizon_min: int
    n_fit: int
    n_oos: int
    spearman_fit: float | None
    spearman_oos: float | None
    top_half: Arm
    bottom_half: Arm
    gap_ci: tuple[float, float] | None
    deflated_sharpe: float | None
    dsr_notes: tuple[str, ...]
    trials: int
    pbo: float | None
    note: str


def combined_gate(
    fit: Dataset,
    oos: Dataset,
    *,
    horizon_min: int,
    strict: bool = False,
    weights: WeightSet | None = None,
    trials: int | None = None,
    evidenced: bool = True,
) -> CombinedResult:
    """Does LITERATURE_V1, scored as a whole, rank better than chance out of sample?

    The selection rule is "score above the median of the EARLIER half" - a threshold fitted
    in-sample and applied unchanged out of sample. The deflated Sharpe is a per-trade Sharpe
    over the selected out-of-sample rows, not an annualised one, and ``trials`` is the number
    of configurations this module looked at, because a Sharpe that ignores the search is a
    number about nothing.
    """
    weight_set = weights or LITERATURE_V1
    fit_scored = [(row, (ev if evidenced else allv)) for row, ev, allv in score_rows(fit.rows, weight_set)]
    oos_scored = [(row, (ev if evidenced else allv)) for row, ev, allv in score_rows(oos.rows, weight_set)]

    fit_pairs = [(s, r) for r, s in fit_scored if r.net(horizon_min, strict=strict) is not None]
    oos_pairs = [(s, r) for r, s in oos_scored if r.net(horizon_min, strict=strict) is not None]
    spearman_fit = _spearman(
        [s for s, _ in fit_pairs], [r.net(horizon_min, strict=strict) or 0.0 for _, r in fit_pairs]
    )
    spearman_oos = _spearman(
        [s for s, _ in oos_pairs], [r.net(horizon_min, strict=strict) or 0.0 for _, r in oos_pairs]
    )

    if fit_pairs:
        scores = sorted(s for s, _ in fit_pairs)
        mid = len(scores) // 2
        cut = scores[mid] if len(scores) % 2 else (scores[mid - 1] + scores[mid]) / 2.0
    else:
        cut = 0.0
    cuts = {_row_key(r): s for s, r in oos_pairs}

    def split(row: OutcomeRow) -> bool | None:
        value = cuts.get(_row_key(row))
        return None if value is None else value > cut

    pair, _used = _pair(oos.rows, split, horizon_min, strict=strict)
    _, gap_ci = _bootstrap(oos.rows, split, horizon_min, strict=strict)

    selected = [
        r.net(horizon_min, strict=strict)
        for s, r in oos_pairs
        if s > cut and r.net(horizon_min, strict=strict) is not None
    ]
    fit_selected = [
        r.net(horizon_min, strict=strict)
        for s, r in fit_pairs
        if s > cut and r.net(horizon_min, strict=strict) is not None
    ]
    n_trials = trials if trials is not None else len(weight_set.variables) + len(CANDIDATE_VARIABLES)
    trial_sharpes = _trial_sharpes(fit, horizon_min, strict=strict, weights=weight_set)
    dsr, notes = gates.deflated_sharpe([v for v in selected if v is not None], n_trials, trial_sharpes=trial_sharpes)
    pbo = _pbo_matrix(fit, oos, horizon_min, strict=strict, weights=weight_set)

    return CombinedResult(
        horizon_min=horizon_min,
        n_fit=len(fit_pairs),
        n_oos=len(oos_pairs),
        spearman_fit=spearman_fit,
        spearman_oos=spearman_oos,
        top_half=pair.good,
        bottom_half=pair.bad,
        gap_ci=gap_ci,
        deflated_sharpe=dsr,
        dsr_notes=tuple(notes),
        trials=n_trials,
        pbo=pbo,
        note=(
            f"selection = {'evidenced' if evidenced else 'all-variables'} score above "
            f"{cut:.4f} nats, the median of the earlier half; "
            f"{len(fit_selected)} of {len(fit_pairs)} selected in-sample"
        ),
    )


def _trial_sharpes(
    fit: Dataset, horizon_min: int, *, strict: bool, weights: WeightSet
) -> list[float]:
    """Per-trade Sharpe of every single-variable rule on the FIT half.

    This is the cross-trial variance the deflation term needs. Without it,
    ``gates.deflated_sharpe`` says so and degrades to a plain PSR against zero.
    """
    out: list[float] = []
    for name in [s.name for s in weights.variables] + list(CANDIDATE_VARIABLES):
        spec = weights.by_name().get(name)
        if spec is not None and spec.mapping != "report_only":
            split: SplitFn = _published_split(name)
        else:
            fitted = _fit_median_rule(fit.rows, name, horizon_min, strict=strict)
            if fitted is None:
                continue
            split = fitted[0]
        picked = [
            r.net(horizon_min, strict=strict)
            for r in fit.rows
            if split(r) is True and r.net(horizon_min, strict=strict) is not None
        ]
        sharpe = gates._sharpe_raw([v for v in picked if v is not None])  # noqa: SLF001
        if sharpe is not None:
            out.append(sharpe)
    return out


def _pbo_matrix(
    fit: Dataset, oos: Dataset, horizon_min: int, *, strict: bool, weights: WeightSet
) -> float | None:
    """PBO over every single-variable configuration, on the whole span.

    CSCV does its own splitting, so it gets both halves. A cell is the mean net return of
    the rows that configuration selected inside that time bucket; a bucket a configuration
    selected nothing in gets 0.0, which is what "we did not trade" returns.
    """
    rows = sorted([*fit.rows, *oos.rows], key=lambda r: r.entry_ms)
    if len(rows) < PBO_PERIODS * 2:
        return None
    configs: list[SplitFn] = []
    for name in [s.name for s in weights.variables] + list(CANDIDATE_VARIABLES):
        spec = weights.by_name().get(name)
        if spec is not None and spec.mapping != "report_only":
            configs.append(_published_split(name))
        else:
            fitted = _fit_median_rule(fit.rows, name, horizon_min, strict=strict)
            if fitted is not None:
                configs.append(fitted[0])
    if len(configs) < 2:
        return None
    size = max(1, len(rows) // PBO_PERIODS)
    buckets = [rows[i : i + size] for i in range(0, len(rows), size)][:PBO_PERIODS]
    matrix: list[list[float]] = []
    for bucket in buckets:
        row_values: list[float] = []
        for split in configs:
            picked = [
                v
                for r in bucket
                if split(r) is True and (v := r.net(horizon_min, strict=strict)) is not None
            ]
            row_values.append(sum(picked) / len(picked) if picked else 0.0)
        matrix.append(row_values)
    return gates.pbo_cscv(matrix)


# --------------------------------------------------------------------------------------
# sweeps and reporting
# --------------------------------------------------------------------------------------


def sweep_pseudo_entry_lag(
    conn: sqlite3.Connection,
    lags_ms: Sequence[int] = (30_000, 60_000, 120_000),
    **kwargs: Any,
) -> dict[int, list[str]]:
    """Is the variable ranking an artefact of the INVENTED pseudo-entry lag?

    Returns the ranked variable names per lag so a reader can see whether the top of the
    table moves. It does not average them: averaging over a parameter nobody justified would
    hide exactly the instability this is for.
    """
    out: dict[int, list[str]] = {}
    horizon = int(kwargs.pop("horizon_min", 20))
    for lag in lags_ms:
        data = build_dataset(conn, pseudo_entry_lag_ms=lag, **kwargs)
        fit, oos = data.time_split()
        out[lag] = [s.variable for s in rank_variables(fit, oos, horizon_min=horizon)]
    return out


@dataclass(frozen=True, slots=True)
class Artefact:
    """How much of a variable's separation is an entry price nobody could have been filled at."""

    variable: str
    horizon_min: int
    jump_good_pct: float | None
    jump_bad_pct: float | None
    jump_gap_pct: float | None
    net_gap_pct: float | None
    n_good: int
    n_bad: int

    @property
    def explained_share(self) -> float | None:
        """Fraction of the measured net gap that the entry-price gap alone accounts for."""
        if self.jump_gap_pct is None or not self.net_gap_pct:
            return None
        return self.jump_gap_pct / self.net_gap_pct


def artefact_share(
    fit: Dataset,
    oos: Dataset,
    name: str,
    *,
    horizon_min: int,
    strict: bool = False,
    weights: WeightSet | None = None,
) -> Artefact | None:
    """The test that decides whether a separation is real on this dataset.

    Every entry here is priced at the last print BEFORE it, so the "return" silently
    includes the move from that print to the next trade - a move we would have paid, not
    earned. If a variable's good arm also has a bigger jump to the next print, its
    separation is that artefact wearing a variable's name.

    MEASURED 2026-09-22 at +20 min on the Solana one-row-per-token dataset: the jump gap
    accounts for 72% of ``pre_entry_swaps``, 76% of ``pre_entry_swaps_per_min_5m``, 53% of
    ``pre_entry_drawdown_from_peak_pct`` and 29% of ``pre_entry_buy_share`` - against 12%
    for ``bot_dominated_early_activity``, which is why that one is the only candidate left
    standing.
    """
    weight_set = weights or LITERATURE_V1
    spec = weight_set.by_name().get(name)
    if spec is not None and spec.mapping != "report_only":
        split: SplitFn = _published_split(name)
    else:
        fitted = _fit_median_rule(fit.rows, name, horizon_min, strict=strict)
        if fitted is None:
            return None
        split = fitted[0]
    good = [r.next_print_move_pct for r in oos.rows if split(r) is True and r.next_print_move_pct is not None]
    bad = [r.next_print_move_pct for r in oos.rows if split(r) is False and r.next_print_move_pct is not None]
    pair, _ = _pair(oos.rows, split, horizon_min, strict=strict)
    jg = sum(good) / len(good) if good else None
    jb = sum(bad) / len(bad) if bad else None
    return Artefact(
        variable=name,
        horizon_min=horizon_min,
        jump_good_pct=jg,
        jump_bad_pct=jb,
        jump_gap_pct=(jg - jb) if (jg is not None and jb is not None) else None,
        net_gap_pct=pair.mean_gap_pct,
        n_good=len(good),
        n_bad=len(bad),
    )


def entry_price_bias(dataset: Dataset) -> dict[str, tuple[int, float, float]]:
    """``{source: (n, mean, median)}`` of the move from the entry price to the next print.

    This is the fill we are pretending to get, per source. It is the first number to read
    before believing any level in this module.
    """
    out: dict[str, tuple[int, float, float]] = {}
    for kind in TRUST_ORDER:
        moves = sorted(
            r.next_print_move_pct
            for r in dataset.rows
            if r.source is kind and r.next_print_move_pct is not None
        )
        if not moves:
            continue
        mid = len(moves) // 2
        median = moves[mid] if len(moves) % 2 else (moves[mid - 1] + moves[mid]) / 2.0
        out[kind.value] = (len(moves), sum(moves) / len(moves), median)
    return out


@dataclass(frozen=True, slots=True)
class Stability:
    """One variable's verdict across every horizon and both label conventions."""

    variable: str
    grade: str
    views: int
    expected_views: int
    signs: tuple[int, ...]
    significant_views: int
    agreeing_views: int
    min_arm_n: int
    worst_gap_pct: float | None

    @property
    def sign_stable(self) -> bool:
        nonzero = {s for s in self.signs if s}
        return len(nonzero) == 1

    @property
    def verdict(self) -> str:
        """What we would actually let this variable do.

        SIZE is reserved for a variable that (a) keeps one sign in every view, (b) is
        significant out of sample in most of them, and (c) never rests on an arm under 30.
        Everything else is a note to the operator, not a lever.
        """
        if self.views < self.expected_views:
            return "incomplete"
        if self.min_arm_n < 30:
            return "n-too-small"
        if not self.sign_stable:
            return "label-artefact"
        if self.significant_views >= max(2, self.views - 1) and self.agreeing_views == self.views:
            return "SIZE"
        if self.significant_views >= 1:
            return "watch"
        return "no"


def stability(views: Mapping[str, Sequence[Separation]]) -> list[Stability]:
    """Collapse several (horizon, convention) rankings into one verdict per variable.

    This is the guard against the mistake this dataset invites. ``strict`` and ``carry``
    label the SAME entry differently, and a variable that separates under one and reverses
    under the other has not found an edge - it has found which tokens our collector kept
    polling. A sign flip across views is therefore fatal, and no amount of significance
    inside a single view rescues it.
    """
    per: dict[str, list[Separation]] = {}
    for seps in views.values():
        for sep in seps:
            per.setdefault(sep.variable, []).append(sep)
    out: list[Stability] = []
    for name, seps in per.items():
        measured = [s.measured_half_nats for s in seps]
        signs = tuple(0 if m is None else (1 if m > 0 else -1) for m in measured)
        significant = sum(1 for s in seps if s.survives)
        nonzero = [s for s in signs if s]
        majority = 1 if sum(1 for s in nonzero if s > 0) >= sum(1 for s in nonzero if s < 0) else -1
        agreeing = sum(1 for s in signs if s == majority)
        arms = [min(s.out_of_sample.good.n, s.out_of_sample.bad.n) for s in seps if s.out_of_sample]
        gaps = [s.out_of_sample.mean_gap_pct for s in seps if s.out_of_sample and s.out_of_sample.mean_gap_pct is not None]
        out.append(
            Stability(
                variable=name,
                grade=seps[0].grade,
                views=len(seps),
                expected_views=len(views),
                signs=signs,
                significant_views=significant,
                agreeing_views=agreeing,
                min_arm_n=min(arms) if arms else 0,
                worst_gap_pct=min(gaps, key=abs) if gaps else None,
            )
        )
    out.sort(key=lambda s: (s.verdict != "SIZE", s.verdict != "watch", -s.significant_views))
    return out


def format_stability(items: Sequence[Stability]) -> str:
    header = (
        f"{'variable':34s} {'grade':9s} {'views':>5s} {'sig':>4s} {'agree':>5s} "
        f"{'min_arm_n':>9s} {'signs':22s} {'verdict':>13s}"
    )
    out = [header, "-" * len(header)]
    for item in items:
        signs = "".join("+" if s > 0 else ("-" if s < 0 else ".") for s in item.signs)
        out.append(
            f"{item.variable:34s} {item.grade:9s} {item.views:5d} {item.significant_views:4d} "
            f"{item.agreeing_views:5d} {item.min_arm_n:9d} {signs:22s} {item.verdict:>13s}"
        )
    out.append("")
    out.append(
        "views = (horizon x label convention). signs = the sign of the out-of-sample "
        "separation in each view, in order. A variable that changes sign across views has "
        "found the collector's polling schedule, not an edge."
    )
    return "\n".join(out)


def describe(dataset: Dataset) -> str:
    """The denominator, the sources and the cost, before any result."""
    lines = [
        f"dataset {dataset.built_from}: {len(dataset)} rows, cost {dataset.cost_bps} bps round trip",
        f"  census: {dataset.census.line()}",
    ]
    span = dataset.span_ms()
    if span:
        hours = (span[1] - span[0]) / 3_600_000.0
        lines.append(f"  entry span: {span[0]} .. {span[1]} ({hours:.1f} hours)")
    for kind in TRUST_ORDER:
        rows = [r for r in dataset.rows if r.source is kind]
        if rows:
            lines.append(f"  {kind.value:9s} n={len(rows):5d}   bias: {SOURCE_BIAS[kind]}")
    for horizon in dataset.horizons_min:
        strict = sum(1 for r in dataset.rows if r.net(horizon, strict=True) is not None)
        carry = sum(1 for r in dataset.rows if r.net(horizon, strict=False) is not None)
        censored = len(dataset) - carry
        lines.append(
            f"  +{horizon:2d}min labelled: strict {strict}, carry {carry}, censored {censored} "
            f"({censored / len(dataset) * 100:.0f}% of rows have no print in the window)"
            if len(dataset)
            else f"  +{horizon:2d}min labelled: no rows"
        )
    return "\n".join(lines)


def format_ranking(seps: Sequence[Separation], *, cost_bps: int) -> str:
    """The deliverable table: variables by out-of-sample separation, with n."""
    header = (
        f"{'variable':34s} {'grade':9s} {'split':16s} {'claim':>7s} {'OOS':>7s} "
        f"{'ci_lo':>7s} {'ci_hi':>7s} {'n_good':>6s} {'n_bad':>6s} {'net_gap%':>9s} {'verdict':>10s}"
    )
    out = [header, "-" * len(header)]
    for sep in seps:
        oos = sep.out_of_sample
        good_n = oos.good.n if oos else 0
        bad_n = oos.bad.n if oos else 0
        measured = sep.measured_half_nats
        ci = sep.oos_half_nats_ci
        gap = oos.mean_gap_pct if oos else None
        verdict = "SIZE" if (sep.survives and sep.economically_survives) else ("watch" if sep.survives else "no")
        if good_n < 30 or bad_n < 30:
            verdict = "n<30"
        out.append(
            f"{sep.variable:34s} {sep.grade:9s} {sep.split:16s} "
            f"{(f'{sep.claimed_half_nats:.3f}' if sep.claimed_half_nats else '-'):>7s} "
            f"{(f'{measured:+.3f}' if measured is not None else '-'):>7s} "
            f"{(f'{ci[0]:+.3f}' if ci else '-'):>7s} {(f'{ci[1]:+.3f}' if ci else '-'):>7s} "
            f"{good_n:6d} {bad_n:6d} "
            f"{(f'{gap:+.2f}' if gap is not None else '-'):>9s} {verdict:>10s}"
        )
    out.append("")
    out.append(
        f"claim = the literature's half-separation in nats (confluence weight). OOS = the same "
        f"quantity measured on the held-out later half, on win rate at this horizon net of "
        f"{cost_bps} bps. ci = 5th/95th bootstrap percentile. net_gap% = mean net return of the "
        f"good arm minus the bad arm. verdict SIZE = both intervals exclude zero AND both arms "
        f"have n>=30; watch = win-rate interval excludes zero but the economic one does not."
    )
    return "\n".join(out)


__all__ = [
    "BOOTSTRAP_RESAMPLES",
    "CANDIDATE_PROVENANCE",
    "CANDIDATE_VARIABLES",
    "COST_BPS_ROUND_TRIP",
    "DEFAULT_HORIZONS_MIN",
    "MAX_ENTRY_PRICE_AGE_MS",
    "MAX_FILL_LAG_MS",
    "MIN_PRINTS_BEFORE_PSEUDO_ENTRY",
    "MODEL_VERSION",
    "PROVENANCE",
    "PSEUDO_ENTRY_LAG_MS",
    "SOURCE_BIAS",
    "TRUST_ORDER",
    "Arm",
    "ArmPair",
    "Artefact",
    "Census",
    "CombinedResult",
    "CreationFact",
    "Dataset",
    "Entry",
    "EntryFill",
    "EventIndex",
    "Forward",
    "OutcomeRow",
    "Separation",
    "SourceKind",
    "Stability",
    "artefact_share",
    "build_dataset",
    "collect_entries",
    "combined_gate",
    "describe",
    "entry_price_bias",
    "format_ranking",
    "format_stability",
    "forward_returns",
    "map_observations",
    "measure_variable",
    "net_pct",
    "observe_before_entry",
    "rank_variables",
    "score_rows",
    "stability",
    "sweep_pseudo_entry_lag",
]
