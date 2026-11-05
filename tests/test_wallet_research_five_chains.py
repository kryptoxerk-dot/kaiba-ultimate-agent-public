"""Five-chain research evidence keeps original clocks and grants no trading authority."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from kaiba.core.schemas import Chain
from kaiba.intelligence import campaign_watchlist, wallet_campaign, wallet_candidates
from kaiba.learning import autolearn_wallets as aw
from kaiba.learning import runner_cohort
from kaiba.learning.autolearn_io import GuardedReader

NOW = 1_800_000_000_000
DAY = 86_400_000
LABELS = {"sol": "solana", "bsc": "bnb", "robinhood": "robinhood", "eth": "eth", "base": "base"}
EVM = "0x" + "ab" * 20
SOL = "A" * 32


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr("socket.socket", lambda *args, **kwargs: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: pytest.fail("provider subprocess forbidden"))


def reader(conn):
    return GuardedReader(Path(conn.execute("PRAGMA database_list").fetchone()[2]))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    os.utime(path, (NOW / 1000, NOW / 1000))


def caches(root, chain, *, at=NOW - 3_600_000, profit=1000, p30=True):
    address = SOL if chain == "sol" else EVM
    presented = address if chain == "sol" else "0x" + address[2:].upper()
    write(root / "exports" / f"kaiba-{LABELS[chain]}-hightier.txt", [{"address": presented, "name": "A fixture"}])
    if p30:
        write(root / chain / "p30.json", {address: {"_at": at // 1000, "realized_profit": profit, "buy": 20}})
    write(root / chain / "ht_stats.json", {address: {"_at": at // 1000, "rp": profit, "buy": 20,
                                                   "ret": .3, "win": .5, "hold": 600, "tokens": 12}})
    return address


def review(packet):
    source = copy.deepcopy(packet)
    source.pop("present", None)
    return wallet_candidates.review_candidates(source, now_ms=NOW)


def test_real_packet_reads_all_five_chains_and_full_profiles_with_original_clocks(tmp_db, tmp_path):
    root = tmp_path / "runners"
    original_at = NOW - 3_600_000
    for chain in LABELS:
        caches(root, chain, at=original_at)
    packet = aw._packet(reader(tmp_db), root, NOW, {})
    assert {x["chain"] for x in packet["exports"]} == set(LABELS)
    assert len(packet["profiles"]) == 10
    for chain in LABELS:
        profiles = [x for x in packet["profiles"] if x["chain"] == chain]
        assert {x["source"] for x in profiles} == {"p30", "ht_stats"}
        assert all(x["data"]["_at"] * 1000 == original_at for x in profiles)
    result = review(packet)
    assert result["coverage"]["by_chain"] == dict.fromkeys(LABELS, 1)
    assert result["coverage"]["provider_positive_count"] == 5
    assert all(x["at_ms"] == original_at for c in result["candidates"] for x in c["profiles"])
    assert result["funding_allowed"] is result["live_copy_approved"] is False
    assert result["copy_qualified_count"] == 0
    assert result["queue_policy"]["provider_budget_increase"] == 0
    assert all(q["provider_calls_authorized"] == 0 and q["roster_change_authorized"] is False
               for q in result["observation_queue"])


@pytest.mark.parametrize("chain", LABELS)
def test_recent_export_cannot_refresh_stale_full_profile_on_any_chain(tmp_db, tmp_path, chain):
    root = tmp_path / "runners"
    caches(root, chain, at=NOW - 2 * DAY, p30=False)
    result = review(aw._packet(reader(tmp_db), root, NOW, {}))
    candidate = result["candidates"][0]
    assert candidate["profiles"][0]["at_ms"] == NOW - 2 * DAY
    assert "provider_observation_stale_missing_or_future" in candidate["profiles"][0]["reasons"]
    assert "PROVIDER_POSITIVE" not in candidate["roles"]
    assert candidate["copy_status"] == "COPY_UNPROVEN"


def test_same_evm_address_on_eth_and_base_keeps_distinct_profit_evidence(tmp_db, tmp_path):
    root = tmp_path / "runners"
    caches(root, "eth", profit=1000)
    caches(root, "base", profit=-1000)
    result = review(aw._packet(reader(tmp_db), root, NOW, {}))
    candidates = {x["chain"]: x for x in result["candidates"]}
    assert set(candidates) == {"eth", "base"}
    assert candidates["eth"]["address"] == candidates["base"]["address"] == EVM
    assert candidates["eth"]["candidate_key"] != candidates["base"]["candidate_key"]
    assert candidates["eth"]["roles"] == ["PROVIDER_POSITIVE"]
    assert candidates["base"]["roles"] == []
    assert result["coverage"]["provider_positive_count"] == 1


@pytest.mark.parametrize("chain", LABELS)
def test_registry_owner_is_preserved_and_excluded_on_every_chain(tmp_db, tmp_path, chain):
    root = tmp_path / "runners"
    address = caches(root, chain)
    tmp_db.execute("INSERT INTO wallets(chain,address,name,source,first_seen_ms,last_seen_ms,cohort,meta_json) "
                   "VALUES(?,?,?,?,?,?,?,?)", (chain, address, "Owner fixture", "owner", 123, 456,
                                             "research", '{"is_owner":true,"keep":1}'))
    tmp_db.commit()
    before = tuple(tmp_db.execute("SELECT * FROM wallets").fetchone())
    result = aw.run_wallet_review(reader(tmp_db), root, now_ms=NOW)
    assert tuple(tmp_db.execute("SELECT * FROM wallets").fetchone()) == before
    assert result["intake"]["added"] == []
    assert result["review"]["observation_queue"] == []
    assert result["review"]["candidates"][0]["exclusions"]
    assert tmp_db.execute("SELECT COUNT(*) FROM wallet_scores").fetchone()[0] == 0


def test_wildcard_exclusion_covers_new_chains_without_changing_other_addresses(tmp_db, tmp_path):
    root = tmp_path / "runners"
    for chain in LABELS:
        caches(root, chain)
    packet = aw._packet(reader(tmp_db), root, NOW, {})
    packet["exclusions"].append({"address": EVM, "reason": "explicit_owner_exclusion"})
    result = review(packet)
    assert result["coverage"]["excluded_count"] == 4
    assert [x["chain"] for x in result["observation_queue"]] == ["sol"]
    assert {x["chain"] for x in result["candidates"] if x["exclusions"]} == {"bsc", "robinhood", "eth", "base"}


@pytest.mark.parametrize("chain", ("eth", "base"))
def test_new_chain_intake_uses_existing_research_only_quota_and_identity(tmp_db, tmp_path, chain):
    root = tmp_path / "runners"
    address = caches(root, chain)
    result = aw.run_wallet_review(reader(tmp_db), root, now_ms=NOW)
    assert result["intake"]["added"] == [{"chain": chain, "address": address, "cohort": "research"}]
    assert result["intake"]["provider_calls"] == 0
    assert result["intake"]["daily_count"] == 1
    stored = tmp_db.execute("SELECT chain,address,name,source,cohort FROM wallets").fetchone()
    assert tuple(stored) == (chain, address, None, "codex-autolearn-research", "research")
    again = aw.run_wallet_review(reader(tmp_db), root, now_ms=NOW, prior_state=result["next_state"])
    assert again["intake"]["added"] == []
    assert tuple(tmp_db.execute("SELECT chain,address,name,source,cohort FROM wallets").fetchone()) == tuple(stored)
    assert tmp_db.execute("SELECT COUNT(*) FROM tracker_watchlist").fetchone()[0] == 0


def test_legacy_campaign_and_runner_trial_remain_three_chains():
    assert wallet_campaign.CHAINS == ("bsc", "sol", "robinhood")
    assert campaign_watchlist.CHAINS == wallet_campaign.CHAINS
    assert set(runner_cohort.EXPORT_CHAINS.values()) == {Chain.SOL, Chain.BSC, Chain.ROBINHOOD}
    def row(chain):
        return {"chain": chain, "address": EVM, "grade": "B", "score": 45, "evidence_weight": 40,
                "scored_at_ms": NOW - 1000, "model_version": "kaiba-wallet-gmgn-v1"}
    original = wallet_campaign.summarize([row("bsc"), row("robinhood")], [], now_ms=NOW)
    combined = wallet_campaign.summarize([row(c) for c in ("bsc", "robinhood", "eth", "base")], [], now_ms=NOW)
    assert combined.pop("invalid_identity_rows") == 2
    assert original.pop("invalid_identity_rows") == 0
    assert combined == original
    assert (aw.DAILY_INTAKE, aw.MAX_ROWS, aw.MAX_TOTAL_BYTES, aw.ACQUISITION_CAP) == (20, 2000, 48 * 1024 * 1024, 20)
