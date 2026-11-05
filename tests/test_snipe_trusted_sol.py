"""launch-snipe ``trusted_early`` on SOLANA: a pump.fun launch fires when a trusted_copy or
proven wallet's buy is on the ``swaps`` tape before the lane's normal decision moment, the
tape the ``sol_wallets`` stream (``ingest/wallet_stream_sol.py``) writes.

OWNER 2026-10-04 (relayed by the lead): launch sniping on sol should also fire when really
good wallets are among a new token's first buyers. The default ``trusted_early_chains``
stays ``["robinhood"]``; the lead adds "sol" in the box config after review.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from kaiba.core.db import fetch_one, jload
from kaiba.core.schemas import Chain, Lane
from kaiba.execution import snipe
from kaiba.ingest import launch_feed as lf
from kaiba.ingest import wallet_stream_sol as ss
from tests.test_wallet_stream_sol import BLOCK_TIME, SIG, addr, buy_tx

T0 = BLOCK_TIME - 2                    # the launch, two seconds before the trusted buy's block
MINT = addr("MintTokenAAA")            # the token buy_tx moves
CREATOR = addr("CreatorDev")
PROVEN = addr("TrackedWa11etAAA")      # buy_tx's default wallet
TRUSTED = addr("TrustedCopyPick")
STRANGER = addr("StrangerXXX")
P_SOL = {**snipe.DEFAULT_PARAMS, "trusted_early_chains": ["robinhood", "sol"]}
P_DEFAULT = dict(snipe.DEFAULT_PARAMS)
NOBODY = snipe.Record("w", 3, 0, 0, snipe.EvidenceBasis.DERIVED)  # low/no_prior: no record alpha
DECISION_MS = BLOCK_TIME * 1000 + 1500


def sol_launch(**over) -> lf.Launch:
    base = dict(chain=Chain.SOL, token=MINT, venue="pump.fun", creator=CREATOR, launched_ms=T0 * 1000,
                tx="5ig" + "c" * 85, received_ms=T0 * 1000 + 600, meta={"pool": "pump"})
    base.update(over)
    return lf.Launch(**base)


@pytest.fixture
def proven(monkeypatch):
    from kaiba.learning import proven as pv

    members: dict[str, object] = {"value": frozenset({PROVEN})}
    monkeypatch.setattr(pv, "proven_members", lambda conn, chain, **kw: (
        None if members["value"] is None else SimpleNamespace(members=members["value"])))
    return members


def stream_writes_the_buy(conn, *, wallet: str = PROVEN) -> dict:
    """The REAL sol stream books the proven wallet's pump.fun buy of the launch's mint."""
    async def rpc(method, params):
        return buy_tx(wallet=wallet) if method == "getTransaction" else None

    async def no_sleep(_: float) -> None:
        return None

    e = ss.SolWalletStream(rpc, reader=conn, writer=conn, sol_usd=lambda ts: Decimal("200"),
                           clock_ms=lambda: BLOCK_TIME * 1000 + 900, sleep=no_sleep)
    e.wallets = frozenset({wallet})
    (out,) = asyncio.run(e.process_signature(SIG))
    assert out.status == "written"
    return out.row


def test_a_proven_buy_the_sol_stream_booked_before_the_decision_fires_with_the_param(tmp_db, proven):
    row = stream_writes_the_buy(tmp_db)
    assert (row["chain"], row["token"], row["wallet"], row["ts_ms"]) == ("sol", MINT, PROVEN, BLOCK_TIME * 1000)
    early = snipe.trusted_early(tmp_db, sol_launch(), None, snipe.cohorts_for(tmp_db, Chain.SOL, P_SOL),
                                at_ms=DECISION_MS)
    assert early.proven == (PROVEN,) and early.trusted == () and early.sources == ("tape",)
    v = snipe.evaluate(sol_launch(), P_SOL, NOBODY, trusted=early)
    assert v.fire and v.rule == "trusted_early:0/1" and v.strength == P_SOL["record_strength"]
    assert v.features["proven_early"] == 1 and v.features["trusted_early_sources"] == ["tape"]


def test_a_trusted_copy_sol_wallet_counts_as_trusted(tmp_db, proven):
    tmp_db.execute("INSERT INTO wallets (chain, address, cohort, first_seen_ms, last_seen_ms) "
                   "VALUES ('sol', ?, 'trusted_copy', 1, 1)", (TRUSTED,))
    proven["value"] = frozenset()
    stream_writes_the_buy(tmp_db, wallet=TRUSTED)
    early = snipe.trusted_early(tmp_db, sol_launch(), None, snipe.cohorts_for(tmp_db, Chain.SOL, P_SOL),
                                at_ms=DECISION_MS)
    assert early.rule == "trusted_early:1/0"
    assert snipe.evaluate(sol_launch(), P_SOL, NOBODY, trusted=early).fire


def test_without_sol_in_the_param_the_same_tape_does_not_fire(tmp_db, proven):
    stream_writes_the_buy(tmp_db)
    early = snipe.trusted_early(tmp_db, sol_launch(), None, snipe.cohorts_for(tmp_db, Chain.SOL, P_DEFAULT),
                                at_ms=DECISION_MS)
    assert early.count == 1  # the evidence is there ...
    v = snipe.evaluate(sol_launch(), P_DEFAULT, NOBODY, trusted=early)
    assert not v.fire and "trusted_early" not in v.features  # ... and the default ignores it
    assert P_DEFAULT["trusted_early_chains"] == ["robinhood"]


def test_a_buy_after_the_decision_moment_does_not_count(tmp_db, proven):
    stream_writes_the_buy(tmp_db)
    before = BLOCK_TIME * 1000 - 1  # decided a millisecond before the buy's block
    early = snipe.trusted_early(tmp_db, sol_launch(), None, snipe.cohorts_for(tmp_db, Chain.SOL, P_SOL),
                                at_ms=before)
    assert early.count == 0
    assert not snipe.evaluate(sol_launch(), P_SOL, NOBODY, trusted=early).fire


def test_a_stranger_or_the_creator_is_not_a_known_early_buyer(tmp_db, proven):
    stream_writes_the_buy(tmp_db, wallet=STRANGER)
    cohorts = snipe.cohorts_for(tmp_db, Chain.SOL, P_SOL)
    assert snipe.trusted_early(tmp_db, sol_launch(), None, cohorts, at_ms=DECISION_MS).count == 0
    proven["value"] = frozenset({CREATOR})
    tmp_db.execute("UPDATE swaps SET wallet=?", (CREATOR,))
    assert snipe.trusted_early(tmp_db, sol_launch(), None, snipe.cohorts_for(tmp_db, Chain.SOL, P_SOL),
                               at_ms=DECISION_MS).count == 0  # the dev's own buy is dev_watchlist's rule


def test_every_veto_still_applies_to_a_sol_trusted_early_fire(tmp_db, proven):
    stream_writes_the_buy(tmp_db)
    early = snipe.trusted_early(tmp_db, sol_launch(), None, snipe.cohorts_for(tmp_db, Chain.SOL, P_SOL),
                                at_ms=DECISION_MS)
    assert snipe.evaluate(sol_launch(), P_SOL, NOBODY, trusted=early).fire
    spam = snipe.Record("w", 80, 5, 0, snipe.EvidenceBasis.DERIVED)
    assert "deployer_record:spam/all_dud" in snipe.evaluate(sol_launch(), P_SOL, spam, trusted=early).reasons
    capped = snipe.evaluate(sol_launch(), {**P_SOL, "max_snipes_per_day": {"sol": 2}}, NOBODY, trusted=early,
                            snipes_today=2)
    assert "daily_snipe_cap:2/2" in capped.reasons and not capped.fire
    other = snipe.evaluate(sol_launch(venue="moonshot"), P_SOL, NOBODY, trusted=early)
    assert "venue_not_enabled:moonshot" in other.reasons and not other.fire
    off = snipe.evaluate(sol_launch(), {**P_SOL, "chains": ["robinhood"]}, NOBODY, trusted=early)
    assert off.reasons == ["chain_not_enabled:sol"]
    assert not snipe.evaluate(sol_launch(), {**P_SOL, "trusted_early_min": 2}, NOBODY, trusted=early).fire


@pytest.mark.skipif(snipe.LANE_VALUE not in {x.value for x in Lane}, reason="Lane 'launch-snipe' not wired")
@pytest.mark.parametrize("chains,fires", [(["robinhood", "sol"], True), (["robinhood"], False)],
                         ids=["sol-enabled", "default"])
def test_the_service_reads_the_sol_tape_at_its_decision_moment_without_waiting(tmp_db, proven, monkeypatch,
                                                                               chains, fires):
    stream_writes_the_buy(tmp_db)
    monkeypatch.setattr(snipe, "get_conn", lambda: tmp_db)
    monkeypatch.setattr(snipe, "params", lambda cfg=None: {**snipe.DEFAULT_PARAMS, "trusted_early_chains": chains})
    monkeypatch.setattr(snipe, "now_ms", lambda: DECISION_MS)

    def forbidden(*a, **k):
        raise AssertionError("the sol decision waited or read the Robinhood book")

    monkeypatch.setattr(snipe, "wait_for_tax", forbidden)
    monkeypatch.setattr(snipe, "read_early_book", forbidden)
    monkeypatch.setattr(snipe, "tx_senders", forbidden)
    monkeypatch.setattr(snipe.time, "sleep", forbidden)
    monkeypatch.setattr(snipe, "paper_entry_sol", lambda launch, p, **kw: {"basis": "pumpfun_curve_model",
                                                                           "quote_in": 370_000_000,
                                                                           "tokens": 10 ** 12, "shadow_quote": 1})
    handed: list[str] = []
    monkeypatch.setattr(snipe, "hand_to_engine",
                        lambda conn, launch, verdict, p, budget, **kw: handed.append(verdict.rule) or (None, "stand_in"))
    sniper = snipe.Sniper(tmp_db, chains=["sol"])
    sniper._sol_count = 19  # the 20th pump.fun launch is always measured, so a skip is recorded too
    sniper._handle(sol_launch())
    obs = fetch_one(tmp_db, f"SELECT * FROM {snipe.TABLE}")
    feats = jload(obs["features_json"])
    if fires:
        assert obs["fire"] == 1 and obs["rule"] == "trusted_early:0/1" and handed == ["trusted_early:0/1"]
        assert feats["proven_early"] == 1 and PROVEN not in obs["features_json"]  # counts, never the address
    else:
        assert obs["fire"] == 0 and handed == [] and "trusted_early" not in feats
