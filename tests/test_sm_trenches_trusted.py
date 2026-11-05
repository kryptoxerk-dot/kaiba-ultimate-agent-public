"""sm-trenches: a trusted_copy wallet is confluence evidence, never a signal on its own.

OWNER DECISION 2026-10-04: "Copy only the tokens the agent wants to. With confluences."
A buyer whose DATABASE cohort is ``trusted_copy`` counts as one smart buyer. Every
threshold is unchanged, so it adds to the count and can never fire the lane alone; entity
independence still collapses two trusted addresses of one operator into one vote; and the
cohort comes from the ``wallets`` table, never from what a feed row claims. The payload
records ``trusted_buyers`` and ``trusted_first`` for the learning loop, with no threshold.

All addresses are synthetic.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from kaiba.core.schemas import Chain, Lane
from kaiba.execution import lanes
from kaiba.execution.lanes import LaneContext
from tests.test_lanes import build_ctx

TOKEN = "0x" + "70" * 20
SMART_A = "0x" + "a1" * 20
SMART_B = "0x" + "b2" * 20
TRUSTED_1 = "0x" + "c3" * 20
TRUSTED_2 = "0x" + "d4" * 20
TRUSTED_3 = "0x" + "e5" * 20
NOISE = "0x" + "f6" * 20


def _wallet(address: str, *, cohort: str = "tracked", tags: list[str] | None = None) -> dict[str, Any]:
    return {"address": address, "cohort": cohort, "tags": tags or []}


def _buy(wallet: str, age_s: int, usd: str = "500", **extra: Any) -> dict[str, Any]:
    return {"wallet": wallet, "side": "buy", "age_s": age_s, "usd_value": usd,
            "amount_native": "100000000000000000", "price_usd": "0.0001",
            "tx": f"0x{wallet[2:6]}{age_s:060d}", **extra}


BASE: dict[str, Any] = {
    "chain": "robinhood",
    "token": TOKEN,
    "wallets": [
        _wallet(SMART_A, tags=["smart_money"]),
        _wallet(SMART_B, tags=["top_trader"]),
        _wallet(TRUSTED_1, cohort="trusted_copy"),
        _wallet(TRUSTED_2, cohort="trusted_copy"),
        _wallet(TRUSTED_3, cohort="trusted_copy"),
        _wallet(NOISE, tags=["fomo"]),
    ],
    "buys": [],
    "dossier": {"built_age_s": 5, "grade": "B", "price_usd": "0.0001",
                "liquidity_usd": "90000", "rug_ratio": "0.05"},
    "token_meta": {"symbol": "TRST", "decimals": 18, "created_age_s": 3600, "launchpad": "pons"},
}


def fixture(*buys: dict[str, Any], wallets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    fx = copy.deepcopy(BASE)
    fx["buys"] = list(buys)
    if wallets is not None:
        fx["wallets"] = wallets
    return fx


def trenches(conn, fx: dict[str, Any], **params: Any):
    return lanes.sm_trenches(build_ctx(conn, fx, params=params or None))


def same_entity(conn, entity_id: str, *addresses: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO entities (entity_id, chain, confidence, size, created_ms, updated_ms) "
        "VALUES (?, 'robinhood', 0.9, ?, 0, 0)", (entity_id, len(addresses)))
    for a in addresses:
        conn.execute("INSERT OR REPLACE INTO entity_members (entity_id, chain, address) VALUES (?, 'robinhood', ?)",
                     (entity_id, a))


# ====================================================================== counts as smart


def test_a_trusted_buyer_completes_confluence_that_two_smart_wallets_cannot(tmp_db):
    fx = fixture(_buy(SMART_A, 300), _buy(SMART_B, 200), _buy(TRUSTED_1, 100))
    signal = trenches(tmp_db, fx)
    assert signal is not None
    assert signal.payload["smart_wallets"] == 3
    assert TRUSTED_1 in signal.wallets
    assert any("trusted_copy" in r for r in signal.reasons)
    # positive control: the same tape with that wallet NOT trusted is one smart wallet short
    fx_untrusted = fixture(*fx["buys"], wallets=[
        w if w["address"] != TRUSTED_1 else _wallet(TRUSTED_1) for w in BASE["wallets"]])
    assert trenches(tmp_db, fx_untrusted) is None


def test_thresholds_are_unchanged_by_trusted_buyers(tmp_db):
    """Same tape, the lane's own min_smart_degen raised by one: trusted does not bend it."""
    fx = fixture(_buy(SMART_A, 300), _buy(SMART_B, 200), _buy(TRUSTED_1, 100))
    assert trenches(tmp_db, fx) is not None
    assert trenches(tmp_db, fx, min_smart_degen=4) is None
    assert trenches(tmp_db, fx, min_independent_entities=4) is None


# ====================================================================== never alone


def test_one_trusted_buyer_alone_never_fires(tmp_db):
    assert trenches(tmp_db, fixture(_buy(TRUSTED_1, 100, usd="50000"))) is None


def test_trusted_buyers_only_still_need_independent_entities(tmp_db):
    """Three trusted addresses run by ONE operator are one opinion: smart count 3, entities 1."""
    fx = fixture(_buy(TRUSTED_1, 300), _buy(TRUSTED_2, 200), _buy(TRUSTED_3, 100))
    same_entity(tmp_db, "ent-owner", TRUSTED_1, TRUSTED_2, TRUSTED_3)
    assert trenches(tmp_db, fx) is None


def test_two_trusted_addresses_of_one_entity_count_once(tmp_db):
    """A + T1 + T2 is three smart wallets but two entities when T1/T2 are one operator."""
    fx = fixture(_buy(SMART_A, 300), _buy(TRUSTED_1, 200), _buy(TRUSTED_2, 100))
    assert trenches(tmp_db, fx, min_independent_entities=3) is not None  # unlinked: 3 entities
    same_entity(tmp_db, "ent-t", TRUSTED_1, TRUSTED_2)
    signal = trenches(tmp_db, fx)
    assert signal is not None and signal.payload["entity_count"] == 2
    assert trenches(tmp_db, fx, min_independent_entities=3) is None


# ====================================================================== the database decides


def test_a_feed_row_claiming_trusted_copy_promotes_nobody(tmp_db):
    """The cohort is read from `wallets`, never from the row. NOISE is `tracked` in the
    database; its row says `trusted_copy` (and carries no smart tag)."""
    fx = fixture(_buy(SMART_A, 300), _buy(SMART_B, 200), _buy(NOISE, 100, cohort="trusted_copy"))
    assert trenches(tmp_db, fx) is None


def test_a_trusted_wallet_with_a_hard_quarantine_tag_does_not_count(tmp_db):
    """`_source_allowed` runs first: curation does not launder a sandwich bot."""
    wallets = [w if w["address"] != TRUSTED_1 else _wallet(TRUSTED_1, cohort="trusted_copy",
                                                           tags=["sandwich_bot"])
               for w in BASE["wallets"]]
    fx = fixture(_buy(SMART_A, 300), _buy(SMART_B, 200), _buy(TRUSTED_1, 100), wallets=wallets)
    assert trenches(tmp_db, fx) is None


def test_a_trusted_wallet_that_sold_out_is_not_a_buyer(tmp_db):
    sell = {**_buy(TRUSTED_1, 50, usd="900"), "side": "sell", "tx": "0x" + "99" * 32}
    fx = fixture(_buy(SMART_A, 300), _buy(SMART_B, 200), _buy(TRUSTED_1, 100), sell)
    assert trenches(tmp_db, fx) is None


def test_an_unpriced_trusted_buy_is_not_conviction(tmp_db):
    """`_net_buyers` reads an unpriced fill as zero conviction; the stream writes NULL
    usd_value when it cannot measure one, and that must not count."""
    fx = fixture(_buy(SMART_A, 300), _buy(SMART_B, 200), _buy(TRUSTED_1, 100, usd=None))
    assert trenches(tmp_db, fx) is None


# ====================================================================== recorded features


def test_trusted_features_are_recorded_and_first_means_first(tmp_db):
    # trusted bought FIRST
    s1 = trenches(tmp_db, fixture(_buy(TRUSTED_1, 400), _buy(SMART_A, 300), _buy(SMART_B, 200)))
    assert s1 is not None
    assert s1.payload["trusted_buyers"] == 1 and s1.payload["trusted_first"] == 1
    assert s1.payload["features_v"] == lanes.ENTRY_FEATURES_VERSION == 2
    # trusted bought LAST
    s2 = trenches(tmp_db, fixture(_buy(SMART_A, 300), _buy(SMART_B, 200), _buy(TRUSTED_1, 100)))
    assert s2.payload["trusted_buyers"] == 1 and s2.payload["trusted_first"] == 0
    # two trusted, no linkage: both counted
    s3 = trenches(tmp_db, fixture(_buy(SMART_A, 300), _buy(TRUSTED_1, 200), _buy(TRUSTED_2, 100)))
    assert s3.payload["trusted_buyers"] == 2 and s3.payload["trusted_first"] == 0
    # none trusted: a real zero, not unknown
    s4 = trenches(tmp_db, fixture(_buy(SMART_A, 300), _buy(SMART_B, 200), _buy(NOISE, 100)),
                  min_smart_degen=2)
    assert s4.payload["trusted_buyers"] == 0 and s4.payload["trusted_first"] == 0


def test_a_trusted_wallet_that_is_also_tagged_smart_is_still_a_trusted_buyer(tmp_db):
    wallets = [w if w["address"] != TRUSTED_1 else _wallet(TRUSTED_1, cohort="trusted_copy",
                                                           tags=["top_trader"])
               for w in BASE["wallets"]]
    s = trenches(tmp_db, fixture(_buy(SMART_A, 300), _buy(SMART_B, 200), _buy(TRUSTED_1, 100),
                                 wallets=wallets))
    assert s is not None and s.payload["trusted_buyers"] == 1
    # it qualified through its tag, so the "counted because trusted" reason is absent
    assert not any("trusted_copy" in r for r in s.reasons)


def test_without_a_connection_the_trusted_features_are_unknown_not_zero():
    ctx = LaneContext(chain=Chain.ROBINHOOD, token=TOKEN, now_ms=10_000_000)
    feats = lanes.entry_features(ctx, window_s=300, smart=[TRUSTED_1], buyers={})
    assert feats["trusted_buyers"] is None and feats["trusted_first"] is None
    assert lanes._trusted_features(ctx, None, None) == {"trusted_buyers": None, "trusted_first": None}


@pytest.mark.parametrize("lane", (Lane.SM_TRENCHES, Lane.CONFLUENCE_5), ids=lambda x: x.value)
def test_no_threshold_is_enforced_on_the_trusted_features(lane):
    """Recorded for measurement only: the learnable keys exist (so the replay gate can
    judge a proposal) and ship None; the shipped risk.yaml sets none of them."""
    from kaiba.core.config import get_risk

    for feature in ("trusted_buyers", "trusted_first"):
        assert feature in lanes.ENTRY_FEATURES
        for side in ("min_", "max_"):
            key = side + feature
            assert key in lanes.FEATURE_THRESHOLD_KEYS[lane]
            assert lanes.DEFAULT_PARAMS[lane][key] is None
            assert key not in (get_risk().lane(lane).params or {})


# ====================================================================== robinhood only


@pytest.mark.parametrize("chain", ["sol", "bsc"])
def test_a_trusted_copy_buyer_off_robinhood_is_not_smart(tmp_db, chain):
    """The owner's instruction was about his ROBINHOOD wallets; bsc holds unrelated
    trusted_copy rows. Same tape that fires on robinhood stays silent elsewhere."""
    fx = fixture(_buy(SMART_A, 300), _buy(SMART_B, 200), _buy(TRUSTED_1, 100))
    fx["chain"] = chain
    assert trenches(tmp_db, fx) is None
    # and with the bar lowered so the two tagged wallets fire it, the trusted wallet is
    # neither counted nor measured
    s = trenches(tmp_db, fx, min_smart_degen=2)
    assert s is not None and TRUSTED_1 not in s.wallets
    assert s.payload["smart_wallets"] == 2
    assert s.payload["trusted_buyers"] is None and s.payload["trusted_first"] is None
    assert not any("trusted_copy" in r for r in s.reasons)
    # positive control on the chain the owner meant
    fx["chain"] = "robinhood"
    assert trenches(tmp_db, fx) is not None


def test_the_chain_set_is_robinhood_alone():
    assert lanes.TRUSTED_COPY_CHAINS == frozenset({Chain.ROBINHOOD})
