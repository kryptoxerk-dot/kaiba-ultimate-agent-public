"""Run Kaiba's wallet paper replay on bounded public Elysium index captures."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from kaiba.learning.evm_wallet_paper import MAX_BYTES, MAX_ROWS, Swap, paper

ADDRESS = re.compile(r"0x[0-9a-f]{40}")
HASH = re.compile(r"0x[0-9a-f]{64}")
HYPE_QUOTES = {"0x" + "0" * 40, "0xba876237b0f021a6c6d852d6c26e107997cc5e81"}
MAX_MARKETS = 32
MAX_PRINTS = 200


def _hex(value: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value.lower()):
        raise ValueError("invalid_hex_identity")
    return value.lower()


def _pool(value: str) -> str:
    return _hex(value, HASH if isinstance(value, str) and len(value) == 66 else ADDRESS)


def _units(value: object) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite() or not Decimal("1e-36") <= amount <= Decimal("1e36"):
        raise ValueError("invalid_swap_amount")
    return amount


def _eleos_quote(witness: dict, token: str) -> bool:
    currencies = {_hex(address, ADDRESS) for address in witness.get("currencies", [])}
    return len(currencies) == 2 and token in currencies and (currencies - {token}) <= HYPE_QUOTES


def _print(source: str, row: dict, token: str, pool: str) -> tuple:
    if source == "eleos":
        if (_hex(row["token"], ADDRESS), _pool(row["pool"])) != (token, pool):
            raise ValueError("eleos_print_identity_mismatch")
        if type(row["side"]) is not int or row["side"] not in (0, 1):
            raise ValueError("invalid_eleos_side")
        side = ("sell", "buy")[row["side"]]
        timestamp, tx, log_index = row["ts"], row["tx"], row["li"]
        if type(log_index) is not int or log_index < 0:
            raise ValueError("invalid_log_index")
        amount, quote = _units(row["amount"]), _units(row["hype"])
    else:
        side, timestamp, tx = row["side"], row["timestamp"], row["tx_hash"]
        log_index = None
        amount, quote = _units(row["amount_token"]), _units(row["amount_quote"])
    wallet = _hex(row["trader"], ADDRESS)
    if type(timestamp) is not int or timestamp <= 0 or side not in ("buy", "sell") or int(wallet, 16) == 0:
        raise ValueError("invalid_trade_print")
    return timestamp, _hex(tx, HASH), log_index, wallet, side, amount, quote


def normalize(markets: list[dict], launch_witnesses: list[dict], as_of_ms: int) -> tuple[list[Swap], dict]:
    """Bind token, pool and quote identity before combining print histories."""
    if len(markets) > MAX_MARKETS or len(launch_witnesses) > MAX_MARKETS:
        raise ValueError("market_cap_exceeded")
    if type(as_of_ms) is not int or as_of_ms <= 0:
        raise ValueError("invalid_replay_clock")
    witnesses = {_pool(row["pool"]): row for row in launch_witnesses}
    if len(witnesses) != len(launch_witnesses):
        raise ValueError("duplicate_launch_witness")
    seen_pools = set()
    swaps = []
    counts: Counter[str] = Counter()
    for market in markets:
        source = market["source"]
        if source not in ("eleos", "perpme"):
            raise ValueError("unsupported_elysium_source")
        token, pool = _hex(market["token"], ADDRESS), _pool(market["pool"])
        if pool in seen_pools:
            raise ValueError("duplicate_pool")
        seen_pools.add(pool)
        if "trades" not in market:
            counts["unavailable_tape"] += 1
            continue
        body = market["trades"]
        if source == "eleos":
            witness = witnesses.get(pool, {})
            if not _eleos_quote(witness, token):
                counts["unbound_quote"] += 1
                continue
            rows = body
        else:
            quote = body["quote_token"]
            if _hex(body["token"], ADDRESS) != token:
                raise ValueError("perpme_tape_identity_mismatch")
            if quote.get("native") is not True or quote.get("decimals") != 18 or (
                _hex(quote["address"], ADDRESS) not in HYPE_QUOTES
            ):
                counts["unbound_quote"] += 1
                continue
            rows = body["items"]
        if not isinstance(rows, list) or len(rows) > MAX_PRINTS:
            raise ValueError("print_cap_exceeded")
        counts[source] += 1
        counts["response_limit_reached"] += len(rows) == MAX_PRINTS

        # PerpMe supplies no log index. Identical records can be collapsed, but
        # different rows for the same tx and side cannot be safely disambiguated.
        events = {}
        for row in rows:
            timestamp, tx, index, wallet, side, amount, value = _print(source, row, token, pool)
            if timestamp * 1_000 > as_of_ms:
                raise ValueError("print_after_capture")
            key = (tx, index) if index is not None else (tx, wallet, side)
            event = (timestamp, tx, wallet, side, amount, value)
            if events.setdefault(key, event) != event:
                raise ValueError("conflicting_print_identity")
        trades = {}
        for timestamp, tx, wallet, side, amount, value in events.values():
            key = (tx, wallet, side)
            clock, quantity, proceeds = trades.get(key, (timestamp, Decimal(0), Decimal(0)))
            if clock != timestamp:
                raise ValueError("conflicting_trade_clock")
            trades[key] = clock, quantity + amount, proceeds + value
        swaps.extend(
            Swap(timestamp * 1_000, pool, token, wallet, tx, side, quote / amount)
            for (tx, wallet, side), (timestamp, amount, quote) in trades.items()
        )
    if len(swaps) > MAX_ROWS:
        raise ValueError("swap_cap_exceeded")
    return swaps, dict(counts)


def _jsonl(path: Path) -> list[dict]:
    with path.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("capture_file_cap")
    rows = [json.loads(line, parse_float=Decimal) for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("invalid_capture_row")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trades", type=Path)
    parser.add_argument("launch_witnesses", type=Path)
    parser.add_argument("--as-of-ms", type=int, required=True)
    parser.add_argument("--hold-minutes", type=int, choices=(5, 30), default=30)
    args = parser.parse_args()
    try:
        swaps, counts = normalize(_jsonl(args.trades), _jsonl(args.launch_witnesses), args.as_of_ms)
        report = paper(swaps, chain="elysium", as_of_ms=args.as_of_ms, hold_minutes=args.hold_minutes)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, InvalidOperation) as exc:
        parser.exit(2, f"Elysium public tape refused ({type(exc).__name__})\n")
    print(json.dumps({"source_basis": "public_index_prints", "coverage": counts, **report}, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
