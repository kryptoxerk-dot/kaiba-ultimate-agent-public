"""Robinhood Chain's native asset IS ETH, so its USD price comes from ETH's samples.

MEASURED 2026-09-22: `native_prices` held 1,430 sol rows, 1,106 eth rows, 93 bsc rows and
ZERO robinhood rows -- DexScreener has no slug for robinhood and it has no WRAPPED_NATIVE
entry, so the sampler cannot reach it. `at(Chain.ROBINHOOD)` therefore returned
UNAVAILABLE, every robinhood round-trip cost was unpriceable, `sizing_band` refused, and a
funded, armed robinhood chain had never placed a single order.

This is an identity (a unit of ETH is worth the same wherever it sits), not a proxy -- but
the receipt still names the chain the sample came from, because a substitution the reader
cannot see is indistinguishable from a bug.
"""

from __future__ import annotations

from decimal import Decimal

from kaiba.core.schemas import Chain, EvidenceBasis
from kaiba.providers import native_price as npmod
from kaiba.providers.native_price import NATIVE_PRICE_ALIAS, at, price_source_chain

TS = 1_790_000_000_000


def put(conn, chain: Chain, ts_ms: int, price: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO native_prices (chain, ts_ms, price_usd, source, receipt_json) "
        "VALUES (?,?,?,?,?)",
        (chain.value, ts_ms, price, "dexscreener", "{}"),
    )
    conn.commit()


def test_robinhood_is_routed_to_eth():
    assert price_source_chain(Chain.ROBINHOOD) is Chain.ETH
    assert NATIVE_PRICE_ALIAS[Chain.ROBINHOOD] is Chain.ETH


def test_every_other_chain_answers_for_itself():
    for chain in (Chain.SOL, Chain.BSC, Chain.ETH, Chain.BASE):
        assert price_source_chain(chain) is chain


def test_an_eth_sample_prices_robinhood(tmp_db):
    put(tmp_db, Chain.ETH, TS, "2700.46")
    got = at(Chain.ROBINHOOD, TS, tmp_db)
    assert got.price_usd == Decimal("2700.46")
    assert got.basis is EvidenceBasis.PROVIDER_REPORTED
    assert got.chain is Chain.ROBINHOOD, "the answer is about robinhood"


def test_the_receipt_says_where_the_sample_came_from(tmp_db):
    """A substitution the reader cannot see is indistinguishable from a bug."""
    put(tmp_db, Chain.ETH, TS, "2700.46")
    note = (at(Chain.ROBINHOOD, TS, tmp_db).receipt.note or "").lower()
    assert "eth" in note and "same native asset" in note, note


def test_no_eth_samples_means_unavailable_not_zero(tmp_db):
    got = at(Chain.ROBINHOOD, TS, tmp_db)
    assert got.price_usd is None and got.basis is EvidenceBasis.UNAVAILABLE
    assert "eth" in (got.receipt.note or "").lower(), got.receipt.note


def test_a_stale_eth_sample_is_still_refused_for_robinhood(tmp_db):
    """The alias changes WHERE we look, never the freshness rule."""
    put(tmp_db, Chain.ETH, TS - 3_600_000, "2700.46")
    got = at(Chain.ROBINHOOD, TS, tmp_db, tolerance_ms=300_000)
    assert got.price_usd is None and got.basis is EvidenceBasis.UNAVAILABLE
    assert "not contemporaneous" in (got.receipt.note or "")


def test_a_robinhood_row_is_not_required_and_eth_is_not_polluted(tmp_db):
    """We never write robinhood rows; the alias is read-side only."""
    put(tmp_db, Chain.ETH, TS, "2700.46")
    at(Chain.ROBINHOOD, TS, tmp_db)
    n = tmp_db.execute("SELECT COUNT(*) FROM native_prices WHERE chain='robinhood'").fetchone()[0]
    assert n == 0
