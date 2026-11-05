"""Session cookie and CSRF token for the dashboard.

``itsdangerous`` is not in the dependency list and this needs exactly one signed value, so
the cookie is a small HMAC-SHA256 construction written out here:

    <base64url(json payload)>.<base64url(hmac-sha256(key, payload))>

The key is derived from ``settings.kaiba_dashboard_password`` plus a per-process random
salt. The salt means every restart invalidates outstanding cookies, which is the behaviour
we want for an operator console that can halt trading.

When no password is configured there is nothing to authenticate against. In that case the
app refuses every non-loopback client outright and shows a banner saying auth is off, which
is honest rather than pretending a blank password is security.

Every comparison goes through :func:`secrets.compare_digest` so a wrong guess costs the
same time as a right one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

COOKIE_NAME = "kaiba_session"
CSRF_FIELD = "_csrf"
CSRF_HEADER = "x-kaiba-csrf"
DEFAULT_TTL_S = 7 * 24 * 3600
ANON = "anon"

_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", "testclient", "::ffff:127.0.0.1"})

# Rotates on restart; also makes the CSRF token unguessable when auth is off.
_PROCESS_SALT = os.urandom(32)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def secret_key(password: str) -> bytes:
    """Derive the signing key. A different password yields a different key."""
    return hashlib.sha256(b"kaiba-dashboard-v1|" + _PROCESS_SALT + b"|" + password.encode()).digest()


def auth_enabled(password: str) -> bool:
    return bool(password)


def is_loopback(host: str | None) -> bool:
    """True for a client we are willing to trust when no password is configured."""
    if not host:
        return False
    return host.strip().lower() in _LOOPBACK


def issue_session(password: str, *, ttl_s: int = DEFAULT_TTL_S, now: float | None = None) -> str:
    """Mint a signed cookie value for an operator who just proved the password."""
    issued = int(now if now is not None else time.time())
    payload = json.dumps(
        {"sub": "operator", "iat": issued, "exp": issued + ttl_s, "jti": secrets.token_hex(8)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    mac = hmac.new(secret_key(password), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(mac)}"


def verify_session(token: str | None, password: str, *, now: float | None = None) -> bool:
    """Constant-time check of signature then expiry. Any malformed token is simply false."""
    if not token or not password:
        return False
    part, _, sig = token.partition(".")
    if not part or not sig:
        return False
    try:
        payload = _unb64(part)
        given = _unb64(sig)
    except (ValueError, TypeError):
        return False
    expected = hmac.new(secret_key(password), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, given):
        return False
    try:
        claims = json.loads(payload)
    except ValueError:
        return False
    return int(claims.get("exp", 0)) > int(now if now is not None else time.time())


def check_password(given: str, configured: str) -> bool:
    """Compare a submitted password with the configured one in constant time."""
    if not configured:
        return False
    return secrets.compare_digest(given.encode(), configured.encode())


def csrf_token(session_value: str | None, password: str) -> str:
    """Double-submit token bound to the session cookie (or to the process when auth is off)."""
    basis = (session_value or ANON).encode()
    mac = hmac.new(secret_key(password), b"csrf|" + basis, hashlib.sha256).digest()
    return _b64(mac)


def verify_csrf(given: str | None, session_value: str | None, password: str) -> bool:
    if not given:
        return False
    return hmac.compare_digest(given, csrf_token(session_value, password))
