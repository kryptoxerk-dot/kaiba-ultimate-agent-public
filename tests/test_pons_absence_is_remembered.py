"""Asking the Pons factory the same question every 12 seconds is what halted all trading.

THE BUG, MEASURED on the live box 2026-09-24.

``read_pons`` already distinguishes two very different answers:

    pons_factory_read_failed   the endpoint did not answer -- rate limited, down, refused
    pons_curve_unknown         the factory ANSWERED, and said it never launched this token

The first is transient. The second is permanent: a launch record is written at launch and
never changes, which is the same property that makes ``remember_pons_curve`` safe. But only
the POSITIVE answer was ever written down. A token the factory disowned was re-asked on
every tick, of every position, forever.

Five open robinhood positions were each in exactly that state. The arithmetic:

    5 positions x 1 factory read, every 12 s   =  0.42 reads/s
    plus the fallback leg that actually priced =  0.83 reads/s
    robinhood-rpc bucket refill                =  0.60 reads/s

So the bucket could never catch up. In twelve hours of protection logs:

    17,354  robinhood-rpc: EXIT drawing on the reserved overdraft
    10,727  robinhood-rpc: EXIT bypassing chain cooldown
     3,501  robinhood-rpc: EXIT bypassing max_inflight

A saturated bucket stretches the protection tick; a tick past its interval arms
``protection_overrun``; and that halts entries on EVERY chain. Two such halts on
2026-09-24 recorded ticks of 37,343 ms and 50,225 ms against a 12,000 ms budget. The owner
had asked for MORE frequent trading, and an unremembered negative was delivering none.

WHAT THIS DOES NOT DO. It does not make a position blind. The Pons leg is skipped, not the
price: the fallback chain still runs, and for these five tokens the fallback was what had
been pricing them all along. And it never caches a FAILED read -- conflating "we could not
ask" with "we asked and it said no" is the exact mistake the existing comment in
``read_pons`` warns about, so it is pinned here as a test.
"""

from __future__ import annotations

import pytest

from kaiba.execution import evm_price

TOKEN = "0xc0d6457c16cc1111111111111111111111111111"
CURVE = "0xabcdef01234567890abcdef01234567890abcdef"

#: A factory record of five zero words: well formed, and it disowns the token.
DISOWNED = "0x" + "00" * 32 * 5
#: The same record, but word 1 carries a real curve address.
KNOWN = "0x" + "00" * 32 + "00" * 12 + CURVE[2:] + "00" * 32 * 3


def row_for(conn, token=TOKEN):
    """A ``tokens`` row for the token.

    Both ``remember_pons_curve`` and ``remember_pons_absent`` UPDATE rather than INSERT --
    deliberately, so a pricing path can never invent registry rows for arbitrary addresses.
    Every token we actually hold a position in has a row, so this matches the live shape.
    """
    conn.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, first_seen_ms) VALUES (?,?,?)",
        (evm_price.Chain.ROBINHOOD.value, token.lower(), 1),
    )
    conn.commit()


@pytest.fixture(autouse=True)
def _clear_caches():
    evm_price._PONS_ABSENT.clear()
    evm_price._PONS_CURVE.clear()
    yield
    evm_price._PONS_ABSENT.clear()
    evm_price._PONS_CURVE.clear()


class Rpc:
    """A fake batch that counts how many times it was asked to reach the network."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, calls):
        self.calls += 1
        if not self.responses:
            return [None for _ in calls]
        nxt = self.responses.pop(0)
        return nxt if isinstance(nxt, list) else [nxt]


# ------------------------------------------------------------------ the memory


def test_a_disowned_token_is_remembered(tmp_db):
    """THE FIX: the factory says no once, and we write it down."""
    row_for(tmp_db)
    rpc = Rpc(DISOWNED)
    _state, note = evm_price.read_pons(TOKEN, rpc)
    assert note == "pons_curve_unknown"
    assert evm_price.pons_absent_recently(TOKEN, conn=tmp_db)


def test_the_second_tick_spends_no_network_read(tmp_db):
    """THE POINT. This is the read that kept the bucket in permanent overdraft."""
    evm_price.read_pons(TOKEN, Rpc(DISOWNED))
    again = Rpc(DISOWNED)
    _state, note = evm_price.read_pons(TOKEN, again)
    assert again.calls == 0, f"still paying for the factory read ({again.calls} calls)"
    assert note == "pons_curve_absent_cached"


def test_the_memory_survives_a_restart(tmp_db):
    """The in-memory dict dies with the process; the tokens row is what must carry it.

    Clearing ``_PONS_ABSENT`` first is the whole point: with it populated every lookup is
    answered from memory and the persisted row is never exercised at all.
    """
    row_for(tmp_db)
    evm_price.remember_pons_absent(TOKEN, conn=tmp_db)
    evm_price._PONS_ABSENT.clear()                     # as a restart would
    assert evm_price.pons_absent_recently(TOKEN, conn=tmp_db)


# ------------------------------------------------------------------ the distinction


def test_a_FAILED_read_is_never_remembered_as_an_absence(tmp_db):
    """THE SAFETY PROPERTY. A 429 is not a launch record.

    ``read_pons`` carries a comment saying conflating these two 'told an operator a
    transient 429 was a permanent absence'. Caching the failure would make one rate-limited
    minute suppress the factory read for a full day.
    """
    rpc = Rpc(None)                                     # the endpoint did not answer
    _state, note = evm_price.read_pons(TOKEN, rpc)
    assert note == "pons_factory_read_failed"
    assert not evm_price.pons_absent_recently(TOKEN, conn=tmp_db), (
        "a transient read failure was cached as a permanent absence"
    )


def test_a_short_record_is_not_an_absence_either(tmp_db):
    """A malformed answer is not an answer."""
    _state, note = evm_price.read_pons(TOKEN, Rpc("0x" + "00" * 32))
    assert note == "pons_factory_record_short"
    assert not evm_price.pons_absent_recently(TOKEN, conn=tmp_db)


# ------------------------------------------------------------------ the TTL


def test_the_memory_expires(tmp_db):
    """Belt and braces: if this were ever wrong, it self-heals within a day."""
    row_for(tmp_db)
    evm_price.remember_pons_absent(TOKEN, conn=tmp_db, observed_ms=1_000_000)
    stale = 1_000_000 + evm_price.PONS_ABSENT_TTL_MS + 1
    assert not evm_price.pons_absent_recently(TOKEN, conn=tmp_db, now_ms=stale)
    fresh = 1_000_000 + evm_price.PONS_ABSENT_TTL_MS - 1
    assert evm_price.pons_absent_recently(TOKEN, conn=tmp_db, now_ms=fresh)


def test_a_future_stamp_is_not_trusted(tmp_db):
    """Clock skew must not become a permanent waiver of the factory read."""
    row_for(tmp_db)
    evm_price.remember_pons_absent(TOKEN, conn=tmp_db, observed_ms=5_000_000)
    assert not evm_price.pons_absent_recently(TOKEN, conn=tmp_db, now_ms=1_000_000)


def test_the_shipped_ttl_is_sane():
    day = 86_400_000
    assert 0 < evm_price.PONS_ABSENT_TTL_MS <= 7 * day


# ------------------------------------------------------------------ a real curve wins


def test_a_token_with_a_curve_is_not_marked_absent(tmp_db):
    _state, _note = evm_price.read_pons(TOKEN, Rpc(KNOWN, [None]))
    assert not evm_price.pons_absent_recently(TOKEN, conn=tmp_db)


def test_finding_a_curve_clears_an_earlier_absence(tmp_db):
    """If the world ever did change, the positive answer must win immediately.

    This pins MY change -- the ``_PONS_ABSENT.pop`` on the success path -- not the
    pre-existing registry write, so it fails for the right reason if the pop is removed.
    """
    row_for(tmp_db)
    # An absence OLDER than the TTL, so the factory is probed again rather than the
    # cached "no" being returned -- which is itself the behaviour the TTL exists for.
    evm_price.remember_pons_absent(TOKEN, conn=tmp_db, observed_ms=1)
    assert TOKEN.lower() in evm_price._PONS_ABSENT
    evm_price.read_pons(TOKEN, Rpc(KNOWN, [None]))          # the factory now knows it
    assert TOKEN.lower() not in evm_price._PONS_ABSENT, (
        "a token with a real curve is still marked absent"
    )


# ------------------------------------------------------------------ junk


@pytest.mark.parametrize("junk", ["", "not-an-address", "0x1234", None])
def test_junk_is_never_written_or_believed(tmp_db, junk):
    evm_price.remember_pons_absent(junk, conn=tmp_db)
    assert not evm_price.pons_absent_recently(junk, conn=tmp_db)


def test_without_a_registry_row_the_memory_is_process_local_only(tmp_db):
    """HONEST LIMIT, pinned so it is a known shape rather than a surprise.

    ``remember_pons_absent`` UPDATEs and never INSERTs, so a token with no ``tokens`` row
    keeps its absence only in memory. That costs one factory read per token per restart,
    which is nothing next to one per tick -- and it is the right trade, because inventing
    registry rows from a pricing path is how a registry fills with junk.
    """
    evm_price.read_pons(TOKEN, Rpc(DISOWNED))
    assert evm_price.pons_absent_recently(TOKEN, conn=tmp_db)   # in memory
    evm_price._PONS_ABSENT.clear()                              # as a restart would
    assert not evm_price.pons_absent_recently(TOKEN, conn=tmp_db)
