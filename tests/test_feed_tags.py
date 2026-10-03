"""wallet_feed_tags: the rollup that lets old wallet.trade events be deleted.

What these tests protect, in the order a regression would cost money or data:

* every reader -- grade's provider tags, discover's cohort view, naming's facts, the gather
  queue's feed signals -- answers EXACTLY the same before the switch (events), after it
  (table), and after every wallet.trade event has been deleted (table only);
* the writer rolls an event up in the event's own transaction, once, and never raises;
* the backfill covers exactly the ids below the writer's first one, once, resumably, and
  keeps the OLDEST wallet name;
* parity catches a table that disagrees with the events, and only an exact pass switches
  the readers;
* retention deletes nothing until the gate opens, and then only old wallet.trade events.
"""

from __future__ import annotations

import json
import random
import sqlite3
from typing import Any

import pytest

from kaiba.core.db import fetch_all, jdump
from kaiba.core.schemas import Chain, now_ms
from kaiba.ingest import gmgn_feeds
from kaiba.intelligence import discover, feed_tags, grade, naming
from kaiba.ops import retention
from kaiba.ops import scheduler as S

W1 = "Bnd5oBSWpPpXoaWTckgXTVx9TCKkX5GyVqxcrwBzUYsB"
W2 = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
W3 = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
W5 = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
EVM = "0x68eee5c2fe8883a63cd9e5f0e71a3116fb728b3a"
BSC = "0x1111111111111111111111111111111111111111"
TOKEN = "So11111111111111111111111111111111111111112"
DAY = 86_400_000


# ---------------------------------------------------------------- seeding


def legacy_feed_event(conn: Any, chain: str, wallet: str, tags: list[str], *, feed: str = "smartmoney",
                      tx: str, ts: int, name: str | None = None, side: str = "buy") -> None:
    """What the OLD write_swap left behind: a swaps row and a wallet.trade event, no rollup."""
    conn.execute(
        "INSERT OR IGNORE INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, source) "
        "VALUES (?,?,?,?,?,?,?,?)", (chain, tx, ts, wallet, TOKEN, side, "1.5", f"gmgn:{feed}"),
    )
    payload = {"chain": chain, "tx": tx, "ts_ms": ts, "wallet": wallet, "token": TOKEN, "side": side,
               "source": f"gmgn:{feed}", "feed": feed, "wallet_name": name, "tags": tags}
    conn.execute(
        "INSERT INTO events (ts_ms, kind, level, chain, subject, payload, dedupe_key) VALUES (?,?,?,?,?,?,?)",
        (ts, "wallet.trade", "info", chain, wallet, jdump(payload), f"wallet.trade:gmgn:{feed}:{chain}:{tx}:{wallet}:{side}"),
    )


def plain_event(conn: Any, chain: str, wallet: str, payload: dict[str, Any], ts: int) -> None:
    conn.execute(
        "INSERT INTO events (ts_ms, kind, level, chain, subject, payload) VALUES (?,?,?,?,?,?)",
        (ts, "wallet.trade", "info", chain, wallet, jdump(payload)),
    )


def pump_swap(conn: Any, wallet: str, i: int, ts: int) -> None:
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, source) VALUES (?,?,?,?,?,?,?,?)",
        ("sol", f"pump-{wallet[:5]}-{i}", ts, wallet, f"TOK{i % 3}", "buy" if i % 2 == 0 else "sell", "10",
         "pumpfun:trades"),
    )


def feed_row(chain: Chain, wallet: str, tx: str, tags: list[str], *, feed: str = "smartmoney",
             name: str | None = None) -> gmgn_feeds.SwapRow:
    return gmgn_feeds.SwapRow(chain=chain, tx=tx, ts_ms=now_ms(), wallet=wallet, token=TOKEN, side="buy",
                              amount_token="2", feed=feed, tags=tags, wallet_name=name)


def seed_universe(conn: Any) -> None:
    """Old events (before the writer existed), then the writer's own, plus the noise that
    must never reach the table."""
    old = now_ms() - 10 * DAY
    legacy_feed_event(conn, "sol", W1, ["smart_degen", "gmgn"], tx="a1", ts=old)
    legacy_feed_event(conn, "sol", W1, ["smart_degen", "axiom"], tx="a2", ts=old + 1_000)
    legacy_feed_event(conn, "sol", W1, ["kol"], feed="kol", tx="a3", ts=old + 2_000)
    legacy_feed_event(conn, "sol", W2, ["launchpad_smart"], tx="b1", ts=old + 3_000, name="Alpha")
    legacy_feed_event(conn, "sol", W2, ["launchpad_smart", "fomo"], tx="b2", ts=old + 4_000, name="Beta")
    legacy_feed_event(conn, "robinhood", EVM, ["app_smart_money"], tx="r1", ts=old + 5_000)
    # robinhood listener trades and a tracker detection: wallet.trade, nothing to roll up
    plain_event(conn, "robinhood", EVM, {"chain": "robinhood", "wallet": EVM, "side": "buy", "source": "robinhood"},
                old + 6_000)
    plain_event(conn, "sol", W1, {"tracker": "t1", "observation": True, "wallet": W1, "side": "buy"}, old + 7_000)
    # tags on a non-feed source: grade reads it, the feed readers must not
    plain_event(conn, "sol", W5, {"wallet": W5, "source": "helius_webhook", "tags": ["dex_bot"]}, old + 8_000)
    for i, w in enumerate((W1, W2, W3, W5)):
        for j in range(4):
            pump_swap(conn, w, 10 * i + j, old + 9_000 + j)
    # the live writer from here on
    gmgn_feeds.write_swap(conn, feed_row(Chain.SOL, W1, "a4", ["smart_degen", "wash_trader"]))
    gmgn_feeds.write_swap(conn, feed_row(Chain.SOL, W3, "c1", ["sandwich_bot"], feed="kol"))
    gmgn_feeds.write_swap(conn, feed_row(Chain.BSC, BSC, "d1", ["smart_degen"]))
    gmgn_feeds.write_swap(conn, feed_row(Chain.SOL, W2, "b3", ["launchpad_smart"], name="Gamma"))


def all_events(conn: Any) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in conn.execute(
        "SELECT ts_ms, chain, subject, payload FROM events WHERE kind = 'wallet.trade' ORDER BY id")]


def table(conn: Any) -> dict[Any, feed_tags.TagRow]:
    return {r.key: r for r in feed_tags.chain_rows(conn, None)}


def finish_backfill(conn: Any) -> dict[str, Any]:
    return feed_tags.backfill_step(conn, deadline_ms=now_ms() + 60_000, window_ids=3)


def make_ready(conn: Any, monkeypatch: Any) -> dict[str, Any]:
    monkeypatch.setattr(feed_tags, "PARITY_MIN_SAMPLE", 1)
    assert finish_backfill(conn)["complete"]
    result = feed_tags.parity_check(conn, sample=100, record=True)
    assert result["ok"], result
    assert feed_tags.table_ready(conn)
    return result


# ---------------------------------------------------------------- what an event contributes


def test_only_feed_rows_and_tagged_events_contribute():
    feed = {"source": "gmgn:smartmoney", "feed": "smartmoney", "tags": [" smart_degen ", "kol", "kol", ""],
            "wallet_name": "Ann"}
    assert feed_tags.contribution("sol", W1, jdump(feed)) == ("sol", W1, "gmgn:smartmoney", ["smart_degen", "kol"], "Ann")
    assert feed_tags.contribution("robinhood", EVM, {"source": "robinhood", "side": "buy"}) is None
    assert feed_tags.contribution("sol", W1, {"tracker": "x", "observation": True}) is None
    assert feed_tags.contribution("sol", W5, {"source": "helius_webhook", "tags": ["dex_bot"]}) == (
        "sol", W5, "helius_webhook", ["dex_bot"], None)
    assert feed_tags.contribution("sol", W1, {"feed": "kol", "tags": []}) == ("sol", W1, "gmgn:kol", [], None)
    assert feed_tags.contribution("sol", W1, "not json") is None


def test_rows_from_events_counts_events_and_keeps_the_first_name():
    rows = feed_tags.rows_from_events([
        (100, "sol", W1, {"source": "gmgn:kol", "feed": "kol", "tags": ["kol"], "wallet_name": None}),
        (200, "sol", W1, {"source": "gmgn:kol", "feed": "kol", "tags": ["kol", "fomo"], "wallet_name": "First"}),
        (300, "sol", W1, {"source": "gmgn:kol", "feed": "kol", "tags": [], "wallet_name": "Second"}),
    ])
    member = rows[("sol", W1, "", "gmgn:kol")]
    assert (member.n, member.first_ms, member.last_ms, member.wallet_name) == (3, 100, 300, "First")
    assert (rows[("sol", W1, "kol", "gmgn:kol")].n, rows[("sol", W1, "fomo", "gmgn:kol")].n) == (2, 1)
    assert rows[("sol", W1, "kol", "gmgn:kol")].wallet_name is None


# ---------------------------------------------------------------- the writer


def test_the_writer_rolls_each_new_event_up_once_in_its_own_transaction(tmp_db):
    row = feed_row(Chain.SOL, W1, "w1", ["smart_degen", "gmgn"], name="Ann")
    assert gmgn_feeds.write_swap(tmp_db, row) is True
    assert gmgn_feeds.write_swap(tmp_db, row) is False  # the same trade again: dedupe, no recount
    ev = fetch_all(tmp_db, "SELECT id, ts_ms FROM events WHERE kind = 'wallet.trade'")
    assert len(ev) == 1
    t = table(tmp_db)
    assert set(t) == {("sol", W1, "", "gmgn:smartmoney"), ("sol", W1, "smart_degen", "gmgn:smartmoney"),
                      ("sol", W1, "gmgn", "gmgn:smartmoney")}
    for r in t.values():
        assert (r.n, r.first_ms, r.last_ms) == (1, ev[0]["ts_ms"], ev[0]["ts_ms"])
    assert t[("sol", W1, "", "gmgn:smartmoney")].wallet_name == "Ann"
    assert feed_tags.writer_marker(tmp_db)["first_event_id"] == ev[0]["id"]
    # the table equals a re-derivation from the events: the parity rule, on the writer alone
    assert feed_tags.rows_from_events(all_events(tmp_db)) == t
    # a second event moves the marker nowhere and counts once more
    gmgn_feeds.write_swap(tmp_db, feed_row(Chain.SOL, W1, "w2", ["smart_degen"]))
    assert table(tmp_db)[("sol", W1, "smart_degen", "gmgn:smartmoney")].n == 2
    assert feed_tags.writer_marker(tmp_db)["first_event_id"] == ev[0]["id"]


def test_a_failed_rollup_takes_its_event_with_it_and_never_raises(tmp_db, monkeypatch):
    def boom(*_a: Any, **_k: Any) -> int:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(feed_tags, "record_event", boom)
    assert gmgn_feeds.write_swap(tmp_db, feed_row(Chain.SOL, W1, "x1", ["kol"])) is True  # the swap stands
    assert fetch_all(tmp_db, "SELECT id FROM events WHERE kind = 'wallet.trade'") == []
    assert table(tmp_db) == {}


# ---------------------------------------------------------------- backfill


def test_backfill_waits_for_the_writer(tmp_db):
    legacy_feed_event(tmp_db, "sol", W1, ["kol"], tx="o1", ts=1_000)
    assert feed_tags.backfill_step(tmp_db, deadline_ms=now_ms() + 10_000)["phase"] == "waiting_for_writer"
    assert table(tmp_db) == {} and feed_tags.backfill_state(tmp_db) == {}


def test_backfill_covers_exactly_the_ids_below_the_writer_once_and_resumes(tmp_db):
    seed_universe(tmp_db)
    ticks = {"ms": now_ms()}

    def clock() -> float:  # every reading is one millisecond later
        ticks["ms"] += 1
        return ticks["ms"] / 1000

    # A deadline three ticks away (one reading before the loop, one per loop test, one per
    # commit) allows exactly one window per call: the run has to resume from kv.
    calls = 0
    while True:
        calls += 1
        out = feed_tags.backfill_step(tmp_db, deadline_ms=ticks["ms"] + 3, window_ids=2, clock=clock)
        if out["complete"]:
            break
        assert out["windows"] <= 1
        assert calls < 50
    assert calls > 2, "the backfill must have needed several runs"
    assert table(tmp_db) == feed_tags.rows_from_events(all_events(tmp_db))
    # complete is complete: another run adds nothing
    before = table(tmp_db)
    assert feed_tags.backfill_step(tmp_db, deadline_ms=now_ms() + 10_000)["windows"] == 0
    assert table(tmp_db) == before


def test_backfill_keeps_the_oldest_wallet_name(tmp_db):
    seed_universe(tmp_db)
    finish_backfill(tmp_db)
    # Alpha (oldest event) beats Beta (older window) and Gamma (the writer's, newest).
    assert table(tmp_db)[("sol", W2, "", "gmgn:smartmoney")].wallet_name == "Alpha"


def test_backfill_refuses_a_window_another_run_already_took(tmp_db, monkeypatch):
    seed_universe(tmp_db)
    real = feed_tags.backfill_state
    state = {"n": 0}

    def moved(conn: Any) -> dict[str, Any]:
        got = real(conn)
        state["n"] += 1
        if state["n"] == 3 and got:  # the re-check inside the first window's transaction
            return {**got, "cursor": int(got["cursor"]) - 1}
        return got

    monkeypatch.setattr(feed_tags, "backfill_state", moved)
    out = feed_tags.backfill_step(tmp_db, deadline_ms=now_ms() + 10_000, window_ids=2)
    assert out["phase"] == "raced" and out["windows"] == 0


# ---------------------------------------------------------------- parity


def test_parity_is_exact_and_only_an_exact_pass_makes_the_table_ready(tmp_db, monkeypatch):
    monkeypatch.setattr(feed_tags, "PARITY_MIN_SAMPLE", 1)
    seed_universe(tmp_db)
    assert not feed_tags.table_ready(tmp_db)
    early = feed_tags.parity_check(tmp_db, sample=100, record=True)
    assert not early["ok"] and early["row_mismatches"] >= 1, "old wallets are missing before the backfill"
    assert not feed_tags.table_ready(tmp_db)

    finish_backfill(tmp_db)
    assert not feed_tags.table_ready(tmp_db), "complete is not enough: parity has to pass"
    result = feed_tags.parity_check(tmp_db, sample=100, record=True)
    assert result["ok"] and result["mode"] == "exact" and result["compared"] >= 5, result
    assert result["reader_mismatches"] == {}
    assert feed_tags.table_ready(tmp_db)

    # a table one count off is caught, and the failure is recorded -- readiness is sticky
    tmp_db.execute("UPDATE wallet_feed_tags SET n = n + 1 WHERE address = ? AND tag = 'kol'", (W1,))
    bad = feed_tags.parity_check(tmp_db, wallets=[("sol", W1)], record=True)
    assert not bad["ok"] and bad["row_mismatches"] == 1 and bad["examples"][0]["differ"]
    assert feed_tags.parity_record(tmp_db)["ok"] is False
    assert feed_tags.table_ready(tmp_db)
    gate = feed_tags.retention_gate(tmp_db, now_ms=now_ms(), parity_max_age_s=3600)
    assert gate == {"ok": False, "reason": "latest_parity_failed", "checked_ms": bad["checked_ms"]}


def test_parity_catches_a_missing_row_and_a_reader_that_disagrees(tmp_db, monkeypatch):
    monkeypatch.setattr(feed_tags, "PARITY_MIN_SAMPLE", 1)
    seed_universe(tmp_db)
    finish_backfill(tmp_db)
    tmp_db.execute("DELETE FROM wallet_feed_tags WHERE address = ? AND tag = 'fomo'", (W2,))
    result = feed_tags.parity_check(tmp_db, wallets=[("sol", W2)])
    assert not result["ok"] and result["examples"][0]["only_in_events"] == [("fomo", "gmgn:smartmoney")]
    # fomo is also one of the gather queue's negative labels, so all four readers disagree
    assert result["reader_mismatches"] == {"grade": 1, "discover": 1, "naming": 1, "gather": 1}


def test_a_short_parity_pass_neither_switches_readers_nor_opens_the_gate(tmp_db, monkeypatch):
    seed_universe(tmp_db)
    finish_backfill(tmp_db)
    monkeypatch.setattr(feed_tags, "PARITY_MIN_SAMPLE", 50)  # more than the universe holds
    result = feed_tags.parity_check(tmp_db, sample=100, record=True)
    assert result["ok"] and not result["sufficient"] and result["first_exact_ok_ms"] is None
    assert not feed_tags.table_ready(tmp_db)
    # once an exact pass has been sufficient, a later short one closes the gate but not the readers
    monkeypatch.setattr(feed_tags, "PARITY_MIN_SAMPLE", 1)
    assert feed_tags.parity_check(tmp_db, sample=100, record=True)["sufficient"]
    monkeypatch.setattr(feed_tags, "PARITY_MIN_SAMPLE", 50)
    feed_tags.parity_check(tmp_db, sample=100, record=True)
    assert feed_tags.table_ready(tmp_db)
    gate = feed_tags.retention_gate(tmp_db, now_ms=now_ms(), parity_max_age_s=3600)
    assert gate["reason"] == "latest_parity_too_small"


def test_parity_stops_at_its_deadline(tmp_db, monkeypatch):
    seed_universe(tmp_db)
    finish_backfill(tmp_db)
    t = {"s": 1000.0}

    def clock() -> float:
        t["s"] += 1.0
        return t["s"]

    result = feed_tags.parity_check(tmp_db, sample=100, deadline_ms=int(1002.5 * 1000), clock=clock)
    assert result["stopped"] == "deadline" and result["compared"] == 2 < result["sampled"]


def test_after_deletion_parity_switches_to_containment(tmp_db, monkeypatch):
    seed_universe(tmp_db)
    make_ready(tmp_db, monkeypatch)
    tmp_db.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?)",
                   (feed_tags.KV_RETENTION, json.dumps({"deleted_through_id": 3, "deleted_total": 3}), 1))
    tmp_db.execute("DELETE FROM events WHERE id <= 3")
    result = feed_tags.parity_check(tmp_db, sample=100)
    assert result["ok"] and result["mode"] == "containment"


# ---------------------------------------------------------------- every reader, across the switch


def reader_outputs(conn: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out["grade_index"] = {ch.value: {a: sorted(t) for a, t in grade.provider_tag_index(conn, ch).items()}
                          for ch in (Chain.SOL, Chain.BSC, Chain.ROBINHOOD)}
    out["grade_one"] = {w: sorted(grade.provider_tags_from_events(conn, Chain.SOL, w)) for w in (W1, W2, W3, W5)}
    tags, names, seen = discover._feed_tags(conn, Chain.SOL, [W1, W2, W3, W5])
    out["discover"] = ({a: sorted(t) for a, t in tags.items()}, names, sorted(seen))
    out["cohort"] = sorted(
        (c.address, tuple(sorted(c.gmgn_tags)), c.tags_basis.value, c.wallet_name, c.cohort_label)
        for c in discover.cohort_wallets(Chain.SOL, conn))
    facts, _ = naming.gather_facts(conn)
    out["naming"] = {k: (dict(sorted(f.gmgn_tags.items())), dict(sorted(f.gmgn_feeds.items())),
                         naming.registry_name(f)) for k, f in facts.items()}
    S._FEED_TAG_CACHE.clear()
    queue = S.gather_queue(conn, Chain.SOL, limit=50, now=now_ms())
    out["gather"] = sorted((r["wallet"], r["feed"], r["tags_positive"], r["tags_negative"]) for r in queue.rows)
    out["gather_signals"] = (queue.signals.get("feed"), queue.signals.get("tags"))
    return out


def test_every_reader_answers_the_same_from_events_from_the_table_and_after_deletion(tmp_db, monkeypatch):
    seed_universe(tmp_db)
    from_events = reader_outputs(tmp_db)
    # the fixture really exercises every reader
    assert from_events["grade_index"]["sol"][W5] == ["dex_bot"]
    assert from_events["grade_one"][W1] == ["axiom", "gmgn", "kol", "smart_degen", "wash_trader"]
    assert W5 not in from_events["discover"][2] and from_events["discover"][1] == {W2: "Alpha"}
    assert from_events["naming"][("sol", W1)][0] == {"axiom": 1, "gmgn": 1, "kol": 1, "smart_degen": 3,
                                                       "wash_trader": 1}
    assert (W1, 1, 1, 1) in from_events["gather"] and (W5, 0, 0, 0) in from_events["gather"]

    make_ready(tmp_db, monkeypatch)
    assert reader_outputs(tmp_db) == from_events

    # the point of the exercise: delete every wallet.trade event and nothing changes
    tmp_db.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?)",
                   (feed_tags.KV_RETENTION, json.dumps({"deleted_through_id": 10**9, "deleted_total": 1}), 1))
    tmp_db.execute("DELETE FROM events WHERE kind = 'wallet.trade'")
    assert reader_outputs(tmp_db) == from_events


def test_once_events_are_deleted_the_readers_never_go_back_to_them(tmp_db, monkeypatch):
    """After a delete the events are no longer a complete source, whatever kv says."""
    seed_universe(tmp_db)
    make_ready(tmp_db, monkeypatch)
    before = reader_outputs(tmp_db)
    tmp_db.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?)",
                   (feed_tags.KV_RETENTION, json.dumps({"deleted_through_id": 10**9, "deleted_total": 1}), 1))
    tmp_db.execute("DELETE FROM events WHERE kind = 'wallet.trade'")
    tmp_db.execute("DELETE FROM kv WHERE key IN (?, ?)", (feed_tags.KV_PARITY, feed_tags.KV_BACKFILL))
    assert feed_tags.table_ready(tmp_db)
    assert reader_outputs(tmp_db) == before


def test_before_the_switch_the_readers_do_not_touch_the_table(tmp_db):
    seed_universe(tmp_db)
    finish_backfill(tmp_db)  # complete, but no parity pass yet
    tmp_db.execute("UPDATE wallet_feed_tags SET tag = 'planted' WHERE tag = 'smart_degen'")
    assert "planted" not in grade.provider_tags_from_events(tmp_db, Chain.SOL, W1)
    assert "planted" not in discover._feed_tags(tmp_db, Chain.SOL, [W1])[0][W1]


# ---------------------------------------------------------------- query plans


def _plan(conn: Any, sql: str, params: tuple[Any, ...]) -> str:
    return " | ".join(str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params))


def test_the_event_reads_are_bounded_seeks_not_scans(tmp_db):
    assert "idx_events_kind" in _plan(tmp_db, feed_tags.BACKFILL_READ_SQL, (1, 100))
    plan = _plan(tmp_db, feed_tags.WALLET_EVENTS_SQL, (W1, "sol", 10))
    assert "idx_events_subj" in plan and "idx_events_kind" not in plan
    plan = _plan(tmp_db, f"SELECT count(*) FROM events WHERE {retention._WT_WHERE}", (1, 100, 5))
    assert "SCAN events" not in plan, plan
    plan = _plan(tmp_db, "SELECT chain, address, last_ms FROM wallet_feed_tags WHERE last_ms > ? AND last_ms <= ? "
                 "ORDER BY last_ms LIMIT ?", (1, 2, 3))
    assert "idx_wallet_feed_tags_last" in plan, plan


# ---------------------------------------------------------------- retention


def retention_cfg(**kw: Any) -> retention.RetentionConfig:
    base = dict(enabled=True, wallet_trade_enabled=True, wallet_trade_days=7, batch_rows=2, sleep_s=0,
                budget_s=60, sample_rows=0)
    base.update(kw)
    return retention.RetentionConfig(**base)


def test_retention_refuses_wallet_trade_until_the_gate_opens(tmp_db, monkeypatch):
    seed_universe(tmp_db)
    n0 = len(all_events(tmp_db))
    report = retention.run(tmp_db, retention_cfg(), dry_run=False, sleep=lambda s: None)
    assert report["tables"][retention.WALLET_TRADE]["refused"] == "backfill_incomplete"
    finish_backfill(tmp_db)
    report = retention.run(tmp_db, retention_cfg(), dry_run=False, sleep=lambda s: None)
    assert report["tables"][retention.WALLET_TRADE]["refused"] == "no_exact_parity_pass"
    assert len(all_events(tmp_db)) == n0
    # its own switch, under the global one
    make_ready(tmp_db, monkeypatch)
    report = retention.run(tmp_db, retention_cfg(wallet_trade_enabled=False), dry_run=False, sleep=lambda s: None)
    assert report["tables"][retention.WALLET_TRADE] == {"skipped": "wallet_trade_enabled is false"}
    report = retention.run(tmp_db, retention_cfg(enabled=False), dry_run=False, sleep=lambda s: None)
    assert report["mode"] == "dry_run" and report["tables"][retention.WALLET_TRADE]["gate"]["ok"]
    assert len(all_events(tmp_db)) == n0
    # a stale parity closes the gate again
    late = now_ms() + 3 * DAY
    report = retention.run(tmp_db, retention_cfg(), dry_run=False, now_ms=late, sleep=lambda s: None)
    assert report["tables"][retention.WALLET_TRADE]["refused"] == "parity_stale"


def test_retention_deletes_only_old_wallet_trade_events_and_records_how_far(tmp_db, monkeypatch):
    seed_universe(tmp_db)
    tmp_db.execute("INSERT INTO events (ts_ms, kind, chain, subject, payload) VALUES (?,?,?,?,?)",
                   (now_ms() - 20 * DAY, "system", "sol", "x", "{}"))  # old, but not wallet.trade
    make_ready(tmp_db, monkeypatch)
    before = reader_outputs(tmp_db)
    kinds_before = fetch_all(tmp_db, "SELECT kind, count(*) AS n FROM events GROUP BY kind ORDER BY kind")
    cutoff = now_ms() - 7 * DAY
    old_wt = tmp_db.execute("SELECT count(*) FROM events WHERE kind='wallet.trade' AND ts_ms < ?", (cutoff,)).fetchone()[0]
    new_wt = tmp_db.execute("SELECT count(*) FROM events WHERE kind='wallet.trade' AND ts_ms >= ?", (cutoff,)).fetchone()[0]
    assert old_wt == 9 and new_wt == 4

    report = retention.run(tmp_db, retention_cfg(), dry_run=False, sleep=lambda s: None)
    wt = report["tables"][retention.WALLET_TRADE]
    assert wt["deleted"] == old_wt and wt["done"] and report["max_rows_per_tx"] <= 2
    assert tmp_db.execute("SELECT count(*) FROM events WHERE kind='wallet.trade'").fetchone()[0] == new_wt
    assert tmp_db.execute("SELECT count(*) FROM events WHERE kind='system'").fetchone()[0] == \
        {r["kind"]: r["n"] for r in kinds_before}["system"]
    assert feed_tags.deleted_through(tmp_db) >= 1
    assert reader_outputs(tmp_db) == before, "the readers do not notice the delete"
    again = retention.run(tmp_db, retention_cfg(), dry_run=False, sleep=lambda s: None)
    assert again["tables"][retention.WALLET_TRADE]["deleted"] == 0


def test_the_dry_run_estimates_the_wallet_trade_backlog_without_writing(tmp_db, monkeypatch):
    seed_universe(tmp_db)
    make_ready(tmp_db, monkeypatch)
    n0 = len(all_events(tmp_db))
    report = retention.run(tmp_db, retention_cfg(), dry_run=True, rng=random.Random(1))
    est = report["tables"][retention.WALLET_TRADE]
    assert est["gate"]["ok"] and est["boundary_id"] is not None and est["rows_older_est"] > 0
    assert len(all_events(tmp_db)) == n0


# ---------------------------------------------------------------- the scheduler job


def ctx(conn: Any, params: dict[str, Any] | None = None) -> S.JobContext:
    now = now_ms()
    return S.JobContext("wallet_feed_tags", conn, params or {}, S.ScheduleConfig(), now, now + 300_000)


def test_the_job_waits_then_backfills_then_proves_parity(tmp_db, monkeypatch):
    monkeypatch.setattr(feed_tags, "PARITY_MIN_SAMPLE", 1)
    legacy_feed_event(tmp_db, "sol", W1, ["kol"], tx="o1", ts=now_ms() - DAY)
    out = S.job_wallet_feed_tags(ctx(tmp_db))
    assert out["backfill"]["phase"] == "waiting_for_writer" and out["ready"] is False

    seed_universe(tmp_db)
    out = S.job_wallet_feed_tags(ctx(tmp_db, {"window_ids": 3, "sleep_s": 0}))
    assert out["backfill"]["complete"] and out["parity"]["ok"] and out["ready"] is True
    out = S.job_wallet_feed_tags(ctx(tmp_db))
    assert out["parity"]["skipped"] == "interval"

    tmp_db.execute("UPDATE wallet_feed_tags SET n = n + 5")
    with pytest.raises(S.JobFailed) as failed:
        S.job_wallet_feed_tags(ctx(tmp_db, {"parity_interval_s": 0}))
    assert failed.value.result["parity"]["row_mismatches"] >= 1
