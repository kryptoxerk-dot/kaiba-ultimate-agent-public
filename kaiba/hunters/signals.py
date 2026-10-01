"""Early-alpha detection: free, unauthenticated sources that leak days of lead time.

Why this exists at all. The edge review (`docs/EDGE-AND-VARIABLES.md`) concludes that speed
is closed to us and prediction is very weak, and that the one defensible edge left is
"exclusion plus discipline plus **lead time**". Lead time is the only one of the three that
is bought with HTTP requests rather than infrastructure. A certificate for
`claim.<project>` is issued days before the claim page is announced; a Hyperliquid
pre-launch market exists before the token does; an exchange publishes a listing notice
before the listing opens. None of that requires being fast and none of it requires being
right about which token moons.

**This points at the trading book, not a farming book.** Knowing a claim page went live
three days early is a pre-event trade. It is not a reason to farm twenty wallets; the same
review records that airdrop farming resolved against farmers over six months.

Five source families, every one verified live and free:

1. ``crtsh``      - certificate transparency. ~3-4 days lead, near 100% precision,
                    55-60% recall. crt.sh is the flakiest endpoint here: it returns
                    intermittent 502s and it times out, so it gets real retries and a long
                    timeout, and a failure is recorded rather than swallowed.
2. ``binance`` / ``okx`` / ``upbit`` - venue listing announcements. OKX is the documented
                    public API and therefore the stable one; Binance's is an undocumented
                    internal CMS route and is treated as liable to change without notice.
3. ``snapshot`` / ``discourse`` - governance. New Snapshot spaces and DAO forum threads.
4. ``github``     - per-repo Atom feeds, which cost **no** API rate limit because they are
                    CDN-cached, unlike the Events API at 60/hour unauthenticated.
5. ``hyperliquid`` / ``aevo`` - pre-launch perp markets, detected by diffing the asset list.

Three properties this module is built around, in order of how badly their absence hurts:

* **A dead source is loud.** ``alpha_source_health`` records last-success per source and
  :func:`check_health` emits when one has been failing longer than its tolerance. A quiet
  detector is indistinguishable from a quiet market, so silence is never evidence.
* **A diff source needs a baseline first.** The first poll of Hyperliquid or Aevo records
  the asset list and reports *nothing*. Otherwise day one produces 200 "new pre-launch
  markets", every one of them a lie, and the operator learns to ignore the feed.
* **Nothing raises.** A source being down is data. Every poller returns a list, possibly
  empty, and writes why it was empty.

Operator configuration lives in ``config/signals.yaml`` (optional; the defaults below are
used when it is absent). Shape::

    domains:   [scroll.io, monad.xyz]     # certificate transparency watchlist
    repos:     [ethereum-optimism/optimism]
    forums:    [gov.uniswap.org]
    intervals: {crtsh: 3600, okx: 120}
    cert_max_age_days: 30
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump, upsert
from kaiba.core.events import emit
from kaiba.core.limiter import Priority, RateLimited, guarded
from kaiba.core.schemas import Chain, EventKind, digest, now_ms
from kaiba.providers._http import (
    Fetched,
    cache_read,
    cache_write,
    get_json,
    post_json,
    redact_text,
)

log = logging.getLogger(__name__)

USER_AGENT = "kaiba-signals/1.0 (+operator-contact-only)"
MAX_TEXT_CHARS = 4_000_000

# --------------------------------------------------------------------------- endpoints

CRTSH_URL = "https://crt.sh/"
BINANCE_URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
BINANCE_PARAMS: dict[str, Any] = {"type": 1, "catalogId": 48, "pageNo": 1, "pageSize": 20}
OKX_URL = "https://www.okx.com/api/v5/support/announcements"
OKX_PARAMS: dict[str, Any] = {"annType": "announcements-new-listings"}
#: ``category`` is required; without it the endpoint answers 400.
UPBIT_URL = "https://api-manager.upbit.com/api/v1/announcements"
UPBIT_PARAMS: dict[str, Any] = {"os": "web", "page": 1, "per_page": 20, "category": "trade"}
SNAPSHOT_URL = "https://hub.snapshot.org/graphql"
HYPERLIQUID_URL = "https://api.hyperliquid.xyz/info"
AEVO_URL = "https://api.aevo.xyz/assets"

SNAPSHOT_QUERY = (
    "query NewSpaces($first: Int!) { spaces(first: $first, orderBy: \"created\", "
    "orderDirection: desc) { id name created followersCount network symbol about } }"
)

# ------------------------------------------------------------------------ defaults

#: Projects worth watching for a claim subdomain. Operator-configurable; this list is a
#: starting point, not a thesis. Adding a domain costs one HTTP request per poll.
DEFAULT_DOMAINS: tuple[str, ...] = (
    "scroll.io",
    "monad.xyz",
    "linea.build",
    "eclipse.xyz",
    "berachain.com",
    "movementlabs.xyz",
    "megaeth.com",
    "abs.xyz",
    "fogo.io",
    "hyperliquid.xyz",
)

DEFAULT_REPOS: tuple[str, ...] = (
    "ethereum-optimism/optimism",
    "scroll-tech/scroll",
    "monad-crypto/monad",
    "Uniswap/governance",
)

DEFAULT_FORUMS: tuple[str, ...] = (
    "gov.uniswap.org",
    "forum.arbitrum.foundation",
    "gov.optimism.io",
    "governance.aave.com",
)

#: Seconds between polls. crt.sh is hourly on purpose: the lead it gives is measured in
#: days, so a faster poll buys nothing and only makes a fragile endpoint angrier.
DEFAULT_INTERVALS: dict[str, int] = {
    "crtsh": 3600,
    "binance": 180,
    "okx": 180,
    "upbit": 180,
    "snapshot": 900,
    "discourse": 900,
    "github": 900,
    "hyperliquid": 300,
    "aevo": 300,
}

#: A source that has not succeeded in this long is reported dead. Six hours is the floor
#: the operator asked for; slow sources get six of their own intervals instead.
DEAD_AFTER_FLOOR_S = 6 * 3600


class SignalsConfig(BaseModel):
    """Operator knobs. Absent file means defaults; a broken file means defaults plus a warning."""

    domains: list[str] = Field(default_factory=lambda: list(DEFAULT_DOMAINS))
    repos: list[str] = Field(default_factory=lambda: list(DEFAULT_REPOS))
    forums: list[str] = Field(default_factory=lambda: list(DEFAULT_FORUMS))
    intervals: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_INTERVALS))
    #: Certificates older than this are history, not news. Without this bound the first
    #: poll of a long-lived domain would record every claim subdomain it ever had as a
    #: fresh signal, all stamped with today's date.
    cert_max_age_days: int = 30
    snapshot_spaces: int = 25
    max_items_per_endpoint: int = 60

    def interval_for(self, source: str) -> int:
        return int(self.intervals.get(source, DEFAULT_INTERVALS.get(source, 600)))

    def dead_after_s(self, source: str) -> int:
        return max(DEAD_AFTER_FLOOR_S, 6 * self.interval_for(source))


def config_path() -> Path:
    from kaiba.core.config import REPO_ROOT

    override = os.environ.get("KAIBA_SIGNALS_CONFIG")
    return Path(override) if override else REPO_ROOT / "config" / "signals.yaml"


def load_config(path: Path | None = None) -> SignalsConfig:
    """Not cached: the operator edits the watchlist while the poller is running."""
    p = path or config_path()
    try:
        if not p.exists():
            return SignalsConfig()
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("top level of signals.yaml must be a mapping")
        return SignalsConfig(**raw)
    except Exception as exc:  # noqa: BLE001 - a bad config must not stop detection
        log.warning("signals config %s unusable, using defaults: %s", p, exc)
        return SignalsConfig()


# ------------------------------------------------------------------------- the signal


class SignalKind:
    """String constants rather than an enum: these are written to SQL and read by SQL."""

    CERT_SUBDOMAIN = "cert_subdomain"
    VENUE_LISTING = "venue_listing"
    GOV_SPACE = "gov_space"
    GOV_TOPIC = "gov_topic"
    REPO_RELEASE = "repo_release"
    REPO_COMMIT = "repo_commit"
    PRELAUNCH_MARKET = "prelaunch_market"


#: Prior precision per kind, from the research digest where one exists and from a
#: deliberately pessimistic guess where none does. These are priors, not measurements;
#: the weekly report prints them next to the counts so the guesses stay visible.
CONFIDENCE: dict[str, float] = {
    "cert_subdomain_strong": 0.90,
    "cert_subdomain_weak": 0.45,
    SignalKind.VENUE_LISTING: 0.90,
    "venue_delisting": 0.80,
    SignalKind.GOV_SPACE: 0.15,
    SignalKind.GOV_TOPIC: 0.30,
    SignalKind.REPO_RELEASE: 0.35,
    SignalKind.REPO_COMMIT: 0.30,
    SignalKind.PRELAUNCH_MARKET: 0.80,
}

#: Measured median lead from certificate issuance to public announcement, from the two
#: reproductions in the research digest (scroll.io 3d, monad.xyz 4d). Two data points is
#: not a distribution, which is exactly why `lead_basis` says where the number came from.
CERT_PRIOR_LEAD_MS = int(3.5 * 86_400_000)


class AlphaSignal(BaseModel):
    """One pre-event observation. ``lead_ms is None`` means unknowable, never "on time"."""

    source: str
    kind: str
    subject: str
    title: str = ""
    url: str | None = None
    chain: Chain | None = None
    event_at_ms: int | None = None
    first_seen_ms: int = Field(default_factory=now_ms)
    lead_ms: int | None = None
    lead_basis: str = "not_estimable"
    confidence: float = 0.0
    #: What makes this signal unique. Defaults to the subject; a source whose subject can
    #: legitimately recur (a ticker listed on two venues) sets something narrower.
    key_seed: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def signal_key(self) -> str:
        return digest({"s": self.source, "k": self.kind, "x": self.key_seed or self.subject})[:32]

    @property
    def detect_lag_ms(self) -> int | None:
        """How late we were to something the source timestamped. ``None`` when it did not."""
        if self.event_at_ms is None:
            return None
        return self.first_seen_ms - self.event_at_ms


# ------------------------------------------------------------------------ http helpers


def _provider_error(source: str, endpoint: str, detail: str, conn: sqlite3.Connection | None = None,
                    **extra: Any) -> None:
    safe = redact_text(detail)[:300]
    log.warning("signal source %s %s: %s", source, endpoint, safe)
    emit(
        EventKind.PROVIDER_ERROR,
        {"provider": source, "endpoint": endpoint, "detail": safe, **extra},
        level="warn",
        dedupe_key=f"signals:{source}:{endpoint}:{safe[:60]}:{now_ms() // 600_000}",
        conn=conn,
    )


def fetch_text(
    source: str,
    endpoint: str,
    url: str,
    *,
    ttl_s: float = 0.0,
    timeout_s: float = 20.0,
    retries: int = 2,
    wait_for_slot_s: float = 10.0,
    conn: sqlite3.Connection | None = None,
) -> Fetched:
    """Text sibling of :func:`kaiba.providers._http.get_json`, for the Atom feeds.

    ``_http`` only speaks JSON - it parses every body with ``json.loads`` and treats a
    failure to parse as the provider being down - and GitHub's release/commit feeds are
    XML. The JSON alternative is the Events API at 60 requests an hour unauthenticated,
    while the Atom feeds are CDN-cached and cost no rate limit at all, so the feeds win.

    Everything that can be shared with ``_http`` is shared: its cache, its ``Fetched``
    envelope, its redaction, and the same wait-for-a-limiter-slot retry semantics that
    two earlier adapters got wrong. Only the parse differs. **This belongs in
    ``_http.request_text`` and should move there** as soon as that file is being edited
    for another reason; it lives here because this change does not own that file.
    """
    from kaiba.core.schemas import EvidenceBasis, Receipt

    key = f"GET:{url}"
    req_digest = digest(key)
    if ttl_s > 0:
        hit = cache_read(source, key, ttl_s)
        if hit is not None:
            data, is_stale, fetched_ms = hit
            return Fetched(
                data,
                Receipt(
                    provider=source,
                    endpoint=endpoint,
                    observed_at_ms=fetched_ms,
                    basis=EvidenceBasis.STALE if is_stale else EvidenceBasis.CACHED,
                    request_digest=req_digest,
                ),
            )
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - httpx is a hard dependency
        note = f"httpx missing: {exc}"
        return Fetched(None, Receipt(provider=source, endpoint=endpoint,
                                     basis=EvidenceBasis.UNAVAILABLE, note=note))

    deadline = time.monotonic() + wait_for_slot_s if wait_for_slot_s > 0 else 0.0
    note = "no attempt made"
    attempt = -1
    while True:
        attempt += 1
        if attempt >= max(1, retries) and time.monotonic() >= deadline:
            break
        try:
            with guarded(source, endpoint, Priority.RESEARCH, conn=conn):
                resp = httpx.get(url, timeout=timeout_s, follow_redirects=True,
                                 headers={"user-agent": USER_AGENT})
                resp.raise_for_status()
                body = resp.text[:MAX_TEXT_CHARS]
        except RateLimited as exc:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(remaining, max(0.05, float(getattr(exc, "retry_after_s", 0.25) or 0.25))))
                continue
            note = redact_text(f"rate limited: {exc}")[:300]
            break
        except Exception as exc:  # noqa: BLE001 - a feed being down is data, not a crash
            note = redact_text(f"{type(exc).__name__}: {exc}")[:300]
            if attempt + 1 < max(1, retries):
                time.sleep(min(2.0, 0.25 * (2**attempt)))
                continue
            break
        else:
            if ttl_s > 0:
                cache_write(source, key, body)
            return Fetched(
                body,
                Receipt(provider=source, endpoint=endpoint,
                        basis=EvidenceBasis.PROVIDER_REPORTED, request_digest=req_digest),
            )
    return Fetched(None, Receipt(provider=source, endpoint=endpoint,
                                 basis=EvidenceBasis.UNAVAILABLE, request_digest=req_digest,
                                 note=note))


def _iso_ms(value: str | None) -> int | None:
    """ISO-8601, with or without ``Z`` and with or without an offset. Naive means UTC."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _int_ms(value: Any) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    #: Some venues publish seconds, some milliseconds. Anything under 10^11 is seconds.
    return n * 1000 if n < 100_000_000_000 else n


# -------------------------------------------------------------- 1. certificate transparency

STRONG_CERT_LABELS = frozenset(
    {"claim", "claims", "airdrop", "airdrops", "drop", "tge", "merkle", "allocation", "distributor"}
)
WEAK_CERT_LABELS = frozenset(
    {"rewards", "reward", "distribution", "eligibility", "checker", "points", "genesis",
     "presale", "sale", "unlock", "vesting", "token"}
)


def classify_subdomain(fqdn: str, domain: str) -> tuple[str, float] | None:
    """``claim.scroll.io`` -> ``("strong", 0.90)``. ``None`` when nothing interesting."""
    name = (fqdn or "").strip().lower().rstrip(".")
    base = (domain or "").strip().lower()
    # The dot is load-bearing: a bare ``endswith(base)`` accepted `claim.notscroll.io`
    # for a watchlist entry of `scroll.io`, which is how a lookalike domain registered by
    # someone else gets reported as the real project's claim page going live.
    if not name or name.startswith("*") or not base or not name.endswith("." + base):
        return None
    prefix = name[: -(len(base) + 1)]
    labels = {part for part in prefix.split(".") if part}
    if labels & STRONG_CERT_LABELS:
        return "strong", CONFIDENCE["cert_subdomain_strong"]
    if labels & WEAK_CERT_LABELS:
        return "weak", CONFIDENCE["cert_subdomain_weak"]
    return None


def parse_crtsh(rows: Any, domain: str, *, max_age_days: int = 30,
                now: int | None = None) -> list[AlphaSignal]:
    """One signal per *newly issued* interesting subdomain certificate.

    Certificates older than ``max_age_days`` are dropped before anything else happens.
    They are history: `claim.scroll.io` has had a certificate since 2024 and re-reporting
    it today as a fresh signal would be a lie stamped with today's timestamp.
    """
    seen_ms = now if now is not None else now_ms()
    cutoff = seen_ms - max_age_days * 86_400_000
    out: dict[str, AlphaSignal] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        issued = _iso_ms(row.get("not_before"))
        if issued is None or issued < cutoff or issued > seen_ms + 86_400_000:
            continue
        names = str(row.get("name_value") or row.get("common_name") or "").split("\n")
        for raw_name in names:
            verdict = classify_subdomain(raw_name, domain)
            if verdict is None:
                continue
            strength, confidence = verdict
            fqdn = raw_name.strip().lower()
            lag = seen_ms - issued
            lead: int | None = CERT_PRIOR_LEAD_MS - lag
            basis = "prior_3.5d_median_minus_cert_age"
            if lead is not None and lead <= 0:
                lead, basis = None, "cert_older_than_prior_lead_window"
            existing = out.get(fqdn)
            if existing is not None and (existing.event_at_ms or 0) <= issued:
                continue
            out[fqdn] = AlphaSignal(
                source="crtsh",
                kind=SignalKind.CERT_SUBDOMAIN,
                subject=fqdn,
                title=f"certificate issued for {fqdn}",
                url=f"https://crt.sh/?q={fqdn}",
                event_at_ms=issued,
                first_seen_ms=seen_ms,
                lead_ms=lead,
                lead_basis=basis,
                confidence=confidence,
                key_seed=fqdn,
                payload={
                    "domain": domain,
                    "strength": strength,
                    "issuer": str(row.get("issuer_name") or "")[:160],
                    "not_before": row.get("not_before"),
                    "crtsh_id": row.get("id"),
                },
            )
    return sorted(out.values(), key=lambda s: -(s.event_at_ms or 0))


def poll_certificate_transparency(
    conn: sqlite3.Connection | None = None,
    *,
    cfg: SignalsConfig | None = None,
    raw: dict[str, Any] | None = None,
) -> tuple[list[AlphaSignal], int, int, str | None]:
    """Returns ``(signals, endpoints_ok, endpoints_total, last_error)``.

    crt.sh 502s intermittently and timed out on one of two domains during verification, so
    retries are mandatory here rather than optional, and a domain that fails is counted as
    a failed endpoint rather than as "no claim subdomains found".
    """
    config = cfg or load_config()
    domains = list(raw) if raw is not None else list(config.domains)
    found: list[AlphaSignal] = []
    ok = 0
    err: str | None = None
    for domain in domains:
        if raw is not None:
            payload: Any = raw[domain]
        else:
            got = get_json(
                "crtsh",
                "crt.search",
                CRTSH_URL,
                params={"q": f"%.{domain}", "output": "json"},
                timeout_s=45.0,
                retries=3,
                ttl_s=600,
                stale_grace_s=3600,
                wait_for_slot_s=30.0,
                conn=conn,
            )
            if not got.ok:
                err = f"{domain}: {got.receipt.note or 'unavailable'}"
                continue
            payload = got.data
        ok += 1
        try:
            found.extend(parse_crtsh(payload, domain, max_age_days=config.cert_max_age_days))
        except Exception as exc:  # noqa: BLE001 - a layout change is not an outage
            err = f"{domain}: parse failed: {type(exc).__name__}: {exc}"
            _provider_error("crtsh", "crt.search", err, conn=conn)
    return found, ok, len(domains), err


# ------------------------------------------------------------------- 2. venue listings

_LISTING_RE = re.compile(
    r"(will list|will launch|to list|new listing|listing of|market support|spot trading|"
    r"perpetual|launchpool|seed tag|상장|거래지원|거래 지원|신규 거래)",
    re.I,
)
_DELISTING_RE = re.compile(r"(delist|removal of|will remove|종료|상장 ?폐지|거래 ?종료)", re.I)
_TICKER_RE = re.compile(r"\(([A-Z0-9]{2,12})\)")
_QUOTE_SYMBOLS = frozenset({"USDT", "USDC", "KRW", "BTC", "ETH", "BNB", "USD", "FDUSD", "EUR", "TRY"})
#: OKX writes the *project name* in parentheses and the ticker in the pair, as in
#: "OKX to list VVV/USDT (Venice) for spot trading". Reading only the parentheses there
#: gives you "Venice", which is not tradeable anywhere.
_PAIR_RE = re.compile(r"\b([A-Z0-9]{2,12})/(?:" + "|".join(sorted(_QUOTE_SYMBOLS)) + r")\b")
#: Binance futures writes the pair with no separator - "PONSUSDT", "GPROUSDT" - and puts
#: nothing useful in parentheses at all, so without this those announcements would be
#: filed under a truncated title instead of under a ticker.
_CONCAT_PAIR_RE = re.compile(r"\b([A-Z0-9]{2,12})(?:USDT|USDC|FDUSD|BUSD)\b")


def extract_symbols(title: str) -> list[str]:
    """Tickers in a listing title: pair notation first, then parenthesised, deduped in order."""
    out: list[str] = []
    for regex in (_PAIR_RE, _CONCAT_PAIR_RE, _TICKER_RE):
        for match in regex.finditer(title or ""):
            sym = match.group(1).upper()
            if sym in _QUOTE_SYMBOLS or sym in out:
                continue
            out.append(sym)
    return out


def _venue_signal(source: str, title: str, url: str | None, announced_ms: int | None,
                  extra: dict[str, Any] | None = None) -> AlphaSignal | None:
    delisting = bool(_DELISTING_RE.search(title or ""))
    if not delisting and not _LISTING_RE.search(title or ""):
        return None
    symbols = extract_symbols(title)
    subject = symbols[0] if symbols else (title or "")[:60]
    return AlphaSignal(
        source=source,
        kind=SignalKind.VENUE_LISTING,
        subject=subject,
        title=(title or "")[:240],
        url=url,
        event_at_ms=announced_ms,
        # The venue publishes when it *announced*, not when trading opens, so the warning
        # ahead of the actual event is not derivable from this payload. `detect_lag_ms`
        # (how late we were to the announcement) is, and the report shows that instead.
        lead_ms=None,
        lead_basis="venue_publishes_announcement_time_not_listing_time",
        confidence=CONFIDENCE["venue_delisting"] if delisting else CONFIDENCE[SignalKind.VENUE_LISTING],
        key_seed=url or f"{source}:{title}",
        payload={"symbols": symbols, "delisting": delisting, **(extra or {})},
    )


def parse_binance(data: Any) -> list[AlphaSignal]:
    """Undocumented internal CMS route. Every level is optional on purpose."""
    out: list[AlphaSignal] = []
    catalogs = ((data or {}).get("data") or {}).get("catalogs") or []
    for catalog in catalogs if isinstance(catalogs, list) else []:
        for art in (catalog or {}).get("articles") or []:
            code = art.get("code")
            url = f"https://www.binance.com/en/support/announcement/{code}" if code else None
            sig = _venue_signal("binance", art.get("title") or "", url,
                                _int_ms(art.get("releaseDate")), {"article_id": art.get("id")})
            if sig:
                out.append(sig)
    return out


def parse_okx(data: Any) -> list[AlphaSignal]:
    """OKX's documented public API - the stable one of the three."""
    out: list[AlphaSignal] = []
    if str((data or {}).get("code", "0")) not in {"0", "00000"}:
        return out
    for page in (data or {}).get("data") or []:
        for item in (page or {}).get("details") or []:
            sig = _venue_signal("okx", item.get("title") or "", item.get("url"),
                                _int_ms(item.get("pTime")),
                                {"business_p_time": item.get("businessPTime")})
            if sig:
                out.append(sig)
    return out


def parse_upbit(data: Any) -> list[AlphaSignal]:
    """Korean-language titles; the ticker still arrives in parentheses."""
    out: list[AlphaSignal] = []
    for notice in ((data or {}).get("data") or {}).get("notices") or []:
        nid = notice.get("id")
        url = f"https://upbit.com/service_center/notice?id={nid}" if nid else None
        sig = _venue_signal("upbit", notice.get("title") or "", url,
                            _iso_ms(notice.get("first_listed_at") or notice.get("listed_at")),
                            {"notice_id": nid})
        if sig:
            out.append(sig)
    return out


VENUES: dict[str, tuple[str, str, dict[str, Any], Callable[[Any], list[AlphaSignal]]]] = {
    "binance": ("cms.articles", BINANCE_URL, BINANCE_PARAMS, parse_binance),
    "okx": ("support.announcements", OKX_URL, OKX_PARAMS, parse_okx),
    "upbit": ("api.announcements", UPBIT_URL, UPBIT_PARAMS, parse_upbit),
}


def poll_venue(
    venue: str,
    conn: sqlite3.Connection | None = None,
    *,
    cfg: SignalsConfig | None = None,
    raw: Any = None,
) -> tuple[list[AlphaSignal], int, int, str | None]:
    endpoint, url, params, parser = VENUES[venue]
    if raw is None:
        got = get_json(venue, endpoint, url, params=params, timeout_s=20.0, retries=2,
                       wait_for_slot_s=10.0, conn=conn)
        if not got.ok:
            return [], 0, 1, got.receipt.note or "unavailable"
        raw = got.data
    try:
        return parser(raw), 1, 1, None
    except Exception as exc:  # noqa: BLE001 - an undocumented route redesigning is expected
        detail = f"parse failed: {type(exc).__name__}: {exc}"
        _provider_error(venue, endpoint, detail, conn=conn)
        return [], 0, 1, detail


# ---------------------------------------------------------------------- 3. governance

_GOV_KEYWORDS = re.compile(
    r"(\bairdrop|token ?distribution|tokenomics|\bTGE\b|\bclaim|\bgenesis|\ballocation|"
    r"retroactive|points program|token launch|distribute .*token|\bmerkle)",
    re.I,
)


def parse_snapshot(data: Any) -> list[AlphaSignal]:
    """Newly created Snapshot spaces.

    This is the noisiest source in the module by a wide margin - a live sample returned
    "PEPE Burn bank" as the newest space - so it carries confidence 0.15 and the report
    prints that next to the count. A declared ``symbol`` is the one cheap tell that a
    token is intended, so those get a modest bump.
    """
    out: list[AlphaSignal] = []
    spaces = ((data or {}).get("data") or {}).get("spaces") or []
    for space in spaces if isinstance(spaces, list) else []:
        sid = space.get("id")
        if not sid:
            continue
        symbol = space.get("symbol") or None
        chain = _chain_from_network(space.get("network"))
        out.append(
            AlphaSignal(
                source="snapshot",
                kind=SignalKind.GOV_SPACE,
                subject=str(sid),
                title=f"new Snapshot space {space.get('name') or sid}",
                url=f"https://snapshot.org/#/{sid}",
                chain=chain,
                event_at_ms=_int_ms(space.get("created")),
                lead_ms=None,
                lead_basis="no_published_lead_distribution_for_space_creation",
                confidence=CONFIDENCE[SignalKind.GOV_SPACE] + (0.2 if symbol else 0.0),
                key_seed=str(sid),
                payload={
                    "symbol": symbol,
                    "followers": space.get("followersCount"),
                    "network": space.get("network"),
                    "about": str(space.get("about") or "")[:300],
                },
            )
        )
    return out


def _chain_from_network(network: Any) -> Chain | None:
    from kaiba.core.schemas import CHAIN_IDS

    try:
        chain_id = int(network)
    except (TypeError, ValueError):
        return None
    for chain, cid in CHAIN_IDS.items():
        if cid == chain_id:
            return chain
    return None


def parse_discourse(data: Any, host: str) -> list[AlphaSignal]:
    """DAO forum threads that mention a token event. Keyword-filtered, not everything."""
    out: list[AlphaSignal] = []
    topics = ((data or {}).get("topic_list") or {}).get("topics") or []
    for topic in topics if isinstance(topics, list) else []:
        title = str(topic.get("title") or "")
        excerpt = str(topic.get("excerpt") or "")
        if not _GOV_KEYWORDS.search(f"{title}\n{excerpt}"):
            continue
        tid = topic.get("id")
        slug = topic.get("slug") or "t"
        out.append(
            AlphaSignal(
                source="discourse",
                kind=SignalKind.GOV_TOPIC,
                subject=f"{host}#{tid}",
                title=title[:240],
                url=f"https://{host}/t/{slug}/{tid}",
                event_at_ms=_iso_ms(topic.get("created_at")),
                lead_ms=None,
                lead_basis="forum_thread_has_no_published_event_date",
                confidence=CONFIDENCE[SignalKind.GOV_TOPIC],
                key_seed=f"{host}:{tid}",
                payload={"forum": host, "posts": topic.get("posts_count"),
                         "excerpt": excerpt[:300]},
            )
        )
    return out


def poll_snapshot(conn: sqlite3.Connection | None = None, *, cfg: SignalsConfig | None = None,
                  raw: Any = None) -> tuple[list[AlphaSignal], int, int, str | None]:
    config = cfg or load_config()
    if raw is None:
        got = post_json("snapshot", "graphql.spaces", SNAPSHOT_URL,
                        json_body={"query": SNAPSHOT_QUERY,
                                   "variables": {"first": config.snapshot_spaces}},
                        timeout_s=20.0, retries=2, wait_for_slot_s=10.0, conn=conn)
        if not got.ok:
            return [], 0, 1, got.receipt.note or "unavailable"
        raw = got.data
    try:
        return parse_snapshot(raw), 1, 1, None
    except Exception as exc:  # noqa: BLE001
        detail = f"parse failed: {type(exc).__name__}: {exc}"
        _provider_error("snapshot", "graphql.spaces", detail, conn=conn)
        return [], 0, 1, detail


def poll_discourse(conn: sqlite3.Connection | None = None, *, cfg: SignalsConfig | None = None,
                   raw: dict[str, Any] | None = None) -> tuple[list[AlphaSignal], int, int, str | None]:
    config = cfg or load_config()
    hosts = list(raw) if raw is not None else list(config.forums)
    found: list[AlphaSignal] = []
    ok = 0
    err: str | None = None
    for host in hosts:
        if raw is not None:
            payload: Any = raw[host]
        else:
            got = get_json("discourse", "forum.latest", f"https://{host}/latest.json",
                           timeout_s=20.0, retries=2, ttl_s=120, wait_for_slot_s=20.0, conn=conn)
            if not got.ok:
                err = f"{host}: {got.receipt.note or 'unavailable'}"
                continue
            payload = got.data
        ok += 1
        try:
            found.extend(parse_discourse(payload, host))
        except Exception as exc:  # noqa: BLE001
            err = f"{host}: parse failed: {type(exc).__name__}: {exc}"
            _provider_error("discourse", "forum.latest", err, conn=conn)
    return found, ok, len(hosts), err


# ------------------------------------------------------------------ 4. repository feeds

#: Matched against the entry **title** - the commit message or release tag. Word-boundary
#: anchored on purpose: without ``\b`` the substring ``claim`` matched
#: ``GetL2UnclaimedWithdrawalsByAddress`` in a live scroll-tech feed.
_REPO_KEYWORDS = re.compile(
    r"(\bairdrop|\bmerkle|\bdistributor\b|token ?distribution|\bclaim\b|\bTGE\b|\bvesting\b|"
    r"(?:token|airdrop|genesis|initial) ?allocation|token ?contract|genesis ?(?:drop|alloc))",
    re.I,
)
#: Matched against the entry **body**, and deliberately much narrower.
#:
#: Release notes are long prose and the ordinary vocabulary of an infrastructure repo
#: collides with the ordinary vocabulary of a token launch. Both of the two GitHub
#: "signals" produced by the first live run were false positives of exactly this kind: a
#: monad release matched ``allocation`` in "past the end of the vector's allocation", and
#: an op-challenger release matched ``claim`` in "the claim count query is now skipped".
#: Two out of two is not a precision problem at the margin, it is a source that does not
#: work, so the body now needs an unambiguously token-flavoured phrase.
_REPO_BODY_KEYWORDS = re.compile(
    r"(\bairdrop|merkle ?distributor|token ?distribution|\bTGE\b|"
    r"token ?generation ?event|(?:token|airdrop|genesis) ?allocation)",
    re.I,
)
_ATOM_NS = "{http://www.w3.org/2005/Atom}"


def parse_atom(body: str, repo: str, kind: str) -> list[AlphaSignal]:
    """Parse a GitHub ``releases.atom`` / ``commits.atom`` feed, keyword-filtered.

    A repo's release cadence is noise on its own; what is worth waking up for is a
    *distribution* or *token contract* landing. The title and the body are filtered
    against different vocabularies - see :data:`_REPO_BODY_KEYWORDS` - because changelog
    prose uses "claim" and "allocation" to mean something else entirely. An unparseable
    feed yields an empty list, because GitHub changing its markup is not an exception this
    system should raise.
    """
    out: list[AlphaSignal] = []
    try:
        root = ET.fromstring(body or "")
    except ET.ParseError:
        return out
    for entry in root.findall(f"{_ATOM_NS}entry"):
        title = (entry.findtext(f"{_ATOM_NS}title") or "").strip()
        content = (entry.findtext(f"{_ATOM_NS}content") or "")[:4000]
        matched = _REPO_KEYWORDS.search(title) or _REPO_BODY_KEYWORDS.search(content)
        if not matched:
            continue
        entry_id = (entry.findtext(f"{_ATOM_NS}id") or title).strip()
        link_el = entry.find(f"{_ATOM_NS}link")
        url = link_el.get("href") if link_el is not None else None
        author = entry.findtext(f"{_ATOM_NS}author/{_ATOM_NS}name")
        out.append(
            AlphaSignal(
                source="github",
                kind=kind,
                subject=repo,
                title=title[:240],
                url=url,
                event_at_ms=_iso_ms(entry.findtext(f"{_ATOM_NS}updated")),
                lead_ms=None,
                lead_basis="repo_activity_has_no_published_event_date",
                confidence=CONFIDENCE.get(kind, 0.3),
                key_seed=entry_id,
                payload={"repo": repo, "author": author, "entry_id": entry_id,
                         "matched": matched.group(0)},
            )
        )
    return out


def poll_github(conn: sqlite3.Connection | None = None, *, cfg: SignalsConfig | None = None,
                raw: dict[str, str] | None = None) -> tuple[list[AlphaSignal], int, int, str | None]:
    """Atom feeds only. The Events API is capped at 60/hour unauthenticated; these are not."""
    config = cfg or load_config()
    feeds: list[tuple[str, str, str]] = []
    if raw is not None:
        for key in raw:
            repo, _, leaf = key.rpartition(":")
            feeds.append((repo or key, leaf or "releases", key))
    else:
        for repo in config.repos:
            feeds.append((repo, "releases", f"{repo}:releases"))
            feeds.append((repo, "commits", f"{repo}:commits"))
    found: list[AlphaSignal] = []
    ok = 0
    err: str | None = None
    for repo, leaf, key in feeds:
        if raw is not None:
            body: str | None = raw[key]
        else:
            got = fetch_text("github", f"atom.{leaf}", f"https://github.com/{repo}/{leaf}.atom",
                             ttl_s=120, retries=2, wait_for_slot_s=20.0, conn=conn)
            if not got.ok:
                err = f"{repo}/{leaf}: {got.receipt.note or 'unavailable'}"
                continue
            body = got.data
        ok += 1
        kind = SignalKind.REPO_RELEASE if leaf == "releases" else SignalKind.REPO_COMMIT
        found.extend(parse_atom(body or "", repo, kind))
    return found, ok, len(feeds), err


# ------------------------------------------------------------- 5. pre-launch markets


def parse_hyperliquid(data: Any) -> list[str]:
    """Asset names from ``{"type":"meta"}``. Delisted assets are not markets."""
    universe = (data or {}).get("universe") or []
    out: list[str] = []
    for item in universe if isinstance(universe, list) else []:
        name = (item or {}).get("name")
        if name and not item.get("isDelisted"):
            out.append(str(name).upper())
    return out


def parse_aevo(data: Any) -> list[str]:
    """Aevo answers with a bare list of ticker strings."""
    if not isinstance(data, list):
        return []
    return [str(x).upper() for x in data if x]


def diff_assets(conn: sqlite3.Connection, source: str, items: Iterable[str]) -> list[str]:
    """Return assets never seen before, establishing the baseline silently on first poll.

    The first successful poll writes the whole asset list and returns **nothing**. Without
    this, day one reports every asset Hyperliquid has ever listed as a brand-new
    pre-launch market, and the operator correctly stops reading the feed. An empty
    ``items`` never establishes a baseline, so a source that answers 200-with-nothing does
    not quietly arm the next poll to report its entire universe.
    """
    current = [str(i) for i in items if i]
    if not current:
        return []
    ts = now_ms()
    known = {r["item"] for r in fetch_all(conn, "SELECT item FROM alpha_baseline WHERE source=?",
                                          (source,))}
    fresh = [i for i in current if i not in known]
    for item in fresh:
        conn.execute(
            "INSERT OR IGNORE INTO alpha_baseline(source, item, first_seen_ms) VALUES (?,?,?)",
            (source, item, ts),
        )
    if not known:
        upsert(conn, "alpha_source_health",
               {"source": source, "kind": "prelaunch", "baseline_ms": ts},
               conflict=["source"], update=["baseline_ms"])
        log.info("baseline established for %s with %d assets; reporting none", source, len(current))
        return []
    return fresh


def poll_prelaunch(
    venue: str,
    conn: sqlite3.Connection | None = None,
    *,
    cfg: SignalsConfig | None = None,
    raw: Any = None,
) -> tuple[list[AlphaSignal], int, int, str | None]:
    c = conn or get_conn()
    if venue == "hyperliquid":
        if raw is None:
            got = post_json("hyperliquid", "info.meta", HYPERLIQUID_URL, json_body={"type": "meta"},
                            timeout_s=20.0, retries=2, wait_for_slot_s=10.0, conn=c)
            if not got.ok:
                return [], 0, 1, got.receipt.note or "unavailable"
            raw = got.data
        assets = parse_hyperliquid(raw)
        url_for = "https://app.hyperliquid.xyz/trade/{}".format
    else:
        if raw is None:
            got = get_json("aevo", "assets.list", AEVO_URL, timeout_s=20.0, retries=2,
                           wait_for_slot_s=10.0, conn=c)
            if not got.ok:
                return [], 0, 1, got.receipt.note or "unavailable"
            raw = got.data
        assets = parse_aevo(raw)
        url_for = "https://app.aevo.xyz/perpetual/{}".format
    if not assets:
        return [], 0, 1, "source returned no assets"
    seen = now_ms()
    out = [
        AlphaSignal(
            source=venue,
            kind=SignalKind.PRELAUNCH_MARKET,
            subject=symbol,
            title=f"{venue} opened a market for {symbol}",
            url=url_for(symbol),
            event_at_ms=None,
            first_seen_ms=seen,
            lead_ms=None,
            lead_basis="venue_publishes_no_listing_timestamp",
            confidence=CONFIDENCE[SignalKind.PRELAUNCH_MARKET],
            key_seed=symbol,
            payload={"venue": venue, "universe_size": len(assets)},
        )
        for symbol in diff_assets(c, venue, assets)
    ]
    return out, 1, 1, None


# ------------------------------------------------------------------------- persistence


def record_signal(conn: sqlite3.Connection, sig: AlphaSignal) -> bool:
    """Insert and emit. ``False`` means we already had it, so it does not fire twice."""
    if fetch_one(conn, "SELECT signal_key FROM alpha_signals WHERE signal_key=?", (sig.signal_key,)):
        return False
    conn.execute(
        "INSERT OR IGNORE INTO alpha_signals (signal_key, source, kind, subject, title, url, "
        "chain, event_at_ms, first_seen_ms, lead_ms, lead_basis, confidence, payload_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            sig.signal_key, sig.source, sig.kind, sig.subject, sig.title, sig.url,
            sig.chain.value if sig.chain else None, sig.event_at_ms, sig.first_seen_ms,
            sig.lead_ms, sig.lead_basis, sig.confidence, jdump(sig.payload),
        ),
    )
    # ALPHA_LISTING is consumed by the listing-pop lane, and that lane computes latency as
    # `announced_ms or ts_ms`, where `ts_ms` is filled in from the event row itself. An
    # ALPHA_LISTING with no announced_ms therefore reads as "announced zero seconds ago"
    # and scores near maximum strength on a latency we never measured. So this module only
    # ever raises ALPHA_LISTING when the venue published a real announcement time, and
    # passes that time through under the key the lane reads. Everything else is a plain
    # ALPHA_SIGNAL, which no lane treats as a latency-sensitive trigger.
    venue_event = sig.kind == SignalKind.VENUE_LISTING and sig.event_at_ms is not None
    extra: dict[str, Any] = (
        {"announced_ms": sig.event_at_ms, "exchange": sig.source} if venue_event else {}
    )
    emit(
        EventKind.ALPHA_LISTING if venue_event else EventKind.ALPHA_SIGNAL,
        {
            "signal_key": sig.signal_key,
            "source": sig.source,
            "signal_kind": sig.kind,
            "subject": sig.subject,
            "title": sig.title,
            "url": sig.url,
            "event_at_ms": sig.event_at_ms,
            "first_seen_ms": sig.first_seen_ms,
            "lead_ms": sig.lead_ms,
            "lead_basis": sig.lead_basis,
            "lead_estimable": sig.lead_ms is not None,
            "detect_lag_ms": sig.detect_lag_ms,
            "confidence": sig.confidence,
            **sig.payload,
            **extra,
        },
        chain=sig.chain,
        subject=sig.subject,
        conn=conn,
        dedupe_key=f"alpha_signal:{sig.signal_key}",
    )
    return True


def record_health(
    conn: sqlite3.Connection,
    source: str,
    kind: str,
    *,
    ok: bool,
    count: int,
    new: int,
    interval_s: int,
    error: str | None = None,
) -> None:
    """Per-source last-success.

    ``ok`` is passed in rather than inferred from ``count == 0``. "crt.sh answered and
    there were no new claim subdomains this hour" is the healthy, expected case, and a
    health table that calls it a failure trains the operator to ignore the column that
    exists precisely so they do not have to guess.
    """
    ts = now_ms()
    row = fetch_one(conn, "SELECT fail_streak, total_new FROM alpha_source_health WHERE source=?",
                    (source,))
    streak = int(row["fail_streak"]) if row else 0
    total = int(row["total_new"]) if row else 0
    fields: dict[str, Any] = {
        "source": source,
        "kind": kind,
        "interval_s": interval_s,
        "last_poll_ms": ts,
        "last_count": count,
        "last_new": new,
        "total_new": total + new,
        "fail_streak": 0 if ok else streak + 1,
        "last_error": redact_text(error)[:300] if error else None,
    }
    updates = list(fields)
    updates.remove("source")
    if ok:
        fields["last_ok_ms"] = ts
        updates.append("last_ok_ms")
    upsert(conn, "alpha_source_health", fields, conflict=["source"], update=updates)


# ------------------------------------------------------------------------------ refresh


@dataclass(frozen=True)
class Source:
    name: str
    kind: str
    poll: Callable[..., tuple[list[AlphaSignal], int, int, str | None]]


SOURCES: dict[str, Source] = {
    "crtsh": Source("crtsh", "certificate", poll_certificate_transparency),
    "binance": Source("binance", "venue", lambda conn=None, **kw: poll_venue("binance", conn, **kw)),
    "okx": Source("okx", "venue", lambda conn=None, **kw: poll_venue("okx", conn, **kw)),
    "upbit": Source("upbit", "venue", lambda conn=None, **kw: poll_venue("upbit", conn, **kw)),
    "snapshot": Source("snapshot", "governance", poll_snapshot),
    "discourse": Source("discourse", "governance", poll_discourse),
    "github": Source("github", "repository", poll_github),
    "hyperliquid": Source("hyperliquid", "prelaunch",
                          lambda conn=None, **kw: poll_prelaunch("hyperliquid", conn, **kw)),
    "aevo": Source("aevo", "prelaunch", lambda conn=None, **kw: poll_prelaunch("aevo", conn, **kw)),
}


def due_sources(conn: sqlite3.Connection, cfg: SignalsConfig, *, force: bool = False,
                only: Iterable[str] | None = None) -> list[str]:
    """Which sources are past their interval. Each one runs on its own clock."""
    names = [n for n in (list(only) if only else list(SOURCES)) if n in SOURCES]
    if force:
        return names
    ts = now_ms()
    out: list[str] = []
    for name in names:
        row = fetch_one(conn, "SELECT last_poll_ms FROM alpha_source_health WHERE source=?", (name,))
        last = int(row["last_poll_ms"]) if row and row["last_poll_ms"] else 0
        if ts - last >= cfg.interval_for(name) * 1000:
            out.append(name)
    return out


def refresh(
    conn: sqlite3.Connection | None = None,
    *,
    only: Iterable[str] | None = None,
    force: bool = False,
    cfg: SignalsConfig | None = None,
) -> int:
    """Poll every due source and record what is new. Returns the count of new signals.

    One source failing costs the others nothing, and nothing in here raises: the caller is
    a cron loop and an exception there means every source stops, which is the exact
    failure mode this module is supposed to make impossible.
    """
    c = conn or get_conn()
    config = cfg or load_config()
    total_new = 0
    for name in due_sources(c, config, force=force, only=only):
        src = SOURCES[name]
        try:
            signals, ok_n, total_n, err = src.poll(c, cfg=config)
        except Exception as exc:  # noqa: BLE001 - a poller must never take down refresh
            detail = f"{type(exc).__name__}: {exc}"
            _provider_error(name, "poll", detail, conn=c)
            record_health(c, name, src.kind, ok=False, count=0, new=0,
                          interval_s=config.interval_for(name), error=detail)
            continue
        new = sum(1 for sig in signals[: config.max_items_per_endpoint * max(1, total_n)]
                  if record_signal(c, sig))
        record_health(c, name, src.kind, ok=ok_n > 0, count=len(signals), new=new,
                      interval_s=config.interval_for(name), error=err)
        total_new += new
    check_health(c, config)
    return total_new


# -------------------------------------------------------------------------- health


def source_health(conn: sqlite3.Connection | None = None,
                  cfg: SignalsConfig | None = None) -> list[dict[str, Any]]:
    """Every configured source, including ones that have never run.

    A source missing from the table is the most dangerous state of all - it looks like
    nothing rather than like a problem - so it is listed with ``state='never_run'``.
    """
    c = conn or get_conn()
    config = cfg or load_config()
    rows = {r["source"]: r for r in fetch_all(c, "SELECT * FROM alpha_source_health")}
    ts = now_ms()
    out: list[dict[str, Any]] = []
    for name, src in SOURCES.items():
        row = rows.get(name)
        last_ok = int(row["last_ok_ms"]) if row and row["last_ok_ms"] else None
        tolerance = config.dead_after_s(name)
        age_s = None if last_ok is None else (ts - last_ok) / 1000.0
        if row is None:
            state = "never_run"
        elif last_ok is None:
            state = "dead"
        elif age_s is not None and age_s > tolerance:
            state = "dead"
        elif age_s is not None and age_s > tolerance / 2:
            state = "degraded"
        else:
            state = "ok"
        out.append({
            "source": name,
            "kind": src.kind,
            "state": state,
            "interval_s": config.interval_for(name),
            "dead_after_s": tolerance,
            "last_poll_ms": (row or {}).get("last_poll_ms"),
            "last_ok_ms": last_ok,
            "since_ok_s": None if age_s is None else int(age_s),
            "last_count": int((row or {}).get("last_count") or 0),
            "last_new": int((row or {}).get("last_new") or 0),
            "total_new": int((row or {}).get("total_new") or 0),
            "fail_streak": int((row or {}).get("fail_streak") or 0),
            "baseline_ms": (row or {}).get("baseline_ms"),
            "last_error": (row or {}).get("last_error"),
        })
    return out


def check_health(conn: sqlite3.Connection | None = None,
                 cfg: SignalsConfig | None = None) -> list[dict[str, Any]]:
    """Emit for every source that has stopped working. Returns the ones it complained about.

    Deduped per hour so a source that is down all week produces a readable trail rather
    than one event per poll. ``never_run`` is not emitted - a source that was never
    started is a configuration state, not an outage.
    """
    c = conn or get_conn()
    bad = [h for h in source_health(c, cfg) if h["state"] == "dead"]
    for item in bad:
        emit(
            EventKind.SYSTEM,
            {
                "reason": "alpha_source_dead",
                "source": item["source"],
                "since_ok_s": item["since_ok_s"],
                "dead_after_s": item["dead_after_s"],
                "fail_streak": item["fail_streak"],
                "last_error": item["last_error"],
                "detail": "a quiet detector and a quiet market look identical; this one is quiet "
                          "because it is broken",
            },
            level="error",
            subject=item["source"],
            dedupe_key=f"alpha_source_dead:{item['source']}:{now_ms() // 3_600_000}",
            conn=c,
        )
    return bad


# -------------------------------------------------------------------------- reporting


def _ago(ms: int | None) -> str:
    if not ms:
        return "never"
    delta = (now_ms() - ms) / 1000.0
    if delta < 90:
        return f"{delta:.0f}s"
    if delta < 5400:
        return f"{delta / 60:.0f}m"
    if delta < 172_800:
        return f"{delta / 3600:.1f}h"
    return f"{delta / 86_400:.1f}d"


def _lead(ms: int | None) -> str:
    if ms is None:
        return "-"
    return f"{ms / 86_400_000:.1f}d" if abs(ms) >= 86_400_000 else f"{ms / 3_600_000:.1f}h"


def recent_signals(conn: sqlite3.Connection | None = None, limit: int = 50,
                   kind: str | None = None, source: str | None = None) -> list[dict[str, Any]]:
    c = conn or get_conn()
    sql = "SELECT * FROM alpha_signals"
    where: list[str] = []
    params: list[Any] = []
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if source:
        where.append("source = ?")
        params.append(source)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY first_seen_ms DESC LIMIT ?"
    params.append(limit)
    rows = fetch_all(c, sql, params)
    for row in rows:
        row["payload"] = json.loads(row.pop("payload_json") or "{}")
        row["detect_lag_ms"] = (
            None if row["event_at_ms"] is None else row["first_seen_ms"] - row["event_at_ms"]
        )
    return rows


def weekly_report(conn: sqlite3.Connection | None = None, limit: int = 25) -> str:
    """Health first, then signals. A detector nobody trusts is worth less than none.

    Health goes at the top because the first question about any silent feed is whether it
    is silent or broken, and the second table cannot answer that.
    """
    c = conn or get_conn()
    config = load_config()
    health = source_health(c, config)
    dead = [h for h in health if h["state"] in {"dead", "never_run"}]
    lines = [
        "# Early-alpha signals - weekly",
        "",
        "## Source health",
        "",
        "| Source | Kind | State | Last OK | Every | Last poll found | New all-time | Fails |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for item in health:
        lines.append(
            "| {s} | {k} | {st} | {ok} | {iv}s | {n} | {tot} | {f} |".format(
                s=item["source"], k=item["kind"], st=item["state"].upper(),
                ok=_ago(item["last_ok_ms"]), iv=item["interval_s"],
                n=item["last_count"], tot=item["total_new"], f=item["fail_streak"],
            )
        )
    if dead:
        lines += [
            "",
            f"**{len(dead)} of {len(health)} sources are not producing.** "
            + ", ".join(f"`{d['source']}` ({d['state']}: {d['last_error'] or 'no error recorded'})"
                        for d in dead)[:600],
            "",
            "A quiet detector is indistinguishable from a quiet market. Treat the sources above "
            "as absent evidence, not as evidence of absence.",
        ]

    counts = fetch_all(
        c,
        "SELECT source, kind, COUNT(*) AS n, "
        "SUM(CASE WHEN lead_ms IS NOT NULL THEN 1 ELSE 0 END) AS with_lead, "
        "AVG(lead_ms) AS avg_lead, AVG(confidence) AS conf "
        "FROM alpha_signals GROUP BY source, kind ORDER BY n DESC",
    )
    lines += [
        "",
        "## Signals by source",
        "",
        "| Source | Kind | Count | Lead estimable | Mean lead | Prior confidence |",
        "|---|---|---|---|---|---|",
    ]
    if not counts:
        lines.append("| - | - | 0 | 0 | - | - |")
    for row in counts:
        lines.append(
            "| {s} | {k} | {n} | {wl} | {lead} | {conf:.2f} |".format(
                s=row["source"], k=row["kind"], n=row["n"], wl=row["with_lead"] or 0,
                lead=_lead(int(row["avg_lead"])) if row["avg_lead"] is not None else "-",
                conf=float(row["conf"] or 0.0),
            )
        )

    rows = recent_signals(c, limit=limit)
    lines += [
        "",
        "## Most recent",
        "",
        "| Seen | Source | Kind | Subject | Lead | Detect lag | Title |",
        "|---|---|---|---|---|---|---|",
    ]
    if not rows:
        lines.append("| - | - | - | - | - | - | nothing detected yet |")
    for row in rows:
        lines.append(
            "| {seen} | {s} | {k} | {subj} | {lead} | {lag} | {t} |".format(
                seen=_ago(row["first_seen_ms"]), s=row["source"], k=row["kind"],
                subj=str(row["subject"])[:34], lead=_lead(row["lead_ms"]),
                lag=_lead(row["detect_lag_ms"]),
                t=str(row["title"] or "").replace("|", "/")[:60],
            )
        )
    no_lead = sum(1 for r in rows if r["lead_ms"] is None)
    if rows and no_lead:
        lines += [
            "",
            f"{no_lead} of {len(rows)} recent signals carry no lead estimate. That is the honest "
            "state: only certificate transparency has a published lead distribution to estimate "
            "from. For the rest, `detect lag` is what is actually measured - how late we were to "
            "a fact the source had already published.",
        ]
    return "\n".join(lines)


__all__ = [
    "AlphaSignal",
    "SOURCES",
    "SignalKind",
    "SignalsConfig",
    "check_health",
    "classify_subdomain",
    "diff_assets",
    "due_sources",
    "extract_symbols",
    "fetch_text",
    "load_config",
    "parse_aevo",
    "parse_atom",
    "parse_binance",
    "parse_crtsh",
    "parse_discourse",
    "parse_hyperliquid",
    "parse_okx",
    "parse_snapshot",
    "parse_upbit",
    "poll_certificate_transparency",
    "poll_discourse",
    "poll_github",
    "poll_prelaunch",
    "poll_snapshot",
    "poll_venue",
    "recent_signals",
    "record_health",
    "record_signal",
    "refresh",
    "source_health",
    "weekly_report",
]
