"""Performance must not add lamports to wei or round money through SQLite REAL."""
from kaiba.core.schemas import now_ms
from kaiba.mcp import server


def trade(conn, ident, chain, pnl, mode='live'):
    stamp=now_ms()-1000
    conn.execute('INSERT INTO trades(trade_id,position_id,lane,mode,chain,token,opened_ms,closed_ms,hold_s,cost_native,proceeds_native,pnl_native,pnl_pct) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (ident,'pos_'+ident,'sm-trenches',mode,chain,'token_'+ident,stamp-1000,stamp,1,'100000000000000000000000',str(100000000000000000000000+pnl),str(pnl),10.0 if pnl>0 else -10.0))


def test_mixed_native_pnl_is_not_published_as_one_money_number(tmp_db,monkeypatch):
    monkeypatch.setattr(server,'_conn',lambda:tmp_db)
    trade(tmp_db,'sol','sol',100)
    trade(tmp_db,'bsc','bsc',-200)
    got=server.kaiba_performance(mode='live')['by_lane'][0]
    assert got['pnl'] is None
    assert got['pnl_by_chain']=={'sol':100,'bsc':-200}
    assert got['n']==2 and got['wins']==1


def test_exact_large_native_integers_and_mode_filter(tmp_db,monkeypatch):
    monkeypatch.setattr(server,'_conn',lambda:tmp_db)
    value=2**80+7
    trade(tmp_db,'a','bsc',value)
    trade(tmp_db,'b','bsc',value)
    trade(tmp_db,'paper','bsc',-value,mode='shadow')
    got=server.kaiba_performance(mode='live')
    assert got['by_lane'][0]['pnl']==2*value
    assert got['by_chain'][0]['pnl']==2*value
    assert got['by_chain'][0]['n']==2


def test_missing_closed_outcome_is_disclosed_not_silently_dropped(tmp_db,monkeypatch):
    monkeypatch.setattr(server,'_conn',lambda:tmp_db)
    stamp=now_ms()-1000
    tmp_db.execute('INSERT INTO positions(position_id,chain,token,lane,mode,opened_ms,closed_ms) VALUES(?,?,?,?,?,?,?)',
                   ('missing','bsc','tok','sm-trenches','live',stamp-1000,stamp))
    got=server.kaiba_performance(mode='live')
    assert got['coverage']['recorded_outcomes']==0
    assert got['coverage']['missing_closed_outcomes']==[{'chain':'bsc','n':1}]


def test_future_outcome_is_excluded(tmp_db,monkeypatch):
    monkeypatch.setattr(server,'_conn',lambda:tmp_db)
    trade(tmp_db,'future','sol',100)
    tmp_db.execute('UPDATE trades SET closed_ms=?',(now_ms()+86400000,))
    assert server.kaiba_performance(mode='live')['by_lane']==[]
