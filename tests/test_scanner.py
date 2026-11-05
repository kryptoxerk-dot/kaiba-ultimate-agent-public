"""Tier-1 scanner: the joint between the triage queue and the lane evaluators.

Four of these carry weight beyond coverage:

* ``test_classic_curve_reproduces_thirty_and_eightyfive`` — the curve geometry is derived
  per token rather than hard-coded, and the proof that the derivation is right is that it
  returns exactly 30 SOL and exactly 85 SOL on a classic pump.fun curve. If that ever
  drifts, every non-classic curve this module reports is quietly wrong too.
* ``test_swap_count_refused_when_coverage_starts_late`` — dividing curve SOL by a swap
  count we only partially observed inflates ``sol_per_swap`` by orders of magnitude and
  fires ``curve-velocity`` on garbage. This is the test that keeps the temptation out.
* ``test_missing_curve_produces_no_signal_and_no_defaults`` — a failed fetch must leave
  ``curve=None``, never a zero or a plausible-looking stand-in.
* ``test_tier1_signal_reaches_the_engine`` — the whole point of the module: a lane
  evaluation lands in ``signals`` and ``engine.run_once`` turns it into a persisted
  decision. Before this module existed nothing constructed a ``LaneContext`` at all.

Nothing here touches the network: ``dyor.scan_token`` and the pump.fun fetch are both
stubbed, and ``test_scan_makes_no_unstubbed_provider_calls`` enforces it.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import (
    Chain,
    EvidenceBasis,
    Grade,
    Lane,
    Measure,
    Receipt,
    TokenDossier,
    now_ms,
)
from kaiba.execution import scanner as S
from kaiba.execution import triage as T

MINT = "2TFDpjKVcdzwAgXyCXxTVkzgmJ9BgVSbXV3uCU2rpump"
MINT_B = "GUy6Y8QUVLdvkxxJ1j12H2ESAz1bXcmRwqeCD6aPpump"
CREATOR = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"


# --------------------------------------------------------------------------------------
# payload fixtures, copied from live pump.fun responses on 2026-09-20
# --------------------------------------------------------------------------------------


def classic_payload(**over: Any) -> dict[str, Any]:
    """A real SOL-quoted coin whose curve follows the classic 30 SOL geometry."""
    payload: dict[str, Any] = {
        "mint": MINT,
        "complete": False,
        "quote_mint": "11111111111111111111111111111111",
        "quote_decimals": 9,
        "base_decimals": 6,
        "virtual_sol_reserves": 45_470_657_913,
        "real_sol_reserves": 15_470_657_913,
        "virtual_token_reserves": 707_929_058_275_612,
        "real_token_reserves": 428_029_058_275_612,
        "total_supply": 1_000_000_000_000_000,
        "created_timestamp": now_ms() - 600_000,
        "last_trade_timestamp": now_ms() - 1_000,
        "usd_market_cap": 6980.57,
    }
    payload.update(over)
    return payload


def raised_start_payload(**over: Any) -> dict[str, Any]:
    """A live coin that started at ~40.6 virtual SOL instead of 30.

    pump.fun no longer uses one starting market cap for every launch. This payload is the
    reason the 30/85 constants are derived rather than assumed.
    """
    payload: dict[str, Any] = {
        "mint": MINT_B,
        "complete": False,
        "quote_mint": "11111111111111111111111111111111",
        "quote_decimals": 9,
        "virtual_sol_reserves": 40_767_930_285,
        "real_sol_reserves": 134_280_773,
        "virtual_token_reserves": 1_068_090_495_992_688,
        "real_token_reserves": 788_190_495_992_688,
        "created_timestamp": now_ms() - 600_000,
    }
    payload.update(over)
    return payload


def low_cap_payload(**over: Any) -> dict[str, Any]:
    """A live coin that started at 0.107 virtual SOL and graduates at 0.41.

    The classic 793,100,000,000,000 denominator rejects this outright: it has *more* real
    tokens on the curve than a classic launch starts with.
    """
    payload: dict[str, Any] = {
        "mint": "BXVHb6rH3w9V1E7TYvV4r2f3mMfCwCgkoSAuESECpump",
        "complete": False,
        "quote_mint": "11111111111111111111111111111111",
        "quote_decimals": 9,
        "virtual_sol_reserves": 118_664_778,
        "real_sol_reserves": 11_972_621,
        "virtual_token_reserves": 1_210_859_527_743_826,
        "real_token_reserves": 930_959_527_743_826,
        "created_timestamp": now_ms() - 600_000,
    }
    payload.update(over)
    return payload


def fresh_dossier(
    token: str = MINT,
    *,
    chain: Chain = Chain.SOL,
    grade: Grade = Grade.B,
    at_ms: int | None = None,
    bundler_pct: Any = None,
    price_usd: Any = "0.000031",
    liquidity_usd: Any = "18000",
) -> TokenDossier:
    ts = at_ms if at_ms is not None else now_ms()

    def m(value: Any) -> Measure:
        if value is None:
            return Measure.unknown()
        return Measure(
            value=Decimal(str(value)),
            basis=EvidenceBasis.PROVIDER_REPORTED,
            receipt=Receipt(provider="fixture", endpoint="scanner", observed_at_ms=ts),
            freshness_budget_s=86_400,
        )

    return TokenDossier(
        address=token,
        chain=chain,
        price_usd=m(price_usd),
        liquidity_usd=m(liquidity_usd),
        bundler_pct=m(bundler_pct),
        grade=grade,
        score=70.0,
        built_at_ms=ts,
    )


def store_token(conn: sqlite3.Connection, token: str = MINT, **over: Any) -> None:
    row = {
        "chain": Chain.SOL.value,
        "address": token,
        "symbol": "TEST",
        "name": "Test",
        "decimals": 6,
        "creator": CREATOR,
        "created_ms": now_ms() - 600_000,
        "launchpad": "pump.fun",
        "pool": None,
        "migrated_ms": None,
        "first_seen_ms": now_ms() - 600_000,
    }
    row.update(over)
    conn.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, symbol, name, decimals, creator, created_ms,"
        " launchpad, pool, migrated_ms, first_seen_ms, meta_json) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,'{}')",
        tuple(row[k] for k in (
            "chain", "address", "symbol", "name", "decimals", "creator", "created_ms",
            "launchpad", "pool", "migrated_ms", "first_seen_ms",
        )),
    )


def store_dossier(conn: sqlite3.Connection, dossier: TokenDossier) -> None:
    from kaiba.core.db import jdump, upsert

    upsert(
        conn,
        "token_dossiers",
        {
            "chain": dossier.chain.value,
            "address": dossier.address,
            "built_at_ms": dossier.built_at_ms,
            "score": dossier.score,
            "grade": dossier.grade.value,
            "blockers_json": jdump([b.value for b in dossier.blockers]),
            "warnings_json": jdump([w.value for w in dossier.warnings]),
            "unknowns_json": jdump(list(dossier.unknowns)),
            "dossier_json": dossier.model_dump_json(),
        },
        ["chain", "address"],
    )


def store_swap(
    conn: sqlite3.Connection,
    *,
    token: str = MINT,
    wallet: str = WALLET,
    ts_ms: int | None = None,
    side: str = "buy",
    tx: str = "sig1",
    usd_value: str | None = "120",
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO swaps (chain, tx, slot, ts_ms, wallet, token, side, amount_token,"
        " amount_native, price_usd, usd_value, program, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            Chain.SOL.value, tx, 1, ts_ms if ts_ms is not None else now_ms() - 30_000,
            wallet, token, side, "1000000", "50000000", "0.00003", usd_value, "pump", "test",
        ),
    )


@pytest.fixture(autouse=True)
def _clear_recent():
    """The re-scan cooldown is process-wide state; one test must not seed the next."""
    S.RECENT.clear()
    yield
    S.RECENT.clear()


@pytest.fixture
def no_network(monkeypatch):
    """Fail loudly rather than dial out. Both tier-1 provider paths are stubbed by name."""

    def _boom(*a: Any, **k: Any):
        raise AssertionError("tier-1 test made an unstubbed provider call")

    monkeypatch.setattr(S, "fetch_curve_payload", _boom)
    import kaiba.intelligence.dyor as dyor

    monkeypatch.setattr(dyor, "scan_token", _boom)
    return None


def stub_curve(monkeypatch, payload: dict[str, Any] | None) -> None:
    receipt = Receipt(
        provider=S.PROVIDER,
        endpoint=S.CURVE_ENDPOINT,
        basis=EvidenceBasis.PROVIDER_REPORTED if payload else EvidenceBasis.UNAVAILABLE,
        note=None if payload else "ConnectError: pump.fun unreachable",
    )
    monkeypatch.setattr(S, "fetch_curve_payload", lambda *a, **k: (payload, receipt))


def stub_dyor(monkeypatch, dossier: TokenDossier, calls: list[str] | None = None) -> None:
    import kaiba.intelligence.dyor as dyor

    def _scan(address: str, chain: Any = Chain.SOL, *, conn: Any = None) -> TokenDossier:
        if calls is not None:
            calls.append(address)
        return dossier

    monkeypatch.setattr(dyor, "scan_token", _scan)


# --------------------------------------------------------------------------------------
# curve arithmetic
# --------------------------------------------------------------------------------------


def test_classic_curve_reproduces_thirty_and_eightyfive():
    """The derived geometry must return the known constants on a known curve."""
    curve, basis = S.curve_from_payload(classic_payload())
    assert curve is not None
    assert curve["virtual_sol_initial"] == pytest.approx(Decimal(30), abs=Decimal("0.01"))
    assert curve["graduation_sol"] == pytest.approx(S.CLASSIC_GRADUATION_SOL, abs=Decimal("0.01"))
    # Not exact: the on-chain reserves are integers, so the invariant carries a few
    # parts per billion of rounding drift. Anything larger is a broken derivation.
    assert curve["real_token_initial"] == pytest.approx(S.CLASSIC_REAL_TOKEN_ATOMS, rel=1e-7)
    assert curve["classic_curve"] is True
    assert basis == "sol_per_min"


def test_progress_comes_from_the_derived_initial_reserves():
    curve, _ = S.curve_from_payload(classic_payload())
    assert curve is not None
    expected = (
        Decimal(S.CLASSIC_REAL_TOKEN_ATOMS - 428_029_058_275_612)
        / Decimal(S.CLASSIC_REAL_TOKEN_ATOMS)
        * 100
    )
    assert curve["progress_pct"] == pytest.approx(expected, abs=Decimal("0.001"))
    assert curve["progress_basis"] == "real_token_reserves_vs_derived_initial"
    assert curve["sol_in_curve"] == Decimal("15.470657913")


def test_non_classic_start_derives_its_own_graduation_target():
    """A 40.6 SOL start must not be reported as needing 85 SOL to graduate."""
    curve, _ = S.curve_from_payload(raised_start_payload())
    assert curve is not None
    assert curve["virtual_sol_initial"] > Decimal(35)
    assert curve["graduation_sol"] > Decimal(100)
    assert curve["classic_curve"] is False


def test_low_cap_launch_is_measured_not_rejected():
    """A live payload that the hard-coded 793.1e12 denominator threw away.

    Three of thirty launches in the first live tier-1 run started with *more* real tokens
    on the curve than the classic constant, because the creator chose a lower starting
    market cap. Rejecting them lost a tenth of the feed; worse, the same wrong denominator
    silently overstates progress on every launch that starts below the classic point,
    which is how a token nowhere near the mid band ends up inside curve-velocity's 30-70%
    window.
    """
    curve, _ = S.curve_from_payload(low_cap_payload())
    assert curve is not None
    assert curve["real_token_initial"] > S.CLASSIC_REAL_TOKEN_ATOMS
    assert curve["virtual_sol_initial"] < Decimal("0.2")
    assert curve["graduation_sol"] < Decimal(1)
    assert Decimal(5) < curve["progress_pct"] < Decimal(30)
    # Token progress and SOL progress diverge sharply on a convex curve; both are
    # reported so a threshold can never be compared against the wrong one by accident.
    assert curve["sol_raised_pct_of_graduation"] < curve["progress_pct"]


def test_non_sol_quote_is_refused_rather_than_unit_mixed():
    curve, note = S.curve_from_payload(
        classic_payload(quote_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", quote_decimals=6)
    )
    assert curve is None
    assert note.startswith("non_sol_quote")


def test_completed_curve_and_unreadable_reserves_produce_no_curve():
    assert S.curve_from_payload(classic_payload(complete=True))[0] is None
    assert S.curve_from_payload(classic_payload(real_token_reserves=None))[0] is None
    assert S.curve_from_payload(classic_payload(virtual_sol_reserves=0))[0] is None
    # real_sol above virtual_sol would put this curve's starting point at or below zero.
    assert S.curve_from_payload(classic_payload(real_sol_reserves=99_000_000_000))[0] is None
    # Reserves that do not satisfy the invariant produce a progress outside [0, 100];
    # refusing beats reporting a number nothing supports.
    assert S.curve_from_payload(classic_payload(real_token_reserves=10**15))[0] is None


def test_per_minute_velocity_is_not_extrapolated_from_seconds():
    """A 5-second-old token must not report a per-minute rate."""
    young, _ = S.curve_from_payload(classic_payload(created_timestamp=now_ms() - 5_000))
    assert young is not None
    assert young["sol_per_min"] is None

    old, basis = S.curve_from_payload(classic_payload(created_timestamp=now_ms() - 600_000))
    assert old is not None
    assert old["sol_per_min"] == pytest.approx(Decimal("1.547"), abs=Decimal("0.01"))
    assert basis == "sol_per_min"


def test_absent_swap_count_is_absent_not_zero():
    """The lane divides by this. An absent key is safe; a zero is a crash or a lie."""
    curve, basis = S.curve_from_payload(classic_payload(), swaps=None)
    assert curve is not None
    assert "swaps" not in curve
    assert basis == "sol_per_min"

    with_swaps, basis2 = S.curve_from_payload(classic_payload(), swaps=84, swaps_basis="covered")
    assert with_swaps is not None
    assert with_swaps["swaps"] == 84
    assert basis2 == "sol_per_swap"


def test_curve_dict_is_what_curve_velocity_reads(tmp_db):
    """The contract with the lane, asserted rather than assumed."""
    from kaiba.execution.lanes import LaneContext, curve_velocity

    curve, _ = S.curve_from_payload(classic_payload(), swaps=84)
    ctx = LaneContext(
        chain=Chain.SOL,
        token=MINT,
        conn=tmp_db,
        curve=curve,
        dossier=fresh_dossier(bundler_pct="5"),
        recent_buys=[],
    )
    # progress 46% is inside [30, 70] and 15.47/84 = 0.184 SOL/swap clears the 0.18 floor,
    # so the only thing left to stop it is the graded-wallet requirement — which is
    # exactly the honest reason this lane cannot fire today.
    signal = curve_velocity(ctx)
    assert signal is None  # no graded wallets exist

    ctx.dossier = fresh_dossier(bundler_pct="5")
    ctx.dossier.graded_wallets = [WALLET]
    tmp_db.execute(
        "INSERT OR REPLACE INTO wallet_scores (chain, address, score, grade, evidence_weight,"
        " archetype, factors_json, penalties_json, blockers_json, receipts_json, model_version,"
        " scored_at_ms) VALUES (?,?,?,?,?,?,'[]','[]','[]','[]','test',?)",
        (Chain.SOL.value, WALLET, 70.0, Grade.B.value, 100.0, "trader", now_ms()),
    )
    fired = curve_velocity(ctx)
    assert fired is not None
    assert fired.payload["velocity_basis"] == "sol_per_swap"


# --------------------------------------------------------------------------------------
# swap coverage
# --------------------------------------------------------------------------------------


def test_swap_count_refused_when_there_are_no_rows(tmp_db):
    n, why = S.swap_count_if_covered(Chain.SOL, MINT, tmp_db, created_ms=now_ms() - 600_000)
    assert n is None and why == "no_swap_rows"


def test_swap_count_refused_when_coverage_starts_late(tmp_db):
    """Three observed swaps out of four hundred would overstate per-swap by ~130x."""
    created = now_ms() - 600_000
    store_swap(tmp_db, ts_ms=created + 400_000, tx="late1")
    store_swap(tmp_db, ts_ms=created + 410_000, tx="late2")
    n, why = S.swap_count_if_covered(Chain.SOL, MINT, tmp_db, created_ms=created)
    assert n is None
    assert why.startswith("coverage_starts_")


def test_swap_count_accepted_when_we_watched_from_launch(tmp_db):
    created = now_ms() - 600_000
    store_swap(tmp_db, ts_ms=created + 2_000, tx="a")
    store_swap(tmp_db, ts_ms=created + 9_000, tx="b")
    n, why = S.swap_count_if_covered(Chain.SOL, MINT, tmp_db, created_ms=created)
    assert n == 2 and why == "covered_from_launch"


def test_swap_count_refused_when_creation_time_unknown(tmp_db):
    store_swap(tmp_db, tx="x")
    n, why = S.swap_count_if_covered(Chain.SOL, MINT, tmp_db, created_ms=None)
    assert n is None and why == "creation_time_unknown"


# --------------------------------------------------------------------------------------
# context assembly
# --------------------------------------------------------------------------------------


def test_build_context_populates_every_field_it_can(tmp_db, monkeypatch):
    store_token(tmp_db)
    store_swap(tmp_db, tx="s1")
    stub_curve(monkeypatch, classic_payload())
    stub_dyor(monkeypatch, fresh_dossier())

    ctx, partial = S.build_context(Chain.SOL, MINT, tmp_db)
    assert ctx.chain is Chain.SOL and ctx.token == MINT
    assert ctx.conn is tmp_db
    assert ctx.dossier is not None and ctx.dossier.grade is Grade.B
    assert ctx.token_meta is not None and ctx.token_meta.creator == CREATOR
    assert len(ctx.recent_buys) == 1
    assert ctx.curve is not None
    assert ctx.curve["progress_basis"] == "real_token_reserves_vs_derived_initial"
    assert ctx.caller is None  # caller_calls is empty, and that is the honest answer
    assert partial.curve_ok is True and partial.recent_buys == 1


def test_fresh_dossier_is_reused_and_a_stale_one_is_not(tmp_db, monkeypatch):
    calls: list[str] = []
    store_token(tmp_db)
    stub_curve(monkeypatch, None)
    stub_dyor(monkeypatch, fresh_dossier(), calls)

    store_dossier(tmp_db, fresh_dossier(at_ms=now_ms() - 10_000))
    _, partial = S.build_context(Chain.SOL, MINT, tmp_db)
    assert partial.dossier_reused is True and calls == []

    store_dossier(
        tmp_db, fresh_dossier(at_ms=now_ms() - (S.DEFAULT_CONFIG.dossier_max_age_s + 60) * 1000)
    )
    _, partial = S.build_context(Chain.SOL, MINT, tmp_db)
    assert partial.dossier_reused is False and calls == [MINT]


def test_dossier_reuse_leaves_the_engine_headroom():
    """A signal must not be born with a dossier the engine will already call stale.

    The engine refuses an entry whose dossier is over ``DOSSIER_MAX_AGE_S`` at *decide*
    time, which is always later than scan time. Reusing right up to that budget produced
    exactly that in the first live run: "dossier is 483s old (budget 300s)".
    """
    from kaiba.execution.engine import DOSSIER_MAX_AGE_S

    assert S.DEFAULT_CONFIG.dossier_max_age_s < DOSSIER_MAX_AGE_S


def test_force_dossier_ignores_a_fresh_one(tmp_db, monkeypatch):
    calls: list[str] = []
    store_token(tmp_db)
    stub_curve(monkeypatch, None)
    stub_dyor(monkeypatch, fresh_dossier(), calls)
    store_dossier(tmp_db, fresh_dossier(at_ms=now_ms() - 5_000))

    _, partial = S.build_context(Chain.SOL, MINT, tmp_db, force_dossier=True)
    assert partial.dossier_reused is False and calls == [MINT]


def test_migrated_token_skips_the_curve_call(tmp_db, monkeypatch):
    store_token(tmp_db, migrated_ms=now_ms() - 30_000)
    stub_dyor(monkeypatch, fresh_dossier())
    monkeypatch.setattr(
        S, "fetch_curve_payload", lambda *a, **k: pytest.fail("migrated tokens have no curve")
    )
    ctx, partial = S.build_context(Chain.SOL, MINT, tmp_db)
    assert ctx.curve is None and partial.curve_note == "already_migrated"


def test_missing_curve_produces_no_signal_and_no_defaults(tmp_db, monkeypatch):
    """A dead provider leaves the field absent. It never becomes a zero."""
    store_token(tmp_db)
    stub_curve(monkeypatch, None)
    stub_dyor(monkeypatch, fresh_dossier(bundler_pct="1"))

    ctx, partial = S.build_context(Chain.SOL, MINT, tmp_db)
    assert ctx.curve is None
    assert partial.curve_ok is False
    assert partial.curve_note is not None and partial.curve_note.startswith("curve_unavailable")

    from kaiba.execution.lanes import curve_velocity

    assert curve_velocity(ctx) is None


def test_recent_buys_include_sells_so_netting_works(tmp_db, monkeypatch):
    store_token(tmp_db)
    stub_curve(monkeypatch, None)
    stub_dyor(monkeypatch, fresh_dossier())
    store_swap(tmp_db, tx="b1", side="buy")
    store_swap(tmp_db, tx="s1", side="sell")
    ctx, _ = S.build_context(Chain.SOL, MINT, tmp_db)
    assert {r["side"] for r in ctx.recent_buys} == {"buy", "sell"}
    assert ctx.recent_buys[0]["ts_ms"] <= ctx.recent_buys[-1]["ts_ms"]  # oldest first


def test_buys_outside_the_window_are_not_loaded(tmp_db, monkeypatch):
    store_token(tmp_db)
    stub_curve(monkeypatch, None)
    stub_dyor(monkeypatch, fresh_dossier())
    store_swap(tmp_db, tx="old", ts_ms=now_ms() - 7_200_000)
    ctx, _ = S.build_context(Chain.SOL, MINT, tmp_db)
    assert ctx.recent_buys == []


def test_caller_is_loaded_when_one_exists(tmp_db, monkeypatch):
    store_token(tmp_db)
    stub_curve(monkeypatch, None)
    stub_dyor(monkeypatch, fresh_dossier())
    tmp_db.execute(
        "INSERT OR REPLACE INTO callers (platform, caller_id, display_name, channel, calls, wins,"
        " losses, expectancy, avg_peak_x, last_call_ms, mode) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("telegram", "kol1", "KOL One", None, 30, 12, 18, 0.2, None, now_ms(), "follow"),
    )
    tmp_db.execute(
        "INSERT OR REPLACE INTO caller_calls (platform, caller_id, chain, token, ts_ms, channel,"
        " price_at_call_usd, peak_x, outcome) VALUES (?,?,?,?,?,?,?,?,'pending')",
        ("telegram", "kol1", Chain.SOL.value, MINT, now_ms() - 30_000, None, None, None),
    )
    ctx, _ = S.build_context(Chain.SOL, MINT, tmp_db)
    assert ctx.caller is not None and ctx.caller["caller_id"] == "kol1"


# --------------------------------------------------------------------------------------
# the pass
# --------------------------------------------------------------------------------------


def test_scan_never_raises_and_records_the_failure(tmp_db, monkeypatch):
    store_token(tmp_db)
    stub_curve(monkeypatch, None)
    stub_dyor(monkeypatch, fresh_dossier())

    def _explode(ctx: Any, conn: Any) -> list[Any]:
        raise RuntimeError("lane registry exploded")

    monkeypatch.setattr(S.lanes_mod, "evaluate_and_record", _explode)
    result = S.scan(Chain.SOL, MINT, tmp_db)
    assert result.ok is False
    assert "lane registry exploded" in (result.error or "")
    rows = fetch_all(tmp_db, "SELECT kind FROM events WHERE kind=?", (S.EVENT_SCAN_FAILED,))
    assert len(rows) == 1


def test_one_bad_token_does_not_stop_the_batch(tmp_db, monkeypatch):
    store_token(tmp_db)
    store_token(tmp_db, address=MINT_B)
    stub_curve(monkeypatch, None)
    stub_dyor(monkeypatch, fresh_dossier())
    real = S.lanes_mod.evaluate_and_record

    def _sometimes(ctx: Any, conn: Any) -> list[Any]:
        if ctx.token == MINT:
            raise ValueError("bad token")
        return real(ctx, conn)

    monkeypatch.setattr(S.lanes_mod, "evaluate_and_record", _sometimes)
    results = S.run_once(tmp_db, tokens=[(Chain.SOL, MINT), (Chain.SOL, MINT_B)])
    assert [r.ok for r in results] == [False, True]


def test_scan_makes_no_unstubbed_provider_calls(tmp_db, no_network):
    """The dossier is fresh and the token has migrated, so nothing may dial out."""
    store_token(tmp_db, migrated_ms=now_ms() - 30_000)
    store_dossier(tmp_db, fresh_dossier(at_ms=now_ms() - 5_000))
    result = S.scan(Chain.SOL, MINT, tmp_db)
    assert result.ok is True and result.dossier_reused is True


def test_tier1_signal_reaches_the_engine(tmp_db, monkeypatch):
    """The keystone: a lane evaluation becomes a signal row and then a decision row."""
    from kaiba.execution import engine

    migrated = now_ms() - 20_000
    store_token(tmp_db, migrated_ms=migrated)
    dossier = fresh_dossier(at_ms=now_ms() - 5_000)
    store_dossier(tmp_db, dossier)
    stub_dyor(monkeypatch, dossier)

    result = S.scan(Chain.SOL, MINT, tmp_db, source="migration", extras={"migration_ms": migrated})
    assert result.ok is True
    assert Lane.MIGRATION_FADE.value in result.lanes_fired

    stored = fetch_all(tmp_db, "SELECT * FROM signals WHERE token=?", (MINT,))
    assert len(stored) == 1
    assert stored[0]["lane"] == Lane.MIGRATION_FADE.value

    decisions = engine.run_once(tmp_db)
    assert len(decisions) == 1
    assert decisions[0].token == MINT
    assert stored[0]["signal_id"] in decisions[0].signals
    persisted = fetch_one(
        tmp_db, "SELECT * FROM decisions WHERE decision_id=?", (decisions[0].decision_id,)
    )
    assert persisted is not None


def test_re_scanning_the_same_bucket_does_not_duplicate_signals(tmp_db, monkeypatch):
    migrated = now_ms() - 20_000
    store_token(tmp_db, migrated_ms=migrated)
    dossier = fresh_dossier(at_ms=now_ms() - 5_000)
    store_dossier(tmp_db, dossier)
    stub_dyor(monkeypatch, dossier)

    first = S.scan(Chain.SOL, MINT, tmp_db, extras={"migration_ms": migrated})
    second = S.scan(Chain.SOL, MINT, tmp_db, extras={"migration_ms": migrated})
    assert first.signals and not second.signals  # same bucket, already recorded
    assert len(fetch_all(tmp_db, "SELECT 1 FROM signals WHERE token=?", (MINT,))) == 1


# --------------------------------------------------------------------------------------
# work sources
# --------------------------------------------------------------------------------------


def _promoted(token: str = MINT, score: float = 0.9) -> T.TriageDecision:
    return T.TriageDecision(
        chain=Chain.SOL,
        token=token,
        verdict=T.Verdict.PROMOTE,
        score=score,
        ts_ms=now_ms(),
    )


def test_run_once_drains_the_triage_queue(tmp_db, monkeypatch):
    store_token(tmp_db)
    stub_curve(monkeypatch, None)
    stub_dyor(monkeypatch, fresh_dossier())
    queue = T.TriageQueue(capacity=16, name="test")
    queue.offer(_promoted())

    results = S.run_once(tmp_db, limit=4, queue=queue)
    assert [r.token for r in results] == [MINT]
    assert results[0].source == "queue"
    assert len(queue) == 0


def test_the_queue_orders_the_work_it_hands_over(tmp_db, monkeypatch):
    store_token(tmp_db)
    store_token(tmp_db, address=MINT_B)
    stub_curve(monkeypatch, None)
    stub_dyor(monkeypatch, fresh_dossier())
    queue = T.TriageQueue(capacity=16, name="test")
    queue.offer(_promoted(MINT_B, score=0.2))
    queue.offer(_promoted(MINT, score=0.95))

    results = S.run_once(tmp_db, limit=2, queue=queue)
    assert [r.token for r in results] == [MINT, MINT_B]


def test_migrations_are_offered_ahead_of_the_queue_and_watermarked(tmp_db, monkeypatch):
    from kaiba.core.events import emit
    from kaiba.core.schemas import EventKind

    migrated = now_ms() - 10_000
    store_token(tmp_db, migrated_ms=migrated)
    emit(
        EventKind.TOKEN_MIGRATED,
        {"mint": MINT, "migrated_ms": migrated},
        chain=Chain.SOL,
        subject=MINT,
        conn=tmp_db,
    )
    queue = T.TriageQueue(capacity=16, name="test")
    queue.offer(_promoted(MINT_B))

    work = S.next_work(tmp_db, 4, queue=queue)
    assert [(w.token, w.source) for w in work] == [(MINT, "migration"), (MINT_B, "queue")]
    assert work[0].extras["migration_ms"] == migrated
    # The watermark moved, so a restart does not re-scan it.
    assert S.next_work(tmp_db, 4, queue=T.TriageQueue(name="empty")) == []


def test_stale_migrations_are_skipped_but_still_watermarked(tmp_db):
    from kaiba.core.events import emit
    from kaiba.core.schemas import EventKind

    old = now_ms() - 3_600_000
    emit(
        EventKind.TOKEN_MIGRATED,
        {"mint": MINT, "migrated_ms": old},
        chain=Chain.SOL,
        subject=MINT,
        conn=tmp_db,
    )
    assert S.next_work(tmp_db, 4, queue=T.TriageQueue(name="empty")) == []
    row = fetch_one(tmp_db, "SELECT value FROM kv WHERE key=?", (S.MIGRATION_WATERMARK_KEY,))
    assert row is not None


def test_migrations_can_be_turned_off(tmp_db):
    from kaiba.core.events import emit
    from kaiba.core.schemas import EventKind

    emit(
        EventKind.TOKEN_MIGRATED,
        {"mint": MINT, "migrated_ms": now_ms()},
        chain=Chain.SOL,
        subject=MINT,
        conn=tmp_db,
    )
    config = replace(S.DEFAULT_CONFIG, include_migrations=False)
    assert S.next_work(tmp_db, 4, queue=T.TriageQueue(name="empty"), config=config) == []


def test_run_once_with_an_empty_queue_returns_nothing(tmp_db):
    assert S.run_once(tmp_db, queue=T.TriageQueue(name="empty")) == []


def test_tier1_finds_work_when_tier0_ran_in_another_process(tmp_db):
    """The in-memory queue is process-local; the table is what a separate service reads.

    ``kaiba ingest run`` and ``kaiba scan run`` are two systemd units. If tier 1 could
    only see ``triage.get_queue()`` it would idle forever in production while tier 0
    screened the whole market next door.
    """
    T.record_decision(_promoted(MINT, score=0.8), tmp_db)
    work = S.next_work(tmp_db, 4, queue=T.TriageQueue(name="empty"))
    assert [(w.token, w.source) for w in work] == [(MINT, "triage_db")]
    # Watermarked, so the next cycle does not re-serve it.
    assert S.next_work(tmp_db, 4, queue=T.TriageQueue(name="empty")) == []


def test_db_queue_ranks_promote_above_defer_and_drops_the_tail(tmp_db):
    T.record_decision(
        T.TriageDecision(chain=Chain.SOL, token=MINT_B, verdict=T.Verdict.DEFER,
                         score=0.99, ts_ms=now_ms()),
        tmp_db,
    )
    T.record_decision(_promoted(MINT, score=0.10), tmp_db)
    third = "3jZ5kQ8WvnR6yNXqLpTtA1cGdFbHmKsVwXyZaBcDeFgh"
    T.record_decision(
        T.TriageDecision(chain=Chain.SOL, token=third, verdict=T.Verdict.DEFER,
                         score=0.05, ts_ms=now_ms()),
        tmp_db,
    )
    work = S.next_work(tmp_db, 2, queue=T.TriageQueue(name="empty"))
    assert [w.token for w in work] == [MINT, MINT_B]  # promote first, then best defer
    # The unreachable tail is dropped rather than carried forward as a stale backlog.
    assert S.next_work(tmp_db, 4, queue=T.TriageQueue(name="empty")) == []


def test_db_queue_ignores_rejects_and_stale_rows(tmp_db):
    T.record_decision(
        T.TriageDecision(chain=Chain.SOL, token=MINT, verdict=T.Verdict.REJECT,
                         score=0.0, ts_ms=now_ms()),
        tmp_db,
    )
    T.record_decision(
        T.TriageDecision(chain=Chain.SOL, token=MINT_B, verdict=T.Verdict.PROMOTE,
                         score=0.9, ts_ms=now_ms() - 3_600_000),
        tmp_db,
    )
    assert S.next_work(tmp_db, 4, queue=T.TriageQueue(name="empty")) == []


def test_a_token_is_not_scanned_twice_by_two_sources(tmp_db, monkeypatch):
    """In-process ingest puts a launch in both the memory queue and the table."""
    store_token(tmp_db)
    stub_curve(monkeypatch, None)
    stub_dyor(monkeypatch, fresh_dossier())
    queue = T.TriageQueue(capacity=16, name="test")
    decision = _promoted(MINT)
    queue.offer(decision)
    T.record_decision(decision, tmp_db)

    first = S.run_once(tmp_db, limit=4, queue=queue)
    assert [w.token for w in first] == [MINT]
    assert S.next_work(tmp_db, 4, queue=queue) == []  # cooldown, not a second slot


def test_the_cooldown_never_blocks_a_migration(tmp_db):
    from kaiba.core.events import emit
    from kaiba.core.schemas import EventKind

    S.RECENT.mark(Chain.SOL, MINT)
    emit(
        EventKind.TOKEN_MIGRATED,
        {"mint": MINT, "migrated_ms": now_ms()},
        chain=Chain.SOL,
        subject=MINT,
        conn=tmp_db,
    )
    work = S.next_work(tmp_db, 4, queue=T.TriageQueue(name="empty"))
    assert [w.source for w in work] == ["migration"]


def test_an_explicit_token_request_ignores_the_cooldown(tmp_db, monkeypatch):
    store_token(tmp_db)
    stub_curve(monkeypatch, None)
    stub_dyor(monkeypatch, fresh_dossier())
    S.RECENT.mark(Chain.SOL, MINT)
    results = S.scan_tokens([MINT], conn=tmp_db)
    assert [r.token for r in results] == [MINT]


def test_db_queue_can_be_turned_off(tmp_db):
    T.record_decision(_promoted(MINT), tmp_db)
    config = replace(S.DEFAULT_CONFIG, db_queue=False)
    assert S.next_work(tmp_db, 4, queue=T.TriageQueue(name="empty"), config=config) == []


# --------------------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------------------


def test_run_loop_stops_on_the_token_budget(tmp_db, monkeypatch):
    store_token(tmp_db)
    store_token(tmp_db, address=MINT_B)
    stub_curve(monkeypatch, None)
    stub_dyor(monkeypatch, fresh_dossier())
    queue = T.TriageQueue(capacity=16, name="test")
    queue.offer(_promoted(MINT))
    queue.offer(_promoted(MINT_B))

    stats = S.run_loop(tmp_db, queue=queue, max_tokens=1, config=replace(S.DEFAULT_CONFIG, batch=4))
    assert stats.scanned + stats.failed == 1


def test_run_loop_honours_the_stop_event(tmp_db):
    stop = threading.Event()
    stop.set()
    stats = S.run_loop(tmp_db, queue=T.TriageQueue(name="empty"), stop=stop)
    assert stats.scanned == 0


def test_run_loop_idles_without_spinning(tmp_db, monkeypatch):
    config = replace(S.DEFAULT_CONFIG, idle_sleep_s=0.01)
    stats = S.run_loop(
        tmp_db, queue=T.TriageQueue(name="empty"), config=config, max_seconds=0.05
    )
    assert stats.scanned == 0 and stats.failed == 0


def test_parallel_workers_scan_every_item(tmp_db, monkeypatch):
    """Concurrency is allowed; correctness under it is asserted, not assumed."""
    seen: list[str] = []
    lock = threading.Lock()

    def _fake(chain: Chain, token: str, conn: Any = None, **kw: Any) -> S.ScanResult:
        with lock:
            seen.append(token)
        return S.ScanResult(chain=chain, token=token, source=kw.get("source", "queue"))

    monkeypatch.setattr(S, "scan", _fake)
    config = replace(S.DEFAULT_CONFIG, workers=3, batch=3)
    queue = T.TriageQueue(capacity=16, name="test")
    for tok in (MINT, MINT_B, "3jZ5kQ8WvnR6yNXqLpTtA1cGdFbHmKsVwXyZaBcDeFgh"):
        queue.offer(_promoted(tok))
    results = S.run_once(tmp_db, config=config, queue=queue)
    assert len(results) == 3 and len(set(seen)) == 3


# --------------------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------------------


def test_stats_and_rate_report_the_measured_numbers():
    stats = S.ScanStats()
    stats.record(
        S.ScanResult(chain=Chain.SOL, token=MINT, elapsed_ms=8000, dossier_ms=7500,
                     dossier_grade=Grade.C, curve_ok=True, velocity_basis="sol_per_min")
    )
    stats.record(S.ScanResult(chain=Chain.SOL, token=MINT_B, ok=False, error="ValueError: x"))
    snap = stats.as_dict()
    assert snap["scanned"] == 1 and snap["failed"] == 1
    assert snap["grades"] == {"C": 1}
    assert snap["errors"] == {"ValueError": 1}

    rate = S.measure_rate(stats, launches_per_min=14.0)
    assert rate["launches_per_min"] == 14.0
    assert rate["market_coverage_pct"] is not None
    assert rate["backlog_ratio"] is not None


def test_silence_report_explains_a_zero_signal_pass():
    results = [
        S.ScanResult(chain=Chain.SOL, token=MINT, curve_ok=False,
                     curve_note="curve_unavailable:ConnectError", dossier_grade=Grade.D),
        S.ScanResult(chain=Chain.SOL, token=MINT_B, ok=False, error="boom"),
    ]
    report = S.lane_silence_report(results)
    assert report["scanned"] == 2 and report["ok"] == 1 and report["failed"] == 1
    assert report["signals"] == 0
    assert report["curve_missing_reasons"] == {"curve_unavailable": 1}
    assert report["dossier_grades"] == {"D": 1}
    assert Lane.MIGRATION_FADE.value in report["lanes_available"]


def test_scan_result_round_trips_to_json():
    import json

    result = S.ScanResult(chain=Chain.SOL, token=MINT, dossier_grade=Grade.A, elapsed_ms=12)
    assert json.loads(json.dumps(result.as_dict(), default=str))["dossier_grade"] == "A"


# ------------------------------- a graduation target that moves is not a target


def test_a_drifting_graduation_target_is_dropped_not_used(tmp_db, monkeypatch):
    """Observed live: one token's derived target collapsed 38.5 -> 0.114 SOL in 4.5 min.

    The curve-velocity floor is `graduation_sol * 0.0021`, so the floor moved 345x with
    it and the lane would have judged identical behaviour against thresholds three
    orders of magnitude apart from one minute to the next.
    """
    monkeypatch.setattr(
        S, "_observe_flow",
        lambda chain, token, curve, conn, now, basis: (curve, basis),
        raising=False,
    )
    import kaiba.ingest.token_flow as tf

    monkeypatch.setattr(tf, "latest_snapshot", lambda c, t, conn: {"graduation_sol": "38.5"})
    out = S._drop_unstable_graduation(Chain.SOL, MINT, {"graduation_sol": Decimal("0.114")}, tmp_db)
    assert out["graduation_sol"] is None
    assert "345" in out["graduation_note"] or "x since" in out["graduation_note"]


def test_a_stable_graduation_target_survives(tmp_db, monkeypatch):
    import kaiba.ingest.token_flow as tf

    monkeypatch.setattr(tf, "latest_snapshot", lambda c, t, conn: {"graduation_sol": "85.0"})
    out = S._drop_unstable_graduation(Chain.SOL, MINT, {"graduation_sol": Decimal("85.01")}, tmp_db)
    assert out["graduation_sol"] == Decimal("85.01")
    assert "graduation_note" not in out


def test_no_history_is_not_instability(tmp_db, monkeypatch):
    import kaiba.ingest.token_flow as tf

    monkeypatch.setattr(tf, "latest_snapshot", lambda c, t, conn: None)
    out = S._drop_unstable_graduation(Chain.SOL, MINT, {"graduation_sol": Decimal("85")}, tmp_db)
    assert out["graduation_sol"] == Decimal("85")


# --------------------------------------------------------------------------------------
# capturing the trade tape at scan time
#
# The pump.fun trade route serves a mint's tape only while that mint is trading (0-12 min
# idle: 200 on 11 of 11; 48-170 min idle: 503 on 37 of 37, measured 2026-09-20). A backfill
# over the 435 tokens already in the database attempted 78 and recovered 2. Tier-1 scan
# time is therefore the only moment a tape can reliably be had, which makes these the
# tests for the one chance we get per launch.
# --------------------------------------------------------------------------------------


def _flow_stub(monkeypatch, reason: str, pages: int = 1, rows: int = 4):
    """Stand in for ``token_flow.observe``, which the scanner calls to collect the tape."""
    import kaiba.ingest.token_flow as tf

    def _observe(chain, token, curve, conn=None, *, at_ms=None, **kw):
        out = dict(curve)
        out["flow_reason"] = reason
        out["flow_pages"] = pages
        out["flow_complete"] = reason in ("end_of_history", "reached_watermark")
        out["flow_rows_written"] = rows
        return out, "sol_per_swap"

    monkeypatch.setattr(tf, "observe", _observe)


def test_a_terminated_walk_at_scan_time_proves_the_tape(tmp_db, monkeypatch):
    from kaiba.ingest import tape as tape_mod

    store_token(tmp_db, MINT)
    stub_dyor(monkeypatch, fresh_dossier())
    stub_curve(monkeypatch, classic_payload())
    _flow_stub(monkeypatch, "end_of_history", pages=1)

    result = S.scan(Chain.SOL, MINT, tmp_db)

    assert result.tape_coverage == "complete"
    assert tape_mod.is_complete(Chain.SOL, MINT, tmp_db) is True
    assert result.as_dict()["tape_coverage"] == "complete"


def test_a_truncated_walk_at_scan_time_is_captured_but_not_proved(tmp_db, monkeypatch):
    """Capture is not completeness. The rows land; the claim does not."""
    from kaiba.ingest import tape as tape_mod

    store_token(tmp_db, MINT)
    stub_dyor(monkeypatch, fresh_dossier())
    stub_curve(monkeypatch, classic_payload())
    _flow_stub(monkeypatch, "page_budget_exhausted", pages=2)

    result = S.scan(Chain.SOL, MINT, tmp_db)

    assert result.tape_coverage == "partial"
    assert tape_mod.is_complete(Chain.SOL, MINT, tmp_db) is False
    record = tape_mod.record_of(Chain.SOL, MINT, tmp_db)
    assert record is not None and record.pages == 2


def test_a_partial_capture_is_queued_for_a_quick_retry_inside_the_hot_window(tmp_db, monkeypatch):
    """A mint that answered is still hot now and will not be in an hour."""
    from kaiba.ingest import tape as tape_mod

    store_token(tmp_db, MINT)
    stub_dyor(monkeypatch, fresh_dossier())
    stub_curve(monkeypatch, classic_payload())
    _flow_stub(monkeypatch, "page_budget_exhausted", pages=2)

    S.scan(Chain.SOL, MINT, tmp_db)

    record = tape_mod.record_of(Chain.SOL, MINT, tmp_db)
    assert record is not None and record.next_attempt_ms is not None
    due_in_s = (record.next_attempt_ms - now_ms()) / 1000
    assert 0 < due_in_s <= tape_mod.DEFAULT_CONFIG.partial_retry_s + 5
    assert due_in_s < tape_mod.HOT_WINDOW_ANSWERED_MAX_IDLE_MIN * 60, "retry falls outside the window"


def test_a_failed_rescan_does_not_destroy_a_tape_proved_earlier(tmp_db, monkeypatch):
    """The live regression: a 503 on re-scan overwrote the proof and coverage went 12 -> 7."""
    from kaiba.ingest import tape as tape_mod

    store_token(tmp_db, MINT)
    stub_dyor(monkeypatch, fresh_dossier())
    stub_curve(monkeypatch, classic_payload())
    _flow_stub(monkeypatch, "end_of_history", pages=1)
    S.scan(Chain.SOL, MINT, tmp_db)
    assert tape_mod.is_complete(Chain.SOL, MINT, tmp_db) is True

    S.RECENT.clear()
    _flow_stub(monkeypatch, "unavailable", pages=0)  # the hot window closed
    result = S.scan(Chain.SOL, MINT, tmp_db)

    assert result.tape_coverage == "complete"
    assert tape_mod.is_complete(Chain.SOL, MINT, tmp_db) is True
    assert tape_mod.complete_tokens(Chain.SOL, tmp_db) == [MINT]


def test_tape_bookkeeping_can_never_fail_a_scan(tmp_db, monkeypatch):
    """A scan is worth more than its bookkeeping; the rows are in `swaps` either way."""
    from kaiba.ingest import tape as tape_mod

    store_token(tmp_db, MINT)
    stub_dyor(monkeypatch, fresh_dossier())
    stub_curve(monkeypatch, classic_payload())
    _flow_stub(monkeypatch, "end_of_history", pages=1)

    def _boom(*a: Any, **k: Any):
        raise RuntimeError("token_tape is on fire")

    monkeypatch.setattr(tape_mod, "record_scan_capture", _boom)
    result = S.scan(Chain.SOL, MINT, tmp_db)

    assert result.ok is True
    assert result.curve_ok is True
    assert result.tape_coverage is None


def test_scan_stats_count_tape_coverage(tmp_db, monkeypatch):
    """A pass that captured nothing is a token lost, so it is as visible as a silent lane."""
    store_token(tmp_db, MINT)
    store_token(tmp_db, MINT_B)
    stub_dyor(monkeypatch, fresh_dossier())
    stub_curve(monkeypatch, classic_payload())

    stats = S.ScanStats()
    _flow_stub(monkeypatch, "end_of_history", pages=1)
    stats.record(S.scan(Chain.SOL, MINT, tmp_db))
    _flow_stub(monkeypatch, "unavailable", pages=0)
    stats.record(S.scan(Chain.SOL, MINT_B, tmp_db))

    snap = stats.as_dict()
    assert snap["tape_complete"] == 1
    assert snap["tape_coverage"]["complete"] == 1
    assert snap["tape_coverage"]["unavailable"] == 1


def test_a_scan_with_no_curve_records_no_tape_claim(tmp_db, monkeypatch):
    """No collection was attempted, which is different from one that failed."""
    from kaiba.ingest import tape as tape_mod

    store_token(tmp_db, MINT)
    stub_dyor(monkeypatch, fresh_dossier())
    stub_curve(monkeypatch, None)

    result = S.scan(Chain.SOL, MINT, tmp_db)

    assert result.curve_ok is False
    assert result.tape_coverage is None
    assert tape_mod.record_of(Chain.SOL, MINT, tmp_db) is None
