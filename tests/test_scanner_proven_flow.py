"""The scanner offers the tokens PROVEN wallets are buying, when the lane counts them.

MEASURED 2026-10-02 on the box: of 19 robinhood tokens where five B-graded wallets
net-bought within 120 s over seven days, 3 were scanned within +-120 s and none within
30 s of qualifying. A lane is only as good as the work it is offered.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kaiba.core.schemas import Chain, now_ms
from kaiba.execution import scanner
from kaiba.learning import proven as P

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "GMFCWQv8CfnjGR2xCJebyxqSsCmyMo31pXcCEnfDpump"
OTHER = "EfcmyGs6M8auzbHmyW3tki8WbZh2rnbb3bS4eiyUHedq"
WALLETS = [f"prov{i}AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" for i in range(4)]


@pytest.fixture(autouse=True)
def _reset():
    P.clear_cache()
    scanner._PROVEN_NEXT_AT.clear()
    scanner._PROVEN_OFFERED.clear()
    yield
    P.clear_cache()
    scanner._PROVEN_NEXT_AT.clear()
    scanner._PROVEN_OFFERED.clear()


def _risk(tmp_path, monkeypatch, **params) -> None:
    raw = yaml.safe_load((ROOT / "config" / "risk.yaml").read_text(encoding="utf-8"))
    lane = raw["lanes"]["confluence-5"]
    lane["chains"] = ["sol"]
    lane["params"] = {**lane.get("params", {}), **params}
    path = tmp_path / "risk.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))


PROVEN_PARAMS = {"wallet_source": "proven", "min_entities": 3, "window_s": 1800, "max_signal_age_s": 120}


def _freeze(conn, members, at_ms=None):
    at = at_ms if at_ms is not None else now_ms() - 3_600_000
    report = P.ProvenReport(chain=Chain.SOL, as_of_ms=at, config=P.ProvenConfig(), split_ms=at - 86_400_000)
    report.proven = [P.WalletEvidence(wallet=w, source="test") for w in members]
    P.freeze(conn, report)
    P.clear_cache()


def _buy(conn, wallet, token, age_s=10):
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, usd_value, source) "
        "VALUES ('sol',?,?,?,?,'buy','1000','500','pumpfun:trades')",
        (f"tx_{wallet}_{token}_{age_s}", now_ms() - age_s * 1000, wallet, token),
    )


def _work(conn):
    scanner._PROVEN_NEXT_AT.clear()
    return scanner._proven_flow_work(conn, 10, config=scanner.DEFAULT_CONFIG)


def test_a_token_three_proven_wallets_are_buying_is_offered(tmp_db, tmp_path, monkeypatch):
    _risk(tmp_path, monkeypatch, **PROVEN_PARAMS)
    _freeze(tmp_db, WALLETS)
    for w in WALLETS[:3]:
        _buy(tmp_db, w, TOKEN)
    _buy(tmp_db, WALLETS[0], OTHER)  # one proven buyer is not confluence
    work = _work(tmp_db)
    assert [w.token for w in work] == [TOKEN]
    assert work[0].source == "proven_flow"
    assert work[0].extras["proven_buyers"] == 3
    assert tmp_db.execute("SELECT 1 FROM tokens WHERE address=?", (TOKEN,)).fetchone() is not None


def test_nothing_is_offered_while_the_lane_counts_grades(tmp_db, tmp_path, monkeypatch):
    _risk(tmp_path, monkeypatch, **{**PROVEN_PARAMS, "wallet_source": "grade"})
    _freeze(tmp_db, WALLETS)
    for w in WALLETS[:3]:
        _buy(tmp_db, w, TOKEN)
    assert _work(tmp_db) == []


def test_buyers_outside_the_cohort_do_not_count(tmp_db, tmp_path, monkeypatch):
    _risk(tmp_path, monkeypatch, **PROVEN_PARAMS)
    _freeze(tmp_db, WALLETS[:2])
    for w in WALLETS[:3]:
        _buy(tmp_db, w, TOKEN)
    assert _work(tmp_db) == []


def test_a_token_past_the_lanes_age_gate_is_not_offered(tmp_db, tmp_path, monkeypatch):
    _risk(tmp_path, monkeypatch, **PROVEN_PARAMS)
    _freeze(tmp_db, WALLETS)
    for w in WALLETS[:3]:
        _buy(tmp_db, w, TOKEN, age_s=200)
    assert _work(tmp_db) == []


def test_a_token_is_re_offered_only_on_new_evidence(tmp_db, tmp_path, monkeypatch):
    _risk(tmp_path, monkeypatch, **PROVEN_PARAMS)
    _freeze(tmp_db, WALLETS)
    for w in WALLETS[:3]:
        _buy(tmp_db, w, TOKEN)
    assert len(_work(tmp_db)) == 1
    assert _work(tmp_db) == []  # same three buyers: nothing new
    _buy(tmp_db, WALLETS[3], TOKEN, age_s=1)
    again = _work(tmp_db)
    assert len(again) == 1 and again[0].extras["proven_buyers"] == 4


def test_the_query_respects_its_minimum_interval(tmp_db, tmp_path, monkeypatch):
    _risk(tmp_path, monkeypatch, **PROVEN_PARAMS)
    _freeze(tmp_db, WALLETS)
    for w in WALLETS[:3]:
        _buy(tmp_db, w, TOKEN)
    assert len(scanner._proven_flow_work(tmp_db, 10, config=scanner.DEFAULT_CONFIG)) == 1
    _buy(tmp_db, WALLETS[3], TOKEN, age_s=1)
    assert scanner._proven_flow_work(tmp_db, 10, config=scanner.DEFAULT_CONFIG) == []


def test_next_work_offers_proven_flow_through_the_rescan_cooldown(tmp_db, tmp_path, monkeypatch):
    """The token was just scanned (two proven buyers then). A third arrives: offer it now,
    because the lane's age gate is shorter than the cooldown."""
    _risk(tmp_path, monkeypatch, **PROVEN_PARAMS)
    _freeze(tmp_db, WALLETS)
    for w in WALLETS[:3]:
        _buy(tmp_db, w, TOKEN)
    scanner.RECENT.mark(Chain.SOL, TOKEN)
    try:
        work = scanner.next_work(tmp_db, 8, config=scanner.DEFAULT_CONFIG)
        assert TOKEN in [w.token for w in work if w.source == "proven_flow"]
    finally:
        scanner.RECENT.clear()


def test_a_stale_or_empty_cohort_offers_nothing(tmp_db, tmp_path, monkeypatch):
    _risk(tmp_path, monkeypatch, **PROVEN_PARAMS)
    _freeze(tmp_db, WALLETS, at_ms=now_ms() - 4 * 86_400_000)
    for w in WALLETS[:3]:
        _buy(tmp_db, w, TOKEN)
    assert _work(tmp_db) == []
    _freeze(tmp_db, [])
    assert _work(tmp_db) == []
