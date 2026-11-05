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
  :func:`normalize_insider_graph` takes it as an argument and returns nothing without it,
  and nothing for an empty or unrecognised graph (see :func:`read_insider_graph`).

``RUGCHECK_JWT`` is optional: the public tier answers these routes unauthenticated.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
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
    # "Permanent Control Enabled" ("The token creator can permanently control all tokens") is
    # RugCheck's name for a Token-2022 permanent delegate: an authority that can move or burn
    # any holder's balance, which ``balance_mutable`` already blocks. Unmapped until
    # 2026-10-18; seen on 10 of the ~5,300 cached sol summaries from 10-17/18, every one
    # already quarantined by a live mint and freeze authority. This catches the same delegate
    # on a token whose creator has revoked both.
    ("permanent control", "balance_mutable", True),
)

#: The all-ones system program id: how a parsed Token-2022 extension says "no authority".
_SOL_NULL = "11111111111111111111111111111111"


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


def _apply_extension_traps(extensions: Any, out: dict[str, Any]) -> None:
    """Token-2022 extensions that can take or trap a holder's balance.

    Read from the mint state RugCheck copies into ``token_extensions``, in the on-chain parsed
    shape :func:`_extension_fee_bps` already reads. Only the harmful direction is asserted.
    ``token_extensions`` is null on about 94% of Token-2022 reports (2,614 of 2,816 cached on
    2026-10-17/18), so a null key here never stands in for the other reports. None of the 331
    cached reports with the block carried any of these; this is a guard, not a measured edge.
    """
    if not isinstance(extensions, dict):
        return

    def live(value: Any) -> bool:
        return isinstance(value, str) and value not in ("", _SOL_NULL)

    delegate = extensions.get("permanentDelegate")
    if isinstance(delegate, dict) and live(delegate.get("delegate")):
        out["balance_mutable"] = True
    # A live pause authority can stop every transfer at once, which is what a freeze
    # authority does one account at a time. The EVM side maps ``transfer_pausable`` the same
    # way (dyor's GMGN table), so the existing freeze_authority_live blocker covers both.
    pausable = extensions.get("pausableConfig")
    if isinstance(pausable, dict):
        if pausable.get("paused") is True:
            out["can_sell"] = False
        elif live(pausable.get("authority")):
            out["freeze_authority_revoked"] = False
    if extensions.get("nonTransferable") is True:
        out["can_sell"] = False
    state = extensions.get("defaultAccountState")
    if isinstance(state, dict) and str(state.get("accountState") or "").lower() == "frozen":
        out["can_sell"] = False
    hook = extensions.get("transferHook")
    if isinstance(hook, dict) and live(hook.get("programId")):
        out["transfer_hook"] = True


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
    _apply_extension_traps(p.get("token_extensions"), out)
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


# --------------------------------------------------------------------------------------
# the insider graph
#
# WRONG FIELD MAPPING SINCE THE FIRST COMMIT, fixed 2026-10-21. The old parser read
# ``size`` and ``tokenAmount`` off each network. Those keys exist, but on the REPORT's
# ``insiderNetworks`` rows, not on ``/insiders/graph``, which is the body it was fed. Every
# network was skipped, ``networks == 0`` returned a literal ``cluster_pct = 0``, and that 0
# was a claim: it entered the merge as a measured negative, ``coverage`` counted it as 12
# established separation points, and dyor's 30% reject and 20% review could not fire on
# any token, ever. MEASURED on 8 live captures (verification lens, 2026-10-20 UTC): the old
# parser returned 0 on all 8, including one whose networks hold 34.3% of supply.
#
# The live shape, read off those captures (and pinned by tests/fixtures/rugcheck/live/):
#
#     [{"net_id": "...", "network_type": "transfer", "relatedMint": null,
#       "nodes": [{"id": "<wallet>", "participant": true, "holdings": <raw atoms>}, ...],
#       "links": [{"source": "<wallet>", "target": "<wallet>"}, ...]}, ...]
#
# * ``holdings`` is raw atoms, the unit of the report's ``token.supply``. MEASURED: each
#   network's summed holdings equals the report's ``insiderNetworks[].currentHolding`` for
#   the same network exactly, on every network present in both bodies.
# * One wallet can sit in two networks with DIFFERENT holdings (0 in one and the balance in
#   the other, or two non-zero values). MEASURED in 6 of 8 captures. Summing per network
#   double-counts it; the total below counts each wallet once, at its larger value.
# * An empty list is what the route returns for every fresh mint we tried: 7 of 7 pump.fun
#   mints seconds old, and again about six minutes later, all with
#   ``graphInsidersDetected = 0`` in the report. "Not analysed yet" and "analysed, nothing
#   found" look identical, so an empty graph is UNKNOWN, not zero (PLAN §5.5: missing data
#   is unknown, never zero). The shape is not new: the September recordings under
#   tests/fixtures/dyor_live/ carry the same ``nodes[].holdings`` body.
# * A sum above the supply is not a measurement. The report captured beside one graph has
#   an ``insiderNetworks`` row of 2,018 wallets holding 215% of supply, and the graph omits
#   that network. Clamping such a sum to 100 would manufacture a certain reject out of a
#   malformed body, so it is unknown.
# * ``participant`` was true on every node and ``relatedMint`` null on every network in
#   every capture. A network naming a related mint is a shape we have never seen, so it
#   makes the reading unknown rather than being silently counted or dropped.
# --------------------------------------------------------------------------------------

#: A supply share above this is a malformed body, never a reading. DEFINITIONAL.
_MAX_SUPPLY_PCT = Decimal(100)


@dataclass(frozen=True)
class InsiderReading:
    """What ``/insiders/graph`` established about clustered supply, or why it did not.

    ``cluster_pct`` is the share of supply held by every wallet in a network of two or more
    wallets, each wallet counted once. ``largest_pct`` is the biggest single network as
    RugCheck groups it. They are both kept because dyor's rule text reads "ONE unexplained
    cluster holding over 30%" while this field has always been the SUM; which one a veto
    should act on is unmeasured, and the receipt carries both so the measurement can.
    """

    cluster_pct: Decimal | None
    largest_pct: Decimal | None = None
    networks: int = 0
    wallets: int = 0
    why_unknown: str | None = None

    def props(self) -> dict[str, Any]:
        """Property dict for the merge. Unknown is ``{}``: an absent key, never a 0."""
        if self.cluster_pct is None:
            return {}
        out: dict[str, Any] = {
            "cluster_pct": self.cluster_pct,
            "cluster_networks": Decimal(self.networks),
            "cluster_wallets": Decimal(self.wallets),
        }
        if self.largest_pct is not None:
            out["cluster_largest_pct"] = self.largest_pct
        return out

    def render(self, insiders_detected: Any = None) -> str:
        """One line for a DERIVED receipt, short enough for ``Receipt.note``."""
        detected = f" report_graphInsidersDetected={insiders_detected}" if insiders_detected is not None else ""
        if self.cluster_pct is None:
            return f"insider graph: cluster_pct UNKNOWN ({self.why_unknown}){detected}"[:300]
        # A receipt line must never be the thing that takes the RugCheck read down.
        largest = "n/a" if self.largest_pct is None else f"{self.largest_pct:.3f}"
        return (
            f"insider graph: cluster_pct={self.cluster_pct:.3f} (sum, each wallet once) "
            f"largest_network_pct={largest} networks={self.networks} "
            f"wallets={self.wallets}{detected}"
        )[:300]


def _unknown_graph(why: str) -> InsiderReading:
    return InsiderReading(cluster_pct=None, why_unknown=why)


def read_insider_graph(payload: Any, total_supply: Decimal | None) -> InsiderReading:
    """Parse the live ``/insiders/graph`` body into a share of supply, or say why not.

    Without the supply the raw holdings are meaningless, and guessing decimals to make a
    number appear would be worse than admitting we cannot compute it.
    """
    if total_supply is None or total_supply <= 0:
        return _unknown_graph("no token supply to divide by")
    if not isinstance(payload, list):
        return _unknown_graph(f"unrecognised body: {type(payload).__name__}")
    if not payload:
        return _unknown_graph("no networks returned; indistinguishable from not analysed")

    per_wallet: dict[str, Decimal] = {}
    largest = Decimal(0)
    networks = 0
    for network in payload:
        if not isinstance(network, dict) or not isinstance(network.get("nodes"), list):
            return _unknown_graph("unrecognised network shape: no nodes list")
        if network.get("relatedMint") not in (None, ""):
            return _unknown_graph("network names a related mint; never seen in a capture")
        held: dict[str, Decimal] = {}
        for node in network["nodes"]:
            wallet = node.get("id") if isinstance(node, dict) else None
            holdings = _dec(node.get("holdings")) if isinstance(node, dict) else None
            if not isinstance(wallet, str) or not wallet or holdings is None or holdings < 0:
                return _unknown_graph("unrecognised node shape: needs id and holdings >= 0")
            held[wallet] = max(held.get(wallet, Decimal(0)), holdings)
        if len(held) < 2:
            continue  # a "cluster" of one wallet is just a holder
        networks += 1
        largest = max(largest, sum(held.values(), Decimal(0)))
        for wallet, amount in held.items():
            per_wallet[wallet] = max(per_wallet.get(wallet, Decimal(0)), amount)

    if networks == 0:
        return _unknown_graph("no network of two or more wallets")
    pct = sum(per_wallet.values(), Decimal(0)) / total_supply * 100
    largest_pct = largest / total_supply * 100
    if pct > _MAX_SUPPLY_PCT or largest_pct > _MAX_SUPPLY_PCT:
        return _unknown_graph(f"networks hold {pct:.1f}% of supply; malformed, not clamped")
    return InsiderReading(
        cluster_pct=pct, largest_pct=largest_pct, networks=networks, wallets=len(per_wallet)
    )


def normalize_insider_graph(payload: Any, total_supply: Decimal | None) -> dict[str, Any]:
    """Cluster-held supply as a percentage, or ``{}`` (unknown). Never a placeholder 0.

    See :func:`read_insider_graph` for the shape and for every reason a reading is unknown.
    """
    return read_insider_graph(payload, total_supply).props()


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
            reading = read_insider_graph(graph.data, props.get("total_supply"))
            props.update(reading.props())
            # The reading's own receipt, known or not, so a stored dossier says what the
            # graph established and why it established nothing. The report's own insider
            # count rides along: a graph far smaller than it is incomplete, and nobody has
            # measured how often that happens.
            detected = full.data.get("graphInsidersDetected") if isinstance(full.data, dict) else None
            receipts.append(
                Receipt(
                    provider=PROVIDER,
                    endpoint="token.insiders.reading",
                    basis=EvidenceBasis.DERIVED,
                    note=reading.render(detected),
                )
            )
    return props, receipts


__all__ = [
    "BASE",
    "GRAPH_TTL_S",
    "INCINERATOR",
    "PROVIDER",
    "REPORT_TTL_S",
    "SUMMARY_TTL_S",
    "TOKENKEG",
    "InsiderReading",
    "insider_graph",
    "normalize_insider_graph",
    "normalize_report",
    "normalize_summary",
    "read_insider_graph",
    "report",
    "report_summary",
    "security_properties",
]
