import importlib.util
import json
import sqlite3
from pathlib import Path

from kaiba.hunters.grok_alpha_bridge import Store


def module():
    spec = importlib.util.spec_from_file_location("grok_monitor", Path(__file__).parents[1]/"deploy/grok-alpha-monitor.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_monitor_never_creates_a_missing_store(tmp_path, monkeypatch, capsys):
    monitor = module()
    monkeypatch.setattr(monitor, "ROOT", tmp_path)
    monitor.main()
    assert json.loads(capsys.readouterr().out)["delivery"] == "intake_unavailable"
    assert list(tmp_path.iterdir()) == []


def test_readonly_snapshot_does_not_modify_store(tmp_path):
    path = tmp_path/"inbox.db"
    Store(path)
    before = path.read_bytes()
    assert Store(path, readonly=True).status()["delivery"] == "no_receipt"
    assert path.read_bytes() == before


def test_unchanged_heartbeat_clocks_do_not_wake_model_but_health_and_pending_do():
    monitor = module()
    one = {"pending": [], "delivery": "recent_receipt", "last_delivery_ms": 100,
           "sender_reported_run": {"run_id": "1", "status": "complete"}}
    two = {**one, "last_delivery_ms": 200, "sender_reported_run": {"run_id": "2", "status": "complete"}}
    assert monitor.payload(one) == monitor.payload(two)
    assert monitor.payload(one) != monitor.payload({**two, "delivery": "stale"})
    # Arrival alone belongs to the deterministic processor, not an early model wake.
    assert monitor.payload(one) == monitor.payload({**two, "pending": [{"key": "new"}]})
    assert monitor.payload(one) != monitor.payload(two, [{"observation_id": "new"}], "ready")


def test_model_handoff_waits_for_classification_and_tracks_review_not_lease(tmp_path):
    from tests.test_grok_alpha_bridge import NOW, card

    monitor = module()
    store = Store(tmp_path / "inbox.db", clock=lambda: NOW)
    nft, token = store.ingest([card("nft"), card("token")])["accepted"]
    reader = Store(store.path, readonly=True, clock=lambda: NOW)
    path = tmp_path / "pipeline.db"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE pipeline(observation_id TEXT, revision TEXT,next_action TEXT)")
    assert monitor.model_handoff(reader, path) == []
    with sqlite3.connect(path) as con:
        con.executemany("INSERT INTO pipeline VALUES(?,?,?)", [
            (nft["observation_id"], nft["revision"], "model_verify_official_terms"),
            (token["observation_id"], token["revision"], "follow_order")])
    ready = monitor.model_handoff(reader, path)
    assert len(ready) == 1 and ready[0]["observation_id"] == nft["observation_id"]
    store.claim(only=[(nft["observation_id"], nft["revision"])])
    assert monitor.model_handoff(reader, path) == ready
    store.review(nft["observation_id"], nft["revision"], "watch", "Official terms checked; unresolved cost")
    assert monitor.model_handoff(reader, path) == []


def test_missing_pipeline_is_reported_without_creation(tmp_path, monkeypatch, capsys):
    monitor = module()
    monkeypatch.setattr(monitor, "ROOT", tmp_path)
    Store(tmp_path / "data/grok-alpha/inbox.db")
    monitor.main()
    result = json.loads(capsys.readouterr().out)
    assert result["pipeline"] == "unavailable"
    assert not (tmp_path / "data/grok-alpha/pipeline.db").exists()
