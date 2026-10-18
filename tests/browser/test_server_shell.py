"""React shell acceptance on a real isolated Server and PostgreSQL."""

# ruff: noqa: F811
import os
import socket
import threading
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright
from test_server_auth import server_settings  # noqa: F401

from watt_profile.server.app import create_app

pytestmark = pytest.mark.skipif(
    not any("tests/browser" in str(arg) for arg in __import__("sys").argv[1:]),
    reason="Select tests/browser explicitly for browser acceptance",
)


@pytest.fixture
def shell(server_engine, server_settings):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    app = create_app(replace(server_settings, public_origin=origin))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.01)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            context.route(
                "**/*",
                lambda route: (
                    route.continue_()
                    if route.request.url.startswith(origin + "/")
                    else route.abort()
                ),
            )
            page = context.new_page()
            yield page, origin, app
            context.close()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
        assert not thread.is_alive()


def login(page, origin):
    page.goto(origin)
    page.get_by_label("Username").fill("admin")
    page.get_by_label("Password").fill("synthetic-password-long-enough")
    page.get_by_role("button", name="Log in", exact=True).click()
    expect(page.get_by_role("status")).to_have_text("Administrator session active")


def test_shell_routes_preferences_accessibility_and_motion(shell, tmp_path):
    page, origin, _ = shell
    login(page, origin)
    screenshots = Path(os.environ.get("WATT_TEST_SCREENSHOT_DIR", str(tmp_path / "screenshots")))
    screenshots.mkdir(exist_ok=True)
    page.screenshot(path=str(screenshots / "dark-desktop.png"), full_page=True)
    for path in (
        "/",
        "/sites",
        "/devices",
        "/registration",
        f"/sites/{uuid4()}",
        f"/devices/{uuid4()}",
    ):
        page.goto(origin + path)
        expect(page.get_by_role("status")).to_have_text("Administrator session active")
        page.reload()
        expect(page.get_by_role("status")).to_have_text("Administrator session active")
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted:true}))")
    expect(page.locator(".workspace")).to_have_count(0)
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
    expect(page.get_by_role("status")).to_have_text("Administrator session active")
    page.get_by_role("button", name="Switch to light mode").click()
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    page.screenshot(path=str(screenshots / "light-desktop.png"), full_page=True)
    page.get_by_label("Language").select_option("zh")
    expect(page.get_by_role("heading", name="Watt Profile 服务器")).to_be_visible()
    page.reload()
    expect(page.locator("html")).to_have_attribute("lang", "zh-CN")
    assert set(page.evaluate("Object.keys(localStorage)")) == {
        "watt-profile-theme",
        "watt-profile-language",
    }
    assert page.evaluate("Object.keys(sessionStorage)") == []
    for width in (1024, 360):
        page.set_viewport_size({"width": width, "height": 800})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(screenshots / "light-zh-360.png"), full_page=True)
    page.get_by_role("button", name="导航", exact=True).click()
    expect(page.get_by_role("dialog")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_be_hidden()
    expect(page.get_by_role("button", name="导航", exact=True)).to_be_focused()
    page.keyboard.press("Shift+Tab")
    assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle != 'none'")
    page.emulate_media(reduced_motion="reduce")
    assert (
        page.locator(".neon-panel").evaluate("e => getComputedStyle(e,'::before').animationName")
        == "none"
    )
    page.emulate_media(reduced_motion="no-preference")
    page.evaluate("""() => {
      Object.defineProperty(document, 'hidden', {value:true, configurable:true});
      document.dispatchEvent(new Event('visibilitychange'));
    }""")
    expect(page.locator("html")).to_have_attribute("data-hidden", "true")
    assert (
        page.locator(".neon-panel").evaluate(
            "e => getComputedStyle(e,'::before').animationPlayState"
        )
        == "paused"
    )


def test_native_auth_errors_csrf_logout_and_expiry(shell):
    page, origin, app = shell
    page.goto(origin + "/login")
    page.get_by_label("Username").fill("admin")
    page.get_by_label("Password").fill("wrong-password")
    page.get_by_role("button", name="Log in", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("Administrator credentials are invalid")
    assert page.get_by_label("Password").input_value() == ""
    page.locator('input[name="csrf"]').first.evaluate("e => e.value='bad-csrf'")
    page.get_by_label("Password").fill("synthetic-password-long-enough")
    page.get_by_role("button", name="Log in", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("Login form expired")
    login(page, origin)
    csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")
    assert page.request.post(origin + "/logout", form={"csrf": "bad"}).status == 403
    assert page.request.post(origin + "/api/admin/v1/devices", data={}).status == 403
    assert (
        page.request.post(
            origin + "/api/admin/v1/devices", data={}, headers={"X-CSRF-Token": csrf}
        ).status
        == 422
    )
    page.get_by_role("button", name="Log out", exact=True).click()
    expect(page.get_by_role("heading", name="Server login")).to_be_visible()
    assert page.request.get(origin + "/api/admin/v1/devices").status == 401
    # A native browser POST retains the existing Server 303 and root target.
    page.get_by_label("Username").fill("admin")
    page.get_by_label("Password").fill("synthetic-password-long-enough")
    page.locator('form[action="/login"]').first.evaluate("form => form.submit()")
    expect(page.get_by_role("status")).to_have_text("Administrator session active")
    app.state.auth.clear()
    page.get_by_role("link", name="Devices", exact=True).click()
    expect(page.get_by_role("heading", name="Server login")).to_be_visible()
    assert page.locator('meta[name="csrf-token"]').get_attribute("content") != csrf
    assert page.locator(".sidebar").count() == 0


def test_browser_language_default_and_stale_route_response(shell):
    page, origin, _ = shell
    page.add_init_script("Object.defineProperty(navigator,'language',{value:'zh-CN'})")
    page.goto(origin + "/login")
    expect(page.get_by_role("heading", name="服务器登录")).to_be_visible()
    page.get_by_role("button", name="登录", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text("请输入用户名和密码。")
    expect(page.get_by_label("用户名")).to_be_focused()
    page.get_by_label("语言").select_option("en")
    expect(page.get_by_role("alert")).to_have_text("Enter a username and password.")
    login(page, origin)
    held = []
    page.route("**/api/admin/v1/devices?limit=1", lambda route: held.append(route))
    page.get_by_role("link", name="Devices", exact=True).click()
    expect(page.get_by_role("status")).to_have_text("Checking session…")
    assert held
    page.get_by_role("link", name="Registration", exact=True).click()
    expect(page.get_by_role("heading", name="Registration", exact=True)).to_be_visible()
    page.unroute("**/api/admin/v1/devices?limit=1")
    for route in held:
        try:
            route.fulfill(status=503, json={"error": {"code": "temporarily_unavailable"}})
        except Exception:
            pass  # Chromium may already have cancelled the obsolete transport.
    page.get_by_role("link", name="Sites", exact=True).click()
    expect(page.get_by_role("status")).to_have_text("Administrator session active")
    expect(page.get_by_role("alert")).to_have_count(0)
