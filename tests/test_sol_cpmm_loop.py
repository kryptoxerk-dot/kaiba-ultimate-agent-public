import base64
import struct

import base58
import pytest

from kaiba.learning import sol_cpmm_loop as study


def key(n):
    return base58.b58encode(bytes([n]) * 32).decode()


def packed(raw, owner):
    return {"data": [base64.b64encode(raw).decode(), "base64"],
            "owner": owner, "executable": False}


def encode_u64(raw, offset, value):
    struct.pack_into("<Q", raw, offset, value)


def test_integer_input_and_output_creator_fee_rounding():
    mint = key(1)
    pool = study.Pool(key(2), (study.WSOL_MINT, mint), (100_000, 200_000), 2_500, 500, 1)
    # First direction charges a combined input fee rounded up. Reverse charges
    # the trade fee on input and the creator fee on curve output.
    assert pool.swap(study.WSOL_MINT, 1_001) == (1_974, True)
    assert pool.swap(mint, 1_001) == (495, True)
    assert pool.swap(study.WSOL_MINT, 5_001)[1] is False


def test_pool_reads_effective_reserves_and_binds_vault_to_mint():
    p, config, mint = (key(n) for n in range(1, 4))
    a, b = (study.find_program_address(
        [b"pool_vault", base58.b58decode(p), base58.b58decode(token)], study.CPMM,
    )[0] for token in (study.WSOL_MINT, mint))
    raw = bytearray(637)
    raw[:8] = study.discriminator("PoolState")
    for offset, address in ((8, config), (72, a), (104, b), (168, study.WSOL_MINT),
                            (200, mint), (232, study.SPL), (264, study.SPL)):
        raw[offset:offset + 32] = base58.b58decode(address)
    raw[328] = study.AUTH_BUMP
    raw[331:333] = bytes((9, 6))
    raw[389:391] = bytes((1, 1))
    for offset, value in ((341, 10), (357, 11), (397, 12), (349, 20), (373, 80)):
        encode_u64(raw, offset, value)
    cfg = bytearray(236)
    cfg[:8] = study.discriminator("AmmConfig")
    encode_u64(cfg, 12, 2500)
    encode_u64(cfg, 108, 500)
    accounts = {p: packed(raw, study.CPMM), config: packed(cfg, study.CPMM)}
    for address, vault_key, decimals in ((study.WSOL_MINT, a, 9), (mint, b, 6)):
        m = bytearray(82)
        m[44:46] = bytes((decimals, 1))
        vault = bytearray(165)
        vault[:32] = base58.b58decode(address)
        vault[32:64] = base58.b58decode(study.AUTHORITY)
        vault[108] = 1
        encode_u64(vault, 64, 1000)
        accounts[address] = packed(m, study.SPL)
        accounts[vault_key] = packed(vault, study.SPL)
    assert study.read_pool(p, mint, accounts, 80).reserves == (967, 980)
    with pytest.raises(ValueError, match="pool_not_open"):
        study.read_pool(p, mint, accounts, 79)
    reused = bytearray(raw)
    reused[104:136] = base58.b58decode(a)
    with pytest.raises(ValueError, match="vault_address"):
        study.read_pool(p, mint, {**accounts, p: packed(reused, study.CPMM)}, 80)
    accounts[b] = accounts[a]
    with pytest.raises(ValueError, match="vault_identity"):
        study.read_pool(p, mint, accounts, 80)


def test_bank_refuses_a_clock_from_a_different_slot():
    raw = bytearray(40)
    encode_u64(raw, 0, 5)
    encode_u64(raw, 32, 20)
    response = {"keys": [study.CLOCK], "commitment": "finalized",
                "result": {"context": {"slot": 6}, "value": [packed(raw, study.SYSVAR)]}}
    with pytest.raises(ValueError, match="bank_clock"):
        study.bank(response)
    response["result"]["context"]["slot"] = 5
    assert study.bank(response)[:2] == (5, 20)


def test_positive_loop_requires_distinct_pools_and_enough_reserves():
    mint = key(1)
    mints = (study.WSOL_MINT, mint)
    buy = study.Pool(key(2), mints, (10_000_000_000, 20_000_000_000), 2500, 0, 0)
    sell = study.Pool(key(3), mints, (20_000_000_000, 20_000_000_000), 2500, 0, 0)
    assert study.loop(buy, sell, mint, 100_000_000)["simulate"]
    assert not study.loop(buy, sell, mint, 1_000_000_000)["simulate"]
    with pytest.raises(ValueError, match="loop_pool_identity"):
        study.loop(buy, buy, mint, 100_000_000)
