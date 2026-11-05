"""Reclaim the rent locked in EMPTY token accounts of a wallet you control. Owner-run, on your own PC.

What it does: closes token accounts holding a balance of exactly zero, and the rent returns to the same
wallet. It never burns, sells or moves a token. An account with any balance, a frozen account, the wrapped
SOL account, and one that another address controls are all left alone.

Without --apply it signs nothing and needs no key: it lists what could be closed and simulates each
batch against the chain, so you can see it would work first.

With --apply, the key is read from the file you name, used only to sign here on your PC, never printed
and never sent anywhere. It must match the wallet you named, or nothing happens. The agent never runs
this with a key, and the key file must stay outside this repository.

A wallet with no SOL cannot pay the transaction fee: send it about 0.002 SOL first.

    python deploy/owner/close_empty_accounts.py <WALLET>
    python deploy/owner/close_empty_accounts.py <WALLET> --apply --keyfile <PATH_TO_KEY>
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import base58
import httpx

RPC = "https://api.mainnet-beta.solana.com"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
NATIVE_MINT = "So11111111111111111111111111111111111111112"
CLOSE_ACCOUNT = 9  # SPL Token instruction index
BATCH = 20  # 20 closes serialise to about 1,000 bytes, under the 1,232-byte packet limit
MAX_TX_BYTES = 1_232


def compact_u16(n: int) -> bytes:
    out = bytearray()
    while True:
        low = n & 0x7F
        n >>= 7
        if n:
            out.append(low | 0x80)
        else:
            out.append(low)
            return bytes(out)


def closable(accounts: Iterable[dict[str, Any]], wallet: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The accounts safe to close, and a count of what was left alone and why."""
    keep, skipped = [], {"holding": 0, "frozen": 0, "wrapped_sol": 0, "other_close_authority": 0, "not_owner": 0}
    for a in accounts:
        info = a["account"]["data"]["parsed"]["info"]
        reason = None
        if info.get("owner") != wallet:
            reason = "not_owner"
        elif int(info["tokenAmount"]["amount"]) != 0:
            reason = "holding"
        elif info.get("state") == "frozen":
            reason = "frozen"
        elif info.get("mint") == NATIVE_MINT:
            reason = "wrapped_sol"
        elif info.get("closeAuthority") not in (None, wallet):
            reason = "other_close_authority"
        if reason:
            skipped[reason] += 1
        else:
            keep.append({"address": a["pubkey"], "lamports": int(a["account"]["lamports"])})
    return keep, skipped


def message(wallet: str, program: str, accounts: list[str], blockhash: str) -> bytes:
    """A legacy message: the wallet pays, signs and receives the rent from every close."""
    keys = [wallet, *accounts, program]
    header = bytes([1, 0, 1])  # one signer, the program is the one read-only account
    body = header + compact_u16(len(keys)) + b"".join(base58.b58decode(k) for k in keys) + base58.b58decode(blockhash)
    body += compact_u16(len(accounts))
    program_index = len(keys) - 1
    for i in range(1, len(accounts) + 1):
        body += bytes([program_index]) + compact_u16(3) + bytes([i, 0, 0]) + compact_u16(1) + bytes([CLOSE_ACCOUNT])
    return body


def transaction(msg: bytes, signature: bytes = bytes(64)) -> bytes:
    raw = compact_u16(1) + signature + msg
    if len(raw) > MAX_TX_BYTES:
        raise ValueError("transaction_too_large")
    return raw


def load_signer(path: Path, wallet: str) -> Callable[[bytes], bytes]:
    """A signing function from a key file. Refuses unless the key belongs to ``wallet``."""
    from nacl.signing import SigningKey

    raw = path.read_text(encoding="utf-8").strip()
    secret = bytes(json.loads(raw)) if raw.startswith("[") else base58.b58decode(raw)
    if len(secret) not in (32, 64):
        raise SystemExit("key_file_not_a_solana_key")
    key = SigningKey(secret[:32])
    if base58.b58encode(bytes(key.verify_key)).decode() != wallet:
        raise SystemExit("key_does_not_match_wallet")
    return lambda data: key.sign(data).signature


class Chain:
    def __init__(self, url: str = RPC):
        self.url = url
        self.client = httpx.Client(timeout=30, headers={"User-Agent": "kaiba-close-empty-accounts"})

    def call(self, method: str, params: list[Any]) -> Any:
        for _ in range(5):
            r = self.client.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if r.status_code == 429:
                time.sleep(11)
                continue
            r.raise_for_status()
            body = r.json()
            if "error" in body:
                raise RuntimeError(str(body["error"])[:200])
            return body["result"]
        raise RuntimeError("rate_limited")


def run(chain: Any, wallet: str, sign: Callable[[bytes], bytes] | None, *, batch: int = BATCH) -> dict[str, Any]:
    balance = chain.call("getBalance", [wallet, {"commitment": "confirmed"}])["value"]
    out: dict[str, Any] = {"balance_sol": balance / 1e9, "closed": 0, "reclaimed_sol": 0.0, "simulated_ok": 0,
                           "simulated_failed": 0, "signatures": []}
    total, skipped_all = [], {}
    for program in (TOKEN, TOKEN_2022):
        res = chain.call("getTokenAccountsByOwner", [wallet, {"programId": program},
                                                     {"encoding": "jsonParsed", "commitment": "confirmed"}])["value"]
        keep, skipped = closable(res, wallet)
        for k, v in skipped.items():
            skipped_all[k] = skipped_all.get(k, 0) + v
        total.append((program, keep))
    out["left_alone"] = skipped_all
    out["closable"] = sum(len(k) for _, k in total)
    out["closable_sol"] = sum(a["lamports"] for _, k in total for a in k) / 1e9
    for program, keep in total:
        for start in range(0, len(keep), batch):
            part = keep[start:start + batch]
            blockhash = chain.call("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"]
            msg = message(wallet, program, [a["address"] for a in part], blockhash)
            sim = chain.call("simulateTransaction", [base64.b64encode(transaction(msg)).decode(), {
                "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True, "commitment": "confirmed"}])
            error = sim["value"].get("err")
            if error:
                out["simulated_failed"] += 1
                out["simulation_error"] = str(error)[:160]
                continue
            out["simulated_ok"] += 1
            if sign is None:
                continue
            raw = transaction(msg, sign(msg))
            sig = chain.call("sendTransaction", [base64.b64encode(raw).decode(), {"encoding": "base64"}])
            for _ in range(30):
                st = chain.call("getSignatureStatuses", [[sig]])["value"][0]
                if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                    break
                time.sleep(1)
            if st and not st.get("err"):
                out["closed"] += len(part)
                out["reclaimed_sol"] += sum(a["lamports"] for a in part) / 1e9
                out["signatures"].append(sig)
            else:
                raise SystemExit(f"stopped: a close did not confirm ({sig})")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wallet")
    parser.add_argument("--apply", action="store_true", help="sign and send; without it nothing is signed")
    parser.add_argument("--keyfile", type=Path, help="path to the wallet's key; needed only with --apply")
    parser.add_argument("--rpc", default=RPC)
    args = parser.parse_args()
    if args.apply and not args.keyfile:
        parser.error("--apply needs --keyfile")
    sign = load_signer(args.keyfile, args.wallet) if args.apply else None
    print(json.dumps(run(Chain(args.rpc), args.wallet, sign), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
