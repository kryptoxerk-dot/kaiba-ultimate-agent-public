"""Actual intake -> durable method registry, with network explicitly forbidden."""

import copy
import json
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from kaiba.hunters.grok_alpha_bridge import Store, normalize
from kaiba.learning import method_intake as lab

AT = int(datetime.fromisoformat("2026-11-03T00:00:00+00:00").timestamp() * 1000)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("method worker attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", denied)


def card():
    return {
        "schema_version": "kaiba.grok.observation/1",
        "track": "flow",
        "project": "Fixture method",
        "claim": "Unverified source description",
        "network": "Solana",
        "contract": None,
        "observed_at": "2026-11-03T00:00:00Z",
        "received_at": None,
        "sources": [
            {
                "url": "https://x.com/example/status/2117002382601015453",
                "published_at": "2026-11-02T20:00:00Z",
                "retrieved_at": "2026-11-03T00:00:00Z",
            }
        ],
        "provenance": {"backend": "manual_export"},
        "state": {
            "opportunity_type": "trading_method",
            "research_only": True,
            "method_hypothesis": {
                "method_id": "author-id",
                "entry": "Original incomplete entry",
                "exit": None,
                "claimed_performance": "100% profits (unverified)",
            },
        },
    }


def row(c=None, *, received=AT):
    c = c or card()
    key, digest, raw, _ = normalize(c, now_ms=received)
    return {"key": key, "digest": digest, "payload": raw, "received_ms": received}


def empty():
    return {"version": lab.VERSION, "methods": {}}


def test_incomplete_rules_preserved_without_execution_or_invented_exits():
    source = row()
    original = copy.deepcopy(source)
    state, report = lab.update(empty(), [source], now_ms=AT)
    item = next(iter(state["methods"].values()))
    assert source == original
    assert item["source_method"]["exit"] is None
    assert item["receipts"][0]["observation"]["sources"] == card()["sources"]
    assert item["registered_ms"] == item["first_received_ms"] == AT
    assert "exit" in item["missing_fields"]
    assert item["research_status"] == "needs_source_details"
    assert report["financial_execution"] is report["auto_promotion"] is False


def test_complete_description_never_becomes_a_qualified_strategy():
    c = card()
    c["state"]["method_hypothesis"].update({key: "Unverified source text" for key in lab.REQUIRED})
    state, report = lab.update(empty(), [row(c)], now_ms=AT)
    m = next(iter(state["methods"].values()))
    assert m["missing_fields"] == []
    assert m["research_status"] == "needs_formalization"
    assert report["source_claims_verified"] is False


def test_explicitly_unstated_cost_and_source_clock_remain_research_gaps():
    c = card()
    method = c["state"]["method_hypothesis"]
    method.update(dict.fromkeys(lab.REQUIRED, "Unverified source text"))
    method["costs"] = "Not stated; claims up to 20 bots can share one signal"
    c["sources"][0]["retrieved_at"] = None
    state, report = lab.update(empty(), [row(c)], now_ms=AT)
    task = report["tasks"][0]
    assert task["missing_fields"] == ["costs"]
    assert task["missing_evidence_clocks"] == ["sources[0].retrieved_at"]
    assert task["next_action"] == "resolve_missing_source_rules"
    assert state["methods"][task["method_id"]]["source_method"]["costs"] == method["costs"]

    method["costs"] = "0 bps venue fee plus network costs from signed receipts"
    _, report = lab.update(empty(), [row(c)], now_ms=AT)
    task = report["tasks"][0]
    assert task["missing_fields"] == []
    assert task["research_status"] == "needs_source_details"
    assert task["next_action"] == "resolve_missing_source_clocks"


def test_duplicate_claims_keep_original_registration_and_receipts():
    first, _ = lab.update(empty(), [row()], now_ms=AT)
    c = card()
    c["project"] = "A renamed promotion"
    c["state"]["method_hypothesis"]["claimed_performance"] = "New exaggerated claim"
    second, report = lab.update(first, [row(c, received=AT + 1000), row()], now_ms=AT + 2000)
    assert len(second["methods"]) == 1
    m = next(iter(second["methods"].values()))
    assert m["registered_ms"] == AT and m["first_received_ms"] == AT
    assert len(m["receipts"]) == 2
    assert report["duplicates"] == 1
    assert report["new_methods"] == 0


def test_changed_rules_have_new_identity_and_retain_old_failure_evidence():
    state, _ = lab.update(empty(), [row()], now_ms=AT)
    old = copy.deepcopy(state)
    c = card()
    c["state"]["method_hypothesis"]["entry"] = "Different entry rule"
    new, _ = lab.update(state, [row(c)], now_ms=AT + 1000)
    assert len(new["methods"]) == 2
    assert all(new["methods"][key] == value for key, value in old["methods"].items())


@pytest.mark.parametrize("change", ["contract", "track", "research", "identity", "future", "json"])
def test_bad_or_executable_cards_do_not_enter_registry(change):
    c = card()
    if change == "contract":
        c["contract"] = "An asset address"
    if change == "track":
        c["track"] = "token"
    if change == "research":
        c["state"]["research_only"] = False
    r = row(c)
    if change == "identity":
        r["digest"] = "f" * 64
    if change == "future":
        r["received_ms"] = AT + 6000
    if change == "json":
        r["payload"] = "[]"
    state, report = lab.update(empty(), [r], now_ms=AT)
    assert state["methods"] == {}
    assert report["invalid_rows"]["invalid_method_or_receipt"] == 1
    # Positive control: the same incomplete method is retained after repair.
    repaired, _ = lab.update(empty(), [row()], now_ms=AT)
    assert len(repaired["methods"]) == 1


def test_unknown_evidence_clocks_are_explicit():
    c = card()
    c["observed_at"] = None
    c["sources"][0]["retrieved_at"] = None
    state, _ = lab.update(empty(), [row(c)], now_ms=AT)
    m = next(iter(state["methods"].values()))
    assert m["missing_evidence_clocks"] == ["observed_at", "sources[0].retrieved_at"]


def test_capacity_does_not_erase_previous_methods(monkeypatch):
    state, _ = lab.update(empty(), [row()], now_ms=AT)
    monkeypatch.setattr(lab, "MAX_METHODS", 1)
    c = card()
    c["state"]["method_hypothesis"]["exit"] = "A newly supplied exit"
    new, report = lab.update(state, [row(c)], now_ms=AT + 1000)
    assert new["methods"] == state["methods"]
    assert report["deferred"] == 1


def test_actual_receiver_to_worker_is_readonly_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lab.shutil, "disk_usage", lambda _: type("Disk", (), {"used": 1, "total": 10, "free": 10**10})()
    )
    db = tmp_path / "grok-alpha/inbox.db"
    store = Store(db, clock=lambda: AT)
    store.ingest([card()])
    before = db.read_bytes()
    first = lab.run(db, tmp_path / "method-lab", now_ms=AT)
    second = lab.run(db, tmp_path / "method-lab", now_ms=AT + 1000)
    assert db.read_bytes() == before
    assert first["new_methods"] == 1 and second["new_methods"] == 0
    assert second["duplicates"] == 0
    assert second["reader"]["page_rows"] == 0
    assert (tmp_path / "method-lab/worker.lock").is_file()
    with lab.worker_lock(tmp_path / "method-lab/worker.lock"):
        pass
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT state,lease_until_ms FROM revisions").fetchone() == ("unverified", 0)


def test_canonical_database_is_never_opened(tmp_path):
    with pytest.raises(ValueError, match="only_grok_intake_allowed"):
        lab.read_intake(tmp_path / "kaiba.db")


def test_malicious_rule_is_inert_data_and_nonmethods_are_not_signals():
    c = card()
    c["state"]["method_hypothesis"]["entry"] = "__import__('os').system('should never run')"
    unrelated = card()
    unrelated["track"] = "token"
    unrelated["state"] = {"opportunity_type": "token"}
    state, report = lab.update(empty(), [row(c), row(unrelated)], now_ms=AT)
    assert len(state["methods"]) == 1 and report["nonmethods"] == 1
    assert next(iter(state["methods"].values()))["source_method"]["entry"].startswith("__import__")
    assert report["financial_execution"] is False


def test_refresh_never_refreshes_the_original_evidence_clock():
    first, _ = lab.update(empty(), [row()], now_ms=AT)
    _, report = lab.update(first, [], now_ms=AT + 86400000)
    assert report["observed_ms"] == AT + 86400000
    assert report["latest_method_received_ms"] == AT
    assert report["latest_input_received_ms"] is None


def test_real_sender_metadata_does_not_multiply_rules():
    c = card()
    c["state"]["method_hypothesis"].update(
        {"observed_at": "old", "backend": "old", "required_data": ["pool tape"]}
    )
    state, _ = lab.update(empty(), [row(c)], now_ms=AT)
    c["state"]["method_hypothesis"].update({"observed_at": "new", "backend": "new"})
    updated, _ = lab.update(state, [row(c)], now_ms=AT + 1000)
    assert len(updated["methods"]) == 1
    m = next(iter(updated["methods"].values()))
    assert "required_data" not in m["missing_fields"]
    assert len(m["receipts"]) == 2


def test_first_deploy_migration_keeps_old_ids_clocks_and_evidence():
    r = row()
    item = lab.assess(r)
    method = item["source_method"]
    old_id = lab.fingerprint(
        {k: v for k, v in method.items() if k not in {"method_id", "permalinks", "claimed_performance"}}
    )
    legacy = {
        "version": "method-evidence/1",
        "methods": {
            old_id: {
                **item,
                "method_id": old_id,
                "rules_hash": old_id,
                "registered_ms": AT - 100,
                "first_received_ms": AT,
                "receipts": [item["receipt"]],
            }
        },
    }
    migrated, report = lab.update(legacy, [r], now_ms=AT + 1000)
    assert migrated["version"] == lab.VERSION
    m = next(iter(migrated["methods"].values()))
    assert m["registered_ms"] == AT - 100 and m["first_received_ms"] == AT
    assert m["legacy_method_ids"] == [old_id]
    assert m["receipts"] == [item["receipt"]]
    assert report["new_methods"] == 0


def test_intake_read_budget_closes_connection_and_leaves_rows(tmp_path, monkeypatch):
    db = tmp_path / "grok-alpha/inbox.db"
    store = Store(db, clock=lambda: AT)
    c = card()
    c["project"] = "Another record"
    store.ingest([card(), c])
    monkeypatch.setattr(lab, "PAGE_ROWS", 1)
    first = lab.read_intake(db)
    second = lab.read_intake(db, first["next_anchor"])
    assert len(first["rows"]) == len(second["rows"]) == 1
    assert first["has_more"] is True and second["has_more"] is False
    assert first["rows"][0]["key"] != second["rows"][0]["key"]
    with sqlite3.connect(db, timeout=0.15) as con:
        con.execute("BEGIN IMMEDIATE")
        assert con.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 2


def test_another_workers_lock_is_retained(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lab.shutil, "disk_usage", lambda _: type("Disk", (), {"used": 1, "total": 10, "free": 10**10})()
    )
    directory = tmp_path / "method-lab"
    directory.mkdir()
    lock = directory / "worker.lock"
    lock.write_text("a different worker owns this")
    with lab.worker_lock(lock):
        with pytest.raises(OSError):
            lab.run(tmp_path / "grok-alpha/inbox.db", directory, now_ms=AT)
    assert lock.read_text() == "a different worker owns this"


def test_corrupt_checkpoint_preserved_without_regeneration(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lab.shutil, "disk_usage", lambda _: type("Disk", (), {"used": 1, "total": 10, "free": 10**10})()
    )
    directory = tmp_path / "method-lab"
    directory.mkdir()
    checkpoint = directory / "state.json"
    checkpoint.write_text('{"version":"wrong","methods":{}}')
    prior = checkpoint.read_bytes()
    db = tmp_path / "grok-alpha/inbox.db"
    Store(db, clock=lambda: AT).ingest([card()])
    with pytest.raises(ValueError, match="method_checkpoint_invalid"):
        lab.run(db, directory, now_ms=AT)
    assert checkpoint.read_bytes() == prior
    with lab.worker_lock(directory / "worker.lock"):
        pass


def test_disk_guard_refuses_before_input_read(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lab.shutil,
        "disk_usage",
        lambda _: type("Disk", (), {"used": 92 * 1024**3, "total": 110 * 1024**3, "free": 8 * 1024**3})(),
    )
    monkeypatch.setattr(lab, "read_intake", lambda _: pytest.fail("input read while disk admission refused"))
    with pytest.raises(ValueError, match="research_disk_capacity_deferred"):
        lab.run(Path("data/grok-alpha/inbox.db"), tmp_path / "method-lab", now_ms=AT)


def test_lock_is_released_after_process_death_without_finally(tmp_path):
    lock = tmp_path / "worker.lock"
    program = (
        "import os; from pathlib import Path; from kaiba.learning.method_intake import worker_lock\nwith worker_lock(Path("
        + repr(str(lock))
        + ")):\n os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", program], check=True, timeout=15)
    assert lock.exists()
    with lab.worker_lock(lock):
        pass


def test_reserved_blocks_use_the_same_halt_percentage_as_disk_guard(tmp_path, monkeypatch):
    # Raw used/total=85%, but df/guard used/(used+free)=94.4%.
    monkeypatch.setattr(
        lab.shutil,
        "disk_usage",
        lambda _: type("Disk", (), {"used": 85 * 1024**3, "total": 100 * 1024**3, "free": 5 * 1024**3})(),
    )
    monkeypatch.setattr(lab, "read_intake", lambda *a: pytest.fail("disk HALT must refuse before reading"))
    with pytest.raises(ValueError, match="research_disk_capacity_deferred"):
        lab.run(Path("data/grok-alpha/inbox.db"), tmp_path / "method-lab", now_ms=AT)


def test_deleted_anchor_and_reused_rowid_replay_current_rows(tmp_path):
    db = tmp_path / "grok-alpha/inbox.db"
    store = Store(db, clock=lambda: AT)
    store.ingest([card()])
    first = lab.read_intake(db)
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM revisions")
    changed = card()
    changed["project"] = "replacement after retention"
    store.ingest([changed])
    second = lab.read_intake(db, first["next_anchor"])
    assert second["anchor_reset"] is True
    assert len(second["rows"]) == 1
    assert second["next_anchor"]["rowid"] == first["next_anchor"]["rowid"]
    assert second["next_anchor"]["key"] != first["next_anchor"]["key"]


def test_byte_pages_resume_without_skipping_overflow_record(tmp_path, monkeypatch):
    db = tmp_path / "grok-alpha/inbox.db"
    store = Store(db, clock=lambda: AT)
    changed = card()
    changed["project"] = "second source"
    store.ingest([card(), changed])
    monkeypatch.setattr(lab, "PAGE_BYTES", len(lab.encoded(row())) + 20)
    first = lab.read_intake(db)
    assert len(first["rows"]) == 1 and first["has_more"] is True
    second = lab.read_intake(db, first["next_anchor"])
    assert len(second["rows"]) == 1
    assert first["rows"][0]["key"] != second["rows"][0]["key"]


def test_source_larger_than_old_eight_megabyte_limit_is_still_paged(tmp_path):
    db = tmp_path / "grok-alpha/inbox.db"
    Store(db, clock=lambda: AT)
    with sqlite3.connect(db) as con:
        con.executemany(
            "INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?)",
            (
                (f"{i:064x}", "e" * 64, AT, '{"padding":"' + "p" * 9000 + '"}', None, "unverified", 0, None)
                for i in range(1000)
            ),
        )
    assert db.stat().st_size > lab.MAX_BYTES
    page = lab.read_intake(db)
    assert 0 < len(page["rows"]) <= lab.PAGE_ROWS
    assert page["bytes"] <= lab.PAGE_BYTES and page["has_more"] is True


def test_empty_store_after_retention_clears_the_old_anchor(tmp_path):
    db = tmp_path / "grok-alpha/inbox.db"
    store = Store(db, clock=lambda: AT)
    store.ingest([card()])
    first = lab.read_intake(db)
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM revisions")
    empty_page = lab.read_intake(db, first["next_anchor"])
    assert empty_page["rows"] == [] and empty_page["next_anchor"] is None
    assert empty_page["anchor_reset"] is True


def test_capacity_deferred_page_never_advances_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lab.shutil, "disk_usage", lambda _: type("Disk", (), {"used": 1, "total": 10, "free": 10**10})()
    )
    db = tmp_path / "grok-alpha/inbox.db"
    store = Store(db, clock=lambda: AT)
    store.ingest([card()])
    monkeypatch.setattr(lab, "MAX_METHODS", 0)
    directory = tmp_path / "method-lab"
    result = lab.run(db, directory, now_ms=AT)
    assert result["reader"]["blocked"] is True
    assert result["deferred"] == 1
    assert json.loads((directory / "state.json").read_text()).get("intake_cursor") is None
    monkeypatch.setattr(lab, "MAX_METHODS", 256)
    result = lab.run(db, directory, now_ms=AT + 1000)
    assert result["new_methods"] == 1 and result["reader"]["cursor_advanced"] is True
    assert result["status"] == "ok"


def test_rejected_page_advances_and_next_valid_method_is_retained(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lab.shutil, "disk_usage", lambda _: type("Disk", (), {"used": 1, "total": 10, "free": 10**10})()
    )
    monkeypatch.setattr(lab, "PAGE_ROWS", 1)
    db = tmp_path / "grok-alpha/inbox.db"
    Store(db, clock=lambda: AT)
    # Cursor metadata is opaque. A bad identity/clock must be quarantined, not
    # mistaken for a valid method or poison the next cursor validation.
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?)",
            ("bad-key", "bad-digest", -1, "[]", None, "unverified", 0, None),
        )
        valid = row()
        con.execute(
            "INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?)",
            (valid["key"], valid["digest"], AT, valid["payload"], None, "unverified", 0, None),
        )
    directory = tmp_path / "method-lab"
    result = lab.run(db, directory, now_ms=AT + 2000)
    state = json.loads((directory / "state.json").read_text())
    assert result["status"] == "ok" and result["reader"]["cursor_advanced"] is True
    assert result["reader"]["has_more"] is True
    assert result["new_rejections"] == 1 and result["methods"] == 0
    assert result["cursor_received_ms"] is None
    reject = next(iter(state["rejections"].values()))
    assert reject["identity"]["key"] == "bad-key"
    assert reject["identity"]["payload_sha256"] == lab.hashlib.sha256(b"[]").hexdigest()
    assert reject["payload_retained"] is False and "payload" not in reject
    result = lab.run(db, directory, now_ms=AT + 3000)
    assert result["new_methods"] == 1 and result["reader"]["blocked"] is False
    assert result["latest_input_received_ms"] == AT
    idle = lab.run(db, directory, now_ms=AT + 3100)
    assert idle["latest_input_received_ms"] is None
    assert idle["cursor_received_ms"] == AT
    assert idle["observed_ms"] == AT + 3100
    # A retention rebase replays receipts, without changing rejection clocks.
    state = json.loads((directory / "state.json").read_text())
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM revisions WHERE rowid=?", (state["intake_cursor"]["rowid"],))
    result = lab.run(db, directory, now_ms=AT + 4000)
    state = json.loads((directory / "state.json").read_text())
    assert result["reader"]["anchor_reset"] is True and result["new_rejections"] == 0
    assert next(iter(state["rejections"].values())) == reject


def test_metadata_worker_admits_act_band_only_with_actual_filesystem_reserve(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lab.shutil,
        "disk_usage",
        lambda _: type("Disk", (), {"used": 88 * 1024**3, "total": 110 * 1024**3, "free": 12 * 1024**3})(),
    )
    db = tmp_path / "grok-alpha/inbox.db"
    Store(db, clock=lambda: AT).ingest([card()])
    assert lab.run(db, tmp_path / "method-lab", now_ms=AT)["new_methods"] == 1


def test_low_filesystem_reserve_defers_even_below_halt_percentage(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lab.shutil,
        "disk_usage",
        lambda _: type("Disk", (), {"used": 10 * 1024**3, "total": 20 * 1024**3, "free": 4 * 1024**3})(),
    )
    monkeypatch.setattr(lab, "read_intake", lambda *a: pytest.fail("reserve must refuse before reading"))
    with pytest.raises(ValueError, match="research_disk_capacity_deferred"):
        lab.run(Path("data/grok-alpha/inbox.db"), tmp_path / "method-lab", now_ms=AT)


def test_quarantine_capacity_blocks_without_discarding_unrecorded_input(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lab.shutil, "disk_usage", lambda _: type("Disk", (), {"used": 1, "total": 10, "free": 10**10})()
    )
    monkeypatch.setattr(lab, "MAX_REJECTIONS", 0)
    db = tmp_path / "grok-alpha/inbox.db"
    Store(db, clock=lambda: AT)
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?)",
            ("bad-key", "bad-digest", AT, "[]", None, "unverified", 0, None),
        )
    directory = tmp_path / "method-lab"
    report = lab.run(db, directory, now_ms=AT)
    assert report["status"] == "blocked" and report["deferred"] == 1
    assert report["deferred_reasons"] == {"rejection_capacity": 1}
    assert report["new_rejections"] == 0 and report["reader"]["cursor_advanced"] is False
    assert json.loads((directory / "state.json").read_text()).get("intake_cursor") is None
    monkeypatch.setattr(lab, "MAX_REJECTIONS", 4096)
    assert lab.run(db, directory, now_ms=AT)["new_rejections"] == 1


def test_blocked_cli_exits_nonzero_and_reports_capacity_condition(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        lab.shutil, "disk_usage", lambda _: type("Disk", (), {"used": 1, "total": 10, "free": 10**10})()
    )
    monkeypatch.setattr(lab, "MAX_METHODS", 0)
    db = tmp_path / "grok-alpha/inbox.db"
    Store(db, clock=lambda: AT).ingest([card()])
    monkeypatch.setattr(
        sys, "argv", ["method_intake", "--intake", str(db), "--directory", str(tmp_path / "method-lab")]
    )
    with pytest.raises(SystemExit) as exc:
        lab.main()
    assert exc.value.code == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "blocked" and report["reader"]["blocked"] is True
    assert report["deferred_reasons"] == {"method_capacity": 1}
    assert report["financial_execution"] is False


def test_normalizer_upgrade_retains_old_evidence_and_still_checks_new_inputs(monkeypatch):
    first, _ = lab.update(empty(), [row()], now_ms=AT)
    normalizer = lab.normalize
    old_stamps = copy.deepcopy(first["receipt_validation"])
    monkeypatch.setattr(lab, "NORMALIZER_VERSION", "e" * 64)

    def changed_normalizer(card, *, now_ms):
        key, _, raw, expires = normalizer(card, now_ms=now_ms)
        return key, "d" * 64, raw, expires

    monkeypatch.setattr(lab, "normalize", changed_normalizer)
    different = card()
    different["state"]["method_hypothesis"]["entry"] = "A different rule"
    new_input = row(different)
    second, report = lab.update(first, [new_input], now_ms=AT + 1000)
    assert second["methods"] == first["methods"]
    assert second["receipt_validation"] == old_stamps
    assert len(report["receipt_migration_tasks"]) == 1
    assert report["new_rejections"] == 1  # Old-format NEW data is not grandfathered.
    migration = report["receipt_migration_tasks"][0]
    assert migration["from_version"] != migration["to_version"] == "e" * 64
    new_input["digest"] = "d" * 64
    third, report = lab.update(second, [new_input], now_ms=AT + 2000)
    assert report["new_methods"] == 1 and len(third["methods"]) == 2
    assert len(report["receipt_migration_tasks"]) == 1
    assert all(third["receipt_validation"][key] == value for key, value in old_stamps.items())
    assert report["financial_execution"] is report["auto_promotion"] is False


@pytest.mark.parametrize("change", ["claim", "clock", "rules"])
def test_old_normalizer_does_not_allow_stored_evidence_tampering(change, monkeypatch):
    state, _ = lab.update(empty(), [row()], now_ms=AT)
    monkeypatch.setattr(lab, "NORMALIZER_VERSION", "e" * 64)
    receipt = next(iter(state["methods"].values()))["receipts"][0]
    if change == "claim":
        receipt["observation"]["claim"] = "Tampered profit claim"
    if change == "clock":
        receipt["received_ms"] += 1000
    if change == "rules":
        receipt["observation"]["state"]["method_hypothesis"]["entry"] = "Replacement rule"
    with pytest.raises(ValueError, match="receipt_validation_(hash_mismatch|missing)"):
        lab.update(state, [], now_ms=AT + 2000)


def test_legacy_state_gains_validation_provenance_without_changing_methods():
    state, _ = lab.update(empty(), [row()], now_ms=AT)
    del state["receipt_validation"]
    changed, report = lab.update(state, [], now_ms=AT + 1000)
    assert changed["methods"] == state["methods"] and report["receipt_migration_tasks"] == []
    assert len(changed["receipt_validation"]) == 1


def test_caught_cli_failure_persists_unavailable_without_changing_checkpoint(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        lab.shutil, "disk_usage", lambda _: type("Disk", (), {"used": 1, "total": 10, "free": 10**10})()
    )
    db = tmp_path / "grok-alpha/inbox.db"
    Store(db, clock=lambda: AT).ingest([card()])
    directory = tmp_path / "method-lab"
    lab.run(db, directory, now_ms=AT)
    checkpoint = directory / "state.json"
    checkpoint.write_text('{"version":"broken"}')
    before = checkpoint.read_bytes()
    monkeypatch.setattr(sys, "argv", ["method_intake", "--intake", str(db), "--directory", str(directory)])
    with pytest.raises(SystemExit) as exc:
        lab.main()
    assert exc.value.code == 1
    stdout = json.loads(capsys.readouterr().out)
    assert stdout["failure_report_persisted"] is True
    report = json.loads((directory / "latest.json").read_text())
    assert report["status"] == "unavailable" and report["reason"] == "method_checkpoint_invalid"
    assert report["tasks"] == [] and checkpoint.read_bytes() == before
    assert report["checkpoint_sha256"] == lab.hashlib.sha256(before).hexdigest()


def test_failure_report_cannot_overwrite_an_active_or_newer_workers_report(tmp_path):
    directory = tmp_path / "method-lab"
    directory.mkdir()
    latest = directory / "latest.json"
    previous = {"status": "ok", "observed_ms": AT + 1000}
    lab.atomic(latest, previous)
    failure = {"status": "unavailable", "observed_ms": AT + 2000}
    with lab.worker_lock(directory / "worker.lock"):
        assert lab.persist_unavailable(directory, failure) is False
    assert json.loads(latest.read_text()) == previous
    failure["observed_ms"] = AT
    assert lab.persist_unavailable(directory, failure) is False
    assert json.loads(latest.read_text()) == previous


def test_disk_refusal_can_persist_a_small_diagnostic_without_reading_intake(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "method-lab"
    directory.mkdir()
    checkpoint = directory / "state.json"
    lab.atomic(checkpoint, empty())
    before = checkpoint.read_bytes()
    monkeypatch.setattr(
        lab.shutil,
        "disk_usage",
        lambda _: type("Disk", (), {"used": 93 * 1024**3, "total": 105 * 1024**3, "free": 7 * 1024**3})(),
    )
    monkeypatch.setattr(lab, "read_intake", lambda *a: pytest.fail("disk refusal must not read intake"))
    monkeypatch.setattr(sys, "argv", ["method_intake", "--directory", str(directory)])
    with pytest.raises(SystemExit) as exc:
        lab.main()
    assert exc.value.code == 1
    report = json.loads((directory / "latest.json").read_text())
    assert report["status"] == "unavailable" and report["reason"] == "research_disk_capacity_deferred"
    assert report["tasks"] == [] and checkpoint.read_bytes() == before
    assert len((directory / "latest.json").read_bytes()) < 1024
    capsys.readouterr()
