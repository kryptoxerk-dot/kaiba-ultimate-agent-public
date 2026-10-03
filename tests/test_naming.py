"""Registry naming: a deterministic handle for every wallet we hold evidence on.

What these tests protect, in the order the operator would notice a regression:

* the name is a pure function of the evidence (determinism, idempotent re-runs);
* it never asserts quality we did not grade (a GMGN word is attributed, a grade is real);
* a GMGN label never becomes a word the lanes or the grader read as a finding: it lands in
  ``tags_json`` as ``gmgn:<label>`` and nowhere else (the vendor-label pin at the end);
* entity membership shows and disappears with the graph;
* the registry write merges into ``wallets`` without clobbering an operator's own rows,
  and never plants keys the grader would mistake for provider claims.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from kaiba.core import events as ev
from kaiba.core.db import fetch_all, fetch_one, jdump, jload
from kaiba.core.schemas import Chain, EventKind, Grade, WalletTag
from kaiba.execution import lanes
from kaiba.intelligence import grade, naming, tracker
from kaiba.intelligence.naming import WalletFacts

SOL_A = "Bnd5oBSWpPpXoaWTckgXTVx9TCKkX5GyVqxcrwBzUYsB"
SOL_B = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
SOL_C = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
SOL_D = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
EVM_MIXED = "0x68EEE5c2FE8883A63CD9E5F0e71a3116FB728B3a"
TOKEN_1 = "So11111111111111111111111111111111111111112"
TOKEN_2 = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


# ---------------------------------------------------------------- seeding helpers


def seed_swaps(conn, chain: str, wallet: str, *, buys: int, sells: int, source="pumpfun:trades", start_ms=1_000):
    ts = start_ms
    for i in range(buys):
        conn.execute(
            "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, source) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (chain, f"tx-{wallet[:6]}-b{i}", ts, wallet, TOKEN_1 if i % 2 else TOKEN_2, "buy", str(i + 1), source),
        )
        ts += 1_000
    for i in range(sells):
        conn.execute(
            "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, source) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (chain, f"tx-{wallet[:6]}-s{i}", ts, wallet, TOKEN_1, "sell", str(i + 1), source),
        )
        ts += 1_000
    return ts - 1_000  # last ts written


def seed_gmgn_event(conn, chain: str, wallet: str, tags: list[str], feed="smartmoney", tx="tx1"):
    ev.emit_once(
        EventKind.WALLET_TRADE,
        {
            "chain": chain, "tx": tx, "wallet": wallet, "token": TOKEN_1, "side": "buy",
            "source": f"gmgn:{feed}", "feed": feed, "wallet_name": None, "token_symbol": "X",
            "tags": tags,
        },
        chain=chain,
        subject=wallet,
        dedupe_key=f"wallet.trade:gmgn:{feed}:{chain}:{tx}:{wallet}:buy",
        conn=conn,
    )


def seed_entity(conn, chain: str, entity_id: str, members: list[str]):
    conn.execute(
        "INSERT INTO entities (entity_id, chain, archetype, confidence, size, edge_types_json, created_ms, "
        "updated_ms, version) VALUES (?,?,?,?,?,?,?,?,?)",
        (entity_id, chain, "trader", 0.9, len(members), '["co_signed"]', 1, 1, 1),
    )
    for m in members:
        conn.execute(
            "INSERT INTO entity_members (entity_id, chain, address) VALUES (?,?,?)", (entity_id, chain, m)
        )


def seed_bundle_role(conn, chain: str, wallet: str, role: str, token: str = TOKEN_1):
    conn.execute(
        "INSERT INTO token_bundle_members (chain, token, address, role, atoms, buys) VALUES (?,?,?,?,?,?)",
        (chain, token, wallet, role, "1000", 1),
    )


def seed_score(conn, chain: str, wallet: str, grade_value: str, archetype: str):
    conn.execute(
        "INSERT INTO wallet_scores (chain, address, score, grade, evidence_weight, archetype, model_version, "
        "scored_at_ms) VALUES (?,?,?,?,?,?,?,?)",
        (chain, wallet, 50.0, grade_value, 80.0, archetype, "v1", 1),
    )


def seed_token(conn, chain: str, token: str, creator: str):
    conn.execute(
        "INSERT INTO tokens (chain, address, creator, first_seen_ms) VALUES (?,?,?,?)", (chain, token, creator, 1)
    )


def wallet_row(conn, chain: str, address: str) -> dict:
    row = fetch_one(conn, "SELECT * FROM wallets WHERE chain=? AND address=?", (chain, address))
    assert row is not None, f"{address} was not written to the registry"
    return row


def facts(**overrides) -> WalletFacts:
    base = dict(chain=Chain.SOL, address=SOL_A)
    base.update(overrides)
    return WalletFacts(**base)


# ================================================================ pure naming


def test_short_id_is_six_hex_stable_and_chain_aware():
    first = naming.short_id(Chain.SOL, SOL_A)
    assert first == naming.short_id(Chain.SOL, SOL_A)
    assert len(first) == naming.SHORT_ID_LEN == 6
    assert all(c in "0123456789abcdef" for c in first)
    assert naming.short_id(Chain.SOL, SOL_A) != naming.short_id(Chain.SOL, SOL_B)
    # The same EVM address on two chains is two wallets; case never changes the id.
    assert naming.short_id(Chain.BSC, EVM_MIXED) != naming.short_id(Chain.ROBINHOOD, EVM_MIXED)
    assert naming.short_id(Chain.BSC, EVM_MIXED) == naming.short_id(Chain.BSC, EVM_MIXED.lower())


def test_name_encodes_archetype_gmgn_cohort_entity_and_grade():
    f = facts(
        gmgn_tags={"smart_degen": 12, "arbitrager": 3, "gmgn": 12},
        bundle_roles={"sniper": 2},
        entity_id="sol:ent:ec0941ded8a7ebec",
        entity_size=3,
        grade="B",
    )
    sid = naming.short_id(Chain.SOL, SOL_A)
    assert naming.registry_name(f) == f"sniper#{sid} [gmgn:smart_degen,arbitrager] (entity ec0941, 3 members) grade B"


def test_name_is_deterministic_for_the_same_evidence():
    kwargs = dict(gmgn_tags={"kol": 4, "axiom": 4}, entity_id="sol:ent:abcdef0123456789", entity_size=2)
    assert naming.registry_name(facts(**kwargs)) == naming.registry_name(facts(**kwargs))
    # Insertion order of the evidence dicts must not matter either.
    a = facts(gmgn_tags={"arbitrager": 1, "smart_degen": 1})
    b = facts(gmgn_tags={"smart_degen": 1, "arbitrager": 1})
    assert naming.registry_name(a) == naming.registry_name(b)


def test_wallet_with_no_evidence_is_unknown_with_only_a_handle():
    name = naming.registry_name(facts())
    assert name == f"unknown#{naming.short_id(Chain.SOL, SOL_A)}"
    assert naming.infer_registry_archetype(facts()) == ("unknown", "none")


def test_long_operator_label_is_cut_at_a_word_and_quotes_are_softened():
    existing = {"name": '  my   "whale"  from the 2024 list, do not lose  ', "meta_json": "{}"}
    assert naming.prior_name(facts(existing=existing)) == "my 'whale' from the 2024 list, do not"
    short = {"name": "whale", "meta_json": "{}"}
    assert naming.prior_name(facts(existing=short)) == "whale"
    assert naming.prior_name(facts(existing={"name": None, "meta_json": "{}"})) is None
    # A name this module wrote is not an operator label.
    generated = {"name": "unknown#abc123", "meta_json": jdump({"naming": {"name": "unknown#abc123"}})}
    assert naming.prior_name(facts(existing=generated)) is None


def test_gmgn_quality_word_is_always_attributed_in_the_bracket():
    name = naming.registry_name(facts(gmgn_tags={"smart_degen": 5}))
    assert name.startswith("smart_degen#")
    assert "[gmgn:smart_degen]" in name
    assert naming.name_violates_quality_rule(name) is None
    # GMGN's other two "smart" cohorts name the same archetype word; the bracket attributes it.
    for label in ("app_smart_money", "launchpad_smart"):
        other = naming.registry_name(facts(gmgn_tags={label: 5}))
        assert other.startswith("smart_degen#") and f"[gmgn:{label}]" in other
        assert naming.name_violates_quality_rule(other) is None, other
    # The rule itself catches an unattributed claim.
    assert naming.name_violates_quality_rule("smart_degen#abc123") is not None
    assert naming.name_violates_quality_rule("smart_degen#abc123 [gmgn:arbitrager]") is not None
    assert naming.name_violates_quality_rule("top_trader#abc123 (entity ab, 2 members)") is not None
    assert naming.name_violates_quality_rule("diamond#abc123 grade B") is None


@pytest.mark.parametrize("grade_value", [None, "UNSCORED"])
def test_no_grade_means_no_grade_word(grade_value):
    name = naming.registry_name(facts(grade=grade_value, gmgn_tags={"arbitrager": 1}))
    assert "grade" not in name and "UNSCORED" not in name


def test_real_grades_and_quarantine_are_spelled_out():
    assert naming.registry_name(facts(grade="D")).endswith(" grade D")
    assert naming.registry_name(facts(grade="QUARANTINED")).endswith(" QUARANTINED")


def test_score_quality_archetype_needs_a_grade_to_be_named():
    ungraded = facts(score_archetype="smart_money", grade="UNSCORED")
    assert naming.infer_registry_archetype(ungraded) == ("unknown", "none")
    assert naming.name_violates_quality_rule(naming.registry_name(ungraded)) is None
    graded = facts(score_archetype="smart_money", grade="B")
    assert naming.infer_registry_archetype(graded) == ("smart_degen", "wallet_scores")
    assert naming.name_violates_quality_rule(naming.registry_name(graded)) is None
    # A non-quality archetype from the grader is fine without a grade letter.
    assert naming.infer_registry_archetype(facts(score_archetype="insider", grade="UNSCORED")) == (
        "insider", "wallet_scores",
    )
    # The grader's "trader" is the absence of a finding, not a finding.
    assert naming.infer_registry_archetype(facts(score_archetype="trader", grade="D")) == ("unknown", "none")


def test_our_measurement_claims_the_word_before_the_provider_does():
    f = facts(bundle_roles={"sniper": 1}, gmgn_tags={"sniper": 9})
    assert naming.infer_registry_archetype(f) == ("sniper", "bundles")
    only_gmgn = facts(gmgn_tags={"sniper": 9})
    assert naming.infer_registry_archetype(only_gmgn) == ("sniper", "gmgn")


def test_precedence_sell_only_then_origin_then_provider_cohort():
    everything = facts(
        observed_buys=0, observed_sells=20, bundle_roles={"creator": 1, "sniper": 3},
        gmgn_tags={"smart_degen": 3, "kol": 1}, created_tokens=2,
    )
    assert naming.infer_registry_archetype(everything) == ("sell-only", "swaps")
    no_asymmetry = facts(bundle_roles={"creator": 1, "sniper": 3}, gmgn_tags={"smart_degen": 3, "kol": 1})
    assert naming.infer_registry_archetype(no_asymmetry) == ("creator", "bundles")
    assert naming.infer_registry_archetype(facts(bundle_roles={"sniper": 3}, gmgn_tags={"smart_degen": 3})) == (
        "sniper", "bundles",
    )
    assert naming.infer_registry_archetype(facts(gmgn_tags={"smart_degen": 3, "kol": 1})) == ("kol", "gmgn")
    assert naming.infer_registry_archetype(facts(created_tokens=1)) == ("creator", "tokens")


def test_sell_only_rule_is_the_graders_rule():
    assert naming.is_sell_only(0, 10) is True
    assert naming.is_sell_only(0, 9) is None, "below the minimum sample there is no opinion"
    assert naming.is_sell_only(1, 49) is True, "2% buy share is the floor, inclusive"
    assert naming.is_sell_only(2, 48) is False
    assert naming.is_sell_only(None, None) is None
    assert naming.is_sell_only(5, 5) is False
    # Pinned to grade.py so the two can never disagree.
    assert grade.SELL_ONLY_MIN_TRADES == 10
    assert grade.SELL_ONLY_MAX_BUY_SHARE == Decimal("0.02")


def test_app_tags_stay_out_of_the_name_but_land_in_meta():
    f = facts(gmgn_tags={"axiom": 3, "trojan": 1, "gmgn": 3})
    name = naming.registry_name(f)
    assert "[gmgn:" not in name and "axiom" not in name
    meta = naming.registry_meta(f, name, naming.registry_tags(f))["naming"]
    assert meta["gmgn_apps"] == {"axiom": 3, "gmgn": 3, "trojan": 1}
    assert meta["gmgn_tags"] == {}
    assert "axiom" not in naming.registry_tags(f)


def test_more_than_three_cohort_tags_is_marked_as_overflow():
    f = facts(gmgn_tags={"smart_degen": 1, "kol": 1, "arbitrager": 1, "fresh_wallet": 1})
    assert "[gmgn:smart_degen,kol,arbitrager+]" in naming.registry_name(f)


def test_unknown_gmgn_label_is_kept_not_dropped():
    f = facts(gmgn_tags={"brand_new_cohort": 2, "kol": 1})
    assert naming.cohort_tags(f.gmgn_tags) == ["kol", "brand_new_cohort"]
    assert "gmgn:brand_new_cohort" in naming.registry_tags(f)
    assert "brand_new_cohort" not in naming.registry_tags(f)


def test_registry_tags_keep_gmgn_labels_under_the_vendor_namespace_only():
    f = facts(
        gmgn_tags={"smart_degen": 1, "wash_trader": 1, "launchpad_smart": 1, "kol": 1, "axiom": 1},
        bundle_roles={"creator": 1, "bundler": 2},
        observed_buys=0, observed_sells=30,
    )
    tags = set(naming.registry_tags(f))
    assert {"gmgn:smart_degen", "gmgn:wash_trader", "gmgn:launchpad_smart", "gmgn:kol"} <= tags
    # The label is never written bare, and never as the WalletTag it might "mean".
    assert not ({"smart_degen", "wash_trader", "launchpad_smart", "kol"} & tags)
    assert not ({WalletTag.SMART_MONEY.value, WalletTag.PUMP_SMART.value, WalletTag.KOL.value} & tags)
    # Our own measurements are ours and stay bare.
    assert {WalletTag.DEV.value, WalletTag.BUNDLER.value} <= tags
    assert naming.SELL_ONLY_TAG in tags
    assert "axiom" not in tags and "gmgn:axiom" not in tags
    assert naming.registry_tags(f) == sorted(tags), "sorted, so the JSON is byte-stable"
    assert naming.vendor_tag("smart_degen") == naming.GMGN_TAG_PREFIX + "smart_degen" == grade.VENDOR_TAG_PREFIX + "smart_degen"
    assert not hasattr(naming, "GMGN_TO_WALLET_TAG"), "the v1 label->WalletTag mapping must stay gone"


def test_failure_rate_is_unavailable_not_zero():
    f = facts(observed_buys=3, observed_sells=4)
    meta = naming.registry_meta(f, "x", [])["naming"]
    assert meta["failure_rate"] is None
    assert meta["failure_rate_basis"] == "unavailable"
    assert meta["observed"]["basis"] == "derived"
    unmeasured = naming.registry_meta(facts(), "x", [])["naming"]
    assert unmeasured["observed"]["buys"] is None and unmeasured["observed"]["basis"] == "unavailable"
    assert unmeasured["sell_only"] is None


def test_every_threshold_declares_its_provenance():
    numeric = {
        name
        for name, value in vars(naming).items()
        if name.isupper() and isinstance(value, int | float) and not isinstance(value, bool)
    }
    undeclared = numeric - set(naming.THRESHOLD_PROVENANCE)
    assert not undeclared, f"thresholds with no provenance entry: {sorted(undeclared)}"
    stale = set(naming.THRESHOLD_PROVENANCE) - numeric
    assert not stale, f"provenance entries for constants that no longer exist: {sorted(stale)}"
    for name, text in naming.THRESHOLD_PROVENANCE.items():
        assert any(
            word in text
            for word in ("INVENTED", "MEASURED", "DERIVED", "DEFINITIONAL", "STRUCTURAL", "OPERATIONAL")
        ), f"{name} does not classify its own provenance"


def test_legacy_follow_list_name_still_works():
    """The GMGN follow-list surface is untouched; test_grade.py pins its exact strings."""
    from kaiba.core.schemas import Archetype, WalletScore

    score = WalletScore(address=SOL_A, chain=Chain.SOL, score=55.0, grade=Grade.B, evidence_weight=80.0,
                        archetype=Archetype.EARLY_BUYER)
    assert naming.build_name(grade.WalletEvidence(address=SOL_A, chain=Chain.SOL), score) == "Early Buyer B"


# ================================================================ the registry write


def seed_universe(conn):
    """Four wallets, each with a different mix of evidence, plus one EVM wallet."""
    # A: GMGN smart_degen + sniper role + entity + graded B + pumpfun swaps
    seed_swaps(conn, "sol", SOL_A, buys=6, sells=4, start_ms=10_000)
    seed_gmgn_event(conn, "sol", SOL_A, ["smart_degen", "arbitrager", "gmgn"], tx="ta1")
    seed_gmgn_event(conn, "sol", SOL_A, ["smart_degen", "axiom"], tx="ta2")
    seed_bundle_role(conn, "sol", SOL_A, "sniper", TOKEN_1)
    seed_bundle_role(conn, "sol", SOL_A, "sniper", TOKEN_2)
    seed_score(conn, "sol", SOL_A, "B", "sniper")
    # B: sell-only settlement address, entity member with A and C
    seed_swaps(conn, "sol", SOL_B, buys=0, sells=12, start_ms=20_000)
    # C: token creator, entity member, GMGN wash_trader
    seed_swaps(conn, "sol", SOL_C, buys=2, sells=1, start_ms=30_000)
    seed_token(conn, "sol", TOKEN_2, SOL_C)
    seed_gmgn_event(conn, "sol", SOL_C, ["wash_trader", "trojan"], feed="kol", tx="tc1")
    seed_entity(conn, "sol", "sol:ent:ec0941ded8a7ebec", [SOL_A, SOL_B, SOL_C])
    # D: only ever seen trading, nothing else known
    seed_swaps(conn, "sol", SOL_D, buys=1, sells=0, start_ms=40_000)
    # EVM, mixed case in the tape
    seed_swaps(conn, "robinhood", EVM_MIXED, buys=2, sells=1, source="robinhood", start_ms=50_000)
    seed_swaps(conn, "robinhood", EVM_MIXED.lower(), buys=1, sells=0, source="robinhood", start_ms=60_000)


def test_name_wallets_populates_the_registry_from_every_source(tmp_db):
    seed_universe(tmp_db)
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM wallets")["n"] == 0

    report = naming.name_wallets(tmp_db)

    assert report.wallets_before == 0
    assert report.wallets_after == 5 == report.inserted == report.considered
    assert report.updated == 0 and report.unchanged == 0
    assert report.by_archetype == {"sniper": 1, "sell-only": 1, "wash": 1, "unknown": 2}
    assert report.by_basis == {"bundles": 1, "swaps": 1, "gmgn": 1, "none": 2}
    assert report.by_chain == {"sol": 4, "robinhood": 1}
    assert report.with_gmgn_cohort == 2 and report.in_entity == 3 and report.graded == 1 and report.sell_only == 1

    a = wallet_row(tmp_db, "sol", SOL_A)
    sid = naming.short_id(Chain.SOL, SOL_A)
    assert a["name"] == f"sniper#{sid} [gmgn:smart_degen,arbitrager] (entity ec0941, 3 members) grade B"
    assert a["source"] == "gmgn:smartmoney"
    assert a["first_seen_ms"] == 10_000 and a["last_seen_ms"] == 19_000
    assert a["cohort"] is None, "naming does not make cohort decisions"
    tags = jload(a["tags_json"], [])
    assert {"gmgn:smart_degen", "gmgn:arbitrager", "sniper"} <= set(tags)
    assert not ({"smart_degen", "arbitrager", "smart_money", "gmgn", "gmgn:gmgn", "gmgn:axiom"} & set(tags))
    meta = jload(a["meta_json"], {})["naming"]
    assert meta["version"] == naming.NAMING_VERSION == "naming-v2"
    assert meta["archetype"] == "sniper" and meta["archetype_basis"] == "bundles"
    assert meta["gmgn_tags"] == {"smart_degen": 2, "arbitrager": 1}
    assert meta["gmgn_apps"] == {"axiom": 1, "gmgn": 1}
    assert meta["gmgn_feeds"] == {"smartmoney": 2}
    assert meta["bundle_roles"] == {"sniper": 2}
    assert meta["entity_id"] == "sol:ent:ec0941ded8a7ebec" and meta["entity_size"] == 3
    assert meta["observed"] == {"buys": 6, "sells": 4, "tokens": 2, "sources": {"pumpfun:trades": 10}, "basis": "derived"}
    assert meta["grade"] == "B" and meta["score_archetype"] == "sniper"

    b = wallet_row(tmp_db, "sol", SOL_B)
    assert b["name"].startswith("sell-only#") and "(entity ec0941, 3 members)" in b["name"]
    assert naming.SELL_ONLY_TAG in jload(b["tags_json"], [])
    assert b["source"] == "pumpfun:trades"

    c = wallet_row(tmp_db, "sol", SOL_C)
    # A disqualifying provider label outranks our own "creator" finding: what we would
    # do with the wallet is decided by the worse of the two, and both stay in meta.
    assert c["name"].startswith("wash#") and "[gmgn:wash_trader]" in c["name"]
    assert "trojan" not in c["name"]
    assert jload(c["meta_json"], {})["naming"]["archetype_basis"] == "gmgn"
    assert jload(c["meta_json"], {})["naming"]["created_tokens"] == 1
    assert jload(c["meta_json"], {})["naming"]["gmgn_feeds"] == {"kol": 1}
    assert "gmgn:wash_trader" in jload(c["tags_json"], [])
    assert WalletTag.WASH_TRADER.value not in jload(c["tags_json"], []), "even a bad label stays a vendor's"
    assert c["source"] == "gmgn:kol"

    d = wallet_row(tmp_db, "sol", SOL_D)
    assert d["name"] == f"unknown#{naming.short_id(Chain.SOL, SOL_D)}"
    assert jload(d["meta_json"], {})["naming"]["sell_only"] is None, "one trade is no opinion"

    for row in fetch_all(tmp_db, "SELECT name FROM wallets"):
        assert naming.name_violates_quality_rule(row["name"]) is None, row["name"]


def test_evm_addresses_collapse_into_one_lowercase_row(tmp_db):
    seed_universe(tmp_db)
    naming.name_wallets(tmp_db)
    rows = fetch_all(tmp_db, "SELECT address, first_seen_ms, last_seen_ms, meta_json FROM wallets WHERE chain='robinhood'")
    assert [r["address"] for r in rows] == [EVM_MIXED.lower()]
    observed = jload(rows[0]["meta_json"], {})["naming"]["observed"]
    assert observed["buys"] == 3 and observed["sells"] == 1, "both spellings accumulate into one wallet"
    assert observed["sources"] == {"robinhood": 4}
    assert rows[0]["first_seen_ms"] == 50_000 and rows[0]["last_seen_ms"] == 60_000


def test_rerun_over_unchanged_evidence_writes_nothing(tmp_db):
    seed_universe(tmp_db)
    naming.name_wallets(tmp_db)
    snapshot = fetch_all(tmp_db, "SELECT * FROM wallets ORDER BY chain, address")

    again = naming.name_wallets(tmp_db)

    assert again.inserted == 0 and again.updated == 0
    assert again.unchanged == 5 == again.considered
    assert fetch_all(tmp_db, "SELECT * FROM wallets ORDER BY chain, address") == snapshot


def test_entity_membership_is_reflected_and_removed_with_the_graph(tmp_db):
    seed_swaps(tmp_db, "sol", SOL_A, buys=2, sells=1)
    seed_swaps(tmp_db, "sol", SOL_B, buys=2, sells=1)
    seed_entity(tmp_db, "sol", "sol:ent:0123456789abcdef", [SOL_A, SOL_B])
    naming.name_wallets(tmp_db)
    assert wallet_row(tmp_db, "sol", SOL_A)["name"].endswith("(entity 012345, 2 members)")

    tmp_db.execute("DELETE FROM entity_members WHERE address=?", (SOL_A,))
    report = naming.name_wallets(tmp_db)

    assert report.updated == 1 and report.unchanged == 1
    assert "entity" not in wallet_row(tmp_db, "sol", SOL_A)["name"]
    assert jload(wallet_row(tmp_db, "sol", SOL_A)["meta_json"], {})["naming"]["entity_id"] is None
    assert "(entity 012345, 2 members)" in wallet_row(tmp_db, "sol", SOL_B)["name"]


def test_new_evidence_changes_the_name_and_only_the_name_columns(tmp_db):
    seed_swaps(tmp_db, "sol", SOL_A, buys=2, sells=1)
    naming.name_wallets(tmp_db)
    before = wallet_row(tmp_db, "sol", SOL_A)
    assert before["name"].startswith("unknown#")

    seed_gmgn_event(tmp_db, "sol", SOL_A, ["kol"], feed="kol")
    seed_score(tmp_db, "sol", SOL_A, "QUARANTINED", "bot")
    report = naming.name_wallets(tmp_db)

    after = wallet_row(tmp_db, "sol", SOL_A)
    assert report.updated == 1
    assert after["name"].startswith("bot#") and after["name"].endswith("[gmgn:kol] QUARANTINED")
    assert after["source"] == before["source"] == "pumpfun:trades", "source is where we first saw it"
    assert after["first_seen_ms"] == before["first_seen_ms"]


def test_operator_row_is_merged_not_clobbered(tmp_db):
    tmp_db.execute(
        "INSERT INTO wallets (chain, address, name, source, tags_json, first_seen_ms, last_seen_ms, cohort, "
        "twitter, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("sol", SOL_A, '  my   "whale"  from the 2024 list, do not lose  ', "gmgn_export", jdump(["kol"]),
         500, 600, "blacklist", "@whale", jdump({"operator_label": "my whale", "label_basis": "operator_label"})),
    )
    seed_swaps(tmp_db, "sol", SOL_A, buys=3, sells=2, start_ms=1_000)
    seed_gmgn_event(tmp_db, "sol", SOL_A, ["smart_degen"])

    report = naming.name_wallets(tmp_db)
    assert report.inserted == 0 and report.updated == 1

    row = wallet_row(tmp_db, "sol", SOL_A)
    assert row["name"].endswith(' "my \'whale\' from the 2024 list, do not"'), row["name"]
    assert row["name"].startswith("kol#") is False, "the operator's own tag is not treated as our finding"
    assert row["name"].startswith("smart_degen#") and "[gmgn:smart_degen]" in row["name"]
    assert row["cohort"] == "blacklist" and row["source"] == "gmgn_export" and row["twitter"] == "@whale"
    assert row["first_seen_ms"] == 500 and row["last_seen_ms"] == 5_000
    tags = jload(row["tags_json"], [])
    assert "kol" in tags, "the operator's own bare tag survives"
    assert "gmgn:smart_degen" in tags
    assert "smart_degen" not in tags and "smart_money" not in tags
    meta = jload(row["meta_json"], {})
    assert meta["operator_label"] == "my whale" and meta["label_basis"] == "operator_label"
    assert meta["naming"]["prior_name"] == "my 'whale' from the 2024 list, do not"

    # A second run must not mistake its own output for an operator label.
    snapshot = dict(row)
    again = naming.name_wallets(tmp_db)
    assert again.updated == 0 and dict(wallet_row(tmp_db, "sol", SOL_A)) == snapshot


def test_meta_never_plants_keys_the_grader_reads_as_provider_claims(tmp_db):
    seed_universe(tmp_db)
    naming.name_wallets(tmp_db)
    forbidden = set(grade._PROVIDER_FIELDS) | {
        "twitter", "followers", "verified", "kol", "created_token_count", "seed_confluence",
        "sample_capped", "trade_count_lifetime", "tokens_30d", "lead_lag_of", "fomo_flag",
    }
    for row in fetch_all(tmp_db, "SELECT address, meta_json FROM wallets"):
        meta = jload(row["meta_json"], {})
        assert set(meta) == {"naming"}, row["address"]
        assert not (set(meta) & forbidden)


def test_grader_sees_our_roles_and_only_the_disqualifying_vendor_labels(tmp_db):
    seed_universe(tmp_db)
    naming.name_wallets(tmp_db)
    evidence_a = grade.build_evidence(SOL_A, Chain.SOL, tmp_db)
    assert WalletTag.SNIPER in evidence_a.tags, "our launch-window measurement reaches the grader"
    assert WalletTag.SMART_MONEY not in evidence_a.tags, "GMGN's smart_degen does not"
    assert not any(f.name == "reputation" for f in grade.components_for(evidence_a)[0])
    assert evidence_a.created_token_count == 0, "created_tokens stayed inside meta.naming"
    evidence_c = grade.build_evidence(SOL_C, Chain.SOL, tmp_db)
    assert WalletTag.WASH_TRADER in evidence_c.tags, "a label that can only hurt is admitted"
    assert grade.score_wallet(evidence_c).grade is Grade.QUARANTINED


# Every cohort label GMGN is known to send, plus every WalletTag spelling, so a new alias
# for "smart" cannot slip past by being spelled like a tag the money path already reads.
_EVERY_VENDOR_LABEL = sorted(set(naming.GMGN_COHORT_ORDER) | {t.value for t in WalletTag} | {"brand_new_cohort"})
_MONEY_PATH_WORDS = (
    {t.value for t in lanes.SMART_TAGS}
    | {t.value for t in grade.POSITIVE_REPUTATION_TAGS}
    | tracker.LANE_SMART_TAGS
)


@pytest.mark.parametrize("label", _EVERY_VENDOR_LABEL)
def test_a_wallet_known_only_by_a_gmgn_label_never_carries_a_money_path_tag(tmp_db, label):
    """The vendor-label pin. Re-enabling the v1 label->WalletTag mapping, or writing the
    label bare, fails this for the label concerned."""
    seed_gmgn_event(tmp_db, "sol", SOL_A, [label, "axiom"], tx=f"t-{label}")
    report = naming.name_wallets(tmp_db)
    assert report.inserted == 1

    row = wallet_row(tmp_db, "sol", SOL_A)
    tags = set(jload(row["tags_json"], []))
    assert tags == {f"gmgn:{label}"}, tags
    assert not (tags & _MONEY_PATH_WORDS)
    assert not (tags & {t.value for t in WalletTag}), "a vendor label is never spelled as a WalletTag"
    assert jload(row["meta_json"], {})["naming"]["gmgn_tags"] == {label: 1}, "the raw label is kept in meta"
    # The two consumers, read the way they read it.
    assert tracker.lane_smart_wallets(Chain.SOL, tmp_db) == set()
    evidence = grade.build_evidence(SOL_A, Chain.SOL, tmp_db)
    assert not (set(evidence.tags) & set(grade.POSITIVE_REPUTATION_TAGS))
    assert not (set(evidence.tags) & lanes.SMART_TAGS)
    assert all(t in grade.VENDOR_ADMITTED_TAGS for t in evidence.tags)
    # The name still says what GMGN said, attributed.
    if label in naming.GMGN_COHORT_ORDER or label == "brand_new_cohort":
        assert f"[gmgn:{label}]" in row["name"]
    assert naming.name_violates_quality_rule(row["name"]) is None

    # Idempotent and dry-run safe under the namespace, for every label.
    again = naming.name_wallets(tmp_db)
    assert again.updated == 0 and again.unchanged == 1
    dry = naming.name_wallets(tmp_db, dry_run=True)
    assert dry.updated == 0 and dry.inserted == 0
    assert set(jload(wallet_row(tmp_db, "sol", SOL_A)["tags_json"], [])) == tags


def test_vendor_namespace_never_collides_with_the_lane_or_grader_vocabulary():
    assert not any(w.startswith(naming.GMGN_TAG_PREFIX) for w in _MONEY_PATH_WORDS)
    assert naming.GMGN_TAG_PREFIX == grade.VENDOR_TAG_PREFIX
    assert tracker.LANE_SMART_TAGS == {t.value for t in lanes.SMART_TAGS}
    # The only bare words naming ever writes are its own measurements.
    ours = {t.value for t in naming.BUNDLE_ROLE_TAG.values()} | {naming.SELL_ONLY_TAG}
    assert not (ours & _MONEY_PATH_WORDS)


def test_dry_run_computes_everything_and_writes_nothing(tmp_db):
    seed_universe(tmp_db)
    report = naming.name_wallets(tmp_db, dry_run=True)
    assert report.dry_run is True and report.inserted == 5 and report.wallets_after == 5
    assert report.by_archetype["sniper"] == 1 and report.samples
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM wallets")["n"] == 0


def test_chain_filter_names_only_that_chain(tmp_db):
    seed_universe(tmp_db)
    report = naming.name_wallets(tmp_db, Chain.ROBINHOOD)
    assert report.considered == 1 and report.by_chain == {"robinhood": 1}
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM wallets")["n"] == 1


def test_rows_on_an_unknown_chain_are_skipped_and_counted(tmp_db):
    seed_swaps(tmp_db, "notachain", SOL_A, buys=2, sells=1)
    seed_swaps(tmp_db, "sol", SOL_B, buys=2, sells=1)
    report = naming.name_wallets(tmp_db)
    assert report.considered == 1 and report.skipped_unknown_chain == 2, "both swaps queries saw the row"


def test_cli_dry_run_is_read_only_and_a_real_run_writes(tmp_db, tmp_path, capsys):
    seed_universe(tmp_db)
    db_path = str(tmp_path / "kaiba.db")

    assert naming.main(["--db", db_path, "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["inserted"] == 5 and out["wallets_before"] == 0
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM wallets")["n"] == 0

    assert naming.main(["--db", db_path, "--chain", "sol"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is False and out["wallets_after"] == 4
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM wallets")["n"] == 4


# ================================================================ reading it back


def test_wallet_name_and_resolve_handle_round_trip(tmp_db):
    seed_universe(tmp_db)
    naming.name_wallets(tmp_db)
    name = naming.wallet_name(tmp_db, Chain.SOL, SOL_A)
    assert name and name.startswith("sniper#")
    assert naming.wallet_name(tmp_db, Chain.SOL, "11111111111111111111111111111111") is None

    handle = name.split(" ", 1)[0]
    found = naming.resolve_handle(tmp_db, handle)
    assert [r["address"] for r in found] == [SOL_A]
    assert found[0]["name"] == name and "gmgn:smart_degen" in found[0]["tags"]
    assert naming.resolve_handle(tmp_db, handle.split("#")[1]) == found
    assert naming.resolve_handle(tmp_db, "not a handle; DROP TABLE wallets") == []
    assert naming.resolve_handle(tmp_db, "%") == [], "a LIKE wildcard must not match every row"
    assert naming.resolve_handle(tmp_db, "ffffff") == []


def test_archetype_counts_read_back_what_was_written(tmp_db):
    seed_universe(tmp_db)
    naming.name_wallets(tmp_db)
    assert naming.archetype_counts(tmp_db) == {"sell-only": 1, "sniper": 1, "unknown": 2, "wash": 1}
    assert naming.archetype_counts(tmp_db, Chain.ROBINHOOD) == {"unknown": 1}


def test_mcp_wallet_tools_return_the_registry_name(tmp_db, monkeypatch):
    server = pytest.importorskip("kaiba.mcp.server")
    monkeypatch.setattr(server, "_conn", lambda: tmp_db)
    seed_universe(tmp_db)
    naming.name_wallets(tmp_db)

    one = server.kaiba_wallet(SOL_A, "sol")
    assert one["found"] is True
    assert one["name"] == naming.wallet_name(tmp_db, Chain.SOL, SOL_A)
    assert "gmgn:smart_degen" in one["tags"] and one["entity_size"] == 3

    listed = server.kaiba_wallets(limit=10)["wallets"]
    names = {w["address"]: w["name"] for w in listed}
    assert names[SOL_A].startswith("sniper#") and names[SOL_B].startswith("sell-only#")
    assert listed[0]["address"] == SOL_A, "the graded wallet sorts first"
