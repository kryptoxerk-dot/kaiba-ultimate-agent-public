"""Keyless launch checks ported from Vanguard, for the tweet launcher.

Owner, 2026-10-07: "use vanguard inside the agent if possible". Vanguard
(``C:\\Codex\\vanguard``) is a fair-launch toolkit whose send path needs a raw deployer key,
which never goes on this box (the agent launches through GMGN's custodial wallet). Everything
Vanguard does BEFORE signing is keyless, and these are the pieces worth having here:

* :func:`decode_pump_global` -- port of ``decodeGlobal`` (``dashboard/pump-sign.js:198-226``,
  pump.fun's published IDL layout). It tells us, from the chain and at launch time, whether
  coin creation is on, whether **Cashback** is still enabled (pump.fun deprecated it for new
  launches on 2026-09-12) and whether **Holder Rewards** is, plus the curve's initial
  reserves -- so the 5% dev buy is sized on the live curve, not a remembered constant.
* :func:`dev_buy_receipt_check` -- Vanguard's post-launch receipt rule
  (``snipers/adapters/pons.js:354-398``: the dev balance must equal the dev buy). On Pons it
  is the guard for the one way the deployer's snipe-tax exemption could fail: the factory
  exempts ``msg.sender`` and the creator fee recipient, so if GMGN launches through a router
  the exemption rides on GMGN's choice of recipient (``docs/ROBINHOOD_PONS_V2_OPENING_BUNDLE.md:36-59``).
  A dev buy that received far fewer tokens than the curve promised is a tax (or a fee setting)
  we did not expect, and the chain is switched off until a human looks.

MEASURED (docs/research/line4-antisniper-tax-pons.md:116-146): ``currentSnipeTaxBps(deployer)``
is 0, and 1,106 of the 1,120 second-0 buys -- the deployer's atomic buy inside
``launchToken`` -- paid 0 snipe tax. Vanguard's Pons V2 profile
(``automation/reports/robinhood-pons-v2-profile-candidate.json``): launch fee 0.0005 ETH,
curve fee 100 bps, phantom quote 1.68 ETH, supply 1e27 atoms, graduation 4.2 ETH real.
"""

from __future__ import annotations

import base64
import hashlib
import struct
from decimal import Decimal
from typing import Any

from kaiba.core.schemas import Chain

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
#: Anchor discriminator of pump.fun's ``Global`` account (Vanguard DISC.global).
GLOBAL_DISC = bytes([167, 232, 232, 177, 200, 108, 114, 127])

#: pump.fun ``Global`` fields in IDL order (Vanguard GLOBAL_FIELDS, pump-sign.js:198-209).
GLOBAL_FIELDS: tuple[tuple[str, Any], ...] = (
    ("initialized", "bool"), ("authority", "pubkey"), ("feeRecipient", "pubkey"),
    ("initialVirtualTokenReserves", "u64"), ("initialVirtualSolReserves", "u64"),
    ("initialRealTokenReserves", "u64"), ("tokenTotalSupply", "u64"), ("feeBasisPoints", "u64"),
    ("withdrawAuthority", "pubkey"), ("enableMigrate", "bool"), ("poolMigrationFee", "u64"),
    ("creatorFeeBasisPoints", "u64"), ("feeRecipients", ("pubkey", 7)),
    ("setCreatorAuthority", "pubkey"), ("adminSetCreatorAuthority", "pubkey"),
    ("createV2Enabled", "bool"), ("whitelistPda", "pubkey"), ("reservedFeeRecipient", "pubkey"),
    ("mayhemModeEnabled", "bool"), ("reservedFeeRecipients", ("pubkey", 7)),
    ("isCashbackEnabled", "bool"), ("buybackFeeRecipients", ("pubkey", 8)),
    ("buybackBasisPoints", "u64"), ("initialVirtualQuoteReserves", "u64"),
    ("whitelistedQuoteMints", ("pubkey", 1)), ("creatorFeeConfigurable", "bool"),
    ("maxConfigurableCreatorFeeBps", "u64"), ("holderRewardClaimAuthority", "pubkey"),
    ("isHolderRewardEnabled", "bool"),
)

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


def decode_pump_global(data: bytes) -> dict[str, Any]:
    """pump.fun's ``Global`` account, decoded field by field. Raises on a foreign account."""
    if data[:8] != GLOBAL_DISC:
        raise ValueError("not a pump.fun Global account")
    at = 8
    out: dict[str, Any] = {}

    def one(kind: str) -> Any:
        nonlocal at
        if kind == "bool":
            v = data[at] == 1
            at += 1
            return v
        if kind == "u64":
            (v,) = struct.unpack_from("<Q", data, at)
            at += 8
            return v
        v = _b58encode(data[at:at + 32])
        at += 32
        return v

    for name, kind in GLOBAL_FIELDS:
        if at >= len(data):
            raise ValueError(f"pump.fun Global is shorter than its published layout (at {name})")
        out[name] = [one(kind[0]) for _ in range(kind[1])] if isinstance(kind, tuple) else one(kind)
    return out


def pump_global_address() -> str:
    from kaiba.ingest.launch_feed import _b58decode, _on_ed25519_curve

    program = _b58decode(PUMP_PROGRAM)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"global" + bytes([bump]) + program + b"ProgramDerivedAddress").digest()
        if not _on_ed25519_curve(h):
            return _b58encode(h)
    raise RuntimeError("no PDA")  # pragma: no cover


def read_pump_global(conn: Any = None) -> tuple[dict[str, Any] | None, str]:
    """The live Global account over the box's Solana RPC: (decoded | None, note)."""
    from kaiba.core.config import get_settings
    from kaiba.core.limiter import Priority
    from kaiba.providers._http import post_json

    url = get_settings().rpc_for(Chain.SOL)
    if not url:
        return None, "no_solana_rpc"
    got = post_json("rpc", "sol.getAccountInfo", url,
                    json_body={"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                               "params": [pump_global_address(), {"encoding": "base64"}]},
                    priority=Priority.ENTRY, ttl_s=60.0, cache_key="tweet_launch:pump_global",
                    wait_for_slot_s=5.0, timeout_s=10.0, conn=conn)
    value = ((got.data or {}).get("result") or {}).get("value") if got.ok and isinstance(got.data, dict) else None
    if not isinstance(value, dict) or not value.get("data"):
        return None, "global_unread"
    try:
        return decode_pump_global(base64.b64decode(value["data"][0])), "ok"
    except ValueError as exc:
        return None, str(exc)


def pump_preflight(glob: dict[str, Any] | None, holder_fee_args: tuple[str, ...]) -> str | None:
    """Refusal reason for a pump.fun create right now, or None. Unreadable -> no refusal
    (GMGN still validates); a READ that says no is believed."""
    if glob is None:
        return None
    if not glob.get("createV2Enabled", True):
        return "pump:create_disabled"
    if "--is-cashback" in holder_fee_args and not glob.get("isCashbackEnabled", False):
        return "pump:cashback_disabled_on_chain"
    return None


def pump_dev_buy_for_supply(glob: dict[str, Any], pct: float, fee_bps: int = 125) -> Decimal:
    """SOL for ``pct``% of supply on the LIVE initial curve (constant product + buy fee)."""
    vt = Decimal(glob["initialVirtualTokenReserves"])
    vs = Decimal(glob["initialVirtualSolReserves"])
    want = Decimal(glob["tokenTotalSupply"]) * Decimal(str(pct)) / 100
    lamports = vs * vt / (vt - want) - vs
    return lamports * (1 + Decimal(fee_bps) / 10000) / Decimal(10**9)


# --------------------------------------------------------------------------------------
# post-launch receipt check
# --------------------------------------------------------------------------------------

#: A dev buy must receive at least this share of the tokens the curve promised. Fees and
#: slippage on an empty curve are a few percent; a snipe tax is 99%.
RECEIPT_MIN_RATIO = Decimal("0.85")


def expected_dev_tokens(chain: Chain, supply_pct: float | None) -> Decimal | None:
    """Tokens (whole units) a dev buy sized for ``supply_pct`` should receive. 1e9 supply on
    all three launchpads (pump Global tokenTotalSupply 1e15 atoms @6dp; Flap and Pons V2 1e27 @18)."""
    if supply_pct is None:
        return None
    return Decimal(1_000_000_000) * Decimal(str(supply_pct)) / 100


def dev_buy_receipt_check(chain: Chain, supply_pct: float | None, received_atoms: int | None,
                          decimals: int) -> tuple[bool | None, str]:
    """(ok | None when unknowable, note)."""
    exp = expected_dev_tokens(chain, supply_pct)
    if exp is None or received_atoms is None:
        return None, "unknown"
    got = Decimal(received_atoms) / (Decimal(10) ** decimals)
    ratio = got / exp if exp > 0 else Decimal(0)
    return (ratio >= RECEIPT_MIN_RATIO), f"received {got:.0f} of {exp:.0f} expected ({ratio:.3f})"
