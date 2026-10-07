"""kaiba/execution/snipe.py: what fires, what vetoes, when, and the curve arithmetic."""

from __future__ import annotations

from dataclasses import replace

import pytest

from kaiba.core.schemas import Chain, EvidenceBasis
from kaiba.execution import snipe
from kaiba.ingest import launch_feed as lf
from kaiba.ingest.robinhood import TOPIC_CURVE_BUY

DEV = "0x1a4e077c1abd3fe3674516aeff53ab400e92add7"
CURVE = "0x5575479424e114fafa0cd314115c7897a7a2441b"
LAUNCH_TX = "0x" + "ab" * 32
T0 = 1_791_000_000


def rh_launch(**over) -> lf.Launch:
    base = lf.Launch(chain=Chain.ROBINHOOD, token="0x9122cbc7a76c8518989aae029148d331538dadb7", venue="pons",
                     creator=DEV, curve=CURVE, pair_token="0x" + "0" * 40, graduation_threshold=4 * 10**18,
                     block=1000, launched_ms=T0 * 1000, tx=LAUNCH_TX, name="Dope Cat", symbol="DOPECAT",
                     received_ms=T0 * 1000 + 400)
    return replace(base, **over)


def sol_launch(**over) -> lf.Launch:
    base = lf.Launch(chain=Chain.SOL, token="Mint1111", venue="launchlab", creator="Creator1", name="Moon", symbol="MOON",
                     launched_ms=T0 * 1000, received_ms=T0 * 1000 + 900)
    return replace(base, **over)


def rec(label: str) -> snipe.Record:
    if label == "unknown":
        return snipe.Record(None, 0, 0, 0, EvidenceBasis.UNAVAILABLE)
    band, record = label.split("/")
    launches = {"low": 3, "mid": 20, "spam": 80}[band]
    scored, runners = {"no_prior": (0, 0), "runner": (5, 1), "all_dud": (5, 0)}[record]
    return snipe.Record("w", launches, scored, runners, EvidenceBasis.DERIVED)


CLEAN = snipe.EarlyBook(dev=DEV, dev_buy_wei=5 * 10**16, outside_buys_window=2, read=True)
P = dict(snipe.DEFAULT_PARAMS)


def test_every_param_says_where_its_number_comes_from():
    assert set(snipe.DEFAULT_PARAMS) == set(snipe.PARAMS_PROVENANCE)


@pytest.mark.parametrize("label,fires", [("low/runner", True), ("mid/runner", True), ("low/no_prior", False),
                                         ("mid/all_dud", False), ("spam/runner", False), ("unknown", False)])
def test_a_deployer_record_fires_only_on_the_measured_runner_cells(label, fires):
    v = snipe.evaluate(sol_launch(), P, rec(label))
    assert v.fire is fires, v.reasons
    if fires:
        assert v.rule == f"record:{label}" and v.strength >= 0.70


def test_fire_on_records_can_differ_per_chain():
    """2026-10-04: robinhood mid/runner measured BELOW baseline (O/E 0.80), sol mid/runner above
    (1.26). A {chain: [labels]} mapping keeps one lane config honest on both chains."""
    per_chain = {**P, "fire_on_records": {"sol": ["low/runner", "mid/runner"], "robinhood": ["low/runner"]}}
    assert snipe.evaluate(sol_launch(), per_chain, rec("mid/runner")).fire
    rh = snipe.evaluate(rh_launch(), per_chain, rec("mid/runner"), book=CLEAN)
    assert any(r.startswith("no_alpha") for r in rh.reasons), rh.reasons
    rh_low = snipe.evaluate(rh_launch(), per_chain, rec("low/runner"), book=CLEAN)
    assert rh_low.rule == "record:low/runner"
    # a chain missing from the mapping fires on no record
    only_sol = {**P, "fire_on_records": {"sol": ["low/runner"]}}
    assert any(r.startswith("no_alpha") for r in snipe.evaluate(rh_launch(), only_sol, rec("low/runner"), book=CLEAN).reasons)


def test_never_records_can_differ_per_chain():
    per_chain = {**P, "never_records": {"robinhood": ["spam/all_dud"]}}
    rh = snipe.evaluate(rh_launch(), per_chain, rec("spam/all_dud"), book=CLEAN)
    assert "deployer_record:spam/all_dud" in rh.reasons
    sol = snipe.evaluate(sol_launch(), per_chain, rec("spam/all_dud"))
    assert "deployer_record:spam/all_dud" not in sol.reasons


def test_watchlists_fire_and_an_explicit_dev_overrides_a_bad_record_but_a_name_does_not():
    p = {**P, "dev_watchlist": ["CREATOR1"], "name_watchlist": []}
    v = snipe.evaluate(sol_launch(creator="creator1"), p, rec("spam/all_dud"))
    assert v.fire and v.rule == "dev_watchlist"
    p = {**P, "name_watchlist": [{"text": " moon ", "exact": True, "field": "symbol"}]}
    assert snipe.evaluate(sol_launch(), p, rec("low/no_prior")).rule == "name_watchlist"
    v = snipe.evaluate(sol_launch(), p, rec("spam/all_dud"))
    assert not v.fire and "deployer_record:spam/all_dud" in v.reasons


def test_name_targets_match_exactly_or_by_contains_in_the_field_asked():
    launch = sol_launch(name="Moon Dog", symbol="MDOG")
    assert snipe.name_hits(launch, [{"text": "moon dog", "exact": True, "field": "name"}]) == ["moon dog"]
    assert snipe.name_hits(launch, [{"text": "moon", "exact": True, "field": "name"}]) == []
    assert snipe.name_hits(launch, [{"text": "moon", "exact": False, "field": "either"}]) == ["moon"]
    assert snipe.name_hits(launch, [{"text": "mdog", "exact": True, "field": "name"}]) == []
    assert snipe.name_hits(launch, ["MDOG"]) == ["mdog"]


def test_a_venue_or_chain_not_enabled_never_fires():
    assert "venue_not_enabled:pump.fun" in snipe.evaluate(sol_launch(venue="pump.fun"), {**P, "venues": {"sol": ["launchlab"]}}, rec("low/runner")).reasons
    assert snipe.evaluate(sol_launch(), {**P, "chains": ["robinhood"]}, rec("low/runner")).reasons == ["chain_not_enabled:sol"]


def test_robinhood_vetoes_an_exempt_first_second_buyer():
    book = replace(CLEAN, exempt_buyers=["0x" + "77" * 20])
    v = snipe.evaluate(rh_launch(), P, rec("low/runner"), book=book)
    assert not v.fire and "exempt_buyer:1" in v.reasons
    assert snipe.evaluate(rh_launch(), {**P, "exempt_buyer_veto": False}, rec("low/runner"), book=book).fire


def test_robinhood_vetoes_a_small_dev_buy_only_when_nobody_else_wanted_it():
    small = replace(CLEAN, dev_buy_wei=10**16, outside_buys_window=0)
    assert "dev_atomic_no_demand" in snipe.evaluate(rh_launch(), P, rec("low/runner"), book=small).reasons
    assert snipe.evaluate(rh_launch(), P, rec("low/runner"), book=replace(small, outside_buys_window=1)).fire
    # a token pair's dev buy is not in wei of ETH: the screen does not apply
    assert snipe.evaluate(rh_launch(pair_token="0x" + "55" * 20), P, rec("low/runner"), book=small).fire


def test_robinhood_without_an_early_book_does_not_fire():
    assert "early_book_unread" in snipe.evaluate(rh_launch(), P, rec("low/runner"), book=None).reasons


def test_the_daily_cap_stops_a_firing_launch():
    v = snipe.evaluate(sol_launch(), {**P, "max_snipes_per_day": {"sol": 3}}, rec("low/runner"), snipes_today=3)
    assert not v.fire and "daily_snipe_cap:3/3" in v.reasons


def buy_log(*, block, quote_in, toll_bps, recipient, tx="0x" + "cd" * 32, trader=None):
    fee = quote_in * (snipe.PONS_PROTOCOL_FEE_BPS + toll_bps) // 10_000
    topic = lambda a: "0x" + a.removeprefix("0x").rjust(64, "0")  # noqa: E731
    return {"address": CURVE, "topics": [TOPIC_CURVE_BUY, topic(trader or recipient), topic(recipient)],
            "data": "0x" + "".join(format(x, "064x") for x in (quote_in, 10**21, fee, 0)),
            "blockNumber": hex(block), "transactionHash": tx, "logIndex": "0x0"}


def test_the_early_book_tells_an_exempt_buyer_from_a_taxed_one_and_counts_demand():
    blocks = {1000: T0, 1005: T0, 1012: T0 + 1, 1030: T0 + 3}
    logs = [
        buy_log(block=1000, quote_in=5 * 10**16, toll_bps=0, recipient=DEV, tx=LAUNCH_TX),  # the dev, atomic
        buy_log(block=1005, quote_in=10**17, toll_bps=0, recipient="0x" + "aa" * 20),       # second 0, untaxed: exempt
        buy_log(block=1012, quote_in=10**17, toll_bps=618, recipient="0x" + "bb" * 20),     # second 1, taxed
        buy_log(block=1030, quote_in=10**17, toll_bps=0, recipient="0x" + "cc" * 20),       # second 3: no toll owed
    ]
    book = snipe.early_book(rh_launch(), logs, tx_from=DEV, block_ts=blocks, demand_window_s=2, exempt_toll_bps=100)
    assert book.dev_buy_wei == 5 * 10**16
    assert book.exempt_buyers == ["0x" + "aa" * 20]
    assert book.outside_buys_window == 2


def test_the_wait_ends_on_the_chain_clock_at_the_first_second_under_the_limit(monkeypatch):
    heads = iter([T0 + 1, T0 + 2, T0 + 3, T0 + 9])
    seen = []

    def fake_rpc(calls, **kw):
        ts = next(heads)
        seen.append(ts)
        return [{"timestamp": hex(ts), "number": hex(2000 + len(seen))}]

    monkeypatch.setattr(snipe, "rh_rpc", fake_rpc)
    ts, block = snipe.wait_for_tax(rh_launch(), {**P, "max_entry_tax_bps": 0}, sleep=lambda s: None, wall=lambda: T0 + 4)
    assert ts == T0 + 3 and block == 2003
    heads = iter([T0, T0 + 1])
    seen.clear()
    ts, _ = snipe.wait_for_tax(rh_launch(), {**P, "max_entry_tax_bps": 618}, sleep=lambda s: None, wall=lambda: T0 + 4)
    assert ts == T0 + 1


def test_a_stalled_chain_gives_up_instead_of_waiting_forever(monkeypatch):
    calls = []

    def stalled(calls_, **kw):
        calls.append(1)
        assert len(calls) < 200, "the wait never gave up on a stalled head"
        return [{"timestamp": hex(T0), "number": hex(1)}]

    monkeypatch.setattr(snipe, "rh_rpc", stalled)
    clock = iter(range(0, 1000))
    assert snipe.wait_for_tax(rh_launch(), {**P, "chain_wait_max_s": 5}, sleep=lambda s: None,
                              clock=lambda: next(clock), wall=lambda: T0 + 10) == (None, None)
    assert len(calls) <= 6


def test_record_for_reads_a_first_launch_as_no_prior_and_stale_stats_as_unknown(tmp_db):
    from kaiba.intelligence.deployer import ensure_table

    ensure_table(tmp_db)
    assert snipe.record_for(tmp_db, Chain.SOL, "NewDev").label == "low/no_prior"
    tmp_db.execute("INSERT INTO deployer_stats (chain, wallet, launches, scored, runners, best_multiple, computed_ms) "
                   "VALUES ('sol', 'Good', 20, 6, 2, '3.1', ?)", (T0 * 1000,))
    assert snipe.record_for(tmp_db, Chain.SOL, "Good", at_ms=T0 * 1000 + 60_000).label == "mid/runner"
    assert snipe.record_for(tmp_db, Chain.SOL, "Good", at_ms=T0 * 1000 + 10**9).label == "unknown"
    assert snipe.record_for(tmp_db, Chain.SOL, None).label == "unknown"


def test_with_holding_is_exact_when_everyone_else_only_bought():
    q, t = 168 * 10**16, 10**27
    def buy(q, t, net):
        out = net * t // (q + net)
        return q + net, t - out, out
    q1, t1, mine = buy(q, t, 5 * 10**16)
    added = snipe.quote_for_tokens(q, t, mine)
    with_us = buy(*buy(q1, t1, 2 * 10**18)[:2], 3 * 10**17)[:2]
    without = buy(*buy(q, t, 2 * 10**18)[:2], 3 * 10**17)[:2]
    got = snipe.with_holding(*without, added)
    assert abs(got[0] - with_us[0]) * 10**9 // with_us[0] < 10
    assert abs(got[1] - with_us[1]) * 10**9 // with_us[1] < 10


def test_selling_back_takes_fee_and_creator_tax_and_never_more_than_the_real_reserve():
    gross = 10**24 * 2 * 10**18 // (900 * 10**24 + 10**24)
    assert snipe.pons_sell_quote(2 * 10**18, 900 * 10**24, 10**24, 100, 200) == gross - gross * 100 // 10_000 - gross * 200 // 10_000
    assert snipe.pons_sell_quote(2 * 10**18, 900 * 10**24, 10**27, 100, 0, real_quote_reserve=10**15) == 10**15
    assert snipe.pons_sell_quote(0, 1, 1, 100, 0) == 0


def test_the_buy_calldata_is_the_curve_buy_selector_and_three_words():
    data = snipe.buy_calldata(10**16, 0, "0x" + "ab" * 20)
    assert data.startswith(snipe.PONS_BUY_SELECTOR) and len(data) == 10 + 3 * 64
    assert data.endswith("ab" * 20)


def test_the_wait_sleeps_on_our_clock_first_and_reads_the_chain_once(monkeypatch):
    slept, reads = [], []
    monkeypatch.setattr(snipe, "rh_rpc", lambda calls, **kw: reads.append(1) or [{"timestamp": hex(T0 + 3), "number": hex(7)}])
    got = snipe.wait_for_tax(rh_launch(), {**P, "max_entry_tax_bps": 0, "chain_clock_margin_ms": 250},
                             sleep=slept.append, wall=lambda: T0 + 0.4)
    assert got == (T0 + 3, 7) and len(reads) == 1
    assert slept and abs(slept[0] - 2.85) < 1e-6  # until T0+3.25 on our clock, then one chain read
