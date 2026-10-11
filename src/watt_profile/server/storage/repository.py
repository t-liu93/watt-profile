"""HTTP-independent typed storage primitives with short owned transactions."""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
from uuid import UUID

from pydantic import StrictStr, TypeAdapter
from sqlalchemy import Engine, insert, select, text
from sqlalchemy.orm import Session

from watt_profile.contracts.domain import (
    UUID4,
    DomainModel,
    SessionDescriptor,
    Snapshot,
    SourceDescriptor,
    UtcTime,
    canonical_snapshot_bytes,
    decode_snapshot,
    measurement_point_id,
)
from watt_profile.server.storage import schema as db

MUTATION_NAMESPACE = 1465078856
LOCK_CLASSES = ("device", "site", "session", "source")


class Site(DomainModel):
    site_id: UUID4
    address: StrictStr = ""
    description: StrictStr = ""


class StoredSnapshot(DomainModel):
    snapshot: Snapshot
    ingested_at: UtcTime
    canonical: bytes
    payload_sha256: StrictStr


def acquire_mutation_locks(session: Session, /, **identities: Sequence[UUID]) -> None:
    """Take stable transaction locks in the shared device/site/session/source order."""
    if set(identities) - set(LOCK_CLASSES):
        raise ValueError("unknown_lock_class")
    for kind in LOCK_CLASSES:
        for identifier in sorted(set(identities.get(kind, ()))):
            digest = sha256(kind.encode("ascii") + identifier.bytes).digest()
            key = int.from_bytes(digest[:4], "big", signed=True)
            session.execute(
                text("SELECT pg_advisory_xact_lock(:namespace, :key)"),
                {"namespace": MUTATION_NAMESPACE, "key": key},
            )


def snapshot_values(snapshot: Snapshot, ingested_at: datetime) -> dict[str, object]:
    """Revalidate even constructed/mutated models before producing every projection."""
    snapshot = Snapshot.model_validate(snapshot.model_dump())
    canonical = canonical_snapshot_bytes(snapshot)
    values = snapshot.model_dump()
    values.update(values.pop("metrics"))
    json_content = snapshot.model_dump(mode="json")
    values["fields"] = json_content["fields"]
    values["quality"] = json_content["quality"]
    values["canonical_snapshot"] = canonical.decode("utf-8")
    values["payload_sha256"] = sha256(canonical).hexdigest()
    values["ingested_at"] = TypeAdapter(UtcTime).validate_python(ingested_at)
    return values


def load_snapshot(row: Mapping[str, object]) -> StoredSnapshot:
    """Reject corrupted canonical text, hash or any flattened/JSONB projection."""
    snapshot = decode_snapshot(row["canonical_snapshot"])
    expected = snapshot_values(snapshot, row["ingested_at"])
    if any(row[name] != value for name, value in expected.items()):
        raise ValueError("snapshot_projection_mismatch")
    return StoredSnapshot(
        snapshot=snapshot,
        ingested_at=row["ingested_at"],
        canonical=row["canonical_snapshot"].encode("utf-8"),
        payload_sha256=row["payload_sha256"],
    )


class ServerRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """Service implementations compose primitives inside a single owned transaction."""
        with Session(self.engine) as session, session.begin():
            yield session

    def load_descriptors(
        self, session: Session, session_id: UUID
    ) -> tuple[SessionDescriptor, SourceDescriptor]:
        session_row = (
            session.execute(select(db.sessions).where(db.sessions.c.session_id == session_id))
            .mappings()
            .one()
        )
        source_row = (
            session.execute(select(db.sources).where(db.sources.c.session_id == session_id))
            .mappings()
            .one()
        )
        descriptor = SessionDescriptor.model_validate(dict(session_row))
        source = SourceDescriptor.model_validate(
            {name: source_row[name] for name in SourceDescriptor.model_fields}
        )
        point = (
            session.execute(
                select(db.points).where(
                    db.points.c.measurement_point_id == source.measurement_point_id
                )
            )
            .mappings()
            .one()
        )
        if (
            source_row["site_id"] != descriptor.site_id
            or point["site_id"] != descriptor.site_id
            or point["kind"] != "grid-connection"
            or source.measurement_point_id != measurement_point_id(descriptor.site_id)
        ):
            raise ValueError("descriptor_mismatch")
        return descriptor, source

    def insert_descriptors(
        self, session: Session, descriptor: SessionDescriptor, source: SourceDescriptor
    ) -> None:
        descriptor = SessionDescriptor.model_validate(descriptor.model_dump())
        source = SourceDescriptor.model_validate(source.model_dump())
        if (
            source.session_id != descriptor.session_id
            or source.measurement_point_id != measurement_point_id(descriptor.site_id)
        ):
            raise ValueError("descriptor_mismatch")
        point = (
            session.execute(
                select(db.points).where(
                    db.points.c.measurement_point_id == source.measurement_point_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if point is None:
            session.execute(
                insert(db.points).values(
                    measurement_point_id=source.measurement_point_id, site_id=descriptor.site_id
                )
            )
        elif point["site_id"] != descriptor.site_id or point["kind"] != "grid-connection":
            raise ValueError("descriptor_conflict")
        existing = (
            session.execute(
                select(db.sessions).where(db.sessions.c.session_id == descriptor.session_id)
            )
            .mappings()
            .one_or_none()
        )
        if existing is None:
            session.execute(insert(db.sessions).values(**descriptor.model_dump()))
        elif SessionDescriptor.model_validate(dict(existing)) != descriptor:
            raise ValueError("descriptor_conflict")
        existing_source = (
            session.execute(select(db.sources).where(db.sources.c.source_id == source.source_id))
            .mappings()
            .one_or_none()
        )
        if existing_source is None:
            session.execute(
                insert(db.sources).values(**source.model_dump(), site_id=descriptor.site_id)
            )
        else:
            loaded = SourceDescriptor.model_validate(
                {name: existing_source[name] for name in SourceDescriptor.model_fields}
            )
            if loaded != source or existing_source["site_id"] != descriptor.site_id:
                raise ValueError("descriptor_conflict")
        if self.load_descriptors(session, descriptor.session_id) != (descriptor, source):
            raise ValueError("descriptor_conflict")

    def insert_snapshot(self, session: Session, snapshot: Snapshot, ingested_at: datetime) -> bool:
        values = snapshot_values(snapshot, ingested_at)
        snapshot = decode_snapshot(values["canonical_snapshot"])
        snapshot.check_bindings(*self.load_descriptors(session, snapshot.session_id))
        existing = (
            session.execute(
                select(db.snapshots).where(
                    db.snapshots.c.device_id == snapshot.device_id,
                    db.snapshots.c.record_id == snapshot.record_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            stored = load_snapshot(existing)
            if stored.payload_sha256 != values["payload_sha256"]:
                raise ValueError("record_conflict")
            return False
        session.execute(insert(db.snapshots).values(**values))
        return True

    def get_snapshot(self, device_id: UUID, record_id: UUID) -> StoredSnapshot | None:
        with self.transaction() as session:
            row = (
                session.execute(
                    select(db.snapshots).where(
                        db.snapshots.c.device_id == device_id, db.snapshots.c.record_id == record_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            return load_snapshot(row) if row is not None else None

    def list_sites(self) -> tuple[Site, ...]:
        with self.transaction() as session:
            return tuple(
                Site.model_validate(dict(row))
                for row in session.execute(select(db.sites).order_by(db.sites.c.site_id)).mappings()
            )
