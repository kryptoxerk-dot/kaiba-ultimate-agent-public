"""RugCheck — Solana-only token report, summary and the insider graph.

Three routes, deliberately kept separate because they cost different things and answer
different questions:

* ``tokens/{mint}/report`` — the full body. This is the only one that states authorities
  as *facts* (``token.mintAuthority`` is null or it is an address), so it is the only one
  allowed to clear a token.
* ``tokens/{mint}/report/summary`` — a cheap risk list plus a normalised score. It can only
  ever assert that something is **wrong**. The absence of "Mint Authority still enabled"
  from a summary is not evidence that the mint authority is revoked, and this module never
  turns a missing risk into a reassuring ``True``. That inference is exactly the failure
  mode the DYOR merge exists to prevent.
* ``tokens/{mint}/insiders/graph`` — clustered wallets and how much supply each cluster
  holds. Converting that into a percentage needs the supply, which lives in the report, so
  :func:`normalize_insider_graph` takes it as an argument and returns nothing without it.

``RUGCHECK_JWT`` is optional: the public tier answers these routes unauthenticated.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, EvidenceBasis, Receipt
from kaiba.providers._http import Fetched, get_json

log = logging.getLogger(__name__)

PROVIDER = "rugcheck"
BASE = "https://api.rugcheck.xyz/v1"

REPORT_TTL_S = 300.0
SUMMARY_TTL_S = 180.0
GRAPH_TTL_S = 900.0
STALE_GRACE_S = 900.0

#: The canonical burn destination on Solana. A balance parked here is not a holder.
INCINERATOR = "1nc1nerator11111111111111111111111111111111"

#: The classic SPL Token program. A mint owned by it cannot carry a Token-2022 extension,
#: so a transfer hook or a transfer fee is impossible by construction — one of the few
#: places where an absence really is a fact.
TOKENKEG = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

#: Substrings of RugCheck risk names we act on, mapped to the property they falsify.
#: Only *negative* assertions appear here, by design — see the module docstring.
#:
#: ``("rugged", "rugged", True)`` used to be in this table and is deliberately gone. RugCheck
#: names one of its risks **"Creator history of rugged tokens"** — a statement about the
#: developer's *other* launches — and the substring "rugged" matched it, setting
#: ``rugged=True``, which is the dossier's ``already_rugged`` BLOCKER ("there is no thesis
#: left, only an exit queue"). Live proof, 2026-09-20:
#: ``BrYrSE7Y5BJPy1AstdCQnk4cXEVd3fGTkyLBkCKVpump`` returns ``"rugged": false`` in the same
#: report that lists that risk name. Measured blast radius on this machine's database:
#: **49 of 59 quarantined dossiers carry ``rug_history``, and for 45 of them it is the only
#: blocker.** Three quarters of everything the exclusion layer refused, refused on a
#: substring collision. The creator's history is real evidence, so it is kept below — routed
#: to ``creator_rug_count``, which the dossier prices as a warning, which is what it is.
_RISK_MARKERS: tuple[tuple[str, str, bool], ...] = (
    ("mint authority", "mint_authority_revoked", False),
    ("freeze authority", "freeze_authority_revoked", False),
    ("honeypot", "can_sell", False),
    ("mutable metadata", "metadata_mutable", True),
    ("metadata is mutable", "metadata_mutable", True),
)


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return None if d.is_nan() else d


def _unavailable(endpoint: str, note: str) -> Fetched:
    return Fetched(
        None,
        Receipt(
            provider=PROVIDER, endpoint=endpoint, basis=EvidenceBasis.UNAVAILABLE, note=note[:300]
        ),
    )


def _headers() -> dict[str, str]:
    """Bearer the operator's JWT when one is configured; the public tier needs none."""
    from kaiba.core.config import get_settings

    jwt = (get_settings().rugcheck_jwt or "").strip()
    return {"Authorization": f"Bearer {jwt}"} if jwt else {}


#: Longest we will ever stall to respect the limiter's floor before giving up and letting
#: the request be refused normally.
MAX_PACE_S = 5.0
#: The limiter compares wall-clock milliseconds with a strict ``<``; sleeping for exactly
#: the interval lands on the boundary and loses the race about half the time.
PACE_MARGIN_S = 0.05
_last_request_s: float = 0.0


def _pace() -> None:
    """Space our own consecutive requests by the limiter's minimum interval.

    ``_http.request_json`` does not block on a limiter refusal — by design it returns an
    UNAVAILABLE receipt — so a module that issues three routes in a row rate-limits itself:
    with the shipped 500 ms floor, ``report`` and ``insiders/graph`` would come back
    unavailable on *every* scan, and the report is the only route that can clear a token.
    The interval is read from the limiter's own config rather than invented here. This
    really belongs in ``_http`` next to the retry logic; it lives here because that file is
    owned elsewhere.
    """
    global _last_request_s
    try:
        from kaiba.core.limiter import limits_for

        interval = limits_for(PROVIDER).min_interval_ms / 1000.0 + PACE_MARGIN_S
    except Exception as exc:  # noqa: BLE001 - pacing must never be the thing that fails
        log.debug("rugcheck could not read its limiter interval: %s", exc)
        interval = 0.5 + PACE_MARGIN_S
    wait = _last_request_s + interval - time.monotonic()
    if wait > 0:
        time.sleep(min(wait, MAX_PACE_S))
    _last_request_s = time.monotonic()


def _fetch(mint: str, chain: Chain, path: str, endpoint: str, ttl_s: float, conn: Any) -> Fetched:
    if chain is not Chain.SOL:
        return _unavailable(endpoint, f"rugcheck covers Solana only, not {chain.value}")
    address = mint.strip()
    if not address:
        return _unavailable(endpoint, "empty mint")
    _pace()
    return get_json(
        PROVIDER,
        endpoint,
        f"{BASE}/tokens/{address}/{path}",
        headers=_headers(),
        priority=Priority.RESEARCH,
        ttl_s=ttl_s,
        stale_grace_s=STALE_GRACE_S,
        cache_key=f"rugcheck:{endpoint}:{address}",
        retries=2,
        conn=conn,
    )


def report(mint: str, chain: Chain = Chain.SOL, *, conn: Any = None) -> Fetched:
    """Full report. The only route allowed to establish that an authority is revoked."""
    return _fetch(mint, chain, "report", "token.report", REPORT_TTL_S, conn)


def report_summary(mint: str, chain: Chain = Chain.SOL, *, conn: Any = None) -> Fetched:
    """Cheap risk list plus ``score_normalised`` (higher means riskier)."""
    return _fetch(mint, chain, "report/summary", "token.summary", SUMMARY_TTL_S, conn)


def insider_graph(mint: str, chain: Chain = Chain.SOL, *, conn: Any = None) -> Fetched:
    """Clustered insider networks and the supply each one holds."""
    return _fetch(mint, chain, "insiders/graph", "token.insiders", GRAPH_TTL_S, conn)


# --------------------------------------------------------------------------------------
# normalisation into the shared DYOR vocabulary
# --------------------------------------------------------------------------------------


def _risk_names(payload: Any) -> list[str]:
    risks = payload.get("risks") if isinstance(payload, dict) else None
    if not isinstance(risks, list):
        return []
    out: list[str] = []
    for item in risks:
        if isinstance(item, dict):
            name = item.get("name") or item.get("description")
            if name:
                out.append(str(name).lower())
        elif isinstance(item, str):
            out.append(item.lower())
    return out


def _about_the_creator(name: str) -> bool:
    """Does this risk name describe the developer's other tokens rather than this one?"""
    return "rugged" in name and ("creator" in name or "history" in name)


def _apply_risk_markers(names: list[str], out: dict[str, Any]) -> None:
    for marker, key, value in _RISK_MARKERS:
        if any(marker in name for name in names):
            out[key] = value

    # Split the two claims RugCheck used to have collapsed into one. "This token rugged" is
    # a blocker; "the person who deployed it has rugged before" raises the base rate and is
    # a warning. The count is a floor, not a tally: RugCheck names the condition without
    # saying how many, and 1 is enough to trip the creator_rug_history rule.
    if any(_about_the_creator(name) for name in names):
        out["creator_rug_count"] = max(_dec(out.get("creator_rug_count")) or Decimal(0), Decimal(1))
    if any("rugged" in name and not _about_the_creator(name) for name in names):
        out["rugged"] = True


def _apply_token_program(payload: dict[str, Any], out: dict[str, Any]) -> None:
    """Record whether the mint is Token-2022, which is what makes extensions possible."""
    program = payload.get("tokenProgram")
    if isinstance(program, str) and program:
        out["token_2022"] = program != TOKENKEG


def _market_accounts(markets: Any) -> set[str]:
    """Pool-side accounts, so LP balances are not counted as holder concentration."""
    found: set[str] = {INCINERATOR}
    if not isinstance(markets, list):
        return found
    for market in markets:
        if not isinstance(market, dict):
            continue
        for key in ("liquidityA", "liquidityB", "lpMint", "pubkey", "mintLP"):
            value = market.get(key)
            if isinstance(value, str) and value:
                found.add(value)
    return found


def _top_holders(payload: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    """``(top10_pct, insider_pct)`` from ``topHolders``, both as 0–100 percentages."""
    rows = payload.get("topHolders")
    if not isinstance(rows, list) or not rows:
        return None, None
    excluded = _market_accounts(payload.get("markets"))
    ranked: list[Decimal] = []
    insider = Decimal(0)
    saw_insider_flag = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        pct = _dec(row.get("pct"))
        if pct is None:
            continue
        owner = str(row.get("owner") or "")
        address = str(row.get("address") or "")
        if owner in excluded or address in excluded:
            continue
        ranked.append(pct)
        if "insider" in row:
            saw_insider_flag = True
            if row.get("insider"):
                insider += pct
    if not ranked:
        return None, None
    ranked.sort(reverse=True)
    top10 = sum(ranked[:10], Decimal(0))
    return top10, (insider if saw_insider_flag else None)


def _lp_locked_pct(markets: Any) -> Decimal | None:
    """Liquidity-weighted ``lpLockedPct`` across markets.

    Weighting matters: a token with a burned dust pool and a live million-dollar pool is
    not 50% locked, and taking the best market would let a decoy pool launder the number.
    """
    if not isinstance(markets, list) or not markets:
        return None
    weighted = Decimal(0)
    weight_total = Decimal(0)
    fallback: list[Decimal] = []
    for market in markets:
        if not isinstance(market, dict):
            continue
        lp = market.get("lp")
        if not isinstance(lp, dict):
            continue
        pct = _dec(lp.get("lpLockedPct"))
        if pct is None:
            continue
        fallback.append(pct)
        weight = (_dec(lp.get("baseUSD")) or Decimal(0)) + (_dec(lp.get("quoteUSD")) or Decimal(0))
        if weight > 0:
            weighted += pct * weight
            weight_total += weight
    if weight_total > 0:
        return weighted / weight_total
    if fallback:
        return max(fallback)
    return None


def _extension_fee_bps(extensions: Any) -> Decimal | None:
    """Transfer fee straight out of the mint's Token-2022 extension, in basis points.

    RugCheck copies the on-chain ``transferFeeConfig`` into the report verbatim, so this is
    chain truth carried by the provider rather than the provider's own summary of it.
    """
    if not isinstance(extensions, dict):
        return None
    config = extensions.get("transferFeeConfig")
    if not isinstance(config, dict):
        return None
    for key in ("newerTransferFee", "olderTransferFee"):
        block = config.get(key)
        if isinstance(block, dict):
            bps = _dec(block.get("transferFeeBasisPoints"))
            if bps is not None:
                return bps
    return None


def normalize_report(payload: Any) -> dict[str, Any]:
    """Translate a full RugCheck report into the shared DYOR property vocabulary."""
    if not isinstance(payload, dict):
        return {}
    try:
        return _normalize_report(payload)
    except Exception as exc:  # noqa: BLE001 - a layout change degrades to "unknown"
        log.warning("rugcheck report did not parse (%s); treating every field as unknown", exc)
        return {}


def _normalize_report(p: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    token = p.get("token") if isinstance(p.get("token"), dict) else {}

    # ``mintAuthority``/``freezeAuthority`` are facts here: null means genuinely revoked.
    # They are only read when the key is present, so a truncated body stays unknown.
    for src, key in (("mintAuthority", "mint_authority_revoked"), ("freezeAuthority", "freeze_authority_revoked")):
        holder = token if src in token else (p if src in p else None)
        if holder is not None:
            value = holder.get(src)
            out[key] = value in (None, "", "null")

    meta = p.get("tokenMeta") if isinstance(p.get("tokenMeta"), dict) else {}
    if "mutable" in meta:
        out["metadata_mutable"] = bool(meta.get("mutable"))
    if meta.get("symbol"):
        out["symbol"] = meta["symbol"]
    if meta.get("name"):
        out["name"] = meta["name"]

    if "rugged" in p:
        out["rugged"] = bool(p.get("rugged"))

    supply = _dec(token.get("supply"))
    if supply is not None:
        out["total_supply"] = supply

    top10, insider = _top_holders(p)
    if top10 is not None:
        out["top10_pct"] = top10
    if insider is not None:
        out["insider_pct"] = insider

    lp = _lp_locked_pct(p.get("markets"))
    if lp is not None:
        out["lp_burned_pct"] = lp

    # RugCheck answers 0 for market data it has not indexed instead of omitting the field.
    # On USDC, 2026-09-20, the report returns totalMarketLiquidity=0, totalHolders=0 and
    # price=0 — with markets=null and topHolders=null — for a token holding tens of millions
    # of dollars in a single Raydium pool and 8.9M holders. Read literally that is an
    # assertion of zero, and it is the expensive direction: these three resolve to the
    # *lower* of the providers' claims, so the unindexed zero wins, fires low_liquidity on a
    # token with real depth, and raises a provider_conflict against GoPlus's correct figure.
    # An exact zero with no corroborating structure is missing data, and missing data is None.
    # A genuinely dead pool still reports something: 3eB4xJAd...pump reads 2.17e-07.
    liquidity = _dec(p.get("totalMarketLiquidity"))
    if liquidity is not None and (liquidity > 0 or p.get("markets")):
        out["liquidity_usd"] = liquidity
    holders = _dec(p.get("totalHolders"))
    if holders is not None and holders > 0:
        out["holder_count"] = holders
    price = _dec(p.get("price"))
    if price is not None and price > 0:
        out["price_usd"] = price

    # The top-level transferFee block is not reliable. FluxBot
    # (FLUXBmPhT3Fd1EDVFdg46YREqHBeNypn1h4EbnTzWERX) charges 300 bps — its mint's
    # transferFeeConfig says so and GoPlus agrees — yet RugCheck's transferFee read
    # {"pct": 0, "maxAmount": 0} on 2026-09-20 in the very same response whose
    # token_extensions.transferFeeConfig.newerTransferFee.transferFeeBasisPoints was 300.
    # Under-reporting a sell-side fee to zero is the direction that costs money, so the
    # extension block is read first and pct is only a fallback. The unit of pct stays
    # unverified: no sampled token returned a non-zero one.
    fee_bps = _extension_fee_bps(p.get("token_extensions"))
    if fee_bps is None:
        fee = p.get("transferFee")
        if isinstance(fee, dict):
            pct = _dec(fee.get("pct"))
            if pct is not None:
                fee_bps = pct * 100
    if fee_bps is not None:
        out["transfer_fee_bps"] = fee_bps

    creator = p.get("creator")
    if creator:
        out["creator"] = creator
    creator_balance = _dec(p.get("creatorBalance"))
    if creator_balance is not None and supply is not None and supply > 0:
        out["dev_pct"] = creator_balance / supply * 100

    _apply_risk_markers(_risk_names(p), out)
    _apply_token_program(p, out)
    return out


def normalize_summary(payload: Any) -> dict[str, Any]:
    """Risk names only. This never asserts that anything is safe — see the module docstring."""
    if not isinstance(payload, dict):
        return {}
    out: dict[str, Any] = {}
    _apply_risk_markers(_risk_names(payload), out)
    _apply_token_program(payload, out)
    score = _dec(payload.get("score_normalised"))
    if score is not None:
        out["rugcheck_risk_score"] = score
    return out


def normalize_insider_graph(payload: Any, total_supply: Decimal | None) -> dict[str, Any]:
    """Cluster-held supply as a percentage, or ``{}`` when the supply is unknown.

    Without the supply the raw ``tokenAmount`` is meaningless, and guessing decimals to
    make a number appear would be worse than admitting we cannot compute it.
    """
    if not isinstance(payload, list) or total_supply is None or total_supply <= 0:
        return {}
    held = Decimal(0)
    networks = 0
    for network in payload:
        if not isinstance(network, dict):
            continue
        size = _dec(network.get("size")) or Decimal(0)
        amount = _dec(network.get("tokenAmount"))
        if amount is None or size < 2:
            continue  # a "cluster" of one wallet is just a holder
        held += amount
        networks += 1
    if networks == 0:
        return {"cluster_pct": Decimal(0), "cluster_networks": Decimal(0)}
    return {
        "cluster_pct": min(held / total_supply * 100, Decimal(100)),
        "cluster_networks": Decimal(networks),
    }


def security_properties(
    mint: str, chain: Chain = Chain.SOL, *, conn: Any = None, with_graph: bool = True
) -> tuple[dict[str, Any], list[Receipt]]:
    """Fetch report + summary (+ insider graph) and merge them into one property dict.

    The three routes are one provider's opinion, so they are merged here rather than in the
    DYOR layer — a disagreement between RugCheck's own report and its own summary is a
    RugCheck bug, not the cross-provider disagreement the dossier is meant to surface.
    Report facts win over summary risk names for that reason.
    """
    receipts: list[Receipt] = []
    props: dict[str, Any] = {}

    summary = report_summary(mint, chain, conn=conn)
    receipts.append(summary.receipt)
    if summary.ok:
        props.update(normalize_summary(summary.data))

    full = report(mint, chain, conn=conn)
    receipts.append(full.receipt)
    if full.ok:
        props.update(normalize_report(full.data))

    if with_graph and full.ok:
        graph = insider_graph(mint, chain, conn=conn)
        receipts.append(graph.receipt)
        if graph.ok:
            props.update(normalize_insider_graph(graph.data, props.get("total_supply")))
    return props, receipts


__all__ = [
    "BASE",
    "GRAPH_TTL_S",
    "INCINERATOR",
    "PROVIDER",
    "REPORT_TTL_S",
    "SUMMARY_TTL_S",
    "TOKENKEG",
    "insider_graph",
    "normalize_insider_graph",
    "normalize_report",
    "normalize_summary",
    "report",
    "report_summary",
    "security_properties",
]
