"""Consumer integration: distinguish refreshed rows from qualified research."""
import io

import pytest
from rich.console import Console


@pytest.mark.parametrize('outcome,healthy', [('healthy_empty', True), ('source_error', False), ('partial_source_error', False)])
def test_mcp_preserves_evidence_outcome(tmp_db, monkeypatch, outcome, healthy):
    from kaiba.hunters import airdrops
    from kaiba.mcp import server
    report = dict(written=12, new_count=2, qualified_count=0, outcome=outcome,
                  funded_action_authorized=False, sources={'one': {'state': 'error'}})
    monkeypatch.setattr(server, '_conn', lambda: tmp_db)
    monkeypatch.setattr(airdrops, 'refresh_report', lambda conn: report)
    monkeypatch.setattr(airdrops, 'refresh', lambda *a, **k: pytest.fail('legacy bypass'))
    result = server.kaiba_run_hunter('airdrop')
    assert result['ok'] is healthy
    assert result['qualified_count'] == 0 and result['new_count'] == 2
    assert result['found'] == 12
    assert result['found_semantics'] == 'legacy_upsert_count_not_qualified'
    assert result['sources'] == report['sources']
    assert result['funded_action_authorized'] is False


def test_listing_keeps_legacy_contract(tmp_db, monkeypatch):
    from kaiba.hunters import listings
    from kaiba.mcp import server
    monkeypatch.setattr(server, '_conn', lambda: tmp_db)
    monkeypatch.setattr(listings, 'refresh', lambda conn: 3)
    assert server.kaiba_run_hunter('listing') == {'ok': True, 'kind': 'listing', 'found': 3}


def test_cli_reports_distinct_counts(tmp_db, monkeypatch):
    from kaiba.cli import main
    from kaiba.core import db
    from kaiba.hunters import airdrops
    stream = io.StringIO()
    monkeypatch.setattr(main, 'console', Console(file=stream, width=200))
    monkeypatch.setattr(db, 'ensure_db', lambda: tmp_db)
    monkeypatch.setattr(airdrops, 'refresh_report', lambda conn: dict(
        written=200, new_count=0, qualified_count=0, outcome='source_error'))
    monkeypatch.setattr(airdrops, 'refresh', lambda *a, **k: pytest.fail('legacy bypass'))
    main.hunt_refresh('airdrop')
    rendered=stream.getvalue()
    for value in ('written', 'new', 'qualified', 'source_error', '200'):
        assert value in rendered
    assert 'found' not in rendered
