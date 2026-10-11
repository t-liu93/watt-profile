"""Pure sampling checks using mapped synthetic readings and explicit clocks."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from watt_profile.contracts.domain import SessionDescriptor, SourceDescriptor, measurement_point_id
from watt_profile.logger.sampler import WindowSampler
from watt_profile.p1.parser import SanitizedField
from watt_profile.p1.profile import METRIC_CODES, MappedReading

START = datetime(2026, 1, 1, tzinfo=UTC)


def descriptors():
    site_id, device_id, session_id, source_id = (uuid4() for _ in range(4))
    session = SessionDescriptor(
        session_id=session_id, device_id=device_id, site_id=site_id, started_at=START
    )
    source = SourceDescriptor(
        source_id=source_id,
        session_id=session_id,
        measurement_point_id=measurement_point_id(site_id),
    )
    return session, source


def reading(received_at: datetime, marker: str) -> MappedReading:
    metrics = {name: None for name in METRIC_CODES}
    metrics["power_import_kw"] = Decimal(marker)
    return MappedReading(
        profile_id="dsmr5_nl",
        profile_revision=1,
        frame_length=100,
        meter_time_raw="260101000000W",
        meter_time_utc=START,
        received_at=received_at,
        metrics=metrics,
        fields=(
            SanitizedField(
                code="1-0:1.7.0",
                raw_values=[marker + "*kW"],
                value=Decimal(marker),
                unit="kW",
                redacted=False,
            ),
        ),
        issues=(),
    )


def test_latest_received_reading_wins_regardless_of_meter_timestamp():
    session, source = descriptors()
    sampler = WindowSampler(session, source, 100.0)
    sampler.offer(reading(START, "1.00"), 101.0)
    sampler.offer(reading(START - timedelta(days=1), "2.00"), 109.999)

    [snapshot] = sampler.advance(110.0, START + timedelta(seconds=10))
    assert snapshot.metrics.power_import_kw == Decimal("2.00")
    assert snapshot.sampled_at == START + timedelta(seconds=10)
    assert snapshot.record_id.version == 4


def test_exact_boundary_finalizes_before_new_reading_and_empty_windows_are_gaps():
    session, source = descriptors()
    sampler = WindowSampler(session, source, 0.0)
    sampler.offer(reading(START, "1"), 1.0)

    [first] = sampler.offer(reading(START + timedelta(seconds=10), "2"), 10.0)
    assert first.metrics.power_import_kw == Decimal("1")
    assert first.sampled_at == START + timedelta(seconds=10)
    [second] = sampler.advance(20.0, START + timedelta(seconds=20))
    assert second.metrics.power_import_kw == Decimal("2")
    assert second.sampled_at == START + timedelta(seconds=20)
    assert sampler.advance(30.0, START + timedelta(seconds=30)) == ()
    assert first.record_id != second.record_id


def test_skipped_windows_emit_one_candidate_without_catchup_rows():
    session, source = descriptors()
    sampler = WindowSampler(session, source, 50.0)
    sampler.offer(reading(START, "1"), 51.0)

    [snapshot] = sampler.advance(85.0, START + timedelta(seconds=35))
    assert snapshot.metrics.power_import_kw == Decimal("1")
    assert sampler.advance(90.0, START + timedelta(seconds=40)) == ()


def test_wall_clock_jump_does_not_change_monotonic_window_selection():
    session, source = descriptors()
    sampler = WindowSampler(session, source, 0.0)
    sampler.offer(reading(START, "1"), 2.0)
    sampler.offer(reading(START - timedelta(hours=1), "2"), 9.0)

    [snapshot] = sampler.advance(10.0, START - timedelta(days=3))
    assert snapshot.metrics.power_import_kw == Decimal("2")
    assert snapshot.sampled_at == START - timedelta(days=3)


def test_restart_uses_fresh_anchor_and_does_not_recover_old_candidate():
    session, source = descriptors()
    old_run = WindowSampler(session, source, 0.0)
    old_run.offer(reading(START, "1"), 1.0)

    restarted = WindowSampler(session, source, 500.0)
    assert restarted.advance(510.0, START + timedelta(seconds=10)) == ()
