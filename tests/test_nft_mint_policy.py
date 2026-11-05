import concurrent.futures
import datetime as dt

import pytest

from kaiba.execution.nft_mint_policy import GasEnvelope, GasLedger, MintRefused, day_of


def reserve(ledger, n, *, usd=100_000, native=10, at=0, chain="sol"):
    return ledger.reserve(key=f"{n:064x}", chain=chain, payload_sha="a" * 64,
                          upper_usd=usd, upper_native=native, at_ms=at)


def test_global_budget_shared_across_chains_and_pending_counts(tmp_path):
    ledger = GasLedger(tmp_path / "gas.db", GasEnvelope(100_000, 300_000))
    for i, chain in enumerate(["sol", "bsc", "robinhood"]):
        assert reserve(ledger, i, chain=chain)["new"]
    with pytest.raises(MintRefused, match="daily_gas_cap"):
        reserve(ledger, 4)
    assert ledger.status(0)["pending_micro_usd"] == 300_000


def test_new_chains_share_existing_ledger_and_do_not_gain_separate_daily_budgets(tmp_path):
    ledger = GasLedger(tmp_path / "gas.db", GasEnvelope(100_000, 300_000))
    reserve(ledger, 1, chain="bsc")
    # Reopening an existing ledger preserves the frozen numeric envelope.
    ledger = GasLedger(tmp_path / "gas.db", GasEnvelope(100_000, 300_000))
    reserve(ledger, 2, chain="base")
    reserve(ledger, 3, chain="eth")
    with pytest.raises(MintRefused, match="daily_gas_cap"):
        reserve(ledger, 4, chain="sol")
    with pytest.raises(MintRefused, match="chain_refused"):
        reserve(ledger, 5, chain="abstract")
    assert ledger.status(0)["pending_micro_usd"] == 300_000


def test_atomic_concurrent_reservations_cannot_overspend(tmp_path):
    ledger = GasLedger(tmp_path / "gas.db", GasEnvelope(100_000, 300_000))

    def attempt(n):
        try:
            return reserve(ledger, n)["new"]
        except MintRefused:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(20))) == 3


def test_unknown_submit_never_released_by_midnight_or_timeout(tmp_path):
    ledger = GasLedger(tmp_path / "gas.db", GasEnvelope(100_000, 100_000))
    reserve(ledger, 1)
    ledger.submitted(f"{1:064x}", "fixture-request", ambiguous=True)
    with pytest.raises(MintRefused, match="daily_gas_cap"):
        reserve(ledger, 2, at=2*86_400_000)
    assert ledger.status(2*86_400_000)["pending_micro_usd"] == 100_000


def test_failed_transaction_fee_counts_and_unused_reservation_is_released_on_receipt(tmp_path):
    ledger = GasLedger(tmp_path / "gas.db", GasEnvelope(100_000, 200_000))
    reserve(ledger, 1)
    ledger.submitted(f"{1:064x}", "fixture-request")
    ledger.settle(f"{1:064x}", actual_usd=40_000, actual_native=4, settled_ms=1000, receipt_sha="b" * 64)
    assert ledger.status(1000)["spent_micro_usd"] == 40_000
    assert ledger.status(1000)["pending_micro_usd"] == 0
    reserve(ledger, 2, at=1001)


def test_exact_idempotency_and_conflicts(tmp_path):
    ledger = GasLedger(tmp_path / "gas.db")
    assert reserve(ledger, 1)["new"]
    assert not reserve(ledger, 1)["new"]
    with pytest.raises(MintRefused, match="idempotency_conflict"):
        reserve(ledger, 1, native=11)
    assert ledger.submitted(f"{1:064x}", "fixture", ambiguous=True) is True
    assert ledger.submitted(f"{1:064x}", "fixture", ambiguous=True) is False


def test_fee_breach_halts_future_entries(tmp_path):
    ledger = GasLedger(tmp_path / "gas.db")
    reserve(ledger, 1)
    ledger.submitted(f"{1:064x}", "fixture")
    ledger.settle(f"{1:064x}", actual_usd=100_001, actual_native=11, settled_ms=1, receipt_sha="b" * 64)
    assert ledger.status(1)["halted"]
    with pytest.raises(MintRefused, match="gas_ledger_halted"):
        reserve(ledger, 2, at=2)


def test_envelope_cannot_silently_reset_existing_budget(tmp_path):
    path = tmp_path / "gas.db"
    GasLedger(path)
    with pytest.raises(MintRefused, match="envelope_changed"):
        GasLedger(path, GasEnvelope(100_000, 20_000_000))


def test_day_boundary_is_jakarta_not_host_timezone():
    ms = int(dt.datetime(2026, 11, 2, 17, tzinfo=dt.UTC).timestamp() * 1000)
    assert day_of(ms - 1) == "2026-11-02" and day_of(ms) == "2026-11-03"


@pytest.mark.parametrize("usd,native", [(100_001, 10), (0, 10), (100_000, 0), (True, 10)])
def test_per_mint_and_native_bounds_cannot_be_missing(usd, native, tmp_path):
    ledger = GasLedger(tmp_path / "gas.db")
    with pytest.raises(MintRefused):
        reserve(ledger, 1, usd=usd, native=native)
