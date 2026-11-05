"""The Pons tax-wait entry rule: refuse before the toll is gone, then prove the book is clean.

What is MEASURED here and what is not:

* The anti-sniper schedule 9900 / 618 / 19 / 0 bps at 0 / 1 / 2 / 3+ s is MEASURED three
  ways (``currentSnipeTaxBps`` per block; the toll recovered from 913k buys; and the
  sample in :data:`MEASURED_TOLL_SAMPLE` below, pulled by this line from chain logs).
  ``kaiba/ingest/robinhood.py::CurveState.snipe_tax_bps_at`` still models it as linear
  (9900 / 6600 / 3300) and :func:`test_schedule_is_the_measured_one_not_the_linear_model`
  is the alarm that the lane never quietly inherits that model.
* The exclusions (dev in the post-tax window, a tax-exempt outside buyer, a known bundler,
  the dead-launch veto) are the rules that replicated out of sample in
  ``docs/research/line4-antisniper-tax-pons.md`` and its REFUTATION.
* The strength weights and saturations are DERIVED or INVENTED, labelled as such in
  ``lanes.PONS_TAX_PROVENANCE``; :func:`test_every_parameter_carries_its_provenance`
  enforces the labels.

Every test below is a near-miss pair where it can be: the clean book fires, and the same
book with one thing wrong stays silent.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from kaiba.core.schemas import (
    Chain,
    EvidenceBasis,
    Lane,
    LaneMode,
    Measure,
    Receipt,
    Token,
    TokenDossier,
    now_ms,
)
from kaiba.execution import lanes
from kaiba.execution.lanes import LaneContext
from kaiba.execution.risk import score_fraction, score_from_strength

TOKEN = "0x234661cf2ac52bb5379fe977c5504952f60de6fa"
CURVE = "0x9e7e83f628a317e907435271be7f9b80723553e3"
CREATOR = "0xf3bb3e3e0688d40fdc37d31f1b0557e095f97789"
LAUNCH_TX = "0x42e6be9693b72b53d588d54e02171859ef6545297a19d1496a6bccd6ac546d50"
#: The helper contract that fronts the dev's atomic buy inside ``launchToken`` (MEASURED:
#: the ``trader`` on 6,536 of 6,536 launch-tx buys in the local tape).
LAUNCH_HELPER = "0xe33e9e479df8802cb0866d5d05258bec4cf62948"
ETH_USD = Decimal("2673.87")  # MEASURED off the local tape's usd_value / amount_native
WEI = 10**18

NOW = (now_ms() // 120_000) * 120_000


def _wallet(i: int) -> str:
    return "0x" + f"{i:040x}"


def buy(
    wallet: str,
    elapsed_s: float,
    *,
    eth: float = 0.02,
    tx: str | None = None,
    side: str = "buy",
    fee_bps: int | None = None,
    recipient: str | None = None,
    usd: str | None = None,
) -> dict[str, Any]:
    """One swaps-shaped row, ``elapsed_s`` after launch. ``fee_bps`` adds the ``fee`` word
    a live ``CurveBuy`` carries (protocol fee + snipe tax)."""
    wei = int(Decimal(str(eth)) * WEI)
    row: dict[str, Any] = {
        "wallet": wallet,
        "side": side,
        "elapsed_s": elapsed_s,
        "amount_native": str(wei),
        "usd_value": usd if usd is not None else str((Decimal(str(eth)) * ETH_USD).quantize(Decimal("0.01"))),
        "tx": tx or f"0xtx{wallet[-6:]}{int(elapsed_s * 1000)}",
    }
    if fee_bps is not None:
        row["fee"] = wei * fee_bps // 10_000
    if recipient is not None:
        row["recipient"] = recipient
    return row


def dev_atomic(eth: float = 0.05) -> dict[str, Any]:
    return buy(LAUNCH_HELPER, 0, eth=eth, tx=LAUNCH_TX, fee_bps=100)


def clean_book() -> list[dict[str, Any]]:
    """A launch with a real dev buy and four independent outside buyers after tax-zero."""
    return [
        dev_atomic(0.05),
        buy(_wallet(1), 12, eth=0.03),
        buy(_wallet(2), 20, eth=0.04),
        buy(_wallet(3), 30, eth=0.02),
        buy(_wallet(4), 45, eth=0.05),
    ]


def make_ctx(
    conn,
    *,
    age_s: float = 60,
    buys: list[dict[str, Any]] | None = None,
    creator: str | None = CREATOR,
    launch_tx: str | None = LAUNCH_TX,
    params: dict[str, Any] | None = None,
    curve: dict[str, Any] | None = None,
    liquidity: str | None = "12000",
    launchpad: str = "pons",
    created: bool = True,
    now: int = NOW,
) -> LaneContext:
    created_ms = now - int(age_s * 1000) if created else None
    meta: dict[str, Any] = {"curve": CURVE, "quote_is_native": True}
    if launch_tx:
        meta["signature"] = launch_tx
    token_meta = Token(
        address=TOKEN,
        chain=Chain.ROBINHOOD,
        creator=creator,
        created_ms=created_ms,
        launchpad=launchpad,
        pool=CURVE,
        meta=meta,
    )
    liq = (
        Measure(
            value=Decimal(liquidity),
            basis=EvidenceBasis.PROVIDER_REPORTED,
            receipt=Receipt(provider="fixture", endpoint="pons", observed_at_ms=now),
            freshness_budget_s=86_400,
        )
        if liquidity is not None
        else Measure.unknown()
    )
    dossier = TokenDossier(address=TOKEN, chain=Chain.ROBINHOOD, liquidity_usd=liq, built_at_ms=now)
    rows = []
    base = created_ms if created_ms is not None else now - int(age_s * 1000)
    for b in buys if buys is not None else clean_book():
        row = dict(b)
        row["ts_ms"] = base + int(row.pop("elapsed_s") * 1000)
        row["chain"] = "robinhood"
        row["token"] = TOKEN
        rows.append(row)
    return LaneContext(
        chain=Chain.ROBINHOOD,
        token=TOKEN,
        now_ms=now,
        conn=conn,
        dossier=dossier,
        recent_buys=rows,
        token_meta=token_meta,
        curve=curve,
        params=params or {},
    )


# ---------------------------------------------------------------- the schedule


#: MEASURED by this line from chain logs (RPC from the local machine, browser User-Agent):
#: 247 ``TokenLaunched`` in blocks 68,879,952-68,891,952, 1,210 ``CurveBuy`` within 60
#: blocks of their launch, exact block timestamps, toll = fee word / quoteIn - 100 bps.
#: ``(elapsed_s, toll_bps, buys)`` for buys outside the launch tx.
MEASURED_TOLL_SAMPLE: tuple[tuple[int, int, int], ...] = (
    (0, 0, 258),  # exempt: the deployer's allowlist landing in the launch second
    (0, 9400, 1),  # 9900 less the creator's pro-rata share; the wall works
    (1, 618, 226),
    (1, 0, 104),
    (2, 19, 120),
    (2, 0, 22),
    (3, 0, 73),
    (4, 0, 71),
    (5, 0, 98),
)


def test_schedule_matches_every_paid_toll_in_the_measured_sample():
    for elapsed, toll, _ in MEASURED_TOLL_SAMPLE:
        if toll == 0 and elapsed < lanes.PONS_SNIPE_TAX_SECONDS:
            continue  # zero inside the window is the exemption, not the schedule
        scheduled = lanes.pons_snipe_tax_bps(elapsed)
        assert scheduled >= toll, (elapsed, toll, scheduled)
        assert scheduled - toll <= 500, (elapsed, toll, scheduled)  # creator pro-rata only
    assert lanes.pons_snipe_tax_bps(0) == 9900
    assert lanes.pons_snipe_tax_bps(1) == 618
    assert lanes.pons_snipe_tax_bps(2) == 19
    assert lanes.pons_snipe_tax_bps(3) == 0
    assert lanes.pons_snipe_tax_bps(1800) == 0


def test_schedule_is_the_measured_one_not_the_linear_model():
    """The ingest module's linear decay would say 6600 at 1 s and 3300 at 2 s."""
    assert lanes.pons_snipe_tax_bps(1) != 6600
    assert lanes.pons_snipe_tax_bps(2) != 3300
    assert lanes.pons_snipe_tax_bps(1) < lanes.pons_snipe_tax_bps(0) // 10
    assert lanes.pons_snipe_tax_bps(2) < lanes.pons_snipe_tax_bps(1) // 10


def test_schedule_fails_closed_on_an_unmeasured_configuration():
    assert lanes.pons_snipe_tax_bps(-1) == 9900  # before launch: the full wall
    assert lanes.pons_snipe_tax_bps(2, start_bps=5000, seconds=10) == 5000
    assert lanes.pons_snipe_tax_bps(10, start_bps=5000, seconds=10) == 0
    assert lanes.pons_snipe_tax_bps(0, start_bps=0, seconds=0) == 0


def test_exemption_share_in_the_sample_is_why_the_wait_alone_is_not_enough():
    """The premise "snipers are priced out for 3 s" is refuted by the sample itself."""
    inside = [(e, t, n) for e, t, n in MEASURED_TOLL_SAMPLE if e < lanes.PONS_SNIPE_TAX_SECONDS]
    exempt = sum(n for _, t, n in inside if t == 0)
    total = sum(n for _, _, n in inside)
    assert exempt / total > 0.4, (exempt, total)


# ---------------------------------------------------------------- pre-decay refusals


@pytest.mark.parametrize("age_s,toll", [(0.5, 9900), (1.2, 618), (2.9, 19)])
def test_entry_before_the_toll_has_decayed_is_refused(tmp_db, age_s, toll):
    ctx = make_ctx(tmp_db, age_s=age_s, buys=[dev_atomic(), buy(_wallet(1), 0.2, fee_bps=100 + 618)])
    check = lanes.pons_entry_check(ctx)
    assert not check.ok
    assert f"pre_decay_tax_{toll}bps" in check.refusals
    assert check.tax_now_bps == toll
    assert lanes.pons_robinhood(ctx) is None


def test_entry_inside_the_post_tax_window_is_refused(tmp_db):
    fast = [dev_atomic(), *[buy(_wallet(i), 3.5 + i, eth=0.03) for i in range(1, 5)]]
    at_tax_zero = make_ctx(tmp_db, age_s=8, buys=fast)
    check = lanes.pons_entry_check(at_tax_zero)
    assert check.tax_now_bps == 0
    assert "post_tax_window_open" in check.refusals
    assert lanes.pons_robinhood(at_tax_zero) is None

    past_window = make_ctx(tmp_db, age_s=14, buys=fast)
    assert lanes.pons_entry_check(past_window).ok
    assert lanes.pons_robinhood(past_window) is not None

    wider = make_ctx(tmp_db, age_s=14, buys=fast, params={"post_tax_window_s": 30})
    assert "post_tax_window_open" in lanes.pons_entry_check(wider).refusals
    assert lanes.pons_robinhood(wider) is None


def test_a_curve_read_schedule_wins_over_the_factory_default(tmp_db):
    launched_at_s = (NOW - 20_000) // 1000
    curve = {"launched_at_s": launched_at_s, "snipe_tax_start_bps": 9900, "snipe_tax_seconds": 3}
    ctx = make_ctx(tmp_db, age_s=20, curve=curve)
    check = lanes.pons_entry_check(ctx)
    assert check.tax_basis == "curve_read"
    assert check.ok

    odd = {"launched_at_s": launched_at_s, "snipe_tax_start_bps": 5000, "snipe_tax_seconds": 60}
    ctx = make_ctx(tmp_db, age_s=20, curve=odd)
    check = lanes.pons_entry_check(ctx)
    assert check.tax_now_bps == 5000  # unmeasured shape: the full toll until the window closes
    assert "pre_decay_tax_5000bps" in check.refusals
    assert lanes.pons_robinhood(ctx) is None


def test_unknown_launch_time_is_none_not_zero(tmp_db):
    ctx = make_ctx(tmp_db, created=False)
    assert lanes.pons_entry_check(ctx).refusals == ["launch_time_unknown"]
    assert lanes.pons_robinhood(ctx) is None


# ---------------------------------------------------------------- the clean-book check


def test_clean_book_fires_and_counts_only_post_tax_buyers(tmp_db):
    book = [
        dev_atomic(0.05),
        buy(_wallet(9), 1, eth=0.05, fee_bps=100 + 618),  # paid the toll: real, but pre-tax-zero
        *clean_book()[1:],
    ]
    ctx = make_ctx(tmp_db, age_s=60, buys=book)
    check = lanes.pons_entry_check(ctx)
    assert check.ok, check.refusals
    assert check.unverified == []
    assert check.exempt_buys == 0
    assert check.dev_atomic_wei == int(Decimal("0.05") * WEI)
    assert set(check.post_tax_buyers) == {_wallet(i) for i in range(1, 5)}
    assert _wallet(9) in check.early_buyers  # early demand still counts toward the graduation term

    signal = lanes.pons_robinhood(ctx)
    assert signal is not None
    assert signal.lane is Lane.PONS_ROBINHOOD
    assert signal.payload["entity_count"] == 4
    assert _wallet(9) not in signal.wallets
    assert CREATOR not in signal.wallets
    assert signal.payload["book"]["dev_buys_post_tax"] == 0
    assert signal.payload["tax"]["now_bps"] == 0
    assert signal.payload["unverified"] == []


def test_dev_buy_after_tax_zero_is_refused(tmp_db):
    book = clean_book() + [buy(CREATOR, 25, eth=0.1)]
    ctx = make_ctx(tmp_db, buys=book)
    check = lanes.pons_entry_check(ctx)
    assert "dev_bought_after_tax_zero" in check.refusals
    assert check.dev_buys_post_tax == 1
    assert lanes.pons_robinhood(ctx) is None

    via_recipient = clean_book() + [buy(_wallet(7), 25, eth=0.1, recipient=CREATOR)]
    ctx = make_ctx(tmp_db, buys=via_recipient)
    assert "dev_bought_after_tax_zero" in lanes.pons_entry_check(ctx).refusals

    before_tax_zero = clean_book() + [buy(CREATOR, 1, eth=0.1, fee_bps=100)]
    ctx = make_ctx(tmp_db, buys=before_tax_zero)
    check = lanes.pons_entry_check(ctx)
    assert check.dev_buys_post_tax == 0
    assert check.ok, check.refusals  # the dev's own exempt buy inside the window is not the book


def test_tax_exempt_outside_buyer_in_the_launch_second_is_refused(tmp_db):
    bundled = clean_book() + [buy(_wallet(8), 0, eth=0.01)]  # a different tx, same second, no toll
    ctx = make_ctx(tmp_db, buys=bundled)
    check = lanes.pons_entry_check(ctx)
    assert "tax_exempt_buyer_in_window" in check.refusals
    assert check.exempt_buys == 1
    assert check.exempt_basis == "elapsed0_proxy"
    assert lanes.pons_robinhood(ctx) is None


def test_fee_word_makes_the_exemption_check_exact(tmp_db):
    exempt_at_1s = clean_book() + [buy(_wallet(8), 1, eth=0.01, fee_bps=100)]
    check = lanes.pons_entry_check(make_ctx(tmp_db, buys=exempt_at_1s))
    assert check.exempt_basis == "fee_word"
    assert check.exempt_buys == 1
    assert "tax_exempt_buyer_in_window" in check.refusals

    paid_at_1s = clean_book() + [buy(_wallet(8), 1, eth=0.01, fee_bps=100 + 618)]
    check = lanes.pons_entry_check(make_ctx(tmp_db, buys=paid_at_1s))
    assert check.exempt_buys == 0
    assert check.ok, check.refusals

    paid_at_2s = clean_book() + [buy(_wallet(8), 2, eth=0.01, fee_bps=100 + 19)]
    assert lanes.pons_entry_check(make_ctx(tmp_db, buys=paid_at_2s)).exempt_buys == 0


def test_known_bundler_among_post_tax_buyers_is_refused(tmp_db):
    tmp_db.execute(
        "INSERT INTO token_bundle_members (chain, token, address, role, entity_id, atoms, lamports, "
        "first_slot, first_index, buys) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("robinhood", TOKEN, _wallet(3), "bundler", None, "1", None, None, None, 1),
    )
    ctx = make_ctx(tmp_db)
    check = lanes.pons_entry_check(ctx)
    assert check.known_bundlers == [_wallet(3)]
    assert "known_bundler_in_window" in check.refusals
    assert lanes.pons_robinhood(ctx) is None


def test_bundler_tag_on_the_wallet_row_also_refuses(tmp_db):
    tmp_db.execute(
        "INSERT INTO wallets (chain, address, name, source, tags_json, first_seen_ms, last_seen_ms, "
        "cohort, meta_json) VALUES (?,?,?,?,?,?,?,?,'{}')",
        ("robinhood", _wallet(2), None, "fixture", '["bundler"]', NOW - 1000, NOW, None),
    )
    check = lanes.pons_entry_check(make_ctx(tmp_db))
    assert check.known_bundlers == [_wallet(2)]
    assert not check.ok


def test_dead_launch_veto(tmp_db):
    dead = [dev_atomic(0.01), *clean_book()[1:]]  # tiny dev buy, first outside buy at 12 s
    check = lanes.pons_entry_check(make_ctx(tmp_db, buys=dead))
    assert "veto_dead_launch" in check.refusals
    assert lanes.pons_robinhood(make_ctx(tmp_db, buys=dead)) is None

    funded_dev = [dev_atomic(0.03), *clean_book()[1:]]
    assert lanes.pons_entry_check(make_ctx(tmp_db, buys=funded_dev)).ok

    early_demand = [dev_atomic(0.01), buy(_wallet(9), 2, eth=0.01, fee_bps=100 + 19), *clean_book()[1:]]
    check = lanes.pons_entry_check(make_ctx(tmp_db, buys=early_demand))
    assert check.outside_demand_early_wei == int(Decimal("0.01") * WEI)
    assert check.ok, check.refusals


def test_missing_launch_tx_leaves_the_veto_unverified_rather_than_safe(tmp_db):
    no_launch_row = clean_book()[1:]  # the launch tx was never ingested
    check = lanes.pons_entry_check(make_ctx(tmp_db, buys=no_launch_row))
    assert check.dev_atomic_wei is None
    assert "dev_atomic_buy_unavailable" in check.unverified
    assert "veto_dead_launch" not in check.refusals
    signal = lanes.pons_robinhood(make_ctx(tmp_db, buys=no_launch_row))
    assert signal is not None
    assert signal.strength <= lanes.PONS_TAX_DEFAULTS["unverified_strength_cap"]
    assert score_fraction(score_from_strength(signal.strength)) == Decimal(0)


# ---------------------------------------------------------------- strength


def crowded_book(n: int, *, first_s: float = 4, step_s: float = 2) -> list[dict[str, Any]]:
    return [dev_atomic(0.05)] + [
        buy(_wallet(100 + i), first_s + i * step_s, eth=0.03) for i in range(n)
    ]


def test_strength_is_capped_below_the_ladder_top(tmp_db):
    ctx = make_ctx(tmp_db, age_s=120, buys=crowded_book(30))
    signal = lanes.pons_robinhood(ctx)
    assert signal is not None
    assert signal.payload["strength_raw"] > lanes.PONS_TAX_DEFAULTS["max_strength"]
    assert signal.strength == lanes.PONS_TAX_DEFAULTS["max_strength"]
    score = score_from_strength(signal.strength)
    assert score < 80
    assert score_fraction(score) == Decimal("0.25")


def test_strength_rises_with_early_buyers_and_entities(tmp_db):
    thin = lanes.pons_robinhood(make_ctx(tmp_db, age_s=120, buys=crowded_book(4)))
    busy = lanes.pons_robinhood(make_ctx(tmp_db, age_s=120, buys=crowded_book(12)))
    assert thin is not None and busy is not None
    assert busy.strength > thin.strength
    assert busy.payload["strength_components"]["early_buyers"] > thin.payload["strength_components"]["early_buyers"]
    assert busy.payload["book"]["early_buyers"] == 12
    assert thin.payload["book"]["early_buyers"] == 4


def test_buyers_after_the_early_window_do_not_count_as_early(tmp_db):
    late = [dev_atomic(0.05)] + [buy(_wallet(200 + i), 400 + i * 5, eth=0.03) for i in range(8)]
    signal = lanes.pons_robinhood(make_ctx(tmp_db, age_s=600, buys=late))
    assert signal is not None
    assert signal.payload["book"]["early_buyers"] == 0
    assert signal.payload["strength_components"]["early_buyers"] == 0
    assert signal.payload["entity_count"] == 8


def test_unknown_creator_caps_strength_below_the_ladder(tmp_db):
    signal = lanes.pons_robinhood(make_ctx(tmp_db, age_s=120, buys=crowded_book(30), creator=None))
    assert signal is not None
    assert "creator_unknown" in signal.payload["unverified"]
    assert signal.strength <= lanes.PONS_TAX_DEFAULTS["unverified_strength_cap"]
    assert score_fraction(score_from_strength(signal.strength)) == Decimal(0)
    assert any(r.startswith("UNVERIFIED: creator_unknown") for r in signal.reasons)


def test_strength_scale_can_no_longer_read_as_max_size():
    for strength in (0.77, 0.9, 0.998):  # what the previous lane emitted
        assert score_fraction(score_from_strength(min(strength, lanes.PONS_TAX_DEFAULTS["max_strength"]))) <= Decimal("0.25")


# ---------------------------------------------------------------- the existing gates still hold


def test_entity_floor_liquidity_and_age_gates_still_apply(tmp_db):
    assert lanes.pons_robinhood(make_ctx(tmp_db, params={"min_entities": 5})) is None
    assert lanes.pons_robinhood(make_ctx(tmp_db, liquidity=None)) is None
    assert lanes.pons_robinhood(make_ctx(tmp_db, params={"min_liquidity_usd": 20_000})) is None
    assert lanes.pons_robinhood(make_ctx(tmp_db, age_s=2000)) is None
    assert lanes.pons_robinhood(make_ctx(tmp_db, launchpad="clanker")) is None


def test_dust_buyers_do_not_count_toward_the_post_tax_book(tmp_db):
    dust = [dev_atomic(0.05)] + [buy(_wallet(300 + i), 10 + i, eth=0.0001) for i in range(6)]
    check = lanes.pons_entry_check(make_ctx(tmp_db, buys=dust))
    assert check.post_tax_buyers == []
    assert lanes.pons_robinhood(make_ctx(tmp_db, buys=dust)) is None


# ---------------------------------------------------------------- provenance and posture


def test_every_parameter_carries_its_provenance():
    words = ("MEASURED", "DERIVED", "INVENTED")
    assert set(lanes.PONS_TAX_PROVENANCE) == set(lanes.PONS_TAX_DEFAULTS)
    for name, note in lanes.PONS_TAX_PROVENANCE.items():
        assert note.startswith(words), name
        if note.startswith("INVENTED"):
            assert "settled" in note.lower(), f"{name} is INVENTED but never says what would settle it"


def test_lane_mode_is_untouched():
    from kaiba.core.config import get_risk

    try:
        mode = get_risk().lane(Lane.PONS_ROBINHOOD).mode
    except Exception as exc:  # noqa: BLE001 - a malformed shipped file is another test's job
        pytest.skip(f"risk.yaml unreadable here: {exc}")
    assert mode is LaneMode.SHADOW


def test_signal_says_the_lane_is_unproven(tmp_db):
    signal = lanes.pons_robinhood(make_ctx(tmp_db))
    assert signal is not None
    assert "MEASURED negative" in signal.payload["evidence"]
    assert signal.payload["tax"]["schedule_bps"] == {0: 9900, 1: 618, 2: 19}


# ---------------------------------------------------------------- provider rows


def test_decimal_eth_amounts_from_provider_rows_do_not_crash_or_vanish(tmp_db):
    """MEASURED on the live box: robinhood rows with amount_native '0.07128' (ETH, not wei)."""
    assert lanes._pons_native_wei("0.07128") == 71_280_000_000_000_000
    assert lanes._pons_native_wei("71280000000000000") == 71_280_000_000_000_000
    assert lanes._pons_native_wei(None) is None
    assert lanes._pons_native_wei("") is None
    book = clean_book() + [{**buy(_wallet(5), 50), "amount_native": "0.07128"}]
    ctx = make_ctx(tmp_db, buys=book)
    check = lanes.pons_entry_check(ctx)
    assert _wallet(5) in check.post_tax_buyers  # counted as 0.07128 ETH, not as dust
    signal = lanes.pons_robinhood(ctx)
    assert signal is not None
    assert _wallet(5) in signal.wallets
    assert signal.payload["entity_count"] == 5
