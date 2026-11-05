"""A robinhood Pons CURVE leg pays the curve's own take, ``feeBps() + creatorTaxBps()`` (2026-10-23,
step 3 follow-up, blocking).

Before: a Pons curve entry (``curve:pons``) and a curve-decided exit (``curve_mark``), both tagged
honest and counted, paid a flat 250 bps ``VENUE_EXTRA_BPS`` and never read the token's own
``creatorTaxBps()`` -- 0-500 bps per token on chain (``viability.read_pons_venue``). Per leg, paper =
broker 60 + router 100 + 250 = 410; live = router 100 + ``feeBps`` 100 + creator tax. Every token with
a creator tax above 210 bps was under-charged, by up to ~290 bps a leg.

Now ``evm_price.read_pons`` reads ``creatorTaxBps()`` in the SAME batch (one more item, no extra call),
and a curve leg pays ``max(250, feeBps + creatorTax - the broker's own robinhood fee)``; when the tax
cannot be read the flat stand-in is charged and the leg is tagged NOT honest.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain, now_ms
from kaiba.execution import engine
from kaiba.execution import evm_pool_price as m
from kaiba.execution import evm_price as ep
from kaiba.execution import paper_costs as pc
from kaiba.execution.paper import DEFAULT_FEE_BPS, fill_basis, load_position
from tests.test_evm_price_source import PONS_LIVE, _word
from tests.test_paper_entry_quote_evm import LANDED, priced_event, rh_handoff
from tests.test_paper_exit_evm import QTY, last_sell_basis, mark, sell
from tests.test_watchdog import make_position, risk_file  # noqa: F401

ETH_USD = Decimal("2500")
CURVE = "0x" + "a2" * 20
TOKEN = "0x" + "a1" * 20


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from kaiba.execution import viability

    m.clear_caches()
    engine._POOL_REFUSALS.clear()
    ep._PONS_CURVE.clear()
    monkeypatch.setattr(viability, "paper_native_usd",
                        lambda conn, at_ms=None, chains=None: {Chain.ROBINHOOD: ETH_USD, Chain.BSC: Decimal("600")})
    yield
    ep._PONS_CURVE.clear()
    m.clear_caches()


def live_leg_bps(creator_tax: int, fee: int = 100) -> int:
    """What a live Pons curve leg pays, independent of the code under test: GMGN's router 1% plus the
    curve's ``feeBps`` plus its ``creatorTaxBps`` (``viability.read_pons_venue``'s ``take``)."""
    return 100 + fee + creator_tax


# ----------------------------------------------------------------------------- the read


class PonsRpc:
    """Answers each call by its selector; the creator-tax item answers ``tax`` (None = item failed)."""

    def __init__(self, tax: str | None, *, short: bool = False) -> None:
        from kaiba.ingest import robinhood as rh

        self.table = {sel: "0x" + _word(v) for (_name, sel), v in zip(rh.CURVE_READS, PONS_LIVE, strict=True)}
        self.table[ep.SEL_DECIMALS] = "0x" + _word(18)
        self.table[ep.PONS_CREATOR_TAX_SELECTOR] = tax
        self.short = short
        self.batches: list[list[tuple[str, str]]] = []

    def __call__(self, calls):
        self.batches.append(list(calls))
        out = [self.table.get(data) for _to, data in calls]
        return out[:-1] if self.short else out


def test_the_creator_tax_selector_is_the_one_viability_reads():
    from kaiba.execution import viability

    assert ep.PONS_CREATOR_TAX_SELECTOR == viability.PONS_CREATOR_TAX_SELECTOR == "0xc1bb8901"


def test_read_pons_reads_the_creator_tax_in_the_same_batch(monkeypatch):
    from kaiba.ingest import robinhood as rh

    ep._PONS_CURVE[TOKEN] = (CURVE, None)
    rpc = PonsRpc("0x" + _word(500))
    priced, note = ep.read_pons(TOKEN, rpc)
    assert note == "ok" and priced is not None
    assert priced.fee_bps == 100 and priced.creator_tax_bps == 500
    assert len(rpc.batches) == 1, "no extra round trip"
    assert len(rpc.batches[0]) == len(rh.CURVE_READS) + 2
    assert rpc.batches[0][-1] == (CURVE, ep.PONS_CREATOR_TAX_SELECTOR), "asked of the CURVE, last"


@pytest.mark.parametrize("answer,short", [(None, False), ("0x", False), ("0x" + _word(10_000), False), (None, True)])
def test_an_unreadable_creator_tax_is_unknown_and_never_changes_the_mark(answer, short):
    """Protection's mark must not depend on the new item: a failed, empty, out-of-range or missing
    answer leaves ``creator_tax_bps`` None and the price exactly what it was."""
    ep._PONS_CURVE[TOKEN] = (CURVE, None)
    baseline, _ = ep.read_pons(TOKEN, PonsRpc("0x" + _word(0)))
    priced, note = ep.read_pons(TOKEN, PonsRpc(answer, short=short))
    assert note == "ok" and priced is not None
    assert priced.creator_tax_bps is None and priced.fee_bps == 100
    assert priced.price_quote_per_token == baseline.price_quote_per_token


def _priced(creator_tax: int | None, fee: int | None = 100) -> ep.VenuePrice:
    return ep.VenuePrice(price_quote_per_token=Decimal("8e-8"), quote_token=None, quote_reserve_base=2 * 10**18,
                         quote_decimals=18, venue="pons", observed_ms=now_ms(), note="pons curve",
                         pool_id=CURVE, fee_bps=fee, creator_tax_bps=creator_tax)


def test_the_protection_mark_carries_the_curve_take_for_the_paper_exit():
    usd = ep.QuoteUsd(price_usd=lambda chain, token: ETH_USD)
    for tax, take in ((500, 600), (0, 100), (None, None)):
        source = ep.EvmVenuePriceSource(readers={Chain.ROBINHOOD: lambda token, rpc, t=tax: (_priced(t), "ok")},
                                        transports={Chain.ROBINHOOD: lambda calls: []}, quote_usd=usd)
        quote = source.quote(Chain.ROBINHOOD, TOKEN)
        assert quote.usable and quote.source == "evm-venue:pons" and quote.curve_take_bps == take, (tax, quote)


def test_the_curve_paper_entry_carries_the_curve_take(monkeypatch):
    for tax, take in ((500, 600), (None, None)):
        monkeypatch.setattr(ep, "read_pons", lambda token, rpc, t=tax: (_priced(t), "ok"))
        entry, why = m._pons_entry(TOKEN, None, ETH_USD, None)
        assert entry is not None and entry.source == "curve:pons" and entry.curve_take_bps == take, why


# ----------------------------------------------------------------------------- the entry, through handoff


def _curve_entry(take: int | None) -> m.PaperEntry:
    return m.PaperEntry(source="curve:pons", price_usd=Decimal("0.0002"), liquidity_usd=Decimal("5000"),
                        executable_price_usd=None, fill="curve_mark", pool_id=CURVE, found_by="pons_curve",
                        curve_take_bps=take)


def test_a_500_bps_creator_tax_token_pays_its_whole_take_on_the_curve_entry(tmp_db, monkeypatch):
    """Mutant pinned: charging the flat 250 regardless (ignoring ``venue_bps``) must fail here."""
    order, position_id = rh_handoff(tmp_db, monkeypatch, entry=_curve_entry(100 + 500))
    basis = fill_basis(tmp_db, order.order_id)
    broker_fee = int(basis["fee_bps"])
    assert broker_fee == DEFAULT_FEE_BPS[Chain.ROBINHOOD] == 60
    venue = live_leg_bps(500) - 100 - broker_fee  # 540: what is left after the router and the broker
    assert Decimal(basis["quote_price_usd"]) == Decimal("0.0002") * (1 + Decimal(100 + venue) / 10_000)
    # paper per leg (router + venue + broker) == live per leg (router + feeBps + creatorTax)
    assert 100 + venue + broker_fee == live_leg_bps(500) == 700
    event = priced_event(tmp_db)
    assert event["entry_cost_bps"] == 640 and event["curve_take_bps"] == 600
    tag = pc.read_basis(tmp_db, position_id)
    assert tag["entry"]["curve_take_bps"] == 600 and tag["entry"]["venue_bps"] == 540
    # 2026-10-23 (hA_): no executable at our size, so the broker approximates the fill: not counted.
    assert event["honest"] is False and not pc.entry_counts(tag)


def test_a_0_bps_creator_tax_token_keeps_the_flat_stand_in_as_the_floor(tmp_db, monkeypatch):
    """feeBps 100 + creatorTax 0 - broker 60 = 40 < 250: the measured stand-in is never lowered."""
    order, position_id = rh_handoff(tmp_db, monkeypatch, entry=_curve_entry(100))
    basis = fill_basis(tmp_db, order.order_id)
    assert Decimal(basis["quote_price_usd"]) == Decimal("0.0002") * (1 + Decimal(100 + 250) / 10_000)
    assert 100 + 250 + int(basis["fee_bps"]) >= live_leg_bps(0)
    tag = pc.read_basis(tmp_db, position_id)
    assert tag["entry"]["venue_bps"] == 250
    assert not pc.entry_counts(tag) and priced_event(tmp_db)["honest"] is False  # hA_: approximated fill


def test_an_unread_creator_tax_charges_the_flat_stand_in_and_is_never_counted(tmp_db, monkeypatch):
    """Mutant pinned (the writer): recording the entry honest without the take must fail here."""
    order, position_id = rh_handoff(tmp_db, monkeypatch, entry=_curve_entry(None))
    basis = fill_basis(tmp_db, order.order_id)
    assert Decimal(basis["quote_price_usd"]) == Decimal("0.0002") * (1 + Decimal(100 + 250) / 10_000)
    event = priced_event(tmp_db)
    assert event["honest"] is False and event["curve_take_bps"] is None
    tag = pc.read_basis(tmp_db, position_id)
    assert tag["entry"]["honest"] is False and not pc.entry_counts(tag)


def test_a_curve_entry_tag_without_the_take_never_counts_whoever_wrote_it():
    """Mutant pinned (the reader): ``entry_counts`` trusting a tag's own ``honest`` must fail here. A
    tag written before 2026-10-23 (the flat 250) says honest and carries no take."""
    legacy = {"chain": "robinhood", "entry": {"source": "curve:pons", "fill": "curve_mark", "honest": True,
                                              "transfer_tax": pc.TRANSFER_TAX_PONS,
                                              # hA_: an at-size fill, so only the take decides here
                                              "broker": {"basis": "router", "impact_basis": "executable_quote"},
                                              "landing": dict(LANDED)},  # fd_: and a measured landing drift
              "exits": []}
    assert not pc.entry_counts(legacy)
    assert pc.entry_counts({**legacy, "entry": {**legacy["entry"], "curve_take_bps": 600}})
    assert not pc.entry_counts({**legacy, "entry": {**legacy["entry"], "curve_take_bps": True}})


# ----------------------------------------------------------------------------- the exit


RC_TOKEN = "0x" + "7d" * 20


def _curve_position(conn, monkeypatch, *, entry_take: int | None):
    make_position(conn, position_id="pos_rc", token=RC_TOKEN, chain=Chain.ROBINHOOD, qty=QTY, entry="0.0002")
    pc.record_entry_basis(conn, "pos_rc", Chain.ROBINHOOD, source="curve:pons", fill="curve_mark", buy_tax_bps=0,
                          sell_tax_bps=0, tax_in_price=False, transfer_tax=pc.TRANSFER_TAX_PONS,
                          curve_take_bps=entry_take, landing=LANDED)
    conn.commit()

    def no_pool(*a, **k):
        raise AssertionError("a curve-decided exit must not read a pool")

    monkeypatch.setattr(m, "exit_fill", no_pool)
    return load_position(conn, "pos_rc")


def _curve_mark(take: int | None):
    return mark("0.0002", source="evm-venue:pons", chain=Chain.ROBINHOOD, token=RC_TOKEN,
                liquidity="5000").model_copy(update={"curve_take_bps": take})


@pytest.mark.parametrize("quote_take,entry_take", [(600, 600), (None, 600), (100, 600), (600, 100)])
def test_a_500_bps_creator_tax_token_pays_its_whole_take_on_the_curve_exit(
        tmp_db, monkeypatch, risk_file, quote_take, entry_take):  # noqa: F811
    """The HIGHER of the take read with the deciding mark and the one recorded at the entry.
    Mutant pinned: the flat 250 on every curve_mark exit must fail here."""
    position = _curve_position(tmp_db, monkeypatch, entry_take=entry_take)
    assert sell(tmp_db, position, _curve_mark(quote_take)).ok
    basis = last_sell_basis(tmp_db)
    broker_fee = int(basis["fee_bps"])
    venue = live_leg_bps(500) - 100 - broker_fee
    assert venue == 540
    assert Decimal(basis["quote_price_usd"]) == Decimal("0.0002") * (1 - Decimal(100 + venue) / 10_000)
    tag = pc.read_basis(tmp_db, "pos_rc")
    leg = tag["exits"][-1]
    assert leg["fill"] == "curve_mark" and leg["venue_bps"] == 540
    # 2026-10-23 (hA_): sold at the curve MARK, filled on the broker's approximation: not honest.
    assert leg["broker"]["basis"] == "dex" and not leg["honest"] and not pc.is_honest(tag)


def test_a_0_bps_creator_tax_token_exits_at_the_flat_floor(tmp_db, monkeypatch, risk_file):  # noqa: F811
    position = _curve_position(tmp_db, monkeypatch, entry_take=100)
    assert sell(tmp_db, position, _curve_mark(100)).ok
    assert Decimal(last_sell_basis(tmp_db)["quote_price_usd"]) == Decimal("0.0002") * (1 - Decimal(100 + 250) / 10_000)
    leg = pc.read_basis(tmp_db, "pos_rc")["exits"][-1]
    assert leg["venue_bps"] == 250 and not leg["honest"]  # hA_: approximated fill


def test_a_curve_exit_whose_take_is_unread_everywhere_pays_the_flat_stand_in_and_is_not_honest(
        tmp_db, monkeypatch, risk_file):  # noqa: F811
    """Mutant pinned (the exit): a leg whose take could not be read recorded as honest must fail."""
    position = _curve_position(tmp_db, monkeypatch, entry_take=None)
    assert sell(tmp_db, position, _curve_mark(None)).ok
    assert Decimal(last_sell_basis(tmp_db)["quote_price_usd"]) == Decimal("0.0002") * (1 - Decimal(100 + 250) / 10_000)
    tag = pc.read_basis(tmp_db, "pos_rc")
    leg = tag["exits"][-1]
    assert leg["fill"] == "curve_mark" and leg["honest"] is False and "UNREAD" in leg["note"]
    assert not pc.is_honest(tag)
    # a counted entry with such an exit is an exit-stage row (withholds a PASS)
    counted = {**tag, "entry": {**tag["entry"], "honest": True, "curve_take_bps": 100,
                                "broker": {"basis": "router", "impact_basis": "executable_quote"}}}
    assert pc.exit_degraded(counted)


def test_the_venue_rule_never_lowers_the_stand_in_and_subtracts_only_the_broker_fee():
    assert pc.broker_fee_bps(Chain.ROBINHOOD) == DEFAULT_FEE_BPS[Chain.ROBINHOOD]
    assert pc.pons_curve_venue_bps(None) is None
    assert pc.pons_curve_venue_bps(100) == 250 and pc.pons_curve_venue_bps(310) == 250
    assert pc.pons_curve_venue_bps(311) == 251 and pc.pons_curve_venue_bps(600) == 540
    assert pc.pons_curve_take_bps(100, 500) == 600 and pc.pons_curve_take_bps(100, None) is None
    assert pc.pons_curve_take_bps(100, -1) is None and pc.pons_curve_take_bps(100, Decimal("1.5")) is None
    assert pc.exit_bps(Chain.ROBINHOOD, venue_bps=540) == 640 and pc.entry_bps(Chain.ROBINHOOD, venue_bps=540) == 640
    assert pc.exit_bps(Chain.ROBINHOOD, venue_bps=540, venue_fee_in_price=True) == 100
