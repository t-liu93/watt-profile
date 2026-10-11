"""Explicit browser acceptance with a disposable loopback app and fake serial."""

from __future__ import annotations

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
    app = create_app(
        settings, serial_factory=Factory(), clock=Clock(), auth_clock=lambda: auth_time[0]
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
                    yield page, origin, app, auth_time
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
    page, origin, app, _ = browser_app
    login(page, origin)
    expect(
        page.get_by_text("Upload not implemented in M2; records remain pending", exact=True)
    ).to_be_visible()
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
    token = page.locator("#registration-text").input_value().split("Token: ")[1]
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


def test_optional_preview_switch_current_freshness_errors_and_expiry(browser_app):
    page, origin, app, auth_time = browser_app
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


def test_eligible_cleanup_and_paginated_sites(browser_app):
    page, origin, app, _ = browser_app
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
    page.locator("#sessions input").check()
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
    page, origin, app, _ = browser_app
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
    page, origin, app, _ = browser_app
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
