"""Typed short-lived repository transactions, without runtime or network ownership."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Self
from uuid import UUID

from pydantic import Field, StrictInt, StrictStr, TypeAdapter, model_validator
from sqlalchemy import Engine, LargeBinary, case, delete, func, insert, select, update
from sqlalchemy.orm import Session

from watt_profile.contracts.domain import (
    UUID4,
    DecimalValue,
    DomainModel,
    SessionDescriptor,
    Snapshot,
    SourceDescriptor,
    UtcTime,
    canonical_snapshot_bytes,
    decode_snapshot,
    measurement_point_id,
)
from watt_profile.contracts.lifecycle import (
    Lifecycle,
    LocalSession,
    LocalSourceBinding,
    LoggerStatus,
    PersistedBlock,
)
from watt_profile.contracts.upload import (
    MAX_BATCH_RECORDS,
    MAX_BODY_BYTES,
    InvalidConfirmation,
    UploadBatch,
    UploadConfirmation,
    validate_confirmation_body,
)
from watt_profile.logger.storage import schema as db
from watt_profile.logger.storage.database import valid_origin


def _time(value: datetime) -> str:
    return TypeAdapter(UtcTime).dump_python(
        TypeAdapter(UtcTime).validate_python(value), mode="json"
    )


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class LocalSettings(DomainModel):
    server_url: StrictStr | None = None
    serial_path: StrictStr | None = None
    profile_id: StrictStr = "dsmr5_nl"
    profile_revision: StrictInt = 1
    baudrate: Annotated[StrictInt, Field(gt=0)] = 115200
    bytesize: StrictInt = 8
    parity: StrictStr = "N"
    stopbits: DecimalValue = Decimal(1)
    timeout: DecimalValue = Decimal(1)

    @model_validator(mode="after")
    def settings(self) -> Self:
        if (
            self.profile_id != "dsmr5_nl"
            or self.profile_revision != 1
            or self.bytesize not in {5, 6, 7, 8}
            or self.parity not in {"N", "E", "O", "M", "S"}
            or not self.stopbits.is_finite()
            or self.stopbits not in {1, Decimal("1.5"), 2}
            or not self.timeout.is_finite()
            or self.timeout <= 0
            or self.stopbits.is_signed()
            or self.timeout.is_signed()
            or self.serial_path is not None
            and (not self.serial_path or "\x00" in self.serial_path)
            or self.server_url is not None
            and not valid_origin(self.server_url)
        ):
            raise ValueError("invalid_local_settings")
        return self

    def storage_values(self) -> dict[str, object]:
        result = self.model_dump()
        for name in ("stopbits", "timeout"):
            result[name] = format(result[name], "f")
        return result


class Site(DomainModel):
    site_id: UUID4
    address: Annotated[StrictStr, Field(max_length=500)] = ""
    description: Annotated[StrictStr, Field(max_length=2000)] = ""


class Installation(DomainModel):
    device_id: UUID4
    settings: LocalSettings
    lifecycle: Lifecycle
    last_successful_upload_at: UtcTime | None


class PendingGroup(DomainModel):
    batch: UploadBatch
    canonical_records: tuple[bytes, ...]
    body: bytes


def _projection(snapshot: Snapshot) -> dict[str, object]:
    content = snapshot.model_dump(mode="json")
    metrics = content.pop("metrics")
    content.update(metrics)
    content["fields"] = _json(content["fields"])
    content["quality"] = _json(content["quality"])
    encoded = canonical_snapshot_bytes(snapshot)
    content["canonical_snapshot"] = encoded.decode("utf-8")
    content["payload_sha256"] = hashlib.sha256(encoded).hexdigest()
    return content


_UNSET_SESSION = object()


class LoggerRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[Session]:
        with Session(self.engine) as session:
            # Explicit BEGIN also gives reads a coherent view with SQLite's legacy driver.
            session.connection().exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    def initialize_installation(self, device_id: UUID, now: datetime) -> Installation:
        descriptor = SessionDescriptor(
            session_id=device_id, device_id=device_id, site_id=device_id, started_at=now
        )
        with self._transaction(write=True) as session:
            if session.scalar(select(func.count()).select_from(db.devices)) or session.scalar(
                select(func.count()).select_from(db.config)
            ):
                raise ValueError("installation_inconsistent")
            session.execute(
                insert(db.devices).values(
                    device_id=str(descriptor.device_id), created_at=_time(now)
                )
            )
            session.execute(insert(db.config).values(device_id=str(descriptor.device_id)))
            return self._load_installation(session)

    def _load_installation(self, session: Session) -> Installation | None:
        configurations = session.execute(select(db.config)).mappings().all()
        devices = session.execute(select(db.devices)).mappings().all()
        if not configurations and not devices:
            return None
        if len(configurations) != 1 or len(devices) != 1:
            raise ValueError("installation_inconsistent")
        row = configurations[0]
        if row["singleton_id"] != 1 or row["device_id"] != devices[0]["device_id"]:
            raise ValueError("installation_inconsistent")
        # Creation timestamp is also a persisted contract value.
        _time(datetime.fromisoformat(devices[0]["created_at"].replace("Z", "+00:00")))
        settings = LocalSettings(**{name: row[name] for name in LocalSettings.model_fields})
        local_session = binding = None
        if row["active_session_id"] is not None:
            local_session, binding = self._descriptors(session, row["active_session_id"])
        blocks = tuple(json.loads(row["persisted_blocks"]))
        lifecycle = Lifecycle(
            device_id=row["device_id"],
            session=local_session,
            source=binding,
            persisted_blocks=blocks,
            collection_state="blocked" if blocks else "idle",
        )
        return Installation(
            device_id=row["device_id"],
            settings=settings,
            lifecycle=lifecycle,
            last_successful_upload_at=row["last_successful_upload_at"],
        )

    def _descriptors(
        self, session: Session, session_id: str
    ) -> tuple[LocalSession, LocalSourceBinding]:
        row = (
            session.execute(select(db.sessions).where(db.sessions.c.session_id == session_id))
            .mappings()
            .one()
        )
        source = (
            session.execute(select(db.sources).where(db.sources.c.session_id == session_id))
            .mappings()
            .one()
        )
        local = LocalSession(
            descriptor=SessionDescriptor(**{n: row[n] for n in SessionDescriptor.model_fields}),
            state=row["state"],
            ended_at=row["ended_at"],
            collection_enabled=bool(row["collection_enabled"]),
        )
        binding = LocalSourceBinding(
            descriptor=SourceDescriptor(**{n: source[n] for n in SourceDescriptor.model_fields}),
            meter_fingerprint=source["meter_fingerprint"],
        )
        point = (
            session.execute(
                select(db.points).where(
                    db.points.c.measurement_point_id == source["measurement_point_id"]
                )
            )
            .mappings()
            .one()
        )
        Site.model_validate(
            session.execute(select(db.sites).where(db.sites.c.site_id == row["site_id"]))
            .mappings()
            .one()
        )
        Lifecycle(device_id=row["device_id"], session=local, source=binding)
        if (
            source["site_id"] != row["site_id"]
            or point["site_id"] != row["site_id"]
            or point["kind"] != "grid-connection"
        ):
            raise ValueError("descriptor_mismatch")
        return local, binding

    def load_installation(self) -> Installation | None:
        with self._transaction() as session:
            return self._load_installation(session)

    def load_lifecycle(self) -> Lifecycle:
        installation = self.load_installation()
        if installation is None:
            raise ValueError("installation_required")
        return installation.lifecycle

    def save_settings(
        self, settings: LocalSettings, *, expected_session_id: UUID | None | object = _UNSET_SESSION
    ) -> LocalSettings:
        settings = LocalSettings.model_validate(settings.model_dump())
        with self._transaction(write=True) as session:
            installation = self._load_installation(session)
            if installation is None:
                raise ValueError("installation_required")
            selected = (
                installation.lifecycle.session.descriptor.session_id
                if installation.lifecycle.session
                else None
            )
            if expected_session_id is not _UNSET_SESSION and expected_session_id != selected:
                raise ValueError("stale_session")
            session.execute(update(db.config).values(**settings.storage_values()))
        return settings

    def save_site(self, site: Site, *, edit: bool = False) -> Site:
        site = Site.model_validate(site.model_dump())
        with self._transaction(write=True) as session:
            old = (
                session.execute(select(db.sites).where(db.sites.c.site_id == str(site.site_id)))
                .mappings()
                .one_or_none()
            )
            if old is None:
                session.execute(insert(db.sites).values(**site.model_dump(mode="json")))
                session.execute(
                    insert(db.points).values(
                        site_id=str(site.site_id),
                        measurement_point_id=str(measurement_point_id(site.site_id)),
                    )
                )
            elif edit:
                session.execute(
                    update(db.sites)
                    .where(db.sites.c.site_id == str(site.site_id))
                    .values(address=site.address, description=site.description)
                )
            else:
                site = Site.model_validate(old)
        return site

    def commit_deployment_change(
        self, expected_session_id: UUID | None, lifecycle: Lifecycle
    ) -> Lifecycle:
        # Runtime transition state is intentionally never persisted.
        lifecycle = Lifecycle.model_validate(
            {n: getattr(lifecycle, n) for n in Lifecycle.model_fields}
        )
        with self._transaction(write=True) as session:
            installation = self._load_installation(session)
            if installation is None or installation.device_id != lifecycle.device_id:
                raise ValueError("installation_inconsistent")
            previous = installation.lifecycle
            selected = previous.session.descriptor.session_id if previous.session else None
            if selected != expected_session_id:
                raise ValueError("stale_session")
            target = lifecycle.session
            if previous.session and (target is None or target.descriptor.session_id != selected):
                if target is None:
                    raise ValueError("session_required")
                if previous.session.state != "completed":
                    ended = target.descriptor.started_at
                    session.execute(
                        update(db.sessions)
                        .where(db.sessions.c.session_id == str(selected))
                        .values(state="completed", ended_at=_time(ended), collection_enabled=0)
                    )
            if target is not None:
                values = target.descriptor.model_dump(mode="json") | {
                    "state": target.state,
                    "ended_at": _time(target.ended_at) if target.ended_at else None,
                    "collection_enabled": int(target.collection_enabled),
                }
                existing = (
                    session.execute(
                        select(db.sessions).where(
                            db.sessions.c.session_id == str(target.descriptor.session_id)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing:
                    local, source = self._descriptors(session, str(target.descriptor.session_id))
                    if (
                        local.descriptor != target.descriptor
                        or source.descriptor != lifecycle.source.descriptor
                    ):
                        raise ValueError("descriptor_conflict")
                    if local.state == "completed" and target != local:
                        raise ValueError("completed_session")
                    session.execute(
                        update(db.sessions)
                        .where(db.sessions.c.session_id == str(target.descriptor.session_id))
                        .values(**values)
                    )
                    if source.meter_fingerprint != lifecycle.source.meter_fingerprint:
                        raise ValueError("fingerprint_conflict")
                else:
                    session.execute(insert(db.sessions).values(**values))
                    session.execute(
                        insert(db.sources).values(
                            **lifecycle.source.descriptor.model_dump(mode="json"),
                            site_id=str(target.descriptor.site_id),
                            meter_fingerprint=lifecycle.source.meter_fingerprint,
                        )
                    )
            # Preserve independently committed unresolved reasons.
            blocks = list(previous.persisted_blocks)
            if (
                selected != (target.descriptor.session_id if target else None)
                or target is not None
                and target.state == "completed"
            ):
                blocks = [b for b in blocks if b != "meter_changed"]
            for block in lifecycle.persisted_blocks:
                if block not in blocks:
                    blocks.append(block)
            session.execute(
                update(db.config).values(
                    active_session_id=str(target.descriptor.session_id) if target else None,
                    persisted_blocks=_json(blocks),
                )
            )
            return self._load_installation(session).lifecycle

    def bind_meter(self, expected_session_id: UUID, fingerprint: str | None) -> Lifecycle:
        with self._transaction(write=True) as session:
            installation = self._load_installation(session)
            state = installation.lifecycle if installation else None
            if (
                state is None
                or state.session is None
                or state.session.descriptor.session_id != expected_session_id
            ):
                raise ValueError("stale_session")
            candidate = LocalSourceBinding(
                descriptor=state.source.descriptor, meter_fingerprint=fingerprint
            )
            old = state.source.meter_fingerprint
            if old is None:
                session.execute(
                    update(db.sources)
                    .where(db.sources.c.session_id == str(expected_session_id))
                    .values(meter_fingerprint=candidate.meter_fingerprint)
                )
            elif fingerprint is not None and old != fingerprint:
                blocks = list(state.persisted_blocks)
                if "meter_changed" not in blocks:
                    blocks.append("meter_changed")
                session.execute(update(db.config).values(persisted_blocks=_json(blocks)))
            return self._load_installation(session).lifecycle

    def update_blocks(self, reason: PersistedBlock, *, blocked: bool) -> Lifecycle:
        if reason not in {"meter_changed", "storage_full"} or type(blocked) is not bool:
            raise ValueError("invalid_block")
        with self._transaction(write=True) as session:
            installation = self._load_installation(session)
            if installation is None:
                raise ValueError("installation_required")
            state = installation.lifecycle
            blocks = list(state.persisted_blocks)
            if blocked and reason not in blocks:
                blocks.append(reason)
            if not blocked:
                blocks = [b for b in blocks if b != reason]
                if reason == "storage_full" and state.session:
                    session.execute(
                        update(db.sessions)
                        .where(db.sessions.c.session_id == str(state.session.descriptor.session_id))
                        .values(collection_enabled=0)
                    )
            session.execute(update(db.config).values(persisted_blocks=_json(blocks)))
            return self._load_installation(session).lifecycle

    def _load_snapshot(self, session: Session, row) -> Snapshot:
        snapshot = decode_snapshot(row["canonical_snapshot"])
        if any(row[name] != value for name, value in _projection(snapshot).items()):
            raise ValueError("snapshot_projection_mismatch")
        local, source = self._descriptors(session, row["session_id"])
        snapshot.check_bindings(local.descriptor, source.descriptor)
        return snapshot

    def insert_snapshot(self, snapshot: Snapshot) -> bool:
        snapshot = decode_snapshot(canonical_snapshot_bytes(snapshot))
        values = _projection(snapshot)
        with self._transaction(write=True) as session:
            local, source = self._descriptors(session, str(snapshot.session_id))
            snapshot.check_bindings(local.descriptor, source.descriptor)
            existing = (
                session.execute(
                    select(db.snapshots).where(
                        db.snapshots.c.device_id == str(snapshot.device_id),
                        db.snapshots.c.record_id == str(snapshot.record_id),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing:
                self._load_snapshot(session, existing)
                if existing["canonical_snapshot"] != values["canonical_snapshot"]:
                    raise ValueError("record_conflict")
                return False
            session.execute(
                insert(db.snapshots).values(**values, upload_state="pending", uploaded_at=None)
            )
            return True

    def select_pending_group(
        self, limit: int = MAX_BATCH_RECORDS, byte_limit: int = MAX_BODY_BYTES
    ) -> PendingGroup | None:
        if (
            type(limit) is not int
            or not 1 <= limit <= MAX_BATCH_RECORDS
            or type(byte_limit) is not int
            or not 1 <= byte_limit <= MAX_BODY_BYTES
        ):
            raise ValueError("invalid_batch_limit")
        with self._transaction() as session:
            first = (
                session.execute(
                    select(db.snapshots)
                    .where(db.snapshots.c.upload_state == "pending")
                    .order_by(
                        db.snapshots.c.received_at,
                        db.snapshots.c.record_id,
                        db.snapshots.c.device_id,
                    )
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            if first is None:
                return None
            rows = session.execute(
                select(db.snapshots)
                .where(
                    db.snapshots.c.upload_state == "pending",
                    *[
                        db.snapshots.c[n] == first[n]
                        for n in (
                            "device_id",
                            "site_id",
                            "session_id",
                            "source_id",
                            "measurement_point_id",
                        )
                    ],
                )
                .order_by(db.snapshots.c.received_at, db.snapshots.c.record_id)
                .limit(limit)
            ).mappings()
            local, source = self._descriptors(session, first["session_id"])
            records, canonical = [], []
            chosen = None
            for row in rows:
                record = self._load_snapshot(session, row)
                batch = UploadBatch(
                    schema_version=1,
                    device_id=record.device_id,
                    site_id=record.site_id,
                    measurement_point_id=record.measurement_point_id,
                    session=local.descriptor,
                    source=source.descriptor,
                    records=(*records, record),
                )
                values = batch.model_dump(mode="json")
                # Inject the stored canonical texts directly into the bounded request body.
                texts = [*canonical, row["canonical_snapshot"].encode("utf-8")]
                values.pop("records")
                prefix = _json(values)[:-1].encode("utf-8")
                body = prefix + b',"records":[' + b",".join(texts) + b"]}"
                if len(body) > byte_limit:
                    if chosen is None:
                        raise ValueError("batch_too_large")
                    break
                records.append(record)
                canonical = texts
                chosen = PendingGroup(batch=batch, canonical_records=tuple(texts), body=body)
            return chosen

    def apply_confirmation(
        self, batch: UploadBatch, confirmation: UploadConfirmation | bytes, now: datetime
    ) -> int:
        body = (
            confirmation
            if isinstance(confirmation, bytes)
            else confirmation.model_dump_json().encode()
        )
        validate_confirmation_body(body, batch)
        batch = UploadBatch.model_validate(batch.model_dump())
        confirmed_at = _time(now)
        with self._transaction(write=True) as session:
            count = 0
            for record in batch.records:
                row = (
                    session.execute(
                        select(db.snapshots).where(
                            db.snapshots.c.device_id == str(batch.device_id),
                            db.snapshots.c.record_id == str(record.record_id),
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    raise InvalidConfirmation()
                stored = self._load_snapshot(session, row)
                local, source = self._descriptors(session, row["session_id"])
                if (
                    canonical_snapshot_bytes(stored) != canonical_snapshot_bytes(record)
                    or local.descriptor != batch.session
                    or source.descriptor != batch.source
                ):
                    raise InvalidConfirmation()
                if row["upload_state"] == "pending":
                    session.execute(
                        update(db.snapshots)
                        .where(
                            db.snapshots.c.device_id == str(batch.device_id),
                            db.snapshots.c.record_id == str(record.record_id),
                        )
                        .values(upload_state="uploaded", uploaded_at=confirmed_at)
                    )
                    count += 1
            if count:
                session.execute(
                    update(db.config)
                    .where(db.config.c.device_id == str(batch.device_id))
                    .values(last_successful_upload_at=confirmed_at)
                )
            return count

    def get_status(self, *, free_bytes: int = 0) -> LoggerStatus:
        with self._transaction() as session:
            installation = self._load_installation(session)
            state = installation.lifecycle if installation else None
            pending = session.execute(
                select(
                    func.count(),
                    func.coalesce(
                        func.sum(
                            func.length(func.cast(db.snapshots.c.canonical_snapshot, LargeBinary))
                        ),
                        0,
                    ),
                    func.min(db.snapshots.c.received_at),
                ).where(db.snapshots.c.upload_state == "pending")
            ).one()
            descriptor = state.session.descriptor if state and state.session else None
            source = state.source.descriptor if state and state.source else None
            return LoggerStatus(
                collection_state=state.collection_state if state else "idle",
                device_id=state.device_id if state else None,
                site_id=descriptor.site_id if descriptor else None,
                session_id=descriptor.session_id if descriptor else None,
                source_id=source.source_id if source else None,
                measurement_point_id=source.measurement_point_id if source else None,
                latest_received_at=session.scalar(select(func.max(db.snapshots.c.received_at))),
                latest_collection_error=None,
                last_upload_error=None,
                last_successful_upload_at=installation.last_successful_upload_at
                if installation
                else None,
                pending_count=pending[0],
                pending_bytes=pending[1],
                oldest_pending_received_at=pending[2],
                free_bytes=free_bytes,
                storage_blocked=bool(state and "storage_full" in state.persisted_blocks),
            )

    def active_session_latest_received_at(self) -> datetime | None:
        """Current deployment freshness, independent of all-session history."""
        with self._transaction() as session:
            installation = self._load_installation(session)
            state = installation.lifecycle if installation else None
            if state is None or state.session is None:
                return None
            value = session.scalar(
                select(func.max(db.snapshots.c.received_at)).where(
                    db.snapshots.c.session_id == str(state.session.descriptor.session_id)
                )
            )
            return TypeAdapter(UtcTime).validate_python(value) if value else None

    def list_sessions(self, limit: int = 100) -> tuple[LocalSession, ...]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid_session_limit")
        with self._transaction() as session:
            ids = session.scalars(
                select(db.sessions.c.session_id)
                .order_by(db.sessions.c.started_at.desc(), db.sessions.c.session_id)
                .limit(limit)
            ).all()
            return tuple(self._descriptors(session, session_id)[0] for session_id in ids)

    def cleanup_completed(self, session_ids: tuple[UUID, ...]) -> int:
        identifiers = [str(UUID4_validator(value)) for value in session_ids]
        with self._transaction(write=True) as session:
            completed = select(db.sessions.c.session_id).where(
                db.sessions.c.state == "completed", db.sessions.c.session_id.in_(identifiers)
            )
            result = session.execute(
                delete(db.snapshots).where(
                    db.snapshots.c.upload_state == "uploaded",
                    db.snapshots.c.session_id.in_(completed),
                )
            )
            return result.rowcount

    def check_safe_write(self, *, resolve_storage: bool = False) -> Lifecycle:
        """Commit a revalidated no-op config write, optionally clearing pressure atomically."""
        with self._transaction(write=True) as session:
            installation = self._load_installation(session)
            if installation is None:
                raise ValueError("installation_required")
            session.execute(update(db.config).values(singleton_id=1))
            if resolve_storage:
                blocks = [b for b in installation.lifecycle.persisted_blocks if b != "storage_full"]
                session.execute(update(db.config).values(persisted_blocks=_json(blocks)))
                if installation.lifecycle.session:
                    session.execute(
                        update(db.sessions)
                        .where(
                            db.sessions.c.session_id
                            == str(installation.lifecycle.session.descriptor.session_id)
                        )
                        .values(collection_enabled=0)
                    )
            return self._load_installation(session).lifecycle

    def cleanup_summary(self) -> tuple[dict[str, object], ...]:
        with self._transaction() as session:
            rows = session.execute(
                select(
                    db.sessions.c.session_id,
                    db.sessions.c.state,
                    func.count(db.snapshots.c.record_id).label("total_count"),
                    func.sum(case((db.snapshots.c.upload_state == "pending", 1), else_=0)).label(
                        "pending_count"
                    ),
                    func.sum(
                        case(
                            (
                                (db.sessions.c.state == "completed")
                                & (db.snapshots.c.upload_state == "uploaded"),
                                1,
                            ),
                            else_=0,
                        )
                    ).label("eligible_uploaded_count"),
                )
                .select_from(
                    db.sessions.outerjoin(
                        db.snapshots, db.snapshots.c.session_id == db.sessions.c.session_id
                    )
                )
                .group_by(db.sessions.c.session_id)
                .order_by(db.sessions.c.started_at)
            ).mappings()
            return tuple(dict(row) | {"session_id": UUID(row["session_id"])} for row in rows)

    def validate_all(self) -> Installation:
        """Revalidate every owner and canonical row, including inactive sessions."""
        with self._transaction() as session:
            installation = self._load_installation(session)
            if installation is None:
                raise ValueError("installation_required")
            for row in session.execute(select(db.sites)).mappings():
                Site.model_validate(row)
            for row in session.execute(select(db.points)).mappings():
                if (
                    row["measurement_point_id"] != str(measurement_point_id(UUID(row["site_id"])))
                    or row["kind"] != "grid-connection"
                ):
                    raise ValueError("descriptor_mismatch")
            ids = session.scalars(select(db.sessions.c.session_id)).all()
            if session.scalar(select(func.count()).select_from(db.sources)) != len(ids):
                raise ValueError("descriptor_mismatch")
            for identifier in ids:
                local, _ = self._descriptors(session, identifier)
                if local.descriptor.device_id != installation.device_id:
                    raise ValueError("descriptor_mismatch")
            for row in session.execute(select(db.snapshots)).mappings():
                self._load_snapshot(session, row)
                if row["uploaded_at"] is not None:
                    TypeAdapter(UtcTime).validate_python(row["uploaded_at"])
            return installation


def UUID4_validator(value: UUID) -> UUID:
    return Site(site_id=value).site_id
