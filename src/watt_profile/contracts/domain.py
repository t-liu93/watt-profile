"""Validated, immutable shared domain values and the canonical snapshot codec."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4, uuid5

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

from watt_profile.p1.parser import sanitize_field_values
from watt_profile.p1.profile import MappedReading

MAX_SNAPSHOT_BYTES = 16_384
_OBIS = re.compile(r"[0-9]+-[0-9]+:[0-9]+\.[0-9]+\.[0-9]+\Z")
_PLAIN = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")


def _uuid(value: object, version: int) -> UUID:
    if type(value) is str:
        parsed = UUID(value)
        if str(parsed) != value:
            raise ValueError("canonical_uuid_required")
        value = parsed
    if not isinstance(value, UUID) or value.version != version:
        raise ValueError("invalid_uuid_version")
    return value


UUID4 = Annotated[UUID, BeforeValidator(lambda value: _uuid(value, 4))]
UUID5 = Annotated[UUID, BeforeValidator(lambda value: _uuid(value, 5))]


def new_id() -> UUID:
    """Create a fresh offline v4 identity."""
    return uuid4()


def measurement_point_id(site_id: UUID | str) -> UUID:
    return uuid5(_uuid(site_id, 4), "grid-connection")


def _utc(value: object) -> datetime:
    if type(value) is str:
        if "T" not in value:
            raise ValueError("invalid_datetime")
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware_datetime_required")
    return value.astimezone(UTC)


UtcTime = Annotated[
    datetime,
    BeforeValidator(_utc),
    PlainSerializer(
        lambda value: value.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        when_used="json",
    ),
]


def _decimal(value: object) -> Decimal:
    if type(value) is str and _PLAIN.fullmatch(value):
        value = Decimal(value)
    if not isinstance(value, Decimal) or not value.is_finite() or value.is_signed():
        raise ValueError("nonnegative_plain_decimal_required")
    return value


DecimalValue = Annotated[
    Decimal,
    BeforeValidator(_decimal),
    PlainSerializer(lambda value: format(value, "f"), when_used="json"),
]


class DomainModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, validate_default=True, hide_input_in_errors=True
    )


class SessionDescriptor(DomainModel):
    session_id: UUID4
    device_id: UUID4
    site_id: UUID4
    started_at: UtcTime


class SourceDescriptor(DomainModel):
    source_id: UUID4
    session_id: UUID4
    measurement_point_id: UUID5
    kind: Literal["p1_serial"] = "p1_serial"
    profile_id: Literal["dsmr5_nl"] = "dsmr5_nl"
    profile_revision: StrictInt = 1

    @model_validator(mode="after")
    def revision(self) -> Self:
        if self.profile_revision != 1:
            raise ValueError("unsupported_profile_revision")
        return self


class Metrics(DomainModel):
    energy_import_t1_kwh: DecimalValue | None
    energy_import_t2_kwh: DecimalValue | None
    energy_export_t1_kwh: DecimalValue | None
    energy_export_t2_kwh: DecimalValue | None
    power_import_kw: DecimalValue | None
    power_export_kw: DecimalValue | None
    voltage_l1_v: DecimalValue | None
    voltage_l2_v: DecimalValue | None
    voltage_l3_v: DecimalValue | None
    current_l1_a: DecimalValue | None
    current_l2_a: DecimalValue | None
    current_l3_a: DecimalValue | None
    power_import_l1_kw: DecimalValue | None
    power_import_l2_kw: DecimalValue | None
    power_import_l3_kw: DecimalValue | None
    power_export_l1_kw: DecimalValue | None
    power_export_l2_kw: DecimalValue | None
    power_export_l3_kw: DecimalValue | None


IssueReason = Literal[
    "missing",
    "invalid_value",
    "invalid_unit",
    "duplicate",
    "redacted",
    "invalid_timestamp",
    "clock_offset_mismatch",
]


class FieldIssue(DomainModel):
    code: StrictStr | None
    reason: IssueReason

    @model_validator(mode="after")
    def code_grammar(self) -> Self:
        if self.code is not None and _OBIS.fullmatch(self.code) is None:
            raise ValueError("invalid_obis_code")
        return self


class SanitizedField(DomainModel):
    code: StrictStr
    raw_values: tuple[StrictStr, ...]
    value: DecimalValue | None
    unit: StrictStr | None
    redacted: StrictBool

    @model_validator(mode="after")
    def privacy(self) -> Self:
        if _OBIS.fullmatch(self.code) is None or not self.code.isascii():
            raise ValueError("invalid_obis_code")
        if self.redacted:
            if (
                self.raw_values != ("<redacted>",)
                or self.value is not None
                or self.unit is not None
            ):
                raise ValueError("invalid_redaction")
        else:
            if any(not value.isascii() for value in self.raw_values):
                raise ValueError("unsafe_field")
            safe, number, unit, issue = sanitize_field_values(self.code, list(self.raw_values))
            if issue or tuple(safe) != self.raw_values or unit != self.unit:
                raise ValueError("unsafe_field")
            if number != self.value or (
                number is not None and number.as_tuple() != self.value.as_tuple()
            ):
                raise ValueError("inconsistent_field_value")
        return self


class Quality(DomainModel):
    integrity: Literal["valid"] = "valid"
    frame_length: Annotated[StrictInt, Field(ge=1, le=16_384)]
    issues: tuple[FieldIssue, ...]

    @model_validator(mode="after")
    def ordered_issues(self) -> Self:
        keys = [(issue.code, issue.reason) for issue in self.issues]
        if keys != sorted(set(keys), key=lambda key: (key[0] is not None, key[0] or "", key[1])):
            raise ValueError("issues_must_be_unique_and_sorted")
        return self


class Snapshot(DomainModel):
    record_id: UUID4
    device_id: UUID4
    site_id: UUID4
    session_id: UUID4
    source_id: UUID4
    measurement_point_id: UUID5
    schema_version: StrictInt = 1
    meter_time_raw: StrictStr | None
    meter_time_utc: UtcTime | None
    received_at: UtcTime
    sampled_at: UtcTime
    metrics: Metrics
    fields: tuple[SanitizedField, ...]
    quality: Quality

    @model_validator(mode="after")
    def contract(self) -> Self:
        if self.schema_version != 1:
            raise ValueError("unsupported_schema")
        if self.measurement_point_id != measurement_point_id(self.site_id):
            raise ValueError("point_site_mismatch")
        if self.meter_time_raw is not None and not re.fullmatch(
            r"[0-9]{12}[SW]", self.meter_time_raw
        ):
            raise ValueError("unsafe_meter_time")
        if len(_canonical_bytes(self)) > MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot_too_large")
        return self

    def check_bindings(self, session: SessionDescriptor, source: SourceDescriptor) -> None:
        if (self.session_id, self.device_id, self.site_id) != (
            session.session_id,
            session.device_id,
            session.site_id,
        ) or (self.source_id, self.session_id, self.measurement_point_id) != (
            source.source_id,
            source.session_id,
            source.measurement_point_id,
        ):
            raise ValueError("descriptor_mismatch")


def _canonical_bytes(snapshot: Snapshot) -> bytes:
    return json.dumps(
        snapshot.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def canonical_snapshot_bytes(snapshot: Snapshot) -> bytes:
    """Complete canonical UTF-8 record, shared by persistence and transport."""
    validated = Snapshot.model_validate(snapshot.model_dump())
    return _canonical_bytes(validated)


def decode_snapshot(data: bytes | str) -> Snapshot:
    return Snapshot.model_validate_json(data)


def payload_sha256(snapshot: Snapshot) -> str:
    return hashlib.sha256(canonical_snapshot_bytes(snapshot)).hexdigest()


def snapshot_from_reading(
    reading: MappedReading,
    session: SessionDescriptor,
    source: SourceDescriptor,
    sampled_at: datetime,
    *,
    record_id: UUID | None = None,
) -> Snapshot:
    if (reading.profile_id, reading.profile_revision) != (
        source.profile_id,
        source.profile_revision,
    ):
        raise ValueError("profile_mismatch")
    snapshot = Snapshot(
        record_id=record_id if record_id is not None else new_id(),
        device_id=session.device_id,
        site_id=session.site_id,
        session_id=session.session_id,
        source_id=source.source_id,
        measurement_point_id=source.measurement_point_id,
        meter_time_raw=reading.meter_time_raw,
        meter_time_utc=reading.meter_time_utc,
        received_at=reading.received_at,
        sampled_at=sampled_at,
        metrics=Metrics(**reading.metrics),
        fields=tuple(
            SanitizedField(
                code=item.code,
                raw_values=tuple(item.raw_values),
                value=item.value,
                unit=item.unit,
                redacted=item.redacted,
            )
            for item in reading.fields
        ),
        quality=Quality(
            frame_length=reading.frame_length,
            issues=tuple(FieldIssue(code=item.code, reason=item.reason) for item in reading.issues),
        ),
    )
    snapshot.check_bindings(session, source)
    return snapshot


class DeviceStatus(DomainModel):
    device_id: UUID4
    last_authenticated_contact_at: UtcTime | None
    latest_reading_received_at: UtcTime | None
