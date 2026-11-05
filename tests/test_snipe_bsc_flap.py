"""launch-snipe on BSC (Flap), paper only: the pre-trade guards, the paper entry and marks,
the paper-only rule (producer AND engine), and the pre-dossier band that used to refuse every
Flap launch.

Synthetic addresses only. The curve is Flap's published native curve (r = 6.14 BNB,
h = 107,036,752, K = 6,797,205,657.28; MEASURED on the box 2026-10-05) with records built
here so the portal's own cross-checks (``evm_price.parse_flap_record``) pass. Each guard
test starts from a launch that FIRES and changes one input, and asserts that exactly that
guard's reason is the only one -- so with the guard removed the launch would fire and the
test fails. No test reaches the network: every RPC is a stand-in.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, localcontext

import pytest

from kaiba.core.config import RiskConfig, get_risk, load_risk
from kaiba.core.db import fetch_all, fetch_one, jdump, jload
from kaiba.core.schemas import Action, Chain, EvidenceBasis, Lane, LaneMode, Signal, now_ms
from kaiba.execution import engine, lanes, snipe, viability
from kaiba.execution import evm_price as ep
from kaiba.ingest import launch_feed as lf
from tests.test_paper import write_dossier

W = 10**18
R = int(Decimal("6.14") * W)
H = 107_036_752 * W
K = int(Decimal("6797205657.28") * W)
SUPPLY = 10**9 * W
GRAD = 8 * 10**26
TOKEN = "0x" + "ab" * 18 + "7777"
CREATOR = "0x" + "c0" * 20
ERC20_QUOTE = "0x" + "e1" * 20
T0 = 1_791_000_000
PAPER = 129_420_000_000_000_000   # the repo's bsc min position (0.12942 BNB)
PLANNED = 155_773_349_776_075_800  # the repo's bsc max position
DEV_SOLD = 5_000_000 * W           # the dev's atomic buy: 5M tokens off the curve
NO_QUOTA = "0x" + "0" * 128


def word(n: int) -> str:
    return format(n, "064x")


def v8_hex(*, sold: int = DEV_SOLD, status: int = 1, quote: str | None = None, buy_tax: int = 0, sell_tax: int = 0,
           reserve_override: int | None = None) -> str:
    """A ``getTokenV8Safe`` record (18 words) consistent with the curve identity at ``sold``."""
    with localcontext() as ctx:
        ctx.prec = 60
        k, h, s = Decimal(K) / W, Decimal(H) / W, Decimal(SUPPLY) / W
        x = (Decimal(SUPPLY) - Decimal(sold)) / W
        reserve = k / (x + h) - k / (s + h)
        price = k / ((x + h) ** 2)
        words = [status, int(reserve * W) if reserve_override is None else reserve_override, sold, int(price * W), 6,
                 R, H, K, GRAD, int(quote or "0x0", 16), 0, 0, buy_tax, sell_tax, 0, sold * W // GRAD, 0, 0]
    return "0x" + "".join(word(w) for w in words)


def quota_hex(bps: int = 0, max_atoms: int = 0) -> str:
    return "0x" + word(bps) + word(max_atoms)


_UNSET = object()


def results(*, v8: str | None = None, quota: str | None = None, quoted: int | None = None,
            supply: int = SUPPLY, quote_raw: object = _UNSET) -> list:
    """The five answers of ``flap_calls``. ``quoted`` -> a one-word quote; ``quote_raw`` sets
    the raw answer instead (``None`` = the call reverted, ``"0x"`` = an empty return)."""
    raw = ("0x" + word(quoted) if quoted is not None else None) if quote_raw is _UNSET else quote_raw
    return [v8 if v8 is not None else v8_hex(), hex(18), hex(supply), quota if quota is not None else quota_hex(), raw]


def clean_flap(**kw) -> snipe.FlapLaunch:
    """A fresh, native, 0/0-tax, quota-free curve whose venue quote equals the curve model."""
    first = snipe.flap_launch_from_results(TOKEN, results(**kw), planned_size=PLANNED, paper_size=PAPER)
    if "quoted" in kw or "quote_raw" in kw:
        return first
    return snipe.flap_launch_from_results(TOKEN, results(quoted=first.paper_tokens_model, **kw),
                                          planned_size=PLANNED, paper_size=PAPER)


def bsc_launch(**over) -> lf.Launch:
    base = lf.Launch(chain=Chain.BSC, token=TOKEN, venue="flap", creator=CREATOR, launched_ms=T0 * 1000,
                     received_ms=T0 * 1000 + 900, block=125_000_000, tx="0x" + "11" * 32,
                     meta={"source": lf.FLAP_SOURCE})
    return replace(base, **over)


def runner() -> snipe.Record:
    return snipe.Record("w", 3, 5, 1, EvidenceBasis.DERIVED)  # low/runner


P = {**snipe.DEFAULT_PARAMS, "chains": ["robinhood", "sol", "bsc"]}


def verdict(flap=None, *, native_ok=True, p=P, launch=None, record=None) -> snipe.Verdict:
    return snipe.evaluate(launch or bsc_launch(), p, record or runner(), flap=clean_flap() if flap is None else flap,
                          native_ok=native_ok)


# ------------------------------------------------------------------------- the positive path

def test_a_clean_native_untaxed_quota_free_flap_launch_fires_and_says_what_it_read():
    v = verdict()
    assert v.fire and v.rule == "record:low/runner" and v.reasons == [], v.reasons
    f = v.features
    assert f["quote_native"] is True and f["buy_tax_bps"] == 0 and f["sell_tax_bps"] == 0 and f["flap_status"] == 1
    assert f["quota_bps"] == 0 and f["quote_vs_model_bps"] == 10_000 and Decimal(f["progress_pct"]) == Decimal("0.625")
    assert int(f["planned_tokens"]) > 0 and f["quote_raised_wei"] != "0"


def test_a_watched_dev_fires_on_bsc_too_and_every_guard_still_applies_to_it():
    p = {**P, "dev_watchlist": [CREATOR.upper().replace("0X", "0x")]}
    assert verdict(p=p, record=snipe.Record(None, 0, 0, 0, EvidenceBasis.DERIVED)).rule == "dev_watchlist"
    taxed = clean_flap(v8=v8_hex(sell_tax=899))
    assert not verdict(taxed, p=p).fire


def test_bsc_is_never_in_the_trusted_early_rule_and_a_no_alpha_launch_does_not_fire():
    v = verdict(record=snipe.Record("w", 3, 0, 0, EvidenceBasis.DERIVED))
    assert v.reasons == ["no_alpha:not_watched_and_record_low/no_prior"]
    assert "bsc" not in snipe.DEFAULT_PARAMS["trusted_early_chains"]
    assert "bsc" not in get_risk().lane(snipe.lane()).params["trusted_early_chains"]


# ------------------------------------------------------------------------- each guard alone

def test_guard_quote_must_be_bnb():
    clean = clean_flap()
    erc20 = replace(clean, record=replace(clean.record, quote_token=ERC20_QUOTE))
    assert verdict(erc20).reasons == [f"quote_not_native:{ERC20_QUOTE}"]
    wbnb = replace(clean, record=replace(clean.record, quote_token=ep.BSC_WRAPPED_NATIVE))
    assert verdict(wbnb).fire  # WBNB is BNB
    # and from the chain: an ERC-20-quoted record is not priced as a BNB curve at all
    real = snipe.flap_launch_from_results(TOKEN, results(v8=v8_hex(quote=ERC20_QUOTE), quoted=10**24),
                                          planned_size=PLANNED, paper_size=PAPER)
    assert real.curve is None and real.price_note == f"quote_not_native:{ERC20_QUOTE}"
    assert set(verdict(real).reasons) == {f"quote_not_native:{ERC20_QUOTE}", f"unpriceable:quote_not_native:{ERC20_QUOTE}"}


def test_guard_token_tax_from_the_portal_record_against_max_entry_tax_bps():
    taxed = clean_flap(v8=v8_hex(sell_tax=300))
    assert taxed.curve is not None and taxed.curve.sell_tax_bps == 300
    assert verdict(taxed).reasons == ["token_tax:0/300bps>0"]
    assert verdict(taxed, p={**P, "max_entry_tax_bps": {"robinhood": 0, "bsc": 300}}).fire
    clean = clean_flap()
    unread = replace(clean, record=replace(clean.record, buy_tax_bps=None))
    assert verdict(unread).reasons == ["token_tax_unread"]


def test_guard_token_tax_refuses_a_buy_only_tax_too():
    """Mutant M2 (only the sell leg compared) survived until this pin (review 2026-10-05)."""
    buy_taxed = clean_flap(v8=v8_hex(buy_tax=300, sell_tax=0))
    assert verdict(buy_taxed).reasons == ["token_tax:300/0bps>0"]


def test_guard_buy_quota_must_cover_the_largest_size_the_engine_could_send():
    """Flap REFUNDS the excess of a capped buy instead of reverting. MEASURED 2026-10-05: every
    quota seen was 2% of supply (20M tokens, ~0.114 BNB of a fresh curve) -- under our size."""
    clean = clean_flap()
    need = clean.planned_tokens
    assert need is not None and need > 20_000_000 * W
    capped = clean_flap(quota=quota_hex(200, 20_000_000 * W))
    assert verdict(capped).reasons == [f"buy_quota_below_size:{20_000_000 * W}<{need}"]
    roomy = clean_flap(quota=quota_hex(500, 50_000_000 * W))
    assert roomy.quota.active and verdict(roomy).fire
    unread = replace(clean, quota=None)
    assert verdict(unread).reasons == ["buy_quota_unread"]
    assert ep.parse_buy_quota("0x") is None and not ep.parse_buy_quota(quota_hex()).active


def test_guard_protection_must_be_able_to_price_the_curve():
    """The record is run through the same cross-checks read_flap applies for protection; a
    supply that breaks K = r(h + S) is refused there, so it is refused here."""
    broken = snipe.flap_launch_from_results(TOKEN, results(supply=SUPPLY * 2, quoted=10**24), planned_size=PLANNED,
                                            paper_size=PAPER)
    assert broken.read and broken.curve is None and broken.price_note.startswith("flap_supply_identity_failed")
    reasons = verdict(broken).reasons
    assert len(reasons) == 1 and reasons[0].startswith("unpriceable:flap_supply_identity_failed"), reasons


def test_guard_bnb_native_price_must_be_on_the_books():
    assert verdict(native_ok=False).reasons == ["no_native_price:bsc"]
    assert verdict(native_ok=None).reasons == ["no_native_price:bsc"]


def test_guard_the_venue_quote_must_not_fall_short_of_the_curve():
    model = clean_flap().paper_tokens_model
    short = clean_flap(quoted=model * 77 // 100)  # what a 2% quota cap looked like on 2026-10-05
    assert verdict(short).reasons == [f"quote_short_of_curve:{(model * 77 // 100) * 10_000 // model}bps"]
    assert 7_690 < (model * 77 // 100) * 10_000 // model <= 7_700
    assert verdict(clean_flap(quoted=model * 995 // 1000)).fire  # rounding is not a cap


def test_guard_the_venue_quote_fails_closed_when_it_was_not_read():
    """A reverted or empty quoteExactInput is when a buy would be restricted; it refuses, as
    an unread tax or quota does (review 2026-10-05: it used to fire with no reason)."""
    assert verdict(clean_flap(quote_raw=None)).reasons == ["venue_quote_unread"]
    assert verdict(clean_flap(quote_raw="0x")).reasons == ["venue_quote_unread"]


def test_guard_the_venue_quote_is_two_sided():
    """3x the model fired before (review 2026-10-05) and booked 3x the tokens."""
    model = clean_flap().paper_tokens_model
    above = clean_flap(quoted=model * 3)
    assert verdict(above).reasons == ["quote_above_curve:30000bps"]
    assert verdict(clean_flap(quoted=model * 1005 // 1000)).fire  # within the band


def test_guard_a_graduated_or_unread_or_stale_launch_does_not_fire():
    clean = clean_flap()
    graduated = replace(clean, record=replace(clean.record, status=ep.FLAP_STATUS_DEX))
    assert verdict(graduated).reasons == ["flap_not_on_curve:status=4"]
    assert snipe.evaluate(bsc_launch(), P, runner(), flap=None, native_ok=True).reasons == ["flap_unread:not_read"]
    stale = bsc_launch(received_ms=T0 * 1000 + 61_000)
    assert verdict(launch=stale).reasons == ["launch_stale:61000ms>60000ms"]


# ------------------------------------------------------------------------- reading the portal

def test_the_read_is_one_batch_of_five_calls_with_the_quote_asked_from_a_synthetic_origin():
    seen = {}

    def fake(calls, **kw):
        seen["calls"], seen["kw"] = calls, kw
        return results(quoted=10**24)

    got = snipe.read_flap_launch(bsc_launch(), P, rpc=fake, priority=snipe.Priority.ENTRY)
    calls = seen["calls"]
    assert len(calls) == 5 and seen["kw"]["priority"] is snipe.Priority.ENTRY
    assert calls[0][1][0] == {"to": ep.FLAP_PORTAL, "data": ep.SEL_GET_TOKEN_V8_SAFE + TOKEN[2:].rjust(64, "0")}
    assert calls[3][1][0]["data"].startswith(ep.SEL_MAX_BUY_PER_ORIGIN)
    qei = calls[4][1][0]
    spend = PAPER * (10_000 - P["bsc_router_bps_per_leg"]) // 10_000  # GMGN's commission comes off first
    assert qei["from"] == snipe.PAPER_WHO and qei["data"].startswith(ep.SEL_QUOTE_EXACT_INPUT)
    assert qei["data"].endswith(word(spend)) and len(qei["data"]) == 10 + 3 * 64
    assert got.read and got.paper_tokens_quoted == 10**24 and got.paper_spend == spend and got.paper_size == PAPER
    # the quota is checked at the LARGEST size the engine could send, not the paper size
    assert got.planned_size == get_risk().chain_budget(Chain.BSC).max_position_base_units
    failed = snipe.read_flap_launch(bsc_launch(), P, rpc=lambda calls, **kw: None)
    assert not failed.read and failed.note == "flap_rpc_failed"


def _risk_with_bsc_max(max_units: int) -> RiskConfig:
    cfg = load_risk()
    return cfg.model_copy(update={"chains": {
        **cfg.chains, Chain.BSC: cfg.chain_budget(Chain.BSC).model_copy(update={"max_position_base_units": max_units})}})


def test_the_planned_size_is_the_larger_of_the_paper_size_and_the_chains_max_position():
    """Mutant M4 (``return paper``) survived until this pin: a quota between the paper size
    and the max position would pass while the engine could size above it."""
    assert snipe.planned_bsc_size(P, cfg=_risk_with_bsc_max(PAPER * 2)) == PAPER * 2
    assert snipe.planned_bsc_size(P, cfg=_risk_with_bsc_max(PAPER // 2)) == PAPER


def test_the_record_decodes_words_ten_to_seventeen_and_a_short_one_leaves_them_unknown():
    rec = ep.flap_record(snipe._words_of(v8_hex(buy_tax=100, sell_tax=300)))
    assert (rec.buy_tax_bps, rec.sell_tax_bps, rec.token_version, rec.pool) == (100, 300, 6, None)
    short = ep.flap_record(snipe._words_of(v8_hex())[:10])
    assert short is not None and short.buy_tax_bps is None and short.sell_tax_bps is None


@pytest.mark.parametrize("tax,venue_tokens_m", [(100, Decimal("25.88")), (300, Decimal("25.37")), (899, Decimal("23.82"))])
def test_the_curve_model_reproduces_the_venues_own_quotes_with_fee_and_tax_off_the_input(tax, venue_tokens_m):
    """MEASURED 2026-10-05: quoteExactInput(0.15 BNB) on a fresh native curve at 1/3/8.99% tax."""
    fresh = ep.FlapCurve(r_scaled=R, h_scaled=H, k_scaled=K, total_supply_atoms=SUPPLY, tokens_sold_atoms=0,
                         graduation_tokens_atoms=GRAD, quote_reserve_base=0, token_decimals=18, quote_decimals=18)
    got = Decimal(fresh.tokens_out(ep.flap_buy_net_quote(15 * 10**16, tax))) / W / 10**6
    assert abs(got - venue_tokens_m) / venue_tokens_m < Decimal("0.0005"), got


# ------------------------------------------------------------------------- paper entry and marks

def test_the_paper_entry_books_the_smaller_of_quote_and_model_and_the_bnb_that_reached_the_curve():
    flap = clean_flap()
    entry = snipe.paper_entry_bsc(bsc_launch(), P, flap)
    assert entry["basis"] == "flap_decision:quote" and entry["tokens"] == flap.paper_tokens_quoted
    assert entry["quote_in"] == PAPER and entry["shadow_quote"] == PAPER * 99 // 100
    assert entry["entry_curve"]["sold"] == DEV_SOLD and entry["entry_curve"]["sell_tax_bps"] == 0
    # a quote ABOVE the model books the model: never tokens the curve cannot deliver
    above = snipe.paper_entry_bsc(bsc_launch(), P, replace(flap, paper_tokens_quoted=flap.paper_tokens_model * 3))
    assert above["basis"] == "flap_decision:model" and above["tokens"] == flap.paper_tokens_model
    unread = snipe.paper_entry_bsc(bsc_launch(), P, replace(flap, paper_tokens_quoted=None))
    assert unread["basis"] == "flap_decision:quote_unread" and "tokens" not in unread
    assert snipe.paper_entry_bsc(bsc_launch(), P, replace(flap, curve=None, price_note="x"))["basis"] == \
        "flap_decision:curve_unpriced"


def test_gmgns_commission_comes_off_the_paper_buy_before_the_venue_sees_it():
    flap = routed_flap(100)
    entry = snipe.paper_entry_bsc(bsc_launch(), P, flap)
    spend = PAPER * 9_900 // 10_000
    assert flap.paper_spend == spend and entry["quote_in"] == PAPER and entry["router_bps"] == 100
    assert entry["shadow_quote"] == spend * 99 // 100 and entry["tokens"] < clean_flap().paper_tokens_model


def test_a_mark_on_an_unmoved_curve_loses_the_fee_and_the_commission_each_way():
    flap = clean_flap()
    entry = snipe.paper_entry_bsc(bsc_launch(), P, flap)
    value, note, status = snipe.flap_mark_value(v8_hex(), entry["entry_curve"], entry["shadow_quote"], entry["tokens"])
    assert note is None and status is None
    assert Decimal("0.975") < Decimal(value) / PAPER < Decimal("0.985")  # 1% in, 1% out, nothing else
    routed, _, _ = snipe.flap_mark_value(v8_hex(), entry["entry_curve"], entry["shadow_quote"], entry["tokens"],
                                         router_bps=100)
    assert Decimal(routed) / Decimal(value) == pytest.approx(Decimal("0.9898"), abs=Decimal("0.0002"))


def test_a_graduated_curve_is_sent_to_the_dex_and_a_killed_one_is_zero():
    flap = clean_flap()
    entry = snipe.paper_entry_bsc(bsc_launch(), P, flap)
    got = snipe.flap_mark_value(v8_hex(status=ep.FLAP_STATUS_DEX), entry["entry_curve"], entry["shadow_quote"],
                                entry["tokens"])
    assert got == (None, "graduated", snipe.FLAP_MARK_ON_DEX)
    assert snipe.flap_mark_value(v8_hex(status=ep.FLAP_STATUS_KILLED), entry["entry_curve"], 1, 1)[:2] == (0, "flap_killed")
    assert snipe.flap_mark_value("0x", entry["entry_curve"], 1, 1) == (None, "flap_record_unread", None)


def amounts_out(amount_in: int, out: int) -> str:
    """``getAmountsOut``'s ``uint256[2]`` return."""
    return "0x" + word(0x20) + word(2) + word(amount_in) + word(out)


def test_a_graduate_is_valued_on_its_pancake_pair_net_of_tax_and_commission():
    call = snipe.flap_dex_call(TOKEN, 10**24)
    to, data = ep.encode_amounts_out(10**24, [TOKEN.lower(), ep.BSC_WRAPPED_NATIVE])
    assert call == ("eth_call", [{"to": to, "data": data}, "latest"]) and to == ep.PANCAKE_V2_ROUTER
    assert snipe.flap_dex_value(amounts_out(10**24, 10**18), 100, router_bps=100) == 10**18 * 9_800 // 10_000
    assert snipe.flap_dex_value(None, 0) is None  # a revert is no value, not zero


def routed_flap(router_bps: int = 100) -> snipe.FlapLaunch:
    """``clean_flap`` with GMGN's commission taken off the paper buy, as the service reads it."""
    first = snipe.flap_launch_from_results(TOKEN, results(), planned_size=PLANNED, paper_size=PAPER,
                                           router_bps=router_bps)
    return snipe.flap_launch_from_results(TOKEN, results(quoted=first.paper_tokens_model), planned_size=PLANNED,
                                          paper_size=PAPER, router_bps=router_bps)


def _observation(tmp_db, *, token: str = TOKEN, at_ms: int = T0 * 1000) -> None:
    flap = routed_flap()
    launch = bsc_launch(token=token)
    v = verdict(flap, launch=launch)
    entry = snipe.paper_entry_bsc(launch, P, flap)
    v.features["entry_curve"] = entry["entry_curve"]
    snipe.record_observation(tmp_db, launch, v, entry, at_ms=at_ms)


def test_mark_bsc_marks_due_observations_from_one_batched_read(tmp_db):
    _observation(tmp_db)
    batches = []

    def fake(calls, **kw):
        batches.append(calls)
        return [v8_hex()] * len(calls)

    at = T0 * 1000 + 301_000
    assert snipe.mark_bsc(tmp_db, snipe.due_marks(tmp_db, [300, 900], at_ms=at), [300, 900], at_ms=at, rpc=fake,
                          router_bps=100) == 1
    assert len(batches) == 1 and len(batches[0]) == 1
    row = fetch_one(tmp_db, f"SELECT * FROM {snipe.TABLE}")
    ratio = Decimal(jload(row["marks_json"])["300"]["value"]) / PAPER
    assert row["status"] == "open" and Decimal("0.955") < ratio < Decimal("0.965"), ratio  # fee + commission, both legs


def test_mark_bsc_caps_each_batch(tmp_db):
    for i in range(snipe.BSC_MARK_BATCH_MAX + 5):
        _observation(tmp_db, token="0x" + format(i, "02x") * 18 + "7777")
    sizes = []

    def fake(calls, **kw):
        sizes.append(len(calls))
        return [v8_hex()] * len(calls)

    at = T0 * 1000 + 301_000
    rows = snipe.due_marks(tmp_db, [300], at_ms=at)
    assert len(rows) == snipe.BSC_MARK_BATCH_MAX + 5
    assert snipe.mark_bsc(tmp_db, rows, [300], at_ms=at, rpc=fake, router_bps=0) == snipe.BSC_MARK_BATCH_MAX
    assert sizes == [snipe.BSC_MARK_BATCH_MAX]


def test_a_graduate_keeps_being_marked_on_the_pair_at_every_horizon(tmp_db):
    """Until 2026-10-05 a graduate was frozen at the entry curve's graduation point."""
    _observation(tmp_db)
    row0 = fetch_one(tmp_db, f"SELECT entry_tokens FROM {snipe.TABLE}")
    tokens = int(row0["entry_tokens"])
    dex_outs = iter([3 * PAPER, PAPER // 2])
    seen = []

    def fake(calls, **kw):
        seen.append([c[1][0]["to"] for c in calls])
        if calls[0][1][0]["to"] == ep.FLAP_PORTAL:
            return [v8_hex(status=ep.FLAP_STATUS_DEX)] * len(calls)
        return [amounts_out(tokens, next(dex_outs)) for _ in calls]

    for at in (T0 * 1000 + 301_000, T0 * 1000 + 901_000):
        snipe.mark_bsc(tmp_db, snipe.due_marks(tmp_db, [300, 900], at_ms=at), [300, 900], at_ms=at, rpc=fake,
                       router_bps=100)
    row = fetch_one(tmp_db, f"SELECT * FROM {snipe.TABLE}")
    marks = jload(row["marks_json"])
    assert seen == [[ep.FLAP_PORTAL], [ep.PANCAKE_V2_ROUTER], [ep.FLAP_PORTAL], [ep.PANCAKE_V2_ROUTER]]
    # a 0-tax token: the pair fee is inside getAmountsOut, so only the commission comes off
    assert int(marks["300"]["value"]) == 3 * PAPER * 9_900 // 10_000 and marks["300"]["note"] == "graduated_pancake_v2"
    assert int(marks["900"]["value"]) == (PAPER // 2) * 9_900 // 10_000
    assert row["status"] == "marked"


# ------------------------------------------------------------------------- the fill re-read

def test_the_fill_is_re_read_after_the_latency_and_booked_from_that_curve():
    decided = clean_flap()
    later = v8_hex(sold=DEV_SOLD + 30_000_000 * W)  # others bought in the meantime: a dearer curve
    seen = {}

    def fake(calls, **kw):
        seen["calls"] = calls
        model = snipe.flap_fill_from_results(TOKEN, [later, None], decided).paper_tokens_model
        return [later, "0x" + word(model)]

    fill = snipe.read_flap_fill(bsc_launch(), P, decided, rpc=fake)
    assert len(seen["calls"]) == 2 and seen["calls"][1][1][0]["data"].endswith(word(decided.paper_spend))
    assert fill.read and fill.note == "fill" and fill.quota == decided.quota
    entry = snipe.paper_entry_bsc(bsc_launch(), P, fill)
    assert entry["basis"] == "flap_fill:quote" and entry["tokens"] < decided.paper_tokens_model
    assert entry["entry_curve"]["sold"] == DEV_SOLD + 30_000_000 * W
    unread = snipe.read_flap_fill(bsc_launch(), P, decided, rpc=lambda calls, **kw: None)
    assert snipe.paper_entry_bsc(bsc_launch(), P, unread)["basis"] == "flap_fill:curve_unread"


def test_the_service_waits_the_latency_before_the_fill(tmp_db, monkeypatch):
    p = {**P, "measure_bsc_every": 1000, "bsc_exec_latency_ms": 7_000}
    s = sniper(tmp_db, monkeypatch, p)
    decided = clean_flap()
    monkeypatch.setattr(snipe, "read_flap_launch", lambda launch, p, **kw: decided)
    monkeypatch.setattr(snipe, "record_for", lambda conn, chain, wallet, **kw: runner())
    monkeypatch.setattr(snipe, "hand_to_engine", lambda *a, **k: (None, "stand_in"))
    fills, slept = [], []
    monkeypatch.setattr(snipe, "read_flap_fill",
                        lambda launch, p, d, **kw: fills.append(d) or replace(d, note="fill", read_ms=now_ms()))
    s._handle_bsc(tmp_db, bsc_launch(), p, sleep=slept.append)
    assert fills == [decided] and len(slept) == 1 and 6.5 < slept[0] <= 7.0
    obs = fetch_one(tmp_db, f"SELECT * FROM {snipe.TABLE}")
    assert obs["entry_basis"] == "flap_fill:quote" and obs["fire"] == 1


# ------------------------------------------------------------------------- the native price check

def test_the_native_price_check_reads_the_books_and_refuses_a_sample_from_the_future(tmp_db):
    now = T0 * 1000
    assert snipe.bsc_native_price_ok(tmp_db, at_ms=now) is False
    tmp_db.execute("INSERT INTO native_prices (chain, ts_ms, price_usd, source, receipt_json) VALUES ('bsc', ?, '600', 't', '{}')",
                   (now + 30_000,))
    assert snipe.bsc_native_price_ok(tmp_db, at_ms=now) is False  # within tolerance, but not yet observed
    tmp_db.execute("INSERT INTO native_prices (chain, ts_ms, price_usd, source, receipt_json) VALUES ('bsc', ?, '600', 't', '{}')",
                   (now - 20_000,))
    assert snipe.bsc_native_price_ok(tmp_db, at_ms=now - 15_000) is True


# ------------------------------------------------------------------------- the dedicated endpoint

def test_the_snipe_reads_never_fall_back_to_the_shared_bsc_key(monkeypatch):
    """BSC_RPC_URL's key serves protection's price reads; a snipe read must not spend it."""
    from kaiba.core.config import get_settings
    from kaiba.providers import _http

    monkeypatch.setenv("BSC_RPC_URL", "https://bnb-mainnet.example/v2/SHARED")
    monkeypatch.delenv(lf.BSC_SNIPE_RPC_ENV, raising=False)
    monkeypatch.setattr(lf, "bsc_snipe_rpc_url", lambda: None)
    get_settings.cache_clear()
    sent = []
    monkeypatch.setattr(_http, "post_json", lambda *a, **k: sent.append(a) or None)
    try:
        assert snipe.bsc_rpc([("eth_call", [{}, "latest"])]) is None and sent == []
    finally:
        get_settings.cache_clear()


def test_the_dedicated_endpoint_is_read_from_its_own_setting(monkeypatch, tmp_path):
    monkeypatch.setenv(lf.BSC_SNIPE_RPC_ENV, "https://bnb-mainnet.example/v2/OWN")
    assert lf.bsc_snipe_rpc_url() == "https://bnb-mainnet.example/v2/OWN"
    monkeypatch.delenv(lf.BSC_SNIPE_RPC_ENV)
    (tmp_path / ".env").write_text(f"{lf.BSC_SNIPE_RPC_ENV}=https://bnb-mainnet.example/v2/FROMFILE\n", encoding="utf-8")
    monkeypatch.setenv("KAIBA_CONFIG_DIR", str(tmp_path))
    assert lf.bsc_snipe_rpc_url() == "https://bnb-mainnet.example/v2/FROMFILE"


# ------------------------------------------------------------------------- paper only: the producer

def cfg(mode: str = "live", allowlist: dict | None = None, *, with_bsc_key: bool = True) -> RiskConfig:
    params = {"live_launchpads_by_chain": allowlist if allowlist is not None else ({"bsc": []} if with_bsc_key else {})}
    return RiskConfig.model_validate({"global_mode": "live", "bounds": {"max_lane_mode": "live"},
                                      "lanes": {"launch-snipe": {"mode": mode, "chains": ["bsc"], "params": params}}})


@pytest.mark.parametrize("mode", ["live", "canary"])
def test_paper_only_a_money_lane_signals_bsc_only_when_every_entry_is_a_shadow_twin(mode):
    """CANARY pinned too: mutant M3 (``mode is not LIVE``) survived until this test."""
    assert snipe.paper_only_refusal(bsc_launch(), cfg=cfg(mode, {"bsc": [], "robinhood": ["pons"]})) is None
    assert snipe.paper_only_refusal(bsc_launch(), cfg=cfg(mode, with_bsc_key=False)) == \
        "paper_only:bsc:live_allowlist_admits_flap"
    assert snipe.paper_only_refusal(bsc_launch(), cfg=cfg(mode, {"bsc": ["flap"]})) == \
        "paper_only:bsc:live_allowlist_admits_flap"
    # exactly empty, not "does not name this venue": the engine checks tokens.launchpad
    assert snipe.paper_only_refusal(bsc_launch(), cfg=cfg(mode, {"bsc": ["fourmeme"]})) == \
        "paper_only:bsc:live_allowlist_not_empty:fourmeme"
    sol = lf.Launch(chain=Chain.SOL, token="Mint", venue="pump.fun", creator="C")
    assert snipe.paper_only_refusal(sol, cfg=cfg(mode, with_bsc_key=False)) is None


def test_paper_only_a_shadow_lane_signals_bsc():
    assert snipe.paper_only_refusal(bsc_launch(), cfg=cfg("shadow", with_bsc_key=False)) is None


def test_the_shipped_config_lists_bsc_on_the_lane_and_keeps_it_paper_even_if_the_lane_goes_live():
    shipped = get_risk()
    lane_cfg = shipped.lane(snipe.lane())
    assert Chain.BSC in lane_cfg.chains and "bsc" in lane_cfg.params["chains"]
    assert lane_cfg.params["venues"]["bsc"] == ["flap"] and lane_cfg.params["live_launchpads_by_chain"]["bsc"] == []
    live = shipped.model_copy(update={"global_mode": LaneMode.LIVE, "lanes": {
        **shipped.lanes, snipe.lane(): lane_cfg.model_copy(update={"mode": LaneMode.LIVE})}})
    assert live.effective_mode(snipe.lane()) is LaneMode.LIVE
    assert snipe.paper_only_refusal(bsc_launch(), cfg=live) is None


def test_a_refused_paper_only_launch_spends_nothing(tmp_db):
    scans, waits = [], []
    got = snipe.hand_to_engine(tmp_db, bsc_launch(), verdict(), {**P, "token_row_wait_s": 0.01}, snipe.DossierBudget(5),
                               scan=lambda *a: scans.append(a), precheck=lambda *a, **k: waits.append(a) or (True, "ok"),
                               paper_guard=lambda launch: "paper_only:bsc:live_allowlist_admits_flap")
    assert got == (None, "paper_only:bsc:live_allowlist_admits_flap") and scans == [] and waits == []


@pytest.mark.parametrize("mode", ["live", "canary"])
def test_hand_to_engine_applies_the_paper_only_guard_by_default(tmp_db, monkeypatch, mode):
    """Mutant M12 (the default guard replaced by ``lambda _l: None``) survived until this."""
    monkeypatch.setattr(snipe, "get_risk", lambda: cfg(mode, {"bsc": ["flap"]}))
    scans, checks = [], []
    got = snipe.hand_to_engine(tmp_db, bsc_launch(), verdict(), {**P, "token_row_wait_s": 0.01}, snipe.DossierBudget(5),
                               scan=lambda *a, **k: scans.append(a),
                               precheck=lambda *a, **k: checks.append(a) or (True, "ok"))
    assert got == (None, "paper_only:bsc:live_allowlist_admits_flap") and scans == [] and checks == []


# ------------------------------------------------------------------------- paper only: the engine

class _Gate:
    """Isolate the engine from risk.py's arithmetic (as tests/test_engine_launchpad_allowlist.py)."""

    def check_entry(self, **kwargs):
        return None

    def position_size(self, chain, lane, score, conn=None, *, token=None):
        return PAPER


def _armed(monkeypatch, *, snipe_mode: LaneMode = LaneMode.LIVE, allow: object = _UNSET) -> RiskConfig:
    """The SHIPPED config with bsc enabled and funded, global live, launch-snipe in
    ``snipe_mode`` (the box runs it live) and, optionally, its bsc allowlist overridden."""
    base = load_risk()
    lane_cfg = base.lane(Lane.LAUNCH_SNIPE)
    params = dict(lane_cfg.params or {})
    if allow is not _UNSET:
        by_chain = dict(params.get(engine.LIVE_LAUNCHPADS_PARAM) or {})
        if allow is None:
            by_chain.pop("bsc", None)
        else:
            by_chain["bsc"] = allow
        params[engine.LIVE_LAUNCHPADS_PARAM] = by_chain
    armed = base.model_copy(update={
        "global_mode": LaneMode.LIVE, "kill_switch": False, "entries_paused": False, "reduce_only": False,
        "bounds": base.bounds.model_copy(update={"max_lane_mode": LaneMode.LIVE}),
        "lanes": {**base.lanes, Lane.LAUNCH_SNIPE: lane_cfg.model_copy(update={"mode": snipe_mode, "params": params})},
        "chains": {**base.chains, Chain.BSC: base.chain_budget(Chain.BSC).model_copy(
            update={"enabled": True, "bankroll_base_units": 10 * W})},
    })
    monkeypatch.setattr(engine, "get_risk", lambda: armed)
    monkeypatch.setattr(engine, "RiskGate", _Gate)
    monkeypatch.setattr(engine, "_protectability_probe", lambda chain, token: (True, "fixture"))
    return armed


def _ready_bsc(conn, token: str = TOKEN, launchpad: str = "flap") -> None:
    write_dossier(conn, token=token, chain=Chain.BSC, price="0.0000001", liquidity="50000")
    # migrated_ms set: the paper broker prices off the pool quote and never reads a curve
    conn.execute("INSERT OR REPLACE INTO tokens (chain, address, decimals, launchpad, migrated_ms, first_seen_ms) "
                 "VALUES ('bsc', ?, 18, ?, ?, ?)", (token, launchpad, now_ms() - 60_000, now_ms()))
    conn.commit()


def test_the_engine_paper_only_set_is_the_snipe_lanes():
    assert engine.PAPER_ONLY_LANE_CHAINS[Lane.LAUNCH_SNIPE] == snipe.PAPER_ONLY_CHAINS == frozenset({Chain.BSC})


@pytest.mark.parametrize("allow", [_UNSET, None, ["flap"]], ids=["shipped", "key_removed", "flap_listed"])
def test_a_live_bsc_snipe_is_a_shadow_twin_whatever_the_allowlist_says(tmp_db, monkeypatch, allow):
    """The money boundary (review 2026-10-05): the producer's guard is not enough -- a signal
    recorded before a config edit, or another producer, reaches the engine directly."""
    _armed(monkeypatch, allow=allow)
    assert engine.get_risk().effective_mode(Lane.LAUNCH_SNIPE) is LaneMode.LIVE
    _ready_bsc(tmp_db)
    sig = snipe.build_signal(bsc_launch(), verdict())
    lanes.record(sig, tmp_db)
    [live] = engine.run_once(tmp_db)
    assert live.action is Action.SKIP and live.mode is LaneMode.LIVE
    assert live.blockers == ["launchpad_not_live:flap"], (live.blockers, live.thesis)
    twin = engine.load_decision(engine.shadow_twin_id(sig.signal_id), tmp_db)
    assert twin is not None and (twin.mode, twin.action) == (LaneMode.SHADOW, Action.ENTER)
    assert tmp_db.execute("SELECT COUNT(*) FROM orders WHERE mode != 'shadow'").fetchone()[0] == 0
    assert all(r["mode"] == "shadow" for r in fetch_all(tmp_db, "SELECT mode FROM positions"))


@pytest.mark.parametrize("snipe_mode", [LaneMode.LIVE, LaneMode.CANARY])
def test_launch_snipe_can_never_enter_bsc_with_money_once_bsc_is_enabled(tmp_db, monkeypatch, snipe_mode):
    """Enabling chains.bsc must never put money on a bsc SNIPE. OWNER DECISION 2026-10-05: the
    sm-trenches bsc lane trades LIVE by his choice, so it is deliberately not fenced here; this
    pins only the paper-only snipe lane (and any future paper-only lane in PAPER_ONLY_LANE_CHAINS)."""
    armed = _armed(monkeypatch, snipe_mode=snipe_mode)
    on_bsc = [lane for lane, lc in armed.lanes.items()
              if Chain.BSC in lc.chains and Chain.BSC in engine.PAPER_ONLY_LANE_CHAINS.get(lane, frozenset())]
    assert Lane.LAUNCH_SNIPE in on_bsc
    for i, lane in enumerate(on_bsc):
        token = "0x" + format(i + 1, "02x") * 18 + "7777"
        _ready_bsc(tmp_db, token)
        sig = Signal(signal_id=f"sig_bsc_fence_{lane.value}", lane=lane, chain=Chain.BSC, token=token, strength=0.75,
                     reasons=["fixture"], payload={"smart_wallets": 4, "entity_count": 3, "thesis": "fixture"})
        d = engine.decide(sig, tmp_db, twins=[])
        money = d.mode in (LaneMode.LIVE, LaneMode.CANARY)
        assert not (money and d.action is Action.ENTER), (lane.value, d.mode, d.action, d.blockers)


# ------------------------------------------------------------------------- the pre-dossier band

def _priced_curve(**kw):
    record = ep.flap_record(snipe._words_of(v8_hex(**kw)))
    return ep.parse_flap_record(record, token_decimals=18, token_supply_atoms=SUPPLY, quote_decimals=18)


@pytest.fixture
def portal(monkeypatch):
    """The portal read faked, and the transport answering only the Buy Quota read."""
    state = {"curve": {}, "quota": NO_QUOTA, "calls": []}
    viability.reset_venue_cache()
    monkeypatch.setattr(ep, "read_flap", lambda token, rpc: _priced_curve(**state["curve"]))

    def transport(calls):
        state["calls"].extend(calls)
        return [state["quota"] for _ in calls]

    monkeypatch.setattr(ep, "json_rpc_batch", lambda *a, **kw: transport)
    monkeypatch.setattr(viability, "_evm_gas_price_wei", lambda chain, conn: (50_000_000, "gas:test"))
    yield state
    viability.reset_venue_cache()


def put_tax_dossier(conn, buy: str, sell: str, token: str = TOKEN) -> None:
    def measure(value: str) -> dict:
        return {"value": value, "basis": "provider_reported",
                "receipt": {"provider": "gmgn", "endpoint": "token.security", "observed_at_ms": now_ms(),
                            "basis": "provider_reported"}, "freshness_budget_s": 900}

    body = {"address": token, "chain": "bsc", "built_at_ms": now_ms(), "grade": "B",
            "buy_tax_bps": measure(buy), "sell_tax_bps": measure(sell)}
    conn.execute("INSERT OR REPLACE INTO token_dossiers (chain, address, built_at_ms, score, grade, blockers_json, "
                 "warnings_json, unknowns_json, dossier_json) VALUES ('bsc',?,?,?,?,?,?,?,?)",
                 (token, now_ms(), None, "B", "[]", "[]", "[]", jdump(body)))
    conn.commit()


@pytest.mark.parametrize("tax,sizes_at_min", [(0, True), (100, False)])
def test_the_band_prices_a_fresh_flap_curve_from_its_on_chain_tax_with_no_dossier(tmp_db, portal, tax, sizes_at_min):
    """Until 2026-10-05 the tax came only from the DOSSIER, which the snipe precheck runs
    before -- so every Flap launch was refused cost_model_unavailable:token_tax."""
    portal["curve"] = {"buy_tax": tax, "sell_tax": tax}
    venue = viability.read_venue(Chain.BSC, TOKEN, tmp_db)
    assert venue.priced and venue.tax_bps_per_leg == Decimal(tax) and venue.fee_bps_per_leg == Decimal(100), venue.note
    assert f"tax{tax}bps_onchain({tax}/{tax})_no_dossier" in venue.note
    band = viability.sizing_band(Chain.BSC, tmp_db, token=TOKEN, ceiling_pct=Decimal("7.0"), tolerance_bps=2500)
    assert band.reason == "band", band.reason
    assert (band.max_viable_base_units >= PAPER) is sizes_at_min, band.max_viable_base_units


@pytest.mark.parametrize("dossier,onchain,charged", [
    (("300", "300"), (0, 0), 300),   # the dossier reads higher: it still binds
    (None, (0, 100), 100),           # no dossier: the worse on-chain leg (the commonest graduate)
    (None, (100, 0), 100),
    (("0", "100"), (0, 300), 300),   # the chain reads higher: it binds
], ids=["dossier_higher", "sell_only", "buy_only", "chain_higher"])
def test_the_tax_charged_is_the_worse_of_chain_and_dossier(tmp_db, portal, dossier, onchain, charged):
    """Mutant M1 (only the buy leg charged) survived 231 tests until these (review 2026-10-05)."""
    if dossier is not None:
        put_tax_dossier(tmp_db, *dossier)
    portal["curve"] = {"buy_tax": onchain[0], "sell_tax": onchain[1]}
    venue = viability.read_venue(Chain.BSC, TOKEN, tmp_db)
    assert venue.tax_bps_per_leg == Decimal(charged), venue.note


def test_a_record_without_tax_words_still_takes_the_dossier_answer(tmp_db, monkeypatch, portal):
    short = ep.flap_record(snipe._words_of(v8_hex())[:10])
    monkeypatch.setattr(ep, "read_flap", lambda token, rpc: ep.parse_flap_record(
        short, token_decimals=18, token_supply_atoms=SUPPLY, quote_decimals=18))
    venue = viability.read_venue(Chain.BSC, TOKEN, tmp_db)
    assert venue.tax_bps_per_leg is None and not venue.priced and "no_dossier" in venue.note


@pytest.mark.parametrize("quota,refusal", [
    (NO_QUOTA, None),
    (quota_hex(500, 50_000_000 * W), None),                                    # roomy: covers the max position
    (quota_hex(200, 20_000_000 * W), "flap_probe_unavailable:buy_quota_below_size:"),
    (None, "flap_probe_unavailable:buy_quota_unread"),                         # unread is not "no quota"
], ids=["no_quota", "roomy", "capped", "unread"])
def test_every_bsc_lane_refuses_a_flap_curve_whose_buy_quota_could_cap_its_largest_size(tmp_db, portal, quota, refusal):
    """sm-trenches sizes through here too, live, with no quota check of its own: a capped
    buy is REFUNDED, not reverted, and would be booked at a size and price it never had."""
    portal["quota"] = quota
    venue = viability.read_venue(Chain.BSC, TOKEN, tmp_db)
    assert portal["calls"] == [(ep.FLAP_PORTAL, ep.SEL_MAX_BUY_PER_ORIGIN + TOKEN[2:].rjust(64, "0"))]
    if refusal is None:
        assert venue.priced, venue.note
    else:
        assert not venue.priced and venue.note.startswith(refusal), venue.note


# ------------------------------------------------------------------------- the service

def sniper(tmp_db, monkeypatch, p):
    monkeypatch.setattr(snipe, "get_conn", lambda: tmp_db)
    monkeypatch.setattr(snipe, "params", lambda cfg=None: p)
    monkeypatch.setattr(snipe, "bsc_native_price_ok", lambda conn, **kw: True)
    monkeypatch.setattr(snipe, "snipes_today", lambda conn, chain, **kw: 0)
    return snipe.Sniper(tmp_db, chains=["bsc"])


def test_a_launch_with_no_alpha_is_not_read_unless_sampled(tmp_db, monkeypatch):
    p = {**P, "measure_bsc_every": 3, "bsc_exec_latency_ms": 0}
    s = sniper(tmp_db, monkeypatch, p)
    reads = []
    monkeypatch.setattr(snipe, "read_flap_launch", lambda launch, p, **kw: reads.append(kw["priority"]) or clean_flap())
    monkeypatch.setattr(snipe, "read_flap_fill", lambda launch, p, d, **kw: replace(d, note="fill"))
    monkeypatch.setattr(snipe, "record_for", lambda conn, chain, wallet, **kw: snipe.Record(wallet, 1, 0, 0, EvidenceBasis.DERIVED))
    for i in range(6):
        s._handle(bsc_launch(token="0x" + format(i, "02x") * 18 + "7777"))
    assert reads == [snipe.Priority.DISCOVERY] * 2 and s.stats["bsc_unsampled"] == 4
    rows = fetch_one(tmp_db, f"SELECT COUNT(*) AS n, SUM(fire) AS f FROM {snipe.TABLE} WHERE chain='bsc'")
    assert rows["n"] == 2 and rows["f"] == 0


def test_a_firing_flap_launch_is_measured_and_handed_to_the_engine_with_its_own_budget(tmp_db, monkeypatch):
    p = {**P, "measure_bsc_every": 1000, "dossier_budget_by_chain": {"bsc": 3}, "bsc_exec_latency_ms": 0}
    s = sniper(tmp_db, monkeypatch, p)
    monkeypatch.setattr(snipe, "read_flap_launch", lambda launch, p, **kw: clean_flap())
    monkeypatch.setattr(snipe, "read_flap_fill", lambda launch, p, d, **kw: replace(d, note="fill"))
    monkeypatch.setattr(snipe, "record_for", lambda conn, chain, wallet, **kw: runner())
    handed = []

    def stand_in(conn, launch, v, p, budget, **kw):
        handed.append((launch.token, v.rule, budget))
        return "sig_x", "dossier:B:signal"

    monkeypatch.setattr(snipe, "hand_to_engine", stand_in)
    s._handle(bsc_launch())
    obs = fetch_one(tmp_db, f"SELECT * FROM {snipe.TABLE}")
    assert obs["fire"] == 1 and obs["rule"] == "record:low/runner" and obs["status"] == "open"
    assert obs["entry_basis"] == "flap_fill:quote" and obs["signal_id"] == "sig_x"
    assert jload(obs["features_json"])["entry_curve"]["k"] == K
    assert len(handed) == 1 and handed[0][2] is not s.budget and handed[0][2].per_hour == 3
    assert s.budget_for(Chain.SOL) is s.budget


def test_a_stale_flap_launch_spends_no_read(tmp_db, monkeypatch):
    s = sniper(tmp_db, monkeypatch, {**P, "measure_bsc_every": 1})

    def boom(*a, **k):
        raise AssertionError("a stale launch was read")

    monkeypatch.setattr(snipe, "read_flap_launch", boom)
    s._handle(bsc_launch(received_ms=T0 * 1000 + 120_000))
    assert s.stats["bsc_stale"] == 1


def test_the_robinhood_toll_wait_reads_a_per_chain_max_entry_tax(monkeypatch):
    monkeypatch.setattr(snipe, "rh_rpc", lambda calls, **kw: [{"timestamp": hex(T0 + 1), "number": hex(5)}])
    rh_launch = lf.Launch(chain=Chain.ROBINHOOD, token="0x" + "12" * 20, venue="pons", creator=CREATOR,
                          launched_ms=T0 * 1000)
    got = snipe.wait_for_tax(rh_launch, {**P, "max_entry_tax_bps": {"robinhood": 618, "bsc": 0}},
                             sleep=lambda s: None, wall=lambda: T0 + 2)
    assert got == (T0 + 1, 5)


def test_the_paper_broker_charges_bsc_the_router_and_the_venue(tmp_db):
    """60 bps a leg against ~2% real flattered every bsc twin by ~2.8% a round trip."""
    from kaiba.execution.paper import DEFAULT_FEE_BPS, PaperBroker

    assert DEFAULT_FEE_BPS[Chain.BSC] == viability.ROUTER_BPS_PER_LEG + ep.FLAP_PROTOCOL_FEE_BPS
    assert DEFAULT_FEE_BPS[Chain.BSC] == viability.ROUTER_BPS_PER_LEG + viability.DEX_FEE_BPS_UPPER[Chain.BSC]
    assert PaperBroker(tmp_db)._dex_fee_bps(Chain.BSC, "") == 200


# ------------------------------------------------------------------------- the pass criterion

def test_the_arming_criterion_is_written_down_and_judges_the_uncensored_table():
    c = snipe.BSC_ARMING_CRITERION
    assert c["min_n"] >= 100 and c["min_distinct_utc_days"] >= 7
    assert "snipe_observations" in c["population"] and "fire=1" in c["population"]
    assert "peak_ratio" in c["not_evidence"] and "twins" in c["not_evidence"]
