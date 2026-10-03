"""The hunter-wallet watcher only reads, reports changes, and never mistakes an outage
for an empty wallet. Its stdout is delivered to Telegram verbatim, so "silent when
unchanged" is a contract, not a nicety."""

from __future__ import annotations

import pytest

from kaiba.core.schemas import Chain
from kaiba.hunters import watch_wallets as ww

SOL = ww.Watched(Chain.SOL, "rUHrovRbRDoCmn9ojmZ2Pi3bLPUJ15kGjmLfGm6MdTo", "hunter-sol")
BASE = ww.Watched(Chain.BASE, "0xcC4450c80735778E9F5eaA7A4eA47990e57807c2", "hunter-evm")


def reader_of(values: dict):
    def read(w):
        v = values.get((w.chain, w.address))
        return ww.Reading(v, "stub", "" if v is not None else "provider down")
    return read


def test_first_sighting_reports_the_baseline(tmp_db):
    lines = ww.refresh(tmp_db, [SOL], reader=reader_of({(Chain.SOL, SOL.address): 8_098_892_000}))
    assert len(lines) == 1 and "8.098892 SOL" in lines[0] and "now watching" in lines[0]


def test_an_unchanged_wallet_is_silent(tmp_db):
    r = reader_of({(Chain.SOL, SOL.address): 8_098_892_000})
    ww.refresh(tmp_db, [SOL], reader=r)
    assert ww.refresh(tmp_db, [SOL], reader=r) == []


def test_a_real_change_is_reported_with_direction_and_size(tmp_db):
    ww.refresh(tmp_db, [BASE], reader=reader_of({(Chain.BASE, BASE.address): 363_886_000_000_000_000}))
    lines = ww.refresh(tmp_db, [BASE], reader=reader_of({(Chain.BASE, BASE.address): 13_886_000_000_000_000}))
    assert len(lines) == 1
    assert "0.363886 ETH -> 0.013886 ETH" in lines[0] and "(-0.35 ETH)" in lines[0]


def test_dust_moves_are_not_news(tmp_db):
    ww.refresh(tmp_db, [SOL], reader=reader_of({(Chain.SOL, SOL.address): 8_098_892_000}))
    assert ww.refresh(tmp_db, [SOL], reader=reader_of({(Chain.SOL, SOL.address): 8_098_891_000})) == []


def test_an_outage_never_reads_as_the_funds_leaving(tmp_db):
    ww.refresh(tmp_db, [SOL], reader=reader_of({(Chain.SOL, SOL.address): 8_098_892_000}))
    assert ww.refresh(tmp_db, [SOL], reader=reader_of({})) == []
    snap = ww.snapshot(tmp_db)
    assert snap[0]["native_units"] == "8098892000", "the last known value must survive an outage"


def test_an_unreadable_new_wallet_says_so_instead_of_zero(tmp_db):
    lines = ww.refresh(tmp_db, [SOL], reader=reader_of({}))
    assert len(lines) == 1 and "cannot read yet" in lines[0] and "0 SOL" not in lines[0]
    assert ww.snapshot(tmp_db) == []


def test_watch_spec_parsing():
    w = ww.Watched.parse("robinhood:0xcC4450c80735778E9F5eaA7A4eA47990e57807c2:hunter-evm")
    assert w.chain is Chain.ROBINHOOD and w.label == "hunter-evm"
    with pytest.raises(ValueError):
        ww.Watched.parse("robinhood")


def test_gmgn_miss_falls_back_to_public_rpc(monkeypatch):
    monkeypatch.setattr(ww, "_gmgn_native", lambda w: ww.Reading(None, "gmgn", "unsupported"))
    monkeypatch.setattr(ww, "_rpc_native", lambda w: ww.Reading(42, "rpc"))
    assert ww.read_native(BASE).base_units == 42
    assert ww.read_native(SOL).base_units == 42


def test_a_failed_fallback_stays_unknown(monkeypatch):
    monkeypatch.setattr(ww, "_gmgn_native", lambda w: ww.Reading(None, "gmgn", "unsupported"))
    monkeypatch.setattr(ww, "_rpc_native", lambda w: ww.Reading(None, "rpc", "timeout"))
    assert ww.read_native(SOL).base_units is None


def test_sol_rpc_reply_is_parsed_from_lamports(monkeypatch):
    import io
    import json as _json

    seen = {}

    def fake_urlopen(req, timeout):
        seen["body"] = _json.loads(req.data)
        return io.BytesIO(_json.dumps({"result": {"context": {"slot": 1}, "value": 8098892000}}).encode())

    monkeypatch.setattr(ww.urllib.request, "urlopen", fake_urlopen)
    assert ww._rpc_native(SOL).base_units == 8_098_892_000
    assert seen["body"]["method"] == "getBalance"


def test_the_module_cannot_sign_or_send():
    """Watch-only by construction: nothing here reaches an executor, signer or swap."""
    import inspect

    src = inspect.getsource(ww)
    for forbidden in ("executor", "signer", "submit", "swap", "private", "keystore"):
        assert forbidden not in src.split('"""', 2)[-1].lower(), forbidden
