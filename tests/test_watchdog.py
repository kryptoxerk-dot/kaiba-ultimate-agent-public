"""The exit watchdog: blindness, deduplication, the ambiguous-send latch and the loop.

``protection.evaluate`` is tested elsewhere and is pure. Everything here is about the
service: what happens when there is no price, when two people ask for the same exit, when
the kill switch is on, when a send comes back ambiguous, and when one position explodes.
"""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest

from kaiba.core import events as ev
from kaiba.core.config import load_risk, save_risk
from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import (
    SOL_NATIVE_MINT,
    Chain,
    EventKind,
    EvidenceBasis,
    Lane,
    LaneMode,
    OrderState,
    now_ms,
)
from kaiba.execution import watchdog as wd

TOKEN = "Bnd5oBSWpPpXoaWTckgXTVx9TCKkX5GyVqxcrwBzUYsB"
TOKEN_B = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"


# --------------------------------------------------------------------------------------
# fixtures and doubles
# --------------------------------------------------------------------------------------


def make_position(
    conn,
    *,
    position_id: str = "pos_1",
    token: str = TOKEN,
    entry: str | None = "1.0",
    qty: int = 1_000_000,
    mode: LaneMode = LaneMode.SHADOW,
    lane: Lane = Lane.CONFLUENCE_5,
    chain: Chain = Chain.SOL,
) -> str:
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty, qty_total, "
        "cost_native, proceeds_native, realized_native, entry_price_usd, peak_price_usd) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            position_id, chain.value, token, lane.value, mode.value, now_ms(),
            str(qty), str(qty), "5000000", "0", "0", entry, entry,
        ),
    )
    return position_id


class FakeSource:
    """A price source under the test's control, including the ability to be absent."""

    name = "fake"

    def __init__(
        self,
        prices: dict[str, str | None] | None = None,
        *,
        liquidity: str | None = "1000000",
        raises: set[str] | None = None,
        basis: EvidenceBasis = EvidenceBasis.PROVIDER_REPORTED,
    ) -> None:
        self.prices = prices or {}
        self.liquidity = liquidity
        self.raises = raises or set()
        self.basis = basis
        self.calls: list[str] = []

    def quote(self, chain: Chain, token: str) -> wd.PriceQuote:
        self.calls.append(token)
        if token in self.raises:
            raise RuntimeError("provider on fire")
        price = self.prices.get(token)
        if price is None:
            return wd.PriceQuote.unavailable("fixture has no price", source=self.name)
        return wd.PriceQuote(
            price_usd=Decimal(price),
            liquidity_usd=Decimal(self.liquidity) if self.liquidity else None,
            basis=self.basis,
            source=self.name,
        )


class RecordingSubmitter:
    """Stands in for the executor / paper broker so a test can dictate the outcome."""

    def __init__(self, outcome: wd.ExitOutcome | None = None, *, explode_on: set[str] | None = None):
        self.outcome = outcome or wd.ExitOutcome(True, OrderState.FILLED, "ord_fake", "filled")
        self.explode_on = explode_on or set()
        self.calls: list[tuple[str, Decimal, str]] = []

    def submit_exit(self, position, pct, *, quote, reason):  # noqa: ANN001 - test double
        if position.token in self.explode_on:
            raise RuntimeError("submitter blew up")
        self.calls.append((position.position_id, pct, reason))
        return self.outcome


def events_named(conn, name: str) -> list[dict]:
    return [
        e.payload
        for e in ev.recent(limit=500, conn=conn)
        if isinstance(e.payload, dict) and e.payload.get("event") == name
    ]


def sell_orders(conn) -> list[dict]:
    return fetch_all(conn, "SELECT * FROM orders WHERE side='sell'")


@pytest.fixture
def risk_file(tmp_path, monkeypatch):
    """An isolated risk.yaml so a test can flip the kill switch without touching config/."""
    cfg = load_risk()
    path = tmp_path / "risk.yaml"
    save_risk(cfg, path)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))
    return path


def set_flag(path, **flags) -> None:
    import yaml

    raw = yaml.safe_load(path.read_text())
    raw.update(flags)
    path.write_text(yaml.safe_dump(raw))


# --------------------------------------------------------------------------------------
# blindness
# --------------------------------------------------------------------------------------


def test_blind_position_never_exits_and_says_so(tmp_db, risk_file):
    """The single most dangerous outcome: reporting healthy while unable to see a price."""
    make_position(tmp_db)
    submitter = RecordingSubmitter()
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource(), submitter=submitter)

    report = dog.tick()

    assert report.checked == 1
    assert report.blind == 1
    assert report.exits == 0
    assert submitter.calls == []
    assert sell_orders(tmp_db) == []
    blind = events_named(tmp_db, "protection_blind")
    assert len(blind) == 1
    assert blind[0]["basis"] == EvidenceBasis.UNAVAILABLE.value
    assert blind[0]["stops_evaluated"] is False
    assert blind[0]["position_id"] == "pos_1"
    # and it is an error, not a debug line nobody reads
    levels = [e.level for e in ev.recent(limit=50, conn=tmp_db)
              if isinstance(e.payload, dict) and e.payload.get("event") == "protection_blind"]
    assert levels == ["error"]


def test_blind_heartbeat_carries_the_blind_count(tmp_db, risk_file):
    make_position(tmp_db)
    make_position(tmp_db, position_id="pos_2", token=TOKEN_B)
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource(), submitter=RecordingSubmitter())

    dog.tick()

    beat = events_named(tmp_db, "heartbeat")[0]
    assert beat["checked"] == 2 and beat["blind"] == 2 and beat["exits"] == 0
    assert "NOT evaluating" in beat["blind_note"]


def test_blind_warning_is_throttled_but_the_count_is_not(tmp_db, risk_file):
    make_position(tmp_db)
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource(), submitter=RecordingSubmitter())

    first = dog.tick()
    second = dog.tick()

    assert first.blind == second.blind == 1
    assert len(events_named(tmp_db, "protection_blind")) == 1
    assert len(events_named(tmp_db, "heartbeat")) == 2


def test_a_position_without_an_entry_price_is_blind_not_assumed_flat(tmp_db, risk_file):
    make_position(tmp_db, entry=None)
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "0.5"}), submitter=RecordingSubmitter())

    report = dog.tick()

    assert report.blind == 1 and report.exits == 0
    assert events_named(tmp_db, "protection_blind")[0]["reason"] == "entry_price_unavailable"


def test_a_provider_that_raises_is_blindness_not_a_crash(tmp_db, risk_file):
    make_position(tmp_db)
    source = FakeSource({TOKEN: "0.5"}, raises={TOKEN})
    dog = wd.Watchdog(tmp_db, price_source=source, submitter=RecordingSubmitter())

    report = dog.tick()

    assert report.blind == 1 and report.errors == 0
    assert "RuntimeError" in events_named(tmp_db, "protection_blind")[0]["reason"]


def test_protection_restored_is_announced_when_a_price_comes_back(tmp_db, risk_file):
    make_position(tmp_db)
    source = FakeSource({TOKEN: None})
    dog = wd.Watchdog(tmp_db, price_source=source, submitter=RecordingSubmitter())
    dog.tick()
    source.prices[TOKEN] = "1.1"

    dog.tick()

    assert len(events_named(tmp_db, "protection_restored")) == 1


# --------------------------------------------------------------------------------------
# stops
# --------------------------------------------------------------------------------------


def test_stop_breach_exits_once_and_only_once_on_the_paper_broker(tmp_db, risk_file):
    """Entry 1.0, default stop -3000 bps, price 0.6: one sell, and no second one."""
    make_position(tmp_db)
    source = FakeSource({TOKEN: "0.6"})
    dog = wd.Watchdog(tmp_db, price_source=source)  # real DefaultExitSubmitter -> paper

    first = dog.tick()
    second = dog.tick()

    assert first.exits == 1
    assert second.checked == 0 and second.exits == 0  # the position is closed
    assert len(sell_orders(tmp_db)) == 1
    position = fetch_one(tmp_db, "SELECT * FROM positions WHERE position_id='pos_1'")
    assert position["closed_ms"] is not None
    assert int(position["qty"]) == 0
    triggered = [
        e for e in ev.recent(limit=200, conn=tmp_db) if e.kind == EventKind.PROTECTION_TRIGGERED.value
    ]
    assert triggered and triggered[0].payload["reason"] == "stop_loss"
    assert triggered[0].payload["source"] == "watchdog"


def test_a_filled_full_exit_is_not_repeated_when_the_position_row_stays_open(tmp_db, risk_file):
    """Live fills do not write back to `positions` yet. The latch is what stops a double sell."""
    make_position(tmp_db, mode=LaneMode.CANARY)
    submitter = RecordingSubmitter()
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "0.6"}), submitter=submitter)

    dog.tick()
    dog.tick()

    assert len(submitter.calls) == 1
    assert events_named(tmp_db, "exit_suppressed")


def test_hold_above_the_stop_does_not_sell(tmp_db, risk_file):
    make_position(tmp_db)
    submitter = RecordingSubmitter()
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "1.5"}), submitter=submitter)

    report = dog.tick()

    assert report.exits == 0 and submitter.calls == []


def test_the_ratchet_survives_a_restart(tmp_db, risk_file):
    """Tick once at 2.5x (tp1 + ratchet), throw the object away, and check the stop held."""
    make_position(tmp_db)
    submitter = RecordingSubmitter()
    first_dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "2.5"}), submitter=submitter)

    first_dog.tick()

    row = fetch_one(tmp_db, "SELECT * FROM watchdog_state WHERE position_id='pos_1'")
    assert row["tp_done_json"] == '["tp1"]'
    assert Decimal(row["stop_price_usd"]) == Decimal("1.75")  # peak 2.5 less the 3000 bps tier
    mirrored = fetch_one(tmp_db, "SELECT * FROM positions WHERE position_id='pos_1'")
    assert Decimal(mirrored["stop_price_usd"]) == Decimal("1.75")
    assert submitter.calls[0][1] == Decimal(50)

    # restart: a brand new Watchdog, nothing carried over in memory
    restarted = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "1.6"}), submitter=submitter)
    report = restarted.tick()

    assert report.exits == 1
    # 80, not 100: since 2026-09-23 a profitable trailing stop keeps a moon bag
    # (`ProtectionConfig.moonbag_retain_pct`, default 20). The position is at 1.6 against a
    # 1.0 entry, so this is a winner being trimmed, not a loss being cut. What this test
    # pins is that the RATCHET survived the restart and the stop fired from the rehydrated
    # state; how much it sells is the ladder's business.
    assert submitter.calls[-1][1] == Decimal(80)
    assert "trailing_stop" in submitter.calls[-1][2]


def test_a_restart_does_not_re_arm_a_rung_that_already_fired(tmp_db, risk_file):
    make_position(tmp_db)
    submitter = RecordingSubmitter()
    wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "2.5"}), submitter=submitter).tick()
    wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "2.6"}), submitter=submitter).tick()

    assert len(submitter.calls) == 1


# --------------------------------------------------------------------------------------
# requests from the three sources
# --------------------------------------------------------------------------------------


def test_two_exit_requests_for_one_position_produce_one_exit(tmp_db, risk_file):
    """kaiba_request_exit and the dashboard's close button, same position, same tick."""
    make_position(tmp_db)
    ev.emit(
        EventKind.PROTECTION_TRIGGERED,
        {"position_id": "pos_1", "pct": 100, "reason": "agent wants out", "source": "agent"},
        conn=tmp_db,
    )
    ev.emit(
        EventKind.SYSTEM,
        {"intent": "close_position", "position_id": "pos_1", "requested_by": "operator",
         "executed": False, "note": "intent only"},
        conn=tmp_db,
    )
    submitter = RecordingSubmitter()
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "1.5"}), submitter=submitter)

    report = dog.tick()

    assert report.exits == 1
    assert len(submitter.calls) == 1
    assert submitter.calls[0][1] == Decimal(100)


def test_a_request_is_not_replayed_on_the_next_tick(tmp_db, risk_file):
    make_position(tmp_db)
    ev.emit(
        EventKind.PROTECTION_TRIGGERED,
        {"position_id": "pos_1", "pct": 50, "reason": "trim please", "source": "agent"},
        conn=tmp_db,
    )
    submitter = RecordingSubmitter()
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "1.5"}), submitter=submitter)

    dog.tick()
    dog.tick()

    assert len(submitter.calls) == 1
    assert submitter.calls[0][1] == Decimal(50)
    assert wd.read_cursor(tmp_db) > 0


def test_the_watchdog_does_not_consume_its_own_exit_events(tmp_db, risk_file):
    make_position(tmp_db, mode=LaneMode.CANARY)
    submitter = RecordingSubmitter()
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "0.6"}), submitter=submitter)

    dog.tick()  # emits PROTECTION_TRIGGERED with source=watchdog
    dog.tick()  # must not read that back as a fresh request

    assert len(submitter.calls) == 1


def test_a_request_for_a_closed_position_is_reported_not_acted_on(tmp_db, risk_file):
    ev.emit(
        EventKind.PROTECTION_TRIGGERED,
        {"position_id": "pos_gone", "pct": 100, "reason": "x", "source": "agent"},
        conn=tmp_db,
    )
    submitter = RecordingSubmitter()

    wd.Watchdog(tmp_db, price_source=FakeSource(), submitter=submitter).tick()

    assert submitter.calls == []
    assert events_named(tmp_db, "exit_request_orphaned")[0]["position_id"] == "pos_gone"


def test_a_request_on_a_blind_position_is_deferred_not_dropped(tmp_db, risk_file):
    make_position(tmp_db)
    ev.emit(
        EventKind.PROTECTION_TRIGGERED,
        {"position_id": "pos_1", "pct": 100, "reason": "get me out", "source": "agent"},
        conn=tmp_db,
    )
    source = FakeSource({TOKEN: None})
    submitter = RecordingSubmitter()
    dog = wd.Watchdog(tmp_db, price_source=source, submitter=submitter)

    blind_tick = dog.tick()
    assert blind_tick.exits == 0 and blind_tick.deferred == 1
    assert submitter.calls == []
    assert events_named(tmp_db, "protection_blind")[0]["deferred_exit_pct"] == "100"

    source.prices[TOKEN] = "1.5"
    dog.tick()

    assert len(submitter.calls) == 1  # honoured as soon as it can be priced


def test_set_protection_tightens_the_stop(tmp_db, risk_file):
    make_position(tmp_db)
    ev.emit(
        EventKind.PROTECTION_SET,
        {"position_id": "pos_1", "stop_loss_bps": 1000, "source": "agent"},
        conn=tmp_db,
    )
    submitter = RecordingSubmitter()
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "0.85"}), submitter=submitter)

    report = dog.tick()

    # 0.85 is above the shipped -30% stop and below the requested -10% one.
    assert report.exits == 1
    assert events_named(tmp_db, "protection_updated")[0]["applied"] == {"stop_loss_bps": 1000}


def test_a_widening_stop_request_is_reported_as_ignored(tmp_db, risk_file):
    make_position(tmp_db)
    tmp_db.execute("UPDATE positions SET stop_price_usd='0.9' WHERE position_id='pos_1'")
    ev.emit(
        EventKind.PROTECTION_SET,
        {"position_id": "pos_1", "stop_loss_bps": 4000, "source": "agent"},
        conn=tmp_db,
    )
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "1.5"}), submitter=RecordingSubmitter())

    dog.tick()

    assert "does not lower a stop" in events_named(tmp_db, "protection_updated")[0]["ignored"]


# --------------------------------------------------------------------------------------
# exits are never gated
# --------------------------------------------------------------------------------------


def test_the_kill_switch_does_not_block_an_exit(tmp_db, risk_file):
    make_position(tmp_db)
    set_flag(risk_file, kill_switch=True, entries_paused=True, reduce_only=True)
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "0.6"}))

    report = dog.tick()

    assert report.exits == 1
    assert fetch_one(tmp_db, "SELECT closed_ms FROM positions WHERE position_id='pos_1'")["closed_ms"]
    triggered = [
        e for e in ev.recent(limit=200, conn=tmp_db) if e.kind == EventKind.PROTECTION_TRIGGERED.value
    ]
    findings = triggered[0].payload["risk_findings"]
    assert "kill_switch_active" in findings and "reduce_only_active" in findings


def test_the_daily_loss_stop_does_not_block_an_exit(tmp_db, risk_file):
    make_position(tmp_db)
    tmp_db.execute(
        "INSERT INTO risk_state (day_key, realized_native_json, entries, halted, halt_reason, updated_ms) "
        "VALUES (?,?,?,?,?,?)",
        ("1970-01-01", '{"sol": -999999999}', 0, 1, "daily_loss_stop", now_ms()),
    )
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "0.6"}))

    assert dog.tick().exits == 1


# --------------------------------------------------------------------------------------
# the ambiguous-send rule
# --------------------------------------------------------------------------------------


def test_an_unknown_submit_is_not_resubmitted_on_the_next_tick(tmp_db, risk_file):
    make_position(tmp_db, mode=LaneMode.CANARY)
    tmp_db.execute(
        "INSERT INTO orders (order_id, chain, token, side, lane, mode, input_token, output_token, "
        "amount_in, min_out, slippage_bps, state, provider, created_ms, updated_ms) "
        "VALUES ('ord_amb',?,?,'sell',?,?,?,?,'1','0',300,'unknown','gmgn',?,?)",
        (Chain.SOL.value, TOKEN, Lane.CONFLUENCE_5.value, LaneMode.CANARY.value, TOKEN,
         SOL_NATIVE_MINT, now_ms(), now_ms()),
    )
    submitter = RecordingSubmitter(wd.ExitOutcome(False, OrderState.UNKNOWN, "ord_amb", "timed out"))
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "0.6"}), submitter=submitter)

    dog.tick()
    dog.tick()
    dog.tick()

    assert len(submitter.calls) == 1
    assert events_named(tmp_db, "exit_ambiguous")
    assert len(events_named(tmp_db, "exit_not_resubmitted")) == 2


def test_an_unknown_submit_with_no_order_row_still_latches(tmp_db, risk_file):
    make_position(tmp_db, mode=LaneMode.CANARY)
    submitter = RecordingSubmitter(wd.ExitOutcome(False, OrderState.UNKNOWN, "ord_ghost", "timeout"))
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "0.6"}), submitter=submitter)

    dog.tick()
    dog.tick()

    assert len(submitter.calls) == 1


def test_reconciliation_releases_the_latch(tmp_db, risk_file):
    """Only the order resolving lets the watchdog act on that position again."""
    make_position(tmp_db, mode=LaneMode.CANARY)
    tmp_db.execute(
        "INSERT INTO orders (order_id, chain, token, side, lane, mode, input_token, output_token, "
        "amount_in, min_out, slippage_bps, state, provider, created_ms, updated_ms) "
        "VALUES ('ord_amb',?,?,'sell',?,?,?,?,'1','0',300,'unknown','gmgn',?,?)",
        (Chain.SOL.value, TOKEN, Lane.CONFLUENCE_5.value, LaneMode.CANARY.value, TOKEN,
         SOL_NATIVE_MINT, now_ms(), now_ms()),
    )
    submitter = RecordingSubmitter(wd.ExitOutcome(False, OrderState.UNKNOWN, "ord_amb", "timed out"))
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "0.6"}), submitter=submitter)
    dog.tick()

    tmp_db.execute("UPDATE orders SET state='failed' WHERE order_id='ord_amb'")
    dog.tick()

    assert len(submitter.calls) == 2


def test_a_submitted_exit_that_later_fills_latches_instead_of_selling_again(tmp_db, risk_file):
    """`executor.submit` returns SUBMITTED. The latch has to be set when it *fills*."""
    make_position(tmp_db, mode=LaneMode.CANARY)
    tmp_db.execute(
        "INSERT INTO orders (order_id, chain, token, side, lane, mode, input_token, output_token, "
        "amount_in, min_out, slippage_bps, state, provider, created_ms, updated_ms) "
        "VALUES ('ord_live',?,?,'sell',?,?,?,?,'1','0',300,'submitted','gmgn',?,?)",
        (Chain.SOL.value, TOKEN, Lane.CONFLUENCE_5.value, LaneMode.CANARY.value, TOKEN,
         SOL_NATIVE_MINT, now_ms(), now_ms()),
    )
    submitter = RecordingSubmitter(wd.ExitOutcome(True, OrderState.SUBMITTED, "ord_live", "sent"))
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "0.6"}), submitter=submitter)

    dog.tick()
    dog.tick()  # still in flight
    tmp_db.execute("UPDATE orders SET state='filled' WHERE order_id='ord_live'")
    dog.tick()  # filled, but `positions` was never written back

    assert len(submitter.calls) == 1
    assert fetch_one(tmp_db, "SELECT exit_final FROM watchdog_state WHERE position_id='pos_1'")[
        "exit_final"
    ] == 1
    assert events_named(tmp_db, "exit_suppressed")


def test_a_drained_request_is_durable_before_it_is_acted_on(tmp_db, risk_file):
    """The cursor must not outrun the state row, or a crash eats the exit request."""
    make_position(tmp_db)
    ev.emit(
        EventKind.PROTECTION_TRIGGERED,
        {"position_id": "pos_1", "pct": 100, "reason": "close it", "source": "agent"},
        conn=tmp_db,
    )

    def explode(self, position, cfg, report):
        raise RuntimeError("crash right after the drain")

    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "1.5"}), submitter=RecordingSubmitter())
    dog._check_position = explode.__get__(dog, wd.Watchdog)
    dog.tick()

    row = fetch_one(tmp_db, "SELECT * FROM watchdog_state WHERE position_id='pos_1'")
    assert row["pending_pct"] == "100"

    fresh = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "1.5"}), submitter=RecordingSubmitter())
    fresh.tick()

    assert len(fresh.submitter.calls) == 1


def test_a_failed_exit_backs_off_instead_of_hammering(tmp_db, risk_file):
    make_position(tmp_db, mode=LaneMode.CANARY)
    submitter = RecordingSubmitter(wd.ExitOutcome(False, OrderState.FAILED, "ord_f", "refused"))
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "0.6"}), submitter=submitter)

    dog.tick()
    dog.tick()

    assert len(submitter.calls) == 1
    assert events_named(tmp_db, "exit_failed")[0]["retry_in_ms"] == wd.RETRY_BASE_MS


def test_an_exit_refused_by_an_entry_gate_is_flagged_as_a_bug(tmp_db, risk_file):
    make_position(tmp_db, mode=LaneMode.CANARY)
    submitter = RecordingSubmitter(
        wd.ExitOutcome(False, OrderState.FAILED, "ord_f", "ExecutionRefused: kill switch engaged")
    )
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "0.6"}), submitter=submitter)

    dog.tick()

    assert events_named(tmp_db, "exit_failed")[0]["gated_exit_bug"] is True


# --------------------------------------------------------------------------------------
# resilience
# --------------------------------------------------------------------------------------


def test_one_exploding_position_does_not_stop_the_loop(tmp_db, risk_file):
    make_position(tmp_db, position_id="pos_bad", token=TOKEN)
    make_position(tmp_db, position_id="pos_good", token=TOKEN_B)
    source = FakeSource({TOKEN: "0.6", TOKEN_B: "0.6"})
    submitter = RecordingSubmitter(explode_on={TOKEN})
    dog = wd.Watchdog(tmp_db, price_source=source, submitter=submitter)

    report = dog.tick()

    assert report.checked == 2
    assert report.errors == 1
    assert report.exits == 1
    assert [c[0] for c in submitter.calls] == ["pos_good"]
    error = events_named(tmp_db, "position_error")[0]
    assert error["position_id"] == "pos_bad" and "RuntimeError" in error["error"]
    assert events_named(tmp_db, "heartbeat")[0]["errors"] == 1


def test_an_unreadable_protection_config_falls_back_to_defaults(tmp_db, risk_file):
    def boom():
        raise ValueError("risk.yaml is garbage")

    make_position(tmp_db)
    dog = wd.Watchdog(
        tmp_db, price_source=FakeSource({TOKEN: "1.5"}),
        submitter=RecordingSubmitter(), cfg_provider=boom,
    )

    report = dog.tick()

    assert report.checked == 1 and report.errors == 0
    assert events_named(tmp_db, "config_unreadable")


# --------------------------------------------------------------------------------------
# the live submission path
# --------------------------------------------------------------------------------------


def test_a_live_exit_refuses_to_size_without_token_decimals(tmp_db, risk_file):
    make_position(tmp_db, mode=LaneMode.LIVE)
    source = FakeSource({TOKEN: "0.6", SOL_NATIVE_MINT: "200"})
    dog = wd.Watchdog(tmp_db, price_source=source, submitter=wd.DefaultExitSubmitter(tmp_db, source))

    dog.tick()

    assert sell_orders(tmp_db) == []
    assert "decimals unknown" in events_named(tmp_db, "exit_failed")[0]["detail"]


def test_a_live_exit_refuses_min_out_zero_when_the_native_price_is_unknown(tmp_db, risk_file):
    make_position(tmp_db, mode=LaneMode.LIVE)
    tmp_db.execute(
        "INSERT INTO tokens (chain, address, decimals, first_seen_ms) VALUES (?,?,?,?)",
        (Chain.SOL.value, TOKEN, 6, now_ms()),
    )
    source = FakeSource({TOKEN: "0.6"})  # no price for SOL itself
    dog = wd.Watchdog(tmp_db, price_source=source, submitter=wd.DefaultExitSubmitter(tmp_db, source))

    dog.tick()

    assert sell_orders(tmp_db) == []
    assert "min_out" in events_named(tmp_db, "exit_failed")[0]["detail"]


def test_a_live_exit_sizes_min_out_from_the_native_price(tmp_db, risk_file, monkeypatch):
    make_position(tmp_db, mode=LaneMode.LIVE, qty=2_000_000)
    tmp_db.execute(
        "INSERT INTO tokens (chain, address, decimals, first_seen_ms) VALUES (?,?,?,?)",
        (Chain.SOL.value, TOKEN, 6, now_ms()),
    )
    sent: list = []

    def fake_submit(order, conn=None):
        sent.append(order)
        return wd.executor.SubmitResult(order.order_id, OrderState.SUBMITTED, "prov1", "0xdead")

    monkeypatch.setattr(wd.executor, "submit", fake_submit)
    source = FakeSource({TOKEN: "0.6", SOL_NATIVE_MINT: "200"})
    dog = wd.Watchdog(tmp_db, price_source=source, submitter=wd.DefaultExitSubmitter(tmp_db, source))

    report = dog.tick()

    assert report.exits == 1
    order = sent[0]
    assert order.side.value == "sell"
    assert order.amount_in == 2_000_000
    # 2 tokens * $0.6 = $1.20 -> 0.006 SOL = 6_000_000 lamports, less the slippage ceiling.
    assert 0 < order.min_out < 6_000_000
    assert order.min_out == int(
        Decimal("6000000") * Decimal(10_000 - wd._exit_slippage_bps()) / Decimal(10_000)
    )


def test_shadow_positions_never_take_the_live_path(tmp_db, risk_file, monkeypatch):
    make_position(tmp_db, mode=LaneMode.SHADOW)

    def explode(*a, **kw):
        raise AssertionError("a shadow position must exit on paper")

    monkeypatch.setattr(wd.executor, "submit", explode)
    source = FakeSource({TOKEN: "0.6", SOL_NATIVE_MINT: "200"})
    dog = wd.Watchdog(tmp_db, price_source=source, submitter=wd.DefaultExitSubmitter(tmp_db, source))

    assert dog.tick().exits == 1
    assert sell_orders(tmp_db)[0]["provider"] == "paper"


def test_a_paper_exit_without_liquidity_is_refused_not_invented(tmp_db, risk_file):
    make_position(tmp_db)
    source = FakeSource({TOKEN: "0.6"}, liquidity=None)
    dog = wd.Watchdog(tmp_db, price_source=source, submitter=wd.DefaultExitSubmitter(tmp_db, source))

    report = dog.tick()

    assert report.exits == 0 and report.exit_failures == 1
    assert "liquidity unavailable" in events_named(tmp_db, "exit_failed")[0]["detail"]


# --------------------------------------------------------------------------------------
# the service wrapper
# --------------------------------------------------------------------------------------


def test_run_watchdog_stops_after_max_ticks_and_summarises(tmp_db, risk_file):
    make_position(tmp_db)
    summary = wd.run_watchdog(
        tmp_db,
        price_source=FakeSource({TOKEN: "0.6"}),
        submitter=RecordingSubmitter(),
        interval_s=0,
        max_ticks=2,
        install_signals=False,
    )

    assert summary["ticks"] == 2
    assert summary["exits"] == 1  # the second tick is latched by exit_final
    assert events_named(tmp_db, "started") and events_named(tmp_db, "stopped")


def test_run_watchdog_honours_a_stop_event(tmp_db, risk_file):
    make_position(tmp_db)
    stop = threading.Event()
    stop.set()

    summary = wd.run_watchdog(
        tmp_db, price_source=wd.NullPriceSource(), interval_s=0, stop=stop, install_signals=False
    )

    assert summary["ticks"] == 0


def test_run_watchdog_survives_a_tick_that_raises(tmp_db, risk_file, monkeypatch):
    def boom(self):
        raise RuntimeError("tick exploded")

    monkeypatch.setattr(wd.Watchdog, "tick", boom)

    summary = wd.run_watchdog(tmp_db, interval_s=0, max_ticks=3, install_signals=False)

    assert summary["ticks"] == 3 and summary["errors"] == 3
    assert len(events_named(tmp_db, "tick_failed")) == 3


# --------------------------------------------------------------------------------------
# the P1-3 adapter
# --------------------------------------------------------------------------------------


def test_an_unnamed_or_unknown_price_source_resolves_to_blindness(tmp_db, risk_file):
    """Blind is the safe resolution for a name we do not recognise: it never invents one."""
    assert isinstance(wd.resolve_price_source(None), wd.NullPriceSource)
    assert isinstance(wd.resolve_price_source("nonsense"), wd.NullPriceSource)
    assert isinstance(wd.resolve_price_source("dexscreener"), wd.ProviderPriceSource)


def test_the_shipped_config_names_a_real_price_source():
    """A watchdog with no price source ticks, reports healthy and can never fire a stop.

    This reads the repository's own `config/risk.yaml` rather than a fixture, because the
    thing worth pinning is the shipped default an operator actually gets.
    """
    import yaml

    from kaiba.core.config import REPO_ROOT

    raw = yaml.safe_load((REPO_ROOT / "config" / "risk.yaml").read_text(encoding="utf-8"))
    name = (raw.get("protection") or {}).get("price_source")
    assert name, "protection.price_source is unset; the watchdog would ship blind"
    assert not isinstance(wd.resolve_price_source(name), wd.NullPriceSource)


def test_risk_yaml_can_turn_the_provider_source_on(tmp_db, risk_file):
    import yaml

    raw = yaml.safe_load(risk_file.read_text())
    raw.setdefault("protection", {})["price_source"] = "dexscreener"
    risk_file.write_text(yaml.safe_dump(raw))

    assert wd.configured_price_source_name() == "dexscreener"
    assert isinstance(wd.resolve_price_source(wd.configured_price_source_name()), wd.ProviderPriceSource)


def test_the_provider_adapter_maps_a_quote(tmp_db, monkeypatch):
    from kaiba.providers import prices

    def fake_quote(chain, token, **kw):
        return prices.Quote(
            chain=chain,
            token=token,
            price_usd=Decimal("0.42"),
            liquidity_usd=Decimal("12345"),
            receipt=prices.Receipt(
                provider="dexscreener",
                endpoint="price.quote",
                observed_at_ms=now_ms(),
                basis=EvidenceBasis.PROVIDER_REPORTED,
            ),
            source="dexscreener",
        )

    monkeypatch.setattr(prices, "quote", fake_quote)
    got = wd.ProviderPriceSource().quote(Chain.SOL, TOKEN)

    assert got.usable
    assert got.price_usd == Decimal("0.42") and got.liquidity_usd == Decimal("12345")


def test_the_provider_adapter_turns_an_outage_into_blindness(tmp_db, monkeypatch):
    from kaiba.providers import prices

    def boom(chain, token, **kw):
        raise TimeoutError("dexscreener down")

    monkeypatch.setattr(prices, "quote", boom)
    got = wd.ProviderPriceSource().quote(Chain.SOL, TOKEN)

    assert not got.usable
    assert got.basis is EvidenceBasis.UNAVAILABLE
    assert "TimeoutError" in (got.note or "")


def test_the_cli_import_path_resolves(tmp_db):
    """`kaiba run protection` does `from kaiba.execution.protection import run_watchdog`."""
    from kaiba.execution.protection import run_watchdog

    assert run_watchdog is wd.run_watchdog


def test_a_successful_exit_submit_resets_the_backoff_counter(tmp_db, risk_file):
    """Found 2026-09-21: ``exit_attempts`` was incremented on every attempt and reset
    NOWHERE, so a position that had once failed to exit carried its exponential backoff
    (RETRY_BASE_MS * 2**(attempts-1), capped at RETRY_MAX_MS) forever -- into the next,
    unrelated stop. A burnt counter on a live position is a stop that fires late by
    construction. The fourth of four exit-protection gaps from the high-volume review.

    Drives one FAILED submit (attempts -> 1), clears the backoff the way time would, then
    a successful submit -- and asserts the persisted counter is back to zero.
    """
    make_position(tmp_db, mode=LaneMode.CANARY)
    pid = fetch_one(tmp_db, "SELECT position_id FROM positions")["position_id"]
    submitter = RecordingSubmitter(wd.ExitOutcome(False, OrderState.FAILED, "ord_f", "refused"))
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "0.6"}), submitter=submitter)

    dog.tick()
    row = fetch_one(tmp_db, "SELECT exit_attempts, exit_retry_after_ms FROM watchdog_state WHERE position_id=?", (pid,))
    assert row["exit_attempts"] == 1 and row["exit_retry_after_ms"] is not None, "the failure must arm a backoff"

    # let the backoff elapse without a clock: the watchdog reads this row each tick
    tmp_db.execute("UPDATE watchdog_state SET exit_retry_after_ms=NULL WHERE position_id=?", (pid,))
    tmp_db.commit()
    submitter.outcome = wd.ExitOutcome(True, OrderState.SUBMITTED, "ord_s", "submitted")
    dog.tick()

    assert len(submitter.calls) == 2, "the second tick must have submitted again"
    row = fetch_one(tmp_db, "SELECT exit_attempts FROM watchdog_state WHERE position_id=?", (pid,))
    assert row["exit_attempts"] == 0, (
        f"a successful submit must reset the counter; it is {row['exit_attempts']}, so the "
        "next unrelated failure would back off from 2x instead of 1x"
    )
