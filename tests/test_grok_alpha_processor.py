"""kaiba.hunters.grok_alpha_processor: signed Grok research into the existing Kaiba paths.

The intake is Codex's real ``grok_alpha_bridge.Store`` (so a schema change there breaks these
tests); the provider scan and the engine's intent path are fakes that record their calls.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from kaiba.core.schemas import Chain, Lane
from kaiba.hunters import grok_alpha_processor as gp
from kaiba.hunters.grok_alpha_bridge import Store

T0 = 1_793_600_000_000  # fixed clock, ms
SOL_MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
SOL_MINT_2 = "EGLpQAhQVB3gN9ZMsgjCBjabf42sPQ3K7T6DRAvy8pr"
WSOL = "So11111111111111111111111111111111111111112"


class Clock:
    def __init__(self, at=T0):
        self.at = at

    def __call__(self):
        return self.at


def card(track="token", project="Alpha", network="solana", contract=SOL_MINT, post="1234567890", **extra):
    out = {"schema_version": "kaiba.grok.observation/1", "track": track, "project": project,
           "claim": f"{project} looks early", "network": network, "contract": contract,
           "sources": [{"url": f"https://x.com/someone/status/{post}", "published_at": "2026-11-02T06:00:00Z"}],
           "provenance": {"backend": "grok_bot_x_search"}}
    out.update(extra)
    return out


class Fakes:
    def __init__(self, *, dossier_age_ms=None, scan_error=None):
        self.scans, self.intents = [], []
        self.dossier_age_ms, self.scan_error = dossier_age_ms, scan_error
        self.facts_calls, self.facts_unknowns = [], []
        self.market_calls, self.market_unknowns, self.facts_code = [], [], True

    def token_market(self, network, token):
        """An observed_market view as Codex's provider returns it; never a live request."""
        self.market_calls.append((network, token))
        pair = {"pair_address": "0x" + "9" * 40, "dex_id": "fixture", "url": "https://dexscreener.com/hyperevm/x",
                "base_symbol": "HHH", "quote_symbol": "WHYPE", "requested_token_role": "base",
                "requested_token_price_usd": "0.0123", "pool_liquidity_usd": "45678.9",
                "requested_token_market_cap_usd": None, "requested_token_fdv_usd": "1230000",
                "pool_volume_usd": {"h24": "999"}, "pair_created_ms": T0 - 1000, "secret_field": "private"}
        ok = not self.market_unknowns
        return {"schema": "kaiba.observed-market/1", "network": network, "chain_id": 999, "address": token,
                "available": ok, "pairs": [pair] if ok else [], "selected_price_pair": pair if ok else None,
                "response_pair_count": 1 if ok else None, "accepted_pair_count": 1 if ok else 0,
                "omitted_pair_count": 0, "rejected_pair_reasons": {}, "unknowns": list(self.market_unknowns),
                "receipts": [{"provider": "dexscreener", "endpoint": "research.observed_token_pairs", "basis": "provider_reported",
                              "observed_at_ms": T0, "request_digest": "r", "response_digest": "d", "note": "private"}],
                "method_attempts_upper_bound": 1, "price_range_usd": {"minimum": "0.0123", "maximum": "0.0123"} if ok else None,
                "execution_ready": False, "executable_quote": None}

    def token_facts(self, network, token):
        """A token_facts view as hyper_intelligence returns it; never a live read."""
        self.facts_calls.append((network, token))
        return {"schema": "kaiba.network-facts/1", "kind": "token", "network": network, "chain_id": 999,
                "address": token, "facts": {"code_present": self.facts_code, "code_sha256": "c" * 64, "symbol": "HHH",
                                            "decimals": 18, "total_supply_base_units": str(10**27),
                                            "liquidity_usd": None, "market_cap_usd": None, "holders": None},
                "unknowns": list(self.facts_unknowns),
                "network_proof": {"available": not self.facts_unknowns, "expected_chain_id": 999,
                                  "observed_chain_id": 999, "block_number": 7, "rpc_url": "https://private.invalid"},
                "receipts": [{"provider": "network-probe-rpc", "endpoint": "network.eth_chainId", "basis": "provider_reported",
                              "observed_at_ms": T0, "request_digest": "r", "response_digest": "d",
                              "note": "private RPC URL"}],
                "method_attempts_upper_bound": 6 if not self.facts_unknowns else 1,
                "atomic_snapshot": False, "execution_ready": False}

    def dossier(self, at):
        return SimpleNamespace(built_at_ms=at, buy_tax_bps=SimpleNamespace(value=None),
                               sell_tax_bps=SimpleNamespace(value=None))

    def scan(self, token, chain):
        self.scans.append((token, chain))
        if self.scan_error:
            raise self.scan_error
        return self.dossier(T0)

    def intent(self, **kw):
        self.intents.append(kw)
        n = len(self.intents)
        return {"ok": True, "decision_id": f"dec_{n}", "action": "enter", "mode": "shadow",
                "lane": "manual", "blockers": [], "order_id": f"ord_{n}", "order_state": "planned"}

    def load(self, chain, token):
        return None if self.dossier_age_ms is None else self.dossier(T0 - self.dossier_age_ms)


@pytest.fixture
def world(tmp_db, tmp_path):
    clock = Clock()
    intake = Store(tmp_path / "inbox.db", clock=clock)
    fakes = Fakes()

    def make(**kw):
        kw.setdefault("token_facts", fakes.token_facts)  # no test reaches a live RPC by accident
        kw.setdefault("token_market", fakes.token_market)  # ... or the vendor
        return gp.Processor(tmp_db, intake=tmp_path / "inbox.db", pipeline=gp.Pipeline(tmp_path / "pipeline.db"),
                            clock=clock, scan=fakes.scan, intent=fakes.intent, load_dossier=fakes.load, **kw)

    return SimpleNamespace(conn=tmp_db, clock=clock, intake=intake, fakes=fakes, make=make, tmp=tmp_path)


def states(world):
    with sqlite3.connect(world.tmp / "inbox.db") as con:
        return {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT o.project, r.state, r.lease_until_ms FROM revisions r JOIN "
            "(SELECT key, json_extract(payload,'$.project') AS project FROM revisions) o ON o.key=r.key")}


def rows(world):
    return {r["project"]: r for r in gp.Pipeline(world.tmp / "pipeline.db", readonly=True).view(100)["rows"]}


# --------------------------------------------------------------------------------------


def test_identity_resolution_keeps_unknowns():
    sol = gp.resolve_identity(card())
    assert (sol["chain"], sol["token"], sol["contract_state"]) == ("sol", SOL_MINT, "resolved")
    assert sol["posts"][0]["post_id"] == "1234567890" and sol["posts"][0]["author"] == "someone"
    evm = gp.resolve_identity(card(network="BNB Chain", contract="0xAbCdEf0123456789aBcDeF0123456789AbCdEf01"))
    assert (evm["chain"], evm["token"]) == ("bsc", "0xabcdef0123456789abcdef0123456789abcdef01")
    assert gp.resolve_identity(card(contract=WSOL))["contract_state"] == "quote_asset"
    assert gp.resolve_identity(card(network="bsc", contract=SOL_MINT))["contract_state"] == "malformed"
    odd = gp.resolve_identity(card(network="Monad"))
    assert odd["chain"] is None and "network_not_supported_by_kaiba" in odd["unknowns"]
    assert "contract_unknown" in gp.resolve_identity(card(contract=None))["unknowns"]


@pytest.mark.parametrize("network, chain, why", [
    # the first 17 real Grok cards on the box (2026-11-02) used these spellings
    ("Robinhood Chain (Orbit L2 on Arbitrum)", Chain.ROBINHOOD, None),
    ("BNB Smart Chain", Chain.BSC, None),
    ("Ethereum mainnet", Chain.ETH, None),
    ("robinhood, sol", None, "network_ambiguous:robinhood,sol"),
    ("bsc, base", None, "network_ambiguous:base,bsc"),
    ("Rome Protocol testnet", None, "network_testnet_not_tradeable"),
    # observed networks (schemas.OBSERVED_NETWORKS): named precisely, never decided
    ("Elysium (pre-mainnet)", None, "network_observed_not_tradeable:elysium"),
    ("HyperEVM", None, "network_observed_not_tradeable:hyperevm"),
    ("Hyperliquid EVM mainnet", None, "network_observed_not_tradeable:hyperevm"),
    ("hyperevm, base", None, "network_ambiguous:base,hyperevm"),
    ("Hyperliquid", None, "network_not_supported_by_kaiba"),  # may be HyperCore, not an EVM network
    ("Quip Network", None, "network_not_supported_by_kaiba"),
    ("Base mainnet", Chain.BASE, None),
])
def test_real_network_spellings(network, chain, why):
    assert gp.network_chain(network) == (chain, why)


def test_a_token_goes_through_the_existing_intent_path_on_manual(world):
    world.intake.ingest([card()])
    out = world.make().run()
    assert out["submitted"] == 1 and out["scans"] == 1
    (call,) = world.fakes.intents
    assert call["lane"] is Lane.MANUAL and call["chain"] is Chain.SOL and call["token"] == SOL_MINT
    assert call["size_base_units"] == 0  # the risk ladder sizes it; no confidence or size passed
    row = rows(world)["Alpha"]
    assert (row["route"], row["mode"], row["decision_id"], row["review_state"]) == ("token_decided", "shadow", "dec_1", "watch")
    assert row["est_round_trip_bps"] > 0 and row["cost_basis"] == "paper_costs_known_parts_only"
    # an unknown tax is named, never folded in as 0; impact and per-leg gas are never in the number
    assert {"buy_tax", "sell_tax", "price_impact_at_size", "gas_per_leg_not_in_bps"} <= set(row["cost_unknowns"])
    assert row["evidence_basis"] == "x_post_citation_only" and row["primary_source_verified"] is False
    assert row["source_age_at_receipt_s"] == (T0 - 1_793_599_200_000) // 1000  # 06:00Z post, received at T0
    assert states(world)["Alpha"][0] == "watch"
    body = world.conn.execute("SELECT body FROM journal ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    assert "claim unverified" in body and "mode=shadow" in body


def test_a_fresh_dossier_is_not_rescanned_and_the_scan_budget_defers_the_rest(world):
    world.intake.ingest([card(project=f"T{i}", contract=m, post=str(1234567890 + i))
                         for i, m in enumerate([SOL_MINT, SOL_MINT_2,
                                                "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
                                                "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"])])
    out = world.make(max_scans=3).run()
    assert (out["scans"], out["submitted"], out["deferred_tokens"]) == (3, 3, 1)
    world.clock.at += 301_000  # the deferred revision's lease expires
    second = world.make(max_scans=3).run()
    assert second["submitted"] == 1 and len(world.fakes.intents) == 4
    fresh = Fakes()
    fresh.load = lambda chain, token: fresh.dossier(world.clock.at - 30_000)  # built 30 s ago
    world.intake.ingest([card(project="Fresh", contract="DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", post="999999999")])
    gp.Processor(world.conn, intake=world.tmp / "inbox.db", pipeline=gp.Pipeline(world.tmp / "pipeline.db"),
                 clock=world.clock, scan=fresh.scan, intent=fresh.intent, load_dossier=fresh.load).run()
    assert fresh.scans == [] and len(fresh.intents) == 1


def test_the_same_token_is_not_resubmitted_inside_the_window(world):
    world.intake.ingest([card(project="First", post="1111111111")])
    world.make().run()
    world.intake.ingest([card(project="Second", post="2222222222")])
    world.make().run()
    assert len(world.fakes.intents) == 1
    assert rows(world)["Second"]["route"] == "token_already_decided"
    world.clock.at += gp.RESUBMIT_AFTER_S * 1000 + 1
    world.intake.ingest([card(project="Third", post="3333333333")])
    world.make().run()
    assert len(world.fakes.intents) == 2


def test_an_observed_network_token_is_recorded_with_its_blockers_and_never_decided(world):
    world.intake.ingest([card(project="Hyper", network="HyperEVM",
                              contract="0xAbCdEf0123456789aBcDeF0123456789AbCdEf01")])
    out = world.make().run()
    assert world.fakes.intents == [] and out["left_for_model"] == 0  # nothing for the model to resolve
    row = rows(world)["Hyper"]
    assert (row["route"], row["next_action"]) == ("token_network_observed_only", "record_only_no_trade_route")
    assert row["chain"] is None and row["token"] == "0xabcdef0123456789abcdef0123456789abcdef01"
    assert {"network_observed_not_tradeable:hyperevm", "executable_market_not_built",
            "trade_execution_unsupported"} <= set(row["blockers"])


def test_non_token_tracks_are_recorded_registered_and_left_for_the_model(world):
    world.intake.ingest([card(track="nft", project="Mint A", contract=None),
                         card(track="macro", project="Fed", network=None, contract=None, post="5555555555")])
    out = world.make().run()
    assert out["left_for_model"] == 2 and out["registered"] == 1 and world.fakes.intents == []
    st = states(world)
    assert st["Mint A"] == ("unverified", 0) and st["Fed"] == ("unverified", 0)  # no lease, no review
    nft, macro = rows(world)["Mint A"], rows(world)["Fed"]
    assert nft["route"] == "opportunity_registry" and gp.NFT_EXECUTION_BLOCKER in nft["blockers"]
    assert "official_source_unavailable" in nft["blockers"]  # the existing qualification's verdict
    assert macro["route"] == "research_watch" and "no_executable_catalyst_verified" in macro["blockers"]
    reg = world.conn.execute("SELECT kind, status, evidence_json FROM opportunities").fetchall()
    assert len(reg) == 1 and reg[0][0] == "nft_mint"
    assert json.loads(reg[0][2])["source"] == "grok_signed_intake_unverified"
    assert world.make().run()["processed"] == 0  # processed once per revision


def test_malformed_or_quote_asset_contracts_are_rejected_without_a_decision(world):
    world.intake.ingest([card(project="Bad", network="bsc", contract="0x1234"),
                         card(project="Wrapped", contract=WSOL, post="7777777777")])
    world.make().run()
    assert world.fakes.intents == [] and world.fakes.scans == []
    st = states(world)
    assert st["Bad"][0] == "rejected" and st["Wrapped"][0] == "rejected"


def test_a_failed_scan_is_a_blocker_and_is_retried(world):
    world.intake.ingest([card()])
    failing = Fakes(scan_error=TimeoutError("provider"))
    gp.Processor(world.conn, intake=world.tmp / "inbox.db", pipeline=gp.Pipeline(world.tmp / "pipeline.db"),
                 clock=world.clock, scan=failing.scan, intent=failing.intent, load_dossier=failing.load).run()
    assert failing.intents == [] and rows(world)["Alpha"]["route"] == "token_scan_failed"
    assert states(world)["Alpha"][0] == "unverified"
    world.clock.at += 301_000
    world.make().run()
    assert len(world.fakes.intents) == 1 and rows(world)["Alpha"]["route"] == "token_decided"


def test_outcomes_follow_the_order_and_position(world):
    world.intake.ingest([card()])
    world.make().run()
    c = world.conn
    c.execute("INSERT INTO orders (order_id, decision_id, chain, token, side, lane, mode, input_token, output_token, "
              "amount_in, min_out, slippage_bps, state, provider, fee_native, created_ms, updated_ms) "
              "VALUES ('ord_x','dec_1','sol',?,'buy','manual','shadow','sol',?,'100','1',500,'filled','paper','3',?,?)",
              (SOL_MINT, SOL_MINT, T0, T0))
    c.execute("INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, cost_native, "
              "proceeds_native, realized_native, exit_reason) VALUES ('pos_x','sol',?,'manual','shadow',?,?, "
              "'1000','800','-200','stop_loss')", (SOL_MINT, T0, T0 + 60_000))
    c.execute("INSERT INTO position_orders VALUES ('pos_x','ord_x','buy',?)", (T0,))
    c.commit()
    assert world.make().refresh_outcomes() == 1
    row = rows(world)["Alpha"]
    assert (row["order_state"], row["position_id"], row["fee_native"]) == ("filled", "pos_x", "3")
    assert row["realized_pct"] == pytest.approx(-0.2) and row["exit_reason"] == "stop_loss"
    assert world.make().refresh_outcomes() == 0  # closed rows are final


def test_a_missing_intake_is_a_state_not_a_crash(tmp_db, tmp_path):
    out = gp.Processor(tmp_db, intake=tmp_path / "absent.db", pipeline=gp.Pipeline(tmp_path / "p.db")).run()
    assert out["intake"].startswith("unavailable")
    view = gp.overview(intake=tmp_path / "absent.db", pipeline=tmp_path / "p.db")
    assert view["intake"]["delivery"] == "intake_unavailable" and view["available"] is True
    assert gp.overview(pipeline=tmp_path / "never.db")["available"] is False


def test_nothing_new_takes_no_lease(world, monkeypatch):
    world.intake.ingest([card()])
    world.make().run()
    calls = []
    monkeypatch.setattr(Store, "claim", lambda self, limit=5: calls.append(limit) or [])
    world.make().run()
    assert calls == []


def test_a_stale_dossier_is_rebuilt_before_the_decision(world):
    # decide() refuses a dossier older than its budget; an old one must be rebuilt, not trusted
    world.intake.ingest([card()])
    stale = Fakes(dossier_age_ms=600_000)
    gp.Processor(world.conn, intake=world.tmp / "inbox.db", pipeline=gp.Pipeline(world.tmp / "pipeline.db"),
                 clock=world.clock, scan=stale.scan, intent=stale.intent, load_dossier=stale.load).run()
    assert stale.scans == [(SOL_MINT, Chain.SOL)] and len(stale.intents) == 1


# --------------------------------------------------------------------------------------
# dashboard: the /alpha panel reads the same stores
# --------------------------------------------------------------------------------------

PASSWORD = "grok-panel-test-password"


@pytest.fixture
def console(tmp_db, tmp_path, monkeypatch):
    import shutil

    from fastapi.testclient import TestClient

    from dashboard import intel, trading
    from dashboard.app import create_app
    from kaiba.core.config import DEFAULT_RISK_PATH, get_settings

    risk_path = tmp_path / "risk.yaml"
    shutil.copy(DEFAULT_RISK_PATH, risk_path)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(risk_path))
    monkeypatch.setenv("KAIBA_DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("KAIBA_DASHBOARD_ISOLATE", "0")
    get_settings.cache_clear()
    trading.clear_caches()
    intel.clear_caches()
    client = TestClient(create_app())
    assert client.post("/login", data={"password": PASSWORD}, follow_redirects=False).status_code == 303
    try:
        yield client
    finally:
        client.close()
        get_settings.cache_clear()


def test_the_pending_count_is_the_processors_not_the_capped_list(console, tmp_db, tmp_path):
    """Codex's UI finding: the intake's status list is capped at 20; the processor counts them all."""
    clock = Clock()
    store = Store(tmp_path / "grok-alpha" / "inbox.db", clock=clock)
    cards = [card(track="macro", project=f"Macro {i}", network=None, contract=None,
                  post=str(5_000_000_000 + i)) for i in range(25)]
    store.ingest(cards[:13])  # the intake takes bounded batches
    store.ingest(cards[13:])
    page = console.get("/alpha").text
    assert "≥20 pending" in page  # before any run: the cap is shown as a floor, not a total
    fakes = Fakes()
    gp.Processor(tmp_db, clock=clock, scan=fakes.scan, intent=fakes.intent, load_dossier=fakes.load,
                 token_facts=fakes.token_facts).run()
    page = console.get("/alpha").text
    assert "25 pending at last run" in page and "≥20 pending" not in page


def test_alpha_page_says_when_nothing_was_delivered(console):
    page = console.get("/alpha")
    assert page.status_code == 200 and "Grok research pipeline" in page.text
    assert "intake: intake unavailable" in page.text and "processor has not run" in page.text


def test_alpha_page_shows_read_facts_and_refusals_without_reading(console, tmp_db, tmp_path):
    grok = tmp_path / "grok-alpha"
    clock = Clock()
    Store(grok / "inbox.db", clock=clock).ingest([hyper("FactsRow"), hyper("TestnetRow", HYPER_B, identity={**MAINNET, "chain_id": 998})])
    fakes = Fakes()
    gp.Processor(tmp_db, clock=clock, scan=fakes.scan, intent=fakes.intent, load_dossier=fakes.load,
                 token_facts=fakes.token_facts).run()
    assert len(fakes.facts_calls) == 1
    page = console.get("/alpha").text
    assert "facts (complete): HHH · 18 decimals" in page and "hyperevm:0xa1…a1a1" in page
    assert "facts not read: chain identity chain id:998" in page
    assert len(fakes.facts_calls) == 1  # rendering read nothing


def test_alpha_page_shows_a_processed_card(console, tmp_db, tmp_path):
    grok = tmp_path / "grok-alpha"
    clock = Clock()
    Store(grok / "inbox.db", clock=clock).ingest([card(project="PanelToken")])
    fakes = Fakes()
    gp.Processor(tmp_db, clock=clock, scan=fakes.scan, intent=fakes.intent, load_dossier=fakes.load).run()
    page = console.get("/alpha")
    assert page.status_code == 200
    assert "PanelToken" in page.text and "token decided" in page.text and "@someone" in page.text
    assert ">paper<" in page.text  # the MANUAL lane's shadow mode, not a live trade
    assert "price impact at size" in page.text and "claim unverified" in page.text


# --------------------------------------------------------------------------------------
# leases (Codex's reproduction, 2026-11-02): only the processor's own token revisions are leased
# --------------------------------------------------------------------------------------


def test_an_older_macro_card_does_not_starve_a_newer_token(world):
    world.intake.ingest([card(track="macro", project="Older macro", network=None, contract=None, post="4444444444")])
    world.clock.at += 1000
    world.intake.ingest([card(project="Newer token")])
    world.clock.at += 1000
    out = world.make().run()
    assert out["submitted"] == 1 and len(world.fakes.intents) == 1
    assert states(world)["Older macro"] == ("unverified", 0)  # never leased away from the model
    assert states(world)["Newer token"][0] == "watch"


def test_a_token_leased_by_another_worker_is_not_decided_twice(world):
    world.intake.ingest([card()])
    (theirs,) = world.intake.claim(1)  # e.g. the Hermes model took it first
    out = world.make().run()
    assert world.fakes.intents == [] and out["submitted"] == 0 and "Alpha" not in rows(world)
    world.clock.at += 301_000  # their lease lapses unreviewed: the processor may decide it
    world.make().run()
    assert len(world.fakes.intents) == 1 and theirs["observation_id"] == rows(world)["Alpha"]["observation_id"]


def test_a_lease_taken_between_the_peek_and_the_claim_is_respected(world, monkeypatch):
    world.intake.ingest([card()])
    peek = gp.Processor.pending

    def peek_then_lose_the_race(self, known=None):
        seen = peek(self, known)
        world.intake.claim(1)  # another worker leases it after the processor looked
        return seen

    monkeypatch.setattr(gp.Processor, "pending", peek_then_lose_the_race)
    out = world.make().run()
    assert world.fakes.intents == [] and out["submitted"] == 0


def test_old_unreviewed_cards_do_not_hide_a_new_token(world, monkeypatch):
    # cards left for the model stay pending; more of them than one page must not hide a new card
    monkeypatch.setattr(gp, "PAGE_ROWS", 3)
    world.intake.ingest([card(track="macro", project=f"M{i}", network=None, contract=None, post=str(5000000000 + i))
                         for i in range(8)])
    first = world.make().run()
    assert first["left_for_model"] == 8 and world.fakes.intents == []
    world.clock.at += 1000
    world.intake.ingest([card(project="Late token")])
    out = world.make().run()
    assert out["pending"] == 9 and out["submitted"] == 1 and len(world.fakes.intents) == 1
    assert world.make().run()["processed"] == 0


# --------------------------------------------------------------------------------------
# HyperEVM token facts for gated observed-network cards (GROK-HYPER-FACTS-CLAUDE-20261102)
# --------------------------------------------------------------------------------------

HYPER_A = "0x" + "a1" * 20
HYPER_B = "0x" + "b2" * 20
HYPER_C = "0x" + "c3" * 20
MAINNET = {"family": "hyperevm", "environment": "mainnet", "chain_id": 999, "gas_token": "HYPE", "layer": "hyperevm"}


def hyper(project, contract=HYPER_A, network="hyperevm", identity=MAINNET, post=None, **state):
    extra = {"state": {**({"chain_identity": dict(identity)} if identity is not None else {}), **state}}
    return card(project=project, network=network, contract=contract,
                post=post or str(abs(hash(project)) % 10**12).rjust(10, "1"), **extra)


def test_a_mainnet_999_card_gets_one_bounded_read_and_nothing_else(world):
    world.intake.ingest([hyper("Pos")])
    out = world.make().run()
    assert world.fakes.facts_calls == [("hyperevm", HYPER_A.lower())]
    assert world.fakes.intents == [] and world.fakes.scans == []  # no decision, no order, no dossier
    nf = out["network_facts"]
    assert (nf["read"], nf["refused"], nf["method_attempts_upper_bound"]) == (1, 0, 6)
    row = rows(world)["Pos"]
    assert (row["route"], row["next_action"]) == ("token_network_observed_only", "record_only_no_trade_route")
    assert "trade_execution_unsupported" in row["blockers"]  # unchanged by the facts
    facts = row["network_facts"]
    assert facts["state"] == "complete" and facts["facts"]["symbol"] == "HHH" and facts["facts"]["holders"] is None
    assert facts["atomic_snapshot"] is False and facts["trading_inference"] is None and facts["safety_verified"] is None
    stored = json.dumps(facts)
    assert "private" not in stored and "rpc_url" not in stored  # redacted record only


@pytest.mark.parametrize("network, identity, reason", [
    # Codex's reproductions: network_chain() drops the parenthetical, the gate does not
    ("hyperevm (testnet, chain 998)", MAINNET, "network_text_not_mainnet"),
    ("hyperevm / sol testnet", MAINNET, "network_text_not_mainnet"),
    ("HyperEVM (chain 1339)", MAINNET, "network_text_chain_id:1339"),
    ("hyperevm (hypercore spot)", MAINNET, "network_text_not_mainnet"),
    ("hyperevm", {**MAINNET, "environment": "testnet", "chain_id": 998}, "chain_identity_environment:testnet"),
    ("hyperevm", {**MAINNET, "chain_id": 998}, "chain_identity_chain_id:998"),
    ("hyperevm", {**MAINNET, "chain_id": 99801}, "chain_identity_chain_id:99801"),
    ("hyperevm", {**MAINNET, "chain_id": True}, "chain_identity_chain_id:True"),
    ("hyperevm", {**MAINNET, "family": "elysium"}, "chain_identity_family:elysium"),
    ("hyperevm", {**MAINNET, "layer": "hypercore"}, "chain_identity_layer:hypercore"),
    ("hyperevm", {**MAINNET, "gas_token": "ETH"}, "chain_identity_gas_token:ETH"),
    ("hyperevm", {**MAINNET, "status": "prelaunch"}, "chain_identity_not_mainnet"),
    ("hyperevm", {**MAINNET, "environment": "unknown"}, "chain_identity_environment:unknown"),
    ("hyperevm", None, "chain_identity_absent"),
    # Elysium has no verified mainnet id: refused whatever the card claims
    ("elysium", {**MAINNET, "family": "elysium", "chain_id": 99801}, "mainnet_identity_unverified"),
    ("elysium (testnet 99801)", None, "mainnet_identity_unverified"),
])
def test_non_mainnet_or_conflicting_identity_is_refused_before_any_read(world, network, identity, reason):
    world.intake.ingest([hyper("Neg", network=network, identity=identity)])
    out = world.make().run()
    assert world.fakes.facts_calls == [] and world.fakes.intents == []
    row = rows(world)["Neg"]
    assert row["network_facts_state"] == "refused" and reason in row["network_facts_reasons"]
    assert out["network_facts"]["refused"] == 1 and out["network_facts"]["method_attempts_upper_bound"] == 0


def test_a_conflicting_top_level_identity_is_refused():
    ident = gp.resolve_identity({**hyper("X"), "chain_identity": {**MAINNET, "chain_id": 998}})
    assert gp.observed_facts_gate(ident) == (None, ["chain_identity_conflict"])
    assert gp.observed_facts_gate(gp.resolve_identity(hyper("Y"))) == (gp.ObservedNetwork.HYPEREVM, [])


def test_baskets_and_ordinary_routing_are_unchanged(world):
    basket = hyper("Basket", contract=f"{HYPER_A}, {HYPER_B}")
    sol = card(project="Sol", post="7777777777")
    world.intake.ingest([basket, sol])
    world.make().run()
    assert world.fakes.facts_calls == []  # a basket is malformed: never one address to read
    assert rows(world)["Basket"]["route"] != "token_network_observed_only"
    assert len(world.fakes.intents) == 1 and world.fakes.intents[0]["chain"] == "sol"  # ordinary path as before


def test_a_token_is_read_once_however_many_cards_name_it(world):
    world.intake.ingest([hyper("First"), hyper("Second")])
    world.make().run()
    world.intake.ingest([hyper("Third")])
    out = world.make().run()
    assert world.fakes.facts_calls == [("hyperevm", HYPER_A.lower())]
    assert out["network_facts"]["linked"] == 1
    assert {p: rows(world)[p]["network_facts"]["facts"]["symbol"] for p in ("First", "Second", "Third")} == {
        "First": "HHH", "Second": "HHH", "Third": "HHH"}


@pytest.mark.parametrize("kw, stopped", [
    ({"facts_per_run": 1}, "candidate_budget"),
    ({"facts_method_budget": 11}, "method_budget"),  # one 6-method read fits, a second does not
    ({"facts_seconds": 30}, "time_budget"),
])
def test_per_run_budgets_cap_reads_and_carry_the_rest_to_the_next_run(world, kw, stopped):
    world.intake.ingest([hyper("A", HYPER_A), hyper("B", HYPER_B), hyper("C", HYPER_C)])
    ticks = iter([0.0, 0.0, 10.0, 10.0, 10.0, 10.0, 10.0])
    out = world.make(monotonic=lambda: next(ticks, 10.0), **kw).run()
    assert len(world.fakes.facts_calls) == 1 and out["network_facts"]["stopped"] == stopped
    waiting = [p for p in "ABC" if rows(world)[p]["network_facts_state"] is None]
    assert len(waiting) == 2  # not gated, not lost: first in line next run
    world.make().run()
    read = [t for _, t in world.fakes.facts_calls]
    assert sorted(read) == sorted(x.lower() for x in (HYPER_A, HYPER_B, HYPER_C))  # each once, none twice


def test_a_transient_refusal_ends_the_run_and_retries_only_after_the_cooldown(world):
    world.fakes.facts_unknowns = ["network:rpc_evidence_unavailable"]
    world.intake.ingest([hyper("A", HYPER_A), hyper("B", HYPER_B)])
    out = world.make().run()
    assert len(world.fakes.facts_calls) == 1 and out["network_facts"]["stopped"] == "transient_refusal"
    first = world.fakes.facts_calls[0][1]
    world.make().run()  # the other card is read; the first one's retry is not due
    assert sorted(t for _, t in world.fakes.facts_calls) == sorted([HYPER_A.lower(), HYPER_B.lower()])
    assert all(rows(world)[p]["network_facts"]["state"] == "transient" for p in "AB")
    world.make().run()  # nothing due, nothing new: zero calls
    assert len(world.fakes.facts_calls) == 2
    world.fakes.facts_unknowns = []
    for _ in range(gp.FACTS_MAX_TRIES + 2):
        world.clock.at += gp.FACTS_RETRY_AFTER_S * 1000 + 1
        world.make().run()
    assert sum(t == first for _, t in world.fakes.facts_calls) == 2  # one retry, then complete: no more
    assert all(rows(world)[p]["network_facts"]["state"] == "complete" for p in "AB")
    assert world.fakes.intents == []


def test_a_read_that_never_recovers_stops_at_the_try_cap(world):
    world.fakes.facts_unknowns = ["network:rpc_evidence_unavailable"]
    world.intake.ingest([hyper("A", HYPER_A)])
    for _ in range(gp.FACTS_MAX_TRIES + 3):
        world.make().run()
        world.clock.at += gp.FACTS_RETRY_AFTER_S * 1000 + 1
    assert len(world.fakes.facts_calls) == gp.FACTS_MAX_TRIES
    assert rows(world)["A"]["network_facts"]["tries"] == gp.FACTS_MAX_TRIES


def test_page_loads_and_the_mcp_view_never_read_the_network(world):
    world.intake.ingest([hyper("Pos")])
    world.make().run()
    calls = list(world.fakes.facts_calls)
    for _ in range(3):
        gp.overview(20, intake=world.tmp / "inbox.db", pipeline=world.tmp / "pipeline.db", clock=world.clock)
        rows(world)
    assert world.fakes.facts_calls == calls


def test_the_real_reader_stops_at_a_refused_chain_proof_with_zero_extra_calls(world, monkeypatch):
    """Codex's hyper_intelligence + hyper_rpc with a fake transport: a 429-style refusal or a wrong
    chain on the first card's proof ends the run; no address read and no second card's request."""
    from kaiba.core.schemas import EvidenceBasis, Receipt
    from kaiba.providers import hyper_intelligence as hi
    from kaiba.providers import hyper_rpc
    from kaiba.providers._http import Fetched

    sent = []
    mode = {"basis": EvidenceBasis.UNAVAILABLE, "chain": "0x3e7"}

    def fetch(provider, endpoint, url, **kw):
        sent.append(kw["json_body"]["method"])
        receipt = Receipt(provider=provider, endpoint=endpoint, basis=mode["basis"], observed_at_ms=T0)
        body = kw["json_body"]
        return Fetched({"jsonrpc": "2.0", "id": body["id"], "result": mode["chain"]}, receipt)

    monkeypatch.setattr(hyper_rpc, "request_json", fetch)
    monkeypatch.setattr(hi, "request_json", lambda *a, **k: pytest.fail("address read after a refused proof"))
    monkeypatch.setattr(hyper_rpc, "now_ms", lambda: T0)
    settings = SimpleNamespace(hyperevm_rpc_url="https://rpc.hyperliquid.xyz/evm", elysium_rpc_url="")
    real = lambda network, token: hi.token_facts(network, token, settings=settings)  # noqa: E731
    world.intake.ingest([hyper("A", HYPER_A), hyper("B", HYPER_B), hyper("C", HYPER_C)])
    out = world.make(token_facts=real).run()  # the limiter refused (429 cooldown): basis unavailable
    assert sent == ["eth_chainId"] and out["network_facts"]["stopped"] == "transient_refusal"
    mode.update(basis=EvidenceBasis.PROVIDER_REPORTED, chain=hex(998))  # the testnet answering
    world.clock.at += gp.FACTS_RETRY_AFTER_S * 1000 + 1
    sent.clear()
    out = world.make(token_facts=real).run()
    assert sent == ["eth_chainId"] and out["network_facts"]["stopped"] == "transient_refusal"
    unknowns = [u for p in "ABC" for u in ((rows(world)[p]["network_facts"] or {}).get("unknowns") or [])]
    assert "network:rpc_chain_id_mismatch" in unknowns
    assert world.fakes.intents == []


@pytest.mark.parametrize("state", [
    # the canonical slug is clean; the sender's original wording is where the warning lives
    {"network_text": "HyperEVM (testnet998)"},
    {"network_text": "HyperEVM testnet2"},  # no word boundary and no 2-digit id: only the substring catches it
    {"network_text": "HyperEVM testnet"},
    {"network_text": "hyperevm, base"},
    {"assets": [{"role": "token", "address": HYPER_A, "network_slug": "hyperevm", "network_text": "HyperCore spot"}]},
    {"assets": [{"role": "token", "address": HYPER_A, "network_slug": "hyperevm", "_raw_slug": "hyperevm-testnet"}]},
    {"assets": [{"role": "token", "address": HYPER_A, "network_slug": "hyperevm", "network_text": "chain 998"}]},
])
def test_original_environment_text_in_the_card_state_is_gated_too(world, state):
    world.intake.ingest([hyper("Txt", **state)])
    world.make().run()
    assert world.fakes.facts_calls == [] and rows(world)["Txt"]["network_facts_state"] == "refused"


def test_clean_original_texts_are_the_positive_control(world):
    world.intake.ingest([hyper("Clean", network_text="HyperEVM mainnet (chain 999)",
                               assets=[{"role": "token", "address": HYPER_A, "network_slug": "hyperevm",
                                        "network_text": "HyperEVM"}])])
    world.make().run()
    assert world.fakes.facts_calls == [("hyperevm", HYPER_A.lower())]
    assert not gp.NON_MAINNET.search("hyperpool on hyperevm")  # "perp" only as a word


@pytest.mark.parametrize("transient, calls", [(True, 1), (False, 2)])
def test_two_valid_cards_and_a_transient_first_read_make_exactly_one_call(world, transient, calls):
    """Codex's pin: two VALID candidates; a refused first read must stop the run (negative),
    and without the refusal both are read (positive control)."""
    world.fakes.facts_unknowns = ["network:rpc_evidence_unavailable"] if transient else []
    world.intake.ingest([hyper("One", HYPER_A), hyper("Two", HYPER_B)])
    out = world.make().run()
    assert len(world.fakes.facts_calls) == calls
    assert out["network_facts"]["stopped"] == ("transient_refusal" if transient else None)


def test_the_view_reads_only_the_visible_rows_facts(world, monkeypatch):
    world.intake.ingest([hyper("A", HYPER_A), hyper("B", HYPER_B)])
    world.make().run()
    seen = []
    pipeline = gp.Pipeline(world.tmp / "pipeline.db", readonly=True)
    original = pipeline.connection

    from contextlib import contextmanager

    @contextmanager
    def spying():
        with original() as con:
            class Spy:
                def __getattr__(self, name):
                    return getattr(con, name)

                def execute(self, sql, *args):
                    seen.append((sql, args))
                    return con.execute(sql, *args)
            yield Spy()

    monkeypatch.setattr(pipeline, "connection", spying)
    view = pipeline.view(1)
    facts_sql = [(q, a) for q, a in seen if "FROM network_facts" in q]
    assert len(facts_sql) == 1 and "WHERE (network, token) IN" in facts_sql[0][0]
    assert len(facts_sql[0][1][0]) == 2  # one visible row: one (network, token) key
    assert view["rows"][0]["network_facts"]["state"] == "complete"


# --------------------------------------------------------------------------------------
# first-sight vendor market observation (OBSERVED-MARKET-WIRING-CLAUDE-20261102)
# --------------------------------------------------------------------------------------


def test_a_proven_mainnet_token_gets_one_market_observation_and_nothing_else(world):
    world.intake.ingest([hyper("Mkt")])
    out = world.make().run()
    assert world.fakes.market_calls == [("hyperevm", HYPER_A.lower())]
    assert world.fakes.intents == [] and world.fakes.scans == []
    assert out["network_market"]["read"] == 1 and out["network_market"]["method_attempts_upper_bound"] == 1
    m = rows(world)["Mkt"]["network_market"]
    assert m["state"] == "complete" and m["selected_price_pair"]["requested_token_price_usd"] == "0.0123"
    assert m["execution_ready"] is False and m["executable_quote"] is None and m["trading_inference"] is None
    assert "private" not in json.dumps(m) and "secret_field" not in json.dumps(m)  # named fields only
    assert "executable_market_not_built" in rows(world)["Mkt"]["blockers"]  # routing unchanged
    world.make().run()
    assert len(world.fakes.market_calls) == 1  # first sight only: no repeat polling


@pytest.mark.parametrize("network, identity", [
    ("hyperevm (testnet, chain 998)", MAINNET),
    ("hyperevm", {**MAINNET, "chain_id": 998}),
    ("hyperevm", None),
    ("elysium", {**MAINNET, "family": "elysium", "chain_id": 99801}),
])
def test_no_market_request_without_the_mainnet_gate(world, network, identity):
    world.intake.ingest([hyper("Gate", network=network, identity=identity)])
    world.make().run()
    assert world.fakes.facts_calls == [] and world.fakes.market_calls == []


def test_no_market_request_without_a_metadata_proof(world):
    world.fakes.facts_code = False  # the facts read found no contract code
    world.intake.ingest([hyper("NoCode")])
    out = world.make().run()
    assert len(world.fakes.facts_calls) == 1 and world.fakes.market_calls == []
    assert out["network_market"]["refused"] == 1
    assert rows(world)["NoCode"]["network_market"]["reasons"] == ["contract_code_not_proven"]
    world.make().run()
    assert world.fakes.market_calls == []  # a refusal is final: no later request


def test_no_market_request_while_the_facts_read_is_transient(world):
    world.fakes.facts_unknowns = ["network:rpc_evidence_unavailable"]
    world.intake.ingest([hyper("Wait")])
    world.make().run()
    assert world.fakes.market_calls == []


def test_one_market_request_per_token_however_many_cards(world):
    world.intake.ingest([hyper("First"), hyper("Second"), hyper("Third")])
    world.make().run()
    world.make().run()
    assert world.fakes.market_calls == [("hyperevm", HYPER_A.lower())]


@pytest.mark.parametrize("kw, stopped", [
    ({"market_per_run": 1}, "candidate_budget"),
    ({"market_request_budget": 1}, "request_budget"),
    ({"market_seconds": 8}, "time_budget"),  # one 5 s call fits, the second does not
])
def test_market_budgets_cap_requests_and_carry_the_rest(world, kw, stopped):
    world.intake.ingest([hyper("A", HYPER_A), hyper("B", HYPER_B), hyper("C", HYPER_C)])
    now = {"s": 0.0}
    fake = world.fakes.token_market

    def slow_market(network, token):  # each vendor request takes 4 s of the run's clock
        now["s"] += 4.0
        return fake(network, token)

    out = world.make(monotonic=lambda: now["s"], token_market=slow_market, **kw).run()
    assert len(world.fakes.market_calls) == 1 and out["network_market"]["stopped"] == stopped
    world.make().run()
    world.make().run()
    assert sorted(t for _, t in world.fakes.market_calls) == sorted(x.lower() for x in (HYPER_A, HYPER_B, HYPER_C))


@pytest.mark.parametrize("transient, calls", [(True, 1), (False, 2)])
def test_a_transient_market_answer_stops_the_run(world, transient, calls):
    world.fakes.market_unknowns = ["market_evidence_unavailable"] if transient else []
    world.intake.ingest([hyper("One", HYPER_A), hyper("Two", HYPER_B)])
    out = world.make().run()
    assert len(world.fakes.market_calls) == calls
    assert out["network_market"]["stopped"] == ("transient_refusal" if transient else None)


def test_a_transient_market_answer_retries_after_the_cooldown_up_to_the_cap(world):
    world.fakes.market_unknowns = ["market_evidence_unavailable"]
    world.intake.ingest([hyper("A", HYPER_A)])
    for _ in range(gp.MARKET_MAX_TRIES + 3):
        world.make().run()
        world.make().run()  # not due yet: no extra call
        world.clock.at += gp.MARKET_RETRY_AFTER_S * 1000 + 1
    assert len(world.fakes.market_calls) == gp.MARKET_MAX_TRIES
    assert rows(world)["A"]["network_market"]["state"] == "transient"


def test_a_vendor_answer_without_a_base_price_is_partial_and_final(world):
    world.fakes.market_unknowns = ["requested_token_price_unavailable"]
    world.intake.ingest([hyper("Q")])
    world.make().run()
    world.clock.at += gp.MARKET_RETRY_AFTER_S * 1000 + 1
    world.make().run()
    assert len(world.fakes.market_calls) == 1 and rows(world)["Q"]["network_market"]["state"] == "partial"


def test_page_loads_never_request_the_market(world):
    world.intake.ingest([hyper("Pos")])
    world.make().run()
    calls = list(world.fakes.market_calls)
    for _ in range(3):
        gp.overview(20, intake=world.tmp / "inbox.db", pipeline=world.tmp / "pipeline.db", clock=world.clock)
    assert world.fakes.market_calls == calls


def test_the_real_provider_stops_at_a_refused_request_with_zero_extra_calls(world, monkeypatch):
    """Codex's observed_market over a fake transport: a 429/limiter refusal (receipt basis
    unavailable) on the first token ends the run; the second token makes no request."""
    from kaiba.core.schemas import EvidenceBasis, Receipt
    from kaiba.providers import observed_market as market
    from kaiba.providers._http import Fetched

    sent = []

    def fetch(provider, endpoint, url, **kw):
        sent.append(url)
        return Fetched(None, Receipt(provider=provider, endpoint=endpoint, basis=EvidenceBasis.UNAVAILABLE, observed_at_ms=T0))

    monkeypatch.setattr(market, "get_json", fetch)
    monkeypatch.setattr(market, "now_ms", lambda: T0)
    real = lambda network, token: market.token_market(network, token)  # noqa: E731
    world.intake.ingest([hyper("A", HYPER_A), hyper("B", HYPER_B)])
    out = world.make(token_market=real).run()
    assert len(sent) == 1 and out["network_market"]["stopped"] == "transient_refusal"
    assert world.fakes.intents == []


def test_alpha_page_shows_the_stored_market_observation(console, tmp_db, tmp_path):
    clock = Clock()
    Store(tmp_path / "grok-alpha" / "inbox.db", clock=clock).ingest([hyper("MarketRow")])
    fakes = Fakes()
    gp.Processor(tmp_db, clock=clock, scan=fakes.scan, intent=fakes.intent, load_dossier=fakes.load,
                 token_facts=fakes.token_facts, token_market=fakes.token_market).run()
    page = console.get("/alpha").text
    assert "market (complete): $0.0123 \u00b7 liq $45678 \u00b7 1 pools" in page
    assert len(fakes.market_calls) == 1
