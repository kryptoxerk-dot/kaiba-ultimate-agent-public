"""Live copy trading (owner 2026-10-07): leader choice, the fast feeder, the 60 s time exit.

Synthetic addresses only (AGENTS.md: no owner or agent wallet in tests).
"""

from __future__ import annotations

import json
import time

import pytest
import yaml

from kaiba.core.schemas import EventKind, now_ms
from kaiba.learning import copy_leaders as cl
from tests.test_risk import BASE_RISK, _merge

LEADER = "LeaderWa11et1111111111111111111111111111111"
OTHER = "OtherWa11et22222222222222222222222222222222"
OWNER_PICK = "OwnerPick333333333333333333333333333333333"
TOKEN = "CopyTok3n444444444444444444444444444444pump"


@pytest.fixture
def risk(tmp_path, monkeypatch):
    path = tmp_path / "risk.yaml"
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))

    def write(mode: str = "live", chains=("sol",), hold: int = 60):
        lanes = {"trusted-copy": {"mode": mode, "size_pct_min": 1.0, "size_pct_max": 5.0,
                                  "chains": list(chains),
                                  "params": {"max_copy_delay_s": 20, "max_price_drift_pct": 12,
                                             "max_hold_s": hold}}}
        path.write_text(yaml.safe_dump(_merge(BASE_RISK, {"lanes": lanes})), encoding="utf-8")
    write()
    return write


def swap(conn, wallet, token, side, usd, atoms, ts):
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, usd_value, price_usd, source) "
        "VALUES ('sol',?,?,?,?,?,?,?,?, 'test')",
        (f"tx{wallet[:4]}{token[:4]}{side}{ts}", ts, wallet, token, side, str(atoms), str(usd), "0.001"),
    )


# ------------------------------------------------------------------ choosing leaders

def scalper_rows(n: int, *, win: bool, base: int):
    rows = []
    for k in range(n):
        t0 = base + k * 60_000
        rows.append((f"tok{k}", "buy", 1000, 10.0, t0))
        rows.append((f"tok{k}", "sell", 1000, 12.0 if win else 8.0, t0 + 13_000))
    return rows


def test_a_profitable_scalper_qualifies_and_a_loser_does_not():
    now = now_ms()
    lo = now - 7 * 86_400_000
    got = cl.score_wallet(scalper_rows(10, win=True, base=lo + 1000), lo, now)
    assert got is not None and got.trips == 10 and got.win == 1.0 and abs(got.hold_s - 13.0) < 0.01
    assert cl.score_wallet(scalper_rows(10, win=False, base=lo + 1000), lo, now) is None
    assert cl.score_wallet(scalper_rows(cl.MIN_TRIPS - 1, win=True, base=lo + 1000), lo, now) is None


def test_leaders_go_into_sol_trusted_copy_and_owner_rows_are_never_touched(tmp_db):
    now = now_ms()
    tmp_db.execute("INSERT INTO wallets (chain, address, source, cohort, first_seen_ms, last_seen_ms) "
                   "VALUES ('sol', ?, 'owner', 'trusted_copy', 1, 1)", (OWNER_PICK,))
    first = cl.apply_leaders(tmp_db, [cl.Leader(LEADER, 0.3, 0.8, 12, 13.0)], now=now)
    assert first["added"] == 1
    cohorts = dict(tmp_db.execute("SELECT address, cohort FROM wallets WHERE chain='sol'").fetchall())
    assert cohorts[LEADER] == "trusted_copy" and cohorts[OWNER_PICK] == "trusted_copy"

    # A partial pass only adds; a complete pass demotes the auto leader no longer chosen.
    cl.apply_leaders(tmp_db, [cl.Leader(OTHER, 0.2, 0.7, 9, 10.0)], now=now, complete=False)
    assert dict(tmp_db.execute("SELECT address, cohort FROM wallets").fetchall())[LEADER] == "trusted_copy"
    out = cl.apply_leaders(tmp_db, [cl.Leader(OTHER, 0.2, 0.7, 9, 10.0)], now=now, complete=True)
    cohorts = dict(tmp_db.execute("SELECT address, cohort FROM wallets WHERE chain='sol'").fetchall())
    assert out["demoted"] == 1 and cohorts[LEADER] == "tracked"
    assert cohorts[OWNER_PICK] == "trusted_copy", "an owner-curated wallet is never demoted"
    meta = json.loads(tmp_db.execute("SELECT meta_json FROM wallets WHERE address=?", (OTHER,)).fetchone()[0])
    assert meta["copy_leader"]["auto"] is True

    # The owner's own trusted_copy wallet qualifying as a leader must not become an AUTO row,
    # or the next pass that drops it would demote the owner's pick.
    out = cl.apply_leaders(tmp_db, [cl.Leader(OWNER_PICK, 0.5, 0.9, 20, 8.0)], now=now, complete=True)
    assert out["skipped_owner"] == 1
    cl.apply_leaders(tmp_db, [], now=now, complete=True)
    assert tmp_db.execute("SELECT cohort FROM wallets WHERE address=?", (OWNER_PICK,)).fetchone()[0] == "trusted_copy"


def test_robinhood_trusted_copy_is_the_owners_list_and_is_refused():
    with pytest.raises(ValueError):
        cl.apply_leaders(None, [], chain="robinhood")


# ------------------------------------------------------------------ the fast feeder

def test_copy_flow_offers_a_fresh_leader_buy_once_and_first(tmp_db, risk, monkeypatch):
    from kaiba.execution import scanner as sc

    monkeypatch.setattr(sc, "_COPY_NEXT_AT", {})
    monkeypatch.setattr(sc, "_COPY_OFFERED", {})
    cfg = sc.ScanConfig(copy_flow_min_interval_s=0.0)
    tmp_db.execute("INSERT INTO wallets (chain, address, source, cohort, first_seen_ms, last_seen_ms) "
                   "VALUES ('sol', ?, 'test', 'trusted_copy', 1, 1)", (LEADER,))
    tmp_db.execute("INSERT INTO wallets (chain, address, source, cohort, first_seen_ms, last_seen_ms) "
                   "VALUES ('sol', ?, 'test', 'tracked', 1, 1)", (OTHER,))
    swap(tmp_db, LEADER, TOKEN, "buy", 50, 1000, now_ms() - 3_000)
    swap(tmp_db, LEADER, "OldTok3n", "buy", 50, 1000, now_ms() - 120_000)   # past the 20 s cap
    swap(tmp_db, OTHER, "NotALeader", "buy", 50, 1000, now_ms() - 1_000)    # not in the cohort
    tmp_db.commit()
    work = sc._copy_flow_work(tmp_db, 4, config=cfg)
    assert [(w.token, w.source) for w in work] == [(TOKEN, "copy_flow")]
    assert sc._copy_flow_work(tmp_db, 4, config=cfg) == [], "the same buy is offered once"
    assert sc.next_work(tmp_db, 8, config=cfg) is not None

    risk(mode="off")
    swap(tmp_db, LEADER, "Fresh2", "buy", 50, 1000, now_ms() - 1_000)
    tmp_db.commit()
    assert sc._copy_flow_work(tmp_db, 4, config=cfg) == [], "an OFF lane gets no feed"


# ------------------------------------------------------------------ the 60 s time exit

def _ctx(conn):
    from kaiba.ops import scheduler as S

    now = now_ms()
    return S.JobContext("copy_time_exit", conn, {}, S.ScheduleConfig(), now, now + 30_000)


def test_time_exit_requests_once_for_a_copy_held_past_max_hold(tmp_db, risk):
    from kaiba.ops import scheduler as S

    for pid, age_s, mode, lane in (("old", 61, "live", "trusted-copy"), ("young", 10, "live", "trusted-copy"),
                                   ("paper", 300, "shadow", "trusted-copy"), ("other", 300, "live", "sm-trenches")):
        tmp_db.execute("INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty) "
                       "VALUES (?, 'sol', ?, ?, ?, ?, '1')", (pid, TOKEN, lane, mode, now_ms() - age_s * 1000))
    tmp_db.commit()
    out = S.job_copy_time_exit(_ctx(tmp_db))
    assert out["requested"] == 1 and out["hold_s"] == 60
    rows = tmp_db.execute("SELECT payload FROM events WHERE kind=?", (EventKind.PROTECTION_TRIGGERED.value,)).fetchall()
    payloads = [json.loads(r[0]) for r in rows]
    assert [p["position_id"] for p in payloads] == ["old"]
    assert payloads[0]["pct"] == 100 and payloads[0]["source"] == "copy_timer"
    assert S.job_copy_time_exit(_ctx(tmp_db))["requested"] == 0, "one request per position"


def test_a_copy_is_scanned_at_once_even_while_the_main_batch_is_busy(tmp_db, risk, monkeypatch):
    """MEASURED live 2026-10-07: inside the batch, 160 of 188 leader tokens were never scanned
    and the rest a median 179 s late. The copy thread must not wait for the batch."""
    import threading

    from kaiba.execution import scanner as sc

    monkeypatch.setattr(sc, "_COPY_NEXT_AT", {})
    monkeypatch.setattr(sc, "_COPY_OFFERED", {})
    tmp_db.execute("INSERT INTO wallets (chain, address, source, cohort, first_seen_ms, last_seen_ms) "
                   "VALUES ('sol', ?, 'test', 'trusted_copy', 1, 1)", (LEADER,))
    swap(tmp_db, LEADER, TOKEN, "buy", 50, 1000, now_ms() - 1_000)
    tmp_db.commit()
    scanned: list[tuple[str, float]] = []
    t0 = time.monotonic()

    def fake_scan(chain, token, conn, *, config, source, extras):
        scanned.append((token, time.monotonic() - t0))
        return sc.ScanResult(chain=chain, token=token) if hasattr(sc, "ScanResult") else None

    def busy_batch(*a, **k):
        time.sleep(1.5)       # a slow main batch: the copy must not queue behind it
        return []

    monkeypatch.setattr(sc, "scan", fake_scan)
    monkeypatch.setattr(sc, "run_once", busy_batch)
    monkeypatch.setattr(sc.ScanStats, "record", lambda self, r: None)
    sc.run_loop(tmp_db, config=sc.ScanConfig(copy_flow_min_interval_s=0.2, idle_sleep_s=0.1),
                stop=threading.Event(), max_seconds=1.2)
    assert scanned and scanned[0][0] == TOKEN and scanned[0][1] < 1.0, scanned


def test_the_copy_feeder_query_seeks_by_wallet_never_walks_the_chain(tmp_db, risk, monkeypatch):
    """MEASURED 2026-10-08: planned onto idx_swaps_token it took 66 s a call on the box,
    every second, and pinned the WAL until the disk filled."""
    from kaiba.execution import scanner as sc

    monkeypatch.setattr(sc, "_COPY_NEXT_AT", {})
    monkeypatch.setattr(sc, "_COPY_OFFERED", {})
    statements: list[str] = []
    tmp_db.set_trace_callback(statements.append)
    try:
        sc._copy_flow_work(tmp_db, 4, config=sc.ScanConfig(copy_flow_min_interval_s=0.0))
    finally:
        tmp_db.set_trace_callback(None)
    tmp_db.execute("INSERT INTO wallets (chain, address, source, cohort, first_seen_ms, last_seen_ms) "
                   "VALUES ('sol', ?, 'test', 'trusted_copy', 1, 1)", (LEADER,))
    statements.clear()
    tmp_db.set_trace_callback(statements.append)
    try:
        sc._copy_flow_work(tmp_db, 4, config=sc.ScanConfig(copy_flow_min_interval_s=0.0))
    finally:
        tmp_db.set_trace_callback(None)
    reads = [x for x in statements if "FROM swaps" in x]
    assert reads, statements
    assert all("INDEXED BY idx_swaps_wallet" in x and "wallet = " in x for x in reads), reads
