"""The hunter digest: what reaches the owner, why, and that it never repeats or writes.

Seeded through the real pipelines where one exists (OpenSea fixture -> ``nft.refresh``,
collector -> ``airdrops.refresh``) so the digest reads exactly the shapes production writes.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from kaiba.core.config import ChainBudget, RiskConfig
from kaiba.core.schemas import Chain, now_ms
from kaiba.hunters import airdrops, digest, nft
from kaiba.hunters.ev import HunterConfig, OpportunityEvidence, OpportunityKind, SybilRisk

FIXTURE = Path(__file__).parent / "fixtures" / "hunters" / "opensea_drops.json"
AT = int(datetime(2026, 10, 2, tzinfo=UTC).timestamp() * 1000)
H = 3_600_000
#: robinhood trading, sol switched off but still holding a bankroll, bsc empty.
RISK = RiskConfig(chains={
    Chain.ROBINHOOD: ChainBudget(enabled=True, bankroll_base_units=10**17),
    Chain.SOL: ChainBudget(enabled=False, bankroll_base_units=8 * 10**9),
    Chain.BSC: ChainBudget(enabled=False, bankroll_base_units=0),
})


def seed_nft(conn, *, liquid_margin: bool = True) -> None:
    b = json.loads(FIXTURE.read_text(encoding="utf-8"))["body"]
    if liquid_margin:
        # early-men-origins: 0.01 ETH floor with 22 sales today -> a real below-floor mint.
        b["stats"]["early-men-origins"]["total"]["floor_price"] = 0.01
    mints = nft.opensea_drops(raw=b["drops"], stats=b["stats"], eth_usd=Decimal("2700"), at_ms=AT)
    nft.refresh(conn, mints=mints, force=True)


def lead(name: str, **kw) -> OpportunityEvidence:
    base = dict(kind=OpportunityKind.POINTS, name=name, sources=["airdrops_io"], url=f"https://{name.lower().replace(' ', '')}.example",
                points_program=True, meta={"excerpt": name, "publication_ms": now_ms() - 2 * H})
    base.update(kw)
    return OpportunityEvidence(**base)


def seed_airdrops(conn) -> None:
    opps = [
        lead("Arcus Points", meta={"excerpt": "Arcus just launched Points Season 1", "publication_ms": now_ms() - H},
             url="https://app.arcus.xyz/ref/AIRDROPSIO"),
        lead("Sol Checker Drop", kind=OpportunityKind.AIRDROP, chain=Chain.SOL, points_program=False,
             meta={"excerpt": "The eligibility checker is live for season 1 claims"}),
        lead("Faraway Points", chain_hint="arbitrum"),
        lead("No Link Points", url=None),
        lead("Bare Mention", kind=OpportunityKind.AIRDROP, points_program=False,
             meta={"excerpt": "a project that might airdrop"}),
        lead("Kyc Points", chain=Chain.SOL, sybil_risk=SybilRisk.HIGH),
        lead("Big Exchange", kind=OpportunityKind.AIRDROP, chain=Chain.SOL, points_program=False,
             sources=["defillama"], meta={"tvl_usd": 2e9, "category": "CEX"}),
        lead("Small Dex", kind=OpportunityKind.AIRDROP, points_program=False, sources=["defillama"],
             meta={"tvl_usd": 3e7, "category": "Dexs", "chains": ["Robinhood"]}),
    ]
    airdrops.refresh(conn, {"test": lambda conn=None: opps}, HunterConfig())


def seed_alpha_and_listings(conn) -> None:
    t = now_ms()
    conn.executemany(
        "INSERT INTO alpha_signals (signal_key, source, kind, subject, title, url, chain, event_at_ms, "
        "first_seen_ms, lead_ms, lead_basis, confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("s1", "crtsh", "cert_subdomain", "claim.monad.xyz", "certificate for claim.monad.xyz",
             "https://crt.sh/?q=claim.monad.xyz", None, None, t - H, 3 * 86_400_000, "prior", 0.6),
            ("s2", "snapshot", "gov_space", "x.eth", "new Snapshot space X", "https://snapshot.org/#/x.eth",
             None, None, t - H, None, "none", 0.15),
            ("s3", "binance", "venue_listing", "ONDO", "Binance Will List Ondo (ONDO)",
             "https://www.binance.com/a1", None, t - 2 * H, t - 2 * H, None, "n/a", 0.9),
            ("s4", "binance", "venue_listing", "CT", "Binance Futures Will Launch CTUSDT Perpetual Contract",
             "https://www.binance.com/a2", None, t - 2 * H, t - 2 * H, None, "n/a", 0.9),
            ("s5", "okx", "venue_listing", "OLD", "OKX to list OLD", "https://okx.example/old", None,
             t - 30 * H, t - 30 * H, None, "n/a", 0.9),
        ],
    )
    conn.executemany(
        "INSERT INTO listing_events (listing_id, exchange, symbol, title, url, chain, token, announced_ms, "
        "detected_ms, latency_ms, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("l1", "binance", "ONDO", "Binance EN: Binance Will List Ondo (ONDO)", "https://t.me/BWEnews/1",
             None, None, t - 2 * H, t - 2 * H + 60_000, 60_000, "bwenews"),
            ("l2", "upbit", "PUMP", "UPBIT LISTING: PUMP (KRW market)", "https://t.me/BWEnews/2", "sol",
             "pumpMint111", t - 3 * H, t - 3 * H + 120_000, 120_000, "bwenews"),
            ("l3", "upbit", "GONE", "UPBIT: GONE 거래 종료", "https://t.me/BWEnews/3", None, None,
             t - H, t - H, 0, "bwenews"),
        ],
    )
    conn.executemany(
        "INSERT INTO radar_finds (radar_key, reported_ms, kind, layer, identity, display_name, chain_slug, tier, "
        "value, unit, basis, floor, headroom, rank_score, tractability, actionable, verdict, payload_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("r1", t - H, "venue", "llama_venues", "robinhood:arcus-perps", "Arcus Perps", "robinhood", 1,
             "70327", "usd_per_day", "provider_reported", "25000", "2.81", "2.81", "full", 1, "NEW venue",
             json.dumps({"slug": "arcus-perps"})),
            ("r2", t - H, "venue", "llama_venues", "megaeth:x", "Unreadable Venue", "megaeth", 1,
             "90000", "usd_per_day", "provider_reported", "25000", "3.6", "0", "none", 0, "not actionable", "{}"),
        ],
    )


@pytest.fixture
def seeded(tmp_db):
    seed_nft(tmp_db)
    seed_airdrops(tmp_db)
    seed_alpha_and_listings(tmp_db)
    tmp_db.commit()
    return tmp_db


def titles(d, kind):
    return [i["title"] for i in d["sections"][kind]["items"]]


# ---------------------------------------------------------------- inclusion and ranking


def test_funded_chains_are_where_money_sits_not_where_trading_is_on():
    assert digest.funded_chains(RISK) == frozenset({"robinhood", "sol"})
    assert digest.funded_chains(object()) == digest.FALLBACK_FUNDED  # unreadable -> fallback


def test_nft_shows_mints_below_their_own_market_and_drops_known_losses(seeded):
    d = digest.build(seeded, now=AT, risk=RISK)
    nft_sec = d["sections"]["nft"]
    assert titles(d, "nft") == ["Early Men", "ROBBIN HOOD"]
    first, second = nft_sec["items"]
    assert first["chain"] == "robinhood" and first["link"] == "https://opensea.io/collection/early-men-origins"
    assert any("~$12.86/mint after 40% haircut and gas" in w for w in first["why"])
    assert any("22 sales in 24h" in w for w in first["why"])
    # A margin that exists only on an ask nobody filled today is shown as exactly that.
    assert any("thin market" in w for w in second["why"]) and first["score"] > second["score"]
    # Mooncats (floor 40.50 x 0.6 < 27 mint) and JPEG Frens (mainnet gas) are known losses;
    # the presale is not a public phase.
    assert nft_sec["dropped"] == {"no_margin_at_floor": 2, "no_public_phase": 1}


def test_an_unfunded_chain_needs_a_traded_margin_to_be_shown(tmp_db):
    b = json.loads(FIXTURE.read_text(encoding="utf-8"))["body"]
    b["stats"]["jpeg-frens"]["total"]["floor_price"] = 0.05  # $135 floor vs $10.26 mint on eth
    mints = nft.opensea_drops(raw=b["drops"], stats=b["stats"], eth_usd=Decimal("2700"), at_ms=AT)
    nft.refresh(tmp_db, mints=mints, force=True)
    d = digest.build(tmp_db, now=AT, risk=RISK)
    jpeg = next(i for i in d["sections"]["nft"]["items"] if i["title"] == "JPEG Frens")
    assert jpeg["why"][0] == "on eth: the agent's EVM key works there but holds no funds"
    assert any("357 sales in 24h" in w for w in jpeg["why"])
    b["stats"]["jpeg-frens"]["intervals"][0]["sales"] = 0  # same margin, nobody trading it today
    tmp_db.execute("DELETE FROM opportunities")
    nft.refresh(tmp_db, mints=nft.opensea_drops(raw=b["drops"], stats=b["stats"], eth_usd=Decimal("2700"),
                                                at_ms=AT), force=True)
    d = digest.build(tmp_db, now=AT, risk=RISK)
    assert "JPEG Frens" not in titles(d, "nft")
    assert d["sections"]["nft"]["dropped"]["unfunded_chain_without_margin"] == 1


def test_an_ended_mint_is_never_shown(seeded):
    later = int(datetime(2026, 10, 30, tzinfo=UTC).timestamp() * 1000)
    d = digest.build(seeded, now=later, since_hours=24 * 60, risk=RISK)
    assert d["sections"]["nft"]["items"] == []
    assert d["sections"]["nft"]["dropped"] == {"mint_ended": 5}  # every stage closed by 10-26


def test_airdrops_rank_funded_chain_radar_and_checker_facts(seeded):
    d = digest.build(seeded, risk=RISK)
    sec = d["sections"]["airdrop"]
    got = {i["title"]: i for i in sec["items"]}
    assert set(got) == {"Arcus Points", "Sol Checker Drop", "Kyc Points", "Faraway Points", "Small Dex"}
    order = titles(d, "airdrop")
    assert order[:2] == ["Sol Checker Drop", "Arcus Points"]
    assert order.index("Kyc Points") < order.index("Faraway Points")
    # TVL alone (p=0.15 speculative) never outranks a live programme on the same chain.
    assert got["Small Dex"]["chain"] == "robinhood" and got["Arcus Points"]["chain"] == "robinhood"
    assert got["Small Dex"]["score"] < got["Arcus Points"]["score"]
    assert "tokenless Dexs, TVL $30.00m (DefiLlama)" in got["Small Dex"]["why"]
    arcus = got["Arcus Points"]["why"]
    assert any(w.startswith("radar: Arcus Perps on robinhood, $70,327/day fees") for w in arcus)
    assert arcus[0] == "REFERRAL link, use the project's own site"
    checker = got["Sol Checker Drop"]["why"]
    assert checker[0] == "on sol, where a Kaiba wallet is funded"
    assert any("checker" in w for w in checker)
    assert any("hard sybil/KYC filtering" in w for w in got["Kyc Points"]["why"])
    assert sec["dropped"] == {"no_programme_link": 1, "no_stated_fact": 1, "institutional_tvl_category": 1}


def test_listings_dedupe_relays_rank_spot_and_drop_delistings(seeded):
    d = digest.build(seeded, risk=RISK)
    sec = d["sections"]["listing"]
    assert titles(d, "listing") == ["PUMP on upbit", "ONDO on binance", "CT on binance"]
    pump, ondo, ct = sec["items"]
    assert any("trades on-chain on sol: pumpMint111" in w for w in pump["why"])
    assert any("perp listing, not spot" in w for w in ct["why"])
    # binance ONDO came from two sources (venue poll + BWEnews relay): one line, earliest kept.
    assert ondo["link"] == "https://www.binance.com/a1"
    assert sec["dropped"] == {"duplicate_of_another_source": 1, "delisting": 1}
    assert "OLD on okx" not in titles(d, "listing")  # outside the 24 h window


def test_alpha_keeps_actionable_radar_and_precise_signals(seeded):
    d = digest.build(seeded, risk=RISK)
    sec = d["sections"]["alpha"]
    assert titles(d, "alpha") == ["Arcus Perps", "certificate for claim.monad.xyz"]
    arcus = sec["items"][0]
    assert arcus["link"] == "https://defillama.com/protocol/arcus-perps"
    assert "on robinhood, where a Kaiba wallet is funded" in arcus["why"]
    assert sec["dropped"] == {"low_precision_prior": 1, "radar_not_actionable": 1}


def test_per_kind_caps_and_counts_what_qualified(seeded):
    d = digest.build(seeded, risk=RISK, per_kind=1)
    assert len(d["sections"]["airdrop"]["items"]) == 1
    assert d["sections"]["airdrop"]["qualifying"] == 5
    text = digest.render(d)
    assert "AIRDROPS/POINTS (5 new, top 1):" in text


# ---------------------------------------------------------------- dedupe and writes


def test_sent_items_are_not_repeated_and_only_mark_sent_writes(seeded):
    before = seeded.execute("SELECT COUNT(*) FROM kv").fetchone()[0]
    first = digest.build(seeded, risk=RISK)
    assert seeded.execute("SELECT COUNT(*) FROM kv").fetchone()[0] == before  # build never writes
    shown = sum(len(s["items"]) for s in first["sections"].values())
    assert digest.mark_sent(seeded, first) == shown > 0
    again = digest.build(seeded, risk=RISK)
    assert all(not s["items"] for s in again["sections"].values())
    assert again["sections"]["airdrop"]["already_sent"] == len(first["sections"]["airdrop"]["items"])
    assert "nothing new" in digest.render(again)


def test_cursor_forgets_after_its_horizon(tmp_db):
    old = now_ms() - digest.CURSOR_KEEP_MS - 1
    tmp_db.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?)",
                   (digest.CURSOR_KEY, json.dumps({"v": 1, "sent": {"airdrop:old": old}}), old))
    digest.mark_sent(tmp_db, {"sections": {"nft": {"items": [{"key": "nft:new"}]}}})
    assert set(digest.read_cursor(tmp_db)) == {"nft:new"}


def test_cli_is_read_only_unless_told_to_mark(seeded, tmp_path, capsys):
    path = Path(seeded.execute("PRAGMA database_list").fetchone()[2])
    seeded.commit()
    assert digest.main(["--db", str(path), "--since-hours", "24"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("HUNTERS last 24h") and "read-only" in out
    assert digest.read_cursor(seeded) == {}
    conn = digest._open(path, writable=False)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES ('x','y',1)")
    conn.close()
    assert digest.main(["--db", str(path), "--mark-sent", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["marked_sent"] > 0
    assert digest.read_cursor(seeded)


def test_a_missing_table_is_reported_not_raised(tmp_db):
    tmp_db.execute("DROP TABLE alpha_signals")
    d = digest.build(tmp_db, risk=RISK)
    assert d["sections"]["listing"]["items"] == []  # listing_events still read
    assert "error" not in d["sections"]["nft"]
    tmp_db.execute("DROP TABLE opportunities")
    d = digest.build(tmp_db, risk=RISK)
    assert d["sections"]["nft"]["error"].startswith("cannot measure")
    assert "cannot measure" in digest.render(d)
