"""Cached nominations cannot overwrite trusted wallets or spend provider credits."""
from __future__ import annotations

import datetime
import io
import json
import os
from pathlib import Path

import pytest

from kaiba.learning import autolearn_wallets as aw
from kaiba.learning.autolearn_io import GuardedReader, SourceDeferred

NOW = 1_800_000_000_000
DAY = 86_400_000
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz123456789"


def _sol(index):
    return "A" * 30 + ALPHABET[index // len(ALPHABET)] + ALPHABET[index % len(ALPHABET)]


def _path(conn):
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


def _write(path, value, stamp=NOW):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    os.utime(path, (stamp / 1000, stamp / 1000))


def _caches(root, n=1, *, profile_at=NOW, chain="sol"):
    addresses = [_sol(i) if chain == "sol" else f"0x{i + 1:040x}" for i in range(n)]
    marker = {"sol": "solana", "bsc": "bnb", "robinhood": "robinhood"}[chain]
    _write(root / "exports" / f"kaiba-{marker}-snipers-early-runners.txt",
           [{"address": a, "name": "TOP $999k 99%WR", "groups": ["default"]} for a in addresses])
    _write(root / chain / "p30.json", {a: {"_at": profile_at // 1000, "realized_profit": 1000, "buy": 20}
                                        for a in addresses})
    return addresses


def _wallet(conn, address, *, cohort="trusted_copy", source="owner_curated", name="My trusted wallet"):
    conn.execute("INSERT INTO wallets(chain,address,name,source,tags_json,first_seen_ms,last_seen_ms,cohort,meta_json) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", ("sol", address, name, source, '["kol"]', 1, 2, cohort, '{"keep":1}'))


def test_missing_only_intake_preserves_existing_trust_name_source_tags_and_metadata(tmp_db, tmp_path):
    root = tmp_path / "runners"
    addresses = _caches(root, 2)
    _wallet(tmp_db, addresses[0])
    before = dict(tmp_db.execute("SELECT * FROM wallets WHERE address=?", (addresses[0],)).fetchone())
    result = aw.run_wallet_review(GuardedReader(_path(tmp_db)), root, now_ms=NOW)
    after = dict(tmp_db.execute("SELECT * FROM wallets WHERE address=?", (addresses[0],)).fetchone())
    assert before == after
    assert result["intake"]["added"] == [{"chain": "sol", "address": addresses[1], "cohort": "research"}]
    inserted = tmp_db.execute("SELECT name,source,cohort FROM wallets WHERE address=?", (addresses[1],)).fetchone()
    assert tuple(inserted) == (None, "codex-autolearn-research", "research")
    assert result["review"]["live_copy_approved"] is False and result["intake"]["provider_calls"] == 0


def test_new_export_mtime_cannot_refresh_original_stale_provider_profile(tmp_db, tmp_path):
    root = tmp_path / "runners"
    _caches(root, profile_at=NOW - 3 * DAY)
    result = aw.run_wallet_review(GuardedReader(_path(tmp_db)), root, now_ms=NOW)
    candidate = result["review"]["candidates"][0]
    assert candidate["profiles"][0]["at_ms"] == NOW - 3 * DAY
    assert "PROVIDER_POSITIVE" not in candidate["roles"] and candidate["copy_status"] == "COPY_UNPROVEN"
    assert result["review"]["observation_queue"][0]["selection_arm"] == "exploration"
    assert result["review"]["coverage"]["source_coverage"]["files"][0]["sha256"]


def test_daily_budget_survives_lost_caller_state_and_resets_on_next_utc_day(tmp_db, tmp_path):
    root = tmp_path / "runners"
    _caches(root, 45)
    reader = GuardedReader(_path(tmp_db))
    first = aw.run_wallet_review(reader, root, now_ms=NOW)
    assert len(first["intake"]["added"]) == 20 and first["intake"]["daily_count"] == 20
    lost_state_retry = aw.run_wallet_review(reader, root, now_ms=NOW)
    assert lost_state_retry["intake"]["added"] == [] and lost_state_retry["intake"]["daily_count"] == 20
    next_day = aw.run_wallet_review(reader, root, now_ms=NOW + DAY, prior_state=first["next_state"])
    assert len(next_day["intake"]["added"]) <= 20
    assert tmp_db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] <= 40
    assert next_day["next_state"]["day"] != first["next_state"]["day"]


def test_write_lock_rechecks_missing_identity_after_concurrent_trusted_insert(tmp_db, tmp_path, monkeypatch):
    root = tmp_path / "runners"
    address = _caches(root)[0]
    original = aw._intake

    def racing(reader, records, **kwargs):
        _wallet(tmp_db, address)
        return original(reader, records, **kwargs)

    monkeypatch.setattr(aw, "_intake", racing)
    result = aw.run_wallet_review(GuardedReader(_path(tmp_db)), root, now_ms=NOW)
    assert result["intake"]["added"] == [] and result["intake"]["daily_count"] == 0
    assert result["intake"]["existing_preserved"] == [{"chain": "sol", "address": address}]
    assert tuple(tmp_db.execute("SELECT name,source,cohort FROM wallets").fetchone()) == (
        "My trusted wallet", "owner_curated", "trusted_copy")


def test_deferred_registry_or_wal_never_turns_unknown_identity_into_missing(tmp_db, tmp_path, monkeypatch):
    root = tmp_path / "runners"
    _caches(root)
    reader = GuardedReader(_path(tmp_db))
    original = reader.rows

    def deferred(sql, args=()):
        if "wallet_feed_tags" in sql:
            raise SourceDeferred("tag_evidence_unavailable")
        return original(sql, args)

    monkeypatch.setattr(reader, "rows", deferred)
    with pytest.raises(SourceDeferred, match="tag_evidence"):
        aw.run_wallet_review(reader, root, now_ms=NOW)
    assert tmp_db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 0


def test_adverse_feed_tags_and_hubs_are_excluded_before_research_intake(tmp_db, tmp_path):
    root = tmp_path / "runners"
    addresses = _caches(root, 3)
    tmp_db.execute("INSERT INTO wallet_feed_tags VALUES (?,?,?,?,?,?,?,?)",
                   ("sol", addresses[0], "dev", "historical", 1, 2, 1, None))
    tmp_db.execute("INSERT INTO hub_addresses VALUES (?,?,?,?,?)", ("sol", addresses[1], "router", "hub", "known"))
    result = aw.run_wallet_review(GuardedReader(_path(tmp_db)), root, now_ms=NOW)
    assert [r["address"] for r in result["intake"]["added"]] == [addresses[2]]
    assert result["review"]["coverage"]["excluded_count"] == 2


def test_bnb_alias_exact_chain_intake_and_empty_runner_path(tmp_db, tmp_path):
    reader = GuardedReader(_path(tmp_db))
    empty = aw.run_wallet_review(reader, tmp_path / "absent", now_ms=NOW)
    assert empty["review"]["candidates"] == [] and empty["intake"]["added"] == []
    root = tmp_path / "runners"
    address = _caches(root, chain="bsc")[0]
    result = aw.run_wallet_review(reader, root, now_ms=NOW)
    assert result["intake"]["added"] == [{"chain": "bsc", "address": address, "cohort": "research"}]
    assert tmp_db.execute("SELECT chain FROM wallets").fetchone()[0] == "bsc"


def _only_sol(root, address, at):
    _write(root / "exports/kaiba-solana-snipers-early-runners.txt", [{"address": address}], stamp=at)
    _write(root / "sol/p30.json", {address: {"_at": at // 1000, "realized_profit": 10, "buy": 20}}, stamp=at)


def test_acquisition_survives_export_rotation_without_entering_current_followthrough(tmp_db, tmp_path):
    root = tmp_path / "runners"
    original, replacement = _sol(10), _sol(11)
    _only_sol(root, original, NOW)
    reader = GuardedReader(_path(tmp_db))
    first = aw.run_wallet_review(reader, root, now_ms=NOW)
    frozen = first["next_state"]["acquisition_state"]["pending"][0].copy()
    assert first["review"]["acquisition_queue"] == []  # Must first verify the committed import next round.
    _only_sol(root, replacement, NOW + 60_000)
    second = aw.run_wallet_review(reader, root, now_ms=NOW + 60_000, prior_state=first["next_state"])
    current = {r["address"] for r in second["review"]["observation_queue"]}
    assert current == {replacement}
    from kaiba.learning.fixed_wallet_study import MEMBERS
    observed = {r["wallet"] for r in second["review"]["behavior_learning"]["behavior_reports"]}
    assert observed <= current | {a for _, a, _ in MEMBERS}
    assert original not in observed  # Carried acquisition nominees never enter current followthrough.
    queue = second["review"]["acquisition_queue"]
    assert [r["address"] for r in queue] == [original]
    assert queue[0]["selected_ms"] == NOW and queue[0]["registry_checked_ms"] >= NOW + 60_000
    retained = next(r for r in second["next_state"]["acquisition_state"]["pending"] if r["address"] == original)
    for field in aw.ACQUISITION_FROZEN_FIELDS:
        assert retained[field] == frozen[field]
    assert retained["nominations"][0]["observed_ms"] == NOW
    assert second["review"]["acquisition_learning"]["full_history_proven"] is False


def test_existing_cohort_null_is_never_retagged_or_enrolled_in_acquisition(tmp_db, tmp_path):
    root = tmp_path / "runners"
    address = _caches(root)[0]
    _wallet(tmp_db, address, cohort=None, source="gmgn_feed", name="existing unassigned wallet")
    before = dict(tmp_db.execute("SELECT * FROM wallets").fetchone())
    result = aw.run_wallet_review(GuardedReader(_path(tmp_db)), root, now_ms=NOW)
    assert result["review"]["observation_queue"][0]["address"] == address
    assert result["review"]["acquisition_queue"] == []
    assert result["next_state"]["acquisition_state"]["pending"] == []
    assert dict(tmp_db.execute("SELECT * FROM wallets").fetchone()) == before


@pytest.mark.parametrize("unsafe", ["owner", "owner_int", "quarantine", "hub", "cohort_null", "trusted_copy"])
def test_rotated_pending_target_revalidates_current_exclusions(tmp_db, tmp_path, unsafe):
    root = tmp_path / "runners"
    address, other = _sol(14), _sol(15)
    _only_sol(root, address, NOW)
    reader = GuardedReader(_path(tmp_db))
    first = aw.run_wallet_review(reader, root, now_ms=NOW)
    if unsafe in {"owner", "owner_int"}:
        tmp_db.execute("UPDATE wallets SET meta_json=? WHERE address=?",
                       (json.dumps({"is_owner": True if unsafe == "owner" else 1}), address))
    elif unsafe == "quarantine":
        tmp_db.execute("INSERT INTO wallet_feed_tags VALUES ('sol',?,'scammer','new',1,2,1,NULL)", (address,))
    elif unsafe == "hub":
        tmp_db.execute("INSERT INTO hub_addresses VALUES ('sol',?,'router','hub','known')", (address,))
    else:
        tmp_db.execute("UPDATE wallets SET cohort=? WHERE address=?",
                       (None if unsafe == "cohort_null" else "trusted_copy", address))
    before = dict(tmp_db.execute("SELECT * FROM wallets WHERE address=?", (address,)).fetchone())
    _only_sol(root, other, NOW + 60_000)
    result = aw.run_wallet_review(reader, root, now_ms=NOW + 60_000, prior_state=first["next_state"])
    assert result["review"]["acquisition_queue"] == []
    pending = next(r for r in result["review"]["acquisition_candidates"] if r["address"] == address)
    assert pending["status"] == "blocked" and pending["exclusions"]
    assert dict(tmp_db.execute("SELECT * FROM wallets WHERE address=?", (address,)).fetchone()) == before


def test_pending_registry_deferral_is_unknown_and_never_exposes_a_target(tmp_db, tmp_path):
    root = tmp_path / "runners"
    address, other = _sol(17), _sol(18)
    _only_sol(root, address, NOW)
    first = aw.run_wallet_review(GuardedReader(_path(tmp_db)), root, now_ms=NOW)
    _only_sol(root, other, NOW + 60_000)

    class PendingDeferred(GuardedReader):
        def rows(self, sql, args=()):
            if "FROM wallets" in sql and address in args:
                raise SourceDeferred("pending_identity_unavailable")
            return super().rows(sql, args)

    result = aw.run_wallet_review(PendingDeferred(_path(tmp_db)), root, now_ms=NOW + 60_000,
                                  prior_state=first["next_state"])
    assert result["review"]["acquisition_learning"]["status"] == "unknown"
    assert result["review"]["acquisition_queue"] == []
    assert result["review"]["acquisition_learning"]["retry_after_ms"] >= NOW + 120_000
    assert next(r for r in result["next_state"]["acquisition_state"]["pending"]
                if r["address"] == address)["selected_ms"] == NOW


def test_transient_pending_wal_refusal_stops_behavior_and_intake_even_if_next_read_succeeds(
        tmp_db, tmp_path, monkeypatch):
    root = tmp_path / "runners"
    address, other = _sol(19), _sol(20)
    _only_sol(root, address, NOW)
    first = aw.run_wallet_review(GuardedReader(_path(tmp_db)), root, now_ms=NOW)
    _only_sol(root, other, NOW + 60_000)

    class TransientWal(GuardedReader):
        refused = False

        def rows(self, sql, args=()):
            if not self.refused and "FROM wallets" in sql and address in args:
                self.refused = True
                raise SourceDeferred("wal_over_limit:1500000001; retry after at least 60s")
            return super().rows(sql, args)

    after_refusal = []
    monkeypatch.setattr(aw, "collect_behavior", lambda *a, **kw: after_refusal.append("behavior"))
    monkeypatch.setattr(aw, "_intake", lambda *a, **kw: after_refusal.append("intake"))
    reader = TransientWal(_path(tmp_db))
    with pytest.raises(SourceDeferred, match="wal_over_limit.*at least 60s"):
        aw.run_wallet_review(reader, root, now_ms=NOW + 60_000, prior_state=first["next_state"])
    assert reader.rows("SELECT 1 AS available") == [{"available": 1}]
    assert after_refusal == []
    assert tmp_db.execute("SELECT 1 FROM wallets WHERE address=?", (other,)).fetchone() is None


def test_carried_missing_sol_uses_same_twenty_intakes_after_utc_reset(tmp_db, tmp_path):
    root = tmp_path / "runners"
    original = _caches(root)[0]
    day = datetime.datetime.fromtimestamp(NOW / 1000, datetime.UTC).date().isoformat()
    tmp_db.execute("INSERT INTO kv(key,value,updated_ms) VALUES (?,?,?)",
                   ("autolearn:wallet-intake:" + day, '{"count":20}', NOW))
    reader = GuardedReader(_path(tmp_db))
    first = aw.run_wallet_review(reader, root, now_ms=NOW)
    assert first["intake"]["daily_count"] == 20 and first["intake"]["added"] == []
    assert first["next_state"]["acquisition_state"]["pending"][0]["address"] == original
    (root / "exports/kaiba-solana-snipers-early-runners.txt").unlink()
    _caches(root, 20, chain="bsc", profile_at=NOW + DAY)
    second = aw.run_wallet_review(reader, root, now_ms=NOW + DAY, prior_state=first["next_state"])
    assert second["intake"]["daily_count"] == 20 and len(second["intake"]["added"]) == 20
    assert second["intake"]["added"][0] == {"chain": "sol", "address": original, "cohort": "research"}
    assert all(r["chain"] == "bsc" for r in second["review"]["observation_queue"])
    third = aw.run_wallet_review(reader, root, now_ms=NOW + DAY + 60_000, prior_state=second["next_state"])
    assert third["intake"]["added"] == [] and third["intake"]["daily_count"] == 20
    assert third["review"]["acquisition_queue"][0]["address"] == original
    assert third["review"]["acquisition_queue"][0]["selected_ms"] == NOW


def test_pending_caps_corruption_and_age_are_explicit(tmp_db, tmp_path):
    import copy

    root = tmp_path / "runners"
    _caches(root, 45)
    reader = GuardedReader(_path(tmp_db))
    first = aw.run_wallet_review(reader, root, now_ms=NOW)
    state = first["next_state"]["acquisition_state"]
    assert len(state["pending"]) == 20 and aw._acquisition_bytes(state) <= 32 * 1024
    broken = copy.deepcopy(first["next_state"])
    broken["acquisition_state"]["pending"][0]["selected_ms"] += 1
    corrupt = aw.run_wallet_review(reader, root, now_ms=NOW + 60_000, prior_state=broken)
    assert corrupt["review"]["acquisition_learning"]["status"] == "unknown"
    assert corrupt["review"]["acquisition_queue"] == []
    _only_sol(root, _sol(50), NOW + 13 * DAY + 1)
    expired = aw.run_wallet_review(reader, root, now_ms=NOW + 13 * DAY + 1, prior_state=first["next_state"])
    assert len(expired["review"]["acquisition_learning"]["expired"]) == 20
    assert not ({r["address"] for r in state["pending"]}
                & {r["address"] for r in expired["next_state"]["acquisition_state"]["pending"]})


def test_backfill_page_advancement_completes_work_without_claiming_quality(tmp_db, tmp_path):
    root = tmp_path / "runners"
    address = _caches(root)[0]
    reader = GuardedReader(_path(tmp_db))
    first = aw.run_wallet_review(reader, root, now_ms=NOW)
    tmp_db.execute("INSERT INTO kv(key,value,updated_ms) VALUES (?,?,?)",
                   ("backfill:swaps:sol:" + address, '{"pages":1}', NOW + 1000))
    _only_sol(root, _sol(51), NOW + 60_000)
    result = aw.run_wallet_review(reader, root, now_ms=NOW + 60_000, prior_state=first["next_state"])
    assert all(r["address"] != address for r in result["next_state"]["acquisition_state"]["pending"])
    assert result["review"]["acquisition_learning"]["completed"][0]["reason"] == (
        "backfill_page_observed_not_full_history_or_profit_proof")
    assert result["review"]["acquisition_learning"]["full_history_proven"] is False


@pytest.mark.parametrize("invalid", ["oversized_state", "source_changed", "oversized_metadata", "corrupt_cursor"])
def test_acquisition_invalid_state_or_source_rows_remain_unknown(tmp_db, tmp_path, invalid):
    root = tmp_path / "runners"
    address = _caches(root)[0]
    reader = GuardedReader(_path(tmp_db))
    first = aw.run_wallet_review(reader, root, now_ms=NOW)
    if invalid == "oversized_state":
        first["next_state"]["acquisition_state"]["unbounded"] = "x" * (33 * 1024)
    elif invalid == "source_changed":
        first["next_state"]["acquisition_state"]["source_hashes"] = {"changed": "yes"}
    elif invalid == "oversized_metadata":
        tmp_db.execute("UPDATE wallets SET meta_json=? WHERE address=?", (json.dumps({"large": "x" * 65536}), address))
    else:
        tmp_db.execute("INSERT INTO kv(key,value,updated_ms) VALUES (?,?,?)",
                       ("backfill:swaps:sol:" + address, "not-json", NOW))
    _only_sol(root, _sol(52), NOW + 60_000)
    result = aw.run_wallet_review(reader, root, now_ms=NOW + 60_000, prior_state=first["next_state"])
    assert result["review"]["acquisition_learning"]["status"] == "unknown"
    assert result["review"]["acquisition_queue"] == []
    assert aw._acquisition_bytes(result["next_state"]["acquisition_state"]) <= 32 * 1024
    if invalid == "source_changed":
        assert result["next_state"]["acquisition_state"] == first["next_state"]["acquisition_state"]


@pytest.mark.parametrize("updated", [NOW + DAY, 0, -1])
def test_invalid_cursor_clock_cannot_complete_pending_or_allow_sol_intake(tmp_db, tmp_path, updated):
    root = tmp_path / "runners"
    address, other = _sol(53), _sol(54)
    _only_sol(root, address, NOW)
    reader = GuardedReader(_path(tmp_db))
    first = aw.run_wallet_review(reader, root, now_ms=NOW)
    tmp_db.execute("INSERT INTO kv(key,value,updated_ms) VALUES (?,?,?)",
                   ("backfill:swaps:sol:" + address, '{"pages":1}', updated))
    _only_sol(root, other, NOW + 60_000)
    blocked = aw.run_wallet_review(reader, root, now_ms=NOW + 60_000, prior_state=first["next_state"])
    learning = blocked["review"]["acquisition_learning"]
    assert learning["status"] == "unknown" and learning.get("completed", []) == []
    assert blocked["review"]["acquisition_queue"] == [] and blocked["intake"]["added"] == []
    pending = next(r for r in blocked["next_state"]["acquisition_state"]["pending"] if r["address"] == address)
    assert pending["selected_ms"] == NOW and pending["baseline_pages"] == 0
    assert tmp_db.execute("SELECT 1 FROM wallets WHERE address=?", (other,)).fetchone() is None
    # A corrected, current page receipt advances the SAME frozen selection.
    tmp_db.execute("UPDATE kv SET updated_ms=? WHERE key=?", (NOW + 1000, "backfill:swaps:sol:" + address))
    recovered = aw.run_wallet_review(reader, root, now_ms=NOW + 120_000, prior_state=blocked["next_state"])
    assert recovered["review"]["acquisition_learning"]["status"] == "complete"
    assert recovered["review"]["acquisition_learning"]["completed"][0]["selection_hash"] == pending["selection_hash"]
    assert recovered["intake"]["added"] == [{"chain": "sol", "address": other, "cohort": "research"}]


@pytest.mark.parametrize("clock", [None, 0, "not-an-integer", NOW + DAY])
def test_bad_new_nomination_clock_is_omitted_without_poisoning_frozen_pending(
        tmp_db, tmp_path, monkeypatch, clock):
    root = tmp_path / "runners"
    address, other = _sol(55), _sol(56)
    _only_sol(root, address, NOW)
    reader = GuardedReader(_path(tmp_db))
    first = aw.run_wallet_review(reader, root, now_ms=NOW)
    frozen = first["next_state"]["acquisition_state"]["pending"][0].copy()
    before = dict(tmp_db.execute("SELECT * FROM wallets WHERE address=?", (address,)).fetchone())
    _only_sol(root, other, NOW + 60_000)
    original_packet = aw._packet

    def bad_packet(*args, **kwargs):
        packet = original_packet(*args, **kwargs)
        for export in packet["exports"]:
            export["observed_ms"] = clock
        return packet

    monkeypatch.setattr(aw, "_packet", bad_packet)
    result = aw.run_wallet_review(reader, root, now_ms=NOW + 60_000, prior_state=first["next_state"])
    assert result["review"]["acquisition_learning"]["status"] == "complete"
    assert result["review"]["acquisition_learning"]["invalid_nominations"][0]["reason"] == "nomination_clock_unknown_or_future"
    assert result["next_state"]["acquisition_state"]["pending"] == [frozen]
    assert dict(tmp_db.execute("SELECT * FROM wallets WHERE address=?", (address,)).fetchone()) == before
    assert [r["address"] for r in result["review"]["acquisition_queue"]] == [address]


def _growing_file(monkeypatch, path, now=NOW):
    """The file passes stat admission, then its stream has grown past the bound."""
    from types import SimpleNamespace

    path = path.resolve()
    original_stat, original_open = Path.stat, Path.open
    requests = []

    class GrowingStream(io.BytesIO):
        def read(self, size=-1):
            requests.append(size)
            assert size > 0, "a concurrently growing cache must never be read without a bound"
            return super().read(size)

    def stat(candidate, *args, **kwargs):
        if candidate == path:
            return SimpleNamespace(st_size=1, st_mtime=now / 1000)
        return original_stat(candidate, *args, **kwargs)

    def open_file(candidate, *args, **kwargs):
        return GrowingStream(b"x" * 100) if candidate == path else original_open(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "open", open_file)
    monkeypatch.setattr(aw, "MAX_FILE_BYTES", 32)
    monkeypatch.setattr(aw, "MAX_TOTAL_BYTES", 64)
    return requests


@pytest.mark.parametrize("already_read,requested", [(0, 33), (52, 13)])
def test_concurrent_file_growth_is_bounded_before_json_decode(tmp_path, monkeypatch, already_read, requested):
    path = tmp_path / "cache.json"
    path.write_text("[]")
    receipts, budget = [], [already_read]
    requests = _growing_file(monkeypatch, path)
    decoded = []
    with monkeypatch.context() as scoped:
        scoped.setattr(aw.json, "loads", lambda raw: decoded.append(raw) or [])
        with pytest.raises(ValueError, match="changed beyond bounded read"):
            aw._read(path, tmp_path.resolve(), receipts, budget)
    assert requests == [requested] and decoded == []
    assert receipts == [] and budget == [already_read]


def test_concurrently_grown_export_cannot_decode_or_import_wallets(tmp_db, tmp_path, monkeypatch):
    root = tmp_path / "runners"
    path = root / "exports" / "kaiba-solana-snipers-early-runners.txt"
    _write(path, [])
    requests = _growing_file(monkeypatch, path)
    imported = []
    monkeypatch.setattr(aw, "import_records", lambda *a, **kw: imported.append(a) or None)
    result = aw.run_wallet_review(GuardedReader(_path(tmp_db)), root, now_ms=NOW)
    assert requests == [33] and imported == []
    assert result["review"]["candidates"] == [] and result["intake"]["added"] == []
    assert tmp_db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 0
    receipts = result["review"]["coverage"]["source_coverage"]["files"]
    assert receipts == [{"path": str(path), "status": "unavailable:ValueError"}]


def test_dense_history_uses_top_two_index_seeks_with_deterministic_timestamp_ties(tmp_db):
    addresses = [_sol(i) for i in range(100)]
    tmp_db.execute("BEGIN")
    try:
        tmp_db.executemany("INSERT INTO wallet_score_history(chain,address,score,grade,scored_at_ms,model_version) "
                           "VALUES (?,?,?,?,?,?)", (("sol", address, i, "B", 1000 + i // 3, f"fixture-{i}")
                                                    for address in addresses for i in range(201)))
        tmp_db.commit()
    except Exception:
        tmp_db.rollback()
        raise

    class RecordingReader(GuardedReader):
        statements = []

        def rows(self, sql, args=()):
            self.statements.append((sql, args))
            return super().rows(sql, args)

    reader = RecordingReader(_path(tmp_db))
    rows = aw._recent_history(reader, "sol", addresses)
    assert len(rows) == 200
    for address in addresses:
        assert {r["model_version"] for r in rows if r["address"] == address} == {"fixture-200", "fixture-199"}
    assert reader.queries == len(reader.statements) == 5
    assert [len(args) // 2 for _sql, args in reader.statements] == [20] * 5
    for sql, args in reader.statements:
        plan = " ".join(row[3] for row in tmp_db.execute("EXPLAIN QUERY PLAN " + sql, args))
        assert "SEARCH x USING COVERING INDEX idx_wsh (chain=? AND address=?)" in plan
        assert "SEARCH h USING INTEGER PRIMARY KEY" in plan
        assert not any(term in plan for term in ("TEMP B-TREE", "SCAN h", "SCAN x", "SCAN wallet_score_history"))

    def steps(statement, parameters):
        count = [0]

        def tick():
            count[0] += 100
            return 0

        tmp_db.set_progress_handler(tick, 100)
        try:
            tmp_db.execute(statement, parameters).fetchall()
        finally:
            tmp_db.set_progress_handler(None, 0)
        return count[0]

    marks = ",".join("?" for _ in addresses)
    previous = ("SELECT chain,address,score,grade,scored_at_ms,model_version FROM "
                "(SELECT *,ROW_NUMBER() OVER(PARTITION BY chain,address ORDER BY scored_at_ms DESC) AS rank "
                f"FROM wallet_score_history WHERE chain=? AND address IN ({marks})) WHERE rank<=2")
    assert steps(previous, ("sol", *addresses)) > 10 * sum(steps(sql, args) for sql, args in reader.statements)


def test_history_batch_rejects_more_than_100_requested_identities_before_query(tmp_db):
    reader = GuardedReader(_path(tmp_db))
    assert aw._recent_history(reader, "sol", []) == []
    with pytest.raises(ValueError, match="exceeds 100"):
        aw._recent_history(reader, "sol", [_sol(i) for i in range(101)])
    assert reader.queries == 0


def test_history_subbatch_deferral_cannot_be_returned_as_complete_missing_history(tmp_db):
    class DeferringReader(GuardedReader):
        batches = []

        def rows(self, sql, args=()):
            self.batches.append(tuple(args[1::2]))
            assert len(args) <= 40, "each guarded history read may request at most20identities"
            if len(self.batches) == 2:
                raise SourceDeferred("second_history_subbatch_unavailable")
            return super().rows(sql, args)

    reader = DeferringReader(_path(tmp_db))
    addresses = [_sol(i) for i in range(100)]
    with pytest.raises(SourceDeferred, match="second_history_subbatch"):
        aw._recent_history(reader, "sol", addresses)
    assert reader.batches == [tuple(addresses[:20]), tuple(addresses[20:40])]
    assert reader.queries == 1  # Only the first independently guarded read succeeded.


def test_real_losing_teacher_enriches_selected_wallet_without_reselecting_or_promoting(tmp_db, tmp_path):
    root = tmp_path / "runners"
    addresses = _caches(root, 2)
    r = GuardedReader(_path(tmp_db))
    packet = aw._packet(r, root, NOW, {})
    packet.pop("present")
    expected = aw.review_candidates(packet, now_ms=NOW, budget=20, exploration_slots=5)
    for offset, side, money in ((-2000, "buy", "100"), (-1000, "sell", "50")):
        tmp_db.execute("INSERT INTO swaps(chain,tx,ts_ms,wallet,token,side,amount_token,amount_native,usd_value,source) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)", ("sol", side, NOW + offset, addresses[0], "TOKEN", side,
                                                       "100", money, money, "helius"))
    from kaiba.learning.fixed_wallet_study import initial_study_state
    result = aw.run_wallet_review(r, root, now_ms=NOW,
                                  prior_state={"fixed_study_state": initial_study_state()})
    review = result["review"]
    assert review["observation_queue"] == expected["observation_queue"]
    assert review["next_state"] == expected["next_state"]
    teacher = next(c for c in review["candidates"] if c["address"] == addresses[0])
    assert "BEHAVIOR_TEACHER" in teacher["roles"]
    assert teacher["behavior_evidence"][0]["inspectable_episodes"] == 1
    assert review["copy_qualified_count"] == 0 and teacher["copy_status"] == "COPY_UNPROVEN"
    assert {r["address"] for r in result["intake"]["added"]} == set(addresses)
    assert tmp_db.execute("SELECT COUNT(*) FROM wallet_scores").fetchone()[0] == 0
    assert result["next_state"]["behavior_state"]["selections"]


def test_fixed_study_hook_uses_one_collector_and_preserves_nomination_intake_population(tmp_db, tmp_path, monkeypatch):
    from kaiba.learning import fixed_wallet_study as fs
    root = tmp_path / "runners"
    addresses = _caches(root, 25)
    reader = GuardedReader(_path(tmp_db))
    packet = aw._packet(reader, root, NOW, {})
    packet.pop("present")
    expected = aw.review_candidates(packet, now_ms=NOW, budget=20, exploration_slots=5)
    calls, original = [], aw.collect_behavior

    def collected(*args, **kwargs):
        calls.append(list(args[1]))
        return original(*args, **kwargs)

    monkeypatch.setattr(aw, "collect_behavior", collected)
    out = aw.run_wallet_review(reader, root, now_ms=NOW,
                               prior_state={"fixed_study_state": fs.initial_study_state()})
    assert out["review"]["observation_queue"] == expected["observation_queue"]
    assert out["review"]["next_state"] == expected["next_state"]
    assert len(calls) == 1 and len(calls[0]) == 10
    assert {(r["chain"], r["address"]) for r in calls[0][:6]} == {(c, a) for c, a, _ in fs.MEMBERS}
    assert len(out["intake"]["added"]) == out["intake"]["daily_count"] == 20
    assert {r["address"] for r in out["intake"]["added"]} <= set(addresses)
    assert not ({r["address"] for r in out["intake"]["added"]} & {a for _, a, _ in fs.MEMBERS})
    assert tmp_db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 20
    assert out["review"]["acquisition_learning"]["pending_count"] == 20
    assert len(out["next_state"]["fixed_study_state"]["selections"]) == 6
    assert out["review"]["behavior_learning"]["coverage"]["attempted"] == 10


def test_missing_fixed_state_does_not_register_any_epoch_or_block_existing_intake(tmp_db, tmp_path):
    root = tmp_path / "runners"
    address = _caches(root)[0]
    out = aw.run_wallet_review(GuardedReader(_path(tmp_db)), root, now_ms=NOW)
    learning = out["review"]["behavior_learning"]
    assert learning["fixed_study"]["reason"] == "fixed_study_state_missing"
    assert learning["coverage"]["attempted"] == 0 and learning["behavior_reports"] == []
    assert out["next_state"]["fixed_study_state"]["status"] == "unknown"
    assert "manifest" not in out["next_state"]["fixed_study_state"]
    assert out["intake"]["added"] == [{"chain": "sol", "address": address, "cohort": "research"}]


def test_transient_fixed_tape_wal_prevents_later_reads_and_intake(tmp_db, tmp_path, monkeypatch):
    from kaiba.learning.fixed_wallet_study import initial_study_state
    root = tmp_path / "runners"
    _caches(root)
    reader = GuardedReader(_path(tmp_db))
    original, after_failure = reader.rows, []

    def transient(sql, args=()):
        if "FROM swaps INDEXED BY idx_swaps_wallet" in sql and not after_failure:
            after_failure.append("refused")
            raise SourceDeferred("wal_over_limit:1500000001; retry after at least 60s")
        if after_failure:
            after_failure.append("unexpected_later_query")
        return original(sql, args)

    monkeypatch.setattr(reader, "rows", transient)
    with pytest.raises(SourceDeferred, match="wal_over_limit.*at least 60s"):
        aw.run_wallet_review(reader, root, now_ms=NOW,
                             prior_state={"fixed_study_state": initial_study_state()})
    assert after_failure == ["refused"]
    assert tmp_db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 0


def test_fixed_baseline_clock_is_refreshed_after_actual_registry_work(tmp_db, tmp_path, monkeypatch):
    from kaiba.learning.fixed_wallet_study import initial_study_state
    root = tmp_path / "runners"
    address = _caches(root)[0]
    wall = [NOW]
    monkeypatch.setattr(aw.time, "time", lambda: wall[0] / 1000)
    original = aw._acquisitions

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        wall[0] += 17_000  # Registry work happens before the actual baseline capture.
        return result

    monkeypatch.setattr(aw, "_acquisitions", delayed)
    for offset, side, money in ((-2000, "buy", "100"), (-1000, "sell", "50")):
        tmp_db.execute("INSERT INTO swaps(chain,tx,ts_ms,wallet,token,side,amount_token,amount_native,usd_value,source) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)", ("sol", side, NOW + offset, address, "TOKEN", side,
                                                       "100", money, money, "helius"))
    out = aw.run_wallet_review(GuardedReader(_path(tmp_db)), root, now_ms=NOW,
                               prior_state={"fixed_study_state": initial_study_state()})
    fixed = out["next_state"]["fixed_study_state"]
    assert fixed["manifest"]["selected_ms"] == NOW + 17_000
    assert all(r["selection_ms"] == r["baseline"]["captured_ms"] == NOW + 17_000
               for r in fixed["selections"].values())
    assert all(r["as_of_ms"] == NOW + 17_000 for r in out["review"]["behavior_learning"]["receipts"])
    assert out["review"]["observed_ms"] == NOW
    assert all(r["selected_ms"] == NOW for r in out["next_state"]["acquisition_state"]["pending"])
    teacher = next(r for r in out["review"]["candidates"] if r["address"] == address)
    assert "BEHAVIOR_TEACHER" in teacher["roles"]
    assert teacher["behavior_evidence"][0]["fresh"] is True
    assert teacher["behavior_evidence"][0]["inspectable_episodes"] == 1
    report = next(r for r in out["review"]["behavior_learning"]["behavior_reports"] if r["wallet"] == address)
    stale_annotation = aw._behavior(report, ("sol", address), NOW)
    assert stale_annotation["fresh"] is False and stale_annotation["inspectable_episodes"] == 0


def test_optional_tape_deadline_does_not_destroy_complete_registry_absence_proof(tmp_db, tmp_path):
    class TapeDeferred(GuardedReader):
        def rows(self, sql, args=()):
            if "FROM swaps INDEXED BY idx_swaps_wallet" in sql:
                raise SourceDeferred("tape_query_deadline")
            return super().rows(sql, args)

    root = tmp_path / "runners"
    address = _caches(root)[0]
    from kaiba.learning.fixed_wallet_study import initial_study_state
    result = aw.run_wallet_review(TapeDeferred(_path(tmp_db)), root, now_ms=NOW,
                                  prior_state={"fixed_study_state": initial_study_state()})
    assert result["intake"]["added"] == [{"chain": "sol", "address": address, "cohort": "research"}]
    learning = result["review"]["behavior_learning"]
    assert learning["receipts"][0]["query_status"] == "tape_query_deadline"
    assert learning["behavior_reports"][0]["episodes"] == []
    assert result["review"]["coverage"]["behavior_teacher_count"] == 0


def test_actual_packet_uses_twenty_identity_registry_queries_without_losing_population(tmp_db, tmp_path):
    root = tmp_path / "runners"
    addresses = _caches(root, 63)
    for address in addresses:
        _wallet(tmp_db, address, cohort="research", source="fixture")

    class RecordingReader(GuardedReader):
        statements = []

        def rows(self, sql, args=()):
            self.statements.append((sql, tuple(args)))
            return super().rows(sql, args)

    r = RecordingReader(_path(tmp_db))
    packet = aw._packet(r, root, NOW, {})
    tables = ("wallets", "wallet_score_history", "wallet_feed_tags", "entity_members", "hub_addresses")
    for table in tables:
        statements = [(sql, args) for sql, args in r.statements if table in sql]
        assert len(statements) == 4
        queried = []
        for _, args in statements:
            identities = args[1::2] if table == "wallet_score_history" else args[1:]
            assert 0 < len(identities) <= 20
            queried.extend(identities)
        assert len(queried) == 63 and set(queried) == set(addresses)
    assert packet["present"] == {("sol", address) for address in addresses}
    assert len(packet["profiles"]) == 63
    assert packet["coverage"]["registry_complete_for_selected_identities"] is True


@pytest.mark.parametrize("table", ["wallets", "wallet_score_history", "wallet_feed_tags", "entity_members", "hub_addresses"])
def test_second_registry_subbatch_deferral_has_safe_context_and_never_authorizes_intake(
        tmp_db, tmp_path, monkeypatch, table):
    root = tmp_path / "runners"
    addresses = _caches(root, 45)
    _wallet(tmp_db, addresses[0])
    before = dict(tmp_db.execute("SELECT * FROM wallets").fetchone())
    intake_calls = []
    monkeypatch.setattr(aw, "_intake", lambda *args, **kwargs: intake_calls.append(args))

    class DeferredReader(GuardedReader):
        matching_queries = 0

        def rows(self, sql, args=()):
            if table in sql:
                self.matching_queries += 1
                if self.matching_queries == 2:
                    raise SourceDeferred("forced_deadline")
            return super().rows(sql, args)

    with pytest.raises(SourceDeferred) as error:
        aw.run_wallet_review(DeferredReader(_path(tmp_db)), root, now_ms=NOW)
    message = str(error.value)
    assert f"table={table};chain=sol;batch_offset=20" in message
    assert "forced_deadline" in message and "SELECT" not in message
    assert not any(address in message for address in addresses)
    assert intake_calls == []
    assert dict(tmp_db.execute("SELECT * FROM wallets").fetchone()) == before
    assert tmp_db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 1


def test_smaller_registry_queries_preserve_original_hundred_identity_tag_cap(tmp_db, tmp_path):
    root = tmp_path / "runners"
    addresses = _caches(root, 40)
    tmp_db.executemany("INSERT INTO wallet_feed_tags VALUES (?,?,?,?,?,?,?,?)",
                       [("sol", address, f"tag{i}", "fixture", 1, 2, 1, None)
                        for address in addresses for i in range(51)])
    with pytest.raises(SourceDeferred, match="batch_offset=20;wallet_tag_history_exceeds_bounded_review"):
        aw.run_wallet_review(GuardedReader(_path(tmp_db)), root, now_ms=NOW)
    assert tmp_db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 0
