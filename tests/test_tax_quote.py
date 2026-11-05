"""Tax-only views and actual frozen quote → admission → PaperBroker controls."""
from __future__ import annotations

import copy
import json
import socket
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from kaiba.core.schemas import Measure, Receipt
from kaiba.learning import admission_paper as ap
from kaiba.learning import autolearn_io as io
from kaiba.learning import strategy_campaign as sc
from kaiba.learning import tax_quote as tq
from kaiba.learning.signal_audit import ExitLadder


@pytest.fixture(autouse=True)
def offline_and_raw9_unchanged(monkeypatch):
    pins = sc.source_versions()
    assert len(pins) == 9
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("provider forbidden"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("transport forbidden"))
    yield
    assert sc.source_versions() == pins


def measure(value="0.00", *, basis="provider_reported", receipt_basis="provider_reported", stamp=1000):
    return {"value": value, "basis": basis, "receipt": {
        "provider": "synthetic-fixture", "endpoint": "not-market-evidence",
        "basis": receipt_basis, "observed_at_ms": stamp, "response_digest": "keep-exact"}}


class Reader:
    def __init__(self, rows):
        self.result, self.calls, self.callback = rows, [], object()

    def rows(self, sql, args=()):
        self.calls.append(("rows", sql, args))
        return self.result

    def q(self, *args, **kwargs):
        self.calls.append(("q", args, kwargs))
        return self.result

    def guard(self, *args, **kwargs):
        self.calls.append(("guard", args, kwargs))
        return self.result


def viewed(dossier):
    before = json.dumps(dossier)
    reader = Reader([{"built_at_ms": 1100, "dossier_json": before}])
    result = tq.TaxQuoteReader(reader, chain="bsc", token="token").rows(tq.DOSSIER_SQL, ("bsc", "token"))
    assert len(reader.calls) == 1 and reader.result[0]["dossier_json"] == before
    return json.loads(result[0]["dossier_json"]), result is reader.result


def test_exact_tax_value_token_splices_preserve_every_other_byte_and_input():
    raw = (' \n{ "note":"0.00 \\u0061", "nested":{"value":"0.00"},'
           ' "buy_tax_bps" : {"value" : "0.00", "basis":"cached",'
           '"receipt":{"basis":"cached","observed_at_ms":1000,"note":"0.00"}},'
           '"sell_tax_bps":{"basis":"provider_reported","receipt":{"basis":"provider_reported",'
           '"observed_at_ms":999},"value":"100.0000"}, "unicode":"💰" }\t')
    expected = raw.replace('"value" : "0.00"', '"value" : "0"').replace('"value":"100.0000"', '"value":"100"')
    original = [{"built_at_ms": 1100, "dossier_json": raw, "other": {"bytes": b"unchanged"}}]
    frozen = copy.deepcopy(original)
    reader = Reader(original)
    result = tq.TaxQuoteReader(reader, chain="bsc", token="token").rows(tq.DOSSIER_SQL, ["bsc", "token"])
    assert result[0]["dossier_json"].encode() == expected.encode()
    assert original == frozen and result is not original
    assert result[0]["other"] is original[0]["other"]
    assert len(reader.calls) == 1


@pytest.mark.parametrize("value,expected", [("0.00", "0"), ("100.0000", "100"), ("1E+2", "100"),
                                            ("9999.000", "9999"), ("-0.00", "0")])
def test_explicit_integral_decimal_only(value, expected):
    dossier, unchanged = viewed({"buy_tax_bps": measure(value), "sell_tax_bps": measure(value)})
    assert not unchanged and dossier["buy_tax_bps"]["value"] == dossier["sell_tax_bps"]["value"] == expected


@pytest.mark.parametrize("value", [None, True, False, 0, 100, 0.5, "", " 0.00", "0.1", "NaN", "Infinity",
                                  "-1.00", "10000.000", "1e999999999", "1e-999999999", "1" * 129])
def test_unsupported_numeric_input_is_unchanged(value):
    before = {"buy_tax_bps": measure(value)}
    dossier, unchanged = viewed(before)
    assert unchanged and dossier == before


@pytest.mark.parametrize("change", ["missing_basis", "missing_receipt", "missing_receipt_basis", "missing_clock",
                                   "unavailable", "stale", "estimated", "receipt_stale", "clock_bool", "clock_zero"])
def test_no_default_basis_receipt_or_observation_clock(change):
    tax = measure()
    if change == "missing_basis":
        tax.pop("basis")
    elif change == "missing_receipt":
        tax.pop("receipt")
    elif change == "missing_receipt_basis":
        tax["receipt"].pop("basis")
    elif change == "missing_clock":
        tax["receipt"].pop("observed_at_ms")
    elif change in ("unavailable", "stale", "estimated"):
        tax["basis"] = change
    elif change == "receipt_stale":
        tax["receipt"]["basis"] = "stale"
    else:
        tax["receipt"]["observed_at_ms"] = True if change == "clock_bool" else 0
    dossier, unchanged = viewed({"buy_tax_bps": tax})
    assert unchanged and dossier["buy_tax_bps"] == tax


def test_different_queries_identities_q_guard_and_callback_delegate_exactly():
    raw = json.dumps({"buy_tax_bps": measure()})
    original = [{"dossier_json": raw}]
    reader = Reader(original)
    view = tq.TaxQuoteReader(reader, chain="bsc", token="one")
    for sql, args in ((tq.DOSSIER_SQL + " ", ("bsc", "one")), (tq.DOSSIER_SQL, ("sol", "one")),
                      (tq.DOSSIER_SQL, ("bsc", "two")), ("SELECT other", ("bsc", "one"))):
        assert view.rows(sql, args) is original
        assert reader.calls[-1] == ("rows", sql, args)
    assert view.q("SELECT 1", (7,), dictionaries=False) is original
    assert reader.calls[-1] == ("q", ("SELECT 1", (7,)), {"dictionaries": False})
    assert view.guard("same", reason="untouched") is original
    assert reader.calls[-1] == ("guard", ("same",), {"reason": "untouched"})
    assert view.callback is reader.callback


@pytest.mark.parametrize("method", ["rows", "q", "guard"])
def test_source_deferred_and_callback_errors_propagate_without_retry(monkeypatch, method):
    reader, error, calls = Reader([]), io.SourceDeferred("wal_over_limit:fixture"), []
    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise error
    monkeypatch.setattr(reader, method, fail)
    view = tq.TaxQuoteReader(reader, chain="bsc", token="token")
    with pytest.raises(io.SourceDeferred) as observed:
        getattr(view, method)(tq.DOSSIER_SQL, ("bsc", "token"))
    assert observed.value is error and len(calls) == 1


@pytest.mark.parametrize("raw", ["", "null", "[]", "{bad", '{"buy_tax_bps":{"value":"0.00","value":"1"}}',
                                '{"x":NaN}', " " * (tq.MAX_DOSSIER_BYTES + 1), '{"x":"' + "💰" * 70000 + '"}'],
                         ids=["empty", "null", "array", "invalid", "duplicate", "nonfinite", "character-cap", "utf8-cap"])
def test_malformed_duplicate_and_oversize_inputs_are_bounded_noops(raw):
    reader = Reader([{"dossier_json": raw}])
    assert tq.TaxQuoteReader(reader, chain="bsc", token="token").rows(tq.DOSSIER_SQL, ("bsc", "token")) is reader.result
    assert len(reader.calls) == 1


def _db_path(conn):
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


def _seed_quote(conn, now, tax, *, stamp=None, basis="provider_reported"):
    token = "0x" + "a" * 40
    conn.execute("INSERT OR IGNORE INTO tokens(chain,address,decimals,first_seen_ms,migrated_ms) VALUES(?,?,?,?,?)",
                 ("robinhood", token, 18, 1, 1))
    conn.execute("INSERT OR REPLACE INTO native_prices(chain,ts_ms,price_usd,source) VALUES(?,?,?,?)",
                 ("eth", now, "1000", "synthetic-fixture-not-market-evidence"))
    def typed(value):
        return json.loads(Measure(value=Decimal(value), basis="provider_reported", receipt=Receipt(
            provider="synthetic-fixture", endpoint="not-market-evidence", basis="provider_reported",
            observed_at_ms=now)).model_dump_json())
    dossier = {"address": token, "chain": "robinhood", "price_usd": typed("1"), "liquidity_usd": typed("1000000"),
               "buy_tax_bps": measure(tax, stamp=now if stamp is None else stamp, basis=basis),
               "sell_tax_bps": measure(tax, stamp=now if stamp is None else stamp, basis=basis)}
    raw = json.dumps(dossier)
    conn.execute("INSERT INTO token_dossiers(chain,address,built_at_ms,dossier_json) VALUES(?,?,?,?) "
                 "ON CONFLICT(chain,address) DO UPDATE SET built_at_ms=excluded.built_at_ms,dossier_json=excluded.dossier_json",
                 ("robinhood", token, now, raw))
    return token, raw


def test_actual_frozen_quote_repairs_only_tax_and_does_not_rewrite_canonical(tmp_db, tmp_path):
    token, raw = _seed_quote(tmp_db, 1000, "100.0000")
    reader = io.GuardedReader(_db_path(tmp_db))
    original = io.quote_evidence(reader, "robinhood", token, now_ms=1100, max_age_ms=60_000, cache_root=tmp_path / "empty")
    query_count = reader.queries
    wrapped = tq.quote_evidence(reader, "robinhood", token, now_ms=1100, max_age_ms=60_000, cache_root=tmp_path / "empty")
    assert original["buy_tax_bps"] is original["sell_tax_bps"] is None
    assert wrapped["buy_tax_bps"] == wrapped["sell_tax_bps"] == 100
    assert {k: v for k, v in original.items() if k not in ("buy_tax_bps", "sell_tax_bps")} == {
        k: v for k, v in wrapped.items() if k not in ("buy_tax_bps", "sell_tax_bps")}
    assert reader.queries - query_count == query_count
    assert tmp_db.execute("SELECT dossier_json FROM token_dossiers").fetchone()[0] == raw


@pytest.mark.parametrize("condition,known,synthetic_venue", [
    ("zero", True, False), ("hundred", True, False), ("stale", False, False),
    ("future", False, False), ("unavailable", False, False), ("nonintegral", False, False),
    ("zero", True, True),
])
def test_actual_quote_admission_broker_keeps_cost_unknown_without_venue(tmp_db, tmp_path, condition, known, synthetic_venue):
    selected = 1_800_000_000_000
    clock = {"ms": selected + 31_000}
    manifest = ap.generate_admission_campaign([], ExitLadder(), now_ms=selected,
        groups=[("sm-trenches", "robinhood")], max_candidates=2)
    # A disposable one-arm broker fixture; no production manifest/family is modified.
    manifest["hypotheses"] = [a for a in manifest["hypotheses"] if a["spec"]["exit"]["hold_ms"] == 60_000]
    assert len(manifest["hypotheses"]) == 1
    manifest["campaign_id"] = sc._campaign_id(manifest)
    runner = ap.AdmissionPaperRunner(tmp_path / "learning/paper", learning_root=tmp_path / "learning",
                                    max_stake_usd="30", clock=lambda: clock["ms"] / 1000)

    def packet():
        tax = "100.0000" if condition == "hundred" else "0.5" if condition == "nonintegral" else "0.00"
        stamp = clock["ms"] - 60_001 if condition == "stale" else clock["ms"] + 1 if condition == "future" else clock["ms"]
        token, raw = _seed_quote(tmp_db, clock["ms"], tax, stamp=stamp,
                                 basis="unavailable" if condition == "unavailable" else "provider_reported")
        quote = tq.quote_evidence(io.GuardedReader(_db_path(tmp_db)), "robinhood", token,
                                  now_ms=clock["ms"], max_age_ms=60_000, cache_root=tmp_path / "empty")
        assert tmp_db.execute("SELECT dossier_json FROM token_dossiers").fetchone()[0] == raw
        assert (quote["buy_tax_bps"] is not None) is known
        assert (quote["sell_tax_bps"] is not None) is known
        if synthetic_venue:
            quote.update(venue_fee_bps=60, venue_fee_source="SYNTHETIC_API_POSITIVE_ONLY_NOT_REAL_VENUE",
                         venue_fee_ts_ms=quote["quote_ts_ms"])
        return token, quote

    token, quote = packet()
    opportunity = {"decision_id": "synthetic-source-opportunity", "ts_ms": selected + 1000,
        "signal_ms": selected + 1000, "lane": "sm-trenches", "chain": "robinhood", "token": token,
        "mode": "shadow", "paper_stake_usd": "30", "features": {}}
    opened = runner.run_candidates(manifest, [opportunity], {("robinhood", token): quote}, now_ms=clock["ms"])
    assert not opened["errors"] and opened["arms"][0]["open"] == 1
    clock["ms"] += 72_000
    token, quote = packet()
    closed = runner.run_candidates(manifest, [], {("robinhood", token): quote}, now_ms=clock["ms"])
    assert not closed["errors"] and len(closed["outcomes"]) == 1
    row = closed["outcomes"][0]
    assert row["timing_complete"] and row["inventory_complete"] and row["commission_complete"]
    assert row["tax_complete"] is known and row["venue_complete"] is synthetic_venue
    assert row["cost_complete"] is (known and synthetic_venue)
    assert row["evidence_complete"] is (known and synthetic_venue)
    assert row["cost_model_assumed"] and row["live_cost_verified"] is False
    assert all(e["tax_complete"] is known for e in row["fill_evidence"])
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert tmp_db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
