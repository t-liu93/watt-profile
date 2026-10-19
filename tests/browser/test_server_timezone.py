"""Browser-native instant presentation without changing the Amsterdam calendar."""

# ruff: noqa: F811
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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


@pytest.mark.parametrize(
    "zone,mode,first,last",
    [
        ("Asia/Tokyo", "native", "2026-03-08 15:59:59", "2026-03-08 16:00:09"),
        ("America/New_York", "native", "2026-03-08 01:59:59", "2026-03-08 03:00:09"),
        ("Asia/Kathmandu", "native", "2026-03-08 12:44:59", "2026-03-08 12:45:09"),
        ("UTC", "native", "2026-03-08 06:59:59", "2026-03-08 07:00:09"),
        ("America/Los_Angeles", "native", "2026-03-07 22:59:59", "2026-03-07 23:00:09"),
        ("Asia/Tokyo", "absent", "2026-03-08T06:59:59Z", "2026-03-08T07:00:09Z"),
    ],
)
def test_browser_timezone_instants_charts_and_calendar(
    shell, analysis_data, zone, mode, first, last, tmp_path
):
    original_page, origin, _ = shell
    _, summary, _, add = analysis_data
    start = datetime(2026, 3, 8, 6, 59, 59, 123456, tzinfo=UTC)
    metrics = {
        "power_import_kw": Decimal("1.005"),
        "voltage_l1_v": Decimal("230.005"),
        "current_l1_a": Decimal("1.005"),
        "power_import_l1_kw": Decimal("1.005"),
    }
    session, _ = add(
        started_at=start,
        times=[start, start + timedelta(seconds=10)],
        metrics=[metrics, metrics],
    )
    context = original_page.context.browser.new_context(
        timezone_id=zone, viewport={"width": 1440, "height": 900}
    )
    context.route(
        "**/*",
        lambda route: (
            route.continue_() if route.request.url.startswith(origin + "/") else route.abort()
        ),
    )
    # Test-only canvas observation, installed before rendering; no production hooks.
    context.add_init_script("""window.painted = [];
      const paint = CanvasRenderingContext2D.prototype.fillText;
      CanvasRenderingContext2D.prototype.fillText = function(text, ...args) {
        window.painted.push(String(text)); return paint.call(this, text, ...args);
      };""")
    if mode == "absent":
        context.add_init_script("""const resolved = Intl.DateTimeFormat.prototype.resolvedOptions;
          Intl.DateTimeFormat.prototype.resolvedOptions = function() {
            const options = resolved.call(this); delete options.timeZone; return options;
          };""")
    page = context.new_page()
    try:
        login(page, origin)
        page.goto(origin + f"/devices/{summary.device_id}")
        expect(page.locator("#status")).to_contain_text(last)
        assert "(UTC)" not in page.locator("#status").inner_text()
        page.goto(origin + f"/sites/{summary.site_id}")
        expect(page.locator("#period-statistics")).to_be_visible()
        expect(page.get_by_label("Monitoring session").locator("option:checked")).to_contain_text(
            first
        )
        expect(page.locator("body")).to_contain_text(last)
        original_query = query(page)
        assert original_query["received_before"] == "2026-03-08T07:00:09.123457Z"
        assert "." in page.locator("time").first.get_attribute("datetime")
        for tab, count in [("overview", 2), ("phases", 3)]:
            if tab == "phases":
                page.goto(origin + f"/sites/{summary.site_id}?tab=phases")
            expect(page.locator("canvas")).to_have_count(count)
            # Axis text is painted by the actual power/phase chart renderer.
            page.wait_for_function(
                "window.painted.some(s => /^(2026-03-0[78]T|03-0[78] )/.test(s))"
            )
            axes = page.evaluate(
                "window.painted.filter(s => /^(2026-03-0[78]T|03-0[78] )/.test(s))"
            )
            assert any(
                (first[:10] in text if mode == "absent" else first[5:10] in text) for text in axes
            ), axes
            if mode == "native":
                assert all("T" not in text and "Z" not in text for text in axes), axes
            for canvas in page.locator("canvas").all()[: 1 if tab == "overview" else 3]:
                canvas.scroll_into_view_if_needed()
                box = canvas.bounding_box()
                page.evaluate("window.painted = []")
                page.mouse.move(box["x"] + 67, box["y"] + 140)
                page.wait_for_function("expected => window.painted.includes(expected)", arg=first)
                painted = page.evaluate("window.painted")
                assert first in painted, painted
                assert any("1.01" in text or "230.01" in text for text in painted), painted
                page.mouse.move(0, 0)
            if tab == "overview" and zone == "Asia/Tokyo" and mode == "native":
                page.set_viewport_size({"width": 360, "height": 900})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.wait_for_timeout(300)
                page.screenshot(path=str(tmp_path / "power-360.png"), full_page=True)
                page.set_viewport_size({"width": 1440, "height": 900})
            page.evaluate("window.painted = []")
        page.set_viewport_size({"width": 360, "height": 900})
        expect(page.locator("canvas")).to_have_count(3)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if zone == "Asia/Tokyo" and mode == "native":
            page.wait_for_timeout(300)
            page.screenshot(path=str(tmp_path / "phases-360.png"), full_page=True)
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(origin + f"/sites/{summary.site_id}?tab=quality")
        expect(page.get_by_role("heading", name="Nominal sampling coverage")).to_be_visible()
        expect(page.locator("body")).to_contain_text(first + " → " + last)
        page.goto(origin + f"/sites/{summary.site_id}?tab=usage")
        expect(page.get_by_role("heading", name="Average-day grid power")).to_be_visible()
        page.get_by_text("Average-day values and contributing dates", exact=True).click()
        expect(page.locator("#typical-day-values")).to_contain_text("08:00")
        expect(page.locator("body")).to_contain_text("Amsterdam local time")
        # Explicit complete dates remain Amsterdam dates even in another browser zone.
        page.get_by_label("First date", exact=True).fill("2026-03-29")
        page.get_by_label("Last date", exact=True).fill("2026-03-29")
        page.get_by_role("button", name="Apply dates", exact=True).click()
        expect(page).to_have_url(re.compile("range_mode=custom"))
        assert query(page)["received_from"] == "2026-03-28T23:00:00.000Z"
        assert query(page)["received_before"] == "2026-03-29T22:00:00.000Z"
        raw = {
            "tab": "raw",
            "raw_session_id": str(session.session_id),
            "raw_received_from": "2026-03-08T06:59:59.123456Z",
            "raw_received_before": "2026-03-08T06:59:59.123457Z",
        }
        page.goto(origin + f"/sites/{summary.site_id}?" + urlencode(raw))
        expect(page.locator("#readings tbody tr")).to_have_count(1)
        expect(page.locator("#readings tbody tr td").first).to_have_text(first)
        expect(page.get_by_label("Received from (UTC, inclusive)")).to_have_value(
            raw["raw_received_from"]
        )
        page.get_by_role("button", name="Switch to light mode").click()
        page.get_by_label("Language").select_option("zh")
        expect(page.locator("#readings tbody tr td").first).to_have_text(first)
        assert set(page.evaluate("Object.keys(localStorage)")) == {
            "watt-profile-theme",
            "watt-profile-language",
        }
    finally:
        context.close()
