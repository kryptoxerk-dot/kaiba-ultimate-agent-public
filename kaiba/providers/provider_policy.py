"""Owner's spending constraint, 2026-11-02; no network, DB, or credential reads.

Alchemy is unnecessary in the measured current deployment: all configured RPC
hosts are public and GMGN is prepaid. Block new Alchemy network traffic at the
shared HTTP and websocket boundaries, including the diagnostic capacity probe.
Cached evidence is still usable under its original freshness rules.

This is a Kaiba network policy, not an Alchemy billing-account cancellation.
Do not add an environment-variable escape hatch or reset provider budgets.
"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

PAID_USE_BEFORE = datetime.fromisoformat("2026-12-01T00:00:00+07:00")
ALCHEMY_NETWORK_ENABLED = False
_HOSTS = ("alchemy.com", "alchemyapi.io")


def is_alchemy(url: str, *, provider: str = "") -> bool:
    """Classify without exposing a URL/key or mistaking a substring for a host."""
    if provider.strip().lower() == "alchemy":
        return True
    try:
        host = (urlsplit(str(url)).hostname or "").lower().rstrip(".")
    except ValueError:
        return False  # the transport rejects malformed URLs; never guess a host
    return any(host == suffix or host.endswith("." + suffix) for suffix in _HOSTS)


def refusal(url: str, *, provider: str = "", at: datetime | None = None) -> str | None:
    """A fixed, safe reason when an Alchemy request must spend zero credits."""
    if not is_alchemy(url, provider=provider):
        return None
    current = at or datetime.now(PAID_USE_BEFORE.tzinfo)
    if current.tzinfo is None:
        return "alchemy_policy_clock_unavailable"
    if current >= PAID_USE_BEFORE:
        return "alchemy_disabled_after_2026_11_owner_budget"
    if not ALCHEMY_NETWORK_ENABLED:
        return "alchemy_disabled_owner_budget_2026_11_02"
    # Enabling this requires a separately reviewed bounded allowance. A boolean
    # edit alone must never authorize unmetered PAYG consumption.
    return "alchemy_verified_allowance_required"


def require_network(url: str, *, provider: str = "") -> None:
    """Used by the existing websocket dialer; error text never contains a key."""
    reason = refusal(url, provider=provider)
    if reason:
        raise PermissionError(reason)
