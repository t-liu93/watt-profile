"""HTTP-independent upload orchestration with fixed M1 errors and confirmations."""

from typing import Protocol

from watt_profile.contracts.domain import UUID4, DomainModel
from watt_profile.contracts.upload import UploadBatch, UploadConfirmation, validate_upload_body


class AuthenticatedCredential(DomainModel):
    credential_id: UUID4
    device_id: UUID4


class IngestStorage(Protocol):
    def authenticate_contact(self, token: str | None) -> AuthenticatedCredential: ...

    def accept(
        self, credential: AuthenticatedCredential, batch: UploadBatch
    ) -> UploadConfirmation: ...


class IngestService:
    def __init__(self, repository: IngestStorage):
        self.repository = repository

    def authenticate_contact(self, token: str | None) -> AuthenticatedCredential:
        """Also usable before a transport rejects media type or a bounded body read."""
        return self.repository.authenticate_contact(token)

    def accept_body(
        self,
        credential: AuthenticatedCredential,
        body: bytes,
        *,
        content_encoding: str | None = None,
    ) -> UploadConfirmation:
        batch = validate_upload_body(body, content_encoding=content_encoding)
        return self.repository.accept(credential, batch)

    def ingest(
        self, token: str | None, body: bytes, *, content_encoding: str | None = None
    ) -> UploadConfirmation:
        credential = self.authenticate_contact(token)
        return self.accept_body(credential, body, content_encoding=content_encoding)
