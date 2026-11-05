"""Retain original parsed GMGN observations inside the existing admission path.

Codex AGENT-GMGN-ORIGINAL-CAPTURE. The base collector owns eligibility, the
worker lock, permanent 100/day admissions and its one typed provider call. This
subclass only retains the returned data/full Receipt before the old verifier.
Capsules describe trusted local capture, not HTTP wire bytes or paid trade costs.
No CLI or service is changed merely by importing this module.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any

from kaiba.core.config import get_settings
from kaiba.core.schemas import digest
from kaiba.learning import prospective_quotes as base
from kaiba.learning.gmgn_capture_store import MAX_CAPSULE_BYTES, CaptureStore, CaptureStoreError

VERSION = "gmgn_observation/1"
_SHA = re.compile(r"[0-9a-f]{64}")
_RECEIPT = {"provider", "endpoint", "observed_at_ms", "basis", "request_digest", "response_digest", "note"}
_BASE_SOURCES = ("learning/strategy_campaign.py", "learning/strategy_search.py", "learning/sprint.py",
    "learning/signal_audit.py", "execution/paper.py", "execution/curve_price.py",
    "execution/protection.py", "learning/candidate_paper.py", "learning/autolearn_io.py")
SOURCE_PATHS = (*_BASE_SOURCES, "learning/prospective_quotes.py", "providers/gmgn_cli.py",
    "learning/gmgn_observations.py", "learning/gmgn_capture_store.py", "learning/gmgn_capture_quote.py")


class CapsuleError(ValueError):
    """Fixed reason only: private bodies, notes and identities never enter errors."""


def _require(value, reason="original_capture_invalid"):
    if not value:
        raise CapsuleError(reason)


def _int(value, minimum=0):
    _require(type(value) is int and minimum <= value <= 2**63 - 1)
    return value


def _hash(value):
    _require(type(value) is str and _SHA.fullmatch(value))
    return value


def _tree(value):
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        _require(count <= 16_384 and depth <= 32, "capture_json_complexity")
        if type(item) is dict:
            _require(len(item) <= 16_384 - count, "capture_json_complexity")
            _require(all(type(k) is str and len(k) <= MAX_CAPSULE_BYTES for k in item))
            pending.extend((v, depth + 1) for v in item.values())
        elif type(item) is list:
            _require(len(item) <= 16_384 - count, "capture_json_complexity")
            pending.extend((v, depth + 1) for v in item)
        elif type(item) is str:
            _require(len(item) <= MAX_CAPSULE_BYTES, "capture_json_oversize")
        elif type(item) is float:
            _require(math.isfinite(item))
        elif type(item) is int:
            _require(item.bit_length() <= 4096)
        else:
            _require(item is None or type(item) is bool)


def _encode(value):
    _tree(value)
    result, size = [], 0
    try:
        for part in json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False).iterencode(value):
            raw = part.encode("utf-8")
            size += len(raw)
            _require(size <= MAX_CAPSULE_BYTES, "capture_json_oversize")
            result.append(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CapsuleError("capture_json_invalid") from exc
    return b"".join(result)


def _pairs(items):
    result = {}
    for key, value in items:
        _require(key not in result, "capture_json_duplicate_key")
        result[key] = value
    return result


def _bad_constant(_):
    raise CapsuleError("capture_json_invalid")


def _target(value):
    _require(type(value) is dict)
    chain, token = value.get("chain"), value.get("token")
    _require(chain in ("sol", "bsc", "robinhood") and base._token(chain, token) == token)
    _require(value.get("kind") in ("pending", "open"))
    _int(value.get("signal_ms"), 1)
    _hash(value.get("spec_hash"))
    _require(value.get("hypothesis_id") == "ph_" + value["spec_hash"][:24])
    _require(type(value.get("campaign_id")) is str and re.fullmatch(r"pc_[0-9a-f]{24}", value["campaign_id"]))
    book = value.get("book")
    _require(type(book) is str and 0 < len(book) <= 256 and "/" not in book and "\\" not in book
             and book not in (".", ".."))
    return chain, token


def _validate(value):
    _require(type(value) is dict and set(value) == {"version", "target", "admission", "data", "receipt",
        "cache", "captured_ms", "received_ms", "sources"} and value["version"] == VERSION)
    target, admission, receipt = value["target"], value["admission"], value["receipt"]
    chain, token = _target(target)
    captured, received = _int(value["captured_ms"], 1), _int(value["received_ms"], 1)
    _require(type(admission) is dict and set(admission) == {"day", "ordinal", "attempt"})
    attempt, ordinal, day = admission["attempt"], _int(admission["ordinal"], 1), admission["day"]
    _require(ordinal <= base.DAILY_ATTEMPTS and type(day) is str and day.isascii() and day.isdecimal())
    _require(type(attempt) is dict and set(attempt) == {"attempt_id", "target", "admitted_ms", "status"}
             and attempt["target"] == target and attempt["status"] == "admitted")
    admitted = _int(attempt["admitted_ms"], 1)
    _require(day == str(admitted // base.DAY_MS) and target["signal_ms"] <= admitted <= received <= captured
             and attempt["attempt_id"] == digest([day, ordinal, chain + ":" + token]))
    _require(type(receipt) is dict and set(receipt) == _RECEIPT and receipt["provider"] == "gmgn"
             and receipt["endpoint"] == "token.info")
    observed = _int(receipt["observed_at_ms"], 1)
    _require(observed <= received and receipt["basis"] in {"provider_reported", "cached", "stale", "unavailable"}
             and (receipt["note"] is None or type(receipt["note"]) is str))
    request = f"gmgn-cli token info --address {token} --chain {chain} --raw"
    _require(receipt["request_digest"] is None or receipt["request_digest"] == digest(request))
    _require(receipt["response_digest"] is None or receipt["response_digest"] == digest(value["data"]))
    if receipt["basis"] == "provider_reported":
        _require(observed >= admitted and receipt["request_digest"] == digest(request)
                 and receipt["response_digest"] == digest(value["data"]))
    cache = value["cache"]
    if cache is not None:
        _require(type(cache) is dict and set(cache) == {"fetched_ms", "cache_sha256", "data_sha256"})
        fetched = _int(cache["fetched_ms"], 1)
        _hash(cache["cache_sha256"])
        _require(fetched <= observed and cache["data_sha256"] == digest(value["data"]))
    sources = value["sources"]
    _require(type(sources) is dict and set(sources) == set(SOURCE_PATHS))
    for source in sources.values():
        _hash(source)
    return value


def validate_capsule(raw: bytes) -> dict:
    """Decode a canonical bounded capsule without IO or timestamp replacement."""
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_CAPSULE_BYTES, "capture_json_oversize")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_bad_constant)
        _require(_encode(value) == raw, "capture_json_noncanonical")
        return _validate(value)
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as exc:
        raise CapsuleError("original_capture_invalid") from exc


def build_capsule(*, target, admission, data, receipt, cache, captured_ms, sources,
                  received_ms=None) -> bytes:
    """Detach all original JSON first; later mutation of the provider result cannot alter it."""
    raw = _encode({"version": VERSION, "target": target, "admission": admission, "data": data,
        "receipt": receipt, "cache": cache, "captured_ms": captured_ms,
        "received_ms": captured_ms if received_ms is None else received_ms, "sources": sources})
    validate_capsule(raw)
    return raw


def source_bindings() -> dict[str, str]:
    """Actual leaf/base bytes, in addition to the base Context's unchanged authority."""
    root = Path(__file__).absolute().parents[1]
    result = {}
    for name in SOURCE_PATHS:
        path = root / name
        _require(not any(p.is_symlink() for p in (path, *path.parents)))
        with path.open("rb") as stream:
            raw = stream.read(1_048_577)
        _require(len(raw) <= 1_048_576)
        result[name] = hashlib.sha256(raw).hexdigest()
    return result


class CapturingQuoteCollector(base.ProspectiveQuoteCollector):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.capture_store = CaptureStore(self.state_dir / "original-captures", learning_root=self.root)

    def _admission(self, target):
        lock = base._load(self.state_dir / "worker.lock", self.root, limit=4096)
        _require(type(lock) is dict and lock == {"pid": os.getpid()}, "capture_worker_lock_not_proved")
        state = base._load(self.state_path, self.root)
        _require(type(state) is dict and state.get("version") == base.VERSION and state.get("policy") == base.POLICY)
        attempts = state.get("attempts")
        _require(type(attempts) is list and 0 < len(attempts) <= 32)
        attempt = attempts[-1]
        _require(type(attempt) is dict and attempt.get("target") == target and attempt.get("status") == "admitted",
                 "capture_original_admission_not_proved")
        admitted = _int(attempt.get("admitted_ms"), 1)
        day, key = str(admitted // base.DAY_MS), target["chain"] + ":" + target["token"]
        days, tokens = state.get("days"), state.get("tokens")
        _require(type(days) is dict and type(tokens) is dict and tokens.get(key) == admitted
                 and state.get("updated_ms") == admitted and state.get("last_target") == key)
        admission = {"day": day, "ordinal": days.get(day), "attempt": attempt}
        # Strict validation of the ordinal, identifier and full target follows in the codec.
        return json.loads(base._json(admission))

    def _cache_binding(self, target, data, receipt):
        from kaiba.providers._http import cache_path

        root = (get_settings().kaiba_data_dir / "cache/gmgn").resolve()
        request = f"gmgn-cli token info --address {target['token']} --chain {target['chain']} --raw"
        path = base._safe(cache_path("gmgn", request), root)
        try:
            _require(path.is_file() and path.stat().st_size <= base.MAX_CACHE_BYTES)
            with path.open("rb") as stream:
                raw = stream.read(base.MAX_CACHE_BYTES + 1)
            _require(len(raw) <= base.MAX_CACHE_BYTES)
            packet = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_bad_constant)
            _require(type(packet) is dict and digest(packet.get("data")) == digest(data))
            fetched = _int(packet.get("fetched_ms"), 1)
            _require(fetched <= _int(receipt.get("observed_at_ms"), 1))
            return {"fetched_ms": fetched, "cache_sha256": hashlib.sha256(raw).hexdigest(), "data_sha256": digest(data)}
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _verify_cache(self, target: dict, result: Any, now: int) -> dict:
        capture = {"version": VERSION, "status": "not_retained", "reason": "capture_unavailable"}
        raw = None
        try:
            admission = self._admission(target)
            receipt = result.receipt.model_dump(mode="json")
            cache = self._cache_binding(target, result.data, receipt)
            raw = build_capsule(target=target, admission=admission, data=result.data, receipt=receipt,
                cache=cache, received_ms=now, captured_ms=self._now(), sources=source_bindings())
            reference = self.capture_store.put(raw)
            _require(self.capture_store.get(reference) == raw, "capture_readback_unknown")
            _require(self._admission(target) == admission, "capture_admission_changed")
            capture = {"version": VERSION, "status": "retained_unqualified", "reference": reference,
                       "retained_ms": self._now()}
        except (CapsuleError, CaptureStoreError, OSError, ValueError, TypeError, KeyError, AttributeError):
            pass  # The base result and permanently consumed admission remain unchanged.
        evidence = super()._verify_cache(target, result, self._now())
        if raw is not None and "reference" in capture:
            capsule = validate_capsule(raw)
            expected = capsule["cache"]
            if (evidence.get("verified") is True and expected is not None
                    and all(evidence.get(key) == expected[key] for key in expected)
                    and evidence.get("receipt") == {k: capsule["receipt"][k] for k in evidence["receipt"]}):
                capture["status"] = "retained"
        return {**evidence, "original_capture": capture}


def _selected_attempt(quote, state, expected_snapshot):
    _require(type(state) is dict and state.get("version") == base.VERSION and state.get("policy") == base.POLICY)
    attempts, days = state.get("attempts"), state.get("days")
    _require(type(attempts) is list and len(attempts) <= 32 and type(days) is dict and len(days) <= 3)
    selected = quote.get("diagnostics", {}).get("cached_info")
    _require(type(selected) is dict and quote.get("basis") == "captured_gmgn_pool_depth")
    matches = []
    for attempt in attempts:
        _require(type(attempt) is dict)
        target, evidence = attempt.get("target"), attempt.get("cache")
        if (type(target) is not dict or target.get("chain") != quote.get("chain")
                or target.get("token") != quote.get("token") or type(evidence) is not dict):
            continue
        marker = evidence.get("original_capture")
        if (attempt.get("status") != "captured" or evidence.get("verified") is not True
                or evidence.get("market_fields_usable") is not True or type(marker) is not dict
                or marker.get("version") != VERSION or marker.get("status") != "retained"
                or evidence.get("cache_sha256") != selected.get("sha256")
                or evidence.get("fetched_ms") != selected.get("fetched_ms")
                or target.get("allocation_snapshot_token") != expected_snapshot):
            continue
        matches.append(attempt)
    _require(len(matches) == 1, "capture_exact_reference_unavailable")
    attempt = matches[0]
    _require(_int(days.get(str(_int(attempt.get("admitted_ms"), 1) // base.DAY_MS))) <= base.DAILY_ATTEMPTS)
    return attempt


def quote_evidence(reader, chain, token, *, now_ms, max_age_ms=120_000, cache_root=None,
                   learning_root=None, observer_state_dir=None, expected_allocation_snapshot=None):
    """The existing quote path plus one exact, read-only original-capture lookup.

    The installing worker must supply its full validated Context snapshot for an
    allocated study. None is the legacy/no-allocation scope, never a wildcard.
    Missing/invalid captures preserve the original quote, including unknown taxes.
    """
    from kaiba.learning import gmgn_capture_quote, tax_quote

    quote = tax_quote.quote_evidence(reader, chain, token, now_ms=now_ms,
                                    max_age_ms=max_age_ms, cache_root=cache_root)
    if quote.get("unavailable_reason") or quote.get("basis") != "captured_gmgn_pool_depth":
        return quote
    try:
        root = Path(learning_root or get_settings().kaiba_data_dir / "learning").absolute()
        own = base._safe(Path(observer_state_dir or root / "prospective-quotes"), root)
        state = base._load(own / "state.json", root)
        attempt = _selected_attempt(quote, state, expected_allocation_snapshot)
        reference = attempt["cache"]["original_capture"]["reference"]
        store = CaptureStore(own / "original-captures", learning_root=root)
        raw = store.get(reference)
        capsule = validate_capsule(raw)
        expected_attempt = {"attempt_id": attempt["attempt_id"], "target": attempt["target"],
                            "admitted_ms": attempt["admitted_ms"], "status": "admitted"}
        _require(capsule["admission"]["attempt"] == expected_attempt
                 and capsule["sources"] == source_bindings())
        day, ordinal = capsule["admission"]["day"], capsule["admission"]["ordinal"]
        _require(ordinal <= _int(state["days"].get(day)) <= base.DAILY_ATTEMPTS)
        return gmgn_capture_quote.augment(quote, raw, chain, token, now_ms, max_age_ms=max_age_ms)
    except (CapsuleError, CaptureStoreError, OSError, ValueError, TypeError, KeyError):
        return quote


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--research-dir", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--db", type=Path)
    args = parser.parse_args(argv)
    root = get_settings().kaiba_data_dir / "learning"
    report = CapturingQuoteCollector(args.research_dir or root / "autolearn",
        args.state_dir or root / "prospective-quotes", db_path=args.db).once()
    capture = report.get("attempt", {}).get("cache", {}).get("original_capture", {})
    print(base._json({"status": report.get("status"), "provider_attempts": report.get("provider_attempts"),
                     "capture_status": capture.get("status"), "funding_allowed": False}))
    return 1 if report.get("status") in ("partial", "unknown") else 0


if __name__ == "__main__":
    raise SystemExit(main())
