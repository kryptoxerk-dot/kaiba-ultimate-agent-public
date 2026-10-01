"""Historical wallet backfill: turn Helius history into `swaps`, `first_buyers`, `swap_meta`.

Phase 1 of `docs/PLAN-TO-RUNNING-AGENT.md`. 6,236 wallets were imported and none could be
graded, because `swaps` had never held a row. Everything downstream — PnL reconstruction,
wallet grading, clustering, confluence — reads `swaps`, so this module is the precondition
for all of it.

Five decisions shape the code, and each one was made against a real payload rather than
the documentation.

**Amounts come from `accountData`, never from `tokenTransfers`.** The enhanced payload
reports `tokenTransfers[].tokenAmount` as a *decimalised* string ("14346631.500951").
Turning that back into atoms means multiplying by a power of ten, which is exactly the
float-shaped operation `docs/CONTRACT.md` forbids for money.
`accountData[].tokenBalanceChanges[].rawTokenAmount.tokenAmount` is the same quantity in
base units as an integer string, alongside its `decimals`. That is the only amount source
used here. Native amounts come from `nativeBalanceChange`, also an integer.

**The wallet's own net balance delta defines the trade, not the route.** A swap can touch
a dozen accounts through three programs; what we can state without modelling any venue is
how much of the mint the wallet gained or lost and how much SOL it gave up or received.
The transaction fee is added back when the wallet paid it, so the figure is the swap's
economics rather than the swap plus network overhead. This is net of platform and creator
fees, which is the honest number: it is what the wallet actually paid or received.

**A transaction we cannot classify is skipped and counted.** Requiring exactly one
non-quote mint with a non-zero delta, and a quote leg moving the opposite way, rejects
multi-hop routes, LP operations and transactions where the wallet is merely referenced.
That last case is common and is not an error: address history includes every transaction
that *mentions* the address, and in one 25-transaction sample 4 of 10 `SWAP`-typed
transactions moved nothing for our wallet at all. :func:`parse_page` separates those
mechanical exclusions from genuine classification failures so the reported skip rate means
something.

**USD is left `None`.** Helius history carries no price. Stamping a swap from six weeks ago
with today's SOL price would be a fabricated number in a money column, and every wallet
grade downstream would inherit it. Episodes reconstruct ROI, win rate and big-win rate from
native amounts (`kaiba.intelligence.pnl`), so a wallet still reaches a real grade without
it; only `realized_profit` stays unmeasured. USD is filled in only when the counter-leg is
a stablecoin, where the amount genuinely is dollars.

**Signer and lookup-table metadata needs the other route.** The Enhanced Transactions API
does not expose the signer list or address table lookups — the premise that `instructions`
and `accountData` carry them does not survive contact with a live payload. Both are in the
raw `getTransactionsForAddress` response (`message.header.numRequiredSignatures` and
`message.addressTableLookups`), which is also ten times cheaper per transaction. The two
routes return different transaction sets for the same address, so they are walked as two
independent passes with their own cursors and joined in SQLite on the signature. What the
raw pass cannot give is the *authority* of a lookup table — the payload has the table
address only — so `alt_authority` is recorded as `None` and `derive_shared_alt_authority`
keeps returning nothing. That is a real gap, stated rather than papered over.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump, jload
from kaiba.core.events import emit
from kaiba.core.schemas import Chain, EventKind, Receipt, normalize_address, now_ms
from kaiba.intelligence.hubs import JITO_TIP_ACCOUNTS
from kaiba.providers import helius

log = logging.getLogger(__name__)

#: Value of `swaps.source` / `first_buyers.source` for everything written here.
SOURCE = "helius:backfill"

#: Wrapped SOL is a quote currency, not a position. A wrap/unwrap inside a swap nets to
#: zero on the token account and shows up in `nativeBalanceChange` instead, but a wallet
#: that keeps a standing wSOL account shows the movement there, so both are summed.
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
STABLE_MINTS: frozenset[str] = frozenset({USDC_MINT, USDT_MINT})
QUOTE_MINTS: frozenset[str] = frozenset({WSOL_MINT}) | STABLE_MINTS

#: Below this the native leg is rent, a tip or rounding, not consideration for a trade.
#: 10,000 lamports is 0.00001 SOL — two orders of magnitude under the smallest real buy
#: seen in the sample, and above the 5,000-lamport base signature fee.
MIN_QUOTE_LAMPORTS = 10_000

#: Programs that appear in almost every transaction and never identify the venue.
INFRASTRUCTURE_PROGRAMS: frozenset[str] = frozenset(
    {
        "11111111111111111111111111111111",
        "ComputeBudget111111111111111111111111111111",
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
        "AddressLookupTab1e1111111111111111111111111",
    }
)

_TIP_ACCOUNTS: frozenset[str] = frozenset(JITO_TIP_ACCOUNTS)

#: Skips that mean "this was never ours to write", as opposed to "we could not tell".
#: Only the second group belongs in an honest classification-failure rate.
MECHANICAL_SKIPS: frozenset[str] = frozenset(
    {"not_swap_type", "failed_transaction", "wallet_not_a_party", "no_signature"}
)

DEFAULT_PAGE_LIMIT = 100
DEFAULT_PAGES_PER_WALLET = 1

#: A single run may not spend more than this. The free allowance is 1,000,000 credits per
#: calendar month and an enhanced page costs an assumed 100, so an unbounded run over
#: 6,236 wallets would take a large bite out of the month in one go. Burning the month in
#: one command is a failure even when every row written is correct.
DEFAULT_MAX_CREDITS = 25_000

#: Beyond this many recorded buyers a token's tail tells us nothing about who was early.
FIRST_BUYERS_PER_TOKEN = 200


# --------------------------------------------------------------------------------------
# small conversions — integers only, never float()
# --------------------------------------------------------------------------------------


def _as_int(value: Any) -> int | None:
    """Integer or ``None``. Accepts the int, str and Decimal shapes provider JSON yields."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _safe_normalize(address: str, chain: Chain) -> str:
    try:
        return normalize_address(address, chain)
    except ValueError:
        return address.strip()


def _text(value: int | None) -> str | None:
    """Base units are stored as TEXT so SQLite's 2^63 limit cannot silently truncate."""
    return None if value is None else str(value)


# --------------------------------------------------------------------------------------
# parsed shapes
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SwapRow:
    """One row destined for `swaps`, already in the column contract of 001_core.sql."""

    chain: Chain
    tx: str
    slot: int | None
    ts_ms: int
    wallet: str
    token: str
    side: str
    amount_token: int | None
    amount_native: int | None
    price_usd: Decimal | None
    usd_value: Decimal | None
    program: str | None
    fee_payer: str | None
    venue: str | None = None
    block_index: int | None = None

    def as_params(self) -> tuple[Any, ...]:
        return (
            self.chain.value,
            self.tx,
            self.slot,
            self.block_index,
            self.ts_ms,
            self.wallet,
            self.token,
            self.side,
            _text(self.amount_token),
            _text(self.amount_native),
            None if self.price_usd is None else str(self.price_usd),
            None if self.usd_value is None else str(self.usd_value),
            self.program,
            SOURCE,
            0,
            self.fee_payer,
        )


@dataclass(frozen=True)
class TransferRow:
    """A native transfer worth keeping: a Jito tip, or one leg touching the wallet."""

    chain: Chain
    tx: str
    slot: int | None
    ts_ms: int
    src: str
    dst: str
    amount: int


@dataclass
class PageParse:
    """Everything one page of enhanced history yielded."""

    swaps: list[SwapRow] = field(default_factory=list)
    transfers: list[TransferRow] = field(default_factory=list)
    meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    skips: Counter[str] = field(default_factory=Counter)

    @property
    def ambiguous(self) -> int:
        """Transactions that were plausibly ours and still could not be classified."""
        return sum(n for reason, n in self.skips.items() if reason not in MECHANICAL_SKIPS)


@dataclass
class WalletResult:
    """Outcome of backfilling one wallet. Never raises; failures land in ``error``."""

    wallet: str
    chain: Chain
    pages: int = 0
    transactions: int = 0
    swaps_written: int = 0
    swaps_duplicate: int = 0
    transfers_written: int = 0
    meta_written: int = 0
    meta_pages: int = 0
    ambiguous: int = 0
    skips: Counter[str] = field(default_factory=Counter)
    credits_spent: int = 0
    exhausted: bool = False
    stopped: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "wallet": self.wallet,
            "chain": self.chain.value,
            "pages": self.pages,
            "transactions": self.transactions,
            "swaps_written": self.swaps_written,
            "swaps_duplicate": self.swaps_duplicate,
            "transfers_written": self.transfers_written,
            "meta_written": self.meta_written,
            "meta_pages": self.meta_pages,
            "ambiguous": self.ambiguous,
            "skips": dict(self.skips),
            "credits_spent": self.credits_spent,
            "exhausted": self.exhausted,
            "stopped": self.stopped,
            "error": self.error,
        }


@dataclass
class BackfillReport:
    """Aggregate across a run, including the numbers that say how much to trust it."""

    chain: Chain
    wallets: int = 0
    wallets_failed: int = 0
    pages: int = 0
    transactions: int = 0
    swaps_written: int = 0
    swaps_duplicate: int = 0
    transfers_written: int = 0
    meta_written: int = 0
    first_buyers_written: int = 0
    ambiguous: int = 0
    skips: Counter[str] = field(default_factory=Counter)
    credits_spent: int = 0
    stopped: str | None = None
    results: list[WalletResult] = field(default_factory=list)

    @property
    def classification_skip_rate(self) -> float:
        """Ambiguous over (ambiguous + written): how often we gave up on a real candidate."""
        base = self.ambiguous + self.swaps_written + self.swaps_duplicate
        return round(self.ambiguous / base, 4) if base else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain": self.chain.value,
            "wallets": self.wallets,
            "wallets_failed": self.wallets_failed,
            "pages": self.pages,
            "transactions": self.transactions,
            "swaps_written": self.swaps_written,
            "swaps_duplicate": self.swaps_duplicate,
            "transfers_written": self.transfers_written,
            "meta_written": self.meta_written,
            "first_buyers_written": self.first_buyers_written,
            "ambiguous": self.ambiguous,
            "classification_skip_rate": self.classification_skip_rate,
            "skips": dict(self.skips),
            "credits_spent": self.credits_spent,
            "stopped": self.stopped,
        }


# --------------------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------------------


def _wallet_deltas(
    tx: Mapping[str, Any], wallet: str
) -> tuple[int | None, dict[str, int], dict[str, int]]:
    """``(native_delta, token_atoms_by_mint, decimals_by_mint)`` for one wallet.

    ``native_delta`` is ``None`` when the wallet does not appear in ``accountData`` at all,
    which is different from appearing with a delta of zero.
    """
    native: int | None = None
    atoms: dict[str, int] = defaultdict(int)
    decimals: dict[str, int] = {}
    for entry in tx.get("accountData") or []:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("account") == wallet:
            native = (native or 0) + (_as_int(entry.get("nativeBalanceChange")) or 0)
        for change in entry.get("tokenBalanceChanges") or []:
            if not isinstance(change, Mapping) or change.get("userAccount") != wallet:
                continue
            mint = str(change.get("mint") or "")
            raw = change.get("rawTokenAmount") or {}
            amount = _as_int(raw.get("tokenAmount") if isinstance(raw, Mapping) else None)
            if not mint or amount is None:
                continue
            atoms[mint] += amount
            dec = _as_int(raw.get("decimals")) if isinstance(raw, Mapping) else None
            if dec is not None:
                decimals[mint] = dec
    return native, dict(atoms), decimals


def _venue_program(tx: Mapping[str, Any]) -> str | None:
    """The first top-level program that is not shared infrastructure.

    Deliberately not a hard-coded venue table: `docs/PLAN-TO-RUNNING-AGENT.md` §3.5 records
    that 39% of Solana DEX volume sits in prop AMMs that redeploy, so a fixed list of
    program ids goes stale. Whatever signed for the route is recorded as-is.
    """
    for instruction in tx.get("instructions") or []:
        if not isinstance(instruction, Mapping):
            continue
        program = instruction.get("programId")
        if program and str(program) not in INFRASTRUCTURE_PROGRAMS:
            return str(program)
    return None


def classify_swap(
    tx: Mapping[str, Any], wallet: str, chain: Chain = Chain.SOL
) -> tuple[SwapRow | None, str | None]:
    """Turn one enhanced transaction into a swap row, or say why it was skipped.

    Returns ``(row, None)`` or ``(None, reason)``. Never raises and never guesses: a
    transaction whose legs do not resolve to exactly one position change against exactly
    one quote leg is refused, because a wrong row silently corrupts every wallet grade that
    reads it afterwards.
    """
    signature = str(tx.get("signature") or "")
    if not signature:
        return None, "no_signature"
    if tx.get("transactionError"):
        return None, "failed_transaction"
    if str(tx.get("type") or "").upper() != "SWAP":
        return None, "not_swap_type"

    native, atoms, decimals = _wallet_deltas(tx, wallet)
    if native is None and not atoms:
        return None, "wallet_not_a_party"

    fee = _as_int(tx.get("fee")) or 0
    quote_native = native or 0
    if tx.get("feePayer") == wallet:
        # Undo the network fee so the figure is the trade, not the trade plus overhead.
        quote_native += fee
    quote_native += atoms.pop(WSOL_MINT, 0)

    base = {mint: amount for mint, amount in atoms.items() if amount and mint not in QUOTE_MINTS}
    stables = {mint: amount for mint, amount in atoms.items() if amount and mint in STABLE_MINTS}
    if not base:
        return None, "wallet_not_a_party" if not stables else "no_position_leg"
    if len(base) > 1:
        return None, "multi_leg_route"

    token, qty = next(iter(base.items()))
    side = "buy" if qty > 0 else "sell"
    amount_token = abs(qty)
    price_usd: Decimal | None = None
    usd_value: Decimal | None = None
    amount_native: int | None = None

    if abs(quote_native) >= MIN_QUOTE_LAMPORTS and (quote_native > 0) != (qty > 0):
        amount_native = abs(quote_native)
    elif len(stables) == 1:
        stable_mint, stable_qty = next(iter(stables.items()))
        if (stable_qty > 0) == (qty > 0) or abs(quote_native) >= MIN_QUOTE_LAMPORTS:
            return None, "quote_leg_mismatch"
        scale = Decimal(10) ** decimals.get(stable_mint, 6)
        usd_value = Decimal(abs(stable_qty)) / scale
        token_units = Decimal(amount_token) / (Decimal(10) ** decimals.get(token, 0))
        if token_units > 0:
            price_usd = usd_value / token_units
    else:
        return None, "quote_leg_mismatch"

    timestamp = _as_int(tx.get("timestamp"))
    if timestamp is None:
        return None, "no_timestamp"

    return (
        SwapRow(
            chain=chain,
            tx=signature,
            slot=_as_int(tx.get("slot")),
            ts_ms=timestamp * 1000,
            wallet=wallet,
            token=token,
            side=side,
            amount_token=amount_token,
            amount_native=amount_native,
            price_usd=price_usd,
            usd_value=usd_value,
            program=_venue_program(tx),
            fee_payer=str(tx.get("feePayer")) if tx.get("feePayer") else None,
            venue=str(tx.get("source")) if tx.get("source") else None,
        ),
        None,
    )


def transfer_rows(tx: Mapping[str, Any], wallet: str, chain: Chain) -> list[TransferRow]:
    """The two kinds of native transfer that `transfers` is actually for.

    1. **Jito tips.** `cluster.derive_same_bundle` reconstructs bundles from slots that
       paid a tip and reads those tips out of this table, so without them `same_bundle`
       stays at zero. They arrive free inside a payload we already paid for.
    2. **Transfers between accounts in a `TRANSFER`-typed transaction**, where the wallet
       is one of the two ends. That is funding and withdrawal flow, which is what the table
       comment in 001_core.sql says it holds.

    Explicitly *not* recorded: the lamport routing inside a swap. A pump.fun sell moves
    SOL to four or five fee, rent and creator accounts, and writing those here would make
    every wallet that paid the same platform fee look commonly funded —
    `derive_same_funder` reads `transfers.src` as a funder and would emit thousands of
    edges that mean nothing. That was measured, not assumed: an earlier draft of this
    function produced 9,190 `same_funder` edges from 25 wallets.
    """
    signature = str(tx.get("signature") or "")
    timestamp = _as_int(tx.get("timestamp"))
    slot = _as_int(tx.get("slot"))
    if not signature or timestamp is None or tx.get("transactionError"):
        return []
    is_transfer_tx = str(tx.get("type") or "").upper() == "TRANSFER"
    out: list[TransferRow] = []
    for transfer in tx.get("nativeTransfers") or []:
        if not isinstance(transfer, Mapping):
            continue
        src = str(transfer.get("fromUserAccount") or "")
        dst = str(transfer.get("toUserAccount") or "")
        amount = _as_int(transfer.get("amount"))
        if not src or not dst or not amount:
            continue
        if dst not in _TIP_ACCOUNTS and not (is_transfer_tx and wallet in (src, dst)):
            continue
        out.append(
            TransferRow(
                chain=chain, tx=signature, slot=slot, ts_ms=timestamp * 1000,
                src=src, dst=dst, amount=amount,
            )
        )
    return out


def enhanced_meta(tx: Mapping[str, Any]) -> dict[str, Any]:
    """Per-transaction metadata the enhanced payload genuinely carries.

    The fee payer is the one signer we can prove from this route, so ``signers_complete``
    is false and the clustering code is told as much rather than left to assume the list is
    whole. ``alt_authority`` is ``None`` because the enhanced payload has no lookup-table
    section at all — see :func:`raw_meta` for the route that does.
    """
    fee_payer = tx.get("feePayer")
    programs = sorted(
        {
            str(i.get("programId"))
            for i in tx.get("instructions") or []
            if isinstance(i, Mapping) and i.get("programId")
        }
    )
    return {
        "fee_payer": str(fee_payer) if fee_payer else None,
        "signers": [str(fee_payer)] if fee_payer else [],
        "signers_complete": False,
        "programs": programs,
        "venue": str(tx.get("source")) if tx.get("source") else None,
        "alt_authority": None,
        "source": SOURCE,
    }


def raw_meta(raw: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """``(signature, meta)`` from one `getTransactionsForAddress` row.

    This is the only Helius route that states who signed: the first
    ``header.numRequiredSignatures`` entries of ``message.accountKeys`` are the signers, by
    the transaction format itself rather than by inference. ``addressTableLookups`` gives
    the *table* addresses; their authority would need a further account read, so
    ``alt_authority`` stays ``None`` and ``alt_tables`` records what is actually there.
    """
    transaction = raw.get("transaction")
    if not isinstance(transaction, Mapping):
        return None
    signatures = transaction.get("signatures") or []
    if not isinstance(signatures, Sequence) or not signatures:
        return None
    signature = str(signatures[0])
    message = transaction.get("message")
    if not isinstance(message, Mapping):
        return None
    header = message.get("header") if isinstance(message.get("header"), Mapping) else {}
    required = _as_int(header.get("numRequiredSignatures")) or 1
    keys = [str(k) for k in (message.get("accountKeys") or []) if k]
    signers = keys[: max(1, required)]
    tables = sorted(
        {
            str(lookup.get("accountKey"))
            for lookup in (message.get("addressTableLookups") or [])
            if isinstance(lookup, Mapping) and lookup.get("accountKey")
        }
    )
    meta_block = raw.get("meta") if isinstance(raw.get("meta"), Mapping) else {}
    return signature, {
        "fee_payer": signers[0] if signers else None,
        "signers": signers,
        "signers_complete": True,
        "alt_tables": tables or None,
        "alt_authority": None,
        "slot": _as_int(raw.get("slot")),
        "block_index": _as_int(raw.get("transactionIndex")),
        "failed": bool(meta_block.get("err")),
        "source": SOURCE,
    }


def parse_page(
    payload: Iterable[Mapping[str, Any]], wallet: str, chain: Chain = Chain.SOL
) -> PageParse:
    """Classify one page of enhanced history. Pure: no database, no network."""
    out = PageParse()
    for tx in payload:
        if not isinstance(tx, Mapping):
            out.skips["unreadable_payload"] += 1
            continue
        # Transfers are collected from every transaction, not only the classified ones:
        # a Jito tip and a funding transfer both live in transactions that are not swaps.
        out.transfers.extend(transfer_rows(tx, wallet, chain))
        row, reason = classify_swap(tx, wallet, chain)
        if row is None:
            out.skips[reason or "unknown"] += 1
            continue
        out.swaps.append(row)
        out.meta[row.tx] = enhanced_meta(tx)
    return out


# --------------------------------------------------------------------------------------
# cursors
# --------------------------------------------------------------------------------------


def cursor_key(chain: Chain, wallet: str, kind: str = "swaps") -> str:
    return f"backfill:{kind}:{chain.value}:{wallet}"


def load_cursor(
    conn: sqlite3.Connection, chain: Chain, wallet: str, kind: str = "swaps"
) -> dict[str, Any]:
    row = fetch_one(conn, "SELECT value FROM kv WHERE key = ?", (cursor_key(chain, wallet, kind),))
    cursor = jload(row["value"], {}) if row else {}
    return cursor if isinstance(cursor, dict) else {}


def save_cursor(
    conn: sqlite3.Connection, chain: Chain, wallet: str, cursor: Mapping[str, Any],
    kind: str = "swaps",
) -> None:
    payload = dict(cursor)
    payload["updated_ms"] = now_ms()
    conn.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ms = excluded.updated_ms",
        (cursor_key(chain, wallet, kind), jdump(payload), payload["updated_ms"]),
    )


# --------------------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------------------

_SWAP_INSERT = (
    "INSERT OR IGNORE INTO swaps "
    "(chain, tx, slot, block_index, ts_ms, wallet, token, side, amount_token, amount_native, "
    " price_usd, usd_value, program, source, is_create_tx, fee_payer) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def write_swaps(conn: sqlite3.Connection, rows: Sequence[SwapRow]) -> tuple[int, int]:
    """``(written, duplicate)``. Idempotent on the table's own uniqueness constraint,
    which is (chain, tx, wallet, token, side, amount_token) — a re-run of the same
    signature cannot produce a second row."""
    written = 0
    for row in rows:
        if conn.execute(_SWAP_INSERT, row.as_params()).rowcount:
            written += 1
    return written, len(rows) - written


def write_transfers(conn: sqlite3.Connection, rows: Sequence[TransferRow]) -> int:
    written = 0
    for row in rows:
        cur = conn.execute(
            "INSERT OR IGNORE INTO transfers (chain, tx, slot, ts_ms, src, dst, amount, source) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (row.chain.value, row.tx, row.slot, row.ts_ms, row.src, row.dst, str(row.amount), SOURCE),
        )
        written += cur.rowcount or 0
    return written


def write_meta(
    conn: sqlite3.Connection, chain: Chain, metas: Mapping[str, Mapping[str, Any]]
) -> int:
    """Merge metadata into `swap_meta`, keeping whichever route knew more.

    The raw route's signer list is complete and the enhanced route's is not, so a merge
    that blindly overwrote would let a later enhanced page demote a good signer list back
    to the fee payer alone.
    """
    from kaiba.intelligence.cluster import record_swap_meta, swap_meta

    written = 0
    for signature, meta in metas.items():
        existing = swap_meta(conn, chain, signature)
        merged = dict(existing)
        if existing.get("signers_complete") and not meta.get("signers_complete"):
            incoming = {k: v for k, v in meta.items() if k not in ("signers", "signers_complete")}
            merged.update({k: v for k, v in incoming.items() if v is not None})
        else:
            merged.update({k: v for k, v in meta.items() if v is not None or k not in merged})
        if merged != existing:
            record_swap_meta(conn, chain, signature, merged)
            written += 1
    return written


def _apply_block_index(
    conn: sqlite3.Connection, chain: Chain, metas: Mapping[str, Mapping[str, Any]]
) -> None:
    """Backfill `swaps.block_index` for signatures the raw pass also saw."""
    for signature, meta in metas.items():
        index = meta.get("block_index")
        if index is None:
            continue
        conn.execute(
            "UPDATE swaps SET block_index = ? WHERE chain = ? AND tx = ? AND block_index IS NULL",
            (int(index), chain.value, signature),
        )


# --------------------------------------------------------------------------------------
# budget
# --------------------------------------------------------------------------------------


def _enhanced_cost() -> int:
    cost = helius.CREDIT_COSTS.get(helius.ROUTE_ENHANCED)
    return cost.credits if cost else 100


def _meta_cost(page_limit: int) -> int:
    return helius.transaction_credits(page_limit, "full")


def credits_used(conn: sqlite3.Connection) -> int:
    try:
        return int(helius.budget_status(conn=conn)["used"])
    except (KeyError, TypeError, ValueError):  # pragma: no cover - defensive
        return 0


def _budget_room(conn: sqlite3.Connection, need: int) -> bool:
    try:
        return int(helius.budget_status(conn=conn)["remaining"]) >= need
    except (KeyError, TypeError, ValueError):  # pragma: no cover - defensive
        return False


# --------------------------------------------------------------------------------------
# one wallet
# --------------------------------------------------------------------------------------


def _degrade(receipt: Receipt | None, wallet: str, conn: sqlite3.Connection, route: str) -> None:
    """A provider failure is an event and a stop, never an exception."""
    note = getattr(receipt, "note", None) if receipt is not None else None
    try:
        emit(
            EventKind.PROVIDER_ERROR,
            {"provider": "helius", "route": route, "detail": helius.scrub(note) or "no payload"},
            chain=Chain.SOL,
            subject=wallet,
            level="warn",
            conn=conn,
        )
    except sqlite3.Error as exc:  # pragma: no cover - telemetry must not break ingest
        log.debug("backfill: could not emit provider error for %s: %s", wallet, exc)


def backfill_wallet(
    wallet: str,
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    pages: int = DEFAULT_PAGES_PER_WALLET,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    with_meta: bool = True,
    fresh: bool = False,
    dry_run: bool = False,
) -> WalletResult:
    """Pull, classify and store one wallet's swap history.

    Walks backwards through history by default, resuming from the stored ``oldest_sig`` so
    a second run continues rather than paying for the same page twice. ``fresh=True``
    instead asks only for transactions newer than ``newest_sig``, which is the mode a
    scheduled top-up wants.
    """
    c = conn or get_conn()
    address = _safe_normalize(wallet, chain)
    result = WalletResult(wallet=address, chain=chain)
    if chain is not Chain.SOL:
        result.error = "only solana history is available from helius"
        return result

    cursor = load_cursor(c, chain, address)
    if cursor.get("exhausted") and not fresh:
        result.exhausted = True
        result.stopped = "already_exhausted"
        return result

    before = None if fresh else cursor.get("oldest_sig")
    until = cursor.get("newest_sig") if fresh else None
    started_credits = credits_used(c)
    newest_seen: str | None = None

    for _ in range(max(1, pages)):
        if not _budget_room(c, _enhanced_cost()):
            result.stopped = "budget_exhausted"
            break
        payload, receipt = helius.get_enhanced_transactions(
            address, limit=page_limit, before=before, until=until, conn=c
        )
        if payload is None:
            _degrade(receipt, address, c, helius.ROUTE_ENHANCED)
            result.error = helius.scrub(getattr(receipt, "note", None)) or "helius returned nothing"
            result.stopped = result.stopped or "provider_unavailable"
            break
        result.pages += 1
        result.transactions += len(payload)
        if not payload:
            result.exhausted = True
            break

        parsed = parse_page(payload, address, chain)
        result.skips.update(parsed.skips)
        result.ambiguous += parsed.ambiguous
        if not dry_run:
            written, duplicate = write_swaps(c, parsed.swaps)
            result.swaps_written += written
            result.swaps_duplicate += duplicate
            result.transfers_written += write_transfers(c, parsed.transfers)
            if with_meta:
                result.meta_written += write_meta(c, chain, parsed.meta)
        else:
            result.swaps_written += len(parsed.swaps)

        first_sig = str(payload[0].get("signature") or "") or None
        last_sig = str(payload[-1].get("signature") or "") or None
        if newest_seen is None and first_sig:
            newest_seen = first_sig
        if fresh:
            break  # `until` already bounded the window; one page keeps the top-up cheap
        if not last_sig or last_sig == before:
            result.exhausted = True
            break
        before = last_sig
        if len(payload) < page_limit:
            result.exhausted = True
            break

    if not dry_run:
        cursor["oldest_sig"] = before or cursor.get("oldest_sig")
        if newest_seen and (fresh or not cursor.get("newest_sig")):
            cursor["newest_sig"] = newest_seen
        cursor["pages"] = int(cursor.get("pages") or 0) + result.pages
        cursor["exhausted"] = bool(result.exhausted)
        cursor["swaps"] = int(cursor.get("swaps") or 0) + result.swaps_written
        save_cursor(c, chain, address, cursor)

    if with_meta and not dry_run and result.pages:
        _meta_pass(address, chain, c, result, pages=pages, page_limit=page_limit)

    result.credits_spent = max(0, credits_used(c) - started_credits)
    return result


def _meta_pass(
    address: str,
    chain: Chain,
    conn: sqlite3.Connection,
    result: WalletResult,
    *,
    pages: int,
    page_limit: int,
) -> None:
    """Second, independent walk over raw history for signer and lookup-table facts.

    Kept separate rather than joined page-by-page because the two routes do not return the
    same transactions for the same address: in a 25-row sample they overlapped on 2. They
    are joined on the signature in SQLite instead, so a miss costs coverage and never
    correctness.
    """
    cursor = load_cursor(conn, chain, address, kind="meta")
    if cursor.get("exhausted"):
        return
    token = cursor.get("pagination_token")
    for _ in range(max(1, pages)):
        if not _budget_room(conn, _meta_cost(page_limit)):
            result.stopped = result.stopped or "budget_exhausted"
            break
        payload, receipt = helius.get_transactions_for_address(
            address, limit=page_limit, pagination_token=token, conn=conn
        )
        if payload is None:
            _degrade(receipt, address, conn, helius.ROUTE_TRANSACTIONS)
            break
        rows = payload.get("data") or []
        result.meta_pages += 1
        metas: dict[str, dict[str, Any]] = {}
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            parsed = raw_meta(raw)
            if parsed is not None:
                metas[parsed[0]] = parsed[1]
        result.meta_written += write_meta(conn, chain, metas)
        _apply_block_index(conn, chain, metas)
        token = payload.get("paginationToken")
        if not token or len(rows) < page_limit:
            cursor["exhausted"] = True
            break
    cursor["pagination_token"] = token
    cursor["pages"] = int(cursor.get("pages") or 0) + result.meta_pages
    save_cursor(conn, chain, address, cursor, kind="meta")


# --------------------------------------------------------------------------------------
# many wallets
# --------------------------------------------------------------------------------------


def tracked_wallets(
    conn: sqlite3.Connection,
    chain: Chain = Chain.SOL,
    *,
    limit: int = 25,
    cohorts: Sequence[str] | None = None,
) -> list[str]:
    """Wallets to backfill, least-recently-touched first so a repeated run makes progress.

    The blacklist cohort is excluded: paying credits to grade an address we have already
    decided never to copy is the one clearly wasted call.
    """
    sql = "SELECT address FROM wallets WHERE chain = ? AND COALESCE(cohort,'') != 'blacklist'"
    params: list[Any] = [chain.value]
    if cohorts:
        sql += f" AND cohort IN ({','.join('?' for _ in cohorts)})"
        params.extend(cohorts)
    sql += (
        " ORDER BY COALESCE((SELECT updated_ms FROM kv WHERE kv.key = "
        "  'backfill:swaps:' || wallets.chain || ':' || wallets.address), 0) ASC, address ASC"
        " LIMIT ?"
    )
    params.append(int(limit))
    return [row["address"] for row in fetch_all(conn, sql, params)]


def backfill_wallets(
    conn: sqlite3.Connection | None = None,
    chain: Chain = Chain.SOL,
    *,
    limit: int = 25,
    pages: int = DEFAULT_PAGES_PER_WALLET,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    cohorts: Sequence[str] | None = None,
    wallets: Sequence[str] | None = None,
    with_meta: bool = True,
    fresh: bool = False,
    dry_run: bool = False,
    max_credits: int = DEFAULT_MAX_CREDITS,
    rebuild_buyers: bool = True,
) -> BackfillReport:
    """Backfill a batch, stopping cleanly on the credit ceiling and reporting how far it got."""
    c = conn or get_conn()
    report = BackfillReport(chain=chain)
    targets = list(wallets) if wallets else tracked_wallets(c, chain, limit=limit, cohorts=cohorts)
    started = credits_used(c)

    for address in targets:
        spent = credits_used(c) - started
        if spent >= max_credits:
            report.stopped = "run_credit_ceiling"
            break
        if not _budget_room(c, _enhanced_cost()):
            report.stopped = "budget_exhausted"
            break
        result = backfill_wallet(
            address, chain, c,
            pages=pages, page_limit=page_limit, with_meta=with_meta, fresh=fresh, dry_run=dry_run,
        )
        report.results.append(result)
        report.wallets += 1
        report.pages += result.pages
        report.transactions += result.transactions
        report.swaps_written += result.swaps_written
        report.swaps_duplicate += result.swaps_duplicate
        report.transfers_written += result.transfers_written
        report.meta_written += result.meta_written
        report.ambiguous += result.ambiguous
        report.skips.update(result.skips)
        if result.error:
            report.wallets_failed += 1
        if result.stopped == "budget_exhausted":
            report.stopped = "budget_exhausted"
            break

    report.credits_spent = max(0, credits_used(c) - started)
    if rebuild_buyers and not dry_run and report.swaps_written:
        report.first_buyers_written = rebuild_first_buyers(c, chain)
    if not dry_run:
        _emit_summary(c, chain, report)
    return report


def _emit_summary(conn: sqlite3.Connection, chain: Chain, report: BackfillReport) -> None:
    """One event per run, not one per swap.

    A live listener emits `wallet.trade` per fill because each one is a signal. A backfill
    replays history, and pushing tens of thousands of weeks-old trades onto the bus would
    drown the dashboard and hand the reflection job a fake burst of activity.
    """
    try:
        emit(EventKind.SYSTEM, {"action": "backfill", **report.as_dict()}, chain=chain, conn=conn)
    except sqlite3.Error as exc:  # pragma: no cover - telemetry must not break ingest
        log.debug("backfill: summary event not recorded: %s", exc)


# --------------------------------------------------------------------------------------
# first buyers
# --------------------------------------------------------------------------------------


def rebuild_first_buyers(
    conn: sqlite3.Connection | None = None,
    chain: Chain = Chain.SOL,
    *,
    tokens: Sequence[str] | None = None,
    per_token: int = FIRST_BUYERS_PER_TOKEN,
) -> int:
    """Rank the earliest buyers we have observed for each token.

    **This is an observation ranking, not a chain ranking**, and the ``source`` column says
    so. We only see the wallets we backfilled, so rank 1 here means "first among the
    addresses we watch", which is a biased sample of the true buy order. The bias is
    one-directional and knowable, which is why it is recorded rather than hidden:
    downstream, `grade._early_metrics` requires at least ten observed buyers on a token
    before it counts an early entry as validated, and `seconds_after_open` is left ``None``
    unless `tokens.created_ms` gives a real launch time — never derived from our own first
    sighting, which would manufacture insider flags out of sampling luck.
    """
    c = conn or get_conn()
    sql = (
        "SELECT token, wallet, ts_ms, slot, amount_native FROM swaps "
        "WHERE chain = ? AND side = 'buy'"
    )
    params: list[Any] = [chain.value]
    if tokens:
        sql += f" AND token IN ({','.join('?' for _ in tokens)})"
        params.extend(tokens)
    sql += " ORDER BY token, ts_ms ASC, COALESCE(slot, 0) ASC, id ASC"
    rows = fetch_all(c, sql, params)
    if not rows:
        return 0

    opens = {
        row["address"]: _as_int(row["created_ms"])
        for row in fetch_all(
            c, "SELECT address, created_ms FROM tokens WHERE chain = ?", (chain.value,)
        )
    }

    written = 0
    per_token_seen: dict[str, set[str]] = defaultdict(set)
    ranks: dict[str, int] = defaultdict(int)
    for row in rows:
        token, wallet = str(row["token"]), str(row["wallet"])
        if wallet in per_token_seen[token] or ranks[token] >= per_token:
            continue
        per_token_seen[token].add(wallet)
        ranks[token] += 1
        opened = opens.get(token)
        ts_ms = _as_int(row["ts_ms"]) or 0
        seconds = (ts_ms - opened) / 1000 if opened is not None else None
        c.execute(
            "INSERT INTO first_buyers "
            "(chain, token, wallet, rank, slot, ts_ms, seconds_after_open, amount_native, source) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(chain, token, wallet) DO UPDATE SET "
            "  rank = excluded.rank, slot = excluded.slot, ts_ms = excluded.ts_ms, "
            "  seconds_after_open = excluded.seconds_after_open, "
            "  amount_native = excluded.amount_native, source = excluded.source",
            (
                chain.value, token, wallet, ranks[token], _as_int(row["slot"]), ts_ms,
                seconds, row["amount_native"], f"{SOURCE}:observed",
            ),
        )
        written += 1
    return written


# --------------------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------------------


def backfill_status(
    conn: sqlite3.Connection | None = None, chain: Chain = Chain.SOL
) -> dict[str, Any]:
    """Progress and credit spend, for `kaiba backfill status` and the dashboard."""
    c = conn or get_conn()
    prefix = f"backfill:swaps:{chain.value}:"
    cursors = fetch_all(c, "SELECT value FROM kv WHERE key LIKE ?", (prefix + "%",))
    exhausted = 0
    for row in cursors:
        state = jload(row["value"], {})
        if isinstance(state, dict) and state.get("exhausted"):
            exhausted += 1

    def _count(sql: str, params: Sequence[Any] = ()) -> int:
        row = fetch_one(c, sql, params)
        return int(row["n"]) if row else 0

    swaps = _count("SELECT COUNT(*) AS n FROM swaps WHERE chain = ?", (chain.value,))
    meta_matched = _count(
        "SELECT COUNT(DISTINCT s.tx) AS n FROM swaps s JOIN swap_meta m "
        "ON m.chain = s.chain AND m.tx = s.tx WHERE s.chain = ?",
        (chain.value,),
    )
    swap_txs = _count("SELECT COUNT(DISTINCT tx) AS n FROM swaps WHERE chain = ?", (chain.value,))
    graded = _count(
        "SELECT COUNT(*) AS n FROM wallet_scores WHERE chain = ? AND evidence_weight > 0",
        (chain.value,),
    )
    budget = helius.budget_status(conn=c)
    return {
        "chain": chain.value,
        "wallets_tracked": _count(
            "SELECT COUNT(*) AS n FROM wallets WHERE chain = ? AND COALESCE(cohort,'') != 'blacklist'",
            (chain.value,),
        ),
        "wallets_started": len(cursors),
        "wallets_exhausted": exhausted,
        "swaps": swaps,
        "swap_transactions": swap_txs,
        "swaps_buy": _count(
            "SELECT COUNT(*) AS n FROM swaps WHERE chain = ? AND side = 'buy'", (chain.value,)
        ),
        "swaps_sell": _count(
            "SELECT COUNT(*) AS n FROM swaps WHERE chain = ? AND side = 'sell'", (chain.value,)
        ),
        "swap_wallets": _count(
            "SELECT COUNT(DISTINCT wallet) AS n FROM swaps WHERE chain = ?", (chain.value,)
        ),
        "swap_tokens": _count(
            "SELECT COUNT(DISTINCT token) AS n FROM swaps WHERE chain = ?", (chain.value,)
        ),
        "first_buyers": _count(
            "SELECT COUNT(*) AS n FROM first_buyers WHERE chain = ?", (chain.value,)
        ),
        "swap_meta": _count("SELECT COUNT(*) AS n FROM swap_meta WHERE chain = ?", (chain.value,)),
        "swap_meta_matched_pct": round(100.0 * meta_matched / swap_txs, 1) if swap_txs else 0.0,
        "transfers": _count(
            "SELECT COUNT(*) AS n FROM transfers WHERE chain = ? AND source = ?",
            (chain.value, SOURCE),
        ),
        "wallets_with_evidence": graded,
        "helius": {
            "period": budget["period"],
            "used": budget["used"],
            "remaining": budget["remaining"],
            "allowance": budget["allowance"],
            "pct_used": budget["pct_used"],
            "estimated_credits": budget["estimated_credits"],
            "resets_in_s": budget["resets_in_s"],
        },
    }


__all__ = [
    "DEFAULT_MAX_CREDITS",
    "DEFAULT_PAGES_PER_WALLET",
    "DEFAULT_PAGE_LIMIT",
    "MIN_QUOTE_LAMPORTS",
    "SOURCE",
    "BackfillReport",
    "PageParse",
    "SwapRow",
    "TransferRow",
    "WalletResult",
    "backfill_status",
    "backfill_wallet",
    "backfill_wallets",
    "classify_swap",
    "cursor_key",
    "enhanced_meta",
    "load_cursor",
    "parse_page",
    "raw_meta",
    "rebuild_first_buyers",
    "save_cursor",
    "tracked_wallets",
    "transfer_rows",
    "write_meta",
    "write_swaps",
    "write_transfers",
]
