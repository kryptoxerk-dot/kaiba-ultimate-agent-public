"""Do not poll Pump.fun reserves for a token already known to be off that curve."""
from decimal import Decimal
from unittest.mock import Mock
import pytest
from kaiba.core.schemas import Chain, EvidenceBasis, now_ms
from kaiba.execution.curve_price import CurvePriceSource
from kaiba.execution.watchdog import PriceQuote

TOKEN = "Bnd5oBSWpPpXoaWTckgXTVx9TCKkX5GyVqxcrwBzUYsB"

@pytest.mark.parametrize("launchpad,migrated", [("pump.fun", True), ("meteora_virtual_curve", False)])
def test_known_non_curve_tokens_skip_pump_resolver(tmp_db, launchpad, migrated):
    tmp_db.execute("INSERT INTO tokens(chain,address,launchpad,migrated_ms,first_seen_ms) VALUES(?,?,?,?,?)",
                   ("sol", TOKEN, launchpad, now_ms() - 1000 if migrated else None, now_ms()))
    resolver = Mock(return_value=(None, "wrong venue"))
    fallback = Mock()
    fallback.quote.return_value = PriceQuote(price_usd=Decimal("1"),
        basis=EvidenceBasis.PROVIDER_REPORTED, source="verified-pool")
    source = CurvePriceSource(tmp_db, sol_usd=Decimal("100"), resolver=resolver, fallback=fallback)
    assert source.quote(Chain.SOL, TOKEN).source == "verified-pool"
    resolver.assert_not_called()
    fallback.quote.assert_called_once_with(Chain.SOL, TOKEN)


def test_unknown_venue_still_attempts_the_resolver(tmp_db):
    resolver = Mock(return_value=(None, "no evidence"))
    source = CurvePriceSource(tmp_db, sol_usd=Decimal("100"), resolver=resolver)
    assert not source.quote(Chain.SOL, TOKEN).usable
    resolver.assert_called_once()


def test_routing_does_not_make_unavailable_pool_price_safe(tmp_db):
    tmp_db.execute("INSERT INTO tokens(chain,address,launchpad,migrated_ms,first_seen_ms) VALUES(?,?,?,?,?)",
                   ("sol", TOKEN, "pump.fun", now_ms() - 1000, now_ms()))
    resolver = Mock(return_value=(None, "wrong venue"))
    fallback = Mock()
    fallback.quote.return_value = PriceQuote.unavailable("pool down")
    source = CurvePriceSource(tmp_db, sol_usd=Decimal("100"), resolver=resolver, fallback=fallback)
    assert not source.quote(Chain.SOL, TOKEN).usable
    resolver.assert_not_called()
