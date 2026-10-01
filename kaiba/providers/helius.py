"""Helius adapter: Solana history, DAS token accounts, priority fees, and the webhook sink.

Three things shape this module.

**The allowance is monthly and the routes are not equally priced.** Helius Free is
1,000,000 credits per calendar month at 10 RPS. A priority-fee estimate costs 1 credit, a
DAS page costs 10, and a full-transaction history page costs 10 per 100 transactions
*returned*. ``kaiba.core.limiter`` counts requests, which cannot express that, so credit
spend is recorded per route in ``provider_budget`` (migration 004) and checked before every
call. When the month's allowance is gone the call returns ``None`` plus an ``UNAVAILABLE``
receipt instead of rolling quietly into overage billing at $5 per extra million.

**Some costs are documented and some are not.** :data:`CREDIT_COSTS` marks each one
``verified`` or not, and any ledger row touched by an unverified rate is flagged
``estimated`` so :func:`budget_status` can say what fraction of its own number is a guess.
Where a cost is unknown the code charges the *higher* plausible figure: over-counting
starves us early, under-counting bills us.

**A webhook is an unauthenticated public endpoint until proven otherwise.** Every payload
must carry a valid HMAC-SHA256 over the raw body, compared with :func:`hmac.compare_digest`;
an unsigned or wrongly signed payload is rejected with 401 and an emitted event, and is
never parsed for trading signal. The receiver fails closed: with no
``HELIUS_WEBHOOK_SECRET`` configured, nothing is accepted.

Nothing here raises on Helius being down or unconfigured — a dead provider returns
``(None, Receipt(basis=UNAVAILABLE))``, per docs/CONTRACT.md.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import math
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kaiba.core import events as ev
from kaiba.core.config import get_risk, get_settings
from kaiba.core.db import get_conn
from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, EventKind, EvidenceBasis, Receipt, normalize_address, now_ms
from kaiba.providers._http import Fetched, get_json, post_json

try:  # FastAPI is a declared dependency, but a provider must import without a web stack.
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse

    HAS_FASTAPI = True
except ImportError:  # pragma: no cover - exercised only on a stripped install
    HAS_FASTAPI = False

log = logging.getLogger(__name__)

PROVIDER = "helius"

RPC_URL = "https://mainnet.helius-rpc.com/"
ENHANCED_BASE = "https://api.helius.xyz/v0"

#: Free-plan monthly allowance. Verified on helius.dev/pricing, 2026-09-20 ("1M credits",
#: "10 Requests / sec"). Override per deployment with
#: ``provider_budgets.helius.monthly_credit_cap`` in config/risk.yaml.
FREE_MONTHLY_CREDITS = 1_000_000

#: Warn on the event bus once per period as the allowance runs down.
WARN_THRESHOLDS: tuple[float, ...] = (0.80, 0.95)

# Limiter endpoint strings are ``family.name``; the family is what a 429 cools down, so
# routes that share a backend share a family.
ROUTE_TRANSACTIONS = "tx.getTransactionsForAddress"
ROUTE_ENHANCED = "tx.enhancedHistory"
ROUTE_TOKEN_ACCOUNTS = "das.getTokenAccounts"
ROUTE_PRIORITY_FEE = "fee.getPriorityFeeEstimate"
ROUTE_HEALTH = "rpc.getHealth"
ROUTE_WEBHOOK = "webhook.push"

#: Suffix for the ledger row that holds charges we are not sure we were billed for. Kept
#: separate from the route's real row so one uncertain charge cannot make a month of
#: verified spend look like guesswork.
ERROR_SUFFIX = "!error"

#: Wallet cohorts the webhook receiver treats as "tracked".
TRACKED_COHORTS: tuple[str, ...] = ("tracked", "trusted_copy")

#: Enhanced-transaction types this receiver acts on.
WEBHOOK_TYPES: frozenset[str] = frozenset({"SWAP", "TRANSFER"})


@dataclass(frozen=True)
class CreditCost:
    """What one call to a route costs, and whether we could prove it.

    ``credits`` is the base charge. Routes whose price depends on the response size
    (``getTransactionsForAddress``) compute the real figure in :func:`transaction_credits`
    and use this only as the pre-call reservation floor.
    """

    credits: int
    verified: bool
    source: str


CREDIT_COSTS: dict[str, CreditCost] = {
    ROUTE_TRANSACTIONS: CreditCost(
        10,
        True,
        "helius.dev/docs/rpc/gettransactionsforaddress, read 2026-09-20: full transactions "
        "'10 credits per 100 returned transactions, rounded up; 10-credit minimum', "
        "signatures only '10 credits flat', failed responses free",
    ),
    ROUTE_TOKEN_ACCOUNTS: CreditCost(
        10,
        True,
        "helius.dev/pricing, read 2026-09-20: 'DAS calls 10 credits' (per page)",
    ),
    ROUTE_PRIORITY_FEE: CreditCost(
        1,
        True,
        "helius.dev/pricing, read 2026-09-20: 'Priority Fee API calls 1 credit'",
    ),
    ROUTE_HEALTH: CreditCost(
        1,
        True,
        "helius.dev/pricing, read 2026-09-20: standard RPC calls 1 credit",
    ),
    ROUTE_WEBHOOK: CreditCost(
        1,
        True,
        "helius.dev/pricing, read 2026-09-20: 'Webhook pushes 1 credit'. Charged per HTTP "
        "delivery here; whether Helius meters per delivery or per transaction inside it is "
        "NOT verified",
    ),
    ROUTE_ENHANCED: CreditCost(
        100,
        False,
        "ASSUMED 100 credits/call. The Enhanced Transactions API pages (legacy, maintenance "
        "mode) publish no credit cost; 100 comes from third-party summaries and from the "
        "Wallet API's documented 100 cr/call. Treat every figure derived from this route as "
        "a guess and prefer getTransactionsForAddress",
    ),
}


# --------------------------------------------------------------------------------- secrets


_API_KEY_QS = re.compile(r"(?i)(api[-_]?key=)[^&\s'\"]+")


def _api_key() -> str:
    return get_settings().helius_api_key or ""


def _webhook_secret() -> str:
    return get_settings().helius_webhook_secret or ""


def scrub(text: str | None) -> str | None:
    """Strip the API key out of any string we are about to hand back or log.

    ``httpx`` puts the full request URL into ``HTTPStatusError``, and the API key lives in
    that URL because Helius documents no header alternative. Everything this module returns
    therefore goes through here. Note this does **not** protect the note that
    ``_http.request_json`` emits to the event bus before we see it — see the module report;
    that fix belongs in ``_http.py``, which this task does not own.
    """
    if not text:
        return text
    out = _API_KEY_QS.sub(r"\1<redacted>", text)
    key = _api_key()
    if key and len(key) >= 8:
        out = out.replace(key, "<redacted>")
    return out


def _receipt(
    endpoint: str,
    basis: EvidenceBasis = EvidenceBasis.PROVIDER_REPORTED,
    note: str | None = None,
    **kw: Any,
) -> Receipt:
    return Receipt(provider=PROVIDER, endpoint=endpoint, basis=basis, note=scrub(note), **kw)


def _clean(receipt: Receipt) -> Receipt:
    """A receipt from the shared HTTP layer, with any leaked credential removed."""
    if receipt.note and receipt.note != scrub(receipt.note):
        return receipt.model_copy(update={"note": scrub(receipt.note)})
    return receipt


def _unavailable(endpoint: str, note: str) -> tuple[None, Receipt]:
    return None, _receipt(endpoint, EvidenceBasis.UNAVAILABLE, note)


def available() -> bool:
    """True when a Helius credential is configured. No call is made."""
    return bool(_api_key())


def _no_credential(endpoint: str, conn: sqlite3.Connection | None = None) -> tuple[None, Receipt]:
    """Uniform answer when HELIUS_API_KEY is absent: unavailable, not an exception."""
    _emit(
        EventKind.PROVIDER_ERROR,
        {"provider": PROVIDER, "endpoint": endpoint, "detail": "HELIUS_API_KEY not set"},
        level="warn",
        dedupe_key=f"provider_error:helius:missing_credential:{_period()}",
        conn=conn,
    )
    return _unavailable(endpoint, "HELIUS_API_KEY not set")


def _emit(kind: EventKind, payload: dict[str, Any], **kw: Any) -> None:
    """Telemetry must never break a fetch, and the DB may not exist yet."""
    try:
        ev.emit(kind, payload, **kw)
    except Exception as exc:  # noqa: BLE001 - see docstring
        log.debug("helius event not recorded: %s", exc)


# ---------------------------------------------------------------------------- credit ledger


def _period(ts_ms: int | None = None) -> str:
    """UTC month id. Helius resets the allowance on the calendar month, not on signup date."""
    return datetime.fromtimestamp((ts_ms or now_ms()) / 1000, UTC).strftime("%Y-%m")


def period_reset_ms(period: str | None = None) -> int:
    """First millisecond of the month after ``period`` — the allowance reset boundary."""
    p = period or _period()
    year, month = (int(x) for x in p.split("-"))
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return int(datetime(year, month, 1, tzinfo=UTC).timestamp() * 1000)


def monthly_allowance() -> int:
    """Credits per month. ``provider_budgets.helius.monthly_credit_cap`` overrides the free tier."""
    try:
        cfg = (get_risk().provider_budgets or {}).get(PROVIDER) or {}
        return max(0, int(cfg.get("monthly_credit_cap", FREE_MONTHLY_CREDITS)))
    except (TypeError, ValueError, OSError) as exc:
        log.debug("falling back to the free-tier allowance: %s", exc)
        return FREE_MONTHLY_CREDITS


def transaction_credits(returned: int, transaction_details: str = "full") -> int:
    """Cost of one ``getTransactionsForAddress`` page, from the documented metering.

    Full transactions: 10 credits per 100 returned, rounded up, minimum 10.
    Signatures only: 10 credits flat. Failed responses: free.
    """
    if transaction_details != "full":
        return 10
    return max(10, 10 * math.ceil(max(0, returned) / 100))


def record_spend(
    route: str,
    credits: int,
    *,
    calls: int = 1,
    denied: int = 0,
    estimated: bool | None = None,
    conn: sqlite3.Connection | None = None,
    ts_ms: int | None = None,
) -> None:
    """Add spend to this period's ledger. Never raises; a lost row is not worth a crash."""
    cost = CREDIT_COSTS.get(route)
    est = (cost is not None and not cost.verified) if estimated is None else estimated
    ts = ts_ms or now_ms()
    c = conn or get_conn()
    try:
        c.execute(
            "INSERT INTO provider_budget "
            "(provider, route, period, calls, credits, denied, estimated, first_ms, last_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(provider, route, period) DO UPDATE SET "
            "  calls     = provider_budget.calls   + excluded.calls, "
            "  credits   = provider_budget.credits + excluded.credits, "
            "  denied    = provider_budget.denied  + excluded.denied, "
            "  estimated = MAX(provider_budget.estimated, excluded.estimated), "
            "  last_ms   = excluded.last_ms",
            (PROVIDER, route, _period(ts), int(calls), int(max(0, credits)), int(denied),
             1 if est else 0, ts, ts),
        )
    except sqlite3.Error as exc:
        log.warning("helius credit spend not recorded for %s: %s", route, exc)


def budget_status(
    conn: sqlite3.Connection | None = None, period: str | None = None
) -> dict[str, Any]:
    """Credits used, credits remaining, and when the allowance resets.

    ``estimated_credits`` is the part of ``used`` that was charged at a rate we could not
    verify from Helius' own documentation — read ``used`` with that in mind.
    """
    p = period or _period()
    allowance = monthly_allowance()
    rows: list[dict[str, Any]] = []
    used = estimated_used = denied = calls = 0
    c = conn or get_conn()
    try:
        for row in c.execute(
            "SELECT route, calls, credits, denied, estimated, first_ms, last_ms "
            "FROM provider_budget WHERE provider=? AND period=? ORDER BY credits DESC",
            (PROVIDER, p),
        ):
            r = dict(row)
            route = str(r["route"])
            if route.endswith(ERROR_SUFFIX):
                base = route[: -len(ERROR_SUFFIX)]
                r["cost_verified"] = False
                r["cost_source"] = (
                    f"ASSUMED: {base} answered with an RPC-level error over HTTP 200. Helius "
                    "does not document whether those are metered, so they are charged at the "
                    "base rate here"
                )
            else:
                cost = CREDIT_COSTS.get(route)
                r["cost_verified"] = bool(cost.verified) if cost else False
                r["cost_source"] = cost.source if cost else "unknown route"
            rows.append(r)
            used += int(r["credits"])
            calls += int(r["calls"])
            denied += int(r["denied"])
            if r["estimated"]:
                estimated_used += int(r["credits"])
    except sqlite3.Error as exc:
        log.warning("helius budget ledger unreadable: %s", exc)
        rows = []
    reset_ms = period_reset_ms(p)
    return {
        "provider": PROVIDER,
        "period": p,
        "allowance": allowance,
        "used": used,
        "remaining": max(0, allowance - used),
        "exhausted": used >= allowance,
        "pct_used": round(100.0 * used / allowance, 3) if allowance else 100.0,
        "calls": calls,
        "denied_calls": denied,
        "estimated_credits": estimated_used,
        "estimated_pct_of_used": round(100.0 * estimated_used / used, 1) if used else 0.0,
        "resets_at_ms": reset_ms,
        "resets_in_s": max(0, (reset_ms - now_ms()) // 1000),
        "by_route": rows,
        "ledger_is_local": True,
    }


def _warn_if_low(conn: sqlite3.Connection | None) -> None:
    st = budget_status(conn=conn)
    if not st["allowance"]:
        return
    frac = st["used"] / st["allowance"]
    for threshold in WARN_THRESHOLDS:
        if frac >= threshold:
            _emit(
                EventKind.PROVIDER_BUDGET,
                {
                    "provider": PROVIDER,
                    "period": st["period"],
                    "used": st["used"],
                    "remaining": st["remaining"],
                    "allowance": st["allowance"],
                    "threshold_pct": int(threshold * 100),
                    "resets_at_ms": st["resets_at_ms"],
                },
                level="warn",
                dedupe_key=f"helius:budget:{st['period']}:{int(threshold * 100)}",
                conn=conn,
            )


def _budget_block(
    route: str, reserve: int, conn: sqlite3.Connection | None
) -> tuple[None, Receipt] | None:
    """Refuse the call when the month's credits cannot cover it.

    Returns ``None`` when there is room. Otherwise records the denial, emits a budget event
    and hands back the ``UNAVAILABLE`` answer the caller should return.
    """
    st = budget_status(conn=conn)
    if st["remaining"] >= max(1, reserve):
        return None
    record_spend(route, 0, calls=0, denied=1, conn=conn)
    _emit(
        EventKind.PROVIDER_BUDGET,
        {
            "provider": PROVIDER,
            "period": st["period"],
            "route": route,
            "used": st["used"],
            "allowance": st["allowance"],
            "needed": reserve,
            "resets_at_ms": st["resets_at_ms"],
            "state": "exhausted",
        },
        level="warn",
        dedupe_key=f"helius:budget:{st['period']}:exhausted",
        conn=conn,
    )
    return _unavailable(
        route,
        f"helius monthly credit budget exhausted: {st['used']}/{st['allowance']} used, "
        f"resets in {st['resets_in_s']}s",
    )


# ------------------------------------------------------------------------------- transport


def _charged(fetched: Fetched) -> bool:
    """Only a real network round trip costs credits; a cache or stale hit is free."""
    return fetched.receipt.basis is EvidenceBasis.PROVIDER_REPORTED


def _rpc(
    method: str,
    params: Any,
    *,
    route: str,
    priority: Priority = Priority.RESEARCH,
    reserve: int | None = None,
    ttl_s: float = 0.0,
    cache_key: str | None = None,
    timeout_s: float = 10.0,
    conn: sqlite3.Connection | None = None,
) -> tuple[Any, Receipt, Fetched | None]:
    """One JSON-RPC call through the shared HTTP layer, with the credit ledger applied.

    Returns ``(result, receipt, fetched)``. ``fetched`` is ``None`` when we never called —
    missing credential or exhausted budget — so callers can tell "no answer" from "no call".
    """
    key = _api_key()
    if not key:
        data, receipt = _no_credential(route, conn)
        return data, receipt, None

    base = CREDIT_COSTS.get(route)
    blocked = _budget_block(route, reserve if reserve is not None else (base.credits if base else 1), conn)
    if blocked is not None:
        return blocked[0], blocked[1], None

    fetched = post_json(
        PROVIDER,
        route,
        RPC_URL,
        params={"api-key": key},
        json_body={"jsonrpc": "2.0", "id": f"kaiba-{method}", "method": method, "params": params},
        priority=priority,
        ttl_s=ttl_s,
        cache_key=cache_key,
        timeout_s=timeout_s,
        conn=conn,
    )
    receipt = _clean(fetched.receipt)
    if not fetched.ok or not isinstance(fetched.data, dict):
        return None, receipt, fetched

    if "error" in fetched.data and fetched.data.get("error"):
        # An RPC-level error still arrives as HTTP 200. Whether Helius meters it is not
        # documented, so charge the base cost (over-count rather than under-count) into the
        # route's uncertainty row.
        if _charged(fetched):
            record_spend(
                route + ERROR_SUFFIX, base.credits if base else 1, estimated=True, conn=conn
            )
        detail = json.dumps(fetched.data["error"], default=str)[:200]
        return None, _receipt(route, EvidenceBasis.UNAVAILABLE, f"rpc error: {detail}"), fetched

    return fetched.data.get("result"), receipt, fetched


# --------------------------------------------------------------------------------- reads


def ping(conn: sqlite3.Connection | None = None) -> tuple[str | None, Receipt]:
    """Cheapest authenticated read there is: ``getHealth``, 1 credit. Used by ``kaiba probe``."""
    result, receipt, fetched = _rpc("getHealth", [], route=ROUTE_HEALTH, conn=conn)
    if fetched is not None and _charged(fetched) and fetched.ok:
        record_spend(ROUTE_HEALTH, CREDIT_COSTS[ROUTE_HEALTH].credits, conn=conn)
        _warn_if_low(conn)
    return (result if isinstance(result, str) else None), receipt


def get_transactions_for_address(
    address: str,
    *,
    limit: int = 100,
    transaction_details: str = "full",
    sort_order: str = "desc",
    pagination_token: str | None = None,
    filters: Mapping[str, Any] | None = None,
    priority: Priority = Priority.RESEARCH,
    ttl_s: float = 30.0,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, Any] | None, Receipt]:
    """History for one address: ``{"data": [...], "paginationToken": str | None}``.

    Costs 10 credits per 100 transactions returned (rounded up, 10 minimum) for
    ``transaction_details="full"``, or 10 flat for ``"signatures"``. The short default TTL
    exists so that two lanes asking about the same wallet inside half a minute pay once.
    """
    try:
        addr = normalize_address(address, Chain.SOL)
    except ValueError as exc:
        return _unavailable(ROUTE_TRANSACTIONS, f"invalid solana address: {exc}")

    # Verified against the live RPC on 2026-09-20: the address is a positional string and
    # everything else is a config object. Passing a single object gets you
    # "Invalid params: invalid type: map, expected a string".
    config: dict[str, Any] = {
        "limit": max(1, min(int(limit), 1000)),
        "transactionDetails": transaction_details,
        "sortOrder": sort_order,
    }
    if pagination_token:
        config["paginationToken"] = pagination_token
    if filters:
        config["filters"] = dict(filters)

    # Reserve the worst case for this page so a nearly-empty budget cannot be overrun.
    reserve = transaction_credits(config["limit"], transaction_details)
    result, receipt, fetched = _rpc(
        "getTransactionsForAddress",
        [addr, config],
        route=ROUTE_TRANSACTIONS,
        priority=priority,
        reserve=reserve,
        ttl_s=ttl_s,
        cache_key=f"gtfa:{addr}:{json.dumps(config, sort_keys=True)}",
        conn=conn,
    )
    if result is None:
        return None, receipt
    if isinstance(result, list):  # tolerate a bare-array response shape
        result = {"data": result, "paginationToken": None}
    if not isinstance(result, dict):
        return _unavailable(ROUTE_TRANSACTIONS, f"unexpected result type {type(result).__name__}")

    if fetched is not None and _charged(fetched):
        rows = result.get("data") or []
        record_spend(
            ROUTE_TRANSACTIONS,
            transaction_credits(len(rows) if isinstance(rows, list) else 0, transaction_details),
            conn=conn,
        )
        _warn_if_low(conn)
    return result, receipt


def get_enhanced_transactions(
    address: str,
    *,
    limit: int = 100,
    before: str | None = None,
    until: str | None = None,
    tx_type: str | None = None,
    source: str | None = None,
    priority: Priority = Priority.RESEARCH,
    ttl_s: float = 30.0,
    conn: sqlite3.Connection | None = None,
) -> tuple[list[dict[str, Any]] | None, Receipt]:
    """Legacy Enhanced Transactions history (``/v0/addresses/{a}/transactions``).

    Helius has put this API into maintenance mode and points new work at
    ``getTransactionsForAddress`` (see :func:`get_transactions_for_address`), and its credit
    cost is **not documented** — we charge an assumed 100 per call. It is kept because the
    parsed ``type``/``description``/``tokenTransfers`` shape is the same one the webhook
    delivers, so backfill and live stream can share a parser.
    """
    try:
        addr = normalize_address(address, Chain.SOL)
    except ValueError as exc:
        return _unavailable(ROUTE_ENHANCED, f"invalid solana address: {exc}")
    key = _api_key()
    if not key:
        return _no_credential(ROUTE_ENHANCED, conn)

    cost = CREDIT_COSTS[ROUTE_ENHANCED]
    blocked = _budget_block(ROUTE_ENHANCED, cost.credits, conn)
    if blocked is not None:
        return blocked

    params: dict[str, Any] = {"api-key": key, "limit": max(1, min(int(limit), 100))}
    for name, value in (("before", before), ("until", until), ("type", tx_type), ("source", source)):
        if value:
            params[name] = value

    fetched = get_json(
        PROVIDER,
        ROUTE_ENHANCED,
        f"{ENHANCED_BASE}/addresses/{addr}/transactions",
        params=params,
        priority=priority,
        ttl_s=ttl_s,
        cache_key=f"enhanced:{addr}:{limit}:{before}:{until}:{tx_type}:{source}",
        conn=conn,
    )
    receipt = _clean(fetched.receipt)
    if not fetched.ok:
        return None, receipt
    if _charged(fetched):
        record_spend(ROUTE_ENHANCED, cost.credits, estimated=True, conn=conn)
        _warn_if_low(conn)
    if not isinstance(fetched.data, list):
        return _unavailable(ROUTE_ENHANCED, f"unexpected payload {type(fetched.data).__name__}")
    return fetched.data, receipt


def get_token_accounts_by_mint(
    mint: str,
    *,
    page: int = 1,
    limit: int = 1000,
    cursor: str | None = None,
    show_zero_balance: bool = False,
    priority: Priority = Priority.RESEARCH,
    ttl_s: float = 120.0,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, Any] | None, Receipt]:
    """One DAS ``getTokenAccounts`` page for a mint — the holder list. 10 credits per page."""
    try:
        addr = normalize_address(mint, Chain.SOL)
    except ValueError as exc:
        return _unavailable(ROUTE_TOKEN_ACCOUNTS, f"invalid solana mint: {exc}")

    params: dict[str, Any] = {
        "mint": addr,
        "limit": max(1, min(int(limit), 1000)),
        "options": {"showZeroBalance": bool(show_zero_balance)},
    }
    if cursor:
        params["cursor"] = cursor
    else:
        params["page"] = max(1, int(page))

    result, receipt, fetched = _rpc(
        "getTokenAccounts",
        params,
        route=ROUTE_TOKEN_ACCOUNTS,
        priority=priority,
        ttl_s=ttl_s,
        cache_key=f"das:{json.dumps(params, sort_keys=True)}",
        conn=conn,
    )
    if result is None:
        return None, receipt
    if not isinstance(result, dict):
        return _unavailable(ROUTE_TOKEN_ACCOUNTS, f"unexpected result type {type(result).__name__}")
    if fetched is not None and _charged(fetched):
        record_spend(ROUTE_TOKEN_ACCOUNTS, CREDIT_COSTS[ROUTE_TOKEN_ACCOUNTS].credits, conn=conn)
        _warn_if_low(conn)
    return result, receipt


def iter_token_accounts_by_mint(
    mint: str,
    *,
    max_pages: int = 10,
    limit: int = 1000,
    show_zero_balance: bool = False,
    priority: Priority = Priority.RESEARCH,
    conn: sqlite3.Connection | None = None,
) -> tuple[list[dict[str, Any]] | None, Receipt]:
    """Walk the holder pages for a mint, stopping at ``max_pages``.

    A page costs 10 credits, so an unbounded walk over a 40,000-holder token is 400 credits
    of a 1,000,000 monthly allowance for one dossier. ``max_pages`` is the guard; the receipt
    says ``partial`` when the walk stopped early so a caller cannot mistake a truncated
    holder list for the whole set.
    """
    out: list[dict[str, Any]] = []
    last: Receipt | None = None
    cursor: str | None = None
    truncated = False
    for index in range(max(1, max_pages)):
        payload, last = get_token_accounts_by_mint(
            mint,
            page=index + 1,
            limit=limit,
            cursor=cursor,
            show_zero_balance=show_zero_balance,
            priority=priority,
            conn=conn,
        )
        if payload is None:
            return (out, last) if out else (None, last)
        accounts = payload.get("token_accounts") or payload.get("tokenAccounts") or []
        out.extend(a for a in accounts if isinstance(a, dict))
        cursor = payload.get("cursor")
        if not accounts or len(accounts) < limit or (not cursor and payload.get("page") is None):
            break
        if index + 1 >= max(1, max_pages):
            truncated = True
    receipt = last or _receipt(ROUTE_TOKEN_ACCOUNTS, EvidenceBasis.UNAVAILABLE, "no pages fetched")
    if truncated:
        receipt = receipt.model_copy(
            update={"note": f"partial: stopped after {max_pages} pages ({len(out)} accounts)"}
        )
    return out, receipt


def get_priority_fee_estimate(
    *,
    account_keys: Sequence[str] | None = None,
    transaction: str | None = None,
    priority_level: str | None = None,
    include_all_levels: bool = False,
    recommended: bool = False,
    lookback_slots: int | None = None,
    priority: Priority = Priority.ENTRY,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, Any] | None, Receipt]:
    """Micro-lamports per compute unit for the accounts a trade will write to. 1 credit.

    Never cached: the whole point is the fee *now*. Defaults to ``Priority.ENTRY`` because
    this is called on the execution path, not during research.
    """
    payload: dict[str, Any] = {}
    if transaction:
        payload["transaction"] = transaction
        payload.setdefault("options", {})["transactionEncoding"] = "Base64"
    if account_keys:
        payload["accountKeys"] = list(account_keys)
    if not payload:
        return _unavailable(ROUTE_PRIORITY_FEE, "need accountKeys or a serialized transaction")

    options: dict[str, Any] = payload.get("options", {})
    if priority_level:
        options["priorityLevel"] = priority_level
    if include_all_levels:
        options["includeAllPriorityFeeLevels"] = True
    if recommended:
        options["recommended"] = True
    if lookback_slots is not None:
        options["lookbackSlots"] = max(1, min(int(lookback_slots), 150))
    if options:
        payload["options"] = options

    result, receipt, fetched = _rpc(
        "getPriorityFeeEstimate",
        [payload],
        route=ROUTE_PRIORITY_FEE,
        priority=priority,
        ttl_s=0.0,
        conn=conn,
    )
    if result is None:
        return None, receipt
    if not isinstance(result, dict):
        return _unavailable(ROUTE_PRIORITY_FEE, f"unexpected result type {type(result).__name__}")
    if fetched is not None and _charged(fetched):
        record_spend(ROUTE_PRIORITY_FEE, CREDIT_COSTS[ROUTE_PRIORITY_FEE].credits, conn=conn)
        _warn_if_low(conn)
    return result, receipt


# -------------------------------------------------------------------------------- webhook


def _hex_digest(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(
    body: bytes,
    *,
    signature: str | None,
    authorization: str | None = None,
    secret: str | None = None,
) -> bool:
    """Constant-time check of a webhook delivery. Fails closed.

    Accepts an HMAC-SHA256 of the raw body, hex or base64, with or without a ``sha256=``
    prefix. Helius' own webhook UI ships a *static* shared secret in the ``Authorization``
    header rather than an HMAC, so that form is accepted too — also with
    :func:`hmac.compare_digest`, never ``==`` — otherwise this receiver could not be pointed
    at a real Helius webhook. Everything is compared against ``HELIUS_WEBHOOK_SECRET``; with
    no secret configured, nothing verifies.
    """
    key = secret if secret is not None else _webhook_secret()
    if not key:
        return False

    expected_hex = _hex_digest(key, body)
    expected_raw = bytes.fromhex(expected_hex)

    if signature:
        candidate = signature.strip()
        if candidate.lower().startswith("sha256="):
            candidate = candidate[7:].strip()
        if hmac.compare_digest(candidate.lower(), expected_hex):
            return True
        try:
            decoded = base64.b64decode(candidate, validate=True)
        except (binascii.Error, ValueError):
            decoded = b""
        if decoded and hmac.compare_digest(decoded, expected_raw):
            return True

    if authorization:
        token = authorization.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if hmac.compare_digest(token, key):
            return True
        if hmac.compare_digest(token.lower(), expected_hex):
            return True
    return False


def tracked_wallets(
    conn: sqlite3.Connection | None = None, cohorts: Iterable[str] = TRACKED_COHORTS
) -> set[str]:
    """Addresses the webhook is allowed to raise signal for."""
    c = conn or get_conn()
    names = tuple(cohorts)
    if not names:
        return set()
    placeholders = ",".join("?" for _ in names)
    try:
        rows = c.execute(
            f"SELECT address FROM wallets WHERE chain='sol' AND cohort IN ({placeholders})", names
        ).fetchall()
    except sqlite3.Error as exc:
        log.warning("tracked wallet lookup failed: %s", exc)
        return set()
    return {r["address"] for r in rows}


def _addresses_in(tx: Mapping[str, Any]) -> set[str]:
    """Every address a parsed Helius transaction touches, cheaply."""
    found: set[str] = set()
    fee_payer = tx.get("feePayer")
    if isinstance(fee_payer, str):
        found.add(fee_payer)
    for key in ("nativeTransfers", "tokenTransfers"):
        for item in tx.get(key) or []:
            if not isinstance(item, dict):
                continue
            for field in ("fromUserAccount", "toUserAccount"):
                value = item.get(field)
                if isinstance(value, str):
                    found.add(value)
    for item in tx.get("accountData") or []:
        if isinstance(item, dict) and isinstance(item.get("account"), str):
            found.add(item["account"])
    return found


def _trim(tx: Mapping[str, Any], wallet: str) -> dict[str, Any]:
    """The part of a parsed transaction worth keeping on the bus."""
    return {
        "source": "helius_webhook",
        "type": tx.get("type"),
        "wallet": wallet,
        "signature": tx.get("signature"),
        "slot": tx.get("slot"),
        "timestamp": tx.get("timestamp"),
        "fee_payer": tx.get("feePayer"),
        "provider_source": tx.get("source"),
        "description": (tx.get("description") or "")[:300],
        "token_transfers": [
            {
                "mint": t.get("mint"),
                "from": t.get("fromUserAccount"),
                "to": t.get("toUserAccount"),
                "amount": t.get("tokenAmount"),
            }
            for t in (tx.get("tokenTransfers") or [])[:8]
            if isinstance(t, dict)
        ],
        "native_transfers": [
            {
                "from": t.get("fromUserAccount"),
                "to": t.get("toUserAccount"),
                "lamports": t.get("amount"),
            }
            for t in (tx.get("nativeTransfers") or [])[:8]
            if isinstance(t, dict)
        ],
    }


def handle_webhook(
    body: bytes,
    headers: Mapping[str, str] | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[int, dict[str, Any]]:
    """Verify, parse and fan out one webhook delivery. Returns ``(status_code, body)``.

    This is the whole receiver; the FastAPI route is a three-line adapter over it so the
    security behaviour is testable without a web server.
    """
    head = {k.lower(): v for k, v in (headers or {}).items()}
    signature = head.get("x-helius-signature") or head.get("x-signature") or head.get("x-hub-signature-256")
    authorization = head.get("authorization")

    if not verify_signature(body, signature=signature, authorization=authorization):
        _emit(
            EventKind.PROVIDER_ERROR,
            {
                "provider": PROVIDER,
                "endpoint": ROUTE_WEBHOOK,
                "detail": "webhook signature verification failed",
                "had_signature": bool(signature),
                "had_authorization": bool(authorization),
                "secret_configured": bool(_webhook_secret()),
                "body_bytes": len(body),
                "body_sha256": hashlib.sha256(body).hexdigest(),
            },
            level="warn",
            conn=conn,
        )
        return 401, {"ok": False, "error": "signature verification failed"}

    # A verified delivery is a real push, and Helius meters those. There is no budget gate
    # here on purpose: the credit was spent by Helius before we saw the request, so refusing
    # to process it would lose the data and save nothing. It still counts against the month,
    # and it can still trip the low-allowance warning.
    record_spend(ROUTE_WEBHOOK, CREDIT_COSTS[ROUTE_WEBHOOK].credits, conn=conn)
    _warn_if_low(conn)

    try:
        payload = json.loads(body.decode("utf-8") or "null")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _emit(
            EventKind.PROVIDER_ERROR,
            {"provider": PROVIDER, "endpoint": ROUTE_WEBHOOK, "detail": f"unparseable body: {exc}"},
            level="warn",
            conn=conn,
        )
        return 400, {"ok": False, "error": "unparseable body"}

    items = payload if isinstance(payload, list) else [payload]
    tracked = tracked_wallets(conn)
    emitted = 0
    matched: set[str] = set()
    considered = 0

    for tx in items:
        if not isinstance(tx, dict):
            continue
        considered += 1
        kind = str(tx.get("type") or "").upper()
        if kind not in WEBHOOK_TYPES:
            continue
        hits = sorted(_addresses_in(tx) & tracked)
        for wallet in hits:
            matched.add(wallet)
            written = ev.emit(
                EventKind.WALLET_TRADE,
                _trim(tx, wallet),
                chain=Chain.SOL,
                subject=wallet,
                dedupe_key=f"helius:webhook:{tx.get('signature')}:{wallet}",
                conn=conn,
            )
            if written is not None:
                emitted += 1

    return 200, {
        "ok": True,
        "received": considered,
        "emitted": emitted,
        "matched_wallets": sorted(matched),
        "tracked_wallets": len(tracked),
    }


def build_router() -> Any:
    """The webhook APIRouter, for the dashboard (or any app) to mount. ``None`` without FastAPI.

    Deliberately not mounted here: what is exposed to the internet is the operator's
    decision, and this module must stay importable by the CLI and the tests without pulling
    a web app into the process.
    """
    if not HAS_FASTAPI:  # pragma: no cover - exercised only on a stripped install
        log.warning("fastapi missing; the helius webhook router was not built")
        return None

    api = APIRouter(prefix="/webhooks", tags=["helius"])

    @api.post("/helius")
    async def helius_webhook(request: Request) -> JSONResponse:
        status_code, response = handle_webhook(await request.body(), dict(request.headers))
        return JSONResponse(status_code=status_code, content=response)

    return api


#: Importable router: ``app.include_router(helius.router)``.
router = build_router()
