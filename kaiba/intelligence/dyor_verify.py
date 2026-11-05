"""Reconcile the DYOR safety providers against chain truth, property by property.

Every field mapping in :mod:`kaiba.providers.goplus` and :mod:`kaiba.providers.rugcheck` was
written against documented response shapes and hand-built fixtures. A fixture proves the
normaliser parses the shape its author imagined; it proves nothing about the shape the
provider actually returns, and nothing at all about whether the value is true. The dossier
those two feed decides whether a token is tradeable, so an unverified mapping there is a
scanner confidently blessing or refusing on a misread field with nothing downstream able to
notice.

This module is the check. Three rules shape it:

**1. Chain truth beats provider claims.** The reference set's ground truth is read from the
mint account over the public RPC (``getAccountInfo`` with ``jsonParsed``, and ``eth_call
owner()`` on EVM), never from a provider. Where a provider disagrees with the mint account,
the provider is wrong and that is the finding. Only properties the mint account can actually
settle are judged against it; the rest (liquidity, holder counts, concentration) can only be
cross-checked provider against provider, and this module says so rather than pretending.

**2. Absent is a third outcome, not a failure.** A provider that stays silent on a property
is doing the right thing. The table therefore counts agree / disagree / absent separately,
because a provider that is right whenever it speaks but speaks about one token in five is a
very different thing from one that answers everything slightly wrong.

**3. It has to be re-runnable.** Provider schemas drift — this whole exercise exists because
one did. :func:`reconcile` runs live, :func:`replay` re-runs the same reconciliation over
recorded responses under ``tests/fixtures/dyor_live/``, and ``kaiba token verify-providers``
drives both. Recordings are raw provider bodies for public tokens: nothing in them is secret,
so nothing is scrubbed.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, EvidenceBasis, Receipt, now_ms
from kaiba.intelligence import dyor
from kaiba.providers._http import get_json, post_json

log = logging.getLogger(__name__)

#: The classic SPL Token program. A mint it owns cannot carry a Token-2022 extension, so a
#: hook, a fee or a permanent delegate is impossible by construction rather than merely absent.
TOKENKEG = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
#: Solana's system program doubles as "nobody" in several provider payloads.
SOL_NULL = "11111111111111111111111111111111"

#: EVM addresses that mean "ownership was given up". GoPlus documents that its owner-gated
#: risk flags describe a capability that, at one of these addresses, has no one able to call
#: it: "When the contract does not have an owner (or if the owner is a black hole address)
#: ... this function will most likely be disabled."
EVM_BLACKHOLES: frozenset[str] = frozenset(
    {
        "",
        "0x0000000000000000000000000000000000000000",
        "0x000000000000000000000000000000000000dead",
        "0x0000000000000000000000000000000000000001",
    }
)

#: Read-only public endpoints, used only when the operator has configured no RPC for a chain.
#: Ground truth must not depend on a credential we may not have.
PUBLIC_RPC: dict[Chain, str] = {
    Chain.SOL: "https://api.mainnet-beta.solana.com",
    Chain.ETH: "https://ethereum-rpc.publicnode.com",
    Chain.BSC: "https://bsc-dataseed.binance.org",
    Chain.BASE: "https://mainnet.base.org",
}

#: ``owner()``, ``decimals()`` and ``totalSupply()`` selectors.
_SEL_OWNER = "0x8da5cb5b"
_SEL_DECIMALS = "0x313ce567"
_SEL_TOTAL_SUPPLY = "0x18160ddd"

#: Longest a single verification call will wait for limiter capacity. A verification run is
#: not latency-sensitive and a 429 costs far more than a slow pass.
SLOT_WAIT_S = 20.0
#: Breathing room between tokens, on top of each adapter's own pacing.
SETTLE_S = 0.35

RECORDING_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "dyor_live"


# --------------------------------------------------------------------------------------
# the reference set
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceToken:
    """One token whose truth we can state independently of the providers being tested."""

    label: str
    address: str
    chain: Chain
    #: Why this token is in the set — the property it is here to exercise.
    why: str
    #: Facts asserted by something other than a provider (Circle's published authorities, a
    #: launchpad's fixed supply). Cross-checked against the mint account, never trusted over it.
    known: dict[str, Any] = field(default_factory=dict)

    @property
    def slug(self) -> str:
        return f"{self.chain.value}-{self.label.lower()}"


#: Solana anchors first: tokens whose authority state is public knowledge *and* readable from
#: the mint account, so a provider disagreeing with them is unambiguously wrong.
REFERENCE: tuple[ReferenceToken, ...] = (
    ReferenceToken(
        "USDC", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", Chain.SOL,
        "Circle holds both the mint and the freeze authority. The single most important "
        "negative control: a provider that reports USDC as revoked is unusable for the "
        "property our blockers are built on.",
        {"mint_authority_revoked": False, "freeze_authority_revoked": False},
    ),
    ReferenceToken(
        "USDT", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", Chain.SOL,
        "Tether holds both authorities under one key. Second negative control.",
        {"mint_authority_revoked": False, "freeze_authority_revoked": False},
    ),
    ReferenceToken(
        "WSOL", "So11111111111111111111111111111111111111112", Chain.SOL,
        "Native mint: no authorities, classic SPL, supply floats with wrapping.",
        {"mint_authority_revoked": True, "freeze_authority_revoked": True},
    ),
    ReferenceToken(
        "BONK", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", Chain.SOL,
        "Both authorities revoked, classic SPL, deep liquidity and two million holders — the "
        "positive control for the market-depth properties.",
        {"mint_authority_revoked": True, "freeze_authority_revoked": True},
    ),
    ReferenceToken(
        "JUP", "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", Chain.SOL,
        "Both authorities revoked; a large non-memecoin control.",
        {"mint_authority_revoked": True, "freeze_authority_revoked": True},
    ),
    ReferenceToken(
        "PYUSD", "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo", Chain.SOL,
        "Token-2022 with a permanent delegate, a mint close authority and a transfer-hook "
        "slot whose program is unset. The only reference token where balance_mutable is "
        "genuinely true, and the test of whether a provider distinguishes a hook slot from "
        "an installed hook.",
        {"mint_authority_revoked": False, "freeze_authority_revoked": False, "balance_mutable": True},
    ),
    ReferenceToken(
        "FLUXB", "FLUXBmPhT3Fd1EDVFdg46YREqHBeNypn1h4EbnTzWERX", Chain.SOL,
        "Token-2022 charging a real 300 bps transfer fee with both authorities revoked. The "
        "only token in the set that can catch a transfer-fee mapping that reads zero.",
        {"transfer_fee_bps": Decimal(300)},
    ),
    # pump.fun, across the lifecycle. Addresses come from this machine's `tokens` table.
    ReferenceToken(
        "PUMPCURVE", "268oC616gC8tBRD5iGn4xfBqgfxZhjHCjaTNys5Qpump", Chain.SOL,
        "Still on the bonding curve: authorities revoked at mint, liquidity in the curve "
        "account rather than a pool. Tests whether a provider invents holder or market data "
        "it does not have.",
    ),
    ReferenceToken(
        "PUMPGRAD", "BQnxyUvZAn6gg2wHwRipNzojhxgGJCJQ9QWdWxkdpump", Chain.SOL,
        "Graduated to the pump.fun AMM. Our dossier graded this A.",
    ),
    ReferenceToken(
        "PUMPCOLLAPSE", "BrYrSE7Y5BJPy1AstdCQnk4cXEVd3fGTkyLBkCKVpump", Chain.SOL,
        "Collapsed to a few dollars of liquidity, and RugCheck reports a creator with a "
        "rug history. Our dossier quarantined it on rugged=True; RugCheck's own report says "
        "rugged=false. This token is why the rugged mapping was rewritten.",
    ),
    ReferenceToken(
        "PUMPDEAD", "3eB4xJAdTEFJVztbW5JmcxfiwjPZfYQzdHG6ZEN1pump", Chain.SOL,
        "Liquidity of 2.2e-07 USD — genuinely dead, and the control proving a real zero-ish "
        "liquidity reading is distinguishable from an unindexed one.",
    ),
    ReferenceToken(
        "PUMPDEVHEAVY", "6iVQbtBvYCrxaCTy3C8TQsAn81KoATHspqRPpkoVpump", Chain.SOL,
        "Our dossier quarantined it on dev_pct=79.3% from RugCheck's creatorBalance. Tests "
        "that figure against the creator's actual token balance on chain.",
    ),
    # EVM. The two judgement mappings live or die here.
    ReferenceToken(
        "PEPE", "0x6982508145454ce325ddbe47a25d4ec3d2311933", Chain.ETH,
        "transfer_pausable=1 with ownership renounced to the zero address. The mapping "
        "transfer_pausable -> freeze authority turns this into a blocker on one of the most "
        "liquid tokens on Ethereum.",
        {"owner_renounced": True},
    ),
    ReferenceToken(
        "BOBO", "0xb90b2a35c65dbc466b04240097ca756ad2005295", Chain.ETH,
        "Second transfer_pausable=1 token with ownership renounced. Confirms PEPE is not a "
        "one-off.",
        {"owner_renounced": True},
    ),
    ReferenceToken(
        "WBTC", "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599", Chain.ETH,
        "Pausable with a live owner: the case where transfer_pausable really does describe "
        "an authority that can strand a holder, so the corrected mapping must still fire.",
    ),
    ReferenceToken(
        "SAFEMOON", "0x8076c74c5e3f5852037f31ff0093eeb8c8add8d3", Chain.BSC,
        "can_take_back_ownership=1 alongside owner_change_balance=0. The mapping "
        "can_take_back_ownership -> balance_mutable makes this a blocker for an authority "
        "that cannot rewrite balances.",
    ),
    ReferenceToken(
        "PIT", "0xa57ac35ce91ee92caefaa8dc04140c8e232c2e50", Chain.BSC,
        "can_take_back_ownership=1 with ownership burned to 0x..dead: renouncement that is "
        "reversible, which is the one case where a dormant capability is still real.",
    ),
    ReferenceToken(
        "VIRTUAL", "0x0b3e328455c4059eeb9e3f84b5543f74e24e7e1b", Chain.BASE,
        "owner_change_balance=1 and hidden_owner=1 — a genuinely balance-mutable token, and "
        "the control proving the corrected mapping has not simply been switched off.",
    ),
    ReferenceToken(
        "BSCUSD", "0x55d398326f99059ff775485246999027b3197955", Chain.BSC,
        "Binance-Peg USDT: mintable by a live owner, and GoPlus reports holder_count=0 for a "
        "token with millions of holders.",
    ),
    ReferenceToken(
        "BRETT", "0x532f27101965dd16442e59d40670faf5ebb142e4", Chain.BASE,
        "Renounced, not mintable, not pausable: the EVM positive control.",
        {"owner_renounced": True},
    ),
)


# --------------------------------------------------------------------------------------
# chain truth
# --------------------------------------------------------------------------------------


def _rpc_url(chain: Chain) -> str | None:
    from kaiba.core.config import get_settings

    configured = (get_settings().rpc_for(chain) or "").strip()
    return configured or PUBLIC_RPC.get(chain)


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return None if d.is_nan() else d


@dataclass(frozen=True)
class ChainTruth:
    """What the mint account itself says. The arbiter, not another opinion."""

    address: str
    chain: Chain
    ok: bool
    receipt: Receipt
    program: str | None = None
    mint_authority: str | None = None
    freeze_authority: str | None = None
    owner_address: str | None = None
    decimals: int | None = None
    supply: int | None = None
    permanent_delegate: str | None = None
    transfer_hook_program: str | None = None
    transfer_fee_bps: Decimal | None = None
    #: Who may raise the Token-2022 transfer fee. Non-null means today's fee is not a promise.
    transfer_fee_authority: str | None = None
    non_transferable: bool | None = None
    default_account_state: str | None = None
    raw: dict[str, Any] | None = None

    @property
    def owner_renounced(self) -> bool | None:
        if self.owner_address is None:
            return None
        return self.owner_address.lower() in EVM_BLACKHOLES

    def properties(self) -> dict[str, Any]:
        """Chain truth expressed in the shared DYOR vocabulary.

        A key is present only where the mint account genuinely settles it. Notably absent:
        ``can_sell`` when nothing on the mint blocks a transfer — a mint account cannot prove
        a token is sellable, only that it is not, so asserting ``True`` here would be the very
        inference this whole module exists to catch.
        """
        if not self.ok:
            return {}
        out: dict[str, Any] = {}
        if self.chain is Chain.SOL:
            out["mint_authority_revoked"] = self.mint_authority is None
            out["freeze_authority_revoked"] = self.freeze_authority is None
            if self.program is not None:
                out["token_2022"] = self.program != TOKENKEG
                out["balance_mutable"] = self.permanent_delegate is not None
                out["transfer_hook"] = self.transfer_hook_program is not None
                out["transfer_fee_bps"] = self.transfer_fee_bps or Decimal(0)
                # A classic SPL mint has no fee mechanism to raise, and a Token-2022 mint's
                # fee is raisable exactly when transferFeeConfigAuthority is set. Both are
                # facts, so this blocker-backing property is checkable rather than taken on
                # trust — which on Solana it otherwise would be.
                out["tax_modifiable"] = self.transfer_fee_authority is not None
            if self.non_transferable or self.default_account_state == "frozen":
                out["can_sell"] = False
            if self.supply is not None:
                out["total_supply"] = Decimal(self.supply)
        return out


def _sol_truth(address: str, conn: Any) -> ChainTruth:
    url = _rpc_url(Chain.SOL)
    if not url:
        return ChainTruth(address, Chain.SOL, False, _dead("rpc.getAccountInfo", "no solana rpc configured"))
    fetched = post_json(
        "rpc",
        "rpc.getAccountInfo",
        url,
        json_body={
            "jsonrpc": "2.0",
            "id": "kaiba-verify",
            "method": "getAccountInfo",
            "params": [address, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        },
        priority=Priority.RESEARCH,
        ttl_s=0,
        wait_for_slot_s=SLOT_WAIT_S,
        conn=conn,
    )
    if not fetched.ok or not isinstance(fetched.data, dict):
        return ChainTruth(address, Chain.SOL, False, fetched.receipt)
    if fetched.data.get("error"):
        note = json.dumps(fetched.data["error"], default=str)[:200]
        return ChainTruth(address, Chain.SOL, False, _dead("rpc.getAccountInfo", note))
    value = (fetched.data.get("result") or {}).get("value")
    if not isinstance(value, dict):
        return ChainTruth(address, Chain.SOL, False, _dead("rpc.getAccountInfo", "mint account not found"))
    return _sol_truth_from_value(address, value, fetched.receipt)


def _sol_truth_from_value(address: str, value: dict[str, Any], receipt: Receipt) -> ChainTruth:
    """Parse one ``getAccountInfo`` value. Split out so a recording replays through it."""
    parsed = (value.get("data") or {}).get("parsed") or {}
    info = parsed.get("info") or {}
    delegate: str | None = None
    hook: str | None = None
    fee_bps: Decimal | None = None
    fee_authority: str | None = None
    non_transferable = False
    account_state: str | None = None
    for ext in info.get("extensions") or []:
        if not isinstance(ext, dict):
            continue
        name = ext.get("extension")
        state = ext.get("state") if isinstance(ext.get("state"), dict) else {}
        if name == "permanentDelegate":
            delegate = state.get("delegate") or None
        elif name == "transferHook":
            # The extension existing only means a hook *may* be installed later; a hook is
            # installed when programId names one. PYUSD carries the slot with programId null.
            program_id = state.get("programId")
            hook = program_id if program_id and program_id != SOL_NULL else None
        elif name == "transferFeeConfig":
            newer = state.get("newerTransferFee") or {}
            fee_bps = _dec(newer.get("transferFeeBasisPoints"))
            authority = state.get("transferFeeConfigAuthority")
            fee_authority = authority if authority and authority != SOL_NULL else None
        elif name == "nonTransferable":
            non_transferable = True
        elif name == "defaultAccountState":
            account_state = str(state.get("accountState") or "") or None
    supply = _dec(info.get("supply"))
    decimals = info.get("decimals")
    return ChainTruth(
        address=address,
        chain=Chain.SOL,
        ok=parsed.get("type") == "mint",
        receipt=receipt,
        program=value.get("owner"),
        mint_authority=info.get("mintAuthority") or None,
        freeze_authority=info.get("freezeAuthority") or None,
        decimals=int(decimals) if isinstance(decimals, int) else None,
        supply=int(supply) if supply is not None else None,
        permanent_delegate=delegate,
        transfer_hook_program=hook,
        transfer_fee_bps=fee_bps,
        transfer_fee_authority=fee_authority,
        non_transferable=non_transferable,
        default_account_state=account_state,
        raw=value,
    )


def _evm_call(chain: Chain, address: str, selector: str, conn: Any) -> str | None:
    url = _rpc_url(chain)
    if not url:
        return None
    fetched = post_json(
        "rpc",
        "rpc.eth_call",
        url,
        json_body={
            "jsonrpc": "2.0",
            "id": "kaiba-verify",
            "method": "eth_call",
            "params": [{"to": address, "data": selector}, "latest"],
        },
        priority=Priority.RESEARCH,
        ttl_s=0,
        wait_for_slot_s=SLOT_WAIT_S,
        conn=conn,
    )
    if not fetched.ok or not isinstance(fetched.data, dict) or fetched.data.get("error"):
        return None
    result = fetched.data.get("result")
    return result if isinstance(result, str) and result.startswith("0x") else None


def _evm_truth(address: str, chain: Chain, conn: Any) -> ChainTruth:
    """Owner, decimals and supply straight from the contract.

    ``owner()`` is the load-bearing one: both judgement mappings under test assume an
    authority exists to exercise the capability GoPlus reports, and this is the only way to
    establish that without trusting GoPlus's own ``owner_address``.
    """
    raw_owner = _evm_call(chain, address, _SEL_OWNER, conn)
    if raw_owner is None:
        return ChainTruth(address, chain, False, _dead("rpc.eth_call", "owner() did not answer"))
    body = raw_owner[2:]
    owner = "0x" + body[-40:] if len(body) >= 40 else None
    decimals_hex = _evm_call(chain, address, _SEL_DECIMALS, conn)
    supply_hex = _evm_call(chain, address, _SEL_TOTAL_SUPPLY, conn)
    return ChainTruth(
        address=address,
        chain=chain,
        ok=True,
        receipt=Receipt(provider="rpc", endpoint="rpc.eth_call", basis=EvidenceBasis.PROVIDER_REPORTED),
        owner_address=owner,
        decimals=_hex_int(decimals_hex),
        supply=_hex_int(supply_hex),
        raw={"owner": raw_owner, "decimals": decimals_hex, "totalSupply": supply_hex},
    )


def _hex_int(value: str | None) -> int | None:
    if not value or not value.startswith("0x") or len(value) < 3:
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def chain_truth(address: str, chain: Chain, *, conn: Any = None) -> ChainTruth:
    """Ground truth for one token, read from the chain and nothing else."""
    if chain is Chain.SOL:
        return _sol_truth(address, conn)
    from kaiba.core.schemas import EVM_CHAINS

    if chain in EVM_CHAINS:
        return _evm_truth(address, chain, conn)
    return ChainTruth(address, chain, False, _dead("rpc", f"no ground-truth reader for {chain.value}"))


def creator_supply_pct(mint: str, creator: str | None, supply: int | None, *, conn: Any = None) -> Decimal | None:
    """The creator's current share of supply, summed over their token accounts.

    This is the independent check on ``dev_pct``, which is a blocker above 30% (a review
    above 10% since 2026-09-22) and is currently sourced from RugCheck's ``creatorBalance``
    with nothing verifying it.

    One theory this function was pointed at and which it does NOT support, recorded so
    nobody re-opens it: that ``creatorBalance`` was routinely counting the pump.fun bonding
    curve's own 79.31% of supply as the creator's. MEASURED ON THE LIVE BOX 2026-09-22,
    only 24 of 546 dev_concentration refusals (4.4%) sit in 79.0-79.7%, and 0.2% of the
    9,809 dossiers carrying a dev_pct do. It happens; it is not systematic. (The claim came
    from ``data/kaiba.db``, the local scratch copy, where it looked like 78.8%.)
    """
    if not creator or not supply:
        return None
    url = _rpc_url(Chain.SOL)
    if not url:
        return None
    total = 0
    for program in (TOKENKEG, TOKEN_2022):
        fetched = post_json(
            "rpc",
            "rpc.getTokenAccountsByOwner",
            url,
            json_body={
                "jsonrpc": "2.0",
                "id": "kaiba-verify",
                "method": "getTokenAccountsByOwner",
                "params": [creator, {"mint": mint, "programId": program}, {"encoding": "jsonParsed"}],
            },
            priority=Priority.RESEARCH,
            ttl_s=0,
            wait_for_slot_s=SLOT_WAIT_S,
            conn=conn,
        )
        if not fetched.ok or not isinstance(fetched.data, dict) or fetched.data.get("error"):
            continue
        for row in (fetched.data.get("result") or {}).get("value") or []:
            amount = (((row.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            raw = (amount.get("tokenAmount") or {}).get("amount")
            parsed_amount = _dec(raw)
            if parsed_amount is not None:
                total += int(parsed_amount)
    return Decimal(total) / Decimal(supply) * 100


def _dead(endpoint: str, note: str) -> Receipt:
    return Receipt(provider="rpc", endpoint=endpoint, basis=EvidenceBasis.UNAVAILABLE, note=note[:300])


# --------------------------------------------------------------------------------------
# reconciliation
# --------------------------------------------------------------------------------------


class Verdict(StrEnum):
    AGREE = "agree"
    DISAGREE = "disagree"
    ABSENT = "absent"
    #: The provider answered but the mint account cannot settle this property. Cross-provider
    #: agreement is the only available check, and it is a weaker one.
    NO_TRUTH = "no_truth"


PROVIDERS: tuple[str, ...] = ("goplus", "rugcheck")

#: Ordered for the printed table: the properties a blocker depends on come first.
TABLE_PROPERTIES: tuple[str, ...] = (
    "can_sell",
    "mint_authority_revoked",
    "freeze_authority_revoked",
    "balance_mutable",
    "transfer_hook",
    "tax_modifiable",
    "transfer_fee_bps",
    "buy_tax_bps",
    "sell_tax_bps",
    "rugged",
    "creator_rug_count",
    "dev_pct",
    "cluster_pct",
    "insider_pct",
    "top10_pct",
    "lp_burned_pct",
    "liquidity_usd",
    "holder_count",
    "price_usd",
    "token_2022",
    "metadata_mutable",
    "source_verified",
    "total_supply",
    "creator",
)


#: Properties the merge does not carry a tolerance for, judged relatively here instead. A
#: supply read twice seconds apart is not the same number on a token that burns on transfer:
#: BONK's report and its mint account differed by 6.7e8 atoms out of 8.8e18, which is drift,
#: not disagreement.
_RELATIVE_FALLBACK: dict[str, Decimal] = {"total_supply": Decimal("0.001")}


def _numbers_agree(prop: str, provider_value: Decimal, truth_value: Decimal) -> bool:
    """Same tolerance the merge itself uses, so this table and the merge cannot disagree."""
    spec = dyor.NUM_PROPERTIES.get(prop)
    if spec is None:
        tolerance = _RELATIVE_FALLBACK.get(prop)
        if tolerance is None:
            return provider_value == truth_value
        scale = max(abs(provider_value), abs(truth_value))
        return scale == 0 or abs(provider_value - truth_value) / scale <= tolerance
    gap = abs(provider_value - truth_value)
    if not spec.relative:
        return gap <= spec.tolerance
    scale = max(abs(provider_value), abs(truth_value))
    return scale == 0 or gap / scale <= spec.tolerance


@dataclass(frozen=True)
class PropertyCheck:
    prop: str
    provider: str
    verdict: Verdict
    provider_value: Any = None
    truth_value: Any = None

    def render(self) -> str:
        if self.verdict is Verdict.ABSENT:
            return f"{self.prop}: {self.provider} silent"
        if self.verdict is Verdict.NO_TRUTH:
            return f"{self.prop}: {self.provider}={_short(self.provider_value)} (no chain truth)"
        return (
            f"{self.prop}: {self.provider}={_short(self.provider_value)} "
            f"chain={_short(self.truth_value)} -> {self.verdict.value}"
        )


def _short(value: Any) -> str:
    if isinstance(value, Decimal):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    text = str(value)
    return text if len(text) <= 24 else text[:21] + "..."


@dataclass
class TokenReport:
    """Everything one reference token established."""

    token: ReferenceToken
    truth: ChainTruth
    props: dict[str, dict[str, Any]] = field(default_factory=dict)
    checks: list[PropertyCheck] = field(default_factory=list)
    #: Properties where the two providers both answered and contradicted each other.
    cross_conflicts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    chain_dev_pct: Decimal | None = None

    @property
    def disagreements(self) -> list[PropertyCheck]:
        return [c for c in self.checks if c.verdict is Verdict.DISAGREE]


def _compare(prop: str, provider: str, provider_value: Any, truth: dict[str, Any]) -> PropertyCheck:
    if provider_value is None:
        return PropertyCheck(prop, provider, Verdict.ABSENT)
    if prop not in truth:
        return PropertyCheck(prop, provider, Verdict.NO_TRUTH, provider_value)
    truth_value = truth[prop]
    if isinstance(truth_value, bool) or isinstance(provider_value, bool):
        agree = bool(provider_value) == bool(truth_value)
    else:
        a, b = _dec(provider_value), _dec(truth_value)
        agree = a is not None and b is not None and _numbers_agree(prop, a, b)
    return PropertyCheck(
        prop, provider, Verdict.AGREE if agree else Verdict.DISAGREE, provider_value, truth_value
    )


def compare_props(
    token: ReferenceToken, truth: ChainTruth, props: dict[str, dict[str, Any]], *, chain_dev_pct: Decimal | None = None
) -> TokenReport:
    """Score one token's provider output against chain truth. Pure; the replay path uses it."""
    truth_props = truth.properties()
    if chain_dev_pct is not None:
        truth_props["dev_pct"] = chain_dev_pct
    report = TokenReport(token=token, truth=truth, props=props, chain_dev_pct=chain_dev_pct)

    for prop in TABLE_PROPERTIES:
        for provider in PROVIDERS:
            if provider not in props:
                continue  # provider does not cover this chain; not a gap in its accuracy
            report.checks.append(_compare(prop, provider, props[provider].get(prop), truth_props))

    for prop in TABLE_PROPERTIES:
        values = [props[p].get(prop) for p in PROVIDERS if p in props and props[p].get(prop) is not None]
        if len(values) < 2:
            continue
        if isinstance(values[0], bool) or isinstance(values[1], bool):
            if bool(values[0]) != bool(values[1]):
                report.cross_conflicts.append(prop)
            continue
        a, b = _dec(values[0]), _dec(values[1])
        if a is not None and b is not None and not _numbers_agree(prop, a, b):
            report.cross_conflicts.append(prop)

    for name, expected in token.known.items():
        if name == "owner_renounced":
            actual: Any = truth.owner_renounced
        else:
            actual = truth.properties().get(name)
        if actual is not None and actual != expected:
            report.notes.append(f"published fact {name}={expected} but chain says {actual}")
    return report


def _goplus_props(token: ReferenceToken, conn: Any) -> tuple[dict[str, Any] | None, Any]:
    from kaiba.providers import goplus

    fetched = goplus.token_security(token.address, token.chain, conn=conn)
    if not fetched.ok:
        return None, None
    return goplus.normalize_security(fetched.data, token.chain), fetched.data


def _rugcheck_props(token: ReferenceToken, conn: Any) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    from kaiba.providers import rugcheck

    if token.chain is not Chain.SOL:
        return None, {}
    summary = rugcheck.report_summary(token.address, conn=conn)
    report = rugcheck.report(token.address, conn=conn)
    props: dict[str, Any] = {}
    raw: dict[str, Any] = {}
    if summary.ok:
        raw["summary"] = summary.data
        props.update(rugcheck.normalize_summary(summary.data))
    if report.ok:
        raw["report"] = report.data
        props.update(rugcheck.normalize_report(report.data))
        graph = rugcheck.insider_graph(token.address, conn=conn)
        if graph.ok:
            raw["insiders"] = graph.data
            props.update(rugcheck.normalize_insider_graph(graph.data, props.get("total_supply")))
    return (props if raw else None), raw


def probe_token(token: ReferenceToken, *, conn: Any = None, record_dir: Path | None = None) -> TokenReport:
    """Read chain truth, run both providers live, and reconcile. Never raises on an outage."""
    truth = chain_truth(token.address, token.chain, conn=conn)
    props: dict[str, dict[str, Any]] = {}
    raw: dict[str, Any] = {}

    try:
        goplus_props, goplus_raw = _goplus_props(token, conn)
    except Exception as exc:  # noqa: BLE001 - a provider failing is the thing we are measuring
        log.warning("goplus failed on %s: %s", token.label, exc)
        goplus_props, goplus_raw = None, None
    if goplus_props is not None:
        props["goplus"] = goplus_props
        raw["goplus"] = goplus_raw

    try:
        rugcheck_props, rugcheck_raw = _rugcheck_props(token, conn)
    except Exception as exc:  # noqa: BLE001
        log.warning("rugcheck failed on %s: %s", token.label, exc)
        rugcheck_props, rugcheck_raw = None, {}
    if rugcheck_props is not None:
        props["rugcheck"] = rugcheck_props
        raw["rugcheck"] = rugcheck_raw

    dev_pct = None
    if token.chain is Chain.SOL:
        creator = (props.get("rugcheck") or {}).get("creator") or (props.get("goplus") or {}).get("creator")
        dev_pct = creator_supply_pct(token.address, creator, truth.supply, conn=conn)

    report = compare_props(token, truth, props, chain_dev_pct=dev_pct)
    if not truth.ok:
        report.notes.append(f"no chain truth: {truth.receipt.note or truth.receipt.basis.value}")
    for provider in PROVIDERS:
        if provider == "rugcheck" and token.chain is not Chain.SOL:
            continue
        if provider not in props:
            report.notes.append(f"{provider} returned nothing")

    if record_dir is not None:
        _record(record_dir, token, truth, raw, dev_pct)
    return report


def _record(
    record_dir: Path, token: ReferenceToken, truth: ChainTruth, raw: dict[str, Any], dev_pct: Decimal | None
) -> None:
    """Write the raw responses so the same reconciliation can be re-run offline.

    Nothing here is scrubbed: these are public facts about public tokens, and a fixture with
    the interesting parts removed would not be evidence of anything.
    """
    record_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "note": "Live recording, not hand-built. goplus is the per-address entry already "
        "unwrapped from GoPlus's result map; rugcheck holds one key per route.",
        "recorded_at_ms": now_ms(),
        "token": {
            "label": token.label,
            "address": token.address,
            "chain": token.chain.value,
            "why": token.why,
        },
        "chain": truth.raw if truth.ok else None,
        "chain_dev_pct": str(dev_pct) if dev_pct is not None else None,
        "goplus": raw.get("goplus"),
        "rugcheck": raw.get("rugcheck") or None,
    }
    path = record_dir / f"{token.slug}.json"
    path.write_text(json.dumps(payload, indent=1, default=str, sort_keys=False), encoding="utf-8")


def dossier_preview(report: TokenReport) -> Any:
    """The dossier the merge would build from exactly these provider answers.

    A property-level table says whether a mapping is right; this says whether being right
    changes the verdict. They are different questions, and the second is the one the
    operator is actually asking — a mapping can be wrong on a field nothing reads, and a
    mapping can be right while the rule on top of it refuses a token anyway.
    """
    claims: list[dyor.Claim] = []
    for provider, props in report.props.items():
        receipt = Receipt(
            provider=provider, endpoint="verify.replay", basis=EvidenceBasis.PROVIDER_REPORTED
        )
        claims.extend(
            dyor.Claim(prop=key, provider=provider, value=value, receipt=receipt)
            for key, value in props.items()
            if value is not None
            and key in (set(dyor.BOOL_PROPERTIES) | set(dyor.NUM_PROPERTIES) | set(dyor.TEXT_PROPERTIES))
        )
    resolution = dyor.resolve(claims)
    resolution.providers_ok = set(report.props)
    return dyor.build_dossier(report.token.address, report.token.chain, resolution)


@dataclass
class Reconciliation:
    reports: list[TokenReport] = field(default_factory=list)
    source: Literal["live", "replay"] = "live"

    def table(self) -> list[dict[str, Any]]:
        """Per property per provider: how often it agreed with the chain, and how often not."""
        rows: list[dict[str, Any]] = []
        for prop in TABLE_PROPERTIES:
            for provider in PROVIDERS:
                counts = {v: 0 for v in Verdict}
                for report in self.reports:
                    for check in report.checks:
                        if check.prop == prop and check.provider == provider:
                            counts[check.verdict] += 1
                total = sum(counts.values())
                if not total:
                    continue
                rows.append(
                    {
                        "property": prop,
                        "provider": provider,
                        "agree": counts[Verdict.AGREE],
                        "disagree": counts[Verdict.DISAGREE],
                        "absent": counts[Verdict.ABSENT],
                        "no_truth": counts[Verdict.NO_TRUTH],
                        "answered": total - counts[Verdict.ABSENT],
                        "blocker": prop in _BLOCKER_PROPERTIES,
                    }
                )
        return rows

    @property
    def disagreements(self) -> list[tuple[str, PropertyCheck]]:
        return [(r.token.label, c) for r in self.reports for c in r.disagreements]

    @property
    def cross_conflicts(self) -> list[tuple[str, str]]:
        return [(r.token.label, p) for r in self.reports for p in r.cross_conflicts]


#: Properties a blocker in :data:`kaiba.intelligence.dyor.RULES` reads. A disagreement here is
#: not a data-quality note; it is the scanner refusing or blessing a position on a wrong fact.
_BLOCKER_PROPERTIES: frozenset[str] = frozenset(
    {
        "can_sell",
        "mint_authority_revoked",
        "freeze_authority_revoked",
        "balance_mutable",
        "transfer_hook",
        "tax_modifiable",
        "rugged",
        "dev_pct",
        "cluster_pct",
    }
)


def reconcile(
    tokens: Sequence[ReferenceToken] | None = None,
    *,
    conn: Any = None,
    record_dir: Path | None = None,
    settle_s: float = SETTLE_S,
) -> Reconciliation:
    """Run the whole reference set live against both providers."""
    out = Reconciliation(source="live")
    for token in tokens if tokens is not None else REFERENCE:
        out.reports.append(probe_token(token, conn=conn, record_dir=record_dir))
        if settle_s:
            time.sleep(settle_s)
    return out


# --------------------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------------------


def load_recordings(directory: Path | None = None) -> list[dict[str, Any]]:
    source = directory or RECORDING_DIR
    if not source.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(source.glob("*.json")):
        if path.name.startswith("_"):
            continue  # reserved for the auth finding and anything else that is not a token
        try:
            out.append(json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal))
        except (OSError, ValueError) as exc:
            log.warning("could not read recording %s: %s", path.name, exc)
    return out


def replay_one(recording: dict[str, Any]) -> TokenReport:
    """Re-run the reconciliation over one recorded response set, with no network at all."""
    from kaiba.providers import goplus, rugcheck

    meta = recording.get("token") or {}
    chain = Chain(str(meta.get("chain") or "sol"))
    known = next((t.known for t in REFERENCE if t.address == meta.get("address")), {})
    token = ReferenceToken(
        label=str(meta.get("label") or "?"),
        address=str(meta.get("address") or ""),
        chain=chain,
        why=str(meta.get("why") or ""),
        known=dict(known),
    )

    raw_chain = recording.get("chain")
    if isinstance(raw_chain, dict) and chain is Chain.SOL:
        truth = _sol_truth_from_value(
            token.address,
            raw_chain,
            Receipt(provider="rpc", endpoint="rpc.getAccountInfo", basis=EvidenceBasis.CACHED),
        )
    elif isinstance(raw_chain, dict):
        owner = raw_chain.get("owner")
        body = str(owner or "")[2:]
        truth = ChainTruth(
            token.address,
            chain,
            True,
            Receipt(provider="rpc", endpoint="rpc.eth_call", basis=EvidenceBasis.CACHED),
            owner_address="0x" + body[-40:] if len(body) >= 40 else None,
            decimals=_hex_int(raw_chain.get("decimals")),
            supply=_hex_int(raw_chain.get("totalSupply")),
            raw=raw_chain,
        )
    else:
        truth = ChainTruth(token.address, chain, False, _dead("rpc", "recording has no chain truth"))

    props: dict[str, dict[str, Any]] = {}
    if recording.get("goplus") is not None:
        props["goplus"] = goplus.normalize_security(recording["goplus"], chain)
    rug = recording.get("rugcheck")
    if isinstance(rug, dict) and rug:
        merged: dict[str, Any] = {}
        if rug.get("summary") is not None:
            merged.update(rugcheck.normalize_summary(rug["summary"]))
        if rug.get("report") is not None:
            merged.update(rugcheck.normalize_report(rug["report"]))
        if rug.get("insiders") is not None:
            merged.update(rugcheck.normalize_insider_graph(rug["insiders"], merged.get("total_supply")))
        props["rugcheck"] = merged

    return compare_props(token, truth, props, chain_dev_pct=_dec(recording.get("chain_dev_pct")))


def replay(directory: Path | None = None) -> Reconciliation:
    out = Reconciliation(source="replay")
    for recording in load_recordings(directory):
        out.reports.append(replay_one(recording))
    return out


# --------------------------------------------------------------------------------------
# GoPlus authentication
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthFinding:
    """What we could actually establish about GoPlus's credentialed path."""

    credentials_configured: bool
    keyless_ok: bool
    keyless_code: Any = None
    signin_code: Any = None
    signin_message: str | None = None
    token_obtained: bool = False
    signed_call_ok: bool | None = None
    bad_token_code: Any = None
    notes: list[str] = field(default_factory=list)

    def render(self) -> list[str]:
        return list(self.notes)


#: GoPlus signals failure inside an HTTP 200 body. These are the codes that matter here,
#: read from docs.gopluslabs.io/reference/api-status-code on 2026-09-20.
GOPLUS_CODES: dict[int, str] = {
    1: "complete data prepared",
    2: "partial data; retry in ~15s",
    4010: "app_key not exist",
    4011: "signature expired (a request may not be replayed)",
    4012: "wrong signature",
    4023: "access token not found",
    4029: "request limit reached",
    5000: "system error",
}


def verify_goplus_auth(*, conn: Any = None, probe_address: str | None = None) -> AuthFinding:
    """Establish what the keyless and credentialed paths actually do.

    Deliberately does *not* hunt for the rate-limit ceiling by hammering the endpoint: code
    4029 is a real ban risk and the cost of finding the exact number is far higher than the
    value of knowing it. What is measured is behavioural — does keyless work, does a bad
    credential degrade or fail, and does the documented signature scheme reach the server.
    """
    from kaiba.providers import goplus

    address = probe_address or REFERENCE[0].address
    notes: list[str] = []

    keyless = get_json(
        goplus.PROVIDER,
        "token.security_sol",
        f"{goplus.BASE}/solana/token_security",
        params={"contract_addresses": address},
        priority=Priority.RESEARCH,
        ttl_s=0,
        wait_for_slot_s=SLOT_WAIT_S,
        conn=conn,
    )
    keyless_body = keyless.data if isinstance(keyless.data, dict) else {}
    keyless_code = keyless_body.get("code")
    keyless_ok = keyless.ok and keyless_code == 1
    notes.append(
        f"keyless: {'works' if keyless_ok else 'FAILED'} (code={keyless_code} "
        f"{GOPLUS_CODES.get(keyless_code, '?') if isinstance(keyless_code, int) else ''})"
    )

    # A deliberately invalid bearer, to learn whether a stale or wrong credential degrades to
    # the keyless tier or kills the call. This is the failure that would otherwise hide.
    bad = get_json(
        goplus.PROVIDER,
        "token.security_sol",
        f"{goplus.BASE}/solana/token_security",
        params={"contract_addresses": address},
        headers={"Authorization": "kaiba-verify-invalid-token"},
        priority=Priority.RESEARCH,
        ttl_s=0,
        wait_for_slot_s=SLOT_WAIT_S,
        conn=conn,
    )
    bad_body = bad.data if isinstance(bad.data, dict) else {}
    bad_code = bad_body.get("code")
    if bad_code == 1:
        notes.append("an invalid Authorization header is ignored: the call still answers keyless")
    else:
        notes.append(
            f"an invalid Authorization header FAILS the call (code={bad_code} "
            f"{GOPLUS_CODES.get(bad_code, '?') if isinstance(bad_code, int) else ''}); a stale or "
            "wrong credential therefore blinds the scanner rather than degrading it"
        )

    creds_configured = False
    signin_code: Any = None
    signin_message: str | None = None
    token_obtained = False
    signed_ok: bool | None = None
    try:
        from kaiba.core.config import get_settings

        settings = get_settings()
        creds_configured = bool((settings.goplus_app_key or "").strip() and (settings.goplus_app_secret or "").strip())
    except Exception as exc:  # noqa: BLE001
        notes.append(f"could not read settings: {exc}")

    if creds_configured:
        goplus.reset_credentials_cache()
        token = goplus.access_token(conn=conn, force=True)
        token_obtained = bool(token)
        notes.append(
            "credentialed sign-in " + ("succeeded" if token_obtained else "FAILED with configured credentials")
        )
        if token_obtained:
            signed = goplus.token_security(address, Chain.SOL, conn=conn)
            signed_ok = signed.ok
            notes.append(f"signed security call {'works' if signed_ok else 'FAILED: ' + (signed.receipt.note or '')}")
    else:
        # No credentials on this machine. Probe the endpoint's contract anyway: a body the
        # server rejects for the *key* rather than the *shape* proves the request shape and
        # the signature scheme in kaiba.providers.goplus.access_token reach the right handler.
        import hashlib

        stamp = int(time.time())
        app_key = "kaiba-verify-probe"
        sign = hashlib.sha1(f"{app_key}{stamp}kaiba-verify-probe-secret".encode()).hexdigest()  # noqa: S324
        probe = post_json(
            goplus.PROVIDER,
            "auth.token",
            f"{goplus.BASE}/token",
            json_body={"app_key": app_key, "time": stamp, "sign": sign},
            priority=Priority.RESEARCH,
            ttl_s=0,
            wait_for_slot_s=SLOT_WAIT_S,
            conn=conn,
        )
        probe_body = probe.data if isinstance(probe.data, dict) else {}
        signin_code = probe_body.get("code")
        signin_message = probe_body.get("message")
        meaning = GOPLUS_CODES.get(signin_code, "?") if isinstance(signin_code, int) else "?"
        notes.append(f"no credentials configured; sign-in probe returned code={signin_code} ({meaning})")
        if signin_code == 4010:
            notes.append(
                "code 4010 is 'app_key not exist', so the documented body {app_key,time,sign} "
                "and the sha1(app_key+time+app_secret) scheme reached the key check. The "
                "credentialed path is UNVERIFIED beyond that: without a real key we cannot "
                "confirm the Authorization header format or the raised rate limit."
            )
        elif signin_code == 5000:
            notes.append("code 5000 is a system error, i.e. the request shape was rejected before the key check")

    return AuthFinding(
        credentials_configured=creds_configured,
        keyless_ok=keyless_ok,
        keyless_code=keyless_code,
        signin_code=signin_code,
        signin_message=str(signin_message) if signin_message is not None else None,
        token_obtained=token_obtained,
        signed_call_ok=signed_ok,
        bad_token_code=bad_code,
        notes=notes,
    )


__all__ = [
    "EVM_BLACKHOLES",
    "GOPLUS_CODES",
    "PROVIDERS",
    "REFERENCE",
    "RECORDING_DIR",
    "TABLE_PROPERTIES",
    "TOKENKEG",
    "TOKEN_2022",
    "AuthFinding",
    "ChainTruth",
    "PropertyCheck",
    "Reconciliation",
    "ReferenceToken",
    "TokenReport",
    "Verdict",
    "chain_truth",
    "dossier_preview",
    "compare_props",
    "creator_supply_pct",
    "load_recordings",
    "probe_token",
    "reconcile",
    "replay",
    "replay_one",
    "verify_goplus_auth",
]
