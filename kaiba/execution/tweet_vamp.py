"""Prepare volume-backed tweet relaunches for the existing launcher.

Codex P8-TL-VAMP, 2026-10-07. Metadata is data, never instructions. No second
wallet, exit loop, market-making loop or budget. The lead runner owns live send;
this additive module does not bypass its unresolved admission/holder controls.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from kaiba.core.db import tx
from kaiba.core.schemas import Chain, EvidenceBasis, Receipt
from kaiba.execution import tweet_launch as tl
from kaiba.ingest.x_stream import XPost

APPROVED_CAPS = {Chain.SOL: (Decimal("1.5"), Decimal("6")),
                 Chain.BSC: (Decimal("0.5"), Decimal("1")),
                 Chain.ROBINHOOD: (Decimal("0.1"), Decimal("0.3"))}


@dataclass(frozen=True)
class Candidate:
    chain: Chain
    address: str
    name: str
    symbol: str
    image_url: str
    created_ms: int
    volume_5m_usd: Decimal
    swaps_5m: int
    receipt: Receipt


def from_gmgn(chain: Chain, address: str, info: dict, receipt: Receipt) -> Candidate:
    """Documented token.info fields only; missing/non-finite data is unavailable."""
    if not isinstance(info, dict) or not isinstance(info.get("price"), dict):
        raise ValueError("token_info_unavailable")
    same = info.get("address") == address if chain is Chain.SOL else str(info.get("address", "")).lower() == address.lower()
    if not same or not address:
        raise ValueError("source_address_mismatch")
    stats = info["price"]
    try:
        volume = Decimal(str(stats["volume_5m"]))
        swaps = stats["swaps_5m"]
        created = Decimal(str(info["creation_timestamp"])) * 1000
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("volume_or_creation_unavailable") from exc
    if not volume.is_finite() or volume < 0 or not created.is_finite() or created != int(created):
        raise ValueError("invalid_volume_or_creation")
    if isinstance(swaps, bool) or not isinstance(swaps, int) or swaps < 0:
        raise ValueError("invalid_swap_count")
    return Candidate(chain, address, str(info.get("name") or ""), str(info.get("symbol") or ""),
                     str(info.get("logo") or ""), int(created), volume, swaps, receipt)


def assess(post: XPost, candidate: Candidate, cfg: tl.Config, *, now_ms: int,
           min_volume_usd: Decimal = Decimal(0), owned_tokens: frozenset[str] = frozenset()) -> str | None:
    """Volume is provider reported; a positive amount does not prove organic demand."""
    if not min_volume_usd.is_finite() or min_volume_usd < 0:
        return "invalid_volume_threshold"
    if candidate.chain not in cfg.accounts.get(post.author, ()):
        return "unwatched_author_or_chain"
    route = cfg.chains.get(candidate.chain)
    if route is None or candidate.chain not in APPROVED_CAPS:
        return "route_unavailable"
    single, daily = APPROVED_CAPS[candidate.chain]
    configured_daily = cfg.daily_native_cap.get(candidate.chain)
    if not 0 < route.max_buy_native <= single or configured_daily is None or not 0 < configured_daily <= daily:
        return "outside_authorized_launch_limits"
    if cfg.dev_buy_supply_pct != 5:
        return "five_percent_target_required"
    if post.created_ms is None or not 0 <= now_ms-post.created_ms <= cfg.max_tweet_age_s*1000:
        return "invalid_or_stale_tweet"
    if not post.text or post.kind not in cfg.account_kinds.get(post.author, cfg.post_kinds):
        return "post_kind_or_empty"
    receipt = candidate.receipt
    if receipt.provider != "gmgn" or receipt.endpoint != "token.info" or receipt.basis is not EvidenceBasis.PROVIDER_REPORTED:
        return "source_evidence_unavailable"
    if not 0 <= now_ms-receipt.observed_at_ms <= cfg.max_tweet_age_s*1000:
        return "stale_volume"
    if not post.created_ms <= candidate.created_ms <= now_ms:
        return "source_not_a_fresh_tweet_launch"
    if not candidate.volume_5m_usd.is_finite():
        return "invalid_volume"
    if candidate.volume_5m_usd <= 0 or candidate.volume_5m_usd < min_volume_usd or candidate.swaps_5m <= 0:
        return "no_qualifying_volume"
    if any(candidate.address == a if candidate.chain is Chain.SOL else candidate.address.lower() == a.lower()
           for a in owned_tokens):
        return "source_is_ours"
    if not candidate.name or len(candidate.name) > 32 or any(ord(c) < 32 for c in candidate.name):
        return "unsupported_source_name"
    if not tl._ok_symbol(candidate.symbol) or candidate.symbol.upper() in tl.MAJORS:
        return "unsupported_source_symbol"
    image = urlsplit(candidate.image_url)
    if image.scheme != "https" or not image.hostname or image.username or image.password:
        return "source_image_unavailable"
    # A literal contract or both exact deterministic identity fields establish context.
    # Identity matching is an inference, recorded below, never a claim of affiliation.
    literal = re.search(r"(?<![A-Za-z0-9])" + re.escape(candidate.address) + r"(?![A-Za-z0-9])",
                        post.text, flags=0 if candidate.chain is Chain.SOL else re.I)
    identity = tl.derive_identity(post.text)
    if not literal and not (identity and identity.name.casefold() == candidate.name.casefold()
                            and identity.symbol.casefold() == candidate.symbol.casefold()):
        return "tweet_context_unmatched"
    return None


def plan_vamp(post: XPost, candidate: Candidate, cfg: tl.Config, *, now_ms: int,
              min_volume_usd: Decimal = Decimal(0), owned_tokens: frozenset[str] = frozenset()) -> tl.Plan:
    reason = assess(post, candidate, cfg, now_ms=now_ms, min_volume_usd=min_volume_usd, owned_tokens=owned_tokens)
    if reason:
        raise ValueError(reason)
    ai = SimpleNamespace(name=candidate.name, symbol=candidate.symbol, model="vamp-metadata",
                         launch=True, reason="reported_volume", latency_ms=0)
    logo = SimpleNamespace(source="source_token", url=candidate.image_url, b64=None, note="")
    plan = next(p for p in tl.plan_post(post, cfg, now_ms=now_ms, ai=ai, logo=logo) if p.chain is candidate.chain)
    # Use the source coin's image even if the tweet contains a different picture.
    plan.image_source, plan.image_url, plan.image_b64 = "source_token", candidate.image_url, None
    plan.reasons.extend([f"vamp_source:{candidate.address}",
                         f"provider_reported_volume_5m_usd:{candidate.volume_5m_usd}",
                         f"source_swaps_5m:{candidate.swaps_5m}", "tweet_context:contract_or_exact_identity"])
    plan.argv = tl.build_argv(plan, post, cfg, "<chain-wallet>") if plan.verdict == "launch" else None
    return plan


def record_vamp(conn: sqlite3.Connection, post: XPost, plan: tl.Plan) -> bool:
    """Shared intent: insert once or replace only an untouched skip, never a failed send.

    The same tweet+chain primary key arbitrates normal/vamp and competing source
    tokens. No transaction surrounds network calls. Returns whether we claimed it.
    """
    if plan.launch_id != f"tl:{post.tweet_id}:{plan.chain.value}" or plan.verdict != "launch":
        return False
    tl.record_post(conn, post)
    with tx(conn):
        before = conn.total_changes
        tl.record_plan(conn, plan)
        if conn.total_changes > before:
            return True
        cur = conn.execute(
            "UPDATE tweet_launches SET verdict='launch',reasons_json=?,score=?,name=?,symbol=?,"
            "image_source=?,image_url=?,buy_amt_native=?,supply_pct=?,buy_basis=?,argv_json=?,"
            "mode=?,decided_ms=?,decide_latency_ms=?,state=? WHERE launch_id=? AND verdict='skip' "
            "AND state IS NULL AND provider_order_id IS NULL AND token IS NULL AND order_id IS NULL",
            (json.dumps(plan.reasons), plan.score, plan.name, plan.symbol, plan.image_source, plan.image_url,
             str(plan.buy_amt_native), plan.supply_pct, plan.buy_basis,
             json.dumps(tl.recordable_argv(plan.argv)), plan.mode, plan.decided_ms, plan.decide_latency_ms,
             "planned" if plan.mode == "live" else None, plan.launch_id))
    return cur.rowcount == 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline vamp preview; no provider calls or live writes")
    parser.add_argument("--input", type=Path, required=True, help="JSON with post, chain, address, info, receipt")
    parser.add_argument("--config", type=Path, default=tl.CONFIG_PATH)
    parser.add_argument("--min-volume-usd", type=Decimal, default=Decimal(0))
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    post = XPost(**payload["post"])
    candidate = from_gmgn(Chain(payload["chain"]), payload["address"], payload["info"], Receipt(**payload["receipt"]))
    # Preview always shadow; user-provided receipts are test data, not authentication.
    from dataclasses import replace
    cfg = replace(tl.load_config(args.config), mode="shadow")
    try:
        result = plan_vamp(post, candidate, cfg, now_ms=payload["now_ms"], min_volume_usd=args.min_volume_usd)
    except ValueError as exc:
        print(json.dumps({"verdict": "skip", "reason": str(exc)}))
        return 0
    print(json.dumps(asdict(result), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
