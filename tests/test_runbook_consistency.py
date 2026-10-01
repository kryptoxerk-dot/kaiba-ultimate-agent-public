from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_kill_switch_runbooks_describe_entry_halt_and_live_exits():
    arming = (ROOT / "docs" / "runbooks" / "arming.md").read_text(encoding="utf-8")
    incident = (ROOT / "docs" / "runbooks" / "incident-provider-down.md").read_text(
        encoding="utf-8"
    )
    executor = (ROOT / "kaiba" / "execution" / "executor.py").read_text(encoding="utf-8")

    assert "The kill switch stops new entries; it does not stop exits." in arming
    assert "still allow exits for already-open" in incident
    assert "Gate entries. Never gate exits." in executor
    assert "including the lanes that would exit" not in incident

