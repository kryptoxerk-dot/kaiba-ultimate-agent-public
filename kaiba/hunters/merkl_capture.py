"""One bounded, read-only workstation reward capture. Not installed or scheduled.

No credentials, redirects, retries, DB writes or financial RPC. Production integration
must supply its existing limiter transport and retention; this is not a free cron API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from kaiba.hunters.merkl_entitlements import (
    MAX_BODY,
    MAX_REQUESTS,
    SCHEMA,
    EvidenceError,
    address,
    analyze,
    parse_json,
    replay,
)


def _write(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as out:
        json.dump(value, out, allow_nan=False, separators=(",", ":"))
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())


def public_read(request: dict) -> tuple[int, bytes]:
    """Called only with the closed request sequence constructed by analyze()."""
    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        headers = {"Accept": "application/json", "Accept-Encoding": "identity",
                   "User-Agent": "kaiba-earned-reward-research/1"}
        body = None
        if request["json"] is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(request["json"], separators=(",", ":")).encode()
        with client.stream(request["method"], request["url"], content=body, headers=headers) as response:
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise EvidenceError("encoded_response_refused")
            raw = bytearray()
            for piece in response.iter_raw():
                if len(raw) + len(piece) > MAX_BODY:
                    raise EvidenceError("response_size")
                raw.extend(piece)
            return response.status_code, bytes(raw)


def safe_summary(result: dict) -> dict:
    """No owner/token identities, endpoints or provider error messages in console/report."""
    keys = ("status", "requests", "reward_rows", "allocation_verified_count", "remaining_allocation_count",
            "observed_ms", "block_number", "block_timestamp_s", "coverage", "reason", "error_type")
    return {**{k: result[k] for k in keys if k in result}, "qualified": False,
            "funded_action_authorized": False, "net_profit_usd": None}


def capture(wallet: str, output: Path, *, transport: Callable = public_read,
            clock: Callable = lambda: int(time.time() * 1000), monotonic: Callable = time.monotonic,
            pause: Callable = time.sleep) -> dict:
    """Exclusive attempt, consumed before dispatch; never resume an interrupted attempt.

    Files are fsynced. Parent-directory crash durability is platform dependent. A caller
    must enforce a hard process deadline (150 s); HTTP timeout alone is not a total deadline.
    No global provider allowance is created here; this is one research invocation.
    """
    wallet = address(wallet)
    output.mkdir(exist_ok=False)
    start = monotonic()
    _write(output / "intent.json", {"schema": SCHEMA, "wallet": wallet, "started_ms": clock(),
                                   "max_requests": MAX_REQUESTS, "financial_execution": False})
    receipts = []
    calls = 0

    def exchange(request):
        nonlocal calls
        if calls >= MAX_REQUESTS or monotonic() - start > 110:
            raise EvidenceError("request_budget_or_deadline")
        if calls:
            pause(1.1)
        calls += 1
        started = clock()
        # Persist admission before network dispatch, including unsuccessful attempts.
        _write(output / f"request-{calls}.json", {"request": request, "started_ms": started})
        status, raw = transport(request)
        if not isinstance(raw, bytes) or len(raw) > MAX_BODY:
            raise EvidenceError("response_size")
        receipt = {"request": request, "started_ms": started, "received_ms": clock(),
                   "http_status": status, "body_hex": raw.hex(),
                   "body_sha256": hashlib.sha256(raw).hexdigest()}
        _write(output / f"receipt-{calls}.json", receipt)
        receipts.append(receipt)
        if type(status) is not int or status != 200:
            raise EvidenceError("http_unsuccessful")
        return parse_json(raw), receipt["received_ms"]

    try:
        analyze(wallet, exchange)
        bundle = {"schema": SCHEMA, "wallet": wallet, "receipts": receipts}
        _write(output / "bundle.json", bundle)
        result = replay(bundle, at_ms=clock())
        _write(output / "evidence.json", result)
        summary = safe_summary(result)
    except (EvidenceError, OSError, httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        summary = safe_summary({"status": "unavailable", "requests": calls,
                                "error_type": type(exc).__name__})
        if isinstance(exc, EvidenceError):
            summary["reason"] = str(exc)
    _write(output / "result.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wallet-file", type=Path, required=True, help="private text file containing one address")
    parser.add_argument("--output", type=Path, required=True, help="new private directory under repository data/")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2] / "data"
    if root.resolve() not in args.output.resolve().parents:
        parser.error("capture output must be under repository data/")
    try:
        with args.wallet_file.open(encoding="utf-8") as source:
            wallet = source.read(100).strip()
        result = capture(wallet, args.output)
    except (EvidenceError, OSError) as exc:
        parser.exit(2, f"capture refused: {type(exc).__name__}\n")
    print(json.dumps(result, sort_keys=True))
    return 2 if result["status"] == "unavailable" else 0


if __name__ == "__main__":
    raise SystemExit(main())
