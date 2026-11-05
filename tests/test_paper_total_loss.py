"""A counted robinhood / bsc paper position whose token takes the whole sale CLOSES, at zero, and stays
counted (2026-10-23, step 3 follow-up, blocking).

Reproduced first from the verifier's probe (``v3b_probe_honeypot.py``): a bsc position whose current
dossier says sell tax 10,000 bps (GMGN reports a honeypot as ``sell_tax`` 1; dyor scales it) was
charged router 100 + 10,000 = 10,100 bps; ``paper_costs.exit_price`` went NEGATIVE (-0.006 on a $0.60
mark), ``PaperBroker.sell`` refused it ``no_price``, and the position never closed -- every tick, for
ever: its ~-100% never booked, its shadow-cap slot held. The same for an entry-recorded sell tax of
9,900+ and a robinhood measured sell tax up to 9,999.

And the path that DID close such rows dropped them from every scoreboard: a counted paper position no
source can price was written off ``abandoned_unpriceable`` -- a family ``kaiba.learning.fabricated``
excludes as a booked write-off -- although a drained pool is exactly the token no mark source prices.
The second follow-up (verifier probe ``v3c_probe_dust_pool.py``) found the same exclusion whenever
the pool PAID anything -- a rugged pair's dust included -- or could not be read: see the last section.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.db import fetch_all, fetch_one, jload
from kaiba.core.schemas import Chain, EvidenceBasis, LaneMode, Measure, Receipt, TokenRisk, now_ms
from kaiba.execution import evm_pool_price as m
from kaiba.execution import paper_costs as pc
from kaiba.execution import watchdog as wd
from kaiba.execution.paper import load_position
from kaiba.learning import fabricated
from kaiba.learning import paper_report as PR
from tests.test_paper import write_dossier
from tests.test_paper_entry_quote_evm import LANDED
from tests.test_paper_exit_evm import (  # noqa: F401 - fixtures
    BSC_TOKEN,
    _clean,
    bsc_position,
    last_sell_basis,
    mark,
    sell,
)
from tests.test_paper_exit_evm import QTY as QTY_BX
from tests.test_watchdog import make_position, risk_file  # noqa: F401


def _dossier(conn, token: str, chain: Chain, *, sell_tax: int | None = None, can_sell: bool | None = None,
             honeypot_blocker: bool = False):
    dossier = write_dossier(conn, token=token, chain=chain)
    update: dict = {}
    if sell_tax is not None:
        update["sell_tax_bps"] = Measure(value=Decimal(sell_tax), basis=EvidenceBasis.PROVIDER_REPORTED,
                                         receipt=Receipt(provider="gmgn", endpoint="token.security"),
                                         freshness_budget_s=86_400)
    if can_sell is not None:
        update["can_sell"] = can_sell
    if honeypot_blocker:
        update["blockers"] = [TokenRisk.HONEYPOT]
    dossier = dossier.model_copy(update=update)
    conn.execute("UPDATE token_dossiers SET dossier_json=? WHERE address=?", (dossier.model_dump_json(), token))
    conn.commit()
    return dossier


def _bsc_counted(conn, *, entry_sell_tax: int = 200) -> None:
    pc.record_entry_basis(conn, "pos_bx", Chain.BSC, source="pool:pancake-v2:v2_formula", fill="v2_formula",
                          buy_tax_bps=0, sell_tax_bps=entry_sell_tax, tax_in_price=False, landing=LANDED)


def _row(conn, pid: str = "pos_bx") -> dict:
    return fetch_one(conn, "SELECT closed_ms, cost_native, proceeds_native, realized_native, exit_reason "
                           "FROM positions WHERE position_id=?", (pid,))


def _closed_at_zero(row: dict) -> bool:
    return (row["closed_ms"] is not None and int(row["proceeds_native"]) == 0
            and int(row["realized_native"]) == -int(row["cost_native"]))


# ----------------------------------------------------------------------------- the probe, as a test


def test_a_honeypot_sell_tax_closes_the_position_at_zero_and_keeps_it_counted(tmp_db, monkeypatch, risk_file):  # noqa: F811
    """The verifier's probe: entry recorded 200 bps, the current dossier says 10,000 (a honeypot).
    Before: ``no_price``, ``closed_ms`` None, forever. Mutant pinned: removing the total-loss cap (the
    check in ``_paper_total_loss`` AND the guard in ``_paper``) must fail here."""
    position, rpc = bsc_position(tmp_db, monkeypatch)
    _bsc_counted(tmp_db, entry_sell_tax=200)
    _dossier(tmp_db, BSC_TOKEN, Chain.BSC, sell_tax=10_000)
    assert pc.exit_price(Chain.BSC, Decimal("0.6"), tax_bps=10_000) < 0, "the arithmetic the probe found"
    out = sell(tmp_db, position, mark("0.6"))
    assert out.ok and out.detail != "no_price", out
    assert _closed_at_zero(_row(tmp_db)), _row(tmp_db)
    assert rpc.sends == [], "no pool answer changes what a honeypot sale receives"
    tag = pc.read_basis(tmp_db, "pos_bx")
    leg = tag["exits"][-1]
    assert leg["fill"] == pc.TOTAL_LOSS and leg["filled"] and leg["tax_bps"] == 10_000 and leg["honest"]
    # the loss is real and exact: the row counts, honestly, and is NOT an exit-stage (bound) row
    assert pc.entry_counts(tag) and pc.is_honest(tag) and not pc.exit_degraded(tag)
    assert PR.paper_filter(tmp_db, now_ms()).reason("pos_bx", "bsc", now_ms()) is None
    assert not fabricated.is_fabricated_outcome(_row(tmp_db)["exit_reason"], "shadow")


@pytest.mark.parametrize("entry_tax,dossier_tax", [(9_900, None), (9_900, 0), (0, 9_900), (9_950, 9_999)])
def test_any_sell_tax_that_with_the_router_reaches_100_percent_closes_at_zero(
        tmp_db, monkeypatch, risk_file, entry_tax, dossier_tax):  # noqa: F811
    """9,900 + the router's 100 = 10,000: exit_price exactly 0, which the broker also refuses."""
    position, rpc = bsc_position(tmp_db, monkeypatch)
    _bsc_counted(tmp_db, entry_sell_tax=entry_tax)
    if dossier_tax is not None:
        _dossier(tmp_db, BSC_TOKEN, Chain.BSC, sell_tax=dossier_tax)
    out = sell(tmp_db, position, mark("0.6"))
    assert out.ok, out
    assert _closed_at_zero(_row(tmp_db))
    assert pc.read_basis(tmp_db, "pos_bx")["exits"][-1]["fill"] == pc.TOTAL_LOSS


def test_a_sell_tax_just_under_the_line_is_still_filled_from_the_pool(tmp_db, monkeypatch, risk_file):  # noqa: F811
    """The boundary: 9,899 + 100 = 9,999 bps is a 0.01% sale, not a total loss -- the pool fills it."""
    position, rpc = bsc_position(tmp_db, monkeypatch)
    _bsc_counted(tmp_db, entry_sell_tax=9_899)
    out = sell(tmp_db, position, mark("0.6"))
    assert out.ok
    # the broker was handed the real (tiny, positive) price, not the zero-proceeds route
    assert Decimal(last_sell_basis(tmp_db)["quote_price_usd"]) == pc.exit_price(Chain.BSC, Decimal("0.6"), tax_bps=9_899)
    assert pc.exit_price(Chain.BSC, Decimal("0.6"), tax_bps=9_899) > 0
    assert last_sell_basis(tmp_db)["impact_basis"] == "executable_quote"
    assert pc.read_basis(tmp_db, "pos_bx")["exits"][-1]["fill"] == "v2_formula" and len(rpc.sends) == 1


@pytest.mark.parametrize("how", ["can_sell", "blocker"])
def test_a_token_the_dossier_now_says_cannot_be_sold_closes_at_zero(tmp_db, monkeypatch, risk_file, how):  # noqa: F811
    """A transfer switched off after the entry: no quoter here runs the token's transfer (QuoterV2 /
    V4Quoter revert before it is paid in, the v2 formula is reserve arithmetic), so the pool would
    happily price a sale that a live wallet cannot make."""
    position, rpc = bsc_position(tmp_db, monkeypatch)
    _bsc_counted(tmp_db, entry_sell_tax=0)
    _dossier(tmp_db, BSC_TOKEN, Chain.BSC, sell_tax=0, can_sell=False if how == "can_sell" else None,
             honeypot_blocker=how == "blocker")
    assert sell(tmp_db, position, mark("0.6")).ok
    assert _closed_at_zero(_row(tmp_db)) and rpc.sends == []
    leg = pc.read_basis(tmp_db, "pos_bx")["exits"][-1]
    assert leg["fill"] == pc.TOTAL_LOSS and "cannot be sold" in leg["note"]


RH_TOKEN = "0x" + "9e" * 20
QTY = 1000 * 10**18


def _rh_measured(conn, *, sell_tax: int) -> object:
    make_position(conn, position_id="pos_rh", token=RH_TOKEN, chain=Chain.ROBINHOOD, qty=QTY, entry="0.0002")
    pc.record_entry_basis(conn, "pos_rh", Chain.ROBINHOOD, source="pool:uniswap-v4:v4_quoter", fill="v4_quoter",
                          buy_tax_bps=0, sell_tax_bps=sell_tax, tax_in_price=False,
                          transfer_tax=pc.TRANSFER_TAX_MEASURED, landing=LANDED)
    conn.commit()
    return load_position(conn, "pos_rh")


def test_a_robinhood_measured_sell_tax_of_9999_closes_at_zero_without_a_pool_read(tmp_db, monkeypatch, risk_file):  # noqa: F811
    def no_pool(*a, **k):
        raise AssertionError("a total loss must not wait on a pool read")

    monkeypatch.setattr(m, "exit_fill", no_pool)
    position = _rh_measured(tmp_db, sell_tax=9_999)
    quote = mark("0.0002", source="onchain-sample:v4", chain=Chain.ROBINHOOD, token=RH_TOKEN, liquidity=None)
    assert sell(tmp_db, position, quote).ok
    assert _closed_at_zero(_row(tmp_db, "pos_rh"))
    assert pc.read_basis(tmp_db, "pos_rh")["exits"][-1]["fill"] == pc.TOTAL_LOSS


def test_the_venue_charge_that_tips_a_leg_over_100_percent_also_books_zero(tmp_db, monkeypatch, risk_file):  # noqa: F811
    """9,700 measured + router 100 is a 2% sale on a pool fill, but at the MARK (the pool unknown to
    this module) the 250 bps stand-in is added too: 10,050 bps. Caught by the general guard in
    ``_paper``, not the pre-read check -- the price the broker would get is negative either way."""
    monkeypatch.setattr(m, "exit_fill", lambda *a, **k: (None, "pool_unknown"))
    position = _rh_measured(tmp_db, sell_tax=9_700)
    quote = mark("0.0002", source="prices:dexscreener", chain=Chain.ROBINHOOD, token=RH_TOKEN, liquidity=None)
    assert pc.exit_price(Chain.ROBINHOOD, Decimal("0.0002"), tax_bps=9_700) < 0
    out = sell(tmp_db, position, quote)
    assert out.ok, out
    assert _closed_at_zero(_row(tmp_db, "pos_rh"))
    leg = pc.read_basis(tmp_db, "pos_rh")["exits"][-1]
    assert leg["fill"] == pc.TOTAL_LOSS and "was: mark" in leg["note"]


def test_the_total_loss_rule_itself():
    assert pc.exit_is_total_loss(Chain.BSC, 9_900) and pc.exit_is_total_loss(Chain.BSC, 10_000)
    assert not pc.exit_is_total_loss(Chain.BSC, 9_899)
    assert pc.exit_is_total_loss(Chain.ROBINHOOD, 9_650)  # + router 100 + flat venue 250
    assert not pc.exit_is_total_loss(Chain.ROBINHOOD, 9_650, venue_fee_in_price=True)
    assert not pc.exit_is_total_loss(Chain.SOL, 0)
    assert pc.TOTAL_LOSS in pc.HONEST_EXIT_FILLS and pc.NO_FILL_BOUND not in pc.HONEST_EXIT_FILLS


# ----------------------------------------------------------------------------- no source can price it
#
# 2026-10-23 (step 3 follow-up, blocking; verifier probe ``v3c_probe_dust_pool.py``). A COUNTED bsc /
# robinhood paper position no mark source prices went, on ONE blind tick once it was an hour old, to
# ``abandoned_unpriceable`` -- excluded from every scoreboard (``fabricated.FABRICATED_EXIT_FAMILIES``)
# -- whenever its pool PAID anything or could not be read. A rugged v2 pair keeps MINIMUM_LIQUIDITY
# dust in both reserves, so it always pays a few wei: the worst losers left the record. Now the
# position must be BLIND for an hour, then its pool is asked once: it pays -> sold at that fill
# (counted); it cannot pay / the token takes the sale -> zero (counted); nothing read -> asked again,
# bounded, then abandoned.

HOUR_S = 3_600


def _blind_for(conn, seconds: float | None, pid: str = "pos_bx") -> None:
    """Set the persisted blind clock (``watchdog_state.blind_since_ms``) ``seconds`` ago."""
    position = load_position(conn, pid)
    state = wd.load_state(conn, position)
    state.blind_since_ms = None if seconds is None else now_ms() - int(seconds * 1000)
    wd.save_state(conn, state, mirror=False)
    conn.commit()


def _old_unpriceable(conn, monkeypatch, *, reserves=(0, 0), tag: str | None = "pool:pancake-v2:v2_formula",
                     blind_s: float | None = 2 * HOUR_S, cost: int | None = None):
    position, rpc = bsc_position(conn, monkeypatch, reserves=reserves)
    conn.execute("UPDATE positions SET opened_ms=? WHERE position_id='pos_bx'", (now_ms() - 3 * HOUR_S * 1000,))
    if cost is not None:
        conn.execute("UPDATE positions SET cost_native=? WHERE position_id='pos_bx'", (str(cost),))
    if tag is not None:
        pc.record_entry_basis(conn, "pos_bx", Chain.BSC, source=tag, fill=None, buy_tax_bps=0, sell_tax_bps=0,
                              tax_in_price=False, landing=LANDED)
    conn.commit()
    if blind_s is not None:
        _blind_for(conn, blind_s)
    return rpc


def _events(conn, event: str) -> list[dict]:
    rows = fetch_all(conn, "SELECT payload FROM events WHERE kind='system' ORDER BY id")
    return [p for p in (jload(r["payload"], {}) for r in rows) if p.get("event") == event]


def _scoreboard_returns(conn) -> list[float | None]:
    rows = PR.paper_rows(conn, now_ms(), flt=PR.paper_filter(conn, now_ms()))
    assert all(PR.outcome_exclusion(r[3], r[4]) is None for r in rows), rows
    return [r[3] for r in rows]


def test_a_dust_pool_that_still_pays_a_few_wei_is_sold_at_that_fill_and_stays_counted(
        tmp_db, monkeypatch, risk_file):  # noqa: F811
    """The verifier's probe, as a test. Reserves (1e18 tokens, 1,000 wei WBNB): a fully rugged v2 pair
    (MINIMUM_LIQUIDITY keeps both nonzero). The pool pays 998 wei for 1,000 tokens against a
    5,000,000-wei cost (-99.98%). Before: ``abandoned_unpriceable``, off the scoreboard. Now: sold at
    that fill, an honest pool leg, counted at ~-100%. Mutant pinned: abandoning when the read returns
    a fill (``paper_blind_exit`` -> BLIND_ABANDON on a fill) must fail here."""
    reserves = (10**18, 10**3)
    rpc = _old_unpriceable(tmp_db, monkeypatch, reserves=reserves)
    pays = m.v2_amount_out(QTY_BX, reserves[0], reserves[1], 25)
    cost = int(load_position(tmp_db, "pos_bx").cost_native)
    assert 0 < pays < cost // 5000, (pays, cost)  # the probe's arithmetic: dust, but a positive fill
    wd.Watchdog(tmp_db, price_source=wd.NullPriceSource()).tick()
    row = _row(tmp_db)
    assert row["closed_ms"] is not None, row
    assert row["exit_reason"] == wd.Watchdog.PAPER_BLIND_EXIT_REASON, row
    assert not fabricated.is_fabricated_outcome(row["exit_reason"], "shadow")
    assert 0 <= int(row["proceeds_native"]) <= pays, "never more than the pool pays"
    assert int(row["realized_native"]) / int(row["cost_native"]) <= -0.999
    assert len(rpc.sends) == 1, "ONE pool read, and the sell used it -- no second read"
    tag = pc.read_basis(tmp_db, "pos_bx")
    leg = tag["exits"][-1]
    assert leg["fill"] == "v2_formula" and leg["filled"] and leg["honest"], leg
    assert "no mark source had priced it for an hour" in leg["note"]
    assert pc.entry_counts(tag) and pc.is_honest(tag) and not pc.exit_degraded(tag)
    assert _scoreboard_returns(tmp_db) == [pytest.approx(-1.0, abs=1e-3)]
    assert _events(tmp_db, "shadow_position_blind_exit") and not _events(tmp_db, "shadow_position_abandoned")


def test_a_pool_that_pays_well_is_sold_at_its_fill_as_an_honest_counted_pool_leg(tmp_db, monkeypatch, risk_file):  # noqa: F811
    """1,000 tokens into a 1,000,000-token / 1,000-WBNB pair, cost 1 BNB (the $0.60 entry): the leg is
    booked at what the v2 formula pays, less the router and the broker's floor -- a small loss, counted,
    not written off and not invented."""
    rpc = _old_unpriceable(tmp_db, monkeypatch, reserves=(10**24, 10**21), cost=10**18)
    wd.Watchdog(tmp_db, price_source=wd.NullPriceSource()).tick()
    row = _row(tmp_db)
    assert row["exit_reason"] == wd.Watchdog.PAPER_BLIND_EXIT_REASON and len(rpc.sends) == 1, row
    pays = m.v2_amount_out(QTY_BX, 10**24, 10**21, 25)
    assert 0 < int(row["proceeds_native"]) < pays, "the router and the floor are charged on the fill"
    basis = last_sell_basis(tmp_db)
    executable = Decimal(pays) * Decimal(600) / Decimal(10**18) / Decimal(1000)
    assert Decimal(basis["executable_price_usd"]) == pc.exit_price(Chain.BSC, executable, tax_bps=0)
    assert basis["impact_basis"] == "executable_quote"
    ret = _scoreboard_returns(tmp_db)
    assert len(ret) == 1 and -0.05 < ret[0] < 0, ret
    assert pc.is_honest(pc.read_basis(tmp_db, "pos_bx"))


def test_an_unreadable_pool_keeps_the_position_open_and_is_asked_again_bounded_then_abandoned(
        tmp_db, monkeypatch, risk_file):  # noqa: F811
    """Every read learns nothing (``pool_read_failed``: the pacer / transport case). Before: written off
    at once. Now: open after each of the first PAPER_POOL_RETRIES + 1 reads but the last, abandoned on
    the last, and nothing more is read. Mutants pinned: dropping the retry bound (keep open for ever)
    and abandoning on the first unread both fail here."""
    monkeypatch.setattr(wd, "PAPER_BLIND_RETRY_GAP_MS", 0)
    rpc = _old_unpriceable(tmp_db, monkeypatch, reserves=(10**24, 10**21))
    rpc.table.clear()
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    for read in range(1, wd.PAPER_POOL_RETRIES + 1):
        dog.tick()
        assert _row(tmp_db)["closed_ms"] is None, f"closed after unread read {read}"
        assert len(rpc.sends) == read
    dog.tick()
    row = _row(tmp_db)
    assert row["closed_ms"] is not None and row["exit_reason"] == "abandoned_unpriceable", row
    assert len(rpc.sends) == wd.PAPER_POOL_RETRIES + 1
    abandoned = _events(tmp_db, "shadow_position_abandoned")
    assert len(abandoned) == 1 and f"pool unread after {wd.PAPER_POOL_RETRIES + 1} reads" in abandoned[0]["evidence"]
    assert len(_events(tmp_db, "shadow_position_blind_exit_retry")) == wd.PAPER_POOL_RETRIES
    dog.tick()
    assert len(rpc.sends) == wd.PAPER_POOL_RETRIES + 1, "a closed position is never read again"


def test_a_blind_position_is_not_read_every_tick(tmp_db, monkeypatch, risk_file):  # noqa: F811
    """The protection load stays bounded: after an unread read the pool is not asked again until
    PAPER_BLIND_RETRY_GAP_MS has passed, however many ticks run. Mutant pinned: the gap check removed."""
    rpc = _old_unpriceable(tmp_db, monkeypatch, reserves=(10**24, 10**21))
    rpc.table.clear()
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    for _ in range(4):
        dog.tick()
    assert len(rpc.sends) == 1 and _row(tmp_db)["closed_ms"] is None
    episode, last, reads = dog._blind_exit_reads["pos_bx"]
    dog._blind_exit_reads["pos_bx"] = (episode, last - wd.PAPER_BLIND_RETRY_GAP_MS, reads)  # the gap passed
    dog.tick()
    assert len(rpc.sends) == 2 and _row(tmp_db)["closed_ms"] is None


def test_a_read_that_recovers_on_a_retry_sells_at_the_fill(tmp_db, monkeypatch, risk_file):  # noqa: F811
    monkeypatch.setattr(wd, "PAPER_BLIND_RETRY_GAP_MS", 0)
    rpc = _old_unpriceable(tmp_db, monkeypatch, reserves=(10**24, 10**21), cost=10**18)
    table = dict(rpc.table)
    rpc.table.clear()
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    dog.tick()
    assert _row(tmp_db)["closed_ms"] is None
    rpc.table.update(table)
    dog.tick()
    row = _row(tmp_db)
    assert row["exit_reason"] == wd.Watchdog.PAPER_BLIND_EXIT_REASON and int(row["proceeds_native"]) > 0, row
    assert len(rpc.sends) == 2 and "pos_bx" not in dog._blind_exit_reads


def test_a_fill_the_broker_cannot_book_is_asked_again_not_abandoned(tmp_db, monkeypatch, risk_file):  # noqa: F811
    monkeypatch.setattr(wd.DefaultExitSubmitter, "paper_sell_at_fill",
                        lambda self, position, fill, reason: wd.ExitOutcome(False, None, None, "broker said no"))
    rpc = _old_unpriceable(tmp_db, monkeypatch, reserves=(10**24, 10**21))
    wd.Watchdog(tmp_db, price_source=wd.NullPriceSource()).tick()
    assert _row(tmp_db)["closed_ms"] is None and len(rpc.sends) == 1
    assert "broker said no" in _events(tmp_db, "shadow_position_blind_exit_retry")[0]["note"]


def test_an_old_position_blind_for_less_than_an_hour_stays_open_and_reads_nothing(tmp_db, monkeypatch, risk_file):  # noqa: F811
    """Opened three hours ago, but the abandon trigger is the BLIND clock: one missed quote on an old
    position must not end it (it used to: one blind tick at age >= 1 h). Mutant pinned: the blind-clock
    check removed (age alone) must fail here."""
    rpc = _old_unpriceable(tmp_db, monkeypatch, reserves=(10**24, 10**21), blind_s=None, cost=10**18)
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    dog.tick()  # first blind tick: the clock starts now
    assert _row(tmp_db)["closed_ms"] is None and rpc.sends == []
    assert wd.load_state(tmp_db, load_position(tmp_db, "pos_bx")).blind_since_ms is not None, "the clock persisted"
    _blind_for(tmp_db, HOUR_S - 60)
    dog.tick()
    assert _row(tmp_db)["closed_ms"] is None and rpc.sends == []
    _blind_for(tmp_db, HOUR_S + 60)
    dog.tick()
    assert _row(tmp_db)["exit_reason"] == wd.Watchdog.PAPER_BLIND_EXIT_REASON and len(rpc.sends) == 1


def test_a_counted_position_no_source_prices_on_a_drained_pool_is_booked_at_zero_and_stays_counted(
        tmp_db, monkeypatch, risk_file):  # noqa: F811
    """The pool, asked, says it cannot pay (no reserves): a rug, booked at zero and counted.
    Mutant pinned: skipping ``_settle_blind_counted_paper`` must fail here."""
    rpc = _old_unpriceable(tmp_db, monkeypatch, reserves=(0, 0))
    wd.Watchdog(tmp_db, price_source=wd.NullPriceSource()).tick()
    row = _row(tmp_db)
    assert _closed_at_zero(row), row
    assert row["exit_reason"] == f"{wd.Watchdog.PAPER_TOTAL_LOSS_REASON}:{pc.NO_FILL_BOUND}"
    assert not fabricated.is_fabricated_outcome(row["exit_reason"], "shadow"), "a measured loss, counted"
    assert len(rpc.sends) == 1, "one pool read, at the abandon"
    tag = pc.read_basis(tmp_db, "pos_bx")
    assert tag["exits"][-1]["fill"] == pc.NO_FILL_BOUND and pc.entry_counts(tag) and pc.exit_degraded(tag)
    assert _scoreboard_returns(tmp_db) == [-1.0]


def test_a_counted_honeypot_no_source_prices_is_booked_at_zero_without_a_pool_read(tmp_db, monkeypatch, risk_file):  # noqa: F811
    rpc = _old_unpriceable(tmp_db, monkeypatch, reserves=(10**24, 10**21))
    _dossier(tmp_db, BSC_TOKEN, Chain.BSC, sell_tax=10_000)
    wd.Watchdog(tmp_db, price_source=wd.NullPriceSource()).tick()
    row = _row(tmp_db)
    assert _closed_at_zero(row) and row["exit_reason"].endswith(pc.TOTAL_LOSS) and rpc.sends == []
    tag = pc.read_basis(tmp_db, "pos_bx")
    assert pc.is_honest(tag) and not pc.exit_degraded(tag)


def test_a_token_this_module_cannot_price_at_all_is_still_abandoned_without_a_read(tmp_db, monkeypatch, risk_file):  # noqa: F811
    """The one remaining route from a counted row to the write-off: no read is possible at all (here no
    bsc pool host is configured, ``EXIT_AT_MARK``), so nothing is known about what the remainder is
    worth. Pinned so a change to it is a decision, not an accident."""
    rpc = _old_unpriceable(tmp_db, monkeypatch, reserves=(10**24, 10**21))
    monkeypatch.setattr(m, "bsc_pool_rpc_url", lambda: (None, "bsc_pool_rpc_unset"))
    wd.Watchdog(tmp_db, price_source=wd.NullPriceSource()).tick()
    assert _row(tmp_db)["exit_reason"] == "abandoned_unpriceable" and rpc.sends == []
    assert "bsc_pool_rpc_unset" in _events(tmp_db, "shadow_position_abandoned")[0]["evidence"]


def test_an_uncounted_unpriceable_position_is_abandoned_on_the_age_rule_without_a_pool_read(
        tmp_db, monkeypatch, risk_file):  # noqa: F811
    """Unchanged for a row the scoreboard never counts: one blind tick at age >= 1 h, no read."""
    rpc = _old_unpriceable(tmp_db, monkeypatch, reserves=(0, 0), tag="dossier", blind_s=None)
    wd.Watchdog(tmp_db, price_source=wd.NullPriceSource()).tick()
    assert _row(tmp_db)["exit_reason"] == "abandoned_unpriceable" and rpc.sends == []


def test_a_live_position_is_never_closed_by_this_path(tmp_db, monkeypatch, risk_file):  # noqa: F811
    rpc = _old_unpriceable(tmp_db, monkeypatch, reserves=(10**24, 10**21))
    tmp_db.execute("UPDATE positions SET mode=? WHERE position_id='pos_bx'", (LaneMode.LIVE.value,))
    tmp_db.commit()
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    position = load_position(tmp_db, "pos_bx")
    state = wd.load_state(tmp_db, position)
    assert state.blind_since_ms is not None and dog._counted_paper(position) is False
    assert dog._settle_blind_counted_paper(position, 3.0 * HOUR_S, state) == (False, None)
    assert dog._abandon_unpriceable_shadow(position, wd.PriceQuote.unavailable("none"), state) is False
    assert _row(tmp_db)["closed_ms"] is None and rpc.sends == []
    assert fetch_all(tmp_db, "SELECT * FROM orders WHERE side='sell'") == []
