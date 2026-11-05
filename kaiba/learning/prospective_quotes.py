"""Bounded market observations for genuine candidate PAPER inventory.

Codex AGENT-OBSERVATION-20261015. This separate worker can make one allowlisted
read; the AF_UNIX paper worker remains transport-free. Local reservations count
attempts, including crashes and refusals, rather than claiming actual provider spend.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kaiba.core.config import get_settings
from kaiba.core.schemas import Chain, digest
from kaiba.learning.autolearn import _pid_alive
from kaiba.learning.autolearn_io import DAY_MS, WAL_LIMIT, GuardedReader, SourceDeferred
from kaiba.learning.candidate_paper import CandidatePaperRunner
from kaiba.learning.strategy_campaign import CAMPAIGN_VERSION, _campaign_id, source_versions

VERSION = "prospective_quotes/1"
MAX_BOOKS, MAX_TARGETS, ROW_LIMIT = 18, 32, 64
DAILY_ATTEMPTS, TOKEN_INTERVAL_MS = 100, 15_000
MAX_JSON_BYTES, MAX_CACHE_BYTES = 1_000_000, 2_000_000
SCAN_SECONDS = 10
POLICY = {"daily_attempts": DAILY_ATTEMPTS, "token_interval_ms": TOKEN_INTERVAL_MS,
          "max_books": MAX_BOOKS, "max_targets": MAX_TARGETS, "calls_per_cycle": 1,
          "research_scan_seconds": SCAN_SECONDS,
          "priority": "RESEARCH", "ttl_s": 15, "stale_grace_s": 0,
          "timeout_s": 6, "wait_for_slot_s": 0}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _integer(value: Any, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("invalid_integer_clock_or_counter")
    return value


def _positive(value: Any) -> bool:
    try:
        number = Decimal(str(value))
        return not isinstance(value, bool) and number.is_finite() and number > 0
    except (ValueError, TypeError, InvalidOperation):
        return False


def _failure_category(note: Any) -> str:
    """Fixed labels only; provider notes may contain arbitrary confidential text."""
    if not isinstance(note, str) or not note:
        return "unavailable_detail_not_retained"
    match = re.fullmatch(
        r"limiter refused: gmgn: (minimum interval|max inflight|bucket exhausted|provider cooldown|token cooldown|daily credit cap) "
        r"\(retry in [0-9.]+s\)", note)
    if match:
        return "local_limiter_" + match.group(1).replace(" ", "_")
    if note.startswith("gmgn-cli not found; install it globally or set GMGN_CLI_PATH"):
        return "local_cli_not_installed"
    return "unavailable_other_redacted"


def _token(chain: str, token: Any) -> str:
    pattern = r"[1-9A-HJ-NP-Za-km-z]{32,44}" if chain == "sol" else r"0x[0-9a-fA-F]{40}"
    if chain not in ("sol", "bsc", "robinhood") or not isinstance(token, str) or not re.fullmatch(pattern, token):
        raise ValueError("invalid_candidate_token_identity")
    return token if chain == "sol" else token.lower()


def _safe(path: Path, root: Path) -> Path:
    # Check the lexical path before resolving: resolving first conceals a symlink.
    absolute = path.absolute()
    for item in (absolute, *absolute.parents):
        if item.is_symlink():
            raise ValueError("research_symlink_refused")
    resolved = absolute.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("research_path_escape")
    return resolved


def _load(path: Path, root: Path, *, default: Any = None, limit: int = MAX_JSON_BYTES) -> Any:
    path = _safe(path, root)
    if not path.exists():
        return default
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError("research_json_size_or_type_refused")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("research_json_grew_over_cap")
    return json.loads(raw)


def _save(path: Path, root: Path, value: Any) -> None:
    path = _safe(path, root)
    raw = (_json(value) + "\n").encode()
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("observer_state_size_refused")
    temporary = _safe(path.with_suffix(".partial"), root)
    with temporary.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class ProspectiveQuoteCollector:
    def __init__(self, research_dir: Path | str, state_dir: Path | str, *,
                 learning_root: Path | str | None = None, db_path: Path | str | None = None,
                 clock: Callable[[], float] = time.time, provider: Any = None):
        settings = get_settings()
        self.root = Path(learning_root or settings.kaiba_data_dir / "learning").resolve()
        self.research_dir = _safe(Path(research_dir), self.root)
        self.state_dir = _safe(Path(state_dir), self.root)
        self.db_path = Path(db_path or settings.db_path).resolve()
        if self.db_path != settings.db_path.resolve():
            raise ValueError("WAL_guard_must_match_provider_accounting_database")
        if (self.research_dir == self.state_dir or self.db_path.is_relative_to(self.root)
                or self.state_dir.is_relative_to(self.research_dir)):
            raise ValueError("observer_requires_separate_owned_state")
        self.clock, self.provider = clock, provider
        self.frontier_ms = 0
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / "state.json"

    def _now(self) -> int:
        value = self.clock()
        if isinstance(value, bool) or not _positive(value):
            raise ValueError("invalid_observer_clock")
        self.frontier_ms = max(self.frontier_ms, int(Decimal(str(value)) * 1000))
        return self.frontier_ms

    def _wal_guard(self, state: dict) -> None:
        path = Path(str(self.db_path) + "-wal")
        size = path.stat().st_size if path.exists() else 0
        if size > WAL_LIMIT:
            state["retry_after_ms"] = self._now() + 60_000
            raise SourceDeferred("wal_over_limit:" + str(size))

    def _fresh_cache(self, target: dict) -> dict | None:
        """Observe existing canonical bytes, without a new provider receipt or clock."""
        chain, token = target["chain"], target["token"]
        key = f"gmgn-cli token info --address {token} --chain {chain} --raw"
        root = (get_settings().kaiba_data_dir / "cache/gmgn").resolve()
        try:
            path = _safe(root / (digest(key)[:32] + ".json"), root)
            packet = _load(path, root, limit=MAX_CACHE_BYTES)
            if not isinstance(packet, dict):
                return None
            data, fetched = packet["data"], _integer(packet["fetched_ms"], minimum=1)
            now = self._now()  # File I/O must not make a new capture appear future-dated.
            if not 0 <= now - fetched <= TOKEN_INTERVAL_MS or not isinstance(data, dict):
                return None
            price = data.get("price")
            if (_token(chain, data.get("address")) != token or not isinstance(price, dict)
                    or not _positive(price.get("price")) or not _positive(data.get("liquidity"))
                    or type(data.get("decimals")) is not int or not 0 <= data["decimals"] <= 30):
                return None
            for block in (data, price, data.get("dev"), data.get("link")):
                if isinstance(block, dict) and (block.get("chain", chain) != chain
                        or "address" in block and _token(chain, block["address"]) != token):
                    return None
            pool = data.get("pool")
            if isinstance(pool, dict) and (pool.get("chain", chain) != chain or
                    pool.get("base_address") and pool.get("quote_address") and token not in
                    {_token(chain, pool["base_address"]), _token(chain, pool["quote_address"])}):
                return None
            return {"sampled_ms": now, "fetched_ms": fetched, "age_ms": now - fetched,
                    "cache_path": str(path), "data_sha256": digest(data),
                    "basis": "existing_canonical_cache_only", "new_receipt_created": False}
        except (OSError, ValueError, TypeError, KeyError):
            return None  # Unknown cache evidence cannot authorize a preflight skip.

    def _limiter_preflight(self, state: dict) -> dict:
        """Read-only hints; the existing atomic reserve remains the final authority."""
        from kaiba.core.limiter import INFLIGHT_LEASE_S, INFLIGHT_STALE_MS, limits_for
        reader = GuardedReader(self.db_path, query_seconds=3, max_rows=2)
        try:
            self._wal_guard(state)
            rows = reader.rows("SELECT credit_milli,last_refill_ms,last_call_ms,inflight,banned_until_ms,"
                "penalty_level,spent_today,day_key,inflight_over_since_ms FROM provider_state WHERE provider='gmgn'")
            self._wal_guard(state)
            bans = reader.rows("SELECT banned_until_ms FROM provider_family_bans WHERE provider='gmgn' AND family='token'")
        except SourceDeferred:
            state["retry_after_ms"] = max(state.get("retry_after_ms", 0), self._now() + 60_000)
            raise
        now = self._now()
        result = {"sampled_ms": now, "category": "unknown_allow_authoritative_guard", "known_refusal": False,
                  "query_receipts": reader.receipts}
        if not rows:
            return result
        try:
            st = rows[0]
            values = {k: _integer(st[k]) for k in ("last_refill_ms", "last_call_ms", "inflight",
                       "banned_until_ms", "penalty_level", "spent_today")}
            credit = st["credit_milli"]
            if type(credit) is not int or not isinstance(st["day_key"], str):
                return result
            lease = st["inflight_over_since_ms"]
            if lease is not None:
                lease = _integer(lease)
            if (values["last_refill_ms"] > now or values["last_call_ms"] > now
                    or lease is not None and lease > now):
                return result
            family = _integer(bans[0]["banned_until_ms"]) if bans else 0
            limits = limits_for("gmgn")
            weight, capacity, interval = limits.weight_for("token.info"), limits.capacity, limits.min_interval_ms
            refill = Decimal(str(limits.refill_per_s))
            if (type(weight) is not int or type(capacity) is not int or type(interval) is not int
                    or weight <= 0 or capacity <= 0 or interval < 0 or not refill.is_finite() or refill < 0
                    or type(limits.max_inflight) is not int or limits.max_inflight <= 0):
                return result
            category, retry_ms = None, 0
            if values["banned_until_ms"] > now:
                category, retry_ms = "provider_cooldown", values["banned_until_ms"] - now
            elif family > now:
                category, retry_ms = "token_cooldown", family - now
            else:
                day = datetime.datetime.fromtimestamp(now / 1000, datetime.UTC).date().isoformat()
                spent = values["spent_today"] if st["day_key"] == day else 0
                effective = interval * 2 ** min(values["penalty_level"], 8)
                if limits.daily_credit_cap is not None and spent + weight > _integer(limits.daily_credit_cap):
                    category, retry_ms = "daily_credit_cap", 3_600_000
                elif 0 < values["last_call_ms"] <= now and now - values["last_call_ms"] < effective:
                    category, retry_ms = "minimum_interval", effective - (now - values["last_call_ms"])
                elif (values["inflight"] >= limits.max_inflight and lease is not None and 0 < lease <= now
                        and now - lease < INFLIGHT_LEASE_S * 1000
                        and not (values["last_call_ms"] and now - values["last_call_ms"] > max(INFLIGHT_STALE_MS, interval * 10))):
                    category, retry_ms = "max_inflight", 500
                elif weight > capacity:
                    category = "weight_exceeds_capacity"
                else:
                    # Match the authoritative limiter's float operation order at
                    # the integer milli-credit boundary; Decimal rounding here
                    # could falsely classify an atomically admissible request.
                    available = min(capacity * 1000, int(credit +
                        max(0.0, (now - values["last_refill_ms"]) / 1000.0) * limits.refill_per_s * 1000))
                    if available < weight * 1000:
                        category = "bucket_exhausted"
                        retry_ms = max(200, int(Decimal(weight * 1000 - available) / max(refill, Decimal("0.1"))))
            if category:
                result.update(category=category, known_refusal=True, retry_in_ms=retry_ms,
                              retry_not_before_ms=now + retry_ms)
            else:
                result["category"] = "no_known_refusal_atomic_guard_required"
        except (ValueError, TypeError, KeyError, ArithmeticError, OverflowError):
            pass  # Malformed/uncertain hints must not replace the authoritative limiter.
        return result

    def _book(self, manifest: dict, arm: dict, *, now: int) -> list[dict]:
        # Derive the path from immutable identities, never from an arbitrary report path.
        runner_type = CandidatePaperRunner
        if ("execution_policy" in manifest.get("assessment_policy", {})
                or any("execution_policy" in row.get("spec", {}) for row in manifest.get("hypotheses", []))):
            from kaiba.learning.admission_paper import AdmissionPaperRunner, validate_manifest
            validate_manifest(manifest, now_ms=now)
            runner_type = AdmissionPaperRunner
        runner = runner_type(self.research_dir / "paper", learning_root=self.root)
        path = _safe(runner.book_path(manifest["campaign_id"], arm["hypothesis_id"]), self.research_dir)
        if not path.exists():
            return []
        if self.db_path.exists() and path.samefile(self.db_path):
            raise ValueError("research_book_aliases_main_database")
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.25, isolation_level=None)
        conn.row_factory = sqlite3.Row
        started = time.monotonic()
        conn.set_progress_handler(lambda: int(time.monotonic() - started >= 3), 1000)
        try:
            conn.execute("PRAGMA query_only=ON")
            row = conn.execute("SELECT substr(value,1,32769) FROM candidate_meta WHERE key='identity'").fetchone()
            if not row or len(row[0]) > 32_768:
                raise ValueError("candidate_identity_missing_or_oversized")
            identity = json.loads(row[0])
            policy = identity["runner"]
            if (policy["entry_delay_ms"] != 30_000 or policy["entry_max_age_ms"] != 120_000
                    or policy["quote_max_age_ms"] != 60_000 or policy["poll_ms"] != 12_000
                    or policy["processing_clock_policy"] not in ("observed_wall_clock/1", "explicit_frontier/1")):
                raise ValueError("unsupported_frozen_candidate_policy")
            runner = runner_type(self.research_dir / "paper", learning_root=self.root,
                max_stake_usd=policy["max_stake_usd"], max_open_per_arm=policy["max_open_per_arm"],
                clock=self.clock if policy["processing_clock_policy"] == "observed_wall_clock/1" else None)
            if identity != runner._identity(manifest, arm):
                raise ValueError("candidate_frozen_identity_changed")
            tick = conn.execute("SELECT value FROM candidate_meta WHERE key='last_tick_ms'").fetchone()
            if tick and int(tick[0]) > now:
                raise ValueError("candidate_book_from_future")
            if conn.execute("SELECT 1 FROM orders WHERE mode!='shadow' OR provider!='paper' LIMIT 1").fetchone():
                raise ValueError("foreign_orders_in_candidate_book")
            rows = conn.execute(
                "SELECT e.signal_id,substr(e.payload,1,32769) AS payload,e.status,e.decision_id,e.position_id,"
                "e.registered_ms,e.source_observed_ms,e.candidate_received_ms,e.processing_ms,e.quote_ms,"
                "p.chain AS p_chain,p.token AS p_token,p.lane AS p_lane,p.mode AS p_mode,"
                "p.opened_ms,p.closed_ms,p.qty,p.cost_native FROM candidate_entries e "
                "LEFT JOIN positions p ON p.position_id=e.position_id "
                "WHERE e.status='pending' OR (p.position_id IS NOT NULL AND p.closed_ms IS NULL) "
                "ORDER BY e.signal_id LIMIT ?", (ROW_LIMIT + 1,)).fetchall()
            if len(rows) > ROW_LIMIT:
                raise ValueError("active_candidate_row_cap")
            targets = []
            for entry in rows:
                if len(entry["payload"]) > 32_768:
                    raise ValueError("candidate_payload_cap")
                payload = json.loads(entry["payload"])
                signal = _integer(payload["signal_ms"], minimum=1)
                chain = arm["spec"]["chain"]
                token = _token(chain, payload["token"])
                if (not manifest["selected_ms"] < signal <= now or payload["chain"] != chain
                        or payload["lane"] != arm["spec"]["lane"] or payload["signal_id"] != entry["signal_id"]):
                    raise ValueError("candidate_entry_identity_or_clock_invalid")
                for key in ("source_observed_ms", "feature_ts_ms"):
                    if payload.get(key) is not None and _integer(payload[key], minimum=1) > now:
                        raise ValueError("candidate_payload_future_clock")
                for key in ("registered_ms", "source_observed_ms", "candidate_received_ms", "processing_ms", "quote_ms"):
                    if entry[key] is not None and _integer(entry[key], minimum=1) > now:
                        raise ValueError("candidate_entry_future_clock")
                if payload.get("feature_ts_ms", signal) > signal:
                    raise ValueError("candidate_future_features")
                opened = entry["position_id"] is not None and entry["closed_ms"] is None
                if opened:
                    if (entry["p_mode"] != "shadow" or entry["p_chain"] != chain
                            or _token(chain, entry["p_token"]) != token or entry["p_lane"] != payload["lane"]
                            or not signal + 30_000 <= _integer(entry["opened_ms"], minimum=1) <= now
                            or not _positive(entry["qty"]) or not _positive(entry["cost_native"])):
                        raise ValueError("candidate_open_position_not_proved")
                    buy = conn.execute(
                        "SELECT 1 FROM position_orders po JOIN orders o ON o.order_id=po.order_id "
                        "WHERE po.position_id=? AND o.decision_id=? AND o.chain=? AND o.token=? "
                        "AND o.lane=? AND o.side='buy' AND o.state='filled' AND o.mode='shadow' "
                        "AND o.provider='paper' AND o.created_ms<=? LIMIT 1",
                        (entry["position_id"], entry["decision_id"], chain, payload["token"], payload["lane"], now)).fetchone()
                    if not buy:
                        raise ValueError("candidate_open_buy_not_proved")
                elif entry["status"] != "pending" or not 30_000 <= now - signal <= 120_000:
                    continue
                if not opened:
                    decision_id = entry["decision_id"] or "cp_" + digest([arm["hypothesis_id"], entry["signal_id"]])[:32]
                    if conn.execute("SELECT 1 FROM orders WHERE decision_id=? LIMIT 1", (decision_id,)).fetchone():
                        continue  # uncertain ledger exposure is never treated as a new entry
                targets.append({"chain": chain, "token": token, "kind": "open" if opened else "pending",
                    "signal_ms": signal, "campaign_id": manifest["campaign_id"],
                    "hypothesis_id": arm["hypothesis_id"], "spec_hash": arm["spec_hash"],
                    "book": path.name})
            return targets
        finally:
            conn.close()

    def _targets(self, now: int) -> tuple[list[dict], list[dict]]:
        from kaiba.learning.allocation_runtime import load_context

        began = time.monotonic()
        active = _load(self.research_dir / "state.json", self.research_dir)
        allocation = load_context(self.research_dir, learning_root=self.root, state=active,
                                  now_ms=now, clock=self.clock)
        if active is None and allocation is None:
            return [], []
        pairs, targets, errors = [], {}, []
        if allocation is not None:
            pairs = [(p["manifest"], p["arm"], p["lease"]) for p in allocation.leased_pairs()]
            if len(pairs) > 12:
                raise ValueError("allocation_active_lease_cap")
        else:
            names = active.get("campaign_files", [])
            if not isinstance(names, list) or len(names) > MAX_BOOKS or len(set(names)) != len(names):
                raise ValueError("active_campaign_cap_or_duplicates")
            current = source_versions()
            for name in names:
                if not isinstance(name, str) or Path(name).name != name or not name.endswith(".json"):
                    raise ValueError("active_manifest_path_refused")
                manifest = _load(self.research_dir / name, self.research_dir)
                if (not isinstance(manifest, dict) or manifest.get("version") != CAMPAIGN_VERSION
                        or manifest.get("source_versions") != current
                        or manifest.get("campaign_id") != _campaign_id(manifest)
                        or not 0 < _integer(manifest["selected_ms"], minimum=1) <= now):
                    raise ValueError("active_manifest_not_frozen_current")
                pairs.extend((manifest, arm, None) for arm in manifest["hypotheses"])
                if len(pairs) > MAX_BOOKS:
                    raise ValueError("active_book_cap")
        for manifest, arm, lease in pairs:
            if time.monotonic() - began >= SCAN_SECONDS:
                raise SourceDeferred("research_metadata_scan_deadline")
            if (arm.get("status") != "PAPER_ONLY" or arm.get("spec_hash") != digest(arm["spec"])
                    or arm.get("hypothesis_id") != "ph_" + arm["spec_hash"][:24]):
                raise ValueError("active_arm_not_frozen_paper")
            try:
                for target in self._book(manifest, arm, now=now):
                    if allocation is not None:
                        target.update(allocation_snapshot_token=allocation.snapshot_token, allocation_lease=lease)
                    identity = target["chain"], target["token"]
                    if identity not in targets or target["kind"] == "open":
                        targets[identity] = target
            except (sqlite3.Error, ValueError, KeyError, TypeError, OSError) as exc:
                errors.append({"hypothesis_id": arm["hypothesis_id"], "reason": type(exc).__name__ + ":" + str(exc)[:160]})
        if allocation is not None:
            allocation.revalidate(now_ms=now)
        if len(targets) > MAX_TARGETS:
            raise ValueError("unique_target_cap")
        return [targets[k] for k in sorted(targets)], errors

    def _verify_cache(self, target: dict, result: Any, now: int) -> dict:
        receipt = result.receipt.model_dump(mode="json") if hasattr(result.receipt, "model_dump") else dict(result.receipt)
        kept = {k: receipt.get(k) for k in ("provider", "endpoint", "basis", "observed_at_ms", "request_digest", "response_digest")}
        evidence = {"verified": False, "receipt": kept}
        if not result.ok:
            evidence["failure_category"] = _failure_category(receipt.get("note"))
        try:
            return self._cache_evidence(target, result, now, receipt, evidence)
        except (ValueError, TypeError, KeyError, OSError):
            return {**evidence, "reason": "canonical_cache_or_receipt_invalid"}

    def _cache_evidence(self, target: dict, result: Any, now: int, receipt: dict, evidence: dict) -> dict:
        from kaiba.providers._http import cache_path
        if not result.ok:
            return {**evidence, "reason": "provider_unavailable"}
        chain, token = target["chain"], target["token"]
        key = f"gmgn-cli token info --address {token} --chain {chain} --raw"
        if (receipt.get("provider") != "gmgn" or receipt.get("endpoint") != "token.info"
                or receipt.get("basis") not in ("provider_reported", "cached")
                or not 0 <= now - _integer(receipt.get("observed_at_ms"), minimum=1) <= TOKEN_INTERVAL_MS
                or receipt.get("request_digest") != digest(key) or receipt.get("response_digest") != digest(result.data)):
            return {**evidence, "reason": "provider_receipt_not_proved"}
        root = (get_settings().kaiba_data_dir / "cache" / "gmgn").resolve()
        path = _safe(cache_path("gmgn", key), root)
        if not path.is_file() or path.stat().st_size > MAX_CACHE_BYTES:
            return {**evidence, "reason": "canonical_cache_missing_or_oversized"}
        with path.open("rb") as stream:
            raw = stream.read(MAX_CACHE_BYTES + 1)
        if len(raw) > MAX_CACHE_BYTES:
            return {**evidence, "reason": "canonical_cache_grew_over_cap"}
        packet = json.loads(raw)
        if not isinstance(packet, dict) or digest(packet.get("data")) != digest(result.data):
            return {**evidence, "reason": "canonical_cache_missing_or_changed"}
        fetched = _integer(packet.get("fetched_ms"), minimum=1)
        if not 0 <= now - fetched <= TOKEN_INTERVAL_MS or fetched > receipt["observed_at_ms"]:
            return {**evidence, "reason": "canonical_cache_clock_not_proved"}
        data = packet["data"]
        if not isinstance(data, dict) or _token(chain, data.get("address")) != token:
            return {**evidence, "reason": "canonical_cache_token_mismatch"}
        for block in (data, data.get("price"), data.get("dev"), data.get("link")):
            if isinstance(block, dict) and (block.get("chain", chain) != chain
                    or ("address" in block and _token(chain, block["address"]) != token)):
                return {**evidence, "reason": "canonical_cache_component_identity_mismatch"}
        pool = data.get("pool")
        if isinstance(pool, dict):
            if pool.get("chain", chain) != chain:
                return {**evidence, "reason": "canonical_cache_pool_chain_mismatch"}
            # Pool.address identifies the LP. Either constituent may be the token;
            # a lone opposite constituent does not prove a response conflict.
            if (pool.get("base_address") and pool.get("quote_address")
                    and token not in {_token(chain, pool["base_address"]), _token(chain, pool["quote_address"])}):
                return {**evidence, "reason": "canonical_cache_pool_constituents_mismatch"}
        price = data.get("price")
        decimals = data.get("decimals")
        usable = (isinstance(price, dict) and _positive(price.get("price")) and _positive(data.get("liquidity"))
                  and type(decimals) is int and 0 <= decimals <= 30)
        return {**evidence, "verified": True, "reason": "captured" if usable else "captured_market_fields_incomplete",
                "market_fields_usable": usable, "fetched_ms": fetched, "data_sha256": digest(data),
                "cache_path": str(path), "cache_sha256": hashlib.sha256(raw).hexdigest()}

    def once(self) -> dict:
        lock = _safe(self.state_dir / "worker.lock", self.root)
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                existing = _load(lock, self.root, limit=4096)
                if isinstance(existing, dict) and not _pid_alive(_integer(existing.get("pid"), minimum=1)):
                    lock.unlink()
                    return self.once()
            except (PermissionError, ValueError):
                pass
            return {"version": VERSION, "status": "locked", "provider_attempts": 0, "funding_allowed": False}
        with os.fdopen(fd, "w") as stream:
            stream.write(_json({"pid": os.getpid()}))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            return self._once()
        finally:
            lock.unlink(missing_ok=True)

    def _once(self) -> dict:
        from kaiba.learning.allocation_runtime import load_context

        def same_target(target, candidates):
            return any(t["chain"] == target["chain"] and t["token"] == target["token"]
                       and t.get("allocation_snapshot_token") == target.get("allocation_snapshot_token")
                       for t in candidates)

        def allocation_current(target):
            context = load_context(self.research_dir, learning_root=self.root,
                                   now_ms=self._now(), clock=self.clock)
            token = context.snapshot_token if context is not None else None
            if token != target.get("allocation_snapshot_token"):
                raise SourceDeferred("allocation_authority_snapshot_changed")

        now = self._now()
        state = _load(self.state_path, self.root, default={"version": VERSION, "policy": POLICY,
            "days": {}, "tokens": {}, "attempts": [], "retry_after_ms": 0, "updated_ms": 0})
        report = {"version": VERSION, "at_ms": now, "status": "idle", "provider_attempts": 0,
                  "funding_allowed": False, "mode": "paper_observation", "policy": POLICY,
                  "source_versions": {**source_versions(),
                      "learning/prospective_quotes.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                      "providers/gmgn_cli.py": hashlib.sha256(
                          (Path(__file__).parents[1] / "providers/gmgn_cli.py").read_bytes()).hexdigest()}}
        try:
            if state.get("version") != VERSION or state.get("policy") != POLICY:
                raise ValueError("observer_state_policy_changed")
            if _integer(state.get("updated_ms", 0)) > now:
                raise ValueError("observer_clock_moved_backward")
            if (not isinstance(state.get("days"), dict) or len(state["days"]) > 3
                    or not isinstance(state.get("tokens"), dict) or len(state["tokens"]) > 201
                    or not isinstance(state.get("attempts"), list) or len(state["attempts"]) > 32):
                raise ValueError("observer_state_structure_or_cap")
            for day, count in state["days"].items():
                if (not day.isdecimal() or int(day) > now // DAY_MS
                        or not 0 <= _integer(count) <= DAILY_ATTEMPTS):
                    raise ValueError("observer_admission_counter_invalid")
            if now < _integer(state.get("retry_after_ms", 0)):
                raise SourceDeferred("source_retry_backoff")
            now = self._now()
            if _integer(state["days"].get(str(now // DAY_MS), 0)) >= DAILY_ATTEMPTS:
                # No eligibility scan is needed when no further observation can
                # be admitted. Unknown is distinct from an evaluated empty book.
                report.update(status="deferred", reason="daily_research_attempt_cap",
                              eligible_targets=None, eligibility_evaluated=False)
                return report
            targets, errors = self._targets(now)
            report.update(eligible_targets=len(targets), eligibility_evaluated=True, book_errors=errors)
            if not targets:
                report["status"] = "partial" if errors else "idle"
                return report
            now = self._now()
            day = str(now // DAY_MS)
            count = _integer(state["days"].get(day, 0))
            if count >= DAILY_ATTEMPTS:
                report.update(status="deferred", reason="daily_research_attempt_cap")
                return report
            last = state.get("last_target", "")
            available = [t for t in targets if now - _integer(state["tokens"].get(t["chain"] + ":" + t["token"], 0)) >= TOKEN_INTERVAL_MS
                         and (t["kind"] == "open" or 30_000 <= now - t["signal_ms"] <= 120_000)]
            if not available:
                report.update(status="deferred", reason="token_cadence_or_entry_window")
                return report
            available.sort(key=lambda t: t["chain"] + ":" + t["token"])
            target = next((t for t in available if t["chain"] + ":" + t["token"] > last), available[0])
            self._wal_guard(state)
            token_key = target["chain"] + ":" + target["token"]
            existing = self._fresh_cache(target)
            if existing is not None:
                state["last_target"] = token_key  # A repeatedly refreshed token cannot starve other targets.
                report.update(status="deferred", reason="fresh_canonical_cache_already_present", existing_cache=existing)
                return report
            preflight = self._limiter_preflight(state)
            report["limiter_preflight"] = preflight
            if preflight["known_refusal"]:
                report.update(status="deferred", reason="known_shared_limiter_refusal")
                return report
            # Preflight metadata I/O can cross UTC midnight, token cadence or the
            # unchanged entry deadline. Charge the actual admission day only.
            self._wal_guard(state)
            now = self._now()
            day = str(now // DAY_MS)
            count = _integer(state["days"].get(day, 0))
            if count >= DAILY_ATTEMPTS:
                report.update(status="deferred", reason="daily_research_attempt_cap")
                return report
            if (now - _integer(state["tokens"].get(token_key, 0)) < TOKEN_INTERVAL_MS
                    or target["kind"] == "pending" and not 30_000 <= now - target["signal_ms"] <= 120_000):
                report.update(status="deferred", reason="cadence_or_entry_window_elapsed_before_admission")
                return report
            if target.get("allocation_snapshot_token") is not None:
                current, _ = self._targets(now)
                if not same_target(target, current):
                    report.update(status="deferred", reason="eligibility_or_allocation_changed_before_admission")
                    return report
            else:
                allocation_current(target)
            now = self._now()
            day = str(now // DAY_MS)
            count = _integer(state["days"].get(day, 0))
            if count >= DAILY_ATTEMPTS:
                report.update(status="deferred", reason="daily_research_attempt_cap")
                return report
            if (now - _integer(state["tokens"].get(token_key, 0)) < TOKEN_INTERVAL_MS
                    or target["kind"] == "pending" and not 30_000 <= now - target["signal_ms"] <= 120_000):
                report.update(status="deferred", reason="cadence_or_entry_window_elapsed_before_admission")
                return report
            attempt = {"attempt_id": digest([day, count + 1, token_key]), "target": target,
                       "admitted_ms": now, "status": "admitted"}
            # Durable reservation precedes any provider call. Crashes consume the slot;
            # an admitted ID is retained, never resumed or refunded on a later cycle.
            state["days"] = {k: v for k, v in state["days"].items() if int(k) >= int(day) - 1}
            state["days"][day] = count + 1
            state["tokens"] = {k: v for k, v in state["tokens"].items() if now - _integer(v) <= DAY_MS}
            if len(state["tokens"]) > 200:
                raise ValueError("observer_token_state_cap")
            state["tokens"][token_key] = now
            state["last_target"] = token_key
            state["attempts"] = state["attempts"][-31:] + [attempt]
            state["updated_ms"] = now
            _save(self.state_path, self.root, state)
            report["attempt"] = attempt
            # Recheck eligibility after reservation I/O; a closed/expired attempt
            # cannot authorize transport merely because it was active earlier.
            current, _ = self._targets(self._now())
            if not same_target(target, current):
                attempt["status"] = "no_longer_active"
                report["status"] = "deferred"
                return report
            if target["kind"] == "pending" and not 30_000 <= self._now() - target["signal_ms"] <= 120_000:
                attempt["status"] = "entry_window_elapsed_during_validation"
                report["status"] = "deferred"
                return report
            self._wal_guard(state)
            from kaiba.core.db import close_thread_conn
            from kaiba.core.limiter import Priority
            if self.provider is None:
                from kaiba.providers import gmgn_cli
                provider = gmgn_cli
            else:
                provider = self.provider
            allocation_current(target)
            if target["kind"] == "pending" and not 30_000 <= self._now() - target["signal_ms"] <= 120_000:
                attempt["status"] = "entry_window_elapsed_during_validation"
                report["status"] = "deferred"
                return report
            self._wal_guard(state)
            report["provider_attempts"] = 1
            report["provider_attempts_basis"] = "typed_wrapper_invocations_not_network_calls"
            try:
                result = provider.token_info(target["token"], Chain(target["chain"]), priority=Priority.RESEARCH,
                    ttl_s=15, stale_grace_s=0, timeout_s=6, wait_for_slot_s=0)
                attempt["cache"] = self._verify_cache(target, result, self._now())
                attempt["status"] = "captured" if attempt["cache"]["verified"] else "unavailable"
            except Exception as exc:  # retain the admitted slot, never retry this attempt
                attempt["status"], attempt["failure_type"] = "unknown", type(exc).__name__
            finally:
                close_thread_conn()
            attempt["completed_ms"] = self._now()
            report["status"] = attempt["status"]
            return report
        except SourceDeferred as exc:
            report.update(status="deferred", reason=str(exc))
            return report
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
            report.update(status="partial", reason=type(exc).__name__ + ":" + str(exc)[:200])
            return report
        finally:
            report["completed_ms"] = self._now()
            state["updated_ms"] = max(state.get("updated_ms", 0), report["completed_ms"])
            report["daily_admitted_attempts"] = state.get("days", {}).get(str(state["updated_ms"] // DAY_MS), 0)
            _save(self.state_path, self.root, state)
            _save(self.state_dir / "latest-report.json", self.root, report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--research-dir", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--db", type=Path)
    args = parser.parse_args(argv)
    root = get_settings().kaiba_data_dir / "learning"
    report = ProspectiveQuoteCollector(args.research_dir or root / "autolearn",
        args.state_dir or root / "prospective-quotes", db_path=args.db).once()
    print(_json({k: report.get(k) for k in ("status", "provider_attempts", "reason", "funding_allowed")}))
    return 1 if report["status"] in ("partial", "unknown") else 0


if __name__ == "__main__":
    raise SystemExit(main())
