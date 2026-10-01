"""Live excursion regressions through real accounting and Watchdog.tick, offline only.

AUDIT-INTEGRATE delegated slice: source/clock/exit transport are fixtures, not quote
validation, protection evaluation or persistence. No provider or signer is reachable.
"""
from __future__ import annotations

import socket
import subprocess
from decimal import Decimal

import pytest
import yaml

from kaiba.core.config import load_risk
from kaiba.core.db import fetch_all
from kaiba.core.schemas import LaneMode
from kaiba.execution import accounting
from kaiba.execution import watchdog as wd
from kaiba.execution.paper import load_position
from tests.test_accounting import make_order, persist

NOW = 1_790_000_020_000


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("live excursion tests must not reach external transport")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    cfg = load_risk()
    cfg.protection["use_provider_orders"] = False
    cfg.protection["stop_loss_bps"] = 3000
    path = tmp_path / "risk.yaml"
    path.write_text(yaml.safe_dump(cfg.model_dump(mode="json")))
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))
    monkeypatch.setattr(wd, "now_ms", lambda: NOW)
    monkeypatch.setattr(accounting, "now_ms", lambda: NOW)


def open_live(conn, *, mode=LaneMode.LIVE):
    order = persist(conn, make_order(decision_id=None, mode=mode))
    result = accounting.apply_fill(order, conn, price_usd=Decimal("100"), ts=NOW - 10_000)
    assert result is not None
    return result


@pytest.mark.parametrize("mode", [LaneMode.LIVE, LaneMode.CANARY])
def test_fresh_filled_entry_stays_unknown_until_a_quote_is_observed(tmp_db, mode):
    opened = open_live(tmp_db, mode=mode)
    restored = load_position(tmp_db, opened.position_id)
    assert restored.entry_price_usd == restored.peak_price_usd == Decimal("100")
    assert opened.mae_pct is opened.mfe_pct is None
    assert restored.mae_pct is None
    assert restored.mfe_pct is None, "an initialized peak is not an observed flat excursion"
    assert fetch_all(tmp_db, "SELECT * FROM position_marks") == []


class QuoteFixture:
    name = "offline-quote"

    def __init__(self, price="120", *, observed_ms=NOW - 9_000, **changes):
        from tests.test_accounting import TOKEN
        from kaiba.core.schemas import Chain, EvidenceBasis

        self.value = wd.PriceQuote(
            price_usd=Decimal(price), observed_ms=observed_ms,
            basis=EvidenceBasis.PROVIDER_REPORTED, source="fixture:mid",
            chain=Chain.SOL, token=TOKEN,
        ).model_copy(update=changes)
        self.calls = []

    def quote(self, chain, token):
        self.calls.append((chain, token))
        return self.value


def dog(conn, source, submitter=None):
    from kaiba.execution.protection import ProtectionConfig
    from tests.test_watchdog import RecordingSubmitter

    return wd.Watchdog(
        conn, price_source=source, submitter=submitter or RecordingSubmitter(),
        cfg_provider=lambda: ProtectionConfig(
            use_provider_orders=False, stop_loss_bps=3000, tp_ladder=[], trailing=[],
        ),
    )


def test_accepted_quotes_sample_extremes_with_source_time_and_provenance(tmp_db):
    from kaiba.core.db import jload
    from tests.test_watchdog import events_named

    opened = open_live(tmp_db)
    source = QuoteFixture()
    watchdog = dog(tmp_db, source)
    assert watchdog.tick().errors == 0
    marks = fetch_all(tmp_db, "SELECT * FROM position_marks ORDER BY id")
    assert len(marks) == 1, "live accepted quotes currently leave no excursion marks"
    assert marks[0]["ts_ms"] == NOW - 9_000
    assert Decimal(marks[0]["price_usd"]) == Decimal("120")
    assert marks[0]["return_pct"] == pytest.approx(20)
    assert marks[0]["mfe_pct"] == pytest.approx(20)
    assert marks[0]["mae_pct"] == 0

    source.value = source.value.model_copy(update={"price_usd": Decimal("90"), "observed_ms": NOW - 7_000})
    assert watchdog.tick().errors == 0
    sampled = load_position(tmp_db, opened.position_id)
    assert sampled.mfe_pct == pytest.approx(20)
    assert sampled.mae_pct == pytest.approx(-10)

    source.value = source.value.model_copy(update={"price_usd": Decimal("140"), "observed_ms": NOW - 6_000})
    assert dog(tmp_db, source).tick().errors == 0  # restart reads durable extrema
    sampled = load_position(tmp_db, opened.position_id)
    assert sampled.mfe_pct == pytest.approx(40)
    assert sampled.mae_pct == pytest.approx(-10)
    marks = fetch_all(tmp_db, "SELECT * FROM position_marks ORDER BY id")
    assert len(marks) == 3
    assert len(source.calls) == 3, "sampling must not fetch a second quote"
    evidence = events_named(tmp_db, "live_excursion_sample")
    assert len(evidence) == 3
    assert {e["mark_id"] for e in evidence} == {m["id"] for m in marks}
    latest = evidence[0]
    assert latest["quote_provenance"] == source.value.model_dump(mode="json")
    assert latest["entry_price_usd"] == "100"
    assert latest["coverage"] == "sampled_quotes_only"
    assert latest["history_complete"] is False
    metadata = jload(fetch_all(tmp_db, "SELECT value FROM kv WHERE key=?", (
        f"watchdog.excursion:{opened.position_id}",
    ))[0]["value"])
    assert metadata["sample_count"] == 3
    assert metadata["first_observed_ms"] == NOW - 9_000
    assert metadata["last_observed_ms"] == NOW - 6_000


@pytest.mark.parametrize("case", [
    "pre_open", "missing_chain", "missing_token", "missing_source", "blank_source",
    "wrong_chain", "wrong_token", "future", "stale", "zero_time", "stale_basis",
    "unavailable", "zero_price", "negative_price", "missing_price", "nan_price", "inf_price",
])
def test_invalid_quote_never_contaminates_live_samples(tmp_db, case):
    from kaiba.core.schemas import Chain, EvidenceBasis

    opened = open_live(tmp_db)
    source = QuoteFixture("110")
    watchdog = dog(tmp_db, source)
    assert watchdog.tick().errors == 0
    baseline = fetch_all(tmp_db, "SELECT * FROM position_marks")
    assert len(baseline) == 1  # a broken/disconnected sampler cannot pass a refusal test
    changes = {
        "pre_open": {"observed_ms": opened.opened_ms - 1},
        "missing_chain": {"chain": None}, "missing_token": {"token": None},
        "missing_source": {"source": "none"}, "blank_source": {"source": ""},
        "wrong_chain": {"chain": Chain.BSC}, "wrong_token": {"token": "wrong"},
        "future": {"observed_ms": NOW + 1}, "stale": {"observed_ms": NOW - 600_000},
        "zero_time": {"observed_ms": 0}, "stale_basis": {"basis": EvidenceBasis.STALE},
        "unavailable": {"basis": EvidenceBasis.UNAVAILABLE},
        "zero_price": {"price_usd": Decimal(0)}, "negative_price": {"price_usd": Decimal(-1)},
        "missing_price": {"price_usd": None}, "nan_price": {"price_usd": Decimal("NaN")},
        "inf_price": {"price_usd": Decimal("Infinity")},
    }[case]
    source.value = source.value.model_copy(update={
        "price_usd": Decimal("150"), "observed_ms": NOW - 7_000, **changes,
    })
    report = watchdog.tick()
    assert report.errors == 0
    assert fetch_all(tmp_db, "SELECT * FROM position_marks") == baseline
    restored = load_position(tmp_db, opened.position_id)
    assert restored.mfe_pct == pytest.approx(10)
    assert restored.mae_pct == 0
    source.value = QuoteFixture("90", observed_ms=NOW - 5_000).value
    assert dog(tmp_db, source).tick().errors == 0
    assert len(fetch_all(tmp_db, "SELECT * FROM position_marks")) == 2
    assert load_position(tmp_db, opened.position_id).mae_pct == pytest.approx(-10)


@pytest.mark.parametrize("mode", [LaneMode.SHADOW, LaneMode.OFF])
def test_non_live_modes_remain_owned_by_paper_not_live_sampler(tmp_db, mode):
    from kaiba.execution.paper import PaperBroker

    opened = open_live(tmp_db, mode=mode)
    if mode is LaneMode.SHADOW:
        PaperBroker(tmp_db).mark_to_market(opened, Decimal("105"), now_ms=NOW - 9_500)
    before = fetch_all(tmp_db, "SELECT * FROM position_marks")
    source = QuoteFixture()
    assert dog(tmp_db, source).tick().errors == 0
    assert fetch_all(tmp_db, "SELECT * FROM position_marks") == before


def test_source_validation_marks_only_accepted_fallback_not_rejected_original(tmp_db):
    opened = open_live(tmp_db)
    original = QuoteFixture()
    alternative = QuoteFixture("105", source="fixture:alternative", observed_ms=NOW - 8_000)
    source = wd.FallbackPriceSource(original, alternative)
    assert dog(tmp_db, source).tick().errors == 0
    # A price-only source transition below the stop requires independent corroboration.
    original.value = QuoteFixture("20", source="fixture:suspect", observed_ms=NOW - 7_000).value
    assert dog(tmp_db, source).tick().errors == 0
    marks = fetch_all(tmp_db, "SELECT * FROM position_marks ORDER BY id")
    assert [Decimal(m["price_usd"]) for m in marks] == [Decimal("120"), Decimal("105")]
    assert len(alternative.calls) == 1  # _validate_quote actually asked the other source
    assert load_position(tmp_db, opened.position_id).mae_pct == 0


def test_uncorroborated_source_transition_produces_no_mark(tmp_db):
    from tests.test_watchdog import events_named

    open_live(tmp_db)
    source = QuoteFixture()
    assert dog(tmp_db, source).tick().errors == 0
    before = fetch_all(tmp_db, "SELECT * FROM position_marks")
    source.value = QuoteFixture("20", source="fixture:suspect", observed_ms=NOW - 7_000).value
    report = dog(tmp_db, source).tick()
    assert report.blind == 1 and report.errors == report.exits == 0
    assert "uncorroborated" in events_named(tmp_db, "protection_blind")[0]["reason"]
    assert fetch_all(tmp_db, "SELECT * FROM position_marks") == before
