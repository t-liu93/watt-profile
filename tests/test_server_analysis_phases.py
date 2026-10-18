"""Phase aggregation over synthetic isolated PostgreSQL projections."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import test_server_analysis

from watt_profile.server.analysis import AnalysisRange, InvalidAnalysis

phases_data = test_server_analysis.analysis_data
BASE = datetime(2026, 3, 10, 12, tzinfo=UTC)


def interval(seconds):
    return AnalysisRange(received_from=BASE, received_before=BASE + timedelta(seconds=seconds))


def metric_values(
    *,
    voltage=("230", "231", "232"),
    current=("1", "2", "3"),
    imported=("1", "2", "3"),
    exported=("4", "5", "6"),
):
    values = {}
    for index, phase in enumerate(("l1", "l2", "l3")):
        values[f"voltage_{phase}_v"] = (
            None
            if voltage is None
            else (None if voltage[index] is None else Decimal(voltage[index]))
        )
        values[f"current_{phase}_a"] = (
            None
            if current is None
            else (None if current[index] is None else Decimal(current[index]))
        )
        values[f"power_import_{phase}_kw"] = (
            None
            if imported is None
            else (None if imported[index] is None else Decimal(imported[index]))
        )
        values[f"power_export_{phase}_kw"] = (
            None
            if exported is None
            else (None if exported[index] is None else Decimal(exported[index]))
        )
    return values


def test_three_phase_curves_are_aligned_and_directional(phases_data):
    service, summary, _, add = phases_data
    descriptor, _ = add(
        times=[BASE, BASE + timedelta(seconds=10)],
        metrics=[
            metric_values(),
            metric_values(imported=("0", "8", "9"), exported=("7", "0", "2")),
        ],
    )
    with service.read(summary.site_id, descriptor.session_id, interval(20)) as read:
        imported = read.phases()
        exported = read.phases(direction="export")
    assert imported.direction == "import" and exported.direction == "export"
    assert (
        len({len(c.buckets) for c in (*imported.voltage, *imported.current, *imported.power)}) == 1
    )
    assert [curve.metric for curve in imported.voltage] == [
        "voltage_l1_v",
        "voltage_l2_v",
        "voltage_l3_v",
    ]
    assert [curve.buckets[0].mean for curve in imported.power] == [
        Decimal("1"),
        Decimal("2"),
        Decimal("3"),
    ]
    assert [curve.buckets[1].mean for curve in imported.power] == [
        Decimal("0"),
        Decimal("8"),
        Decimal("9"),
    ]
    assert [curve.buckets[0].mean for curve in exported.power] == [
        Decimal("4"),
        Decimal("5"),
        Decimal("6"),
    ]
    assert [curve.buckets[1].mean for curve in exported.power] == [
        Decimal("7"),
        Decimal("0"),
        Decimal("2"),
    ]
    assert imported.resolution_seconds == exported.resolution_seconds == 10
    assert all(
        len({b.received_from for b in c.buckets}) == len(c.buckets)
        for c in (*imported.voltage, *imported.current, *imported.power)
    )


def test_single_phase_and_partial_missing_preserve_null_and_zero(phases_data):
    service, summary, _, add = phases_data
    descriptor, _ = add(
        times=[BASE],
        metrics=[
            metric_values(
                voltage=("230", None, None),
                current=("0", None, None),
                imported=("0", None, None),
                exported=(None, None, None),
            )
        ],
    )
    with service.read(summary.site_id, descriptor.session_id, interval(10)) as read:
        result = read.phases()
    assert result.voltage[0].buckets[0].mean == 230
    assert result.voltage[1].buckets[0].mean is None
    assert result.current[0].buckets[0].mean == 0
    assert result.current[1].buckets[0].valid_count == 0
    assert result.power[0].buckets[0].mean == 0
    assert result.power[1].buckets[0].mean is None
    assert result.power[0].buckets[0].received_from == result.voltage[2].buckets[0].received_from


def test_all_phase_series_reuse_gap_flags_and_stay_bounded(phases_data):
    service, summary, _, add = phases_data
    descriptor, _ = add(
        times=[BASE, BASE + timedelta(seconds=40)], metrics=[metric_values(), metric_values()]
    )
    with service.read(summary.site_id, descriptor.session_id, interval(86400)) as read:
        result = read.phases()
    assert len(result.voltage[0].buckets) <= 500
    assert result.resolution_seconds == 300
    assert result.voltage[0].buckets[0].has_gap
    assert result.power[2].buckets[0].has_gap
    assert [b.received_from for b in result.voltage[0].buckets] == [
        b.received_from for b in result.power[2].buckets
    ]
    assert [b.break_before for b in result.current[0].buckets] == [
        b.break_before for b in result.power[1].buckets
    ]


def test_invalid_power_direction_is_rejected(phases_data):
    service, summary, _, add = phases_data
    descriptor, _ = add()
    with service.read(summary.site_id, descriptor.session_id, interval(10)) as read:
        with pytest.raises(InvalidAnalysis):
            read.phases(direction="consumption")
