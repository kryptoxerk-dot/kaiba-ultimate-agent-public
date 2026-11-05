"""Disposable files and synthetic identifiers; no transport or canonical DB."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from kaiba.execution import fill_cost_witness as w

NOW = 1_800_000_000_000


def order(**changes):
    value = {"order_id": "synthetic-order", "chain": "sol", "side": "buy", "lane": "synthetic-lane",
             "token": "synthetic-token", "tx_hash": "synthetic-transaction", "amount_in": "100000000",
             "min_out": "10", "slippage_bps": 100, "filled_out": "12345678901234567890", "updated_ms": NOW - 100}
    value.update(changes)
    return value


def fetched(data=None, **receipt_changes):
    receipt = {"provider": "rpc", "endpoint": "sol.getTransaction", "basis": "provider_reported",
               "observed_at_ms": NOW - 10, "request_digest": "a" * 64, "response_digest": "b" * 64, "note": None}
    receipt.update(receipt_changes)
    return SimpleNamespace(data={"jsonrpc": "2.0", "result": data, "id": 1},
                           receipt=SimpleNamespace(model_dump=lambda **_: copy.deepcopy(receipt)))


def call(recorder, result=None, **changes):
    values = {"chain": "sol", "signature": "synthetic-transaction",
              "request": {"jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                          "params": ["synthetic-transaction", {"encoding": "jsonParsed", "commitment": "finalized"}]},
              "fetched": fetched(result)}
    values.update(changes)
    return recorder(**values)


def body(result=None, rows=None, at=NOW):
    saved = []
    recorder = w.WitnessRecorder(lambda identity, checksum, raw: saved.append((identity, checksum, raw)) or checksum,
                                 selected_orders=rows or [order()], clock=lambda: at)
    assert call(recorder, result)["status"] == "retained"
    return saved[0]


def store(tmp_path, **kwargs):
    return w.PrivateWitnessStore(tmp_path / "private", clock=lambda: NOW, **kwargs)


def index(sink):
    return json.loads((sink.path / "index.json").read_bytes())


def private_write(path, raw):
    path.write_bytes(raw)
    path.chmod(0o600)


def test_original_values_exact_selected_snapshot_and_explicit_unknowns():
    rows = [order()]
    saved = []
    recorder = w.WitnessRecorder(lambda i, s, r: saved.append((i, s, r)) or s,
                                 selected_orders=rows, clock=lambda: NOW)
    rows[0]["token"] = "mutated-after-selection"
    result = {"amount": 2**80, "precise": Decimal("1.000000000000000001"), "text": "1.000000000000000001"}
    assert call(recorder, result)["status"] == "retained"
    record = json.loads(saved[0][2])
    restored = w.restore(record["parsed_response"])["result"]
    assert restored == result and type(restored["amount"]) is int
    assert type(restored["precise"]) is Decimal and type(restored["text"]) is str
    assert record["selected_orders"] == [order()]
    assert type(record["selected_orders"][0]["amount_in"]) is str
    assert record["selected_orders_sha256"] == hashlib.sha256(w.encoded([order()])).hexdigest()
    assert record["historical_trading_wallet"] is None and record["native_unit"] is None
    for key in ("economic_complete", "profitability_proven", "transaction_allocation_proven",
                "provider_request_digest_binds_json_body", "transport_wire_bytes_available"):
        assert record[key] is False
    assert record["coverage"] == "UNKNOWN"


def test_all_matching_orders_preserved_without_transaction_allocation():
    _, _, raw = body(rows=[order(), order(order_id="synthetic-other", side="sell")])
    value = json.loads(raw)
    assert len(value["selected_orders"]) == 2
    assert value["transaction_allocation_proven"] is False


@pytest.mark.parametrize("changes", [{"amount_in": True}, {"amount_in": "01"}, {"amount_in": "1e9"},
    {"amount_in": -1}, {"amount_in": 1.0}, {"filled_out": str(2**256)}, {"updated_ms": True},
    {"updated_ms": NOW + 1}, {"min_out": "-1"}, {"slippage_bps": True}, {"slippage_bps": 10001},
    {"side": "other"}, {"token": "https://secret.invalid/key"}])
def test_selected_input_refusal(changes):
    with pytest.raises(ValueError):
        w.WitnessRecorder(lambda *_: pytest.fail("sink called"), selected_orders=[order(**changes)], clock=lambda: NOW)


@pytest.mark.parametrize("quantity", [0, "0", 2**80, str(2**80)])
def test_canonical_sqlite_text_and_integer_quantities_have_positive_controls(quantity):
    _, _, raw = body(rows=[order(amount_in=quantity, min_out=quantity, filled_out=quantity)])
    value = json.loads(raw)["selected_orders"][0]
    assert value["amount_in"] == quantity and type(value["amount_in"]) is type(quantity)


@pytest.mark.parametrize("changes", [{"chain": "bsc"}, {"signature": "different"},
    {"request": {"method": "getTransaction", "params": ["different"]}},
    {"fetched": fetched(observed_at_ms=NOW + 1)}, {"fetched": fetched(observed_at_ms=True)},
    {"fetched": fetched({"headers": {"authorization": "secret"}})},
    {"fetched": fetched({"error": "https://private.invalid/key"})},
    {"fetched": fetched({"amount": float("nan")})}])
def test_refused_capture_never_calls_sink_or_leaks_input(changes):
    recorder = w.WitnessRecorder(lambda *_: pytest.fail("sink called"), selected_orders=[order()], clock=lambda: NOW)
    status = call(recorder, **changes)
    assert status["status"] == "unavailable"
    assert "secret" not in json.dumps(status) and "invalid" not in json.dumps(status)


def test_provider_digest_is_not_request_identity():
    first = body()[0]
    other = order(tx_hash="synthetic-other-transaction")
    recorder = w.WitnessRecorder(lambda i, s, r: s, selected_orders=[other], clock=lambda: NOW)
    status = call(recorder, signature=other["tx_hash"], request={"method": "getTransaction", "params": [other["tx_hash"]]})
    assert status["status"] == "retained" and status["identity_sha256"] != first


def test_structural_and_decimal_encoding_budget(monkeypatch):
    monkeypatch.setattr(w, "MAX_NODES", 10)
    with pytest.raises(ValueError, match="bounded_structure"):
        w.typed([None] * 10)
    monkeypatch.setattr(w, "MAX_NODES", 50000)
    monkeypatch.setattr(w, "CAP", 4096)
    with pytest.raises(ValueError):
        w.typed({"key" * 100: [Decimal("1" * 900)] * 10})


def test_private_store_commits_exact_bytes_idempotently_and_null_is_not_zero(tmp_path):
    sink = store(tmp_path)
    item = body()
    assert sink(*item) == item[1]
    prior = (sink.path / "index.json").read_bytes()
    assert sink(*item) == item[1]
    assert (sink.path / "index.json").read_bytes() == prior
    assert len(index(sink)["records"]) == 1
    assert sink.read_record(item[1]) == item[2]
    value = json.loads(item[2])
    assert w.restore(value["parsed_response"])["result"] is None
    assert value["economic_complete"] is False and value["historical_trading_wallet"] is None
    if os.name == "posix":
        assert sink.path.stat().st_mode & 0o777 == 0o700
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in sink.path.iterdir())


def test_capacity_bound_does_not_change_existing_commit(tmp_path, monkeypatch):
    sink = store(tmp_path)
    sink(*body({"one": 1}))
    prior = (sink.path / "index.json").read_bytes()
    monkeypatch.setattr(w, "MAX_RECORDS", 1)
    with pytest.raises(ValueError, match="store_capacity"):
        sink(*body({"two": 2}))
    assert (sink.path / "index.json").read_bytes() == prior
    assert len(list(sink.path.glob("*.json"))) == 2


def test_total_byte_bound_before_writing_new_body(tmp_path, monkeypatch):
    sink = store(tmp_path)
    monkeypatch.setattr(w, "MAX_TOTAL", 2 * w.INDEX_CAP)
    with pytest.raises(ValueError, match="store_capacity"):
        sink(*body())
    assert not list(sink.path.glob("*.json"))


@pytest.mark.parametrize("fault", ["body", "index", "orphan", "lock", "hardlink"])
def test_corrupt_partial_and_foreign_files_fail_closed(tmp_path, fault):
    sink = store(tmp_path)
    item = body()
    sink(*item)
    existing_index = (sink.path / "index.json").read_bytes()
    path = sink.path / (item[1] + ".json")
    if fault == "body":
        private_write(path, item[2][:-1] + b"!")
    elif fault == "index":
        private_write(sink.path / "index.json", b"{}")
    elif fault == "orphan":
        private_write(sink.path / ".index.pending", b"uncertain")
    elif fault == "lock":
        private_write(sink.path / "store.lock", b"")
    else:
        os.link(path, sink.path / "foreign-link")
    with pytest.raises(ValueError):
        sink(*body({"next": 1}))
    if fault != "index":
        assert (sink.path / "index.json").read_bytes() == existing_index
    if fault == "lock":
        assert (sink.path / "store.lock").read_bytes() == b""


def test_reader_rehashes_even_when_index_metadata_is_replaced(tmp_path):
    sink = store(tmp_path)
    item = body()
    sink(*item)
    path = sink.path / (item[1] + ".json")
    private_write(path, item[2][:-1] + b"!")
    value = index(sink)
    value["records"][0]["file_identity"] = list(w._identity(path.stat()))
    private_write(sink.path / "index.json", w.encoded(value))
    with pytest.raises(ValueError, match="store_corrupt"):
        sink.read_record(item[1])


def test_expiry_is_not_a_fresh_capture_and_old_body_is_pruned_on_next_admission(tmp_path):
    clock = [NOW]
    sink = w.PrivateWitnessStore(tmp_path / "private", clock=lambda: clock[0])
    old = body()
    sink(*old)
    clock[0] += w.RETENTION_MS + 1
    with pytest.raises(ValueError):
        sink.read_record(old[1])
    new = body({"new": 1}, at=clock[0])
    assert sink(*new) == new[1]
    assert not (sink.path / (old[1] + ".json")).exists()
    assert len(index(sink)["records"]) == 1
    with pytest.raises(ValueError):
        sink(*old)


def test_no_full_historical_payload_scan_on_append(tmp_path, monkeypatch):
    sink = store(tmp_path)
    first = body({"one": 1})
    sink(*first)
    original = w._read
    def guarded(path, cap):
        assert path.name != first[1] + ".json", "historical payload scan"
        return original(path, cap)
    monkeypatch.setattr(w, "_read", guarded)
    sink(*body({"two": 2}))
    assert len(index(sink)["records"]) == 2


def test_store_same_instance_concurrency_and_deadline_refuse(tmp_path):
    sink = store(tmp_path)
    sink._active.acquire()
    try:
        with pytest.raises(ValueError, match="store_busy"):
            sink(*body())
    finally:
        sink._active.release()
    sink._deadline = 0
    with pytest.raises(ValueError, match="store_deadline"):
        sink._check()


def test_lock_replacement_same_bytes_is_preserved(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX replacement of an open inode")
    sink = store(tmp_path)
    original = sink._inventory
    def replace(now):
        path = sink.path / "store.lock"
        raw = path.read_bytes()
        path.unlink()
        private_write(path, raw)
        return original(now)
    monkeypatch.setattr(sink, "_inventory", replace)
    with pytest.raises(ValueError):
        sink(*body())
    assert (sink.path / "store.lock").exists()
    os.unlink(sink.path / "store.lock")


def test_late_index_cas_preserves_replacement_during_staged_read(tmp_path, monkeypatch):
    sink = store(tmp_path)
    sink(*body({"first": 1}))
    original, pending_reads = w._read, [0]
    def replace(path, cap):
        result = original(path, cap)
        if path.name == ".index.pending":
            pending_reads[0] += 1
            if pending_reads[0] == 2:
                private_write(sink.path / "index.json", b"foreign-index")
        return result
    monkeypatch.setattr(w, "_read", replace)
    with pytest.raises(ValueError, match="store_unsafe"):
        sink(*body({"second": 2}))
    assert (sink.path / "index.json").read_bytes() == b"foreign-index"


def test_expired_replacement_during_ownership_check_is_never_unlinked(tmp_path, monkeypatch):
    import inspect

    clock = [NOW]
    sink = w.PrivateWitnessStore(tmp_path / "private", clock=lambda: clock[0])
    old = body()
    sink(*old)
    clock[0] += w.RETENTION_MS + 1
    original, injected = sink._owns, [False]
    expired = sink.path / (old[1] + ".json")
    def replace():
        result = original()
        caller = inspect.currentframe().f_back.f_code.co_name
        # Initial admission also calls _owns. Inject only after the expiry
        # index has committed, immediately before the prune's body check.
        pruned = not any(row["witness_sha256"] == old[1] for row in index(sink)["records"])
        if caller == "_store" and pruned and expired.exists() and not injected[0]:
            injected[0] = True
            foreign = tmp_path / "replacement"
            private_write(foreign, b"foreign-expired-body")
            os.replace(foreign, expired)
        return result
    monkeypatch.setattr(sink, "_owns", replace)
    with pytest.raises(ValueError, match="store_corrupt"):
        sink(*body({"next": 1}, at=clock[0]))
    assert injected[0] and expired.read_bytes() == b"foreign-expired-body"


def test_mutation_after_index_commit_cannot_receive_retention_ack(tmp_path, monkeypatch):
    sink = store(tmp_path)
    item = body()
    original = sink._commit
    def mutate(value, prior):
        original(value, prior)
        private_write(sink.path / (item[1] + ".json"), b"post-commit-mutation")
    monkeypatch.setattr(sink, "_commit", mutate)
    recorder = w.WitnessRecorder(sink, selected_orders=[order()], clock=lambda: NOW)
    assert call(recorder)["status"] == "unavailable"
    assert recorder.summary()["status_counts"]["retained"] == 0


def test_directory_sync_failure_after_publication_is_unconfirmed(tmp_path, monkeypatch):
    sink = store(tmp_path)
    original = w._sync
    def fail(directory):
        if directory == sink.path and (sink.path / "index.json").exists():
            raise OSError("synthetic secret-looking content")
        original(directory)
    monkeypatch.setattr(w, "_sync", fail)
    recorder = w.WitnessRecorder(sink, selected_orders=[order()], clock=lambda: NOW)
    status = call(recorder)
    assert status["status"] == "unavailable" and "secret" not in json.dumps(status)
    assert (sink.path / "index.json").exists()  # Publication presence is not a durable acknowledgement.


@pytest.mark.parametrize("field,value", [("economic_complete", True), ("historical_trading_wallet", "synthetic-wallet"),
                                        ("provider_observed_ms", NOW + 1), ("selected_orders_sha256", "0" * 64)])
def test_recomputed_body_hash_does_not_authorize_false_proofs_or_context(tmp_path, field, value):
    identity, _, raw = body()
    changed = json.loads(raw)
    changed[field] = value
    raw = w.encoded(changed)
    with pytest.raises(ValueError):
        store(tmp_path)(identity, hashlib.sha256(raw).hexdigest(), raw)
    assert not (tmp_path / "private").exists()


def test_hung_worker_has_bounded_queue_no_false_ack_or_raw_reference():
    entered, release = threading.Event(), threading.Event()
    retained = []
    def slow(identity, checksum, raw):
        entered.set()
        assert release.wait(3)
        retained.append(raw)
        return checksum
    sink = w._QueuedSink(slow)
    recorder = w.WitnessRecorder(sink, selected_orders=[order()], clock=lambda: NOW)
    mutable = {"value": [1]}
    assert call(recorder, mutable)["status"] == "queued"
    assert entered.wait(1)
    mutable["value"][0] = 99
    assert recorder.summary()["status_counts"]["pending"] == 1
    assert call(recorder, {"next": 1})["status"] == "queued"
    start = time.monotonic()
    third = call(recorder, {"overflow": 1})
    assert time.monotonic() - start < 0.5
    assert third["status"] == "unavailable" and third["reason"] == "queue_full"
    assert recorder.summary()["status_counts"]["retained"] == 0
    release.set()
    sink.queue.join()
    assert w.restore(json.loads(retained[0])["parsed_response"])["result"] == {"value": [1]}
    assert recorder.summary()["status_counts"] == {"queued": 0, "pending": 0, "retained": 2, "unavailable": 1}


def test_unexpected_secret_exception_is_sanitized_and_worker_survives():
    def bad(*_):
        raise ValueError({"secret": "https://secret.invalid"})
    sink = w._QueuedSink(bad)
    recorder = w.WitnessRecorder(sink, selected_orders=[order()], clock=lambda: NOW)
    call(recorder)
    sink.queue.join()
    summary = recorder.summary()
    assert summary["status_counts"]["unavailable"] == 1
    assert "secret" not in json.dumps(summary) and "https" not in json.dumps(summary)
    assert sink._worker.is_alive()


def test_default_factory_uses_one_worker_and_refuses_other_root(tmp_path, monkeypatch):
    monkeypatch.setattr(w.time, "time", lambda: NOW / 1000)
    monkeypatch.setattr(w, "_DEFAULT_ROOT", None)
    monkeypatch.setattr(w, "_DEFAULT_SINK", None)
    first = w.default_recorder(tmp_path / "one", [order()])
    second = w.default_recorder(tmp_path / "one", [order()])
    third = w.default_recorder(tmp_path / "two", [order()])
    assert first.sink is second.sink and w._DEFAULT_SINK is first.sink
    assert call(third)["reason"] == "different_store_root"
    assert not (tmp_path / "one").exists() and not (tmp_path / "two").exists()


def test_tail_is_bounded_and_no_status_can_promote_economics():
    recorder = w.WitnessRecorder(lambda *_: {"status": "queued", "reason": "none"},
                                 selected_orders=[order()], clock=lambda: NOW)
    for _ in range(40):
        call(recorder)
    summary = recorder.summary()
    assert len(summary["statuses"]) == 32 and summary["omitted_statuses"] == 8
    assert summary["status_counts"]["queued"] == 32
    assert summary["economic_complete"] is False and summary["additional_provider_calls"] == 0
