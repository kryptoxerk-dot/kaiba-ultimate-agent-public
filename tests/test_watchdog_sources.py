"""Every price-source name resolves to a REAL source, never silently to blind.

INCIDENT 2026-09-21. ``config/risk.yaml`` ``protection.price_source`` was set to ``venue``
before ``watchdog.PRICE_SOURCES`` had that key. ``resolve_price_source`` logged one warning
and returned ``NullPriceSource`` -- and the exit watchdog ran BLIND on every chain, on a
box that was live-armed with the sizer able to place orders. Nothing filled in the window,
by luck of timing. A configured name that degrades to blindness is the worst kind of
failure this system has: it looks armed and protects nothing.
"""

from __future__ import annotations

import pytest
import yaml

from kaiba.core.config import DEFAULT_RISK_PATH
from kaiba.execution import watchdog


#: Every source name an operator might reasonably write. If a new one is added to the
#: code it must be added here too, so that the "shipped config resolves" test below can
#: never be satisfied by a typo that happens to match nothing.
KNOWN_SOURCES = ("none", "null", "off", "prices", "provider", "dexscreener",
                 "curve", "jupiter", "evm", "venue")


@pytest.mark.parametrize("name", KNOWN_SOURCES)
def test_every_known_source_name_is_registered(name: str) -> None:
    assert name in watchdog.PRICE_SOURCES, f"{name!r} would silently run blind"


@pytest.mark.parametrize("name", ["evm", "venue", "curve", "jupiter"])
def test_a_real_source_never_resolves_to_null(name: str) -> None:
    """The null sources are the ONLY names allowed to resolve to NullPriceSource."""
    src = watchdog.resolve_price_source(name)
    assert not isinstance(src, watchdog.NullPriceSource), (
        f"price_source {name!r} resolved to NullPriceSource: the watchdog would be blind"
    )


def test_the_shipped_config_price_source_is_registered() -> None:
    """The exact string in config/risk.yaml must be a key in PRICE_SOURCES.

    This is the test that would have caught the incident. It reads the file the live
    box reads, so a config edit that outruns the code fails here before it ships.
    """
    raw = yaml.safe_load(open(DEFAULT_RISK_PATH, encoding="utf-8"))
    name = (raw.get("protection") or {}).get("price_source")
    assert name is not None, "shipped config declares no price_source at all"
    assert name in watchdog.PRICE_SOURCES, (
        f"config/risk.yaml price_source={name!r} is not registered; the watchdog would run blind"
    )
    assert name not in ("none", "null", "off"), "shipped config must not ship blind"


def test_an_unknown_name_is_loud_not_silent(caplog) -> None:
    """The fallback must warn. It did, and one warning is not enough -- but it must exist."""
    import logging
    with caplog.at_level(logging.WARNING):
        src = watchdog.resolve_price_source("definitely-not-a-source")
    assert isinstance(src, watchdog.NullPriceSource)
    assert any("running blind" in r.message for r in caplog.records)
