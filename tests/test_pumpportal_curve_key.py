"""PumpPortal's ``bondingCurveKey`` is not the curve on a mayhem-mode create; the stored key must be.

``kaiba.ingest.pumpportal`` writes the curve key into ``tokens.pool`` and
``tokens.meta_json.bonding_curve``. MEASURED 2026-10-04: on 11.9% of a day's pump.fun creates
(mayhem mode) the frame's key was ``BwWK17cb...``, a data-less system account that trades
(it is a wallet, not a curve), while the program address ``["bonding-curve", mint]`` held
the curve. These tests feed the real
mint -> frame key -> curve triples pinned in ``tests/test_snipe_launch_feed.py`` through the
parser and the database writer, and fail if the frame's key is what gets stored.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kaiba.core.db import fetch_one, jload
from kaiba.ingest import launch_feed, pumpportal
from tests.test_snipe_launch_feed import PUMP_CREATES

FRAME = Path(__file__).parent / "fixtures" / "ingest" / "pumpportal_new_token.json"

#: The two creates whose frame named the empty system account rather than the curve.
MAYHEM = [c for c in PUMP_CREATES if c[1] != c[2]]


def create_frame(mint: str, frame_key: str | None, *, pool: str | None = "pump") -> dict:
    """The recorded ``subscribeNewToken`` frame, re-pointed at a real mint and frame key."""
    msg = json.loads(FRAME.read_text(encoding="utf-8"))
    msg["mint"] = mint
    if frame_key is None:
        msg.pop("bondingCurveKey", None)
    else:
        msg["bondingCurveKey"] = frame_key
    if pool is None:
        msg.pop("pool", None)
    else:
        msg["pool"] = pool
    return msg


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    pumpportal.reset_latency()
    # Tier-0 triage is advisory and has its own tests; keep these about the stored key.
    monkeypatch.setattr(pumpportal, "_screen", lambda msg, conn: None)
    yield
    pumpportal.reset_latency()


def test_the_fixture_really_contains_mayhem_creates():
    """Guard the guard: without a frame/curve disagreement these tests prove nothing."""
    assert len(MAYHEM) == 2
    assert all(frame == "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s" for _, frame, _ in MAYHEM)


@pytest.mark.parametrize("mint,frame_key,curve", PUMP_CREATES)
def test_a_pump_create_stores_the_derived_curve_and_keeps_the_frame_key(mint, frame_key, curve):
    token = pumpportal.parse_new_token(create_frame(mint, frame_key))
    assert token is not None
    assert token.pool == curve
    assert token.meta["bonding_curve"] == curve
    assert token.meta["bonding_curve_frame"] == frame_key


@pytest.mark.parametrize("mint,frame_key,curve", MAYHEM)
def test_the_tokens_row_carries_the_curve_on_a_mayhem_create(tmp_db, mint, frame_key, curve):
    """``tokens.pool`` is what ``bundles.analyse`` reads; the meta is what ``launch_feed`` reads."""
    assert pumpportal.handle_message(json.dumps(create_frame(mint, frame_key)), conn=tmp_db) == \
        pumpportal.STREAM_NEW_TOKEN
    row = fetch_one(tmp_db, "SELECT pool, meta_json FROM tokens WHERE chain='sol' AND address=?", (mint,))
    meta = jload(row["meta_json"], {})
    assert row["pool"] == curve != frame_key
    assert meta["bonding_curve"] == curve
    assert meta["bonding_curve_frame"] == frame_key


@pytest.mark.parametrize("mint,frame_key,curve", MAYHEM)
def test_a_pump_create_without_a_pool_field_is_still_derived(mint, frame_key, curve):
    """PumpPortal's default pool is pump.fun, here and in ``launch_feed.sol_launch_from_row``."""
    token = pumpportal.parse_new_token(create_frame(mint, frame_key, pool=None))
    assert token is not None and token.pool == curve and token.meta["pool"] == "pump"


def test_a_pump_create_without_a_frame_key_still_gets_its_curve():
    mint, _, curve = MAYHEM[0]
    token = pumpportal.parse_new_token(create_frame(mint, None))
    assert token is not None
    assert token.pool == curve and token.meta["bonding_curve"] == curve
    assert "bonding_curve_frame" not in token.meta  # absent, not invented


def test_a_launchlab_create_keeps_its_own_key():
    """``bonk`` is Raydium LaunchLab: no pump.fun derivation applies to its pool key."""
    mint, frame_key, _ = MAYHEM[0]
    token = pumpportal.parse_new_token(create_frame(mint, frame_key, pool="bonk"))
    assert token is not None
    assert token.pool == frame_key and token.meta["bonding_curve"] == frame_key


def test_a_failed_derivation_falls_back_to_the_frame_key_and_keeps_the_row(tmp_db, monkeypatch):
    """A doubtful curve key is worth more than a dropped creation."""

    def explode(mint):
        raise RuntimeError("derivation is having a bad day")

    monkeypatch.setattr(launch_feed, "pump_curve_address", explode)
    mint, frame_key, _ = MAYHEM[0]
    assert pumpportal.handle_message(create_frame(mint, frame_key), conn=tmp_db) == pumpportal.STREAM_NEW_TOKEN
    row = fetch_one(tmp_db, "SELECT pool FROM tokens WHERE chain='sol' AND address=?", (mint,))
    assert row is not None and row["pool"] == frame_key
