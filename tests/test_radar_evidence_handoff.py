"""Offline regression: normalized registry evidence must survive the find handoff."""
from copy import deepcopy

import pytest

from kaiba.core.db import fetch_one, jload
from kaiba.hunters import radar

DAY = 86_400_000
START = 1_790_035_200_000
CONFIG = "handoff-fixture-only-config"


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Evidence handoff regressions must not use providers or the default DB')
    for name in ('get_json', 'post_json', 'get_conn'):
        monkeypatch.setattr(radar, name, forbidden)


def page(ts, volume='3000000'):
    return {'success': True, 'data': {'rows': [{
        'createAt': ts - 1000, 'mint': 'handoff-fixture-only-mint', 'volumeU': volume,
        'platformInfo': {'pubKey': CONFIG, 'name': 'Fixture only', 'feeRate': '10000'},
    }], 'nextPageId': None}}


def poll(conn, ts, census=True):
    cands, ok, total, requests, error = radar.poll_launchlab(
        conn, census=census, raw_pages=[page(ts)], now=ts)
    assert (len(cands), ok, total, requests, error) == (1, 1, 1, 0, None)
    return cands[0]


def test_normalized_census_evidence_reaches_the_find_reader(tmp_db):
    original = []
    snapshots = []
    for day in range(3):
        ts = START + day * DAY
        cand = poll(tmp_db, ts)
        original.append(cand)
        snapshots.append(deepcopy(cand.meta))
        out = radar.observe(tmp_db, cand, now=ts, oracles=radar.OracleSets())
        assert out.confirm_days == day + 1
        assert out.reported == (day == 2)
        if out.reported:
            radar.record_find(tmp_db, out, now=ts)
        discovered = radar.observe(tmp_db, poll(tmp_db, ts + 10000, False), now=ts + 10000)
        assert not discovered.reported and discovered.candidate.value is None
        assert discovered.rank_score is None

    row = fetch_one(tmp_db, 'SELECT * FROM radar_registry')
    meta = jload(row['meta_json'])
    finds = radar.recent_finds(tmp_db)
    assert len(finds) == 1
    assert finds[0]['identity'] == CONFIG
    assert finds[0]['radar_key'] == row['radar_key']
    assert finds[0]['layer'] == row['layer'] == 'launchlab_census'
    evidence = finds[0]['payload']
    assert evidence.get('value_observed_ms') == START + 2 * DAY
    assert evidence.get('value_sample_day') == radar.utc_day(START + 2 * DAY)
    assert evidence.get('first_pool_ms') == START - 1000
    assert evidence.get('first_pool_source_layer') == 'launchlab_census'
    assert evidence.get('first_pool_observed_ms') == START
    assert evidence.get('sample_first_pool_ms') == START + 2 * DAY - 1000
    for field in ('value_observed_ms', 'value_sample_day', 'first_pool_ms',
                  'first_pool_source_layer', 'first_pool_observed_ms', 'sample_first_pool_ms'):
        assert evidence[field] == meta[field]
    assert [c.meta for c in original] == snapshots
    assert fetch_one(tmp_db, 'SELECT COUNT(*) n FROM radar_registry')['n'] == 1
    assert fetch_one(tmp_db, "SELECT COUNT(*) n FROM events WHERE kind='hunter.found'")['n'] == 1


def test_bus_consumer_receives_the_same_evidence_snapshot(tmp_db, monkeypatch):
    # Seed the existing layer baseline without a venue, then run real sweep logic.
    empty = {'success': True, 'data': {'rows': [], 'nextPageId': None}}
    radar.sweep(tmp_db, only=['launchlab_census'], force=True, now=START - DAY,
                oracles=radar.OracleSets(),
                raw={'launchlab_census': {'raw_pages': [empty]}})
    for day in range(4):
        ts = START + day * DAY
        result = radar.sweep(tmp_db, only=['launchlab_census'], force=True, now=ts,
                             oracles=radar.OracleSets(),
                             raw={'launchlab_census': {'raw_pages': [page(ts)]}})
        assert (result.seen, result.new, result.reported, result.requests) == (
            1, int(day == 0), int(day == 2), 0)
        assert not result.errors
    found = radar.recent_finds(tmp_db)
    assert len(found) == 1
    event = fetch_one(tmp_db, "SELECT * FROM events WHERE kind='hunter.found'")
    payload = jload(event['payload'])
    assert payload['identity'] == found[0]['identity'] == CONFIG
    assert payload['layer'] == found[0]['layer'] == 'launchlab_census'
    assert event['dedupe_key'] == f"radar:{found[0]['radar_key']}:tier1"
    assert payload.get('evidence') == found[0]['payload']
    assert payload['evidence']['value_observed_ms'] == START + 2 * DAY
    # A later registry sample may not refresh the immutable find/event evidence.
    current = jload(fetch_one(tmp_db, 'SELECT meta_json FROM radar_registry')['meta_json'])
    assert current['value_observed_ms'] == START + 3 * DAY
    assert fetch_one(tmp_db, "SELECT COUNT(*) n FROM events WHERE kind='hunter.found'")['n'] == 1
    assert fetch_one(tmp_db, 'SELECT COUNT(*) n FROM orders')['n'] == 0
    # The actual MCP consumer keeps numeric evidence clocks and identity intact.
    from kaiba.mcp import server
    monkeypatch.setattr(server, '_conn', lambda: tmp_db)
    delivered = server.kaiba_events(after_id=0, kinds=['hunter.found'])['events']
    assert len(delivered) == 1
    assert delivered[0]['payload']['identity'] == CONFIG
    assert delivered[0]['payload']['evidence']['value_observed_ms'] == START + 2 * DAY
    assert delivered[0]['payload']['evidence']['first_pool_ms'] == START - 1000


@pytest.mark.parametrize('volume', [None, '0'])
def test_unknown_or_zero_value_does_not_become_a_find(tmp_db, volume):
    raw = page(START, volume=volume)
    candidates, ok, total, requests, error = radar.poll_launchlab(
        tmp_db, census=True, raw_pages=[raw], now=START)
    out = radar.observe(tmp_db, candidates[0], now=START)
    assert not out.reported
    assert radar.recent_finds(tmp_db) == []
    expected = None if volume is None else START
    row = fetch_one(tmp_db, 'SELECT * FROM radar_registry')
    assert jload(row['meta_json'])['value_observed_ms'] == expected
    assert fetch_one(tmp_db, 'SELECT COUNT(*) n FROM orders')['n'] == 0


def test_metadata_cannot_override_bus_identity_or_ranking(tmp_db):
    from dataclasses import replace
    cand = radar.poll_launchlab(tmp_db, census=True, raw_pages=[page(START, '30000000')],
                               now=START)[0][0]
    cand = replace(cand, meta={**cand.meta, 'identity': 'untrusted replacement',
                              'hunter': 'untrusted source', 'rank_score': '999999',
                              'first_report': False, 'confirm_days': 999})
    original = deepcopy(cand.meta)
    out = radar.observe(tmp_db, cand, now=START)
    assert out.reported
    radar.record_find(tmp_db, out, now=START)
    event = jload(fetch_one(tmp_db, "SELECT payload FROM events WHERE kind='hunter.found'")['payload'])
    assert event['identity'] == CONFIG
    assert event['hunter'] == 'radar'
    assert event['rank_score'] == str(out.rank_score)
    assert event['first_report'] is True
    assert event['evidence']['first_report'] is True
    assert event['evidence']['confirm_days'] == 1
    assert cand.meta == original
