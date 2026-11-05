"""Never open a position we cannot monitor, even when we can price it perfectly.

Sizing and protection do not share a price path. `quote_asset_rate` reads the quote leg
through `gmgn_cli.token_info`; the watchdog prices a position through
`evm_price.QuoteUsd` -> `_provider_price_usd` -> DexScreener. They disagree, and the
disagreement is not academic.

MEASURED on the live box 2026-09-22, the 8 most-used non-native quote assets on robinhood:

    quote asset   tokens   priced by sizing   priced by protection
    USDG             719   yes ($1.00064)     NO  "no pair prices this token"
    NVDA             496   yes               yes
    META             124   yes               yes
    GME               80   yes               yes
    (4 others)       557   yes               yes

USDG is Global Dollar, 6 decimals, $19.5M liquidity, and it is structurally unpriceable
by that path -- it is the QUOTE side of every pair it appears in, so `prices.pick_pair`
discards all 30 of them. Three consecutive probes confirmed it.

Without this gate all 719 could be entered and would then be blind: no working stop, and
`max_blind_halt_entries` tripping agent-wide after `max_blind_s`. That is the same shape
as the defect that left two funded bsc positions unable to exit for 53 minutes the same
day, which is the most expensive thing this system has done.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain
from kaiba.execution import viability as V

USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
GME = "0x1b0e319c6a659f002271b69db8a7df2f911c153e"


@pytest.fixture(autouse=True)
def _clean():
    V.reset_protectable_cache()
    yield
    V.reset_protectable_cache()


def price_path(monkeypatch, answers: dict[str, Decimal | None], calls: list | None = None):
    """Stand in for the PROTECTION path -- the source the watchdog actually resolves.

    Deliberately patched at `watchdog.resolve_price_source` rather than at a provider
    underneath it. The first version of this file stubbed
    `evm_price._provider_price_usd`, which is DexScreener alone, and MEASURED on the live
    box that is a different answer: the raw provider prices 7 of 59 quote assets while the
    configured `venue` source prices all but USDG, because it falls back through the curve
    and the pair.
    """
    from kaiba.execution import watchdog as wd

    class _Q:
        def __init__(self, price):
            self.price_usd = price
            self.usable = price is not None and price > 0
            self.note = None

    class _Src:
        def quote(self, chain, token):
            if calls is not None:
                calls.append(token.lower())
            return _Q(answers.get(token.lower()))

    monkeypatch.setattr(wd, "configured_price_source_name", lambda *a, **k: "venue")
    monkeypatch.setattr(wd, "resolve_price_source", lambda name: _Src())


# ------------------------------------------------------------------ the verdict


def test_an_asset_the_protection_path_prices_is_protectable(monkeypatch):
    price_path(monkeypatch, {GME: Decimal("23.59")})
    ok, why = V.quote_asset_is_protectable(Chain.ROBINHOOD, GME)
    assert ok is True, why


def test_the_live_usdg_case_is_not_protectable(monkeypatch):
    """The measured one: priced by sizing, invisible to protection."""
    price_path(monkeypatch, {USDG: None})
    ok, why = V.quote_asset_is_protectable(Chain.ROBINHOOD, USDG)
    assert ok is False
    assert "no_price" in why, why


@pytest.mark.parametrize("bad", [None, Decimal(0), Decimal("-1")])
def test_a_missing_zero_or_negative_price_is_not_protection(monkeypatch, bad):
    price_path(monkeypatch, {USDG: bad})
    assert V.quote_asset_is_protectable(Chain.ROBINHOOD, USDG)[0] is False


def test_a_raising_provider_reads_as_unprotectable(monkeypatch):
    """"We could not check" and "we cannot see it" have the same consequence."""
    from kaiba.execution import watchdog as wd

    class _Boom:
        def quote(self, chain, token):
            raise RuntimeError("provider down")

    monkeypatch.setattr(wd, "configured_price_source_name", lambda *a, **k: "venue")
    monkeypatch.setattr(wd, "resolve_price_source", lambda name: _Boom())
    ok, why = V.quote_asset_is_protectable(Chain.ROBINHOOD, USDG)
    assert ok is False and "raised" in why, why


def test_the_verdict_never_raises(monkeypatch):
    for token in (None, "", "   "):
        assert V.quote_asset_is_protectable(Chain.ROBINHOOD, token)[0] is False


# ------------------------------------------------------------------ caching


def test_both_outcomes_are_cached_because_the_property_is_structural(monkeypatch):
    calls: list[str] = []
    price_path(monkeypatch, {USDG: None, GME: Decimal("23.59")}, calls)
    for _ in range(4):
        V.quote_asset_is_protectable(Chain.ROBINHOOD, USDG)
        V.quote_asset_is_protectable(Chain.ROBINHOOD, GME)
    assert calls.count(USDG) == 1, "a structural refusal must not re-probe every call"
    assert calls.count(GME) == 1


def test_the_cache_expires(monkeypatch):
    calls: list[str] = []
    price_path(monkeypatch, {USDG: None}, calls)
    V.quote_asset_is_protectable(Chain.ROBINHOOD, USDG, at_ms=0)
    V.quote_asset_is_protectable(Chain.ROBINHOOD, USDG,
                                 at_ms=(V.PROTECTABLE_TTL_S + 1) * 1000)
    assert calls.count(USDG) == 2, "the verdict must be re-checked after its TTL"


# ------------------------------------------------------------ the gate on the rate


def stub_gmgn(monkeypatch, *, price="1.00064", decimals=6, symbol="USDG"):
    """Make the SIZING leg succeed, so only the protection gate can refuse."""
    from kaiba.providers import gmgn_cli

    class _Receipt:
        observed_at_ms = 0
        note = None

        class _B:
            value = "provider_reported"

        basis = _B()

    class _Got:
        ok = True
        data = {"price": {"price": price}, "decimals": decimals, "symbol": symbol}
        receipt = _Receipt()

    monkeypatch.setattr(gmgn_cli, "token_info", lambda *a, **k: _Got())
    monkeypatch.setattr(gmgn_cli, "flatten_payload",
                        lambda d: {"price": price, "decimals": decimals, "symbol": symbol})


def test_the_rate_refuses_an_unprotectable_asset_it_could_price_perfectly(monkeypatch, tmp_db):
    """THE POINT OF THIS FILE. Sizing succeeds completely and the answer is still no."""
    stub_gmgn(monkeypatch)
    monkeypatch.setattr(V, "_native_usd", lambda *a, **k: Decimal("2723"))
    price_path(monkeypatch, {USDG: None})
    V.reset_quote_asset_cache()

    rate, note = V.quote_asset_rate(Chain.ROBINHOOD, USDG, tmp_db, at_ms=0)

    assert rate is None, "an asset the watchdog cannot price must never be sized"
    assert note.startswith("quote_price_unprotectable:"), note
    assert "USDG" in note, note


def test_the_same_asset_is_rated_when_protection_can_see_it(monkeypatch, tmp_db):
    """The control. Without it the test above could pass because sizing was broken."""
    stub_gmgn(monkeypatch, price="23.59", decimals=18, symbol="GME")
    monkeypatch.setattr(V, "_native_usd", lambda *a, **k: Decimal("2723"))
    price_path(monkeypatch, {GME: Decimal("23.59")})
    V.reset_quote_asset_cache()

    rate, note = V.quote_asset_rate(Chain.ROBINHOOD, GME, tmp_db, at_ms=0)

    assert rate is not None, f"a protectable asset must still be rated: {note}"
    assert rate.symbol == "GME" and rate.decimals == 18


def test_an_unprotectable_refusal_is_not_cached_as_a_rate(monkeypatch, tmp_db):
    """A refusal must not land in the rate cache and be served as success later."""
    stub_gmgn(monkeypatch)
    monkeypatch.setattr(V, "_native_usd", lambda *a, **k: Decimal("2723"))
    price_path(monkeypatch, {USDG: None})
    V.reset_quote_asset_cache()
    V.quote_asset_rate(Chain.ROBINHOOD, USDG, tmp_db, at_ms=0)
    rate, note = V.quote_asset_rate(Chain.ROBINHOOD, USDG, tmp_db, at_ms=0)
    assert rate is None, note


def test_the_refusal_names_the_asset_and_the_reason():
    """An operator reading a refusal must be able to act on it without a debugger."""
    src = __import__("pathlib").Path(V.__file__).read_text(encoding="utf-8")
    assert "quote_price_unprotectable:" in src
    assert "quote_asset_is_protectable" in src


# ------------------------------------------------- the two verdicts are not symmetric


def test_a_refusal_is_rechecked_far_sooner_than_a_pass(monkeypatch):
    """"Can see it" stays true; "could not see it" may have been a 429.

    MEASURED 2026-09-22: GME priced through the protection path on one probe and failed on
    another taken while the robinhood-rpc family was in cooldown. Caching that second
    answer as long as a positive one would lock a protectable asset out over a blip.
    """
    assert V.PROTECTABLE_RETRY_S < V.PROTECTABLE_TTL_S

    calls: list[str] = []
    price_path(monkeypatch, {USDG: None}, calls)
    V.quote_asset_is_protectable(Chain.ROBINHOOD, USDG, at_ms=0)
    # still inside the retry window -> served from cache
    V.quote_asset_is_protectable(Chain.ROBINHOOD, USDG,
                                 at_ms=(V.PROTECTABLE_RETRY_S - 1) * 1000)
    assert calls.count(USDG) == 1
    # past it -> probed again, long before a positive verdict would have expired
    V.quote_asset_is_protectable(Chain.ROBINHOOD, USDG,
                                 at_ms=(V.PROTECTABLE_RETRY_S + 1) * 1000)
    assert calls.count(USDG) == 2


def test_a_pass_is_held_for_the_full_ttl(monkeypatch):
    calls: list[str] = []
    price_path(monkeypatch, {GME: Decimal("23.59")}, calls)
    V.quote_asset_is_protectable(Chain.ROBINHOOD, GME, at_ms=0)
    V.quote_asset_is_protectable(Chain.ROBINHOOD, GME,
                                 at_ms=(V.PROTECTABLE_RETRY_S + 10) * 1000)
    assert calls.count(GME) == 1, "a positive verdict must not expire on the retry clock"
