import sqlite3
import pytest
@pytest.fixture
def tmp_db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE swaps(id INTEGER PRIMARY KEY, chain TEXT, tx TEXT, ts_ms INTEGER, '
                 'wallet TEXT, token TEXT, side TEXT, amount_token TEXT, price_usd TEXT, '
                 'usd_value TEXT, source TEXT)')
    try:
        yield conn
    finally:
        conn.close()

from decimal import Decimal

from kaiba.core.schemas import Chain
from kaiba.learning.copytrade import evaluate_wallet, next_print, rank_wallets


def put(db, ident, stamp, price, side='buy', wallet='other'):
    db.execute('INSERT INTO swaps(id,chain,wallet,token,ts_ms,side,price_usd) VALUES(?,?,?,?,?,?,?)',
               (ident,'sol',wallet,'token',stamp,side,price))

def test_future_exit_print_cannot_change_training_score(tmp_db):
    put(tmp_db,1,100,'1',wallet='leader')
    put(tmp_db,2,200,None,side='sell',wallet='leader')
    put(tmp_db,3,400,'10')
    first=evaluate_wallet(tmp_db,'leader',lag_ms=0,fee_bps=0,until_ms=300)
    tmp_db.execute("UPDATE swaps SET price_usd='0.1' WHERE id=3")
    second=evaluate_wallet(tmp_db,'leader',lag_ms=0,fee_bps=0,until_ms=300)
    assert first.as_dict()==second.as_dict()
    assert first.trips_seen==1 and first.trips_fillable==0
    assert first.mean_net is None

def test_future_entry_print_cannot_supply_missing_training_fill(tmp_db):
    put(tmp_db,1,100,None,wallet='leader')
    put(tmp_db,2,200,None,side='sell',wallet='leader')
    put(tmp_db,3,400,'10')
    result=evaluate_wallet(tmp_db,'leader',lag_ms=0,fee_bps=0,until_ms=300)
    assert result.trips_fillable==0
    assert result.reasons=={'no_entry_print':1}

def test_delayed_exit_beyond_boundary_stays_unknown(tmp_db):
    put(tmp_db,1,100,'1',wallet='leader')
    put(tmp_db,2,150,'1')
    put(tmp_db,3,280,'2',side='sell',wallet='leader')
    put(tmp_db,4,330,'10')
    result=evaluate_wallet(tmp_db,'leader',lag_ms=50,fee_bps=0,until_ms=300)
    assert result.trips_seen==1 and result.trips_fillable==0
    assert result.reasons=={'no_exit_print':1}

def test_window_edge_is_inclusive_and_no_window_preserves_old_behavior(tmp_db):
    put(tmp_db,1,100,'1',wallet='leader')
    put(tmp_db,2,200,None,side='sell',wallet='leader')
    put(tmp_db,3,300,'2')
    exact=evaluate_wallet(tmp_db,'leader',lag_ms=0,fee_bps=0,until_ms=300)
    assert exact.trips_fillable==1 and exact.mean_net==Decimal(1)
    cut=evaluate_wallet(tmp_db,'leader',lag_ms=0,fee_bps=0,until_ms=299)
    assert cut.trips_fillable==0
    unbounded=evaluate_wallet(tmp_db,'leader',lag_ms=0,fee_bps=0)
    assert unbounded.mean_net==Decimal(1)

def test_next_print_respects_both_deadlines(tmp_db):
    put(tmp_db,1,300,'2')
    assert next_print(tmp_db,Chain.SOL,'token',200,max_wait_ms=99,until_ms=400) is None
    assert next_print(tmp_db,Chain.SOL,'token',200,max_wait_ms=200,until_ms=299) is None
    assert next_print(tmp_db,Chain.SOL,'token',200,max_wait_ms=100,until_ms=300)==(Decimal(2),300)
    assert next_print(tmp_db,Chain.SOL,'token',400,until_ms=300) is None


def test_training_ranking_does_not_select_a_winner_using_future_prices(tmp_db):
    put(tmp_db,1,100,'1',wallet='future_winner')
    put(tmp_db,2,200,None,side='sell',wallet='future_winner')
    put(tmp_db,3,400,'10')
    tmp_db.executemany('INSERT INTO swaps(id,chain,wallet,token,ts_ms,side,price_usd) VALUES(?,?,?,?,?,?,?)',[
        (4,'sol','known_winner','other_token',100,'buy','1'),
        (5,'sol','known_winner','other_token',200,'sell','2'),
    ])
    options=dict(lag_ms=0,fee_bps=0,min_buys=1,min_sells=1,min_fillable=1,until_ms=300)
    first=rank_wallets(tmp_db,**options)
    tmp_db.execute("UPDATE swaps SET price_usd='0.1' WHERE id=3")
    second=rank_wallets(tmp_db,**options)
    assert [r.wallet for r in first]==[r.wallet for r in second]==['known_winner']

