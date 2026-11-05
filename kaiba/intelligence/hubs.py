"""Hub pruning — the step that has to happen before any clustering.

Every linkage heuristic in :mod:`kaiba.intelligence.cluster` is transitive: if A links to X
and X links to B then A and B end up in one entity. That is only sound when X is a private
wallet. Route a single CEX hot wallet, DEX router, disperser or bridge into the graph and
one component swallows the chain — the failure mode the research doc calls "hub collapse"
(``docs/research/03-wallet-clustering.md`` heuristic 16).

So there are two defences, and both run before an edge is ever considered:

1. **Labels.** Known service addresses live in ``hub_addresses`` and are never clustered.
   :func:`seed_hubs` writes the ones we can hardcode; a label provider adds the rest.
2. **Degree.** Anything that has transacted with more than ``cap`` distinct counterparties
   behaves like a service whether or not we have a label for it, so it is dropped too.
   That is the catch-all for the unlabelled disperser and the launch-service treasury.

Dropping a genuine user wallet costs us one edge. Keeping a hub costs us the whole graph,
so the bias here is deliberately aggressive.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable

from kaiba.core.db import fetch_all, get_conn, upsert
from kaiba.core.schemas import EVM_CHAINS, Chain, normalize_address

log = logging.getLogger(__name__)

#: Counterparty count above which an address is treated as a service, labelled or not.
#: 200 sits well above a bundler fanning out to ~20 sub-wallets (research heuristic 6) and
#: well below a CEX deposit sweeper.
DEFAULT_DEGREE_CAP = 200

#: Values allowed in ``hub_addresses.kind``.
HUB_KINDS: frozenset[str] = frozenset({"cex", "router", "pool", "bridge", "disperser", "program"})

#: The eight Jito tip accounts. A transfer to one of these in the last transaction of a slot
#: is how a bundle is reconstructed (research heuristic 7), so `cluster` needs them as data
#: and not only as hub rows.
JITO_TIP_ACCOUNTS: tuple[str, ...] = (
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
)

# (address, kind, label)
_SOLANA_HUBS: tuple[tuple[str, str, str], ...] = (
    # programs
    ("11111111111111111111111111111111", "program", "System Program"),
    ("ComputeBudget111111111111111111111111111111", "program", "ComputeBudget"),
    ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "program", "SPL Token"),
    ("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", "program", "Token-2022"),
    ("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL", "program", "Associated Token Account"),
    ("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr", "program", "Memo v2"),
    ("1nc1nerator11111111111111111111111111111111", "program", "Incinerator (burn)"),
    ("So11111111111111111111111111111111111111112", "program", "Wrapped SOL mint"),
    # routers / AMM programs
    ("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4", "router", "Jupiter v6"),
    ("JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB", "router", "Jupiter v4"),
    ("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8", "router", "Raydium AMM v4"),
    ("CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C", "router", "Raydium CPMM"),
    ("CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK", "router", "Raydium CLMM"),
    ("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA", "router", "PumpSwap"),
    ("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P", "router", "pump.fun"),
    ("whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc", "router", "Orca Whirlpools"),
    ("LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM489hLGQ9F", "router", "Meteora DLMM"),
    # exchanges — these move and must be refreshed from a label provider, they are only a floor
    ("5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9", "cex", "Binance (SOL hot)"),
    ("2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S", "cex", "Coinbase 1"),
    ("H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS", "cex", "Coinbase 2"),
    ("FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5", "cex", "Kraken"),
    ("AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2", "cex", "Bybit"),
)

# EVM hubs are seeded onto every EVM chain: router deployments differ per chain, but an
# address that does not exist on a chain simply never appears in its data, while Permit2,
# the burn addresses and Binance's hot wallets genuinely are reused across chains.
_EVM_HUBS: tuple[tuple[str, str, str], ...] = (
    ("0x0000000000000000000000000000000000000000", "program", "Zero address"),
    ("0x000000000000000000000000000000000000dead", "program", "Burn (dead)"),
    ("0x000000000022d473030f116ddee9f6b43ac78ba3", "router", "Permit2"),
    ("0x7a250d5630b4cf539739df2c5dacb4c659f2488d", "router", "Uniswap V2 Router 02"),
    ("0xe592427a0aece92de3edee1f18e0157c05861564", "router", "Uniswap V3 SwapRouter"),
    ("0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45", "router", "Uniswap SwapRouter02"),
    ("0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad", "router", "Uniswap Universal Router"),
    ("0x66a9893cc07d91d95644aedd05d03f95e1dba8af", "router", "Uniswap Universal Router v2"),
    ("0x10ed43c718714eb63d5aa57b78b54704e256024e", "router", "PancakeSwap V2 Router"),
    ("0x13f4ea83d0bd40e75c8222255bc855a974568dd4", "router", "PancakeSwap Smart Router V3"),
    ("0x1111111254eeb25477b68fb85ed929f73a960582", "router", "1inch v5"),
    ("0xdef1c0ded9bec7f1a1670819833240f027b25eff", "router", "0x Exchange Proxy"),
    ("0xd152f549545093347a162dce210e7293f1452150", "disperser", "Disperse.app"),
    ("0x28c6c06298d514db089934071355e5743bf21d60", "cex", "Binance 14"),
    ("0x21a31ee1afc51d94c2efccaa2092ad1028285549", "cex", "Binance 15"),
    ("0xdfd5293d8e347dfe59e90efd55b2956a1343963d", "cex", "Binance 16"),
    ("0x71660c4005ba85c37ccec55d0c4493e66fe775d3", "cex", "Coinbase 1"),
    ("0x503828976d22510aad0201ac7ec88293211d23da", "cex", "Coinbase 2"),
    ("0xddfabcdc4d8ffc6d5beaf154f18b778f892a0740", "cex", "Coinbase 3"),
    ("0x2910543af39aba0cd09dbb2d50200b3e800a63d2", "cex", "Kraken"),
)


def safe_normalize(address: str, chain: Chain) -> str:
    """:func:`normalize_address` that tolerates addresses we did not get from a chain.

    Clustering runs over whatever the ingest layer stored. A malformed address must not
    take the nightly job down, so the shape check degrades to case folding for EVM.
    """
    a = (address or "").strip()
    try:
        return normalize_address(a, chain)
    except ValueError:
        return a.lower() if chain in EVM_CHAINS else a


def _conn(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    return conn if conn is not None else get_conn()


def builtin_hubs(chain: Chain) -> tuple[tuple[str, str, str], ...]:
    """The hardcoded hub list for one chain, as ``(address, kind, label)``."""
    if chain is Chain.SOL:
        return _SOLANA_HUBS + tuple(
            (a, "program", f"Jito tip account {i + 1}") for i, a in enumerate(JITO_TIP_ACCOUNTS)
        )
    if chain in EVM_CHAINS:
        return _EVM_HUBS
    return ()


def seed_hubs(conn: sqlite3.Connection | None = None, source: str = "builtin") -> int:
    """Write the hardcoded hub set for every chain. Idempotent; returns rows written."""
    c = _conn(conn)
    written = 0
    for chain in Chain:
        for address, kind, label in builtin_hubs(chain):
            add_hub(chain, address, kind, label=label, source=source, conn=c)
            written += 1
    return written


def add_hub(
    chain: Chain,
    address: str,
    kind: str,
    label: str | None = None,
    source: str = "manual",
    conn: sqlite3.Connection | None = None,
) -> None:
    """Record one address as never-clusterable."""
    if kind not in HUB_KINDS:
        raise ValueError(f"unknown hub kind {kind!r}; expected one of {sorted(HUB_KINDS)}")
    upsert(
        _conn(conn),
        "hub_addresses",
        {
            "chain": chain.value,
            "address": safe_normalize(address, chain),
            "kind": kind,
            "label": label,
            "source": source,
        },
        conflict=["chain", "address"],
        update=["kind", "label", "source"],
    )


def is_hub(chain: Chain, address: str, conn: sqlite3.Connection | None = None) -> bool:
    row = _conn(conn).execute(
        "SELECT 1 FROM hub_addresses WHERE chain=? AND address=?",
        (chain.value, safe_normalize(address, chain)),
    ).fetchone()
    return row is not None


def hub_set(chain: Chain, conn: sqlite3.Connection | None = None, kind: str | None = None) -> set[str]:
    """Every hub address on a chain, optionally of one kind. One query, used per run."""
    sql = "SELECT address FROM hub_addresses WHERE chain=?"
    params: list[object] = [chain.value]
    if kind is not None:
        sql += " AND kind=?"
        params.append(kind)
    return {r["address"] for r in fetch_all(_conn(conn), sql, params)}


def counterparties(chain: Chain, address: str, conn: sqlite3.Connection | None = None) -> int:
    """Distinct addresses this one has sent to or received from in ``transfers``."""
    a = safe_normalize(address, chain)
    row = _conn(conn).execute(
        "SELECT COUNT(*) AS n FROM ("
        "  SELECT DISTINCT dst AS other FROM transfers WHERE chain=? AND src=?"
        "  UNION"
        "  SELECT DISTINCT src AS other FROM transfers WHERE chain=? AND dst=?"
        ")",
        (chain.value, a, chain.value, a),
    ).fetchone()
    return int(row["n"]) if row else 0


def degree_cap_exceeded(
    chain: Chain,
    address: str,
    conn: sqlite3.Connection | None = None,
    cap: int = DEFAULT_DEGREE_CAP,
) -> bool:
    """True when the address has too many counterparties to be treated as a person."""
    return counterparties(chain, address, conn) > cap


def degrees(chain: Chain, conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """Counterparty count for every address seen in ``transfers`` on this chain."""
    rows = fetch_all(
        _conn(conn),
        "SELECT addr, COUNT(*) AS n FROM ("
        "  SELECT DISTINCT src AS addr, dst AS other FROM transfers WHERE chain=?"
        "  UNION"
        "  SELECT DISTINCT dst AS addr, src AS other FROM transfers WHERE chain=?"
        ") GROUP BY addr",
        (chain.value, chain.value),
    )
    return {r["addr"]: int(r["n"]) for r in rows}


class HubFilter:
    """A snapshot of the hub set and the degree table for one chain.

    The derivation functions test thousands of candidate addresses per run; taking the
    snapshot once and answering from memory keeps that from becoming thousands of queries.
    Build a new one per run — it is deliberately not cached across calls, because the hub
    table is edited while the agent is running.
    """

    def __init__(
        self,
        chain: Chain,
        conn: sqlite3.Connection | None = None,
        cap: int = DEFAULT_DEGREE_CAP,
    ) -> None:
        self.chain = chain
        self.cap = cap
        self._conn = _conn(conn)
        self.hubs = hub_set(chain, self._conn)
        self._degrees: dict[str, int] | None = None

    @property
    def degree_map(self) -> dict[str, int]:
        if self._degrees is None:
            self._degrees = degrees(self.chain, self._conn)
        return self._degrees

    def blocked(self, address: str) -> bool:
        a = safe_normalize(address, self.chain)
        if not a or a in self.hubs:
            return True
        return self.degree_map.get(a, 0) > self.cap

    def keep(self, addresses: Iterable[str]) -> list[str]:
        """Pruned addresses, order preserved, duplicates collapsed."""
        out: list[str] = []
        seen: set[str] = set()
        for raw in addresses:
            a = safe_normalize(raw, self.chain)
            if not a or a in seen or self.blocked(a):
                continue
            seen.add(a)
            out.append(a)
        return out


def prune(
    candidates: Iterable[str],
    chain: Chain,
    conn: sqlite3.Connection | None = None,
    cap: int = DEFAULT_DEGREE_CAP,
) -> list[str]:
    """Drop hubs and over-degree nodes from a candidate wallet list.

    Convenience wrapper around :class:`HubFilter` for one-off calls; a derivation pass that
    prunes many groups should build one filter and reuse it.
    """
    return HubFilter(chain, conn, cap).keep(candidates)
