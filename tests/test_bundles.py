"""Tests for :mod:`kaiba.intelligence.bundles`.

The bulk of these are about one property rather than about arithmetic: **a token whose
tape we have not pulled back to its launch must come back ``None`` with
``EvidenceBasis.UNAVAILABLE``, never 0%**. ``curve-velocity`` gates on
``bundler_pct < max_bundler_pct`` and fails closed on an unknown, so a false zero would
convert a fail-closed gate into a fail-open one and fire the lane on exactly the launches
it exists to avoid. Every refusal path in :func:`kaiba.intelligence.bundles.analyse` has a
test here that asserts the value is ``None`` and not merely "falsy".
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain, EvidenceBasis
from kaiba.intelligence import bundles

SOL = Chain.SOL
CREATOR = "Creator1111111111111111111111111111111111111"
CREATED_MS = 1_789_891_020_283
CREATE_SLOT = 448_670_460
LAUNCH_TOKEN = "Mint1111111111111111111111111111111111111111"

#: pump.fun standard reserves, checked for equality by ``resolve_supply``.
REAL_ATOMS = bundles.PUMPFUN_LAUNCH_CURVE_ATOMS
VIRTUAL_ATOMS = REAL_ATOMS + bundles.PUMPFUN_CURVE_INVARIANT_ATOMS
SUPPLY = bundles.PUMPFUN_TOTAL_SUPPLY_ATOMS

#: 1% of total supply, so the expected percentages below are readable by eye.
ONE_PCT = SUPPLY // 100


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


def _token(conn: sqlite3.Connection, address: str, *, created_ms: int | None = CREATED_MS) -> None:
    conn.execute(
        "INSERT INTO tokens (chain, address, creator, created_ms, launchpad, first_seen_ms) "
        "VALUES (?,?,?,?,?,?)",
        (SOL.value, address, CREATOR, created_ms, "pump.fun", created_ms or 0),
    )


def _snapshot(
    conn: sqlite3.Connection,
    address: str,
    *,
    coverage_from_ms: int | None = CREATED_MS - 1_000,
    real: int = REAL_ATOMS,
    virtual: int | None = VIRTUAL_ATOMS,
) -> None:
    conn.execute(
        "INSERT INTO curve_snapshots (chain, token, observed_ms, real_sol_lamports, "
        " virtual_sol_lamports, real_token_atoms, virtual_token_atoms, coverage_from_ms, "
        " created_ms, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            SOL.value,
            address,
            CREATED_MS,
            0,
            30_000_000_000,
            str(real),
            str(virtual if virtual is not None else real),
            coverage_from_ms,
            CREATED_MS,
            "pumpfun",
        ),
    )


def _buy(
    conn: sqlite3.Connection,
    address: str,
    wallet: str,
    *,
    slot: int,
    block_index: int | None,
    atoms: int,
    ts_ms: int = CREATED_MS,
    side: str = "buy",
    fee_payer: str | None = None,
    lamports: int = 1_000_000,
    source: str = "pumpfun:trades",
) -> None:
    conn.execute(
        "INSERT INTO swaps (chain, tx, slot, block_index, ts_ms, wallet, token, side, "
        " amount_token, amount_native, source, is_create_tx, fee_payer) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            SOL.value,
            f"tx-{address[:6]}-{wallet}-{slot}-{block_index}-{side}",
            slot,
            block_index,
            ts_ms,
            wallet,
            address,
            side,
            str(atoms),
            str(lamports),
            source,
            0,
            fee_payer,
        ),
    )


@pytest.fixture
def launch(tmp_db: sqlite3.Connection) -> sqlite3.Connection:
    """A standard pump.fun launch: creator buy, a two-wallet bundle, one later sniper.

    Slot ``CREATE_SLOT`` holds block indexes 10, 11, 12 — the creator plus two other
    wallets, contiguous, which is the structural fingerprint of an atomic bundle. Slot
    ``CREATE_SLOT + 2`` holds a lone buy at index 900, far from anything, which is a
    sniper racing the launch from outside the block ordering.
    """
    token = LAUNCH_TOKEN
    _token(tmp_db, token)
    _snapshot(tmp_db, token)
    _buy(tmp_db, token, CREATOR, slot=CREATE_SLOT, block_index=10, atoms=2 * ONE_PCT)
    _buy(tmp_db, token, "BundlerA", slot=CREATE_SLOT, block_index=11, atoms=3 * ONE_PCT)
    _buy(tmp_db, token, "BundlerB", slot=CREATE_SLOT, block_index=12, atoms=4 * ONE_PCT)
    _buy(tmp_db, token, "Sniper1", slot=CREATE_SLOT + 2, block_index=900, atoms=5 * ONE_PCT)
    tmp_db.commit()
    return tmp_db


# --------------------------------------------------------------------------------------
# the measurement
# --------------------------------------------------------------------------------------


def test_bundle_and_sniper_are_measured_separately(launch: sqlite3.Connection) -> None:
    report = bundles.analyse(SOL, LAUNCH_TOKEN, launch)

    assert report.measured
    # 2 + 3 + 4 = 9% of supply arrived in one contiguous same-slot run.
    assert report.bundled_pct == Decimal(9)
    # The sniper is 5% and is deliberately NOT folded into the bundle number.
    assert report.sniped_pct == Decimal(5)
    assert report.bundled.basis is EvidenceBasis.DERIVED
    assert report.bundled.receipt is not None
    assert report.sniped.receipt is not None
    assert len(report.groups) == 1
    assert report.groups[0].wallets == ("BundlerA", "BundlerB", CREATOR)
    assert report.sniper_wallets == ("Sniper1",)


def test_supply_basis_is_recorded_not_assumed(launch: sqlite3.Connection) -> None:
    report = bundles.analyse(SOL, LAUNCH_TOKEN, launch)
    assert report.supply_basis is bundles.SupplyBasis.PUMPFUN_STANDARD_VERIFIED
    assert report.supply_atoms == SUPPLY


def test_non_standard_reserves_fall_back_to_curve_denominator(tmp_db: sqlite3.Connection) -> None:
    """A mint whose curve is not a standard pump.fun curve must say so loudly."""
    token = "Odd11111111111111111111111111111111111111111"
    _token(tmp_db, token)
    _snapshot(tmp_db, token, real=500_000_000_000_000, virtual=None)
    _buy(tmp_db, token, CREATOR, slot=CREATE_SLOT, block_index=10, atoms=1_000_000_000_000)
    _buy(tmp_db, token, "BundlerA", slot=CREATE_SLOT, block_index=11, atoms=1_000_000_000_000)
    tmp_db.commit()

    report = bundles.analyse(SOL, token, tmp_db)
    assert report.supply_basis is bundles.SupplyBasis.CURVE_LAUNCH_RESERVES
    assert report.supply_atoms == 500_000_000_000_000
    assert any("NOT a share of total supply" in n for n in report.notes)


def test_a_depleted_curve_still_yields_total_supply_via_the_invariant(
    tmp_db: sqlite3.Connection,
) -> None:
    """real_token_atoms is a live reserve, not a supply, and it moves in both directions.

    On one live mint it had *risen* to 802.8e12 from a 793.1e12 launch value by the time
    we first snapshotted it, because net selling returns tokens to the curve. Dividing by
    it would silently rescale the percentage; the invariant is what actually identifies a
    standard pump.fun curve, and it holds whatever the reserves have done since.
    """
    token = "Traded111111111111111111111111111111111111111"
    moved = 802_865_509_239_527
    _token(tmp_db, token)
    _snapshot(tmp_db, token, real=moved, virtual=moved + bundles.PUMPFUN_CURVE_INVARIANT_ATOMS)
    _buy(tmp_db, token, CREATOR, slot=CREATE_SLOT, block_index=10, atoms=ONE_PCT)
    _buy(tmp_db, token, "BundlerA", slot=CREATE_SLOT, block_index=11, atoms=ONE_PCT)
    tmp_db.commit()

    report = bundles.analyse(SOL, token, tmp_db)
    assert report.supply_basis is bundles.SupplyBasis.PUMPFUN_STANDARD_VERIFIED
    assert report.supply_atoms == SUPPLY
    assert report.bundled_pct == Decimal(2)
    assert any("not a reading of the launch state" in n for n in report.notes)


def test_a_post_launch_snapshot_of_an_unknown_curve_gives_no_denominator(
    tmp_db: sqlite3.Connection,
) -> None:
    token = "Unknown11111111111111111111111111111111111111"
    _token(tmp_db, token)
    tmp_db.execute(
        "INSERT INTO curve_snapshots (chain, token, observed_ms, real_sol_lamports, "
        " virtual_sol_lamports, real_token_atoms, virtual_token_atoms, coverage_from_ms, "
        " created_ms, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            SOL.value, token, CREATED_MS + 3_600_000, 0, 30_000_000_000,
            "500000000000000", "500000000000000", CREATED_MS - 1_000, CREATED_MS, "pumpfun",
        ),
    )
    _buy(tmp_db, token, CREATOR, slot=CREATE_SLOT, block_index=10, atoms=ONE_PCT)
    _buy(tmp_db, token, "BundlerA", slot=CREATE_SLOT, block_index=11, atoms=ONE_PCT)
    tmp_db.commit()

    report = bundles.analyse(SOL, token, tmp_db)
    assert report.measured
    assert report.supply_basis is bundles.SupplyBasis.UNKNOWN
    assert report.bundled.value is None
    assert report.bundled_share_of_launch_buys.value == Decimal(100)


def test_bundled_share_of_launch_buys_survives_a_missing_denominator(
    tmp_db: sqlite3.Connection,
) -> None:
    """No supply figure means no percentage of supply — but not no information."""
    token = "NoSupply111111111111111111111111111111111111"
    _token(tmp_db, token)
    _buy(tmp_db, token, CREATOR, slot=CREATE_SLOT, block_index=10, atoms=ONE_PCT)
    _buy(tmp_db, token, "BundlerA", slot=CREATE_SLOT, block_index=11, atoms=ONE_PCT)
    _buy(tmp_db, token, "Sniper1", slot=CREATE_SLOT + 1, block_index=800, atoms=2 * ONE_PCT)
    tmp_db.commit()

    report = bundles.analyse(SOL, token, tmp_db)
    assert report.measured
    assert report.supply_basis is bundles.SupplyBasis.UNKNOWN
    assert report.bundled.value is None
    assert report.bundled.basis is EvidenceBasis.UNAVAILABLE
    assert "bundler_pct" in report.unknowns
    assert report.bundled_share_of_launch_buys.value == Decimal(50)


# --------------------------------------------------------------------------------------
# the 98.7% base rate: a lone creator buy is not a bundle
# --------------------------------------------------------------------------------------


def test_creator_only_run_is_not_a_bundle(tmp_db: sqlite3.Connection) -> None:
    """EDGE §4 #16: 'the dev bought his own bundle' has a 98.7% base rate.

    Two contiguous buys from the same address in the create slot are one operator, so the
    group fails the entity test and nothing is reported as bundled. The number must be a
    measured 0, not an unknown — the tape is complete and we genuinely found no bundle.
    """
    token = "DevOnly11111111111111111111111111111111111111"
    _token(tmp_db, token)
    _snapshot(tmp_db, token)
    _buy(tmp_db, token, CREATOR, slot=CREATE_SLOT, block_index=10, atoms=3 * ONE_PCT)
    _buy(tmp_db, token, CREATOR, slot=CREATE_SLOT, block_index=11, atoms=2 * ONE_PCT)
    tmp_db.commit()

    report = bundles.analyse(SOL, token, tmp_db)
    assert report.measured
    assert report.groups == ()
    assert report.bundled_pct == Decimal(0)
    assert report.bundled.basis is EvidenceBasis.DERIVED  # measured zero, not unknown
    # The creator is not a sniper of his own launch.
    assert report.sniped_pct == Decimal(0)
    assert any("98.7%" in n for n in report.notes)


def test_a_stampede_is_not_a_bundle(tmp_db: sqlite3.Connection) -> None:
    token = "Stampede1111111111111111111111111111111111111"
    _token(tmp_db, token)
    _snapshot(tmp_db, token)
    for i in range(bundles.MAX_BUNDLE_TXS + 1):
        _buy(tmp_db, token, f"W{i:02d}", slot=CREATE_SLOT, block_index=10 + i, atoms=ONE_PCT)
    tmp_db.commit()

    report = bundles.analyse(SOL, token, tmp_db)
    assert report.measured
    assert report.groups == ()
    assert report.bundled_pct == Decimal(0)
    assert any("stampede" in n for n in report.notes)


# --------------------------------------------------------------------------------------
# grouping, as a pure function
# --------------------------------------------------------------------------------------


def _lb(wallet: str, slot: int, index: int | None, atoms: int = 1) -> bundles.LaunchBuy:
    return bundles.LaunchBuy(
        wallet=wallet, slot=slot, block_index=index, atoms=atoms, lamports=None, tx=f"{wallet}{slot}"
    )


def test_a_group_never_spans_two_slots() -> None:
    """A Jito bundle executes atomically inside one block, so two slots is two events."""
    buys = [_lb("A", 100, 10), _lb("B", 101, 11)]
    groups, _ = bundles.group_buys(buys, {})
    assert groups == []


def test_gap_beyond_tolerance_splits_a_run() -> None:
    buys = [
        _lb("A", 100, 10),
        _lb("B", 100, 11),
        _lb("C", 100, 11 + bundles.MAX_INDEX_GAP + 1),
        _lb("D", 100, 11 + bundles.MAX_INDEX_GAP + 2),
    ]
    groups, _ = bundles.group_buys(buys, {})
    assert len(groups) == 2
    assert groups[0].wallets == ("A", "B")
    assert groups[1].wallets == ("C", "D")


def test_entities_collapse_a_run_below_the_bundle_bar() -> None:
    """Two addresses the resolver says are one operator are one entity, so not a bundle."""
    buys = [_lb("A", 100, 10), _lb("B", 100, 11)]
    groups, notes = bundles.group_buys(buys, {"A": "ent:1", "B": "ent:1"})
    assert groups == []
    assert notes
    # The same two addresses, unlinked, do qualify.
    groups, _ = bundles.group_buys(buys, {})
    assert len(groups) == 1


def test_unindexed_buys_cannot_join_a_group() -> None:
    buys = [_lb("A", 100, None), _lb("B", 100, None)]
    groups, _ = bundles.group_buys(buys, {})
    assert groups == []


# --------------------------------------------------------------------------------------
# coverage: the property that matters most
# --------------------------------------------------------------------------------------


def test_no_tape_is_unavailable_not_zero(tmp_db: sqlite3.Connection) -> None:
    token = "NoTape111111111111111111111111111111111111111"
    _token(tmp_db, token)
    tmp_db.commit()

    report = bundles.analyse(SOL, token, tmp_db)
    assert not report.measured
    assert report.bundled.value is None
    assert report.sniped.value is None
    assert report.bundled.basis is EvidenceBasis.UNAVAILABLE
    assert report.unknowns == ("bundler_pct", "sniper_pct")
    assert "never pulled" in report.reason


def test_tape_starting_after_the_launch_is_unavailable_not_zero(tmp_db: sqlite3.Connection) -> None:
    """The live failure mode: pump.fun's route returns the tail, and the tail has no bundle."""
    token = "LateTape1111111111111111111111111111111111111"
    _token(tmp_db, token)
    late = CREATED_MS + 3_600_000
    _buy(tmp_db, token, "Trader1", slot=CREATE_SLOT + 9000, block_index=10, atoms=ONE_PCT, ts_ms=late)
    _buy(tmp_db, token, "Trader2", slot=CREATE_SLOT + 9000, block_index=11, atoms=ONE_PCT, ts_ms=late)
    tmp_db.commit()

    report = bundles.analyse(SOL, token, tmp_db)
    assert not report.measured
    assert report.bundled.value is None
    assert "not proved complete back to launch" in report.reason


def test_a_wallet_walk_row_at_the_launch_is_not_a_complete_tape(tmp_db: sqlite3.Connection) -> None:
    """The subtlest fail-open path, and the one that nearly shipped.

    ``helius:backfill`` walks a *wallet's* history, so it can leave exactly one row at a
    mint's create slot while holding none of the other transactions in that slot. A naive
    timestamp anchor accepts that as a launch-complete tape, and the bundle share of a
    one-transaction tape is zero by construction — a confident 0% on a mint nobody has
    looked at, which is exactly what would open the curve-velocity gate.
    """
    token = "WalletWalk11111111111111111111111111111111111"
    _token(tmp_db, token)
    _snapshot(tmp_db, token, coverage_from_ms=None)
    _buy(
        tmp_db, token, CREATOR, slot=CREATE_SLOT, block_index=10, atoms=ONE_PCT,
        source="helius:backfill",
    )
    tmp_db.commit()

    report = bundles.analyse(SOL, token, tmp_db)
    assert not report.measured
    assert report.bundled.value is None
    assert "not proved complete back to launch" in report.reason
    assert token not in bundles.measurable_tokens(SOL, tmp_db)

    # The same row from the per-token route IS accepted, because that route paginates
    # backwards through one mint and cannot hold a launch row without the rest.
    tmp_db.execute("UPDATE swaps SET source='pumpfun:trades' WHERE token=?", (token,))
    tmp_db.commit()
    assert bundles.analyse(SOL, token, tmp_db).measured


def test_unknown_creation_time_is_unavailable(tmp_db: sqlite3.Connection) -> None:
    token = "NoBirth11111111111111111111111111111111111111"
    _token(tmp_db, token, created_ms=None)
    _buy(tmp_db, token, "Trader1", slot=CREATE_SLOT, block_index=10, atoms=ONE_PCT)
    tmp_db.commit()

    report = bundles.analyse(SOL, token, tmp_db)
    assert not report.measured
    assert report.bundled.value is None
    assert "creation time unknown" in report.reason


def test_mostly_unindexed_launch_volume_is_refused(tmp_db: sqlite3.Connection) -> None:
    """Silently treating un-adjacent-able volume as unbundled understates. Refuse instead."""
    token = "NoIndex11111111111111111111111111111111111111"
    _token(tmp_db, token)
    _snapshot(tmp_db, token)
    _buy(tmp_db, token, "Whale1", slot=CREATE_SLOT, block_index=None, atoms=9 * ONE_PCT)
    _buy(tmp_db, token, "Small1", slot=CREATE_SLOT, block_index=10, atoms=ONE_PCT)
    tmp_db.commit()

    report = bundles.analyse(SOL, token, tmp_db)
    assert not report.measured
    assert report.bundled.value is None
    assert "no block_index" in report.reason


def test_coverage_proved_by_a_terminated_walk_even_when_the_first_trade_is_late(
    tmp_db: sqlite3.Connection,
) -> None:
    """A token whose first trade came minutes after launch is still measurable if the
    collector walked its history to the end. That is the case ``token_flow`` documented
    the timestamp proxy getting wrong on 4 of 10 live tokens."""
    token = "SlowStart111111111111111111111111111111111111"
    _token(tmp_db, token)
    _snapshot(tmp_db, token, coverage_from_ms=CREATED_MS - 5_000)
    late = CREATED_MS + 400_000
    _buy(tmp_db, token, CREATOR, slot=CREATE_SLOT, block_index=10, atoms=ONE_PCT, ts_ms=late)
    _buy(tmp_db, token, "BundlerA", slot=CREATE_SLOT, block_index=11, atoms=ONE_PCT, ts_ms=late)
    tmp_db.commit()

    report = bundles.analyse(SOL, token, tmp_db)
    assert report.measured
    assert "walked" in report.reason
    assert report.bundled_pct == Decimal(2)


def test_launch_window_excludes_later_buys(launch: sqlite3.Connection) -> None:
    token = LAUNCH_TOKEN
    _buy(
        launch,
        token,
        "LateBuyer",
        slot=CREATE_SLOT + bundles.LAUNCH_WINDOW_SLOTS + 1,
        block_index=5,
        atoms=50 * ONE_PCT,
    )
    launch.commit()

    report = bundles.analyse(SOL, token, launch)
    assert report.bundled_pct == Decimal(9)
    assert report.sniped_pct == Decimal(5)  # the late buyer is not a sniper


# --------------------------------------------------------------------------------------
# entity resolution
# --------------------------------------------------------------------------------------


def test_entity_source_is_none_when_nothing_links(launch: sqlite3.Connection) -> None:
    report = bundles.analyse(SOL, LAUNCH_TOKEN, launch)
    assert report.entity_source is bundles.EntitySource.NONE
    assert any("over-counts independence" in n for n in report.notes)


def test_shared_third_party_fee_payer_merges_two_wallets(tmp_db: sqlite3.Connection) -> None:
    token = "FeePayer1111111111111111111111111111111111111"
    _token(tmp_db, token)
    _snapshot(tmp_db, token)
    _buy(tmp_db, token, CREATOR, slot=CREATE_SLOT, block_index=9, atoms=ONE_PCT, fee_payer="Payer9")
    _buy(tmp_db, token, "RingA", slot=CREATE_SLOT, block_index=10, atoms=ONE_PCT, fee_payer="Payer1")
    _buy(tmp_db, token, "RingB", slot=CREATE_SLOT, block_index=11, atoms=ONE_PCT, fee_payer="Payer1")
    tmp_db.commit()

    mapping, notes = bundles.fallback_entity_links(SOL, ["RingA", "RingB"], tmp_db)
    assert mapping["RingA"] == mapping["RingB"]
    assert any("F1" in n for n in notes)

    report = bundles.analyse(SOL, token, tmp_db)
    assert report.entity_source is bundles.EntitySource.FALLBACK
    # Creator + ring = 2 entities, so the run still qualifies; the ring is one of them.
    assert report.bundle_entities == 2


def test_same_slot_cobuy_across_two_tokens_merges(tmp_db: sqlite3.Connection) -> None:
    for i, other in enumerate(("CoA11111111111111111111111111111111111111111",
                               "CoB11111111111111111111111111111111111111111")):
        _token(tmp_db, other)
        _buy(tmp_db, other, "RingA", slot=500_000 + i, block_index=1, atoms=ONE_PCT)
        _buy(tmp_db, other, "RingB", slot=500_000 + i, block_index=2, atoms=ONE_PCT)
    tmp_db.commit()

    mapping, notes = bundles.fallback_entity_links(SOL, ["RingA", "RingB"], tmp_db)
    assert mapping["RingA"] == mapping["RingB"]
    assert any("F2" in n for n in notes)


def test_a_wide_fee_payer_is_a_service_and_does_not_merge(tmp_db: sqlite3.Connection) -> None:
    token = "Relayer11111111111111111111111111111111111111"
    _token(tmp_db, token)
    wallets = [f"S{i:03d}" for i in range(bundles.MAX_SHARED_FEE_PAYER_WALLETS + 1)]
    for i, wallet in enumerate(wallets):
        _buy(tmp_db, token, wallet, slot=CREATE_SLOT, block_index=i, atoms=1, fee_payer="Relay1")
    tmp_db.commit()

    mapping, notes = bundles.fallback_entity_links(SOL, wallets, tmp_db)
    assert mapping == {}
    assert any("service" in n for n in notes)


def test_the_entity_graph_wins_when_it_has_anything(tmp_db: sqlite3.Connection) -> None:
    tmp_db.execute(
        "INSERT INTO entities (entity_id, chain, archetype, confidence, size, edge_types_json, "
        " created_ms, updated_ms, version) VALUES (?,?,?,?,?,?,?,?,?)",
        ("sol:ent:abc", SOL.value, "trader", 0.9, 2, "[]", CREATED_MS, CREATED_MS, 1),
    )
    for address in ("RingA", "RingB"):
        tmp_db.execute(
            "INSERT INTO entity_members (entity_id, chain, address) VALUES (?,?,?)",
            ("sol:ent:abc", SOL.value, address),
        )
    tmp_db.commit()

    mapping, source, notes = bundles.resolve_launch_entities(SOL, ["RingA", "RingB"], tmp_db)
    assert source is bundles.EntitySource.GRAPH
    assert mapping == {"RingA": "sol:ent:abc", "RingB": "sol:ent:abc"}
    assert any("tape fallback was not used" in n for n in notes)


# --------------------------------------------------------------------------------------
# persistence and the dossier call site
# --------------------------------------------------------------------------------------


def test_store_round_trips_a_measurement(launch: sqlite3.Connection) -> None:
    token = LAUNCH_TOKEN
    bundles.store(bundles.analyse(SOL, token, launch), launch)
    launch.commit()

    row = launch.execute(
        "SELECT * FROM token_bundles WHERE chain=? AND token=?", (SOL.value, token)
    ).fetchone()
    assert row["coverage"] == "measured"
    assert Decimal(row["bundled_pct"]) == Decimal(9)
    assert Decimal(row["sniped_pct"]) == Decimal(5)
    assert row["supply_basis"] == bundles.SupplyBasis.PUMPFUN_STANDARD_VERIFIED.value
    assert row["model"] == bundles.MODEL_ID

    roles = {
        r["address"]: r["role"]
        for r in launch.execute(
            "SELECT address, role FROM token_bundle_members WHERE token=?", (token,)
        )
    }
    assert roles == {
        CREATOR: "bundler",
        "BundlerA": "bundler",
        "BundlerB": "bundler",
        "Sniper1": "sniper",
    }


def test_store_records_a_refusal_with_null_percentages(tmp_db: sqlite3.Connection) -> None:
    """A stored refusal is how 'the number is missing' becomes 'the tape is missing'."""
    token = "NoTape222222222222222222222222222222222222222"
    _token(tmp_db, token)
    tmp_db.commit()
    bundles.store(bundles.analyse(SOL, token, tmp_db), tmp_db)
    tmp_db.commit()

    row = tmp_db.execute("SELECT * FROM token_bundles WHERE token=?", (token,)).fetchone()
    assert row["coverage"] == "unavailable"
    assert row["bundled_pct"] is None
    assert row["sniped_pct"] is None
    assert row["reason"]


def test_collect_bundles_emits_no_claim_when_unmeasurable(tmp_db: sqlite3.Connection) -> None:
    """No claim means the property stays unanswered, which keeps curve-velocity closed."""
    token = "NoTape333333333333333333333333333333333333333"
    _token(tmp_db, token)
    tmp_db.commit()

    claims, receipts, status = bundles.collect_bundles(token, SOL, tmp_db)
    assert claims == []
    assert status == "ok"  # we ran and found nothing; the module is not "down"
    assert receipts and receipts[0].basis is EvidenceBasis.UNAVAILABLE


def test_collect_bundles_emits_both_claims_when_measured(launch: sqlite3.Connection) -> None:
    claims, receipts, status = bundles.collect_bundles(LAUNCH_TOKEN, SOL, launch)
    assert status == "ok"
    assert {c.prop for c in claims} == {"bundler_pct", "sniper_pct"}
    assert {c.value for c in claims} == {Decimal(9), Decimal(5)}
    assert all(c.provider == "bundles" for c in claims)
    assert receipts


def test_claims_land_in_the_dyor_vocabulary(launch: sqlite3.Connection) -> None:
    """The wiring contract: both property names must already be known to dyor."""
    from kaiba.intelligence import dyor

    claims, _, _ = bundles.collect_bundles(LAUNCH_TOKEN, SOL, launch)
    for claim in claims:
        assert claim.prop in dyor.NUM_PROPERTIES
        assert claim.prop in dyor.TRACKED_PROPERTIES
    resolution = dyor.resolve(claims)
    assert resolution.n("bundler_pct") == Decimal(9)
    assert resolution.n("sniper_pct") == Decimal(5)


def test_a_measured_bundle_share_satisfies_the_curve_velocity_gate(launch: sqlite3.Connection) -> None:
    """The point of the whole module: the lane's gate can now be answered without GMGN."""
    from kaiba.execution import lanes

    max_bundler = Decimal(str(lanes.DEFAULT_PARAMS[lanes.Lane.CURVE_VELOCITY]["max_bundler_pct"]))
    report = bundles.analyse(SOL, LAUNCH_TOKEN, launch)
    assert report.bundled_pct is not None
    assert report.bundled_pct < max_bundler  # 9% < 20%, so the gate passes on evidence


# --------------------------------------------------------------------------------------
# provenance and calibration
# --------------------------------------------------------------------------------------

#: Module constants that are not thresholds and so are not expected in the provenance
#: table: identifiers, arithmetic helpers, and the freshness budget, which is a cache
#: policy rather than a decision boundary.
_NOT_THRESHOLDS = {"ZERO", "HUNDRED", "FRESHNESS_BUDGET_S"}


def test_every_threshold_declares_its_provenance() -> None:
    """A silently added knob fails the build, the way triage.py's table does it."""
    numeric = {
        name
        for name, value in vars(bundles).items()
        if name.isupper() and isinstance(value, int | Decimal) and not isinstance(value, bool)
    }
    undeclared = numeric - set(bundles.THRESHOLD_PROVENANCE) - _NOT_THRESHOLDS
    assert not undeclared, f"thresholds with no provenance entry: {sorted(undeclared)}"
    stale = set(bundles.THRESHOLD_PROVENANCE) - numeric
    assert not stale, f"provenance entries for constants that no longer exist: {sorted(stale)}"


def test_invented_thresholds_say_so() -> None:
    """Every entry must classify itself, and the invented ones must use the word."""
    for name, text in bundles.THRESHOLD_PROVENANCE.items():
        assert any(
            word in text for word in ("INVENTED", "MEASURED", "DERIVED", "DEFINITIONAL", "STRUCTURAL")
        ), f"{name} does not classify its own provenance"


def test_separation_refuses_to_report_a_gap_on_a_tiny_sample(launch: sqlite3.Connection) -> None:
    bundles.build_corpus(SOL, conn=launch)
    result = bundles.separation(SOL, launch)
    assert result.gap_pp is None
    assert "too small" in result.verdict
    assert result.measured >= 1


def test_separation_reads_the_corpus_and_not_the_tape(launch: sqlite3.Connection) -> None:
    """Same contract as ``rug_separation``: one table, one answer, one SQL query to check.

    It used to call ``analyse`` on every measurable mint, which on the live database is a
    27-minute recompute that can disagree with the table an operator would check it
    against. A measurable token with no stored row now contributes nothing.
    """
    result = bundles.separation(SOL, launch)
    assert result.measured == 0


def test_measurable_tokens_excludes_a_late_tape(tmp_db: sqlite3.Connection) -> None:
    good = "Good1111111111111111111111111111111111111111"
    bad = "Bad11111111111111111111111111111111111111111"
    _token(tmp_db, good)
    _snapshot(tmp_db, good)
    _buy(tmp_db, good, CREATOR, slot=CREATE_SLOT, block_index=10, atoms=ONE_PCT)
    _token(tmp_db, bad)
    _buy(
        tmp_db, bad, CREATOR, slot=CREATE_SLOT, block_index=10, atoms=ONE_PCT,
        ts_ms=CREATED_MS + 3_600_000,
    )
    tmp_db.commit()

    found = bundles.measurable_tokens(SOL, tmp_db)
    assert good in found
    assert bad not in found


# --------------------------------------------------------------------------------------
# idempotence: one row per token, a deeper tape wins, a later refusal does not
# --------------------------------------------------------------------------------------
#
# ``token_bundles`` held zero rows against 6,814 stored dossiers because nothing called
# ``store``. Wiring it is one line; making the wiring safe to run on a schedule is what
# these tests are. Three properties, and the third is the one that is not obvious:
#
#   1. a recompute updates the row, it never duplicates it;
#   2. a token whose tape has since deepened gets the better answer, not the first one;
#   3. a later *refusal* does not erase an earlier measurement, because ``analyse`` can
#      refuse for reasons that are about the caller rather than about the tape.


def _deepen(conn: sqlite3.Connection) -> None:
    """Add the two buys that turn a lone creator buy into a qualifying bundle."""
    _buy(conn, LAUNCH_TOKEN, "BundlerA", slot=CREATE_SLOT, block_index=11, atoms=3 * ONE_PCT)
    _buy(conn, LAUNCH_TOKEN, "BundlerB", slot=CREATE_SLOT, block_index=12, atoms=4 * ONE_PCT)
    conn.commit()


@pytest.fixture
def shallow(tmp_db: sqlite3.Connection) -> sqlite3.Connection:
    """The same launch as ``launch``, but the tape holds only the creator's own buy.

    This is the state 4,043 of our 4,071 'complete' tapes are in: written once, never
    touched, median span 41 seconds. It measures -- coverage is proved -- and it measures
    0% bundled, because one transaction cannot be a bundle. That answer is correct for
    the tape we hold and wrong about the launch, which is exactly why rule 2 exists.
    """
    _token(tmp_db, LAUNCH_TOKEN)
    _snapshot(tmp_db, LAUNCH_TOKEN)
    _buy(tmp_db, LAUNCH_TOKEN, CREATOR, slot=CREATE_SLOT, block_index=10, atoms=2 * ONE_PCT)
    tmp_db.commit()
    return tmp_db


def _row(conn: sqlite3.Connection, token: str = LAUNCH_TOKEN) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM token_bundles WHERE chain=? AND token=?", (SOL.value, token)
    ).fetchone()
    assert row is not None, f"no corpus row for {token}"
    return row


def _count(conn: sqlite3.Connection, table: str, token: str = LAUNCH_TOKEN) -> int:
    return int(
        conn.execute(f"SELECT COUNT(*) FROM {table} WHERE token=?", (token,)).fetchone()[0]
    )


def test_recomputing_a_token_updates_its_row_and_never_duplicates_it(
    launch: sqlite3.Connection,
) -> None:
    for _ in range(3):
        outcome = bundles.store(bundles.analyse(SOL, LAUNCH_TOKEN, launch), launch)
        assert outcome is bundles.StoreOutcome.WROTE
    launch.commit()

    assert _count(launch, "token_bundles") == 1
    # And the members did not accumulate either: four addresses took part, four rows.
    assert _count(launch, "token_bundle_members") == 4


def test_a_deepened_tape_replaces_the_first_answer(shallow: sqlite3.Connection) -> None:
    """Rule 2. The launch is fixed; what changed is how much of it we hold."""
    bundles.store(bundles.analyse(SOL, LAUNCH_TOKEN, shallow), shallow)
    first = _row(shallow)
    assert first["coverage"] == "measured"
    assert Decimal(first["bundled_pct"]) == Decimal(0)  # one buy cannot be a bundle
    assert first["bundle_groups"] == 0

    _deepen(shallow)
    outcome = bundles.store(bundles.analyse(SOL, LAUNCH_TOKEN, shallow), shallow)
    shallow.commit()

    assert outcome is bundles.StoreOutcome.WROTE
    second = _row(shallow)
    assert Decimal(second["bundled_pct"]) == Decimal(9)
    assert second["bundle_groups"] == 1
    assert second["launch_buys"] == 3
    assert second["computed_ms"] >= first["computed_ms"]
    assert _count(shallow, "token_bundles") == 1


def test_a_deepened_tape_rewrites_the_member_roles_and_leaves_nothing_stale(
    shallow: sqlite3.Connection,
) -> None:
    """The creator was alone and therefore not a bundler; now the same address is one.

    An upsert on the member rows would leave the old role behind or collide on the
    primary key. They are deleted and rewritten for this reason.
    """
    bundles.store(bundles.analyse(SOL, LAUNCH_TOKEN, shallow), shallow)
    before = dict(
        shallow.execute(
            "SELECT address, role FROM token_bundle_members WHERE token=?", (LAUNCH_TOKEN,)
        ).fetchall()
    )
    assert before == {CREATOR: "creator"}

    _deepen(shallow)
    bundles.store(bundles.analyse(SOL, LAUNCH_TOKEN, shallow), shallow)
    shallow.commit()

    after = dict(
        shallow.execute(
            "SELECT address, role FROM token_bundle_members WHERE token=?", (LAUNCH_TOKEN,)
        ).fetchall()
    )
    assert after == {CREATOR: "bundler", "BundlerA": "bundler", "BundlerB": "bundler"}


def test_a_later_refusal_does_not_erase_a_stored_measurement(launch: sqlite3.Connection) -> None:
    """Rule 3, the asymmetry.

    ``analyse`` refuses for reasons that are not always about the tape -- a connection to
    a database with no ``curve_snapshots``, an entity graph mid-rebuild, a transient
    ``sqlite3.Error`` swallowed by a coverage lookup. Rule 2 applied blindly would let
    any of those delete a real measurement and leave a NULL behind, and the NULL is the
    value ``curve-velocity`` fails closed on, so the lane would go dark on a token we had
    already measured.
    """
    bundles.store(bundles.analyse(SOL, LAUNCH_TOKEN, launch), launch)
    refusal = bundles.BundleReport(
        chain=SOL,
        token=LAUNCH_TOKEN,
        coverage=bundles.Coverage.UNAVAILABLE,
        reason="curve snapshots unreadable",
    )

    outcome = bundles.store(refusal, launch)
    launch.commit()

    assert outcome is bundles.StoreOutcome.KEPT_MEASURED
    row = _row(launch)
    assert row["coverage"] == "measured"
    assert Decimal(row["bundled_pct"]) == Decimal(9)
    detail = json.loads(row["detail_json"])
    assert detail["last_refusal"]["reason"] == "curve snapshots unreadable"
    assert detail["later_refusals"] == 1
    # The members of the kept measurement survive too.
    assert _count(launch, "token_bundle_members") == 4


def test_repeated_refusals_are_counted_not_accumulated(launch: sqlite3.Connection) -> None:
    """A nightly sweep over a thin tape must not grow detail_json without bound."""
    bundles.store(bundles.analyse(SOL, LAUNCH_TOKEN, launch), launch)
    refusal = bundles.BundleReport(
        chain=SOL, token=LAUNCH_TOKEN, coverage=bundles.Coverage.UNAVAILABLE, reason="thin tape"
    )
    for _ in range(4):
        bundles.store(refusal, launch)
    launch.commit()

    detail = json.loads(_row(launch)["detail_json"])
    assert detail["later_refusals"] == 4
    assert isinstance(detail["last_refusal"], dict)


def test_allow_downgrade_overwrites_a_measurement_when_explicitly_asked(
    launch: sqlite3.Connection,
) -> None:
    bundles.store(bundles.analyse(SOL, LAUNCH_TOKEN, launch), launch)
    refusal = bundles.BundleReport(
        chain=SOL, token=LAUNCH_TOKEN, coverage=bundles.Coverage.UNAVAILABLE, reason="retired"
    )

    outcome = bundles.store(refusal, launch, allow_downgrade=True)
    launch.commit()

    assert outcome is bundles.StoreOutcome.WROTE
    row = _row(launch)
    assert row["coverage"] == "unavailable"
    assert row["bundled_pct"] is None
    assert _count(launch, "token_bundle_members") == 0


def test_a_refusal_under_a_new_model_replaces_the_old_measurement(
    launch: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A number the current definition would not produce must not wear its name.

    Rule 3 protects a measurement only while it is a measurement *this* definition made.
    Once MODEL_ID moves, the stored row is an artefact of a definition no longer in the
    tree, and keeping it would let the corpus report a mix of two definitions as one.
    """
    bundles.store(bundles.analyse(SOL, LAUNCH_TOKEN, launch), launch)
    monkeypatch.setattr(bundles, "MODEL_ID", "kaiba-bundles-v2")
    refusal = bundles.BundleReport(
        chain=SOL, token=LAUNCH_TOKEN, coverage=bundles.Coverage.UNAVAILABLE, reason="redefined"
    )

    outcome = bundles.store(refusal, launch)
    launch.commit()

    assert outcome is bundles.StoreOutcome.WROTE
    row = _row(launch)
    assert row["coverage"] == "unavailable"
    assert row["model"] == "kaiba-bundles-v2"


def test_a_refusal_replaces_a_stored_refusal(tmp_db: sqlite3.Connection) -> None:
    token = "NoTape444444444444444444444444444444444444444"
    _token(tmp_db, token)
    tmp_db.commit()
    bundles.store(bundles.analyse(SOL, token, tmp_db), tmp_db)
    bundles.store(
        bundles.BundleReport(
            chain=SOL, token=token, coverage=bundles.Coverage.UNAVAILABLE, reason="newer reason"
        ),
        tmp_db,
    )
    tmp_db.commit()

    assert _row(tmp_db, token)["reason"] == "newer reason"
    assert _count(tmp_db, "token_bundles", token) == 1


# --------------------------------------------------------------------------------------
# the wiring: every computed report is persisted
# --------------------------------------------------------------------------------------


def test_the_dossier_call_site_persists_every_measurement(launch: sqlite3.Connection) -> None:
    """The whole point. ``collect_bundles`` used to compute this and throw it away."""
    claims, _, status = bundles.collect_bundles(LAUNCH_TOKEN, SOL, launch)
    launch.commit()

    assert status == "ok" and claims
    row = _row(launch)
    assert row["coverage"] == "measured"
    assert Decimal(row["bundled_pct"]) == Decimal(9)
    assert row["model"] == bundles.MODEL_ID


def test_the_dossier_call_site_persists_a_refusal_too(tmp_db: sqlite3.Connection) -> None:
    """'The tape for this mint was never pulled' is a finding about our collection."""
    token = "NoTape555555555555555555555555555555555555555"
    _token(tmp_db, token)
    tmp_db.commit()

    claims, _, status = bundles.collect_bundles(token, SOL, tmp_db)
    tmp_db.commit()

    assert claims == [] and status == "ok"
    row = _row(tmp_db, token)
    assert row["coverage"] == "unavailable"
    assert row["bundled_pct"] is None
    assert "never pulled" in row["reason"]


def test_persist_false_writes_nothing(launch: sqlite3.Connection) -> None:
    claims, _, _ = bundles.collect_bundles(LAUNCH_TOKEN, SOL, launch, persist=False)
    launch.commit()

    assert claims
    assert launch.execute("SELECT COUNT(*) FROM token_bundles").fetchone()[0] == 0


def test_a_write_failure_does_not_break_the_dossier_scan(launch: sqlite3.Connection) -> None:
    """Tier 1 has a 7.6 s budget; it is not the place to discover a locked database.

    The failure is real rather than mocked: the table is gone, which is what a node
    running a schema older than 024_bundles.sql looks like.
    """
    launch.execute("DROP TABLE token_bundles")
    launch.commit()

    claims, receipts, status = bundles.collect_bundles(LAUNCH_TOKEN, SOL, launch)

    assert status == "ok"
    assert {c.prop for c in claims} == {"bundler_pct", "sniper_pct"}
    assert receipts


# --------------------------------------------------------------------------------------
# the corpus
# --------------------------------------------------------------------------------------


def _trade(
    conn: sqlite3.Connection,
    token: str,
    *,
    exit_reason: str,
    trade_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO trades (trade_id, position_id, lane, mode, chain, token, opened_ms, "
        " closed_ms, hold_s, cost_native, proceeds_native, pnl_native, pnl_pct, exit_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            trade_id or f"t-{token[:8]}-{exit_reason[:12]}",
            f"p-{token[:8]}",
            "migration-fade",
            "shadow",
            SOL.value,
            token,
            CREATED_MS,
            CREATED_MS + 60_000,
            60,
            "1000000",
            "900000",
            "-100000",
            -10.0,
            exit_reason,
        ),
    )


def test_build_corpus_writes_a_row_for_every_target(launch: sqlite3.Connection) -> None:
    stats = bundles.build_corpus(SOL, conn=launch)
    launch.commit()

    assert stats.considered == 1
    assert stats.measured == 1
    assert stats.failed == 0
    assert _row(launch)["coverage"] == "measured"
    assert "1 measured" in stats.summary()


def test_the_corpus_covers_tokens_we_traded_even_when_they_cannot_be_measured(
    tmp_db: sqlite3.Connection,
) -> None:
    """A token with an outcome is the only kind that can calibrate anything.

    Without this, a token we traded and cannot measure has no row at all, and 'we never
    looked' becomes indistinguishable from 'we looked and the tape starts an hour late'.
    Only the second is a finding, and it is the finding that describes our own 49 trades.
    """
    traded = "Traded22222222222222222222222222222222222222"
    _token(tmp_db, traded)
    _buy(
        tmp_db,
        traded,
        CREATOR,
        slot=CREATE_SLOT,
        block_index=10,
        atoms=ONE_PCT,
        ts_ms=CREATED_MS + 3_600_000,
        source="helius:backfill",
    )
    _trade(tmp_db, traded, exit_reason="rug:lp_-90.1pct")
    tmp_db.commit()

    assert traded not in bundles.measurable_tokens(SOL, tmp_db)
    assert traded in bundles.corpus_targets(SOL, tmp_db)

    stats = bundles.build_corpus(SOL, conn=tmp_db)
    tmp_db.commit()

    assert stats.refused == 1
    row = _row(tmp_db, traded)
    assert row["coverage"] == "unavailable"
    assert "not proved complete back to launch" in row["reason"]
    assert stats.reasons


def test_build_corpus_steps_over_a_token_that_raises(
    launch: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 4,778-mint sweep that dies on one malformed row has produced nothing."""
    other = "Second222222222222222222222222222222222222222"
    _token(launch, other)
    _snapshot(launch, other)
    _buy(launch, other, CREATOR, slot=CREATE_SLOT, block_index=10, atoms=ONE_PCT)
    launch.commit()

    real = bundles.analyse

    def boom(chain, token, conn=None, **kw):  # type: ignore[no-untyped-def]
        if token == LAUNCH_TOKEN:
            raise RuntimeError("malformed row")
        return real(chain, token, conn, **kw)

    monkeypatch.setattr(bundles, "analyse", boom)
    stats = bundles.build_corpus(SOL, conn=launch)
    launch.commit()

    assert stats.failed == 1
    assert stats.measured == 1
    assert _row(launch, other)["coverage"] == "measured"


def test_only_missing_resumes_a_sweep_instead_of_redoing_it(launch: sqlite3.Connection) -> None:
    bundles.build_corpus(SOL, conn=launch)
    again = bundles.build_corpus(SOL, conn=launch, only_missing=True)

    assert again.skipped == 1
    assert again.measured == 0


def test_only_missing_does_not_skip_a_row_from_an_older_model(
    launch: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundles.build_corpus(SOL, conn=launch)
    monkeypatch.setattr(bundles, "MODEL_ID", "kaiba-bundles-v2")

    again = bundles.build_corpus(SOL, conn=launch, only_missing=True)

    assert again.skipped == 0
    assert again.measured == 1


# --------------------------------------------------------------------------------------
# the question the corpus exists for
# --------------------------------------------------------------------------------------


def _corpus_row(
    conn: sqlite3.Connection,
    token: str,
    *,
    share: str | None,
    pct: str | None = None,
    basis: str | None = "pumpfun_standard_verified",
    coverage: str = "measured",
) -> None:
    conn.execute(
        "INSERT INTO token_bundles (chain, token, computed_ms, model, coverage, reason, "
        " supply_basis, bundled_pct, bundled_share_of_launch_buys) VALUES (?,?,?,?,?,?,?,?,?)",
        (SOL.value, token, CREATED_MS, bundles.MODEL_ID, coverage, "fixture", basis, pct, share),
    )


def test_rug_separation_refuses_on_a_sample_this_size(tmp_db: sqlite3.Connection) -> None:
    """The honest answer for our own book, and the one this function must give today."""
    rugged = "RuggedA1111111111111111111111111111111111111"
    survived = "SurvivedB11111111111111111111111111111111111"
    _trade(tmp_db, rugged, exit_reason="rug:lp_-90.1pct")
    _trade(tmp_db, survived, exit_reason="stop_loss")
    _corpus_row(tmp_db, rugged, share="40")
    _corpus_row(tmp_db, survived, share="2")
    tmp_db.commit()

    result = bundles.rug_separation(SOL, tmp_db)

    assert result.gap_pp is None
    assert result.closed_trades == 2
    assert result.rugged_tokens == 1
    assert result.measured_rugged == 1
    assert result.measured_survived == 1
    assert "NO ANSWER" in result.verdict
    assert f"{bundles.MIN_SEPARATION_ARM} per arm" in result.verdict


def test_rug_separation_reads_the_corpus_and_never_recomputes(launch: sqlite3.Connection) -> None:
    """A measurable tape with no stored row must contribute nothing.

    The answer has to be reproducible with one SQL query against ``token_bundles``. A
    function that quietly fell back to ``analyse`` would report a separation that is not
    in the table it claims to be reading.
    """
    _trade(launch, LAUNCH_TOKEN, exit_reason="rug:lp_-90.1pct")
    launch.commit()

    result = bundles.rug_separation(SOL, launch)

    assert result.tokens == 1
    assert result.in_corpus == 0
    assert result.measured == 0


def test_rug_separation_counts_a_token_as_rugged_if_any_leg_rugged(
    tmp_db: sqlite3.Connection,
) -> None:
    token = "Twice111111111111111111111111111111111111111"
    _trade(tmp_db, token, exit_reason="trailing_stop", trade_id="leg-1")
    _trade(tmp_db, token, exit_reason="rug:lp_-56.3pct", trade_id="leg-2")
    _corpus_row(tmp_db, token, share="12")
    tmp_db.commit()

    result = bundles.rug_separation(SOL, tmp_db)

    assert result.closed_trades == 2
    assert result.tokens == 1
    assert result.rugged_tokens == 1
    assert result.measured_rugged == 1
    assert result.measured_survived == 0


def test_rug_separation_reports_a_gap_once_both_arms_are_large_enough(
    tmp_db: sqlite3.Connection,
) -> None:
    """The arithmetic, on a synthetic sample we do not have and may never have."""
    for i in range(bundles.MIN_SEPARATION_ARM):
        rug = f"Rug{i}1111111111111111111111111111111111111111"[:44]
        ok = f"Ok{i}11111111111111111111111111111111111111111"[:44]
        _trade(tmp_db, rug, exit_reason="rug:lp_-90.1pct")
        _trade(tmp_db, ok, exit_reason="stop_loss")
        _corpus_row(tmp_db, rug, share=str(30 + i))
        _corpus_row(tmp_db, ok, share=str(4 + i))
    tmp_db.commit()

    result = bundles.rug_separation(SOL, tmp_db)

    assert result.measured_rugged == bundles.MIN_SEPARATION_ARM
    assert result.measured_survived == bundles.MIN_SEPARATION_ARM
    assert result.rugged_median == Decimal(32)
    assert result.survived_median == Decimal(6)
    assert result.gap_pp == Decimal(26)
    assert "no significance test" in result.verdict


def test_rug_separation_refuses_to_mix_supply_bases(tmp_db: sqlite3.Connection) -> None:
    """Curve reserves at launch are 79.31% of supply, so the same bundle is 1.26x larger.

    A gap computed across the two bases is the denominator, not the bundling. MELT's 24pp
    is a share of total supply; a row that does not say which denominator it used cannot
    be compared with it.
    """
    bases = ("pumpfun_standard_verified", "curve_launch_reserves")
    for i in range(bundles.MIN_SEPARATION_ARM):
        rug = f"Rugb{i}111111111111111111111111111111111111111"[:44]
        ok = f"Okb{i}1111111111111111111111111111111111111111"[:44]
        _trade(tmp_db, rug, exit_reason="rug:lp_-90.1pct")
        _trade(tmp_db, ok, exit_reason="stop_loss")
        _corpus_row(tmp_db, rug, share=str(30 + i), pct=str(30 + i), basis=bases[i % 2])
        _corpus_row(tmp_db, ok, share=str(4 + i), pct=str(4 + i), basis=bases[0])
    tmp_db.commit()

    mixed = bundles.rug_separation(SOL, tmp_db, metric=bundles.SeparationMetric.SUPPLY)
    assert mixed.gap_pp is None
    assert "mix supply bases" in mixed.verdict
    assert len(mixed.supply_bases) == 2

    # The same rows on the share-of-launch-buys metric need no denominator, so they do
    # produce a gap. The two metrics are not interchangeable and this is the proof.
    unmixed = bundles.rug_separation(SOL, tmp_db, metric=bundles.SeparationMetric.LAUNCH_BUYS)
    assert unmixed.gap_pp == Decimal(26)


def test_rug_separation_ignores_a_stored_refusal(tmp_db: sqlite3.Connection) -> None:
    """``coverage`` decides whether a row has a number, not whether a column is populated.

    The row below says ``unavailable`` and carries a percentage anyway, which is what a
    row written by an older model, a half-finished migration or a hand edit looks like.
    ``coverage`` is the NOT NULL, CHECK-constrained column the schema put there to be the
    single answer to 'did we measure this', so it is the one that is read.
    """
    token = "Refused11111111111111111111111111111111111111"
    _trade(tmp_db, token, exit_reason="rug:lp_-90.1pct")
    _corpus_row(tmp_db, token, share="40", coverage="unavailable", basis=None)
    tmp_db.commit()

    result = bundles.rug_separation(SOL, tmp_db)

    assert result.in_corpus == 1
    assert result.measured == 0
    assert result.rugged_median is None


def test_a_measured_zero_is_claimed_only_behind_a_proved_tape(
    shallow: sqlite3.Connection,
) -> None:
    """The riskiest number this module emits, pinned with the reason it is allowed.

    A tape holding one launch buy measures 0% bundled, and 0% passes
    ``curve-velocity``'s ``max_bundler_pct: 20`` gate. That is fail-open on exactly the
    field the gate exists for, so the only thing separating "we looked and found none"
    from "we did not look" is the coverage proof in ``analyse``. This test asserts both
    halves: with the proof the zero is a claim, and without it there is no claim at all.

    It is also the shape of most of the live corpus. On 2026-09-21, 1,122 of 2,470
    measured mints had exactly one launch-window buy, so the coverage proof carries the
    correctness of nearly half the rows.
    """
    claims, _, status = bundles.collect_bundles(LAUNCH_TOKEN, SOL, shallow)
    assert status == "ok"
    assert {(c.prop, c.value) for c in claims} == {
        ("bundler_pct", Decimal(0)),
        ("sniper_pct", Decimal(0)),
    }

    # Same single buy, but the coverage proof withdrawn: the tape now starts an hour
    # after the launch and the per-token route cannot vouch for it either.
    shallow.execute("DELETE FROM curve_snapshots WHERE token=?", (LAUNCH_TOKEN,))
    shallow.execute(
        "UPDATE swaps SET ts_ms=?, source='helius:backfill' WHERE token=?",
        (CREATED_MS + 3_600_000, LAUNCH_TOKEN),
    )
    shallow.commit()

    report = bundles.analyse(SOL, LAUNCH_TOKEN, shallow)
    assert not report.measured
    assert report.bundled_pct is None
    unproved_claims, _, _ = bundles.collect_bundles(LAUNCH_TOKEN, SOL, shallow)
    assert unproved_claims == []
