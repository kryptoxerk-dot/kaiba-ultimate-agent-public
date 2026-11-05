"""The live LP runner: its gate, its caps, its sends, and what happens when they go wrong.

Nothing here touches a network. `Ledger` is a small chain: it parses each transaction's real bytes and
executes them against balances, positions and bins. A simulation runs the same executor on a copy.
So a swap under its minimum really reverts, and a close really refuses an account holding token,
and each safety test exercises the path a real wallet would take, with a positive control.

Each guard here is mutation-checked. Run `python -m tests.test_lp_live --mutants` from the repository
root to remove each one in turn and confirm the suite fails. A guard this suite passes without is a
bug in this suite. The `-m` matters: run as a script, `kaiba` can resolve to another checkout.
"""

from __future__ import annotations

import base64
import copy
import json
import re
import sqlite3
import struct
import subprocess
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

import base58
import pytest
from nacl.signing import SigningKey

from kaiba.execution import lp_live as live
from kaiba.execution import lp_tx as tx
from kaiba.execution.lp_model import POSITION_RENT_LAMPORTS, Arm, Plan, PoolState, bin_price, fees, holdings
from kaiba.execution.lp_paper import FAST
from kaiba.learning.dlmm_state import DISCRIMINATORS, PROGRAM, decode_bins, decode_position, snapshot
from tests.test_dlmm_state import fixture as capture

SOL = 1_000_000_000
LIQUIDITY = 10**15
ATA_RENT = 2_039_280
ARM = Arm("t", 4, 100, 6.0, 2.0, 1_000)
PROFILE = replace(FAST, arms=(ARM,), primary="t", max_open=5, poll_s=1.0, select_s=1.0, cooldown_s=0.0)


def key(n: int) -> str:
    return base58.b58encode(bytes(SigningKey(bytes([n]) * 32).verify_key)).decode()


WALLET_KEY = SigningKey(bytes([1]) * 32)
WALLET = key(1)
KAIBA = key(2)
POOL_A, POOL_B, POOL_C = key(3), key(4), key(5)
MINTS = {POOL_A: key(6), POOL_B: key(7), POOL_C: key(8)}


def parse(raw: bytes) -> tuple[list[str], list[dict[str, Any]], int]:
    def compact(at: int) -> tuple[int, int]:
        value = shift = 0
        while True:
            byte = raw[at]
            at += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return value, at

    sigs, at = compact(0)
    at += 64 * sigs + 3
    n, at = compact(at)
    keys = [base58.b58encode(raw[at + 32 * i:at + 32 * (i + 1)]).decode() for i in range(n)]
    n, at = compact(at + 32 * n + 32)
    out = []
    for _ in range(n):
        program = keys[raw[at]]
        count, at = compact(at + 1)
        accounts = [keys[i] for i in raw[at:at + count]]
        length, at = compact(at + count)
        out.append({"program": program, "accounts": accounts, "data": raw[at:at + length]})
        at += length
    return keys, out, sigs


def kind_of(ix: dict[str, Any]) -> str:
    return next((name for name, spec in tx.INSTRUCTIONS.items() if ix["data"][:8] == spec.discriminator), "")


class Reverted(Exception):
    def __init__(self, code: int):
        self.code = code


class Ledger:
    def __init__(self) -> None:
        self.clock = 1_800_000_000.0
        self.height = 5_000
        self.slot = 1_000
        self.world: dict[str, Any] = {"lamports": 5 * SOL, "tokens": {}, "positions": {}}
        self.active = {POOL_A: -500, POOL_B: -500, POOL_C: -500}
        self.missing: set[int] = set()
        self.rewards = False
        self.status = 0
        self.landing = "land"
        self.raise_on_send = False
        self.fail_reads: set[str] = set()
        self.sim_error: dict[str, Any] | None = None
        self.bad_price = 0.0  # share of a token sale's quote that the pool will not pay
        self.tx_unreadable = False
        self.sent: list[bytes] = []
        self.statuses: dict[str, dict[str, Any]] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self.calls: Counter[str] = Counter()
        self.arrays = {tx.bin_array_key(p, i): (p, i) for p in self.active for i in range(-14, -3)}
        self.exts = {tx.bitmap_extension_address(p) for p in self.active}
        self.atas = {tx.ata_address(WALLET, m, tx.TOKEN_2022): m for m in MINTS.values()}
        self.wsol = tx.ata_address(WALLET, tx.NATIVE_MINT, tx.TOKEN)
        self.gate_seen: list[Any] = []
        self.on_send: Any = None

    def now(self) -> float:
        return self.clock

    base_factor = 30_000  # 3% at bin step 100, what `swap` below pays.

    def next_key(self) -> SigningKey:
        self.keys_made += 1
        return SigningKey(bytes([100 + self.keys_made]) * 32)

    keys_made = 0

    # ---------------------------------------------------------------------------------- accounts
    def pool_account(self, pool: str) -> dict[str, Any]:
        raw = bytearray(904)
        raw[:8] = DISCRIMINATORS["pool"]
        struct.pack_into("<iH", raw, 76, self.active[pool], 100)
        struct.pack_into("<H", raw, tx.LB_BASE_FACTOR, self.base_factor)
        raw[35], raw[82], raw[882] = 2, self.status, 1
        for at, value in ((88, MINTS[pool]), (120, tx.NATIVE_MINT), (152, key(10)), (184, key(11)), (552, key(12))):
            raw[at:at + 32] = base58.b58decode(value)
        raw[880] = 1
        if self.rewards:
            raw[264:296] = base58.b58decode(key(13))
        for index in range(-14, -3):
            bit = index + 512
            value = struct.unpack_from("<Q", raw, 584 + 8 * (bit // 64))[0] | 1 << (bit % 64)
            struct.pack_into("<Q", raw, 584 + 8 * (bit // 64), value)
        return self.account(bytes(raw))

    @staticmethod
    def account(raw: bytes) -> dict[str, Any]:
        return {"data": [base64.b64encode(raw).decode(), "base64"], "owner": PROGRAM, "executable": False, "lamports": 1}

    def bin_row(self, pool: str, b: int) -> bytes:
        active, p = self.active[pool], bin_price(b, 100)
        x = 0 if b < active else int(LIQUIDITY / p / (2 if b == active else 1))
        y = 0 if b > active else LIQUIDITY // (2 if b == active else 1)
        row = struct.pack("<QQ", x, y) + int(p * 2**64).to_bytes(16, "little") + (LIQUIDITY << 64).to_bytes(16, "little")
        growth = int(self.growth * 2**64).to_bytes(16, "little")
        return row + bytes(32) + growth + growth + bytes(32)

    growth = 0.0

    def bin_account(self, pool: str, index: int) -> dict[str, Any]:
        head = bytes(DISCRIMINATORS["bins"]) + struct.pack("<qB7x", index, 2) + base58.b58decode(pool)
        return self.account(head + b"".join(self.bin_row(pool, index * 70 + i) for i in range(70)))

    def position_account(self, address: str, world: dict[str, Any]) -> dict[str, Any]:
        p = world["positions"][address]
        lo, hi = p["lo"], p["hi"]
        shares = [p["shares"].get(lo + i, 0) for i in range(70)]
        fee = bytes(16) * 2 + struct.pack("<QQ", 0, 0)
        data = (bytes(DISCRIMINATORS["position"]) + base58.b58decode(p["pool"]) + base58.b58decode(WALLET) +
                b"".join(s.to_bytes(16, "little") for s in shares) + bytes(48 * 70) + fee * 70 +
                struct.pack("<iiqQQQQ", lo, hi, 0, 0, 0, 0, 0) + bytes(32) + struct.pack("<Q", 0) + bytes(1) +
                bytes(32) + bytes([1, 0]) + bytes(85))
        return self.account(data)

    def parsed_token(self, account: str, world: dict[str, Any]) -> dict[str, Any] | None:
        if account not in world["tokens"]:
            return None
        return {"data": {"parsed": {"info": {"tokenAmount": {"amount": str(world["tokens"][account])}}}}}

    # --------------------------------------------------------------------------------------- RPC
    def api(self, path: str) -> dict[str, Any]:
        return {"data": [pool_row(p) for p in (self.listed or self.active)]}

    listed: list[str] | None = None

    def rpc(self, method: str, params: list[Any]) -> Any:
        self.calls[method] += 1
        if method in self.fail_reads:
            raise RuntimeError(f"{method} down")
        self.slot += 1
        if method == "getMultipleAccounts":
            keys, options = params
            return {"context": {"slot": self.slot}, "value": [self.lookup(k, options) for k in keys]}
        if method == "getAccountInfo":
            return {"value": {"data": {"parsed": {"info": {"freezeAuthority": None, "extensions": []}}}}}
        if method == "getBalance":
            return {"value": self.world["lamports"]}
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": tx.SYSTEM, "lastValidBlockHeight": self.height + 150}}
        if method == "getBlockHeight":
            return self.height
        if method == "getSignatureStatuses":
            return {"value": [self.statuses.get(params[0][0])]}
        if method == "getTransaction":
            return None if self.tx_unreadable else self.results.get(params[0])
        if method == "getProgramAccounts":
            return [{"pubkey": a} for a in self.world["positions"]]
        if method == "simulateTransaction":
            return {"value": self.simulate(base64.b64decode(params[0]))}
        raise AssertionError(method)

    def lookup(self, k: str, options: dict[str, Any]) -> Any:
        if k in self.active:
            return self.pool_account(k)
        if k in self.exts:
            return None
        if k in self.arrays:
            pool, index = self.arrays[k]
            return None if index in self.missing else self.bin_account(pool, index)
        if k in self.world["positions"]:
            return self.position_account(k, self.world)
        if options.get("encoding") == "jsonParsed":
            return self.parsed_token(k, self.world)
        return None

    def simulate(self, raw: bytes) -> dict[str, Any]:
        if self.sim_error:
            return {"err": self.sim_error, "unitsConsumed": 1000}
        try:
            self.execute(copy.deepcopy(self.world), raw)
        except Reverted as exc:
            return {"err": {"InstructionError": [0, {"Custom": exc.code}]}, "unitsConsumed": 1000}
        return {"err": None, "unitsConsumed": 200_000}

    # ---------------------------------------------------------------------------------- execution
    def execute(self, w: dict[str, Any], raw: bytes) -> None:
        _, ixs, _ = parse(raw)
        for ix in ixs:
            self.run_ix(w, ix)

    def run_ix(self, w: dict[str, Any], ix: dict[str, Any]) -> None:
        program, accounts, data = ix["program"], ix["accounts"], ix["data"]
        if program == tx.ASSOCIATED_TOKEN:
            if accounts[1] not in w["tokens"]:
                w["tokens"][accounts[1]] = 0
                w["lamports"] -= ATA_RENT
        elif program == tx.SYSTEM:
            _, lamports = struct.unpack("<IQ", data)
            w["lamports"] -= lamports
            w["tokens"][accounts[1]] += lamports
        elif program in (tx.TOKEN, tx.TOKEN_2022) and data == b"\x09":
            account = accounts[0]
            held = w["tokens"].get(account)
            if held is None:
                raise Reverted(9001)
            if held and account != self.wsol:
                raise Reverted(9002)  # The real token program refuses a nonzero balance.
            w["lamports"] += held + ATA_RENT
            del w["tokens"][account]
        elif program == PROGRAM:
            self.run_dlmm(w, kind_of(ix), accounts, data)

    def run_dlmm(self, w: dict[str, Any], name: str, accounts: list[str], data: bytes) -> None:
        if name == "initialize_bin_array":
            index = struct.unpack_from("<q", data, 8)[0]
            self.missing.discard(index)
            w["lamports"] -= 71_437_440
        elif name == "initialize_position":
            lo, width = struct.unpack_from("<ii", data, 8)
            w["positions"][accounts[1]] = {"pool": accounts[2], "lo": lo, "hi": lo + width - 1, "shares": {}}
            w["lamports"] -= POSITION_RENT_LAMPORTS
        elif name == "add_liquidity_by_strategy2":
            self.add(w, accounts, data)
        elif name == "remove_liquidity_by_range2":
            self.remove(w, accounts)
        elif name == "close_position_if_empty":
            p = w["positions"].get(accounts[0])
            if p and not any(p["shares"].values()):
                del w["positions"][accounts[0]]
                w["lamports"] += POSITION_RENT_LAMPORTS
        elif name == "swap2":
            self.swap(w, accounts, data)

    def add(self, w: dict[str, Any], accounts: list[str], data: bytes) -> None:
        amount_x, amount_y, active_id, slip, lo, hi = struct.unpack_from("<QQiiii", data, 8)
        pool = accounts[1]
        if abs(self.active[pool] - active_id) > slip:
            raise Reverted(6004)
        if any(i // 70 in self.missing for i in (lo, hi)):
            raise Reverted(6009)
        p = w["positions"][accounts[0]]
        active = self.active[pool]
        below = [b for b in range(lo, hi + 1) if b <= active]
        above = [b for b in range(lo, hi + 1) if b > active]
        if amount_x and not above:
            raise Reverted(6002)  # Token cannot sit below the price.
        for b in below:
            p["shares"][b] = (amount_y // len(below)) << 64
        for b in above:
            p["shares"][b] = int(amount_x / len(above) * bin_price(b, 100)) << 64
        for account, amount in ((accounts[4], amount_y), (accounts[3], amount_x)):
            if amount and w["tokens"].get(account, 0) < amount:
                raise Reverted(9003)
            if amount:
                w["tokens"][account] -= amount

    def remove(self, w: dict[str, Any], accounts: list[str]) -> None:
        p = w["positions"][accounts[0]]
        active = self.active[accounts[1]]
        for b, shares in list(p["shares"].items()):
            worth = shares >> 64
            if b < active:
                w["tokens"][accounts[4]] += worth
            else:
                w["tokens"][accounts[3]] += int(worth / bin_price(b, 100))
            p["shares"][b] = 0
        if self.left_after_exit is not None:
            w["tokens"][accounts[3]] = self.left_after_exit

    left_after_exit: int | None = None

    def swap(self, w: dict[str, Any], accounts: list[str], data: bytes) -> None:
        amount_in, min_out = struct.unpack_from("<QQ", data, 8)
        price = bin_price(self.active[accounts[0]], 100)
        sells_token = accounts[4] != self.wsol
        out = int((amount_in * price if sells_token else amount_in / price) * (1 - self.bad_price) * 0.97)
        if out < min_out or out == 0:  # MEASURED: the program refuses an output that rounds to nothing.
            raise Reverted(6039)
        if w["tokens"].get(accounts[4], 0) < amount_in:
            raise Reverted(9003)
        w["tokens"][accounts[4]] -= amount_in
        w["tokens"][accounts[5]] += out

    def broadcast(self, gate: Any, raw: bytes) -> str:
        live.require_gate(gate)
        self.gate_seen.append(gate)
        if self.on_send:
            self.on_send()
        self.sent.append(raw)
        signature = base58.b58encode(raw[1:65]).decode()
        if signature not in self.statuses and self.landing != "drop":
            self.land(signature, raw)
        if self.raise_on_send:
            raise TimeoutError("rpc timed out")
        return signature

    def land(self, signature: str, raw: bytes) -> None:
        keys, _, sigs = parse(raw)
        before = self.world["lamports"], self.world["tokens"].get(self.wsol, 0)
        pre_tokens = self.token_rows(self.world)
        fee = 5_000 * sigs + 295_000
        err = {"InstructionError": [0, {"Custom": 6004}]} if self.landing == "revert" else None
        if not err:
            trial = copy.deepcopy(self.world)
            try:
                self.execute(trial, raw)
                self.world = trial
            except Reverted as exc:
                err = {"InstructionError": [0, {"Custom": exc.code}]}
        self.world["lamports"] -= fee
        self.statuses[signature] = {"slot": self.slot, "err": err, "confirmationStatus": "confirmed"}
        self.results[signature] = {
            "transaction": {"message": {"accountKeys": keys}},
            "meta": {"fee": fee, "preBalances": [before[0] if k == WALLET else 0 for k in keys],
                     "postBalances": [self.world["lamports"] if k == WALLET else 0 for k in keys],
                     "preTokenBalances": pre_tokens, "postTokenBalances": self.token_rows(self.world)}}

    def token_rows(self, world: dict[str, Any]) -> list[dict[str, Any]]:
        if self.wsol not in world["tokens"]:
            return []
        return [{"owner": WALLET, "mint": tx.NATIVE_MINT, "uiTokenAmount": {"amount": str(world["tokens"][self.wsol])}}]

    # ------------------------------------------------------------------------------------ helpers
    def kinds(self) -> list[list[str]]:
        return [[kind_of(ix) for ix in parse(raw)[1] if kind_of(ix)] for raw in self.sent]

    def token(self, pool: str = POOL_A) -> int | None:
        return self.world["tokens"].get(tx.ata_address(WALLET, MINTS[pool], tx.TOKEN_2022))


def pool_row(address: str) -> dict[str, Any]:
    return {"address": address, "name": "X-SOL", "token_y": {"address": tx.NATIVE_MINT},
            "token_x": {"address": MINTS[address], "freeze_authority_disabled": True}, "tvl": 90_000,
            "pool_config": {"bin_step": 100, "base_fee_pct": 2.0}, "dynamic_fee_pct": 1.0,
            "volume": {"30m": 20_000}, "fee_tvl_ratio": {"30m": 1.0}, "created_at": 0}


@pytest.fixture
def chain() -> Ledger:
    return Ledger()


@pytest.fixture
def key_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    path = tmp_path / "wallet.json"
    path.write_text(json.dumps(list(bytes(WALLET_KEY) + bytes(WALLET_KEY.verify_key))), encoding="utf-8")
    monkeypatch.setattr(live, "trading_wallet", lambda: KAIBA)
    return {live.LIVE_ENV: "1", live.KEY_ENV: str(path)}


ONE_AT_A_TIME = replace(live.Caps(), max_open=1)


def settings(**over: Any) -> live.Settings:
    """One position by default: the runner rightly keeps opening other pools, and most tests follow one."""
    return replace(live.Settings(wallet=WALLET, arm=ARM, profile=PROFILE, ticket_lamports=SOL // 2, caps=ONE_AT_A_TIME), **over)


def runner(chain: Ledger, env: dict[str, str] | None, tmp_path: Path, **over: Any) -> live.Runner:
    sender = live.LiveSender(chain, live.open_gate(WALLET, env)) if env else None
    return live.Runner(chain, live.Store(tmp_path / "lp"), settings(**over), sender=sender, now=chain.now,
                       keygen=chain.next_key)


def step(r: live.Runner, chain: Ledger, n: int = 1) -> None:
    for _ in range(n):
        chain.clock += 2.0
        chain.height += 5
        r.cycle()


def open_one(r: live.Runner, chain: Ledger) -> str:
    """One cycle: the chain scan passes and the first open goes out. It is not yet reconciled."""
    step(r, chain)
    return r.store.active()[0]["address"]


# ----------------------------------------------------------------------------------------- the gate
def test_gate_opens_when_everything_holds(key_env):
    gate = live.open_gate(WALLET, key_env)
    assert gate.wallet == WALLET
    signature = gate.sign(b"message")
    WALLET_KEY.verify_key.verify(b"message", signature)


def test_gate_needs_the_env_flag(key_env):
    for value in (None, "0", "true", ""):
        env = {k: v for k, v in key_env.items() if k != live.LIVE_ENV}
        if value is not None:
            env[live.LIVE_ENV] = value
        with pytest.raises(live.LiveRefused, match="live_env_not_set"):
            live.open_gate(WALLET, env)


def test_gate_needs_a_key_file(key_env):
    with pytest.raises(live.LiveRefused, match="key_file_not_set"):
        live.open_gate(WALLET, {live.LIVE_ENV: "1"})


def test_gate_refuses_a_key_for_another_wallet(key_env):
    with pytest.raises(live.LiveRefused, match="key_does_not_match_wallet"):
        live.open_gate(key(20), key_env)


def test_gate_refuses_kaibas_trading_wallet(key_env, tmp_path, monkeypatch):
    monkeypatch.setattr(live, "trading_wallet", lambda: WALLET)
    with pytest.raises(live.LiveRefused, match="wallet_is_kaibas"):
        live.open_gate(WALLET, key_env)


def test_kaibas_wallet_is_refused_before_the_key_is_read(key_env, monkeypatch):
    monkeypatch.setattr(live, "trading_wallet", lambda: WALLET)
    monkeypatch.setattr(live, "load_signer", lambda *a: pytest.fail("the key was read"))
    with pytest.raises(live.LiveRefused):
        live.open_gate(WALLET, key_env)


def test_gate_refuses_when_kaibas_wallet_is_unknown(key_env, monkeypatch):
    monkeypatch.setattr(live, "trading_wallet", lambda: None)
    with pytest.raises(live.LiveRefused, match="trading_wallet_unknown"):
        live.open_gate(WALLET, key_env)


def test_trading_wallet_comes_from_config_at_run_time(tmp_path, monkeypatch):
    path = tmp_path / "risk.yaml"
    path.write_text(f"chains:\n  sol:\n    wallet: {KAIBA}\n", encoding="utf-8")
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))
    assert live.trading_wallet() == KAIBA
    path.write_text("chains: [broken\n", encoding="utf-8")
    assert live.trading_wallet() is None


def test_trading_wallet_address_is_not_in_the_source():
    source = Path(live.__file__).read_text(encoding="utf-8")
    assert not re.search(r"(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{43,44}(?![A-Za-z0-9])", source)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_a_readable_key_file_is_refused_on_posix(key_env, monkeypatch, mode):
    monkeypatch.setattr(live, "POSIX", True)
    real = Path.stat

    def fake(self: Path, *a: Any, **k: Any) -> Any:
        got = real(self, *a, **k)
        return type("S", (), {"st_mode": mode, "st_size": got.st_size})()

    monkeypatch.setattr(Path, "stat", fake)
    with pytest.raises(live.LiveRefused, match="key_file_too_open"):
        live.open_gate(WALLET, key_env)


def test_a_private_key_file_is_accepted_on_posix(key_env, monkeypatch):
    monkeypatch.setattr(live, "POSIX", True)
    monkeypatch.setattr(Path, "stat", lambda self, *a, **k: type("S", (), {"st_mode": 0o600})())
    assert live.open_gate(WALLET, key_env).wallet == WALLET


def test_a_base58_key_file_works(tmp_path, key_env):
    path = tmp_path / "wallet.b58"
    path.write_text(base58.b58encode(bytes(WALLET_KEY) + bytes(WALLET_KEY.verify_key)).decode(), encoding="utf-8")
    assert live.open_gate(WALLET, {**key_env, live.KEY_ENV: str(path)}).wallet == WALLET


def test_a_bad_key_file_never_shows_its_contents(tmp_path, key_env):
    path = tmp_path / "wallet.txt"
    path.write_text("[1, 2, not-json-SECRETMARKER", encoding="utf-8")
    with pytest.raises(live.LiveRefused) as caught:
        live.open_gate(WALLET, {**key_env, live.KEY_ENV: str(path)})
    assert "SECRETMARKER" not in str(caught.value)
    assert caught.value.__suppress_context__ and caught.value.__cause__ is None


def test_a_missing_key_file_is_a_named_refusal(tmp_path, key_env):
    with pytest.raises(live.LiveRefused, match="key_file_unreadable"):
        live.open_gate(WALLET, {**key_env, live.KEY_ENV: str(tmp_path / "absent")})


def test_a_gate_cannot_be_forged():
    forged = live.Gate(wallet=WALLET, sign=lambda d: bytes(64), proof=object())
    with pytest.raises(live.LiveRefused, match="no_live_gate"):
        live.require_gate(forged)
    with pytest.raises(live.LiveRefused, match="no_live_gate"):
        live.LiveSender(Ledger(), None)


def test_the_gate_wallet_must_be_the_run_wallet(chain, key_env, tmp_path):
    sender = live.LiveSender(chain, live.open_gate(WALLET, key_env))
    with pytest.raises(live.LiveRefused, match="gate_wallet_mismatch"):
        live.Runner(chain, live.Store(tmp_path / "x"), settings(wallet=key(20)), sender=sender, now=chain.now)


def test_key_material_is_not_in_a_gates_repr(key_env):
    assert "sign" not in repr(live.open_gate(WALLET, key_env))


# -------------------------------------------------------------------------------------- no send path
def test_the_default_runner_cannot_send(chain, tmp_path):
    r = runner(chain, None, tmp_path)
    assert isinstance(r.sender, live.DrySender)
    with pytest.raises(live.LiveRefused, match="dry_mode_cannot_send"):
        r.sender.send(b"x")
    with pytest.raises(live.LiveRefused, match="dry_mode_cannot_send"):
        r.transmit("open", "t", "p", [], units=1, priority=0)


def test_dry_mode_simulates_and_records_nothing(chain, tmp_path):
    r = runner(chain, None, tmp_path)
    step(r, chain, 3)
    assert chain.calls["simulateTransaction"] >= 1
    assert r.counts["dry_open_ok"] >= 1
    assert chain.sent == []
    assert r.store.rows("SELECT * FROM sends") == []
    assert r.store.active() == []


class FakeHttp:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    def post(self, url: str, json: dict[str, Any]) -> Any:  # noqa: A002 - httpx's name
        self.posts.append(json)
        return type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: {"result": "sig"}})()


def transport() -> tuple[live.Transport, FakeHttp]:
    t = live.Transport("https://rpc.example")
    t.client = FakeHttp()
    return t, t.client


@pytest.mark.parametrize("method", ["sendTransaction", "sendRawTransaction", "requestAirdrop"])
def test_the_transport_refuses_anything_off_the_read_list(method):
    t, http = transport()
    with pytest.raises(live.LiveRefused, match="rpc_method_refused"):
        t.rpc(method, [])
    assert http.posts == []


def test_broadcast_without_a_gate_posts_nothing():
    t, http = transport()
    with pytest.raises(live.LiveRefused, match="no_live_gate"):
        t.broadcast(None, b"x")
    assert http.posts == []


def test_broadcast_with_a_gate_posts_the_bytes(key_env):
    t, http = transport()
    t.broadcast(live.open_gate(WALLET, key_env), b"xyz")
    assert http.posts[0]["method"] == "sendTransaction"
    assert http.posts[0]["params"][0] == base64.b64encode(b"xyz").decode()


def test_send_appears_once_in_the_source_and_the_executor_nowhere():
    source = Path(live.__file__).read_text(encoding="utf-8")
    code = re.sub(r'"""[\s\S]*?"""', "", source)
    assert len(re.findall(r'"sendTransaction"', code)) == 1
    assert "sendRawTransaction" not in code
    for name in ("execution.executor", "gmgn", "kaiba.db"):
        assert name not in code
    pure = Path(tx.__file__).read_text(encoding="utf-8")
    assert not re.search(r"import (httpx|requests|socket|nacl|sqlite3|os)\b", pure)
    assert "sendTransaction" not in pure


def test_importing_the_module_pulls_in_no_executor_or_gmgn():
    code = ("import sys; import kaiba.execution.lp_live;"
            "print([m for m in sys.modules if m == 'kaiba.execution.executor' or 'gmgn' in m])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120)
    assert out.stdout.strip() == "[]"


def test_the_rpc_guard_is_paper_s_guard():
    from kaiba.execution import lp_paper

    assert live.rpc_url is lp_paper.rpc_url


class Started(Exception):
    pass


def launch(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> Any:
    """`main()` up to the point the runner would start, with what it would start."""
    seen: dict[str, Any] = {}

    def run(self: live.Runner, seconds: float | None = None) -> None:
        seen["runner"] = self
        raise Started

    monkeypatch.setattr(live.Runner, "run", run)
    monkeypatch.setattr(live, "rpc_url", lambda: "https://rpc.example")
    monkeypatch.setattr(sys, "argv", ["lp_live", *argv])
    with pytest.raises(Started):
        live.main()
    return seen["runner"]


def test_the_default_run_is_dry(monkeypatch, tmp_path):
    for name in (live.LIVE_ENV, live.KEY_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(live.DIR_ENV, str(tmp_path / "lp"))
    r = launch(monkeypatch, ["run", "--wallet", WALLET])
    assert isinstance(r.sender, live.DrySender)
    assert r.s.arm.name == FAST.primary


def test_live_needs_nothing_but_the_gate_and_starts_live_when_it_passes(monkeypatch, tmp_path, key_env):
    for name, value in key_env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(live.DIR_ENV, str(tmp_path / "lp"))
    r = launch(monkeypatch, ["run", "--wallet", WALLET])
    assert isinstance(r.sender, live.LiveSender)


def test_a_failed_gate_refuses_to_start_and_never_falls_back_to_dry(monkeypatch, tmp_path):
    monkeypatch.setenv(live.LIVE_ENV, "1")
    monkeypatch.delenv(live.KEY_ENV, raising=False)
    monkeypatch.setenv(live.DIR_ENV, str(tmp_path / "lp"))
    monkeypatch.setattr(live, "rpc_url", lambda: "https://rpc.example")
    monkeypatch.setattr(live.Runner, "run", lambda *a, **k: pytest.fail("the run started without its gate"))
    monkeypatch.setattr(sys, "argv", ["lp_live", "run", "--wallet", WALLET])
    with pytest.raises(SystemExit, match="key_file_not_set"):
        live.main()


def test_a_shared_rpc_host_refuses_to_start(monkeypatch, tmp_path):
    monkeypatch.delenv(live.LIVE_ENV, raising=False)
    monkeypatch.setenv(live.DIR_ENV, str(tmp_path / "lp"))
    monkeypatch.setenv("KAIBA_LP_RPC_URL", "https://protection.example/rpc")
    monkeypatch.setattr(live, "rpc_url", lambda: lp_paper_guard(["protection.example"]))
    monkeypatch.setattr(live.Runner, "run", lambda *a, **k: pytest.fail("the run started on a shared host"))
    monkeypatch.setattr(sys, "argv", ["lp_live", "run", "--wallet", WALLET])
    with pytest.raises(SystemExit, match="lp_rpc_host_shared_with_protection"):
        live.main()


def lp_paper_guard(hosts: list[str]) -> str:
    from kaiba.execution.lp_paper import rpc_url

    return rpc_url(hosts)


def test_no_wallet_refuses_to_start(monkeypatch):
    monkeypatch.delenv(live.WALLET_ENV, raising=False)
    monkeypatch.setattr(sys, "argv", ["lp_live", "run"])
    with pytest.raises(SystemExit, match="lp_wallet_missing"):
        live.main()


def test_the_store_lives_under_labs_and_never_touches_the_main_database(chain, tmp_path, monkeypatch):
    monkeypatch.delenv(live.DIR_ENV, raising=False)
    assert live.data_root() == Path("data/labs/lp_live")
    r = runner(chain, None, tmp_path)
    assert Path(r.store.db.execute("PRAGMA database_list").fetchone()[2]).parent == tmp_path / "lp"


# ------------------------------------------------------------------------------------- a clean trip
def test_a_whole_trip_with_a_real_result(chain, key_env, tmp_path):
    chain.listed = [POOL_A]
    r = runner(chain, key_env, tmp_path, profile=replace(PROFILE, cooldown_s=10_000.0))
    start = chain.world["lamports"]
    address = open_one(r, chain)
    assert r.store.position(address)["status"] == "opening"
    assert address in chain.world["positions"]
    step(r, chain)
    assert r.store.position(address)["status"] == "open"
    chain.active[POOL_A] = -520  # Price falls through the range.
    step(r, chain, 6)
    row = r.store.position(address)
    assert row["status"] == "done"
    assert row["exit_reason"] == "below_range"
    assert chain.world["positions"] == {}
    assert chain.token() is None  # Token sold and its account closed.
    trip = r.store.rows("SELECT * FROM trips")[0]
    assert trip["net_lamports"] == chain.world["lamports"] - start
    assert trip["net_lamports"] < 0
    assert chain.kinds()[0] == ["initialize_position", "add_liquidity_by_strategy2"]
    assert chain.kinds()[1] == ["remove_liquidity_by_range2", "claim_fee2", "close_position_if_empty"]
    assert chain.kinds()[2] == ["swap2"]


def test_a_take_profit_exit_needs_no_swap(chain, key_env, tmp_path):
    r = runner(chain, key_env, tmp_path)
    address = open_one(r, chain)
    step(r, chain)
    chain.clock += 200  # Past max_hold, price unchanged, all still SOL.
    step(r, chain, 4)
    assert r.store.position(address)["exit_reason"] == "max_hold"
    assert r.store.position(address)["status"] == "done"
    assert not any("swap2" in k for k in chain.kinds())


def test_the_position_is_opened_where_the_plan_says(chain, key_env, tmp_path):
    r = runner(chain, key_env, tmp_path)
    address = open_one(r, chain)
    held = chain.world["positions"][address]
    assert (held["lo"], held["hi"]) == (-504, -501)
    assert sum(s >> 64 for s in held["shares"].values()) == pytest.approx(SOL // 2, abs=4)


def test_marks_match_the_chains_own_arithmetic(chain):
    """A real position through `stakes_from_chain` and `model_view` against `dlmm_state.snapshot`."""
    chain.growth = 3.0
    pool, index = POOL_A, -8
    world = {"positions": {key(30): {"pool": pool, "lo": -504, "hi": -501, "shares": {b: 7 * 10**12 << 64 for b in range(-504, -500)}}}}
    position = live.decode_position(chain.position_account(key(30), world))
    res = chain.bin_account(pool, index)
    arr = live.decode_bins(res)
    state = PoolState(slot=1, at_ms=0, active=chain.active[pool], bin_step=100, bins={b["id"]: b for b in arr["bins"]})
    plan = Plan(arm=ARM, ticket=SOL, lo=-504, hi=-501, stakes=live.stakes_from_chain(position, state))
    fee_x, fee_y = fees(plan, live.model_view(state))
    assert fee_x == fee_y == 4 * ((7 * 10**12 * (3 * 2**64)) >> 64)
    with_dilution = fees(replace(plan, last_fees={}), state)[0]
    assert with_dilution < fee_x  # The trap `model_view` removes.
    x, y = holdings(plan, state)
    assert x == 0
    assert y == pytest.approx(4 * 7 * 10**12, rel=1e-9)


def test_marks_agree_with_the_capture_decoder():
    """Raw accounts through both routes: the independent decoder and the mark `exit_reason` sees."""
    receipt = capture(pending=0)
    got = snapshot(receipt)
    values = receipt["response"]["result"]["value"]
    position = decode_position(values[1])
    bins = {b["id"]: b for account in values[2:] for b in decode_bins(account)["bins"]}
    state = PoolState(slot=1, at_ms=0, active=got["pool"]["active_bin"], bin_step=got["pool"]["bin_step"], bins=bins)
    plan = Plan(arm=ARM, ticket=1, lo=-1, hi=0, stakes=live.stakes_from_chain(position, state))
    assert list(fees(plan, live.model_view(state))) == got["fee_atoms_gross"]
    assert got["fee_atoms_gross"][0] > 0
    x, y = holdings(plan, live.model_view(state))
    assert x == pytest.approx(got["principal_atoms"][0], abs=1.01)  # The decoder floors; a mark does not.
    assert y == pytest.approx(got["principal_atoms"][1], abs=1.01)


def test_a_partial_read_never_becomes_a_mark():
    position = {"bins": [{"id": 5, "shares": 9, "fee_checkpoint": [0, 0]}]}
    with pytest.raises(ValueError, match="position_bin_unread"):
        live.stakes_from_chain(position, PoolState(slot=1, at_ms=0, active=0, bin_step=100, bins={}))


def test_tx_delta_counts_native_and_wrapped_sol():
    result = {"transaction": {"message": {"accountKeys": [key(30), WALLET]}},
              "meta": {"fee": 9, "preBalances": [0, 100], "postBalances": [0, 70],
                       "preTokenBalances": [{"owner": WALLET, "mint": tx.NATIVE_MINT, "uiTokenAmount": {"amount": "5"}},
                                            {"owner": key(30), "mint": tx.NATIVE_MINT, "uiTokenAmount": {"amount": "999"}}],
                       "postTokenBalances": [{"owner": WALLET, "mint": tx.NATIVE_MINT, "uiTokenAmount": {"amount": "45"}},
                                             {"owner": WALLET, "mint": key(31), "uiTokenAmount": {"amount": "777"}}]}}
    assert live.tx_delta(result, WALLET) == (10, 9)


def test_live_range_covers_only_what_holds_something():
    bins = [{"id": i, "shares": 0, "fee_pending": [0, 0]} for i in range(5)]
    assert live.live_range({"bins": bins}) == (None, None, False)
    bins[1]["fee_pending"] = [3, 0]
    bins[3]["shares"] = 8
    assert live.live_range({"bins": bins}) == (1, 3, True)
    bins[3]["shares"] = 0
    assert live.live_range({"bins": bins}) == (1, 1, False)


def test_sweep_quote_sells_what_the_book_can_fill():
    rows = {b: {"amounts": [0, 1_000], "fee_growth": [0, 0], "supply": 1} for b in range(-3, 1)}
    state = PoolState(slot=1, at_ms=0, active=0, bin_step=100, bins=rows)
    amount, out = live.sweep_quote(10**9, state, 0.0, 100)
    assert 0 < amount < 10**9
    assert out == int(sum(1_000 for _ in rows) * 0.99) or out < 4_000
    empty = replace(state, bins={})
    assert live.sweep_quote(10, empty, 0.0, 100) == (0, 0)


# ---------------------------------------------------------------------------------- ambiguous sends
def test_the_record_is_committed_before_the_first_broadcast(chain, key_env, tmp_path):
    r = runner(chain, key_env, tmp_path)
    seen = []

    def check() -> None:
        other = sqlite3.connect(tmp_path / "lp" / "lp_live.db")
        seen.append(other.execute("SELECT status, kind FROM sends").fetchall())
        seen.append(other.execute("SELECT status FROM positions").fetchall())
        other.close()

    chain.on_send = check
    step(r, chain, 2)
    assert seen[0] == [("signed", "open")]
    assert seen[1] == [("opening",)]


def test_a_broadcast_that_raised_is_ambiguous_not_failed(chain, key_env, tmp_path):
    chain.raise_on_send = True
    r = runner(chain, key_env, tmp_path)
    address = open_one(r, chain)
    send = r.store.rows("SELECT * FROM sends")[0]
    assert send["status"] == "ambiguous"
    assert r.store.position(address)["status"] == "opening"
    chain.raise_on_send = False
    step(r, chain)
    assert r.store.position(address)["status"] == "open"  # It had landed.
    assert len({raw[1:65] for raw in chain.sent}) == 1


def test_an_ambiguous_open_never_builds_a_second_transaction(chain, key_env, tmp_path):
    chain.raise_on_send = True
    chain.landing = "drop"
    r = runner(chain, key_env, tmp_path)
    address = open_one(r, chain)
    last_valid = r.store.rows("SELECT last_valid FROM sends")[0]["last_valid"]
    while chain.height + 10 <= last_valid + live.EXPIRY_MARGIN_BLOCKS:
        step(r, chain)
        assert r.store.position(address)["status"] == "opening"
    assert len({raw for raw in chain.sent}) == 1  # Same bytes, every time.
    assert len(chain.sent) > 1  # And it kept trying to get them through.
    assert len(r.store.rows("SELECT * FROM sends")) == 1
    assert chain.world["positions"] == {}


def test_only_a_height_past_validity_settles_a_silent_send(chain, key_env, tmp_path):
    chain.landing = "drop"
    r = runner(chain, key_env, tmp_path)
    address = open_one(r, chain)
    send = r.store.rows("SELECT * FROM sends")[0]
    chain.height = send["last_valid"] + live.EXPIRY_MARGIN_BLOCKS
    r.reconcile(send)
    assert r.store.rows("SELECT status FROM sends")[0]["status"] == "sent"
    chain.height += 1
    r.reconcile(send)
    assert r.store.rows("SELECT status FROM sends")[0]["status"] == "expired"
    assert r.store.position(address)["status"] == "failed"
    assert r.store.rows("SELECT status FROM trips")[0]["status"] == "failed"


def test_an_expired_open_whose_position_exists_is_an_open(chain, key_env, tmp_path):
    chain.landing = "drop"
    r = runner(chain, key_env, tmp_path)
    address = open_one(r, chain)
    chain.world["positions"][address] = {"pool": POOL_A, "lo": -504, "hi": -501, "shares": {-504: 1 << 64}}
    send = r.store.rows("SELECT * FROM sends")[0]
    chain.height = send["last_valid"] + live.EXPIRY_MARGIN_BLOCKS + 1
    r.reconcile(send)
    assert r.store.position(address)["status"] == "open"
    assert r.store.rows("SELECT status FROM sends")[0]["status"] == "landed"


def test_a_reverted_open_is_a_paid_miss_and_frees_the_slot(chain, key_env, tmp_path):
    chain.landing = "revert"
    r = runner(chain, key_env, tmp_path)
    address = open_one(r, chain)
    step(r, chain)
    assert r.store.position(address)["status"] == "failed"
    trip = r.store.rows("SELECT * FROM trips")[0]
    assert trip["status"] == "failed"
    assert trip["net_lamports"] == -(10_000 + 295_000)


def test_an_ambiguous_exit_is_not_rebuilt(chain, key_env, tmp_path):
    r = runner(chain, key_env, tmp_path)
    address = open_one(r, chain)
    step(r, chain)
    chain.landing, chain.raise_on_send = "drop", True
    chain.active[POOL_A] = -520
    step(r, chain)
    exits = r.store.rows("SELECT * FROM sends WHERE kind='exit'")
    assert [e["status"] for e in exits] == ["ambiguous"]
    step(r, chain, 5)
    assert len(r.store.rows("SELECT * FROM sends WHERE kind='exit'")) == 1
    assert r.counts["waiting_on_send"] >= 1
    assert address in chain.world["positions"]


def test_a_landed_open_with_no_position_is_held_then_cleaned_up(chain, key_env, tmp_path):
    r = runner(chain, key_env, tmp_path)
    address = open_one(r, chain)
    del chain.world["positions"][address]
    step(r, chain)
    row = r.store.position(address)
    assert row["status"] == "opening" and row["note"] == "landed_position_unread"
    step(r, chain, 3)
    assert r.store.position(address)["status"] == "opening"  # A lagging node: not assumed either way.
    assert r.may_open(counting_slots=False) is None
    assert r.may_open() == "max_open"  # And it keeps its slot meanwhile.
    chain.clock += live.OPEN_UNREAD_S + 1
    step(r, chain, 3)
    assert r.store.position(address)["status"] == "done"
    assert r.store.position(address)["note"] == "landed_never_readable"


def test_an_expired_exit_is_retried_on_fresh_state_at_a_higher_priority(chain, key_env, tmp_path):
    r = runner(chain, key_env, tmp_path)
    address = open_one(r, chain)
    step(r, chain)
    chain.landing = "drop"
    chain.active[POOL_A] = -520
    step(r, chain)
    first = r.store.rows("SELECT * FROM sends WHERE kind='exit'")[0]
    chain.height = first["last_valid"] + live.EXPIRY_MARGIN_BLOCKS + 1
    chain.landing = "land"
    step(r, chain, 2)
    exits = r.store.rows("SELECT * FROM sends WHERE kind='exit' ORDER BY created_ms")
    assert len(exits) == 2 and exits[1]["priority"] > exits[0]["priority"]
    assert address not in chain.world["positions"]


def test_a_landed_exit_that_left_the_position_open_is_retried(chain, key_env, tmp_path):
    r = runner(chain, key_env, tmp_path)
    address = open_one(r, chain)
    step(r, chain)
    chain.active[POOL_A] = -520
    real = chain.run_dlmm

    def stubborn(w: dict[str, Any], name: str, accounts: list[str], data: bytes) -> None:
        if name != "close_position_if_empty":
            real(w, name, accounts, data)

    chain.run_dlmm = stubborn
    step(r, chain, 2)
    assert r.store.position(address)["status"] == "exiting"
    chain.run_dlmm = real
    step(r, chain, 2)
    assert address not in chain.world["positions"]


# ---------------------------------------------------------------------------------- crash and restart
def test_a_crash_between_record_and_broadcast_cannot_open_twice(chain, key_env, tmp_path):
    r = runner(chain, key_env, tmp_path)

    def die() -> None:
        raise KeyboardInterrupt

    chain.on_send = die
    with pytest.raises(KeyboardInterrupt):
        step(r, chain, 2)
    chain.on_send = None
    assert chain.sent == []
    again = runner(chain, key_env, tmp_path)
    row = again.store.active()[0]
    assert row["status"] == "opening"
    step(again, chain, 2)
    assert len(again.store.rows("SELECT * FROM sends WHERE kind='open'")) == 1
    assert len(again.store.active()) == 1  # The same one, waiting: not a second position.


def test_a_restart_keeps_managing_what_the_store_holds(chain, key_env, tmp_path):
    r = runner(chain, key_env, tmp_path)
    address = open_one(r, chain)
    step(r, chain)
    chain.active[POOL_A] = -520
    again = runner(chain, key_env, tmp_path)
    assert again.chain_scan_ok is False
    step(again, chain, 5)
    assert again.store.position(address)["status"] == "done"
    assert address not in chain.world["positions"]


def test_a_restart_keeps_the_original_hold_clock(chain, key_env, tmp_path):
    r = runner(chain, key_env, tmp_path)
    address = open_one(r, chain)
    step(r, chain)
    chain.clock += 90
    again = runner(chain, key_env, tmp_path)
    step(again, chain, 8)
    assert again.store.position(address)["exit_reason"] == "max_hold"


def test_a_position_only_the_chain_knows_is_adopted_and_exited(chain, key_env, tmp_path):
    orphan = key(40)
    chain.world["positions"][orphan] = {"pool": POOL_A, "lo": -504, "hi": -501, "shares": {b: 10**8 << 64 for b in range(-504, -500)}}
    r = runner(chain, key_env, tmp_path)
    step(r, chain, 4)
    row = r.store.position(orphan)
    assert row["adopted"] == 1
    assert row["exit_reason"] == "adopted"
    assert orphan not in chain.world["positions"]


def expire_open(r, chain):
    send = r.store.rows("SELECT * FROM sends")[0]
    chain.height = send["last_valid"] + live.EXPIRY_MARGIN_BLOCKS + 1
    r.reconcile(send)


def test_an_open_written_off_that_exists_after_all_is_found_and_exited(chain, key_env, tmp_path):
    chain.landing = "drop"
    r = runner(chain, key_env, tmp_path)
    address = open_one(r, chain)
    expire_open(r, chain)
    assert r.store.position(address)["status"] == "failed"
    chain.world["positions"][address] = {"pool": POOL_A, "lo": -504, "hi": -501, "shares": {b: 10**8 << 64 for b in range(-504, -500)}}
    r.scan_chain()
    row = r.store.position(address)
    assert (row["status"], row["adopted"], row["note"]) == ("open", 1, "revived_from_chain")
    assert r.store.rows("SELECT status, adopted, finished_ms FROM trips") == [{"status": "running", "adopted": 1, "finished_ms": None}]
    chain.landing = "land"
    step(r, chain, 4)
    assert address not in chain.world["positions"]


def test_an_open_that_never_happened_stays_written_off(chain, key_env, tmp_path):
    chain.landing = "drop"
    r = runner(chain, key_env, tmp_path)
    address = open_one(r, chain)
    expire_open(r, chain)
    r.revive(r.store.position(address))
    assert r.store.position(address)["status"] == "failed"
    assert r.store.rows("SELECT status FROM trips") == [{"status": "failed"}]


def test_an_adopted_trip_is_left_out_of_the_days_result(chain, key_env, tmp_path):
    orphan = key(40)
    chain.world["positions"][orphan] = {"pool": POOL_A, "lo": -504, "hi": -501, "shares": {b: 10**8 << 64 for b in range(-504, -500)}}
    r = runner(chain, key_env, tmp_path)
    step(r, chain, 4)
    assert r.store.realized_loss_since(0) == (0, True)


def test_nothing_opens_until_the_chain_scan_has_succeeded(chain, key_env, tmp_path):
    chain.fail_reads.add("getProgramAccounts")
    r = runner(chain, key_env, tmp_path)
    step(r, chain, 3)
    assert chain.sent == []
    assert r.may_open() == "chain_scan_pending"
    assert r.counts["scan_failed"] >= 1
    chain.fail_reads.clear()
    r.last_scan = float("-inf")
    step(r, chain, 2)
    assert len(chain.sent) >= 1


def test_a_position_closed_behind_the_bots_back_is_still_swept(chain, key_env, tmp_path):
    r = runner(chain, key_env, tmp_path)
    address = open_one(r, chain)
    step(r, chain)
    del chain.world["positions"][address]
    step(r, chain, 3)
    assert r.store.position(address)["exit_reason"] == "vanished"
    assert r.store.position(address)["status"] == "done"


# ------------------------------------------------------------------------------------ the exit path
def moved(chain: Ledger, key_env: dict[str, str], tmp_path: Path, **over: Any) -> tuple[live.Runner, str]:
    r = runner(chain, key_env, tmp_path, **over)
    address = open_one(r, chain)
    step(r, chain)
    chain.active[POOL_A] = -520
    return r, address


def test_an_exit_lands_when_the_pool_has_moved_far_past_the_range(chain, key_env, tmp_path):
    r, address = moved(chain, key_env, tmp_path)
    chain.active[POOL_A] = -640  # Several arrays away from the position's own.
    step(r, chain, 2)
    assert address not in chain.world["positions"]
    assert (chain.token() or 0) > 0 or chain.token() is None


def test_a_failed_swap_keeps_the_token_and_widens_within_the_bound(chain, key_env, tmp_path):
    r, address = moved(chain, key_env, tmp_path)
    chain.bad_price = 0.99  # No minimum on the ladder can be met.
    step(r, chain, 2)
    assert address not in chain.world["positions"]
    held = chain.token()
    assert held and held > 0
    rungs = []
    for _ in range(9):
        before = r.store.position(address)["sweep_tries"]
        chain.clock += 400
        step(r, chain)
        if r.store.position(address)["sweep_tries"] > before:
            rungs.append(before)
    row = r.store.position(address)
    assert row["status"] == "sweeping"
    assert row["sweep_tries"] >= 4
    assert chain.token() == held  # Kept, never written off.
    assert live.status(r.store)["stuck_tokens"][0]["mint"] == MINTS[POOL_A]
    assert r.counts["sweep_stuck"] >= 1
    assert r.store.rows("SELECT * FROM sends WHERE kind='sweep'") == []  # A sale that cannot fill costs nothing.


def test_the_minimum_widens_rung_by_rung_and_stops_at_the_last(chain, key_env, tmp_path, monkeypatch):
    r, address = moved(chain, key_env, tmp_path)
    step(r, chain, 1)
    chain.bad_price = 0.99
    used: list[int] = []
    real = live.sweep_quote

    def spy(balance: int, state: PoolState, rate: float, slip: int) -> tuple[int, int]:
        used.append(slip)
        return real(balance, state, rate, slip)

    monkeypatch.setattr(live, "sweep_quote", spy)
    for _ in range(7):
        chain.clock += 400
        step(r, chain)
    ladder = settings().sweep_slip_bps
    assert used[:4] == list(ladder)
    assert set(used[4:]) == {ladder[-1]}


def test_a_sale_is_quoted_at_the_chains_fee_not_a_figure_from_selection(chain, key_env, tmp_path, monkeypatch):
    """A pool's fee moves while a position is held. A stale figure sets a minimum the pool cannot pay."""
    r, address = moved(chain, key_env, tmp_path)
    step(r, chain, 1)
    chain.base_factor = 60_000  # 6% now, double what it was at selection.
    rates: list[float] = []
    real = live.sweep_quote

    def spy(balance: int, state: PoolState, rate: float, slip: int) -> tuple[int, int]:
        rates.append(rate)
        return real(balance, state, rate, slip)

    monkeypatch.setattr(live, "sweep_quote", spy)
    chain.clock += 400
    step(r, chain)
    assert rates == [pytest.approx(0.06)]


def test_unsellable_dust_is_kept_recorded_and_frees_the_slot(chain, key_env, tmp_path):
    r, address = moved(chain, key_env, tmp_path)
    chain.left_after_exit = 3  # About 0.02 lamports' worth at this price.
    step(r, chain, 2)
    row = r.store.position(address)
    assert row["status"] == "done" and row["note"] == "dust_left" and row["token_left"] == 3
    assert chain.token() == 3  # Kept. Nothing burned it.
    assert r.store.rows("SELECT finished_ms FROM trips")[0]["finished_ms"] is not None
    assert live.status(r.store)["dust_left"] == [{"mint": MINTS[POOL_A], "atoms": 3}]
    assert live.status(r.store)["stuck_tokens"] == []


def test_dust_does_not_block_the_next_position(chain, key_env, tmp_path):
    chain.listed = [POOL_A, POOL_B]
    # Swapping the leg's token for dust also books it as a loss, which is not what this test is about.
    r, address = moved(chain, key_env, tmp_path, caps=replace(ONE_AT_A_TIME, max_daily_loss_lamports=5 * SOL))
    chain.left_after_exit = 3
    step(r, chain, 4)
    assert r.store.position(address)["status"] == "done"
    assert len(r.store.rows("SELECT * FROM positions")) >= 2  # The slot went to a new position.


def test_a_small_balance_that_can_still_sell_is_sold(chain, key_env, tmp_path):
    r, address = moved(chain, key_env, tmp_path)
    chain.left_after_exit = 1_000  # About 6.7 lamports' worth: small, but it clears.
    step(r, chain, 3)
    assert chain.token() is None  # Sold, and the account closed for its rent.
    assert r.store.position(address)["note"] != "dust_left"


def book(bins: dict[int, int], active: int = 0) -> PoolState:
    rows = {b: {"amounts": [0, y], "fee_growth": [0, 0], "supply": 1} for b, y in bins.items()}
    return PoolState(slot=1, at_ms=0, active=active, bin_step=100, bins=rows)


def test_dust_is_what_the_book_would_take_and_pay_nothing_for():
    assert live.is_dust(1, book({0: 10**9}), 0.0)  # Fills whole, pays 1 lamport: under the floor.
    assert not live.is_dust(2, book({0: 10**9}), 0.0)  # Two lamports clears.


def test_an_empty_book_is_not_dust():
    """It also pays nothing, but it can recover: writing this off would strand real token."""
    assert not live.is_dust(10**12, book({}), 0.0)
    assert not live.is_dust(10**12, book({0: 0, -1: 0}), 0.0)


def test_an_empty_book_keeps_retrying(chain, key_env, tmp_path, monkeypatch):
    r, address = moved(chain, key_env, tmp_path)
    monkeypatch.setattr(live, "sweep_quote", lambda *a: (0, 0))
    monkeypatch.setattr(live, "is_dust", lambda *a: False)
    step(r, chain, 3)
    row = r.store.position(address)
    assert row["status"] == "sweeping" and row["note"] == "sweep_no_bid_in_pool"
    assert row["sweep_tries"] >= 1


def test_a_wider_minimum_clears_a_bad_print(chain, key_env, tmp_path):
    r, address = moved(chain, key_env, tmp_path)
    chain.bad_price = 0.05  # Beyond the first two rungs, inside the third.
    for _ in range(8):
        chain.clock += 400
        step(r, chain)
    assert r.store.position(address)["status"] == "done"
    assert chain.token() is None


def test_a_swap_that_lands_reverted_is_retried(chain, key_env, tmp_path):
    r, address = moved(chain, key_env, tmp_path)
    step(r, chain, 1)  # The exit lands; the swap is next.
    chain.landing = "revert"
    chain.clock += 400
    step(r, chain, 2)
    assert r.store.position(address)["sweep_tries"] >= 1
    chain.landing = "land"
    for _ in range(4):
        chain.clock += 400
        step(r, chain)
    assert r.store.position(address)["status"] == "done"


def test_an_exit_goes_out_when_the_simulator_is_down(chain, key_env, tmp_path):
    r = runner(chain, key_env, tmp_path)
    address = open_one(r, chain)
    step(r, chain)
    chain.active[POOL_A] = -520
    chain.fail_reads.add("simulateTransaction")
    step(r, chain)
    assert address not in chain.world["positions"]
    assert r.counts["exit_unsimulated"] == 1


def test_a_known_bad_exit_is_not_sent(chain, key_env, tmp_path):
    r = runner(chain, key_env, tmp_path)
    address = open_one(r, chain)
    step(r, chain)
    chain.active[POOL_A] = -520
    chain.sim_error = {"InstructionError": [3, {"Custom": 6055}]}
    sent = len(chain.sent)
    step(r, chain, 2)
    assert len(chain.sent) == sent
    assert r.store.position(address)["exit_tries"] >= 1
    chain.sim_error = None
    step(r, chain, 2)
    assert address not in chain.world["positions"]


def test_a_failed_pool_listing_does_not_stop_an_exit(chain, key_env, tmp_path):
    r = runner(chain, key_env, tmp_path)
    address = open_one(r, chain)
    step(r, chain)
    chain.active[POOL_A] = -520
    chain.api = lambda path: (_ for _ in ()).throw(RuntimeError("api down"))
    step(r, chain, 3)
    assert address not in chain.world["positions"]


def test_a_position_in_the_store_does_not_expire_on_a_read_error(chain, key_env, tmp_path):
    r = runner(chain, key_env, tmp_path)
    address = open_one(r, chain)
    step(r, chain)
    chain.fail_reads.add("getMultipleAccounts")
    step(r, chain, 3)
    assert r.store.position(address)["status"] == "open"
    assert r.counts["manage_failed"] >= 1


# ---------------------------------------------------------------------------------------- the caps
def test_max_open_stops_the_third_and_only_the_third(chain, key_env, tmp_path):
    r = runner(chain, key_env, tmp_path, caps=live.Caps())
    step(r, chain, 10)
    assert len(r.store.active()) == live.Caps().max_open
    assert len({row["pool"] for row in r.store.active()}) == 2
    assert r.may_open() == "max_open"
    assert len(chain.sent) == 2


def test_a_ticket_above_the_cap_will_not_start(chain, tmp_path):
    with pytest.raises(live.LiveRefused, match="ticket_above_cap"):
        live.Runner(chain, live.Store(tmp_path / "x"), settings(ticket_lamports=live.Caps().ticket_lamports + 1), now=chain.now)
    live.Runner(chain, live.Store(tmp_path / "y"), settings(ticket_lamports=live.Caps().ticket_lamports), now=chain.now)


def spend(r: live.Runner, chain: Ledger, *, priority: int = 0, delta: int | None = 0, trip: str = "t1", status: str = "landed") -> None:
    at = int(chain.clock * 1000)
    if trip != "none":
        r.store.insert("trips", id=trip, pool=key(50), arm="t", status="done", started_ms=at, finished_ms=at, net_lamports=delta)
    r.store.insert("sends", signature=f"s{trip}{priority}", position="p", trip=trip, kind="open", status=status, raw="", last_valid=1,
                   priority=priority, created_ms=at, delta=delta, fee=priority)
    r.store.db.commit()


def ready(chain: Ledger, env: dict[str, str], tmp_path: Path, **over: Any) -> live.Runner:
    r = runner(chain, env, tmp_path, **over)
    chain.fail_reads.add("__none__")
    r.scan_chain()
    assert r.may_open() is None
    return r


def test_priority_cap_stops_opening(chain, key_env, tmp_path):
    r = ready(chain, key_env, tmp_path)
    cap = r.s.caps.max_priority_lamports_day
    spend(r, chain, priority=cap - r.s.priority_lamports)
    assert r.may_open() is None
    spend(r, chain, priority=1, trip="t2")
    assert r.may_open() == "priority_cap"


def test_daily_loss_stops_opening(chain, key_env, tmp_path):
    r = ready(chain, key_env, tmp_path)
    cap = r.s.caps.max_daily_loss_lamports
    spend(r, chain, delta=-(cap - 1))
    assert r.may_open() is None
    spend(r, chain, delta=-1, trip="t2")
    assert r.may_open() == "daily_loss_stop"


def test_a_gain_offsets_the_days_loss(chain, key_env, tmp_path):
    r = ready(chain, key_env, tmp_path)
    cap = r.s.caps.max_daily_loss_lamports
    spend(r, chain, delta=-cap)
    spend(r, chain, delta=1, trip="t2")
    assert r.may_open() is None


def test_yesterdays_loss_does_not_count(chain, key_env, tmp_path):
    r = ready(chain, key_env, tmp_path)
    cap = r.s.caps.max_daily_loss_lamports
    spend(r, chain, delta=-2 * cap)
    assert r.may_open() == "daily_loss_stop"
    chain.clock += 86_400
    assert r.may_open() is None


def test_a_trip_whose_cost_is_unknown_stops_opening(chain, key_env, tmp_path):
    r = ready(chain, key_env, tmp_path)
    spend(r, chain, delta=None)
    assert r.may_open() == "pnl_unknown"


def test_unsold_token_counts_as_worth_nothing(chain, key_env, tmp_path):
    r, address = moved(chain, key_env, tmp_path)
    chain.bad_price = 0.99
    step(r, chain, 3)
    row = r.store.position(address)
    assert row["status"] == "sweeping"
    r.store.update("positions", "address", address, next_sweep_ms=int(chain.clock * 1000 + 1e7))
    r.settle_deltas()
    loss, known = r.store.realized_loss_since(r.day_start_ms())
    assert known
    assert loss > 0


def test_wallet_floor_stops_opening(chain, key_env, tmp_path):
    r = ready(chain, key_env, tmp_path)
    need = r.s.ticket_lamports + live.OPEN_FLOAT_LAMPORTS + r.s.caps.wallet_floor_lamports
    chain.world["lamports"] = need
    assert r.may_open() is None
    chain.world["lamports"] = need - 1
    assert r.may_open() == "wallet_floor"


def test_an_unresolved_send_stops_opening(chain, key_env, tmp_path):
    chain.landing = "drop"
    r = runner(chain, key_env, tmp_path)
    open_one(r, chain)
    assert r.may_open() == "send_unresolved"
    step(r, chain, 3)
    assert len(r.store.rows("SELECT * FROM sends")) == 1


def test_an_unknown_balance_refuses(chain, key_env, tmp_path):
    r = ready(chain, key_env, tmp_path)
    chain.fail_reads.add("getBalance")
    with pytest.raises(RuntimeError):
        r.may_open()
    step(r, chain, 2)
    assert chain.sent == []
    assert r.counts["select_failed"] >= 1


@pytest.mark.parametrize("trip_the_cap", ["priority", "loss", "floor"])
def test_exits_are_never_blocked_by_a_cap(chain, key_env, tmp_path, trip_the_cap):
    r = runner(chain, key_env, tmp_path)
    address = open_one(r, chain)
    step(r, chain)
    chain.active[POOL_A] = -520
    if trip_the_cap == "priority":
        spend(r, chain, priority=r.s.caps.max_priority_lamports_day * 2)
        assert r.may_open(counting_slots=False) == "priority_cap"
    elif trip_the_cap == "loss":
        spend(r, chain, delta=-r.s.caps.max_daily_loss_lamports * 2)
        assert r.may_open(counting_slots=False) == "daily_loss_stop"
    else:
        chain.world["lamports"] = 10 * 10**7  # Enough to pay for the exit, below the floor.
        assert r.may_open(counting_slots=False) == "wallet_floor"
    step(r, chain, 5)
    assert r.store.position(address)["status"] == "done"


def test_a_stuck_token_holds_its_slot(chain, key_env, tmp_path):
    r, address = moved(chain, key_env, tmp_path, caps=replace(live.Caps(), max_open=1))
    chain.bad_price = 0.99
    step(r, chain, 3)
    assert r.store.position(address)["status"] == "sweeping"
    assert r.may_open() == "max_open"


# ------------------------------------------------------------------------------------ opening rules
def test_rent_is_refused_through_the_runner(chain, key_env, tmp_path):
    chain.missing = {-8}
    r = runner(chain, key_env, tmp_path)
    step(r, chain, 3)
    assert chain.sent == []
    assert "bin_array_rent_refused" in r.last_reasons


def test_rent_with_the_explicit_setting(chain, key_env, tmp_path):
    chain.missing = {-8}
    r = runner(chain, key_env, tmp_path, allow_bin_array_rent=True)
    step(r, chain, 2)
    assert "initialize_bin_array" in chain.kinds()[0]


def test_a_pool_with_rewards_is_refused(chain, key_env, tmp_path):
    chain.rewards = True
    r = runner(chain, key_env, tmp_path)
    step(r, chain, 3)
    assert chain.sent == []
    assert "pool_has_rewards" in r.last_reasons


def test_a_disabled_pool_is_refused(chain, key_env, tmp_path):
    chain.status = 1
    r = runner(chain, key_env, tmp_path)
    step(r, chain, 3)
    assert chain.sent == []
    assert "pool_disabled" in r.last_reasons


def test_an_open_that_fails_simulation_is_not_sent(chain, key_env, tmp_path):
    chain.sim_error = {"InstructionError": [1, {"Custom": 6004}]}
    r = runner(chain, key_env, tmp_path)
    step(r, chain, 3)
    assert chain.sent == []
    assert "simulation_failed" in r.last_reasons


def test_an_oversized_open_is_a_named_refusal(chain, key_env, tmp_path, monkeypatch):
    real = tx.compile_message

    def too_big(*a: Any, **k: Any) -> Any:
        raise tx.TxTooLarge("transaction_too_large:1300")

    monkeypatch.setattr(tx, "compile_message", too_big)
    r = runner(chain, key_env, tmp_path)
    step(r, chain, 2)
    assert chain.sent == []
    assert "transaction_too_large" in r.last_reasons
    monkeypatch.setattr(tx, "compile_message", real)


def test_a_two_sided_open_walks_at_most_two_arrays(chain, key_env, tmp_path):
    arm = Arm("w", 30, 21_600, 15.0, 6.0, 1_800, two_sided=10)
    r = runner(chain, key_env, tmp_path, arm=arm, profile=replace(PROFILE, arms=(arm,), primary="w"))
    open_one(r, chain)
    swap = next(ix for ix in parse(chain.sent[0])[1] if kind_of(ix) == "swap2")
    assert len(swap["accounts"]) - len(tx.INSTRUCTIONS["swap2"].accounts) == live.OPEN_SWAP_ARRAYS


def test_a_mint_the_paper_bot_refuses_is_refused_here(chain, key_env, tmp_path, monkeypatch):
    r = runner(chain, key_env, tmp_path)
    monkeypatch.setattr(r.reader, "mint_problem", lambda mint: "transfer_hook")
    step(r, chain, 3)
    assert chain.sent == []
    assert "transfer_hook" in r.last_reasons


def test_two_pools_of_one_token_are_not_held_together(chain, key_env, tmp_path):
    MINTS_BEFORE = dict(MINTS)
    MINTS[POOL_B] = MINTS[POOL_A]
    try:
        r = runner(chain, key_env, tmp_path, caps=live.Caps())
        open_one(r, chain)
        step(r, chain, 3)
        assert {row["pool"] for row in r.store.active()} == {POOL_A, POOL_C}
    finally:
        MINTS.update(MINTS_BEFORE)


def test_a_cooling_pool_is_skipped(chain, key_env, tmp_path):
    long_cool = replace(PROFILE, cooldown_s=10_000.0)
    r = runner(chain, key_env, tmp_path, profile=long_cool)
    address = open_one(r, chain)
    step(r, chain)
    chain.clock += 200
    step(r, chain, 4)
    assert r.store.position(address)["status"] == "done"
    pools = [row["pool"] for row in r.store.rows("SELECT pool FROM positions ORDER BY created_ms")]
    assert pools.count(POOL_A) == 1


def test_a_two_sided_arm_swaps_and_opens_in_one_transaction(chain, key_env, tmp_path):
    arm = Arm("w", 30, 21_600, 15.0, 6.0, 1_800, two_sided=10)
    r = runner(chain, key_env, tmp_path, arm=arm, profile=replace(PROFILE, arms=(arm,), primary="w"))
    address = open_one(r, chain)
    assert chain.kinds()[0] == ["swap2", "initialize_position", "add_liquidity_by_strategy2"]
    step(r, chain)
    assert r.store.position(address)["status"] == "open"
    held = chain.world["positions"][address]
    assert (held["lo"], held["hi"]) == (-510, -490)


# ------------------------------------------------------------------------------------------ re-open
REOPEN = Arm("r", 4, 100, 6.0, 2.0, 60, reopen_width=3, reopen_hold_s=50)


def reopening(chain: Ledger, env: dict[str, str], tmp_path: Path) -> tuple[live.Runner, str]:
    chain.listed = [POOL_A]
    r = runner(chain, env, tmp_path, arm=REOPEN, profile=replace(PROFILE, arms=(REOPEN,), primary="r", cooldown_s=10_000.0))
    address = open_one(r, chain)
    step(r, chain)
    chain.active[POOL_A] = -506
    return r, address


def test_a_leg_ending_in_token_reopens_around_the_price(chain, key_env, tmp_path):
    r, first = reopening(chain, key_env, tmp_path)
    step(r, chain, 3)
    rows = r.store.rows("SELECT * FROM positions ORDER BY created_ms")
    assert rows[0]["address"] == first and rows[0]["note"] == "reopened"
    second = rows[1]
    assert second["reopened"] == 1 and second["trip"] == rows[0]["trip"]
    assert (second["lo"], second["hi"]) == (-509, -503)
    assert second["address"] in chain.world["positions"]
    assert ["swap2"] not in chain.kinds()


def test_a_reopened_position_exits_for_good(chain, key_env, tmp_path):
    r, _ = reopening(chain, key_env, tmp_path)
    step(r, chain, 4)
    chain.active[POOL_A] = -530
    step(r, chain, 6)
    assert r.store.active() == []
    assert chain.world["positions"] == {}
    assert len(r.store.rows("SELECT * FROM trips")) == 1
    assert r.store.rows("SELECT * FROM trips")[0]["net_lamports"] is not None


def test_a_token_only_leg_reopens_without_touching_wallet_sol(chain, key_env, tmp_path):
    """The rent refunded by the exit is the wallet's, not the leg's. It must not go into the pool."""
    r, _ = reopening(chain, key_env, tmp_path)
    step(r, chain, 3)
    raw = chain.sent[2]
    parsed = parse(raw)[1]
    assert not any(ix["program"] == tx.SYSTEM for ix in parsed)  # Nothing wrapped.
    add = next(ix for ix in parsed if kind_of(ix) == "add_liquidity_by_strategy2")
    amount_x, amount_y, active_id, slip, lo, hi = struct.unpack_from("<QQiiii", add["data"], 8)
    assert amount_y == 0 and amount_x > 0
    assert (lo, hi) == (-505, -503)  # Strictly above the price.
    assert (active_id, slip) == (-514, 8)  # Lands only if the price has not risen.


def test_a_leg_holding_sol_too_reopens_on_both_sides(chain, key_env, tmp_path):
    r, first = reopening(chain, key_env, tmp_path)
    chain.active[POOL_A] = -503  # Inside the range and mostly token, so it wants a re-open, with SOL too.
    real = live.exit_reason
    live.exit_reason = lambda plan, state: "stop"
    try:
        step(r, chain)
    finally:
        live.exit_reason = real
    assert r.store.position(first)["reopen_lamports"] > live.REOPEN_MIN_LAMPORTS
    step(r, chain, 2)
    parsed = parse(chain.sent[2])[1]
    assert any(ix["program"] == tx.SYSTEM for ix in parsed)
    add = next(ix for ix in parsed if kind_of(ix) == "add_liquidity_by_strategy2")
    assert struct.unpack_from("<QQ", add["data"], 8)[1] == r.store.position(first)["reopen_lamports"]


def test_a_reopen_never_exceeds_the_ticket(chain, key_env, tmp_path):
    r, first = reopening(chain, key_env, tmp_path)
    step(r, chain)
    r.store.update("positions", "address", first, reopen_lamports=10 * SOL, reopen_wanted=1)
    step(r, chain, 2)
    add = next(ix for ix in parse(chain.sent[2])[1] if kind_of(ix) == "add_liquidity_by_strategy2")
    assert struct.unpack_from("<QQ", add["data"], 8)[1] == r.s.ticket_lamports


def test_a_reopen_the_caps_refuse_sells_instead(chain, key_env, tmp_path):
    r, first = reopening(chain, key_env, tmp_path)
    spend(r, chain, delta=-2 * r.s.caps.max_daily_loss_lamports)
    step(r, chain, 4)
    assert len(r.store.rows("SELECT * FROM positions")) == 1
    assert r.store.position(first)["status"] == "done"
    assert any("swap2" in k for k in chain.kinds())


def test_a_reopen_that_fails_to_land_sells_the_token(chain, key_env, tmp_path):
    r, _ = reopening(chain, key_env, tmp_path)
    step(r, chain, 1)  # The exit landed.
    chain.landing = "revert"
    step(r, chain, 2)
    second = r.store.rows("SELECT * FROM positions ORDER BY created_ms")[1]
    assert second["status"] == "sweeping"
    chain.landing = "land"
    for _ in range(3):
        chain.clock += 400
        step(r, chain)
    assert r.store.active() == []
    assert any("swap2" in k for k in chain.kinds())


# ------------------------------------------------------------------------------------------ reporting
def test_status_names_no_wallet_or_key(chain, key_env, tmp_path):
    r, _ = moved(chain, key_env, tmp_path)
    step(r, chain)
    text = json.dumps(live.status(r.store))
    assert WALLET not in text
    assert "version" in text


def test_a_daily_status_file_is_written_atomically(chain, key_env, tmp_path):
    r = runner(chain, key_env, tmp_path)
    r.housekeeping()
    body = json.loads((tmp_path / "lp" / "status.json").read_text(encoding="utf-8"))
    assert body["live"] is True
    assert body["version"] == live.VERSION


def test_a_finished_trip_still_unread_stops_opening(chain, key_env, tmp_path):
    r = runner(chain, key_env, tmp_path)
    address = open_one(r, chain)
    step(r, chain)
    chain.tx_unreadable = True
    chain.clock += 200  # Past the hold limit, all still SOL.
    step(r, chain, 4)
    assert r.store.position(address)["status"] == "done"
    assert r.may_open() == "pnl_unknown"
    assert r.store.rows("SELECT net_lamports FROM trips")[0]["net_lamports"] is None
    chain.tx_unreadable = False
    step(r, chain)
    assert r.store.rows("SELECT net_lamports FROM trips")[0]["net_lamports"] is not None


# -------------------------------------------------------------------------------------- mutation run
MUTANTS: list[tuple[str, str, str, str]] = [
    # (label, file, guard as written, guard removed)
    ("gate: env flag", "lp_live", 'if env.get(LIVE_ENV) != "1":', "if False:"),
    ("gate: failure stops the run", "lp_live", "            raise SystemExit(str(exc)) from None", "            pass"),
    ("run: default is dry", "lp_live", 'if os.environ.get(LIVE_ENV) == "1":\n        try:', "if True:\n        try:"),
    ("run: rpc guard used", "lp_live", "transport = Transport(rpc_url())", 'transport = Transport("https://x")'),
    ("reconcile: a lost open is cleaned up", "lp_live", '        elif self.now_ms() - row["updated_ms"] > OPEN_UNREAD_S * 1000:', "        elif False:"),
    ("reconcile: a lagging node gets time", "lp_live", '        elif self.now_ms() - row["updated_ms"] > OPEN_UNREAD_S * 1000:', "        elif True:"),
    ("gate: key path set", "lp_live", "    if not path:\n        raise LiveRefused(\"key_file_not_set\")", "    path = path or 'x'"),
    ("gate: key matches wallet", "lp_live", "if base58.b58encode(bytes(key.verify_key)).decode() != wallet:", "if False:"),
    ("gate: Kaiba's wallet refused", "lp_live", "if wallet == kaiba:", "if False:"),
    ("gate: Kaiba's wallet unknown refused", "lp_live", "    if not kaiba:", "    if False:"),
    ("gate: key file private", "lp_live", "if POSIX and mode & 0o077:", "if False:"),
    ("gate: proof checked", "lp_live", "if not isinstance(gate, Gate) or gate.proof is not _PROOF:", "if False:"),
    ("send: read allowlist", "lp_live", "if method not in READ_METHODS:", "if False:"),
    ("send: broadcast demands gate", "lp_live", "        require_gate(gate)\n        return super()", "        return super()"),
    ("send: dry sender refuses", "lp_live", '    def send(self, raw: bytes) -> str:\n        raise LiveRefused("dry_mode_cannot_send")',
     '    def send(self, raw: bytes) -> str:\n        return ""'),
    ("send: transmit refuses dry", "lp_live", '        if not self.sender.can_send:\n            raise LiveRefused("dry_mode_cannot_send")\n', ""),
    ("run: gate wallet is run wallet", "lp_live", "if self.sender.can_send and require_gate(self.sender.gate).wallet != settings.wallet:", "if False:"),
    ("cap: ticket", "lp_live", "if settings.ticket_lamports > settings.caps.ticket_lamports:", "if False:"),
    ("cap: max open", "lp_live", "if counting_slots and len(self.store.active()) >= min(caps.max_open, self.s.profile.max_open):", "if False:"),
    ("cap: priority", "lp_live", "if self.store.priority_since(day) + self.s.priority_lamports > caps.max_priority_lamports_day:", "if False:"),
    ("cap: daily loss", "lp_live", "if loss >= caps.max_daily_loss_lamports:", "if False:"),
    ("cap: wallet floor", "lp_live", "if balance - self.s.ticket_lamports - OPEN_FLOAT_LAMPORTS < caps.wallet_floor_lamports:", "if False:"),
    ("cap: unknown P&L refuses", "lp_live", "        if not known:\n            return \"pnl_unknown\"", "        if False:\n            return \"pnl_unknown\""),
    ("cap: chain scan first", "lp_live", "if not self.chain_scan_ok:", "if False:"),
    ("cap: no open past an unresolved send", "lp_live", '        if self.store.unresolved():\n            return "send_unresolved"', ""),
    ("cap: unsold token counted at zero", "lp_live", '" (finished_ms >= ? OR (status=\'running\' AND id IN"\n                              " (SELECT trip FROM positions WHERE status=\'sweeping\' AND sweep_tries > 0)))", ms):',
     '" finished_ms >= ?", ms):'),
    ("cap: an untried sale is not yet a loss", "lp_live", "WHERE status='sweeping' AND sweep_tries > 0)", "WHERE status='sweeping')"),
    ("exit: a cap may not block it", "lp_live", '        """Remove, claim, close. No cap is consulted anywhere on this path."""',
     '        """x"""\n        if self.may_open(counting_slots=False):\n            return'),
    ("exit: goes without a simulator", "lp_live", '            log.warning("exit not simulated: %s", type(exc).__name__)\n',
     '            log.warning("exit not simulated: %s", type(exc).__name__)\n            return\n'),
    ("exit: known-bad not sent", "lp_live", '            if sim.get("err"):\n                self.counts["exit_sim_failed"] += 1',
     '            if False:\n                self.counts["exit_sim_failed"] += 1'),
    ("ambiguous: position waits", "lp_live", 'if self.store.unresolved(row["address"]):', "if False:"),
    ("ambiguous: raised send stays unresolved", "lp_live", 'self.store.update("sends", "signature", signature, status="ambiguous", error=type(exc).__name__)',
     'self.store.update("sends", "signature", signature, status="expired", error=type(exc).__name__)'),
    ("ambiguous: only past validity settles", "lp_live", 'if height <= send["last_valid"] + EXPIRY_MARGIN_BLOCKS:', "if False:"),
    ("ambiguous: margin on the height", "lp_live", 'if height <= send["last_valid"] + EXPIRY_MARGIN_BLOCKS:', 'if height <= send["last_valid"]:'),
    ("ambiguous: rebuilt bytes never sent", "lp_live", 'self.sender.send(base64.b64decode(send["raw"]))', "pass"),
    ("record: committed before broadcast", "lp_live", "        self.store.db.commit()\n        try:\n            self.sender.send(raw)",
     "        try:\n            self.sender.send(raw)"),
    ("reconcile: position decides an expired open", "lp_live", 'if outcome == "expired" and send["kind"] == "open" and position:', "if False:"),
    ("reconcile: exit retried if position remains", "lp_live",
     '            if position:\n                self.store.update("positions", "address", address, status="exiting",',
     '            if False:\n                self.store.update("positions", "address", address, status="exiting",'),
    ("recovery: a written-off open is revived", "lp_live", 'elif row["status"] == "failed":', "elif False:"),
    ("recovery: a revive reads the chain first", "lp_live", 'if self.read_position(row["address"]) is None:' + chr(10) + "            return" + chr(10) + "        at = self.now_ms()" + chr(10) + '        self.store.update("trips"', "at = self.now_ms()" + chr(10) + '        self.store.update("trips"'),
    ("recovery: chain positions adopted", "lp_live", "if row is None:" + chr(10) + '                self.adopt(item["pubkey"])', "if False:"),
    ("sweep: dust frees its slot", "lp_live", "dust = is_dust(token, state, keys.fee_rate)", "dust = False"),
    ("sweep: an empty book is not dust", "lp_live", "return unfilled <= 0 and out < MIN_SALE_LAMPORTS", "return out < MIN_SALE_LAMPORTS"),
    ("sweep: the dust floor is a floor", "lp_live", "return unfilled <= 0 and out < MIN_SALE_LAMPORTS", "return unfilled <= 0"),
    ("sweep: dust is recorded", "lp_live", 'self.finish(row, token_left=token, note="dust_left")', "self.finish(row)"),
    ("sweep: fee read from the chain", "lp_live", "sweep_quote(token, state, keys.fee_rate, slip)", "sweep_quote(token, state, 0.03, slip)"),
    ("sweep: ladder widens", "lp_live", 'slip = ladder[min(row["sweep_tries"], len(ladder) - 1)]', "slip = ladder[0]"),
    ("sweep: ladder is bounded", "lp_live", 'slip = ladder[min(row["sweep_tries"], len(ladder) - 1)]', 'slip = row["sweep_tries"] * 5_000'),
    ("sweep: failure keeps the token", "lp_live", '        tries = row["sweep_tries"] + 1\n',
     '        tries = row["sweep_tries"] + 1\n        self.finish(row)\n'),
    ("marks: dilution off for real shares", "lp_live", 'bins={b: {**row, "supply": 0} for b, row in state.bins.items()}', "bins=state.bins"),
    ("marks: partial read refused", "lp_live", '            raise ValueError("position_bin_unread")', "            continue"),
    ("open: swap array limit", "lp_live", "liquid, take=OPEN_SWAP_ARRAYS)", "liquid)"),
    ("open: simulation must pass", "lp_live", '        if sim.get("err"):\n            log.info("open simulation failed: %s", str(sim["err"])[:160])',
     '        if False:\n            log.info("open simulation failed: %s", str(sim["err"])[:160])'),
    ("open: rewards pool refused", "lp_live", "        if keys.rewards_present:", "        if False:"),
    ("open: disabled pool refused", "lp_live", "        if keys.status != 0:", "        if False:"),
    ("open: one token at a time", "lp_live", "(mint in held or self.cooling(address)", "(self.cooling(address)"),
    ("open: cooldown", "lp_live", "(mint in held or self.cooling(address) or", "(mint in held or"),
    ("reopen: a cap refuses it", "lp_live", "if not self.sender.can_send or self.may_open(counting_slots=False):", "if not self.sender.can_send:"),
    ("reopen: bounded by the ticket", "lp_live", 'lamports = min(row["reopen_lamports"], row["ticket"])', 'lamports = row["reopen_lamports"]'),
    ("reopen: sized from holdings, not the refund", "lp_live", "reopen_lamports=int(worth.y + worth.fee_y)", "reopen_lamports=10**9"),
    ("reopen: bound keeps token above the price", "lp_tx", "active_id=active - down_bins, max_slippage=down_bins", "active_id=active + down_bins, max_slippage=down_bins"),
    ("reopen: failed re-open sells", "lp_live", '            elif row["reopened"]:', "            elif False:"),
    ("rent: bin array refused", "lp_tx", "if absent and not allow_bin_array_rent:", "if False:"),
    ("rent: missing extension refused", "lp_tx", "if pool.extension is None and any(overflows_bitmap(i) for i in indices):", "if False:"),
    ("bound: one-sided clear of the active bin", "lp_tx", "    if hi >= active:", "    if False:"),
    ("bound: centred up, not on the price", "lp_tx", "return active + span, span", "return active, span"),
    ("bound: two-sided straddles", "lp_tx", "    if not lo <= active <= hi:", "    if False:"),
    ("pool: quote must be SOL", "lp_tx", "    if pool.mint_y != NATIVE_MINT:", "    if False:"),
    ("packet: over the limit refused", "lp_tx", "    if message.size > MAX_TX_BYTES:", "    if False:"),
    ("idl: a required account is required", "lp_tx", "            if not acc.optional:", "            if False:"),
]


def run_mutants() -> int:
    """Remove each guard in turn. A mutant that survives is a guard no test holds. Restores every file."""
    import os
    import time

    root = Path(__file__).resolve().parents[1]
    paths = {"lp_live": root / "kaiba/execution/lp_live.py", "lp_tx": root / "kaiba/execution/lp_tx.py"}
    if Path(live.__file__).resolve() != paths["lp_live"]:
        raise SystemExit(f"this run imported another checkout: {live.__file__}")
    originals = {k: p.read_bytes() for k, p in paths.items()}
    survived, caught, stale, hung = [], 0, [], []
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        for label, name, old, new in MUTANTS:
            source = originals[name].decode("utf-8")
            if source.count(old) < 1:
                stale.append(label)
                continue
            mutated = source.replace(old, new, 1)
            paths[name].write_bytes(mutated.encode("utf-8"))
            started = time.monotonic()
            try:
                done = subprocess.run([sys.executable, "-m", "pytest", "tests/test_lp_tx.py", "tests/test_lp_live.py", "-x", "-q",
                                       "--no-header", "-p", "no:cacheprovider"], cwd=root, env=env, capture_output=True,
                                      text=True, timeout=90, check=False)
            except subprocess.TimeoutExpired:
                # A hang is not a pass, but no test said what broke, so it is reported as its own failure.
                paths[name].write_bytes(originals[name])
                hung.append(label)
                print(f"HUNG      {label}", flush=True)
                continue
            paths[name].write_bytes(originals[name])
            failing = re.findall(r"^FAILED \S+::(\S+)", done.stdout, re.M)
            if done.returncode == 0:
                survived.append(label)
                print(f"SURVIVED  {label}", flush=True)
            else:
                caught += 1
                print(f"caught    {label} ({failing[0] if failing else 'error'}, {time.monotonic() - started:.1f}s)", flush=True)
    finally:
        for name, path in paths.items():
            path.write_bytes(originals[name])
    print(json.dumps({"mutants": len(MUTANTS), "caught": caught, "survived": survived, "stale": stale, "hung": hung}, indent=1))
    return 1 if survived or stale or hung else 0


if __name__ == "__main__":
    raise SystemExit(run_mutants() if "--mutants" in sys.argv else 2)
