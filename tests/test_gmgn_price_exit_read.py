"""Protection's GMGN last resort reads at EXIT and never settles for a stale cache entry.

MEASURED 2026-10-01 on the box: 1,695 of 1,967 robinhood ``protection_blind`` events (86%)
ended "gmgn: gmgn:token.info" -- the GMGN layer HAD a price and served it STALE.
``token.info`` caches 15 s with a 120 s stale grace, and inside the grace
``gmgn_cli.run_read`` answers from disk without trying the network; the cache file for
the blind token 0xc777 aged 26 s -> 95 s across six protection ticks with no refresh. The
read also ran at ``token_info``'s RESEARCH default, the lowest priority in the limiter.
"""

from __future__ import annotations

import json

from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, now_ms
from kaiba.execution import watchdog as W
from kaiba.execution.protection import ProtectionConfig

TOKEN = "0xc777f874a7350cfa2bf123ed09253e225dc254d9"


def test_the_read_is_exit_priority_with_no_stale_grace(monkeypatch):
    seen: dict = {}

    def fake(address, chain=Chain.SOL, **kw):
        seen.update(kw)
        return type("R", (), {"data": None, "receipt": None})()

    monkeypatch.setattr(W, "_gmgn_token_info", fake)
    W.GmgnPriceSource().quote(Chain.ROBINHOOD, TOKEN)
    assert seen.get("priority") is Priority.EXIT, seen
    assert seen.get("stale_grace_s") == 0, seen
    assert 0 < seen.get("ttl_s", 0) < ProtectionConfig().poll_interval_s, seen


def test_a_stale_cache_entry_is_refetched_not_served(tmp_db, monkeypatch):
    """End to end through gmgn_cli: a 40 s old token.info (inside the old 120 s grace)
    must cost one CLI call and come back as a usable, fresh price."""
    from kaiba.providers import _http
    from kaiba.providers import gmgn_cli as g

    argv = ["token", "info", "--address", TOKEN, "--chain", "robinhood", "--raw"]
    path = _http.cache_path(g.PROVIDER, "gmgn-cli " + " ".join(argv))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_ms": now_ms() - 40_000,
                                "data": {"price": {"price": "0.000001"}}}), encoding="utf-8")

    spawned: list = []

    def fake_spawn(cmd, timeout_s):
        spawned.append(cmd)
        body = {"code": 0, "data": {"address": TOKEN, "price": {"price": "0.0000046"}}}
        return g._Raw(0, json.dumps(body), "")

    monkeypatch.setattr(g, "cli_argv", lambda: ["node", "index.js"])
    monkeypatch.setattr(g, "_spawn", fake_spawn)

    quote = W.GmgnPriceSource().quote(Chain.ROBINHOOD, TOKEN)

    assert len(spawned) == 1, "the stale entry was served instead of asking GMGN"
    assert quote.usable, quote.invalid_reason
    assert str(quote.price_usd) == "0.0000046"
