"""Meteora DLMM transactions for the LP bot, as bytes. No network, no clock, no key, no send.

Owner 2026-11-04: "Get everything going trading lp". GMGN executes swaps, not liquidity, so live LP
needs its own transactions, and this module is the pure half of that: account orders and encodings.
`kaiba.execution.lp_live` decides when to use them. It has no way to sign or to reach the network,
so nothing here can move money.

**Every instruction comes from the pinned IDL, not from memory.** `INSTRUCTIONS` was generated from
`idls/dlmm.json` at MeteoraAg/dlmm-sdk `af36dd53`, SHA-256 `045c0f4a...d8b35`, the revision
`kaiba.learning.dlmm_state` decodes against. `tests/test_lp_tx.py` holds the table against a subset
of that IDL, and `deploy/research/lp_scalp_audit/simulate_live.py idl` re-fetches the real file,
checks its hash and compares again. Instruction choices follow the same revision's TypeScript
client, which production callers use:

* `add_liquidity_by_strategy2` with `SpotImBalanced` (6): `toStrategyParameters` sends this for a
  Spot shape, with byte 0 of `parameteres` as the favoured side. Each bin holds one token. Y fills up
  to the active bin and X fills above it, or X fills from the active bin up when `favor_x` is set.
* `close_position_if_empty` after `remove_liquidity_by_range2` and `claim_fee2`, the order of its
  `removeLiquidity(shouldClaimAndClose)`. `close_position2` would revert the whole exit on any
  remainder. This one does nothing instead, so the money is out either way and the runner can see
  the position still exists.
* Bin arrays go in remaining accounts, ascending and covering exactly the range, and a swap's go in
  walk order from the liquidity bitmap (`get_bin_array_pubkeys_for_swap`).

**Rent is refused here, in the builder.** A bin array nobody has initialised costs 0.0714 SOL that
never comes back, 7% of a 1 SOL ticket, which is the difference between a viable and an unviable
position (`claude-lp-scalp-audit-20261104.md`). A plan that needs one raises `RentRefused` unless the
caller set `allow_bin_array_rent`, so no path around the check exists in the runner. A bitmap
extension that does not exist is refused for the same reason, with no override.

Hook and transfer-fee mints are out at selection (`lp_paper.Bot.mint_problem`), so
`remaining_accounts_info` is always empty and no amount here needs a transfer-fee adjustment.
"""

from __future__ import annotations

import base64
import binascii
import struct
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import base58

from kaiba.execution.fill_costs import find_program_address
from kaiba.execution.lp_model import PoolState, array_index, arrays_for, bin_price
from kaiba.learning.dlmm_state import PROGRAM, SDK_REVISION, DecodeError, decode_pool, public_key

IDL_SHA256 = "045c0f4af044be046b6a14b350077e4242e8ac026b64a855803dab30fcdd8b35"
__all__ = ["IDL_SHA256", "PROGRAM", "SDK_REVISION"]

SYSTEM = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ASSOCIATED_TOKEN = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
RENT_SYSVAR = "SysvarRent111111111111111111111111111111111"
MEMO = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
NATIVE_MINT = "So11111111111111111111111111111111111111112"
EVENT_AUTHORITY = find_program_address([b"__event_authority"], PROGRAM)[0]

MAX_TX_BYTES = 1_232
#: `initialize_position` takes at most this width (`DEFAULT_BIN_PER_POSITION`); more needs a resize.
MAX_POSITION_WIDTH = 70
#: Array indices the pool's own bitmap covers (`BIN_ARRAY_BITMAP_SIZE`); beyond them, the extension.
BITMAP_SIZE = 512
BPS_ALL = 10_000
#: `StrategyType`, IDL enum order.
SPOT_IMBALANCED = 6
#: Base fee of one signature, in lamports.
BASE_FEE_LAMPORTS = 5_000
#: Enough for a simulation of any path here. The live limit comes from what the simulation used.
SIMULATION_UNITS = 1_400_000
#: Byte offsets in `LbPair` and `BinArrayBitmapExtension`, from the IDL. Tests pin them.
LB_RESERVE_X, LB_RESERVE_Y, LB_ORACLE, LB_BITMAP = 152, 184, 552, 584
LB_BASE_FACTOR, LB_VARIABLE_FEE_CONTROL, LB_BASE_POWER, LB_VOLATILITY = 8, 16, 34, 40
FEE_PRECISION = 1_000_000_000
#: The program's cap on the total fee rate: 10%.
MAX_FEE_RATE = 100_000_000
EXT_DISCRIMINATOR = bytes([80, 111, 124, 113, 55, 237, 18, 5])
EXT_POSITIVE, EXT_WORDS, EXT_ROWS = 40, 8, 12


class LpTxError(ValueError):
    """A request this module cannot turn into a correct transaction."""


class RentRefused(LpTxError):
    """The plan needs a non-refundable account and the caller did not allow it."""


class TxTooLarge(LpTxError):
    """Over the 1,232-byte packet limit. It would not be accepted, so it is never built."""


@dataclass(frozen=True)
class Acc:
    name: str
    writable: bool = False
    signer: bool = False
    optional: bool = False
    address: str | None = None


@dataclass(frozen=True)
class Ix:
    discriminator: bytes
    accounts: tuple[Acc, ...]


#: Generated from the pinned IDL. Do not edit by hand: `tests/test_lp_tx.py` compares it field by field.
INSTRUCTIONS: dict[str, Ix] = {
    "initialize_position": Ix(bytes([219, 192, 234, 71, 190, 191, 102, 80]), (
        Acc("payer", writable=True, signer=True),
        Acc("position", writable=True, signer=True),
        Acc("lb_pair"),
        Acc("owner", signer=True),
        Acc("system_program", address=SYSTEM),
        Acc("rent"),
        Acc("event_authority"),
        Acc("program"),
    )),
    "add_liquidity_by_strategy2": Ix(bytes([3, 221, 149, 218, 111, 141, 118, 213]), (
        Acc("position", writable=True),
        Acc("lb_pair", writable=True),
        Acc("bin_array_bitmap_extension", writable=True, optional=True),
        Acc("user_token_x", writable=True),
        Acc("user_token_y", writable=True),
        Acc("reserve_x", writable=True),
        Acc("reserve_y", writable=True),
        Acc("token_x_mint"),
        Acc("token_y_mint"),
        Acc("sender", signer=True),
        Acc("token_x_program"),
        Acc("token_y_program"),
        Acc("event_authority"),
        Acc("program"),
    )),
    "remove_liquidity_by_range2": Ix(bytes([204, 2, 195, 145, 53, 145, 145, 205]), (
        Acc("position", writable=True),
        Acc("lb_pair", writable=True),
        Acc("bin_array_bitmap_extension", writable=True, optional=True),
        Acc("user_token_x", writable=True),
        Acc("user_token_y", writable=True),
        Acc("reserve_x", writable=True),
        Acc("reserve_y", writable=True),
        Acc("token_x_mint"),
        Acc("token_y_mint"),
        Acc("sender", signer=True),
        Acc("token_x_program"),
        Acc("token_y_program"),
        Acc("memo_program", address=MEMO),
        Acc("event_authority"),
        Acc("program"),
    )),
    "claim_fee2": Ix(bytes([112, 191, 101, 171, 28, 144, 127, 187]), (
        Acc("lb_pair", writable=True),
        Acc("position", writable=True),
        Acc("sender", signer=True),
        Acc("reserve_x", writable=True),
        Acc("reserve_y", writable=True),
        Acc("user_token_x", writable=True),
        Acc("user_token_y", writable=True),
        Acc("token_x_mint"),
        Acc("token_y_mint"),
        Acc("token_program_x"),
        Acc("token_program_y"),
        Acc("memo_program", address=MEMO),
        Acc("event_authority"),
        Acc("program"),
    )),
    "close_position_if_empty": Ix(bytes([59, 124, 212, 118, 91, 152, 110, 157]), (
        Acc("position", writable=True),
        Acc("sender", signer=True),
        Acc("rent_receiver", writable=True),
        Acc("event_authority"),
        Acc("program"),
    )),
    "swap2": Ix(bytes([65, 75, 63, 76, 235, 91, 91, 136]), (
        Acc("lb_pair", writable=True),
        Acc("bin_array_bitmap_extension", writable=True, optional=True),
        Acc("reserve_x", writable=True),
        Acc("reserve_y", writable=True),
        Acc("user_token_in", writable=True),
        Acc("user_token_out", writable=True),
        Acc("token_x_mint"),
        Acc("token_y_mint"),
        Acc("oracle", writable=True),
        Acc("host_fee_in", writable=True, optional=True),
        Acc("user", signer=True),
        Acc("token_x_program"),
        Acc("token_y_program"),
        Acc("memo_program", address=MEMO),
        Acc("event_authority"),
        Acc("program"),
    )),
    "initialize_bin_array": Ix(bytes([35, 86, 19, 185, 78, 212, 75, 211]), (
        Acc("lb_pair"),
        Acc("bin_array", writable=True),
        Acc("funder", writable=True, signer=True),
        Acc("system_program", address=SYSTEM),
    )),
}


@dataclass(frozen=True)
class Meta:
    key: str
    signer: bool = False
    writable: bool = False


@dataclass(frozen=True)
class Instruction:
    program: str
    metas: tuple[Meta, ...]
    data: bytes


@dataclass(frozen=True)
class Message:
    raw: bytes
    signers: tuple[str, ...]

    @property
    def size(self) -> int:
        """Bytes on the wire once signed."""
        return len(compact_u16(len(self.signers))) + 64 * len(self.signers) + len(self.raw)


@dataclass(frozen=True)
class PoolKeys:
    """What a transaction needs from one pool account."""

    address: str
    mint_x: str
    mint_y: str
    reserve_x: str
    reserve_y: str
    oracle: str
    program_x: str
    program_y: str
    active: int
    bin_step: int
    status: int
    rewards_present: bool
    bitmap: tuple[int, ...]
    #: Only when the account exists: `add_liquidity` and `swap` reject a wrong one.
    extension: str | None = None
    #: The fee a swap pays now, from this account. See `total_fee_rate`.
    fee_rate: float = 0.0


# ------------------------------------------------------------------------------------- encoding
def compact_u16(n: int) -> bytes:
    if not 0 <= n < 2**16:
        raise LpTxError("compact_u16_out_of_range")
    out = bytearray()
    while True:
        low = n & 0x7F
        n >>= 7
        if not n:
            out.append(low)
            return bytes(out)
        out.append(low | 0x80)


def _key(address: str) -> bytes:
    try:
        return public_key(address)
    except DecodeError as exc:
        raise LpTxError("invalid_address") from exc


def compile_message(payer: str, instructions: Iterable[Instruction], blockhash: str) -> Message:
    """A legacy message. The payer leads, then the order the runtime requires: signed and writable,
    signed, unsigned and writable, unsigned. Flags on one key are combined across instructions."""
    items = list(instructions)
    flags: dict[str, list[bool]] = {payer: [True, True]}
    for ix in items:
        for meta in ix.metas:
            seen = flags.setdefault(meta.key, [False, False])
            seen[0] |= meta.signer
            seen[1] |= meta.writable
        flags.setdefault(ix.program, [False, False])
    rest = [k for k in flags if k != payer]
    groups = (
        [k for k in rest if flags[k][0] and flags[k][1]],
        [k for k in rest if flags[k][0] and not flags[k][1]],
        [k for k in rest if not flags[k][0] and flags[k][1]],
        [k for k in rest if not flags[k][0] and not flags[k][1]],
    )
    keys = [payer, *groups[0], *groups[1], *groups[2], *groups[3]]
    if len(keys) > 255:
        raise TxTooLarge("too_many_accounts")
    index = {k: i for i, k in enumerate(keys)}
    signers = (payer, *groups[0], *groups[1])
    body = bytes([len(signers), len(groups[1]), len(groups[3])]) + compact_u16(len(keys))
    body += b"".join(_key(k) for k in keys)
    body += _key(blockhash) + compact_u16(len(items))
    for ix in items:
        body += bytes([index[ix.program]]) + compact_u16(len(ix.metas))
        body += bytes(index[m.key] for m in ix.metas) + compact_u16(len(ix.data)) + ix.data
    message = Message(raw=body, signers=signers)
    if message.size > MAX_TX_BYTES:
        raise TxTooLarge(f"transaction_too_large:{message.size}")
    return message


def serialize(message: Message, signatures: Iterable[bytes] | None = None) -> bytes:
    """Wire bytes. Without signatures, zeros stand in: only a `sigVerify: false` simulation takes it."""
    sigs = list(signatures) if signatures is not None else [bytes(64)] * len(message.signers)
    if len(sigs) != len(message.signers) or any(len(s) != 64 for s in sigs):
        raise LpTxError("signature_count_or_size_mismatch")
    return compact_u16(len(sigs)) + b"".join(sigs) + message.raw


def dlmm(name: str, data: bytes, accounts: dict[str, str | None], remaining: Iterable[Meta] = ()) -> Instruction:
    """One program instruction, ordered and flagged by the IDL table. A name the table does not
    know, or a required account left out, raises: a wrong account list is never built."""
    spec = INSTRUCTIONS[name]
    known = {a.name for a in spec.accounts}
    unknown = sorted(set(accounts) - known)
    if unknown:
        raise LpTxError(f"unknown_account:{name}.{unknown[0]}")
    fixed = {"event_authority": EVENT_AUTHORITY, "program": PROGRAM, "rent": RENT_SYSVAR}
    metas = []
    for acc in spec.accounts:
        key = accounts.get(acc.name) or acc.address or fixed.get(acc.name)
        if key is None:
            if not acc.optional:
                raise LpTxError(f"missing_account:{name}.{acc.name}")
            # Anchor's encoding of an absent optional account: the program id, read-only.
            metas.append(Meta(PROGRAM))
            continue
        metas.append(Meta(key, signer=acc.signer, writable=acc.writable))
    return Instruction(PROGRAM, (*metas, *remaining), spec.discriminator + data)


# -------------------------------------------------------------------------------- pool accounts
def decode_keys(account: dict[str, Any], address: str, *, extension: str | None = None) -> PoolKeys:
    """`decode_pool`'s validation, plus the accounts it leaves out. An unknown program flag raises."""
    head = decode_pool(account)
    raw = _bytes(account)
    flags = head["token_program_flags"]
    if any(f not in (0, 1) for f in flags):
        raise DecodeError("unsupported_token_program_flag")
    programs = [TOKEN_2022 if f else TOKEN for f in flags]
    return PoolKeys(
        address=address, mint_x=head["mints"][0], mint_y=head["mints"][1],
        reserve_x=_pubkey(raw, LB_RESERVE_X), reserve_y=_pubkey(raw, LB_RESERVE_Y), oracle=_pubkey(raw, LB_ORACLE),
        program_x=programs[0], program_y=programs[1], active=head["active_bin"], bin_step=head["bin_step"],
        status=head["status"], rewards_present=head["rewards_present"],
        bitmap=struct.unpack_from("<16Q", raw, LB_BITMAP), extension=extension,
        fee_rate=total_fee_rate(raw, head["bin_step"]))


def total_fee_rate(raw: bytes, bin_step: int) -> float:
    """The pool's fee rate now, as `get_total_fee` computes it: base plus variable, capped.

    Read from the chain and not the data API because a minimum out is set from it. The API's dynamic
    fee lags a moving pool, and a figure recorded at selection is hours old on a slow arm. The swap
    itself raises the variable part with each bin it crosses, so this is a floor on what a large
    sale pays, and the ladder of minimums covers the rest.
    """
    base_factor = struct.unpack_from("<H", raw, LB_BASE_FACTOR)[0]
    control = struct.unpack_from("<I", raw, LB_VARIABLE_FEE_CONTROL)[0]
    volatility = struct.unpack_from("<I", raw, LB_VOLATILITY)[0]
    base = base_factor * bin_step * 10 * 10 ** raw[LB_BASE_POWER]
    variable = (control * (volatility * bin_step) ** 2 + 99_999_999_999) // 100_000_000_000 if control else 0
    return min(base + variable, MAX_FEE_RATE) / FEE_PRECISION


def _bytes(account: dict[str, Any]) -> bytes:
    try:
        return base64.b64decode(account["data"][0], validate=True)
    except (KeyError, TypeError, IndexError, binascii.Error, ValueError) as exc:
        raise DecodeError("account_encoding_invalid") from exc


def _pubkey(raw: bytes, offset: int) -> str:
    return base58.b58encode(raw[offset:offset + 32]).decode()


def bitmap_extension_address(pool: str) -> str:
    return find_program_address([b"bitmap", _key(pool)], PROGRAM)[0]


def bin_array_key(pool: str, index: int) -> str:
    return find_program_address([b"bin_array", _key(pool), index.to_bytes(8, "little", signed=True)], PROGRAM)[0]


def ata_address(owner: str, mint: str, token_program: str) -> str:
    return find_program_address([_key(owner), _key(token_program), _key(mint)], ASSOCIATED_TOKEN)[0]


def overflows_bitmap(index: int) -> bool:
    return index > BITMAP_SIZE - 1 or index < -BITMAP_SIZE


def liquid_arrays(bitmap: Iterable[int]) -> frozenset[int]:
    """Array indices the pool's own 1,024-bit bitmap marks as holding liquidity."""
    out = set()
    for word, value in enumerate(bitmap):
        for bit in range(64):
            if value >> bit & 1:
                out.add(word * 64 + bit - BITMAP_SIZE)
    return frozenset(out)


def liquid_arrays_extension(account: dict[str, Any]) -> frozenset[int]:
    """The same for the extension, from `to_bin_array_index`: `(row + 1) * 512 + bit` on the positive
    side, and `-((row + 1) * 512 + bit) - 1` on the negative."""
    raw = _bytes(account)
    if raw[:8] != EXT_DISCRIMINATOR or len(raw) != EXT_POSITIVE + 2 * EXT_ROWS * EXT_WORDS * 8:
        raise DecodeError("bitmap_extension_layout_invalid")
    out = set()
    for side in (0, 1):
        for row in range(EXT_ROWS):
            at = EXT_POSITIVE + (side * EXT_ROWS + row) * EXT_WORDS * 8
            for word, value in enumerate(struct.unpack_from("<8Q", raw, at)):
                for bit in range(64):
                    if value >> bit & 1:
                        n = (row + 1) * BITMAP_SIZE + word * 64 + bit
                        out.add(-n - 1 if side else n)
    return frozenset(out)


def swap_arrays(active: int, swap_for_y: bool, liquid: Iterable[int], take: int = 3) -> list[int]:
    """Up to ``take`` array indices with liquidity, in the order a swap walks them from ``active``."""
    start = array_index(active)
    if swap_for_y:
        return sorted((i for i in liquid if i <= start), reverse=True)[:take]
    return sorted(i for i in liquid if i >= start)[:take]


def quote_buy(y: float, state: PoolState, fee_rate: float) -> tuple[float, float, int]:
    """(token atoms out, lamports unspent, last bin touched) buying up this pool's bins. The mirror
    of `lp_model.quote_sell`, with the same fee rate. It counts only the reserves `decode_bins`
    reads, so limit orders in a bin make it a slight understatement, the safe side for a minimum out."""
    left, out, last = y, 0.0, state.active
    for b in sorted(b for b in state.bins if b >= state.active):
        reserve = float(state.bins[b]["amounts"][0])
        if reserve <= 0 or left <= 0:
            continue
        p = bin_price(b, state.bin_step)
        take = min(left * (1 - fee_rate) / p, reserve)
        out += take
        left -= take * p / (1 - fee_rate)
        last = b
    return out, max(left, 0.0), last


# --------------------------------------------------------------------------- plain instructions
def compute_budget(units: int, micro_lamports: int) -> list[Instruction]:
    if not 0 < units <= SIMULATION_UNITS or micro_lamports < 0:
        raise LpTxError("compute_budget_out_of_range")
    return [Instruction(COMPUTE_BUDGET, (), bytes([2]) + struct.pack("<I", units)),
            Instruction(COMPUTE_BUDGET, (), bytes([3]) + struct.pack("<Q", micro_lamports))]


def priority_micro_lamports(priority_lamports: int, units: int) -> int:
    """The unit price that spends ``priority_lamports`` at this limit. It is charged on the limit
    requested, not on the units used, so the limit must come from a simulation."""
    if units <= 0:
        raise LpTxError("compute_budget_out_of_range")
    return priority_lamports * 1_000_000 // units


def create_ata(payer: str, owner: str, mint: str, token_program: str) -> Instruction:
    """`CreateIdempotent`: a no-op when the account exists, so a retry cannot fail on it."""
    return Instruction(ASSOCIATED_TOKEN, (
        Meta(payer, signer=True, writable=True), Meta(ata_address(owner, mint, token_program), writable=True),
        Meta(owner), Meta(mint), Meta(SYSTEM), Meta(token_program)), bytes([1]))


def wrap_sol(payer: str, account: str, lamports: int) -> list[Instruction]:
    if lamports <= 0:
        raise LpTxError("wrap_amount_not_positive")
    return [Instruction(SYSTEM, (Meta(payer, signer=True, writable=True), Meta(account, writable=True)),
                        struct.pack("<IQ", 2, lamports)),
            Instruction(TOKEN, (Meta(account, writable=True),), bytes([17]))]


def close_token_account(owner: str, account: str, token_program: str = TOKEN) -> Instruction:
    """Unwraps when ``account`` holds wrapped SOL: its balance and rent both go to ``owner``. On any
    other mint a nonzero balance reverts, so it can never destroy a token."""
    return Instruction(token_program, (
        Meta(account, writable=True), Meta(owner, writable=True), Meta(owner, signer=True)), bytes([9]))


# -------------------------------------------------------------------------- program instructions
def initialize_position(payer: str, owner: str, position: str, pool: str, lower: int, width: int) -> Instruction:
    if not 1 <= width <= MAX_POSITION_WIDTH:
        raise LpTxError(f"position_width_unsupported:{width}")
    return dlmm("initialize_position", struct.pack("<ii", lower, width),
                {"payer": payer, "position": position, "lb_pair": pool, "owner": owner})


def initialize_bin_array(pool: str, funder: str, index: int) -> Instruction:
    return dlmm("initialize_bin_array", struct.pack("<q", index),
                {"lb_pair": pool, "bin_array": bin_array_key(pool, index), "funder": funder})


def _array_metas(pool: PoolKeys, lo: int, hi: int) -> tuple[Meta, ...]:
    return tuple(Meta(bin_array_key(pool.address, i), writable=True) for i in arrays_for(lo, hi))


def _liquidity_accounts(pool: PoolKeys, sender: str, position: str) -> dict[str, str | None]:
    return {"position": position, "lb_pair": pool.address, "bin_array_bitmap_extension": pool.extension,
            "user_token_x": ata_address(sender, pool.mint_x, pool.program_x),
            "user_token_y": ata_address(sender, pool.mint_y, pool.program_y),
            "reserve_x": pool.reserve_x, "reserve_y": pool.reserve_y, "token_x_mint": pool.mint_x,
            "token_y_mint": pool.mint_y, "sender": sender, "token_x_program": pool.program_x,
            "token_y_program": pool.program_y}


def add_liquidity_spot(pool: PoolKeys, sender: str, position: str, *, lo: int, hi: int, amount_x: int,
                       amount_y: int, active_id: int, max_slippage: int, favor_x: bool = False) -> Instruction:
    """A Spot deposit. ``active_id`` and ``max_slippage`` are the program's only price bound: it
    reverts unless the pool's real active bin is within ``max_slippage`` of ``active_id``."""
    if lo > hi or amount_x < 0 or amount_y < 0 or amount_x + amount_y == 0 or max_slippage < 0:
        raise LpTxError("deposit_out_of_range")
    params = bytes([1 if favor_x else 0]) + bytes(63)
    data = struct.pack("<QQiiiiB", amount_x, amount_y, active_id, max_slippage, lo, hi, SPOT_IMBALANCED)
    return dlmm("add_liquidity_by_strategy2", data + params + _no_hooks(),
                _liquidity_accounts(pool, sender, position), _array_metas(pool, lo, hi))


def _no_hooks() -> bytes:
    """An empty `RemainingAccountsInfo`. Hook mints never reach a transaction."""
    return struct.pack("<I", 0)


def remove_all_liquidity(pool: PoolKeys, sender: str, position: str, lo: int, hi: int) -> Instruction:
    return dlmm("remove_liquidity_by_range2", struct.pack("<iiH", lo, hi, BPS_ALL) + _no_hooks(),
                _liquidity_accounts(pool, sender, position), _array_metas(pool, lo, hi))


def claim_fee(pool: PoolKeys, sender: str, position: str, lo: int, hi: int) -> Instruction:
    return dlmm("claim_fee2", struct.pack("<ii", lo, hi) + _no_hooks(), {
        "lb_pair": pool.address, "position": position, "sender": sender, "reserve_x": pool.reserve_x,
        "reserve_y": pool.reserve_y, "user_token_x": ata_address(sender, pool.mint_x, pool.program_x),
        "user_token_y": ata_address(sender, pool.mint_y, pool.program_y), "token_x_mint": pool.mint_x,
        "token_y_mint": pool.mint_y, "token_program_x": pool.program_x, "token_program_y": pool.program_y,
    }, _array_metas(pool, lo, hi))


def close_position(sender: str, position: str) -> Instruction:
    return dlmm("close_position_if_empty", b"", {"position": position, "sender": sender, "rent_receiver": sender})


def swap(pool: PoolKeys, user: str, *, swap_for_y: bool, amount_in: int, min_out: int, arrays: Iterable[int],
         token_in: str | None = None, token_out: str | None = None) -> Instruction:
    """``swap_for_y`` sells token for SOL. Arrays must be in walk order; `swap_arrays` gives them.
    ``token_in`` and ``token_out`` default to the user's associated accounts."""
    order = list(arrays)
    if amount_in <= 0 or min_out < 0:
        raise LpTxError("swap_amount_out_of_range")
    if not order:
        raise LpTxError("swap_has_no_liquid_bin_array")
    ata_x = ata_address(user, pool.mint_x, pool.program_x)
    ata_y = ata_address(user, pool.mint_y, pool.program_y)
    return dlmm("swap2", struct.pack("<QQ", amount_in, min_out) + _no_hooks(), {
        "lb_pair": pool.address, "bin_array_bitmap_extension": pool.extension, "reserve_x": pool.reserve_x,
        "reserve_y": pool.reserve_y, "user_token_in": token_in or (ata_x if swap_for_y else ata_y),
        "user_token_out": token_out or (ata_y if swap_for_y else ata_x), "token_x_mint": pool.mint_x,
        "token_y_mint": pool.mint_y, "oracle": pool.oracle, "user": user, "token_x_program": pool.program_x,
        "token_y_program": pool.program_y,
    }, tuple(Meta(bin_array_key(pool.address, i), writable=True) for i in order))


# ------------------------------------------------------------------------------- whole transactions
def entry_bound(active: int, span: int) -> tuple[int, int]:
    """(active_id, max_slippage) for a position strictly below ``active``. The program's bound is
    symmetric, so it is centred ``span`` bins up: the deposit lands while the pool's active bin is
    in ``[active, active + 2 * span]``. One bin lower and the price is inside the planned range,
    which is the paid miss `lp_model.land` models, and the revert is what makes it paid."""
    if span < 0:
        raise LpTxError("entry_bound_out_of_range")
    return active + span, span


def require_arrays(pool: PoolKeys, lo: int, hi: int, missing: Iterable[int], payer: str,
                   allow_bin_array_rent: bool) -> list[Instruction]:
    """Instructions that create the arrays a range needs, which is none on a viable plan. Refuses
    rent by default, and refuses a bitmap extension that does not exist with no override."""
    indices = arrays_for(lo, hi)
    absent = sorted(set(indices) & set(missing))
    if absent and not allow_bin_array_rent:
        raise RentRefused(f"bin_array_rent_refused:{absent}")
    if pool.extension is None and any(overflows_bitmap(i) for i in indices):
        raise RentRefused("bitmap_extension_missing")
    return [initialize_bin_array(pool.address, payer, i) for i in absent]


def _open(pool: PoolKeys, owner: str, payer: str, position: str, *, lo: int, hi: int, amount_x: int, amount_y: int,
          wrap: int, active_id: int, max_slippage: int, missing: Iterable[int], allow_bin_array_rent: bool,
          middle: Iterable[Instruction] = (), deposit_lo: int | None = None, favor_x: bool = False) -> list[Instruction]:
    if pool.mint_y != NATIVE_MINT:
        raise LpTxError("pool_quote_not_sol_y")
    wsol = ata_address(owner, pool.mint_y, pool.program_y)
    out = require_arrays(pool, lo, hi, missing, payer, allow_bin_array_rent)
    out += [create_ata(payer, owner, pool.mint_x, pool.program_x), create_ata(payer, owner, pool.mint_y, pool.program_y)]
    if wrap:
        out += wrap_sol(payer, wsol, wrap)
    out += list(middle)
    out += [initialize_position(payer, owner, position, pool.address, lo, hi - lo + 1),
            add_liquidity_spot(pool, owner, position, lo=lo if deposit_lo is None else deposit_lo, hi=hi, amount_x=amount_x,
                               amount_y=amount_y, active_id=active_id, max_slippage=max_slippage, favor_x=favor_x),
            close_token_account(owner, wsol)]
    return out


def open_one_sided(pool: PoolKeys, owner: str, position: str, *, lo: int, hi: int, lamports: int, active: int,
                   up_bins: int, missing: Iterable[int] = (), allow_bin_array_rent: bool = False,
                   payer: str | None = None) -> list[Instruction]:
    """SOL only, strictly below the active bin: no composition fee and no token held until price
    falls into the range. Create accounts, wrap, open and deposit, in one transaction, so a revert
    leaves no half-open position and no token. Refused if ``hi`` is not below ``active``."""
    if hi >= active:
        raise LpTxError("one_sided_range_touches_active_bin")
    active_id, slippage = entry_bound(active, up_bins)
    return _open(pool, owner, payer or owner, position, lo=lo, hi=hi, amount_x=0, amount_y=lamports, wrap=lamports,
                 active_id=active_id, max_slippage=slippage, missing=missing,
                 allow_bin_array_rent=allow_bin_array_rent)


def open_two_sided(pool: PoolKeys, owner: str, position: str, *, lo: int, hi: int, lamports: int, swap_in: int,
                   swap_min_out: int, arrays: Iterable[int], amount_x: int = 0, active: int, max_slippage: int,
                   missing: Iterable[int] = (), allow_bin_array_rent: bool = False,
                   payer: str | None = None) -> list[Instruction]:
    """Around the price, in one transaction, so a revert cannot leave token without a position.

    With ``swap_in``, that part of ``lamports`` buys token first and ``swap_min_out`` is the token
    deposited; a better fill leaves dust in the wallet, which the sweep sells. ``active`` should be
    where that swap leaves the pool, or the deposit's own bound refuses it. With ``swap_in`` 0 this
    is a re-open: ``amount_x`` of token already in the wallet goes in, with no swap.

    This deposit touches the active bin, so unlike `lp_model.spread` that one bin's share pays a
    composition fee. Skipping it needs two deposits, which does not fit in one packet.
    """
    if not lo <= active <= hi:
        raise LpTxError("two_sided_range_misses_active_bin")
    if swap_in < 0 or swap_in >= lamports:
        raise LpTxError("swap_in_out_of_range")
    middle: list[Instruction] = []
    deposit_x = amount_x
    if swap_in:
        deposit_x = swap_min_out
        middle.append(swap(pool, owner, swap_for_y=False, amount_in=swap_in, min_out=swap_min_out, arrays=arrays))
    return _open(pool, owner, payer or owner, position, lo=lo, hi=hi, amount_x=deposit_x, amount_y=lamports - swap_in,
                 wrap=lamports, active_id=active, max_slippage=max_slippage, missing=missing,
                 allow_bin_array_rent=allow_bin_array_rent, middle=middle)


def open_token_only(pool: PoolKeys, owner: str, position: str, *, lo: int, hi: int, amount_x: int, active: int,
                    down_bins: int, missing: Iterable[int] = (), allow_bin_array_rent: bool = False,
                    payer: str | None = None) -> list[Instruction]:
    """A re-open holding token and no SOL, the usual case: a leg that ended below its range. The
    position spans ``lo`` to ``hi`` around the price, so a further fall inside it is not an exit, and
    the token goes strictly above the active bin, which pays no composition fee. It needs the price
    no higher than ``active`` when it lands: a bound mirroring `entry_bound`, because token cannot sit
    below the active bin."""
    if not lo <= active < hi:
        raise LpTxError("token_only_range_misses_active_bin")
    return _open(pool, owner, payer or owner, position, lo=lo, hi=hi, amount_x=amount_x, amount_y=0, wrap=0,
                 active_id=active - down_bins, max_slippage=down_bins, missing=missing,
                 allow_bin_array_rent=allow_bin_array_rent, deposit_lo=active + 1, favor_x=True)


def exit_position(pool: PoolKeys, owner: str, position: str, *, lo: int | None, hi: int | None,
                  has_shares: bool, payer: str | None = None) -> list[Instruction]:
    """Remove, claim, close, unwrap, in the client's order. ``lo`` to ``hi`` covers only bins that
    hold shares or fees, because the program would otherwise load arrays that may not exist; both
    None means nothing is left to take and only the close remains. It needs no price bound, so it
    lands however far the pool has moved. Leftover token stays in the wallet for `sweep`."""
    payer = payer or owner
    wsol = ata_address(owner, pool.mint_y, pool.program_y)
    out = [create_ata(payer, owner, pool.mint_x, pool.program_x), create_ata(payer, owner, pool.mint_y, pool.program_y)]
    if lo is not None and hi is not None:
        if has_shares:
            out.append(remove_all_liquidity(pool, owner, position, lo, hi))
        out.append(claim_fee(pool, owner, position, lo, hi))
    out += [close_position(owner, position), close_token_account(owner, wsol)]
    return out


def sweep(pool: PoolKeys, owner: str, *, amount_in: int, min_out: int, arrays: Iterable[int], close_token: bool,
          unwrap: bool = False, token_in: str | None = None, payer: str | None = None) -> list[Instruction]:
    """Sell leftover token, unwrap, and when the wallet ends with none, close the token account to
    recover its rent. With ``amount_in`` 0 it does only the cleanup asked for: ``unwrap`` for a
    wrapped balance left behind, ``close_token`` for an empty token account's rent."""
    payer = payer or owner
    wsol = ata_address(owner, pool.mint_y, pool.program_y)
    ata_x = ata_address(owner, pool.mint_x, pool.program_x)
    out: list[Instruction] = []
    if amount_in > 0:
        out += [create_ata(payer, owner, pool.mint_y, pool.program_y),
                swap(pool, owner, swap_for_y=True, amount_in=amount_in, min_out=min_out, arrays=arrays,
                     token_in=token_in, token_out=wsol),
                close_token_account(owner, wsol)]
    elif unwrap:
        out.append(close_token_account(owner, wsol))
    if close_token:
        out.append(close_token_account(owner, ata_x, pool.program_x))
    if not out:
        raise LpTxError("sweep_has_nothing_to_do")
    return out


def finish(instructions: Iterable[Instruction], units: int, priority_lamports: int) -> list[Instruction]:
    """Prepend the compute budget. The last step before compiling, once units are known."""
    return [*compute_budget(units, priority_micro_lamports(priority_lamports, units)), *instructions]
