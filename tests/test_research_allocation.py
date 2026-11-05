"""Actual frozen book routing and separate lifetime-family readiness, offline."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
from contextlib import closing

import pytest

from kaiba.learning import admission_paper as ap
from kaiba.learning import candidate_paper as cp
from kaiba.learning import research_allocation as ra
from kaiba.learning import strategy_campaign as sc
from kaiba.learning.signal_audit import ExitLadder

DAY = 86_400_000


def build_fixture(root):
    """Finite real-broker fixture shared in design with the independent review."""
    t0, clock = 1_800_000_000_000, {"ms": 1_800_000_031_000}
    learning = root / "learning"
    books = learning / "autolearn/paper"
    manifest = sc.generate_campaign([], ExitLadder(), now_ms=t0, max_candidates=12,
        groups=[("sm-trenches", c) for c in ("sol", "bsc", "robinhood")])
    runner = cp.CandidatePaperRunner(books, learning_root=learning, max_stake_usd="30", max_open_per_arm=3,
                                     clock=lambda: clock["ms"] / 1000)
    token = "0x" + "a" * 40

    def quotes():
        return {("robinhood", token): {"quote_ts_ms": clock["ms"], "price_usd": "1",
            "liquidity_usd": "1000000", "native_usd": "1000", "decimals": 6,
            "source": "synthetic_fixture_not_market_evidence", "tax_known_zero": True,
            "venue_fee_bps": 0, "venue_fee_source": "synthetic_fixture", "venue_fee_ts_ms": clock["ms"]}}

    opportunity = {"decision_id": "old-fixture-entry", "ts_ms": t0 + 1000, "lane": "sm-trenches",
                   "chain": "robinhood", "token": token, "mode": "shadow", "paper_stake_usd": "30", "features": {}}
    first = runner.run_candidates(manifest, [opportunity], quotes(), now_ms=clock["ms"])
    assert not first["errors"] and sum(a["open"] for a in first["arms"]) == 4
    clock["ms"] = t0 + 400_000
    settled = runner.run_candidates(manifest, [], quotes(), now_ms=clock["ms"])
    assert not settled["errors"] and sum(a["open"] for a in settled["arms"]) == 2
    inspection = ap.inspect_rollover([manifest], books, learning_root=learning, now_ms=clock["ms"], max_active_arms=12)
    retired = [a["hypothesis_id"] for a in inspection["checks"] if a["status"] == "settled"]
    draining = [a["hypothesis_id"] for a in inspection["checks"] if a["status"] != "settled"]
    assert len(retired) == 10 and len(draining) == 2
    return {"runner": runner, "manifest": manifest, "clock": clock, "t0": t0, "learning": learning,
            "books": books, "token": token, "quotes": quotes, "retired": retired, "draining": draining,
            "inspection": inspection, "initial_closed_rows": settled["outcomes"], "opportunity": opportunity}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket", lambda *a, **kw: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("subprocess forbidden"))


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def rig(tmp_path):
    fixture = build_fixture(tmp_path)
    root, old, clock = fixture["books"].parent, fixture["manifest"], fixture["clock"]
    _write(root / (old["campaign_id"] + ".json"), old)
    prepared = clock["ms"]
    new = ap.generate_admission_campaign([], ExitLadder(), now_ms=prepared,
        groups=[("sm-trenches", c) for c in ("sol", "bsc", "robinhood")], max_candidates=10,
        max_stake_usd="30", max_open_per_arm=3)
    _write(root / (new["campaign_id"] + ".json"), new)
    prior = sc.assess_campaign(old, fixture["initial_closed_rows"], now_ms=prepared)["next_state"]
    receipt = ra.prepare_receipt([old], new, {old["campaign_id"]: prior}, paper_dir=fixture["books"],
                                 learning_root=fixture["learning"], now_ms=prepared)
    activation = prepared + 20_000
    authority = root / "allocation-authority.fixture.json"
    _write(root / "worker.lock", {"pid": os.getpid(), "started_ms": fixture["t0"]})
    seal_path = root / "new-books.fixture.json"
    seal = ra.prepare_new_books(receipt, paper_dir=fixture["books"], learning_root=fixture["learning"],
        authority_path=authority, seal_path=seal_path, now_ms=prepared, clock=lambda: clock["ms"] / 1000)
    _write(authority, {"version": ra.VERSION, "receipt_sha256": receipt["receipt_sha256"],
                       "new_books_sha256": seal["seal_sha256"],
                       "activation_ms": activation, "generation": 1, "funding_allowed": False})
    clock["ms"] = activation + 31_000
    kwargs = {"paper_dir": fixture["books"], "learning_root": fixture["learning"], "authority_path": authority,
              "new_books_seal_path": seal_path, "expected_new_books_sha256": seal["seal_sha256"]}
    runner = ra.AllocationRunner(receipt, **kwargs, expected_receipt_sha256=receipt["receipt_sha256"],
                                 activation_ms=activation, clock=lambda: clock["ms"] / 1000)
    return {**fixture, "new": new, "receipt": receipt, "allocation": runner,
            "activation": activation, "authority": authority, "kwargs": kwargs}


def _archives(value):
    return {artifact["file"]: ra._artifact(value["books"] / artifact["file"])
            for record in value["receipt"]["legacy_books"] if record["lease"] == "retired"
            for artifact in record["artifacts"]}


def _op(value, name="new", *, signal=None, token=None):
    return {"decision_id": name, "ts_ms": signal or value["activation"] + 1,
            "lane": "sm-trenches", "chain": "robinhood", "token": token or value["token"],
            "mode": "shadow", "paper_stake_usd": "30", "features": {}}


def test_ten_retired_two_drain_ten_new_with_full_twenty_two_family_and_immutable_history(tmp_path):
    value = rig(tmp_path)
    receipt = value["receipt"]
    assert receipt["active_lease_count"] == 12 and receipt["lifetime_family_size"] == 22
    assert sum(b["lease"] == "retired" for b in receipt["legacy_books"]) == 10
    assert sum(b["lease"] == "drain_only" for b in receipt["legacy_books"]) == 2
    assert receipt["legacy_assessments"][value["manifest"]["campaign_id"]]["campaign_id"] == value["manifest"]["campaign_id"]
    original = copy.deepcopy(value["manifest"])
    sources, archives = sc.source_versions(), _archives(value)
    for day in (1, 3, 12):
        value["clock"]["ms"] = value["t0"] + day * 86_400_000
        runner = value["allocation"]
        assert not runner.expire_pending(original, now_ms=value["clock"]["ms"])["errors"]
        assert runner.watched_tokens(original, now_ms=value["clock"]["ms"]) == [("robinhood", value["token"])]
        result = runner.run_candidates(original, [_op(value)], value["quotes"](), now_ms=value["clock"]["ms"])
        assert not result["errors"] and len(result["arms"]) == 12
        assert sum(a["open"] for a in result["arms"]) == 2
        assert len(result["outcomes"]) == 2
        assert len([r for r in result["assessment_rows"] if r["status"] == "open"]) == 2
        assert result["new_registrations_authorized"] == 0
        assert _archives(value) == archives
    assert value["manifest"] == original and sc.source_versions() == sources


def test_new_entries_strictly_after_actual_activation_not_prepared_selection(tmp_path):
    value = rig(tmp_path)
    runner = value["allocation"]
    rows = [_op(value, "before", signal=value["receipt"]["prepared_ms"] + 1),
            _op(value, "exact", signal=value["activation"]), _op(value, "after")]
    result = runner.run_candidates(value["new"], rows, value["quotes"](), now_ms=value["clock"]["ms"])
    assert not result["errors"] and result["activation_excluded_count"] == 2
    assert sum(a["open"] for a in result["arms"]) >= 2
    for arm in value["new"]["hypotheses"]:
        path = ap.AdmissionPaperRunner(value["books"], learning_root=value["learning"], max_stake_usd="30", clock=lambda: value["clock"]["ms"] / 1000).book_path(value["new"]["campaign_id"], arm["hypothesis_id"])
        with closing(sqlite3.connect(path)) as conn:
            assert all(json.loads(r[0])["signal_ms"] > value["activation"] for r in conn.execute("SELECT payload FROM candidate_entries"))


@pytest.mark.parametrize("method", ["run", "expire", "watch", "assess"])
@pytest.mark.parametrize("fault", ["authority", "archive", "manifest", "source"])
def test_all_public_paths_refuse_lost_or_changed_authority_history_and_sources(tmp_path, monkeypatch, method, fault):
    value = rig(tmp_path)
    if fault == "authority":
        value["authority"].unlink()
    elif fault == "archive":
        path = value["books"] / next(a["file"] for b in value["receipt"]["legacy_books"] if b["lease"] == "retired" for a in b["artifacts"] if a["sha256"])
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("INSERT OR REPLACE INTO candidate_meta VALUES ('external_tick','1')")
            conn.commit()
    elif fault == "manifest":
        path = value["books"].parent / (value["manifest"]["campaign_id"] + ".json")
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        original = ra.source_versions
        monkeypatch.setattr(ra, "source_versions", lambda: {**original(), "changed": "bad"})
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in value["books"].glob("*.db")}
    with pytest.raises(ValueError):
        if method == "run":
            value["allocation"].run_candidates(value["new"], [_op(value)], value["quotes"](), now_ms=value["clock"]["ms"])
        elif method == "expire":
            value["allocation"].expire_pending(value["new"], now_ms=value["clock"]["ms"])
        elif method == "watch":
            value["allocation"].watched_tokens(value["new"], now_ms=value["clock"]["ms"])
        else:
            ra.assess_family(value["receipt"], {}, now_ms=value["clock"]["ms"], activation_ms=value["activation"], **value["kwargs"])
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in before.items())


def test_internal_subset_is_never_accepted_as_full_original_manifest(tmp_path):
    value = rig(tmp_path)
    view = {**value["manifest"], "hypotheses": value["manifest"]["hypotheses"][:2]}
    assert view["campaign_id"] != sc._campaign_id(view)
    with pytest.raises(ValueError, match="full_original_manifest"):
        value["allocation"].run_candidates(view, [], {}, now_ms=value["clock"]["ms"])


def test_existing_preactivation_pending_attempt_refuses_run_expiry_and_watch(tmp_path):
    value = rig(tmp_path)
    raw = ap.AdmissionPaperRunner(value["books"], learning_root=value["learning"], max_stake_usd="30", clock=lambda: value["clock"]["ms"] / 1000)
    result = raw.run_candidates(value["new"], [_op(value, signal=value["activation"] - 1)], {}, now_ms=value["clock"]["ms"])
    assert not result["errors"]
    for method in (lambda: value["allocation"].run_candidates(value["new"], [], {}, now_ms=value["clock"]["ms"]),
                   lambda: value["allocation"].expire_pending(value["new"], now_ms=value["clock"]["ms"]),
                   lambda: value["allocation"].watched_tokens(value["new"])):
        with pytest.raises(ValueError, match="before_activation"):
            method()


def test_new_books_keep_thirty_dollars_and_three_positions_per_independent_arm(tmp_path):
    value = rig(tmp_path)
    for index in range(4):
        token = "0x" + f"{index + 100:040x}"
        quote = next(iter(value["quotes"]().values()))
        result = value["allocation"].run_candidates(value["new"], [_op(value, name=f"new{index}", token=token)],
            {("robinhood", token): quote}, now_ms=value["clock"]["ms"])
        assert not result["errors"]
    occupied = [a for a in result["arms"] if a["open"]]
    assert occupied and all(a["open"] == 3 for a in occupied)
    assert any("arm_capacity" == r.get("reason") for r in result["refusals"])
    for arm in value["new"]["hypotheses"]:
        path = value["allocation"]._admission(value["new"], value["clock"]["ms"]).book_path(value["new"]["campaign_id"], arm["hypothesis_id"])
        with closing(sqlite3.connect(path)) as conn:
            identity = json.loads(conn.execute("SELECT value FROM candidate_meta WHERE key='identity'").fetchone()[0])
            assert identity["runner"]["max_stake_usd"] == "30" and identity["runner"]["max_open_per_arm"] == 3


def test_no_automatic_refill_after_legacy_closes_and_no_second_generation(tmp_path):
    value = rig(tmp_path)
    original = copy.deepcopy(value["receipt"])
    value["clock"]["ms"] += 20_000
    q = next(iter(value["quotes"]().values())) | {"price_usd": "0.5"}
    value["allocation"].run_candidates(value["manifest"], [], {("robinhood", value["token"]): q}, now_ms=value["clock"]["ms"])
    value["clock"]["ms"] += 20_000
    q["quote_ts_ms"] = value["clock"]["ms"]
    result = value["allocation"].run_candidates(value["manifest"], [], {("robinhood", value["token"]): q}, now_ms=value["clock"]["ms"])
    assert sum(a["open"] for a in result["arms"]) == 0
    assert value["receipt"] == original and value["receipt"]["active_lease_count"] == 12
    with pytest.raises(ValueError, match="generation"):
        ra.AllocationRunner(value["receipt"], **value["kwargs"], expected_receipt_sha256=value["receipt"]["receipt_sha256"],
                             activation_ms=value["activation"], generation=2)


def test_pure_preparation_does_not_write_receipt_authority_or_any_old_book(tmp_path):
    value = rig(tmp_path)
    before = {p: p.read_bytes() for p in value["books"].parent.glob("*.json")}
    archives = _archives(value)
    result = ra.prepare_receipt([value["manifest"]], value["new"], value["receipt"]["legacy_assessments"],
        paper_dir=value["books"], learning_root=value["learning"], now_ms=value["receipt"]["prepared_ms"])
    assert result["activation_authorized"] is result["funding_allowed"] is False
    assert all(p.read_bytes() == body for p, body in before.items()) and _archives(value) == archives


def _evaluate(value, rows, now, prior=None, legacy_rows=()):
    return ra.assess_family(value["receipt"], {value["new"]["campaign_id"]: rows,
        value["manifest"]["campaign_id"]: legacy_rows}, now_ms=now, activation_ms=value["activation"],
        prior_state=prior, **value["kwargs"])


def _own(value, result, index=0):
    return next(a for a in result["arms"] if a["campaign_id"] == value["new"]["campaign_id"]
                and a["hypothesis_id"] == value["new"]["hypotheses"][index]["hypothesis_id"])


@pytest.mark.parametrize("fault", ["lost_old", "lost_new", "identity_new", "lost_seal"])
def test_all_routes_refuse_missing_allocated_books_or_seal_before_any_writer(tmp_path, fault):
    value = rig(tmp_path)
    if fault == "lost_old":
        path = value["runner"].book_path(value["manifest"]["campaign_id"], value["draining"][0])
        path.rename(path.with_suffix(".removed"))
    elif fault in ("lost_new", "identity_new"):
        path = value["allocation"]._admission(value["new"], value["clock"]["ms"]).book_path(
            value["new"]["campaign_id"], value["new"]["hypotheses"][0]["hypothesis_id"])
        if fault == "lost_new":
            path.rename(path.with_suffix(".removed"))
        else:
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("UPDATE candidate_meta SET value='{}' WHERE key='identity'")
                conn.commit()
    else:
        value["kwargs"]["new_books_seal_path"].unlink()
    def economic_files():
        # SQLite read-only opens can create empty WAL/SHM coordination files.
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in value["books"].glob("*")
                if p.is_file() and (p.suffix == ".db" or p.name.endswith("-wal") and p.stat().st_size)}
    before = economic_files()
    calls = [lambda: value["allocation"].run_candidates(value["new"], [_op(value)], value["quotes"](), now_ms=value["clock"]["ms"]),
             lambda: value["allocation"].expire_pending(value["manifest"], now_ms=value["clock"]["ms"]),
             lambda: value["allocation"].watched_tokens(value["manifest"], now_ms=value["clock"]["ms"]),
             lambda: _evaluate(value, [], value["clock"]["ms"])]
    for call in calls:
        with pytest.raises((ValueError, sqlite3.DatabaseError)):
            call()
    assert before == economic_files()


def test_explicit_empty_book_preparation_needs_lock_and_refuses_missing_book_after_seal(tmp_path):
    value = rig(tmp_path)
    value["authority"].unlink()
    arguments = dict(paper_dir=value["books"], learning_root=value["learning"],
        authority_path=value["authority"], seal_path=value["kwargs"]["new_books_seal_path"],
        now_ms=value["clock"]["ms"], clock=lambda: value["clock"]["ms"] / 1000)
    sealed = ra.prepare_new_books(value["receipt"], **arguments)
    assert len(sealed["new_books"]) == 10 and sealed["orders_allowed"] is False
    for record in sealed["new_books"]:
        with closing(sqlite3.connect(value["books"] / record["book"])) as conn:
            assert all(conn.execute("SELECT count(*) FROM " + t).fetchone()[0] == 0
                       for t in ("orders", "positions", "candidate_entries", "candidate_protection"))
    lock = value["books"].parent / "worker.lock"
    lock.unlink()
    with pytest.raises(ValueError, match="missing_owned_artifact"):
        ra.prepare_new_books(value["receipt"], **arguments)
    _write(lock, {"pid": os.getpid(), "started_ms": value["t0"]})
    missing = value["books"] / sealed["new_books"][0]["book"]
    missing.unlink()
    with pytest.raises(ValueError, match="initialized_new_book_missing"):
        ra.prepare_new_books(value["receipt"], **arguments)
    assert not missing.exists()


def test_empty_preparation_recovers_exact_prefix_but_never_after_activation(tmp_path, monkeypatch):
    value = rig(tmp_path)
    value["authority"].unlink()
    seal_path = value["kwargs"]["new_books_seal_path"]
    _, sealed = ra._load(seal_path)
    seal_path.unlink()
    for record in sealed["new_books"][3:]:
        (value["books"] / record["book"]).unlink()
    prior = {r["book"]: (value["books"] / r["book"]).read_bytes() for r in sealed["new_books"][:3]}
    arguments = dict(paper_dir=value["books"], learning_root=value["learning"], authority_path=value["authority"],
        seal_path=seal_path, now_ms=value["receipt"]["prepared_ms"], clock=lambda: value["clock"]["ms"] / 1000)
    actual = ra.prepare_new_books(value["receipt"], **arguments)
    assert actual["new_books"] == sealed["new_books"] and len(actual["initial_artifacts"]) == 10
    assert actual["seal_sha256"] == ra._sha({k: v for k, v in actual.items() if k != "seal_sha256"})
    assert all((value["books"] / name).read_bytes() == raw for name, raw in prior.items())
    _write(value["authority"], value["allocation"].expected)
    monkeypatch.setattr(ap.AdmissionPaperRunner, "_open", lambda *a: pytest.fail("activation forbids preparation writer"))
    with pytest.raises(ValueError, match="authority_already_exists"):
        ra.prepare_new_books(value["receipt"], **arguments)


def complete_rows(manifest, arm, *, now_ms, target_p, tag, uniform=False):
    """Synthetic protocol fixtures; frozen tests/score/cashflow arithmetic are real."""
    lo, hi = 0.0, .2
    for _ in range(70):
        middle = (lo + hi) / 2
        if sc.ss.p_mean_positive([middle + (-.2 if i % 2 else .2) for i in range(40)]) > target_p:
            lo = middle
        else:
            hi = middle
    selected, policy = manifest["selected_ms"], manifest["assessment_policy"].get(ap.POLICY_KEY)
    span, rows = now_ms - selected, []
    for i in range(40):
        signal = (selected + (i + 1) * (span - 600_000) // 40 if uniform
                  else selected + span * (1 if i < 20 else 3) // 4 + (i % 20) * 1000)
        opened = signal + 30_000
        hold = arm["spec"]["exit"].get("hold_ms")
        closed = opened + (hold if hold is not None else 60_000) + 12_000
        entry = 10**12
        row = {"campaign_id": manifest["campaign_id"], "hypothesis_id": arm["hypothesis_id"], "spec_hash": arm["spec_hash"],
            "provider": "paper", "source": "PaperBroker", "mode": "shadow", "lane": arm["spec"]["lane"],
            "chain": arm["spec"]["chain"], "features": {}, "outcome_id": f"fixture-{tag}-{i}",
            "signal_id": f"fixture-signal-{tag}-{i}", "token": f"fixture-token-{tag}-{i}",
            "signal_ms": signal, "opened_ms": opened, "closed_ms": closed, "observed_ms": now_ms,
            "entry_debit_native": entry, "exit_credit_native": round(entry * (1 + (lo + hi) / 2 + (-.2 if i % 2 else .2))),
            "status": "closed", "evidence_complete": True, "cost_complete": True, "inventory_complete": True}
        if policy:
            row.update({ap.POLICY_KEY: copy.deepcopy(policy), "timing_complete": True, "entry_processing_ms": opened})
            legs = []
            for side, clock in (("buy", opened), ("sell", closed)):
                leg = {"order_id": f"fixture-{tag}-{i}-{side}", "side": side, "mode": "shadow", "provider": "paper",
                    "state": "filled", ap.POLICY_KEY: copy.deepcopy(policy),
                    **{key: clock for key in ("admission_ms", "processing_ms", "ledger_ms", "quote_ms", "price_ts_ms",
                                              "depth_ts_ms", "native_price_ts_ms")}}
                if side == "sell":
                    trigger = opened + hold if hold is not None else closed - 12_000
                    leg["exit_timing"] = {"trigger_ms": trigger, "recognized_ms": trigger, "trigger_price_ms": trigger,
                        "due_ms": trigger + 12_000, "basis": "fixed_admission_deadline" if hold is not None else "observed_price_recognition",
                        "reason": "time_exit" if hold is not None else "take_profit"}
                legs.append(leg)
            row["fill_evidence"] = legs
        rows.append(row)
    return rows


def test_joint_twenty_two_prevents_local_false_readiness_and_preserves_frozen_outputs(tmp_path):
    value = rig(tmp_path)
    manifest, rows, state = value["new"], [], None
    for day in range(3):
        start = value["activation"] + (10 + day) * DAY
        now = start + 8 * 3_600_000
        # One fixed cumulative economic sample, a single fresh addition each date.
        if day == 0:
            rows = complete_rows(manifest, manifest["hypotheses"][0], now_ms=now, target_p=.007, tag="local-pass", uniform=True)
        else:
            extra = complete_rows({**manifest, "selected_ms": start}, manifest["hypotheses"][0],
                now_ms=now, target_p=.5, tag=f"fresh-{day}")[0]
            extra["exit_credit_native"] = extra["entry_debit_native"]
            rows.append(extra)
        result = _evaluate(value, rows, now, state)
        own = _own(value, result)
        local = result["assessments"][manifest["campaign_id"]]
        direct = ap.assess_admission_campaign(manifest, rows, now_ms=now,
            prior_state=None if state is None else state["scorer_states"][manifest["campaign_id"]])
        assert local == direct and local["arms"][0]["significant"]
        assert not own["joint_significant"] and own["streak"] == 0
        assert result["family_size"] == 22 and sum(a["p"] == 1 for a in result["arms"]) == 21
        assert result["funding_allowed"] is result["activation_authorized"] is False
        state = result["next_state"]
    assert local["arms"][0]["status"] == "PAPER_EVIDENCE_READY"
    assert own["status"] == "UNPROVEN"


def test_orphan_trade_is_not_an_empty_preparation_prefix(tmp_path):
    value = rig(tmp_path)
    value["authority"].unlink()
    seal_path = value["kwargs"]["new_books_seal_path"]
    _, sealed = ra._load(seal_path)
    seal_path.unlink()
    new_path = value["books"] / sealed["new_books"][0]["book"]
    source_row = None
    for arm in value["manifest"]["hypotheses"]:
        old_path = value["runner"].book_path(value["manifest"]["campaign_id"], arm["hypothesis_id"])
        with closing(sqlite3.connect(old_path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM trades LIMIT 1").fetchone()
            if row:
                source_row = dict(row)
                break
    assert source_row is not None
    with closing(sqlite3.connect(new_path)) as conn:
        columns = list(source_row)
        conn.execute("INSERT INTO trades (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")",
                     list(source_row.values()))
        conn.commit()
        assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 0
    before = new_path.read_bytes()
    with pytest.raises(ValueError, match="new_book_preparation_has_history"):
        ra.prepare_new_books(value["receipt"], paper_dir=value["books"], learning_root=value["learning"],
            authority_path=value["authority"], seal_path=seal_path, now_ms=value["clock"]["ms"],
            clock=lambda: value["clock"]["ms"] / 1000)
    assert new_path.read_bytes() == before and not seal_path.exists()


def test_global_bh_can_rescue_genuine_raw_local_uncorrected_without_relabeling_legacy(tmp_path):
    value = rig(tmp_path)
    now = value["activation"] + 8 * 3_600_000
    old = value["manifest"]
    # Pair the same signals across old exit policies as the frozen comparison requires.
    old_rows = [r for arm in old["hypotheses"] for r in complete_rows(old, arm,
        now_ms=now, target_p=.001, tag="old-paired")]
    new = value["new"]
    rows = complete_rows(new, new["hypotheses"][0], now_ms=now, target_p=.02, tag="new-global")
    result = _evaluate(value, rows, now, legacy_rows=old_rows)
    local, own = result["assessments"][new["campaign_id"]]["arms"][0], _own(value, result)
    assert local["status"] == "UNCORRECTED" and local["score"]["verdict"] == "PASS"
    assert own["joint_significant"] and own["status"] == "PASS" and own["streak"] == 1
    old_report = result["assessments"][old["campaign_id"]]
    assert sum(a["p"] < .002 for a in old_report["arms"]) >= 8
    assert any(a["unknown_count"] for a in old_report["arms"] if a["hypothesis_id"] in value["draining"])
    assert all(a["status"] == "LEGACY_EVIDENCE_RETAINED" and a["legacy_timing_qualified_for_admission"] is False
               for a in result["arms"] if a["campaign_id"] == old["campaign_id"])


def test_authoritative_readiness_requires_three_dates_new_ids_prefix_and_complete_cost(tmp_path):
    value = rig(tmp_path)
    manifest, rows, prior, snapshots = value["new"], [], None, []
    for day in range(3):
        start, now = value["activation"] + day * DAY, value["activation"] + day * DAY + 8 * 3_600_000
        rows += complete_rows({**manifest, "selected_ms": start}, manifest["hypotheses"][0], now_ms=now,
                              target_p=.001, tag=f"ready-day-{day}")
        result = _evaluate(value, rows, now, prior)
        own = _own(value, result)
        assert own["streak"] == day + 1 and own["joint_significant"]
        assert (own["status"] == "PAPER_EVIDENCE_READY") == (day == 2)
        prior = result["next_state"]
        assert _own(value, _evaluate(value, rows, now + 1000, prior))["streak"] == day + 1
        snapshots.append((copy.deepcopy(rows), now, copy.deepcopy(prior)))
    first_rows, _, first_state = snapshots[0]
    last_rows, last_now, _ = snapshots[2]
    assert _own(value, _evaluate(value, last_rows, last_now, first_state))["streak"] == 1  # skipped date
    assert _own(value, _evaluate(value, first_rows, last_now, first_state))["streak"] == 0  # stale or no new IDs
    changed = copy.deepcopy(last_rows)
    changed[0]["exit_credit_native"] += 1
    conflict = _evaluate(value, changed, last_now, first_state)
    assert _own(value, conflict)["streak"] == 0
    key = ra._key(manifest["campaign_id"], manifest["hypotheses"][0]["hypothesis_id"])
    assert conflict["next_state"]["arms"][key]["complete_ids"][first_rows[0]["outcome_id"]] == first_state["arms"][key]["complete_ids"][first_rows[0]["outcome_id"]]
    invalid = copy.deepcopy(last_rows)
    invalid[-1]["cost_complete"] = False
    assert _own(value, _evaluate(value, invalid, last_now, first_state))["streak"] == 0
    forged = copy.deepcopy(first_state)
    forged["arms"][key].update(complete_ids={}, streak=2)
    forged["state_sha256"] = ra._sha({k: v for k, v in forged.items() if k != "state_sha256"})
    with pytest.raises(ValueError, match="prior_streak_without_complete_evidence"):
        _evaluate(value, first_rows, snapshots[0][1], forged)


@pytest.mark.parametrize("foreign_field", ["campaign_id", "hypothesis_id"])
def test_foreign_identity_cannot_supply_next_date_complete_evidence(tmp_path, foreign_field):
    value = rig(tmp_path)
    new, now = value["new"], value["activation"] + 8 * 3_600_000
    rows = complete_rows(new, new["hypotheses"][0], now_ms=now, target_p=.001, tag="initial")
    first = _evaluate(value, rows, now)
    assert _own(value, first)["streak"] == 1
    later = now + DAY
    foreign = complete_rows({**new, "selected_ms": value["activation"] + DAY}, new["hypotheses"][0],
                             now_ms=later, target_p=.001, tag="foreign")[0]
    foreign[foreign_field] = "foreign"
    # A foreign packet cannot refresh the unchanged complete prefix.
    result = _evaluate(value, rows + [foreign], later, first["next_state"])
    assert _own(value, result)["streak"] == 0 and _own(value, result)["new_complete_ids"] == []
    own_state = next(iter(result["next_state"]["arms"].values()))
    assert foreign["outcome_id"] not in own_state["complete_ids"]


def test_assessment_cannot_omit_actual_new_open_inventory(tmp_path):
    value = rig(tmp_path)
    opened = value["allocation"].run_candidates(value["new"], [_op(value)], value["quotes"](), now_ms=value["clock"]["ms"])
    assert not opened["errors"] and any(a["open"] for a in opened["arms"])
    now = value["activation"] + 8 * 3_600_000
    rh = [(i, a) for i, a in enumerate(value["new"]["hypotheses"]) if a["spec"]["chain"] == "robinhood"]
    rows = [r for _, arm in rh for r in complete_rows(value["new"], arm, now_ms=now,
        target_p=.000001, tag="paired-new")]
    result = _evaluate(value, rows, now)
    for i, _ in rh:
        local = result["assessments"][value["new"]["campaign_id"]]["arms"][i]
        assert local["unknown_count"] >= 1 and _own(value, result, i)["streak"] == 0


@pytest.mark.parametrize("old", [False, True])
def test_reverse_entry_join_refuses_real_orphan_inventory_across_every_route(tmp_path, old):
    value = rig(tmp_path)
    if old:
        manifest = value["manifest"]
        hid = value["draining"][0]
        path = value["runner"].book_path(manifest["campaign_id"], hid)
    else:
        manifest = value["new"]
        result = value["allocation"].run_candidates(manifest, [_op(value)], value["quotes"](), now_ms=value["clock"]["ms"])
        assert not result["errors"]
        hid = next(a["hypothesis_id"] for a in result["arms"] if a["open"])
        path = value["allocation"]._admission(manifest, value["clock"]["ms"]).book_path(manifest["campaign_id"], hid)
    # The real order/position/protection survive; only its bookkeeping link is lost.
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT count(*) FROM positions WHERE closed_ms IS NULL").fetchone()[0] == 1
        conn.execute("DELETE FROM candidate_entries WHERE position_id IS NOT NULL")
        conn.commit()
        assert conn.execute("SELECT count(*) FROM orders WHERE state='filled'").fetchone()[0] >= 1
    before = path.read_bytes()
    calls = [lambda: value["allocation"].run_candidates(manifest, [], {}, now_ms=value["clock"]["ms"]),
             lambda: value["allocation"].expire_pending(manifest, now_ms=value["clock"]["ms"]),
             lambda: value["allocation"].watched_tokens(manifest, now_ms=value["clock"]["ms"]),
             lambda: _evaluate(value, [], value["clock"]["ms"])]
    for call in calls:
        with pytest.raises(ValueError, match="allocated_inventory_entry_join_unknown"):
            call()
    assert path.read_bytes() == before


def test_ledger_guard_accepts_genuine_partial_ladder_quantity_and_keeps_inventory_unknown(tmp_path):
    value = rig(tmp_path)
    op = _op(value) | {"chain": "bsc"}
    key = ("bsc", value["token"])
    quote = next(iter(value["quotes"]().values()))
    first = value["allocation"].run_candidates(value["new"], [op], {key: quote}, now_ms=value["clock"]["ms"])
    assert not first["errors"]
    for advance in (20_000, 13_000):
        value["clock"]["ms"] += advance
        quote = quote | {"price_usd": "2.2", "quote_ts_ms": value["clock"]["ms"], "venue_fee_ts_ms": value["clock"]["ms"]}
        result = value["allocation"].run_candidates(value["new"], [], {key: quote}, now_ms=value["clock"]["ms"])
        assert not result["errors"]
    incumbent = next(a for a in value["new"]["hypotheses"] if a["spec"]["chain"] == "bsc" and a["spec"]["exit"]["name"] == "incumbent")
    runner = value["allocation"]._admission(value["new"], value["clock"]["ms"])
    path = runner.book_path(value["new"]["campaign_id"], incumbent["hypothesis_id"])
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        qty, total, closed = conn.execute("SELECT qty,qty_total,closed_ms FROM positions").fetchone()
        assert 0 < int(qty) < int(total) and closed is None
    assert key in value["allocation"].watched_tokens(value["new"], now_ms=value["clock"]["ms"])
    assessed = _evaluate(value, [], value["clock"]["ms"])
    assert next(a for a in assessed["arms"] if a["hypothesis_id"] == incumbent["hypothesis_id"])["status"] == "UNPROVEN"
