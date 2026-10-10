"""Executable privacy, identity and immutable serialization contracts."""

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from watt_profile.contracts.domain import (
    Metrics,
    SanitizedField,
    SessionDescriptor,
    Snapshot,
    SourceDescriptor,
    canonical_snapshot_bytes,
    decode_snapshot,
    measurement_point_id,
    payload_sha256,
    snapshot_from_reading,
)
from watt_profile.p1.parser import parse_telegram
from watt_profile.p1.profile import map_telegram

NOW = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)


def descriptors():
    session = SessionDescriptor(
        session_id=uuid4(), device_id=uuid4(), site_id=uuid4(), started_at=NOW
    )
    source = SourceDescriptor(
        source_id=uuid4(),
        session_id=session.session_id,
        measurement_point_id=measurement_point_id(session.site_id),
    )
    return session, source


def snapshot():
    frame = (Path(__file__).parent / "fixtures/p1/profile/single_phase.telegram").read_bytes()
    reading = map_telegram(parse_telegram(frame), NOW)
    return snapshot_from_reading(reading, *descriptors(), NOW)


def test_roundtrip_canonical_and_precision():
    record = snapshot()
    encoded = canonical_snapshot_bytes(record)
    decoded = decode_snapshot(encoded)
    assert canonical_snapshot_bytes(decoded) == encoded
    assert len(decoded.metrics.model_dump()) == 18
    assert decoded.metrics.voltage_l2_v is None
    assert b"2026-01-02T03:04:05.123456Z" in encoded
    assert b"fingerprint" not in encoded
    content = json.loads(encoded)
    content = dict(reversed(list(content.items())))
    assert canonical_snapshot_bytes(Snapshot.model_validate(content)) == encoded
    changed = json.loads(encoded)
    changed["metrics"]["power_import_kw"] = "0.00100"
    changed2 = json.loads(encoded)
    changed2["metrics"]["power_import_kw"] = "0.0010"
    assert payload_sha256(Snapshot.model_validate(changed)) != payload_sha256(
        Snapshot.model_validate(changed2)
    )
    assert decoded.record_id == record.record_id


@pytest.mark.parametrize(
    "value",
    [1, True, 1.2, "1e3", "-1", "-0", "NaN", "Infinity", ".2", "1.", Decimal("NaN"), Decimal("-1")],
)
def test_decimal_rejects_unsafe_scalars(value):
    metrics = {key: None for key in Metrics.model_fields}
    metrics["power_import_kw"] = value
    with pytest.raises(ValidationError):
        Metrics(**metrics)


def test_internal_decimal_and_offset_normalization():
    metrics = {key: None for key in Metrics.model_fields}
    metrics["power_import_kw"] = Decimal("123456789012345678901234567890.00100")
    assert Metrics(**metrics).model_dump(mode="json")["power_import_kw"].endswith(".00100")
    session, _ = descriptors()
    data = session.model_dump()
    data["started_at"] = NOW.astimezone(timezone(timedelta(hours=2)))
    assert SessionDescriptor(**data).started_at == NOW
    data["started_at"] = NOW.replace(tzinfo=None)
    with pytest.raises(ValidationError):
        SessionDescriptor(**data)


def test_identity_consistency_and_frozen_binding():
    record = snapshot()
    session, source = descriptors()
    with pytest.raises(ValueError, match="descriptor_mismatch"):
        record.check_bindings(session, source)
    with pytest.raises(ValidationError):
        session.site_id = uuid4()
    data = record.model_dump()
    data["site_id"] = uuid4()
    with pytest.raises(ValidationError, match="point_site_mismatch"):
        Snapshot(**data)
    data = source.model_dump()
    data["profile_revision"] = True
    with pytest.raises(ValidationError):
        SourceDescriptor(**data)
    data = record.model_dump()
    data["schema_version"] = True
    with pytest.raises(ValidationError):
        Snapshot(**data)
    data = session.model_dump()
    data["session_id"] = measurement_point_id(session.site_id)
    with pytest.raises(ValidationError):
        SessionDescriptor(**data)


@pytest.mark.parametrize(
    "patch",
    [
        {"raw_values": ["private-id"], "redacted": True},
        {"raw_values": ["123"], "redacted": False},
        {"raw_values": ["<redacted>"], "value": "2", "redacted": True},
        {"raw_values": ["<redacted>"], "redacted": 1},
        {"raw_values": ["<redacted>"], "fingerprint": "a" * 64},
    ],
)
def test_untrusted_redaction_and_unknown_values(patch):
    data = dict(code="0-0:96.1.1", raw_values=["<redacted>"], value=None, unit=None, redacted=True)
    with pytest.raises(ValidationError):
        SanitizedField(**(data | patch))


def test_safe_grouped_fields_standard_tariff_and_matching_numeric():
    for tariff in ("0001", "0002", "1", "2"):
        item = SanitizedField(
            code="0-0:96.14.0", raw_values=[tariff], value=None, unit=None, redacted=False
        )
        assert item.model_dump(mode="json")["raw_values"] == [tariff]
    log = ["1", "0-0:96.7.19", "260101000000W", "00123*s"]
    item = SanitizedField(code="1-0:99.97.0", raw_values=log, value=None, unit=None, redacted=False)
    assert item.model_dump(mode="json")["raw_values"] == log
    mbus = SanitizedField(
        code="0-1:24.2.1",
        raw_values=["260101000000W", "001.020*m3"],
        value=Decimal("1.020"),
        unit="m3",
        redacted=False,
    )
    assert mbus.model_dump(mode="json")["value"] == "1.020"
    with pytest.raises(ValidationError):
        SanitizedField(
            code="1-0:1.7.0", raw_values=["1.020*kW"], value="1.02", unit="kW", redacted=False
        )
    with pytest.raises(ValidationError):
        SanitizedField(
            code="1-0:1.7.0", raw_values=["1.020*kW"], value="1.020", unit="V", redacted=False
        )


def test_nested_extra_keys_and_bounds():
    data = snapshot().model_dump(mode="json")
    data["metrics"]["unknown"] = None
    with pytest.raises(ValidationError):
        Snapshot(**data)
    data = snapshot().model_dump(mode="json")
    data["quality"]["frame_length"] = True
    with pytest.raises(ValidationError):
        Snapshot(**data)
    data = snapshot().model_dump(mode="json")
    data["fields"] *= 100
    with pytest.raises(ValidationError, match="snapshot_too_large"):
        Snapshot(**data)
    data = snapshot().model_dump(mode="json")
    data["quality"]["issues"] *= 2
    with pytest.raises(ValidationError, match="unique_and_sorted"):
        Snapshot(**data)


def test_exact_utf8_record_limit_and_group_order():
    data = snapshot().model_dump(mode="json")
    data["fields"].append(
        dict(code="9-9:1.0.0", raw_values=["<redacted>"], value=None, unit=None, redacted=True)
    )
    baseline = canonical_snapshot_bytes(Snapshot(**data))
    available = 16_384 - len(baseline)
    data["fields"][-1]["code"] = "9-9:" + "1" * (available + 1) + ".0.0"
    record = Snapshot(**data)
    assert len(canonical_snapshot_bytes(record)) == 16_384
    assert canonical_snapshot_bytes(
        decode_snapshot(canonical_snapshot_bytes(record))
    ) == canonical_snapshot_bytes(record)
    data["fields"][-1]["code"] += "0"
    with pytest.raises(ValidationError, match="snapshot_too_large"):
        Snapshot(**data)
    record = snapshot()
    data = record.model_dump(mode="json")
    data["fields"].reverse()
    assert payload_sha256(Snapshot(**data)) != payload_sha256(record)


def test_standard_tariff_survives_parser_and_domain_boundary():
    from watt_profile.p1.parser import dsmr_crc16

    body = b"/SYNTHETIC\r\n1-3:0.2.8(50)\r\n0-0:96.14.0(0002)\r\n1-0:1.7.0(1.020*kW)\r\n!"
    frame = body + f"{dsmr_crc16(body):04X}\r\n".encode("ascii")
    record = snapshot_from_reading(map_telegram(parse_telegram(frame), NOW), *descriptors(), NOW)
    tariff = next(
        item
        for item in decode_snapshot(canonical_snapshot_bytes(record)).fields
        if item.code == "0-0:96.14.0"
    )
    assert tariff.raw_values == ("0002",) and not tariff.redacted
