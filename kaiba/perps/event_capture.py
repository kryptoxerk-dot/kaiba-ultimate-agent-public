"""Bounded public BTC market-data research; no credentials, account or order path.

capture: one Bybit public connection, exact received payloads, no reconnect/retry.
export: causal minute observations for reversal_research; funding stays UNKNOWN.
Prices/volume are observed feed values, not account executions or complete tape.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import shutil
import time
from collections import Counter, defaultdict
from pathlib import Path

SCHEMA = "kaiba/perps-event-capture/2"
URL = "wss://stream.bybit.com/v5/public/linear"
SYMBOL = "BTCUSDT"
TOPICS = (f"allLiquidation.{SYMBOL}", f"publicTrade.{SYMBOL}", f"tickers.{SYMBOL}",
          f"kline.1.{SYMBOL}")
MAX_BYTES, MAX_FRAMES, MAX_SECONDS = 32 * 1024 * 1024, 100_000, 3600
MINUTE, GRACE_MS, OBSERVATION_GAP_MS = 60_000, 1000, 5000
SEND_TIMEOUT_S = 3


def clock():
    return {"received_ms": time.time_ns() // 1_000_000, "mono_ns": time.monotonic_ns()}


def integer(x):
    if type(x) is not int or x < 0:
        raise ValueError("invalid_clock")
    return x


def number(x, *, positive=True):
    if type(x) not in (str, int, float):
        raise ValueError("invalid_number")
    value = float(x)
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError("invalid_number")
    return value


async def capture(path, seconds=90):
    """One bounded read-only subscription. Failure is recorded, never retried."""
    from websockets.asyncio.client import connect

    from kaiba.providers.provider_policy import require_network

    if type(seconds) is not int or not 1 <= seconds <= MAX_SECONDS:
        raise ValueError("duration_bound")
    require_network(URL)  # preserve the shared no-Alchemy policy; fixed URL only
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(path.parent).free < 1024 ** 3:
        raise ValueError("research_disk_reserve")
    total, frames, reason, error = 0, 0, "duration", None
    with path.open("xb") as dest:  # refuse overwrite BEFORE any socket is opened
        def write(row, *, terminal=False):
            nonlocal total
            encoded = (json.dumps(row, allow_nan=False, separators=(",", ":")) + "\n").encode()
            if total + len(encoded) > MAX_BYTES - (0 if terminal else 4096):
                return False
            dest.write(encoded)
            total += len(encoded)
            return True

        write({"kind": "header", "schema": SCHEMA, "endpoint": URL, "symbol": SYMBOL,
               "topics": list(TOPICS), "requested_seconds": seconds, **clock()})
        try:
            async with connect(URL, open_timeout=10, close_timeout=2, ping_interval=None,
                               max_size=262_144, max_queue=32) as ws:
                await asyncio.wait_for(ws.send(json.dumps({"op": "subscribe", "req_id": "capture", "args": TOPICS})),
                                       SEND_TIMEOUT_S)
                deadline = time.monotonic() + seconds
                next_ping = time.monotonic() + 20
                while frames < MAX_FRAMES:
                    now = time.monotonic()
                    if now >= deadline:
                        break
                    if now >= next_ping:
                        await asyncio.wait_for(ws.send('{"op":"ping"}'), SEND_TIMEOUT_S)
                        next_ping = now + 20
                    try:
                        raw = await asyncio.wait_for(ws.recv(), min(deadline - now, max(.01, next_ping - now)))
                    except TimeoutError:
                        continue
                    if not isinstance(raw, str):
                        raise ValueError("unexpected_binary_frame")
                    if not write({"kind": "frame", **clock(), "payload": raw}):
                        reason = "byte_cap"
                        break
                    frames += 1
                    if frames % 500 == 0:
                        dest.flush()
                else:
                    reason = "frame_cap"
        except Exception as exc:
            reason, error = "provider_error", type(exc).__name__  # no exception URLs/headers/secrets
        footer = {"kind": "footer", **clock(), "reason": reason, "error_class": error,
                  "frames": frames, "bytes_before_footer": total}
        if not write(footer, terminal=True):
            raise ValueError("footer_budget")
    return footer


def read_capture(path):
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("capture_byte_bound")
    rows = []
    with path.open(encoding="utf-8") as source:
        for line in source:
            if len(rows) >= MAX_FRAMES + 2:
                raise ValueError("capture_frame_bound")
            rows.append(json.loads(line))
    if (len(rows) < 2 or rows[0].get("kind") != "header" or rows[-1].get("kind") != "footer"
            or any(r.get("kind") != "frame" for r in rows[1:-1])):
        raise ValueError("interrupted_or_malformed_capture")
    head, foot = rows[0], rows[-1]
    if (head.get("schema") != SCHEMA or head.get("endpoint") != URL or head.get("symbol") != SYMBOL
            or head.get("topics") != list(TOPICS) or foot.get("frames") != len(rows) - 2):
        raise ValueError("capture_provenance")
    duration = head.get("requested_seconds")
    if type(duration) is not int or not 1 <= duration <= MAX_SECONDS:
        raise ValueError("capture_duration_bound")
    if foot.get("reason") in ("byte_cap", "frame_cap"):
        raise ValueError("capture_censored_by_resource_cap")
    if foot.get("reason") != "duration":
        raise ValueError("capture_not_cleanly_closed")
    last_w, last_m = -1, -1
    w0, m0 = integer(head["received_ms"]), integer(head["mono_ns"])
    for r in rows:
        wall, mono = integer(r["received_ms"]), integer(r["mono_ns"])
        if wall < last_w or mono <= last_m or abs((wall - w0) - (mono - m0) / 1e6) > 2000:
            raise ValueError("capture_clock_jump")
        last_w, last_m = wall, mono
    if last_w - w0 > (MAX_SECONDS + 30) * 1000:
        raise ValueError("capture_duration_bound")
    return rows


def export(rows, fee_bps, slippage_bps):
    """Observed-delivery sums only; unavailable/late coverage never becomes venue zero."""
    from kaiba.perps.reversal_research import VERSION, validate

    for x in (fee_bps, slippage_bps):
        if number(x, positive=False) > 100:
            raise ValueError("cost_bound")
    observed_end = rows[-2]["received_ms"]
    ack, last_market = None, None
    ticks, last_quote, last_tick, quotes = None, -1, -1, []
    confirmed, events, trade_ids, invalid_minutes = {}, defaultdict(list), {}, set()
    observation_gaps = []
    counts = Counter()
    for row in rows[1:-1]:
        received = row["received_ms"]
        msg = json.loads(row["payload"])
        if not isinstance(msg, dict):
            raise ValueError("payload_shape")
        if msg.get("op") == "subscribe" and msg.get("req_id") == "capture":
            if msg.get("success") is not True or ack is not None:
                raise ValueError("subscription_refused_or_repeated")
            ack = received
            continue
        topic = msg.get("topic")
        if topic not in TOPICS:
            continue
        if ack is None:
            raise ValueError("market_before_ack")
        previous_market = last_market if last_market is not None else ack
        if received - previous_market > OBSERVATION_GAP_MS:
            observation_gaps.append((previous_market + OBSERVATION_GAP_MS, received))
            counts["observation_gaps"] += 1
        last_market = received
        counts[topic] += 1
        if topic == f"tickers.{SYMBOL}":
            data, t = msg["data"], integer(msg["ts"])
            if not isinstance(data, dict):
                raise ValueError("ticker_shape")
            if msg.get("type") == "snapshot":
                ticks = dict(data)
            elif msg.get("type") == "delta" and ticks is not None:
                ticks.update(data)
            else:
                raise ValueError("ticker_requires_snapshot")
            if ticks.get("symbol") != SYMBOL:
                raise ValueError("symbol_mismatch")
            bid, ask, mark = [number(ticks[k]) for k in ("bid1Price", "ask1Price", "markPrice")]
            if bid > ask or t < last_tick:
                raise ValueError("crossed_or_out_of_order_quote")
            last_tick = t
            if t > received:
                counts["future_quote_clock"] += 1
                invalid_minutes.add(t // MINUTE * MINUTE)
            elif t == last_quote:
                counts["same_timestamp_quote_omitted"] += 1
            else:
                quotes.append({"t": t, "available_ms": received, "bid": bid, "ask": ask, "mark": mark})
                last_quote = t
        elif topic in (f"allLiquidation.{SYMBOL}", f"publicTrade.{SYMBOL}"):
            if not isinstance(msg["data"], list) or len(msg["data"]) > 1024:
                raise ValueError("event_batch_bound")
            for item in msg["data"]:
                t, side = integer(item["T"]), item["S"]
                if item["s"] != SYMBOL or side not in ("Buy", "Sell"):
                    raise ValueError("event_symbol_or_side")
                price, quantity = number(item["p"]), number(item["v"])
                value = price * quantity
                if not math.isfinite(value):
                    raise ValueError("notional_overflow")
                liquidation = topic.startswith("allLiquidation.")
                if not liquidation:
                    identity = item["i"]
                    if not isinstance(identity, str) or not identity or len(identity) > 128:
                        raise ValueError("trade_id_required")
                    fingerprint = (t, side, price, quantity)
                    if identity in trade_ids:
                        if trade_ids[identity] != fingerprint:
                            raise ValueError("trade_id_conflict")
                        counts["duplicate_trade_id"] += 1
                        continue
                    trade_ids[identity] = fingerprint
                if counts["normalized_events"] >= 200_000:
                    raise ValueError("normalized_event_bound")
                events[t // MINUTE * MINUTE].append((received, liquidation, side, value))
                counts["normalized_events"] += 1
                if t > received:
                    invalid_minutes.update((t // MINUTE * MINUTE, received // MINUTE * MINUTE))
                    counts["future_event_clock"] += 1
        elif topic == f"kline.1.{SYMBOL}":
            for b in msg["data"]:
                if b.get("confirm") is not True:
                    continue
                t = integer(b["start"])
                if t % MINUTE or integer(b["end"]) != t + MINUTE - 1 or b["interval"] != "1":
                    raise ValueError("closed_bar_clock")
                prices = [number(b[k]) for k in ("open", "high", "low", "close")]
                o, h, low, c = prices
                if not low <= min(o, c) <= max(o, c) <= h:
                    raise ValueError("closed_bar_prices")
                bar = {"t": t, "available_ms": max(t + MINUTE + GRACE_MS, received),
                       "o": o, "h": h, "l": low, "c": c, "volume": number(b["volume"], positive=False)}
                if t in confirmed:
                    previous = confirmed[t]
                    if any(previous[k] != bar[k] for k in ("o", "h", "l", "c", "volume")):
                        raise ValueError("confirmed_bar_revision")
                else:
                    confirmed[t] = bar
    if ack is None:
        raise ValueError("subscription_not_acknowledged")
    observed_end = min(observed_end, last_market if last_market is not None else ack)
    bars = []
    for t, bar in sorted(confirmed.items()):
        if t < ack or bar["available_ms"] > observed_end:
            counts["partial_or_unfinished_minute"] += 1
            continue
        usable = t not in invalid_minutes and not any(a <= bar["available_ms"] and b >= t
                                                       for a, b in observation_gaps)
        values = {k: 0.0 if usable else None for k in ("long_liq_usd", "short_liq_usd", "buy_usd", "sell_usd")}
        for received, liq, side, value in events[t]:
            if received > bar["available_ms"]:
                counts["late_delivery_excluded"] += 1
            elif usable:
                key = (("long_liq_usd" if side == "Buy" else "short_liq_usd") if liq
                       else ("buy_usd" if side == "Buy" else "sell_usd"))
                values[key] += value
        bars.append({**bar, **values, "liq_basis": "observed" if usable else "unknown"})
    data = {"schema": VERSION, "symbol": SYMBOL, "venue": "bybit_public_linear",
            "source": SCHEMA, "contract_type": "linear", "quote_asset": "USDT",
            "fee_bps_per_side": float(fee_bps), "slippage_bps_per_side": float(slippage_bps),
            "funding_complete": False, "funding": [], "bars": bars, "quotes": quotes,
            "capture_quality": {"counts": dict(counts), "exchange_completeness_proven": False,
                                "acknowledged_ms": ack, "grace_ms": GRACE_MS,
                                "last_recorded_frame_ms": observed_end,
                                "liquidation_metric": "delivered_size_times_BANKRUPTCY_reference_not_fill",
                                "zero_semantics": "zero_delivered_in_observed_window_not_zero_venue_activity",
                                "funding": "ticker_forecast_is_not_settlement_evidence",
                                "execution_qualified": False}}
    validate(data)
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    live = sub.add_parser("capture")
    live.add_argument("--output", type=Path, required=True)
    live.add_argument("--seconds", type=int, default=90)
    offline = sub.add_parser("export")
    offline.add_argument("--input", type=Path, required=True)
    offline.add_argument("--output", type=Path, required=True)
    offline.add_argument("--fee-bps", type=float, required=True)
    offline.add_argument("--slippage-bps", type=float, required=True)
    args = parser.parse_args()
    if args.command == "capture":
        result = asyncio.run(capture(args.output, args.seconds))
        print(json.dumps(result))
        if result["reason"] == "provider_error":
            raise SystemExit(2)
    else:
        if args.output.exists():
            parser.error("output already exists")
        data = export(read_capture(args.input), args.fee_bps, args.slippage_bps)
        data["capture_sha256"] = hashlib.sha256(args.input.read_bytes()).hexdigest()
        with args.output.open("x", encoding="utf-8") as dest:
            json.dump(data, dest, allow_nan=False)
        print(json.dumps({"bars": len(data["bars"]), "quotes": len(data["quotes"]),
                          "quality": data["capture_quality"], "funding_complete": False}))


if __name__ == "__main__":
    main()
