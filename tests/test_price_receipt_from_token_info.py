"""A dossier price carries the clock of the read that observed it, not the merge's oldest half.

MEASURED on the live box (journal #4981, re-measured 2026-10-03): 20 approved live entries
in 7 days (robinhood 16, sol 4) placed no order. ``engine._plan_min_out`` refused every one
``token_price_stale`` at 304-601 s against the 120 s budget, although each dossier had been
rebuilt 0.9-46.9 s before the plan. The pattern on all 20: a first scan ~300 s earlier
cached ``token security`` (900 s TTL); the rescan re-read ``token info`` fresh (15 s TTL)
but ``gmgn_cli._merged_receipt`` stamped the merged record with the OLDER half, so a price
seconds old reached the engine five to ten minutes "old" -- and, past 900 s, STALE.

The fix is provenance, not a wider budget: ``price_usd`` / ``market_cap_usd`` come only from
``token info`` and now carry its own receipt. The 120 s budget is untouched and still bites
(``test_the_120s_budget_still_refuses_an_old_token_info_price``).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from kaiba.core.schemas import (
    Action,
    Chain,
    Decision,
    EvidenceBasis,
    Lane,
    LaneMode,
    Receipt,
    now_ms,
)
from kaiba.execution import engine
from kaiba.intelligence import dyor
from kaiba.providers import _http
from kaiba.providers import gmgn_cli as g

FIXTURES = Path(__file__).parent / "fixtures" / "gmgn"
WSOL = "So11111111111111111111111111111111111111112"
#: ``token security`` is served from cache this old on the rescan: the median of the 20
#: live refusals was ~315 s, inside its 900 s TTL and far past token info's 15 s + 120 s.
SECURITY_AGE_MS = 305_000


def _body(name: str) -> dict[str, Any]:
    raw = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    parsed = json.loads(raw["stdout"])
    return parsed.get("data", parsed) if isinstance(parsed, dict) else parsed


def _result(body: Any, endpoint: str, *, age_ms: int = 0,
            basis: EvidenceBasis = EvidenceBasis.PROVIDER_REPORTED) -> g.GmgnResult:
    return g.GmgnResult(
        body,
        Receipt(provider=g.PROVIDER, endpoint=endpoint, observed_at_ms=now_ms() - age_ms,
                basis=basis),
    )


def _stub_reads(monkeypatch, *, sec: g.GmgnResult, info: g.GmgnResult) -> None:
    monkeypatch.setattr(g, "token_security", lambda *a, **k: sec)
    monkeypatch.setattr(g, "token_info", lambda *a, **k: info)


@pytest.fixture(autouse=True)
def _isolated(tmp_db):
    return tmp_db


# ----------------------------------------------------------------------- the adapter


def test_price_carries_token_infos_receipt_not_the_merged_one(monkeypatch):
    sec = _result(_body("token_security"), "token.security", age_ms=SECURITY_AGE_MS,
                  basis=EvidenceBasis.CACHED)
    info = _result(_body("token_info"), "token.info", age_ms=2_000)
    _stub_reads(monkeypatch, sec=sec, info=info)

    result = g.security_properties(WSOL, Chain.SOL)

    assert result.ok
    own = result.data.field_receipts
    assert set(own) == {"price_usd", "market_cap_usd"}
    assert own["price_usd"] is info.receipt
    assert own["market_cap_usd"] is info.receipt
    # The merged receipt is unchanged: still the honest, older half.
    assert result.receipt.observed_at_ms == sec.receipt.observed_at_ms
    assert result.receipt.basis is EvidenceBasis.CACHED


def test_security_fields_keep_the_merged_receipt(monkeypatch):
    """Only market fields are re-stamped; the authorities and taxes stay on the old clock."""
    sec = _result(_body("token_security"), "token.security", age_ms=SECURITY_AGE_MS,
                  basis=EvidenceBasis.CACHED)
    info = _result(_body("token_info"), "token.info")
    _stub_reads(monkeypatch, sec=sec, info=info)

    props = g.security_properties(WSOL, Chain.SOL).data

    assert "top10_pct" in props and "mint_authority_revoked" in props
    assert set(props.field_receipts) <= set(g.INFO_MARKET_PROPS)


def test_a_stale_token_info_price_stays_stale(monkeypatch):
    """The receipt handed on is token info's own, basis included -- never upgraded."""
    sec = _result(_body("token_security"), "token.security", age_ms=SECURITY_AGE_MS)
    info = _result(_body("token_info"), "token.info", age_ms=60_000, basis=EvidenceBasis.STALE)
    _stub_reads(monkeypatch, sec=sec, info=info)

    own = g.security_properties(WSOL, Chain.SOL).data.field_receipts

    assert own["price_usd"].basis is EvidenceBasis.STALE
    assert own["price_usd"].observed_at_ms == info.receipt.observed_at_ms


def test_a_value_token_info_alone_cannot_reproduce_keeps_the_merged_receipt(monkeypatch):
    """Provenance, not field names: if ``token security`` supplied the price, it is its clock."""
    sec_body = {**_body("token_security"), "price": "999.5"}
    sec = _result(sec_body, "token.security", age_ms=SECURITY_AGE_MS)
    info = _result(_body("token_info"), "token.info")
    _stub_reads(monkeypatch, sec=sec, info=info)

    result = g.security_properties(WSOL, Chain.SOL)

    assert result.data["price_usd"] == Decimal("999.5")  # security wins the collision
    assert "price_usd" not in result.data.field_receipts


def test_one_half_only_needs_no_override(monkeypatch):
    sec = g.GmgnResult(None, Receipt(provider=g.PROVIDER, endpoint="token.security",
                                     basis=EvidenceBasis.UNAVAILABLE))
    info = _result(_body("token_info"), "token.info")
    _stub_reads(monkeypatch, sec=sec, info=info)

    result = g.security_properties(WSOL, Chain.SOL)

    assert result.receipt.endpoint == "token.info"
    assert result.data.field_receipts == {}


def test_the_real_cache_path_reproduces_the_live_shape(monkeypatch):
    """Through ``run_read`` and the disk cache, not stubs: security CACHED 305 s, info fresh."""
    from tests.test_gmgn_cli import raw_from

    monkeypatch.setattr(g, "cli_argv", lambda: ["node", "index.js"])
    from kaiba.core import limiter as lim

    real = lim.limits_for

    def relaxed(provider: str):
        import dataclasses

        base = real(provider)
        if provider != g.PROVIDER:
            return base
        return dataclasses.replace(base, min_interval_ms=0, capacity=10_000, refill_per_s=10_000.0)

    monkeypatch.setattr(lim, "limits_for", relaxed)
    monkeypatch.setattr(g, "_spawn", lambda argv, timeout_s: raw_from("token_security"))
    assert g.token_security(WSOL, Chain.SOL).ok
    # Back-date the cached security read to the first scan, 305 s ago.
    key = "gmgn-cli token security --address " + WSOL + " --chain sol --raw"
    path = _http.cache_path(g.PROVIDER, key)
    blob = json.loads(path.read_text(encoding="utf-8"))
    first_scan_ms = now_ms() - SECURITY_AGE_MS
    blob["fetched_ms"] = first_scan_ms
    path.write_text(json.dumps(blob), encoding="utf-8")

    def spawn(argv: list[str], timeout_s: float) -> g._Raw:
        assert "security" not in argv, "token security must be served from its cache"
        return raw_from("token_info")

    monkeypatch.setattr(g, "_spawn", spawn)
    before = now_ms()
    result = g.security_properties(WSOL, Chain.SOL)

    assert result.receipt.observed_at_ms == first_scan_ms  # the merge: oldest half
    price_receipt = result.data.field_receipts["price_usd"]
    assert price_receipt.endpoint == "token.info"
    assert price_receipt.observed_at_ms >= before          # the price: just read


# ------------------------------------------------------------- dossier -> engine plan


def _dossier_from_scan(monkeypatch, *, info_age_ms: int = 2_000):
    sec = _result(_body("token_security"), "token.security", age_ms=SECURITY_AGE_MS,
                  basis=EvidenceBasis.CACHED)
    info = _result(_body("token_info"), "token.info", age_ms=info_age_ms)
    _stub_reads(monkeypatch, sec=sec, info=info)
    claims, receipts, status = dyor.collect_gmgn(WSOL, Chain.SOL, None)
    assert status == "ok"
    resolution = dyor.resolve(claims, chain=Chain.SOL)
    resolution.receipts.extend(receipts)
    return dyor.build_dossier(WSOL, Chain.SOL, resolution), sec, info


def _plan(monkeypatch, tmp_db, dossier):
    monkeypatch.setattr(engine, "load_dossier", lambda *a, **k: dossier)
    monkeypatch.setattr(engine, "_token_decimals", lambda *a, **k: (9, "verified_onchain", "fixture"))
    monkeypatch.setattr(engine, "_native_usd", lambda *a, **k: (Decimal(150), "fixture"))
    decision = Decision(decision_id="d1", chain=Chain.SOL, token=WSOL, lane=Lane.SM_TRENCHES,
                        mode=LaneMode.LIVE, action=Action.ENTER, size_base_units=830_000_000)
    return engine._plan_min_out(decision, tmp_db)


def test_the_dossier_price_is_on_token_infos_clock(monkeypatch):
    dossier, sec, info = _dossier_from_scan(monkeypatch)

    assert dossier.price_usd.receipt.endpoint == "token.info"
    assert dossier.price_usd.receipt.observed_at_ms == info.receipt.observed_at_ms
    assert dossier.market_cap_usd.receipt.observed_at_ms == info.receipt.observed_at_ms
    # A security field still reads the merged, older clock.
    assert dossier.top10_pct.receipt.observed_at_ms == sec.receipt.observed_at_ms


def test_the_live_refusal_shape_now_plans(monkeypatch, tmp_db):
    """THE REGRESSION: security cached 305 s, price 2 s old -> refused token_price_stale."""
    dossier, _sec, _info = _dossier_from_scan(monkeypatch)

    plan = _plan(monkeypatch, tmp_db, dossier)

    assert plan.reason == "derived_from_dossier_price_and_native_sample", plan
    assert plan.value and plan.value > 0
    assert plan.detail["token_price_freshness_budget_s"] == 120


def test_the_120s_budget_still_refuses_an_old_token_info_price(monkeypatch, tmp_db):
    """The budget is not widened: a token info read 130 s old is still refused."""
    dossier, _sec, _info = _dossier_from_scan(monkeypatch, info_age_ms=130_000)

    plan = _plan(monkeypatch, tmp_db, dossier)

    assert plan.value is None
    assert plan.reason == "token_price_stale"
