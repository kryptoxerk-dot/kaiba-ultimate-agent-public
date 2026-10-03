"""copy_manager on the OWNER's wallet: named explicitly, never Kaiba's, dry until GMGN binds it.

Owner, 2026-10-03: "I need the agent to control my copy trade as well on the kaiba wallet I
will allow it in the api, do not buy on the kaiba wallet of 0x41.. but control the copy trade
sell before it rug". Kaiba trades from 0xcc44... (risk.yaml); the owner copy-trades on 0x7243....
GMGN only swaps ``--from`` a wallet bound to the API key, and today only 0xcc44... is bound on
robinhood (``portfolio info``, 2026-10-03). All payloads and wallets here are fixtures.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
import yaml

from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, Lane, OrderState, Side, now_ms
from kaiba.execution import copy_manager as CM

RH = Chain.ROBINHOOD
OWNER = "0x72430877378522d1b759ac721561aa9eb9e25c2b"
KAIBA = "0xcc4450c80735778e9f5eaa7a4ea47990e57807c2"
STRANGER = "0x2222222222222222222222222222222222222222"
TOK = "0x96c59a1883aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

#: ``gmgn-cli portfolio info --raw`` as MEASURED 2026-10-03, balances trimmed.
INFO_TODAY = {"wallets": [
    {"chain": "arbitrum", "address": "0x5189ccbd22291a9c7143bac23512c5bd9229bac8", "balances": []},
    {"chain": "base", "address": KAIBA, "balances": []},
    {"chain": "bsc", "address": KAIBA, "balances": []},
    {"chain": "robinhood", "address": KAIBA, "balances": []},
    {"chain": "sol", "address": "rUHrovRbRDoCmn9ojmZ2Pi3bLPUJ15kGjmLfGm6MdTo", "balances": []},
]}
INFO_BOTH = {"wallets": [*INFO_TODAY["wallets"], {"chain": "robinhood", "address": OWNER, "balances": []}]}
INFO_REPLACED = {"wallets": [w for w in INFO_TODAY["wallets"] if w["chain"] != "robinhood"]
                 + [{"chain": "robinhood", "address": OWNER, "balances": []}]}


def row(pnl="0.30"):
    return {"usd_value": "150", "unrealized_profit_pnl": pnl, "start_holding_at": 1_790_690_000,
            "token": {"token_address": TOK, "symbol": "COPY", "decimals": 18, "price": "0.001",
                      "liquidity": "50000", "is_honeypot": False}}


# --------------------------------------------------------------------------------------
# the wallet itself
# --------------------------------------------------------------------------------------


def test_the_wallet_must_be_named_owned_and_not_kaibas():
    owned = {OWNER, KAIBA}
    ok = CM.wallet_refusal(RH, OWNER.upper().replace("0X", "0x"), kaiba_wallet=KAIBA, owned=owned)
    assert ok is None
    assert CM.wallet_refusal(RH, "", kaiba_wallet=KAIBA, owned=owned).startswith("wallet_missing")
    assert CM.wallet_refusal(RH, None, kaiba_wallet=KAIBA, owned=owned).startswith("wallet_missing")
    assert CM.wallet_refusal(RH, KAIBA.upper().replace("0X", "0x"), kaiba_wallet=KAIBA,
                             owned=owned).startswith("wallet_is_kaibas")
    assert CM.wallet_refusal(RH, STRANGER, kaiba_wallet=KAIBA, owned=owned).startswith("wallet_not_owned")
    assert CM.wallet_refusal(RH, OWNER, kaiba_wallet=None, owned=owned).startswith("kaiba_wallet_unknown")
    assert CM.wallet_refusal(RH, "nonsense", kaiba_wallet=KAIBA, owned=owned) == "wallet_invalid"
    assert CM.wallet_refusal(Chain.SOL, OWNER, kaiba_wallet=KAIBA, owned=owned).startswith("chain_unsupported")


def test_the_measured_binding_shape_today_and_after_the_owner_adds_the_wallet():
    today = CM.parse_binding(INFO_TODAY, RH, OWNER, KAIBA)
    assert today.bound is False and today.kaiba_bound is True and today.wallets_on_chain == (KAIBA,)
    both = CM.parse_binding({"data": INFO_BOTH}, RH, OWNER, KAIBA)
    assert both.bound is True and both.kaiba_bound is True
    replaced = CM.parse_binding(INFO_REPLACED, RH, OWNER, KAIBA)
    assert replaced.bound is True and replaced.kaiba_bound is False
    assert CM.parse_binding({"nope": 1}, RH, OWNER, KAIBA).bound is None


# --------------------------------------------------------------------------------------
# the job
# --------------------------------------------------------------------------------------


@pytest.fixture
def seams(tmp_db, tmp_path, monkeypatch):
    """Every side effect of job_copy_manager, recorded. The signer policy is a temp file."""
    from kaiba.execution import executor, policy, watchdog
    from kaiba.providers import gmgn_cli, native_price

    pol = policy.load_policy().model_copy(update={"owned_addresses": {"robinhood": [OWNER, KAIBA]}})
    path = tmp_path / "signer-policy.yaml"
    path.write_text(yaml.safe_dump(pol.model_dump(mode="json")))
    monkeypatch.setenv("KAIBA_SIGNER_POLICY_PATH", str(path))
    rec = SimpleNamespace(holdings=[], balances=[], sent=[], info_calls=[], info=INFO_TODAY, info_ok=True)
    monkeypatch.setattr(watchdog, "exit_wallet_for", lambda chain: KAIBA)

    def units(chain, wallet, token, decimals):
        rec.balances.append(wallet)
        return 10**24

    monkeypatch.setattr(watchdog, "wallet_token_units", units)

    def holdings(wallet, chain, **kw):
        rec.holdings.append(wallet)
        return SimpleNamespace(ok=True, data={"list": [row()]}, receipt=None)

    monkeypatch.setattr(gmgn_cli, "portfolio_holdings", holdings)

    def info(**kw):
        rec.info_calls.append(kw)
        return SimpleNamespace(ok=rec.info_ok, data=rec.info if rec.info_ok else None,
                               receipt=SimpleNamespace(note="fixture: provider down"))

    monkeypatch.setattr(gmgn_cli, "account_info", info)
    monkeypatch.setattr(native_price, "latest",
                        lambda c, conn=None: SimpleNamespace(ts_ms=now_ms(), price_usd=Decimal("2700")))

    def submit(order, conn=None, *, from_wallet=None):
        rec.sent.append((order, from_wallet))
        return executor.SubmitResult(order.order_id, OrderState.SUBMITTED)

    monkeypatch.setattr(executor, "submit", submit)
    return rec


def job(tmp_db, **params):
    from kaiba.ops import scheduler as S

    ts = now_ms()
    ctx = S.JobContext("copy_manager", tmp_db, {"chain": "robinhood", **params}, S.ScheduleConfig(), ts, ts + 60_000)
    return S.job_copy_manager(ctx)


@pytest.mark.parametrize("wallet, why", [(None, "wallet_missing"), (KAIBA, "wallet_is_kaibas"),
                                         (STRANGER, "wallet_not_owned")])
def test_the_job_refuses_a_missing_kaiba_or_unowned_wallet_before_reading_anything(tmp_db, seams, wallet, why):
    from kaiba.ops.scheduler import JobFailed

    params = {"live": True} if wallet is None else {"live": True, "wallet": wallet}
    with pytest.raises(JobFailed, match=why):
        job(tmp_db, **params)
    assert seams.holdings == [] and seams.sent == [] and seams.info_calls == []


def test_configured_live_runs_dry_and_says_why_until_the_wallet_is_bound(tmp_db, seams):
    out = job(tmp_db, live=True, wallet=OWNER)
    assert out["configured_live"] is True and out["live"] is False
    assert out["blocked"] == [CM.BLOCK_NOT_BOUND] and out["binding"]["bound"] is False
    assert [d["action"] for d in out["decisions"]] == ["dry_run"] and seams.sent == []


def test_once_bound_it_reads_and_sells_only_the_named_wallet(tmp_db, seams):
    seams.info = INFO_BOTH
    out = job(tmp_db, live=True, wallet=OWNER)
    assert out["live"] is True and out["blocked"] == [] and out["alarms"] == []
    assert seams.holdings == [OWNER] and seams.balances == [OWNER]
    assert len(seams.sent) == 1
    order, from_wallet = seams.sent[0]
    assert from_wallet == OWNER and order.side is Side.SELL and order.lane is Lane.MANUAL


def test_an_unreadable_binding_is_never_taken_as_bound(tmp_db, seams):
    seams.info_ok = False
    out = job(tmp_db, live=True, wallet=OWNER)
    assert out["live"] is False and out["blocked"] == [CM.BLOCK_BINDING_UNKNOWN] and seams.sent == []


def test_a_binding_that_replaced_kaibas_wallet_raises_an_alarm(tmp_db, seams):
    seams.info = INFO_REPLACED
    out = job(tmp_db, live=True, wallet=OWNER)
    assert out["alarms"] == ["kaiba_wallet_not_api_bound"]
    assert out["live"] is True and len(seams.sent) == 1, "the owner's protection still runs"


def test_the_binding_read_is_hourly_at_discovery_priority(tmp_db, seams):
    job(tmp_db, live=False, wallet=OWNER)
    assert seams.info_calls == [{"priority": Priority.DISCOVERY, "ttl_s": 3600.0}]


# --------------------------------------------------------------------------------------
# the seam: a wallet is required, and only a sell can be built
# --------------------------------------------------------------------------------------


def test_the_submitter_has_no_default_wallet_and_only_sells(tmp_db, monkeypatch):
    from kaiba.execution import executor

    with pytest.raises(TypeError):
        CM.gmgn_submitter(tmp_db, RH, 2500)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="wallet is required"):
        CM.gmgn_submitter(tmp_db, RH, 2500, wallet=" ")
    seen = []
    monkeypatch.setattr(executor, "submit", lambda order, conn=None, *, from_wallet=None: (
        seen.append((order.side, order.lane, from_wallet)) or executor.SubmitResult(order.order_id, OrderState.SUBMITTED)))
    CM.gmgn_submitter(tmp_db, RH, 2500, wallet=OWNER)(TOK, 10, 1)
    assert seen == [(Side.SELL, Lane.MANUAL, OWNER)]


def test_wallet_is_a_job_key_not_a_policy_key():
    cfg = CM.CopyConfig.from_params({"wallet": OWNER, "chain": "robinhood", "binding_ttl_s": 600})
    assert not hasattr(cfg, "wallet")
    with pytest.raises(ValueError, match="unknown keys"):
        CM.CopyConfig.from_params({"walet": OWNER})
