import copy
import json
from pathlib import Path

import pytest

from kaiba.learning import autolearn
from kaiba.learning.research_report import EVIDENCE_FLAGS, compact_report, history_summary


def example():
    row = {"status": "closed", "cost_complete": False, "venue_complete": False,
           "tax_complete": True, "live_cost_verified": False, "net_return": -0.4,
           "fill_evidence": [{"original": "x" * 10000}], "outcome_id": "test-outcome"}
    paper = {"outcomes": [row], "assessment_rows": [row, {"status": "pending"}],
             "refusals": [], "errors": [], "arms": [{"book_path": "paper/example.db",
             "hypothesis_id": "test-arm", "open": 1, "closed": 1,
             "incomplete": [{"status": "expired", "reason": "no_usable_quote"}]}]}
    return {"status": "complete", "at_ms": 100, "completed_ms": 120, "funding_allowed": False,
            "campaigns": [{"campaign_id": "test-campaign", "paper": paper,
                           "assessment": {"status": "UNPROVEN", "arms": []}}],
            "unknown_future_diagnostic": {"data": "preserved"}}


def test_every_row_and_unknown_flag_survive_as_counts_without_mutating_source():
    source = example()
    before = copy.deepcopy(source)
    result = compact_report(source)
    assert source == before
    paper = result["campaigns"][0]["paper"]
    assert "outcomes" not in paper and "assessment_rows" not in paper
    assert paper["history_summary"]["outcomes"]["rows"] == 1
    assert paper["history_summary"]["assessment_rows"]["rows"] == 2
    assert paper["history_summary"]["assessment_rows"]["evidence_flags"]["cost_complete"] == {
        "true": 0, "false": 1, "unknown": 1}
    assert paper["arms"][0]["incomplete_summary"]["reason"]["counts"] == {"no_usable_quote": 1}
    assert result["operational_projection"]["omitted_list_entries"] == 4
    assert result["campaigns"][0]["assessment"] == before["campaigns"][0]["assessment"]
    assert result["unknown_future_diagnostic"] == before["unknown_future_diagnostic"]
    assert result["funding_allowed"] is False
    assert result["at_ms"] == 100 and result["completed_ms"] == 120
    assert len(json.dumps(result)) < len(json.dumps(source)) / 2


def test_unknown_or_malformed_evidence_cannot_become_complete_or_empty():
    summary = history_summary([None, {"status": None}, {"status": "closed", "cost_complete": 1}])
    assert summary["rows"] == 3 and summary["malformed_rows"] == 1
    assert summary["status"]["missing_rows"] == 2
    for field in EVIDENCE_FLAGS:
        assert summary["evidence_flags"][field] == {"true": 0, "false": 0, "unknown": 3}
    assert history_summary(None)["rows"] is None
    assert not history_summary(None)["available"]
    assert history_summary([])["rows"] == 0
    assert history_summary([])["available"]


def test_label_overflow_is_bounded_and_keeps_exact_population_count():
    rows = [{"status": "expired", "reason": f"reason_{i}"} for i in range(1000)]
    result = history_summary(rows)
    assert result["rows"] == 1000
    assert len(result["reason"]["counts"]) == 128
    assert result["reason"]["overflow_rows"] == 872
    assert sum(result["reason"]["counts"].values()) + result["reason"]["overflow_rows"] == 1000


def test_missing_histories_differ_from_empty_history_and_projection_is_not_repeatable():
    raw = example()
    del raw["campaigns"][0]["paper"]["outcomes"]
    result = compact_report(raw)
    assert "outcomes" not in result["campaigns"][0]["paper"]["history_summary"]
    assert result["campaigns"][0]["paper"]["history_summary"]["refusals"]["rows"] == 0
    with pytest.raises(ValueError, match="raw_research_report"):
        compact_report(result)


@pytest.mark.parametrize("campaigns", [None, [None], [{"paper": None}]])
def test_bad_campaign_shape_refuses_instead_of_asserting_no_outcomes(campaigns):
    with pytest.raises(ValueError):
        compact_report({"campaigns": campaigns})


def test_actual_hook_changes_only_persisted_latest_report(tmp_path):
    """Exercise the production writer, not a separately patched copy."""
    writer = autolearn.atomic_json
    raw = example()
    before = copy.deepcopy(raw)
    writer(tmp_path / "latest-report.json", raw)
    writer(tmp_path / "other.json", raw)
    writer(tmp_path / "state.json", raw)
    saved = json.loads((tmp_path / "latest-report.json").read_text())
    assert saved["operational_projection"]["scoring_input"] is False
    assert json.loads((tmp_path / "other.json").read_text()) == before
    assert json.loads((tmp_path / "state.json").read_text()) == before
    assert raw == before
    assert not list(Path(tmp_path).glob("*.partial"))
    # The shared writer's other payload shapes keep their existing serialization.
    writer(tmp_path / "latest-report.json", {"pending": [1, 2, 3]})
    assert json.loads((tmp_path / "latest-report.json").read_text()) == {"pending": [1, 2, 3]}
