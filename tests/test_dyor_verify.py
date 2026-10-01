"""Provider verification: the mappings, checked against recorded live responses.

``tests/test_dyor.py`` replays hand-built fixtures and proves the merge behaves. It cannot
prove the adapters read the fields providers actually send, because its fixtures were
written from documentation by the same person who wrote the parsers. Everything here
replays **recorded live bodies** from ``tests/fixtures/dyor_live/`` instead, so each test
fails if a provider changes shape or if someone re-guesses a mapping that live data settled.

The load-bearing ones, each pinned to the response that proved the old mapping wrong:

* ``test_creator_rug_history_is_not_a_rugged_token`` — the substring collision that held 45
  of this machine's 59 quarantines on a single blocker.
* ``test_usdc_is_never_reported_as_revoked`` — the negative control. If this passes while
  the authority mappings are broken, nothing else here matters.
* ``test_renounced_pausable_is_not_a_freeze_authority`` / ``test_live_owner_pausable_still_blocks``
  — the first judgement mapping, and its control.
* ``test_reclaimable_ownership_is_not_balance_mutable`` / ``test_owner_change_balance_is_balance_mutable``
  — the second judgement mapping, and its control.
* ``test_rugcheck_unindexed_zero_is_not_a_measurement`` — 0 is not a liquidity reading.
* ``test_transfer_fee_is_read_in_basis_points`` — both providers, against the mint's own
  ``transferFeeConfig``.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from kaiba.core.schemas import Chain, EvidenceBasis, Grade, TokenRisk
from kaiba.intelligence import dyor
from kaiba.intelligence import dyor_verify as dv
from kaiba.providers import goplus, rugcheck

LIVE_FIXTURES = Path(__file__).parent / "fixtures" / "dyor_live"


def recording(slug: str) -> dict[str, Any]:
    return json.loads((LIVE_FIXTURES / f"{slug}.json").read_text(encoding="utf-8"), parse_float=Decimal)


def goplus_props(slug: str, chain: Chain) -> dict[str, Any]:
    return goplus.normalize_security(recording(slug)["goplus"], chain)


def rugcheck_report_props(slug: str) -> dict[str, Any]:
    return rugcheck.normalize_report(recording(slug)["rugcheck"]["report"])


def rugcheck_summary_props(slug: str) -> dict[str, Any]:
    return rugcheck.normalize_summary(recording(slug)["rugcheck"]["summary"])


# --------------------------------------------------------------------------------------
# the recordings themselves
# --------------------------------------------------------------------------------------


def test_the_reference_set_is_actually_recorded():
    """Every reference token has a recording, or the offline suite is quietly measuring less."""
    missing = [t.slug for t in dv.REFERENCE if not (LIVE_FIXTURES / f"{t.slug}.json").is_file()]
    assert missing == [], f"no recording for {missing}"


def test_recordings_carry_chain_truth_not_just_provider_output():
    """A recording without the mint account is a provider echo chamber, not evidence."""
    for token in dv.REFERENCE:
        assert recording(token.slug)["chain"] is not None, token.slug


def test_replay_reproduces_the_reference_set():
    result = dv.replay(LIVE_FIXTURES)
    assert len(result.reports) == len(dv.REFERENCE)
    assert result.source == "replay"
    assert result.table(), "the agreement table must not be empty"


# --------------------------------------------------------------------------------------
# the negative controls: a provider that gets these wrong cannot be used at all
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("slug", ["sol-usdc", "sol-usdt"])
def test_usdc_is_never_reported_as_revoked(slug: str):
    """Circle and Tether hold both authorities. Both providers must say so.

    This is the single check the exclusion layer rests on: if a provider reports a live
    freeze authority as revoked, every clean dossier it produces is worthless.
    """
    truth = dv.replay_one(recording(slug)).truth
    assert truth.mint_authority and truth.freeze_authority, "the mint account must show both live"
    gp = goplus_props(slug, Chain.SOL)
    rc = rugcheck_report_props(slug)
    assert gp["mint_authority_revoked"] is False
    assert gp["freeze_authority_revoked"] is False
    assert rc["mint_authority_revoked"] is False
    assert rc["freeze_authority_revoked"] is False


@pytest.mark.parametrize("slug", ["sol-wsol", "sol-bonk", "sol-jup"])
def test_revoked_authorities_are_reported_as_revoked(slug: str):
    gp = goplus_props(slug, Chain.SOL)
    rc = rugcheck_report_props(slug)
    assert gp["mint_authority_revoked"] is True and gp["freeze_authority_revoked"] is True
    assert rc["mint_authority_revoked"] is True and rc["freeze_authority_revoked"] is True


def test_every_authority_claim_matches_the_mint_account():
    """Across the whole Solana set, zero disagreements on the two authority properties."""
    result = dv.replay(LIVE_FIXTURES)
    wrong = [
        (label, check.render())
        for label, check in result.disagreements
        if check.prop in ("mint_authority_revoked", "freeze_authority_revoked")
    ]
    assert wrong == []


def test_no_property_disagrees_with_chain_truth():
    """The whole point. Any new disagreement is either a provider drift or a bad mapping."""
    result = dv.replay(LIVE_FIXTURES)
    assert [(label, c.render()) for label, c in result.disagreements] == []


# --------------------------------------------------------------------------------------
# RugCheck: the rugged substring collision
# --------------------------------------------------------------------------------------


def test_creator_rug_history_is_not_a_rugged_token():
    """RugCheck's "Creator history of rugged tokens" is about the developer, not the token.

    The marker table matched the substring "rugged" and set ``rugged=True``, which is the
    ``already_rugged`` BLOCKER. The recorded report says ``rugged: false`` in the same body.
    """
    report = recording("sol-pumpcollapse")["rugcheck"]["report"]
    names = [r["name"] for r in report["risks"]]
    assert any("Creator history of rugged tokens" == n for n in names), names
    assert report["rugged"] is False

    props = rugcheck.normalize_report(report)
    assert props["rugged"] is False, "the report's own rugged flag must win"
    assert props["creator_rug_count"] == Decimal(1), "the creator's history must not be discarded"


def test_creator_rug_history_downgrades_instead_of_quarantining():
    """End to end: the same token is now a warning, not a refusal."""
    report = next(r for r in dv.replay(LIVE_FIXTURES).reports if r.token.label == "PUMPCOLLAPSE")
    dossier = dv.dossier_preview(report)
    assert TokenRisk.RUG_HISTORY in dossier.warnings
    assert dossier.blockers == []
    assert dossier.grade is not Grade.QUARANTINED


def test_a_token_rugged_risk_still_sets_rugged():
    """Narrowing the marker must not switch the real signal off."""
    props = rugcheck.normalize_summary({"risks": [{"name": "Token has been rugged"}]})
    assert props["rugged"] is True
    assert "creator_rug_count" not in props


def test_summary_route_also_splits_the_two_claims():
    props = rugcheck.normalize_summary({"risks": [{"name": "Creator history of rugged tokens"}]})
    assert props.get("rugged") is None
    assert props["creator_rug_count"] == Decimal(1)


# --------------------------------------------------------------------------------------
# RugCheck: zero is not a measurement
# --------------------------------------------------------------------------------------


def test_rugcheck_unindexed_zero_is_not_a_measurement():
    """USDC's report reads 0 liquidity, 0 holders and price 0, with markets and topHolders null."""
    report = recording("sol-usdc")["rugcheck"]["report"]
    assert report["totalMarketLiquidity"] == 0 and report["totalHolders"] == 0
    assert report["markets"] is None

    props = rugcheck.normalize_report(report)
    assert "liquidity_usd" not in props
    assert "holder_count" not in props
    assert "price_usd" not in props


def test_a_real_near_zero_liquidity_is_still_reported():
    """The guard must not swallow a genuinely dead pool: 2.17e-07 USD is a measurement."""
    props = rugcheck_report_props("sol-pumpdead")
    assert props["liquidity_usd"] > 0


def test_unindexed_zero_no_longer_fires_low_liquidity_on_usdc():
    report = next(r for r in dv.replay(LIVE_FIXTURES).reports if r.token.label == "USDC")
    dossier = dv.dossier_preview(report)
    assert TokenRisk.LOW_LIQUIDITY not in dossier.warnings
    assert TokenRisk.PROVIDER_CONFLICT not in dossier.warnings


def test_goplus_zero_holder_count_is_unknown():
    """Binance-Peg USDT returns holder_count "0" for a token with millions of holders."""
    assert recording("bsc-bscusd")["goplus"]["holder_count"] == "0"
    assert "holder_count" not in goplus_props("bsc-bscusd", Chain.BSC)


# --------------------------------------------------------------------------------------
# transfer fees: both providers, against the mint's own extension
# --------------------------------------------------------------------------------------


def test_transfer_fee_is_read_in_basis_points():
    """FluxBot charges 300 bps. GoPlus spells it 0.03 under a key nobody read; RugCheck's
    top-level transferFee says 0 while its own copy of the extension says 300."""
    rec = recording("sol-fluxb")
    chain_bps = rec["chain"]["data"]["parsed"]["info"]
    fee_ext = next(e for e in chain_bps["extensions"] if e["extension"] == "transferFeeConfig")
    assert Decimal(str(fee_ext["state"]["newerTransferFee"]["transferFeeBasisPoints"])) == 300

    assert Decimal(str(rec["goplus"]["transfer_fee"]["current_fee_rate"]["fee_rate"])) == Decimal("0.03")
    assert goplus_props("sol-fluxb", Chain.SOL)["transfer_fee_bps"] == Decimal(300)

    assert rec["rugcheck"]["report"]["transferFee"]["pct"] == 0
    assert rugcheck_report_props("sol-fluxb")["transfer_fee_bps"] == Decimal(300)


def test_a_zero_fee_token_2022_still_reads_zero():
    """PYUSD carries the fee extension set to 0 bps; that is a measurement, not an absence."""
    assert goplus_props("sol-pyusd", Chain.SOL)["transfer_fee_bps"] == Decimal(0)
    assert rugcheck_report_props("sol-pyusd")["transfer_fee_bps"] == Decimal(0)


def test_transfer_fee_becomes_a_sell_cost():
    """The merge derives a sell tax from the fee, so getting the fee wrong mispriced exits."""
    report = next(r for r in dv.replay(LIVE_FIXTURES).reports if r.token.label == "FLUXB")
    dossier = dv.dossier_preview(report)
    assert dossier.sell_tax_bps.value == Decimal(300)
    assert TokenRisk.TRANSFER_FEE in dossier.warnings


# --------------------------------------------------------------------------------------
# judgement mapping 1: transfer_pausable -> freeze authority
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("slug", ["eth-pepe", "eth-bobo"])
def test_renounced_pausable_is_not_a_freeze_authority(slug: str):
    """A pause function nobody can call is not an authority. Chain agrees the owner is gone."""
    rec = recording(slug)
    assert rec["goplus"]["transfer_pausable"] == "1"
    assert rec["goplus"]["owner_address"] == "0x0000000000000000000000000000000000000000"
    # eth_call owner() — the provider's own owner_address is not what we trust here.
    assert rec["chain"]["owner"].endswith("0" * 40)

    props = goplus.normalize_security(rec["goplus"], Chain.ETH)
    assert "freeze_authority_revoked" not in props, "unknown, not a blocker and not a clean bill"


@pytest.mark.parametrize("slug", ["eth-pepe", "eth-bobo"])
def test_renounced_pausable_token_is_not_quarantined(slug: str):
    label = {"eth-pepe": "PEPE", "eth-bobo": "BOBO"}[slug]
    report = next(r for r in dv.replay(LIVE_FIXTURES).reports if r.token.label == label)
    dossier = dv.dossier_preview(report)
    assert TokenRisk.FREEZE_AUTHORITY not in dossier.blockers
    assert dossier.grade is not Grade.QUARANTINED
    # The reduced coverage is declared rather than hidden.
    assert TokenRisk.UNKNOWN_SAFETY in dossier.warnings


def test_live_owner_pausable_still_blocks():
    """WBTC: transfer_pausable=1 with an owner that exists on chain. Must still block."""
    rec = recording("eth-wbtc")
    assert rec["goplus"]["transfer_pausable"] == "1"
    assert not rec["chain"]["owner"].endswith("0" * 40)

    props = goplus.normalize_security(rec["goplus"], Chain.ETH)
    assert props["freeze_authority_revoked"] is False

    report = next(r for r in dv.replay(LIVE_FIXTURES).reports if r.token.label == "WBTC")
    assert TokenRisk.FREEZE_AUTHORITY in dv.dossier_preview(report).blockers


def test_reclaimable_ownership_keeps_a_dormant_pause_live():
    """Renounced but reclaimable is not renounced: the pessimistic reading has to survive."""
    props = goplus.normalize_security(
        {
            "transfer_pausable": "1",
            "owner_address": "0x0000000000000000000000000000000000000000",
            "can_take_back_ownership": "1",
        },
        Chain.ETH,
    )
    assert props["freeze_authority_revoked"] is False


def test_hidden_owner_keeps_a_dormant_pause_live():
    props = goplus.normalize_security(
        {"transfer_pausable": "1", "owner_address": "", "hidden_owner": "1"}, Chain.ETH
    )
    assert props["freeze_authority_revoked"] is False


def test_unknown_owner_stays_pessimistic():
    """GoPlus documents a missing owner_address as unknown, which is not the same as none."""
    props = goplus.normalize_security({"transfer_pausable": "1"}, Chain.ETH)
    assert props["freeze_authority_revoked"] is False


def test_not_pausable_is_still_a_clean_read():
    props = goplus.normalize_security({"transfer_pausable": "0"}, Chain.ETH)
    assert props["freeze_authority_revoked"] is True


# --------------------------------------------------------------------------------------
# judgement mapping 2: can_take_back_ownership -> balance mutable
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("slug", ["bsc-safemoon", "bsc-pit"])
def test_reclaimable_ownership_is_not_balance_mutable(slug: str):
    """Reclaiming ownership is not rewriting balances; GoPlus has a separate field for that."""
    rec = recording(slug)
    assert rec["goplus"]["can_take_back_ownership"] == "1"
    assert rec["goplus"]["owner_change_balance"] == "0"

    props = goplus.normalize_security(rec["goplus"], Chain.BSC)
    assert props["balance_mutable"] is False


@pytest.mark.parametrize("label", ["SAFEMOON", "PIT"])
def test_reclaimable_ownership_no_longer_blocks_on_mint_authority(label: str):
    report = next(r for r in dv.replay(LIVE_FIXTURES).reports if r.token.label == label)
    dossier = dv.dossier_preview(report)
    # These remain quarantined on their modifiable tax, which is a different and real reason.
    assert TokenRisk.MINT_AUTHORITY not in dossier.blockers


def test_owner_change_balance_is_balance_mutable():
    """VIRTUAL is the genuine case, and the corrected mapping must still catch it."""
    rec = recording("base-virtual")
    assert rec["goplus"]["owner_change_balance"] == "1"
    assert goplus.normalize_security(rec["goplus"], Chain.BASE)["balance_mutable"] is True


def test_solana_balance_mutable_matches_the_permanent_delegate():
    """PYUSD's permanent delegate is the chain fact balance_mutable_authority describes."""
    rec = recording("sol-pyusd")
    delegate = next(
        e for e in rec["chain"]["data"]["parsed"]["info"]["extensions"] if e["extension"] == "permanentDelegate"
    )
    assert delegate["state"]["delegate"]
    assert goplus_props("sol-pyusd", Chain.SOL)["balance_mutable"] is True
    # And a mint without one reads False rather than unknown.
    assert goplus_props("sol-bonk", Chain.SOL)["balance_mutable"] is False


def test_tax_modifiable_matches_the_fee_config_authority():
    """A Token-2022 fee is raisable exactly when transferFeeConfigAuthority is set.

    PYUSD's is set, so today's 0 bps is not a promise; FluxBot's is null, so its 300 bps
    cannot be raised. Both are chain facts, which makes this blocker-backing property
    checkable rather than a provider claim taken on trust.
    """
    pyusd = dv.replay_one(recording("sol-pyusd")).truth
    fluxb = dv.replay_one(recording("sol-fluxb")).truth
    assert pyusd.transfer_fee_authority is not None
    assert fluxb.transfer_fee_authority is None

    assert goplus_props("sol-pyusd", Chain.SOL)["tax_modifiable"] is True
    assert goplus_props("sol-fluxb", Chain.SOL)["tax_modifiable"] is False
    assert dv.replay_one(recording("sol-bonk")).truth.properties()["tax_modifiable"] is False


def test_a_hook_slot_is_not_an_installed_hook():
    """PYUSD carries the transferHook extension with programId null: nothing is installed."""
    rec = recording("sol-pyusd")
    hook = next(
        e for e in rec["chain"]["data"]["parsed"]["info"]["extensions"] if e["extension"] == "transferHook"
    )
    assert hook["state"]["programId"] is None
    assert goplus_props("sol-pyusd", Chain.SOL)["transfer_hook"] is False


# --------------------------------------------------------------------------------------
# LP percentages
# --------------------------------------------------------------------------------------


def test_uninterpretable_lp_percent_is_unknown_not_fully_locked():
    """GoPlus's Solana lp_holders percent is not a share: USDC's top row reads 199651036.37."""
    rows = recording("sol-usdc")["goplus"]["lp_holders"]
    assert Decimal(str(rows[0]["percent"])) > 100

    locked = [dict(row, is_locked=1) for row in rows]
    assert goplus._locked_lp_pct(locked) is None, "an out-of-range sum must not read as locked LP"


def test_evm_lp_percent_is_still_a_fraction():
    """BOBO's burn row is the documented 0-1 fraction, and must keep working."""
    props = goplus_props("eth-bobo", Chain.ETH)
    assert Decimal(99) < props["lp_burned_pct"] <= Decimal(100)


# --------------------------------------------------------------------------------------
# chain truth plumbing
# --------------------------------------------------------------------------------------


def test_chain_truth_reads_authorities_from_the_mint_account():
    truth = dv.replay_one(recording("sol-usdc")).truth
    assert truth.ok
    assert truth.mint_authority == "BJE5MMbqXjVwjAF7oxwPYXnTXDyspzZyt4vwenNw5ruG"
    assert truth.freeze_authority == "7dGbd2QZcCKcTndnHcTL8q7SMVXAkp688NTQYwrRCrar"
    assert truth.properties()["mint_authority_revoked"] is False


def test_chain_truth_never_asserts_that_a_token_is_sellable():
    """A mint account can prove a transfer is blocked; it cannot prove one will succeed."""
    for slug in ("sol-usdc", "sol-bonk", "sol-pyusd"):
        assert "can_sell" not in dv.replay_one(recording(slug)).truth.properties()


def test_evm_chain_truth_classifies_the_owner():
    assert dv.replay_one(recording("eth-pepe")).truth.owner_renounced is True
    assert dv.replay_one(recording("eth-wbtc")).truth.owner_renounced is False


def test_absent_is_counted_separately_from_wrong():
    """A silent provider must never be scored as a disagreement."""
    truth = dv.replay_one(recording("sol-usdc")).truth
    check = dv._compare("cluster_pct", "goplus", None, truth.properties())
    assert check.verdict is dv.Verdict.ABSENT


def test_table_marks_the_blocker_properties():
    rows = dv.replay(LIVE_FIXTURES).table()
    blockers = {r["property"] for r in rows if r["blocker"]}
    assert {"mint_authority_revoked", "freeze_authority_revoked", "can_sell", "rugged"} <= blockers


# --------------------------------------------------------------------------------------
# GoPlus authentication
# --------------------------------------------------------------------------------------


class Stub:
    """Minimal httpx stub; the auth probe is about status bodies, not payload shapes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, bodies: dict[str, Any]) -> None:
        self.bodies = bodies
        self.calls: list[dict[str, Any]] = []
        monkeypatch.setattr(httpx, "request", self._request)
        import time as _time

        monkeypatch.setattr(_time, "sleep", lambda _s: None)

    def _request(self, method: str, url: Any, **kw: Any) -> httpx.Response:
        full = str(url)
        self.calls.append({"url": full, "headers": dict(kw.get("headers") or {})})
        for suffix, body in self.bodies.items():
            if full.endswith(suffix):
                has_auth = "Authorization" in (kw.get("headers") or {})
                chosen = body[1] if isinstance(body, tuple) and has_auth else (
                    body[0] if isinstance(body, tuple) else body
                )
                return httpx.Response(200, json=chosen, request=httpx.Request(method, full))
        raise httpx.ConnectError(f"no route for {full}", request=httpx.Request(method, full))


@pytest.fixture
def no_credentials(tmp_db, tmp_path, monkeypatch):
    for var in ("GOPLUS_APP_KEY", "GOPLUS_APP_SECRET", "RUGCHECK_JWT"):
        monkeypatch.setenv(var, "")
    risk = tmp_path / "risk.yaml"
    risk.write_text(
        "version: v1\nglobal_mode: shadow\nprovider_budgets:\n"
        "  goplus: {min_interval_ms: 0, capacity: 500, refill_per_s: 500}\n"
        "  rpc: {min_interval_ms: 0, capacity: 500, refill_per_s: 500}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KAIBA_RISK_PATH", str(risk))
    from kaiba.core.config import get_settings

    get_settings.cache_clear()
    goplus.reset_credentials_cache()
    yield
    goplus.reset_credentials_cache()
    get_settings.cache_clear()


def test_auth_probe_reports_keyless_working_and_a_bad_token_failing(no_credentials, monkeypatch, tmp_db):
    """The recorded live behaviour: keyless answers, an invalid Authorization returns 4012."""
    Stub(
        monkeypatch,
        {
            "solana/token_security": ({"code": 1, "result": {}}, {"code": 4012, "message": "signature verification failure"}),
            "/api/v1/token": {"code": 4010, "message": None, "result": None},
        },
    )
    finding = dv.verify_goplus_auth(conn=tmp_db)

    assert finding.keyless_ok is True
    assert finding.bad_token_code == 4012
    assert finding.credentials_configured is False
    assert finding.signin_code == 4010
    joined = " ".join(finding.render())
    assert "blinds the scanner" in joined
    assert "UNVERIFIED" in joined


def test_a_wrong_credential_is_not_silently_downgraded(no_credentials, monkeypatch, tmp_db):
    """The failure that would otherwise hide: 4012 comes back inside an HTTP 200."""
    Stub(monkeypatch, {"solana/token_security": {"code": 4012, "message": "signature verification failure"}})
    fetched = goplus.token_security(dv.REFERENCE[0].address, Chain.SOL, conn=tmp_db)
    assert not fetched.ok
    assert fetched.receipt.basis is EvidenceBasis.UNAVAILABLE
    assert "4012" in (fetched.receipt.note or "")


def test_goplus_status_codes_are_documented():
    assert dv.GOPLUS_CODES[4029] == "request limit reached"
    assert dv.GOPLUS_CODES[4012] == "wrong signature"


# --------------------------------------------------------------------------------------
# the merge still behaves on live data
# --------------------------------------------------------------------------------------


def test_no_reference_token_is_quarantined_on_an_unknown():
    """A blocker must always name a measured fact, never the absence of one."""
    for report in dv.replay(LIVE_FIXTURES).reports:
        dossier = dv.dossier_preview(report)
        if TokenRisk.UNKNOWN_SAFETY in dossier.blockers:
            pytest.fail(f"{report.token.label} was refused for lack of evidence, not for a fact")


def test_stablecoin_issuers_are_still_refused():
    """Not a bug: Circle can freeze USDC, so a memecoin scanner refusing it is correct."""
    for label in ("USDC", "USDT"):
        report = next(r for r in dv.replay(LIVE_FIXTURES).reports if r.token.label == label)
        dossier = dv.dossier_preview(report)
        assert TokenRisk.FREEZE_AUTHORITY in dossier.blockers
        assert dossier.grade is Grade.QUARANTINED


def test_clean_pump_tokens_are_not_refused():
    for label in ("PUMPCURVE", "PUMPGRAD"):
        report = next(r for r in dv.replay(LIVE_FIXTURES).reports if r.token.label == label)
        assert dv.dossier_preview(report).blockers == []


def test_dyor_rules_still_load():
    """dyor.py is not ours to edit; this pins that the verification reads the shipped rules."""
    assert dyor.RULES
    assert "rugged" in dyor.BOOL_PROPERTIES


# --------------------------------------------------------------------------------------
# live — skipped unless KAIBA_LIVE_TESTS=1
# --------------------------------------------------------------------------------------


@pytest.mark.live
def test_live_chain_truth_matches_the_recording(tmp_db):
    """Authorities do not change often; if this fails the recording is stale, not wrong."""
    truth = dv.chain_truth(dv.REFERENCE[0].address, Chain.SOL, conn=tmp_db)
    assert truth.ok, truth.receipt.note
    assert truth.mint_authority is not None, "USDC's mint authority is live"


@pytest.mark.live
def test_live_reconciliation_finds_no_new_disagreement(tmp_db):
    result = dv.reconcile(dv.REFERENCE[:4], conn=tmp_db)
    assert [c.render() for _, c in result.disagreements] == []


@pytest.mark.live
def test_live_goplus_auth(tmp_db):
    finding = dv.verify_goplus_auth(conn=tmp_db)
    assert finding.keyless_ok, finding.render()
