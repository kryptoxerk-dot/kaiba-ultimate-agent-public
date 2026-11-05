"""The owner's rent reclaim tool: what it refuses to touch, and that a dry run signs nothing."""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import base58
import pytest
from nacl.signing import SigningKey

path = Path(__file__).parents[1] / "deploy" / "owner" / "close_empty_accounts.py"
spec = importlib.util.spec_from_file_location("close_empty_accounts", path)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

KEY = SigningKey(bytes(range(32)))
WALLET = base58.b58encode(bytes(KEY.verify_key)).decode()
OTHER = base58.b58encode(bytes([9] * 32)).decode()
HASH = base58.b58encode(bytes([7] * 32)).decode()


def address(n: int) -> str:
    return base58.b58encode(bytes([n % 250 + 1] * 32)).decode()


def account(n: int = 1, amount: str = "0", lamports: int = 2_039_280, **info) -> dict:
    base = {"owner": WALLET, "mint": address(200), "state": "initialized", "tokenAmount": {"amount": amount}}
    return {"pubkey": address(n), "account": {"lamports": lamports, "data": {"parsed": {"info": {**base, **info}}}}}


class FakeChain:
    def __init__(self, accounts: list[dict], error=None):
        self.accounts, self.error, self.sent, self.simulated = accounts, error, [], 0

    def call(self, method: str, params: list):
        if method == "getBalance":
            return {"value": 5_000_000}
        if method == "getTokenAccountsByOwner":
            return {"value": self.accounts if params[1]["programId"] == tool.TOKEN else []}
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": HASH}}
        if method == "simulateTransaction":
            self.simulated += 1
            return {"value": {"err": self.error}}
        if method == "sendTransaction":
            self.sent.append(params[0])
            return "sig"
        if method == "getSignatureStatuses":
            return {"value": [{"confirmationStatus": "confirmed", "err": None}]}
        raise AssertionError(method)


@pytest.mark.parametrize("acct, why", [
    (account(amount="1"), "holding"),
    (account(state="frozen"), "frozen"),
    (account(mint=tool.NATIVE_MINT), "wrapped_sol"),
    (account(closeAuthority=OTHER), "other_close_authority"),
    (account(owner=OTHER), "not_owner"),
])
def test_anything_but_a_plain_empty_account_is_left_alone(acct: dict, why: str) -> None:
    keep, skipped = tool.closable([acct], WALLET)
    assert keep == [] and skipped[why] == 1


def test_an_empty_account_is_closable_including_one_the_wallet_itself_controls() -> None:
    keep, _ = tool.closable([account(1), account(2, closeAuthority=WALLET)], WALLET)
    assert [k["lamports"] for k in keep] == [2_039_280, 2_039_280]


def test_a_dry_run_simulates_and_sends_nothing() -> None:
    chain = FakeChain([account(n) for n in range(1, 46)])
    out = tool.run(chain, WALLET, None)
    assert out["closable"] == 45 and out["closed"] == 0 and chain.sent == []
    assert chain.simulated == 3 and out["simulated_ok"] == 3  # batches of 20, 20 and 5


def test_a_failed_simulation_never_sends_even_with_a_key() -> None:
    chain = FakeChain([account(1)], error={"InstructionError": [0, "x"]})
    out = tool.run(chain, WALLET, lambda m: KEY.sign(m).signature)
    assert chain.sent == [] and out["simulated_failed"] == 1 and out["closed"] == 0


def test_an_applied_close_is_signed_by_the_wallet_and_the_rent_comes_back_to_it() -> None:
    chain = FakeChain([account(1), account(2), account(3, amount="5")])
    out = tool.run(chain, WALLET, lambda m: KEY.sign(m).signature)
    assert out["closed"] == 2 and out["reclaimed_sol"] == pytest.approx(0.00407856)
    raw = base64.b64decode(chain.sent[0])
    assert raw[0] == 1
    signature, msg = raw[1:65], raw[65:]
    KEY.verify_key.verify(msg, signature)
    assert msg[:3] == bytes([1, 0, 1])
    assert msg[4:36] == base58.b58decode(WALLET)  # first key: pays, signs, receives
    assert msg.count(bytes([3, 1, 0, 0, 1, tool.CLOSE_ACCOUNT])) == 1  # account 1, destination 0, owner 0


def test_a_full_batch_fits_in_one_transaction() -> None:
    msg = tool.message(WALLET, tool.TOKEN, [address(n) for n in range(tool.BATCH)], HASH)
    assert len(tool.transaction(msg)) <= tool.MAX_TX_BYTES
    with pytest.raises(ValueError, match="too_large"):
        tool.transaction(tool.message(WALLET, tool.TOKEN, [address(n) for n in range(40)], HASH))


@pytest.mark.parametrize("n, encoded", [(0, b"\x00"), (127, b"\x7f"), (128, b"\x80\x01"), (300, b"\xac\x02")])
def test_compact_lengths(n: int, encoded: bytes) -> None:
    assert tool.compact_u16(n) == encoded


def test_a_key_for_another_wallet_is_refused(tmp_path: Path) -> None:
    keyfile = tmp_path / "k.json"
    keyfile.write_text(json.dumps(list(bytes(SigningKey(bytes([1] * 32))))), encoding="utf-8")
    with pytest.raises(SystemExit, match="key_does_not_match_wallet"):
        tool.load_signer(keyfile, WALLET)


@pytest.mark.parametrize("encode", [
    lambda s: json.dumps(list(s)), lambda s: base58.b58encode(s).decode(),
])
def test_both_common_key_formats_load_and_sign(tmp_path: Path, encode) -> None:
    secret = bytes(KEY) + bytes(KEY.verify_key)
    keyfile = tmp_path / "k.txt"
    keyfile.write_text(encode(secret), encoding="utf-8")
    assert tool.load_signer(keyfile, WALLET)(b"m") == KEY.sign(b"m").signature
