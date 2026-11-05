"""Feeding the BSC lanes: the inputs ``sm-trenches`` actually reads, made to exist honestly.

The lane's input chain (traced read-only in ``kaiba/execution/lanes.py``) is
``swaps`` -> ``wallets.tags_json`` (or ``wallet_scores.archetype``) -> ``dossier.rug_ratio``
-> ``entity_members``. It never reads ``tracker_watchlist``. So the load-bearing test here
is the first one: the lane is silent on a tape of three GMGN smart-money buyers until the
cohort seed has run, and fires on the *same* tape afterwards. Everything else guards the
way the seed gets there — the failure-rate gate stays, the source is a label and not a
grade, a re-run changes nothing, one chain's pass touches one chain — and the honest
reporting of the gate the seed cannot satisfy on its own (entity collapse).

The rug-ratio gate changed on 2026-09-21. MEASURED on the live box: ``rug_ratio`` is not on
GMGN token security or token info at all (0/4,532 dossiers); it lives on the trenches feed
rows, 180/180 on sol and 8/180 on bsc (all 0, 172 null). "Block on unknown" was therefore a
permanent off switch on the one chain the lane exists for. Now only a MEASURED ratio at or
over the ceiling refuses; an UNAVAILABLE one neither refuses nor earns strength, and the rug
defence is the dossier blockers ``engine.decide`` refuses before any size (section 8).
"""

from __future__ import annotations

import json
import types
from decimal import Decimal

import pytest

from kaiba.core.config import load_risk, save_risk
from kaiba.core.db import fetch_all, fetch_one, jload
from kaiba.core.events import emit
from kaiba.core.schemas import (
    Action,
    Chain,
    EventKind,
    EvidenceBasis,
    Grade,
    Lane,
    LaneMode,
    Measure,
    Receipt,
    TokenDossier,
    TokenRisk,
    WalletTag,
    now_ms,
)
from kaiba.execution import engine, lanes
from kaiba.execution.lanes import LaneContext
from kaiba.intelligence import discover, dyor, tracker
from kaiba.intelligence import grade as grade_mod
from kaiba.intelligence.naming import vendor_tag

BSC = Chain.BSC
RH = Chain.ROBINHOOD
SOL = Chain.SOL
SMART = "gmgn:smartmoney"
KOL = "gmgn:kol"
NOW = now_ms()
HOUR = 3_600_000
#: 0.15 BNB in wei: base units, as the schema says and as ``lanes._net_buyers`` requires.
WEI = "150000000000000000"


def _evm(i: int) -> str:
    return "0x" + f"{i:040x}"


def _sol(i: int) -> str:
    return "So" + "ABCDEFGHJK"[i] + "A" * 40


def _tok(i: int) -> str:
    return "0x" + f"{0xF000 + i:040x}"


# --------------------------------------------------------------------------------------
# fixtures: the GMGN feeds as gmgn_feeds.write_swap stores them (swaps + wallet.trade event)
# --------------------------------------------------------------------------------------


def _feed_trade(
    conn,
    chain: Chain,
    wallet: str,
    token: str,
    side: str,
    ts: int,
    *,
    usd: str = "120",
    native: str = WEI,
    source: str = SMART,
    tags: tuple[str, ...] = ("smart_degen", "gmgn"),
    name: str | None = None,
    event: bool = True,
) -> None:
    tx = f"tx-{wallet[-6:]}-{token[-4:]}-{side}-{ts}"
    conn.execute(
        "INSERT OR IGNORE INTO swaps (chain, tx, slot, ts_ms, wallet, token, side, amount_token, "
        " amount_native, price_usd, usd_value, program, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (chain.value, tx, None, ts, wallet, token, side, "1000000", native, "0.0001", usd, None, source),
    )
    if event:
        emit(
            EventKind.WALLET_TRADE,
            {
                "chain": chain.value, "tx": tx, "ts_ms": ts, "wallet": wallet, "token": token,
                "side": side, "usd_value": usd, "source": source, "feed": source.split(":", 1)[1],
                "wallet_name": name, "tags": list(tags),
            },
            chain=chain,
            subject=wallet,
            conn=conn,
        )


def _feed_wallet(
    conn,
    chain: Chain,
    wallet: str,
    *,
    buys: int = 8,
    sells: int = 6,
    source: str = SMART,
    tags: tuple[str, ...] = ("smart_degen", "gmgn"),
    name: str | None = None,
    event: bool = True,
    start: int = NOW - 6 * HOUR,
) -> None:
    """A wallet with enough feed history to be shape-checked (>= 10 swaps, buys > 10%)."""
    ts = start
    for k in range(buys):
        _feed_trade(conn, chain, wallet, _tok(k), "buy", ts, source=source, tags=tags, name=name, event=event)
        ts += 60_000
    for k in range(sells):
        _feed_trade(conn, chain, wallet, _tok(k), "sell", ts, source=source, tags=tags, name=name, event=event)
        ts += 60_000


def _score(conn, chain: Chain, address: str, grade: Grade, score: float = 30.0) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO wallet_scores (chain, address, score, grade, evidence_weight, "
        " archetype, model_version, scored_at_ms) VALUES (?,?,?,?,?,?,?,?)",
        (chain.value, address, score, grade.value, 50.0, "trader", grade_mod.MODEL_ID, NOW),
    )


def _measured(rate: float, *, seen: int = 500, failed: int | None = None):
    """A stand-in for the Helius signature page: a measured rate, any chain, no network."""

    def _fake(chain, address, conn=None, *, config=tracker.DEFAULT_CONFIG):
        return (
            rate,
            {"signatures_seen": seen, "signatures_failed": failed if failed is not None else int(rate * seen),
             "last_activity_ms": NOW},
            Receipt(provider="helius", endpoint="tx.getTransactionsForAddress"),
        )

    return _fake


def _ctx(conn, chain: Chain, token: str, *, rug_ratio: str | None, at_ms: int = NOW) -> LaneContext:
    """The context the scanner would build: ``SELECT * FROM swaps`` for the token, plus a dossier."""
    buys = fetch_all(
        conn, "SELECT * FROM swaps WHERE chain=? AND token=? ORDER BY ts_ms, id", (chain.value, token)
    )
    measure = (
        Measure.unknown()
        if rug_ratio is None
        else Measure(
            value=Decimal(rug_ratio),
            basis=EvidenceBasis.PROVIDER_REPORTED,
            receipt=Receipt(provider="fixture", endpoint="dossier", observed_at_ms=at_ms),
            freshness_budget_s=86_400,
        )
    )
    # A real sm-trenches signal always carries a liquidity reading; a fixture without one
    # was never realistic, and since 2026-09-23 the lane refuses on it. These tests are
    # about rug_ratio, tags and tax, so the book is set comfortably above the floor.
    dossier = TokenDossier(
        # sm-trenches ships a holder floor of 200 (config/risk.yaml) and an UNKNOWN
        # count refuses, so without this every case here would refuse at the floor
        # instead of reaching the blocker handling this file is about. The floor has
        # its own tests in tests/test_holder_floor.py.
        holder_count=Measure(
            value=Decimal("500"),
            basis=EvidenceBasis.PROVIDER_REPORTED,
            receipt=Receipt(provider="fixture", endpoint="dossier", observed_at_ms=at_ms),
            freshness_budget_s=86_400,
        ),
        address=token, chain=chain, rug_ratio=measure, grade=Grade.B, built_at_ms=at_ms,
        liquidity_usd=Measure(
            value=Decimal("50000"),
            basis=EvidenceBasis.PROVIDER_REPORTED,
            receipt=Receipt(provider="fixture", endpoint="dossier", observed_at_ms=at_ms),
            freshness_budget_s=86_400,
        ),
    )
    # A launchpad, because since 2026-09-23 a token without one is a MANUAL deploy and
    # needs `manual_min_smart_degen` (5) rather than `min_smart_degen` (3). These tests
    # seed three smart buyers and are about rug_ratio, tags and tax -- not about deploy
    # provenance -- so they say where the token came from.
    from kaiba.core.schemas import Token

    meta = Token(address=token, chain=chain, launchpad="flap")
    return LaneContext(chain=chain, token=token, now_ms=at_ms, conn=conn, dossier=dossier,
                       recent_buys=buys, token_meta=meta)


def _wallet_tags(conn, chain: Chain, address: str) -> list[str]:
    row = fetch_one(conn, "SELECT tags_json FROM wallets WHERE chain=? AND address=?", (chain.value, address))
    return [str(t) for t in (jload(row["tags_json"], []) if row else [])]


def _three_smart_buyers(conn, chain: Chain, token: str, *, native: str = WEI) -> list[str]:
    """Three smart-labelled wallets whose histories never overlap inside a window, then
    one token all three net-buy inside 300 s."""
    smart = [_evm(1), _evm(2), _evm(3)]
    for i, w in enumerate(smart):
        _feed_wallet(conn, chain, w, start=NOW - (12 + 3 * i) * HOUR)
    for i, w in enumerate(smart):
        _feed_trade(conn, chain, w, token, "buy", NOW - (200 - i * 50) * 1000, usd="300", native=native)
    conn.commit()
    return smart


# --------------------------------------------------------------------------------------
# 1. the input chain, end to end: the lane reads wallets.tags_json, and the seed fills it
# --------------------------------------------------------------------------------------


def test_the_lane_is_fed_by_wallets_tags_json_and_the_seed_fills_it(tmp_db):
    conn = tmp_db
    token = _tok(99)
    smart = _three_smart_buyers(conn, BSC, token)

    # Before: the tape is there, the labels are in the events, and the lane is silent,
    # because nothing has told it these wallets are smart.
    assert fetch_one(conn, "SELECT COUNT(*) AS n FROM wallets")["n"] == 0
    assert lanes.sm_trenches(_ctx(conn, BSC, token, rug_ratio="0.12")) is None

    report = tracker.seed_from_cohorts(BSC, conn)
    assert sorted(report.admitted) == sorted(smart)
    assert report.watchlist_before["active"] == 0 and report.watchlist_after["active"] == 3
    for w in smart:
        assert WalletTag.SMART_MONEY.value in _wallet_tags(conn, BSC, w)
        # the vendor's own label, kept as a fact under naming's namespace and never bare
        assert vendor_tag("smart_degen") in _wallet_tags(conn, BSC, w)
        assert "smart_degen" not in _wallet_tags(conn, BSC, w)

    # After: the same tape, the same dossier, and the lane fires on exactly those wallets.
    signal = lanes.sm_trenches(_ctx(conn, BSC, token, rug_ratio="0.12"))
    assert signal is not None and signal.lane is Lane.SM_TRENCHES
    assert sorted(signal.wallets) == sorted(smart)
    # No entity rows on bsc, so the lane counted addresses. The report below says so too.
    assert signal.payload["entity_count"] == 3

    # An unavailable rug ratio no longer keeps the lane silent (MEASURED 2026-09-21: GMGN
    # fills it on 8/180 bsc feed rows and 0/4,532 dossiers). It fires, says so, and earns
    # nothing for it; the rug defence is the dossier blockers engine.decide refuses
    # (section 8). A MEASURED ratio at the ceiling still refuses.
    unknown = lanes.sm_trenches(_ctx(conn, BSC, token, rug_ratio=None))
    assert unknown is not None and unknown.payload["rug_ratio"] is None
    assert unknown.strength == signal.strength  # unknown earns nothing; 0.12 earned nothing
    assert lanes.sm_trenches(_ctx(conn, BSC, token, rug_ratio="0.3")) is None


def test_the_watchlist_alone_would_not_have_fed_the_lane(tmp_db):
    """Documenting the chain: a watchlist row without the tag write leaves the lane silent."""
    conn = tmp_db
    token = _tok(98)
    smart = _three_smart_buyers(conn, BSC, token)
    tracker.seed_from_cohorts(BSC, conn, write_wallet_tags=False)
    assert len(tracker.watched_addresses(BSC, conn)) == 3
    assert all(w in tracker.watched_addresses(BSC, conn) for w in smart)
    assert lanes.sm_trenches(_ctx(conn, BSC, token, rug_ratio="0.12")) is None
    assert tracker.lane_smart_wallets(BSC, conn) == set()


# --------------------------------------------------------------------------------------
# 2. the failure-rate gate is kept
# --------------------------------------------------------------------------------------


def test_a_measured_failure_rate_over_the_ceiling_refuses_a_cohort_wallet(tmp_db, monkeypatch):
    conn = tmp_db
    w = _evm(7)
    _feed_wallet(conn, BSC, w)
    conn.commit()
    monkeypatch.setattr(tracker, "measure_failure_rate", _measured(0.37, seen=300))

    report = tracker.seed_from_cohorts(BSC, conn)
    assert report.admitted == []
    assert report.refused[0]["address"] == w
    assert report.refused[0]["codes"] == ["failure_rate_over_ceiling"]
    assert report.refusal_counts == {"failure_rate_over_ceiling": 1}
    assert tracker.get_entry(BSC, w, conn) is None
    assert WalletTag.SMART_MONEY.value not in _wallet_tags(conn, BSC, w)
    assert [r["action"] for r in tracker.audit_trail(BSC, w, conn)] == ["refused"]
    assert "failure rate" in tracker.audit_trail(BSC, w, conn)[0]["detail"]


def test_unmeasurable_failure_rate_admits_at_the_lower_tier_and_says_so(tmp_db):
    conn = tmp_db
    w = _evm(8)
    _feed_wallet(conn, BSC, w)
    conn.commit()

    report = tracker.seed_from_cohorts(BSC, conn)  # no monkeypatch: bsc has no signature source
    entry = tracker.get_entry(BSC, w, conn)
    assert entry is not None and entry.tier is tracker.DEFAULT_CONFIG.cohort_tier_unmeasured
    assert entry.tier is tracker.Tier.OBSERVE
    assert entry.screen["tx_failure_rate"] is None  # None, never 0
    assert entry.screen["tx_failure_basis"] == EvidenceBasis.UNAVAILABLE.value
    assert "unmeasurable" in entry.reason and "lower tier" in entry.reason
    assert any(x.startswith("failure_rate_unmeasurable") for x in entry.meta["waived"])
    assert report.failure_rate_unmeasurable == 1 and report.failure_rate_measured == 0
    assert report.admitted_by_tier == {"observe": 1}
    assert report.waived_counts["failure_rate_unmeasurable"] == 1
    assert any("not skipped" in n for n in report.notes)


def test_a_measured_clean_failure_rate_takes_the_higher_tier(tmp_db, monkeypatch):
    conn = tmp_db
    w = _evm(9)
    _feed_wallet(conn, BSC, w)
    conn.commit()
    monkeypatch.setattr(tracker, "measure_failure_rate", _measured(0.02))
    report = tracker.seed_from_cohorts(BSC, conn)
    entry = tracker.get_entry(BSC, w, conn)
    assert entry.tier is tracker.Tier.CANDIDATE
    assert entry.screen["tx_failure_rate"] == 0.02
    assert report.failure_rate_measured == 1
    assert report.credits_spent == 0  # nothing is paid for off Solana


def test_the_credit_budget_bounds_a_solana_pass_and_the_rest_enter_lower(tmp_db, monkeypatch):
    conn = tmp_db
    a, b = _sol(0), _sol(1)
    _feed_wallet(conn, SOL, a)
    _feed_wallet(conn, SOL, b)
    conn.commit()
    monkeypatch.setattr(tracker, "measure_failure_rate", _measured(0.02))
    report = tracker.seed_from_cohorts(SOL, conn, max_credits=tracker.COHORT_SCREEN_CREDITS_SOL)
    assert report.credits_spent == tracker.COHORT_SCREEN_CREDITS_SOL
    assert report.failure_rate_measured == 1 and report.failure_rate_unmeasurable == 1
    tiers = {tracker.get_entry(SOL, x, conn).tier for x in (a, b)}
    assert tiers == {tracker.Tier.CANDIDATE, tracker.Tier.OBSERVE}
    lower = next(e for e in tracker.watchlist(SOL, conn) if e.tier is tracker.Tier.OBSERVE)
    assert "credit budget" in lower.reason
    assert any(x.startswith("failure_rate_disabled") for x in lower.meta["waived"])


# --------------------------------------------------------------------------------------
# 3. the source is the cohort label, never a grade
# --------------------------------------------------------------------------------------


def test_source_is_the_cohort_label_and_never_a_grade(tmp_db):
    conn = tmp_db
    w = _evm(10)
    _feed_wallet(conn, BSC, w, name="whale.bnb")
    conn.commit()
    tracker.seed_from_cohorts(BSC, conn)
    entry = tracker.get_entry(BSC, w, conn)
    assert entry.source == f"{tracker.COHORT_SOURCE_PREFIX}{discover.COHORT_LABEL_SMART}"
    assert entry.source == "gmgn:cohort:smart_degen"
    assert not entry.source.startswith("grade:")
    assert entry.grade_at_add is None and entry.score_at_add is None
    assert entry.policy == tracker.ADMISSION_POLICY_COHORT
    assert "not a grade" in entry.reason and "no published method" in entry.reason
    assert any(x.startswith("grade_missing") for x in entry.meta["waived"])
    assert fetch_one(conn, "SELECT COUNT(*) AS n FROM wallet_scores WHERE chain=?", (BSC.value,))["n"] == 0
    # The vendor's name is kept as a vendor fact in meta, not written over wallets.name.
    row = fetch_one(conn, "SELECT name, meta_json FROM wallets WHERE chain=? AND address=?", (BSC.value, w))
    assert row["name"] is None and jload(row["meta_json"], {})["gmgn"]["name"] == "whale.bnb"


def test_our_own_measured_bad_grade_is_not_waived(tmp_db):
    conn = tmp_db
    w = _evm(11)
    _feed_wallet(conn, BSC, w)
    _score(conn, BSC, w, Grade.D)
    conn.commit()
    report = tracker.seed_from_cohorts(BSC, conn)
    assert report.admitted == []
    assert report.refused[0]["codes"] == ["grade_below_floor"]
    assert WalletTag.SMART_MONEY.value not in _wallet_tags(conn, BSC, w)


def test_a_hard_quarantine_label_from_the_vendor_refuses(tmp_db):
    conn = tmp_db
    w = _evm(12)
    _feed_wallet(conn, BSC, w, tags=("smart_degen", "wash_trader"))
    conn.commit()
    report = tracker.seed_from_cohorts(BSC, conn)
    assert report.refused[0]["codes"] == ["quarantine_tags"]
    tags = _wallet_tags(conn, BSC, w)
    assert vendor_tag("wash_trader") in tags and "wash_trader" not in tags
    assert WalletTag.SMART_MONEY.value not in tags


def test_kol_only_wallets_are_watched_but_never_become_smart_money(tmp_db):
    conn = tmp_db
    w = _evm(13)
    _feed_wallet(conn, BSC, w, source=KOL, tags=("kol", "gmgn"))
    conn.commit()
    report = tracker.seed_from_cohorts(BSC, conn)
    assert report.admitted == [w] and report.kol_cohort == 1 and report.smart_cohort == 0
    entry = tracker.get_entry(BSC, w, conn)
    assert entry.source == "gmgn:cohort:kol"
    tags = _wallet_tags(conn, BSC, w)
    # ``kol`` is a POSITIVE_REPUTATION_TAG: it may only ever appear namespaced from a feed
    assert vendor_tag("kol") in tags and "kol" not in tags
    assert WalletTag.SMART_MONEY.value not in tags
    assert tracker.lane_smart_wallets(BSC, conn) == set()
    assert report.smart_tags_written == 0


def test_a_feed_row_without_any_label_is_not_smart(tmp_db):
    conn = tmp_db
    w = _evm(14)
    _feed_wallet(conn, BSC, w, event=False)  # swaps say smartmoney feed; no event carried tags
    conn.commit()
    (cw,) = discover.cohort_wallets(BSC, conn)
    assert cw.gmgn_tags == [] and cw.tags_basis is EvidenceBasis.UNAVAILABLE
    assert not cw.smart_cohort and cw.cohort_label == discover.COHORT_LABEL_FEED_ONLY
    report = tracker.seed_from_cohorts(BSC, conn)
    assert report.feed_only == 1 and report.tags_unavailable == 1
    assert tracker.get_entry(BSC, w, conn).source == "gmgn:cohort:feed_only"
    assert WalletTag.SMART_MONEY.value not in _wallet_tags(conn, BSC, w)


# --------------------------------------------------------------------------------------
# 4. idempotent, per chain, dry run
# --------------------------------------------------------------------------------------


def test_seeding_twice_changes_nothing(tmp_db):
    conn = tmp_db
    w = _evm(15)
    _feed_wallet(conn, BSC, w)
    conn.commit()
    first = tracker.seed_from_cohorts(BSC, conn)
    before = fetch_one(conn, "SELECT * FROM tracker_watchlist WHERE chain=? AND address=?", (BSC.value, w))
    second = tracker.seed_from_cohorts(BSC, conn)
    after = fetch_one(conn, "SELECT * FROM tracker_watchlist WHERE chain=? AND address=?", (BSC.value, w))

    assert first.admitted == [w] and second.admitted == [] and second.already_watched == [w]
    for col in ("added_ms", "tier", "source", "reason", "added_by", "status"):
        assert before[col] == after[col], col
    assert [r["action"] for r in tracker.audit_trail(BSC, w, conn)] == ["admitted", "rescreened"]
    assert _wallet_tags(conn, BSC, w).count(WalletTag.SMART_MONEY.value) == 1
    assert fetch_one(conn, "SELECT COUNT(*) AS n FROM wallets")["n"] == 1
    assert fetch_one(conn, "SELECT COUNT(*) AS n FROM tracker_watchlist")["n"] == 1
    assert first.watchlist_after == second.watchlist_after


def test_a_rerun_only_retiers_upward_on_new_evidence(tmp_db, monkeypatch):
    conn = tmp_db
    w = _evm(16)
    _feed_wallet(conn, BSC, w)
    conn.commit()
    tracker.seed_from_cohorts(BSC, conn)
    assert tracker.get_entry(BSC, w, conn).tier is tracker.Tier.OBSERVE
    monkeypatch.setattr(tracker, "measure_failure_rate", _measured(0.03))
    report = tracker.seed_from_cohorts(BSC, conn)
    assert report.retiered == [w]
    assert tracker.get_entry(BSC, w, conn).tier is tracker.Tier.CANDIDATE
    assert "retiered" in [r["action"] for r in tracker.audit_trail(BSC, w, conn)]


def test_a_watched_cohort_wallet_that_now_fails_the_gate_is_removed(tmp_db, monkeypatch):
    conn = tmp_db
    w = _evm(17)
    _feed_wallet(conn, BSC, w)
    conn.commit()
    tracker.seed_from_cohorts(BSC, conn)
    assert WalletTag.SMART_MONEY.value in _wallet_tags(conn, BSC, w)
    monkeypatch.setattr(tracker, "measure_failure_rate", _measured(0.49))
    report = tracker.seed_from_cohorts(BSC, conn)
    assert report.removed[0]["address"] == w
    assert tracker.get_entry(BSC, w, conn).status == "removed"
    assert WalletTag.SMART_MONEY.value not in _wallet_tags(conn, BSC, w)  # the lane loses it too
    assert vendor_tag("smart_degen") in _wallet_tags(conn, BSC, w)  # the vendor's label stays a fact
    assert report.smart_tags_stripped == 1


def test_seeding_is_per_chain(tmp_db):
    conn = tmp_db
    b, r, s = _evm(20), _evm(21), _sol(2)
    _feed_wallet(conn, BSC, b)
    _feed_wallet(conn, RH, r)
    _feed_wallet(conn, SOL, s)
    conn.commit()

    bsc = tracker.seed_from_cohorts(BSC, conn)
    assert bsc.considered == 1 and bsc.admitted == [b]
    assert tracker.watched_addresses(BSC, conn) == {b}
    assert tracker.watched_addresses(RH, conn) == set()
    assert tracker.watched_addresses(SOL, conn) == set()
    assert {row["chain"] for row in fetch_all(conn, "SELECT chain FROM wallets")} == {BSC.value}

    rh = tracker.seed_from_cohorts(RH, conn)
    assert rh.considered == 1 and rh.admitted == [r]
    assert tracker.watched_addresses(RH, conn) == {r}
    assert tracker.watched_addresses(SOL, conn) == set()
    assert tracker.get_entry(BSC, b, conn).added_ms == tracker.get_entry(BSC, b, conn).added_ms
    assert discover.cohort_wallets(SOL, conn)[0].address == s


def test_a_dry_run_decides_but_writes_nothing(tmp_db):
    conn = tmp_db
    w, bad = _evm(22), _evm(23)
    _feed_wallet(conn, BSC, w)
    _feed_wallet(conn, BSC, bad, tags=("smart_degen", "sandwich_bot"))
    conn.commit()
    report = tracker.seed_from_cohorts(BSC, conn, dry_run=True)
    assert report.dry_run and report.admitted == [w]
    assert report.refused[0]["address"] == bad and report.refused[0]["codes"] == ["quarantine_tags"]
    for table in ("tracker_watchlist", "tracker_watchlist_log", "wallets"):
        assert fetch_one(conn, f"SELECT COUNT(*) AS n FROM {table}")["n"] == 0, table
    assert report.watchlist_after == report.watchlist_before


# --------------------------------------------------------------------------------------
# 5. the rest of the tracker honours the policy: rescreen, poll
# --------------------------------------------------------------------------------------


def test_rescreen_applies_the_policy_each_wallet_entered_under(tmp_db, monkeypatch):
    conn = tmp_db
    std, coh = _sol(3), _sol(4)
    _feed_wallet(conn, SOL, std, event=False, source="test")
    _score(conn, SOL, std, Grade.C)
    _feed_wallet(conn, SOL, coh)
    conn.commit()
    monkeypatch.setattr(tracker, "measure_failure_rate", _measured(0.02))
    assert tracker.admit(SOL, std, reason="graded C", source="grade:test", added_by="t", conn=conn)
    assert tracker.seed_from_cohorts(SOL, conn).admitted == [coh]
    assert tracker.get_entry(SOL, std, conn).policy == tracker.ADMISSION_POLICY_STANDARD
    assert tracker.get_entry(SOL, coh, conn).policy == tracker.ADMISSION_POLICY_COHORT

    # The standard wallet loses its grade: standard policy evicts, cohort policy does not
    # evict an ungraded wallet because it never entered on a grade.
    conn.execute("DELETE FROM wallet_scores WHERE chain=? AND address=?", (SOL.value, std))
    result = tracker.rescreen(SOL, conn)
    assert [e["address"] for e in result["evicted"]] == [std]
    assert result["kept"] == [coh]

    # But a *measured* failure rate over the ceiling evicts the cohort wallet as anyone.
    monkeypatch.setattr(tracker, "measure_failure_rate", _measured(0.40))
    result = tracker.rescreen(SOL, conn)
    assert [e["address"] for e in result["evicted"]] == [coh]
    assert "failure rate" in result["evicted"][0]["why"]
    assert tracker.get_entry(SOL, coh, conn).status == "removed"


def test_rescreen_moves_a_cohort_wallet_up_once_its_failure_rate_is_measured(tmp_db, monkeypatch):
    """The bounded daily rescreen is where a Solana cohort wallet's rate gets paid for."""
    conn = tmp_db
    w = _sol(8)
    _feed_wallet(conn, SOL, w)
    conn.commit()
    tracker.seed_from_cohorts(SOL, conn, check_failure_rate=False)
    assert tracker.get_entry(SOL, w, conn).tier is tracker.Tier.OBSERVE
    monkeypatch.setattr(tracker, "measure_failure_rate", _measured(0.04))
    result = tracker.rescreen(SOL, conn)
    assert result["kept"] == [w] and result["retiered"] == [w] and result["evicted"] == []
    assert tracker.get_entry(SOL, w, conn).tier is tracker.Tier.CANDIDATE
    # a later unmeasurable answer never demotes it

    def _unmeasured(chain, address, conn=None, *, config=tracker.DEFAULT_CONFIG):
        return None, {"reason": "helius down"}, Receipt(
            provider="helius", endpoint="x", basis=EvidenceBasis.UNAVAILABLE, note="helius down"
        )

    monkeypatch.setattr(tracker, "measure_failure_rate", _unmeasured)
    result = tracker.rescreen(SOL, conn)
    assert result["kept"] == [w] and result["retiered"] == []
    assert tracker.get_entry(SOL, w, conn).tier is tracker.Tier.CANDIDATE


def test_rescreen_is_bounded_and_rotates_least_recently_checked_first(tmp_db):
    conn = tmp_db
    wallets = [_evm(30), _evm(31), _evm(32)]
    for w in wallets:
        _feed_wallet(conn, BSC, w)
    conn.commit()
    tracker.seed_from_cohorts(BSC, conn)
    first = tracker.rescreen(BSC, conn, limit=2)
    assert first["screened"] == 2 and first["skipped_for_budget"] == 1 and first["credits"] == 0
    left = (set(wallets) - set(first["kept"])).pop()
    second = tracker.rescreen(BSC, conn, limit=2)
    assert left in second["kept"]
    assert second["kept"][0] == left  # oldest check goes first
    # The default bound is the config's, and it is labelled.
    assert tracker.DEFAULT_CONFIG.rescreen_max_wallets == 50
    unbounded = tracker.rescreen(BSC, conn)
    assert unbounded["screened"] == 3 and unbounded["skipped_for_budget"] == 0


def test_cohort_wallets_are_not_polled_on_the_paid_route(tmp_db, monkeypatch):
    conn = tmp_db
    std, coh = _sol(5), _sol(6)
    _feed_wallet(conn, SOL, std, event=False, source="test")
    _score(conn, SOL, std, Grade.C)
    _feed_wallet(conn, SOL, coh)
    conn.commit()
    monkeypatch.setattr(tracker, "measure_failure_rate", _measured(0.02))
    tracker.admit(SOL, std, reason="graded C", source="grade:test", added_by="t", conn=conn)
    tracker.seed_from_cohorts(SOL, conn)

    assert tracker.watched_addresses(SOL, conn) == {std, coh}
    assert tracker.pollable_addresses(SOL, conn) == {std}
    assert tracker.get_entry(SOL, coh, conn).meta["poll_route"] == tracker.ROUTE_GMGN_FEED
    assert tracker.get_entry(SOL, coh, conn).helius_polled is False

    polled: list[str] = []

    def _fake_poll(chain, address, conn=None, *, config=tracker.DEFAULT_CONFIG, baseline_only=False):
        polled.append(address)
        return tracker.PollResult(tracker.ROUTE_HELIUS, address, True, 1, tracker.HELIUS_POLL_CREDITS, 0)

    monkeypatch.setattr(tracker, "poll_wallet", _fake_poll)
    results = tracker.poll_wallets(SOL, conn)
    assert polled == [std] and len(results) == 1


def test_legacy_rows_without_the_flag_stay_pollable(tmp_db):
    conn = tmp_db
    conn.execute(
        "INSERT INTO tracker_watchlist (chain, address, tier, status, reason, source, added_by, added_ms) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (SOL.value, _sol(7), "observe", "active", "old row", "grade:old", "t", NOW),
    )
    conn.commit()
    assert tracker.pollable_addresses(SOL, conn) == {_sol(7)}
    assert tracker.get_entry(SOL, _sol(7), conn).policy == tracker.ADMISSION_POLICY_STANDARD


# --------------------------------------------------------------------------------------
# 6. the gate the seed cannot satisfy (entities) and the one that no longer blocks on
#    unknown (rug ratio), reported honestly
# --------------------------------------------------------------------------------------


def _link(conn, chain: Chain, entity_id: str, members: list[str]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO entities (entity_id, chain, confidence, size, created_ms, updated_ms) "
        "VALUES (?,?,?,?,?,?)",
        (entity_id, chain.value, 0.9, len(members), NOW, NOW),
    )
    for m in members:
        conn.execute(
            "INSERT OR REPLACE INTO entity_members (entity_id, chain, address) VALUES (?,?,?)",
            (entity_id, chain.value, m),
        )


def test_with_no_entities_on_the_chain_the_entity_gate_counts_addresses(tmp_db):
    conn = tmp_db
    token = _tok(97)
    smart = _three_smart_buyers(conn, BSC, token)
    tracker.seed_from_cohorts(BSC, conn)

    report = tracker.trenches_input_report(BSC, conn, since_ms=0)
    assert report["smart_wallets_available"] == 3
    assert report["entity_collapse_available"] is False and report["entities_on_chain"] == 0
    assert report["tokens_with_enough_smart_buyers"] == 1
    assert report["tokens_qualifying_on_entities"] == 1
    (q,) = report["qualifying_tokens"]
    assert q["token"] == token and q["smart_net_buyers"] == 3 and q["entities"] == 3
    assert q["entities_are_addresses"] is True
    assert any("counting addresses" in b for b in report["blockers"])

    # Two of the three behind one operator: still two entities, still qualifies.
    _link(conn, BSC, "bsc:ent:one", smart[:2])
    conn.commit()
    report = tracker.trenches_input_report(BSC, conn, since_ms=0)
    assert report["entity_collapse_available"] is True
    (q,) = report["qualifying_tokens"]
    assert q["entities"] == 2 and q["entities_are_addresses"] is False

    # All three behind one operator: one opinion, the lane must not count it.
    _link(conn, BSC, "bsc:ent:all", smart)
    conn.commit()
    report = tracker.trenches_input_report(BSC, conn, since_ms=0)
    assert report["tokens_with_enough_smart_buyers"] == 1
    assert report["tokens_qualifying_on_entities"] == 0
    assert any("independent entities" in b for b in report["blockers"])
    assert lanes.sm_trenches(_ctx(conn, BSC, token, rug_ratio="0.12")) is None


def test_the_report_no_longer_names_an_unknown_rug_ratio_as_a_blocker(tmp_db):
    conn = tmp_db
    token = _tok(96)
    _three_smart_buyers(conn, BSC, token)
    tracker.seed_from_cohorts(BSC, conn)

    # No dossier stored at all: counted, noted with the real defences, not a blocker.
    report = tracker.trenches_input_report(BSC, conn, since_ms=0)
    assert report["no_dossier_on_qualifying"] == 1
    assert report["tokens_lane_eligible"] == 1 and report["lane_can_fire_now"] is True
    assert not any("rug_ratio" in b for b in report["blockers"])
    (note,) = [n for n in report["notes"] if n.startswith("rug_ratio unavailable")]
    assert "not a lane blocker" in note and "QUARANTINED" in note and "engine.decide" in note
    (q,) = report["qualifying_tokens"]
    assert q["rug_ratio_known"] is None and q["rug_ratio"] is None and q["rug_refused"] is False

    # A stored dossier that says UNAVAILABLE: the same.
    conn.execute(
        "INSERT INTO token_dossiers (chain, address, built_at_ms, grade, dossier_json) VALUES (?,?,?,?,?)",
        (BSC.value, token, NOW, "D", '{"rug_ratio": {"value": null, "basis": "unavailable"}}'),
    )
    conn.commit()
    report = tracker.trenches_input_report(BSC, conn, since_ms=0)
    assert report["rug_ratio_unknown_on_qualifying"] == 1 and report["lane_can_fire_now"] is True
    assert not any("rug_ratio" in b for b in report["blockers"])

    # MEASURED under the ceiling: eligible, and the value is on the row.
    conn.execute(
        "UPDATE token_dossiers SET dossier_json=? WHERE chain=? AND address=?",
        ('{"rug_ratio": {"value": "0.12", "basis": "provider_reported"}}', BSC.value, token),
    )
    conn.commit()
    report = tracker.trenches_input_report(BSC, conn, since_ms=0)
    assert report["rug_ratio_known_on_qualifying"] == 1 and report["lane_can_fire_now"] is True
    assert not any("rug_ratio" in b for b in report["blockers"])
    (q,) = report["qualifying_tokens"]
    assert q["rug_ratio"] == "0.12" and q["rug_refused"] is False

    # MEASURED at the ceiling: the one rug reading that does refuse, and the report says so
    # in the same terms the lane uses. The ceiling it applied is on the report.
    conn.execute(
        "UPDATE token_dossiers SET dossier_json=? WHERE chain=? AND address=?",
        ('{"rug_ratio": {"value": "0.3", "basis": "provider_reported"}}', BSC.value, token),
    )
    conn.commit()
    report = tracker.trenches_input_report(BSC, conn, since_ms=0)
    assert report["max_rug_ratio"] == "0.3"
    assert report["rug_ratio_over_max_on_qualifying"] == 1
    assert report["tokens_lane_eligible"] == 0 and report["lane_can_fire_now"] is False
    assert any("MEASURED rug_ratio >= 0.3" in b for b in report["blockers"])
    (q,) = report["qualifying_tokens"]
    assert q["rug_ratio"] == "0.3" and q["rug_refused"] is True
    assert lanes.sm_trenches(_ctx(conn, BSC, token, rug_ratio="0.3")) is None


def _store_dossier(conn, chain: Chain, token: str, rug_ratio: dict) -> None:
    conn.execute("DELETE FROM token_dossiers WHERE chain=? AND address=?", (chain.value, token))
    conn.execute(
        "INSERT INTO token_dossiers (chain, address, built_at_ms, grade, dossier_json) VALUES (?,?,?,?,?)",
        (chain.value, token, NOW, "D", json.dumps({"rug_ratio": rug_ratio})),
    )
    conn.commit()


def _rug_json(value: str, *, age_s: int | None = None, basis: str = "provider_reported") -> dict:
    """The ``rug_ratio`` Measure as ``dossier.model_dump_json`` stores it: the lane's 900 s
    budget and, when ``age_s`` is given, a receipt that old by the wall clock."""
    body: dict = {"value": value, "basis": basis, "freshness_budget_s": dyor.RUG_RATIO_BUDGET_S}
    if age_s is not None:
        body["receipt"] = {
            "provider": "gmgn", "endpoint": "feed.trenches", "observed_at_ms": now_ms() - age_s * 1000,
        }
    return body


def _ctx_with_rug(conn, chain: Chain, token: str, value: str, *, age_s: int) -> LaneContext:
    """``_ctx`` with the rug Measure as dyor builds it: the lane's 900 s budget and a
    receipt ``age_s`` old by the wall clock ``Measure.stale`` reads."""
    ctx = _ctx(conn, chain, token, rug_ratio=None)
    measure = Measure(
        value=Decimal(value),
        basis=EvidenceBasis.PROVIDER_REPORTED,
        receipt=Receipt(provider="gmgn", endpoint="feed.trenches", observed_at_ms=now_ms() - age_s * 1000),
        freshness_budget_s=dyor.RUG_RATIO_BUDGET_S,
    )
    return ctx.model_copy(update={"dossier": ctx.dossier.model_copy(update={"rug_ratio": measure})})


def test_the_report_reads_an_evm_zero_rug_ratio_as_unavailable_like_the_reader(tmp_db):
    """MEASURED 2026-09-21: every rug_ratio GMGN filled on an EVM row was exactly 0 (bsc
    8/180, robinhood 8/180, base 54/180) and one token moved 0 -> 0.682 within minutes, so
    an EVM 0 is "not scored yet". The feed reader returns UNAVAILABLE for it; a stored
    dossier holding the 0 must read the same way here, or the report would count a
    placeholder as coverage. It is not a blocker either way. A sol 0 is a real score."""
    conn = tmp_db
    token = _tok(93)
    _three_smart_buyers(conn, BSC, token)
    tracker.seed_from_cohorts(BSC, conn)
    _store_dossier(conn, BSC, token, _rug_json("0"))
    assert tracker._rug_ratio_known(conn, BSC, token) is False

    report = tracker.trenches_input_report(BSC, conn, since_ms=0)
    assert report["rug_ratio_known_on_qualifying"] == 0
    assert report["rug_ratio_unknown_on_qualifying"] == 1
    assert report["rug_ratio_evm_zero_on_qualifying"] == 1
    assert report["tokens_lane_eligible"] == 1 and report["lane_can_fire_now"] is True
    assert not any("rug_ratio" in b for b in report["blockers"])
    (q,) = report["qualifying_tokens"]
    assert q["rug_ratio_known"] is False and q["rug_ratio"] is None
    assert q["rug_ratio_basis"] == "unavailable" and q["rug_ratio_stored"] == "0"
    assert q["rug_refused"] is False
    (note,) = [n for n in report["notes"] if "placeholder" in n]
    assert "EVM" in note and "UNAVAILABLE" in note and "not a blocker" in note

    # The same 0 on sol is a score: known, measured, on the row.
    sol_token = _tok(92)
    sol_smart = [_sol(0), _sol(1), _sol(2)]
    for i, w in enumerate(sol_smart):
        _feed_trade(conn, SOL, w, sol_token, "buy", NOW - (200 - i * 50) * 1000, usd="300", native="150000000")
    _store_dossier(conn, SOL, sol_token, _rug_json("0"))
    assert tracker._rug_ratio_known(conn, SOL, sol_token) is True
    report = tracker.trenches_input_report(SOL, conn, since_ms=0, smart_wallets=set(sol_smart))
    assert report["rug_ratio_known_on_qualifying"] == 1
    assert report["rug_ratio_evm_zero_on_qualifying"] == 0
    assert report["lane_can_fire_now"] is True
    (q,) = report["qualifying_tokens"]
    assert q["token"] == sol_token and q["rug_ratio"] == "0" and q["rug_ratio_basis"] == "measured"


def test_lane_can_fire_now_needs_no_known_rug_ratio_and_staleness_is_read_like_the_lane(tmp_db):
    """``lane_can_fire_now`` is the entity gate plus a readable ceiling; a known rug ratio
    is not required because the lane does not require one. A stored value past its budget
    is read the way ``lanes.sm_trenches`` reads it: under the ceiling it is unavailable
    (basis "stale", no number), at or over it it refuses -- and the lane agrees on the same
    tape at the same ages."""
    conn = tmp_db
    token = _tok(91)
    _three_smart_buyers(conn, BSC, token)
    tracker.seed_from_cohorts(BSC, conn)

    report = tracker.trenches_input_report(BSC, conn, since_ms=0)
    assert report["rug_ratio_known_on_qualifying"] == 0 and report["no_dossier_on_qualifying"] == 1
    assert report["tokens_lane_eligible"] == 1 and report["lane_can_fire_now"] is True

    # Stale under the ceiling: still eligible, and the row says stale rather than measured.
    _store_dossier(conn, BSC, token, _rug_json("0.12", age_s=901))
    assert tracker._rug_ratio_known(conn, BSC, token) is True
    report = tracker.trenches_input_report(BSC, conn, since_ms=0)
    assert report["rug_ratio_known_on_qualifying"] == 1 and report["rug_ratio_stale_on_qualifying"] == 1
    assert report["tokens_lane_eligible"] == 1 and report["lane_can_fire_now"] is True
    (q,) = report["qualifying_tokens"]
    assert q["rug_ratio_basis"] == "stale" and q["rug_ratio"] is None and q["rug_ratio_stored"] == "0.12"
    assert any("stale" in n and "budget" in n for n in report["notes"])
    stale_signal = lanes.sm_trenches(_ctx_with_rug(conn, BSC, token, "0.12", age_s=901))
    assert stale_signal is not None and stale_signal.payload["rug_ratio_basis"] == "stale"

    # Fresh under the ceiling: measured, number on the row.
    _store_dossier(conn, BSC, token, _rug_json("0.12", age_s=0))
    report = tracker.trenches_input_report(BSC, conn, since_ms=0)
    assert report["rug_ratio_stale_on_qualifying"] == 0
    (q,) = report["qualifying_tokens"]
    assert q["rug_ratio_basis"] == "measured" and q["rug_ratio"] == "0.12"
    fresh_signal = lanes.sm_trenches(_ctx_with_rug(conn, BSC, token, "0.12", age_s=0))
    assert fresh_signal is not None and fresh_signal.payload["rug_ratio_basis"] == "measured"

    # Stale at or over the ceiling: a stale bad number is still a bad number.
    _store_dossier(conn, BSC, token, _rug_json("0.9", age_s=901))
    report = tracker.trenches_input_report(BSC, conn, since_ms=0)
    assert report["rug_ratio_over_max_on_qualifying"] == 1
    assert report["tokens_lane_eligible"] == 0 and report["lane_can_fire_now"] is False
    (q,) = report["qualifying_tokens"]
    assert q["rug_ratio_basis"] == "refused" and q["rug_ratio"] == "0.9" and q["rug_refused"] is True
    assert any("MEASURED rug_ratio >= 0.3" in b and "stale" in b for b in report["blockers"])
    assert lanes.sm_trenches(_ctx_with_rug(conn, BSC, token, "0.9", age_s=901)) is None


def test_ui_unit_and_null_amount_native_are_counted_but_do_not_block_the_lane(tmp_db):
    """The feed stored "0.147380625" in a base-units column on 49,347 of 49,405 rows
    (MEASURED 2026-09-21), and the backfill nulls those. ``lanes._net_buyers`` reads both a
    dotted value and NULL as UNAVAILABLE and runs the net-buyer test on ``usd_value``, so
    neither fails the pass; the report counts them, says which is which, and does not call
    either a blocker. The lane firing on each tape is the proof, not the report's word."""
    conn = tmp_db
    dotted = _tok(95)
    _three_smart_buyers(conn, BSC, dotted, native="0.147380625")
    tracker.seed_from_cohorts(BSC, conn)
    assert lanes.sm_trenches(_ctx(conn, BSC, dotted, rug_ratio=None)) is not None
    report = tracker.trenches_input_report(BSC, conn, since_ms=0)
    assert report["amount_native_not_base_units"] == 3
    assert report["amount_native_unavailable"] == 0
    assert report["tokens_lane_eligible"] == 1 and report["lane_can_fire_now"] is True
    assert not any("amount_native" in b for b in report["blockers"])
    assert any("UI-unit" in n and "backfill-amount-native" in n for n in report["notes"])

    # What the backfill leaves behind: NULL, which is UNAVAILABLE and not a blocker either.
    nulled = _tok(94)
    for i, w in enumerate([_evm(1), _evm(2), _evm(3)]):
        _feed_trade(conn, BSC, w, nulled, "buy", NOW - (200 - i * 50) * 1000, usd="300", native=None)
    conn.commit()
    assert lanes.sm_trenches(_ctx(conn, BSC, nulled, rug_ratio=None)) is not None
    report = tracker.trenches_input_report(BSC, conn, since_ms=0)
    assert report["amount_native_unavailable"] == 3
    assert report["tokens_lane_eligible"] == 2 and report["lane_can_fire_now"] is True
    assert not any("amount_native" in b for b in report["blockers"])
    assert any("NULL (UNAVAILABLE" in n for n in report["notes"])


def test_no_smart_wallet_means_the_first_gate_is_the_blocker(tmp_db):
    report = tracker.trenches_input_report(BSC, tmp_db, since_ms=0)
    assert report["smart_wallets_available"] == 0 and report["lane_can_fire_now"] is False
    assert report["blockers"] and "min_smart_degen" in report["blockers"][0]


# --------------------------------------------------------------------------------------
# 7. gathering: what discover.cohort_wallets says and does not say
# --------------------------------------------------------------------------------------


def test_cohort_wallets_gathers_labels_from_events_and_reports_floors(tmp_db):
    conn = tmp_db
    a, b = _evm(50), _evm(51)
    _feed_wallet(conn, BSC, a, buys=5, sells=2, tags=("smart_degen", "gmgn"))
    _feed_trade(conn, BSC, a, _tok(70), "buy", NOW - HOUR, source=KOL, tags=("kol", "arbitrager"))
    _feed_wallet(conn, BSC, b, buys=2, sells=1, tags=("fomo",))
    conn.commit()

    wallets = {w.address: w for w in discover.cohort_wallets(BSC, conn)}
    assert set(wallets) == {a, b}
    wa = wallets[a]
    assert wa.sources == [KOL, SMART] and wa.feed_rows == 8 and wa.buys == 6 and wa.sells == 2
    assert wa.distinct_tokens == 6 and wa.tokens_basis is EvidenceBasis.PROVIDER_REPORTED
    assert set(wa.gmgn_tags) == {"smart_degen", "gmgn", "kol", "arbitrager"}
    assert wa.tags_basis is EvidenceBasis.PROVIDER_REPORTED
    assert wa.smart_cohort and wa.kol_cohort and wa.cohort_label == "smart_degen"
    assert wallets[b].cohort_label == discover.COHORT_LABEL_FEED_ONLY

    summary = discover.cohort_summary(list(wallets.values()))
    assert summary == {
        "wallets": 2, "by_source": {KOL: 1, SMART: 2}, "smart_cohort": 1, "kol_cohort": 1,
        "feed_only": 1, "tags_unavailable": 0, "feed_rows": 11,
    }
    # ranking is by feed rows, and limit cuts after ranking
    assert [w.address for w in discover.cohort_wallets(BSC, conn, limit=1)] == [a]
    assert discover.cohort_wallets(BSC, conn, sources=[KOL]) [0].feed_rows == 1
    assert discover.cohort_wallets(SOL, conn) == []


def test_cohort_wallets_ignores_rows_from_other_sources_and_other_chains(tmp_db):
    conn = tmp_db
    _feed_wallet(conn, BSC, _evm(60), source="pumpfun:trades", event=False)
    _feed_wallet(conn, RH, _evm(61))
    conn.commit()
    assert discover.cohort_wallets(BSC, conn) == []
    assert [w.address for w in discover.cohort_wallets(RH, conn)] == [_evm(61)]


# --------------------------------------------------------------------------------------
# 8. provenance and drift guards
# --------------------------------------------------------------------------------------


def test_every_cohort_number_carries_its_provenance():
    allowed = {"MEASURED", "CITED", "INVENTED", "DERIVED"}
    for key, (label, why) in tracker.COHORT_PROVENANCE.items():
        assert label in allowed, key
        assert len(why) > 40, key
    assert tracker.COHORT_PROVENANCE["cohort_tier_measured"][0] == "INVENTED"
    assert tracker.COHORT_PROVENANCE["cohort_tier_unmeasured"][0] == "INVENTED"
    assert "INVENTED" in tracker.TrackerConfig.__doc__
    # and the config's values are the ones the provenance table describes
    assert tracker.DEFAULT_CONFIG.cohort_tier_measured is tracker.Tier.CANDIDATE
    assert tracker.DEFAULT_CONFIG.cohort_tier_unmeasured is tracker.Tier.OBSERVE
    assert "50 wallets" in tracker.COHORT_PROVENANCE["rescreen_max_wallets"][1]


def test_the_tracker_reads_the_same_smart_tags_the_lane_does():
    assert tracker.LANE_SMART_TAGS == {t.value for t in lanes.SMART_TAGS}
    assert tracker.LANE_SMART_ARCHETYPES == {"smart_money", "top_trader"}
    # GMGN's own labels are not lane tags; only the explicit mapping bridges them.
    assert not (discover.GMGN_SMART_COHORT_TAGS & tracker.LANE_SMART_TAGS)
    assert tracker.Tier.__members__.keys() == {"OBSERVE", "CANDIDATE"}  # still no trust tier


def test_config_reads_the_lanes_actual_trenches_keys(monkeypatch):
    class _Risk:
        def lane(self, lane):
            params = {"min_smart_degen": 4, "min_independent_entities": 3, "window_s": 240}
            return types.SimpleNamespace(params=params if lane is Lane.SM_TRENCHES else {})

    monkeypatch.setattr(tracker, "get_risk", lambda: _Risk())
    cfg = tracker.config_from_risk()
    assert cfg.trenches_min_wallets == 4
    assert cfg.trenches_min_entities == 3
    assert cfg.trenches_window_s == 240
    assert cfg.cohort_tier_measured is tracker.Tier.CANDIDATE  # carried through, not dropped


def test_the_shipped_risk_file_reaches_the_report_thresholds():
    """The tracker must CARRY the lane's thresholds, whatever they are set to.

    `trenches_window_s` was asserted as a literal 300 and broke when the lane's
    `window_s` moved to 1800. The invariant is that the tracker follows the lane -- a
    literal here just re-reads the shipped file and pins nothing.
    """
    from kaiba.core.config import get_risk

    params = get_risk().lanes["sm-trenches"].params
    cfg = tracker.config_from_risk()
    assert cfg.trenches_min_wallets == int(params["min_smart_degen"])
    assert cfg.trenches_min_entities == int(params["min_independent_entities"])
    assert cfg.trenches_window_s == int(params.get("window_s", 300))


def test_verdicts_expose_what_was_enforced_and_what_was_waived(tmp_db):
    conn = tmp_db
    w = _evm(80)
    _feed_wallet(conn, BSC, w)
    conn.commit()
    screen = tracker.screen_wallet(BSC, w, conn)
    assert screen.blocker_codes == ["grade_missing", "failure_rate_unmeasurable"]
    assert len(screen.blocker_codes) == len(screen.blockers)
    std = tracker.standard_verdict(screen)
    coh = tracker.cohort_verdict(screen)
    assert std.admissible is False and std.blocker_codes == screen.blocker_codes and std.waived == []
    assert coh.admissible is True and coh.blockers == [] and len(coh.waived) == 2
    assert coh.tier is tracker.Tier.OBSERVE
    assert tracker.COHORT_WAIVABLE_CODES == {"grade_missing", "failure_rate_unmeasurable", "failure_rate_disabled"}
    assert "failure_rate_over_ceiling" not in tracker.COHORT_WAIVABLE_CODES


@pytest.mark.parametrize("code", sorted(tracker.COHORT_WAIVABLE_CODES))
def test_waivable_codes_are_only_the_absence_of_evidence(code):
    """Nothing that is a *measurement* may be waived: the codes are all 'we do not know'."""
    assert code.endswith(("_missing", "_unmeasurable", "_disabled"))


# --------------------------------------------------------------------------------------
# 8. the rug defence behind an unavailable rug ratio: engine.decide refuses the dossier's
#    blockers before any size exists, so the lane firing on an unknown rug ratio can never
#    reach a size for a token the dossier already condemned
# --------------------------------------------------------------------------------------

#: The dossier blockers the lane's reason string names as its rug defence, by the dyor rule
#: that raises each: honeypot, mint_authority_live, freeze_authority_live, dev_concentration,
#: cluster_concentration_reject, already_rugged. Any one of them grades QUARANTINED.
RUG_DEFENCES = [
    TokenRisk.HONEYPOT,
    TokenRisk.MINT_AUTHORITY,
    TokenRisk.FREEZE_AUTHORITY,
    TokenRisk.DEV_CONCENTRATION,
    TokenRisk.CLUSTER_CONCENTRATION,
    TokenRisk.RUG_HISTORY,
]


@pytest.fixture
def armed_live(tmp_path, monkeypatch):
    """sm-trenches LIVE in an isolated risk.yaml, the way config/risk.yaml ships it (mode:
    live, chains sol + bsc), so the proof covers real money and not only shadow."""
    cfg = load_risk()
    cfg.global_mode = LaneMode.LIVE
    cfg.kill_switch = False
    cfg.lanes[Lane.SM_TRENCHES].mode = LaneMode.LIVE
    path = tmp_path / "risk.yaml"
    save_risk(cfg, path)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))
    assert engine.get_risk().effective_mode(Lane.SM_TRENCHES) is LaneMode.LIVE, "could not arm"


def _stored(conn, chain: Chain, token: str, blockers: list[TokenRisk]) -> None:
    dossier = TokenDossier(
        # sm-trenches ships a holder floor of 200 (config/risk.yaml) and an UNKNOWN
        # count refuses, so without this every case here would refuse at the floor
        # instead of reaching the blocker handling this file is about. The floor has
        # its own tests in tests/test_holder_floor.py.
        holder_count=Measure(
            value=Decimal("500"),
            basis=EvidenceBasis.PROVIDER_REPORTED,
            receipt=Receipt(provider="fixture", endpoint="dossier", observed_at_ms=now_ms()),
            freshness_budget_s=86_400,
        ),
        address=token,
        chain=chain,
        rug_ratio=Measure.unknown(),
        blockers=list(blockers),
        score=0.0 if blockers else None,
        grade=Grade.QUARANTINED if blockers else Grade.B,
        built_at_ms=now_ms(),
        liquidity_usd=Measure(
            value=Decimal("50000"),
            basis=EvidenceBasis.PROVIDER_REPORTED,
            receipt=Receipt(provider="fixture", endpoint="dossier", observed_at_ms=now_ms()),
            freshness_budget_s=86_400,
        ),
    )
    assert dyor.store_dossier(dossier, conn)


def _unknown_rug_signal(conn, token: str):
    _three_smart_buyers(conn, BSC, token)
    tracker.seed_from_cohorts(BSC, conn)
    signal = lanes.sm_trenches(_ctx(conn, BSC, token, rug_ratio=None))
    assert signal is not None and signal.payload["rug_ratio"] is None
    return signal


@pytest.mark.parametrize("blocker", RUG_DEFENCES, ids=[b.value for b in RUG_DEFENCES])
def test_engine_refuses_a_condemned_dossier_before_any_size_on_an_unknown_rug_ratio(
    tmp_db, armed_live, monkeypatch, blocker
):
    conn = tmp_db
    token = _tok(80)
    signal = _unknown_rug_signal(conn, token)
    _stored(conn, BSC, token, [blocker])

    def _never(*_a, **_k):
        raise AssertionError("the sizer was reached for a dossier that carries a blocker")

    monkeypatch.setattr(engine, "_size_for", _never)
    decision = engine.decide(signal, conn)
    assert decision.action is Action.SKIP
    assert decision.blockers == [blocker.value]
    assert decision.dossier_grade is Grade.QUARANTINED
    assert decision.thesis.startswith("dossier blockers: " + blocker.value)
    row = fetch_one(
        conn, "SELECT action, blockers_json FROM decisions WHERE decision_id=?", (decision.decision_id,)
    )
    assert row["action"] == "skip" and blocker.value in row["blockers_json"]


def test_engine_lists_every_blocker_of_a_condemned_dossier(tmp_db, armed_live, monkeypatch):
    conn = tmp_db
    token = _tok(81)
    signal = _unknown_rug_signal(conn, token)
    _stored(conn, BSC, token, RUG_DEFENCES)
    monkeypatch.setattr(engine, "_size_for", lambda *_a, **_k: pytest.fail("sizer reached"))
    decision = engine.decide(signal, conn)
    assert decision.action is Action.SKIP
    assert decision.blockers == [b.value for b in RUG_DEFENCES]


def test_the_same_signal_reaches_the_sizer_once_the_dossier_is_clean(tmp_db, armed_live, monkeypatch):
    """The refusal above is the blockers and nothing else swallowing every signal: with the
    same tape, the same unknown rug ratio and a clean dossier, decide gets as far as size."""
    conn = tmp_db
    token = _tok(82)
    signal = _unknown_rug_signal(conn, token)
    _stored(conn, BSC, token, [])
    reached: list[str] = []

    def _size(sig, _conn):
        reached.append(sig.token)
        return 0

    monkeypatch.setattr(engine, "_size_for", _size)
    decision = engine.decide(signal, conn)
    assert reached == [token]
    assert not set(decision.blockers) & {b.value for b in RUG_DEFENCES}
    assert decision.dossier_grade is Grade.B
