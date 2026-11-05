"""job_wallet_deepen: more history only where it can change an A verdict, under a credit cap.

Written as the failures it prevents:

* paying for pages that cannot buy an A -- a tape or provider grade (never an A by
  construction), a walk already exhausted or at its page cap, a wallet far under the A
  score, or a spray buyer with no closed round trip;
* paying past the day's credits, or for the meta pass the grade never reads;
* switching off the first-buyer rebuild the regrade's early_edge reads;
* deepening the same wallet on every run instead of spreading the budget.

Nothing touches the network: the backfill and the grader are fakes.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from kaiba.core.db import fetch_one
from kaiba.core.schemas import Chain, now_ms
from kaiba.ops import scheduler as S

TAPE = S.TAPE_MODEL_VERSION
FULL = "kaiba-wallet-v1"
GMGN = S.GMGN_PROVIDER_MODEL_VERSION


def ctx_for(conn: Any, params: dict[str, Any]) -> S.JobContext:
    now = now_ms()
    config = S.ScheduleConfig(helius=S.HeliusGate(floor_credits=300_000))
    return S.JobContext("wallet_deepen", conn, params, config, now, now + 60_000)


def score(conn: Any, w: str, *, s: float, grade: str = "B", model: str = FULL, ev: float = 94.0,
          closed: int = 5) -> None:
    conn.execute(
        "INSERT INTO wallet_scores (chain, address, score, grade, evidence_weight, archetype, "
        "closed_trades, model_version, scored_at_ms) VALUES (?,?,?,?,?,?,?,?,?)",
        ("sol", w, s, grade, ev, "unknown", closed, model, now_ms()),
    )


def walk(conn: Any, w: str, *, pages: int = 1, exhausted: bool = False) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?)",
        (f"backfill:swaps:sol:{w}", json.dumps({"pages": pages, "exhausted": exhausted}), 1),
    )


def mark(conn: Any, w: str, *, age_ms: int) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?)",
        (f"{S.DEEPEN_MARK_PREFIX}sol:{w}", "{}", now_ms() - age_ms),
    )


#: (wallet, score kwargs, walk kwargs or None for no walk, mark age in ms or None)
POPULATION: list[tuple[str, dict[str, Any], dict[str, Any] | None, int | None]] = [
    ("IN_HIGH", {"s": 68.0, "closed": 3}, {}, None),
    ("IN_LOW", {"s": 62.0, "closed": 12}, {"pages": 2}, None),
    ("IN_OLD_MARK", {"s": 61.0, "closed": 4}, {}, 7 * 3_600_000),
    ("OUT_EXHAUSTED", {"s": 66.0}, {"pages": 3, "exhausted": True}, None),
    ("OUT_TAPE", {"s": 69.0, "model": TAPE}, {}, None),
    ("OUT_GMGN", {"s": 69.5, "model": GMGN}, {}, None),
    ("OUT_FAR", {"s": 55.0, "closed": 20}, {}, None),
    ("OUT_SPRAY", {"s": 67.0, "closed": 0, "ev": 37.0}, {}, None),
    ("OUT_NO_WALK", {"s": 64.0}, None, None),
    ("OUT_CAPPED", {"s": 63.0}, {"pages": 20}, None),
    ("OUT_RECENT", {"s": 65.0}, {}, 3_600_000),
    ("OUT_UNSCORED", {"s": 88.0, "grade": "UNSCORED", "ev": 12.0}, {}, None),
]


def seed_population(conn: Any) -> None:
    for wallet, scored, walked, mark_age in POPULATION:
        score(conn, wallet, **scored)
        if walked is not None:
            walk(conn, wallet, **walked)
        if mark_age is not None:
            mark(conn, wallet, age_ms=mark_age)


def targets(conn: Any, **kw: Any) -> list[str]:
    kw.setdefault("min_score", 60.0)
    kw.setdefault("max_pages", 20)
    kw.setdefault("limit", 50)
    kw.setdefault("retry_after_ms", 6 * 3_600_000)
    return [t["wallet"] for t in S.deepen_targets(conn, Chain.SOL, **kw)]


def test_the_grader_reads_the_cursor_the_backfill_writes():
    from kaiba.ingest.backfill import cursor_key
    from kaiba.intelligence.grade import BACKFILL_CURSOR_PREFIX

    assert f"{BACKFILL_CURSOR_PREFIX}sol:W" == cursor_key(Chain.SOL, "W")


def test_only_paid_walks_near_the_a_score_with_room_to_go_are_targets(tmp_db):
    """MUTATIONS each caught here: dropping the exhausted skip (OUT_EXHAUSTED), the page
    cap (OUT_CAPPED), the model filter (OUT_TAPE, OUT_GMGN), min_closed (OUT_SPRAY), the
    score floor (OUT_FAR), the retry window (OUT_RECENT) or the B/C grade filter
    (OUT_UNSCORED); and ordering by anything but score."""
    seed_population(tmp_db)
    assert targets(tmp_db) == ["IN_HIGH", "IN_LOW", "IN_OLD_MARK"]
    assert targets(tmp_db, limit=1) == ["IN_HIGH"]
    # The A line itself, not a constant: a narrower margin drops the 62 and the 61.
    assert targets(tmp_db, min_score=65.0) == ["IN_HIGH"]


def test_the_shipped_margin_is_ten_points_under_the_a_score():
    from kaiba.intelligence.grade import A_MIN_SCORE

    assert A_MIN_SCORE - S.DEEPEN_SCORE_MARGIN == 60.0 and S.DEEPEN_MIN_CLOSED == 1


@pytest.fixture
def fakes(monkeypatch: Any) -> SimpleNamespace:
    from kaiba.ingest import backfill as BF
    from kaiba.intelligence import grade as GR
    from kaiba.providers import helius

    state = SimpleNamespace(calls=[], graded=[])
    monkeypatch.setattr(helius, "budget_status",
                        lambda conn=None, period=None: {"remaining": 900_000, "resets_in_s": 20 * 86_400})

    def fake_backfill(conn: Any, chain: Chain, *, wallets: list[str], pages: int, with_meta: bool,
                      max_credits: int, **kw: Any) -> BF.BackfillReport:
        state.calls.append({"wallets": list(wallets), "pages": pages, "with_meta": with_meta,
                            "max_credits": max_credits,
                            "rebuild_buyers": kw.get("rebuild_buyers", True)})
        report = BF.BackfillReport(chain=chain, credits_spent=100 * pages * len(wallets),
                                   first_buyers_written=3 * len(wallets))
        for w in wallets:
            report.results.append(BF.WalletResult(wallet=w, chain=chain, pages=pages))
        return report

    def fake_grade(address: str, chain: Any, conn: Any, *, store: bool = True) -> SimpleNamespace:
        state.graded.append(address)
        conn.execute("UPDATE wallet_scores SET score = 71.0 WHERE chain = 'sol' AND address = ?", (address,))
        return SimpleNamespace(grade="B")

    monkeypatch.setattr(BF, "backfill_wallets", fake_backfill)
    monkeypatch.setattr(GR, "grade_address", fake_grade)
    return state


PARAMS = {"wallets_per_run": 2, "pages_per_wallet": 5, "max_pages": 20, "credits_per_day": 1000,
          "max_credits_per_run": 1000, "retry_after_hours": 6}


def test_a_run_walks_regrades_marks_and_books_its_credits(tmp_db, fakes):
    """MUTATIONS caught: the meta pass back on (with_meta), the first-buyer rebuild
    switched off (rebuild_buyers=False: the regrade would read stale early entries), no
    mark written (the second run would walk the same wallets again), the quota not booked."""
    seed_population(tmp_db)
    out = S.job_wallet_deepen(ctx_for(tmp_db, PARAMS))
    assert [c["wallets"] for c in fakes.calls] == [["IN_HIGH"], ["IN_LOW"]]
    assert all(c["pages"] == 5 and c["with_meta"] is False and c["rebuild_buyers"] is True
               for c in fakes.calls)
    assert out["first_buyers"] == 6
    assert fakes.graded == ["IN_HIGH", "IN_LOW"]
    assert out["wallets"] == 2 and out["pages"] == 10 and out["credits"] == 1000
    assert out["walked"][0]["score"] == "68.0->71.0" and out["walked"][0]["grade"] == "B->B"
    assert S.quota_used(tmp_db, "wallet_deepen", S.utc_day(now_ms())) == (2, 1000)
    assert fetch_one(tmp_db, "SELECT 1 AS x FROM kv WHERE key = ?", (f"{S.DEEPEN_MARK_PREFIX}sol:IN_HIGH",))
    # Second run, with credits to spare: the two are inside their retry window, the third
    # candidate is next, and OUT_FAR (55, under the A score's margin) is not -- the room
    # would have paid for it. MUTATION: a score floor at 0 walks OUT_FAR here.
    out = S.job_wallet_deepen(ctx_for(tmp_db, {**PARAMS, "credits_per_day": 3000,
                                                "max_credits_per_run": 3000}))
    assert [c["wallets"] for c in fakes.calls[2:]] == [["IN_OLD_MARK"]] and out["wallets"] == 1
    assert out["min_score"] == 60.0


def test_the_days_credits_bind_the_pages(tmp_db, fakes):
    """MUTATION FINDING: a plan that ignored the credit room handed both wallets 5 pages
    against 700 credits. The room is split in page units, highest score first, and a run
    with nothing left calls nothing and names the binding limit."""
    seed_population(tmp_db)
    out = S.job_wallet_deepen(ctx_for(tmp_db, {**PARAMS, "credits_per_day": 700}))
    assert [(c["wallets"], c["pages"]) for c in fakes.calls] == [(["IN_HIGH"], 5), (["IN_LOW"], 2)]
    assert out["credits"] == 700 and out["credit_room"]["binding"] == "daily_ceiling"
    n = len(fakes.calls)
    out = S.job_wallet_deepen(ctx_for(tmp_db, {**PARAMS, "credits_per_day": 700, "retry_after_hours": 0}))
    assert out["reason"] == S.REASON_QUOTA and out["wallets"] == 0 and len(fakes.calls) == n


def test_a_wallet_near_its_page_cap_gets_only_the_pages_left(tmp_db, fakes):
    score(tmp_db, "NEAR_CAP", s=69.0)
    walk(tmp_db, "NEAR_CAP", pages=18)
    S.job_wallet_deepen(ctx_for(tmp_db, PARAMS))
    assert fakes.calls == [{"wallets": ["NEAR_CAP"], "pages": 2, "with_meta": False,
                            "max_credits": 1000, "rebuild_buyers": True}]


def test_nothing_to_deepen_spends_nothing(tmp_db, fakes):
    out = S.job_wallet_deepen(ctx_for(tmp_db, PARAMS))
    assert out["reason"] == "no_candidates" and out["wallets"] == 0 and fakes.calls == []


def test_chains_without_a_paid_history_route_are_refused(tmp_db, fakes):
    out = S.job_wallet_deepen(ctx_for(tmp_db, {**PARAMS, "chain": "robinhood"}))
    assert out == {"reason": "no_history_route", "chain": "robinhood", "wallets": 0}
    assert fakes.calls == []


def test_the_job_is_registered_as_a_helius_spender():
    """spends_helius puts it behind the dispatcher's credit floor like every paid job."""
    assert S.JOBS["wallet_deepen"].spends_helius is True
