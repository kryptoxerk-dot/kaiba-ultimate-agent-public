"""twin_exits: a challenger is the incumbent plus one earlier exit, costed like the incumbent."""
import json
import re
import time
from pathlib import Path

from kaiba.learning import position_tape, shadow_tape
from kaiba.learning import twin_exits as tx

T0 = 1_791_000_000_000
NATIVE_USD = 200.0
COST = 1_000_000_000  # 1 SOL
QTY = 1_000_000


def _prints(path, *, liq="1000000", start=T0 + 1_000, step=12_000):
    return [{"obs": start + i * step, "px": str(px), "liq": liq, "venue": "pumpswap"} for i, px in enumerate(path)]


def _pos(legs, *, closed=None, proceeds=None, entry=1.0):
    legs = [tx.Leg(ts, q) for ts, q in legs]
    return tx.Pos("pos_1", "sm-trenches", "sol", T0, closed or legs[-1].ts, COST, QTY,
                  proceeds if proceeds is not None else COST, entry, "stop_loss", legs)


def test_an_incumbent_that_exits_before_the_trigger_leaves_the_challenger_identical():
    prints = _prints([1.0, 0.9, 0.7])
    got = tx.evaluate(_pos([(T0 + 25_000, QTY)]), prints, NATIVE_USD)
    pair = got["hold60s"]
    assert isinstance(pair, tx.Pair) and not pair.triggered and pair.challenger == pair.incumbent_model


def test_hold60s_sells_the_rest_at_the_first_print_a_tick_after_60s():
    # price rises to 1.5 by ~60 s, then the incumbent rides it down and exits at 0.6 after 10 min
    path = [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.5, 1.4] + [0.6] * 45
    prints = _prints(path)
    got = tx.evaluate(_pos([(T0 + 600_000, QTY)]), prints, NATIVE_USD)
    pair = got["hold60s"]
    assert isinstance(pair, tx.Pair) and pair.triggered
    assert pair.challenger > pair.incumbent_model + 0.5  # sold near 1.5x instead of 0.6x


def test_take_profit_all_sells_the_rest_after_the_first_print_at_or_above_1_25x():
    path = [1.0, 1.1, 1.3, 1.35, 1.2] + [0.8] * 30
    got = tx.evaluate(_pos([(T0 + 400_000, QTY)]), _prints(path), NATIVE_USD)
    pair = got["tp1.25all"]
    assert isinstance(pair, tx.Pair) and pair.triggered and pair.challenger > pair.incumbent_model


def test_partial_sells_before_the_trigger_are_the_challengers_too():
    path = [1.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 1.0] + [0.5] * 40
    legs = [(T0 + 13_000 + 12_000, QTY // 2), (T0 + 500_000, QTY - QTY // 2)]
    pair = tx.evaluate(_pos(legs), _prints(path), NATIVE_USD)["hold60s"]
    assert isinstance(pair, tx.Pair) and pair.triggered
    # half was already sold at 2.0 by the incumbent; the challenger sells the other half at ~1.0-2.0
    # instead of 0.5 -- better, but by less than if it held the whole position
    assert pair.incumbent_model < pair.challenger < pair.incumbent_model + 0.9


def test_no_print_inside_the_fill_window_drops_the_pair_rather_than_guessing():
    prints = _prints([1.0, 1.0]) + _prints([0.5] * 5, start=T0 + 400_000)
    got = tx.evaluate(_pos([(T0 + 450_000, QTY)]), prints, NATIVE_USD)
    assert got["hold60s"] == "no_fill_print"


def test_a_leg_without_liquidity_is_an_unknown_cost():
    got = tx.evaluate(_pos([(T0 + 100_000, QTY)]), _prints([1.0] * 10, liq=None), NATIVE_USD)
    assert set(got.values()) == {"incumbent_leg_unpriced"}


def test_a_different_price_scale_at_entry_is_dropped():
    got = tx.evaluate(_pos([(T0 + 100_000, QTY)], entry=1000.0), _prints([1.0] * 10), NATIVE_USD)
    assert set(got.values()) == {"entry_scale_mismatch"}


def test_costs_are_charged_on_the_challenger_sale():
    flat = _prints([1.0] * 60)
    pair = tx.evaluate(_pos([(T0 + 600_000, QTY)]), flat, NATIVE_USD)["hold60s"]
    # flat price: both arms lose exactly the modelled sell cost (pumpswap 120 + floor 30 bps + tiny impact + tip)
    assert -0.03 < pair.challenger < -0.015 and abs(pair.challenger - pair.incumbent_model) < 0.001


def test_pass_needs_the_pass_line_and_a_positive_paired_lower_bound():
    start, end = T0, T0 + 10 * 86_400_000
    good = [tx.Pair(start + i * 3_600_000 * 4, 0.10 + (i % 3) * 0.01, -0.05, -0.05, True) for i in range(60)]
    bad = [tx.Pair(start + i * 3_600_000 * 4, -0.10, -0.05, -0.05, True) for i in range(60)]
    rows = {r["challenger"]: r for r in tx.summarize({("sm-trenches", "sol", "hold60s"): good,
                                                      ("sm-trenches", "sol", "hold5m"): bad}, start, end)}
    assert rows["hold60s"]["verdict"] == "PASS"
    assert rows["hold5m"]["verdict"] == "FAIL"


def test_halves_split_the_evidence_span_not_the_window():
    """Pairs all inside the last 2 days of a 7-day window still split into two halves."""
    end = T0 + 7 * 86_400_000
    pairs = [tx.Pair(end - 2 * 86_400_000 + i * 3_600_000, 0.08 + (i % 4) * 0.01, -0.02, -0.02, True)
             for i in range(46)]
    [row] = tx.summarize({("migration-fade", "sol", "hold60s"): pairs}, T0, end)
    assert row["pass_line"]["mean_old"] is not None and row["pass_line"]["mean_new"] is not None
    assert row["verdict"] == "PASS"


def test_the_stressed_scoring_charges_every_leg_and_can_flip_a_thin_pass():
    """Codex review 2026-10-18: a percentage fee does not cancel when the arms' proceeds differ.
    Each pair carries a +STRESS_BPS-per-leg score; a flat price loses exactly that much more."""
    flat = _prints([1.0] * 60)
    pair = tx.evaluate(_pos([(T0 + 600_000, QTY)]), flat, NATIVE_USD)["hold60s"]
    extra = pair.challenger - pair.challenger_stress
    assert 0.019 < extra < 0.021  # ~1% on the entry + ~1% on the sale
    end = T0 + 10 * 86_400_000
    thin = [tx.Pair(T0 + i * 3_600_000 * 4, 0.012, -0.01, -0.01, True, 0.012 - 0.02, -0.01 - 0.02) for i in range(60)]
    [row] = tx.summarize({("migration-fade", "sol", "hold60s"): thin}, T0, end)
    assert row["verdict"] == "PASS" and row["survives_stress"] is False


def test_native_usd_is_as_of_and_rejects_bad_rows(tmp_path):
    """Codex twin review 2026-10-18: the hour-bucket read could use a price from up to 59 min
    AFTER the position, and stale or NaN rows were accepted. Never after; never invalid."""
    import sqlite3

    from kaiba.learning.twin_exits import FX_MAX_AGE_MS, NativeUsd

    db = tmp_path / "fx.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE native_prices (chain TEXT, ts_ms INTEGER, price_usd TEXT)")
    t = 1_800_000_000_000  # on a minute boundary
    conn.executemany("INSERT INTO native_prices VALUES (?,?,?)", [
        ("sol", t - 120_000, "100.0"),       # 2 min before: the as-of answer
        ("sol", t + 30_000, "250.0"),        # after the time: must never be used
        ("eth", t - FX_MAX_AGE_MS - 60_000, "3000"),  # too old
        ("bsc", t - 60_000, "NaN"),
        ("base", t - 60_000, "-1"),
    ])
    conn.commit()
    conn.close()
    fx = NativeUsd(db)
    assert fx.at("sol", t + 10_000) == 100.0, "a price observed after the time must not be used"
    assert fx.at("sol", t + 59_000) == 100.0
    assert fx.at("eth", t) == 0.0 and fx.at("robinhood", t) == 0.0, "stale -> unknown"
    assert fx.at("bsc", t) == 0.0 and fx.at("base", t) == 0.0, "NaN / negative -> unknown"


def _tape_row(pid, obs, px=1.0):
    return {"pid": pid, "obs": obs, "px": str(px), "liq": "1000000", "venue": "pumpswap"}


def _run(tmp_path, monkeypatch, positions, rows, *, now):
    tape_dir = tmp_path / "tape"
    tape_dir.mkdir(exist_ok=True)
    days = {}
    for row in rows:
        day = time.strftime("%Y-%m-%d", time.gmtime(row["obs"] / 1000))
        days.setdefault(day, []).append(row)
    for day, kept in days.items():
        (tape_dir / f"{day}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in kept), encoding="utf-8")
    monkeypatch.setattr(tx, "load_positions", lambda *_a: list(positions))
    monkeypatch.setattr(tx.time_exit_trial, "load_trials", lambda: {})
    monkeypatch.setattr(tx.NativeUsd, "at", lambda *_a: NATIVE_USD)
    monkeypatch.setattr(tx, "_spool_root", lambda: tmp_path)
    return tx.run_once(tmp_path / "db", tape_dir, tmp_path / "out", days=7, now_ms=now), tape_dir


def test_the_report_is_the_same_as_reading_the_whole_tape(tmp_path, monkeypatch):
    now = T0 + 86_400_000
    two = tx.Pos("pos_2", "sm-trenches", "sol", T0 + 5_000, T0 + 300_000, COST, QTY, COST // 2, 1.0, "stop_loss",
                 [tx.Leg(T0 + 300_000, QTY)])
    positions = [_pos([(T0 + 600_000, QTY)]), two]
    rows = []
    for i, px in enumerate([1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.5, 1.4] + [0.6] * 45):
        rows.append(_tape_row("pos_1", T0 + 1_000 + i * 12_000, px))
        rows.append(_tape_row("pos_2", T0 + 6_000 + i * 12_000, 2.0 - i * 0.02))
        rows.append(_tape_row("someone_else", T0 + 1_000 + i * 12_000, 9.0))

    report, tape_dir = _run(tmp_path, monkeypatch, positions, rows, now=now)

    results = {}
    tape = shadow_tape.load(tape_dir, now - 7 * 86_400_000, now - tx.SETTLE_MS)
    for pos in positions:
        prints = [r for r in tape[pos.pid] if pos.opened <= r["obs"] <= pos.closed + tx.FILL_MAX_AGE_MS]
        for name, got in tx.evaluate(pos, prints, NATIVE_USD).items():
            results.setdefault((pos.lane, pos.chain, name), []).append(got)
    assert report["rows"] == tx.summarize(results, now - 7 * 86_400_000, now - tx.SETTLE_MS)
    assert report["positions"] == 2 and report["positions_without_tape"] == 0
    assert json.loads((tmp_path / "out" / "latest.json").read_text(encoding="utf-8"))["rows"] == report["rows"]


def test_only_the_positions_rows_are_ever_read_and_the_spool_is_removed(tmp_path, monkeypatch):
    def refuse(*_a):
        raise AssertionError("loading the whole tape is what ran the unit out of memory")

    monkeypatch.setattr(tx.shadow_tape, "load", refuse)
    positions = [_pos([(T0 + 600_000, QTY)])]
    rows = [_tape_row("pos_1", T0 + 1_000 + i * 12_000) for i in range(40)]
    rows += [_tape_row("someone_else", T0 + 1_000 + i * 12_000) for i in range(40)]

    report, _dir = _run(tmp_path, monkeypatch, positions, rows, now=T0 + 86_400_000)

    assert report["tape_provenance"]["selected_rows"] == 40
    assert report["tape_provenance"]["source_rows"] == 80
    assert not list(tmp_path.glob(position_tape.PREFIX + "*"))


def test_a_position_with_no_tape_or_a_bad_span_is_counted_not_fatal(tmp_path, monkeypatch):
    silent = tx.Pos("pos_2", "sm-trenches", "sol", T0, T0 + 1_000, COST, QTY, COST, 1.0, "stop_loss", [])
    backwards = tx.Pos("pos_3", "sm-trenches", "sol", T0 + 9_000, T0, COST, QTY, COST, 1.0, "stop_loss", [])
    rows = [_tape_row("pos_1", T0 + 1_000 + i * 12_000) for i in range(40)]

    report, _dir = _run(tmp_path, monkeypatch, [_pos([(T0 + 400_000, QTY)]), silent, backwards], rows,
                        now=T0 + 86_400_000)

    assert report["positions"] == 3
    assert report["positions_without_tape"] == 2


def test_the_spool_ceiling_fits_inside_the_units_memory_cap():
    """The spool lives in /dev/shm and is charged to the unit, so it must leave room for Python."""
    unit = (Path(__file__).resolve().parents[1] / "deploy" / "systemd" / "user" / "kaiba-twin-exits.service")
    cap = int(re.search(r"^MemoryMax=(\d+)M$", unit.read_text(encoding="utf-8"), re.M).group(1))
    assert tx.TAPE_LIMITS.max_spool_bytes + 64 * 1024 * 1024 <= cap * 1024 * 1024
