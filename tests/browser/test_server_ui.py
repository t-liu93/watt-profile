"""Real loopback Server/PostgreSQL and Logger JSON browser interoperability."""

# Imported pytest fixtures are resolved through test arguments.
# ruff: noqa: F811
import csv
import io
import json
import socket
import threading
import time
from dataclasses import replace
from uuid import uuid4

import pytest
import uvicorn
from playwright.sync_api import expect
from test_logger_ui import browser_app, saved_setup, section  # noqa: F401
from test_logger_ui import login as logger_login
from test_server_auth import server_settings  # noqa: F401
from test_server_ingest import batch

from watt_profile.server.app import create_app
from watt_profile.server.registration import RegistrationSummary

pytestmark = pytest.mark.skipif(
    not any("tests/browser" in str(arg) for arg in __import__("sys").argv[1:]),
    reason="Select tests/browser explicitly for development browser acceptance",
)
P = "/api/admin/v1"


@pytest.fixture
def server_browser(browser_app, server_engine, server_settings):
    logger_page, logger_origin, _, _ = browser_app
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    app = create_app(replace(server_settings, public_origin=origin))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    context = None
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.01)
        context = logger_page.context.browser.new_context()
        context.route(
            "**/*",
            lambda route: (
                route.continue_()
                if route.request.url.startswith(origin + "/")
                or route.request.url.startswith("blob:")
                else route.abort()
            ),
        )
        page = context.new_page()
        page.goto(origin)
        page.get_by_label("Username").fill("admin")
        page.get_by_label("Password").fill("synthetic-password-long-enough")
        page.get_by_role("button", name="Log in", exact=True).click()
        expect(page.get_by_role("heading", name="Watt Profile Server")).to_be_visible()
        logger_login(logger_page, logger_origin)
        section(logger_page, "Site and settings")
        logger_page.locator('#create-site textarea[name="description"]').fill(
            "Description\nwith <b>literal</b> text"
        )
        saved_setup(logger_page, "Synthetic <script> site")
        section(logger_page, "Registration")
        logger_page.get_by_role("button", name="Reveal registration information").click()
        expect(logger_page.locator("#registration-text")).to_be_visible()
        raw = logger_page.locator("#registration-text").input_value()
        summary = json.loads(raw)
        assert set(summary) == {"site_id", "address", "description", "device_id", "token"}
        yield page, origin, app, raw, summary, logger_page
    finally:
        if context is not None:
            context.close()
        server.should_exit = True
        thread.join(10)
        listener.close()
        assert not thread.is_alive()


def preview(page, raw):
    page.get_by_label("Logger registration JSON").fill(raw)
    page.get_by_role("button", name="Preview registration").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#registration-preview")).to_be_visible()


def register(page, raw):
    preview(page, raw)
    page.get_by_role("button", name="Confirm registration").click()
    expect(page.get_by_role("link", name="Open registered site")).to_be_visible()
    assert page.locator("#summary").input_value() == ""
    expect(page.locator("#registration-preview")).to_be_hidden()


def test_logger_registration_preserved_metadata_and_secret_cleanup(server_browser):
    page, origin, _, raw, summary, logger_page = server_browser
    preview(page, raw)
    expect(page.locator("#preview")).to_contain_text("********")
    assert summary["token"] not in page.locator("#preview").inner_text()
    assert summary["token"] not in page.url
    assert page.evaluate("Object.keys(localStorage).filter(k=>k!=='watt-profile-theme')") == []
    page.get_by_role("button", name="Cancel registration").click()
    assert page.locator("#summary").input_value() == ""
    register(page, raw)
    # Actual Logger-generated JSON remains source; edit only known-site metadata.
    changed = dict(summary, address="Different\naddress", description="Different description")
    preview(page, json.dumps(changed))
    expect(page.locator("#preview")).to_contain_text("Existing metadata differs")
    page.get_by_role("button", name="Confirm registration").click()
    expect(page.locator("#registration-preview")).to_be_hidden()
    page.get_by_role("link", name="Open registered site").click()
    expect(page.locator("#status")).to_contain_text(summary["address"])
    expect(page.locator("#readings")).to_contain_text("No accepted readings")
    page.get_by_label("Address", exact=True).fill("Explicit new address\nline 2")
    page.get_by_role("button", name="Save metadata").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    page.get_by_role("button", name="Confirm", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Explicit new address")
    page.reload()
    expect(page.locator("#status")).to_contain_text("Explicit new address")
    section(logger_page, "Collection")
    assert logger_page.locator("#registration-text").input_value() == ""
    page.goto(origin)
    preview(page, raw)
    page.get_by_role("button", name="Log out", exact=True).click()
    expect(page.get_by_role("heading", name="Server login")).to_be_visible()
    assert summary["token"] not in page.content()


@pytest.mark.parametrize("refresh_failure", ["unavailable", "disconnected", "expired"])
def test_registration_success_clears_secrets_before_failed_refresh(server_browser, refresh_failure):
    page, origin, _, raw, summary, _ = server_browser
    preview(page, raw)
    commits = []
    page.on(
        "request",
        lambda request: (
            commits.append(request)
            if request.method == "POST" and request.url == origin + P + "/registrations"
            else None
        ),
    )
    held = []
    page.route("**/api/admin/v1/sites?*", lambda route: held.append(route))
    page.get_by_role("button", name="Confirm registration").click()
    expect(page.locator("#registration-result")).to_contain_text(
        "Registration applied. Actual actions:"
    )
    expect(page.get_by_role("link", name="Open registered device")).to_be_visible()
    expect(page.locator("#registration-preview")).to_be_hidden()
    assert page.locator("#summary").input_value() == ""
    assert summary["token"] not in page.content()
    page.wait_for_function("document.querySelector('#registration-result').textContent.length > 0")
    assert held
    for route in held:
        if refresh_failure == "disconnected":
            route.abort()
        else:
            code = (
                "authentication_required"
                if refresh_failure == "expired"
                else "temporarily_unavailable"
            )
            route.fulfill(
                status=401 if refresh_failure == "expired" else 503, json={"error": {"code": code}}
            )
    if refresh_failure == "expired":
        expect(page.locator("#error")).to_have_text("Login expired. Log in again to continue.")
        expect(page.get_by_role("link", name="Log in again")).to_be_visible()
    else:
        expect(page.locator("#error")).to_contain_text(
            "Registration applied, but status could not be refreshed"
        )
    expect(page.locator("#registration-result")).to_contain_text(summary["site_id"])
    expect(page.get_by_role("link", name="Open registered device")).to_have_attribute(
        "href", "/devices/" + summary["device_id"]
    )
    response = page.request.get(origin + P + "/devices/" + summary["device_id"])
    assert response.ok
    assert len(commits) == 1
    # A hidden confirm button must also have no retained summary to resubmit.
    page.locator("#register").evaluate("button => button.click()")
    page.wait_for_timeout(100)
    assert len(commits) == 1
    page.unroute("**/api/admin/v1/sites?*")


@pytest.mark.parametrize("next_action", ["preview", "cancel"])
def test_registration_refresh_does_not_block_or_restore_old_state(server_browser, next_action):
    page, _, _, raw, _, _ = server_browser
    preview(page, raw)
    held = []
    page.route("**/api/admin/v1/sites?*", lambda route: held.append(route))
    page.get_by_role("button", name="Confirm registration").click()
    expect(page.get_by_role("link", name="Open registered site")).to_be_visible()
    assert held
    if next_action == "preview":
        # A new registration can begin even while the previous status GET is pending.
        preview(page, raw)
        expect(page.locator("#preview")).to_contain_text('"reuse"')
    else:
        page.get_by_role("button", name="Cancel registration").click()
        expect(page.locator("#registration-result")).to_be_empty()
    with page.expect_response("**/api/admin/v1/sites?*"):
        for route in held:
            route.fulfill(status=503, json={"error": {"code": "temporarily_unavailable"}})
    page.wait_for_timeout(100)
    expect(page.locator("#error")).to_be_hidden()
    if next_action == "preview":
        expect(page.locator("#registration-preview")).to_be_visible()
    else:
        expect(page.locator("#registration-preview")).to_be_hidden()
        expect(page.locator("#registration-result")).to_be_empty()
        expect(page.locator("#message")).to_have_text("Registration cancelled.")
    page.unroute("**/api/admin/v1/sites?*")


def test_readings_filters_detail_complete_csv_and_polling(server_browser, tmp_path):
    page, origin, _, raw, summary, _ = server_browser
    register(page, raw)
    payload = batch(
        RegistrationSummary.model_validate(summary), record_ids=[uuid4() for _ in range(3)]
    )
    response = page.request.post(
        origin + "/api/v1/ingest/batches",
        headers={"Authorization": "Bearer " + summary["token"]},
        data=payload.model_dump(mode="json"),
    )
    assert response.ok
    page.get_by_role("link", name="Open registered site").click()
    expect(page.locator("#status")).to_contain_text("3")
    page.get_by_label("Rows per page").fill("1")
    page.get_by_label("Device UUID", exact=True).fill(summary["device_id"])
    page.get_by_label("Session UUID", exact=True).fill(str(payload.session.session_id))
    page.get_by_role("button", name="Apply filters").click()
    expect(page.get_by_role("button", name="View reading")).to_have_count(1)
    page.get_by_role("button", name="View reading").click()
    expect(page.locator("#metrics dt")).to_have_count(18)
    expect(page.locator("#fields-quality")).to_contain_text('"quality"')
    page.keyboard.press("Escape")
    expect(page.locator("#detail-dialog")).to_be_hidden()
    with page.expect_download() as downloading:
        page.get_by_role("button", name="Export all filtered readings (CSV)").click()
    download = downloading.value
    destination = tmp_path / "synthetic.csv"
    download.save_as(destination)
    rows = list(csv.DictReader(io.StringIO(destination.read_text())))
    assert len(rows) == 3
    assert {r["record_id"] for r in rows} == {str(r.record_id) for r in payload.records}
    destination.unlink()
    # Headers-before-stream failures open a readable error tab, never a fake CSV.
    downloads = []
    page.on("download", lambda download: downloads.append(download))
    page.context.route(
        "**/readings.csv*",
        lambda route: route.fulfill(
            status=503, json={"error": {"code": "temporarily_unavailable"}}
        ),
    )
    with page.context.expect_page() as error_tab:
        page.get_by_role("button", name="Export all filtered readings (CSV)").click()
    popup = error_tab.value
    expect(popup.locator("body")).to_contain_text("temporarily_unavailable")
    assert downloads == []
    popup.close()
    page.context.unroute("**/readings.csv*")
    page.get_by_role("button", name="Next readings").click()
    expect(page.locator("#reading-count")).to_contain_text("offset 1")
    # Operator's metadata draft and table DOM survive status polling.
    page.get_by_label("Address", exact=True).fill("Unsaved draft")
    page.evaluate(r"""() => {
      window.savedRow = document.querySelector('#readings tr');
      const original = fetch; window.stats={active:0,max:0,status:0};
      window.fetch=async (...args)=> {
        if(!String(args[0]).match(/\/sites\/[0-9a-f-]+$/)) return original(...args);
        window.stats.active++; window.stats.status++;
        window.stats.max=Math.max(window.stats.max,window.stats.active);
        try {await new Promise(r=>setTimeout(r,500));return await original(...args);}
        finally {window.stats.active--;}
      };
    }""")
    page.wait_for_timeout(10500)
    assert page.evaluate("window.stats.status") >= 1
    assert page.evaluate("window.stats.max") == 1
    assert page.evaluate("window.savedRow === document.querySelector('#readings tr')")
    assert page.get_by_label("Address", exact=True).input_value() == "Unsaved draft"
    page.get_by_label("Received from (UTC, inclusive)").fill("2026-01-01T00:00:00")
    page.get_by_role("button", name="Apply filters").click()
    expect(page.locator("#error")).to_contain_text("invalid_input")
    page.get_by_label("Received from (UTC, inclusive)").fill("")
    page.get_by_label("Device UUID", exact=True).fill(str(uuid4()))
    page.get_by_role("button", name="Apply filters").click()
    expect(page.locator("#readings")).to_contain_text("No accepted readings")


def test_device_creation_grants_confirmations_errors_and_expiry(server_browser):
    page, origin, app, raw, summary, _ = server_browser
    register(page, raw)
    page.get_by_role("link", name="Open registered device").click()
    expect(page.locator("#status")).to_contain_text("Last authenticated contact (UTC)")
    expect(page.locator("#status")).to_contain_text("Unavailable")
    page.get_by_role("button", name="Revoke grant").click()
    expect(page.get_by_role("dialog")).to_contain_text("Pending Logger records")
    expect(page.get_by_role("dialog")).to_contain_text("Historical readings remain")
    page.keyboard.press("Escape")
    expect(page.get_by_role("button", name="Revoke grant")).to_have_count(1)
    page.get_by_role("button", name="Revoke grant").click()
    page.get_by_role("button", name="Confirm", exact=True).click()
    expect(page.locator("#grants")).to_contain_text("No authorized sites")
    page.get_by_label("Site UUID", exact=True).fill(summary["site_id"])
    page.get_by_role("button", name="Add site grant").click()
    expect(page.get_by_role("button", name="Revoke grant")).to_have_count(1)
    page.get_by_role("button", name="Disable credential").click()
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.get_by_role("button", name="Disable credential")).to_have_count(1)
    page.get_by_role("button", name="Disable credential").click()
    page.get_by_role("button", name="Confirm", exact=True).click()
    expect(page.get_by_role("button", name="Disable credential")).to_have_count(0)
    page.goto(origin)
    page.get_by_label("Logger registration JSON").fill(raw)
    page.get_by_role("button", name="Preview registration").click()
    expect(page.locator("#error")).to_contain_text("registration_conflict")
    # Separate site/device creation and first credential use the same guarded APIs.
    new_site, new_device = str(uuid4()), str(uuid4())
    page.get_by_text("Create site separately", exact=True).click()
    page.locator('#create-site input[name="site_id"]').fill(new_site)
    page.locator('#create-site textarea[name="address"]').fill("Separate synthetic site")
    page.get_by_role("button", name="Create site", exact=True).click()
    expect(page.locator("#sites")).to_contain_text(new_site)
    page.get_by_label("Device UUID", exact=True).fill(new_device)
    page.get_by_role("button", name="Create device", exact=True).click()
    expect(page.locator("#devices")).to_contain_text(new_device)
    page.goto(origin + "/devices/" + new_device)
    page.get_by_label("First credential Token").fill("b" * 43)
    page.get_by_role("button", name="Register first credential").click()
    expect(page.get_by_role("button", name="Disable credential")).to_have_count(1)
    page.get_by_label("Site UUID", exact=True).fill(new_site)
    page.get_by_role("button", name="Add site grant").click()
    expect(page.locator("#grants")).to_contain_text(new_site)
    page.goto(origin)
    page.route(
        "**/registrations/preview",
        lambda route: route.fulfill(
            status=503, json={"error": {"code": "temporarily_unavailable"}}
        ),
    )
    page.get_by_label("Logger registration JSON").fill(raw)
    page.get_by_role("button", name="Preview registration").click()
    expect(page.locator("#error")).to_contain_text("temporarily_unavailable")
    page.unroute("**/registrations/preview")
    app.state.auth.clear()
    page.get_by_role("button", name="Reload status / retry").click()
    expect(page.locator("#error")).to_have_text("Login expired. Log in again to continue.")
    assert page.locator("#summary").input_value() == ""
    expect(page.get_by_role("link", name="Log in again")).to_be_visible()


def test_late_preview_cancel_navigation_and_theme(server_browser):
    page, origin, _, raw, summary, _ = server_browser
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.get_by_role("button", name="Switch to light mode").click()
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    held = []

    def hold(route):
        held.append((route, route.fetch()))

    page.route("**/registrations/preview", hold)
    page.get_by_label("Logger registration JSON").fill(raw)
    page.get_by_role("button", name="Preview registration").click()
    page.wait_for_function("document.querySelector('#summary').value.length > 0")
    assert held
    page.get_by_role("button", name="Cancel registration").click()
    route, response = held.pop()
    route.fulfill(response=response)
    page.wait_for_timeout(200)
    expect(page.locator("#registration-preview")).to_be_hidden()
    assert page.locator("#summary").input_value() == ""
    assert summary["token"] not in page.content()
    page.unroute("**/registrations/preview")
    preview(page, raw)
    page.get_by_role("link", name="Dashboard", exact=True).click()
    expect(page.locator("#registration-preview")).to_be_hidden()
    assert page.locator("#summary").input_value() == ""
    assert page.evaluate("Object.keys(sessionStorage)") == []
    assert page.evaluate("Object.keys(localStorage)") == ["watt-profile-theme"]
    expect(page.locator("#devices")).to_contain_text("No devices found")
    # Hidden status polling is paused, and simultaneous refreshes cannot overlap.
    page.evaluate("""() => {
      window.pollStats={active:0,max:0,count:0}; const original=fetch;
      window.fetch=async (...args)=> {
        if(!String(args[0]).includes('/sites?')) return original(...args);
        const s=window.pollStats; s.active++; s.count++; s.max=Math.max(s.max,s.active);
        try {await new Promise(r=>setTimeout(r,300));return await original(...args);}
        finally{s.active--;}
      };
      Object.defineProperty(document,'hidden',{value:true,configurable:true});
      document.dispatchEvent(new Event('visibilitychange'));
    }""")
    page.get_by_role("button", name="Reload status / retry").click()
    assert page.evaluate("window.pollStats.count") == 0
    page.evaluate("""() => {
      Object.defineProperty(document,'hidden',{value:false,configurable:true});
      document.dispatchEvent(new Event('visibilitychange'));
      document.querySelector('#reload').click(); document.querySelector('#reload').click();
    }""")
    page.wait_for_timeout(500)
    assert page.evaluate("window.pollStats.count") == 1
    assert page.evaluate("window.pollStats.max") == 1


def test_login_safe_errors_and_theme(server_browser):
    page, origin, _, _, _, _ = server_browser
    page.goto(origin + "/login")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.get_by_role("button", name="Switch to light mode").click()
    page.get_by_label("Username").fill("admin")
    page.get_by_label("Password").fill("wrong-synthetic-password")
    page.get_by_role("button", name="Log in", exact=True).click()
    expect(page.locator("#error")).to_contain_text("Administrator credentials are invalid")
    assert page.get_by_label("Password").input_value() == ""
    page.get_by_role("link", name="Reload login form").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    page.locator('input[name="csrf"]').evaluate("e => e.value='expired-synthetic-form'")
    page.get_by_label("Username").fill("admin")
    page.get_by_label("Password").fill("synthetic-password-long-enough")
    page.get_by_role("button", name="Log in", exact=True).click()
    expect(page.locator("#error")).to_contain_text("Login form expired")
    assert page.get_by_label("Password").input_value() == ""
    page.route(
        "**/login",
        lambda route: (
            route.fulfill(status=429, json={"error": {"code": "login_throttled"}})
            if route.request.method == "POST"
            else route.continue_()
        ),
    )
    page.get_by_label("Password").fill("synthetic-password-long-enough")
    page.get_by_role("button", name="Log in", exact=True).click()
    expect(page.locator("#error")).to_contain_text("Wait one minute")
    assert page.get_by_label("Password").input_value() == ""
