"""Bounded keyless Lighter BTC research tape and quality summary, never orders.

Raw public fields are retained; liquidation direction, funding cashflow, quote
currency, exchange-wide completeness and execution profitability remain unknown.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import time
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from kaiba.perps.event_capture import clock, integer

SCHEMA = "kaiba/lighter-public-capture/1"
URL = "wss://mainnet.zklighter.elliot.ai/stream"
METADATA = "https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails"
MAX_BYTES, MAX_FRAMES, MAX_SECONDS = 8 * 1024**2, 10_000, 90
MAX_METADATA = 2 * 1024**2
KINDS = ("order_book", "trade", "market_stats", "candle")


def decimal(value, *, zero=False):
    if type(value) not in (str, int, float):
        raise ValueError("numeric_type")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("numeric_value") from exc
    if (
        not number.is_finite()
        or number < 0
        or (not zero and number == 0)
        or len(str(value)) > 80
        or (number != 0 and not -24 <= number.adjusted() <= 24)
    ):
        raise ValueError("numeric_value")
    return number


def market_metadata(obj):
    if not isinstance(obj, dict) or type(obj.get("code")) is not int or obj["code"] != 200:
        raise ValueError("metadata_status")
    matches = [
        m
        for m in obj["order_book_details"]
        if m.get("symbol") == "BTC" and m.get("market_type") == "perp" and m.get("status") == "active"
    ]
    if len(matches) != 1:
        raise ValueError("unique_active_btc_perp_required")
    m = matches[0]
    integer(m["market_id"])
    for key in ("base_asset_id", "quote_asset_id", "supported_size_decimals", "supported_price_decimals"):
        integer(m[key])
    for key in ("min_base_amount", "min_quote_amount"):
        decimal(m[key])
    return m


def topics(market):
    identity = integer(market["market_id"])
    return [
        f"order_book/{identity}",
        f"trade/{identity}",
        f"market_stats/{identity}",
        f"candle/{identity}/1m",
    ]


async def get_metadata():
    import httpx

    async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
        async with client.stream(
            "GET", METADATA, params={"filter": "perp"}, headers={"Accept-Encoding": "identity"}
        ) as response:
            if response.status_code != 200:
                raise ValueError(f"metadata_http_{response.status_code}")
            raw = bytearray()
            if response.headers.get("content-encoding", "identity") != "identity":
                raise ValueError("metadata_content_encoding")
            async for chunk in response.aiter_raw(chunk_size=65_536):
                raw.extend(chunk)
                if len(raw) > MAX_METADATA:
                    raise ValueError("metadata_byte_cap")
    return bytes(raw).decode("utf-8")


async def capture(path, seconds=90):
    """One metadata GET, then one public socket; no proxy, credentials or retry."""
    from websockets.asyncio.client import connect

    from kaiba.providers.provider_policy import require_network

    if type(seconds) is not int or not 1 <= seconds <= MAX_SECONDS:
        raise ValueError("duration_bound")
    require_network(METADATA)
    require_network(URL)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(path.parent).free < 1024**3:
        raise ValueError("research_disk_reserve")
    frames, used, reason, error = 0, 0, "duration", None
    with path.open("xb") as output:

        def write(row, terminal=False):
            nonlocal used
            encoded = (json.dumps(row, allow_nan=False, separators=(",", ":")) + "\n").encode()
            if used + len(encoded) > MAX_BYTES - (0 if terminal else 4096):
                return False
            output.write(encoded)
            used += len(encoded)
            return True

        write(
            {
                "kind": "header",
                "schema": SCHEMA,
                "endpoint": URL,
                "metadata_url": METADATA,
                "symbol": "BTC",
                "requested_seconds": seconds,
                **clock(),
            }
        )
        try:
            raw = await asyncio.wait_for(get_metadata(), 12)
            m = market_metadata(json.loads(raw))
            if not write({"kind": "metadata", "payload": raw, **clock()}):
                raise ValueError("metadata_record_cap")
            channels = topics(m)
            async with connect(
                URL,
                proxy=None,
                open_timeout=10,
                close_timeout=2,
                ping_interval=20,
                ping_timeout=10,
                max_size=512 * 1024,
                max_queue=16,
                compression=None,
            ) as ws:
                deadline = time.monotonic() + seconds
                greeting = await asyncio.wait_for(ws.recv(), min(seconds, 5))
                if type(greeting) is not str or json.loads(greeting).get("type") != "connected":
                    raise ValueError("connected_greeting_required")
                if not write({"kind": "frame", "payload": greeting, **clock()}):
                    raise ValueError("greeting_record_cap")
                frames += 1
                for channel in channels:
                    await asyncio.wait_for(ws.send(json.dumps({"type": "subscribe", "channel": channel})), 3)
                while time.monotonic() < deadline:
                    try:
                        payload = await asyncio.wait_for(
                            ws.recv(), min(5, max(0.001, deadline - time.monotonic()))
                        )
                    except TimeoutError:
                        continue
                    if type(payload) is not str:
                        raise ValueError("nontext_public_frame")
                    if not write({"kind": "frame", "payload": payload, **clock()}):
                        reason = "byte_cap"
                        break
                    frames += 1
                    if json.loads(payload).get("type") == "ping":
                        await asyncio.wait_for(ws.send('{"type":"pong"}'), 3)
                    if frames >= MAX_FRAMES:
                        reason = "frame_cap"
                        break
        except Exception as exc:
            reason, error = "provider_error", type(exc).__name__
        footer = {"kind": "footer", "reason": reason, "error_class": error, "frames": frames, **clock()}
        if not write(footer, True):
            raise ValueError("footer_reserve_exhausted")
    return footer


def read_capture(path, *, with_sha=False):
    path = Path(path)
    with path.open("rb") as source:
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("capture_byte_bound")
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    if (
        len(rows) < 2
        or len(rows) > MAX_FRAMES + 3
        or rows[0].get("kind") != "header"
        or rows[-1].get("kind") != "footer"
    ):
        raise ValueError("interrupted_or_oversized_capture")
    head, foot = rows[0], rows[-1]
    if foot.get("reason") not in ("duration", "byte_cap", "frame_cap", "provider_error"):
        raise ValueError("capture_terminal_reason")
    error = foot.get("error_class")
    if (foot["reason"] == "provider_error" and (type(error) is not str or not error.isidentifier())) or (
        foot["reason"] != "provider_error" and error is not None
    ):
        raise ValueError("capture_terminal_error")
    if (
        head.get("schema") != SCHEMA
        or head.get("endpoint") != URL
        or head.get("metadata_url") != METADATA
        or head.get("symbol") != "BTC"
        or type(head.get("requested_seconds")) is not int
        or not 1 <= head["requested_seconds"] <= MAX_SECONDS
    ):
        raise ValueError("capture_provenance")
    previous_wall, previous_mono = -1, -1
    for row in rows:
        wall, mono = integer(row["received_ms"]), integer(row["mono_ns"])
        # Windows clocks can share a tick; file order preserves receive order.
        if wall < previous_wall or mono < previous_mono:
            raise ValueError("capture_clock_order")
        elapsed_wall = wall - head["received_ms"]
        elapsed_mono = (mono - head["mono_ns"]) / 1e6
        if abs(elapsed_wall - elapsed_mono) > 100:
            raise ValueError("capture_clock_jump")
        previous_wall, previous_mono = wall, mono
    if (foot["mono_ns"] - head["mono_ns"]) / 1e9 > head["requested_seconds"] + 35:
        raise ValueError("capture_duration_bound")
    frames = [row for row in rows[1:-1] if row.get("kind") == "frame"]
    metadata = [row for row in rows[1:-1] if row.get("kind") == "metadata"]
    if (
        len(metadata) > 1
        or type(foot.get("frames")) is not int
        or len(frames) != foot["frames"]
        or len(frames) + len(metadata) != len(rows) - 2
        or (frames and (len(metadata) != 1 or rows[1].get("kind") != "metadata"))
    ):
        raise ValueError("capture_layout")
    if not metadata and foot["reason"] != "provider_error":
        raise ValueError("capture_missing_metadata")
    for row in metadata + frames:
        bound = MAX_METADATA if row["kind"] == "metadata" else 512 * 1024
        if type(row.get("payload")) is not str or len(row["payload"].encode()) > bound:
            raise ValueError("capture_payload_bound")
    return (rows, hashlib.sha256(raw).hexdigest()) if with_sha else rows


class Book:
    """Snapshot then nonce-linked absolute-size deltas; offset is not a sequence."""

    def __init__(self):
        self.asks, self.bids = {}, {}
        self.nonce, self.synced = None, False
        self.gaps, self.observations = 0, 0
        self.spreads = []

    def update(self, message, snapshot):
        book = message["order_book"]
        if type(book.get("code")) is not int or book["code"] != 0:
            raise ValueError("book_status")
        nonce = integer(book["nonce"])
        if snapshot:
            self.asks, self.bids = {}, {}
            self.synced = True
        elif not self.synced or integer(book["begin_nonce"]) != self.nonce or nonce < self.nonce:
            self.gaps += 1
            self.synced = False
            return
        for name in ("asks", "bids"):
            for level in book[name]:
                price, size = decimal(level["price"]), decimal(level["size"], zero=True)
                levels = getattr(self, name)
                if size == 0:
                    levels.pop(price, None)
                else:
                    levels[price] = size
                if len(levels) > 10_000:
                    raise ValueError("book_level_bound")
        self.nonce = nonce
        if self.asks and self.bids:
            ask, bid = min(self.asks), max(self.bids)
            if bid > ask:
                raise ValueError("crossed_book")
            self.observations += 1
            self.spreads.append(float((ask - bid) / ((ask + bid) / 2) * 10_000))


def summarize(rows):
    head, foot = rows[0], rows[-1]
    metadata_rows = [r for r in rows if r["kind"] == "metadata"]
    result = {
        "schema": SCHEMA,
        "mode": "public_tape_quality_only",
        "reason": foot["reason"],
        "execution_allowed": False,
        "promotable": False,
        "profitability_proven": False,
        "exchange_trade_completeness_proven": False,
        "directional_liquidation_qualified": False,
        "funding_complete": False,
        "funding_price_basis": "index",
        "quote_currency_symbol": None,
        "funding_rate_unit": "UNVERIFIED",
        "normalized_funding_rate": None,
        "trade_timestamp_basis": "block_open_ms",
        "transaction_time_basis": "execution_us_optional",
        "client_latency_inferred": False,
        "long_liquidation_notional": None,
        "short_liquidation_notional": None,
        "raw_frames": foot["frames"],
        "counts": {},
        "usable_for_economic_replay": False,
    }
    if not metadata_rows:
        result["status"] = "NO_VALIDATED_MARKET"
        return result
    market = market_metadata(json.loads(metadata_rows[0]["payload"]))
    identity = market["market_id"]
    counts, acks, seen, closed = Counter(), set(), {}, set()
    book, stats_count = Book(), 0
    for row in rows:
        if row["kind"] != "frame":
            continue
        message = json.loads(row["payload"])
        typ = message.get("type", "")
        if "error" in message:
            counts["provider_error_messages"] += 1
            continue
        if typ in ("connected", "ping", "pong"):
            counts[typ] += 1
            continue
        channel = message.get("channel", "")
        parts = channel.split(":")
        if len(parts) < 2 or parts[1] != str(identity) or parts[0] not in KINDS:
            raise ValueError("unexpected_public_channel")
        kind = parts[0]
        if typ not in (f"subscribed/{kind}", f"update/{kind}"):
            raise ValueError("unexpected_message_type")
        if kind == "candle" and parts != ["candle", str(identity), "1m"]:
            raise ValueError("unexpected_candle_resolution")
        if kind != "candle" and len(parts) != 2:
            raise ValueError("unexpected_channel_shape")
        snapshot = typ.startswith("subscribed/")
        if snapshot:
            acks.add(kind)
        elif kind not in acks:
            raise ValueError("update_before_subscription")
        counts[kind + "_frames"] += 1
        if kind == "order_book":
            book.update(message, snapshot)
        elif kind == "trade":
            for array in ("trades", "liquidation_trades"):
                for trade in message.get(array, []):
                    if integer(trade["market_id"]) != identity or type(trade["is_maker_ask"]) is not bool:
                        raise ValueError("trade_identity")
                    event = trade["type"]
                    if event not in ("trade", "liquidation", "deleverage", "market-settlement"):
                        raise ValueError("unknown_trade_category")
                    if array == "liquidation_trades" and event != "liquidation":
                        raise ValueError("liquidation_array_category")
                    key = str(integer(trade["trade_id"]))
                    if "trade_id_str" in trade and trade["trade_id_str"] != key:
                        raise ValueError("trade_id_precision")
                    integer(trade["timestamp"])
                    integer(trade.get("transaction_time", 0))
                    for field in ("size", "price", "usd_amount"):
                        decimal(trade[field], zero=event in ("deleverage", "market-settlement"))
                    signature = tuple(
                        str(trade.get(k))
                        for k in (
                            "type",
                            "timestamp",
                            "transaction_time",
                            "size",
                            "price",
                            "usd_amount",
                            "is_maker_ask",
                        )
                    )
                    if key in seen:
                        if seen[key] != signature:
                            raise ValueError("conflicting_trade_identity")
                        counts["duplicate_trade_id"] += 1
                    else:
                        seen[key] = signature
                        counts["received_" + event] += 1
                        counts[("snapshot_" if snapshot else "update_delivered_") + event] += 1
        elif kind == "market_stats":
            stats = message["market_stats"]
            if str(identity) in stats:
                stats = stats[str(identity)]
            if integer(stats.get("market_id", identity)) != identity or stats.get("symbol") != "BTC":
                raise ValueError("stats_identity")
            decimal(stats["index_price"])
            decimal(stats["mark_price"])
            stats_count += 1
        else:
            candles = message["candles"]
            if len(candles) > 2:
                raise ValueError("candle_count")
            for candle in candles:
                if integer(candle["t"]) % 60_000:
                    raise ValueError("candle_open_clock")
            if not snapshot and len(candles) == 2:
                old, new = [integer(c["t"]) for c in candles]
                if old % 60_000 or new != old + 60_000:
                    raise ValueError("candle_rollover_clock")
                closed.add(old)
    result.update(
        {
            "status": "OBSERVED_UNQUALIFIED"
            if foot["reason"] == "duration"
            and acks == set(KINDS)
            and counts["connected"]
            and not counts["provider_error_messages"]
            else "FAILED_OR_CENSORED",
            "market_id": identity,
            "quote_asset_id": market["quote_asset_id"],
            "symbol": "BTC",
            "counts": dict(counts),
            "subscription_acks": sorted(acks),
            "book_unlinked_updates": book.gaps,
            "book_synced_at_end": book.synced,
            "valid_book_observations": book.observations,
            "mark_index_observations": stats_count,
            "min_observed_spread_bps": min(book.spreads, default=None),
            "max_observed_spread_bps": max(book.spreads, default=None),
            "rollover_closed_candles": len(closed),
            "receive_duration_s": (foot["mono_ns"] - head["mono_ns"]) / 1e9,
        }
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    live = sub.add_parser("capture")
    live.add_argument("--output", type=Path, required=True)
    live.add_argument("--seconds", type=int, default=90)
    quality = sub.add_parser("summarize")
    quality.add_argument("--input", type=Path, required=True)
    quality.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "capture":
        footer = asyncio.run(capture(args.output, args.seconds))
        print(json.dumps(footer))
        return 0 if footer["reason"] == "duration" else 2
    rows, digest = read_capture(args.input, with_sha=True)
    report = summarize(rows)
    report["capture_sha256"] = digest
    with args.output.open("x", encoding="utf-8") as dest:
        json.dump(report, dest, indent=2, allow_nan=False)
    print(json.dumps(report))
    return 0 if report["status"] == "OBSERVED_UNQUALIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
