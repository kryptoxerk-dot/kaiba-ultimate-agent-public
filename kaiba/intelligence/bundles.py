"""Launch bundles and snipers, measured from our own tape instead of bought from GMGN.

``bundler_pct`` and ``sniper_pct`` are two of the eight evidence-ranked variables in
``docs/EDGE-AND-VARIABLES.md`` §1 and they were unavailable on **132 of 132** dossiers
this system has ever built. The only source wired for them is GMGN, the account is on the
free tier, and the free tier cannot serve a quote. So the fifth-strongest variable we hold
— bundle-adjusted concentration, a **24pp** discriminative gap against **6pp** for raw
top-10 (§1 #5, from MELT, arXiv:2602.13480) — had never once been computed, and
``curve-velocity``, whose ``max_bundler_pct: 20`` gate cannot be satisfied by an unknown,
failed closed on every token. Correctly, and permanently.

None of that needs a vendor. **A bundle is a structural fact about the block and we
already record the structure**: ``swaps.slot`` is which block a trade landed in and
``swaps.block_index`` is where in that block's transaction order it sits.

The definition, stated so it can be argued with
---------------------------------------------

A **launch bundle** is a maximal set of buy transactions that

1. land in the **same slot** — a Jito bundle executes atomically inside one block, so a
   group spanning two slots is two events, not one;
2. occupy **contiguous or near-contiguous positions** in that slot's transaction order
   (``block_index`` gap at most :data:`MAX_INDEX_GAP`) — the block engine places a
   bundle's transactions consecutively, which is the fingerprint that needs no Jito API
   and no tip field;
3. fall inside the **launch window**, the create slot through create slot +
   :data:`LAUNCH_WINDOW_SLOTS`; and
4. are placed by at least :data:`MIN_BUNDLE_ENTITIES` distinct **entities**, not
   addresses.

Rule 4 is what stops this being a non-signal. ``docs/EDGE-AND-VARIABLES.md`` §4 #16:
*"the dev bought his own bundle" has a 98.7% base rate*. A detector that fires on a lone
creator buy fires on essentially every launch and carries no information, so a group of
one operator is not a bundle here however many addresses it used.

A **sniper buy** is a buy inside the same launch window that is **not** part of a
qualifying group and was not made by the creator. Sniping and bundling are different
behaviours — a sniper races the launch from outside the block ordering, a bundler rides
inside it — and the two numbers are reported separately. GMGN ships them together; that
is a packaging decision, not a measurement one.

**Where this definition differs from MELT's, which is where the 24pp number comes from.**
MELT resolves coordinated accounts by co-purchase in a single transaction, shared funding
addresses, and Jito bundle ids, and reports share of *total supply*. We have none of those
three inputs: no instruction-level decode, an empty funding graph, and no bundle ids. We
substitute block-order adjacency, which is the observable consequence of a Jito bundle
rather than the bundle id itself. Consequences to hold in mind before comparing any number
here with 24pp:

* We will **miss** a bundle whose transactions our tape does not fully hold, and we will
  **miss** coordinated buying that was deliberately spread across slots to avoid looking
  like a bundle. Both understate.
* We will **over-count** if an unrelated buy happens to land between two bundle
  transactions and inside the gap tolerance. At 250 ms slots with hundreds of
  transactions per block this is possible; the entity test does not catch it.
* Our denominator is stated per row (:class:`SupplyBasis`) because curve reserves at
  launch are 79.31% of total supply on a standard pump.fun mint, so the same bundle is
  1.26x larger as a share of the curve than of the supply.

Treat the output as a feature, not a decision rule. The paper's own classifier is AUPRC
~0.57 on an 84%-positive class.

Coverage is the correctness property, not the arithmetic
--------------------------------------------------------

A token whose tape we have not pulled back to its launch is ``None`` with
``EvidenceBasis.UNAVAILABLE``. **Never 0%.** "We did not look" and "we looked and found
none" must be different values, because ``curve-velocity`` gates on
``bundler_pct < max_bundler_pct`` and fails closed on an unknown: a false 0% converts that
fail-closed gate into a fail-open one and fires the lane on exactly the launches it exists
to avoid. Everything in :func:`analyse` before the arithmetic is there to establish that
the tape is complete back to the create transaction, and it refuses far more often than it
answers.

The completeness test is ``curve_snapshots.coverage_from_ms <= tokens.created_ms``, which
is the record that a backwards walk of the trade route *terminated* rather than being cut
off. That query is deliberately duplicated from
``kaiba.ingest.token_flow.launch_coverage_proved`` rather than imported: this task does not
own ``kaiba/ingest`` and that module is being edited concurrently. If the two ever diverge,
``token_flow``'s is authoritative — the same convention ``token_flow`` itself applied to
``scanner.swap_count_if_covered``.

The corpus, and why the measurement is stored rather than recomputed
--------------------------------------------------------------------

Until now nothing called :func:`store`. :func:`collect_bundles` computed a report for the
dossier, handed over two claims and dropped everything else, so ``token_bundles`` held
**zero rows** against 6,814 stored dossiers: the bundle share of a token was recomputed
from the tape on every scan, nothing accumulated, and there was no way to ask the only
question worth asking — *does this variable separate anything, in our data?* — because
there was no data, only a function.

Three things follow from storing it, and they are the reason this is not just a cache.

1. **The refusals are the corpus too.** A stored ``coverage='unavailable'`` row with its
   reason turns "the number is missing" into "the tape for this mint starts 3,587 s after
   the launch", which is a claim about our collection rather than about the token, and it
   is the only form in which that fact is countable.
2. **It has to be idempotent in a specific direction.** Tapes deepen; the launch does not
   change. A recompute must replace the row, and a *later* measurement must win — but a
   later *refusal* must not erase an earlier measurement, because refusals can come from
   the caller rather than from the tape. :func:`store` states all three rules and is
   tested against each of them.
3. **It makes the variable falsifiable.** :func:`rug_separation` reads this table and
   nothing else, so its answer is reproducible with one SQL query, including when the
   honest answer is that the sample is too small to have one.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump, jload
from kaiba.core.schemas import Chain, EvidenceBasis, Measure, Receipt, now_ms
from kaiba.intelligence.concentration import default_exclusions
from kaiba.intelligence.entity import SOLO_PREFIX, entity_ids_for
from kaiba.intelligence.hubs import safe_normalize

log = logging.getLogger(__name__)

#: Bump when the definition changes, so a stored row says which definition produced it.
MODEL_ID = "kaiba-bundles-v1"

ZERO = Decimal(0)
HUNDRED = Decimal(100)

#: The entity graph is rebuilt nightly and the tape only grows, so a day-old answer about
#: a fixed historical event is still the right answer; it is the *entity* half that ages.
FRESHNESS_BUDGET_S = 86_400


# --------------------------------------------------------------------------------------
# thresholds, and where every one of them came from
# --------------------------------------------------------------------------------------

#: Slots after the create slot in which a buy still counts as "at launch".
LAUNCH_WINDOW_SLOTS = 4

#: Largest permitted gap between consecutive ``block_index`` values inside one group.
MAX_INDEX_GAP = 2

#: Transactions needed before a run is a group at all.
MIN_BUNDLE_TXS = 2

#: Distinct *entities* needed before a group counts as a bundle.
MIN_BUNDLE_ENTITIES = 2

#: Above this many transactions a contiguous run is a launch stampede, not a bundle.
MAX_BUNDLE_TXS = 25

#: How close our first observed trade must be to ``tokens.created_ms`` for the tape to be
#: treated as anchored at the launch, in milliseconds.
LAUNCH_ANCHOR_TOLERANCE_MS = 30_000

#: Fraction of launch-window buy *atoms* that may lack a ``block_index`` before the
#: measurement is refused outright.
MAX_UNINDEXED_ATOM_FRACTION = Decimal("0.25")

#: A fee payer sponsoring more than this many distinct wallets is a service, not an
#: operator, and merging on it would collapse strangers into one entity.
MAX_SHARED_FEE_PAYER_WALLETS = 25

#: Distinct tokens on which two addresses must co-buy in the same slot before the
#: tape-derived fallback calls them one operator.
MIN_COBUY_TOKENS = 2

#: Smallest number of measured tokens each arm of a separation must hold before any gap
#: is reported at all. Below it the arms' medians are still returned, and ``gap_pp`` is
#: ``None`` — a refusal, not a zero.
MIN_SEPARATION_ARM = 5

#: ``swaps.source`` prefixes for routes that walk **one mint's** trade history. Only these
#: can support the weaker timestamp proof in :func:`_first_trade_at_launch`.
#:
#: This distinction is load-bearing and it is the one thing here most likely to be got
#: wrong by a later change. ``helius:backfill`` walks a *wallet's* history, so it can
#: deposit a single row at a token's create slot while holding none of the other
#: transactions in that slot. Such a tape looks perfectly anchored to a timestamp check
#: and contains, by construction, no bundle — which would produce a confident 0% on a
#: mint nobody has actually looked at. ``pumpfun:trades`` paginates backwards through one
#: mint's whole tape, so a row at the launch means the walk passed through everything
#: after it.
PER_TOKEN_TAPE_SOURCES: tuple[str, ...] = ("pumpfun:trades",)

#: pump.fun curve invariant, quoted in ``015_curve_snapshots.sql`` and
#: ``kaiba/ingest/token_flow.py`` as the one thing about that curve that is constant.
PUMPFUN_CURVE_INVARIANT_ATOMS = 279_900_000_000_000

#: Real token reserves at launch on a standard pump.fun mint. Observed exactly, in every
#: ``curve_snapshots`` row this database holds.
PUMPFUN_LAUNCH_CURVE_ATOMS = 793_100_000_000_000

#: Total supply implied when both values above are observed exactly.
PUMPFUN_TOTAL_SUPPLY_ATOMS = 1_000_000_000_000_000

#: Where each number above came from. ``INVENTED`` means nobody has backtested it and it
#: is a hypothesis, exactly as ``kaiba/execution/triage.py::THRESHOLD_PROVENANCE`` and the
#: tier-0 weights use the word. A test asserts this dict covers every threshold, so a
#: silently added knob fails the build — we already have one invented weight quietly
#: load-bearing (``creator_prior_graduate`` at 0.45) and a second arriving undeclared is
#: the failure mode this table exists to prevent.
THRESHOLD_PROVENANCE: dict[str, str] = {
    "LAUNCH_WINDOW_SLOTS": (
        "INVENTED. Four slots is 1.0 s at the 250 ms slot time confirmed on-chain "
        "2026-09-16 (EDGE §3b). No published study defines the window in which a buy is "
        "'at launch'. The direction of error is deliberate: a short window understates "
        "both numbers, and understating bundle share is the direction that makes the "
        "curve-velocity gate more permissive, so this is the one threshold here whose "
        "conservative setting is the LARGER value. Four is a compromise and should be "
        "swept against outcomes once more than a handful of tokens are measurable."
    ),
    "MAX_INDEX_GAP": (
        "STRUCTURAL basis, INVENTED tolerance. A Jito bundle's transactions are placed "
        "consecutively by the block engine, so the true gap is 0. The tolerance of 2 "
        "absorbs the case where our tape holds a bundle's buys but not a non-swap or "
        "sell transaction sitting between them. Raising it merges unrelated same-slot "
        "buys, which over-counts; lowering it to 0 refuses any bundle we hold "
        "incompletely, which under-counts."
    ),
    "MIN_BUNDLE_TXS": (
        "DEFINITIONAL, not a threshold. A group of one transaction is a transaction."
    ),
    "MIN_BUNDLE_ENTITIES": (
        "EVIDENCED direction, INVENTED count. EDGE §4 #16: 'the dev bought his own "
        "bundle' has a 98.7% base rate (MELT), so a group that resolves to one operator "
        "fires on essentially every launch and carries no information. Two is the "
        "smallest count that excludes it. Note this makes the number sensitive to entity "
        "resolution quality: with an empty graph and the tape fallback under-merging, a "
        "single operator's ring reads as several entities and its group qualifies. That "
        "over-counts, which is the safe direction for a gate that rejects on high values."
    ),
    "MAX_BUNDLE_TXS": (
        "INVENTED. Jito caps a single bundle at 5 transactions, so a longer contiguous "
        "run is either several bundles from one operator (which we still want to count) "
        "or an organic stampede into a hyped launch (which we do not). 25 is the value "
        "skills/holder-cluster-analysis/scripts/bundle_share.py already used, kept for "
        "consistency rather than for evidence. Neither the number nor the existence of a "
        "cap has been tested against outcomes."
    ),
    "LAUNCH_ANCHOR_TOLERANCE_MS": (
        "INVENTED number, MEASURED separation. In this database the lag between "
        "tokens.created_ms and the first swap row is bimodal: anchored tapes sit within "
        "+/-2 s and unanchored ones start at 3,587 s or later. Any cut inside that gap "
        "gives the same answer, so the precise value carries no weight today; it will "
        "start to matter when the tape sources diversify."
    ),
    "MAX_UNINDEXED_ATOM_FRACTION": (
        "INVENTED. Adjacency is undecidable for a buy with no block_index, so those buys "
        "can never join a group and their supply is silently treated as unbundled. That "
        "understates, which is the fail-open direction, so above a quarter of launch "
        "atoms the whole measurement is refused rather than reported low. 0.25 is a "
        "guess; the correct value is whatever keeps the understatement below the gate's "
        "own resolution, and nobody has measured that."
    ),
    "MAX_SHARED_FEE_PAYER_WALLETS": (
        "INVENTED. A third-party fee payer is evidence of one operator only while the "
        "sponsorship is narrow; a launch service or relayer paying for hundreds of "
        "strangers produces the same edge and merging on it would collapse a crowd into "
        "one entity, which is the error entity.py is explicitly biased against."
    ),
    "MIN_COBUY_TOKENS": (
        "INVENTED. Two addresses landing buys in the same 250 ms slot on two different "
        "tokens is not plausibly chance, but 'not plausibly chance' is an argument, not a "
        "measurement, and no false-positive rate has been computed for this rule."
    ),
    "MIN_SEPARATION_ARM": (
        "INVENTED, and deliberately far below what would make a gap trustworthy. It is "
        "not a power calculation: with 5 per arm a median is one or two observations "
        "wide and a 'gap' is noise. It exists only to stop a number being emitted at all "
        "on the single digits we actually have, because EDGE §4 #17 records that every "
        "temporal generalisation test in this literature failed and that graduation "
        "rates moved ~10x in 18 months, so a small-sample gap from here would be quoted "
        "later without its denominator. Raising it refuses more; lowering it below 2 "
        "would let a two-token 'separation' out of this module."
    ),
    "PUMPFUN_CURVE_INVARIANT_ATOMS": (
        "MEASURED and quoted upstream: 015_curve_snapshots.sql and token_flow.py both "
        "name virtual_token_reserves - real_token_reserves = 279,900,000,000,000 as the "
        "only pump.fun invariant. Used here as a *test*, never as an assumption: supply "
        "is only inferred when this equality and the launch reserve below both hold "
        "exactly."
    ),
    "PUMPFUN_LAUNCH_CURVE_ATOMS": (
        "MEASURED. Every curve_snapshots row in this database reports exactly "
        "793,100,000,000,000 real token atoms at launch. Checked for equality, not "
        "assumed: a mint that reports anything else falls through to the curve-reserves "
        "denominator and says so."
    ),
    "PUMPFUN_TOTAL_SUPPLY_ATOMS": (
        "DERIVED from the two exact observations above: 793.1M curve tokens plus 206.9M "
        "held back for the graduation pool is pump.fun's documented 1B supply. This is "
        "the only path by which a percentage here is a share of TOTAL supply, which is "
        "what MELT's 24pp figure is a share of."
    ),
}


class SupplyBasis(StrEnum):
    """Which denominator a percentage on this report divides by."""

    CALLER = "caller"
    #: Total supply, inferred only after both pump.fun reserve values matched exactly.
    PUMPFUN_STANDARD_VERIFIED = "pumpfun_standard_verified"
    #: Tokens purchasable on the curve at launch. ~79.31% of total supply on a standard
    #: mint, so a percentage on this basis is ~1.26x the same share of total supply.
    CURVE_LAUNCH_RESERVES = "curve_launch_reserves"
    UNKNOWN = "unknown"


class EntitySource(StrEnum):
    """Which resolver produced the entity ids behind the entity counts."""

    #: kaiba.intelligence.entity resolved at least one of these addresses.
    GRAPH = "graph"
    #: The graph knew none of them; the tape-derived rules below carried the result.
    FALLBACK = "fallback"
    #: Neither produced a link. Every address is its own entity.
    NONE = "none"


class Coverage(StrEnum):
    MEASURED = "measured"
    UNAVAILABLE = "unavailable"


class StoreOutcome(StrEnum):
    """What :func:`store` did, so a sweep can count downgrades it refused to make."""

    #: The row now holds this report.
    WROTE = "wrote"
    #: A refusal arrived for a token that already has a measurement under the current
    #: model. The measurement was kept and the refusal was recorded beside it.
    KEPT_MEASURED = "kept_measured"


class SeparationMetric(StrEnum):
    """Which stored column a separation is computed on. They are not interchangeable.

    ``bundled_pct`` is a share of the denominator named in ``supply_basis`` and is the
    only one comparable with MELT's 24pp, which is a share of *total supply*. It is NULL
    whenever no denominator could be established. ``bundled_share_of_launch_buys`` needs
    no supply figure and therefore survives a missing denominator, but it is a share of
    the launch window's own buying and says nothing about the token.
    """

    SUPPLY = "bundled_pct"
    LAUNCH_BUYS = "bundled_share_of_launch_buys"


# --------------------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LaunchBuy:
    """One buy inside the launch window, in base units."""

    wallet: str
    slot: int
    block_index: int | None
    atoms: int
    lamports: int | None
    tx: str

    @property
    def indexed(self) -> bool:
        return self.block_index is not None


@dataclass(frozen=True, slots=True)
class BundleGroup:
    """A same-slot, block-adjacent run of buys that resolved to >= 2 entities."""

    slot: int
    first_index: int
    last_index: int
    buys: tuple[LaunchBuy, ...]
    entity_ids: tuple[str, ...]

    @property
    def atoms(self) -> int:
        return sum(b.atoms for b in self.buys)

    @property
    def wallets(self) -> tuple[str, ...]:
        return tuple(sorted({b.wallet for b in self.buys}))


@dataclass(frozen=True)
class BundleReport:
    """Both behaviours, their provenance, and an explicit reason when there is no number."""

    chain: Chain
    token: str
    coverage: Coverage
    reason: str
    bundled: Measure = field(default_factory=lambda: Measure.unknown(FRESHNESS_BUDGET_S))
    sniped: Measure = field(default_factory=lambda: Measure.unknown(FRESHNESS_BUDGET_S))
    #: Bundled atoms over every atom bought in the launch window. Needs no supply figure,
    #: so it survives a missing denominator — but it is a share of the launch, not of the
    #: token, and must never be substituted for ``bundled``.
    bundled_share_of_launch_buys: Measure = field(
        default_factory=lambda: Measure.unknown(FRESHNESS_BUDGET_S)
    )
    supply_atoms: int | None = None
    supply_basis: SupplyBasis = SupplyBasis.UNKNOWN
    bundled_atoms: int = 0
    sniped_atoms: int = 0
    launch_buy_atoms: int = 0
    groups: tuple[BundleGroup, ...] = ()
    bundle_wallets: tuple[str, ...] = ()
    sniper_wallets: tuple[str, ...] = ()
    bundle_entities: int = 0
    sniper_entities: int = 0
    launch_buys: int = 0
    unindexed_buys: int = 0
    entity_source: EntitySource = EntitySource.NONE
    entity_ids: Mapping[str, str] = field(default_factory=dict)
    creator: str | None = None
    create_slot: int | None = None
    last_slot: int | None = None
    unknowns: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def measured(self) -> bool:
        return self.coverage is Coverage.MEASURED

    @property
    def bundled_pct(self) -> Decimal | None:
        return self.bundled.value

    @property
    def sniped_pct(self) -> Decimal | None:
        return self.sniped.value

    def summary(self) -> str:
        if not self.measured:
            return f"bundle share unavailable for {self.token[:12]}: {self.reason}"
        bun = "n/a" if self.bundled_pct is None else f"{self.bundled_pct:.2f}%"
        sni = "n/a" if self.sniped_pct is None else f"{self.sniped_pct:.2f}%"
        return (
            f"bundled {bun} across {len(self.groups)} group(s)/{self.bundle_entities} "
            f"entities, sniped {sni} across {self.sniper_entities} entities, "
            f"supply basis {self.supply_basis.value}, entities via {self.entity_source.value}"
        )


# --------------------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------------------


def _conn(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    return conn if conn is not None else get_conn()


def _receipt(endpoint: str, basis: EvidenceBasis, note: str | None = None) -> Receipt:
    return Receipt(
        provider="kaiba.intelligence",
        endpoint=endpoint,
        observed_at_ms=now_ms(),
        basis=basis,
        note=note,
    )


def _measure(value: Decimal | None, endpoint: str, note: str | None = None) -> Measure:
    if value is None:
        return Measure.unknown(FRESHNESS_BUDGET_S)
    return Measure(
        value=value,
        basis=EvidenceBasis.DERIVED,
        receipt=_receipt(endpoint, EvidenceBasis.DERIVED, note),
        freshness_budget_s=FRESHNESS_BUDGET_S,
    )


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(Decimal(str(value)))
    except (ArithmeticError, ValueError, TypeError):
        return None


def _pct(part: int, whole: int | None) -> Decimal | None:
    if not whole or whole <= 0:
        return None
    return Decimal(part) / Decimal(whole) * HUNDRED


class _UnionFind:
    """Local, because ``entity._UnionFind`` is private and this must not depend on it."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        parent = self.parent
        if x not in parent:
            parent[x] = x
            return x
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        lo, hi = (ra, rb) if ra <= rb else (rb, ra)
        self.parent[hi] = lo


# --------------------------------------------------------------------------------------
# coverage
# --------------------------------------------------------------------------------------


def launch_coverage_proved(
    chain: Chain, token: str, conn: sqlite3.Connection, *, created_ms: int | None
) -> bool:
    """Did a trade-route walk ever terminate at or before this mint's creation?

    Duplicated from ``kaiba.ingest.token_flow.launch_coverage_proved`` on purpose; see the
    module docstring. ``coverage_from_ms`` is written by the collector and a value at or
    before ``created_ms`` means the backwards walk ran out of history rather than out of
    budget — which is the only direct evidence that what we hold is everything there was.
    """
    if created_ms is None:
        return False
    try:
        row = fetch_one(
            conn,
            "SELECT MIN(coverage_from_ms) AS m FROM curve_snapshots "
            "WHERE chain=? AND token=? AND coverage_from_ms IS NOT NULL",
            (chain.value, token),
        )
    except sqlite3.Error as exc:
        log.warning("bundles: coverage lookup failed for %s (%s)", token[:12], exc)
        return False
    earliest = _int_or_none(row["m"]) if row else None
    return earliest is not None and earliest <= int(created_ms)


def _first_trade_at_launch(
    chain: Chain, token: str, conn: sqlite3.Connection, *, created_ms: int | None
) -> bool:
    """The weaker proof: a **per-token** route's earliest row sits at the launch.

    Weaker than :func:`launch_coverage_proved` because it establishes where the tape
    *starts*, not that nothing is missing after that. It is accepted anyway for the route
    we actually use: ``pumpfun:trades`` paginates backwards from the present, so holding a
    row at the launch implies the walk passed through everything in between.

    The source restriction is not a detail. Without it a single ``helius:backfill`` row —
    deposited by a walk of one *wallet's* history that happened to include this mint's
    create slot — satisfies a timestamp check while the rest of that slot is missing. The
    bundle share of a tape containing one transaction is zero by construction, and a
    confident 0% on a mint nobody looked at is precisely the fail-open failure this module
    exists to prevent. ``curve-velocity`` would fire on it.
    """
    if created_ms is None:
        return False
    clauses = " OR ".join("source = ? OR source LIKE ?" for _ in PER_TOKEN_TAPE_SOURCES)
    params: list[Any] = [chain.value, token]
    for prefix in PER_TOKEN_TAPE_SOURCES:
        params.extend((prefix, f"{prefix}:%"))
    try:
        row = fetch_one(
            conn,
            f"SELECT MIN(ts_ms) AS m FROM swaps WHERE chain=? AND token=? AND ({clauses})",
            params,
        )
    except sqlite3.Error as exc:
        log.warning("bundles: per-token tape lookup failed for %s (%s)", token[:12], exc)
        return False
    first_ms = _int_or_none(row["m"]) if row else None
    if first_ms is None:
        return False
    return (first_ms - int(created_ms)) <= LAUNCH_ANCHOR_TOLERANCE_MS


# --------------------------------------------------------------------------------------
# entity resolution: the graph where it works, the tape where it does not
# --------------------------------------------------------------------------------------


def fallback_entity_links(
    chain: Chain, addresses: Sequence[str], conn: sqlite3.Connection
) -> tuple[dict[str, str], list[str]]:
    """Two tape-derived rules, used only because ``entities`` is empty today.

    Returns ``(address -> synthetic entity id, notes)``. Only addresses that actually
    linked to something appear in the mapping; a lone address stays unmapped and is its
    own entity, the convention ``entity.independent_entity_count`` uses.

    **Rule F1, shared third-party fee payer.** Two addresses whose swaps were paid for by
    the same third party are one actor paying gas for its own wallets. Capped by
    :data:`MAX_SHARED_FEE_PAYER_WALLETS` so a relayer does not merge a crowd.

    **Rule F2, same-slot co-buy across >= 2 tokens.** Two addresses landing buys in the
    same 250 ms slot on two different mints are coordinated; the chance version of that
    coincidence twice over is negligible.

    Both are weaker than a hard edge in ``kaiba.intelligence.cluster`` and neither is
    persisted as one. Nothing here writes to ``cluster_edges`` or ``entities``: inventing
    graph edges from a heuristic is exactly the wrong merge that ``entity.py`` is built to
    avoid, and a merge nobody can see downstream is the worst kind.
    """
    notes: list[str] = []
    unique = list(dict.fromkeys(a for a in addresses if a))
    if len(unique) < 2:
        return {}, notes
    placeholders = ",".join("?" for _ in unique)
    uf = _UnionFind()
    linked: set[str] = set()

    try:
        payer_rows = fetch_all(
            conn,
            "SELECT fee_payer, wallet FROM swaps WHERE chain=? AND fee_payer IS NOT NULL "
            f"AND fee_payer <> wallet AND wallet IN ({placeholders}) "
            "GROUP BY fee_payer, wallet",
            [chain.value, *unique],
        )
    except sqlite3.Error as exc:
        log.warning("bundles: fee-payer rule unreadable (%s)", exc)
        payer_rows = []
    by_payer: dict[str, list[str]] = {}
    for row in payer_rows:
        by_payer.setdefault(str(row["fee_payer"]), []).append(str(row["wallet"]))
    f1 = 0
    for payer, wallets in by_payer.items():
        if len(wallets) < 2:
            continue
        if len(wallets) > MAX_SHARED_FEE_PAYER_WALLETS:
            notes.append(
                f"fee payer {payer[:8]} sponsors {len(wallets)} launch wallets: treated as a "
                "service, not merged"
            )
            continue
        head = wallets[0]
        for other in wallets[1:]:
            uf.union(head, other)
        linked.update(wallets)
        f1 += 1

    try:
        cobuy_rows = fetch_all(
            conn,
            "SELECT a.wallet AS wa, b.wallet AS wb, COUNT(DISTINCT a.token) AS tokens "
            "FROM swaps a JOIN swaps b ON a.chain=b.chain AND a.token=b.token "
            "  AND a.slot=b.slot AND a.wallet < b.wallet "
            "WHERE a.chain=? AND a.side='buy' AND b.side='buy' AND a.slot IS NOT NULL "
            f"  AND a.wallet IN ({placeholders}) AND b.wallet IN ({placeholders}) "
            "GROUP BY a.wallet, b.wallet HAVING tokens >= ?",
            [chain.value, *unique, *unique, MIN_COBUY_TOKENS],
        )
    except sqlite3.Error as exc:
        log.warning("bundles: co-buy rule unreadable (%s)", exc)
        cobuy_rows = []
    for row in cobuy_rows:
        wa, wb = str(row["wa"]), str(row["wb"])
        uf.union(wa, wb)
        linked.update((wa, wb))

    if f1:
        notes.append(f"fallback F1 merged on {f1} shared third-party fee payer(s)")
    if cobuy_rows:
        notes.append(f"fallback F2 merged on {len(cobuy_rows)} same-slot co-buy pair(s)")

    mapping: dict[str, str] = {}
    for address in unique:
        if address not in linked:
            continue
        mapping[address] = f"tape:{uf.find(address)}"
    return mapping, notes


def resolve_launch_entities(
    chain: Chain, addresses: Sequence[str], conn: sqlite3.Connection
) -> tuple[dict[str, str], EntitySource, list[str]]:
    """Graph first, tape fallback second, every address its own entity otherwise."""
    unique = list(dict.fromkeys(a for a in addresses if a))
    if not unique:
        return {}, EntitySource.NONE, []
    try:
        graph = entity_ids_for(chain, unique, conn)
    except sqlite3.Error as exc:
        log.warning("bundles: entity graph unreadable (%s)", exc)
        graph = {}
    if graph:
        note = (
            f"entity graph resolved {len(graph)} of {len(unique)} launch addresses; "
            "the tape fallback was not used"
        )
        return dict(graph), EntitySource.GRAPH, [note]
    fallback, notes = fallback_entity_links(chain, unique, conn)
    if fallback:
        notes.insert(
            0,
            "entity graph is empty for this chain, so entity counts come from the "
            "tape-derived fallback rules and are weaker than a cluster_edges merge",
        )
        return fallback, EntitySource.FALLBACK, notes
    notes.insert(
        0,
        "neither the entity graph nor the tape fallback linked any launch address; every "
        "address counts as its own entity, which over-counts independence",
    )
    return {}, EntitySource.NONE, notes


def _entity_of(mapping: Mapping[str, str], address: str) -> str:
    return mapping.get(address) or f"{SOLO_PREFIX}{address}"


# --------------------------------------------------------------------------------------
# supply
# --------------------------------------------------------------------------------------


def resolve_supply(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection,
    *,
    total_supply_atoms: int | None = None,
    created_ms: int | None = None,
) -> tuple[int | None, SupplyBasis, str | None]:
    """The denominator, and an honest label for which denominator it is.

    Total supply is only ever *inferred* after the pump.fun curve invariant
    ``virtual_token_reserves - real_token_reserves = 279,900,000,000,000`` is observed to
    hold on this mint. ``015_curve_snapshots.sql`` names that as the one thing about the
    curve that is constant, and it is a property of the whole curve rather than of its
    launch, so it can be checked on any snapshot no matter how much trading has happened
    since. A mint whose curve satisfies it is a standard pump.fun token curve, which mints
    1e9 tokens at 6 decimals.

    ``real_token_atoms`` is deliberately **not** used as a denominator except on a
    snapshot taken at the launch. It is a live reserve, not a supply: on one mint here it
    had risen to 802.8e12 from a 793.1e12 launch value by the time we first read it,
    because net selling returns tokens to the curve. Dividing by it on an arbitrary
    snapshot produces a number that drifts with trading and is labelled as if it did not.
    """
    if total_supply_atoms is not None and total_supply_atoms > 0:
        return int(total_supply_atoms), SupplyBasis.CALLER, None
    try:
        row = fetch_one(
            conn,
            "SELECT real_token_atoms, virtual_token_atoms, observed_ms FROM curve_snapshots "
            "WHERE chain=? AND token=? AND real_token_atoms IS NOT NULL "
            "ORDER BY observed_ms ASC LIMIT 1",
            (chain.value, token),
        )
    except sqlite3.Error as exc:
        log.warning("bundles: curve snapshot unreadable for %s (%s)", token[:12], exc)
        return None, SupplyBasis.UNKNOWN, "curve snapshots unreadable"
    if row is None:
        return None, SupplyBasis.UNKNOWN, "no curve snapshot, so no supply denominator"
    real = _int_or_none(row["real_token_atoms"])
    virtual = _int_or_none(row["virtual_token_atoms"])
    if real is None or real <= 0:
        return None, SupplyBasis.UNKNOWN, "curve snapshot has no real token reserve"
    if virtual is not None and virtual - real == PUMPFUN_CURVE_INVARIANT_ATOMS:
        note = None
        if real != PUMPFUN_LAUNCH_CURVE_ATOMS:
            note = (
                "curve invariant holds so total supply is known, but the snapshot was taken "
                f"after {abs(real - PUMPFUN_LAUNCH_CURVE_ATOMS)} atoms of net flow, so it is "
                "not a reading of the launch state"
            )
        return PUMPFUN_TOTAL_SUPPLY_ATOMS, SupplyBasis.PUMPFUN_STANDARD_VERIFIED, note
    observed = _int_or_none(row["observed_ms"])
    at_launch = (
        created_ms is not None
        and observed is not None
        and abs(observed - int(created_ms)) <= LAUNCH_ANCHOR_TOLERANCE_MS
    )
    if at_launch:
        return (
            real,
            SupplyBasis.CURVE_LAUNCH_RESERVES,
            "curve invariant does not hold, so total supply is unknown; the denominator is "
            "this mint's reserves at launch and the percentage is NOT a share of total supply",
        )
    return (
        None,
        SupplyBasis.UNKNOWN,
        "curve invariant does not hold and the only snapshot postdates the launch, so "
        "real_token_atoms is a live reserve rather than a supply and there is no denominator",
    )


# --------------------------------------------------------------------------------------
# grouping
# --------------------------------------------------------------------------------------


def group_buys(
    buys: Sequence[LaunchBuy], entity_ids: Mapping[str, str]
) -> tuple[list[BundleGroup], list[str]]:
    """Find qualifying bundle groups. Pure: no database, no clock.

    Within each slot, buys are ordered by ``block_index`` and cut into maximal runs whose
    consecutive gaps are at most :data:`MAX_INDEX_GAP`. A run qualifies when it holds at
    least :data:`MIN_BUNDLE_TXS` transactions, at most :data:`MAX_BUNDLE_TXS`, and
    resolves to at least :data:`MIN_BUNDLE_ENTITIES` distinct entities.
    """
    notes: list[str] = []
    by_slot: dict[int, list[LaunchBuy]] = {}
    for buy in buys:
        if not buy.indexed:
            continue
        by_slot.setdefault(buy.slot, []).append(buy)

    groups: list[BundleGroup] = []
    for slot in sorted(by_slot):
        ordered = sorted(by_slot[slot], key=lambda b: (b.block_index or 0, b.wallet))
        run: list[LaunchBuy] = []
        for buy in ordered:
            if run and (buy.block_index or 0) - (run[-1].block_index or 0) > MAX_INDEX_GAP:
                groups.extend(_qualify(slot, run, entity_ids, notes))
                run = []
            run.append(buy)
        if run:
            groups.extend(_qualify(slot, run, entity_ids, notes))
    return groups, notes


def _qualify(
    slot: int,
    run: Sequence[LaunchBuy],
    entity_ids: Mapping[str, str],
    notes: list[str],
) -> list[BundleGroup]:
    if len(run) < MIN_BUNDLE_TXS:
        return []
    if len(run) > MAX_BUNDLE_TXS:
        notes.append(
            f"slot {slot}: contiguous run of {len(run)} buys exceeds {MAX_BUNDLE_TXS}; "
            "treated as a launch stampede, not a bundle"
        )
        return []
    entities = sorted({_entity_of(entity_ids, b.wallet) for b in run})
    if len(entities) < MIN_BUNDLE_ENTITIES:
        notes.append(
            f"slot {slot}: run of {len(run)} buys resolves to one entity; not a bundle "
            "(EDGE §4 #16, the dev buying his own launch has a 98.7% base rate)"
        )
        return []
    return [
        BundleGroup(
            slot=slot,
            first_index=run[0].block_index or 0,
            last_index=run[-1].block_index or 0,
            buys=tuple(run),
            entity_ids=tuple(entities),
        )
    ]


# --------------------------------------------------------------------------------------
# the measurement
# --------------------------------------------------------------------------------------


def _unavailable(
    chain: Chain, token: str, reason: str, *, unknowns: Iterable[str] = ("bundler_pct", "sniper_pct")
) -> BundleReport:
    return BundleReport(
        chain=chain,
        token=token,
        coverage=Coverage.UNAVAILABLE,
        reason=reason,
        unknowns=tuple(unknowns),
    )


def analyse(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection | None = None,
    *,
    total_supply_atoms: int | None = None,
    creator: str | None = None,
) -> BundleReport:
    """Bundle and sniper share for one token, or an explicit refusal with a reason.

    Every early return here is a refusal, and each one is a case where reporting a number
    would be worse than reporting none: an unknown makes ``curve-velocity`` fail closed,
    a wrong low number makes it fail open.
    """
    c = _conn(conn)
    token = safe_normalize(token, chain)

    row = fetch_one(
        c,
        "SELECT creator, created_ms, pool FROM tokens WHERE chain=? AND address=?",
        (chain.value, token),
    )
    created_ms = _int_or_none(row["created_ms"]) if row else None
    creator_addr = creator or (str(row["creator"]) if row and row["creator"] else None)
    if creator_addr:
        creator_addr = safe_normalize(creator_addr, chain)
    pool = str(row["pool"]) if row and row["pool"] else None

    if created_ms is None:
        return _unavailable(
            chain, token, "creation time unknown, so the launch window cannot be placed"
        )

    tape = fetch_one(
        c,
        "SELECT COUNT(*) AS n, MIN(ts_ms) AS first_ms FROM swaps WHERE chain=? AND token=?",
        (chain.value, token),
    )
    swap_rows = _int_or_none(tape["n"]) if tape else 0
    if not swap_rows:
        return _unavailable(chain, token, "no swap rows: the tape for this mint was never pulled")
    first_ms = _int_or_none(tape["first_ms"]) if tape else None

    if launch_coverage_proved(chain, token, c, created_ms=created_ms):
        proof = "collection walked this mint's trades back past its creation"
    elif _first_trade_at_launch(chain, token, c, created_ms=created_ms):
        proof = "a per-token tape route's earliest row sits at the launch"
    else:
        lag_s = None if first_ms is None else (first_ms - created_ms) / 1000.0
        where = "unknown" if lag_s is None else f"{lag_s:.0f}s after launch"
        return _unavailable(
            chain,
            token,
            f"tape is not proved complete back to launch (starts {where}); a bundle share "
            "computed from a partial tape understates, and understating is the direction "
            "that opens the curve-velocity gate",
        )

    create_slot = _create_slot(chain, token, c, creator_addr)
    if create_slot is None:
        return _unavailable(chain, token, "no slot recorded on any row, so adjacency is undecidable")
    last_slot = create_slot + LAUNCH_WINDOW_SLOTS

    drop = set(default_exclusions(chain))
    if pool:
        drop.add(safe_normalize(pool, chain))
    buys = _launch_buys(chain, token, c, create_slot, last_slot, drop)
    if not buys:
        return _unavailable(
            chain,
            token,
            f"no buys observed in slots {create_slot}..{last_slot} although the tape is "
            "anchored; the window may be too narrow for this launch",
        )

    total_atoms = sum(b.atoms for b in buys)
    unindexed = [b for b in buys if not b.indexed]
    unindexed_atoms = sum(b.atoms for b in unindexed)
    if total_atoms > 0 and unindexed:
        share = Decimal(unindexed_atoms) / Decimal(total_atoms)
        if share > MAX_UNINDEXED_ATOM_FRACTION:
            return _unavailable(
                chain,
                token,
                f"{share * HUNDRED:.1f}% of launch-window buy volume has no block_index, so "
                "adjacency is undecidable for it and any bundle share would be an "
                "understatement of unknown size",
            )

    wallets = sorted({b.wallet for b in buys})
    entity_ids, entity_source, notes = resolve_launch_entities(chain, wallets, c)
    groups, group_notes = group_buys(buys, entity_ids)
    notes.extend(group_notes)

    # ``LaunchBuy`` is frozen and slotted, so set membership is by value; ``tx`` makes
    # every row distinct, which is what keeps this from collapsing two real buys into one.
    bundled_buys = {b for g in groups for b in g.buys}
    bundled_atoms = sum(b.atoms for b in bundled_buys)
    bundle_wallets = sorted({b.wallet for b in bundled_buys})
    bundle_entities = len({e for g in groups for e in g.entity_ids})

    sniper_buys = [
        b for b in buys if b not in bundled_buys and b.wallet != (creator_addr or "")
    ]
    sniped_atoms = sum(b.atoms for b in sniper_buys)
    sniper_wallets = sorted({b.wallet for b in sniper_buys})
    sniper_entities = len({_entity_of(entity_ids, w) for w in sniper_wallets})

    supply, supply_basis, supply_note = resolve_supply(
        chain, token, c, total_supply_atoms=total_supply_atoms, created_ms=created_ms
    )
    if supply_note:
        notes.append(supply_note)
    if unindexed:
        notes.append(
            f"{len(unindexed)} launch buy(s) carry no block_index and could not join any "
            "group; the bundled figure is a floor"
        )
    if creator_addr is None:
        notes.append(
            "creator unknown, so no buy could be attributed to the creator and the whole "
            "launch window counts as bundled or sniped"
        )

    unknowns: list[str] = []
    if supply is None:
        unknowns.extend(("bundler_pct", "sniper_pct"))

    endpoint_note = f"{MODEL_ID}; supply basis {supply_basis.value}; {proof}"
    return BundleReport(
        chain=chain,
        token=token,
        coverage=Coverage.MEASURED,
        reason=proof,
        bundled=_measure(_pct(bundled_atoms, supply), "bundles.bundled_pct", endpoint_note),
        sniped=_measure(_pct(sniped_atoms, supply), "bundles.sniped_pct", endpoint_note),
        bundled_share_of_launch_buys=_measure(
            _pct(bundled_atoms, total_atoms), "bundles.bundled_share_of_launch_buys", endpoint_note
        ),
        supply_atoms=supply,
        supply_basis=supply_basis,
        bundled_atoms=bundled_atoms,
        sniped_atoms=sniped_atoms,
        launch_buy_atoms=total_atoms,
        groups=tuple(groups),
        bundle_wallets=tuple(bundle_wallets),
        sniper_wallets=tuple(sniper_wallets),
        bundle_entities=bundle_entities,
        sniper_entities=sniper_entities,
        launch_buys=len(buys),
        unindexed_buys=len(unindexed),
        entity_source=entity_source,
        entity_ids=dict(entity_ids),
        creator=creator_addr,
        create_slot=create_slot,
        last_slot=last_slot,
        unknowns=tuple(unknowns),
        notes=tuple(notes),
    )


def _create_slot(
    chain: Chain, token: str, conn: sqlite3.Connection, creator: str | None
) -> int | None:
    """Where the launch is, in decreasing order of directness.

    ``is_create_tx`` is the direct answer and is set on zero rows in this database today,
    so the creator's earliest buy carries it in practice — on pump.fun the create
    instruction and the creator's own first buy are the same transaction. The last resort
    is the tape's earliest slot, which is only reached once coverage has already been
    proved, so it is the launch by construction rather than by assumption.
    """
    row = fetch_one(
        conn,
        "SELECT MIN(slot) AS s FROM swaps WHERE chain=? AND token=? AND is_create_tx=1 "
        "AND slot IS NOT NULL",
        (chain.value, token),
    )
    slot = _int_or_none(row["s"]) if row else None
    if slot is not None:
        return slot
    if creator:
        row = fetch_one(
            conn,
            "SELECT MIN(slot) AS s FROM swaps WHERE chain=? AND token=? AND wallet=? "
            "AND side='buy' AND slot IS NOT NULL",
            (chain.value, token, creator),
        )
        slot = _int_or_none(row["s"]) if row else None
        if slot is not None:
            return slot
    row = fetch_one(
        conn,
        "SELECT MIN(slot) AS s FROM swaps WHERE chain=? AND token=? AND slot IS NOT NULL",
        (chain.value, token),
    )
    return _int_or_none(row["s"]) if row else None


def _launch_buys(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection,
    create_slot: int,
    last_slot: int,
    drop: set[str],
) -> list[LaunchBuy]:
    rows = fetch_all(
        conn,
        "SELECT tx, slot, block_index, wallet, amount_token, amount_native FROM swaps "
        "WHERE chain=? AND token=? AND side='buy' AND slot BETWEEN ? AND ? "
        "ORDER BY slot, block_index, tx",
        (chain.value, token, create_slot, last_slot),
    )
    out: list[LaunchBuy] = []
    for row in rows:
        wallet = safe_normalize(str(row["wallet"]), chain)
        if not wallet or wallet in drop:
            continue
        atoms = _int_or_none(row["amount_token"])
        slot = _int_or_none(row["slot"])
        if atoms is None or atoms <= 0 or slot is None:
            continue
        out.append(
            LaunchBuy(
                wallet=wallet,
                slot=slot,
                block_index=_int_or_none(row["block_index"]),
                atoms=atoms,
                lamports=_int_or_none(row["amount_native"]),
                tx=str(row["tx"]),
            )
        )
    return out


# --------------------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------------------


def store(
    report: BundleReport,
    conn: sqlite3.Connection | None = None,
    *,
    allow_downgrade: bool = False,
) -> StoreOutcome:
    """Write the report, including the refusals. One row per token, forever.

    A refusal is stored on purpose. "We looked at this mint and could not measure it, for
    this reason" is a different and more useful state than no row, and it is what turns
    'the number is missing' into 'the tape is missing', which is an actionable difference
    for the operator.

    **Idempotence, stated as three rules, because this is the part that decides whether a
    corpus accumulates or rots.**

    1. *A recompute updates, never duplicates.* ``(chain, token)`` is the primary key and
       the write is an upsert, so a token swept a hundred times has one row. The member
       rows are deleted and rewritten rather than upserted, so a wallet that was a sniper
       under a shallow tape and is a bundler under a deeper one does not end up stored as
       both.
    2. *A later measurement wins over an earlier one.* Our tape only ever grows and the
       event being measured is fixed and historical, so a measurement taken against more
       tape is strictly the better reading of the same launch. This is the case the
       corpus exists for: 4,043 of 4,071 tapes in this database were written once and
       never touched, and when the deepening lands, the bundle numbers must follow it.
    3. *A later refusal does not erase an earlier measurement.* This is the asymmetry, and
       it is the only rule here that is not obvious. ``analyse`` can refuse for reasons
       that are about the caller rather than the tape — a connection to a database that
       has no ``curve_snapshots``, an entity graph mid-rebuild, a transient
       ``sqlite3.Error`` inside a coverage lookup — and rule 2 applied blindly would let
       any of those silently delete a real number. So a refusal against a token that
       already holds a measurement **under the current** :data:`MODEL_ID` keeps the
       measurement and records the refusal in ``detail_json.last_refusal``. Pass
       ``allow_downgrade=True`` to overrule it. A refusal under a *different* model id
       does overwrite, because a number the current definition would not produce must not
       sit in the corpus wearing the current definition's name.

    Returns which of those happened, so :func:`build_corpus` can report the count rather
    than a sweep quietly leaving stale rows behind.
    """
    c = _conn(conn)
    if not report.measured and not allow_downgrade:
        prior = _prior_row(report, c)
        if prior is not None:
            _note_later_refusal(report, prior, c)
            return StoreOutcome.KEPT_MEASURED
    detail = {
        "notes": list(report.notes),
        "groups": [
            {
                "slot": g.slot,
                "first_index": g.first_index,
                "last_index": g.last_index,
                "txs": len(g.buys),
                "atoms": str(g.atoms),
                "wallets": list(g.wallets),
                "entities": list(g.entity_ids),
            }
            for g in report.groups
        ],
        "thresholds": {
            "LAUNCH_WINDOW_SLOTS": LAUNCH_WINDOW_SLOTS,
            "MAX_INDEX_GAP": MAX_INDEX_GAP,
            "MIN_BUNDLE_ENTITIES": MIN_BUNDLE_ENTITIES,
            "MAX_BUNDLE_TXS": MAX_BUNDLE_TXS,
        },
    }
    c.execute(
        "INSERT INTO token_bundles (chain, token, computed_ms, model, coverage, reason, "
        " supply_atoms, supply_basis, bundled_atoms, bundled_pct, sniped_atoms, sniped_pct, "
        " launch_buy_atoms, bundled_share_of_launch_buys, bundle_groups, bundle_entities, "
        " sniper_entities, launch_buys, unindexed_buys, entity_source, creator, create_slot, "
        " last_slot, detail_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(chain, token) DO UPDATE SET computed_ms=excluded.computed_ms, "
        " model=excluded.model, coverage=excluded.coverage, reason=excluded.reason, "
        " supply_atoms=excluded.supply_atoms, supply_basis=excluded.supply_basis, "
        " bundled_atoms=excluded.bundled_atoms, bundled_pct=excluded.bundled_pct, "
        " sniped_atoms=excluded.sniped_atoms, sniped_pct=excluded.sniped_pct, "
        " launch_buy_atoms=excluded.launch_buy_atoms, "
        " bundled_share_of_launch_buys=excluded.bundled_share_of_launch_buys, "
        " bundle_groups=excluded.bundle_groups, bundle_entities=excluded.bundle_entities, "
        " sniper_entities=excluded.sniper_entities, launch_buys=excluded.launch_buys, "
        " unindexed_buys=excluded.unindexed_buys, entity_source=excluded.entity_source, "
        " creator=excluded.creator, create_slot=excluded.create_slot, "
        " last_slot=excluded.last_slot, detail_json=excluded.detail_json",
        (
            report.chain.value,
            report.token,
            now_ms(),
            MODEL_ID,
            report.coverage.value,
            report.reason,
            None if report.supply_atoms is None else str(report.supply_atoms),
            report.supply_basis.value if report.measured else None,
            str(report.bundled_atoms) if report.measured else None,
            None if report.bundled_pct is None else str(report.bundled_pct),
            str(report.sniped_atoms) if report.measured else None,
            None if report.sniped_pct is None else str(report.sniped_pct),
            str(report.launch_buy_atoms) if report.measured else None,
            None
            if report.bundled_share_of_launch_buys.value is None
            else str(report.bundled_share_of_launch_buys.value),
            len(report.groups) if report.measured else None,
            report.bundle_entities if report.measured else None,
            report.sniper_entities if report.measured else None,
            report.launch_buys if report.measured else None,
            report.unindexed_buys if report.measured else None,
            report.entity_source.value if report.measured else None,
            report.creator,
            report.create_slot,
            report.last_slot,
            jdump(detail),
        ),
    )
    c.execute(
        "DELETE FROM token_bundle_members WHERE chain=? AND token=?",
        (report.chain.value, report.token),
    )
    if report.measured:
        for address, role, atoms, lamports, slot, index, count in _member_rows(report):
            c.execute(
                "INSERT INTO token_bundle_members (chain, token, address, role, entity_id, "
                " atoms, lamports, first_slot, first_index, buys) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    report.chain.value,
                    report.token,
                    address,
                    role,
                    report.entity_ids.get(address),
                    str(atoms),
                    None if lamports is None else str(lamports),
                    slot,
                    index,
                    count,
                ),
            )
    return StoreOutcome.WROTE


def _prior_row(
    report: BundleReport, conn: sqlite3.Connection
) -> Mapping[str, Any] | None:
    """The existing row for this token, but only when it is a measurement we must keep.

    ``None`` means "nothing worth protecting here": no row, a stored refusal, or a
    measurement made by a different definition.
    """
    try:
        row = fetch_one(
            conn,
            "SELECT coverage, model, computed_ms, detail_json FROM token_bundles "
            "WHERE chain=? AND token=?",
            (report.chain.value, report.token),
        )
    except sqlite3.Error as exc:
        log.warning("bundles: could not read the prior row for %s (%s)", report.token[:12], exc)
        return None
    if row is None:
        return None
    if str(row["coverage"]) != Coverage.MEASURED.value:
        return None
    if str(row["model"]) != MODEL_ID:
        return None
    return row


def _note_later_refusal(
    report: BundleReport, prior: Mapping[str, Any], conn: sqlite3.Connection
) -> None:
    """Record, beside a kept measurement, that a later look could not reproduce it.

    Only the latest refusal is kept plus a count. An unbounded history would grow by one
    entry per sweep on every token whose tape is thin, which is most of them.
    """
    detail = jload(prior["detail_json"], {})
    if not isinstance(detail, dict):
        detail = {"detail": detail}
    detail["last_refusal"] = {
        "at_ms": now_ms(),
        "model": MODEL_ID,
        "reason": report.reason,
        "measured_at_ms": _int_or_none(prior["computed_ms"]),
    }
    detail["later_refusals"] = int(detail.get("later_refusals") or 0) + 1
    conn.execute(
        "UPDATE token_bundles SET detail_json=? WHERE chain=? AND token=?",
        (jdump(detail), report.chain.value, report.token),
    )
    log.info(
        "bundles: kept the stored measurement for %s; a later look refused (%s)",
        report.token[:12],
        report.reason[:120],
    )


def _member_rows(
    report: BundleReport,
) -> list[tuple[str, str, int, int | None, int | None, int | None, int]]:
    """One row per address, with the role it played. Bundler beats creator beats sniper."""
    bundlers = set(report.bundle_wallets)
    snipers = set(report.sniper_wallets)
    per: dict[str, dict[str, Any]] = {}
    for group in report.groups:
        for buy in group.buys:
            _accumulate(per, buy)
    for address in snipers | ({report.creator} if report.creator else set()):
        per.setdefault(address, {"atoms": 0, "lamports": None, "slot": None, "index": None, "n": 0})
    out: list[tuple[str, str, int, int | None, int | None, int | None, int]] = []
    for address, agg in per.items():
        if address in bundlers:
            role = "bundler"
        elif address == report.creator:
            role = "creator"
        elif address in snipers:
            role = "sniper"
        else:
            continue
        out.append(
            (address, role, int(agg["atoms"]), agg["lamports"], agg["slot"], agg["index"], agg["n"])
        )
    return out


def _accumulate(per: dict[str, dict[str, Any]], buy: LaunchBuy) -> None:
    agg = per.setdefault(
        buy.wallet, {"atoms": 0, "lamports": None, "slot": None, "index": None, "n": 0}
    )
    agg["atoms"] += buy.atoms
    if buy.lamports is not None:
        agg["lamports"] = (agg["lamports"] or 0) + buy.lamports
    if agg["slot"] is None or buy.slot < agg["slot"]:
        agg["slot"], agg["index"] = buy.slot, buy.block_index
    agg["n"] += 1


def persist_report(
    report: BundleReport, conn: sqlite3.Connection | None = None
) -> StoreOutcome | None:
    """:func:`store`, but a write failure is logged instead of raised.

    Used on the paths where persistence is a side effect of doing something else — the
    dossier scan and the sweep — and where a locked database or a schema older than
    ``024_bundles.sql`` must not turn a successful measurement into an exception in
    somebody else's pipeline. Returns ``None`` when nothing was written, which is what
    the sweep counts as a failure.
    """
    try:
        return store(report, conn)
    except Exception as exc:  # noqa: BLE001 - the corpus is a side effect, not the job
        log.warning(
            "bundles: could not persist the report for %s (%s)", report.token[:12], exc
        )
        return None


# --------------------------------------------------------------------------------------
# the dossier call site
# --------------------------------------------------------------------------------------


def collect_bundles(
    address: str, chain: Chain, conn: Any = None, *, persist: bool = True
) -> tuple[list[Any], list[Receipt], str]:
    """A ``kaiba.intelligence.dyor.Collected`` for ``bundler_pct`` and ``sniper_pct``.

    Already wired: ``dyor.scan`` calls this through its own ``collect_bundles`` wrapper
    (``dyor.py:1081``, registered in the collector tuple at ``dyor.py:1469``). Both
    property names are in ``NUM_PROPERTIES`` and ``TRACKED_PROPERTIES``, and both
    ``Measure`` fields exist on ``TokenDossier``.

    Claims are emitted **only** when the measurement succeeded and a supply denominator
    was established. A refusal returns no claims, which leaves both properties unanswered,
    which is what puts them in ``dossier.unknowns`` and keeps ``curve-velocity`` failing
    closed. Returning a 0 here would be the single worst thing this module could do.

    **Every report computed here is persisted**, claims or no claims, and that is the
    difference between this module having a corpus and not having one. Before it, the
    dossier path recomputed a token's bundle share from the tape on every scan and then
    dropped the report on the floor: ``token_bundles`` held zero rows while
    ``token_dossiers`` held 6,814, so the fifth-strongest variable we have had nothing to
    be measured against and no way to notice that it had deepened. A refusal is stored
    too — see :func:`store` — because "we looked at this mint and the tape starts 3,587 s
    after the launch" is the finding, not the absence of one.

    Persistence is best-effort by construction: a dossier scan that could not write a
    bundle row is still a dossier scan, and the 7.6 s tier-1 budget is not the place to
    discover that the database is locked. Pass ``persist=False`` for a pure read.
    """
    try:
        from kaiba.intelligence.dyor import Claim
    except ImportError:  # pragma: no cover - dyor is a sibling, not an optional dep
        return [], [], "n/a"
    if chain is not Chain.SOL:
        return [], [], "n/a"
    try:
        report = analyse(chain, address, conn)
    except Exception as exc:  # noqa: BLE001 - a dead sibling is data, not a crash
        log.warning("bundles: analyse failed for %s (%s)", address[:16], exc)
        return [], [_receipt("bundles.analyse", EvidenceBasis.UNAVAILABLE, str(exc)[:200])], "down"
    if persist:
        persist_report(report, conn)

    if not report.measured or report.bundled_pct is None or report.sniped_pct is None:
        return (
            [],
            [_receipt("bundles.analyse", EvidenceBasis.UNAVAILABLE, report.reason[:200])],
            "ok",
        )
    receipts = [r for r in (report.bundled.receipt, report.sniped.receipt) if r is not None]
    receipt = receipts[0] if receipts else _receipt("bundles.analyse", EvidenceBasis.DERIVED)
    claims = [
        Claim(prop="bundler_pct", provider="bundles", value=report.bundled_pct, receipt=receipt),
        Claim(prop="sniper_pct", provider="bundles", value=report.sniped_pct, receipt=receipt),
    ]
    return claims, receipts, "ok"


# --------------------------------------------------------------------------------------
# calibration
# --------------------------------------------------------------------------------------


def measurable_tokens(chain: Chain, conn: sqlite3.Connection | None = None) -> list[str]:
    """Mints whose tape might be complete back to launch. Candidates, not guarantees.

    A cheap pre-filter so a sweep does not run the full :func:`analyse` refusal path over
    every mint we have ever heard of. :func:`analyse` re-checks everything.
    """
    c = _conn(conn)
    source_clause = " OR ".join("s2.source = ? OR s2.source LIKE ?" for _ in PER_TOKEN_TAPE_SOURCES)
    params: list[Any] = [chain.value]
    for prefix in PER_TOKEN_TAPE_SOURCES:
        params.extend((prefix, f"{prefix}:%"))
    params.append(LAUNCH_ANCHOR_TOLERANCE_MS)
    rows = fetch_all(
        c,
        "SELECT t.address AS token FROM tokens t WHERE t.chain=? AND t.created_ms IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM swaps s WHERE s.chain=t.chain AND s.token=t.address) "
        "AND ( EXISTS (SELECT 1 FROM curve_snapshots cs WHERE cs.chain=t.chain "
        "               AND cs.token=t.address AND cs.coverage_from_ms IS NOT NULL "
        "               AND cs.coverage_from_ms <= t.created_ms) "
        "   OR (SELECT MIN(s2.ts_ms) FROM swaps s2 WHERE s2.chain=t.chain "
        f"        AND s2.token=t.address AND ({source_clause})) - t.created_ms <= ? ) "
        "ORDER BY t.address",
        params,
    )
    return [str(r["token"]) for r in rows]


def traded_tokens(chain: Chain, conn: sqlite3.Connection | None = None) -> list[str]:
    """Mints we actually closed a trade on. Every one of these must get a row.

    A token with an outcome is the only kind that can ever calibrate anything, so the
    corpus stores a row for it even when the answer is a refusal. Without that, the
    absence of a row conflates "we never looked" with "we looked and the tape starts an
    hour after the launch", and only the second one is a finding.
    """
    c = _conn(conn)
    try:
        rows = fetch_all(
            c,
            "SELECT DISTINCT token FROM trades WHERE chain=? AND token IS NOT NULL "
            "ORDER BY token",
            (chain.value,),
        )
    except sqlite3.Error as exc:
        log.warning("bundles: trades table unreadable (%s)", exc)
        return []
    return [str(r["token"]) for r in rows]


def corpus_targets(chain: Chain, conn: sqlite3.Connection | None = None) -> list[str]:
    """What a full sweep covers: every measurable candidate, plus everything we traded."""
    c = _conn(conn)
    seen = dict.fromkeys(measurable_tokens(chain, c))
    for token in traded_tokens(chain, c):
        seen.setdefault(token, None)
    return list(seen)


@dataclass(frozen=True)
class CorpusStats:
    """What one sweep did. Refusals are an outcome, not an error."""

    considered: int
    measured: int
    refused: int
    kept_measured: int
    failed: int
    skipped: int
    elapsed_ms: int
    reasons: Mapping[str, int] = field(default_factory=dict)

    @property
    def written(self) -> int:
        return self.measured + self.refused

    def summary(self) -> str:
        return (
            f"{self.considered} token(s) in {self.elapsed_ms / 1000:.1f}s: "
            f"{self.measured} measured, {self.refused} refusal(s) stored, "
            f"{self.kept_measured} refusal(s) declined to overwrite a measurement, "
            f"{self.skipped} skipped, {self.failed} failed"
        )


def build_corpus(
    chain: Chain,
    tokens: Sequence[str] | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    limit: int | None = None,
    only_missing: bool = False,
) -> CorpusStats:
    """Analyse and store, for every target. This is what makes ``token_bundles`` exist.

    ``only_missing`` skips tokens that already carry a row written by the current
    :data:`MODEL_ID`. It is **off by default and should usually stay off**: the whole
    reason to re-run this is that a tape deepened, and a token that already has a row is
    exactly the one whose row is now out of date. Use it to resume an interrupted sweep,
    not as the normal mode.

    A token that raises is counted and stepped over. A sweep of 4,778 mints that dies on
    one malformed row has produced nothing, and the corpus is the point.
    """
    c = _conn(conn)
    targets = list(tokens) if tokens is not None else corpus_targets(chain, c)
    if limit is not None:
        targets = targets[:limit]

    existing: set[str] = set()
    if only_missing:
        try:
            existing = {
                str(r["token"])
                for r in fetch_all(
                    c,
                    "SELECT token FROM token_bundles WHERE chain=? AND model=?",
                    (chain.value, MODEL_ID),
                )
            }
        except sqlite3.Error as exc:
            log.warning("bundles: could not read the existing corpus (%s)", exc)

    started = now_ms()
    measured = refused = kept = failed = skipped = 0
    reasons: dict[str, int] = {}
    for token in targets:
        if only_missing and token in existing:
            skipped += 1
            continue
        try:
            report = analyse(chain, token, c)
        except Exception as exc:  # noqa: BLE001 - one bad mint must not end the sweep
            failed += 1
            log.warning("bundles: analyse failed for %s (%s)", token[:12], exc)
            continue
        outcome = persist_report(report, c)
        if outcome is None:
            failed += 1
            continue
        if outcome is StoreOutcome.KEPT_MEASURED:
            kept += 1
        elif report.measured:
            measured += 1
        else:
            refused += 1
        if not report.measured:
            key = report.reason.split(";")[0].split("(")[0].strip()[:80]
            reasons[key] = reasons.get(key, 0) + 1

    return CorpusStats(
        considered=len(targets),
        measured=measured,
        refused=refused,
        kept_measured=kept,
        failed=failed,
        skipped=skipped,
        elapsed_ms=now_ms() - started,
        reasons=dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
    )


@dataclass(frozen=True)
class Separation:
    """Does bundle share separate graduating tokens from the rest, in *our* data?"""

    measured: int
    graduated: int
    not_graduated: int
    graduated_median: Decimal | None
    not_graduated_median: Decimal | None
    gap_pp: Decimal | None
    verdict: str


def _arms(
    chain: Chain,
    conn: sqlite3.Connection,
    metric: SeparationMetric,
    labelled: Sequence[tuple[str, bool]],
) -> tuple[list[Decimal], list[Decimal], set[str], int]:
    """Split stored values into (positive arm, negative arm), with bases and corpus hits.

    One reader for every separation in this module, so ``separation`` and
    :func:`rug_separation` cannot drift into disagreeing about what "measured" means. It
    reads :data:`token_bundles` and nothing else — see :func:`rug_separation` for why
    recomputing here would be the wrong kind of convenient.
    """
    pos: list[Decimal] = []
    neg: list[Decimal] = []
    bases: set[str] = set()
    in_corpus = 0
    for token, is_positive in labelled:
        row = fetch_one(
            conn,
            "SELECT coverage, supply_basis, bundled_pct, bundled_share_of_launch_buys "
            "FROM token_bundles WHERE chain=? AND token=?",
            (chain.value, token),
        )
        if row is None:
            continue
        in_corpus += 1
        if str(row["coverage"]) != Coverage.MEASURED.value:
            continue
        raw = row[metric.value]
        if raw is None:
            continue
        try:
            value = Decimal(str(raw))
        except ArithmeticError:
            continue
        if metric is SeparationMetric.SUPPLY and row["supply_basis"]:
            bases.add(str(row["supply_basis"]))
        (pos if is_positive else neg).append(value)
    return pos, neg, bases, in_corpus


def separation(
    chain: Chain,
    conn: sqlite3.Connection | None = None,
    *,
    metric: SeparationMetric = SeparationMetric.LAUNCH_BUYS,
) -> Separation:
    """Compare bundle share across graduated and non-graduated mints in the corpus.

    Deliberately reports medians and the sample sizes that produced them, and refuses to
    state a gap at all when either arm is below a size at which a gap would mean anything.
    ``docs/EDGE-AND-VARIABLES.md`` §4 #17 is explicit that every temporal generalisation
    test in the literature failed and that graduation rates moved ~10x in 18 months, so a
    number computed here on a handful of tokens would be worse than no number: it would be
    quoted later without its denominator.

    Reads the stored corpus, like :func:`rug_separation`. It used to call :func:`analyse`
    on every measurable mint, which meant a 27-minute recompute that could disagree with
    the table an operator would check it against. Run :func:`build_corpus` first.
    """
    c = _conn(conn)
    try:
        rows = fetch_all(
            c,
            "SELECT b.token AS token, t.migrated_ms AS migrated_ms FROM token_bundles b "
            "LEFT JOIN tokens t ON t.chain=b.chain AND t.address=b.token "
            "WHERE b.chain=? AND b.coverage=? ORDER BY b.token",
            (chain.value, Coverage.MEASURED.value),
        )
    except sqlite3.Error as exc:
        log.warning("bundles: corpus unreadable (%s)", exc)
        rows = []
    labelled = [(str(r["token"]), r["migrated_ms"] is not None) for r in rows]
    grad, other, _, _ = _arms(chain, c, metric, labelled)

    n = len(grad) + len(other)
    if len(grad) < MIN_SEPARATION_ARM or len(other) < MIN_SEPARATION_ARM:
        return Separation(
            measured=n,
            graduated=len(grad),
            not_graduated=len(other),
            graduated_median=_median(grad),
            not_graduated_median=_median(other),
            gap_pp=None,
            verdict=(
                f"sample too small to say anything: {len(grad)} graduated and "
                f"{len(other)} not, out of {n} measurable mints. No gap is reported, "
                "because a gap computed here could not support its own decimal places."
            ),
        )
    gm, om = _median(grad), _median(other)
    gap = None if gm is None or om is None else gm - om
    return Separation(
        measured=n,
        graduated=len(grad),
        not_graduated=len(other),
        graduated_median=gm,
        not_graduated_median=om,
        gap_pp=gap,
        verdict=(
            f"median bundled share of launch buys: {gm} graduated (n={len(grad)}) vs {om} "
            f"not (n={len(other)}). Descriptive only; no holdout, no significance test."
        ),
    )


#: ``exit_reason`` prefix written by the rug monitor. Verified against the one place that
#: produces it: ``kaiba/execution/protection.py:233`` emits
#: ``ProtectionAction.exit_all(f"rug:lp_-{drop_pct}pct")``. Nothing else in the tree
#: writes an exit reason starting with ``rug:``.
RUG_EXIT_PREFIX = "rug:"


@dataclass(frozen=True)
class RugSeparation:
    """Does bundle share at launch separate the tokens that rugged on us from the rest?

    Every count here exists so the answer cannot be quoted without its denominator. The
    two that matter most are :attr:`measured_rugged` and :attr:`measured_survived`: they
    are the only tokens that contributed a number, and everything above them in the chain
    — closed trades, distinct tokens, rows in the corpus — is there to show how much fell
    away between "we traded 49 times" and "we could measure this many".
    """

    metric: SeparationMetric
    closed_trades: int
    tokens: int
    rugged_tokens: int
    survived_tokens: int
    in_corpus: int
    measured: int
    measured_rugged: int
    measured_survived: int
    rugged_median: Decimal | None
    survived_median: Decimal | None
    gap_pp: Decimal | None
    supply_bases: tuple[str, ...]
    verdict: str

    def summary(self) -> str:
        return self.verdict


def rug_separation(
    chain: Chain,
    conn: sqlite3.Connection | None = None,
    *,
    metric: SeparationMetric = SeparationMetric.LAUNCH_BUYS,
) -> RugSeparation:
    """Bundle share at launch, split by whether the rug monitor closed the position.

    This is the question the corpus exists for. arXiv:2608.20271 (Solana, 6.4M tokens)
    reports that rug pulls are predictable from the first five minutes of trading
    microstructure with no contract features at all, and 35% of our closed paper trades
    exited to the rug monitor, so the finding is directly testable on our own book — as
    soon as there is a book deep enough to test it on.

    **Reads the stored corpus, never recomputes.** That is deliberate: the number this
    returns must be the number an operator can reproduce with one SQL query against
    ``token_bundles``, and a function that quietly recomputed would report a separation
    that is not in the table it claims to be reading. Run :func:`build_corpus` first.

    A token counts as rugged when **any** of its closed trades exited with
    :data:`RUG_EXIT_PREFIX`. One token here carries more than one trade, and a position
    the rug monitor closed is a rug whatever the other leg did.

    ``gap_pp`` is ``None`` unless both arms reach :data:`MIN_SEPARATION_ARM`, and, on
    :attr:`SeparationMetric.SUPPLY`, unless every contributing row shares one
    ``supply_basis``: curve reserves at launch are 79.31% of total supply, so mixing the
    two bases inflates one arm by 1.26x and the 'separation' is the denominator.
    """
    c = _conn(conn)
    try:
        trades = fetch_all(
            c,
            "SELECT token, exit_reason FROM trades WHERE chain=? AND token IS NOT NULL",
            (chain.value,),
        )
    except sqlite3.Error as exc:
        log.warning("bundles: trades table unreadable (%s)", exc)
        trades = []

    rugged: set[str] = set()
    tokens: set[str] = set()
    for row in trades:
        token = str(row["token"])
        tokens.add(token)
        reason = str(row["exit_reason"] or "")
        if reason.startswith(RUG_EXIT_PREFIX):
            rugged.add(token)

    rug_values, other_values, bases, in_corpus = _arms(
        chain, c, metric, [(token, token in rugged) for token in sorted(tokens)]
    )

    measured = len(rug_values) + len(other_values)
    rm, om = _median(rug_values), _median(other_values)
    mixed = metric is SeparationMetric.SUPPLY and len(bases) > 1
    thin = len(rug_values) < MIN_SEPARATION_ARM or len(other_values) < MIN_SEPARATION_ARM

    if thin:
        verdict = (
            f"NO ANSWER. {len(trades)} closed trade(s) on {len(tokens)} token(s); "
            f"{len(rugged)} token(s) exited to the rug monitor. Only {measured} of those "
            f"tokens carry a measured {metric.value} in the corpus "
            f"({len(rug_values)} rugged, {len(other_values)} not), against the "
            f"{MIN_SEPARATION_ARM} per arm below which this function refuses to state a "
            "gap. The tokens we traded predate the tape capture that this measurement "
            "needs, so the sample is not small by chance — it is small by construction, "
            "and it stays that way until a token is traded after its launch tape was "
            "kept. No separation is reported."
        )
        gap = None
    elif mixed:
        verdict = (
            f"NO ANSWER. {measured} token(s) measured, but the rows mix supply bases "
            f"({', '.join(sorted(bases))}). Curve reserves at launch are 79.31% of total "
            "supply, so a percentage on one basis is 1.26x the same share on the other "
            "and any gap across them would be the denominator, not the bundling."
        )
        gap = None
    else:
        gap = None if rm is None or om is None else rm - om
        verdict = (
            f"median {metric.value}: {rm} rugged (n={len(rug_values)}) vs {om} not "
            f"(n={len(other_values)}), gap {gap} pp. Descriptive only: no holdout, no "
            "significance test, and the tokens are the ones we happened to trade."
        )

    return RugSeparation(
        metric=metric,
        closed_trades=len(trades),
        tokens=len(tokens),
        rugged_tokens=len(rugged),
        survived_tokens=len(tokens) - len(rugged),
        in_corpus=in_corpus,
        measured=measured,
        measured_rugged=len(rug_values),
        measured_survived=len(other_values),
        rugged_median=rm,
        survived_median=om,
        gap_pp=gap,
        supply_bases=tuple(sorted(bases)),
        verdict=verdict,
    )


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal(2)


__all__ = [
    "LAUNCH_WINDOW_SLOTS",
    "MAX_BUNDLE_TXS",
    "MAX_INDEX_GAP",
    "MIN_BUNDLE_ENTITIES",
    "MIN_SEPARATION_ARM",
    "MODEL_ID",
    "RUG_EXIT_PREFIX",
    "THRESHOLD_PROVENANCE",
    "BundleGroup",
    "BundleReport",
    "CorpusStats",
    "Coverage",
    "EntitySource",
    "LaunchBuy",
    "RugSeparation",
    "Separation",
    "SeparationMetric",
    "StoreOutcome",
    "SupplyBasis",
    "analyse",
    "build_corpus",
    "collect_bundles",
    "corpus_targets",
    "fallback_entity_links",
    "group_buys",
    "launch_coverage_proved",
    "measurable_tokens",
    "persist_report",
    "resolve_launch_entities",
    "resolve_supply",
    "rug_separation",
    "separation",
    "store",
    "traded_tokens",
]
