import json

import pytest

from kaiba.core.schemas import Chain
from kaiba.intelligence import campaign_watchlist as campaign
from kaiba.intelligence import tracker

NOW = 100_000_000
ADDRESS = '0x' + 'a'*40


def row(**changes):
    return dict(chain='bsc', address=ADDRESS, grade='B', score=45.0,
                evidence_weight=40, scored_at_ms=NOW-1000, tags_json='[]',
                model_version='kaiba-wallet-gmgn-v1', watch_status=None,
                feed_observed=1) | changes


def test_each_chain_must_reach_active_target_without_transferring_grades():
    rows = [row(watch_status='active'), row(chain='robinhood', watch_status='active')]
    report, _ = campaign.inventory(rows, [], now_ms=NOW, target_per_chain=1)
    assert report['qualified_active'] == dict(bsc=1, sol=0, robinhood=1)
    assert report['qualified_active_distinct_addresses'] == 1
    assert not report['inventory_target_reached']
    sol = row(chain='sol', address='BiTsy'+'1'*39, watch_status='active', model_version='kaiba-wallet-tape-v1')
    audit = dict(chain='sol', address=sol['address'], current_tape_grade='B', current_tape_score=45,
                 evidence_weight=40, clean_closed_episodes=8, closed_tokens=4, audit_started_ms=NOW-500)
    report, _ = campaign.inventory(rows+[sol], [audit], now_ms=NOW, target_per_chain=1)
    assert report['inventory_target_reached']


@pytest.mark.parametrize('change', [dict(hub_address=ADDRESS), dict(cluster_status='quarantined'),
    dict(cohort='blacklist'), dict(tags_json='["gmgn:wash_trader"]'),
    dict(model_version='kaiba-wallet-v1'), dict(score=39.9), dict(evidence_weight=29.9),
    dict(scored_at_ms=NOW-86_400_001), dict(registry_address=None)])
def test_bad_or_unreviewed_evidence_never_qualifies(change):
    report, candidates = campaign.inventory([row(watch_status='active', **change)], [], now_ms=NOW)
    assert report['qualified_active']['bsc'] == 0
    assert not candidates


def test_duplicate_case_and_missing_feed_do_not_manufacture_admissions():
    report, candidates = campaign.inventory([row(), row(address=ADDRESS.upper().replace('0X', '0x'))], [], now_ms=NOW)
    assert report['qualified_inventory']['bsc'] == 0
    assert candidates == []
    assert campaign.admission_blocker(row(feed_observed=0)) == 'no_existing_feed_coverage'
    assert campaign.admission_blocker(row(watch_status='removed')) == 'existing_roster_row_preserved'


def offer_fixture(db):
    db.execute('INSERT INTO wallets(chain,address,first_seen_ms,last_seen_ms) VALUES(?,?,?,?)',
               ('bsc', ADDRESS, NOW-2000, NOW))
    db.execute('INSERT INTO wallet_scores(chain,address,grade,score,evidence_weight,archetype,model_version,scored_at_ms) VALUES(?,?,?,?,?,?,?,?)',
               ('bsc', ADDRESS, 'B', 45, 40, 'trader', 'kaiba-wallet-gmgn-v1', NOW-1000))
    for i in range(24):
        db.execute('INSERT INTO swaps(chain,tx,ts_ms,wallet,token,side,amount_token,amount_native,source) VALUES(?,?,?,?,?,?,?,?,?)',
                   ('bsc', str(i), NOW-2000+i, ADDRESS, '0x'+'b'*40,
                    'buy' if i%2 == 0 else 'sell', '10', '100', 'gmgn:kol'))
    original = dict(db.execute(campaign.SELECT_ROWS).fetchone())
    screen = tracker.screen_wallet(Chain.BSC, ADDRESS, db, check_failure_rate=False)
    assert tracker.cohort_verdict(screen).admissible
    return dict(row=original, screen=screen.as_dict(), screened_ms=NOW)


def test_real_admission_is_additive_observe_only_and_never_enables_paid_poll(tmp_db):
    offer = offer_fixture(tmp_db)
    result = campaign.activate_offers(tmp_db, [offer], [], now_ms=NOW)
    assert (result['inserted'], result['verified'], result['store_failed']) == (1, 1, 0)
    entry = dict(tmp_db.execute('SELECT * FROM tracker_watchlist').fetchone())
    assert entry['tier'] == 'observe'
    assert json.loads(entry['meta_json'])['helius_poll'] is False
    assert tmp_db.execute('SELECT COUNT(*) FROM tracker_watchlist_log').fetchone()[0] == 1
    assert campaign.activate_offers(tmp_db, [offer], [], now_ms=NOW)['preserved'] == 1
    assert dict(tmp_db.execute('SELECT * FROM tracker_watchlist').fetchone()) == entry


def test_concurrent_grade_or_quarantine_is_preserved(tmp_db):
    offer = offer_fixture(tmp_db)
    tmp_db.execute("UPDATE wallet_scores SET grade='C', score=39")
    assert campaign.activate_offers(tmp_db, [offer], [], now_ms=NOW)['preserved'] == 1
    assert tmp_db.execute('SELECT COUNT(*) FROM tracker_watchlist').fetchone()[0] == 0


def test_suppressed_tracker_write_failure_is_counted_and_batch_rolled_back(tmp_db, monkeypatch):
    offer = offer_fixture(tmp_db)
    def partial_failure(*args, **kwargs):
        tmp_db.execute("INSERT INTO tracker_watchlist(chain,address,tier,status,reason,source,added_by,added_ms) VALUES('bsc',?,'observe','active','r','s','a',?)", (ADDRESS, NOW))
        return None
    monkeypatch.setattr(tracker, 'admit', partial_failure)
    result = campaign.activate_offers(tmp_db, [offer], [], now_ms=NOW)
    assert result['store_failed'] == 1 and result['inserted'] == 0
    assert tmp_db.execute('SELECT COUNT(*) FROM tracker_watchlist').fetchone()[0] == 0
