"""The pool-vs-feed stop study (``kaiba.learning.onchain_vs_feed``).

The shapes come from audit-20261001-strategy §1c: a feed that holds one price for ~36 s and
then jumps past the stop, and the opposite error -- a pool wick that crosses the stop and
recovers, which a faster stop would have sold.
"""

from __future__ import annotations

from kaiba.core.schemas import now_ms
from kaiba.learning import onchain_vs_feed as study

ENTRY = "1.0"


def _position(**kw) -> dict:
    base = {
        "position_id": "pos_x", "token": "0xt", "opened_ms": 0, "closed_ms": 90_000,
        "exit_reason": "stop_loss", "entry_price_usd": ENTRY, "cost_native": "1000", "realized_native": "-450",
    }
    base.update(kw)
    return base


def _samples(prices: list[tuple[int, str]], *, stop: str = "0.7", feed: dict[int, str] | None = None) -> list[dict]:
    feed = feed or {}
    out = []
    for ts_s, price in prices:
        inc = feed.get(ts_s)
        out.append({
            "ts_ms": ts_s * 1000, "price_usd": price, "price_quote": price, "stop_price_usd": stop,
            "incumbent_price_usd": inc, "incumbent_observed_ms": ts_s * 1000 if inc else None,
            "incumbent_age_ms": 12_000 if inc else None,
        })
    return out


def _marks(prices: list[tuple[int, str]]) -> list[dict]:
    return [{"ts_ms": ts_s * 1000, "price_usd": p} for ts_s, p in prices]


def test_a_frozen_feed_triggers_late_and_the_pool_would_have_sold_higher():
    pool = [(0, "1.0"), (12, "0.9"), (24, "0.75"), (36, "0.68"), (48, "0.6"), (60, "0.55"), (72, "0.54")]
    feed = [(0, "1.0"), (12, "0.92"), (24, "0.92"), (36, "0.92"), (48, "0.92"), (72, "0.58")]
    r = study.analyse_position(_position(), _samples(pool), _marks(feed), latency_s=2.0)
    assert r.onchain_cross_ms == 36_000 and r.incumbent_cross_ms == 72_000
    assert r.lead_s == 36.0
    # the pool decides at 36 s; the sell lands on the next block we saw (48 s) at 0.60
    assert round(r.onchain_exit_pct, 6) == -40.0
    # the feed triggered at 0.58; the pool said 0.54 at that moment
    assert round(r.incumbent_trigger_pct, 6) == -42.0 and round(r.pool_at_incumbent_trigger_pct, 6) == -46.0
    assert round(r.improvement_pp, 6) == 6.0
    assert r.onchain_only is False and r.realized_pct == -45.0
    assert r.incumbent_repeat_share is None or r.incumbent_repeat_share >= 0


def test_a_pool_wick_that_recovers_is_counted_as_a_cost_not_a_gain():
    pool = [(0, "1.0"), (12, "0.66"), (24, "0.95"), (36, "1.1"), (48, "1.12")]
    feed = [(0, "1.0"), (12, "0.97"), (24, "0.97"), (36, "1.09")]
    pos = _position(exit_reason="trailing_stop", realized_native="85")  # +8.5%
    r = study.analyse_position(pos, _samples(pool), _marks(feed), latency_s=0.0)
    assert r.onchain_only and r.incumbent_cross_ms is None
    assert r.recovered_after_onchain_cross is True
    assert round(r.onchain_exit_pct, 6) == -34.0
    assert round(r.onchain_only_delta_pp, 6) == -42.5, "selling the wick would have cost 42.5pp"


def test_the_stop_is_the_one_the_watchdog_carried_at_that_tick():
    # Ratcheted to 1.20 after a run-up: a pool print at 1.15 is a trailing stop, the feed
    # still at 1.25 is not.
    pool = [(0, "1.3"), (12, "1.15"), (24, "1.3")]
    feed = [(0, "1.3"), (12, "1.25"), (24, "1.3")]
    r = study.analyse_position(_position(exit_reason="tp1"), _samples(pool, stop="1.20"), _marks(feed))
    assert r.onchain_cross_ms == 12_000 and r.onchain_only


def test_samples_without_a_usd_rate_are_reported_not_guessed():
    samples = _samples([(0, "1.0")])
    samples[0]["price_usd"] = None
    r = study.analyse_position(_position(), samples, [])
    assert r.note == "no_usd_priced_samples" and r.onchain_cross_ms is None


def test_the_feed_falls_back_to_the_per_sample_copy_without_marks():
    pool = [(0, "1.0"), (12, "0.65"), (24, "0.6")]
    r = study.analyse_position(_position(), _samples(pool, feed={0: "1.0", 12: "0.9", 24: "0.62"}), [])
    assert r.incumbent_cross_ms == 24_000 and r.lead_s == 12.0


def test_run_reads_the_tables_and_holds_the_verdict_until_thirty_stops(tmp_db):
    now = now_ms()
    tmp_db.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, qty, qty_total, "
        "cost_native, proceeds_native, realized_native, entry_price_usd, exit_reason) "
        "VALUES ('pos_rh','robinhood','0xt','sm-trenches','live',?,?,'0','1','1000','550','-450','1.0','stop_loss')",
        (now - 120_000, now - 10_000),
    )
    for ts_s, price, inc in [(0, "1.0", "1.0"), (12, "0.68", "0.92"), (24, "0.6", "0.92"), (36, "0.55", "0.58")]:
        tmp_db.execute(
            "INSERT INTO onchain_price_samples (ts_ms, position_id, token, pool, pool_kind, price_quote, price_usd, "
            "source, stop_price_usd, incumbent_price_usd, incumbent_observed_ms, incumbent_age_ms) "
            "VALUES (?, 'pos_rh', '0xt', '0xpool', 'v4', ?, ?, 'onchain:v4', '0.7', ?, ?, 0)",
            (now - 100_000 + ts_s * 1000, price, price, inc, now - 100_000 + ts_s * 1000),
        )
        tmp_db.execute("INSERT INTO position_marks (position_id, ts_ms, price_usd, return_pct) VALUES ('pos_rh',?,?,0)",
                       (now - 100_000 + ts_s * 1000, inc))
    report = study.run(tmp_db, since_ms=now - 86_400_000)
    assert report.positions_closed == 1 and report.both_crossed == 1 and report.stop_exits == 1
    assert report.lead_s_p50 == 24.0 and report.onchain_first == 1
    assert report.verdict.startswith("INSUFFICIENT")
    assert "1/30" in report.evidence_progress
    assert "pos_rh"[-6:] in study._table(report)


def test_feed_marks_from_before_the_pool_was_watched_are_not_compared():
    pool = [(100, "1.0"), (112, "0.65")]
    marks = [(0, "0.5"), (100, "1.0"), (124, "0.6")]  # the 0 s mark predates the samples
    r = study.analyse_position(_position(), _samples(pool), _marks(marks))
    assert r.incumbent_cross_ms == 124_000 and r.lead_s == 12.0
