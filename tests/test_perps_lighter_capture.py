"""Public feed contracts and bounded transport, with synthetic messages only."""

import asyncio
import hashlib
import json
import sys
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest

from kaiba.perps import lighter_capture as lc


def metadata(identity=7):
    return {
        "code": 200,
        "order_book_details": [
            {
                "symbol": "BTC",
                "market_type": "perp",
                "status": "active",
                "market_id": identity,
                "base_asset_id": 8,
                "quote_asset_id": 99,
                "supported_size_decimals": 6,
                "supported_price_decimals": 1,
                "min_base_amount": ".0001",
                "min_quote_amount": "5",
            }
        ],
        "spot_order_book_details": [],
    }


def message(kind, snapshot=True, **kwargs):
    return {
        "type": ("subscribed/" if snapshot else "update/") + kind,
        "channel": kind + (":7:1m" if kind == "candle" else ":7"),
        **kwargs,
    }


def book(snapshot=True, nonce=10, begin=9, **kwargs):
    return message(
        "order_book",
        snapshot,
        offset=99999999,
        order_book={
            "code": 0,
            "nonce": nonce,
            "begin_nonce": begin,
            "asks": [{"price": "101", "size": "2"}],
            "bids": [{"price": "100", "size": "3"}],
            **kwargs,
        },
    )


def trade(identity=2**55, category="liquidation", maker=False):
    return {
        "trade_id": identity,
        "trade_id_str": str(identity),
        "market_id": 7,
        "type": category,
        "timestamp": 60_000,
        "transaction_time": 60_000_007,
        "size": "2",
        "price": "100",
        "usd_amount": "200",
        "is_maker_ask": maker,
    }


def tape(extra=(), reason="duration"):
    payloads = [
        {"type": "connected"},
        book(),
        message("trade", trades=[], liquidation_trades=[]),
        message(
            "market_stats",
            market_stats={
                "market_id": 7,
                "symbol": "BTC",
                "index_price": "110",
                "mark_price": "100",
                "current_funding_rate": ".5",
                "funding_rate": ".1",
                "funding_timestamp": 0,
            },
        ),
        message("candle", candles=[{"t": 60_000}]),
        *extra,
    ]
    rows = [
        {
            "kind": "header",
            "schema": lc.SCHEMA,
            "endpoint": lc.URL,
            "metadata_url": lc.METADATA,
            "symbol": "BTC",
            "requested_seconds": 90,
            "received_ms": 0,
            "mono_ns": 0,
        },
        {"kind": "metadata", "payload": json.dumps(metadata()), "received_ms": 1, "mono_ns": 1_000_000},
    ]
    for i, payload in enumerate(payloads, 2):
        rows.append(
            {"kind": "frame", "payload": json.dumps(payload), "received_ms": i, "mono_ns": i * 1_000_000}
        )
    rows.append(
        {
            "kind": "footer",
            "reason": reason,
            "frames": len(payloads),
            "received_ms": 90_000,
            "mono_ns": 90_000_000_000,
            "error_class": "ConnectionError" if reason == "provider_error" else None,
        }
    )
    return rows


def save(tmp_path, rows):
    path = tmp_path / "fixture.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return path


def test_metadata_discovers_identity_without_assuming_btc_or_currency_id():
    m = lc.market_metadata(metadata())
    assert m["market_id"] == 7 and m["quote_asset_id"] == 99
    assert lc.topics(m) == ["order_book/7", "trade/7", "market_stats/7", "candle/7/1m"]
    report = lc.summarize(tape())
    assert report["quote_currency_symbol"] is None


@pytest.mark.parametrize("fault", ["duplicate", "inactive", "spot", "wrong_symbol", "boolean_id"])
def test_metadata_cannot_silently_select_another_market(fault):
    obj = metadata()
    m = obj["order_book_details"][0]
    if fault == "duplicate":
        obj["order_book_details"].append(deepcopy(m))
    elif fault == "inactive":
        m["status"] = "inactive"
    elif fault == "spot":
        m["market_type"] = "spot"
    elif fault == "wrong_symbol":
        m["symbol"] = "ETH"
    else:
        m["market_id"] = True
    with pytest.raises(ValueError):
        lc.market_metadata(obj)


def test_book_nonce_continuity_ignores_offset_jumps_and_zero_removes_levels():
    b = lc.Book()
    b.update(book(), True)
    update = book(False, 15, 10, asks=[{"price": "101", "size": "0"}, {"price": "102", "size": "1"}])
    b.update(update, False)
    assert b.synced and b.gaps == 0 and b.nonce == 15
    assert min(b.asks) == Decimal(102) and Decimal(101) not in b.asks


def test_book_nonce_gap_cannot_recover_without_full_snapshot():
    b = lc.Book()
    b.update(book(), True)
    before = b.observations
    b.update(book(False, 12, 9), False)
    b.update(book(False, 13, 10), False)
    assert not b.synced and b.gaps == 2 and b.observations == before
    b.update(book(True, 50, 49), True)
    assert b.synced and b.nonce == 50


def test_crossed_or_error_book_never_produces_quote():
    b = lc.Book()
    with pytest.raises(ValueError, match="crossed_book"):
        b.update(book(bids=[{"price": "102", "size": "1"}]), True)
    with pytest.raises(ValueError, match="book_status"):
        b.update(book(code=5), True)


def test_trade_dedup_across_arrays_preserves_large_id_and_unknown_direction():
    t = trade()
    report = lc.summarize(tape([message("trade", False, trades=[t], liquidation_trades=[t])]))
    assert report["counts"]["received_liquidation"] == 1
    assert report["counts"]["duplicate_trade_id"] == 1
    assert report["long_liquidation_notional"] is report["short_liquidation_notional"] is None
    assert report["directional_liquidation_qualified"] is False


def test_ordinary_liquidation_adl_settlement_not_conflated():
    ts = [
        trade(i, category)
        for i, category in enumerate(("trade", "liquidation", "deleverage", "market-settlement"), 1)
    ]
    ts[2]["price"] = "0"
    report = lc.summarize(tape([message("trade", False, trades=ts, liquidation_trades=[])]))
    assert all(
        report["counts"]["received_" + k] == 1
        for k in ("trade", "liquidation", "deleverage", "market-settlement")
    )
    assert report["usable_for_economic_replay"] is False


def test_snapshot_trades_are_not_new_update_deliveries():
    report = lc.summarize(tape([message("trade", True, trades=[trade()], liquidation_trades=[])]))
    assert report["counts"]["snapshot_liquidation"] == 1
    assert report["counts"].get("update_delivered_liquidation", 0) == 0


@pytest.mark.parametrize("fault", ["precision", "conflict", "wrong_market", "wrong_category"])
def test_conflicting_or_wrong_identity_fails(fault):
    a, b = trade(), trade()
    if fault == "precision":
        b["trade_id_str"] = "1"
    elif fault == "conflict":
        b["size"] = "3"
    elif fault == "wrong_market":
        b["market_id"] = 2
    else:
        b["type"] = "unknown"
    with pytest.raises(ValueError):
        lc.summarize(tape([message("trade", False, trades=[a, b], liquidation_trades=[])]))


def test_forecast_and_last_rate_never_become_settled_funding_cashflow():
    r = lc.summarize(tape())
    assert r["funding_complete"] is False and r["funding_price_basis"] == "index"
    assert r["normalized_funding_rate"] is None and r["funding_rate_unit"] == "UNVERIFIED"
    assert r["client_latency_inferred"] is False


def test_initial_candle_not_closed_and_only_rollover_marks_closure():
    r = lc.summarize(tape())
    assert r["rollover_closed_candles"] == 0
    r = lc.summarize(tape([message("candle", False, candles=[{"t": 60_000}, {"t": 120_000}])]))
    assert r["rollover_closed_candles"] == 1
    with pytest.raises(ValueError, match="rollover_clock"):
        lc.summarize(tape([message("candle", False, candles=[{"t": 60_000}, {"t": 180_000}])]))


def test_private_channel_and_unacknowledged_update_are_refused():
    with pytest.raises(ValueError, match="public_channel"):
        lc.summarize(tape([{"type": "update/account_all", "channel": "account_all:7"}]))
    rows = tape()
    rows[3]["payload"] = json.dumps(book(False))
    with pytest.raises(ValueError, match="update_before_subscription"):
        lc.summarize(rows)


@pytest.mark.parametrize("reason", ["byte_cap", "frame_cap", "provider_error"])
def test_cap_or_provider_failure_remains_censored(tmp_path, reason):
    report = lc.summarize(lc.read_capture(save(tmp_path, tape(reason=reason))))
    assert report["status"] == "FAILED_OR_CENSORED"
    assert not any(report[k] for k in ("execution_allowed", "promotable", "profitability_proven"))


@pytest.mark.parametrize("fault", ["footer", "backward", "wall_jump", "wrong_endpoint", "frame_count"])
def test_reader_refuses_truncation_clock_and_provenance_failures(tmp_path, fault):
    rows = tape()
    if fault == "footer":
        rows.pop()
    elif fault == "backward":
        rows[4]["mono_ns"] = 1
    elif fault == "wall_jump":
        rows[4]["received_ms"] += 500
    elif fault == "wrong_endpoint":
        rows[0]["endpoint"] = "wss://example.test/account"
    else:
        rows[-1]["frames"] += 1
    with pytest.raises(ValueError):
        lc.read_capture(save(tmp_path, rows))


def test_reader_preserves_raw_execution_microseconds_and_block_clock(tmp_path):
    path = save(tmp_path, tape([message("trade", False, trades=[trade()], liquidation_trades=[])]))
    rows = lc.read_capture(path)
    raw = json.loads(rows[-2]["payload"])["trades"][0]
    assert raw["timestamp"] == 60_000 and raw["transaction_time"] == 60_000_007


def test_same_clock_tick_is_valid_but_file_order_is_preserved(tmp_path):
    rows = tape()
    rows[3]["received_ms"] = rows[2]["received_ms"]
    rows[3]["mono_ns"] = rows[2]["mono_ns"]
    assert lc.read_capture(save(tmp_path, rows))[3] == rows[3]


@pytest.mark.parametrize("fault", ["unknown_reason", "error_on_duration", "no_error_on_failure"])
def test_terminal_reason_and_error_must_be_consistent(tmp_path, fault):
    rows = tape()
    if fault == "unknown_reason":
        rows[-1]["reason"] = "invented"
    elif fault == "error_on_duration":
        rows[-1]["error_class"] = "ConnectionError"
    else:
        rows[-1]["reason"] = "provider_error"
    with pytest.raises(ValueError, match="terminal"):
        lc.read_capture(save(tmp_path, rows))


@pytest.mark.parametrize("kind", ["trade", "market_stats"])
@pytest.mark.parametrize("invalid", [True, 1.0])
def test_boolean_or_float_market_id_cannot_alias_integer_one(kind, invalid):
    rows = tape([message("trade", False, trades=[trade()], liquidation_trades=[])])
    rows[1]["payload"] = json.dumps(metadata(1))
    for row in rows:
        if row["kind"] != "frame":
            continue
        msg = json.loads(row["payload"])
        if "channel" in msg:
            msg["channel"] = msg["channel"].replace(":7", ":1")
        if "market_stats" in msg:
            msg["market_stats"]["market_id"] = 1
        for t in msg.get("trades", []):
            t["market_id"] = 1
        row["payload"] = json.dumps(msg)
    assert lc.summarize(rows)["status"] == "OBSERVED_UNQUALIFIED"
    for row in rows:
        if row["kind"] == "frame":
            msg = json.loads(row["payload"])
            if kind == "trade" and msg.get("trades"):
                msg["trades"][0]["market_id"] = invalid
            if kind == "market_stats" and "market_stats" in msg:
                msg["market_stats"]["market_id"] = invalid
            row["payload"] = json.dumps(msg)
    with pytest.raises(ValueError, match="invalid_clock"):
        lc.summarize(rows)


def test_cli_hash_binds_bytes_parsed_before_input_changes(tmp_path, monkeypatch):
    path = save(tmp_path, tape())
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    original = lc.summarize

    def summarize_then_change(rows):
        report = original(rows)
        path.write_text("changed later", encoding="utf-8")
        return report

    monkeypatch.setattr(lc, "summarize", summarize_then_change)
    output = tmp_path / "quality.json"
    monkeypatch.setattr(sys, "argv", ["lighter", "summarize", "--input", str(path), "--output", str(output)])
    assert lc.main() == 0
    assert json.loads(output.read_text())["capture_sha256"] == original_hash


def test_cli_censored_quality_returns_failure_status(tmp_path, monkeypatch):
    path = save(tmp_path, tape(reason="frame_cap"))
    output = tmp_path / "quality.json"
    monkeypatch.setattr(sys, "argv", ["lighter", "summarize", "--input", str(path), "--output", str(output)])
    assert lc.main() == 2
    assert json.loads(output.read_text())["status"] == "FAILED_OR_CENSORED"


def test_existing_file_refuses_before_metadata_or_socket(tmp_path, monkeypatch):
    path = tmp_path / "existing"
    path.write_text("preserve")

    async def forbidden():
        raise AssertionError("network should not run")

    monkeypatch.setattr(lc, "get_metadata", forbidden)
    with pytest.raises(FileExistsError):
        asyncio.run(lc.capture(path, 1))
    assert path.read_text() == "preserve"


@pytest.mark.parametrize("seconds", [0, 91, True])
def test_unbounded_capture_refused_before_network(tmp_path, seconds):
    with pytest.raises(ValueError, match="duration_bound"):
        asyncio.run(lc.capture(tmp_path / "bad", seconds))


def test_one_metadata_failure_is_recorded_without_retry(tmp_path, monkeypatch):
    calls = []

    async def failure():
        calls.append(1)
        raise ConnectionError("do not log a sensitive exception message")

    monkeypatch.setattr(lc, "get_metadata", failure)
    path = tmp_path / "failed"
    footer = asyncio.run(lc.capture(path, 1))
    assert calls == [1] and footer["reason"] == "provider_error"
    assert "sensitive" not in path.read_text()
    assert lc.summarize(lc.read_capture(path))["status"] == "NO_VALIDATED_MARKET"


def test_transport_sends_only_public_subscriptions_and_pong_once_connected(tmp_path, monkeypatch):
    import websockets.asyncio.client

    calls, sent = [], []
    packets = [r["payload"] for r in tape() if r["kind"] == "frame"] + ['{"type":"ping"}']

    class Socket:
        async def recv(self):
            return packets.pop(0) if packets else '{"type":"pong"}'

        async def send(self, raw):
            sent.append(json.loads(raw))

    class Connection:
        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *args):
            return None

    def connect(url, **kwargs):
        calls.append((url, kwargs))
        return Connection()

    async def get_metadata():
        return json.dumps(metadata())

    times = iter(i / 100 for i in range(1000))
    monkeypatch.setattr(lc, "time", SimpleNamespace(monotonic=lambda: next(times)))
    monkeypatch.setattr(lc, "get_metadata", get_metadata)
    monkeypatch.setattr(websockets.asyncio.client, "connect", connect)
    result = asyncio.run(lc.capture(tmp_path / "fake", 1))
    assert result["reason"] == "duration" and len(calls) == 1 and calls[0][1]["proxy"] is None
    assert sent[:4] == [
        {"type": "subscribe", "channel": c} for c in lc.topics(lc.market_metadata(metadata()))
    ]
    assert all(m["type"] in ("subscribe", "pong") for m in sent)
    assert {"type": "pong"} in sent
