"""M5-02 context and ranges, using synthetic isolated PostgreSQL data only."""

from datetime import UTC, date, datetime, timedelta
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

    def add(*, session_id=None, started_at=start, times=(), ingested_at=None):
        value = batch(summary, session_id=session_id, record_ids=[uuid4() for _ in times])
        descriptor = value.session.model_copy(update={"started_at": started_at})
        records = [
            record.model_copy(update={"received_at": received, "sampled_at": received})
            for record, received in zip(value.records, times, strict=False)
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
