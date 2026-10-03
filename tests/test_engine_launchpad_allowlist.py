"""Live money enters only allowlisted launchpads on a chain; the rest are paper-traded.

OWNER-APPROVED 2026-10-01 (``docs/research/audit-20261001-strategy.md`` section 2). On all
89 Robinhood live fills:

    pons / pons_v2    n=42   mean  +1.0%   18 wins
    other launchpads  n=31   mean -23.7%    3 wins    (longxyz, bankr, lunch, ...)
    unknown           n=17   mean  -9.9%    4 wins

One-sided permutation p=0.024, and the split holds in both halves of the record.

**Measured on the scanned population before shipping**, as AGENTS.md requires (box,
read-only, 2026-10-01, RH ``sm-trenches`` signals since 09-23, launchpad as the ``tokens``
row would have read at decision time): 171 of 481 signals (35.6%) and 38 of 94 enters
(40.4%) are not on the allowlist -- 127 / 28 from other launchpads, 44 / 10 unknown. Of 312
signals on tokens later known to be pons, **0** were unknown at decision time and 2 (0
enters) carried a GMGN relabel (``uniswap_v4``). So "unknown is not live" costs no pons
entry, and it is the allowlist's own semantics: a launchpad nobody named is not on the list.

The evidence is on FILLS ONLY. What the skipped non-pons signals would have done is
unmeasured, which is why each one that would otherwise have entered is paper-traded as a
SHADOW twin through the ordinary paper broker and protection path: the twin's position is
the forward outcome, priced and exited by the same machinery as every shadow lane.
"""

from __future__ import annotations

import pytest

from kaiba.core.config import load_risk, save_risk
from kaiba.core.schemas import Action, Chain, Lane, LaneMode, Signal
from kaiba.execution import engine, lanes
from kaiba.execution.risk import RiskGate
from tests.test_paper import write_dossier

RH = Chain.ROBINHOOD
TOKEN = "0x" + "ab" * 20
#: 0.04 ETH: what the flat $120 cap sizes to on Robinhood at ~$3k/ETH.
SIZE = 40_000_000_000_000_000
ALLOW = {"robinhood": ["pons", "pons_v2"]}


class _Gate:
    """Isolate the engine from risk.py's arithmetic (covered by the risk tests)."""

    def check_entry(self, **kwargs):
        return None

    def position_size(self, chain, lane, score, conn=None, *, token=None):
        return SIZE


@pytest.fixture
def arm(monkeypatch):
    """sm-trenches in ``mode`` on every chain, with the allowlist param set as given."""

    def _arm(mode: LaneMode = LaneMode.LIVE, allow=ALLOW, *, chains=(RH, Chain.SOL, Chain.BSC)):
        cfg = load_risk()
        params = {k: v for k, v in dict(cfg.lane(Lane.SM_TRENCHES).params or {}).items()
                  if k != engine.LIVE_LAUNCHPADS_PARAM}
        if allow is not None:
            params[engine.LIVE_LAUNCHPADS_PARAM] = allow
        lane_cfg = cfg.lane(Lane.SM_TRENCHES).model_copy(
            update={"mode": mode, "chains": list(chains), "params": params})
        chain_cfgs = {
            ch: cfg.chain_budget(ch).model_copy(update={"enabled": True}) for ch in chains
        }
        cfg = cfg.model_copy(update={
            "global_mode": LaneMode.LIVE,
            "kill_switch": False,
            "entries_paused": False,
            "reduce_only": False,
            "bounds": cfg.bounds.model_copy(update={"max_lane_mode": LaneMode.LIVE}),
            "lanes": {**cfg.lanes, Lane.SM_TRENCHES: lane_cfg},
            "chains": {**cfg.chains, **chain_cfgs},
        })
        monkeypatch.setattr(engine, "get_risk", lambda: cfg)
        monkeypatch.setattr(engine, "RiskGate", _Gate)
        assert cfg.effective_mode(Lane.SM_TRENCHES) is mode, "fixture could not arm the lane"
        return cfg

    return _arm


def _token_row(conn, token: str = TOKEN, *, chain: Chain = RH, launchpad: str | None) -> None:
    # migrated_ms set: the paper broker prices a graduated token off the pool quote and
    # never goes looking for a curve, so no test here can reach a provider.
    conn.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, decimals, launchpad, migrated_ms, "
        "first_seen_ms) VALUES (?,?,?,?,?,?)",
        (chain.value, token, 18, launchpad, engine.now_ms() - 60_000, engine.now_ms()),
    )
    conn.commit()


def _signal(conn=None, token: str = TOKEN, *, chain: Chain = RH) -> Signal:
    ts = engine.now_ms() - 2_000
    sig = Signal(
        signal_id=lanes.signal_id_for(Lane.SM_TRENCHES, chain, token, ts, 1800),
        lane=Lane.SM_TRENCHES, chain=chain, token=token, strength=0.7,
        reasons=["4 smart wallets in the trenches preset (min 3)"], window_s=1800,
        created_ms=ts, payload={"smart_wallets": 4, "entity_count": 3},
    )
    if conn is not None:
        lanes.record(sig, conn)
    return sig


def _ready(conn, launchpad: str | None, token: str = TOKEN, chain: Chain = RH) -> None:
    write_dossier(conn, token=token, chain=chain, price="0.0001", liquidity="50000")
    _token_row(conn, token, chain=chain, launchpad=launchpad)


@pytest.fixture
def protectable(monkeypatch):
    monkeypatch.setattr(engine, "_protectability_probe", lambda chain, token: (True, "fixture"))


# ------------------------------------------------------------------ live: the allowlist


@pytest.mark.parametrize("launchpad", ["pons", "pons_v2", "PONS", " Pons_V2 "])
def test_a_pons_family_token_enters_live(tmp_db, arm, launchpad):
    arm()
    _ready(tmp_db, launchpad)
    d = engine.decide(_signal(), tmp_db)
    assert d.action is Action.ENTER and d.mode is LaneMode.LIVE, (d.blockers, d.thesis)
    assert d.size_base_units == SIZE
    assert not d.blockers


@pytest.mark.parametrize("launchpad", ["longxyz", "bankr", "lunch", "pool_uniswap_v4", "pons_v3"])
def test_any_other_launchpad_is_skipped_live_with_a_named_blocker(tmp_db, arm, launchpad):
    """An allowlist, not a blocklist: a launchpad nobody has measured is not live either."""
    arm()
    _ready(tmp_db, launchpad)
    d = engine.decide(_signal(), tmp_db)
    assert d.action is Action.SKIP and d.mode is LaneMode.LIVE
    assert d.blockers == [f"launchpad_not_live:{launchpad.strip().lower()}"]


@pytest.mark.parametrize("launchpad", [None, "", "   "])
def test_an_unknown_launchpad_is_not_live(tmp_db, arm, launchpad):
    arm()
    _ready(tmp_db, launchpad)
    d = engine.decide(_signal(), tmp_db)
    assert d.action is Action.SKIP
    assert d.blockers == ["launchpad_not_live:unknown"]


def test_a_token_with_no_tokens_row_is_unknown_and_not_live(tmp_db, arm):
    arm()
    write_dossier(tmp_db, token=TOKEN, chain=RH, price="0.0001", liquidity="50000")
    d = engine.decide(_signal(), tmp_db)
    assert d.blockers == ["launchpad_not_live:unknown"]


def test_canary_is_restricted_too(tmp_db, arm):
    arm(LaneMode.CANARY)
    _ready(tmp_db, "longxyz")
    d = engine.decide(_signal(), tmp_db)
    assert d.action is Action.SKIP and d.blockers == ["launchpad_not_live:longxyz"]


def test_the_skip_is_readable_off_the_decisions_table(tmp_db, arm):
    arm()
    _ready(tmp_db, "longxyz")
    d = engine.decide(_signal(), tmp_db)
    row = tmp_db.execute("SELECT mode, action, blockers_json FROM decisions WHERE decision_id=?",
                         (d.decision_id,)).fetchone()
    assert (row["mode"], row["action"]) == ("live", "skip")
    assert "launchpad_not_live:longxyz" in row["blockers_json"]


def test_the_rule_runs_after_every_other_gate(tmp_db, arm, monkeypatch):
    """A twin must be a trade we would really have taken: a risk refusal wins first."""
    arm()
    _ready(tmp_db, "longxyz")
    monkeypatch.setattr(engine, "_risk_refusal", lambda *a, **k: "daily_loss_stop")
    twins: list = []
    d = engine.decide(_signal(), tmp_db, twins=twins)
    assert d.blockers == ["daily_loss_stop"] and twins == []


# ------------------------------------------------------------------ shadow: the paper twin


def test_run_once_paper_trades_the_skipped_signal_as_a_shadow_twin(tmp_db, arm, protectable):
    arm()
    _ready(tmp_db, "longxyz")
    sig = _signal(tmp_db)
    [live] = engine.run_once(tmp_db)
    assert live.action is Action.SKIP and live.blockers == ["launchpad_not_live:longxyz"]

    twin_id = engine.shadow_twin_id(sig.signal_id)
    assert twin_id != live.decision_id
    assert twin_id in live.thesis, live.thesis
    twin = engine.load_decision(twin_id, tmp_db)
    assert twin is not None, "no shadow twin was recorded"
    assert (twin.mode, twin.action, twin.lane) == (LaneMode.SHADOW, Action.ENTER, Lane.SM_TRENCHES)
    assert twin.blockers == ["launchpad_not_live:longxyz"], "the twin must say why it is not live"
    assert twin.size_base_units == SIZE, "the twin is the trade we would have taken, same size"
    assert twin.signals == [sig.signal_id]

    pos = tmp_db.execute("SELECT * FROM positions").fetchone()
    assert pos is not None, "the twin opened no paper position: there is no forward outcome"
    assert (pos["mode"], pos["lane"], pos["chain"]) == ("shadow", "sm-trenches", "robinhood")
    assert int(pos["qty"]) > 0
    link = tmp_db.execute("SELECT outcome, position_id FROM decision_outcomes WHERE decision_id=?",
                          (twin_id,)).fetchone()
    assert link["outcome"] == "open" and link["position_id"] == pos["position_id"]

    live_orders = tmp_db.execute("SELECT COUNT(*) FROM orders WHERE mode != 'shadow'").fetchone()[0]
    assert live_orders == 0, "a skipped live signal must never leave an order for the executor"


def test_a_pons_entry_gets_no_twin(tmp_db, arm, protectable):
    arm()
    _ready(tmp_db, "pons")
    sig = _signal(tmp_db)
    twins: list = []
    d = engine.decide(sig, tmp_db, twins=twins)
    assert d.action is Action.ENTER and twins == []
    assert engine.load_decision(engine.shadow_twin_id(sig.signal_id), tmp_db) is None


def test_one_open_twin_per_token(tmp_db, arm, protectable):
    """A second signal on a token whose twin is still open must not pile more paper in."""
    arm()
    _ready(tmp_db, "bankr")
    first = _signal(tmp_db)
    engine.run_once(tmp_db)
    assert tmp_db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1

    # Same token, a later window: a distinct signal id (the lane buckets ids by window).
    sig2 = first.model_copy(update={"signal_id": first.signal_id + "_next",
                                    "created_ms": first.created_ms + 1_000})
    lanes.record(sig2, tmp_db)
    [d2] = engine.run_once(tmp_db)
    assert d2.blockers == ["launchpad_not_live:bankr"]
    assert "no paper twin" in d2.thesis and "already open" in d2.thesis, d2.thesis
    assert engine.load_decision(engine.shadow_twin_id(sig2.signal_id), tmp_db) is None
    assert tmp_db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1


def test_decide_alone_records_no_twin(tmp_db, arm):
    """Only a caller that will hand the twin off gets one; no dangling ENTER rows."""
    arm()
    _ready(tmp_db, "longxyz")
    sig = _signal()
    d = engine.decide(sig, tmp_db)
    assert d.action is Action.SKIP
    assert engine.load_decision(engine.shadow_twin_id(sig.signal_id), tmp_db) is None


def test_a_shadow_lane_is_untouched_and_records_as_before(tmp_db, arm):
    arm(LaneMode.SHADOW)
    _ready(tmp_db, "longxyz")
    twins: list = []
    d = engine.decide(_signal(), tmp_db, twins=twins)
    assert d.action is Action.ENTER and d.mode is LaneMode.SHADOW
    assert not d.blockers and twins == []


# ------------------------------------------------------------------ scope


@pytest.mark.parametrize("chain", [Chain.SOL, Chain.BSC])
def test_a_chain_absent_from_the_map_is_unrestricted(tmp_db, arm, chain):
    arm()
    token = "So1anaTokenAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" if chain is Chain.SOL else "0x" + "cd" * 20
    _ready(tmp_db, "longxyz", token=token, chain=chain)
    d = engine.decide(_signal(token=token, chain=chain), tmp_db)
    assert d.action is Action.ENTER and d.mode is LaneMode.LIVE, (d.blockers, d.thesis)


def test_no_param_means_no_restriction(tmp_db, arm):
    arm(allow=None)
    _ready(tmp_db, "longxyz")
    d = engine.decide(_signal(), tmp_db)
    assert d.action is Action.ENTER and d.mode is LaneMode.LIVE


@pytest.mark.parametrize("bad", [5, None, {"pons": True}])
def test_a_malformed_chain_entry_fails_closed(tmp_db, arm, bad):
    """A typo in the allowlist must not open the chain to every launchpad."""
    arm(allow={"robinhood": bad})
    _ready(tmp_db, "pons")
    d = engine.decide(_signal(), tmp_db)
    assert d.action is Action.SKIP and d.blockers == ["launchpad_not_live:pons"]


def test_a_malformed_map_fails_closed(tmp_db, arm):
    arm(allow=["pons", "pons_v2"])
    _ready(tmp_db, "pons")
    d = engine.decide(_signal(), tmp_db)
    assert d.action is Action.SKIP and d.blockers == ["launchpad_not_live:pons"]


def test_a_bare_string_is_one_launchpad_not_its_letters(tmp_db, arm):
    arm(allow={"robinhood": "pons"})
    _ready(tmp_db, "p")
    assert engine.decide(_signal(), tmp_db).blockers == ["launchpad_not_live:p"]


def test_the_param_survives_a_config_round_trip(tmp_path):
    """save_risk is what the self-tune writes with; a key it drops is a rule that vanishes."""
    cfg = load_risk()
    cfg.lanes[Lane.SM_TRENCHES].params[engine.LIVE_LAUNCHPADS_PARAM] = ALLOW
    path = save_risk(cfg, tmp_path / "risk.yaml")
    again = load_risk(path)
    assert again.lane(Lane.SM_TRENCHES).params[engine.LIVE_LAUNCHPADS_PARAM] == ALLOW


# ------------------------------------------------------------------ paper is not live exposure


def _position(conn, pid: str, mode: str, cost: int, token: str = TOKEN) -> None:
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty, "
        "qty_total, cost_native, proceeds_native) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (pid, RH.value, token, Lane.SM_TRENCHES.value, mode, engine.now_ms(), "1", "1",
         str(cost), "0"),
    )
    conn.commit()


def test_a_paper_position_is_not_live_exposure(tmp_db):
    """Without this a twin shrinks or refuses the live pons entries it exists to protect.

    ``open_exposure`` feeds the total-exposure cap, the per-token cap and ``free`` in the
    bankroll reading. On the box RH ran at ~62% of a 70% cap when this was written, so a
    single 0.04 ETH paper twin would have refused the next live entry. Same rule as
    ``realized_today``: a shadow position is not money.
    """
    gate = RiskGate()
    _position(tmp_db, "pos_live", "live", 30_000)
    _position(tmp_db, "pos_paper", "shadow", 1_000_000)
    assert gate.open_exposure(RH, tmp_db) == 30_000
    assert gate.open_exposure(RH, tmp_db, token=TOKEN) == 30_000
