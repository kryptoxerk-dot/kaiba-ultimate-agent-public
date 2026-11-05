"""Our own limiter refusing is not the venue failing, and must not refuse an entry.

MEASURED 2026-09-22, traced end to end. Robinhood was the only chain in profit that day
(+0.02142 ETH, against sol -0.45196 and bsc -0.10480, both of which had tripped their
daily loss stops) and it could not size a single position: 17 of 28 skips in three hours
were ``no_viable_band``. The chain:

    provider_budgets.robinhood-rpc = 1500ms / capacity 3 / refill 0.6/s
      -> the bucket sits permanently overdrawn (-15,913 on 25,001 spent)
      -> the Pons factory read returns `pons:factory_unreadable:no attempt made`
      -> read_venue is unpriced, so evm_cost_model has no venue fee and no token tax
      -> sizing_band returns `cost_model_unavailable`
      -> the risk gate refuses with `no_viable_band`

``_GRADUATED_NOTES`` lets a curve read fall back to :func:`read_dex_venue` only when the
token has permanently LEFT the curve, and the reason is sound: "a transient RPC failure
must keep refusing, or a flaky node silently downgrades every entry to a looser cost
model". A node that is timing out is a node whose chain is misbehaving, and that is
exactly when a looser cost model is most dangerous.

**But "no attempt made" is not a flaky node.** It is this process's own rate limiter
declining to spend a credit. The venue was never asked, nothing about the chain is
unhealthy, and the evidence the fallback needs -- the token's tax from its dossier, the
chain's gas from the CLI, and a published DEX fee bound -- is all still available and
still fresh. Refusing there protects against nothing and costs every entry on the chain.

The throttle itself is NOT the thing to loosen: the endpoint is the public
``rpc.mainnet.chain.robinhood.com`` and it produced 9 rate-limit events in 24 hours even at
this budget. The limiter is doing its job. What has to change is the reading of its
refusal.
"""

from __future__ import annotations

import pytest

from kaiba.execution import viability as V


# ---------------------------------------------------------------- the distinction


@pytest.mark.parametrize(
    "note",
    [
        "pons:factory_unreadable:no attempt made",
        "pons:factory_unreadable:no attempt made:dossier_liquidity_stale:4066s>600s",
        "pons:rpc_refused:no attempt made",
        "flap:curve_unreadable:no attempt made",
    ],
)
def test_our_own_limiter_refusing_may_fall_back(note):
    assert V._limiter_refused(note)


@pytest.mark.parametrize(
    "note",
    [
        "pons:rpc_timeout",
        "pons:factory_unreadable:ETIMEDOUT",
        "pons:factory_unreadable:connection reset",
        "pons:probe_disabled",
        "",
        None,
    ],
)
def test_a_real_venue_failure_still_refuses(note):
    """A node that is timing out is a chain that is misbehaving. Keep refusing there."""
    assert not V._limiter_refused(note)


def test_a_graduated_token_is_still_its_own_reason():
    """The existing path is untouched; this adds a second reason, it does not replace one."""
    assert V._left_the_curve("pons:graduated")
    assert not V._limiter_refused("pons:graduated")


# ---------------------------------------------------------------- the fallback


def _stub_pons(monkeypatch, note: str):
    monkeypatch.setattr(
        V, "read_pons_venue",
        lambda token, conn, at_ms=None: V.VenueRead(
            V.NoDepth(source=note), None, None, None, note),
    )
    V.reset_venue_cache()


def put_dossier(conn, chain, token, *, buy="0", sell="0"):
    import json

    def measure(value):
        return {"value": value, "basis": "provider_reported",
                "receipt": {"provider": "gmgn", "endpoint": "token.security",
                            "observed_at_ms": 1, "basis": "provider_reported"},
                "freshness_budget_s": 900}

    body = {"address": token, "chain": chain.value, "built_at_ms": 1, "grade": "B",
            "buy_tax_bps": measure(buy), "sell_tax_bps": measure(sell)}
    conn.execute(
        "INSERT OR REPLACE INTO token_dossiers (chain, address, built_at_ms, score, grade, "
        "blockers_json, warnings_json, unknowns_json, dossier_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (chain.value, token, 1, None, "B", "[]", "[]", "[]", json.dumps(body)),
    )
    conn.commit()


TOKEN = "0x51377fe2ad6137e323500c9db71bb63b867cecab"


@pytest.fixture(autouse=True)
def _pin_gas(monkeypatch):
    monkeypatch.setattr(V, "_evm_gas_price_wei", lambda chain, conn: (120_000_000, "gas:test"))


def test_a_limiter_refusal_reaches_the_dex_reader(tmp_db, monkeypatch):
    """THE REGRESSION: this returned unpriced and cost robinhood every entry."""
    from kaiba.core.schemas import Chain

    put_dossier(tmp_db, Chain.ROBINHOOD, TOKEN)
    _stub_pons(monkeypatch, "pons:factory_unreadable:no attempt made")
    venue = V.read_venue(Chain.ROBINHOOD, TOKEN, tmp_db)
    assert venue.priced, venue.note
    assert "dex:" in venue.note


def test_a_timeout_still_produces_an_unpriced_venue(tmp_db, monkeypatch):
    """The guard that matters: a misbehaving chain must not get a looser cost model."""
    from kaiba.core.schemas import Chain

    put_dossier(tmp_db, Chain.ROBINHOOD, TOKEN)
    _stub_pons(monkeypatch, "pons:rpc_timeout")
    venue = V.read_venue(Chain.ROBINHOOD, TOKEN, tmp_db)
    assert not venue.priced, venue.note


def test_the_fallback_still_needs_a_measured_tax(tmp_db, monkeypatch):
    """Falling back is not inventing. With no tax on the dossier it still refuses."""
    from kaiba.core.schemas import Chain

    _stub_pons(monkeypatch, "pons:factory_unreadable:no attempt made")
    venue = V.read_venue(Chain.ROBINHOOD, TOKEN, tmp_db)
    assert not venue.priced, venue.note


@pytest.mark.parametrize(
    "note",
    [
        "pons:no_curve:no attempt made",
        "pons:graduated:no attempt made",
        "pons:factory_has_no_curve:no attempt made",
    ],
)
def test_a_graduated_note_is_never_relabelled_as_a_limiter_refusal(note):
    """Both reasons can appear in one string, and they must stay distinguishable.

    A permanently-curveless token whose read ALSO got rate-limited is still, first and
    foremost, a token that left the curve -- that is the durable fact and the one an
    operator reading the note needs. The fallback fires either way, so this costs nothing
    at the venue; what it protects is the reporting, and a reason that silently changes
    meaning is how a note stops being evidence.
    """
    assert V._left_the_curve(note)
    assert not V._limiter_refused(note), "a graduated token was relabelled"


# ------------------------------------------------- our own database is also not the venue


@pytest.mark.parametrize(
    "note",
    [
        "pons:factory_unreadable:OperationalError: database is locked",
        "pons:unreadable:OperationalError: database is locked",
        "flap:curve_unreadable:database is locked",
    ],
)
def test_sqlite_write_contention_may_fall_back(note):
    """MEASURED 2026-09-23: the dominant robinhood refusal after the limiter one was fixed.

    A curve READ takes a WRITE first, to reserve the limiter credit, so eleven writers on
    one SQLite file surface as `database is locked` inside a venue note. The database was
    healthy (a passive checkpoint ran in 11 ms, 871 of 872 pages) and so was the chain --
    we just could not get the lock inside the 5 s busy timeout. That says nothing about
    the venue and must not refuse the entry.
    """
    assert V._limiter_refused(note)


def test_a_lock_error_still_needs_the_dossier_tax_to_price(tmp_db, monkeypatch):
    """Falling back is not inventing: with no measured tax it still refuses."""
    from kaiba.core.schemas import Chain

    _stub_pons(monkeypatch, "pons:factory_unreadable:OperationalError: database is locked")
    assert not V.read_venue(Chain.ROBINHOOD, TOKEN, tmp_db).priced


def test_a_lock_error_with_a_measured_tax_prices(tmp_db, monkeypatch):
    from kaiba.core.schemas import Chain

    put_dossier(tmp_db, Chain.ROBINHOOD, TOKEN)
    _stub_pons(monkeypatch, "pons:factory_unreadable:OperationalError: database is locked")
    venue = V.read_venue(Chain.ROBINHOOD, TOKEN, tmp_db)
    assert venue.priced, venue.note
