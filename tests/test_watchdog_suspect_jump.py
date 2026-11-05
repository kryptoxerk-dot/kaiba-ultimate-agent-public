"""A new quote identity that jumps >= SUSPECT_JUMP_X must be corroborated before it decides a tick.

2026-11-02, shadow sm-trenches ``pos_d7dd91df`` (sol, 10-28 01:53 UTC): the first watchdog quote
came from DexScreener's "deepest" pool, a Meteora DLMM at $0.8525 with liq_usd 352,214, while the
token's other pool and GMGN said ~$0.00066 (GMGN 1m kline high $0.000817). Nothing asked a second
leaf, because only a suspect STOP is corroborated, so tp1/tp2/tp3 trimmed at 1,300-1,500x and the
position closed +63,876%. The numbers below are that position's own.
"""

from __future__ import annotations

from decimal import Decimal

from kaiba.core.db import fetch_one
from kaiba.core.schemas import EvidenceBasis, Lane
from kaiba.execution import watchdog as wd
from kaiba.execution.paper import load_position
from tests.test_watchdog import events_named, make_position, risk_file  # noqa: F401

TOKEN = "EGLpQAhQVB3gN9ZMsgjCBjabf42sPQ3K7T6DRAvy8pr"
ENTRY = "0.0006486607806648857167696866659"
QTY = 78_355_978_217


class Dex:
    """DexScreener's pick for the token: the pool and price are the test's to set."""

    name = "prices"

    def __init__(self, price: str, pool: str = "AR5Xmeteoradlmm", liquidity: str = "352214.04") -> None:
        self.price, self.pool, self.liquidity = price, pool, liquidity

    def quote(self, chain, token):
        return wd.PriceQuote(
            price_usd=Decimal(self.price), liquidity_usd=Decimal(self.liquidity),
            basis=EvidenceBasis.PROVIDER_REPORTED, source="prices:dexscreener", chain=chain, token=token,
            pool_id=self.pool, venue="meteora", liquidity_kind="dex_tvl_usd", quote_symbol="SOL",
            settlement=wd.SETTLE_NATIVE_POOL,
        )


class Gmgn:
    name = "gmgn"

    def __init__(self, price: str | None) -> None:
        self.price = price

    def quote(self, chain, token):
        if self.price is None:
            return wd.PriceQuote.unavailable("gmgn refused", source="gmgn:token.info")
        return wd.PriceQuote(price_usd=Decimal(self.price), basis=EvidenceBasis.PROVIDER_REPORTED,
                             source="gmgn:token.info", chain=chain, token=token)


class Submitter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def submit_exit(self, position, pct, *, quote, reason):  # noqa: ANN001 - test double
        self.calls.append({"pct": pct, "quote": quote, "reason": reason})
        return wd.ExitOutcome(True, wd.OrderState.FILLED, "ord_fake", "filled")


def _tick(tmp_db, dex: Dex, gmgn: Gmgn) -> tuple[Submitter, wd.TickReport]:
    make_position(tmp_db, position_id="pos_jump", token=TOKEN, entry=ENTRY, qty=QTY, lane=Lane.SM_TRENCHES)
    tmp_db.commit()
    sub = Submitter()
    dog = wd.Watchdog(tmp_db, price_source=wd.FallbackPriceSource(dex, gmgn, name="box-shape"), submitter=sub)
    return sub, dog.tick()


def _peak(tmp_db) -> Decimal:
    row = fetch_one(tmp_db, "SELECT peak_price_usd FROM positions WHERE position_id='pos_jump'")
    return Decimal(row["peak_price_usd"])


def test_the_incident_a_1314x_first_quote_from_a_new_pool_decides_nothing_gmgn_does(tmp_db, risk_file):  # noqa: F811
    sub, report = _tick(tmp_db, Dex("0.8525"), Gmgn("0.00066864846"))
    assert report.blind == 0
    assert sub.calls == [], "a take-profit fired on an uncorroborated 1,314x mark"
    assert _peak(tmp_db) < Decimal("0.001"), "the peak (and so the trailing stop) took the bogus mark"
    position = load_position(tmp_db, "pos_jump")
    assert wd.Watchdog(tmp_db, submitter=sub)._read_quote_memory(position)[0].source == "gmgn:token.info"


def test_a_jump_another_leaf_corroborates_is_acted_on_at_the_corroborating_price(tmp_db, risk_file):  # noqa: F811
    sub, _ = _tick(tmp_db, Dex("0.0078"), Gmgn("0.0078"))  # 12x, and GMGN agrees
    assert sub.calls and sub.calls[0]["quote"].source == "gmgn:token.info"


def test_an_uncorroborated_jump_leaves_the_position_blind_not_exited(tmp_db, risk_file):  # noqa: F811
    sub, report = _tick(tmp_db, Dex("0.8525"), Gmgn(None))
    assert sub.calls == [] and report.blind == 1
    blind = events_named(tmp_db, "protection_blind")
    assert blind and "quote_uncorroborated_source_transition" in blind[0]["reason"]


def test_a_new_pool_below_the_factor_still_decides_on_its_own(tmp_db, risk_file):  # noqa: F811
    """9x on a new identity is not suspect: the leaf's own quote decides, nobody else is asked."""
    sub, _ = _tick(tmp_db, Dex("0.0058"), Gmgn(None))
    assert sub.calls and sub.calls[0]["quote"].source == "prices:dexscreener"
