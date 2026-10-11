"""HTTP-independent validated reading filters and consistent PostgreSQL queries."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Self
from uuid import UUID

from pydantic import Field, StrictInt, TypeAdapter, ValidationError, model_validator
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError

from watt_profile.contracts.domain import UUID4, DomainModel, SessionDescriptor, Snapshot, UtcTime
from watt_profile.server.registration import (
    Device,
    ObjectNotFound,
    Page,
    PageOptions,
    SiteInput,
    SiteSearch,
)
from watt_profile.server.storage import schema as db
from watt_profile.server.storage.repository import load_snapshot


class InvalidQuery(ValueError):
    status_code = 422

    def __init__(self):
        super().__init__("invalid_query")


class QueryUnavailable(RuntimeError):
    status_code = 503

    def __init__(self):
        super().__init__("temporarily_unavailable")


def query_id(value: UUID | str) -> UUID:
    try:
        return TypeAdapter(UUID4).validate_python(value)
    except (ValueError, TypeError):
        raise InvalidQuery() from None


class ReadingFilters(DomainModel):
    session_id: UUID4 | None = None
    device_id: UUID4 | None = None
    received_from: UtcTime | None = None
    received_before: UtcTime | None = None

    @model_validator(mode="after")
    def interval(self) -> Self:
        if (
            self.received_from is not None
            and self.received_before is not None
            and self.received_from >= self.received_before
        ):
            raise ValueError("invalid_interval")
        return self

    def predicates(self, site_id: UUID):
        conditions = [db.snapshots.c.site_id == site_id]
        for name in ("session_id", "device_id"):
            if (value := getattr(self, name)) is not None:
                conditions.append(db.snapshots.c[name] == value)
        if self.received_from is not None:
            conditions.append(db.snapshots.c.received_at >= self.received_from)
        if self.received_before is not None:
            conditions.append(db.snapshots.c.received_at < self.received_before)
        return conditions


def parse_filters(value: ReadingFilters | dict | None = None) -> ReadingFilters:
    try:
        return ReadingFilters.model_validate(
            value.model_dump()
            if isinstance(value, ReadingFilters)
            else {}
            if value is None
            else value
        )
    except (ValidationError, TypeError):
        raise InvalidQuery() from None


class ReadingPageOptions(DomainModel):
    offset: Annotated[StrictInt, Field(ge=0)] = 0
    limit: Annotated[StrictInt, Field(ge=1, le=200)] = 50


class SiteStatus(DomainModel):
    site: SiteInput
    reading_count: int
    latest_reading_received_at: UtcTime | None
    authorized_device_ids: tuple[UUID4, ...]


class DeviceStatus(DomainModel):
    device: Device
    latest_reading_received_at: UtcTime | None


def readings_statement(site_id: UUID, filters: ReadingFilters):
    return (
        select(db.snapshots)
        .where(*filters.predicates(site_id))
        .order_by(db.snapshots.c.received_at, db.snapshots.c.device_id, db.snapshots.c.record_id)
    )


def require_site(connection, site_id):
    row = (
        connection.execute(select(db.sites).where(db.sites.c.site_id == site_id))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ObjectNotFound()
    return SiteInput.model_validate(dict(row))


class QueryService:
    def __init__(self, engine: Engine):
        self.engine = engine

    @contextmanager
    def read(self) -> Iterator:
        try:
            with self.engine.connect().execution_options(
                isolation_level="REPEATABLE READ"
            ) as connection:
                with connection.begin():
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                    yield connection
        except SQLAlchemyError:
            raise QueryUnavailable() from None

    def reading_page(self, site_id, filters=None, options=ReadingPageOptions()) -> Page[Snapshot]:
        site_id, filters = query_id(site_id), parse_filters(filters)
        try:
            options = ReadingPageOptions.model_validate(options.model_dump())
        except (ValidationError, AttributeError):
            raise InvalidQuery() from None
        with self.read() as connection:
            require_site(connection, site_id)
            total = connection.scalar(
                select(func.count()).select_from(db.snapshots).where(*filters.predicates(site_id))
            )
            rows = connection.execute(
                readings_statement(site_id, filters).offset(options.offset).limit(options.limit)
            ).mappings()
            return Page[Snapshot](
                items=tuple(load_snapshot(row).snapshot for row in rows), total=total
            )

    def snapshot_detail(self, site_id, device_id, record_id) -> Snapshot:
        site_id, device_id, record_id = map(query_id, (site_id, device_id, record_id))
        with self.read() as connection:
            row = (
                connection.execute(
                    select(db.snapshots).where(
                        db.snapshots.c.site_id == site_id,
                        db.snapshots.c.device_id == device_id,
                        db.snapshots.c.record_id == record_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ObjectNotFound()
            return load_snapshot(row).snapshot

    def session_page(
        self, site_id, device_id=None, options=PageOptions()
    ) -> Page[SessionDescriptor]:
        site_id = query_id(site_id)
        try:
            options = PageOptions.model_validate(options.model_dump())
        except (ValidationError, AttributeError):
            raise InvalidQuery() from None
        conditions = [db.sessions.c.site_id == site_id]
        if device_id is not None:
            conditions.append(db.sessions.c.device_id == query_id(device_id))
        with self.read() as connection:
            require_site(connection, site_id)
            total = connection.scalar(
                select(func.count()).select_from(db.sessions).where(*conditions)
            )
            rows = connection.execute(
                select(db.sessions)
                .where(*conditions)
                .order_by(db.sessions.c.started_at, db.sessions.c.session_id)
                .offset(options.offset)
                .limit(options.limit)
            ).mappings()
            return Page[SessionDescriptor](
                items=tuple(SessionDescriptor.model_validate(dict(row)) for row in rows),
                total=total,
            )

    def _site_status(self, connection, site: SiteInput) -> SiteStatus:
        count, latest = connection.execute(
            select(func.count(), func.max(db.snapshots.c.received_at)).where(
                db.snapshots.c.site_id == site.site_id
            )
        ).one()
        devices = connection.execute(
            select(db.authorizations.c.device_id)
            .where(db.authorizations.c.site_id == site.site_id)
            .order_by(db.authorizations.c.device_id)
        ).scalars()
        return SiteStatus(
            site=site,
            reading_count=count,
            latest_reading_received_at=latest,
            authorized_device_ids=tuple(devices),
        )

    def site_status(self, site_id) -> SiteStatus:
        with self.read() as connection:
            return self._site_status(connection, require_site(connection, query_id(site_id)))

    def site_page(self, search=SiteSearch()) -> Page[SiteStatus]:
        try:
            search = SiteSearch.model_validate(search.model_dump())
            search.predicate()
        except ValueError:
            raise InvalidQuery() from None
        conditions = []
        if search.site_id is not None:
            conditions.append(db.sites.c.site_id == search.site_id)
        if search.q is not None:
            conditions.append(db.sites.c.address.icontains(search.q, autoescape=True))
        with self.read() as connection:
            total = connection.scalar(select(func.count()).select_from(db.sites).where(*conditions))
            sites = (
                connection.execute(
                    select(db.sites)
                    .where(*conditions)
                    .order_by(db.sites.c.site_id)
                    .offset(search.offset)
                    .limit(search.limit)
                )
                .mappings()
                .all()
            )
            return Page[SiteStatus](
                items=tuple(
                    self._site_status(connection, SiteInput.model_validate(dict(row)))
                    for row in sites
                ),
                total=total,
            )

    def device_status(self, device_id) -> DeviceStatus:
        device_id = query_id(device_id)
        with self.read() as connection:
            row = (
                connection.execute(select(db.devices).where(db.devices.c.device_id == device_id))
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ObjectNotFound()
            latest = connection.scalar(
                select(func.max(db.snapshots.c.received_at)).where(
                    db.snapshots.c.device_id == device_id
                )
            )
            return DeviceStatus(
                device=Device.model_validate(dict(row)), latest_reading_received_at=latest
            )

    def device_page(self, options=PageOptions()) -> Page[DeviceStatus]:
        try:
            options = PageOptions.model_validate(options.model_dump())
        except (ValidationError, AttributeError):
            raise InvalidQuery() from None
        with self.read() as connection:
            total = connection.scalar(select(func.count()).select_from(db.devices))
            latest = (
                select(
                    db.snapshots.c.device_id, func.max(db.snapshots.c.received_at).label("latest")
                )
                .group_by(db.snapshots.c.device_id)
                .subquery()
            )
            rows = connection.execute(
                select(db.devices, latest.c.latest)
                .outerjoin(latest, db.devices.c.device_id == latest.c.device_id)
                .order_by(db.devices.c.device_id)
                .offset(options.offset)
                .limit(options.limit)
            ).mappings()
            return Page[DeviceStatus](
                items=tuple(
                    DeviceStatus(
                        device=Device.model_validate(
                            {name: row[name] for name in Device.model_fields}
                        ),
                        latest_reading_received_at=row["latest"],
                    )
                    for row in rows
                ),
                total=total,
            )
