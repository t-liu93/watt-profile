"""M5-12 overview acceptance on disposable PostgreSQL and a real Chromium canvas."""

# ruff: noqa: F811
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from playwright.sync_api import expect
from test_server_analysis import analysis_data  # noqa: F401
from test_server_auth import server_settings  # noqa: F401
from test_server_shell import shell  # noqa: F401

from watt_profile.server.registration import SiteInput

pytestmark = pytest.mark.skipif(
    not any("tests/browser" in str(arg) for arg in __import__("sys").argv[1:]),
    reason="Select tests/browser explicitly for browser acceptance",
)


def login(page, origin):
    page.goto(origin)
    page.get_by_label("Username").fill("admin")
    page.get_by_label("Password").fill("synthetic-password-long-enough")
    page.get_by_role("button", name="Log in", exact=True).click()
    expect(page.locator("#sites")).to_be_visible()


def seed(analysis_data):
    _, summary, _, add = analysis_data
    start = datetime(2026, 3, 28, 22, tzinfo=UTC)
    times = [start + timedelta(seconds=i * 10) for i in range(3)]
    times += [start + timedelta(days=1), start + timedelta(days=1, seconds=10)]
    session, records = add(
        started_at=start,
        times=times,
        metrics=[
            {
                "power_import_kw": Decimal(i),
                "power_export_kw": Decimal("0.123456789012345678901"),
                "energy_import_t1_kwh": Decimal(i),
                "energy_import_t2_kwh": Decimal(i),
                "energy_export_t1_kwh": Decimal(i),
                "energy_export_t2_kwh": Decimal(i),
            }
            for i in range(len(times))
        ],
    )
    return summary, add, session, records


def query(page):
    return {key: values[0] for key, values in parse_qs(urlparse(page.url).query).items()}


def test_overview_exact_range_charts_zoom_dates_history_and_latest(shell, analysis_data):
    page, origin, _ = shell
    login(page, origin)
    summary, add, session, records = seed(analysis_data)
    historical, _ = add(
        started_at=session.started_at - timedelta(days=20),
        times=[session.started_at - timedelta(days=10)],
    )
    page.goto(origin + f"/sites/{summary.site_id}")
    expect(page.locator("#period-statistics")).to_contain_text("0.123456789012345678901")
    assert query(page)["session_id"] == str(session.session_id)
    assert query(page)["received_from"] == "2026-03-28T22:00:00.000000Z"
    assert query(page)["received_before"] == "2026-03-29T22:00:10.000001Z"
    expect(page.get_by_text("Readings: 5", exact=False)).to_be_visible()
    expect(page.locator("canvas")).to_have_count(2)
    assert page.locator("canvas").first.evaluate("e => e.width > 500 && e.height > 100")
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    before = page.locator("#period-statistics").inner_text()
    # Operate the real ECharts slider and its finer HTTP request.
    canvas = page.locator("canvas").first
    canvas.scroll_into_view_if_needed()
    box = canvas.bounding_box()
    page.mouse.move(box["x"] + 70, box["y"] + box["height"] - 25)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.4, box["y"] + box["height"] - 25, steps=10)
    page.mouse.up()
    page.wait_for_timeout(400)
    # A precise slider click is renderer dependent; retry on the left handle at slider center.
    if not any("/analysis/power?" in url for url in requests):
        page.mouse.move(box["x"] + 65, box["y"] + box["height"] - 32)
        page.mouse.down()
        page.mouse.move(box["x"] + 250, box["y"] + box["height"] - 32, steps=10)
        page.mouse.up()
    page.wait_for_function("document.querySelectorAll('canvas').length === 2")
    assert any("/analysis/power?" in url for url in requests)
    zoom_request = next(url for url in requests if "/analysis/power?" in url)
    assert parse_qs(urlparse(zoom_request).query)["zoom_before"] == [query(page)["received_before"]]
    assert len([url for url in requests if "/analysis/overview?" in url]) == 0
    assert page.locator("#period-statistics").inner_text() == before
    old_canvas = page.locator("canvas").first.evaluate("e => e.toDataURL()")
    page.get_by_role("button", name="Switch to light mode").click()
    page.wait_for_function(
        "old => document.querySelector('canvas').toDataURL() !== old", arg=old_canvas
    )
    screenshot_dir = os.environ.get("WATT_TEST_SCREENSHOT_DIR")
    if screenshot_dir:
        target = Path(screenshot_dir)
        target.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(target / "overview-light-desktop.png"), full_page=True)
        page.get_by_role("button", name="Switch to dark mode").click()
        page.screenshot(path=str(target / "overview-dark-desktop.png"), full_page=True)
        for width in (1024, 360):
            page.set_viewport_size({"width": width, "height": 900})
            page.wait_for_timeout(200)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            expect(page.locator("canvas")).to_have_count(2)
            page.screenshot(path=str(target / f"overview-dark-{width}.png"), full_page=True)
        page.set_viewport_size({"width": 1440, "height": 900})
    page.get_by_label("Language").select_option("zh")
    expect(page.get_by_role("heading", name="功率平均值与最小—最大包络")).to_be_visible()
    page.get_by_label("开始日期", exact=True).fill("")
    page.get_by_role("button", name="应用日期", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text("请选择有效完整日期，最多 31 个阿姆斯特丹日期。")
    page.get_by_label("语言").select_option("en")
    page.get_by_label("First date", exact=True).fill("2026-03-29")
    page.get_by_label("Last date", exact=True).fill("2026-03-29")
    page.get_by_role("button", name="Apply dates", exact=True).click()
    expect(page.locator("#period-statistics")).to_be_visible()
    assert query(page)["received_from"] == "2026-03-28T23:00:00.000Z"
    assert query(page)["received_before"] == "2026-03-29T22:00:00.000Z"
    assert query(page)["range_mode"] == "custom"
    custom = page.url
    page.get_by_role("button", name="Refresh analysis", exact=True).click()
    assert page.url == custom
    page.reload()
    expect(page.locator("#period-statistics")).to_be_visible()
    assert page.url == custom
    page.get_by_role("link", name="Settings", exact=True).click()
    assert query(page)["session_id"] == str(session.session_id)
    page.get_by_role("link", name="Overview", exact=True).click()
    expect(page.locator("#period-statistics")).to_be_visible()
    page.go_back()
    expect(page.locator("#edit-site")).to_be_visible()
    page.go_back()
    expect(page.locator("#period-statistics")).to_be_visible()
    page.get_by_role("button", name="Recent seven dates", exact=True).click()
    expect(page.get_by_label("Monitoring session")).to_have_value(str(session.session_id))
    page.get_by_label("Monitoring session").select_option(str(historical.session_id))
    expect(page.get_by_label("Monitoring session")).to_have_value(str(historical.session_id))
    expect(page.get_by_text("Readings: 1", exact=False)).to_be_visible()
    page.get_by_role("button", name="Refresh analysis", exact=True).click()
    expect(page.get_by_label("Monitoring session")).to_have_value(str(session.session_id))
    expect(page.get_by_text("Readings: 5", exact=False)).to_be_visible()
    assert records[-1].record_id.hex not in page.url
    page.get_by_role("link", name="Inspect raw record").first.click()
    assert query(page)["raw_session_id"] == str(session.session_id)
    assert query(page)["raw_device_id"] == str(summary.device_id)
    assert query(page)["raw_record_id"]
    assert query(page)["received_before"] == "2026-03-29T22:00:10.000001Z"


def test_overview_poll_ownership_pause_hidden_failures_and_expiry(shell, analysis_data):
    page, origin, app = shell
    login(page, origin)
    summary, _, session, _ = seed(analysis_data)
    page.clock.install()
    page.clock.pause_at(datetime.now(UTC) + timedelta(seconds=1))
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    page.goto(origin + f"/sites/{summary.site_id}")
    expect(page.locator("#period-statistics")).to_be_visible()
    latest_heading = page.get_by_role("heading", name="Latest complete reading in selected session")
    latest_panel = latest_heading.locator("..")
    expect(latest_panel.locator("time")).to_be_visible()
    initial_history = len([url for url in requests if "/analysis/overview?" in url])
    initial_latest = len([url for url in requests if "/analysis/latest?" in url])
    page.clock.run_for(9999)
    assert len([url for url in requests if "/analysis/latest?" in url]) == initial_latest
    page.clock.run_for(1)
    expect(latest_panel.locator("time")).to_be_visible()
    page.wait_for_timeout(50)
    assert len([url for url in requests if "/analysis/latest?" in url]) == initial_latest + 1
    assert len([url for url in requests if "/analysis/overview?" in url]) == initial_history
    original = page.url
    page.get_by_role("button", name="Pause latest refresh", exact=True).click()
    page.clock.run_for(20000)
    assert len([url for url in requests if "/analysis/latest?" in url]) == initial_latest + 1
    page.get_by_role("button", name="Resume latest refresh", exact=True).click()
    held = []
    page.route("**/analysis/latest?*", lambda route: held.append(route))
    page.clock.run_for(10000)
    assert len(held) == 1
    page.clock.run_for(10000)
    assert len(held) == 1  # no overlap
    held.pop().fulfill(status=503, json={"error": {"code": "temporarily_unavailable"}})
    expect(latest_panel.get_by_role("alert")).to_be_visible()
    expect(latest_panel.locator("time")).to_be_visible()
    assert page.url == original
    page.evaluate(
        """() => {
        Object.defineProperty(document,'hidden',{value:true,configurable:true});
        document.dispatchEvent(new Event('visibilitychange')); }"""
    )
    page.clock.run_for(30000)
    assert not held
    page.unroute("**/analysis/latest?*")
    page.evaluate(
        """() => {
        Object.defineProperty(document,'hidden',{value:false,configurable:true});
        document.dispatchEvent(new Event('visibilitychange')); }"""
    )
    expect(latest_panel.get_by_role("alert")).to_have_count(0)
    overview = page.locator("#period-statistics").inner_text()
    page.route(
        "**/analysis/overview?*",
        lambda route: route.fulfill(
            status=503, json={"error": {"code": "temporarily_unavailable"}}
        ),
    )
    page.get_by_role("button", name="Refresh analysis", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("Refresh failed")
    assert page.locator("#period-statistics").inner_text() == overview
    page.unroute("**/analysis/overview?*")
    app.state.auth.clear()
    page.clock.run_for(10000)
    expect(page.get_by_role("heading", name="Server login", exact=True)).to_be_visible()
    assert str(session.session_id) not in page.content()


@pytest.mark.parametrize("obsolete_status", [200, 503])
def test_overview_empty_and_obsolete_success_failure(shell, analysis_data, obsolete_status):
    page, origin, _ = shell
    login(page, origin)
    summary, _, _, _ = seed(analysis_data)
    empty_id = uuid4()
    csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")
    response = page.request.post(
        origin + "/api/admin/v1/sites",
        headers={"X-CSRF-Token": csrf},
        data=SiteInput(site_id=empty_id, address="Empty synthetic", description="").model_dump(
            mode="json"
        ),
    )
    assert response.ok
    page.goto(origin + f"/sites/{empty_id}")
    expect(page.get_by_text("No readings in this session or site.", exact=True)).to_be_visible()
    assert page.locator("canvas").count() == 0
    held = []
    page.route("**/analysis/overview?*", lambda route: held.append(route))
    page.goto(origin + f"/sites/{summary.site_id}")
    expect(page.get_by_label("Monitoring session")).not_to_have_value("")
    page.wait_for_timeout(100)
    assert held
    page.get_by_label("First date", exact=True).fill("2026-03-29")
    page.get_by_label("Last date", exact=True).fill("2026-03-29")
    page.get_by_role("button", name="Apply dates", exact=True).click()
    page.wait_for_timeout(100)
    assert len(held) >= 2
    newest = held.pop()
    newest.fulfill(response=page.request.get(newest.request.url))
    expect(page.locator("#period-statistics")).to_be_visible()
    for route in held:
        if obsolete_status == 200:
            route.fulfill(response=page.request.get(route.request.url))
        else:
            route.fulfill(status=503, json={"error": {"code": "temporarily_unavailable"}})
    expect(page.get_by_role("alert")).to_have_count(0)
    assert query(page)["range_mode"] == "custom"


def test_sessions_pagination_refresh_race_and_timeout(shell, analysis_data):
    page, origin, _ = shell
    login(page, origin)
    summary, add, session, _ = seed(analysis_data)
    for index in range(21):
        add(
            started_at=session.started_at - timedelta(days=50 + index),
            times=[session.started_at - timedelta(days=30 + index)],
        )
    page.goto(origin + f"/sites/{summary.site_id}")
    expect(page.locator("#period-statistics")).to_be_visible()
    page.get_by_role("button", name="Next sessions", exact=True).click()
    expect(page.get_by_label("Monitoring session").locator("option")).to_have_count(4)
    options = (
        page.get_by_label("Monitoring session")
        .locator("option")
        .evaluate_all("items => items.map(item => item.value).filter(Boolean)")
    )
    older = next(value for value in options if value != str(session.session_id))
    page.get_by_label("Monitoring session").select_option(older)
    expect(page.get_by_text("Readings: 1", exact=False)).to_be_visible()
    held = []
    page.route("**/analysis/sessions?*", lambda route: held.append(route))
    page.get_by_role("button", name="Refresh analysis", exact=True).click()
    page.wait_for_timeout(100)
    assert len(held) == 1
    # A later user selection owns the URL even if the context refresh then completes.
    another = next(
        value
        for value in page.get_by_label("Monitoring session")
        .locator("option")
        .evaluate_all("items => items.map(item => item.value).filter(Boolean)")
        if value != older
    )
    page.get_by_label("Monitoring session").select_option(another)
    new_url = page.url
    route = held.pop()
    route.fulfill(response=page.request.get(route.request.url))
    page.wait_for_timeout(100)
    assert page.url == new_url
    page.unroute("**/analysis/sessions?*")
    page.clock.install()
    page.clock.pause_at(datetime.now(UTC) + timedelta(seconds=1))
    page.route("**/analysis/overview?*", lambda route: held.append(route))
    page.get_by_label("First date", exact=True).fill("2026-03-29")
    page.get_by_label("Last date", exact=True).fill("2026-03-29")
    page.get_by_role("button", name="Apply dates", exact=True).click()
    page.wait_for_timeout(100)
    assert held
    assert page.locator("#period-statistics").count() == 0
    page.clock.run_for(22001)
    expect(page.get_by_role("alert")).to_contain_text("timed out")
    assert page.locator("#period-statistics").count() == 0
