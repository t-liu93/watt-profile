"""Offline integration from synthetic P1 bytes through upload confirmation."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from watt_profile.contracts.domain import (
    SessionDescriptor,
    SourceDescriptor,
    canonical_snapshot_bytes,
    measurement_point_id,
    payload_sha256,
    snapshot_from_reading,
)
from watt_profile.contracts.upload import (
    InvalidConfirmation,
    UploadBatch,
    validate_confirmation_body,
    validate_upload_body,
)
from watt_profile.p1.parser import TelegramFramer, dsmr_crc16, parse_telegram
from watt_profile.p1.profile import map_telegram


def synthetic_frame() -> bytes:
    fixture = (Path(__file__).parent / "fixtures/p1/profile/three_phase.telegram").read_bytes()
    # Add valid grouped event-log data and an unknown private value, then CRC the final bytes.
    covered = fixture[: fixture.rfind(b"!")].replace(b"1-0:72.7.0(233.3*V)\r\n", b"")
    covered += (
        b"0-0:96.7.21(1)\r\n"
        b"1-0:99.97.0(1)(0-0:96.7.19)(261024120000S)(60*s)\r\n"
        b"0-0:96.13.0(SYNTHETIC-MESSAGE-SENTINEL)\r\n"
        b"0-0:96.1.1(SYNTHETIC-EQUIPMENT-SENTINEL)\r\n!"
    )
    return covered + f"{dsmr_crc16(covered):04X}\r\n".encode("ascii")


def json_bytes(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def test_synthetic_bytes_to_complete_confirmation_preserves_contracts():
    frame = synthetic_frame()
    framer = TelegramFramer()
    extracted = []
    # Exercise incremental framing across arbitrary serial read boundaries.
    for offset in range(0, len(frame), 11):
        extracted.extend(framer.feed(frame[offset : offset + 11]))
    assert extracted == [frame]
    telegram = parse_telegram(extracted[0])
    assert telegram.integrity == "valid"
    assert "SYNTHETIC-EQUIPMENT-SENTINEL" not in repr(telegram)

    received = datetime(2026, 10, 25, 1, 31, tzinfo=UTC)
    reading = map_telegram(telegram, received)
    assert reading is not None
    assert reading.metrics["energy_import_t1_kwh"] == Decimal("99999999999999999999.123456")
    assert reading.metrics["voltage_l2_v"] == Decimal("232.2")
    assert reading.metrics["voltage_l3_v"] is None
    assert reading.metrics["current_l1_a"] == Decimal("1.1")
    assert reading.metrics["power_import_l2_kw"] == Decimal("0.200")
    assert reading.metrics["power_export_l3_kw"] == Decimal("0.600")
    assert reading.metrics["voltage_l1_v"].as_tuple().exponent == -1
    assert reading.metrics["voltage_l2_v"].as_tuple().exponent == -1

    fields = {field.code: field for field in reading.fields}
    assert fields["1-0:99.97.0"].raw_values == [
        "1",
        "0-0:96.7.19",
        "261024120000S",
        "60*s",
    ]
    assert fields["0-0:96.13.0"].raw_values == ["<redacted>"]
    assert fields["0-0:96.13.0"].redacted
    assert fields["0-0:96.1.1"].raw_values == ["<redacted>"]
    assert fields["0-0:96.1.1"].value is None and fields["0-0:96.1.1"].unit is None
    assert all("SENTINEL" not in repr(field) for field in reading.fields)

    site_id = UUID("00000000-0000-4000-8000-000000000001")
    device_id = UUID("00000000-0000-4000-8000-000000000002")
    session_id = UUID("00000000-0000-4000-8000-000000000003")
    source_id = UUID("00000000-0000-4000-8000-000000000004")
    record_id = UUID("00000000-0000-4000-8000-000000000006")
    point_id = measurement_point_id(site_id)
    session = SessionDescriptor(
        session_id=session_id,
        device_id=device_id,
        site_id=site_id,
        started_at=datetime(2026, 10, 25, 1, 0, tzinfo=UTC),
    )
    source = SourceDescriptor(
        source_id=source_id, session_id=session_id, measurement_point_id=point_id
    )
    snapshot = snapshot_from_reading(
        reading,
        session,
        source,
        datetime(2026, 10, 25, 1, 32, tzinfo=UTC),
        record_id=record_id,
    )
    assert (
        snapshot.site_id,
        snapshot.device_id,
        snapshot.session_id,
        snapshot.source_id,
        snapshot.measurement_point_id,
        snapshot.record_id,
    ) == (site_id, device_id, session_id, source_id, point_id, record_id)
    assert snapshot.metrics.power_import_l3_kw == Decimal("0.300")
    assert snapshot.metrics.voltage_l2_v == Decimal("232.2")
    assert snapshot.metrics.voltage_l1_v.as_tuple().exponent == -1
    assert snapshot.metrics.voltage_l2_v.as_tuple().exponent == -1
    assert snapshot.metrics.energy_import_t1_kwh.as_tuple().exponent == -6
    assert snapshot.metrics.voltage_l1_v == Decimal("231.1")
    # Nullable C3 fields are represented explicitly, including no synthetic fallback.
    assert snapshot.metrics.model_dump()["energy_export_t1_kwh"] == Decimal("3.000")
    serialized_snapshot = canonical_snapshot_bytes(snapshot)
    assert b"SENTINEL" not in serialized_snapshot
    assert b'"<redacted>"' in serialized_snapshot
    assert b'"voltage_l3_v":null' in serialized_snapshot

    batch_content = {
        "schema_version": 1,
        "device_id": str(device_id),
        "site_id": str(site_id),
        "measurement_point_id": str(point_id),
        "session": session.model_dump(mode="json"),
        "source": source.model_dump(mode="json"),
        "records": [snapshot.model_dump(mode="json")],
    }
    first = validate_upload_body(json_bytes(batch_content))
    replay = validate_upload_body(
        json.dumps(batch_content, indent=2, sort_keys=True).encode("utf-8")
    )
    assert isinstance(first, UploadBatch)
    assert payload_sha256(first.records[0]) == payload_sha256(replay.records[0])
    assert canonical_snapshot_bytes(first.records[0]) == canonical_snapshot_bytes(replay.records[0])
    record = first.records[0]
    assert (
        record.site_id,
        record.device_id,
        record.session_id,
        record.source_id,
        record.measurement_point_id,
        record.record_id,
    ) == (site_id, device_id, session_id, source_id, point_id, record_id)

    confirmation = {
        "schema_version": 1,
        "device_id": str(device_id),
        "confirmed_record_ids": [str(record_id)],
    }
    accepted = validate_confirmation_body(json_bytes(confirmation), first)
    assert accepted.confirmed_record_ids == (record_id,)
    # Lost, incomplete, and mismatched confirmations all leave the batch unacknowledged.
    for lost_or_malformed in (b"", b"upstream response lost", b"null"):
        with pytest.raises(InvalidConfirmation, match="^invalid_confirmation$"):
            validate_confirmation_body(lost_or_malformed, first)
    incomplete = confirmation | {"confirmed_record_ids": []}
    mismatch = confirmation | {"device_id": str(UUID("00000000-0000-4000-8000-000000000099"))}
    for rejected in (incomplete, mismatch):
        with pytest.raises(InvalidConfirmation, match="^invalid_confirmation$"):
            validate_confirmation_body(json_bytes(rejected), first)
