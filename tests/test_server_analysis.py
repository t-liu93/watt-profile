"""M5-02 context and ranges, using synthetic isolated PostgreSQL data only."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, text
from test_server_ingest import batch

from watt_profile.server.analysis import (
    AnalysisRange,
    AnalysisService,
    InvalidAnalysis,
    SessionContext,
    initial_range,
    parse_range,
)
from watt_profile.server.queries import QueryUnavailable
from watt_profile.server.registration import ObjectNotFound, PageOptions, RegistrationSummary
from watt_profile.server.storage import schema as db
from watt_profile.server.storage.registration import RegistrationRepository
from watt_profile.server.storage.repository import ServerRepository, snapshot_values


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"received_from": "2026-01-01T00:00:00", "received_before": "2026-01-02T00:00:00Z"},
        {"received_from": "2026-01-02T00:00:00Z", "received_before": "2026-01-01T00:00:00Z"},
        {"received_from": "2026-01-01T00:00:00Z", "received_before": "2026-01-01T00:00:00Z"},
        {"received_from": "2026-01-01T00:00:00Z", "received_before": None},
        {
            "received_from": "2026-01-01T00:00:00Z",
            "received_before": "2026-01-02T00:00:00Z",
            "x": 1,
        },
        None,
    ],
)
def test_invalid_explicit_ranges(value):
    with pytest.raises(InvalidAnalysis, match="^invalid_analysis_range$") as failure:
        parse_range(value)
    assert failure.value.status_code == 422


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_complete_local_day_dst(day, hours):
    value = AnalysisRange.local_days(day, day)
    assert value.received_before - value.received_from == timedelta(hours=hours)
    assert value.received_from.tzinfo == UTC
    assert value.received_before.tzinfo == UTC


def test_31_dates_uses_exclusive_amsterdam_end():
    value = AnalysisRange.local_days(date(2026, 3, 1), date(2026, 3, 31))
    assert value.received_from == datetime(2026, 2, 28, 23, tzinfo=UTC)
    assert value.received_before == datetime(2026, 3, 31, 22, tzinfo=UTC)
    with pytest.raises(InvalidAnalysis):
        parse_range(
            value.model_dump()
            | {"received_before": value.received_before + timedelta(microseconds=1)}
        )
    with pytest.raises(InvalidAnalysis):
        AnalysisRange.local_days(date(2026, 3, 1), date(2026, 4, 1))
    with pytest.raises(InvalidAnalysis):
        AnalysisRange.local_days(date(2026, 3, 2), date(2026, 3, 1))
    # A 31*24 hour period can still intersect 32 local calendar dates.
    with pytest.raises(InvalidAnalysis):
        parse_range(
            {"received_from": "2026-01-01T12:00:00Z", "received_before": "2026-02-01T12:00:00Z"}
        )


@pytest.fixture
def analysis_data(server_engine):
    summary = RegistrationSummary(
        device_id=uuid4(), site_id=uuid4(), address="Synthetic", description="", token="a" * 43
    )
    registration = RegistrationRepository(server_engine)
    registration.commit(summary)
    repository = ServerRepository(server_engine)
    start = datetime(2026, 3, 1, tzinfo=UTC)

    def add(
        *,
        session_id=None,
        started_at=start,
        times=(),
        ingested_at=None,
        metrics=None,
        record_ids=None,
    ):
        value = batch(
            summary, session_id=session_id, record_ids=record_ids or [uuid4() for _ in times]
        )
        descriptor = value.session.model_copy(update={"started_at": started_at})
        records = [
            record.model_copy(update={"received_at": received, "sampled_at": received})
            for record, received in zip(value.records, times, strict=False)
        ]
        if metrics is not None:
            records = [
                record.model_copy(update={"metrics": record.metrics.model_copy(update=values)})
                for record, values in zip(records, metrics, strict=True)
            ]
        with repository.transaction() as connection:
            repository.insert_descriptors(connection, descriptor, value.source)
            if records:
                connection.execute(
                    insert(db.snapshots),
                    [snapshot_values(record, ingested_at or start) for record in records],
                )
        return descriptor, records

    return AnalysisService(server_engine), summary, registration, add


def test_empty_sites_and_empty_sessions(analysis_data):
    service, summary, _, add = analysis_data
    page = service.session_page(summary.site_id)
    assert page.state == "empty" and page.total == 0 and page.items == ()
    assert page.default_session_id is None and page.initial_range is None
    descriptor, _ = add()
    page = service.session_page(summary.site_id)
    assert page.state == "empty" and page.total == 1
    assert page.default_session_id is None and page.initial_range is None
    assert page.items[0].session == descriptor and page.items[0].reading_count == 0
    assert initial_range(page.items[0]) is None
    with pytest.raises(ObjectNotFound):
        service.session_page(uuid4())


def test_selection_backfill_ties_pagination_and_default_range(analysis_data):
    service, summary, _, add = analysis_data
    latest = datetime(2026, 3, 30, 12, tzinfo=UTC)
    old, _ = add(times=(latest - timedelta(days=10),), ingested_at=latest + timedelta(days=5))
    low_id = UUID("00000000-0000-4000-8000-000000000001")
    high_id = UUID("ffffffff-ffff-4fff-bfff-ffffffffffff")
    low, _ = add(session_id=low_id, times=(latest,))
    high, _ = add(session_id=high_id, times=(latest - timedelta(days=2), latest))
    later_start, _ = add(started_at=latest - timedelta(days=1), times=(latest,))
    empty, _ = add(started_at=latest + timedelta(days=1))
    page = service.session_page(summary.site_id, PageOptions(limit=2))
    assert page.state == "ready" and page.total == 5
    assert [item.session for item in page.items] == [later_start, high]
    assert page.default_session_id == later_start.session_id
    assert page.initial_range.received_from == later_start.started_at  # clipped
    assert page.initial_range.received_before == latest + timedelta(microseconds=1)
    second = service.session_page(summary.site_id, PageOptions(offset=2, limit=2))
    assert [item.session for item in second.items] == [low, old]
    assert second.default_session_id == later_start.session_id
    assert service.session_page(summary.site_id, PageOptions(offset=4)).items[0].session == empty
    beyond = service.session_page(summary.site_id, PageOptions(offset=100))
    assert beyond.items == () and beyond.default_session_id == later_start.session_id
    with pytest.raises(InvalidAnalysis):
        service.session_page(summary.site_id, PageOptions.model_construct(limit=101, offset=0))
    context = SessionContext(
        session=high,
        reading_count=2,
        first_reading_received_at=latest - timedelta(days=2),
        latest_reading_received_at=latest,
    )
    # Midnight 24 March is CET; latest is after the spring change.
    assert initial_range(context).received_from == datetime(2026, 3, 23, 23, tzinfo=UTC)


def test_explicit_context_edges_site_ownership_and_empty_range(analysis_data):
    service, summary, registration, add = analysis_data
    base = datetime(2026, 3, 10, tzinfo=UTC)
    descriptor, records = add(times=(base, base + timedelta(seconds=10)))
    value = {
        "received_from": "2026-03-10T01:00:00+01:00",
        "received_before": "2026-03-10T00:00:10Z",
    }
    with service.read(summary.site_id, descriptor.session_id, value) as read:
        assert read.context.reading_count == 1
        assert read.context.first_reading_received_at == records[0].received_at
        assert read.context.latest_reading_received_at == records[0].received_at
        assert read.context.range.received_from == base
        assert read.context.timezone == "Europe/Amsterdam"
        assert read.context.calculated_at.tzinfo is not None
    other_site = uuid4()
    registration.commit(summary.model_copy(update={"site_id": other_site}))
    for site_id, session_id in ((other_site, descriptor.session_id), (summary.site_id, uuid4())):
        with pytest.raises(ObjectNotFound):
            with service.read(site_id, session_id, value):
                pytest.fail("Foreign or absent session accepted")
    with service.read(
        summary.site_id,
        descriptor.session_id,
        AnalysisRange.local_days(date(2026, 3, 11), date(2026, 3, 11)),
    ) as read:
        assert read.context.reading_count == 0
        assert read.context.first_reading_received_at is None
        assert read.context.latest_reading_received_at is None


def test_read_snapshot_independent_writer_and_transaction_configuration(analysis_data):
    service, summary, _, add = analysis_data
    base = datetime(2026, 3, 10, tzinfo=UTC)
    descriptor, records = add(times=(base,))
    value = AnalysisRange.local_days(base.date(), base.date())
    with service.read(summary.site_id, descriptor.session_id, value) as read:
        assert read.connection.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert read.connection.scalar(text("SHOW transaction_read_only")) == "on"
        assert read.connection.scalar(text("SHOW statement_timeout")) == "10s"
        with service.engine.begin() as writer:
            assert writer.scalar(text("SELECT pg_backend_pid()")) != read.connection.scalar(
                text("SELECT pg_backend_pid()")
            )
            writer.execute(
                insert(db.snapshots).values(
                    **snapshot_values(records[0].model_copy(update={"record_id": uuid4()}), base)
                )
            )
        assert read.context.reading_count == 1
        assert read.connection.scalar(select(func.count()).select_from(db.snapshots)) == 1
    assert read.connection.closed
    with service.read(summary.site_id, descriptor.session_id, value) as second:
        assert second.context.reading_count == 2
    with service.engine.begin() as connection:
        assert connection.scalar(text("SHOW transaction_read_only")) == "off"
        assert connection.scalar(text("SHOW statement_timeout")) == "30s"


def test_analysis_jit_setting_is_local_to_reused_connection(analysis_data):
    service, _, _, _ = analysis_data
    with service.engine.connect() as connection:
        default = connection.scalar(text("SHOW jit"))
        backend = connection.scalar(text("SELECT pg_backend_pid()"))
    with service.transaction() as connection:
        assert connection.scalar(text("SELECT pg_backend_pid()")) == backend
        assert connection.scalar(text("SHOW jit")) == "off"
    with service.engine.connect() as connection:
        assert connection.scalar(text("SELECT pg_backend_pid()")) == backend
        assert connection.scalar(text("SHOW jit")) == default
    assert service.engine.pool.checkedout() == 0


def test_read_only_and_errors_close_connection(analysis_data):
    service, summary, _, add = analysis_data
    descriptor, _ = add(times=(datetime(2026, 3, 10, tzinfo=UTC),))
    value = AnalysisRange.local_days(date(2026, 3, 10), date(2026, 3, 10))
    with pytest.raises(QueryUnavailable, match="^temporarily_unavailable$"):
        with service.read(summary.site_id, descriptor.session_id, value) as read:
            read.connection.execute(insert(db.sites).values(site_id=uuid4()))
    assert read.connection.closed
    with pytest.raises(RuntimeError, match="caller"):
        with service.read(summary.site_id, descriptor.session_id, value) as read:
            raise RuntimeError("caller")
    assert read.connection.closed
    assert service.engine.pool.checkedout() == 0


def counters(import_t1, import_t2, export_t1="0", export_t2="0"):
    return {
        name: None if value is None else Decimal(value)
        for name, value in zip(
            (
                "energy_import_t1_kwh",
                "energy_import_t2_kwh",
                "energy_export_t1_kwh",
                "energy_export_t2_kwh",
            ),
            (import_t1, import_t2, export_t1, export_t2),
            strict=True,
        )
    }


def energy_for(service, summary, descriptor, first, before):
    with service.read(
        summary.site_id,
        descriptor.session_id,
        {
            "received_from": first,
            "received_before": before,
        },
    ) as read:
        return read.energy()


def test_energy_partial_missing_gaps_and_independent_directions(analysis_data):
    service, summary, _, add = analysis_data
    base = datetime(2026, 3, 10, 12, tzinfo=UTC)
    times = tuple(base + timedelta(seconds=n) for n in (0, 10, 20, 70, 80))
    descriptor, _ = add(
        times=times,
        metrics=[
            counters("100", "50", "10", "5"),
            counters(None, "51", "11", "6"),
            counters("102", "52", None, "7"),
            counters("105", "55", "13", "8"),
            counters("1000", "1000", "1000", "1000"),
        ],
    )
    result = energy_for(service, summary, descriptor, base, times[-1])
    imported, exported = result.import_energy, result.export_energy
    assert imported.energy_kwh == Decimal("10")
    assert exported.energy_kwh == Decimal("6")
    for value in (imported, exported):
        assert value.valid_endpoint_count == 3 and value.missing_endpoint_count == 1
        assert value.valid_interval_count == 2 and value.bridged_gap_count == 1
        assert value.reset_count == 0
        assert value.first_endpoint_received_at == base
        assert value.last_endpoint_received_at == times[3]
        assert value.daily[0].date == date(2026, 3, 10)
        assert value.daily[0].energy_kwh == value.energy_kwh
        assert value.daily[0].valid_interval_count == 2
        assert value.cross_day_unallocated_kwh == 0
        assert set(value.incompleteness_reasons) == {"missing_tariffs", "sampling_gaps"}
    assert result.model_dump(mode="json")["import_energy"]["energy_kwh"] == "10"
    # Never borrow the endpoint just before the requested range.
    narrowed = energy_for(service, summary, descriptor, times[2], times[-1])
    assert narrowed.import_energy.energy_kwh == Decimal("6")
    assert narrowed.export_energy.energy_kwh is None


def test_energy_tariff_regression_even_when_total_increases(analysis_data):
    service, summary, _, add = analysis_data
    base = datetime(2026, 3, 10, 12, tzinfo=UTC)
    times = tuple(base + timedelta(seconds=10 * n) for n in range(4))
    descriptor, _ = add(
        times=times,
        metrics=[
            counters("10", "10", "5", "5"),
            counters("9", "30", "6", "6"),  # combined total rises, T1 regresses
            counters("11", "31", "7", "7"),
            counters("12", "30", "8", "8"),  # T2 regression also excluded
        ],
    )
    result = energy_for(service, summary, descriptor, base, times[-1] + timedelta(seconds=1))
    assert result.import_energy.energy_kwh == 3
    assert result.import_energy.reset_count == 2
    assert result.import_energy.valid_interval_count == 1
    assert result.import_energy.daily[0].first_endpoint_received_at == times[1]
    assert "counter_regressions" in result.import_energy.incompleteness_reasons
    assert result.export_energy.energy_kwh == 6
    assert result.export_energy.reset_count == 0


@pytest.mark.parametrize("count", [0, 1, 2])
def test_energy_insufficient_endpoints_and_zero(analysis_data, count):
    service, summary, _, add = analysis_data
    base = datetime(2026, 3, 10, 12, tzinfo=UTC)
    descriptor, _ = add(
        times=[base + timedelta(seconds=10 * n) for n in range(count)],
        metrics=[counters("0", "0", None, "0") for _ in range(count)],
    )
    result = energy_for(service, summary, descriptor, base, base + timedelta(seconds=21))
    imported, exported = result.import_energy, result.export_energy
    assert imported.energy_kwh == (Decimal(0) if count == 2 else None)
    assert imported.cross_day_unallocated_kwh == (Decimal(0) if count == 2 else None)
    assert exported.energy_kwh is None and exported.daily == ()
    assert "insufficient_endpoints" in exported.incompleteness_reasons
    assert exported.missing_endpoint_count == count
    if count < 2:
        assert imported.daily == ()
        assert "insufficient_endpoints" in imported.incompleteness_reasons


def test_energy_large_exact_values_without_python_context_rounding(analysis_data):
    service, summary, _, add = analysis_data
    base = datetime(2026, 3, 10, 12, tzinfo=UTC)
    descriptor, _ = add(
        times=[base, base + timedelta(seconds=10), base + timedelta(seconds=20)],
        metrics=[
            counters("10000000000000000000000000000000000000000.00000000001", "0"),
            counters(
                "10000000000000000000000000000000000000000.00000000002",
                "123456789012345678901234567890.12345678901",
            ),
            counters(
                "10000000000000000000000000000000000000000.00000000003",
                "123456789012345678901234567890.12345678902",
            ),
        ],
    )
    with localcontext() as context:
        context.prec = 6
        result = energy_for(service, summary, descriptor, base, base + timedelta(seconds=21))
    expected = Decimal("123456789012345678901234567890.12345678904")
    assert result.import_energy.energy_kwh == expected
    assert result.import_energy.daily[0].energy_kwh == expected
    assert result.model_dump(mode="json")["import_energy"]["energy_kwh"] == str(expected)


@pytest.mark.parametrize("day", [date(2026, 3, 29), date(2026, 10, 25)])
def test_energy_midnight_dst_and_cross_day_identity(analysis_data, day):
    from watt_profile.server.analysis import AMSTERDAM

    service, summary, _, add = analysis_data
    midnight = datetime.combine(day, datetime.min.time(), AMSTERDAM).astimezone(UTC)
    next_midnight = datetime.combine(
        day + timedelta(days=1),
        datetime.min.time(),
        AMSTERDAM,
    ).astimezone(UTC)
    times = [
        midnight,
        next_midnight - timedelta(seconds=10),
        next_midnight,
        next_midnight + timedelta(hours=12),
        next_midnight + timedelta(days=1, seconds=1),
    ]
    descriptor, _ = add(
        times=times,
        metrics=[
            counters("0", "0", "10", "0"),
            counters("1", "1", "11", "0"),
            counters("2", "2", "12", "0"),
            counters("3", "3", "13", "0"),
            counters("4", "4", "14", "0"),
        ],
    )
    result = energy_for(service, summary, descriptor, midnight, times[-1] + timedelta(seconds=1))
    for value, total, daily, unallocated in (
        (result.import_energy, "8", ["4", "2"], "2"),
        (result.export_energy, "4", ["2", "1"], "1"),
    ):
        assert value.energy_kwh == Decimal(total)
        assert [row.date for row in value.daily] == [day, day + timedelta(days=1)]
        assert [row.energy_kwh for row in value.daily] == list(map(Decimal, daily))
        assert value.daily[0].last_endpoint_received_at == next_midnight
        assert value.cross_day_unallocated_kwh == Decimal(unallocated)
        assert (
            sum(row.energy_kwh for row in value.daily) + value.cross_day_unallocated_kwh
            == value.energy_kwh
        )
        assert "cross_day_unallocated" in value.incompleteness_reasons


def test_energy_missing_tariffs_over_dense_readings_are_not_sampling_gaps(analysis_data):
    service, summary, _, add = analysis_data
    base = datetime(2026, 3, 10, 12, tzinfo=UTC)
    descriptor, _ = add(
        times=[base + timedelta(seconds=10 * n) for n in range(6)],
        metrics=[counters("0", "0")]
        + [counters(None, "1") for _ in range(4)]
        + [counters("2", "3")],
    )
    result = energy_for(service, summary, descriptor, base, base + timedelta(seconds=51))
    assert result.import_energy.energy_kwh == 5
    assert result.import_energy.bridged_gap_count == 0
    assert result.import_energy.incompleteness_reasons == ("missing_tariffs",)


def test_energy_tied_times_follow_record_order_not_insertion(analysis_data):
    service, summary, _, add = analysis_data
    base = datetime(2026, 3, 10, 12, tzinfo=UTC)
    descriptor, _ = add(
        times=[base, base, base + timedelta(seconds=10)],
        record_ids=[
            UUID("ffffffff-ffff-4fff-bfff-ffffffffffff"),
            UUID("00000000-0000-4000-8000-000000000001"),
            uuid4(),
        ],
        metrics=[counters("100", "0"), counters("0", "0"), counters("101", "0")],
    )
    result = energy_for(service, summary, descriptor, base, base + timedelta(seconds=11))
    assert result.import_energy.energy_kwh == 101
    assert result.import_energy.valid_interval_count == 2
    assert result.import_energy.reset_count == 0


def test_energy_only_regressions_are_unavailable(analysis_data):
    service, summary, _, add = analysis_data
    base = datetime(2026, 3, 10, 12, tzinfo=UTC)
    descriptor, _ = add(
        times=[base, base + timedelta(seconds=10)],
        metrics=[counters("10", "10"), counters("0", "0")],
    )
    result = energy_for(service, summary, descriptor, base, base + timedelta(seconds=11))
    assert result.import_energy.energy_kwh is None
    assert result.import_energy.cross_day_unallocated_kwh is None
    assert result.import_energy.valid_endpoint_count == 2
    assert result.import_energy.reset_count == 1
    assert "no_valid_intervals" in result.import_energy.incompleteness_reasons
