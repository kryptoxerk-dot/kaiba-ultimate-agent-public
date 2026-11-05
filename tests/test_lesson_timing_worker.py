"""Actual 22-book worker and disposable canonical credit writer, fully offline.

Only venue evidence is synthetic. Existing pending credit is retained when the
same actual book outcome later fails the frozen admission timing policy.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from kaiba.core import journal
from kaiba.learning import allocation_runtime, autolearn, autolearn_io, tax_quote
from tests.test_autolearn_tax_quote import add_decision, db_path, quote
from tests.test_lesson_timing_upgrade import provision_lesson_runtime
from tests.test_research_service_upgrade import originals


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("external dispatch forbidden"))


def _saved(path):
    return json.loads(path.read_bytes())


@pytest.mark.parametrize("prior_credit_without_journal_ack", [False, True])
def test_actual_worker_requalifies_retained_pending_without_reversing_history(
        tmp_path, tmp_db, monkeypatch, prior_credit_without_journal_ack):
    value = provision_lesson_runtime(tmp_path, monkeypatch)
    (value["root"] / "worker.lock").unlink()
    old_five = originals(value)
    historic = {path: path.read_bytes() for folder in
                ("paper-service-upgrade", "tax-quote-upgrade", "configured-receipts-upgrade")
                for path in (value["runtime_dir"] / folder).rglob("*") if path.is_file()}
    retired = {value["books"] / row["book"]: (value["books"] / row["book"]).read_bytes()
               for row in value["receipt"]["legacy_books"] if row["lease"] == "retired"}
    learner = autolearn.AutoLearner(db_path(tmp_db), value["root"], learning_root=value["learning"],
        clock=lambda: value["clock"]["ms"] / 1000)
    original_quote = tax_quote.quote_evidence

    def synthetic_venue(*args, **kwargs):
        captured = original_quote(*args, **kwargs)
        if not captured.get("unavailable_reason"):
            captured.update(venue_fee_bps=60, venue_fee_source="synthetic-test-venue",
                            venue_fee_ts_ms=captured["quote_ts_ms"])
        return captured

    monkeypatch.setattr(tax_quote, "quote_evidence", synthetic_venue)
    real_persist = autolearn.persist_lessons

    def keep_outbox(*args, **kwargs):
        # No credit commits here. A separate control below exercises the actual
        # writer's historical credit-before-journal failure.
        raise sqlite3.OperationalError("synthetic writer contention before any commit")

    monkeypatch.setattr(autolearn, "persist_lessons", keep_outbox)
    value["clock"]["ms"] = value["activation"] + 40_000
    quote(tmp_db, value["clock"], "hundred")
    add_decision(tmp_db, value["token"], signal=value["activation"] + 1)
    opened = learner.once()
    assert not opened.get("paper_errors"), opened
    value["clock"]["ms"] += 74_000
    quote(tmp_db, value["clock"], "hundred")
    closed = learner.once()
    assert not closed.get("paper_errors"), closed
    new = next(c for c in closed["campaigns"] if c["campaign_id"] == value["new"]["campaign_id"])
    row = next(r for r in new["paper"]["outcomes"] if r["cost_complete"] and r["timing_complete"])
    state = _saved(learner.state_path)
    pending = {lesson["evidence_id"]: lesson for lesson in state["pending_lessons"]}
    bad = copy.deepcopy(pending[row["outcome_id"]])
    good = next(copy.deepcopy(lesson) for lesson in pending.values()
                if lesson.get("outcome") in ("hit", "miss") and lesson["evidence_id"] != bad["evidence_id"])
    operating = {"evidence_id": "timing-test-operating-note", "lane": None,
                 "text": "Synthetic noncredit operating note for the offline timing regression.",
                 "body": json.dumps({"type": "synthetic_operating_note", "funding_allowed": False})}
    state["pending_lessons"] = [bad, operating] + [lesson for lesson in state["pending_lessons"]
                                                 if lesson["evidence_id"] != bad["evidence_id"]]
    learner.state_path.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    arm = next(a for a in new["paper"]["arms"] if a["hypothesis_id"] == row["hypothesis_id"])
    with closing(sqlite3.connect(arm["book_path"])) as book, book:
        fill_key = "fill_evidence:" + row["fill_evidence"][0]["order_id"]
        original_fill = json.loads(book.execute("SELECT value FROM candidate_meta WHERE key=?", (fill_key,)).fetchone()[0])
        invalid_fill = {**original_fill, "price_ts_ms": row["signal_ms"] + 1}
        # Change only evidence clocks. Cashflow, order identity, position,
        # original manifests and all allocation artifacts remain untouched.
        book.execute("UPDATE candidate_meta SET value=? WHERE key=?", (json.dumps(invalid_fill), fill_key))

    if prior_credit_without_journal_ack:
        real_append = journal.append

        def fail_ack(*args, **kwargs):
            kind = args[0] if args else kwargs.get("kind")
            if kind == "lesson" and kwargs.get("subject") == "autolearn:" + bad["evidence_id"]:
                assert tmp_db.execute("SELECT COUNT(*) FROM playbook_hits WHERE ref=?",
                                      (bad["evidence_id"],)).fetchone()[0] == 1
                key = "autolearn:evidence:" + hashlib.sha256(bad["evidence_id"].encode()).hexdigest()
                assert json.loads(tmp_db.execute("SELECT value FROM kv WHERE key=?",
                                                (key,)).fetchone()[0])["journal_seq"] is None
                raise sqlite3.OperationalError("synthetic lost journal acknowledgment after committed credit")
            return real_append(*args, **kwargs)

        with monkeypatch.context() as patcher:
            patcher.setattr(journal, "append", fail_ack)
            with pytest.raises(sqlite3.OperationalError):
                autolearn_io.persist_lessons(db_path(tmp_db), [bad])
        assert tmp_db.execute("SELECT COUNT(*) FROM playbook_hits WHERE ref=?", (bad["evidence_id"],)).fetchone()[0] == 1

    monkeypatch.setattr(autolearn, "persist_lessons", real_persist)
    value["clock"]["ms"] = max(value["clock"]["ms"] + 61_000, int(state.get("journal_retry_after_ms", 0)) + 1)
    quote(tmp_db, value["clock"], "hundred")
    checked = learner.once()
    assert checked["status"] == "partial", checked
    assert not checked.get("paper_errors"), checked
    latest = next(c for c in checked["campaigns"] if c["campaign_id"] == value["new"]["campaign_id"])
    rejected = next(r for r in latest["paper"]["outcomes"] if r["outcome_id"] == bad["evidence_id"])
    assert rejected["cost_complete"] is True and rejected["timing_complete"] is False
    assert learner._lessons(value["new"], {"outcomes": [rejected]}) == []
    retained = _saved(learner.state_path)["pending_lessons"]
    assert next(lesson for lesson in retained if lesson["evidence_id"] == bad["evidence_id"]) == bad
    assert not any(lesson["evidence_id"] in (good["evidence_id"], operating["evidence_id"]) for lesson in retained)
    expected_bad_credits = int(prior_credit_without_journal_ack)
    assert tmp_db.execute("SELECT COUNT(*) FROM playbook_hits WHERE ref=?", (bad["evidence_id"],)).fetchone()[0] == expected_bad_credits
    assert tmp_db.execute("SELECT COUNT(*) FROM journal WHERE subject=?", ("autolearn:" + bad["evidence_id"],)).fetchone()[0] == 0
    assert tmp_db.execute("SELECT COUNT(*) FROM playbook_hits WHERE ref=?", (good["evidence_id"],)).fetchone()[0] == 1
    assert tmp_db.execute("SELECT COUNT(*) FROM journal WHERE subject=?", ("autolearn:" + operating["evidence_id"],)).fetchone()[0] == 1

    value["clock"]["ms"] += 1000
    quote(tmp_db, value["clock"], "hundred")
    again = learner.once()
    assert again["status"] == "partial" and not again.get("paper_errors"), again
    assert tmp_db.execute("SELECT COUNT(*) FROM playbook_hits WHERE ref=?", (bad["evidence_id"],)).fetchone()[0] == expected_bad_credits
    assert tmp_db.execute("SELECT COUNT(*) FROM playbook_hits WHERE ref=?", (good["evidence_id"],)).fetchone()[0] == 1
    assert next(lesson for lesson in _saved(learner.state_path)["pending_lessons"] if lesson["evidence_id"] == bad["evidence_id"]) == bad
    context = allocation_runtime.load_context(value["root"], learning_root=value["learning"],
        now_ms=value["clock"]["ms"], clock=lambda: value["clock"]["ms"] / 1000)
    assert context.lesson_timing_upgrade is not None
    assert context.authority["activation_ms"] == value["activation"]
    assert len(context.leased_pairs()) == 12 and sum(len(m["hypotheses"]) for m in context.manifests()) == 22
    assert originals(value) == old_five and all(path.read_bytes() == raw for path, raw in historic.items())
    assert all(path.read_bytes() == raw for path, raw in retired.items())
    assert all(tmp_db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
               for table in ("orders", "positions", "trades"))
    assert journal.verify(tmp_db) == (True, None)
    assert checked["funding_allowed"] is again["funding_allowed"] is False
