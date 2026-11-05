"""Market-data causality tests. Synthetic fixtures cannot establish provider profit."""
import asyncio
import json

import pytest

from kaiba.perps import event_capture as ec
from kaiba.perps import reversal_research as lab

M = 60_000


def frame(received, payload):
    return {"kind": "frame", "received_ms": received, "mono_ns": received * 1_000_000 + 1,
            "payload": json.dumps(payload)}


def tape(extra=()):
    rows = [{"kind": "header", "schema": ec.SCHEMA, "endpoint": ec.URL, "symbol": ec.SYMBOL,
             "topics": list(ec.TOPICS), "requested_seconds": 180, "received_ms": 0, "mono_ns": 0},
            frame(100, {"op": "subscribe", "req_id": "capture", "success": True})]
    # Fresh same-connection market observations: no undocumented transport outage.
    for t in range(1000, 124_000, 1000):
        rows.append(frame(t, {"topic": "tickers.BTCUSDT", "type": "snapshot" if t == 1000 else "delta",
                              "ts": t - 1, "data": {"symbol": ec.SYMBOL, "bid1Price": "100",
                                                       "ask1Price": "101", "markPrice": "100.5"}}))
    rows.extend(extra)
    rows.append(frame(121_500, {"topic": "kline.1.BTCUSDT", "data": [
        {"start": M, "end": 2 * M - 1, "interval": "1", "confirm": True,
         "open": "100", "high": "102", "low": "99", "close": "101", "volume": "20"}]}))
    rows[1:] = sorted(rows[1:], key=lambda r: r["received_ms"])
    # Keep monotonic clocks strictly ordered even when messages share a wall ms.
    for i, row in enumerate(rows):
        row["mono_ns"] = row["received_ms"] * 1_000_000 + i
    rows.append({"kind": "footer", "received_ms": 125_000, "mono_ns": 125_000_000_000,
                 "reason": "duration", "error_class": None, "frames": len(rows) - 1})
    return rows


def liq(t, side="Buy", size="2"):
    return {"topic": "allLiquidation.BTCUSDT", "type": "snapshot", "ts": t + 1,
            "data": [{"T": t, "s": ec.SYMBOL, "S": side, "v": size, "p": "100"}]}


def trade(t, identity, side="Buy"):
    return {"topic": "publicTrade.BTCUSDT", "type": "snapshot", "ts": t + 1,
            "data": [{"T": t, "s": ec.SYMBOL, "S": side, "v": "1", "p": "100", "i": identity, "seq": 7}]}


def test_side_mapping_duplicate_liquidation_rows_and_bankruptcy_semantics():
    data = ec.export(tape([frame(70_001, liq(70_000)), frame(70_002, liq(70_000)),
                           frame(71_001, liq(71_000, "Sell", "3"))]), 5, 2)
    bar = data["bars"][0]
    assert bar["long_liq_usd"] == 400 and bar["short_liq_usd"] == 300
    assert "BANKRUPTCY" in data["capture_quality"]["liquidation_metric"]
    assert data["capture_quality"]["exchange_completeness_proven"] is False


def test_trade_ids_dedup_but_shared_sequence_does_not_drop_distinct_trades():
    data = ec.export(tape([frame(70_001, trade(70_000, "one")),
                           frame(70_002, trade(70_000, "one")),
                           frame(71_001, trade(71_000, "two", "Sell"))]), 5, 2)
    assert data["bars"][0]["buy_usd"] == 100
    assert data["bars"][0]["sell_usd"] == 100
    assert data["capture_quality"]["counts"]["duplicate_trade_id"] == 1


def test_late_events_never_change_an_earlier_signal():
    early = frame(80_001, liq(80_000))
    late = frame(123_100, liq(80_000, size="900"))
    before = ec.export(tape([early]), 5, 2)
    after = ec.export(tape([early, late]), 5, 2)
    assert before["bars"] == after["bars"]
    assert after["bars"][0]["available_ms"] == 121_500
    assert after["capture_quality"]["counts"]["late_delivery_excluded"] == 1


def test_ticker_delta_carries_only_unchanged_fields_and_preserves_first_timestamp():
    rows = tape([frame(70_500, {"topic": "tickers.BTCUSDT", "type": "delta", "ts": 70_499,
                               "data": {"bid1Price": "100.1"}})])
    data = ec.export(rows, 5, 2)
    q = next(q for q in data["quotes"] if q["t"] == 70_499)
    assert q["bid"] == 100.1 and q["ask"] == 101 and q["mark"] == 100.5
    assert q["available_ms"] == 70_500


def test_partial_first_minute_and_unfinished_grace_are_not_admitted():
    rows = tape()
    raw = next(r for r in rows[1:-1] if "kline.1" in r["payload"])
    item = json.loads(raw["payload"])
    item["data"][0].update(start=0, end=M - 1)
    raw["payload"] = json.dumps(item)
    assert ec.export(rows, 5, 2)["bars"] == []


def test_gap_makes_observed_flow_unknown_not_zero():
    rows = tape()
    rows = [r for r in rows if not (r["kind"] == "frame" and 75_000 <= r["received_ms"] <= 85_000)]
    data = ec.export(rows, 5, 2)
    b = data["bars"][0]
    assert b["liq_basis"] == "unknown" and b["long_liq_usd"] is None and b["buy_usd"] is None


def test_initial_silence_after_ack_is_not_full_observed_coverage():
    rows = tape()
    rows = [r for r in rows if r["kind"] != "frame" or r["received_ms"] == 100 or r["received_ms"] >= 70_000]
    msg = json.loads(rows[2]["payload"])
    msg["type"] = "snapshot"
    rows[2]["payload"] = json.dumps(msg)
    result = ec.export(rows, 5, 2)
    assert result["bars"][0]["liq_basis"] == "unknown"
    assert result["bars"][0]["buy_usd"] is None
    assert result["capture_quality"]["counts"]["observation_gaps"] == 1


def test_footer_clock_cannot_complete_grace_after_last_recorded_frame():
    rows = tape()
    rows = [r for r in rows if r["kind"] != "frame" or r["received_ms"] < 120_001]
    candle = {"topic": "kline.1.BTCUSDT", "data": [{"start": M, "end": 2 * M - 1,
               "interval": "1", "confirm": True, "open": "100", "high": "102",
               "low": "99", "close": "101", "volume": "20"}]}
    rows.insert(-1, frame(120_001, candle))
    assert rows[-1]["received_ms"] > 121_000
    assert ec.export(rows, 5, 2)["bars"] == []


def test_future_clock_state_cannot_leak_into_an_earlier_exchange_tick():
    bad = frame(70_500, {"topic": "tickers.BTCUSDT", "type": "delta", "ts": 100_000,
                         "data": {"markPrice": "100.6"}})
    with pytest.raises(ValueError, match="out_of_order"):
        ec.export(tape([bad]), 5, 2)


def test_future_exchange_clock_is_not_clamped_to_receipt():
    data = ec.export(tape([frame(70_500, {"topic": "tickers.BTCUSDT", "type": "delta", "ts": 70_501,
                                         "data": {"markPrice": "100.6"}})]), 5, 2)
    assert all(q["t"] != 70_501 for q in data["quotes"])
    assert data["bars"][0]["liq_basis"] == "unknown"


def test_funding_forecast_never_becomes_settlement_and_lab_remains_unproven():
    data = ec.export(tape([frame(70_500, {"topic": "tickers.BTCUSDT", "type": "delta", "ts": 70_499,
                                         "data": {"fundingRate": ".01", "nextFundingTime": "120000"}})]), 5, 2)
    assert data["funding"] == [] and data["funding_complete"] is False
    report = lab.run(data)
    assert report["profitability_proven"] is report["execution_allowed"] is report["promotable"] is False


def test_raw_delivery_to_closed_paper_trade_still_withholds_unreconciled_net():
    rows = tape([frame(80_001, liq(80_000, size="500"))])[:-1]
    for t in range(124_000, 190_000, 1000):
        rows.append(frame(t, {"topic": "tickers.BTCUSDT", "type": "delta", "ts": t - 1,
                               "data": {"bid1Price": "98" if t == 150_000 else "100",
                                        "ask1Price": "98.5" if t == 150_000 else "101",
                                        "markPrice": "98.2" if t == 150_000 else "100.5"}}))
    rows.append({"kind": "footer", "received_ms": 191_000})
    data = ec.export(rows, 5, 2)
    report = lab.run(data)
    book = report["results"]["LF"]["base"]
    assert book["candidates"] == 1 and book["closed"] == 1
    assert book["unknown_closed"] == 1 and book["net_pnl"] is None
    assert book["economic_coverage"] == "incomplete_or_empty"


@pytest.mark.parametrize("fault", ["missing_footer", "clock_backwards", "wrong_endpoint", "provider_error",
                                   "byte_cap", "frame_cap", "frame_count"])
def test_reader_refuses_incomplete_or_untrusted_capture(tmp_path, fault):
    rows = tape()
    if fault == "missing_footer":
        rows.pop()
    elif fault == "clock_backwards":
        rows[10]["received_ms"] = 0
    elif fault == "wrong_endpoint":
        rows[0]["endpoint"] = "wss://example.test/private"
    elif fault in ("provider_error", "byte_cap", "frame_cap"):
        rows[-1]["reason"] = fault
    else:
        rows[-1]["frames"] += 1
    p = tmp_path / "capture.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        ec.read_capture(p)


def test_unused_high_rate_book_subscription_cannot_starve_event_budget():
    assert ec.TOPICS == ("allLiquidation.BTCUSDT", "publicTrade.BTCUSDT",
                         "tickers.BTCUSDT", "kline.1.BTCUSDT")


def test_reader_preserves_actual_clocks(tmp_path):
    rows = tape()
    p = tmp_path / "capture.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert ec.read_capture(p) == rows


def test_live_failure_is_one_attempt_public_only_and_raw_error_is_not_logged(tmp_path, monkeypatch):
    calls, sent = [], []
    class Fake:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            return None
        async def send(self, message):
            sent.append(json.loads(message))
        async def recv(self):
            raise RuntimeError("do not print exception credentials or headers")
    def connect(url, **kwargs):
        calls.append((url, kwargs))
        return Fake()
    monkeypatch.setattr("websockets.asyncio.client.connect", connect)
    p = tmp_path / "capture.jsonl"
    foot = asyncio.run(ec.capture(p, 1))
    assert len(calls) == 1 and calls[0][0] == ec.URL
    assert sent == [{"op": "subscribe", "req_id": "capture", "args": list(ec.TOPICS)}]
    assert foot["reason"] == "provider_error" and foot["error_class"] == "RuntimeError"
    assert "credentials" not in p.read_text()
    with pytest.raises(FileExistsError):
        asyncio.run(ec.capture(p, 1))
    assert len(calls) == 1


def test_stalled_subscribe_is_time_bounded_without_retry(tmp_path, monkeypatch):
    calls = []
    class Stalled:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            return None
        async def send(self, _):
            await asyncio.Event().wait()
    def connect(*args, **_):
        calls.append(args)
        return Stalled()
    monkeypatch.setattr("websockets.asyncio.client.connect", connect)
    monkeypatch.setattr(ec, "SEND_TIMEOUT_S", .01)
    result = asyncio.run(ec.capture(tmp_path / "stalled.jsonl", 1))
    assert len(calls) == 1 and result["error_class"] == "TimeoutError"
    assert result["frames"] == 0 and result["reason"] == "provider_error"


def test_cli_reports_capture_failure_as_nonzero(tmp_path, monkeypatch):
    async def failed(*_):
        return {"reason": "provider_error", "error_class": "ConnectionRefusedError", "frames": 0}
    monkeypatch.setattr(ec, "capture", failed)
    monkeypatch.setattr("sys.argv", ["event_capture", "capture", "--output", str(tmp_path / "capture")])
    with pytest.raises(SystemExit) as result:
        ec.main()
    assert result.value.code == 2


@pytest.mark.parametrize("x", [float("nan"), float("inf"), -1, True])
def test_invalid_costs_never_default_to_zero(x):
    with pytest.raises(ValueError):
        ec.export(tape(), x, 0)
