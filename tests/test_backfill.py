"""Wallet history backfill: classification, idempotency, cursors, budget and first buyers.

Offline. Every Helius response is either a recorded live payload under
``tests/fixtures/backfill/`` (captured 2026-09-20 against mainnet, trimmed but not
reshaped) or a small mutation of one, so the field names asserted here are the field names
the provider actually sends rather than the ones its documentation promises.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import Chain, EventKind
from kaiba.ingest import backfill
from kaiba.intelligence.hubs import JITO_TIP_ACCOUNTS

FIXTURES = Path(__file__).parent / "fixtures" / "backfill"

#: The wallet the recorded page belongs to.
WALLET = "6gFyhzmVVW5Stv6pPZok59i9Xox3GCctpvNujbbLeJHS"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def page() -> list[dict[str, Any]]:
    return fixture("enhanced_page")


@pytest.fixture
def raw_page() -> dict[str, Any]:
    return fixture("raw_page")


def tx_named(page: list[dict[str, Any]], prefix: str) -> dict[str, Any]:
    for item in page:
        if item["signature"].startswith(prefix):
            return item
    raise AssertionError(f"no transaction starting {prefix} in the fixture")


# --------------------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------------------


def test_sell_is_classified_from_raw_balance_changes(page):
    row, reason = backfill.classify_swap(tx_named(page, "5Mdn4VAt"), WALLET)
    assert reason is None
    assert row is not None
    assert row.side == "sell"
    # Straight out of accountData.tokenBalanceChanges.rawTokenAmount, sign dropped.
    assert row.amount_token == 24_036_973_406_001
    assert row.amount_native == 1_141_375_084
    assert row.slot == 448_382_908
    assert row.ts_ms % 1000 == 0
    assert row.venue == "RAYDIUM_LAUNCHLAB"


def test_buy_is_classified_with_the_opposite_signs(page):
    row, reason = backfill.classify_swap(tx_named(page, "2RvVg6gn"), WALLET)
    assert reason is None
    assert row is not None
    assert row.side == "buy"
    assert row.amount_token == 6_335_392_656_410
    assert row.amount_native == 337_436_389


def test_amounts_are_integers_and_usd_is_absent_not_zero(page):
    row, _ = backfill.classify_swap(tx_named(page, "5Mdn4VAt"), WALLET)
    assert isinstance(row.amount_token, int)
    assert isinstance(row.amount_native, int)
    # Helius history carries no price. Missing money is None, never a default that reads
    # as a measurement (docs/CONTRACT.md rule 2).
    assert row.price_usd is None
    assert row.usd_value is None
    params = row.as_params()
    assert isinstance(params[8], str) and isinstance(params[9], str)
    assert float not in {type(p) for p in params}


def test_a_swap_that_moves_nothing_for_us_is_not_our_trade(page):
    """Address history includes transactions that merely mention the address."""
    row, reason = backfill.classify_swap(tx_named(page, "63D2CnWw"), WALLET)
    assert row is None
    assert reason == "wallet_not_a_party"
    assert reason in backfill.MECHANICAL_SKIPS


def test_non_swap_and_failed_transactions_are_skipped(page):
    assert backfill.classify_swap(tx_named(page, "4Ss1Jq3b"), WALLET)[1] == "not_swap_type"
    assert backfill.classify_swap(tx_named(page, "5R1E6qLM"), WALLET)[1] == "not_swap_type"
    assert backfill.classify_swap(tx_named(page, "FAILEDtx"), WALLET)[1] == "failed_transaction"


def test_a_multi_leg_route_is_refused_rather_than_guessed(page):
    tx = copy.deepcopy(tx_named(page, "2RvVg6gn"))
    extra = copy.deepcopy(tx["accountData"][-1])
    for change in extra.get("tokenBalanceChanges") or []:
        change["mint"] = "SecondMint1111111111111111111111111111111111"
    # Make sure the clone really carries a second mint for this wallet.
    extra["tokenBalanceChanges"] = [
        {
            "userAccount": WALLET,
            "mint": "SecondMint1111111111111111111111111111111111",
            "rawTokenAmount": {"tokenAmount": "123456", "decimals": 6},
        }
    ]
    tx["accountData"].append(extra)
    row, reason = backfill.classify_swap(tx, WALLET)
    assert row is None
    assert reason == "multi_leg_route"
    assert reason not in backfill.MECHANICAL_SKIPS  # this one counts against the skip rate


def test_a_dust_native_leg_is_not_consideration(page):
    tx = copy.deepcopy(tx_named(page, "2RvVg6gn"))
    for entry in tx["accountData"]:
        if entry["account"] == WALLET:
            entry["nativeBalanceChange"] = -100
    tx["fee"] = 0
    tx["nativeTransfers"] = []
    assert backfill.classify_swap(tx, WALLET)[1] == "quote_leg_mismatch"


def test_a_same_direction_quote_leg_is_refused(page):
    """Token in and SOL in together is not a swap; it is somebody funding the wallet."""
    tx = copy.deepcopy(tx_named(page, "2RvVg6gn"))
    for entry in tx["accountData"]:
        if entry["account"] == WALLET:
            entry["nativeBalanceChange"] = 500_000_000
    assert backfill.classify_swap(tx, WALLET)[1] == "quote_leg_mismatch"


def test_a_stablecoin_quote_leg_gives_real_usd(page):
    tx = copy.deepcopy(tx_named(page, "2RvVg6gn"))
    for entry in tx["accountData"]:
        if entry["account"] == WALLET:
            entry["nativeBalanceChange"] = -5_000
    tx["fee"] = 5_000
    tx["accountData"].append(
        {
            "account": "UsdcAta11111111111111111111111111111111111",
            "nativeBalanceChange": 0,
            "tokenBalanceChanges": [
                {
                    "userAccount": WALLET,
                    "mint": backfill.USDC_MINT,
                    "rawTokenAmount": {"tokenAmount": "-250000000", "decimals": 6},
                }
            ],
        }
    )
    row, reason = backfill.classify_swap(tx, WALLET)
    assert reason is None
    assert row.side == "buy"
    assert row.amount_native is None  # there was no SOL leg, so do not invent one
    assert row.usd_value == Decimal("250")
    assert isinstance(row.price_usd, Decimal)


def test_the_fee_is_added_back_only_for_the_fee_payer(page):
    tx = copy.deepcopy(tx_named(page, "5Mdn4VAt"))
    paid = backfill.classify_swap(tx, WALLET)[0].amount_native
    tx["feePayer"] = "SomebodyElse111111111111111111111111111111"
    sponsored = backfill.classify_swap(tx, WALLET)[0].amount_native
    assert paid - sponsored == tx["fee"]


def test_page_parse_separates_mechanical_skips_from_real_failures(page):
    parsed = backfill.parse_page(page, WALLET)
    assert len(parsed.swaps) == 3
    assert parsed.skips["not_swap_type"] == 2
    assert parsed.skips["failed_transaction"] == 1
    assert parsed.skips["wallet_not_a_party"] == 1
    assert parsed.ambiguous == 0


# --------------------------------------------------------------------------------------
# metadata
# --------------------------------------------------------------------------------------


def test_enhanced_meta_admits_the_signer_list_is_partial(page):
    meta = backfill.enhanced_meta(tx_named(page, "5Mdn4VAt"))
    assert meta["signers"] == [WALLET]
    assert meta["signers_complete"] is False
    assert meta["alt_authority"] is None  # genuinely absent, recorded as absent
    assert meta["programs"]


def test_raw_meta_reads_signers_and_lookup_tables(raw_page):
    rows = raw_page["data"]
    parsed = dict(backfill.raw_meta(r) for r in rows)
    first_sig = rows[0]["transaction"]["signatures"][0]
    first = parsed[first_sig]
    assert first["signers_complete"] is True
    assert first["signers"] == [WALLET]
    assert first["block_index"] == 65

    with_tables = [m for m in parsed.values() if m["alt_tables"]]
    assert with_tables, "the recorded page contains lookup-table transactions"
    assert all(m["alt_authority"] is None for m in with_tables)


def test_raw_meta_reads_a_multi_signer_transaction(raw_page):
    raw = copy.deepcopy(raw_page["data"][0])
    raw["transaction"]["message"]["header"]["numRequiredSignatures"] = 2
    _, meta = backfill.raw_meta(raw)
    assert len(meta["signers"]) == 2
    assert meta["signers_complete"] is True


def test_a_complete_signer_list_is_not_demoted_by_a_later_partial_one(tmp_db):
    from kaiba.intelligence.cluster import swap_meta

    backfill.write_meta(
        tmp_db, Chain.SOL, {"sig1": {"signers": ["a", "b"], "signers_complete": True}}
    )
    backfill.write_meta(
        tmp_db, Chain.SOL, {"sig1": {"signers": ["a"], "signers_complete": False, "venue": "PUMP_FUN"}}
    )
    stored = swap_meta(tmp_db, Chain.SOL, "sig1")
    assert stored["signers"] == ["a", "b"]
    assert stored["signers_complete"] is True
    assert stored["venue"] == "PUMP_FUN"


def test_jito_tips_are_recorded_so_bundle_detection_has_an_input(tmp_db, page):
    tx = copy.deepcopy(tx_named(page, "5Mdn4VAt"))
    tx["nativeTransfers"] = [
        {"fromUserAccount": WALLET, "toUserAccount": JITO_TIP_ACCOUNTS[0], "amount": 100_000}
    ]
    parsed = backfill.parse_page([tx], WALLET)
    assert backfill.write_transfers(tmp_db, parsed.transfers) == 1
    row = fetch_one(tmp_db, "SELECT * FROM transfers WHERE dst = ?", (JITO_TIP_ACCOUNTS[0],))
    assert row["amount"] == "100000"
    assert row["slot"] == tx["slot"]


def test_swap_internal_fee_routing_is_never_written_as_a_transfer(page):
    """It is not funding, and `derive_same_funder` reads this table as if it were.

    The live 25-wallet run produced 9,190 spurious `same_funder` edges before this rule
    existed, every one of them from pump.fun fee and rent accounts.
    """
    tx = copy.deepcopy(tx_named(page, "5Mdn4VAt"))
    tx["nativeTransfers"] = [
        {"fromUserAccount": WALLET, "toUserAccount": "PumpFeeAccount1111111111", "amount": 9_131_780},
        {"fromUserAccount": "SomePool111111111", "toUserAccount": WALLET, "amount": 4_392_503},
    ]
    assert backfill.transfer_rows(tx, WALLET, Chain.SOL) == []


def test_a_real_transfer_transaction_is_recorded_as_funding_flow(page):
    tx = copy.deepcopy(tx_named(page, "4Ss1Jq3b"))  # type == TRANSFER in the recording
    tx["nativeTransfers"] = [
        {"fromUserAccount": "Funder1111111111", "toUserAccount": WALLET, "amount": 2_000_000_000},
        {"fromUserAccount": "Stranger11111111", "toUserAccount": "Other111111", "amount": 5_000},
    ]
    rows = backfill.transfer_rows(tx, WALLET, Chain.SOL)
    assert len(rows) == 1
    assert rows[0].src == "Funder1111111111"
    assert rows[0].amount == 2_000_000_000


# --------------------------------------------------------------------------------------
# storage and idempotency
# --------------------------------------------------------------------------------------


def test_writing_the_same_page_twice_writes_nothing_twice(tmp_db, page):
    parsed = backfill.parse_page(page, WALLET)
    first = backfill.write_swaps(tmp_db, parsed.swaps)
    second = backfill.write_swaps(tmp_db, parsed.swaps)
    assert first == (3, 0)
    assert second == (0, 3)
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM swaps")["n"] == 3


def test_stored_rows_match_the_migration_contract(tmp_db, page):
    parsed = backfill.parse_page(page, WALLET)
    backfill.write_swaps(tmp_db, parsed.swaps)
    row = fetch_one(tmp_db, "SELECT * FROM swaps WHERE side = 'buy'")
    assert row["source"] == backfill.SOURCE
    assert row["chain"] == "sol"
    assert row["fee_payer"] == WALLET
    assert row["is_create_tx"] == 0
    assert int(row["amount_token"]) == 6_335_392_656_410
    assert row["price_usd"] is None


# --------------------------------------------------------------------------------------
# the provider loop
# --------------------------------------------------------------------------------------


class FakeHelius:
    """Stands in for the two Helius routes, with a credit ledger the code must respect."""

    def __init__(self, pages, raw=None, *, remaining=1_000_000):
        self.pages = list(pages)
        self.raw = raw
        self.remaining = remaining
        self.used = 0
        self.enhanced_calls: list[dict[str, Any]] = []
        self.raw_calls = 0

    def install(self, monkeypatch):
        monkeypatch.setattr(backfill.helius, "get_enhanced_transactions", self.enhanced)
        monkeypatch.setattr(backfill.helius, "get_transactions_for_address", self.transactions)
        monkeypatch.setattr(backfill.helius, "budget_status", self.budget_status)
        return self

    def budget_status(self, conn=None, period=None):
        return {
            "provider": "helius",
            "period": period or "2026-09",
            "used": self.used,
            "remaining": self.remaining,
            "allowance": 1_000_000,
            "pct_used": round(100.0 * self.used / 1_000_000, 3),
            "estimated_credits": self.used,
            "resets_in_s": 86_400,
        }

    def enhanced(self, address, *, limit=100, before=None, until=None, conn=None, **kw):
        self.enhanced_calls.append({"address": address, "before": before, "until": until})
        self.used += 100
        self.remaining -= 100
        if not self.pages:
            return [], _receipt()
        return self.pages.pop(0), _receipt()

    def transactions(self, address, *, limit=100, pagination_token=None, conn=None, **kw):
        self.raw_calls += 1
        self.used += 10
        self.remaining -= 10
        return (self.raw or {"data": [], "paginationToken": None}), _receipt()


def _receipt():
    from kaiba.core.schemas import EvidenceBasis, Receipt

    return Receipt(provider="helius", endpoint="tx.enhancedHistory", basis=EvidenceBasis.PROVIDER_REPORTED)


def test_a_wallet_run_writes_swaps_meta_and_a_cursor(tmp_db, monkeypatch, page, raw_page):
    fake = FakeHelius([page], raw_page).install(monkeypatch)
    result = backfill.backfill_wallet(WALLET, Chain.SOL, tmp_db)
    assert result.swaps_written == 3
    assert result.error is None
    assert result.exhausted is True  # a short page means history ran out
    assert fake.raw_calls == 1
    assert result.meta_written >= len(raw_page["data"])
    cursor = backfill.load_cursor(tmp_db, Chain.SOL, WALLET)
    assert cursor["newest_sig"] == page[0]["signature"]
    assert cursor["exhausted"] is True


def test_a_rerun_resumes_instead_of_refetching(tmp_db, monkeypatch, page):
    full = page + page  # pretend a full page so the walk continues
    fake = FakeHelius([full[:7], full[:7]], None).install(monkeypatch)
    backfill.backfill_wallet(WALLET, Chain.SOL, tmp_db, page_limit=7, with_meta=False)
    first_cursor = backfill.load_cursor(tmp_db, Chain.SOL, WALLET)
    assert first_cursor["oldest_sig"] == page[-1]["signature"]

    backfill.backfill_wallet(WALLET, Chain.SOL, tmp_db, page_limit=7, with_meta=False)
    assert fake.enhanced_calls[0]["before"] is None
    assert fake.enhanced_calls[1]["before"] == page[-1]["signature"]
    # And the rows are still unique.
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM swaps")["n"] == 3


def test_an_exhausted_wallet_is_not_paid_for_again(tmp_db, monkeypatch, page):
    fake = FakeHelius([page], None).install(monkeypatch)
    backfill.backfill_wallet(WALLET, Chain.SOL, tmp_db, with_meta=False)
    calls = len(fake.enhanced_calls)
    again = backfill.backfill_wallet(WALLET, Chain.SOL, tmp_db, with_meta=False)
    assert len(fake.enhanced_calls) == calls
    assert again.stopped == "already_exhausted"


def test_fresh_mode_asks_only_for_newer_transactions(tmp_db, monkeypatch, page):
    fake = FakeHelius([page, page], None).install(monkeypatch)
    backfill.backfill_wallet(WALLET, Chain.SOL, tmp_db, with_meta=False)
    backfill.backfill_wallet(WALLET, Chain.SOL, tmp_db, with_meta=False, fresh=True)
    assert fake.enhanced_calls[-1]["until"] == page[0]["signature"]
    assert fake.enhanced_calls[-1]["before"] is None


def test_a_fresh_top_up_keeps_the_backward_walk_exhausted(tmp_db, monkeypatch, page):
    """``exhausted`` is about how far BACK the walk reached. A forward top-up used to
    overwrite it with its own result -- False for any non-empty page -- so a complete
    history read as truncated after its next top-up, and the grader's A gate now reads
    this flag (grade.history_truncated). MUTATION: writing ``cursor["exhausted"] =
    bool(result.exhausted)`` in fresh mode again fails the second assertion."""
    FakeHelius([page, page], None).install(monkeypatch)
    backfill.backfill_wallet(WALLET, Chain.SOL, tmp_db, with_meta=False)
    assert backfill.load_cursor(tmp_db, Chain.SOL, WALLET)["exhausted"] is True
    backfill.backfill_wallet(WALLET, Chain.SOL, tmp_db, with_meta=False, fresh=True)
    cursor = backfill.load_cursor(tmp_db, Chain.SOL, WALLET)
    assert cursor["exhausted"] is True
    assert not cursor.get("forward_gap")  # a short forward page left no hole


def test_a_full_forward_page_records_a_possible_hole(tmp_db, monkeypatch, page):
    """A forward page that comes back FULL may have skipped transactions between the old
    ``newest_sig`` and its own oldest one, and the cursor jumps past them. That is a gap
    in a history that otherwise reads complete, so it is recorded and the grader treats
    it as truncated. MUTATION: dropping the ``forward_gap`` write fails here."""
    FakeHelius([page, page], None).install(monkeypatch)
    backfill.backfill_wallet(WALLET, Chain.SOL, tmp_db, with_meta=False)
    backfill.backfill_wallet(WALLET, Chain.SOL, tmp_db, with_meta=False, fresh=True,
                             page_limit=len(page))
    cursor = backfill.load_cursor(tmp_db, Chain.SOL, WALLET)
    assert cursor["exhausted"] is True and cursor["forward_gap"] is True


def test_the_first_buyer_rebuild_touches_only_tokens_this_walk_bought(tmp_db, monkeypatch, page):
    """Brought over from the box (backfill.py there since 2026-09-27, untested): a walk
    re-ranks the buyers of the tokens it wrote BUYS for, not every buy on the chain.
    MUTATION: calling ``rebuild_first_buyers(c, chain)`` unscoped again ranks the
    unrelated token below."""
    tmp_db.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, source) "
        "VALUES ('sol', 'other-tx', 1, 'someone-else', 'UNRELATED', 'buy', '1', 'pumpfun:trades')"
    )
    FakeHelius([page], None).install(monkeypatch)
    report = backfill.backfill_wallets(tmp_db, Chain.SOL, wallets=[WALLET], with_meta=False)
    bought = {r["token"] for r in fetch_all(
        tmp_db, "SELECT DISTINCT token FROM swaps WHERE wallet = ? AND side = 'buy'", (WALLET,))}
    ranked = {r["token"] for r in fetch_all(tmp_db, "SELECT DISTINCT token FROM first_buyers")}
    assert bought and report.results[0].buy_tokens == bought
    assert ranked == bought and report.first_buyers_written >= 1


def test_an_empty_wallet_list_backfills_nobody(tmp_db, monkeypatch, page):
    """Also from the box: ``wallets=[]`` used to fall through to ``tracked_wallets`` and
    pay for the registry's least-recently-touched rows instead of nobody."""
    tmp_db.execute(
        "INSERT INTO wallets (chain, address, source, cohort, first_seen_ms, last_seen_ms) "
        "VALUES ('sol', ?, 'test', 'research', 0, 0)", (WALLET,),
    )
    fake = FakeHelius([page], None).install(monkeypatch)
    report = backfill.backfill_wallets(tmp_db, Chain.SOL, wallets=[], with_meta=False)
    assert report.wallets == 0 and fake.enhanced_calls == []


def test_an_exhausted_budget_stops_the_run_cleanly(tmp_db, monkeypatch, page):
    fake = FakeHelius([page], None, remaining=10).install(monkeypatch)
    report = backfill.backfill_wallets(
        tmp_db, Chain.SOL, wallets=[WALLET], with_meta=False
    )
    assert report.stopped == "budget_exhausted"
    assert report.swaps_written == 0
    assert fake.enhanced_calls == []


def test_the_run_credit_ceiling_is_honoured(tmp_db, monkeypatch, page):
    fake = FakeHelius([page, page, page], None).install(monkeypatch)
    report = backfill.backfill_wallets(
        tmp_db, Chain.SOL, wallets=[WALLET, "w2", "w3"], with_meta=False, max_credits=150
    )
    assert report.stopped == "run_credit_ceiling"
    assert len(fake.enhanced_calls) < 3


def test_a_provider_failure_degrades_instead_of_raising(tmp_db, monkeypatch):
    from kaiba.core.schemas import EvidenceBasis, Receipt

    def dead(address, **kw):
        return None, Receipt(
            provider="helius", endpoint="tx.enhancedHistory",
            basis=EvidenceBasis.UNAVAILABLE, note="helius is down",
        )

    monkeypatch.setattr(backfill.helius, "get_enhanced_transactions", dead)
    monkeypatch.setattr(
        backfill.helius, "budget_status",
        lambda conn=None, period=None: {"used": 0, "remaining": 1_000_000, "allowance": 1_000_000},
    )
    result = backfill.backfill_wallet(WALLET, Chain.SOL, tmp_db, with_meta=False)
    assert result.error is not None
    assert result.stopped == "provider_unavailable"
    errors = fetch_all(
        tmp_db, "SELECT * FROM events WHERE kind = ?", (EventKind.PROVIDER_ERROR.value,)
    )
    assert errors


def test_a_backfill_does_not_flood_the_bus_with_stale_trades(tmp_db, monkeypatch, page):
    FakeHelius([page], None).install(monkeypatch)
    backfill.backfill_wallets(tmp_db, Chain.SOL, wallets=[WALLET], with_meta=False)
    trades = fetch_all(
        tmp_db, "SELECT * FROM events WHERE kind = ?", (EventKind.WALLET_TRADE.value,)
    )
    summaries = fetch_all(tmp_db, "SELECT * FROM events WHERE kind = ?", (EventKind.SYSTEM.value,))
    assert trades == []
    assert len(summaries) == 1


def test_dry_run_writes_nothing(tmp_db, monkeypatch, page):
    FakeHelius([page], None).install(monkeypatch)
    report = backfill.backfill_wallets(
        tmp_db, Chain.SOL, wallets=[WALLET], with_meta=False, dry_run=True
    )
    assert report.swaps_written == 3
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM swaps")["n"] == 0
    assert backfill.load_cursor(tmp_db, Chain.SOL, WALLET) == {}


def test_blacklisted_wallets_are_never_selected(tmp_db):
    from kaiba.core.schemas import now_ms

    for address, cohort in (("aaa", "research"), ("bbb", "blacklist"), ("ccc", "trusted_copy")):
        tmp_db.execute(
            "INSERT INTO wallets (chain, address, source, first_seen_ms, last_seen_ms, cohort) "
            "VALUES ('sol',?,'test',?,?,?)",
            (address, now_ms(), now_ms(), cohort),
        )
    assert set(backfill.tracked_wallets(tmp_db, Chain.SOL, limit=10)) == {"aaa", "ccc"}
    assert backfill.tracked_wallets(tmp_db, Chain.SOL, limit=10, cohorts=["trusted_copy"]) == ["ccc"]


# --------------------------------------------------------------------------------------
# first buyers
# --------------------------------------------------------------------------------------


def _swap(conn, wallet, token, ts_ms, side="buy", slot=1, native=1_000):
    conn.execute(
        "INSERT INTO swaps (chain, tx, slot, ts_ms, wallet, token, side, amount_token, "
        "amount_native, source) VALUES ('sol',?,?,?,?,?,?,'1',?,?)",
        (f"{wallet}-{token}-{ts_ms}-{side}", slot, ts_ms, wallet, token, side, str(native), backfill.SOURCE),
    )


def test_first_buyers_are_ranked_in_observation_order(tmp_db):
    _swap(tmp_db, "w2", "tokA", 2_000)
    _swap(tmp_db, "w1", "tokA", 1_000)
    _swap(tmp_db, "w3", "tokA", 3_000)
    _swap(tmp_db, "w1", "tokA", 4_000)  # a second buy must not create a second row
    _swap(tmp_db, "w9", "tokA", 5_000, side="sell")
    assert backfill.rebuild_first_buyers(tmp_db, Chain.SOL) == 3
    rows = fetch_all(tmp_db, "SELECT * FROM first_buyers ORDER BY rank")
    assert [r["wallet"] for r in rows] == ["w1", "w2", "w3"]
    assert [r["rank"] for r in rows] == [1, 2, 3]
    assert all(r["source"].endswith(":observed") for r in rows)


def test_seconds_after_open_is_none_without_a_real_launch_time(tmp_db):
    """Deriving it from our own first sighting would manufacture insider flags."""
    _swap(tmp_db, "w1", "tokA", 1_000)
    backfill.rebuild_first_buyers(tmp_db, Chain.SOL)
    assert fetch_one(tmp_db, "SELECT * FROM first_buyers")["seconds_after_open"] is None

    tmp_db.execute(
        "INSERT INTO tokens (chain, address, created_ms, first_seen_ms) VALUES ('sol','tokA',0,0)"
    )
    backfill.rebuild_first_buyers(tmp_db, Chain.SOL)
    assert fetch_one(tmp_db, "SELECT * FROM first_buyers")["seconds_after_open"] == 1.0


def test_rebuilding_first_buyers_is_idempotent_and_reranks(tmp_db):
    _swap(tmp_db, "w2", "tokA", 2_000)
    backfill.rebuild_first_buyers(tmp_db, Chain.SOL)
    assert fetch_one(tmp_db, "SELECT rank FROM first_buyers WHERE wallet='w2'")["rank"] == 1
    _swap(tmp_db, "w1", "tokA", 1_000)
    backfill.rebuild_first_buyers(tmp_db, Chain.SOL)
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM first_buyers")["n"] == 2
    assert fetch_one(tmp_db, "SELECT rank FROM first_buyers WHERE wallet='w2'")["rank"] == 2


# --------------------------------------------------------------------------------------
# status, and the end-to-end effect on grading
# --------------------------------------------------------------------------------------


def test_status_reports_progress_and_credit_spend(tmp_db, monkeypatch, page, raw_page):
    FakeHelius([page], raw_page).install(monkeypatch)
    backfill.backfill_wallets(tmp_db, Chain.SOL, wallets=[WALLET])
    status = backfill.backfill_status(tmp_db, Chain.SOL)
    assert status["swaps"] == 3
    assert status["wallets_started"] == 1
    assert status["first_buyers"] >= 1
    assert status["swap_meta"] >= 1
    assert status["helius"]["remaining"] > 0


# --------------------------------------------------------------------------------------
# the sell-only wallet filter
#
# Lives here rather than in tests/test_grade.py because that file belongs to another
# task running against this tree at the same time, and the filter is Phase 1 work: it has
# to be in place before the first backfilled wallet is ever graded.
# --------------------------------------------------------------------------------------


def _evidence(**kw):
    from kaiba.intelligence.grade import WalletEvidence

    return WalletEvidence(address=WALLET, chain=Chain.SOL, **kw)


def test_a_sell_only_address_is_refused_before_scoring():
    """The published case: 1,793 trades, zero buys, ranked top of a PnL leaderboard."""
    from kaiba.core.schemas import Grade
    from kaiba.intelligence.grade import score_wallet, sell_only_rejection

    ev = _evidence(observed_buys=0, observed_sells=1_793)
    reason = sell_only_rejection(ev)
    assert reason is not None and "0 buys" in reason
    score = score_wallet(ev)
    assert score.grade is Grade.QUARANTINED
    assert score.score == 0.0
    assert score.evidence_weight == 0.0
    assert score.factors == []


def test_a_profitable_sell_only_address_cannot_buy_its_way_past_the_filter():
    """A large realised profit must not absorb the rejection the way a penalty would."""
    from kaiba.core.schemas import Grade
    from kaiba.intelligence.grade import ProviderStats, score_wallet
    from kaiba.intelligence.pnl import WalletPnl

    ev = _evidence(
        observed_buys=0,
        observed_sells=1_793,
        pnl=WalletPnl(
            closed_episodes=40, distinct_tokens=30, win_rate=0.8,
            realized_pnl_usd=Decimal("4000000"), roi=Decimal("3.0"), big_wins=12,
        ),
        provider_stats=ProviderStats(realized_profit_usd=Decimal("4000000"), win_rate=0.8),
    )
    assert score_wallet(ev).grade is Grade.QUARANTINED


def test_a_thin_sample_is_unscored_rather_than_quarantined():
    from kaiba.core.schemas import Grade
    from kaiba.intelligence.grade import score_wallet, sell_only_rejection

    ev = _evidence(observed_buys=0, observed_sells=3)
    assert sell_only_rejection(ev) is None
    assert score_wallet(ev).grade is Grade.UNSCORED


def test_a_buy_starved_wallet_is_penalised_not_refused():
    from kaiba.intelligence.grade import BUY_STARVED_PENALTY, penalties_for, sell_only_rejection

    ev = _evidence(observed_buys=5, observed_sells=95)  # 5% buy share
    assert sell_only_rejection(ev) is None
    hits = [p for p in penalties_for(ev) if p.name == "buy_starved"]
    assert len(hits) == 1
    assert hits[0].points == BUY_STARVED_PENALTY


def test_a_normal_trader_is_untouched_by_the_filter():
    from kaiba.intelligence.grade import penalties_for, sell_only_rejection

    ev = _evidence(observed_buys=60, observed_sells=40)
    assert sell_only_rejection(ev) is None
    assert not [p for p in penalties_for(ev) if p.name == "buy_starved"]


def test_a_buy_only_wallet_is_not_mistaken_for_a_settlement_address():
    from kaiba.intelligence.grade import ProviderStats, penalties_for, sell_only_rejection

    ev = _evidence(
        observed_buys=40, observed_sells=0,
        provider_stats=ProviderStats(buy_count=40, sell_count=0),
    )
    assert sell_only_rejection(ev) is None
    names = [p.name for p in penalties_for(ev)]
    assert "no_sells" in names  # the pre-existing, opposite penalty still applies
    assert "buy_starved" not in names


def test_provider_counts_are_used_when_we_have_not_backfilled_the_wallet():
    from kaiba.intelligence.grade import ProviderStats, sell_only_rejection

    ev = _evidence(provider_stats=ProviderStats(buy_count=0, sell_count=1_793))
    assert sell_only_rejection(ev) is not None


def test_raw_swap_counts_beat_the_episode_aggregate_that_hides_the_asymmetry(tmp_db):
    """`pnl.summarize` drops sell-without-buy episodes, so the aggregate looks balanced."""
    from kaiba.core.schemas import Grade, now_ms
    from kaiba.intelligence.grade import build_evidence, score_wallet

    tmp_db.execute(
        "INSERT INTO wallets (chain, address, source, first_seen_ms, last_seen_ms, cohort) "
        "VALUES ('sol',?,'test',?,?,'research')",
        (WALLET, now_ms(), now_ms()),
    )
    for i in range(60):  # sells of inventory that never arrived on chain
        _swap(tmp_db, WALLET, f"tok{i}", 1_000 + i, side="sell")
    _swap(tmp_db, WALLET, "tokClean", 2_000, side="buy")
    _swap(tmp_db, WALLET, "tokClean", 2_100, side="sell")

    ev = build_evidence(WALLET, Chain.SOL, tmp_db)
    assert ev.pnl is not None
    assert ev.pnl.buys == 1 and ev.pnl.sells == 1  # the aggregate looks symmetric
    assert ev.observed_buys == 1 and ev.observed_sells == 61  # the raw rows do not
    assert score_wallet(ev).grade is Grade.QUARANTINED


def test_a_backfilled_wallet_stops_being_unscored(tmp_db, monkeypatch, page):
    """The whole point of Phase 1: evidence weight above zero where there was none."""
    from kaiba.core.schemas import Grade, now_ms
    from kaiba.intelligence.grade import grade_address

    tmp_db.execute(
        "INSERT INTO wallets (chain, address, source, first_seen_ms, last_seen_ms, cohort) "
        "VALUES ('sol',?,'test',?,?,'research')",
        (WALLET, now_ms(), now_ms()),
    )
    before = grade_address(WALLET, Chain.SOL, tmp_db, store=False)
    assert before.evidence_weight == 0.0
    assert before.grade is Grade.UNSCORED

    FakeHelius([page], None).install(monkeypatch)
    backfill.backfill_wallets(tmp_db, Chain.SOL, wallets=[WALLET], with_meta=False)
    after = grade_address(WALLET, Chain.SOL, tmp_db, store=False)
    assert after.evidence_weight > 0.0
