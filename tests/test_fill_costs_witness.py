"""Original paid RPC boundary and the real default fill-cost caller, offline."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from kaiba.core import config
from kaiba.core.schemas import Chain, EvidenceBasis, Lane, LaneMode, Receipt, Side, now_ms
from kaiba.execution import executor, fill_costs
from kaiba.providers import _http


def _response(data, *, available=True):
    return _http.Fetched(data, Receipt(
        provider="rpc", endpoint="sol.getTransaction", observed_at_ms=now_ms() - 1,
        basis=EvidenceBasis.PROVIDER_REPORTED if available else EvidenceBasis.UNAVAILABLE,
        request_digest="a" * 64, response_digest="b" * 64,
    ))


def _configured(monkeypatch, tmp_path, fetched):
    settings = SimpleNamespace(kaiba_data_dir=tmp_path, rpc_for=lambda chain: "https://example.invalid/rpc",
                               model_dump=lambda: {})
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    calls = []

    def post(*args, **kwargs):
        calls.append((args, {key: deepcopy(value) if key == "json_body" else value
                             for key, value in kwargs.items()}))
        return fetched

    monkeypatch.setattr(_http, "post_json", post)
    return calls


@pytest.mark.parametrize("data,available", [
    ({"result": {"meta": {"fee": 2**64 + 1}}}, True),
    ({"result": None}, True), ({"error": {"code": -1}}, True),
    (["malformed"], True), (None, False),
])
def test_original_envelope_is_observed_without_an_additional_fetch(monkeypatch, tmp_path, data, available):
    fetched = _response(data, available=available)
    calls = _configured(monkeypatch, tmp_path, fetched)
    expected = fill_costs.limiter_fetcher()("sol", "synthetic_signature")
    observations = []

    def witness(**values):
        observations.append(values)

    actual = fill_costs.limiter_fetcher(witness=witness)("sol", "synthetic_signature")
    assert actual == expected
    assert len(calls) == 2  # One original invocation for each compared fetcher.
    assert calls[0] == calls[1]
    assert observations[0]["fetched"] is fetched
    assert observations[0]["request"] == calls[1][1]["json_body"]
    assert observations[0]["chain"] == "sol"
    assert observations[0]["signature"] == "synthetic_signature"
    assert calls[1][1]["timeout_s"] == 20.0
    assert calls[1][1]["retries"] == 1
    assert calls[1][1]["wait_for_slot_s"] == 5.0
    assert calls[1][1]["ttl_s"] == 0.0


def test_failed_witness_keeps_original_fetch_result(monkeypatch, tmp_path):
    fetched = _response({"result": {"fee": 900}})
    calls = _configured(monkeypatch, tmp_path, fetched)

    def fail(**kwargs):
        raise OSError("synthetic private error must not be logged")

    assert fill_costs.limiter_fetcher(witness=fail)("sol", "synthetic_signature") == (fetched.data["result"], None)
    assert len(calls) == 1


def test_no_endpoint_neither_dispatches_nor_claims_capture(monkeypatch):
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(rpc_for=lambda chain: None))

    def forbidden(*args, **kwargs):
        pytest.fail("missing endpoint cannot dispatch or capture")

    monkeypatch.setattr(_http, "post_json", forbidden)
    assert fill_costs.limiter_fetcher(witness=forbidden)("sol", "synthetic_signature") == (None, "no_rpc_configured")


def _fill(conn):
    order = executor.build_order(
        decision_id=None, chain=Chain.SOL, token="1" * 32, side=Side.BUY,
        lane=Lane.SM_TRENCHES, mode=LaneMode.LIVE, amount_in=100_000_000,
        min_out=750, slippage_bps=2500,
    )
    executor._persist(order, conn)
    conn.execute("UPDATE orders SET state='filled',filled_out='1000',tx_hash=?,updated_ms=? WHERE order_id=?",
                 ("synthetic_signature", now_ms() - 10, order.order_id))
    return order.order_id


def test_default_cost_job_wires_exact_selected_orders_and_original_envelope(tmp_db, tmp_path, monkeypatch):
    from kaiba.execution import fill_cost_witness

    oid = _fill(tmp_db)
    fetched = _response({"result": None})
    calls = _configured(monkeypatch, tmp_path, fetched)
    observations, selections = [], []

    class Recorder:
        def __call__(self, **values):
            observations.append(values)

        def summary(self):
            return {"status": "test_observation", "economic_complete": False}

    def factory(path, orders):
        assert path == tmp_path / "fill-cost-witness"
        selections.extend(deepcopy(orders))
        return Recorder()

    monkeypatch.setattr(fill_cost_witness, "default_recorder", factory)
    summary = fill_costs.record_fill_costs(tmp_db, since_ms=0, pace_s=0)
    assert summary["selected"] == summary["written"] == 1
    assert len(calls) == len(observations) == len(selections) == 1
    assert selections[0]["order_id"] == oid
    assert selections[0]["tx_hash"] == observations[0]["signature"]
    assert observations[0]["fetched"] is fetched
    assert summary["response_witness"]["status"] == "test_observation"


def test_observer_setup_failure_does_not_suppress_original_job(tmp_db, tmp_path, monkeypatch):
    from kaiba.execution import fill_cost_witness

    _fill(tmp_db)
    calls = _configured(monkeypatch, tmp_path, _response({"result": None}))

    def fail(*args):
        raise OSError("synthetic unavailable store")

    monkeypatch.setattr(fill_cost_witness, "default_recorder", fail)
    summary = fill_costs.record_fill_costs(tmp_db, since_ms=0, pace_s=0)
    assert len(calls) == 1
    assert summary["written"] == 1
    assert summary["response_witness"]["reason"] == "witness_setup_unavailable"
    assert not summary["response_witness"]["economic_complete"]


def test_actual_sqlite_text_quantities_reach_real_recorder(tmp_db, tmp_path, monkeypatch):
    import json

    from kaiba.execution import fill_cost_witness

    oid = _fill(tmp_db)
    orders = fill_costs.pending_orders(tmp_db, since_ms=0, limit=10)
    assert type(orders[0]["amount_in"]) is str
    assert type(orders[0]["filled_out"]) is str
    fetched = _response({"result": None})
    calls = _configured(monkeypatch, tmp_path, fetched)
    saved = []

    def sink(identity, checksum, raw):
        saved.append((identity, checksum, raw))
        return checksum

    def factory(path, selected):
        assert selected == orders
        return fill_cost_witness.WitnessRecorder(sink, selected_orders=selected)

    monkeypatch.setattr(fill_cost_witness, "default_recorder", factory)
    summary = fill_costs.record_fill_costs(tmp_db, since_ms=0, pace_s=0)
    assert len(calls) == len(saved) == 1
    body = json.loads(saved[0][2])
    assert body["selected_orders"][0]["order_id"] == oid
    assert body["selected_orders"][0]["amount_in"] == orders[0]["amount_in"]
    assert body["parsed_response"]["value"] == fetched.data
    assert body["historical_trading_wallet"] is None
    assert not body["transaction_allocation_proven"]
    assert not body["economic_complete"]
    assert summary["response_witness"]["status_counts"]["retained"] == 1
