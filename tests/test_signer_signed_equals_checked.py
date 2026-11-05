"""What the signer checks is exactly what it signs (design review 2026-10-02, defect 1).

Before the fix, ``signer.py`` judged ``payload["instructions"]`` (Solana) / ``payload["tx"]``
(EVM) and signed ``payload["transaction"]`` / ``payload``: two caller-supplied inputs that
could disagree. Every test here builds real wire bytes and attacks that gap: a benign
description beside malicious bytes, an appended instruction, bytes or a tx swapped after
evaluation, a chain-id swap, a signing library that alters the tx, and a replayed id.
"""

from __future__ import annotations

import base64
import os
from types import SimpleNamespace

import base58
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from kaiba.core.schemas import Chain
from kaiba.execution import policy, signer, txwire
from tests.signer_fixtures import (
    BSC_ROUTER,
    EVM_ATTACKER,
    KNOWN_EVM_ADDRESS,
    MEME,
    FakeEvmKey,
    FixedKeystore,
    compile_message,
    create_account,
    cu_price,
    describe,
    encode_signed,
    erc20_transfer,
    evm_tx,
    new_pubkey,
    raw_message,
    system_transfer,
    unsigned_tx,
    write_policy,
)


class ExplodingKeystore(signer.Keystore):
    def load(self, chain, wallet):  # type: ignore[override]
        raise AssertionError("a refused request must never reach the keystore")


@pytest.fixture
def env(tmp_db, tmp_path, monkeypatch):
    keys = tmp_path / "keys"
    wallet = signer.keygen("sol", keys)
    owned2, stranger = new_pubkey(), new_pubkey()
    pol = write_policy(tmp_path / "policy.yaml", sol_owned=[wallet, owned2],
                       evm_owned=[KNOWN_EVM_ADDRESS.lower()])
    monkeypatch.setenv("KAIBA_SIGNER_POLICY_PATH", str(pol))
    monkeypatch.setenv("KAIBA_SIGNER_STATE", str(tmp_path / "state"))
    return SimpleNamespace(keys=keys, ks=signer.Keystore(keys), wallet=wallet,
                           owned2=owned2, stranger=stranger, tmp=tmp_path)


def sol_req(env, ixs, *, order_id="ord-1", description=None, fee_payer=None, version=None):
    msg = compile_message(fee_payer or env.wallet, ixs, version=version)
    payload = {"transaction": unsigned_tx(msg)}
    if description is not None:
        payload["instructions"] = description
    return signer.SignRequest(chain=Chain.SOL, order_id=order_id, payload=payload, wallet=env.wallet), msg


def assert_signed_exactly(env, resp, msg):
    assert resp.ok is True, resp.reason
    out = txwire.parse_sol_transaction(base64.b64decode(resp.signature))
    assert out.message_bytes == msg
    idx = out.message.signer_keys.index(env.wallet)
    Ed25519PublicKey.from_public_bytes(base58.b58decode(env.wallet)).verify(out.signatures[idx], msg)
    return out


# ============================================================================ Solana


def test_positive_control_signs_exactly_the_judged_message(env):
    good = system_transfer(env.wallet, env.owned2, 1_000)
    req, msg = sol_req(env, [cu_price(1_000), good])
    assert_signed_exactly(env, signer.sign_request(req, env.ks), msg)


def test_benign_description_beside_malicious_bytes_is_refused(env):
    good = system_transfer(env.wallet, env.owned2, 1_000)
    bad = system_transfer(env.wallet, env.stranger, 1_000)
    req, _ = sol_req(env, [bad], description=[describe(good)])
    resp = signer.sign_request(req, ExplodingKeystore(env.keys))
    assert resp.ok is False and resp.signature is None
    assert resp.reason == "request_parts_disagree:ix0.accounts"


def test_malicious_bytes_are_judged_from_the_bytes_without_any_description(env):
    bad = system_transfer(env.wallet, env.stranger, 1_000)
    req, _ = sol_req(env, [bad])
    resp = signer.sign_request(req, ExplodingKeystore(env.keys))
    assert resp.ok is False
    assert resp.reason.startswith(f"system_transfer_to_non_owned:{env.stranger}")


def test_a_matching_description_is_accepted(env):
    good = system_transfer(env.wallet, env.owned2, 1_000)
    req, msg = sol_req(env, [good], description=[describe(good)])
    assert_signed_exactly(env, signer.sign_request(req, env.ks), msg)


def test_description_with_different_amount_is_refused(env):
    req, _ = sol_req(env, [system_transfer(env.wallet, env.owned2, 1_000)],
                     description=[describe(system_transfer(env.wallet, env.owned2, 999))])
    assert signer.sign_request(req, ExplodingKeystore(env.keys)).reason == "request_parts_disagree:ix0.data"


def test_an_appended_instruction_is_seen(env):
    good = system_transfer(env.wallet, env.owned2, 1_000)
    bad = system_transfer(env.wallet, env.stranger, 5_000_000_000)
    req, _ = sol_req(env, [good, bad], description=[describe(good)])
    resp = signer.sign_request(req, ExplodingKeystore(env.keys))
    assert resp.reason == "request_parts_disagree:instruction_count:1!=2"

    req, _ = sol_req(env, [good, bad], order_id="ord-2")
    resp = signer.sign_request(req, ExplodingKeystore(env.keys))
    assert resp.ok is False and resp.reason.endswith("@ix1")
    assert "system_transfer_to_non_owned" in resp.reason


def test_signer_flags_come_from_the_message_header_not_the_caller(env):
    # The policy lets CreateAccount fund a stranger's account only if that account signs.
    # The old signer took is_signer from the caller, so a caller could simply claim it.
    ix = create_account(env.wallet, env.stranger, new_signs=False)
    lie = describe(ix, is_signer=[True, True], is_writable=[True, True])
    req, _ = sol_req(env, [ix], description=[lie])
    assert signer.sign_request(req, ExplodingKeystore(env.keys)).reason == "request_parts_disagree:ix0.is_signer"

    req, _ = sol_req(env, [ix], order_id="ord-2")
    assert signer.sign_request(req, ExplodingKeystore(env.keys)).reason.startswith("system_create_account_not_owned")


def test_ephemeral_co_signer_is_allowed_and_only_our_slot_is_signed(env):
    ephemeral = new_pubkey()
    req, msg = sol_req(env, [create_account(env.wallet, ephemeral, new_signs=True)])
    out = assert_signed_exactly(env, signer.sign_request(req, env.ks), msg)
    assert out.message.signer_keys == (env.wallet, ephemeral)
    assert out.signatures[1] == bytes(64)  # the builder signs its own ephemeral account


def test_bytes_swapped_during_evaluation_are_not_what_gets_signed(env, monkeypatch):
    good = system_transfer(env.wallet, env.owned2, 1_000)
    req, msg = sol_req(env, [good])
    evil_msg = compile_message(env.wallet, [system_transfer(env.wallet, env.stranger, 10**9)])
    real = policy.check_solana_transaction

    def judge_then_swap(*a, **kw):
        decision = real(*a, **kw)
        req.payload["transaction"] = unsigned_tx(evil_msg)  # the frozen request's dict is mutable
        return decision

    monkeypatch.setattr(policy, "check_solana_transaction", judge_then_swap)
    resp = signer.sign_request(req, env.ks)
    out = assert_signed_exactly(env, resp, msg)
    assert env.stranger not in out.message.account_keys


def test_a_second_description_key_is_refused(env):
    good = system_transfer(env.wallet, env.owned2, 1_000)
    req, _ = sol_req(env, [good])
    req.payload["tx"] = {"to": "x"}
    assert signer.sign_request(req, ExplodingKeystore(env.keys)).reason == "payload_keys_unexpected:tx"


def test_our_wallet_must_be_a_required_signer(env):
    req, _ = sol_req(env, [system_transfer(env.owned2, env.wallet, 1_000)], fee_payer=env.owned2)
    resp = signer.sign_request(req, env.ks)
    assert resp.ok is False and resp.reason == "wallet_not_a_required_signer"


def test_an_account_from_a_lookup_table_is_refused(env):
    keys = [env.wallet, policy.SYSTEM_PROGRAM]
    data = (2).to_bytes(4, "little") + (1_000).to_bytes(8, "little")
    msg = raw_message((1, 0, 1), keys, [(1, [0, 2], data)], version=0,
                      lookups=[(new_pubkey(), [0], [])])
    req = signer.SignRequest(chain=Chain.SOL, order_id="alt", wallet=env.wallet,
                             payload={"transaction": unsigned_tx(msg)})
    assert signer.sign_request(req, ExplodingKeystore(env.keys)).reason.startswith("alt_unresolved")


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda raw: raw + b"\x00", id="trailing-byte"),
        pytest.param(lambda raw: b"\x02" + bytes(64) + raw[1:], id="extra-signature-slot"),
        pytest.param(lambda raw: b"\x81\x00" + raw[1:], id="non-canonical-length"),
    ],
)
def test_malformed_wire_bytes_are_refused(env, tamper):
    good = system_transfer(env.wallet, env.owned2, 1_000)
    req, _ = sol_req(env, [good])
    raw = tamper(base64.b64decode(req.payload["transaction"]))
    bad = signer.SignRequest(chain=Chain.SOL, order_id="w", wallet=env.wallet,
                             payload={"transaction": base64.b64encode(raw).decode()})
    resp = signer.sign_request(bad, ExplodingKeystore(env.keys))
    assert resp.ok is False and resp.reason.startswith("undecodable transaction: WireError")


def test_a_key_file_holding_another_wallet_is_refused(env):
    impostor = env.keys / f"sol-{env.owned2}.key"
    impostor.write_bytes((env.keys / f"sol-{env.wallet}.key").read_bytes())
    impostor.chmod(0o600)
    with pytest.raises(signer.SignerRefused, match="different wallet"):
        signer.Keystore(env.keys).load(Chain.SOL, env.owned2)


@pytest.mark.parametrize("wallet", ["../../etc/passwd", "wallet1", "", "0xabc"])
def test_a_wallet_string_cannot_name_an_arbitrary_file(env, wallet):
    with pytest.raises(signer.SignerRefused, match="not a valid sol address"):
        env.ks.load(Chain.SOL, wallet)


def test_the_loaded_key_exposes_no_material(env):
    key = env.ks.load(Chain.SOL, env.wallet)
    assert sorted(n for n in dir(key) if not n.startswith("_")) == ["address", "sign", "verify"]
    assert env.wallet in repr(key)
    secret = (env.keys / f"sol-{env.wallet}.key").read_text().strip()
    assert secret not in repr(key)


# ============================================================================ replay


def test_a_request_id_is_single_use(env):
    good = system_transfer(env.wallet, env.owned2, 1_000)
    req, msg = sol_req(env, [good], order_id="ord-replay")
    assert_signed_exactly(env, signer.sign_request(req, env.ks), msg)

    again = signer.sign_request(req, env.ks)
    assert again.ok is False and again.reason == "request_id_replayed:ord-replay"

    other, _ = sol_req(env, [system_transfer(env.wallet, env.owned2, 2)], order_id="ord-replay")
    assert signer.sign_request(other, env.ks).reason == "request_id_replayed:ord-replay"

    fresh, msg2 = sol_req(env, [system_transfer(env.wallet, env.owned2, 2)], order_id="ord-fresh")
    assert_signed_exactly(env, signer.sign_request(fresh, env.ks), msg2)


def test_the_replay_ledger_survives_a_restart(env, monkeypatch):
    req, msg = sol_req(env, [system_transfer(env.wallet, env.owned2, 1)], order_id="ord-persist")
    assert signer.sign_request(req, env.ks).ok
    monkeypatch.setattr(signer, "_LEDGERS", {})  # a new process
    assert signer.sign_request(req, env.ks).reason == "request_id_replayed:ord-persist"
    ledger = env.tmp / "state" / "signed-requests.jsonl"
    assert "ord-persist" in ledger.read_text()
    if os.name == "posix":
        assert ledger.stat().st_mode & 0o077 == 0


def test_a_refused_request_does_not_consume_its_id(env):
    bad, _ = sol_req(env, [system_transfer(env.wallet, env.stranger, 1)], order_id="ord-q")
    assert signer.sign_request(bad, env.ks).ok is False
    good, msg = sol_req(env, [system_transfer(env.wallet, env.owned2, 1)], order_id="ord-q")
    assert_signed_exactly(env, signer.sign_request(good, env.ks), msg)


# ============================================================================ EVM


def evm_req(tx, *, order_id="evm-1", chain=Chain.BSC, **extra):
    return signer.SignRequest(chain=chain, order_id=order_id, wallet=KNOWN_EVM_ADDRESS,
                              payload={"tx": tx, **extra})


def test_evm_positive_control_signs_the_canonical_dict_it_judged(env):
    key = FakeEvmKey()
    resp = signer.sign_request(evm_req(evm_tx()), FixedKeystore(key))
    assert resp.ok is True, resp.reason
    sent = key.received[0]
    assert sent["to"] == "0x10ED43C718714eb63d5aA57B78B54704E256024E"  # EIP-55 of the router
    assert sent["chainId"] == 56 and sent["value"] == 10**16
    assert set(sent) == {"chainId", "nonce", "to", "value", "data", "gas",
                         "maxFeePerGas", "maxPriorityFeePerGas"}
    decoded = txwire.decode_signed_evm_transaction(bytes.fromhex(resp.signature[2:]))
    assert decoded["to"] == BSC_ROUTER and decoded["data"] == evm_tx()["data"]


def test_evm_legacy_eip155_round_trip(env):
    tx = evm_tx(gasPrice=3 * 10**9)
    del tx["maxFeePerGas"], tx["maxPriorityFeePerGas"]
    assert signer.sign_request(evm_req(tx), FixedKeystore(FakeEvmKey())).ok is True


def test_evm_description_beside_the_tx_is_refused(env):
    ks = FixedKeystore(FakeEvmKey())
    req = evm_req(evm_tx(), to=EVM_ATTACKER, data=erc20_transfer(EVM_ATTACKER, 10**30))
    resp = signer.sign_request(req, ks)
    assert resp.ok is False and resp.reason == "payload_keys_unexpected:data,to"
    assert ks.loads == 0


def test_evm_withdrawal_is_judged_from_the_tx_itself(env):
    tx = evm_tx(to=MEME, value=0, data=erc20_transfer(EVM_ATTACKER, 10**30))
    ks = FixedKeystore(FakeEvmKey())
    resp = signer.sign_request(evm_req(tx), ks)
    assert resp.ok is False and resp.reason.startswith("erc20_transfer_forbidden")
    assert ks.loads == 0


def test_evm_recipient_must_be_owned(env):
    resp = signer.sign_request(evm_req(evm_tx(recipient=EVM_ATTACKER)), FixedKeystore(FakeEvmKey()))
    assert resp.ok is False and resp.reason == f"recipient_not_owned:{EVM_ATTACKER}"


@pytest.mark.parametrize(
    ("chain", "tx", "reason"),
    [
        (Chain.BSC, evm_tx(chainId=1), "chain_id_mismatch:1!=56"),
        (Chain.ROBINHOOD, evm_tx(), "chain_id_mismatch:56!=4663"),
        (Chain.BSC, {k: v for k, v in evm_tx().items() if k != "chainId"}, "chain_id_missing"),
        (Chain.BSC, evm_tx(chainId="0x1"), "chain_id_mismatch:1!=56"),
    ],
)
def test_evm_chain_id_swap_is_refused(env, chain, tx, reason):
    ks = FixedKeystore(FakeEvmKey())
    resp = signer.sign_request(evm_req(tx, chain=chain), ks)
    assert resp.ok is False and resp.reason == reason
    assert ks.loads == 0


@pytest.mark.parametrize(
    ("tx", "reason"),
    [
        (evm_tx(chainId=1), "chain_id_mismatch:1!=56"),
        ({k: v for k, v in evm_tx().items() if k != "chainId"}, "chain_id_missing"),
    ],
)
def test_the_signers_chain_binding_does_not_lean_on_the_policy(env, monkeypatch, tx, reason):
    """Mutation-found: with the signer's own check removed, the policy's identical
    `chain_id_mismatch` reason hid it. Pin the signer's binding with a permissive policy."""
    monkeypatch.setattr(policy, "check_evm_transaction",
                        lambda *a, **k: policy.PolicyDecision(allowed=True, reason="permissive"))
    ks = FixedKeystore(FakeEvmKey())
    resp = signer.sign_request(evm_req(tx), ks)
    assert resp.ok is False and resp.reason == reason
    assert ks.loads == 0


def test_policy_alone_would_pass_a_missing_chain_id(env):
    """Why the signer requires chainId: policy skips its chain check when it is None."""
    tx = policy.EvmTx(to=BSC_ROUTER, value=10**16, data=evm_tx()["data"], chain_id=None)
    assert policy.check_evm_transaction(tx, chain=Chain.BSC, policy=policy.load_policy()).allowed


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"from": KNOWN_EVM_ADDRESS}, "tx_fields_unexpected:from"),
        ({"input": "0x"}, "tx_fields_unexpected:input"),
        ({"authorizationList": [{"address": EVM_ATTACKER}]}, "tx_fields_unexpected:authorizationList"),
        ({"maxFeePerBlobGas": 1}, "tx_fields_unexpected:maxFeePerBlobGas"),
        ({"accessList": [{"address": EVM_ATTACKER, "storageKeys": []}]}, "access_list_not_empty"),
        ({"type": 4}, "tx_type_inconsistent"),
        ({"gasPrice": 1}, "tx_fee_fields_mixed"),
        ({"value": True}, "tx_field_invalid:value"),
        ({"value": -1}, "tx_field_out_of_range:value"),
        ({"value": "1000"}, "tx_field_invalid:value"),
        ({"nonce": 2**64}, "tx_field_out_of_range:nonce"),
        ({"data": "0xabc"}, "tx_field_invalid:data"),
        ({"to": "0x10Ed43C718714eb63d5aA57B78B54704E256024E"}, "to_checksum_invalid"),
    ],
)
def test_evm_tx_outside_the_closed_shape_is_refused(env, change, reason):
    resp = signer.sign_request(evm_req(evm_tx(**change)), FixedKeystore(FakeEvmKey()))
    assert resp.ok is False and resp.reason == reason


def test_evm_contract_creation_is_refused(env):
    tx = {k: v for k, v in evm_tx().items() if k != "to"}
    resp = signer.sign_request(evm_req(tx), FixedKeystore(FakeEvmKey()))
    assert resp.ok is False and resp.reason == "tx_fields_missing:to"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("to", EVM_ATTACKER),
        ("value", 10**24),
        ("data", erc20_transfer(EVM_ATTACKER, 10**30)),
        ("chainId", 1),
    ],
)
def test_evm_tx_mutated_after_evaluation_is_not_what_gets_signed(env, monkeypatch, field, value):
    req = evm_req(evm_tx())
    original = dict(req.payload["tx"])
    real = policy.check_evm_transaction

    def judge_then_swap(*a, **kw):
        decision = real(*a, **kw)
        req.payload["tx"][field] = value
        return decision

    monkeypatch.setattr(policy, "check_evm_transaction", judge_then_swap)
    key = FakeEvmKey()
    resp = signer.sign_request(req, FixedKeystore(key))
    assert resp.ok is True, resp.reason
    sent = key.received[0]
    want = original[field].lower() if isinstance(original[field], str) else original[field]
    got = sent[field].lower() if isinstance(sent[field], str) else sent[field]
    assert got == want


def _set(field, value):
    def mutate(tx):
        tx[field] = value
    return mutate


@pytest.mark.parametrize(
    ("mutate", "diff"),
    [
        (_set("to", EVM_ATTACKER), "to"),
        (_set("value", 10**24), "value"),
        (_set("data", erc20_transfer(EVM_ATTACKER, 10**30)), "data"),
        (_set("chainId", 1), "chainId"),
        (_set("nonce", 8), "nonce"),
        (_set("gas", 10**7), "gas"),
        (_set("maxFeePerGas", 10**15), "maxFeePerGas"),
    ],
)
def test_a_signing_library_that_alters_the_tx_is_caught(env, mutate, diff):
    resp = signer.sign_request(evm_req(evm_tx()), FixedKeystore(FakeEvmKey(mutate=mutate)))
    assert resp.ok is False and resp.signature is None
    assert resp.reason == f"signed_differs_from_checked:{diff}"


def test_a_pre_eip155_legacy_signature_is_refused(env):
    tx = evm_tx(gasPrice=3 * 10**9)
    del tx["maxFeePerGas"], tx["maxPriorityFeePerGas"]
    key = FakeEvmKey(encode=lambda t: encode_signed(t, v=27))
    resp = signer.sign_request(evm_req(tx), FixedKeystore(key))
    assert resp.ok is False
    assert resp.reason == "signed_differs_from_checked:undecodable:legacy_signature_without_chain_id"


def test_a_set_code_envelope_from_the_library_is_refused(env):
    key = FakeEvmKey(encode=lambda t: b"\x04" + encode_signed(t)[1:])
    resp = signer.sign_request(evm_req(evm_tx()), FixedKeystore(key))
    assert resp.ok is False and resp.reason.startswith("signed_differs_from_checked:undecodable:evm_tx_type_unsupported")


def test_evm_request_id_is_single_use(env):
    ks = FixedKeystore(FakeEvmKey())
    assert signer.sign_request(evm_req(evm_tx(), order_id="e-r"), ks).ok
    assert signer.sign_request(evm_req(evm_tx(nonce=8), order_id="e-r"), ks).reason == "request_id_replayed:e-r"


def test_real_eth_account_round_trip(env, monkeypatch):
    """Runs where the signer extra is installed (the box); skipped on a dev machine without it."""
    eth_account = pytest.importorskip("eth_account")
    keys = env.tmp / "evm-keys"
    address = signer.keygen("evm", keys)
    pol = write_policy(env.tmp / "p2.yaml", sol_owned=[env.wallet], evm_owned=[address.lower()])
    monkeypatch.setenv("KAIBA_SIGNER_POLICY_PATH", str(pol))
    req = signer.SignRequest(chain=Chain.BSC, order_id="real-1", wallet=address,
                             payload={"tx": evm_tx(recipient=address)})
    resp = signer.sign_request(req, signer.Keystore(keys))
    assert resp.ok is True, resp.reason
    raw = bytes.fromhex(resp.signature[2:])
    assert eth_account.Account.recover_transaction(raw).lower() == address.lower()
    assert txwire.decode_signed_evm_transaction(raw)["to"] == BSC_ROUTER
