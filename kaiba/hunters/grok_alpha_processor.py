"""Deterministic processor for signed Grok research (lead, 2026-11-02).

Owner brief: ``docs/research/claude-grok-backend-brief-20261102.md``. Codex's
``grok_alpha_bridge`` receives signed observations into a small store; this module turns them
into the EXISTING Kaiba paths and records every step in its own pipeline store
(``<data dir>/grok-alpha/pipeline.db``), which the dashboard and ``kaiba_grok_pipeline`` read.

What it does per new observation revision:

* **Identity.** Network alias -> :class:`Chain`, contract shape and normalisation, quote-asset
  refusal, the cited X post ids/authors/times. Unknowns stay unknown and are listed.
* **Token track.** The only automatic action, and it is the agent's existing entry path:
  :func:`kaiba.execution.engine.submit_agent_intent` on ``Lane.MANUAL``. That runs ``decide()``
  with every gate (kill switch, pause, chain budget, dossier freshness and blockers, risk gate,
  flat size ladder) and the MANUAL lane's own mode, which the operator sets (box, 2026-11-02:
  SHADOW, so a fill is paper). No size or confidence is passed: the ladder sizes it. A missing or
  stale dossier is rebuilt first with the existing ``scan_token`` (shared limiter), at most
  ``max_scans`` per run. The same token is not re-submitted inside ``resubmit_after_s``.
* **NFT / airdrop.** Registered as an UNVERIFIED lead in the existing ``opportunities`` registry
  and judged by :func:`kaiba.hunters.qualification.qualification`; its refusal reasons are the
  blockers. NFT minting has an exact extra blocker: the BAW execution adapter is incomplete and
  the wallet unpaired (owner deferred pairing).
* **Chain / macro / AI / flow.** Recorded with the primary-source check still to do; never a new
  RPC, bridge, chain or signer trust.
* **Launch.** One launchable X post per card. Recorded here as ``tweet_launch_feed``; the tweet
  launcher reads the card itself (``kaiba/ingest/grok_launch_feed.py``) and decides. Not left for
  the model.
* **Outcomes.** Decisions are followed to their order and position (``position_orders``): order
  state, fees, cost, proceeds, realised PnL. Estimated round-trip cost uses the same
  ``paper_costs`` model the paper broker charges.

Research disposition. Token revisions this processor decided are reviewed in the intake store
(``watch``; ``rejected`` for a malformed or quote-asset contract). Every other revision is left
UNREVIEWED for the Hermes model (Codex's grok-alpha-processing skill), which verifies claims
against primary sources; this processor never leases them, so the model is not starved. A
revision is processed once (pipeline key = observation id + revision digest).

Nothing here bypasses a gate, sizes on confidence, adds a filter to a lane or treats a model's
score, a summary or a repeated post as evidence of safety or edge.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

from kaiba.core.schemas import (
    EVM_CHAINS,
    OBSERVED_NETWORKS,
    Chain,
    Lane,
    ObservedNetwork,
    is_quote_asset,
    normalize_address,
)

log = logging.getLogger(__name__)

#: Codex's intake store and this processor's pipeline, under the data dir (``data/grok-alpha/``).
INTAKE_NAME, PIPELINE_NAME = "inbox.db", "pipeline.db"
MAX_SCANS = 3
CLAIM_LIMIT = 10
RESUBMIT_AFTER_S = 24 * 3600
OUTCOME_ROWS = 50
#: Pending revisions are read in pages of PAGE_ROWS, at most PAGE_LIMIT pages a run (the intake
#: holds at most 4,000 revisions), and at most NEW_PER_RUN unseen ones are kept.
PAGE_ROWS, PAGE_LIMIT, NEW_PER_RUN = 200, 25, 200

#: Network spellings Grok uses -> Kaiba chain. Anything else is an unknown network: a research
#: lead, never a new chain/RPC/signer trust.
NETWORKS: dict[str, Chain] = {
    "sol": Chain.SOL, "solana": Chain.SOL, "solana mainnet": Chain.SOL,
    "bsc": Chain.BSC, "bnb": Chain.BSC, "bnb chain": Chain.BSC, "bnb smart chain": Chain.BSC,
    "binance smart chain": Chain.BSC,
    "robinhood": Chain.ROBINHOOD, "robinhood chain": Chain.ROBINHOOD,
    "base": Chain.BASE, "base mainnet": Chain.BASE,
    "eth": Chain.ETH, "ethereum": Chain.ETH, "ethereum mainnet": Chain.ETH,
}
#: Names of the observed networks (``schemas.OBSERVED_NETWORKS``): EVM networks Kaiba reads by RPC
#: and cannot trade (gmgn-cli 1.6.1 rejects them). A card naming one is recorded with
#: ``network_observed_not_tradeable:<slug>``, never decided. Bare "hyperliquid" is not here: it may
#: mean HyperCore, which is not an EVM network.
OBSERVED_NAMES: dict[str, ObservedNetwork] = {
    "hyperevm": ObservedNetwork.HYPEREVM, "hyper evm": ObservedNetwork.HYPEREVM,
    "hyperliquid evm": ObservedNetwork.HYPEREVM,
    "elysium": ObservedNetwork.ELYSIUM, "kinetiq elysium": ObservedNetwork.ELYSIUM,
}
OBSERVED_PREFIX = "network_observed_not_tradeable:"
#: Bounded latest-state token facts for observed-network token cards (lead 2026-11-02, board
#: GROK-HYPER-FACTS-CLAUDE-20261102): Codex's kaiba.providers.hyper_intelligence.token_facts, a chain
#: proof plus at most four fixed reads. Per-run caps are INVENTED defaults, set in schedule.yaml.
FACTS_PER_RUN = 3
FACTS_METHOD_BUDGET = 18
FACTS_SECONDS = 60
FACTS_RETRY_AFTER_S = 3600
FACTS_MAX_TRIES = 3
#: token_facts' own bounds (hyper_intelligence 8904f88): methods per call, seconds per call.
FACTS_METHODS_PER_CALL = 6
FACTS_CALL_SECONDS = 25
FACTS_SCAN_ROWS = 50
#: Any of these in an ORIGINAL network text or the card's chain_identity means "not this mainnet".
#: network_chain() reads the text without its parentheticals, which is where Grok writes
#: "(testnet, chain 998)", so the gate reads every text whole. No word boundaries on the
#: distinctive words: "testnet998" must match (Codex's review, 2026-11-02); "perp" alone needs
#: letter boundaries so a name like "hyperpool" does not.
NON_MAINNET = re.compile(r"test-?net|devnet|pre-?mainnet|sepolia|hypercore|(?<![a-z])perps?(?![a-z])")
NUMBER = re.compile(r"(?<!\d)\d{2,}(?!\d)")
#: Unknowns that say the read itself failed (limiter, 429, wrong or unproven chain), not the token.
TRANSIENT_MARKS = (":evidence_unavailable", ":observation_budget_exhausted", ":response_identity_mismatch")
#: First-sight market observation (lead 2026-11-02, board OBSERVED-MARKET-WIRING-CLAUDE-20261102):
#: Codex's kaiba.providers.observed_market.token_market, at most one DexScreener request. Only for
#: tokens whose facts proved a contract (code present, decimals known) after the mainnet gate.
#: Its own INVENTED caps, set in schedule.yaml.
MARKET_PER_RUN = 2
MARKET_REQUEST_BUDGET = 2
MARKET_SECONDS = 20
MARKET_RETRY_AFTER_S = 3600
MARKET_MAX_TRIES = 3
MARKET_CALL_SECONDS = 5  # token_market's own request timeout
#: A request that did not return usable evidence (limiter, 429, stale) -- not the token's market.
MARKET_TRANSIENT = frozenset({"market_evidence_unavailable", "market_evidence_out_of_window"})
SOL_ADDRESS = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
POST = re.compile(r"https://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15}|i/web)/status/(\d{5,25})")
#: Exact, current blocker for automatic NFT participation. The BAW wallet is paired and preview-capable
#: (Codex BAW-PAIR-20261102), but the worker has no execute route, so no card qualifies to mint.
NFT_EXECUTION_BLOCKER = "nft_execution_adapter_incomplete:baw_preview_only_no_execute_route"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline (
  observation_id TEXT NOT NULL, revision TEXT NOT NULL, track TEXT NOT NULL, project TEXT NOT NULL,
  received_ms INTEGER NOT NULL, processed_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
  chain TEXT, token TEXT, identity_json TEXT NOT NULL, route TEXT NOT NULL, next_action TEXT NOT NULL,
  blockers_json TEXT NOT NULL DEFAULT '[]', review_state TEXT, decision_json TEXT,
  decision_id TEXT, mode TEXT, est_round_trip_bps INTEGER, cost_basis TEXT,
  order_id TEXT, order_state TEXT, fee_native TEXT, position_id TEXT, cost_native TEXT,
  proceeds_native TEXT, realized_native TEXT, closed_ms INTEGER, exit_reason TEXT,
  cost_unknowns_json TEXT,
  PRIMARY KEY (observation_id, revision));
CREATE INDEX IF NOT EXISTS pipeline_recent ON pipeline(processed_ms DESC);
CREATE INDEX IF NOT EXISTS pipeline_token ON pipeline(chain, token, processed_ms DESC);
CREATE TABLE IF NOT EXISTS runs (
  started_ms INTEGER PRIMARY KEY, finished_ms INTEGER, summary_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS network_facts (
  network TEXT NOT NULL, token TEXT NOT NULL, state TEXT NOT NULL, tries INTEGER NOT NULL,
  first_try_ms INTEGER NOT NULL, last_try_ms INTEGER NOT NULL, view_json TEXT NOT NULL,
  PRIMARY KEY (network, token));
CREATE TABLE IF NOT EXISTS network_market (
  network TEXT NOT NULL, token TEXT NOT NULL, state TEXT NOT NULL, tries INTEGER NOT NULL,
  first_try_ms INTEGER NOT NULL, last_try_ms INTEGER NOT NULL, view_json TEXT NOT NULL,
  PRIMARY KEY (network, token));
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def grok_dir() -> Path:
    from kaiba.core.config import get_settings

    return Path(get_settings().kaiba_data_dir) / "grok-alpha"


# --------------------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------------------


def _iso_ms(value: Any) -> int | None:
    from datetime import datetime

    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(parsed.timestamp() * 1000) if parsed.tzinfo else None


def network_chain(network: str) -> tuple[Chain | None, str | None]:
    """Grok's free-text network -> one Kaiba chain, or the reason there is none.

    MEASURED on the first 17 real cards (2026-11-02): "Robinhood Chain (Orbit L2 ...)",
    "robinhood, sol", "bsc, base". Parentheticals are notes; a list naming one supported chain is
    that chain, two are ambiguous; a testnet is never tradeable."""
    text = re.sub(r"\([^)]*\)", " ", network).casefold()
    parts = [p.strip(" .-") for p in re.split(r"[,/;+&]|\band\b", text) if p.strip(" .-")]
    found: set[Chain] = set()
    observed: set[ObservedNetwork] = set()
    testnet = False
    for part in parts:
        if re.search(r"\b(testnet|devnet|pre-?mainnet)\b", part):
            testnet = True
            continue
        words = part.split()
        for candidate in (part, " ".join(w for w in words if w not in ("mainnet", "network", "l1", "l2"))):
            if candidate in NETWORKS:
                found.add(NETWORKS[candidate])
                break
            if candidate in OBSERVED_NAMES:
                observed.add(OBSERVED_NAMES[candidate])
                break
    if len(found) + len(observed) > 1:
        return None, "network_ambiguous:" + ",".join(sorted([c.value for c in found] + [n.value for n in observed]))
    if found:
        return found.pop(), None
    if observed:
        return None, OBSERVED_PREFIX + observed.pop().value
    return None, "network_testnet_not_tradeable" if testnet else "network_not_supported_by_kaiba"


def _chain_identity(card: dict[str, Any]) -> dict[str, Any] | None:
    """The sender's ``state.chain_identity`` (SCHEMA.md), scalars only. A top-level copy that
    disagrees is kept as a conflict, never resolved by guessing."""
    def clean(raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        return {k: (v[:120] if isinstance(v, str) else v) for k, v in list(raw.items())[:12]
                if isinstance(k, str) and (v is None or type(v) in (str, int, bool))}

    state = card.get("state")
    inner = clean(state.get("chain_identity") if isinstance(state, dict) else None)
    outer = clean(card.get("chain_identity"))
    if inner is not None and outer is not None and inner != outer:
        return {"conflict": True}
    return inner if inner is not None else outer


def _network_texts(card: dict[str, Any]) -> list[str]:
    """The sender's ORIGINAL environment wording, which the canonical ``network`` slug no longer
    carries: ``state.network_text`` and each asset's ``network_text`` / raw slug (sender SCHEMA,
    snapshot-1930). Kept verbatim (bounded) so the facts gate can read every warning in them."""
    state = card.get("state") if isinstance(card.get("state"), dict) else {}
    texts = [state.get("network_text")]
    for asset in (state.get("assets") or [])[:12] if isinstance(state.get("assets"), list) else []:
        if isinstance(asset, dict):
            texts += [asset.get("network_text"), asset.get("_raw_slug"), asset.get("network_slug")]
    return [t[:160] for t in texts if isinstance(t, str) and t.strip()][:24]


def observed_facts_gate(identity: dict[str, Any]) -> tuple[ObservedNetwork | None, list[str]]:
    """May this card's contract be read on its observed network? Decided BEFORE any read.

    Needs the card's own explicit mainnet identity (``state.chain_identity``: the network's family,
    environment ``mainnet``, the canonical chain id) and no contrary signal in it or anywhere in the
    original network text. A refusal makes zero calls; ordinary routing is not changed by it."""
    try:
        network = ObservedNetwork(identity.get("observed_network"))
    except ValueError:
        return None, ["network_not_observed"]
    spec = OBSERVED_NETWORKS[network]
    if spec.chain_id is None:
        return None, ["mainnet_identity_unverified"]
    reasons: list[str] = []
    if identity.get("contract_state") != "observed_network_not_tradeable" or not identity.get("token"):
        reasons.append("contract_not_one_address")
    texts = [str(identity.get("network") or "")] + [str(t) for t in identity.get("network_texts") or []]
    for text in (t.casefold() for t in texts):
        if NON_MAINNET.search(text):
            reasons.append("network_text_not_mainnet")
        reasons += [f"network_text_chain_id:{n}" for n in NUMBER.findall(text) if int(n) != spec.chain_id]
        slugs = {s.value for s in ObservedNetwork} | {c.value for c in Chain}
        named = {w for w in re.split(r"[^a-z0-9]+", text) if w in slugs} - {network.value}
        if named:  # another network named alongside this one: conflicting, never guessed
            reasons.append("network_text_names:" + ",".join(sorted(named)))
    ci = identity.get("chain_identity")
    if not isinstance(ci, dict):
        reasons.append("chain_identity_absent")
    elif ci.get("conflict"):
        reasons.append("chain_identity_conflict")
    else:
        family, env = str(ci.get("family") or "").casefold(), str(ci.get("environment") or "").casefold()
        if family != network.value:
            reasons.append(f"chain_identity_family:{family[:40] or 'missing'}")
        if env != "mainnet":
            reasons.append(f"chain_identity_environment:{env[:40] or 'missing'}")
        cid = ci.get("chain_id")
        if type(cid) is not int or cid != spec.chain_id:
            reasons.append(f"chain_identity_chain_id:{str(cid)[:24]}")
        layer = str(ci.get("layer") or network.value).casefold()  # absent does not contradict
        if layer != network.value:
            reasons.append(f"chain_identity_layer:{layer[:40]}")
        gas = ci.get("gas_token")
        if gas is not None and str(gas).upper() != spec.native_symbol:
            reasons.append(f"chain_identity_gas_token:{str(gas)[:24]}")
        words = " ".join(str(v) for k, v in ci.items() if isinstance(v, str) and k != "source_url").casefold()
        if NON_MAINNET.search(words) or str(ci.get("status") or "").casefold() == "prelaunch":
            reasons.append("chain_identity_not_mainnet")
    reasons = list(dict.fromkeys(reasons))
    return (None, reasons) if reasons else (network, [])


def resolve_identity(card: dict[str, Any]) -> dict[str, Any]:
    """What the observation names, normalised, with every unknown listed. Never raises."""
    unknowns: list[str] = []
    network = card.get("network")
    chain: Chain | None = None
    observed: str | None = None
    if not isinstance(network, str) or not network.strip():
        unknowns.append("network_unknown")
    else:
        chain, why = network_chain(network)
        if why:
            unknowns.append(why)
            observed = why[len(OBSERVED_PREFIX):] if why.startswith(OBSERVED_PREFIX) else None
    contract = card.get("contract")
    token: str | None = None
    contract_state = "absent"
    if isinstance(contract, str) and contract.strip():
        raw = contract.strip()
        shaped = (EVM_ADDRESS.fullmatch(raw) if chain in EVM_CHAINS or observed
                  else SOL_ADDRESS.fullmatch(raw) if chain is Chain.SOL
                  else EVM_ADDRESS.fullmatch(raw) or SOL_ADDRESS.fullmatch(raw))
        if not shaped:
            contract_state = "malformed"
            unknowns.append("contract_malformed")
        elif observed:
            token = raw.lower()  # recorded for the reader; chain stays None, so nothing decides on it
            contract_state = "observed_network_not_tradeable"
        elif chain is not None:
            token = normalize_address(raw, chain)
            contract_state = "quote_asset" if is_quote_asset(chain, token) else "resolved"
            if contract_state == "quote_asset":
                unknowns.append("contract_is_quote_asset")
        else:
            contract_state = "shaped_chain_unknown"
    else:
        unknowns.append("contract_unknown")
    posts = []
    for source in card.get("sources") or []:
        match = POST.match(str(source.get("url") or ""))
        if match:
            posts.append({"url": source["url"], "author": match[1], "post_id": match[2],
                          "published_ms": _iso_ms(source.get("published_at")),
                          "retrieved_ms": _iso_ms(source.get("retrieved_at"))})
    if posts and all(p["published_ms"] is None for p in posts):
        unknowns.append("publication_time_unknown")
    return {"track": card.get("track"), "project": str(card.get("project") or "")[:160],
            "network": network, "chain": chain.value if chain else None, "observed_network": observed,
            "chain_identity": _chain_identity(card), "network_texts": _network_texts(card), "token": token,
            "contract_state": contract_state, "posts": posts,
            "oldest_published_ms": min((p["published_ms"] for p in posts if p["published_ms"]), default=None),
            "expires_ms": _iso_ms(card.get("expires_at")), "event_ms": _iso_ms(card.get("event_at")),
            # What the sender says it checked. Untrusted: kept for the reader, never used as a gate.
            "sender_verification_untrusted": str(card.get("verification") or "")[:300] or None,
            "unknowns": unknowns}


# --------------------------------------------------------------------------------------
# pipeline store
# --------------------------------------------------------------------------------------


class Pipeline:
    """The processor's own small store: one row per processed observation revision."""

    def __init__(self, path: Path | str | None = None, *, readonly: bool = False):
        self.path, self.readonly = Path(path) if path else grok_dir() / PIPELINE_NAME, readonly
        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.connection() as con:
                con.executescript(SCHEMA)
                cols = {r[1] for r in con.execute("PRAGMA table_info(pipeline)")}
                if "cost_unknowns_json" not in cols:  # stores created by ea3ef00/332bdfc
                    con.execute("ALTER TABLE pipeline ADD COLUMN cost_unknowns_json TEXT")
                for col in ("network_facts_state", "network_facts_reasons_json"):  # before 2026-11-02 evening
                    if col not in cols:
                        con.execute(f"ALTER TABLE pipeline ADD COLUMN {col} TEXT")

    @contextmanager
    def connection(self):
        if self.readonly:
            con = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        else:
            con = sqlite3.connect(self.path, timeout=2)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=2000")
        try:
            yield con
        finally:
            con.close()

    def known(self) -> set[tuple[str, str]]:
        """Revisions already handled. A failed scan is not handled: the next run retries it."""
        with self.connection() as con:
            return {(r[0], r[1]) for r in con.execute(
                "SELECT observation_id, revision FROM pipeline WHERE next_action != 'retry_next_run'")}

    def recent_decision(self, chain: str, token: str, since_ms: int) -> dict[str, Any] | None:
        with self.connection() as con:
            row = con.execute("SELECT observation_id, decision_id, processed_ms FROM pipeline WHERE chain=? "
                              "AND token=? AND decision_id IS NOT NULL AND processed_ms>=? "
                              "ORDER BY processed_ms DESC LIMIT 1", (chain, token, since_ms)).fetchone()
        return dict(row) if row else None

    def record(self, row: dict[str, Any]) -> None:
        cols = list(row)
        with self.connection() as con, con:
            con.execute(f"INSERT OR REPLACE INTO pipeline({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                        [row[c] for c in cols])

    def update(self, observation_id: str, revision: str, **fields: Any) -> None:
        sets = ",".join(f"{k}=?" for k in fields)
        with self.connection() as con, con:
            con.execute(f"UPDATE pipeline SET {sets} WHERE observation_id=? AND revision=?",
                        [*fields.values(), observation_id, revision])

    def open_decisions(self, limit: int = OUTCOME_ROWS) -> list[dict[str, Any]]:
        with self.connection() as con:
            return [dict(r) for r in con.execute(
                "SELECT observation_id, revision, chain, token, decision_id, order_id, position_id FROM pipeline "
                "WHERE decision_id IS NOT NULL AND closed_ms IS NULL ORDER BY processed_ms DESC LIMIT ?",
                (limit,))]

    # -- observed-network token facts (one row per (network, token): the dedupe key) ----------
    def facts_candidates(self, limit: int = FACTS_SCAN_ROWS) -> list[dict[str, Any]]:
        """Observed-network token rows not yet gated, oldest first."""
        with self.connection() as con:
            return [dict(r) for r in con.execute(
                "SELECT observation_id, revision, token, identity_json FROM pipeline "
                "WHERE route='token_network_observed_only' AND network_facts_state IS NULL "
                "ORDER BY received_ms, observation_id, revision LIMIT ?", (limit,))]

    def facts_row(self, network: str, token: str) -> dict[str, Any] | None:
        with self.connection() as con:
            row = con.execute("SELECT * FROM network_facts WHERE network=? AND token=?", (network, token)).fetchone()
        return dict(row) if row else None

    def facts_retries(self, *, before_ms: int, max_tries: int, limit: int,
                      table: str = "network_facts") -> list[dict[str, Any]]:
        assert table in ("network_facts", "network_market")
        with self.connection() as con:
            return [dict(r) for r in con.execute(
                f"SELECT network, token, tries FROM {table} WHERE state='transient' AND tries<? "
                "AND last_try_ms<=? ORDER BY last_try_ms, network, token LIMIT ?", (max_tries, before_ms, limit))]

    def save_facts(self, network: str, token: str, state: str, view: dict[str, Any], at: int,
                   table: str = "network_facts", tries: int = 1) -> None:
        assert table in ("network_facts", "network_market")
        with self.connection() as con, con:
            con.execute(
                f"INSERT INTO {table} VALUES(?,?,?,?,?,?,?) ON CONFLICT(network, token) DO UPDATE SET "
                f"state=excluded.state, tries={table}.tries+excluded.tries, last_try_ms=excluded.last_try_ms, "
                "view_json=excluded.view_json", (network, token, state, tries, at, at, json.dumps(view, sort_keys=True)))

    def market_candidates(self, limit: int = FACTS_SCAN_ROWS) -> list[dict[str, Any]]:
        """Tokens with a final facts read (the mainnet gate passed) and no market row, oldest first."""
        with self.connection() as con:
            return [dict(r) for r in con.execute(
                "SELECT f.network, f.token, f.view_json FROM network_facts f LEFT JOIN network_market m "
                "ON m.network=f.network AND m.token=f.token WHERE m.network IS NULL "
                "AND f.state IN ('complete','partial') ORDER BY f.first_try_ms, f.network, f.token LIMIT ?",
                (limit,))]

    def mark_facts(self, observation_id: str, revision: str, state: str, reasons: list[str]) -> None:
        self.update(observation_id, revision, network_facts_state=state,
                    network_facts_reasons_json=json.dumps(reasons))

    def note_run(self, started_ms: int, summary: dict[str, Any]) -> None:
        with self.connection() as con, con:
            con.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?)", (started_ms, now_ms(), json.dumps(summary)))
            con.execute("DELETE FROM runs WHERE started_ms < ?", (started_ms - 7 * 86_400_000,))

    def view(self, limit: int = 50) -> dict[str, Any]:
        with self.connection() as con:
            rows = [dict(r) for r in con.execute("SELECT * FROM pipeline ORDER BY processed_ms DESC LIMIT ?",
                                                 (max(1, min(500, int(limit))),))]
            routes = {r[0]: r[1] for r in con.execute("SELECT route, COUNT(*) FROM pipeline GROUP BY route")}
            last = con.execute("SELECT started_ms, finished_ms, summary_json FROM runs "
                               "ORDER BY started_ms DESC LIMIT 1").fetchone()
        keys = sorted({(json.loads(r.get("identity_json") or "{}").get("observed_network"), r.get("token"))
                       for r in rows if r.get("network_facts_state") == "linked"} - {(None, None)})
        stored: dict[tuple[str, str], dict[str, Any]] = {}
        market: dict[tuple[str, str], dict[str, Any]] = {}
        if keys:  # only the visible rows' facts and market rows, never the whole tables
            with self.connection() as con:
                for table, into in (("network_facts", stored), ("network_market", market)):
                    into.update({(r[0], r[1]): {"state": r[2], "tries": r[3], "last_try_ms": r[4], **json.loads(r[5])}
                                 for r in con.execute(
                                     f"SELECT network, token, state, tries, last_try_ms, view_json FROM {table} "
                                     f"WHERE (network, token) IN (VALUES {','.join('(?,?)' for _ in keys)})",
                                     [v for key in keys for v in key])})
        for r in rows:
            for key in ("identity_json", "blockers_json", "decision_json", "cost_unknowns_json",
                        "network_facts_reasons_json"):
                r[key[:-5]] = json.loads(r.pop(key, None) or "null")
            # Read from the store only: a page load never triggers a provider read.
            observed = (r.get("identity") or {}).get("observed_network")
            r["network_facts"] = stored.get((observed, r.get("token"))) if r.get("network_facts_state") == "linked" else None
            r["network_market"] = market.get((observed, r.get("token"))) if r.get("network_facts_state") == "linked" else None
            r["realized_pct"] = _pct(r.get("realized_native"), r.get("cost_native"))
            published = (r.get("identity") or {}).get("oldest_published_ms")
            # Freshness at RECEIPT (the causal clock), not at Grok's scan time.
            r["source_age_at_receipt_s"] = (r["received_ms"] - published) // 1000 if published else None
            # The processor checks identity and the dossier; it never verifies the post's claim.
            r["evidence_basis"] = "x_post_citation_only"
            r["primary_source_verified"] = False
        return {"rows": rows, "routes": routes,
                "last_run": {"started_ms": last[0], "finished_ms": last[1], **json.loads(last[2])} if last else None}


def _pct(realized: Any, cost: Any) -> float | None:
    try:
        r, c = int(str(realized)), int(str(cost))
    except (TypeError, ValueError):
        return None
    return r / c if c > 0 else None


def _facts_record(view: dict[str, Any]) -> dict[str, Any]:
    """What is kept of one token_facts read: facts, unknowns, the chain proof and redacted receipts.

    The reader's view already omits the RPC URL; this keeps only named scalar fields of it."""
    proof = view.get("network_proof") if isinstance(view.get("network_proof"), dict) else {}
    keep_receipt = ("provider", "endpoint", "basis", "observed_at_ms", "request_digest", "response_digest")
    return {
        "schema": view.get("schema"), "chain_id": view.get("chain_id"),
        "facts": {k: v for k, v in (view.get("facts") or {}).items() if v is None or type(v) in (str, int, bool)},
        "unknowns": [str(u)[:120] for u in view.get("unknowns") or []][:24],
        "network_proof": {k: proof.get(k) for k in ("available", "expected_chain_id", "observed_chain_id",
                                                     "block_number", "block_timestamp_ms", "block_observed_ms")},
        "receipts": [{k: r.get(k) for k in keep_receipt} for r in (view.get("receipts") or [])[:8] if isinstance(r, dict)],
        "method_attempts_upper_bound": view.get("method_attempts_upper_bound"),
        "state_basis": "independent_latest_observations", "atomic_snapshot": False,
        "execution_ready": False, "safety_verified": None, "trading_inference": None,
    }


_PAIR_KEEP = ("pair_address", "dex_id", "url", "base_symbol", "quote_symbol", "requested_token_role",
              "requested_token_price_usd", "pool_liquidity_usd", "requested_token_market_cap_usd",
              "requested_token_fdv_usd", "pair_created_ms")


def _market_record(view: dict[str, Any]) -> dict[str, Any]:
    """What is kept of one token_market observation: named fields only, receipts redacted."""
    def pair(p: Any) -> dict[str, Any] | None:
        if not isinstance(p, dict):
            return None
        kept = {k: p.get(k) for k in _PAIR_KEEP if p.get(k) is None or type(p.get(k)) in (str, int)}
        volume = p.get("pool_volume_usd") if isinstance(p.get("pool_volume_usd"), dict) else {}
        kept["pool_volume_usd_h24"] = volume.get("h24") if isinstance(volume.get("h24"), str) else None
        return kept

    keep_receipt = ("provider", "endpoint", "basis", "observed_at_ms", "request_digest", "response_digest")
    receipts = [{k: r.get(k) for k in keep_receipt} for r in (view.get("receipts") or [])[:2] if isinstance(r, dict)]
    return {
        "schema": view.get("schema"), "chain_id": view.get("chain_id"), "available": view.get("available"),
        "selected_price_pair": pair(view.get("selected_price_pair")),
        "pairs": [p for p in (pair(x) for x in (view.get("pairs") or [])[:5]) if p],
        "price_range_usd": view.get("price_range_usd") if isinstance(view.get("price_range_usd"), dict) else None,
        "response_pair_count": view.get("response_pair_count"), "accepted_pair_count": view.get("accepted_pair_count"),
        "omitted_pair_count": view.get("omitted_pair_count"),
        "rejected_pair_reasons": view.get("rejected_pair_reasons") if isinstance(view.get("rejected_pair_reasons"), dict) else {},
        "unknowns": [str(u)[:120] for u in view.get("unknowns") or []][:24],
        "receipts": receipts, "observed_at_ms": receipts[0].get("observed_at_ms") if receipts else None,
        "method_attempts_upper_bound": view.get("method_attempts_upper_bound"),
        "evidence_basis": "vendor_reported_market_observation", "vendor_update_time": None,
        "complete_market_snapshot": False, "execution_ready": False, "executable_quote": None,
        "executable_depth": None, "holders": None, "safety_verified": None, "trading_inference": None,
    }


# --------------------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------------------


def estimate_round_trip(chain: Chain, dossier: Any) -> dict[str, Any]:
    """The KNOWN part of one round trip's cost, and every part that is not in it, by name.

    ``known_bps`` is the paper broker's own model (``paper_costs.entry_bps + exit_bps``): router fee,
    venue charge, the measured sol entry latency, and buy/sell tax WHEN the dossier knows it. It is a
    floor, not the cost: price impact at our size and the per-leg gas are not in it, and an unknown
    tax is an unknown (it was labelled ``tax_unknown_as_0`` before 2026-11-02, which read as known).
    """
    from kaiba.execution import paper_costs

    def tax(name: str) -> int | None:
        measure = getattr(dossier, name, None)
        value = getattr(measure, "value", None)
        return int(value) if value is not None else None

    buy, sell = tax("buy_tax_bps"), tax("sell_tax_bps")
    unknown = [name for name, value in (("buy_tax", buy), ("sell_tax", sell)) if value is None]
    unknown += ["price_impact_at_size", "gas_per_leg_not_in_bps"]
    assumed = ["router_fee_assumed_not_measured"] if chain in paper_costs.ASSUMED_ROUTER_FEE_CHAINS else []
    return {"known_bps": paper_costs.entry_bps(chain, buy or 0) + paper_costs.exit_bps(chain, sell or 0),
            "unknown": unknown, "assumed": assumed, "basis": "paper_costs_known_parts_only"}


class Processor:
    """One pass: new token revisions -> existing intent path; others -> registry/research rows."""

    def __init__(self, conn: sqlite3.Connection, *, intake: Path | str | None = None,
                 pipeline: Pipeline | None = None, max_scans: int = MAX_SCANS,
                 resubmit_after_s: int = RESUBMIT_AFTER_S, submit: bool = True,
                 clock: Callable[[], int] = now_ms,
                 scan: Callable[..., Any] | None = None, intent: Callable[..., dict[str, Any]] | None = None,
                 load_dossier: Callable[..., Any] | None = None, facts_per_run: int = FACTS_PER_RUN,
                 facts_method_budget: int = FACTS_METHOD_BUDGET, facts_seconds: float = FACTS_SECONDS,
                 facts_retry_after_s: int = FACTS_RETRY_AFTER_S, facts_max_tries: int = FACTS_MAX_TRIES,
                 token_facts: Callable[..., dict[str, Any]] | None = None,
                 market_per_run: int = MARKET_PER_RUN, market_request_budget: int = MARKET_REQUEST_BUDGET,
                 market_seconds: float = MARKET_SECONDS, market_retry_after_s: int = MARKET_RETRY_AFTER_S,
                 market_max_tries: int = MARKET_MAX_TRIES, token_market: Callable[..., dict[str, Any]] | None = None,
                 monotonic: Callable[[], float] = time.monotonic):
        self.conn, self.intake_path = conn, Path(intake) if intake else grok_dir() / INTAKE_NAME
        self.pipeline = pipeline or Pipeline()
        self.max_scans, self.resubmit_after_s, self.submit, self.clock = max_scans, resubmit_after_s, submit, clock
        self._scan, self._intent, self._load = scan, intent, load_dossier
        self.facts_per_run, self.facts_method_budget, self.facts_seconds = facts_per_run, facts_method_budget, facts_seconds
        self.facts_retry_after_s, self.facts_max_tries = facts_retry_after_s, facts_max_tries
        self._token_facts, self.monotonic = token_facts, monotonic
        self.market_per_run, self.market_request_budget, self.market_seconds = market_per_run, market_request_budget, market_seconds
        self.market_retry_after_s, self.market_max_tries, self._token_market = market_retry_after_s, market_max_tries, token_market

    # -- dependencies (the real ones by default; tests pass fakes) ----------------------
    def scan(self, token: str, chain: Chain) -> Any:
        if self._scan is not None:
            return self._scan(token, chain)
        from kaiba.intelligence.dyor import scan_token
        return scan_token(token, chain, conn=self.conn)

    def intent(self, **kw: Any) -> dict[str, Any]:
        if self._intent is not None:
            return self._intent(**kw)
        from kaiba.execution.engine import submit_agent_intent
        return submit_agent_intent(conn=self.conn, **kw)

    def dossier(self, chain: Chain, token: str) -> Any:
        if self._load is not None:
            return self._load(chain, token)
        from kaiba.execution.engine import load_dossier
        return load_dossier(chain, token, self.conn)

    def token_facts(self, network: str, token: str) -> dict[str, Any]:
        if self._token_facts is not None:
            return self._token_facts(network, token)
        from kaiba.providers.hyper_intelligence import token_facts
        return token_facts(network, token)

    def token_market(self, network: str, token: str) -> dict[str, Any]:
        if self._token_market is not None:
            return self._token_market(network, token)
        from kaiba.providers.observed_market import token_market
        return token_market(network, token, conn=self.conn)

    # -- intake ---------------------------------------------------------------------------
    def _store(self, readonly: bool):
        from kaiba.hunters.grok_alpha_bridge import Store
        return Store(self.intake_path, clock=self.clock, readonly=readonly)

    def pending(self, known: set[tuple[str, str]] | None = None) -> tuple[list[dict[str, Any]], int]:
        """Unseen pending revisions (unreviewed, unleased, unexpired), oldest first, WITHOUT a lease.

        Cards left for the model stay pending until it reviews them, so a plain ``LIMIT 200`` would
        stop seeing NEW cards once 200 old ones wait (Codex, 2026-11-02). Keyset pages instead, each
        on its own short read-only connection, skipping ``known``; bounded by PAGE_LIMIT pages and
        NEW_PER_RUN unseen rows. Returns (unseen rows, pending rows scanned).
        """
        store = self._store(readonly=True)
        known = known or set()
        at = self.clock()
        out: list[dict[str, Any]] = []
        scanned, cursor = 0, (-1, "", "")
        for _ in range(PAGE_LIMIT):
            with store.connection() as con:
                rows = con.execute(
                    "SELECT r.key, r.digest, r.received_ms, r.payload, o.track FROM revisions r JOIN observations o "
                    "ON o.key=r.key AND o.digest=r.digest WHERE r.state='unverified' AND r.lease_until_ms<=? "
                    "AND (r.expires_ms IS NULL OR r.expires_ms>?) "
                    "AND (r.received_ms, r.key, r.digest) > (?, ?, ?) "
                    "ORDER BY r.received_ms, r.key, r.digest LIMIT ?",
                    (at, at, *cursor, PAGE_ROWS)).fetchall()
            scanned += len(rows)
            for r in rows:
                if (r[0], r[1]) not in known and len(out) < NEW_PER_RUN:
                    out.append({"observation_id": r[0], "revision": r[1], "received_ms": r[2],
                                "observation": json.loads(r[3]), "track": r[4]})
            if len(rows) < PAGE_ROWS or len(out) >= NEW_PER_RUN:
                break
            cursor = (rows[-1][2], rows[-1][0], rows[-1][1])
        return out, scanned

    # -- one pass ---------------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        started = self.clock()
        summary: dict[str, Any] = {"processed": 0, "submitted": 0, "scans": 0, "registered": 0,
                                   "left_for_model": 0, "deferred_tokens": 0, "outcomes_updated": 0}
        try:
            known = self.pipeline.known()
            new, scanned = self.pending(known)
        except Exception as exc:  # noqa: BLE001 - an unreadable intake is a state, not a crash
            summary["intake"] = f"unavailable:{type(exc).__name__}"
            summary["outcomes_updated"] = self.refresh_outcomes()
            self.pipeline.note_run(started, summary)
            return summary
        summary["intake"] = "readable"
        summary["pending"] = scanned
        decidable = ("resolved", "malformed", "quote_asset")
        tokens = [p for p in new if p["track"] == "token"
                  and resolve_identity(p["observation"])["contract_state"] in decidable]
        token_keys = {(t["observation_id"], t["revision"]) for t in tokens}
        for item in new:
            if (item["observation_id"], item["revision"]) in token_keys:
                continue
            registered, for_model = self._record_other(item)
            summary["registered"] += 1 if registered else 0
            summary["processed"] += 1
            summary["left_for_model"] += 1 if for_model else 0
        if tokens:
            summary.update(self._process_tokens(tokens, summary))
        try:
            summary["network_facts"] = self.enrich_network_facts()
        except Exception as exc:  # noqa: BLE001 - research enrichment never stops the run's bookkeeping
            summary["network_facts"] = {"error": type(exc).__name__}
        try:
            summary["network_market"] = self.enrich_network_market()
        except Exception as exc:  # noqa: BLE001
            summary["network_market"] = {"error": type(exc).__name__}
        summary["outcomes_updated"] = self.refresh_outcomes()
        self.pipeline.note_run(started, summary)
        return summary

    # -- observed-network token facts ----------------------------------------------------------
    def enrich_network_facts(self) -> dict[str, Any]:
        """Latest-state token facts for gated observed-network token cards, inside per-run budgets.

        Each row is gated first (:func:`observed_facts_gate`); a refusal is recorded and makes zero
        calls. One read per (network, token): a later card for the same token links to it. A read
        that failed for a transient reason (limiter, 429, unproven chain) is retried after
        ``facts_retry_after_s``, at most ``facts_max_tries`` times, and ENDS this run's reads so a
        refusal is not followed by more calls. Nothing here changes a route, blocker or decision.
        """
        out = {"gated": 0, "refused": 0, "linked": 0, "read": 0, "retried": 0,
               "method_attempts_upper_bound": 0, "stopped": None}
        if self.facts_per_run <= 0:
            out["stopped"] = "disabled"
            return out
        started = self.monotonic()

        def budget_left() -> str | None:
            if out["read"] + out["retried"] >= self.facts_per_run:
                return "candidate_budget"
            if out["method_attempts_upper_bound"] + FACTS_METHODS_PER_CALL > self.facts_method_budget:
                return "method_budget"
            if self.monotonic() - started + FACTS_CALL_SECONDS > self.facts_seconds:
                return "time_budget"
            return None

        def read(network: str, token: str, retry: bool) -> bool:
            view = self.token_facts(network, token)
            out["method_attempts_upper_bound"] += int(view.get("method_attempts_upper_bound") or 0)
            unknowns = [str(u) for u in view.get("unknowns") or []]
            transient = any(u.startswith("network:") or u.endswith(TRANSIENT_MARKS) for u in unknowns)
            state = "transient" if transient else "partial" if unknowns else "complete"
            self.pipeline.save_facts(network, token, state, _facts_record(view), self.clock())
            out["retried" if retry else "read"] += 1
            if transient:
                out["stopped"] = "transient_refusal"
            return not transient

        for row in self.pipeline.facts_candidates():
            identity = json.loads(row["identity_json"] or "{}")
            network, reasons = observed_facts_gate(identity)
            out["gated"] += 1
            if network is None:
                self.pipeline.mark_facts(row["observation_id"], row["revision"], "refused", reasons)
                out["refused"] += 1
                continue
            if self.pipeline.facts_row(network.value, identity["token"]) is None:
                why = budget_left()
                if why:
                    out["stopped"] = why
                    return out  # this row stays ungated and is first next run
                ok = read(network.value, identity["token"], retry=False)
                self.pipeline.mark_facts(row["observation_id"], row["revision"], "linked", [])
                if not ok:
                    return out
                continue
            self.pipeline.mark_facts(row["observation_id"], row["revision"], "linked", [])
            out["linked"] += 1
        due = self.clock() - self.facts_retry_after_s * 1000
        for row in self.pipeline.facts_retries(before_ms=due, max_tries=self.facts_max_tries, limit=self.facts_per_run):
            why = budget_left()
            if why:
                out["stopped"] = why
                break
            if not read(row["network"], row["token"], retry=True):
                break
        return out

    def enrich_network_market(self) -> dict[str, Any]:
        """ONE first-sight vendor market observation per token, inside its own per-run budgets.

        Candidates are tokens whose facts read is final, so the mainnet gate already passed. The
        facts must also prove a contract (code present, decimals known); otherwise the token is
        recorded ``refused`` and nothing is requested. A transient result (limiter, 429, stale)
        ends this run's requests and is retried after ``market_retry_after_s``, at most
        ``market_max_tries`` times. Prices are vendor-reported: nothing here quotes, sizes, grades
        or trades.
        """
        out = {"candidates": 0, "refused": 0, "read": 0, "retried": 0,
               "method_attempts_upper_bound": 0, "stopped": None}
        if self.market_per_run <= 0:
            out["stopped"] = "disabled"
            return out
        started = self.monotonic()

        def budget_left() -> str | None:
            if out["read"] + out["retried"] >= self.market_per_run:
                return "candidate_budget"
            if out["method_attempts_upper_bound"] + 1 > self.market_request_budget:
                return "request_budget"
            if self.monotonic() - started + MARKET_CALL_SECONDS > self.market_seconds:
                return "time_budget"
            return None

        def read(network: str, token: str, retry: bool) -> bool:
            view = self.token_market(network, token)
            out["method_attempts_upper_bound"] += int(view.get("method_attempts_upper_bound") or 0)
            unknowns = [str(u) for u in view.get("unknowns") or []]
            transient = any(u in MARKET_TRANSIENT for u in unknowns)
            state = "transient" if transient else "complete" if view.get("selected_price_pair") else "partial"
            self.pipeline.save_facts(network, token, state, _market_record(view), self.clock(), table="network_market")
            out["retried" if retry else "read"] += 1
            if transient:
                out["stopped"] = "transient_refusal"
            return not transient

        for row in self.pipeline.market_candidates():
            out["candidates"] += 1
            facts = (json.loads(row["view_json"] or "{}").get("facts") or {})
            reasons = ([] if facts.get("code_present") is True else ["contract_code_not_proven"]) + \
                      ([] if type(facts.get("decimals")) is int else ["decimals_not_proven"])
            if reasons:
                self.pipeline.save_facts(row["network"], row["token"], "refused", {"reasons": reasons},
                                         self.clock(), table="network_market", tries=0)
                out["refused"] += 1
                continue
            why = budget_left()
            if why:
                out["stopped"] = why
                return out
            if not read(row["network"], row["token"], retry=False):
                return out
        due = self.clock() - self.market_retry_after_s * 1000
        for row in self.pipeline.facts_retries(before_ms=due, max_tries=self.market_max_tries,
                                               limit=self.market_per_run, table="network_market"):
            why = budget_left()
            if why:
                out["stopped"] = why
                break
            if not read(row["network"], row["token"], retry=True):
                break
        return out

    def _base_row(self, item: dict[str, Any], ident: dict[str, Any], route: str, next_action: str,
                  blockers: list[str]) -> dict[str, Any]:
        at = self.clock()
        return {"observation_id": item["observation_id"], "revision": item["revision"], "track": item["track"],
                "project": ident["project"] or "?", "received_ms": int(item["received_ms"]),
                "processed_ms": at, "updated_ms": at, "chain": ident["chain"], "token": ident["token"],
                "identity_json": json.dumps(ident), "route": route, "next_action": next_action,
                "blockers_json": json.dumps(blockers)}

    def _record_other(self, item: dict[str, Any]) -> tuple[bool, bool]:
        """Every non-decided revision: recorded, and left unreviewed for the model.

        Returns (an NFT/airdrop lead reached the opportunities registry, the model has work)."""
        ident = resolve_identity(item["observation"])
        registered = False
        track = item["track"]
        if track == "launch":
            # The tweet launcher reads these itself every few seconds (kaiba/ingest/grok_launch_feed.py)
            # and records its own verdict in tweet_launches; nothing for the model or the registry.
            self.pipeline.record(self._base_row(item, ident, "tweet_launch_feed", "launcher_decides", []))
            return False, False
        if track == "token" and ident.get("observed_network"):
            # Nothing for the model to resolve: the network is known and has no trade route.
            row = self._base_row(item, ident, "token_network_observed_only", "record_only_no_trade_route",
                                 list(ident["unknowns"]) + ["executable_market_not_built", "trade_execution_unsupported"])
            self.pipeline.record(row)
            return False, False
        if track == "token":
            row = self._base_row(item, ident, "token_identity_incomplete", "model_resolve_identity",
                                 list(ident["unknowns"]))
        elif track in ("nft", "airdrop"):
            registered, reasons = self._register_opportunity(item, ident)
            blockers = list(reasons) + ([NFT_EXECUTION_BLOCKER] if track == "nft" else [])
            row = self._base_row(item, ident, "opportunity_registry", "model_verify_official_terms", blockers)
        else:
            blockers = list(ident["unknowns"]) + ["no_executable_catalyst_verified"]
            row = self._base_row(item, ident, "research_watch", "model_verify_primary_source", blockers)
        self.pipeline.record(row)
        return registered, True

    def _register_opportunity(self, item: dict[str, Any], ident: dict[str, Any]) -> tuple[bool, list[str]]:
        """An UNVERIFIED lead in the existing registry, judged by the existing qualification."""
        from kaiba.core.db import upsert
        from kaiba.hunters.ev import hunter_config
        from kaiba.hunters.qualification import qualification

        kind = "nft_mint" if item["track"] == "nft" else "airdrop"
        at = self.clock()
        row = {
            "opportunity_id": "grok:" + item["observation_id"][:32], "kind": kind,
            "name": ident["project"][:120] or "?", "chain": ident["chain"],
            "url": ident["posts"][0]["url"] if ident["posts"] else None, "status": "open",
            "ev_score": None, "cost_usd": None, "deadline_ms": ident["expires_ms"],
            "evidence_json": json.dumps({
                "kind": kind, "chain": ident["chain"], "source": "grok_signed_intake_unverified",
                "observation_id": item["observation_id"], "revision": item["revision"],
                "posts": [p["url"] for p in ident["posts"]], "official_url": None, "wallet_count": None,
                "claim": str(item["observation"].get("claim") or "")[:1000], "meta": {"qualification": {}}}),
            "plan_json": "{}", "created_ms": at, "updated_ms": at,
        }
        try:
            upsert(self.conn, "opportunities", row, conflict=["opportunity_id"],
                   update=["name", "chain", "url", "deadline_ms", "evidence_json", "updated_ms"])
        except sqlite3.Error as exc:
            return False, [f"registry_unavailable:{type(exc).__name__}"]
        try:
            floor = Decimal(str(hunter_config().ev_threshold_usd))
        except Exception:  # noqa: BLE001 - an unreadable config cannot lower the bar to zero
            return True, ["ev_floor_unavailable"]
        return True, list(qualification(row, at_ms=at, ev_floor=floor)["reasons"])

    def _process_tokens(self, tokens: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
        """Lease ONLY these token revisions, then decide through the existing intent path.

        ``claim(n)`` alone leases the oldest pending revisions of EVERY track: an older macro card
        took the lease and the token never reached intent, while the macro card was wrongly leased
        away from the model (reproduced by Codex, 2026-11-02). ``only=`` filters in SQL before
        leasing. A revision another worker leased between the peek and this claim is simply absent.
        """
        store = self._store(readonly=False)
        batch = [(t["observation_id"], t["revision"]) for t in tokens[:CLAIM_LIMIT]]  # oldest first
        wanted = set(batch)
        leased = [c for c in store.claim(len(batch), only=batch)
                  if (c["observation_id"], c["revision"]) in wanted]
        scans = 0
        for claim in leased:
            item = {"observation_id": claim["observation_id"], "revision": claim["revision"],
                    "received_ms": claim["received_ms"], "observation": claim["observation"], "track": "token"}
            ident = resolve_identity(item["observation"])
            if ident["contract_state"] in ("malformed", "quote_asset"):
                row = self._base_row(item, ident, "token_rejected_identity", "none", list(ident["unknowns"]))
                row["review_state"] = "rejected"
                self.pipeline.record(row)
                store.review(item["observation_id"], item["revision"], "rejected",
                             f"kaiba processor: contract {ident['contract_state']}; no decision made")
                summary["processed"] += 1
                continue
            chain = Chain(ident["chain"])
            token = ident["token"]
            prior = self.pipeline.recent_decision(chain.value, token, self.clock() - self.resubmit_after_s * 1000)
            if prior:
                row = self._base_row(item, ident, "token_already_decided", "follow_prior_decision",
                                     [f"decided_{(self.clock() - prior['processed_ms']) // 60000}min_ago:"
                                      f"{prior['decision_id']}"])
                row["review_state"] = "watch"
                self.pipeline.record(row)
                store.review(item["observation_id"], item["revision"], "watch",
                             f"kaiba processor: same token decided earlier ({prior['decision_id']}); not resubmitted")
                summary["processed"] += 1
                continue
            dossier = self.dossier(chain, token)
            fresh = dossier is not None and (self.clock() - int(getattr(dossier, "built_at_ms", 0) or 0)) <= 120_000
            if not fresh:
                if scans >= self.max_scans:
                    summary["deferred_tokens"] += 1  # lease expires; next run picks it up
                    continue
                scans += 1
                try:
                    dossier = self.scan(token, chain)
                except Exception as exc:  # noqa: BLE001 - a provider outage is a blocker, not a crash
                    row = self._base_row(item, ident, "token_scan_failed", "retry_next_run",
                                         [f"scan_failed:{type(exc).__name__}"])
                    self.pipeline.record(row)  # unreviewed: the lease expires and a later run retries
                    summary["processed"] += 1
                    continue
            est = estimate_round_trip(chain, dossier)
            est_bps = est["known_bps"]
            thesis = (f"grok:{item['observation_id'][:12]} {ident['project'][:60]} "
                      f"{ident['posts'][0]['url'] if ident['posts'] else ''}").strip()
            if self.submit:
                decision = self.intent(chain=chain, token=token, lane=Lane.MANUAL, size_base_units=0, thesis=thesis)
            else:
                decision = {"ok": False, "action": "not_submitted", "blockers": ["processor_submit_disabled"]}
            blockers = list(decision.get("blockers") or []) + ([decision["reason"]] if decision.get("reason") else [])
            route = "token_decided" if decision.get("decision_id") else "token_refused_before_decision"
            next_action = ("follow_order" if decision.get("order_id") else
                           "none" if decision.get("action") == "skip" else "inspect_blockers")
            row = self._base_row(item, ident, route, next_action, blockers)
            row.update(review_state="watch", decision_json=json.dumps(decision, default=str)[:4000],
                       decision_id=decision.get("decision_id"), mode=decision.get("mode"),
                       est_round_trip_bps=est_bps, cost_basis=est["basis"],
                       cost_unknowns_json=json.dumps(est["unknown"] + est["assumed"]),
                       order_id=decision.get("order_id"),
                       order_state=decision.get("order_state"))
            self.pipeline.record(row)
            note = (f"kaiba processor: identity resolved ({chain.value}); decision {decision.get('decision_id')} "
                    f"action {decision.get('action')} mode {decision.get('mode')} blockers "
                    f"{','.join(blockers[:6]) or 'none'}; known round-trip costs >= {est_bps} bps, unknown: "
                    f"{','.join(est['unknown'])}. "
                    "The post's claim itself is NOT verified.")
            store.review(item["observation_id"], item["revision"], "watch", note[:2000])
            self._journal(item, ident, decision, est)
            summary["processed"] += 1
            summary["submitted"] += 1 if decision.get("decision_id") else 0
        summary["scans"] = scans
        return summary

    def _journal(self, item: dict[str, Any], ident: dict[str, Any], decision: dict[str, Any],
                 est: dict[str, Any]) -> None:
        from kaiba.core import journal

        text = (f"Grok research -> decision: {ident['project'][:60]} {ident['chain']}:{(ident['token'] or '')[:16]} "
                f"action={decision.get('action')} mode={decision.get('mode')} "
                f"blockers={','.join((decision.get('blockers') or [])[:6]) or 'none'} "
                f"known_round_trip>={est['known_bps']}bps unknown={','.join(est['unknown'])}; "
                f"sources {', '.join(p['url'] for p in ident['posts'][:3])}; claim unverified")
        try:
            journal.append("observation", text, subject=ident["token"],
                           refs=[x for x in (item["observation_id"], decision.get("decision_id")) if x],
                           conn=self.conn)
        except Exception as exc:  # noqa: BLE001 - the pipeline row still holds the record
            log.warning("grok processor journal append failed: %s", exc)

    # -- outcomes -------------------------------------------------------------------------
    def refresh_outcomes(self) -> int:
        """Follow each open decision to its order and position. Short reads, bounded rows."""
        updated = 0
        for row in self.pipeline.open_decisions():
            # idx_orders_token (chain, token): orders has no decision_id index on the 22 GB database
            order = self.conn.execute(
                "SELECT order_id, state, fee_native FROM orders WHERE chain=? AND token=? AND decision_id=? "
                "ORDER BY created_ms LIMIT 1", (row["chain"], row["token"], row["decision_id"])).fetchone()
            if order is None:
                continue
            fields: dict[str, Any] = {"order_id": order[0], "order_state": order[1], "fee_native": order[2]}
            pos = self.conn.execute(
                "SELECT p.position_id, p.mode, p.cost_native, p.proceeds_native, p.realized_native, p.closed_ms, "
                "p.exit_reason FROM position_orders po JOIN positions p ON p.position_id=po.position_id "
                "WHERE po.order_id=? LIMIT 1", (order[0],)).fetchone()
            if pos is not None:
                fields.update(position_id=pos[0], mode=pos[1], cost_native=pos[2], proceeds_native=pos[3],
                              realized_native=pos[4], closed_ms=pos[5], exit_reason=pos[6])
            fields["updated_ms"] = self.clock()
            self.pipeline.update(row["observation_id"], row["revision"], **fields)
            updated += 1
        return updated


# --------------------------------------------------------------------------------------
# read-outs (dashboard, MCP)
# --------------------------------------------------------------------------------------


def overview(limit: int = 50, *, intake: Path | str | None = None, pipeline: Path | str | None = None,
             clock: Callable[[], int] = now_ms) -> dict[str, Any]:
    """Intake delivery state plus the pipeline, for /alpha and kaiba_grok_pipeline. Read-only."""
    out: dict[str, Any] = {"financial_execution_by_research": False,
                           "note": "Research rows are not authorization; decisions ran the existing gates."}
    try:
        from kaiba.hunters.grok_alpha_bridge import Store
        out["intake"] = Store(Path(intake) if intake else grok_dir() / INTAKE_NAME, clock=clock,
                              readonly=True).status()
    except Exception as exc:  # noqa: BLE001
        out["intake"] = {"delivery": "intake_unavailable", "reason": type(exc).__name__}
    try:
        out.update(Pipeline(pipeline or grok_dir() / PIPELINE_NAME, readonly=True).view(limit))
        out["available"] = True
    except Exception as exc:  # noqa: BLE001
        out.update(available=False, rows=[], routes={}, last_run=None, reason=f"pipeline_unavailable:{type(exc).__name__}")
    return out
