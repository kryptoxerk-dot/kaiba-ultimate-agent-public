"""Wire-format decoders for the signer: what the bytes it signs actually say.

The signer must judge exactly what it signs. A signer that checks a caller-supplied
*description* and signs caller-supplied *bytes* has two inputs that can disagree, and the
one that moves value is the one nobody checked (design review 2026-10-02, signer.py:96-103
vs 213-226). This module turns those bytes into the view the policy judges:

* :func:`parse_sol_transaction` decodes a serialized Solana transaction (legacy or v0
  message). Instruction accounts, and whether each is a signer or writable, come from the
  message header -- never from the caller. An account loaded from an address lookup table
  is marked unresolved, because the signer must never guess what a table contained.
* :func:`decode_signed_evm_transaction` decodes a signed EVM transaction (EIP-155 legacy
  or EIP-1559), so the signer can confirm that what came out of the signing library is the
  transaction it judged.

Everything here is strict and fails closed: non-canonical length prefixes, trailing bytes,
duplicate accounts, unsupported versions and transaction types are all errors. Nothing in
this module touches key material.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import base58


class WireError(ValueError):
    """The bytes are not a transaction this module will vouch for."""


# --------------------------------------------------------------------------------------
# Solana
# --------------------------------------------------------------------------------------

SIGNATURE_LEN = 64
PUBKEY_LEN = 32


def read_compact_u16(buf: bytes, pos: int) -> tuple[int, int]:
    """Solana ``short_vec`` length. Rejects aliases, as the runtime does."""
    value = 0
    for i in range(3):
        if pos >= len(buf):
            raise WireError("compact_u16_truncated")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << (7 * i)
        if not byte & 0x80:
            if i > 0 and byte == 0:
                raise WireError("compact_u16_non_canonical")
            if i == 2 and byte > 0x03:
                raise WireError("compact_u16_overflow")
            return value, pos
    raise WireError("compact_u16_overflow")


def encode_compact_u16(value: int) -> bytes:
    if not 0 <= value <= 0xFFFF:
        raise WireError("compact_u16_out_of_range")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _take(buf: bytes, pos: int, n: int, what: str) -> tuple[bytes, int]:
    end = pos + n
    if n < 0 or end > len(buf):
        raise WireError(f"{what}_truncated")
    return buf[pos:end], end


def b58(raw: bytes) -> str:
    return base58.b58encode(raw).decode("ascii")


@dataclass(frozen=True)
class SolCompiledInstruction:
    program_index: int
    account_indexes: tuple[int, ...]
    data: bytes


@dataclass(frozen=True)
class SolLookup:
    table: str
    writable_indexes: tuple[int, ...]
    readonly_indexes: tuple[int, ...]


@dataclass(frozen=True)
class SolMessage:
    version: int | None  # None = legacy
    num_required_signatures: int
    num_readonly_signed: int
    num_readonly_unsigned: int
    account_keys: tuple[str, ...]
    recent_blockhash: str
    instructions: tuple[SolCompiledInstruction, ...]
    lookups: tuple[SolLookup, ...]

    @property
    def signer_keys(self) -> tuple[str, ...]:
        return self.account_keys[: self.num_required_signatures]

    def is_writable_static(self, index: int) -> bool:
        n_static = len(self.account_keys)
        if index < self.num_required_signatures:
            return index < self.num_required_signatures - self.num_readonly_signed
        return index < n_static - self.num_readonly_unsigned


@dataclass(frozen=True)
class SolTransaction:
    signatures: tuple[bytes, ...]
    message_bytes: bytes
    message: SolMessage


def parse_sol_message(buf: bytes) -> SolMessage:
    if not buf:
        raise WireError("message_empty")
    pos = 0
    version: int | None = None
    if buf[0] & 0x80:
        version = buf[0] & 0x7F
        if version != 0:
            raise WireError(f"message_version_unsupported:{version}")
        pos = 1
    header, pos = _take(buf, pos, 3, "header")
    n_req, n_ro_signed, n_ro_unsigned = header[0], header[1], header[2]

    n_keys, pos = read_compact_u16(buf, pos)
    keys: list[str] = []
    for _ in range(n_keys):
        raw, pos = _take(buf, pos, PUBKEY_LEN, "account_key")
        keys.append(b58(raw))
    blockhash_raw, pos = _take(buf, pos, PUBKEY_LEN, "recent_blockhash")

    n_ix, pos = read_compact_u16(buf, pos)
    compiled: list[SolCompiledInstruction] = []
    for _ in range(n_ix):
        prog, pos = _take(buf, pos, 1, "program_index")
        n_acc, pos = read_compact_u16(buf, pos)
        accs, pos = _take(buf, pos, n_acc, "account_indexes")
        n_data, pos = read_compact_u16(buf, pos)
        data, pos = _take(buf, pos, n_data, "instruction_data")
        compiled.append(SolCompiledInstruction(prog[0], tuple(accs), bytes(data)))

    lookups: list[SolLookup] = []
    if version == 0:
        n_lookups, pos = read_compact_u16(buf, pos)
        for _ in range(n_lookups):
            table, pos = _take(buf, pos, PUBKEY_LEN, "lookup_table")
            n_w, pos = read_compact_u16(buf, pos)
            w, pos = _take(buf, pos, n_w, "lookup_writable")
            n_r, pos = read_compact_u16(buf, pos)
            r, pos = _take(buf, pos, n_r, "lookup_readonly")
            lookups.append(SolLookup(b58(table), tuple(w), tuple(r)))

    if pos != len(buf):
        raise WireError(f"message_trailing_bytes:{len(buf) - pos}")

    # The runtime's sanitize rules, so we never judge a message the chain would read
    # differently (or not at all).
    if n_req < 1 or n_keys < 1:
        raise WireError("message_has_no_signer")
    if n_req > n_keys or n_req + n_ro_unsigned > n_keys:
        raise WireError("message_header_inconsistent")
    if n_ro_signed >= n_req:
        raise WireError("fee_payer_not_writable")
    if len(set(keys)) != len(keys):
        raise WireError("duplicate_account_keys")
    n_loaded = sum(len(lk.writable_indexes) + len(lk.readonly_indexes) for lk in lookups)
    for idx, ix in enumerate(compiled):
        if not 0 < ix.program_index < n_keys:
            raise WireError(f"program_index_invalid@ix{idx}")
        if any(a >= n_keys + n_loaded for a in ix.account_indexes):
            raise WireError(f"account_index_out_of_range@ix{idx}")

    return SolMessage(
        version=version,
        num_required_signatures=n_req,
        num_readonly_signed=n_ro_signed,
        num_readonly_unsigned=n_ro_unsigned,
        account_keys=tuple(keys),
        recent_blockhash=b58(blockhash_raw),
        instructions=tuple(compiled),
        lookups=tuple(lookups),
    )


def parse_sol_transaction(raw: bytes) -> SolTransaction:
    """Decode ``[compact_u16 n][n x 64-byte signature][message]``. The message is what is signed."""
    n_sigs, pos = read_compact_u16(raw, 0)
    sigs: list[bytes] = []
    for _ in range(n_sigs):
        sig, pos = _take(raw, pos, SIGNATURE_LEN, "signature")
        sigs.append(bytes(sig))
    message_bytes = bytes(raw[pos:])
    message = parse_sol_message(message_bytes)
    if n_sigs != message.num_required_signatures:
        raise WireError(f"signature_count_mismatch:{n_sigs}!={message.num_required_signatures}")
    return SolTransaction(signatures=tuple(sigs), message_bytes=message_bytes, message=message)


def serialize_sol_transaction(signatures: tuple[bytes, ...] | list[bytes], message_bytes: bytes) -> bytes:
    if any(len(s) != SIGNATURE_LEN for s in signatures):
        raise WireError("signature_length_invalid")
    return encode_compact_u16(len(signatures)) + b"".join(signatures) + message_bytes


def sol_instruction_views(message: SolMessage) -> list[dict[str, Any]]:
    """The instruction list the policy judges, derived only from the message bytes.

    ``is_signer`` / ``is_writable`` come from the header. A lookup-table account cannot be
    resolved offline, so it is left as a placeholder and the instruction is marked
    ``resolved_from_alt=False`` -- which the policy refuses.
    """
    keys = message.account_keys
    n_static = len(keys)
    n_writable_loaded = sum(len(lk.writable_indexes) for lk in message.lookups)
    views: list[dict[str, Any]] = []
    for ix in message.instructions:
        accounts: list[str] = []
        signer: list[bool] = []
        writable: list[bool] = []
        resolved = True
        for a in ix.account_indexes:
            if a < n_static:
                accounts.append(keys[a])
                signer.append(a < message.num_required_signatures)
                writable.append(message.is_writable_static(a))
            else:
                resolved = False
                accounts.append(f"<alt:{a - n_static}>")
                signer.append(False)
                writable.append(a - n_static < n_writable_loaded)
        views.append(
            {
                "program_id": keys[ix.program_index],
                "accounts": accounts,
                "data": ix.data,
                "is_signer": signer,
                "is_writable": writable,
                "resolved_from_alt": resolved,
            }
        )
    return views


# --------------------------------------------------------------------------------------
# RLP and signed EVM transactions
# --------------------------------------------------------------------------------------


def _rlp_length(data: bytes, pos: int, n_len: int, limit: int) -> tuple[int, int]:
    start = pos + 1 + n_len
    if start > limit:
        raise WireError("rlp_truncated")
    length_bytes = data[pos + 1 : start]
    if length_bytes[0] == 0:
        raise WireError("rlp_non_canonical_length")
    length = int.from_bytes(length_bytes, "big")
    if length < 56:
        raise WireError("rlp_non_canonical_length")
    return length, start


def _rlp_at(data: bytes, pos: int, limit: int) -> tuple[Any, int]:
    if pos >= limit:
        raise WireError("rlp_truncated")
    b0 = data[pos]
    if b0 < 0x80:
        return bytes([b0]), pos + 1
    if b0 < 0xB8:
        n = b0 - 0x80
        start, end = pos + 1, pos + 1 + n
        if end > limit:
            raise WireError("rlp_truncated")
        if n == 1 and data[start] < 0x80:
            raise WireError("rlp_non_canonical_byte")
        return bytes(data[start:end]), end
    if b0 < 0xC0:
        n, start = _rlp_length(data, pos, b0 - 0xB7, limit)
        end = start + n
        if end > limit:
            raise WireError("rlp_truncated")
        return bytes(data[start:end]), end
    if b0 < 0xF8:
        start, end = pos + 1, pos + 1 + (b0 - 0xC0)
    else:
        n, start = _rlp_length(data, pos, b0 - 0xF7, limit)
        end = start + n
    if end > limit:
        raise WireError("rlp_truncated")
    items: list[Any] = []
    p = start
    while p < end:
        item, p = _rlp_at(data, p, end)
        items.append(item)
    return items, end


def rlp_decode(data: bytes) -> Any:
    item, end = _rlp_at(data, 0, len(data))
    if end != len(data):
        raise WireError("rlp_trailing_bytes")
    return item


def _rlp_uint(item: Any, what: str) -> int:
    if not isinstance(item, bytes):
        raise WireError(f"{what}_not_a_scalar")
    if len(item) > 32:
        raise WireError(f"{what}_too_large")
    if item[:1] == b"\x00":
        raise WireError(f"{what}_non_canonical")
    return int.from_bytes(item, "big")


def _rlp_address(item: Any) -> str:
    if not isinstance(item, bytes) or len(item) != 20:
        # Empty `to` is contract creation: never ours to sign.
        raise WireError("to_not_an_address")
    return "0x" + item.hex()


def decode_signed_evm_transaction(raw: bytes) -> dict[str, Any]:
    """Decode a signed EIP-1559 (type 2) or EIP-155 legacy transaction.

    Every other envelope is refused, including EIP-2930 (type 1), blob (type 3) and
    EIP-7702 set-code (type 4) transactions -- the last of which delegates the account's
    code and is a standing withdrawal capability in all but name.
    """
    if not raw:
        raise WireError("evm_tx_empty")
    if raw[0] == 0x02:
        fields = rlp_decode(raw[1:])
        if not isinstance(fields, list) or len(fields) != 12:
            raise WireError("evm_1559_field_count")
        access_list = fields[8]
        if not isinstance(access_list, list):
            raise WireError("access_list_not_a_list")
        if not isinstance(fields[7], bytes):
            raise WireError("data_not_bytes")
        return {
            "type": 2,
            "chainId": _rlp_uint(fields[0], "chainId"),
            "nonce": _rlp_uint(fields[1], "nonce"),
            "maxPriorityFeePerGas": _rlp_uint(fields[2], "maxPriorityFeePerGas"),
            "maxFeePerGas": _rlp_uint(fields[3], "maxFeePerGas"),
            "gas": _rlp_uint(fields[4], "gas"),
            "to": _rlp_address(fields[5]),
            "value": _rlp_uint(fields[6], "value"),
            "data": "0x" + fields[7].hex(),
            "accessList": access_list,
            "v": _rlp_uint(fields[9], "yParity"),
        }
    if raw[0] >= 0xC0:
        fields = rlp_decode(raw)
        if not isinstance(fields, list) or len(fields) != 9:
            raise WireError("evm_legacy_field_count")
        if not isinstance(fields[5], bytes):
            raise WireError("data_not_bytes")
        v = _rlp_uint(fields[6], "v")
        if v < 35:
            # Pre-EIP-155: no chain id in the signature, replayable on every chain.
            raise WireError("legacy_signature_without_chain_id")
        return {
            "type": 0,
            "chainId": (v - 35) // 2,
            "nonce": _rlp_uint(fields[0], "nonce"),
            "gasPrice": _rlp_uint(fields[1], "gasPrice"),
            "gas": _rlp_uint(fields[2], "gas"),
            "to": _rlp_address(fields[3]),
            "value": _rlp_uint(fields[4], "value"),
            "data": "0x" + fields[5].hex(),
            "accessList": [],
            "v": v,
        }
    raise WireError(f"evm_tx_type_unsupported:{raw[0]:#04x}")
