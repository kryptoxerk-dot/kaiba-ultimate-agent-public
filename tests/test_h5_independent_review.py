"""Codex independent witnesses for H5 receipt clocks and recovery accounting.

Pure callbacks and fixture ledgers only; no provider or canonical database.
"""

import hashlib
import socket

import pytest

from kaiba.learning import h5_reentry as h


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("network forbidden"))


def evaluator(ledger, clock, *, fetch=lambda *a: [], history=lambda *a: [], price=None):
    return h.Evaluator(
        ledger,
        frozenset({"slow"}),
        frozenset(),
        fetch_new=fetch,
        fetch_history=history,
        read_price=price or (lambda *a: h.Read(False)),
        now=lambda: clock[0],
    )


def test_delay_begins_after_both_reads_and_price_time_follows_slow_rpc(tmp_path):
    clock = [h.T0_MS + 60_000]

    def fetch(*args):
        clock[0] += 40_000
        return [
            {
                "wallet": "slow",
                "token": "token",
                "side": "buy",
                "ts_ms": clock[0] - 1000,
                "tx": "fixture",
                "source": "fixture",
            }
        ]

    def history(*args):
        clock[0] += 50_000
        return []

    def price(*args):
        clock[0] += 20_000
        return h.Read(True, 1.0, 10_000.0, 10.0, "pumpswap", "fixture")

    ledger = h.Ledger(tmp_path / "ledger.jsonl")
    ev = evaluator(ledger, clock, fetch=fetch, history=history, price=price)
    ev.start()
    ev.poll()
    seen = clock[0]
    assert next(r for r in ledger.lines if r["type"] == "trigger")["seen_ms"] == seen
    ev.step()
    assert not any(r["type"] == "entry" for r in ledger.lines)
    clock[0] += h.ENTRY_DELAY_MS
    ev.step()
    entry = next(r for r in ledger.lines if r["type"] == "entry")
    assert entry["ts"] == clock[0] == seen + h.ENTRY_DELAY_MS + 20_000


def test_wal_defers_both_readers_before_real_connection_boundary(tmp_path, monkeypatch):
    db = tmp_path / "fixture.db"
    (tmp_path / "fixture.db-wal").write_bytes(b"x")
    calls = []

    class Empty:
        def execute(self, *args):
            return []

        def set_progress_handler(self, *args):
            pass

        def close(self):
            pass

    def connect(*args, **kwargs):
        calls.append(True)
        return Empty()

    monkeypatch.setattr(h.sqlite3, "connect", connect)
    monkeypatch.setattr(h, "WAL_LIMIT", 0)
    new, history = h.box_fetchers(db)
    for invoke in (lambda: new(["slow"], h.T0_MS), lambda: history("slow", "token", h.T0_MS, h.T0_MS + 1)):
        with pytest.raises(h.ReadDeferred):
            invoke()
    assert calls == []
    monkeypatch.setattr(h, "WAL_LIMIT", 2)
    assert history("slow", "token", h.T0_MS, h.T0_MS + 1) == []
    assert calls == [True]  # Real empty history remains distinct from deferred history.


def test_second_crash_before_recovery_publication_cannot_lose_intent(tmp_path):
    clock = [h.T0_MS + 60_000]
    path = tmp_path / "ledger.jsonl"
    initial = h.Ledger(path)
    evaluator(initial, clock).start()
    first_heartbeat = initial.lines[0]
    initial.append(
        {
            "type": "trigger",
            "id": "lost",
            "arm": "H5",
            "token": "token",
            "trigger_ts": clock[0],
            "seen_ms": clock[0],
            "status": "pending",
        }
    )
    prefix = path.read_bytes()
    fragment = b'{"id": "lost", "type": "entry", "price_usd":'
    with path.open("ab") as stream:
        stream.write(fragment)
    first = h.Ledger(path)
    assert first.recovered
    del first  # Simulate a second death before Evaluator.start publishes recovery.
    second = h.Ledger(path)
    assert second.recovered and path.read_bytes() == prefix
    clock[0] += 1_000
    evaluator(second, clock).start()
    recovery = next(r for r in second.lines if r["type"] == "recovery")
    assert recovery["torn_sha256"] == hashlib.sha256(fragment).hexdigest()
    assert any(r["type"] == "recovery_unknown" and r["id"] == "lost" for r in second.lines)
    assert next(r for r in second.lines if r["type"] == "heartbeat_first") == first_heartbeat
    assert (tmp_path / recovery["sidecar"]).read_bytes() == fragment


def test_readonly_report_cannot_truncate_an_in_progress_writer(tmp_path):
    path = tmp_path / "ledger.jsonl"
    prefix = b'{"type":"heartbeat_first","id":"first","ts":1}\n'
    body = prefix + b'{"id":"inflight","type":'
    path.write_bytes(body)
    names = set(tmp_path.iterdir())
    ledger = h.Ledger(path, readonly=True)
    assert ledger.torn_tail_present is True
    assert path.read_bytes() == body and set(tmp_path.iterdir()) == names


def outcomes(known, unknown):
    rows = [{"type": "heartbeat_first", "id": "first", "ts": h.T0_MS}]
    for i in range(known + unknown):
        tid = "entry" + str(i)
        entry = {
            "type": "entry",
            "id": tid,
            "arm": "H5",
            "token": "token" + str(i),
            "trigger_ts": h.T0_MS + i,
            "ts": h.T0_MS + 30_000 + i,
            "delay_ms": 30_000,
            "price_usd": 1.0,
            "liquidity_usd": 100_000.0,
            "sol_usd": 100.0,
            "venue": "pumpswap",
        }
        rows.append(
            {
                "type": "trigger",
                "id": tid,
                "arm": "H5",
                "token": entry["token"],
                "trigger_ts": entry["trigger_ts"],
                "status": "pending",
            }
        )
        rows.append(entry)
        rows.append(
            {**entry, "type": "exit", "price_usd": 1.5, "reason": "take_profit"}
            if i < known
            else {"type": "recovery_unknown", "id": tid, "ts": h.T0_MS + 60_000 + i}
        )
    return rows


def test_lost_exposures_cannot_turn_a_negative_worst_case_into_pass():
    positive = h.build_report(outcomes(40, 0), h.T0_MS + h.DAY)["arms"]["H5"]
    assert positive["verdict"]["verdict"] == "PASS"
    actual = h.build_report(outcomes(40, 40), h.T0_MS + h.DAY)["arms"]["H5"]
    assert actual["entered"] == 80 and actual["settled"] == 40
    assert actual["verdict"]["n_recovery_unknown"] == 40
    assert actual["verdict"]["worst_case"] == pytest.approx((40 * 0.40495526 - 40) / 80)
    assert actual["verdict"]["verdict"] != "PASS"
