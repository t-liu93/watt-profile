"""M5-14 phase diagnostics acceptance on disposable PostgreSQL and real charts."""

# ruff: noqa: F811
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

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


def seed_phases(analysis_data):
    _, summary, _, add = analysis_data
    start = datetime(2026, 3, 8, 12, tzinfo=UTC)
    single_metrics = {
        "voltage_l1_v": Decimal("230"),
        "current_l1_a": Decimal("0"),
        "power_import_l1_kw": Decimal("0"),
        "power_export_l1_kw": Decimal("1"),
    }
    single, _ = add(started_at=start, times=[start], metrics=[single_metrics])

    full_start = start + timedelta(days=2)
    times = [full_start, full_start + timedelta(seconds=10)]
    metrics = []
    for index in range(2):
        item = {}
        for phase, n in zip(("l1", "l2", "l3"), (1, 2, 3), strict=True):
            item[f"voltage_{phase}_v"] = Decimal(230 + n + index)
            item[f"current_{phase}_a"] = Decimal(index * n)
            item[f"power_import_{phase}_kw"] = Decimal(index * n)
            item[f"power_export_{phase}_kw"] = Decimal((index + 1) * n)
        metrics.append(item)
    full, _ = add(started_at=full_start, times=times, metrics=metrics)

    gap_start = start + timedelta(days=1)
    gap_metrics = [
        {
            "voltage_l1_v": Decimal("221"),
            "current_l1_a": Decimal("1"),
            "power_import_l1_kw": Decimal("0.4"),
        },
        {
            "voltage_l1_v": Decimal("229"),
            "current_l1_a": Decimal("2"),
            "power_import_l1_kw": Decimal("0.8"),
        },
    ]
    gap, _ = add(
        started_at=gap_start,
        times=[gap_start, gap_start + timedelta(seconds=60)],
        metrics=gap_metrics,
    )
    return summary, single, full, gap


def test_phases_direct_url_direction_legend_numeric_raw_theme_language_and_refresh(
    shell, analysis_data
):
    page, origin, _ = shell
    login(page, origin)
    summary, single_session, session, gap_session = seed_phases(analysis_data)
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    page.goto(origin + f"/sites/{summary.site_id}?tab=phases")
    expect(page.get_by_role("heading", name="Phase diagnostics")).to_be_visible()
    expect(page.locator("canvas")).to_have_count(3)
    assert query(page)["session_id"] == str(session.session_id)
    phase_calls = [url for url in requests if "/analysis/phases?" in url]
    assert phase_calls and "direction=import" in phase_calls[-1]
    assert not any(
        any(
            path in url
            for path in ("/analysis/overview?", "/analysis/latest?", "/analysis/typical-day?")
        )
        for url in requests
    )

    page.get_by_text("Exact phase buckets and gap flags", exact=True).click()
    expect(page.locator(".analysis-panel details tbody tr")).to_have_count(18)
    expect(page.locator(".analysis-panel details")).to_contain_text("L3")
    expect(page.locator(".analysis-panel details")).to_contain_text("0")
    page.get_by_text("Exact phase buckets and gap flags", exact=True).click()

    # A shared phase checkbox removes that phase from every metric chart.
    before_canvases = page.locator("canvas").evaluate_all("nodes => nodes.map(e => e.toDataURL())")
    page.get_by_label("L2").uncheck()
    expect(page.locator("canvas")).to_have_count(3)
    page.wait_for_function(
        "old => [...document.querySelectorAll('canvas')].every((e, i) => e.toDataURL() !== old[i])",
        arg=before_canvases,
    )
    page.get_by_label("Power direction", exact=True).select_option("export")
    expect(page).to_have_url(re.compile(r"direction=export"))
    expect(page.locator("canvas")).to_have_count(3)
    export_calls = [
        url for url in requests if "/analysis/phases?" in url and "direction=export" in url
    ]
    assert export_calls

    # Bucket lookup carries the selected session and exact bucket bounds, without fabricated IDs.
    page.get_by_text("Exact phase buckets and gap flags", exact=True).click()
    link = page.get_by_role("link", name="Inspect bucket range").first
    href = link.get_attribute("href")
    args = parse_qs(urlparse(href).query)
    assert args["tab"] == ["raw"] and args["raw_session_id"] == [str(session.session_id)]
    assert "raw_received_from" in args and "raw_received_before" in args
    assert "raw_record_id" not in args and "raw_device_id" not in args

    old = page.locator("canvas").first.evaluate("e => e.toDataURL()")
    page.get_by_role("button", name="Switch to light mode").click()
    page.wait_for_function("old => document.querySelector('canvas').toDataURL() !== old", arg=old)
    page.get_by_label("Language").select_option("zh")
    expect(page.get_by_role("heading", name="相位诊断")).to_be_visible()
    expect(page.get_by_label("功率方向")).to_have_value("export")
    saved = page.url
    page.reload()
    expect(page.get_by_label("功率方向")).to_have_value("export")
    assert page.url == saved
    page.get_by_role("button", name="刷新分析").click()
    page.wait_for_function("document.querySelectorAll('canvas').length === 3")
    page.get_by_label("监测会话", exact=True).select_option(str(single_session.session_id))
    expect(page.locator(".analysis-chart")).to_have_count(3)
    # The one-sample voltage/current curves have no line segment. Their blue
    # mean markers must still render, including the exact zero current sample.
    page.wait_for_function("""() => {
      const blueMeanIsVisible = canvas => {
        const context = canvas.getContext('2d')
        const { data } = context.getImageData(0, 0, canvas.width, canvas.height)
        for (let i = 0; i < data.length; i += 4) {
          const blue = data[i + 3] > 0 && Math.abs(data[i] - 102) < 8
            && Math.abs(data[i + 1] - 149) < 8 && data[i + 2] > 235
          if (blue) return true
        }
        return false
      }
      return [...document.querySelectorAll('canvas')].slice(0, 2).every(blueMeanIsVisible)
    }""")
    page.get_by_text("精确相位桶及缺口标记", exact=True).click()
    expect(page.locator(".analysis-panel details")).to_contain_text("未提供")
    expect(page.locator(".analysis-panel details")).to_contain_text("0")
    page.get_by_label("监测会话", exact=True).select_option(str(gap_session.session_id))
    expect(page.locator(".analysis-chart")).to_have_count(3)
    # Two valid readings separated by null buckets must each retain a marker.
    # The selected range spans the session, so the endpoints lie in opposite
    # halves of the voltage chart's plot area.
    page.wait_for_function("""() => {
      const canvas = document.querySelector('canvas')
      const { data } = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height)
      const half = canvas.width / 2
      const found = [false, false]
      for (let i = 0; i < data.length; i += 4) {
        const pixel = i / 4
        const x = pixel % canvas.width
        const blue = data[i + 3] > 0 && Math.abs(data[i] - 102) < 8
          && Math.abs(data[i + 1] - 149) < 8 && data[i + 2] > 235
        if (blue) found[x < half ? 0 : 1] = true
      }
      return found.every(Boolean)
    }""")
    page.get_by_text("精确相位桶及缺口标记", exact=True).click()
    expect(page.locator(".analysis-panel details")).to_contain_text("221")
    expect(page.locator(".analysis-panel details")).to_contain_text("229")
    page.get_by_text("精确相位桶及缺口标记", exact=True).click()
    page.get_by_label("监测会话", exact=True).select_option(str(session.session_id))
    expect(page.get_by_text("L3", exact=True).first).to_be_visible()
    for width in (1024, 360):
        page.set_viewport_size({"width": width, "height": 900})
        expect(page.locator("canvas")).to_have_count(3)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.locator(".table-scroll").count() >= 3
    page.set_viewport_size({"width": 1440, "height": 900})
