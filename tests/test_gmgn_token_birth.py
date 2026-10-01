"""Provider pool-open metadata must not make an existing token young again."""
from datetime import datetime, timezone
import json

from kaiba.core.schemas import Chain
from kaiba.ingest import gmgn_feeds as feeds

TOKEN = '0x7218bd6c1c2038e613ce9203ffcdddde2623c12d'
BIRTH = int(datetime(2026,9,23,9,20,10,tzinfo=timezone.utc).timestamp()*1000)
POOL_OPEN = int(datetime(2026,9,23,9,42,19,tzinfo=timezone.utc).timestamp()*1000)


def seed(conn, *, created_ms=BIRTH, migrated_ms=None, source='gmgn:trending', extra_meta=None):
    meta={'source':source, **(extra_meta or {})}
    conn.execute('INSERT INTO tokens(chain,address,symbol,created_ms,migrated_ms,first_seen_ms,meta_json) VALUES(?,?,?,?,?,?,?)',
                 ('robinhood',TOKEN,'DOAI',created_ms,migrated_ms,BIRTH+35000,json.dumps(meta)))


def refresh(*, opened_ms=POOL_OPEN, completed_ms=None):
    row={'address':TOKEN,'symbol':'DOAI','open_timestamp':opened_ms//1000,'liquidity':'37000'}
    if completed_ms is not None:
        row['complete_timestamp']=completed_ms//1000
    return feeds.parse_trending([row],Chain.ROBINHOOD)[0]


def test_later_pool_open_cannot_overwrite_existing_birth(tmp_db):
    seed(tmp_db)
    assert feeds.write_token(tmp_db,refresh())=='updated'
    row=tmp_db.execute('SELECT created_ms,first_seen_ms,meta_json FROM tokens WHERE chain=? AND address=?',('robinhood',TOKEN)).fetchone()
    assert row['created_ms']==BIRTH
    assert row['first_seen_ms']==BIRTH+35000
    assert json.loads(row['meta_json'])['open_timestamp']==POOL_OPEN//1000


def test_provider_open_is_labelled_as_unverified_creation():
    opened=refresh()
    assert opened.token_facts['meta'].get('creation_time_basis')=='provider_open_timestamp_unverified'
    explicit=feeds.parse_trending([{'address':TOKEN,'created_timestamp':BIRTH//1000,'open_timestamp':POOL_OPEN//1000}],Chain.ROBINHOOD)[0]
    assert explicit.token_facts['created_ms']==BIRTH
    assert explicit.token_facts['meta'].get('creation_time_basis')=='provider_created_timestamp'


def test_verified_birth_provenance_survives_provider_refresh(tmp_db):
    seed(tmp_db,extra_meta={'creation_time_basis':'verified_onchain','creation_proof':{'tx':'verified-fixture'}})
    feeds.write_token(tmp_db,refresh())
    row=tmp_db.execute('SELECT created_ms,meta_json FROM tokens WHERE chain=? AND address=?',('robinhood',TOKEN)).fetchone()
    assert row['created_ms']==BIRTH
    meta=json.loads(row['meta_json'])
    assert meta['creation_time_basis']=='verified_onchain'
    assert meta['creation_proof']=={'tx':'verified-fixture'}


def test_provider_refresh_cannot_move_known_migration_forward(tmp_db):
    migrated=POOL_OPEN-11000
    seed(tmp_db,migrated_ms=migrated)
    feeds.write_token(tmp_db,refresh(completed_ms=POOL_OPEN+300000))
    assert tmp_db.execute('SELECT migrated_ms FROM tokens WHERE chain=? AND address=?',('robinhood',TOKEN)).fetchone()['migrated_ms']==migrated


def test_unknown_birth_can_be_filled_without_inventing_first_seen(tmp_db):
    seed(tmp_db,created_ms=None)
    row=feeds.parse_trending([{'address':TOKEN,'created_timestamp':BIRTH//1000}],Chain.ROBINHOOD)[0]
    feeds.write_token(tmp_db,row)
    stored=tmp_db.execute('SELECT created_ms,first_seen_ms,meta_json FROM tokens WHERE chain=? AND address=?',('robinhood',TOKEN)).fetchone()
    assert stored['created_ms']==BIRTH
    assert stored['first_seen_ms']==BIRTH+35000
    assert json.loads(stored['meta_json'])['creation_time_basis']=='provider_created_timestamp'


def test_missing_birth_and_migration_remain_unknown(tmp_db):
    seed(tmp_db,created_ms=None)
    row=feeds.parse_trending([{'address':TOKEN,'symbol':'DOAI'}],Chain.ROBINHOOD)[0]
    feeds.write_token(tmp_db,row)
    stored=tmp_db.execute('SELECT created_ms,migrated_ms FROM tokens WHERE chain=? AND address=?',('robinhood',TOKEN)).fetchone()
    assert stored['created_ms'] is None and stored['migrated_ms'] is None


def test_listener_owned_token_is_not_changed(tmp_db):
    seed(tmp_db,source='robinhood:launch')
    before=tuple(tmp_db.execute('SELECT * FROM tokens WHERE chain=? AND address=?',('robinhood',TOKEN)).fetchone())
    assert feeds.write_token(tmp_db,refresh())=='kept'
    assert tuple(tmp_db.execute('SELECT * FROM tokens WHERE chain=? AND address=?',('robinhood',TOKEN)).fetchone())==before


def test_first_migration_can_be_recorded_once(tmp_db):
    seed(tmp_db)
    migrated=POOL_OPEN-11000
    feeds.write_token(tmp_db,refresh(completed_ms=migrated))
    assert tmp_db.execute('SELECT migrated_ms FROM tokens WHERE chain=? AND address=?',('robinhood',TOKEN)).fetchone()['migrated_ms']==migrated


def test_existing_unlabelled_birth_is_not_upgraded_by_a_pool_open(tmp_db):
    seed(tmp_db)
    feeds.write_token(tmp_db,refresh())
    meta=json.loads(tmp_db.execute('SELECT meta_json FROM tokens WHERE chain=? AND address=?',('robinhood',TOKEN)).fetchone()['meta_json'])
    assert meta['creation_time_basis']=='legacy_unverified'
