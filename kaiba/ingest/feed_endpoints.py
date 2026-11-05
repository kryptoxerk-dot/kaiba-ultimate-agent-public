"""Which endpoint an ingest feed may use: one resolver for leader_exit, sol_wallets and flap.

Codex 2026-11-02, PROVIDER-ECONOMY: owner-authorized Alchemy no-spend
checks only; Claude retains the resolver, isolation, and fallback rules.

Why this exists (lead, 2026-10-21). From 2026-10-09 four feeds were dark for twelve days.
Each one gated on the same string check, ``"/v2/" in url``, against a URL it SHARED with
protection: the Alchemy key ran into its monthly cap, the chain URLs moved to public hosts
on 10-13, and every feed went idle for want of a ``/v2/``. Two lessons are built in here:

* **A feed must never share a host with protection.** Protection's stop reads go to
  ``rpc_for(chain)`` (sol), ``BSC_RPC_URL`` (bsc), or ``dyor_verify.PUBLIC_RPC`` when that is
  blank, and the Robinhood public RPC; a feed's 429s
  on the same host raise the ``rpc`` bucket's penalty for every stop read on that chain, and
  on a public node the per-IP limit is shared outright. The protection hosts are computed AT
  RUNTIME from the live settings, never from a constant: ``config.py`` defaults
  ``solana_rpc_url`` to mainnet-beta, so if ``SOLANA_RPC_URL`` ever drops out of ``.env``,
  protection silently moves onto the very host the sol feeds fall back to, and this check
  is what notices.
* **Nothing changes until someone lists a feed.** With every switch off (the default) a
  box whose URLs carry no ``/v2/`` gets exactly the idle result each feed returned before
  this module existed (:data:`TODAY_IDLE`; pinned by a test with the box's 2026-10-21 URL
  shapes).

``resolve(feed) -> FeedEndpoint | Refusal``, trying in this order (``KAIBA_FEED_PREFER_PUBLIC``
moves 3 to the front for the chains it lists):

1. a feeds-only keyed URL: ``SOLANA_FEED_ALCHEMY_URL`` / ``ROBINHOOD_FEED_ALCHEMY_URL``, and
   for bsc ``BSC_SNIPE_RPC_URL`` when it is a ``/v2/`` URL (its meaning before today);
2. the chain URL protection also uses, when it is a ``/v2/`` URL (exactly the old
   behaviour) -- only with ``KAIBA_FEED_ALLOW_SHARED_ALCHEMY=1``, because sharing that key
   is what blocked BSC entries 10-09 to 10-12;
3. a public endpoint, only for a feed listed in ``KAIBA_FEED_PUBLIC_FALLBACK`` (comma list,
   default EMPTY): sol ``SOLANA_FEED_WS_URL`` / ``SOLANA_FEED_HTTP_URL`` (default
   ``api.mainnet-beta.solana.com``, MEASURED 2026-10-21: 1.6-1.8 s push lag, 100
   subscriptions per socket accepted, two-address filters refused with an explicit error);
   bsc ``BSC_SNIPE_WS_URL`` (NO default) for the socket and ``BSC_SNIPE_RPC_URL`` for HTTP
   (optional: unset is socket-only mode); robinhood has none (no public websocket).

Never ``solana-rpc.publicnode.com`` as a websocket, by default OR configured: MEASURED
2026-10-21 it answered 6 of 34 ``logsSubscribe`` tries, silently accepted a two-address
filter and delivered nothing (:data:`SILENT_WS_HOSTS`).

Host invariant: a feed's HTTP host is never a protection host (except an Alchemy slot the
operator accepted with ``KAIBA_FEED_ALLOW_SHARED_ALCHEMY=1``). Its websocket host may be one
only on bsc with ``KAIBA_FEED_SHARED_WS_OK=bsc`` (MEASURED 2026-10-21: no second public BSC
websocket works, and that socket only receives pushes); flap then pauses its reconnect
backfill while protection's ``rpc`` bucket is under pressure.

Capacity probe (keyed slots only): one ``getHealth`` (sol) / ``eth_chainId`` (EVM), 5 s
timeout, on its own limiter bucket. An HTTP 429 whose body says "capacity" or "usage"
(Alchemy's "Monthly capacity limit exceeded", Helius's "max usage reached", both MEASURED
2026-10-21) means the key is capped: skip it. The verdict is cached in kv
``feed_endpoint:alchemy:<chain>`` for 6 h, storing the HOST only. Mid-run, :class:`CapWatch`
re-probes on a 429 and, when the key is capped, the feed returns ``{"fallback":
"alchemy_monthly_cap"}``; the runner restarts it (never marks it idle) and it re-resolves.

Secrets: the URLs carry keys. Nothing here logs, stores or returns one in a reason; hosts
only (``host_of``, ``alchemy_ws.mask_url``). :class:`FeedEndpoint`'s repr is masked.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from kaiba.core import db as _db
from kaiba.core.limiter import Priority, RateLimited, guarded
from kaiba.core.schemas import Chain, now_ms
from kaiba.ingest import alchemy_ws as aws

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# switches and endpoint variables (env, not risk.yaml: self-tune deletes loose keys there)
# --------------------------------------------------------------------------------------

ENV_PUBLIC_FALLBACK = "KAIBA_FEED_PUBLIC_FALLBACK"
ENV_ALLOW_SHARED_ALCHEMY = "KAIBA_FEED_ALLOW_SHARED_ALCHEMY"
ENV_SHARED_WS_OK = "KAIBA_FEED_SHARED_WS_OK"
ENV_PREFER_PUBLIC = "KAIBA_FEED_PREFER_PUBLIC"
ENV_SOL_FEED_ALCHEMY = "SOLANA_FEED_ALCHEMY_URL"
ENV_RH_FEED_ALCHEMY = "ROBINHOOD_FEED_ALCHEMY_URL"
ENV_SOL_FEED_WS = "SOLANA_FEED_WS_URL"
ENV_SOL_FEED_HTTP = "SOLANA_FEED_HTTP_URL"
ENV_BSC_SNIPE_WS = "BSC_SNIPE_WS_URL"
#: ``launch_feed.BSC_SNIPE_RPC_ENV``: flap's sender lookups and the snipe lane's portal reads.
ENV_BSC_SNIPE_RPC = "BSC_SNIPE_RPC_URL"

SOL_PUBLIC_WS_DEFAULT = "wss://api.mainnet-beta.solana.com"
SOL_PUBLIC_HTTP_DEFAULT = "https://api.mainnet-beta.solana.com"

#: Websocket hosts MEASURED to accept subscriptions and deliver nothing. Refused even when an
#: operator configures one: a feed that looks connected and receives nothing is the failure
#: these feeds already spent twelve days in.
SILENT_WS_HOSTS: dict[Chain, frozenset[str]] = {Chain.SOL: frozenset({"solana-rpc.publicnode.com"})}

#: The only chains whose feed websocket may share protection's host (with ENV_SHARED_WS_OK).
SHARED_WS_CHAINS: frozenset[Chain] = frozenset({Chain.BSC})

#: The feeds this resolver serves and their chain.
FEED_CHAINS: dict[str, Chain] = {
    "leader_exit": Chain.SOL,
    "sol_wallets": Chain.SOL,
    "flap": Chain.BSC,
    "rh_wallets": Chain.ROBINHOOD,
}
#: Feeds that cannot work without an HTTP endpoint (flap can run socket-only).
HTTP_REQUIRED: frozenset[str] = frozenset({"leader_exit", "sol_wallets", "rh_wallets"})

#: The exact idle result each feed returned before 2026-10-21 when nothing was configured.
#: With every switch off and no ``/v2/`` URL the resolver returns these unchanged, so a
#: deploy with the switches off changes nothing on the box.
TODAY_IDLE: dict[str, str] = {
    "leader_exit": "no Alchemy Solana endpoint (SOLANA_RPC_URL with /v2/)",
    "sol_wallets": "no websocket endpoint",
    "flap": "no websocket endpoint",
    "rh_wallets": "no websocket endpoint",
}

KIND_ALCHEMY = "alchemy"                # a feeds-only keyed URL
KIND_ALCHEMY_SHARED = "alchemy_shared"  # the chain URL protection uses too (opt-in)
KIND_PUBLIC = "public"
ALCHEMY_KINDS: frozenset[str] = frozenset({KIND_ALCHEMY, KIND_ALCHEMY_SHARED})

VERDICT_OK = "ok"
VERDICT_CAPPED = "capped"
VERDICT_UNKNOWN = "unknown"

CACHE_KEY = "feed_endpoint:alchemy:{chain}"
CACHE_TTL_MS = 6 * 3_600_000
PROBE_TIMEOUT_S = 5.0
#: The probe's own limiter bucket (built-in default budget, 1 call/s): never ``rpc``.
PROBE_BUCKET = "feed-probe"
#: A feed re-probes its key on a mid-run 429 at most this often.
CAP_PROBE_MIN_GAP_S = 60.0
#: The reason a feed returns, and the runner restarts on, when its key hit the cap mid-run.
FALLBACK_MONTHLY_CAP = "alchemy_monthly_cap"

_TRUE = frozenset({"1", "true", "yes", "on"})


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------


def host_of(url: Any) -> str | None:
    """The URL's host, lowercased, without port, credentials or a trailing dot; None if none."""
    text = str(url or "").strip()
    if not text:
        return None
    try:
        host = urlsplit(text if "://" in text else f"https://{text}").hostname
    except ValueError:
        return None
    host = (host or "").strip().rstrip(".").lower()
    return host or None


def _dotenv_values(name: str) -> str | None:
    """``name`` from the repo ``.env`` then ``~/.config/kaiba/.env`` (later wins, as in Settings)."""
    try:
        from dotenv import dotenv_values

        from kaiba.core.config import REPO_ROOT

        user_dir = Path(os.environ.get("KAIBA_CONFIG_DIR", Path.home() / ".config" / "kaiba"))
        found: str | None = None
        for path in (REPO_ROOT / ".env", user_dir / ".env"):
            if path.is_file():
                values = {str(k).upper(): v for k, v in dotenv_values(path).items()}
                found = values.get(name) or found
        return found
    except Exception as exc:  # noqa: BLE001 - an unreadable file is "not set"
        log.debug("feed_endpoints: %s unreadable from .env (%s)", name, type(exc).__name__)
        return None


def env_value(name: str, env: Mapping[str, str] | None = None) -> str | None:
    """A switch or URL: ``env`` when given (tests), else the process environment, else the
    ``.env`` files (``launch_feed.bsc_snipe_rpc_url``'s convention). Blank is unset."""
    if env is not None:
        value: str | None = env.get(name)
    else:
        value = os.environ.get(name) or os.environ.get(name.lower()) or _dotenv_values(name)
    value = (value or "").strip()
    return value or None


def listed(value: str | None) -> frozenset[str]:
    """``"leader_exit, flap"`` -> ``{"leader_exit", "flap"}`` (lowercased)."""
    return frozenset(p.strip().lower() for p in str(value or "").replace(";", ",").split(",") if p.strip())


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in _TRUE


def masked(url: Any) -> str:
    """A URL safe to log: ``alchemy_ws.mask_url`` (host only for a non-``/v2/`` shape)."""
    return aws.mask_url(str(url or "")) if url else "-"


def protection_hosts(chain: Chain, settings: Any = None) -> frozenset[str]:
    """The hosts protection's stop and price reads use on ``chain``, read from live settings.

    The ``rpc``-bucket readers resolve their URL as ``rpc_for(chain)`` when it is non-blank,
    ELSE ``dyor_verify.PUBLIC_RPC[chain]`` (``evm_price._rpc_url``, behind
    ``evm_price.json_rpc_batch`` at EXIT; ``dyor_verify._rpc_url``). This mirrors that rule
    exactly, so a blank or unset chain URL is guarded on the host protection falls back to,
    not on nothing. Review 2026-10-21: ``config.py`` defaults ``bsc_rpc_url`` to ``""``,
    and with BSC_RPC_URL unset protection reads ``bsc-dataseed.binance.org`` while this
    returned an empty set; ``SOLANA_RPC_URL=`` (blank) did the same on mainnet-beta.

    sol: also ``pumpswap_price`` (``rpc_for(SOL)``); bsc: ``evm_price.json_rpc_batch``;
    robinhood: ``rpc_for(ROBINHOOD)`` and ``robinhood.RPC_URL``
    (``evm_price.robinhood_rpc_batch``, hard-coded public host). A missing fallback table
    raises, and :func:`resolve` turns that into a refusal: the guard fails closed.
    """
    if settings is None:
        from kaiba.core.config import get_settings

        settings = get_settings()
    from kaiba.intelligence.dyor_verify import PUBLIC_RPC

    urls: list[Any] = []
    configured = str(settings.rpc_for(chain) or "").strip()
    if chain is Chain.BSC:
        configured = str(getattr(settings, "bsc_rpc_url", "") or "").strip() or configured
    # exactly evm_price._rpc_url / dyor_verify._rpc_url: the configured URL, else the public one
    urls.append(configured or PUBLIC_RPC.get(chain))
    if chain is Chain.ROBINHOOD:
        try:
            from kaiba.ingest import robinhood

            urls.append(robinhood.RPC_URL)
        except Exception as exc:  # noqa: BLE001 - the settings URL still binds
            log.debug("feed_endpoints: robinhood.RPC_URL unreadable (%s)", type(exc).__name__)
    return frozenset(h for h in (host_of(u) for u in urls) if h)


def capped_body(text: Any) -> bool:
    """A 429 body that means a spent plan ("capacity" / "usage"), not a burst limit."""
    low = str(text or "").lower()
    return "capacity" in low or "usage" in low


# --------------------------------------------------------------------------------------
# the result types
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, repr=False)
class FeedEndpoint:
    """Where a feed connects. ``ws_url`` / ``http_url`` may carry a key: never log them."""

    feed: str
    chain: Chain
    kind: str
    ws_url: str
    http_url: str | None
    ws_host: str
    http_host: str | None
    #: The websocket host is one of protection's (bsc only, with ENV_SHARED_WS_OK).
    shared_ws: bool = False
    #: Which variable supplied it (a name, never a value).
    source: str = ""

    @property
    def host(self) -> str:
        return self.http_host or self.ws_host

    @property
    def keyed(self) -> bool:
        return self.kind in ALCHEMY_KINDS

    def describe(self) -> dict[str, Any]:
        """Hosts and kind only: what may go into an event, a log or a status row."""
        return {"feed": self.feed, "chain": self.chain.value, "kind": self.kind, "ws": masked(self.ws_url),
                "http": masked(self.http_url), "shared_ws": self.shared_ws, "source": self.source}

    def __repr__(self) -> str:
        return f"FeedEndpoint({self.describe()})"


@dataclass(frozen=True)
class Refusal:
    """No endpoint. ``reason`` is host-only text, safe for ``ingest_status.last_error``."""

    feed: str
    chain: Chain
    reason: str

    def as_idle(self) -> dict[str, str]:
        return {"idle": self.reason}


@dataclass(frozen=True)
class _Candidate:
    kind: str
    ws_url: str | None
    http_url: str | None
    source: str
    #: Why the slot has no usable URL (a reason instead of a URL).
    missing: str | None = None


# --------------------------------------------------------------------------------------
# the capacity probe and its cache
# --------------------------------------------------------------------------------------


class _ProbeError(RuntimeError):
    """A probe outcome. The message never carries the URL (``guarded`` stores it)."""

    def __init__(self, message: str, *, status_code: int | None = None, capped: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.capped = capped


def probe_capacity(chain: Chain, http_url: str, *, timeout_s: float = PROBE_TIMEOUT_S) -> str:
    """``"ok"`` | ``"capped"`` | ``"unknown"`` for a keyed endpoint, from ONE cheap call.

    Through ``guarded`` on :data:`PROBE_BUCKET` (its own bucket, never ``rpc``) so the call
    is counted in ``provider_calls``. Every error raised inside is our own text without the
    URL, because ``guarded`` records it as the row's ``detail``.
    """
    from kaiba.providers.provider_policy import refusal

    if refusal(http_url):
        return VERDICT_UNKNOWN  # policy refusal is not a measured quota verdict
    method = "getHealth" if chain is Chain.SOL else "eth_chainId"
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": []}
    try:
        import httpx

        with guarded(PROBE_BUCKET, f"probe.{chain.value}", Priority.RESEARCH):
            try:
                resp = httpx.post(http_url, json=body, timeout=timeout_s)
            except Exception as exc:  # noqa: BLE001 - the type is the whole story
                raise _ProbeError(f"{method}: {type(exc).__name__}") from None
            status = int(getattr(resp, "status_code", 0) or 0)
            if status == 429:
                try:
                    text = str(resp.text or "")[:4096]
                except Exception:  # noqa: BLE001
                    text = ""
                capped = capped_body(text)
                raise _ProbeError(f"{method}: 429 {'monthly cap' if capped else 'rate limited'}",
                                  status_code=429, capped=capped)
            if status >= 400:
                raise _ProbeError(f"{method}: http {status}", status_code=status)
    except RateLimited:
        return VERDICT_UNKNOWN  # our own limiter refused the probe: no evidence either way
    except _ProbeError as exc:
        return VERDICT_CAPPED if exc.capped else VERDICT_UNKNOWN
    except Exception as exc:  # noqa: BLE001 - a broken probe is "unknown", never "capped"
        log.debug("feed_endpoints: capacity probe failed (%s)", type(exc).__name__)
        return VERDICT_UNKNOWN
    return VERDICT_OK


def cached_verdict(chain: Chain, host: str | None, *, conn: Any = None, at_ms: int | None = None) -> str | None:
    """The cached probe verdict for ``chain`` when it is for ``host`` and under 6 h old."""
    if not host:
        return None
    try:
        c = conn if conn is not None else _db.get_conn()
        row = c.execute("SELECT value FROM kv WHERE key=?", (CACHE_KEY.format(chain=chain.value),)).fetchone()
    except Exception as exc:  # noqa: BLE001 - no cache is a probe, not a failure
        log.debug("feed_endpoints: verdict cache unreadable (%s)", type(exc).__name__)
        return None
    if row is None:
        return None
    from kaiba.core.db import jload

    value = jload(row[0], None)
    if not isinstance(value, Mapping) or value.get("host") != host:
        return None
    at = int(at_ms if at_ms is not None else now_ms())
    try:
        age = at - int(value.get("at_ms") or 0)
    except (TypeError, ValueError):
        return None
    verdict = value.get("verdict")
    return str(verdict) if 0 <= age < CACHE_TTL_MS and verdict in (VERDICT_OK, VERDICT_CAPPED) else None


def save_verdict(chain: Chain, host: str | None, verdict: str, *, conn: Any = None, at_ms: int | None = None) -> None:
    """Cache ``verdict`` for ``chain``. Stores the HOST only, never the URL or the key."""
    if not host or verdict not in (VERDICT_OK, VERDICT_CAPPED):
        return
    from kaiba.core.db import jdump

    at = int(at_ms if at_ms is not None else now_ms())
    try:
        c = conn if conn is not None else _db.get_conn()
        c.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
                  "value=excluded.value, updated_ms=excluded.updated_ms",
                  (CACHE_KEY.format(chain=chain.value), jdump({"verdict": verdict, "host": host, "at_ms": at}), at))
    except Exception as exc:  # noqa: BLE001 - an uncached verdict only costs a probe
        log.debug("feed_endpoints: verdict not cached (%s)", type(exc).__name__)


Probe = Callable[[Chain, str], str]


def _capacity(chain: Chain, http_url: str | None, *, conn: Any, probe: Probe | None, at_ms: int | None) -> str:
    host = host_of(http_url)
    if not http_url or not host:
        return VERDICT_UNKNOWN
    hit = cached_verdict(chain, host, conn=conn, at_ms=at_ms)
    if hit is not None:
        return hit
    verdict = (probe or probe_capacity)(chain, http_url)
    save_verdict(chain, host, verdict, conn=conn, at_ms=at_ms)
    return verdict


# --------------------------------------------------------------------------------------
# the resolver
# --------------------------------------------------------------------------------------


def _keyed_candidates(chain: Chain, env: Mapping[str, str] | None, settings: Any) -> list[_Candidate]:
    out: list[_Candidate] = []
    if chain is Chain.BSC:
        if env is not None:
            snipe = env_value(ENV_BSC_SNIPE_RPC, env)
        else:
            from kaiba.ingest import launch_feed as lf

            snipe = lf.bsc_snipe_rpc_url()
        if snipe and "/v2/" in snipe:
            out.append(_Candidate(KIND_ALCHEMY, snipe, snipe, ENV_BSC_SNIPE_RPC))
        # No shared slot on bsc: flap never used BSC_RPC_URL, whose key protection's reads use.
        return out
    own_env = {Chain.SOL: ENV_SOL_FEED_ALCHEMY, Chain.ROBINHOOD: ENV_RH_FEED_ALCHEMY}.get(chain)
    own = env_value(own_env, env) if own_env else None
    if own:
        out.append(_Candidate(KIND_ALCHEMY, own, own, own_env or ""))
    legacy = str(settings.rpc_for(chain) or "").strip()
    if legacy and "/v2/" in legacy and legacy != own:
        name = {Chain.SOL: "SOLANA_RPC_URL", Chain.ROBINHOOD: "ROBINHOOD_RPC_URL"}.get(chain, "rpc_for")
        out.append(_Candidate(KIND_ALCHEMY_SHARED, legacy, legacy, name))
    return out


def _public_candidate(chain: Chain, env: Mapping[str, str] | None) -> _Candidate:
    if chain is Chain.SOL:
        ws = env_value(ENV_SOL_FEED_WS, env) or SOL_PUBLIC_WS_DEFAULT
        http = env_value(ENV_SOL_FEED_HTTP, env) or SOL_PUBLIC_HTTP_DEFAULT
        return _Candidate(KIND_PUBLIC, ws, http, f"{ENV_SOL_FEED_WS}/{ENV_SOL_FEED_HTTP}")
    if chain is Chain.BSC:
        ws = env_value(ENV_BSC_SNIPE_WS, env)
        if env is not None:
            http = env_value(ENV_BSC_SNIPE_RPC, env)
        else:
            from kaiba.ingest import launch_feed as lf

            http = lf.bsc_snipe_rpc_url()
        if http and "/v2/" in http:
            http = None  # that is the keyed slot; the public slot is socket-only then
        if not ws:
            return _Candidate(KIND_PUBLIC, None, http, ENV_BSC_SNIPE_WS, missing=f"{ENV_BSC_SNIPE_WS} unset")
        return _Candidate(KIND_PUBLIC, ws, http, f"{ENV_BSC_SNIPE_WS}/{ENV_BSC_SNIPE_RPC}")
    if chain is Chain.ROBINHOOD:
        return _Candidate(KIND_PUBLIC, None, None, "", missing="no public websocket for robinhood (owner decision)")
    return _Candidate(KIND_PUBLIC, None, None, "", missing=f"no public endpoint for {chain.value}")


def _check(feed: str, chain: Chain, cand: _Candidate, guard: frozenset[str], *, allow_shared: bool,
           shared_ws_ok: bool) -> FeedEndpoint | str:
    """The candidate as an endpoint, or the host-only reason it may not be used."""
    if cand.missing:
        return cand.missing
    from kaiba.providers.provider_policy import refusal

    for url in (cand.ws_url, cand.http_url):
        reason = refusal(str(url or ""))
        if reason:
            return reason
    if cand.kind == KIND_ALCHEMY_SHARED and not allow_shared:
        return f"{cand.source} shares protection's key; set {ENV_ALLOW_SHARED_ALCHEMY}=1 to allow"
    try:
        ws = aws.ws_url(str(cand.ws_url or ""))
    except ValueError:
        return f"{cand.source}: not a websocket-capable URL"
    ws_host = host_of(ws)
    if not ws_host:
        return f"{cand.source}: no host"
    http = cand.http_url or None
    http_host = host_of(http) if http else None
    if http and not http_host:
        return f"{cand.source}: http URL has no host"
    if http is None and feed in HTTP_REQUIRED:
        return "no http endpoint"
    if ws_host in SILENT_WS_HOSTS.get(chain, frozenset()):
        return f"ws host {ws_host} accepts subscriptions and delivers nothing (measured 2026-10-21)"
    shared_ok = cand.kind in ALCHEMY_KINDS and allow_shared
    if http_host and http_host in guard and not shared_ok:
        return f"http host == protection host {http_host}"
    shared_ws = False
    if ws_host in guard:
        if shared_ok:
            shared_ws = True
        elif chain in SHARED_WS_CHAINS and shared_ws_ok:
            shared_ws = True
        elif chain in SHARED_WS_CHAINS:
            return f"ws host == protection host {ws_host}; set {ENV_SHARED_WS_OK}={chain.value} to allow"
        else:
            return f"ws host == protection host {ws_host}"
    return FeedEndpoint(feed=feed, chain=chain, kind=cand.kind, ws_url=ws, http_url=http, ws_host=ws_host,
                        http_host=http_host, shared_ws=shared_ws, source=cand.source)


def resolve(feed: str, chain: Chain | None = None, *, env: Mapping[str, str] | None = None, settings: Any = None,
            conn: Any = None, probe: Probe | None = None, at_ms: int | None = None) -> FeedEndpoint | Refusal:
    """The endpoint ``feed`` may use now, or why there is none. Never raises.

    ``env`` / ``settings`` / ``probe`` are injectable for tests; production reads the live
    environment, ``.env`` files and settings on every call (the protection hosts included).
    A keyed slot costs at most one probe call per chain per 6 h (the kv cache).
    """
    chain = chain or FEED_CHAINS.get(feed) or Chain.SOL
    try:
        if settings is None:
            from kaiba.core.config import get_settings

            settings = get_settings()
        guard = protection_hosts(chain, settings)
        fallback_on = feed.lower() in listed(env_value(ENV_PUBLIC_FALLBACK, env))
        allow_shared = _truthy(env_value(ENV_ALLOW_SHARED_ALCHEMY, env))
        shared_ws_ok = chain in SHARED_WS_CHAINS and chain.value in listed(env_value(ENV_SHARED_WS_OK, env))
        prefer_public = chain.value in listed(env_value(ENV_PREFER_PUBLIC, env))
        keyed = _keyed_candidates(chain, env, settings)
        public = _public_candidate(chain, env) if fallback_on else None
    except Exception as exc:  # noqa: BLE001 - an unreadable config is a refusal, not a crash
        log.warning("feed_endpoints: %s: configuration unreadable (%s); idle", feed, type(exc).__name__)
        return Refusal(feed, chain, f"endpoint configuration unreadable ({type(exc).__name__})")
    if not keyed and public is None:
        return Refusal(feed, chain, TODAY_IDLE.get(feed, "no websocket endpoint"))
    order: list[_Candidate] = [*keyed]
    if public is not None:
        order = [public, *keyed] if prefer_public else [*keyed, public]
    reasons: list[str] = []
    for cand in order:
        got = _check(feed, chain, cand, guard, allow_shared=allow_shared, shared_ws_ok=shared_ws_ok)
        if isinstance(got, str):
            reasons.append(f"{cand.kind}: {got}")
            continue
        if got.keyed:
            verdict = _capacity(chain, got.http_url, conn=conn, probe=probe, at_ms=at_ms)
            if verdict == VERDICT_CAPPED:
                reasons.append(f"{cand.kind}: {got.http_host} at its monthly cap (probe, cached 6 h)")
                continue
        log.info("feed_endpoints: %s -> %s ws=%s http=%s%s", feed, got.kind, masked(got.ws_url),
                 masked(got.http_url), " (ws shared with protection)" if got.shared_ws else "")
        return got
    if public is None:
        reasons.append(f"public: {feed} not in {ENV_PUBLIC_FALLBACK}")
    reason = "; ".join(reasons)[:300]
    log.warning("feed_endpoints: %s: no usable endpoint (%s)", feed, reason)
    return Refusal(feed, chain, reason)


async def resolve_async(feed: str, chain: Chain | None = None, **kw: Any) -> FeedEndpoint | Refusal:
    """:func:`resolve` in a worker thread: a probe may take up to 5 s, and the ingest
    process runs every listener (the PumpPortal socket among them) on one event loop."""
    return await asyncio.to_thread(resolve, feed, chain, **kw)


# --------------------------------------------------------------------------------------
# mid-run: the monthly cap
# --------------------------------------------------------------------------------------


def looks_throttled(text: Any) -> bool:
    """A failure text that could be the provider's 429 (ours say "rate limited" too)."""
    low = str(text or "").lower()
    return "429" in low


class CapWatch:
    """Notices a keyed endpoint hitting its monthly cap while a feed runs.

    A 429 through ``post_json`` carries no body, so it cannot say "monthly cap" by itself.
    On a 429 (at most every :data:`CAP_PROBE_MIN_GAP_S`) this re-runs the capacity probe,
    which reads the body. A capped verdict is cached (so the next resolve skips the key)
    and :attr:`capped` turns True; the feed then returns
    ``{"fallback": FALLBACK_MONTHLY_CAP}`` and the runner restarts it. Inactive (never
    probes) for a public endpoint, which has no plan to run out of.
    """

    def __init__(self, ep: FeedEndpoint | None, *, probe: Probe | None = None, conn: Any = None,
                 min_gap_s: float = CAP_PROBE_MIN_GAP_S, clock: Callable[[], float] = time.monotonic,
                 on_capped: Iterable[Callable[[], None]] = ()) -> None:
        self.ep = ep
        self.active = bool(ep is not None and ep.keyed and ep.http_url)
        self.probe, self.conn = probe, conn
        self.min_gap_s, self.clock = float(min_gap_s), clock
        self.capped = False
        self.probes = 0
        self._last: float | None = None
        self._callbacks: list[Callable[[], None]] = list(on_capped)

    def on_capped(self, fn: Callable[[], None]) -> None:
        self._callbacks.append(fn)

    def fallback(self) -> dict[str, str]:
        return {"fallback": FALLBACK_MONTHLY_CAP}

    async def check(self, text: Any) -> bool:
        """Note one failure; True once the key is known to be capped. Never raises."""
        if self.capped or not self.active or not looks_throttled(text):
            return self.capped
        now = self.clock()
        if self._last is not None and now - self._last < self.min_gap_s:
            return False
        self._last = now
        assert self.ep is not None and self.ep.http_url
        self.probes += 1
        try:
            verdict = await asyncio.to_thread(self.probe or probe_capacity, self.ep.chain, self.ep.http_url)
        except Exception as exc:  # noqa: BLE001 - an unprobed key keeps running
            log.debug("feed_endpoints: mid-run probe failed (%s)", type(exc).__name__)
            return False
        if verdict != VERDICT_CAPPED:
            return False
        await asyncio.to_thread(save_verdict, self.ep.chain, self.ep.http_host, VERDICT_CAPPED, conn=self.conn)
        self.capped = True
        log.warning("feed_endpoints: %s: %s is at its monthly cap; falling back", self.ep.feed, self.ep.http_host)
        for fn in self._callbacks:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - a callback never breaks the check
                log.debug("feed_endpoints: cap callback failed (%s)", type(exc).__name__)
        return True


__all__ = (
    "ALCHEMY_KINDS", "CACHE_KEY", "CACHE_TTL_MS", "CapWatch", "ENV_ALLOW_SHARED_ALCHEMY", "ENV_BSC_SNIPE_RPC",
    "ENV_BSC_SNIPE_WS", "ENV_PREFER_PUBLIC", "ENV_PUBLIC_FALLBACK", "ENV_RH_FEED_ALCHEMY", "ENV_SHARED_WS_OK",
    "ENV_SOL_FEED_ALCHEMY", "ENV_SOL_FEED_HTTP", "ENV_SOL_FEED_WS", "FALLBACK_MONTHLY_CAP", "FEED_CHAINS",
    "FeedEndpoint", "KIND_ALCHEMY", "KIND_ALCHEMY_SHARED", "KIND_PUBLIC", "PROBE_BUCKET", "Refusal",
    "SILENT_WS_HOSTS", "SOL_PUBLIC_HTTP_DEFAULT", "SOL_PUBLIC_WS_DEFAULT", "TODAY_IDLE", "cached_verdict",
    "capped_body", "env_value", "host_of", "listed", "looks_throttled", "masked", "probe_capacity",
    "protection_hosts", "resolve", "resolve_async", "save_verdict",
)
