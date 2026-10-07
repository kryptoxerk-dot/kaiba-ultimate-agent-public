"""The executor's ``from_wallet`` override: a MANUAL SELL from an owned wallet, and nothing else.

Added 2026-10-03 for copy_manager, which sells the owner's GMGN copy trades on his own
wallet (0x7243...) while Kaiba trades from the chain wallet in risk.yaml (0xcc44...). Every
refusal below happens BEFORE an order row exists or the CLI is spawned, and a fill sold from
the other wallet never moves Kaiba's ledger. All addresses and orders are fixtures.
"""

from __future__ import annotations

import pytest
import yaml

from kaiba.core.config import load_risk
from kaiba.core.schemas import Chain, Lane, LaneMode, OrderState, Side
from kaiba.execution import executor
from kaiba.execution.policy import load_policy

RH = Chain.ROBINHOOD
KAIBA = "0xcc4450c80735778e9f5eaa7a4ea47990e57807c2"
OWNER = "0x72430877378522d1b759ac721561aa9eb9e25c2b"
STRANGER = "0x1111111111111111111111111111111111111111"
TOKEN = "0x96c59a1883aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture
def rh_live(tmp_path, monkeypatch):
    risk = load_risk()
    risk.kill_switch = False
    risk.entries_paused = False
    risk.reduce_only = False
    risk.chains[RH].wallet = KAIBA
    monkeypatch.setattr(executor, "get_risk", lambda: risk)
    policy = load_policy().model_copy(update={"owned_addresses": {"robinhood": [OWNER.upper().replace("0X", "0x"), KAIBA]}})
    path = tmp_path / "signer-policy.yaml"
    path.write_text(yaml.safe_dump(policy.model_dump(mode="json")))
    monkeypatch.setenv("KAIBA_SIGNER_POLICY_PATH", str(path))
    sent: list[list[str]] = []

    def cli(args, timeout_s=45, *, mutating=False):
        sent.append(list(args))
        if args[0] == "order":
            return {"data": {"status": "confirmed", "hash": "0xfixture",
                             "report": {"input_amount": "1000", "output_amount": "777"}}}
        return {"data": {"order_id": f"fixture-{len(sent)}", "tx_hash": None}}

    monkeypatch.setattr(executor, "_run_gmgn", cli)
    return risk, sent


def order(side=Side.SELL, lane=Lane.MANUAL, amount=1000):
    return executor.build_order(
        decision_id=None, chain=RH, token=TOKEN, side=side, lane=lane, mode=LaneMode.LIVE,
        amount_in=amount, min_out=1, slippage_bps=300,
    )


def _from(args: list[str]) -> str:
    return args[args.index("--from") + 1]


def _rows(conn, order_id):
    return conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchall()


def test_a_sell_from_another_wallet_is_refused_because_kaiba_never_bought_there(tmp_db, rh_live):
    """OWNER RULE 2026-10-05: never sell anything Kaiba did not buy. Before that date this test
    asserted the opposite (copy_manager sold the owner's copy trades on his wallet)."""
    _, sent = rh_live
    o = order()
    with pytest.raises(executor.ExecutionRefused, match=executor.NEVER_SELL_UNBOUGHT):
        executor.submit(o, tmp_db, from_wallet=OWNER)
    assert sent == [] and _rows(tmp_db, o.order_id) == [], "refused before an order row exists"


def test_without_the_override_every_order_keeps_the_chain_wallet(tmp_db, rh_live, kaiba_bought):
    """Positive control: a sell of what Kaiba bought goes from Kaiba's own wallet."""
    _, sent = rh_live
    o = order(amount=2000)
    kaiba_bought(tmp_db, RH, TOKEN, qty=10_000)
    executor.submit(o, tmp_db)
    assert _from(sent[-1]) == KAIBA
    assert "from_wallet" not in str(tmp_db.execute(
        "SELECT group_concat(detail) AS d FROM order_events WHERE order_id=?", (o.order_id,)).fetchone()["d"])


def test_a_buy_may_never_name_another_wallet(tmp_db, rh_live):
    _, sent = rh_live
    o = order(side=Side.BUY)
    with pytest.raises(executor.ExecutionRefused, match="sell-only"):
        executor.submit(o, tmp_db, from_wallet=OWNER)
    with pytest.raises(executor.ExecutionRefused, match="sell-only"):
        executor.submit_gmgn(o, tmp_db, from_wallet=OWNER)
    assert sent == [] and _rows(tmp_db, o.order_id) == [], "refused before an order row exists"


def test_a_strategy_lane_may_not_redirect_its_sells(tmp_db, rh_live):
    _, sent = rh_live
    o = order(lane=Lane.SM_TRENCHES)
    with pytest.raises(executor.ExecutionRefused, match="lane manual only"):
        executor.submit(o, tmp_db, from_wallet=OWNER)
    assert sent == [] and _rows(tmp_db, o.order_id) == []


def test_a_wallet_the_operator_did_not_declare_is_refused(tmp_db, rh_live):
    _, sent = rh_live
    o = order()
    with pytest.raises(executor.ExecutionRefused, match="owned_addresses"):
        executor.submit(o, tmp_db, from_wallet=STRANGER)
    with pytest.raises(executor.ExecutionRefused, match="owned_addresses"):
        executor.submit(o, tmp_db, from_wallet="")
    assert sent == [] and _rows(tmp_db, o.order_id) == []


def test_the_direct_lane_cannot_take_the_override(tmp_db, rh_live):
    o = order().model_copy(update={"provider": "direct"})
    with pytest.raises(executor.ExecutionRefused, match="GMGN lane"):
        executor.submit(o, tmp_db, from_wallet=OWNER)


def test_only_kaibas_own_sell_is_sent_and_applied(tmp_db, rh_live, monkeypatch, kaiba_bought):
    """The owner's sell is refused outright; Kaiba's sell of its own buy fills and is applied."""
    from kaiba.execution import accounting

    applied: list[str] = []
    monkeypatch.setattr(accounting, "apply_fill", lambda o, c=None, **kw: applied.append(o.order_id))
    kaiba_bought(tmp_db, RH, TOKEN, qty=10_000)
    theirs, ours = order(amount=3000), order(amount=4000)
    with pytest.raises(executor.ExecutionRefused, match=executor.NEVER_SELL_UNBOUGHT):
        executor.submit(theirs, tmp_db, from_wallet=OWNER)
    executor.submit(ours, tmp_db)
    assert executor.reconcile(ours.order_id, tmp_db) is OrderState.FILLED
    assert applied == [ours.order_id]
