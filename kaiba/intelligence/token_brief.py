"""Receipt-aware token research formatting, with no database, provider or action calls.

Sections accept ``{facts: {...}, receipt: {...}}`` (flat facts are also accepted).
Buyer facts use ``rows``, ``ordering``, ``coverage`` and optional ``total_count``.
This reports evidence and source claims; it does not score an entry or recommend a buy.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

SECTIONS = (
    "market", "security", "holder_composition", "buyers", "early_buyers",
    "developer", "social", "calls", "coverage",
)
MAX_AGE_MS = {
    "market": 120_000, "security": 900_000, "holder_composition": 900_000,
    "buyers": 900_000, "early_buyers": 900_000, "developer": 86_400_000,
    "social": 3_600_000, "calls": 3_600_000, "coverage": 86_400_000,
}
CHAINS = frozenset({"sol", "bsc", "eth", "base", "robinhood", "arc", "stable"})
DENOMINATORS = frozenset({"total_supply", "circulating_supply", "tradable_float", "free_float"})
_MARKDOWN = re.compile(r"([\\`*_{}\[\]()<>#!|~])")
_CONTROLS = re.compile(r"[\x00-\x1f\x7f]")
_NUMERIC_SUFFIXES = ("_usd", "_pct", "_bps", "_ratio", "_count", "_ms", "_score")
_NUMERIC_KEYS = frozenset({"price", "supply", "total_supply", "circulating_supply", "tradable_float",
                          "holder_count", "count", "total_count", "value", "weighted_score", "score"})


def _warn(warnings: list[dict[str, str]], path: str, reason: str) -> None:
    item = {"field": path, "reason": reason}
    if item not in warnings:
        warnings.append(item)


def _number(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not parsed.is_finite():
        return None
    if parsed == parsed.to_integral_value():
        return int(parsed)
    result = float(parsed)
    return result if math.isfinite(result) else None


def _timestamp(value: Any) -> int | None:
    parsed = _number(value)
    return parsed if isinstance(parsed, int) and 0 <= parsed <= 253_402_300_799_999 else None


def _text(value: Any) -> str:
    return _CONTROLS.sub(" ", str(value))[:4000]


def _escape(value: Any) -> str:
    return _MARKDOWN.sub(r"\\\1", _text(value))


def safe_url(value: Any) -> str | None:
    """Allow absolute HTTP(S) links, without credentials or Markdown delimiters."""
    if not isinstance(value, str) or len(value) > 2048 or _CONTROLS.search(value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        if any(c.isspace() for c in parts.netloc) or any(c in parts.netloc for c in '<>"\\'):
            return None
        _ = parts.port  # Reject malformed ports.
        return urlunsplit((parts.scheme.lower(), parts.netloc,
                          quote(parts.path, safe="/%:@-._~!$&'+,;="),
                          quote(parts.query, safe="%=&/:@-._~!$'+,;?"),
                          quote(parts.fragment, safe="%-._~")))
    except (ValueError, TypeError):
        return None


def _ratio(value: dict[str, Any], path: str, warnings: list[dict[str, str]]) -> dict[str, Any]:
    out = dict(value)
    unit = out.get("unit")
    denominator = out.get("denominator")
    number = _number(out.get("value"))
    if unit not in {"percent", "fraction", "bps"}:
        _warn(warnings, path, "ratio_unit_unknown")
        number = None
    elif number is not None and not 0 <= number <= {"percent": 100, "fraction": 1, "bps": 10_000}[unit]:
        _warn(warnings, path, "ratio_out_of_range")
        number = None
    elif out.get("value") is not None and number is None:
        _warn(warnings, path, "invalid_number")
    if denominator not in DENOMINATORS:
        _warn(warnings, path, "ratio_denominator_unknown")
        out["denominator"] = None
    out["value"] = number
    return out


def _clean(value: Any, path: str, warnings: list[dict[str, str]]) -> Any:
    key = path.rsplit(".", 1)[-1]
    if isinstance(value, dict):
        out = {str(k): _clean(v, f"{path}.{k}", warnings) for k, v in value.items()}
        if "value" in out and ("unit" in out or "denominator" in out):
            return _ratio(out, path, warnings)
        return out
    if isinstance(value, (list, tuple)):
        if len(value) > 200:
            _warn(warnings, path, "list_capped_at_200_partial_coverage")
        return [_clean(v, f"{path}[{i}]", warnings) for i, v in enumerate(value[:200])]
    if value is None:
        return value
    if isinstance(value, bool):
        if key in _NUMERIC_KEYS or key.endswith(_NUMERIC_SUFFIXES):
            _warn(warnings, path, "invalid_number")
            return None
        return value
    if key in _NUMERIC_KEYS or key.endswith(_NUMERIC_SUFFIXES) or isinstance(value, (float, Decimal)):
        parsed = _number(value)
        if parsed is None:
            _warn(warnings, path, "invalid_number")
        return parsed
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if key in {"url", "website", "link", "explorer_url", "profile_url"} or key.endswith("_url"):
            cleaned = safe_url(value)
            if cleaned is None:
                _warn(warnings, path, "unsafe_or_invalid_link")
            return cleaned
        return _text(value)
    _warn(warnings, path, "unsupported_value")
    return None


def _receipt(raw: Any, section: str, as_of_ms: int, warnings: list[dict[str, str]],
             *, path: str | None = None) -> dict[str, Any]:
    value = raw if isinstance(raw, dict) else {}
    prefix = path or section
    observed = _timestamp(value.get("observed_at_ms"))
    max_age = _number(value.get("max_age_ms", MAX_AGE_MS[section]))
    if max_age is None or max_age < 0:
        max_age = MAX_AGE_MS[section]
        _warn(warnings, f"{prefix}.receipt.max_age_ms", "invalid_freshness_budget")
    basis = _text(value.get("basis") or "UNKNOWN")
    status = "fresh"
    age = as_of_ms - observed if observed is not None else None
    if observed is None:
        status = "undated"
        _warn(warnings, f"{prefix}.receipt.observed_at_ms", "observation_time_unknown")
    elif age is not None and age < 0:
        status = "future"
        _warn(warnings, f"{prefix}.receipt.observed_at_ms", "future_observation_excluded")
    elif age is not None and age > max_age:
        status = "stale"
        _warn(warnings, f"{prefix}.receipt.observed_at_ms", "stale_evidence_retained_as_stale")
    if basis.upper() in {"UNAVAILABLE", "UNKNOWN"}:
        _warn(warnings, f"{prefix}.receipt.basis", "evidence_basis_unknown_or_unavailable")
    return {"basis": basis, "observed_at_ms": observed,
            "provider": _text(value["provider"]) if value.get("provider") is not None else None,
            "endpoint": _text(value["endpoint"]) if value.get("endpoint") is not None else None,
            "status": status, "age_ms": age, "max_age_ms": max_age}


def _key(chain: str, address: str) -> tuple[str, str]:
    return chain, address if chain == "sol" else address.lower()


def _history(raw: Any, chain: str, name: str, as_of_ms: int, path: str,
             warnings: list[dict[str, str]], *, period: str | None = None) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        _warn(warnings, path, "wallet_history_invalid")
        return None
    if raw.get("chain") != chain:
        _warn(warnings, path, "chain_mismatched_history_excluded")
        return None
    if period is not None and raw.get("period") != period:
        _warn(warnings, path, "history_period_mismatch_excluded")
        return None
    history = dict(raw)
    history["receipt"] = _receipt(raw.get("receipt"), name, as_of_ms, warnings, path=path)
    return None if history["receipt"]["status"] == "future" else history


def _observations(raw: Any, chain: str, name: str, as_of_ms: int, path: str,
                  warnings: list[dict[str, str]]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        _warn(warnings, path, "supplemental_observations_invalid")
        return []
    observations = []
    for index, value in enumerate(raw):
        field = f"{path}[{index}]"
        if not isinstance(value, dict) or not isinstance(value.get("facts"), dict):
            _warn(warnings, field, "supplemental_observation_invalid")
            continue
        if value.get("chain") is not None and value["chain"] != chain:
            _warn(warnings, field, "chain_mismatched_observation_excluded")
            continue
        observation = dict(value)
        observation["receipt"] = _receipt(value.get("receipt"), name, as_of_ms, warnings, path=field)
        if observation["receipt"]["status"] != "future":
            observations.append(observation)
    return observations


def _buyer_context(facts: dict[str, Any], chain: str, name: str, as_of_ms: int,
                   seen: set[tuple[str, str]], warnings: list[dict[str, str]]) -> None:
    excluded = facts.get("excluded_protocol_buyers")
    if excluded is not None:
        valid_protocols = []
        for index, raw in enumerate(excluded if isinstance(excluded, list) else []):
            path = f"{name}.excluded_protocol_buyers[{index}]"
            if not isinstance(raw, dict) or not isinstance(raw.get("address"), str) or not raw["address"]:
                _warn(warnings, path, "excluded_protocol_identity_invalid")
                continue
            if raw.get("chain", chain) != chain:
                _warn(warnings, path, "chain_mismatched_protocol_context_excluded")
                continue
            item = dict(raw)
            item["receipt"] = _receipt(raw.get("receipt"), name, as_of_ms, warnings, path=path)
            if "trade_receipt" in raw:
                item["trade_receipt"] = _receipt(raw["trade_receipt"], name, as_of_ms,
                                                  warnings, path=f"{path}.trade")
                if item["trade_receipt"]["status"] == "future":
                    for key in ("token_realized_usd", "token_unrealized_usd", "buy_usd", "holding_share"):
                        item[key] = None
            if item["receipt"]["status"] != "future":
                valid_protocols.append(item)
        facts["excluded_protocol_buyers"] = valid_protocols

    raw_crosschecks = facts.get("wallet_crosschecks")
    markers = facts.get("wallet_crosschecks_not_top_trader_rows")
    if raw_crosschecks is None and markers is None:
        return
    crosschecks = raw_crosschecks if isinstance(raw_crosschecks, dict) else {}
    markers = markers if isinstance(markers, dict) else {}
    validated: dict[str, list[dict[str, Any]]] = {}
    outside: dict[tuple[str, str], dict[str, Any]] = {}
    for period in ("7d", "30d"):
        histories = {}
        values = crosschecks.get(period)
        validated[period] = []
        for index, raw in enumerate(values if isinstance(values, list) else []):
            path = f"{name}.wallet_crosschecks.{period}[{index}]"
            if not isinstance(raw, dict) or not isinstance(raw.get("address"), str) or not raw["address"]:
                _warn(warnings, path, "wallet_crosscheck_identity_invalid")
                continue
            history = _history(raw, chain, name, as_of_ms, path, warnings, period=period)
            if history is not None:
                key = _key(chain, history["address"])
                if key in histories:
                    _warn(warnings, path, "duplicate_wallet_crosscheck_excluded")
                    continue
                histories[key] = history
                validated[period].append(history)
        addresses = markers.get(period)
        for address in addresses if isinstance(addresses, list) else []:
            if not isinstance(address, str) or not address:
                continue
            key = _key(chain, address)
            if key in seen:
                _warn(warnings, f"{name}.wallet_crosschecks_not_top_trader_rows.{period}",
                      "outside_page_marker_conflicts_with_trader_rows")
                continue
            row = outside.setdefault(key, {"chain": chain, "address": address,
                "token_realized_usd": None, "token_unrealized_usd": None, "buy_usd": None,
                "wallet_histories": {"7d": None, "30d": None}})
            row["wallet_histories"][period] = histories.get(key)
    facts["wallet_crosschecks"] = validated
    for key, row in outside.items():
        for period in ("7d", "30d"):
            row["wallet_histories"][period] = next(
                (history for history in validated[period] if _key(chain, history["address"]) == key), None)
        for history in row["wallet_histories"].values():
            if not isinstance(history, dict):
                continue
            if isinstance(history.get("identity"), dict):
                row.setdefault("identity", history["identity"])
            if "supplemental_observations" in history and "supplemental_observations" not in row:
                row["supplemental_observations"] = _observations(
                    history["supplemental_observations"], chain, name, as_of_ms,
                    f"{name}.wallet_only_crosschecks.{row['address']}.supplemental_observations", warnings)
    facts["wallet_only_crosschecks"] = list(outside.values())


def _buyer_rows(section: dict[str, Any], name: str, chain: str, as_of_ms: int,
                warnings: list[dict[str, str]]) -> None:
    facts = section["facts"]
    raw_rows = facts.get("rows")
    rows_provided = isinstance(raw_rows, list)
    if not isinstance(raw_rows, list):
        _warn(warnings, f"{name}.rows", "buyer_rows_unknown" if raw_rows is None else "buyer_rows_invalid")
        raw_rows = []
    seen: set[tuple[str, str]] = set()
    rows = []
    for index, value in enumerate(raw_rows):
        path = f"{name}.rows[{index}]"
        if not isinstance(value, dict):
            _warn(warnings, path, "buyer_row_invalid")
            continue
        row = dict(value)
        row_chain = row.get("chain")
        address = row.get("address")
        if row_chain != chain or not isinstance(address, str) or not address:
            _warn(warnings, path, "buyer_chain_or_identity_invalid")
            continue
        identity = _key(chain, address)
        if identity in seen:
            _warn(warnings, path, "duplicate_buyer_excluded")
            continue
        seen.add(identity)
        legacy = _history(row.get("wallet_history"), chain, name, as_of_ms,
                          f"{path}.wallet_history", warnings)
        row["wallet_history"] = legacy
        raw_histories = row.get("wallet_histories")
        histories: dict[str, Any] = {}
        if raw_histories is not None and not isinstance(raw_histories, dict):
            _warn(warnings, f"{path}.wallet_histories", "wallet_histories_invalid")
        elif isinstance(raw_histories, dict):
            for period, raw_history in raw_histories.items():
                if period not in {"7d", "30d"}:
                    _warn(warnings, f"{path}.wallet_histories.{period}", "history_window_unsupported")
                    continue
                histories[period] = _history(raw_history, chain, name, as_of_ms,
                                             f"{path}.wallet_histories.{period}", warnings, period=period)
        if legacy is not None:
            period = legacy.get("period")
            if period not in histories:
                if period in {"7d", "30d"}:
                    histories[period] = legacy
            elif histories[period] != legacy:
                # Plural windows are authoritative when provided, including a rejected window.
                _warn(warnings, f"{path}.wallet_history", "legacy_history_conflicts_with_window")
                row["wallet_history"] = histories[period]
        row["wallet_histories"] = histories
        if "supplemental_observations" in row:
            row["supplemental_observations"] = _observations(
                row["supplemental_observations"], chain, name, as_of_ms,
                f"{path}.supplemental_observations", warnings)
        row["receipt"] = _receipt(row.get("receipt", section["receipt"]), name, as_of_ms, warnings, path=path)
        if row["receipt"]["status"] == "future":
            continue
        rows.append(row)
    facts["rows"] = rows
    _buyer_context(facts, chain, name, as_of_ms, seen, warnings)
    facts["sample_wallet_count"] = len(rows) if rows_provided else None
    facts["reported_row_count"] = len(raw_rows) if rows_provided else None
    facts["excluded_row_count"] = len(raw_rows) - len(rows) if rows_provided else None
    # An entity count is complete only when every row supplies an entity mapping.
    entities = [r.get("entity_id") for r in rows]
    facts["mapped_entity_count"] = len({str(e) for e in entities if e is not None}) if rows_provided else None
    facts["independent_entity_count"] = None
    facts["entity_mapping_complete"] = bool(rows) and all(e is not None for e in entities)
    # Even a complete mapping is a clustering observation, not proof of independent bets.
    if rows:
        _warn(warnings, name, "wallet_count_is_not_independent_entity_count")
    ordering = facts.get("ordering")
    expected = "current_top_traders" if name == "buyers" else "first_20_chronological"
    facts["chronological_first_buyers_verified"] = name == "early_buyers" and ordering == expected
    if ordering is None:
        _warn(warnings, f"{name}.ordering", "buyer_ordering_unknown")
    elif name == "early_buyers" and ordering != expected:
        _warn(warnings, f"{name}.ordering", "early_buyer_ordering_not_verified")
    if facts.get("coverage") != "complete":
        _warn(warnings, f"{name}.coverage", "partial_or_unknown_buyer_coverage")
    total = facts.get("total_count")
    if total is not None and (_number(total) is None or total < len(rows)):
        facts["total_count"] = None
        _warn(warnings, f"{name}.total_count", "invalid_population_count")


def build_brief(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-safe evidence brief without reading files or calling other modules."""
    if not isinstance(payload, dict):
        raise ValueError("token brief input must be an object")
    chain = payload.get("chain")
    address = payload.get("address")
    as_of_ms = _timestamp(payload.get("as_of_ms"))
    if not isinstance(chain, str) or chain not in CHAINS or not isinstance(address, str) or not address.strip():
        raise ValueError("explicit supported chain and token address are required")
    if as_of_ms is None:
        raise ValueError("an explicit finite as_of_ms timestamp is required")
    warnings: list[dict[str, str]] = []
    sources = payload.get("sections", payload)
    if not isinstance(sources, dict):
        raise ValueError("sections must be an object")
    sections = {}
    for name in SECTIONS:
        value = sources.get(name)
        raw = value if isinstance(value, dict) else {}
        if isinstance(value, list):
            raw = {"facts": {"rows": value}}
        if value is None:
            _warn(warnings, name, "section_missing")
        facts = raw.get("facts", {k: v for k, v in raw.items() if k != "receipt"})
        if isinstance(facts, list):
            facts = {"rows": facts}
        if not isinstance(facts, dict):
            _warn(warnings, name, "facts_invalid")
            facts = {}
        receipt = _receipt(raw.get("receipt"), name, as_of_ms, warnings)
        cleaned = _clean(facts, name, warnings)
        supplemental = cleaned.pop("supplemental_observations", None)
        sections[name] = {"facts": cleaned, "receipt": receipt}
        if receipt["status"] == "future":
            sections[name]["facts"] = {}
        if supplemental is not None:
            sections[name]["facts"]["supplemental_observations"] = _observations(
                supplemental, chain, name, as_of_ms, f"{name}.supplemental_observations", warnings)
        if name in {"buyers", "early_buyers"}:
            _buyer_rows(sections[name], name, chain, as_of_ms, warnings)
    # Scalar concentration percentages may be quoted, but their denominator is not inferred.
    for key, value in sections["holder_composition"]["facts"].items():
        if value is not None and not isinstance(value, dict) and key.endswith(("_pct", "_ratio")):
            _warn(warnings, f"holder_composition.{key}", "ratio_denominator_unknown")
    return {"schema_version": 1, "kind": "token_evidence_brief", "chain": chain,
            "address": _text(address), "as_of_ms": as_of_ms, "sections": sections,
            "warnings": warnings, "automatic_trade_verdict": None}


def _display(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict):
        if "value" in value and ("unit" in value or "denominator" in value):
            number = value.get("value")
            unit = value.get("unit")
            if number is None:
                measured = "unknown"
            elif unit == "fraction":
                measured = f"{number * 100:g}%"
            elif unit == "percent":
                measured = f"{number:g}%"
            else:
                measured = f"{number:g} bps"
            denominator = value.get("denominator") or "denominator unknown"
            return f"{measured} of {_escape(denominator)}"
        if len(value) > 8 or any(isinstance(v, (dict, list)) for v in value.values()):
            return f"{len(value)} fields (details in structured JSON)"
        return "; ".join(f"{_escape(k)}: {_display(v)}" for k, v in value.items())
    if isinstance(value, list):
        if any(isinstance(v, (dict, list)) for v in value):
            return f"{len(value)} source sample rows (details in structured JSON)"
        if not value:
            return "empty reported list"
        displayed = ", ".join(_display(v) for v in value[:8])
        return displayed if len(value) <= 8 else displayed + f"; {len(value)} entries, details in structured JSON"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:.6g}"
    return _escape(value)


def _money(value: Any) -> str:
    if value is None:
        return "unknown"
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return _display(value)
    magnitude = abs(value)
    prefix = "-$" if value < 0 else "$"
    if magnitude == 0:
        return "$0"
    if magnitude < .01:
        return f"{prefix}{magnitude:.4g}"
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if magnitude >= threshold:
            return f"{prefix}{magnitude / threshold:,.2f}{suffix}"
    return f"{prefix}{magnitude:,.2f}"


def _source(receipt: dict[str, Any]) -> str:
    observed = receipt.get("observed_at_ms")
    stamp = (datetime.fromtimestamp(observed / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
             if observed is not None else "time unknown")
    return (f"Source: {_escape(receipt.get('provider') or 'unknown')} / "
            f"{_escape(receipt.get('endpoint') or 'unknown')} · "
            f"{_escape(receipt.get('basis') or 'UNKNOWN')} · "
            f"{_escape(receipt.get('status') or 'unknown')} · {stamp}")


def _fact_lines(section: dict[str, Any]) -> list[str]:
    facts = section["facts"]
    lines = []
    for key, value in facts.items():
        if key in {"rows", "links", "supplemental_observations"}:
            continue
        label = _escape(key.replace("_", " "))
        if "score" in key or key in {"tags", "tier", "grade", "narrative", "description"}:
            label += " (source claim)"
        if key.endswith(("_pct", "_ratio")) and not isinstance(value, dict):
            label += " (denominator unknown)"
        if "count" in key and any(tag in key for tag in ("smart", "kol", "whale", "sniper", "bundler")):
            label += " (source tag count; independence unverified)"
        displayed = _money(value) if key.endswith("_usd") else _display(value)
        lines.append(f"- {label}: {displayed}")
    if not lines:
        lines = ["- Evidence unavailable or not collected."]
    lines.append(_source(section["receipt"]))
    lines.extend(_observation_lines(facts.get("supplemental_observations", [])))
    return lines


def _observation_lines(observations: list[dict[str, Any]]) -> list[str]:
    lines = []
    for observation in observations:
        label = observation.get("label") or "Supplemental observation"
        kind = observation.get("kind") or "observation"
        lines.append(f"- {_escape(label)} ({_escape(kind)}):")
        for field, value in observation["facts"].items():
            rendered = _money(value) if field.endswith("_usd") else _display(value)
            lines.append(f"  {_escape(field.replace('_', ' '))}: {rendered}")
        lines.append("  " + _source(observation["receipt"]))
    return lines


def _history_lines(history: dict[str, Any] | None, *, period: str) -> list[str]:
    if history is None:
        return [f"  Wallet history {period}: unavailable for this chain/window."]
    basis = history.get("roi_basis")
    basis_text = str(basis).lower() if basis is not None else ""
    if any(term in basis_text for term in ("derived", "computed", "inference")):
        roi_label = "derived ROI"
    elif any(term in basis_text for term in ("reported", "provider")):
        roi_label = "reported ROI"
    else:
        roi_label = "ROI (basis unclassified)" if basis is not None else "ROI (basis unknown)"
    roi = history.get("roi_pct")
    roi_value = "unknown" if roi is None else f"{_display(roi)}%"
    return [f"  Wallet history {_escape(period)} on {_display(history.get('chain'))}: "
            f"realized {_money(history.get('realized_usd'))}; "
            f"current open PnL {_money(history.get('unrealized_usd'))} (snapshot, not period flow); "
            f"{roi_label} {roi_value}; ROI basis {_display(basis)}.",
            "  " + _source(history["receipt"])]


def _wallet_lines(section: dict[str, Any], *, early: bool) -> list[str]:
    facts = section["facts"]
    rows = facts.get("rows", [])
    title = "Early buyer sample" if early else "Current top trader sample"
    count = _display(facts.get("sample_wallet_count"))
    lines = [f"{title}: {count} distinct observed wallets; "
             f"coverage {_escape(facts.get('coverage') or 'unknown')}; "
             f"ordering {_escape(facts.get('ordering') or 'unknown')}.",
             "Wallet counts do not establish independent entities."]
    if any(key in facts for key in ("limit", "provider_returned_count", "trader_page_cap_hit")):
        lines.append(f"Provider returned {_display(facts.get('provider_returned_count'))} rows; "
                     f"page limit {_display(facts.get('limit'))}; "
                     f"page cap hit {_display(facts.get('trader_page_cap_hit'))}. "
                     "This returned page is not the complete buyer population.")
    protocols = facts.get("excluded_protocol_buyers", [])
    if protocols:
        lines.append("Excluded protocol accounts (excluded from trader-wallet assessment):")
        for protocol in protocols[:20]:
            lines.append(f"- {_escape(protocol.get('label') or 'Protocol context')} · "
                         f"{_escape(protocol['address'])}; classification basis "
                         f"{_display(protocol.get('classification_basis'))}.")
            if "token_realized_usd" in protocol:
                lines.append(f"  Excluded reported token realized PnL: {_money(protocol['token_realized_usd'])}.")
                if "trade_receipt" in protocol:
                    lines.append("  Token observation " + _source(protocol["trade_receipt"]))
            lines.append("  " + _source(protocol["receipt"]))
    if early and not facts.get("chronological_first_buyers_verified"):
        lines.append("The first 20 chronological buyers have not been established.")
    researched = [row for row in rows[20:] if any(
        isinstance(history, dict) for history in row.get("wallet_histories", {}).values())
        or isinstance(row.get("wallet_history"), dict)]
    displayed_rows = rows[:20]
    for index, row in enumerate(displayed_rows + researched[:20]):
        if index == len(displayed_rows):
            lines.append("Researched buyers beyond displayed ranking:")
            lines.append("Targeted research within the returned trader page; these additional rows "
                         "are not presented as the top 20 or chronological early buyers.")
        lines.append(f"- {_escape(row['chain'])} {_escape(row['address'])}")
        lines.append(f"  Observed buys (cashflow): {_money(row.get('buy_usd'))}; "
                     f"realized token PnL (reported): {_money(row.get('token_realized_usd'))}; "
                     f"open token PnL (reported): {_money(row.get('token_unrealized_usd'))}.")
        if row.get("token_pnl_basis"):
            lines.append(f"  Token PnL basis: {_display(row['token_pnl_basis'])}.")
        if row.get("tags"):
            lines.append(f"  Provider tags (claims): {_display(row['tags'])}.")
        identity = row.get("identity")
        if isinstance(identity, dict) and identity.get("handle"):
            lines.append(f"  Identity claim: {_display(identity['handle'])}; "
                         f"basis {_display(identity.get('basis'))}.")
        histories = row.get("wallet_histories", {})
        if histories:
            for period in ("7d", "30d"):
                lines.extend(_history_lines(histories.get(period), period=period))
        elif isinstance(row.get("wallet_history"), dict):
            history = row["wallet_history"]
            lines.extend(_history_lines(history, period=history.get("period") or "unknown period"))
        else:
            lines.append("  Wallet history: unavailable for this chain.")
        lines.extend("  " + line for line in _observation_lines(row.get("supplemental_observations", [])))
        lines.append("  " + _source(row["receipt"]))
    if len(rows) > 20:
        lines.append(f"Ranking display covers 20 of {len(rows)} sample wallets; "
                     f"{min(len(researched), 20)} additional researched buyers shown separately. "
                     "The structured brief retains the sample.")
    outside = facts.get("wallet_only_crosschecks", [])
    if outside:
        lines.append("Wallet-only crosschecks outside this returned trader page:")
        lines.append("Token buy cashflow and token realized/open PnL are unknown for these wallets; "
                     "wallet-wide history does not fill that gap.")
        for row in outside[:20]:
            lines.append(f"- {_escape(row['chain'])} {_escape(row['address'])} · "
                         "token realized PnL unknown; token open PnL unknown.")
            identity = row.get("identity")
            if isinstance(identity, dict) and identity.get("handle"):
                lines.append(f"  Identity claim: {_display(identity['handle'])}; "
                             f"basis {_display(identity.get('basis'))}.")
            for period in ("7d", "30d"):
                lines.extend(_history_lines(row["wallet_histories"].get(period), period=period))
            lines.extend("  " + line for line in _observation_lines(row.get("supplemental_observations", [])))
        if len(outside) > 20:
            lines.append(f"Displaying 20 of {len(outside)} wallet-only crosschecks.")
    lines.append(_source(section["receipt"]))
    return lines


def render_brief(brief: dict[str, Any]) -> str:
    """Render a built brief as escaped Markdown; metadata is never executable syntax."""
    if brief.get("kind") != "token_evidence_brief":
        brief = build_brief(brief)
    sections = brief["sections"]
    stamp = datetime.fromtimestamp(brief["as_of_ms"] / 1000, UTC).isoformat()
    lines = [f"Token evidence brief · {_escape(brief['chain'])}",
             f"Contract: {_escape(brief['address'])}", f"As of: {stamp}", ""]
    market = sections["market"]["facts"]
    lines.extend(["Market snapshot: " + " · ".join(
        f"{label} {_money(market.get(key))}" for label, key in
        (("Price", "price_usd"), ("MC", "market_cap_usd"), ("Liquidity", "liquidity_usd"),
         ("24h volume", "volume_24h_usd"))), ""])
    for title, name in (("Market", "market"), ("Security", "security"),
                        ("Holder structure", "holder_composition")):
        lines.extend([f"### {title}", "", *_fact_lines(sections[name]), ""])
        if name == "security":
            lines.extend(["Token-tax observations are separate from total execution costs.", ""])
    lines.extend(["### Wallet behavior", "", *_wallet_lines(sections["buyers"], early=False), "",
                  *_wallet_lines(sections["early_buyers"], early=True), ""])
    for title, name in (("Developer and funding", "developer"), ("Narrative and social claims", "social"),
                        ("Timing and calls", "calls"), ("Reachability and coverage", "coverage")):
        lines.extend([f"### {title}", "", *_fact_lines(sections[name]), ""])
    links = []
    for name in SECTIONS:
        facts = sections[name]["facts"]
        for key, value in facts.items():
            if key in {"url", "website", "link", "explorer_url", "profile_url"} or key.endswith("_url"):
                link = safe_url(value)
                if link:
                    links.append(f"- [{_escape(key.replace('_', ' '))}]({link})")
    if links:
        lines.extend(["Links supplied by the evidence collector:", "", *dict.fromkeys(links), ""])
    lines.extend(["### Unknowns and limitations", ""])
    for warning in brief["warnings"]:
        lines.append(f"- {_escape(warning['field'])}: {_escape(warning['reason'].replace('_', ' '))}")
    if not brief["warnings"]:
        lines.append("- No input validation warnings; profitability and copyability remain unproven.")
    lines.append("")
    lines.append("Observed buy cashflow is not current position cost basis. Realized and open PnL are separate.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--input", type=Path, required=True, help="Normalized evidence JSON; no live reads")
    parser.add_argument("--json", action="store_true", help="Output the structured brief")
    args = parser.parse_args(argv)
    try:
        brief = build_brief(json.loads(args.input.read_text(encoding="utf-8-sig")))
        print(json.dumps(brief, ensure_ascii=False, indent=2, allow_nan=False) if args.json else render_brief(brief))
    except (OSError, ValueError, TypeError) as exc:
        print(f"Token brief input rejected: {_text(exc)}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
