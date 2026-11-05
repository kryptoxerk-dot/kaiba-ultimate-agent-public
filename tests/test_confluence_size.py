"""The pre-registered sizing rule for confluence-5 (proven): what may be PROPOSED, and when.

What must hold, because each is a way a sizing argument has fooled this book before:

* below 30 closed paper trades the answer is NOT_YET, however good the mean looks;
* the 5 pp paper-to-live haircut is applied BEFORE the interval;
* a level that does not beat the levels below it is a REJECT ("keep it flat");
* grade-route trades and trades opened before the rule was registered never count;
* the pooled line never proposes anything.
"""

from __future__ import annotations

import itertools
from decimal import Decimal

from kaiba.core.db import jdump
from kaiba.learning import confluence_size as CS

T0 = CS.RULE_REGISTERED_MS + 3_600_000
_n = itertools.count(1)


def _trade(conn, *, chain="robinhood", level=3, ret=0.2, source="proven", opened_ms=None, lane="confluence-5"):
    i = next(_n)
    opened = opened_ms if opened_ms is not None else T0 + i * 60_000
    sig, dec = f"sig{i}", f"dec{i}"
    conn.execute(
        "INSERT INTO signals (signal_id, lane, chain, token, strength, created_ms, payload_json) "
        "VALUES (?,?,?,?,0.75,?,?)",
        (sig, lane, chain, f"tok{i}", opened - 5_000,
         jdump({"wallet_source": source, "confluence_level": level, "cohort_id": "proven:x:1"})),
    )
    conn.execute(
        "INSERT INTO decisions (decision_id, ts_ms, lane, mode, chain, token, action, signals_json) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (dec, opened - 1_000, lane, "shadow", chain, f"tok{i}", "enter", jdump([sig])),
    )
    cost = 1_000_000
    conn.execute(
        "INSERT INTO trades (trade_id, position_id, decision_id, lane, mode, chain, token, opened_ms, "
        "closed_ms, hold_s, cost_native, proceeds_native, pnl_native, pnl_pct) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"trd{i}", f"pos{i}", dec, lane, "shadow", chain, f"tok{i}", opened, opened + 600_000, 600,
         str(cost), str(int(cost * (1 + ret))), str(int(cost * ret)), ret * 100),
    )


def _verdict(conn, chain="robinhood", level=3):
    report = CS.report(conn, with_forward=False)
    return next(s for s in report["levels"][chain] if s["level"] == level), report


def test_under_thirty_trades_is_not_yet_however_good(tmp_db):
    for _ in range(29):
        _trade(tmp_db, ret=0.5)
    st, _ = _verdict(tmp_db)
    assert st["n"] == 29 and st["verdict"] == "NOT_YET"


def test_thirty_consistent_winners_at_a_level_that_beats_the_rest_are_proposed(tmp_db):
    for i in range(30):
        _trade(tmp_db, level=4, ret=0.25 + (i % 5) * 0.02)
    for _ in range(10):
        _trade(tmp_db, level=2, ret=-0.1)
    st, report = _verdict(tmp_db, level=4)
    assert st["verdict"] == "PROPOSE", st["reasons"]
    assert {"chain": "robinhood", "level_ge": 4} in report["proposals"]
    assert report["rule_digest"] == CS.rule_digest()


def test_the_haircut_comes_off_before_the_interval(tmp_db):
    """+4% per trade with no spread: positive raw, negative after the 5 pp haircut."""
    for _ in range(30):
        _trade(tmp_db, ret=0.04)
    st, _ = _verdict(tmp_db)
    assert st["mean_pct"] > 0
    assert st["verdict"] == "REJECT"
    assert any("haircut" in r for r in st["reasons"])


def test_the_lower_bound_is_taken_after_the_haircut(tmp_db):
    """+30% / -14% alternating: raw lower bound ~ +1.4%, haircut mean +3%, haircut lower
    bound below zero. Bounding the RAW mean and subtracting later would propose this."""
    for i in range(30):
        _trade(tmp_db, ret=0.30 if i % 2 == 0 else -0.14)
    st, _ = _verdict(tmp_db)
    assert st["haircut_mean_pct"] > 0
    assert st["lower95_pct"] < 0
    assert st["verdict"] == "REJECT"


def test_a_level_that_does_not_beat_the_levels_below_is_rejected(tmp_db):
    for _ in range(30):
        _trade(tmp_db, level=4, ret=0.2)
    for _ in range(30):
        _trade(tmp_db, level=2, ret=0.3)
    st, _ = _verdict(tmp_db, level=4)
    assert st["verdict"] == "REJECT"
    assert any("keep it flat" in r for r in st["reasons"])


def test_a_decaying_edge_fails_on_the_later_half(tmp_db):
    for _ in range(20):
        _trade(tmp_db, ret=0.6)
    for _ in range(20):
        _trade(tmp_db, ret=-0.06)
    st, _ = _verdict(tmp_db, level=2)
    assert st["second_half_pct"] < 0
    assert st["verdict"] == "REJECT"


def test_levels_are_cumulative(tmp_db):
    _trade(tmp_db, level=5, ret=0.1)
    _trade(tmp_db, level=3, ret=0.1)
    report = CS.report(tmp_db, with_forward=False)
    by_level = {s["level"]: s["n"] for s in report["levels"]["robinhood"]}
    assert by_level == {2: 2, 3: 2, 4: 1, 5: 1}


def test_grade_route_and_pre_registration_trades_never_count(tmp_db):
    _trade(tmp_db, source="grade", ret=0.5)
    _trade(tmp_db, opened_ms=CS.RULE_REGISTERED_MS - 1, ret=0.5)
    _trade(tmp_db, lane="sm-trenches", ret=0.5)
    _trade(tmp_db, ret=0.1)
    trades, skipped = CS.collect_paper_trades(tmp_db)
    assert len(trades) == 1
    assert skipped == {"wallet_source_grade": 1}


def test_each_chain_is_judged_alone_and_pooled_never_proposes(tmp_db):
    for i in range(20):
        _trade(tmp_db, chain="sol", level=3, ret=0.3 + (i % 3) * 0.01)
        _trade(tmp_db, chain="robinhood", level=3, ret=0.3 + (i % 3) * 0.01)
    report = CS.report(tmp_db, with_forward=False)
    pooled = next(s for s in report["levels"]["pooled"] if s["level"] == 3)
    assert pooled["n"] == 40 and pooled["verdict"] == "INFO_ONLY"
    assert next(s for s in report["levels"]["sol"] if s["level"] == 3)["verdict"] == "NOT_YET"
    assert report["proposals"] == []


def test_signal_forward_prices_every_proven_signal_from_the_tape(tmp_db):
    """Entry at the next print, mark at the horizon; a token that never prints again is -100%."""
    def swap(token, ts, price):
        tmp_db.execute(
            "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, price_usd, source) "
            "VALUES ('robinhood',?,?,?,?,'buy','1',?,'test')",
            (f"tx{next(_n)}", ts, "0xw", token, str(price)),
        )

    for i, (tok, exit_px) in enumerate((("0xa", 2.0), ("0xb", None))):
        created = T0 + i * 1_000_000
        tmp_db.execute(
            "INSERT INTO signals (signal_id, lane, chain, token, strength, created_ms, payload_json) "
            "VALUES (?,?,?,?,0.75,?,?)",
            (f"fs{i}", "confluence-5", "robinhood", tok, created,
             jdump({"wallet_source": "proven", "confluence_level": 3})),
        )
        swap(tok, created + 6_000, 1.0)
        if exit_px is not None:
            swap(tok, created + 3_600_000 + 1_000, exit_px)
    out = CS.signal_forward(tmp_db, horizons_s=(3_600,))
    cell = next(r for r in out["table"] if r["level_ge"] == 3)
    assert cell["n"] == 2
    assert out["counts"]["dead"] == 1
    assert abs(cell["mean_pct"] - ((2.0 - 1 - 0.02) + (-1.0)) / 2 * 100) < 1e-6


def test_the_rule_is_what_was_registered():
    """Changing the rule must be a visible act: the digest is pinned here."""
    assert CS.RULE.min_trades == 30
    assert CS.RULE.live_haircut == Decimal("0.05")
    assert CS.RULE.alpha == 0.05
    assert CS.rule_digest() == CS.rule_digest(CS.SizeRule())
