"""Offline boundary witnesses; no wallet/RPC/signing or guessed current state."""
import base64
import copy
import json

import base58
import pytest

from kaiba.execution import rent_recovery as r

AT = 1_793_726_000_000
OWNER_BYTES = bytes([7])*32
WALLET = base58.b58encode(OWNER_BYTES).decode()
OTHER = bytes([8])*32


def fixture(*, amount=0, state=1, native=0, close=0, authority=None, key=9):
    raw = bytearray(165)
    raw[:32] = bytes([3])*32
    raw[32:64] = OWNER_BYTES
    raw[64:72] = amount.to_bytes(8, "little")
    raw[108] = state
    raw[109:113] = native.to_bytes(4, "little")
    raw[129:133] = close.to_bytes(4, "little")
    raw[133:165] = authority or bytes(32)
    row = {"pubkey": base58.b58encode(bytes([key])*32).decode(), "account": {
        "owner": r.TOKEN_PROGRAM, "executable": False, "lamports": 2_039_280,
        "data": [base64.b64encode(raw).decode(), "base64"], "space": 165}}
    request = {"jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner", "params": [
        WALLET, {"programId": r.TOKEN_PROGRAM}, {"encoding": "base64", "commitment": "finalized"}]}
    response = {"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 449_100_000}, "value": [row]}}
    return request, response


def assess(request, response, **kwargs):
    return r.assess(request, response, expected_wallet=WALLET, observed_ms=AT, at_ms=AT+1000, **kwargs)


def mutate_bytes(response, offset, raw):
    row = response["result"]["value"][0]["account"]
    decoded = bytearray(base64.b64decode(row["data"][0]))
    decoded[offset:offset+len(raw)] = raw
    row["data"][0] = base64.b64encode(decoded).decode()


@pytest.mark.parametrize("state,close,authority", [(1, 0, None), (2, 0, None), (1, 1, OWNER_BYTES)])
def test_zero_initialized_or_frozen_with_own_authority_is_only_a_candidate(state, close, authority):
    # Classic frozen-zero closure is allowed by the official v9 close processor:
    # https://github.com/solana-program/token/blob/program%40v9.0.0/program/src/processor.rs#L615-L649
    req, res = fixture(state=state, close=close, authority=authority)
    out = assess(req, res)
    assert out["candidate_count"] == 1 and out["candidate_lamports"] == 2_039_280
    assert out["candidate_sol"] == "0.00203928"
    assert out["status"] == "candidates_observed"
    assert out["net_recoverable_lamports"] is out["fee_lamports"] is None
    assert out["financial_execution"] is out["profit_proven"] is False
    summary = json.dumps(r.public_summary(out))
    assert WALLET not in summary and res["result"]["value"][0]["pubkey"] not in summary


@pytest.mark.parametrize("args,reason", [
    ({"amount": 1}, "nonzero_token_amount"),
    ({"native": 1}, "native_wrapper_excluded"),
    ({"close": 1, "authority": OTHER}, "foreign_close_authority"),
    ({"state": 0}, "uninitialized_or_invalid_state"),
    ({"state": 3}, "uninitialized_or_invalid_state"),
    ({"close": 2}, "invalid_option_tag"),
    ({"native": 2}, "invalid_option_tag"),
])
def test_excluded_or_unproved_accounts_are_never_closed_or_lost(args, reason):
    req, res = fixture(**args)
    out = assess(req, res)
    assert out["candidate_count"] == 0 and out["refused_count"] == 1
    assert out["refusal_reasons"] == {reason: 1}
    good = assess(*fixture())
    assert good["candidate_count"] == 1  # positive control: cannot pass by rejecting everything


@pytest.mark.parametrize("change,reason", [
    (lambda a: a.update(lamports=True), "invalid_integer"),
    (lambda a: a.update(lamports=2_039_280.0), "invalid_integer"),
    (lambda a: a.update(lamports=-1), "invalid_integer"),
    (lambda a: a.update(lamports=2**64), "invalid_integer"),
    (lambda a: a.update(owner="unsupported-token-2022-or-program"), "unsupported_program"),
    (lambda a: a.update(executable=True), "executable_or_unknown"),
    (lambda a: a.pop("executable"), "executable_or_unknown"),
    (lambda a: a.update(space=165.0), "account_layout"),
    (lambda a: a.update(data={"parsed": {"info": {"tokenAmount": {"amount": "0"}}}}), "account_encoding_or_size"),
])
def test_invalid_or_partial_metadata_remains_unknown(change, reason):
    req, res = fixture()
    change(res["result"]["value"][0]["account"])
    out = assess(req, res)
    assert out["status"] == "incomplete" and out["unknown_count"] == 1
    assert out["refusal_reasons"] == {reason: 1}
    assert out["net_recoverable_lamports"] is None


def test_data_owner_and_delegate_layout_tags_are_checked():
    req, res = fixture()
    mutate_bytes(res, 32, OTHER)
    assert assess(req, res)["refusal_reasons"] == {"token_owner_mismatch": 1}
    req, res = fixture()
    mutate_bytes(res, 72, (9).to_bytes(4, "little"))
    assert assess(req, res)["refusal_reasons"] == {"invalid_option_tag": 1}


@pytest.mark.parametrize("change", [
    lambda q: q["params"][2].update(commitment="processed"),
    lambda q: q["params"][2].update(encoding="jsonParsed"),
    lambda q: q["params"].__setitem__(0, base58.b58encode(OTHER).decode()),
    lambda q: q.update(method="getProgramAccounts"),
])
def test_request_scope_not_just_plausible_result(change):
    req, res = fixture()
    change(req)
    out = assess(req, res)
    assert out["status"] == "unavailable" and out["reason"] == "request_scope_mismatch"
    assert out["candidate_lamports"] is None


def test_duplicate_accounts_cannot_double_the_recovery_estimate():
    req, res = fixture()
    res["result"]["value"] *= 2
    out = assess(req, res)
    assert out["status"] == "unavailable" and out["candidate_lamports"] is None
    assert out["reason"] == "missing_or_duplicate_account_identity"


def test_empty_success_is_distinct_from_rpc_failure():
    req, res = fixture()
    res["result"]["value"] = []
    out = assess(req, res)
    assert out["status"] == "no_candidates_observed" and out["returned_accounts"] == 0
    res.update(error={"message": WALLET + " PRIVATE_TEXT"})
    out = assess(req, res)
    assert out["status"] == "unavailable" and out["candidate_lamports"] is None
    assert "PRIVATE_TEXT" not in json.dumps(out) and WALLET not in json.dumps(out)


@pytest.mark.parametrize("offset", [-1, 60_001])
def test_stale_or_future_clock_never_qualifies(offset):
    req, res = fixture()
    out = r.assess(req, res, expected_wallet=WALLET, observed_ms=AT, at_ms=AT+offset)
    assert out["reason"] == "stale_or_future_receipt" and out["candidate_lamports"] is None


def test_mixed_population_retains_every_record_without_guessing_fees():
    req, res = fixture()
    res["result"]["value"].extend(fixture(amount=123, key=10)[1]["result"]["value"])
    res["result"]["value"].extend(fixture(native=1, key=11)[1]["result"]["value"])
    bad = fixture(key=12)[1]["result"]["value"][0]
    bad["account"]["space"] = 999
    res["result"]["value"].append(bad)
    original = copy.deepcopy((req, res))
    out = assess(req, res)
    assert out["returned_accounts"] == 4 and out["candidate_count"] == 1
    assert out["refused_count"] == 3 and out["unknown_count"] == 1
    assert out["candidate_lamports"] == 2_039_280
    assert out["net_recoverable_lamports"] is None
    assert (req, res) == original


@pytest.mark.parametrize("bad", [None, True, "not-an-account", []])
def test_malformed_account_shape_is_counted_unknown(bad):
    req, res = fixture()
    res["result"]["value"][0]["account"] = bad
    out = assess(req, res)
    assert out["unknown_count"] == 1 and out["candidate_count"] == 0
    assert out["refusal_reasons"] == {"malformed_account": 1}


def test_binary_layout_fixture_from_independent_struct_pack():
    import struct

    req, res = fixture()
    raw = struct.pack("<32s32sQ36sB12sQ36s", bytes([3])*32, OWNER_BYTES, 0,
                      bytes(36), 2, bytes(12), 0, struct.pack("<I",1)+OWNER_BYTES)
    res["result"]["value"][0]["account"]["data"][0] = base64.b64encode(raw).decode()
    assert assess(req, res)["candidate_count"] == 1
    res["result"]["value"][0]["account"]["data"][0] = base64.b64encode(
        struct.pack("<32s32sQ36sB12sQ36s", bytes([3])*32, OWNER_BYTES, 2**64-1,
                    bytes(36), 1, bytes(12), 0, bytes(36))).decode()
    assert assess(req, res)["refusal_reasons"] == {"nonzero_token_amount": 1}


def fixture2022(extensions=(), *, suffix=None, **kwargs):
    req, res = fixture(**kwargs)
    req["params"][1]["programId"] = r.TOKEN_2022_PROGRAM
    account = res["result"]["value"][0]["account"]
    account["owner"] = r.TOKEN_2022_PROGRAM
    raw = base64.b64decode(account["data"][0])
    tail = (bytes([2])+b"".join(t.to_bytes(2,"little")+len(v).to_bytes(2,"little")+v
                             for t,v in extensions)) if suffix is None else suffix
    raw += tail
    account["data"][0] = base64.b64encode(raw).decode()
    account["space"] = len(raw)
    return req, res


def assess2022(req, res):
    return assess(req, res, program=r.TOKEN_2022_PROGRAM)


@pytest.mark.parametrize("extensions,suffix", [
    ((), b""), ((), bytes([2])), (((7,b""),), None),
    (((2,bytes(8)),), None), (((7,b""),(2,bytes(8))), None),
])
def test_token2022_known_empty_subset_positive_controls(extensions, suffix):
    req, res = fixture2022(extensions, suffix=suffix)
    out = assess2022(req,res)
    assert out["candidate_count"] == 1 and out["candidate_lamports"] == 2_039_280
    assert out["program"] == r.TOKEN_2022_PROGRAM and out["net_recoverable_lamports"] is None
    assert out["financial_execution"] is False
    assert out["candidates"][0]["extension_types"] == sorted(t for t,_ in extensions)


@pytest.mark.parametrize("withheld", [1, 2**64-1])
def test_withheld_fees_require_other_action_never_faked_as_empty(withheld):
    req, res = fixture2022(((2,withheld.to_bytes(8,"little")),))
    out = assess2022(req,res)
    assert out["candidate_count"] == 0 and out["refusal_reasons"] == {"withheld_transfer_fees": 1}
    assert out["net_recoverable_lamports"] is None
    assert out["unknown_count"] == 0  # known blocker, not unknown or closable capital
    assert out["withheld_fee_account_lamports"] == 2_039_280
    assert out["blocked_accounts"][0]["withheld_atoms"] == str(withheld)
    summary = json.dumps(r.public_summary(out))
    assert res["result"]["value"][0]["pubkey"] not in summary
    assert assess2022(*fixture2022(((2,bytes(8)),)))["candidate_count"] == 1


@pytest.mark.parametrize("extensions,reason", [
    (((5,bytes(64)),), "unassessed_token2022_extension"),
    (((17,bytes(64)),), "unassessed_token2022_extension"),
    (((65535,b""),), "unassessed_token2022_extension"),
    (((1,bytes(8)),), "unassessed_token2022_extension"),  # mint-only extension
    (((2,bytes(7)),), "malformed_tlv"),
    (((7,bytes(1)),), "malformed_tlv"),
    (((7,b""),(7,b"")), "duplicate_extension"),
    (((2,bytes(8)),(2,bytes(8))), "duplicate_extension"),
])
def test_unverified_extensions_stay_unknown_not_zero(extensions,reason):
    out = assess2022(*fixture2022(extensions))
    assert out["status"] == "incomplete" and out["unknown_count"] == 1
    assert out["refusal_reasons"] == {reason: 1}


@pytest.mark.parametrize("suffix,reason", [
    (bytes([1]), "invalid_token2022_account_type_or_size"),
    (bytes([2,7]), "malformed_tlv"),
    (bytes([2,2,0,8,0,1]), "malformed_tlv"),
    (bytes([2])+bytes(189), "invalid_token2022_account_type_or_size"),  # multisig total355
    (bytes([2,0,0,0,0,7,0,0,0]), "unassessed_token2022_extension"),
])
def test_token2022_malformed_type_tlv_or_hidden_tail(suffix,reason):
    out = assess2022(*fixture2022(suffix=suffix))
    assert out["candidate_count"] == 0 and out["refusal_reasons"] == {reason: 1}


def test_program_is_request_bound_and_each_coverage_is_separate():
    req, res = fixture2022(((7,b""),))
    out = assess(req,res)  # default classic cannot absorb a Token-2022 response
    assert out["status"] == "unavailable" and out["reason"] == "request_scope_mismatch"
    req["params"][1]["programId"] = r.TOKEN_PROGRAM
    out = assess(req,res)
    assert out["unknown_count"] == 1 and out["refusal_reasons"] == {"unsupported_program":1}


def test_unknown_extension_census_is_first_per_account_not_complete_types():
    req, res = fixture2022(((15,bytes(1)),(11,bytes(1))))
    other = fixture2022(((17,bytes(64)),), key=11)[1]["result"]["value"][0]
    res["result"]["value"].append(other)
    out = assess2022(req,res)
    assert out["first_unassessed_extension_counts"] == {"15": 1, "17": 1}
    assert out["unknown_count"] == 2 and out["candidate_count"] == 0
    assert "11" not in out["first_unassessed_extension_counts"]  # parser did not claim full census


def test_foreign_owner_not_classified_as_owned_withheld_capital():
    req, res = fixture2022(((2,(1).to_bytes(8,"little")),))
    mutate_bytes(res,32,OTHER)
    out = assess2022(req,res)
    assert out["refusal_reasons"] == {"token_owner_mismatch": 1}
    assert out["withheld_fee_account_lamports"] == 0 and not out["blocked_accounts"]


def test_frozen_token2022_has_its_own_source_and_extension_checks():
    # v10 Token-2022 close path: no frozen-state refusal, but separate closable() checks.
    # https://github.com/solana-program/token-2022/blob/program%40v10.0.0/program/src/processor.rs#L1112-L1168
    assert assess2022(*fixture2022(((2,bytes(8)),),state=2))["candidate_count"] == 1
    assert assess2022(*fixture2022(((2,(1).to_bytes(8,"little")),),state=2))["candidate_count"] == 0
