"""Real disposable SQLite controls for the loss-review CLI read boundary."""
from __future__ import annotations

import json
import random
import sqlite3
import time
from pathlib import Path

import pytest

from kaiba.learning import autolearn_io as source
from kaiba.learning import loss_review as review

CUTOFF = 2_000_000


def _path(conn):
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


def _seed(conn):
    conn.execute("INSERT INTO positions(position_id,chain,token,lane,mode,opened_ms,closed_ms,qty,"
        "qty_total,cost_native,proceeds_native,realized_native,entry_price_usd,peak_price_usd) "
        "VALUES('loss','sol','fixture-token','sm-trenches','live',1000000,1001000,'0','100',"
        "'100','75','-25','1','2')")
    conn.execute("INSERT INTO signals(signal_id,lane,chain,token,strength,created_ms,wallets_json,entities_json) "
        "VALUES('signal','sm-trenches','sol','fixture-token',0.7,999000,'[\"wallet\"]','[\"addr:fixture\"]')")
    conn.execute("INSERT INTO decisions(decision_id,ts_ms,lane,mode,chain,token,action,thesis,confidence,signals_json) "
        "VALUES('decision',999500,'sm-trenches','live','sol','fixture-token','enter','fixture',0.7,'[\"signal\"]')")
    conn.execute("INSERT INTO trades(trade_id,position_id,decision_id,lane,mode,chain,token,opened_ms,closed_ms,"
        "hold_s,cost_native,proceeds_native,pnl_native,pnl_pct,mfe_pct,mae_pct,slippage_bps) "
        "VALUES('trade','loss','decision','sm-trenches','live','sol','fixture-token',1000000,1001000,"
        "1,'100','75','-25',-25,0,-25,17)")
    conn.commit()


def _track_connections(monkeypatch, *, after_close=None):
    actual_connect = sqlite3.connect
    connections = []

    class Tracked(sqlite3.Connection):
        closed = False

        def close(self):
            super().close()
            self.closed = True
            if after_close is not None:
                after_close()

    def connect(*args, **kwargs):
        conn = actual_connect(*args, **kwargs, factory=Tracked)
        connections.append(conn)
        return conn

    monkeypatch.setattr(source.sqlite3, "connect", connect)
    return connections


def _old_output(tmp_path):
    output = tmp_path / "report"
    output.mkdir()
    for name, data in {"manifest.json": b'{"fingerprints":{}}', "reviews.json": b"old report\n",
            "reviews.csv": b"old csv\n", "unrelated.txt": b"preserve\n"}.items():
        (output / name).write_bytes(data)
    return output, _output_bytes(output)


def _output_bytes(output):
    return {p.name: p.read_bytes() for p in output.iterdir()}


def _main(database, output):
    return review.main(["--db", str(database), "--output", str(output)])


CURRENT = {"state": "current", "pages_this_run": 0, "event_ids_remaining": 0}


def _assert_deferred(capsys, reason, *, structural=False, retained=None, scan=None):
    raw = capsys.readouterr().out
    assert json.loads(raw) == {
        "status": "source_deferred", "reason": reason, "structural": structural, "outputs_written": False,
        "retained_evidence": retained or {"state": "never_completed_or_unreadable"},
        "incident_scan": scan or {"state": "not_started"}, "retry_after_s": 60,
        "retry_guidance": "Wait at least 60 seconds; recheck WAL before each query."}
    # No source exception text, identifier or filesystem path enters the status.
    assert "/" not in raw.replace("recheck WAL", "") and "\\" not in raw and "fixture" not in raw


def test_quiet_database_preserves_connection_api_reviews_and_fingerprints(tmp_db):
    _seed(tmp_db)
    tmp_db.execute("BEGIN")
    baseline = review.build_reviews(tmp_db, cutoff_ms=CUTOFF)
    assert tmp_db.in_transaction  # The legacy API still leaves ownership with its caller.
    tmp_db.rollback()
    reader = review._ReviewReader(_path(tmp_db))
    actual = review.build_reviews(reader, cutoff_ms=CUTOFF)
    assert actual == baseline and actual["reviews"][0]["source_signals"]
    assert actual["reviews"][0]["evidence_fingerprint"] == baseline["reviews"][0]["evidence_fingerprint"]
    assert actual["reviews"][0]["causal_verdict"] == "UNPROVEN"
    assert reader.query_seconds == 3 and reader.max_rows == 10_000
    assert reader.queries == len(reader.receipts) == 7


def test_cli_scope_is_per_query_and_does_not_change_review_fingerprints(tmp_db, tmp_path, monkeypatch):
    _seed(tmp_db)
    expected = review.build_reviews(tmp_db, cutoff_ms=CUTOFF)
    output = tmp_path / "report"
    monkeypatch.setattr(review.time, "time", lambda: CUTOFF / 1000)
    connections = _track_connections(monkeypatch)
    monkeypatch.setattr(review.time, "sleep", lambda *args: pytest.fail("a guarded report must never sleep"))
    assert _main(_path(tmp_db), output) == 0
    actual = json.loads((output / "reviews.json").read_bytes())
    scope = actual.pop("capture_scope")
    assert actual == expected
    assert scope["basis"] == "per_query_observations" and scope["single_snapshot"] is False
    assert scope["query_seconds"] == 3 and scope["max_rows_per_query"] == 10_000
    assert scope["capture_budget_seconds"] == 30
    assert scope["query_count"] == len(scope["query_receipts"]) == len(connections) == 7
    assert all(c.closed for c in connections)
    manifest = json.loads((output / "manifest.json").read_bytes())
    assert manifest["capture_scope"] == scope
    assert manifest["fingerprints"] == {r["position_id"]: r["evidence_fingerprint"] for r in expected["reviews"]}


def test_each_real_query_closes_before_wal_checkpoint_and_checks_wal_again(tmp_db, monkeypatch):
    _seed(tmp_db)
    path = _path(tmp_db)
    tmp_db.execute("PRAGMA busy_timeout=0")
    tmp_db.execute("CREATE TABLE checkpoint_control(n)")
    # Positive sensitivity control: the former report-wide read snapshot really
    # does prevent the writer's checkpoint until that snapshot is closed.
    held = sqlite3.connect(path)
    held.execute("BEGIN")
    held.execute("SELECT * FROM positions").fetchall()
    tmp_db.execute("INSERT INTO checkpoint_control VALUES(0)")
    assert tmp_db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 1
    held.close()
    assert tuple(tmp_db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()) == (0, 0, 0)
    checkpoints, guards = [], []
    actual_wal_bytes = review._ReviewReader._wal_bytes
    def wal_bytes(self):
        guards.append(actual_wal_bytes(self))
        return guards[-1]
    def checkpoint():
        tmp_db.execute("INSERT INTO checkpoint_control VALUES(?)", (len(checkpoints),))
        checkpoints.append(tuple(tmp_db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()))
    monkeypatch.setattr(review._ReviewReader, "_wal_bytes", wal_bytes)
    connections = _track_connections(monkeypatch, after_close=checkpoint)
    reader = review._ReviewReader(path)
    assert review.build_reviews(reader, cutoff_ms=CUTOFF)["counts"] == {"live": 1}
    assert len(guards) == len(connections) == len(checkpoints) == 7
    assert all(c.closed for c in connections) and checkpoints == [(0, 0, 0)] * 7


def test_wal_refusal_happens_before_connect_and_preserves_all_old_outputs(tmp_db, tmp_path, monkeypatch, capsys):
    path = _path(tmp_db)
    output, before = _old_output(tmp_path)
    monkeypatch.setattr(review._ReviewReader, "_wal_bytes", lambda self: source.WAL_LIMIT + 1)
    calls = []
    def refused(*args, **kwargs):
        calls.append(1)
        pytest.fail("source opened before WAL admission")
    monkeypatch.setattr(source.sqlite3, "connect", refused)
    assert _main(path, output) == 2
    assert calls == [] and _output_bytes(output) == before
    _assert_deferred(capsys, "wal_over_limit")


def test_later_actual_sql_error_closes_connections_and_leaves_outputs_unchanged(tmp_db, tmp_path, monkeypatch, capsys):
    _seed(tmp_db)
    path = _path(tmp_db)
    output, before = _old_output(tmp_path)
    closed = []
    def break_a_later_query():
        closed.append(1)
        if len(closed) == 1:
            tmp_db.execute("DROP TABLE trades")
    connections = _track_connections(monkeypatch, after_close=break_a_later_query)
    assert _main(path, output) == 2
    assert len(connections) == 3 and all(c.closed for c in connections)  # incident top, positions, trades
    assert _output_bytes(output) == before
    _assert_deferred(capsys, "source_query_unavailable", scan=CURRENT)


def test_expensive_actual_statement_hits_inherited_deadline_and_closes(tmp_db, monkeypatch):
    path = _path(tmp_db)
    connections = _track_connections(monkeypatch)
    value = {"s": 0.0}
    def elapsed():
        value["s"] += .5
        return value["s"]
    reader = review._ReviewReader(path, clock=elapsed)
    started = time.monotonic()
    # Bounded real recursive work: without the progress/deadline guard it returns
    # a number, rather than raising. The clock advances only when actually read.
    with pytest.raises(source.SourceDeferred, match="source_query_unavailable"):
        review._rows(reader, "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000) "
            "SELECT sum(x) FROM n")
    assert time.monotonic() - started < 3
    assert reader.receipts[0]["duration_s"] >= reader.query_seconds == 3
    assert len(connections) == 1 and connections[0].closed
    reader.clock = time.monotonic
    reader.deadline = time.monotonic() + review.CAPTURE_SECONDS
    assert review._rows(reader, "SELECT 17 AS positive_control") == [{"positive_control": 17}]
    assert all(c.closed for c in connections)


def test_exact_row_cap_succeeds_but_overflow_never_returns_a_partial_sample(tmp_db, monkeypatch):
    connections = _track_connections(monkeypatch)
    reader = review._ReviewReader(_path(tmp_db))
    sql = "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<?) SELECT x FROM n"
    rows = review._rows(reader, sql, (10_000,))
    assert len(rows) == 10_000 and rows[-1] == {"x": 10_000}
    with pytest.raises(source.SourceDeferred, match="source_row_cap; no partial sample accepted"):
        review._rows(reader, sql, (10_001,))
    assert len(connections) == 2 and all(c.closed for c in connections)


def _many_positions(tmp_db):
    tmp_db.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<10000) "
        "INSERT INTO positions(position_id,chain,token,lane,mode,opened_ms,closed_ms,realized_native) "
        "SELECT 'bounded-'||x,'sol','fixture-token','sm-trenches','live',1000000,1001000,'-1' FROM n")


def test_a_source_larger_than_the_row_cap_is_read_in_guarded_pages(tmp_db, tmp_path, monkeypatch, capsys):
    # The box's position_orders x orders join had 19,692 rows: one unpaged query deferred every run.
    _seed(tmp_db)
    _many_positions(tmp_db)
    output, _ = _old_output(tmp_path)
    connections = _track_connections(monkeypatch)
    assert _main(_path(tmp_db), output) == 0
    reviews = json.loads((output / "reviews.json").read_bytes())
    assert reviews["counts"]["live"] == 10_001  # every losing position, none sampled away
    scope = reviews["capture_scope"]
    assert scope["max_rows_per_query"] == 10_000 and scope["query_count"] >= 12  # positions took six pages
    assert all(c.closed for c in connections)


def test_a_refusal_on_a_later_page_defers_everything_and_keeps_old_outputs(tmp_db, tmp_path, monkeypatch, capsys):
    _seed(tmp_db)
    _many_positions(tmp_db)
    output, before = _old_output(tmp_path)
    sizes = iter([0, 0, source.WAL_LIMIT + 1])  # incident top and positions page 1 admitted, page 2 refused
    monkeypatch.setattr(review._ReviewReader, "_wal_bytes", lambda self: next(sizes, 0))
    assert _main(_path(tmp_db), output) == 2
    assert _output_bytes(output) == before  # no partial publication
    _assert_deferred(capsys, "wal_over_limit", scan=CURRENT)


def test_a_deferral_reports_the_age_of_the_evidence_it_leaves_in_place(tmp_db, tmp_path, monkeypatch, capsys):
    output, _ = _old_output(tmp_path)
    # The real manifest counts losses per mode (box 2026-10-19: {"shadow": 2525, "live": 353}).
    (output / "manifest.json").write_text(json.dumps({"cutoff_ms": CUTOFF - 7_200_000, "counts": {"shadow": 9, "live": 3}}))
    monkeypatch.setattr(review._ReviewReader, "_wal_bytes", lambda self: source.WAL_LIMIT + 1)
    monkeypatch.setattr(review.time, "time", lambda: CUTOFF / 1000)
    assert _main(_path(tmp_db), output) == 2
    _assert_deferred(capsys, "wal_over_limit", retained={
        "state": "stale", "last_completed_cutoff_ms": CUTOFF - 7_200_000, "age_s": 7200, "reviewed_losses": 12})


def test_deferral_reasons_are_fixed_categories():
    assert review.deferral_reason(source.SourceDeferred("source_row_cap; no partial sample accepted")) == "source_row_cap"
    assert review.deferral_reason(source.SourceDeferred("wal_over_limit:1600000000; retry")) == "wal_over_limit"
    assert review.deferral_reason(source.SourceDeferred("incident_scan_in_progress")) == "incident_scan_in_progress"
    assert review.deferral_reason(source.SourceDeferred("something new")) == "other"
    assert review.STRUCTURAL_DEFERRALS == {"source_row_cap"}  # catching up is progress, not structural


def test_total_capture_budget_refuses_after_closed_query_before_publication(tmp_db, tmp_path, monkeypatch, capsys):
    _seed(tmp_db)
    output, before = _old_output(tmp_path)
    clock = {"s": 0.0}
    actual_reader = review._ReviewReader
    readers = []
    def reader(path):
        result = actual_reader(path, clock=lambda: clock["s"])
        readers.append(result)
        return result
    monkeypatch.setattr(review, "_ReviewReader", reader)
    connections = _track_connections(monkeypatch,
        after_close=lambda: clock.update(s=review.CAPTURE_SECONDS + 1))
    assert _main(_path(tmp_db), output) == 2
    assert readers[0].queries == 1 and len(connections) == 1 and connections[0].closed
    assert _output_bytes(output) == before
    _assert_deferred(capsys, "loss_review_capture_budget")


_CONNECT = sqlite3.connect  # captured at import: a second _trace in one test must not wrap the first


def _trace(monkeypatch, *, refuse=lambda log: False):
    """An ordered log of WAL checks, statements and closes across every guarded query."""
    log = []

    class Traced(sqlite3.Connection):
        closed = False

        def execute(self, sql, *args):
            if not sql.startswith("PRAGMA"):
                log.append(("sql", sql))
            return super().execute(sql, *args)

        def close(self):
            super().close()
            self.closed = True
            log.append(("close", None))

    def wal_bytes(self):
        log.append(("wal", None))
        return source.WAL_LIMIT + 1 if refuse(log) else 0

    monkeypatch.setattr(source.sqlite3, "connect", lambda *a, **k: _CONNECT(*a, **k, factory=Traced))
    monkeypatch.setattr(review._ReviewReader, "_wal_bytes", wal_bytes)
    return log


JOIN = "FROM position_orders po JOIN orders o"


def _many_links(tmp_db, n=10_500):
    """n position_orders x orders rows over two losses and a winner, inserted out of time order."""
    tmp_db.execute("INSERT INTO positions(position_id,chain,token,lane,mode,opened_ms,closed_ms,realized_native) "
        "VALUES('loss-b','sol','fixture-token','sm-trenches','live',1000000,1001000,'-3'),"
        "('win','sol','fixture-token','sm-trenches','live',1000000,1001000,'5')")
    rng = random.Random(20261102)
    links = [(("loss", "loss-b", "win")[x % 3], f"ord-{rng.randrange(10**9):09d}-{x}", 1_000_100 + rng.randrange(800))
             for x in range(n)]  # 800 distinct times for 10,500 rows: order_id breaks the ties
    rng.shuffle(links)  # rowid order (the page key) is not time order
    tmp_db.executemany("INSERT INTO orders(order_id,chain,token,side,lane,mode,input_token,output_token,amount_in,"
        "min_out,slippage_bps,state,provider,created_ms,updated_ms) "
        "VALUES(?,'sol','fixture-token','sell','sm-trenches','live','in','out','1','0',50,'filled','fixture',?,?)",
        [(oid, ts, ts) for _, oid, ts in links])
    tmp_db.executemany("INSERT INTO position_orders(position_id,order_id,side,ts_ms) VALUES(?,?,'sell',?)", links)
    tmp_db.commit()
    return {pid: [oid for _, oid, _ in sorted((x for x in links if x[0] == pid), key=lambda x: (x[2], x[1]))]
            for pid in ("loss", "loss-b", "win")}


def test_a_join_larger_than_the_row_cap_keeps_every_linked_order_in_time_order(tmp_db, tmp_path, monkeypatch, capsys):
    # The exact box mechanism: position_orders x orders had 19,692 rows against a 10,000-row cap.
    _seed(tmp_db)
    expected = _many_links(tmp_db)
    output = tmp_path / "report"
    log = _trace(monkeypatch)
    assert _main(_path(tmp_db), output) == 0
    reviews = {r["position_id"]: r for r in json.loads((output / "reviews.json").read_bytes())["reviews"]}
    assert sorted(reviews) == ["loss", "loss-b"]  # the winner's 3,500 orders were read, never reviewed
    for pid in ("loss", "loss-b"):
        assert reviews[pid]["linked_order_ids"] == expected[pid]  # every order, (ts_ms, order_id) order
        assert len(expected[pid]) == 3_500
    joins = [sql for kind, sql in log if kind == "sql" and JOIN in sql]
    assert review.PAGE_ROWS == 2_000 and len(joins) == 6  # 5 full keyset pages + 500 rows
    assert "> (?)" not in joins[0] and all("> (?)" in sql for sql in joins[1:])
    # Every query, each join page included: WAL checked first, its own connection closed after.
    events = [kind for kind, _ in log]
    assert events == ["wal", "sql", "close"] * (len(events) // 3)


def test_a_refused_second_join_page_leaves_the_previous_outputs_byte_identical(tmp_db, tmp_path, monkeypatch, capsys):
    _seed(tmp_db)
    _many_links(tmp_db)
    output = tmp_path / "report"
    assert _main(_path(tmp_db), output) == 0  # a real completed capture to protect
    capsys.readouterr()
    before = _output_bytes(output)
    tmp_db.execute("INSERT INTO orders(order_id,chain,token,side,lane,mode,input_token,output_token,amount_in,"
        "min_out,slippage_bps,state,provider,created_ms,updated_ms) "
        "VALUES('ord-new','sol','fixture-token','buy','sm-trenches','live','in','out','1','0',50,'filled','fixture',1,1)")
    tmp_db.execute("INSERT INTO position_orders VALUES('loss','ord-new','buy',1000050)")
    tmp_db.commit()  # a rerun would change the loss's evidence, so a leak would show
    log = _trace(monkeypatch, refuse=lambda log: sum(JOIN in str(sql) for _, sql in log) == 1)
    assert _main(_path(tmp_db), output) == 2
    assert sum(JOIN in str(sql) for _, sql in log) == 1 and log[-1] == ("wal", None)
    assert [kind for kind, _ in log[:-1]] == ["wal", "sql", "close"] * ((len(log) - 1) // 3)
    assert _output_bytes(output) == before
    _assert_deferred(capsys, "wal_over_limit", retained={"state": "stale", "last_completed_cutoff_ms": json.loads(
        before["manifest.json"])["cutoff_ms"], "age_s": 0, "reviewed_losses": 2}, scan=CURRENT)


def test_a_trades_source_larger_than_the_row_cap_is_paged(tmp_db, tmp_path, monkeypatch, capsys):
    _seed(tmp_db)
    _many_positions(tmp_db)
    tmp_db.execute("INSERT INTO trades(trade_id,position_id,lane,mode,chain,token,opened_ms,closed_ms,hold_s,"
        "cost_native,proceeds_native,pnl_native,pnl_pct) SELECT 'trade-'||position_id,position_id,lane,mode,chain,"
        "token,opened_ms,closed_ms,1,'1','0','-1',-100 FROM positions WHERE position_id LIKE 'bounded-%'")
    tmp_db.commit()
    output = tmp_path / "report"
    log = _trace(monkeypatch)
    assert _main(_path(tmp_db), output) == 0
    reviews = json.loads((output / "reviews.json").read_bytes())["reviews"]
    assert len(reviews) == 10_001
    assert all(r["canonical_trade_ids"] == ["trade" if r["position_id"] == "loss" else "trade-" + r["position_id"]]
               for r in reviews)  # 10,001 trades: none lost or duplicated at the page boundary
    assert sum("FROM trades" in str(sql) for _, sql in log) == 6  # 10,001 rows in 2,000-row pages


def _watchdog(tmp_db, rows):
    """(kind, payload) rows as events at increasing ids, ts 1,000,500."""
    tmp_db.executemany("INSERT INTO events(ts_ms,kind,payload) VALUES(1000500,?,?)",
                       [(k, p if isinstance(p, str) else json.dumps(p)) for k, p in rows])
    tmp_db.commit()


INCIDENT_ROWS = [
    ("system", {"service": "watchdog", "event": "exit_failed", "position_id": "loss", "detail": "route refused"}),
    ("system", {"service": "heartbeat", "event": "exit_failed", "position_id": "loss"}),
    ("scan.tier1", {"service": "watchdog", "event": "exit_failed", "position_id": "loss"}),
    ("system", "{not json"),
    ("system", {"service": "watchdog", "event": "protection_blind_timeout", "position_id": "elsewhere"}),
    ("system", {"service": "watchdog", "event": "protection_blind_timeout", "position_id": "loss", "reason": "blind"}),
    ("system", {"service": "watchdog", "event": "exit_failed", "position_id": ["unhashable"]}),
]


def test_ledger_incidents_equal_the_single_events_query(tmp_db, tmp_path, monkeypatch):
    _seed(tmp_db)
    _watchdog(tmp_db, INCIDENT_ROWS)
    monkeypatch.setattr(review.time, "time", lambda: CUTOFF / 1000)
    expected = review.build_reviews(tmp_db, cutoff_ms=CUTOFF)  # the original single query
    loss = expected["reviews"][0]
    assert [e["event"] for e in loss["incident_evidence"]] == ["exit_failed", "protection_blind_timeout"]
    assert {"exit_failure_observed_during_position", "protection_blind_timeout_observed"} <= set(loss["observed_flags"])
    output = tmp_path / "report"
    assert _main(_path(tmp_db), output) == 0
    actual = json.loads((output / "reviews.json").read_bytes())
    assert actual.pop("capture_scope")["incident_basis"] == "ledger_by_event_id_watermark"
    assert actual == expected  # same reviews, same fingerprints
    ledger = json.loads(review.incident_ledger_path(output).read_bytes())
    assert ledger["scanned_through_id"] == 7 and sorted(ledger["incidents"]) == ["elsewhere", "loss"]
    assert not review.incident_ledger_path(output).is_relative_to(output)  # never a published output


def test_the_ledger_catches_up_one_bounded_page_a_run_and_publishes_only_when_current(tmp_db, tmp_path, monkeypatch, capsys):
    _seed(tmp_db)
    _watchdog(tmp_db, INCIDENT_ROWS)
    monkeypatch.setattr(review, "INCIDENT_ID_PAGE", 2)
    monkeypatch.setattr(review, "INCIDENT_SCAN_SECONDS", 0)  # exactly one page a run
    monkeypatch.setattr(review.time, "time", lambda: CUTOFF / 1000)
    output, before = _old_output(tmp_path)
    ledger = review.incident_ledger_path(output)
    watermarks = []
    for remaining in (5, 3, 1):
        assert _main(_path(tmp_db), output) == 2
        _assert_deferred(capsys, "incident_scan_in_progress",
                         scan={"state": "catching_up", "pages_this_run": 1, "event_ids_remaining": remaining})
        assert _output_bytes(output) == before  # nothing published while behind
        watermarks.append(json.loads(ledger.read_bytes())["scanned_through_id"])
    assert watermarks == [2, 4, 6]
    log = _trace(monkeypatch)
    assert _main(_path(tmp_db), output) == 0
    pages = [sql for kind, sql in log if kind == "sql" and "id>? AND id<=?" in sql]
    assert len(pages) == 1  # the fourth run read only ids 7..7
    actual = json.loads((output / "reviews.json").read_bytes())
    actual.pop("capture_scope")
    assert actual == review.build_reviews(tmp_db, cutoff_ms=CUTOFF)
    stored = json.loads(ledger.read_bytes())["incidents"]["loss"]
    assert [e["event_id"] for e in stored] == [1, 6]  # each event once across four runs


def test_a_refused_incident_page_is_not_counted_and_is_read_again(tmp_db, tmp_path, monkeypatch, capsys):
    _seed(tmp_db)
    _watchdog(tmp_db, INCIDENT_ROWS)
    monkeypatch.setattr(review, "INCIDENT_ID_PAGE", 4)
    monkeypatch.setattr(review.time, "time", lambda: CUTOFF / 1000)
    output, before = _old_output(tmp_path)
    _trace(monkeypatch, refuse=lambda log: sum("id>? AND id<=?" in str(sql) for _, sql in log) == 1)
    assert _main(_path(tmp_db), output) == 2  # page ids 1..4 read, page 5..7 refused
    _assert_deferred(capsys, "wal_over_limit", scan={"state": "catching_up", "pages_this_run": 1, "event_ids_remaining": 3})
    ledger = json.loads(review.incident_ledger_path(output).read_bytes())
    assert ledger["scanned_through_id"] == 4 and sorted(ledger["incidents"]) == ["loss"]
    assert _output_bytes(output) == before
    _trace(monkeypatch)
    assert _main(_path(tmp_db), output) == 0
    ledger = json.loads(review.incident_ledger_path(output).read_bytes())
    assert ledger["scanned_through_id"] == 7
    assert [e["event_id"] for e in ledger["incidents"]["loss"]] == [1, 6]
    assert [e["event_id"] for e in ledger["incidents"]["elsewhere"]] == [5]


@pytest.mark.parametrize("stored", [b"{not json", b'{"schema":"other","scanned_through_id":3,"incidents":{}}',
                                    b'{"schema":"loss-review-incidents/1","scanned_through_id":99,"incidents":{"loss":[{"event_id":98}]}}'])
def test_an_unreadable_foreign_or_ahead_ledger_is_rebuilt_from_the_start(tmp_db, tmp_path, monkeypatch, stored):
    _seed(tmp_db)
    _watchdog(tmp_db, INCIDENT_ROWS)
    monkeypatch.setattr(review.time, "time", lambda: CUTOFF / 1000)
    output = tmp_path / "report"
    review.incident_ledger_path(output).write_bytes(stored)
    assert _main(_path(tmp_db), output) == 0
    actual = json.loads((output / "reviews.json").read_bytes())
    actual.pop("capture_scope")
    assert actual == review.build_reviews(tmp_db, cutoff_ms=CUTOFF)
    assert json.loads(review.incident_ledger_path(output).read_bytes())["scanned_through_id"] == 7
