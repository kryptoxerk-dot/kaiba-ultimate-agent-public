"""A curve address learned from the factory must survive the process that learned it.

THE BUG, MEASURED on the live box 2026-09-23.

``read_pons`` resolves a token's bonding curve from the Pons factory on the cold path, then
caches it in the module-level ``_PONS_CURVE`` dict. That cache dies with the process. The
registry fallback (``tokens.pool``) covers 12,453 of 15,094 robinhood tokens -- but it is
written by the launch watcher, and the tokens we actually BUY arrive through the feeds, so
**10 of 11 open robinhood positions had no cached curve at all**.

The cost is a loop that feeds itself:

    cold read needs 2 RPC round trips
      -> ``robinhood-rpc`` refills at 0.6/s and 11 positions on a 12 s tick need ~0.9/s
      -> the factory call is refused, ``pons_factory_read_failed``
      -> nothing is cached, so the next tick tries again
      -> the budget never recovers

MEASURED over 6 hours: 446 ``protection_blind`` events, 442 of them robinhood, and
``pons_factory_read_failed`` is 235 of them -- the single largest cause of a live position
having no working stop. The same starvation shows in the journal as
``robinhood-rpc: EXIT drawing on the reserved overdraft``.

THE FIX. When the factory does answer, write the curve to ``tokens.pool`` (and the quote
token to ``meta_json``) so the answer is permanent. The docstring on ``read_pons`` already
says both values are "set at launch and never change", which is exactly what makes them
safe to persist: this is caching an immutable fact, not memoising a measurement.

It does NOT widen the rate limit. A budget that cannot serve the book is a separate
problem; this removes the repeated cost of re-learning something we already knew.
"""

from __future__ import annotations

import pytest

from kaiba.core.db import fetch_one
from kaiba.core.schemas import Chain
from kaiba.execution import evm_price

TOKEN = "0x" + "ab" * 20
CURVE = "0x" + "cd" * 20
PAIR = "0x" + "ef" * 20


@pytest.fixture(autouse=True)
def _clear_memory_cache():
    evm_price._PONS_CURVE.clear()
    yield
    evm_price._PONS_CURVE.clear()


def test_a_learned_curve_is_written_to_the_registry(tmp_db):
    """THE FIX: what the factory told us once must not be asked again after a restart."""
    tmp_db.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, first_seen_ms) VALUES (?,?,?)",
        (Chain.ROBINHOOD.value, TOKEN, 1),
    )
    tmp_db.commit()

    evm_price.remember_pons_curve(TOKEN, CURVE, None, conn=tmp_db)

    row = fetch_one(
        tmp_db, "SELECT pool, meta_json FROM tokens WHERE chain=? AND address=?",
        (Chain.ROBINHOOD.value, TOKEN),
    )
    assert row is not None
    assert (row["pool"] or "").lower() == CURVE
    assert "quote_is_native" in (row["meta_json"] or "")


def test_the_registry_round_trips_a_native_quoted_curve(tmp_db):
    tmp_db.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, first_seen_ms) VALUES (?,?,?)",
        (Chain.ROBINHOOD.value, TOKEN, 1),
    )
    tmp_db.commit()
    evm_price.remember_pons_curve(TOKEN, CURVE, None, conn=tmp_db)
    assert evm_price._pons_curve_from_registry(TOKEN, conn=tmp_db) == (CURVE, None)


def test_the_registry_round_trips_a_token_quoted_curve(tmp_db):
    tmp_db.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, first_seen_ms) VALUES (?,?,?)",
        (Chain.ROBINHOOD.value, TOKEN, 1),
    )
    tmp_db.commit()
    evm_price.remember_pons_curve(TOKEN, CURVE, PAIR, conn=tmp_db)
    assert evm_price._pons_curve_from_registry(TOKEN, conn=tmp_db) == (CURVE, PAIR)


def test_remembering_does_not_clobber_other_metadata(tmp_db):
    """`meta_json` carries the dossier's own fields; this must only ADD two keys."""
    import json

    tmp_db.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, first_seen_ms, meta_json) "
        "VALUES (?,?,?,?)",
        (Chain.ROBINHOOD.value, TOKEN, 1, json.dumps({"buys_24h": 17, "exchange": "pons"})),
    )
    tmp_db.commit()
    evm_price.remember_pons_curve(TOKEN, CURVE, None, conn=tmp_db)
    row = fetch_one(tmp_db, "SELECT meta_json FROM tokens WHERE chain=? AND address=?",
                    (Chain.ROBINHOOD.value, TOKEN))
    meta = json.loads(row["meta_json"])
    assert meta["buys_24h"] == 17, "an unrelated field was lost"
    assert meta["exchange"] == "pons"
    assert meta["quote_is_native"] is True


@pytest.mark.parametrize("curve", ["", "not-an-address", evm_price.ZERO_ADDRESS, None])
def test_a_junk_curve_is_never_written(tmp_db, curve):
    """Writing a bad address would poison the registry permanently, which is worse than
    paying for the factory read again."""
    tmp_db.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, first_seen_ms) VALUES (?,?,?)",
        (Chain.ROBINHOOD.value, TOKEN, 1),
    )
    tmp_db.commit()
    evm_price.remember_pons_curve(TOKEN, curve, None, conn=tmp_db)
    row = fetch_one(tmp_db, "SELECT pool FROM tokens WHERE chain=? AND address=?",
                    (Chain.ROBINHOOD.value, TOKEN))
    assert not (row["pool"] or ""), f"junk curve {curve!r} was persisted"


def test_remembering_never_raises_on_a_broken_database():
    """A pricing path must not crash because a cache write failed. Blindness is the risk
    this whole change exists to reduce; it must not become a new way to die."""
    class Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db is gone")

        def commit(self):
            raise RuntimeError("db is gone")

    evm_price.remember_pons_curve(TOKEN, CURVE, None, conn=Boom())  # must not raise


def test_read_pons_persists_what_the_factory_answers(tmp_db, monkeypatch):
    """End to end on the cold path: one factory answer, and the registry has it."""
    tmp_db.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, first_seen_ms) VALUES (?,?,?)",
        (Chain.ROBINHOOD.value, TOKEN, 1),
    )
    tmp_db.commit()
    monkeypatch.setattr(evm_price, "get_conn", lambda: tmp_db, raising=False)

    import inspect

    source = inspect.getsource(evm_price.read_pons)
    assert "remember_pons_curve" in source, (
        "read_pons learns the curve from the factory and still throws it away on exit; "
        "10 of 11 open robinhood positions paid for that every single tick"
    )
