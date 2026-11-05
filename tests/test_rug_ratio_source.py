"""Where ``rug_ratio`` comes from, and why it was UNAVAILABLE on every dossier.

MEASURED 2026-09-21 on the live box: ``token_dossiers.rug_ratio`` had basis ``unavailable``
and value ``null`` on 4,298 of 4,298 rows, and ``lanes.sm_trenches`` fails closed on an
unknown rug ratio, so the lane could never fire. The cause is not a parse bug: GMGN's
``token security`` and ``token info`` bodies carry no rug field at all (35 / 176 keys on
Solana, 27 / 163 on bsc, read live, plus both recorded fixture pairs). The number lives on
the ``market trenches`` feed row -- the same feed whose smart-money preset applies the
``max_rug_ratio 0.3`` the lane copies.

These tests pin four things:

* the recorded security/info bodies really do lack the field (so nobody "fixes" the
  mapping back into a key that never arrives);
* ``gmgn_cli.feed_rug_ratio`` lifts exactly that one field from the token's trenches row,
  as a 0-1 ``Decimal``, and comes back UNAVAILABLE -- never 0 -- when the token is absent,
  the value is ``null`` (bsc: 172 of 180 rows) or out of range;
* ``dyor.collect_gmgn_feed`` turns it into a claim that survives ``resolve`` and lands on
  the dossier with ``PROVIDER_REPORTED`` basis and the 900 s budget, falls back to a
  stored feed row only inside that budget, and is wired into ``scan_token``;
* every number behind the wiring is labelled MEASURED / CITED / DERIVED / INVENTED;
* what a reported ``0`` means (section 7). MEASURED 2026-09-22 on the live box, two
  independent re-reads: every EVM ``rug_ratio`` ever observed is exactly 0 (bsc 12/180
  present all 0, robinhood 36/180 all 0, base 0/180, trending 50/50, signal 50/50), and on
  sol a 0 marks the unscored young token (rug==0 median age 211 s vs 771 s; 8 of 9 changes
  in a 90 s re-read were 0 -> non-zero). So both readers serve an EVM 0 as UNAVAILABLE
  with a note that says why, serve a sol 0 with the "may mean unscored" caveat, and never
  say "opaque score" alone; a stored row's ``filter_preset`` rides on the receipt.

Replay is by patching ``gmgn_cli._spawn``, the same seam ``tests/test_gmgn_cli.py`` uses,
so argv construction, caching and the limiter stay real.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
import types
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from kaiba.core.schemas import EVM_CHAINS, Chain, EventKind, EvidenceBasis, Receipt, now_ms
from kaiba.intelligence import dyor
from kaiba.providers import gmgn_cli as g

FIXTURES = Path(__file__).parent / "fixtures" / "gmgn"

SOL_MINT = "So11111111111111111111111111111111111111112"
BSC_TOKEN = "0x6d1e9a4195039b4ed9cec2b3edaef6d6ca6d7777"


# ------------------------------------------------------------------------- helpers


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def raw_from(name: str) -> g._Raw:
    f = load_fixture(name)
    return g._Raw(f["returncode"], f["stdout"], f["stderr"])


def synthetic_trenches(rows_by_category: dict[str, list[dict[str, Any]]]) -> g._Raw:
    """A ``market trenches --raw`` body built by hand; the three keys are always present."""
    body = {"new_creation": [], "near_completion": [], "completed": []}
    body.update(rows_by_category)
    return g._Raw(0, json.dumps(body) + "\n", "")


def first_feed_row() -> dict[str, Any]:
    body = json.loads(load_fixture("market_trenches")["stdout"])
    return next(row for rows in body.values() for row in (rows or []))


@pytest.fixture
def fast_limiter(monkeypatch):
    """Drop gmgn's 1.2 s pacing so one test may issue several reads; everything else real."""
    from kaiba.core import limiter as lim

    real = lim.limits_for

    def relaxed(provider: str):
        base = real(provider)
        if provider != g.PROVIDER:
            return base
        return dataclasses.replace(base, min_interval_ms=0, capacity=10_000, refill_per_s=10_000.0)

    monkeypatch.setattr(lim, "limits_for", relaxed)


@pytest.fixture
def replay(monkeypatch, fast_limiter, tmp_db):
    """Route ``_spawn`` by a needle in argv. Returns the recorded argv list."""
    calls: list[list[str]] = []

    def install(route: dict[str, str | g._Raw]):
        def fake_spawn(argv: list[str], timeout_s: float) -> g._Raw:
            calls.append(list(argv))
            for needle, target in route.items():
                if needle in argv:
                    return target if isinstance(target, g._Raw) else raw_from(target)
            raise AssertionError(f"no fixture routed for {argv}")

        monkeypatch.setattr(g, "_spawn", fake_spawn)
        return calls

    monkeypatch.setattr(g, "cli_argv", lambda: ["node", "index.js"])
    return install


def provider_errors(conn) -> list[dict[str, Any]]:
    return [
        dict(r) for r in conn.execute("SELECT * FROM events WHERE kind='provider.error'").fetchall()
    ]


def store_feed_row(conn, chain: Chain, token: str, *, age_s: int, **payload: Any) -> None:
    """Write an ``alpha.signal`` row the way ``gmgn_feeds.write_alpha`` does, back-dated."""
    body = {"provider": "gmgn", "feed": "trenches", "token": token, "chain": chain.value, **payload}
    conn.execute(
        "INSERT INTO events (ts_ms, kind, level, chain, subject, payload, trace_id, dedupe_key) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            now_ms() - age_s * 1000,
            EventKind.ALPHA_SIGNAL.value,
            "info",
            chain.value,
            token,
            json.dumps(body),
            None,
            f"test:{token}:{age_s}:{payload.get('rug_ratio')!r}",
        ),
    )
    conn.commit()


def stub_gmgn_module(monkeypatch, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType("kaiba.providers.gmgn_cli")
    module.TRENCHES_ROW_ENDPOINT = g.TRENCHES_ROW_ENDPOINT  # type: ignore[attr-defined]
    for name, value in attrs.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", module)
    return module


def offline_other_collectors(monkeypatch) -> None:
    for name in ("collect_goplus", "collect_rugcheck", "collect_dedup", "collect_bundles"):
        monkeypatch.setattr(dyor, name, lambda address, chain, conn=None: ([], [], "n/a"))


# --------------------------------------------------- 1. the field is not on security/info


@pytest.mark.parametrize(
    "name", ["token_security", "token_info", "token_security_bundled", "token_info_bundled"]
)
def test_security_and_info_bodies_carry_no_rug_field(name):
    """The mapping ``rug_ratio -> rug_ratio`` was reading a key GMGN never sends here."""
    body = json.loads(load_fixture(name)["stdout"])
    blob = json.dumps(body)
    assert not re.search(r'"[a-z_]*rug[a-z_]*"', blob), name
    assert "rug_ratio" not in g.normalize_security(body, chain=Chain.SOL)
    assert "rug_ratio" not in dyor.normalize_gmgn(body, chain=Chain.SOL)


def test_the_feed_rows_do_carry_it_as_a_zero_to_one_number():
    """Trenches, trending and signal rows all have ``rug_ratio``; the fixtures prove it."""
    for name in ("market_trenches", "market_trending", "market_signal"):
        blob = load_fixture(name)["stdout"]
        values = [Decimal(v) for v in re.findall(r'"rug_ratio":\s*"?([0-9.]+)"?', blob)]
        assert values, name
        assert all(0 <= v <= 1 for v in values), (name, values)


# -------------------------------------------------------- 2. gmgn_cli.feed_rug_ratio


def test_feed_rug_ratio_reads_the_tokens_own_trenches_row(replay, tmp_db):
    calls = replay({"trenches": "market_trenches"})
    row = first_feed_row()

    result = g.feed_rug_ratio(row["address"], Chain.SOL, conn=tmp_db)

    assert result.ok
    assert result.data == {"rug_ratio": Decimal(str(row["rug_ratio"]))}
    assert isinstance(result.data["rug_ratio"], Decimal)
    assert result.receipt.provider == "gmgn"
    assert result.receipt.endpoint == g.TRENCHES_ROW_ENDPOINT == "market.trenches.row"
    assert result.receipt.basis is EvidenceBasis.PROVIDER_REPORTED
    assert "opaque" in (result.receipt.note or "")
    # Unfiltered feed, the CLI maximum per category, on the chain asked for.
    argv = calls[-1]
    assert argv[2:4] == ["market", "trenches"]
    assert "--filter-preset" not in argv
    assert argv[argv.index("--limit") + 1] == str(g.TRENCHES_FEED_LIMIT) == "80"
    assert argv[argv.index("--chain") + 1] == "sol"


def test_only_rug_ratio_is_lifted_from_the_row(replay, tmp_db):
    """The row has top_10_holder_rate too; taking it again would conflict with ourselves."""
    replay({"trenches": "market_trenches"})
    row = first_feed_row()
    assert "top_10_holder_rate" in row  # the temptation is real
    result = g.feed_rug_ratio(row["address"], Chain.SOL, conn=tmp_db)
    assert set(result.data) == {"rug_ratio"}


def test_a_token_absent_from_the_feed_is_unavailable_and_not_an_outage(replay, tmp_db):
    replay({"trenches": "market_trenches"})

    result = g.feed_rug_ratio(SOL_MINT, Chain.SOL, conn=tmp_db)

    assert not result.ok and result.data is None
    assert result.receipt.basis is EvidenceBasis.UNAVAILABLE
    assert result.receipt.endpoint == g.TRENCHES_ROW_ENDPOINT
    assert "not among the 2 rows" in (result.receipt.note or "")
    assert provider_errors(tmp_db) == []  # GMGN answered; nothing went wrong


def test_a_null_rug_ratio_is_unavailable_never_zero(replay, tmp_db):
    """The bsc shape: the row is there, the field is ``null`` (172 of 180 rows measured)."""
    replay({"trenches": synthetic_trenches({"near_completion": [{"address": BSC_TOKEN, "rug_ratio": None}]})})

    result = g.feed_rug_ratio(BSC_TOKEN, Chain.BSC, conn=tmp_db)

    assert not result.ok and result.data is None
    assert result.receipt.basis is EvidenceBasis.UNAVAILABLE
    assert result.receipt.endpoint == g.TRENCHES_ROW_ENDPOINT
    assert "null" in (result.receipt.note or "").lower() or "None" in (result.receipt.note or "")
    assert provider_errors(tmp_db) == []


@pytest.mark.parametrize("bad", ["1.5", "-0.1", "abc", "", True, {"x": 1}])
def test_out_of_range_or_unparseable_values_are_dropped(replay, tmp_db, bad):
    replay({"trenches": synthetic_trenches({"completed": [{"address": BSC_TOKEN, "rug_ratio": bad}]})})
    result = g.feed_rug_ratio(BSC_TOKEN, Chain.BSC, conn=tmp_db)
    assert not result.ok and result.data is None
    assert result.receipt.basis is EvidenceBasis.UNAVAILABLE


@pytest.mark.parametrize(
    ("chain", "raw", "expected"),
    [
        (Chain.SOL, 0, Decimal(0)),
        (Chain.SOL, "0", Decimal(0)),
        (Chain.BSC, 1, Decimal(1)),
        (Chain.BSC, "0.25", Decimal("0.25")),
        (Chain.BSC, 0.202, Decimal("0.202")),
    ],
    ids=str,
)
def test_boundary_and_string_values_are_read_as_decimals(replay, tmp_db, chain, raw, expected):
    """A sol 0 is an answer (unlike a missing field) and 1.0 is inside the range.

    A 0 on an EVM chain is not an answer -- section 7 -- so the zero cases run on sol.
    """
    token = SOL_MINT if chain is Chain.SOL else BSC_TOKEN
    replay({"trenches": synthetic_trenches({"completed": [{"address": token, "rug_ratio": raw}]})})
    result = g.feed_rug_ratio(token, chain, conn=tmp_db)
    assert result.ok
    assert result.data["rug_ratio"] == expected


def test_evm_addresses_match_case_insensitively_and_solana_exactly(replay, tmp_db):
    checksummed = "0x6D1E9A4195039B4ED9CEC2B3EDAEF6D6CA6D7777"
    replay({"trenches": synthetic_trenches({"completed": [{"address": BSC_TOKEN, "rug_ratio": 0.2}]})})
    assert g.feed_rug_ratio(checksummed, Chain.BSC, conn=tmp_db).ok

    sol_row = first_feed_row()
    replay({"trenches": "market_trenches"})
    assert g.feed_rug_ratio(sol_row["address"], Chain.SOL, conn=tmp_db).ok
    assert not g.feed_rug_ratio(sol_row["address"].lower(), Chain.SOL, conn=tmp_db).ok


def test_a_failed_read_keeps_the_reads_own_receipt(replay, tmp_db):
    """Rate-limited: the receipt is the bare ``market.trenches`` one, so dyor can tell."""
    replay({"trenches": "rate_limited_429"})

    result = g.feed_rug_ratio(SOL_MINT, Chain.SOL, conn=tmp_db)

    assert not result.ok
    assert result.receipt.endpoint == "market.trenches"
    assert result.receipt.basis is EvidenceBasis.UNAVAILABLE
    assert [e["kind"] for e in provider_errors(tmp_db)] == ["provider.error"]


def test_the_feed_read_is_cached_so_a_burst_of_scans_shares_it(replay, tmp_db):
    calls = replay({"trenches": "market_trenches"})
    row = first_feed_row()
    first = g.feed_rug_ratio(row["address"], Chain.SOL, conn=tmp_db)
    second = g.feed_rug_ratio(row["address"], Chain.SOL, conn=tmp_db)
    assert first.receipt.basis is EvidenceBasis.PROVIDER_REPORTED
    assert second.receipt.basis is EvidenceBasis.CACHED
    assert len(calls) == 1


def test_the_feed_read_is_never_served_from_the_grace_window(replay, tmp_db, monkeypatch):
    """15-60 s old came back STALE on the first live re-scan; that earns a -15 warning.

    Inside the TTL the entry is CACHED without a spawn; past it the CLI is spawned again
    and the answer is PROVIDER_REPORTED. STALE cannot happen on this read.
    """
    from kaiba.providers import _http

    calls = replay({"trenches": "market_trenches"})
    row = first_feed_row()
    first = g.feed_rug_ratio(row["address"], Chain.SOL, conn=tmp_db)
    assert first.receipt.basis is EvidenceBasis.PROVIDER_REPORTED and len(calls) == 1
    key = "gmgn-cli " + " ".join(calls[-1][2:])
    path = _http.cache_path(g.PROVIDER, key)
    entry = json.loads(path.read_text(encoding="utf-8"))

    def age_entry(seconds: float) -> None:
        entry["fetched_ms"] = now_ms() - int(seconds * 1000)
        path.write_text(json.dumps(entry), encoding="utf-8")

    age_entry(g.TRENCHES_FEED_TTL_S - 5)
    inside = g.feed_rug_ratio(row["address"], Chain.SOL, conn=tmp_db)
    assert inside.receipt.basis is EvidenceBasis.CACHED and len(calls) == 1

    age_entry(g.TRENCHES_FEED_TTL_S + 5)  # inside the 60 s grace the ingest read tolerates
    past = g.feed_rug_ratio(row["address"], Chain.SOL, conn=tmp_db)
    assert past.receipt.basis is EvidenceBasis.PROVIDER_REPORTED
    assert len(calls) == 2
    assert g.TRENCHES_FEED_GRACE_S == 0.0
    assert g.TRENCHES_FEED_TTL_S < g._TTL["market.trenches"][0] + g._TTL["market.trenches"][1]


def test_security_properties_still_makes_exactly_two_reads(replay, tmp_db):
    """The feed read is a separate collector on purpose; it must not creep in here."""
    calls = replay({"security": "token_security", "info": "token_info"})
    assert g.security_properties(SOL_MINT, Chain.SOL, conn=tmp_db).ok
    assert sorted(argv[3] for argv in calls) == ["info", "security"]


# ------------------------------------------------------- 3. dyor.collect_gmgn_feed


def _live_result(value: Any, *, basis=EvidenceBasis.PROVIDER_REPORTED, endpoint=g.TRENCHES_ROW_ENDPOINT):
    receipt = Receipt(provider="gmgn", endpoint=endpoint, basis=basis, note="stub")
    data = None if value is None else {"rug_ratio": value}
    return g.GmgnResult(data, receipt)


def test_collector_turns_the_live_row_into_a_claim_that_reaches_the_dossier(monkeypatch, tmp_db):
    stub_gmgn_module(monkeypatch, feed_rug_ratio=lambda a, c, conn=None: _live_result(Decimal("0.12")))

    claims, receipts, status = dyor.collect_gmgn_feed(SOL_MINT, Chain.SOL, tmp_db)

    assert status == "ok"
    assert [(c.prop, c.provider, c.value) for c in claims] == [("rug_ratio", "gmgn", Decimal("0.12"))]
    resolution = dyor.resolve(claims)
    resolution.receipts.extend(receipts)
    dossier = dyor.build_dossier(SOL_MINT, Chain.SOL, resolution)
    assert dossier.rug_ratio.value == Decimal("0.12")
    assert dossier.rug_ratio.basis is EvidenceBasis.PROVIDER_REPORTED
    assert dossier.rug_ratio.receipt is not None and dossier.rug_ratio.receipt.endpoint == "market.trenches.row"
    assert dossier.rug_ratio.freshness_budget_s == dyor.RUG_RATIO_BUDGET_S == 900
    assert dossier.rug_ratio.known and not dossier.rug_ratio.stale


def test_collector_reports_absent_as_ok_and_a_failed_read_as_down(monkeypatch, tmp_db):
    stub_gmgn_module(monkeypatch, feed_rug_ratio=lambda a, c, conn=None: _live_result(None, basis=EvidenceBasis.UNAVAILABLE))
    claims, receipts, status = dyor.collect_gmgn_feed(SOL_MINT, Chain.SOL, tmp_db)
    assert claims == [] and status == "ok"
    assert [r.endpoint for r in receipts] == ["market.trenches.row"]

    stub_gmgn_module(
        monkeypatch,
        feed_rug_ratio=lambda a, c, conn=None: _live_result(None, basis=EvidenceBasis.UNAVAILABLE, endpoint="market.trenches"),
    )
    claims, receipts, status = dyor.collect_gmgn_feed(SOL_MINT, Chain.SOL, tmp_db)
    assert claims == [] and status == "down"

    def boom(a, c, conn=None):
        raise RuntimeError("cli exploded")

    stub_gmgn_module(monkeypatch, feed_rug_ratio=boom)
    claims, receipts, status = dyor.collect_gmgn_feed(SOL_MINT, Chain.SOL, tmp_db)
    assert claims == [] and status == "down"
    assert receipts and receipts[0].basis is EvidenceBasis.UNAVAILABLE


def test_collector_never_manufactures_a_number_from_an_unavailable_receipt(monkeypatch, tmp_db):
    """Data present but the receipt says UNAVAILABLE: the receipt wins, no claim."""
    stub_gmgn_module(monkeypatch, feed_rug_ratio=lambda a, c, conn=None: _live_result(Decimal("0"), basis=EvidenceBasis.UNAVAILABLE))
    claims, _receipts, _status = dyor.collect_gmgn_feed(SOL_MINT, Chain.SOL, tmp_db)
    assert claims == []


def test_collector_without_the_cli_module_falls_back_to_a_fresh_stored_row(monkeypatch, tmp_db):
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", None)
    store_feed_row(tmp_db, Chain.SOL, SOL_MINT, age_s=60, rug_ratio=0.05)

    claims, receipts, status = dyor.collect_gmgn_feed(SOL_MINT, Chain.SOL, tmp_db)

    assert status == "ok"
    assert [c.value for c in claims] == [Decimal("0.05")]
    receipt = claims[0].receipt
    assert receipt.basis is EvidenceBasis.CACHED
    assert receipt.endpoint == "feed.trenches"
    assert 55_000 <= now_ms() - receipt.observed_at_ms <= 70_000  # the row's own time, not now
    dossier = dyor.build_dossier(SOL_MINT, Chain.SOL, dyor.resolve(claims))
    assert dossier.rug_ratio.known and not dossier.rug_ratio.stale


def test_a_stored_row_older_than_the_budget_is_not_served(monkeypatch, tmp_db):
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", None)
    store_feed_row(tmp_db, Chain.SOL, SOL_MINT, age_s=dyor.RUG_RATIO_BUDGET_S + 1, rug_ratio=0.05)
    claims, _receipts, status = dyor.collect_gmgn_feed(SOL_MINT, Chain.SOL, tmp_db)
    assert claims == []
    assert status == "down"  # nothing live and nothing fresh: that is a gap, not an answer


def test_stored_rows_without_a_number_or_from_another_provider_are_ignored(monkeypatch, tmp_db):
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", None)
    store_feed_row(tmp_db, Chain.SOL, SOL_MINT, age_s=10, rug_ratio=None)
    store_feed_row(tmp_db, Chain.SOL, SOL_MINT, age_s=20, rug_ratio=1.7)
    tmp_db.execute(
        "INSERT INTO events (ts_ms, kind, level, chain, subject, payload, dedupe_key) VALUES (?,?,?,?,?,?,?)",
        (now_ms() - 5_000, EventKind.ALPHA_SIGNAL.value, "info", "sol", SOL_MINT,
         json.dumps({"provider": "someone_else", "rug_ratio": 0.01}), "test:other"),
    )
    tmp_db.commit()
    claims, _receipts, _status = dyor.collect_gmgn_feed(SOL_MINT, Chain.SOL, tmp_db)
    assert claims == []


def test_the_newest_fresh_stored_row_wins_and_a_checksummed_query_still_finds_it(monkeypatch, tmp_db):
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", None)
    # The feed writes EVM subjects lowercase; a caller passing the checksummed spelling
    # still finds the row because the lookup lowercases on EVM before comparing.
    store_feed_row(tmp_db, Chain.BSC, BSC_TOKEN, age_s=300, rug_ratio=0.4)
    checksummed = BSC_TOKEN.upper().replace("0X", "0x")
    claims, _receipts, _status = dyor.collect_gmgn_feed(checksummed, Chain.BSC, tmp_db)
    assert [c.value for c in claims] == [Decimal("0.4")]
    # A newer row wins over an older one, whatever order they were written in.
    store_feed_row(tmp_db, Chain.BSC, BSC_TOKEN, age_s=100, rug_ratio=0.2)
    claims, _receipts, _status = dyor.collect_gmgn_feed(BSC_TOKEN, Chain.BSC, tmp_db)
    assert [c.value for c in claims] == [Decimal("0.2")]


def test_the_stored_row_lookup_walks_the_subject_index_not_the_table(tmp_db):
    """229,403 events on the live box; ``lower(subject)`` would have scanned all of them."""
    plan = " ".join(
        str(row[3]) for row in tmp_db.execute(
            "EXPLAIN QUERY PLAN " + dyor.STORED_FEED_SQL, (SOL_MINT, "alpha.signal", "sol", 0)
        ).fetchall()
    )
    assert "idx_events_subj" in plan, plan
    assert "SCAN events" not in plan.replace("SEARCH events", ""), plan


def test_a_live_answer_is_preferred_over_a_stored_row(monkeypatch, tmp_db):
    stub_gmgn_module(monkeypatch, feed_rug_ratio=lambda a, c, conn=None: _live_result(Decimal("0.3")))
    store_feed_row(tmp_db, Chain.SOL, SOL_MINT, age_s=10, rug_ratio=0.01)
    claims, _receipts, _status = dyor.collect_gmgn_feed(SOL_MINT, Chain.SOL, tmp_db)
    assert [(c.value, c.receipt.endpoint) for c in claims] == [(Decimal("0.3"), "market.trenches.row")]


def test_a_stored_row_backs_up_a_token_the_live_feed_no_longer_lists(monkeypatch, tmp_db):
    stub_gmgn_module(monkeypatch, feed_rug_ratio=lambda a, c, conn=None: _live_result(None, basis=EvidenceBasis.UNAVAILABLE))
    store_feed_row(tmp_db, Chain.SOL, SOL_MINT, age_s=10, rug_ratio=0.01)
    claims, receipts, status = dyor.collect_gmgn_feed(SOL_MINT, Chain.SOL, tmp_db)
    assert status == "ok"
    assert [c.value for c in claims] == [Decimal("0.01")]
    assert [r.endpoint for r in receipts] == ["market.trenches.row", "feed.trenches"]


# --------------------------------------------------------------- 4. scan_token wiring


def test_scan_token_runs_the_feed_collector_after_gmgn(monkeypatch, tmp_db):
    order: list[str] = []

    def sec(address, chain, conn=None):
        order.append("security")
        return g.GmgnResult({"can_sell": True}, Receipt(provider="gmgn", endpoint="token.security+token.info"))

    def feed(address, chain, conn=None):
        order.append("feed")
        return _live_result(Decimal("0.07"))

    stub_gmgn_module(monkeypatch, security_properties=sec, feed_rug_ratio=feed)
    offline_other_collectors(monkeypatch)

    dossier = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)

    assert order == ["security", "feed"]
    assert dossier.rug_ratio.value == Decimal("0.07")
    assert dossier.rug_ratio.basis is EvidenceBasis.PROVIDER_REPORTED
    stored = tmp_db.execute(
        "SELECT dossier_json FROM token_dossiers WHERE chain='sol' AND address=?", (SOL_MINT,)
    ).fetchone()
    body = json.loads(stored[0])
    assert body["rug_ratio"]["value"] == "0.07" and body["rug_ratio"]["basis"] == "provider_reported"


def test_scan_token_with_gmgn_cut_out_leaves_rug_ratio_unknown_not_zero(monkeypatch, tmp_db):
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", None)
    offline_other_collectors(monkeypatch)
    dossier = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)
    assert dossier.rug_ratio.value is None
    assert dossier.rug_ratio.basis is EvidenceBasis.UNAVAILABLE


# ------------------------------------------------------------- 5. the fallback parser


def test_normalize_gmgn_still_reads_a_raw_feed_row_and_range_checks_it():
    assert dyor.normalize_gmgn({"rug_ratio": "0.2"})["rug_ratio"] == Decimal("0.2")
    assert dyor.normalize_gmgn({"rug_ratio": 0})["rug_ratio"] == Decimal(0)
    assert "rug_ratio" not in dyor.normalize_gmgn({"rug_ratio": "1.5"})
    assert "rug_ratio" not in dyor.normalize_gmgn({"rug_ratio": "-0.2"})
    assert "rug_ratio" not in dyor.normalize_gmgn({"rug_ratio": None})


def test_normalize_security_range_checks_it_the_same_way():
    dropped: list[str] = []
    assert "rug_ratio" not in g.normalize_security({"rug_ratio": "1.5"}, dropped=dropped)
    assert dropped and "rug_ratio" in dropped[0]
    assert g.normalize_security({"rug_ratio": "0.3"})["rug_ratio"] == Decimal("0.3")


# ------------------------------------------------------------------- 6. provenance


def test_every_number_behind_the_wiring_is_labelled():
    words = ("MEASURED", "CITED", "DERIVED", "INVENTED")
    assert {
        "source_endpoint", "unit", "semantics", "RUG_RATIO_BUDGET_S", "lane_threshold_0.3",
        "feed_cache_window", "evm_zero",
    } <= set(dyor.RUG_RATIO_PROVENANCE)
    assert dyor.RUG_RATIO_PROVENANCE["evm_zero"].startswith("INVENTED")
    assert "never within 30 s" not in dyor.RUG_RATIO_PROVENANCE["feed_cache_window"]  # was not measured
    for name, note in dyor.RUG_RATIO_PROVENANCE.items():
        assert note.startswith(words), name
        if "INVENTED" in note:
            assert "settled" in note.lower(), f"{name} is INVENTED but never says what would settle it"
    assert "PROVIDER_REPORTED" in dyor.RUG_RATIO_PROVENANCE["semantics"]
    assert "DERIVED" in dyor.RUG_RATIO_PROVENANCE["RUG_RATIO_BUDGET_S"]


def test_the_provider_source_notes_carry_the_measurements():
    src = Path(g.__file__).read_text(encoding="utf-8")
    for needle in ("MEASURED", "180/180 sol", "8/180", "PROVIDER_REPORTED", "0 -> 0.682"):
        assert needle in src, needle
    assert "MEASURED" in (dyor.collect_gmgn_feed.__doc__ or "")
    assert "4,298 of 4,298" in (dyor.collect_gmgn_feed.__doc__ or "")


# ------------------------------------------------------- 7. what a reported 0 means
#
# MEASURED 2026-09-22 on the live box, two independent re-reads: every EVM rug_ratio ever
# observed is exactly 0 and category-linked (bsc 12/180 present, all 0; robinhood 36/180,
# all 0; base 0/180; trending 50/50 int 0; signal 50/50 int 0). On sol, 0 marks the young
# unscored token (rug==0 group median age 211 s, 54/95 first-time creators; rug>0 group
# 771 s) and in one 90 s re-read 8 of 9 changes were 0 -> non-zero (0.041..0.270). Round 1
# served that 0 as a MEASURED score (basis CACHED / known=True; the lane payload said
# ``rug_ratio_basis: measured``). The wording below is literal on purpose: a reader that
# drops or rewords it fails here, not only in its own module's test.

EVM_ZERO_NOTE = "reported 0 = unscored on this chain (INVENTED inference: 100% of EVM values observed are 0)"
SOL_ZERO_NOTE = "zero may mean unscored (young tokens flip 0 -> non-zero within minutes)"
EVM_TEST_CHAINS = sorted(EVM_CHAINS, key=str)
ABSENT = "<absent>"


def test_the_zero_wording_is_one_vocabulary_in_both_readers():
    assert g.EVM_ZERO_RUG_NOTE == dyor.EVM_ZERO_RUG_NOTE == EVM_ZERO_NOTE
    assert g.SOL_ZERO_RUG_NOTE == dyor.SOL_ZERO_RUG_NOTE == SOL_ZERO_NOTE
    assert "INVENTED" in EVM_ZERO_NOTE  # an inference, and it says so
    assert "unscored" in SOL_ZERO_NOTE and "0 -> non-zero" in SOL_ZERO_NOTE


def test_the_evm_verdict_comes_from_the_chain_enum_not_a_string_list():
    assert set(EVM_CHAINS) == {Chain.BSC, Chain.ROBINHOOD, Chain.BASE, Chain.ETH, Chain.ARC, Chain.STABLE}
    for chain in EVM_CHAINS:
        assert g._zero_means_unscored(chain, Decimal(0)), chain
        assert g._zero_means_unscored(chain.value, Decimal("0.0")), chain  # the CLI's spelling too
        assert not g._zero_means_unscored(chain, Decimal("0.2")), chain
    assert not g._zero_means_unscored(Chain.SOL, Decimal(0))
    assert not g._zero_means_unscored("sol", Decimal(0))
    assert not g._zero_means_unscored("not-a-chain", Decimal(0))
    assert not g._zero_means_unscored(None, Decimal(0))


# --- the live reader


@pytest.mark.parametrize("raw", [0, "0", 0.0, "0.0"], ids=repr)
@pytest.mark.parametrize("chain", EVM_TEST_CHAINS, ids=str)
def test_a_live_evm_zero_is_unavailable_with_the_unscored_note(replay, tmp_db, chain, raw):
    replay({"trenches": synthetic_trenches({"new_creation": [{"address": BSC_TOKEN, "rug_ratio": raw}]})})

    result = g.feed_rug_ratio(BSC_TOKEN, chain, conn=tmp_db)

    assert not result.ok and result.data is None  # never {"rug_ratio": 0}
    assert result.receipt.basis is EvidenceBasis.UNAVAILABLE
    assert result.receipt.endpoint == g.TRENCHES_ROW_ENDPOINT  # the feed answered: not an outage
    assert (result.receipt.note or "").startswith(EVM_ZERO_NOTE)
    assert provider_errors(tmp_db) == []


@pytest.mark.parametrize("chain", EVM_TEST_CHAINS, ids=str)
def test_a_live_evm_non_zero_is_served_provider_reported(replay, tmp_db, chain):
    replay({"trenches": synthetic_trenches({"completed": [{"address": BSC_TOKEN, "rug_ratio": 0.2}]})})

    result = g.feed_rug_ratio(BSC_TOKEN, chain, conn=tmp_db)

    assert result.ok and result.data == {"rug_ratio": Decimal("0.2")}
    assert result.receipt.basis is EvidenceBasis.PROVIDER_REPORTED
    note = result.receipt.note or ""
    assert "opaque" in note and "unscored" not in note


@pytest.mark.parametrize("raw", [0, "0", 0.0], ids=repr)
def test_a_live_sol_zero_is_served_with_the_unscored_caveat_never_opaque_alone(replay, tmp_db, raw):
    replay({"trenches": synthetic_trenches({"new_creation": [{"address": SOL_MINT, "rug_ratio": raw}]})})

    result = g.feed_rug_ratio(SOL_MINT, Chain.SOL, conn=tmp_db)

    assert result.ok and result.data == {"rug_ratio": Decimal(0)}
    assert result.receipt.basis is EvidenceBasis.PROVIDER_REPORTED
    note = result.receipt.note or ""
    assert SOL_ZERO_NOTE in note
    assert "opaque" in note and note.index(SOL_ZERO_NOTE) < note.index("opaque")
    assert EVM_ZERO_NOTE not in note


def test_a_live_sol_non_zero_carries_no_unscored_caveat(replay, tmp_db):
    replay({"trenches": synthetic_trenches({"new_creation": [{"address": SOL_MINT, "rug_ratio": 0.041}]})})
    result = g.feed_rug_ratio(SOL_MINT, Chain.SOL, conn=tmp_db)
    assert result.ok and result.data == {"rug_ratio": Decimal("0.041")}
    assert "unscored" not in (result.receipt.note or "")


@pytest.mark.parametrize("raw", [ABSENT, None, "1.5", "-0.1", "abc", "", True], ids=repr)
@pytest.mark.parametrize("chain", [Chain.BSC, Chain.SOL], ids=str)
def test_null_absent_and_out_of_range_stay_unavailable_and_are_not_called_unscored(
    replay, tmp_db, chain, raw
):
    token = BSC_TOKEN if chain is Chain.BSC else SOL_MINT
    row: dict[str, Any] = {"address": token}
    if raw is not ABSENT:
        row["rug_ratio"] = raw
    replay({"trenches": synthetic_trenches({"completed": [row]})})

    result = g.feed_rug_ratio(token, chain, conn=tmp_db)

    assert not result.ok and result.data is None
    assert result.receipt.basis is EvidenceBasis.UNAVAILABLE
    assert EVM_ZERO_NOTE not in (result.receipt.note or "")  # absent is absent, not a reported 0


def test_the_recorded_sol_row_that_carries_a_zero_is_served_with_the_caveat(replay, tmp_db):
    """The recorded feed's first row is a sol 0 (``first_feed_row``): served, and it says so."""
    replay({"trenches": "market_trenches"})
    row = first_feed_row()
    assert Decimal(str(row["rug_ratio"])) == 0
    result = g.feed_rug_ratio(row["address"], Chain.SOL, conn=tmp_db)
    assert result.ok and SOL_ZERO_NOTE in (result.receipt.note or "")


# --- the stored-row reader


@pytest.mark.parametrize("chain", EVM_TEST_CHAINS, ids=str)
def test_a_stored_evm_zero_is_unavailable_with_the_note_and_never_a_claim(
    monkeypatch, tmp_db, caplog, chain
):
    import logging

    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", None)
    store_feed_row(tmp_db, chain, BSC_TOKEN, age_s=60, rug_ratio=0)

    read = dyor._stored_feed_rug_ratio_read(tmp_db, BSC_TOKEN, chain)

    assert read is not None
    value, receipt = read
    assert value is None
    assert receipt.basis is EvidenceBasis.UNAVAILABLE
    assert receipt.endpoint == "feed.trenches"
    assert (receipt.note or "").startswith(EVM_ZERO_NOTE)
    assert 55_000 <= now_ms() - receipt.observed_at_ms <= 70_000  # the row's own time
    # The narrowed reader the collector calls today drops the verdict (and logs it), never the 0.
    caplog.set_level(logging.INFO, logger=dyor.log.name)
    assert dyor._stored_feed_rug_ratio(tmp_db, BSC_TOKEN, chain) is None
    assert EVM_ZERO_NOTE in caplog.text
    claims, _receipts, _status = dyor.collect_gmgn_feed(BSC_TOKEN, chain, tmp_db)
    assert claims == []
    dossier = dyor.build_dossier(BSC_TOKEN, chain, dyor.resolve(claims))
    assert dossier.rug_ratio.value is None
    assert dossier.rug_ratio.basis is EvidenceBasis.UNAVAILABLE and not dossier.rug_ratio.known


def test_a_stored_evm_non_zero_is_still_served_cached(monkeypatch, tmp_db):
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", None)
    store_feed_row(tmp_db, Chain.BSC, BSC_TOKEN, age_s=60, rug_ratio=0.2)
    value, receipt = dyor._stored_feed_rug_ratio(tmp_db, BSC_TOKEN, Chain.BSC)
    assert value == Decimal("0.2") and receipt.basis is EvidenceBasis.CACHED
    assert "unscored" not in (receipt.note or "")
    claims, _receipts, status = dyor.collect_gmgn_feed(BSC_TOKEN, Chain.BSC, tmp_db)
    assert status == "ok" and [c.value for c in claims] == [Decimal("0.2")]


@pytest.mark.parametrize("raw", [0, 0.0], ids=repr)
def test_a_stored_sol_zero_is_served_with_the_unscored_caveat(monkeypatch, tmp_db, raw):
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", None)
    store_feed_row(tmp_db, Chain.SOL, SOL_MINT, age_s=60, rug_ratio=raw)
    value, receipt = dyor._stored_feed_rug_ratio(tmp_db, SOL_MINT, Chain.SOL)
    assert value == Decimal(0) and receipt.basis is EvidenceBasis.CACHED
    note = receipt.note or ""
    assert SOL_ZERO_NOTE in note and "opaque" in note and EVM_ZERO_NOTE not in note
    assert note.index(SOL_ZERO_NOTE) < note.index("opaque")
    claims, _receipts, status = dyor.collect_gmgn_feed(SOL_MINT, Chain.SOL, tmp_db)
    assert status == "ok" and [c.value for c in claims] == [Decimal(0)]
    assert SOL_ZERO_NOTE in (claims[0].receipt.note or "")


def test_the_newest_stored_verdict_decides_even_when_it_is_unscored(monkeypatch, tmp_db):
    """A newer EVM 0 is "no score as of then"; an older score does not outrank it. A null
    newer row is not an answer at all and is skipped, as before."""
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", None)
    store_feed_row(tmp_db, Chain.BSC, BSC_TOKEN, age_s=300, rug_ratio=0.2)
    store_feed_row(tmp_db, Chain.BSC, BSC_TOKEN, age_s=100, rug_ratio=0)
    read = dyor._stored_feed_rug_ratio_read(tmp_db, BSC_TOKEN, Chain.BSC)
    assert read is not None and read[0] is None and read[1].basis is EvidenceBasis.UNAVAILABLE
    assert dyor._stored_feed_rug_ratio(tmp_db, BSC_TOKEN, Chain.BSC) is None

    store_feed_row(tmp_db, Chain.SOL, SOL_MINT, age_s=300, rug_ratio=0.2)
    store_feed_row(tmp_db, Chain.SOL, SOL_MINT, age_s=100, rug_ratio=None)
    value, _receipt = dyor._stored_feed_rug_ratio(tmp_db, SOL_MINT, Chain.SOL)
    assert value == Decimal("0.2")


def test_filter_preset_rides_on_the_stored_receipt(monkeypatch, tmp_db):
    """The ingester polls with the smart-money preset (server-side max_rug_ratio 0.3), so a
    stored fallback is < 0.3 by construction and its receipt must say so."""
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", None)
    store_feed_row(tmp_db, Chain.SOL, SOL_MINT, age_s=60, rug_ratio=0.05, filter_preset="smart-money")
    value, receipt = dyor._stored_feed_rug_ratio(tmp_db, SOL_MINT, Chain.SOL)
    assert value == Decimal("0.05")
    note = receipt.note or ""
    assert "filter_preset=smart-money" in note and "< 0.3 by construction" in note
    assert note.index("rug_ratio=0.05") < note.index("filter_preset")

    store_feed_row(tmp_db, Chain.BSC, BSC_TOKEN, age_s=60, rug_ratio=0, filter_preset="smart-money")
    _value, receipt = dyor._stored_feed_rug_ratio_read(tmp_db, BSC_TOKEN, Chain.BSC)
    note = receipt.note or ""
    assert note.startswith(EVM_ZERO_NOTE)
    assert "filter_preset=smart-money" in note and "< 0.3 by construction" in note


@pytest.mark.parametrize("preset", [ABSENT, None, "", "   ", True, 7, {"x": 1}], ids=repr)
def test_a_stored_row_without_a_usable_filter_preset_gets_no_preset_clause(monkeypatch, tmp_db, preset):
    """Rows written before the ingester carried the key, and anything that is not a name."""
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", None)
    extra = {} if preset is ABSENT else {"filter_preset": preset}
    store_feed_row(tmp_db, Chain.SOL, SOL_MINT, age_s=60, rug_ratio=0.05, **extra)
    value, receipt = dyor._stored_feed_rug_ratio(tmp_db, SOL_MINT, Chain.SOL)
    assert value == Decimal("0.05")
    assert "filter_preset" not in (receipt.note or "")
    assert "by construction" not in (receipt.note or "")


def test_an_unknown_preset_is_named_but_earns_no_ceiling_claim(monkeypatch, tmp_db):
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", None)
    store_feed_row(tmp_db, Chain.SOL, SOL_MINT, age_s=60, rug_ratio=0.5, filter_preset="whales")
    _value, receipt = dyor._stored_feed_rug_ratio(tmp_db, SOL_MINT, Chain.SOL)
    note = receipt.note or ""
    assert "filter_preset=whales" in note and "< 0.3" not in note


def test_the_verdict_and_the_caveat_survive_the_300_char_note_cap(monkeypatch, tmp_db):
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", None)
    long_feed = "f" * 250
    store_feed_row(
        tmp_db, Chain.BSC, BSC_TOKEN, age_s=60, rug_ratio=0, feed=long_feed, filter_preset="smart-money"
    )
    _value, receipt = dyor._stored_feed_rug_ratio_read(tmp_db, BSC_TOKEN, Chain.BSC)
    assert len(receipt.note or "") <= 300 and (receipt.note or "").startswith(EVM_ZERO_NOTE)

    store_feed_row(
        tmp_db, Chain.SOL, SOL_MINT, age_s=899, rug_ratio=0.12345678901234567,
        feed=long_feed, filter_preset="smart-money",
    )
    _value, receipt = dyor._stored_feed_rug_ratio(tmp_db, SOL_MINT, Chain.SOL)
    assert len(receipt.note or "") <= 300 and "< 0.3 by construction" in (receipt.note or "")
    store_feed_row(tmp_db, Chain.SOL, SOL_MINT, age_s=10, rug_ratio=0, feed=long_feed, filter_preset="smart-money")
    _value, receipt = dyor._stored_feed_rug_ratio(tmp_db, SOL_MINT, Chain.SOL)
    assert SOL_ZERO_NOTE in (receipt.note or "") and "< 0.3 by construction" in (receipt.note or "")


# --- end to end


def test_scan_token_on_bsc_reports_a_live_zero_as_unavailable_end_to_end(replay, tmp_db, monkeypatch):
    """The path the verifiers traced -- feed row 0 -> reader -> collector -> resolve -> dossier
    -> stored JSON -- with the real ``gmgn_cli`` in it. Nothing on it calls the 0 measured."""
    replay(
        {
            "security": "token_security",
            "info": "token_info",
            "trenches": synthetic_trenches({"new_creation": [{"address": BSC_TOKEN, "rug_ratio": 0}]}),
        }
    )
    offline_other_collectors(monkeypatch)

    dossier = dyor.scan_token(BSC_TOKEN, Chain.BSC, conn=tmp_db)

    assert dossier.rug_ratio.value is None
    assert dossier.rug_ratio.basis is EvidenceBasis.UNAVAILABLE and not dossier.rug_ratio.known
    notes = [r.note or "" for r in dossier.receipts if r.endpoint == "market.trenches.row"]
    assert notes and notes[0].startswith(EVM_ZERO_NOTE)
    stored = json.loads(
        tmp_db.execute(
            "SELECT dossier_json FROM token_dossiers WHERE chain='bsc' AND address=?", (BSC_TOKEN,)
        ).fetchone()[0]
    )
    assert stored["rug_ratio"]["value"] is None and stored["rug_ratio"]["basis"] == "unavailable"
    assert EVM_ZERO_NOTE in json.dumps(stored)  # the reason travels with the record
