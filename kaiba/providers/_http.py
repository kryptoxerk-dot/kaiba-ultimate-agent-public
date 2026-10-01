"""Shared HTTP plumbing for every provider adapter.

Four adapters were about to be written in parallel, and without this they would each have
invented their own caching, their own idea of what a failure returns, and their own places
to accidentally log an API key. The contract (docs/CONTRACT.md) says a provider must never
raise on being down, must go through the limiter, must cache with a TTL, and must attach a
Receipt to everything. That is fiddly enough to get subtly wrong four times, so it lives
here once.

The single most important property: **a failure returns data=None with an UNAVAILABLE
receipt, never a zero and never an exception.** A zero from a dead provider looks exactly
like a real zero to a scoring function, and that is how a trading system talks itself into
a position on no evidence.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from kaiba.core import redact as _redact_mod
from kaiba.core.limiter import Priority, RateLimited, guarded
from kaiba.core.schemas import EvidenceBasis, Receipt, digest, now_ms

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 10.0
MAX_CACHE_BYTES = 8 * 1024 * 1024

# Redaction lives in kaiba.core.redact so the limiter can use it too without a cycle,
# and so there is exactly one implementation to keep correct. Re-exported here because
# every provider adapter already imports it from this module.
SECRET_NAMES = _redact_mod.SECRET_NAMES
redact = _redact_mod.redact
redact_text = _redact_mod.redact_text


@dataclass(frozen=True)
class Fetched:
    """A provider response plus the proof of where it came from."""

    data: Any | None
    receipt: Receipt

    @property
    def ok(self) -> bool:
        """True only when we actually hold data. A cached or stale hit still counts."""
        return self.data is not None and self.receipt.basis is not EvidenceBasis.UNAVAILABLE

    def __bool__(self) -> bool:
        return self.ok


# ------------------------------------------------------------------------------- cache


def _cache_root() -> Path:
    from kaiba.core.config import get_settings

    return Path(get_settings().kaiba_data_dir) / "cache"


def cache_path(provider: str, key: str) -> Path:
    safe = re.sub(r"[^a-z0-9_.-]", "_", provider.lower())[:40]
    return _cache_root() / safe / f"{digest(key)[:32]}.json"


def cache_read(
    provider: str, key: str, ttl_s: float, stale_grace_s: float = 0.0
) -> tuple[Any, bool, int] | None:
    """Return ``(data, is_stale, fetched_ms)`` or ``None``.

    ``fetched_ms`` is the moment the provider answered, not the moment we read the file.
    A Receipt stamped with the read time makes every cached value look brand new, so
    ``Measure.stale`` could never fire and a stop-loss could act on a price of unbounded
    age believing it was fresh.
    """
    if ttl_s <= 0 and stale_grace_s <= 0:
        # A zero TTL means "never serve this from disk". Without this guard an entry
        # written in the same millisecond has age exactly 0, and `0.0 <= 0` served it —
        # so an uncached price could still come back cached, intermittently.
        return None
    p = cache_path(provider, key)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        fetched_ms = int(raw["fetched_ms"])
        age_s = (now_ms() - fetched_ms) / 1000.0
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if age_s <= ttl_s:
        return raw.get("data"), False, fetched_ms
    if stale_grace_s and age_s <= ttl_s + stale_grace_s:
        return raw.get("data"), True, fetched_ms
    return None


def cache_write(provider: str, key: str, data: Any) -> None:
    """Best effort. A cache that cannot be written is not a reason to fail a request."""
    p = cache_path(provider, key)
    try:
        blob = json.dumps({"fetched_ms": now_ms(), "data": data}, separators=(",", ":"))
        if len(blob) > MAX_CACHE_BYTES:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(blob, encoding="utf-8")
        tmp.replace(p)
    except (OSError, TypeError, ValueError) as exc:
        log.debug("cache write skipped for %s: %s", provider, exc)


# ------------------------------------------------------------------------------ request


def _unavailable(provider: str, endpoint: str, note: str, req_digest: str | None = None) -> Fetched:
    return Fetched(
        None,
        Receipt(
            provider=provider,
            endpoint=endpoint,
            basis=EvidenceBasis.UNAVAILABLE,
            request_digest=req_digest,
            note=redact_text(note)[:300],
        ),
    )


def _report_error(provider: str, endpoint: str, note: str, conn: Any = None) -> None:
    from kaiba.core import events as ev
    from kaiba.core.schemas import EventKind

    try:
        ev.emit(
            EventKind.PROVIDER_ERROR,
            {"provider": provider, "endpoint": endpoint, "detail": redact_text(note)[:300]},
            level="warn",
            dedupe_key=f"provider_error:{provider}:{endpoint}:{redact_text(note)[:60]}",
            conn=conn,
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never break a fetch
        log.debug("could not record provider error: %s", exc)


def request_json(
    provider: str,
    endpoint: str,
    url: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    priority: Priority = Priority.RESEARCH,
    ttl_s: float = 0.0,
    stale_grace_s: float = 0.0,
    cache_key: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    retries: int = 1,
    wait_for_slot_s: float = 0.0,
    conn: Any = None,
) -> Fetched:
    """Fetch JSON through the limiter and the disk cache. Never raises.

    ``endpoint`` is the limiter's ``family.name`` string; the family before the dot is what
    gets cooled down on a 429, so keep related routes under one family.

    ``ttl_s`` of 0 disables caching, which is correct for anything price-like.
    ``stale_grace_s`` lets a dead provider serve an expired value marked ``STALE`` rather
    than nothing, which the caller can then choose to distrust.

    ``wait_for_slot_s`` waits that long for limiter capacity instead of returning
    ``UNAVAILABLE`` immediately. **Any adapter that makes more than one call per logical
    operation needs it.** Two adapters were written against the non-waiting default and
    both silently lost every call after the first: RugCheck has three routes per scan and
    only the first ran, so the one route that can *clear* a token never did and the
    provider contributed nothing but risk names. A batched price lookup lost every chunk
    after the first, which is fifteen positions with no price and no error. Leave it at 0
    only for a genuinely single-shot call where being late is worse than being absent.
    """
    key = cache_key or f"{method}:{url}:{json.dumps(params or {}, sort_keys=True)}"
    req_digest = digest(key)

    if ttl_s > 0:
        hit = cache_read(provider, key, ttl_s, stale_grace_s)
        if hit is not None:
            data, is_stale, fetched_ms = hit
            return Fetched(
                data,
                Receipt(
                    provider=provider,
                    endpoint=endpoint,
                    observed_at_ms=fetched_ms,
                    basis=EvidenceBasis.STALE if is_stale else EvidenceBasis.CACHED,
                    request_digest=req_digest,
                    response_digest=digest(data),
                ),
            )

    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - httpx is a hard dependency
        return _unavailable(provider, endpoint, f"httpx missing: {exc}", req_digest)

    slot_deadline = time.monotonic() + wait_for_slot_s if wait_for_slot_s > 0 else 0.0
    last_note = "no attempt made"
    attempt = -1
    while True:
        attempt += 1
        if attempt >= max(1, retries) and time.monotonic() >= slot_deadline:
            break
        try:
            with guarded(provider, endpoint, priority, conn=conn):
                resp = httpx.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    json=json_body,
                    timeout=timeout_s,
                    follow_redirects=True,
                )
                if resp.status_code == 429:
                    err = RuntimeError("429 rate limited")
                    err.status_code = 429  # type: ignore[attr-defined]
                    retry_after = resp.headers.get("retry-after")
                    if retry_after:
                        try:
                            err.retry_after_s = float(retry_after)  # type: ignore[attr-defined]
                        except ValueError:
                            pass
                    raise err
                resp.raise_for_status()
                # parse_float=Decimal so a price or a liquidity figure never
                # passes through binary floating point on its way to a decision.
                data = json.loads(resp.text, parse_float=Decimal)
        except RateLimited as exc:
            # Wait for capacity rather than giving up, when the caller allowed it. The
            # retry re-enters ``guarded``, so exactly one reservation is charged per
            # successful request; reserving here as well would double-charge and then
            # trip the provider's own minimum interval on the very next call.
            remaining = slot_deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(remaining, max(0.05, float(getattr(exc, "retry_after_s", 0.25) or 0.25))))
                continue
            note = redact_text(f"rate limited: {exc}")
            _report_error(provider, endpoint, note, conn)
            return _unavailable(provider, endpoint, note, req_digest)
        except Exception as exc:  # noqa: BLE001 - a provider being down is data, not a crash
            last_note = redact_text(f"{type(exc).__name__}: {exc}")[:300]
            if attempt + 1 < max(1, retries):
                time.sleep(min(2.0, 0.25 * (2**attempt)))
                continue
            _report_error(provider, endpoint, last_note, conn)
            if stale_grace_s > 0:
                stale = cache_read(provider, key, ttl_s, stale_grace_s + 86_400)
                if stale is not None:
                    return Fetched(
                        stale[0],
                        Receipt(
                            provider=provider,
                            endpoint=endpoint,
                            observed_at_ms=stale[2],
                            basis=EvidenceBasis.STALE,
                            request_digest=req_digest,
                            note=redact_text(f"serving stale after failure: {last_note}")[:300],
                        ),
                    )
            return _unavailable(provider, endpoint, last_note, req_digest)
        else:
            if ttl_s > 0:
                cache_write(provider, key, data)
            return Fetched(
                data,
                Receipt(
                    provider=provider,
                    endpoint=endpoint,
                    basis=EvidenceBasis.PROVIDER_REPORTED,
                    request_digest=req_digest,
                    response_digest=digest(data),
                ),
            )

    return _unavailable(provider, endpoint, last_note, req_digest)  # pragma: no cover


def get_json(provider: str, endpoint: str, url: str, **kw: Any) -> Fetched:
    """GET shorthand for :func:`request_json`."""
    return request_json(provider, endpoint, url, method="GET", **kw)


def post_json(provider: str, endpoint: str, url: str, **kw: Any) -> Fetched:
    """POST shorthand for :func:`request_json`."""
    return request_json(provider, endpoint, url, method="POST", **kw)
