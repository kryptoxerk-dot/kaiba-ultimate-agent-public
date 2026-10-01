"""Unresolved execution-owner seam: manual deploys require volume as well as wallets.

No production gate or threshold is changed here. This reproduction is handed to the
parent because AUDIT-VOLUME owns intelligence/ingest, not execution/lanes.py.
"""
from __future__ import annotations

from kaiba.core.schemas import Lane
from kaiba.execution import lanes
from kaiba.intelligence import tracker
from tests.test_bsc_lane_inputs import (
    BSC, HOUR, NOW, _ctx, _evm, _feed_trade, _feed_wallet, _three_smart_buyers, _tok,
)


def test_manual_deploy_with_five_smart_buyers_cannot_pass_unknown_volume(tmp_db):
    token = _tok(905)
    _three_smart_buyers(tmp_db, BSC, token)
    for i in (4, 5):
        wallet = _evm(i)
        _feed_wallet(tmp_db, BSC, wallet, start=NOW - (12 + 3 * i) * HOUR)
        _feed_trade(tmp_db, BSC, wallet, token, "buy", NOW - 30_000, usd="300")
    tracker.seed_from_cohorts(BSC, tmp_db)
    ctx = _ctx(tmp_db, BSC, token, rug_ratio=None)
    assert lanes.DEFAULT_PARAMS[Lane.SM_TRENCHES]["manual_min_smart_degen"] == 5
    assert lanes.sm_trenches(ctx) is not None, "control: launchpad passes the unchanged gates"
    manual = ctx.model_copy(update={"token_meta": ctx.token_meta.model_copy(update={"launchpad": None})})
    assert manual.dossier.volume_24h_usd.value is None
    assert lanes.sm_trenches(manual) is None, "manual launch passed on wallets without volume evidence"
