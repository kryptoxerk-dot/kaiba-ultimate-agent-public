"""``wallet_tape`` stores what moved, stops at its deadline, and resumes where it stopped.

MEASURED on the box before 2026-10-02: 3 of the last 4 runs timed out at 1,500 s (sol
755-1,149 s, robinhood 389-691 s, bsc 142-187 s for 928 wallets) and every run upserted
every scored wallet -- 424k sol + 100k robinhood rows with their JSON payloads -- although
97.4% of the history rows those passes wrote repeated the wallet's previous grade. The
per-wallet lookup path is no way out: 2,000 sol wallets took more than 500 s read-only.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kaiba.core.schemas import Chain, now_ms
from kaiba.intelligence import grade as GR
from kaiba.ops import scheduler as S

_n = iter(range(10**9))


def round_trips(conn, wallet: str, trips: int, *, chain: Chain = Chain.ROBINHOOD,
                token: str | None = None, start: int = 1_790_000_000_000) -> None:
    """``trips`` closed buy/sell round trips for ``wallet`` on one token."""
    tok = token or "0xtok" + wallet[-6:]
    for i in range(trips * 2):
        k = next(_n)
        conn.execute(
            "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, "
            "amount_native, usd_value, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (chain.value, f"0xtx{k}", start + k * 1000, wallet, tok,
             "buy" if i % 2 == 0 else "sell", 1000, 10**16 * (1 + i % 2), 25.0, "test"),
        )
    conn.commit()


def wallets(n: int) -> list[str]:
    return ["0x%040x" % (i + 1) for i in range(n)]


def stamps(conn) -> dict[str, int]:
    return {r[0]: r[1] for r in conn.execute("SELECT address, scored_at_ms FROM wallet_scores")}


# ------------------------------------------------------------------ store only what moved


def test_an_unchanged_tape_grade_is_not_rewritten(tmp_db):
    """THE WRITE STORM: the second pass over an unchanged tape writes nothing."""
    for w in wallets(4):
        round_trips(tmp_db, w, 3)
    first = GR.grade_tape(tmp_db, Chain.ROBINHOOD, store=True, skip_unchanged=True)
    assert first.stored == 4 and first.unchanged_skipped == 0
    before = stamps(tmp_db)
    second = GR.grade_tape(tmp_db, Chain.ROBINHOOD, store=True, skip_unchanged=True)
    assert second.wallets_scored == 4
    assert second.stored == 0 and second.unchanged_skipped == 4, second.as_dict()
    assert stamps(tmp_db) == before


def test_a_grade_that_moved_is_rewritten_and_only_that_one(tmp_db):
    ws = wallets(3)
    for w in ws:
        round_trips(tmp_db, w, 3)
    GR.grade_tape(tmp_db, Chain.ROBINHOOD, store=True, skip_unchanged=True)
    round_trips(tmp_db, ws[1], 6, token="0xother")          # new evidence for one wallet
    rep = GR.grade_tape(tmp_db, Chain.ROBINHOOD, store=True, skip_unchanged=True)
    assert rep.stored == 1 and rep.unchanged_skipped == 2, rep.as_dict()
    row = tmp_db.execute("SELECT closed_trades FROM wallet_scores WHERE address=?",
                         (ws[1],)).fetchone()
    assert row[0] == 9


def test_a_trust_grade_is_restamped_every_pass(tmp_db, monkeypatch):
    """A and B carry a freshness budget for their readers, so they are always rewritten."""
    for w in wallets(2):
        round_trips(tmp_db, w, 3)
    GR.grade_tape(tmp_db, Chain.ROBINHOOD, store=True, skip_unchanged=True)
    grade_seen = tmp_db.execute("SELECT DISTINCT grade FROM wallet_scores").fetchall()
    monkeypatch.setattr(GR, "TAPE_ALWAYS_RESTAMP", frozenset(g[0] for g in grade_seen))
    rep = GR.grade_tape(tmp_db, Chain.ROBINHOOD, store=True, skip_unchanged=True)
    assert rep.stored == 2 and rep.unchanged_skipped == 0


def test_an_old_unchanged_grade_is_restamped(tmp_db):
    ws = wallets(2)
    for w in ws:
        round_trips(tmp_db, w, 3)
    GR.grade_tape(tmp_db, Chain.ROBINHOOD, store=True, skip_unchanged=True)
    old = now_ms() - 2 * GR.TAPE_RESTAMP_AFTER_MS - 1     # past base + any jitter
    tmp_db.execute("UPDATE wallet_scores SET scored_at_ms=? WHERE address=?", (old, ws[0]))
    tmp_db.commit()
    rep = GR.grade_tape(tmp_db, Chain.ROBINHOOD, store=True, skip_unchanged=True)
    assert rep.stored == 1 and rep.unchanged_skipped == 1
    assert stamps(tmp_db)[ws[0]] > old


def test_restamp_jitter_spreads_the_due_time():
    now = 10 * GR.TAPE_RESTAMP_AFTER_MS
    due = [GR._restamp_due(w, now - int(1.5 * GR.TAPE_RESTAMP_AFTER_MS), now)
           for w in wallets(200)]
    assert 0 < sum(due) < 200, "every wallet comes due on the same run"


# ------------------------------------------------------------------ deadline and resume


def test_the_pass_stops_between_wallets_and_resumes_after_the_last_one(tmp_db):
    ws = wallets(5)
    for w in ws:
        round_trips(tmp_db, w, 2)
    ticks = iter([0.0, 0.0, 9.0] + [9.0] * 10)
    cut = GR.grade_tape(tmp_db, Chain.ROBINHOOD, store=True, deadline=1.0,
                        clock=lambda: next(ticks))
    assert cut.truncated and cut.wallets_seen == 2 and cut.resume_after == ws[1]
    assert cut.stored == 2, "work done before the deadline must be flushed, not lost"
    rest = GR.grade_tape(tmp_db, Chain.ROBINHOOD, store=True, start_after=cut.resume_after)
    assert not rest.truncated and rest.resume_after is None
    assert rest.wallets_seen == 3
    assert tmp_db.execute("SELECT COUNT(*) FROM wallet_scores").fetchone()[0] == 5


def test_candidates_are_kept_in_a_bounded_heap_with_the_same_order(tmp_db, monkeypatch):
    """The list held a row for EVERY qualifying wallet; the heap holds ``candidate_limit``."""
    monkeypatch.setattr(GR, "tape_candidate_reason", lambda ev, score: "test")
    ws = wallets(6)
    for i, w in enumerate(ws):
        round_trips(tmp_db, w, (i % 3) + 1)   # closed episodes 1,2,3,1,2,3: ties on purpose
    full = GR.grade_tape(tmp_db, Chain.ROBINHOOD, candidate_limit=100).candidates
    top = GR.grade_tape(tmp_db, Chain.ROBINHOOD, candidate_limit=3).candidates
    assert [c["address"] for c in top] == [c["address"] for c in full[:3]]
    assert [c["address"] for c in full[:2]] == [ws[2], ws[5]], "ties must keep scan order"
    # A tie AT the cut: the wallet met first keeps the last place, as the stable sort did.
    one = GR.grade_tape(tmp_db, Chain.ROBINHOOD, candidate_limit=1).candidates
    assert [c["address"] for c in one] == [ws[2]]
    assert GR.grade_tape(tmp_db, Chain.ROBINHOOD, candidate_limit=0).candidates == []


def _score(address: str, **over: Any) -> GR.WalletScore:
    base: dict[str, Any] = dict(
        chain=Chain.ROBINHOOD, address=address, score=31.0, grade=GR.Grade.C,
        evidence_weight=40.0, archetype=GR.Archetype.TRADER, closed_trades=5,
        distinct_tokens=3, win_rate=0.6, realized_pnl_usd=None, penalties=["p"],
        blockers=["b"], median_hold_s=60, model_version=GR.MODEL_ID_TAPE,
        scored_at_ms=now_ms(),
    )
    base.update(over)
    return GR.WalletScore(**base)


@pytest.mark.parametrize(("field", "value", "unchanged"), [
    ("scored_at_ms", now_ms() + 60_000, True),  # a newer stamp alone is not a change ...
    ("median_hold_s", 99_999, True),         # ... nor is open-episode hold drift
    ("score", 31.004, True),                 # below the stored precision
    ("score", 31.5, False),
    ("grade", GR.Grade.D, False),
    ("archetype", GR.Archetype.SNIPER, False),
    ("evidence_weight", 41.0, False),
    ("closed_trades", 6, False),
    ("distinct_tokens", 4, False),
    ("win_rate", 0.7, False),
    ("penalties", ["q"], False),
    ("blockers", ["c"], False),
    ("model_version", "kaiba-wallet-gmgn-v1", False),
])
def test_the_unchanged_rule_column_by_column(tmp_db, field, value, unchanged):
    """Which columns count as 'moved'. Every decision column does; bookkeeping does not."""
    GR.store_scores([_score("0xa")], tmp_db)
    candidate = _score("0xa", **{field: value})
    assert (GR.unchanged_tape_scores(tmp_db, [candidate]) == {"0xa"}) is unchanged


# ------------------------------------------------------------------ the job


def ctx(conn, params: dict[str, Any], *, timeout_s: int = 600, clock=None):
    now = now_ms()
    kw = {"clock": clock} if clock is not None else {}
    return S.JobContext("wallet_tape", conn, params, S.ScheduleConfig(), now,
                        now + timeout_s * 1000, **kw)


def test_the_job_skips_unchanged_grades_by_default(tmp_db):
    for w in wallets(3):
        round_trips(tmp_db, w, 3)
    first = S.job_wallet_tape(ctx(tmp_db, {"chains": ["robinhood"], "store": True}))
    assert first["stored"] == 3
    second = S.job_wallet_tape(ctx(tmp_db, {"chains": ["robinhood"], "store": True}))
    assert second["stored"] == 0 and second["unchanged_skipped"] == 3, second


def test_the_job_resumes_a_cut_pass_and_runs_small_tapes_first(tmp_db, monkeypatch):
    calls: list[dict[str, Any]] = []
    plan = {"sol": [("w2", True), (None, False)], "bsc": [(None, False)] * 3,
            "robinhood": [(None, False)] * 3}

    def fake_tape(conn, chain, **kw):
        calls.append({"chain": chain.value, "start_after": kw.get("start_after"),
                      "deadline": kw.get("deadline")})
        resume, truncated = plan[chain.value].pop(0)
        rep = GR.TapeRunReport(chain=chain.value, wallets_seen={"sol": 50, "bsc": 1,
                                                                "robinhood": 10}[chain.value])
        rep.truncated, rep.resume_after = truncated, resume
        rep.candidates = [{"address": f"{chain.value}-new", "closed_episodes": 1}]
        return rep

    monkeypatch.setattr(GR, "grade_tape", fake_tape)
    params = {"chains": ["sol", "bsc", "robinhood"], "store": True}
    out = S.job_wallet_tape(ctx(tmp_db, params))
    assert [c["chain"] for c in calls] == ["sol", "bsc", "robinhood"]   # nothing known yet
    assert out["per_chain"]["sol"]["truncated"] is True
    state = json.loads(tmp_db.execute(
        "SELECT value FROM kv WHERE key='ops:wallet_tape:pass:sol'").fetchone()[0])
    assert state["resume_after"] == "w2" and state["full_pass_wallets"] is None

    calls.clear()
    out = S.job_wallet_tape(ctx(tmp_db, params))
    # bsc (1) and robinhood (10) have completed passes; sol has not: it runs last and
    # inherits whatever time they leave.
    assert [c["chain"] for c in calls] == ["bsc", "robinhood", "sol"]
    assert calls[2]["start_after"] == "w2" and out["per_chain"]["sol"]["resumed"] is True
    assert calls[0]["deadline"] < calls[1]["deadline"] < calls[2]["deadline"]
    state = json.loads(tmp_db.execute(
        "SELECT value FROM kv WHERE key='ops:wallet_tape:pass:sol'").fetchone()[0])
    assert state["resume_after"] is None and state["full_pass_wallets"] == 100


def test_a_partial_pass_keeps_candidates_it_did_not_reach(tmp_db):
    tmp_db.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,0)",
        ("ops:wallet_tape:candidates:sol",
         json.dumps([{"address": "a", "closed_episodes": 9}, {"address": "m", "closed_episodes": 8},
                     {"address": "z", "closed_episodes": 7}])),
    )
    tmp_db.commit()
    fresh = [{"address": "n", "closed_episodes": 5}]
    merged = S._merge_tape_candidates(tmp_db, Chain.SOL, fresh, lo="c", hi="p", limit=10)
    # 'm' was inside the covered range (c, p] and was re-judged: gone. 'a' and 'z' stay.
    assert [c["address"] for c in merged] == ["a", "z", "n"]
    assert len(S._merge_tape_candidates(tmp_db, Chain.SOL, fresh, lo=None, hi=None,
                                        limit=10)) == 1


def test_a_job_that_hits_its_deadline_still_finishes_inside_it(tmp_db):
    """The point of the deadline: the scheduler records ok, not timeout."""
    for w in wallets(4):
        round_trips(tmp_db, w, 2)
    t = {"now": 1_000.0}

    def clock() -> float:
        t["now"] += 20.0          # every look at the clock costs 20 s of "work"
        return t["now"]

    job = S.JobContext("wallet_tape", tmp_db, {"chains": ["robinhood"], "store": True},
                       S.ScheduleConfig(), 1_000_000, 1_000_000 + 120_000, clock=clock)
    out = S.job_wallet_tape(job)
    assert out["per_chain"]["robinhood"]["truncated"] is True
    assert out["wallets_seen"] < 4


@pytest.mark.parametrize("bad", [None, "", "not-json"])
def test_a_missing_or_corrupt_pass_state_starts_from_the_beginning(tmp_db, bad):
    if bad is not None:
        tmp_db.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,0)",
                       ("ops:wallet_tape:pass:sol", bad))
        tmp_db.commit()
    assert S._tape_pass_state(tmp_db, Chain.SOL).get("resume_after") is None
