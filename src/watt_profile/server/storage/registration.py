"""Atomic PostgreSQL registration and access mutations under shared advisory locks."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError, TimeoutError
from sqlalchemy.orm import Session

from watt_profile.contracts.domain import UUID4
from watt_profile.server.registration import (
    Credential,
    CredentialInput,
    Device,
    DeviceDetail,
    DeviceInput,
    DevicePatch,
    Grant,
    InvalidRegistration,
    ObjectNotFound,
    Page,
    PageOptions,
    RegistrationActions,
    RegistrationConflict,
    RegistrationResult,
    RegistrationSummary,
    SiteInput,
    SitePatch,
    SiteSearch,
    validate_summary,
)
from watt_profile.server.storage import schema as db
from watt_profile.server.storage.repository import ServerRepository, acquire_mutation_locks


class StorageUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("storage_unavailable")


def _id(value: UUID) -> UUID:
    return TypeAdapter(UUID4).validate_python(value)


def _credential(row) -> Credential:
    return Credential.model_validate({name: row[name] for name in Credential.model_fields})


class RegistrationRepository(ServerRepository):
    @contextmanager
    def transaction(self) -> Iterator[Session]:
        try:
            with super().transaction() as session:
                yield session
        except IntegrityError as exc:
            if (
                getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
                == "uq_credentials_token_hash"
            ):
                raise RegistrationConflict() from None
            raise StorageUnavailable() from None
        except (DBAPIError, TimeoutError):
            raise StorageUnavailable() from None

    def _site(self, session: Session, site_id: UUID) -> SiteInput | None:
        row = (
            session.execute(select(db.sites).where(db.sites.c.site_id == site_id))
            .mappings()
            .one_or_none()
        )
        return SiteInput.model_validate(dict(row)) if row is not None else None

    def _device(self, session: Session, device_id: UUID) -> Device | None:
        row = (
            session.execute(select(db.devices).where(db.devices.c.device_id == device_id))
            .mappings()
            .one_or_none()
        )
        return Device.model_validate(dict(row)) if row is not None else None

    def _matching_credential(self, session: Session, device_id: UUID, digest: str):
        """Any prior credential prevents implicit rotation, even if it was disabled."""
        rows = (
            session.execute(select(db.credentials).where(db.credentials.c.device_id == device_id))
            .mappings()
            .all()
        )
        matching = (
            session.execute(select(db.credentials).where(db.credentials.c.token_sha256 == digest))
            .mappings()
            .one_or_none()
        )
        if matching is not None and (
            matching["device_id"] != device_id or matching["disabled_at"] is not None
        ):
            raise RegistrationConflict()
        if rows and matching is None:
            raise RegistrationConflict()
        return matching

    def _evaluate(self, session: Session, summary: RegistrationSummary) -> RegistrationResult:
        site = self._site(session, summary.site_id)
        device = self._device(session, summary.device_id)
        credential = self._matching_credential(
            session, summary.device_id, sha256(summary.token.encode("ascii")).hexdigest()
        )
        grant = session.execute(
            select(db.authorizations).where(
                db.authorizations.c.device_id == summary.device_id,
                db.authorizations.c.site_id == summary.site_id,
            )
        ).first()
        return RegistrationResult(
            site=site
            or SiteInput(
                site_id=summary.site_id, address=summary.address, description=summary.description
            ),
            device_id=summary.device_id,
            credential_id=credential["credential_id"] if credential is not None else None,
            actions=RegistrationActions(
                site="reuse" if site else "create",
                device="reuse" if device else "create",
                credential="reuse" if credential else "create",
                grant="reuse" if grant else "create",
            ),
            metadata_differs=site is not None
            and (site.address != summary.address or site.description != summary.description),
        )

    def preview(self, summary: RegistrationSummary) -> RegistrationResult:
        summary = validate_summary(summary)
        with self.transaction() as session:
            return self._evaluate(session, summary)

    def commit(self, summary: RegistrationSummary) -> RegistrationResult:
        summary = validate_summary(summary)
        with self.transaction() as session:
            acquire_mutation_locks(session, device=[summary.device_id], site=[summary.site_id])
            result = self._evaluate(session, summary)
            now = datetime.now(UTC)
            if result.actions.site == "create":
                session.execute(insert(db.sites).values(**result.site.model_dump()))
            if result.actions.device == "create":
                session.execute(
                    insert(db.devices).values(device_id=summary.device_id, created_at=now)
                )
            credential_id = result.credential_id
            if result.actions.credential == "create":
                credential_id = uuid4()
                session.execute(
                    insert(db.credentials).values(
                        credential_id=credential_id,
                        device_id=summary.device_id,
                        token_sha256=sha256(summary.token.encode("ascii")).hexdigest(),
                        created_at=now,
                    )
                )
            if result.actions.grant == "create":
                session.execute(
                    insert(db.authorizations).values(
                        device_id=summary.device_id, site_id=summary.site_id
                    )
                )
            return RegistrationResult(**{**result.model_dump(), "credential_id": credential_id})

    def create_site(self, site: SiteInput) -> SiteInput:
        site = SiteInput.model_validate(site.model_dump())
        with self.transaction() as session:
            acquire_mutation_locks(session, site=[site.site_id])
            existing = self._site(session, site.site_id)
            if existing is not None:
                return existing
            session.execute(insert(db.sites).values(**site.model_dump()))
            return site

    def edit_site(self, site_id: UUID, patch: SitePatch) -> SiteInput:
        site_id = _id(site_id)
        values = SitePatch.model_validate(patch.model_dump(exclude_unset=True)).changes()
        with self.transaction() as session:
            acquire_mutation_locks(session, site=[site_id])
            if self._site(session, site_id) is None:
                raise ObjectNotFound()
            session.execute(update(db.sites).where(db.sites.c.site_id == site_id).values(**values))
            return self._site(session, site_id)

    def get_site(self, site_id: UUID) -> SiteInput:
        with self.transaction() as session:
            result = self._site(session, _id(site_id))
            if result is None:
                raise ObjectNotFound()
            return result

    def site_page(self, search: SiteSearch = SiteSearch()) -> Page[SiteInput]:
        search = SiteSearch.model_validate(search.model_dump())
        search.predicate()
        conditions = []
        if search.site_id is not None:
            conditions.append(db.sites.c.site_id == search.site_id)
        if search.q is not None:
            conditions.append(db.sites.c.address.icontains(search.q, autoescape=True))
        with self.transaction() as session:
            total = session.scalar(select(func.count()).select_from(db.sites).where(*conditions))
            rows = session.execute(
                select(db.sites)
                .where(*conditions)
                .order_by(db.sites.c.site_id)
                .offset(search.offset)
                .limit(search.limit)
            ).mappings()
            return Page[SiteInput](
                items=tuple(SiteInput.model_validate(dict(row)) for row in rows), total=total
            )

    def create_device(self, device: DeviceInput) -> Device:
        device = DeviceInput.model_validate(device.model_dump())
        with self.transaction() as session:
            acquire_mutation_locks(session, device=[device.device_id])
            existing = self._device(session, device.device_id)
            if existing is not None:
                return existing
            session.execute(
                insert(db.devices).values(device_id=device.device_id, created_at=datetime.now(UTC))
            )
            return self._device(session, device.device_id)

    def edit_device(self, device_id: UUID, patch: DevicePatch) -> Device:
        device_id = _id(device_id)
        patch = DevicePatch.model_validate(patch.model_dump())
        with self.transaction() as session:
            acquire_mutation_locks(session, device=[device_id])
            if self._device(session, device_id) is None:
                raise ObjectNotFound()
            session.execute(
                update(db.devices)
                .where(db.devices.c.device_id == device_id)
                .values(**patch.model_dump())
            )
            return self._device(session, device_id)

    def device_page(self, options: PageOptions = PageOptions()) -> Page[Device]:
        options = PageOptions.model_validate(options.model_dump())
        with self.transaction() as session:
            total = session.scalar(select(func.count()).select_from(db.devices))
            rows = session.execute(
                select(db.devices)
                .order_by(db.devices.c.device_id)
                .offset(options.offset)
                .limit(options.limit)
            ).mappings()
            return Page[Device](
                items=tuple(Device.model_validate(dict(row)) for row in rows), total=total
            )

    def get_device(self, device_id: UUID) -> DeviceDetail:
        device_id = _id(device_id)
        with self.transaction() as session:
            device = self._device(session, device_id)
            if device is None:
                raise ObjectNotFound()
            credentials = session.execute(
                select(db.credentials)
                .where(db.credentials.c.device_id == device_id)
                .order_by(db.credentials.c.credential_id)
            ).mappings()
            sites = session.execute(
                select(db.sites)
                .join(db.authorizations, db.sites.c.site_id == db.authorizations.c.site_id)
                .where(db.authorizations.c.device_id == device_id)
                .order_by(db.sites.c.site_id)
            ).mappings()
            return DeviceDetail(
                device=device,
                credentials=tuple(_credential(row) for row in credentials),
                sites=tuple(SiteInput.model_validate(dict(row)) for row in sites),
            )

    def register_credential(self, device_id: UUID, token: str) -> Credential:
        device_id = _id(device_id)
        try:
            token = CredentialInput.model_validate({"token": token}).token
        except ValidationError:
            raise InvalidRegistration() from None
        digest = sha256(token.encode("ascii")).hexdigest()
        with self.transaction() as session:
            acquire_mutation_locks(session, device=[device_id])
            if self._device(session, device_id) is None:
                raise ObjectNotFound()
            existing = self._matching_credential(session, device_id, digest)
            if existing is not None:
                return _credential(existing)
            credential = Credential(
                credential_id=uuid4(),
                device_id=device_id,
                created_at=datetime.now(UTC),
                disabled_at=None,
            )
            session.execute(
                insert(db.credentials).values(**credential.model_dump(), token_sha256=digest)
            )
            return credential

    def disable_credential(self, device_id: UUID, credential_id: UUID) -> Credential:
        device_id, credential_id = _id(device_id), _id(credential_id)
        with self.transaction() as session:
            acquire_mutation_locks(session, device=[device_id])
            row = (
                session.execute(
                    select(db.credentials).where(
                        db.credentials.c.device_id == device_id,
                        db.credentials.c.credential_id == credential_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ObjectNotFound()
            if row["disabled_at"] is None:
                session.execute(
                    update(db.credentials)
                    .where(db.credentials.c.credential_id == credential_id)
                    .values(disabled_at=datetime.now(UTC))
                )
                row = (
                    session.execute(
                        select(db.credentials).where(
                            db.credentials.c.credential_id == credential_id
                        )
                    )
                    .mappings()
                    .one()
                )
            return _credential(row)

    def _grant_objects(self, session: Session, device_id: UUID, site_id: UUID):
        if self._device(session, device_id) is None or self._site(session, site_id) is None:
            raise ObjectNotFound()

    def grant_site(self, device_id: UUID, site_id: UUID) -> Grant:
        device_id, site_id = _id(device_id), _id(site_id)
        with self.transaction() as session:
            acquire_mutation_locks(session, device=[device_id], site=[site_id])
            self._grant_objects(session, device_id, site_id)
            existing = session.execute(
                select(db.authorizations).where(
                    db.authorizations.c.device_id == device_id,
                    db.authorizations.c.site_id == site_id,
                )
            ).first()
            if existing is None:
                session.execute(
                    insert(db.authorizations).values(device_id=device_id, site_id=site_id)
                )
            return Grant(device_id=device_id, site_id=site_id)

    def revoke_site(self, device_id: UUID, site_id: UUID) -> None:
        device_id, site_id = _id(device_id), _id(site_id)
        with self.transaction() as session:
            acquire_mutation_locks(session, device=[device_id], site=[site_id])
            self._grant_objects(session, device_id, site_id)
            session.execute(
                delete(db.authorizations).where(
                    db.authorizations.c.device_id == device_id,
                    db.authorizations.c.site_id == site_id,
                )
            )
