"""Management analysis composition; every call owns its worker and read snapshot."""

from functools import partial
from threading import BoundedSemaphore, Lock
from typing import Literal
from uuid import UUID

from sqlalchemy import func, or_, select

from watt_profile.contracts.domain import UUID4, DecimalValue, DomainModel, Snapshot, UtcTime
from watt_profile.server.analysis import AnalysisContext, AnalysisService, EnergyResult
from watt_profile.server.analysis_power import PowerResult
from watt_profile.server.analysis_quality import QualityResult
from watt_profile.server.queries import QueryUnavailable, query_id, require_site
from watt_profile.server.registration import ObjectNotFound, Page, SiteInput, SiteSearch
from watt_profile.server.storage import schema as db
from watt_profile.server.storage.repository import load_snapshot


class WorkspaceSite(DomainModel):
    site: SiteInput
    reading_count: int
    latest_reading_received_at: UtcTime | None


class PowerStatistics(DomainModel):
    metric: Literal["power_import_kw", "power_export_kw"]
    mean: DecimalValue | None
    minimum: DecimalValue | None
    maximum: DecimalValue | None
    valid_count: int
    unavailability_reason: Literal["no_valid_readings"] | None


class Overview(DomainModel):
    context: AnalysisContext
    statistics: tuple[PowerStatistics, ...]
    energy: EnergyResult
    power: PowerResult
    quality: QualityResult


class Latest(DomainModel):
    site_id: UUID4
    session_id: UUID4
    device_id: UUID4
    calculated_at: UtcTime
    snapshot: Snapshot | None
    ingested_at: UtcTime | None
    device_last_authenticated_contact_at: UtcTime | None
    site_latest_reading_received_at: UtcTime | None


class HistoricalJob:
    """Admit before scheduling, then transfer slot ownership to the worker.

    Cancelling a queued job releases admission immediately; a running job retains
    admission until its database transaction exits (at most the statement timeout
    for a blocked query). This also covers cancellation before thread dispatch.
    """

    def __init__(self, slots, call):
        if not slots.acquire(blocking=False):
            raise QueryUnavailable()
        self.slots, self.call = slots, call
        self.lock = Lock()
        self.state = "pending"

    def __call__(self):
        with self.lock:
            if self.state != "pending":
                raise QueryUnavailable()
            self.state = "running"
        try:
            return self.call()
        finally:
            with self.lock:
                self.state = "done"
                self.slots.release()

    def cancel_pending(self):
        with self.lock:
            if self.state == "pending":
                self.state = "done"
                self.slots.release()


class AnalysisAPI:
    def __init__(self, engine):
        self.service = AnalysisService(engine)
        self.slots = BoundedSemaphore(2)

    def admit(self, site_id, session_id, analysis_range, operation, **options):
        return HistoricalJob(
            self.slots,
            partial(self._historical, site_id, session_id, analysis_range, operation, **options),
        )

    def _historical(self, site_id, session_id, analysis_range, operation, **options):
        with self.service.read(site_id, session_id, analysis_range) as read:
            if operation == "overview":
                statistics = []
                for name in ("power_import_kw", "power_export_kw"):
                    metric = db.snapshots.c[name]
                    mean, minimum, maximum, count = read.connection.execute(
                        select(
                            func.avg(metric),
                            func.min(metric),
                            func.max(metric),
                            func.count(metric),
                        ).where(
                            *read.context.range.predicates(
                                read.context.site_id, read.context.session_id
                            )
                        )
                    ).one()
                    statistics.append(
                        PowerStatistics(
                            metric=name,
                            mean=mean,
                            minimum=minimum,
                            maximum=maximum,
                            valid_count=count,
                            unavailability_reason=None if count else "no_valid_readings",
                        )
                    )
                return Overview(
                    context=read.context,
                    statistics=tuple(statistics),
                    energy=read.energy(),
                    power=read.power(),
                    quality=read.quality(),
                )
            return getattr(read, operation)(**options)

    def workspace(self, search: SiteSearch) -> Page[WorkspaceSite]:
        search.predicate()
        conditions = []
        if search.site_id is not None:
            conditions.append(db.sites.c.site_id == search.site_id)
        if search.q is not None:
            matches = [
                db.sites.c.address.icontains(search.q, autoescape=True),
                db.sites.c.display_name.icontains(search.q, autoescape=True),
            ]
            try:
                identifier = UUID(search.q)
                matches.append(db.sites.c.site_id == identifier)
            except ValueError:
                pass
            conditions.append(or_(*matches))
        with self.service.transaction() as connection:
            total = connection.scalar(select(func.count()).select_from(db.sites).where(*conditions))
            totals = (
                select(
                    db.snapshots.c.site_id,
                    func.count().label("reading_count"),
                    func.max(db.snapshots.c.received_at).label("latest"),
                )
                .group_by(db.snapshots.c.site_id)
                .subquery()
            )
            rows = connection.execute(
                select(
                    db.sites,
                    func.coalesce(totals.c.reading_count, 0).label("reading_count"),
                    totals.c.latest,
                )
                .outerjoin(totals, totals.c.site_id == db.sites.c.site_id)
                .where(*conditions)
                .order_by(db.sites.c.site_id)
                .offset(search.offset)
                .limit(search.limit)
            ).mappings()
            return Page[WorkspaceSite](
                total=total,
                items=tuple(
                    WorkspaceSite(
                        site=SiteInput.model_validate(
                            {name: row[name] for name in SiteInput.model_fields}
                        ),
                        reading_count=row["reading_count"],
                        latest_reading_received_at=row["latest"],
                    )
                    for row in rows
                ),
            )

    def latest(self, site_id, session_id) -> Latest:
        site_id, session_id = query_id(site_id), query_id(session_id)
        with self.service.transaction() as connection:
            require_site(connection, site_id)
            device_id = connection.scalar(
                select(db.sessions.c.device_id).where(
                    db.sessions.c.site_id == site_id, db.sessions.c.session_id == session_id
                )
            )
            if device_id is None:
                raise ObjectNotFound()
            row = (
                connection.execute(
                    select(db.snapshots)
                    .where(
                        db.snapshots.c.site_id == site_id, db.snapshots.c.session_id == session_id
                    )
                    .order_by(
                        db.snapshots.c.received_at.desc(),
                        db.snapshots.c.device_id.desc(),
                        db.snapshots.c.record_id.desc(),
                    )
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            try:
                stored = load_snapshot(row) if row is not None else None
            except (ValueError, TypeError, KeyError):
                raise QueryUnavailable() from None
            if stored is not None:
                device_id = stored.snapshot.device_id
            return Latest(
                site_id=site_id,
                session_id=session_id,
                device_id=device_id,
                calculated_at=connection.scalar(select(func.transaction_timestamp())),
                snapshot=stored.snapshot if stored else None,
                ingested_at=stored.ingested_at if stored else None,
                device_last_authenticated_contact_at=connection.scalar(
                    select(db.devices.c.last_authenticated_contact_at).where(
                        db.devices.c.device_id == device_id
                    )
                ),
                site_latest_reading_received_at=connection.scalar(
                    select(func.max(db.snapshots.c.received_at)).where(
                        db.snapshots.c.site_id == site_id
                    )
                ),
            )
