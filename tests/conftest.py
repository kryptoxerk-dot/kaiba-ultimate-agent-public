"""Keep the default suite offline as required by the repository contract.

The test process must not depend on permissions or files in the operator's user
profile.  Pytest imports this file before it imports test modules, so establish
workspace-local paths here before any Kaiba settings module is imported.
"""

import os
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYTEST_CONFIG = _REPO_ROOT / "data" / "pytest" / "config"
_PYTEST_CONFIG.mkdir(parents=True, exist_ok=True)

# Keep Settings construction independent of the operator's profile. This part is safe and
# stays.
os.environ.setdefault("KAIBA_CONFIG_DIR", str(_PYTEST_CONFIG))

# Tests run against a fixture config with fake wallets and enabled chains, so the shipped
# config/ can stay a locked-down paper-mode template a fresh install cannot trade from.
_FIXTURE_CONFIG = _REPO_ROOT / "tests" / "fixtures" / "config"
os.environ.setdefault("KAIBA_RISK_PATH", str(_FIXTURE_CONFIG / "risk.yaml"))
os.environ.setdefault("KAIBA_SIGNER_POLICY_PATH", str(_FIXTURE_CONFIG / "signer-policy.yaml"))

# Deliberately NOT redirected: TEMP, TMP and tempfile.tempdir.
#
# Pointing pytest's tmp_path into data/pytest/tmp was added to work around an
# inaccessible host TEMP on one machine. On this one it did the opposite: pytest's
# `pytest-of-<user>` directory under the repo ended up in a state where even reading its
# ACL is denied, and every one of 1,490 tests errored at fixture setup with WinError 5.
# The directory cannot be removed or inspected, so the workaround is strictly worse than
# the problem it solved — a poisoned directory inside the repo blocks every future run,
# whereas the system temp is cleaned by the OS.
#
# If a host genuinely cannot use the system temp, set PYTEST_DEBUG_TEMPROOT to a writable
# path in that host's environment. Do not hardcode a repo path here again.


def pytest_collection_modifyitems(items):
    if os.environ.get("KAIBA_LIVE_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="Live provider tests require KAIBA_LIVE_TESTS=1")
    for item in items:
        if item.get_closest_marker("live"):
            item.add_marker(skip)

@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """Point every test at its own data directory, whether or not it asks for one.

    Two reasons this is autouse rather than opt-in. First, the provider cache in
    ``kaiba/providers/_http.py`` lives under the data dir, so tests that did not request
    ``tmp_db`` were sharing one on-disk cache across the whole session: the gmgn cache
    tests passed alone and failed in the suite because an earlier file had already written
    the entries they were asserting were absent. Second, and worse, the real
    ``data/kaiba.db`` holds the operator's imported wallets, and nothing structural stopped
    a stray test from writing to it.
    """
    monkeypatch.setenv("KAIBA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KAIBA_DB_PATH", str(tmp_path / "kaiba.db"))
    from kaiba.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """A migrated database per test, isolated from the developer's real data dir."""
    from kaiba.core import db

    path = tmp_path / "kaiba.db"
    monkeypatch.setenv("KAIBA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KAIBA_DB_PATH", str(path))
    from kaiba.core.config import get_settings

    get_settings.cache_clear()
    db.close_thread_conn()
    conn = db.connect(path)
    db.migrate(conn)
    monkeypatch.setattr(db, "get_conn", lambda path=None: conn)
    try:
        yield conn
    finally:
        conn.close()
        db.close_thread_conn()
        get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_protectability_cache():
    """No test may inherit another test's protectability verdict.

    `viability._protectable_cache` is process-global and deliberately caches BOTH
    outcomes -- a positive for `PROTECTABLE_TTL_S` and a negative for the shorter
    `PROTECTABLE_RETRY_S` -- because whether the watchdog can price an asset is a property
    of the pair set, not of the minute.

    That is right in production and poison across tests: a negative verdict cached by one
    test made two unrelated tests in `test_paper.py` fail while each passed alone. The
    cache is reset either side rather than only before, so a test that populates it cannot
    leak into whatever runs next even if it fails mid-way.
    """
    try:
        from kaiba.execution.viability import reset_protectable_cache
    except Exception:  # pragma: no cover - the module is always importable in this tree
        yield
        return
    reset_protectable_cache()
    yield
    reset_protectable_cache()
