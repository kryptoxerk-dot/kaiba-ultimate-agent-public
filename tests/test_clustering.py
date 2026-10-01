"""Tests for the clustering pass.

The properties worth pinning here are not "does it find clusters" — ``test_cluster.py``
already covers both derivation and ``entity.py``'s resolution. They are the four things
that make a populated graph safe to gate money on:

1. a service address in the middle does not merge everyone who touched it;
2. a component too large to attribute is refused, and refused *visibly*, not deleted;
3. "we did not check" never reads as "they are independent";
4. the confluence reading and the identity reading of the same graph disagree in the
   directions their consumers need, and neither can be made to answer confidently on
   evidence it does not have.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from kaiba.core.schemas import Chain, EdgeType, EvidenceBasis, Receipt, now_ms
from kaiba.intelligence import cluster, clustering, entity, hubs

SOL = Chain.SOL

# base58 excludes 0, O, I and l. 'z' pads rather than '1' so that "Sink1" and "Sink11"
# do not pad to the same string, which silently halved a fan-out fixture once already.
_PAD = "z"


def addr(tag: str) -> str:
    """A deterministic, shape-valid Solana address for a readable tag."""
    return (tag + _PAD * 44)[:44]


ALICE = addr("Alice")
BOB = addr("Bob")
CAROL = addr("Carol")
DAVE = addr("Dave")
ERIN = addr("Erin")
FUNDER = addr("Funder")
SERVICE = addr("Service")
TOKEN = addr("Token")


# --------------------------------------------------------------------------------------
# fixture helpers
# --------------------------------------------------------------------------------------


def _transfer(conn: sqlite3.Connection, src: str, dst: str, tx: str, ts: int = 1_700_000_000_000,
              amount: int = 1_000_000_000, first_inbound: int = 0) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO transfers (chain, tx, slot, ts_ms, src, dst, amount, "
        " is_first_inbound, source) VALUES (?,?,?,?,?,?,?,?,?)",
        (SOL.value, tx, 1, ts, src, dst, str(amount), first_inbound, "test"),
    )


def _swap(conn: sqlite3.Connection, wallet: str, token: str, tx: str, ts: int = 1_700_000_000_000,
          fee_payer: str | None = None, slot: int = 1, side: str = "buy") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO swaps (chain, tx, slot, block_index, ts_ms, wallet, token, side, "
        " amount_token, amount_native, source, fee_payer) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (SOL.value, tx, slot, 0, ts, wallet, token, side, "1000", "1000", "test", fee_payer),
    )


def _meta(conn: sqlite3.Connection, tx: str, signers: list[str], complete: bool = True) -> None:
    cluster.record_swap_meta(
        conn, SOL, tx,
        {"fee_payer": signers[0], "signers": signers, "signers_complete": complete,
         "alt_authority": None, "source": "test"},
    )


@pytest.fixture
def db(tmp_db: sqlite3.Connection) -> sqlite3.Connection:
    hubs.seed_hubs(tmp_db)
    return tmp_db


# --------------------------------------------------------------------------------------
# provenance: a silently added knob fails the build
# --------------------------------------------------------------------------------------

#: Not thresholds: identifiers and provider coordinates carry no decision boundary.
_NOT_THRESHOLDS: set[str] = set()


def test_every_threshold_declares_its_provenance() -> None:
    numeric = {
        name
        for name, value in vars(clustering).items()
        if name.isupper() and isinstance(value, int | float) and not isinstance(value, bool)
    }
    undeclared = numeric - set(clustering.THRESHOLD_PROVENANCE) - _NOT_THRESHOLDS
    assert not undeclared, f"thresholds with no provenance entry: {sorted(undeclared)}"
    stale = set(clustering.THRESHOLD_PROVENANCE) - numeric
    assert not stale, f"provenance entries for constants that no longer exist: {sorted(stale)}"


def test_every_threshold_classifies_itself() -> None:
    for name, text in clustering.THRESHOLD_PROVENANCE.items():
        assert any(
            word in text
            for word in ("INVENTED", "MEASURED", "DERIVED", "DEFINITIONAL", "STRUCTURAL", "OPERATIONAL")
        ), f"{name} does not classify its own provenance"


def test_every_edge_type_declares_a_strength_and_a_trap() -> None:
    """A rule with no declared strength cannot be filtered on, and one with no declared
    trap is being presented as a fact."""
    missing_strength = set(EdgeType) - set(clustering.RULE_STRENGTH)
    assert not missing_strength, f"edge types with no strength: {sorted(missing_strength)}"
    missing_trap = set(EdgeType) - set(clustering.RULE_TRAP)
    assert not missing_trap, f"edge types with no declared false-positive case: {sorted(missing_trap)}"


def test_hard_edges_are_control_or_flow_grade() -> None:
    from kaiba.core.schemas import HARD_EDGES

    for edge_type in HARD_EDGES:
        assert clustering.RULE_STRENGTH[edge_type] in (
            clustering.RuleStrength.CONTROL,
            clustering.RuleStrength.FLOW,
        )


# --------------------------------------------------------------------------------------
# 1. a service in the middle does not merge everyone who touched it
# --------------------------------------------------------------------------------------


def test_fanout_service_is_detected_and_registered(db: sqlite3.Connection) -> None:
    for i in range(clustering.MAX_SERVICE_FANOUT + 5):
        _transfer(db, SERVICE, addr(f"Sink{i}"), f"tx{i}")
    found = clustering.detect_service_addresses(db, SOL)
    assert [f.address for f in found] == [SERVICE]
    assert found[0].role == "transfer"
    assert found[0].counterparties == clustering.MAX_SERVICE_FANOUT + 5
    assert hubs.is_hub(SOL, SERVICE, db)


def test_service_detection_can_measure_without_writing(db: sqlite3.Connection) -> None:
    """A threshold sweep must not leave hub rows behind."""
    for i in range(clustering.MAX_SERVICE_FANOUT + 2):
        _transfer(db, SERVICE, addr(f"Sink{i}"), f"tx{i}")
    found = clustering.detect_service_addresses(db, SOL, register=False)
    assert found and not found[0].registered
    assert not hubs.is_hub(SOL, SERVICE, db)


def test_a_disperser_does_not_merge_its_recipients(db: sqlite3.Connection) -> None:
    """The failure this module was written for: one hub, one giant entity.

    Without fan-out detection the 25 sinks are one component through SERVICE, because
    every leg is a hard DIRECT_TRANSFER edge.
    """
    sinks = [addr(f"Sink{i}") for i in range(clustering.MAX_SERVICE_FANOUT + 5)]
    for i, sink in enumerate(sinks):
        _transfer(db, SERVICE, sink, f"tx{i}")

    unpruned = cluster.derive_direct_transfer(db, SOL)
    assert len(unpruned) == len(sinks), "precondition: the hub links every recipient"

    result = clustering.run(db, SOL)
    assert result.entities == [], "a disperser's recipients are not one operator"
    assert result.quarantined == []
    cover = clustering.coverage_for(SOL, [SERVICE], db)
    assert cover[SERVICE] is clustering.Coverage.HUB


def test_a_second_run_still_reports_the_services_it_found(db: sqlite3.Connection) -> None:
    """The count must not fall to zero once the hub rows exist, or the run history reads
    as though the fan-out problem went away."""
    for i in range(clustering.MAX_SERVICE_FANOUT + 5):
        _transfer(db, SERVICE, addr(f"Sink{i}"), f"tx{i}")
    first = clustering.run(db, SOL)
    second = clustering.run(db, SOL)
    assert len(first.services) == len(second.services) == 1
    assert second.services[0].registered
    assert clustering.last_run(SOL, db)["fanout_hubs"] == 1


def test_a_hub_someone_else_labelled_is_not_reported_as_our_finding(db: sqlite3.Connection) -> None:
    hubs.add_hub(SOL, SERVICE, "cex", label="an exchange", source="label-provider", conn=db)
    for i in range(clustering.MAX_SERVICE_FANOUT + 5):
        _transfer(db, SERVICE, addr(f"Sink{i}"), f"tx{i}")
    assert clustering.detect_service_addresses(db, SOL) == []
    row = db.execute("SELECT kind, source FROM hub_addresses WHERE address=?", (SERVICE,)).fetchone()
    assert (row["kind"], row["source"]) == ("cex", "label-provider"), "another label is not overwritten"


def test_edges_written_before_the_label_are_pruned(db: sqlite3.Connection) -> None:
    """``cluster.persist_edges`` never deletes, so a naive earlier run's hub edges would
    otherwise keep the giant component alive after the hub is labelled."""
    sinks = [addr(f"Sink{i}") for i in range(clustering.MAX_SERVICE_FANOUT + 5)]
    for i, sink in enumerate(sinks):
        _transfer(db, SERVICE, sink, f"tx{i}")
    # what `kaiba wallet cluster` does today: derive with no fan-out detection at all
    cluster.persist_edges(cluster.derive_direct_transfer(db, SOL), db)
    before = db.execute("SELECT COUNT(*) FROM cluster_edges").fetchone()[0]
    assert before == len(sinks)

    result = clustering.run(db, SOL)
    assert result.pruned_edges == before
    assert db.execute("SELECT COUNT(*) FROM cluster_edges").fetchone()[0] == 0
    assert result.entities == []


def test_pruning_can_be_turned_off(db: sqlite3.Connection) -> None:
    _transfer(db, ALICE, BOB, "t1")
    cluster.persist_edges(cluster.derive_direct_transfer(db, SOL), db)
    result = clustering.run(db, SOL, prune_hubs=False)
    assert result.pruned_edges == 0
    assert db.execute("SELECT COUNT(*) FROM cluster_edges").fetchone()[0] == 1


def test_cosigner_fanout_uses_swap_meta_not_swaps(db: sqlite3.Connection) -> None:
    """swaps.fee_payer is NULL on the whole pump.fun tape, so the signer list is the only
    complete source and the detector has to read it."""
    partners = [addr(f"Partner{i}") for i in range(clustering.MAX_SERVICE_FANOUT + 3)]
    for i, partner in enumerate(partners):
        _meta(db, f"sig{i}", [SERVICE, partner])
    found = {f.address: f for f in clustering.detect_service_addresses(db, SOL, register=False)}
    assert SERVICE in found
    assert found[SERVICE].role == "co-signer"


# --------------------------------------------------------------------------------------
# 2. an unattributable component is refused visibly, not merged and not deleted
# --------------------------------------------------------------------------------------


def _long_chain(db: sqlite3.Connection, n: int) -> list[str]:
    """A payment chain A->B->C->... Each address has two counterparties, so no single
    address trips the fan-out cap; only the transitive closure is oversized."""
    members = [addr(f"Chain{i:02d}") for i in range(n)]
    for i in range(n - 1):
        _transfer(db, members[i], members[i + 1], f"chaintx{i}")
    return members


def test_oversized_component_is_quarantined_not_merged(db: sqlite3.Connection) -> None:
    members = _long_chain(db, clustering.MAX_ENTITY_SIZE + 5)
    result = clustering.run(db, SOL)

    assert result.largest_component == len(members)
    assert [q.size for q in result.quarantined] == [len(members)]
    assert result.entities == [], "an oversized component must not be asserted as an operator"
    assert entity.entity_ids_for(SOL, members, db) == {}


def test_quarantined_members_are_not_silently_unclustered(db: sqlite3.Connection) -> None:
    """Deleting the component would return its members to 'unclustered', which is the
    fail-open hole: unclustered reads as independent."""
    members = _long_chain(db, clustering.MAX_ENTITY_SIZE + 5)
    clustering.run(db, SOL)
    cover = clustering.coverage_for(SOL, members, db)
    assert set(cover.values()) == {clustering.Coverage.QUARANTINED}

    stored = clustering.quarantine(SOL, db)
    assert len(stored) == 1
    assert stored[0].size == len(members)
    assert set(stored[0].members) == set(members)
    assert EdgeType.DIRECT_TRANSFER in stored[0].edge_types
    assert stored[0].held_by, "a refused component records who held it together"


def test_quarantine_key_is_stable_across_runs(db: sqlite3.Connection) -> None:
    _long_chain(db, clustering.MAX_ENTITY_SIZE + 5)
    first = clustering.run(db, SOL).quarantined[0].component_key
    second = clustering.run(db, SOL).quarantined[0].component_key
    assert first == second


# --------------------------------------------------------------------------------------
# 3. "we did not check" never reads as "they are independent"
# --------------------------------------------------------------------------------------


def test_an_address_we_never_examined_is_unchecked_not_unclustered(db: sqlite3.Connection) -> None:
    """A pump.fun tape row gives a wallet no funding record and no signer list, so neither
    identity-grade rule has ever been evaluated against it."""
    for i in range(5):
        _swap(db, addr(f"Tape{i}"), TOKEN, f"tapetx{i}")
    clustering.run(db, SOL)
    cover = clustering.coverage_for(SOL, [addr(f"Tape{i}") for i in range(5)], db)
    assert set(cover.values()) == {clustering.Coverage.UNCHECKED}
    assert clustering.coverage_summary(SOL, db)[clustering.Coverage.UNCHECKED.value] >= 5


def test_five_unchecked_addresses_cannot_satisfy_confluence_five(db: sqlite3.Connection) -> None:
    """The spoof this module exists to stop, stated as a test.

    ``entity.independent_entity_count`` returns 5 here, because absence of evidence is its
    only honest default. ``satisfies`` returns None — falsy — so a caller that writes
    ``if view.satisfies(5)`` refuses instead of firing.
    """
    tape = [addr(f"Tape{i}") for i in range(5)]
    for i, wallet in enumerate(tape):
        _swap(db, wallet, TOKEN, f"tapetx{i}")
    clustering.run(db, SOL)

    assert entity.independent_entity_count(SOL, tape, db) == 5  # the old answer

    view = clustering.independence(SOL, tape, db)
    assert view.floor == 0
    assert view.ceiling == 5
    assert view.satisfies(5) is None
    assert not view.satisfies(5), "None must be falsy so the gate fails closed"
    assert view.basis is EvidenceBasis.ESTIMATED
    assert "never checked" in view.explain()


def test_an_address_the_pass_has_never_seen_is_unchecked(db: sqlite3.Connection) -> None:
    """No coverage row at all must read the same as a row that says we had no inputs.
    A missing row is the most common way a consumer meets an address it should not trust."""
    clustering.run(db, SOL)
    stranger = addr("Stranger")
    assert clustering.coverage_for(SOL, [stranger], db) == {stranger: clustering.Coverage.UNCHECKED}
    view = clustering.independence(SOL, [stranger], db)
    assert view.unchecked == [stranger]
    assert view.floor == 0
    assert view.satisfies(1) is None


def test_the_schema_refuses_to_store_independence_as_a_status(db: sqlite3.Connection) -> None:
    """``unchecked`` is derived, not stored, and there is deliberately no ``independent``.
    The CHECK constraint and the enum have to agree or one of them is decoration."""
    stamp = now_ms()

    def insert(status: str) -> None:
        db.execute(
            "INSERT INTO clustering_coverage (chain, address, status, checked, funding_basis, "
            " signer_basis, run_id, updated_ms) VALUES (?,?,?,0,'unavailable','unavailable',1,?)",
            (SOL.value, f"{status}{addr('X')}"[:44], status, stamp),
        )

    for status in clustering._STORED_STATUSES:
        insert(status.value)
    for rejected in (clustering.Coverage.UNCHECKED.value, "independent"):
        with pytest.raises(sqlite3.IntegrityError):
            insert(rejected)


def test_a_checked_and_unlinked_address_is_reported_as_such(db: sqlite3.Connection) -> None:
    """Checked-and-clean is a different, stronger statement than never-looked-at."""
    wallets = [addr(f"Solo{i}") for i in range(5)]
    for i, wallet in enumerate(wallets):
        _swap(db, wallet, TOKEN, f"solotx{i}")
        _meta(db, f"solotx{i}", [wallet])  # a complete signer list: the co-sign rule ran
    clustering.run(db, SOL)

    cover = clustering.coverage_for(SOL, wallets, db)
    assert set(cover.values()) == {clustering.Coverage.UNCLUSTERED}
    view = clustering.independence(SOL, wallets, db)
    assert view.complete
    assert view.floor == 5
    assert view.satisfies(5) is True
    assert view.basis is EvidenceBasis.DERIVED


def test_a_partly_checked_set_refuses_rather_than_rounds_up(db: sqlite3.Connection) -> None:
    checked = [addr(f"Solo{i}") for i in range(3)]
    for i, wallet in enumerate(checked):
        _swap(db, wallet, TOKEN, f"solotx{i}")
        _meta(db, f"solotx{i}", [wallet])
    unchecked = [addr(f"Tape{i}") for i in range(2)]
    for i, wallet in enumerate(unchecked):
        _swap(db, wallet, TOKEN, f"tapetx{i}")
    clustering.run(db, SOL)

    view = clustering.independence(SOL, checked + unchecked, db)
    assert view.floor == 3
    assert view.ceiling == 5
    assert view.satisfies(5) is None
    assert view.satisfies(3) is True
    assert view.satisfies(6) is False


# --------------------------------------------------------------------------------------
# 4. one graph, two readings, opposite errors
# --------------------------------------------------------------------------------------


def test_confluence_collapses_a_quarantined_component_identity_refuses_it(
    db: sqlite3.Connection,
) -> None:
    """The same component, read by the two gates that fear opposite errors.

    Confluence must not see 30 opinions where there are 30 linked addresses, so it
    collapses them to one. Concentration must not claim 30 addresses are one holder on
    evidence we refused to assert, so it declines to answer.
    """
    members = _long_chain(db, clustering.MAX_ENTITY_SIZE + 5)
    clustering.run(db, SOL)

    conf = clustering.independence(SOL, members, db, policy=clustering.Policy.CONFLUENCE)
    assert conf.floor == 1, "a linked component is one opinion"
    assert conf.satisfies(5) is False
    assert not conf.unresolved

    ident = clustering.independence(SOL, members, db, policy=clustering.Policy.IDENTITY)
    assert ident.unresolved == sorted(members)
    assert ident.satisfies(5) is None, "no identity claim is defensible here"


def test_lead_lag_is_one_opinion_for_confluence_and_two_identities(db: sqlite3.Connection) -> None:
    """entity.py is right never to merge a copier into an identity, and confluence is
    right always to collapse one: a copy bot is a different person, not a second opinion."""
    leader, follower = addr("Leader"), addr("Follow")
    for i in range(cluster.LEAD_LAG_MIN_TOKENS + 1):
        token = addr(f"Tok{i}")
        base = 1_700_000_000_000 + i * 10_000_000
        _swap(db, leader, token, f"lead{i}", ts=base)
        _swap(db, follower, token, f"follow{i}", ts=base + 5_000)
        _meta(db, f"lead{i}", [leader])
        _meta(db, f"follow{i}", [follower])
    clustering.run(db, SOL)

    edges = clustering.edges_between(SOL, [leader, follower], db)
    assert any(e.edge_type is EdgeType.LEAD_LAG for e in edges)
    assert entity.entity_ids_for(SOL, [leader, follower], db) == {}, "no identity merge"

    conf = clustering.independence(SOL, [leader, follower], db, policy=clustering.Policy.CONFLUENCE)
    assert conf.floor == 1
    ident = clustering.independence(SOL, [leader, follower], db, policy=clustering.Policy.IDENTITY)
    assert ident.floor == 2


def test_edges_between_filters_by_what_the_rule_claims(db: sqlite3.Connection) -> None:
    """A consumer picks its bar by kind of evidence, not only by a confidence number."""
    _transfer(db, ALICE, BOB, "t1")
    _meta(db, "t1", [ALICE])
    clustering.run(db, SOL)

    control_only = clustering.edges_between(
        SOL, [ALICE, BOB], db, strengths=[clustering.RuleStrength.CONTROL]
    )
    flow = clustering.edges_between(SOL, [ALICE, BOB], db, strengths=[clustering.RuleStrength.FLOW])
    assert control_only == []
    assert [e.edge_type for e in flow] == [EdgeType.DIRECT_TRANSFER]


# --------------------------------------------------------------------------------------
# funding
# --------------------------------------------------------------------------------------


def _raw_funding_tx(target: str, funder: str, amount: int = 5_000_000_000) -> dict[str, Any]:
    """The shape helius getTransactionsForAddress returns for a plain SOL transfer."""
    return {
        "transaction": {
            "signatures": ["fundsig"],
            "message": {
                "header": {"numRequiredSignatures": 1},
                "accountKeys": [funder, target, "11111111111111111111111111111111"],
            },
        },
        "meta": {
            "err": None,
            "preBalances": [amount + 10_000, 0, 1],
            "postBalances": [5_000, amount, 1],
        },
        "blockTime": 1_700_000_000,
    }


def test_funder_is_read_from_the_balance_delta() -> None:
    funder, amount, reason = clustering._funder_from_earliest_tx(
        _raw_funding_tx(ALICE, FUNDER), ALICE, SOL
    )
    assert funder == FUNDER
    assert amount == 5_000_000_000
    assert reason == ""


def test_the_relayer_is_not_mistaken_for_the_funder() -> None:
    """A sponsored funding: a third party signs and pays the fee, the lamports come from
    somewhere else. Ranking signer-first would name the relayer."""
    relayer, source = addr("Relayer"), addr("Source")
    raw = {
        "transaction": {
            "signatures": ["sig"],
            "message": {
                "header": {"numRequiredSignatures": 1},
                "accountKeys": [relayer, ALICE, source],
            },
        },
        "meta": {"err": None, "preBalances": [100_000, 0, 9_000_000_000],
                 "postBalances": [95_000, 5_000_000_000, 4_000_000_000]},
        "blockTime": 1_700_000_000,
    }
    funder, amount, _reason = clustering._funder_from_earliest_tx(raw, ALICE, SOL)
    assert funder == source, "the account whose outflow covers the gain is the funder"
    assert amount == 5_000_000_000


def test_a_transaction_that_drains_the_address_is_not_a_funding(db: sqlite3.Connection) -> None:
    raw = _raw_funding_tx(ALICE, FUNDER)
    raw["meta"]["preBalances"], raw["meta"]["postBalances"] = (
        raw["meta"]["postBalances"],
        raw["meta"]["preBalances"],
    )
    funder, amount, reason = clustering._funder_from_earliest_tx(raw, ALICE, SOL)
    assert funder is None and amount is None
    assert "did not increase" in reason


def test_a_provider_failure_is_unavailable_not_zero(db: sqlite3.Connection, monkeypatch) -> None:
    from kaiba.providers import helius

    receipt = Receipt(provider="helius", endpoint="tx.getTransactionsForAddress",
                      basis=EvidenceBasis.UNAVAILABLE, note="boom")
    monkeypatch.setattr(helius, "get_transactions_for_address", lambda *a, **k: (None, receipt))
    fact = clustering.lookup_funding(ALICE, SOL, db)
    assert fact.funder is None
    assert fact.basis is EvidenceBasis.UNAVAILABLE
    assert fact.reason == "boom"
    assert not fact.known


def test_a_definite_negative_is_stored_so_it_is_not_re_bought(db: sqlite3.Connection) -> None:
    """We read the earliest transaction and it was not a funding. That answer cost
    credits; remembering it is the point."""
    _swap(db, ALICE, TOKEN, "s1")
    raw = _raw_funding_tx(ALICE, FUNDER)
    raw["meta"]["preBalances"], raw["meta"]["postBalances"] = (
        raw["meta"]["postBalances"],
        raw["meta"]["preBalances"],
    )
    funder, _amount, reason = clustering._funder_from_earliest_tx(raw, ALICE, SOL)
    fact = clustering.FundingFact(
        chain=SOL, address=ALICE, funder=funder, amount_lamports=None, funded_ms=None,
        tx="sig", basis=EvidenceBasis.UNAVAILABLE, reason=reason,
        receipt=Receipt(provider="helius", endpoint="tx.getTransactionsForAddress"),
    )
    assert clustering.store_funding(fact, credits=10, conn=db) is True
    row = db.execute(
        "SELECT basis, credits FROM clustering_funding WHERE address=?", (ALICE,)
    ).fetchone()
    assert row["basis"] == EvidenceBasis.UNAVAILABLE.value
    assert row["credits"] == 10
    assert ALICE not in clustering.funding_candidates(db, SOL)


def test_a_rate_limit_does_not_buy_a_seven_day_cooldown(db: sqlite3.Connection, monkeypatch) -> None:
    """Observed live: three addresses became unqueryable for a week over a 'retry in
    0.1s' limiter refusal, because a transient failure was stored like an answer."""
    from kaiba.providers import helius

    receipt = Receipt(provider="helius", endpoint="tx.getTransactionsForAddress",
                      basis=EvidenceBasis.UNAVAILABLE, note="rate limited: retry in 0.1s")
    monkeypatch.setattr(helius, "get_transactions_for_address", lambda *a, **k: (None, receipt))
    _swap(db, ALICE, TOKEN, "s1")
    fact = clustering.lookup_funding(ALICE, SOL, db)
    assert fact.retryable
    assert clustering.store_funding(fact, credits=0, conn=db) is False
    assert db.execute("SELECT COUNT(*) FROM clustering_funding").fetchone()[0] == 0
    assert ALICE in clustering.funding_candidates(db, SOL), "it must be tried again"


def _store_funder(db: sqlite3.Connection, address: str, funder: str, ts: int, amount: int) -> None:
    clustering.store_funding(
        clustering.FundingFact(
            chain=SOL, address=address, funder=funder, amount_lamports=amount, funded_ms=ts,
            tx=f"fund:{address}", basis=EvidenceBasis.VERIFIED_ONCHAIN, reason="test",
            receipt=Receipt(provider="helius", endpoint="tx.getTransactionsForAddress"),
        ),
        conn=db,
    )


def test_same_funder_edges_come_out_of_the_receipted_table(db: sqlite3.Connection) -> None:
    ts = now_ms()
    for wallet in (ALICE, BOB, CAROL):
        _store_funder(db, wallet, FUNDER, ts, 1_000_000_000)
    edges = clustering.derive_funding_edges(db, SOL)
    assert {e.edge_type for e in edges} == {EdgeType.SAME_FUNDER}
    assert len(edges) == 3  # every pair
    # base + equal amounts + same minute
    expected = (
        cluster.SAME_FUNDER_BASE
        + cluster.SAME_FUNDER_AMOUNT_BONUS
        + cluster.SAME_FUNDER_TIME_BONUS
    )
    assert all(abs(e.confidence - expected) < 1e-9 for e in edges)


def test_the_verified_funder_window_is_wider_than_the_transfers_one(db: sqlite3.Connection) -> None:
    """Measured over 3,468 live candidate pairs: cluster.py's 30-day window keeps 70% and
    365 days keeps 95%. Dropping a real pair is an under-merge, the fail-open direction."""
    assert clustering.VERIFIED_FUNDER_WINDOW_DAYS > cluster.SAME_FUNDER_WINDOW_DAYS
    ts = now_ms()
    apart = 200 * 86_400_000  # inside 365 days, far outside 30
    _store_funder(db, ALICE, FUNDER, ts, 1_000_000_000)
    _store_funder(db, BOB, FUNDER, ts - apart, 7_000_000_000)
    edges = clustering.derive_funding_edges(db, SOL)
    assert len(edges) == 1
    assert edges[0].confidence == cluster.SAME_FUNDER_BASE, "no bonus at 200 days apart"


def test_a_funding_gap_beyond_the_window_links_nobody(db: sqlite3.Connection) -> None:
    ts = now_ms()
    beyond = (clustering.VERIFIED_FUNDER_WINDOW_DAYS + 1) * 86_400_000
    _store_funder(db, ALICE, FUNDER, ts, 1_000_000_000)
    _store_funder(db, BOB, FUNDER, ts - beyond, 1_000_000_000)
    assert clustering.derive_funding_edges(db, SOL) == []


def test_an_exchange_withdrawal_links_nobody(db: sqlite3.Connection) -> None:
    """Measured on the live database: 117 of 1,373 resolved wallets were funded straight
    out of Binance's labelled SOL hot wallet. They are 117 exchange customers, not an
    operator, and they are the largest funder group in the table by a factor of three."""
    binance = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"
    assert hubs.is_hub(SOL, binance, db), "precondition: the seed list labels it"
    ts = now_ms()
    for wallet in (ALICE, BOB, CAROL):
        _store_funder(db, wallet, binance, ts, 1_000_000_000)
    assert clustering.derive_funding_edges(db, SOL) == []


def test_a_funder_that_feeds_too_many_wallets_links_nobody(db: sqlite3.Connection) -> None:
    ts = now_ms()
    for i in range(cluster.SAME_FUNDER_GROUP_CAP + 1):
        _store_funder(db, addr(f"Fed{i:02d}"), FUNDER, ts, 1_000_000_000)
    assert clustering.derive_funding_edges(db, SOL) == []


def test_funding_alone_does_not_merge_an_identity_but_does_collapse_confluence(
    db: sqlite3.Connection,
) -> None:
    """The direction-of-error decision, in one test.

    Five wallets from one funder is exactly the cheap sybil ``confluence-5`` exists to
    reject. ``entity.py`` will not call them one operator on a single soft edge type — it
    is right not to, for concentration — so the confluence reading has to collapse them
    itself.
    """
    ring = [ALICE, BOB, CAROL, DAVE, ERIN]
    ts = now_ms()
    for i, wallet in enumerate(ring):
        _swap(db, wallet, TOKEN, f"ring{i}")
        _meta(db, f"ring{i}", [wallet])
        _store_funder(db, wallet, FUNDER, ts, 1_000_000_000)
    clustering.run(db, SOL)

    assert entity.independent_entity_count(SOL, ring, db) == 5, "entity.py under-merges here"
    conf = clustering.independence(SOL, ring, db, policy=clustering.Policy.CONFLUENCE)
    assert conf.floor == 1
    assert conf.satisfies(5) is False


def test_enrich_funding_spends_nothing_by_default(db: sqlite3.Connection, monkeypatch) -> None:
    from kaiba.providers import helius

    def explode(*_a: Any, **_k: Any) -> None:
        raise AssertionError("the pass must not call a provider without a credit budget")

    monkeypatch.setattr(helius, "get_transactions_for_address", explode)
    _swap(db, ALICE, TOKEN, "s1")
    assert clustering.enrich_funding(db, SOL, max_credits=0) == (0, 0, 0)
    result = clustering.run(db, SOL)
    assert result.helius_credits is None, "None means the funding pass never ran, not that it was free"


def test_enrich_funding_reports_the_ledger_not_an_estimate(db: sqlite3.Connection, monkeypatch) -> None:
    from kaiba.providers import helius

    spent = {"used": 0}
    calls: list[str] = []

    def fake_fetch(address: str, **_k: Any) -> tuple[dict[str, Any], Receipt]:
        calls.append(address)
        spent["used"] += clustering.FUNDING_CREDITS_PER_WALLET
        return (
            {"data": [_raw_funding_tx(address, FUNDER)], "paginationToken": None},
            Receipt(provider="helius", endpoint="tx.getTransactionsForAddress"),
        )

    monkeypatch.setattr(helius, "available", lambda: True)
    monkeypatch.setattr(helius, "get_transactions_for_address", fake_fetch)
    monkeypatch.setattr(helius, "budget_status", lambda **_k: {"used": spent["used"]})
    for i in range(10):
        _swap(db, addr(f"Cand{i}"), TOKEN, f"c{i}")

    looked_up, resolved, credits = clustering.enrich_funding(db, SOL, max_credits=30)
    assert looked_up == 3, "the budget, not a wallet count, is what stops the pass"
    assert resolved == 3
    assert credits == 30
    assert len(calls) == 3

    # Resumable: the same budget against the same candidates buys the *next* three, not
    # the same three again.
    clustering.enrich_funding(db, SOL, max_credits=30)
    assert len(calls) == 6
    assert len(set(calls)) == 6


def test_an_explicit_address_list_is_resumable_too(db: sqlite3.Connection, monkeypatch) -> None:
    """A caller that re-submits its list after an interruption must not pay twice."""
    from kaiba.providers import helius

    spent = {"used": 0}
    calls: list[str] = []

    def fake_fetch(address: str, **_k: Any) -> tuple[dict[str, Any], Receipt]:
        calls.append(address)
        spent["used"] += clustering.FUNDING_CREDITS_PER_WALLET
        return (
            {"data": [_raw_funding_tx(address, FUNDER)], "paginationToken": None},
            Receipt(provider="helius", endpoint="tx.getTransactionsForAddress"),
        )

    monkeypatch.setattr(helius, "available", lambda: True)
    monkeypatch.setattr(helius, "get_transactions_for_address", fake_fetch)
    monkeypatch.setattr(helius, "budget_status", lambda **_k: {"used": spent["used"]})
    wanted = [ALICE, BOB, CAROL]

    assert clustering.enrich_funding(db, SOL, max_credits=100, addresses=wanted)[0] == 3
    assert clustering.enrich_funding(db, SOL, max_credits=100, addresses=wanted) == (0, 0, 0)
    assert calls == wanted


# --------------------------------------------------------------------------------------
# shared fee payer — written so it strengthens when swaps.fee_payer is repaired
# --------------------------------------------------------------------------------------


def test_shared_fee_payer_is_a_soft_edge(db: sqlite3.Connection) -> None:
    for i in range(clustering.MIN_SHARED_FEE_PAYER_TXS):
        _swap(db, ALICE, TOKEN, f"fa{i}", fee_payer=SERVICE)
        _swap(db, BOB, TOKEN, f"fb{i}", fee_payer=SERVICE)
    edges = clustering.derive_shared_fee_payer(db, SOL)
    assert [e.edge_type for e in edges] == [EdgeType.SHARED_COUNTERPARTY]
    assert edges[0].confidence == clustering.SHARED_FEE_PAYER_CONFIDENCE
    assert clustering.RULE_STRENGTH[EdgeType.SHARED_COUNTERPARTY] is clustering.RuleStrength.SPONSORSHIP


def test_one_shared_sponsorship_is_not_enough(db: sqlite3.Connection) -> None:
    _swap(db, ALICE, TOKEN, "fa0", fee_payer=SERVICE)
    _swap(db, BOB, TOKEN, "fb0", fee_payer=SERVICE)
    assert clustering.derive_shared_fee_payer(db, SOL) == []


def test_a_paymaster_for_the_crowd_links_nobody(db: sqlite3.Connection) -> None:
    for i in range(clustering.MAX_SHARED_FEE_PAYER_WALLETS + 1):
        for j in range(clustering.MIN_SHARED_FEE_PAYER_TXS):
            _swap(db, addr(f"Paid{i:02d}"), TOKEN, f"p{i}_{j}", fee_payer=SERVICE)
    assert clustering.derive_shared_fee_payer(db, SOL) == []


def test_the_rule_survives_a_null_fee_payer_column(db: sqlite3.Connection) -> None:
    """21,245 of 24,909 swap rows have fee_payer NULL. The rule must return nothing rather
    than fail, and must pick the signer list up instead where one exists."""
    for i in range(clustering.MIN_SHARED_FEE_PAYER_TXS):
        _swap(db, ALICE, TOKEN, f"na{i}", fee_payer=None)
        _swap(db, BOB, TOKEN, f"nb{i}", fee_payer=None)
    assert clustering.derive_shared_fee_payer(db, SOL) == []
    for i in range(clustering.MIN_SHARED_FEE_PAYER_TXS):
        _meta(db, f"na{i}", [SERVICE, ALICE])
        _meta(db, f"nb{i}", [SERVICE, BOB])
    assert [e.edge_type for e in clustering.derive_shared_fee_payer(db, SOL)] == [
        EdgeType.SHARED_COUNTERPARTY
    ]


# --------------------------------------------------------------------------------------
# the pass itself
# --------------------------------------------------------------------------------------


def _mixed_graph(db: sqlite3.Connection) -> None:
    _transfer(db, ALICE, BOB, "t1")
    _meta(db, "t1", [ALICE])
    _swap(db, ALICE, TOKEN, "t1")
    _long_chain(db, clustering.MAX_ENTITY_SIZE + 5)
    for i in range(5):
        _swap(db, addr(f"Tape{i}"), TOKEN, f"tape{i}")


def test_running_twice_changes_no_row_identity(db: sqlite3.Connection) -> None:
    """Idempotence as the operator asked for it: no duplicate edge, no renumbered entity."""
    _mixed_graph(db)

    def snapshot() -> dict[str, list[tuple[Any, ...]]]:
        return {
            "edges": [tuple(r) for r in db.execute(
                "SELECT chain, a, b, edge_type, confidence FROM cluster_edges ORDER BY 1,2,3,4")],
            "entities": [tuple(r) for r in db.execute(
                "SELECT entity_id, size, confidence FROM entities ORDER BY 1")],
            "members": [tuple(r) for r in db.execute(
                "SELECT entity_id, address FROM entity_members ORDER BY 1,2")],
            "quarantine": [tuple(r) for r in db.execute(
                "SELECT component_key, size, members_json FROM clustering_quarantine ORDER BY 1")],
            "coverage": [tuple(r) for r in db.execute(
                "SELECT address, status, checked, entity_id, component_key "
                "FROM clustering_coverage ORDER BY 1")],
        }

    clustering.run(db, SOL)
    first = snapshot()
    clustering.run(db, SOL)
    assert snapshot() == first


def test_a_run_records_what_it_could_and_could_not_see(db: sqlite3.Connection) -> None:
    _mixed_graph(db)
    result = clustering.run(db, SOL)
    row = clustering.last_run(SOL, db)
    assert row is not None
    assert row["status"] == "ok"
    assert row["model"] == clustering.MODEL_ID
    assert row["addresses_seen"] == result.addresses_seen
    assert row["addresses_checked"] == result.addresses_checked
    assert row["addresses_checked"] < row["addresses_seen"], "coverage is partial and says so"
    assert row["largest_component"] >= row["largest_entity"]
    assert row["helius_credits"] is None


def test_a_failed_run_does_not_stay_running(db: sqlite3.Connection, monkeypatch) -> None:
    monkeypatch.setattr(
        clustering.cluster, "derive_all", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    with pytest.raises(RuntimeError):
        clustering.run(db, SOL)
    row = clustering.last_run(SOL, db)
    assert row is not None and row["status"] == "failed"
    assert "nope" in row["reason"]


def test_independence_of_an_empty_set_is_unavailable(db: sqlite3.Connection) -> None:
    view = clustering.independence(SOL, [], db)
    assert view.basis is EvidenceBasis.UNAVAILABLE
    assert view.floor == 0
    assert view.satisfies(1) is False


def test_hub_addresses_are_excluded_rather_than_counted(db: sqlite3.Connection) -> None:
    """A router in a buyer list is not a sixth independent opinion."""
    router = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
    wallets = [addr(f"Solo{i}") for i in range(4)]
    for i, wallet in enumerate(wallets):
        _swap(db, wallet, TOKEN, f"s{i}")
        _meta(db, f"s{i}", [wallet])
    _swap(db, router, TOKEN, "srouter")
    clustering.run(db, SOL)
    view = clustering.independence(SOL, [*wallets, router], db)
    assert view.hubs == [router]
    assert view.floor == 4
    assert view.satisfies(5) is False
