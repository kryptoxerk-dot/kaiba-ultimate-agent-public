"""The withdrawal gate — the one thing the agent may not do.

EXIT-1 (Codex, scope granted by Claude 2026-09-24): side-aware slippage only;
withdrawal, recipient and signer-policy rules are unchanged.

The owner's mandate (``docs/PLAN.md`` §1, §6.4) is that Hermes has full authority over
research, sizing, strategy and code, and that **exactly one** operation is excluded: value
leaving an agent-controlled wallet to a destination that is not ours. Everything else is a
tunable number in ``config/risk.yaml``. This module is where the exclusion is enforced, and
it is enforced as a classifier over the bytes that are about to be signed — not as a prompt,
a flag, or a policy sentence in a system message.

Three properties make this different from a deny-list:

**Fail closed.** The absence of a matching *allow* rule is a rejection. An instruction whose
program we cannot decode, a selector that is not in the known-function table, a GMGN body
with one extra key, a Solana transaction with unresolved address-lookup-table accounts —
all rejected, because an operation nobody enumerated is exactly where a recipient hides.

**Configuration can only narrow.** ``config/signer-policy.yaml`` selects a subset of what
the code already understands. The Jito tip list is intersected with the eight canonical tip
accounts compiled in below; the Solana program allowlist is intersected with the registry
of programs this decoder can read; the EVM selector allowlist is intersected with the known
functions minus :data:`HARD_DENIED_FUNCTIONS`. Editing the YAML to add ``transfer``, to add
an attacker's address to the tip list, or to raise the tip cap to a whole wallet changes
nothing. The two entries that genuinely grant trust — ``owned_addresses`` and
``evm.routers`` — are operator declarations of what belongs to us, and even those cannot
re-enable a hard-denied operation.

**There is no switch.** Search this file for a boolean that turns withdrawal on. There is
none, and there is no code path that returns ``allowed=True`` for an ERC-20 ``transfer``, a
SystemProgram transfer to a stranger, a ``SetAuthority``, or a bridge deposit.

Known limits, stated rather than hidden:

* Universal Router ``execute(bytes,bytes[],uint256)`` calldata is opaque (research note
  ``08-gating-and-budget-verification.md``). We require an owned address to appear in the
  calldata and flag the call; that is weaker than decoding it, and a full command decoder is
  the follow-up if that lane is ever used at size.
* DEX program instructions (Jupiter, Raydium, PumpSwap, ...) are allowlisted by program id,
  not decoded account-by-account. The destination-token-account check that would catch a
  malicious route lives in the swap builder, which constructs the accounts itself.
* This module signs nothing and moves nothing. It is the gate in front of the signer; the
  real boundary is the OS process boundary in ``deploy/`` that keeps keys out of every
  process an LLM can reach.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from kaiba.core import events
from kaiba.core.config import REPO_ROOT
from kaiba.core.schemas import (
    CHAIN_IDS,
    EVM_CHAINS,
    EVM_ZERO,
    SOL_NATIVE_MINT,
    Chain,
    EventKind,
    Side,
    looks_evm,
    looks_solana,
)

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# keccak-256, so selectors are derived from signatures instead of pasted as magic hex
# --------------------------------------------------------------------------------------

_MASK64 = (1 << 64) - 1
_KECCAK_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)
_KECCAK_ROT = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)


def _rol(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (64 - shift))) & _MASK64 if shift else value


def _keccak_f(a: list[list[int]]) -> None:
    for rnd in range(24):
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                a[x][y] ^= d[x]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _KECCAK_ROT[x][y])
        for x in range(5):
            for y in range(5):
                a[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & _MASK64 & b[(x + 2) % 5][y])
        a[0][0] ^= _KECCAK_RC[rnd]


def keccak256(data: bytes) -> bytes:
    """Keccak-256 (the pre-NIST padding Ethereum uses), pure stdlib.

    ``hashlib.sha3_256`` is not this function: SHA3 pads with ``0x06`` where Keccak pads
    with ``0x01``. We need the Ethereum one to derive function selectors, and the signer
    host must not need a C extension to enforce the policy.
    """
    rate = 136
    a = [[0] * 5 for _ in range(5)]
    padded = bytearray(data) + b"\x01" + b"\x00" * ((-len(data) - 1) % rate)
    padded[-1] ^= 0x80
    for off in range(0, len(padded), rate):
        block = padded[off : off + rate]
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(block[i * 8 : i * 8 + 8], "little")
        _keccak_f(a)
    out = b"".join(a[i % 5][i // 5].to_bytes(8, "little") for i in range(4))
    return out[:32]


def selector_of(signature: str) -> str:
    """``"approve(address,uint256)"`` -> ``"0x095ea7b3"``."""
    return "0x" + keccak256(signature.encode()).hex()[:8]


# --------------------------------------------------------------------------------------
# Solana: the program registry the decoder understands
# --------------------------------------------------------------------------------------

SYSTEM_PROGRAM = "11111111111111111111111111111111"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"

#: DEX / launchpad programs. Allowlisted by id: we do not decode their inner accounts, so
#: the swap builder is responsible for the destination token account it constructs.
DEX_PROGRAMS: dict[str, str] = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "jupiter-v6",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "raydium-amm-v4",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "raydium-cpmm",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "raydium-clmm",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "raydium-launchlab",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pumpswap",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "meteora-dbc",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "meteora-damm-v1",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "meteora-damm-v2",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "meteora-dlmm",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "orca-whirlpool",
}

#: Every program id the policy may allowlist. The YAML selects a subset of this; it cannot
#: add to it, because a program we cannot decode is a program whose transfers we cannot see.
KNOWN_SOLANA_PROGRAMS: frozenset[str] = frozenset(
    {
        SYSTEM_PROGRAM,
        COMPUTE_BUDGET_PROGRAM,
        TOKEN_PROGRAM,
        TOKEN_2022_PROGRAM,
        ATA_PROGRAM,
        MEMO_PROGRAM,
        *DEX_PROGRAMS,
    }
)

#: The eight Jito tip accounts, compiled in. ``jito_tip_accounts`` in the YAML is
#: intersected with this set, so an edit cannot introduce a ninth "tip" destination.
JITO_TIP_ACCOUNTS: frozenset[str] = frozenset(
    {
        "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
        "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
        "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
        "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
        "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
        "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
        "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
        "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
    }
)

#: Absolute ceilings the YAML cannot exceed. 0.1 SOL is already an absurd tip; the p99
#: landed tip on 2026-09-19 was 0.00006 SOL (research 08).
MAX_TIP_LAMPORTS_CEILING = 100_000_000
MAX_CU_PRICE_CEILING = 50_000_000

# SystemProgram instruction indices (little-endian u32 discriminator).
_SYS_CREATE_ACCOUNT = 0
_SYS_ASSIGN = 1
_SYS_TRANSFER = 2
_SYS_CREATE_ACCOUNT_WITH_SEED = 3
_SYS_WITHDRAW_NONCE = 5
_SYS_ALLOCATE = 8
_SYS_ALLOCATE_WITH_SEED = 9
_SYS_ASSIGN_WITH_SEED = 10
_SYS_TRANSFER_WITH_SEED = 11

# SPL Token instruction discriminators (single byte).
_TOK_INIT_ACCOUNT = 1
_TOK_TRANSFER = 3
_TOK_APPROVE = 4
_TOK_REVOKE = 5
_TOK_SET_AUTHORITY = 6
_TOK_MINT_TO = 7
_TOK_BURN = 8
_TOK_CLOSE_ACCOUNT = 9
_TOK_FREEZE = 10
_TOK_THAW = 11
_TOK_TRANSFER_CHECKED = 12
_TOK_APPROVE_CHECKED = 13
_TOK_INIT_ACCOUNT2 = 16
_TOK_SYNC_NATIVE = 17
_TOK_INIT_ACCOUNT3 = 18
_TOK_INIT_IMMUTABLE_OWNER = 22

#: Token instructions that touch nothing we care about. Everything not here and not handled
#: explicitly below is rejected.
_TOK_BENIGN = frozenset(
    {_TOK_INIT_ACCOUNT, _TOK_REVOKE, _TOK_INIT_ACCOUNT2, _TOK_SYNC_NATIVE,
     _TOK_INIT_ACCOUNT3, _TOK_INIT_IMMUTABLE_OWNER}
)

# --------------------------------------------------------------------------------------
# EVM: the function table
# --------------------------------------------------------------------------------------

#: Canonical Permit2, identical on every chain. Always a valid `approve` spender.
PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"

#: name -> signature for every function the decoder understands. A selector outside this
#: table is `selector_unknown` and is refused; there is no "pass through" branch.
EVM_FUNCTIONS: dict[str, str] = {
    # Uniswap v3 SwapRouter02 (no deadline in the struct) and the older SwapRouter.
    "exactInputSingle": "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))",
    "exactInputSingleWithDeadline": (
        "exactInputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))"
    ),
    "exactInput": "exactInput((bytes,address,uint256,uint256))",
    # Uniswap/Pancake v2-style routers.
    "swapExactETHForTokens": "swapExactETHForTokens(uint256,address[],address,uint256)",
    "swapExactETHForTokensSupportingFeeOnTransferTokens": (
        "swapExactETHForTokensSupportingFeeOnTransferTokens(uint256,address[],address,uint256)"
    ),
    "swapExactTokensForETH": "swapExactTokensForETH(uint256,uint256,address[],address,uint256)",
    "swapExactTokensForETHSupportingFeeOnTransferTokens": (
        "swapExactTokensForETHSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)"
    ),
    "swapExactTokensForTokens": "swapExactTokensForTokens(uint256,uint256,address[],address,uint256)",
    "swapExactTokensForTokensSupportingFeeOnTransferTokens": (
        "swapExactTokensForTokensSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)"
    ),
    "unwrapWETH9": "unwrapWETH9(uint256,address)",
    "approve": "approve(address,uint256)",
    "execute": "execute(bytes,bytes[],uint256)",
    "executeNoDeadline": "execute(bytes,bytes[])",
}

#: Functions that are refused no matter what the policy file says, with the reason the
#: alert will carry. This is the list an edit to ``allowed_selectors`` cannot defeat.
HARD_DENIED_FUNCTIONS: dict[str, str] = {
    "transfer(address,uint256)": "erc20_transfer_forbidden",
    "transferFrom(address,address,uint256)": "erc20_transfer_from_forbidden",
    "safeTransferFrom(address,address,uint256)": "erc721_transfer_forbidden",
    "safeTransferFrom(address,address,uint256,bytes)": "erc721_transfer_forbidden",
    "safeTransferFrom(address,address,uint256,uint256,bytes)": "erc1155_transfer_forbidden",
    "setApprovalForAll(address,bool)": "blanket_approval_forbidden",
    "increaseAllowance(address,uint256)": "allowance_creates_standing_withdrawal_capability",
    "permit(address,address,uint256,uint256,uint8,bytes32,bytes32)": (
        "allowance_creates_standing_withdrawal_capability"
    ),
    "delegate(address)": "delegation_creates_standing_withdrawal_capability",
    "withdraw(uint256)": "withdrawal_operation_excluded",
    "withdrawTo(address,uint256)": "withdrawal_operation_excluded",
    "sweepToken(address,uint256,address)": "sweep_moves_value_outside_swap",
    "selfdestruct(address)": "arbitrary_call_forbidden",
    # Bridge deposits: value leaves the chain, which is a withdrawal with extra steps.
    "depositTransaction(address,uint256,uint64,bool,bytes)": "bridge_deposit_forbidden",
    "depositETHTo(address,uint32,bytes)": "bridge_deposit_forbidden",
    "depositERC20To(address,address,address,uint256,uint32,bytes)": "bridge_deposit_forbidden",
    "bridgeERC20To(address,address,uint256,uint32,bytes)": "bridge_deposit_forbidden",
    "depositFor(address,address,bytes)": "bridge_deposit_forbidden",
    "outboundTransfer(address,address,uint256,uint256,uint256,bytes)": "bridge_deposit_forbidden",
    "sendFrom(address,uint16,bytes32,uint256,address,address,bytes)": "bridge_deposit_forbidden",
    "transferTokens(address,uint256,uint16,bytes32,uint256,uint32)": "bridge_deposit_forbidden",
    "relayTokens(address,uint256)": "bridge_deposit_forbidden",
}

SELECTOR_TO_NAME: dict[str, str] = {selector_of(sig): name for name, sig in EVM_FUNCTIONS.items()}
DENIED_SELECTORS: dict[str, str] = {
    selector_of(sig): reason for sig, reason in HARD_DENIED_FUNCTIONS.items()
}
#: Names the YAML may list. Hard-denied functions are not in :data:`EVM_FUNCTIONS` at all,
#: so this is belt and braces against someone adding one there later.
ALLOWABLE_SELECTOR_NAMES: frozenset[str] = frozenset(
    name for name, sig in EVM_FUNCTIONS.items() if selector_of(sig) not in DENIED_SELECTORS
)

_WORD = 32


def _word(args: bytes, index: int) -> bytes | None:
    start = index * _WORD
    return args[start : start + _WORD] if len(args) >= start + _WORD else None


def _address_at(args: bytes, index: int) -> str | None:
    """Read an ABI address word. Rejects dirty upper bytes rather than truncating them."""
    w = _word(args, index)
    if w is None or any(w[:12]):
        return None
    return "0x" + w[12:].hex()


def _uint_at(args: bytes, index: int) -> int | None:
    w = _word(args, index)
    return int.from_bytes(w, "big") if w is not None else None


def _exact_input_recipient(args: bytes) -> str | None:
    """``exactInput((bytes,address,uint256,uint256))`` — one dynamic tuple, so hop the offset."""
    offset = _uint_at(args, 0)
    if offset is None or offset % _WORD or offset > len(args):
        return None
    return _address_at(args[offset:], 1)


#: Where the wallet that receives the output sits, per known function. A function in
#: :data:`EVM_FUNCTIONS` but absent here is allowed only if it has no recipient at all.
RECIPIENT_DECODERS = {
    "exactInputSingle": lambda a: _address_at(a, 3),
    "exactInputSingleWithDeadline": lambda a: _address_at(a, 3),
    "exactInput": _exact_input_recipient,
    "swapExactETHForTokens": lambda a: _address_at(a, 2),
    "swapExactETHForTokensSupportingFeeOnTransferTokens": lambda a: _address_at(a, 2),
    "swapExactTokensForETH": lambda a: _address_at(a, 3),
    "swapExactTokensForETHSupportingFeeOnTransferTokens": lambda a: _address_at(a, 3),
    "swapExactTokensForTokens": lambda a: _address_at(a, 3),
    "swapExactTokensForTokensSupportingFeeOnTransferTokens": lambda a: _address_at(a, 3),
    "unwrapWETH9": lambda a: _address_at(a, 1),
}

_UNIVERSAL_ROUTER = frozenset({"execute", "executeNoDeadline"})

# --------------------------------------------------------------------------------------
# GMGN swap body: the tripwire ported from the money-maker repo
# --------------------------------------------------------------------------------------

#: The exact key set the GMGN swap body must have. Equality, not containment: a body that
#: has lost ``min_output_amount`` is as changed as one that has gained ``beneficiary``.
GMGN_SWAP_BODY_KEYS: frozenset[str] = frozenset(
    {
        "chain",
        "from_address",
        "input_token",
        "output_token",
        "input_amount",
        "min_output_amount",
        "swap_mode",
        "slippage",
        "auto_slippage",
    }
)

#: Substrings that make an unexpected key an alert rather than a shrug. A key containing any
#: of these is where a destination, a fee beneficiary or raw calldata would arrive.
DANGEROUS_KEY_MARKERS = (
    "recipient", "receiver", "destination", "beneficiary", "spender", "delegate",
    "authority", "calldata", "call_data", "raw", "tx", "fee", "tip", "bribe",
    "withdraw", "transfer", "bridge", "to_address", "payout", "refund",
)

_ALLOWED_SWAP_MODES = frozenset({"ExactIn", "ExactOut"})

# --------------------------------------------------------------------------------------
# the closed operation vocabulary
# --------------------------------------------------------------------------------------

#: Operations that can move value and are nevertheless permitted, because they settle in a
#: wallet that is ours. This set has three members and is not configurable.
ALLOWED_VALUE_OPERATIONS: frozenset[str] = frozenset({"swap", "multi_swap", "order_strategy"})

#: Reads. They cannot move value; they are enumerated so the classifier never has to guess.
ALLOWED_READ_OPERATIONS: frozenset[str] = frozenset(
    {
        "quote", "order_get", "order_list", "order_cancel", "gas_price", "config_show",
        "token_info", "token_security", "token_pool", "token_holders", "token_traders",
        "market_kline", "market_trending", "market_trenches", "market_signal",
        "market_hot_searches", "market_search",
        "portfolio_holdings", "portfolio_activity", "portfolio_stats", "portfolio_profits",
        "portfolio_info", "portfolio_token_balance", "portfolio_created_tokens",
        "track_follow_wallet", "track_kol", "track_smartmoney", "track_follow_tokens",
        "track_follow_token_groups",
        "price", "balance", "position", "positions", "status", "dossier", "scan", "grade",
        "journal_read", "events_tail", "risk_read",
        # --- the MCP surface. Every tool Hermes can call must name one of these, so a
    # tool nobody enumerated cannot be registered at all. None of them moves value:
    # they read our own tables or write our own bookkeeping.
    "signals_read",
    "performance_read",
    "playbook_read",
    "opportunities_read",
    "experiments_read",
    "wallets_read",
    "journal_append",
    "experiment_propose",
    "entities_rebuild",
    "hunter_refresh",
    "cohort_set",
    "lane_mode_set",
    "lane_param_set",
    "protection_set",
    "entries_pause",
    "entries_resume",
    "reduce_only_set",
    "exit_request",
    "entry_intent",
}
)

#: Named refusals. Membership changes only the *reason*: an operation in neither allow set
#: is refused just as hard, as ``operation_not_allowlisted``.
DENIED_OPERATIONS: dict[str, str] = {
    "transfer": "transfer_moves_value_outside_swap",
    "transfer_from": "transfer_moves_value_outside_swap",
    "transfer_checked": "transfer_moves_value_outside_swap",
    "send": "transfer_moves_value_outside_swap",
    "send_native": "transfer_moves_value_outside_swap",
    "sweep": "transfer_moves_value_outside_swap",
    "withdraw": "withdrawal_operation_excluded",
    "withdraw_all": "withdrawal_operation_excluded",
    "withdrawal": "withdrawal_operation_excluded",
    "unwrap_and_withdraw": "withdrawal_operation_excluded",
    "bridge": "withdrawal_operation_excluded",
    "cross_chain_transfer": "withdrawal_operation_excluded",
    "approve": "allowance_creates_standing_withdrawal_capability",
    "approve_unlimited": "allowance_creates_standing_withdrawal_capability",
    "increase_allowance": "allowance_creates_standing_withdrawal_capability",
    "set_approval_for_all": "allowance_creates_standing_withdrawal_capability",
    "permit": "allowance_creates_standing_withdrawal_capability",
    "permit2": "allowance_creates_standing_withdrawal_capability",
    "delegate": "delegation_creates_standing_withdrawal_capability",
    "set_authority": "delegation_creates_standing_withdrawal_capability",
    "stake": "custody_transfer_is_not_a_swap",
    "unstake": "custody_transfer_is_not_a_swap",
    "add_liquidity": "liquidity_position_is_not_the_configured_wallet",
    "remove_liquidity": "liquidity_position_is_not_the_configured_wallet",
    "close": "close_moves_rent_or_inventory_out",
    "close_account": "close_moves_rent_or_inventory_out",
    "export_key": "key_export_forbidden",
    "export_private_key": "key_export_forbidden",
    "import_key": "key_export_forbidden",
    "deploy": "arbitrary_call_forbidden",
    "multicall": "arbitrary_call_forbidden",
    "call": "arbitrary_call_forbidden",
    "raw_call": "arbitrary_call_forbidden",
    "sign_message": "arbitrary_signature_forbidden",
    "sign_typed_data": "arbitrary_signature_forbidden",
}

#: Parameter names no permitted operation may carry. The executor sets fees and recipients
#: from configuration; a model that supplies one is doing something else.
FORBIDDEN_PARAM_KEYS: frozenset[str] = frozenset(
    {
        "recipient", "receiver", "destination", "destination_address", "to_address",
        "beneficiary", "output_recipient", "settlement_address", "payout_address",
        "spender", "delegate", "operator", "approved_address", "authority",
        "allowance_target", "calldata", "call_data", "input_data", "raw_transaction",
        "instructions", "fee_recipient", "tip_account", "bribe", "private_key",
        "secret_key", "mnemonic", "keypair",
    }
)


# --------------------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------------------


class WithdrawalBlocked(PermissionError):
    """Raised by :func:`assert_no_withdrawal`. Never caught inside this package."""

    def __init__(self, reason: str, operation: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.operation = operation


class PolicyDecision(BaseModel):
    """The whole answer, including why. ``allowed`` is never inferred from an empty reason."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: str
    findings: list[str] = Field(default_factory=list)

    def __bool__(self) -> bool:  # `if decision:` reads correctly at call sites
        return self.allowed


# --------------------------------------------------------------------------------------
# transaction models
# --------------------------------------------------------------------------------------


class SolInstruction(BaseModel):
    """One decoded Solana instruction, after address-lookup-table resolution.

    ``resolved_from_alt`` is the caller's assertion that every account in ``accounts`` is a
    real pubkey and not an unresolved lookup-table index. We reject on ``False`` rather than
    resolving it ourselves: the signer must never guess what a table contained.
    """

    program_id: str
    accounts: list[str] = Field(default_factory=list)
    data: bytes = b""
    is_signer: list[bool] = Field(default_factory=list)
    is_writable: list[bool] = Field(default_factory=list)
    resolved_from_alt: bool = True

    @field_validator("data", mode="before")
    @classmethod
    def _coerce_data(cls, v: Any) -> Any:
        if isinstance(v, str):
            return bytes.fromhex(v[2:] if v.startswith("0x") else v)
        if isinstance(v, (list, tuple)):
            return bytes(v)
        return v

    def signer(self, index: int) -> bool:
        return bool(self.is_signer[index]) if index < len(self.is_signer) else False

    def writable(self, index: int) -> bool:
        return bool(self.is_writable[index]) if index < len(self.is_writable) else False

    def account(self, index: int) -> str | None:
        return self.accounts[index] if index < len(self.accounts) else None


class EvmTx(BaseModel):
    """One EVM transaction as the planner hands it to the signer."""

    to: str
    value: int = 0
    data: str = "0x"
    chain_id: int | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, v: Any) -> Any:
        return int(v, 16) if isinstance(v, str) and v.startswith("0x") else v


# --------------------------------------------------------------------------------------
# the policy file
# --------------------------------------------------------------------------------------


def _as_str_list(value: Any) -> Any:
    """YAML turns an unquoted all-digit base58 id into an int. Stringify rather than crash:
    a loader that raises takes the whole gate offline, which is worse than a narrow list."""
    if isinstance(value, list):
        return [v if isinstance(v, str) else str(v) for v in value]
    return value


class SolanaPolicy(BaseModel):
    allowed_programs: list[str] = Field(default_factory=list)
    jito_tip_accounts: list[str] = Field(default_factory=list)
    max_tip_lamports: int = 0
    max_compute_unit_price_micro_lamports: int = 0

    @field_validator("allowed_programs", "jito_tip_accounts", mode="before")
    @classmethod
    def _stringify(cls, v: Any) -> Any:
        return _as_str_list(v)

    @property
    def effective_programs(self) -> frozenset[str]:
        """Config narrows the code registry; it can never extend it."""
        return frozenset(p for p in self.allowed_programs if p in KNOWN_SOLANA_PROGRAMS)

    @property
    def effective_tip_accounts(self) -> frozenset[str]:
        return frozenset(a for a in self.jito_tip_accounts if a in JITO_TIP_ACCOUNTS)

    @property
    def effective_max_tip_lamports(self) -> int:
        return max(0, min(int(self.max_tip_lamports), MAX_TIP_LAMPORTS_CEILING))

    @property
    def effective_max_cu_price(self) -> int:
        return max(0, min(int(self.max_compute_unit_price_micro_lamports), MAX_CU_PRICE_CEILING))


class EvmPolicy(BaseModel):
    routers: dict[str, list[str]] = Field(default_factory=dict)
    allowed_selectors: list[str] = Field(default_factory=list)
    max_approval_wei: int | None = None

    def routers_for(self, chain: Chain) -> frozenset[str]:
        raw = self.routers.get(chain.value, [])
        return frozenset(a.strip().lower() for a in raw if looks_evm(a))

    @property
    def effective_selector_names(self) -> frozenset[str]:
        return frozenset(n for n in self.allowed_selectors if n in ALLOWABLE_SELECTOR_NAMES)


class SignerPolicy(BaseModel):
    version: str = "v1"
    owned_addresses: dict[str, list[str]] = Field(default_factory=dict)
    solana: SolanaPolicy = Field(default_factory=SolanaPolicy)
    evm: EvmPolicy = Field(default_factory=EvmPolicy)

    @field_validator("owned_addresses", mode="before")
    @classmethod
    def _stringify_owned(cls, v: Any) -> Any:
        return {k: _as_str_list(val) for k, val in v.items()} if isinstance(v, dict) else v

    def owned(self, chain: Chain) -> frozenset[str]:
        """Addresses the operator has declared ours, normalised per chain.

        EVM is lowercased so a checksummed proposal matches; Solana is left exactly as
        written, because base58 is case-sensitive and lowercasing it would make a
        withdrawal to a look-alike account read as a self-transfer.
        """
        raw = self.owned_addresses.get(chain.value, []) or []
        if chain in EVM_CHAINS:
            return frozenset(a.strip().lower() for a in raw if looks_evm(a))
        return frozenset(a.strip() for a in raw if looks_solana(a))


DEFAULT_POLICY_PATH = REPO_ROOT / "config" / "signer-policy.yaml"


def load_policy(path: Path | None = None) -> SignerPolicy:
    """Read the operator's policy. Not cached, and never written back from this process."""
    p = path or Path(os.environ.get("KAIBA_SIGNER_POLICY_PATH", DEFAULT_POLICY_PATH))
    if not p.exists():
        # No policy file is not "no policy" — an empty policy owns nothing and allows nothing.
        log.warning("signer policy missing at %s; running fully closed", p)
        return SignerPolicy()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return SignerPolicy.model_validate(raw)


def get_policy() -> SignerPolicy:
    return load_policy()


# --------------------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------------------


def _audit(reason: str, payload: dict[str, Any], chain: Chain | None, conn: sqlite3.Connection | None) -> None:
    """Every refusal lands on the bus. Storage failures must not turn a refusal into a pass."""
    try:
        events.emit(
            EventKind.RISK_HALT,
            {"gate": "withdrawal", "reason": reason, **payload},
            chain=chain,
            level="warn",
            conn=conn,
        )
    except Exception as exc:  # pragma: no cover - the refusal stands regardless
        log.warning("policy audit emit failed reason=%s: %s", reason, exc)


def _reject(
    reason: str,
    *,
    findings: list[str] | None = None,
    chain: Chain | None = None,
    payload: dict[str, Any] | None = None,
    conn: sqlite3.Connection | None = None,
) -> PolicyDecision:
    _audit(reason, payload or {}, chain, conn)
    return PolicyDecision(allowed=False, reason=reason, findings=findings or [])


def _allow(reason: str, findings: list[str] | None = None) -> PolicyDecision:
    return PolicyDecision(allowed=True, reason=reason, findings=findings or [])


def same_address(left: object, right: object) -> bool:
    """Compare addresses without widening what counts as equal.

    EVM hex compares case-insensitively; base58 compares exactly. Lower-casing everything
    would be the obvious implementation and would make a Solana withdrawal to a look-alike
    account pass as a self-transfer.
    """
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    if looks_evm(left) != looks_evm(right):
        return False
    return left.lower() == right.lower() if looks_evm(left) else left == right


# --------------------------------------------------------------------------------------
# Solana
# --------------------------------------------------------------------------------------


def _check_system_instruction(
    ix: SolInstruction, owned: frozenset[str], tips: frozenset[str], max_tip: int
) -> tuple[str | None, list[str]]:
    """Return ``(rejection_reason | None, findings)`` for one SystemProgram instruction."""
    if len(ix.data) < 4:
        return "system_instruction_undecodable", []
    kind = int.from_bytes(ix.data[:4], "little")

    if kind == _SYS_TRANSFER:
        dest = ix.account(1)
        if dest is None or len(ix.data) < 12:
            return "system_transfer_undecodable", []
        lamports = int.from_bytes(ix.data[4:12], "little")
        if dest in owned:
            return None, [f"system_transfer_to_owned:{lamports}"]
        if dest in tips:
            if lamports > max_tip:
                return f"jito_tip_above_max:{lamports}>{max_tip}", []
            return None, [f"jito_tip:{lamports}"]
        return f"system_transfer_to_non_owned:{dest}", []

    if kind == _SYS_TRANSFER_WITH_SEED:
        dest = ix.account(2)
        if dest is None:
            return "system_transfer_undecodable", []
        if dest in owned:
            return None, ["system_transfer_with_seed_to_owned"]
        return f"system_transfer_to_non_owned:{dest}", []

    if kind in (_SYS_CREATE_ACCOUNT, _SYS_CREATE_ACCOUNT_WITH_SEED):
        new = ix.account(1)
        if new is None:
            return "system_create_undecodable", []
        # An ephemeral account we sign for (a temporary wSOL account, for example) is ours
        # even though it is not in the policy file.
        if new in owned or ix.signer(1):
            return None, ["system_create_account"]
        return f"system_create_account_not_owned:{new}", []

    if kind in (_SYS_ASSIGN, _SYS_ALLOCATE, _SYS_ALLOCATE_WITH_SEED, _SYS_ASSIGN_WITH_SEED):
        target = ix.account(0)
        if target is None:
            return "system_allocate_undecodable", []
        if target in owned or ix.signer(0):
            return None, ["system_allocate"]
        return f"system_allocate_not_owned:{target}", []

    if kind == _SYS_WITHDRAW_NONCE:
        return "system_withdraw_nonce_forbidden", []

    return f"system_instruction_not_allowed:{kind}", []


def _check_token_instruction(
    ix: SolInstruction, owned: frozenset[str], programs: frozenset[str]
) -> tuple[str | None, list[str]]:
    if not ix.data:
        return "token_instruction_undecodable", []
    disc = ix.data[0]

    if disc in (_TOK_TRANSFER, _TOK_TRANSFER_CHECKED):
        dest = ix.account(2 if disc == _TOK_TRANSFER_CHECKED else 1)
        if dest is None:
            return "token_transfer_undecodable", []
        if dest in owned:
            return None, ["spl_transfer_to_owned"]
        return f"spl_transfer_to_non_owned:{dest}", []

    if disc in (_TOK_APPROVE, _TOK_APPROVE_CHECKED):
        delegate = ix.account(2 if disc == _TOK_APPROVE_CHECKED else 1)
        if delegate is None:
            return "token_approve_undecodable", []
        if delegate in programs or delegate in owned:
            return None, [f"spl_approve:{delegate}"]
        return f"spl_approve_to_non_allowlisted:{delegate}", []

    if disc == _TOK_SET_AUTHORITY:
        return "set_authority_forbidden", []

    if disc == _TOK_CLOSE_ACCOUNT:
        dest = ix.account(1)
        if dest is None:
            return "token_close_undecodable", []
        if dest in owned:
            return None, ["spl_close_account_to_owned"]
        return f"spl_close_account_to_non_owned:{dest}", []

    if disc in (_TOK_MINT_TO, _TOK_BURN, _TOK_FREEZE, _TOK_THAW):
        return f"token_instruction_not_allowed:{disc}", []

    if disc in _TOK_BENIGN:
        return None, [f"spl_housekeeping:{disc}"]

    return f"token_instruction_not_allowed:{disc}", []


def _check_ata_instruction(ix: SolInstruction, owned: frozenset[str]) -> tuple[str | None, list[str]]:
    kind = ix.data[0] if ix.data else 0
    if kind == 2:
        return "ata_recover_nested_forbidden", []
    if kind not in (0, 1):
        return f"ata_instruction_not_allowed:{kind}", []
    owner = ix.account(2)
    if owner is None:
        return "ata_instruction_undecodable", []
    if owner in owned or ix.signer(2):
        return None, ["ata_create"]
    return f"ata_owner_not_owned:{owner}", []


def _check_compute_budget(ix: SolInstruction, max_cu_price: int) -> tuple[str | None, list[str]]:
    if not ix.data:
        return "compute_budget_undecodable", []
    kind = ix.data[0]
    if kind == 3:  # SetComputeUnitPrice(u64 micro-lamports)
        if len(ix.data) < 9:
            return "compute_budget_undecodable", []
        price = int.from_bytes(ix.data[1:9], "little")
        if price > max_cu_price:
            return f"compute_unit_price_above_max:{price}>{max_cu_price}", []
        return None, [f"cu_price:{price}"]
    if kind in (1, 2, 4):
        return None, ["compute_budget"]
    return f"compute_budget_instruction_not_allowed:{kind}", []


def check_solana_transaction(
    instructions: Sequence[SolInstruction],
    *,
    chain: Chain = Chain.SOL,
    policy: SignerPolicy | None = None,
    conn: sqlite3.Connection | None = None,
) -> PolicyDecision:
    """Classify a Solana transaction instruction by instruction.

    Every program id must be allowlisted; a SystemProgram transfer may go only to an owned
    address or an allowlisted Jito tip account within the cap; an SPL transfer may go only to
    a token account we own; ``Approve`` may name only an allowlisted program or an owned
    account; ``SetAuthority``, ``CloseAccount`` to a stranger, and any program or instruction
    the decoder does not recognise are refused.
    """
    policy = policy or get_policy()
    if chain is not Chain.SOL:
        return _reject("chain_not_solana", chain=chain, payload={"chain": chain.value}, conn=conn)
    if not instructions:
        return _reject("empty_transaction", chain=chain, conn=conn)

    owned = policy.owned(chain)
    programs = policy.solana.effective_programs
    tips = policy.solana.effective_tip_accounts
    max_tip = policy.solana.effective_max_tip_lamports
    max_cu = policy.solana.effective_max_cu_price
    findings: list[str] = []

    for idx, ix in enumerate(instructions):
        where = f"ix{idx}"
        if not ix.resolved_from_alt:
            return _reject(
                "alt_unresolved", chain=chain, payload={"index": idx, "program": ix.program_id}, conn=conn
            )
        for pos, acct in enumerate(ix.accounts):
            if not looks_solana(acct):
                return _reject(
                    f"account_unresolved:{where}.{pos}", chain=chain, payload={"index": idx}, conn=conn
                )
        if ix.program_id not in programs:
            reason = (
                f"program_not_allowlisted:{ix.program_id}"
                if ix.program_id in KNOWN_SOLANA_PROGRAMS
                else f"unknown_program:{ix.program_id}"
            )
            return _reject(reason, chain=chain, payload={"index": idx}, conn=conn)

        if ix.program_id == SYSTEM_PROGRAM:
            bad, notes = _check_system_instruction(ix, owned, tips, max_tip)
        elif ix.program_id in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            bad, notes = _check_token_instruction(ix, owned, programs)
        elif ix.program_id == ATA_PROGRAM:
            bad, notes = _check_ata_instruction(ix, owned)
        elif ix.program_id == COMPUTE_BUDGET_PROGRAM:
            bad, notes = _check_compute_budget(ix, max_cu)
        elif ix.program_id == MEMO_PROGRAM:
            bad, notes = None, ["memo"]
        elif ix.program_id in DEX_PROGRAMS:
            # Allowlisted by id. We do not decode the route, so say so out loud.
            bad, notes = None, [f"dex_not_decoded:{DEX_PROGRAMS[ix.program_id]}"]
        else:  # pragma: no cover - KNOWN_SOLANA_PROGRAMS and the branches above are in sync
            bad, notes = f"unknown_program:{ix.program_id}", []

        if bad:
            return _reject(f"{bad}@{where}", findings=findings, chain=chain, payload={"index": idx}, conn=conn)
        findings.extend(f"{where}:{n}" for n in notes)

    return _allow("solana_transaction_settles_in_owned_accounts", findings)


# --------------------------------------------------------------------------------------
# EVM
# --------------------------------------------------------------------------------------


def check_evm_transaction(
    tx: EvmTx,
    *,
    chain: Chain,
    policy: SignerPolicy | None = None,
    conn: sqlite3.Connection | None = None,
) -> PolicyDecision:
    """Classify one EVM transaction by target, selector and decoded recipient.

    ``to`` must be an allowlisted router for the chain — the single exception being
    ``approve``, which is sent to the token contract and is judged on its spender instead.
    The selector must be a known function that is not hard-denied and that the policy lists.
    Where the recipient parameter is decodable it must be an address we own.
    """
    policy = policy or get_policy()
    if chain not in EVM_CHAINS:
        return _reject("chain_not_evm", chain=chain, payload={"chain": chain.value}, conn=conn)
    if not looks_evm(tx.to):
        return _reject("to_invalid", chain=chain, payload={"to": tx.to}, conn=conn)

    to = tx.to.lower()
    owned = policy.owned(chain)
    routers = policy.evm.routers_for(chain)
    allowed_names = policy.evm.effective_selector_names
    findings: list[str] = []

    expected_chain_id = CHAIN_IDS.get(chain)
    if tx.chain_id is not None and expected_chain_id is not None and tx.chain_id != expected_chain_id:
        return _reject(
            f"chain_id_mismatch:{tx.chain_id}!={expected_chain_id}",
            chain=chain,
            payload={"to": to},
            conn=conn,
        )

    raw = tx.data or "0x"
    hexdata = raw[2:] if raw.lower().startswith("0x") else raw
    if hexdata == "":
        if tx.value == 0 and to in owned:
            return _allow("self_call_no_value", ["empty_calldata_to_owned"])
        if tx.value > 0:
            return _reject(
                "native_value_transfer_forbidden", chain=chain, payload={"to": to, "value": tx.value}, conn=conn
            )
        return _reject("empty_calldata_to_non_owned", chain=chain, payload={"to": to}, conn=conn)

    if len(hexdata) < 8:
        return _reject("calldata_truncated", chain=chain, payload={"to": to}, conn=conn)
    try:
        args = bytes.fromhex(hexdata[8:])
    except ValueError:
        return _reject("calldata_not_hex", chain=chain, payload={"to": to}, conn=conn)

    selector = "0x" + hexdata[:8].lower()

    # Hard denials first, so the alert says "bridge" or "erc20 transfer" and not "unknown".
    if selector in DENIED_SELECTORS:
        return _reject(
            f"{DENIED_SELECTORS[selector]}:{selector}", chain=chain, payload={"to": to}, conn=conn
        )

    name = SELECTOR_TO_NAME.get(selector)
    if name is None:
        return _reject(f"selector_unknown:{selector}", chain=chain, payload={"to": to}, conn=conn)
    if name not in allowed_names:
        return _reject(f"selector_not_allowlisted:{name}", chain=chain, payload={"to": to}, conn=conn)

    if name == "approve":
        if tx.value != 0:
            return _reject("approve_with_value", chain=chain, payload={"to": to}, conn=conn)
        spender = _address_at(args, 0)
        amount = _uint_at(args, 1)
        if spender is None or amount is None:
            return _reject("approve_undecodable", chain=chain, payload={"to": to}, conn=conn)
        if spender not in routers and spender != PERMIT2:
            return _reject(
                f"approve_spender_not_allowlisted:{spender}", chain=chain, payload={"to": to}, conn=conn
            )
        cap = policy.evm.max_approval_wei
        if cap is not None and amount > int(cap):
            return _reject(f"approve_above_cap:{amount}", chain=chain, payload={"to": to}, conn=conn)
        if amount >= (1 << 255):
            findings.append("unlimited_approval")
        findings.append(f"approve_target_is_token:{to}")
        return _allow("approve_to_allowlisted_spender", findings)

    if to not in routers:
        return _reject(f"to_not_allowlisted_router:{to}", chain=chain, payload={"selector": name}, conn=conn)
    if tx.value > 0:
        findings.append(f"native_value_to_router:{tx.value}")

    if name in _UNIVERSAL_ROUTER:
        # Opaque command stream. We cannot say where the output lands, so we require our own
        # address to appear somewhere in the calldata and we flag the call as undecoded.
        if not owned:
            return _reject("universal_router_no_owned_address", chain=chain, payload={"to": to}, conn=conn)
        blob = hexdata.lower()
        if not any(a[2:] in blob for a in owned):
            return _reject(
                "recipient_not_found_in_calldata", chain=chain, payload={"to": to, "selector": name}, conn=conn
            )
        findings.append("universal_router_calldata_opaque")
        return _allow("universal_router_with_owned_recipient", findings)

    decoder = RECIPIENT_DECODERS.get(name)
    if decoder is None:
        return _reject(f"recipient_not_decodable:{name}", chain=chain, payload={"to": to}, conn=conn)
    recipient = decoder(args)
    if recipient is None:
        return _reject(f"recipient_undecodable:{name}", chain=chain, payload={"to": to}, conn=conn)
    if recipient not in owned:
        return _reject(
            f"recipient_not_owned:{recipient}", chain=chain, payload={"to": to, "selector": name}, conn=conn
        )
    findings.append(f"recipient_owned:{recipient}")
    return _allow("router_swap_to_owned_recipient", findings)


# --------------------------------------------------------------------------------------
# GMGN swap body
# --------------------------------------------------------------------------------------


def _positive_amount(value: object) -> bool:
    """Accept an int or an exact decimal string. Reject bool, float, sign and zero."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str):
        return value.isdigit() and int(value) > 0
    return False


def check_gmgn_swap_body(
    body: Mapping[str, Any] | str,
    *,
    wallet: str,
    chain: Chain | str,
    side: Side | str | None = None,
    max_slippage_bps: int = 2500,
    max_exit_slippage_bps: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> PolicyDecision:
    """Classify the exact JSON body that is about to be signed and sent to GMGN.

    Ported from the money-maker repo's ``withdrawal_guard.classify_swap_body``. GMGN's API
    has no transfer endpoint, so this is not the primary control — it is the tripwire that
    catches the day a provider-supplied field, a helper that forwards an upstream body, or a
    model with an idea starts adding keys. The key set is compared for **equality**: a body
    that lost ``min_output_amount`` is as changed as one that gained ``beneficiary``.
    """
    chain_value = chain.value if isinstance(chain, Chain) else str(chain)
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return _reject("gmgn_body_unparseable", conn=conn)
    if not isinstance(body, Mapping):
        return _reject("gmgn_body_not_a_mapping", conn=conn)
    if any(not isinstance(k, str) for k in body):
        return _reject("gmgn_body_key_not_a_string", conn=conn)

    keys = set(body)
    extra = sorted(keys - GMGN_SWAP_BODY_KEYS)
    for key in extra:
        low = key.lower()
        if any(marker in low for marker in DANGEROUS_KEY_MARKERS):
            return _reject(f"gmgn_body_forbidden_field:{key}", payload={"chain": chain_value}, conn=conn)
    if extra:
        return _reject(f"gmgn_body_unexpected_field:{extra[0]}", payload={"chain": chain_value}, conn=conn)
    missing = sorted(GMGN_SWAP_BODY_KEYS - keys)
    if missing:
        return _reject(f"gmgn_body_missing_field:{missing[0]}", payload={"chain": chain_value}, conn=conn)

    if body["chain"] != chain_value:
        return _reject(f"gmgn_body_chain_mismatch:{body['chain']}", conn=conn)
    if not same_address(body["from_address"], wallet):
        return _reject("gmgn_body_from_address_not_our_wallet", payload={"chain": chain_value}, conn=conn)
    if body["auto_slippage"] is not False:
        # Letting the provider pick slippage is letting it pick how much of the balance the
        # trade gives away. Not a destination, but the same outcome.
        return _reject("gmgn_body_auto_slippage_forbidden", payload={"chain": chain_value}, conn=conn)
    if body["swap_mode"] not in _ALLOWED_SWAP_MODES:
        return _reject(f"gmgn_body_swap_mode_invalid:{body['swap_mode']}", conn=conn)

    in_tok, out_tok = body["input_token"], body["output_token"]
    if not isinstance(in_tok, str) or not isinstance(out_tok, str) or not in_tok or not out_tok:
        return _reject("gmgn_body_token_invalid", conn=conn)
    if same_address(in_tok, out_tok):
        return _reject("gmgn_body_input_equals_output", conn=conn)
    if same_address(in_tok, wallet) or same_address(out_tok, wallet):
        return _reject("gmgn_body_token_is_our_wallet", conn=conn)

    for field in ("input_amount", "min_output_amount"):
        if not _positive_amount(body[field]):
            return _reject(f"gmgn_body_amount_invalid:{field}", conn=conn)

    slippage = body["slippage"]
    if isinstance(slippage, bool) or not isinstance(slippage, (int, float, str)):
        return _reject("gmgn_body_slippage_invalid", conn=conn)
    try:
        slip = Decimal(str(slippage))
    except (InvalidOperation, ValueError):
        return _reject("gmgn_body_slippage_invalid", conn=conn)
    if not slip.is_finite() or not 0 < slip < 100:
        return _reject(f"gmgn_body_slippage_out_of_range:{slippage}", conn=conn)

    # Side arrives from the Order, never from a provider-controlled body key.
    # Even a SELL label cannot grant the wide ceiling to a native-to-token buy.
    if side is not None and side not in (Side.BUY, Side.SELL):
        return _reject("gmgn_body_side_invalid", conn=conn)
    try:
        ch = Chain(chain_value)
    except ValueError:
        return _reject("gmgn_body_chain_invalid", conn=conn)
    native = SOL_NATIVE_MINT if ch is Chain.SOL else EVM_ZERO
    token_input = looks_solana(in_tok) if ch is Chain.SOL else looks_evm(in_tok)
    proven_exit = (side == Side.SELL and token_input and
                   not same_address(in_tok, native) and same_address(out_tok, native))
    # Bounds are trusted executor context too. The withdrawal gate itself must
    # remain independent of mutable risk configuration.
    try:
        entry_bps = Decimal(str(max_slippage_bps))
        exit_bps = Decimal(str(max_exit_slippage_bps or entry_bps))
        ceiling_bps = max(entry_bps, exit_bps) if proven_exit else entry_bps
        if not ceiling_bps.is_finite() or not 0 < ceiling_bps < 10_000:
            return _reject("gmgn_body_slippage_bound_invalid", conn=conn)
    except (InvalidOperation, ValueError, TypeError):
        return _reject("gmgn_body_slippage_bound_invalid", conn=conn)
    if slip * 100 > ceiling_bps:
        return _reject(f"gmgn_body_slippage_out_of_range:{slippage}", conn=conn)

    return _allow("gmgn_swap_settles_in_our_wallet", [f"slippage_pct:{slippage}"])


# --------------------------------------------------------------------------------------
# the operation vocabulary
# --------------------------------------------------------------------------------------


def assert_no_withdrawal(operation: str, params: dict[str, Any] | None = None) -> None:
    """Raise :class:`WithdrawalBlocked` unless ``operation`` is in the closed vocabulary.

    This is the gate in front of every typed tool the MCP server exposes to Hermes. It is an
    allowlist: an operation nobody enumerated is refused, and no key in any configuration
    file adds a member to :data:`ALLOWED_VALUE_OPERATIONS`.
    """
    params = params or {}
    if not isinstance(operation, str) or not operation:
        _audit("operation_missing", {"operation": repr(operation)}, None, None)
        raise WithdrawalBlocked("operation_missing")
    if operation.strip().lower() != operation:
        # Do not normalise an operation into the allowlist: " Swap " is not `swap`.
        _audit("operation_not_canonical", {"operation": operation}, None, None)
        raise WithdrawalBlocked("operation_not_canonical", operation)

    if not isinstance(params, Mapping):
        _audit("params_not_a_mapping", {"operation": operation}, None, None)
        raise WithdrawalBlocked("params_not_a_mapping", operation)
    for key in sorted(params):
        low = str(key).lower()
        if low in FORBIDDEN_PARAM_KEYS:
            _audit("forbidden_param", {"operation": operation, "param": low}, None, None)
            raise WithdrawalBlocked(f"forbidden_param:{low}", operation)

    if operation in DENIED_OPERATIONS:
        reason = DENIED_OPERATIONS[operation]
        _audit(reason, {"operation": operation}, None, None)
        raise WithdrawalBlocked(reason, operation)
    if operation in ALLOWED_READ_OPERATIONS or operation in ALLOWED_VALUE_OPERATIONS:
        return
    _audit("operation_not_allowlisted", {"operation": operation}, None, None)
    raise WithdrawalBlocked("operation_not_allowlisted", operation)


__all__ = [
    "ALLOWABLE_SELECTOR_NAMES",
    "ALLOWED_READ_OPERATIONS",
    "ALLOWED_VALUE_OPERATIONS",
    "ATA_PROGRAM",
    "COMPUTE_BUDGET_PROGRAM",
    "DEFAULT_POLICY_PATH",
    "DENIED_OPERATIONS",
    "DENIED_SELECTORS",
    "EVM_FUNCTIONS",
    "MAX_TIP_LAMPORTS_CEILING",
    "PERMIT2",
    "SYSTEM_PROGRAM",
    "TOKEN_2022_PROGRAM",
    "TOKEN_PROGRAM",
    "EvmPolicy",
    "EvmTx",
    "GMGN_SWAP_BODY_KEYS",
    "JITO_TIP_ACCOUNTS",
    "KNOWN_SOLANA_PROGRAMS",
    "PolicyDecision",
    "SignerPolicy",
    "SolInstruction",
    "SolanaPolicy",
    "WithdrawalBlocked",
    "assert_no_withdrawal",
    "check_evm_transaction",
    "check_gmgn_swap_body",
    "check_solana_transaction",
    "get_policy",
    "keccak256",
    "load_policy",
    "same_address",
    "selector_of",
]
