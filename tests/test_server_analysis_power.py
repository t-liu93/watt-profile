"""Projection-only power envelopes, zoom, and traceable sampled peaks."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID

import pytest
import test_server_analysis

from watt_profile.server.analysis import AnalysisRange, InvalidAnalysis
from watt_profile.server.analysis_power import aggregate_curves, select_resolution

power_data = test_server_analysis.analysis_data

BASE = datetime(2026, 3, 10, 12, tzinfo=UTC)


def interval(seconds, start=BASE):
    return AnalysisRange(received_from=start, received_before=start + timedelta(seconds=seconds))


def powers(value, export="0"):
    return {
        "power_import_kw": None if value is None else Decimal(value),
        "power_export_kw": None if export is None else Decimal(export),
    }


def test_resolution_caps_and_zoom():
    for seconds in (0.000001, 10, 20000, 20001, 86400, 30 * 86400):
        selected = interval(seconds)
        step = select_resolution(selected)
        assert (seconds + step - 0.000001) // step <= 2000
    assert select_resolution(interval(86400)) > select_resolution(interval(60)) == 10
    assert select_resolution(interval(86400), max_buckets=20) >= 4320
    for invalid in (0, 2001, True):
        with pytest.raises(InvalidAnalysis):
            select_resolution(interval(1), max_buckets=invalid)


def test_bucket_boundaries_missing_zero_and_peak(power_data):
    service, summary, _, add = power_data
    descriptor, records = add(
        times=[BASE + timedelta(seconds=n) for n in (0, 1, 10, 20, 30)],
        metrics=[powers("0"), powers("100"), powers(None), powers("0"), powers("999")],
    )
    with service.read(summary.site_id, descriptor.session_id, interval(30)) as read:
        result = read.power()
    buckets = result.import_power.buckets
    assert len(buckets) == 3
    assert [(b.mean, b.minimum, b.maximum, b.valid_count) for b in buckets] == [
        (Decimal(50), Decimal(0), Decimal(100), 2),
        (None, None, None, 0),
        (Decimal(0), Decimal(0), Decimal(0), 1),
    ]
    assert buckets[2].break_before and not any(b.has_gap for b in buckets)
    assert result.import_sampled_peak.value_kw == 100
    assert result.import_sampled_peak.record_id == records[1].record_id
    assert result.import_sampled_peak.device_id == summary.device_id
    assert result.export_sampled_peak.value_kw == 0
    assert result.export_sampled_peak.record_id == records[0].record_id
    assert result.model_dump(mode="json")["import_sampled_peak"]["value_kw"] == "100"


def test_finer_zoom_preserves_global_context_and_peak(power_data):
    service, summary, _, add = power_data
    descriptor, _ = add(
        times=[BASE, BASE + timedelta(hours=1)], metrics=[powers("100"), powers("2")]
    )
    with service.read(summary.site_id, descriptor.session_id, interval(86400)) as read:
        overview = read.power()
        zoom = read.power(zoom=interval(20, BASE + timedelta(hours=1)))
        assert zoom.context == overview.context == read.context
        assert zoom.import_sampled_peak == overview.import_sampled_peak
        assert zoom.resolution_seconds < overview.resolution_seconds
        assert zoom.import_power.buckets[0].mean == 2
        for invalid in (interval(20, BASE - timedelta(seconds=1)), interval(86401), {}):
            with pytest.raises(InvalidAnalysis):
                read.power(zoom=invalid)
        _, _, limited = aggregate_curves(read, ("voltage_l1_v",), max_buckets=20)
        assert len(limited[0].buckets) <= 20


def test_sampled_peak_ties_use_time_then_record_order(power_data):
    service, summary, _, add = power_data
    low = UUID("00000000-0000-4000-8000-000000000001")
    high = UUID("ffffffff-ffff-4fff-bfff-ffffffffffff")
    descriptor, _ = add(
        times=[BASE, BASE, BASE + timedelta(seconds=1)],
        record_ids=[high, low, UUID("00000000-0000-4000-8000-000000000002")],
        metrics=[powers("7")] * 3,
    )
    with service.read(summary.site_id, descriptor.session_id, interval(10)) as read:
        peak = read.power().import_sampled_peak
    assert peak.received_at == BASE and peak.record_id == low


def test_observed_gaps_inside_filled_buckets_and_edges(power_data):
    service, summary, _, add = power_data
    descriptor, _ = add(
        times=[BASE + timedelta(seconds=n) for n in (0, 40, 50, 80)], metrics=[powers("1")] * 4
    )
    with service.read(summary.site_id, descriptor.session_id, interval(86400)) as read:
        result = read.power()
    # A 60-second bucket can contain both a real gap and valid readings.
    first = result.import_power.buckets[0]
    assert first.valid_count == 3 and first.has_gap
    assert result.import_power.buckets[1].break_before
    assert result.import_power.buckets[-1].has_gap
    # Exactly 30 seconds is not a gap.
    with service.read(
        summary.site_id, descriptor.session_id, interval(40, BASE + timedelta(seconds=50))
    ) as read:
        assert not any(b.has_gap for b in read.power().import_power.buckets)


def test_dense_missing_metrics_are_not_observed_gaps(power_data):
    service, summary, _, add = power_data
    descriptor, _ = add(
        times=[BASE + timedelta(seconds=n) for n in range(0, 61, 10)],
        metrics=[powers("0")] + [powers(None)] * 5 + [powers("1")],
    )
    with service.read(summary.site_id, descriptor.session_id, interval(70)) as read:
        result = read.power()
    assert not any(b.has_gap for b in result.import_power.buckets)
    assert result.import_power.buckets[-1].break_before


def test_no_readings_and_missing_metrics(power_data):
    service, summary, _, add = power_data
    descriptor, _ = add()
    with service.read(summary.site_id, descriptor.session_id, interval(20000)) as read:
        result = read.power()
    assert len(result.import_power.buckets) == 2000
    assert result.import_sampled_peak is result.export_sampled_peak is None
    assert all(
        b.mean is None and b.valid_count == 0 and b.has_gap for b in result.import_power.buckets
    )
    descriptor, _ = add(times=[BASE], metrics=[powers(None, None)])
    with service.read(summary.site_id, descriptor.session_id, interval(10)) as read:
        result = read.power()
    assert result.import_sampled_peak is result.export_sampled_peak is None
    assert result.import_power.buckets[0].mean is None
    assert not result.import_power.buckets[0].has_gap


def test_large_exact_power_mean_ignores_python_context(power_data):
    service, summary, _, add = power_data
    first = "1234567890123456789012345678901234567890.00000000002"
    second = "1234567890123456789012345678901234567890.00000000004"
    descriptor, _ = add(
        times=[BASE, BASE + timedelta(seconds=1)], metrics=[powers(first), powers(second)]
    )
    with localcontext() as context:
        context.prec = 6
        with service.read(summary.site_id, descriptor.session_id, interval(10)) as read:
            result = read.power()
    assert result.import_power.buckets[0].mean == Decimal(
        "1234567890123456789012345678901234567890.00000000003"
    )
    assert result.import_sampled_peak.value_kw == Decimal(second)


def test_gap_exclusive_end_breaks_without_false_inside_flag(power_data):
    service, summary, _, add = power_data
    descriptor, _ = add(
        times=[BASE, BASE + timedelta(seconds=60)], metrics=[powers("1"), powers("2")]
    )
    with service.read(summary.site_id, descriptor.session_id, interval(86400)) as read:
        result = read.power()
    assert result.resolution_seconds == 60
    assert result.import_power.buckets[0].has_gap
    # Gap ends at 60s; trailing edge is deliberately excluded by this zoom.
    with service.read(summary.site_id, descriptor.session_id, interval(86400)) as read:
        _, _, curves = aggregate_curves(
            read, ("power_import_kw",), zoom=interval(61), max_buckets=2
        )
    assert curves[0].buckets[1].received_from == BASE + timedelta(seconds=60)
    assert not curves[0].buckets[1].has_gap
    assert curves[0].buckets[1].break_before


def test_short_zoom_retains_gap_crossing_its_edge(power_data):
    service, summary, _, add = power_data
    descriptor, _ = add(
        times=[BASE, BASE + timedelta(seconds=55)], metrics=[powers("1"), powers("2")]
    )
    with service.read(summary.site_id, descriptor.session_id, interval(60)) as read:
        result = read.power(zoom=interval(10, BASE + timedelta(seconds=50)))
    assert result.import_power.buckets[0].valid_count == 1
    assert result.import_power.buckets[0].has_gap
