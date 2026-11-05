"""The read-only GMGN `market kline` route (KLINE-READONLY-ADAPTER-CLAUDE-20261103).

Each test is a way this narrow read could turn into something else: a weight the limiter
undercharges, a mislabelled command that escapes the rules keyed by endpoint, a caller that picks
another resolution, a stale value, a wait or EXIT capacity, a malformed payload read as data, an
empty answer read as unavailable (or the reverse), a raw field rewritten, or the CLI retrying a 429
behind the limiter's back. No test calls the provider.
"""
from __future__ import annotations

import dataclasses
import json
import time
from types import SimpleNamespace

import pytest

from kaiba.core import limiter as lim
from kaiba.core.limiter import Priority
from kaiba.core.schemas import EvidenceBasis
from kaiba.providers import _http
from kaiba.providers import gmgn_cli as g

MINT = "So11111111111111111111111111111111111111112"
NOW_S = int(time.time())
TO = NOW_S - 3600
CANDLE = {"time": 1_793_000_000_000, "open": "0.0002", "high": "0.00021", "low": "0.00019",
          "close": "0.0002", "volume": "1214", "amount": "5379110"}


def args(resolution="1m", bars=60, *, to=TO, chain="sol", address=MINT, extra=()):
    res = g.KLINE_RESOLUTIONS.get(resolution, 60)
    return ["market", "kline", "--chain", chain, "--address", address, "--resolution", resolution,
            "--from", str(to - bars * res), "--to", str(to), *extra]


@pytest.fixture
def wire(monkeypatch, tmp_path):
    """Fake subprocess, a spy on the real reservation, and a private cache directory."""
    monkeypatch.setattr(_http, "_cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(g, "cli_argv", lambda: ["node", "index.js"])
    real_limits = lim.limits_for

    def relaxed(provider):  # pacing off so a test can make two calls; weights stay real
        base = real_limits(provider)
        return dataclasses.replace(base, min_interval_ms=0, capacity=10_000, refill_per_s=10_000.0)

    monkeypatch.setattr(lim, "limits_for", relaxed)
    state = SimpleNamespace(spawned=[], reserved=[], stdout=json.dumps({"list": [CANDLE]}), returncode=0)
    real_reserve = lim.reserve

    def spy_reserve(provider, endpoint, priority=Priority.RESEARCH, conn=None):
        weight = real_reserve(provider, endpoint, priority, conn=conn)
        state.reserved.append((endpoint, priority, weight))
        return weight

    def fake_spawn(argv, timeout_s):
        state.spawned.append(list(argv))
        return g._Raw(state.returncode, state.stdout, "")

    monkeypatch.setattr(lim, "reserve", spy_reserve)
    monkeypatch.setattr(g, "_spawn", fake_spawn)
    return state


def refused(result):
    return result.data is None and result.receipt.basis is EvidenceBasis.UNAVAILABLE


# ------------------------------------------------------------------ the allow-list and weight


def test_the_kline_pair_is_allowed_and_nothing_that_trades_is():
    assert ("market", "kline") in g._ALLOWED
    assert not any(grp in {"swap", "multi-swap", "cooking", "config"} for grp, _ in g._ALLOWED)
    assert {cmd for grp, cmd in g._ALLOWED if grp == "order"} == {"quote"}


@pytest.mark.parametrize("weights, expected", [
    ({"quote": 10, "swap": 10, "default": 1}, 2),        # the box's table: kline absent
    ({"quote": 10, "swap": 10, "kline": 1, "default": 1}, 2),  # config cannot undercut the vendor
    ({"quote": 10, "swap": 10, "market.kline": 1, "default": 1}, 2),
    ({"quote": 10, "swap": 10, "kline": 5, "default": 1}, 5),  # but may charge more
])
def test_the_vendor_weight_floor_survives_a_config_that_replaces_the_table(monkeypatch, weights, expected):
    budgets = {"gmgn": {"min_interval_ms": 250, "capacity": 60, "refill_per_s": 3.0, "max_inflight": 4,
                        "weights": weights}}
    monkeypatch.setattr(lim, "get_risk", lambda: SimpleNamespace(provider_budgets=budgets))
    got = lim.limits_for("gmgn")
    assert got.weight_for("market.kline") == expected
    assert (got.weight_for("token.info"), got.weight_for("trade.swap"), got.weight_for("quote")) == (1, 10, 10)


def test_the_real_reservation_charges_two(tmp_db, wire):
    out = g.market_kline(MINT, resolution="1m", from_s=TO - 3600, to_s=TO, conn=tmp_db)
    assert out.ok and wire.reserved == [("market.kline", Priority.RESEARCH, 2)]


# ------------------------------------------------------------------ the run_read boundary


def test_kline_args_under_another_endpoint_are_refused_before_cache_and_limiter(tmp_db, wire):
    out = g.run_read("token.info", args(), conn=tmp_db)
    assert refused(out) and "does not match" in out.receipt.note
    assert wire.spawned == [] and wire.reserved == []


def test_the_kline_endpoint_with_another_command_is_refused(tmp_db, wire):
    out = g.run_read("market.kline", ["token", "info", "--address", MINT, "--chain", "sol"], conn=tmp_db)
    assert refused(out) and "does not match" in out.receipt.note
    assert wire.spawned == [] and wire.reserved == []


@pytest.mark.parametrize("bad", [
    args(resolution="1s"), args(resolution="30s"), args(resolution="15m"), args(resolution="1h"),
    args(chain="bsc"), args(address="0x" + "a" * 40), args(address="short"),
    args(bars=201), args(resolution="5m", bars=201),
    args(to=NOW_S + 600),                                              # ends in the future
    ["market", "kline", "--chain", "sol", "--address", MINT, "--resolution", "1m", "--from", str(TO), "--to", str(TO)],
    ["market", "kline", "--chain", "sol", "--address", MINT, "--resolution", "1m", "--from", "1.5", "--to", str(TO)],
    ["market", "kline", "--chain", "sol", "--address", MINT, "--resolution", "1m", "--from", "1000", "--to", "2000"],
    ["market", "kline", "--chain", "sol", "--address", MINT, "--resolution", "1m", "--to", str(TO)],   # no --from
    args(extra=("--limit", "5")),
    ["market", "kline", "--chain", "sol", "--chain", "sol", "--address", MINT, "--resolution", "1m",
     "--from", str(TO - 60), "--to", str(TO)],
])
def test_a_raw_caller_cannot_widen_the_read(tmp_db, wire, bad):
    out = g.run_read("market.kline", bad, conn=tmp_db)
    assert refused(out), bad
    assert wire.spawned == [] and wire.reserved == []


@pytest.mark.parametrize("kw", [
    {"priority": Priority.EXIT}, {"priority": Priority.POSITION}, {"priority": Priority.ENTRY},
    {"priority": Priority.DISCOVERY}, {"wait_for_slot_s": 1.0}, {"stale_grace_s": 60.0},
])
def test_a_raw_caller_cannot_borrow_capacity_wait_or_read_stale(tmp_db, wire, kw):
    out = g.run_read("market.kline", args(), conn=tmp_db, **kw)
    assert refused(out) and wire.spawned == [] and wire.reserved == []


@pytest.mark.parametrize("resolution, bars", [("1m", 100), ("1m", 101), ("1m", 200), ("5m", 200)])
def test_ranges_up_to_the_hard_ceiling_are_allowed(tmp_db, wire, resolution, bars):
    """R1 needs one range longer than 100 bars; 200 is the ceiling, 201 is refused above."""
    out = g.run_read("market.kline", args(resolution=resolution, bars=bars), conn=tmp_db)
    assert out.ok and len(wire.spawned) == 1


def test_the_typed_helper_builds_exactly_the_bounded_command(tmp_db, wire):
    out = g.market_kline(MINT, resolution="5m", from_s=TO - 3000, to_s=TO, conn=tmp_db)
    assert out.ok
    assert wire.spawned == [["node", "index.js", "market", "kline", "--chain", "sol", "--address", MINT,
                             "--resolution", "5m", "--from", str(TO - 3000), "--to", str(TO), "--raw"]]


@pytest.mark.parametrize("frm, to", [(True, TO), (TO - 60, 1.0e9), ("1", TO)])
def test_the_typed_helper_refuses_non_integer_times(tmp_db, wire, frm, to):
    assert refused(g.market_kline(MINT, resolution="1m", from_s=frm, to_s=to, conn=tmp_db))
    assert wire.spawned == []


def span(resolution, seconds, to=TO):
    return ["market", "kline", "--chain", "sol", "--address", MINT, "--resolution", resolution,
            "--from", str(to - seconds), "--to", str(to)]


@pytest.mark.parametrize("resolution, seconds", [("1m", 200 * 60 + 1), ("1m", 200 * 60 + 59), ("5m", 200 * 300 + 1)])
def test_the_bar_ceiling_is_exact_not_floored(tmp_db, wire, resolution, seconds):
    """Codex boundary review: floor division let 200 bars plus up to 59 s through."""
    assert refused(g.run_read("market.kline", span(resolution, seconds), conn=tmp_db))
    assert wire.spawned == [] and wire.reserved == []


@pytest.mark.parametrize("kw", [{"ttl_s": float("inf")}, {"ttl_s": float("nan")}, {"ttl_s": 3601.0},
                                {"ttl_s": -1.0}, {"timeout_s": 0}, {"timeout_s": -1.0},
                                {"timeout_s": float("inf")}, {"timeout_s": float("nan")}, {"timeout_s": 46.0},
                                {"timeout_s": True}])
def test_a_raw_caller_cannot_unbound_the_cache_life_or_the_call_time(tmp_db, wire, kw):
    assert refused(g.run_read("market.kline", args(), conn=tmp_db, **kw))
    assert wire.spawned == [] and wire.reserved == []


@pytest.mark.parametrize("kw", [{"ttl_s": 0.0}, {"ttl_s": 3600.0}, {"timeout_s": 45.0}, {"timeout_s": 0.5}])
def test_bounded_cache_life_and_call_time_are_accepted(tmp_db, wire, kw):
    assert g.run_read("market.kline", args(), conn=tmp_db, **kw).ok and len(wire.spawned) == 1


@pytest.mark.parametrize("cached", [{"not_candles": "bad"}, [1, 2], "ok", None])
def test_a_malformed_cached_value_is_a_miss_never_candles(tmp_db, wire, cached):
    """Codex boundary review: the cache-hit path returned before payload validation."""
    key = "gmgn-cli " + " ".join(args(bars=10) + ["--raw"])
    path = _http.cache_path(g.PROVIDER, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_ms": int(time.time() * 1000) - 5_000, "data": cached}))
    out = g.run_read("market.kline", args(bars=10), conn=tmp_db)
    assert out.ok and out.data == [CANDLE] and out.receipt.basis is EvidenceBasis.PROVIDER_REPORTED
    assert len(wire.spawned) == 1 and wire.reserved == [("market.kline", Priority.RESEARCH, 2)]


# ------------------------------------------------------------------ payload


@pytest.mark.parametrize("stdout", [json.dumps({"list": [CANDLE]}), json.dumps([CANDLE]),
                                    json.dumps({"code": 0, "data": {"list": [CANDLE]}})])
def test_candles_come_back_untouched_with_both_raw_volume_fields(tmp_db, wire, stdout):
    wire.stdout = stdout
    out = g.market_kline(MINT, resolution="1m", from_s=TO - 600, to_s=TO, conn=tmp_db)
    assert out.ok and out.receipt.basis is EvidenceBasis.PROVIDER_REPORTED
    assert out.data == [CANDLE]  # strings stay strings; neither volume nor amount is relabelled
    assert g.KLINE_VOLUME_UNIT == "UNKNOWN"


def test_an_explicit_empty_list_is_data_not_unavailable(tmp_db, wire):
    wire.stdout = json.dumps({"list": []})
    out = g.market_kline(MINT, resolution="1m", from_s=TO - 600, to_s=TO, conn=tmp_db)
    assert out.ok and out.data == [] and out.receipt.basis is EvidenceBasis.PROVIDER_REPORTED


@pytest.mark.parametrize("stdout", [json.dumps({"candles": [CANDLE]}), json.dumps({"list": None}),
                                    json.dumps({"list": [1, 2]}), json.dumps({"list": {"a": 1}}),
                                    json.dumps("ok"), "not json"])
def test_a_missing_or_malformed_payload_is_unavailable(tmp_db, wire, stdout):
    wire.stdout = stdout
    out = g.market_kline(MINT, resolution="1m", from_s=TO - 600, to_s=TO, conn=tmp_db)
    assert refused(out)


# ------------------------------------------------------------------ cache


def test_a_fresh_cached_range_is_reused_and_an_expired_one_is_never_served(tmp_db, wire):
    first = g.market_kline(MINT, resolution="1m", from_s=TO - 600, to_s=TO, conn=tmp_db)
    again = g.market_kline(MINT, resolution="1m", from_s=TO - 600, to_s=TO, conn=tmp_db)
    assert first.ok and again.ok and again.receipt.basis is EvidenceBasis.CACHED
    assert len(wire.spawned) == 1
    key = "gmgn-cli " + " ".join(args(bars=10) + ["--raw"])
    path = _http.cache_path(g.PROVIDER, key)
    path.write_text(json.dumps({"fetched_ms": int(time.time() * 1000) - 3_601_000, "data": [CANDLE]}))
    late = g.market_kline(MINT, resolution="1m", from_s=TO - 600, to_s=TO, conn=tmp_db)
    assert late.ok and late.receipt.basis is EvidenceBasis.PROVIDER_REPORTED and len(wire.spawned) == 2


def test_a_raw_caller_without_a_grace_still_never_gets_a_stale_range(tmp_db, wire):
    key = "gmgn-cli " + " ".join(args(bars=10) + ["--raw"])
    path = _http.cache_path(g.PROVIDER, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_ms": int(time.time() * 1000) - 3_601_000, "data": [CANDLE]}))
    out = g.run_read("market.kline", args(bars=10), conn=tmp_db)  # stale_grace_s left to the defaults
    assert out.ok and out.receipt.basis is EvidenceBasis.PROVIDER_REPORTED and len(wire.spawned) == 1


# ------------------------------------------------------------------ the child's own retry


def test_the_child_process_is_told_never_to_retry_a_429_itself(monkeypatch):
    """gmgn-cli 1.6.1 retries an API-key GET once when the reset is inside its max wait; the env
    below makes every wait exceed it (dist/client/OpenApiClient.js getRateLimitRetryDelayMs)."""
    seen = {}

    class FakeProc:
        returncode = 0

        def __init__(self, argv, **kw):
            seen.update(kw)

        def communicate(self, timeout=None):
            return json.dumps({"list": []}), ""

    monkeypatch.setattr(g.subprocess, "Popen", FakeProc)
    raw = g._spawn(["node", "index.js", *args(), "--raw"], 5.0)
    assert raw.returncode == 0
    assert seen["env"]["GMGN_RATE_LIMIT_AUTO_RETRY_MAX_WAIT_MS"] == "0"
    assert "GMGN_DEBUG" not in seen["env"] and seen["env"]["GMGN_ALLOW_AUTOMATED_TRADES"] == "0"
