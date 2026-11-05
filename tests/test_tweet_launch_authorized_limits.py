"""Owner-approved native spend boundaries; isolated SQLite, no provider calls."""
import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from kaiba.core.schemas import NATIVE_DECIMALS, Chain
from kaiba.execution import tweet_launch as tl

APPROVED = [(Chain.SOL, "1.5", "6"), (Chain.BSC, "0.5", "1"),
            (Chain.ROBINHOOD, "0.1", "0.3")]
DAY = 1_791_331_200_000 // 86_400_000 * 86_400_000


@pytest.fixture
def ledger():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE tweet_launches (chain TEXT,author TEXT,tweet_id TEXT,"
              "decided_ms INTEGER,buy_amt_native TEXT,mode TEXT,state TEXT)")
    yield c
    c.close()


def settings():
    return tl.load_config(Path(__file__).resolve().parents[1] / "config/tweet_launch.yaml")


def finite_settings():
    """Original allowance fixture remains a positive control for configured finite caps."""
    return replace(settings(),daily_launch_cap=10,
                   daily_native_cap={chain:Decimal(daily) for chain,_,daily in APPROVED})


def prior(c, chain, amount, state="confirmed", at=DAY+1):
    c.execute("INSERT INTO tweet_launches VALUES (?,?,?,?,?,'live',?)",
              (chain.value, "prior_author", "prior_post", at, str(amount), state))


def proposed(chain, amount):
    return SimpleNamespace(chain=chain, buy_amt_native=Decimal(amount),
                           author="new_author", tweet_id="new_post")


@pytest.mark.parametrize("chain,launch,daily", APPROVED)
def test_deployed_config_matches_explicit_owner_limits(chain, launch, daily):
    cfg = settings()
    assert cfg.chains[chain].max_buy_native == Decimal(launch)
    assert cfg.daily_native_cap[chain] is None
    assert cfg.daily_launch_cap is None


@pytest.mark.parametrize("chain,launch,daily", APPROVED)
@pytest.mark.parametrize("state", ["submitting", "submitted", "ambiguous", "confirmed"])
def test_exact_daily_boundary_admitted_one_atom_more_refused(ledger, chain, launch, daily, state):
    cfg = finite_settings()
    prior(ledger, chain, Decimal(daily)-Decimal(launch), state)
    assert tl.spend_check(ledger, proposed(chain, launch), cfg, DAY+1000) is None
    extra = Decimal(launch) + Decimal(1).scaleb(-NATIVE_DECIMALS[chain])
    reason = tl.spend_check(ledger, proposed(chain, extra), cfg, DAY+1000)
    assert reason.startswith(f"daily_native_cap:{chain.value}:")


@pytest.mark.parametrize("chain,launch,daily", APPROVED)
def test_daily_reset_uses_utc_boundary(ledger, chain, launch, daily):
    prior(ledger, chain, daily, at=DAY-1)
    assert tl.spend_check(ledger, proposed(chain, launch), finite_settings(), DAY+1000) is None
    prior(ledger, chain, daily, at=DAY)
    assert tl.spend_check(ledger, proposed(chain, launch), finite_settings(), DAY+1000).startswith("daily_native_cap")


@pytest.mark.parametrize("chain,launch,daily", APPROVED)
def test_five_percent_quote_fits_approval_and_never_silently_shrinks(chain, launch, daily):
    route = settings().chains[chain]
    amount, share, _ = tl.dev_buy_native(route, 5)
    assert Decimal(0) < amount <= Decimal(launch) and share == 5
    smaller = replace(route, max_buy_native=amount-Decimal(1).scaleb(-NATIVE_DECIMALS[chain]))
    assert tl.dev_buy_native(smaller, 5) == (None, None, "five_percent_exceeds_route_cap")


def test_chain_spend_is_separate_but_launch_count_is_global(ledger):
    cfg = finite_settings()
    prior(ledger, Chain.BSC, "1")
    assert tl.spend_check(ledger, proposed(Chain.SOL, "1.5"), cfg, DAY+1000) is None
    for _ in range(cfg.daily_launch_cap-1):
        prior(ledger, Chain.ROBINHOOD, "0.01")
    assert tl.spend_check(ledger, proposed(Chain.SOL, "1.5"), cfg, DAY+1000).startswith("daily_launch_cap")


@pytest.mark.parametrize("chain,launch,daily",APPROVED)
def test_unlimited_daily_allowance_admits_after_many_ambiguous_and_confirmed_reservations(ledger,chain,launch,daily):
    for _ in range(100):
        prior(ledger,chain,daily,state="ambiguous")
    cfg=settings()
    assert tl.spend_check(ledger,proposed(chain,launch),cfg,DAY+1000) is None
    assert tl.spend_check(ledger,proposed(chain,launch),finite_settings(),DAY+1000).startswith("daily_launch_cap")


def test_unlimited_retains_author_cooldown(ledger):
    p=proposed(Chain.SOL,"1.5")
    ledger.execute("INSERT INTO tweet_launches VALUES (?,?,?,?,?,'live','confirmed')",
                   ("sol",p.author,"different_post",DAY+1,"1.5"))
    assert tl.spend_check(ledger,p,settings(),DAY+1000)=="author_cooldown:new_author"
