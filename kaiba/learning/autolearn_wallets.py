"""Bounded runner-cache review and missing-only research intake; no provider calls.

Codex AGENT-UPGRADE-20261015. Existing wallet identities are never updated. Cache
mtime describes the file, while each provider observation keeps its original _at.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
import time
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kaiba.core.schemas import Chain
from kaiba.intelligence import grade
from kaiba.intelligence.import_wallets import import_records
from kaiba.intelligence.wallet_campaign import key
from kaiba.intelligence.wallet_candidates import _behavior, review_candidates
from kaiba.learning.autolearn_behavior import collect_behavior
from kaiba.learning.autolearn_io import GuardedReader, SourceDeferred

VERSION = "autolearn_wallets/1"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 48 * 1024 * 1024
MAX_ROWS = 2_000
DAILY_INTAKE = 20
HISTORY_QUERY_IDENTITIES = 20
REGISTRY_QUERY_IDENTITIES = 20
ACQUISITION_VERSION = "wallet-acquisition/1"
ACQUISITION_CAP = 20
ACQUISITION_BYTES = 32 * 1024
DAY_MS = 86_400_000
ACQUISITION_FROZEN_FIELDS = ("chain", "address", "selected_ms", "evidence_id", "selection_hash",
                             "nominations_hash", "nominations")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _acquisition_bytes(value: Any) -> int:
    raw = json.dumps(value, indent=2, sort_keys=True, allow_nan=False).encode()
    return len(raw) + 16 * raw.count(b"\n")  # Reserve indentation inside the calling worker's state.


def _acquisition_sources() -> dict:
    return {"adapter": hashlib.sha256(Path(__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
            "reviewer": hashlib.sha256(Path(review_candidates.__code__.co_filename).read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
            "identity": hashlib.sha256(Path(key.__code__.co_filename).read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
            "grade_tags": hashlib.sha256(Path(grade.__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest()}


def _acquisition_state(raw: Any) -> tuple[dict, str | None]:
    sources = _acquisition_sources()
    empty = {"version": ACQUISITION_VERSION, "source_hashes": sources, "round": 0, "pending": []}
    if raw is None:
        return empty, None
    reason = None
    try:
        if (not isinstance(raw, Mapping) or raw.get("version") != ACQUISITION_VERSION
                or _acquisition_bytes(raw) > ACQUISITION_BYTES
                or raw.get("status") == "unknown"):
            raise ValueError("invalid_state")
        if raw.get("source_hashes") != sources:
            raise ValueError("source_changed")
        pending = raw["pending"]
        if (not isinstance(pending, list) or len(pending) > ACQUISITION_CAP
                or type(raw.get("round")) is not int or raw["round"] < 0):
            raise ValueError("invalid_state")
        seen = set()
        for row in pending:
            identity = key(row.get("chain"), row.get("address"))
            if (identity is None or identity[0] != "sol" or identity in seen
                    or type(row.get("selected_ms")) is not int or row["selected_ms"] <= 0
                    or not isinstance(row.get("evidence_id"), str) or not row["evidence_id"].startswith("wallet-observe:")
                    or not isinstance(row.get("selection_hash"), str) or len(row["selection_hash"]) != 64
                    or not isinstance(row.get("nominations_hash"), str) or len(row["nominations_hash"]) != 64
                    or not isinstance(row.get("nominations"), list) or len(row["nominations"]) > 4
                    or "baseline_pages" not in row
                    or row.get("frozen_hash") != _hash({k: row.get(k) for k in ACQUISITION_FROZEN_FIELDS})
                    or row.get("baseline_pages") is not None and
                    (type(row["baseline_pages"]) is not int or row["baseline_pages"] < 0)):
                raise ValueError("invalid_state")
            for receipt in row["nominations"]:
                if (not isinstance(receipt, dict) or type(receipt.get("observed_ms")) is not int
                        or receipt["observed_ms"] <= 0 or not isinstance(receipt.get("receipt_hash"), str)
                        or len(receipt["receipt_hash"]) != 64):
                    raise ValueError("invalid_state")
            seen.add(identity)
        return dict(raw) | {"pending": [dict(r) for r in pending]}, None
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        reason = str(exc) if str(exc) in {"invalid_state", "source_changed"} else "invalid_state"
    if isinstance(raw, Mapping):
        try:
            if _acquisition_bytes(raw) <= ACQUISITION_BYTES:
                return dict(raw), reason  # Explicit migration/repair; never silently erase frozen selections.
        except (ValueError, TypeError):
            pass
    return {"version": ACQUISITION_VERSION, "status": "unknown", "reason": reason,
            "prior_state_hash": _hash(raw)}, reason


def _acquisition_snapshot(reader: GuardedReader, pending: list[dict]) -> dict:
    """Current exact identities only; never a wallet-history scan or provider request."""
    if not pending:
        return {"wallets": {}, "scores": {}, "cursors": {}, "history": [], "exclusions": []}
    addresses = [r["address"] for r in pending]
    if len(addresses) > ACQUISITION_CAP:
        raise SourceDeferred("acquisition_identity_cap")
    marks = ",".join("?" * len(addresses))
    args = ("sol", *addresses)
    wallets = _registry_rows(reader, "acquisition_wallets", "sol", 0,
        "SELECT chain,address,cohort,CASE WHEN length(name)<=512 THEN name END AS name,"
        "CASE WHEN length(source)<=512 THEN source END AS source,length(name) AS name_chars,"
        "length(source) AS source_chars,CASE WHEN length(tags_json)<=65536 THEN tags_json END AS tags_json,"
        "CASE WHEN length(meta_json)<=65536 THEN meta_json END AS meta_json "
        f"FROM wallets WHERE chain=? AND address IN ({marks})", args)
    for row in wallets:
        if (row["tags_json"] is None or row["meta_json"] is None
                or (row["name_chars"] or 0) > 512 or (row["source_chars"] or 0) > 512):
            raise SourceDeferred("acquisition_registry_field_cap")
        meta = json.loads(row.get("meta_json") or "{}")
        if not isinstance(meta, dict):
            raise SourceDeferred("acquisition_registry_metadata_invalid")
        row["is_owner"] = bool(meta.get("is_owner"))
    tags = _registry_rows(reader, "acquisition_tags", "sol", 0,
                          f"SELECT chain,address,tag,source FROM wallet_feed_tags "
                          f"WHERE chain=? AND address IN ({marks}) LIMIT 2001", args)
    if len(tags) > 2000:
        raise SourceDeferred("acquisition_tag_cap")
    tag_counts = defaultdict(int)
    for row in tags:
        tag_counts[row["address"]] += 1
    if any(n > 500 for n in tag_counts.values()):
        raise SourceDeferred("acquisition_identity_tag_cap")
    hubs = _registry_rows(reader, "acquisition_hubs", "sol", 0,
                          f"SELECT chain,address FROM hub_addresses WHERE chain=? AND address IN ({marks})", args)
    scores = _registry_rows(reader, "acquisition_scores", "sol", 0,
                            f"SELECT chain,address,grade,model_version FROM wallet_scores "
                            f"WHERE chain=? AND address IN ({marks})", args)
    cursor_keys = tuple("backfill:swaps:sol:" + address for address in addresses)
    cursors = _registry_rows(reader, "acquisition_cursors", "sol", 0,
        f"SELECT key,CASE WHEN length(value)<=8192 THEN value END AS value,updated_ms "
        f"FROM kv WHERE key IN ({marks})", cursor_keys)
    parsed = {}
    for row in cursors:
        value = json.loads(row["value"])
        if (not isinstance(value, dict) or type(value.get("pages", 0)) is not int
                or value.get("pages", 0) < 0 or type(row["updated_ms"]) is not int):
            raise SourceDeferred("acquisition_cursor_invalid")
        parsed[row["key"].removeprefix("backfill:swaps:sol:")] = {"pages": value.get("pages", 0),
                                                               "updated_ms": row["updated_ms"]}
    return {"wallets": {r["address"]: r for r in wallets}, "scores": {r["address"]: r for r in scores},
            "cursors": parsed, "history": [*wallets, *({**r, "tags_json": json.dumps([r["tag"]])} for r in tags)],
            "exclusions": [{**r, "reason": "existing_hub_address"} for r in hubs]}


def _acquisitions(reader: GuardedReader, review: dict, packet: dict, prior: Any, *, now: int,
                  started: float | None = None) -> dict:
    """Preserve frozen nominations separately; no followthrough or copy approval."""
    began = time.monotonic() if started is None else started
    state, error = _acquisition_state(prior)
    if error:
        return {"status": "unknown", "reason": error, "queue": [], "candidates": [],
                "carry_missing": [], "next_state": state}
    pending, expired, omitted, invalid_nominations = [], [], 0, []
    for row in state["pending"]:
        if not 0 <= now - row["selected_ms"] <= 13 * DAY_MS:
            expired.append({"identity_hash": _hash([row["chain"], row["address"]]),
                            "selection_hash": row["selection_hash"], "reason": "selection_expired_or_future"})
        else:
            pending.append(row)
    seen = {r["address"] for r in pending}
    current_wallets = {r["address"]: r for r in packet["history"]
                       if r.get("chain") == "sol" and "meta_json" in r}
    candidates = {(r["chain"], r["address"]): r for r in review["candidates"]}
    for selected in review["observation_queue"]:
        if selected["chain"] != "sol" or selected["address"] in seen:
            continue
        wallet = current_wallets.get(selected["address"])
        candidate = candidates[("sol", selected["address"])]
        if candidate["exclusions"] or wallet is not None and wallet.get("cohort") != "research":
            continue  # Existing cohortNULL/operator identities are never enrolled or changed.
        receipts = candidate["nominations"]
        if not receipts or any(type(r.get("observed_ms")) is not int
                               or not 0 < r["observed_ms"] <= now for r in receipts[:4]):
            invalid_nominations.append({"identity_hash": _hash(["sol", selected["address"]]),
                                        "selection_hash": _hash(selected), "reason": "nomination_clock_unknown_or_future"})
            continue
        row = {"chain": "sol", "address": selected["address"], "selected_ms": now,
               "evidence_id": selected["evidence_id"], "selection_hash": _hash(selected),
               "nominations_hash": _hash(receipts), "nominations": [
                   {"source": str(r.get("source") or "")[:80], "observed_ms": r.get("observed_ms"),
                    "receipt_hash": _hash(r)} for r in receipts[:4]], "baseline_pages": None}
        row["frozen_hash"] = _hash({k: row[k] for k in ACQUISITION_FROZEN_FIELDS})
        trial_state = {**state, "pending": [*pending, row]}
        if len(pending) >= ACQUISITION_CAP or _acquisition_bytes(trial_state) > ACQUISITION_BYTES:
            omitted += 1
            continue
        pending.append(row)
        seen.add(row["address"])
    state = {**state, "pending": pending}
    try:
        snapshot = _acquisition_snapshot(reader, pending)
        checked = now + max(0, int((time.monotonic() - began) * 1000))
        if any(not 0 < cursor["updated_ms"] <= checked for cursor in snapshot["cursors"].values()):
            raise SourceDeferred("acquisition_cursor_clock_unknown_or_future")
        # Separate pure review: controls carry the ORIGINAL selection clock. It is
        # never supplied to the main queue or to collect_behavior.
        subreview = review_candidates({"controls": [{"chain": r["chain"], "address": r["address"],
            "source": "frozen_acquisition_nomination", "observed_ms": r["selected_ms"]} for r in pending],
            "history": snapshot["history"], "exclusions": snapshot["exclusions"]},
            now_ms=now, budget=0, exploration_slots=0)
    except (SourceDeferred, ValueError, TypeError) as exc:
        if isinstance(exc, SourceDeferred) and "wal_over_limit" in str(exc):
            raise  # Observed WAL refusal requires the global>=60s pause even if it shrinks next query.
        return {"status": "unknown", "reason": "acquisition_registry_unavailable:" + type(exc).__name__,
                "queue": [], "candidates": [], "carry_missing": [], "next_state": state,
                "retry_after_ms": now + 60_000, "expired": expired, "omitted_capacity": omitted,
                "invalid_nominations": invalid_nominations}
    by_address = {r["address"]: r for r in subreview["candidates"]}
    queue, carry, summaries, retained, completed = [], [], [], [], []
    rotation = state["round"] % max(1, len(pending))
    for row in pending[rotation:] + pending[:rotation]:
        address = row["address"]
        candidate = by_address[address]
        wallet, score, cursor = (snapshot[k].get(address) for k in ("wallets", "scores", "cursors"))
        if row["baseline_pages"] is None:
            row["baseline_pages"] = cursor["pages"] if cursor else 0
        if cursor and cursor["pages"] > row["baseline_pages"] and cursor["updated_ms"] >= row["selected_ms"]:
            completed.append({"identity_hash": _hash(["sol", address]), "selection_hash": row["selection_hash"],
                              "reason": "backfill_page_observed_not_full_history_or_profit_proof"})
            continue
        reasons = list(candidate["exclusions"])
        if wallet is not None and wallet.get("cohort") != "research":
            reasons.append("current_registry_not_research")
        if score and (score["grade"] not in {"C", "D", "UNSCORED"} or score["model_version"] != grade.MODEL_ID_TAPE):
            reasons.append("outside_existing_grade10_score_eligibility")
        if cursor and cursor["updated_ms"] >= now - DAY_MS:
            reasons.append("backfill_cursor_not_due_24h")
        summary = {"chain": "sol", "address": address, "selected_ms": row["selected_ms"],
                   "selection_hash": row["selection_hash"], "exclusions": reasons,
                   "registry_checked_ms": checked, "registry_present": wallet is not None,
                   "cohort": wallet.get("cohort") if wallet else None}
        summaries.append(summary)
        if not reasons:
            if wallet is None:
                carry.append({"chain": "sol", "address": address})
                summary["status"] = "awaiting_existing_intake_capacity"
            else:
                summary["status"] = "manual_probe_required"
                queue.append({"chain": "sol", "address": address, "selected_ms": row["selected_ms"],
                    "evidence_id": row["evidence_id"], "selection_hash": row["selection_hash"],
                    "nominations_hash": row["nominations_hash"], "nominations": row["nominations"],
                    "registry_checked_ms": checked, "route": "existing_budgeted_history_observation",
                    "provider_calls_authorized": 0, "roster_change_authorized": False})
        else:
            summary["status"] = "blocked"
        retained.append(row)
    next_state = {**state, "round": state["round"] + 1, "pending": retained}
    # Adding a small baseline counter must not silently exceed the persisted cap.
    if _acquisition_bytes(next_state) > ACQUISITION_BYTES:
        return {"status": "unknown", "reason": "acquisition_state_byte_cap", "queue": [],
                "candidates": [], "carry_missing": [], "next_state": state}
    return {"status": "complete", "queue": queue, "candidates": summaries, "carry_missing": carry,
            "next_state": next_state, "expired": expired, "completed": completed, "omitted_capacity": omitted,
            "invalid_nominations": invalid_nominations,
            "registry_checked_ms": checked, "full_history_proven": False, "provider_calls": 0}


def _read(path: Path, root: Path, receipts: list, budget: list[int]) -> Any:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("runner cache escapes supplied root")
    stat = resolved.stat()
    if stat.st_size > MAX_FILE_BYTES or budget[0] + stat.st_size > MAX_TOTAL_BYTES:
        receipts.append({"path": str(path), "status": "file_budget_exceeded", "bytes": stat.st_size})
        return None
    with resolved.open("rb") as handle:
        raw = handle.read(min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - budget[0]) + 1)
    if len(raw) > MAX_FILE_BYTES or len(raw) + budget[0] > MAX_TOTAL_BYTES:
        raise ValueError("runner file changed beyond bounded read")
    budget[0] += len(raw)
    receipts.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                     "observed_ms": int(stat.st_mtime * 1000), "bytes": len(raw), "status": "read"})
    return json.loads(raw)


def _chain(path: Path) -> str | None:
    name = path.name.lower()
    return next((chain for marker, chain in (("kaiba-bnb-", "bsc"), ("kaiba-bsc-", "bsc"),
                 ("kaiba-solana-", "sol"), ("kaiba-sol-", "sol"), ("kaiba-robinhood-", "robinhood"),
                 ("kaiba-eth-", "eth"), ("kaiba-base-", "base"))
                 if name.startswith(marker)), None)


def _registry_rows(reader: GuardedReader, table: str, chain: str, begin: int,
                   sql: str, args: tuple) -> list[dict]:
    try:
        return reader.rows(sql, args)
    except SourceDeferred as exc:
        raise SourceDeferred(f"wallet_registry_deferred:table={table};chain={chain};batch_offset={begin};{exc}") from exc


def _recent_history(reader: GuardedReader, chain: str, addresses: list[str], *, batch_offset: int = 0) -> list[dict]:
    """Indexed latest-two reads in fresh groups of20; no partial history is returned."""
    if not addresses:
        return []
    if len(addresses) > 100:
        raise ValueError("wallet history batch exceeds 100 identities")
    rows = []
    for begin in range(0, len(addresses), HISTORY_QUERY_IDENTITIES):
        batch = addresses[begin:begin + HISTORY_QUERY_IDENTITIES]
        requested = ",".join("(?,?)" for _ in batch)
        args = tuple(value for address in batch for value in (chain, address))
        rows.extend(_registry_rows(reader, "wallet_score_history", chain, batch_offset + begin,
            f"WITH requested(chain,address) AS (VALUES {requested}) "
            "SELECT h.chain,h.address,h.score,h.grade,h.scored_at_ms,h.model_version "
            "FROM requested AS r JOIN wallet_score_history AS h ON h.id IN "
            "(SELECT x.id FROM wallet_score_history AS x INDEXED BY idx_wsh "
            "WHERE x.chain=r.chain AND x.address=r.address "
            "ORDER BY x.scored_at_ms DESC,x.id DESC LIMIT 2)", args))
    return rows


def _packet(reader: GuardedReader, root: Path, now: int, prior: Mapping[str, Any]) -> dict:
    receipts, budget, exports = [], [0], []
    directory = root / "exports"
    files = sorted((p for p in directory.iterdir() if p.suffix in (".txt", ".json") and _chain(p)),
                   key=lambda p: p.name)[:24] if directory.is_dir() else []
    allowance = max(1, MAX_ROWS // max(1, len(files)))
    rotation = int(prior.get("file_round", 0))
    for path in files:
        try:
            data = _read(path, root, receipts, budget)
        except (OSError, ValueError, TypeError) as exc:
            receipts.append({"path": str(path), "status": "unavailable:" + type(exc).__name__})
            continue
        rows = data.get("list", data.get("wallets", [])) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            continue
        offset = rotation * allowance % len(rows) if rows else 0
        rows = (rows[offset:] + rows[:offset])[:allowance]
        receipt = receipts[-1]
        exports.append({"chain": _chain(path), "source": "runner_cache_export", "rows": rows,
                        **{k: receipt[k] for k in ("path", "sha256", "observed_ms")}})
    identities = sorted({identity for export in exports for row in export["rows"] if isinstance(row, Mapping)
                         for identity in [key(export["chain"], row.get("address"))] if identity is not None})
    grouped = defaultdict(list)
    for chain, address in identities:
        grouped[chain].append(address)
    profiles, history, entities, exclusions, present = [], [], [], [], set()
    for chain, addresses in sorted(grouped.items()):
        for profile_name in ("p30", "ht_stats"):
            path = root / chain / (profile_name + ".json")
            if not path.is_file():
                continue
            try:
                data = _read(path, root, receipts, budget)
            except (OSError, ValueError, TypeError) as exc:
                receipts.append({"path": str(path), "status": "unavailable:" + type(exc).__name__})
                continue
            if isinstance(data, dict):
                for address in addresses:
                    row = data.get(address)
                    if isinstance(row, dict):
                        profiles.append({"chain": chain, "address": address, "source": profile_name,
                                         "data": row})  # original _at, never mtime
        tags_in_original_batch = 0
        for begin in range(0, len(addresses), REGISTRY_QUERY_IDENTITIES):
            batch = addresses[begin:begin + REGISTRY_QUERY_IDENTITIES]
            if begin % 100 == 0:
                tags_in_original_batch = 0
            marks = ",".join("?" * len(batch))
            args = (chain, *batch)
            rows = _registry_rows(reader, "wallets", chain, begin,
                                  f"SELECT * FROM wallets WHERE chain=? AND address IN ({marks})", args)
            for row in rows:
                present.add((row["chain"], row["address"]))
                meta = json.loads(row.get("meta_json") or "{}")
                row["is_owner"] = isinstance(meta, dict) and meta.get("is_owner") is True
            history.extend(rows)
            history.extend(_recent_history(reader, chain, batch, batch_offset=begin))
            tags = _registry_rows(reader, "wallet_feed_tags", chain, begin,
                                  f"SELECT chain,address,tag,source FROM wallet_feed_tags "
                                  f"WHERE chain=? AND address IN ({marks}) LIMIT 2001", args)
            tags_in_original_batch += len(tags)
            if tags_in_original_batch > 2000:
                raise SourceDeferred(f"wallet_registry_deferred:table=wallet_feed_tags;chain={chain};"
                                     f"batch_offset={begin};wallet_tag_history_exceeds_bounded_review")
            history.extend({**r, "tags_json": json.dumps([r["tag"]])} for r in tags)
            entities.extend(_registry_rows(reader, "entity_members", chain, begin,
                                            f"SELECT chain,address,entity_id FROM entity_members "
                                            f"WHERE chain=? AND address IN ({marks})", args))
            exclusions.extend({**r, "reason": "existing_hub_address"} for r in _registry_rows(
                reader, "hub_addresses", chain, begin,
                f"SELECT chain,address FROM hub_addresses WHERE chain=? AND address IN ({marks})", args))
    return {"exports": exports, "profiles": profiles, "history": history, "entities": entities,
            "exclusions": exclusions, "prior_state": prior.get("review_state", {}),
            "coverage": {"files": receipts, "source_bytes": budget[0], "nomination_cap": MAX_ROWS,
                         "registry_complete_for_selected_identities": True}, "present": present}


def _intake(reader: GuardedReader, records: list[dict], *, day: str, prior_count: int) -> dict:
    reader.guard()
    conn = sqlite3.connect(reader.db_path, isolation_level=None, timeout=.25)
    conn.row_factory = sqlite3.Row
    started = time.monotonic()
    conn.set_progress_handler(lambda: int(time.monotonic() - started >= 3), 1000)
    added, existing = [], []
    counter_key = "autolearn:wallet-intake:" + day
    try:
        conn.execute("BEGIN IMMEDIATE")
        stored = conn.execute("SELECT value FROM kv WHERE key=?", (counter_key,)).fetchone()
        count = max(prior_count, int(json.loads(stored[0]).get("count", 0)) if stored else 0)
        for row in records:
            if count >= DAILY_INTAKE:
                break
            if time.monotonic() - started >= 3:
                raise SourceDeferred("wallet_intake_transaction_deadline")
            if conn.execute("SELECT 1 FROM wallets WHERE chain=? AND address=?", (row["chain"], row["address"])).fetchone():
                existing.append({"chain": row["chain"], "address": row["address"]})
                continue
            rep = import_records([{"address": row["address"], "name": ""}],
                                 source="codex-autolearn-research", default_evm_chain=Chain(row["chain"]), conn=conn)
            if rep.imported:
                count += 1
                added.append({"chain": row["chain"], "address": row["address"], "cohort": "research"})
        conn.execute("INSERT INTO kv(key,value,updated_ms) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE "
                     "SET value=excluded.value,updated_ms=excluded.updated_ms",
                     (counter_key, json.dumps({"version": VERSION, "count": count}), int(time.time() * 1000)))
        conn.commit()
        return {"added": added, "existing_preserved": existing, "daily_count": count,
                "daily_limit": DAILY_INTAKE, "provider_calls": 0, "roster_changes": 0}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_wallet_review(reader: GuardedReader, runner_root: Path | str, *, now_ms: int,
                      prior_state: Mapping[str, Any] | None = None) -> dict:
    """Review bounded original caches, queue20/five exploration, insert only absent wallets.

    A canonical transactional daily counter protects against lost caller state and
    concurrent runs. All registry sections must succeed before absence can authorize
    intake. Existing labels, source, grades, trust and rosters remain untouched.
    """
    from kaiba.learning.fixed_wallet_study import collect_fixed_study
    prior = dict(prior_state or {})
    review_started = time.monotonic()
    day = datetime.datetime.fromtimestamp(now_ms / 1000, datetime.UTC).date().isoformat()
    count = int(prior.get("daily_count", 0)) if prior.get("day") == day else 0
    packet = _packet(reader, Path(runner_root).resolve(), now_ms, prior)
    present = packet.pop("present")
    review = review_candidates(packet, now_ms=now_ms, budget=20, exploration_slots=5)
    acquisitions = _acquisitions(reader, review, packet, prior.get("acquisition_state"),
                                 now=now_ms, started=review_started)
    study_now_ms = max(now_ms, int(time.time() * 1000))
    study = collect_fixed_study(reader, review["observation_queue"], now_ms=study_now_ms,
                                dynamic_state=prior.get("behavior_state"),
                                study_state=prior.get("fixed_study_state"), collector=collect_behavior)
    behavior = study["behavior"]
    # The queue and intake are already frozen. Descriptive teacher evidence must not
    # cause a second selection or alter which identities receive observation slots.
    reports = {(r["chain"], r["wallet"]): r for r in behavior["behavior_reports"]}
    for candidate in review["candidates"]:
        identity = (candidate["chain"], candidate["address"])
        if identity in reports:
            evidence = _behavior(reports[identity], identity, study_now_ms)
            candidate["behavior_evidence"].append(evidence)
            if evidence["inspectable_episodes"] and "BEHAVIOR_TEACHER" not in candidate["roles"]:
                candidate["roles"].append("BEHAVIOR_TEACHER")
    review["coverage"]["behavior_teacher_count"] = sum("BEHAVIOR_TEACHER" in c["roles"]
                                                      for c in review["candidates"])
    review["behavior_learning"] = {k: v for k, v in behavior.items() if k != "next_state"}
    review["acquisition_queue"] = acquisitions["queue"]
    review["acquisition_candidates"] = acquisitions["candidates"]
    review["acquisition_learning"] = {k: v for k, v in acquisitions.items()
                                      if k not in {"queue", "candidates", "carry_missing", "next_state"}}
    review["acquisition_learning"].update(pending_count=len(acquisitions["next_state"].get("pending", [])),
                                         identity_cap=ACQUISITION_CAP, state_byte_cap=ACQUISITION_BYTES)
    # History/tag rows can mark an identity known to the pure reviewer. Only the
    # exact current wallet registry determines whether a research import is missing.
    current_missing = [r for r in review["observation_queue"] if (r["chain"], r["address"]) not in present]
    if acquisitions["status"] == "unknown":
        # No SOL intake can be inferred safe from an unknown acquisition state/snapshot.
        current_missing = [r for r in current_missing if r["chain"] != "sol"]
    missing = list({(r["chain"], r["address"]): r
                    for r in [*acquisitions["carry_missing"], *current_missing]}.values())
    intake = _intake(reader, missing, day=day, prior_count=count)
    return {"review": review, "intake": intake, "funding_allowed": False,
            "next_state": {"version": VERSION, "day": day, "daily_count": intake["daily_count"],
                           "file_round": int(prior.get("file_round", 0)) + 1,
                           "review_state": review["next_state"], "behavior_state": study["dynamic_next_state"],
                           "fixed_study_state": study["study_next_state"],
                           "acquisition_state": acquisitions["next_state"]}}
