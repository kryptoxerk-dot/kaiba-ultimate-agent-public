"""StonkFun (Raydium LaunchLab) ingest — the second venue.

Six of these carry weight well beyond coverage, and every one of them exists because the
failure it describes produces a *plausible number* rather than an error:

* ``test_amount_native_is_none_when_the_quote_is_not_sol`` — the venue-shaped bug. 93% of
  StonkFun does not trade against SOL, and ``swaps.amount_native`` is a lamports column
  that four separate consumers divide by 1e9 and call SOL. A STONK amount written there
  reads as 26,000 SOL; a 4-decimal quote reads as 0.0000001. Neither looks wrong.
* ``test_a_fractional_base_unit_is_refused_not_truncated`` — the route reports amounts
  already scaled by the mint's decimals, so a wrong exponent truncates into a plausible
  amount instead of failing.
* ``test_a_failed_observation_never_downgrades_a_proof`` — inherited from tape.py rather
  than rediscovered. Five proved tapes were overwritten with ``unavailable`` by failed
  re-scans on 2026-09-20 and could not be re-earned.
* ``test_a_partial_tape_is_indistinguishable_from_no_tape`` — a half-walked tape has a
  bundle share of 0% by construction, which opens every gate that fails closed on unknown.
* ``test_proved_predicate_matches_tape`` — the drift detector. This module carries a copy
  of ``tape.TapeRecord.proved`` only because that one hard-codes the pump.fun route; the
  copy has to be provably the same predicate or "complete" quietly means two things.
* ``test_a_non_stonkfun_platform_config_is_refused`` — the venue test. The whole point of
  this module is a *second* sample; a third launchpad on the same program leaking into it
  would be worse than having no second venue.

Nothing here touches the network. Fixtures under ``tests/fixtures/stonkfun/`` are real
responses recorded from ``launch-mint-v1.raydium.io`` and ``launch-history-v1.raydium.io``
on 2026-09-21, saved as raw bytes so no amount round-tripped through a float on the way in.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import Chain, EvidenceBasis, Receipt, now_ms
from kaiba.ingest import stonkfun as SF
from kaiba.ingest import tape

FIXTURES = Path(__file__).parent / "fixtures" / "stonkfun"

#: Real mints from the recorded launch lookup. The first quotes in wrapped SOL, the
#: second in STONK — the two halves of this venue.
SOL_MINT = "BkpyJeuEK8rRHXCbsyJ4mRAbbFdRYX4oDSiUUszDrqC5"
SOL_POOL = "4Jv2C9HxEX6k9QfVSqauZw2792XEiiM69df7GnMBQy7q"
STONK_MINT = "5khnYCzMZFNq6q5DkR9kmTBUEK2DqJvviTeV7pzeUFD6"
STONK_POOL = "D4Ec8zwFgASgd8ArncDrAc1dVvy9QWnKv4st4gZaPt5o"
STONK_QUOTE = "6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx"
WSOL = "So11111111111111111111111111111111111111112"


def fixture(name: str) -> dict[str, Any]:
    """Load a recorded response with the same Decimal parsing ``_http`` applies."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"), parse_float=Decimal)


def launch_rows() -> list[dict[str, Any]]:
    return list(fixture("launch_by_mints.json")["data"]["rows"])


def raw_launch(mint: str) -> dict[str, Any]:
    for row in launch_rows():
        if row["mint"] == mint:
            return copy.deepcopy(row)
    raise AssertionError(f"{mint} not in the recorded fixture")


def launch(mint: str) -> SF.Launch:
    parsed = SF.parse_launch(raw_launch(mint))
    assert parsed is not None
    return parsed


def receipt(basis: EvidenceBasis = EvidenceBasis.PROVIDER_REPORTED) -> Receipt:
    return Receipt(provider=SF.PROVIDER, endpoint=SF.TRADES_ENDPOINT, basis=basis)


def stub_pages(monkeypatch, pages: list[dict[str, Any] | None], calls: list[Any] | None = None):
    """Serve ``pages`` in order to :func:`fetch_trades_page`; ``None`` means unavailable."""
    state = {"i": 0}

    def _fetch(pool: str, *, page_key: str | None = None, **kw: Any):
        idx = min(state["i"], len(pages) - 1)
        state["i"] += 1
        if calls is not None:
            calls.append((pool, page_key))
        page = pages[idx]
        if page is None:
            return None, receipt(EvidenceBasis.UNAVAILABLE)
        return page, receipt()

    monkeypatch.setattr(SF, "fetch_trades_page", _fetch)
    return state


def trade(
    tx: str,
    block_time: int,
    *,
    pool: str = STONK_POOL,
    side: str = "buy",
    owner: str = "H6PjRLnqGPz5aQe3zCKZ1TekDLVWMCnyLpGfDqQzCYQx",
    amount_a: str = "1.0",
    amount_b: str = "2.0",
) -> dict[str, Any]:
    return {
        "txid": tx,
        "owner": owner,
        "blockTime": block_time,
        "poolId": pool,
        "side": side,
        "amountA": Decimal(amount_a),
        "amountB": Decimal(amount_b),
    }


def page(rows: list[dict[str, Any]], next_key: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {"rows": rows}
    if next_key is not None:
        out["nextPageKey"] = next_key
    return out


# --------------------------------------------------------------------------------------
# the launch index
# --------------------------------------------------------------------------------------


def test_parse_launch_reads_a_real_row() -> None:
    sol = launch(SOL_MINT)
    assert sol.pool == SOL_POOL
    assert sol.platform_config in SF.PLATFORM_CONFIGS
    assert sol.platform_kind == "standard"
    assert sol.base_decimals == 6
    assert sol.quote_mint == WSOL
    assert sol.quote_decimals == 9
    assert sol.quote_is_sol is True
    assert sol.total_base_sell == SF.OBSERVED_TOTAL_BASE_SELL
    assert sol.graduation_quote == 85_000_000_000
    assert sol.migrate_type == "cpmm"
    assert sol.created_ms == 1_789_965_203_000

    stonk = launch(STONK_MINT)
    assert stonk.platform_kind == "reward"
    assert stonk.quote_mint == STONK_QUOTE
    assert stonk.quote_is_sol is False
    # CHAIN-VERIFIED class of fact: a Token-2022 transfer fee on the base mint, so the
    # curve-side and wallet-side base amounts differ by 1%.
    assert stonk.transfer_fee_bps == 100
    # Two launches, two graduation targets, ten orders of magnitude apart across the
    # population. Nothing here may be hard-coded.
    assert stonk.graduation_quote != sol.graduation_quote


def test_a_non_stonkfun_platform_config_is_refused() -> None:
    """The venue test. LaunchLab hosts other launchpads and they are not our second sample."""
    row = raw_launch(SOL_MINT)
    row["platformInfo"]["pubKey"] = "LetsBonkfun11111111111111111111111111111111"
    assert SF.parse_launch(row) is None

    row = raw_launch(SOL_MINT)
    row.pop("platformInfo")
    assert SF.parse_launch(row) is None


def test_a_launch_without_a_quote_exponent_is_refused() -> None:
    """501 distinct quote mints at 4 to 12 decimals: there is no default to fall back on."""
    row = raw_launch(STONK_MINT)
    row["mintB"].pop("decimals")
    assert SF.parse_launch(row) is None

    row = raw_launch(STONK_MINT)
    row["mintB"].pop("address")
    assert SF.parse_launch(row) is None


def test_a_launch_without_base_decimals_is_refused() -> None:
    row = raw_launch(STONK_MINT)
    row["decimals"] = None
    assert SF.parse_launch(row) is None
    row["decimals"] = 99
    assert SF.parse_launch(row) is None


def test_every_row_of_the_recorded_list_page_parses() -> None:
    rows = fixture("launch_list.json")["data"]["rows"]
    assert rows, "the recorded list page is empty"
    parsed = [SF.parse_launch(r) for r in rows]
    assert all(p is not None for p in parsed)
    assert {p.platform_kind for p in parsed} <= {"reward", "standard"}


def test_record_launch_writes_the_token_and_its_units(tmp_db) -> None:
    sol = launch(SOL_MINT)
    assert SF.record_launch(sol, tmp_db) is True          # first sighting
    assert SF.record_launch(sol, tmp_db) is False         # already emitted

    row = fetch_one(
        tmp_db, "SELECT * FROM tokens WHERE chain=? AND address=?", (Chain.SOL.value, SOL_MINT)
    )
    assert row["launchpad"] == SF.LAUNCHPAD
    assert row["decimals"] == 6
    assert row["pool"] == SOL_POOL
    assert row["created_ms"] == sol.created_ms
    meta = json.loads(row["meta_json"])
    assert meta["quote_mint"] == WSOL
    assert meta["program"] == SF.LAUNCHLAB_PROGRAM

    stored = SF.launch_of(Chain.SOL, SOL_MINT, tmp_db)
    assert stored["quote_decimals"] == 9
    assert stored["platform_config"] == sol.platform_config
    # migrated_ms is never stamped from an observation: see migration 027.
    assert row["migrated_ms"] is None


def test_launch_from_row_round_trips_the_units(tmp_db) -> None:
    original = launch(STONK_MINT)
    SF.record_launch(original, tmp_db)
    rebuilt = SF.launch_from_row(SF.launch_of(Chain.SOL, STONK_MINT, tmp_db))
    assert rebuilt is not None
    for attr in ("token", "pool", "platform_config", "base_decimals", "quote_mint",
                 "quote_decimals", "transfer_fee_bps", "created_ms", "graduation_quote"):
        assert getattr(rebuilt, attr) == getattr(original, attr), attr


def test_ingest_launches_records_both_platform_configs(tmp_db, monkeypatch) -> None:
    rows = launch_rows()
    served: list[str] = []

    def _fetch(platform_config: str, *, page_id: str | None = None, **kw: Any):
        served.append(platform_config)
        wanted = SF.PLATFORM_CONFIGS[platform_config]
        mine = [r for r in rows
                if SF.PLATFORM_CONFIGS.get(r["platformInfo"]["pubKey"]) == wanted]
        return {"rows": mine, "nextPageId": None}, receipt()

    monkeypatch.setattr(SF, "fetch_launch_page", _fetch)
    report = SF.ingest_launches(tmp_db)
    # Both configs are polled. MEASURED 5.7 launches/min on one and 1.2/min on the other;
    # polling only the busy one silently drops a sixth of the venue.
    assert set(served) == set(SF.PLATFORM_CONFIGS)
    assert report.rows_seen == 2
    assert report.tokens_new == 2
    assert report.rows_rejected == 0
    known = {r["address"] for r in fetch_all(tmp_db, "SELECT address FROM tokens", ())}
    assert known == {SOL_MINT, STONK_MINT}


def test_ingest_launches_rejects_a_foreign_launchpad(tmp_db, monkeypatch) -> None:
    foreign = raw_launch(SOL_MINT)
    foreign["platformInfo"]["pubKey"] = "SomeOtherLaunchpadConfig1111111111111111111"
    monkeypatch.setattr(
        SF, "fetch_launch_page",
        lambda pc, **kw: ({"rows": [foreign], "nextPageId": None}, receipt()),
    )
    report = SF.ingest_launches(tmp_db)
    assert report.rows_seen == 0
    assert report.rows_rejected == 2  # one per platform config polled
    assert fetch_all(tmp_db, "SELECT address FROM tokens", ()) == []


# --------------------------------------------------------------------------------------
# parsing a trade
# --------------------------------------------------------------------------------------


def test_parse_trade_converts_a_real_row_to_exact_base_units() -> None:
    sol = launch(SOL_MINT)
    raw = fixture("trades_sol_last_page.json")["data"]["rows"][0]
    row = SF.parse_trade(raw, sol)
    assert row is not None
    # amountA 94353.741243 at 6 decimals, amountB 0.002679242 at 9. Exact, no float.
    assert row.amount_token == 94_353_741_243
    assert row.amount_quote == 2_679_242
    assert isinstance(row.amount_token, int) and isinstance(row.amount_quote, int)
    assert row.ts_ms == raw["blockTime"] * 1000
    assert row.side in {"buy", "sell"}
    assert row.token == SOL_MINT


def test_a_fractional_base_unit_is_refused_not_truncated() -> None:
    """A wrong exponent truncates into a plausible amount. Refuse instead."""
    stonk = launch(STONK_MINT)
    row = SF.parse_trade(trade("sig1", 1_700_000_000, amount_a="1.0000001"), stonk)
    assert row is not None
    assert row.amount_token is None          # 7 decimal places on a 6-decimal mint
    assert row.amount_quote == 2_000_000_000  # the quote leg is still good
    # And the row is still written: half a trade recorded honestly beats none recorded.
    assert row.tx == "sig1"


def test_amount_native_is_none_when_the_quote_is_not_sol() -> None:
    """The venue-shaped bug. NULL, never 0, and never the quote amount in disguise."""
    stonk = launch(STONK_MINT)
    row = SF.parse_trade(trade("sig1", 1_700_000_000, amount_a="1.0", amount_b="26516.0"), stonk)
    assert row is not None
    assert row.amount_quote == 26_516_000_000_000
    assert row.amount_native is None
    assert row.amount_native != 0
    assert row.quote_mint == STONK_QUOTE


def test_amount_native_is_lamports_when_the_quote_is_wrapped_sol() -> None:
    sol = launch(SOL_MINT)
    row = SF.parse_trade(
        trade("sig1", 1_700_000_000, pool=SOL_POOL, amount_b="1.5"), sol
    )
    assert row is not None
    assert row.amount_native == 1_500_000_000
    assert row.amount_quote == row.amount_native   # WSOL base units are lamports
    assert row.quote_mint == WSOL


def test_usd_is_never_invented() -> None:
    """The route reports no USD and the quote is a token. A peg is not a measurement."""
    for mint in (SOL_MINT, STONK_MINT):
        current = launch(mint)
        row = SF.parse_trade(trade("sig1", 1_700_000_000, pool=current.pool), current)
        assert row is not None
        params = row.as_params()
        assert params[10] is None, "price_usd must stay unknown"
        assert params[11] is None, "usd_value must stay unknown"


def test_fee_payer_is_never_the_trader() -> None:
    """CHAIN-VERIFIED: `owner` is the token-account owner and is not always the signer."""
    stonk = launch(STONK_MINT)
    row = SF.parse_trade(trade("sig1", 1_700_000_000, owner="TraderWallet11111111111111"), stonk)
    assert row is not None
    assert row.wallet == "TraderWallet11111111111111"
    assert row.as_params()[15] is None, "fee_payer must not be guessed from the trader"
    assert "fee payer is account index 0" in SF.FEE_PAYER_NOTE


def test_a_trade_from_another_pool_is_refused() -> None:
    """Matching a tape to the wrong mint corrupts a whole token, not one row of it."""
    stonk = launch(STONK_MINT)
    assert SF.parse_trade(trade("sig1", 1_700_000_000, pool=SOL_POOL), stonk) is None


def test_a_malformed_trade_is_refused() -> None:
    stonk = launch(STONK_MINT)
    for mutate in (
        {"txid": ""},
        {"owner": None},
        {"side": "transfer"},
        {"blockTime": None},
    ):
        raw = trade("sig1", 1_700_000_000)
        raw.update(mutate)
        assert SF.parse_trade(raw, stonk) is None, mutate


# --------------------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------------------


def test_write_trades_is_idempotent(tmp_db) -> None:
    """The collector runs on every pass; a non-idempotent write inflates every count."""
    stonk = launch(STONK_MINT)
    rows = [SF.parse_trade(trade(f"sig{i}", 1_700_000_000 + i), stonk) for i in range(5)]
    assert all(r is not None for r in rows)
    assert SF.write_trades(tmp_db, rows) == (5, 0)
    assert SF.write_trades(tmp_db, rows) == (0, 5)
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM swaps", ())["n"] == 5


def test_write_trades_records_the_quote_leg(tmp_db) -> None:
    stonk = launch(STONK_MINT)
    row = SF.parse_trade(trade("sig1", 1_700_000_000, amount_b="26516.0"), stonk)
    SF.write_trades(tmp_db, [row])
    stored = fetch_one(tmp_db, "SELECT * FROM swaps WHERE tx=?", ("sig1",))
    assert stored["source"] == SF.SOURCE
    assert stored["program"] == SF.VENUE
    assert stored["amount_quote"] == "26516000000000"
    assert stored["quote_mint"] == STONK_QUOTE
    assert stored["amount_native"] is None
    assert stored["usd_value"] is None
    assert stored["slot"] is None, "the route reports no slot; an invented one fakes ordering"


def test_a_row_with_unresolvable_atoms_is_not_duplicated(tmp_db) -> None:
    """SQLite treats NULLs as distinct, so the UNIQUE alone would let this re-insert."""
    stonk = launch(STONK_MINT)
    row = SF.parse_trade(trade("sig1", 1_700_000_000, amount_a="1.0000001"), stonk)
    assert row.amount_token is None
    assert SF.write_trades(tmp_db, [row]) == (1, 0)
    assert SF.write_trades(tmp_db, [row]) == (0, 1)
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM swaps", ())["n"] == 1


# --------------------------------------------------------------------------------------
# walking the tape
# --------------------------------------------------------------------------------------


def test_collect_trades_walks_to_end_of_history(tmp_db, monkeypatch) -> None:
    stonk = launch(STONK_MINT)
    SF.record_launch(stonk, tmp_db)
    limit = SF.DEFAULT_CONFIG.trade_page_limit
    first = [trade(f"a{i}", 1_800_000_000 - i) for i in range(limit)]
    last = [trade(f"b{i}", 1_790_000_000 - i) for i in range(7)]
    calls: list[Any] = []
    stub_pages(monkeypatch, [page(first, "cursor-1"), page(last, None)], calls)

    flow = SF.collect_trades(stonk, tmp_db)
    assert flow.reason == "end_of_history"
    assert flow.complete is True
    assert flow.pages == 2
    assert flow.trades_seen == limit + 7
    assert flow.rows_written == limit + 7
    assert calls == [(STONK_POOL, None), (STONK_POOL, "cursor-1")]


def test_a_full_page_with_no_cursor_is_the_end_of_history(tmp_db, monkeypatch) -> None:
    """MEASURED edge: a pool whose trade count is an exact multiple of the page size.

    Treating only a short page as terminal would leave it forever `partial` and re-walked
    on every pass, which is both a permanent false negative and a standing waste of a free
    endpoint's capacity.
    """
    stonk = launch(STONK_MINT)
    SF.record_launch(stonk, tmp_db)
    full = [trade(f"a{i}", 1_800_000_000 - i) for i in range(SF.DEFAULT_CONFIG.trade_page_limit)]
    stub_pages(monkeypatch, [page(full, None)])
    flow = SF.collect_trades(stonk, tmp_db)
    assert flow.reason == "end_of_history"
    assert flow.pages == 1


def test_collect_trades_stops_at_the_watermark(tmp_db, monkeypatch) -> None:
    stonk = launch(STONK_MINT)
    SF.record_launch(stonk, tmp_db)
    limit = SF.DEFAULT_CONFIG.trade_page_limit
    rows = [trade(f"a{i}", 1_800_000_000 - i) for i in range(limit)]
    stub_pages(monkeypatch, [page(rows, "cursor-1"), page(rows, "cursor-2")])
    flow = SF.collect_trades(stonk, tmp_db, since_ms=1_800_000_000_000)
    assert flow.reason == "reached_watermark"
    assert flow.complete is True
    assert flow.pages == 1


def test_a_page_budget_is_partial_never_complete(tmp_db, monkeypatch) -> None:
    stonk = launch(STONK_MINT)
    SF.record_launch(stonk, tmp_db)
    limit = SF.DEFAULT_CONFIG.trade_page_limit
    rows = [trade(f"a{i}", 1_800_000_000 - i) for i in range(limit)]
    stub_pages(monkeypatch, [page(rows, "cursor-1")] * 5)
    flow = SF.collect_trades(stonk, tmp_db, max_pages=2)
    assert flow.reason == "page_budget_exhausted"
    assert flow.complete is False
    assert flow.coverage_from_ms is not None
    # Coverage reaches back only as far as we actually walked, never to the launch.
    assert flow.coverage_from_ms > (stonk.created_ms or 0)


def test_an_unavailable_first_page_writes_nothing(tmp_db, monkeypatch) -> None:
    stonk = launch(STONK_MINT)
    SF.record_launch(stonk, tmp_db)
    stub_pages(monkeypatch, [None])
    flow = SF.collect_trades(stonk, tmp_db)
    assert flow.reason == "unavailable"
    assert flow.complete is False
    assert flow.rows_written == 0
    assert flow.coverage_from_ms is None, "coverage must be unknown, not zero"
    assert fetch_all(tmp_db, "SELECT 1 FROM swaps", ()) == []


def test_a_cursor_is_exclusive_so_a_repeated_row_is_not_double_counted(
    tmp_db, monkeypatch
) -> None:
    stonk = launch(STONK_MINT)
    SF.record_launch(stonk, tmp_db)
    limit = SF.DEFAULT_CONFIG.trade_page_limit
    rows = [trade(f"a{i}", 1_800_000_000 - i) for i in range(limit)]
    # The same page served twice: a provider hiccup, not 200 distinct trades.
    stub_pages(monkeypatch, [page(rows, "cursor-1"), page(rows, None)])
    flow = SF.collect_trades(stonk, tmp_db)
    assert flow.trades_seen == limit
    assert flow.rows_written == limit


# --------------------------------------------------------------------------------------
# coverage
# --------------------------------------------------------------------------------------


def prove_tape(tmp_db, monkeypatch, mint: str = STONK_MINT) -> tape.TapeRecord:
    current = launch(mint)
    SF.record_launch(current, tmp_db)
    created_s = (current.created_ms or 0) // 1000
    rows = [trade(f"t{i}", created_s + 10 - i, pool=current.pool) for i in range(4)]
    stub_pages(monkeypatch, [page(rows, None)])
    record, _flow = SF.collect_token(Chain.SOL, mint, tmp_db)
    return record


def test_collect_token_proves_a_complete_tape(tmp_db, monkeypatch) -> None:
    record = prove_tape(tmp_db, monkeypatch)
    assert record.coverage == tape.COMPLETE
    assert record.route == SF.ROUTE
    assert record.proof == tape.REASON_END_OF_HISTORY
    assert record.covered_from_ms is not None
    assert record.created_ms is not None
    assert record.covered_from_ms <= record.created_ms
    assert SF.is_complete(Chain.SOL, STONK_MINT, tmp_db) is True
    assert SF.complete_tokens(Chain.SOL, tmp_db) == [STONK_MINT]


def test_a_token_with_no_launch_record_is_unavailable_without_a_request(
    tmp_db, monkeypatch
) -> None:
    """Both exponents come from the launch record. Without it every amount is a guess."""
    calls: list[Any] = []
    stub_pages(monkeypatch, [page([], None)], calls)
    record, flow = SF.collect_token(Chain.SOL, "NeverSeenMint1111111111111", tmp_db)
    assert record.coverage == tape.UNAVAILABLE
    assert "no_stonkfun_launch_record" in record.reason
    assert flow is None
    assert calls == [], "a refusal must not spend a request"
    assert SF.is_complete(Chain.SOL, "NeverSeenMint1111111111111", tmp_db) is False


def test_a_partial_tape_is_indistinguishable_from_no_tape(tmp_db, monkeypatch) -> None:
    """A half-walked tape's bundle share is 0% by construction. It must not read as data."""
    current = launch(STONK_MINT)
    SF.record_launch(current, tmp_db)
    limit = SF.DEFAULT_CONFIG.trade_page_limit
    rows = [trade(f"a{i}", 1_800_000_000 - i) for i in range(limit)]
    stub_pages(monkeypatch, [page(rows, "cursor")] * 40)
    record, _flow = SF.collect_token(Chain.SOL, STONK_MINT, tmp_db)
    assert record.coverage == tape.PARTIAL
    ok, reason = SF.completeness(Chain.SOL, STONK_MINT, tmp_db)
    assert ok is False
    assert reason.startswith("partial:")
    assert SF.complete_tokens(Chain.SOL, tmp_db) == []
    # Rows were still written; "partial" is a statement about coverage, not about data.
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM swaps", ())["n"] == limit


def test_a_failed_observation_never_downgrades_a_proof(tmp_db, monkeypatch) -> None:
    """Inherited from tape.py: a 503 says nothing about what we saw while it answered."""
    proved = prove_tape(tmp_db, monkeypatch)
    assert proved.coverage == tape.COMPLETE

    stub_pages(monkeypatch, [None])
    after, flow = SF.collect_token(Chain.SOL, STONK_MINT, tmp_db)
    assert flow.reason in tape.NON_OBSERVATIONS
    assert after.coverage == tape.COMPLETE
    assert after.proof == proved.proof
    assert after.covered_from_ms == proved.covered_from_ms
    assert after.attempts == proved.attempts + 1
    assert "failed attempt" in after.reason
    assert SF.is_complete(Chain.SOL, STONK_MINT, tmp_db) is True


def test_a_topup_that_misses_the_watermark_demotes_to_partial(tmp_db, monkeypatch) -> None:
    """An unreached watermark is a gap, and a gap is not a complete tape."""
    prove_tape(tmp_db, monkeypatch)
    limit = SF.DEFAULT_CONFIG.trade_page_limit
    busy = [trade(f"n{i}", 1_900_000_000 - i) for i in range(limit)]
    stub_pages(monkeypatch, [page(busy, f"cursor-{i}") for i in range(10)])
    record, _flow = SF.collect_token(Chain.SOL, STONK_MINT, tmp_db)
    assert record.coverage == tape.PARTIAL
    assert record.reason.startswith("topup_gap")
    assert SF.is_complete(Chain.SOL, STONK_MINT, tmp_db) is False


def test_a_topup_that_reaches_the_watermark_keeps_the_proof(tmp_db, monkeypatch) -> None:
    proved = prove_tape(tmp_db, monkeypatch)
    limit = SF.DEFAULT_CONFIG.trade_page_limit
    fresh = [trade(f"n{i}", 1_900_000_000 - i) for i in range(limit)]
    old = [trade("t0", ((launch(STONK_MINT).created_ms or 0) // 1000) + 10)]
    stub_pages(monkeypatch, [page(fresh, "cursor"), page(old, None)])
    record, _flow = SF.collect_token(Chain.SOL, STONK_MINT, tmp_db)
    assert record.coverage == tape.COMPLETE
    assert record.covered_from_ms == proved.covered_from_ms
    assert SF.is_complete(Chain.SOL, STONK_MINT, tmp_db) is True


def test_completeness_fails_closed_when_the_launch_moves_earlier(tmp_db, monkeypatch) -> None:
    prove_tape(tmp_db, monkeypatch)
    assert SF.is_complete(Chain.SOL, STONK_MINT, tmp_db) is True
    tmp_db.execute(
        "UPDATE tokens SET created_ms = created_ms - 600000 WHERE chain=? AND address=?",
        (Chain.SOL.value, STONK_MINT),
    )
    ok, reason = SF.completeness(Chain.SOL, STONK_MINT, tmp_db)
    assert ok is False
    assert "launch_moved_before_coverage" in reason


def test_proved_predicate_matches_tape() -> None:
    """Drift detector for the one predicate this module had to copy.

    ``tape.TapeRecord.proved`` hard-codes ``route == 'pumpfun:trades'``; migration 027
    widened the schema but cannot widen tape.py, which this task does not own. So
    :func:`SF._proved` is a copy, and a copy of a safety predicate that is allowed to
    drift is worse than no predicate. Every combination below must agree with tape.py's
    own answer once the route is substituted.
    """
    base = dict(chain=Chain.SOL, token=STONK_MINT, reason="r")
    cases = [
        dict(coverage=tape.COMPLETE, proof="end_of_history", covered_from_ms=5, created_ms=9),
        dict(coverage=tape.COMPLETE, proof="end_of_history", covered_from_ms=9, created_ms=9),
        dict(coverage=tape.COMPLETE, proof="end_of_history", covered_from_ms=11, created_ms=9),
        dict(coverage=tape.COMPLETE, proof=None, covered_from_ms=5, created_ms=9),
        dict(coverage=tape.COMPLETE, proof="p", covered_from_ms=None, created_ms=9),
        dict(coverage=tape.COMPLETE, proof="p", covered_from_ms=5, created_ms=None),
        dict(coverage=tape.PARTIAL, proof="p", covered_from_ms=5, created_ms=9),
        dict(coverage=tape.UNAVAILABLE, proof=None, covered_from_ms=None, created_ms=None),
    ]
    for case in cases:
        mine = tape.TapeRecord(route=SF.ROUTE, **base, **case)
        theirs = replace(mine, route=tape.ROUTE_TRADES)
        assert SF._proved(mine) == theirs.proved, case
    assert SF._proved(None) is False
    # tape.py was widened on 2026-09-21 (tape.PER_TOKEN_ROUTES) and SF._proved is now a
    # null-safe alias rather than a copy. This guard flipped from "they must disagree" to
    # "they must agree", which is the whole point: one definition of proved, not two.
    widened = tape.TapeRecord(
        route=SF.ROUTE, coverage=tape.COMPLETE, proof="p", covered_from_ms=1, created_ms=2, **base
    )
    assert widened.proved is True, "tape.PER_TOKEN_ROUTES must admit raydium:launchlab"
    assert SF._proved(widened) is widened.proved


# --------------------------------------------------------------------------------------
# migration 027
# --------------------------------------------------------------------------------------


def _tape_row(**over: Any) -> tuple[Any, ...]:
    row: dict[str, Any] = {
        "chain": Chain.SOL.value, "token": "M", "model": tape.MODEL_ID,
        "coverage": tape.COMPLETE, "route": SF.ROUTE, "proof": "end_of_history",
        "reason": "r", "covered_from_ms": 1, "covered_to_ms": 2, "created_ms": 2,
        "oldest_ms": None, "newest_ms": None, "swaps_route": None, "swaps_total": None,
        "pages": None, "create_tx": None, "create_tx_basis": None, "attempts": 0,
        "last_attempt_ms": 1, "next_attempt_ms": None, "first_seen_ms": 1, "updated_ms": 1,
    }
    row.update(over)
    return tuple(row.values())


_TAPE_INSERT = (
    "INSERT INTO token_tape (chain, token, model, coverage, route, proof, reason, "
    " covered_from_ms, covered_to_ms, created_ms, oldest_ms, newest_ms, swaps_route, "
    " swaps_total, pages, create_tx, create_tx_basis, attempts, last_attempt_ms, "
    " next_attempt_ms, first_seen_ms, updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def test_migration_admits_launchlab_and_still_refuses_a_wallet_walk(tmp_db) -> None:
    tmp_db.execute(_TAPE_INSERT, _tape_row(token="LAUNCHLAB"))
    assert fetch_one(tmp_db, "SELECT route FROM token_tape WHERE token=?", ("LAUNCHLAB",))[
        "route"] == SF.ROUTE

    # The constraint migration 025 exists for is untouched: a wallet-walk still cannot
    # claim completeness however the route vocabulary grows.
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        tmp_db.execute(_TAPE_INSERT, _tape_row(token="WALLET", route=tape.ROUTE_WALLET))
    with pytest.raises(sqlite3.IntegrityError):
        tmp_db.execute(_TAPE_INSERT, _tape_row(token="NOPROOF", proof=None))
    with pytest.raises(sqlite3.IntegrityError):
        tmp_db.execute(_TAPE_INSERT, _tape_row(token="LATE", covered_from_ms=9, created_ms=2))
    with pytest.raises(sqlite3.IntegrityError):
        tmp_db.execute(_TAPE_INSERT, _tape_row(token="BOGUS", route="some:other:venue"))


def test_the_no_downgrade_trigger_survived_the_rebuild(tmp_db) -> None:
    import sqlite3

    tmp_db.execute(_TAPE_INSERT, _tape_row(token="PROVED"))
    with pytest.raises(sqlite3.IntegrityError):
        tmp_db.execute(
            "UPDATE token_tape SET coverage='unavailable' WHERE token=?", ("PROVED",)
        )


def test_swaps_quote_columns_default_to_null_for_other_collectors(tmp_db) -> None:
    """Existing writers keep working and their rows read as "quote asset unrecorded"."""
    tmp_db.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, source) "
        "VALUES (?,?,?,?,?,?,?)",
        (Chain.SOL.value, "legacy", 1, "w", "t", "buy", "pumpfun:trades"),
    )
    row = fetch_one(tmp_db, "SELECT * FROM swaps WHERE tx=?", ("legacy",))
    assert row["amount_quote"] is None
    assert row["quote_mint"] is None


# --------------------------------------------------------------------------------------
# create flag, candidates, run
# --------------------------------------------------------------------------------------


def test_mark_create_tx_needs_a_proved_tape(tmp_db, monkeypatch) -> None:
    """On a partial tape the oldest row we hold is an artefact of our page budget."""
    current = launch(STONK_MINT)
    SF.record_launch(current, tmp_db)
    limit = SF.DEFAULT_CONFIG.trade_page_limit
    rows = [trade(f"a{i}", 1_800_000_000 - i) for i in range(limit)]
    stub_pages(monkeypatch, [page(rows, "cursor")] * 40)
    SF.collect_token(Chain.SOL, STONK_MINT, tmp_db)
    assert SF.mark_create_tx(Chain.SOL, STONK_MINT, tmp_db) == 0
    assert fetch_one(
        tmp_db, "SELECT SUM(is_create_tx) AS n FROM swaps WHERE token=?", (STONK_MINT,)
    )["n"] == 0


def test_a_proved_tape_marks_its_first_trade_as_the_create_tx(tmp_db, monkeypatch) -> None:
    prove_tape(tmp_db, monkeypatch)
    flagged = fetch_all(
        tmp_db, "SELECT tx, ts_ms FROM swaps WHERE token=? AND is_create_tx=1", (STONK_MINT,)
    )
    assert len(flagged) == 1
    oldest = fetch_one(
        tmp_db, "SELECT MIN(ts_ms) AS m FROM swaps WHERE token=?", (STONK_MINT,)
    )["m"]
    assert flagged[0]["ts_ms"] == oldest
    # Idempotent, and it never marks a second row.
    assert SF.mark_create_tx(Chain.SOL, STONK_MINT, tmp_db) == 0


def test_candidates_skips_proved_tapes_and_honours_backoff(tmp_db, monkeypatch) -> None:
    for mint in (SOL_MINT, STONK_MINT):
        SF.record_launch(launch(mint), tmp_db)
    assert set(SF.candidates(Chain.SOL, tmp_db)) == {SOL_MINT, STONK_MINT}

    prove_tape(tmp_db, monkeypatch, STONK_MINT)
    assert SF.candidates(Chain.SOL, tmp_db) == [SOL_MINT]

    # A token in backoff is not asked at all -- the thing that makes a long resumable job
    # cheaper than a fast one.
    tmp_db.execute(
        _TAPE_INSERT,
        _tape_row(token=SOL_MINT, coverage=tape.UNAVAILABLE, proof=None, covered_from_ms=None,
                  created_ms=None, next_attempt_ms=now_ms() + 3_600_000, attempts=1),
    )
    assert SF.candidates(Chain.SOL, tmp_db) == []
    assert SF.candidates(Chain.SOL, tmp_db, at_ms=now_ms() + 7_200_000) == [SOL_MINT]


def test_run_reports_what_it_did(tmp_db, monkeypatch) -> None:
    current = launch(STONK_MINT)
    SF.record_launch(current, tmp_db)
    created_s = (current.created_ms or 0) // 1000
    rows = [trade(f"t{i}", created_s + 10 - i) for i in range(6)]
    stub_pages(monkeypatch, [page(rows, None)])
    report = SF.run(Chain.SOL, tmp_db, launches=False)
    assert report.attempted == 1
    assert report.completed == 1
    assert report.rows_written == 6
    assert report.complete_before == 0
    assert report.complete_after == 1
    out = report.as_dict()
    assert out["request_rate_per_s"] >= 0.0
    assert "reasons" in out


def test_coverage_summary_counts_the_quote_assets(tmp_db, monkeypatch) -> None:
    for mint in (SOL_MINT, STONK_MINT):
        current = launch(mint)
        SF.record_launch(current, tmp_db)
        row = SF.parse_trade(trade("sig" + mint[:4], 1_700_000_000, pool=current.pool), current)
        SF.write_trades(tmp_db, [row])
    summary = SF.coverage_summary(Chain.SOL, tmp_db)
    assert summary["launches_known"] == 2
    assert summary["swaps"] == 2
    # One of the two quotes in wrapped SOL; the other's lamport column is honestly empty.
    assert summary["swaps_with_sol_quote"] == 1
    assert summary["quote_mints"] == 2
    assert summary["unassessed"] == 2
    assert summary["complete"] == 0


def test_watch_polls_launches_and_does_not_touch_the_tape(tmp_db, monkeypatch) -> None:
    """The feed collects launches only, because the tape has no window to beat."""
    import asyncio

    rows = launch_rows()
    monkeypatch.setattr(
        SF, "fetch_launch_page",
        lambda pc, **kw: ({"rows": [r for r in rows
                                    if r["platformInfo"]["pubKey"] == pc], "nextPageId": None},
                          receipt()),
    )
    trade_calls: list[Any] = []
    monkeypatch.setattr(
        SF, "fetch_trades_page",
        lambda pool, **kw: (trade_calls.append(pool), (page([], None), receipt()))[1],
    )
    totals = asyncio.run(
        asyncio.wait_for(
            SF.watch(conn=tmp_db, config=replace(SF.DEFAULT_CONFIG, poll_interval_s=0.01),
                     max_polls=2),
            timeout=10,
        )
    )
    assert totals["polls"] == 2
    assert totals["tokens_new"] == 2      # deduped across the two polls
    assert trade_calls == [], "the launch feed must not pull tapes"
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM tokens", ())["n"] == 2


def test_recorded_error_shape_is_what_we_expect() -> None:
    """The refusal body is recorded rather than imagined, so the envelope guard is real."""
    body = fixture("trade_limit_error.json")
    assert body["success"] is False
    assert "limit max 100" in body["msg"]
    assert SF._payload(body) is None
    assert SF.DEFAULT_CONFIG.trade_page_limit <= 100
