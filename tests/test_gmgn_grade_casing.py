"""gmgn_grade after the 2026-10-03 repair (journal #4987): exact base58 keys, no grade built
on an unanswered enrichment, and a paced, budgeted, resumable scheduled pass.

MEASURED on the box before the fix: all 5,439 ``sol`` rows under ``kaiba-wallet-gmgn-v1``
keyed on a lowercased base58 string (each with a real-case twin in ``wallets``), and 628
robinhood rows UNSCORED at evidence weight 29.0 with win_rate NULL. Every address and
provider answer below is a fixture.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain
from kaiba.intelligence import gmgn_grade as G
from kaiba.intelligence import grade

SOL_A = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
SOL_B = "62EbtFbmJATT94Y6Uguf6a1kdaJuZWAu8ycqbg3xKfQg"
EVM = ["0x" + c * 40 for c in "abcdef"]


def put_wallet(conn, chain, address, *, tags="[]", cohort=None):
    conn.execute("INSERT OR REPLACE INTO wallets (chain, address, source, tags_json, first_seen_ms, last_seen_ms, cohort) "
                 "VALUES (?,?,?,?,?,?,?)", (chain.value, address, "fixture", tags, 1, 1, cohort))


def put_score(conn, chain, address, model, *, grade_="C", win=0.5):
    conn.execute("INSERT OR REPLACE INTO wallet_scores (chain, address, score, grade, evidence_weight, archetype, "
                 "win_rate, factors_json, penalties_json, blockers_json, receipts_json, model_version, scored_at_ms) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (chain.value, address, 30.0, grade_, 29.0, "trader", win, "[]", "[]", "[]", "[]", model, 1))


class Provider:
    """``portfolio profits`` + ``portfolio stats``, recorded. ``stats_ok`` may be a set of
    addresses whose enrichment fails (the call itself, not an empty answer)."""

    def __init__(self, monkeypatch, *, fold=False, profits_ok=True, fail=()):
        self.profits, self.stats, self.kw = [], [], []
        self.fold, self.profits_ok, self.fail = fold, profits_ok, set(fail)
        monkeypatch.setattr(G.gmgn_cli, "portfolio_profits", self._profits)
        monkeypatch.setattr(G.gmgn_cli, "portfolio_stats", self._stats)

    def _profits(self, wallets, chain, **kw):
        self.profits.append(list(wallets))
        self.kw.append(kw)
        if not self.profits_ok:
            return SimpleNamespace(ok=False, data=None)
        return SimpleNamespace(ok=True, data=[
            {"wallet_address": w.lower() if self.fold else w, "buy": 40, "sell": 30, "realized_profit": "2500"}
            for w in wallets])

    def _stats(self, address, chain, **kw):
        self.stats.append(address)
        self.kw.append(kw)
        if address in self.fail:
            return SimpleNamespace(ok=False, data=None)
        return SimpleNamespace(ok=True, data={"pnl_stat": {"winrate": 0.61, "token_num": 80}})


def stored(conn, chain):
    return {r["address"]: r for r in conn.execute(
        "SELECT address, model_version, win_rate FROM wallet_scores WHERE chain=?", (chain.value,))}


# --------------------------------------------------------------------------------------
# casing
# --------------------------------------------------------------------------------------


def test_base58_keeps_its_case_and_evm_is_lowercased():
    assert G.normalize(Chain.SOL, f" {SOL_A} ") == SOL_A
    assert G.normalize(Chain.ROBINHOOD, "0xABCdef" + "0" * 34) == "0xabcdef" + "0" * 34
    assert G.is_junk_key(Chain.SOL, SOL_A.lower()) and not G.is_junk_key(Chain.SOL, SOL_A)
    assert not G.is_junk_key(Chain.BSC, EVM[0])


def test_a_sol_grade_is_stored_under_the_real_address(tmp_db, monkeypatch):
    put_wallet(tmp_db, Chain.SOL, SOL_A)
    Provider(monkeypatch)
    rep = G.run(Chain.SOL, tmp_db, store=True)
    assert rep["stored"] == 1
    assert set(stored(tmp_db, Chain.SOL)) == {SOL_A}, "never the lowercased junk key"


def test_a_provider_that_folds_case_is_keyed_back_to_the_address_asked(monkeypatch):
    Provider(monkeypatch, fold=True)
    out = G.provider_stats_for(Chain.SOL, [SOL_A, SOL_B])
    assert set(out) == {SOL_A, SOL_B}


def test_two_asked_addresses_that_fold_together_are_not_guessed(monkeypatch):
    twin = SOL_A.swapcase()
    Provider(monkeypatch, fold=True)
    out = G.provider_stats_for(Chain.SOL, [SOL_A, twin])
    assert out == {}


def test_lowercased_junk_keys_are_not_candidates(tmp_db):
    put_wallet(tmp_db, Chain.SOL, SOL_A)
    put_wallet(tmp_db, Chain.SOL, SOL_A.lower())
    assert G.candidates(tmp_db, Chain.SOL) == [SOL_A]


# --------------------------------------------------------------------------------------
# an unanswered enrichment is never written down
# --------------------------------------------------------------------------------------


def test_a_grade_is_not_stored_when_its_enrichment_went_unanswered(tmp_db, monkeypatch):
    for w in EVM[:2]:
        put_wallet(tmp_db, Chain.ROBINHOOD, w)
    Provider(monkeypatch, fail={EVM[0]})
    rep = G.run(Chain.ROBINHOOD, tmp_db, store=True)
    assert rep["enrich_unanswered"] == 1 and rep["stored"] == 1
    rows = stored(tmp_db, Chain.ROBINHOOD)
    assert EVM[0] not in rows, "unanswered: retried later, never stored a point short"
    assert rows[EVM[1]]["win_rate"] == pytest.approx(0.61), "positive control: answered is stored with its win rate"


# --------------------------------------------------------------------------------------
# the scheduled pass
# --------------------------------------------------------------------------------------


def test_the_pool_puts_failed_enrichments_first_and_never_our_own_grades(tmp_db):
    rh = Chain.ROBINHOOD
    put_score(tmp_db, rh, EVM[3], G.MODEL_ID_PROVIDER, grade_="B")
    put_score(tmp_db, rh, EVM[4], G.MODEL_ID_PROVIDER, grade_="UNSCORED", win=None)     # tier 0
    put_wallet(tmp_db, rh, EVM[1], tags='["gmgn:smart_degen"]')                           # tagged, ungraded
    put_wallet(tmp_db, rh, EVM[2], tags='["gmgn:smart_degen"]')
    put_score(tmp_db, rh, EVM[2], grade.MODEL_ID_TAPE)                                    # ours: never
    put_wallet(tmp_db, rh, EVM[5], tags='["gmgn:smart_degen"]', cohort="blacklist")
    put_wallet(tmp_db, rh, EVM[0])                                                        # untagged
    assert G.pool(tmp_db, rh) == [EVM[4], EVM[1], EVM[3]]


def test_the_sol_pool_regrades_the_real_twins_of_the_junk_keys(tmp_db):
    sol = Chain.SOL
    for real in (SOL_A, SOL_B):
        put_wallet(tmp_db, sol, real)
        put_wallet(tmp_db, sol, real.lower())                       # the junk registry row
        put_score(tmp_db, sol, real.lower(), G.MODEL_ID_PROVIDER)   # the junk grade
    put_score(tmp_db, sol, SOL_B, grade.MODEL_ID_TAPE)              # ours: never overwritten
    assert G.pool(tmp_db, sol) == [SOL_A]


def test_the_pool_is_built_once_a_day_not_every_run(tmp_db):
    """Building it is three whole-chain scans (~6 s each on the box under idle I/O)."""
    rh = Chain.ROBINHOOD
    put_wallet(tmp_db, rh, EVM[0], tags='["gmgn:kol"]')
    keys, rebuilt = G.cached_pool_keys(tmp_db, rh, now=1_000)
    assert keys == [f"1|{EVM[0]}"] and rebuilt
    put_wallet(tmp_db, rh, EVM[1], tags='["gmgn:kol"]')
    assert G.cached_pool_keys(tmp_db, rh, now=1_000 + 3_600_000) == (keys, False)
    tmp_db.execute("UPDATE kv SET updated_ms = 0 WHERE key = ?", (G.POOL_PREFIX + rh.value,))
    later, rebuilt = G.cached_pool_keys(tmp_db, rh, now=G.POOL_MAX_AGE_MS + 1)
    assert later == [f"1|{EVM[0]}", f"1|{EVM[1]}"] and rebuilt


def _tagged(conn, chain, addrs):
    for a in addrs:
        put_wallet(conn, chain, a, tags='["gmgn:kol"]')


def test_the_budget_stops_the_pass_and_the_cursor_resumes_it(tmp_db, monkeypatch):
    rh = Chain.ROBINHOOD
    _tagged(tmp_db, rh, EVM[:3])
    p = Provider(monkeypatch)
    first = G.run_budgeted(tmp_db, rh, max_calls=3)              # 1 profits + 2 stats
    assert first["stopped"] == "budget" and first["stored"] == 2 and first["calls"] == 3
    assert first["cursor_to"] == f"1|{EVM[1]}"
    second = G.run_budgeted(tmp_db, rh, max_calls=10)
    assert p.profits[-1] == [EVM[2]], "resumed after the cursor, not from the start"
    assert second["stored"] == 1 and second["stopped"] is None
    third = G.run_budgeted(tmp_db, rh, max_calls=10)
    assert third["wrapped"] is True and third["visited"] == 3


def test_an_unanswered_enrichment_stops_the_pass_without_moving_past_the_wallet(tmp_db, monkeypatch):
    rh = Chain.ROBINHOOD
    _tagged(tmp_db, rh, EVM[:3])
    Provider(monkeypatch, fail={EVM[1]})
    rep = G.run_budgeted(tmp_db, rh, max_calls=50)
    assert rep["stopped"] == "enrich_unanswered" and rep["cursor_to"] == f"1|{EVM[0]}"
    assert set(stored(tmp_db, rh)) == {EVM[0]}
    p = Provider(monkeypatch)
    G.run_budgeted(tmp_db, rh, max_calls=50)
    assert p.profits[0] == [EVM[1], EVM[2]], "the next run retries the wallet it could not finish"


def test_a_provider_outage_moves_nothing(tmp_db, monkeypatch):
    rh = Chain.ROBINHOOD
    _tagged(tmp_db, rh, EVM[:2])
    Provider(monkeypatch, profits_ok=False)
    rep = G.run_budgeted(tmp_db, rh, max_calls=50)
    assert rep["stopped"] == "provider_unavailable" and rep["calls"] == 1
    assert G.load_cursor(tmp_db, rh) == "" and stored(tmp_db, rh) == {}


def test_reads_go_at_discovery_priority_and_wait_for_their_slot(tmp_db, monkeypatch):
    rh = Chain.ROBINHOOD
    _tagged(tmp_db, rh, EVM[:1])
    p = Provider(monkeypatch)
    G.run_budgeted(tmp_db, rh, max_calls=10)
    assert p.kw and all(k["priority"] is Priority.DISCOVERY and k["wait_for_slot_s"] == G.WAIT_FOR_SLOT_S
                        for k in p.kw)


def test_a_tape_grade_landing_mid_pass_is_not_overwritten(tmp_db, monkeypatch):
    rh = Chain.ROBINHOOD
    _tagged(tmp_db, rh, EVM[:1])
    p = Provider(monkeypatch)
    real = p._stats

    def stats_then_tape(address, chain, **kw):
        put_score(tmp_db, rh, address, grade.MODEL_ID_TAPE)     # wallet_tape stores meanwhile
        return real(address, chain, **kw)

    monkeypatch.setattr(G.gmgn_cli, "portfolio_stats", stats_then_tape)
    rep = G.run_budgeted(tmp_db, rh, max_calls=10)
    assert rep["scored"] == 1 and rep["stored"] == 0
    assert stored(tmp_db, rh)[EVM[0]]["model_version"] == grade.MODEL_ID_TAPE


# --------------------------------------------------------------------------------------
# the scheduler job
# --------------------------------------------------------------------------------------


def _job(conn, **params):
    from kaiba.core.schemas import now_ms
    from kaiba.ops import scheduler as S

    ts = now_ms()
    ctx = S.JobContext("gmgn_grade", conn, {"pace_s": 0, **params}, S.ScheduleConfig(), ts, ts + 300_000)
    return S.job_gmgn_grade(ctx), ctx


def test_the_job_spends_its_daily_quota_and_no_more(tmp_db, monkeypatch):
    rh = Chain.ROBINHOOD
    _tagged(tmp_db, rh, EVM[:4])
    p = Provider(monkeypatch)
    out, ctx = _job(tmp_db, chains="robinhood", max_calls_per_run=50, max_calls_per_day=4)
    assert out["calls"] == 4 and ctx.quota_used()[0] == 4
    again, _ = _job(tmp_db, chains="robinhood", max_calls_per_run=50, max_calls_per_day=4)
    assert again["reason"] == "daily_quota_reached" and len(p.profits) + len(p.stats) == 4


def test_the_job_shares_one_budget_across_chains_in_order(tmp_db, monkeypatch):
    _tagged(tmp_db, Chain.ROBINHOOD, EVM[:2])
    put_wallet(tmp_db, Chain.SOL, SOL_A, tags='["gmgn:kol"]')
    Provider(monkeypatch)
    out, _ = _job(tmp_db, chains="robinhood,sol", max_calls_per_run=3, max_calls_per_day=100)
    assert out["per_chain"]["robinhood"]["calls"] == 3
    assert out["per_chain"]["sol"] == {"stopped": "budget"}
