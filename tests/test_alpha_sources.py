"""Synthetic-fixture tests for kaiba.learning.alpha_sources (no real addresses, no live data).

The two things a wrong answer would hide behind are pinned hardest: the forward-return
arithmetic (entry, horizon carry, censoring, barrier order) and the baseline matching
(time window, age band, chain, exclusions). Both are mutation-checked.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from kaiba.learning import alpha_sources as A

M = A.MINUTE_MS
H = A.HOUR_MS
T0 = 1_790_000_000_000  # synthetic epoch ms
NOW = T0 + 30 * H


def series(*points: tuple[int, float]) -> tuple[list[int], list[float]]:
    pts = sorted(points)
    return [p[0] for p in pts], [p[1] for p in pts]


def triple(at: int, price: float) -> list[tuple[int, float]]:
    """Three prints ending at ``at`` so a median-of-three reads ``price`` exactly."""
    return [(at - 2 * M, price), (at - M, price), (at, price)]


# ---------------------------------------------------------------------------------------
# forward-return arithmetic
# ---------------------------------------------------------------------------------------


def test_forward_returns_read_the_price_at_each_horizon_against_the_entry() -> None:
    ts, px = series(
        *triple(T0, 1.0),
        *triple(T0 + H, 1.5),
        *triple(T0 + 6 * H, 0.8),
        *triple(T0 + 24 * H, 3.0),
    )
    out = A.forward_outcome(ts, px, T0, now_ms=NOW)

    assert out.entry == pytest.approx(1.0)
    assert out.entry_basis == "before"
    assert out.returns == {"1h": pytest.approx(50.0), "6h": pytest.approx(-20.0), "24h": pytest.approx(200.0)}
    assert out.censored == {}
    assert out.window_complete is True
    assert out.reach_up is True
    assert out.fell_first is False
    assert out.max_multiple == pytest.approx(3.0)
    assert out.prints == 9  # the three entry prints are not forward prints


def test_horizon_price_is_the_latest_print_before_the_horizon_not_a_later_one() -> None:
    # Prints just AFTER the horizon must not be read as the horizon price.
    ts, px = series(*triple(T0, 1.0), *triple(T0 + H - 5 * M, 1.2), *triple(T0 + H + 4 * M, 9.0))
    out = A.forward_outcome(ts, px, T0, now_ms=NOW)
    assert out.returns["1h"] == pytest.approx(20.0)


def test_entry_is_read_at_or_before_the_pick_and_only_falls_forward_inside_the_grace() -> None:
    before = A.forward_outcome(*series((T0 - M, 1.0), (T0 + M, 5.0)), T0, now_ms=NOW)
    assert before.entry == pytest.approx(1.0) and before.entry_basis == "before"

    after = A.forward_outcome(*series((T0 + M, 2.0)), T0, now_ms=NOW)
    assert after.entry == pytest.approx(2.0) and after.entry_basis == "after"

    late = A.forward_outcome(*series((T0 + A.ENTRY_GRACE_MS + 1, 2.0)), T0, now_ms=NOW)
    assert late.entry is None

    stale = A.forward_outcome(*series((T0 - A.ENTRY_STALE_MS - 1, 2.0)), T0, now_ms=NOW)
    assert stale.entry is None


def test_one_mispriced_swap_cannot_make_a_token_reach_2x_or_fall_50() -> None:
    ts, px = series(
        *triple(T0, 1.0), (T0 + 20 * M, 1.0), (T0 + 30 * M, 100.0), (T0 + 31 * M, 1.0),
        (T0 + 40 * M, 0.01), *triple(T0 + H, 1.0), *triple(T0 + 24 * H, 1.0),
    )
    out = A.forward_outcome(ts, px, T0, now_ms=NOW)
    assert out.reach_up is False
    assert out.fell_first is False
    assert out.max_multiple == pytest.approx(1.0)
    assert out.returns["1h"] == pytest.approx(0.0)


def test_fell_50_first_follows_the_order_the_barriers_were_hit() -> None:
    down_then_up = series(
        *triple(T0, 1.0), (T0 + 10 * M, 0.4), (T0 + 11 * M, 0.4),
        (T0 + 20 * M, 2.5), (T0 + 21 * M, 2.5), *triple(T0 + 24 * H, 2.5),
    )
    out = A.forward_outcome(*down_then_up, T0, now_ms=NOW)
    assert (out.fell_first, out.reach_up) == (True, True)

    up_then_down = series(
        *triple(T0, 1.0), (T0 + 10 * M, 2.5), (T0 + 11 * M, 2.5),
        (T0 + 20 * M, 0.4), (T0 + 21 * M, 0.4), *triple(T0 + 24 * H, 0.4),
    )
    out = A.forward_outcome(*up_then_down, T0, now_ms=NOW)
    assert (out.fell_first, out.reach_up) == (False, True)
    assert out.returns["24h"] == pytest.approx(-60.0)


def test_a_continuous_tape_carries_through_silence_and_a_sparse_one_censors() -> None:
    ts, px = series((T0 - M, 1.0), (T0 + 5 * M, 0.6))
    carried = A.forward_outcome(ts, px, T0, now_ms=NOW, continuous_until=NOW)
    assert carried.returns == {"1h": pytest.approx(-40.0), "6h": pytest.approx(-40.0), "24h": pytest.approx(-40.0)}
    assert carried.window_complete is True
    assert (carried.reach_up, carried.fell_first) == (False, False)

    sparse = A.forward_outcome(ts, px, T0, now_ms=NOW)
    assert sparse.returns == {"1h": None, "6h": None, "24h": None}
    assert sparse.censored == {"1h": "tape_gap", "6h": "tape_gap", "24h": "tape_gap"}
    assert (sparse.reach_up, sparse.fell_first, sparse.max_multiple) == (None, None, None)


def test_a_hit_seen_before_our_tape_stopped_is_not_counted_as_a_determined_outcome() -> None:
    # Sparse tape: a 2x shows up, then our collection stops. Counting it would keep every hit
    # and drop every miss, so neither barrier may be read off this window.
    ts, px = series(*triple(T0, 1.0), (T0 + 10 * M, 2.5), (T0 + 11 * M, 2.5))
    out = A.forward_outcome(ts, px, T0, now_ms=NOW)
    assert out.window_complete is False
    assert (out.reach_up, out.fell_first) == (None, None)


def test_on_a_continuous_tape_a_final_dump_held_by_silence_counts_as_a_fall() -> None:
    ts, px = series((T0 - M, 1.0), (T0 + 5 * M, 0.3))
    out = A.forward_outcome(ts, px, T0, now_ms=NOW, continuous_until=NOW)
    assert out.fell_first is True
    assert out.returns["24h"] == pytest.approx(-70.0)


def test_continuity_ending_before_a_horizon_censors_it() -> None:
    ts, px = series((T0 - M, 1.0), (T0 + 5 * M, 1.1))
    out = A.forward_outcome(ts, px, T0, now_ms=NOW, continuous_until=T0 + 2 * H)
    assert out.returns["1h"] == pytest.approx(10.0)
    assert out.censored == {"6h": "continuity_ended", "24h": "continuity_ended"}
    assert out.window_complete is False and out.reach_up is None


def test_graduation_censors_later_horizons_instead_of_booking_the_last_curve_print() -> None:
    ts, px = series((T0 - M, 1.0), (T0 + 30 * M, 2.2), (T0 + 31 * M, 2.4))
    out = A.forward_outcome(ts, px, T0, now_ms=NOW, continuous_until=NOW, migrated_ms=T0 + 2 * H)
    assert out.returns["1h"] == pytest.approx(140.0)
    assert out.censored == {"6h": "graduated", "24h": "graduated"}
    # The uncensored variant values the graduated horizons at the last curve print.
    assert out.grad_exit == {"1h": pytest.approx(140.0), "6h": pytest.approx(140.0), "24h": pytest.approx(140.0)}
    assert out.graduated is True
    assert out.reach_up is True and out.fell_first is False  # hit before graduation: determined
    assert out.max_multiple is None  # the window was cut short


def test_a_token_graduated_before_the_pick_gets_no_curve_continuity() -> None:
    ts, px = series((T0 - M, 1.0))
    out = A.forward_outcome(ts, px, T0, now_ms=NOW, continuous_until=NOW, migrated_ms=T0 - 5 * M)
    assert out.returns["1h"] is None and out.censored["1h"] == "tape_gap"


def test_horizons_not_yet_elapsed_are_censored_not_zero() -> None:
    ts, px = series((T0 - M, 1.0), (T0 + 30 * M, 1.5))
    out = A.forward_outcome(ts, px, T0, now_ms=T0 + 3 * H, continuous_until=T0 + 3 * H)
    assert out.returns["1h"] == pytest.approx(50.0)
    assert out.censored == {"6h": "not_elapsed", "24h": "not_elapsed"}
    assert out.graduated is None and out.reach_up is None


# ---------------------------------------------------------------------------------------
# baseline matching
# ---------------------------------------------------------------------------------------


def _obs(token: str, t: int, band: str | None = "age2", chain: str = "robinhood", entry: float | None = 1.0) -> A.BaseObs:
    return A.BaseObs(chain, token, t, band, A.Outcome(entry=entry))


def test_baseline_cell_is_same_chain_same_age_band_within_30_minutes_minus_exclusions() -> None:
    index = A.BaselineIndex([
        _obs("inside", T0 + 29 * M),
        _obs("edge", T0 - 30 * M),
        _obs("edge_late", T0 + 30 * M),
        _obs("late", T0 + 31 * M),
        _obs("early", T0 - 31 * M),
        _obs("other_band", T0, band="age5"),
        _obs("other_chain", T0, chain="sol"),
        _obs("no_age", T0, band=None),
        _obs("no_price", T0, entry=None),
        _obs("the_pick", T0),
        _obs("same_source_pick", T0 + M),
        _obs("dup", T0 + 20 * M),
        _obs("dup", T0 - 5 * M),
    ])
    cell = index.match("robinhood", "age2", T0, frozenset({"the_pick", "same_source_pick"}))
    assert sorted(o.token for o in cell) == ["dup", "edge", "edge_late", "inside"]
    assert next(o for o in cell if o.token == "dup").t == T0 - 5 * M  # closest sighting wins
    assert index.match("robinhood", None, T0, frozenset()) == []


def _done(ret: float, prints: int) -> A.Outcome:
    out = A.Outcome(entry=1.0, prints=prints, window_complete=True, reach_up=ret >= 100, fell_first=False,
                    graduated=False, max_multiple=1.0 + max(ret, 0) / 100)
    out.returns = {name: ret for name, _ in A.HORIZONS}
    out.grad_exit = dict(out.returns)
    return out


def test_activity_matching_compares_a_busy_pick_only_with_equally_busy_tokens() -> None:
    cell = [_obs(f"quiet{i}", T0) for i in range(3)] + [_obs(f"busy{i}", T0) for i in range(3)]
    cell = [A.BaseObs(o.chain, o.token, o.t, o.band, _done(0.0 if "quiet" in o.token else 50.0,
                                                            2 if "quiet" in o.token else 60)) for o in cell]
    pick = A._Scored(A.Pick("src:x", "robinhood", "the_pick", T0, T0), "age2", _done(60.0, 60))
    row = A.score_source([pick], A.BaselineIndex(cell), frozenset({"the_pick"}))
    assert row["returns_pct"]["24h"]["base_mean"] == pytest.approx(25.0)
    assert row["returns_pct_activity_matched"]["24h"]["base_mean"] == pytest.approx(50.0)
    assert row["returns_pct_activity_matched"]["24h"]["diff"] == pytest.approx(10.0)


def test_age_bands_put_a_10_minute_token_with_12_minute_tokens_not_1_minute_ones() -> None:
    assert A.age_band(10 * M) == A.age_band(12 * M)
    assert A.age_band(10 * M) != A.age_band(1 * M)
    assert A.age_band(10 * M) != A.age_band(3 * H)
    assert A.age_band(None) is None and A.age_band(-1) is None
    edges = {0: "age0", 2 * M - 1: "age0", 2 * M: "age1", 5 * M - 1: "age1", 5 * M: "age2",
             15 * M - 1: "age2", 15 * M: "age3", H: "age4", 4 * H: "age5", 24 * H: "age6",
             7 * 24 * H: "age7"}
    assert {age: A.age_band(age) for age in edges} == edges


def test_scan_sightings_are_resampled_to_one_per_token_per_30_minutes(tmp_db: sqlite3.Connection) -> None:
    for minute in (0, 10, 29, 31, 70):
        _event(tmp_db, T0 + minute * M, "scan.tier1", "robinhood", "0x" + "a1" * 20, {})
    scans = A.load_scans(tmp_db, T0 - M, T0 + 2 * H)
    assert [t for _, _, t in scans] == [T0, T0 + 31 * M, T0 + 70 * M]


# ---------------------------------------------------------------------------------------
# statistics and verdict
# ---------------------------------------------------------------------------------------


def test_difference_is_pick_mean_minus_cell_mean_and_the_interval_brackets_it() -> None:
    # The first cell is skewed (mean 3, median 0) so a median-for-mean slip is visible.
    pairs = [(10.0, [0.0, 0.0, 9.0]), (20.0, [5.0, 5.0, 5.0]), (-4.0, [-1.0, -2.0, -3.0]), (8.0, [2.0, 4.0, 6.0])]
    s = A.summarize_pairs(pairs)
    assert s["n"] == 4
    assert s["pick_mean"] == pytest.approx(8.5)
    assert s["base_mean"] == pytest.approx((3 + 5 - 2 + 4) / 4)
    assert s["diff"] == pytest.approx(8.5 - 2.5)
    assert s["diff"] == pytest.approx(s["pick_mean"] - s["base_mean"])
    lo, hi = s["ci90"]
    assert lo <= s["diff"] <= hi
    assert A.summarize_pairs([(3.0, [1.0, 1.0, 1.0])] * 5)["ci90"] == (pytest.approx(2.0), pytest.approx(2.0))
    assert A.summarize_pairs([]) == {"n": 0}


def test_weighted_median_and_bootstrap_are_deterministic() -> None:
    assert A.weighted_median([1.0, 2.0, 100.0], [1.0, 1.0, 1.0]) == 2.0
    assert A.weighted_median([1.0, 3.0], [1.0, 1.0]) == 2.0
    assert A.weighted_median([1.0, 3.0], [3.0, 1.0]) == 1.0
    values = [float(i % 7) for i in range(50)]
    assert A.bootstrap_ci(values) == A.bootstrap_ci(values)


def test_verdict_uses_the_longest_horizon_with_enough_pairs() -> None:
    enough = {"n": 12, "ci90": (1.0, 5.0)}
    thin = {"n": 4, "ci90": (-9.0, -1.0)}
    assert A.verdict({"24h": thin, "6h": enough, "1h": {"n": 0}}) == ("beats baseline", "6h mean return, n=12")
    assert A.verdict({"24h": {"n": 30, "ci90": (-5.0, -0.5)}})[0] == "worse"
    assert A.verdict({"24h": {"n": 30, "ci90": (-5.0, 0.5)}})[0] == "no different"
    assert A.verdict({"24h": thin, "6h": thin, "1h": thin})[0] == "insufficient data"


# ---------------------------------------------------------------------------------------
# reading the database
# ---------------------------------------------------------------------------------------


def _event(conn: sqlite3.Connection, ts: int, kind: str, chain: str | None, subject: str | None,
           payload: dict) -> None:
    conn.execute(
        "INSERT INTO events (ts_ms, kind, chain, subject, payload) VALUES (?,?,?,?,?)",
        (ts, kind, chain, subject, json.dumps(payload)),
    )


_SLOT = [0]


def _swap(conn: sqlite3.Connection, chain: str, token: str, ts: int, price: float, source: str = "robinhood") -> None:
    _SLOT[0] += 1
    conn.execute(
        "INSERT INTO swaps (chain, tx, slot, ts_ms, wallet, token, side, amount_token, price_usd, source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (chain, f"tx{_SLOT[0]}", _SLOT[0] if source == "robinhood" else None, ts, "0x" + "ee" * 20,
         token, "buy", "1", repr(price), source),
    )


def _token(conn: sqlite3.Connection, chain: str, address: str, created: int | None, migrated: int | None = None) -> None:
    conn.execute(
        "INSERT INTO tokens (chain, address, created_ms, migrated_ms, first_seen_ms) VALUES (?,?,?,?,?)",
        (chain, address, created, migrated, created or T0),
    )


def test_chain_tape_records_outages_and_continuity_stops_at_them(tmp_db: sqlite3.Connection) -> None:
    hb = "0x" + "11" * 20
    for minute in list(range(0, 60, 5)) + list(range(100, 200, 5)):  # dark 55 -> 100 min
        _swap(tmp_db, "robinhood", hb, T0 + minute * M, 1.0)
    _swap(tmp_db, "robinhood", hb, T0 + 70 * M, 1.0, source="gmgn:smartmoney")  # not a continuous source
    tape = A.chain_tape(tmp_db, "robinhood", T0, T0 + 10 * H)
    assert tape.dark == [(T0 + 55 * M, T0 + 100 * M)]
    assert tape.continuous_until(T0 + 10 * M) == T0 + 55 * M
    assert tape.continuous_until(T0 + 70 * M) is None
    assert tape.continuous_until(T0 + 120 * M) == T0 + 195 * M
    assert tape.continuous_until(T0 - M) is None


def test_a_token_is_priced_from_one_source_so_a_scale_slip_cannot_fake_a_2x(tmp_db: sqlite3.Connection) -> None:
    tok = "0x" + "c3" * 20
    for minute in range(-3, 25 * 60, 30):
        _swap(tmp_db, "bsc", tok, T0 + minute * M, 1.0, source="onchain:walk")
    for minute in (40, 41, 300, 301):  # same token, another feed, a millionfold off
        _swap(tmp_db, "bsc", tok, T0 + minute * M, 1_000_000.0, source="gmgn:smartmoney")
    ts, px, continuous = A._series(tmp_db, "bsc", tok, T0 - H, T0 + 26 * H)
    assert continuous is False and set(px) == {1.0}
    out = A.forward_outcome(ts, px, T0, now_ms=NOW)
    assert out.reach_up is False and out.returns["1h"] == pytest.approx(0.0)


def test_parse_pick_uses_the_sources_own_time_only_when_plausible() -> None:
    tok = "0x" + "ab" * 20
    gmgn = {"provider": "gmgn", "feed": "signal", "token": tok, "chain": "robinhood", "timestamp_ms": T0 - 2 * M}
    pick = A.parse_pick("alpha.signal", T0, "robinhood", tok, gmgn)
    assert isinstance(pick, A.Pick) and pick.source == "gmgn:signal" and pick.published_ms == T0 - 2 * M

    gmgn["timestamp_ms"] = T0 - 2 * 24 * H  # two days stale: not believed
    assert A.parse_pick("alpha.signal", T0, "robinhood", tok, gmgn).published_ms == T0

    trending = {"provider": "gmgn", "feed": "trending", "token": tok, "observed_ms": T0 - H}
    assert A.parse_pick("alpha.signal", T0, "robinhood", tok, trending).published_ms == T0

    hunter = {"source": "crtsh", "signal_kind": "cert_subdomain", "subject": "claim.example.org"}
    assert A.parse_pick("alpha.signal", T0, None, "claim.example.org", hunter).startswith("not_a_token")

    milestone = {"source": "telegram:rhscannerr", "record_type": "milestone", "address": tok}
    assert A.parse_pick("alpha.meta", T0, "robinhood", tok, milestone) == "follow_up_not_pick:rhscannerr:milestone"

    upper = A.parse_pick("alpha.meta", T0, "robinhood", tok.upper().replace("0X", "0x"),
                         {"source": "telegram:rhscannerr", "record_type": "alert",
                          "address": tok.upper().replace("0X", "0x"), "message_ts_ms": T0 - 30_000})
    assert upper.token == tok and upper.source == "rhscannerr:alert" and upper.published_ms == T0 - 30_000


def _world(conn: sqlite3.Connection) -> dict[str, str]:
    """One robinhood hour: a picked winner, four flat ordinary tokens, and noise to ignore."""
    names = {k: "0x" + v * 20 for k, v in {
        "hb": "10", "pick": "20", "b1": "31", "b2": "32", "b3": "33", "b4": "34", "old": "40", "rep": "50",
    }.items()}
    for minute in range(-120, 31 * 60, 5):  # chain heartbeat: the poller never went dark
        _swap(conn, "robinhood", names["hb"], T0 + minute * M, 1.0)
    _token(conn, "robinhood", names["pick"], T0 - 6 * M)
    _swap(conn, "robinhood", names["pick"], T0 - M, 1.0)
    _swap(conn, "robinhood", names["pick"], T0 - 10_000, 1.6)  # after the alert, before we saw it
    _swap(conn, "robinhood", names["pick"], T0 + 40 * M, 2.0)
    _swap(conn, "robinhood", names["pick"], T0 + 41 * M, 2.0)
    # The scanner also saw the picked token; it must never sit in its own baseline.
    _event(conn, T0 + 2 * M, "scan.tier1", "robinhood", names["pick"], {"token": names["pick"]})
    for i, key in enumerate(("b1", "b2", "b3", "b4")):
        seen = T0 + (i * 5 - 10) * M
        _token(conn, "robinhood", names[key], seen - 7 * M)
        _swap(conn, "robinhood", names[key], seen - M, 1.0)
        _swap(conn, "robinhood", names[key], seen + 20 * M, 1.0)
        _event(conn, seen, "scan.tier1", "robinhood", names[key], {"token": names[key]})
    _token(conn, "robinhood", names["old"], T0 - 3 * 24 * H)  # same time, wrong age band
    _swap(conn, "robinhood", names["old"], T0 - M, 1.0)
    _swap(conn, "robinhood", names["old"], T0 + 10 * M, 9.0)
    _swap(conn, "robinhood", names["old"], T0 + 11 * M, 9.0)
    _event(conn, T0, "scan.tier1", "robinhood", names["old"], {"token": names["old"]})
    alert = {"source": "telegram:rhscannerr", "record_type": "alert", "chain": "robinhood",
             "address": names["pick"], "message_ts_ms": T0 - 20_000}
    _event(conn, T0, "alpha.meta", "robinhood", names["pick"], alert)
    _event(conn, T0 + H, "alpha.meta", "robinhood", names["pick"],
           {**alert, "record_type": "milestone", "message_ts_ms": T0 + H})
    sig = {"provider": "gmgn", "feed": "signal", "chain": "robinhood"}
    _event(conn, T0 - 3 * H, "alpha.signal", "robinhood", names["rep"], {**sig, "token": names["rep"]})
    _event(conn, T0 + 5 * M, "alpha.signal", "robinhood", names["rep"], {**sig, "token": names["rep"]})
    _event(conn, NOW, "system", None, None, {})  # the database's clock
    return names


def test_end_to_end_pick_is_scored_against_its_matched_cell_only(tmp_db: sqlite3.Connection) -> None:
    names = _world(tmp_db)
    result = A.run(tmp_db, since_ms=T0 - H, now_ms=NOW)
    rows = {(r["source"], r["chain"]): r for r in result["sources"]}

    rh = rows[("rhscannerr:alert", "robinhood")]
    assert rh["n_picks"] == 1
    assert rh["coverage"] == {"scored": 1}
    for name in ("1h", "6h", "24h"):
        cell = rh["returns_pct"][name]
        assert cell["n"] == 1
        assert cell["pick_mean"] == pytest.approx(100.0)
        assert cell["base_mean"] == pytest.approx(0.0)  # "old" (+800%) is in another age band
        assert cell["base_n"] == 4
        assert cell["diff"] == pytest.approx(100.0)
    assert rh["reach_2x"]["pick_mean"] == 1.0 and rh["reach_2x"]["base_mean"] == 0.0
    assert rh["verdict"] == "insufficient data"
    assert rh["ingest_lag_s_median"] == pytest.approx(20.0)
    # Re-anchored at our sighting (T0) the entry is the 1.6 print the alert caused: 2.0 / 1.6.
    seen = rh["from_our_sighting"]["returns_pct"]["24h"]
    assert (seen["n"], seen["pick_mean"], seen["base_mean"]) == (1, pytest.approx(25.0), pytest.approx(0.0))

    assert result["not_picks"]["follow_up_not_pick:rhscannerr:milestone"] == 1
    assert result["not_picks"]["republished:gmgn:signal"] == 1
    assert ("gmgn:signal", "robinhood") not in rows
    assert result["chain_tapes"]["robinhood"]["outages"] == []
    assert result["gmgn_kline"]["calls"] == 0
    assert names["old"] not in json.dumps(rh)  # the wrong-age token never entered a cell


def test_run_issues_no_writes(tmp_db: sqlite3.Connection, tmp_path) -> None:
    _world(tmp_db)
    path = tmp_path / "kaiba.db"
    ro = A.connect_ro(path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro.execute("CREATE TABLE should_not_exist (a)")
        result = A.run(ro, since_ms=T0 - H, now_ms=NOW)  # would raise on any write
    finally:
        ro.close()
    assert result["meta"]["picks"] == 1
    with pytest.raises(sqlite3.OperationalError):
        A.connect_ro(tmp_path / "missing.db").execute("SELECT 1 FROM events")


def test_hermes_outputs_silent_runs_name_no_pick_and_text_runs_resolve_their_chain(
    tmp_db: sqlite3.Connection, tmp_path,
) -> None:
    names = _world(tmp_db)
    out = tmp_path / "hermes"
    out.mkdir()
    header = "# Cron Job: alpha scan\n\n## Script Output\n" + names["b1"] + "\n\n## Response\n\n"
    (out / "a.md").write_text(header + "[SILENT]\n", encoding="utf-8")
    (out / "b.md").write_text(header + f"1. PICK: strong find {names['pick']}\n2. unknown 0x{'90' * 20}\n",
                              encoding="utf-8")
    for f in ("a.md", "b.md"):
        os.utime(out / f, (T0 / 1000, T0 / 1000))
    picks, stats = A.load_hermes_picks(tmp_db, out, T0 - H, NOW)
    assert stats == {"runs": 2, "silent_runs": 1, "runs_with_text": 1, "addresses": 2, "unresolved": 1}
    assert [(p.source, p.chain, p.token, p.published_ms) for p in picks] == [
        (A.HERMES_SOURCE, "robinhood", names["pick"], T0)
    ]


def test_cli_prints_json_and_a_table(tmp_db: sqlite3.Connection, tmp_path, capsys) -> None:
    _world(tmp_db)
    path = str(tmp_path / "kaiba.db")
    # 1.29 days back from the data clock starts the window at ~T0 - 57 min, so the token
    # first published at T0 - 3 h is a re-publication and not a pick.
    assert A.main(["--db", path, "--since-days", "1.29", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {(r["source"], r["chain"]) for r in data["sources"]} == {("rhscannerr:alert", "robinhood")}
    assert A.main(["--db", path, "--since-days", "1.29"]) == 0
    text = capsys.readouterr().out
    assert "rhscannerr:alert [robinhood]" in text and "verdict: insufficient data" in text
