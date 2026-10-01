"""EVM exit floors must use the priced native asset, never an ERC-20 zero address."""
from decimal import Decimal

import pytest

from kaiba.core.config import RiskConfig
from kaiba.core.schemas import Chain, EvidenceBasis, Lane, LaneMode
from kaiba.execution import watchdog as wd
from tests.test_watchdog import make_position

TOKEN = "0x" + "ab" * 20
TS = 1790053500000


def put_sample(conn, chain, ts, price):
    conn.execute(
        "INSERT INTO native_prices (chain,ts_ms,price_usd,source,receipt_json) VALUES (?,?,?,?,?)",
        (chain.value, ts, price, "fixture", "{}"),
    )


def position(conn, chain):
    make_position(conn, token=TOKEN, chain=chain, mode=LaneMode.LIVE,
                  lane=Lane.SM_TRENCHES, qty=2 * 10**18)
    return wd.open_positions(conn)[0]


@pytest.mark.parametrize("chain,sample_chain,price", [
    (Chain.BSC, Chain.BSC, "600"),
    (Chain.ROBINHOOD, Chain.ETH, "3000"),
])
def test_evm_exit_floor_uses_fresh_native_sample_not_zero_address(
    tmp_db, monkeypatch, chain, sample_chain, price
):
    monkeypatch.setattr(wd, "now_ms", lambda: TS)
    cfg = RiskConfig()
    monkeypatch.setattr(wd, "get_risk", lambda: cfg)
    put_sample(tmp_db, sample_chain, TS - 1000, price)
    pos = position(tmp_db, chain)
    quote = wd.PriceQuote(price_usd=Decimal("0.6"), basis=EvidenceBasis.PROVIDER_REPORTED)
    sender = wd.DefaultExitSubmitter(tmp_db, wd.NullPriceSource())
    expected = int(Decimal("1.2") / Decimal(price) * 10**18 *
                   (Decimal(10000) - wd._exit_slippage_bps()) / 10000)
    assert sender._min_out(pos, pos.qty, 18, quote) == expected
    # Native pricing now has a dedicated seam; a raw token-source probe may
    # remain unavailable for EVM_ZERO without invalidating the sampler fallback.
    assert sender._native_usd(chain) == Decimal(price)
    from kaiba.providers import native_price
    sampled = native_price.at(chain, TS, tmp_db)
    assert sampled.sample_ts_ms == TS - 1000
    assert sample_chain.value in (sampled.receipt.note or "")


@pytest.mark.parametrize("chain,sample_chain", [(Chain.BSC, Chain.BSC), (Chain.ROBINHOOD, Chain.ETH)])
@pytest.mark.parametrize("sample_kind", ["missing", "stale", "future", "zero", "wrong_chain"])
def test_unusable_native_evidence_cannot_authorize_a_sell_floor(
    tmp_db, monkeypatch, chain, sample_chain, sample_kind
):
    monkeypatch.setattr(wd, "now_ms", lambda: TS)
    monkeypatch.setattr(wd, "get_risk", lambda: RiskConfig())
    if sample_kind != "missing":
        ts = TS - 3600000 if sample_kind == "stale" else (TS + 1000 if sample_kind == "future" else TS)
        stored_chain = Chain.SOL if sample_kind == "wrong_chain" else sample_chain
        put_sample(tmp_db, stored_chain, ts, "0" if sample_kind == "zero" else "600")
    pos = position(tmp_db, chain)
    sender = wd.DefaultExitSubmitter(tmp_db, wd.NullPriceSource())
    quote = wd.PriceQuote(price_usd=Decimal("0.6"), basis=EvidenceBasis.PROVIDER_REPORTED)
    assert sender._min_out(pos, pos.qty, 18, quote) is None


def test_native_floor_rounding_to_zero_is_refused(tmp_db, monkeypatch):
    monkeypatch.setattr(wd, "now_ms", lambda: TS)
    monkeypatch.setattr(wd, "get_risk", lambda: RiskConfig())
    put_sample(tmp_db, Chain.BSC, TS, "600")
    pos = position(tmp_db, Chain.BSC)
    sender = wd.DefaultExitSubmitter(tmp_db, wd.NullPriceSource())
    quote = wd.PriceQuote(price_usd=Decimal("0.6"), basis=EvidenceBasis.PROVIDER_REPORTED)
    assert sender._min_out(pos, 1, 18, quote) is None
