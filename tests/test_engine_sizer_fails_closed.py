"""A sizer that cannot run refuses. It never guesses.

FOUND 2026-09-22 by an adversarial verifier, on a funded box with a live lane filling.

``kaiba/execution/risk.py`` exports no module-level ``position_size`` (``hasattr`` is
False), so ``engine``'s ``except ImportError`` branch is PERMANENTLY taken and the name
``engine.position_size`` was its own stub. ``_size_for`` caught every exception from
``RiskGate.position_size`` and fell through to that stub, which applied none of the
controls -- no live equity, no daily loss stop, no ``max_total_exposure_pct``, no
viability band, no free-balance check, no concentration -- and which RAISED a
below-minimum size up to ``min_position_base_units``, turning a refusal into an order.

One unexpected exception inside the real sizer was therefore an unchecked live position.
"""

from __future__ import annotations

import pytest

from kaiba.core.config import load_risk, save_risk
from kaiba.core.schemas import Action, Chain, Lane, LaneMode, Signal
from kaiba.execution import engine, lanes

TOKEN = "GMFCWQv8CfnjGR2xCJebyxqSsCmyMo31pXcCEnfDpump"


@pytest.fixture
def armed(tmp_path, monkeypatch):
    cfg = load_risk()
    cfg.global_mode = LaneMode.LIVE
    cfg.kill_switch = False
    cfg.lanes[Lane.SM_TRENCHES].mode = LaneMode.LIVE
    path = tmp_path / "risk.yaml"
    save_risk(cfg, path)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))
    return path


class Exploding:
    """A risk gate that raises where it used to be silently routed around."""

    exc: type[Exception] = RuntimeError

    def __init__(self, *a, **k) -> None:
        pass

    def position_size(self, *a, **k):
        raise self.exc("the sizer blew up")

    def check_entry(self, **k):
        return None


def _signal() -> Signal:
    ts = engine.now_ms()
    return Signal(
        signal_id=lanes.signal_id_for(Lane.SM_TRENCHES, Chain.SOL, TOKEN, ts, 300),
        lane=Lane.SM_TRENCHES, chain=Chain.SOL, token=TOKEN, strength=0.99,
        reasons=["smart wallets"], window_s=300, created_ms=ts, payload={},
    )


def test_the_import_fallback_branch_really_is_the_live_one():
    """The premise of the bug: risk.py exports no module-level position_size."""
    import kaiba.execution.risk as risk

    assert not hasattr(risk, "position_size"), (
        "risk.py now exports position_size; re-check which function engine falls back to"
    )
    assert engine.position_size.__module__ == "kaiba.execution.engine"


def test_the_fallback_stub_refuses_instead_of_sizing(tmp_db, armed):
    """Whatever it is handed, it returns 0. It is not a sizer."""
    for score in (0.0, 50.0, 99.0, 1e9):
        assert engine.position_size(Chain.SOL, Lane.SM_TRENCHES, score, tmp_db) == 0


def test_a_raising_risk_gate_produces_no_size(tmp_db, armed, monkeypatch):
    monkeypatch.setattr(engine, "RiskGate", Exploding)
    assert engine._size_for(_signal(), tmp_db) == 0


def test_a_raising_gate_makes_the_engine_skip_not_enter(tmp_db, armed, monkeypatch):
    """End to end: no ENTER decision, therefore no order."""
    monkeypatch.setattr(engine, "RiskGate", Exploding)
    decision = engine.decide(_signal(), tmp_db)
    assert decision.action is Action.SKIP
    assert decision.size_base_units in (None, 0)


def test_an_absent_risk_gate_produces_no_size(tmp_db, armed, monkeypatch):
    monkeypatch.setattr(engine, "RiskGate", None)
    assert engine._size_for(_signal(), tmp_db) == 0


def test_the_refusal_is_labelled_for_the_operator(tmp_db, armed, monkeypatch):
    """A silent zero is how several of these hid all day. Name the cause."""
    import kaiba.execution.risk as risk

    class Valuey(Exploding):
        exc = ValueError

    monkeypatch.setattr(engine, "RiskGate", Valuey)
    engine._size_for(_signal(), tmp_db)
    cause = risk.zero_size_cause(tmp_db, Chain.SOL, Lane.SM_TRENCHES, TOKEN)
    assert cause is not None and "sizer_raised:ValueError" in cause, cause


def test_size_for_never_consults_the_module_level_sizer(tmp_db, armed, monkeypatch):
    """Both layers refuse, so pin the INTENT rather than relying on the second one.

    If the module-level fallback is ever reachable again it will be by someone restoring
    a real implementation to it. Make that loud: here it is patched to hand back a large
    size, and ``_size_for`` must still refuse, because it does not call it at all.
    """
    called: list[str] = []

    def loud(*a, **k):
        called.append("module-level sizer was consulted")
        return 999_999_999_999

    monkeypatch.setattr(engine, "position_size", loud)

    monkeypatch.setattr(engine, "RiskGate", None)
    assert engine._size_for(_signal(), tmp_db) == 0, "an absent gate must refuse, not fall through"

    monkeypatch.setattr(engine, "RiskGate", Exploding)
    assert engine._size_for(_signal(), tmp_db) == 0, "a raising gate must refuse, not fall through"

    assert called == [], called
