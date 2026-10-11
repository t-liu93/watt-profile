"""M3-05 queries against isolated PostgreSQL and synthetic validated readings."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import event, insert, update
from test_server_ingest import batch

from watt_profile.contracts.domain import Metrics
from watt_profile.server.queries import (
    InvalidQuery,
    QueryService,
    ReadingPageOptions,
    parse_filters,
)
from watt_profile.server.registration import ObjectNotFound, RegistrationSummary, SiteSearch
from watt_profile.server.storage import schema as db
from watt_profile.server.storage.registration import RegistrationRepository
from watt_profile.server.storage.repository import ServerRepository, snapshot_values


@pytest.fixture
def dataset(server_engine):
    registration = RegistrationRepository(server_engine)
    summary = RegistrationSummary(
        device_id=uuid4(),
        site_id=uuid4(),
        address="Synthetic 50%_\\ lane",
        description="",
        token="a" * 43,
    )
    registration.commit(summary)
    value = batch(summary, record_ids=[uuid4() for _ in range(4)])
    base = datetime(2026, 1, 1, tzinfo=UTC)
    records = []
    for index, record in enumerate(value.records):
        metrics = (
            record.metrics.model_copy(
                update={
                    name: Decimal("12345678901234567890.123400") for name in Metrics.model_fields
                }
            )
            if index == 1
            else record.metrics
        )
        metrics = metrics.model_copy(
            update={"power_import_kw": Decimal("0.000"), "power_export_kw": Decimal("1.2300")}
        )
        records.append(
            record.model_copy(
                update={
                    "received_at": base + timedelta(seconds=index // 2),
                    "sampled_at": base + timedelta(seconds=index // 2),
                    "metrics": metrics,
                }
            )
        )
    repository = ServerRepository(server_engine)
    with repository.transaction() as session:
        repository.insert_descriptors(session, value.session, value.source)
        session.execute(insert(db.snapshots), [snapshot_values(record, base) for record in records])
    return server_engine, summary, value, records, registration


def test_combined_edges_ties_empty_detail_and_sessions(dataset):
    engine, summary, value, records, _ = dataset
    service = QueryService(engine)
    filters = {
        "received_from": "2026-01-01T01:00:00+01:00",
        "received_before": "2026-01-01T00:00:01Z",
        "device_id": str(summary.device_id),
        "session_id": str(value.session.session_id),
    }
    page = service.reading_page(summary.site_id, filters)
    assert page.items == tuple(sorted(records[:2], key=lambda r: r.record_id))
    assert page.total == 2
    assert service.reading_page(summary.site_id, {"device_id": str(uuid4())}).total == 0
    assert service.reading_page(summary.site_id, {"session_id": str(uuid4())}).items == ()
    assert (
        service.reading_page(summary.site_id, options=ReadingPageOptions(offset=3, limit=1)).total
        == 4
    )
    assert (
        service.snapshot_detail(summary.site_id, records[0].device_id, records[0].record_id)
        == records[0]
    )
    with pytest.raises(ObjectNotFound):
        service.snapshot_detail(uuid4(), records[0].device_id, records[0].record_id)
    assert service.session_page(summary.site_id).items == (value.session,)
    assert service.session_page(summary.site_id, uuid4()).total == 0


@pytest.mark.parametrize(
    "filters",
    [
        {"device_id": "bad"},
        {"session_id": "1"},
        {"received_from": "2026-01-01T00:00:00"},
        {"received_before": "no"},
        {"received_from": "2026-01-01T00:00:00Z", "received_before": "2026-01-01T00:00:00Z"},
        {"extra": 1},
    ],
)
def test_strict_filters(filters):
    with pytest.raises(InvalidQuery):
        parse_filters(filters)


def test_status_independent_contact_site_history_and_literal_search(dataset):
    engine, summary, _, records, registration = dataset
    service = QueryService(engine)
    now = datetime.now(UTC)
    other_site = uuid4()
    registration.commit(summary.model_copy(update={"site_id": other_site, "address": "other"}))
    with engine.begin() as connection:
        connection.execute(update(db.devices).values(last_authenticated_contact_at=now))
    status = service.device_status(summary.device_id)
    assert status.device.last_authenticated_contact_at == now
    assert status.latest_reading_received_at == max(r.received_at for r in records)
    assert service.device_page().items == (status,)
    assert service.site_status(summary.site_id).reading_count == 4
    assert service.site_status(other_site).latest_reading_received_at is None
    assert service.site_status(other_site).authorized_device_ids == (summary.device_id,)
    assert service.site_page(SiteSearch(q="50%_\\")).total == 1
    assert service.site_page(SiteSearch(q="50X")).total == 0


def test_page_total_and_items_keep_same_snapshot_during_insert(dataset):
    engine, summary, _, records, _ = dataset
    inserted = records[0].model_copy(update={"record_id": uuid4()})
    fired = False

    def concurrent(_connection, _cursor, statement, *_args):
        nonlocal fired
        if "count(" in statement and "snapshots" in statement and not fired:
            fired = True
            with engine.begin() as writer:
                writer.execute(
                    insert(db.snapshots).values(**snapshot_values(inserted, datetime.now(UTC)))
                )

    event.listen(engine, "after_cursor_execute", concurrent)
    try:
        page = QueryService(engine).reading_page(summary.site_id)
    finally:
        event.remove(engine, "after_cursor_execute", concurrent)
    assert fired and page.total == len(page.items) == 4
    assert QueryService(engine).reading_page(summary.site_id).total == 5


@pytest.mark.parametrize(
    "column,value", [("power_import_kw", Decimal("9")), ("payload_sha256", "f" * 64)]
)
def test_canonical_projection_corruption_rejected(dataset, column, value):
    engine, summary, _, records, _ = dataset
    with engine.begin() as connection:
        connection.execute(
            update(db.snapshots)
            .where(db.snapshots.c.record_id == records[0].record_id)
            .values(**{column: value})
        )
    with pytest.raises(ValueError, match="snapshot_projection_mismatch"):
        QueryService(engine).reading_page(summary.site_id)


def test_tied_timestamps_order_by_device_then_record(dataset):
    engine, summary, _, records, registration = dataset
    other = summary.model_copy(update={"device_id": uuid4(), "token": "b" * 43})
    registration.commit(other)
    value = batch(other, record_ids=[uuid4(), uuid4()])
    added = [
        record.model_copy(update={"received_at": records[0].received_at})
        for record in value.records
    ]
    repository = ServerRepository(engine)
    with repository.transaction() as session:
        repository.insert_descriptors(session, value.session, value.source)
        session.execute(
            insert(db.snapshots), [snapshot_values(record, datetime.now(UTC)) for record in added]
        )
    page = QueryService(engine).reading_page(summary.site_id)
    assert page.items == tuple(
        sorted(records + added, key=lambda r: (r.received_at, r.device_id, r.record_id))
    )
