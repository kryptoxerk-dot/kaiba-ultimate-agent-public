import csv
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kaiba.ingest.social_dispatch import watched_accounts
from kaiba.ingest.wallet_stream_sol import USDC_MINT, WSOL_MINT
from kaiba.learning.ansem_paper import (
    Buy,
    Ledger,
    Quote,
    Watcher,
    decode_buy,
    exact_quote,
    price_ran,
)

WALLET = "CuyLCJVPqJCcyUCNSGgzxWhZAJ7h6Gknn79hu4effVUN"
TOKEN = "98HEU4TvXB3xjB1t15fgxHwYDLUH4fDomvp2DCzJpump"
JUP = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
AMOUNT = 10_000_000


@pytest.fixture(autouse=True)
def free_disk(monkeypatch):
    monkeypatch.setattr("kaiba.learning.ansem_paper.shutil.disk_usage",
                        lambda _path: SimpleNamespace(used=80, total=100))


def receipt(quote_mint=USDC_MINT):
    def balance(mint, amount):
        return {"owner": WALLET, "mint": mint, "uiTokenAmount": {"amount": str(amount), "decimals": 6}}

    is_sol = quote_mint == WSOL_MINT
    return {
        "slot": 12, "blockTime": int(time.time()),
        "transaction": {"signatures": ["signature"], "message": {
            "accountKeys": [{"pubkey": WALLET, "signer": True}],
            "instructions": [{"programId": JUP}],
        }},
        "meta": {
            "err": None, "fee": 5_000, "preBalances": [500_000_000],
            "postBalances": [499_995_000 - (100_000_000 if is_sol else 0)],
            "preTokenBalances": [] if is_sol else [balance(USDC_MINT, AMOUNT)],
            "postTokenBalances": [balance(TOKEN, 10_000)],
        },
    }


def test_sol_and_stable_buy_and_ambiguous_rejection():
    tx = receipt()
    assert decode_buy(tx, WALLET) == Buy(TOKEN, USDC_MINT, AMOUNT, 10_000)
    assert decode_buy(receipt(WSOL_MINT), WALLET) == Buy(TOKEN, WSOL_MINT, 100_000_000, 10_000)
    tx["meta"]["postBalances"][0] -= 10_000_000
    assert decode_buy(tx, WALLET) is None
    tx = receipt()
    tx["transaction"]["message"]["instructions"] = []
    assert decode_buy(tx, WALLET) is None


def test_quote_binding_and_cutoff():
    payload = dict(inputMint=USDC_MINT, outputMint=TOKEN, inAmount=str(AMOUNT),
                   outAmount="10000", otherAmountThreshold="9900", contextSlot=12,
                   swapMode="ExactIn", routePlan=[{}], slippageBps=100)
    assert exact_quote(payload, USDC_MINT, TOKEN, AMOUNT) == Quote(10_000, 9900, 12)
    for invalid in ({"inAmount": "1"}, {"outputMint": WSOL_MINT}, {"otherAmountThreshold": "9000"}):
        with pytest.raises(ValueError):
            exact_quote(payload | invalid, USDC_MINT, TOKEN, AMOUNT)
    buy = Buy(TOKEN, USDC_MINT, AMOUNT, 11_000)
    assert not price_ran(buy, AMOUNT, 10_000)
    assert price_ran(buy, AMOUNT, 9999)


@pytest.mark.parametrize(("quote", "status"), [
    (Quote(10_000, 9900, 12), "open"),
    (Quote(9_000, 8910, 12), "price_already_ran"),
    (Quote(10_000, 9900, 11), "stale_quote"),
])
async def test_notice_dedup_and_paper_exit(tmp_path, quote, status):
    ledger = Ledger(tmp_path / "ledger.json")
    watcher = Watcher(WALLET, AsyncMock(), ledger)
    watcher.receipt = AsyncMock(return_value=receipt())
    watcher.quote = AsyncMock(return_value=quote)
    await watcher.on_notice("signature", 12, time.time())
    await watcher.on_notice("signature", 12, time.time())
    watcher.quote.assert_awaited_once_with(USDC_MINT, TOKEN, AMOUNT)
    assert ledger.rows[0]["status"] == status
    assert Ledger(ledger.path).rows == ledger.rows
    if status == "open":
        watcher.quote = AsyncMock(return_value=Quote(9_500_000, 9_405_000, 13))
        await watcher.exit_once(dict(ledger.rows[0], exit_due_at=time.time()))
        watcher.quote.assert_awaited_once_with(TOKEN, USDC_MINT, 9900)
        assert ledger.rows[0]["status"] == "closed"
        assert ledger.rows[0]["quote_pnl_atoms"] == -595_000


async def test_stale_receipt_never_requests_quote(tmp_path):
    ledger = Ledger(tmp_path / "ledger.json")
    watcher = Watcher(WALLET, AsyncMock(), ledger)
    watcher.receipt = AsyncMock(return_value=receipt() | {"blockTime": int(time.time()) - 20})
    watcher.quote = AsyncMock()
    await watcher.on_notice("signature", 12, time.time())
    watcher.quote.assert_not_awaited()
    assert ledger.rows[0]["status"] == "stale_or_unbound_receipt"


def test_kol_registry_includes_j7_and_owner_accounts():
    path = Path("skills/j7-social-tracking/references/j7-accounts.csv")
    with path.open(encoding="utf-8-sig") as f:
        original = {row["handle"].lower() for row in csv.DictReader(f)}
    registered = watched_accounts(j7=True)
    if not original:
        import pytest

        pytest.skip("public snapshot: the author's account rosters ship header-only")
    assert original | {"blknoiz06", "zhaidf_", "0xwassie", "51bodila"} <= registered
