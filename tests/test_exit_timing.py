"""Exit timing: every exit attempt says where its time went (journal #4997).

Robinhood stops closed a median 83 s after the tape's first -30% print against a 12 s poll,
and the operator asked for each stop to be timed end to end before any stop parameter
changes. These pin the ``exit_timing`` event the watchdog writes and the analysis that reads
it back (``kaiba.learning.exit_latency``), including the reconstruction for older exits.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from kaiba.core import events as ev
from kaiba.core.db import fetch_all
from kaiba.core.schemas import Chain, EventKind, EvidenceBasis, Lane, LaneMode, OrderState, now_ms
from kaiba.execution import watchdog as wd
from kaiba.execution.protection import ProtectionConfig
from kaiba.learning import exit_latency as xl
from tests.test_watchdog import RecordingSubmitter, events_named, make_position
from tests.test_watchdog_quote_provenance import offline_book  # noqa: F401 - pytest fixture


class Clocked:
    """A price source whose observation time the test sets."""

    name = "fake"

    def __init__(self) -> None:
        self.price = "1.0"
        self.observed = now_ms()

    def quote(self, chain, token):
        return wd.PriceQuote(price_usd=Decimal(self.price), liquidity_usd=Decimal("1000000"),
                             basis=EvidenceBasis.PROVIDER_REPORTED, source="fake", chain=chain,
                             token=token, observed_ms=self.observed)


def cfg() -> ProtectionConfig:
    return ProtectionConfig(use_provider_orders=False, stop_loss_bps=3000, poll_interval_s=12,
                            tp_ladder=[(Decimal("2.0"), Decimal(50))], trailing=[(Decimal("100.0"), 1000)],
                            breakeven_after_tp1=False, stale_no_volume_exit_s=0, moonbag_retain_pct=0)


@pytest.fixture
def live(tmp_db):
    make_position(tmp_db, entry="1.0", mode=LaneMode.LIVE, lane=Lane.SM_TRENCHES)
    return tmp_db


def run_to_stop(conn, outcome: wd.ExitOutcome):
    source = Clocked()
    dog = wd.Watchdog(conn, price_source=source, submitter=RecordingSubmitter(outcome), cfg_provider=cfg)
    t0 = now_ms()
    source.observed = t0 - 20_000
    dog.tick()
    source.observed = t0 - 10_000
    dog.tick()                         # same price again: the feed is "frozen"
    source.observed = t0 - 1_000
    source.price = "0.6"               # the jump that fires the stop
    report = dog.tick()
    return report, (t0 - 20_000, t0 - 10_000, t0 - 1_000)


def timing_events(conn) -> list[dict]:
    rows = fetch_all(conn, "SELECT payload, dedupe_key FROM events WHERE dedupe_key >= ? AND dedupe_key < ?",
                     (xl.DEDUPE_LO, xl.DEDUPE_HI))
    return [json.loads(r["payload"]) for r in rows]


def test_an_exit_records_every_stamp_it_owns(live):
    outcome = wd.ExitOutcome(True, OrderState.SUBMITTED, "ord_fake", "sent", timing={"executor_ms": 7})
    report, (first, second, third) = run_to_stop(live, outcome)
    assert report.exits == 1

    [timing] = timing_events(live)
    assert timing["event"] == "exit_timing" and timing["service"] == wd.SERVICE
    assert timing["order_id"] == "ord_fake" and timing["outcome"] == "submitted"
    assert timing["reason"] == "stop_loss" and timing["mode"] == "live" and timing["attempt"] == 1
    mark, ms, seg = timing["mark"], timing["ms"], timing["segments_ms"]
    assert mark["observed_ms"] == third and mark["previous_observed_ms"] == second
    # The price before the jump first appeared at the FIRST tick, not the second.
    assert mark["price_since_ms"] == third and mark["prev_price_since_ms"] == first
    assert seg["prev_price_held"] == third - first
    assert seg["previous_mark_to_mark"] == third - second
    assert ms["tick_started"] <= ms["decided"] <= ms["submit_started"] <= ms["submit_returned"]
    assert seg["mark_age_at_decision"] == ms["decided"] - third >= 0
    assert seg["decision_to_submit"] == ms["submit_started"] - ms["decided"]
    assert seg["submit_call"] == ms["submit_returned"] - ms["submit_started"]
    assert timing["submitter_ms"] == {"executor_ms": 7}
    assert timing["poll_interval_ms"] == 12_000


@pytest.mark.parametrize("outcome,label", [
    (wd.ExitOutcome(False, OrderState.FAILED, "ord_f", "venue said no"), "failed"),
    (wd.ExitOutcome(False, OrderState.UNKNOWN, "ord_u", "timed out after send"), "ambiguous"),
])
def test_failed_and_ambiguous_attempts_are_timed_too(live, outcome, label):
    run_to_stop(live, outcome)
    [timing] = timing_events(live)
    assert timing["outcome"] == label and timing["order_id"] == outcome.order_id


def test_timing_never_breaks_an_exit(live):
    """A payload the helper cannot build is a missing diagnostic, never a failed exit."""
    unbuildable = wd.ExitOutcome(True, OrderState.SUBMITTED, "ord_fake", "sent", timing=5)  # not a dict
    report, _ = run_to_stop(live, unbuildable)
    assert report.exits == 1 and timing_events(live) == []
    assert events_named(live, "exit_submitted")


def test_a_held_price_remembers_when_it_first_appeared(live):
    source = Clocked()
    dog = wd.Watchdog(live, price_source=source, submitter=RecordingSubmitter(), cfg_provider=cfg)
    t0 = now_ms()
    source.observed = t0 - 20_000
    dog.tick()
    source.observed = t0 - 5_000
    dog.tick()
    memory = json.loads(fetch_all(live, "SELECT value FROM kv WHERE key LIKE 'watchdog.quote:%'")[0]["value"])
    assert memory["price_since_ms"] == t0 - 20_000


def test_the_live_submitter_times_its_own_steps(tmp_db, monkeypatch, offline_book):  # noqa: F811
    """Real DefaultExitSubmitter, real executor, transport stubbed at gmgn-cli."""
    from tests.test_watchdog_quote_provenance import BAD, dex_reply, dog

    dex_reply(monkeypatch)
    assert dog(tmp_db).tick().exits == 0
    dex_reply(monkeypatch, price=BAD)
    assert dog(tmp_db).tick().exits == 1 and len(offline_book) == 1
    [timing] = timing_events(tmp_db)
    assert {"wallet_read_ms", "min_out_ms", "executor_ms", "total_ms"} <= set(timing["submitter_ms"])
    assert timing["order_id"] and timing["mark"]["settlement"] == wd.SETTLE_FOREIGN_POOL


# --------------------------------------------------------------------------------------
# the analysis
# --------------------------------------------------------------------------------------


def emit_attempt(conn, *, position_id="pos_9", chain=Chain.ROBINHOOD, outcome, order_id, decided,
                 started, returned, observed, reason="stop_loss", attempt=1, submitter=None,
                 mode="live"):
    payload = {
        "service": "watchdog", "event": "exit_timing", "version": 1, "position_id": position_id,
        "order_id": order_id, "outcome": outcome, "mode": mode, "reason": reason, "attempt": attempt,
        "mark": {"observed_ms": observed},
        "ms": {"tick_started": decided - 50, "decided": decided, "submit_started": started,
               "submit_returned": returned},
        "segments_ms": {"mark_age_at_decision": decided - observed, "tick_to_decision": 50,
                        "decision_to_submit": started - decided, "submit_call": returned - started},
        "submitter_ms": submitter or {},
    }
    ev.emit(EventKind.SYSTEM, payload, chain=chain, subject="0xtoken", conn=conn,
            dedupe_key=f"exit_timing:{position_id}:{started}:{attempt}")


def insert_order(conn, order_id, *, state="filled", created, updated, chain="robinhood"):
    conn.execute(
        "INSERT INTO orders (order_id, chain, token, side, lane, mode, input_token, output_token, amount_in, "
        "min_out, slippage_bps, state, provider, created_ms, updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (order_id, chain, "0xtoken", "sell", "sm-trenches", "live", "0xtoken", "0x0", "1", "1", 100, state,
         "gmgn", created, updated),
    )


def test_report_splits_an_exit_into_its_segments(tmp_db):
    # A failed attempt, a 4 s backoff, then a send that reconcile saw filled 14.1 s later.
    emit_attempt(tmp_db, outcome="failed", order_id="ord_a", decided=1_000, started=1_100, returned=1_200,
                 observed=900)
    emit_attempt(tmp_db, outcome="submitted", order_id="ord_b", decided=5_000, started=5_100, returned=5_900,
                 observed=4_950, attempt=2, submitter={"wallet_read_ms": 300, "min_out_ms": 200,
                                                        "executor_ms": 290})
    insert_order(tmp_db, "ord_b", created=5_600, updated=20_000)
    # Noise that must not count: a shadow exit and an unrelated system event.
    emit_attempt(tmp_db, position_id="pos_s", outcome="submitted", order_id="ord_s", decided=1, started=2,
                 returned=3, observed=0, mode="shadow")
    ev.emit(EventKind.SYSTEM, {"service": "watchdog", "event": "heartbeat"}, conn=tmp_db)

    got = xl.report(tmp_db)
    assert got["attempts"] == 2 and got["exits"] == 1 and got["unsent_exits"] == 0
    rh = got["by_chain"]["robinhood"]
    assert rh["exits"] == 1
    expect = {
        "mark_age_at_decision": 100, "tick_to_decision": 50, "retry": 4_100,
        "decision_to_submit": 100, "submit_call": 800, "wallet_read": 300, "min_out": 200,
        "executor": 290, "confirm": 14_100, "decision_to_fill": 19_000, "mark_to_fill": 19_100,
    }
    for name, value in expect.items():
        assert rh[name]["median_ms"] == value, name
    assert xl.report(tmp_db, include_shadow=True)["exits"] == 2
    assert xl.report(tmp_db, chains=["sol"])["exits"] == 0
    assert xl.report(tmp_db, reasons=["trailing_stop"])["exits"] == 0


def test_an_unfilled_order_has_no_confirmation_time(tmp_db):
    emit_attempt(tmp_db, outcome="submitted", order_id="ord_c", decided=1_000, started=1_100, returned=1_500,
                 observed=990)
    insert_order(tmp_db, "ord_c", state="submitted", created=1_200, updated=9_000)
    rh = xl.report(tmp_db)["by_chain"]["robinhood"]
    assert rh["confirm"]["n"] == 0 and rh["decision_to_fill"]["n"] == 0
    assert rh["submit_call"]["median_ms"] == 400


def test_an_exit_that_never_sent_is_counted(tmp_db):
    emit_attempt(tmp_db, outcome="failed", order_id=None, decided=1_000, started=1_100, returned=1_200,
                 observed=900)
    got = xl.report(tmp_db)
    assert got["exits"] == 1 and got["unsent_exits"] == 1
    assert got["by_chain"]["robinhood"]["retry"]["n"] == 0


def test_the_watchdog_output_round_trips_through_the_report(live):
    outcome = wd.ExitOutcome(True, OrderState.SUBMITTED, "ord_rt", "sent", timing={"executor_ms": 9})
    run_to_stop(live, outcome)
    [timing] = timing_events(live)
    insert_order(live, "ord_rt", created=timing["ms"]["submit_started"],
                 updated=timing["ms"]["submit_returned"] + 7_000, chain="sol")
    sol = xl.report(live)["by_chain"]["sol"]
    assert sol["confirm"]["median_ms"] == 7_000
    assert sol["executor"]["median_ms"] == 9


@pytest.mark.parametrize("values,q,expected", [
    ([], 0.5, None), ([5], 0.9, 5.0), ([1, 2, 3, 4], 0.5, 2.5), ([0, 10], 0.9, 9.0),
])
def test_percentile(values, q, expected):
    assert xl.percentile(values, q) == expected


def test_legacy_reconstruction_of_a_stop(tmp_db):
    """Tape at 1000 s, our mark 36 s later, decision, order, send, fill, close."""
    base = now_ms() - 3_600_000
    pid, token = "pos_l", "0xlegacy"
    tmp_db.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, qty, qty_total, "
        "cost_native, proceeds_native, realized_native, entry_price_usd, exit_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, "robinhood", token, "sm-trenches", "live", base, base + 1_090_000, "0", "1", "1", "1", "0",
         "1.0", "stop_loss"),
    )

    def swap(ts, price, tx):
        tmp_db.execute("INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, price_usd, source) "
                       "VALUES (?,?,?,?,?,?,?,?)", ("robinhood", tx, ts, "w", token, "sell", str(price), "t"))

    swap(base + 900_000, 0.9, "tx1")
    swap(base + 1_000_000, 0.68, "tx2")          # the first -30% print
    swap(base + 1_085_000, 0.5, "our_sell")       # ours: never the market
    for ts, ret in ((base + 1_024_000, -10.0), (base + 1_036_000, -33.0)):
        tmp_db.execute("INSERT INTO position_marks (position_id, ts_ms, price_usd, return_pct) VALUES (?,?,?,?)",
                       (pid, ts, "0.67", ret))
    insert_order(tmp_db, "ord_l", created=base + 1_040_000, updated=base + 1_060_000)
    tmp_db.execute("UPDATE orders SET token=?, tx_hash='our_sell' WHERE order_id='ord_l'", (token,))

    def system(ts_offset, kind, payload):
        tmp_db.execute("INSERT INTO events (ts_ms, kind, level, chain, subject, payload) VALUES (?,?,?,?,?,?)",
                       (base + ts_offset, kind, "info", "robinhood", token, json.dumps(payload)))

    system(1_010_000, "system", {"event": "protection_blind", "position_id": pid})
    system(1_037_000, "system", {"event": "quote_decision", "position_id": pid, "reason": "stop_loss"})
    system(1_041_000, "protection.triggered", {"event": "exit_submitted", "position_id": pid,
                                               "reason": "stop_loss", "order_id": "ord_l"})

    # A second stop where the market never printed through -30%: only OUR sell did.
    tmp_db.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, qty, qty_total, "
        "cost_native, proceeds_native, realized_native, entry_price_usd, exit_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("pos_m", "robinhood", "0xmarket", "sm-trenches", "live", base, base + 500_000, "0", "1", "1", "1", "0",
         "1.0", "stop_loss"),
    )
    tmp_db.execute("INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, price_usd, source) "
                   "VALUES ('robinhood','our_m',?, 'w','0xmarket','sell','0.5','t')", (base + 400_000,))
    insert_order(tmp_db, "ord_m", created=base + 399_000, updated=base + 401_000)
    tmp_db.execute("UPDATE orders SET token='0xmarket', tx_hash='our_m' WHERE order_id='ord_m'")
    by_id = {t["position_id"]: t for t in xl.legacy_stop_timelines(tmp_db, chains=["robinhood"])}
    assert by_id["pos_m"]["t_tape"] is None, "our own sell is not the market crossing the line"
    line = by_id[pid]
    assert line["t_tape"] == base + 1_000_000
    assert line["t_mark"] == base + 1_036_000 and line["t_prev_mark"] == base + 1_024_000
    assert line["t_decide"] == base + 1_037_000 and line["t_order"] == base + 1_040_000
    assert line["t_submitted"] == base + 1_041_000 and line["t_fill"] == base + 1_060_000
    assert line["blind_events_after_tape"] == 1
    summary = xl.summarize_legacy(list(by_id.values()))["robinhood"]
    assert summary["stop_exits"] == 2
    assert summary["tape_crossed_before_close"] == 1
    assert summary["tape_to_mark"]["median_ms"] == 36_000
    assert summary["tape_to_close"]["median_ms"] == 90_000
    assert summary["submitted_to_fill"]["median_ms"] == 19_000
