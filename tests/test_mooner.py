"""The mooner autopsy must not learn from the future, and must not reward buying everything.

Two failure modes would make this study worse than useless, because both produce a
confident, plausible ranking that is pure artefact:

* **lookahead.** If the outcome is measured from the FIRST print rather than from the end
  of the feature window, every token that moved during the window scores as a mooner and
  every feature read from that window "predicts" it. The study would conclude that tokens
  which have already started running tend to run.

* **frequency masquerading as precision.** MEASURED on the live tape: the wallet present in
  the most >=5x tokens is in 408 of 1,400 of them, because it buys nearly everything. Rank
  by count and it comes first; rank by precision and it scores the base rate, which is the
  truth about it. Our own book has already paid for this once -- smart-wallet COUNT is
  anti-calibrated in 54 closed fills (3 wallets -8.9%, 4+ -18.4%, 7+ a 0% win rate).
"""

from __future__ import annotations

import pytest

from kaiba.core.schemas import Chain
from kaiba.learning import mooner


def put_swap(conn, token, ts_ms, price, *, side="buy", wallet="w1", chain=Chain.SOL):
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, price_usd, source) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (chain.value, f"tx{token}{ts_ms}{wallet}", ts_ms, wallet, token, side,
         str(price), "test"),
    )


def tape(conn, token, prices, *, wallets=None, chain=Chain.SOL, step=1000):
    """One print per price, one second apart, so the early window is bounded by count."""
    for i, price in enumerate(prices):
        wallet = (wallets or {}).get(i, "w1")
        put_swap(conn, token, 1_000_000 + i * step, price, wallet=wallet, chain=chain)
    conn.commit()


# ------------------------------------------------------------------ no lookahead


def test_a_run_inside_the_feature_window_is_not_counted_as_the_outcome(tmp_db):
    """THE TRAP: 1 -> 50 during the window, flat after. That is not a mooner here.

    Measured from the first print it is a 50x. Measured from where the features stop
    being read -- which is the only honest place -- it went nowhere.
    """
    prices = [1.0] * 9 + [50.0] + [50.0] * 6
    tape(tmp_db, "RUNEARLY", prices)
    cases = mooner.build_cases(tmp_db)
    assert len(cases) == 1
    case = cases[0]
    assert case.multiple == pytest.approx(1.0)
    assert not case.mooned


def test_a_run_after_the_window_is_the_outcome(tmp_db):
    prices = [1.0] * 10 + [1.0, 2.0, 9.0, 3.0, 1.0]
    tape(tmp_db, "RUNLATE", prices)
    case = mooner.build_cases(tmp_db)[0]
    assert case.multiple == pytest.approx(9.0)
    assert case.mooned


def test_the_window_closes_on_time_as_well_as_on_count(tmp_db):
    """A token printing once an hour must not carry a day of hindsight into 'early'."""
    prices = [1.0] * 16
    tape(tmp_db, "SLOW", prices, step=mooner.EARLY_WINDOW_MS)
    cases = mooner.build_cases(tmp_db)
    assert cases, "a slow token should still be scored"
    # Only the prints inside EARLY_WINDOW_MS may be early, not the first ten by count.
    assert cases[0].early_buys + cases[0].early_sells < mooner.EARLY_PRINTS


def test_a_token_without_enough_tape_is_not_scored(tmp_db):
    tape(tmp_db, "THIN", [1.0, 2.0, 3.0])
    assert mooner.build_cases(tmp_db) == []


# ------------------------------------------------------------------ precision, not count


def test_a_wallet_that_buys_everything_scores_the_base_rate(tmp_db):
    """THE REGRESSION this module is shaped around: 'appears in the most mooners' is noise.

    ``everyone`` is early on all eight tokens; two of them moon. ``sniper`` is early on
    two, and both moon. Counting mooners ranks ``everyone`` first (2 vs 2, then more
    tokens); counting precision ranks ``sniper`` first, which is the true statement.
    """
    for i in range(8):
        moons = i < 2
        prices = [1.0] * 10 + ([20.0] if moons else [1.0]) + [1.0] * 5
        wallets = {j: "everyone" for j in range(10)}
        if moons:
            wallets[0] = "sniper"
        tape(tmp_db, f"T{i}", prices, wallets=wallets)

    result = mooner.run(tmp_db, top=10)
    cells = {cell.key: cell for cell in result.wallets}
    assert result.baseline_rate == pytest.approx(25.0)
    assert cells["everyone"].rate == pytest.approx(result.baseline_rate), (
        "a wallet that buys everything must score exactly the base rate"
    )
    assert cells["everyone"].lift(result.baseline_rate) == pytest.approx(1.0)


def test_a_thin_actor_is_not_ranked_at_all(tmp_db):
    """One lucky hit is not a 100% hit rate. Below MIN_ACTOR_TOKENS it does not rank."""
    for i in range(8):
        prices = [1.0] * 10 + ([20.0] if i == 0 else [1.0]) + [1.0] * 5
        wallets = {j: "everyone" for j in range(10)}
        if i == 0:
            wallets[0] = "lucky"
        tape(tmp_db, f"T{i}", prices, wallets=wallets)

    result = mooner.run(tmp_db, top=10)
    assert "lucky" not in {cell.key for cell in result.wallets}


def test_every_reported_row_carries_its_sample(tmp_db):
    for i in range(10):
        tape(tmp_db, f"S{i}", [1.0] * 10 + [20.0] + [1.0] * 5,
             wallets={j: "everyone" for j in range(10)})
    result = mooner.run(tmp_db, top=10)
    assert result.wallets
    for cell in result.wallets:
        assert cell.n > 0 and "n=" not in cell.key
        assert cell.label and str(cell.n) in cell.label


# ------------------------------------------------------------------ honest reporting


def test_an_empty_tape_reports_nothing_rather_than_zero(tmp_db):
    """No data is not a measurement of zero (CONTRACT rule 2)."""
    result = mooner.run(tmp_db)
    assert result.sample == 0
    assert result.wallets == [] and result.words == []
    assert "no token had enough tape" in " ".join(mooner.lines(result))


def test_the_report_states_the_threshold_and_the_baseline(tmp_db):
    tape(tmp_db, "ONE", [1.0] * 10 + [20.0] + [1.0] * 5)
    text = " ".join(mooner.lines(mooner.run(tmp_db)))
    assert f"{mooner.MOON_MULTIPLE:g}x" in text
    assert "baseline" in text


def test_a_chain_filter_only_studies_that_chain(tmp_db):
    tape(tmp_db, "SOLTOK", [1.0] * 10 + [20.0] + [1.0] * 5, chain=Chain.SOL)
    tape(tmp_db, "BSCTOK", [1.0] * 10 + [1.0] * 6, chain=Chain.BSC)
    assert mooner.run(tmp_db, Chain.SOL).sample == 1
    assert mooner.run(tmp_db, Chain.BSC).sample == 1
    assert mooner.run(tmp_db).sample == 2


def test_early_flow_bands_report_even_when_small(tmp_db):
    """Flow is a partition: a rare band is a finding, not a row to suppress."""
    tape(tmp_db, "FLOW", [1.0] * 10 + [20.0] + [1.0] * 5)
    result = mooner.run(tmp_db)
    assert result.flow, "the flow partition was suppressed"
    assert sum(cell.n for cell in result.flow) == result.sample


def test_precision_outranks_count_in_the_ordering(tmp_db):
    """THE SURVIVOR: the rates were right and the ORDER was never asserted.

    Both actors clear ``MIN_ACTOR_TOKENS``, and they disagree about which is better
    depending on how you rank:

        everyone  n=20  mooners=5  rate 25%   <- buys the whole tape, scores the base rate
        picky     n= 8  mooners=4  rate 50%   <- half of what it touches moons

    Ranking by how many mooners an actor appeared in puts ``everyone`` first (5 > 4).
    That is the exact mistake this module was built to refuse: on the live tape the
    top-by-count wallet appears in 408 of 1,400 mooners and is a router, not an oracle.
    """
    for i in range(20):
        moons = i < 5
        prices = [1.0] * 10 + ([20.0] if moons else [1.0]) + [1.0] * 5
        wallets = {j: "everyone" for j in range(10)}
        if i < 4 or 10 <= i < 14:  # picky: 4 mooners + 4 duds = n8, rate 50%
            wallets[0] = "picky"
        tape(tmp_db, f"P{i}", prices, wallets=wallets)

    result = mooner.run(tmp_db, top=10)
    cells = {cell.key: cell for cell in result.wallets}
    assert cells["everyone"].n == 20 and cells["everyone"].mooners == 5
    assert cells["picky"].n == 8 and cells["picky"].mooners == 4
    assert cells["everyone"].mooners > cells["picky"].mooners, "fixture no longer inverts"

    order = [cell.key for cell in result.wallets]
    assert order.index("picky") < order.index("everyone"), (
        "ranked by count, not precision: the wallet that buys everything came first"
    )
