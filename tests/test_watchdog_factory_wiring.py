"""The production wiring of ``prefetch_source_factory``, which nothing else covers.

WHY THIS FILE EXISTS. `Watchdog._prefetch_quotes` prices the book across
QUOTE_PREFETCH_WORKERS threads. The conn-bound price sources
(`curve_price.curve_price_source`, `evm_price.evm_price_source`, `venue_price_source`)
capture `db.get_conn()` at CONSTRUCTION, so one source built on the service thread hands a
single SQLite connection to eight workers. `db.get_conn` is thread-local, so building the
source INSIDE the worker is what makes its connection private -- which is the whole point
of the factory.

The `Watchdog` side of that is covered by `tests/test_watchdog_connection_isolation.py`.
The SERVICE side was not: `run_watchdog` is what the live box actually calls, and a
mutation that simply stopped it passing the factory SURVIVED the whole suite. That is the
worst shape of gap -- the unit is correct, the wiring is dead, and every test still
passes while the live box quietly shares one connection across eight threads again.

Found by the adversarial mutation pass on 2026-09-22, reported as a surviving mutant.
"""

from __future__ import annotations

from kaiba.execution import watchdog as W


class _Recorder:
    """Captures the kwargs `run_watchdog` builds its Watchdog with, then stops the loop."""

    instances: list[dict] = []

    def __init__(self, conn=None, **kwargs):
        _Recorder.instances.append(kwargs)
        self.conn = conn

    def run(self, *args, **kwargs):
        return {"ticks": 0, "note": "recorder"}

    def __getattr__(self, name):
        # `run_watchdog` does housekeeping on the instance it builds (`_emit`, signal
        # wiring). None of that is what this file is about, so absorb it rather than
        # mirroring the real class and having to chase it every time that changes.
        return lambda *a, **k: None


class _Source:
    def quote(self, *args, **kwargs):  # pragma: no cover - never called
        return None


def _run(monkeypatch, tmp_db, **kwargs) -> dict:
    _Recorder.instances.clear()
    monkeypatch.setattr(W, "Watchdog", _Recorder)
    # `interval_s` is supplied so the startup event does not reach for `dog.config()`;
    # this file is about the constructor kwargs, not the loop.
    W.run_watchdog(tmp_db, max_ticks=0, install_signals=False, interval_s=1, **kwargs)
    assert _Recorder.instances, "run_watchdog must construct a Watchdog"
    return _Recorder.instances[-1]


def test_the_service_wires_a_factory_when_it_resolves_the_source_itself(monkeypatch, tmp_db):
    """The live path. kaiba-protection passes no price_source, so this is what runs."""
    monkeypatch.setattr(W, "configured_price_source_name", lambda *a, **k: "curve")
    monkeypatch.setattr(W, "resolve_price_source", lambda name: _Source())

    built = _run(monkeypatch, tmp_db)

    factory = built.get("prefetch_source_factory")
    assert factory is not None, (
        "run_watchdog resolved the price source itself and must wire the matching "
        "per-worker factory; without it eight threads share one SQLite connection"
    )
    assert callable(factory)
    assert isinstance(factory(), _Source), "the factory must build the CONFIGURED source"


def test_an_injected_source_is_left_alone(monkeypatch, tmp_db):
    """A caller that chose its own source owns that decision, including the sharing."""
    built = _run(monkeypatch, tmp_db, price_source=_Source())
    assert built.get("prefetch_source_factory") is None, (
        "injecting a price_source must not silently acquire a factory the caller did not ask for"
    )


def test_an_explicit_factory_is_never_overwritten(monkeypatch, tmp_db):
    monkeypatch.setattr(W, "configured_price_source_name", lambda *a, **k: "curve")
    monkeypatch.setattr(W, "resolve_price_source", lambda name: _Source())
    mine = lambda: _Source()  # noqa: E731 - identity is the whole assertion

    built = _run(monkeypatch, tmp_db, prefetch_source_factory=mine)

    assert built.get("prefetch_source_factory") is mine


def test_the_factory_builder_resolves_in_the_calling_thread(monkeypatch):
    """`prefetch_source_factory_for` must defer resolution, not capture one source.

    A builder that resolved eagerly would hand every worker the same object and the
    connection would be shared again, with the factory present and useless.
    """
    seen: list[str] = []

    def fake_resolve(name):
        seen.append(name)
        return _Source()

    monkeypatch.setattr(W, "resolve_price_source", fake_resolve)
    factory = W.prefetch_source_factory_for("curve")
    assert seen == [], "building the factory must not resolve anything yet"
    first, second = factory(), factory()
    assert seen == ["curve", "curve"], seen
    assert first is not second, "each call must build a NEW source, not reuse one"
