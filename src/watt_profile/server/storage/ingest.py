"""Separate authenticated contact and atomic, locked PostgreSQL batch acceptance."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError, TimeoutError
from sqlalchemy.orm import Session

from watt_profile.contracts.upload import (
    UploadBatch,
    UploadConfirmation,
    UploadValidationError,
    validate_upload_body,
)
from watt_profile.server.ingest import AuthenticatedCredential
from watt_profile.server.storage import schema as db
from watt_profile.server.storage.repository import ServerRepository, acquire_mutation_locks


class IngestRepository(ServerRepository):
    @contextmanager
    def transaction(self) -> Iterator[Session]:
        try:
            with super().transaction() as session:
                yield session
        except (DBAPIError, TimeoutError):
            raise UploadValidationError("temporarily_unavailable") from None

    def authenticate_contact(self, token: str | None) -> AuthenticatedCredential:
        if not isinstance(token, str) or not token or not token.isascii():
            raise UploadValidationError("invalid_credential")
        digest = sha256(token.encode("ascii")).hexdigest()
        with self.transaction() as session:
            predicate = (
                db.credentials.c.token_sha256 == digest,
                db.credentials.c.disabled_at.is_(None),
            )
            row = session.execute(select(db.credentials).where(*predicate)).mappings().one_or_none()
            if row is None:
                raise UploadValidationError("invalid_credential")
            acquire_mutation_locks(session, device=[row["device_id"]])
            # A concurrent disable may have won the device lock after the initial lookup.
            row = session.execute(select(db.credentials).where(*predicate)).mappings().one_or_none()
            if row is None:
                raise UploadValidationError("invalid_credential")
            session.execute(
                update(db.devices)
                .where(db.devices.c.device_id == row["device_id"])
                .values(
                    last_authenticated_contact_at=func.greatest(
                        db.devices.c.last_authenticated_contact_at, datetime.now(UTC)
                    )
                )
            )
            credential = AuthenticatedCredential(
                credential_id=row["credential_id"], device_id=row["device_id"]
            )
        return credential

    def accept(self, credential: AuthenticatedCredential, batch: UploadBatch) -> UploadConfirmation:
        credential = AuthenticatedCredential.model_validate(credential.model_dump())
        try:
            # Validate Python types before JSON serializers can normalize invalid values.
            batch = UploadBatch.model_validate(batch.model_dump(mode="python", warnings=False))
            batch = validate_upload_body(batch.model_dump_json().encode("utf-8"))
        except ValidationError as exc:
            reasons = {str(item.get("ctx", {}).get("error", "")) for item in exc.errors()}
            code = "invalid_payload"
            if reasons & {"batch_too_large", "snapshot_too_large"}:
                code = "batch_too_large"
            elif "unsupported_schema" in reasons:
                code = "unsupported_schema"
            raise UploadValidationError(code) from None
        except UploadValidationError:
            raise
        except (ValueError, TypeError, RecursionError):
            raise UploadValidationError("invalid_payload") from None
        with self.transaction() as session:
            acquire_mutation_locks(
                session,
                device=[credential.device_id, batch.device_id],
                site=[batch.site_id],
                session=[batch.session.session_id],
                source=[batch.source.source_id],
            )
            enabled = session.execute(
                select(db.credentials.c.credential_id).where(
                    db.credentials.c.credential_id == credential.credential_id,
                    db.credentials.c.device_id == credential.device_id,
                    db.credentials.c.disabled_at.is_(None),
                )
            ).first()
            if enabled is None:
                raise UploadValidationError("invalid_credential")
            if batch.device_id != credential.device_id:
                raise UploadValidationError("device_mismatch")
            granted = session.execute(
                select(db.authorizations.c.site_id).where(
                    db.authorizations.c.device_id == credential.device_id,
                    db.authorizations.c.site_id == batch.site_id,
                )
            ).first()
            if granted is None:
                raise UploadValidationError("site_not_authorized")
            # A different source for an already bound session is a descriptor conflict,
            # before the final UNIQUE(session_id) constraint can reject its insertion.
            bound_source = session.scalar(
                select(db.sources.c.source_id).where(
                    db.sources.c.session_id == batch.session.session_id
                )
            )
            if bound_source is not None and bound_source != batch.source.source_id:
                raise UploadValidationError("descriptor_conflict")
            try:
                self.insert_descriptors(session, batch.session, batch.source)
                now = datetime.now(UTC)
                for record in batch.records:
                    self.insert_snapshot(session, record, now)
            except ValueError as exc:
                code = str(exc)
                if code not in {"descriptor_conflict", "record_conflict"}:
                    code = "temporarily_unavailable"
                raise UploadValidationError(code) from None
        # Leaving the owned context commits first. No confirmation escapes before commit.
        return UploadConfirmation(
            schema_version=1,
            device_id=batch.device_id,
            confirmed_record_ids=tuple(record.record_id for record in batch.records),
        )
