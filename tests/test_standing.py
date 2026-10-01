"""Venue-side standing orders: the protection that has to outlive this process.

The gap these tests exist for: ``protection.py``'s ladder is pure, correct and entirely
in memory. Kill the process with a position open and there is no stop. ``standing.py``
mirrors the price-triggered rungs onto GMGN, and the three things that can go wrong with
a mirror are all worse than having none:

* two parties selling the same tokens (the double-sell);
* a placement we could not read being treated as "not placed" and sent twice;
* a placement that silently did not happen while the operator reads ``stop_loss_bps``
  and sizes as though a stop exists.

Every safety assertion below is written so that removing the guard it covers makes it
fail. No test here spawns a process or touches a network: :class:`FakeRunner` is the
venue, and it records the exact argv that would have been sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
import yaml

from kaiba.core import events as ev
from kaiba.core.config import load_risk, save_risk
from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, EvidenceBasis, Lane, LaneMode, Position, now_ms
from kaiba.execution import standing as st
from kaiba.execution.protection import ProtectionConfig, evaluate, initial_state

TOKEN = "CJF7MNqb9xv1XrTs5St1Du5JuQXLsfFBB137vmYRKpnb"
WALLET = "8Kk2Yy7Y6PY7dLuLkVrbYpHn5iFPveiSRvsTjBpHfFRj"
POSITION = "pos_standing_1"


# --------------------------------------------------------------------------------------
# doubles and fixtures
# --------------------------------------------------------------------------------------


@dataclass
class Call:
    argv: list[str]
    endpoint: str
    priority: Priority
    mutating: bool

    @property
    def action(self) -> str:
        return " ".join(self.argv[:3])

    def flag(self, name: str) -> str | None:
        return self.argv[self.argv.index(name) + 1] if name in self.argv else None


@dataclass
class FakeRunner:
    """The venue, under the test's control. Records argv; never runs anything."""

    scripted: dict[str, list[st.VenueResult]] = field(default_factory=dict)
    listed: list[dict[str, Any]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    _n: int = 0
    explode: bool = False

    def run(
        self,
        argv,  # noqa: ANN001 - test double
        *,
        endpoint: str,
        priority: Priority,
        mutating: bool,
        conn=None,  # noqa: ANN001
    ) -> st.VenueResult:
        self.calls.append(Call(list(argv), endpoint, priority, mutating))
        if self.explode:
            raise RuntimeError("runner on fire")
        queue = self.scripted.get(endpoint)
        if queue:
            return queue.pop(0)
        if endpoint == st.CREATE_ENDPOINT:
            self._n += 1
            return st.VenueResult(st.Outcome.OK, {"data": {"order_id": f"gm_{self._n}"}})
        if endpoint == st.LIST_ENDPOINT:
            return st.VenueResult(st.Outcome.OK, {"data": {"orders": list(self.listed)}})
        return st.VenueResult(st.Outcome.OK, {"data": {}})

    def creates(self) -> list[Call]:
        return [c for c in self.calls if c.endpoint == st.CREATE_ENDPOINT]

    def cancels(self) -> list[Call]:
        return [c for c in self.calls if c.endpoint == st.CANCEL_ENDPOINT]


def ambiguous(detail: str = "gmgn-cli returned unparseable output") -> st.VenueResult:
    return st.VenueResult(st.Outcome.AMBIGUOUS, None, detail)


def refused(detail: str = "provider rejected the order") -> st.VenueResult:
    return st.VenueResult(st.Outcome.REFUSED, None, detail)


ENABLED_ENV = st.VenueEnv(
    api_key="key",
    signing_key="pem",
    automation_enabled=True,
    cli_present=True,
    wallet=WALLET,
    price_unit="usd",
    priority_fee="0.0001",
    tip_fee="0.002",
)


@pytest.fixture
def risk_file(tmp_path, monkeypatch):
    """An isolated risk.yaml with the feature switched on and declared."""
    path = tmp_path / "risk.yaml"
    save_risk(load_risk(), path)
    raw = yaml.safe_load(path.read_text())
    raw["protection"].update(
        {
            "use_provider_orders": True,
            "standing_price_unit": "usd",
            "standing_priority_fee_sol": "0.0001",
            "standing_tip_fee_sol": "0.002",
            "standing_min_sync_interval_s": 0,
            "standing_max_writes": 8,
            "standing_reprice_bps": 100,
        }
    )
    raw["chains"]["sol"]["wallet"] = WALLET
    path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))
    return path


@pytest.fixture
def enabled(risk_file, monkeypatch):
    """The feature fully available, without depending on the operator's own .env."""
    monkeypatch.setattr(st, "venue_env", lambda chain, block=None: ENABLED_ENV)
    return risk_file


def set_protection(path, **values) -> None:
    raw = yaml.safe_load(path.read_text())
    raw["protection"].update(values)
    path.write_text(yaml.safe_dump(raw))


def make_position(
    conn,
    *,
    position_id: str = POSITION,
    entry: str | None = "1.0",
    qty: int = 1_000_000,
    mode: LaneMode = LaneMode.LIVE,
) -> Position:
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty, qty_total, "
        "cost_native, proceeds_native, realized_native, entry_price_usd, peak_price_usd) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            position_id, Chain.SOL.value, TOKEN, Lane.MANUAL.value, mode.value, now_ms(),
            str(qty), str(qty), "5000000", "0", "0", entry, entry,
        ),
    )
    return Position(
        position_id=position_id,
        chain=Chain.SOL,
        token=TOKEN,
        lane=Lane.MANUAL,
        mode=mode,
        qty=qty,
        qty_total=qty,
        entry_price_usd=Decimal(entry) if entry else None,
    )


def standing_rows(conn, *, open_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM standing_orders"
    if open_only:
        sql += " WHERE state IN ('placing','live','unknown')"
    return fetch_all(conn, sql + " ORDER BY id")


def events_named(conn, name: str) -> list[dict]:
    return [
        e.payload
        for e in ev.recent(limit=800, conn=conn)
        if isinstance(e.payload, dict) and e.payload.get("event") == name
    ]


@pytest.fixture
def cfg() -> ProtectionConfig:
    """The shipped ladder: -30% stop, 2x/5x/10x rungs, trailing from 2x."""
    return ProtectionConfig()


# --------------------------------------------------------------------------------------
# rendering: protection.py stays the only ladder
# --------------------------------------------------------------------------------------


def test_render_turns_the_rendered_percentages_into_absolute_prices(cfg):
    state = initial_state(POSITION, "2.0", cfg)
    rendering = st.render_intents(state, chain=Chain.SOL, cfg=cfg)
    by_tag = rendering.by_tag()

    assert by_tag["tp1"].trigger_price_usd == Decimal(4)  # 2x of a 2.00 entry
    assert by_tag["tp2"].trigger_price_usd == Decimal(10)
    assert by_tag["tp3"].trigger_price_usd == Decimal(20)
    assert by_tag["stop"].trigger_price_usd == Decimal("1.4")  # -30%
    assert by_tag["tp1"].sub_order_type == st.SUB_TAKE_PROFIT
    assert by_tag["stop"].sub_order_type == st.SUB_STOP_LOSS
    assert by_tag["tp1"].sell_ratio == Decimal(50)
    assert by_tag["stop"].sell_ratio == Decimal(100)


def test_render_drops_rungs_the_ladder_already_fired(cfg):
    state = initial_state(POSITION, "1.0", cfg)
    evaluate(state, price_usd=2, cfg=cfg)  # fires tp1
    assert state.tp_done == ["tp1"]

    tags = set(st.render_intents(state, chain=Chain.SOL, cfg=cfg).by_tag())
    assert "tp1" not in tags, "a fired rung must never be mirrored; that is a double-sell"
    assert {"tp2", "tp3", "stop"} <= tags


def test_render_uses_the_ratcheted_stop_when_it_is_above_entry(cfg):
    """`loss_stop` clamps at breakeven; `limit_order` takes an absolute price and need not."""
    state = initial_state(POSITION, "1.0", cfg)
    evaluate(state, price_usd=2, cfg=cfg)
    evaluate(state, price_usd=8, cfg=cfg)
    assert state.stop_price > state.entry_price

    stop = st.render_intents(state, chain=Chain.SOL, cfg=cfg).by_tag()["stop"]
    assert stop.trigger_price_usd == state.stop_price
    assert stop.trigger_price_usd > state.entry_price


def test_render_never_places_a_stop_below_what_protection_says(cfg):
    state = initial_state(POSITION, "1.0", cfg)
    for price in ("1.5", "2.0", "3.0", "6.0", "11.0"):
        evaluate(state, price_usd=price, cfg=cfg)
        stop = st.render_intents(state, chain=Chain.SOL, cfg=cfg).by_tag()["stop"]
        assert stop.trigger_price_usd >= state.stop_price


def test_render_reports_the_trailing_rung_as_unmirrorable(cfg):
    """`order strategy create --order-type limit_order` has no trailing form. Say so."""
    state = initial_state(POSITION, "1.0", cfg)
    rendering = st.render_intents(state, chain=Chain.SOL, cfg=cfg)
    assert any("profit_stop_trace" in u for u in rendering.unmirrorable)
    assert "trail" not in rendering.by_tag()


def test_render_is_empty_where_the_venue_refuses_condition_orders(cfg):
    state = initial_state(POSITION, "1.0", cfg)
    rendering = st.render_intents(state, chain=Chain.ARC, cfg=cfg)
    assert not rendering.ok
    assert "arc" in (rendering.reason or "")


def test_render_refuses_without_an_entry_price(cfg):
    state = initial_state(POSITION, 0, cfg)
    rendering = st.render_intents(state, chain=Chain.SOL, cfg=cfg)
    assert not rendering.ok and rendering.reason == "entry_price_unavailable"


# --------------------------------------------------------------------------------------
# feature honesty: `use_provider_orders` may only be true when it is true
# --------------------------------------------------------------------------------------


def test_the_flag_is_off_by_default_and_effective_is_false(tmp_path, monkeypatch):
    path = tmp_path / "risk.yaml"
    save_risk(load_risk(), path)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))
    state = st.feature_state(Chain.SOL, env=ENABLED_ENV)
    assert state.configured is False
    assert state.effective is False


@pytest.mark.parametrize(
    ("field_name", "value", "blocker"),
    [
        ("signing_key", "", "gmgn_signing_key_missing"),
        ("api_key", "", "gmgn_api_key_missing"),
        ("automation_enabled", False, "automation_not_enabled"),
        ("cli_present", False, "gmgn_cli_not_found"),
        ("wallet", None, "wallet_unbound_in_risk_yaml"),
        ("price_unit", None, "standing_price_unit_undeclared"),
        ("priority_fee", None, "sol_fee_params_unconfigured"),
        ("tip_fee", None, "sol_fee_params_unconfigured"),
    ],
)
def test_configured_but_impossible_is_never_effective(risk_file, field_name, value, blocker):
    """Each of these is a way the venue call cannot be made. None of them may read as on."""
    import dataclasses

    env = dataclasses.replace(ENABLED_ENV, **{field_name: value})
    state = st.feature_state(Chain.SOL, env=env)
    assert state.configured is True, "the fixture turns the flag on"
    assert state.available is False
    assert state.effective is False, "configured but impossible must never report protected"
    assert blocker in state.blockers


def test_a_chain_without_condition_orders_is_a_blocker(risk_file):
    assert "chain_unsupported:arc" in st.feature_state(Chain.ARC, env=ENABLED_ENV).blockers


def test_everything_present_makes_the_flag_effective(risk_file):
    state = st.feature_state(Chain.SOL, env=ENABLED_ENV)
    assert state.blockers == ()
    assert state.effective is True


def test_health_never_says_protected_without_a_live_venue_stop(tmp_db, enabled):
    make_position(tmp_db)
    report = st.health(Chain.SOL, tmp_db)
    assert report["feature"]["effective"] is True
    assert report["says_protected"] is False, "a switched-on feature is not a stop"
    assert report["unprotected"] == [POSITION]


# --------------------------------------------------------------------------------------
# placement
# --------------------------------------------------------------------------------------


def sync(tmp_db, position, state, runner, cfg, **kw) -> st.SyncReport:
    return st.sync_position(
        position, state, conn=tmp_db, runner=runner, cfg=cfg, force=True, **kw
    )


def test_sync_places_the_whole_surviving_ladder(tmp_db, enabled, cfg):
    position = make_position(tmp_db)
    runner = FakeRunner()
    report = sync(tmp_db, position, initial_state(POSITION, "1.0", cfg), runner, cfg)

    assert set(report.placed) == {"stop", "tp1", "tp2", "tp3"}
    rows = {r["tag"]: r for r in standing_rows(tmp_db)}
    assert all(r["state"] == "live" for r in rows.values())
    assert all(r["owner"] == "venue" for r in rows.values())
    assert rows["stop"]["trigger_price_usd"] == "0.7"
    assert [c.action for c in runner.creates()][0] == "order strategy create"


def test_the_stop_is_placed_before_the_take_profits(tmp_db, enabled, cfg):
    """If the write budget runs out, the rung whose absence loses money goes first."""
    position = make_position(tmp_db)
    set_protection(enabled, standing_max_writes=1)
    runner = FakeRunner()
    report = sync(tmp_db, position, initial_state(POSITION, "1.0", cfg), runner, cfg)
    assert report.placed == ["stop"]
    assert len(runner.creates()) == 1


def test_a_row_exists_before_the_venue_call(tmp_db, enabled, cfg):
    """Reserve before submit: a crash mid-placement must leave evidence, not a mystery."""
    seen: list[int] = []

    class Peeking(FakeRunner):
        def run(self, argv, *, endpoint, priority, mutating, conn=None):  # noqa: ANN001
            if endpoint == st.CREATE_ENDPOINT:
                seen.append(
                    len(fetch_all(tmp_db, "SELECT id FROM standing_orders WHERE state='placing'"))
                )
            return super().run(argv, endpoint=endpoint, priority=priority, mutating=mutating, conn=conn)

    position = make_position(tmp_db)
    sync(tmp_db, position, initial_state(POSITION, "1.0", cfg), Peeking(), cfg)
    assert seen and all(n >= 1 for n in seen), "the row must be written before the send"


def test_an_ambiguous_placement_is_unknown_and_is_never_placed_again(tmp_db, enabled, cfg):
    """The swap path's rule, applied here: UNKNOWN is never 'not placed'."""
    position = make_position(tmp_db)
    runner = FakeRunner(scripted={st.CREATE_ENDPOINT: [ambiguous()]})
    first = sync(tmp_db, position, initial_state(POSITION, "1.0", cfg), runner, cfg)
    assert "stop" in first.ambiguous

    row = fetch_one(tmp_db, "SELECT * FROM standing_orders WHERE tag='stop'")
    assert row["state"] == "unknown"
    assert row["owner"] == "contested"
    assert row["settled_ms"] is None, "nothing about an ambiguous placement is settled"

    creates_before = len(runner.creates())
    second = sync(tmp_db, position, initial_state(POSITION, "1.0", cfg), runner, cfg)
    stop_creates = [
        c for c in runner.creates()[creates_before:] if c.flag("--sub-order-type") == "stop_loss"
    ]
    assert stop_creates == [], "re-placing an ambiguous order is how you get two live stops"
    assert "stop" in second.ambiguous


def test_an_ambiguous_placement_is_reported_as_unprotected(tmp_db, enabled, cfg):
    position = make_position(tmp_db)
    sync(
        tmp_db,
        position,
        initial_state(POSITION, "1.0", cfg),
        FakeRunner(scripted={st.CREATE_ENDPOINT: [ambiguous()]}),
        cfg,
    )
    status = st.protection_status(POSITION, tmp_db)
    assert status.venue_protected is False
    assert status.basis is EvidenceBasis.UNAVAILABLE
    assert status.unprotected_reason == "venue_stop_unknown"
    assert "stop" in status.unknown_tags


def test_a_refused_placement_is_failed_and_may_be_retried(tmp_db, enabled, cfg):
    """A refusal is positive evidence nothing was sent, so a retry is correct here."""
    position = make_position(tmp_db)
    runner = FakeRunner(scripted={st.CREATE_ENDPOINT: [refused()]})
    first = sync(tmp_db, position, initial_state(POSITION, "1.0", cfg), runner, cfg)
    assert "stop" in first.failed
    assert fetch_one(tmp_db, "SELECT state FROM standing_orders WHERE tag='stop'")["state"] == "failed"

    second = sync(tmp_db, position, initial_state(POSITION, "1.0", cfg), FakeRunner(), cfg)
    assert "stop" in second.placed


def test_a_failed_placement_is_loud_and_names_the_impact(tmp_db, enabled, cfg):
    """A failure that only appears in a log is the same hole with extra steps."""
    position = make_position(tmp_db)
    sync(
        tmp_db,
        position,
        initial_state(POSITION, "1.0", cfg),
        FakeRunner(scripted={st.CREATE_ENDPOINT: [refused("429 IP rate limit exceeded")]}),
        cfg,
    )
    failures = events_named(tmp_db, "standing_placement_failed")
    assert failures and "unprotected" in failures[0]["impact"]
    assert [
        e for e in ev.recent(limit=800, conn=tmp_db)
        if e.payload.get("event") == "standing_placement_failed" and e.level == "error"
    ]
    unprotected = events_named(tmp_db, "position_not_venue_protected")
    assert unprotected and unprotected[0]["basis"] == EvidenceBasis.UNAVAILABLE.value


def test_nothing_is_placed_when_the_feature_is_not_effective(tmp_db, risk_file, cfg, monkeypatch):
    import dataclasses

    monkeypatch.setattr(
        st, "venue_env", lambda chain, block=None: dataclasses.replace(ENABLED_ENV, signing_key="")
    )
    position = make_position(tmp_db)
    runner = FakeRunner()
    report = sync(tmp_db, position, initial_state(POSITION, "1.0", cfg), runner, cfg)

    assert runner.calls == []
    assert standing_rows(tmp_db) == []
    assert report.unprotected_reason and "gmgn_signing_key_missing" in report.unprotected_reason
    assert events_named(tmp_db, "standing_unavailable"), "configured-but-impossible must be said"


def test_an_undeclared_price_unit_blocks_placement(tmp_db, risk_file, cfg, monkeypatch):
    """`--check-price` is absolute and its denomination is not documented. Never guess."""
    import dataclasses

    monkeypatch.setattr(
        st, "venue_env", lambda chain, block=None: dataclasses.replace(ENABLED_ENV, price_unit=None)
    )
    runner = FakeRunner()
    sync(tmp_db, make_position(tmp_db), initial_state(POSITION, "1.0", cfg), runner, cfg)
    assert runner.calls == []


def test_a_quote_denominated_venue_needs_a_native_price(tmp_db, risk_file, cfg, monkeypatch):
    import dataclasses

    monkeypatch.setattr(
        st,
        "venue_env",
        lambda chain, block=None: dataclasses.replace(ENABLED_ENV, price_unit="quote"),
    )
    position = make_position(tmp_db)
    runner = FakeRunner()

    blind = sync(
        tmp_db, position, initial_state(POSITION, "1.0", cfg), runner, cfg, native_price_usd=None
    )
    assert runner.calls == []
    assert blind.unprotected_reason == "native_price_unavailable"

    priced = sync(
        tmp_db,
        position,
        initial_state(POSITION, "1.0", cfg),
        runner,
        cfg,
        native_price_usd=lambda: Decimal(200),
    )
    assert "stop" in priced.placed
    stop = next(c for c in runner.creates() if c.flag("--sub-order-type") == "stop_loss")
    assert stop.flag("--check-price") == "0.0035"  # 0.70 USD / 200 USD per SOL


def test_a_missing_entry_price_places_nothing(tmp_db, enabled, cfg):
    position = make_position(tmp_db, entry=None)
    runner = FakeRunner()
    report = sync(tmp_db, position, initial_state(POSITION, 0, cfg), runner, cfg)
    assert runner.calls == []
    assert report.unprotected_reason == "entry_price_unavailable"


# --------------------------------------------------------------------------------------
# the withdrawal gate
# --------------------------------------------------------------------------------------


_DESTINATION_WORDS = (
    "recipient", "receiver", "destination", "beneficiary", "to-address", "to_address",
    "payout", "withdraw", "transfer", "bridge", "spender", "delegate",
)


def test_no_standing_order_argv_can_carry_a_destination(tmp_db, enabled, cfg):
    runner = FakeRunner()
    sync(tmp_db, make_position(tmp_db), initial_state(POSITION, "1.0", cfg), runner, cfg)
    assert runner.calls
    for call in runner.calls:
        blob = " ".join(call.argv).lower()
        for word in _DESTINATION_WORDS:
            assert word not in blob, f"{word} must not be expressible in a standing order"
        assert call.flag("--from") == WALLET


def test_the_create_parameter_set_is_compared_for_equality():
    """A gained key is as changed as a lost one; a destination could only arrive as one."""
    params = st.create_params(
        chain=Chain.SOL,
        wallet=WALLET,
        token=TOKEN,
        intent=st.StandingIntent("stop", "loss_stop", st.SUB_STOP_LOSS, Decimal("0.7"), Decimal(100)),
        trigger_price=Decimal("0.7"),
        slippage_pct="1.00",
        priority_fee="0.0001",
        tip_fee="0.002",
    )
    assert set(params) == st.CREATE_PARAM_KEYS

    with pytest.raises(st.StandingRefused):
        st._authorize("order_strategy", {**params, "recipient": "x"}, st.CREATE_PARAM_KEYS)
    with pytest.raises(st.StandingRefused):
        st._authorize("order_strategy", {k: v for k, v in params.items() if k != "check_price"},
                      st.CREATE_PARAM_KEYS)


def test_a_forbidden_parameter_trips_the_withdrawal_gate():
    from kaiba.execution.policy import WithdrawalBlocked

    keys = st.CREATE_PARAM_KEYS | {"destination"}
    params = dict.fromkeys(keys, "x")
    with pytest.raises(WithdrawalBlocked):
        st._authorize("order_strategy", params, keys)


def test_the_operations_used_are_in_the_closed_vocabulary():
    from kaiba.execution.policy import ALLOWED_READ_OPERATIONS, ALLOWED_VALUE_OPERATIONS

    assert "order_strategy" in ALLOWED_VALUE_OPERATIONS
    assert "order_cancel" in ALLOWED_READ_OPERATIONS


def test_the_entry_hook_is_empty_unless_the_feature_is_effective(risk_file, monkeypatch):
    import dataclasses

    monkeypatch.setattr(
        st, "venue_env", lambda chain, block=None: dataclasses.replace(ENABLED_ENV, signing_key="")
    )
    assert st.entry_condition_order_argv(Chain.SOL) == []


def test_the_entry_hook_carries_the_ladder_and_the_sell_ratio_basis(enabled):
    """`--sell-ratio-type` is not optional: GMGN defaults to buy_amount, which is not ours."""
    import json

    argv = st.entry_condition_order_argv(Chain.SOL)
    assert argv[0] == "--condition-orders"
    assert argv[2:] == ["--sell-ratio-type", "hold_amount"]

    orders = json.loads(argv[1])
    kinds = [o["order_type"] for o in orders]
    assert kinds.count("profit_stop") == 3
    assert "loss_stop" in kinds
    assert "profit_stop_trace" in kinds, "the trail can only be mirrored on the entry swap"
    assert all(o["side"] == "sell" for o in orders)


def test_the_entry_hook_is_empty_where_the_venue_refuses_condition_orders(enabled):
    assert st.entry_condition_order_argv(Chain.ARC) == []


def test_money_is_never_a_float_in_a_standing_row(tmp_db, enabled, cfg):
    sync(tmp_db, make_position(tmp_db), initial_state(POSITION, "0.000123", cfg), FakeRunner(), cfg)
    for row in standing_rows(tmp_db):
        assert isinstance(row["trigger_price_usd"], str)
        assert isinstance(row["sell_ratio"], str)
        assert "e" not in row["trigger_price_usd"].lower(), "no exponent notation for money"


# --------------------------------------------------------------------------------------
# rate awareness
# --------------------------------------------------------------------------------------


def test_placement_is_scheduled_not_fired_every_tick(tmp_db, enabled, cfg):
    """A GMGN write is weight 10 against a capacity of 10 refilling at 0.2/s."""
    set_protection(enabled, standing_min_sync_interval_s=60)
    position = make_position(tmp_db)
    state = initial_state(POSITION, "1.0", cfg)
    runner = FakeRunner()

    st.sync_position(position, state, conn=tmp_db, runner=runner, cfg=cfg, now=1_000_000)
    first = len(runner.calls)
    st.sync_position(position, state, conn=tmp_db, runner=runner, cfg=cfg, now=1_005_000)
    assert len(runner.calls) == first, "a sync inside the interval must not spend budget"

    st.sync_position(position, state, conn=tmp_db, runner=runner, cfg=cfg, now=1_070_000)
    assert len(runner.calls) == first, "and with nothing changed it still writes nothing"


def test_a_live_order_within_tolerance_is_left_alone(tmp_db, enabled, cfg):
    position = make_position(tmp_db)
    state = initial_state(POSITION, "1.0", cfg)
    runner = FakeRunner()
    sync(tmp_db, position, state, runner, cfg)
    before = len(runner.creates())

    # A tiny ratchet: the stop moves by well under the 100 bps reprice threshold.
    state.stop_price = Decimal("0.7001")
    report = sync(tmp_db, position, state, runner, cfg)
    assert "stop" in report.left_alone
    assert len(runner.creates()) == before
    assert runner.cancels() == []


def test_a_material_ratchet_cancels_before_it_replaces(tmp_db, enabled, cfg):
    position = make_position(tmp_db)
    state = initial_state(POSITION, "1.0", cfg)
    runner = FakeRunner()
    sync(tmp_db, position, state, runner, cfg)
    original = fetch_one(tmp_db, "SELECT provider_order_id FROM standing_orders WHERE tag='stop'")

    state.stop_price = Decimal("0.90")
    report = sync(tmp_db, position, state, runner, cfg)

    assert "stop" in report.replaced
    assert runner.cancels(), "replace must cancel first; two live orders is a double-sell"
    assert runner.cancels()[0].flag("--order-id") == original["provider_order_id"]
    live = [r for r in standing_rows(tmp_db, open_only=True) if r["tag"] == "stop"]
    assert len(live) == 1 and live[0]["trigger_price_usd"] == "0.9"


def test_a_stop_that_went_backwards_is_never_sent(tmp_db, enabled, cfg):
    """The ratchet only raises. A lower desired stop is a bug to report, not an order."""
    position = make_position(tmp_db)
    state = initial_state(POSITION, "1.0", cfg)
    runner = FakeRunner()
    sync(tmp_db, position, state, runner, cfg)
    before = len(runner.calls)

    state.stop_price = Decimal("0.40")
    report = sync(tmp_db, position, state, runner, cfg)
    assert "stop" in report.left_alone
    assert len(runner.calls) == before
    assert events_named(tmp_db, "standing_stop_regressed")


def test_a_rung_the_ladder_fired_is_cancelled_at_the_venue(tmp_db, enabled, cfg):
    position = make_position(tmp_db)
    state = initial_state(POSITION, "1.0", cfg)
    runner = FakeRunner()
    sync(tmp_db, position, state, runner, cfg)

    evaluate(state, price_usd=2, cfg=cfg)  # tp1 fires in process
    sync(tmp_db, position, state, runner, cfg)

    tp1 = fetch_one(tmp_db, "SELECT * FROM standing_orders WHERE tag='tp1' ORDER BY id DESC LIMIT 1")
    assert tp1["state"] == "cancelled"
    assert [r["tag"] for r in standing_rows(tmp_db, open_only=True)].count("tp1") == 0


def test_the_write_budget_is_bounded_per_sync(tmp_db, enabled, cfg):
    set_protection(enabled, standing_max_writes=2)
    runner = FakeRunner()
    sync(tmp_db, make_position(tmp_db), initial_state(POSITION, "1.0", cfg), runner, cfg)
    assert len(runner.creates()) == 2


def test_a_create_is_charged_as_a_swap_and_a_cancel_runs_at_exit_priority(tmp_db, enabled, cfg):
    from kaiba.core.limiter import limits_for

    assert limits_for("gmgn").weight_for(st.CREATE_ENDPOINT) == 10

    position = make_position(tmp_db)
    state = initial_state(POSITION, "1.0", cfg)
    runner = FakeRunner()
    sync(tmp_db, position, state, runner, cfg)
    state.stop_price = Decimal("0.95")
    sync(tmp_db, position, state, runner, cfg)
    assert runner.cancels()[0].priority is Priority.EXIT


# --------------------------------------------------------------------------------------
# ownership: the double-sell rule
# --------------------------------------------------------------------------------------


def claim(tmp_db, runner, **kw) -> st.Claim:
    return st.claim_for_exit(
        POSITION, chain=Chain.SOL, token=TOKEN, conn=tmp_db, runner=runner, **kw
    )


def test_a_full_exit_takes_every_rung_back_before_selling(tmp_db, enabled, cfg):
    position = make_position(tmp_db)
    runner = FakeRunner()
    sync(tmp_db, position, initial_state(POSITION, "1.0", cfg), runner, cfg)

    result = claim(tmp_db, runner, pct=Decimal(100), price_usd=Decimal("0.7"))
    assert set(result.released) == {"stop", "tp1", "tp2", "tp3"}
    assert result.clean
    assert standing_rows(tmp_db, open_only=True) == []
    assert all(r["owner"] == "watchdog" for r in standing_rows(tmp_db))


def test_a_trim_releases_only_the_rungs_that_could_fire_now(tmp_db, enabled, cfg):
    """The venue stop is 'sell everything held', which stays correct after a partial sale."""
    position = make_position(tmp_db)
    runner = FakeRunner()
    sync(tmp_db, position, initial_state(POSITION, "1.0", cfg), runner, cfg)

    result = claim(tmp_db, runner, pct=Decimal(50), price_usd=Decimal("2.0"))
    assert result.released == ("tp1",)
    open_tags = {r["tag"] for r in standing_rows(tmp_db, open_only=True)}
    assert "stop" in open_tags, "cancelling the stop to take a profit leaves the rest naked"
    assert {"tp2", "tp3"} <= open_tags


def test_a_trim_with_no_price_releases_every_take_profit(tmp_db, enabled, cfg):
    position = make_position(tmp_db)
    runner = FakeRunner()
    sync(tmp_db, position, initial_state(POSITION, "1.0", cfg), runner, cfg)

    result = claim(tmp_db, runner, pct=Decimal(50), price_usd=None)
    assert set(result.released) == {"tp1", "tp2", "tp3"}
    assert {r["tag"] for r in standing_rows(tmp_db, open_only=True)} == {"stop"}


def test_an_unconfirmed_cancel_is_contested_and_loud(tmp_db, enabled, cfg):
    position = make_position(tmp_db)
    runner = FakeRunner()
    sync(tmp_db, position, initial_state(POSITION, "1.0", cfg), runner, cfg)
    runner.scripted[st.CANCEL_ENDPOINT] = [ambiguous("cancel timed out")]

    result = claim(tmp_db, runner, pct=Decimal(100), price_usd=Decimal("0.7"))
    assert not result.clean
    assert "stop" in result.contested
    row = fetch_one(tmp_db, "SELECT * FROM standing_orders WHERE tag='stop'")
    assert row["state"] == "unknown" and row["owner"] == "contested"
    warned = events_named(tmp_db, "standing_cancel_unconfirmed")
    assert warned and "double-sell" in warned[0]["impact"]


def test_a_contested_rung_is_not_re_placed(tmp_db, enabled, cfg):
    position = make_position(tmp_db)
    state = initial_state(POSITION, "1.0", cfg)
    runner = FakeRunner()
    sync(tmp_db, position, state, runner, cfg)
    runner.scripted[st.CANCEL_ENDPOINT] = [ambiguous()]
    claim(tmp_db, runner, pct=Decimal(100), price_usd=Decimal("0.7"))

    before = len(runner.creates())
    report = sync(tmp_db, position, state, runner, cfg)
    assert "stop" in report.ambiguous
    assert not [
        c for c in runner.creates()[before:] if c.flag("--sub-order-type") == "stop_loss"
    ]


def test_a_claim_never_raises_even_when_the_venue_explodes(tmp_db, enabled, cfg):
    position = make_position(tmp_db)
    runner = FakeRunner()
    sync(tmp_db, position, initial_state(POSITION, "1.0", cfg), runner, cfg)
    runner.explode = True

    result = claim(tmp_db, runner, pct=Decimal(100), price_usd=Decimal("0.7"))
    assert isinstance(result, st.Claim)
    assert not result.clean or result.skipped


def test_a_claim_on_a_position_with_no_standing_orders_is_a_no_op(tmp_db, enabled):
    runner = FakeRunner()
    result = claim(tmp_db, runner, pct=Decimal(100))
    assert result.skipped == "no_standing_orders"
    assert runner.calls == []


def test_the_database_refuses_two_live_orders_for_one_rung(tmp_db, enabled, cfg):
    import sqlite3

    sync(tmp_db, make_position(tmp_db), initial_state(POSITION, "1.0", cfg), FakeRunner(), cfg)
    row = fetch_one(tmp_db, "SELECT * FROM standing_orders WHERE tag='stop'")
    with pytest.raises(sqlite3.IntegrityError):
        tmp_db.execute(
            "INSERT INTO standing_orders (position_id, chain, token, tag, order_type, "
            "sub_order_type, intent_digest, state, owner, created_ms, updated_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (row["position_id"], "sol", TOKEN, "stop", "loss_stop", "stop_loss", "d",
             "live", "venue", 1, 1),
        )


# --------------------------------------------------------------------------------------
# reconciliation
# --------------------------------------------------------------------------------------


def test_reconcile_confirms_a_live_order_without_writing(tmp_db, enabled, cfg):
    position = make_position(tmp_db)
    runner = FakeRunner()
    sync(tmp_db, position, initial_state(POSITION, "1.0", cfg), runner, cfg)
    runner.listed = [
        {"order_id": r["provider_order_id"]} for r in standing_rows(tmp_db, open_only=True)
    ]
    creates = len(runner.creates())

    report = st.reconcile(chain=Chain.SOL, conn=tmp_db, runner=runner)
    assert len(report.confirmed) == 4 and report.gone == []
    assert len(runner.creates()) == creates, "a matching mirror costs no writes"


def test_reconcile_marks_a_vanished_order_gone(tmp_db, enabled, cfg):
    position = make_position(tmp_db)
    runner = FakeRunner()
    sync(tmp_db, position, initial_state(POSITION, "1.0", cfg), runner, cfg)
    runner.listed = []

    report = st.reconcile(chain=Chain.SOL, conn=tmp_db, runner=runner)
    assert len(report.gone) == 4
    assert st.protection_status(POSITION, tmp_db).venue_protected is False
    assert events_named(tmp_db, "standing_gone")


def test_reconcile_is_the_only_way_out_of_unknown(tmp_db, enabled, cfg):
    position = make_position(tmp_db)
    runner = FakeRunner(scripted={st.CREATE_ENDPOINT: [ambiguous()]})
    sync(tmp_db, position, initial_state(POSITION, "1.0", cfg), runner, cfg)
    row = fetch_one(tmp_db, "SELECT * FROM standing_orders WHERE tag='stop'")
    assert row["state"] == "unknown" and row["provider_order_id"] is None

    # The venue did have it after all; matched back by the id we later learn.
    tmp_db.execute(
        "UPDATE standing_orders SET provider_order_id='gm_x' WHERE id=?", (row["id"],)
    )
    runner.listed = [{"order_id": "gm_x"}]
    report = st.reconcile(chain=Chain.SOL, conn=tmp_db, runner=runner)

    assert report.resolved
    assert fetch_one(tmp_db, "SELECT state FROM standing_orders WHERE id=?", (row["id"],))["state"] == "live"


def test_a_failed_listing_resolves_nothing(tmp_db, enabled, cfg):
    position = make_position(tmp_db)
    runner = FakeRunner(scripted={st.CREATE_ENDPOINT: [ambiguous()]})
    sync(tmp_db, position, initial_state(POSITION, "1.0", cfg), runner, cfg)
    runner.scripted[st.LIST_ENDPOINT] = [st.VenueResult(st.Outcome.RATE_LIMITED, None, "429")]

    report = st.reconcile(chain=Chain.SOL, conn=tmp_db, runner=runner, now=now_ms() + 10 * 60_000)
    assert report.error and "rate_limited" in report.error
    assert fetch_one(tmp_db, "SELECT state FROM standing_orders WHERE tag='stop'")["state"] == "unknown"
    assert events_named(tmp_db, "standing_reconcile_unavailable")


def test_unknown_becomes_failed_only_after_the_grace_window(tmp_db, enabled, cfg):
    position = make_position(tmp_db)
    runner = FakeRunner(scripted={st.CREATE_ENDPOINT: [ambiguous()]})
    base = now_ms()
    st.sync_position(
        position,
        initial_state(POSITION, "1.0", cfg),
        conn=tmp_db,
        runner=runner,
        cfg=cfg,
        force=True,
        now=base,
    )
    runner.listed = []

    early = st.reconcile(chain=Chain.SOL, conn=tmp_db, runner=runner, now=base + 1_000)
    assert early.still_unknown
    assert fetch_one(tmp_db, "SELECT state FROM standing_orders WHERE tag='stop'")["state"] == "unknown"

    late = st.reconcile(
        chain=Chain.SOL, conn=tmp_db, runner=runner, now=base + st.UNKNOWN_GRACE_MS + 1
    )
    assert late.gone
    assert fetch_one(tmp_db, "SELECT state FROM standing_orders WHERE tag='stop'")["state"] == "failed"


def test_reconcile_reports_an_order_we_did_not_place_and_leaves_it_alone(tmp_db, enabled):
    runner = FakeRunner(listed=[{"order_id": "someone_elses", "base_token": TOKEN}])
    report = st.reconcile(chain=Chain.SOL, conn=tmp_db, runner=runner)
    assert report.orphans == ["someone_elses"]
    assert runner.cancels() == [], "an order we did not place may be the operator's"


def test_reconcile_uses_one_listing_for_the_whole_wallet(tmp_db, enabled, cfg):
    for n in range(3):
        position = make_position(tmp_db, position_id=f"pos_{n}")
        st.sync_position(
            position,
            initial_state(f"pos_{n}", "1.0", cfg),
            conn=tmp_db,
            runner=FakeRunner(),
            cfg=cfg,
            force=True,
        )
    runner = FakeRunner()
    st.reconcile(chain=Chain.SOL, conn=tmp_db, runner=runner)
    assert len([c for c in runner.calls if c.endpoint == st.LIST_ENDPOINT]) == 1


# --------------------------------------------------------------------------------------
# the watchdog, wired
# --------------------------------------------------------------------------------------


class FakeQuote:
    name = "fake"

    def __init__(self, price: str | None = "1.0") -> None:
        self.price = price

    def quote(self, chain, token):  # noqa: ANN001 - test double
        from kaiba.execution import watchdog as wd

        if self.price is None:
            return wd.PriceQuote.unavailable("no price", source=self.name)
        return wd.PriceQuote(
            price_usd=Decimal(self.price),
            liquidity_usd=Decimal(1_000_000),
            basis=EvidenceBasis.PROVIDER_REPORTED,
            source=self.name,
        )


class RecordingSubmitter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Decimal]] = []

    def submit_exit(self, position, pct, *, quote, reason):  # noqa: ANN001 - test double
        from kaiba.core.schemas import OrderState
        from kaiba.execution import watchdog as wd

        self.calls.append((position.position_id, pct))
        return wd.ExitOutcome(True, OrderState.FILLED, "ord_x", "filled")


def test_the_watchdog_is_inert_when_the_feature_is_off(tmp_db, risk_file):
    from kaiba.execution import watchdog as wd

    set_protection(risk_file, use_provider_orders=False)
    make_position(tmp_db)
    runner = FakeRunner()
    wd.Watchdog(
        tmp_db,
        price_source=FakeQuote("1.0"),
        submitter=RecordingSubmitter(),
        standing_runner=runner,
    ).tick()

    assert runner.calls == []
    assert standing_rows(tmp_db) == []


def test_the_watchdog_places_the_venue_stop_on_the_first_tick(tmp_db, enabled):
    from kaiba.execution import watchdog as wd

    make_position(tmp_db)
    runner = FakeRunner()
    report = wd.Watchdog(
        tmp_db,
        price_source=FakeQuote("1.0"),
        submitter=RecordingSubmitter(),
        standing_runner=runner,
    ).tick()

    assert report.standing_writes > 0
    assert {r["tag"] for r in standing_rows(tmp_db, open_only=True)} == {"stop", "tp1", "tp2", "tp3"}


def test_the_watchdog_claims_the_rungs_before_it_sells(tmp_db, enabled):
    """The double-sell guard, at the one call site that matters."""
    from kaiba.execution import watchdog as wd

    make_position(tmp_db)
    runner = FakeRunner()
    submitter = RecordingSubmitter()
    wd.Watchdog(
        tmp_db, price_source=FakeQuote("1.0"), submitter=submitter, standing_runner=runner
    ).tick()
    assert standing_rows(tmp_db, open_only=True)

    # Second tick at the stop: the watchdog exits, so it must take the venue rungs back.
    wd.Watchdog(
        tmp_db, price_source=FakeQuote("0.5"), submitter=submitter, standing_runner=runner
    ).tick()

    assert submitter.calls, "the ladder should have exited at -50%"
    assert runner.cancels(), "the venue rungs were not taken back before selling"
    assert standing_rows(tmp_db, open_only=True) == []
    # The cancels come before the sell, and nothing is re-placed on top of an exit that
    # has already been sent.
    assert runner.cancels()[0].priority is Priority.EXIT
    assert st.protection_status(POSITION, tmp_db).venue_protected is False


def test_nothing_is_re_placed_on_top_of_an_exit_in_flight(tmp_db, enabled):
    """Re-arming the venue while our own sell is on the wire recreates the double-sell."""
    from kaiba.core.schemas import OrderState
    from kaiba.execution import watchdog as wd

    class Pending(RecordingSubmitter):
        def submit_exit(self, position, pct, *, quote, reason):  # noqa: ANN001 - test double
            self.calls.append((position.position_id, pct))
            return wd.ExitOutcome(True, OrderState.SUBMITTED, "ord_pending", "")

    make_position(tmp_db)
    runner = FakeRunner()
    submitter = Pending()
    wd.Watchdog(
        tmp_db, price_source=FakeQuote("1.0"), submitter=submitter, standing_runner=runner
    ).tick()
    wd.Watchdog(
        tmp_db, price_source=FakeQuote("0.5"), submitter=submitter, standing_runner=runner
    ).tick()

    assert submitter.calls
    assert standing_rows(tmp_db, open_only=True) == []


def test_a_contested_claim_never_stops_the_exit(tmp_db, enabled):
    """Exits are never gated. A brake that stops you selling is not a brake."""
    from kaiba.execution import watchdog as wd

    make_position(tmp_db)
    runner = FakeRunner()
    submitter = RecordingSubmitter()
    wd.Watchdog(
        tmp_db, price_source=FakeQuote("1.0"), submitter=submitter, standing_runner=runner
    ).tick()

    runner.scripted[st.CANCEL_ENDPOINT] = [ambiguous(), ambiguous(), ambiguous(), ambiguous()]
    report = wd.Watchdog(
        tmp_db, price_source=FakeQuote("0.5"), submitter=submitter, standing_runner=runner
    ).tick()

    assert submitter.calls, "an unconfirmed cancel must not prevent the sell"
    assert report.standing_contested > 0
    assert events_named(tmp_db, "standing_claimed")[-1]["clean"] is False


def test_an_exploding_mirror_never_stops_the_exit(tmp_db, enabled):
    from kaiba.execution import watchdog as wd

    make_position(tmp_db)
    runner = FakeRunner()
    submitter = RecordingSubmitter()
    wd.Watchdog(
        tmp_db, price_source=FakeQuote("1.0"), submitter=submitter, standing_runner=runner
    ).tick()
    runner.explode = True

    wd.Watchdog(
        tmp_db, price_source=FakeQuote("0.5"), submitter=submitter, standing_runner=runner
    ).tick()
    assert submitter.calls


def test_the_watchdog_mirrors_while_it_is_blind(tmp_db, enabled):
    """No price is exactly when a venue-side stop is the only stop there is."""
    from kaiba.execution import watchdog as wd

    make_position(tmp_db)
    runner = FakeRunner()
    wd.Watchdog(
        tmp_db,
        price_source=FakeQuote(None),
        submitter=RecordingSubmitter(),
        standing_runner=runner,
    ).tick()
    assert {r["tag"] for r in standing_rows(tmp_db, open_only=True)} == {"stop", "tp1", "tp2", "tp3"}


def test_the_heartbeat_carries_the_venue_protection_counters(tmp_db, enabled):
    from kaiba.execution import watchdog as wd

    make_position(tmp_db)
    wd.Watchdog(
        tmp_db,
        price_source=FakeQuote("1.0"),
        submitter=RecordingSubmitter(),
        standing_runner=FakeRunner(scripted={st.CREATE_ENDPOINT: [refused()] * 4}),
    ).tick()
    beat = events_named(tmp_db, "heartbeat")[0]
    assert beat["standing_unprotected"] == 1
