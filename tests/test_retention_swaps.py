"""Swaps and telemetry-events retention (kaiba/ops/retention.py), added 2026-10-13 when the
box's disk reached 98%, and the two readers it must not fool.

Each test is the failure it prevents:

* a swap older than the window is deleted wherever its id sits -- backfill inserts OLD trades
  under NEW ids, so a boundary search on the id would leave them forever;
* a token we ever held keeps its whole tape (exit_study, loss attribution read it);
* the horizon is stamped in the delete's own transaction, and only moves forward;
* ``grade.history_truncated`` stops vouching for a complete history once the tape is pruned,
  so a gutted history cannot pass the A gate;
* ``rebuild_first_buyers`` does not hand rank 1 to a later buyer of a token whose early buys
  were pruned;
* a telemetry event with a dedupe_key is never deleted -- the row IS the dedupe record, and
  losing it makes an old token look like a fresh launch;
* the emergency config never goes below the floors.
"""

from __future__ import annotations

import json

from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import Chain
from kaiba.ingest import backfill
from kaiba.intelligence import grade as G
from kaiba.ops import retention as RT
from kaiba.ops import scheduler as S

DAY = RT.DAY_MS
NOW = 1_790_000_000_000


def _cfg(**kw):
    base = {"enabled": True, "swaps_enabled": True, "events_other_enabled": True, "sleep_s": 0,
            "budget_s": 600, "batch_rows": 3}
    base.update(kw)
    return RT.RetentionConfig(**base)


def _swap(conn, token: str, age_days: float, wallet: str = "w1", side: str = "buy", chain: str = "sol") -> None:
    ts = int(NOW - age_days * DAY)
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, amount_native, source) "
        "VALUES (?,?,?,?,?,?,'1','1','t')",
        (chain, f"{token}-{wallet}-{ts}-{side}", ts, wallet, token, side),
    )


def _tokens(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT token FROM swaps ORDER BY id")]


def _hold(conn, token: str, chain: str = "sol") -> None:
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms) VALUES (?,?,?,?,?,?)",
        (f"p-{token}", chain, token, "sm-trenches", "shadow", NOW - 40 * DAY),
    )


def test_old_swaps_go_wherever_their_id_sits_and_new_ones_stay(tmp_db):
    for age in (30, 20, 1, 0.5):
        _swap(tmp_db, f"T{age}", age)
    _swap(tmp_db, "LATE_BACKFILL", 25)  # an old trade inserted last: the highest id
    _swap(tmp_db, "NEW", 0.1)
    report = RT.run(tmp_db, _cfg(), dry_run=False, now_ms=NOW)
    assert _tokens(tmp_db) == ["T1", "T0.5", "NEW"]
    assert report["tables"]["swaps"]["deleted"] == 3
    assert report["tables"]["swaps"]["pass_complete"] is True


def test_held_tokens_keep_their_whole_tape(tmp_db):
    _swap(tmp_db, "HELD", 40)
    _swap(tmp_db, "HELD", 30, side="sell")
    _swap(tmp_db, "OTHER", 40)
    _hold(tmp_db, "HELD")
    report = RT.run(tmp_db, _cfg(), dry_run=False, now_ms=NOW)
    assert _tokens(tmp_db) == ["HELD", "HELD"]
    assert report["tables"]["swaps"]["kept_held"] == 2


def test_the_horizon_is_stamped_with_the_delete_and_only_moves_forward(tmp_db):
    assert RT.swaps_horizon_ms(tmp_db) is None
    RT.run(tmp_db, _cfg(), dry_run=False, now_ms=NOW)
    assert RT.swaps_horizon_ms(tmp_db) is None, "nothing deleted: no horizon"
    _swap(tmp_db, "A", 30)
    RT.run(tmp_db, _cfg(swaps_pass_interval_s=0), dry_run=False, now_ms=NOW)
    assert RT.swaps_horizon_ms(tmp_db) == NOW - 14 * DAY
    _swap(tmp_db, "B", 30)
    RT.run(tmp_db, _cfg(swaps_days=20, swaps_pass_interval_s=0), dry_run=False, now_ms=NOW)
    assert RT.swaps_horizon_ms(tmp_db) == NOW - 14 * DAY, "a longer window must not move it back"


def test_the_walk_resumes_across_runs_and_waits_out_its_interval(tmp_db):
    for i in range(10):
        _swap(tmp_db, f"OLD{i}", 30)
    clock = iter([0.0] + [1000.0] * 10_000)  # the first budget check passes, then out of time

    first = RT.run(tmp_db, _cfg(budget_s=1), dry_run=False, now_ms=NOW,
                   clock=lambda: next(clock), deadline_ms=500_000)
    assert first["tables"]["swaps"].get("pass_complete") is False or first["deleted"] < 10
    RT.run(tmp_db, _cfg(), dry_run=False, now_ms=NOW)
    assert _tokens(tmp_db) == []
    _swap(tmp_db, "LATER", 30)
    waited = RT.run(tmp_db, _cfg(), dry_run=False, now_ms=NOW)
    assert waited["tables"]["swaps"]["skipped"] == "pass_interval"
    assert _tokens(tmp_db) == ["LATER"]


def test_switches_gate_it(tmp_db):
    _swap(tmp_db, "A", 30)
    RT.run(tmp_db, _cfg(swaps_enabled=False), dry_run=False, now_ms=NOW)
    RT.run(tmp_db, _cfg(enabled=False), dry_run=False, now_ms=NOW)
    RT.run(tmp_db, _cfg(), dry_run=True, now_ms=NOW)
    assert _tokens(tmp_db) == ["A"]


def test_the_window_cannot_be_set_under_the_floor():
    import pytest

    with pytest.raises(ValueError):
        RT.RetentionConfig(swaps_days=RT.SWAPS_FLOOR_DAYS - 1)
    with pytest.raises(ValueError):
        RT.RetentionConfig(events_other_days=RT.EVENTS_OTHER_FLOOR_DAYS - 1)


# ---------------------------------------------------------------- events

def _event(conn, kind: str, age_days: float, dedupe: str | None = None) -> None:
    conn.execute(
        "INSERT INTO events (ts_ms, kind, level, payload, dedupe_key) VALUES (?,?,?,?,?)",
        (int(NOW - age_days * DAY), kind, "info", "{}", dedupe),
    )


def test_telemetry_events_go_but_dedupe_rows_and_other_kinds_stay(tmp_db):
    _event(tmp_db, "system", 40)
    _event(tmp_db, "wallet.graded", 35)
    _event(tmp_db, "system", 40, dedupe="system:x")       # a dedupe record: stays
    _event(tmp_db, "token.created", 40, dedupe="tc:sol:A")  # not a telemetry kind and deduped
    _event(tmp_db, "position.closed", 40)                 # not in the list
    _event(tmp_db, "system", 5)                           # inside the window
    report = RT.run(tmp_db, _cfg(), dry_run=False, now_ms=NOW)
    left = [(r[0], r[1]) for r in tmp_db.execute("SELECT kind, dedupe_key FROM events ORDER BY id")]
    assert ("system", None) in left and len([k for k in left if k == ("system", None)]) == 1
    assert ("system", "system:x") in left and ("token.created", "tc:sol:A") in left
    assert ("position.closed", None) in left and ("wallet.graded", None) not in left
    assert report["tables"][RT.EVENTS_OTHER]["deleted"] == 2


def test_a_deleted_dedupe_record_would_make_an_old_token_new_again(tmp_db):
    """Why the dedupe_key condition exists: emit_once's answer IS the insert's success."""
    from kaiba.core.events import emit_once
    from kaiba.core.schemas import EventKind

    key = "token.created:sol:MINT"
    assert emit_once(EventKind.TOKEN_CREATED, {}, dedupe_key=key, conn=tmp_db) is not None
    assert emit_once(EventKind.TOKEN_CREATED, {}, dedupe_key=key, conn=tmp_db) is None
    tmp_db.execute("UPDATE events SET ts_ms = ? WHERE dedupe_key = ?", (NOW - 400 * DAY, key))
    RT.run(tmp_db, _cfg(events_other_kinds=[*RT.EVENTS_OTHER_KINDS, "token.created"]),
           dry_run=False, now_ms=NOW)
    assert emit_once(EventKind.TOKEN_CREATED, {}, dedupe_key=key, conn=tmp_db) is None, \
        "the dedupe record survived even with its kind in the list"


# ---------------------------------------------------------------- the readers it must not fool

WALLET = "W" * 44


def _walked(conn) -> None:
    conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,1)",
                 (f"backfill:swaps:sol:{WALLET}", json.dumps({"exhausted": True, "pages": 3})))


def test_history_truncated_stops_vouching_once_the_tape_is_pruned(tmp_db):
    _walked(tmp_db)
    assert G.history_truncated(tmp_db, Chain.SOL, WALLET, earliest_ts_ms=NOW - 30 * DAY) is False
    tmp_db.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,1)",
                   (RT.SWAPS_HORIZON_KEY, json.dumps(NOW - 14 * DAY)))
    # tape that starts at the horizon may have run past it
    assert G.history_truncated(tmp_db, Chain.SOL, WALLET, earliest_ts_ms=NOW - 14 * DAY + 3600_000) is True
    assert G.history_truncated(tmp_db, Chain.SOL, WALLET, earliest_ts_ms=None) is True
    # a wallet that began well after the horizon is still complete (the positive control)
    assert G.history_truncated(tmp_db, Chain.SOL, WALLET, earliest_ts_ms=NOW - 5 * DAY) is False


def test_build_evidence_passes_the_earliest_kept_swap(tmp_db):
    _walked(tmp_db)
    tmp_db.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,1)",
                   (RT.SWAPS_HORIZON_KEY, json.dumps(NOW - 14 * DAY)))
    _swap(tmp_db, "AAA", 13.9, wallet=WALLET)
    assert G.build_evidence(WALLET, Chain.SOL, tmp_db).sample_capped is True
    tmp_db.execute("DELETE FROM swaps")
    _swap(tmp_db, "AAA", 3, wallet=WALLET)
    assert G.build_evidence(WALLET, Chain.SOL, tmp_db).sample_capped is False


def test_rebuild_first_buyers_leaves_tokens_older_than_the_horizon_alone(tmp_db):
    _swap(tmp_db, "OLDTOK", 20, wallet="first")
    _swap(tmp_db, "OLDTOK", 19, wallet="second")
    _swap(tmp_db, "NEWTOK", 3, wallet="a")
    backfill.rebuild_first_buyers(tmp_db, Chain.SOL)
    assert fetch_one(tmp_db, "SELECT wallet FROM first_buyers WHERE token='OLDTOK' AND rank=1")["wallet"] == "first"

    RT.run(tmp_db, _cfg(), dry_run=False, now_ms=NOW)  # prunes OLDTOK's buys, stamps the horizon
    _swap(tmp_db, "OLDTOK", 1, wallet="late")           # a later buyer arrives
    _swap(tmp_db, "NEWTOK", 2, wallet="b")
    backfill.rebuild_first_buyers(tmp_db, Chain.SOL)

    old = fetch_all(tmp_db, "SELECT wallet, rank FROM first_buyers WHERE token='OLDTOK' ORDER BY rank")
    assert [(r["wallet"], r["rank"]) for r in old] == [("first", 1), ("second", 2)], "no new rank 1"
    new = fetch_all(tmp_db, "SELECT wallet FROM first_buyers WHERE token='NEWTOK' ORDER BY rank")
    assert [r["wallet"] for r in new] == ["a", "b"], "tokens inside the window still re-rank"


def test_the_horizon_key_is_spelled_the_same_in_all_three_modules():
    assert G.SWAPS_HORIZON_KEY == RT.SWAPS_HORIZON_KEY == backfill.SWAPS_HORIZON_KEY


# ---------------------------------------------------------------- emergency + shipped config

def test_emergency_halves_windows_and_never_goes_below_a_floor():
    e = RT.emergency_config(RT.RetentionConfig(swaps_days=14, events_other_days=30), budget_s=60)
    assert e.enabled and e.swaps_enabled and e.events_other_enabled and e.wallet_trade_enabled
    assert e.swaps_days == RT.SWAPS_FLOOR_DAYS and e.events_other_days == 15
    assert e.provider_calls_days == 7 and e.wallet_trade_days == 3 and e.budget_s == 60
    assert e.wal_pause_bytes == RT.EMERGENCY_WAL_PAUSE_BYTES > RT.RetentionConfig().wal_pause_bytes
    deep = RT.emergency_config(e)
    assert deep.swaps_days == RT.SWAPS_FLOOR_DAYS and deep.events_other_days == RT.EVENTS_OTHER_FLOOR_DAYS


def test_the_shipped_config_keeps_loss_attribution_inside_the_swaps_window():
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    cfg = S.load_config(root / "config" / "schedule.yaml")
    raw = yaml.safe_load((root / "config" / "schedule.yaml").read_text(encoding="utf-8"))
    la = raw["jobs"]["loss_attribution"]
    horizon_days = la["horizon_s"] / 86_400
    assert la["scan_days"] + horizon_days <= cfg.retention.swaps_days, "it would read a pruned hole"
    assert cfg.retention.swaps_days >= RT.SWAPS_FLOOR_DAYS


# ---------------------------------------------------------------- WAL brake

def test_an_overgrown_wal_pauses_then_stops_the_run_before_any_delete(tmp_db):
    """The DB read rule: check the WAL before each chunk. A delete run must never be the
    reason the log crosses db_guard's lines."""
    _swap(tmp_db, "A", 30)
    slept: list[float] = []
    pacer = RT._Pacer(_cfg(wal_pause_bytes=100, wal_wait_s=7), NOW + 10**9, lambda: NOW / 1000,
                      slept.append, wal_bytes=lambda: 10_000)
    assert pacer.delete(tmp_db, lambda c: c.execute("DELETE FROM swaps").rowcount) is None
    assert pacer.stopped == "wal" and slept == [7] and _tokens(tmp_db) == ["A"]


def test_a_wal_that_drains_during_the_wait_lets_the_delete_through(tmp_db):
    _swap(tmp_db, "A", 30)
    sizes = iter([10_000, 50])
    pacer = RT._Pacer(_cfg(wal_pause_bytes=100, wal_wait_s=1), NOW + 10**9, lambda: NOW / 1000,
                      lambda s: None, wal_bytes=lambda: next(sizes))
    assert pacer.delete(tmp_db, lambda c: c.execute("DELETE FROM swaps").rowcount) == 1
    assert pacer.stopped is None and pacer.wal_waits == 1


def test_run_reads_the_real_wal_file(tmp_db, tmp_path):
    reader = RT._wal_reader(tmp_db)
    assert reader is not None and isinstance(reader(), int)


def test_a_stage_that_burns_its_slice_does_not_starve_the_swaps(tmp_db, monkeypatch):
    """MEASURED 2026-10-13: in strict order a 1.12M-row provider_calls backlog spent a whole
    pass while swaps waited. Every stage now gets a fair share of the time left."""
    _swap(tmp_db, "OLD", 30)
    clock = {"t": NOW / 1000}

    def burn(conn, t, cutoff_ms, pacer):  # a backlog that eats everything it is given
        clock["t"] = pacer.deadline_ms / 1000
        pacer.out_of_time()
        return {"deleted": 0}

    monkeypatch.setattr(RT, "_prune_time_table", burn)
    monkeypatch.setattr(RT, "_prune_wallet_trade", lambda *a, **k: burn(None, None, None, a[3]))
    # provider_calls, triage and wallet_trade run BEFORE swaps and each burns its slice:
    # without fair shares the first of them would burn the whole run.
    report = RT.run(tmp_db, _cfg(wallet_trade_enabled=True), dry_run=False, now_ms=NOW,
                    clock=lambda: clock["t"], deadline_ms=int(NOW + 700_000))
    assert report["tables"]["swaps"]["deleted"] == 1 and _tokens(tmp_db) == []
    assert report["stopped"] == "budget", "a stage ran short, so the run says work remains"


def test_only_one_delete_run_at_a_time(tmp_db, tmp_path, monkeypatch):
    """The timer and disk_guard's emergency pass share one lock: the second one skips."""
    import pytest

    fcntl = pytest.importorskip("fcntl")
    lock = tmp_path / "ret.lock"
    monkeypatch.setattr(RT, "LOCK_PATH", lock)
    _swap(tmp_db, "A", 30)
    with open(lock, "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        skipped = RT.run_exclusive(tmp_db, _cfg(), dry_run=False, now_ms=NOW)
        assert skipped["mode"] == "skipped" and _tokens(tmp_db) == ["A"]
    ran = RT.run_exclusive(tmp_db, _cfg(), dry_run=False, now_ms=NOW)
    assert ran["mode"] == "delete" and _tokens(tmp_db) == []
