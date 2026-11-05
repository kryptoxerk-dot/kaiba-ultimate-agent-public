"""Venue-specific activity for Kaiba's own BNB launches; no order submission."""
from kaiba.core.db import fetch_one
from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain
from kaiba.execution import evm_price as ep


def bsc_fingerprint(token,conn=None):
    row=fetch_one(conn,"SELECT dex FROM tweet_launches WHERE chain='bsc' AND token=? "
                       "AND mode='live' ORDER BY decided_ms DESC LIMIT 1",(token,))
    if not row or row["dex"] not in {"flap","fourmeme"}:
        return None,"own_bsc_venue_unavailable"
    rpc=ep.json_rpc_batch(Chain.BSC,conn=conn,priority=Priority.EXIT,endpoint="rpc.tweet_launch_activity")
    if row["dex"]=="fourmeme":
        venue,note=ep.read_bsc_non_flap(token,rpc)
        if venue is None:
            return None,note
        if venue.kind=="fourmeme_graduated":
            return ("graduated",),venue.note
        if venue.kind!="fourmeme_curve" or venue.curve is None:
            return None,"own_fourmeme_curve_unavailable:"+venue.kind
        curve=venue.curve
        return (curve.virtual_token_atoms,curve.funds_wei,curve.last_price_wei),venue.note
    priced,note=ep.read_flap(token,rpc)
    if priced is None:
        return (("graduated",),note) if "graduat" in str(note) else (None,note)
    if priced.curve is not None:
        return (priced.curve.tokens_sold_atoms,priced.quote_reserve_base),"ok"
    return (priced.quote_reserve_base,str(priced.price_quote_per_token)),"ok"
