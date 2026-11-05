"""File-only study status through the normal method intake entry point."""
import hashlib
import json

from kaiba.hunters.grok_alpha_bridge import Store
from kaiba.learning import method_intake, paper_progress

NOW = 1793733500000


def write(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(value).encode())
    return path


def h5_report():
    arm = {"triggers": 9, "entered": 5, "settled": 3, "unresolved": 2,
           "recovery_unknown": {"entered": 1, "pending": 0}, "verdict": {"verdict": "PENDING"}}
    return {"version": "h5-reentry/1", "mode": "paper", "as_of_ms": NOW - 15_000,
            "first_heartbeat_ms": NOW - 100_000, "arms": dict.fromkeys(("B1", "B2", "H5"), arm),
            "observation_gaps": {"over_60s": 1}, "unknown_intervals": {"read_deferrals": 2}}


def test_method_report_publishes_projected_counts_and_preserves_source_files(tmp_path, monkeypatch):
    source = write(tmp_path, "learning/h5/report.json", h5_report())
    d1 = write(tmp_path, "perps/report.json", {
        "version": "perps-d1-paper/1", "mode": "paper", "as_of_ms": NOW - 20_000,
        "registered_at": "2026-11-03T12:00:00Z", "pooled": {"n": 0},
        "verdict": {"n": 0, "min_n": 40, "verdict": "PENDING"},
        "symbols": {"fixture1": {"status": "ok", "last_closed_bar": "2026-11-03T00:00:00Z"},
                    "fixture2": {"status": "unavailable", "error": "private diagnostic"}},
    })
    xs = write(tmp_path, "perps/xs/report.json", {
        "version": "perps-xs-paper/1", "mode": "paper", "as_of_ms": NOW - 20_000,
        "first_monday_ms": 1794182400000, "books": {"carry": {"weeks": 2}, "momentum": {"weeks": 1}},
        "mix_50_50": {"weeks": 1}, "missed_formations": [1], "verdict": "PENDING",
    })
    originals = {path: path.read_bytes() for path in (source, d1, xs)}
    monkeypatch.setattr(method_intake, "MIN_FILESYSTEM_FREE", 0)
    monkeypatch.setattr(method_intake, "FILESYSTEM_HALT", 1.0)
    inbox = tmp_path / "grok-alpha/inbox.db"
    Store(inbox, clock=lambda: NOW)
    target = tmp_path / "method-lab"
    method_intake.run(inbox, target, now_ms=NOW)
    report = json.loads((target / "latest.json").read_bytes())
    progress = report["paper_progress"]
    arm = progress["h5"]["arms"]["H5"]
    assert (arm["priced_exits"], arm["unresolved_terminal_marks"], arm["entered_without_terminal"]) == (1, 2, 2)
    assert arm["recovery_unknown_entered"] == 1
    assert progress["h5"]["source_sha256"] == hashlib.sha256(originals[source]).hexdigest()
    assert progress["h5"]["as_of_ms"] == NOW - 15_000
    assert progress["binance_d1"]["symbols_unavailable"] == 1
    assert progress["binance_d1"]["symbols_with_closed_bar_clock"] == 1
    assert "private diagnostic" not in json.dumps(progress) and "fixture2" not in json.dumps(progress)
    assert progress["binance_xs"]["paired_weeks"] == 1
    assert progress["promotion_authority"] is False and report["auto_promotion"] is False
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    assert report["checkpoint_sha256"] == hashlib.sha256((target / "state.json").read_bytes()).hexdigest()


def test_stale_future_and_malformed_reports_never_create_zero_outcomes(tmp_path):
    name = "learning/h5/report.json"
    for clock, status in ((NOW + 1, "future"), (NOW - paper_progress.H5_AGE_MS - 1, "stale")):
        write(tmp_path, name, {**h5_report(), "as_of_ms": clock})
        result = paper_progress.snapshot(tmp_path, now_ms=NOW)
        assert result["h5"]["status"] == status and result["h5"]["as_of_ms"] == clock
        assert "arms" not in result["h5"]
        assert result["binance_xs"]["reason"] == "artifact_missing"
    malformed = h5_report()
    malformed["arms"]["H5"] = {**malformed["arms"]["H5"], "unresolved": 4}
    write(tmp_path, name, malformed)
    result = paper_progress.snapshot(tmp_path, now_ms=NOW)["h5"]
    assert result["status"] == "unavailable" and "arms" not in result
