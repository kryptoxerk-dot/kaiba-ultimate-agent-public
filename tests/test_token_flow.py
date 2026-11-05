"""Per-token trade flow and curve-snapshot deltas.

Five of these carry weight well beyond coverage, and each exists because the failure it
describes produces a *plausible number* rather than an error:

* ``test_thin_coverage_is_refused_not_computed`` — the 130x bug, written down as a test.
  Three observed trades out of four hundred turns 0.19 SOL/swap into 25 and clears a 0.18
  floor on a token pacing at the population average. The module must refuse and say why.
* ``test_trimmed_decimals_are_rescaled`` — pump.fun's trade route trims trailing zeros, so
  the same 6-decimal token arrives as ``decimals: 6`` on one trade and ``decimals: 7`` on
  the next. Reading ``raw`` as atoms is a silent factor-of-ten error per trimmed digit.
* ``test_recollecting_the_same_trades_writes_nothing_new`` — the collector is run on every
  scan, so a non-idempotent write inflates the very trade count the velocity divides by.
* ``test_refusal_leaves_no_bare_swap_count`` — ``lanes.curve_velocity`` divides curve SOL
  by ``swaps`` when ``sol_per_swap`` is absent, so a window count left behind in the curve
  dict is silently reinterpreted as a lifetime count.
* ``test_enriched_curve_fires_curve_velocity`` — the point of the whole module: the lane
  that could not fire, firing, on the contract the scanner already speaks.

Nothing here touches the network. Fixtures under ``tests/fixtures/token_flow/`` are real
responses recorded from ``frontend-api-v3.pump.fun`` on 2026-09-20.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import Chain, EvidenceBasis, Receipt, now_ms
from kaiba.ingest import token_flow as TF

FIXTURES = Path(__file__).parent / "fixtures" / "token_flow"

MINT = "ALPMbbSSc3a8Utw9nDJ1ZqANfFt3rHBY3YsdQHzVpump"
BUSY = "7M3gDRgozcumFsiTeXwjB8cYxpg7Q9R7rH7rkw2Fpump"
WALLET = "8AomZxgirYYBnxbAaPzG3vWa2uob5DgPJv4GHMyKDEap"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def receipt(basis: EvidenceBasis = EvidenceBasis.PROVIDER_REPORTED) -> Receipt:
    return Receipt(provider=TF.PROVIDER, endpoint=TF.TRADES_ENDPOINT, basis=basis)


def stub_pages(monkeypatch, pages: list[dict[str, Any] | None], calls: list[Any] | None = None):
    """Serve ``pages`` in order to :func:`fetch_trades_page`; ``None`` means unavailable."""
    state = {"i": 0}

    def _fetch(mint: str, *, before: str | None = None, **kw: Any):
        idx = min(state["i"], len(pages) - 1)
        state["i"] += 1
        if calls is not None:
            calls.append((mint, before))
        page = pages[idx]
        if page is None:
            return None, receipt(EvidenceBasis.UNAVAILABLE)
        return page, receipt()

    monkeypatch.setattr(TF, "fetch_trades_page", _fetch)
    return state


def trade(
    ordinal: str,
    ts_ms: int,
    *,
    side: str = "buy",
    wallet: str = WALLET,
    base_raw: str = "1000000",
    base_dec: int = 6,
    quote_raw: str = "100000000",
    quote_dec: int = 9,
    quote_mint: str = "11111111111111111111111111111111",
    tx: str | None = None,
) -> dict[str, Any]:
    slot, tx_index, event_index, _ = ordinal.split("-")
    return {
        "ordinalKey": ordinal,
        "blockId": slot,
        "txIndex": int(tx_index),
        "eventIndex": int(event_index),
        "blockTimeMs": ts_ms,
        "txId": tx or f"sig{ordinal}",
        "legIndex": 0,
        "side": side,
        "kind": "swap",
        "venue": "pump",
        "pool": {"address": "pool"},
        "trader": {"address": wallet},
        "baseAmount": {"raw": base_raw, "decimals": base_dec},
        "quoteAmount": {"raw": quote_raw, "decimals": quote_dec},
        "quote": {"id": quote_mint},
        "priceUsd": "0.000001",
        "valueUsd": "10.5",
        "valueNative": "0.1",
    }


def curve(
    *,
    real_sol: int,
    observed_ms: int,
    created_ms: int,
    real_token: int = 793_100_000_000_000,
    virtual_sol: int = 30_000_000_000,
    virtual_token: int = 1_073_000_000_000_000,
) -> dict[str, Any]:
    """The shape ``scanner.curve_from_payload`` produces, with the keys this module reads."""
    return {
        "progress_pct": Decimal("1.5"),
        "sol_in_curve": Decimal(real_sol) / TF.LAMPORTS_PER_SOL,
        "sol_in_curve_lamports": real_sol,
        "sol_per_min": None,
        "graduation_sol": Decimal("85"),
        "virtual_sol_reserves": virtual_sol,
        "virtual_token_reserves": virtual_token,
        "real_token_reserves": real_token,
        "created_ms": created_ms,
        "observed_ms": observed_ms,
        "source": "pumpfun",
    }


def seed_token(conn, mint: str = MINT, *, created_ms: int, decimals: int = 6) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, symbol, decimals, created_ms, "
        "launchpad, first_seen_ms) VALUES (?,?,?,?,?,?,?)",
        (Chain.SOL.value, mint, "T", decimals, created_ms, "pump.fun", created_ms),
    )


# --------------------------------------------------------------------------------------
# unit conversion
# --------------------------------------------------------------------------------------


def test_trimmed_decimals_are_rescaled():
    """The route trims trailing zeros; ``raw`` is not atoms unless the exponents agree."""
    assert TF._scaled_atoms("32751487", 6, 6) == 32_751_487
    # 4975 at 7 decimals is 0.0004975 SOL -> 497,500 lamports, not 4,975.
    assert TF._scaled_atoms("4975", 7, 9) == 497_500
    assert TF._scaled_atoms("910221", 8, 9) == 9_102_210
    assert TF._scaled_atoms("12", 0, 6) == 12_000_000


def test_fractional_base_unit_is_refused_not_truncated():
    """A fractional atom means our idea of the decimals is wrong. Truncating hides that."""
    assert TF._scaled_atoms("1234567", 9, 6) is None
    assert TF._scaled_atoms("abc", 6, 6) is None
    assert TF._scaled_atoms("100", None, 6) is None


def test_parse_trade_from_a_real_page():
    page = fixture("trades_young.json")
    rows = [TF.parse_trade(t, MINT, decimals=6) for t in page["trades"]]
    assert all(r is not None for r in rows)
    first = rows[-1]  # oldest
    assert first.side == "buy"
    assert first.wallet and len(first.wallet) > 30
    assert first.amount_token == 1_414_694_751_852
    # quoteAmount {"raw": "3960566", "decimals": 8} is 0.03960566 SOL.
    assert first.amount_native == 39_605_660
    assert isinstance(first.usd_value, Decimal)
    assert isinstance(first.price_usd, Decimal)
    assert first.program == "pump"
    assert first.slot == 448_670_505


def test_non_sol_quote_leaves_amount_native_unknown():
    """A USDC-quoted leg is a different unit. Missing is None, never 0."""
    raw = trade("1-2-3-1000", 1000, quote_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
    row = TF.parse_trade(raw, MINT, decimals=6)
    assert row is not None
    assert row.amount_native is None
    assert row.amount_token == 1_000_000


def test_non_swap_and_malformed_records_are_dropped():
    assert TF.parse_trade({**trade("1-2-3-1000", 1000), "kind": "transfer"}, MINT, decimals=6) is None
    assert TF.parse_trade({**trade("1-2-3-1000", 1000), "txId": ""}, MINT, decimals=6) is None
    assert TF.parse_trade({**trade("1-2-3-1000", 1000), "side": "mint"}, MINT, decimals=6) is None
    assert TF.parse_trade({**trade("1-2-3-1000", 1000), "blockTimeMs": None}, MINT, decimals=6) is None


def test_no_float_touches_money():
    row = TF.parse_trade(trade("1-2-3-1000", 1000), MINT, decimals=6)
    assert isinstance(row.amount_token, int)
    assert isinstance(row.amount_native, int)
    assert isinstance(row.usd_value, Decimal)
    assert not isinstance(row.amount_native, float)


def test_ordinal_cursor_validation_matches_the_routes_regex():
    assert TF._ordinal_ok("448670505-108-5-1789891032000")
    assert not TF._ordinal_ok("448670505-108-5")
    assert not TF._ordinal_ok("not-an-ordinal-at-all")
    assert not TF._ordinal_ok("4486705051789891032000-108-5-1789891032000")


# --------------------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------------------


def test_trades_are_written_in_the_swaps_column_contract(tmp_db):
    rows = [TF.parse_trade(t, MINT, decimals=6) for t in fixture("trades_young.json")["trades"]]
    written, duplicate = TF.write_trades(tmp_db, rows)
    assert written == 6
    assert duplicate == 0
    stored = fetch_all(tmp_db, "SELECT * FROM swaps WHERE token=?", (MINT,))
    assert len(stored) == 6
    assert {r["source"] for r in stored} == {TF.SOURCE}
    assert {r["chain"] for r in stored} == {"sol"}
    # Big integers are TEXT so nothing overflows 2^63 on the way through SQLite.
    assert all(isinstance(r["amount_token"], str) for r in stored)
    assert {r["side"] for r in stored} == {"buy", "sell"}
    assert all(r["fee_payer"] is None for r in stored)  # the route reports the trader, not the signer


def test_recollecting_the_same_trades_writes_nothing_new(tmp_db):
    rows = [TF.parse_trade(t, MINT, decimals=6) for t in fixture("trades_young.json")["trades"]]
    TF.write_trades(tmp_db, rows)
    written, duplicate = TF.write_trades(tmp_db, rows)
    assert written == 0
    assert duplicate == 6
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM swaps", ())["n"] == 6


def test_idempotent_even_when_the_token_amount_is_unknown(tmp_db):
    """SQLite treats NULLs as distinct, so the UNIQUE constraint cannot carry this alone."""
    raw = trade("1-2-3-1000", 1000, base_raw="1234567", base_dec=9)
    row = TF.parse_trade(raw, MINT, decimals=6)
    assert row.amount_token is None
    assert TF.write_trades(tmp_db, [row]) == (1, 0)
    assert TF.write_trades(tmp_db, [row]) == (0, 1)
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM swaps", ())["n"] == 1


def test_a_row_the_backfill_already_wrote_is_not_duplicated(tmp_db):
    row = TF.parse_trade(trade("1-2-3-1000", 1000), MINT, decimals=6)
    tmp_db.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, source) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("sol", row.tx, 1000, WALLET, MINT, "buy", "1000000", "helius:backfill"),
    )
    assert TF.write_trades(tmp_db, [row]) == (0, 1)
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM swaps", ())["n"] == 1


# --------------------------------------------------------------------------------------
# collection
# --------------------------------------------------------------------------------------


def test_a_short_page_is_the_end_of_history_and_costs_one_request(tmp_db, monkeypatch):
    calls: list[Any] = []
    stub_pages(monkeypatch, [fixture("trades_young.json")], calls)
    result = TF.collect_trades(Chain.SOL, MINT, tmp_db, created_ms=1_789_891_032_000, decimals=6)
    assert result.pages == 1
    assert len(calls) == 1
    assert result.complete is True
    assert result.reason == "end_of_history"
    assert result.trades_seen == 6
    assert result.rows_written == 6
    assert result.coverage_from_ms == 1_789_891_032_000


def test_paging_stops_at_the_watermark(tmp_db, monkeypatch):
    cfg = TF.FlowConfig(page_limit=2)
    full = {"trades": [trade(f"10{i}-1-1-{2000 - i * 100}", 2000 - i * 100) for i in range(2)]}
    older = {"trades": [trade(f"20{i}-1-1-{1500 - i * 100}", 1500 - i * 100) for i in range(2)]}
    calls: list[Any] = []
    stub_pages(monkeypatch, [full, older], calls)
    result = TF.collect_trades(
        Chain.SOL, MINT, tmp_db, since_ms=1_450, created_ms=0, decimals=6, config=cfg
    )
    assert result.pages == 2
    assert result.reason == "reached_watermark"
    assert result.complete is True
    assert calls[1][1] == "101-1-1-1900"  # paged with the oldest ordinal of page 1
    assert result.coverage_from_ms == 1_400


def test_page_budget_exhaustion_is_reported_as_incomplete(tmp_db, monkeypatch):
    cfg = TF.FlowConfig(page_limit=2)
    page = {"trades": [trade(f"30{i}-1-1-{9000 - i}", 9000 - i) for i in range(2)]}

    def _fetch(mint, *, before=None, **kw):
        n = len(page["trades"])
        base = int(before.split("-")[-1]) if before else 9001
        return {"trades": [trade(f"3{base - i}-1-1-{base - 1 - i}", base - 1 - i) for i in range(n)]}, receipt()

    monkeypatch.setattr(TF, "fetch_trades_page", _fetch)
    result = TF.collect_trades(
        Chain.SOL, MINT, tmp_db, since_ms=0, created_ms=0, decimals=6, max_pages=3, config=cfg
    )
    assert result.pages == 3
    assert result.complete is False
    assert result.reason == "page_budget_exhausted"
    # Coverage is still honest: gapless from the oldest trade we reached, not from launch.
    assert result.coverage_from_ms == result.oldest_ms


def test_a_dead_provider_is_data_not_a_crash(tmp_db, monkeypatch):
    stub_pages(monkeypatch, [None])
    result = TF.collect_trades(Chain.SOL, MINT, tmp_db, created_ms=0, decimals=6)
    assert result.pages == 0
    assert result.complete is False
    assert result.reason == "unavailable"
    assert result.coverage_from_ms is None
    assert result.trades_seen == 0


def test_other_chains_have_no_trade_source(tmp_db):
    result = TF.collect_trades(Chain.ETH, "0xabc", tmp_db)
    assert result.reason == "no_trade_source_for_eth"
    assert result.pages == 0


def test_decimals_come_from_the_tokens_row_when_not_passed(tmp_db):
    seed_token(tmp_db, created_ms=1000, decimals=9)
    assert TF.token_decimals(Chain.SOL, MINT, tmp_db) == 9
    assert TF.token_decimals(Chain.SOL, MINT, tmp_db, payload={"base_decimals": 6}) == 6
    assert TF.token_decimals(Chain.SOL, "unknown-mint", tmp_db) is None


# --------------------------------------------------------------------------------------
# snapshots and retention
# --------------------------------------------------------------------------------------


def test_snapshot_stores_raw_reserves_and_reads_back(tmp_db):
    c = curve(real_sol=1_500_000_000, observed_ms=5_000, created_ms=0)
    row_id = TF.record_snapshot(Chain.SOL, MINT, c, tmp_db, trades_seen=7, coverage_from_ms=0)
    assert row_id
    stored = TF.latest_snapshot(Chain.SOL, MINT, tmp_db)
    assert stored["real_sol_lamports"] == 1_500_000_000
    assert stored["real_token_atoms"] == "793100000000000"
    assert stored["trades_seen"] == 7
    assert stored["coverage_from_ms"] == 0
    assert stored["graduation_sol"] == "85"


def test_snapshot_without_reserves_is_refused(tmp_db):
    assert TF.record_snapshot(Chain.SOL, MINT, {"observed_ms": 1}, tmp_db) is None
    assert TF.latest_snapshot(Chain.SOL, MINT, tmp_db) is None


def test_two_snapshots_in_the_same_millisecond_are_one_observation(tmp_db):
    c = curve(real_sol=1, observed_ms=5_000, created_ms=0)
    assert TF.record_snapshot(Chain.SOL, MINT, c, tmp_db)
    assert TF.record_snapshot(Chain.SOL, MINT, c, tmp_db) is None


def test_latest_snapshot_respects_the_before_bound(tmp_db):
    for ms in (1_000, 2_000, 3_000):
        TF.record_snapshot(Chain.SOL, MINT, curve(real_sol=ms, observed_ms=ms, created_ms=0), tmp_db)
    assert TF.latest_snapshot(Chain.SOL, MINT, tmp_db)["observed_ms"] == 3_000
    assert TF.latest_snapshot(Chain.SOL, MINT, tmp_db, before_ms=3_000)["observed_ms"] == 2_000


def test_retention_caps_rows_per_token(tmp_db):
    cfg = TF.FlowConfig(snapshot_keep_per_token=3, prune_every=0)
    now = now_ms()
    for i in range(10):
        TF.record_snapshot(
            Chain.SOL, MINT, curve(real_sol=i + 1, observed_ms=now + i, created_ms=0), tmp_db, config=cfg
        )
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM curve_snapshots", ())["n"] == 10
    TF.prune_snapshots(tmp_db, config=cfg, at_ms=now + 10)
    left = fetch_all(tmp_db, "SELECT observed_ms FROM curve_snapshots ORDER BY observed_ms", ())
    assert [r["observed_ms"] for r in left] == [now + 7, now + 8, now + 9]


def test_retention_drops_rows_past_the_age_bound(tmp_db):
    cfg = TF.FlowConfig(snapshot_max_age_s=60, snapshot_keep_per_token=100, prune_every=0)
    now = now_ms()
    TF.record_snapshot(Chain.SOL, MINT, curve(real_sol=1, observed_ms=now - 120_000, created_ms=0), tmp_db)
    TF.record_snapshot(Chain.SOL, MINT, curve(real_sol=2, observed_ms=now - 1_000, created_ms=0), tmp_db)
    assert TF.prune_snapshots(tmp_db, config=cfg, at_ms=now) == 1
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM curve_snapshots", ())["n"] == 1


def test_retention_runs_on_its_own_schedule(tmp_db):
    """The table must not grow without bound even if nobody ever calls prune by hand."""
    cfg = TF.FlowConfig(snapshot_keep_per_token=2, prune_every=3)
    now = now_ms()
    for i in range(3):
        TF.record_snapshot(
            Chain.SOL, MINT, curve(real_sol=i + 1, observed_ms=now + i, created_ms=0), tmp_db, config=cfg
        )
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM curve_snapshots", ())["n"] == 2


# --------------------------------------------------------------------------------------
# rates, and the refusals that matter more
# --------------------------------------------------------------------------------------


def snapshot(observed_ms: int, lamports: int, coverage_from_ms: int | None = 0) -> dict[str, Any]:
    return {
        "observed_ms": observed_ms,
        "real_sol_lamports": lamports,
        "coverage_from_ms": coverage_from_ms,
    }


def test_velocity_between_two_covered_snapshots():
    older = snapshot(0, 1_000_000_000)
    newer = snapshot(60_000, 3_000_000_000)
    out = TF.velocity_between(older, newer, trades=10)
    assert out["basis"] == "sol_per_swap"
    assert out["sol_per_swap"] == Decimal("0.2")
    assert out["sol_per_min"] == Decimal("2")
    assert out["refusal"] is None
    assert isinstance(out["sol_per_swap"], Decimal)


def test_thin_coverage_is_refused_not_computed():
    """The 130x bug. Coverage that starts inside the interval cannot set the denominator.

    Two snapshots a minute apart with 2 SOL added. Four hundred trades really happened; we
    only saw three of them because our collection started 30 s in. Dividing would say 0.667
    SOL per swap against a true 0.005 — a factor of 133, and enough to clear any floor.
    """
    older = snapshot(0, 1_000_000_000)
    newer = snapshot(60_000, 3_000_000_000, coverage_from_ms=30_000)
    out = TF.velocity_between(older, newer, trades=3)
    assert out["sol_per_swap"] is None
    assert out["refusal"] == "coverage_misses_first_30s_of_interval"
    # And the tempting wrong answer is not smuggled in under another key.
    assert out["basis"] == "none"


def test_unknown_coverage_is_refused():
    out = TF.velocity_between(snapshot(0, 1), snapshot(60_000, 2_000_000_000, None), trades=50)
    assert out["sol_per_swap"] is None
    assert out["refusal"] == "coverage_unknown"


def test_too_few_trades_yields_the_weak_fallback_only():
    out = TF.velocity_between(snapshot(0, 1_000_000_000), snapshot(60_000, 3_000_000_000), trades=2)
    assert out["sol_per_swap"] is None
    assert out["refusal"] == "only_2_trades_in_interval"
    assert out["sol_per_min"] == Decimal("2")
    assert out["basis"] == "sol_per_min"


def test_a_curve_that_gave_sol_back_is_not_a_velocity():
    out = TF.velocity_between(snapshot(0, 3_000_000_000), snapshot(60_000, 1_000_000_000), trades=50)
    assert out["sol_per_swap"] is None
    assert out["sol_per_min"] is None
    assert out["refusal"].startswith("no_sol_added:")


def test_a_too_short_interval_is_refused():
    out = TF.velocity_between(snapshot(0, 1), snapshot(2_000, 3_000_000_000), trades=50)
    assert out["sol_per_swap"] is None
    assert "under_10.0s" in out["refusal"]


def test_snapshots_out_of_order_are_refused():
    out = TF.velocity_between(snapshot(60_000, 1), snapshot(60_000, 2), trades=50)
    assert out["refusal"] == "snapshots_out_of_order"


def test_flow_cross_check_catches_a_missing_chunk_of_trades():
    """Coverage bookkeeping can be wrong. The collected flow is an independent witness."""
    older = snapshot(0, 1_000_000_000)
    newer = snapshot(60_000, 3_000_000_000)  # curve gained 2 SOL
    out = TF.velocity_between(older, newer, trades=10, net_lamports=100_000_000)  # we saw 0.1
    assert out["sol_per_swap"] is None
    assert out["refusal"].startswith("flow_cross_check_ratio_")
    # A net flow that agrees is accepted.
    ok = TF.velocity_between(older, newer, trades=10, net_lamports=1_980_000_000)
    assert ok["sol_per_swap"] == Decimal("0.2")
    assert ok["flow_ratio"] == Decimal("0.99")


def test_flow_cross_check_is_skipped_on_dust():
    out = TF.velocity_between(snapshot(0, 0), snapshot(60_000, 1_000), trades=10, net_lamports=1)
    assert out["sol_per_swap"] is not None
    assert out["flow_ratio"] is None


def test_trades_between_reports_unpriced_windows_as_unknown(tmp_db):
    for i, native in enumerate(["1000", None, "2000"]):
        tmp_db.execute(
            "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, "
            "amount_native, source) VALUES (?,?,?,?,?,?,?,?,?)",
            ("sol", f"t{i}", 100 + i, WALLET, MINT, "buy", str(i), native, TF.SOURCE),
        )
    count, net = TF.trades_between(Chain.SOL, MINT, tmp_db, after_ms=0, until_ms=1_000)
    assert count == 3
    assert net is None  # one leg unpriced: a partial window cannot be cross-checked


def test_trades_between_nets_sells_against_buys(tmp_db):
    rows = [("a", "buy", "3000"), ("b", "sell", "1000")]
    for tx, side, native in rows:
        tmp_db.execute(
            "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, "
            "amount_native, source) VALUES (?,?,?,?,?,?,?,?,?)",
            ("sol", tx, 500, WALLET, MINT, side, "1", native, TF.SOURCE),
        )
    assert TF.trades_between(Chain.SOL, MINT, tmp_db, after_ms=0, until_ms=1_000) == (2, 2_000)


def test_gross_flow_separates_what_went_in_from_what_is_left(tmp_db):
    """A token can take in 5 SOL, give it all back, and hold 1 lamport. Both are true."""
    for tx, side, native in [("a", "buy", "3000000000"), ("b", "buy", "2000000000"), ("c", "sell", "4999999999")]:
        tmp_db.execute(
            "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, "
            "amount_native, source) VALUES (?,?,?,?,?,?,?,?,?)",
            ("sol", tx, 100, WALLET, MINT, side, "1", native, TF.SOURCE),
        )
    flow = TF.gross_flow(Chain.SOL, MINT, tmp_db)
    assert flow["buys"] == 2
    assert flow["buy_lamports"] == 5_000_000_000
    assert flow["sells"] == 1
    assert flow["unpriced"] == 0


def test_gross_per_buy_is_withheld_when_a_leg_is_unpriced(tmp_db, monkeypatch):
    created = now_ms() - 60_000
    seed_token(tmp_db, created_ms=created)
    page = {
        "trades": [
            trade(f"15{i}-1-1-{created + i}", created + i, quote_raw="1000000000")
            for i in range(3)
        ]
        + [
            trade(
                f"159-1-1-{created + 9}",
                created + 9,
                quote_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            )
        ]
    }
    stub_pages(monkeypatch, [page])
    out, _ = TF.observe(
        Chain.SOL, MINT, curve(real_sol=4_000_000_000, observed_ms=now_ms(), created_ms=created), tmp_db
    )
    assert out["buys_seen"] == 4
    assert out["sol_per_buy_gross"] is None  # one leg has no SOL amount; refuse the average


def test_covered_swap_count_matches_the_scanners_rule(tmp_db):
    """Same rule and same grace as ``scanner.swap_count_if_covered``, verified side by side."""
    from kaiba.execution import scanner as S

    created = now_ms() - 600_000
    for i in range(4):
        tmp_db.execute(
            "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, source) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("sol", f"x{i}", created + 1_000 * i, WALLET, MINT, "buy", str(i + 1), TF.SOURCE),
        )
    assert TF.covered_swap_count(Chain.SOL, MINT, tmp_db, created_ms=created) == (4, "covered_from_launch")
    assert S.swap_count_if_covered(Chain.SOL, MINT, tmp_db, created_ms=created) == (
        4,
        "covered_from_launch",
    )
    late = created - 600_000
    mine = TF.covered_swap_count(Chain.SOL, MINT, tmp_db, created_ms=late)
    theirs = S.swap_count_if_covered(Chain.SOL, MINT, tmp_db, created_ms=late)
    assert mine[0] is None and theirs[0] is None
    assert mine[1] == theirs[1]


# --------------------------------------------------------------------------------------
# observe(): the one call the scanner makes
# --------------------------------------------------------------------------------------


def test_a_late_first_trade_is_not_a_coverage_failure(tmp_db, monkeypatch):
    """Walking the history to its end is direct evidence; the timestamp rule is a proxy.

    Measured on the live sample of 2026-09-20: the proxy alone refused 4 of 10 tokens
    whose complete trade history the collector had just walked, purely because their first
    trade came several minutes after launch. That is not thin coverage, it is a quiet
    token, and refusing it throws away a real observation.
    """
    created = now_ms() - 600_000
    seed_token(tmp_db, created_ms=created)
    late = created + 400_000
    page = {
        "trades": [
            trade(f"14{i}-1-1-{late + i * 1_000}", late + i * 1_000, quote_raw="500000000")
            for i in range(4)
        ]
    }
    stub_pages(monkeypatch, [page])
    # The proxy refuses this token outright.
    TF.write_trades(tmp_db, [TF.parse_trade(t, MINT, decimals=6) for t in page["trades"]])
    assert TF.covered_swap_count(Chain.SOL, MINT, tmp_db, created_ms=created)[0] is None

    c = curve(real_sol=2_000_000_000, observed_ms=now_ms(), created_ms=created)
    out, basis = TF.observe(Chain.SOL, MINT, c, tmp_db)
    assert basis == "sol_per_swap"
    assert out["swaps_basis"] == "collected_from_launch"
    assert out["swaps"] == 4
    assert out["sol_per_swap"] == Decimal("0.5")


def test_launch_coverage_survives_a_later_watermark_stop(tmp_db, monkeypatch):
    """Once proved, the fact is persisted; a later pass that stops early keeps the count."""
    created = now_ms() - 600_000
    seed_token(tmp_db, created_ms=created)
    assert TF.launch_coverage_proved(Chain.SOL, MINT, tmp_db, created_ms=created) is False
    TF.record_snapshot(
        Chain.SOL,
        MINT,
        curve(real_sol=1, observed_ms=now_ms() - 60_000, created_ms=created),
        tmp_db,
        coverage_from_ms=created,
    )
    assert TF.launch_coverage_proved(Chain.SOL, MINT, tmp_db, created_ms=created) is True
    assert TF.launch_coverage_proved(Chain.SOL, MINT, tmp_db, created_ms=None) is False


def test_proved_launch_coverage_with_zero_trades_still_refuses(tmp_db, monkeypatch):
    """Proving we hold everything is not the same as holding enough to divide by."""
    created = now_ms() - 60_000
    seed_token(tmp_db, created_ms=created)
    stub_pages(monkeypatch, [{"trades": []}])
    out, basis = TF.observe(
        Chain.SOL, MINT, curve(real_sol=1_000, observed_ms=now_ms(), created_ms=created), tmp_db
    )
    assert basis == "none"
    assert out["velocity_refusal"] == "no_swap_rows"
    assert "swaps" not in out


def test_observe_derives_sol_per_swap_from_launch_coverage(tmp_db, monkeypatch):
    created = now_ms() - 120_000
    seed_token(tmp_db, created_ms=created)
    page = {
        "trades": [
            trade(f"10{i}-1-1-{created + i * 1_000}", created + i * 1_000, quote_raw="500000000")
            for i in range(6)
        ]
    }
    stub_pages(monkeypatch, [page])
    c = curve(real_sol=3_000_000_000, observed_ms=now_ms(), created_ms=created)
    out, basis = TF.observe(Chain.SOL, MINT, c, tmp_db)
    assert basis == "sol_per_swap"
    assert out["swaps"] == 6
    assert out["swaps_basis"] == "collected_from_launch"
    assert out["velocity_window"] == "since_launch"
    assert out["sol_per_swap"] == Decimal("0.5")
    assert out["velocity_refusal"] is None
    assert isinstance(out["sol_per_swap"], Decimal)


def test_observe_records_a_snapshot_even_when_it_refuses(tmp_db, monkeypatch):
    created = now_ms() - 5_000
    seed_token(tmp_db, created_ms=created)
    stub_pages(monkeypatch, [{"trades": []}])
    c = curve(real_sol=40_000_000, observed_ms=now_ms(), created_ms=created)
    out, basis = TF.observe(Chain.SOL, MINT, c, tmp_db)
    assert basis == "none"
    assert out["velocity_refusal"] == "no_swap_rows"
    stored = TF.latest_snapshot(Chain.SOL, MINT, tmp_db)
    assert stored is not None
    assert stored["real_sol_lamports"] == 40_000_000
    assert stored["coverage_from_ms"] == created


def test_refusal_leaves_no_bare_swap_count(tmp_db, monkeypatch):
    """``lanes.curve_velocity`` divides curve SOL by ``swaps`` when ``sol_per_swap`` is absent."""
    created = now_ms() - 5_000
    seed_token(tmp_db, created_ms=created)
    page = {"trades": [trade(f"11{i}-1-1-{created + i}", created + i) for i in range(2)]}
    stub_pages(monkeypatch, [page])
    c = curve(real_sol=40_000_000_000, observed_ms=now_ms(), created_ms=created)
    out, basis = TF.observe(Chain.SOL, MINT, c, tmp_db)
    assert basis == "none"
    assert out["velocity_refusal"] == "only_2_trades_since_launch"
    assert "swaps" not in out
    assert "trade_count" not in out


def test_observe_falls_back_to_the_snapshot_delta(tmp_db, monkeypatch):
    """A mint we met late: the walk never reached its launch, but the interval is covered.

    A full page (``page_limit`` exactly) is not end-of-history, so launch coverage is never
    proved here and the cumulative route is unavailable. The delta route is what is left.
    """
    cfg = TF.FlowConfig(page_limit=11, scan_max_pages=1)
    created = now_ms() - 3_600_000
    seed_token(tmp_db, created_ms=created)
    t0 = now_ms() - 60_000
    t1 = now_ms()
    TF.record_snapshot(
        Chain.SOL,
        MINT,
        curve(real_sol=1_000_000_000, observed_ms=t0, created_ms=created),
        tmp_db,
        coverage_from_ms=t0 - 5_000,
    )
    # Eleven trades: one before the previous snapshot (so the walk reaches the watermark
    # and coverage is provably gapless across the interval) and ten inside it.
    stamps = [t0 - 500] + [t0 + 1_000 + i * 1_000 for i in range(10)]
    page = {
        "trades": [
            trade(f"12{i}-1-1-{ts}", ts, quote_raw="200000000")
            for i, ts in enumerate(sorted(stamps, reverse=True))
        ]
    }
    stub_pages(monkeypatch, [page])
    c = curve(real_sol=3_000_000_000, observed_ms=t1, created_ms=created)
    out, basis = TF.observe(Chain.SOL, MINT, c, tmp_db, config=cfg)
    assert basis == "sol_per_swap"
    assert out["velocity_window"] == "between_snapshots"
    assert out["swaps_basis"] == "delta_between_snapshots"
    assert out["swaps"] == 10
    assert out["sol_per_swap"] == Decimal("0.2")


def test_observe_is_a_copy_and_never_mutates_the_caller(tmp_db, monkeypatch):
    seed_token(tmp_db, created_ms=now_ms() - 1_000)
    stub_pages(monkeypatch, [{"trades": []}])
    c = curve(real_sol=1_000, observed_ms=now_ms(), created_ms=now_ms() - 1_000)
    before = dict(c)
    out, _ = TF.observe(Chain.SOL, MINT, c, tmp_db)
    assert c == before
    assert out is not c


def test_observe_without_a_curve_is_a_no_op(tmp_db):
    assert TF.observe(Chain.SOL, MINT, None, tmp_db) == (None, "none")


def test_observe_can_skip_collection(tmp_db, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("collect=False must not hit the provider")

    monkeypatch.setattr(TF, "fetch_trades_page", _boom)
    seed_token(tmp_db, created_ms=now_ms() - 1_000)
    c = curve(real_sol=1_000, observed_ms=now_ms(), created_ms=now_ms() - 1_000)
    out, basis = TF.observe(Chain.SOL, MINT, c, tmp_db, collect=False)
    assert basis == "none"
    assert TF.latest_snapshot(Chain.SOL, MINT, tmp_db)["coverage_from_ms"] is None


# --------------------------------------------------------------------------------------
# the lane contract
# --------------------------------------------------------------------------------------


def test_enriched_curve_keys_match_the_scanner_contract(tmp_db, monkeypatch):
    """Every key the scanner produced must survive, so the lane sees one shape either way."""
    from kaiba.execution import scanner as S

    payload = fixture("coin_young.json")
    base, _ = S.curve_from_payload(payload)
    assert base is not None
    created = S._int(payload["created_timestamp"])
    seed_token(tmp_db, created_ms=created)
    page = {"trades": fixture("trades_young.json")["trades"]}
    stub_pages(monkeypatch, [page])
    out, _ = TF.observe(Chain.SOL, MINT, base, tmp_db)
    assert set(base) <= set(out)
    for key in ("progress_pct", "sol_in_curve", "graduation_sol"):
        assert out[key] == base[key]


def test_enriched_curve_fires_curve_velocity(tmp_db, monkeypatch):
    """The lane that could not fire, firing, on a curve this module enriched."""
    from kaiba.core.schemas import Grade, Lane, Measure, TokenDossier
    from kaiba.execution.lanes import LaneContext, curve_velocity

    created = now_ms() - 120_000
    seed_token(tmp_db, created_ms=created)
    page = {
        "trades": [
            trade(
                f"13{i}-1-1-{created + i * 1_000}",
                created + i * 1_000,
                wallet=f"Wa11et{i:034d}",
                quote_raw="500000000",
            )
            for i in range(6)
        ]
    }
    stub_pages(monkeypatch, [page])
    c = curve(real_sol=3_000_000_000, observed_ms=now_ms(), created_ms=created)
    c["progress_pct"] = Decimal("45")
    enriched, basis = TF.observe(Chain.SOL, MINT, c, tmp_db)
    assert basis == "sol_per_swap"

    graded = [f"Wa11et{i:034d}" for i in range(3)]
    for address in graded:
        tmp_db.execute(
            "INSERT INTO wallet_scores (chain, address, score, grade, evidence_weight, archetype, "
            "model_version, scored_at_ms) VALUES (?,?,?,?,?,?,?,?)",
            ("sol", address, 80.0, Grade.B.value, 1.0, "sniper", "v1", now_ms()),
        )
    dossier = TokenDossier(
        address=MINT,
        chain=Chain.SOL,
        grade=Grade.B,
        bundler_pct=Measure(value=Decimal("2"), basis=EvidenceBasis.PROVIDER_REPORTED),
        built_at_ms=now_ms(),
    )
    ctx = LaneContext(
        chain=Chain.SOL,
        token=MINT,
        now_ms=now_ms(),
        conn=tmp_db,
        dossier=dossier,
        recent_buys=[
            {"wallet": w, "ts_ms": now_ms() - 1_000, "side": "buy", "usd_value": "500", "amount_native": 1}
            for w in graded
        ],
        curve=enriched,
    )
    signal = curve_velocity(ctx)
    assert signal is not None, "curve-velocity should fire once sol_per_swap exists"
    assert signal.lane is Lane.CURVE_VELOCITY
    assert signal.payload["velocity_basis"] == "sol_per_swap"
    assert signal.payload["sol_per_swap"] == "0.5"


def test_the_lane_stays_silent_when_the_rate_was_refused(tmp_db, monkeypatch):
    """The refusal has to reach the lane as absence, not as a small number."""
    from kaiba.core.schemas import Grade, Measure, TokenDossier
    from kaiba.execution.lanes import LaneContext, curve_velocity

    created = now_ms() - 5_000
    seed_token(tmp_db, created_ms=created)
    stub_pages(monkeypatch, [{"trades": []}])
    c = curve(real_sol=3_000_000_000, observed_ms=now_ms(), created_ms=created)
    c["progress_pct"] = Decimal("45")
    enriched, basis = TF.observe(Chain.SOL, MINT, c, tmp_db)
    assert basis == "none"
    dossier = TokenDossier(
        address=MINT,
        chain=Chain.SOL,
        grade=Grade.B,
        bundler_pct=Measure(value=Decimal("2"), basis=EvidenceBasis.PROVIDER_REPORTED),
        built_at_ms=now_ms(),
    )
    ctx = LaneContext(
        chain=Chain.SOL, token=MINT, now_ms=now_ms(), conn=tmp_db, dossier=dossier, curve=enriched
    )
    assert curve_velocity(ctx) is None


# --------------------------------------------------------------------------------------
# live
# --------------------------------------------------------------------------------------


@pytest.mark.live
def test_live_trade_route_returns_usable_rows():
    page, rec = TF.fetch_trades_page(BUSY)
    assert rec.basis is not EvidenceBasis.UNAVAILABLE
    assert page is not None
    rows = [TF.parse_trade(t, BUSY, decimals=6) for t in page["trades"]]
    assert any(r is not None and r.amount_native for r in rows)
