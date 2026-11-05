"""Price two distinct Raydium CPMM swaps from one retained Solana bank."""

from __future__ import annotations

import argparse
import base64
import hashlib
import itertools
import json
import struct
from dataclasses import dataclass
from pathlib import Path

import base58

from kaiba.execution.fill_costs import find_program_address
from kaiba.execution.pumpswap_price import WSOL_MINT

CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
SPL = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
CLOCK = "SysvarC1ock11111111111111111111111111111111"
SYSVAR = "Sysvar1111111111111111111111111111111111111"
AUTHORITY, AUTH_BUMP = find_program_address([b"vault_and_lp_mint_auth_seed"], CPMM)
SIZE = (100_000_000, 300_000_000)
FEE_DENOMINATOR = 1_000_000


def discriminator(name: str) -> bytes:
    return hashlib.sha256(f"account:{name}".encode()).digest()[:8]


def public_key(raw: bytes, offset: int) -> str:
    return base58.b58encode(raw[offset:offset + 32]).decode()


def u64(raw: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", raw, offset)[0]


def account(accounts: dict, key: str, owner: str, size: int, kind: str = "") -> bytes:
    value = accounts.get(key)
    if not isinstance(value, dict) or value.get("owner") != owner or value.get("executable") is not False:
        raise ValueError("account_owner_or_availability")
    try:
        encoded, fmt = value["data"]
        raw = base64.b64decode(encoded, validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("account_encoding") from exc
    if fmt != "base64" or len(raw) != size or (kind and raw[:8] != discriminator(kind)):
        raise ValueError("account_layout")
    return raw


@dataclass(frozen=True)
class Pool:
    key: str
    mints: tuple[str, str]
    reserves: tuple[int, int]
    trade_fee: int
    creator_fee: int
    fee_mode: int

    def swap(self, source: str, amount: int) -> tuple[int, bool]:
        index = self.mints.index(source)
        reserve_in, reserve_out = self.reserves[index], self.reserves[1 - index]
        creator_on_input = self.fee_mode in (0, index + 1)
        rate = self.trade_fee + (self.creator_fee if creator_on_input else 0)
        amount_in = amount - fee(amount, rate)
        output = (amount_in * reserve_out) // (reserve_in + amount_in)
        if not creator_on_input:
            output -= fee(output, self.creator_fee)
        return output, 0 < amount <= reserve_in // 20


def fee(amount: int, rate: int) -> int:
    return (amount * rate + FEE_DENOMINATOR - 1) // FEE_DENOMINATOR


def read_pool(key: str, other_mint: str, accounts: dict, clock_seconds: int) -> Pool:
    raw = account(accounts, key, CPMM, 637, "PoolState")
    if raw[328] != AUTH_BUMP or raw[329] & 4 or u64(raw, 373) > clock_seconds:
        raise ValueError("pool_not_open")
    mints = public_key(raw, 168), public_key(raw, 200)
    if set(mints) != {WSOL_MINT, other_mint} or other_mint == WSOL_MINT:
        raise ValueError("pool_pair")
    if any(public_key(raw, offset) != SPL for offset in (232, 264)):
        raise ValueError("nonclassic_token")
    config = account(accounts, public_key(raw, 8), CPMM, 236, "AmmConfig")
    trade_fee, creator_fee, mode = u64(config, 12), u64(config, 108), raw[389]
    if mode not in (0, 1, 2) or raw[390] not in (0, 1):
        raise ValueError("fee_model")
    creator_fee = creator_fee if raw[390] else 0
    if trade_fee + creator_fee >= FEE_DENOMINATOR:
        raise ValueError("fee_rate")
    vault_keys = public_key(raw, 72), public_key(raw, 104)
    reserves = []
    for index, (mint, vault_key) in enumerate(zip(mints, vault_keys, strict=True)):
        expected_vault, _ = find_program_address(
            [b"pool_vault", base58.b58decode(key), base58.b58decode(mint)], CPMM,
        )
        if vault_key != expected_vault:
            raise ValueError("vault_address")
        mint_data = account(accounts, mint, SPL, 82)
        vault = account(accounts, vault_key, SPL, 165)
        if mint_data[45] != 1 or mint_data[44] != raw[331 + index]:
            raise ValueError("mint_identity")
        if vault[108] != 1 or public_key(vault, 0) != mint or public_key(vault, 32) != AUTHORITY:
            raise ValueError("vault_identity")
        owed = sum(u64(raw, offset + index * 8) for offset in (341, 357, 397))
        reserve = u64(vault, 64) - owed
        if reserve <= 0:
            raise ValueError("empty_effective_reserve")
        reserves.append(reserve)
    return Pool(key, mints, (reserves[0], reserves[1]), trade_fee, creator_fee, mode)


def bank(snapshot: dict) -> tuple[int, int, dict]:
    keys = snapshot["keys"]
    result = snapshot["result"]
    slot = result["context"]["slot"]
    if (snapshot["commitment"] != "finalized" or type(slot) is not int or slot <= 0
            or not 0 < len(keys) <= 100 or len(set(keys)) != len(keys)):
        raise ValueError("bank_identity")
    accounts = dict(zip(keys, result["value"], strict=True))
    clock = account(accounts, CLOCK, SYSVAR, 40)
    clock_seconds = struct.unpack_from("<q", clock, 32)[0]
    if u64(clock, 0) != slot or clock_seconds <= 0:
        raise ValueError("bank_clock")
    return slot, clock_seconds, accounts


def loop(buy: Pool, sell: Pool, mint: str, size: int) -> dict:
    if buy.key == sell.key or set(buy.mints) != set(sell.mints) or mint not in buy.mints:
        raise ValueError("loop_pool_identity")
    tokens, buy_depth = buy.swap(WSOL_MINT, size)
    if tokens > 0:
        final, sell_depth = sell.swap(mint, tokens)
    else:
        final, sell_depth = 0, False
    base = final - size - 25_000
    stress = final - size - 100_000 - (size + 999) // 1000
    depth = buy_depth and sell_depth
    return {
        "mint": mint, "buy": buy.key, "sell": sell.key, "size": size, "intermediate": tokens,
        "ending_lamports": final, "net_base": base, "net_stress": stress, "depth_pass": depth,
        "simulate": depth and stress > 0,
    }


def analyze(capture: dict) -> dict:
    pairs = capture["pairs"]
    snapshots = capture["snapshots"]
    if len(pairs) > 3 or not 1 <= len(snapshots) <= 3:
        raise ValueError("study_scope")
    slots = []
    results = []
    refusals = []
    for snapshot in snapshots:
        slot, clock_seconds, accounts = bank(snapshot)
        if slots and slot <= slots[-1]:
            raise ValueError("nonadvancing_slot")
        slots.append(slot)
        for mint, keys in pairs.items():
            if len(keys) > 4 or len(set(keys)) != len(keys):
                raise ValueError("pair_scope")
            pools = []
            for key in keys:
                try:
                    pools.append(read_pool(key, mint, accounts, clock_seconds))
                except ValueError as exc:
                    refusals.append({"slot": slot, "pool": key, "reason": str(exc)})
            for buy, sell in itertools.permutations(pools, 2):
                for size in SIZE:
                    results.append({"slot": slot, **loop(buy, sell, mint, size)})
    return {"slots": slots, "results": results, "refusals": refusals}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    args = parser.parse_args()
    if args.capture.stat().st_size > 1_000_000:
        raise ValueError("capture_too_large")
    print(json.dumps(analyze(json.loads(args.capture.read_bytes())), indent=2))


if __name__ == "__main__":
    main()
