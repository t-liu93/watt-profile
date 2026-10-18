"""Quality/raw traceability with native complete streaming downloads."""

# ruff: noqa: F811
import csv
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

import pytest
from playwright.sync_api import expect
from sqlalchemy import update
from test_server_analysis import analysis_data  # noqa: F401
from test_server_auth import server_settings  # noqa: F401
from test_server_overview import login, query
from test_server_shell import shell  # noqa: F401

from watt_profile.contracts.domain import FieldIssue, Quality
from watt_profile.server.storage import schema as db
from watt_profile.server.storage.repository import snapshot_values

pytestmark = pytest.mark.skipif(
    not any("tests/browser" in str(arg) for arg in __import__("sys").argv[1:]),
    reason="Select tests/browser explicitly for browser acceptance",
)


def test_quality_gap_problem_peak_and_independent_raw_complete_export(
    shell, analysis_data, tmp_path
):
    page, origin, _ = shell
    service, summary, _, add = analysis_data
    start = datetime(2026, 3, 1, tzinfo=UTC)
    exact = "0.123456789012345678901234567890"
    session, records = add(
        times=[start, start + timedelta(seconds=60)],
        metrics=[{"power_import_kw": Decimal(exact)}, {"power_import_kw": Decimal("4.000")}],
    )
    revised = records[0].model_copy(
        update={
            "quality": Quality(frame_length=1, issues=(FieldIssue(code=None, reason="missing"),))
        }
    )
    with service.engine.begin() as connection:
        connection.execute(
            update(db.snapshots)
            .where(db.snapshots.c.record_id == revised.record_id)
            .values(**snapshot_values(revised, start))
        )
    _, far = add(started_at=start + timedelta(days=40), times=[start + timedelta(days=40)])
    login(page, origin)
    selection = {
        "tab": "quality",
        "session_id": str(session.session_id),
        "received_from": "2026-03-01T00:00:00.000000Z",
        "received_before": "2026-03-01T00:01:00.000001Z",
        "range_mode": "custom",
    }
    url = origin + f"/sites/{summary.site_id}?" + urlencode(selection)
    page.goto(url)
    expect(page.locator("#metric-validity tbody tr")).to_have_count(18)
    expect(page.locator("#quality-gaps")).to_contain_text("Internal sampling gap")
    page.get_by_role("link", name="Inspect readings around gap").click()
    expect(page.get_by_role("button", name="View reading")).to_have_count(2)
    assert query(page)["raw_received_from"] == "2026-03-01T00:00:00.000000Z"
    assert query(page)["raw_received_before"] == "2026-03-01T00:01:00.000001Z"
    page.goto(url)
    page.locator("#quality-problems").get_by_role("link", name="Inspect raw record").click()
    expect(page.locator("#detail-dialog")).to_be_visible()
    expect(page.locator("#fields-quality")).to_contain_text(exact)
    expect(page.locator("#metrics dt")).to_have_count(18)
    page.keyboard.press("Escape")
    expect(page.locator("#detail-dialog")).to_be_hidden()
    page.goto(url.replace("tab=quality", "tab=overview"))
    page.get_by_role("link", name="Inspect raw record").first.click()
    expect(page.locator("#fields-quality")).to_contain_text(str(records[1].record_id))
    page.keyboard.press("Escape")
    page.goto(url.replace("tab=quality", "tab=phases"))
    page.get_by_text("Exact phase buckets and gap flags", exact=True).click()
    page.get_by_role("link", name="Inspect bucket range").first.click()
    expect(page.get_by_role("button", name="View reading")).to_have_count(1)
    assert query(page)["raw_session_id"] == str(session.session_id)
    assert "raw_record_id" not in query(page)
    # Normal raw entry never adopts the analysis range/session, including >31 dates.
    page.goto(
        origin
        + f"/sites/{summary.site_id}?tab=raw&"
        + urlencode(
            {
                "session_id": str(session.session_id),
                "received_from": selection["received_from"],
                "received_before": selection["received_before"],
            }
        )
    )
    expect(page.get_by_role("button", name="View reading")).to_have_count(3)
    page.get_by_label("Received from (UTC, inclusive)").fill("2026-03-01T00:00:00.000000Z")
    page.get_by_label("Received before (UTC, exclusive)").fill("2026-04-10T00:00:00.000001Z")
    page.get_by_label("Rows per page").fill("1")
    page.get_by_role("button", name="Apply filters").click()
    expect(page.get_by_role("button", name="View reading")).to_have_count(1)
    assert query(page)["received_before"] == selection["received_before"]
    with page.expect_download() as downloaded:
        page.get_by_role("button", name="Export all filtered readings (CSV)").click()
    path = tmp_path / "all.csv"
    downloaded.value.save_as(path)
    rows = list(csv.DictReader(io.StringIO(path.read_text())))
    assert len(rows) == 3
    assert {row["record_id"] for row in rows} == {str(r.record_id) for r in [*records, *far]}
    assert rows[0]["power_import_kw"] == exact


def test_quality_independent_pages_refresh_failure_and_late_detail(shell, analysis_data):
    page, origin, _ = shell
    service, summary, _, add = analysis_data
    start = datetime(2026, 3, 1, tzinfo=UTC)
    session, records = add(times=[start + timedelta(seconds=60 * index) for index in range(52)])
    with service.engine.begin() as connection:
        for record in records:
            revised = record.model_copy(
                update={
                    "quality": Quality(
                        frame_length=1, issues=(FieldIssue(code=None, reason="missing"),)
                    )
                }
            )
            connection.execute(
                update(db.snapshots)
                .where(db.snapshots.c.record_id == revised.record_id)
                .values(**snapshot_values(revised, start))
            )
    login(page, origin)
    page.goto(origin + f"/sites/{summary.site_id}?tab=quality")
    expect(page.locator("#quality-gaps tr")).to_have_count(50)
    expect(page.locator("#quality-problems tr")).to_have_count(50)
    gaps = page.get_by_role("heading", name="Gaps", exact=True).locator("..")
    problems = page.get_by_role("heading", name="Persisted problem records").locator("..")
    gaps.get_by_role("button", name="Next page", exact=True).click()
    expect(page.locator("#quality-gaps tr")).to_have_count(1)
    expect(page.locator("#quality-problems tr")).to_have_count(50)
    assert query(page)["gap_offset"] == "50" and "problem_offset" not in query(page)
    problems.get_by_role("button", name="Next page", exact=True).click()
    expect(page.locator("#quality-problems tr")).to_have_count(2)
    page.route(
        "**/analysis/quality?*",
        lambda route: route.fulfill(
            status=503, json={"error": {"code": "temporarily_unavailable"}}
        ),
    )
    page.get_by_role("button", name="Refresh analysis").click()
    expect(page.get_by_role("alert")).to_contain_text("previous successful result")
    expect(page.locator("#quality-problems tr")).to_have_count(2)
    page.unroute("**/analysis/quality?*")
    # A delayed response cannot reopen the dialog after closing or navigating.
    held = []
    page.goto(origin + f"/sites/{summary.site_id}?tab=raw")
    expect(page.get_by_role("button", name="View reading")).to_have_count(50)
    page.route("**/readings/*/*", lambda route: held.append((route, route.fetch())))
    page.get_by_role("button", name="View reading").first.click()
    expect(page.locator("#detail-dialog")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_role("button", name="View reading").first).to_be_focused()
    assert held
    route, response = held.pop()
    route.fulfill(response=response)
    page.wait_for_timeout(100)
    expect(page.locator("#detail-dialog")).to_be_hidden()
    page.unroute("**/readings/*/*")
    page.set_viewport_size({"width": 360, "height": 780})
    page.get_by_role("button", name="View reading").first.click()
    expect(page.locator("#metrics dt")).to_have_count(18)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.keyboard.press("Escape")
    page.get_by_label("Language").select_option("zh")
    expect(page.get_by_role("heading", name="原始读数")).to_be_visible()
