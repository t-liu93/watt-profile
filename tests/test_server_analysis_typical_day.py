"""M5-06 average-day SQL behavior over synthetic isolated PostgreSQL data."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import test_server_analysis

from watt_profile.server.analysis import AnalysisRange

analysis_data = test_server_analysis.analysis_data


def _profile(analysis_data, session_id, start, end):
    service, summary, _, _ = analysis_data
    with service.read(
        summary.site_id,
        session_id,
        AnalysisRange(received_from=start, received_before=end),
    ) as read:
        return read.typical_day()


def _slot_value(result, group, local_time, metric):
    selected = next(value for value in result.groups if value.group == group)
    return next(
        value
        for value in selected.slots
        if value.local_time.isoformat() == local_time and value.metric == metric
    )


def test_equal_date_weights_ignore_daily_sample_count_and_groups(analysis_data):
    _, _, _, add = analysis_data
    first = datetime(2026, 3, 2, 23, tzinfo=UTC)
    second = first + timedelta(days=1)
    times = tuple(first + timedelta(seconds=10 * n) for n in range(72)) + tuple(
        second + timedelta(seconds=10 * n) for n in range(90)
    )
    values = [Decimal("0")] * 72 + [Decimal("10")] * 90
    _, readings = add(times=times, metrics=[{"power_import_kw": v} for v in values])
    result = _profile(analysis_data, readings[0].session_id, first, second + timedelta(minutes=15))
    slot = _slot_value(result, "all", "00:00:00", "power_import_kw")
    assert slot.mean == Decimal("5")
    assert slot.contributing_date_count == 2
    assert _slot_value(result, "weekday", "00:00:00", "power_import_kw").mean == 5
    assert (
        _slot_value(result, "weekend", "00:00:00", "power_import_kw").contributing_date_count == 0
    )


def test_eighty_percent_boundary_is_independent_per_metric_and_partial_dates(analysis_data):
    _, _, _, add = analysis_data
    start = datetime(2026, 3, 2, 23, tzinfo=UTC)
    times = tuple(start + timedelta(seconds=10 * n) for n in range(72))
    metrics = [
        {"power_import_kw": Decimal("0"), "power_export_kw": Decimal("0")} for _ in range(71)
    ] + [{"power_import_kw": Decimal("0"), "power_export_kw": None}]
    _, records = add(times=times, metrics=metrics)
    session_id = records[0].session_id
    result = _profile(analysis_data, session_id, start, start + timedelta(minutes=15))
    imported = _slot_value(result, "all", "00:00:00", "power_import_kw")
    exported = _slot_value(result, "all", "00:00:00", "power_export_kw")
    assert imported.mean == 0 and imported.contributing_date_count == 1
    assert exported.mean is None and exported.contributing_date_count == 0
    partial = _profile(analysis_data, session_id, start, start + timedelta(minutes=5))
    assert _slot_value(partial, "all", "00:00:00", "power_import_kw").mean is None


def test_empty_groups_and_real_zero_are_distinct_from_absence(analysis_data):
    _, _, _, add = analysis_data
    start = datetime(2026, 3, 2, 23, tzinfo=UTC)
    times = tuple(start + timedelta(seconds=10 * n) for n in range(72))
    _, records = add(times=times, metrics=[{"power_import_kw": Decimal("0")} for _ in times])
    result = _profile(analysis_data, records[0].session_id, start, start + timedelta(minutes=15))
    assert _slot_value(result, "all", "00:00:00", "power_import_kw").mean == 0
    empty = _slot_value(result, "weekend", "00:00:00", "power_import_kw")
    assert empty.mean is None and empty.contributing_date_count == 0


def test_autumn_repeated_slot_uses_full_1800_second_threshold(analysis_data):
    _, _, _, add = analysis_data
    start = datetime(2026, 10, 25, 0, tzinfo=UTC)
    first_fold = tuple(start + timedelta(seconds=10 * n) for n in range(72))
    second_fold = tuple(start + timedelta(hours=1, seconds=10 * n) for n in range(72))
    times = first_fold + second_fold
    range_end = start + timedelta(hours=2)
    _, records = add(
        times=times,
        metrics=[{"power_import_kw": Decimal("7")} for _ in range(143)]
        + [{"power_import_kw": None}],
    )
    result = _profile(analysis_data, records[0].session_id, start, range_end)
    slot = _slot_value(result, "all", "02:00:00", "power_import_kw")
    assert slot.mean is None and slot.contributing_date_count == 0
    _, records = add(
        times=times,
        metrics=[{"power_import_kw": Decimal("7")} for _ in times],
    )
    result = _profile(analysis_data, records[0].session_id, start, range_end)
    slot = _slot_value(result, "all", "02:00:00", "power_import_kw")
    assert slot.mean == 7 and slot.contributing_date_count == 1


def test_spring_missing_slot_is_not_created_and_decimal_precision_survives(analysis_data):
    _, _, _, add = analysis_data
    start = datetime(2026, 3, 28, 23, tzinfo=UTC)
    precise = Decimal("12345678901234567890.123456789")
    times = tuple(start + timedelta(seconds=10 * n) for n in range(72))
    _, records = add(times=times, metrics=[{"power_import_kw": precise} for _ in times])
    result = _profile(
        analysis_data,
        records[0].session_id,
        start,
        datetime(2026, 3, 29, 22, tzinfo=UTC),
    )
    missing = _slot_value(result, "all", "02:00:00", "power_import_kw")
    assert missing.mean is None and missing.contributing_date_count == 0
    assert _slot_value(result, "all", "00:00:00", "power_import_kw").mean == precise
