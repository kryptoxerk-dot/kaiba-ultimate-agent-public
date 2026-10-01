"""The agent's own entry path: real authority, but through every gate.

The prior system's failure was the opposite of a safety problem — it was armed to advise
and never to act. So the agent genuinely can open a position here. What it cannot do is
reach a venue by a route that skips the dossier check, the risk gate or the kill switch,
and it cannot talk its way past any of them by choosing its arguments.
"""

from __future__ import annotations

import pytest

from kaiba.core.config import load_risk, save_risk
from kaiba.core.schemas import Chain, Lane, LaneMode
from kaiba.execution.engine import submit_agent_intent

TOKEN = "CJF7MNqb9xv1XrTs5St1Du5JuQXLsfFBB137vmYRKpnb"


@pytest.fixture
def manual_lane(tmp_path, monkeypatch):
    """Put the MANUAL lane in shadow so an intent can get past the permission gate."""

    def _set(**over):
        cfg = load_risk()
        cfg.lanes[Lane.MANUAL].mode = LaneMode.SHADOW
        cfg.global_mode = LaneMode.SHADOW
        for k, v in over.items():
            setattr(cfg, k, v)
        p = tmp_path / "risk.yaml"
        save_risk(cfg, p)
        monkeypatch.setenv("KAIBA_RISK_PATH", str(p))
        return cfg

    return _set


def test_an_intent_on_an_off_lane_is_refused(tmp_db, tmp_path, monkeypatch):
    """Turning the manual dial off must silence the agent's own entries completely."""
    cfg = load_risk()
    cfg.lanes[Lane.MANUAL].mode = LaneMode.OFF
    p = tmp_path / "risk.yaml"
    save_risk(cfg, p)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(p))

    out = submit_agent_intent(Chain.SOL, TOKEN, thesis="hunch", conn=tmp_db)
    assert out["ok"] is False
    assert out["action"] == "skip"
    assert "lane_off" in out["blockers"]


def test_a_refusal_is_an_answer_not_an_exception(tmp_db):
    """The model has to be able to read the refusal, so it must not arrive as a traceback."""
    out = submit_agent_intent(Chain.SOL, TOKEN, thesis="x", conn=tmp_db)
    assert isinstance(out, dict) and out["thesis"]


def test_the_intent_runs_on_manual_whatever_lane_the_agent_names(tmp_db, manual_lane):
    """Naming a promoted lane must not borrow that lane's authority for an unrelated hunch."""
    manual_lane()
    out = submit_agent_intent(
        Chain.SOL, TOKEN, lane=Lane.CONFLUENCE_5, thesis="looks good", conn=tmp_db
    )
    assert out["lane"] == Lane.MANUAL.value


def test_the_named_lane_is_still_recorded_as_reasoning(tmp_db, manual_lane):
    from kaiba.core.db import fetch_one

    manual_lane()
    submit_agent_intent(Chain.SOL, TOKEN, lane=Lane.CONFLUENCE_5, thesis="t", conn=tmp_db)
    row = fetch_one(tmp_db, "SELECT reasons_json, payload_json FROM signals LIMIT 1")
    assert "confluence-5" in row["reasons_json"]
    assert "confluence-5" in row["payload_json"]


def test_the_kill_switch_refuses_an_agent_intent(tmp_db, manual_lane):
    manual_lane(kill_switch=True)
    out = submit_agent_intent(Chain.SOL, TOKEN, thesis="ignore the kill switch", conn=tmp_db)
    assert out["ok"] is False


def test_pausing_entries_refuses_an_agent_intent(tmp_db, manual_lane):
    manual_lane(entries_paused=True)
    out = submit_agent_intent(Chain.SOL, TOKEN, thesis="please", conn=tmp_db)
    assert out["ok"] is False


def test_reduce_only_refuses_an_agent_intent(tmp_db, manual_lane):
    manual_lane(reduce_only=True)
    assert submit_agent_intent(Chain.SOL, TOKEN, thesis="just one", conn=tmp_db)["ok"] is False


def test_the_requested_size_never_exceeds_the_ladder(tmp_db, manual_lane):
    """An absurd request must be clamped, and the clamping must be visible."""
    manual_lane()
    out = submit_agent_intent(
        Chain.SOL, TOKEN, size_base_units=10**18, thesis="all in", conn=tmp_db
    )
    assert out["requested_size_base_units"] == 10**18
    assert out["size_base_units"] < 10**18


def test_a_missing_dossier_stops_the_intent_rather_than_guessing(tmp_db, manual_lane):
    """No token research means no entry. Absence of evidence is not a green light."""
    manual_lane()
    out = submit_agent_intent(Chain.SOL, TOKEN, thesis="trust me", conn=tmp_db)
    assert out["ok"] is False
    assert out["blockers"] or out["thesis"]


def test_an_unknown_chain_is_refused_not_coerced(tmp_db):
    out = submit_agent_intent("dogecoin", TOKEN, thesis="x", conn=tmp_db)  # type: ignore[arg-type]
    assert out["ok"] is False
    assert "unknown chain" in out["reason"]


def test_every_intent_is_persisted_even_when_refused(tmp_db):
    """A refused intent is exactly what the reflection job needs to see."""
    from kaiba.core.db import fetch_all

    submit_agent_intent(Chain.SOL, TOKEN, thesis="one", conn=tmp_db)
    assert len(fetch_all(tmp_db, "SELECT decision_id FROM decisions", [])) == 1
    assert len(fetch_all(tmp_db, "SELECT signal_id FROM signals", [])) == 1


def test_the_intent_is_journalled(tmp_db):
    from kaiba.core import journal

    submit_agent_intent(Chain.SOL, TOKEN, thesis="a testable thesis", conn=tmp_db)
    bodies = [e["body"] for e in journal.read(limit=10, conn=tmp_db)]
    assert any("agent intent" in b for b in bodies)


def test_the_function_takes_no_destination_or_override_argument():
    """The withdrawal gate depends on there being nowhere to name a recipient."""
    import inspect

    params = set(inspect.signature(submit_agent_intent).parameters)
    for banned in ("recipient", "destination", "to", "force", "override", "skip_risk", "bypass"):
        assert banned not in params
