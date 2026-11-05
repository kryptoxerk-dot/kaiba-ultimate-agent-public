"""Bounded read-only operating-review capture, owned by Codex AGENT-UPGRADE-20261015.

Canonical sections have separate, explicit populations. Paper books and captured
JSON are read only inside the supplied research directory. No providers, migrations,
financial writers, or implicit application database connections are used.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kaiba.learning.autolearn_io import DAY_MS, GuardedReader, SourceDeferred

POSITION_LIMIT = 2000
ORDER_WINDOW = 2000
OPS_WINDOW = 800
BOOK_LIMIT = 18
JSON_LIMIT = 2 * 1024 * 1024


def _owned_json(root: Path, name: str) -> tuple[Any, str]:
    path = root / name
    try:
        if not path.exists():
            return None, "missing"
        if path.is_symlink() or path.resolve().parent != root or path.stat().st_size > JSON_LIMIT:
            raise SourceDeferred("research_json_path_or_size_refused")
        return json.loads(path.read_text(encoding="utf-8")), "captured"
    except (ValueError, OSError) as exc:
        raise SourceDeferred("research_json_unreadable") from exc


def _book(path: Path, *, now_ms: int) -> dict[str, Any]:
    local = GuardedReader(path, query_seconds=3, max_rows=201)
    identity_rows = local.rows("SELECT value FROM candidate_meta WHERE key='identity'")
    if len(identity_rows) != 1:
        raise SourceDeferred("research_book_identity_missing")
    try:
        identity = json.loads(identity_rows[0]["value"])
        if not identity.get("campaign_id") or not identity.get("hypothesis_id"):
            raise ValueError("identity")
    except (ValueError, TypeError, AttributeError) as exc:
        raise SourceDeferred("research_book_identity_invalid") from exc
    entries = local.rows("SELECT status,reason,COUNT(*) AS n FROM candidate_entries GROUP BY status,reason LIMIT 101")
    if len(entries) > 100:
        raise SourceDeferred("research_entry_reason_cap")
    orders = local.rows("SELECT state,COUNT(*) AS n FROM orders GROUP BY state LIMIT 30")
    positions = local.rows("SELECT (closed_ms IS NOT NULL) AS closed,COUNT(*) AS n FROM positions GROUP BY (closed_ms IS NOT NULL)")
    columns = {r["name"] for r in local.rows("PRAGMA table_info(candidate_entries)")}
    metadata = ("registered_ms", "last_checked_ms", "last_quote_reason", "expired_ms", "expiry_diagnostics_json")
    select = ",".join(name if name in columns else f"NULL AS {name}" for name in metadata)
    latest = local.rows(
        "SELECT signal_id,status,reason,quote_ms,latency_ms,decision_id,position_id,"
        "json_extract(payload,'$.signal_ms') AS signal_ms,json_extract(payload,'$.token') AS token," + select
        + " FROM candidate_entries ORDER BY json_extract(payload,'$.signal_ms') DESC,signal_id LIMIT 100"
    )
    return {"path": str(path), "identity": identity, "entries": entries, "orders": orders,
            "positions": positions, "closed": sum(int(r["n"]) for r in positions if r["closed"]),
            "open": sum(int(r["n"]) for r in positions if not r["closed"]), "latest": latest,
            "latest_rows_are_sample": True, "sample_limit": 100, "observed_ms": now_ms,
            "query_receipt": {"count": local.queries, "maximum_s": max((r["duration_s"] for r in local.receipts), default=0)}}


def collect_review(reader: GuardedReader, state_dir: Path | str, report: Mapping[str, Any], *, now_ms: int) -> dict[str, Any]:
    """Capture independent bounded sections; failures stay explicitly unknown.

    ``source_deferred`` requires the caller's normal >=60s source retry backoff.
    A source refusal stops subsequent canonical sections; owned research evidence
    can still be described without touching that source again.
    """
    import sqlite3

    from kaiba.learning.allocation_runtime import load_context

    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms <= 0:
        raise ValueError("review requires an integer millisecond frontier")
    if reader.query_seconds > 3:
        raise ValueError("review source reader must have a query deadline <=3s")
    root = Path(state_dir).resolve()
    start = now_ms - 12 * DAY_MS
    packet: dict[str, Any] = {"at_ms": now_ms, "closed_live": None, "cost_coverage": None,
                              "ops_runs": None, "books": None, "report": dict(report),
                              "section_status": {}, "source_deferred": False,
                              "scope": {"start_ms": start, "end_ms": now_ms, "row_limit": POSITION_LIMIT,
                                        "complete": False, "truncated": None, "total_matching": None}}
    stopped = False

    def section(name, callback):
        nonlocal stopped
        if stopped:
            packet["section_status"][name] = {"status": "unknown", "reason": "source_admission_previously_deferred"}
            return None
        try:
            value = callback()
            packet["section_status"][name] = {"status": "captured"}
            return value
        except (SourceDeferred, ValueError, TypeError, KeyError) as exc:
            packet["source_deferred"] = True
            stopped = "wal_over_limit" in str(exc)
            packet["section_status"][name] = {"status": "unknown", "reason": str(exc)}
            return None

    positions = section("closed_live", lambda: reader.rows(
        "SELECT position_id,chain,token,lane,mode,opened_ms,closed_ms,qty,cost_native,proceeds_native,realized_native,exit_reason "
        "FROM positions WHERE mode='live' AND closed_ms>=? AND closed_ms<=? "
        "ORDER BY closed_ms DESC,position_id LIMIT ?", (start, now_ms, POSITION_LIMIT + 1)))
    if positions is not None:
        packet["closed_live"] = positions[:POSITION_LIMIT]
        truncated = len(positions) > POSITION_LIMIT
        packet["scope"].update({"complete": not truncated, "truncated": truncated,
                                  "total_matching": None if truncated else len(positions),
                                  "total_matching_lower_bound": len(positions)})

    def costs():
        frontier = int(reader.q("SELECT COALESCE(MAX(rowid),0) FROM orders")[0][0])
        lower = max(1, frontier - ORDER_WINDOW + 1)
        packet["cost_coverage_scope"] = {"start_ms": start, "end_ms": now_ms,
            "rowid_min": lower, "rowid_max": frontier, "row_limit": ORDER_WINDOW,
            "basis": "recent order-rowid window, filtered to filled live orders in period",
            "entire_period_population": False}
        return reader.rows(
            "SELECT o.chain,o.mode,COUNT(*) AS filled_orders,"
            "SUM(CASE WHEN fc.order_id IS NOT NULL THEN 1 ELSE 0 END) AS fill_cost_orders,"
            "SUM(CASE WHEN fc.reconciled=1 THEN 1 ELSE 0 END) AS reconciled_orders "
            "FROM orders o LEFT JOIN fill_costs fc ON fc.order_id=o.order_id "
            "WHERE o.rowid BETWEEN ? AND ? AND o.state='filled' AND o.mode='live' "
            "AND o.created_ms>=? AND o.created_ms<=? GROUP BY o.chain,o.mode", (lower, frontier, start, now_ms))

    packet["cost_coverage"] = section("cost_coverage", costs)

    def operations():
        frontier = int(reader.q("SELECT COALESCE(MAX(rowid),0) FROM ops_runs")[0][0])
        packet["ops_scope"] = {"rowid_min": max(1, frontier - OPS_WINDOW + 1), "rowid_max": frontier,
                                "row_limit": OPS_WINDOW, "basis": "recent rowid window"}
        return reader.rows("SELECT rowid AS source_rowid,job,status,started_ms,duration_ms FROM ops_runs "
                           "WHERE rowid BETWEEN ? AND ? AND started_ms<=? ORDER BY rowid DESC",
                           (max(1, frontier - OPS_WINDOW + 1), frontier, now_ms))

    packet["ops_runs"] = section("ops_runs", operations)
    paper_root = (root / "paper").resolve()
    books, book_errors, retired = [], [], []
    active, expected_identities = set(), {}
    active_paths: set[Path] = set()
    allocation, allocation_failed = None, False
    try:
        allocation = load_context(root, learning_root=root.parent, now_ms=now_ms)
        if allocation is not None:
            pairs = allocation.leased_pairs()
            active = {p["manifest"]["campaign_id"] for p in pairs}
            active_paths = {Path(p["book_path"]) for p in pairs}
            expected_identities = {Path(p["book_path"]): p["identity"] for p in pairs}
            retired = [{**record, "path": Path(record["book_path"]).name,
                        "classification": "allocation_retired_history_read_only", "allocated": False,
                        "order_count": record["inspection"].get("orders")}
                       for record in allocation.retired_history()]
            packet["allocation_scope"] = {"status": "validated", "snapshot_token": allocation.snapshot_token,
                "active_leases": len(pairs), "retired_history_books": len(retired),
                "lifetime_family_size": sum(len(m["hypotheses"]) for m in allocation.manifests()),
                "book_paths_basis": "validated_allocation_leases", "funding_allowed": False}
    except (SourceDeferred, ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
        allocation_failed = True
        packet["allocation_scope"] = {"status": "unknown", "funding_allowed": False}
        book_errors.append({"reason": str(exc)})
    if allocation is None and not allocation_failed:
        active = {str(c["campaign_id"]) for c in report.get("campaigns", []) if c.get("campaign_id")}
        for campaign in report.get("campaigns", []):
            for arm in campaign.get("paper", {}).get("arms", []):
                if arm.get("book_path"):
                    active_paths.add(Path(arm["book_path"]))
        if not active_paths:
            try:
                state, _ = _owned_json(root, "state.json")
                if isinstance(state, Mapping):
                    from kaiba.learning.candidate_paper import CandidatePaperRunner
                    runner = CandidatePaperRunner(root / "paper", learning_root=root)
                    for name in state.get("campaign_files", []):
                        if isinstance(name, str) and Path(name).name == name and Path(name).suffix == ".json":
                            manifest, _ = _owned_json(root, name)
                            if isinstance(manifest, Mapping) and manifest.get("campaign_id"):
                                cid = str(manifest["campaign_id"])
                                active.add(cid)
                                for arm in manifest.get("hypotheses", []):
                                    active_paths.add(runner.book_path(cid, str(arm["hypothesis_id"])))
            except (SourceDeferred, ValueError, TypeError, KeyError, OSError) as exc:
                book_errors.append({"reason": str(exc)})
    if allocation_failed:
        pass  # Either allocation latch forbids fallback to report paths or old manifests.
    elif paper_root.parent != root:
        book_errors.append({"reason": "paper_directory_escapes_research_state"})
    elif paper_root.is_dir():
        all_paths = set(paper_root.glob("candidate-*.db"))
        paths = sorted(active_paths)
        if len(paths) > BOOK_LIMIT:
            book_errors.append({"reason": "active_research_book_cap", "found": len(paths), "limit": BOOK_LIMIT})
        if allocation is not None:
            pinned = {Path(record["book_path"]) for record in retired}
            retired.extend({"path": p.name, "classification": "outside_current_allocation_unknown_history",
                            "order_count": None, "allocated": False}
                           for p in sorted(all_paths - active_paths - pinned))
        else:
            retired = [{"path": p.name, "classification": "not_in_current_campaign_manifest" if active else "active_campaign_unknown",
                        "order_count": None} for p in sorted(all_paths - active_paths)]
        for path in paths[:BOOK_LIMIT]:
            try:
                if (path.is_symlink() or path.resolve().parent != paper_root
                        or (reader.db_path.exists() and path.samefile(reader.db_path))):
                    raise SourceDeferred("research_book_path_refused")
                captured = _book(path, now_ms=now_ms)
                if allocation is not None and captured["identity"] != expected_identities[path]:
                    raise SourceDeferred("allocated_book_frozen_identity_mismatch")
                if captured["identity"]["campaign_id"] in active:
                    books.append(captured)
                else:
                    raise SourceDeferred("active_book_campaign_identity_mismatch")
            except (SourceDeferred, ValueError, TypeError, KeyError, OSError) as exc:
                book_errors.append({"path": path.name, "reason": str(exc)})
    elif active_paths:
        book_errors.append({"reason": "active_research_book_directory_missing"})
    if allocation is not None:
        try:
            allocation.revalidate(now_ms=now_ms)
        except (SourceDeferred, ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
            books, retired = [], []
            packet["allocation_scope"]["status"] = "unknown"
            book_errors.append({"reason": str(exc)})
    packet["books"] = books
    packet["retired_books"] = retired
    if retired and not active:
        book_errors.append({"reason": "active_campaign_identity_unknown"})
    packet["section_status"]["paper_books"] = {"status": "partial" if book_errors else "captured", "errors": book_errors}
    for key, name in (("wallet_review", "latest-wallet-review.json"), ("discovery_result", "latest-discovery.json")):
        try:
            value, status = _owned_json(root, name)
            packet[key] = value
            packet["section_status"][key] = {"status": status}
        except SourceDeferred as exc:
            packet[key] = None
            packet["section_status"][key] = {"status": "unknown", "reason": str(exc)}
    for key in ("paper_funnel", "quote_diagnostics", "service_health", "services"):
        if key in report:
            packet[key] = report[key]
    return packet
