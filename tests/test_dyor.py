"""Token DYOR: the provider adapters, the merge, and the four rules that matter.

Everything here is offline. The fixtures under ``tests/fixtures/goplus/`` and
``tests/fixtures/rugcheck/`` are hand-built to each provider's documented response shape
(every file says so in its ``note``) and are replayed through the real
:mod:`kaiba.providers._http` stack — limiter, disk cache and all — by stubbing
``httpx.request``. That matters: a test that mocked the adapter functions would prove the
merge works on data the adapters can never actually produce.

The tests that are load-bearing rather than decorative:

* ``test_all_providers_down_*`` — a scan with every provider dead must produce unknowns and
  a refusal, not a clean bill of health, and ``mint_authority_revoked`` must be ``None``
  rather than ``False``.
* ``test_evidence_weight_orders_the_grades`` — three providers clearing a token, one
  provider clearing it, and nobody reaching it must not land in the same place.
* ``test_conflict_*`` — when GoPlus and RugCheck contradict each other the dossier keeps
  both claims and takes the unsafe reading; it does not let whichever adapter ran last win.
* ``test_every_blocker_carries_a_reason`` / ``test_blocker_set_is_exactly_this`` — the
  blocker list is pinned, so padding it is a test failure rather than a judgement call.
"""

from __future__ import annotations

import json
import sys
import types
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from kaiba.core import events
from kaiba.core.config import get_settings
from kaiba.core.schemas import Chain, EventKind, EvidenceBasis, Grade, Receipt, TokenRisk
from kaiba.intelligence import coverage, dyor
from kaiba.providers import goplus, rugcheck

GOPLUS_FIXTURES = Path(__file__).parent / "fixtures" / "goplus"
RUGCHECK_FIXTURES = Path(__file__).parent / "fixtures" / "rugcheck"

SOL_MINT = "So11111111111111111111111111111111111111112"
RISKY_MINT = "RiskYMint1111111111111111111111111111111111"
EVM_CLEAN = "0x1111111111111111111111111111111111111111"
EVM_TRAP = "0x2222222222222222222222222222222222222222"


def gp(name: str) -> Any:
    return json.loads((GOPLUS_FIXTURES / name).read_text(encoding="utf-8"))["body"]


def rc(name: str) -> Any:
    return json.loads((RUGCHECK_FIXTURES / name).read_text(encoding="utf-8"))["body"]


# --------------------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------------------


class Http:
    """Replays fixtures through the real provider stack by stubbing ``httpx.request``.

    Routes match on the URL suffix, which is unambiguous here because query parameters are
    passed separately by ``_http``. A request with no route raises ``ConnectError`` — the
    default is "this provider is unreachable", so a test that forgets to wire a provider
    exercises the outage path instead of silently passing.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.routes: list[tuple[str, int, Any]] = []
        self.calls: list[dict[str, Any]] = []
        monkeypatch.setattr(httpx, "request", self._request)
        import time as _time

        monkeypatch.setattr(_time, "sleep", lambda _s: None)

    def add(self, suffix: str, body: Any, status: int = 200) -> Http:
        self.routes.append((suffix, status, body))
        return self

    def urls(self) -> list[str]:
        return [c["url"] for c in self.calls]

    def _request(self, method: str, url: Any, **kw: Any) -> httpx.Response:
        full = str(url)
        self.calls.append(
            {
                "method": method,
                "url": full,
                "headers": dict(kw.get("headers") or {}),
                "params": dict(kw.get("params") or {}),
                "json": kw.get("json"),
            }
        )
        for suffix, status, body in self.routes:
            if full.endswith(suffix):
                request = httpx.Request(method, full)
                if isinstance(body, Exception):
                    raise body
                return httpx.Response(status, json=body, request=request)
        raise httpx.ConnectError(f"no stub route for {full}", request=httpx.Request(method, full))


@pytest.fixture
def http(tmp_db, tmp_path, monkeypatch):
    """Offline provider stack: no credentials, no rate-limit floor, isolated cache."""
    for var in ("GOPLUS_APP_KEY", "GOPLUS_APP_SECRET", "RUGCHECK_JWT"):
        monkeypatch.setenv(var, "")
    # The shipped limiter puts a 500 ms floor between calls to each provider, which would
    # rate-limit RugCheck's own three routes against each other inside one scan. The floor
    # is a real production constraint, so it is relaxed through the real config path.
    risk = tmp_path / "risk.yaml"
    risk.write_text(
        "version: v1\n"
        "global_mode: shadow\n"
        "provider_budgets:\n"
        "  goplus: {min_interval_ms: 0, capacity: 500, refill_per_s: 500}\n"
        "  rugcheck: {min_interval_ms: 0, capacity: 500, refill_per_s: 500}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KAIBA_RISK_PATH", str(risk))

    # Cut GMGN out of every scan these tests run. `collect_gmgn` shells out to the real
    # `gmgn-cli`, so with the binary installed -- as it is on the operator's machine and
    # the VPS -- this "offline provider stack" was making live, billed, non-deterministic
    # network calls on every run, against AGENTS.md rule 7 and docs/CONTRACT.md.
    #
    # It went unnoticed for one reason: a bug in `dyor._unwrap_gmgn` discarded everything
    # GMGN returned, so the calls were invisible in the assertions. That bug was fixed on
    # 2026-09-21 and nine tests in this file immediately began failing on live data that
    # changes between runs. The calls were always happening; only the silence was new.
    #
    # Tests that DO want GMGN behaviour install their own stub module (see the cases
    # around line 840), and that still works -- this only sets the default. A test that
    # needs a real provider belongs behind the `live` marker.
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", None)

    get_settings.cache_clear()
    goplus.reset_credentials_cache()
    yield Http(monkeypatch)
    goplus.reset_credentials_cache()
    get_settings.cache_clear()


def wire_solana(
    http: Http,
    *,
    security: str | None = "solana_clean.json",
    summary: str | None = "summary_clean.json",
    report: str | None = "report_clean.json",
    graph: str | None = "insiders_graph_empty.json",
    mint: str = SOL_MINT,
) -> Http:
    if security:
        http.add("solana/token_security", gp(security))
    if summary:
        http.add("/report/summary", rc(summary))
    if report:
        http.add(f"/tokens/{mint}/report", rc(report))
    if graph:
        http.add("/insiders/graph", rc(graph))
    return http


def resolution_of(pairs: dict[str, Any], provider: str = "test") -> dyor.Resolution:
    """A merged view built by hand, for testing rules without a provider round trip."""
    receipt = Receipt(provider=provider, endpoint="test.fixture")
    resolution = dyor.resolve([dyor.Claim(k, provider, v, receipt) for k, v in pairs.items()])
    resolution.providers_ok = {provider}
    return resolution


def provider_errors(conn) -> list:
    return events.recent(limit=50, kinds=[EventKind.PROVIDER_ERROR.value], conn=conn)


# --------------------------------------------------------------------------------------
# GoPlus adapter
# --------------------------------------------------------------------------------------


def test_goplus_solana_normalizes_authorities_lp_and_holders(http):
    wire_solana(http, summary=None, report=None, graph=None)
    props, receipt = goplus.security_properties(SOL_MINT, Chain.SOL)

    assert receipt.basis is EvidenceBasis.PROVIDER_REPORTED
    assert props["mint_authority_revoked"] is True
    assert props["freeze_authority_revoked"] is True
    assert props["metadata_mutable"] is False
    assert props["transfer_hook"] is False
    assert props["balance_mutable"] is False
    assert props["lp_burned_pct"] == Decimal("100.0")
    assert props["top10_pct"] == Decimal("18.000")
    assert props["holder_count"] == Decimal(1200)
    assert props["liquidity_usd"] == Decimal(300_000)
    # GoPlus reports a UI supply while RugCheck reports raw atoms; emitting it would
    # manufacture a conflict out of a units mismatch, so it is deliberately absent.
    assert "total_supply" not in props


def test_goplus_evm_maps_honeypot_pausability_and_taxes(http):
    http.add("/token_security/1", gp("evm_honeypot.json"))
    props, _ = goplus.security_properties(EVM_TRAP, Chain.ETH)

    assert props["can_sell"] is False
    assert props["mint_authority_revoked"] is False
    # An owner who can pause transfers removes the exit exactly as a freeze authority does.
    assert props["freeze_authority_revoked"] is False
    assert props["sell_tax_bps"] == Decimal("9900.00")
    assert props["tax_modifiable"] is True
    assert props["source_verified"] is False
    assert props["creator_rug_count"] == Decimal(3)
    assert props["dev_pct"] == Decimal("35.00")


def test_goplus_evm_excludes_burned_holders_from_concentration(http):
    http.add("/token_security/1", gp("evm_clean.json"))
    props, _ = goplus.security_properties(EVM_CLEAN, Chain.ETH)

    # The 30% sitting in the dead address is the opposite of a concentration risk.
    assert props["top10_pct"] == Decimal("19.000")
    assert props["lp_burned_pct"] == Decimal("100.0")


def test_goplus_refuses_uncovered_chains_without_spending_a_request(http):
    fetched = goplus.token_security(EVM_CLEAN, Chain.ROBINHOOD)
    assert not fetched.ok
    assert fetched.receipt.basis is EvidenceBasis.UNAVAILABLE
    assert http.calls == []


def test_goplus_error_body_is_unavailable_and_is_not_cached(http, tmp_db):
    http.add("solana/token_security", gp("error_code.json"))

    first = goplus.token_security(SOL_MINT, Chain.SOL, conn=tmp_db)
    assert not first.ok
    assert "4029" in (first.receipt.note or "")
    assert provider_errors(tmp_db), "a 200-with-error-code must still report a provider error"

    # A poisoned cache would answer the second call without touching the network.
    goplus.token_security(SOL_MINT, Chain.SOL, conn=tmp_db)
    assert len(http.calls) == 2


def test_goplus_runs_keyless_when_no_credentials_are_configured(http):
    wire_solana(http, summary=None, report=None, graph=None)
    goplus.security_properties(SOL_MINT, Chain.SOL)

    assert goplus.access_token() is None
    assert all("Authorization" not in c["headers"] for c in http.calls)
    assert not any(c["url"].endswith("/api/v1/token") for c in http.calls)


def test_goplus_signs_in_when_credentials_are_configured(http, monkeypatch):
    monkeypatch.setenv("GOPLUS_APP_KEY", "test-key")
    monkeypatch.setenv("GOPLUS_APP_SECRET", "test-secret")
    get_settings.cache_clear()
    goplus.reset_credentials_cache()
    http.add("/api/v1/token", {"code": 1, "message": "OK", "result": {"access_token": "tok", "expires_in": 3600}})
    wire_solana(http, summary=None, report=None, graph=None)

    goplus.security_properties(SOL_MINT, Chain.SOL)

    auth = [c for c in http.calls if c["url"].endswith("/api/v1/token")]
    assert len(auth) == 1
    # The secret is hashed into ``sign`` and must never be sent, logged or cached as-is.
    assert set(auth[0]["json"]) == {"app_key", "time", "sign"}
    assert "test-secret" not in json.dumps(auth[0]["json"])
    security = [c for c in http.calls if "token_security" in c["url"]][0]
    assert security["headers"]["Authorization"] == "tok"


def test_goplus_network_failure_is_unavailable_not_an_exception(http, tmp_db):
    props, receipt = goplus.security_properties(SOL_MINT, Chain.SOL, conn=tmp_db)
    assert props == {}
    assert receipt.basis is EvidenceBasis.UNAVAILABLE
    assert provider_errors(tmp_db)


def test_goplus_mangled_payload_degrades_to_unknown(http):
    http.add("solana/token_security", {"code": 1, "result": {SOL_MINT: "not-a-dict"}})
    props, receipt = goplus.security_properties(SOL_MINT, Chain.SOL)
    assert props == {}
    assert receipt.basis is EvidenceBasis.UNAVAILABLE
    assert goplus.normalize_security(["garbage"], Chain.SOL) == {}


# --------------------------------------------------------------------------------------
# RugCheck adapter
# --------------------------------------------------------------------------------------


def test_rugcheck_report_states_authorities_as_facts(http):
    props = rugcheck.normalize_report(rc("report_clean.json"))
    assert props["mint_authority_revoked"] is True
    assert props["freeze_authority_revoked"] is True
    assert props["token_2022"] is False
    assert props["dev_pct"] == Decimal(0)
    assert props["insider_pct"] == Decimal(0)
    assert props["total_supply"] == Decimal(1_000_000_000_000_000)


def test_rugcheck_summary_never_asserts_that_anything_is_safe():
    """An empty risk list is not evidence that the mint authority is revoked."""
    props = rugcheck.normalize_summary(rc("summary_clean.json"))
    assert "mint_authority_revoked" not in props
    assert "freeze_authority_revoked" not in props
    assert props["token_2022"] is False

    risky = rugcheck.normalize_summary(rc("summary_risky.json"))
    assert risky["mint_authority_revoked"] is False
    assert risky["freeze_authority_revoked"] is False
    assert risky["metadata_mutable"] is True


def test_rugcheck_top_holders_exclude_pool_accounts():
    props = rugcheck.normalize_report(rc("report_clean.json"))
    # The 55% parked in the Raydium base vault is liquidity, not a whale.
    assert props["top10_pct"] == Decimal("18.0")


def test_rugcheck_lp_locked_is_liquidity_weighted():
    payload = {
        "markets": [
            {"lp": {"lpLockedPct": 100.0, "baseUSD": 5, "quoteUSD": 5}},
            {"lp": {"lpLockedPct": 0.0, "baseUSD": 45_000, "quoteUSD": 45_000}},
        ]
    }
    props = rugcheck.normalize_report(payload)
    # A burned dust pool next to a live 90k pool is not "50% locked".
    assert props["lp_burned_pct"] < Decimal("1")


def test_rugcheck_insider_graph_is_unknown_without_a_supply():
    assert rugcheck.normalize_insider_graph(rc("insiders_graph.json"), None) == {}
    assert rugcheck.normalize_insider_graph(rc("insiders_graph.json"), Decimal(0)) == {}


def test_rugcheck_insider_graph_ignores_single_wallet_networks():
    props = rugcheck.normalize_insider_graph(rc("insiders_graph.json"), Decimal(1_000_000_000_000_000))
    # 25% + 10% from the two real clusters; the 9% solo "network" is just a holder.
    assert props["cluster_pct"] == Decimal(35)
    assert props["cluster_networks"] == Decimal(2)


def test_rugcheck_refuses_non_solana_chains_without_spending_a_request(http):
    fetched = rugcheck.report(EVM_CLEAN, Chain.ETH)
    assert not fetched.ok
    assert "Solana only" in (fetched.receipt.note or "")
    assert http.calls == []


def test_rugcheck_sends_the_jwt_when_one_is_configured(http, monkeypatch):
    monkeypatch.setenv("RUGCHECK_JWT", "jwt-value")
    get_settings.cache_clear()
    http.add(f"/tokens/{SOL_MINT}/report", rc("report_clean.json"))

    rugcheck.report(SOL_MINT)
    assert http.calls[0]["headers"]["Authorization"] == "Bearer jwt-value"


def test_rugcheck_dead_provider_returns_empty_properties(http, tmp_db):
    props, receipts = rugcheck.security_properties(SOL_MINT, conn=tmp_db)
    assert props == {}
    assert all(r.basis is EvidenceBasis.UNAVAILABLE for r in receipts)


def test_rugcheck_does_not_rate_limit_its_own_three_routes(tmp_db, tmp_path, monkeypatch):
    """Regression: with the shipped 500 ms floor, routes 2 and 3 were refused every scan.

    ``_http`` returns an UNAVAILABLE receipt on a limiter refusal rather than waiting, so
    without pacing the ``report`` route — the only one that can clear a token — silently
    never arrived. This test deliberately uses the real limiter defaults and real sleeps.
    """
    monkeypatch.setenv("KAIBA_DATA_DIR", str(tmp_path / "paced"))
    monkeypatch.setenv("KAIBA_RISK_PATH", str(tmp_path / "no-such-risk.yaml"))
    for var in ("GOPLUS_APP_KEY", "GOPLUS_APP_SECRET", "RUGCHECK_JWT"):
        monkeypatch.setenv(var, "")
    get_settings.cache_clear()

    real = Http.__new__(Http)
    real.routes, real.calls = [], []
    monkeypatch.setattr(httpx, "request", real._request)
    real.add("/report/summary", rc("summary_clean.json"))
    real.add(f"/tokens/{SOL_MINT}/report", rc("report_clean.json"))
    real.add("/insiders/graph", rc("insiders_graph_empty.json"))

    from kaiba.core.limiter import limits_for

    assert limits_for("rugcheck").min_interval_ms >= 500, "test is meaningless without the floor"

    props, receipts = rugcheck.security_properties(SOL_MINT, conn=tmp_db)
    assert all(r.basis is not EvidenceBasis.UNAVAILABLE for r in receipts), [
        (r.endpoint, r.basis.value, r.note) for r in receipts
    ]
    assert len(real.calls) == 3, real.urls()
    assert props["mint_authority_revoked"] is True  # came from the report, not the summary


# --------------------------------------------------------------------------------------
# the merge: unknowns and disagreement
# --------------------------------------------------------------------------------------


def test_unanswered_property_is_unknown_not_false():
    resolution = resolution_of({"top10_pct": Decimal(10)})
    assert resolution.b("mint_authority_revoked") is None
    assert "mint_authority_revoked" in resolution.unknown
    assert resolution.values.get("mint_authority_revoked") is None


def test_measured_false_is_not_the_same_as_unanswered():
    measured = resolution_of({"mint_authority_revoked": False})
    assert measured.b("mint_authority_revoked") is False
    assert "mint_authority_revoked" not in measured.unknown


def test_conflicting_booleans_resolve_pessimistically_and_are_flagged():
    good = Receipt(provider="goplus", endpoint="token.security_sol")
    bad = Receipt(provider="rugcheck", endpoint="token.report")
    resolution = dyor.resolve(
        [
            dyor.Claim("mint_authority_revoked", "goplus", True, good),
            dyor.Claim("mint_authority_revoked", "rugcheck", False, bad),
        ]
    )
    assert resolution.conflicts == ["mint_authority_revoked"]
    assert resolution.b("mint_authority_revoked") is False
    notes = dyor.conflict_notes(resolution)
    assert "goplus=True" in notes[0] and "rugcheck=False" in notes[0]
    assert {c.receipt.provider for c in resolution.claims["mint_authority_revoked"]} == {
        "goplus",
        "rugcheck",
    }


def test_conflicting_numbers_take_the_worse_side():
    receipt = Receipt(provider="p", endpoint="e")
    resolution = dyor.resolve(
        [
            dyor.Claim("top10_pct", "goplus", Decimal(20), receipt),
            dyor.Claim("top10_pct", "rugcheck", Decimal(65), receipt),
            dyor.Claim("lp_burned_pct", "goplus", Decimal(90), receipt),
            dyor.Claim("lp_burned_pct", "rugcheck", Decimal(10), receipt),
        ]
    )
    assert resolution.n("top10_pct") == Decimal(65)
    assert resolution.n("lp_burned_pct") == Decimal(10)
    assert set(resolution.conflicts) == {"top10_pct", "lp_burned_pct"}


def test_small_numeric_disagreement_is_not_a_conflict():
    receipt = Receipt(provider="p", endpoint="e")
    resolution = dyor.resolve(
        [
            dyor.Claim("liquidity_usd", "goplus", Decimal(300_000), receipt),
            dyor.Claim("liquidity_usd", "rugcheck", Decimal(305_000), receipt),
        ]
    )
    assert resolution.conflicts == []
    assert resolution.n("liquidity_usd") == Decimal(300_000)


def test_a_conflicted_value_is_marked_derived_not_provider_reported():
    receipt = Receipt(provider="p", endpoint="e", basis=EvidenceBasis.PROVIDER_REPORTED)
    resolution = dyor.resolve(
        [
            dyor.Claim("top10_pct", "goplus", Decimal(20), receipt),
            dyor.Claim("top10_pct", "rugcheck", Decimal(65), receipt),
        ]
    )
    assert resolution.measure("top10_pct").basis is EvidenceBasis.DERIVED


# --------------------------------------------------------------------------------------
# the merge: rules
# --------------------------------------------------------------------------------------


def _blockers(pairs: dict[str, Any]) -> list[TokenRisk]:
    return dyor.build_dossier("X", Chain.SOL, resolution_of(pairs)).blockers


def _warnings(pairs: dict[str, Any]) -> list[TokenRisk]:
    return dyor.build_dossier("X", Chain.SOL, resolution_of(pairs)).warnings


SAFE_BASE: dict[str, Any] = {
    "can_sell": True,
    "mint_authority_revoked": True,
    "freeze_authority_revoked": True,
    "transfer_hook": False,
    "balance_mutable": False,
    "tax_modifiable": False,
    "metadata_mutable": False,
    "lp_burned_pct": Decimal(100),
    "top10_pct": Decimal(10),
    "dev_pct": Decimal(1),
    "liquidity_usd": Decimal(500_000),
}


def test_cluster_thresholds_follow_the_operator_mandate():
    """PLAN §5.5: unexplained cluster >20% is a review, >30% is a reject."""
    ok = _warnings({**SAFE_BASE, "cluster_pct": Decimal(15)})
    review = _warnings({**SAFE_BASE, "cluster_pct": Decimal(25)})
    reject = _blockers({**SAFE_BASE, "cluster_pct": Decimal(35)})

    assert TokenRisk.CLUSTER_CONCENTRATION not in ok
    assert TokenRisk.CLUSTER_CONCENTRATION in review
    assert TokenRisk.CLUSTER_CONCENTRATION in reject
    # A rejected cluster must not also be filed as a warning; it is one problem.
    assert TokenRisk.CLUSTER_CONCENTRATION not in _warnings({**SAFE_BASE, "cluster_pct": Decimal(35)})


def test_dev_concentration_rejects_above_fifty_percent_and_warns_below():
    """The veto moved from 10% to 30% on 2026-09-22, on the operator's later instruction.

    Their words: *"if its bundled dev buying more than 20% 30% we can still buy but we need
    to be careful"*. PLAN §5.5's 10% made that impossible -- it vetoed outright, and it was
    MEASURED doing so on 173 of 485 skips in six hours, about 36% of every refusal the
    agent made.

    The 10% line did not disappear; it became `DEV_PCT_WARN`, the point where a dev holding
    starts costing SIZE rather than the trade. "Careful" has to cost something or the
    instruction is only half implemented, and the half that is left is the permissive one.
    """
    assert TokenRisk.DEV_CONCENTRATION not in _blockers({**SAFE_BASE, "dev_pct": Decimal(9)})
    assert TokenRisk.DEV_CONCENTRATION not in _blockers({**SAFE_BASE, "dev_pct": Decimal(25)})
    # 2026-09-23: the veto moved 30 -> 50 on a population study (see dyor.DEV_PCT_BLOCK);
    # 31 is now bought at a reduced size and 51 is refused.
    assert TokenRisk.DEV_CONCENTRATION not in _blockers({**SAFE_BASE, "dev_pct": Decimal(31)})
    assert TokenRisk.DEV_CONCENTRATION in _blockers({**SAFE_BASE, "dev_pct": Decimal(51)})
    assert dyor.DEV_PCT_WARN < dyor.DEV_PCT_BLOCK, "the warn line must sit under the veto"

    # ...and the 10-30% band is not silent: it is still recorded against the token.
    warned = dyor.build_dossier(
        "X", Chain.SOL, resolution_of({**SAFE_BASE, "dev_pct": Decimal(25)})
    )
    assert TokenRisk.DEV_CONCENTRATION in warned.warnings, (
        "a dev holding a quarter of supply must be recorded even when it no longer vetoes"
    )


def test_lp_not_burned_sizes_down_rather_than_refusing():
    dossier = dyor.build_dossier("X", Chain.SOL, resolution_of({**SAFE_BASE, "lp_burned_pct": Decimal(0)}))
    assert TokenRisk.LP_NOT_BURNED in dossier.warnings
    assert TokenRisk.LP_NOT_BURNED not in dossier.blockers
    assert dossier.tradeable


def test_modifiable_tax_blocks_even_when_the_current_tax_is_zero():
    """Today's 0% sell tax is not a commitment if the owner can change it."""
    blockers = _blockers({**SAFE_BASE, "tax_modifiable": True, "sell_tax_bps": Decimal(0)})
    assert TokenRisk.HIGH_TAX in blockers


def test_low_liquidity_is_a_warning_not_a_blocker():
    dossier = dyor.build_dossier(
        "X", Chain.SOL, resolution_of({**SAFE_BASE, "liquidity_usd": Decimal(2_000)})
    )
    assert TokenRisk.LOW_LIQUIDITY in dossier.warnings
    assert not dossier.blockers


def test_every_blocker_carries_a_reason():
    for rule in dyor.RULES:
        assert rule.reason and len(rule.reason) > 40, rule.name
        assert rule.reason == rule.reason.strip()


def test_blocker_set_is_exactly_this():
    """Pinned deliberately: adding a blocker should require changing this line."""
    assert set(dyor.blocker_reasons()) == {
        TokenRisk.HONEYPOT,
        TokenRisk.MINT_AUTHORITY,
        TokenRisk.FREEZE_AUTHORITY,
        TokenRisk.TRANSFER_HOOK,
        TokenRisk.HIGH_TAX,
        TokenRisk.DEV_CONCENTRATION,
        TokenRisk.CLUSTER_CONCENTRATION,
        TokenRisk.RUG_HISTORY,
        TokenRisk.UNKNOWN_SAFETY,
    }


def test_blockers_and_warnings_are_disjoint():
    dossier = dyor.build_dossier(
        "X",
        Chain.SOL,
        resolution_of({**SAFE_BASE, "cluster_pct": Decimal(40), "rugged": True, "creator_rug_count": Decimal(2)}),
    )
    assert not set(dossier.blockers) & set(dossier.warnings)


# --------------------------------------------------------------------------------------
# the merge: end to end
# --------------------------------------------------------------------------------------


def test_clean_solana_token_passes_with_no_blockers(http, tmp_db):
    """Clean on everything read, and capped at B because bundling and sniping are dark.

    This used to assert ``Grade.A``. It was the bug: the two GMGN-only supply splits were
    unknown on 615 of 615 stored dossiers, so an A was being awarded to a token whose
    bundle and sniper share nobody had ever established. The score is untouched — it is
    still a clean 100 — which is the separation working: what we found and how far we
    looked are now two numbers instead of one.
    """
    wire_solana(http)
    dossier = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)

    assert dossier.blockers == []
    assert dossier.warnings == []
    assert dossier.score == pytest.approx(100.0)
    assert dossier.grade is Grade.B
    assert dossier.mint_authority_revoked is True
    assert dossier.can_sell is True  # derived: no freeze authority and no transfer hook
    # The only things nobody could answer are the two GMGN-only supply splits.
    assert dossier.unknowns == ["bundler_pct", "sniper_pct"]
    assert {r.provider for r in dossier.receipts} >= {"goplus", "rugcheck"}

    reach = coverage.assess_unknowns(dossier.unknowns)
    assert reach.tier is coverage.CoverageTier.PARTIAL
    assert reach.unestablished_families == ("bundling", "sniping")
    assert any(
        r.endpoint == "coverage.assessment" and "partial" in (r.note or "")
        for r in dossier.receipts
    ), "coverage must be legible on the stored dossier, not only inside the grader"


def test_all_providers_down_yields_unknowns_not_a_clean_bill(http, tmp_db):
    """No routes are wired, so every provider is unreachable."""
    dossier = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)

    assert dossier.mint_authority_revoked is None
    assert dossier.freeze_authority_revoked is None
    assert dossier.can_sell is None
    assert dossier.lp_burned_pct.value is None
    assert dossier.lp_burned_pct.basis is EvidenceBasis.UNAVAILABLE
    assert set(dossier.unknowns) == set(dyor.TRACKED_PROPERTIES)
    assert TokenRisk.UNKNOWN_SAFETY in dossier.blockers
    assert not dossier.tradeable
    assert dossier.grade is Grade.QUARANTINED


def test_evidence_weight_orders_the_grades(http, tmp_db, tmp_path, monkeypatch):
    """Three providers clearing a token must beat one, which must beat none.

    Each leg gets its own cache directory: without that the second scan would be answered
    from the first one's disk cache and all three would look identical, which is the disk
    cache working correctly and the test measuring nothing.
    """

    def fresh_cache(name: str) -> None:
        monkeypatch.setenv("KAIBA_DATA_DIR", str(tmp_path / name))
        get_settings.cache_clear()

    fresh_cache("full")
    wire_solana(http)
    full = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)

    fresh_cache("partial")
    partial_http = Http(monkeypatch)
    partial_http.add("solana/token_security", gp("solana_clean.json"))
    partial = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)

    fresh_cache("blind")
    Http(monkeypatch)  # nothing wired at all
    blind = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)

    assert full.score > partial.score > blind.score
    # Grades under the coverage ceiling: the three-provider read establishes everything
    # except the two GMGN-only splits (PARTIAL -> B), the one-provider read establishes
    # almost nothing of the predictive surface (BLIND -> C), and nobody reaching it is
    # still a blocker. The *score* ordering above is what carries "how clean"; these
    # carry "how far we looked", and they no longer both live in one letter.
    assert full.grade is Grade.B
    assert partial.grade is Grade.C
    assert blind.grade is Grade.QUARANTINED
    assert len(blind.unknowns) > len(partial.unknowns) > len(full.unknowns)
    assert coverage.assess_unknowns(full.unknowns).tier is coverage.CoverageTier.PARTIAL
    assert coverage.assess_unknowns(partial.unknowns).tier is coverage.CoverageTier.BLIND


def test_conflict_between_providers_blocks_and_warns(http, tmp_db):
    wire_solana(http, report="report_conflict.json")
    dossier = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)

    # GoPlus said the mint authority is revoked, RugCheck said it is live. We take the
    # unsafe reading and say so, rather than letting the last adapter to run decide.
    assert dossier.mint_authority_revoked is False
    assert TokenRisk.MINT_AUTHORITY in dossier.blockers
    assert TokenRisk.PROVIDER_CONFLICT in dossier.warnings

    notes = " ".join(r.note or "" for r in dossier.receipts)
    assert "providers disagree on mint_authority_revoked" in notes
    assert "goplus=True" in notes and "rugcheck=False" in notes


def test_risky_solana_token_collects_every_expected_blocker(http, tmp_db):
    wire_solana(
        http,
        security="solana_risky.json",
        summary="summary_risky.json",
        report="report_risky.json",
        graph="insiders_graph.json",
        mint=RISKY_MINT,
    )
    dossier = dyor.scan_token(RISKY_MINT, Chain.SOL, conn=tmp_db)

    # DEV_CONCENTRATION left this set on 2026-09-22 when the veto moved from 10% to 30%
    # on the operator's instruction ("we can still buy but we need to be careful"). This
    # fixture's creator share sits in the new 10-30% band, so it is now a WARNING that
    # costs size rather than a reject -- asserted below, so the move is visible here
    # rather than looking like the signal was dropped.
    assert set(dossier.blockers) == {
        TokenRisk.MINT_AUTHORITY,
        TokenRisk.FREEZE_AUTHORITY,
        TokenRisk.TRANSFER_HOOK,
        TokenRisk.HIGH_TAX,
        TokenRisk.CLUSTER_CONCENTRATION,
    }
    assert TokenRisk.DEV_CONCENTRATION in dossier.warnings, (
        "the dev holding must still be RECORDED; only its severity changed"
    )
    assert set(dossier.warnings) >= {
        TokenRisk.TOP10_CONCENTRATION,
        TokenRisk.INSIDER_EXPOSURE,
        TokenRisk.LP_NOT_BURNED,
        TokenRisk.METADATA_MUTABLE,
        TokenRisk.LOW_LIQUIDITY,
        TokenRisk.TRANSFER_FEE,
    }
    assert dossier.dev_pct.value == Decimal(24)
    assert dossier.cluster_pct.value == Decimal(35)
    assert dossier.grade is Grade.QUARANTINED
    assert not dossier.tradeable


def test_evm_honeypot_is_blocked(http, tmp_db):
    http.add("/token_security/1", gp("evm_honeypot.json"))
    dossier = dyor.scan_token(EVM_TRAP, Chain.ETH, conn=tmp_db)

    assert TokenRisk.HONEYPOT in dossier.blockers
    assert TokenRisk.MINT_AUTHORITY in dossier.blockers
    assert TokenRisk.HIGH_TAX in dossier.blockers
    assert dossier.can_sell is False
    assert dossier.grade is Grade.QUARANTINED


def test_evm_clean_token_passes(http, tmp_db):
    http.add("/token_security/1", gp("evm_clean.json"))
    dossier = dyor.scan_token(EVM_CLEAN, Chain.ETH, conn=tmp_db)

    assert dossier.blockers == []
    assert dossier.grade in (Grade.A, Grade.B)
    assert dossier.score > 80


def test_scan_never_raises_when_an_adapter_explodes(http, tmp_db, monkeypatch):
    def boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("adapter is on fire")

    monkeypatch.setattr(goplus, "security_properties", boom)
    monkeypatch.setattr(rugcheck, "security_properties", boom)

    dossier = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)
    assert TokenRisk.UNKNOWN_SAFETY in dossier.blockers
    assert dossier.grade is Grade.QUARANTINED


def test_scan_emits_the_verdict_on_the_bus(http, tmp_db):
    wire_solana(http)
    dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)
    scanned = events.recent(limit=20, kinds=[EventKind.TOKEN_SCANNED.value], conn=tmp_db)
    assert scanned and scanned[0].payload["grade"] == "B"
    # Coverage rides alongside the grade on the bus, so anything counting grades off the
    # event feed can also see how much rubric each one rests on.
    assert scanned[0].payload["coverage"] == "partial"
    assert scanned[0].payload["coverage_missing_families"] == ["bundling", "sniping"]


def test_a_malformed_address_is_refused_before_any_provider_call(http, tmp_db):
    dossier = dyor.scan_token("not-a-real-mint", Chain.SOL, conn=tmp_db)
    assert dossier.grade is Grade.QUARANTINED
    assert TokenRisk.UNKNOWN_SAFETY in dossier.blockers
    assert http.calls == []


# --------------------------------------------------------------------------------------
# persistence — the execution engine reads the dossier out of the database, not from us
# --------------------------------------------------------------------------------------


def test_scan_persists_a_dossier_the_engine_can_load(http, tmp_db):
    from kaiba.execution.engine import load_dossier

    wire_solana(http, report="report_conflict.json")
    scanned = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)

    loaded = load_dossier(Chain.SOL, SOL_MINT, tmp_db)
    assert loaded is not None
    assert loaded.grade is scanned.grade
    assert loaded.blockers == scanned.blockers
    assert loaded.warnings == scanned.warnings
    assert loaded.unknowns == scanned.unknowns
    assert loaded.score == scanned.score
    assert loaded.built_at_ms == scanned.built_at_ms
    assert loaded.mint_authority_revoked is False
    assert loaded.top10_pct.value == scanned.top10_pct.value


def test_an_all_unknown_scan_is_still_persisted(http, tmp_db):
    """"We looked and could not see" is a record; storing nothing looks like never looking."""
    from kaiba.execution.engine import load_dossier

    dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)
    loaded = load_dossier(Chain.SOL, SOL_MINT, tmp_db)

    assert loaded is not None
    assert loaded.grade is Grade.QUARANTINED
    assert set(loaded.unknowns) == set(dyor.TRACKED_PROPERTIES)
    assert loaded.mint_authority_revoked is None


def test_rescanning_replaces_the_stored_row(http, tmp_db):
    from kaiba.core.db import fetch_all
    from kaiba.execution.engine import load_dossier

    dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)  # nothing wired: QUARANTINED
    wire_solana(http)
    dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)

    rows = fetch_all(tmp_db, "SELECT * FROM token_dossiers WHERE address=?", (SOL_MINT,))
    assert len(rows) == 1
    assert rows[0]["grade"] == "B"
    assert load_dossier(Chain.SOL, SOL_MINT, tmp_db).grade is Grade.B


def test_built_at_ms_is_the_assembly_time_not_the_evidence_time(http, tmp_db):
    """A cache hit must not make the dossier look older, nor the engine's check meaningless."""
    from kaiba.core.schemas import now_ms

    wire_solana(http)
    before = now_ms()
    first = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)
    assert before <= first.built_at_ms <= now_ms()

    # Second scan is served from the provider cache; the dossier is still freshly built.
    second = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)
    assert second.built_at_ms >= first.built_at_ms
    assert any(r.basis is EvidenceBasis.CACHED for r in second.receipts)


def test_stale_cached_evidence_is_reported_as_an_unknown_safety_warning():
    """``built_at_ms`` cannot express evidence age, so a STALE receipt must say so itself."""
    resolution = resolution_of(SAFE_BASE)
    resolution.receipts.append(
        Receipt(provider="goplus", endpoint="token.security_sol", basis=EvidenceBasis.STALE)
    )
    dossier = dyor.build_dossier("X", Chain.SOL, resolution)
    assert TokenRisk.UNKNOWN_SAFETY in dossier.warnings
    assert any("stale_providers=goplus" in (r.note or "") for r in dossier.receipts)


# --------------------------------------------------------------------------------------
# the GMGN adapter, which is landing in parallel with this module
# --------------------------------------------------------------------------------------


def test_missing_gmgn_module_degrades_cleanly(http, tmp_db, monkeypatch):
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", None)
    wire_solana(http)
    dossier = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)

    assert dossier.grade is Grade.B  # B not A: GMGN is the only source of the two splits
    assert any(r.provider == "gmgn" and r.basis is EvidenceBasis.UNAVAILABLE for r in dossier.receipts)


def test_gmgn_contributes_claims_when_present(http, tmp_db, monkeypatch):
    module = types.ModuleType("kaiba.providers.gmgn_cli")

    def security_properties(address: str, chain: Chain, conn: Any = None):
        return (
            {"bundler_pct": Decimal(40), "sniper_pct": Decimal(5)},
            Receipt(provider="gmgn", endpoint="token.security"),
        )

    module.security_properties = security_properties  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", module)

    wire_solana(http)
    dossier = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)

    assert dossier.bundler_pct.value == Decimal(40)
    assert TokenRisk.BUNDLER_EXPOSURE in dossier.warnings
    assert "bundler_pct" not in dossier.unknowns


def test_gmgn_raw_payload_is_normalised_as_a_fallback(http, tmp_db, monkeypatch):
    module = types.ModuleType("kaiba.providers.gmgn_cli")

    def token_security(address: str, chain: Chain):
        return {"renounced_mint": False, "top_10_holder_rate": 0.62, "burn_ratio": 0.0}

    module.token_security = token_security  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", module)

    dossier = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)
    assert dossier.mint_authority_revoked is False
    assert TokenRisk.MINT_AUTHORITY in dossier.blockers


def test_gmgn_adapter_failure_does_not_fail_the_scan(http, tmp_db, monkeypatch):
    module = types.ModuleType("kaiba.providers.gmgn_cli")

    def security_properties(address: str, chain: Chain, conn: Any = None):
        raise RuntimeError("gmgn-cli not installed")

    module.security_properties = security_properties  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", module)

    wire_solana(http)
    dossier = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)
    assert dossier.grade is Grade.B


def test_gmgn_supplying_the_two_dark_splits_is_what_makes_an_A_possible(http, tmp_db, monkeypatch):
    """The top grade is reachable — it just needs the coverage nothing currently has.

    Counterpart to ``test_clean_solana_token_passes_with_no_blockers``: identical fixture,
    plus a GMGN that answers ``bundler_pct`` and ``sniper_pct`` with clean values. The
    coverage ceiling lifts and the same token grades A. Without this the cap would be
    indistinguishable from "A has been deleted".
    """
    module = types.ModuleType("kaiba.providers.gmgn_cli")

    def security_properties(address: str, chain: Chain, conn: Any = None):
        return (
            {"bundler_pct": Decimal(2), "sniper_pct": Decimal(3)},
            Receipt(provider="gmgn", endpoint="token.security"),
        )

    module.security_properties = security_properties  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", module)

    wire_solana(http)
    dossier = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)

    assert dossier.unknowns == []
    assert coverage.assess_unknowns(dossier.unknowns).tier is coverage.CoverageTier.FULL
    assert dossier.grade is Grade.A


# --------------------------------------------------------------------------------------
# bundles: our own tape answers the two fields GMGN sells
# --------------------------------------------------------------------------------------


def test_a_token_with_no_tape_gets_no_bundle_claim_at_all(http, tmp_db):
    """``UNAVAILABLE``, never 0% bundled.

    ``bundles`` already had to fix one fail-open here — accepting "our earliest swap row
    sits at the launch" as proof of coverage produced a confident 0% on mints nobody had
    pulled, which would have opened ``curve-velocity`` on 15 unexamined tokens. A mint
    that is absent from the tape entirely must come back with nothing.
    """
    wire_solana(http)
    dossier = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)

    assert dossier.bundler_pct.value is None
    assert dossier.sniper_pct.value is None
    assert dossier.bundler_pct.basis is EvidenceBasis.UNAVAILABLE
    assert dossier.sniper_pct.basis is EvidenceBasis.UNAVAILABLE
    assert {"bundler_pct", "sniper_pct"} <= set(dossier.unknowns)
    assert TokenRisk.BUNDLER_EXPOSURE not in dossier.warnings


def test_the_bundles_collector_is_wired_into_the_scan(http, tmp_db, monkeypatch):
    """It must actually be called, and its numbers must reach the dossier.

    Pinned because the module was built, tested and wired to nothing for long enough that
    ``bundler_pct`` reached 100% unknown across 615 dossiers.
    """
    from kaiba.intelligence import bundles

    called: list[str] = []

    def fake(address: str, chain: Chain, conn: Any = None):
        called.append(address)
        receipt = Receipt(provider="kaiba.intelligence", endpoint="bundles.analyse",
                          basis=EvidenceBasis.DERIVED)
        return (
            [
                dyor.Claim("bundler_pct", "bundles", Decimal(18), receipt),
                dyor.Claim("sniper_pct", "bundles", Decimal(4), receipt),
            ],
            [receipt],
            "ok",
        )

    monkeypatch.setattr(bundles, "collect_bundles", fake)
    wire_solana(http)
    dossier = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)

    assert called == [SOL_MINT]
    assert dossier.bundler_pct.value == Decimal(18)
    assert dossier.sniper_pct.value == Decimal(4)
    assert TokenRisk.BUNDLER_EXPOSURE in dossier.warnings  # 18% > the 15% warn threshold
    assert dossier.unknowns == []


def test_bundles_and_gmgn_disagreeing_raises_a_conflict_rather_than_one_winning(
    http, tmp_db, monkeypatch
):
    """The collector runs after gmgn so ``resolve`` sees both claims, not the last writer."""
    from kaiba.intelligence import bundles

    module = types.ModuleType("kaiba.providers.gmgn_cli")

    def security_properties(address: str, chain: Chain, conn: Any = None):
        return (
            {"bundler_pct": Decimal(1), "sniper_pct": Decimal(1)},
            Receipt(provider="gmgn", endpoint="token.security"),
        )

    module.security_properties = security_properties  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", module)

    def fake(address: str, chain: Chain, conn: Any = None):
        receipt = Receipt(provider="kaiba.intelligence", endpoint="bundles.analyse",
                          basis=EvidenceBasis.DERIVED)
        return (
            [
                dyor.Claim("bundler_pct", "bundles", Decimal(44), receipt),
                dyor.Claim("sniper_pct", "bundles", Decimal(1), receipt),
            ],
            [receipt],
            "ok",
        )

    monkeypatch.setattr(bundles, "collect_bundles", fake)
    wire_solana(http)
    dossier = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)

    assert TokenRisk.PROVIDER_CONFLICT in dossier.warnings
    assert dossier.bundler_pct.value == Decimal(44)  # the worse of the two, per resolve()


def test_a_broken_bundles_module_does_not_fail_the_scan(http, tmp_db, monkeypatch):
    from kaiba.intelligence import bundles

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("swaps table is on fire")

    monkeypatch.setattr(bundles, "collect_bundles", boom)
    wire_solana(http)
    dossier = dyor.scan_token(SOL_MINT, Chain.SOL, conn=tmp_db)

    assert dossier.blockers == []
    assert dossier.grade is Grade.B
    assert any(r.provider == "bundles" and r.basis is EvidenceBasis.UNAVAILABLE for r in dossier.receipts)


# --------------------------------------------------------------------------------------
# live — skipped unless KAIBA_LIVE_TESTS=1
# --------------------------------------------------------------------------------------


@pytest.mark.live
def test_live_goplus_solana(tmp_db):
    """USDC on Solana: both authorities are public knowledge, so this is a real check."""
    usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    props, receipt = goplus.security_properties(usdc, Chain.SOL, conn=tmp_db)
    assert receipt.basis is not EvidenceBasis.UNAVAILABLE, receipt.note
    assert "mint_authority_revoked" in props


@pytest.mark.live
def test_live_rugcheck_report(tmp_db):
    usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    fetched = rugcheck.report(usdc, conn=tmp_db)
    assert fetched.ok, fetched.receipt.note
    assert rugcheck.normalize_report(fetched.data)


@pytest.mark.live
def test_live_full_scan(tmp_db):
    bonk = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    dossier = dyor.scan_token(bonk, Chain.SOL, conn=tmp_db)
    assert dossier.grade is not Grade.UNSCORED
    assert dossier.receipts


# ---------------------------------------- calibration against published base rates


def test_wash_trading_can_never_be_a_blocker():
    """Wash trading *positively* predicts graduation, so it must not disqualify a token.

    2.0% graduation with wash trading against 0.90% without, p = 3e-59 across 15.2M
    pump.fun coins (CCS'26, arXiv 2609.10246). The honest residual is that washed volume
    inflates the depth we size from, which is a sizing input, not a verdict.
    """
    from kaiba.intelligence import dyor

    rule = next(r for r in dyor.RULES if r.name == "wash_trading")
    assert rule.severity is not dyor.Severity.BLOCKER
    assert "does NOT predict failure" in rule.reason


def test_the_invented_top10_threshold_says_so_out_loud():
    """No published study establishes a holder-concentration threshold. Ours is a guess."""
    from kaiba.intelligence import dyor

    rule = next(r for r in dyor.RULES if r.name == "top10_concentration")
    assert "INVENTED" in rule.reason


def test_freeze_authority_stays_a_blocker_but_is_not_oversold():
    """Total downside and a free check, so keep it; 0.6% of rugs, so do not count it."""
    from kaiba.intelligence import dyor

    rule = next(r for r in dyor.RULES if r.name == "freeze_authority_live")
    assert rule.severity is dyor.Severity.BLOCKER
    assert "0.6%" in rule.reason


# ---------------------------------------- the coverage ceiling on the grade


#: A resolution that is spotless on everything the free providers answer, scores 100, and
#: clears ``A_MIN_EVIDENCE_WEIGHT``. Under the old grader this was an A while nobody had
#: established its concentration, bundling, sniping or taxes. It is the shape 615 of 615
#: stored dossiers had.
CLEAN_BUT_UNEXAMINED: dict[str, Any] = {
    "can_sell": True,
    "mint_authority_revoked": True,
    "freeze_authority_revoked": True,
    "transfer_hook": False,
    "balance_mutable": False,
    "tax_modifiable": False,
    "metadata_mutable": False,
    "lp_burned_pct": Decimal(100),
    "dev_pct": Decimal(1),
    "cluster_pct": Decimal(0),
    "liquidity_usd": Decimal(500_000),
}


def test_the_coverage_cap_keeps_an_unexamined_token_off_the_top_grade():
    """MUTATION TARGET. Delete the ``cov.cap`` call in ``_assign_grade`` and this fails.

    A perfect score on the properties we could read, and nothing known about the four
    families that predict a rug. It must not be an A, and it must not be a B either:
    ``dev_pct`` and ``cluster_pct`` together are 18 of 86 separation points, under the
    24 that the single strongest published variable is worth.
    """
    verdict = dyor.score_dossier(resolution_of(CLEAN_BUT_UNEXAMINED), [])

    # It clears every gate the old grader had: no blocker, no warning, a score well over
    # A_MIN_SCORE and evidence_weight exactly at A_MIN_EVIDENCE_WEIGHT. Only coverage
    # stops it, and the score it is stopped on is unchanged.
    assert verdict.score == pytest.approx(86.49)
    assert verdict.score >= dyor.A_MIN_SCORE
    assert verdict.evidence_weight >= dyor.A_MIN_EVIDENCE_WEIGHT
    assert verdict.coverage.tier is coverage.CoverageTier.BLIND
    assert verdict.grade is Grade.C
    assert any("capped to C from A" in note for note in verdict.notes)


def test_coverage_never_raises_a_grade():
    """The ceiling is one-directional. Full coverage of a dirty token does not promote it."""
    dirty = {
        **CLEAN_BUT_UNEXAMINED,
        "top10_pct": Decimal(80),
        "insider_pct": Decimal(25),
        "bundler_pct": Decimal(25),
        "sniper_pct": Decimal(25),
        "buy_tax_bps": Decimal(1_200),
        "sell_tax_bps": Decimal(1_200),
        "holder_count": Decimal(40),
    }
    resolution = resolution_of(dirty)
    verdict = dyor.score_dossier(resolution, dyor.evaluate(resolution))

    assert verdict.coverage.tier is coverage.CoverageTier.FULL
    assert verdict.coverage.max_grade is Grade.A
    assert verdict.score < dyor.A_MIN_SCORE
    assert verdict.grade is Grade.B  # the score decides; the ceiling is not a floor


def test_coverage_itself_does_not_subtract_from_the_score():
    """Requirement: one number cannot carry both facts, so the ceiling must not move it.

    Two resolutions with identical scoring components — ``_components`` folds all five
    supply splits into one ``insider_supply`` block, so knowing one of them or all five
    gives the same evidence weight and the same fraction — and very different coverage.
    The score is identical to the cent; only the grade moves. (The separate, pre-existing
    ``CONFIDENCE_FLOOR`` discount does price ``evidence_weight`` into the score. That is
    the scoring rubric, not this ceiling, and it is deliberately untouched.)
    """
    thin = {**CLEAN_BUT_UNEXAMINED, "dev_pct": Decimal(0)}
    thick = {
        **thin,
        "insider_pct": Decimal(0),
        "bundler_pct": Decimal(0),
        "sniper_pct": Decimal(0),
    }
    a = dyor.score_dossier(resolution_of(thin), [])
    b = dyor.score_dossier(resolution_of(thick), [])

    assert a.evidence_weight == b.evidence_weight
    assert a.score == b.score
    assert a.coverage.tier is coverage.CoverageTier.BLIND
    assert b.coverage.tier is coverage.CoverageTier.PARTIAL
    assert a.grade is Grade.C and b.grade is Grade.B


def test_a_blocker_is_not_reinterpreted_by_coverage():
    """QUARANTINED is a finding about the token, not a claim of cleanliness to cap."""
    resolution = resolution_of({**CLEAN_BUT_UNEXAMINED, "rugged": True})
    verdict = dyor.score_dossier(resolution, dyor.evaluate(resolution))
    assert verdict.grade is Grade.QUARANTINED
