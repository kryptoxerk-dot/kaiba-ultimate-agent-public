"""The pass that actually populates the entity graph, and the honest way to read it.

``cluster.py`` knows how to derive edges. ``entity.py`` knows how to resolve them into
operators. On 2026-09-20 both were written, both were tested, and ``cluster_edges``,
``entities`` and ``entity_members`` held **zero rows** against 24,909 swaps and 6,236
wallets. Nothing had ever run them. Every "count entities, not addresses" claim in this
system therefore resolved each address to itself, which is the precise condition under
which ``confluence-5`` can be manufactured for the price of five funded wallets.

This module is the missing orchestration, plus the three things that orchestration alone
would get wrong.

**1. Hubs, measured on the graph we have rather than the one we wish we had.**
``hubs.py`` blocks known service addresses and anything with more than 200 counterparties
*in the ``transfers`` table*. On this database ``transfers`` holds 5,094 rows covering 293
senders, so the degree cap never fires, and a first run collapsed 876 addresses into one
"operator". :func:`detect_service_addresses` measures fan-out where the evidence actually
lives — co-signer partners in ``swap_meta``, wallets a fee payer sponsors, transfer
counterparties — and registers what it finds through :func:`hubs.add_hub` so the existing
derivation prunes it. It writes ``kind='disperser'`` with ``source='clustering:fanout'``,
which is read only by the clustering stack; ``concentration.py`` reads ``cex`` and ``pool``
and is unaffected.

**2. A component that is too large to attribute is refused, not merged and not deleted.**
Transitive closure is sound only while every intermediate node is a private wallet. Even
after fan-out pruning, chains of individually-innocent addresses produce components in the
dozens. Merging them invents one operator; deleting them returns their members to
"unclustered", which under ``confluence-5`` reads as *independent* and hands the gate
exactly the fake agreement it exists to catch. So a component above
:data:`MAX_ENTITY_SIZE` is quarantined: recorded with its members, its edge types and the
addresses holding it together, and surfaced to consumers as an explicit third state.

**3. The two consumers want opposite errors, so they get different readings of one graph.**

    Over-merging makes independent buyers look like one actor.
    Under-merging makes one actor look like a crowd.

``confluence-5`` (five entities must agree) is broken by under-merging. A concentration
gate (reject above 30% in one cluster) is broken by over-merging. There is no single
threshold that is safe for both, so this module does not pretend there is one.
:data:`RULE_STRENGTH` classifies every rule by what it actually claims, and
:func:`independence` takes a :class:`Policy`:

* :attr:`Policy.CONFLUENCE` collapses aggressively — persisted entities, quarantined
  components, and any single edge at or above :data:`CONFLUENCE_MIN_CONFIDENCE`, including
  ``LEAD_LAG``. A copier is a different *person*, which is why ``entity.py`` is right never
  to merge them into one identity, but it is not a different *opinion*, and confluence is a
  question about independent opinions.
* :attr:`Policy.IDENTITY` collapses only on persisted entities, and refuses to answer at
  all while a quarantined address is in the set.

Both return :class:`IndependenceView`, whose :meth:`~IndependenceView.satisfies` is
tri-state: ``None`` means "we cannot tell", and ``None`` is falsy, so a consumer that
writes ``if view.satisfies(5):`` fails closed rather than open. That is the whole point of
the class. The bias in this module is toward **confluence**: the persisted graph stays
conservative because ``entity.py`` owns that decision and concentration depends on it, and
the aggressive collapse lives in the read path where confluence can opt into it.

The fourth thing the pass does is **buy the evidence the strongest rule needs**.
``wallets.first_funder`` was NULL on every row and ``transfers`` covers 737 recipients, so
the funding rule — the one the research names first — had almost nothing to read.
:func:`enrich_funding` reads each address's earliest on-chain transaction and takes the
funder from the balance delta, at the 10-credit floor of the cheapest verified-cost Helius
route. It is off by default, budgeted in credits rather than in wallets, resumable, and
reports the provider ledger's figure rather than an estimate of its own.

Nothing here modifies ``entity.py``, ``cluster.py`` or ``hubs.py``. Edges are written
through :func:`cluster.persist_edges`, entities through :func:`entity.persist_entities`,
and hub labels through :func:`hubs.add_hub`. The single destructive write is
:func:`prune_hub_edges`, which deletes edges that touch a labelled hub — necessary because
``persist_edges`` only ever inserts, so an edge derived before a service was labelled
would otherwise outlive the label and keep the old giant component alive. Every edge it
removes is re-derivable on the next pass.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import combinations
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump, jload, tx
from kaiba.core.events import emit
from kaiba.core.schemas import (
    Chain,
    ClusterEdge,
    EdgeType,
    Entity,
    EventKind,
    EvidenceBasis,
    Receipt,
    digest,
    now_ms,
)
from kaiba.intelligence import cluster, entity
from kaiba.intelligence.hubs import HubFilter, add_hub, hub_set, safe_normalize, seed_hubs

log = logging.getLogger(__name__)

#: Bump when the pass changes what a stored row means, so a row says which pass wrote it.
MODEL_ID = "kaiba-clustering-v1"

PROVIDER = "helius"
FUNDING_ENDPOINT = "tx.getTransactionsForAddress"

#: ``hub_addresses.source`` this pass writes. Kept distinct so its rows can be told apart
#: from the builtin seed and from a label provider's, and undone without touching theirs.
FANOUT_HUB_SOURCE = "clustering:fanout"


# --------------------------------------------------------------------------------------
# thresholds, and where every one of them came from
# --------------------------------------------------------------------------------------

#: Distinct counterparties above which an address is treated as a service by this pass,
#: whatever role produced them (co-signing, fee sponsorship, native transfers).
MAX_SERVICE_FANOUT = 20

#: Members above which a connected component is refused rather than asserted as one
#: operator.
MAX_ENTITY_SIZE = 25

#: Lowest edge confidence the confluence reading will collapse on.
CONFLUENCE_MIN_CONFIDENCE = 0.60

#: Distinct wallets one third-party fee payer may sponsor before the edge is dropped.
MAX_SHARED_FEE_PAYER_WALLETS = 20

#: Distinct transactions two wallets must share a third-party fee payer on before the
#: SHARED_COUNTERPARTY edge is emitted at all.
MIN_SHARED_FEE_PAYER_TXS = 2

#: Confidence of a shared-fee-payer edge.
SHARED_FEE_PAYER_CONFIDENCE = 0.72

#: Largest gap between two wallets' first-funding times that the *verified* funder rule
#: will still link across. Deliberately wider than ``cluster.SAME_FUNDER_WINDOW_DAYS``.
VERIFIED_FUNDER_WINDOW_DAYS = 365

#: Default ceiling on Helius credits one funding pass may spend.
DEFAULT_FUNDING_CREDIT_BUDGET = 10_000

#: Credits :func:`helius.get_transactions_for_address` charges for a one-row page.
FUNDING_CREDITS_PER_WALLET = 10

#: Seconds a funding fact stays fresh. A wallet's first funder never changes, so this is
#: only about re-reading a *failed* lookup.
FUNDING_RETRY_AFTER_S = 7 * 86_400

#: Where each number above came from. ``INVENTED`` means nobody has backtested it and it
#: is a hypothesis, the word ``bundles.py`` and ``triage.py`` use. A test asserts this dict
#: covers every numeric module constant, so a silently added knob fails the build.
THRESHOLD_PROVENANCE: dict[str, str] = {
    "MAX_SERVICE_FANOUT": (
        "INVENTED number, MEASURED necessity. hubs.DEFAULT_DEGREE_CAP is 200 and is "
        "computed only from `transfers`, which on this database holds 5,094 rows over 293 "
        "senders, so it never fires: a first run of cluster.derive_all + "
        "entity.build_entities produced a single 876-address 'operator'. Sweeping the cap "
        "over the hard-edge closure of the same graph gave a largest component of 873 (no "
        "cap), 626 (50), 169 (25), 106 (20), 34 (10); the three-address gap from 876 is "
        "soft-edge bridging, which that sweep left out. 20 is taken from the research "
        "anchor rather than from the curve — "
        "bundlers fan out to about 20 sub-wallets (docs/research/03-wallet-clustering.md "
        "heuristic 6) and Arbitrum's sybil review cut clusters above 20 — because choosing "
        "the value that makes the giant component disappear is fitting the threshold to "
        "this week's tape. It blocks 51 Solana addresses out of 12,611. It will drop real "
        "wallets: hubs.py's own rule applies, that losing one user's edges costs one edge "
        "while keeping one hub costs the whole graph."
    ),
    "MAX_ENTITY_SIZE": (
        "INVENTED. Fan-out pruning removes the service in the middle; it does not stop a "
        "chain of individually-innocent addresses from closing a component, and 106- and "
        "62-member components survive it here. The median pump.fun funding cluster is "
        "three wallets (Szwajcok 2026) and cluster.py already caps bundle, lookup-table "
        "and co-buy groups at 25, so 25 is the house number rather than a measured cut "
        "point. Its direction of error is the reason it is safe to guess: a component "
        "above it is not discarded, it is quarantined, so both readings still see it."
    ),
    "CONFLUENCE_MIN_CONFIDENCE": (
        "DERIVED from cluster.LEAD_LAG_CONFIDENCE (0.6), which is the weakest edge this "
        "system emits. The confluence reading is deliberately set to collapse on every "
        "edge type we produce, including the one entity.py refuses to merge on, because "
        "for a gate that asks whether five *opinions* agree a copy bot is not a sixth "
        "opinion. Lowering it further would do nothing; raising it would start silently "
        "restoring the fake independence this module exists to remove."
    ),
    "MAX_SHARED_FEE_PAYER_WALLETS": (
        "INVENTED, and copied deliberately from bundles.MAX_SHARED_FEE_PAYER_WALLETS "
        "(which is 25) at the tighter value of 20 so it matches MAX_SERVICE_FANOUT: a "
        "third-party fee payer is evidence of one operator only while the sponsorship is "
        "narrow. A launch service or relayer paying for hundreds of strangers produces an "
        "identical edge, and merging on it collapses a crowd into one entity."
    ),
    "MIN_SHARED_FEE_PAYER_TXS": (
        "INVENTED. One shared sponsorship can be a single relayed transaction; a pair that "
        "recurs is a standing arrangement. Two is the smallest value that expresses "
        "'recurs' and no false-positive rate has been measured for it."
    ),
    "SHARED_FEE_PAYER_CONFIDENCE": (
        "INVENTED, placed by argument between cluster.SHARED_CEX_DEPOSIT_CONFIDENCE (0.75) "
        "and cluster.COOCCUR_MIN_CONFIDENCE (0.70). It sits above entity.py's soft floor of "
        "0.70 so the edge can contribute to a merge when a second soft type agrees, and "
        "below the CEX-deposit rule, which is the better-evidenced of the two. "
        "swaps.fee_payer is NULL on every pump.fun row (21,245 of 24,909 when this was "
        "written), so the rule currently sees only the Helius-backfilled slice and the "
        "signer lists in swap_meta, and strengthens on its own as that column is repaired."
    ),
    "VERIFIED_FUNDER_WINDOW_DAYS": (
        "INVENTED number, MEASURED necessity. cluster.SAME_FUNDER_WINDOW_DAYS is 30 and is "
        "right for the rule it guards: that one reads 'first inbound transfer we happen to "
        "hold', so simultaneity is the corroboration that the transfer really was the "
        "funding. This rule reads the *earliest transaction on chain*, verified by balance "
        "delta, so the funder is the funder whenever it happened and the graduated "
        "confidence bonuses already reward simultaneity without a cliff. Measured over "
        "1,373 resolved funding facts on the live database — 3,468 candidate pairs from "
        "non-hub funders under the group cap — first-funding gaps are strongly bimodal: "
        "1,964 within a day (57%, batch wallet creation), then 159 within a week, 309 "
        "within a month, 308 within three, 545 within a year, and 183 beyond one. The "
        "inherited 30-day window keeps 70% and drops 30%; 365 days keeps 95%. Dropping a "
        "real pair is an under-merge, the fail-open direction for confluence and the one "
        "this module is biased against, so the cut goes at the point where the remaining "
        "tail is small and the most likely shared 'funder' in it is a service nobody has "
        "labelled yet. The number is a judgement about that 5% tail, not a measurement "
        "of it. Note that an earlier reading on a 353-row prefix of the same sample said "
        "30 days dropped 61%; the prefix was ordered by token and was not representative. "
        "Re-measure this from the whole table, not from whatever the pass has bought so far."
    ),
    "DEFAULT_FUNDING_CREDIT_BUDGET": (
        "OPERATIONAL, not a decision boundary. 10,000 credits is 1% of the 1,000,000-credit "
        "monthly Helius allowance in config/risk.yaml and buys ~1,000 wallets at the "
        "10-credit floor. The pass stops at the budget rather than at a wallet count so "
        "that a route whose price changes cannot silently overspend."
    ),
    "FUNDING_CREDITS_PER_WALLET": (
        "MEASURED and documented: helius.CREDIT_COSTS marks getTransactionsForAddress as "
        "'10 credits per 100 returned transactions, rounded up; 10-credit minimum' with "
        "cost_verified=True. One wallet's earliest transaction is a one-row page, so it "
        "costs the 10-credit floor. Used only to plan the batch; the credits reported are "
        "the before/after difference of helius.budget_status, never this estimate."
    ),
    "FUNDING_RETRY_AFTER_S": (
        "OPERATIONAL. A wallet's first funder is immutable, so a successful lookup is never "
        "repeated. This governs only how long a *failed* lookup is remembered before we pay "
        "to try again; a week keeps a rebuild from re-buying the same failures daily."
    ),
}


# --------------------------------------------------------------------------------------
# what each rule actually claims
# --------------------------------------------------------------------------------------


class RuleStrength(StrEnum):
    """What an edge type claims, ordered from a statement about keys to one about habits.

    This is the "how strong is the rule" half of an edge, kept separate from the
    per-instance ``confidence`` so a consumer can choose a bar by *kind* of evidence and
    not only by a number. ``confidence`` says how well this instance matched its rule;
    strength says what the rule would mean if it matched perfectly.
    """

    CONTROL = "control"  # one signature authorised both sides: as close to proof as we get
    FLOW = "flow"  # value moved directly between them
    SPONSORSHIP = "sponsorship"  # a third party paid for or funded both
    BEHAVIOUR = "behaviour"  # they repeatedly act alike
    TIMING = "timing"  # one reliably follows the other


RULE_STRENGTH: dict[EdgeType, RuleStrength] = {
    EdgeType.CO_SIGNED: RuleStrength.CONTROL,
    EdgeType.SAME_BUNDLE: RuleStrength.CONTROL,
    EdgeType.SHARED_ALT_AUTHORITY: RuleStrength.CONTROL,
    EdgeType.DIRECT_TRANSFER: RuleStrength.FLOW,
    EdgeType.SAME_FUNDER: RuleStrength.SPONSORSHIP,
    EdgeType.SHARED_CEX_DEPOSIT: RuleStrength.SPONSORSHIP,
    EdgeType.SHARED_COUNTERPARTY: RuleStrength.SPONSORSHIP,
    EdgeType.SAME_SLOT_BUY: RuleStrength.BEHAVIOUR,
    EdgeType.FIRST_N_COOCCUR: RuleStrength.BEHAVIOUR,
    EdgeType.LEAD_LAG: RuleStrength.TIMING,
}

#: What produces each edge when the two addresses are *not* one operator. An edge is
#: evidence, and evidence has an alternative explanation; this is where it is written down
#: so a consumer choosing a bar can see what it is accepting.
RULE_TRAP: dict[EdgeType, str] = {
    EdgeType.CO_SIGNED: (
        "a Squads multisig, a mint keypair signing create, or a relayer that co-signs for "
        "its customers. Bounded by cluster.CO_SIGNED_MAX_SIGNERS and by MAX_SERVICE_FANOUT."
    ),
    EdgeType.SAME_BUNDLE: (
        "several operators buying one hyped launch in the same slot behind one tip. Bounded "
        "by cluster.SAME_BUNDLE_MAX_GROUP."
    ),
    EdgeType.SHARED_ALT_AUTHORITY: (
        "a public protocol lookup table. Produces nothing today: no ingest route resolves a "
        "table's authority, so swap_meta.alt_authority is NULL on all 8,559 rows."
    ),
    EdgeType.DIRECT_TRANSFER: (
        "a payment. Two strangers settling an OTC trade produce this edge, and it is the "
        "heuristic adversaries optimise against by routing through an exchange."
    ),
    EdgeType.SAME_FUNDER: (
        "a CEX withdrawal, a launch service, a faucet or a fee sponsor. The single most "
        "poisonable rule here, which is why entity.py will not merge on it alone."
    ),
    EdgeType.SHARED_CEX_DEPOSIT: "an omnibus or shared-custody deposit address.",
    EdgeType.SHARED_COUNTERPARTY: (
        "a paymaster, relayer or launch service sponsoring unrelated users' transactions."
    ),
    EdgeType.SAME_SLOT_BUY: (
        "two bots reacting to the same feed within one 250 ms slot. Requires repetition "
        "across distinct tokens precisely because one slot proves nothing."
    ),
    EdgeType.FIRST_N_COOCCUR: "two snipers subscribed to the same launch stream.",
    EdgeType.LEAD_LAG: (
        "a copy trader. Usually a *different person* with the same information, which is "
        "why entity.py never merges it and why the confluence reading always does."
    ),
}


class Policy(StrEnum):
    """Which direction of error the caller cannot afford."""

    #: Five entities must agree. Under-merging manufactures agreement, so collapse hard.
    CONFLUENCE = "confluence"
    #: One cluster must not hold too much. Over-merging manufactures concentration, so
    #: collapse only on what has been asserted, and refuse while anything is unresolved.
    IDENTITY = "identity"


class Coverage(StrEnum):
    """What we know about one address. There is no ``independent``; that is not a fact.

    ``UNCHECKED`` is the value this whole module exists to make visible. An unchecked
    address has never been exposed to a rule that could have merged it, so counting it as
    an independent operator is counting our own ignorance as evidence.
    """

    CLUSTERED = "clustered"
    UNCLUSTERED = "unclustered"
    QUARANTINED = "quarantined"
    HUB = "hub"
    UNCHECKED = "unchecked"


#: Persisted statuses. ``UNCHECKED`` is derived from ``clustering_coverage.checked`` and
#: from the absence of a row, so it is not one of the values the table stores.
_STORED_STATUSES = frozenset(
    {Coverage.CLUSTERED, Coverage.UNCLUSTERED, Coverage.QUARANTINED, Coverage.HUB}
)


# --------------------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FundingFact:
    """Who first funded an address, with the receipt that paid for the answer.

    ``retryable`` separates the two ways a lookup fails, which need opposite handling.
    "We read the earliest transaction and it did not fund this address" is an answer: it
    is stored, it cost credits, and re-buying it would waste them. "The limiter refused"
    or "the provider is down" is not an answer: it cost nothing and must not be recorded
    as settled, or a 0.1-second rate limit buys a seven-day cooldown. That happened — the
    first live run left three addresses unqueryable for a week over a retry-in-0.1s
    refusal — which is why the distinction exists as a field rather than as a convention.
    """

    chain: Chain
    address: str
    funder: str | None
    amount_lamports: int | None
    funded_ms: int | None
    tx: str | None
    basis: EvidenceBasis
    reason: str
    receipt: Receipt
    retryable: bool = False

    @property
    def known(self) -> bool:
        return self.funder is not None and self.basis is not EvidenceBasis.UNAVAILABLE


@dataclass
class ServiceFinding:
    """One address that behaves like a service, and the fan-out that says so."""

    address: str
    cosigners: int
    sponsored: int
    counterparties: int
    registered: bool = False

    @property
    def fanout(self) -> int:
        return max(self.cosigners, self.sponsored, self.counterparties)

    @property
    def role(self) -> str:
        if self.cosigners >= max(self.sponsored, self.counterparties):
            return "co-signer"
        if self.sponsored >= self.counterparties:
            return "fee-payer"
        return "transfer"


@dataclass
class QuarantinedComponent:
    """A component the pass declined to call one operator."""

    chain: Chain
    component_key: str
    members: list[str]
    reason: str
    edge_types: list[EdgeType] = field(default_factory=list)
    held_by: list[tuple[str, int]] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.members)


@dataclass
class ClusteringRun:
    """Everything one execution of :func:`run` did, in the shape the report needs."""

    run_id: int
    chain: Chain
    model: str
    started_ms: int
    finished_ms: int | None = None
    status: str = "running"
    reason: str = ""
    edges_by_rule: dict[str, int] = field(default_factory=dict)
    entities: list[Entity] = field(default_factory=list)
    quarantined: list[QuarantinedComponent] = field(default_factory=list)
    services: list[ServiceFinding] = field(default_factory=list)
    addresses_seen: int = 0
    addresses_checked: int = 0
    largest_component: int = 0
    pruned_edges: int = 0
    helius_credits: int | None = None
    funding_looked_up: int = 0
    funding_resolved: int = 0

    @property
    def entity_members(self) -> int:
        return sum(e.size for e in self.entities)

    @property
    def largest_entity(self) -> int:
        return max((e.size for e in self.entities), default=0)

    @property
    def size_distribution(self) -> dict[int, int]:
        out: dict[int, int] = defaultdict(int)
        for ent in self.entities:
            out[ent.size] += 1
        return dict(sorted(out.items()))


@dataclass(frozen=True)
class IndependenceView:
    """How many distinct operators a set of addresses represents — or that we cannot say.

    :attr:`floor` is the number a caller can defend: distinct groups among the addresses
    we actually checked. :attr:`ceiling` adds one per unchecked address, which is what
    :func:`entity.independent_entity_count` returns today. The gap between them is the
    size of the fail-open hole for this particular question.
    """

    chain: Chain
    policy: Policy
    groups: dict[str, list[str]]
    unchecked: list[str]
    unresolved: list[str]
    hubs: list[str]
    basis: EvidenceBasis

    @property
    def checked(self) -> list[str]:
        return sorted(a for members in self.groups.values() for a in members)

    @property
    def floor(self) -> int:
        """Distinct operators among the addresses we checked. Never an over-count."""
        return len(self.groups)

    @property
    def ceiling(self) -> int:
        """:attr:`floor` plus one per address we never looked at."""
        return self.floor + len(self.unchecked)

    @property
    def complete(self) -> bool:
        return not self.unchecked and not self.unresolved

    def satisfies(self, minimum: int) -> bool | None:
        """Tri-state: ``True``, ``False``, or ``None`` for "we cannot tell".

        ``None`` is falsy, so ``if view.satisfies(5):`` fails **closed** on an incomplete
        answer. That asymmetry is deliberate and is the reason this returns an optional
        bool rather than an int: an int cannot carry "we did not check", and every caller
        that has ever been handed one has silently rounded it up to "independent".
        """
        if self.unresolved:
            return None  # the graph collapsed inside this set; no count is defensible
        if self.floor >= minimum:
            return True
        if self.ceiling < minimum:
            return False
        return None  # only reachable by counting addresses we never examined

    def explain(self) -> str:
        parts = [f"{self.floor} entities from {len(self.checked)} checked addresses"]
        if self.unchecked:
            parts.append(f"{len(self.unchecked)} never checked (ceiling {self.ceiling})")
        if self.unresolved:
            parts.append(f"{len(self.unresolved)} in a quarantined component")
        if self.hubs:
            parts.append(f"{len(self.hubs)} service addresses excluded")
        return "; ".join(parts)


def _conn(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    return conn if conn is not None else get_conn()


def _write(conn: sqlite3.Connection) -> Any:
    """A write transaction unless the caller already opened one (SQLite has no nesting)."""
    return nullcontext(conn) if conn.in_transaction else tx(conn)


# --------------------------------------------------------------------------------------
# 1. service detection, measured on the evidence we hold
# --------------------------------------------------------------------------------------


def _cosigner_fanout(conn: sqlite3.Connection, chain: Chain) -> dict[str, set[str]]:
    """Distinct addresses each address has co-signed a transaction with.

    Read from ``swap_meta`` rather than ``swaps``, because ``swaps.fee_payer`` is NULL on
    every pump.fun row and the signer list is the only place a complete answer lives.
    """
    out: dict[str, set[str]] = defaultdict(set)
    for row in fetch_all(conn, "SELECT meta_json FROM swap_meta WHERE chain=?", (chain.value,)):
        meta = jload(row["meta_json"], {})
        if not isinstance(meta, dict):
            continue
        signers = [safe_normalize(str(s), chain) for s in (meta.get("signers") or []) if s]
        unique = sorted(set(signers))
        if len(unique) < 2:
            continue
        for addr in unique:
            out[addr].update(x for x in unique if x != addr)
    return out


def _sponsor_fanout(conn: sqlite3.Connection, chain: Chain) -> dict[str, set[str]]:
    """Distinct wallets each fee payer has paid for, excluding paying for itself."""
    out: dict[str, set[str]] = defaultdict(set)
    rows = fetch_all(
        conn,
        "SELECT DISTINCT fee_payer, wallet FROM swaps WHERE chain=? AND fee_payer IS NOT NULL",
        (chain.value,),
    )
    for row in rows:
        payer = safe_normalize(str(row["fee_payer"]), chain)
        wallet = safe_normalize(str(row["wallet"]), chain)
        if payer and wallet and payer != wallet:
            out[payer].add(wallet)
    return out


def _transfer_fanout(conn: sqlite3.Connection, chain: Chain) -> dict[str, set[str]]:
    """Distinct transfer counterparties, in both directions.

    ``hubs.degrees`` computes the same thing and is compared against a cap of 200. This
    exists because 200 is a cap on a *complete* transfer table and ours is a sample; the
    same measure against a tighter cap is the honest reading of a partial graph.
    """
    out: dict[str, set[str]] = defaultdict(set)
    rows = fetch_all(
        conn, "SELECT DISTINCT src, dst FROM transfers WHERE chain=? AND src<>dst", (chain.value,)
    )
    for row in rows:
        src = safe_normalize(str(row["src"]), chain)
        dst = safe_normalize(str(row["dst"]), chain)
        out[src].add(dst)
        out[dst].add(src)
    return out


def detect_service_addresses(
    conn: sqlite3.Connection | None = None,
    chain: Chain = Chain.SOL,
    cap: int = MAX_SERVICE_FANOUT,
    register: bool = True,
) -> list[ServiceFinding]:
    """Addresses whose fan-out says they are a service, whatever label they carry.

    Registers each one through :func:`hubs.add_hub` as ``kind='disperser'`` with
    ``source='clustering:fanout'`` so that ``cluster.py``'s existing :class:`HubFilter`
    prunes it before an edge is ever derived. Idempotent: :func:`hubs.add_hub` upserts on
    ``(chain, address)``.

    Set ``register=False`` to measure without writing, which is what the calibration test
    and any threshold sweep should do.
    """
    c = _conn(conn)
    cosign = _cosigner_fanout(c, chain)
    sponsor = _sponsor_fanout(c, chain)
    transfer = _transfer_fanout(c, chain)
    # Hubs someone else labelled are excluded from the finding list; ours are kept in it,
    # so a second run reports the same count instead of an empty one.
    labelled_elsewhere = {
        str(r["address"])
        for r in fetch_all(
            c,
            "SELECT address FROM hub_addresses WHERE chain=? AND source<>?",
            (chain.value, FANOUT_HUB_SOURCE),
        )
    }

    findings: list[ServiceFinding] = []
    for address in sorted(set(cosign) | set(sponsor) | set(transfer)):
        finding = ServiceFinding(
            address=address,
            cosigners=len(cosign.get(address, ())),
            sponsored=len(sponsor.get(address, ())),
            counterparties=len(transfer.get(address, ())),
        )
        if finding.fanout <= cap or address in labelled_elsewhere:
            continue
        if register:
            add_hub(
                chain,
                address,
                "disperser",
                label=f"fan-out {finding.fanout} ({finding.role}) >{cap}",
                source=FANOUT_HUB_SOURCE,
                conn=c,
            )
            finding.registered = True
        findings.append(finding)
    log.info(
        "service detection chain=%s cap=%d found=%d registered=%s",
        chain.value,
        cap,
        len(findings),
        register,
    )
    return findings


# --------------------------------------------------------------------------------------
# 2. funding, the rule the research names and the one we had no data for
# --------------------------------------------------------------------------------------


def _funder_from_earliest_tx(
    raw: dict[str, Any], address: str, chain: Chain
) -> tuple[str | None, int | None, str]:
    """``(funder, lamports_in, reason)`` from an address's earliest transaction.

    The funder is the account that lost lamports while ``address`` gained them, ranked by

    1. **could it be the source** — its outflow covers what the target gained. This has to
       come first: the fee payer always loses *something* (the fee), so ranking on
       signer-ness alone would name the relayer of a sweep rather than the account the
       lamports came out of;
    2. signer over non-signer, which breaks the usual tie in favour of the key that
       authorised the move;
    3. largest outflow.

    Balances are indexed over the static account keys, so only addresses that appear
    statically can be read; a target that only appears through an address lookup table is
    reported unresolved rather than guessed at.
    """
    transaction = raw.get("transaction")
    meta = raw.get("meta")
    if not isinstance(transaction, dict) or not isinstance(meta, dict):
        return None, None, "earliest transaction had no readable transaction/meta block"
    message = transaction.get("message")
    if not isinstance(message, dict):
        return None, None, "earliest transaction had no message block"
    keys = [safe_normalize(str(k), chain) for k in (message.get("accountKeys") or []) if k]
    pre = [int(v) for v in (meta.get("preBalances") or []) if isinstance(v, int)]
    post = [int(v) for v in (meta.get("postBalances") or []) if isinstance(v, int)]
    if not keys or len(pre) != len(post) or len(pre) < len(keys):
        return None, None, "balance arrays do not line up with the account keys"
    if address not in keys:
        return None, None, "address is not a static account key in its earliest transaction"
    target = keys.index(address)
    gained = post[target] - pre[target]
    if gained <= 0:
        return None, None, "earliest transaction did not increase this address's balance"

    header = message.get("header") if isinstance(message.get("header"), dict) else {}
    try:
        signers = max(1, int(header.get("numRequiredSignatures") or 1))
    except (TypeError, ValueError):
        signers = 1
    best: tuple[int, int, int, int] | None = None  # (covers_gain, is_signer, outflow, -index)
    for i, key in enumerate(keys):
        if i == target or not key:
            continue
        outflow = pre[i] - post[i]
        if outflow <= 0:
            continue
        candidate = (1 if outflow >= gained else 0, 1 if i < signers else 0, outflow, -i)
        if best is None or candidate > best:
            best = candidate
    if best is None:
        return None, None, "no account lost lamports in the earliest transaction"
    return keys[-best[3]], gained, ""


def lookup_funding(
    address: str,
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
) -> FundingFact:
    """Who first funded ``address``, from its earliest on-chain transaction.

    One ``getTransactionsForAddress`` page of one row, ascending: the 10-credit floor of
    the cheapest verified-cost route Helius publishes. Never raises — a provider that is
    down returns a fact with ``UNAVAILABLE`` basis and a reason, per docs/CONTRACT.md.
    """
    from kaiba.providers import helius

    c = _conn(conn)
    addr = safe_normalize(address, chain)
    if chain is not Chain.SOL:
        return FundingFact(
            chain, addr, None, None, None, None, EvidenceBasis.UNAVAILABLE,
            "funding lookup is implemented for Solana only",
            Receipt(provider=PROVIDER, endpoint=FUNDING_ENDPOINT, basis=EvidenceBasis.UNAVAILABLE),
        )
    payload, receipt = helius.get_transactions_for_address(
        addr, limit=1, sort_order="asc", transaction_details="full", ttl_s=FUNDING_RETRY_AFTER_S, conn=c
    )
    if payload is None:
        note = receipt.note or "provider returned nothing"
        return FundingFact(
            chain, addr, None, None, None, None, EvidenceBasis.UNAVAILABLE, note, receipt,
            retryable=True,
        )
    rows = payload.get("data") or []
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return FundingFact(
            chain, addr, None, None, None, None, EvidenceBasis.UNAVAILABLE,
            "no transactions returned for this address", receipt, retryable=True,
        )
    raw = rows[0]
    funder, amount, reason = _funder_from_earliest_tx(raw, addr, chain)
    block_time = raw.get("blockTime")
    funded_ms = int(block_time) * 1000 if isinstance(block_time, int) else None
    transaction = raw.get("transaction")
    signatures = transaction.get("signatures") or [] if isinstance(transaction, dict) else []
    signature = str(signatures[0]) if signatures else None
    if funder is None:
        return FundingFact(
            chain, addr, None, None, funded_ms, signature, EvidenceBasis.UNAVAILABLE, reason, receipt
        )
    return FundingFact(
        chain=chain,
        address=addr,
        funder=funder,
        amount_lamports=amount,
        funded_ms=funded_ms,
        tx=signature,
        basis=EvidenceBasis.VERIFIED_ONCHAIN,
        reason="earliest transaction, balance delta",
        receipt=receipt,
    )


def store_funding(fact: FundingFact, credits: int = 0, conn: sqlite3.Connection | None = None) -> bool:
    """Persist one funding fact. Idempotent on ``(chain, address)``; returns whether it wrote.

    A :attr:`~FundingFact.retryable` failure is *not* stored: the provider never answered,
    so there is nothing to remember except a cooldown we do not want. Everything else is
    stored, including a definite "the earliest transaction did not fund this address",
    because that answer cost credits and must not be bought twice.
    """
    if fact.retryable:
        log.debug("not storing a retryable funding failure for %s: %s", fact.address, fact.reason)
        return False
    c = _conn(conn)
    c.execute(
        "INSERT INTO clustering_funding (chain, address, funder, amount_lamports, funded_ms, tx, "
        " hops, basis, reason, provider, endpoint, observed_at_ms, credits) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(chain, address) DO UPDATE SET funder=excluded.funder, "
        " amount_lamports=excluded.amount_lamports, funded_ms=excluded.funded_ms, tx=excluded.tx, "
        " basis=excluded.basis, reason=excluded.reason, provider=excluded.provider, "
        " endpoint=excluded.endpoint, observed_at_ms=excluded.observed_at_ms, "
        " credits=clustering_funding.credits + excluded.credits",
        (
            fact.chain.value,
            fact.address,
            fact.funder,
            str(fact.amount_lamports) if fact.amount_lamports is not None else None,
            fact.funded_ms,
            fact.tx,
            1,
            fact.basis.value,
            fact.reason,
            fact.receipt.provider,
            fact.receipt.endpoint,
            fact.receipt.observed_at_ms,
            int(credits),
        ),
    )
    return True


def funding_candidates(
    conn: sqlite3.Connection | None = None,
    chain: Chain = Chain.SOL,
    limit: int = 1000,
    min_tokens: int = 1,
) -> list[str]:
    """Addresses worth paying for, most informative first.

    Ordered by distinct tokens bought, then by buy count: an address that recurs across
    launches is the one whose funder can link it to something. Addresses already resolved,
    already known to be hubs, or whose failed lookup is still inside
    :data:`FUNDING_RETRY_AFTER_S` are skipped, which is what makes the pass resumable.

    This is a proxy and it has a known weakness. A shared funder is only detectable when
    *both* ends are covered, so dense coverage of one crowd beats thin coverage of the
    whole tape, and "recurs across launches" does not select crowds. A caller measuring a
    specific set of launch windows should compute that set and pass it to
    :func:`enrich_funding` as ``addresses`` rather than rely on this ordering.
    """
    c = _conn(conn)
    cutoff = now_ms() - FUNDING_RETRY_AFTER_S * 1000
    done = {
        str(r["address"])
        for r in fetch_all(
            c,
            "SELECT address FROM clustering_funding WHERE chain=? AND (basis<>? OR observed_at_ms>?)",
            (chain.value, EvidenceBasis.UNAVAILABLE.value, cutoff),
        )
    }
    hubs = hub_set(chain, c)
    rows = fetch_all(
        c,
        "SELECT wallet, COUNT(DISTINCT token) AS tokens, COUNT(*) AS buys FROM swaps "
        "WHERE chain=? AND side='buy' GROUP BY wallet HAVING tokens>=? ORDER BY tokens DESC, buys DESC",
        (chain.value, int(min_tokens)),
    )
    out: list[str] = []
    for row in rows:
        address = safe_normalize(str(row["wallet"]), chain)
        if not address or address in done or address in hubs:
            continue
        out.append(address)
        if len(out) >= limit:
            break
    return out


def enrich_funding(
    conn: sqlite3.Connection | None = None,
    chain: Chain = Chain.SOL,
    max_credits: int = DEFAULT_FUNDING_CREDIT_BUDGET,
    addresses: Sequence[str] | None = None,
) -> tuple[int, int, int]:
    """Buy funding facts for as many addresses as the credit budget allows.

    Returns ``(looked_up, resolved, credits_spent)``. Credits are the before/after
    difference of :func:`helius.budget_status`, so a cached hit costs nothing and the
    figure is the provider ledger's, not an estimate of ours.
    """
    from kaiba.providers import helius

    c = _conn(conn)
    if max_credits <= 0:
        return 0, 0, 0
    if not helius.available():
        log.info("funding enrichment skipped: no Helius credential")
        return 0, 0, 0
    planned = max(1, max_credits // FUNDING_CREDITS_PER_WALLET)
    if addresses is None:
        targets = funding_candidates(c, chain, planned)
    else:
        # An explicit list is filtered the same way the default candidate query is, so a
        # caller that re-submits the same list after an interruption pays only for what is
        # still missing. Resumability has to hold for both entry points or neither.
        settled = {
            str(r["address"])
            for r in fetch_all(
                c,
                "SELECT address FROM clustering_funding WHERE chain=? AND (basis<>? OR observed_at_ms>?)",
                (chain.value, EvidenceBasis.UNAVAILABLE.value, now_ms() - FUNDING_RETRY_AFTER_S * 1000),
            )
        }
        targets = [
            a for a in (safe_normalize(x, chain) for x in addresses) if a and a not in settled
        ]
    targets = targets[:planned]
    if not targets:
        return 0, 0, 0

    def used() -> int:
        return int(helius.budget_status(conn=c).get("used") or 0)

    before = used()
    looked_up = 0
    resolved = 0
    deferred = 0
    spent = 0
    for address in targets:
        if spent >= max_credits:
            log.info("funding enrichment stopped at the %d credit budget", max_credits)
            break
        prior = used()
        fact = lookup_funding(address, chain, c)
        after_call = used()
        looked_up += 1
        if fact.known:
            resolved += 1
        if fact.retryable:
            deferred += 1
        store_funding(fact, credits=max(0, after_call - prior), conn=c)
        spent = after_call - before
    credits = max(0, used() - before)
    log.info(
        "funding enrichment chain=%s looked_up=%d resolved=%d deferred=%d credits=%d",
        chain.value, looked_up, resolved, deferred, credits,
    )
    emit(
        EventKind.PROVIDER_BUDGET,
        {"pass": "clustering.enrich_funding", "looked_up": looked_up, "resolved": resolved,
         "deferred": deferred, "credits": credits, "budget": max_credits},
        chain=chain,
        conn=c,
    )
    return looked_up, resolved, credits


def derive_funding_edges(
    conn: sqlite3.Connection | None = None, chain: Chain = Chain.SOL
) -> list[ClusterEdge]:
    """``SAME_FUNDER`` edges from :data:`clustering_funding`.

    ``cluster.derive_same_funder`` reads ``transfers`` and ``wallets.first_funder``; this
    reads the receipted table this pass fills, and uses ``cluster``'s own confidence
    constants so there is exactly one definition of how strong a funder link is. Groups
    above ``cluster.SAME_FUNDER_GROUP_CAP`` are dropped as dispersers, matching that
    function, and the hub filter is applied to both ends — on the live database that alone
    removed a 117-wallet group funded straight out of Binance's labelled hot wallet, which
    is 117 unrelated exchange customers and not an operator. The largest *unlabelled*
    funder group left after that is 39 wallets, under the cap of 50 and therefore linked;
    the run report names those addresses as the first candidates for a human label.

    The one place it deliberately departs from ``cluster.derive_same_funder`` is the time
    window — see :data:`VERIFIED_FUNDER_WINDOW_DAYS`. A funder proved by balance delta on
    an address's earliest transaction does not stop being the funder after thirty days.

    A funder feeding, say, fifteen wallets is deliberately *not* added to
    :func:`detect_service_addresses`' fan-out measure, even though fifteen is below the
    group cap. The reason is the direction of error. A ``SAME_FUNDER`` edge cannot merge an
    identity on its own (``entity.py`` needs two soft types), so a wrong one cannot invent
    concentration; but the confluence reading does collapse on it, where a wrong one only
    makes the gate harder to fire. Labelling such a funder a hub would move the error to
    the side this module is biased against.
    """
    c = _conn(conn)
    hubs = HubFilter(chain, c)
    rows = fetch_all(
        c,
        "SELECT address, funder, amount_lamports, funded_ms, tx FROM clustering_funding "
        "WHERE chain=? AND funder IS NOT NULL AND basis<>?",
        (chain.value, EvidenceBasis.UNAVAILABLE.value),
    )
    by_funder: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        address = safe_normalize(str(row["address"]), chain)
        funder = safe_normalize(str(row["funder"]), chain)
        if hubs.blocked(address) or hubs.blocked(funder) or address == funder:
            continue
        by_funder[funder].append(dict(row) | {"address": address})

    window_ms = VERIFIED_FUNDER_WINDOW_DAYS * 86_400_000
    edges: list[ClusterEdge] = []
    for funder, children in by_funder.items():
        if len(children) < 2 or len(children) > cluster.SAME_FUNDER_GROUP_CAP:
            continue
        for left, right in combinations(sorted(children, key=lambda r: str(r["address"])), 2):
            ts_a, ts_b = left["funded_ms"], right["funded_ms"]
            if ts_a and ts_b and abs(int(ts_a) - int(ts_b)) > window_ms:
                continue
            confidence = cluster.SAME_FUNDER_BASE
            amt_a = int(left["amount_lamports"]) if left["amount_lamports"] else None
            amt_b = int(right["amount_lamports"]) if right["amount_lamports"] else None
            if amt_a and amt_b:
                spread = abs(amt_a - amt_b) / max(amt_a, amt_b)
                if spread <= cluster.SAME_FUNDER_AMOUNT_TOLERANCE:
                    confidence += cluster.SAME_FUNDER_AMOUNT_BONUS
            if ts_a and ts_b and abs(int(ts_a) - int(ts_b)) <= cluster.SAME_FUNDER_TIME_WINDOW_MS:
                confidence += cluster.SAME_FUNDER_TIME_BONUS
            stamps = [int(t) for t in (ts_a, ts_b) if t]
            evidence = [f"funder:{funder}", "hops:1/1"]
            evidence += [str(s) for s in (left["tx"], right["tx"]) if s]
            edges.append(
                _edge(
                    chain,
                    str(left["address"]),
                    str(right["address"]),
                    EdgeType.SAME_FUNDER,
                    confidence,
                    evidence,
                    min(stamps) if stamps else None,
                    max(stamps) if stamps else None,
                )
            )
    return edges


# --------------------------------------------------------------------------------------
# 3. shared fee payer — written now so it strengthens when swaps.fee_payer fills
# --------------------------------------------------------------------------------------


def _edge(
    chain: Chain,
    a: str,
    b: str,
    edge_type: EdgeType,
    confidence: float,
    evidence: Sequence[str],
    first_ms: int | None,
    last_ms: int | None,
    observations: int = 1,
) -> ClusterEdge:
    lo, hi = (a, b) if a <= b else (b, a)
    stamp = now_ms()
    return ClusterEdge(
        chain=chain,
        a=lo,
        b=hi,
        edge_type=edge_type,
        confidence=min(1.0, max(0.0, confidence)),
        observations=max(1, observations),
        first_seen_ms=first_ms or stamp,
        last_seen_ms=last_ms or first_ms or stamp,
        evidence=list(evidence)[: cluster.MAX_EVIDENCE],
    )


def derive_shared_fee_payer(
    conn: sqlite3.Connection | None = None, chain: Chain = Chain.SOL
) -> list[ClusterEdge]:
    """Two wallets whose transactions are repeatedly paid for by the same third party.

    Distinct from ``cluster.derive_co_signed``, which links wallets inside *one*
    transaction. This links them across transactions, which is weaker — a paymaster serves
    many customers — so it is emitted as :attr:`EdgeType.SHARED_COUNTERPARTY`, a soft type
    that ``entity.py`` will only merge on when a second soft type agrees.

    ``swaps.fee_payer`` is NULL on every pump.fun row (21,245 of 24,909 when this was
    written), so it currently sees only the Helius-backfilled slice and the signer lists in
    ``swap_meta``. It reads both sources so that it strengthens by itself as that column is
    repaired, rather than needing a second change later.
    """
    c = _conn(conn)
    hubs = HubFilter(chain, c)
    sponsored: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    stamps: dict[tuple[str, str], list[int]] = defaultdict(list)

    rows = fetch_all(
        c,
        "SELECT fee_payer, wallet, tx, ts_ms FROM swaps WHERE chain=? AND fee_payer IS NOT NULL",
        (chain.value,),
    )
    for row in rows:
        payer = safe_normalize(str(row["fee_payer"]), chain)
        wallet = safe_normalize(str(row["wallet"]), chain)
        if not payer or not wallet or payer == wallet:
            continue
        if hubs.blocked(payer) or hubs.blocked(wallet):
            continue
        sponsored[payer][wallet].add(str(row["tx"]))
        stamps[(payer, wallet)].append(int(row["ts_ms"] or 0))

    for row in fetch_all(c, "SELECT tx, meta_json FROM swap_meta WHERE chain=?", (chain.value,)):
        meta = jload(row["meta_json"], {})
        if not isinstance(meta, dict) or not meta.get("fee_payer"):
            continue
        payer = safe_normalize(str(meta["fee_payer"]), chain)
        if hubs.blocked(payer):
            continue
        for signer in meta.get("signers") or []:
            wallet = safe_normalize(str(signer), chain)
            if not wallet or wallet == payer or hubs.blocked(wallet):
                continue
            sponsored[payer][wallet].add(str(row["tx"]))

    edges: list[ClusterEdge] = []
    for payer, wallets in sponsored.items():
        if len(wallets) < 2 or len(wallets) > MAX_SHARED_FEE_PAYER_WALLETS:
            continue
        eligible = sorted(w for w, txs in wallets.items() if len(txs) >= MIN_SHARED_FEE_PAYER_TXS)
        for a, b in combinations(eligible, 2):
            observed = [t for t in stamps.get((payer, a), []) + stamps.get((payer, b), []) if t]
            edges.append(
                _edge(
                    chain,
                    a,
                    b,
                    EdgeType.SHARED_COUNTERPARTY,
                    SHARED_FEE_PAYER_CONFIDENCE,
                    [f"feepayer:{payer}", *sorted(wallets[a])[:2], *sorted(wallets[b])[:2]],
                    min(observed) if observed else None,
                    max(observed) if observed else None,
                    observations=len(wallets[a]) + len(wallets[b]),
                )
            )
    return edges


# --------------------------------------------------------------------------------------
# 4. the pass
# --------------------------------------------------------------------------------------


def prune_hub_edges(conn: sqlite3.Connection | None = None, chain: Chain = Chain.SOL) -> int:
    """Delete edges with a labelled hub at either end. Returns rows removed.

    ``cluster.persist_edges`` only ever inserts and updates, so an edge derived before an
    address was known to be a service outlives the label. That matters because the label
    usually arrives *because* the edge was wrong: the fan-out detector runs on a graph the
    derivation already built. Leaving those rows would keep the old giant component alive
    through an address the system now says is an exchange or a disperser.

    Deleting is safe in the way that matters: every edge here is re-derivable from
    ``swaps``, ``transfers`` and ``swap_meta`` on the next pass, and an edge through a hub
    violates the invariant ``hubs.py`` states in its first paragraph. It is still the one
    destructive write this module makes, so it is counted and logged.
    """
    c = _conn(conn)
    hubs = hub_set(chain, c)
    if not hubs:
        return 0
    removed = 0
    ordered = sorted(hubs)
    for i in range(0, len(ordered), 400):
        batch = ordered[i : i + 400]
        placeholders = ",".join("?" for _ in batch)
        cur = c.execute(
            f"DELETE FROM cluster_edges WHERE chain=? AND (a IN ({placeholders}) "
            f"OR b IN ({placeholders}))",
            [chain.value, *batch, *batch],
        )
        removed += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    if removed:
        log.info("pruned %d edges touching a labelled hub on %s", removed, chain.value)
    return removed


def _component_key(chain: Chain, members: Iterable[str]) -> str:
    """Stable id for a component, so a re-run neither duplicates nor renumbers it."""
    return f"{chain.value}:comp:{digest(sorted(set(members)))[:16]}"


def _address_universe(conn: sqlite3.Connection, chain: Chain) -> set[str]:
    """Every address on this chain the pass could have had an opinion about."""
    out: set[str] = set()
    queries = (
        ("SELECT DISTINCT wallet AS a FROM swaps WHERE chain=?", (chain.value,)),
        ("SELECT DISTINCT fee_payer AS a FROM swaps WHERE chain=? AND fee_payer IS NOT NULL", (chain.value,)),
        ("SELECT DISTINCT wallet AS a FROM first_buyers WHERE chain=?", (chain.value,)),
        ("SELECT DISTINCT src AS a FROM transfers WHERE chain=?", (chain.value,)),
        ("SELECT DISTINCT dst AS a FROM transfers WHERE chain=?", (chain.value,)),
        ("SELECT DISTINCT address AS a FROM wallets WHERE chain=?", (chain.value,)),
    )
    for sql, params in queries:
        for row in fetch_all(conn, sql, params):
            value = row["a"]
            if value:
                out.add(safe_normalize(str(value), chain))
    return out


def _signer_coverage(conn: sqlite3.Connection, chain: Chain) -> set[str]:
    """Addresses for which we hold at least one *complete* signer list."""
    out: set[str] = set()
    for row in fetch_all(conn, "SELECT meta_json FROM swap_meta WHERE chain=?", (chain.value,)):
        meta = jload(row["meta_json"], {})
        if not isinstance(meta, dict) or not meta.get("signers_complete"):
            continue
        for signer in meta.get("signers") or []:
            if signer:
                out.add(safe_normalize(str(signer), chain))
    return out


def _funding_coverage(conn: sqlite3.Connection, chain: Chain) -> dict[str, str]:
    """Address → the :class:`EvidenceBasis` of its funding fact, for every address we hold one for."""
    out: dict[str, str] = {}
    for row in fetch_all(
        conn, "SELECT dst FROM transfers WHERE chain=? AND is_first_inbound=1", (chain.value,)
    ):
        out[safe_normalize(str(row["dst"]), chain)] = EvidenceBasis.DERIVED.value
    for row in fetch_all(
        conn, "SELECT address FROM wallets WHERE chain=? AND first_funder IS NOT NULL", (chain.value,)
    ):
        out[safe_normalize(str(row["address"]), chain)] = EvidenceBasis.PROVIDER_REPORTED.value
    for row in fetch_all(
        conn, "SELECT address, basis FROM clustering_funding WHERE chain=?", (chain.value,)
    ):
        out[safe_normalize(str(row["address"]), chain)] = str(row["basis"])
    return out


def _write_coverage(
    conn: sqlite3.Connection,
    chain: Chain,
    run_id: int,
    entities: Sequence[Entity],
    quarantined: Sequence[QuarantinedComponent],
    degrees: dict[str, int],
) -> tuple[int, int]:
    """One row per address the pass considered. Returns ``(seen, checked)``."""
    universe = _address_universe(conn, chain)
    signers = _signer_coverage(conn, chain)
    funding = _funding_coverage(conn, chain)
    hubs = hub_set(chain, conn)

    member_of: dict[str, str] = {}
    for ent in entities:
        for address in ent.members:
            member_of[address] = ent.entity_id
    quarantine_of: dict[str, str] = {}
    for comp in quarantined:
        for address in comp.members:
            quarantine_of[address] = comp.component_key
    universe |= set(member_of) | set(quarantine_of)

    stamp = now_ms()
    checked = 0
    for address in sorted(universe):
        funding_basis = funding.get(address, EvidenceBasis.UNAVAILABLE.value)
        has_funding = funding_basis != EvidenceBasis.UNAVAILABLE.value
        has_signers = address in signers
        rules: list[str] = []
        if has_funding:
            rules.append(EdgeType.SAME_FUNDER.value)
        if has_signers:
            rules.extend([EdgeType.CO_SIGNED.value, EdgeType.SHARED_COUNTERPARTY.value])
        is_checked = 1 if (has_funding or has_signers) else 0
        checked += is_checked
        if address in hubs:
            status = Coverage.HUB
        elif address in member_of:
            status = Coverage.CLUSTERED
        elif address in quarantine_of:
            status = Coverage.QUARANTINED
        else:
            status = Coverage.UNCLUSTERED
        conn.execute(
            "INSERT INTO clustering_coverage (chain, address, status, checked, funding_basis, "
            " signer_basis, rules_json, entity_id, component_key, degree, run_id, updated_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(chain, address) DO UPDATE SET status=excluded.status, "
            " checked=excluded.checked, funding_basis=excluded.funding_basis, "
            " signer_basis=excluded.signer_basis, rules_json=excluded.rules_json, "
            " entity_id=excluded.entity_id, component_key=excluded.component_key, "
            " degree=excluded.degree, run_id=excluded.run_id, updated_ms=excluded.updated_ms",
            (
                chain.value,
                address,
                status.value,
                is_checked,
                funding_basis,
                EvidenceBasis.VERIFIED_ONCHAIN.value if has_signers else EvidenceBasis.UNAVAILABLE.value,
                jdump(rules),
                member_of.get(address),
                quarantine_of.get(address),
                int(degrees.get(address, 0)),
                run_id,
                stamp,
            ),
        )
    return len(universe), checked


def _split_components(
    chain: Chain, entities: Sequence[Entity], edges: Sequence[ClusterEdge], max_size: int
) -> tuple[list[Entity], list[QuarantinedComponent]]:
    """Accept the components small enough to attribute; quarantine the rest."""
    accepted: list[Entity] = []
    refused: list[QuarantinedComponent] = []
    for ent in entities:
        if ent.size <= max_size:
            accepted.append(ent)
            continue
        members = set(ent.members)
        degree: dict[str, int] = defaultdict(int)
        types: set[EdgeType] = set()
        for edge in edges:
            if edge.a in members and edge.b in members:
                degree[edge.a] += 1
                degree[edge.b] += 1
                types.add(edge.edge_type)
        refused.append(
            QuarantinedComponent(
                chain=chain,
                component_key=_component_key(chain, ent.members),
                members=sorted(members),
                reason=f"{ent.size} members exceeds MAX_ENTITY_SIZE={max_size}",
                edge_types=sorted(types, key=lambda t: t.value),
                held_by=sorted(degree.items(), key=lambda kv: (-kv[1], kv[0]))[:5],
            )
        )
    return accepted, refused


def _persist_quarantine(
    conn: sqlite3.Connection,
    chain: Chain,
    run_id: int,
    components: Sequence[QuarantinedComponent],
) -> None:
    stamp = now_ms()
    for comp in components:
        conn.execute(
            "INSERT INTO clustering_quarantine (chain, component_key, size, reason, "
            " edge_types_json, held_by_json, members_json, run_id, first_seen_ms, last_seen_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(chain, component_key) DO UPDATE SET size=excluded.size, "
            " reason=excluded.reason, edge_types_json=excluded.edge_types_json, "
            " held_by_json=excluded.held_by_json, members_json=excluded.members_json, "
            " run_id=excluded.run_id, last_seen_ms=excluded.last_seen_ms",
            (
                comp.chain.value,
                comp.component_key,
                comp.size,
                comp.reason,
                jdump([t.value for t in comp.edge_types]),
                jdump([{"address": a, "degree": d} for a, d in comp.held_by]),
                jdump(comp.members),
                run_id,
                stamp,
                stamp,
            ),
        )
    # A component that has since been resolved or broken up is gone, not empty. Scoped to
    # the chain the pass ran on: a Solana run must not clear Ethereum's quarantine.
    keep = {comp.component_key for comp in components}
    rows = fetch_all(
        conn, "SELECT component_key FROM clustering_quarantine WHERE chain=?", (chain.value,)
    )
    for row in rows:
        if str(row["component_key"]) not in keep:
            conn.execute(
                "DELETE FROM clustering_quarantine WHERE chain=? AND component_key=?",
                (chain.value, row["component_key"]),
            )


def run(
    conn: sqlite3.Connection | None = None,
    chain: Chain = Chain.SOL,
    *,
    fund_credits: int = 0,
    fund_addresses: Sequence[str] | None = None,
    register_services: bool = True,
    prune_hubs: bool = True,
    max_entity_size: int = MAX_ENTITY_SIZE,
    min_confidence: float = entity.DEFAULT_MIN_CONFIDENCE,
) -> ClusteringRun:
    """Populate the entity graph end to end, and record what it could and could not see.

    Order matters. Hubs are seeded and fan-out services registered *before* derivation,
    because every heuristic in ``cluster.py`` prunes against the hub table at the moment it
    runs; registering afterwards would leave a run's worth of edges through a service in
    the table. Edges written before a label existed are then pruned, since ``persist_edges``
    never deletes and the label usually arrives *because* those edges were wrong. Funding
    is bought next so that ``SAME_FUNDER`` has data in the same pass. Entities are resolved
    last, split by size, and only then is coverage written — coverage describes the result,
    so it cannot be computed before there is one.

    Idempotent: edges upsert on their primary key, entity ids are a digest of the member
    set, component keys are a digest of the member set, and coverage and funding rows
    upsert on the address. Re-running changes ``entities.version`` and accumulates
    ``cluster_edges.observations`` — both are by design in the modules that own them — but
    creates no duplicate row and renumbers nothing.

    ``fund_credits`` defaults to 0: a pass that silently spends provider budget is not one
    a scheduler should be allowed to run by accident.
    """
    c = _conn(conn)
    started = now_ms()
    cur = c.execute(
        "INSERT INTO clustering_runs (chain, model, started_ms, status) VALUES (?,?,?,'running')",
        (chain.value, MODEL_ID, started),
    )
    run_id = int(cur.lastrowid or 0)
    result = ClusteringRun(run_id=run_id, chain=chain, model=MODEL_ID, started_ms=started)

    try:
        if not hub_set(chain, c):
            log.info("hub table empty for %s; seeding builtins", chain.value)
            seed_hubs(c)
        result.services = detect_service_addresses(c, chain, register=register_services)
        result.pruned_edges = prune_hub_edges(c, chain) if prune_hubs else 0

        if fund_credits > 0 or fund_addresses:
            looked_up, resolved, credits = enrich_funding(
                c, chain, max_credits=fund_credits, addresses=fund_addresses
            )
            result.funding_looked_up = looked_up
            result.funding_resolved = resolved
            result.helius_credits = credits

        counts = dict(cluster.derive_all(c, chain))
        for extra_type, edges in (
            (EdgeType.SAME_FUNDER, derive_funding_edges(c, chain)),
            (EdgeType.SHARED_COUNTERPARTY, derive_shared_fee_payer(c, chain)),
        ):
            if edges:
                cluster.persist_edges(edges, c)
            counts[extra_type.value] = counts.get(extra_type.value, 0) + len(edges)
        result.edges_by_rule = counts

        all_edges = cluster.load_edges(c, chain)
        degrees: dict[str, int] = defaultdict(int)
        for edge in all_edges:
            degrees[edge.a] += 1
            degrees[edge.b] += 1

        components = entity.build_entities(c, chain, min_confidence)
        result.largest_component = max((e.size for e in components), default=0)
        accepted, refused = _split_components(chain, components, all_edges, max_entity_size)
        result.entities = accepted
        result.quarantined = refused

        with _write(c):
            entity.persist_entities(accepted, c)
            _persist_quarantine(c, chain, run_id, refused)
            seen, checked = _write_coverage(c, chain, run_id, accepted, refused, degrees)
            result.addresses_seen = seen
            result.addresses_checked = checked
            result.status = "ok"
            result.finished_ms = now_ms()
            _close_run(c, result)
    except Exception as exc:  # the run row must never be left saying 'running'
        result.status = "failed"
        result.reason = f"{type(exc).__name__}: {exc}"
        result.finished_ms = now_ms()
        _close_run(c, result)
        raise

    emit(
        EventKind.SYSTEM,
        {
            "pass": "clustering.run",
            "run_id": run_id,
            "entities": len(result.entities),
            "entity_members": result.entity_members,
            "largest_entity": result.largest_entity,
            "largest_component": result.largest_component,
            "quarantined": len(result.quarantined),
            "addresses_seen": result.addresses_seen,
            "addresses_checked": result.addresses_checked,
            "edges": result.edges_by_rule,
            "helius_credits": result.helius_credits,
        },
        chain=chain,
        subject=f"clustering:{run_id}",
        conn=c,
    )
    return result


def _close_run(conn: sqlite3.Connection, result: ClusteringRun) -> None:
    conn.execute(
        "UPDATE clustering_runs SET finished_ms=?, status=?, reason=?, edges_by_rule_json=?, "
        " entities=?, entity_members=?, largest_entity=?, quarantined=?, quarantined_members=?, "
        " largest_component=?, addresses_seen=?, addresses_checked=?, fanout_hubs=?, "
        " helius_credits=?, funding_looked_up=?, funding_resolved=?, detail_json=? "
        "WHERE run_id=?",
        (
            result.finished_ms,
            result.status,
            result.reason,
            jdump(result.edges_by_rule),
            len(result.entities),
            result.entity_members,
            result.largest_entity,
            len(result.quarantined),
            sum(q.size for q in result.quarantined),
            result.largest_component,
            result.addresses_seen,
            result.addresses_checked,
            len(result.services),
            result.helius_credits,
            result.funding_looked_up,
            result.funding_resolved,
            jdump(
                {
                    "pruned_hub_edges": result.pruned_edges,
                    "size_distribution": {str(k): v for k, v in result.size_distribution.items()},
                    "services": [
                        {"address": s.address, "fanout": s.fanout, "role": s.role}
                        for s in sorted(result.services, key=lambda s: -s.fanout)[:20]
                    ],
                    "quarantine": [
                        {"key": q.component_key, "size": q.size, "held_by": q.held_by[:3]}
                        for q in result.quarantined
                    ],
                }
            ),
            result.run_id,
        ),
    )


# --------------------------------------------------------------------------------------
# 5. the read side, where "we did not check" stays visible
# --------------------------------------------------------------------------------------


def coverage_for(
    chain: Chain, addresses: Iterable[str], conn: sqlite3.Connection | None = None
) -> dict[str, Coverage]:
    """Address → :class:`Coverage`. An address with no row is :attr:`Coverage.UNCHECKED`.

    So is an address whose row says ``checked=0``: it was seen, but neither of the rules
    that make an identity claim had inputs for it, and reporting it as "unclustered" would
    be reporting the absence of a lookup as the absence of a link.
    """
    c = _conn(conn)
    wanted = list(dict.fromkeys(safe_normalize(a, chain) for a in addresses if a))
    out: dict[str, Coverage] = {a: Coverage.UNCHECKED for a in wanted}
    for i in range(0, len(wanted), 400):  # stay under SQLite's variable limit
        batch = wanted[i : i + 400]
        rows = fetch_all(
            c,
            "SELECT address, status, checked FROM clustering_coverage WHERE chain=? AND address IN "
            f"({','.join('?' for _ in batch)})",
            [chain.value, *batch],
        )
        for row in rows:
            status = Coverage(str(row["status"]))
            if status is Coverage.UNCLUSTERED and not int(row["checked"]):
                status = Coverage.UNCHECKED
            out[str(row["address"])] = status
    return out


def edges_between(
    chain: Chain,
    addresses: Iterable[str],
    conn: sqlite3.Connection | None = None,
    min_confidence: float = 0.0,
    strengths: Iterable[RuleStrength] | None = None,
) -> list[ClusterEdge]:
    """Every surviving edge with both ends inside ``addresses``.

    ``strengths`` filters by what the rule *claims* rather than by its numeric confidence,
    which is how a consumer picks its own bar: concentration can ask for
    :attr:`RuleStrength.CONTROL` only, confluence can take everything.

    Queried by address rather than through ``cluster.load_edges``, which reads the whole
    chain. :func:`independence` calls this once per signal evaluation, so a full table read
    per call would make the graph more expensive to consult the more of it there is — the
    wrong way round for something meant to be asked on every lane tick.
    """
    c = _conn(conn)
    wanted = {safe_normalize(a, chain) for a in addresses if a}
    if len(wanted) < 2:
        return []
    allowed = set(strengths) if strengths is not None else None
    ordered = sorted(wanted)
    seen: set[tuple[str, str, str]] = set()
    out: list[ClusterEdge] = []
    for i in range(0, len(ordered), 400):  # stay under SQLite's variable limit
        batch = ordered[i : i + 400]
        placeholders = ",".join("?" for _ in batch)
        rows = fetch_all(
            c,
            f"SELECT * FROM cluster_edges WHERE chain=? AND a IN ({placeholders})",
            [chain.value, *batch],
        )
        for row in rows:
            a, b = str(row["a"]), str(row["b"])
            edge_type = EdgeType(str(row["edge_type"]))
            if b not in wanted or (a, b, edge_type.value) in seen:
                continue
            confidence = float(row["confidence"])
            if confidence < min_confidence:
                continue
            if allowed is not None and RULE_STRENGTH.get(edge_type) not in allowed:
                continue
            seen.add((a, b, edge_type.value))
            evidence = jload(str(row["evidence_json"]), [])
            out.append(
                ClusterEdge(
                    chain=chain,
                    a=a,
                    b=b,
                    edge_type=edge_type,
                    confidence=confidence,
                    observations=int(row["observations"]),
                    first_seen_ms=int(row["first_seen_ms"]),
                    last_seen_ms=int(row["last_seen_ms"]),
                    evidence=[str(e) for e in evidence] if isinstance(evidence, list) else [],
                )
            )
    return out


def independence(
    chain: Chain,
    addresses: Iterable[str],
    conn: sqlite3.Connection | None = None,
    policy: Policy = Policy.CONFLUENCE,
    min_confidence: float = CONFLUENCE_MIN_CONFIDENCE,
) -> IndependenceView:
    """How many distinct operators ``addresses`` represent, under one policy.

    :attr:`Policy.CONFLUENCE` collapses on persisted entities, on quarantined components
    (they are linked; only the identity claim was refused) and on any single edge at or
    above ``min_confidence``, including ``LEAD_LAG``. It over-merges on purpose, because
    for this gate a fake crowd is the expensive error.

    :attr:`Policy.IDENTITY` collapses only on persisted entities and reports quarantined
    addresses as :attr:`~IndependenceView.unresolved`, which makes
    :meth:`IndependenceView.satisfies` return ``None`` rather than a number that would
    understate concentration.
    """
    c = _conn(conn)
    wanted = list(dict.fromkeys(safe_normalize(a, chain) for a in addresses if a))
    if not wanted:
        return IndependenceView(chain, policy, {}, [], [], [], EvidenceBasis.UNAVAILABLE)

    cover = coverage_for(chain, wanted, c)
    hubs = [a for a in wanted if cover[a] is Coverage.HUB]
    unchecked = [a for a in wanted if cover[a] is Coverage.UNCHECKED]
    quarantined = [a for a in wanted if cover[a] is Coverage.QUARANTINED]
    considered = [a for a in wanted if cover[a] in (Coverage.CLUSTERED, Coverage.UNCLUSTERED)]

    unresolved: list[str] = []
    if policy is Policy.IDENTITY:
        unresolved = sorted(quarantined)
    else:
        considered = sorted(set(considered) | set(quarantined))

    keys: dict[str, str] = {}
    entity_ids = entity.entity_ids_for(chain, considered, c)
    component_keys = _component_keys_for(chain, considered, c)
    for address in considered:
        keys[address] = (
            entity_ids.get(address) or component_keys.get(address) or f"{entity.SOLO_PREFIX}{address}"
        )

    if policy is Policy.CONFLUENCE:
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for edge in edges_between(chain, considered, c, min_confidence=min_confidence):
            ra, rb = find(keys[edge.a]), find(keys[edge.b])
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
        keys = {address: find(key) for address, key in keys.items()}

    groups: dict[str, list[str]] = defaultdict(list)
    for address, key in keys.items():
        groups[key].append(address)

    if unchecked or unresolved:
        basis = EvidenceBasis.ESTIMATED  # the count depends on addresses nobody examined
    elif considered:
        basis = EvidenceBasis.DERIVED
    else:
        basis = EvidenceBasis.UNAVAILABLE  # every address given was a service
    return IndependenceView(
        chain=chain,
        policy=policy,
        groups={k: sorted(v) for k, v in sorted(groups.items())},
        unchecked=sorted(unchecked),
        unresolved=unresolved,
        hubs=sorted(hubs),
        basis=basis,
    )


def _component_keys_for(
    chain: Chain, addresses: Sequence[str], conn: sqlite3.Connection
) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in range(0, len(addresses), 400):
        batch = list(addresses[i : i + 400])
        rows = fetch_all(
            conn,
            "SELECT address, component_key FROM clustering_coverage WHERE chain=? AND "
            f"component_key IS NOT NULL AND address IN ({','.join('?' for _ in batch)})",
            [chain.value, *batch],
        )
        for row in rows:
            out[str(row["address"])] = str(row["component_key"])
    return out


def last_run(chain: Chain = Chain.SOL, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    """The most recent run row, for the dashboard and for a freshness check."""
    return fetch_one(
        _conn(conn),
        "SELECT * FROM clustering_runs WHERE chain=? ORDER BY run_id DESC LIMIT 1",
        (chain.value,),
    )


def quarantine(
    chain: Chain = Chain.SOL, conn: sqlite3.Connection | None = None
) -> list[QuarantinedComponent]:
    """Components the pass refused to call one operator, largest first."""
    out: list[QuarantinedComponent] = []
    rows = fetch_all(
        _conn(conn),
        "SELECT * FROM clustering_quarantine WHERE chain=? ORDER BY size DESC",
        (chain.value,),
    )
    for row in rows:
        held = jload(str(row["held_by_json"]), [])
        types: list[EdgeType] = []
        for value in jload(str(row["edge_types_json"]), []) or []:
            try:
                types.append(EdgeType(str(value)))
            except ValueError:
                continue
        out.append(
            QuarantinedComponent(
                chain=Chain(str(row["chain"])),
                component_key=str(row["component_key"]),
                members=[str(m) for m in (jload(str(row["members_json"]), []) or [])],
                reason=str(row["reason"]),
                edge_types=types,
                held_by=[
                    (str(h.get("address")), int(h.get("degree", 0)))
                    for h in held
                    if isinstance(h, dict)
                ],
            )
        )
    return out


def coverage_summary(chain: Chain = Chain.SOL, conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """Counts per :class:`Coverage` value, with ``unchecked`` split out of ``unclustered``."""
    rows = fetch_all(
        _conn(conn),
        "SELECT status, checked, COUNT(*) AS n FROM clustering_coverage WHERE chain=? "
        "GROUP BY status, checked",
        (chain.value,),
    )
    out: dict[str, int] = {v.value: 0 for v in Coverage}
    for row in rows:
        status = str(row["status"])
        if status == Coverage.UNCLUSTERED.value and not int(row["checked"]):
            status = Coverage.UNCHECKED.value
        out[status] = out.get(status, 0) + int(row["n"])
    return out


__all__ = [
    "CONFLUENCE_MIN_CONFIDENCE",
    "FANOUT_HUB_SOURCE",
    "MAX_ENTITY_SIZE",
    "MAX_SERVICE_FANOUT",
    "MODEL_ID",
    "RULE_STRENGTH",
    "RULE_TRAP",
    "THRESHOLD_PROVENANCE",
    "VERIFIED_FUNDER_WINDOW_DAYS",
    "ClusteringRun",
    "Coverage",
    "FundingFact",
    "IndependenceView",
    "Policy",
    "QuarantinedComponent",
    "RuleStrength",
    "ServiceFinding",
    "coverage_for",
    "coverage_summary",
    "derive_funding_edges",
    "derive_shared_fee_payer",
    "detect_service_addresses",
    "edges_between",
    "enrich_funding",
    "funding_candidates",
    "independence",
    "last_run",
    "lookup_funding",
    "prune_hub_edges",
    "quarantine",
    "run",
    "store_funding",
]
