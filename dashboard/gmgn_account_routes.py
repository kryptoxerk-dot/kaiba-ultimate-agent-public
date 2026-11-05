"""Optional, unwired account visibility routes for the existing operator console.

The lead app supplies its existing authentication and CSRF gates. Installing these
routes never reads settings, credentials, the database or GMGN. GET serves only the
last explicit check; POST may call the injected, read-only account projection once.
This module neither connects a browser wallet nor grants trading permission.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

ACCOUNT_PATH = "/api/gmgn/account"
CHECK_PATH = ACCOUNT_PATH + "/check"
MAX_BODY_BYTES = 512
NO_STORE = {
    "Cache-Control": "no-store, private, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
    "Vary": "Cookie, Authorization",
    "X-Content-Type-Options": "nosniff",
}


def _empty(status: str = "not_checked", reason: str = "explicit_check_required") -> dict[str, Any]:
    return {
        "provider": "gmgn",
        "status": status,
        "scope": "account_visibility_only",
        "configured": None,
        "checked_at_ms": None,
        "observed_at_ms": None,
        "age_ms": None,
        "fresh": None,
        "freshness_budget_ms": 60_000,
        "basis": None,
        "wallets": [],
        "wallet_count": 0,
        "omitted_count": 0,
        "truncated": False,
        "reason": reason,
    }


def install(
    app: FastAPI,
    reader: Callable[[], Mapping[str, Any]],
    *,
    authenticate: Callable[[Request], Any],
    csrf: Callable[[Request, Any, str | None], None],
    clock: Callable[[], float] = time.monotonic,
    cooldown_s: float = 30.0,
) -> None:
    """Install two routes, with no I/O until an authenticated, CSRF-valid POST.

    ``authenticate(request)`` returns the existing app's auth context/password or
    raises HTTPException. ``csrf(request, context, token)`` has the existing
    ``_require_csrf`` signature, including header-token fallback. No credential,
    wallet address, chain, endpoint or provider argument is accepted from clients.

    The lock and cooldown are process-local; a multi-worker deployment must supply
    a shared upstream budget or route these checks through one worker. There is no
    autonomous refresh, cross-worker lock or cancellation of a running reader.
    """
    if not callable(reader) or not callable(authenticate) or not callable(csrf) or not callable(clock):
        raise TypeError("reader, authentication, CSRF and clock callbacks are required")
    if isinstance(cooldown_s, bool) or not isinstance(cooldown_s, (float, int)):
        raise ValueError("cooldown_s must be a finite positive number")
    if not math.isfinite(cooldown_s) or cooldown_s <= 0:
        raise ValueError("cooldown_s must be a finite positive number")
    if any(getattr(route, "path", None) in {ACCOUNT_PATH, CHECK_PATH} for route in app.routes):
        raise ValueError("GMGN account routes are already installed")

    guard = threading.Lock()
    state_lock = threading.Lock()
    snapshot = _empty()
    sampled_at: float | None = None
    next_check_at = 0.0
    in_progress = False

    def now() -> float:
        value = float(clock())
        if not math.isfinite(value):
            raise ValueError("invalid clock")
        return value

    def response(body: dict[str, Any], status_code: int = 200, retry: int | None = None) -> JSONResponse:
        headers = dict(NO_STORE)
        if retry is not None:
            headers["Retry-After"] = str(retry)
        return JSONResponse(body, status_code=status_code, headers=headers)

    def refused(exc: HTTPException) -> JSONResponse:
        # Auth callbacks are trusted gates, but their arbitrary exception details
        # (or provider URLs in a future wrapper) are never sent to the browser.
        messages = {401: "authentication required", 403: "bad or missing CSRF token"}
        return response({"detail": messages.get(exc.status_code, "account check refused")}, exc.status_code)

    def cached(current: float) -> dict[str, Any]:
        with state_lock:
            result = json.loads(json.dumps(snapshot, allow_nan=False))
            elapsed_ms = max(0, int((current - sampled_at) * 1000)) if sampled_at is not None else None
            retry = max(0, math.ceil(next_check_at - current))
            checking = in_progress
        # Receipt freshness is evaluated at the original check. Age that same
        # observation locally instead of leaving an old "fresh" badge forever.
        age = result.get("age_ms")
        if elapsed_ms is not None and type(age) in (int, float) and math.isfinite(age) and age >= 0:
            result["age_ms"] = int(age) + elapsed_ms
            budget = result.get("freshness_budget_ms")
            if type(budget) in (int, float) and math.isfinite(budget) and budget >= 0:
                if result["age_ms"] > budget and result.get("status") == "connected":
                    result.update(status="stale", fresh=False, reason="cached_observation_expired")
        result["check"] = {
            "in_progress": checking,
            "retry_after_s": retry,
            "snapshot_age_ms": elapsed_ms,
            "automatic_refresh": False,
        }
        return result

    def perform_check() -> JSONResponse:
        nonlocal snapshot, sampled_at, next_check_at, in_progress
        if not guard.acquire(blocking=False):
            result = cached(now())
            result["detail"] = "account check already in progress"
            return response(result, 409, max(1, result["check"]["retry_after_s"]))
        try:
            current = now()
            with state_lock:
                retry = max(0, math.ceil(next_check_at - current))
                if not retry:
                    in_progress = True
                    next_check_at = current + cooldown_s
            if retry:
                result = cached(current)
                result["detail"] = "account check cooldown active"
                return response(result, 429, retry)
            status_code = 200
            try:
                observed = reader()
                if not isinstance(observed, Mapping):
                    raise ValueError("account reader must return a projection")
                # Detach mutable provider/reader state, reject NaN/non-JSON values,
                # and keep the original response after the reader returns.
                replacement = json.loads(json.dumps(dict(observed), allow_nan=False))
            except Exception:
                replacement = _empty("unavailable", "account_check_failed")
                status_code = 503
            try:
                finished = now()
            except (TypeError, ValueError, OverflowError):
                # A supplied test/deployment clock cannot refund an attempted
                # check by failing after the provider has already been called.
                finished = current
            with state_lock:
                snapshot = replacement
                sampled_at = finished
                next_check_at = finished + cooldown_s
                in_progress = False
            return response(cached(finished), status_code)
        finally:
            with state_lock:
                in_progress = False
            guard.release()

    @app.get(ACCOUNT_PATH, name="gmgn_account_cached")
    def account_cached(request: Request) -> JSONResponse:
        try:
            authenticate(request)
        except HTTPException as exc:
            return refused(exc)
        if request.query_params:
            return response({"detail": "query arguments are not accepted"}, 400)
        return response(cached(now()))

    @app.post(CHECK_PATH, name="gmgn_account_check")
    async def account_check(request: Request) -> JSONResponse:
        try:
            context = authenticate(request)
        except HTTPException as exc:
            return refused(exc)
        if request.query_params:
            return response({"detail": "query arguments are not accepted"}, 400)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BODY_BYTES:
                return response({"detail": "account check body is too large"}, 413)
            body.extend(chunk)
        token = None
        if body:
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/x-www-form-urlencoded":
                return response({"detail": "only the CSRF form field is accepted"}, 415)
            try:
                fields = parse_qsl(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True,
                                   errors="strict", max_num_fields=1)
            except (ValueError, UnicodeError):
                return response({"detail": "invalid account check form"}, 400)
            if len(fields) != 1 or fields[0][0] != "_csrf":
                return response({"detail": "only the CSRF form field is accepted"}, 400)
            token = fields[0][1]
        try:
            csrf(request, context, token)
        except HTTPException as exc:
            return refused(exc)
        # A synchronous reader belongs in the worker pool. A pending GMGN check
        # must not block the console's event loop or its cache-only GET endpoint.
        return await run_in_threadpool(perform_check)
