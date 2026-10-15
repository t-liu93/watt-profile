"""Explicit browser acceptance with a disposable loopback app and fake serial."""

from __future__ import annotations

import json
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from watt_profile.contracts.upload import UploadConfirmation
from watt_profile.logger.app import create_app
from watt_profile.logger.auth import MAX_AGE
from watt_profile.logger.settings import LoggerSettings
from watt_profile.logger.upload import TransportResult

PASSWORD = "temporary-browser-password"
FRAME = (Path(__file__).parents[1] / "fixtures/p1/profile/single_phase.telegram").read_bytes()
# Collection alone must never start services or browsers in the default suite.
pytestmark = pytest.mark.skipif(
    not any("tests/browser" in str(arg) for arg in __import__("sys").argv[1:]),
    reason="Select tests/browser explicitly to run development browser acceptance",
)


class Clock:
    def __init__(self):
        self.anchor = time.monotonic()

    def monotonic(self):
        return (time.monotonic() - self.anchor) * 50

    def utc_now(self):
        return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=self.monotonic())

    def wait_interruptible(self, seconds, event):
        event.wait(seconds / 50)


class Port:
    def __init__(self):
        self.closed = threading.Event()

    def read(self, size):
        return b"" if self.closed.wait(0.025) else FRAME

    def close(self):
        self.closed.set()


class Factory:
    def __init__(self):
        self.ports = []

    def __call__(self, config):
        assert config.path in {"/temporary-fake-serial", "synthetic"}
        assert all(port.closed.is_set() for port in self.ports)
        port = Port()
        self.ports.append(port)
        return port


class FakeUpload:
    """Explicit isolated transport; a configured origin never reaches the network."""

    def __init__(self):
        self.result = TransportResult(401, error_code="invalid_credential")
        self.calls = []
        self.reject_site_id = None

    async def send(self, origin, token, group):
        self.calls.append((origin, token, group))
        if self.reject_site_id == str(group.batch.site_id):
            return TransportResult(403, error_code="site_not_authorized")
        if self.result.status_code == 200:
            batch = group.batch
            return TransportResult(
                200,
                confirmation=UploadConfirmation(
                    schema_version=1,
                    device_id=batch.device_id,
                    confirmed_record_ids=tuple(record.record_id for record in batch.records),
                ),
            )
        return self.result

    async def aclose(self):
        pass


@pytest.fixture
def browser_app(tmp_path, request):
    password = tmp_path / "password"
    password.write_text(PASSWORD)
    settings = LoggerSettings.from_env(
        {
            "WATT_LOGGER_DATA_DIR": str(tmp_path / "data"),
            "WATT_LOGGER_INPUT": getattr(request, "param", "serial"),
            "WATT_LOGGER_OPERATOR_USER": "operator",
            "WATT_LOGGER_OPERATOR_PASSWORD_FILE": str(password),
        }
    )
    auth_time = [0.0]
    upload = FakeUpload()
    app = create_app(
        settings,
        serial_factory=Factory(),
        clock=Clock(),
        auth_clock=lambda: auth_time[0],
        upload_transport_factory=lambda _: upload,
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.01)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                context = browser.new_context()
                try:
                    page = context.new_page()
                    # Refuse any network destination outside this isolated app.
                    origin = f"http://127.0.0.1:{port}"
                    context.route(
                        "**/*",
                        lambda route: (
                            route.continue_()
                            if route.request.url.startswith(origin + "/")
                            else route.abort()
                        ),
                    )
                    yield page, origin, app, auth_time, upload
                finally:
                    context.close()
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
        assert not thread.is_alive(), "Temporary app did not stop"


def login(page, origin):
    page.goto(origin)
    page.get_by_label("Username").fill("operator")
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Log in", exact=True).click()
    expect(page.get_by_role("heading", name="Watt Profile Logger")).to_be_visible()
    expect(page.locator("#status")).to_contain_text("idle")
    # Initial status precedes the configuration fetch. Wait for its DOM write
    # before editing, so initialization cannot overwrite keyboard-entered values.
    expect(page.locator('#config input[name="baudrate"]')).not_to_have_value("")


def section(page, name):
    page.get_by_role("button", name=name, exact=True).click()


def saved_setup(page, address="Invented A"):
    section(page, "Site and settings")
    page.get_by_label("Address", exact=True).fill(address)
    page.get_by_role("button", name="Create / import site", exact=True).click()
    expect(page.locator("#site-select")).to_contain_text(address)
    page.get_by_role("button", name="Create paused deployment / switch site").click()
    expect(page.locator("#message")).to_have_text("Command completed.")
    page.get_by_label("Serial path", exact=True).fill("/temporary-fake-serial")
    page.get_by_role("button", name="Save settings", exact=True).click()
    expect(page.locator("#message")).to_have_text("Command completed.")


def current_session(page):
    # Poll and read in one DOM evaluation: status polling replaces these nodes.
    session = page.wait_for_function(
        """() => {
            const label = [...document.querySelectorAll('#status dt')]
                .find(el => el.textContent === 'Session UUID');
            const value = label?.nextElementSibling?.textContent;
            return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
                .test(value || '') ? value : false;
        }""",
        timeout=5000,
    )
    try:
        return session.json_value()
    finally:
        session.dispose()


def test_offline_keyboard_preview_lifecycle_and_privacy(browser_app):
    page, origin, app, _, upload = browser_app
    login(page, origin)
    expect(page.locator("#upload-banner")).to_contain_text("unconfigured")
    page.get_by_role("button", name="Retry upload").click()
    expect(page.locator("#message")).to_contain_text("Retry requested")
    assert upload.calls == []
    expect(page.locator("#synthetic")).to_be_hidden()
    # Configure without a site, then preview. Keyboard submission is supported.
    section(page, "Site and settings")
    page.get_by_label("Serial path", exact=True).fill("/temporary-fake-serial")
    page.get_by_role("button", name="Save settings", exact=True).focus()
    page.keyboard.press("Enter")
    expect(page.locator("#message")).to_have_text("Command completed.")
    section(page, "Reading preview")
    page.get_by_role("button", name="Read preview", exact=True).click()
    expect(page.locator("#reading-kind")).to_have_text("Read preview (not stored)")
    expect(page.locator("#metrics tr")).to_have_count(18)
    expect(page.locator("#metrics")).to_contain_text("Unavailable")
    expect(page.locator("#metrics")).to_contain_text("kWh")
    assert app.state.repository.get_status().pending_count == 0
    for _ in range(2):
        page.get_by_role("button", name="Read preview", exact=True).click()
        expect(page.locator("#reading-kind")).to_have_text("Read preview (not stored)")
        assert app.state.repository.get_status().pending_count == 0
    saved_setup(page)
    section(page, "Collection")
    session = current_session(page)
    page.get_by_role("button", name="Start / resume", exact=True).click()
    expect(page.locator("#status")).to_contain_text("collecting")
    page.wait_for_timeout(2300)
    assert app.state.repository.get_status().pending_count > 0
    page.get_by_role("button", name="Pause", exact=True).click()
    expect(page.locator("#status")).to_contain_text("idle")
    pending = app.state.repository.get_status().pending_count
    page.wait_for_timeout(350)
    assert app.state.repository.get_status().pending_count == pending
    page.get_by_role("button", name="Start / resume", exact=True).click()
    expect(page.locator("#status")).to_contain_text("collecting")
    assert current_session(page) == session
    page.get_by_role("button", name="Pause", exact=True).click()
    expect(page.locator("#status")).to_contain_text("idle")
    section(page, "Registration")
    page.get_by_role("button", name="Reveal registration information").click()
    expect(page.locator("#registration-text")).to_be_visible()
    token = json.loads(page.locator("#registration-text").input_value())["token"]
    assert token not in str(page.evaluate("Object.assign({}, localStorage)"))
    page.evaluate(
        "Object.defineProperty(navigator, 'clipboard', {value: undefined, configurable: true})"
    )
    page.get_by_role("button", name="Copy registration information").click()
    expect(page.locator("#copy-help")).to_contain_text("Clipboard unavailable")
    section(page, "Collection")
    assert page.locator("#registration-text").input_value() == ""
    page.get_by_role("button", name="Complete session", exact=True).click()
    expect(page.get_by_role("dialog")).to_contain_text(session)
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.locator("#message")).to_have_text("Action cancelled.")
    assert app.state.repository.load_lifecycle().session.state == "open"
    page.get_by_role("button", name="Complete session", exact=True).click()
    page.get_by_role("button", name="Confirm", exact=True).click()
    expect(page.locator("#status")).to_contain_text("completed")
    before = app.state.repository.page_sessions(0, 100)["total"]
    page.reload()
    expect(page.locator("#status")).to_contain_text("completed")
    assert app.state.repository.page_sessions(0, 100)["total"] == before
    section(page, "Maintenance")
    expect(page.locator("#sessions tr")).to_have_count(1)
    expect(page.locator("#sessions input")).to_be_disabled()


@pytest.mark.parametrize("fault", ["serial_close_failed", "serial_join_failed"])
def test_latched_serial_fault_explains_recovery_and_disables_mutations(browser_app, fault):
    page, origin, app, _, _ = browser_app
    login(page, origin)
    app.state.runtime.runtime_error = fault
    page.get_by_role("button", name="Reload status").click()
    expect(page.locator("#serial-recovery")).to_be_visible()
    expect(page.locator("#serial-recovery")).to_contain_text(fault)
    expect(page.locator("#serial-recovery")).to_contain_text("Restart the Logger container")
    section(page, "Reading preview")
    expect(page.get_by_role("button", name="Read preview", exact=True)).to_be_disabled()
    section(page, "Site and settings")
    expect(page.get_by_role("button", name="Create / import site")).to_be_disabled()
    section(page, "Collection")
    expect(page.get_by_role("button", name="Reload status")).to_be_enabled()


def test_optional_preview_switch_current_freshness_errors_and_expiry(browser_app):
    page, origin, app, auth_time, upload = browser_app
    login(page, origin)
    saved_setup(page)
    section(page, "Collection")
    first = current_session(page)
    page.get_by_role("button", name="Start / resume", exact=True).click()
    expect(page.locator("#status")).to_contain_text("collecting")
    page.wait_for_timeout(300)
    section(page, "Site and settings")
    page.get_by_label("Address", exact=True).fill("Invented B")
    page.get_by_role("button", name="Create / import site", exact=True).click()
    expect(page.locator("#site-select")).to_contain_text("Invented B")
    page.get_by_role("button", name="Create paused deployment / switch site").click()
    expect(page.get_by_role("dialog")).to_contain_text(first)
    page.get_by_role("button", name="Confirm", exact=True).click()
    expect(page.locator("#message")).to_have_text("Command completed.")
    section(page, "Collection")
    assert current_session(page) != first
    expect(page.locator("#status")).to_contain_text("idle")
    expect(
        page.locator("#status dt").filter(has_text="Latest saved current-session").locator("+ dd")
    ).to_have_text("Unavailable")
    assert app.state.repository.get_status().pending_count > 0
    section(page, "Site and settings")
    origin_input = page.get_by_label("Server origin (optional)")
    origin_input.fill("https://saved-upload.invalid")
    page.get_by_role("button", name="Save settings").click()
    expect(page.locator("#message")).to_have_text("Command completed.")
    origin_input.fill("https://new-upload.invalid")
    page.get_by_role("button", name="Save settings").click()
    expect(page.locator("#error")).to_contain_text(
        "Server origin cannot change while uploads are pending"
    )
    expect(page.locator("#error")).to_contain_text("Restore access to the saved server")
    expect(origin_input).to_have_value("https://new-upload.invalid")
    # Fixed API failures remain visible; no false command-success state.
    page.route(
        "**/api/local/v1/preview",
        lambda route: route.fulfill(
            status=409, json={"error": {"code": "busy", "message": "Command cannot be completed."}}
        ),
    )
    section(page, "Reading preview")
    page.get_by_role("button", name="Read preview", exact=True).click()
    expect(page.locator("#error")).to_contain_text("busy")
    expect(page.locator("#error")).to_contain_text("Reload status before retrying")
    page.unroute("**/api/local/v1/preview")
    page.route(
        "**/api/local/v1/preview",
        lambda route: route.fulfill(
            status=507,
            json={"error": {"code": "storage_error", "message": "Storage operation failed."}},
        ),
    )
    page.get_by_role("button", name="Read preview", exact=True).click()
    expect(page.locator("#error")).to_contain_text("storage_error")
    auth_time[0] = MAX_AGE + 1
    section(page, "Collection")
    page.get_by_role("button", name="Reload status").click()
    expect(page.locator("#error")).to_have_text("Login expired. Log in again to continue.")
    expect(page.get_by_role("link", name="Log in again")).to_be_visible()
    expect(page.get_by_role("button", name="Start / resume", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="Retry upload")).to_be_disabled()


def test_retry_is_independent_and_explains_auth_recovery(browser_app):
    page, origin, app, _, upload = browser_app
    login(page, origin)
    saved_setup(page)
    section(page, "Collection")
    page.get_by_role("button", name="Start / resume", exact=True).click()
    expect(page.locator("#status")).to_contain_text("collecting")
    page.wait_for_timeout(500)
    initial_pending = app.state.repository.get_status().pending_count
    assert initial_pending > 0
    section(page, "Site and settings")
    page.get_by_label("Server origin (optional)").fill("https://upload.invalid")
    page.get_by_role("button", name="Save settings").click()
    expect(page.locator("#message")).to_have_text("Command completed.")
    page.wait_for_function(
        "() => document.querySelector('#upload-status').textContent.includes('blocked')"
    )
    expect(page.locator("#upload-help")).to_contain_text("registration Token")
    section(page, "Reading preview")
    upload.result = TransportResult(200)
    # Retry stays enabled during preview and does not take the serial mutation lock.
    page.evaluate("""() => {
      const original = window.fetch;
      window.fetch = async (...args) => {
        if (String(args[0]).endsWith('/preview')) {
          await new Promise(resolve => setTimeout(resolve, 700));
        }
        return original(...args);
      };
    }""")
    page.evaluate("""() => {
      const original = window.fetch;
      window.retryRequests = 0;
      window.fetch = async (...args) => {
        if (String(args[0]).endsWith('/upload/retry')) {
          window.retryRequests++;
          await new Promise(resolve => setTimeout(resolve, 2600));
        }
        return original(...args);
      };
    }""")
    page.get_by_role("button", name="Read preview").click()
    expect(page.get_by_role("button", name="Retry upload")).to_be_enabled()
    expect(page.get_by_role("button", name="Read preview")).to_be_disabled()
    page.get_by_role("button", name="Retry upload").click()
    expect(page.locator("#message")).to_contain_text("Retry requested")
    expect(page.get_by_role("button", name="Retry upload")).to_be_disabled()
    page.wait_for_timeout(2100)
    expect(page.get_by_role("button", name="Retry upload")).to_be_disabled()
    page.evaluate("document.getElementById('retry-upload').click()")
    assert page.evaluate("window.retryRequests") == 1
    deadline = time.monotonic() + 5
    while (
        len(upload.calls) < 2 or app.state.repository.get_status().last_successful_upload_at is None
    ) and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert len(upload.calls) >= 2
    assert app.state.repository.get_status().last_successful_upload_at is not None
    section(page, "Collection")
    page.get_by_role("button", name="Pause", exact=True).click()
    expect(page.locator("#status")).to_contain_text("idle")
    app.state.runtime.runtime_error = "serial_close_failed"
    page.get_by_role("button", name="Reload status").click()
    expect(page.locator("#serial-recovery")).to_be_visible()
    expect(page.locator("#serial-recovery")).to_contain_text("serial_close_failed")
    expect(page.locator("#upload-status")).not_to_contain_text(
        "Last durable confirmation checkpoint (UTC)\nUnavailable"
    )


def test_group_block_stays_visible_while_other_session_uploads(browser_app):
    page, origin, app, _, upload = browser_app
    login(page, origin)
    saved_setup(page, "First site")
    section(page, "Collection")
    page.get_by_role("button", name="Start / resume", exact=True).click()
    page.wait_for_timeout(350)
    first_site = app.state.repository.load_lifecycle().session.descriptor.site_id
    page.get_by_role("button", name="Pause", exact=True).click()
    section(page, "Site and settings")
    page.get_by_label("Address", exact=True).fill("Second site")
    page.get_by_role("button", name="Create / import site", exact=True).click()
    page.get_by_role("button", name="Create paused deployment / switch site").click()
    page.get_by_role("button", name="Confirm", exact=True).click()
    section(page, "Collection")
    page.get_by_role("button", name="Start / resume", exact=True).click()
    page.wait_for_timeout(350)
    page.get_by_role("button", name="Pause", exact=True).click()
    section(page, "Site and settings")
    page.get_by_label("Server origin (optional)").fill("https://upload.invalid")
    upload.reject_site_id = str(first_site)
    upload.result = TransportResult(200)
    page.get_by_role("button", name="Save settings").click()
    expect(page.locator("#message")).to_have_text("Command completed.")
    page.wait_for_function("""() => {
      const dt = [...document.querySelectorAll('#upload-status dt')]
        .find(node => node.textContent === 'Last durable confirmation checkpoint (UTC)');
      return dt && dt.nextElementSibling.textContent !== 'Unavailable';
    }""")
    section(page, "Maintenance")
    first_row = page.locator("#sessions tr").filter(has_text=str(first_site))
    expect(first_row).to_contain_text("site_not_authorized")
    expect(page.locator("#upload-status")).to_contain_text("Blocked session groups")
    rows = page.locator("#sessions tr")
    assert rows.count() == 2
    assert app.state.repository.get_status().pending_count > 0


def test_maintenance_poll_preserves_keyboard_focus(browser_app):
    page, origin, _, _, _ = browser_app
    login(page, origin)
    page.evaluate("""() => {
      const original = window.fetch;
      window.sessionLoads = 0;
      window.fetch = async (...args) => {
        if (String(args[0]).includes('/sessions?')) {
          window.sessionLoads++;
          return new Response(JSON.stringify({data: {total: 1, items: [{
            session_id: '00000000-0000-4000-8000-000000000001',
            device_id: '00000000-0000-4000-8000-000000000002',
            site_id: '00000000-0000-4000-8000-000000000003',
            source_id: '00000000-0000-4000-8000-000000000004',
            measurement_point_id: '00000000-0000-5000-8000-000000000005',
            started_at: '2026-01-01T00:00:00Z', state: 'completed',
            collection_enabled: false, ended_at: '2026-01-01T00:01:00Z',
            total_count: 1, pending_count: 0, eligible_uploaded_count: 1,
            upload_error: null
          }]}}), {headers: {'Content-Type': 'application/json'}});
        }
        return original(...args);
      };
    }""")
    section(page, "Maintenance")
    page.wait_for_function("window.sessionLoads >= 1")
    checkbox = page.locator("#sessions input")
    checkbox.focus()
    page.keyboard.press("Space")
    expect(checkbox).to_be_checked()
    expect(checkbox).to_be_focused()
    page.wait_for_function("window.sessionLoads >= 2", timeout=5000)
    expect(page.locator("#sessions input")).to_be_checked()
    expect(page.locator("#sessions input")).to_be_focused()


def test_session_navigation_during_refresh_loads_requested_page(browser_app):
    page, origin, _, _, _ = browser_app
    login(page, origin)
    page.evaluate("""() => {
      const original = window.fetch;
      window.holdNextSessions = false;
      window.releaseSessions = null;
      window.fetch = async (...args) => {
        const url = String(args[0]);
        if (url.includes('/sessions?')) {
          const offset = Number(new URL(url, location.href).searchParams.get('offset'));
          if (window.holdNextSessions) {
            window.holdNextSessions = false;
            window.sessionRequestPending = true;
            await new Promise(resolve => { window.releaseSessions = resolve; });
          }
          const items = Array.from({length: Math.min(20, 45 - offset)}, (_, index) => {
            const number = offset + index;
            return {
              session_id: `00000000-0000-4000-8000-${String(number + 1).padStart(12, '0')}`,
              device_id: '00000000-0000-4000-8000-000000000002',
              site_id: '00000000-0000-4000-8000-000000000003',
              source_id: '00000000-0000-4000-8000-000000000004',
              measurement_point_id: '00000000-0000-5000-8000-000000000005',
              started_at: '2026-01-01T00:00:00Z', state: 'completed',
              collection_enabled: false, ended_at: '2026-01-01T00:01:00Z',
              total_count: 1, pending_count: 0, eligible_uploaded_count: 1,
              upload_error: null
            };
          });
          return new Response(JSON.stringify({data: {items, total: 45}}),
            {headers: {'Content-Type': 'application/json'}});
        }
        return original(...args);
      };
    }""")
    section(page, "Maintenance")
    expect(page.locator("#sessions tr")).to_have_count(20)
    expect(page.locator("#session-page")).to_have_text("1–20 of 45 sessions")
    page.evaluate("window.holdNextSessions = true")
    page.evaluate("document.getElementById('reload').click()")
    page.wait_for_function("window.sessionRequestPending === true")
    page.get_by_role("button", name="Next sessions").click()
    page.evaluate("window.releaseSessions()")
    expect(page.locator("#sessions tr")).to_have_count(20)
    expect(page.locator("#sessions tr").first).to_contain_text(
        "00000000-0000-4000-8000-000000000021"
    )
    expect(page.locator("#session-page")).to_have_text("21–40 of 45 sessions")


def test_eligible_cleanup_and_paginated_sites(browser_app):
    page, origin, app, _, _ = browser_app
    login(page, origin)
    saved_setup(page)
    section(page, "Collection")
    page.get_by_role("button", name="Start / resume", exact=True).click()
    expect(page.locator("#status")).to_contain_text("collecting")
    page.wait_for_timeout(300)
    page.get_by_role("button", name="Pause", exact=True).click()
    expect(page.locator("#status")).to_contain_text("idle")
    repo = app.state.repository
    batch = repo.select_pending_group().batch
    assert batch.records
    # Legitimate confirmation is only a test storage primitive, never an HTTP endpoint.
    repo.apply_confirmation(
        batch,
        UploadConfirmation(
            schema_version=1,
            device_id=batch.device_id,
            confirmed_record_ids=tuple(record.record_id for record in batch.records),
        ),
        datetime(2026, 1, 2, tzinfo=UTC),
    )
    page.get_by_role("button", name="Start / resume", exact=True).click()
    expect(page.locator("#status")).to_contain_text("collecting")
    page.wait_for_timeout(300)
    page.get_by_role("button", name="Complete session", exact=True).click()
    page.get_by_role("button", name="Confirm", exact=True).click()
    expect(page.locator("#status")).to_contain_text("completed")
    pending = repo.get_status().pending_count
    assert pending > 0
    section(page, "Maintenance")
    expect(page.locator("#sessions input")).to_be_enabled()
    page.evaluate("""() => {
      const original = window.fetch;
      window.sessionRequests = 0;
      window.fetch = async (...args) => {
        if (String(args[0]).includes('/sessions?')) {
          window.sessionRequests++;
          await new Promise(resolve => setTimeout(resolve, 500));
        }
        return original(...args);
      };
    }""")
    section(page, "Maintenance")
    page.wait_for_function("window.sessionRequests > 0")
    page.locator("#sessions input").check()
    expect(page.locator("#sessions input")).to_be_checked()
    page.get_by_role("button", name="Clean selected eligible records").click()
    expect(page.get_by_role("dialog")).to_contain_text(current_session(page))
    page.get_by_role("button", name="Confirm", exact=True).click()
    expect(page.locator("#message")).to_contain_text("Deleted")
    assert repo.get_status().pending_count == pending
    expect(page.locator("#sessions input")).to_be_disabled()
    for number in range(22):
        app.state.runtime.create_site(address=f"Invented pagination {number}")
    section(page, "Site and settings")
    expect(page.locator("#site-select option")).to_have_count(20)
    page.get_by_role("button", name="Next sites", exact=True).click()
    expect(page.locator("#site-select option")).to_have_count(3)
    page.get_by_role("button", name="Previous sites", exact=True).click()
    expect(page.locator("#site-select option")).to_have_count(20)


def test_polling_does_not_overlap_or_replay_commands(browser_app):
    page, origin, app, _, _ = browser_app
    login(page, origin)
    # Track status fetches independently of app latency and hold each one > 2 seconds.
    page.evaluate("""() => {
        const original = window.fetch;
        window.testPolling = {active: 0, max: 0, mutations: 0};
        window.fetch = async (...args) => {
            const stats = window.testPolling;
            if (args[1]?.method !== 'GET') stats.mutations++;
            if (!args[0].endsWith('/status')) return original(...args);
            stats.active++; stats.max = Math.max(stats.max, stats.active);
            try { await new Promise(resolve => setTimeout(resolve, 2500));
                  return await original(...args); }
            finally { stats.active--; }
        };
    }""")
    page.wait_for_timeout(5100)
    assert page.evaluate("window.testPolling.max") == 1
    assert page.evaluate("window.testPolling.mutations") == 0
    assert app.state.repository.page_sessions(0, 100)["total"] == 0


@pytest.mark.parametrize("browser_app", ["synthetic"], indirect=True)
def test_synthetic_banner_meter_confirmation_and_separate_storage_start(browser_app):
    page, origin, app, _, _ = browser_app
    login(page, origin)
    expect(page.locator("#synthetic")).to_be_visible()
    # The injected fake supplies bytes; no deployment synthetic source is introduced.
    section(page, "Reading preview")
    page.get_by_role("button", name="Read preview", exact=True).click()
    expect(page.locator("#reading-kind")).to_have_text("Read preview (not stored)")
    saved_setup(page)
    section(page, "Collection")
    old = current_session(page)
    repo, runtime = app.state.repository, app.state.runtime
    repo.update_blocks("meter_changed", blocked=True)
    repo.update_blocks("storage_full", blocked=True)
    runtime.operations.state = repo.load_lifecycle()
    page.get_by_role("button", name="Reload status").click()
    expect(page.get_by_role("button", name="Confirm meter replacement")).to_be_visible()
    expect(page.get_by_role("button", name="Start / resume", exact=True)).to_be_disabled()
    page.get_by_role("button", name="Confirm meter replacement").click()
    expect(page.get_by_role("dialog")).to_contain_text(old)
    page.get_by_role("button", name="Confirm", exact=True).click()
    expect(page.locator("#message")).to_have_text("Command completed.")
    assert current_session(page) != old
    assert repo.load_lifecycle().persisted_blocks == ("storage_full",)
    page.get_by_role("button", name="Check storage and resolve block").click()
    expect(page.locator("#message")).to_have_text("Command completed.")
    expect(page.locator("#status")).to_contain_text("idle")
    assert repo.load_lifecycle().persisted_blocks == ()
    assert not repo.load_lifecycle().session.collection_enabled
    # Resolution never restarts collection automatically.
    page.get_by_role("button", name="Start / resume", exact=True).click()
    expect(page.locator("#status")).to_contain_text("collecting")
    expect(page.locator("#synthetic")).to_be_visible()
