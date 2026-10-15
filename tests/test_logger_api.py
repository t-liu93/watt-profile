"""Local HTTP acceptance uses temporary credentials/SQLite and fake serial only."""

from __future__ import annotations

import asyncio
import queue
import re
import socket
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from watt_profile.cli import logger as cli
from watt_profile.logger.app import PREFIX, create_app
from watt_profile.logger.auth import COOKIE, MAX_AGE, OperatorAuth
from watt_profile.logger.bootstrap import InstanceBusy, installation_lock
from watt_profile.logger.settings import LoggerSettings

PASSWORD = "temporary-operator-password"
FRAME = Path("tests/fixtures/p1/profile/single_phase.telegram").read_bytes()


class Clock:
    def __init__(self):
        self.now = 0.0
        self.condition = threading.Condition()

    def monotonic(self):
        return self.now

    def utc_now(self):
        return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=self.now)

    def advance(self, seconds):
        with self.condition:
            self.now += seconds
            self.condition.notify_all()

    def wait_interruptible(self, seconds, event):
        deadline = self.now + seconds
        with self.condition:
            while self.now < deadline and not event.is_set():
                self.condition.wait(0.01)


class Port:
    def __init__(self):
        self.chunks = queue.Queue()
        self.closed = False

    def read(self, size):
        try:
            return self.chunks.get(timeout=0.01)
        except queue.Empty:
            return b""

    def close(self):
        self.closed = True
        self.chunks.put(b"")


class Factory:
    def __init__(self):
        self.ports = []

    def __call__(self, config):
        assert config.path == "/temporary-fake-serial"
        assert all(p.closed for p in self.ports)
        port = Port()
        self.ports.append(port)
        return port


@pytest.fixture
def setup(tmp_path, monkeypatch):
    password = tmp_path / "operator-password"
    password.write_text(PASSWORD + "\n")
    settings = LoggerSettings.from_env(
        {
            "WATT_LOGGER_DATA_DIR": str(tmp_path / "data"),
            "WATT_LOGGER_OPERATOR_USER": "operator",
            "WATT_LOGGER_OPERATOR_PASSWORD_FILE": str(password),
        }
    )

    def forbidden_network(*args, **kwargs):
        pytest.fail("Local Logger made an outbound network/upload call")

    monkeypatch.setattr(socket.socket, "connect", forbidden_network)
    return settings, Factory(), Clock(), [0.0]


@asynccontextmanager
async def client_for(setup, *, secure=False):
    settings, factory, clock, auth_time = setup
    if secure:
        from dataclasses import replace

        settings = replace(settings, cookie_secure=True)
    app = create_app(settings, serial_factory=factory, clock=clock, auth_clock=lambda: auth_time[0])
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://logger.test" if secure else "http://logger.test"
        ) as client:
            yield app, client


async def login(client, password=PASSWORD):
    page = await client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
    response = await client.post(
        "/login",
        data={
            "username": "operator",
            "password": password,
            "csrf_token": csrf,
        },
    )
    return response


async def headers(client):
    page = await client.get("/")
    return {"X-CSRF-Token": re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]}


async def deploy(client, csrf, expected=None, site_id=None, confirm=False):
    if site_id is None:
        site_id = (await client.post(PREFIX + "/sites", json={}, headers=csrf)).json()["data"][
            "site_id"
        ]
    response = await client.post(
        PREFIX + "/deployment",
        json={
            "site_id": site_id,
            "expected_session_id": expected,
            "confirm_switch": confirm,
        },
        headers=csrf,
    )
    return response, site_id


async def eventually(check):
    deadline = time.monotonic() + 2
    while not check():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.005)


def test_auth_cookie_expiry_logout_restart(setup):
    async def check():
        async with client_for(setup, secure=True) as (app, client):
            assert (await client.get(PREFIX + "/status")).status_code == 401
            assert (await client.get("/")).headers["location"] == "/login"
            response = await login(client)
            assert response.status_code == 303
            cookie = response.headers.get_list("set-cookie")[-1]
            assert all(
                value in cookie
                for value in ["HttpOnly", "SameSite=lax", "Secure", "Max-Age=28800", "Path=/"]
            )
            csrf = await headers(client)
            identifier = client.cookies[COOKIE]
            assert len(identifier) == 43
            setup[3][0] = MAX_AGE
            assert (await client.get(PREFIX + "/status")).status_code == 401
            setup[3][0] += 1
            await login(client)
            csrf = await headers(client)
            response = await client.post("/logout", data={"csrf_token": csrf["X-CSRF-Token"]})
            assert response.status_code == 303
            assert (await client.get(PREFIX + "/status")).status_code == 401
            await login(client)
            saved_cookie = client.cookies[COOKIE]
        assert not app.state.auth.sessions
        async with client_for(setup, secure=True) as (_, client):
            client.cookies.set(COOKIE, saved_cookie)
            assert (await client.get(PREFIX + "/status")).status_code == 401

    asyncio.run(check())


def test_session_capacity_credentials_and_independent_csrf():
    clock = [0.0]
    auth = OperatorAuth("operator", PASSWORD, lambda: clock[0])
    first, _ = auth.issue()
    tokens = [auth.issue() for _ in range(32)]
    assert auth.session(first) is None
    assert len(auth.sessions) == 32
    assert tokens[0][0] != tokens[0][1].csrf
    assert auth.authenticate("operator", PASSWORD)
    assert not auth.authenticate("other", PASSWORD)
    assert not auth.authenticate("operator", "wrong")
    login_id, login_state = auth.login_state()
    assert not auth.valid_csrf(tokens[0][0], login_state.csrf)
    clock[0] = 900
    assert not auth.valid_csrf(login_id, login_state.csrf, login=True)
    for address in range(1100):
        auth.allow_attempt(str(address))
    assert len(auth.attempts) == 1024
    for _ in range(300):
        auth.login_state()
    assert len(auth.prelogin) == 256


def test_login_throttle_generic_errors_and_forwarded_headers(setup):
    async def check():
        async with client_for(setup) as (_, client):
            page = await client.get("/login")
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
            values = {
                "username": "<script>operator</script>",
                "password": "sensitive-input",
                "csrf_token": csrf,
            }
            for n in range(10):
                response = await client.post(
                    "/login", data=values, headers={"X-Forwarded-For": f"192.0.2.{n}"}
                )
                assert response.status_code == 401
                assert "Login failed." in response.text
                assert values["username"] not in response.text
                assert "&lt;script&gt;operator&lt;/script&gt;" not in response.text
                assert "sensitive-input" not in response.text
            assert (await client.post("/login", data=values)).status_code == 429
            setup[3][0] = 60
            assert (await client.post("/login", data=values)).status_code == 401

    asyncio.run(check())


@pytest.mark.parametrize(
    "path,body",
    [
        ("/sites", {}),
        ("/deployment", {}),
        ("/preview", {}),
        ("/meter-change/confirm", {}),
        ("/storage/resolve", {}),
        ("/registration-summary", {}),
        ("/cleanup", {}),
        (f"/sessions/{uuid4()}/start", {}),
        (f"/sessions/{uuid4()}/pause", {}),
        (f"/sessions/{uuid4()}/complete", {}),
    ],
)
def test_every_mutation_requires_csrf(setup, path, body):
    async def check():
        async with client_for(setup) as (_, client):
            await login(client)
            for token in (None, "invalid"):
                response = await client.post(
                    PREFIX + path,
                    json=body,
                    headers={} if token is None else {"X-CSRF-Token": token},
                )
                assert response.status_code == 403
                assert response.headers["cache-control"] == "no-store"

    asyncio.run(check())


def test_csrf_login_logout_patch_and_origin(setup):
    async def check():
        async with client_for(setup) as (_, client):
            assert (
                await client.post(
                    "/login",
                    data={"username": "operator", "password": PASSWORD, "csrf_token": "wrong"},
                )
            ).status_code == 403
            await login(client)
            csrf = await headers(client)
            for origin in (
                "https://evil.test",
                "null",
                "http://logger.test.evil",
                "http://logger.test/",
            ):
                response = await client.post(
                    PREFIX + "/sites", json={}, headers=csrf | {"Origin": origin}
                )
                assert response.status_code == 403
            assert (
                await client.post(
                    PREFIX + "/sites", json={}, headers=csrf | {"Origin": "http://logger.test:80"}
                )
            ).status_code == 200
            assert (
                await client.patch(PREFIX + "/config", json={"expected_session_id": None})
            ).status_code == 403
            assert (await client.patch(PREFIX + f"/sites/{uuid4()}", json={})).status_code == 403
            assert (await client.post("/logout", data={"csrf_token": "wrong"})).status_code == 403
            assert (await client.get(PREFIX + "/status")).status_code == 200

    asyncio.run(check())


def test_api_validation_safe_errors_and_registration_privacy(setup, caplog):
    async def check():
        async with client_for(setup) as (_, client):
            await login(client)
            csrf = await headers(client)
            dangerous = "<script>private-text</script>"
            bad = await client.post(PREFIX + "/sites", json={"unknown": dangerous}, headers=csrf)
            assert bad.status_code == 422
            assert dangerous not in bad.text and "unknown" not in bad.text
            bad = await client.post(
                PREFIX + "/sites",
                content='{"bad":',
                headers=csrf | {"Content-Type": "application/json"},
            )
            assert bad.status_code == 422 and '"bad"' not in bad.text
            site = (
                await client.post(PREFIX + "/sites", json={"address": dangerous}, headers=csrf)
            ).json()["data"]
            reveal = await client.post(
                PREFIX + "/registration-summary", json={"site_id": site["site_id"]}, headers=csrf
            )
            assert reveal.status_code == 200 and reveal.headers["cache-control"] == "no-store"
            token = reveal.json()["data"]["token"]
            assert len(token) == 43
            for path in ("/status", "/config", "/sites", "/sessions"):
                response = await client.get(PREFIX + path)
                assert token not in response.text and PASSWORD not in response.text
                assert "fingerprint" not in response.text
                assert response.headers["cache-control"] == "no-store"
            assert token not in caplog.text and PASSWORD not in caplog.text
            missing = await client.post(
                PREFIX + "/registration-summary", json={"site_id": str(uuid4())}, headers=csrf
            )
            assert missing.status_code == 404
            assert (await client.get(PREFIX + "/sites?limit=101")).status_code == 422

    asyncio.run(check())


def test_deployment_config_stale_selection_and_idempotency(setup):
    async def check():
        async with client_for(setup) as (_, client):
            await login(client)
            csrf = await headers(client)
            response, site = await deploy(client, csrf)
            assert response.status_code == 200
            sid = response.json()["data"]["logger"]["session_id"]
            response, _ = await deploy(client, csrf, site_id=site)
            assert (
                response.status_code == 409 and response.json()["error"]["code"] == "stale_session"
            )
            original = (await client.get(PREFIX + "/config")).json()["data"]
            response = await client.patch(
                PREFIX + "/config",
                json={"expected_session_id": None, "serial_path": "/temporary-fake-serial"},
                headers=csrf,
            )
            assert response.status_code == 409
            assert (await client.get(PREFIX + "/config")).json()["data"] == original
            response = await client.patch(
                PREFIX + "/config",
                json={
                    "expected_session_id": sid,
                    "server_url": "https://example.invalid",
                    "serial_path": "/temporary-fake-serial",
                    "stopbits": "1.0",
                },
                headers=csrf,
            )
            assert response.status_code == 200
            assert response.json()["data"]["stopbits"] == "1.0"
            assert (await client.get(PREFIX + "/config")).json()["data"]["timeout"] == "1"
            imported = await client.post(
                PREFIX + "/sites",
                json={"site_id": site, "address": "must not replace"},
                headers=csrf,
            )
            assert imported.json()["data"]["address"] == ""
            edited = await client.patch(
                PREFIX + f"/sites/{site}", json={"description": "edited"}, headers=csrf
            )
            assert edited.json()["data"]["description"] == "edited"
            for _ in range(2):
                assert (
                    await client.post(PREFIX + f"/sessions/{sid}/pause", json={}, headers=csrf)
                ).status_code == 200
                assert (
                    await client.post(PREFIX + f"/sessions/{sid}/complete", json={}, headers=csrf)
                ).status_code == 200
            new, _ = await deploy(client, csrf, expected=sid, site_id=site)
            assert new.status_code == 200
            new_sid = new.json()["data"]["logger"]["session_id"]
            assert new_sid != sid
            for action in ("start", "pause", "complete"):
                assert (
                    await client.post(PREFIX + f"/sessions/{sid}/{action}", json={}, headers=csrf)
                ).status_code == 409
            assert (
                await client.post(
                    PREFIX + "/meter-change/confirm",
                    json={"expected_session_id": sid},
                    headers=csrf,
                )
            ).status_code == 409
            page = (await client.get(PREFIX + "/sessions?limit=1&offset=1")).json()["data"]
            assert page["total"] == 2 and len(page["items"]) == 1
            assert "measurement_point_id" in page["items"][0]

    asyncio.run(check())


@pytest.mark.parametrize("target", ["config", "site"])
def test_overlapping_partial_updates_preserve_acknowledged_fields(setup, monkeypatch, target):
    async def check():
        async with client_for(setup) as (app, client):
            await login(client)
            csrf = await headers(client)
            if target == "config":
                path = PREFIX + "/config"
                first = {"expected_session_id": None, "server_url": "https://example.invalid"}
                second = {"expected_session_id": None, "serial_path": "/temporary-fake-serial"}
                read_name = "load_installation"
                expected = {
                    "server_url": "https://example.invalid",
                    "serial_path": "/temporary-fake-serial",
                }
            else:
                site = (await client.post(PREFIX + "/sites", json={}, headers=csrf)).json()["data"]
                path = PREFIX + "/sites/" + site["site_id"]
                first = {"address": "temporary address"}
                second = {"description": "temporary description"}
                read_name = "get_site"
                expected = first | second

            repository = app.state.repository
            original = getattr(repository, read_name)
            entered, release = threading.Event(), threading.Event()
            once = threading.Lock()

            def suspended_read(*args, **kwargs):
                result = original(*args, **kwargs)
                if once.acquire(blocking=False):
                    entered.set()
                    assert release.wait(2), "Overlapping request did not finish"
                return result

            monkeypatch.setattr(repository, read_name, suspended_read)
            request = asyncio.create_task(client.patch(path, json=first, headers=csrf))
            try:
                await eventually(entered.is_set)
                overlapping = await asyncio.wait_for(
                    client.patch(path, json=second, headers=csrf), 1
                )
                assert overlapping.status_code == 409
                assert overlapping.json()["error"]["code"] == "busy"
            finally:
                release.set()
                result = await request
            assert result.status_code == 200
            # A rejected edit has not been acknowledged; retry it against the latest row.
            retry = await client.patch(path, json=second, headers=csrf)
            assert retry.status_code == 200
            actual = retry.json()["data"]
            assert all(actual[name] == value for name, value in expected.items())
            if target == "config":
                saved = (await client.get(path)).json()["data"]
            else:
                saved = (await client.get(PREFIX + "/sites")).json()["data"]["items"][0]
            assert all(saved[name] == value for name, value in expected.items())

    asyncio.run(check())


def test_delayed_server_edit_uses_latest_serial_settings_without_pausing(setup, monkeypatch):
    async def check():
        async with client_for(setup) as (app, client):
            await login(client)
            csrf = await headers(client)
            response, _ = await deploy(client, csrf)
            sid = response.json()["data"]["logger"]["session_id"]
            entered, release = threading.Event(), threading.Event()
            original = app.state.runtime.patch_settings

            def delayed_command(command):
                if "server_url" in command.model_fields_set:
                    entered.set()
                    assert release.wait(2), "Concurrent serial update did not finish"
                return original(command)

            monkeypatch.setattr(app.state.runtime, "patch_settings", delayed_command)
            request = asyncio.create_task(
                client.patch(
                    PREFIX + "/config",
                    json={"expected_session_id": sid, "server_url": "https://example.invalid"},
                    headers=csrf,
                )
            )
            try:
                await eventually(entered.is_set)
                serial = await client.patch(
                    PREFIX + "/config",
                    json={"expected_session_id": sid, "serial_path": "/temporary-fake-serial"},
                    headers=csrf,
                )
                assert serial.status_code == 200
                started = await client.post(
                    PREFIX + f"/sessions/{sid}/start", json={}, headers=csrf
                )
                assert started.status_code == 200
                await eventually(
                    lambda: app.state.runtime.operations.state.collection_state == "collecting"
                )
            finally:
                release.set()
                result = await request
            assert result.status_code == 200
            assert result.json()["data"]["serial_path"] == "/temporary-fake-serial"
            status = (await client.get(PREFIX + "/status")).json()["data"]["logger"]
            assert status["collection_state"] == "collecting"
            assert not setup[1].ports[-1].closed
            # Let teardown drain its temporary reader without waiting on the fake clock.
            pause = asyncio.create_task(
                client.post(PREFIX + f"/sessions/{sid}/pause", json={}, headers=csrf)
            )
            await eventually(
                lambda: app.state.runtime.operations.state.collection_state == "stopping"
            )
            setup[2].advance(10)
            assert (await pause).status_code == 200

    asyncio.run(check())


def test_health_lock_shutdown_and_fatal_storage(setup):
    async def check():
        app = create_app(setup[0])
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://logger.test"
        ) as client:
            assert (await client.get("/health/ready")).status_code == 503
            assert (await client.get("/health/live")).json() == {"live": True}
        async with client_for(setup) as (app, client):
            assert (await client.get("/health/ready")).json() == {"ready": True}
            with pytest.raises(InstanceBusy):
                with installation_lock(setup[0].data_dir):
                    pass
            app.state.repository.update_blocks("storage_full", blocked=True)
            assert (await client.get("/health/ready")).status_code == 200
            app.state.runtime._fatal_storage = True
            assert (await client.get("/health/ready")).status_code == 503
            await login(client)
            csrf = await headers(client)
            response = await client.post(PREFIX + "/sites", json={}, headers=csrf)
            assert response.status_code == 507
        assert not app.state.ready and app.state.runtime._closed
        with installation_lock(setup[0].data_dir):
            pass

    asyncio.run(check())


def test_preview_and_drain_leave_status_responsive(setup):
    async def check():
        async with client_for(setup) as (app, client):
            await login(client)
            csrf = await headers(client)
            response = await client.patch(
                PREFIX + "/config",
                json={"expected_session_id": None, "serial_path": "/temporary-fake-serial"},
                headers=csrf,
            )
            assert response.status_code == 200
            preview = asyncio.create_task(client.post(PREFIX + "/preview", json={}, headers=csrf))
            await eventually(lambda: setup[1].ports)
            assert (await asyncio.wait_for(client.get(PREFIX + "/status"), 0.5)).status_code == 200
            setup[1].ports[-1].chunks.put(FRAME)
            response = await preview
            assert response.status_code == 200
            reading = response.json()["data"]
            assert len(reading["metrics"]) == 18
            assert "fingerprint" not in response.text and "raw_frame" not in response.text
            assert app.state.repository.list_sessions() == ()
            assert setup[1].ports[-1].closed
            response, _ = await deploy(client, csrf)
            sid = response.json()["data"]["logger"]["session_id"]
            for _ in range(2):
                response = await client.post(
                    PREFIX + f"/sessions/{sid}/start", json={}, headers=csrf
                )
                assert response.status_code == 200
            await eventually(lambda: len(setup[1].ports) == 2)
            setup[1].ports[-1].chunks.put(FRAME)
            await eventually(
                lambda: app.state.runtime.get_status()["latest_live_reading"] is not None
            )
            pause = asyncio.create_task(
                client.post(PREFIX + f"/sessions/{sid}/pause", json={}, headers=csrf)
            )
            await eventually(
                lambda: app.state.runtime.operations.state.collection_state == "stopping"
            )
            response = await asyncio.wait_for(client.get(PREFIX + "/status"), 0.5)
            assert response.json()["data"]["logger"]["collection_state"] == "stopping"
            assert (await client.post(PREFIX + "/sites", json={}, headers=csrf)).status_code == 409
            setup[2].advance(10)
            assert (await pause).status_code == 200
            status = (await client.get(PREFIX + "/status")).json()["data"]
            assert status["logger"]["pending_count"] == 1
            assert status["active_session_latest_received_at"] is not None
            assert status["upload_mode"] == "not_implemented"
            assert (
                await client.post(
                    PREFIX + "/cleanup", json={"session_ids": [sid], "confirm": True}, headers=csrf
                )
            ).json()["data"]["retained_pending_count"] == 1
            missing = await client.post(
                PREFIX + "/cleanup",
                json={"session_ids": [sid, str(uuid4())], "confirm": True},
                headers=csrf,
            )
            assert missing.status_code == 404
            assert app.state.repository.get_status().pending_count == 1

    asyncio.run(check())


def test_preview_cancel_disconnect_timeout_and_block(setup):
    async def check():
        async with client_for(setup) as (app, client):
            await login(client)
            csrf = await headers(client)
            await client.patch(
                PREFIX + "/config",
                json={"expected_session_id": None, "serial_path": "/temporary-fake-serial"},
                headers=csrf,
            )
            task = asyncio.create_task(client.post(PREFIX + "/preview", json={}, headers=csrf))
            await eventually(lambda: setup[1].ports)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await eventually(lambda: setup[1].ports[-1].closed)
            task = asyncio.create_task(client.post(PREFIX + "/preview", json={}, headers=csrf))
            await eventually(lambda: len(setup[1].ports) == 2)
            setup[2].advance(15)
            response = await task
            assert (
                response.status_code == 503
                and response.json()["error"]["code"] == "preview_timeout"
            )
            app.state.repository.update_blocks("meter_changed", blocked=True)
            app.state.runtime.operations.state = app.state.repository.load_lifecycle()
            assert (
                await client.post(PREFIX + "/preview", json={}, headers=csrf)
            ).status_code == 409

    asyncio.run(check())


@pytest.mark.parametrize("fault", ["serial_close_failed", "serial_join_failed"])
def test_serial_release_faults_have_fixed_api_guidance_and_keep_status_live(setup, fault):
    async def check():
        async with client_for(setup) as (app, client):
            await login(client)
            csrf = await headers(client)
            app.state.runtime.runtime_error = fault
            app.state.runtime._port = object()
            app.state.runtime._cancel.set()
            response = await client.post(PREFIX + "/sites", json={}, headers=csrf)
            assert response.status_code == 409
            assert response.json()["error"]["code"] == fault
            message = response.json()["error"]["message"]
            assert "restart the Logger" in message
            assert "private" not in message
            status = await client.get(PREFIX + "/status")
            assert status.status_code == 200
            assert status.json()["data"]["runtime_error"] == fault
            app.state.runtime._port = None
            app.state.runtime._cancel.clear()

    asyncio.run(check())


def test_cli_single_worker_and_no_forwarded_trust(setup, monkeypatch):
    monkeypatch.setattr(cli.LoggerSettings, "from_env", lambda: setup[0])
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    assert cli.main([]) == 0
    assert calls == [
        {
            "host": "0.0.0.0",
            "port": 8080,
            "workers": 1,
            "reload": False,
            "access_log": False,
            "proxy_headers": False,
        }
    ]
    assert not setup[0].data_dir.exists()


def test_startup_failure_releases_lock_and_resources(setup, monkeypatch):
    from watt_profile.logger import app as app_module

    async def check():
        monkeypatch.setattr(
            app_module,
            "migrate",
            lambda engine: (_ for _ in ()).throw(RuntimeError("temporary_migration_failure")),
        )
        app = create_app(setup[0], serial_factory=setup[1])
        with pytest.raises(RuntimeError, match="temporary_migration_failure"):
            async with app.router.lifespan_context(app):
                pytest.fail("Startup should have failed")
        assert not app.state.ready
        with installation_lock(setup[0].data_dir):
            pass

    asyncio.run(check())


def test_switch_confirmation_storage_resolution_and_cleanup_validation(setup):
    async def check():
        async with client_for(setup) as (app, client):
            await login(client)
            csrf = await headers(client)
            initial, site = await deploy(client, csrf)
            sid = initial.json()["data"]["logger"]["session_id"]
            same, _ = await deploy(client, csrf, expected=sid, site_id=site, confirm=True)
            assert same.status_code == 409 and same.json()["error"]["code"] == "same_site"
            pending_switch, other = await deploy(client, csrf, expected=sid)
            assert pending_switch.status_code == 409
            confirmed, _ = await deploy(client, csrf, expected=sid, site_id=other, confirm=True)
            assert confirmed.status_code == 200
            assert confirmed.json()["data"]["active_session_state"] == "open"
            assert confirmed.json()["data"]["logger"]["session_id"] != sid
            assert app.state.repository.get_session(UUID(sid)).state == "completed"
            for body in (
                {"session_ids": [sid], "confirm": False},
                {"session_ids": [sid], "confirm": 1},
                {"session_ids": [sid, sid], "confirm": True},
                {"session_ids": [], "confirm": True},
                {"session_ids": [sid], "confirm": True, "private": "do not echo"},
            ):
                response = await client.post(PREFIX + "/cleanup", json=body, headers=csrf)
                assert response.status_code == 422 and "do not echo" not in response.text
            unknown = await client.post(
                PREFIX + f"/sessions/{uuid4()}/start", json={}, headers=csrf
            )
            assert unknown.status_code == 404
            app.state.repository.update_blocks("storage_full", blocked=True)
            app.state.runtime.operations.state = app.state.repository.load_lifecycle()
            resolved = await client.post(PREFIX + "/storage/resolve", json={}, headers=csrf)
            assert resolved.status_code == 200
            assert resolved.json()["data"]["persisted_blocks"] == []
            assert resolved.json()["data"]["logger"]["collection_state"] == "idle"
            bad = await client.post(
                PREFIX + "/sites", json={}, headers=csrf | {"X-CSRF-Token": bytes([233])}
            )
            assert bad.status_code == 403

    asyncio.run(check())
