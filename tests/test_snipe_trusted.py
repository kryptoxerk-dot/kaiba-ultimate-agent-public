"""launch-snipe ``trusted_early``: a launch fires when a known-good wallet is among its early
buyers by the lane's normal decision moment -- the DATABASE's ``trusted_copy`` cohort or the
newest frozen proven cohort, never a feed's claim -- and every existing veto still applies.

OWNER 2026-10-04 (relayed by the lead): "check really really good wallets as confluences to
snipe". Robinhood first; sol has no free trade stream for brand-new pump.fun tokens.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from kaiba.core.db import fetch_one, jload
from kaiba.core.schemas import Chain, Lane
from kaiba.execution import snipe
from kaiba.ingest import launch_feed as lf
from kaiba.ingest.robinhood import TOPIC_CURVE_BUY

T0 = 1_791_000_000
DEV = "0x1a4e077c1abd3fe3674516aeff53ab400e92add7"
CURVE = "0x5575479424e114fafa0cd314115c7897a7a2441b"
TOKEN = "0x9122cbc7a76c8518989aae029148d331538dadb7"
LAUNCH_TX = "0x" + "ab" * 32
ROUTER = "0x" + "70" * 20
TRUSTED = "0x" + "7e" * 20
PROVEN = "0x" + "9e" * 20
STRANGER = "0x" + "55" * 20
P = dict(snipe.DEFAULT_PARAMS)
NOBODY = snipe.Record("w", 3, 0, 0, snipe.EvidenceBasis.DERIVED)  # low/no_prior: no record alpha


def rh_launch(**over) -> lf.Launch:
    base = lf.Launch(chain=Chain.ROBINHOOD, token=TOKEN, venue="pons", creator=DEV, curve=CURVE,
                     pair_token="0x" + "0" * 40, graduation_threshold=4 * 10**18, block=1000,
                     launched_ms=T0 * 1000, tx=LAUNCH_TX, received_ms=T0 * 1000 + 400)
    return replace(base, **over)


def buy_log(*, block, recipient, trader=None, tx="0x" + "cd" * 32, quote_in=10**17, toll_bps=0):
    fee = quote_in * (snipe.PONS_PROTOCOL_FEE_BPS + toll_bps) // 10_000
    topic = lambda a: "0x" + a.removeprefix("0x").rjust(64, "0")  # noqa: E731
    return {"address": CURVE, "topics": [TOPIC_CURVE_BUY, topic(trader or recipient), topic(recipient)],
            "data": "0x" + "".join(format(x, "064x") for x in (quote_in, 10**21, fee, 0)),
            "blockNumber": hex(block), "transactionHash": tx, "logIndex": "0x0"}


def book_of(*logs) -> snipe.EarlyBook:
    blocks = {1000: T0, 1010: T0 + 1, 1030: T0 + 3}
    return snipe.early_book(rh_launch(), list(logs), tx_from=DEV, block_ts=blocks, demand_window_s=2, exempt_toll_bps=100)


def no_signers(*a, **k):
    raise AssertionError("signers read although the book or the tape already named a known wallet")


def wallet(conn, address, cohort):
    conn.execute("INSERT INTO wallets (chain, address, cohort, first_seen_ms, last_seen_ms) VALUES ('robinhood',?,?,1,1)",
                 (address, cohort))


@pytest.fixture
def proven(monkeypatch):
    """The newest frozen proven cohort, as ``proven.proven_members`` returns it."""
    from kaiba.learning import proven as pv

    members: dict[str, object] = {"value": frozenset({PROVEN})}
    monkeypatch.setattr(pv, "proven_members", lambda conn, chain, **kw: (
        None if members["value"] is None else SimpleNamespace(members=members["value"])))
    return members


def test_the_cohorts_are_the_databases_never_a_feeds_claim(tmp_db, proven):
    wallet(tmp_db, TRUSTED.upper().replace("0X", "0x"), "trusted_copy")  # stored mixed-case: still matched
    wallet(tmp_db, STRANGER, "tracked")
    # a feed row that CLAIMS a cohort is not a cohort (lanes._cohort's rule)
    tmp_db.execute("INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, source) VALUES "
                   "('robinhood','0x01',1,?,?,'buy','gmgn')", (STRANGER, TOKEN))
    c = snipe.cohorts_for(tmp_db, Chain.ROBINHOOD, P)
    assert c.trusted == frozenset({TRUSTED}) and c.proven == frozenset({PROVEN}) and c.note == ""
    proven["value"] = None
    assert snipe.cohorts_for(tmp_db, Chain.ROBINHOOD, P).note == "proven_none_or_stale"


def test_a_trusted_early_buyer_fires_at_the_bottom_rung_under_its_own_rule(tmp_db, proven):
    wallet(tmp_db, TRUSTED, "trusted_copy")
    # bought through a router at +3 s: the log's trader is the router, the recipient is the wallet
    book = book_of(buy_log(block=1000, recipient=DEV, tx=LAUNCH_TX, quote_in=5 * 10**16),
                   buy_log(block=1030, recipient=TRUSTED, trader=ROUTER))
    early = snipe.trusted_early(tmp_db, rh_launch(), book, snipe.cohorts_for(tmp_db, Chain.ROBINHOOD, P),
                                at_ms=T0 * 1000 + 3500, senders=no_signers)
    assert early.trusted == (TRUSTED,) and early.proven == () and early.sources == ("book",)
    v = snipe.evaluate(rh_launch(), P, NOBODY, book=book, trusted=early)
    assert v.fire and v.rule == "trusted_early:1/0" and v.strength == P["record_strength"]
    assert v.features["trusted_early"] == 1 and v.features["trusted_early_sources"] == ["book"]
    # without it -- or with nobody known among the buyers -- the same launch has no alpha at all
    assert not snipe.evaluate(rh_launch(), P, NOBODY, book=book).fire
    assert not snipe.evaluate(rh_launch(), P, NOBODY, book=book, trusted=snipe.TrustedEarly()).fire


def test_proven_and_trusted_are_counted_apart_and_a_wallet_in_both_counts_once(tmp_db, proven):
    wallet(tmp_db, TRUSTED, "trusted_copy")
    proven["value"] = frozenset({PROVEN, TRUSTED})
    book = book_of(buy_log(block=1030, recipient=TRUSTED), buy_log(block=1030, recipient=PROVEN, tx="0x" + "ee" * 32))
    early = snipe.trusted_early(tmp_db, rh_launch(), book, snipe.cohorts_for(tmp_db, Chain.ROBINHOOD, P),
                                at_ms=T0 * 1000 + 3500, senders=no_signers)
    assert early.rule == "trusted_early:1/1"


def test_every_veto_still_applies_to_a_trusted_early_fire(tmp_db, proven):
    wallet(tmp_db, TRUSTED, "trusted_copy")
    cohorts = snipe.cohorts_for(tmp_db, Chain.ROBINHOOD, P)
    clean = book_of(buy_log(block=1000, recipient=DEV, tx=LAUNCH_TX, quote_in=5 * 10**16),
                    buy_log(block=1030, recipient=TRUSTED))
    early = snipe.trusted_early(tmp_db, rh_launch(), clean, cohorts, at_ms=T0 * 1000 + 3500, senders=no_signers)
    assert snipe.evaluate(rh_launch(), P, NOBODY, book=clean, trusted=early).fire
    # the trusted wallet itself bought untaxed in second 0: an exempt insider, vetoed
    exempt = book_of(buy_log(block=1000, recipient=DEV, tx=LAUNCH_TX, quote_in=5 * 10**16),
                     buy_log(block=1000, recipient=TRUSTED, tx="0x" + "ee" * 32))
    e2 = snipe.trusted_early(tmp_db, rh_launch(), exempt, cohorts, at_ms=T0 * 1000 + 3500, senders=no_signers)
    assert e2.count == 1 and "exempt_buyer:1" in snipe.evaluate(rh_launch(), P, NOBODY, book=exempt, trusted=e2).reasons
    spam = snipe.Record("w", 80, 5, 0, snipe.EvidenceBasis.DERIVED)
    assert "deployer_record:spam/all_dud" in snipe.evaluate(rh_launch(), P, spam, book=clean, trusted=early).reasons
    capped = snipe.evaluate(rh_launch(), {**P, "max_snipes_per_day": {"robinhood": 2}}, NOBODY, book=clean,
                            trusted=early, snipes_today=2)
    assert "daily_snipe_cap:2/2" in capped.reasons
    small_dev = book_of(buy_log(block=1000, recipient=DEV, tx=LAUNCH_TX, quote_in=10**15),
                        buy_log(block=1030, recipient=TRUSTED))  # dev < 0.03 ETH, no demand in 2 s
    e3 = snipe.trusted_early(tmp_db, rh_launch(), small_dev, cohorts, at_ms=T0 * 1000 + 3500, senders=no_signers)
    assert "dev_atomic_no_demand" in snipe.evaluate(rh_launch(), P, NOBODY, book=small_dev, trusted=e3).reasons


def test_the_dev_is_not_an_early_buyer(tmp_db, proven):
    wallet(tmp_db, DEV, "trusted_copy")
    proven["value"] = frozenset()
    book = book_of(buy_log(block=1000, recipient=DEV, tx=LAUNCH_TX), buy_log(block=1030, recipient=DEV, tx="0x" + "ee" * 32))
    seen = []
    early = snipe.trusted_early(tmp_db, rh_launch(), book, snipe.cohorts_for(tmp_db, Chain.ROBINHOOD, P),
                                at_ms=T0 * 1000 + 3500, senders=lambda *a, **k: seen.append(1) or {DEV})
    assert early.count == 0 and seen == []  # a watched DEV is dev_watchlist's rule; nothing else to look for


def test_the_tape_counts_only_up_to_the_decision_moment(tmp_db, proven):
    wallet(tmp_db, TRUSTED, "trusted_copy")
    at = T0 * 1000 + 3500
    tmp_db.execute("INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, source) VALUES "
                   "('robinhood','0x02',?,?,?,'buy','alchemy:ws')", (at + 700, TRUSTED, TOKEN))
    cohorts = snipe.cohorts_for(tmp_db, Chain.ROBINHOOD, P)
    later = snipe.trusted_early(tmp_db, rh_launch(), book_of(), cohorts, at_ms=at, senders=lambda *a, **k: set())
    assert later.count == 0  # not waited for: what had not bought yet does not count
    tmp_db.execute("UPDATE swaps SET ts_ms=? WHERE tx='0x02'", (at - 300,))
    now = snipe.trusted_early(tmp_db, rh_launch(), book_of(), cohorts, at_ms=at, senders=no_signers)
    assert now.trusted == (TRUSTED,) and now.sources == ("tape",)


def test_signers_are_read_once_and_only_when_nothing_else_named_a_known_wallet(tmp_db, proven):
    wallet(tmp_db, TRUSTED, "trusted_copy")
    cohorts = snipe.cohorts_for(tmp_db, Chain.ROBINHOOD, P)
    routed = book_of(buy_log(block=1030, recipient=ROUTER, trader=ROUTER, tx="0x" + "11" * 32))  # a 7702 / router trade
    calls = []

    def signers(launch, txs, **kw):
        calls.append(list(txs))
        return {TRUSTED}

    early = snipe.trusted_early(tmp_db, rh_launch(), routed, cohorts, at_ms=T0 * 1000 + 3500, senders=signers)
    assert early.trusted == (TRUSTED,) and early.sources == ("signer",) and calls == [["0x" + "11" * 32]]
    assert snipe.trusted_early(tmp_db, rh_launch(), routed, snipe.Cohorts(), at_ms=T0 * 1000 + 3500,
                               senders=no_signers) == snipe.TrustedEarly()  # nothing to look for: no read
    # a buy older than the window is already on the wallet stream's tape: its signer is not paid for
    old = book_of(buy_log(block=1000, recipient=ROUTER, trader=ROUTER, tx="0x" + "22" * 32))
    assert snipe.trusted_early(tmp_db, rh_launch(), old, cohorts, at_ms=T0 * 1000 + 3500, senders=no_signers,
                               signer_window_s=2).count == 0
    calls.clear()
    snipe.trusted_early(tmp_db, rh_launch(), old, cohorts, at_ms=T0 * 1000 + 3500, senders=signers, signer_window_s=5)
    assert calls == [["0x" + "22" * 32]]


def test_trusted_early_is_robinhood_only_until_sol_has_a_trade_stream():
    early = snipe.TrustedEarly(trusted=("Wallet111",))
    sol = lf.Launch(chain=Chain.SOL, token="Mint1111", venue="pump.fun", creator="C", launched_ms=T0 * 1000,
                    received_ms=T0 * 1000)
    v = snipe.evaluate(sol, P, NOBODY, trusted=early)
    assert not v.fire and "trusted_early" not in v.features
    assert P["trusted_early_chains"] == ["robinhood"]


@pytest.mark.skipif(snipe.LANE_VALUE not in {x.value for x in Lane}, reason="Lane 'launch-snipe' not wired")
def test_the_service_reads_trusted_buyers_at_its_decision_moment_and_records_the_rule(tmp_db, proven, monkeypatch):
    wallet(tmp_db, TRUSTED, "trusted_copy")
    order: list[str] = []
    monkeypatch.setattr(snipe, "get_conn", lambda: tmp_db)
    monkeypatch.setattr(snipe, "params", lambda cfg=None: {**snipe.DEFAULT_PARAMS, "token_row_wait_s": 0.01})
    monkeypatch.setattr(snipe, "wait_for_tax", lambda launch, p, **kw: order.append("tax") or (T0 + 3, 1030))
    book = book_of(buy_log(block=1000, recipient=DEV, tx=LAUNCH_TX, quote_in=5 * 10**16), buy_log(block=1030, recipient=TRUSTED))
    monkeypatch.setattr(snipe, "read_early_book", lambda launch, p, head, **kw: order.append(f"book@{head}") or book)
    real = snipe.cohorts_for
    monkeypatch.setattr(snipe, "cohorts_for", lambda *a, **k: order.append("cohorts") or real(*a, **k))
    monkeypatch.setattr(snipe, "paper_entry_rh", lambda launch, p, **kw: {"basis": "eth_simulateV1", "quote_in": 10**16,
                                                                          "tokens": 10**24, "shadow_quote": 10**16})
    monkeypatch.setattr(snipe, "hand_to_engine", lambda conn, launch, verdict, p, budget, **kw: (None, "stand_in"))
    snipe.Sniper(tmp_db, chains=["robinhood"])._handle(rh_launch())
    assert order == ["tax", "book@1030", "cohorts"]  # after the tax wait and the book: nothing waited for
    obs = fetch_one(tmp_db, f"SELECT * FROM {snipe.TABLE}")
    feats = jload(obs["features_json"])
    assert obs["fire"] == 1 and obs["rule"] == "trusted_early:1/0" and feats["decision_block"] == 1030
    assert feats["trusted_early"] == 1 and TRUSTED not in obs["features_json"]  # counts, never the private address
