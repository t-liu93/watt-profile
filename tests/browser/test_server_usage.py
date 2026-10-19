"""M5-13 average-day acceptance with synthetic PostgreSQL and real chart canvases."""

# ruff: noqa: F811
import os
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from urllib.parse import urlencode

import pytest
from playwright.sync_api import expect
from test_server_analysis import analysis_data  # noqa: F401
from test_server_auth import server_settings  # noqa: F401
from test_server_overview import login, query
from test_server_shell import shell  # noqa: F401

pytestmark = pytest.mark.skipif(
    not any("tests/browser" in str(arg) for arg in __import__("sys").argv[1:]),
    reason="Select tests/browser explicitly for browser acceptance",
)


def seed_usage(analysis_data):
    _, summary, _, add = analysis_data
    # Friday/Saturday, per-date counts 72 and 90: equal-date mean is 5, not 50/9.
    start = datetime(2026, 3, 5, 23, tzinfo=UTC)
    times, metrics = [], []
    for day, count, value in [(0, 72, "0"), (1, 90, "10")]:
        for index in range(count):
            times.append(start + timedelta(days=day, seconds=10 * index))
            metrics.append(
                {
                    "power_import_kw": Decimal(value),
                    "power_export_kw": Decimal("0.123456789012345678901") if day == 0 else None,
                }
            )
    session, _ = add(started_at=start, times=times, metrics=metrics)
    return summary, add, session


def assert_api_table(page, origin, site, group):
    params = query(page)
    args = {key: params[key] for key in ("session_id", "received_from", "received_before")}
    response = page.request.get(
        origin
        + f"/api/admin/v1/sites/{site}/analysis/typical-day?"
        + urlencode(args | {"group": group})
    )
    assert response.ok
    slots = response.json()["data"]["groups"][0]["slots"]

    def rounded(value):
        if value is None:
            return "Unavailable"
        with localcontext() as context:
            context.prec = 100
            return str(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    expected = [
        [slot["local_time"], rounded(slot["mean"]), str(slot["contributing_date_count"])]
        for slot in slots
    ]
    expect(page.locator("#typical-day-values tbody tr")).to_have_count(192)
    expect(page.locator("#typical-day-values tbody tr td:nth-child(3)")).to_have_text(
        [row[1] for row in expected]
    )
    actual = page.locator("#typical-day-values tbody tr").evaluate_all(
        "rows => rows.map(row => [row.cells[0].textContent, "
        "row.cells[2].textContent, row.cells[3].textContent])"
    )
    assert actual == expected
    return actual


def test_usage_direct_groups_exact_values_theme_language_url_and_overview(shell, analysis_data):
    page, origin, _ = shell
    login(page, origin)
    summary, _, session = seed_usage(analysis_data)
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    page.goto(
        origin + f"/sites/{summary.site_id}?tab=usage&raw_received_from=2000-01-01T00:00:00Z"
        "&raw_received_before=2000-01-02T00:00:00Z&raw_offset=200"
        "&raw_device_id=stale-device&raw_record_id=stale-record"
    )
    page.get_by_text("Average-day values and contributing dates", exact=True).click()
    values = assert_api_table(page, origin, summary.site_id, "all")
    assert values[0][0] == "00:00:00" and Decimal(values[0][1]) == 5 and values[0][2] == "2"
    assert values[1][1] == "0.12"
    assert values[1][2] == "1"
    assert values[2] == ["00:15:00", "Unavailable", "0"]
    assert query(page)["session_id"] == str(session.session_id)
    assert query(page)["received_before"] == "2026-03-06T23:14:50.000001Z"
    assert not any("/analysis/overview?" in url or "/analysis/latest?" in url for url in requests)
    expect(page.locator("canvas")).to_have_count(1)
    initial_reads = len([url for url in requests if "/analysis/typical-day?" in url])
    canvas = page.locator("canvas")
    old = canvas.evaluate("e => e.toDataURL()")
    page.get_by_role("button", name="Switch to light mode").click()
    page.wait_for_function("old => document.querySelector('canvas').toDataURL() !== old", arg=old)
    assert assert_api_table(page, origin, summary.site_id, "all") == values
    old = canvas.evaluate("e => e.toDataURL()")
    page.get_by_label("Language").select_option("zh")
    expect(page.get_by_role("heading", name="平均日电网功率")).to_be_visible()
    page.wait_for_function("old => document.querySelector('canvas').toDataURL() !== old", arg=old)
    chinese = page.locator("#typical-day-values tbody tr").evaluate_all(
        "rows => rows.map(row => [row.cells[0].textContent, "
        "row.cells[2].textContent, row.cells[3].textContent])"
    )
    assert chinese[0] == values[0] and chinese[1] == values[1]
    page.get_by_label("语言").select_option("en")
    # These preference-only redraws issue no history requests (API comparison calls excluded).
    assert len([url for url in requests if "/analysis/typical-day?" in url]) == initial_reads
    for group, value in [("weekday", "0"), ("weekend", "10")]:
        page.get_by_label("Date group", exact=True).select_option(group)
        rows = assert_api_table(page, origin, summary.site_id, group)
        assert Decimal(rows[0][1]) == Decimal(value) and rows[0][2] == "1"
        assert query(page)["date_group"] == group
    saved = page.url
    page.reload()
    page.get_by_text("Average-day values and contributing dates", exact=True).click()
    expect(page.get_by_label("Date group", exact=True)).to_have_value("weekend")
    assert_api_table(page, origin, summary.site_id, "weekend")
    assert page.url == saved
    page.go_back()
    expect(page.get_by_label("Date group", exact=True)).to_have_value("weekday")
    assert_api_table(page, origin, summary.site_id, "weekday")
    page.get_by_label("First date", exact=True).fill("2026-03-06")
    page.get_by_label("Last date", exact=True).fill("2026-03-07")
    page.get_by_role("button", name="Apply dates", exact=True).click()
    assert_api_table(page, origin, summary.site_id, "weekday")
    custom = query(page)
    page.get_by_role("link", name="Overview", exact=True).click()
    expect(page.locator("#period-statistics")).to_be_visible()
    assert query(page) == custom | {"tab": "overview"}
    page.get_by_role("link", name="Usage patterns", exact=True).click()
    page.get_by_text("Average-day values and contributing dates", exact=True).click()
    assert_api_table(page, origin, summary.site_id, "weekday")
    assert query(page) == custom
    screenshots = os.environ.get("WATT_TEST_SCREENSHOT_DIR")
    if screenshots:
        page.get_by_text("Average-day values and contributing dates", exact=True).click()
        target = Path(screenshots)
        target.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(target / "usage-light-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 360, "height": 900})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(target / "usage-light-360.png"), full_page=True)
    page.get_by_role("link", name="Inspect raw readings in selected range", exact=True).click()
    raw = query(page)
    assert raw["raw_session_id"] == str(session.session_id)
    assert "raw_record_id" not in raw and "raw_device_id" not in raw
    assert raw["received_from"] == custom["received_from"]
    assert raw["received_before"] == custom["received_before"]
    assert raw["raw_received_from"] == custom["received_from"]
    assert raw["raw_received_before"] == custom["received_before"]
    assert "raw_offset" not in raw


def test_usage_noeligible_refresh_failure_no_history_poll_and_session_selection(
    shell, analysis_data
):
    page, origin, _ = shell
    login(page, origin)
    summary, add, session = seed_usage(analysis_data)
    older, _ = add(
        started_at=session.started_at - timedelta(days=1),
        times=[session.started_at - timedelta(hours=2)],
    )
    page.clock.install()
    page.clock.pause_at(datetime.now(UTC) + timedelta(seconds=1))
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    page.goto(origin + f"/sites/{summary.site_id}?tab=usage")
    expect(page.locator("canvas")).to_have_count(1)
    page.get_by_text("Average-day values and contributing dates", exact=True).click()
    assert_api_table(page, origin, summary.site_id, "all")
    initial = len([url for url in requests if "/analysis/typical-day?" in url])
    page.clock.run_for(30000)
    assert len([url for url in requests if "/analysis/typical-day?" in url]) == initial
    panel = page.get_by_role("heading", name="Average-day grid power").locator("..")
    timestamp = panel.locator("time").text_content()
    before = page.locator("#typical-day-values").inner_text()
    page.route(
        "**/analysis/typical-day?*",
        lambda route: route.fulfill(
            status=503, json={"error": {"code": "temporarily_unavailable"}}
        ),
    )
    page.get_by_role("button", name="Refresh analysis", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("Refresh failed")
    assert panel.locator("time").text_content() == timestamp
    assert page.locator("#typical-day-values").inner_text() == before
    page.unroute("**/analysis/typical-day?*")
    page.get_by_label("Monitoring session").select_option(str(older.session_id))
    expect(page.get_by_text("No eligible dates for this group.", exact=False)).to_be_visible()
    for group in ("all", "weekday", "weekend"):
        page.get_by_label("Date group", exact=True).select_option(group)
        rows = assert_api_table(page, origin, summary.site_id, group)
        assert all(row[1:] == ["Unavailable", "0"] for row in rows)
    page.get_by_role("button", name="Refresh analysis", exact=True).click()
    expect(page.get_by_label("Monitoring session")).to_have_value(str(session.session_id))
    assert_api_table(page, origin, summary.site_id, "weekend")
    page.get_by_label("First date", exact=True).fill("2026-03-04")
    page.get_by_label("Last date", exact=True).fill("2026-03-04")
    page.get_by_role("button", name="Apply dates", exact=True).click()
    expect(page.get_by_text("No eligible dates for this group.", exact=False)).to_be_visible()
    saved = page.url
    page.get_by_role("button", name="Refresh analysis", exact=True).click()
    assert page.url == saved
    page.reload()
    expect(page.get_by_text("No eligible dates for this group.", exact=False)).to_be_visible()
    assert page.url == saved


@pytest.mark.parametrize("obsolete_status", [200, 503])
def test_usage_obsolete_group_and_refresh_callbacks(shell, analysis_data, obsolete_status):
    page, origin, _ = shell
    login(page, origin)
    summary, _, _ = seed_usage(analysis_data)
    page.goto(origin + f"/sites/{summary.site_id}?tab=usage")
    expect(page.locator("canvas")).to_have_count(1)
    held = []
    page.route("**/analysis/typical-day?*", lambda route: held.append(route))
    page.get_by_label("Date group", exact=True).select_option("weekday")
    page.wait_for_timeout(100)
    assert held
    page.get_by_label("Date group", exact=True).select_option("weekend")
    page.wait_for_timeout(100)
    newest = held.pop()
    newest.fulfill(response=page.request.get(newest.request.url))
    expect(page.locator("canvas")).to_have_count(1)
    for route in held:
        route.fulfill(
            response=page.request.get(route.request.url)
        ) if obsolete_status == 200 else route.fulfill(
            status=503, json={"error": {"code": "temporarily_unavailable"}}
        )
    expect(page.get_by_role("alert")).to_have_count(0)
    assert query(page)["date_group"] == "weekend"
    page.unroute("**/analysis/typical-day?*")
    page.get_by_text("Average-day values and contributing dates", exact=True).click()
    assert_api_table(page, origin, summary.site_id, "weekend")
    held.clear()
    page.route("**/analysis/sessions?*", lambda route: held.append(route))
    page.get_by_role("button", name="Refresh analysis", exact=True).click()
    page.wait_for_timeout(100)
    assert held
    page.get_by_role("link", name="Overview", exact=True).click()
    expect(page.locator("#period-statistics")).to_be_visible()
    saved = page.url
    for route in held:
        route.fulfill(response=page.request.get(route.request.url))
    page.wait_for_timeout(100)
    assert page.url == saved
