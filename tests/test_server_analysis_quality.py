"""M5-05 quality calculations over synthetic projection rows."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import test_server_analysis
from sqlalchemy import update

from watt_profile.contracts.domain import FieldIssue, Quality, SanitizedField
from watt_profile.server.analysis import AnalysisRange, InvalidAnalysis
from watt_profile.server.registration import PageOptions
from watt_profile.server.storage import schema as db
from watt_profile.server.storage.repository import snapshot_values

analysis_data = test_server_analysis.analysis_data


def _range(start, seconds):
    return AnalysisRange(received_from=start, received_before=start + timedelta(seconds=seconds))


def test_empty_quality_is_entirely_unobserved(analysis_data):
    service, summary, _, add = analysis_data
    start = datetime(2026, 3, 1, tzinfo=UTC)
    session, _ = add()
    with service.read(summary.site_id, session.session_id, _range(start, 31)) as read:
        result = read.quality()
    assert (result.expected_count, result.actual_count, result.coverage_percent) == (
        4,
        0,
        Decimal("0"),
    )
    assert all(item.valid_count == item.missing_count == 0 for item in result.metrics)
    assert [(g.kind, g.received_from, g.received_before) for g in result.gaps] == [
        ("unobserved", start, start + timedelta(seconds=31))
    ]
    assert len(result.metrics) == 18
    with service.read(summary.site_id, session.session_id, _range(start, 31)) as read:
        beyond = read.quality(gaps=PageOptions(offset=10_000, limit=100))
    assert beyond.gaps == () and beyond.gap_total == 1
    with service.read(summary.site_id, session.session_id, _range(start, 31)) as read:
        with pytest.raises(InvalidAnalysis):
            read.quality(problems={"limit": 101})


def test_quality_ties_threshold_excess_redaction_and_problem_pages(analysis_data):
    service, summary, _, add = analysis_data
    start = datetime(2026, 3, 1, tzinfo=UTC)
    session, records = add(
        times=(
            start,
            start + timedelta(seconds=30),
            start + timedelta(seconds=30),
            start + timedelta(seconds=61),
        )
    )
    with service.engine.begin() as connection:
        revised = records[0].model_copy(
            update={
                "quality": Quality(
                    frame_length=1, issues=(FieldIssue(code="1-0:1.8.0", reason="missing"),)
                ),
                "fields": (
                    SanitizedField(
                        code="1-0:1.8.0",
                        raw_values=("<redacted>",),
                        value=None,
                        unit=None,
                        redacted=True,
                    ),
                ),
            }
        )
        connection.execute(
            update(db.snapshots)
            .where(db.snapshots.c.record_id == records[0].record_id)
            .values(**snapshot_values(revised, start))
        )
        revised = records[1].model_copy(
            update={
                "quality": Quality(
                    frame_length=1,
                    issues=(FieldIssue(code=None, reason="invalid_timestamp"),),
                )
            }
        )
        connection.execute(
            update(db.snapshots)
            .where(db.snapshots.c.record_id == records[1].record_id)
            .values(**snapshot_values(revised, start))
        )
        revised = records[2].model_copy(
            update={
                "quality": Quality(
                    frame_length=1, issues=(FieldIssue(code=None, reason="redacted"),)
                )
            }
        )
        connection.execute(
            update(db.snapshots)
            .where(db.snapshots.c.record_id == records[2].record_id)
            .values(**snapshot_values(revised, start))
        )
    with service.read(summary.site_id, session.session_id, _range(start, 92)) as read:
        result = read.quality(gaps=PageOptions(limit=1), problems=PageOptions(limit=1))
    assert result.expected_count == 10 and result.actual_count == 4
    assert result.coverage_percent == Decimal("40") and result.excess_count == 0
    assert result.redacted_field_count > 0 and result.problem_total == 2
    assert {(item.reason, item.count) for item in result.issues} == {
        ("missing", 1),
        ("invalid_timestamp", 1),
    }
    assert len(result.gaps) == 1 and result.gap_total == 2
    assert result.gaps[0].kind == "internal"
    assert result.problems[0].issues == ({"code": "1-0:1.8.0", "reason": "missing"},)
    with service.read(summary.site_id, session.session_id, _range(start, 92)) as read:
        next_page = read.quality(
            gaps=PageOptions(offset=1, limit=1), problems=PageOptions(offset=1, limit=1)
        )
    assert next_page.gaps[0].kind == "trailing"
    assert next_page.problems[0].issues == ({"code": None, "reason": "invalid_timestamp"},)


def test_quality_excess_and_single_phase_counters(analysis_data):
    service, summary, _, add = analysis_data
    start = datetime(2026, 3, 1, tzinfo=UTC)
    session, _ = add(times=(start, start + timedelta(seconds=1), start + timedelta(seconds=2)))
    with service.read(summary.site_id, session.session_id, _range(start, 10)) as read:
        result = read.quality()
    assert result.actual_count == 3 and result.expected_count == 1
    assert (
        result.displayed_count == 1
        and result.coverage_percent == Decimal("100")
        and result.excess_count == 2
    )
    metrics = {item.metric: item for item in result.metrics}
    assert metrics["voltage_l2_v"].missing_count == 3
    assert result.import_endpoint_valid_count == 0 and result.import_endpoint_missing_count == 3


def test_quality_shared_diagnostics_keep_totals_and_summaries_beyond_pages(analysis_data):
    service, summary, _, add = analysis_data
    start = datetime(2026, 3, 1, tzinfo=UTC)
    session, records = add(times=(start, start + timedelta(seconds=60)))
    issues = (
        FieldIssue(code=None, reason="redacted"),
        FieldIssue(code="1-0:1.8.0", reason="missing"),
        FieldIssue(code="1-0:2.8.0", reason="missing"),
    )
    revised = records[1].model_copy(update={"quality": Quality(frame_length=1, issues=issues)})
    with service.engine.begin() as connection:
        connection.execute(
            update(db.snapshots)
            .where(db.snapshots.c.record_id == revised.record_id)
            .values(**snapshot_values(revised, start))
        )
    with service.read(summary.site_id, session.session_id, _range(start, 100)) as read:
        first = read.quality(gaps=PageOptions(limit=1), problems=PageOptions(limit=1))
        beyond = read.quality(gaps=PageOptions(offset=100), problems=PageOptions(offset=100))
    assert first.gap_total == beyond.gap_total == 2
    assert first.problem_total == beyond.problem_total == 1
    assert beyond.gaps == beyond.problems == ()
    assert [(item.reason, item.count) for item in first.issues] == [("missing", 2)]
    assert beyond.issues == first.issues
    assert first.problems[0].record_id == revised.record_id
    assert first.problems[0].issues == tuple(issue.model_dump() for issue in issues[1:])
