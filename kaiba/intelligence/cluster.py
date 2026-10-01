"""Edge derivation for the wallet graph: who is provably the same hand, and who only looks it.

This module turns rows the ingest layer already wrote (``swaps``, ``transfers``,
``first_buyers``) into :class:`~kaiba.core.schemas.ClusterEdge` objects. It does not decide
who is one entity — that is :mod:`kaiba.intelligence.entity`. Keeping derivation and
resolution apart means a heuristic can be retuned without rebuilding the entity table, and
every merge can be explained by the edges that caused it.

Two rules hold everywhere, both from ``docs/research/03-wallet-clustering.md``:

* **Never link through a hub.** Every candidate set is run through
  :class:`kaiba.intelligence.hubs.HubFilter` first. A CEX hot wallet that funds 50 wallets
  is 50 unrelated people, not a cluster of 50.
* **Repetition, not coincidence.** Co-occurrence on one token during a hyped launch is
  noise; the same pair showing up on three separate launches is a signal (Kamat's 1,012
  rings). So the co-buy heuristics count *distinct tokens* and only emit above a floor.

Hard edges (co-signing, Jito bundles, shared lookup-table authority, direct transfers) are
claims about control and carry 0.90. Soft edges are claims about behaviour and carry less;
the entity builder needs two independent soft types before it will merge on them.

Every parameter is a module constant so the nightly tuner can move it without a code change.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

from kaiba.core.db import fetch_all, get_conn, jdump, jload, tx
from kaiba.core.schemas import Chain, ClusterEdge, EdgeType, now_ms
from kaiba.intelligence.hubs import (
    DEFAULT_DEGREE_CAP,
    JITO_TIP_ACCOUNTS,
    HubFilter,
    hub_set,
    safe_normalize,
    seed_hubs,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------- parameters

#: Confidence attached to every hard link. Not 1.0: a shared fee payer can still be a
#: relayer we have not labelled yet.
HARD_CONFIDENCE = 0.90

#: A transaction with more signers than this is a multisig or a relayer batch, not a
#: person operating several wallets (research heuristic 2 trap list).
CO_SIGNED_MAX_SIGNERS = 8
#: Treat the trading wallet of a swap as a signer alongside the fee payer. True because a
#: wallet whose tokens move has authorised the transfer; hub pruning and the degree cap are
#: what keep fee-sponsorship relayers out.
CO_SIGNED_INCLUDE_SWAP_WALLETS = True

#: Jito bundle reconstruction (research heuristic 7): same slot, same mint, slot contains a
#: tip transfer of at least this many lamports.
JITO_TIP_MIN_LAMPORTS = 1_000
#: A bundle needs at least two non-creator wallets to say anything.
SAME_BUNDLE_MIN_WALLETS = 2
#: More than this many wallets buying one mint in one slot is a launch stampede, not a bundle.
SAME_BUNDLE_MAX_GROUP = 25

#: Public protocol lookup tables are shared by everybody; only small sets mean anything.
SHARED_ALT_MAX_GROUP = 25

#: Native transfers below this (base units) are dust/spam and do not link anyone.
DIRECT_TRANSFER_MIN_AMOUNT = 1

#: Funding-source linkage (research heuristic 1).
SAME_FUNDER_BASE = 0.85
SAME_FUNDER_AMOUNT_BONUS = 0.05
SAME_FUNDER_AMOUNT_TOLERANCE = 0.02  # 2% of the larger amount
SAME_FUNDER_TIME_BONUS = 0.05
SAME_FUNDER_TIME_WINDOW_MS = 30 * 60 * 1000  # 30 minutes
SAME_FUNDER_MAX_HOPS = 3
SAME_FUNDER_WINDOW_DAYS = 30
#: Each extra hop is weaker evidence: obfuscation chains are cheap to build.
SAME_FUNDER_HOP_PENALTY = 0.05
#: A non-hub address funding more than this is an unlabelled service; skip the whole group
#: rather than emit thousands of edges (Victor excludes clusters above 1,000; the degree cap
#: of 200 normally catches these first).
#: A funder feeding more than this many wallets is behaving like a disperser, not a
#: person. Research: bundlers fan out to <=20 sub-wallets; Arbitrum cut clusters >20.
SAME_FUNDER_GROUP_CAP = 50

#: Cross-token co-occurrence (research heuristic 3): link only on >= 3 distinct tokens.
COOCCUR_MIN_TOKENS = 3
#: Confidence at COOCCUR_MIN_TOKENS. This must sit at or above the entity builder's
#: DEFAULT_MIN_CONFIDENCE, otherwise the three-token rule fires and is then discarded
#: before it can ever contribute to an entity — the edge exists but does nothing.
#: tests/test_cluster.py::test_min_token_cooccurrence_clears_the_entity_floor pins this.
COOCCUR_MIN_CONFIDENCE = 0.70
COOCCUR_MAX_CONFIDENCE = 0.85
#: Token count at which co-occurrence confidence saturates at COOCCUR_MAX_CONFIDENCE.
COOCCUR_SATURATION_TOKENS = 8
#: Slots with more buyers than this are hyped launches; pairing them all is noise.
SAME_SLOT_MAX_GROUP = 50
FIRST_N_DEFAULT = 20
FIRST_N_MAX_GROUP = 50

#: Lead/lag (research heuristic 3 + copy-trade detection). These are copiers and side
#: wallets: real information, but never an identity claim.
LEAD_LAG_MAX_DELAY_S = 30
LEAD_LAG_MIN_TOKENS = 5
LEAD_LAG_CONFIDENCE = 0.6
LEAD_LAG_MAX_GROUP = 100

#: CEX deposit-address reuse (Victor 2020). Above the cap it is an omnibus address.
SHARED_CEX_DEPOSIT_CAP = 1000
SHARED_CEX_DEPOSIT_CONFIDENCE = 0.75

#: Evidence lists are for humans reading a merge; five signatures is enough to check one.
MAX_EVIDENCE = 5

_TIP_ACCOUNTS: frozenset[str] = frozenset(JITO_TIP_ACCOUNTS)


# ------------------------------------------------------------------------------- helpers


def _conn(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    return conn if conn is not None else get_conn()


def _write(conn: sqlite3.Connection) -> AbstractContextManager[sqlite3.Connection]:
    """A write transaction, unless the caller already opened one (SQLite has no nesting)."""
    return nullcontext(conn) if conn.in_transaction else tx(conn)


def _as_int(value: Any) -> int | None:
    """Amounts are stored as TEXT to survive values above 2^63. Parse defensively."""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _cooccur_confidence(n_tokens: int, min_tokens: int) -> float:
    """0.70 at the floor, rising to 0.85 once the pair has repeated on enough launches."""
    if n_tokens <= min_tokens:
        return COOCCUR_MIN_CONFIDENCE
    span = max(1, COOCCUR_SATURATION_TOKENS - min_tokens)
    frac = min(1.0, (n_tokens - min_tokens) / span)
    return round(COOCCUR_MIN_CONFIDENCE + frac * (COOCCUR_MAX_CONFIDENCE - COOCCUR_MIN_CONFIDENCE), 4)


@dataclass
class _PairAcc:
    """Accumulator for a candidate pair while a heuristic scans the data."""

    tokens: set[str] = field(default_factory=set)
    evidence: list[str] = field(default_factory=list)
    first_ms: int | None = None
    last_ms: int | None = None
    count: int = 0

    def observe(self, token: str | None, evidence: str | None, ts_ms: int | None) -> None:
        self.count += 1
        if token is not None:
            self.tokens.add(token)
        if evidence and evidence not in self.evidence and len(self.evidence) < MAX_EVIDENCE:
            self.evidence.append(evidence)
        if ts_ms:
            self.first_ms = ts_ms if self.first_ms is None else min(self.first_ms, ts_ms)
            self.last_ms = ts_ms if self.last_ms is None else max(self.last_ms, ts_ms)


def _edge(
    chain: Chain,
    a: str,
    b: str,
    edge_type: EdgeType,
    confidence: float,
    *,
    evidence: Sequence[str] = (),
    observations: int = 1,
    first_ms: int | None = None,
    last_ms: int | None = None,
) -> ClusterEdge:
    lo, hi = (a, b) if a <= b else (b, a)
    seen = now_ms()
    return ClusterEdge(
        chain=chain,
        a=lo,
        b=hi,
        edge_type=edge_type,
        confidence=confidence,
        observations=max(1, observations),
        first_seen_ms=first_ms or seen,
        last_seen_ms=last_ms or first_ms or seen,
        evidence=list(evidence)[:MAX_EVIDENCE],
    )


def _pairs(members: Sequence[str]) -> Iterable[tuple[str, str]]:
    return combinations(sorted(set(members)), 2)


# --------------------------------------------------------------------------- swap meta


def record_swap_meta(
    conn: sqlite3.Connection | None,
    chain: Chain,
    tx_sig: str,
    meta: dict[str, Any],
) -> None:
    """Attach signer / lookup-table metadata to a transaction.

    ``swaps`` carries only the fee payer. Ingest writes the richer per-transaction facts
    here (``signers``, ``alt_authority``) so the hard-link heuristics have something to work
    with without changing a table this module does not own.
    """
    _conn(conn).execute(
        "INSERT INTO swap_meta (chain, tx, meta_json) VALUES (?,?,?) "
        "ON CONFLICT(chain, tx) DO UPDATE SET meta_json=excluded.meta_json",
        (chain.value, tx_sig, jdump(meta)),
    )


def swap_meta(conn: sqlite3.Connection | None, chain: Chain, tx_sig: str) -> dict[str, Any]:
    row = _conn(conn).execute(
        "SELECT meta_json FROM swap_meta WHERE chain=? AND tx=?", (chain.value, tx_sig)
    ).fetchone()
    return jload(row["meta_json"], {}) if row else {}


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r["name"] == column for r in fetch_all(conn, f"PRAGMA table_info({table})"))


def _all_swap_meta(conn: sqlite3.Connection, chain: Chain) -> dict[str, dict[str, Any]]:
    """Every transaction's metadata for a chain, from the side table and, if the ingest
    layer ever adds one, a ``swaps.meta_json`` column."""
    out: dict[str, dict[str, Any]] = {}
    for row in fetch_all(conn, "SELECT tx, meta_json FROM swap_meta WHERE chain=?", (chain.value,)):
        meta = jload(row["meta_json"], {})
        if isinstance(meta, dict):
            out[row["tx"]] = meta
    if _has_column(conn, "swaps", "meta_json"):
        rows = fetch_all(
            conn,
            "SELECT tx, meta_json FROM swaps WHERE chain=? AND meta_json IS NOT NULL",
            (chain.value,),
        )
        for row in rows:
            meta = jload(row["meta_json"], {})
            if isinstance(meta, dict) and meta:
                out.setdefault(row["tx"], {}).update(meta)
    return out


def _meta_signers(meta: dict[str, Any]) -> list[str]:
    value = meta.get("signers") or meta.get("signer_list") or []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return []


def _meta_alt_authorities(meta: dict[str, Any]) -> list[str]:
    value = meta.get("alt_authority") or meta.get("alt_authorities") or []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return []


# ------------------------------------------------------------------------- hard edges


def derive_co_signed(conn: sqlite3.Connection | None = None, chain: Chain = Chain.SOL) -> list[ClusterEdge]:
    """Wallets that signed the same transaction.

    The strongest link we can observe without a key: whoever signed held both keys at that
    moment. Traps are Squads multisigs, mint keypairs signing ``create`` and relayers, which
    is why oversized signer sets are dropped and hubs are pruned first.
    """
    c = _conn(conn)
    hubs = HubFilter(chain, c)
    metas = _all_swap_meta(c, chain)
    rows = fetch_all(
        c,
        "SELECT tx, wallet, fee_payer, ts_ms FROM swaps WHERE chain=? AND tx IS NOT NULL",
        (chain.value,),
    )
    per_tx: dict[str, set[str]] = defaultdict(set)
    tx_ts: dict[str, int] = {}
    for row in rows:
        sig = row["tx"]
        tx_ts.setdefault(sig, int(row["ts_ms"] or 0))
        if row["fee_payer"]:
            per_tx[sig].add(str(row["fee_payer"]))
        if CO_SIGNED_INCLUDE_SWAP_WALLETS and row["wallet"]:
            per_tx[sig].add(str(row["wallet"]))
    for sig, meta in metas.items():
        signers = _meta_signers(meta)
        if signers:
            per_tx[sig].update(signers)

    edges: list[ClusterEdge] = []
    for sig, raw_signers in per_tx.items():
        if len(raw_signers) < 2 or len(raw_signers) > CO_SIGNED_MAX_SIGNERS:
            continue
        members = hubs.keep(raw_signers)
        if len(members) < 2:
            continue
        ts = tx_ts.get(sig) or None
        for a, b in _pairs(members):
            edges.append(
                _edge(chain, a, b, EdgeType.CO_SIGNED, HARD_CONFIDENCE, evidence=[sig], first_ms=ts)
            )
    return edges


def _tip_slots(conn: sqlite3.Connection, chain: Chain) -> set[int]:
    """Slots containing a Jito tip transfer — the marker that a bundle landed."""
    rows = fetch_all(
        conn,
        "SELECT slot, dst, amount FROM transfers WHERE chain=? AND slot IS NOT NULL",
        (chain.value,),
    )
    slots: set[int] = set()
    for row in rows:
        if row["dst"] not in _TIP_ACCOUNTS:
            continue
        amount = _as_int(row["amount"]) or 0
        if amount >= JITO_TIP_MIN_LAMPORTS:
            slots.add(int(row["slot"]))
    return slots


def derive_same_bundle(
    conn: sqlite3.Connection | None = None, chain: Chain = Chain.SOL
) -> list[ClusterEdge]:
    """Wallets that bought the same mint in the same slot, in a slot that paid a Jito tip.

    Bundle ids are not on-chain, so the bundle is reconstructed: same-slot buys of one mint
    plus a tip transfer in that slot (research heuristic 7). The token creator is excluded —
    pump.fun's own create+buy is not a co-conspirator — and two non-creator wallets are the
    minimum that says anything.
    """
    c = _conn(conn)
    slots = _tip_slots(c, chain)
    if not slots:
        return []
    hubs = HubFilter(chain, c)
    creators = {
        r["address"]: r["creator"]
        for r in fetch_all(c, "SELECT address, creator FROM tokens WHERE chain=?", (chain.value,))
    }
    rows = fetch_all(
        c,
        "SELECT slot, token, wallet, tx, ts_ms FROM swaps "
        "WHERE chain=? AND side='buy' AND slot IS NOT NULL",
        (chain.value,),
    )
    groups: dict[tuple[int, str], _PairAcc] = {}
    members: dict[tuple[int, str], list[str]] = defaultdict(list)
    for row in rows:
        slot = int(row["slot"])
        if slot not in slots:
            continue
        token = row["token"]
        wallet = str(row["wallet"])
        if creators.get(token) and safe_normalize(creators[token], chain) == safe_normalize(wallet, chain):
            continue
        key = (slot, token)
        members[key].append(wallet)
        acc = groups.setdefault(key, _PairAcc())
        acc.observe(token, row["tx"], int(row["ts_ms"] or 0) or None)

    edges: list[ClusterEdge] = []
    for key, raw in members.items():
        kept = hubs.keep(raw)
        if len(kept) < SAME_BUNDLE_MIN_WALLETS or len(kept) > SAME_BUNDLE_MAX_GROUP:
            continue
        slot, token = key
        acc = groups[key]
        evidence = [f"bundle:{token}:{slot}", *acc.evidence][:MAX_EVIDENCE]
        for a, b in _pairs(kept):
            edges.append(
                _edge(
                    chain,
                    a,
                    b,
                    EdgeType.SAME_BUNDLE,
                    HARD_CONFIDENCE,
                    evidence=evidence,
                    first_ms=acc.first_ms,
                    last_ms=acc.last_ms,
                )
            )
    return edges


def derive_shared_alt_authority(
    conn: sqlite3.Connection | None = None, chain: Chain = Chain.SOL
) -> list[ClusterEdge]:
    """Wallets transacting under a lookup table whose authority is a user wallet.

    Bundlers create one address lookup table per wallet set, and its authority is their own
    key — a hard link that survives every Bubblemaps-bypass trick because the LUT has to
    exist for the bundle to fit in a transaction. This stays a placeholder until ingest
    records ``alt_authority`` in swap meta: with no metadata it returns nothing rather than
    guessing. Public protocol LUTs are excluded by hub pruning and the group cap.
    """
    c = _conn(conn)
    metas = _all_swap_meta(c, chain)
    if not metas:
        return []
    hubs = HubFilter(chain, c)
    rows = fetch_all(
        c,
        "SELECT tx, wallet, fee_payer, ts_ms FROM swaps WHERE chain=? AND tx IS NOT NULL",
        (chain.value,),
    )
    wallets_by_tx: dict[str, set[str]] = defaultdict(set)
    ts_by_tx: dict[str, int] = {}
    for row in rows:
        sig = row["tx"]
        wallets_by_tx[sig].add(str(row["wallet"]))
        if row["fee_payer"]:
            wallets_by_tx[sig].add(str(row["fee_payer"]))
        ts_by_tx.setdefault(sig, int(row["ts_ms"] or 0))

    by_authority: dict[str, set[str]] = defaultdict(set)
    acc_by_authority: dict[str, _PairAcc] = defaultdict(_PairAcc)
    for sig, meta in metas.items():
        for authority in _meta_alt_authorities(meta):
            auth = safe_normalize(authority, chain)
            if hubs.blocked(auth):
                continue
            wallets = set(wallets_by_tx.get(sig, set())) | set(_meta_signers(meta))
            if not wallets:
                continue
            by_authority[auth].update(wallets)
            acc_by_authority[auth].observe(None, sig, ts_by_tx.get(sig) or None)

    edges: list[ClusterEdge] = []
    for authority, raw in by_authority.items():
        kept = hubs.keep(raw)
        if len(kept) < 2 or len(kept) > SHARED_ALT_MAX_GROUP:
            continue
        acc = acc_by_authority[authority]
        evidence = [f"alt:{authority}", *acc.evidence][:MAX_EVIDENCE]
        for a, b in _pairs(kept):
            edges.append(
                _edge(
                    chain,
                    a,
                    b,
                    EdgeType.SHARED_ALT_AUTHORITY,
                    HARD_CONFIDENCE,
                    evidence=evidence,
                    first_ms=acc.first_ms,
                    last_ms=acc.last_ms,
                )
            )
    return edges


def _labelled_addresses(conn: sqlite3.Connection, chain: Chain) -> set[str]:
    """Wallets carrying a service tag. These are counterparties, not identities."""
    from kaiba.core.schemas import WalletTag

    service = {WalletTag.EXCHANGE.value, WalletTag.POOL.value, WalletTag.CONTRACT.value}
    out: set[str] = set()
    for row in fetch_all(conn, "SELECT address, tags_json FROM wallets WHERE chain=?", (chain.value,)):
        tags = jload(row["tags_json"], [])
        if isinstance(tags, list) and service.intersection(str(t) for t in tags):
            out.add(row["address"])
    return out


def derive_direct_transfer(
    conn: sqlite3.Connection | None = None, chain: Chain = Chain.SOL
) -> list[ClusterEdge]:
    """A native transfer between two unlabelled, non-hub wallets.

    Bubblemaps V1 clustering in one line. It is the heuristic adversaries optimise against
    (route through a CEX, randomise amounts), so it is necessary but never sufficient —
    which is exactly what treating it as one hard edge among four achieves.
    """
    c = _conn(conn)
    hubs = HubFilter(chain, c)
    labelled = _labelled_addresses(c, chain)
    rows = fetch_all(
        c,
        "SELECT src, dst, tx, ts_ms, amount FROM transfers WHERE chain=? AND src<>dst",
        (chain.value,),
    )
    acc: dict[tuple[str, str], _PairAcc] = defaultdict(_PairAcc)
    for row in rows:
        amount = _as_int(row["amount"])
        if amount is not None and amount < DIRECT_TRANSFER_MIN_AMOUNT:
            continue
        src = safe_normalize(str(row["src"]), chain)
        dst = safe_normalize(str(row["dst"]), chain)
        if src in labelled or dst in labelled:
            continue
        if hubs.blocked(src) or hubs.blocked(dst):
            continue
        key = (src, dst) if src <= dst else (dst, src)
        acc[key].observe(None, row["tx"], int(row["ts_ms"] or 0) or None)

    return [
        _edge(
            chain,
            a,
            b,
            EdgeType.DIRECT_TRANSFER,
            HARD_CONFIDENCE,
            evidence=state.evidence,
            observations=state.count,
            first_ms=state.first_ms,
            last_ms=state.last_ms,
        )
        for (a, b), state in acc.items()
    ]


# ------------------------------------------------------------------------- soft edges


@dataclass(frozen=True)
class _Funding:
    """One inbound funding event: who paid, how much, when."""

    src: str
    amount: int | None
    ts_ms: int
    tx: str | None


def _first_funders(conn: sqlite3.Connection, chain: Chain) -> dict[str, _Funding]:
    """First inbound native transfer per address, falling back to ``wallets.first_funder``."""
    rows = fetch_all(
        conn,
        "SELECT id, src, dst, amount, ts_ms, tx, is_first_inbound FROM transfers "
        "WHERE chain=? ORDER BY ts_ms ASC, id ASC",
        (chain.value,),
    )
    earliest: dict[str, _Funding] = {}
    flagged: dict[str, _Funding] = {}
    for row in rows:
        dst = safe_normalize(str(row["dst"]), chain)
        src = safe_normalize(str(row["src"]), chain)
        if src == dst:
            continue
        rec = _Funding(src, _as_int(row["amount"]), int(row["ts_ms"] or 0), row["tx"])
        earliest.setdefault(dst, rec)
        if row["is_first_inbound"]:
            flagged.setdefault(dst, rec)
    funders = {**earliest, **flagged}

    known = fetch_all(
        conn,
        "SELECT address, first_funder, first_funded_ms FROM wallets "
        "WHERE chain=? AND first_funder IS NOT NULL",
        (chain.value,),
    )
    for row in known:
        addr = safe_normalize(str(row["address"]), chain)
        if addr in funders:
            continue
        funders[addr] = _Funding(
            safe_normalize(str(row["first_funder"]), chain), None, int(row["first_funded_ms"] or 0), None
        )
    return funders


def _funder_chain(
    wallet: str,
    funders: dict[str, _Funding],
    hubs: HubFilter,
    max_hops: int,
) -> list[tuple[int, _Funding]]:
    """Walk up the funding graph, stopping at the first hub.

    Stopping — not skipping over — is the point: a CEX withdrawal severs the chain, because
    everything upstream of an exchange belongs to the exchange's other customers.
    """
    out: list[tuple[int, _Funding]] = []
    seen = {wallet}
    cur = wallet
    for hop in range(1, max_hops + 1):
        rec = funders.get(cur)
        if rec is None or rec.src in seen or hubs.blocked(rec.src):
            break
        out.append((hop, rec))
        seen.add(rec.src)
        cur = rec.src
    return out


def derive_same_funder(
    conn: sqlite3.Connection | None = None,
    chain: Chain = Chain.SOL,
    max_hops: int = SAME_FUNDER_MAX_HOPS,
    window_days: int = SAME_FUNDER_WINDOW_DAYS,
) -> list[ClusterEdge]:
    """Wallets whose funding traces back to the same private address.

    The workhorse heuristic — over half of pump.fun creators sit in a multi-address cluster
    and the median cluster is three wallets (Szwajcok 2026). Also the easiest to poison,
    hence: hubs terminate the walk, the group is dropped if the "funder" behaves like a
    service, and each extra hop costs confidence.

    Confidence is ``0.85`` base, ``+0.05`` when the two funding amounts are within 2% of
    each other, ``+0.05`` when they landed within 30 minutes (GMGN's own Suspected-Insider
    rule is same creation time + same funder + same transfer time).
    """
    c = _conn(conn)
    hubs = HubFilter(chain, c)
    funders = _first_funders(c, chain)
    window_ms = window_days * 86_400_000

    by_ancestor: dict[str, list[tuple[str, int, _Funding]]] = defaultdict(list)
    for wallet in funders:
        if hubs.blocked(wallet):
            continue
        for hop, rec in _funder_chain(wallet, funders, hubs, max_hops):
            by_ancestor[rec.src].append((wallet, hop, rec))

    edges: list[ClusterEdge] = []
    for ancestor, children in by_ancestor.items():
        if len(children) < 2 or len(children) > SAME_FUNDER_GROUP_CAP:
            continue
        ordered_children = sorted(children, key=lambda item: (item[0], item[1]))
        for (wa, hop_a, rec_a), (wb, hop_b, rec_b) in combinations(ordered_children, 2):
            if wa == wb:
                continue
            if rec_a.ts_ms and rec_b.ts_ms and abs(rec_a.ts_ms - rec_b.ts_ms) > window_ms:
                continue
            confidence = SAME_FUNDER_BASE - SAME_FUNDER_HOP_PENALTY * (max(hop_a, hop_b) - 1)
            if rec_a.amount and rec_b.amount:
                spread = abs(rec_a.amount - rec_b.amount) / max(rec_a.amount, rec_b.amount)
                if spread <= SAME_FUNDER_AMOUNT_TOLERANCE:
                    confidence += SAME_FUNDER_AMOUNT_BONUS
            if (
                rec_a.ts_ms
                and rec_b.ts_ms
                and abs(rec_a.ts_ms - rec_b.ts_ms) <= SAME_FUNDER_TIME_WINDOW_MS
            ):
                confidence += SAME_FUNDER_TIME_BONUS
            evidence = [f"funder:{ancestor}", f"hops:{hop_a}/{hop_b}"]
            evidence += [s for s in (rec_a.tx, rec_b.tx) if s]
            stamps = [t for t in (rec_a.ts_ms, rec_b.ts_ms) if t]
            edges.append(
                _edge(
                    chain,
                    wa,
                    wb,
                    EdgeType.SAME_FUNDER,
                    confidence,
                    evidence=evidence,
                    first_ms=min(stamps) if stamps else None,
                    last_ms=max(stamps) if stamps else None,
                )
            )
    return edges


def _cooccurrence_edges(
    chain: Chain,
    groups: dict[str, list[tuple[str, str, int | None]]],
    edge_type: EdgeType,
    min_tokens: int,
    max_group: int,
    hubs: HubFilter,
) -> list[ClusterEdge]:
    """Shared body of the co-buy heuristics: pair up group members, count distinct tokens.

    ``groups`` maps a group key to ``(wallet, token, ts_ms)`` rows. A pair only becomes an
    edge once it has repeated across ``min_tokens`` distinct tokens, because on a single
    hyped launch everybody buys in the same slot.
    """
    acc: dict[tuple[str, str], _PairAcc] = defaultdict(_PairAcc)
    for key, rows in groups.items():
        kept = hubs.keep(w for w, _t, _ts in rows)
        if len(kept) < 2 or len(kept) > max_group:
            continue
        token = rows[0][1]
        stamps = [ts for _w, _t, ts in rows if ts]
        ts = min(stamps) if stamps else None
        for a, b in _pairs(kept):
            acc[(a, b)].observe(token, key, ts)

    edges: list[ClusterEdge] = []
    for (a, b), state in acc.items():
        n = len(state.tokens)
        if n < min_tokens:
            continue
        edges.append(
            _edge(
                chain,
                a,
                b,
                edge_type,
                _cooccur_confidence(n, min_tokens),
                evidence=state.evidence,
                observations=n,
                first_ms=state.first_ms,
                last_ms=state.last_ms,
            )
        )
    return edges


def derive_same_slot_buy(
    conn: sqlite3.Connection | None = None,
    chain: Chain = Chain.SOL,
    min_tokens: int = COOCCUR_MIN_TOKENS,
) -> list[ClusterEdge]:
    """Wallets buying the same token in the same slot, repeatedly across tokens.

    One shared slot is a coincidence on a hyped launch; three shared tokens is a ring
    (Kamat found 1,012 of them this way). Only rows with a slot are considered — an EVM
    block index goes in ``slot`` for this to work on EVM chains.
    """
    c = _conn(conn)
    hubs = HubFilter(chain, c)
    rows = fetch_all(
        c,
        "SELECT slot, token, wallet, ts_ms FROM swaps "
        "WHERE chain=? AND side='buy' AND slot IS NOT NULL",
        (chain.value,),
    )
    groups: dict[str, list[tuple[str, str, int | None]]] = defaultdict(list)
    for row in rows:
        key = f"slot:{int(row['slot'])}:{row['token']}"
        groups[key].append((str(row["wallet"]), str(row["token"]), int(row["ts_ms"] or 0) or None))
    return _cooccurrence_edges(
        chain, groups, EdgeType.SAME_SLOT_BUY, min_tokens, SAME_SLOT_MAX_GROUP, hubs
    )


def derive_first_n_cooccur(
    conn: sqlite3.Connection | None = None,
    chain: Chain = Chain.SOL,
    n: int = FIRST_N_DEFAULT,
    min_tokens: int = COOCCUR_MIN_TOKENS,
) -> list[ClusterEdge]:
    """Wallets that keep showing up together in the first ``n`` buyers of a token.

    Same rule as :func:`derive_same_slot_buy` but on the ``first_buyers`` table, which is
    populated from provider data and so covers tokens we never saw a swap stream for.
    """
    c = _conn(conn)
    hubs = HubFilter(chain, c)
    rows = fetch_all(
        c,
        "SELECT token, wallet, rank, ts_ms FROM first_buyers WHERE chain=? AND rank<=?",
        (chain.value, n),
    )
    groups: dict[str, list[tuple[str, str, int | None]]] = defaultdict(list)
    for row in rows:
        groups[f"first{n}:{row['token']}"].append(
            (str(row["wallet"]), str(row["token"]), int(row["ts_ms"] or 0) or None)
        )
    return _cooccurrence_edges(
        chain, groups, EdgeType.FIRST_N_COOCCUR, min_tokens, FIRST_N_MAX_GROUP, hubs
    )


def derive_lead_lag(
    conn: sqlite3.Connection | None = None,
    chain: Chain = Chain.SOL,
    max_delay_s: int = LEAD_LAG_MAX_DELAY_S,
    min_tokens: int = LEAD_LAG_MIN_TOKENS,
) -> list[ClusterEdge]:
    """B buys within ``max_delay_s`` after A, on at least ``min_tokens`` distinct tokens.

    This finds side wallets and copy bots. It is emitted as a **directed hint**: the
    evidence list carries ``lead:<leader>`` and the entity builder refuses to merge on it,
    because a copier is a different person with the same information — merging them would
    silently destroy the independence count the confluence lane depends on.
    """
    c = _conn(conn)
    hubs = HubFilter(chain, c)
    rows = fetch_all(
        c,
        "SELECT token, wallet, MIN(ts_ms) AS ts_ms FROM swaps "
        "WHERE chain=? AND side='buy' AND ts_ms IS NOT NULL GROUP BY token, wallet",
        (chain.value,),
    )
    by_token: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for row in rows:
        by_token[str(row["token"])].append((int(row["ts_ms"]), str(row["wallet"])))

    window_ms = max_delay_s * 1000
    follows: dict[tuple[str, str], _PairAcc] = defaultdict(_PairAcc)
    for token, entries in by_token.items():
        kept = set(hubs.keep(w for _ts, w in entries))
        ordered = sorted((ts, w) for ts, w in entries if w in kept)
        if len(ordered) < 2 or len(ordered) > LEAD_LAG_MAX_GROUP:
            continue
        for i, (ts_a, leader) in enumerate(ordered):
            for ts_b, follower in ordered[i + 1 :]:
                delta = ts_b - ts_a
                if delta <= 0:
                    continue
                if delta > window_ms:
                    break
                follows[(leader, follower)].observe(token, f"{token}@+{delta // 1000}s", ts_a)

    edges: list[ClusterEdge] = []
    for (leader, follower), state in follows.items():
        forward = len(state.tokens)
        reverse = len(follows.get((follower, leader), _PairAcc()).tokens)
        if forward < min_tokens or forward <= reverse:
            continue  # not a consistent direction: two wallets reacting to the same feed
        evidence = [f"lead:{leader}", *state.evidence][:MAX_EVIDENCE]
        edges.append(
            _edge(
                chain,
                leader,
                follower,
                EdgeType.LEAD_LAG,
                LEAD_LAG_CONFIDENCE,
                evidence=evidence,
                observations=forward,
                first_ms=state.first_ms,
                last_ms=state.last_ms,
            )
        )
    return edges


def derive_shared_cex_deposit(
    conn: sqlite3.Connection | None = None,
    chain: Chain = Chain.SOL,
    cap: int = SHARED_CEX_DEPOSIT_CAP,
) -> list[ClusterEdge]:
    """Wallets funding the same exchange deposit address.

    A deposit address belongs to one exchange account, so everyone paying into it is that
    account holder (Victor 2020: 17.9% of EOAs resolved this way). A deposit address is
    recognised by what it does — forwards to a labelled CEX hub — not by a label of its own.
    Groups above ``cap`` are omnibus or shared-custody addresses and are dropped.
    """
    c = _conn(conn)
    hubs = HubFilter(chain, c)
    cex = hub_kind_set(c, chain, "cex")
    if not cex:
        return []
    rows = fetch_all(
        c, "SELECT src, dst, tx, ts_ms FROM transfers WHERE chain=? AND src<>dst", (chain.value,)
    )
    forwards_to_cex: set[str] = set()
    inbound: dict[str, list[tuple[str, str | None, int | None]]] = defaultdict(list)
    for row in rows:
        src = safe_normalize(str(row["src"]), chain)
        dst = safe_normalize(str(row["dst"]), chain)
        if dst in cex:
            forwards_to_cex.add(src)
        inbound[dst].append((src, row["tx"], int(row["ts_ms"] or 0) or None))

    edges: list[ClusterEdge] = []
    for deposit in sorted(forwards_to_cex):
        if deposit in cex or deposit in hubs.hubs:
            continue  # the exchange's own hot wallet, not a per-user deposit address
        senders = inbound.get(deposit, [])
        kept = hubs.keep(s for s, _tx, _ts in senders)
        if len(kept) < 2 or len(kept) > cap:
            continue
        acc = _PairAcc()
        for _src, sig, ts in senders:
            acc.observe(None, sig, ts)
        evidence = [f"deposit:{deposit}", *acc.evidence][:MAX_EVIDENCE]
        for a, b in _pairs(kept):
            edges.append(
                _edge(
                    chain,
                    a,
                    b,
                    EdgeType.SHARED_CEX_DEPOSIT,
                    SHARED_CEX_DEPOSIT_CONFIDENCE,
                    evidence=evidence,
                    first_ms=acc.first_ms,
                    last_ms=acc.last_ms,
                )
            )
    return edges


def hub_kind_set(conn: sqlite3.Connection, chain: Chain, kind: str) -> set[str]:
    rows = fetch_all(
        conn, "SELECT address FROM hub_addresses WHERE chain=? AND kind=?", (chain.value, kind)
    )
    return {r["address"] for r in rows}


# -------------------------------------------------------------------------- persistence


def persist_edges(edges: Iterable[ClusterEdge], conn: sqlite3.Connection | None = None) -> int:
    """Upsert edges, accumulating observations and keeping the strongest claim.

    The pair is always stored in sorted order, so ``(a,b)`` and ``(b,a)`` are the same row.
    Confidence takes the max — a later run that saw weaker evidence does not erase what an
    earlier run proved — observations accumulate, and evidence lists merge up to
    ``MAX_EVIDENCE`` so a human can check the claim without the table becoming a log.

    Joins an outer transaction if the caller already opened one, so a nightly job can wrap
    the whole rebuild in a single write.
    """
    c = _conn(conn)
    folded: dict[tuple[str, str, str, str], ClusterEdge] = {}
    for edge in edges:
        a, b = edge.sorted_pair()
        key = (edge.chain.value, a, b, edge.edge_type.value)
        prior = folded.get(key)
        if prior is None:
            folded[key] = edge.model_copy(update={"a": a, "b": b})
            continue
        merged_evidence = list(dict.fromkeys([*prior.evidence, *edge.evidence]))[:MAX_EVIDENCE]
        folded[key] = prior.model_copy(
            update={
                "confidence": max(prior.confidence, edge.confidence),
                "observations": prior.observations + edge.observations,
                "first_seen_ms": min(prior.first_seen_ms, edge.first_seen_ms),
                "last_seen_ms": max(prior.last_seen_ms, edge.last_seen_ms),
                "evidence": merged_evidence,
            }
        )
    if not folded:
        return 0

    with _write(c):
        for (chain_value, a, b, edge_type), edge in folded.items():
            row = c.execute(
                "SELECT confidence, observations, first_seen_ms, last_seen_ms, evidence_json "
                "FROM cluster_edges WHERE chain=? AND a=? AND b=? AND edge_type=?",
                (chain_value, a, b, edge_type),
            ).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO cluster_edges "
                    "(chain, a, b, edge_type, confidence, observations, first_seen_ms, "
                    " last_seen_ms, evidence_json) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        chain_value,
                        a,
                        b,
                        edge_type,
                        edge.confidence,
                        edge.observations,
                        edge.first_seen_ms,
                        edge.last_seen_ms,
                        jdump(edge.evidence[:MAX_EVIDENCE]),
                    ),
                )
                continue
            existing = jload(row["evidence_json"], [])
            if not isinstance(existing, list):
                existing = []
            merged = list(dict.fromkeys([*existing, *edge.evidence]))[:MAX_EVIDENCE]
            c.execute(
                "UPDATE cluster_edges SET confidence=?, observations=?, first_seen_ms=?, "
                "last_seen_ms=?, evidence_json=? WHERE chain=? AND a=? AND b=? AND edge_type=?",
                (
                    max(float(row["confidence"]), edge.confidence),
                    int(row["observations"]) + edge.observations,
                    min(int(row["first_seen_ms"]), edge.first_seen_ms),
                    max(int(row["last_seen_ms"]), edge.last_seen_ms),
                    jdump(merged),
                    chain_value,
                    a,
                    b,
                    edge_type,
                ),
            )
    return len(folded)


def load_edges(
    conn: sqlite3.Connection | None = None,
    chain: Chain = Chain.SOL,
    edge_types: Iterable[EdgeType] | None = None,
) -> list[ClusterEdge]:
    """Read edges back out. The entity builder's only view of the graph."""
    sql = "SELECT * FROM cluster_edges WHERE chain=?"
    params: list[object] = [chain.value]
    if edge_types is not None:
        types = [t.value for t in edge_types]
        if not types:
            return []
        sql += f" AND edge_type IN ({','.join('?' for _ in types)})"
        params.extend(types)
    out: list[ClusterEdge] = []
    for row in fetch_all(_conn(conn), sql, params):
        evidence = jload(row["evidence_json"], [])
        out.append(
            ClusterEdge(
                chain=Chain(row["chain"]),
                a=row["a"],
                b=row["b"],
                edge_type=EdgeType(row["edge_type"]),
                confidence=float(row["confidence"]),
                observations=int(row["observations"]),
                first_seen_ms=int(row["first_seen_ms"]),
                last_seen_ms=int(row["last_seen_ms"]),
                evidence=[str(e) for e in evidence] if isinstance(evidence, list) else [],
            )
        )
    return out


def derive_all(conn: sqlite3.Connection | None = None, chain: Chain = Chain.SOL) -> dict[str, int]:
    """Run every heuristic, persist what it found, return the count per edge type.

    Cheap enough to run nightly over a chain's history. Hard links run first so that a later
    soft heuristic failing loudly still leaves the conservative core in place.

    Seeds the hub table when it is empty for this chain: clustering with no hubs known is
    not a degraded result, it is a wrong one, so it must not be reachable by forgetting a
    setup step.
    """
    c = _conn(conn)
    if not hub_set(chain, c):
        seeded = seed_hubs(c)
        log.info("hub table was empty for %s; seeded %d builtin hubs", chain.value, seeded)
    passes: list[tuple[EdgeType, list[ClusterEdge]]] = [
        (EdgeType.CO_SIGNED, derive_co_signed(c, chain)),
        (EdgeType.SAME_BUNDLE, derive_same_bundle(c, chain)),
        (EdgeType.SHARED_ALT_AUTHORITY, derive_shared_alt_authority(c, chain)),
        (EdgeType.DIRECT_TRANSFER, derive_direct_transfer(c, chain)),
        (EdgeType.SAME_FUNDER, derive_same_funder(c, chain)),
        (EdgeType.SAME_SLOT_BUY, derive_same_slot_buy(c, chain)),
        (EdgeType.FIRST_N_COOCCUR, derive_first_n_cooccur(c, chain)),
        (EdgeType.LEAD_LAG, derive_lead_lag(c, chain)),
        (EdgeType.SHARED_CEX_DEPOSIT, derive_shared_cex_deposit(c, chain)),
    ]
    counts: dict[str, int] = {}
    for edge_type, edges in passes:
        counts[edge_type.value] = len(edges)
        if edges:
            persist_edges(edges, c)
    log.info("derived edges chain=%s %s", chain.value, counts)
    return counts


__all__ = [
    "DEFAULT_DEGREE_CAP",
    "derive_all",
    "derive_co_signed",
    "derive_direct_transfer",
    "derive_first_n_cooccur",
    "derive_lead_lag",
    "derive_same_bundle",
    "derive_same_funder",
    "derive_same_slot_buy",
    "derive_shared_alt_authority",
    "derive_shared_cex_deposit",
    "load_edges",
    "persist_edges",
    "record_swap_meta",
    "swap_meta",
]
