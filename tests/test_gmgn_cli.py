"""Offline tests for the gmgn-cli provider adapter.

Everything here replays recorded CLI output by patching one seam — ``gmgn_cli._spawn`` —
so the tests exercise the real argv construction, classification, caching, limiter and
receipt logic without a network call or a Node process. Fixtures under
``tests/fixtures/gmgn/`` were recorded from gmgn-cli 1.6.1 on 2026-09-20; the two marked
``_synthetic`` could not be recorded (see the fixture's ``_why``).

The one test that touches the real CLI is marked ``live`` and only runs read commands.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from kaiba.core.db import fetch_all
from kaiba.core.limiter import Priority, RateLimited
from kaiba.core.schemas import Chain, EvidenceBasis
from kaiba.providers import gmgn_cli as g

FIXTURES = Path(__file__).parent / "fixtures" / "gmgn"


# --------------------------------------------------------------------------- helpers


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def raw_from(name: str) -> g._Raw:
    f = load_fixture(name)
    return g._Raw(f["returncode"], f["stdout"], f["stderr"])


@pytest.fixture
def fast_limiter(monkeypatch):
    """Keep the real limiter but drop its 1.2s pacing, so a test can make two calls.

    Only the pacing and the bucket size change; weights, families and cooldowns stay real,
    which is what these tests are actually about. ``test_real_min_interval_is_honoured``
    deliberately runs without this.
    """
    import dataclasses

    from kaiba.core import limiter as lim

    real = lim.limits_for

    def relaxed(provider: str):
        base = real(provider)
        if provider != g.PROVIDER:
            return base
        return dataclasses.replace(base, min_interval_ms=0, capacity=10_000, refill_per_s=10_000.0)

    # config/risk.yaml overrides DEFAULTS, so the whole lookup has to be wrapped.
    monkeypatch.setattr(lim, "limits_for", relaxed)
    return relaxed


@pytest.fixture
def replay(monkeypatch, fast_limiter):
    """Patch the subprocess boundary. Returns a recorder of the argv actually built."""

    calls: list[list[str]] = []

    def install(fixture_name: str | None = None, raw: g._Raw | None = None):
        chosen = raw if raw is not None else raw_from(fixture_name or "")

        def fake_spawn(argv: list[str], timeout_s: float) -> g._Raw:
            calls.append(list(argv))
            return chosen

        monkeypatch.setattr(g, "_spawn", fake_spawn)
        return calls

    monkeypatch.setattr(g, "cli_argv", lambda: ["node", "index.js"])
    install.calls = calls  # type: ignore[attr-defined]
    return install


@pytest.fixture
def replay_many(monkeypatch, fast_limiter):
    """Like ``replay`` but dispatches on argv, for a helper that issues two reads."""

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
    install.calls = calls  # type: ignore[attr-defined]
    return install


@pytest.fixture
def wsol_pair(replay_many):
    """The recorded WSOL ``token security`` + ``token info`` pair, routed by subcommand."""
    return replay_many({"security": "token_security", "info": "token_info"})


@pytest.fixture(autouse=True)
def _isolated(tmp_db):
    """Every test gets a migrated DB and its own cache dir, via the shared tmp_db fixture."""
    return tmp_db


def events_of(conn, kind: str = "provider.error") -> list[dict[str, Any]]:
    return fetch_all(conn, "SELECT * FROM events WHERE kind=? ORDER BY id", (kind,))


# ------------------------------------------------------- the recorded command surface


ALL_FIXTURES = sorted(p.stem for p in FIXTURES.glob("*.json"))


def test_fixtures_are_present():
    assert len(ALL_FIXTURES) >= 20, ALL_FIXTURES


def test_fixtures_carry_no_credentials():
    """A recorded fixture must never smuggle a key into the repository."""
    secrets = [v for v in (os.environ.get("GMGN_API_KEY"), os.environ.get("GMGN_PRIVATE_KEY")) if v]
    for name in ALL_FIXTURES:
        blob = (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
        low = blob.lower()
        for token in ("api_key", "apikey", "private_key", "signature="):
            assert token not in low, f"{name} mentions {token}"
        for secret in secrets:
            assert secret not in blob, f"{name} contains a live credential"


def test_synthetic_fixtures_are_labelled():
    """Anything not recorded from the real CLI must say so, so nobody reads it as evidence."""
    synthetic = {n for n in ALL_FIXTURES if load_fixture(n).get("_synthetic")}
    # ``quote_ok`` left this set on 2026-09-21: it is now a live PRO recording. The other
    # two cannot be recorded — a plan refusal needs an account that lacks the plan, and
    # unparseable stdout needs the CLI to malfunction.
    assert synthetic == {"quote_plan_refused", "unparseable_stdout"}
    for name in synthetic:
        assert load_fixture(name).get("_why")


@pytest.mark.parametrize("group,command", sorted(g._ALLOWED))
def test_allowlist_is_read_only(group, command):
    assert (group, command) in g._ALLOWED
    assert group != "swap"
    assert (group, command) not in {("order", "get"), ("order", "strategy")}


def test_run_read_is_the_escape_hatch_for_uncovered_flags(replay, tmp_db):
    """A read the typed helpers do not model still goes through the allowlist and limiter."""
    calls = replay("market_trending")
    data, receipt = g.run_read(
        "market.trending",
        ["market", "trending", "--chain", "sol", "--min-insider-rate", "0.01"],
        conn=tmp_db,
    )
    assert isinstance(data, list)
    assert receipt.endpoint == "market.trending"
    assert "--min-insider-rate" in calls[0]
    assert fetch_all(tmp_db, "SELECT * FROM provider_calls", ())[0]["endpoint"] == "market.trending"


def test_caller_supplied_connection_is_used_for_events(replay, tmp_db):
    replay("error_bad_address")
    g.token_info("AAAA", conn=tmp_db)
    assert events_of(tmp_db)


def test_money_moving_commands_are_unreachable(replay):
    """The executor owns swaps. This module must refuse even when asked directly."""
    replay("token_info")
    for args in (
        ["swap", "--chain", "sol"],
        ["multi-swap", "--chain", "sol"],
        ["order", "get", "--order-id", "abc"],
        ["order", "strategy", "list"],
        ["cooking", "create"],
        ["config"],
    ):
        data, receipt = g.run_read("token.info", args)
        assert data is None
        assert receipt.basis is EvidenceBasis.UNAVAILABLE
        assert "read-only" in (receipt.note or "") or "too short" in (receipt.note or "")


# ------------------------------------------------------------------- happy path reads


def test_token_info_returns_payload_and_receipt(replay):
    calls = replay("token_info")
    result = g.token_info("So11111111111111111111111111111111111111112")

    assert result.ok and bool(result) is True
    assert result.data["symbol"] == "WSOL"
    assert result.receipt.provider == "gmgn"
    assert result.receipt.endpoint == "token.info"
    assert result.receipt.basis is EvidenceBasis.PROVIDER_REPORTED
    assert result.receipt.response_digest

    # tuple unpacking still works, as the contract's `(payload, Receipt)` implies
    data, receipt = result
    assert data is result.data and receipt is result.receipt

    argv = calls[0]
    assert argv[:2] == ["node", "index.js"]
    assert argv[-1] == "--raw"
    assert "token" in argv and "info" in argv
    assert "--chain" in argv and argv[argv.index("--chain") + 1] == "sol"


def test_token_security_is_an_object(replay):
    replay("token_security")
    result = g.token_security("So11111111111111111111111111111111111111112")
    assert result.ok
    assert result.data["burn_status"] == "burn"


def test_token_holders_unwraps_the_list(replay):
    replay("token_holders_full")
    result = g.token_holders("CVoNMSYK9mVd3TzAutnrRTKmptQUQ7r9qBg1ejb7vVd2", limit=3)
    assert isinstance(result.data, list)
    assert len(result.data) == 3


def test_token_traders_unwraps_the_list(replay):
    replay("token_traders_full")
    result = g.token_traders("CVoNMSYK9mVd3TzAutnrRTKmptQUQ7r9qBg1ejb7vVd2", limit=3)
    assert isinstance(result.data, list) and result.data


def test_empty_list_is_data_not_a_failure(replay, tmp_db):
    """GMGN really does return `{"list": []}` for WSOL. That is an answer, not an outage."""
    replay("token_holders_empty")
    result = g.token_holders("So11111111111111111111111111111111111111112")
    assert result.data == []
    assert result.ok
    assert result.receipt.basis is EvidenceBasis.PROVIDER_REPORTED
    assert events_of(tmp_db) == []


def test_market_trending_strips_the_code_data_envelope(replay):
    replay("market_trending")
    result = g.market_trending(interval="1h", limit=3)
    assert isinstance(result.data, list)
    assert result.data[0]["chain"] == "sol"


def test_market_trenches_keeps_all_three_categories(replay):
    replay("market_trenches")
    result = g.market_trenches(types=["completed"])
    assert set(result.data) == {"completed", "near_completion", "new_creation"}


def test_market_signal_is_a_top_level_list(replay):
    replay("market_signal")
    result = g.market_signal(signal_types=[1])
    assert isinstance(result.data, list)


def test_market_search_uses_the_query_flag(replay):
    calls = replay("market_search")
    result = g.market_search("WSOL")
    assert set(result.data) == {"coins", "wallets"}
    assert "--query" in calls[0]
    assert "--address" not in calls[0]


def test_portfolio_stats_accepts_a_single_wallet(replay):
    calls = replay("portfolio_stats")
    result = g.portfolio_stats("5gfn3DiSwaxXHk3miXKz7MoapN3UmAh3TDnXoKCvVQL4")
    assert result.data["wallet_address"].startswith("5gf")
    assert calls[0].count("--wallet") == 1


def test_portfolio_stats_repeats_the_wallet_flag(replay):
    calls = replay("portfolio_stats")
    g.portfolio_stats(["AAAA1111", "BBBB2222"], period="30d")
    assert calls[0].count("--wallet") == 2
    assert calls[0][calls[0].index("--period") + 1] == "30d"


def test_portfolio_profits_unwraps_list(replay):
    replay("portfolio_profits")
    result = g.portfolio_profits(["5gfn3DiSwaxXHk3miXKz7MoapN3UmAh3TDnXoKCvVQL4"])
    assert isinstance(result.data, list) and result.data[0]["buy"] == 752


def test_portfolio_activity_unwraps_activities(replay):
    replay("portfolio_activity")
    result = g.portfolio_activity("5gfn3DiSwaxXHk3miXKz7MoapN3UmAh3TDnXoKCvVQL4", limit=3)
    assert isinstance(result.data, list)
    assert result.data[0]["event_type"] in {"buy", "sell"}


def test_track_smartmoney_unwraps_list(replay):
    replay("track_smartmoney")
    result = g.track_smartmoney(limit=3)
    assert isinstance(result.data, list) and result.data[0]["side"] in {"buy", "sell"}


def test_track_kol_unwraps_list(replay):
    replay("track_kol")
    result = g.track_kol(limit=3)
    assert isinstance(result.data, list) and result.data


def test_track_follow_wallet_builds_repeatable_filters(replay):
    calls = replay("track_smartmoney")
    g.track_follow_wallet(wallet="AAAA", filters=["smart", "kol"], min_amount_usd=100)
    argv = calls[0]
    assert argv.count("--filter") == 2
    assert argv[argv.index("--min-amount-usd") + 1] == "100"


def test_account_info_takes_no_chain_and_stays_read_only(replay):
    """``/v1/user/info`` — the cheapest read that answers 'is this key still valid'."""
    calls = replay(raw=g._Raw(0, json.dumps({"code": 0, "data": {"wallets": []}}), ""))
    result = g.account_info()
    assert result.ok
    argv = calls[0]
    assert argv[-1] == "--raw"
    assert "--chain" not in argv
    assert ("portfolio", "info") in g._ALLOWED
    for forbidden in ("swap", "order", "cooking", "config"):
        assert forbidden not in argv


def test_optional_flags_are_omitted_when_none(replay):
    calls = replay("token_holders_full")
    g.token_holders("AAAA")
    assert "--tag" not in calls[0]
    assert "--order-by" not in calls[0]


# --------------------------------------------------------------- security properties
#
# The regression these guard is not a crash. GMGN contributed *zero* fields to every
# dossier ever built because ``dyor._unwrap_gmgn`` sees the ``can_sell`` key in GMGN's
# raw ``token security`` body, concludes the payload already speaks our vocabulary, skips
# the translator, and hands on a single ``can_sell=0`` that ``resolve`` then discards for
# being an ``int``. Nothing anywhere raised. That is why these assert on *types* and on
# the presence of specific fields, not just on the call succeeding.


def test_security_properties_merges_both_reads(wsol_pair):
    result = g.security_properties("So11111111111111111111111111111111111111112")

    assert result.ok
    assert result.receipt.endpoint == "token.security+token.info"
    subcommands = [argv[argv.index("token") + 1] for argv in wsol_pair]
    assert sorted(subcommands) == ["info", "security"]


def test_security_properties_unlocks_bundler_and_sniper(wsol_pair):
    """The two fields that were UNAVAILABLE on 615 of 615 dossiers."""
    props = g.security_properties("So11111111111111111111111111111111111111112").data

    assert "bundler_pct" in props
    assert "sniper_pct" in props
    # WSOL really has no bundlers: a *reported* zero is an answer, unlike a missing field.
    assert props["bundler_pct"] == Decimal(0)
    assert props["sniper_pct"] == Decimal("0.0115477")


def test_security_properties_also_unlocks_top10_pct(wsol_pair):
    """Unknown on 97% of dossiers for the same reason, and fixed by the same wiring."""
    props = g.security_properties("So11111111111111111111111111111111111111112").data
    assert props["top10_pct"] == Decimal("1.06259")


def test_booleans_are_real_bools_not_gmgns_ints(wsol_pair):
    """``resolve`` drops a boolean claim that is not a ``bool``; ints here changed nothing."""
    props = g.security_properties("So11111111111111111111111111111111111111112").data
    for prop in ("can_sell", "mint_authority_revoked", "freeze_authority_revoked"):
        assert isinstance(props[prop], bool), f"{prop} is {type(props[prop]).__name__}"
        assert props[prop] is True


def test_dyor_now_gets_claims_that_survive_resolution(wsol_pair, tmp_db):
    """End to end through the consumer, which this module may not edit.

    ``dyor.collect_gmgn`` prefers the ``security_properties`` entry point over
    ``token_security``; before it existed the same call resolved to ``{}``.
    """
    from kaiba.core.schemas import Chain as C
    from kaiba.intelligence import dyor

    claims, _receipts, status = dyor.collect_gmgn(
        "So11111111111111111111111111111111111111112", C.SOL, tmp_db
    )
    assert status == "ok"
    resolved = dyor.resolve(claims).values
    assert resolved["bundler_pct"] == Decimal(0)
    assert resolved["sniper_pct"] == Decimal("0.0115477")
    assert resolved["top10_pct"] == Decimal("1.06259")
    assert resolved["can_sell"] is True
    for gone in ("bundler_pct", "sniper_pct", "top10_pct"):
        assert gone not in dyor.resolve(claims).unknown


def test_a_raw_body_converts_even_without_an_adapter_module(wsol_pair):
    """Kept as the counter-example, updated 2026-09-22 when the premise stopped holding.

    This asserted that the path produced ``{}``. That WAS true, and it was the bug:
    ``_unwrap_gmgn`` short-circuited on "payload shares a key with our vocabulary", every
    raw gmgn body does (``can_sell`` on security, ``symbol``/``name``/``holder_count`` on
    info), so the body was returned untranslated and ``resolve`` discarded all of it --
    including ``can_sell: 0``, which is an int and not the ``bool`` ``resolve`` keeps.

    With the probe corrected, a module that exposes no ``normalize_security`` (``object()``
    here) now falls through to the built-in ``normalize_gmgn``, which converts. So the
    counter-example is no longer "nothing comes out"; it is "even the fallback works".
    """
    from kaiba.core.schemas import EvidenceBasis as EB
    from kaiba.core.schemas import Receipt as R
    from kaiba.intelligence import dyor

    raw = json.loads(load_fixture("token_security")["stdout"])
    props, receipt = dyor._unwrap_gmgn((raw, R(provider="gmgn", endpoint="token.security",
                                               basis=EB.PROVIDER_REPORTED)), object())
    values = dyor.resolve(dyor._claims_from(props, "gmgn", receipt)).values
    assert values, "the fallback normaliser produced nothing"
    assert isinstance(values.get("can_sell"), bool), (
        "can_sell must survive as a real bool; as an int resolve() drops it"
    )


@pytest.mark.parametrize(
    "symbol,bundler,top10",
    [("xPad", Decimal("12.93"), Decimal("9.33")), ("Claimr", Decimal("11.85"), Decimal("23.75"))],
)
def test_real_non_zero_magnitudes_from_a_recorded_payload(symbol, bundler, top10):
    """WSOL is all zeros, so the arithmetic is checked against real bundled tokens.

    These rows were recorded from ``market signal`` and carry
    ``bundler_trader_amount_rate``, whose 0-1 scale is observed rather than assumed.
    """
    rows = json.loads(load_fixture("market_signal")["stdout"])
    body = next(r["data"] for r in rows if r["data"].get("symbol") == symbol)
    props = g.normalize_security(body)
    assert props["bundler_pct"] == bundler
    assert props["top10_pct"] == top10


def test_a_really_bundled_launch_reads_as_really_bundled(replay_many):
    """Recorded live from a pump.fun mint with a 51.57% bundle share.

    WSOL is all zeros, so without this fixture nothing proves the scale end to end. This
    token is over ``curve-velocity``'s ``max_bundler_pct: 20`` by 2.5x, which is the case
    the gate exists for and the case that used to arrive as ``UNAVAILABLE``.
    """
    replay_many({"security": "token_security_bundled", "info": "token_info_bundled"})
    props = g.security_properties("C2yUFYGox1ggub6eVfWC8hgxCSyuVDisbizjZ5xtpump").data

    assert props["bundler_pct"] == Decimal("51.57")
    assert props["sniper_pct"] == Decimal("6.49327748")
    assert props["top10_pct"] == Decimal("20.06")
    assert props["dev_pct"] == Decimal("2.108446010")
    assert props["dev_sold"] is True
    assert props["holder_count"] == Decimal(256)


def test_that_launch_would_now_fail_the_curve_velocity_gate(replay_many):
    """The whole point of the field: an unknown fails closed, but so should a real 51%."""
    from kaiba.execution import lanes

    replay_many({"security": "token_security_bundled", "info": "token_info_bundled"})
    props = g.security_properties("C2yUFYGox1ggub6eVfWC8hgxCSyuVDisbizjZ5xtpump").data
    ceiling = Decimal(str(lanes.DEFAULT_PARAMS[lanes.Lane.CURVE_VELOCITY]["max_bundler_pct"]))
    assert props["bundler_pct"] > ceiling


def test_a_percentage_out_of_rate_range_is_dropped_not_divided():
    """The fail-safe behind the one scale we had to assume.

    ``top_bundler_trader_percentage`` is read as a 0-1 rate. If GMGN ever means it as a
    percent, a real 40% arrives as ``40`` and must become *unknown* -- which leaves
    ``curve-velocity`` failing closed, exactly as it does today -- rather than 0.4%, which
    would fire the lane on the launches it exists to avoid.
    """
    dropped: list[str] = []
    props = g.normalize_security({"top_bundler_trader_percentage": "40"}, dropped=dropped)
    assert "bundler_pct" not in props
    assert dropped and "not a 0-1 rate" in dropped[0]


def test_no_percent_field_rests_on_an_assumed_scale():
    """Empty because ``top_bundler_trader_percentage`` was calibrated, not guessed.

    ``market trending`` gives ``bundler_rate`` and ``token info`` gives
    ``top_bundler_trader_percentage`` for the same address; measured over 8 trending
    tokens the ratio is 1.00. If a future edit adds a field whose scale is unknown, it
    goes here and this test is where that gets noticed.
    """
    assert g.SCALE_UNVERIFIED == frozenset()


def test_the_receipt_claims_no_assumption_it_does_not_have(wsol_pair):
    note = g.security_properties("So11111111111111111111111111111111111111112").receipt.note
    assert "scale assumed" not in note
    assert "token.security" in note and "token.info" in note


def test_a_missing_field_is_absent_never_zero():
    props = g.normalize_security({"symbol": "X"})
    for numeric in ("bundler_pct", "sniper_pct", "top10_pct", "dev_pct", "liquidity_usd"):
        assert numeric not in props


def test_one_endpoint_down_still_yields_the_other_half(replay_many):
    replay_many({"security": g._Raw(1, "", "boom"), "info": "token_info"})
    result = g.security_properties("So11111111111111111111111111111111111111112")
    assert result.ok
    assert result.receipt.endpoint == "token.info"
    assert "bundler_pct" in result.data


def test_both_endpoints_down_is_unavailable_not_an_empty_dict(replay_many, tmp_db):
    replay_many({"token": g._Raw(1, "", "boom")})
    result = g.security_properties("So11111111111111111111111111111111111111112", conn=tmp_db)
    assert result.data is None
    assert result.ok is False
    assert result.receipt.basis is EvidenceBasis.UNAVAILABLE
    assert events_of(tmp_db)


def test_merged_receipt_reports_the_stalest_half(replay_many, monkeypatch):
    """A merged view is only as fresh as its oldest part, and the engine reads that."""
    replay_many({"security": "token_security", "info": "token_info"})
    g.security_properties("So11111111111111111111111111111111111111112")

    from kaiba.providers import _http

    base = _http.now_ms()
    # Past token.info's 15s TTL but inside its grace; token.security's 900s TTL still holds.
    monkeypatch.setattr(_http, "now_ms", lambda: base + 100_000)
    replay_many({"info": g._Raw(1, "", "boom")})
    result = g.security_properties("So11111111111111111111111111111111111111112")
    assert result.receipt.basis is EvidenceBasis.STALE


def test_flatten_prefers_the_outer_value_and_reaches_the_stat_block():
    flat = g.flatten_payload(
        {"liquidity": "1", "price": {"price": "7"}, "pool": {"liquidity": "2"},
         "stat": {"top70_sniper_hold_rate": "0.5"}}
    )
    assert flat["liquidity"] == "1"
    assert flat["price"] == "7"
    assert flat["top70_sniper_hold_rate"] == "0.5"


# ------------------------------------------------------------------------- limiter


def test_every_call_goes_through_the_limiter(replay, tmp_db):
    replay("token_info")
    g.token_info("So11111111111111111111111111111111111111112")
    rows = fetch_all(tmp_db, "SELECT * FROM provider_calls", ())
    assert len(rows) == 1
    assert rows[0]["provider"] == "gmgn"
    assert rows[0]["endpoint"] == "token.info"
    assert rows[0]["status"] == "ok"


def test_endpoint_families_are_separate():
    """A 429 on quotes must not cool down token info, so they cannot share a family."""
    from kaiba.core.limiter import _family

    families = {e: _family(e) for e in g._TTL}
    assert families["quote"] == "quote"
    assert families["token.info"] == "token"
    assert families["market.trending"] == "market"
    assert families["portfolio.stats"] == "portfolio"
    assert families["track.smartmoney"] == "track"
    assert families["quote"] not in {families["token.info"], families["market.trending"]}


def test_quote_endpoint_is_charged_gmgns_real_weight():
    """The limiter's weight table is keyed on the full endpoint string, so `quote` it is."""
    from kaiba.core.limiter import limits_for

    lim = limits_for("gmgn")
    assert lim.weight_for("quote") == g.GMGN_WEIGHTS["quote"] == 10
    assert lim.weight_for("token.info") == 1


def test_limiter_refusal_returns_unavailable_and_never_raises(replay, tmp_db, monkeypatch):
    replay("token_info")

    def refuse(provider, endpoint, priority=Priority.RESEARCH, conn=None):
        raise RateLimited(provider, "bucket exhausted", 7.5)

    monkeypatch.setattr(g, "guarded", _raising_ctx(refuse))
    result = g.token_info("AAAA")
    assert result.data is None
    assert result.receipt.basis is EvidenceBasis.UNAVAILABLE
    assert "limiter refused" in result.receipt.note
    payload = json.loads(events_of(tmp_db)[0]["payload"])
    assert payload["failure"] == "limiter"
    assert payload["retry_after_s"] == 7.5


def _raising_ctx(fn):
    """A drop-in for ``guarded`` whose reservation raises before the body runs."""
    from contextlib import contextmanager

    @contextmanager
    def ctx(provider, endpoint, priority=Priority.RESEARCH, conn=None):
        fn(provider, endpoint, priority, conn)
        yield

    return ctx


def test_real_min_interval_is_honoured(monkeypatch, tmp_db):
    """No ``fast_limiter`` here: gmgn's real 1.2s floor must actually stop the second call."""
    calls: list[list[str]] = []

    def fake_spawn(argv, timeout_s):
        calls.append(list(argv))
        return raw_from("token_info")

    monkeypatch.setattr(g, "cli_argv", lambda: ["node", "index.js"])
    monkeypatch.setattr(g, "_spawn", fake_spawn)

    first = g.token_info("AAAA", ttl_s=0)
    second = g.token_info("BBBB", ttl_s=0)

    assert first.ok
    assert len(calls) == 1
    assert second.data is None
    assert second.receipt.basis is EvidenceBasis.UNAVAILABLE
    assert "minimum interval" in second.receipt.note


def test_a_number_in_the_payload_is_not_a_rate_limit(replay, tmp_db):
    """The regression: ``"volume_1h":"429.56"`` used to open a 60s cooldown on a good read.

    ``_classify`` searched ``stderr + stdout`` for a bare ``\\b429\\b``, so any token whose
    volume, price or market cap rendered with a leading ``429.`` classified a successful
    ``token info`` as rate limited — payload discarded, ``penalty_level`` raised, the
    whole ``token`` family cooled down for a minute. Observed live at about 1 read in 450
    during a burst, and self-reinforcing once it fires.
    """
    payload = json.loads(load_fixture("token_info")["stdout"])
    payload["price"]["volume_1h"] = "429.56"
    payload["price"]["volume_24h"] = "1429.00"
    replay(raw=g._Raw(0, json.dumps(payload), ""))

    result = g.token_info("AAAA", conn=tmp_db)

    assert result.ok, result.receipt.note
    assert result.data["price"]["volume_1h"] == "429.56"
    assert fetch_all(tmp_db, "SELECT * FROM provider_calls", ())[0]["status"] == "ok"
    assert fetch_all(tmp_db, "SELECT * FROM provider_family_bans", ()) == []


def test_a_healthy_payload_is_never_searched_for_failure_words(replay, tmp_db):
    """Same class of bug for auth: stdout is a response, not a diagnostic."""
    payload = json.loads(load_fixture("token_info")["stdout"])
    payload["price"]["swaps_1h"] = 401
    payload["note"] = "unauthorized-looking text that is just data"
    replay(raw=g._Raw(0, json.dumps(payload), ""))
    assert g.token_info("AAAA", conn=tmp_db).ok


def test_the_real_recorded_429_still_classifies(replay, tmp_db):
    """Tightening the pattern must not blind us to the genuine article."""
    raw = raw_from("rate_limited_429")
    failure, note, retry_after = g._classify(raw)
    assert failure is g.Failure.RATE_LIMITED
    assert "RATE_LIMIT_EXCEEDED" in note
    # the recorded message carries "(~30s remaining)", which needs no timezone guess
    assert retry_after == 30.0


def test_recorded_429_is_surfaced_to_the_limiter(replay, tmp_db):
    """The real 429 we recorded must open a family cooldown, not just return empty."""
    replay("rate_limited_429")
    result = g.track_follow_wallet()

    assert result.data is None
    assert result.receipt.basis is EvidenceBasis.UNAVAILABLE
    assert "429" in result.receipt.note

    bans = fetch_all(tmp_db, "SELECT * FROM provider_family_bans", ())
    assert [b["family"] for b in bans] == ["track"]
    calls = fetch_all(tmp_db, "SELECT * FROM provider_calls", ())
    assert calls[0]["status"] == "rate_limited"


def test_429_cooldown_does_not_block_a_different_family(replay, tmp_db):
    replay("rate_limited_429")
    g.track_follow_wallet()

    replay("token_info")
    result = g.token_info("So11111111111111111111111111111111111111112")
    assert result.ok, result.receipt.note


def test_retry_after_is_parsed_from_the_reset_hint():
    from datetime import datetime, timedelta

    when = (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
    got = g._retry_after_from(f"IP rate limit exceeded. Rate limit resets at {when}")
    assert got is not None
    assert 200 <= got <= 320


def test_retry_after_is_clamped_and_optional():
    assert g._retry_after_from("no hint here") is None
    assert g._retry_after_from("resets at 2099-01-01 00:00") == 3600.0
    assert g._retry_after_from("resets at 2000-01-01 00:00") == 1.0


def test_retry_after_prefers_the_cli_countdown_over_the_timestamp():
    """gmgn-cli prints both. The countdown needs no timezone guess, so it wins."""
    message = (
        "HTTP 429 error=RATE_LIMIT_EXCEEDED. Rate limit resets at 2000-01-01 00:00 "
        "(~42s remaining). Stop sending requests before then"
    )
    assert g._retry_after_from(message) == 42.0
    assert g._retry_after_from("(~99999s remaining)") == 3600.0


def test_wait_for_slot_rides_out_the_minimum_interval(monkeypatch, tmp_db):
    """No ``fast_limiter``: the real 1.2s floor must be waited out, not returned as a miss.

    The contract's rule for anything issuing more than one call per logical operation.
    Without it ``security_properties`` silently returns half its fields.
    """
    calls: list[list[str]] = []

    def fake_spawn(argv, timeout_s):
        calls.append(list(argv))
        return raw_from("token_info")

    monkeypatch.setattr(g, "cli_argv", lambda: ["node", "index.js"])
    monkeypatch.setattr(g, "_spawn", fake_spawn)

    assert g.token_info("AAAA", ttl_s=0).ok
    second = g.token_info("BBBB", ttl_s=0, wait_for_slot_s=8.0)

    assert second.ok, second.receipt.note
    assert len(calls) == 2


def test_wait_for_slot_still_gives_up_rather_than_blocking_forever(replay, tmp_db, monkeypatch):
    replay("token_info")

    def refuse(provider, endpoint, priority=Priority.RESEARCH, conn=None):
        raise RateLimited(provider, "bucket exhausted", 0.05)

    monkeypatch.setattr(g, "guarded", _raising_ctx(refuse))
    result = g.token_info("AAAA", wait_for_slot_s=0.2)
    assert result.data is None
    assert "limiter refused" in result.receipt.note


def test_wait_for_slot_does_not_sleep_through_a_cooldown_it_cannot_outlast(replay, tmp_db, monkeypatch):
    """A 60s family ban will still be a ban in 5s, so waiting only spends the caller's time."""
    import time as _time

    replay("token_info")
    slept: list[float] = []
    monkeypatch.setattr(g.time, "sleep", lambda s: slept.append(s))

    def refuse(provider, endpoint, priority=Priority.RESEARCH, conn=None):
        raise RateLimited(provider, "token cooldown", 60.0)

    monkeypatch.setattr(g, "guarded", _raising_ctx(refuse))
    started = _time.monotonic()
    result = g.token_info("AAAA", wait_for_slot_s=5.0)

    assert result.data is None
    assert slept == [], "slept through a cooldown it could never outlast"
    assert _time.monotonic() - started < 1.0


def test_a_provider_429_is_never_retried_by_the_wait_loop(replay, tmp_db):
    """Retrying GMGN's own 429 is how a 5s IP ban becomes a 5 minute one."""
    calls = replay("rate_limited_429")
    result = g.track_smartmoney(wait_for_slot_s=10.0)
    assert result.data is None
    assert len(calls) == 1


# ----------------------------------------------------------------------- plan gating


def test_quote_refused_for_plan_says_so_plainly(replay, tmp_db):
    replay("quote_plan_refused")
    result = g.order_quote(
        input_token="So11111111111111111111111111111111111111112",
        output_token="CVoNMSYK9mVd3TzAutnrRTKmptQUQ7r9qBg1ejb7vVd2",
        amount=100_000_000,
        from_address="5gfn3DiSwaxXHk3miXKz7MoapN3UmAh3TDnXoKCvVQL4",
    )
    assert result.data is None
    note = result.receipt.note
    assert "plan" in note.lower()
    assert "weight 10" in note and "allowed 5" in note

    payload = json.loads(events_of(tmp_db)[0]["payload"])
    assert payload["plan_gated"] is True
    assert payload["gmgn_weight"] == 10
    assert payload["free_plan_weight"] == 5
    # Never again "upgrade to Plus": that advice was measured stale on 2026-09-21, when a
    # real quote came back under PRO. Telling a paying operator to buy a tier they hold
    # is worse than saying nothing.
    assert "required_plan" not in payload
    assert "subscription is still active" in payload["operator_action"]
    assert "upgrade the GMGN account to Plus" not in payload["operator_action"]


def test_the_recorded_quote_is_the_real_thing_now(replay):
    """``quote_ok`` is a live PRO recording, not a stand-in for one."""
    fixture = load_fixture("quote_ok")
    assert not fixture.get("_synthetic")
    replay("quote_ok")
    result = g.order_quote(
        input_token="So11111111111111111111111111111111111111112",
        output_token="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        amount=100_000_000,
        from_address="5gfn3DiSwaxXHk3miXKz7MoapN3UmAh3TDnXoKCvVQL4",
    )
    assert result.ok
    assert set(result.data) >= {"input_amount", "output_amount", "min_output_amount"}
    assert int(result.data["output_amount"]) > 0


def test_a_read_429_is_not_reported_as_a_plan_problem(replay, tmp_db):
    replay("rate_limited_429")
    result = g.track_smartmoney()
    assert "plan" not in (result.receipt.note or "").lower()
    payload = json.loads(events_of(tmp_db)[0]["payload"])
    assert "plan_gated" not in payload


def test_quote_is_never_cached(replay):
    calls = replay("quote_ok")
    kwargs = dict(
        input_token="So11111111111111111111111111111111111111112",
        output_token="CVoNMSYK9mVd3TzAutnrRTKmptQUQ7r9qBg1ejb7vVd2",
        amount=100_000_000,
        from_address="5gfn3DiSwaxXHk3miXKz7MoapN3UmAh3TDnXoKCvVQL4",
    )
    first = g.order_quote(**kwargs)
    second = g.order_quote(**kwargs)
    assert first.receipt.basis is EvidenceBasis.PROVIDER_REPORTED
    assert second.receipt.basis is EvidenceBasis.PROVIDER_REPORTED
    assert len(calls) == 2
    assert g._TTL["quote"] == (0.0, 0.0)


def test_quote_sends_slippage_as_decimal_percent(replay):
    """Matching executor.gmgn_swap_body; sending bps here overpays by a factor of 100."""
    calls = replay("quote_ok")
    g.order_quote(
        input_token="AAAA",
        output_token="BBBB",
        amount=5,
        from_address="CCCC",
        slippage_bps=137,
    )
    argv = calls[0]
    assert argv[argv.index("--slippage") + 1] == "1.37"
    assert argv[argv.index("--amount") + 1] == "5"


def test_quote_helper_cannot_submit(replay):
    calls = replay("quote_ok")
    g.order_quote(input_token="A", output_token="B", amount=1, from_address="C")
    argv = calls[0]
    assert "swap" not in argv
    assert "--yes" not in argv
    assert "--anti-mev" not in argv


# ----------------------------------------------------------------------- failure paths


def test_nonzero_exit_returns_none_and_emits(replay, tmp_db):
    replay("error_bad_address")
    result = g.token_info("AAAA")
    assert result.data is None
    assert result.receipt.basis is EvidenceBasis.UNAVAILABLE
    assert result.receipt.note
    assert events_of(tmp_db)


def test_bad_chain_argument_is_refused_before_spawning(replay, tmp_db):
    calls = replay("token_info")
    result = g.token_info("AAAA", chain="dogecoin")
    assert result.data is None
    assert calls == []
    assert "unknown chain" in result.receipt.note


def test_shell_metacharacters_are_refused(replay):
    calls = replay("token_info")
    for hostile in ['AAAA & calc.exe', "AAAA | whoami", 'AAAA" & echo', "A" * 300, "AAAA;ls"]:
        result = g.token_info(hostile)
        assert result.data is None, hostile
        assert result.receipt.basis is EvidenceBasis.UNAVAILABLE
    assert calls == []


def test_timeout_returns_none_and_kills_the_tree(replay, tmp_db):
    replay(raw=g._Raw(None, "", "", timed_out=True))
    result = g.token_info("AAAA")
    assert result.data is None
    assert "deadline" in result.receipt.note
    payload = json.loads(events_of(tmp_db)[0]["payload"])
    assert payload["failure"] == "timeout"


def test_unparseable_stdout_returns_none(replay, tmp_db):
    replay("unparseable_stdout")
    result = g.token_info("AAAA")
    assert result.data is None
    assert "not JSON" in result.receipt.note
    payload = json.loads(events_of(tmp_db)[0]["payload"])
    assert payload["failure"] == "unparseable"


def test_unexpected_payload_shape_fails_loudly(replay, tmp_db):
    replay(raw=g._Raw(0, json.dumps({"items": [1, 2]}), ""))
    result = g.token_holders("AAAA")
    assert result.data is None
    assert "shape changed" in result.receipt.note
    payload = json.loads(events_of(tmp_db)[0]["payload"])
    assert payload["failure"] == "bad_shape"


def test_nonzero_envelope_code_is_a_failure(replay):
    replay(raw=g._Raw(0, json.dumps({"code": 40001, "msg": "bad param", "data": None}), ""))
    result = g.market_trending()
    assert result.data is None
    assert "code=40001" in result.receipt.note


def test_a_rejected_api_key_names_its_own_fix(replay, tmp_db):
    """Recorded live on 2026-09-21, right after the PRO upgrade: GMGN rejects the key.

    This must never read as a plan problem. No subscription makes an unknown key known,
    and reporting it as ``plan_gated`` sends the operator to a checkout page that cannot
    help.
    """
    replay("auth_key_invalid")
    result = g.token_info("So11111111111111111111111111111111111111112", conn=tmp_db)

    assert result.data is None
    note = result.receipt.note
    assert "does not recognise this API key" in note
    assert "gmgn.ai/ai" in note
    assert ".config" in note  # points at the file gmgn-cli actually reads

    payload = json.loads(events_of(tmp_db)[0]["payload"])
    assert payload["failure"] == "auth"
    assert "plan_gated" not in payload


def test_a_rejected_key_is_not_reported_as_plan_gated_even_on_a_quote():
    """``quote`` is the one endpoint whose failures are assumed billable. Not this one."""
    note = "HTTP 401 code=401 error=AUTH_KEY_INVALID message=api key invalid"
    assert g._is_plan_gated("quote", g.Failure.AUTH, note) is False
    assert g._is_plan_gated("quote", g.Failure.AUTH, "upgrade to Plus") is True


def test_credential_source_diagnoses_without_disclosing(monkeypatch, tmp_path):
    """gmgn-cli loads ~/.config/gmgn/.env with override=true, so our .env can be a no-op."""
    cli_env = tmp_path / ".env"
    cli_env.write_text("GMGN_API_KEY=theirs-1234567890\n", encoding="utf-8")
    monkeypatch.setattr(g, "CLI_GLOBAL_ENV", cli_env)

    class S:
        gmgn_api_key = "ours-0987654321"
        gmgn_private_key = ""

    monkeypatch.setattr("kaiba.core.config.get_settings", lambda: S())
    said = g.credential_source()
    assert "DIFFERENT" in said
    assert "theirs-1234567890" not in said
    assert "ours-0987654321" not in said

    cli_env.write_text("GMGN_API_KEY=ours-0987654321\n", encoding="utf-8")
    assert "matches ours" in g.credential_source()
    assert "ours-0987654321" not in g.credential_source()


def test_signed_endpoint_auth_failure_names_the_fix(replay, tmp_db):
    replay("auth_signature_invalid")
    result = g.portfolio_holdings("5gfn3DiSwaxXHk3miXKz7MoapN3UmAh3TDnXoKCvVQL4")
    assert result.data is None
    assert "gmgn-cli config" in result.receipt.note
    assert "Ed25519" in result.receipt.note
    payload = json.loads(events_of(tmp_db)[0]["payload"])
    assert payload["failure"] == "auth"
    # a missing signing key is a credential problem, not something you can buy your way out of
    assert "plan_gated" not in payload


def test_missing_cli_returns_unavailable(monkeypatch, tmp_db):
    monkeypatch.setattr(g, "cli_argv", lambda: None)
    result = g.token_info("AAAA")
    assert result.data is None
    assert "not found" in result.receipt.note
    payload = json.loads(events_of(tmp_db)[0]["payload"])
    assert payload["failure"] == "not_installed"


def test_spawn_failure_is_data_not_an_exception(replay, tmp_db):
    replay(raw=g._Raw(None, "", "", spawn_error="FileNotFoundError: node"))
    result = g.token_info("AAAA")
    assert result.data is None
    assert "could not start" in result.receipt.note


def test_an_unexpected_internal_error_still_returns_a_receipt(replay, monkeypatch, tmp_db):
    replay("token_info")
    monkeypatch.setattr(g, "_classify", lambda raw: (_ for _ in ()).throw(ValueError("boom")))
    result = g.token_info("AAAA")
    assert result.data is None
    assert result.receipt.basis is EvidenceBasis.UNAVAILABLE
    assert "ValueError" in result.receipt.note


@pytest.mark.parametrize(
    "fixture",
    ["rate_limited_429", "auth_signature_invalid", "error_bad_address", "error_bad_chain"],
)
def test_no_recorded_failure_ever_raises(replay, fixture):
    replay(fixture)
    for call in (
        lambda: g.token_info("AAAA"),
        lambda: g.market_trending(),
        lambda: g.track_kol(),
        lambda: g.portfolio_stats("AAAA"),
    ):
        result = call()
        assert result.data is None
        assert result.receipt.basis is EvidenceBasis.UNAVAILABLE
        assert result.ok is False


def test_failure_never_returns_a_zero(replay):
    """A dead provider must not look like a real zero to a scorer."""
    replay("error_bad_address")
    for result in (g.token_info("AAAA"), g.token_holders("AAAA"), g.market_signal()):
        assert result.data is None
        # not 0, not "", not [], not {} — every one of those reads as a real answer upstream
        assert not any(result.data == empty for empty in (0, 0.0, "", [], {}))


# ------------------------------------------------------------------------------ cache


def test_second_call_is_served_from_disk_cache(replay, tmp_path):
    calls = replay("token_info")
    first = g.token_info("So11111111111111111111111111111111111111112")
    second = g.token_info("So11111111111111111111111111111111111111112")

    assert len(calls) == 1
    assert first.receipt.basis is EvidenceBasis.PROVIDER_REPORTED
    assert second.receipt.basis is EvidenceBasis.CACHED
    assert second.data == first.data


def test_cache_lands_under_data_cache_gmgn(replay, tmp_path):
    replay("token_info")
    g.token_info("So11111111111111111111111111111111111111112")
    files = list((tmp_path / "cache" / "gmgn").glob("*.json"))
    assert len(files) == 1


def test_cache_path_contains_no_secret(replay, tmp_path, monkeypatch):
    monkeypatch.setenv("GMGN_API_KEY", "super-secret-key-value")
    replay("token_info")
    g.token_info("So11111111111111111111111111111111111111112")
    for p in (tmp_path / "cache" / "gmgn").glob("*.json"):
        assert "super-secret-key-value" not in str(p)
        assert "super-secret-key-value" not in p.read_text(encoding="utf-8")


def test_different_arguments_do_not_share_a_cache_entry(replay):
    calls = replay("token_info")
    g.token_info("AAAA")
    g.token_info("BBBB")
    assert len(calls) == 2


def test_ttl_zero_disables_caching(replay):
    calls = replay("token_info")
    g.token_info("AAAA", ttl_s=0)
    g.token_info("AAAA", ttl_s=0)
    assert len(calls) == 2


def test_stale_cache_is_offered_within_the_grace_window(replay, monkeypatch):
    replay("token_security")
    g.token_security("AAAA")

    # jump past the TTL but stay inside the grace window
    from kaiba.providers import _http

    base = _http.now_ms()
    monkeypatch.setattr(_http, "now_ms", lambda: base + 1_000_000)

    replay(raw=g._Raw(1, "", "boom"))
    result = g.token_security("AAAA")
    assert result.receipt.basis is EvidenceBasis.STALE
    assert result.data is not None


def test_no_grace_means_no_stale_position_data():
    """Holdings and track feeds must never serve an expired value."""
    for endpoint in ("portfolio.holdings", "track.smartmoney", "track.kol", "track.follow_wallet"):
        assert g._TTL[endpoint][1] == 0.0


def test_every_endpoint_has_a_ttl_and_a_payload_rule():
    assert set(g._TTL) == set(g._PAYLOAD_KEY)
    assert len(g._TTL) == len(g._ALLOWED)


# ---------------------------------------------------------------------------- secrets


def test_scrub_removes_configured_credentials(monkeypatch):
    monkeypatch.setenv("GMGN_API_KEY", "abcdefgh12345678")
    monkeypatch.setenv("GMGN_PRIVATE_KEY", "zzzzzzzzyyyyyyyy")
    out = g.scrub("failed with key abcdefgh12345678 and zzzzzzzzyyyyyyyy")
    assert "abcdefgh12345678" not in out
    assert "zzzzzzzzyyyyyyyy" not in out
    assert out.count("<redacted>") == 2


def test_scrub_removes_labelled_values():
    out = g.scrub("GET /v1/x?api_key=SEKRIT123&signature=SIGNED99 failed")
    assert "SEKRIT123" not in out
    assert "SIGNED99" not in out


def test_scrub_keeps_useful_diagnostics():
    msg = "[gmgn-cli] GET /v1/trade/follow_wallet failed: HTTP 429 code=429 error=RATE_LIMIT_EXCEEDED"
    assert g.scrub(msg) == msg


def test_no_secret_reaches_a_receipt_or_an_event(replay, tmp_db, monkeypatch):
    monkeypatch.setenv("GMGN_API_KEY", "leakme-0123456789")
    replay(raw=g._Raw(1, "", "auth failed for key leakme-0123456789"))
    result = g.token_info("AAAA")
    assert "leakme-0123456789" not in (result.receipt.note or "")
    for row in events_of(tmp_db):
        assert "leakme-0123456789" not in row["payload"]


def test_no_credential_is_ever_passed_on_the_command_line(replay, monkeypatch):
    monkeypatch.setenv("GMGN_API_KEY", "leakme-0123456789")
    calls = replay("token_info")
    g.token_info("AAAA")
    g.order_quote(input_token="A", output_token="B", amount=1, from_address="C")
    for argv in calls:
        joined = " ".join(argv)
        assert "leakme-0123456789" not in joined
        for flag in ("--api-key", "--key", "--private-key", "--secret"):
            assert flag not in argv


def test_child_env_cannot_arm_automated_trades(monkeypatch):
    monkeypatch.setenv("GMGN_ALLOW_AUTOMATED_TRADES", "1")
    assert g._child_env()["GMGN_ALLOW_AUTOMATED_TRADES"] == "0"


def test_child_env_refuses_to_let_the_cli_print_the_request(monkeypatch):
    """GMGN_DEBUG makes gmgn-cli dump the curl line and raw response on every failure.

    Both streams end up in our receipts and the events table, so an operator who exported
    it for one manual session would otherwise turn every provider error into a disclosure.
    """
    monkeypatch.setenv("GMGN_DEBUG", "1")
    assert "GMGN_DEBUG" not in g._child_env()


def test_child_env_makes_the_cli_surface_429s_instead_of_absorbing_them(monkeypatch):
    """gmgn-cli 1.6.1 retries a rate-limited read once, invisibly, inside the child.

    The limiter would then never raise ``penalty_level`` or open a cooldown, while the
    hidden retry is itself one of the repeated requests GMGN says extends the ban.
    """
    monkeypatch.setenv("GMGN_RATE_LIMIT_AUTO_RETRY_MAX_WAIT_MS", "5000")
    assert g._child_env()["GMGN_RATE_LIMIT_AUTO_RETRY_MAX_WAIT_MS"] == "0"


def test_child_env_still_carries_the_credentials_the_cli_needs(monkeypatch):
    monkeypatch.setenv("GMGN_API_KEY", "inherit-me-please")
    assert g._child_env()["GMGN_API_KEY"] == "inherit-me-please"


# ------------------------------------------------------------------------ binary path


def test_cli_argv_prefers_the_configured_path(monkeypatch, tmp_path):
    js = tmp_path / "index.js"
    js.write_text("//", encoding="utf-8")

    class S:
        gmgn_cli_path = str(js)

    monkeypatch.setattr("kaiba.core.config.get_settings", lambda: S())
    assert g.cli_argv() == ["node", str(js)]


def test_cli_argv_falls_back_to_appdata(monkeypatch, tmp_path):
    class S:
        gmgn_cli_path = ""

    monkeypatch.setattr("kaiba.core.config.get_settings", lambda: S())
    monkeypatch.setattr(g.shutil, "which", lambda _n: None)
    target = tmp_path / "npm" / "node_modules" / "gmgn-cli" / "dist" / "index.js"
    target.parent.mkdir(parents=True)
    target.write_text("//", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert g.cli_argv() == ["node", str(target)]


def test_cmd_shim_is_swapped_for_the_js_entrypoint(tmp_path):
    """Running the .cmd shim would put cmd.exe between us and provider-supplied argv."""
    shim = tmp_path / "gmgn-cli.CMD"
    shim.write_text("@echo off", encoding="utf-8")
    js = tmp_path / "node_modules" / "gmgn-cli" / "dist" / "index.js"
    js.parent.mkdir(parents=True)
    js.write_text("//", encoding="utf-8")
    assert g._deshim(shim) == ["node", str(js)]


def test_cmd_shim_is_used_as_a_last_resort(tmp_path):
    shim = tmp_path / "gmgn-cli.cmd"
    shim.write_text("@echo off", encoding="utf-8")
    assert g._deshim(shim) == [str(shim)]


# --------------------------------------------------------------------- spawn plumbing


def test_spawn_pins_utf8_so_windows_does_not_mojibake():
    """The CLI emits UTF-8; letting Python guess the ANSI code page raises on real output."""
    code = "import sys;sys.stdout.reconfigure(encoding='utf-8');print('\\u2014 \\u00e9 \\u4f60\\u597d')"
    raw = g._spawn([sys.executable, "-c", code], 30.0)
    assert raw.returncode == 0
    assert "你好" in raw.stdout


def test_spawn_kills_a_hung_process_and_reports_a_timeout():
    raw = g._spawn([sys.executable, "-c", "import time;time.sleep(60)"], 2.0)
    assert raw.timed_out is True
    failure, note, _ = g._classify(raw)
    assert failure is g.Failure.TIMEOUT
    assert "killed" in note


def test_spawn_kills_the_whole_process_tree():
    """proc.kill() alone leaves Node's children running; the group must go too."""
    child = (
        "import subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "print(p.pid,flush=True);time.sleep(60)"
    )
    raw = g._spawn([sys.executable, "-c", child], 3.0)
    assert raw.timed_out is True
    pid = int((raw.stdout or "0").strip() or 0)
    if pid:
        assert not _pid_alive(pid), f"grandchild {pid} survived the timeout"


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, check=False
        )
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_spawn_never_raises_on_a_missing_binary():
    raw = g._spawn(["definitely-not-a-real-binary-xyz"], 5.0)
    assert raw.spawn_error
    failure, note, _ = g._classify(raw)
    assert failure is g.Failure.NOT_INSTALLED


# -------------------------------------------------------------------------- live tests


@pytest.mark.live
def test_live_token_info_matches_the_recorded_shape():
    """Read-only. Never runs a swap, an order or anything that spends."""
    result = g.token_info("So11111111111111111111111111111111111111112", ttl_s=0)
    if not result.ok:
        pytest.skip(f"gmgn unavailable: {result.receipt.note}")
    recorded = set(json.loads(load_fixture("token_info")["stdout"]))
    missing = recorded - set(result.data)
    assert not missing, f"gmgn-cli dropped fields since the fixture was recorded: {missing}"


@pytest.mark.live
def test_live_command_surface_still_matches_the_helpers():
    """`gmgn-cli <group> --help` is the source of truth; drift here means the map is stale."""
    base = g.cli_argv()
    if base is None:
        pytest.skip("gmgn-cli is not installed")
    for group in ("token", "market", "portfolio", "track", "order"):
        raw = g._spawn([*base, group, "--help"], 30.0)
        text = raw.stdout + raw.stderr
        for grp, cmd in g._ALLOWED:
            if grp == group:
                assert cmd in text, f"gmgn-cli {group} no longer offers {cmd}"


@pytest.mark.live
def test_live_chain_enum_matches_the_cli():
    base = g.cli_argv()
    if base is None:
        pytest.skip("gmgn-cli is not installed")
    raw = g._spawn([*base, "token", "info", "--chain", "not-a-chain", "--address", "x", "--raw"], 30.0)
    text = raw.stdout + raw.stderr
    for chain in Chain:
        assert chain.value in text, f"gmgn-cli no longer lists {chain.value}"
