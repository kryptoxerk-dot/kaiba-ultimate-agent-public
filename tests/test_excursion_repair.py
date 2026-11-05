"""Historical excursion repair: temporary SQLite only; no services or network."""
from __future__ import annotations

import importlib
import sqlite3

import pytest


def api():
    spec = importlib.util.find_spec("kaiba.learning.excursion_repair")
    assert spec is not None, "missing evidence-preserving excursion repair utility"
    return importlib.import_module("kaiba.learning.excursion_repair")


@pytest.fixture
def conn(tmp_db):
    yield tmp_db


def seed(conn, pid="p1", *, mode="live", entry="100", peak="246", mfe=0.0, mae=0.0):
    conn.execute(
        "INSERT INTO positions (position_id,chain,token,lane,mode,opened_ms,closed_ms,"
        "qty,qty_total,cost_native,proceeds_native,realized_native,entry_price_usd,"
        "peak_price_usd,stop_price_usd,mae_pct,mfe_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, "sol", "token-" + pid, "sm_trenches", mode, 1000, 2000,
         "0", "10", "1000", "800", "-200", entry, peak, "70", mae, mfe),
    )
    conn.execute(
        "INSERT INTO trades (trade_id,position_id,chain,token,lane,mode,opened_ms,closed_ms,"
        "hold_s,cost_native,proceeds_native,pnl_native,pnl_pct,mae_pct,mfe_pct) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("t-" + pid, pid, "sol", "token-" + pid, "sm_trenches", mode, 1000, 2000,
         1, "1000", "800", "-200", -20.0, mae, mfe),
    )
    conn.commit()


def mark(conn, pid="p1", *, price="100", ts=1500, return_pct=0.0):
    conn.execute(
        "INSERT INTO position_marks (position_id,ts_ms,price_usd,return_pct,mae_pct,mfe_pct) "
        "VALUES (?,?,?,?,?,?)", (pid, ts, price, return_pct, min(0.0, return_pct), max(0.0, return_pct)),
    )
    conn.commit()


def test_observed_flat_paper_mark_preserves_measured_zeros(conn):
    seed(conn, mode="shadow", peak="100")
    mark(conn)
    manifest = api().inventory(conn, cutoff_ms=3000)
    item = manifest["items"][0]
    assert manifest["changes"] == []
    assert item["measurements"]["mfe_pct"] == {
        "value": 0.0, "basis": "sampled_zero", "samples": 1,
    }
    assert item["measurements"]["mae_pct"]["basis"] == "sampled_zero"
    assert manifest["snapshot"]["sources"]["p1"]["marks"][0]["price_usd"] == "100"


@pytest.mark.parametrize("column,value", [
    ("chain", "bsc"), ("token", "other"), ("opened_ms", 999), ("closed_ms", 1999),
    ("lane", "other"), ("mode", "shadow"),
])
def test_trade_position_identity_mismatch_blocks_all_changes(conn, column, value):
    seed(conn)
    conn.execute(f"UPDATE trades SET {column}=?", (value,))
    conn.commit()
    manifest = api().inventory(conn, cutoff_ms=3000)
    assert manifest["changes"] == []
    assert "trade_position_identity_mismatch" in manifest["items"][0]["blockers"]


def test_apply_is_explicit_transactional_idempotent_and_preserves_accounting(conn):
    seed(conn)
    module = api()
    assert hasattr(module, "apply_manifest"), "missing authorized transactional CAS API"
    manifest = module.inventory(conn, cutoff_ms=3000)
    with pytest.raises(PermissionError):
        module.apply_manifest(conn, manifest)
    result = module.apply_manifest(conn, manifest, authorize=True)
    assert result["status"] == "applied" and result["changed_fields"] == 4
    assert conn.execute("SELECT mfe_pct,mae_pct,qty,cost_native,stop_price_usd FROM positions").fetchone()[:] == (
        None, None, "0", "1000", "70",
    )
    assert conn.execute("SELECT pnl_native,pnl_pct,mfe_pct,mae_pct FROM trades").fetchone()[:] == (
        "-200", -20.0, None, None,
    )
    assert module.apply_manifest(conn, manifest, authorize=True)["status"] == "already_applied"
    assert module.inventory(conn, cutoff_ms=3000)["changes"] == []
    journal = conn.execute("SELECT manifest_json,status FROM excursion_repair_runs").fetchone()
    assert journal[1] == "applied"
    assert __import__("json").loads(journal[0]) == manifest


def test_unwritten_zero_is_unknown_not_measured_flat_or_trusted_peak(conn):
    seed(conn)
    before = conn.total_changes
    manifest = api().inventory(conn, cutoff_ms=3000)
    item = manifest["items"][0]
    assert item["peak_candidate"]["mfe_pct"] == 146.0
    assert item["peak_candidate"]["basis"] == "unverified_retained_peak"
    assert item["measurements"]["mfe_pct"]["value"] is None
    assert item["measurements"]["mae_pct"]["value"] is None
    assert item["measurements"]["mfe_pct"]["basis"] == "unavailable"
    assert len(manifest["changes"]) == 4
    assert all(c["old"] == 0.0 and c["new"] is None for c in manifest["changes"])
    assert conn.total_changes == before
    assert conn.execute("SELECT mfe_pct,mae_pct FROM positions").fetchone()[:] == (0.0, 0.0)
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='excursion_repair_runs'").fetchone() is None
