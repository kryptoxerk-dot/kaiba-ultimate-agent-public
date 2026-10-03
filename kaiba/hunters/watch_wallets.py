"""Watch-only view of the owner's hunter wallets. Kaiba holds NO key for them.

Owner, 2026-10-02: funded a dedicated EVM wallet and a Solana wallet for NFT minting and
airdrop/points hunting, and asked that the keys stay off this system (they do: never in
the repo, never on the box). This module only READS their
public balances, so Kaiba can report what they hold and alert when it changes.

MEASURED at registration: the EVM wallet held 0.363886 ETH on BASE and nothing on
Robinhood Chain, Ethereum or Arbitrum -- the owner funded Base, not Robinhood. Base is
therefore watched too, so a bridge to Robinhood shows up as a pair of changes.

Output contract (it runs as a no-model Hermes job whose stdout is delivered to Telegram):
print ONLY when something changed since the last run (or on the first run), so an
unchanged wallet is silent. State lives in ``kv`` under ``hunter_wallets:<chain>:<addr>``.

Reads use GMGN ``portfolio token-balance`` at DISCOVERY priority -- below every live
stop-loss and copy-manager read in the shared gmgn bucket -- with a public-RPC fallback
for EVM chains GMGN cannot answer.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from kaiba.core.db import connect, fetch_one, jdump, jload, upsert
from kaiba.core.schemas import NATIVE_DECIMALS, Chain, now_ms

log = logging.getLogger(__name__)

KV_PREFIX = "hunter_wallets:"

#: Public JSON-RPC used only when GMGN has no reading for an EVM chain. Robinhood's RPC
#: refuses Python-urllib's default User-Agent (MEASURED 2026-09-21), hence the header.
PUBLIC_EVM_RPC: dict[Chain, str] = {
    Chain.BASE: "https://mainnet.base.org",
    Chain.ROBINHOOD: "https://rpc.mainnet.chain.robinhood.com",
    Chain.ETH: "https://ethereum-rpc.publicnode.com",
}
#: MEASURED 2026-10-02: GMGN's token-balance answers sol for this wallet with a payload
#: parse_native_balance rejects ("unexpected payload list"), so sol needs a fallback too.
PUBLIC_SOL_RPC = "https://api.mainnet-beta.solana.com"
USER_AGENT = "kaiba-watch/1.0"

#: A change smaller than this many base units is noise (rent/fee dust), not news.
MIN_CHANGE_UNITS: dict[Chain, int] = {Chain.SOL: 1_000_000}  # 0.001 SOL
DEFAULT_MIN_CHANGE_UNITS = 10**14  # 0.0001 ETH


@dataclass(frozen=True)
class Watched:
    chain: Chain
    address: str
    label: str = ""

    @classmethod
    def parse(cls, spec: str) -> Watched:
        """``chain:address[:label]``."""
        parts = spec.split(":", 2)
        if len(parts) < 2 or not parts[1]:
            raise ValueError(f"watch spec must be chain:address[:label], got {spec!r}")
        return cls(Chain(parts[0].strip().lower()), parts[1].strip(), (parts[2] if len(parts) > 2 else "").strip())


@dataclass(frozen=True)
class Reading:
    base_units: int | None
    source: str
    note: str = ""


def _gmgn_native(w: Watched) -> Reading:
    from kaiba.core.limiter import Priority  # noqa: PLC0415 - module-local, like watchdog
    from kaiba.execution.risk import NATIVE_BALANCE_TOKEN, parse_native_balance  # noqa: PLC0415
    from kaiba.providers.gmgn_cli import _read  # noqa: PLC0415 - avoid an import cycle

    token = NATIVE_BALANCE_TOKEN.get(w.chain)
    if token is None:
        return Reading(None, "gmgn", f"no native token address for {w.chain.value}")
    got = _read(
        "portfolio.token_balance",
        ["portfolio", "token-balance", "--wallet", w.address, "--token", token],
        w.chain,
        Priority.DISCOVERY,
        {},
    )
    data = getattr(got, "data", None)
    if data is None:
        note = getattr(getattr(got, "receipt", None), "note", None) or "no data"
        return Reading(None, "gmgn", str(note)[:160])
    bal = parse_native_balance(w.chain, data, wallet=w.address)
    return Reading(bal.base_units, "gmgn", bal.note if bal.base_units is None else "")


def _rpc_native(w: Watched) -> Reading:
    if w.chain is Chain.SOL:
        url, method, params = PUBLIC_SOL_RPC, "getBalance", [w.address]
    else:
        url = PUBLIC_EVM_RPC.get(w.chain)
        if url is None:
            return Reading(None, "rpc", f"no public rpc for {w.chain.value}")
        method, params = "eth_getBalance", [w.address, "latest"]
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - fixed https URLs
            out = json.load(resp)
        result = out["result"]
        units = int(result["value"]) if w.chain is Chain.SOL else int(result, 16)
        return Reading(units, "rpc")
    except Exception as exc:  # noqa: BLE001 - a failed read is "unknown", never zero
        return Reading(None, "rpc", f"{type(exc).__name__}: {exc}"[:160])


def read_native(w: Watched) -> Reading:
    """GMGN first; a public RPC when GMGN has nothing. Never invents a zero."""
    got = _gmgn_native(w)
    if got.base_units is None and (w.chain in PUBLIC_EVM_RPC or w.chain is Chain.SOL):
        fallback = _rpc_native(w)
        if fallback.base_units is not None:
            return fallback
    return got


def _human(chain: Chain, units: int | None) -> str:
    if units is None:
        return "unknown"
    dec = NATIVE_DECIMALS.get(chain, 18)
    sym = "SOL" if chain is Chain.SOL else ("BNB" if chain is Chain.BSC else "ETH")
    return f"{(Decimal(units) / (Decimal(10) ** dec)).normalize():f} {sym}"


def _key(w: Watched) -> str:
    return f"{KV_PREFIX}{w.chain.value}:{w.address}"


def refresh(
    conn: sqlite3.Connection,
    wallets: list[Watched],
    *,
    reader: Callable[[Watched], Reading] = read_native,
    now: int | None = None,
) -> list[str]:
    """Read each wallet, store the reading, and return one line per CHANGE.

    A wallet whose reading is unknown keeps its last known value and reports nothing:
    an outage must not look like the funds left.
    """
    ts = now if now is not None else now_ms()
    lines: list[str] = []
    for w in wallets:
        row = fetch_one(conn, "SELECT value FROM kv WHERE key=?", (_key(w),))
        prev: dict[str, Any] = jload(row["value"], {}) if row else {}
        got = reader(w)
        name = f"{w.label or w.chain.value} {w.address[:6]}…{w.address[-4:]} on {w.chain.value}"
        if got.base_units is None:
            if not prev:
                lines.append(f"{name}: cannot read yet ({got.source}: {got.note})")
            continue
        old = prev.get("native_units")
        threshold = MIN_CHANGE_UNITS.get(w.chain, DEFAULT_MIN_CHANGE_UNITS)
        if old is None:
            lines.append(f"{name}: {_human(w.chain, got.base_units)} (now watching)")
        elif abs(got.base_units - int(old)) >= threshold:
            delta = got.base_units - int(old)
            sign = "+" if delta > 0 else "-"
            lines.append(f"{name}: {_human(w.chain, int(old))} -> {_human(w.chain, got.base_units)} "
                         f"({sign}{_human(w.chain, abs(delta))})")
        upsert(conn, "kv", {
            "key": _key(w),
            "value": jdump({"chain": w.chain.value, "address": w.address, "label": w.label,
                            "native_units": str(got.base_units), "source": got.source,
                            "read_ms": ts}),
            "updated_ms": ts,
        }, ["key"])
    return lines


def snapshot(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every watched wallet's last stored reading (for reports and tools)."""
    rows = conn.execute(
        "SELECT value FROM kv WHERE key >= ? AND key < ? ORDER BY key",
        (KV_PREFIX, KV_PREFIX[:-1] + chr(ord(":") + 1)),
    ).fetchall()
    out = []
    for r in rows:
        v = jload(r[0], {})
        if isinstance(v, dict) and v.get("address"):
            try:
                v["native"] = _human(Chain(v["chain"]), int(v["native_units"]))
            except (KeyError, ValueError, TypeError):
                v["native"] = "unknown"
            out.append(v)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="watch-only hunter wallet balances")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--watch", action="append", default=[],
                   help="chain:address[:label]; repeat for each wallet")
    args = p.parse_args(argv)
    wallets = [Watched.parse(s) for s in args.watch]
    if not wallets:
        print("no --watch given", file=sys.stderr)
        return 2
    conn = connect(args.db) if args.db else connect()
    try:
        lines = refresh(conn, wallets)
        conn.commit()
    finally:
        conn.close()
    if lines:
        print("HUNTER WALLETS (watch-only; Kaiba holds no key)")
        for line in lines:
            print("  " + line)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
