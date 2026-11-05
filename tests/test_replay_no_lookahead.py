"""Replay price source is chosen from prints observable by the entry fill (2026-10-14).

Codex reproduction (docs/research/codex-profit-path-audit-20261014.md): adding three source-A
prints strictly AFTER every original observation changed the replayed entry price 2.0 -> 1.0,
because the source was the one with the most prints over the whole path.
"""
from kaiba.learning import signal_audit as sa

T0 = 1_000_000
STEP = sa.LATENCY_MS


def _rows(spec):
    return [(T0 + STEP * i, price, src, "W", "buy") for i, price, src in spec]


def test_prints_after_entry_cannot_change_the_entry_price():
    base = _rows([(i, 1.0, "A") for i in range(1, 6)] + [(i, 2.0, "B") for i in range(1, 7)])
    later = _rows([(i, 1.0, "A") for i in range(7, 10)])
    before, after = sa.price_path(base, T0), sa.price_path(base + later, T0)
    assert before is not None and after is not None
    assert before[0][before[1]] == after[0][after[1]]


def test_majority_before_the_cutoff_picks_the_source():
    # B has more prints by the fill; A dominates afterwards. B must be followed.
    pre = _rows([(0, 2.0, "B")]) + [(T0 + 1_000, 2.0, "B", "W", "buy"), (T0 + 2_000, 1.0, "A", "W", "buy"),
                                    (T0 + 3_000, 2.0, "B", "W", "buy")]
    post = _rows([(i, 1.0, "A") for i in range(2, 12)] + [(i, 2.0, "B") for i in range(2, 5)])
    path = sa.price_path(pre + post, T0)
    assert path is not None
    series, i = path
    assert series[i][1] == 2.0


def test_no_print_before_the_cutoff_uses_the_first_print_after_it():
    rows = _rows([(2, 3.0, "C")] + [(i, 1.0, "A") for i in range(3, 15)])
    assert sa.entry_source([(t, p, s) for t, p, s, _w, _x in rows], T0 + STEP) == "C"


def test_the_old_whole_path_majority_rule_would_have_failed():
    # Mutation guard: the whole-path most_common rule picks A here; the cutoff rule picks B.
    import collections
    rows = _rows([(1, 2.0, "B"), (1, 1.0, "A")]) + [(T0 + 500, 2.0, "B", "W", "buy")]
    rows += _rows([(i, 1.0, "A") for i in range(2, 14)])
    priced = [(t, p, s) for t, p, s, _w, _x in rows]
    assert collections.Counter(s for _, _, s in priced).most_common(1)[0][0] == "A"
    assert sa.entry_source(priced, T0 + STEP) == "B"
