"""Bounded in-memory tax string view around the unchanged paper quote adapter.

No query, provider, cache fallback, source clock or database write is added. Only
explicit receipt-backed integral Decimal strings in the exact dossier query can
change representation. Unsupported input is delegated unchanged; reader errors
always propagate. This module is not installed into any worker by importing it.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kaiba.learning import autolearn_io

DOSSIER_SQL = "SELECT built_at_ms,dossier_json FROM token_dossiers WHERE chain=? AND address=?"
MAX_DOSSIER_BYTES = 256 * 1024
MAX_OBJECT_FIELDS = 256
MAX_NUMBER_CHARS = 128
USABLE_BASES = frozenset({"verified_onchain", "provider_reported", "derived", "cached"})
_NUMBER = re.compile(r"[+-]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", re.ASCII)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len(pairs) > MAX_OBJECT_FIELDS:
        raise ValueError("tax quote object field cap")
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("tax quote duplicate JSON keys")
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("tax quote non-JSON numeric constant")


_DECODER = json.JSONDecoder(object_pairs_hook=_unique, parse_constant=_invalid_constant)


def _space(raw: str, offset: int) -> int:
    while offset < len(raw) and raw[offset] in " \t\r\n":
        offset += 1
    return offset


def _members(raw: str, offset: int) -> dict[str, tuple[int, int]]:
    """Find value-token spans in a previously validated bounded JSON object."""
    offset = _space(raw, offset)
    if raw[offset] != "{":
        raise ValueError("tax quote object required")
    offset, members = _space(raw, offset + 1), {}
    if raw[offset] == "}":
        return members
    for _ in range(MAX_OBJECT_FIELDS):
        key, end = _DECODER.raw_decode(raw, offset)
        if not isinstance(key, str):
            raise ValueError("tax quote string key required")
        offset = _space(raw, end)
        if raw[offset] != ":":
            raise ValueError("tax quote JSON colon required")
        start = _space(raw, offset + 1)
        _, end = _DECODER.raw_decode(raw, start)
        members[key] = (start, end)
        offset = _space(raw, end)
        if raw[offset] == "}":
            return members
        if raw[offset] != ",":
            raise ValueError("tax quote JSON comma required")
        offset = _space(raw, offset + 1)
    raise ValueError("tax quote object field cap")


def _tax_value(measure: Any) -> str | None:
    if not isinstance(measure, dict) or measure.get("basis") not in USABLE_BASES:
        return None
    receipt = measure.get("receipt")
    if not isinstance(receipt, dict) or receipt.get("basis") not in USABLE_BASES:
        return None
    observed = receipt.get("observed_at_ms")
    if type(observed) is not int or observed <= 0:
        return None
    value = measure.get("value")
    if not isinstance(value, str) or len(value) > MAX_NUMBER_CHARS or not _NUMBER.fullmatch(value):
        return None
    number = Decimal(value)
    if not number.is_finite() or not 0 <= number < 10_000 or number != number.to_integral_value():
        return None
    return str(int(number))


def _dossier_view(raw: Any) -> Any:
    """Splice only changed tax value strings; every other input byte survives."""
    if not isinstance(raw, str) or len(raw) > MAX_DOSSIER_BYTES:
        return raw
    try:
        if len(raw.encode("utf-8")) > MAX_DOSSIER_BYTES:
            return raw
        start = _space(raw, 0)
        dossier, end = _DECODER.raw_decode(raw, start)
        if not isinstance(dossier, dict) or _space(raw, end) != len(raw):
            return raw
        members = _members(raw, start)
        changes = []
        for field in ("buy_tax_bps", "sell_tax_bps"):
            value = _tax_value(dossier.get(field))
            if value is None or value == dossier[field]["value"]:
                continue
            measure_start, _ = members[field]
            value_start, value_end = _members(raw, measure_start)["value"]
            changes.append((value_start, value_end, json.dumps(value)))
        for start, end, replacement in sorted(changes, reverse=True):
            raw = raw[:start] + replacement + raw[end:]
        return raw
    except (ValueError, TypeError, KeyError, IndexError, InvalidOperation, OverflowError, RecursionError, UnicodeError):
        return raw


class TaxQuoteReader:
    """One-to-one delegation; only the requested dossier result gets a view."""

    def __init__(self, reader: Any, *, chain: str, token: str):
        self.reader, self.chain, self.token = reader, chain, token

    def rows(self, sql: str, args=()):
        eligible = (sql == DOSSIER_SQL and type(args) in (tuple, list)
                    and len(args) == 2 and tuple(args) == (self.chain, self.token))
        rows = self.reader.rows(sql, args)
        if not eligible or type(rows) is not list or len(rows) != 1 or type(rows[0]) is not dict:
            return rows
        before = rows[0].get("dossier_json")
        after = _dossier_view(before)
        if after is before:
            return rows
        return [{**rows[0], "dossier_json": after}]

    def q(self, *args, **kwargs):
        return self.reader.q(*args, **kwargs)

    def guard(self, *args, **kwargs):
        return self.reader.guard(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.reader, name)


def quote_evidence(reader: Any, chain: str, token: str, *, now_ms: int,
                   max_age_ms: int = 120_000, cache_root: Path | None = None) -> dict:
    """Same frozen quote call and freshness rules, with a tax-only reader view."""
    return autolearn_io.quote_evidence(TaxQuoteReader(reader, chain=chain, token=token), chain, token,
        now_ms=now_ms, max_age_ms=max_age_ms, cache_root=cache_root)


__all__ = ["TaxQuoteReader", "quote_evidence"]
