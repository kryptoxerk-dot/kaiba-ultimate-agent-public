import sqlite3

import pytest

from kaiba.intelligence.campaign_candidates import provider_candidates


def address(i):
    return '0x'+format(i, '040x')


@pytest.fixture
def db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE wallets(chain,address,tags_json,cohort,last_seen_ms);
        CREATE TABLE wallet_scores(chain,address);
        CREATE TABLE swaps(chain,wallet,source,side);
        CREATE TABLE hub_addresses(chain,address);
        CREATE TABLE clustering_coverage(chain,address,status);
        CREATE TABLE tracker_watchlist(chain,address,status);
    ''')
    for i in range(1,12):
        conn.execute('INSERT INTO wallets VALUES(?,?,?,?,?)', ('bsc',address(i),'[]',None,i))
    yield conn
    conn.close()


def pick(db, **kwargs):
    return provider_candidates(db, 'bsc', attempts={}, now_ms=200_000_000, **kwargs)


def test_observed_two_sided_before_single_sided_before_recency(db):
    for i,side in [(1,'buy'),(1,'sell'),(2,'buy')]:
        db.execute('INSERT INTO swaps VALUES(?,?,?,?)',('bsc',address(i),'gmgn:kol',side))
    rows = pick(db, limit=3)
    assert [r['address'] for r in rows] == [address(1),address(2),address(11)]
    assert [r['priority_basis'] for r in rows] == [
        'observed_two_sided_feed','observed_feed','registry_recency']
    assert rows[0]['prior_grade'] is None  # no grade conferred by this ordering


def test_risk_existing_score_removed_and_bad_tags_excluded(db):
    db.execute('INSERT INTO wallet_scores VALUES(?,?)',('bsc',address(11)))
    db.execute('INSERT INTO hub_addresses VALUES(?,?)',('bsc',address(10)))
    db.execute('INSERT INTO clustering_coverage VALUES(?,?,?)',('bsc',address(9),'quarantined'))
    db.execute('INSERT INTO tracker_watchlist VALUES(?,?,?)',('bsc',address(8),'removed'))
    db.execute('UPDATE wallets SET tags_json=? WHERE address=?',('["gmgn:wash_trader"]',address(7)))
    db.execute('UPDATE wallets SET cohort=? WHERE address=?',('blacklist',address(6)))
    db.execute('UPDATE wallets SET tags_json=? WHERE address=?',('{}',address(5)))
    assert [r['address'] for r in pick(db)] == [address(i) for i in (4,3,2,1)]


def test_recent_attempt_omitted_but_other_chain_attempt_does_not_hide_wallet(db):
    rows = provider_candidates(db,'bsc',now_ms=200_000_000,
        attempts={f'bsc:{address(11)}':199_000_000,f'robinhood:{address(10)}':199_000_000},limit=1)
    assert rows[0]['address'] == address(10)


def test_other_chain_and_non_feed_observations_do_not_affect_ranking(db):
    for chain,source in [('robinhood','gmgn:kol'),('bsc','other')]:
        for side in ('buy','sell'):
            db.execute('INSERT INTO swaps VALUES(?,?,?,?)',(chain,address(1),source,side))
    assert pick(db,limit=1)[0]['address'] == address(11)


def test_positive_vendor_tag_alone_gives_no_priority(db):
    db.execute('UPDATE wallets SET tags_json=? WHERE address=?',('["gmgn:kol"]',address(1)))
    assert pick(db,limit=1)[0]['address'] == address(11)


def test_caps_and_solana_excluded(db):
    assert pick(db,limit=0) == []
    with pytest.raises(ValueError):
        pick(db,limit=1001)
    with pytest.raises(ValueError):
        provider_candidates(db,'sol',attempts={},now_ms=200_000_000)
