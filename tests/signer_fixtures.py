"""Builders for signer tests: real Solana wire bytes, RLP-encoded EVM transactions, a policy.

Not a test module (no ``test_`` prefix). Everything here is independent of the code under
test: the Solana compiler follows the wallet ordering rules from the Solana docs, and the
RLP encoder is the textbook one, so a test that agrees with the signer's decoder is two
implementations agreeing rather than one agreeing with itself.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import base58

from kaiba.execution import policy, signer

SYSTEM = policy.SYSTEM_PROGRAM
COMPUTE_BUDGET = policy.COMPUTE_BUDGET_PROGRAM
TOKEN = policy.TOKEN_PROGRAM

#: The classic eth-account docs vector: this private key controls this address.
KNOWN_EVM_KEY = 0x4C0883A69102937D6231471B5DBB6204FE5129617082792AE468D01A3F362318
KNOWN_EVM_ADDRESS = "0x2c7536E3605D9C16a7a3D7b1898e529396a65c23"

BSC_ROUTER = "0x10ed43c718714eb63d5aa57b78b54704e256024e"
EVM_ATTACKER = "0x00000000000000000000000000000000deadbeef"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
MEME = "0x1111111111111111111111111111111111111111"


def new_pubkey() -> str:
    return base58.b58encode(os.urandom(32)).decode()


def _cu16(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


Meta = tuple[str, bool, bool]  # (pubkey, is_signer, is_writable)
Ix = tuple[str, list[Meta], bytes]  # (program_id, metas, data)


def raw_message(
    header: tuple[int, int, int],
    keys: list[str],
    compiled: list[tuple[int, list[int], bytes]],
    *,
    version: int | None = None,
    lookups: list[tuple[str, list[int], list[int]]] = (),  # type: ignore[assignment]
    blockhash: str | None = None,
) -> bytes:
    out = bytearray()
    if version is not None:
        out.append(0x80 | version)
    out += bytes(header)
    out += _cu16(len(keys))
    for k in keys:
        out += base58.b58decode(k)
    out += base58.b58decode(blockhash or new_pubkey())
    out += _cu16(len(compiled))
    for prog, accs, data in compiled:
        out.append(prog)
        out += _cu16(len(accs)) + bytes(accs)
        out += _cu16(len(data)) + data
    if version is not None:
        out += _cu16(len(lookups))
        for table, w, r in lookups:
            out += base58.b58decode(table)
            out += _cu16(len(w)) + bytes(w)
            out += _cu16(len(r)) + bytes(r)
    return bytes(out)


def compile_message(fee_payer: str, instructions: list[Ix], *, version: int | None = None) -> bytes:
    """Order accounts as a wallet does: payer, signer+writable, signer, writable, readonly."""
    flags: dict[str, list[bool]] = {fee_payer: [True, True]}
    order = [fee_payer]
    for program, metas, _ in instructions:
        for key, is_signer, is_writable in [*metas, (program, False, False)]:
            if key not in flags:
                flags[key] = [False, False]
                order.append(key)
            flags[key][0] |= is_signer
            flags[key][1] |= is_writable

    def bucket(k: str) -> int:
        s, w = flags[k]
        if k == fee_payer:
            return 0
        return 1 if (s and w) else 2 if s else 3 if w else 4

    keys = sorted(order, key=lambda k: (bucket(k), order.index(k)))
    n_req = sum(1 for k in keys if flags[k][0])
    n_ro_signed = sum(1 for k in keys if flags[k][0] and not flags[k][1])
    n_ro_unsigned = sum(1 for k in keys if not flags[k][0] and not flags[k][1])
    compiled = [
        (keys.index(prog), [keys.index(m[0]) for m in metas], data) for prog, metas, data in instructions
    ]
    return raw_message((n_req, n_ro_signed, n_ro_unsigned), keys, compiled, version=version)


def unsigned_tx(message: bytes) -> str:
    """Base64 of the transaction with every signature slot zeroed."""
    n_req = message[1] if message[0] & 0x80 else message[0]
    return base64.b64encode(_cu16(n_req) + bytes(64 * n_req) + message).decode()


def system_transfer(src: str, dst: str, lamports: int) -> Ix:
    return (SYSTEM, [(src, True, True), (dst, False, True)],
            (2).to_bytes(4, "little") + lamports.to_bytes(8, "little"))


def create_account(payer: str, new: str, *, new_signs: bool) -> Ix:
    data = (0).to_bytes(4, "little") + (2_039_280).to_bytes(8, "little") + (165).to_bytes(8, "little")
    data += base58.b58decode(TOKEN)
    return (SYSTEM, [(payer, True, True), (new, new_signs, True)], data)


def cu_price(micro_lamports: int) -> Ix:
    return (COMPUTE_BUDGET, [], bytes([3]) + micro_lamports.to_bytes(8, "little"))


def describe(ix: Ix, **extra: Any) -> dict[str, Any]:
    """The caller-side 'decoded instruction' a request may carry beside its bytes."""
    program, metas, data = ix
    return {"program_id": program, "accounts": [m[0] for m in metas], "data": data.hex(), **extra}


# ---------------------------------------------------------------------------------- EVM


def _ibytes(n: int) -> bytes:
    return n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""


def rlp(item: Any) -> bytes:
    if isinstance(item, list):
        payload = b"".join(rlp(i) for i in item)
        if len(payload) < 56:
            return bytes([0xC0 + len(payload)]) + payload
        ln = _ibytes(len(payload))
        return bytes([0xF7 + len(ln)]) + ln + payload
    if isinstance(item, int):
        item = _ibytes(item)
    if len(item) == 1 and item[0] < 0x80:
        return item
    if len(item) < 56:
        return bytes([0x80 + len(item)]) + item
    ln = _ibytes(len(item))
    return bytes([0xB7 + len(ln)]) + ln + item


def _hx(s: str) -> bytes:
    return bytes.fromhex(s[2:])


def encode_signed(tx: dict[str, Any], *, v: int | None = None) -> bytes:
    """What a signing library returns: type-2 or EIP-155 legacy, with a dummy (r, s)."""
    r = s = 1
    if "gasPrice" in tx:
        vv = v if v is not None else tx["chainId"] * 2 + 35
        return rlp([tx["nonce"], tx["gasPrice"], tx["gas"], _hx(tx["to"]), tx["value"], _hx(tx["data"]), vv, r, s])
    fields = [tx["chainId"], tx["nonce"], tx["maxPriorityFeePerGas"], tx["maxFeePerGas"], tx["gas"],
              _hx(tx["to"]), tx["value"], _hx(tx["data"]), [], v or 0, r, s]
    return b"\x02" + rlp(fields)


class FakeEvmKey:
    """Stands in for eth-account (not installed locally). Optionally a hostile library."""

    def __init__(self, address: str = KNOWN_EVM_ADDRESS,
                 mutate: Callable[[dict[str, Any]], None] | None = None,
                 encode: Callable[[dict[str, Any]], bytes] | None = None) -> None:
        self.address = address
        self.mutate = mutate
        self.encode = encode or encode_signed
        self.received: list[dict[str, Any]] = []

    def sign_transaction(self, tx: dict[str, Any]) -> bytes:
        self.received.append(dict(tx))
        tx = dict(tx)
        if self.mutate:
            self.mutate(tx)
        return self.encode(tx)


class FixedKeystore(signer.Keystore):
    def __init__(self, key: Any) -> None:
        super().__init__(Path("unused"))
        self._fixed = key
        self.loads = 0

    def load(self, chain, wallet):  # type: ignore[override]
        self.loads += 1
        return self._fixed


def word(value: int | str) -> str:
    if isinstance(value, str):
        return value.lower()[2:].rjust(64, "0")
    return format(value, "064x")


def swap_eth_for_tokens(recipient: str) -> str:
    sel = policy.selector_of(policy.EVM_FUNCTIONS["swapExactETHForTokens"])
    return sel + word(1) + word(0x80) + word(recipient) + word(2**32) + word(2) + word(WBNB) + word(MEME)


def erc20_transfer(to: str, amount: int) -> str:
    return policy.selector_of("transfer(address,uint256)") + word(to) + word(amount)


def evm_tx(recipient: str = KNOWN_EVM_ADDRESS, **over: Any) -> dict[str, Any]:
    tx = {
        "chainId": 56, "nonce": 7, "to": BSC_ROUTER, "value": 10**16,
        "data": swap_eth_for_tokens(recipient), "gas": 250_000,
        "maxFeePerGas": 3 * 10**9, "maxPriorityFeePerGas": 10**9,
    }
    tx.update(over)
    return tx


# ---------------------------------------------------------------------------------- policy


def write_policy(path: Path, *, sol_owned: list[str], evm_owned: list[str]) -> Path:
    doc = {
        "version": "v1",
        "owned_addresses": {"sol": sol_owned, "bsc": evm_owned, "robinhood": evm_owned},
        "solana": {
            "allowed_programs": [SYSTEM, COMPUTE_BUDGET, TOKEN, policy.ATA_PROGRAM],
            "jito_tip_accounts": [],
            "max_tip_lamports": 0,
            "max_compute_unit_price_micro_lamports": 1_000_000,
        },
        "evm": {
            "routers": {"bsc": [BSC_ROUTER]},
            "allowed_selectors": ["swapExactETHForTokens", "approve"],
        },
    }
    path.write_text(json.dumps(doc), encoding="utf-8")  # JSON is YAML
    return path
