"""Small read-only projection of GMGN's existing account-info provider wrapper.

Importing this module does no I/O. Only an explicit ``read_account()`` invokes the
shared provider once; callers own authentication, CSRF and refresh throttling.
``connected`` means that account-info answered with a usable recent receipt. It
does not establish holdings, custody, a subscription tier or permission to trade.

The outer wallets/chain/address/balances shape is recorded by gmgn_cli and
copy_manager. Balance element fields and units have no verified fixture here, so
nonempty balance arrays remain unsupported rather than acquiring guessed units.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any, Literal, TypedDict

FRESHNESS_BUDGET_MS = 60_000
MAX_PAYLOAD_BYTES = 128 * 1024
MAX_INPUT_WALLETS = 256
MAX_WALLETS = 32
MAX_BALANCE_ROWS = 32

_NATIVE_SYMBOL = {
    "sol": "SOL", "eth": "ETH", "bsc": "BNB", "base": "ETH",
    "robinhood": "ETH",
}
_EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_BASE58_ADDRESS = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}\Z")
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_RATE_LIMITED = re.compile(r"HTTP\s+429\b|RATE_LIMIT_(?:EXCEEDED|BANNED|BLOCKED)|limiter refused:", re.I)

AccountStatus = Literal["connected", "stale", "unavailable", "rate_limited", "invalid_response"]


class WalletView(TypedDict):
    chain: str
    address: str
    native_symbol: str
    native_balance: str | None
    balance_status: Literal["unavailable", "invalid", "unsupported"]


class AccountView(TypedDict):
    provider: str
    scope: str
    configured: None
    status: AccountStatus
    checked_at_ms: int
    observed_at_ms: int | None
    age_ms: int | None
    freshness_budget_ms: int
    fresh: bool | None
    basis: str | None
    wallets: list[WalletView]
    wallet_count: int
    omitted_count: int
    truncated: bool
    reason: str | None


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _default_fetch() -> Any:
    # Keep settings, credentials, cache, limiter and subprocess access exclusively
    # inside the established provider edge, and only after an explicit refresh.
    # That edge needs the key, so inside the wall the broker runs it.
    from dashboard import broker

    if broker.active():
        return broker.gmgn_account()

    from kaiba.providers.gmgn_cli import account_info

    return account_info(timeout_s=8.0, wait_for_slot_s=0.0)


def _empty(now: int) -> AccountView:
    return {
        "provider": "gmgn", "scope": "account_visibility_only", "configured": None,
        "status": "unavailable", "checked_at_ms": now, "observed_at_ms": None,
        "age_ms": None, "freshness_budget_ms": FRESHNESS_BUDGET_MS, "fresh": None,
        "basis": None, "wallets": [], "wallet_count": 0, "omitted_count": 0,
        "truncated": False, "reason": None,
    }


def _solana_address(address: str) -> bool:
    if _BASE58_ADDRESS.fullmatch(address) is None:
        return False
    number = 0
    for character in address:
        number = number * 58 + _BASE58_ALPHABET.index(character)
    leading_zero_bytes = len(address) - len(address.lstrip("1"))
    decoded_bytes = leading_zero_bytes + (number.bit_length() + 7) // 8
    return decoded_bytes == 32


def _wallet(row: Any) -> WalletView | None:
    if type(row) is not dict:
        return None
    chain, address = row.get("chain"), row.get("address")
    if not isinstance(chain, str) or chain not in _NATIVE_SYMBOL or not isinstance(address, str):
        return None
    valid = _solana_address(address) if chain == "sol" else _EVM_ADDRESS.fullmatch(address) is not None
    if not valid:
        return None
    if chain != "sol":
        address = address.lower()
    balances = row.get("balances")
    if type(balances) is not list or len(balances) > MAX_BALANCE_ROWS:
        balance_status = "invalid"
    elif not balances:
        # In particular, Robinhood has returned [] despite a funded native wallet.
        balance_status = "unavailable"
    else:
        balance_status = "unsupported"
    return {
        "chain": chain, "address": address, "native_symbol": _NATIVE_SYMBOL[chain],
        "native_balance": None, "balance_status": balance_status,
    }


def read_account(
    fetch: Callable[[], Any] | None = None, *, clock_ms: Callable[[], int] = _now_ms,
) -> AccountView:
    """Project one explicit account-info response without new retries or fallbacks.

    ``fetch`` supplies the established ``GmgnResult(data, receipt)`` contract; tests
    may inject an equivalent object. No user-supplied endpoint or wallet is accepted.
    Provider error details and unrecognized payload keys never leave this adapter.
    Observation time always comes from the original receipt, never refresh time.
    """
    started = clock_ms()
    if type(started) is not int or started <= 0:
        raise ValueError("clock_ms must return a positive integer")
    failure = None
    try:
        result = (fetch or _default_fetch)()
    except TimeoutError:
        failure = "provider_timeout"
    except Exception:  # The UI must not render provider errors, URLs or credentials.
        failure = "provider_exception"
    # A provider receipt is stamped while the fetch runs. Compare it with the
    # completion clock, including failed checks, rather than the request start.
    now = clock_ms()
    if type(now) is not int or now <= 0:
        raise ValueError("clock_ms must return a positive integer")
    out = _empty(now)
    if now < started:
        out.update(status="invalid_response", reason="check_clock_invalid")
        return out
    if failure:
        out["reason"] = failure
        return out

    receipt = getattr(result, "receipt", None)
    if getattr(receipt, "provider", None) != "gmgn" or getattr(receipt, "endpoint", None) != "portfolio.info":
        out.update(status="invalid_response", reason="receipt_identity_invalid")
        return out
    basis = getattr(receipt, "basis", None)
    if basis not in {"provider_reported", "cached", "stale", "unavailable"}:
        out.update(status="invalid_response", reason="receipt_basis_invalid")
        return out
    out["basis"] = str(basis)
    data = getattr(result, "data", None)
    if basis == "unavailable" or data is None:
        note = getattr(receipt, "note", None)
        if isinstance(note, str) and _RATE_LIMITED.search(note[:300]):
            out.update(status="rate_limited", reason="provider_rate_limited")
        else:
            out["reason"] = "provider_unavailable"
        return out

    observed = getattr(receipt, "observed_at_ms", None)
    if type(observed) is not int or observed <= 0 or observed > now:
        out.update(status="invalid_response", reason="observation_time_invalid")
        return out
    out.update(observed_at_ms=observed, age_ms=now - observed)
    if type(data) is not dict or type(data.get("wallets")) is not list:
        out.update(status="invalid_response", reason="wallets_shape_invalid")
        return out
    rows = data["wallets"]
    if len(rows) > MAX_INPUT_WALLETS:
        out.update(status="invalid_response", reason="payload_limit_exceeded")
        return out
    try:
        if len(json.dumps(data, allow_nan=False, separators=(",", ":")).encode("utf-8")) > MAX_PAYLOAD_BYTES:
            out.update(status="invalid_response", reason="payload_limit_exceeded")
            return out
    except (ValueError, TypeError, OverflowError, RecursionError):
        out.update(status="invalid_response", reason="payload_not_json")
        return out

    wallets: list[WalletView] = []
    seen: set[tuple[str, str]] = set()
    valid_count = 0
    for row in rows:
        wallet = _wallet(row)
        if wallet is None or (wallet["chain"], wallet["address"]) in seen:
            continue
        seen.add((wallet["chain"], wallet["address"]))
        valid_count += 1
        if len(wallets) < MAX_WALLETS:
            wallets.append(wallet)
    out.update(
        wallets=wallets, wallet_count=len(wallets), omitted_count=len(rows) - len(wallets),
        truncated=valid_count > MAX_WALLETS,
    )
    if rows and not wallets:
        out.update(status="invalid_response", reason="wallet_identities_invalid")
        return out
    fresh = basis != "stale" and now - observed <= FRESHNESS_BUDGET_MS
    out.update(status="connected" if fresh else "stale", fresh=fresh)
    if out["omitted_count"]:
        out["reason"] = "partial_wallet_data"
    return out
