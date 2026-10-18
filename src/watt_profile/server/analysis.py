"""HTTP-independent analysis context and bounded, consistent PostgreSQL reads.

All service calls are synchronous. HTTP callers must run the whole operation,
including entering/exiting ``read``, in a worker thread, never on the event loop.
Do not retain a read transaction across requests or while awaiting client input.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import ValidationError, model_validator
from sqlalchemy import Connection, Engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError

from watt_profile.contracts.domain import UUID4, DomainModel, SessionDescriptor, UtcTime
from watt_profile.server.queries import QueryUnavailable, query_id, require_site
from watt_profile.server.registration import ObjectNotFound, PageOptions
from watt_profile.server.storage import schema as db

AMSTERDAM = ZoneInfo("Europe/Amsterdam")
MICROSECOND = timedelta(microseconds=1)


class InvalidAnalysis(ValueError):
    status_code = 422

    def __init__(self):
        super().__init__("invalid_analysis_range")


class AnalysisRange(DomainModel):
    received_from: UtcTime
    received_before: UtcTime

    @model_validator(mode="after")
    def interval(self) -> Self:
        if self.received_from >= self.received_before:
            raise ValueError("invalid_interval")
        first = self.received_from.astimezone(AMSTERDAM).date()
        last = (self.received_before - MICROSECOND).astimezone(AMSTERDAM).date()
        if (last - first).days >= 31:
            raise ValueError("too_many_local_dates")
        return self

    @classmethod
    def local_days(cls, first: date, last: date) -> Self:
        """Convert inclusive local dates to complete days, respecting DST."""
        try:
            if type(first) is not date or type(last) is not date or first > last:
                raise ValueError("invalid_dates")
            return cls(
                received_from=datetime.combine(first, time.min, AMSTERDAM),
                received_before=datetime.combine(last + timedelta(days=1), time.min, AMSTERDAM),
            )
        except (ValueError, OverflowError):
            raise InvalidAnalysis() from None

    def predicates(self, site_id, session_id):
        return (
            db.snapshots.c.site_id == site_id,
            db.snapshots.c.session_id == session_id,
            db.snapshots.c.received_at >= self.received_from,
            db.snapshots.c.received_at < self.received_before,
        )


def parse_range(value: AnalysisRange | dict) -> AnalysisRange:
    try:
        return AnalysisRange.model_validate(
            value.model_dump() if isinstance(value, AnalysisRange) else value
        )
    except (ValidationError, TypeError, OverflowError):
        raise InvalidAnalysis() from None


class SessionContext(DomainModel):
    session: SessionDescriptor
    reading_count: int
    first_reading_received_at: UtcTime | None
    latest_reading_received_at: UtcTime | None


def initial_range(context: SessionContext) -> AnalysisRange | None:
    """Resolve recent seven dates; an empty session has no fabricated range."""
    latest = context.latest_reading_received_at
    if latest is None:
        return None
    try:
        first_date = latest.astimezone(AMSTERDAM).date() - timedelta(days=6)
        start = datetime.combine(first_date, time.min, AMSTERDAM).astimezone(UTC)
        return parse_range(
            {
                "received_from": max(start, context.session.started_at),
                "received_before": latest + MICROSECOND,
            }
        )
    except (ValueError, OverflowError):
        raise InvalidAnalysis() from None


class SessionPage(DomainModel):
    items: tuple[SessionContext, ...]
    total: int
    state: Literal["ready", "empty"]
    default_session_id: UUID4 | None
    initial_range: AnalysisRange | None


class AnalysisContext(DomainModel):
    site_id: UUID4
    session_id: UUID4
    range: AnalysisRange
    timezone: Literal["Europe/Amsterdam"] = "Europe/Amsterdam"
    calculated_at: UtcTime
    reading_count: int
    first_reading_received_at: UtcTime | None
    latest_reading_received_at: UtcTime | None


@dataclass(frozen=True)
class AnalysisRead:
    connection: Connection
    context: AnalysisContext


def session_statement(site_id):
    readings = (
        select(
            db.snapshots.c.session_id,
            func.count().label("reading_count"),
            func.min(db.snapshots.c.received_at).label("first_reading_received_at"),
            func.max(db.snapshots.c.received_at).label("latest_reading_received_at"),
        )
        .where(db.snapshots.c.site_id == site_id)
        .group_by(db.snapshots.c.session_id)
        .subquery()
    )
    return (
        select(
            db.sessions,
            func.coalesce(readings.c.reading_count, 0).label("reading_count"),
            readings.c.first_reading_received_at,
            readings.c.latest_reading_received_at,
        )
        .outerjoin(readings, readings.c.session_id == db.sessions.c.session_id)
        .where(db.sessions.c.site_id == site_id)
        .order_by(
            readings.c.latest_reading_received_at.desc().nulls_last(),
            db.sessions.c.started_at.desc(),
            db.sessions.c.session_id.desc(),
        )
    )


def session_context(row) -> SessionContext:
    return SessionContext(
        session=SessionDescriptor.model_validate(
            {name: row[name] for name in SessionDescriptor.model_fields}
        ),
        **{name: row[name] for name in SessionContext.model_fields if name != "session"},
    )


class AnalysisService:
    def __init__(self, engine: Engine):
        self.engine = engine

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """One short read snapshot; database failures expose only a safe 503."""
        try:
            with self.engine.connect().execution_options(
                isolation_level="REPEATABLE READ"
            ) as connection:
                with connection.begin():
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                    connection.execute(text("SET LOCAL statement_timeout = '10s'"))
                    yield connection
        except SQLAlchemyError:
            raise QueryUnavailable() from None

    def session_page(self, site_id, options=PageOptions()) -> SessionPage:
        site_id = query_id(site_id)
        try:
            options = PageOptions.model_validate(options.model_dump())
        except (ValidationError, AttributeError):
            raise InvalidAnalysis() from None
        with self.transaction() as connection:
            require_site(connection, site_id)
            total = connection.scalar(
                select(func.count())
                .select_from(db.sessions)
                .where(db.sessions.c.site_id == site_id)
            )
            statement = session_statement(site_id)
            default_row = connection.execute(statement.limit(1)).mappings().one_or_none()
            default = session_context(default_row) if default_row is not None else None
            ready = default is not None and default.reading_count > 0
            items = connection.execute(
                statement.offset(options.offset).limit(options.limit)
            ).mappings()
            return SessionPage(
                items=tuple(session_context(row) for row in items),
                total=total,
                state="ready" if ready else "empty",
                default_session_id=default.session.session_id if ready else None,
                initial_range=initial_range(default) if ready else None,
            )

    @contextmanager
    def read(
        self, site_id, session_id, analysis_range: AnalysisRange | dict
    ) -> Iterator[AnalysisRead]:
        """Share this connection across all SQL calculations in one response."""
        site_id, session_id = query_id(site_id), query_id(session_id)
        analysis_range = parse_range(analysis_range)
        with self.transaction() as connection:
            require_site(connection, site_id)
            if (
                connection.scalar(
                    select(db.sessions.c.session_id).where(
                        db.sessions.c.site_id == site_id, db.sessions.c.session_id == session_id
                    )
                )
                is None
            ):
                raise ObjectNotFound()
            count, first, last = connection.execute(
                select(
                    func.count(),
                    func.min(db.snapshots.c.received_at),
                    func.max(db.snapshots.c.received_at),
                ).where(*analysis_range.predicates(site_id, session_id))
            ).one()
            yield AnalysisRead(
                connection=connection,
                context=AnalysisContext(
                    site_id=site_id,
                    session_id=session_id,
                    range=analysis_range,
                    calculated_at=connection.scalar(text("SELECT transaction_timestamp()")),
                    reading_count=count,
                    first_reading_received_at=first,
                    latest_reading_received_at=last,
                ),
            )
