"""HTTP-independent analysis context and bounded, consistent PostgreSQL reads.

All service calls are synchronous. HTTP callers must run the whole operation,
including entering/exiting ``read``, in a worker thread, never on the event loop.
Do not retain a read transaction across requests or while awaiting client input.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import ValidationError, model_validator
from sqlalchemy import Connection, Engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError

from watt_profile.contracts.domain import (
    UUID4,
    DecimalValue,
    DomainModel,
    SessionDescriptor,
    UtcTime,
)
from watt_profile.server.queries import QueryUnavailable, query_id, require_site
from watt_profile.server.registration import ObjectNotFound, PageOptions
from watt_profile.server.storage import schema as db

AMSTERDAM = ZoneInfo("Europe/Amsterdam")
MICROSECOND = timedelta(microseconds=1)

if TYPE_CHECKING:
    from watt_profile.server.analysis_phases import PhaseResult
    from watt_profile.server.analysis_power import PowerResult
    from watt_profile.server.analysis_quality import QualityResult
    from watt_profile.server.analysis_typical_day import TypicalDayResult


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

    def energy(self) -> "EnergyResult":
        return calculate_energy(self)

    def power(self, *, zoom: AnalysisRange | dict | None = None) -> "PowerResult":
        from watt_profile.server.analysis_power import calculate_power

        return calculate_power(self, zoom=zoom)

    def quality(self, *, gaps=None, problems=None) -> "QualityResult":
        from watt_profile.server.analysis_quality import calculate_quality

        return calculate_quality(self, gaps=gaps, problems=problems)

    def typical_day(self) -> "TypicalDayResult":
        from watt_profile.server.analysis_typical_day import calculate_typical_day

        return calculate_typical_day(self)

    def phases(self, *, direction: str = "import", zoom=None) -> "PhaseResult":
        from watt_profile.server.analysis_phases import calculate_phases

        return calculate_phases(self, direction=direction, zoom=zoom)


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
                    # Interactive aggregate requests are shorter than the JIT
                    # compilation cost of their expanded diagnostic plans.
                    connection.execute(text("SET LOCAL jit = off"))
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


class DailyEnergy(DomainModel):
    """Energy over recorded intervals, not an estimate for a complete date."""

    date: date
    energy_kwh: DecimalValue
    valid_interval_count: int
    first_endpoint_received_at: UtcTime
    last_endpoint_received_at: UtcTime


class DirectionEnergy(DomainModel):
    energy_kwh: DecimalValue | None
    cross_day_unallocated_kwh: DecimalValue | None
    valid_endpoint_count: int
    missing_endpoint_count: int
    valid_interval_count: int
    reset_count: int
    bridged_gap_count: int
    first_endpoint_received_at: UtcTime | None
    last_endpoint_received_at: UtcTime | None
    incompleteness_reasons: tuple[str, ...]
    daily: tuple[DailyEnergy, ...]


class EnergyResult(DomainModel):
    context: AnalysisContext
    import_energy: DirectionEnergy
    export_energy: DirectionEnergy


# Only the small grouped result (at most 31 dates per direction) leaves PostgreSQL.
# NUMERIC arithmetic stays in PostgreSQL: Python's default Decimal context would
# round accepted counters with more than 28 significant digits.
_ENERGY_SQL = text("""
WITH projected AS (
    SELECT s.received_at, s.device_id, s.record_id, v.*
    FROM snapshots s
    CROSS JOIN LATERAL (VALUES
        ('import', s.energy_import_t1_kwh, s.energy_import_t2_kwh),
        ('export', s.energy_export_t1_kwh, s.energy_export_t2_kwh)
    ) AS v(direction, t1, t2)
    WHERE s.site_id = :site_id AND s.session_id = :session_id
        AND s.received_at >= :received_from AND s.received_at < :received_before
), observed AS (
    SELECT *, lag(received_at) OVER (
        PARTITION BY direction ORDER BY received_at, device_id, record_id
    ) AS previous_reading_at
    FROM projected
), gap_marked AS (
    SELECT *, sum(CASE WHEN received_at - previous_reading_at > interval '30 seconds'
        THEN 1 ELSE 0 END) OVER (
        PARTITION BY direction ORDER BY received_at, device_id, record_id
        ROWS UNBOUNDED PRECEDING
    ) AS gap_index
    FROM observed
), directions AS (
    SELECT v.direction,
        count(*) FILTER (WHERE p.t1 IS NOT NULL AND p.t2 IS NOT NULL) AS endpoints,
        count(p.direction) FILTER (WHERE p.t1 IS NULL OR p.t2 IS NULL) AS missing,
        min(p.received_at) FILTER (WHERE p.t1 IS NOT NULL AND p.t2 IS NOT NULL) AS first_at,
        max(p.received_at) FILTER (WHERE p.t1 IS NOT NULL AND p.t2 IS NOT NULL) AS last_at
    FROM (VALUES ('import'), ('export')) AS v(direction)
    LEFT JOIN projected p USING (direction)
    GROUP BY v.direction
), previous AS (
    SELECT *, lag(t1) OVER w AS previous_t1, lag(t2) OVER w AS previous_t2,
        lag(received_at) OVER w AS previous_at,
        lag(gap_index) OVER w AS previous_gap_index
    FROM gap_marked WHERE t1 IS NOT NULL AND t2 IS NOT NULL
    WINDOW w AS (PARTITION BY direction ORDER BY received_at, device_id, record_id)
), intervals AS (
    SELECT *, t1 < previous_t1 OR t2 < previous_t2 AS reset,
        (t1 - previous_t1) + (t2 - previous_t2) AS delta,
        gap_index > previous_gap_index AS gap,
        CASE WHEN (previous_at AT TIME ZONE 'Europe/Amsterdam')::date =
                (received_at AT TIME ZONE 'Europe/Amsterdam')::date
            OR (received_at AT TIME ZONE 'Europe/Amsterdam') =
                ((previous_at AT TIME ZONE 'Europe/Amsterdam')::date + 1)::timestamp
            THEN (previous_at AT TIME ZONE 'Europe/Amsterdam')::date
        END AS day
    FROM previous WHERE previous_at IS NOT NULL
), grouped AS (
    SELECT direction, day, grouping(day) AS period,
        sum(delta) FILTER (WHERE NOT reset) AS energy,
        sum(delta) FILTER (WHERE NOT reset AND day IS NULL) AS unallocated,
        count(*) FILTER (WHERE NOT reset) AS valid_intervals,
        count(*) FILTER (WHERE reset) AS resets,
        count(*) FILTER (WHERE NOT reset AND gap) AS gaps,
        min(previous_at) FILTER (WHERE NOT reset) AS first_interval_at,
        max(received_at) FILTER (WHERE NOT reset) AS last_interval_at
    FROM intervals GROUP BY GROUPING SETS ((direction), (direction, day))
)
SELECT d.*, g.day, coalesce(g.period, 1) AS period, g.energy,
    CASE WHEN g.energy IS NOT NULL THEN coalesce(g.unallocated, 0) END AS unallocated,
    coalesce(g.valid_intervals, 0) AS valid_intervals,
    coalesce(g.resets, 0) AS resets, coalesce(g.gaps, 0) AS gaps,
    g.first_interval_at, g.last_interval_at
FROM directions d LEFT JOIN grouped g USING (direction)
ORDER BY d.direction, period DESC, day
""")


def calculate_energy(read: AnalysisRead) -> EnergyResult:
    """Compose cumulative and daily energy in an existing consistent read."""
    context = read.context
    rows = (
        read.connection.execute(
            _ENERGY_SQL,
            {
                "site_id": context.site_id,
                "session_id": context.session_id,
                "received_from": context.range.received_from,
                "received_before": context.range.received_before,
            },
        )
        .mappings()
        .all()
    )
    results = {}
    for direction in ("import", "export"):
        selected = [row for row in rows if row["direction"] == direction]
        period = next(row for row in selected if row["period"] == 1)
        reasons = []
        if period["endpoints"] < 2:
            reasons.append("insufficient_endpoints")
        elif not period["valid_intervals"]:
            reasons.append("no_valid_intervals")
        if period["missing"]:
            reasons.append("missing_tariffs")
        if period["resets"]:
            reasons.append("counter_regressions")
        if period["gaps"]:
            reasons.append("sampling_gaps")
        if period["first_at"] is not None and (
            period["first_at"] - context.range.received_from > timedelta(seconds=30)
            or context.range.received_before - period["last_at"] > timedelta(seconds=30)
        ):
            reasons.append("uncovered_range_edges")
        if any(
            row["period"] == 0 and row["day"] is None and row["valid_intervals"] for row in selected
        ):
            reasons.append("cross_day_unallocated")
        results[direction] = DirectionEnergy(
            energy_kwh=period["energy"],
            cross_day_unallocated_kwh=period["unallocated"],
            valid_endpoint_count=period["endpoints"],
            missing_endpoint_count=period["missing"],
            valid_interval_count=period["valid_intervals"],
            reset_count=period["resets"],
            bridged_gap_count=period["gaps"],
            first_endpoint_received_at=period["first_at"],
            last_endpoint_received_at=period["last_at"],
            incompleteness_reasons=tuple(reasons),
            daily=tuple(
                DailyEnergy(
                    date=row["day"],
                    energy_kwh=row["energy"],
                    valid_interval_count=row["valid_intervals"],
                    first_endpoint_received_at=row["first_interval_at"],
                    last_endpoint_received_at=row["last_interval_at"],
                )
                for row in selected
                if row["period"] == 0 and row["day"] is not None and row["valid_intervals"]
            ),
        )
    return EnergyResult(
        context=context, import_energy=results["import"], export_energy=results["export"]
    )
