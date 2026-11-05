"""A position the venue says we hold NONE of stops eating the protection budget.

MEASURED 2026-09-24: five live positions -- four robinhood, one sol -- carried a ledger
quantity in the hundreds of trillions of base units against an on-chain balance of
exactly ZERO, stable across reads twenty seconds apart. The same reader returned the
exact non-zero balance for the two positions that DO hold their tokens, which is what
rules out a lying reader. Their sells failed on `error=40002700 message=GetSwapRouteError`
and they had gone 21-46 hours without a trade or a usable price.

`_write_off_dust` correctly refuses them (a FULL position is not dust), so they retried
forever and every tick still spent three provider calls pricing each one. This book has
already halted entries on EVERY chain once through `protection_overrun`.

The deferral is rate limiting ONLY. Nothing closes, nothing is realised, the cost stays
encumbered, and an exit request or the hourly recheck brings it straight back.
"""
from kaiba.execution.watchdog import STRANDED_MIN_ATTEMPTS, STRANDED_RECHECK_S, TickReport


def test_the_thresholds_are_sane():
    assert STRANDED_MIN_ATTEMPTS >= 3, "must not defer before the dust path has had a go"
    assert STRANDED_RECHECK_S >= 600, "rechecking too often defeats the purpose"
    assert STRANDED_RECHECK_S <= 86400, "a token that revives must be found the same day"


def test_deferred_is_on_the_heartbeat_under_its_own_name():
    """A deferred position must never become invisible, and must not be conflated.

    `TickReport.deferred` already existed and counts SHADOW QUOTE requests deferred while
    live protection has priority. Reusing it for a live position would have hidden a
    stranded position inside a paper-work counter -- on the very heartbeat meant to keep
    it visible. They are separate fields with separate keys.
    """
    report = TickReport()
    assert report.stranded_deferred == 0
    assert report.deferred == 0
    report.stranded_deferred = 3
    payload = report.as_payload()
    assert payload["stranded_deferred"] == 3
    assert payload["deferred_requests"] == 0, "the shadow-request counter must not move"


def test_the_dust_path_still_runs_first():
    """Deferral must not pre-empt a legitimate dust write-off."""
    import inspect

    from kaiba.execution import watchdog

    src = inspect.getsource(watchdog)
    dust = src.index("self._write_off_dust(position, state)")
    defer = src.index("self._defer_stranded(position, state)")
    assert dust < defer, "the dust write-off must be attempted before deferring"


def test_deferral_is_only_reachable_from_the_wallet_empty_branch():
    """It must never be possible to hide a position we can still see."""
    import inspect

    from kaiba.execution import watchdog

    src = inspect.getsource(watchdog)
    line = next(
        row for row in src.splitlines()
        if "self._defer_stranded(" in row and "def " not in row
    )
    idx = src.splitlines().index(line)
    guard = "\n".join(src.splitlines()[max(0, idx - 3):idx])
    assert "wallet_empty" in guard, f"deferral not guarded by wallet_empty: {guard!r}"


def test_an_exit_request_is_never_deferred():
    """An operator asking for an exit must reach the normal path on the NEXT tick."""
    import inspect

    from kaiba.execution import watchdog

    src = inspect.getsource(watchdog.Watchdog.tick)
    assert "asked_for" in src
    assert "candidate.position_id in asked_for" in src


def test_nothing_in_the_deferral_closes_or_realises():
    """The safety property: rate limiting only."""
    import ast
    import inspect
    import textwrap

    from kaiba.execution import watchdog

    src = textwrap.dedent(inspect.getsource(watchdog.Watchdog._defer_stranded))
    fn = ast.parse(src).body[0]
    # Drop the docstring: it DESCRIBES what this must not do, so scanning it is a false
    # positive. The code body is what binds.
    body = fn.body[1:] if (
        isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant)
    ) else fn.body
    code = "\n".join(ast.unparse(node) for node in body)
    for forbidden in ("write_off", "closed_ms", "proceeds", "realized", "apply_fill"):
        assert forbidden not in code, f"_defer_stranded must not touch {forbidden}"
