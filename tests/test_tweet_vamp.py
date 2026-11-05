"""Vamp context, exact metadata, native budgets and shared intent arbitration."""
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from kaiba.core import db
from kaiba.core.schemas import Chain, EvidenceBasis, Receipt
from kaiba.execution import tweet_launch as tl
from kaiba.execution import tweet_vamp as vamp
from kaiba.ingest.x_stream import XPost

NOW = 1_791_300_000_000
ADDR = "7UvmpTokenAddress1111111111111111111111111111"


def config(chain=Chain.SOL):
    cfg = tl.load_config()
    return replace(cfg, mode="shadow", accounts={"elonmusk": (chain,)}, namer_enabled=False,
                   logo_generate=False)


def post():
    return XPost("2045879341243043889", "elonmusk", "Kekius", "post",
                 ("https://pbs.twimg.com/media/different.jpg",), NOW-1000, NOW, 50, "fixture")


def info(**changes):
    return {**dict(address=ADDR, name="Kekius", symbol="KEKIUS", logo="https://cdn.example/logo.png",
                   creation_timestamp=NOW//1000, price={"volume_5m": "1234.56", "swaps_5m": 4}), **changes}


def candidate(chain=Chain.SOL):
    return vamp.from_gmgn(chain, ADDR, info(), Receipt(provider="gmgn", endpoint="token.info", observed_at_ms=NOW))


@pytest.mark.parametrize("chain", [Chain.SOL, Chain.BSC, Chain.ROBINHOOD])
def test_exact_metadata_and_shared_configuration(chain):
    c = config(chain)
    source = candidate(chain)
    p = vamp.plan_vamp(post(), source, c, now_ms=NOW)
    assert (p.name, p.symbol, p.image_url) == (source.name, source.symbol, source.image_url)
    assert p.launch_id == f"tl:{post().tweet_id}:{chain.value}" and p.mode == "shadow"
    assert p.dex == c.chains[chain].dex and p.supply_pct == 5
    assert p.buy_amt_native == tl.dev_buy_native(c.chains[chain], 5)[0]
    assert p.buy_amt_native <= vamp.APPROVED_CAPS[chain][0]
    assert p.argv[p.argv.index("--image-url")+1] == source.image_url
    assert p.argv[p.argv.index("--twitter")+1] == post().url
    assert "--sell-configs" not in p.argv
    assert "Inspired by a post" in p.argv[p.argv.index("--description")+1]


@pytest.mark.parametrize("stats", [{}, {"volume_5m": None, "swaps_5m": 1},
                                  {"volume_5m": "NaN", "swaps_5m": 1},
                                  {"volume_5m": "Infinity", "swaps_5m": 1},
                                  {"volume_5m": "1", "swaps_5m": True},
                                  {"volume_5m": "1", "swaps_5m": 1.5}])
def test_missing_or_invalid_report_never_becomes_zero_volume(stats):
    with pytest.raises(ValueError):
        vamp.from_gmgn(Chain.SOL, ADDR, info(price=stats), candidate().receipt)


@pytest.mark.parametrize("changes,reason", [
    ({"volume_5m_usd": Decimal(0)}, "no_qualifying_volume"),
    ({"swaps_5m": 0}, "no_qualifying_volume"),
    ({"name": "Unrelated"}, "tweet_context_unmatched"),
    ({"name": "x"*33}, "unsupported_source_name"),
    ({"name": "run\ncommand"}, "unsupported_source_name"),
    ({"symbol": "BTC"}, "unsupported_source_symbol"),
    ({"image_url": "file:///credentials"}, "source_image_unavailable"),
    ({"created_ms": NOW-2000}, "source_not_a_fresh_tweet_launch"),
    ({"created_ms": NOW+1}, "source_not_a_fresh_tweet_launch"),
])
def test_rejection_reasons(changes, reason):
    with pytest.raises(ValueError, match=reason):
        vamp.plan_vamp(post(), replace(candidate(), **changes), config(), now_ms=NOW)


def test_volume_threshold_exact_boundary_and_positive_control():
    source = replace(candidate(), volume_5m_usd=Decimal("1000"))
    assert vamp.plan_vamp(post(), source, config(), now_ms=NOW, min_volume_usd=Decimal("1000")).verdict == "launch"
    with pytest.raises(ValueError, match="no_qualifying_volume"):
        vamp.plan_vamp(post(), replace(source, volume_5m_usd=Decimal("999.999")), config(), now_ms=NOW,
                       min_volume_usd=Decimal("1000"))


@pytest.mark.parametrize("receipt", [Receipt(provider="gmgn", endpoint="token.info", observed_at_ms=NOW-21000),
                                     Receipt(provider="gmgn", endpoint="token.info", observed_at_ms=NOW+1),
                                     Receipt(provider="gmgn", endpoint="token.info", observed_at_ms=NOW,
                                             basis=EvidenceBasis.UNAVAILABLE)])
def test_stale_future_and_unavailable_evidence(receipt):
    with pytest.raises(ValueError):
        vamp.plan_vamp(post(), replace(candidate(), receipt=receipt), config(), now_ms=NOW)


def test_literal_contract_link_and_original_freshness_rules():
    p = replace(post(), text="new coin "+ADDR)
    assert vamp.plan_vamp(p, candidate(), config(), now_ms=NOW).verdict == "launch"
    for bad in [replace(p, author="unwatched"), replace(p, kind="reply"), replace(p, created_ms=NOW-21000)]:
        with pytest.raises(ValueError):
            vamp.plan_vamp(bad, candidate(), config(), now_ms=NOW)
    with pytest.raises(ValueError, match="source_is_ours"):
        vamp.plan_vamp(p, candidate(), config(), now_ms=NOW, owned_tokens=frozenset({ADDR}))


def test_caps_cannot_be_widened_or_daily_cap_omitted():
    c = config()
    route = replace(c.chains[Chain.SOL], max_buy_native=Decimal("1.500000001"))
    with pytest.raises(ValueError, match="outside_authorized_launch_limits"):
        vamp.plan_vamp(post(), candidate(), replace(c, chains={Chain.SOL: route}), now_ms=NOW)
    with pytest.raises(ValueError, match="outside_authorized_launch_limits"):
        vamp.plan_vamp(post(), candidate(), replace(c, daily_native_cap={}), now_ms=NOW)


def test_new_vamp_and_normal_launch_share_unique_intent(tmp_db):
    p = vamp.plan_vamp(post(), candidate(), config(), now_ms=NOW)
    assert vamp.record_vamp(tmp_db, post(), p)
    assert not vamp.record_vamp(tmp_db, post(), p)
    normal = tl.plan_post(post(), config(), now_ms=NOW)[0]
    tl.record_plan(tmp_db, normal)
    assert tmp_db.execute("SELECT COUNT(*) FROM tweet_launches").fetchone()[0] == 1
    assert tmp_db.execute("SELECT image_url FROM tweet_launches").fetchone()[0] == candidate().image_url


def test_normal_launch_wins_over_vamp(tmp_db):
    normal = tl.plan_post(post(), config(), now_ms=NOW)[0]
    tl.record_post(tmp_db, post())
    tl.record_plan(tmp_db, normal)
    p = vamp.plan_vamp(post(), candidate(), config(), now_ms=NOW)
    assert not vamp.record_vamp(tmp_db, post(), p)
    assert tmp_db.execute("SELECT image_url FROM tweet_launches").fetchone()[0] == post().media[0]


@pytest.mark.parametrize("state", ["planned", "submitting", "submitted", "ambiguous", "failed", "confirmed"])
def test_skip_with_any_send_state_is_never_reused(tmp_db, state):
    normal = tl.plan_post(post(), config(), now_ms=NOW)[0]
    normal.verdict = "skip"
    tl.record_post(tmp_db, post())
    tl.record_plan(tmp_db, normal)
    tmp_db.execute("UPDATE tweet_launches SET state=?", (state,))
    p = vamp.plan_vamp(post(), candidate(), config(), now_ms=NOW)
    assert not vamp.record_vamp(tmp_db, post(), p)


def test_two_connections_claim_an_untouched_skip_once(tmp_db):
    normal = tl.plan_post(post(), config(), now_ms=NOW)[0]
    normal.verdict = "skip"
    tl.record_post(tmp_db, post())
    tl.record_plan(tmp_db, normal)
    path = tmp_db.execute("PRAGMA database_list").fetchone()[2]
    other = db.connect(Path(path))
    try:
        first = vamp.plan_vamp(post(), candidate(), config(), now_ms=NOW)
        second = vamp.plan_vamp(post(), replace(candidate(), address="OtherSource"), config(), now_ms=NOW)
        assert vamp.record_vamp(tmp_db, post(), first)
        assert not vamp.record_vamp(other, post(), second)
        row = other.execute("SELECT reasons_json FROM tweet_launches").fetchone()
        assert f"vamp_source:{ADDR}" in json.loads(row[0])
        assert not tmp_db.in_transaction and not other.in_transaction
    finally:
        other.close()
