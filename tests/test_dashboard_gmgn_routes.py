"""Offline controls for the optional account route handoff; no app/core wiring."""

from __future__ import annotations

import ast
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from dashboard import auth
from dashboard import gmgn_account_routes as routes


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


def projection(**changes):
    result = {
        "provider": "gmgn", "scope": "account_visibility_only", "configured": None,
        "status": "connected", "checked_at_ms": 100_000, "observed_at_ms": 99_000,
        "age_ms": 1_000, "fresh": True, "freshness_budget_ms": 60_000, "basis": "live",
        "wallets": [{"chain": "sol", "address": "fixture-address", "native_symbol": "SOL",
                     "native_balance": "1.25", "balance_status": "reported"}],
        "wallet_count": 1, "omitted_count": 0, "truncated": False, "reason": None,
    }
    result.update(changes)
    return result


@pytest.fixture
def setup_routes():
    clock = Clock()
    calls = {"reader": 0, "auth": 0, "csrf": 0}

    def reader():
        calls["reader"] += 1
        return projection()

    def authenticate(request):
        calls["auth"] += 1
        if request.headers.get("Authorization") != "Bearer fixture-auth":
            raise HTTPException(401, "fixture-auth-secret")
        return "fixture-context"

    def csrf(request, context, token):
        calls["csrf"] += 1
        assert context == "fixture-context"
        if (token or request.headers.get("X-CSRF-Token")) != "fixture-csrf":
            raise HTTPException(403, "fixture-csrf-secret")

    def build(use_reader=reader, **kwargs):
        app = FastAPI()
        routes.install(app, use_reader, authenticate=authenticate, csrf=csrf, clock=clock, **kwargs)
        return TestClient(app)

    return build, calls, clock


AUTH = {"Authorization": "Bearer fixture-auth"}
HEADERS = {**AUTH, "X-CSRF-Token": "fixture-csrf"}


def assert_private(response):
    assert response.headers["cache-control"] == "no-store, private, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["expires"] == "0"
    assert "Cookie" in response.headers["vary"]
    assert "Authorization" in response.headers["vary"]


def test_install_and_get_never_check_provider(setup_routes):
    build, calls, _ = setup_routes
    with build() as client:
        for _ in range(2):
            result = client.get(routes.ACCOUNT_PATH, headers=AUTH)
            assert result.status_code == 200
            data = result.json()
            assert data["status"] == "not_checked"
            assert data["reason"] == "explicit_check_required"
            assert data["wallets"] == [] and data["wallet_count"] == 0
            assert data["checked_at_ms"] is data["fresh"] is None
            assert data["check"]["automatic_refresh"] is False
            assert data["check"]["snapshot_age_ms"] is None
            assert_private(result)
    assert calls == {"reader": 0, "auth": 2, "csrf": 0}


@pytest.mark.parametrize("method,path", [("get", routes.ACCOUNT_PATH), ("post", routes.CHECK_PATH)])
def test_authentication_precedes_every_read_and_check(setup_routes, method, path):
    build, calls, _ = setup_routes
    with build() as client:
        result = getattr(client, method)(path)
        assert result.status_code == 401
        assert "secret" not in result.text
        assert_private(result)
    assert calls["reader"] == calls["csrf"] == 0


@pytest.mark.parametrize("form", [False, True])
def test_explicit_authenticated_csrf_check_and_cached_get(setup_routes, form):
    build, calls, clock = setup_routes
    with build() as client:
        bad = client.post(routes.CHECK_PATH, headers=AUTH)
        assert bad.status_code == 403
        assert_private(bad)
        assert calls["reader"] == 0
        options = {"headers": AUTH, "data": {"_csrf": "fixture-csrf"}} if form else {"headers": HEADERS}
        result = client.post(routes.CHECK_PATH, **options)
        assert result.status_code == 200
        assert result.json()["status"] == "connected"
        assert result.json()["wallets"][0]["native_balance"] == "1.25"
        assert result.json()["configured"] is None
        assert_private(result)
        clock.value += 1
        cached = client.get(routes.ACCOUNT_PATH, headers=AUTH)
        assert cached.json()["age_ms"] == 2_000
        assert cached.json()["check"]["snapshot_age_ms"] == 1_000
        assert_private(cached)
    assert calls["reader"] == 1


@pytest.mark.parametrize("params", [{"address": "fixture-address"}, {"chain": "sol"}, {"refresh": "1"}])
def test_no_query_arguments_can_select_account_or_trigger_refresh(setup_routes, params):
    build, calls, _ = setup_routes
    with build() as client:
        for method, path in [("get", routes.ACCOUNT_PATH), ("post", routes.CHECK_PATH)]:
            result = getattr(client, method)(path, headers=HEADERS, params=params)
            assert result.status_code == 400
            assert_private(result)
    assert calls["reader"] == 0


@pytest.mark.parametrize("kwargs,status", [
    ({"json": {"api_key": "fixture-credential"}}, 415),
    ({"data": {"api_key": "fixture-credential"}}, 400),
    ({"data": {"_csrf": "fixture-csrf", "address": "fixture-address"}}, 400),
    ({"content": b"_csrf=one&_csrf=two", "headers": {"Content-Type": "application/x-www-form-urlencoded"}}, 400),
    ({"content": b"_csrf", "headers": {"Content-Type": "application/x-www-form-urlencoded"}}, 400),
    ({"content": b"_csrf=%FF", "headers": {"Content-Type": "application/x-www-form-urlencoded"}}, 400),
    ({"content": b"x" * (routes.MAX_BODY_BYTES + 1)}, 413),
])
def test_only_bounded_csrf_form_input_is_accepted(setup_routes, kwargs, status):
    build, calls, _ = setup_routes
    options = dict(kwargs)
    options["headers"] = {**HEADERS, **options.get("headers", {})}
    with build() as client:
        result = client.post(routes.CHECK_PATH, **options)
        assert result.status_code == status
        assert "fixture-credential" not in result.text
        assert_private(result)
        # Invalid input does not consume the explicit check's allowance.
        valid = client.post(routes.CHECK_PATH, headers=HEADERS)
        assert valid.status_code == 200
    assert calls["reader"] == 1


def test_cooldown_has_positive_control_at_exact_boundary(setup_routes):
    build, calls, clock = setup_routes
    with build() as client:
        assert client.post(routes.CHECK_PATH, headers=HEADERS).status_code == 200
        clock.value += 29.5
        blocked = client.post(routes.CHECK_PATH, headers=HEADERS)
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"] == "1"
        assert_private(blocked)
        assert calls["reader"] == 1
        clock.value += 0.5
        assert client.post(routes.CHECK_PATH, headers=HEADERS).status_code == 200
    assert calls["reader"] == 2


@pytest.mark.parametrize("failure", ["exception", "non_mapping", "nan"])
def test_failed_checks_are_safe_and_do_not_refund_cooldown(setup_routes, failure):
    build, calls, _ = setup_routes

    def broken():
        calls["reader"] += 1
        if failure == "exception":
            raise RuntimeError("fixture-provider-secret")
        if failure == "non_mapping":
            return []
        return {"age_ms": float("nan")}

    with build(broken) as client:
        result = client.post(routes.CHECK_PATH, headers=HEADERS)
        assert result.status_code == 503
        assert result.json()["status"] == "unavailable"
        assert result.json()["reason"] == "account_check_failed"
        assert "secret" not in result.text
        assert_private(result)
        assert client.post(routes.CHECK_PATH, headers=HEADERS).status_code == 429
        assert client.get(routes.ACCOUNT_PATH, headers=AUTH).json()["status"] == "unavailable"
    assert calls["reader"] == 1


def test_concurrent_check_is_refused_and_get_remains_available(setup_routes):
    build, calls, _ = setup_routes
    entered = threading.Event()
    release = threading.Event()

    def slow():
        calls["reader"] += 1
        entered.set()
        assert release.wait(5), "test must release the reader"
        return projection()

    with build(slow) as client, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, routes.CHECK_PATH, headers=HEADERS)
        try:
            assert entered.wait(5)
            result = client.get(routes.ACCOUNT_PATH, headers=AUTH)
            assert result.status_code == 200
            assert result.json()["check"]["in_progress"] is True
            assert result.json()["status"] == "not_checked"
            blocked = client.post(routes.CHECK_PATH, headers=HEADERS)
            assert blocked.status_code == 409
            assert_private(blocked)
            assert calls["reader"] == 1
        finally:
            release.set()
        assert future.result(timeout=5).status_code == 200
        assert client.get(routes.ACCOUNT_PATH, headers=AUTH).json()["check"]["in_progress"] is False
        assert client.post(routes.CHECK_PATH, headers=HEADERS).status_code == 429


def test_cached_connected_projection_expires_without_provider_calls(setup_routes):
    build, calls, clock = setup_routes
    with build() as client:
        assert client.post(routes.CHECK_PATH, headers=HEADERS).json()["fresh"] is True
        clock.value += 59
        assert client.get(routes.ACCOUNT_PATH, headers=AUTH).json()["status"] == "connected"
        clock.value += 0.001
        result = client.get(routes.ACCOUNT_PATH, headers=AUTH).json()
        assert result["status"] == "stale"
        assert result["fresh"] is False
        assert result["reason"] == "cached_observation_expired"
        assert result["wallets"][0]["native_balance"] == "1.25"
    assert calls["reader"] == 1


def test_cached_projection_is_detached_from_mutable_reader_result(setup_routes):
    build, _, _ = setup_routes
    original = projection()
    with build(lambda: original) as client:
        client.post(routes.CHECK_PATH, headers=HEADERS)
        original["wallets"][0]["native_balance"] = "999"
        assert client.get(routes.ACCOUNT_PATH, headers=AUTH).json()["wallets"][0]["native_balance"] == "1.25"


@pytest.mark.parametrize("form", [False, True])
def test_existing_dashboard_session_and_csrf_signature_are_compatible(form):
    app = FastAPI()
    password = "synthetic-dashboard-password"
    session = auth.issue_session(password)
    token = auth.csrf_token(session, password)
    calls = []

    def authenticate(request):
        if not auth.verify_session(request.cookies.get(auth.COOKIE_NAME), password):
            raise HTTPException(401)
        return password

    def csrf(request, context, given):
        supplied = given or request.headers.get(auth.CSRF_HEADER)
        if not auth.verify_csrf(supplied, request.cookies.get(auth.COOKIE_NAME), context):
            raise HTTPException(403)

    def reader():
        calls.append("checked")
        return projection()

    routes.install(app, reader, authenticate=authenticate, csrf=csrf)
    with TestClient(app) as client:
        assert client.post(routes.CHECK_PATH).status_code == 401
        client.cookies.set(auth.COOKIE_NAME, session)
        assert client.post(routes.CHECK_PATH).status_code == 403
        # A correct token for a different session must fail, then the exact same
        # request with the actual page/session token must reach the reader.
        other = auth.csrf_token(auth.issue_session(password), password)
        assert client.post(routes.CHECK_PATH, headers={auth.CSRF_HEADER: other}).status_code == 403
        options = {"data": {"_csrf": token}} if form else {"headers": {auth.CSRF_HEADER: token}}
        assert client.post(routes.CHECK_PATH, **options).status_code == 200
        assert client.get(routes.ACCOUNT_PATH).status_code == 200
    assert calls == ["checked"]


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True, "30"])
def test_invalid_cooldown_cannot_disable_budget_guard(value):
    with pytest.raises(ValueError, match="cooldown_s"):
        routes.install(FastAPI(), lambda: projection(), authenticate=lambda r: "", csrf=lambda *a: None,
                       cooldown_s=value)


def test_installer_refuses_path_collision_without_reading_provider():
    app = FastAPI()
    app.add_api_route(routes.ACCOUNT_PATH, lambda: None)
    with pytest.raises(ValueError, match="already installed"):
        routes.install(app, lambda: pytest.fail("no provider during install"),
                       authenticate=lambda r: "", csrf=lambda *a: None)


def test_module_has_no_production_app_or_core_import_or_mutating_route():
    tree = ast.parse(inspect.getsource(routes))
    imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not any(name and (name.startswith("kaiba") or name == "dashboard.app") for name in imports)
    app = FastAPI()
    routes.install(app, lambda: projection(), authenticate=lambda r: "", csrf=lambda *a: None)
    actual = {(route.path, tuple(sorted(route.methods))) for route in app.routes if route.path.startswith(routes.ACCOUNT_PATH)}
    assert actual == {(routes.ACCOUNT_PATH, ("GET",)), (routes.CHECK_PATH, ("POST",))}
