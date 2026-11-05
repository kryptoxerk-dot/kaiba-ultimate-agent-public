"""Independent binary layout fixtures and accounting counterexamples, no live provider."""

import base64
import copy
import json
import struct
import urllib.error

import base58
import pytest

from kaiba.learning.dlmm_capture import capture
from kaiba.learning.dlmm_replay import compare
from kaiba.learning.dlmm_state import (
    PROGRAM,
    DecodeError,
    bin_array_address,
    decode_bins,
    decode_position,
    snapshot,
)

Q = 2**64
POOL, POSITION, MINT_X, MINT_Y = [base58.b58encode(bytes([i]) * 32).decode() for i in range(1, 5)]


def u128(n):
    return n.to_bytes(16, "little")


def account(raw):
    return {"data": [base64.b64encode(raw).decode(), "base64"], "owner": PROGRAM, "executable": False}


def position_bytes(lo=-1, hi=0, shares=None, pending=7, claimed=0):
    width = hi - lo + 1
    shares = shares if shares is not None else [4 * Q - 1] + [0] * (width - 1)
    fee = u128(0) * 2 + struct.pack("<QQ", pending, 0)
    data = (bytes([117, 176, 212, 199, 245, 180, 133, 182]) + bytes([1]) * 32 + bytes([9]) * 32 +
            b"".join(u128(x) for x in (shares[:70] + [0] * 70)[:70]) + bytes(48 * 70) +
            b"".join(fee if i < width else bytes(48) for i in range(70)) + struct.pack("<iiqQQQQ", lo, hi, 50, claimed, 0, 0, 0) +
            bytes(32) + struct.pack("<Q", 0) + bytes(1) + bytes(32) + bytes([1, 0]) + bytes(85))
    assert len(data) == 8120
    for share in shares[70:]:
        data += u128(share) + bytes(48) + fee
    return data


def bin_bytes(index, amount_x=25, amount_y=50, growth=6 * Q - 1, supply=10 * Q):
    one = (struct.pack("<QQ", amount_x, amount_y) + u128(Q) + u128(supply) + bytes(32) +
           u128(growth) + u128(0) + bytes(8 * 3 + 4 + 1 + 3))
    assert len(one) == 144
    return bytes([92, 142, 92, 220, 5, 148, 70, 181]) + struct.pack("<qB7x", index, 2) + bytes([1]) * 32 + one * 70


def fixture(*, lo=-1, hi=0, shares=None, pending=7, claimed=0, growth=6 * Q - 1):
    pool = bytearray(904)
    pool[:8] = bytes([33, 11, 49, 98, 181, 101, 177, 13])
    struct.pack_into("<iH", pool, 76, -1, 100)
    pool[35], pool[882] = 2, 1
    pool[88:120], pool[120:152] = bytes([3]) * 32, bytes([4]) * 32
    keys = [POOL, POSITION]
    values = [account(pool), account(position_bytes(lo, hi, shares, pending, claimed))]
    for index in range(lo // 70, hi // 70 + 1):
        keys.append(bin_array_address(POOL, index))
        values.append(account(bin_bytes(index, growth=growth)))
    return {"schema": "kaiba-dlmm-capture/1", "pool": POOL, "position": POSITION,
            "started_ms": 1000, "received_ms": 1001,
            "request": {"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
                        "params": [keys, {"encoding": "base64", "commitment": "finalized", "minContextSlot": 99}]},
            "response": {"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 100}, "value": values}}}


def test_raw_accounts_reconcile_shares_and_two_distinct_fee_shifts():
    result = snapshot(fixture())
    # floor((4Q-1)*25/(10Q))=9, not 10. Fee floor((floor((4Q-1)/Q)*(6Q-1))/Q)=17.
    assert result["principal_atoms"] == [9, 19]
    assert result["fee_atoms_gross"] == [31, 0]  # 17 earned + 7 pending in each of two bins
    assert result["position_bins"][0]["fee_atoms_gross"] == [24, 0]
    assert result["position_bins"][1]["fee_atoms_gross"] == [7, 0]  # zero shares still own pending fees
    assert result["strategy_promotable"] is False and result["chain_verified"] is False
    assert "owner" not in result["position"]


def test_extended_position_uses_112_byte_records_and_negative_array_boundaries():
    r = fixture(lo=-71, hi=1, shares=[Q] * 73, pending=0, growth=Q)
    s = snapshot(r)
    assert len(s["position_bins"]) == 73
    assert [s["position_bins"][i]["id"] for i in (0, 70, 72)] == [-71, -1, 1]
    assert s["principal_atoms"] == [146, 365]
    assert s["fee_atoms_gross"] == [73, 0]


@pytest.mark.parametrize("mutation,reason", [
    (lambda r: r["response"].update(id=99), "response"),
    (lambda r: r["response"].update(id=True), "response"),
    (lambda r: r["request"]["params"][0].__setitem__(0, []), "keys"),
    (lambda r: r["request"]["params"][1].update(commitment="processed"), "commitment"),
    (lambda r: r["response"]["result"]["context"].update(slot=98), "slot"),
    (lambda r: r["response"]["result"]["value"].__setitem__(2, None), "owner"),
    (lambda r: r["response"]["result"]["value"][1].update(owner=POOL), "owner"),
    (lambda r: r["response"]["result"]["value"][2].update(executable=True), "owner"),
    (lambda r: r["request"]["params"][0].__setitem__(2, MINT_X), "position_bin_missing"),
    (lambda r: r.update(received_ms=999), "clock"),
])
def test_rejects_unusable_capture_instead_of_zero_balances(mutation, reason):
    r = fixture()
    mutation(r)
    with pytest.raises(DecodeError, match=reason):
        snapshot(r)


def test_foreign_array_and_wrong_pda_refused():
    r = fixture()
    r["response"]["result"]["value"][2] = account(bin_bytes(-2))
    with pytest.raises(DecodeError, match="identity"):
        snapshot(r)


def test_shares_exceeding_supply_and_overflow_refused():
    with pytest.raises(DecodeError, match="share_exceeds"):
        snapshot(fixture(shares=[11 * Q, 0]))
    with pytest.raises(DecodeError, match="fee_overflow"):
        snapshot(fixture(pending=2**64 - 1))


def test_fee_growth_below_checkpoint_is_unknown_not_zero():
    r = fixture()
    a = r["response"]["result"]["value"][1]
    raw = bytearray(base64.b64decode(a["data"][0]))
    raw[4552:4568] = u128(7 * Q)
    r["response"]["result"]["value"][1] = account(raw)
    with pytest.raises(DecodeError, match="below_checkpoint"):
        snapshot(r)


def test_future_layouts_and_truncation_are_not_guessed():
    raw = bytearray(bin_bytes(-1))
    raw[16] = 3
    with pytest.raises(DecodeError, match="version"):
        decode_bins(account(raw))
    with pytest.raises(DecodeError, match="layout"):
        decode_bins(account(bin_bytes(-1)[:-1]))
    with pytest.raises(DecodeError, match="layout"):
        decode_position(account(position_bytes(0, 70, [Q] * 71)[:-1]))


def test_claim_is_not_new_fee_income_and_endpoint_shares_are_not_action_proof():
    first = fixture(pending=7)
    second = fixture(pending=0, claimed=14, growth=7 * Q - 1)
    second.update(started_ms=2000, received_ms=2001)
    second["response"]["result"]["context"]["slot"] = 110
    out = compare([first, second])
    assert out["earned_fee_atoms_gross"] == [3, 0]  # fee growth, not the 14 claimed atoms
    assert out["claimed_fee_delta_atoms_gross"] == [14, 0]
    assert out["intervals"][0]["interpretation"] == "unchanged_endpoint_shares_only"
    assert out["net_pnl"] is None and out["policy_comparison"]["status"] == "unavailable"


def test_changed_shares_require_action_history():
    a, b = fixture(pending=0, growth=0), fixture(shares=[Q, 0], pending=0, growth=0)
    b.update(started_ms=2000, received_ms=2001)
    b["response"]["result"]["context"]["slot"] = 101
    out = compare([a, b])
    assert out["intervals"][0]["shares_changed"]
    assert out["intervals"][0]["interpretation"] == "action_history_required"


@pytest.mark.parametrize("kind", ["same_slot", "backward_clock", "claimed_regression", "entitlement_regression"])
def test_bad_observation_sequence_is_not_a_return(kind):
    a, b = fixture(claimed=5), fixture(claimed=5)
    b.update(started_ms=2000, received_ms=2001)
    b["response"]["result"]["context"]["slot"] = 101
    if kind == "same_slot":
        b["response"]["result"]["context"]["slot"] = 100
    elif kind == "backward_clock":
        b.update(started_ms=0, received_ms=1)
    else:
        replacement = fixture(claimed=4 if kind == "claimed_regression" else 5, pending=0)
        b["response"]["result"]["value"][1] = replacement["response"]["result"]["value"][1]
    with pytest.raises(DecodeError):
        compare([a, b])


def test_two_request_capture_preserves_raw_reply_and_never_overwrites(tmp_path):
    original = fixture()
    known = dict(zip(original["request"]["params"][0], original["response"]["result"]["value"], strict=True))
    for i in (-2, 1):
        known[bin_array_address(POOL, i)] = account(bin_bytes(i))
    known[MINT_X] = known[MINT_Y] = {"mint_fixture": True}
    calls = []

    def transport(request):
        calls.append(copy.deepcopy(request))
        return {"jsonrpc": "2.0", "id": request["id"],
                "result": {"context": {"slot": 100 + len(calls)},
                           "value": [known[k] for k in request["params"][0]]}}

    target = tmp_path / "capture"
    out = capture(POOL, POSITION, target, transport=transport, pause=lambda _: None)
    assert out["status"] == "captured" and len(calls) == 2
    assert calls[1]["params"][1]["minContextSlot"] == 101
    assert (target / "response-2.json").is_file() and (target / "state.json").is_file()
    assert json.loads((target / "result.json").read_text())["response_sha256"] == out["response_sha256"]
    with pytest.raises(FileExistsError):
        capture(POOL, POSITION, target, transport=transport)
    assert len(calls) == 2


def test_throttle_stops_without_retry_or_second_discovery(tmp_path):
    calls = []

    def denied(request):
        calls.append(request)
        raise urllib.error.HTTPError("https://api.mainnet-beta.solana.com", 429, "too many", {}, None)

    out = capture(POOL, POSITION, tmp_path / "failed", transport=denied, pause=lambda _: pytest.fail("unexpected retry"))
    assert out["status"] == "unavailable" and out["http_status"] == 429 and len(calls) == 1
    assert (tmp_path / "failed" / "request-1.json").is_file()
    assert not (tmp_path / "failed" / "state.json").exists()
