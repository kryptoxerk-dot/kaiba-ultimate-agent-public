"""Entity resolution: collapsing the wallet graph into operators.

The edges come from :mod:`kaiba.intelligence.cluster`; this module decides who is one hand.
It exists because of one number — the confluence lane fires when *five independent* graded
entities buy the same token. If five wallets run by one operator count as five, the lane is
not a confluence signal, it is a bundler's exit liquidity plan. :func:`independent_entity_count`
is that guard, and everything else here exists to make it right.

The resolution is deliberately two-tier:

1. **Union-find over hard edges only.** Co-signing, Jito bundles, shared lookup-table
   authority and direct transfers are claims about key control. This is the conservative
   core and it is what a merge means by default.
2. **Soft edges refine it, and only in pairs.** A shared funder alone is not identity — CEX
   withdrawals, launch services and fee sponsors all produce it. Two hard components merge
   on soft evidence only when **at least two distinct soft edge types** connect them.
   LEAD_LAG never merges at all: a copier is a different person with the same information,
   and merging them would quietly delete an independent vote from the confluence count.

A wrong merge is worse than a missed one here: it cannot be seen downstream, it just makes
five people look like one (or one look like five). Hence a bias to under-merge, entity
confidence set to the *weakest* edge in the merge path, and no single-member entities.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence
from contextlib import nullcontext

from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump, jload, tx
from kaiba.core.events import emit
from kaiba.core.schemas import (
    HARD_EDGES,
    Archetype,
    Chain,
    ClusterEdge,
    EdgeType,
    Entity,
    EventKind,
    digest,
    now_ms,
)
from kaiba.intelligence.cluster import load_edges
from kaiba.intelligence.hubs import safe_normalize

log = logging.getLogger(__name__)

#: Soft edges only merge two hard components when this many *distinct* soft types agree.
MIN_SOFT_TYPES_TO_MERGE = 2

#: Minimum confidence for a soft edge to be considered at all.
DEFAULT_MIN_CONFIDENCE = 0.70

#: Edges that are recorded, surfaced and scored but never merge anyone.
NON_MERGING_EDGES: frozenset[EdgeType] = frozenset({EdgeType.LEAD_LAG})

#: Prefix for the synthetic key an unclustered address gets when counting independence.
SOLO_PREFIX = "solo:"


def _conn(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    return conn if conn is not None else get_conn()


class _UnionFind:
    """Union-find with path compression. Small, because the graph is edge-sparse."""

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
        while parent[x] != root:  # path compression
            parent[x], x = root, parent[x]
        return root

    def union(self, a: str, b: str) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        lo, hi = (ra, rb) if ra <= rb else (rb, ra)
        self.parent[hi] = lo  # deterministic: the lexicographically smaller root wins
        return True

    def components(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for node in self.parent:
            out[self.find(node)].append(node)
        return out


def entity_id_for(chain: Chain, members: Iterable[str]) -> str:
    """Deterministic, order-independent id: same member set always gives the same id.

    That is what makes a nightly rebuild diffable — an entity keeps its id until its
    membership actually changes, and a changed membership is a genuinely different entity.
    """
    return f"{chain.value}:ent:{digest(sorted(set(members)))[:16]}"


def build_entities(
    conn: sqlite3.Connection | None = None,
    chain: Chain = Chain.SOL,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> list[Entity]:
    """Resolve the chain's edge table into entities. Pure: nothing is written."""
    c = _conn(conn)
    edges = load_edges(c, chain)
    uf = _UnionFind()
    used: list[ClusterEdge] = []

    # 1. the conservative core: hard links only.
    for edge in edges:
        if edge.edge_type not in HARD_EDGES:
            continue
        a, b = edge.sorted_pair()
        uf.union(a, b)
        used.append(edge)

    hard_root = {node: uf.find(node) for node in list(uf.parent)}

    # 2. soft links, grouped by the pair of hard components they connect.
    bridges: dict[tuple[str, str], dict[EdgeType, ClusterEdge]] = defaultdict(dict)
    for edge in edges:
        if edge.edge_type in HARD_EDGES or edge.edge_type in NON_MERGING_EDGES:
            continue
        if edge.confidence < min_confidence:
            continue
        a, b = edge.sorted_pair()
        ra, rb = hard_root.get(a, a), hard_root.get(b, b)
        if ra == rb:
            continue  # already one component; the soft edge adds nothing to the decision
        key = (ra, rb) if ra <= rb else (rb, ra)
        current = bridges[key].get(edge.edge_type)
        if current is None or edge.confidence < current.confidence:
            bridges[key][edge.edge_type] = edge  # weakest of a type: confidence is a floor

    for (ra, rb), by_type in bridges.items():
        if len(by_type) < MIN_SOFT_TYPES_TO_MERGE:
            continue
        uf.union(ra, rb)
        used.extend(by_type.values())

    # 3. assemble. Confidence is the weakest edge on the path that built the entity.
    confidences: dict[str, list[float]] = defaultdict(list)
    types: dict[str, set[EdgeType]] = defaultdict(set)
    for edge in used:
        root = uf.find(edge.a)
        confidences[root].append(edge.confidence)
        types[root].add(edge.edge_type)

    entities: list[Entity] = []
    created = now_ms()
    for root, members in sorted(uf.components().items()):
        if len(members) < 2:
            continue  # a lone wallet is not an entity, it is a wallet
        ordered = sorted(set(members))
        entities.append(
            Entity(
                entity_id=entity_id_for(chain, ordered),
                chain=chain,
                members=ordered,
                confidence=min(confidences.get(root) or [1.0]),
                archetype=Archetype.TRADER,
                edge_types=sorted(types.get(root, set()), key=lambda t: t.value),
                created_ms=created,
            )
        )
    log.info("built %d entities chain=%s from %d edges", len(entities), chain.value, len(edges))
    return entities


def persist_entities(entities: Sequence[Entity], conn: sqlite3.Connection | None = None) -> int:
    """Replace entity membership transactionally and bump each entity's version.

    Readers must never see an address in two entities or in none, so the member swap happens
    inside one transaction. ``version`` increments on every write, which is what the
    dashboard and the reflection job diff against. Joins an outer transaction when the
    caller already opened one.
    """
    c = _conn(conn)
    if not entities:
        return 0
    chains = {e.chain.value for e in entities}
    stamp = now_ms()
    payloads: list[tuple[Entity, int]] = []

    with nullcontext(c) if c.in_transaction else tx(c):
        for ent in entities:
            prior = fetch_one(
                c,
                "SELECT created_ms, version FROM entities WHERE entity_id=?",
                (ent.entity_id,),
            )
            version = int(prior["version"]) + 1 if prior else 1
            created_ms = int(prior["created_ms"]) if prior else ent.created_ms
            c.execute(
                "INSERT INTO entities (entity_id, chain, label, archetype, confidence, size, "
                " edge_types_json, created_ms, updated_ms, version) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(entity_id) DO UPDATE SET chain=excluded.chain, label=excluded.label, "
                " archetype=excluded.archetype, confidence=excluded.confidence, size=excluded.size, "
                " edge_types_json=excluded.edge_types_json, updated_ms=excluded.updated_ms, "
                " version=excluded.version",
                (
                    ent.entity_id,
                    ent.chain.value,
                    ent.label,
                    ent.archetype.value,
                    ent.confidence,
                    ent.size,
                    jdump([t.value for t in ent.edge_types]),
                    created_ms,
                    stamp,
                    version,
                ),
            )
            c.execute("DELETE FROM entity_members WHERE entity_id=?", (ent.entity_id,))
            for address in ent.members:
                c.execute(
                    "DELETE FROM entity_members WHERE chain=? AND address=?",
                    (ent.chain.value, address),
                )
                c.execute(
                    "INSERT INTO entity_members (entity_id, chain, address) VALUES (?,?,?)",
                    (ent.entity_id, ent.chain.value, address),
                )
            payloads.append((ent, version))

        # An entity whose members all moved elsewhere is gone, not empty.
        for chain_value in chains:
            c.execute(
                "DELETE FROM entities WHERE chain=? AND entity_id NOT IN "
                "(SELECT entity_id FROM entity_members)",
                (chain_value,),
            )

    for ent, version in payloads:
        emit(
            EventKind.ENTITY_UPDATED,
            {
                "entity_id": ent.entity_id,
                "size": ent.size,
                "confidence": ent.confidence,
                "version": version,
                "edge_types": [t.value for t in ent.edge_types],
                "members": ent.members[:25],
            },
            chain=ent.chain,
            subject=ent.entity_id,
            conn=c,
        )
    return len(entities)


def rebuild(
    conn: sqlite3.Connection | None = None,
    chain: Chain = Chain.SOL,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> list[Entity]:
    """Build and persist in one call — what the nightly job runs."""
    c = _conn(conn)
    entities = build_entities(c, chain, min_confidence)
    persist_entities(entities, c)
    return entities


# --------------------------------------------------------------------------- read side


def _entity_from_row(row: dict[str, object], members: list[str]) -> Entity:
    raw_types = jload(str(row["edge_types_json"]), [])
    edge_types: list[EdgeType] = []
    if isinstance(raw_types, list):
        for value in raw_types:
            try:
                edge_types.append(EdgeType(str(value)))
            except ValueError:  # an edge type we have since renamed
                continue
    return Entity(
        entity_id=str(row["entity_id"]),
        chain=Chain(str(row["chain"])),
        members=members,
        confidence=float(row["confidence"]),  # type: ignore[arg-type]
        archetype=Archetype(str(row["archetype"])),
        label=str(row["label"]) if row["label"] is not None else None,
        edge_types=edge_types,
        created_ms=int(row["created_ms"]),  # type: ignore[arg-type]
    )


def entity_members(entity_id: str, conn: sqlite3.Connection | None = None) -> list[str]:
    rows = fetch_all(
        _conn(conn),
        "SELECT address FROM entity_members WHERE entity_id=? ORDER BY address",
        (entity_id,),
    )
    return [str(r["address"]) for r in rows]


def entity_for(
    chain: Chain, address: str, conn: sqlite3.Connection | None = None
) -> Entity | None:
    """The entity an address belongs to, or ``None`` when it stands alone."""
    c = _conn(conn)
    row = fetch_one(
        c,
        "SELECT e.* FROM entities e JOIN entity_members m ON m.entity_id = e.entity_id "
        "WHERE m.chain=? AND m.address=?",
        (chain.value, safe_normalize(address, chain)),
    )
    if row is None:
        return None
    return _entity_from_row(row, entity_members(str(row["entity_id"]), c))


def entity_ids_for(
    chain: Chain, addresses: Iterable[str], conn: sqlite3.Connection | None = None
) -> dict[str, str]:
    """Address → entity id for the addresses that belong to one. One query."""
    normalised = list(dict.fromkeys(safe_normalize(a, chain) for a in addresses if a))
    if not normalised:
        return {}
    out: dict[str, str] = {}
    c = _conn(conn)
    chunk = 400  # stay well under SQLite's variable limit
    for i in range(0, len(normalised), chunk):
        batch = normalised[i : i + chunk]
        rows = fetch_all(
            c,
            "SELECT address, entity_id FROM entity_members WHERE chain=? AND address IN "
            f"({','.join('?' for _ in batch)})",
            [chain.value, *batch],
        )
        for row in rows:
            out[str(row["address"])] = str(row["entity_id"])
    return out


def independent_entity_count(
    chain: Chain, addresses: Iterable[str], conn: sqlite3.Connection | None = None
) -> int:
    """How many *distinct operators* these addresses represent.

    The confluence lane's whole premise. Addresses in one entity collapse to one vote;
    addresses we have never linked count individually, because absence of evidence is not
    evidence of independence — but it is the only honest default, and the DYOR cluster
    check is what catches the rest.
    """
    normalised = list(dict.fromkeys(safe_normalize(a, chain) for a in addresses if a))
    if not normalised:
        return 0
    mapping = entity_ids_for(chain, normalised, conn)
    return len({mapping.get(a, f"{SOLO_PREFIX}{a}") for a in normalised})


def side_wallets(
    chain: Chain, address: str, conn: sqlite3.Connection | None = None
) -> list[str]:
    """Wallets that consistently buy just after this one: copiers and side wallets.

    Read off LEAD_LAG edges, which never merge anyone. The direction lives in the evidence
    list as ``lead:<leader>``; an edge with no direction recorded is returned anyway, since
    an undirected timing relation is still worth showing the operator.
    """
    a = safe_normalize(address, chain)
    rows = fetch_all(
        _conn(conn),
        "SELECT a, b, evidence_json FROM cluster_edges "
        "WHERE chain=? AND edge_type=? AND (a=? OR b=?)",
        (chain.value, EdgeType.LEAD_LAG.value, a, a),
    )
    out: list[str] = []
    for row in rows:
        other = str(row["b"]) if str(row["a"]) == a else str(row["a"])
        evidence = jload(str(row["evidence_json"]), [])
        leaders = [
            str(e).split(":", 1)[1] for e in evidence if isinstance(e, str) and e.startswith("lead:")
        ]
        if leaders and a not in leaders:
            continue  # this address is the follower on that edge, not the leader
        if other not in out:
            out.append(other)
    return sorted(out)


def side_wallet_of(
    chain: Chain, address: str, conn: sqlite3.Connection | None = None
) -> list[str]:
    """The inverse of :func:`side_wallets`: whom this address appears to be following."""
    a = safe_normalize(address, chain)
    rows = fetch_all(
        _conn(conn),
        "SELECT a, b, evidence_json FROM cluster_edges "
        "WHERE chain=? AND edge_type=? AND (a=? OR b=?)",
        (chain.value, EdgeType.LEAD_LAG.value, a, a),
    )
    out: list[str] = []
    for row in rows:
        other = str(row["b"]) if str(row["a"]) == a else str(row["a"])
        evidence = jload(str(row["evidence_json"]), [])
        leaders = [
            str(e).split(":", 1)[1] for e in evidence if isinstance(e, str) and e.startswith("lead:")
        ]
        if other in leaders and other not in out:
            out.append(other)
    return sorted(out)


def cluster_supply_pct(
    chain: Chain,
    token: str,
    holders: dict[str, float],
    conn: sqlite3.Connection | None = None,
    include_singletons: bool = False,
) -> float:
    """Largest share of ``token`` held by a single entity.

    ``holders`` maps holder address to its share of supply; the return value is in whatever
    unit the caller used (the DYOR blocker passes percent and reviews above 20, rejects
    above 30). Unclustered addresses are ignored by default: a lone whale is a
    concentration problem, but it is not a *cluster* problem, and the two have different
    remedies. Pass ``include_singletons=True`` to fold them in.
    """
    if not holders:
        return 0.0
    c = _conn(conn)
    normalised: dict[str, float] = defaultdict(float)
    for address, pct in holders.items():
        normalised[safe_normalize(address, chain)] += float(pct)
    mapping = entity_ids_for(chain, normalised.keys(), c)
    totals: dict[str, float] = defaultdict(float)
    for address, pct in normalised.items():
        key = mapping.get(address)
        if key is None:
            if not include_singletons:
                continue
            key = f"{SOLO_PREFIX}{address}"
        totals[key] += pct
    if not totals:
        return 0.0
    top = max(totals.values())
    log.debug("cluster supply chain=%s token=%s top=%.4f", chain.value, token, top)
    return top


def entity_count(chain: Chain, conn: sqlite3.Connection | None = None) -> int:
    row = fetch_one(_conn(conn), "SELECT COUNT(*) AS n FROM entities WHERE chain=?", (chain.value,))
    return int(row["n"]) if row else 0


__all__ = [
    "MIN_SOFT_TYPES_TO_MERGE",
    "NON_MERGING_EDGES",
    "build_entities",
    "cluster_supply_pct",
    "entity_for",
    "entity_id_for",
    "entity_ids_for",
    "entity_members",
    "independent_entity_count",
    "persist_entities",
    "rebuild",
    "side_wallet_of",
    "side_wallets",
]
