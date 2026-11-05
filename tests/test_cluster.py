"""Wallet clustering: hub pruning, edge derivation, entity resolution.

The tests are built around the failure modes the research doc warns about, because those
are what make clustering useless in production rather than merely imperfect:

* a CEX hot wallet must not turn its 50 withdrawal recipients into a 50-wallet cluster;
* co-buying one hyped token must not link anyone, co-buying three must;
* a copier must never be merged into the wallet it copies, or the confluence lane's
  independence count silently becomes a lie.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kaiba.core import events
from kaiba.core.schemas import Chain, ClusterEdge, EdgeType, EventKind
from kaiba.intelligence import cluster, entity, hubs

SOL = Chain.SOL
T0 = 1_760_000_000_000
SEC = 1000
MIN = 60 * SEC
DAY = 24 * 60 * MIN
FIXTURES = Path(__file__).parent / "fixtures" / "cluster"


def addr(tag: str) -> str:
    """A syntactically valid, collision-free Solana address for a named test actor."""
    assert "z" not in tag, "'z' is the padding character"
    return (tag + "z" * 44)[:44]


# ------------------------------------------------------------------------- row helpers


def add_swap(
    conn,
    *,
    tx,
    wallet,
    token,
    slot=None,
    ts_ms=T0,
    side="buy",
    fee_payer=None,
    is_create_tx=0,
    chain=SOL,
):
    conn.execute(
        "INSERT INTO swaps (chain, tx, slot, ts_ms, wallet, token, side, source, is_create_tx, fee_payer) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (chain.value, tx, slot, ts_ms, wallet, token, side, "test", is_create_tx, fee_payer or wallet),
    )


def add_transfer(conn, *, tx, src, dst, amount, ts_ms=T0, slot=None, first=1, chain=SOL):
    conn.execute(
        "INSERT INTO transfers (chain, tx, slot, ts_ms, src, dst, amount, is_first_inbound, source) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (chain.value, tx, slot, ts_ms, src, dst, str(amount), first, "test"),
    )


def add_token(conn, address, creator=None, chain=SOL):
    conn.execute(
        "INSERT INTO tokens (chain, address, creator, first_seen_ms) VALUES (?,?,?,?)",
        (chain.value, address, creator, T0),
    )


def add_first_buyer(conn, *, token, wallet, rank, ts_ms=T0, chain=SOL):
    conn.execute(
        "INSERT INTO first_buyers (chain, token, wallet, rank, ts_ms, source) VALUES (?,?,?,?,?,?)",
        (chain.value, token, wallet, rank, ts_ms, "test"),
    )


def add_wallet(conn, address, tags=(), chain=SOL):
    conn.execute(
        "INSERT INTO wallets (chain, address, source, tags_json, first_seen_ms, last_seen_ms) "
        "VALUES (?,?,?,?,?,?)",
        (chain.value, address, "test", json.dumps(list(tags)), T0, T0),
    )


def edge(a, b, edge_type, confidence, evidence=(), observations=1):
    return ClusterEdge(
        chain=SOL,
        a=a,
        b=b,
        edge_type=edge_type,
        confidence=confidence,
        observations=observations,
        evidence=list(evidence),
    )


def load_fixture(conn, name="bundled_launch.json"):
    doc = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    chain = Chain(doc["chain"])
    hubs.seed_hubs(conn)
    for hub in doc["hubs"]:
        hubs.add_hub(chain, hub["address"], hub["kind"], label=hub.get("label"), source="fixture", conn=conn)
    for token in doc["tokens"]:
        add_token(conn, token["address"], token.get("creator"), chain=chain)
    for row in doc["transfers"]:
        add_transfer(
            conn,
            tx=row["tx"],
            src=row["src"],
            dst=row["dst"],
            amount=row["amount"],
            ts_ms=row["ts_ms"],
            slot=row.get("slot"),
            first=row.get("is_first_inbound", 0),
            chain=chain,
        )
    for row in doc["swaps"]:
        add_swap(
            conn,
            tx=row["tx"],
            wallet=row["wallet"],
            token=row["token"],
            slot=row.get("slot"),
            ts_ms=row["ts_ms"],
            side=row.get("side", "buy"),
            fee_payer=row.get("fee_payer"),
            is_create_tx=row.get("is_create_tx", 0),
            chain=chain,
        )
    for row in doc.get("swap_meta", []):
        cluster.record_swap_meta(conn, chain, row["tx"], row["meta"])
    for row in doc.get("first_buyers", []):
        add_first_buyer(
            conn, token=row["token"], wallet=row["wallet"], rank=row["rank"], ts_ms=row["ts_ms"], chain=chain
        )
    return doc


# ------------------------------------------------------------------------------- hubs


def test_seed_hubs_marks_known_programs_and_routers(tmp_db):
    hubs.seed_hubs(tmp_db)
    assert hubs.is_hub(SOL, "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4", tmp_db)
    assert hubs.is_hub(SOL, "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8", tmp_db)
    assert hubs.is_hub(SOL, "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P", tmp_db)
    assert hubs.is_hub(SOL, "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA", tmp_db)
    assert hubs.is_hub(SOL, "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", tmp_db)
    assert hubs.is_hub(SOL, "11111111111111111111111111111111", tmp_db)
    assert not hubs.is_hub(SOL, addr("RandomWa11et"), tmp_db)


def test_seed_hubs_covers_evm_routers_and_burn_addresses(tmp_db):
    hubs.seed_hubs(tmp_db)
    # EVM addresses are matched case-insensitively through normalisation.
    assert hubs.is_hub(Chain.ETH, "0x7A250d5630B4cF539739dF2C5dAcb4c659F2488D", tmp_db)
    assert hubs.is_hub(Chain.ETH, "0x000000000022d473030f116ddee9f6b43ac78ba3", tmp_db)
    assert hubs.is_hub(Chain.BASE, "0x0000000000000000000000000000000000000000", tmp_db)
    assert hubs.is_hub(Chain.BSC, "0x000000000000000000000000000000000000dEaD", tmp_db)
    assert not hubs.is_hub(Chain.ETH, "0x00000000000000000000000000000000deadbeef", tmp_db)


def test_seed_hubs_is_idempotent(tmp_db):
    first = hubs.seed_hubs(tmp_db)
    rows_before = tmp_db.execute("SELECT COUNT(*) FROM hub_addresses").fetchone()[0]
    hubs.seed_hubs(tmp_db)
    rows_after = tmp_db.execute("SELECT COUNT(*) FROM hub_addresses").fetchone()[0]
    assert first > 0 and rows_before == rows_after


def test_jito_tip_accounts_are_seeded_as_hubs(tmp_db):
    hubs.seed_hubs(tmp_db)
    assert len(hubs.JITO_TIP_ACCOUNTS) == 8
    assert all(hubs.is_hub(SOL, tip, tmp_db) for tip in hubs.JITO_TIP_ACCOUNTS)


def test_add_hub_rejects_unknown_kind(tmp_db):
    with pytest.raises(ValueError):
        hubs.add_hub(SOL, addr("Whatever"), "friend", conn=tmp_db)


def test_degree_cap_counts_distinct_counterparties(tmp_db):
    hub = addr("Sweeper")
    for i in range(5):
        add_transfer(tmp_db, tx=addr(f"tx{i}"), src=hub, dst=addr(f"Chi1d{i}"), amount=1000)
    # repeats of the same counterparty must not inflate the degree
    add_transfer(tmp_db, tx=addr("txDup"), src=hub, dst=addr("Chi1d0"), amount=42)
    assert hubs.counterparties(SOL, hub, tmp_db) == 5
    assert hubs.degree_cap_exceeded(SOL, hub, tmp_db, cap=4)
    assert not hubs.degree_cap_exceeded(SOL, hub, tmp_db, cap=5)


def test_prune_drops_hubs_and_over_degree_nodes(tmp_db):
    hubs.seed_hubs(tmp_db)
    busy = addr("BusyNode")
    for i in range(6):
        add_transfer(tmp_db, tx=addr(f"tb{i}"), src=busy, dst=addr(f"Peer{i}"), amount=1)
    keep = addr("Norma1Wa11et")
    candidates = [keep, busy, "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4", keep]
    assert hubs.prune(candidates, SOL, tmp_db, cap=3) == [keep]
    assert set(hubs.prune(candidates, SOL, tmp_db, cap=100)) == {keep, busy}


# ------------------------------------------------------------- funding-source linkage


def test_cex_funding_fifty_wallets_produces_no_edges(tmp_db):
    """The single most important negative case: a hub is not an identity."""
    cex = addr("BinanceHot")
    hubs.add_hub(SOL, cex, "cex", label="Binance", conn=tmp_db)
    for i in range(50):
        add_transfer(tmp_db, tx=addr(f"tw{i}"), src=cex, dst=addr(f"Withdraw{i}"), amount=1_000_000_000)
    assert cluster.derive_same_funder(tmp_db, SOL) == []


def test_unlabelled_disperser_is_cut_by_the_degree_cap(tmp_db):
    """No label, no problem: 250 counterparties is not a person."""
    disperser = addr("NoLabe1Fanout")
    for i in range(250):
        add_transfer(tmp_db, tx=addr(f"td{i}"), src=disperser, dst=addr(f"Leaf{i}"), amount=5_000_000)
    assert hubs.degree_cap_exceeded(SOL, disperser, tmp_db)
    assert cluster.derive_same_funder(tmp_db, SOL) == []


def test_private_funder_links_its_children(tmp_db):
    funder = addr("Bund1er")
    kids = [addr("KidA"), addr("KidB"), addr("KidC")]
    for i, kid in enumerate(kids):
        add_transfer(tmp_db, tx=addr(f"tk{i}"), src=funder, dst=kid, amount=500_000_000, ts_ms=T0 + i * MIN)
    edges = cluster.derive_same_funder(tmp_db, SOL)
    assert len(edges) == 3  # every pair of the three children
    assert {e.edge_type for e in edges} == {EdgeType.SAME_FUNDER}
    assert all(funder not in (e.a, e.b) for e in edges)


def test_same_funder_confidence_bonuses(tmp_db):
    funder = addr("Payer")
    twin_a, twin_b = addr("TwinA"), addr("TwinB")
    add_transfer(tmp_db, tx=addr("t1"), src=funder, dst=twin_a, amount=500_000_000, ts_ms=T0)
    add_transfer(tmp_db, tx=addr("t2"), src=funder, dst=twin_b, amount=501_000_000, ts_ms=T0 + 5 * MIN)
    # a third child funded with a very different amount, days later
    other = addr("Later")
    add_transfer(tmp_db, tx=addr("t3"), src=funder, dst=other, amount=9_000_000_000, ts_ms=T0 + 10 * DAY)
    by_pair = {frozenset(e.sorted_pair()): e for e in cluster.derive_same_funder(tmp_db, SOL)}
    twins = by_pair[frozenset({twin_a, twin_b})]
    assert twins.confidence == pytest.approx(
        cluster.SAME_FUNDER_BASE + cluster.SAME_FUNDER_AMOUNT_BONUS + cluster.SAME_FUNDER_TIME_BONUS
    )
    loose = by_pair[frozenset({twin_a, other})]
    assert loose.confidence == pytest.approx(cluster.SAME_FUNDER_BASE)


def test_same_funder_walks_up_to_max_hops(tmp_db):
    root = addr("Root")
    mid_a, mid_b = addr("MidA"), addr("MidB")
    leaf_a, leaf_b = addr("LeafA"), addr("LeafB")
    add_transfer(tmp_db, tx=addr("r1"), src=root, dst=mid_a, amount=1_000_000_000, ts_ms=T0)
    add_transfer(tmp_db, tx=addr("r2"), src=root, dst=mid_b, amount=4_000_000_000, ts_ms=T0 + 2 * DAY)
    add_transfer(tmp_db, tx=addr("m1"), src=mid_a, dst=leaf_a, amount=100_000_000, ts_ms=T0 + 3 * DAY)
    add_transfer(tmp_db, tx=addr("m2"), src=mid_b, dst=leaf_b, amount=700_000_000, ts_ms=T0 + 4 * DAY)

    pairs = {frozenset(e.sorted_pair()): e for e in cluster.derive_same_funder(tmp_db, SOL, max_hops=3)}
    leaves = pairs[frozenset({leaf_a, leaf_b})]
    # matched at hop 2, amounts and times far apart: base minus one hop of penalty
    assert leaves.confidence == pytest.approx(cluster.SAME_FUNDER_BASE - cluster.SAME_FUNDER_HOP_PENALTY)
    assert f"funder:{root}" in leaves.evidence

    shallow = {frozenset(e.sorted_pair()) for e in cluster.derive_same_funder(tmp_db, SOL, max_hops=1)}
    assert frozenset({leaf_a, leaf_b}) not in shallow
    assert frozenset({mid_a, mid_b}) in shallow


def test_same_funder_hop_stops_at_a_hub(tmp_db):
    """A CEX in the middle of the chain severs it — everything above belongs to the CEX."""
    cex = addr("Exchange")
    hubs.add_hub(SOL, cex, "cex", conn=tmp_db)
    mid_a, mid_b = addr("HotA"), addr("HotB")
    leaf_a, leaf_b = addr("TailA"), addr("TailB")
    add_transfer(tmp_db, tx=addr("c1"), src=cex, dst=mid_a, amount=1_000_000_000)
    add_transfer(tmp_db, tx=addr("c2"), src=cex, dst=mid_b, amount=1_000_000_000)
    add_transfer(tmp_db, tx=addr("c3"), src=mid_a, dst=leaf_a, amount=10_000_000)
    add_transfer(tmp_db, tx=addr("c4"), src=mid_b, dst=leaf_b, amount=10_000_000)
    pairs = {frozenset(e.sorted_pair()) for e in cluster.derive_same_funder(tmp_db, SOL)}
    assert frozenset({leaf_a, leaf_b}) not in pairs
    assert frozenset({mid_a, mid_b}) not in pairs


def test_same_funder_respects_the_window(tmp_db):
    funder = addr("S1owPayer")
    early, late = addr("Ear1y"), addr("Late")
    add_transfer(tmp_db, tx=addr("w1"), src=funder, dst=early, amount=1_000_000, ts_ms=T0)
    add_transfer(tmp_db, tx=addr("w2"), src=funder, dst=late, amount=1_000_000, ts_ms=T0 + 60 * DAY)
    assert cluster.derive_same_funder(tmp_db, SOL, window_days=30) == []
    assert len(cluster.derive_same_funder(tmp_db, SOL, window_days=90)) == 1


# ---------------------------------------------------------------------- hard heuristics


def test_direct_transfer_is_a_hard_edge(tmp_db):
    a, b = addr("WalletA"), addr("Wa11etB")
    add_transfer(tmp_db, tx=addr("dt1"), src=a, dst=b, amount=2_000_000)
    edges = cluster.derive_direct_transfer(tmp_db, SOL)
    assert len(edges) == 1
    assert edges[0].edge_type is EdgeType.DIRECT_TRANSFER
    assert edges[0].confidence == cluster.HARD_CONFIDENCE
    assert edges[0].sorted_pair() == tuple(sorted([a, b]))


def test_direct_transfer_skips_hubs_and_labelled_wallets(tmp_db):
    hubs.seed_hubs(tmp_db)
    user = addr("User")
    exchange = addr("TaggedCex")
    add_wallet(tmp_db, exchange, tags=["exchange"])
    add_transfer(tmp_db, tx=addr("dt2"), src=user, dst=exchange, amount=1_000_000)
    add_transfer(
        tmp_db,
        tx=addr("dt3"),
        src=user,
        dst="JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
        amount=1_000_000,
    )
    assert cluster.derive_direct_transfer(tmp_db, SOL) == []


def test_co_signed_uses_fee_payer_and_meta_signers(tmp_db):
    payer, trader, extra = addr("FeePayer"), addr("Trader"), addr("Co5igner")
    token = addr("Mint")
    add_swap(tmp_db, tx=addr("cs1"), wallet=trader, token=token, fee_payer=payer, slot=10)
    cluster.record_swap_meta(tmp_db, SOL, addr("cs1"), {"signers": [extra]})
    edges = cluster.derive_co_signed(tmp_db, SOL)
    pairs = {frozenset(e.sorted_pair()) for e in edges}
    assert pairs == {
        frozenset({payer, trader}),
        frozenset({payer, extra}),
        frozenset({trader, extra}),
    }
    assert all(e.confidence == cluster.HARD_CONFIDENCE for e in edges)


def test_co_signed_ignores_multisig_sized_signer_sets(tmp_db):
    token = addr("Mint")
    signers = [addr(f"Member{i}") for i in range(cluster.CO_SIGNED_MAX_SIGNERS + 1)]
    add_swap(tmp_db, tx=addr("ms1"), wallet=signers[0], token=token, slot=11)
    cluster.record_swap_meta(tmp_db, SOL, addr("ms1"), {"signers": signers})
    assert cluster.derive_co_signed(tmp_db, SOL) == []


def test_same_bundle_requires_a_jito_tip_in_the_slot(tmp_db):
    token, creator = addr("BundMint"), addr("BundDev")
    a, b = addr("Snipe1"), addr("Snipe2")
    add_token(tmp_db, token, creator)
    for i, wallet in enumerate([creator, a, b]):
        add_swap(tmp_db, tx=addr(f"bs{i}"), wallet=wallet, token=token, slot=500, ts_ms=T0)
    assert cluster.derive_same_bundle(tmp_db, SOL) == []

    add_transfer(
        tmp_db,
        tx=addr("tip1"),
        src=a,
        dst=hubs.JITO_TIP_ACCOUNTS[0],
        amount=cluster.JITO_TIP_MIN_LAMPORTS,
        slot=500,
        first=0,
    )
    edges = cluster.derive_same_bundle(tmp_db, SOL)
    assert len(edges) == 1  # the creator is excluded, so only one non-creator pair remains
    assert edges[0].sorted_pair() == tuple(sorted([a, b]))
    assert creator not in (edges[0].a, edges[0].b)


def test_same_bundle_needs_two_non_creator_wallets(tmp_db):
    token, creator = addr("SoloMint"), addr("SoloDev")
    buyer = addr("On1yBuyer")
    add_token(tmp_db, token, creator)
    add_swap(tmp_db, tx=addr("sb1"), wallet=creator, token=token, slot=600)
    add_swap(tmp_db, tx=addr("sb2"), wallet=buyer, token=token, slot=600)
    add_transfer(
        tmp_db, tx=addr("tip2"), src=buyer, dst=hubs.JITO_TIP_ACCOUNTS[1], amount=5000, slot=600, first=0
    )
    assert cluster.derive_same_bundle(tmp_db, SOL) == []


def test_shared_alt_authority_reads_swap_meta(tmp_db):
    token = addr("A1tMint")
    authority = addr("LutOwner")
    w1, w2 = addr("LutWa11et1"), addr("LutWa11et2")
    add_swap(tmp_db, tx=addr("alt1"), wallet=w1, token=token, slot=20)
    add_swap(tmp_db, tx=addr("alt2"), wallet=w2, token=token, slot=21)
    assert cluster.derive_shared_alt_authority(tmp_db, SOL) == []  # placeholder without metadata

    cluster.record_swap_meta(tmp_db, SOL, addr("alt1"), {"alt_authority": authority})
    cluster.record_swap_meta(tmp_db, SOL, addr("alt2"), {"alt_authority": authority})
    edges = cluster.derive_shared_alt_authority(tmp_db, SOL)
    assert len(edges) == 1
    assert edges[0].edge_type is EdgeType.SHARED_ALT_AUTHORITY
    assert edges[0].confidence == cluster.HARD_CONFIDENCE
    assert f"alt:{authority}" in edges[0].evidence


# ---------------------------------------------------------------------- co-occurrence


def _co_buy(conn, wallets, tokens, base_slot=1000):
    for i, token in enumerate(tokens):
        for w in wallets:
            add_swap(
                conn,
                tx=addr(f"cb{i}{wallets.index(w)}"),
                wallet=w,
                token=token,
                slot=base_slot + i,
                ts_ms=T0 + i * MIN,
            )


def test_same_slot_buy_on_one_token_is_noise(tmp_db):
    a, b = addr("CoA"), addr("CoB")
    _co_buy(tmp_db, [a, b], [addr("HypeMint")])
    assert cluster.derive_same_slot_buy(tmp_db, SOL) == []


def test_same_slot_buy_on_three_tokens_is_an_edge(tmp_db):
    a, b = addr("CoA"), addr("CoB")
    _co_buy(tmp_db, [a, b], [addr("Mint1"), addr("Mint2"), addr("Mint3")])
    edges = cluster.derive_same_slot_buy(tmp_db, SOL)
    assert len(edges) == 1
    assert edges[0].edge_type is EdgeType.SAME_SLOT_BUY
    assert edges[0].observations == 3
    assert edges[0].confidence == pytest.approx(cluster.COOCCUR_MIN_CONFIDENCE)


def test_same_slot_buy_confidence_scales_with_shared_tokens(tmp_db):
    a, b = addr("CoA"), addr("CoB")
    _co_buy(tmp_db, [a, b], [addr(f"Mint{i}") for i in range(cluster.COOCCUR_SATURATION_TOKENS)])
    edges = cluster.derive_same_slot_buy(tmp_db, SOL)
    assert edges[0].confidence == pytest.approx(cluster.COOCCUR_MAX_CONFIDENCE)


def test_same_slot_buy_ignores_different_slots(tmp_db):
    a, b = addr("CoA"), addr("CoB")
    for i in range(4):
        token = addr(f"Se1f{i}")
        add_swap(tmp_db, tx=addr(f"sa{i}"), wallet=a, token=token, slot=2000 + i)
        add_swap(tmp_db, tx=addr(f"sb{i}"), wallet=b, token=token, slot=9000 + i)
    assert cluster.derive_same_slot_buy(tmp_db, SOL) == []


def test_first_n_cooccur_respects_rank_and_token_floor(tmp_db):
    a, b, late = addr("EarA"), addr("EarB"), addr("LateBuyer")
    for i in range(3):
        token = addr(f"FbMint{i}")
        add_first_buyer(tmp_db, token=token, wallet=a, rank=2)
        add_first_buyer(tmp_db, token=token, wallet=b, rank=3)
        add_first_buyer(tmp_db, token=token, wallet=late, rank=25)
    edges = cluster.derive_first_n_cooccur(tmp_db, SOL, n=20)
    assert len(edges) == 1
    assert edges[0].sorted_pair() == tuple(sorted([a, b]))
    assert cluster.derive_first_n_cooccur(tmp_db, SOL, n=20, min_tokens=4) == []


# -------------------------------------------------------------------------- lead / lag


def _lead_lag_data(conn, leader, follower, tokens, delay_s=10, prefix="x"):
    for i, token in enumerate(tokens):
        add_swap(
            conn, tx=addr(f"{prefix}L{i}"), wallet=leader, token=token, slot=3000 + i, ts_ms=T0 + i * MIN
        )
        add_swap(
            conn,
            tx=addr(f"{prefix}F{i}"),
            wallet=follower,
            token=token,
            slot=3000 + i,
            ts_ms=T0 + i * MIN + delay_s * SEC,
        )


def test_lead_lag_emits_a_directed_hint(tmp_db):
    leader, follower = addr("A1pha"), addr("Copier")
    _lead_lag_data(tmp_db, leader, follower, [addr(f"L1Mint{i}") for i in range(5)])
    edges = cluster.derive_lead_lag(tmp_db, SOL)
    assert len(edges) == 1
    assert edges[0].edge_type is EdgeType.LEAD_LAG
    assert edges[0].confidence == cluster.LEAD_LAG_CONFIDENCE
    assert f"lead:{leader}" in edges[0].evidence


def test_lead_lag_needs_enough_tokens_and_a_tight_delay(tmp_db):
    leader, follower = addr("A1pha"), addr("Copier")
    _lead_lag_data(tmp_db, leader, follower, [addr(f"Few{i}") for i in range(4)], prefix="few")
    assert cluster.derive_lead_lag(tmp_db, SOL) == []

    slow = addr("S1owHand")
    _lead_lag_data(
        tmp_db, leader, slow, [addr(f"S1ow{i}") for i in range(6)], delay_s=120, prefix="s1o"
    )
    assert cluster.derive_lead_lag(tmp_db, SOL) == []


# ------------------------------------------------------------------ CEX deposit reuse


def test_shared_cex_deposit_groups_the_senders(tmp_db):
    cex, deposit = addr("Kraken"), addr("DepositAddr")
    hubs.add_hub(SOL, cex, "cex", conn=tmp_db)
    senders = [addr("Send1"), addr("Send2"), addr("Send3")]
    for i, s in enumerate(senders):
        add_transfer(tmp_db, tx=addr(f"sd{i}"), src=s, dst=deposit, amount=1_000_000)
    add_transfer(tmp_db, tx=addr("fwd"), src=deposit, dst=cex, amount=2_900_000, first=0)

    edges = cluster.derive_shared_cex_deposit(tmp_db, SOL)
    assert len(edges) == 3
    assert {e.edge_type for e in edges} == {EdgeType.SHARED_CEX_DEPOSIT}
    assert all(deposit not in (e.a, e.b) for e in edges)
    assert cluster.derive_shared_cex_deposit(tmp_db, SOL, cap=2) == []


# -------------------------------------------------------------------------- persistence


def test_persist_edges_accumulates_observations_and_keeps_max_confidence(tmp_db):
    a, b = addr("PersA"), addr("PersB")
    cluster.persist_edges([edge(a, b, EdgeType.SAME_FUNDER, 0.85, ["sig1"])], tmp_db)
    cluster.persist_edges([edge(a, b, EdgeType.SAME_FUNDER, 0.60, ["sig2"], observations=4)], tmp_db)
    row = tmp_db.execute("SELECT * FROM cluster_edges").fetchone()
    assert row["observations"] == 5
    assert row["confidence"] == pytest.approx(0.85)
    assert json.loads(row["evidence_json"]) == ["sig1", "sig2"]


def test_persist_edges_caps_evidence_at_five(tmp_db):
    a, b = addr("EvA"), addr("EvB")
    for i in range(9):
        cluster.persist_edges([edge(a, b, EdgeType.DIRECT_TRANSFER, 0.9, [f"sig{i}"])], tmp_db)
    row = tmp_db.execute("SELECT evidence_json FROM cluster_edges").fetchone()
    stored = json.loads(row["evidence_json"])
    assert len(stored) == cluster.MAX_EVIDENCE
    assert stored == [f"sig{i}" for i in range(cluster.MAX_EVIDENCE)]


def test_persist_edges_always_sorts_the_pair(tmp_db):
    a, b = addr("Aaa"), addr("Bbb")
    cluster.persist_edges([edge(b, a, EdgeType.CO_SIGNED, 0.9, ["x"])], tmp_db)
    cluster.persist_edges([edge(a, b, EdgeType.CO_SIGNED, 0.9, ["y"])], tmp_db)
    rows = tmp_db.execute("SELECT a, b, observations FROM cluster_edges").fetchall()
    assert len(rows) == 1
    assert rows[0]["a"] < rows[0]["b"]
    assert rows[0]["observations"] == 2


def test_persist_edges_folds_duplicates_within_one_batch(tmp_db):
    a, b = addr("FoldA"), addr("FoldB")
    written = cluster.persist_edges(
        [
            edge(a, b, EdgeType.SAME_SLOT_BUY, 0.5, ["one"]),
            edge(b, a, EdgeType.SAME_SLOT_BUY, 0.7, ["two"]),
        ],
        tmp_db,
    )
    assert written == 1
    row = tmp_db.execute("SELECT * FROM cluster_edges").fetchone()
    assert row["confidence"] == pytest.approx(0.7)
    assert row["observations"] == 2


def test_persistence_joins_an_outer_transaction(tmp_db):
    """A nightly job may wrap the whole rebuild in one write; SQLite cannot nest BEGINs."""
    from kaiba.core.db import tx

    a, b = addr("TxA"), addr("TxB")
    with tx(tmp_db):
        cluster.persist_edges([edge(a, b, EdgeType.CO_SIGNED, 0.9, ["sig"])], tmp_db)
        entity.persist_entities(entity.build_entities(tmp_db, SOL), tmp_db)
    assert entity.entity_for(SOL, a, tmp_db) is not None


def test_derive_all_seeds_hubs_when_the_table_is_empty(tmp_db):
    """Clustering with no hubs known is wrong, not degraded — it must not be reachable."""
    assert tmp_db.execute("SELECT COUNT(*) FROM hub_addresses").fetchone()[0] == 0
    cluster.derive_all(tmp_db, SOL)
    assert hubs.is_hub(SOL, "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4", tmp_db)


def test_derive_all_reports_a_count_per_edge_type(tmp_db):
    load_fixture(tmp_db)
    counts = cluster.derive_all(tmp_db, SOL)
    assert set(counts) == {t.value for t in EdgeType} - {EdgeType.SHARED_COUNTERPARTY.value}
    assert counts[EdgeType.SAME_BUNDLE.value] == 3
    assert counts[EdgeType.DIRECT_TRANSFER.value] == 3
    assert counts[EdgeType.SAME_FUNDER.value] == 3
    assert counts[EdgeType.CO_SIGNED.value] == 1
    assert counts[EdgeType.SHARED_ALT_AUTHORITY.value] == 1
    assert counts[EdgeType.SAME_SLOT_BUY.value] == 0  # one token is never enough


# ----------------------------------------------------------------------------- entities


def _persist(conn, *edges):
    cluster.persist_edges(list(edges), conn)


def test_hard_edges_merge_into_one_entity(tmp_db):
    a, b, c = addr("HardA"), addr("HardB"), addr("HardC")
    _persist(
        tmp_db,
        edge(a, b, EdgeType.CO_SIGNED, 0.9),
        edge(b, c, EdgeType.DIRECT_TRANSFER, 0.9),
    )
    entities = entity.build_entities(tmp_db, SOL)
    assert len(entities) == 1
    assert entities[0].members == sorted([a, b, c])
    assert entities[0].confidence == pytest.approx(0.9)


def test_a_single_soft_edge_does_not_merge(tmp_db):
    a, b = addr("SoftA"), addr("SoftB")
    _persist(tmp_db, edge(a, b, EdgeType.SAME_FUNDER, 0.95))
    assert entity.build_entities(tmp_db, SOL) == []


def test_two_distinct_soft_types_do_merge(tmp_db):
    a, b = addr("SoftA"), addr("SoftB")
    _persist(
        tmp_db,
        edge(a, b, EdgeType.SAME_FUNDER, 0.90),
        edge(a, b, EdgeType.SHARED_CEX_DEPOSIT, 0.75),
    )
    entities = entity.build_entities(tmp_db, SOL)
    assert len(entities) == 1
    assert entities[0].members == sorted([a, b])
    # confidence is the weakest link in the merge path, not the strongest
    assert entities[0].confidence == pytest.approx(0.75)
    assert set(entities[0].edge_types) == {EdgeType.SAME_FUNDER, EdgeType.SHARED_CEX_DEPOSIT}


def test_soft_edges_below_min_confidence_are_ignored(tmp_db):
    a, b = addr("WeakA"), addr("WeakB")
    _persist(
        tmp_db,
        edge(a, b, EdgeType.SAME_FUNDER, 0.90),
        edge(a, b, EdgeType.SAME_SLOT_BUY, 0.50),
    )
    assert entity.build_entities(tmp_db, SOL) == []
    assert len(entity.build_entities(tmp_db, SOL, min_confidence=0.4)) == 1


def test_soft_edges_bridge_two_hard_components(tmp_db):
    a1, a2 = addr("GroupA1"), addr("GroupA2")
    b1, b2 = addr("GroupB1"), addr("GroupB2")
    _persist(
        tmp_db,
        edge(a1, a2, EdgeType.CO_SIGNED, 0.9),
        edge(b1, b2, EdgeType.CO_SIGNED, 0.9),
        edge(a1, b1, EdgeType.SAME_FUNDER, 0.85),
    )
    assert len(entity.build_entities(tmp_db, SOL)) == 2  # one soft type is not identity

    _persist(tmp_db, edge(a2, b2, EdgeType.FIRST_N_COOCCUR, 0.8))
    merged = entity.build_entities(tmp_db, SOL)
    assert len(merged) == 1
    assert merged[0].members == sorted([a1, a2, b1, b2])


def test_lead_lag_never_merges_but_is_readable_as_a_side_wallet(tmp_db):
    leader, follower = addr("Lead"), addr("Side")
    _persist(
        tmp_db,
        edge(leader, follower, EdgeType.LEAD_LAG, 0.6, [f"lead:{leader}"]),
        edge(leader, follower, EdgeType.SAME_FUNDER, 0.9),
    )
    # LEAD_LAG must not count toward the two-soft-type rule
    assert entity.build_entities(tmp_db, SOL) == []
    assert entity.side_wallets(SOL, leader, tmp_db) == [follower]
    assert entity.side_wallets(SOL, follower, tmp_db) == []
    assert entity.side_wallet_of(SOL, follower, tmp_db) == [leader]


def test_no_single_member_entities(tmp_db):
    a, b = addr("So1oA"), addr("So1oB")
    _persist(tmp_db, edge(a, b, EdgeType.SAME_SLOT_BUY, 0.5))
    assert entity.build_entities(tmp_db, SOL) == []
    assert all(e.size >= 2 for e in entity.build_entities(tmp_db, SOL, min_confidence=0.1))


def test_entity_ids_are_deterministic_and_order_independent(tmp_db):
    a, b, c = addr("IdA"), addr("IdB"), addr("IdC")
    assert entity.entity_id_for(SOL, [a, b, c]) == entity.entity_id_for(SOL, [c, a, b])
    assert entity.entity_id_for(SOL, [a, b]) != entity.entity_id_for(SOL, [a, b, c])
    assert entity.entity_id_for(SOL, [a, b]) != entity.entity_id_for(Chain.ETH, [a, b])
    assert entity.entity_id_for(SOL, [a, b]).startswith("sol:ent:")

    _persist(tmp_db, edge(a, b, EdgeType.CO_SIGNED, 0.9))
    first = entity.build_entities(tmp_db, SOL)[0]
    second = entity.build_entities(tmp_db, SOL)[0]
    assert first.entity_id == second.entity_id == entity.entity_id_for(SOL, [b, a])


def test_persist_entities_bumps_version_and_emits(tmp_db):
    a, b = addr("PeA"), addr("PeB")
    _persist(tmp_db, edge(a, b, EdgeType.CO_SIGNED, 0.9))
    built = entity.build_entities(tmp_db, SOL)
    entity.persist_entities(built, tmp_db)
    entity.persist_entities(built, tmp_db)
    row = tmp_db.execute("SELECT version, size FROM entities").fetchone()
    assert row["version"] == 2 and row["size"] == 2
    kinds = [e.kind for e in events.recent(10, conn=tmp_db)]
    assert EventKind.ENTITY_UPDATED in kinds


def test_entity_for_round_trips_and_returns_none_for_strangers(tmp_db):
    a, b = addr("RtA"), addr("RtB")
    _persist(tmp_db, edge(a, b, EdgeType.DIRECT_TRANSFER, 0.9))
    entity.rebuild(tmp_db, SOL)
    found = entity.entity_for(SOL, a, tmp_db)
    assert found is not None
    assert found.members == sorted([a, b])
    assert found.edge_types == [EdgeType.DIRECT_TRANSFER]
    assert entity.entity_for(SOL, addr("Stranger"), tmp_db) is None


def test_persist_entities_moves_members_and_drops_empty_entities(tmp_db):
    a, b, c = addr("MvA"), addr("MvB"), addr("MvC")
    _persist(tmp_db, edge(a, b, EdgeType.CO_SIGNED, 0.9))
    entity.rebuild(tmp_db, SOL)
    _persist(tmp_db, edge(b, c, EdgeType.CO_SIGNED, 0.9))
    entity.rebuild(tmp_db, SOL)
    assert entity.entity_count(SOL, tmp_db) == 1
    assert tmp_db.execute("SELECT COUNT(*) FROM entity_members WHERE address=?", (a,)).fetchone()[0] == 1
    assert entity.entity_for(SOL, a, tmp_db).members == sorted([a, b, c])


# ------------------------------------------------------- the confluence lane's guard


def test_independent_entity_count_collapses_one_operator(tmp_db):
    wallets = [addr(f"Farm{i}") for i in range(5)]
    for w in wallets[1:]:
        _persist(tmp_db, edge(wallets[0], w, EdgeType.CO_SIGNED, 0.9))
    entity.rebuild(tmp_db, SOL)
    assert entity.independent_entity_count(SOL, wallets, tmp_db) == 1


def test_independent_entity_count_keeps_strangers_apart(tmp_db):
    strangers = [addr(f"Person{i}") for i in range(5)]
    assert entity.independent_entity_count(SOL, strangers, tmp_db) == 5
    assert entity.independent_entity_count(SOL, [], tmp_db) == 0
    assert entity.independent_entity_count(SOL, strangers + strangers, tmp_db) == 5


def test_independent_entity_count_mixes_clustered_and_solo(tmp_db):
    a, b = addr("PairA"), addr("PairB")
    solo1, solo2 = addr("SoloOne"), addr("SoloTwo")
    _persist(tmp_db, edge(a, b, EdgeType.SAME_BUNDLE, 0.9))
    entity.rebuild(tmp_db, SOL)
    assert entity.independent_entity_count(SOL, [a, b, solo1, solo2], tmp_db) == 3


def test_cluster_supply_pct_sums_across_an_entity(tmp_db):
    a, b = addr("Ho1dA"), addr("Ho1dB")
    whale = addr("Wha1e")
    _persist(tmp_db, edge(a, b, EdgeType.CO_SIGNED, 0.9))
    entity.rebuild(tmp_db, SOL)
    holders = {a: 12.0, b: 9.0, whale: 25.0}
    token = addr("DyorMint")
    assert entity.cluster_supply_pct(SOL, token, holders, tmp_db) == pytest.approx(21.0)
    assert entity.cluster_supply_pct(
        SOL, token, holders, tmp_db, include_singletons=True
    ) == pytest.approx(25.0)


def test_cluster_supply_pct_is_zero_without_clusters(tmp_db):
    assert entity.cluster_supply_pct(SOL, addr("Empty"), {}, tmp_db) == 0.0
    assert entity.cluster_supply_pct(SOL, addr("Empty"), {addr("Lone"): 40.0}, tmp_db) == 0.0


def test_cluster_supply_pct_crosses_the_dyor_thresholds(tmp_db):
    """>20% is review, >30% is reject — the numbers the dossier blocker uses."""
    members = [addr(f"Ring{i}") for i in range(4)]
    for m in members[1:]:
        _persist(tmp_db, edge(members[0], m, EdgeType.SAME_BUNDLE, 0.9))
    entity.rebuild(tmp_db, SOL)
    holders = {m: 8.0 for m in members}  # 32% between them
    assert entity.cluster_supply_pct(SOL, addr("RingMint"), holders, tmp_db) > 30.0


# --------------------------------------------------------------------- end to end


def test_fixture_bundled_launch_end_to_end(tmp_db):
    doc = load_fixture(tmp_db)
    expect = doc["expect"]
    cluster.derive_all(tmp_db, SOL)
    entities = entity.rebuild(tmp_db, SOL)

    assert len(entities) == 1
    members = set(entities[0].members)
    assert members == {expect["bundler"], *expect["sub_wallets"]}
    assert expect["retail"] not in members
    assert expect["cex"] not in members

    watched = [*expect["sub_wallets"], expect["retail"]]
    assert entity.independent_entity_count(SOL, watched, tmp_db) == 2

    holders = {w: 10.0 for w in expect["sub_wallets"]}
    holders[expect["retail"]] = 25.0
    assert entity.cluster_supply_pct(SOL, expect["token"], holders, tmp_db) == pytest.approx(30.0)


def test_rerunning_derive_all_is_stable(tmp_db):
    load_fixture(tmp_db)
    first = cluster.derive_all(tmp_db, SOL)
    second = cluster.derive_all(tmp_db, SOL)
    assert first == second
    rows = tmp_db.execute("SELECT COUNT(*) FROM cluster_edges").fetchone()[0]
    assert rows == sum(first.values())
    ids_first = {e.entity_id for e in entity.rebuild(tmp_db, SOL)}
    ids_second = {e.entity_id for e in entity.rebuild(tmp_db, SOL)}
    assert ids_first == ids_second


# ---------------------------------------------------------------- integration invariant
# Added by Claude 2026-09-20 after the clustering build flagged that the co-occurrence
# scale started below the entity builder's admission threshold, so the documented
# three-token rule produced edges that were then silently filtered out.


def test_min_token_cooccurrence_clears_the_entity_floor():
    """An edge at the documented minimum must be able to reach the entity builder."""
    from kaiba.intelligence import cluster, entity

    at_floor = cluster._cooccur_confidence(cluster.COOCCUR_MIN_TOKENS, cluster.COOCCUR_MIN_TOKENS)
    assert at_floor >= entity.DEFAULT_MIN_CONFIDENCE, (
        f"co-occurrence at {cluster.COOCCUR_MIN_TOKENS} tokens scores {at_floor}, "
        f"below the {entity.DEFAULT_MIN_CONFIDENCE} admission floor"
    )


def test_cooccurrence_confidence_still_rises_with_evidence():
    from kaiba.intelligence import cluster

    floor = cluster.COOCCUR_MIN_TOKENS
    few = cluster._cooccur_confidence(floor, floor)
    many = cluster._cooccur_confidence(floor + 20, floor)
    assert many > few
    assert many <= cluster.COOCCUR_MAX_CONFIDENCE
