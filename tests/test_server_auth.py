"""Independent administrator state and browser security with synthetic credentials."""

import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from watt_profile.server.app import create_app, origin_key
from watt_profile.server.auth import COOKIE, LOGIN_COOKIE, MAX_AGE, AdministratorAuth
from watt_profile.server.settings import ServerSettings


@pytest.fixture
def server_settings(postgres_server, tmp_path):
    tmp_path.chmod(0o700)
    password_file = tmp_path / "password"
    password_file.write_text("synthetic-password-long-enough\n")
    password_file.chmod(0o600)
    return ServerSettings.from_env(
        {
            "WATT_SERVER_DATABASE_URL_FILE": str(postgres_server.database_url_file),
            "WATT_SERVER_ADMIN_PASSWORD_FILE": str(password_file),
            "WATT_SERVER_ADMIN_USER": "admin",
            "WATT_SERVER_PUBLIC_ORIGIN": "http://testserver",
            "WATT_SERVER_COOKIE_SECURE": "false",
        }
    )


@pytest.fixture
def client(server_engine, server_settings):
    with TestClient(create_app(server_settings), base_url="http://testserver") as value:
        yield value


def login(client):
    page = client.get("/login")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "synthetic-password-long-enough",
            "csrf": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client.app.state.auth.session(client.cookies[COOKIE]).csrf


def test_state_bounds_expiry_constant_credentials_and_restart():
    now = [0.0]
    auth = AdministratorAuth("admin", "password", clock=lambda: now[0])
    assert auth.authenticate("admin", "password")
    assert not auth.authenticate("other", "password")
    assert not auth.authenticate("admin", "other")
    first, state = auth.issue()
    assert auth.valid_csrf(first, state.csrf)
    for _ in range(32):
        auth.issue()
    assert len(auth.sessions) == 32 and auth.session(first) is None
    current = next(iter(auth.sessions))
    now[0] = MAX_AGE
    assert auth.session(current) is None
    pre, state = auth.login_state()
    now[0] += 900
    assert not auth.valid_csrf(pre, state.csrf, login=True)
    for _ in range(10):
        assert auth.allow_attempt("peer")
    assert not auth.allow_attempt("peer")
    now[0] += 60
    assert auth.allow_attempt("peer")
    for index in range(1025):
        auth.allow_attempt(str(index))
    assert len(auth.attempts) == 1024
    auth.clear()
    assert not auth.sessions and not auth.prelogin and not auth.attempts
    assert AdministratorAuth("admin", "password").session(current) is None


@pytest.mark.parametrize(
    "configured,supplied",
    [
        ("http://EXAMPLE.com:80", "http://example.com"),
        ("https://example.com", "https://EXAMPLE.COM:443"),
        ("http://[::1]:80", "http://[0:0:0:0:0:0:0:1]"),
    ],
)
def test_origin_canonicalization(configured, supplied):
    assert origin_key(configured) == origin_key(supplied)


def test_login_security_cookie_throttle_and_logout(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    page = client.get("/login")
    assert "HttpOnly" in page.headers["set-cookie"]
    assert "SameSite=lax" in page.headers["set-cookie"]
    assert "Secure" not in page.headers["set-cookie"]
    assert LOGIN_COOKIE in client.cookies
    token = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    data = {"username": "admin", "password": "wrong", "csrf": token}
    assert client.post("/login", data=data, headers={"Origin": "http://evil"}).status_code == 403
    assert client.post("/login", data={**data, "csrf": "wrong"}).status_code == 403
    for _ in range(10):
        assert client.post("/login", data=data).status_code == 401
    assert client.post("/login", data=data, headers={"X-Forwarded-For": "new"}).status_code == 429
    client.app.state.auth.attempts.clear()
    csrf = login(client)
    assert LOGIN_COOKIE not in client.cookies
    assert client.get("/").headers["cache-control"] == "no-store"
    assert client.post("/logout", data={"csrf": "bad"}).status_code == 403
    identifier = client.cookies[COOKIE]
    assert client.post("/logout", data={"csrf": csrf}, follow_redirects=False).status_code == 303
    assert client.app.state.auth.session(identifier) is None
    assert client.get("/api/admin/v1/sites").status_code == 401


def test_host_origin_session_and_auth_separation(client):
    assert client.get("/health/live", headers={"Host": "evil"}).status_code == 403
    assert client.get("/health/live", headers={"Host": "testserver:80"}).json() is True
    assert client.get("/health/live", headers={"Host": "testserver:123"}).status_code == 403
    assert (
        client.get(
            "/api/admin/v1/sites", headers={"Authorization": "Bearer " + "a" * 43}
        ).status_code
        == 401
    )
    csrf = login(client)
    assert (
        client.post(
            "/api/admin/v1/sites", json={}, headers={"X-CSRF-Token": csrf, "Origin": "http://evil"}
        ).status_code
        == 403
    )
    assert client.post("/api/admin/v1/registrations/preview", json={}).status_code == 403
    response = client.post("/api/v1/ingest/batches", json={})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credential"
    state = client.app.state.auth
    state.clock = lambda: next(iter(state.sessions.values())).created + MAX_AGE
    assert client.get("/api/admin/v1/sites").status_code == 401


def test_secure_cookies_restart_and_shutdown(server_engine, server_settings):
    app = create_app(
        replace(server_settings, cookie_secure=True, public_origin="https://testserver")
    )
    with TestClient(app, base_url="https://testserver") as client:
        assert "Secure" in client.get("/login").headers["set-cookie"]
        login(client)
        identifier = client.cookies[COOKIE]
    assert app.state.ready is False and not app.state.auth.sessions
    with TestClient(app, base_url="https://testserver") as client:
        client.cookies.set(COOKIE, identifier)
        assert client.get("/api/admin/v1/sites").status_code == 401


@pytest.mark.parametrize(
    "origin", ["https://server.example.invalid", "https://server.example.invalid:8443"]
)
def test_external_https_origin_over_internal_http(server_engine, server_settings, origin):
    """Model incoming upstream HTTP, explicitly carrying external browser cookies.

    No proxy or TLS listener runs. The HTTP client's cookie jar must withhold
    Secure cookies; explicit Cookie headers represent what the upstream received
    from an HTTPS browser and forwarded to the application.
    """
    app = create_app(replace(server_settings, public_origin=origin, cookie_secure=True))
    scopes = []

    class CaptureConnection:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                scopes.append((scope["scheme"], tuple(scope["server"]), scope["client"]))
            await self.app(scope, receive, send)

    app.add_middleware(CaptureConnection)
    authority = urlsplit(origin).netloc
    headers = {"Host": authority, "Origin": origin}
    spoofed = {
        "Forwarded": f'for=192.0.2.1;host="{authority}";proto=https',
        "X-Forwarded-Host": authority,
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Port": str(urlsplit(origin).port or 443),
        "X-Forwarded-For": "192.0.2.1",
    }
    with TestClient(app, base_url="http://internal.example.invalid:8080") as client:
        assert client.get("/health/ready").status_code == 403
        ready = client.get("/health/ready", headers={"Host": authority})
        assert ready.status_code == 200 and ready.json() is True
        redirect = client.get("/", headers=headers, follow_redirects=False)
        assert redirect.status_code == 303 and redirect.headers["location"] == "/login"
        page = client.get("/login", headers=headers)
        assert page.status_code == 200
        assert all(
            part in page.headers["set-cookie"]
            for part in ("Secure", "HttpOnly", "SameSite=lax", "Path=/")
        )
        token = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        data = {
            "username": "admin",
            "password": "synthetic-password-long-enough",
            "csrf": token,
        }
        # A direct HTTP browser does not send the Secure pre-login cookie.
        assert client.post("/login", data=data, headers=headers).status_code == 403
        prelogin = {**headers, "Cookie": f"{LOGIN_COOKIE}={client.cookies[LOGIN_COOKIE]}"}
        for wrong_origin in (
            "https://evil.example.invalid",
            "http://" + authority,
            "https://server.example.invalid:8444",
        ):
            rejected = client.post(
                "/login", data=data, headers={**prelogin, **spoofed, "Origin": wrong_origin}
            )
            assert rejected.status_code == 403
            assert rejected.json()["error"]["code"] == "csrf_rejected"
        logged_in = client.post("/login", data=data, headers=prelogin, follow_redirects=False)
        assert logged_in.status_code == 303 and logged_in.headers["location"] == "/"
        assert all("Secure" in value for value in logged_in.headers.get_list("set-cookie"))
        identifier = client.cookies[COOKIE]
        csrf = app.state.auth.session(identifier).csrf
        authenticated = {**headers, "Cookie": f"{COOKIE}={identifier}", "X-CSRF-Token": csrf}
        assert client.get("/api/admin/v1/sites", headers=headers).status_code == 401
        assert client.get("/", headers=authenticated).status_code == 200
        for wrong_host in ("evil.example.invalid", "server.example.invalid:8444"):
            rejected = client.post(
                "/api/admin/v1/sites",
                json={"site_id": str(uuid4())},
                headers={**authenticated, **spoofed, "Host": wrong_host},
            )
            assert rejected.status_code == 403
            assert rejected.json()["error"]["code"] == "invalid_host"
        if urlsplit(origin).port:
            assert (
                client.get("/health/ready", headers={"Host": "server.example.invalid"}).status_code
                == 403
            )
        assert (
            client.post(
                "/api/admin/v1/sites",
                json={"site_id": str(uuid4())},
                headers={**authenticated, **spoofed, "Origin": "http://" + authority},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/admin/v1/sites",
                json={"site_id": str(uuid4())},
                headers={**authenticated, "X-CSRF-Token": "wrong"},
            ).status_code
            == 403
        )
        created = client.post(
            "/api/admin/v1/sites", json={"site_id": str(uuid4())}, headers=authenticated
        )
        assert created.status_code == 200
        sites = client.get("/api/admin/v1/sites", headers=authenticated)
        assert sites.status_code == 200 and sites.json()["data"]["total"] == 1
        assert sites.headers["cache-control"] == "no-store"
        logout = client.post(
            "/logout", data={"csrf": csrf}, headers=authenticated, follow_redirects=False
        )
        assert logout.status_code == 303 and logout.headers["location"] == "/login"
        assert "Secure" in logout.headers["set-cookie"]
        assert client.get("/api/admin/v1/sites", headers=authenticated).status_code == 401
    assert scopes and all(scheme == "http" for scheme, _, _ in scopes)
    assert all(server == ("internal.example.invalid", 8080) for _, server, _ in scopes)
    assert all(peer[0] != "192.0.2.1" for _, _, peer in scopes)


def test_construction_has_no_bootstrap_or_file_side_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("construction performed I/O")

    monkeypatch.setattr("watt_profile.server.app.initialize_storage", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    app = create_app()
    assert app.state.ready is False


def test_internal_health_probe_uses_public_host(server_engine, server_settings):
    settings = replace(server_settings, public_origin="https://example.org")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8080") as client:
        assert client.get("/health/ready").status_code == 403
        response = client.get("/health/ready", headers={"Host": "example.org"})
        assert response.status_code == 200 and response.json() is True
        assert client.get("/health/live", headers={"Host": "example.org:443"}).json() is True
        assert (
            client.get(
                "/health/live",
                headers={"Host": "127.0.0.1:8080", "X-Forwarded-Host": "example.org"},
            ).status_code
            == 403
        )


def test_startup_error_does_not_expose_driver_exception(server_settings, monkeypatch):
    import traceback

    def fail(*args):
        raise ValueError("SECRET connection string")

    monkeypatch.setattr("watt_profile.server.app.initialize_storage", fail)
    app = create_app(server_settings)
    with pytest.raises(RuntimeError) as caught, TestClient(app):
        pass
    assert "SECRET" not in "".join(traceback.format_exception(caught.value))
    assert app.state.ready is False
