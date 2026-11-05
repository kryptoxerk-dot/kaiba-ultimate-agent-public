"""The smart-flow queue must be able to reach past tokens that are cooling down.

MEASURED 2026-11-04 on sol over 24 h: 1,075 smart-flow scans hit 211 tokens, and 76% of the
tokens that reached three smart wallets got no scan within an hour. ``next_work`` dropped a
cooling token after the slice was taken, so the top of the list held its slots. The fix sits
behind ``smart_flow_skip_cooling`` because it raises live entries: off must be exactly the old
behaviour, and on must reach the lower token with the same number of slots.
"""

from __future__ import annotations

import pytest

from kaiba.core.schemas import Chain, now_ms
from kaiba.execution import lanes as lanes_mod
from kaiba.execution import scanner

CROWDED = "ErR5nqFX3XVtzYvoeTqHQHqjFb6Zb4WkUGE5dWkwpump"
QUIET = "6d27YXn6Lct1dqRw4oYvVTRtikHSh2Qjp7BYjz2FozRm"


def seed(conn, token: str, wallets: int) -> None:
    for i in range(wallets):
        wallet = f"{token[:6]}{i}"
        conn.execute(
            "INSERT OR REPLACE INTO wallets (chain, address, first_seen_ms, last_seen_ms, tags_json) "
            "VALUES (?,?,?,?,?)",
            (Chain.SOL.value, wallet, now_ms(), now_ms(), '["smart_money"]'),
        )
        conn.execute(
            "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, usd_value, source) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (Chain.SOL.value, f"tx_{wallet}", now_ms() - 10_000, wallet, token, "buy", "1000", "500", "gmgn:smartmoney"),
        )
    conn.commit()


@pytest.fixture
def book(tmp_db):
    scanner.RECENT.clear()
    seed(tmp_db, CROWDED, 5)
    seed(tmp_db, QUIET, 3)
    scanner.RECENT.mark(Chain.SOL, CROWDED)
    yield tmp_db
    scanner.RECENT.clear()


@pytest.fixture
def switch_on(monkeypatch):
    monkeypatch.setattr(scanner, "_smart_flow_skips_cooling", lambda: True)


def tokens(items) -> list[str]:
    return [i.token for i in items]


def test_off_is_the_old_behaviour_the_cooling_token_keeps_the_slot(book):
    assert tokens(scanner._smart_flow_work(book, 1, config=scanner.DEFAULT_CONFIG)) == [CROWDED]


def test_off_next_work_does_not_reach_the_lower_token(book):
    assert QUIET not in tokens(scanner.next_work(book, 1, config=scanner.DEFAULT_CONFIG))


def test_on_a_cooling_token_gives_up_its_slot(book, switch_on):
    assert tokens(scanner._smart_flow_work(book, 1, config=scanner.DEFAULT_CONFIG)) == [QUIET]


def test_on_next_work_reaches_the_lower_token_with_the_same_slots(book, switch_on):
    assert tokens(scanner.next_work(book, 1, config=scanner.DEFAULT_CONFIG)) == [QUIET]


def test_on_the_crowded_token_returns_when_its_cooldown_ends(book, switch_on):
    scanner.RECENT.clear()
    assert tokens(scanner._smart_flow_work(book, 2, config=scanner.DEFAULT_CONFIG)) == [CROWDED, QUIET]


@pytest.mark.parametrize("params, expected", [({}, False), ({"smart_flow_skip_cooling": False}, False), ({"smart_flow_skip_cooling": True}, True)])
def test_the_switch_reads_the_lane_param_and_defaults_off(monkeypatch, params, expected):
    monkeypatch.setattr(lanes_mod.LaneContext, "lane_params", lambda self, lane: params)
    assert scanner._smart_flow_skips_cooling() is expected


def test_an_unreadable_switch_stays_off(monkeypatch):
    def boom(self, lane):
        raise ValueError("no config")

    monkeypatch.setattr(lanes_mod.LaneContext, "lane_params", boom)
    assert scanner._smart_flow_skips_cooling() is False
