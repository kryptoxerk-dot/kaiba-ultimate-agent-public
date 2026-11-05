"""The signer: policy before signature, and no way to argue with it."""

from __future__ import annotations

import inspect
import os
from dataclasses import FrozenInstanceError

import pytest

from kaiba.core.schemas import Chain
from kaiba.execution import signer
from tests.signer_fixtures import compile_message, new_pubkey, system_transfer, unsigned_tx


@pytest.fixture(autouse=True)
def _isolated_replay_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("KAIBA_SIGNER_STATE", str(tmp_path / "signer-state"))


def make_req(chain: Chain = Chain.SOL) -> signer.SignRequest:
    return signer.SignRequest(
        chain=chain, order_id="ord:1", payload={"transaction": "AAA="}, wallet="wallet1"
    )


# ---------------------------------------------------------------- the refusal is absolute


def test_a_policy_refusal_produces_no_signature(tmp_db, monkeypatch):
    monkeypatch.setattr(signer, "_judge", lambda req: (None, False, "transfer to non-owned", ["spl_transfer"]))
    resp = signer.sign_request(make_req())
    assert resp.ok is False
    assert resp.signature is None
    assert "non-owned" in resp.reason


def test_a_refusal_is_recorded_as_an_event(tmp_db, monkeypatch):
    from kaiba.core import events as ev
    from kaiba.core.schemas import EventKind

    monkeypatch.setattr(signer, "_judge", lambda req: (None, False, "nope", []))
    signer.sign_request(make_req())
    kinds = [e.kind for e in ev.recent(conn=tmp_db)]
    assert EventKind.RISK_HALT.value in kinds


def test_policy_runs_before_the_keystore_is_touched(tmp_db, monkeypatch):
    """A refused request must never even load a key."""
    monkeypatch.setattr(signer, "_judge", lambda req: (None, False, "refused", []))

    class Exploding(signer.Keystore):
        def load(self, chain, wallet):
            raise AssertionError("keystore must not be reached on a refusal")

    assert signer.sign_request(make_req(), Exploding()).ok is False


def test_missing_policy_module_is_a_refusal_not_a_bypass(tmp_db, monkeypatch):
    import builtins

    real = builtins.__import__

    def blocked(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "kaiba.execution" and "policy" in (fromlist or ()):
            raise ImportError("not deployed")
        return real(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocked)
    allowed, reason, _ = signer._evaluate(make_req())
    assert allowed is False and "policy" in reason


def test_bytes_that_do_not_decode_are_refused(tmp_db):
    """The signer judges the bytes it signs (2026-10-02); bytes it cannot decode are refused."""
    allowed, reason, _ = signer._evaluate(make_req())  # "AAA=" is two zero bytes
    assert allowed is False and "undecodable" in reason


def test_a_description_without_the_bytes_it_describes_is_refused(tmp_db):
    req = signer.SignRequest(
        chain=Chain.SOL, order_id="o", wallet="w",
        payload={"instructions": [{"not": "an instruction"}]},
    )
    allowed, reason, _ = signer._evaluate(req)
    assert allowed is False and "undecodable" in reason


def test_a_policy_missing_the_expected_interface_is_refused(tmp_db, monkeypatch):
    import kaiba.execution as pkg

    class Hollow:
        pass

    monkeypatch.setattr(pkg, "policy", Hollow)
    wallet = new_pubkey()
    msg = compile_message(wallet, [system_transfer(wallet, new_pubkey(), 1)])
    req = signer.SignRequest(
        chain=Chain.SOL, order_id="o", wallet=wallet, payload={"transaction": unsigned_tx(msg)},
    )
    allowed, reason, _ = signer._evaluate(req)
    assert allowed is False and "interface mismatch" in reason


# ---------------------------------------------------------------- no override exists


def test_the_request_has_no_force_or_override_field():
    fields = set(inspect.signature(signer.SignRequest).parameters)
    for banned in ("force", "override", "bypass", "skip_policy", "recipient", "destination"):
        assert banned not in fields


def test_the_request_is_immutable():
    req = make_req()
    with pytest.raises(FrozenInstanceError):
        req.order_id = "tampered"  # type: ignore[misc]


def test_sign_request_takes_no_bypass_argument():
    params = set(inspect.signature(signer.sign_request).parameters)
    assert params == {"req", "keystore"}


def test_module_exposes_no_withdrawal_helper():
    names = [n.lower() for n in dir(signer)]
    for banned in ("withdraw", "transfer_out", "sweep", "drain", "export_key"):
        assert not any(banned in n for n in names)


# ---------------------------------------------------------------- key handling


def test_a_missing_key_is_a_refusal_not_a_crash(tmp_path):
    ks = signer.Keystore(tmp_path)
    with pytest.raises(signer.SignerRefused, match="no key"):
        ks.load(Chain.SOL, new_pubkey())


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_a_world_readable_key_is_refused(tmp_path):
    wallet = signer.keygen("sol", tmp_path)
    (tmp_path / f"sol-{wallet}.key").chmod(0o644)
    with pytest.raises(signer.SignerRefused, match="readable"):
        signer.Keystore(tmp_path).load(Chain.SOL, wallet)


def test_keystore_never_returns_raw_material(tmp_path):
    ks = signer.Keystore(tmp_path)
    public = [n for n in dir(ks) if not n.startswith("_")]
    assert set(public) == {"directory", "load", "forget"}


def test_forget_clears_the_cache(tmp_path):
    ks = signer.Keystore(tmp_path)
    ks._keys["sol:x"] = object()
    ks.forget()
    assert ks._keys == {}


# ---------------------------------------------------------------- client behaviour


def test_a_missing_socket_is_unavailable_not_refused(tmp_path):
    with pytest.raises(signer.SignerUnavailable, match="socket missing"):
        signer.request_signature(make_req(), tmp_path / "absent.sock")


def test_direct_lane_refuses_rather_than_faking_a_hash(tmp_db, tmp_path, monkeypatch):
    """An unimplemented builder must fail, never return a plausible-looking hash."""
    from kaiba.core.config import load_risk, save_risk
    from kaiba.core.schemas import Lane, LaneMode, Order, OrderState, Side

    cfg = load_risk()
    cfg.chains[Chain.SOL].wallet = "wallet1"
    p = tmp_path / "risk.yaml"
    save_risk(cfg, p)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(p))

    order = Order(
        order_id="o1", chain=Chain.SOL, token="t", side=Side.BUY, lane=Lane.CONFLUENCE_5,
        mode=LaneMode.LIVE, input_token="a", output_token="b", amount_in=1, min_out=1,
        slippage_bps=100, state=OrderState.PLANNED,
    )
    with pytest.raises(signer.SignerUnavailable, match="not implemented"):
        signer.sign_and_send(order)


def test_direct_lane_refuses_without_a_bound_wallet(tmp_db, tmp_path, monkeypatch):
    from kaiba.core.config import load_risk, save_risk
    from kaiba.core.schemas import Lane, LaneMode, Order, OrderState, Side

    cfg = load_risk()
    cfg.chains[Chain.SOL].wallet = None
    p = tmp_path / "risk.yaml"
    save_risk(cfg, p)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(p))
    order = Order(
        order_id="o2", chain=Chain.SOL, token="t", side=Side.BUY, lane=Lane.CONFLUENCE_5,
        mode=LaneMode.LIVE, input_token="a", output_token="b", amount_in=1, min_out=1,
        slippage_bps=100, state=OrderState.PLANNED,
    )
    with pytest.raises(signer.SignerRefused, match="no wallet bound"):
        signer.sign_and_send(order)


def test_health_reports_without_touching_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(signer, "KEYSTORE_DIR", tmp_path)
    out = signer.health(tmp_path / "s.sock")
    assert out["present"] is False and out["keys_present"] == 0
