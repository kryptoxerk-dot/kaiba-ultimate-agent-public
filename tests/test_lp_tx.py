"""Pure DLMM transactions: the IDL table, wire bytes, account order, and the rent refusal.

No network. `tests/fixtures/dlmm_idl_subset.json` was generated from the hash-verified pinned IDL, and
`deploy/research/lp_scalp_audit/simulate_live.py idl` compares it with the real file. Whether the
bytes work on the real program is shown by that script's `run`, not here.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from pathlib import Path

import base58
import pytest
from nacl.signing import SigningKey

from kaiba.execution import lp_tx as tx
from kaiba.execution.lp_model import PoolState, arrays_for, bin_price
from kaiba.learning.dlmm_state import DISCRIMINATORS, PROGRAM, SDK_REVISION, bin_array_address

FIXTURE = json.loads((Path(__file__).parent / "fixtures/dlmm_idl_subset.json").read_text(encoding="utf-8"))
HASH = tx.SYSTEM


def key(n: int) -> str:
    """A valid address nothing on the chain owns. Tests hold no real ones."""
    return base58.b58encode(bytes(SigningKey(bytes([n]) * 32).verify_key)).decode()


OWNER, PAYER, POSITION, POOL, MINT, RES_X, RES_Y, ORACLE = [key(i) for i in range(1, 9)]


def pool_keys(**over) -> tx.PoolKeys:
    fields = dict(address=POOL, mint_x=MINT, mint_y=tx.NATIVE_MINT, reserve_x=RES_X, reserve_y=RES_Y, oracle=ORACLE,
                  program_x=tx.TOKEN_2022, program_y=tx.TOKEN, active=-500, bin_step=100, status=0, rewards_present=False,
                  bitmap=tuple([0] * 16))
    return tx.PoolKeys(**{**fields, **over})


def parse(raw: bytes) -> dict:
    """A decoder written from the wire format, independent of `compile_message`."""
    def compact(b: bytes, at: int) -> tuple[int, int]:
        value = shift = 0
        while True:
            byte = b[at]
            at += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return value, at

    count, at = compact(raw, 0)
    at += 64 * count
    signed, readonly_signed, readonly_unsigned = raw[at:at + 3]
    n, at = compact(raw, at + 3)
    keys = [base58.b58encode(raw[at + 32 * i:at + 32 * (i + 1)]).decode() for i in range(n)]
    at += 32 * n
    blockhash = base58.b58encode(raw[at:at + 32]).decode()
    n, at = compact(raw, at + 32)
    ixs = []
    for _ in range(n):
        program = keys[raw[at]]
        n_accounts, at = compact(raw, at + 1)
        accounts = [keys[i] for i in raw[at:at + n_accounts]]
        length, at = compact(raw, at + n_accounts)
        ixs.append({"program": program, "accounts": accounts, "data": raw[at:at + length]})
        at += length
    assert at == len(raw)
    return {"signatures": count, "signed": signed, "readonly_signed": readonly_signed,
            "readonly_unsigned": readonly_unsigned, "keys": keys, "blockhash": blockhash, "ixs": ixs}


def named(ix: dict) -> str:
    return next((name for name, spec in tx.INSTRUCTIONS.items() if ix["data"][:8] == spec.discriminator), "")


def build(ixs, payer=OWNER) -> dict:
    return parse(tx.serialize(tx.compile_message(payer, tx.finish(ixs, 300_000, 295_000), HASH)))


# ---------------------------------------------------------------------------------- the IDL table
def test_fixture_is_the_pinned_revision():
    assert FIXTURE["sha256"] == tx.IDL_SHA256
    assert FIXTURE["revision"] == SDK_REVISION
    assert FIXTURE["address"] == PROGRAM


@pytest.mark.parametrize("name", sorted(tx.INSTRUCTIONS))
def test_table_matches_the_idl_field_by_field(name):
    want = FIXTURE["instructions"][name]
    spec = tx.INSTRUCTIONS[name]
    assert list(spec.discriminator) == want["discriminator"]
    ours = [{"name": a.name, "writable": a.writable, "signer": a.signer, "optional": a.optional, "address": a.address}
            for a in spec.accounts]
    theirs = [{"name": a["name"], "writable": a.get("writable", False), "signer": a.get("signer", False),
               "optional": a.get("optional", False), "address": a.get("address")} for a in want["accounts"]]
    assert ours == theirs


@pytest.mark.parametrize("name", sorted(tx.INSTRUCTIONS))
def test_discriminators_are_anchors_hash_of_the_name(name):
    assert tx.INSTRUCTIONS[name].discriminator == hashlib.sha256(f"global:{name}".encode()).digest()[:8]


def test_layout_constants_match_the_idl():
    offsets = FIXTURE["lb_pair_offsets"]
    assert (tx.LB_RESERVE_X, tx.LB_RESERVE_Y, tx.LB_ORACLE, tx.LB_BITMAP) == (
        offsets["reserve_x"], offsets["reserve_y"], offsets["oracle"], offsets["bin_array_bitmap"])
    assert list(tx.EXT_DISCRIMINATOR) == FIXTURE["accounts"]["BinArrayBitmapExtension"]
    assert list(DISCRIMINATORS["position"]) == FIXTURE["accounts"]["PositionV2"]
    assert tx.EXT_POSITIVE == FIXTURE["extension_offsets"]["positive_bin_array_bitmap"]
    assert tx.EXT_POSITIVE + tx.EXT_ROWS * tx.EXT_WORDS * 8 == FIXTURE["extension_offsets"]["negative_bin_array_bitmap"]
    assert FIXTURE["strategy_types"][tx.SPOT_IMBALANCED] == "SpotImBalanced"


def test_a_missing_or_unknown_account_is_refused():
    with pytest.raises(tx.LpTxError, match="missing_account:swap2.lb_pair"):
        tx.dlmm("swap2", b"", {})
    with pytest.raises(tx.LpTxError, match="unknown_account"):
        tx.dlmm("close_position_if_empty", b"", {"position": POSITION, "sender": OWNER, "rent_receiver": OWNER, "wrong": OWNER})


# --------------------------------------------------------------------------------------- the wire
@pytest.mark.parametrize(("n", "wire"), [(0, b"\x00"), (127, b"\x7f"), (128, b"\x80\x01"), (16383, b"\xff\x7f"),
                                         (16384, b"\x80\x80\x01")])
def test_compact_u16(n, wire):
    assert tx.compact_u16(n) == wire


def test_message_order_is_what_the_runtime_requires():
    ixs = [tx.Instruction(tx.TOKEN, (tx.Meta(key(20), writable=True), tx.Meta(key(21)), tx.Meta(key(22), signer=True),
                                     tx.Meta(key(23), signer=True, writable=True)), b"\x01")]
    got = parse(tx.serialize(tx.compile_message(OWNER, ixs, HASH)))
    assert got["keys"] == [OWNER, key(23), key(22), key(20), key(21), tx.TOKEN]
    assert (got["signed"], got["readonly_signed"], got["readonly_unsigned"]) == (3, 1, 2)
    assert got["signatures"] == 3
    assert got["ixs"][0]["accounts"] == [key(20), key(21), key(22), key(23)]


def test_flags_on_one_key_combine_across_instructions():
    ixs = [tx.Instruction(tx.TOKEN, (tx.Meta(key(20)),), b""), tx.Instruction(tx.TOKEN, (tx.Meta(key(20), writable=True),), b"")]
    got = parse(tx.serialize(tx.compile_message(OWNER, ixs, HASH)))
    assert got["keys"].index(key(20)) == 1
    assert got["readonly_unsigned"] == 1  # only the program


def test_signed_bytes_verify_against_the_signed_message():
    signer = SigningKey(bytes([9]) * 32)
    payer = base58.b58encode(bytes(signer.verify_key)).decode()
    message = tx.compile_message(payer, [tx.Instruction(tx.TOKEN, (), b"\x01")], HASH)
    raw = tx.serialize(message, [signer.sign(message.raw).signature])
    assert signer.verify_key.verify(message.raw, raw[1:65])
    assert raw[65:] == message.raw
    assert message.size == len(raw)


def test_a_wrong_signature_count_is_refused():
    message = tx.compile_message(OWNER, [tx.Instruction(tx.TOKEN, (), b"\x01")], HASH)
    with pytest.raises(tx.LpTxError):
        tx.serialize(message, [bytes(64), bytes(64)])
    with pytest.raises(tx.LpTxError):
        tx.serialize(message, [bytes(63)])


def test_over_the_packet_limit_is_never_built():
    ixs = [tx.Instruction(tx.TOKEN, tuple(tx.Meta(key(40 + i)) for i in range(40)), b"\x01")]
    with pytest.raises(tx.TxTooLarge):
        tx.compile_message(OWNER, ixs, HASH)


# -------------------------------------------------------------------------------- addresses
def explicit_pda(seeds: list[bytes], program: str) -> str:
    """The derivation written out, so a change to seeds or their order shows."""
    from kaiba.execution.fill_costs import _on_ed25519_curve

    for bump in range(255, -1, -1):
        digest = hashlib.sha256(b"".join(seeds) + bytes([bump]) + base58.b58decode(program) + b"ProgramDerivedAddress").digest()
        if not _on_ed25519_curve(digest):
            return base58.b58encode(digest).decode()
    raise AssertionError


@pytest.mark.parametrize("program", [tx.TOKEN, tx.TOKEN_2022])
def test_associated_account_seeds(program):
    seeds = [base58.b58decode(OWNER), base58.b58decode(program), base58.b58decode(MINT)]
    assert tx.ata_address(OWNER, MINT, program) == explicit_pda(seeds, tx.ASSOCIATED_TOKEN)


@pytest.mark.parametrize("index", [-9, -1, 0, 7, 600])
def test_bin_array_key_matches_the_readers_derivation(index):
    assert tx.bin_array_key(POOL, index) == bin_array_address(POOL, index)


def test_bitmap_extension_seed():
    assert tx.bitmap_extension_address(POOL) == explicit_pda([b"bitmap", base58.b58decode(POOL)], PROGRAM)


# -------------------------------------------------------------------------------- plain instructions
def test_compute_budget_bytes():
    limit, price = tx.compute_budget(300_000, 983)
    assert limit.program == price.program == tx.COMPUTE_BUDGET
    assert limit.data == b"\x02" + struct.pack("<I", 300_000)
    assert price.data == b"\x03" + struct.pack("<Q", 983)


def test_priority_is_spent_on_the_limit_requested():
    assert tx.priority_micro_lamports(300_000, 300_000) == 1_000_000
    assert tx.priority_micro_lamports(295_000, 300_000) == 983_333


def test_wrap_is_a_transfer_and_a_sync():
    wsol = tx.ata_address(OWNER, tx.NATIVE_MINT, tx.TOKEN)
    transfer, sync = tx.wrap_sol(OWNER, wsol, 123)
    assert transfer.program == tx.SYSTEM
    assert transfer.data == struct.pack("<IQ", 2, 123)
    assert [m.key for m in transfer.metas] == [OWNER, wsol]
    assert sync.program == tx.TOKEN and sync.data == b"\x11"
    with pytest.raises(tx.LpTxError):
        tx.wrap_sol(OWNER, wsol, 0)


def test_create_ata_is_idempotent_and_names_the_mints_program():
    ix = tx.create_ata(PAYER, OWNER, MINT, tx.TOKEN_2022)
    assert ix.program == tx.ASSOCIATED_TOKEN
    assert ix.data == b"\x01"
    assert [m.key for m in ix.metas] == [PAYER, tx.ata_address(OWNER, MINT, tx.TOKEN_2022), OWNER, MINT, tx.SYSTEM, tx.TOKEN_2022]
    assert ix.metas[0].signer and ix.metas[0].writable and ix.metas[1].writable


def test_close_account_uses_the_accounts_own_program():
    ix = tx.close_token_account(OWNER, key(30), tx.TOKEN_2022)
    assert ix.program == tx.TOKEN_2022 and ix.data == b"\x09"
    assert [m.key for m in ix.metas] == [key(30), OWNER, OWNER]
    assert ix.metas[2].signer


# ------------------------------------------------------------------------------- program instructions
def test_spot_deposit_layout():
    ix = tx.add_liquidity_spot(pool_keys(), OWNER, POSITION, lo=-520, hi=-501, amount_x=0, amount_y=5, active_id=-400,
                               max_slippage=100)
    body = ix.data[8:]
    assert struct.unpack_from("<QQiiiiB", body) == (0, 5, -400, 100, -520, -501, tx.SPOT_IMBALANCED)
    parameters = body[struct.calcsize("<QQiiiiB"):struct.calcsize("<QQiiiiB") + 64]
    assert parameters == bytes(64)
    assert body[-4:] == bytes(4)  # empty RemainingAccountsInfo
    assert len(ix.data) == 8 + 8 + 8 + 4 + 4 + 4 + 4 + 1 + 64 + 4


def test_favor_x_is_byte_zero_of_the_parameters():
    ix = tx.add_liquidity_spot(pool_keys(), OWNER, POSITION, lo=0, hi=1, amount_x=1, amount_y=1, active_id=0, max_slippage=0,
                               favor_x=True)
    assert ix.data[8 + struct.calcsize("<QQiiiiB")] == 1


def test_an_empty_deposit_is_refused():
    with pytest.raises(tx.LpTxError):
        tx.add_liquidity_spot(pool_keys(), OWNER, POSITION, lo=0, hi=1, amount_x=0, amount_y=0, active_id=0, max_slippage=0)


def test_deposit_accounts_follow_the_idl_and_end_in_ascending_arrays():
    pool = pool_keys()
    ix = tx.add_liquidity_spot(pool, OWNER, POSITION, lo=-75, hi=-60, amount_x=0, amount_y=5, active_id=0, max_slippage=0)
    keys = [m.key for m in ix.metas]
    assert keys[:14] == [POSITION, POOL, PROGRAM, tx.ata_address(OWNER, MINT, tx.TOKEN_2022),
                         tx.ata_address(OWNER, tx.NATIVE_MINT, tx.TOKEN), RES_X, RES_Y, MINT, tx.NATIVE_MINT, OWNER,
                         tx.TOKEN_2022, tx.TOKEN, tx.EVENT_AUTHORITY, PROGRAM]
    assert keys[14:] == [tx.bin_array_key(POOL, -2), tx.bin_array_key(POOL, -1)]
    assert all(m.writable and not m.signer for m in ix.metas[14:])


def test_an_absent_optional_extension_is_the_program_id_read_only():
    ix = tx.add_liquidity_spot(pool_keys(), OWNER, POSITION, lo=0, hi=1, amount_x=1, amount_y=0, active_id=0, max_slippage=0)
    assert ix.metas[2] == tx.Meta(PROGRAM)
    ext = key(31)
    ix = tx.add_liquidity_spot(pool_keys(extension=ext), OWNER, POSITION, lo=0, hi=1, amount_x=1, amount_y=0, active_id=0, max_slippage=0)
    assert ix.metas[2] == tx.Meta(ext, writable=True)


def test_initialize_position_layout():
    ix = tx.initialize_position(PAYER, OWNER, POSITION, POOL, -520, 20)
    assert ix.data[8:] == struct.pack("<ii", -520, 20)
    assert [m.key for m in ix.metas] == [PAYER, POSITION, POOL, OWNER, tx.SYSTEM, tx.RENT_SYSVAR, tx.EVENT_AUTHORITY, PROGRAM]
    assert ix.metas[1].signer and ix.metas[3].signer


@pytest.mark.parametrize("width", [0, 71])
def test_a_width_the_instruction_cannot_take_is_refused(width):
    with pytest.raises(tx.LpTxError, match="position_width_unsupported"):
        tx.initialize_position(PAYER, OWNER, POSITION, POOL, 0, width)


def test_remove_is_all_of_the_range():
    ix = tx.remove_all_liquidity(pool_keys(), OWNER, POSITION, -520, -501)
    assert ix.data[8:] == struct.pack("<iiH", -520, -501, 10_000) + bytes(4)
    assert ix.metas[12].key == tx.MEMO


def test_claim_layout():
    ix = tx.claim_fee(pool_keys(), OWNER, POSITION, -520, -501)
    assert ix.data[8:] == struct.pack("<ii", -520, -501) + bytes(4)
    assert ix.metas[:3] == (tx.Meta(POOL, writable=True), tx.Meta(POSITION, writable=True), tx.Meta(OWNER, signer=True))


def test_close_returns_rent_to_the_sender():
    ix = tx.close_position(OWNER, POSITION)
    assert [m.key for m in ix.metas] == [POSITION, OWNER, OWNER, tx.EVENT_AUTHORITY, PROGRAM]
    assert ix.data == tx.INSTRUCTIONS["close_position_if_empty"].discriminator


@pytest.mark.parametrize("for_y", [True, False])
def test_swap_direction_sets_the_token_accounts(for_y):
    ix = tx.swap(pool_keys(), OWNER, swap_for_y=for_y, amount_in=7, min_out=3, arrays=[-8, -9])
    ata_x, ata_y = tx.ata_address(OWNER, MINT, tx.TOKEN_2022), tx.ata_address(OWNER, tx.NATIVE_MINT, tx.TOKEN)
    assert [ix.metas[4].key, ix.metas[5].key] == ([ata_x, ata_y] if for_y else [ata_y, ata_x])
    assert ix.data[8:] == struct.pack("<QQ", 7, 3) + bytes(4)
    assert ix.metas[8] == tx.Meta(ORACLE, writable=True)
    assert ix.metas[9] == tx.Meta(PROGRAM)  # no host fee
    assert [m.key for m in ix.metas[16:]] == [tx.bin_array_key(POOL, -8), tx.bin_array_key(POOL, -9)]


def test_a_swap_with_no_liquid_array_is_refused():
    with pytest.raises(tx.LpTxError, match="swap_has_no_liquid_bin_array"):
        tx.swap(pool_keys(), OWNER, swap_for_y=True, amount_in=1, min_out=0, arrays=[])


# ---------------------------------------------------------------------------------- pool accounts
def pool_account(flags=(1, 0)) -> dict:
    raw = bytearray(904)
    raw[:8] = DISCRIMINATORS["pool"]
    struct.pack_into("<iH", raw, 76, -500, 100)
    raw[35], raw[882] = 2, 1
    for at, value in ((88, MINT), (120, tx.NATIVE_MINT), (152, RES_X), (184, RES_Y), (552, ORACLE)):
        raw[at:at + 32] = base58.b58decode(value)
    raw[880], raw[881] = flags
    struct.pack_into("<Q", raw, 584 + 8 * 3, 1 << 5)
    return {"data": [base64.b64encode(bytes(raw)).decode(), "base64"], "owner": PROGRAM, "executable": False}


def test_decode_keys_reads_the_accounts_the_reader_leaves_out():
    got = tx.decode_keys(pool_account(), POOL)
    assert (got.reserve_x, got.reserve_y, got.oracle) == (RES_X, RES_Y, ORACLE)
    assert (got.mint_x, got.mint_y, got.active, got.bin_step) == (MINT, tx.NATIVE_MINT, -500, 100)
    assert (got.program_x, got.program_y) == (tx.TOKEN_2022, tx.TOKEN)
    assert got.bitmap[3] == 1 << 5


def test_fee_offsets_match_the_idl():
    offsets = FIXTURE["fee_offsets"]
    assert (tx.LB_BASE_FACTOR, tx.LB_VARIABLE_FEE_CONTROL, tx.LB_BASE_POWER, tx.LB_VOLATILITY) == (
        offsets["base_factor"], offsets["variable_fee_control"], offsets["base_fee_power_factor"], offsets["volatility_accumulator"])


def fee_raw(base_factor=0, control=0, power=0, volatility=0) -> bytes:
    raw = bytearray(904)
    struct.pack_into("<H", raw, tx.LB_BASE_FACTOR, base_factor)
    struct.pack_into("<I", raw, tx.LB_VARIABLE_FEE_CONTROL, control)
    raw[tx.LB_BASE_POWER] = power
    struct.pack_into("<I", raw, tx.LB_VOLATILITY, volatility)
    return bytes(raw)


def test_total_fee_follows_get_total_fee():
    # Base: 10,000 * 100 * 10 = 10^7 of 10^9 = 1%. Variable: (10^5 * 100)^2 * 7,500 / 10^11 = 7.5 * 10^6 = 0.75%.
    assert tx.total_fee_rate(fee_raw(base_factor=10_000), 100) == pytest.approx(0.01)
    assert tx.total_fee_rate(fee_raw(base_factor=10_000, control=7_500, volatility=100_000), 100) == pytest.approx(0.0175)
    assert tx.total_fee_rate(fee_raw(base_factor=1_000, power=1), 100) == pytest.approx(0.01)


def test_variable_fee_rounds_up_as_the_program_does():
    # (1 * 1)^2 * 1 = 1, plus 99,999,999,999, over 10^11 is exactly 1.
    assert tx.total_fee_rate(fee_raw(control=1, volatility=1), 1) == pytest.approx(1e-9)


def test_total_fee_is_capped_at_ten_percent():
    assert tx.total_fee_rate(fee_raw(base_factor=60_000, control=4_000_000_000, volatility=4_000_000), 400) == pytest.approx(0.10)


def test_decode_keys_carries_the_chain_fee():
    account = pool_account()
    raw = bytearray(base64.b64decode(account["data"][0]))
    struct.pack_into("<H", raw, tx.LB_BASE_FACTOR, 20_000)
    account["data"][0] = base64.b64encode(bytes(raw)).decode()
    assert tx.decode_keys(account, POOL).fee_rate == pytest.approx(0.02)


def test_token_program_flag_zero_is_spl_token():
    assert tx.decode_keys(pool_account((0, 0)), POOL).program_x == tx.TOKEN


def test_liquid_arrays_bit_is_index_plus_512():
    words = [0] * 16
    words[3] = 1 << 5  # bit 197 -> index -315
    words[15] = 1 << 63  # bit 1023 -> index 511
    words[0] = 1  # bit 0 -> index -512
    assert tx.liquid_arrays(words) == {-315, 511, -512}


def test_extension_bits_follow_to_bin_array_index():
    raw = bytearray(1576)
    raw[:8] = tx.EXT_DISCRIMINATOR
    # Positive side, row 0, word 0, bit 3: (0 + 1) * 512 + 3 = 515.
    struct.pack_into("<Q", raw, tx.EXT_POSITIVE, 1 << 3)
    # Negative side, row 1, word 1, bit 2: -((1 + 1) * 512 + 64 + 2) - 1 = -1091.
    struct.pack_into("<Q", raw, tx.EXT_POSITIVE + tx.EXT_ROWS * 64 + 64 + 8, 1 << 2)
    account = {"data": [base64.b64encode(bytes(raw)).decode(), "base64"]}
    assert tx.liquid_arrays_extension(account) == {515, -1091}


def test_swap_walks_down_to_sell_and_up_to_buy():
    liquid = {-12, -10, -9, -7, -5}
    assert tx.swap_arrays(-630, True, liquid) == [-9, -10, -12]  # -630 is array -9
    assert tx.swap_arrays(-630, False, liquid) == [-9, -7, -5]
    assert tx.swap_arrays(-623, True, liquid, take=1) == [-9]  # the start itself counts
    assert tx.swap_arrays(-700, False, {-5}) == [-5]


def test_overflow_is_outside_the_pools_own_bitmap():
    assert not tx.overflows_bitmap(511) and not tx.overflows_bitmap(-512)
    assert tx.overflows_bitmap(512) and tx.overflows_bitmap(-513)


def state(active=0, x=10**12, step=100) -> PoolState:
    rows = {b: {"amounts": [x if b >= active else 0, 0 if b > active else 10**12], "supply": 1, "fee_growth": [0, 0]}
            for b in range(active - 5, active + 6)}
    return PoolState(slot=1, at_ms=0, active=active, bin_step=step, bins=rows)


def test_quote_buy_walks_up_and_pays_the_fee():
    s = state(x=1_000)
    p = [bin_price(b, 100) for b in range(0, 3)]
    got, left, last = tx.quote_buy(1_000 * p[0] / 0.98, s, 0.02)
    assert left == pytest.approx(0, abs=1e-6)
    assert got == pytest.approx(1_000)
    assert last == 0
    got, _, last = tx.quote_buy(2_500 * p[1] / 0.98, s, 0.02)
    assert last == 2 and 2_000 < got <= 3_000


def test_quote_buy_reports_what_it_could_not_spend():
    s = state(x=10)
    got, left, _ = tx.quote_buy(10**9, s, 0.0)
    assert got == pytest.approx(60)
    assert left > 0


# ----------------------------------------------------------------------------------- the rent refusal
def test_one_sided_needing_an_array_is_refused_by_default():
    with pytest.raises(tx.RentRefused, match="bin_array_rent_refused"):
        tx.open_one_sided(pool_keys(), OWNER, POSITION, lo=-520, hi=-501, lamports=10**9, active=-500, up_bins=10,
                          missing=frozenset({-8}))


def test_two_sided_needing_an_array_is_refused_by_default():
    with pytest.raises(tx.RentRefused, match="bin_array_rent_refused"):
        tx.open_two_sided(pool_keys(), OWNER, POSITION, lo=-530, hi=-470, lamports=10**9, swap_in=5, swap_min_out=1, arrays=[-8],
                          active=-500, max_slippage=3, missing=frozenset({-7}))


def test_a_missing_array_outside_the_range_costs_nothing():
    built = tx.open_one_sided(pool_keys(), OWNER, POSITION, lo=-520, hi=-501, lamports=10**9, active=-500, up_bins=10,
                              missing=frozenset({-20, 3}))
    assert not any(named({"data": ix.data}) == "initialize_bin_array" for ix in built)


def test_rent_only_with_the_explicit_setting():
    built = tx.open_one_sided(pool_keys(), OWNER, POSITION, lo=-520, hi=-501, lamports=10**9, active=-500, up_bins=10,
                              missing=frozenset({-8}), allow_bin_array_rent=True)
    first = built[0]
    assert named({"data": first.data}) == "initialize_bin_array"
    assert first.data[8:] == struct.pack("<q", -8)
    assert [m.key for m in first.metas] == [POOL, tx.bin_array_key(POOL, -8), OWNER, tx.SYSTEM]


def test_a_missing_bitmap_extension_is_refused_even_with_rent_allowed():
    with pytest.raises(tx.RentRefused, match="bitmap_extension_missing"):
        tx.open_one_sided(pool_keys(), OWNER, POSITION, lo=-50_000, hi=-49_981, lamports=10**9, active=-49_980, up_bins=10,
                          allow_bin_array_rent=True)


def test_an_existing_extension_is_used():
    built = tx.open_one_sided(pool_keys(extension=key(31)), OWNER, POSITION, lo=-50_000, hi=-49_981, lamports=10**9,
                              active=-49_980, up_bins=10)
    add = next(i for i in built if named({"data": i.data}) == "add_liquidity_by_strategy2")
    assert add.metas[2].key == key(31)


# ------------------------------------------------------------------------------- whole transactions
def test_the_bound_is_one_sided_by_centring_it():
    assert tx.entry_bound(-500, 25) == (-475, 25)


def test_one_sided_order_and_amounts():
    ixs = tx.open_one_sided(pool_keys(), OWNER, POSITION, lo=-520, hi=-501, lamports=10**9, active=-500, up_bins=100)
    got = build(ixs)
    assert [i["program"] for i in got["ixs"][:6]] == [tx.COMPUTE_BUDGET, tx.COMPUTE_BUDGET, tx.ASSOCIATED_TOKEN,
                                                      tx.ASSOCIATED_TOKEN, tx.SYSTEM, tx.TOKEN]
    assert [named(i) for i in got["ixs"][6:8]] == ["initialize_position", "add_liquidity_by_strategy2"]
    last = got["ixs"][8]
    assert last["program"] == tx.TOKEN and last["data"] == b"\x09"  # unwrap whatever the deposit left
    assert struct.unpack_from("<QQii", got["ixs"][7]["data"], 8) == (0, 10**9, -400, 100)
    assert struct.unpack_from("<ii", got["ixs"][6]["data"], 8) == (-520, 20)
    assert got["ixs"][4]["data"] == struct.pack("<IQ", 2, 10**9)
    assert got["signed"] == 2 and got["keys"][1] == POSITION


def test_one_sided_may_not_touch_the_active_bin():
    with pytest.raises(tx.LpTxError, match="one_sided_range_touches_active_bin"):
        tx.open_one_sided(pool_keys(), OWNER, POSITION, lo=-520, hi=-500, lamports=10**9, active=-500, up_bins=1)


def test_two_sided_buys_inside_the_same_transaction():
    ixs = tx.open_two_sided(pool_keys(), OWNER, POSITION, lo=-530, hi=-470, lamports=10**9, swap_in=5 * 10**8, swap_min_out=77,
                            arrays=[-8, -7], active=-498, max_slippage=8)
    got = build(ixs)
    names = [named(i) for i in got["ixs"]]
    assert names.index("swap2") < names.index("initialize_position") < names.index("add_liquidity_by_strategy2")
    add = got["ixs"][names.index("add_liquidity_by_strategy2")]
    assert struct.unpack_from("<QQii", add["data"], 8) == (77, 5 * 10**8, -498, 8)
    assert got["ixs"][4]["data"] == struct.pack("<IQ", 2, 10**9)


def test_reopen_deposits_wallet_token_with_no_swap():
    ixs = tx.open_two_sided(pool_keys(), OWNER, POSITION, lo=-510, hi=-490, lamports=4 * 10**8, swap_in=0, swap_min_out=0,
                            arrays=(), amount_x=55, active=-500, max_slippage=8)
    got = build(ixs)
    names = [named(i) for i in got["ixs"]]
    assert "swap2" not in names
    add = got["ixs"][names.index("add_liquidity_by_strategy2")]
    assert struct.unpack_from("<QQ", add["data"], 8) == (55, 4 * 10**8)


def test_token_only_spans_the_range_and_deposits_above_the_price():
    ixs = tx.open_token_only(pool_keys(), OWNER, POSITION, lo=-510, hi=-490, amount_x=55, active=-500, down_bins=8)
    got = build(ixs)
    names = [named(i) for i in got["ixs"]]
    assert tx.SYSTEM not in [i["program"] for i in got["ixs"]]  # No wrap: no SOL goes in.
    assert struct.unpack_from("<ii", got["ixs"][names.index("initialize_position")]["data"], 8) == (-510, 21)
    add = got["ixs"][names.index("add_liquidity_by_strategy2")]
    assert struct.unpack_from("<QQiiii", add["data"], 8) == (55, 0, -508, 8, -499, -490)
    assert add["data"][8 + struct.calcsize("<QQiiiiB")] == 1  # favours X, as the client does for Y of zero


@pytest.mark.parametrize("active", [-511, -490])
def test_token_only_needs_room_above_the_price(active):
    with pytest.raises(tx.LpTxError, match="token_only_range_misses_active_bin"):
        tx.open_token_only(pool_keys(), OWNER, POSITION, lo=-510, hi=-490, amount_x=1, active=active, down_bins=8)


@pytest.mark.parametrize("active", [-531, -469])
def test_two_sided_must_straddle_the_price(active):
    with pytest.raises(tx.LpTxError, match="two_sided_range_misses_active_bin"):
        tx.open_two_sided(pool_keys(), OWNER, POSITION, lo=-530, hi=-470, lamports=10**9, swap_in=1, swap_min_out=1, arrays=[-8],
                          active=active, max_slippage=1)


def test_the_swap_cannot_take_the_whole_ticket():
    with pytest.raises(tx.LpTxError, match="swap_in_out_of_range"):
        tx.open_two_sided(pool_keys(), OWNER, POSITION, lo=-530, hi=-470, lamports=10, swap_in=10, swap_min_out=1, arrays=[-8],
                          active=-500, max_slippage=1)


def test_a_pool_not_quoted_in_sol_is_refused():
    with pytest.raises(tx.LpTxError, match="pool_quote_not_sol_y"):
        tx.open_one_sided(pool_keys(mint_y=key(33)), OWNER, POSITION, lo=-520, hi=-501, lamports=10**9, active=-500, up_bins=1)


def test_exit_order_is_the_clients():
    got = build(tx.exit_position(pool_keys(), OWNER, POSITION, lo=-520, hi=-501, has_shares=True))
    names = [named(i) for i in got["ixs"] if named(i)]
    assert names == ["remove_liquidity_by_range2", "claim_fee2", "close_position_if_empty"]
    assert got["ixs"][-1]["program"] == tx.TOKEN and got["ixs"][-1]["data"] == b"\x09"
    assert got["signed"] == 1


def test_exit_carries_no_price_bound():
    """Nothing here depends on where the pool is, which is what lets it land after a move."""
    built = tx.exit_position(pool_keys(active=-500), OWNER, POSITION, lo=-520, hi=-501, has_shares=True)
    assert built == tx.exit_position(pool_keys(active=9_999), OWNER, POSITION, lo=-520, hi=-501, has_shares=True)


def test_exit_with_fees_only_skips_remove():
    got = build(tx.exit_position(pool_keys(), OWNER, POSITION, lo=-520, hi=-501, has_shares=False))
    names = [named(i) for i in got["ixs"] if named(i)]
    assert names == ["claim_fee2", "close_position_if_empty"]


def test_exit_with_nothing_left_is_only_the_close():
    got = build(tx.exit_position(pool_keys(), OWNER, POSITION, lo=None, hi=None, has_shares=False))
    assert [named(i) for i in got["ixs"] if named(i)] == ["close_position_if_empty"]


def test_sweep_sells_unwraps_and_recovers_the_token_account():
    got = build(tx.sweep(pool_keys(), OWNER, amount_in=9, min_out=4, arrays=[-8], close_token=True))
    programs = [i["program"] for i in got["ixs"]]
    assert programs[2:] == [tx.ASSOCIATED_TOKEN, PROGRAM, tx.TOKEN, tx.TOKEN_2022]
    assert got["ixs"][-1]["data"] == b"\x09"
    assert got["ixs"][-1]["accounts"][0] == tx.ata_address(OWNER, MINT, tx.TOKEN_2022)


def test_sweep_keeps_the_account_when_token_remains():
    got = build(tx.sweep(pool_keys(), OWNER, amount_in=9, min_out=4, arrays=[-8], close_token=False))
    assert tx.TOKEN_2022 not in [i["program"] for i in got["ixs"]]


def test_sweep_of_an_empty_account_needs_no_swap():
    got = build(tx.sweep(pool_keys(), OWNER, amount_in=0, min_out=0, arrays=(), close_token=True))
    assert [i["program"] for i in got["ixs"]][2:] == [tx.TOKEN_2022]


def test_sweep_can_unwrap_alone():
    got = build(tx.sweep(pool_keys(), OWNER, amount_in=0, min_out=0, arrays=(), close_token=False, unwrap=True))
    assert [i["program"] for i in got["ixs"]][2:] == [tx.TOKEN]
    assert got["ixs"][2]["accounts"][0] == tx.ata_address(OWNER, tx.NATIVE_MINT, tx.TOKEN)


def test_sweep_with_nothing_to_do_is_refused():
    with pytest.raises(tx.LpTxError, match="sweep_has_nothing_to_do"):
        tx.sweep(pool_keys(), OWNER, amount_in=0, min_out=0, arrays=(), close_token=False)


def test_a_separate_payer_pays_and_the_owner_still_signs():
    got = build(tx.open_one_sided(pool_keys(), OWNER, POSITION, lo=-520, hi=-501, lamports=10**9, active=-500, up_bins=1, payer=PAYER),
                payer=PAYER)
    assert got["keys"][0] == PAYER
    assert OWNER in got["keys"][:got["signed"]]


def test_the_largest_open_fits_one_packet():
    """Token-2022, an extension, a two-array range, and a swap with both arrays outside it, which
    happens when the active array holds no liquidity: the most a `lp_live.OPEN_SWAP_ARRAYS` swap can add."""
    ixs = tx.open_two_sided(pool_keys(extension=key(31)), OWNER, POSITION, lo=-530, hi=-470, lamports=10**9, swap_in=5 * 10**8,
                            swap_min_out=7, arrays=[-6, -5], active=-500, max_slippage=8)
    message = tx.compile_message(OWNER, tx.finish(ixs, 500_000, 295_000), HASH)
    assert message.size <= tx.MAX_TX_BYTES


def test_three_outside_arrays_with_an_extension_would_not_fit():
    """Why opens are held to two: this is the case that overflows, and it must refuse, not truncate."""
    ixs = tx.open_two_sided(pool_keys(extension=key(31)), OWNER, POSITION, lo=-530, hi=-470, lamports=10**9, swap_in=5 * 10**8,
                            swap_min_out=7, arrays=[-6, -5, -3], active=-500, max_slippage=8)
    with pytest.raises(tx.TxTooLarge, match="transaction_too_large"):
        tx.compile_message(OWNER, tx.finish(ixs, 500_000, 295_000), HASH)


def test_arrays_cover_exactly_the_range():
    assert arrays_for(-71, -70) == [-2, -1]
    ix = tx.remove_all_liquidity(pool_keys(), OWNER, POSITION, -70, -1)
    assert [m.key for m in ix.metas[15:]] == [tx.bin_array_key(POOL, -1)]
