"""The exit watchdog's curve reads compete at EXIT priority, not behind discovery.

Before 2026-09-22 ``curve_price.live_resolver`` called ``scanner.fetch_curve_payload`` with
its default ``Priority.DISCOVERY`` for every caller, including the watchdog -- so the one
read that decides whether to close a position queued behind the scan it protects
against. EVM venue reads (``evm_price``) already defaulted to EXIT; Solana did not.
"""

from __future__ import annotations

from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain
from kaiba.execution import curve_price, scanner

TOKEN = "GMFCWQv8CfnjGR2xCJebyxqSsCmyMo31pXcCEnfDpump"


def _capture(monkeypatch) -> dict:
    seen: dict = {}

    def fake_fetch(token, conn=None, *args, **kwargs):
        seen["token"] = token
        seen["kwargs"] = dict(kwargs)
        return None, None

    monkeypatch.setattr(scanner, "fetch_curve_payload", fake_fetch)
    return seen


def test_live_resolver_passes_the_priority_it_was_given(tmp_db, monkeypatch):
    seen = _capture(monkeypatch)
    curve_price.live_resolver(tmp_db, priority=Priority.EXIT)(Chain.SOL, TOKEN, 0)
    assert seen["kwargs"].get("priority") is Priority.EXIT


def test_live_resolver_without_a_priority_keeps_the_scanner_default(tmp_db, monkeypatch):
    """The scanner and the paper CLI are discovery work; they must not be promoted."""
    seen = _capture(monkeypatch)
    curve_price.live_resolver(tmp_db)(Chain.SOL, TOKEN, 0)
    assert "priority" not in seen["kwargs"]


def test_the_curve_price_source_threads_the_priority_to_its_live_read(tmp_db, monkeypatch):
    seen = _capture(monkeypatch)
    src = curve_price.curve_price_source(priority=Priority.EXIT)
    src.resolver(Chain.SOL, TOKEN, 0)
    assert seen["kwargs"].get("priority") is Priority.EXIT


def test_the_watchdog_venue_source_reads_solana_at_exit(tmp_db, monkeypatch):
    seen = _capture(monkeypatch)
    from kaiba.execution import watchdog as wd

    venue = wd.resolve_price_source("venue")
    # Since 2026-09-23 "venue" is a FallbackPriceSource wrapping the chain-routed source
    # and a last-resort gmgn layer; the routes live on the inner source. Unwrap rather
    # than assert the shape, so this keeps testing the ROUTING it is about.
    routes = getattr(venue, "routes", None)
    if routes is None:
        inner = next((s for s in getattr(venue, "sources", ()) if hasattr(s, "routes")), None)
        assert inner is not None, f"no routed source inside {venue!r}"
        routes = inner.routes
    assert routes and Chain.SOL in routes, "the venue source must route Solana to the curve source"
    routes[Chain.SOL].resolver(Chain.SOL, TOKEN, 0)
    assert seen["kwargs"].get("priority") is Priority.EXIT


def test_the_watchdog_curve_source_reads_at_exit(tmp_db, monkeypatch):
    seen = _capture(monkeypatch)
    from kaiba.execution import watchdog as wd

    src = wd.resolve_price_source("curve")
    src.resolver(Chain.SOL, TOKEN, 0)
    assert seen["kwargs"].get("priority") is Priority.EXIT
