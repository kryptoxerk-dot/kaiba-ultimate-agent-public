"""Rates for a token that trades on a DEX rather than a curve we can read.

WHY THIS EXISTS. Traced 2026-09-22: bsc and graduated-robinhood entries both died at
`no_viable_band`, which reads like a depth problem and was not one. `sizing_band` refuses
on `cost_model_unavailable` first; on EVM that is `evm_cost_model`, which needs three
numbers from `read_venue` -- gas, the venue fee, the token's tax. Only a Pons CURVE reader
existed, so bsc got `no_venue_reader:bsc` and a graduated robinhood token got
`pons:graduated`, and all three numbers were None. The depth was fine throughout: the bsc
refusal literally read `dossier:no_venue_reader:bsc:age87s`.

`read_venue`'s docstring explained why nobody had fixed it, and the reason was sound at
the time -- "Flap and Four.meme charge a creator tax of 1-10% a side ... there is no
reader here that can tell one from another. Guessing would be the $25-pool mistake with a
different number in it." What changed is that the dossier now carries a per-token
`buy_tax_bps`/`sell_tax_bps` from `gmgn token.security`: MEASURED on 31 of 33 bsc dossiers
(94%) and 194 of 200 robinhood (97%), and it DISCRIMINATES -- 26 bsc tokens at 1-100 bps,
3 at exactly zero, 2 at 101-500. We can tell one from another now, so we need not guess.

These tests pin the three ways this reader could quietly start guessing again.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain, EvidenceBasis
from kaiba.execution import viability as V
from kaiba.execution.viability import (
    DEX_FEE_BPS_UPPER,
    _dossier_tax_bps_per_leg,
    _left_the_curve,
    read_dex_venue,
)

TOKEN = "0x7f98ea3d039f2cce3a244c89567694b63f127777"


def put_dossier(conn, chain: Chain, token: str, *, buy: str | None, sell: str | None) -> None:
    """A dossier carrying (or deliberately missing) the two tax legs."""
    def measure(value: str | None) -> dict:
        if value is None:
            return {"value": None, "basis": "unavailable", "receipt": None, "freshness_budget_s": 900}
        return {"value": value, "basis": "provider_reported",
                "receipt": {"provider": "gmgn", "endpoint": "token.security",
                            "observed_at_ms": 1, "basis": "provider_reported"},
                "freshness_budget_s": 900}

    body = {"address": token, "chain": chain.value, "built_at_ms": 1, "grade": "B",
            "buy_tax_bps": measure(buy), "sell_tax_bps": measure(sell)}
    conn.execute(
        "INSERT OR REPLACE INTO token_dossiers (chain, address, built_at_ms, score, grade, "
        "blockers_json, warnings_json, unknowns_json, dossier_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (chain.value, token, 1, None, "B", "[]", "[]", "[]", json.dumps(body)),
    )
    conn.commit()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Gas comes from a CLI. Pin it so these tests measure the rates logic, not a network."""
    monkeypatch.setattr(V, "_evm_gas_price_wei", lambda chain, conn: (120_000_000, "gas:test"))


# ------------------------------------------------------------------ the tax must be read


def test_a_measured_tax_prices_the_venue(tmp_db):
    put_dossier(tmp_db, Chain.BSC, TOKEN, buy="100.00", sell="100.00")
    venue = read_dex_venue(Chain.BSC, TOKEN, tmp_db)
    assert venue.priced
    assert venue.tax_bps_per_leg == Decimal("100.00")
    assert venue.fee_bps_per_leg == Decimal(DEX_FEE_BPS_UPPER[Chain.BSC])
    assert venue.gas_price_wei == 120_000_000


def test_an_unknown_tax_measure_is_not_read_as_a_number(tmp_db):
    """`basis: unavailable` means we do not know it. It must never become 0."""
    put_dossier(tmp_db, Chain.BSC, TOKEN, buy=None, sell=None)
    venue = read_dex_venue(Chain.BSC, TOKEN, tmp_db)
    assert not venue.priced
    assert venue.tax_bps_per_leg is None
    assert "buy_tax_bps_unavailable" in venue.note, venue.note


def test_one_missing_leg_still_refuses(tmp_db):
    """We pay both legs. Knowing half the cost is not knowing the cost."""
    put_dossier(tmp_db, Chain.BSC, TOKEN, buy="50", sell=None)
    venue = read_dex_venue(Chain.BSC, TOKEN, tmp_db)
    assert not venue.priced and venue.tax_bps_per_leg is None
    assert "sell_tax_bps_unavailable" in venue.note, venue.note


def test_the_worse_leg_is_charged_not_the_average(tmp_db):
    """0% in and 10% out costs 10% on the round trip, and looks like 5% on the mean."""
    put_dossier(tmp_db, Chain.BSC, TOKEN, buy="0", sell="1000")
    tax, why = _dossier_tax_bps_per_leg(Chain.BSC, TOKEN, tmp_db)
    assert tax == Decimal(1000), why
    venue = read_dex_venue(Chain.BSC, TOKEN, tmp_db)
    assert venue.tax_bps_per_leg == Decimal(1000)


def test_a_zero_tax_is_a_measurement_not_an_unknown(tmp_db):
    """194 of 200 robinhood dossiers report exactly 0. That is a reading, and it prices."""
    put_dossier(tmp_db, Chain.ROBINHOOD, TOKEN, buy="0", sell="0")
    venue = read_dex_venue(Chain.ROBINHOOD, TOKEN, tmp_db)
    assert venue.priced and venue.tax_bps_per_leg == Decimal(0)


def test_no_dossier_refuses(tmp_db):
    venue = read_dex_venue(Chain.BSC, TOKEN, tmp_db)
    assert not venue.priced and venue.note == "dex:no_dossier"


def test_an_unbounded_chain_refuses(tmp_db):
    """A chain with no published fee bound gets no rates, rather than someone else's."""
    put_dossier(tmp_db, Chain.BASE, TOKEN, buy="0", sell="0")
    assert Chain.BASE not in DEX_FEE_BPS_UPPER
    venue = read_dex_venue(Chain.BASE, TOKEN, tmp_db)
    assert not venue.priced and "no_dex_fee_bound" in venue.note


def test_the_reader_supplies_no_depth(tmp_db):
    """Depth is `resolve_depth`'s job; this reader exists for the three cost numbers."""
    put_dossier(tmp_db, Chain.BSC, TOKEN, buy="0", sell="0")
    venue = read_dex_venue(Chain.BSC, TOKEN, tmp_db)
    assert isinstance(venue.depth, V.NoDepth)


# ------------------------------------------------- the fallback is graduation-only


@pytest.mark.parametrize("note", ["pons:graduated", "pons:factory_has_no_curve", "x:no_curve:y"])
def test_a_permanently_curveless_token_may_fall_back(note):
    assert _left_the_curve(note)


@pytest.mark.parametrize(
    "note",
    ["pons:rpc_timeout", "pons:factory_unreadable:no attempt made", "pons:probe_disabled", None, ""],
)
def test_a_transient_curve_failure_may_not(note):
    """A flaky node must keep refusing. Otherwise it silently downgrades every entry to a
    looser cost model at exactly the moment the chain is misbehaving."""
    assert not _left_the_curve(note)


def test_read_venue_falls_back_for_a_graduated_robinhood_token(tmp_db, monkeypatch):
    """The live case: four of four recent robinhood signals died on `pons:graduated`."""
    put_dossier(tmp_db, Chain.ROBINHOOD, TOKEN, buy="0", sell="0")
    monkeypatch.setattr(
        V, "read_pons_venue",
        lambda token, conn, at_ms=None: V.VenueRead(
            V.NoDepth(source="pons:graduated"), None, None, None, "pons:graduated"),
    )
    V.reset_venue_cache()
    venue = V.read_venue(Chain.ROBINHOOD, TOKEN, tmp_db)
    assert venue.priced, venue.note
    assert venue.tax_bps_per_leg == Decimal(0)
    assert "dex:" in venue.note


def test_read_venue_does_not_fall_back_on_a_transient_curve_failure(tmp_db, monkeypatch):
    put_dossier(tmp_db, Chain.ROBINHOOD, TOKEN, buy="0", sell="0")
    monkeypatch.setattr(
        V, "read_pons_venue",
        lambda token, conn, at_ms=None: V.VenueRead(
            V.NoDepth(source="pons:rpc_timeout"), None, None, None, "pons:rpc_timeout"),
    )
    V.reset_venue_cache()
    venue = V.read_venue(Chain.ROBINHOOD, TOKEN, tmp_db)
    assert not venue.priced, venue.note
    assert venue.note == "pons:rpc_timeout"


def test_the_fee_bound_is_an_upper_bound_not_a_guess():
    """Both listed venues top out at 1.00%; charging the worst tier fails toward refusing."""
    assert set(DEX_FEE_BPS_UPPER) == {Chain.BSC, Chain.ROBINHOOD}
    assert all(v == 100 for v in DEX_FEE_BPS_UPPER.values())
    row = V.PROVENANCE["DEX_FEE_BPS_UPPER"]
    assert "UPPER BOUND" in row.note and "CITED" in row.source
