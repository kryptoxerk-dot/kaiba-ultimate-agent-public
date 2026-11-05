"""The paper runner: its reads, its delays, its refusals, and that it cannot send anything."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from kaiba.execution import lp_model as m
from kaiba.execution import lp_paper as lp
from kaiba.learning import labs

SOL = lp.SOL_MINT
ARM = m.Arm("t", 4, 10, 6.0, 2.0, 60)


def bin_row(b: int) -> dict:
    return {"id": b, "amounts": [0, 10**15], "price_q64": int(m.bin_price(b, 100) * 2**64),
            "supply": 10**15 << 64, "fee_growth": [0, 0]}


class Fake:
    def __init__(self) -> None:
        self.t = 1_000.0
        self.active = 0
        self.pools = [self.pool("P1")]
        self.fail_rpc = False
        self.missing: set[int] = set()
        self.mint: dict = {"freezeAuthority": None, "extensions": []}

    def pool(self, address: str) -> dict:
        return {"address": address, "name": "X-SOL", "token_y": {"address": SOL},
                "token_x": {"address": "M1", "freeze_authority_disabled": True}, "tvl": 9_000,
                "pool_config": {"bin_step": 100, "base_fee_pct": 2.0}, "dynamic_fee_pct": 1.0,
                "volume": {"30m": 20_000}, "fee_tvl_ratio": {"30m": 1.0}, "created_at": 0}

    def api(self, path: str) -> dict:
        return {"data": self.pools}

    def rpc(self, method: str, params: list) -> dict:
        if self.fail_rpc:
            raise RuntimeError("rpc down")
        if method == "getAccountInfo":
            return {"value": {"data": {"parsed": {"info": self.mint}}}}
        keys = params[0]
        return {"context": {"slot": int(self.t)},
                "value": [{"pool": True} if not str(k).startswith("A:") else
                          (None if int(k[2:]) in self.missing else {"index": int(k[2:])}) for k in keys]}

    def now(self) -> float:
        return self.t


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Fake:
    f = Fake()
    monkeypatch.setattr(lp, "bin_array_address", lambda pool, i: f"A:{i}")
    monkeypatch.setattr(lp, "decode_pool", lambda a: {"active_bin": f.active, "bin_step": 100, "status": 0,
                                                      "mints": ["M1", SOL]})
    monkeypatch.setattr(lp, "decode_bins", lambda a: {"pool": "P1", "index": a["index"], "bins": [
        bin_row(a["index"] * 70 + i) for i in range(70)]})
    return f


def bot(f: Fake, tmp_path: Path, arms: tuple[m.Arm, ...] = (ARM,)) -> lp.Bot:
    return lp.Bot(f, tmp_path, now=f.now, profile=replace(lp.FAST, arms=arms, max_open=1))


def advance(b: lp.Bot, f: Fake, steps: int, dt: float = 5.0) -> None:
    for _ in range(steps):
        f.t += dt
        b.cycle()


def results(b: lp.Bot) -> list[tuple]:
    return b.db.execute("SELECT status, exit_reason, hold_s, net_pct FROM arm_results").fetchall()


def test_nothing_in_the_bot_can_sign_or_send() -> None:
    for module in (lp, m):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for word in ("sendTransaction", "sendRawTransaction", "Keypair", "solders", "private_key", "gmgn"):
            assert word not in source, word


def test_it_refuses_a_host_protection_uses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAIBA_LP_RPC_URL", "https://rpc.example.org/x")
    assert lp.rpc_url(["other.example.org"]) == "https://rpc.example.org/x"
    with pytest.raises(SystemExit, match="shared_with_protection"):
        lp.rpc_url(["RPC.example.org"])


def test_a_position_lands_and_closes_on_later_reads_not_on_the_one_that_prompted_it(fake: Fake, tmp_path: Path) -> None:
    b = bot(fake, tmp_path)
    b.cycle()  # picks and plans
    obs = b.open[0]
    run = obs.runs[0]
    assert run.status == "planned"
    fake.t += 1
    b.cycle()
    assert run.status == "planned"  # 1 s later is before LAND_S
    fake.t += 3
    b.cycle()
    assert run.status == "open"
    advance(b, fake, 2, dt=6.0)  # max_hold is decided here, 12 s after opening
    assert run.status == "closing" and not results(b)
    fake.t += 1
    b.cycle()
    assert not results(b)  # a close decided 1 s ago has not landed
    fake.t += 3
    b.cycle()
    assert results(b)[0][:2] == ("closed", "max_hold")
    assert b.open == []


def test_a_price_fall_before_landing_is_recorded_as_a_paid_miss(fake: Fake, tmp_path: Path) -> None:
    b = bot(fake, tmp_path)
    b.cycle()
    fake.active = -2
    advance(b, fake, 1)
    status, _, _, net = results(b)[0]
    assert status == "missed"
    assert net == pytest.approx(-100 * m.TX_FEE_LAMPORTS / lp.TICKET)


def test_an_empty_or_refused_market_shows_its_reasons(fake: Fake, tmp_path: Path) -> None:
    fake.pools = [{**fake.pool("P1"), "tvl": 1}]
    b = bot(fake, tmp_path)
    b.cycle()
    assert b.open == []
    assert b.last_reasons == {"listed": 1, "tvl": 1}
    b.last_status = -1e9
    b.housekeeping()
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["last_selection"]["tvl"] == 1 and status["open"] == 0


@pytest.mark.parametrize("mint, why", [
    ({"freezeAuthority": "someone", "extensions": []}, "freeze_authority"),
    ({"extensions": [{"extension": "transferHook", "state": {"programId": "p"}}]}, "transfer_hook"),
    ({"extensions": [{"extension": "transferFeeConfig", "state": {
        "olderTransferFee": {"transferFeeBasisPoints": 0}, "newerTransferFee": {"transferFeeBasisPoints": 100}}}]},
     "transfer_fee"),
])
def test_an_unholdable_token_is_refused(fake: Fake, tmp_path: Path, mint: dict, why: str) -> None:
    fake.mint = mint
    b = bot(fake, tmp_path)
    b.cycle()
    assert b.open == [] and b.last_reasons[why] == 1


def test_a_zero_fee_extension_and_an_unused_hook_are_not_refused(fake: Fake, tmp_path: Path) -> None:
    fake.mint = {"extensions": [
        {"extension": "transferHook", "state": {"programId": None}},
        {"extension": "transferFeeConfig", "state": {"olderTransferFee": {"transferFeeBasisPoints": 0},
                                                     "newerTransferFee": {"transferFeeBasisPoints": 0}}}]}
    b = bot(fake, tmp_path)
    b.cycle()
    assert len(b.open) == 1


def test_an_unreadable_mint_refuses_rather_than_admits(fake: Fake, tmp_path: Path) -> None:
    b = bot(fake, tmp_path)
    fake.rpc = lambda method, params: {"value": None}  # type: ignore[method-assign]
    b.cycle()
    assert b.open == [] and b.last_reasons["mint_unreadable"] == 1


def test_a_blind_stretch_is_excluded_and_counted_never_scored(fake: Fake, tmp_path: Path) -> None:
    b = bot(fake, tmp_path)
    b.cycle()
    advance(b, fake, 1)
    fake.fail_rpc = True
    advance(b, fake, 8)
    assert results(b)[0][0] == "stale" and results(b)[0][3] is None
    assert b.open == []
    assert lp.report(tmp_path)["arms"]["t"]["n"] == 0
    assert lp.report(tmp_path)["arms"]["t"]["stale_excluded"] == 1


def test_a_dead_api_is_counted_and_does_not_stop_the_loop(fake: Fake, tmp_path: Path) -> None:
    b = bot(fake, tmp_path)
    fake.api = lambda path: (_ for _ in ()).throw(RuntimeError("api down"))  # type: ignore[method-assign]
    b.cycle()
    assert b.counts["select_failed"] == 1


def seed(root: Path, nets: list[float], days: int = 6, arm: str = lp.FAST.primary) -> None:
    b = lp.Bot(Fake(), root)
    for i, net in enumerate(nets):
        at = 1_790_000_000_000 + (i * days * 86_400_000) // len(nets)
        b.db.execute("INSERT INTO arm_results(obs_id, arm, status, at_ms, net_pct, net_sol, fee_pct, inventory_pct,"
                     " exit_cost_pct, tx_sol, rent_sol) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (i, arm, "closed", at, net, net / 100, 1.0, 0.0, 0.0, 0.0, 0.0))
    b.db.commit()
    b.db.close()


def test_a_smaller_ticket_keeps_its_percentage_but_not_its_fixed_costs() -> None:
    row = {"net_pct": 1.0 - 0.5, "tx_sol": 0.005, "rent_sol": 0.0}  # 1% gross on 1 SOL, less 0.005 SOL
    assert lp.at_ticket([row], 1.0, 1.0) == pytest.approx(0.005)
    assert lp.at_ticket([row], 0.5, 1.0) == pytest.approx(0.0)
    assert lp.at_ticket([row], 0.25, 1.0) == pytest.approx(-0.002, abs=0.001)


def test_the_pass_line_needs_every_check(tmp_path: Path) -> None:
    good = [2.0, -1.0] * 200
    seed(tmp_path / "a", good)
    assert lp.report(tmp_path / "a")["verdict"]["pass"] is True
    seed(tmp_path / "b", good[:100])
    assert lp.report(tmp_path / "b")["verdict"]["checks"]["n"] is False
    seed(tmp_path / "c", good, days=3)
    assert lp.report(tmp_path / "c")["verdict"]["checks"]["days"] is False
    seed(tmp_path / "d", [0.1, 0.0] * 200)
    assert lp.report(tmp_path / "d")["verdict"]["checks"]["mean"] is False
    seed(tmp_path / "e", [3.0] * 200 + [-1.0] * 200)
    assert lp.report(tmp_path / "e")["verdict"]["checks"]["halves"] is False


def test_the_daily_line_shows_a_stopped_bot_and_a_failing_verdict(tmp_path: Path) -> None:
    root = tmp_path / "labs" / "lp_paper"
    seed(root, [0.1, -0.2] * 50)
    (root / "status.json").write_text(json.dumps({"at_ms": 1}), encoding="utf-8")
    view = {"lp": {"available": False}, "wallet": {"available": True, "age_s": 1, "stale": False,
                                                   "summary": {"class_counts": {}, "profiled": 0, "tracked": 0, "classed": 0},
                                                   "verdict": {"available": False, "reason": "x"}},
            "lp_bot": labs.lp_bot(tmp_path)}
    line = labs.render_lines(view)[-1]
    assert line.startswith("LABS LP bot fast (paper,") and "STALE" in line
    assert "n 100" in line and "not passing: n, mean, lower95, halves" in line


def test_a_bot_that_never_ran_is_not_a_quiet_one(tmp_path: Path) -> None:
    assert labs.lp_bot(tmp_path) == {"available": False, "reason": "LP bot has not run"}


def test_the_slow_profile_keeps_its_own_store_and_its_own_pass_line(tmp_path: Path) -> None:
    assert lp.SLOW.root(tmp_path / "lp_paper") == tmp_path / "lp_paper_slow"
    assert lp.FAST.root(tmp_path / "lp_paper") == tmp_path / "lp_paper"
    root = tmp_path / "lp_paper_slow"
    seed(root, [2.0, -0.5] * 60, days=8, arm=lp.SLOW.primary)
    verdict = lp.report(root, lp.SLOW)["verdict"]
    assert verdict["arm"] == "w30_6h" and verdict["pass"] is True
    assert lp.report(root, replace(lp.SLOW, pass_min_n=500))["verdict"]["pass"] is False


def test_a_blind_stretch_scales_with_the_poll() -> None:
    assert lp.FAST.stale_s == 30.0 and lp.SLOW.stale_s == 180.0


def test_a_two_sided_arm_opens_centred_and_pays_for_its_swap(fake: Fake, tmp_path: Path) -> None:
    arm = m.Arm("w", 10, 30, 15.0, 6.0, 600, two_sided=10)
    b = bot(fake, tmp_path, (arm,))
    b.cycle()
    fake.active = -3  # a two-sided position centres on where it lands, so it cannot miss
    advance(b, fake, 1)
    plan = b.open[0].runs[0].plan
    assert b.open[0].runs[0].status == "open" and (plan.lo, plan.hi) == (-13, 7)
    assert plan.extra_txs == 1


def test_the_report_splits_by_pool_age_and_counts_unknown_ages() -> None:
    rows = [{"obs_id": i, "net_pct": n, "rent_sol": r}
            for i, n, r in ((1, 2.0, 0.07), (2, 4.0, 0.0), (3, -1.0, 0.0), (4, 9.0, 0.0))]
    out = lp.by_pool_age(rows, {1: 0.4, 2: 0.9, 3: 30.0, 4: None})
    assert out["under_1h"] == {"n": 2, "mean_net_pct": 3.0, "rent_paid_share": 0.5}
    assert out["over_1d"]["n"] == 1 and "1_to_24h" not in out
    assert out["unknown_age"] == 1


class DumpFake(Fake):
    def __init__(self, candles: list[dict]) -> None:
        super().__init__()
        self.candles, self.history_calls = candles, 0

    def api(self, path: str) -> dict:
        if "/ohlcv" in path:
            self.history_calls += 1
            return {"data": self.candles}
        return {"data": self.pools}


RUN = [{"high": h, "low": lo, "close": c, "volume": v} for h, lo, c, v in (
    (1.2, 1.0, 1.1, 100), (4.0, 1.1, 3.5, 900), (10.0, 3.5, 9.0, 1000), (9.0, 5.0, 5.5, 800), (5.5, 3.5, 3.8, 300),
    (3.9, 3.2, 3.4, 200), (3.6, 3.1, 3.3, 150))]


def dump_bot(f: Fake, tmp_path: Path) -> lp.Bot:
    return lp.Bot(f, tmp_path, now=f.now, profile=replace(lp.DUMP, max_open=1))


def test_the_dump_profile_opens_only_on_the_setup_and_records_its_measures(fake: Fake, tmp_path: Path) -> None:
    f = DumpFake(RUN)
    b = dump_bot(f, tmp_path)
    b.select()
    assert len(b.open) == 1
    features = json.loads(b.db.execute("SELECT features_json FROM observations").fetchone()[0])
    assert features["drop"] == 0.67 and features["run_x"] == 10.0


def test_a_pool_that_is_not_the_setup_waits_before_it_is_read_again(fake: Fake, tmp_path: Path) -> None:
    f = DumpFake(RUN[:3])
    b = dump_bot(f, tmp_path)
    b.select()
    b.select()
    assert b.open == [] and f.history_calls == 1 and b.last_reasons["setup_recheck_later"] == 1
    f.t += lp.DUMP_RECHECK_S + 1
    b.select()
    assert f.history_calls == 2


def test_history_reads_are_rationed_per_pass(fake: Fake, tmp_path: Path) -> None:
    f = DumpFake(RUN[:3])
    f.pools = [f.pool(f"P{i}") for i in range(lp.DUMP_CHECKS_PER_SELECT + 5)]
    b = dump_bot(f, tmp_path)
    b.select()
    assert f.history_calls == lp.DUMP_CHECKS_PER_SELECT and b.last_reasons["setup_budget"] == 5


def test_a_fine_bin_step_is_refused_before_any_history_read(fake: Fake, tmp_path: Path) -> None:
    f = DumpFake(RUN)
    f.pools = [{**f.pool("P1"), "pool_config": {"bin_step": 20, "base_fee_pct": 1.0}}]
    b = dump_bot(f, tmp_path)
    b.select()
    assert b.open == [] and f.history_calls == 0 and b.last_reasons["bin_step_too_fine"] == 1


def test_the_dump_profile_keeps_its_own_store_and_pass_line(tmp_path: Path) -> None:
    assert lp.DUMP.root(tmp_path / "lp_paper") == tmp_path / "lp_paper_dump"
    assert (lp.DUMP.primary, lp.DUMP.pass_min_n, lp.DUMP.pass_min_days) == ("d2x_24h", 60, 10)



HOUR = [{"close": 1.0}, {"close": 0.95}]


class HourFake(Fake):
    def __init__(self, candles: list[dict]) -> None:
        super().__init__()
        self.candles = candles
        self.history_calls = 0
        self.t = 100_000.0
        self.pools = [self.pool("P1")]

    def pool(self, address: str) -> dict:
        return {**super().pool(address), "pool_config": {"bin_step": 200, "base_fee_pct": 2.0}, "created_at": 1}

    def api(self, path: str) -> dict:
        if "/ohlcv" in path:
            self.history_calls += 1
            return {"data": self.candles}
        return {"data": self.pools}


def hour_bot(f: Fake, tmp_path: Path) -> lp.Bot:
    return lp.Bot(f, tmp_path, now=f.now, profile=replace(lp.FILTERED, max_open=1))


def test_the_filtered_track_opens_a_calm_high_step_pool_and_records_the_move(fake: Fake, tmp_path: Path) -> None:
    b = hour_bot(HourFake(HOUR), tmp_path)
    b.select()
    assert len(b.open) == 1
    assert json.loads(b.db.execute("SELECT features_json FROM observations").fetchone()[0])["prior_1h"] == -0.05


def test_the_filtered_track_will_not_enter_after_a_fall(fake: Fake, tmp_path: Path) -> None:
    b = hour_bot(HourFake([{"close": 1.0}, {"close": 0.8}]), tmp_path)
    b.select()
    assert b.open == [] and b.last_reasons["dumped_last_hour"] == 1


def test_the_filtered_track_refuses_a_pool_with_no_history(fake: Fake, tmp_path: Path) -> None:
    b = hour_bot(HourFake([]), tmp_path)
    b.select()
    assert b.open == [] and b.last_reasons["setup_short_history"] == 1


def test_the_filtered_track_skips_the_bad_step_before_any_history_read(fake: Fake, tmp_path: Path) -> None:
    f = HourFake(HOUR)
    f.pools = [{**f.pool("P1"), "pool_config": {"bin_step": 100, "base_fee_pct": 2.0}}]
    b = hour_bot(f, tmp_path)
    b.select()
    assert b.open == [] and f.history_calls == 0 and b.last_reasons["bin_step_skipped"] == 1


def test_hour_checks_are_rationed_per_pass(fake: Fake, tmp_path: Path) -> None:
    f = HourFake([{"close": 1.0}, {"close": 0.5}])
    f.pools = [f.pool(f"P{i}") for i in range(lp.HOUR_CHECKS_PER_SELECT + 4)]
    b = hour_bot(f, tmp_path)
    b.select()
    assert f.history_calls == lp.HOUR_CHECKS_PER_SELECT and b.last_reasons["setup_budget"] == 4


def test_the_filtered_track_is_sol_only_with_no_re_open() -> None:
    for arm in lp.FILTERED.arms:
        assert (arm.two_sided, arm.reopen_width, arm.range_x) == (0, 0, 0.0)
    assert lp.FILTERED.primary in {a.name for a in lp.FILTERED.arms}


def test_the_filtered_primary_is_the_slow_tracks_one_sided_shape() -> None:
    primary = next(a for a in lp.FILTERED.arms if a.name == lp.FILTERED.primary)
    slow = next(a for a in lp.SLOW.arms if a.name == "b40_6h")
    assert replace(primary, name="b40_6h") == slow


def test_the_filtered_track_keeps_its_own_store_and_pass_line(tmp_path: Path) -> None:
    assert lp.FILTERED.root(tmp_path / "lp_paper") == tmp_path / "lp_paper_filtered"
    assert (lp.FILTERED.primary, lp.FILTERED.pass_min_n, lp.FILTERED.pass_min_days, lp.FILTERED.pass_min_mean_pct) == ("f40_6h", 100, 7, 0.50)


def test_the_registered_tracks_did_not_change() -> None:
    for profile in (lp.FAST, lp.SLOW):
        assert profile.selector == m.SelectorConfig() and not profile.hour_check
    assert lp.DUMP.selector.skip_bin_steps is None and lp.DUMP.selector.min_age_h == 0.0 and not lp.DUMP.hour_check
    assert (lp.FAST.primary, lp.SLOW.primary, lp.DUMP.primary) == ("r20_60", "w30_6h", "d2x_24h")


@pytest.mark.parametrize(("volume", "fee", "why"), [(1_000, 0.10, None), (999, 0.10, "volume"), (1_000, 0.09, "fee_tvl"), (400_000, 0.30, None)])
def test_the_filtered_track_admits_calm_pools_but_not_dead_ones(volume: float, fee: float, why: str | None) -> None:
    pool = {"token_y": {"address": SOL}, "token_x": {"freeze_authority_disabled": True}, "tvl": 9_000, "created_at": 1,
            "pool_config": {"bin_step": 80, "base_fee_pct": 1.0}, "volume": {"30m": volume}, "fee_tvl_ratio": {"30m": fee}}
    assert m.eligible(pool, lp.FILTERED_SELECTOR, SOL, 7_200_000) == why

