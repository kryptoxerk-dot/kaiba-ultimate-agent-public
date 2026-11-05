"""Capture one DLMM position and its bins through two bounded keyless public RPC reads.

Workstation research CLI. Raw captures contain public position identities: keep in ignored
data/, never commit them. No credentials, paid endpoint override, retry, subscription or trade.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from kaiba.learning.dlmm_state import (
    DecodeError,
    bin_array_address,
    decode_pool,
    decode_position,
    public_key,
    snapshot,
)

RPC = "https://api.mainnet-beta.solana.com"
MAX_RESPONSE_BYTES = 1_000_000


def public_read(request: dict) -> dict:
    req = urllib.request.Request(RPC, data=json.dumps(request).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "kaiba-lp-research/1"})
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise DecodeError("rpc_response_oversized")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise DecodeError("rpc_response_invalid")
    return value


def _save(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def capture(pool: str, position: str, output: Path, *, transport: Callable[[dict], dict] = public_read,
            pause: Callable[[float], None] = time.sleep) -> dict:
    """Create a new immutable attempt directory; failure never erases the last good capture."""
    public_key(pool)
    public_key(position)
    if pool == position:
        raise DecodeError("pool_and_position_must_differ")
    # Never rerun an unknown attempt or overwrite a previous observation in this directory.
    output.mkdir(exist_ok=False)
    _save(output / "intent.json", {"schema": "kaiba-dlmm-attempt/1", "pool": pool, "position": position,
                                   "endpoint": RPC, "started_ms": int(time.time() * 1000), "max_requests": 2})
    calls = 0

    def read(keys: list[str], slot: int = 0) -> dict:
        nonlocal calls
        if calls >= 2 or len(keys) > 30:
            raise DecodeError("request_budget_exceeded")
        calls += 1
        request = {"jsonrpc": "2.0", "id": calls, "method": "getMultipleAccounts",
                   "params": [keys, {"encoding": "base64", "commitment": "finalized", "minContextSlot": slot}]}
        started = int(time.time() * 1000)
        _save(output / f"request-{calls}.json", {"started_ms": started, "request": request})
        response = transport(request)
        receipt = {"schema": "kaiba-dlmm-capture/1", "pool": pool, "position": position,
                   "request": request, "response": response, "started_ms": started,
                   "received_ms": int(time.time() * 1000)}
        _save(output / f"response-{calls}.json", receipt)
        return receipt

    try:
        first = read([pool, position])
        reply = first["response"]
        if reply.get("jsonrpc") != "2.0" or reply.get("id") != 1 or "error" in reply:
            raise DecodeError("rpc_discovery_failed")
        result = reply.get("result", {})
        values = result.get("value")
        slot = result.get("context", {}).get("slot")
        if not isinstance(values, list) or len(values) != 2 or type(slot) is not int or slot < 0:
            raise DecodeError("rpc_discovery_invalid")
        p, pos = decode_pool(values[0]), decode_position(values[1])
        if pos["pool"] != pool:
            raise DecodeError("position_pool_mismatch")
        indices = sorted(set(range(pos["lower_bin"] // 70, pos["upper_bin"] // 70 + 1)) |
                         set(range(p["active_bin"] // 70 - 1, p["active_bin"] // 70 + 2)))
        keys = [pool, position] + [bin_array_address(pool, i) for i in indices] + p["mints"]
        if len(keys) != len(set(keys)):
            raise DecodeError("duplicate_capture_key")
        pause(1.2)
        receipt = read(keys, slot)
        state = snapshot(receipt)
        raw = (output / "response-2.json").read_bytes()
        _save(output / "state.json", state)
        summary = {"status": "captured", "requests": calls, "slot": state["slot"],
                   "received_ms": state["received_ms"], "position_bins": len(state["position_bins"]),
                   "response_sha256": hashlib.sha256(raw).hexdigest(), "financial_execution": False,
                   "net_profit_available": False}
        _save(output / "result.json", summary)
        return summary
    except (DecodeError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        # Never print provider bodies or retry a throttle, timeout, malformed or missing account.
        failure = {"status": "unavailable", "requests": calls, "error_type": type(exc).__name__,
                   "http_status": exc.code if isinstance(exc, urllib.error.HTTPError) else None,
                   "financial_execution": False, "net_profit_available": False}
        if isinstance(exc, DecodeError):
            failure["reason"] = str(exc)
        _save(output / "result.json", failure)
        return failure


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", required=True)
    parser.add_argument("--position", required=True)
    parser.add_argument("--output", type=Path, required=True, help="new attempt directory under repository data/")
    args = parser.parse_args()
    data = Path(__file__).resolve().parents[2] / "data"
    output = args.output.resolve()
    if data.resolve() not in output.parents:
        parser.error("private captures must stay under repository data/")
    try:
        result = capture(args.pool, args.position, output)
    except (DecodeError, OSError) as exc:
        parser.exit(2, f"capture refused: {type(exc).__name__}\n")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "captured" else 2


if __name__ == "__main__":
    raise SystemExit(main())
