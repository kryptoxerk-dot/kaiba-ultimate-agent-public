"""Trade-tape coverage.

Most of these exist because the failure they describe produces a *confident wrong answer*
rather than an error, which is the only kind of bug that reaches a trading decision.

* ``test_wallet_walk_at_the_create_slot_is_not_coverage`` — the trap this module was built
  around. A wallet backfill can leave exactly one row at a mint's create slot and none of
  the rest of that slot. "Our earliest swap sits at the launch" is then true and means
  nothing, the bundle share of a one-transaction tape is 0% by construction, and a
  ``max_bundler_pct`` gate that fails closed on an unknown swings wide open on that zero.
* ``test_schema_refuses_a_complete_claim_from_a_wallet_walk`` — the same rule, enforced one
  layer down. A convention a caller can forget is not a guarantee; a CHECK constraint is.
* ``test_topup_that_misses_its_watermark_is_demoted`` — a gap between the rows we had and
  the rows we just wrote is not a complete tape, however complete each half is.
* ``test_proof_does_not_survive_a_corrected_launch_time`` — a proof is only as good as the
  timestamp it was measured against.
* ``test_create_flag_comes_from_the_launchpad_not_a_guess`` — ``is_create_tx`` is set by
  matching the launchpad's own creation signature, so it stays right on a launchpad where
  create and dev-buy are *not* one transaction.
* ``test_fee_payer_is_never_inferred_from_the_trader`` — on a bonding-curve trade the
  trader usually is the signer, so guessing would be right on the easy cases and wrong on
  the bundled ones the shared-fee-payer rule exists to find.

Nothing here touches the network.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import Chain, EvidenceBasis, Receipt, now_ms
from kaiba.ingest import tape
from kaiba.ingest import token_flow as TF

MINT = "ALPMbbSSc3a8Utw9nDJ1ZqANfFt3rHBY3YsdQHzVpump"
OTHER = "JDdk2di16k1uTJ8xN5c2BYfvKZ3pRCfnWoQbGZpApump"
CREATOR = "9w3tvpQ5AJJH6kBA9gZEQXXcTCASz5NB7epLk9hns2QF"
TRADER = "8AomZxgirYYBnxbAaPzG3vWa2uob5DgPJv4GHMyKDEap"
BUNDLER = "BundLeR4Ynnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnn"
# 88 characters, the real length of a base58 Solana signature.
CREATE_SIG = "5W7pHNV1rKremMSQghYyA7yPJThJvdy3k94rdjNjKmWBAuMoW1cJ9cU53Qe34jWwbZq7Npq9QSNnzsBJSsRJrj7G"

T0 = 1_789_878_905_952


# --------------------------------------------------------------------------------------
# fixtures and builders
# --------------------------------------------------------------------------------------


def add_token(
    conn: sqlite3.Connection,
    address: str = MINT,
    *,
    created_ms: int | None = T0,
    launchpad: str | None = "pump.fun",
    decimals: int | None = None,
    creator: str | None = CREATOR,
    signature: str | None = CREATE_SIG,
) -> None:
    meta = "{}" if signature is None else json.dumps(
        {"signature": signature, "initial_buy_lamports": 97774557}
    )
    conn.execute(
        "INSERT OR REPLACE INTO tokens "
        "(chain, address, symbol, name, decimals, creator, created_ms, launchpad, "
        " first_seen_ms, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("sol", address, "TEST", "Test", decimals, creator, created_ms, launchpad,
         created_ms or T0, meta),
    )


def add_swap(
    conn: sqlite3.Connection,
    *,
    tx: str,
    wallet: str = TRADER,
    token: str = MINT,
    ts_ms: int = T0,
    source: str = tape.WALLET_SOURCE,
    slot: int | None = 448_671_580,
    fee_payer: str | None = None,
    side: str = "buy",
    amount_token: str | None = "1000000",
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO swaps "
        "(chain, tx, slot, block_index, ts_ms, wallet, token, side, amount_token, "
        " amount_native, price_usd, usd_value, program, source, is_create_tx, fee_payer) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("sol", tx, slot, 0, ts_ms, wallet, token, side, amount_token, "100000000",
         None, None, "pump", source, 0, fee_payer),
    )


def trade(ordinal_slot: int, ts_ms: int, *, wallet: str = TRADER, tx: str | None = None) -> dict[str, Any]:
    """One record in the shape ``frontend-api-v3.pump.fun/trades`` actually returns.

    The field set is copied from live pages recorded on 2026-09-20 and is deliberately
    complete, including the absence of any signer or fee-payer field.
    """
    return {
        "ordinalKey": f"{ordinal_slot}-1102-20204-{ts_ms}",
        "blockId": str(ordinal_slot),
        "txIndex": 1102,
        "eventIndex": 20204,
        "blockTimeMs": ts_ms,
        "txId": tx or f"sig{ordinal_slot}{ts_ms}",
        "legIndex": 0,
        "late": False,
        "isBackfill": False,
        "side": "buy",
        "kind": "swap",
        "venue": "pump",
        "pool": {"chainId": TF.SOLANA_CHAIN_ID, "address": "poolpoolpool"},
        "trader": {"address": wallet},
        "baseAmount": {"raw": "32751487", "decimals": 6},
        "quoteAmount": {"raw": "4975", "decimals": 7},
        "quote": {"id": "11111111111111111111111111111111"},
        "priceUsd": "0.001644617110700365",
        "valueUsd": "0.05399865",
        "valueNative": "0.0004975",
    }


def page(trades: list[dict[str, Any]]) -> dict[str, Any]:
    return {"trades": trades, "aggregates": {}, "cursor": "x", "source": "indexed"}


def full_page(base_slot: int, base_ms: int) -> dict[str, Any]:
    """Exactly ``page_limit`` trades, so the collector must ask for another page."""
    return page([trade(base_slot + i, base_ms + i * 1000) for i in range(100)])


def full_page_back_to(base_slot: int, newest_ms: int, oldest_ms: int) -> dict[str, Any]:
    """A full page, newest first, whose oldest trade lands on ``oldest_ms``.

    A short page ends the walk outright, which is a *stronger* result than reaching a
    watermark, so the watermark path can only be exercised with a page that is full.
    """
    step = (newest_ms - oldest_ms) // 99
    return page([trade(base_slot + i, newest_ms - i * step) for i in range(100)])


def stub_pages(monkeypatch, pages: list[dict[str, Any] | None], calls: list[Any] | None = None) -> None:
    """Serve ``pages`` in order to ``token_flow.fetch_trades_page``; ``None`` is unavailable.

    Deliberately stubbed at the fetch boundary rather than at ``collect_trades``, so every
    test below exercises the real parser, the real pagination and the real idempotent write.
    """
    state = {"i": 0}

    def _fetch(mint: str, *, before: str | None = None, **kw: Any):
        idx = min(state["i"], len(pages) - 1)
        state["i"] += 1
        if calls is not None:
            calls.append((mint, before))
        got = pages[idx]
        basis = EvidenceBasis.UNAVAILABLE if got is None else EvidenceBasis.PROVIDER_REPORTED
        return got, Receipt(provider=TF.PROVIDER, endpoint=TF.TRADES_ENDPOINT, basis=basis)

    monkeypatch.setattr(TF, "fetch_trades_page", _fetch)


@pytest.fixture
def cfg() -> tape.TapeConfig:
    """Small page budgets so a test can exhaust one without generating thousands of rows."""
    return tape.TapeConfig(walk_pages=2, topup_pages=1, budget_s=30.0, max_tokens=20)


# --------------------------------------------------------------------------------------
# the schema is the guarantee
# --------------------------------------------------------------------------------------


def _raw_insert(conn: sqlite3.Connection, **over: Any) -> None:
    row = {
        "chain": "sol", "token": MINT, "model": tape.MODEL_ID, "coverage": tape.COMPLETE,
        "route": tape.ROUTE_TRADES, "proof": "end_of_history", "reason": "ok",
        "covered_from_ms": T0 - 1, "covered_to_ms": T0 + 500, "created_ms": T0,
        "attempts": 0, "first_seen_ms": T0, "updated_ms": T0,
    }
    row.update(over)
    cols = ",".join(row)
    conn.execute(
        f"INSERT INTO token_tape ({cols}) VALUES ({','.join('?' * len(row))})", tuple(row.values())
    )


def test_schema_accepts_a_well_formed_complete_row(tmp_db):
    _raw_insert(tmp_db)
    assert fetch_one(tmp_db, "SELECT coverage FROM token_tape")["coverage"] == tape.COMPLETE


def test_schema_refuses_a_complete_claim_from_a_wallet_walk(tmp_db):
    with pytest.raises(sqlite3.IntegrityError):
        _raw_insert(tmp_db, route=tape.ROUTE_WALLET)


def test_schema_refuses_a_complete_claim_without_a_proof(tmp_db):
    with pytest.raises(sqlite3.IntegrityError):
        _raw_insert(tmp_db, proof=None)


def test_schema_refuses_a_complete_claim_that_starts_after_launch(tmp_db):
    with pytest.raises(sqlite3.IntegrityError):
        _raw_insert(tmp_db, covered_from_ms=T0 + 60_000)


def test_schema_refuses_a_complete_claim_with_no_launch_time(tmp_db):
    with pytest.raises(sqlite3.IntegrityError):
        _raw_insert(tmp_db, created_ms=None)


def test_schema_allows_partial_without_any_proof(tmp_db):
    _raw_insert(tmp_db, coverage=tape.PARTIAL, route=tape.ROUTE_WALLET, proof=None,
                covered_from_ms=None, created_ms=None)
    assert fetch_one(tmp_db, "SELECT coverage FROM token_tape")["coverage"] == tape.PARTIAL


# --------------------------------------------------------------------------------------
# reading coverage fails closed
# --------------------------------------------------------------------------------------


def test_unassessed_token_is_not_complete(tmp_db):
    add_token(tmp_db)
    ok, reason = tape.completeness(Chain.SOL, MINT, tmp_db)
    assert ok is False
    assert reason == "no_tape_record"


def test_wallet_walk_at_the_create_slot_is_not_coverage(tmp_db):
    """One row at the launch slot from a *wallet* walk must read exactly like no tape.

    This is the whole point of the module. The timestamp heuristic — "our earliest swap row
    sits at the launch" — is satisfied here, and what we hold is one transaction out of an
    unknown number in that same slot.
    """
    add_token(tmp_db)
    add_swap(tmp_db, tx="walletwalkrow", ts_ms=T0 + 200, source=tape.WALLET_SOURCE)

    earliest = fetch_one(tmp_db, "SELECT MIN(ts_ms) AS m FROM swaps WHERE token=?", (MINT,))["m"]
    assert earliest - T0 < 1_000, "precondition: the naive proxy would call this covered"

    assert tape.is_complete(Chain.SOL, MINT, tmp_db) is False
    assert tape.complete_tokens(Chain.SOL, tmp_db) == []


def test_partial_record_is_not_complete(tmp_db):
    add_token(tmp_db)
    _raw_insert(tmp_db, coverage=tape.PARTIAL, proof=None, covered_from_ms=None)
    ok, reason = tape.completeness(Chain.SOL, MINT, tmp_db)
    assert ok is False
    assert reason.startswith("partial")


def test_proof_does_not_survive_a_launch_time_corrected_backwards(tmp_db):
    """A launch we now believe predates our coverage invalidates the proof."""
    add_token(tmp_db)
    _raw_insert(tmp_db)
    assert tape.is_complete(Chain.SOL, MINT, tmp_db) is True

    tmp_db.execute("UPDATE tokens SET created_ms=? WHERE address=?", (T0 - 900_000, MINT))
    ok, reason = tape.completeness(Chain.SOL, MINT, tmp_db)
    assert ok is False
    assert "launch_moved_before_coverage" in reason
    assert tape.complete_tokens(Chain.SOL, tmp_db) == []


def test_complete_record_reads_complete(tmp_db):
    add_token(tmp_db)
    _raw_insert(tmp_db)
    ok, reason = tape.completeness(Chain.SOL, MINT, tmp_db)
    assert ok is True
    assert reason == "end_of_history"
    assert tape.complete_tokens(Chain.SOL, tmp_db) == [MINT]


# --------------------------------------------------------------------------------------
# a failed observation is not an observation
#
# These are the regression tests for a live incident on 2026-09-20: proved-complete rows
# were re-scanned 8-10 minutes later, hit the hot-window 503, and the refusal was written
# over the proof. `complete_tokens` went 12 -> 7 in two minutes and, because the tape is a
# hot-window resource, none of it could be re-earned.
# --------------------------------------------------------------------------------------


def test_a_failed_refetch_never_downgrades_a_proof(tmp_db, monkeypatch, cfg):
    add_token(tmp_db)
    stub_pages(monkeypatch, [page([trade(1, T0 + 5_000)])])
    tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg)
    assert tape.is_complete(Chain.SOL, MINT, tmp_db) is True

    stub_pages(monkeypatch, [None])  # the hot window closed
    record, _ = tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg)

    assert record.coverage == tape.COMPLETE
    assert record.proof == "end_of_history"
    assert "failed attempt" in record.reason
    assert record.attempts == 2
    assert tape.is_complete(Chain.SOL, MINT, tmp_db) is True
    assert tape.complete_tokens(Chain.SOL, tmp_db) == [MINT]


def test_a_failed_scan_never_downgrades_a_proof(tmp_db, monkeypatch, cfg):
    """The same rule on the scanner path, which is where it actually happened."""
    add_token(tmp_db)
    stub_pages(monkeypatch, [page([trade(1, T0 + 5_000)])])
    tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg)
    assert tape.is_complete(Chain.SOL, MINT, tmp_db) is True

    record = tape.record_scan_capture(
        Chain.SOL, MINT, {"flow_reason": "unavailable", "flow_pages": 0}, tmp_db
    )

    assert record is not None and record.coverage == tape.COMPLETE
    assert tape.is_complete(Chain.SOL, MINT, tmp_db) is True


def test_real_contradicting_evidence_still_demotes(tmp_db, monkeypatch, cfg):
    """A gap we actually observed is evidence. Silence is not. Only the first may demote.

    ``at_ms`` is explicit on both calls, and that is not decoration. A terminated walk now
    advances ``covered_to_ms`` to *the moment it looked* rather than to the last trade it
    found (:func:`tape.observed_to_ms`), so on the real wall clock the watermark would
    land a day past every synthetic trade here and the second walk would "reach" it
    without fetching anything. Pinning the clock is what keeps the gap a gap.
    """
    add_token(tmp_db)
    stub_pages(monkeypatch, [page([trade(1, T0 + 5_000)])])
    tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg, at_ms=T0 + 10_000)

    stub_pages(monkeypatch, [full_page(5_000, T0 + 800_000), full_page(6_000, T0 + 600_000)])
    record, _ = tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg, at_ms=T0 + 900_000)

    assert record.coverage == tape.PARTIAL
    assert record.reason.startswith("topup_gap")
    assert tape.is_complete(Chain.SOL, MINT, tmp_db) is False


def test_schema_refuses_the_downgrade_even_if_the_code_forgets(tmp_db):
    """Backstop. The data this protects cannot be recovered if a future caller slips."""
    add_token(tmp_db)
    _raw_insert(tmp_db)

    with pytest.raises(sqlite3.IntegrityError):
        tmp_db.execute(
            "UPDATE token_tape SET coverage=? WHERE token=?", (tape.UNAVAILABLE, MINT)
        )
    assert tape.is_complete(Chain.SOL, MINT, tmp_db) is True

    # complete -> partial is a real observation and stays permitted.
    tmp_db.execute(
        "UPDATE token_tape SET coverage=?, proof=NULL, covered_from_ms=NULL WHERE token=?",
        (tape.PARTIAL, MINT),
    )
    assert tape.is_complete(Chain.SOL, MINT, tmp_db) is False


def test_seed_re_adopts_over_a_damaged_record(tmp_db):
    """The five rows that were already wrecked have to be recoverable, or the fix is half."""
    add_token(tmp_db, MINT)
    _snapshot(tmp_db, MINT, coverage_from_ms=T0 - 1)
    assert tape.seed_from_snapshots(Chain.SOL, tmp_db) == 1

    # Simulate the damage exactly as it occurred, bypassing both guards.
    tmp_db.execute("DROP TRIGGER trg_token_tape_no_downgrade_on_silence")
    tmp_db.execute(
        "UPDATE token_tape SET coverage=?, proof=NULL, covered_from_ms=NULL, "
        "reason='scan: provider_unavailable', attempts=2 WHERE token=?",
        (tape.UNAVAILABLE, MINT),
    )
    assert tape.complete_tokens(Chain.SOL, tmp_db) == []

    assert tape.seed_from_snapshots(Chain.SOL, tmp_db) == 1
    assert tape.complete_tokens(Chain.SOL, tmp_db) == [MINT]
    restored = tape.record_of(Chain.SOL, MINT, tmp_db)
    assert restored is not None and "re-adopted" in restored.reason
    assert restored.attempts == 2, "attempt history should survive the repair"


def test_seed_leaves_a_standing_proof_alone(tmp_db, monkeypatch, cfg):
    add_token(tmp_db)
    stub_pages(monkeypatch, [page([trade(1, T0 + 5_000)])])
    tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg)
    _snapshot(tmp_db, MINT, coverage_from_ms=T0 - 1)

    assert tape.seed_from_snapshots(Chain.SOL, tmp_db) == 0
    assert tape.record_of(Chain.SOL, MINT, tmp_db).proof == "end_of_history"


# --------------------------------------------------------------------------------------
# a launch time that rounds is not a launch time that moved
# --------------------------------------------------------------------------------------


def test_subsecond_launch_rounding_does_not_break_a_proof(tmp_db):
    """pump.fun floors created_timestamp to the second; the token row is millisecond exact.

    A proof stored against ...028000 was read against the token row's ...029249 and a
    1,249 ms difference silently failed the completeness check.
    """
    add_token(tmp_db, created_ms=T0 + 1_249)
    _raw_insert(tmp_db, created_ms=T0, covered_from_ms=T0)

    ok, reason = tape.completeness(Chain.SOL, MINT, tmp_db)
    assert ok is True, reason
    assert tape.complete_tokens(Chain.SOL, tmp_db) == [MINT]


def test_a_launch_time_moving_earlier_than_our_coverage_does_break_it(tmp_db):
    """The direction that actually matters: coverage no longer reaches the launch."""
    add_token(tmp_db)
    _raw_insert(tmp_db, created_ms=T0, covered_from_ms=T0)
    tmp_db.execute("UPDATE tokens SET created_ms=? WHERE address=?", (T0 - 600_000, MINT))

    ok, reason = tape.completeness(Chain.SOL, MINT, tmp_db)
    assert ok is False
    assert "launch_moved_before_coverage" in reason


def test_scan_capture_stores_the_token_rows_launch_time_not_the_payloads(tmp_db, monkeypatch, cfg):
    """One source of truth for the number `completeness` compares against."""
    add_token(tmp_db, created_ms=T0 + 1_249)
    stub_pages(monkeypatch, [page([trade(1, T0 + 5_000)])])
    tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg)

    record = tape.record_scan_capture(
        Chain.SOL, MINT,
        {"flow_reason": "end_of_history", "flow_pages": 1, "created_ms": T0},
        tmp_db,
    )

    assert record is not None
    assert record.created_ms == T0 + 1_249, "the second-rounded payload value was preferred"
    assert tape.is_complete(Chain.SOL, MINT, tmp_db) is True


# --------------------------------------------------------------------------------------
# collecting
# --------------------------------------------------------------------------------------


def test_walk_to_end_of_history_proves_coverage(tmp_db, monkeypatch, cfg):
    add_token(tmp_db)
    stub_pages(monkeypatch, [page([trade(1, T0 + 5_000), trade(2, T0 + 1_000)])])

    record, flow = tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg)

    assert flow is not None and flow.reason == "end_of_history"
    assert record.coverage == tape.COMPLETE
    assert record.route == tape.ROUTE_TRADES
    assert record.proof == "end_of_history"
    assert record.covered_from_ms is not None and record.covered_from_ms <= T0
    assert tape.is_complete(Chain.SOL, MINT, tmp_db) is True
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM swaps WHERE token=?", (MINT,))["n"] == 2


def test_exhausted_page_budget_is_partial_not_complete(tmp_db, monkeypatch, cfg):
    """Rows arrived, the walk never terminated. That is an unquantified fraction."""
    add_token(tmp_db)
    stub_pages(monkeypatch, [full_page(1000, T0 + 500_000), full_page(2000, T0 + 300_000)])

    record, flow = tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg)

    assert flow is not None and flow.pages == 2
    assert record.coverage == tape.PARTIAL
    assert "walk_incomplete" in record.reason
    assert tape.is_complete(Chain.SOL, MINT, tmp_db) is False
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM swaps WHERE token=?", (MINT,))["n"] == 200


def test_provider_refusal_is_unavailable_and_schedules_a_retry(tmp_db, monkeypatch, cfg):
    add_token(tmp_db)
    stub_pages(monkeypatch, [None])

    record, _ = tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg)

    assert record.coverage == tape.UNAVAILABLE
    assert record.reason == "provider_unavailable"
    assert record.next_attempt_ms is not None and record.next_attempt_ms > now_ms()
    assert tape.is_complete(Chain.SOL, MINT, tmp_db) is False


def test_terminated_walk_without_a_launch_time_is_not_complete(tmp_db, monkeypatch, cfg):
    """We hold every trade, but nothing ties that to a launch, which is what lanes gate on."""
    add_token(tmp_db, created_ms=None)
    stub_pages(monkeypatch, [page([trade(1, T0 + 5_000)])])

    record, _ = tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg)

    assert record.coverage == tape.PARTIAL
    assert record.reason == "walk_terminated_but_creation_time_unknown"
    assert tape.is_complete(Chain.SOL, MINT, tmp_db) is False


def test_unknown_decimals_off_pumpfun_refuses_before_spending_a_request(tmp_db, monkeypatch, cfg):
    add_token(tmp_db, launchpad="letsbonk", decimals=None)
    calls: list[Any] = []
    stub_pages(monkeypatch, [page([trade(1, T0 + 1_000)])], calls)

    record, flow = tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg)

    assert flow is None
    assert calls == [], "a request was spent on a token whose amounts would be unusable"
    assert record.coverage == tape.UNAVAILABLE
    assert record.reason.startswith("decimals_unknown_for_launchpad")
    # NULL would mean "eligible now", so a permanent refusal would be re-offered forever.
    assert record.next_attempt_ms is not None and record.next_attempt_ms > now_ms()
    assert tape.candidates(Chain.SOL, tmp_db, config=cfg) == []


def test_recollecting_writes_no_duplicate_swaps(tmp_db, monkeypatch, cfg):
    add_token(tmp_db)
    trades = [trade(1, T0 + 5_000), trade(2, T0 + 1_000)]
    stub_pages(monkeypatch, [page(trades)])
    tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg)
    first = fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM swaps WHERE token=?", (MINT,))["n"]

    stub_pages(monkeypatch, [page(trades)])
    tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg)
    second = fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM swaps WHERE token=?", (MINT,))["n"]

    assert first == second == 2


def test_topup_of_a_complete_tape_stays_complete_and_costs_one_page(tmp_db, monkeypatch, cfg):
    add_token(tmp_db)
    stub_pages(monkeypatch, [page([trade(1, T0 + 5_000), trade(2, T0 + 1_000)])])
    tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg, at_ms=T0 + 10_000)
    assert tape.is_complete(Chain.SOL, MINT, tmp_db) is True

    calls: list[Any] = []
    stub_pages(monkeypatch, [full_page_back_to(3_000, T0 + 200_000, T0 + 4_000)], calls)
    record, flow = tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg, at_ms=T0 + 300_000)

    assert len(calls) == 1, "a complete tape must not be re-pulled in full"
    assert flow is not None and flow.reason == "reached_watermark"
    assert record.coverage == tape.COMPLETE
    assert record.proof == "topup_contiguous"
    assert record.covered_from_ms is not None and record.covered_from_ms <= T0
    assert tape.is_complete(Chain.SOL, MINT, tmp_db) is True


def test_topup_that_misses_its_watermark_is_demoted(tmp_db, monkeypatch, cfg):
    """New rows that do not join up with the old ones leave a hole. Fail closed."""
    add_token(tmp_db)
    stub_pages(monkeypatch, [page([trade(1, T0 + 5_000), trade(2, T0 + 1_000)])])
    tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg, at_ms=T0 + 10_000)
    assert tape.is_complete(Chain.SOL, MINT, tmp_db) is True

    # A full page of trades all newer than the watermark, with only one page of budget.
    stub_pages(monkeypatch, [full_page(5000, T0 + 800_000), full_page(6000, T0 + 600_000)])
    record, _ = tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg, at_ms=T0 + 900_000)

    assert record.coverage == tape.PARTIAL
    assert record.reason.startswith("topup_gap")
    assert tape.is_complete(Chain.SOL, MINT, tmp_db) is False


# --------------------------------------------------------------------------------------
# adopting the proofs that predate this table
# --------------------------------------------------------------------------------------


def _snapshot(conn: sqlite3.Connection, token: str, *, coverage_from_ms: int | None) -> None:
    conn.execute(
        "INSERT INTO curve_snapshots "
        "(chain, token, observed_ms, real_sol_lamports, virtual_sol_lamports, real_token_atoms, "
        " virtual_token_atoms, coverage_from_ms, created_ms, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("sol", token, T0 + 10_000, 1_000_000, 30_000_000_000, "793100000000000",
         "1073000000000000", coverage_from_ms, T0, "pumpfun"),
    )


def test_seed_adopts_a_proved_snapshot_and_refuses_a_partial_one(tmp_db):
    add_token(tmp_db, MINT)
    add_token(tmp_db, OTHER)
    _snapshot(tmp_db, MINT, coverage_from_ms=T0 - 1)       # walk terminated at/before launch
    _snapshot(tmp_db, OTHER, coverage_from_ms=T0 + 90_000)  # walk stopped well after launch

    seeded = tape.seed_from_snapshots(Chain.SOL, tmp_db)

    assert seeded == 1
    assert tape.complete_tokens(Chain.SOL, tmp_db) == [MINT]
    assert tape.record_of(Chain.SOL, OTHER, tmp_db) is None


def test_seed_is_idempotent_and_does_not_overwrite(tmp_db):
    add_token(tmp_db, MINT)
    _snapshot(tmp_db, MINT, coverage_from_ms=T0 - 1)
    assert tape.seed_from_snapshots(Chain.SOL, tmp_db) == 1
    assert tape.seed_from_snapshots(Chain.SOL, tmp_db) == 0


# --------------------------------------------------------------------------------------
# is_create_tx
# --------------------------------------------------------------------------------------


def test_create_flag_comes_from_the_launchpad_not_a_guess(tmp_db):
    add_token(tmp_db)
    add_swap(tmp_db, tx=CREATE_SIG, wallet=CREATOR, ts_ms=T0, source=tape.WALLET_SOURCE)
    add_swap(tmp_db, tx="someothertrade", wallet=TRADER, ts_ms=T0 + 900, source=tape.WALLET_SOURCE)

    marked, with_sig = tape.repair_create_flags(Chain.SOL, tmp_db)

    assert (marked, with_sig) == (1, 1)
    rows = {r["tx"]: r["is_create_tx"] for r in fetch_all(tmp_db, "SELECT tx, is_create_tx FROM swaps")}
    assert rows[CREATE_SIG] == 1
    assert rows["someothertrade"] == 0


def test_create_flag_is_idempotent(tmp_db):
    add_token(tmp_db)
    add_swap(tmp_db, tx=CREATE_SIG, wallet=CREATOR)
    assert tape.repair_create_flags(Chain.SOL, tmp_db)[0] == 1
    assert tape.repair_create_flags(Chain.SOL, tmp_db)[0] == 0


def test_no_create_flag_when_the_creator_never_bought(tmp_db):
    """No dev buy means no create swap row. The absence is the right answer, not a miss."""
    add_token(tmp_db)
    add_swap(tmp_db, tx="firsttradebysomeoneelse", wallet=TRADER, ts_ms=T0 + 40_000)

    marked, with_sig = tape.repair_create_flags(Chain.SOL, tmp_db)

    assert (marked, with_sig) == (0, 1)
    assert fetch_one(tmp_db, "SELECT SUM(is_create_tx) AS n FROM swaps")["n"] == 0


def test_create_flag_does_not_leak_across_tokens(tmp_db):
    add_token(tmp_db, MINT)
    add_token(tmp_db, OTHER, signature=None)
    add_swap(tmp_db, tx=CREATE_SIG, wallet=CREATOR, token=OTHER)

    marked, _ = tape.repair_create_flags(Chain.SOL, tmp_db)

    assert marked == 0, "a create signature marked a row belonging to a different mint"


@pytest.mark.parametrize("raw", [None, "{}", '{"signature": 42}', '{"signature": "short"}',
                                 '{"signature": ""}', "not json"])
def test_malformed_create_signatures_are_rejected(raw):
    assert tape.create_signature(raw) is None


def test_create_signature_accepts_a_real_one():
    assert tape.create_signature(json.dumps({"signature": CREATE_SIG})) == CREATE_SIG


# --------------------------------------------------------------------------------------
# fee_payer
# --------------------------------------------------------------------------------------


def test_fee_payer_is_never_inferred_from_the_trader(tmp_db):
    add_token(tmp_db)
    add_swap(tmp_db, tx="tradewithnosigner", wallet=TRADER, source=TF.SOURCE)

    copied, still_missing = tape.repair_fee_payer(Chain.SOL, tmp_db)

    assert copied == 0
    assert still_missing == 1
    assert fetch_one(tmp_db, "SELECT fee_payer FROM swaps")["fee_payer"] is None


def test_fee_payer_is_copied_across_collectors_for_the_same_transaction(tmp_db):
    add_token(tmp_db)
    add_swap(tmp_db, tx="sharedtx", wallet=TRADER, source="pumpfun:trades", fee_payer=None,
             amount_token="1000000")
    add_swap(tmp_db, tx="sharedtx", wallet=BUNDLER, source=tape.WALLET_SOURCE,
             fee_payer=BUNDLER, amount_token="2000000")

    copied, _ = tape.repair_fee_payer(Chain.SOL, tmp_db)

    assert copied == 1
    payers = {r["wallet"]: r["fee_payer"] for r in fetch_all(tmp_db, "SELECT wallet, fee_payer FROM swaps")}
    assert payers[TRADER] == BUNDLER, "the signer is the bundler, not the trader"


def test_fee_payer_is_not_copied_when_collectors_disagree(tmp_db):
    add_token(tmp_db)
    add_swap(tmp_db, tx="disputed", wallet=TRADER, source="pumpfun:trades", amount_token="1")
    add_swap(tmp_db, tx="disputed", wallet="w2", source=tape.WALLET_SOURCE,
             fee_payer="payerA", amount_token="2")
    add_swap(tmp_db, tx="disputed", wallet="w3", source=tape.WALLET_SOURCE,
             fee_payer="payerB", amount_token="3")

    copied, _ = tape.repair_fee_payer(Chain.SOL, tmp_db)

    assert copied == 0
    assert fetch_one(tmp_db, "SELECT fee_payer FROM swaps WHERE wallet=?", (TRADER,))["fee_payer"] is None


def test_the_trade_record_really_has_no_signer_field():
    """Guards the claim the module makes: if the route ever adds one, this test must fail."""
    fields = set(trade(1, T0))
    assert not fields & {"feePayer", "fee_payer", "signer", "signers", "payer", "accountKeys"}
    assert "trader" in fields and "txId" in fields


# --------------------------------------------------------------------------------------
# the job
# --------------------------------------------------------------------------------------


def test_candidates_skips_proved_respects_backoff_and_retires(tmp_db, cfg):
    add_token(tmp_db, MINT, created_ms=T0)
    add_token(tmp_db, OTHER, created_ms=T0 + 1_000)
    assert set(tape.candidates(Chain.SOL, tmp_db, config=cfg)) == {MINT, OTHER}

    _raw_insert(tmp_db)  # MINT proved complete
    assert tape.candidates(Chain.SOL, tmp_db, config=cfg) == [OTHER]

    # Replace the proved row with a refused one. It has to be a delete-and-insert rather
    # than an UPDATE, because the schema now refuses complete -> unavailable outright.
    tmp_db.execute("DELETE FROM token_tape WHERE token=?", (MINT,))
    _raw_insert(tmp_db, coverage=tape.UNAVAILABLE, proof=None, covered_from_ms=None,
                created_ms=None, next_attempt_ms=now_ms() + 3_600_000)
    assert tape.candidates(Chain.SOL, tmp_db, config=cfg) == [OTHER], "backoff was ignored"

    tmp_db.execute("UPDATE token_tape SET next_attempt_ms=0, attempts=? WHERE token=?",
                   (cfg.max_attempts, MINT))
    assert tape.candidates(Chain.SOL, tmp_db, config=cfg) == [OTHER], "a retired token came back"
    assert set(tape.candidates(Chain.SOL, tmp_db, config=cfg, include_retired=True)) == {MINT, OTHER}


def test_candidates_orders_newest_launch_first(tmp_db, cfg):
    add_token(tmp_db, MINT, created_ms=T0)
    add_token(tmp_db, OTHER, created_ms=T0 + 600_000)
    assert tape.candidates(Chain.SOL, tmp_db, config=cfg) == [OTHER, MINT]


def test_run_reports_before_and_after_and_measures_its_rate(tmp_db, monkeypatch, cfg):
    add_token(tmp_db, MINT, created_ms=T0)
    add_token(tmp_db, OTHER, created_ms=T0 + 1_000)
    add_swap(tmp_db, tx=CREATE_SIG, wallet=CREATOR, token=MINT)
    stub_pages(monkeypatch, [page([trade(1, T0 + 5_000)])])

    report = tape.run(Chain.SOL, tmp_db, config=cfg)

    assert report.complete_before == 0
    assert report.complete_after == 2
    assert report.attempted == 2
    assert report.completed == 2
    assert report.pages == 2
    assert report.create_flags_set == 1
    # The fetch is stubbed, so nothing reached the limiter's ledger and the request rate is
    # correctly 0. The page rate is the one this test can speak to.
    assert report.page_rate > 0
    assert report.requests == 0
    assert report.as_dict()["complete_after"] == 2


def test_request_rate_counts_failed_attempts_not_pages_obtained(tmp_db, monkeypatch, cfg):
    """A mint outside the route's hot window costs requests and yields no page.

    Reporting pages-per-second as "the rate we sustain" would understate our load on a free
    endpoint roughly threefold, because every refused mint still spends its retry budget.
    """
    add_token(tmp_db, MINT, created_ms=T0)
    stub_pages(monkeypatch, [None])

    def _ledger(*_a: Any, **_kw: Any) -> tuple[int, int]:
        return 3, 1  # three calls on the wire, one of them a 429

    monkeypatch.setattr(tape, "_provider_calls_since", _ledger)
    report = tape.run(Chain.SOL, tmp_db, config=cfg)

    assert report.pages == 0
    assert report.page_rate == 0.0
    assert report.requests == 3
    assert report.rate_limited == 1
    assert report.request_rate > report.page_rate


def test_request_rate_reads_the_limiter_ledger(tmp_db, monkeypatch, cfg):
    add_token(tmp_db, MINT, created_ms=T0)
    stub_pages(monkeypatch, [page([trade(1, T0 + 5_000)])])
    for status in ("ok", "error", "rate_limited"):
        tmp_db.execute(
            "INSERT INTO provider_calls (provider, endpoint, weight, ts_ms, status) VALUES (?,?,?,?,?)",
            (TF.PROVIDER, TF.TRADES_ENDPOINT, 1, now_ms() + 50, status),
        )

    report = tape.run(Chain.SOL, tmp_db, config=cfg)

    assert report.requests == 3, "the limiter's own ledger is the authority on what we sent"
    assert report.rate_limited == 1


def test_run_is_safe_to_repeat_and_does_not_refetch_a_complete_tape(tmp_db, monkeypatch, cfg):
    add_token(tmp_db, MINT, created_ms=T0)
    stub_pages(monkeypatch, [page([trade(1, T0 + 5_000)])])
    tape.run(Chain.SOL, tmp_db, config=cfg)

    calls: list[Any] = []
    stub_pages(monkeypatch, [page([trade(1, T0 + 5_000)])], calls)
    second = tape.run(Chain.SOL, tmp_db, config=cfg)

    assert calls == [], "a proved-complete token was offered as a candidate again"
    assert second.attempted == 0
    assert second.complete_after == 1
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM swaps WHERE token=?", (MINT,))["n"] == 1


def test_run_honours_its_wall_clock_budget(tmp_db, monkeypatch, cfg):
    for i in range(5):
        add_token(tmp_db, f"{MINT[:-4]}{i:04d}", created_ms=T0 + i)
    stub_pages(monkeypatch, [page([trade(1, T0 + 5_000)])])

    report = tape.run(Chain.SOL, tmp_db, config=cfg, budget_s=-1.0)

    assert report.attempted == 0
    assert report.skipped == 5
    assert "budget_exhausted" in report.reasons


def test_coverage_summary_counts_the_two_routes_separately(tmp_db, monkeypatch, cfg):
    add_token(tmp_db, MINT, created_ms=T0)
    add_token(tmp_db, OTHER, created_ms=T0 + 1)
    add_swap(tmp_db, tx="walletrow", token=OTHER, source=tape.WALLET_SOURCE)
    stub_pages(monkeypatch, [page([trade(1, T0 + 5_000)]), None])
    tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg)
    tape.collect_token(Chain.SOL, OTHER, tmp_db, config=cfg)

    summary = tape.coverage_summary(Chain.SOL, tmp_db)

    assert summary["tokens_known"] == 2
    assert summary["complete"] == 1
    assert summary["unavailable"] == 1
    assert summary["swaps_per_token_route"] == 1
    assert summary["swaps_wallet_walk"] == 1


# --------------------------------------------------------------------------------------
# depth: complete is not the same as deep
#
# Measured on data/kaiba.db on 2026-09-21: 4,071 complete tapes, median span 41 s, 17
# reaching five minutes. The published result these tapes exist to test (arXiv 2608.20271)
# is stated over the FIRST FIVE MINUTES of trading microstructure, so 17 samples tests
# nothing. Three mechanisms produced that shape and none of them was the market:
# `candidates` could not re-offer a complete tape, nothing advanced the watermark, and
# depth was being read as last-trade-minus-first-trade.
#
# Every test in this section is written so that removing the guard it describes makes it
# fail. Where that is not obvious from the assertion, the docstring says which line.
# --------------------------------------------------------------------------------------


def _proved_row(conn: sqlite3.Connection, token: str = MINT, **over: Any) -> None:
    """A standing proof, shallow by default: covered to launch+500 ms."""
    add_token(conn, token, created_ms=over.pop("created_ms", T0))
    _raw_insert(conn, token=token, **over)


def test_a_shallow_complete_tape_is_offered_for_deepening(tmp_db, cfg):
    """The headline defect. A proved tape could be finished and never improved.

    Removing the ``OR <deepenable>`` arm of `tape.candidates` makes this fail, and that
    arm is the entire difference between 17 five-minute tapes and a testable sample.
    """
    _proved_row(tmp_db)
    assert tape.is_complete(Chain.SOL, MINT, tmp_db) is True

    due = tape.candidates(Chain.SOL, tmp_db, config=cfg, at_ms=T0 + 60_000)

    assert due == [MINT]
    assert tape.deepenable(Chain.SOL, tmp_db, config=cfg, at_ms=T0 + 60_000) == [MINT]


def test_a_tape_that_already_spans_the_horizon_is_not_re_asked(tmp_db, cfg):
    """Deleting `_SHALLOW_SQL` makes this fail: we would re-ask every mint we ever proved."""
    _proved_row(tmp_db, covered_to_ms=T0 + cfg.deep_enough_ms)

    assert tape.candidates(Chain.SOL, tmp_db, config=cfg, at_ms=T0 + 400_000) == []
    assert tape.deepenable(Chain.SOL, tmp_db, config=cfg, at_ms=T0 + 400_000) == []


def test_a_mint_outside_the_hot_window_is_not_re_asked_however_shallow(tmp_db, cfg):
    """Deleting the idle clause makes this fail.

    The route 503s per-mint and persistently once a mint stops trading -- 37 of 37 at
    48.3-169.7 min idle, 6 of 6 at 81-162 min on 2026-09-21. A first capture is worth a
    maybe because there is no second chance; a deepening re-ask is not, because a refusal
    buys nothing and still spends a request on the provider that rate-limited us.
    """
    _proved_row(tmp_db)
    cold = T0 + cfg.deepen_max_idle_ms + 60_000

    assert tape.candidates(Chain.SOL, tmp_db, config=cfg, at_ms=cold) == []


def test_the_idle_clause_uses_activity_not_age(tmp_db, cfg):
    """A 70-minute-old mint still trading is askable; its *age* is irrelevant.

    The hot window was measured against trade inactivity, not against how long ago the
    mint was minted, and conflating the two would drop exactly the tokens worth deepening.
    """
    old_launch = T0 - 70 * 60_000
    add_token(tmp_db, MINT, created_ms=old_launch)
    _raw_insert(tmp_db, covered_from_ms=old_launch - 1, created_ms=old_launch,
                covered_to_ms=old_launch + 40_000, newest_ms=T0)

    assert tape.candidates(Chain.SOL, tmp_db, config=cfg, at_ms=T0 + 60_000) == [MINT]


def test_a_proved_tape_with_no_watermark_is_left_alone(tmp_db, cfg):
    """No watermark means a re-walk, and a re-walk that runs short would destroy the proof.

    Deleting ``covered_to_ms IS NOT NULL`` from `_DEEPENABLE_SQL` makes this fail. 17 rows
    on the live database are in this state; they are the rows most in need of depth and
    the ones where asking for it risks the most, so they are deliberately not offered.
    """
    _proved_row(tmp_db, covered_to_ms=None)

    assert tape.is_complete(Chain.SOL, MINT, tmp_db) is True
    assert tape.candidates(Chain.SOL, tmp_db, config=cfg, at_ms=T0 + 60_000) == []


def test_deepenable_mints_are_served_before_merely_new_ones(tmp_db, cfg):
    """A perishable 40-second tape outranks an unseen launch minted seconds ago.

    Removing ``deepenable DESC`` from the ORDER BY makes this fail. The unseen launch is
    newer, so the old newest-first rule put it first -- and it will still be reachable on
    the next pass, while the proved-but-shallow mint will not.
    """
    _proved_row(tmp_db, MINT)                         # launched at T0, shallow, still hot
    add_token(tmp_db, OTHER, created_ms=T0 + 59_000)  # a brand new launch, never captured

    due = tape.candidates(Chain.SOL, tmp_db, config=cfg, at_ms=T0 + 60_000)

    assert due == [MINT, OTHER]


def test_deepenable_mints_are_ordered_by_soonest_expiry(tmp_db, cfg):
    """Within the perishable class, least time left in the hot window goes first.

    Flipping that ASC to DESC makes this fail. Both mints will answer now; only one of
    them will still answer in five minutes.
    """
    nearly_gone = f"{MINT[:-4]}aaaa"
    plenty_left = f"{MINT[:-4]}bbbb"
    _proved_row(tmp_db, nearly_gone, covered_to_ms=T0 + 1_000)
    _proved_row(tmp_db, plenty_left, covered_to_ms=T0 + 500_000, created_ms=T0 + 499_000)

    due = tape.candidates(Chain.SOL, tmp_db, config=cfg, at_ms=T0 + 600_000)

    assert due == [nearly_gone, plenty_left]


def test_candidates_and_deepenable_agree_on_the_same_database(tmp_db, cfg):
    """Two queries, one predicate. Asserted rather than assumed, because they did drift.

    The backoff and retirement cases are in here because the first version of
    ``tape.deepenable`` left them out and a live pass on 2026-09-21 caught it: 14 mints
    that had just been deepened and were sitting on their next scheduled visit were still
    being reported as perishable work outstanding. Deleting either clause from
    ``deepenable`` makes this fail.
    """
    backing_off = f"{MINT[:-4]}dddd"
    retired = f"{MINT[:-4]}eeee"
    _proved_row(tmp_db, MINT)
    _proved_row(tmp_db, f"{MINT[:-4]}cccc", covered_to_ms=T0 + 400_000)   # already deep
    _proved_row(tmp_db, backing_off, next_attempt_ms=T0 + 900_000)        # visit not due yet
    _proved_row(tmp_db, retired, attempts=cfg.max_attempts)               # out of attempts
    add_token(tmp_db, OTHER, created_ms=T0 + 1_000)                       # never captured

    at = T0 + 60_000
    due = tape.candidates(Chain.SOL, tmp_db, config=cfg, at_ms=at)
    perishable = tape.deepenable(Chain.SOL, tmp_db, config=cfg, at_ms=at)

    assert perishable == [MINT]
    assert due[: len(perishable)] == perishable
    assert backing_off not in due and retired not in due
    assert retired in tape.deepenable(Chain.SOL, tmp_db, config=cfg, at_ms=at, include_retired=True)


# --------------------------------------------------------------------------------------
# a tape that grows at the END must stay honest about its START
# --------------------------------------------------------------------------------------


def test_deepening_never_moves_the_start_of_the_tape(tmp_db, monkeypatch, cfg):
    """The one invariant the bundle detector and the confluence scorer rest on.

    ``covered_from_ms``, ``proof``, ``created_ms`` and ``coverage`` must read identically
    before and after a successful deepening top-up. Only ``covered_to_ms`` may move.
    """
    add_token(tmp_db)
    stub_pages(monkeypatch, [page([trade(1, T0 + 5_000), trade(2, T0 + 1_000)])])
    tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg, at_ms=T0 + 10_000)
    before = tape.record_of(Chain.SOL, MINT, tmp_db)

    stub_pages(monkeypatch, [full_page_back_to(3_000, T0 + 290_000, T0 + 4_000)])
    after, _ = tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg, at_ms=T0 + 330_000)

    assert before is not None
    assert after.covered_from_ms == before.covered_from_ms
    assert after.created_ms == before.created_ms
    assert after.coverage == tape.COMPLETE
    assert after.covered_to_ms is not None and before.covered_to_ms is not None
    assert after.covered_to_ms > before.covered_to_ms, "the end of the tape did not move"
    assert tape.is_complete(Chain.SOL, MINT, tmp_db) is True


def test_a_rewalk_that_runs_out_of_budget_cannot_demote_a_proof(tmp_db, monkeypatch, cfg):
    """Absence of evidence, wearing a new costume.

    A top-up that misses its watermark observes a hole and may demote (that is
    ``test_topup_that_misses_its_watermark_is_demoted``). A walk with no watermark to aim
    at that simply runs out of pages has observed nothing about completeness, and writing
    ``partial`` on it would be the 2026-09-20 incident again on a resource where the proof
    cannot be re-earned. Deleting the ``rewalk_did_not_reach_proof`` guard in
    `tape.collect_token` makes this fail.
    """
    _proved_row(tmp_db, covered_to_ms=None)  # proved, but nothing to top up from
    stub_pages(monkeypatch, [full_page(5_000, T0 + 800_000), full_page(6_000, T0 + 600_000)])

    record, _ = tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg, at_ms=T0 + 900_000)

    assert record.coverage == tape.COMPLETE
    assert "rewalk_did_not_reach_proof" in record.reason
    assert tape.is_complete(Chain.SOL, MINT, tmp_db) is True


def test_a_failed_deepening_backs_off_instead_of_being_re_asked(tmp_db, monkeypatch, cfg):
    """Restoring ``None if prior.coverage == COMPLETE`` in `record_failed_attempt` fails here.

    NULL means *eligible now*. Once a complete tape can be a candidate, a proved row whose
    deepening attempt has just been refused would be re-asked on every pass, forever, at
    the provider we were rate-limited off.
    """
    _proved_row(tmp_db)
    assert tape.candidates(Chain.SOL, tmp_db, config=cfg, at_ms=T0 + 60_000) == [MINT]

    stub_pages(monkeypatch, [None])  # the hot window closed mid-pass
    record, _ = tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg, at_ms=T0 + 60_000)

    assert record.coverage == tape.COMPLETE, "silence must not erase the proof"
    assert record.next_attempt_ms is not None
    assert record.next_attempt_ms > T0 + 60_000
    assert tape.candidates(Chain.SOL, tmp_db, config=cfg, at_ms=T0 + 60_001) == []


# --------------------------------------------------------------------------------------
# coverage runs to the moment we looked, not to the last trade we found
# --------------------------------------------------------------------------------------


def test_a_quiet_mint_confirmed_quiet_has_a_deep_tape(tmp_db, monkeypatch, cfg):
    """Three trades in four seconds, then silence, then we ask again at launch+340 s.

    We provably hold every trade in the first five minutes of this mint's life. Scoring
    depth as last-trade-minus-first-trade would call it 4 s deep forever and drop exactly
    the population a rug model is about. Making `tape.observed_to_ms` return ``newest_ms``
    unconditionally makes this fail.
    """
    add_token(tmp_db)
    stub_pages(monkeypatch, [page([trade(3, T0 + 4_000), trade(2, T0 + 2_000), trade(1, T0)])])
    tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg, at_ms=T0 + 20_000)

    stub_pages(monkeypatch, [page([trade(3, T0 + 4_000), trade(2, T0 + 2_000), trade(1, T0)])])
    record, _ = tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg, at_ms=T0 + 340_000)

    assert record.newest_ms == T0 + 4_000, "no new trades arrived, and none were invented"
    assert record.depth_ms is not None and record.depth_ms >= cfg.deep_enough_ms
    assert record.deep_enough(cfg) is True
    assert tape.depth_histogram(Chain.SOL, tmp_db)["300s"] == 1


def test_a_partial_walk_never_claims_coverage_to_the_moment_it_looked():
    """The gate inside `observed_to_ms`. A hole somewhere means the claim spans the hole."""
    assert tape.observed_to_ms(tape.PARTIAL, T0 + 900_000, T0 + 5_000) == T0 + 5_000
    assert tape.observed_to_ms(tape.UNAVAILABLE, T0 + 900_000, None) is None
    assert tape.observed_to_ms(tape.COMPLETE, T0 + 900_000, T0 + 5_000) == T0 + 900_000
    assert tape.observed_to_ms(tape.COMPLETE, None, T0 + 5_000) == T0 + 5_000


def test_depth_is_none_not_zero_when_we_cannot_establish_it():
    """Missing data is None with an UNAVAILABLE basis, never 0. A 0 here sorts as shallow."""
    unproved = tape.TapeRecord(
        chain=Chain.SOL, token=MINT, coverage=tape.PARTIAL, route=tape.ROUTE_TRADES,
        reason="x", covered_to_ms=T0 + 400_000, created_ms=T0,
    )
    assert unproved.depth_ms is None
    assert unproved.deep_enough() is False

    no_watermark = tape.TapeRecord(
        chain=Chain.SOL, token=MINT, coverage=tape.COMPLETE, route=tape.ROUTE_TRADES,
        reason="x", proof="end_of_history", covered_from_ms=T0 - 1, created_ms=T0,
    )
    assert no_watermark.proved is True
    assert no_watermark.depth_ms is None
    assert no_watermark.deep_enough() is False


def test_depth_histogram_counts_only_proved_tapes(tmp_db):
    """A deep *partial* tape is not a deep tape: nobody can say what is missing from it."""
    add_token(tmp_db, MINT)
    add_token(tmp_db, OTHER)
    _raw_insert(tmp_db, token=MINT, covered_to_ms=T0 + 400_000)
    _raw_insert(tmp_db, token=OTHER, coverage=tape.PARTIAL, proof=None, covered_from_ms=None,
                covered_to_ms=T0 + 900_000)

    assert tape.depth_histogram(Chain.SOL, tmp_db) == {"60s": 1, "120s": 1, "300s": 1}


# --------------------------------------------------------------------------------------
# the scheduled return visit
# --------------------------------------------------------------------------------------


def test_one_return_visit_is_scheduled_just_after_the_five_minute_mark(cfg):
    assert tape.next_deepen_attempt(T0, T0 + 30_000, cfg) == (
        T0 + cfg.deep_enough_ms + cfg.deepen_settle_ms
    )


def test_no_return_visit_is_scheduled_for_a_tape_that_is_already_deep(cfg):
    assert tape.next_deepen_attempt(T0, T0 + cfg.deep_enough_ms, cfg) is None
    assert tape.next_deepen_attempt(None, T0 + 10, cfg) is None
    assert tape.next_deepen_attempt(T0, None, cfg) is None


def test_the_return_visit_is_actually_written_on_a_fresh_capture(tmp_db, monkeypatch, cfg):
    """Otherwise the timer is a function nobody calls."""
    add_token(tmp_db)
    stub_pages(monkeypatch, [page([trade(1, T0 + 5_000)])])

    record, _ = tape.collect_token(Chain.SOL, MINT, tmp_db, config=cfg, at_ms=T0 + 20_000)

    assert record.coverage == tape.COMPLETE
    assert record.next_attempt_ms == T0 + cfg.deep_enough_ms + cfg.deepen_settle_ms
    assert tape.candidates(Chain.SOL, tmp_db, config=cfg, at_ms=T0 + 60_000) == [], "too early"
    assert tape.candidates(Chain.SOL, tmp_db, config=cfg, at_ms=T0 + 340_000) == [MINT]


def test_the_return_visit_is_written_on_the_scan_path_too(tmp_db, cfg):
    """The scanner is where the overwhelming majority of tapes are born."""
    add_token(tmp_db)
    add_swap(tmp_db, tx="routerow", ts_ms=T0 + 5_000, source=TF.SOURCE)

    record = tape.record_scan_capture(
        Chain.SOL, MINT,
        {"flow_reason": "end_of_history", "flow_pages": 1, "observed_ms": T0 + 20_000},
        tmp_db, at_ms=T0 + 20_000, config=cfg,
    )

    assert record is not None and record.coverage == tape.COMPLETE
    assert record.covered_to_ms == T0 + 20_000, "coverage runs to the observation, not the trade"
    assert record.next_attempt_ms == T0 + cfg.deep_enough_ms + cfg.deepen_settle_ms


# --------------------------------------------------------------------------------------
# reconcile: what the database already knew and never wrote down
# --------------------------------------------------------------------------------------


def _snap_at(
    conn: sqlite3.Connection, token: str, observed_ms: int, coverage_from_ms: int | None
) -> None:
    conn.execute(
        "INSERT INTO curve_snapshots "
        "(chain, token, observed_ms, real_sol_lamports, virtual_sol_lamports, real_token_atoms, "
        " virtual_token_atoms, coverage_from_ms, created_ms, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("sol", token, observed_ms, 1_000_000, 30_000_000_000, "793100000000000",
         "1073000000000000", coverage_from_ms, T0, "pumpfun"),
    )


def test_advance_watermark_bridges_a_chain_and_refuses_a_gap():
    """Pure. Each observation extends coverage only if it reached back to where ours ended."""
    chain = [(T0 + 100_000, T0 - 1), (T0 + 200_000, T0 + 50_000)]
    assert tape.advance_watermark(T0 + 60_000, chain) == (T0 + 200_000, 2)

    # The second walk started after our coverage ended: there is a hole between them.
    gapped = [(T0 + 100_000, T0 - 1), (T0 + 200_000, T0 + 150_000)]
    assert tape.advance_watermark(T0 + 60_000, gapped) == (T0 + 100_000, 1)

    # A walk that was never run says nothing, and does not break what came after it.
    absent = [(T0 + 100_000, None), (T0 + 200_000, T0 + 50_000)]
    assert tape.advance_watermark(T0 + 60_000, absent) == (T0 + 200_000, 1)

    assert tape.advance_watermark(None, chain) == (None, 0)


def test_reconcile_advances_a_stale_watermark_with_no_provider_call(tmp_db):
    """3,035 of 4,074 proved rows on the live database could do this for free."""
    _proved_row(tmp_db, covered_to_ms=T0 + 20_000)
    _snap_at(tmp_db, MINT, observed_ms=T0 + 330_000, coverage_from_ms=T0 + 10_000)

    report = tape.reconcile(Chain.SOL, tmp_db, at_ms=T0 + 400_000)

    record = tape.record_of(Chain.SOL, MINT, tmp_db)
    assert report.watermarks_advanced == 1
    assert record is not None and record.covered_to_ms == T0 + 330_000
    assert record.deep_enough() is True
    assert report.depth_before["300s"] == 0 and report.depth_after["300s"] == 1


def test_reconcile_will_not_bridge_a_snapshot_gap(tmp_db):
    """Dropping the ``coverage_from_ms > watermark`` refusal in `advance_watermark` fails here.

    The snapshot's walk started 100 s after our coverage ended, so the rows between are
    missing and the watermark may not jump across them.
    """
    _proved_row(tmp_db, covered_to_ms=T0 + 20_000)
    _snap_at(tmp_db, MINT, observed_ms=T0 + 330_000, coverage_from_ms=T0 + 120_000)

    report = tape.reconcile(Chain.SOL, tmp_db, at_ms=T0 + 400_000)

    record = tape.record_of(Chain.SOL, MINT, tmp_db)
    assert report.watermarks_advanced == 0
    assert record is not None and record.covered_to_ms == T0 + 20_000


def test_reconcile_never_advances_an_unproved_row(tmp_db):
    """`covered_to_ms` on a partial row is not a coverage claim and must not become one."""
    add_token(tmp_db, MINT)
    _raw_insert(tmp_db, coverage=tape.PARTIAL, proof=None, covered_from_ms=None,
                covered_to_ms=T0 + 20_000)
    _snap_at(tmp_db, MINT, observed_ms=T0 + 330_000, coverage_from_ms=T0 + 10_000)

    tape.reconcile(Chain.SOL, tmp_db, at_ms=T0 + 400_000)

    record = tape.record_of(Chain.SOL, MINT, tmp_db)
    assert record is not None and record.covered_to_ms == T0 + 20_000


def test_reconcile_never_moves_the_start_of_the_tape(tmp_db):
    _proved_row(tmp_db, covered_to_ms=T0 + 20_000)
    _snap_at(tmp_db, MINT, observed_ms=T0 + 330_000, coverage_from_ms=T0 + 10_000)
    before = tape.record_of(Chain.SOL, MINT, tmp_db)

    tape.reconcile(Chain.SOL, tmp_db, at_ms=T0 + 400_000)
    after = tape.record_of(Chain.SOL, MINT, tmp_db)

    assert before is not None and after is not None
    assert after.covered_from_ms == before.covered_from_ms
    assert after.created_ms == before.created_ms
    assert after.proof == before.proof
    assert after.coverage == before.coverage


def test_reconcile_refreshes_row_counts_and_stamps_updated_ms(tmp_db):
    """4,043 of 4,071 rows had ``updated_ms == first_seen_ms``: written once, never touched.

    `kaiba.ops.scheduler.job_token_flow` writes trades straight into `swaps` and tells this
    table nothing, so `newest_ms` described the mint as it had been at first sight -- on
    1,205 rows, a median of 283 s behind. Nothing could tell a stale tape from a fresh one
    because nothing was writing the field that would have said.
    """
    _proved_row(tmp_db, covered_to_ms=T0 + 20_000)
    tmp_db.execute("UPDATE token_tape SET newest_ms=?, swaps_total=1, swaps_route=1", (T0 + 5_000,))
    add_swap(tmp_db, tx="late1", ts_ms=T0 + 200_000, source=TF.SOURCE)
    add_swap(tmp_db, tx="late2", ts_ms=T0 + 250_000, source=TF.SOURCE)

    report = tape.reconcile(Chain.SOL, tmp_db, at_ms=T0 + 400_000)

    row = fetch_one(tmp_db, "SELECT * FROM token_tape WHERE token=?", (MINT,))
    assert report.counts_refreshed == 1
    assert row["newest_ms"] == T0 + 250_000
    assert row["swaps_route"] == 2
    assert row["updated_ms"] != row["first_seen_ms"], "a refreshed row must stop looking untouched"


def test_reconcile_is_idempotent(tmp_db):
    _proved_row(tmp_db, covered_to_ms=T0 + 20_000)
    _snap_at(tmp_db, MINT, observed_ms=T0 + 330_000, coverage_from_ms=T0 + 10_000)

    assert tape.reconcile(Chain.SOL, tmp_db, at_ms=T0 + 400_000).rows_written == 1
    assert tape.reconcile(Chain.SOL, tmp_db, at_ms=T0 + 400_001).rows_written == 0


def test_run_reports_depth_before_and_after(tmp_db, monkeypatch, cfg):
    """Completeness counts cannot show a deepening pass working: everything was complete."""
    _proved_row(tmp_db, covered_to_ms=T0 + 20_000)
    _snap_at(tmp_db, MINT, observed_ms=T0 + 330_000, coverage_from_ms=T0 + 10_000)
    stub_pages(monkeypatch, [None])

    report = tape.run(Chain.SOL, tmp_db, config=cfg)

    assert report.complete_before == report.complete_after == 1
    assert report.depth_before["300s"] == 1, "reconcile runs before the baseline is taken"
    assert report.reconciled == 1
    assert "depth_after" in report.as_dict()


# --------------------------------------------------------------------------------------
# provenance: a threshold with no stated origin is a number someone will defend later
# --------------------------------------------------------------------------------------


def test_every_config_knob_has_provenance():
    """Adding a field to `TapeConfig` without a `PROVENANCE` entry fails here, and vice versa."""
    from dataclasses import fields

    assert {f.name for f in fields(tape.TapeConfig)} == set(tape.PROVENANCE)


def test_provenance_entries_state_a_recognised_basis_and_real_evidence():
    allowed = {"MEASURED", "PUBLISHED", "REQUIREMENT", "INVENTED"}
    for name, knob in tape.PROVENANCE.items():
        assert knob.basis in allowed, f"{name} claims an unknown basis {knob.basis!r}"
        assert len(knob.evidence) > 40, f"{name} has no real evidence line"


def test_no_numeric_module_constant_escapes_documentation():
    """A new bare number at module scope has to be added here deliberately.

    The point is not the list, it is that a knob cannot arrive silently: anyone adding a
    threshold to this module has to come here and say what it is.
    """
    numeric = {
        name for name, value in vars(tape).items()
        if not name.startswith("_")
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    }
    assert numeric == {
        "HOT_WINDOW_ANSWERED_MAX_IDLE_MIN",
        "HOT_WINDOW_REFUSED_MIN_IDLE_MIN",
        "HOT_WINDOW_REFUSED_2026_09_21_MIN_IDLE_MIN",
    }


def test_the_deepening_horizon_is_the_published_one():
    """300 s is not a tuning choice; it is what arXiv 2608.20271 states its result over."""
    assert tape.DEFAULT_CONFIG.deep_enough_ms == 300_000
    assert tape.PROVENANCE["deep_enough_ms"].basis == "REQUIREMENT"
    assert tape.PROVENANCE["deepen_max_idle_ms"].basis == "MEASURED"
    assert tape.DEFAULT_CONFIG.deepen_max_idle_ms == int(
        tape.HOT_WINDOW_ANSWERED_MAX_IDLE_MIN * 60_000
    ), "the deepening re-ask must use the PROVED-ANSWERING edge, not the bracket"
