"""Wallet gathering throughput: the credit-bound quota and the signal-ordered queue.

Written as the failures they prevent, in the scheduler's own style:

* ``test_the_daily_credit_ceiling_binds_before_the_count_quota`` — a run cannot spend past
  what ``ops_quota`` says is left of today's credits, and the per-run ``max_credits`` handed
  to the backfill is that remainder, not the run's nominal size.
* ``test_a_ceiling_above_the_hard_maximum_is_clamped_and_said`` — a typo in the YAML cannot
  burn the month in a day.
* ``test_the_monthly_pace_binds_near_the_floor`` — the budget, not an arbitrary count, is
  the binding limit when the month runs low.
* ``test_the_queue_is_ordered_by_signal_not_recency`` — the newest wallet with nothing going
  for it goes last; the ordering that would have put it first is shown to differ.
* ``test_the_queue_degrades_when_a_signal_source_is_absent`` — a table another piece has
  not created yet is UNAVAILABLE, not a crash, and the queue still comes back ordered.

Nothing here touches the network; the backfill and the grader are fakes.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from kaiba.core.db import fetch_all, fetch_one, jdump
from kaiba.core.schemas import Chain, now_ms
from kaiba.ops import scheduler as S

SOL = Chain.SOL.value
BSC = Chain.BSC.value
EST = S.CREDITS_PER_WALLET_EST


# --------------------------------------------------------------------------- helpers


def ctx_for(conn: Any, name: str, params: dict[str, Any], *, floor: int = 300_000,
            timeout_s: int = 60) -> S.JobContext:
    now = now_ms()
    config = S.ScheduleConfig(helius=S.HeliusGate(floor_credits=floor))
    return S.JobContext(name, conn, params, config, now, now + timeout_s * 1000)


def seed_swap(conn: Any, wallet: str, token: str, *, ts_ms: int, source: str = "pumpfun:trades",
              side: str = "buy", chain: str = SOL) -> None:
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, source) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (chain, f"tx-{wallet}-{token}-{ts_ms}-{side}", ts_ms, wallet, token, side, "1", source),
    )


def seed_score(conn: Any, address: str, *, grade: str = "C", scored_at_ms: int | None = None,
               chain: str = SOL) -> None:
    conn.execute(
        "INSERT INTO wallet_scores (chain, address, score, grade, evidence_weight, archetype, "
        "model_version, scored_at_ms) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(chain, address) DO UPDATE SET grade=excluded.grade, scored_at_ms=excluded.scored_at_ms",
        (chain, address, 50.0, grade, 10.0, "unknown", "test", scored_at_ms or now_ms()),
    )


def seed_token(conn: Any, token: str, *, created_ms: int, migrated_ms: int | None) -> None:
    conn.execute(
        "INSERT INTO tokens (chain, address, created_ms, migrated_ms, first_seen_ms) VALUES (?,?,?,?,?)",
        (SOL, token, created_ms, migrated_ms, created_ms),
    )


def seed_entity(conn: Any, entity_id: str, members: list[str]) -> None:
    ts = now_ms()
    conn.execute(
        "INSERT INTO entities (entity_id, chain, confidence, size, created_ms, updated_ms) "
        "VALUES (?,?,?,?,?,?)", (entity_id, SOL, 0.9, len(members), ts, ts),
    )
    for m in members:
        conn.execute("INSERT INTO entity_members (entity_id, chain, address) VALUES (?,?,?)",
                     (entity_id, SOL, m))


def seed_feed_event(conn: Any, wallet: str, tags: list[str], *, source: str = "gmgn:smartmoney") -> None:
    """What gmgn_feeds.write_swap emits for every feed row: source and tags in the payload."""
    conn.execute(
        "INSERT INTO events (ts_ms, kind, level, chain, subject, payload) VALUES (?,?,?,?,?,?)",
        (now_ms(), "wallet.trade", "info", SOL, wallet,
         jdump({"wallet": wallet, "source": source, "feed": source.split(":")[-1], "tags": tags})),
    )


def fake_ledger(monkeypatch: Any, *, remaining: int, resets_in_s: int = 10 * 86_400) -> None:
    from kaiba.providers import helius

    monkeypatch.setattr(helius, "budget_status", lambda conn=None, period=None: {
        "period": "2026-09", "allowance": 1_000_000, "used": 1_000_000 - remaining,
        "remaining": remaining, "resets_in_s": resets_in_s,
    })


@pytest.fixture
def fakes(monkeypatch: Any) -> SimpleNamespace:
    """A backfill that charges 125 per wallet and a grader that writes C, both recorded."""
    from kaiba.ingest import backfill as BF
    from kaiba.intelligence import grade as GR

    state = SimpleNamespace(backfilled=[], max_credits=[], graded=[])

    def fake_backfill(conn: Any, chain: Chain, *, wallets: list[str] | None = None, **kw: Any) -> BF.BackfillReport:
        targets = list(wallets or [])
        if wallets is None:  # the tracked job selects by limit, not by list
            targets = [f"tracked-{i}" for i in range(int(kw.get("limit", 0)))]
        state.backfilled.append(targets)
        state.max_credits.append(kw["max_credits"])
        report = BF.BackfillReport(chain=chain, credits_spent=EST * len(targets))
        for w in targets:
            report.results.append(BF.WalletResult(wallet=w, chain=chain))
        return report

    def fake_grade(address: str, chain: Any, conn: Any, *, store: bool = True) -> SimpleNamespace:
        state.graded.append((address, Chain(chain).value))
        seed_score(conn, address, grade="C", chain=Chain(chain).value)
        return SimpleNamespace(grade="C")

    monkeypatch.setattr(BF, "backfill_wallets", fake_backfill)
    monkeypatch.setattr(GR, "grade_address", fake_grade)
    return state


# --------------------------------------------------------------------------- the ceiling


def test_the_daily_credit_ceiling_binds_before_the_count_quota(tmp_db, fakes, monkeypatch):
    fake_ledger(monkeypatch, remaining=990_000)
    ts = now_ms()
    for w in "ABCDEFGH":
        seed_swap(tmp_db, w, "T1", ts_ms=ts)
    params = {"wallets_per_run": 10, "wallets_per_day": 100, "credits_per_day": 300}

    out = S.job_wallet_buyers(ctx_for(tmp_db, "wallet_buyers", params))
    # 300 credits is room for two wallets at the 125 estimate, whatever the count quota says,
    # and the backfill's own ceiling is the 300, not the run's nominal 10 x 250.
    assert out["wallets"] == 2 and fakes.backfilled == [["A", "B"]]
    assert fakes.max_credits == [300]
    assert out["credit_room"]["binding"] == "daily_ceiling"
    assert S.quota_used(tmp_db, "wallet_buyers", S.utc_day(now_ms())) == (2, 250)

    out = S.job_wallet_buyers(ctx_for(tmp_db, "wallet_buyers", params))
    # 50 credits left today: under one wallet, so nothing is called and the reason says why.
    assert out["reason"] == "daily_quota_reached" and out["binding"] == "daily_ceiling"
    assert out["wallets"] == 0 and len(fakes.backfilled) == 1
    assert out["credit_room"]["credits"] == 50 and out["credit_room"]["used_today"] == 250


def test_without_a_credits_per_day_the_old_implicit_ceiling_holds(tmp_db, fakes, monkeypatch):
    """No YAML change, no behaviour change: the ceiling is wallets_per_day x estimate x 2."""
    fake_ledger(monkeypatch, remaining=990_000)
    ts = now_ms()
    for w in "ABCDE":
        seed_swap(tmp_db, w, "T1", ts_ms=ts)
    out = S.job_wallet_buyers(ctx_for(tmp_db, "wallet_buyers", {"wallets_per_run": 2, "wallets_per_day": 3}))
    assert out["credit_room"]["daily_ceiling"] == 3 * EST * 2
    assert out["credit_room"]["binding"] == "run_size" and fakes.max_credits == [2 * EST * 2]
    assert out["wallets"] == 2


def test_a_ceiling_above_the_hard_maximum_is_clamped_and_said(tmp_db, fakes, monkeypatch):
    fake_ledger(monkeypatch, remaining=990_000)
    seed_swap(tmp_db, "A", "T1", ts_ms=now_ms())
    params = {"wallets_per_run": 1, "wallets_per_day": 1, "credits_per_day": 10_000_000}
    out = S.job_wallet_buyers(ctx_for(tmp_db, "wallet_buyers", params))
    room = out["credit_room"]
    assert room["ceiling_clamped"] is True
    assert room["daily_ceiling"] == S.GATHER_DAILY_CREDIT_HARD_MAX
    assert S.GATHER_DAILY_CREDIT_HARD_MAX <= 0.05 * S.FREE_MONTHLY_CREDITS


def test_the_monthly_pace_binds_near_the_floor(tmp_db, fakes, monkeypatch):
    ts = now_ms()
    for w in "ABCDE":
        seed_swap(tmp_db, w, "T1", ts_ms=ts)
    params = {"wallets_per_run": 5, "wallets_per_day": 100, "credits_per_day": 10_000}
    # 500 credits above a 300,000 floor with two days to the reset: 250 a day, i.e. two
    # wallets today at the estimate, and the backfill is told 250, not 10,000.
    fake_ledger(monkeypatch, remaining=300_500, resets_in_s=2 * 86_400)
    out = S.job_wallet_buyers(ctx_for(tmp_db, "wallet_buyers", params))
    assert out["credit_room"]["binding"] == "monthly_pace"
    assert out["credit_room"]["pace_per_day"] == 250 and out["credit_room"]["days_left"] == 2
    assert out["wallets"] == 2 and fakes.max_credits == [250]
    # At the floor: nothing, with the floor named.
    fake_ledger(monkeypatch, remaining=300_000, resets_in_s=2 * 86_400)
    out = S.job_wallet_buyers(ctx_for(tmp_db, "wallet_buyers", params))
    assert out["reason"] == "daily_quota_reached" and out["binding"] == "helius_budget_floor"
    assert len(fakes.backfilled) == 1


def test_an_unreadable_ledger_spends_nothing(tmp_db, fakes, monkeypatch):
    from kaiba.providers import helius

    def boom(conn=None, period=None):
        raise RuntimeError("ledger locked")

    monkeypatch.setattr(helius, "budget_status", boom)
    seed_swap(tmp_db, "A", "T1", ts_ms=now_ms())
    out = S.job_wallet_buyers(ctx_for(tmp_db, "wallet_buyers", {"wallets_per_run": 1, "wallets_per_day": 5}))
    assert out["reason"] == "daily_quota_reached" and out["binding"] == "budget_unreadable"
    assert out["credit_room"]["pace_per_day"] == "UNAVAILABLE" and out["credit_room"]["credits"] == 0
    assert fakes.backfilled == []


def test_credit_room_fields_are_integers(tmp_db, monkeypatch):
    fake_ledger(monkeypatch, remaining=650_000, resets_in_s=int(3.5 * 86_400))
    room = S.gather_credit_room(ctx_for(tmp_db, "wallet_buyers", {"credits_per_day": 1000}),
                                daily_default=0, run_max=999)
    for name in ("credits", "daily_ceiling", "used_today", "run_max", "pace_per_day", "remaining", "days_left"):
        assert isinstance(getattr(room, name), int), name
    assert room.days_left == 4 and room.pace_per_day == 350_000 // 4
    assert room.credits == 999 and room.binding == "run_size"


def test_the_tracked_job_is_bound_by_the_same_credit_room(tmp_db, fakes, monkeypatch):
    fake_ledger(monkeypatch, remaining=990_000)
    params = {"wallets_per_run": 3, "wallets_per_day": 10, "credits_per_day": 200}
    out = S.job_wallet_tracked(ctx_for(tmp_db, "wallet_tracked", params))
    assert out["wallets"] == 1 and fakes.max_credits == [200]
    assert out["credit_room"]["binding"] == "daily_ceiling"
    out = S.job_wallet_tracked(ctx_for(tmp_db, "wallet_tracked", params))
    assert out["reason"] == "daily_quota_reached" and out["binding"] == "daily_ceiling"


# --------------------------------------------------------------------------- the ordering


def _seed_signal_universe(conn: Any) -> int:
    """Five ungraded buyers, one per signal, the signal-less one the most recent."""
    ts = now_ms()
    seed_swap(conn, "RECENT", "T1", ts_ms=ts)                      # newest, nothing else
    seed_swap(conn, "FEED", "T1", ts_ms=ts - 5_000)                # on the GMGN feed, app tags only
    seed_swap(conn, "FEED", "X1", ts_ms=ts - 5_000, source="gmgn:smartmoney")
    seed_feed_event(conn, "FEED", ["gmgn", "axiom"])
    seed_token(conn, "GRAD", created_ms=ts - 3_600_000, migrated_ms=ts - 60_000)
    seed_swap(conn, "EARLY", "T1", ts_ms=ts - 6_000)               # bought GRAD at +30 s
    seed_swap(conn, "EARLY", "GRAD", ts_ms=ts - 3_600_000 + 30_000)
    seed_swap(conn, "SIBLING", "T1", ts_ms=ts - 7_000)             # entity with a graded member
    seed_score(conn, "GRADED", grade="B")
    seed_entity(conn, "ent-1", ["GRADED", "SIBLING"])
    seed_swap(conn, "TAGGED", "T1", ts_ms=ts - 8_000)              # smart_degen on the feed
    seed_swap(conn, "TAGGED", "X2", ts_ms=ts - 8_000, source="gmgn:kol")
    seed_feed_event(conn, "TAGGED", ["smart_degen", "gmgn"], source="gmgn:kol")
    return ts


def test_the_queue_is_ordered_by_signal_not_recency(tmp_db):
    ts = _seed_signal_universe(tmp_db)
    q = S.gather_queue(tmp_db, Chain.SOL, limit=10, now=ts)
    order = [r["wallet"] for r in q.rows]
    # TAGGED: feed 4 + tag 2. EARLY: early 3 + one point for its two tokens. FEED: 4 on one
    # token, so it loses the tie to EARLY on tokens. SIBLING: 2. RECENT: newest, nothing, last.
    assert order == ["TAGGED", "EARLY", "FEED", "SIBLING", "RECENT"], order
    by = {r["wallet"]: r for r in q.rows}
    assert by["TAGGED"]["priority"] == 6 and by["EARLY"]["priority"] == 4 and by["FEED"]["priority"] == 4
    assert by["SIBLING"]["priority"] == 2 and by["RECENT"]["priority"] == 0
    assert by["EARLY"]["early_graduated"] == 1 and by["SIBLING"]["graded_siblings"] == 1
    assert by["TAGGED"]["feed"] == 1 and by["TAGGED"]["tags_positive"] == 1
    # The ordering the old job used would have led with the newest wallet.
    by_recency = [r["wallet"] for r in sorted(q.rows, key=lambda r: -r["last_ms"])]
    assert by_recency[0] == "RECENT" and by_recency != order
    # And the old helper still agrees on the admitted set, so nothing was lost in the swap.
    old = {r["wallet"] for r in S.ungraded_buyers(tmp_db, Chain.SOL, limit=10, now=ts)}
    assert old == set(order)
    assert q.signals["feed"] == "ok:2" and q.signals["early_graduated"] == "ok:1"
    # Two members of ent-1 have a graded sibling (GRADED counts itself); GRADED is not a
    # candidate because it already has a score, so only SIBLING carries the point.
    assert q.signals["graded_siblings"] == "ok:2" and q.signals["tags"] == "ok:1"
    assert q.signals["flagged"].startswith("UNAVAILABLE")


def test_recency_is_only_the_last_tiebreak(tmp_db):
    ts = now_ms()
    seed_swap(tmp_db, "NEW", "T1", ts_ms=ts)
    seed_swap(tmp_db, "OLD", "T1", ts_ms=ts - 60_000)
    seed_swap(tmp_db, "OLD", "T2", ts_ms=ts - 60_000)  # two tokens beats one, whatever the clock says
    q = S.gather_queue(tmp_db, Chain.SOL, limit=10, now=ts)
    assert [r["wallet"] for r in q.rows] == ["OLD", "NEW"]
    seed_swap(tmp_db, "NEW", "T2", ts_ms=ts)
    q = S.gather_queue(tmp_db, Chain.SOL, limit=10, now=ts)
    assert [r["wallet"] for r in q.rows] == ["NEW", "OLD"]  # equal priority and tokens: newest first


def test_a_negative_tag_goes_to_the_back_not_out(tmp_db):
    ts = now_ms()
    seed_swap(tmp_db, "PLAIN", "T1", ts_ms=ts - 2)                 # nothing known
    seed_swap(tmp_db, "CLEAN", "T1", ts_ms=ts - 1)                 # on the feed
    seed_feed_event(tmp_db, "CLEAN", ["gmgn"])
    seed_swap(tmp_db, "BOT", "T1", ts_ms=ts)                       # on the feed, newest, labelled a bot
    seed_feed_event(tmp_db, "BOT", ["sandwich_bot"])
    q = S.gather_queue(tmp_db, Chain.SOL, limit=10, now=ts)
    assert [r["wallet"] for r in q.rows] == ["CLEAN", "PLAIN", "BOT"]
    # On the feed (+4) with a bot label (-6): behind a wallet we know nothing about, but
    # still in the queue, because the grader decides and the label is a vendor's word.
    by = {r["wallet"]: r for r in q.rows}
    assert by["BOT"]["priority"] == -2 and by["BOT"]["tags_negative"] == 1 and by["BOT"]["feed"] == 1
    assert by["CLEAN"]["priority"] == 4 and by["PLAIN"]["priority"] == 0


def test_buy_starved_wallets_are_excluded_from_the_paid_queue(tmp_db):
    from kaiba.intelligence.grade import SELL_ONLY_MIN_TRADES

    ts = now_ms()
    seed_swap(tmp_db, "TRADER", "T1", ts_ms=ts)
    # One buy and enough sells to clear the grader's floor: a settlement address.
    seed_swap(tmp_db, "ROUTER", "T1", ts_ms=ts)
    for i in range(SELL_ONLY_MIN_TRADES * 2):
        seed_swap(tmp_db, "ROUTER", f"T{i}", ts_ms=ts - i, side="sell")
    # A sell-heavy wallet UNDER the floor is a thin sample, not a verdict, and stays.
    seed_swap(tmp_db, "THIN", "T1", ts_ms=ts)
    for i in range(SELL_ONLY_MIN_TRADES - 2):
        seed_swap(tmp_db, "THIN", f"T{i}", ts_ms=ts - i, side="sell")
    q = S.gather_queue(tmp_db, Chain.SOL, limit=10, now=ts)
    assert {r["wallet"] for r in q.rows} == {"TRADER", "THIN"}
    assert q.excluded == {"buy_starved": 1} and q.candidates == 3


def test_late_buyers_and_blacklisted_wallets_carry_no_signal(tmp_db):
    ts = now_ms()
    launch = ts - 3_600_000
    seed_token(tmp_db, "GRAD", created_ms=launch, migrated_ms=ts - 60_000)
    seed_swap(tmp_db, "EARLY", "GRAD", ts_ms=launch + S.GATHER_EARLY_WINDOW_MS)       # on the edge: early
    seed_swap(tmp_db, "LATE", "GRAD", ts_ms=launch + S.GATHER_EARLY_WINDOW_MS + 1)    # one ms past: not
    seed_swap(tmp_db, "BEFORE", "GRAD", ts_ms=launch - 1)                             # before launch: not
    seed_swap(tmp_db, "BLACK", "GRAD", ts_ms=launch + 1_000)
    tmp_db.execute(
        "INSERT INTO wallets (chain, address, first_seen_ms, last_seen_ms, cohort) VALUES (?,?,?,?,?)",
        (SOL, "BLACK", ts, ts, "blacklist"),
    )
    q = S.gather_queue(tmp_db, Chain.SOL, limit=10, now=ts)
    by = {r["wallet"]: r for r in q.rows}
    assert set(by) == {"EARLY", "LATE", "BEFORE"}  # a blacklisted wallet is never paid for
    assert by["EARLY"]["early_graduated"] == 1 and by["EARLY"]["priority"] == 3
    assert by["LATE"]["early_graduated"] == 0 and by["BEFORE"]["early_graduated"] == 0
    assert [r["wallet"] for r in q.rows][0] == "EARLY"
    assert q.candidates == 3


def test_the_queue_degrades_when_a_signal_source_is_absent(tmp_db):
    ts = _seed_signal_universe(tmp_db)
    tmp_db.execute("DROP TABLE entity_members")
    tmp_db.execute("DROP TABLE events")
    q = S.gather_queue(tmp_db, Chain.SOL, limit=10, now=ts)
    assert q.signals["graded_siblings"].startswith("UNAVAILABLE")
    assert q.signals["tags"].startswith("UNAVAILABLE")
    # Feed membership falls back to swaps.source when the events are unreadable, and says so.
    assert q.signals["feed"] == "ok:2 via swaps (events unreadable)"
    assert q.signals["early_graduated"] == "ok:1"
    order = [r["wallet"] for r in q.rows]
    # Still ordered by what could be read: the three feed/early wallets tie at 4 and fall
    # to tokens then recency; SIBLING has lost its only signal and joins RECENT at the back,
    # where recency (the last tiebreak) puts the newer of the two first.
    assert order == ["EARLY", "FEED", "TAGGED", "RECENT", "SIBLING"], order
    by = {r["wallet"]: r for r in q.rows}
    assert by["SIBLING"]["priority"] == 0 and by["TAGGED"]["priority"] == 4
    assert by["TAGGED"]["tags_positive"] == 0 and by["SIBLING"]["graded_siblings"] == 0


def test_the_feed_scan_is_incremental_within_a_process(tmp_db):
    ts = now_ms()
    seed_swap(tmp_db, "A", "T1", ts_ms=ts)
    seed_swap(tmp_db, "B", "T1", ts_ms=ts - 1)
    q = S.gather_queue(tmp_db, Chain.SOL, limit=10, now=ts)
    assert q.signals["feed"] == "ok:0" and [r["wallet"] for r in q.rows] == ["A", "B"]
    key = (S._db_path(tmp_db), SOL)
    assert key in S._FEED_TAG_CACHE and S._FEED_TAG_CACHE[key][0] == 0
    # A feed event that arrives after the first scan is picked up by the next one, and the
    # cache advances to its id rather than rescanning from the start.
    seed_feed_event(tmp_db, "B", ["smart_degen"])
    last_id = int(fetch_one(tmp_db, "SELECT MAX(id) AS id FROM events")["id"])
    q = S.gather_queue(tmp_db, Chain.SOL, limit=10, now=ts)
    assert q.signals["feed"] == "ok:1" and q.signals["tags"] == "ok:1"
    assert [r["wallet"] for r in q.rows] == ["B", "A"] and q.rows[0]["priority"] == 6
    assert S._FEED_TAG_CACHE[key][0] == last_id
    # Tags accumulate: a later plain feed row does not un-tag a wallet.
    seed_feed_event(tmp_db, "B", ["gmgn"])
    q = S.gather_queue(tmp_db, Chain.SOL, limit=10, now=ts)
    assert q.rows[0]["tags_positive"] == 1 and S._FEED_TAG_CACHE[key][0] == last_id + 1


def test_the_hook_is_read_when_another_piece_provides_it(tmp_db):
    ts = now_ms()
    seed_swap(tmp_db, "A", "T1", ts_ms=ts)
    seed_swap(tmp_db, "B", "T1", ts_ms=ts - 1)
    seed_swap(tmp_db, "C", "T1", ts_ms=ts - 2)
    q = S.gather_queue(tmp_db, Chain.SOL, limit=10, now=ts)
    assert [r["wallet"] for r in q.rows] == ["A", "B", "C"] and q.signals["flagged"].startswith("UNAVAILABLE")
    tmp_db.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?, '{}', ?)",
                   (f"{S.GATHER_HOOK_KV_PREFIX}{SOL}:C", ts))
    tmp_db.execute(f"CREATE TABLE {S.GATHER_HOOK_TABLE} (chain TEXT, address TEXT)")
    tmp_db.execute(f"INSERT INTO {S.GATHER_HOOK_TABLE} VALUES (?, ?)", (SOL, "B"))
    q = S.gather_queue(tmp_db, Chain.SOL, limit=10, now=ts)
    assert [r["wallet"] for r in q.rows] == ["B", "C", "A"] and q.signals["flagged"] == "ok:2"
    assert q.rows[0]["priority"] == 3


def test_invented_weights_say_so():
    for name, spec in S.GATHER_SIGNALS.items():
        prov = str(spec["provenance"])
        assert "INVENTED" in prov or "MEASURED" in prov, name
        assert isinstance(spec["weight"], int) and isinstance(spec["cap"], int), name
    assert "INVENTED" in S.gather_credit_room.__doc__ or "INVENTED" in open(S.__file__, encoding="utf-8").read()
    for text in S.GATHER_EXCLUSIONS.values():
        assert "grade." in text  # the thresholds are the grader's, and the text says whose


def test_priority_is_pure_capped_and_signed():
    assert S.gather_priority({}) == 0
    assert S.gather_priority({"feed": 1}) == 4
    assert S.gather_priority({"early_graduated": 10}) == 9  # capped at three tokens
    assert S.gather_priority({"tokens": 40}) == 5  # one per two tokens, capped at five
    assert S.gather_priority({"tokens": 3}) == 1
    assert S.gather_priority({"tags_negative": 1, "feed": 1}) == -2
    assert S.gather_priority({"tags_negative": 5}) == -6  # capped at one label
    assert S.gather_priority({"graded_siblings": 4}) == 2
    assert S.gather_priority({"flagged": 1}) == 3


def test_attempt_marks_carry_the_signals_for_later_yield_measurement(tmp_db, fakes, monkeypatch):
    fake_ledger(monkeypatch, remaining=990_000)
    ts = _seed_signal_universe(tmp_db)
    params = {"wallets_per_run": 2, "wallets_per_day": 10, "credits_per_day": 5_000}
    out = S.job_wallet_buyers(ctx_for(tmp_db, "wallet_buyers", params))
    assert fakes.backfilled == [["TAGGED", "EARLY"]] and out["wallets"] == 2
    assert out["queue"]["signals"]["feed"] == "ok:2"
    assert out["queue"]["admitted"][0]["wallet"] == "TAGGED"
    row = fetch_one(tmp_db, "SELECT value FROM kv WHERE key = ?", (f"ops:wallet_buyers:{SOL}:TAGGED",))
    note = json.loads(row["value"])["note"]
    assert note.startswith("backfilled|signals=") and "feed:1" in note and "tags_positive:1" in note
    row = fetch_one(tmp_db, "SELECT value FROM kv WHERE key = ?", (f"ops:wallet_buyers:{SOL}:EARLY",))
    assert "early_graduated:1" in json.loads(row["value"])["note"]
    # The next run does not pay for them again, and moves down the ordering.
    out = S.job_wallet_buyers(ctx_for(tmp_db, "wallet_buyers", params))
    assert fakes.backfilled[-1] == ["FEED", "SIBLING"]
    assert S.quota_used(tmp_db, "wallet_buyers", S.utc_day(ts)) == (4, 4 * EST)


# --------------------------------------------------------------------------- free grades


def test_free_grades_cover_feed_wallets_on_chains_without_a_helius_path_and_never_sol(tmp_db, fakes, monkeypatch):
    fake_ledger(monkeypatch, remaining=990_000)
    ts = now_ms()
    for i in range(3):
        seed_swap(tmp_db, "BSC-DEEP", f"B{i}", ts_ms=ts - i, source="gmgn:smartmoney", chain=BSC)
    seed_swap(tmp_db, "BSC-THIN", "B0", ts_ms=ts, source="gmgn:kol", chain=BSC)
    seed_swap(tmp_db, "BSC-SELLER", "B0", ts_ms=ts, source="gmgn:kol", chain=BSC, side="sell")
    seed_swap(tmp_db, "SOL-FEED", "S0", ts_ms=ts, source="gmgn:smartmoney")
    seed_swap(tmp_db, "SOL-FEED", "T1", ts_ms=ts)
    params = {"wallets_per_run": 0, "wallets_per_day": 0, "free_grade_chains": "bsc, sol, nope",
              "free_grades_per_run": 10}
    out = S.job_wallet_buyers(ctx_for(tmp_db, "wallet_buyers", params))
    assert out["reason"] == "daily_quota_reached" and fakes.backfilled == []
    free = out["free_grades"]
    assert free["bsc"]["candidates"] == 2 and free["bsc"]["graded"] == 2 and free["bsc"]["credits"] == 0
    assert free["sol"] == {"reason": "sol_has_a_paid_path"} and free["nope"] == {"reason": "unknown_chain"}
    assert fakes.graded == [("BSC-DEEP", BSC), ("BSC-THIN", BSC)]  # deepest evidence first
    assert S.quota_used(tmp_db, "wallet_buyers", S.utc_day(ts)) == (0, 0)
    # Graded once, gone from the free queue; the sell-only address never entered it.
    assert S.free_grade_targets(tmp_db, Chain.BSC, limit=10) == []
    # And the SOL feed wallet is still in the paid queue, unscored.
    assert [r["wallet"] for r in S.gather_queue(tmp_db, Chain.SOL, limit=5, now=ts).rows] == ["SOL-FEED"]


def test_free_grades_are_off_unless_the_schedule_names_chains(tmp_db, fakes, monkeypatch):
    fake_ledger(monkeypatch, remaining=990_000)
    seed_swap(tmp_db, "BSC-1", "B0", ts_ms=now_ms(), source="gmgn:smartmoney", chain=BSC)
    out = S.job_wallet_buyers(ctx_for(tmp_db, "wallet_buyers", {"wallets_per_run": 1, "wallets_per_day": 1}))
    assert out["free_grades"] == {} and fakes.graded == []


# --------------------------------------------------------------------------- regrade


def test_the_regrade_job_covers_every_chain_with_scores(tmp_db, monkeypatch):
    from kaiba.intelligence import grade as GR

    ts = now_ms()
    seed_score(tmp_db, "S-OLD", grade="D", scored_at_ms=ts - 30 * 86_400_000)
    seed_score(tmp_db, "B-OLD", grade="D", scored_at_ms=ts - 30 * 86_400_000, chain=BSC)
    seed_score(tmp_db, "B-FRESH", grade="C", scored_at_ms=ts - 60_000, chain=BSC)
    seen: list[tuple[str, str]] = []

    def fake_grade(address: str, chain: Any, conn: Any, *, store: bool = True) -> SimpleNamespace:
        seen.append((address, Chain(chain).value))
        new = "B" if address == "B-OLD" else "D"
        seed_score(conn, address, grade=new, chain=Chain(chain).value)
        return SimpleNamespace(grade=new)

    monkeypatch.setattr(GR, "grade_address", fake_grade)
    out = S.job_wallet_regrade(ctx_for(tmp_db, "wallet_regrade", {"max_age_days": 7}))
    assert sorted(seen) == [("B-OLD", BSC), ("S-OLD", SOL)]
    assert out["candidates"] == 2 and out["changed"] == 1 and out["by_grade"] == {"B": 1, "D": 1}
    assert out["per_chain"]["bsc"]["changed"] == 1 and out["per_chain"]["sol"]["candidates"] == 1
    # Narrowed to one chain, and the run limit is shared across chains.
    seed_score(tmp_db, "S-OLD", grade="D", scored_at_ms=ts - 30 * 86_400_000)
    out = S.job_wallet_regrade(ctx_for(tmp_db, "wallet_regrade", {"max_age_days": 7, "chains": "bsc"}))
    assert out["candidates"] == 0 and list(out["per_chain"]) == ["bsc"]
    seed_score(tmp_db, "B-OLD", grade="D", scored_at_ms=ts - 30 * 86_400_000, chain=BSC)
    out = S.job_wallet_regrade(ctx_for(tmp_db, "wallet_regrade", {"max_age_days": 7, "wallets_per_run": 1}))
    assert out["candidates"] == 1 and "sol" not in out["per_chain"]


# --------------------------------------------------------------------------- the shipped schedule


def test_the_shipped_schedule_is_still_count_bound_until_the_operator_raises_it():
    """Documents the state this piece found: no credits_per_day, so the old ceiling holds.

    When config_change_needed lands this assertion flips; that is the point of it.
    """
    from pathlib import Path

    cfg = S.load_config(Path(S.__file__).resolve().parents[2] / "config" / "schedule.yaml")
    params = cfg.jobs["wallet_buyers"].params
    if "credits_per_day" not in params:
        implicit = int(params["wallets_per_day"]) * EST * 2
        assert implicit <= S.GATHER_DAILY_CREDIT_HARD_MAX
    else:
        assert int(params["credits_per_day"]) <= S.GATHER_DAILY_CREDIT_HARD_MAX
        monthly = int(params["credits_per_day"]) * 31
        assert monthly < S.FREE_MONTHLY_CREDITS - cfg.helius.floor_credits


def test_regrade_and_queue_leave_the_schema_alone(tmp_db):
    names = {r["name"] for r in fetch_all(tmp_db, "SELECT name FROM sqlite_master WHERE type='table'")}
    S.gather_queue(tmp_db, Chain.SOL, limit=1)
    S.free_grade_targets(tmp_db, Chain.BSC, limit=1)
    after = {r["name"] for r in fetch_all(tmp_db, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert after == names


# ----------------------------------------------- the measured best use of a credit first


def seed_tape(conn: Any, address: str, *, closed: int = 25, win: float = 0.6, realized: str = "150.5",
              model: str = S.TAPE_MODEL_VERSION, tokens: int = 6) -> None:
    conn.execute(
        "INSERT INTO wallet_scores (chain, address, score, grade, evidence_weight, archetype, "
        "model_version, scored_at_ms, closed_trades, win_rate, realized_pnl_usd, distinct_tokens) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (SOL, address, 45.0, "C", 1.0, "trader", model, now_ms(), closed, win, realized, tokens),
    )


def _seed_tape_universe(conn: Any) -> int:
    ts = now_ms()
    seed_tape(conn, "T40", closed=40)
    seed_tape(conn, "T20", closed=20)
    seed_tape(conn, "T9", closed=9)
    seed_tape(conn, "LOSER", closed=30, realized="-5")      # closes trades, loses money
    seed_tape(conn, "COINFLIP", closed=30, win=0.3)         # profitable on a few big wins only
    seed_tape(conn, "THIN", closed=5)                       # under the tape B gate's 8
    seed_tape(conn, "PAID", closed=50, realized="999", model="kaiba-wallet-v1")  # already bought
    seed_swap(conn, "RECENT", "T1", ts_ms=ts)               # an ordinary queue candidate
    return ts


def test_tape_profitable_wallets_are_bought_first_most_closed_first(tmp_db):
    # MEASURED 2026-09-29 over 431 paid sol grades: 15.0% of these became an earned B
    # (>= 20 closed trades) against 2.6% for the rest of the queue.
    ts = _seed_tape_universe(tmp_db)
    q = S.gather_queue(tmp_db, Chain.SOL, limit=10, now=ts)
    order = [r["wallet"] for r in q.rows]
    assert order[:3] == ["T40", "T20", "T9"], order
    assert "RECENT" in order[3:], "the ordinary queue still fills what the tape tier leaves"
    for excluded in ("LOSER", "COINFLIP", "THIN", "PAID"):
        assert excluded not in order, excluded
    assert "tape_profitable:1" in q.signal_tag("T40")
    assert q.signals["tape_profitable"] == "ok:3"


def test_a_full_tape_batch_skips_the_swaps_walk(tmp_db):
    # The aggregate walked every sol swap (~7M rows) and timed the job out for four days.
    ts = _seed_tape_universe(tmp_db)
    seen: list[str] = []
    tmp_db.set_trace_callback(seen.append)
    try:
        q = S.gather_queue(tmp_db, Chain.SOL, limit=3, now=ts)
    finally:
        tmp_db.set_trace_callback(None)
    assert [r["wallet"] for r in q.rows] == ["T40", "T20", "T9"]
    assert not any("GROUP BY s.wallet" in s for s in seen), "the full swaps aggregate still ran"


def test_the_tape_tier_keeps_the_queues_admission_rules(tmp_db):
    ts = _seed_tape_universe(tmp_db)
    tmp_db.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?)",
                   (f"ops:wallet_buyers:{SOL}:T40", "backfilled", ts))           # tried today
    tmp_db.execute("INSERT INTO wallets (chain, address, first_seen_ms, last_seen_ms, cohort) "
                   "VALUES (?,?,?,?,?)", (SOL, "T20", ts, ts, "blacklist"))
    q = S.gather_queue(tmp_db, Chain.SOL, limit=10, now=ts)
    order = [r["wallet"] for r in q.rows]
    assert "T40" not in order and "T20" not in order
    assert order[0] == "T9"
