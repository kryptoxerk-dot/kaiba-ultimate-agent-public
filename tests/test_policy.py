"""The withdrawal gate. This is the most important test file in the repository.

Every test below is one sentence: *this specific way of moving money out is refused*. The
allowed cases exist only to prove the gate is not refusing everything, which would be a
different kind of broken. The mutation tests prove the last property that matters — that no
edit to either YAML file turns any of these refusals into an acceptance.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from kaiba.core.schemas import Chain, EventKind, looks_evm, looks_solana
from kaiba.execution.policy import (
    HARD_DENIED_FUNCTIONS,
    ALLOWABLE_SELECTOR_NAMES,
    ATA_PROGRAM,
    COMPUTE_BUDGET_PROGRAM,
    DEFAULT_POLICY_PATH,
    EVM_FUNCTIONS,
    JITO_TIP_ACCOUNTS,
    KNOWN_SOLANA_PROGRAMS,
    MAX_TIP_LAMPORTS_CEILING,
    SYSTEM_PROGRAM,
    TOKEN_PROGRAM,
    EvmTx,
    SignerPolicy,
    SolInstruction,
    WithdrawalBlocked,
    assert_no_withdrawal,
    check_evm_transaction,
    check_gmgn_swap_body,
    check_solana_transaction,
    keccak256,
    load_policy,
    selector_of,
)

# ------------------------------------------------------------------ fixtures & builders

OUR_SOL = "Bnd5oBSWpPpXoaWTckgXTVx9TCKkX5GyVqxcrwBzUYsB"
OUR_SOL_ATA = "9wFFyRfZBsuAha4YcuxcXLKwMxJR43S7fPfQLusDBzvT"
STRANGER_SOL = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
SOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
JITO_TIP = "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5"
FAKE_TIP = "7Np41oeYqPefeNQEHSv1UDhYrehxin3NStELsSKCT4K2"

JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
RAYDIUM_AMM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
UNKNOWN_PROGRAM = "Drainer11111111111111111111111111111111111x"

OUR_EVM = "0x68eee5c2fe8883a63cd9e5f0e71a3116fb728b3a"
ATTACKER_EVM = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
TOKEN_EVM = "0x4200000000000000000000000000000000000006"
BASE_ROUTER = "0x2626664c2603336e57b271c5c0b26f421741e481"
BASE_V2_ROUTER = "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24"
BASE_UNIVERSAL = "0x6ff5693b99212da76ad316178a184ab56d299b43"
PERMIT2_ADDR = "0x000000000022d473030f116ddee9f6b43ac78ba3"
OPTIMISM_PORTAL = "0x49048044d57e1c92a77f79988d21fa8faf74e97e"


@pytest.fixture(autouse=True)
def _event_db(tmp_path, monkeypatch):
    """Point the event bus at a throwaway database so refusals can be audited safely.

    ``kaiba.core.events`` binds ``get_conn`` at import, so redirecting it via the settings
    environment (not by patching ``db.get_conn``) is what actually moves the writes.
    """
    from kaiba.core import db
    from kaiba.core.config import get_settings

    path = tmp_path / "events.db"
    monkeypatch.setenv("KAIBA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KAIBA_DB_PATH", str(path))
    get_settings.cache_clear()
    db.close_thread_conn()
    conn = db.connect(path)
    db.migrate(conn)
    try:
        yield conn
    finally:
        conn.close()
        db.close_thread_conn()
        get_settings.cache_clear()


@pytest.fixture
def policy() -> SignerPolicy:
    """The shipped policy with our wallets declared. Programs and routers are the real ones."""
    shipped = load_policy(DEFAULT_POLICY_PATH)
    return shipped.model_copy(
        update={
            "owned_addresses": {
                "sol": [OUR_SOL, OUR_SOL_ATA],
                "base": [OUR_EVM],
                "bsc": [OUR_EVM],
                "robinhood": [],
            }
        }
    )


def sol_ix(program: str, accounts: list[str], data: bytes = b"", **kw) -> SolInstruction:
    return SolInstruction(program_id=program, accounts=accounts, data=data, **kw)


def sys_transfer(dest: str, lamports: int, src: str = OUR_SOL) -> SolInstruction:
    data = (2).to_bytes(4, "little") + lamports.to_bytes(8, "little")
    return sol_ix(SYSTEM_PROGRAM, [src, dest], data, is_signer=[True, False], is_writable=[True, True])


def spl_transfer(dest: str, amount: int = 1000, checked: bool = False) -> SolInstruction:
    if checked:
        data = bytes([12]) + amount.to_bytes(8, "little") + bytes([9])
        accounts = [OUR_SOL_ATA, TOKEN_SOL, dest, OUR_SOL]
    else:
        data = bytes([3]) + amount.to_bytes(8, "little")
        accounts = [OUR_SOL_ATA, dest, OUR_SOL]
    return sol_ix(TOKEN_PROGRAM, accounts, data)


def cb_limit(units: int = 400_000) -> SolInstruction:
    return sol_ix(COMPUTE_BUDGET_PROGRAM, [], bytes([2]) + units.to_bytes(4, "little"))


def cb_price(micro_lamports: int = 10_000) -> SolInstruction:
    return sol_ix(COMPUTE_BUDGET_PROGRAM, [], bytes([3]) + micro_lamports.to_bytes(8, "little"))


def ata_create(owner: str = OUR_SOL) -> SolInstruction:
    return sol_ix(
        ATA_PROGRAM,
        [OUR_SOL, OUR_SOL_ATA, owner, TOKEN_SOL, SYSTEM_PROGRAM, TOKEN_PROGRAM],
        bytes([1]),
        is_signer=[True, False, False, False, False, False],
    )


def _abi_addr(address: str) -> bytes:
    return bytes(12) + bytes.fromhex(address[2:])


def _abi_uint(n: int) -> bytes:
    return int(n).to_bytes(32, "big")


def calldata(fn_name: str, *words: bytes) -> str:
    return selector_of(EVM_FUNCTIONS[fn_name]) + b"".join(words).hex()


def raw_calldata(signature: str, *words: bytes) -> str:
    """Calldata for a function that is NOT in the known table (denied or unknown)."""
    return selector_of(signature) + b"".join(words).hex()


def exact_input_single(recipient: str) -> str:
    return calldata(
        "exactInputSingle",
        _abi_addr(TOKEN_EVM),
        _abi_addr(ATTACKER_EVM),  # tokenOut; irrelevant to the recipient rule
        _abi_uint(3000),
        _abi_addr(recipient),
        _abi_uint(10**16),
        _abi_uint(1),
        _abi_uint(0),
    )


def gmgn_body(**overrides) -> dict:
    body = {
        "chain": "sol",
        "from_address": OUR_SOL,
        "input_token": SOL_MINT,
        "output_token": TOKEN_SOL,
        "input_amount": "10000000",
        "min_output_amount": "1",
        "swap_mode": "ExactIn",
        "slippage": 5,
        "auto_slippage": False,
    }
    body.update(overrides)
    return body


# ------------------------------------------------------------------ primitives


def test_keccak256_known_vectors():
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert (
        keccak256(b"abc").hex() == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    )


@pytest.mark.parametrize(
    "signature,expected",
    [
        ("approve(address,uint256)", "0x095ea7b3"),
        ("transfer(address,uint256)", "0xa9059cbb"),
        ("transferFrom(address,address,uint256)", "0x23b872dd"),
        ("swapExactETHForTokens(uint256,address[],address,uint256)", "0x7ff36ab5"),
        ("swapExactTokensForTokens(uint256,uint256,address[],address,uint256)", "0x38ed1739"),
        ("exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))", "0x04e45aaf"),
        ("execute(bytes,bytes[],uint256)", "0x3593564c"),
    ],
)
def test_selectors_match_published_values(signature, expected):
    """If the keccak implementation ever drifts, every EVM rule below becomes nonsense."""
    assert selector_of(signature) == expected


def test_test_fixture_addresses_have_valid_shapes():
    for address in (OUR_SOL, OUR_SOL_ATA, STRANGER_SOL, SOL_MINT, TOKEN_SOL, JITO_TIP, FAKE_TIP):
        assert looks_solana(address), address
    for address in (OUR_EVM, ATTACKER_EVM, TOKEN_EVM, BASE_ROUTER, PERMIT2_ADDR):
        assert looks_evm(address), address
    assert FAKE_TIP not in JITO_TIP_ACCOUNTS
    assert UNKNOWN_PROGRAM not in KNOWN_SOLANA_PROGRAMS


def test_shipped_policy_file_loads_and_narrows():
    """Config may only ever NARROW what the code already permits.

    The `owned_addresses` assertion here used to be `== frozenset()`, on the reasoning
    that the gate ships fully closed until an operator funds a wallet. That was true
    until 2026-09-21, when the operator declared a Solana wallet to arm live trading.
    Asserting emptiness would now just be a reminder that the repo is armed, which the
    config says far more loudly.

    What still must hold, and is what this test is actually for: every allowlist is a
    SUBSET of what the code understands. Editing the YAML can remove capability, never
    add it -- adding `transfer`, a stranger's tip account, or an unknown program changes
    nothing, because the code intersects config against its own compiled-in registry.
    """
    shipped = load_policy(DEFAULT_POLICY_PATH)
    assert shipped.solana.effective_programs <= KNOWN_SOLANA_PROGRAMS
    assert shipped.solana.effective_tip_accounts == JITO_TIP_ACCOUNTS
    assert shipped.evm.effective_selector_names <= ALLOWABLE_SELECTOR_NAMES

    # An owned address is an operator declaration, so it may be present or absent -- but
    # whatever is there must be a plausible address, never a placeholder or empty string
    # that would widen the set by accident.
    for chain in (Chain.SOL, Chain.ROBINHOOD, Chain.BSC, Chain.BASE):
        for address in shipped.owned(chain):
            assert isinstance(address, str) and len(address) >= 32, (
                f"{chain.value} owns a malformed address: {address!r}"
            )


def test_declaring_an_owned_address_does_not_widen_anything_else():
    """Arming a wallet grants exactly one thing: value may settle at that address.

    It must not, as a side effect, admit a withdrawal, a new program, or a new selector.
    This is the test that would catch an edit to `owned_addresses` being used as a way in.
    """
    shipped = load_policy(DEFAULT_POLICY_PATH)
    assert shipped.solana.effective_programs <= KNOWN_SOLANA_PROGRAMS
    assert shipped.evm.effective_selector_names <= ALLOWABLE_SELECTOR_NAMES
    for denied in HARD_DENIED_FUNCTIONS:
        assert denied not in shipped.evm.effective_selector_names, (
            f"{denied} is hard-denied and must never appear in an effective allowlist"
        )


def test_missing_policy_file_owns_nothing(tmp_path):
    empty = load_policy(tmp_path / "does-not-exist.yaml")
    assert empty.owned(Chain.SOL) == frozenset()
    assert empty.solana.effective_programs == frozenset()
    decision = check_solana_transaction([cb_limit()], policy=empty)
    assert not decision.allowed and "unknown_program" in decision.reason or not decision.allowed


# ------------------------------------------------------------------ Solana: refusals


def test_reject_system_transfer_to_stranger(policy):
    """The plain withdrawal: lamports to an address that is not ours and not a tip account."""
    decision = check_solana_transaction([sys_transfer(STRANGER_SOL, 500_000_000)], policy=policy)
    assert not decision.allowed
    assert decision.reason.startswith("system_transfer_to_non_owned")
    assert STRANGER_SOL in decision.reason


def test_reject_spl_transfer_to_non_owned_token_account(policy):
    decision = check_solana_transaction([spl_transfer(STRANGER_SOL)], policy=policy)
    assert not decision.allowed
    assert decision.reason.startswith("spl_transfer_to_non_owned")


def test_reject_spl_transfer_checked_to_non_owned_token_account(policy):
    decision = check_solana_transaction([spl_transfer(STRANGER_SOL, checked=True)], policy=policy)
    assert not decision.allowed
    assert decision.reason.startswith("spl_transfer_to_non_owned")


def test_reject_set_authority(policy):
    """Handing mint or account authority away is a withdrawal with a delay."""
    ix = sol_ix(TOKEN_PROGRAM, [OUR_SOL_ATA, OUR_SOL], bytes([6, 2, 1]) + bytes(32))
    decision = check_solana_transaction([ix], policy=policy)
    assert not decision.allowed
    assert decision.reason.startswith("set_authority_forbidden")


def test_reject_close_account_to_non_owned_destination(policy):
    ix = sol_ix(TOKEN_PROGRAM, [OUR_SOL_ATA, STRANGER_SOL, OUR_SOL], bytes([9]))
    decision = check_solana_transaction([ix], policy=policy)
    assert not decision.allowed
    assert decision.reason.startswith("spl_close_account_to_non_owned")


def test_reject_unknown_program_id(policy):
    ix = sol_ix(UNKNOWN_PROGRAM, [OUR_SOL], b"\x00")
    decision = check_solana_transaction([ix], policy=policy)
    assert not decision.allowed
    assert decision.reason.startswith("unknown_program")


def test_reject_unresolved_address_lookup_table(policy):
    """A lookup table we did not resolve is a set of accounts we did not read."""
    ix = sol_ix(JUPITER_V6, [OUR_SOL, OUR_SOL_ATA], b"\x01", resolved_from_alt=False)
    decision = check_solana_transaction([cb_limit(), ix], policy=policy)
    assert not decision.allowed
    assert decision.reason == "alt_unresolved"


def test_reject_tip_above_cap(policy):
    decision = check_solana_transaction([sys_transfer(JITO_TIP, 5 * 10**9)], policy=policy)
    assert not decision.allowed
    assert decision.reason.startswith("jito_tip_above_max")


def test_reject_tip_to_address_that_is_not_a_real_tip_account(policy):
    decision = check_solana_transaction([sys_transfer(FAKE_TIP, 1000)], policy=policy)
    assert not decision.allowed
    assert decision.reason.startswith("system_transfer_to_non_owned")


def test_reject_spl_approve_to_unknown_delegate(policy):
    ix = sol_ix(TOKEN_PROGRAM, [OUR_SOL_ATA, STRANGER_SOL, OUR_SOL], bytes([4]) + (2**63).to_bytes(8, "little"))
    decision = check_solana_transaction([ix], policy=policy)
    assert not decision.allowed
    assert decision.reason.startswith("spl_approve_to_non_allowlisted")


def test_reject_compute_unit_price_above_cap(policy):
    decision = check_solana_transaction([cb_price(999_000_000)], policy=policy)
    assert not decision.allowed
    assert decision.reason.startswith("compute_unit_price_above_max")


def test_reject_system_nonce_withdrawal(policy):
    ix = sol_ix(SYSTEM_PROGRAM, [OUR_SOL, STRANGER_SOL], (5).to_bytes(4, "little") + bytes(8))
    decision = check_solana_transaction([ix], policy=policy)
    assert not decision.allowed
    assert decision.reason.startswith("system_withdraw_nonce_forbidden")


def test_reject_ata_recover_nested(policy):
    ix = sol_ix(ATA_PROGRAM, [OUR_SOL, OUR_SOL_ATA, OUR_SOL, TOKEN_SOL, SYSTEM_PROGRAM, TOKEN_PROGRAM], bytes([2]))
    decision = check_solana_transaction([ix], policy=policy)
    assert not decision.allowed
    assert decision.reason.startswith("ata_recover_nested_forbidden")


def test_reject_solana_transaction_on_an_evm_chain(policy):
    decision = check_solana_transaction([cb_limit()], chain=Chain.BASE, policy=policy)
    assert not decision.allowed and decision.reason == "chain_not_solana"


# ------------------------------------------------------------------ Solana: allowed


def test_allow_jupiter_swap_with_jito_tip(policy):
    """The real shape of a Solana entry: budget, ATA, route, tip."""
    tx = [
        cb_limit(),
        cb_price(50_000),
        ata_create(),
        sol_ix(JUPITER_V6, [OUR_SOL, OUR_SOL_ATA, TOKEN_SOL], b"\xe5\x17\xcb\x97\x7a\xe3\xad\x2a"),
        sys_transfer(JITO_TIP, 1_000_000),
    ]
    decision = check_solana_transaction(tx, policy=policy)
    assert decision.allowed, decision.reason
    assert any("jito_tip:1000000" in f for f in decision.findings)
    assert any("dex_not_decoded:jupiter-v6" in f for f in decision.findings)


def test_allow_raydium_swap(policy):
    tx = [
        cb_limit(),
        sol_ix(RAYDIUM_AMM, [TOKEN_PROGRAM, OUR_SOL_ATA, OUR_SOL], bytes([9]) + bytes(16)),
        sol_ix(TOKEN_PROGRAM, [OUR_SOL_ATA], bytes([17])),  # SyncNative
    ]
    decision = check_solana_transaction(tx, policy=policy)
    assert decision.allowed, decision.reason
    assert any("raydium-amm-v4" in f for f in decision.findings)


def test_allow_spl_transfer_into_our_own_token_account(policy):
    decision = check_solana_transaction([spl_transfer(OUR_SOL_ATA)], policy=policy)
    assert decision.allowed, decision.reason


def test_allow_close_account_back_to_our_wallet(policy):
    ix = sol_ix(TOKEN_PROGRAM, [OUR_SOL_ATA, OUR_SOL, OUR_SOL], bytes([9]))
    assert check_solana_transaction([ix], policy=policy).allowed


def test_allow_system_create_of_an_ephemeral_account_we_sign_for(policy):
    """A temporary wSOL account is ours even though it is not in the policy file."""
    ix = sol_ix(
        SYSTEM_PROGRAM,
        [OUR_SOL, STRANGER_SOL],
        (0).to_bytes(4, "little") + bytes(8) + bytes(8) + bytes(32),
        is_signer=[True, True],
    )
    assert check_solana_transaction([ix], policy=policy).allowed


def test_empty_solana_transaction_is_refused(policy):
    decision = check_solana_transaction([], policy=policy)
    assert not decision.allowed and decision.reason == "empty_transaction"


# ------------------------------------------------------------------ EVM: refusals


def test_reject_erc20_transfer_to_attacker(policy):
    tx = EvmTx(
        to=TOKEN_EVM,
        data=raw_calldata("transfer(address,uint256)", _abi_addr(ATTACKER_EVM), _abi_uint(10**18)),
    )
    decision = check_evm_transaction(tx, chain=Chain.BASE, policy=policy)
    assert not decision.allowed
    assert decision.reason.startswith("erc20_transfer_forbidden")


def test_reject_erc20_transfer_even_to_our_own_address(policy):
    """`transfer` is denied by selector, not by destination. There is no good `transfer`."""
    tx = EvmTx(
        to=TOKEN_EVM, data=raw_calldata("transfer(address,uint256)", _abi_addr(OUR_EVM), _abi_uint(1))
    )
    assert not check_evm_transaction(tx, chain=Chain.BASE, policy=policy).allowed


def test_reject_erc20_transfer_from(policy):
    tx = EvmTx(
        to=TOKEN_EVM,
        data=raw_calldata(
            "transferFrom(address,address,uint256)",
            _abi_addr(OUR_EVM),
            _abi_addr(ATTACKER_EVM),
            _abi_uint(10**18),
        ),
    )
    decision = check_evm_transaction(tx, chain=Chain.BASE, policy=policy)
    assert not decision.allowed
    assert decision.reason.startswith("erc20_transfer_from_forbidden")


def test_reject_approve_to_unknown_spender(policy):
    tx = EvmTx(
        to=TOKEN_EVM, data=calldata("approve", _abi_addr(ATTACKER_EVM), _abi_uint(2**256 - 1))
    )
    decision = check_evm_transaction(tx, chain=Chain.BASE, policy=policy)
    assert not decision.allowed
    assert decision.reason.startswith("approve_spender_not_allowlisted")


def test_reject_set_approval_for_all(policy):
    tx = EvmTx(
        to=TOKEN_EVM,
        data=raw_calldata("setApprovalForAll(address,bool)", _abi_addr(ATTACKER_EVM), _abi_uint(1)),
    )
    decision = check_evm_transaction(tx, chain=Chain.BASE, policy=policy)
    assert not decision.allowed
    assert decision.reason.startswith("blanket_approval_forbidden")


def test_reject_swap_whose_recipient_is_not_our_wallet(policy):
    """Recipient substitution: a perfectly ordinary swap that pays somebody else."""
    tx = EvmTx(to=BASE_ROUTER, data=exact_input_single(ATTACKER_EVM))
    decision = check_evm_transaction(tx, chain=Chain.BASE, policy=policy)
    assert not decision.allowed
    assert decision.reason == f"recipient_not_owned:{ATTACKER_EVM}"


def test_reject_v2_swap_whose_recipient_is_not_our_wallet(policy):
    tx = EvmTx(
        to=BASE_V2_ROUTER,
        value=10**16,
        data=calldata(
            "swapExactETHForTokens",
            _abi_uint(1),
            _abi_uint(0x80),
            _abi_addr(ATTACKER_EVM),
            _abi_uint(1 << 40),
            _abi_uint(2),
            _abi_addr(TOKEN_EVM),
            _abi_addr(OUR_EVM),
        ),
    )
    decision = check_evm_transaction(tx, chain=Chain.BASE, policy=policy)
    assert not decision.allowed and decision.reason.startswith("recipient_not_owned")


def test_reject_bridge_deposit(policy):
    """Value leaving the chain is a withdrawal with extra steps."""
    tx = EvmTx(
        to=OPTIMISM_PORTAL,
        value=10**18,
        data=raw_calldata(
            "depositTransaction(address,uint256,uint64,bool,bytes)",
            _abi_addr(ATTACKER_EVM),
            _abi_uint(10**18),
            _abi_uint(100000),
            _abi_uint(0),
            _abi_uint(0xA0),
        ),
    )
    decision = check_evm_transaction(tx, chain=Chain.BASE, policy=policy)
    assert not decision.allowed
    assert decision.reason.startswith("bridge_deposit_forbidden")


def test_reject_polygon_style_bridge_deposit_for(policy):
    tx = EvmTx(
        to=ATTACKER_EVM,
        data=raw_calldata(
            "depositFor(address,address,bytes)",
            _abi_addr(OUR_EVM),
            _abi_addr(TOKEN_EVM),
            _abi_uint(0x60),
        ),
    )
    decision = check_evm_transaction(tx, chain=Chain.BASE, policy=policy)
    assert not decision.allowed and decision.reason.startswith("bridge_deposit_forbidden")


def test_reject_native_value_transfer_to_a_stranger(policy):
    tx = EvmTx(to=ATTACKER_EVM, value=10**18, data="0x")
    decision = check_evm_transaction(tx, chain=Chain.BASE, policy=policy)
    assert not decision.allowed
    assert decision.reason == "native_value_transfer_forbidden"


def test_reject_native_value_to_a_non_router_contract(policy):
    tx = EvmTx(to=ATTACKER_EVM, value=10**18, data=exact_input_single(OUR_EVM))
    decision = check_evm_transaction(tx, chain=Chain.BASE, policy=policy)
    assert not decision.allowed and decision.reason.startswith("to_not_allowlisted_router")


def test_reject_unknown_selector(policy):
    tx = EvmTx(to=BASE_ROUTER, data=raw_calldata("drain(address)", _abi_addr(ATTACKER_EVM)))
    decision = check_evm_transaction(tx, chain=Chain.BASE, policy=policy)
    assert not decision.allowed and decision.reason.startswith("selector_unknown")


def test_reject_chain_id_mismatch(policy):
    tx = EvmTx(to=BASE_ROUTER, data=exact_input_single(OUR_EVM), chain_id=1)
    decision = check_evm_transaction(tx, chain=Chain.BASE, policy=policy)
    assert not decision.allowed and decision.reason.startswith("chain_id_mismatch")


def test_reject_evm_transaction_on_a_chain_with_no_routers(policy):
    """Robinhood ships with an empty router list; that must mean refuse, not allow."""
    tx = EvmTx(to=BASE_ROUTER, data=exact_input_single(OUR_EVM))
    decision = check_evm_transaction(tx, chain=Chain.ROBINHOOD, policy=policy)
    assert not decision.allowed and decision.reason.startswith("to_not_allowlisted_router")


def test_reject_universal_router_execute_without_our_address(policy):
    tx = EvmTx(
        to=BASE_UNIVERSAL,
        data=calldata("execute", _abi_uint(0x60), _abi_uint(0xA0), _abi_uint(1 << 40), _abi_addr(ATTACKER_EVM)),
    )
    decision = check_evm_transaction(tx, chain=Chain.BASE, policy=policy)
    assert not decision.allowed and decision.reason == "recipient_not_found_in_calldata"


def test_reject_evm_call_on_a_solana_chain(policy):
    tx = EvmTx(to=BASE_ROUTER, data=exact_input_single(OUR_EVM))
    decision = check_evm_transaction(tx, chain=Chain.SOL, policy=policy)
    assert not decision.allowed and decision.reason == "chain_not_evm"


# ------------------------------------------------------------------ EVM: allowed


def test_allow_exact_input_single_to_our_own_wallet(policy):
    tx = EvmTx(to=BASE_ROUTER, value=10**16, data=exact_input_single(OUR_EVM), chain_id=8453)
    decision = check_evm_transaction(tx, chain=Chain.BASE, policy=policy)
    assert decision.allowed, decision.reason
    assert f"recipient_owned:{OUR_EVM}" in decision.findings


def test_allow_approve_to_permit2(policy):
    tx = EvmTx(to=TOKEN_EVM, data=calldata("approve", _abi_addr(PERMIT2_ADDR), _abi_uint(2**256 - 1)))
    decision = check_evm_transaction(tx, chain=Chain.BASE, policy=policy)
    assert decision.allowed, decision.reason
    assert "unlimited_approval" in decision.findings


def test_allow_approve_to_an_allowlisted_router(policy):
    tx = EvmTx(to=TOKEN_EVM, data=calldata("approve", _abi_addr(BASE_ROUTER), _abi_uint(10**18)))
    assert check_evm_transaction(tx, chain=Chain.BASE, policy=policy).allowed


def test_allow_v2_swap_to_our_wallet(policy):
    tx = EvmTx(
        to=BASE_V2_ROUTER,
        value=10**16,
        data=calldata(
            "swapExactETHForTokens",
            _abi_uint(1),
            _abi_uint(0x80),
            _abi_addr(OUR_EVM),
            _abi_uint(1 << 40),
            _abi_uint(2),
            _abi_addr(TOKEN_EVM),
            _abi_addr(ATTACKER_EVM),
        ),
    )
    assert check_evm_transaction(tx, chain=Chain.BASE, policy=policy).allowed


def test_allow_exact_input_with_dynamic_path(policy):
    args = (
        _abi_uint(32)
        + _abi_uint(128)
        + _abi_addr(OUR_EVM)
        + _abi_uint(10**16)
        + _abi_uint(1)
        + _abi_uint(43)
        + bytes.fromhex(TOKEN_EVM[2:] + "000bb8" + ATTACKER_EVM[2:]).ljust(64, b"\x00")
    )
    tx = EvmTx(to=BASE_ROUTER, data=selector_of(EVM_FUNCTIONS["exactInput"]) + args.hex())
    decision = check_evm_transaction(tx, chain=Chain.BASE, policy=policy)
    assert decision.allowed, decision.reason


# ------------------------------------------------------------------ GMGN swap body


def test_reject_gmgn_body_with_extra_recipient_key():
    decision = check_gmgn_swap_body(
        gmgn_body(recipient=STRANGER_SOL), wallet=OUR_SOL, chain=Chain.SOL
    )
    assert not decision.allowed
    assert decision.reason == "gmgn_body_forbidden_field:recipient"


def test_reject_gmgn_body_with_auto_slippage_true():
    decision = check_gmgn_swap_body(gmgn_body(auto_slippage=True), wallet=OUR_SOL, chain=Chain.SOL)
    assert not decision.allowed
    assert decision.reason == "gmgn_body_auto_slippage_forbidden"


@pytest.mark.parametrize(
    "key", ["fee_recipient", "tip_fee", "beneficiary", "calldata", "to_address", "spender"]
)
def test_reject_gmgn_body_with_any_smuggled_key(key):
    decision = check_gmgn_swap_body(gmgn_body(**{key: "x"}), wallet=OUR_SOL, chain=Chain.SOL)
    assert not decision.allowed and decision.reason.startswith("gmgn_body_forbidden_field")


def test_reject_gmgn_body_from_another_wallet():
    decision = check_gmgn_swap_body(
        gmgn_body(from_address=STRANGER_SOL), wallet=OUR_SOL, chain=Chain.SOL
    )
    assert not decision.allowed and decision.reason == "gmgn_body_from_address_not_our_wallet"


def test_reject_gmgn_body_missing_a_field():
    body = gmgn_body()
    body.pop("min_output_amount")
    decision = check_gmgn_swap_body(body, wallet=OUR_SOL, chain=Chain.SOL)
    assert not decision.allowed and decision.reason == "gmgn_body_missing_field:min_output_amount"


def test_reject_gmgn_body_with_input_equal_to_output():
    decision = check_gmgn_swap_body(
        gmgn_body(output_token=SOL_MINT), wallet=OUR_SOL, chain=Chain.SOL
    )
    assert not decision.allowed and decision.reason == "gmgn_body_input_equals_output"


@pytest.mark.parametrize("amount", ["0", 0, -1, "abc", 1.5, True])
def test_reject_gmgn_body_with_a_non_positive_amount(amount):
    decision = check_gmgn_swap_body(
        gmgn_body(input_amount=amount), wallet=OUR_SOL, chain=Chain.SOL
    )
    assert not decision.allowed and decision.reason.startswith("gmgn_body_amount_invalid")


def test_reject_gmgn_body_for_the_wrong_chain():
    decision = check_gmgn_swap_body(gmgn_body(chain="bsc"), wallet=OUR_SOL, chain=Chain.SOL)
    assert not decision.allowed and decision.reason.startswith("gmgn_body_chain_mismatch")


def test_allow_well_formed_gmgn_swap_body():
    decision = check_gmgn_swap_body(gmgn_body(), wallet=OUR_SOL, chain=Chain.SOL)
    assert decision.allowed, decision.reason


def test_allow_well_formed_gmgn_swap_body_as_a_json_string():
    import json

    decision = check_gmgn_swap_body(json.dumps(gmgn_body()), wallet=OUR_SOL, chain=Chain.SOL)
    assert decision.allowed, decision.reason


# ------------------------------------------------------------------ operation vocabulary


@pytest.mark.parametrize(
    "operation",
    [
        "transfer", "withdraw", "withdraw_all", "send", "bridge", "approve_unlimited",
        "approve", "delegate", "stake", "deploy", "close", "close_account", "export_key",
        "set_authority", "multicall", "raw_call", "sign_message", "cross_chain_transfer",
    ],
)
def test_assert_no_withdrawal_refuses_the_denied_vocabulary(operation):
    with pytest.raises(WithdrawalBlocked):
        assert_no_withdrawal(operation, {"chain": "sol"})


def test_assert_no_withdrawal_names_the_reason():
    with pytest.raises(WithdrawalBlocked) as excinfo:
        assert_no_withdrawal("withdraw", {"amount": 1})
    assert excinfo.value.reason == "withdrawal_operation_excluded"


@pytest.mark.parametrize("operation", ["swap", "multi_swap", "quote", "order_get", "order_strategy"])
def test_assert_no_withdrawal_permits_the_trading_vocabulary(operation):
    assert_no_withdrawal(operation, {"chain": "sol", "input_token": SOL_MINT})


@pytest.mark.parametrize("operation", ["token_info", "portfolio_holdings", "market_trenches", "grade"])
def test_assert_no_withdrawal_permits_reads(operation):
    assert_no_withdrawal(operation, {})


def test_assert_no_withdrawal_refuses_an_operation_nobody_enumerated():
    with pytest.raises(WithdrawalBlocked) as excinfo:
        assert_no_withdrawal("swap_and_forward", {})
    assert excinfo.value.reason == "operation_not_allowlisted"


def test_assert_no_withdrawal_refuses_a_non_canonical_spelling():
    with pytest.raises(WithdrawalBlocked) as excinfo:
        assert_no_withdrawal(" Swap ", {})
    assert excinfo.value.reason == "operation_not_canonical"


@pytest.mark.parametrize("key", ["recipient", "spender", "calldata", "private_key", "fee_recipient"])
def test_assert_no_withdrawal_refuses_a_smuggled_parameter(key):
    with pytest.raises(WithdrawalBlocked) as excinfo:
        assert_no_withdrawal("swap", {"chain": "sol", key: ATTACKER_EVM})
    assert excinfo.value.reason == f"forbidden_param:{key}"


# ------------------------------------------------------------------ audit trail


def test_every_refusal_emits_a_risk_halt_event(_event_db, policy):
    from kaiba.core import events

    before = events.latest_id(conn=_event_db)
    check_solana_transaction([sys_transfer(STRANGER_SOL, 10**9)], policy=policy, conn=_event_db)
    rows = events.tail(after_id=before, conn=_event_db)
    assert rows, "a refusal that leaves no trace is a refusal nobody can audit"
    assert rows[-1].kind is EventKind.RISK_HALT
    assert rows[-1].level == "warn"
    assert rows[-1].payload["gate"] == "withdrawal"


# ------------------------------------------------------------------ configuration cannot widen


def _adversarial_cases(policy: SignerPolicy) -> dict[str, object]:
    """Every case that must stay refused, whatever the configuration says."""
    return {
        "system_transfer_to_stranger": lambda p: check_solana_transaction(
            [sys_transfer(STRANGER_SOL, 10**9)], policy=p
        ),
        "spl_transfer_to_stranger": lambda p: check_solana_transaction([spl_transfer(STRANGER_SOL)], policy=p),
        "set_authority": lambda p: check_solana_transaction(
            [sol_ix(TOKEN_PROGRAM, [OUR_SOL_ATA, OUR_SOL], bytes([6, 2, 1]) + bytes(32))], policy=p
        ),
        "unknown_program": lambda p: check_solana_transaction(
            [sol_ix(UNKNOWN_PROGRAM, [OUR_SOL], b"\x00")], policy=p
        ),
        "unresolved_alt": lambda p: check_solana_transaction(
            [sol_ix(JUPITER_V6, [OUR_SOL], b"\x01", resolved_from_alt=False)], policy=p
        ),
        "fake_jito_tip": lambda p: check_solana_transaction([sys_transfer(FAKE_TIP, 1000)], policy=p),
        "erc20_transfer": lambda p: check_evm_transaction(
            EvmTx(
                to=TOKEN_EVM,
                data=raw_calldata("transfer(address,uint256)", _abi_addr(ATTACKER_EVM), _abi_uint(1)),
            ),
            chain=Chain.BASE,
            policy=p,
        ),
        "erc20_transfer_from": lambda p: check_evm_transaction(
            EvmTx(
                to=TOKEN_EVM,
                data=raw_calldata(
                    "transferFrom(address,address,uint256)",
                    _abi_addr(OUR_EVM),
                    _abi_addr(ATTACKER_EVM),
                    _abi_uint(1),
                ),
            ),
            chain=Chain.BASE,
            policy=p,
        ),
        "approve_unknown_spender": lambda p: check_evm_transaction(
            EvmTx(to=TOKEN_EVM, data=calldata("approve", _abi_addr(ATTACKER_EVM), _abi_uint(1))),
            chain=Chain.BASE,
            policy=p,
        ),
        "recipient_substitution": lambda p: check_evm_transaction(
            EvmTx(to=BASE_ROUTER, data=exact_input_single(ATTACKER_EVM)), chain=Chain.BASE, policy=p
        ),
        "bridge_deposit": lambda p: check_evm_transaction(
            EvmTx(
                to=OPTIMISM_PORTAL,
                value=10**18,
                data=raw_calldata(
                    "depositTransaction(address,uint256,uint64,bool,bytes)",
                    _abi_addr(ATTACKER_EVM),
                    _abi_uint(10**18),
                    _abi_uint(1),
                    _abi_uint(0),
                    _abi_uint(0xA0),
                ),
            ),
            chain=Chain.BASE,
            policy=p,
        ),
        "native_send": lambda p: check_evm_transaction(
            EvmTx(to=ATTACKER_EVM, value=10**18), chain=Chain.BASE, policy=p
        ),
        "gmgn_extra_recipient": lambda p: check_gmgn_swap_body(
            gmgn_body(recipient=STRANGER_SOL), wallet=OUR_SOL, chain=Chain.SOL
        ),
        "gmgn_auto_slippage": lambda p: check_gmgn_swap_body(
            gmgn_body(auto_slippage=True), wallet=OUR_SOL, chain=Chain.SOL
        ),
    }


def _mutations(raw: dict) -> list[tuple[str, dict]]:
    """Widening edits an attacker (or an over-eager agent) would try on the policy file.

    ``owned_addresses`` and ``evm.routers`` are deliberately included only as wildcard and
    junk entries, not as "declare the attacker to be ours": declaring ownership is what
    those keys are *for*, and they are root-owned. Everything else must be inert.
    """
    out: list[tuple[str, dict]] = []

    def m(name: str, fn) -> None:
        copy_ = copy.deepcopy(raw)
        fn(copy_)
        out.append((name, copy_))

    m("allow_withdrawals_flag", lambda c: c.update({"allow_withdrawals": True}))
    m("withdrawals_enabled_flag", lambda c: c.update({"withdrawals_enabled": True, "enforce": False}))
    m("disable_policy_flag", lambda c: c.update({"disabled": True, "bypass": True, "dry_run": False}))
    m("version_bump", lambda c: c.update({"version": "v99"}))
    m("programs_add_unknown", lambda c: c["solana"]["allowed_programs"].extend([UNKNOWN_PROGRAM, "*"]))
    m("programs_wildcard_only", lambda c: c["solana"].update({"allowed_programs": ["*"]}))
    m("tips_add_attacker", lambda c: c["solana"]["jito_tip_accounts"].extend([STRANGER_SOL, FAKE_TIP, "*"]))
    m("tip_cap_to_the_moon", lambda c: c["solana"].update({"max_tip_lamports": 10**18}))
    m("cu_price_to_the_moon", lambda c: c["solana"].update({"max_compute_unit_price_micro_lamports": 10**18}))
    m("selectors_add_transfer", lambda c: c["evm"]["allowed_selectors"].extend(
        ["transfer", "transferFrom", "setApprovalForAll", "depositTransaction", "*"]))
    m("selectors_wildcard_only", lambda c: c["evm"].update({"allowed_selectors": ["*"]}))
    m("max_approval_removed", lambda c: c["evm"].update({"max_approval_wei": 10**77}))
    m("owned_wildcard", lambda c: c.update({"owned_addresses": {k: ["*"] for k in c["owned_addresses"]}}))
    m("owned_empty_string", lambda c: c.update({"owned_addresses": {k: [""] for k in c["owned_addresses"]}}))
    m("routers_wildcard", lambda c: c["evm"].update({"routers": {k: ["*"] for k in c["evm"]["routers"]}}))
    m("everything_true", lambda c: c.update({k: True for k in ("kill_switch", "armed", "unsafe", "god_mode")}))
    return out


@pytest.mark.parametrize("mutation_name", [name for name, _ in _mutations(yaml.safe_load(
    Path(DEFAULT_POLICY_PATH).read_text(encoding="utf-8")))])
def test_no_signer_policy_edit_can_turn_a_refusal_into_an_acceptance(mutation_name, policy, tmp_path):
    raw = yaml.safe_load(Path(DEFAULT_POLICY_PATH).read_text(encoding="utf-8"))
    mutated_raw = dict(_mutations(raw))[mutation_name]
    # Keep our own declared addresses so the cases are testing the mutation, not emptiness.
    if mutation_name not in ("owned_wildcard", "owned_empty_string"):
        mutated_raw["owned_addresses"] = {
            "sol": [OUR_SOL, OUR_SOL_ATA], "base": [OUR_EVM], "bsc": [OUR_EVM], "robinhood": []
        }
    path = tmp_path / "mutated.yaml"
    path.write_text(yaml.safe_dump(mutated_raw), encoding="utf-8")
    mutated = load_policy(path)

    for case_name, case in _adversarial_cases(mutated).items():
        decision = case(mutated)
        assert not decision.allowed, f"{mutation_name} unlocked {case_name}"


def test_no_risk_yaml_edit_can_turn_a_refusal_into_an_acceptance(tmp_path, monkeypatch, policy):
    """The withdrawal gate does not read ``config/risk.yaml`` at all — prove it two ways."""
    from kaiba.core.config import DEFAULT_RISK_PATH

    raw = yaml.safe_load(Path(DEFAULT_RISK_PATH).read_text(encoding="utf-8"))
    hostile = copy.deepcopy(raw)
    hostile.update({"kill_switch": False, "allow_withdrawals": True, "global_mode": "live"})
    hostile["bounds"].update({"max_size_pct_bankroll": 100.0, "max_lane_mode": "live"})
    hostile["protection"] = {}
    path = tmp_path / "risk.yaml"
    path.write_text(yaml.safe_dump(hostile), encoding="utf-8")
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))

    for case_name, case in _adversarial_cases(policy).items():
        assert not case(policy).allowed, f"risk.yaml unlocked {case_name}"

    # ...and structurally: the module never imports or binds the risk config at all.
    import ast

    from kaiba.execution import policy as policy_module

    tree = ast.parse(Path(policy_module.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names
    }
    assert not imported & {"get_risk", "RiskConfig", "load_risk", "save_risk"}
    assert not hasattr(policy_module, "get_risk")


def test_policy_object_built_by_hand_cannot_widen_either():
    """Even bypassing the YAML, the model narrows to the code-level registries."""
    hostile = SignerPolicy.model_validate(
        {
            "owned_addresses": {"sol": [OUR_SOL]},
            "solana": {
                "allowed_programs": [UNKNOWN_PROGRAM, SYSTEM_PROGRAM],
                "jito_tip_accounts": [STRANGER_SOL],
                "max_tip_lamports": 10**18,
                "max_compute_unit_price_micro_lamports": 10**18,
            },
            "evm": {"allowed_selectors": ["transfer", "transferFrom", "exactInputSingle"]},
        }
    )
    assert UNKNOWN_PROGRAM not in hostile.solana.effective_programs
    assert hostile.solana.effective_tip_accounts == frozenset()
    assert hostile.solana.effective_max_tip_lamports == MAX_TIP_LAMPORTS_CEILING
    assert hostile.evm.effective_selector_names == {"exactInputSingle"}


# --------------------------------------------------------------------------------------
# bsc + robinhood arming, 2026-09-21
# --------------------------------------------------------------------------------------


def _evm_call(signature: str, *words: str) -> str:
    return selector_of(signature) + "".join(words)


def _word_addr(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def _word_uint(value: int) -> str:
    return format(value, "064x")


_NOT_OURS = "0x000000000000000000000000000000000000dEaD"
_A_TOKEN = "0x0e09fabb73bd3ade0a17ecc321fd13a19e81ce82"
_OURS = "0x72430877378522d1b759ac721561aa9eb9e25c2b"

#: Every way value leaves an EVM wallet that the decoder can see. Native send, the two
#: ERC-20 primitives, the WETH-style unwrap, and the NFT blanket approval.
_EXFIL_ATTEMPTS: tuple[tuple[str, str, int, str], ...] = (
    ("erc20 transfer", _A_TOKEN, 0,
     _evm_call("transfer(address,uint256)", _word_addr(_NOT_OURS), _word_uint(10**18))),
    ("erc20 transferFrom", _A_TOKEN, 0,
     _evm_call("transferFrom(address,address,uint256)",
               _word_addr(_OURS), _word_addr(_NOT_OURS), _word_uint(10**18))),
    ("withdraw", _A_TOKEN, 0, _evm_call("withdraw(uint256)", _word_uint(10**18))),
    ("setApprovalForAll", _A_TOKEN, 0,
     _evm_call("setApprovalForAll(address,bool)", _word_addr(_NOT_OURS), _word_uint(1))),
    ("native send", _NOT_OURS, 10**18, "0x"),
)


@pytest.mark.parametrize("chain", [Chain.BSC, Chain.ROBINHOOD])
@pytest.mark.parametrize("label,to,value,data", _EXFIL_ATTEMPTS, ids=lambda v: v if isinstance(v, str) else "")
def test_declaring_an_evm_wallet_owned_does_not_open_a_withdrawal_path(
    chain: Chain, label: str, to: str, value: int, data: str
) -> None:
    """Arming bsc/robinhood put a real address in ``owned_addresses`` for the first time.

    On Solana "ours" is mostly about where a transfer may land. On EVM the same word is
    far more dangerous, because ``transfer(address,uint256)`` IS the withdrawal
    primitive and it lives on the token contract, not on a router. This pins that
    declaring the wallet bought the executor nothing but the right to swap.
    """
    # The armed policy the suite runs against (tests/fixtures/config, fake owned wallets): the
    # shipped config/signer-policy.yaml is a template with no owned addresses at all.
    shipped = load_policy()
    assert shipped.owned(chain), "this test is meaningless unless the chain is armed"
    decision = check_evm_transaction(
        EvmTx(to=to, value=value, data=data), chain=chain, policy=shipped
    )
    assert not decision.allowed, f"{label} on {chain.value} was ALLOWED: {decision.reason}"


@pytest.mark.parametrize("chain", [Chain.BSC, Chain.ROBINHOOD])
def test_a_hostile_policy_file_cannot_open_an_evm_withdrawal_path(
    chain: Chain, tmp_path: Path
) -> None:
    """The config may only ever NARROW. Proved against the worst file we can write.

    This forges a policy that does all three things at once: allows every hard-denied
    selector, declares the token contract itself a router so ``to`` passes, and claims
    the destination is an address we own. All five exfiltration attempts must still be
    refused, because the selector allowlist is intersected with a compiled-in registry
    minus ``HARD_DENIED_FUNCTIONS`` and no YAML can reach that set.
    """
    raw = yaml.safe_load(Path(DEFAULT_POLICY_PATH).read_text(encoding="utf-8"))
    evm = raw.setdefault("evm", {})
    evm["allowed_selectors"] = list(evm.get("allowed_selectors") or []) + [
        "transfer(address,uint256)",
        "transferFrom(address,address,uint256)",
        "withdraw(uint256)",
        "setApprovalForAll(address,bool)",
    ]
    routers = evm.setdefault("routers", {})
    routers[chain.value] = list(routers.get(chain.value) or []) + [_A_TOKEN]
    raw["owned_addresses"][chain.value] = [_NOT_OURS, _OURS]

    forged = tmp_path / "hostile-policy.yaml"
    forged.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    hostile = load_policy(forged)

    leaked = [
        label
        for label, to, value, data in _EXFIL_ATTEMPTS
        if check_evm_transaction(
            EvmTx(to=to, value=value, data=data), chain=chain, policy=hostile
        ).allowed
    ]
    assert leaked == [], f"a hostile config opened {leaked} on {chain.value}"
