"""Upgrade regressions using the real reader, paper ledger, and worker.

Owned by Codex under AGENT-UPGRADE-20261015. No provider or VPS calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from kaiba.core.schemas import digest
from kaiba.learning import autolearn_io as io
from kaiba.learning.autolearn import AutoLearner, atomic_json

T0 = 1_800_000_000_000
TOKEN = "upgrade-token"


def _db_path(conn):
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _cache(root, at, *, decimals=6, price=".001", depth="1000000"):
    root.mkdir(parents=True, exist_ok=True)
    key = f"gmgn-cli token info --address {TOKEN} --chain sol --raw"
    path = root / (digest(key)[:32] + ".json")
    packet = {"fetched_ms": at, "data": {"address": TOKEN, "decimals": decimals,
              "price": {"address": TOKEN, "price": price}, "liquidity": depth}}
    path.write_text(json.dumps(packet), encoding="utf-8")
    os.utime(path, (at / 1000, at / 1000))
    return path


def _native(conn, at, value="100"):
    conn.execute("INSERT INTO native_prices(chain,ts_ms,price_usd,source) VALUES (?,?,?,?)",
                 ("sol", at, value, "upgrade-fixture"))


def _decision(conn, at):
    conn.execute("INSERT INTO decisions(decision_id,ts_ms,lane,mode,chain,token,action,"
                 "size_base_units,signals_json) VALUES (?,?,?,?,?,?,?,?,?)",
                 ("upgrade-entry", at, "sm-trenches", "live", "sol", TOKEN, "enter", 1, "[]"))


def _books(learner):
    state = _load(learner.state_path)
    manifests = [_load(learner.state_dir / name) for name in state["campaign_files"]]
    return [learner._paper().book_path(m["campaign_id"], arm["hypothesis_id"])
            for m in manifests for arm in m["hypotheses"]]


def _counts(paths):
    counts = []
    for path in paths:
        with sqlite3.connect(path) as conn:
            counts.append(tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                                for t in ("orders", "positions")))
    return counts


@pytest.fixture
def worker(tmp_db, tmp_path, monkeypatch):
    # The only replaced work is expensive historical strategy discovery.
    monkeypatch.setattr(AutoLearner, "_discovery", lambda *args: None)
    clock = {"ms": T0}
    root = tmp_path / "learning"
    learner = AutoLearner(_db_path(tmp_db), root / "worker", learning_root=root,
                          groups=[("sm-trenches", "sol")], max_candidates=2,
                          clock=lambda: clock["ms"] / 1000)
    return learner, clock, tmp_path / "cache" / "gmgn"


def _register_unquoted(worker, conn):
    learner, clock, _ = worker
    assert learner.once()["status"] == "complete"
    clock["ms"] = T0 + 1000
    _decision(conn, clock["ms"])
    report = learner.once()
    assert report["new_opportunities"] == 1
    assert report["captured_quote_count"] == 0
    assert all(c == (0, 0) for c in _counts(_books(learner)))
    return report


def test_cache_capture_time_controls_actual_broker_fill_not_mtime(worker, tmp_db):
    learner, clock, cache = worker
    assert learner.once()["status"] == "complete"
    signal_ms = T0 + 1000
    clock["ms"] = signal_ms
    _decision(tmp_db, signal_ms)
    _native(tmp_db, signal_ms)
    path = _cache(cache, signal_ms)
    pending = learner.once()
    assert pending["captured_quote_count"] == 1
    assert pending["quote_diagnostics"][0]["quote_ts_ms"] == signal_ms
    assert _counts(_books(learner)) == [(0, 0), (0, 0)]

    # The same captured price, touched after the delay, cannot satisfy the delay.
    clock["ms"] = T0 + 31_000
    os.utime(path, (clock["ms"] / 1000, clock["ms"] / 1000))
    touched = learner.once()
    assert touched["status"] == "complete"
    assert touched["quote_diagnostics"][0]["quote_ts_ms"] == signal_ms
    assert _counts(_books(learner)) == [(0, 0), (0, 0)]

    # A separately captured post-delay packet does allow the real PaperBroker.
    clock["ms"] += 1000
    _native(tmp_db, clock["ms"])
    _cache(cache, clock["ms"])
    filled = learner.once()
    assert filled["status"] == "complete"
    assert _counts(_books(learner)) == [(1, 1), (1, 1)]
    for path in _books(learner):
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT opened_ms,cost_native FROM positions").fetchone() == (clock["ms"], "300000000")
            fill = json.loads(conn.execute("SELECT value FROM candidate_meta WHERE key LIKE 'fill_evidence:%'").fetchone()[0])
            assert fill["quote_ms"] == clock["ms"]
            assert fill["cost_model_assumed"] is True
    assert tmp_db.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 0
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert tmp_db.execute("SELECT size_base_units FROM decisions").fetchone()[0] == 1


def test_quote_capture_uses_current_clock_after_book_observation_io(worker, tmp_db, monkeypatch):
    from kaiba.learning.candidate_paper import CandidatePaperRunner

    learner, clock, cache = worker
    _register_unquoted(worker, tmp_db)
    clock["ms"] = T0 + 31_000
    original = CandidatePaperRunner.watched_tokens
    advanced = False

    def observe_with_elapsed_io(self, *args, **kwargs):
        nonlocal advanced
        result = original(self, *args, **kwargs)
        if not advanced:
            advanced = True
            clock["ms"] = T0 + 50_000
            _native(tmp_db, clock["ms"])
            _cache(cache, clock["ms"])
        return result

    monkeypatch.setattr(CandidatePaperRunner, "watched_tokens", observe_with_elapsed_io)
    report = learner.once()
    assert report["status"] == "complete"
    assert report["captured_quote_count"] == 1
    assert report["quote_diagnostics"][0]["quote_ts_ms"] == T0 + 50_000
    assert _counts(_books(learner)) == [(1, 1), (1, 1)]
    assert report["at_ms"] == T0 + 31_000
    assert report["completed_ms"] == T0 + 50_000
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_worker_cannot_backdate_stale_or_expired_fills_after_book_io(worker, tmp_db, monkeypatch):
    from kaiba.learning.candidate_paper import CandidatePaperRunner

    learner, clock, cache = worker
    _register_unquoted(worker, tmp_db)
    clock["ms"] = T0 + 31_000
    _native(tmp_db, clock["ms"])
    _cache(cache, clock["ms"])
    original = CandidatePaperRunner._open

    def open_with_elapsed_io(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        clock["ms"] += 70_000
        return result

    monkeypatch.setattr(CandidatePaperRunner, "_open", open_with_elapsed_io)
    report = learner.once()
    assert report["status"] == "complete"
    assert report["paper_errors"] == []
    assert _counts(_books(learner)) == [(0, 0), (0, 0)]
    entries = []
    for path in _books(learner):
        with sqlite3.connect(path) as conn:
            entries.extend(conn.execute("SELECT status,reason,last_checked_ms,expired_ms FROM candidate_entries"))
    assert len(entries) == 2
    assert entries[0][0] == "pending" and entries[0][2] >= T0 + 101_000
    assert entries[1][0:2] == ("rejected", "latency_unreachable")
    assert entries[1][3] >= T0 + 171_000
    assert report["completed_ms"] >= T0 + 171_000
    assert report["elapsed_ms"] >= 140_000
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_slow_source_failure_backs_off_from_failure_time(worker, monkeypatch):
    from kaiba.learning import autolearn

    learner, clock, _cache_root = worker
    assert learner.once()["status"] == "complete"
    clock["ms"] = T0 + 31_000

    def delayed_source_failure(*args, **kwargs):
        clock["ms"] = T0 + 171_000
        raise io.SourceDeferred("source_query_unavailable:OperationalError")

    monkeypatch.setattr(autolearn, "read_opportunities", delayed_source_failure)
    report = learner.once()
    assert report["status"] == "deferred"
    assert _load(learner.state_path)["retry_after_ms"] == T0 + 231_000
    assert report["at_ms"] == T0 + 31_000
    assert report["completed_ms"] == T0 + 171_000


def test_cache_metadata_cannot_backdate_curve_availability(tmp_db, tmp_path):
    from kaiba.execution.curve_price import RESERVED_TOKEN_ATOMS

    tmp_db.execute("INSERT INTO tokens(chain,address,first_seen_ms) VALUES (?,?,?)", ("sol", TOKEN, T0))
    _native(tmp_db, T0 + 1000)
    real = 700_000_000_000_000
    args = ("sol", TOKEN, T0 + 1000, 30_000_000_000, 60_000_000_000,
            str(real), str(real + RESERVED_TOKEN_ATOMS), "pumpfun")
    tmp_db.execute("INSERT INTO curve_snapshots(chain,token,observed_ms,real_sol_lamports,virtual_sol_lamports,"
                   "real_token_atoms,virtual_token_atoms,source) VALUES (?,?,?,?,?,?,?,?)", args)
    cache = tmp_path / "captured"
    path = _cache(cache, T0 + 2000, price=None, depth=None)
    quote = io.quote_evidence(io.GuardedReader(_db_path(tmp_db)), "sol", TOKEN,
                             now_ms=T0 + 2000, cache_root=cache)
    assert quote["unavailable_reason"] == "pool_price_or_depth_missing_or_stale"
    assert quote["diagnostics"]["metadata"]["fetched_ms"] == T0 + 2000
    assert quote["diagnostics"]["metadata"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()

    # Positive control: an actual subsequent curve capture is now executable.
    tmp_db.execute("UPDATE curve_snapshots SET observed_ms=?", (T0 + 2001,))
    quote = io.quote_evidence(io.GuardedReader(_db_path(tmp_db)), "sol", TOKEN,
                             now_ms=T0 + 2001, cache_root=cache)
    assert quote["basis"] == "captured_curve"
    assert quote["quote_ts_ms"] == quote["curve"]["observed_ms"] == T0 + 2001


def test_late_precision_uses_native_known_by_complete_packet_not_current_native(tmp_db, tmp_path):
    at = T0 + 2000
    _native(tmp_db, T0 + 1000, "100")
    _native(tmp_db, at + 1, "999")
    dossier = {k: {"value": v, "receipt": {"observed_at_ms": T0 + 1000}}
               for k, v in (("price_usd", ".001"), ("liquidity_usd", "1000000"))}
    tmp_db.execute("INSERT INTO token_dossiers(chain,address,built_at_ms,dossier_json) VALUES (?,?,?,?)",
                   ("sol", TOKEN, at + 1, json.dumps(dossier)))
    _cache(tmp_path / "capture", at, price=None, depth=None)
    quote = io.quote_evidence(io.GuardedReader(_db_path(tmp_db)), "sol", TOKEN,
                             now_ms=at + 1, cache_root=tmp_path / "capture")
    assert quote["quote_ts_ms"] == at
    assert quote["price_ts_ms"] == quote["depth_ts_ms"] == T0 + 1000
    assert quote["native_price_ts_ms"] == T0 + 1000 and quote["native_usd"] == "100"


@pytest.mark.parametrize("admission", ["wal", "backoff"])
def test_source_admission_cannot_prevent_local_expiry(worker, tmp_db, admission):
    learner, clock, _ = worker
    pending = _register_unquoted(worker, tmp_db)
    capture = pending["quote_diagnostics"][0]
    assert capture["unavailable_reason"] == "token_decimals_missing_or_invalid"
    books = _books(learner)
    clock["ms"] = T0 + 122_000
    prior_retry = clock["ms"] + 10_000
    if admission == "backoff":
        state = _load(learner.state_path)
        state["retry_after_ms"] = prior_retry
        atomic_json(learner.state_path, state)

    class NoSourceQueries(io.GuardedReader):
        def _wal_bytes(self):
            return io.WAL_LIMIT + 1 if admission == "wal" else 0

        def q(self, *args, **kwargs):
            pytest.fail("source SQL ran during denied admission")

        rows = q

    learner.reader_factory = NoSourceQueries
    report = learner.once()
    assert report["status"] == "deferred" and report["funding_allowed"] is False
    assert report["source_queries"]["count"] == 0
    assert sum(r["expired"] for r in report["local_expiry"]) == 2
    for path in books:
        with sqlite3.connect(path) as conn:
            status, reason, expired, raw = conn.execute(
                "SELECT status,reason,expired_ms,expiry_diagnostics_json FROM candidate_entries").fetchone()
            diagnostics = json.loads(raw)
            assert (status, reason, expired) == ("rejected", "latency_unreachable", clock["ms"])
            assert diagnostics["cause"] == "entry_quote_not_usable_before_deadline"
            assert diagnostics["last_quote_diagnostics"]["capture"] == capture["diagnostics"]
    assert _counts(books) == [(0, 0), (0, 0)]
    state = _load(learner.state_path)
    assert state["retry_after_ms"] == (prior_retry if admission == "backoff" else clock["ms"] + 60_000)


def test_source_change_archives_never_filled_books_without_erasing_old_evidence(worker, tmp_db):
    from kaiba.core import journal

    learner, clock, _ = worker
    _register_unquoted(worker, tmp_db)
    prior = _load(learner.state_path)
    old_path = learner.state_dir / prior["campaign_files"][0]
    manifest = _load(old_path)
    books = _books(learner)
    saved_entries = []
    for path in books:
        with sqlite3.connect(path) as conn:
            saved_entries.append(conn.execute("SELECT * FROM candidate_entries").fetchall())
    manifest["source_versions"]["learning/autolearn_io.py"] = "0" * 64
    atomic_json(old_path, manifest)
    old_bytes = old_path.read_bytes()
    clock["ms"] += 10_000
    report = learner.once()
    assert report["status"] == "complete" and report["funding_allowed"] is False
    state = _load(learner.state_path)
    assert state["campaign_files"] != prior["campaign_files"]
    assert state["selected_ms"] == clock["ms"]
    retired = _load(learner.state_dir / f"retired-{clock['ms']}.json")
    assert retired["campaign_files"] == prior["campaign_files"]
    assert retired["prior_assessment"] == prior["assessment"]
    assert len(retired["book_checks"]) == 2
    assert all(c["orders"] == c["positions"] == 0 for c in retired["book_checks"])
    assert old_path.read_bytes() == old_bytes
    for path, entries in zip(books, saved_entries, strict=True):
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT * FROM candidate_entries").fetchall() == entries
    body = tmp_db.execute("SELECT body FROM journal WHERE subject LIKE 'autolearn:retired:%'").fetchone()[0]
    assert json.loads(body)["type"] == "empty_campaign_retired"
    assert json.loads(body)["funding_allowed"] is False
    assert journal.verify(tmp_db) == (True, None)


def test_source_change_with_real_open_inventory_never_archives_or_recreates_book(worker, tmp_db):
    learner, clock, cache = worker
    learner.once()
    clock["ms"] = T0 + 1000
    _decision(tmp_db, clock["ms"])
    _native(tmp_db, clock["ms"])
    _cache(cache, clock["ms"])
    learner.once()
    clock["ms"] = T0 + 31_000
    _native(tmp_db, clock["ms"])
    _cache(cache, clock["ms"])
    assert learner.once()["status"] == "complete"
    books = _books(learner)
    assert _counts(books) == [(1, 1), (1, 1)]
    state = _load(learner.state_path)
    manifest_path = learner.state_dir / state["campaign_files"][0]
    manifest = _load(manifest_path)
    manifest["source_versions"]["learning/autolearn_io.py"] = "0" * 64
    atomic_json(manifest_path, manifest)
    clock["ms"] += 1000
    report = learner.once()
    assert report["status"] == "partial" and report["funding_allowed"] is False
    assert "changed_campaign_has_broker_history" in report["input_error"]
    assert report["source_queries"]["count"] == 0
    latest = _load(learner.state_dir / "latest-report.json")
    assert latest["at_ms"] == clock["ms"] and latest["status"] == "partial"
    after = _load(learner.state_path)
    assert after["campaign_files"] == state["campaign_files"]
    assert after["assessment"] == state["assessment"]
    assert after["last_attempt_ms"] == clock["ms"]
    assert _counts(books) == [(1, 1), (1, 1)]
    assert list(learner.state_dir.glob("retired-*.json")) == []
    assert not (learner.state_dir / "worker.lock").exists()


@pytest.mark.parametrize("malformed", [[], [1], "not-a-dossier", None])
def test_valid_json_with_wrong_dossier_shape_stays_unavailable_and_reported(worker, tmp_db, malformed):
    learner, clock, _ = worker
    learner.once()
    clock["ms"] = T0 + 1000
    _decision(tmp_db, clock["ms"])
    _native(tmp_db, clock["ms"])
    tmp_db.execute("INSERT INTO tokens(chain,address,first_seen_ms,decimals,migrated_ms) VALUES (?,?,?,?,?)",
                   ("sol", TOKEN, T0, 6, T0))
    tmp_db.execute("INSERT INTO token_dossiers(chain,address,built_at_ms,dossier_json) VALUES (?,?,?,?)",
                   ("sol", TOKEN, clock["ms"], json.dumps(malformed)))
    report = learner.once()
    assert report["status"] in {"complete", "partial"}
    assert report["funding_allowed"] is False and report["captured_quote_count"] == 0
    assert report["quote_diagnostics"][0]["unavailable_reason"] == "pool_price_or_depth_missing_or_stale"
    assert _load(learner.state_dir / "latest-report.json")["at_ms"] == clock["ms"]
    assert _counts(_books(learner)) == [(0, 0), (0, 0)]


def test_actual_scheduled_wallet_intake_and_operating_review_use_distinct_periods(worker, tmp_db):
    from kaiba.core import journal

    learner, clock, _ = worker
    existing, missing = "0x" + "1" * 40, "0x" + "2" * 40
    tmp_db.execute("INSERT INTO wallets(chain,address,name,source,tags_json,first_seen_ms,last_seen_ms,"
                   "cohort,meta_json) VALUES (?,?,?,?,?,?,?,?,?)",
                   ("bsc", existing, "existing tracked identity", "manual-research", '["top_trader"]',
                    T0 - 2000, T0 - 1000, "tracked", '{"custom":"retain"}'))
    original = tuple(tmp_db.execute("SELECT * FROM wallets WHERE chain='bsc' AND address=?", (existing,)).fetchone())
    runner = _db_path(tmp_db).parent / "claude-runners"
    exports = runner / "exports"
    exports.mkdir(parents=True)
    path = exports / "kaiba-bnb-high-tier.txt"
    path.write_text(json.dumps({"list": [{"address": address, "name": "BSC_999KUSD_30dPnL_90wr"}
                                        for address in (existing, missing)]}), encoding="utf-8")
    os.utime(path, (T0 / 1000, T0 / 1000))
    profiles = runner / "bsc"
    profiles.mkdir()
    (profiles / "p30.json").write_text(json.dumps({address: {
        "_at": T0 // 1000, "realized_profit": "1000", "buy": 5}
        for address in (existing, missing)}), encoding="utf-8")

    first = learner.once()
    assert first["status"] == "complete" and first["funding_allowed"] is False
    wallet_path, agent_path = (learner.state_dir / name for name in
                               ("latest-wallet-review.json", "latest-agent-review.json"))
    wallet, agent = _load(wallet_path), _load(agent_path)
    assert wallet["observed_ms"] == agent["at_ms"] == T0
    assert wallet["coverage"]["unique_chain_addresses"] == 2
    assert wallet["coverage"]["provider_positive_count"] == 2
    assert {(row["chain"], row["address"]) for row in wallet["observation_queue"]} == {
        ("bsc", existing), ("bsc", missing)}
    assert all(row["copy_status"] == "COPY_UNPROVEN" and row["provider_calls_authorized"] == 0
               for row in wallet["observation_queue"])
    assert wallet["intake"]["added"] == [{"chain": "bsc", "address": missing, "cohort": "research"}]
    assert wallet["intake"]["daily_count"] == 1 <= wallet["intake"]["daily_limit"] == 20
    assert wallet["intake"]["provider_calls"] == wallet["intake"]["roster_changes"] == 0
    assert agent["economic_profit_verified"] is False and agent["wallet_promotions_authorized"] is False
    assert agent["wallet_review"]["copy_profit_verified"] is False
    assert agent["wallet_review"]["coverage"]["unique_chain_addresses"] == 2
    assert tuple(tmp_db.execute("SELECT * FROM wallets WHERE chain='bsc' AND address=?", (existing,)).fetchone()) == original
    added = tmp_db.execute("SELECT name,source,cohort,tags_json FROM wallets WHERE chain='bsc' AND address=?",
                           (missing,)).fetchone()
    assert tuple(added) == (None, "codex-autolearn-research", "research", "[]")
    day_subject = "autolearn:agent-review:" + str(T0 // io.DAY_MS)
    bodies = tmp_db.execute("SELECT body FROM journal WHERE subject=?", (day_subject,)).fetchall()
    assert len(bodies) == 1
    lesson = json.loads(bodies[0][0])
    assert lesson["type"] == "agent_operating_review" and lesson["funding_allowed"] is False
    assert len(bodies[0][0]) < 3000
    assert lesson["receipt"] == f"agent-review-{T0 // io.DAY_MS}.json"
    receipt_path = learner.state_dir / lesson["receipt"]
    assert _load(receipt_path) == agent
    assert lesson["receipt_hash"] == hashlib.sha256(json.dumps(
        agent, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    receipt_before = receipt_path.read_bytes()
    assert journal.verify(tmp_db) == (True, None)
    wallet_before, agent_before = wallet_path.read_bytes(), agent_path.read_bytes()
    journal_before = tmp_db.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    intake_before = tmp_db.execute("SELECT value FROM kv WHERE key LIKE 'autolearn:wallet-intake:%'").fetchone()[0]

    # A second worker call cannot rerun a current review or import/journal twice.
    again = learner.once()
    assert again["status"] == "complete"
    assert wallet_path.read_bytes() == wallet_before and agent_path.read_bytes() == agent_before
    assert tmp_db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 2
    assert tmp_db.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == journal_before

    # The 15-minute wallet schedule advances while the hourly operating review stays frozen.
    clock["ms"] += 15 * 60_000
    later = learner.once()
    assert later["status"] == "complete"
    assert _load(wallet_path)["observed_ms"] == clock["ms"]
    assert _load(wallet_path)["intake"]["added"] == []
    assert _load(wallet_path)["intake"]["daily_count"] == 1
    assert agent_path.read_bytes() == agent_before
    assert receipt_path.read_bytes() == receipt_before
    assert tmp_db.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == journal_before
    assert tmp_db.execute("SELECT value FROM kv WHERE key LIKE 'autolearn:wallet-intake:%'").fetchone()[0] == intake_before
    assert tuple(tmp_db.execute("SELECT * FROM wallets WHERE chain='bsc' AND address=?", (existing,)).fetchone()) == original
    assert all(c == (0, 0) for c in _counts(_books(learner)))
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert journal.verify(tmp_db) == (True, None)

    # Crash window: journal and receipt were committed, caller acknowledgements were lost.
    state = _load(learner.state_path)
    assert state["pending_lessons"] == []
    state.pop("agent_review_journal_day")
    state.pop("last_agent_review_ms")
    atomic_json(learner.state_path, state)
    clock["ms"] += 60 * 60_000
    recovered = learner.once()
    assert recovered["status"] == "complete"
    assert _load(agent_path)["at_ms"] == clock["ms"]
    assert agent_path.read_bytes() != agent_before
    assert receipt_path.read_bytes() == receipt_before
    retained = _load(receipt_path)
    assert retained["at_ms"] == T0
    after_bodies = tmp_db.execute("SELECT body FROM journal WHERE subject=?", (day_subject,)).fetchall()
    assert len(after_bodies) == 1 and after_bodies[0][0] == bodies[0][0]
    assert json.loads(after_bodies[0][0])["receipt_hash"] == hashlib.sha256(json.dumps(
        retained, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    acknowledged = _load(learner.state_path)
    assert acknowledged["agent_review_journal_day"] == T0 // io.DAY_MS
    assert acknowledged["last_agent_review_ms"] == clock["ms"]
    assert acknowledged["pending_lessons"] == []
    assert recovered["journal"]["repeated"] == 1
    assert tmp_db.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == journal_before
    assert tmp_db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 2
    assert journal.verify(tmp_db) == (True, None)

