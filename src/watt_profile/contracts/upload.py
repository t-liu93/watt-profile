"""Pure C6 transport validation; no authentication, persistence or networking."""

from __future__ import annotations

import json
from typing import Self

from pydantic import StrictBool, StrictInt, StrictStr, ValidationError, model_validator

from watt_profile.contracts.domain import (
    UUID4,
    UUID5,
    DomainModel,
    SessionDescriptor,
    Snapshot,
    SourceDescriptor,
    canonical_snapshot_bytes,
    measurement_point_id,
)

MAX_BATCH_RECORDS = 200
MAX_BODY_BYTES = 4_194_304

# Only these application-generated errors may cross the transport boundary.
ERROR_CONTRACTS = {
    "invalid_credential": (401, "Upload credential is invalid or disabled.", False),
    "device_mismatch": (403, "Device does not match the authenticated credential.", False),
    "site_not_authorized": (403, "Device is not authorized for this site.", False),
    "record_conflict": (409, "Record identity conflicts with immutable content.", False),
    "descriptor_conflict": (409, "Descriptor conflicts with immutable metadata.", False),
    "batch_too_large": (413, "Batch exceeds a transport limit.", False),
    "invalid_payload": (422, "Payload does not satisfy the upload contract.", False),
    "unsupported_schema": (422, "Schema version is not supported.", False),
    "temporarily_unavailable": (503, "Service is temporarily unavailable.", True),
}


class UploadErrorDetail(DomainModel):
    code: StrictStr
    message: StrictStr
    retryable: StrictBool

    @model_validator(mode="after")
    def fixed_contract(self) -> Self:
        expected = ERROR_CONTRACTS.get(self.code)
        if expected is None or (self.message, self.retryable) != expected[1:]:
            raise ValueError("invalid_error_contract")
        return self


class UploadErrorResponse(DomainModel):
    schema_version: StrictInt = 1
    error: UploadErrorDetail

    @model_validator(mode="after")
    def version(self) -> Self:
        if self.schema_version != 1:
            raise ValueError("unsupported_schema")
        return self


def error_response(code: str) -> UploadErrorResponse:
    """Build fixed safe text, never incorporating received values."""
    _, message, retryable = ERROR_CONTRACTS[code]
    return UploadErrorResponse(
        error=UploadErrorDetail(code=code, message=message, retryable=retryable)
    )


class UploadValidationError(ValueError):
    """Safe application rejection, with no nested Pydantic errors or input."""

    def __init__(self, code: str):
        self.response = error_response(code)
        self.status_code = ERROR_CONTRACTS[code][0]
        super().__init__(code)


class UploadBatch(DomainModel):
    schema_version: StrictInt
    device_id: UUID4
    site_id: UUID4
    measurement_point_id: UUID5
    session: SessionDescriptor
    source: SourceDescriptor
    records: tuple[Snapshot, ...]

    @model_validator(mode="before")
    @classmethod
    def transport_bounds(cls, value: object) -> object:
        if isinstance(value, dict):
            version = value.get("schema_version")
            if type(version) is int and version != 1:
                raise ValueError("unsupported_schema")
            records = value.get("records")
            if isinstance(records, (list, tuple)) and len(records) > MAX_BATCH_RECORDS:
                raise ValueError("batch_too_large")
        return value

    @model_validator(mode="after")
    def ownership(self) -> Self:
        if self.schema_version != 1:
            raise ValueError("unsupported_schema")
        if not self.records:
            raise ValueError("empty_batch")
        if len(self.records) > MAX_BATCH_RECORDS:
            raise ValueError("batch_too_large")
        if (
            (self.device_id, self.site_id) != (self.session.device_id, self.session.site_id)
            or self.measurement_point_id != measurement_point_id(self.site_id)
            or self.source.session_id != self.session.session_id
            or self.source.measurement_point_id != self.measurement_point_id
        ):
            raise ValueError("descriptor_mismatch")
        ids = set()
        for record in self.records:
            record.check_bindings(self.session, self.source)
            # Reuse the canonical codec; also revalidate preconstructed model instances.
            canonical_snapshot_bytes(record)
            if record.record_id in ids:
                raise ValueError("repeated_record_id")
            ids.add(record.record_id)
        return self


class UploadConfirmation(DomainModel):
    schema_version: StrictInt
    device_id: UUID4
    confirmed_record_ids: tuple[UUID4, ...]

    @model_validator(mode="after")
    def complete_shape(self) -> Self:
        if self.schema_version != 1:
            raise ValueError("unsupported_schema")
        if not self.confirmed_record_ids or len(set(self.confirmed_record_ids)) != len(
            self.confirmed_record_ids
        ):
            raise ValueError("invalid_confirmation")
        return self


class InvalidConfirmation(ValueError):
    def __init__(self):
        super().__init__("invalid_confirmation")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _json_body(body: bytes) -> object:
    if type(body) is not bytes:
        raise ValueError("bytes_required")
    if len(body) > MAX_BODY_BYTES:
        raise UploadValidationError("batch_too_large")
    return json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)


def validate_upload_body(body: bytes, *, content_encoding: str | None = None) -> UploadBatch:
    """Check actual incoming bytes before JSON parsing; reject compressed bodies.

    A future endpoint must bound its read as well. This function receives already-read
    bytes and intentionally performs no stream I/O or credential checks.
    """
    try:
        if type(body) is bytes and len(body) > MAX_BODY_BYTES:
            raise UploadValidationError("batch_too_large")
        if content_encoding is not None and content_encoding.strip().lower() != "identity":
            raise UploadValidationError("invalid_payload")
        return UploadBatch.model_validate(_json_body(body))
    except UploadValidationError:
        raise
    except ValidationError as exc:
        reasons = {str(item.get("ctx", {}).get("error", "")) for item in exc.errors()}
        code = "invalid_payload"
        if reasons & {"batch_too_large", "snapshot_too_large"}:
            code = "batch_too_large"
        elif "unsupported_schema" in reasons:
            code = "unsupported_schema"
        raise UploadValidationError(code) from None
    except (ValueError, TypeError, RecursionError):
        raise UploadValidationError("invalid_payload") from None


def validate_confirmation_body(body: bytes, batch: UploadBatch) -> UploadConfirmation:
    """Validate the entire confirmation before a caller acknowledges any local row."""
    try:
        batch = UploadBatch.model_validate(batch.model_dump())
        confirmation = UploadConfirmation.model_validate(_json_body(body))
        if confirmation.device_id != batch.device_id or set(confirmation.confirmed_record_ids) != {
            record.record_id for record in batch.records
        }:
            raise InvalidConfirmation()
        return confirmation
    except (ValueError, TypeError, RecursionError):
        raise InvalidConfirmation() from None
