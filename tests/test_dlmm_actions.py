"""Independent IDL-layout fixtures and wallet cash-flow counterexamples; no provider calls."""

import base64
import copy
import struct

import base58
import pytest

from kaiba.learning.dlmm_actions import EVENT_AUTHORITY, PROGRAM, SYSTEM, WSOL, decode_transaction
from kaiba.learning.dlmm_state import DecodeError


def key(n):
    return base58.b58encode(bytes([n]) * 32).decode()


POOL, POSITION, MINT, OWNER = key(1), key(2), key(3), key(9)
X_ACCOUNT, Y_VAULT, TEMP, RECIPIENT = key(10), key(11), key(12), key(13)
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SIGNATURE = base58.b58encode(bytes([17]) * 64).decode()
KEYS = [OWNER, POOL, POSITION, PROGRAM, EVENT_AUTHORITY, SYSTEM, TOKEN, MINT, WSOL,
        X_ACCOUNT, Y_VAULT, TEMP, RECIPIENT]
# Literal discriminators and binary layouts taken from the pinned IDL, not the decoder table.
CREATE = bytes([144, 142, 252, 84, 157, 53, 37, 121])
ADD = bytes([31, 94, 125, 90, 227, 52, 61, 186])
CLAIM = bytes([75, 122, 154, 48, 140, 74, 123, 163])
COMPOSITION = bytes([128, 151, 123, 106, 17, 102, 113, 142])
REBALANCE = bytes([0, 109, 117, 179, 61, 91, 199, 200])
CPI = bytes.fromhex("e445a52e51cb9a1d")


def pubkeys(*keys):
    return b"".join(base58.b58decode(k) for k in keys)


def event(payload):
    return {"programId": PROGRAM, "accounts": [EVENT_AUTHORITY], "data": base58.b58encode(CPI + payload).decode()}


def token_row(account, mint, owner, amount):
    return {"accountIndex": KEYS.index(account), "mint": mint, "owner": owner, "programId": TOKEN,
            "uiTokenAmount": {"amount": str(amount), "decimals": 9 if mint == WSOL else 6}}


def transaction(*, x=100, y=200, funding=500, extra=11):
    pre = [1_000_000] * len(KEYS)
    pre[KEYS.index(POSITION)] = pre[KEYS.index(TEMP)] = 0
    post = list(pre)
    post[0] -= 5 + y + funding + extra
    post[KEYS.index(POSITION)] = funding
    post[KEYS.index(Y_VAULT)] += y
    post[KEYS.index(RECIPIENT)] += extra
    create = CREATE + pubkeys(POOL, POSITION, OWNER)
    add = ADD + pubkeys(POOL, OWNER, POSITION) + struct.pack("<QQi", x, y, -714)
    return {"version": "legacy", "slot": 90, "blockTime": 900,
            "transaction": {"signatures": [SIGNATURE], "message": {
                "accountKeys": [{"pubkey": k, "signer": i == 0, "writable": True, "source": "transaction"} for i, k in enumerate(KEYS)],
                "instructions": [{"programId": PROGRAM, "accounts": [], "data": ""},
                                 {"programId": SYSTEM, "parsed": {"type": "transfer", "info": {"source": OWNER, "destination": RECIPIENT, "lamports": extra}}}]}},
            "meta": {"err": None, "fee": 5, "preBalances": pre, "postBalances": post,
                     "preTokenBalances": [token_row(X_ACCOUNT, MINT, OWNER, 10000), token_row(Y_VAULT, WSOL, POOL, 1000)],
                     "postTokenBalances": [token_row(X_ACCOUNT, MINT, OWNER, 10000 - x), token_row(Y_VAULT, WSOL, POOL, 1000 + y)],
                     "innerInstructions": [{"index": 0, "instructions": [event(create),
                         {"programId": TOKEN, "parsed": {"type": "initializeAccount3", "info": {"owner": OWNER, "account": TEMP, "mint": WSOL}}},
                         event(add)]}], "logMessages": []}}


def decode(tx):
    return decode_transaction(tx, signature=SIGNATURE, pool=POOL, position=POSITION, owner=OWNER, mints=(MINT, WSOL))


def test_deposits_temporary_wrapped_sol_and_position_funding_reconcile_exactly():
    out = decode(transaction())
    assert out["action_cashflows_reconciled"] and out["gaps"] == []
    assert out["deposits_atoms"] == [100, 200] and out["claimed_fee_atoms_gross"] == [0, 0]
    cash = out["cashflows"]
    assert cash["owner_native_delta"] == -716
    assert cash["position_account_funding_delta_lamports"] == 500
    assert cash["other_native_outflows_lamports"] == 11
    assert cash["native_residual_lamports"] == 0 and cash["token_residual_atoms"] == [0, None]
    assert out["net_pnl"] is None and not out["chain_verified"]


def test_unexplained_native_debit_is_a_gap_with_positive_control():
    tx = transaction()
    tx["meta"]["postBalances"][0] -= 1
    out = decode(tx)
    assert not out["action_cashflows_reconciled"]
    assert out["cashflows"]["native_residual_lamports"] == -1
    assert "wallet_event_cashflow_residual" in out["gaps"]
    tx["meta"]["postBalances"][0] += 1
    assert decode(tx)["action_cashflows_reconciled"]


def test_claims_are_distributions_and_composition_fees_are_not_debited_twice():
    tx = transaction(x=0, y=200, funding=0)
    group = tx["meta"]["innerInstructions"][0]["instructions"]
    group[0] = event(COMPOSITION + pubkeys(OWNER) + struct.pack("<hQQQQ", -3, 0, 9, 0, 1))
    group[-1] = event(REBALANCE + pubkeys(POOL, POSITION, OWNER) + struct.pack("<iQQQQQQiiiiQQ", -3, 0, 0, 0, 200, 10, 0, -10, 0, -11, 0, 0, 0))
    tx["meta"]["postTokenBalances"][0]["uiTokenAmount"]["amount"] = "10010"
    out = decode(tx)
    assert out["action_cashflows_reconciled"]
    assert out["claimed_fee_atoms_gross"] == [10, 0]
    assert out["composition_fees_embedded"] == [{"bin": -3, "x": 0, "y": 9, "protocol_x": 0, "protocol_y": 1}]
    assert out["event_implied_owner_delta_atoms"] == [10, -200]


def test_cpi_and_log_carriers_are_not_double_counted_and_disagreement_refuses():
    tx = transaction()
    raws = [base58.b58decode(ix["data"])[8:] for ix in tx["meta"]["innerInstructions"][0]["instructions"] if "data" in ix]
    tx["meta"]["logMessages"] = [f"Program {PROGRAM} invoke [1]", *["Program data: " + base64.b64encode(raw).decode() for raw in raws], f"Program {PROGRAM} success"]
    assert decode(tx)["deposits_atoms"] == [100, 200]
    tx["meta"]["logMessages"].pop(1)
    with pytest.raises(DecodeError, match="carriers_disagree"):
        decode(tx)


def test_foreign_emitter_cannot_supply_missing_events():
    tx = transaction()
    payload = base58.b58decode(tx["meta"]["innerInstructions"][0]["instructions"][0]["data"])[8:]
    tx["meta"]["innerInstructions"] = []
    tx["meta"]["logMessages"] = [f"Program {SYSTEM} invoke [1]", "Program data: " + base64.b64encode(payload).decode(), f"Program {SYSTEM} success"]
    out = decode(tx)
    assert not out["actions"] and "no_target_position_event" in out["gaps"]


def test_cpi_requires_program_event_authority():
    tx = transaction()
    tx["meta"]["innerInstructions"][0]["instructions"][0]["accounts"] = [OWNER]
    with pytest.raises(DecodeError, match="event_authority"):
        decode(tx)


@pytest.mark.parametrize("mutation", [
    lambda t: t["meta"]["postTokenBalances"].pop(0),
    lambda t: t["meta"]["postTokenBalances"][0].update(owner=POOL),
    lambda t: t["meta"]["postTokenBalances"][0]["uiTokenAmount"].update(decimals=7),
])
def test_missing_balances_and_changed_token_identity_are_never_zero_cost(mutation):
    tx = transaction()
    mutation(tx)
    out = decode(tx)
    assert out["cashflows"] is None and not out["action_cashflows_reconciled"]
    assert any(g.startswith("cashflow_evidence_unavailable:") for g in out["gaps"])


def test_failed_attempt_retains_fee_but_discards_logs_of_rolled_back_actions():
    tx = transaction()
    tx["meta"]["err"] = {"InstructionError": [0, "InvalidArgument"]}
    tx["meta"]["postBalances"] = copy.deepcopy(tx["meta"]["preBalances"])
    tx["meta"]["postBalances"][0] -= 5
    out = decode(tx)
    assert out["status"] == "failed" and out["actions"] == [] and out["gaps"] == []
    assert out["owner_network_fee_lamports"] == 5
    tx["meta"]["postBalances"][1] += 1
    assert decode(tx)["gaps"] == ["failed_transaction_native_residual"]


@pytest.mark.parametrize("version", [True, False, 1, 128, None, "0"])
def test_future_or_malformed_versions_are_not_empty_history(version):
    tx = transaction()
    tx["version"] = version
    with pytest.raises(DecodeError, match="version"):
        decode(tx)


def test_unknown_event_and_foreign_position_withhold_reconciliation():
    tx = transaction()
    tx["meta"]["innerInstructions"][0]["instructions"].append(event(bytes(8)))
    assert "unknown_dlmm_event" in decode(tx)["gaps"]
    tx = transaction()
    tx["meta"]["innerInstructions"][0]["instructions"].append(event(CREATE + pubkeys(POOL, RECIPIENT, OWNER)))
    assert "other_position_or_pool_in_transaction" in decode(tx)["gaps"]


def test_shuffled_inner_groups_follow_transaction_instruction_order():
    tx = transaction()
    group = tx["meta"]["innerInstructions"][0]["instructions"]
    tx["meta"]["innerInstructions"] = [{"index": 1, "instructions": group[1:]}, {"index": 0, "instructions": group[:1]}]
    assert [a["name"] for a in decode(tx)["actions"]] == ["PositionCreate", "AddLiquidity"]


def test_event_extension_and_wrong_signature_refuse():
    tx = transaction()
    ix = tx["meta"]["innerInstructions"][0]["instructions"][0]
    ix["data"] = base58.b58encode(base58.b58decode(ix["data"]) + b"x").decode()
    with pytest.raises(DecodeError, match="extension"):
        decode(tx)
    tx = transaction()
    tx["transaction"]["signatures"] = [base58.b58encode(bytes([18]) * 64).decode()]
    with pytest.raises(DecodeError, match="signature"):
        decode(tx)


def test_persistent_wrapped_sol_does_not_count_its_principal_as_rent():
    tx = transaction()
    # Deposit 200 atoms from an existing WSOL account rather than from free SOL.
    tx["meta"]["preTokenBalances"].append(token_row(TEMP, WSOL, OWNER, 1000))
    tx["meta"]["postTokenBalances"].append(token_row(TEMP, WSOL, OWNER, 800))
    tx["meta"]["preBalances"][KEYS.index(TEMP)] = 3000  # 2000 rent + 1000 WSOL
    tx["meta"]["postBalances"][KEYS.index(TEMP)] = 2800
    tx["meta"]["postBalances"][0] += 200
    out = decode(tx)
    assert out["action_cashflows_reconciled"]
    assert out["cashflows"]["owner_token_delta_atoms"] == [-100, -200]
    assert out["cashflows"]["owned_token_account_funding_delta_lamports"] == 0


def test_composition_fee_cannot_be_assigned_between_two_position_events():
    tx = transaction()
    tx["meta"]["innerInstructions"][0]["instructions"].append(event(COMPOSITION + pubkeys(OWNER) + struct.pack("<hQQQQ", -3, 0, 9, 0, 1)))
    assert "composition_fee_position_ambiguous" in decode(tx)["gaps"]


def test_reward_payment_prevents_two_asset_completeness():
    tx = transaction(x=0, y=200, funding=0)
    tx["meta"]["innerInstructions"][0]["instructions"][-1] = event(REBALANCE + pubkeys(POOL, POSITION, OWNER) + struct.pack("<iQQQQQQiiiiQQ", -3, 0, 0, 0, 200, 0, 0, -10, 0, -11, 0, 1, 0))
    assert "third_asset_rewards_unreconciled" in decode(tx)["gaps"]
