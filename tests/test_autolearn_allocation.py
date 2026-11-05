"""Real worker/broker/journal with one authority across flagged and default paths."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from kaiba.core import journal
from kaiba.learning import allocation_runtime as runtime
from kaiba.learning import autolearn_clock, research_allocation
from kaiba.learning.autolearn import AutoLearner
from kaiba.learning.autolearn_io import GuardedReader
from tests.test_allocation_runtime import packed, provision_runtime, snapshot
from tests.test_research_allocation import rig


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("external transport forbidden"))


def worker_fixture(tmp_path, tmp_db, *, admission_clock=False):
    value = provision_runtime(rig(tmp_path / "research"))
    value["root"].joinpath("worker.lock").unlink()
    database = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
    learner = AutoLearner(database, value["root"], learning_root=value["learning"],
        groups=[("sm-trenches", c) for c in ("sol", "bsc", "robinhood")],
        admission_clock=admission_clock, clock=lambda: value["clock"]["ms"] / 1000)
    return value, learner


def canonical_quote(conn, value):
    now, token = value["clock"]["ms"], value["token"]
    conn.execute("INSERT INTO tokens(chain,address,decimals,first_seen_ms,migrated_ms) VALUES(?,?,?,?,?) "
        "ON CONFLICT(chain,address) DO UPDATE SET decimals=excluded.decimals", ("robinhood", token, 6, value["t0"], value["t0"]))
    conn.execute("INSERT INTO native_prices(chain,ts_ms,price_usd,source) VALUES(?,?,?,?)",
                 ("eth", now, "1000", "offline-fixture"))
    def measure(amount):
        return {"value": amount, "receipt": {"observed_at_ms": now}}
    dossier = {"price_usd": measure("1"), "liquidity_usd": measure("1000000"),
               "buy_tax_bps": measure("0"), "sell_tax_bps": measure("0")}
    conn.execute("INSERT INTO token_dossiers(chain,address,built_at_ms,dossier_json) VALUES(?,?,?,?) "
        "ON CONFLICT(chain,address) DO UPDATE SET built_at_ms=excluded.built_at_ms,dossier_json=excluded.dossier_json",
        ("robinhood", token, now, json.dumps(dossier)))


@pytest.mark.parametrize("flag", [False, True])
def test_real_worker_uses_twelve_leases_joint_twenty_two_and_never_reopens_archives(tmp_path, tmp_db, monkeypatch, flag):
    value, learner = worker_fixture(tmp_path, tmp_db, admission_clock=flag)
    monkeypatch.setattr(autolearn_clock, "bootstrap_clock_cohort", lambda *a, **k: pytest.fail("old controller invoked"))
    archives = {r["book"]: (value["books"] / r["book"]).read_bytes()
                for r in value["receipt"]["legacy_books"] if r["lease"] == "retired"}
    signal = value["activation"] + 1
    tmp_db.execute("INSERT INTO signals(signal_id,lane,chain,token,created_ms,payload_json) VALUES(?,?,?,?,?,?)",
        ("allocation-signal", "sm-trenches", "robinhood", value["token"], signal - 1, "{}"))
    tmp_db.execute("INSERT INTO decisions(decision_id,ts_ms,lane,mode,chain,token,action,size_base_units,signals_json) "
        "VALUES(?,?,?,?,?,?,?,?,?)", ("allocation-entry", signal, "sm-trenches", "live", "robinhood", value["token"],
                                   "enter", "1", '["allocation-signal"]'))
    canonical_quote(tmp_db, value)
    original = research_allocation.assess_family
    calls = []

    def assessment(*args, **kwargs):
        calls.append(copy.deepcopy(args[1]))
        return original(*args, **kwargs)

    monkeypatch.setattr(research_allocation, "assess_family", assessment)
    first = learner.once()
    assert first["status"] == "complete", first
    assert len(calls) == 1 and len(calls[0]) == 2
    assert first["allocation_assessment"]["family_size"] == 22
    assert sum(a["open"] for c in first["campaigns"] for a in c["paper"]["arms"]) > 2
    assert all(c["assessment"]["authority"] == "joint_lifetime_family" for c in first["campaigns"])
    assert all("local_assessment" in c for c in first["campaigns"])
    value["clock"]["ms"] += 74_000
    canonical_quote(tmp_db, value)
    second = learner.once()
    assert second["status"] == "complete", second
    assert len(calls) == 2
    assert any(r["status"] == "closed" for c in second["campaigns"] if c["campaign_id"] == value["new"]["campaign_id"]
               for r in c["paper"]["assessment_rows"])
    state = json.loads(learner.state_path.read_text())
    assert state["campaign_files"] == value["parent"]["campaign_files"]
    assert state["allocation_family_assessment"]["receipt_sha256"] == value["receipt"]["receipt_sha256"]
    assert state[runtime.POINTER] == value["parent"][runtime.POINTER]
    assert all((value["books"] / name).read_bytes() == raw for name, raw in archives.items())
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert tmp_db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    assert journal.verify(tmp_db) == (True, None)


def test_unflagged_direct_factory_bootstrap_and_discovery_require_same_authority(tmp_path, tmp_db, monkeypatch):
    value, learner = worker_fixture(tmp_path, tmp_db)
    monkeypatch.setattr(autolearn_clock, "bootstrap_clock_cohort", lambda *a, **k: pytest.fail("old controller invoked"))
    assert isinstance(learner._paper(), runtime.BoundRunner)
    assert len(learner._bootstrap(value["parent"], value["clock"]["ms"])) == 2
    reader = GuardedReader(learner.db_path)
    result = learner._discovery(reader, value["parent"], value["clock"]["ms"])
    assert result["reason"] == "allocation_generation_has_no_additional_leases" and reader.queries == 0
    (value["runtime_dir"] / "authority.json").unlink()
    before = snapshot(value["books"])
    for call in (learner._paper, lambda: learner._bootstrap(value["parent"], value["clock"]["ms"]),
                 lambda: learner._discovery(reader, value["parent"], value["clock"]["ms"])):
        with pytest.raises(ValueError):
            call()
    failed = learner.once()
    assert failed["status"] == "partial" and failed["source_queries"]["count"] == 0
    assert snapshot(value["books"]) == before


def test_lost_parent_with_allocation_artifacts_never_creates_fresh_state_or_books(tmp_path, tmp_db):
    value, learner = worker_fixture(tmp_path, tmp_db)
    learner.state_path.unlink()
    before = snapshot(value["books"])
    result = learner.once()
    assert result["status"] == "partial" and result["source_queries"]["count"] == 0
    assert not learner.state_path.exists() and snapshot(value["books"]) == before


def test_pointer_lost_but_directory_present_never_falls_back_to_unflagged_legacy(tmp_path, tmp_db):
    value, learner = worker_fixture(tmp_path, tmp_db)
    parent = copy.deepcopy(value["parent"])
    parent.pop(runtime.POINTER)
    learner.state_path.write_bytes(packed(parent))
    original = learner.state_path.read_bytes()
    failed = learner.once()
    assert failed["status"] == "partial" and failed["source_queries"]["count"] == 0
    assert learner.state_path.read_bytes() == original
