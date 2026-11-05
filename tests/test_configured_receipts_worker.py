"""Real PAPER worker with the complete configured-receipt provenance chain.

No provider, network, financial order or installed state is used. The integration
keeps the original 22-arm research family and its missing-cost qualifications.
"""
from __future__ import annotations

import json

import pytest

from kaiba.learning import allocation_runtime, autolearn
from kaiba.learning import configured_receipts_upgrade as upgrade
from tests.test_autolearn_tax_quote import add_decision, db_path, quote
from tests.test_configured_receipts_upgrade import provision_configured_runtime
from tests.test_research_service_upgrade import originals


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("external dispatch forbidden"))


def test_actual_worker_preserves_family_history_and_unknown_costs_after_receipt_upgrade(
        tmp_path, tmp_db, monkeypatch):
    value = provision_configured_runtime(tmp_path, monkeypatch)
    (value["root"] / "worker.lock").unlink()
    before = originals(value)
    historic = {}
    for folder in ("paper-service-upgrade", "tax-quote-upgrade", upgrade.DIRECTORY):
        for path in (value["runtime_dir"] / folder).rglob("*"):
            if path.is_file():
                historic[path] = path.read_bytes()
    retired = {value["books"] / row["book"]: (value["books"] / row["book"]).read_bytes()
               for row in value["receipt"]["legacy_books"] if row["lease"] == "retired"}
    learner = autolearn.AutoLearner(db_path(tmp_db), value["root"], learning_root=value["learning"],
        clock=lambda: value["clock"]["ms"] / 1000)
    value["clock"]["ms"] = value["activation"] + 40_000
    context = allocation_runtime.load_context(value["root"], learning_root=value["learning"],
        now_ms=value["clock"]["ms"], clock=lambda: value["clock"]["ms"] / 1000)
    assert context.configured_receipts_upgrade is not None
    assert context.tax_quote_upgrade is not None
    assert len(context.leased_pairs()) == 12 and len(context.retired_history()) == 10
    assert context.authority["activation_ms"] == value["activation"]
    token, raw = quote(tmp_db, value["clock"], "hundred")
    assert token == value["token"]
    add_decision(tmp_db, token, signal=value["activation"] + 1)
    opened = learner.once()
    assert opened["status"] == "complete" and not opened["paper_errors"], opened
    assert opened["allocation_assessment"]["family_size"] == 22
    assert opened["quote_diagnostics"][0]["buy_tax_bps"] == 100
    assert opened["quote_diagnostics"][0]["sell_tax_bps"] == 100
    new = next(c for c in opened["campaigns"] if c["campaign_id"] == value["new"]["campaign_id"])
    assert sum(arm["open"] for arm in new["paper"]["arms"]) > 0
    assert tmp_db.execute("SELECT dossier_json FROM token_dossiers").fetchone()[0] == raw
    value["clock"]["ms"] += 74_000
    quote(tmp_db, value["clock"], "hundred")
    closed = learner.once()
    assert closed["status"] == "complete" and closed["allocation_assessment"]["family_size"] == 22, closed
    new = next(c for c in closed["campaigns"] if c["campaign_id"] == value["new"]["campaign_id"])
    rows = new["paper"]["outcomes"]
    assert rows and all(row["tax_complete"] is True for row in rows)
    assert all(row["venue_complete"] is False and row["cost_complete"] is False for row in rows)
    assert all(row["live_cost_verified"] is False for row in rows)
    parent = json.loads(learner.state_path.read_bytes())
    for key in ("research_allocation", "paper_service_upgrade", "tax_quote_upgrade", upgrade.POINTER):
        assert parent[key] == value["parent"][key]
    assert parent["selected_ms"] == value["parent"]["selected_ms"]
    assert originals(value) == before
    assert all(path.read_bytes() == body for path, body in historic.items())
    assert all(path.read_bytes() == body for path, body in retired.items())
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert tmp_db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0

    # A previously acquired Context cannot use a changed receipt leaf as authority.
    receipt_source = value["code"] / upgrade.CHANGED[1]
    frozen = receipt_source.read_bytes()
    try:
        receipt_source.write_bytes(frozen + b"\n# foreign receipt source\n")
        with pytest.raises(ValueError, match="configured_receipt_source_changed"):
            context.revalidate()
    finally:
        receipt_source.write_bytes(frozen)
