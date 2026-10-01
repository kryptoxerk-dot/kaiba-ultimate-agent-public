"""Etherscan V2 wallet indexes (GRADE-6b, assigned to Codex by Claude).

Normal transactions, internal calls and ERC20 transfers are provider-reported
history, NOT reconstructed swaps/PnL. Preserve failed calls and multiple logs in
one transaction; the ingest owner must reconcile receipts before counting trades.
Callers supply a fixed end block and persist each returned cursor only alongside
its successfully ingested page. No wallet, swap, grade or trust writes occur here.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any

from kaiba.core.config import get_settings
from kaiba.core.limiter import Priority
from kaiba.core.schemas import CHAIN_IDS, Chain, EvidenceBasis, Receipt, digest
from kaiba.providers._http import Fetched, cache_path, get_json

PROVIDER = "etherscan"
BASE = "https://api.etherscan.io/v2/api"
ACTIONS = ("txlist", "txlistinternal", "tokentx")
SUPPORTED_CHAINS = frozenset({Chain.ETH, Chain.BSC, Chain.BASE, Chain.ROBINHOOD})
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")
_UINT = re.compile(r"[0-9]+\Z")


@dataclass(frozen=True)
class HistoryCursor:
    chain: Chain
    address: str
    action: str
    end_block: int
    start_block: int = 0
    page: int = 1
    page_size: int = 100
    previous_page_digest: str | None = None


@dataclass(frozen=True)
class HistoryPage:
    cursor: HistoryCursor
    rows: tuple[dict[str, Any], ...]
    next_cursor: HistoryCursor | None
    # Only means the provider returned a short page in this fixed query window.
    exhausted: bool


@dataclass(frozen=True)
class WalletHistory:
    pages: tuple[Fetched, ...]
    cursors: dict[str, HistoryCursor | None]
    requests_attempted: int
    errors: dict[str, str]

    @property
    def complete(self) -> bool:
        """All three indexes exhausted, not proof of finality or clean round trips."""
        return not self.errors and all(c is None for c in self.cursors.values())


def _unavailable(action: str, reason: str, receipt: Receipt | None = None) -> Fetched:
    receipt = receipt or Receipt(provider=PROVIDER, endpoint="account." + action)
    return Fetched(None, receipt.model_copy(update={
        "basis": EvidenceBasis.UNAVAILABLE, "note": reason,
    }))


def _validate(cursor: HistoryCursor) -> HistoryCursor:
    chain = Chain(cursor.chain)
    if chain not in SUPPORTED_CHAINS:
        raise ValueError("unsupported_chain")
    if not isinstance(cursor.address, str) or not _ADDRESS.fullmatch(cursor.address):
        raise ValueError("invalid_address")
    if cursor.action not in ACTIONS:
        raise ValueError("unsupported_action")
    for n in (cursor.start_block, cursor.end_block, cursor.page, cursor.page_size):
        if type(n) is not int:
            raise ValueError("invalid_pagination")
    if not 0 <= cursor.start_block <= cursor.end_block or cursor.page < 1:
        raise ValueError("invalid_pagination")
    if not 1 <= cursor.page_size <= 1000:
        raise ValueError("invalid_page_size")
    return replace(cursor, chain=chain, address=cursor.address.lower())


def _valid_row(row: Any, cursor: HistoryCursor) -> bool:
    if not isinstance(row, dict) or not _HASH.fullmatch(str(row.get("hash", ""))):
        return False
    for key in ("blockNumber", "timeStamp", "value"):
        # Preserve exact decimal strings. Reject floats, negatives and bools.
        if (not isinstance(row.get(key), str) or len(row[key]) > 78 or
                not _UINT.fullmatch(row[key])):
            return False
    if not cursor.start_block <= int(row["blockNumber"]) <= cursor.end_block:
        return False
    addresses = [row.get("from"), row.get("to"), row.get("contractAddress")]
    if any(a not in (None, "") and
           (not isinstance(a, str) or not _ADDRESS.fullmatch(a)) for a in addresses):
        return False
    if cursor.address not in [a.lower() for a in addresses if isinstance(a, str)]:
        return False
    if cursor.action == "tokentx":
        if not _ADDRESS.fullmatch(str(row.get("contractAddress", ""))):
            return False
        decimals = row.get("tokenDecimal")
        if decimals is not None and (not isinstance(decimals, str) or
                                    len(decimals) > 3 or not _UINT.fullmatch(decimals) or
                                    int(decimals) > 255):
            return False
    return True


def history_page(cursor: HistoryCursor, *, ttl_s: float = 60.0,
                 conn: Any = None) -> Fetched:
    """One bounded request through shared limiter/cache; failure never means empty.

    No paid-plan fallback or credential changes. Status-0 plan/auth/quota failures
    are unavailable and evicted from cache. No provider error text is echoed.
    """
    try:
        cursor = _validate(cursor)
    except (TypeError, ValueError):
        return _unavailable("history", "invalid_history_query")
    key = get_settings().etherscan_api_key
    if not key:
        return _unavailable(cursor.action, "credential_missing")
    params = dict(chainid=CHAIN_IDS[cursor.chain], module="account", action=cursor.action,
                  address=cursor.address, startblock=cursor.start_block, endblock=cursor.end_block,
                  page=cursor.page, offset=cursor.page_size, sort="asc")
    # Exclude credentials, include every query dimension. Never share chains/pages.
    cache_key = "evm-history-v1:" + json.dumps(params, sort_keys=True)
    fetched = get_json(PROVIDER, "account." + cursor.action, BASE,
                       params={**params, "apikey": key}, cache_key=cache_key,
                       ttl_s=ttl_s, stale_grace_s=0, priority=Priority.RESEARCH,
                       retries=1, wait_for_slot_s=3, timeout_s=15, conn=conn)

    def failed(reason: str) -> Fetched:
        try:
            cache_path(PROVIDER, cache_key).unlink(missing_ok=True)
        except OSError:
            pass
        return _unavailable(cursor.action, reason, fetched.receipt)

    if not fetched.ok or fetched.receipt.basis is EvidenceBasis.STALE:
        return failed("transport_unavailable")
    body = fetched.data
    if not isinstance(body, dict):
        return failed("invalid_envelope")
    status, rows = str(body.get("status")), body.get("result")
    message = str(body.get("message", "")).strip().lower()
    explicit_empty = status == "0" and message == "no transactions found" and rows == []
    if status != "1" and not explicit_empty:
        text = (message + " " + str(rows)).lower()
        reason = ("plan_restricted" if any(s in text for s in ("paid", "plan", "subscription"))
                  else "rate_limited" if any(s in text for s in ("rate limit", "max calls", "quota"))
                  else "credential_rejected" if any(s in text for s in ("api key", "apikey"))
                  else "provider_error")
        return failed(reason)
    if not isinstance(rows, list) or len(rows) > cursor.page_size:
        return failed("invalid_rows")
    if any(not _valid_row(row, cursor) for row in rows):
        return failed("invalid_history_row")
    blocks = [int(row["blockNumber"]) for row in rows]
    if blocks != sorted(blocks):
        return failed("rows_out_of_order")
    # Confirmation counts and other enrichment may change between requests for
    # the same events; they cannot turn a repeated page into new progress.
    identity_fields = ("hash", "blockNumber", "transactionIndex", "logIndex", "traceId",
                       "from", "to", "contractAddress", "value", "type")
    page_digest = digest([{k: row.get(k) for k in identity_fields} for row in rows])
    if rows and cursor.previous_page_digest == page_digest:
        return failed("repeated_page")
    exhausted = len(rows) < cursor.page_size
    next_cursor = None if exhausted else replace(
        cursor, page=cursor.page + 1, previous_page_digest=page_digest)
    return Fetched(HistoryPage(cursor, tuple(rows), next_cursor, exhausted), fetched.receipt)


def wallet_history(chain: Chain | str, address: str, *, end_block: int,
                   start_block: int = 0, page_size: int = 100, max_requests: int = 3,
                   cursors: dict[str, HistoryCursor | None] | None = None,
                   ttl_s: float = 60.0, conn: Any = None) -> WalletHistory:
    """Round-robin index pages, explicit request cap and resumable partial results.

    Returned cursors are proposed progress; persist only AFTER consuming their
    associated pages. On failure the failing cursor stays unchanged. Completed
    indexes carry None. No silent restart, ignored errors or automatic plan spend.
    Invalid caller cursor/budget is a ValueError before any request.
    """
    if type(max_requests) is not int or not 0 <= max_requests <= 100:
        raise ValueError("max_requests must be an integer from 0 to 100")
    expected = {a: _validate(HistoryCursor(Chain(chain), address, a, end_block,
                                         start_block, page_size=page_size)) for a in ACTIONS}
    pending = dict(expected) if cursors is None else dict(cursors)
    if set(pending) != set(ACTIONS):
        raise ValueError("resume requires all three index cursors")
    for action, cursor in pending.items():
        if cursor is not None:
            cursor = _validate(cursor)
            if replace(cursor, page=1, previous_page_digest=None) != expected[action]:
                raise ValueError("cursor does not belong to this query")
            pending[action] = cursor
    pages: list[Fetched] = []
    errors: dict[str, str] = {}
    attempts = 0
    while attempts < max_requests and any(c is not None for c in pending.values()):
        for action in ACTIONS:
            cursor = pending[action]
            if cursor is None or attempts >= max_requests:
                continue
            fetched = history_page(cursor, ttl_s=ttl_s, conn=conn)
            attempts += 1
            pages.append(fetched)
            if not fetched.ok:
                errors[action] = fetched.receipt.note or "unavailable"
                # Auth/plan/rate failures should not cost two more calls immediately.
                return WalletHistory(tuple(pages), pending, attempts, errors)
            pending[action] = fetched.data.next_cursor
    return WalletHistory(tuple(pages), pending, attempts, errors)
