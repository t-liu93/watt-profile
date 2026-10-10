"""Offline upload, immutable replay, exact boundary and safe rejection checks."""

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from watt_profile.contracts.domain import (
    Metrics,
    Snapshot,
    canonical_snapshot_bytes,
    measurement_point_id,
    payload_sha256,
)
from watt_profile.contracts.upload import (
    ERROR_CONTRACTS,
    MAX_BODY_BYTES,
    InvalidConfirmation,
    UploadBatch,
    UploadErrorResponse,
    UploadValidationError,
    error_response,
    validate_confirmation_body,
    validate_upload_body,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples/ingest"


def batch_data():
    site = UUID("00000000-0000-4000-8000-000000000001")
    device = "00000000-0000-4000-8000-000000000002"
    session = "00000000-0000-4000-8000-000000000003"
    source = "00000000-0000-4000-8000-000000000004"
    point = str(measurement_point_id(site))
    now = "2026-01-02T03:04:05.123456Z"
    return dict(
        schema_version=1,
        device_id=device,
        site_id=str(site),
        measurement_point_id=point,
        session=dict(session_id=session, device_id=device, site_id=str(site), started_at=now),
        source=dict(
            source_id=source,
            session_id=session,
            measurement_point_id=point,
            kind="p1_serial",
            profile_id="dsmr5_nl",
            profile_revision=1,
        ),
        records=[
            dict(
                record_id="00000000-0000-4000-8000-000000000006",
                device_id=device,
                site_id=str(site),
                session_id=session,
                source_id=source,
                measurement_point_id=point,
                schema_version=1,
                meter_time_raw=None,
                meter_time_utc=None,
                received_at=now,
                sampled_at=now,
                metrics={
                    key: "1.020" if key == "power_import_kw" else None
                    for key in Metrics.model_fields
                },
                fields=[
                    dict(
                        code="1-0:1.7.0",
                        raw_values=["1.020*kW"],
                        value="1.020",
                        unit="kW",
                        redacted=False,
                    ),
                    dict(
                        code="0-0:96.1.1",
                        raw_values=["<redacted>"],
                        value=None,
                        unit=None,
                        redacted=True,
                    ),
                ],
                quality=dict(integrity="valid", frame_length=120, issues=[]),
            )
        ],
    )


def body(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def rejection(data, code="invalid_payload", status=422):
    with pytest.raises(UploadValidationError) as error:
        validate_upload_body(body(data))
    assert str(error.value) == code
    assert error.value.status_code == status
    assert error.value.response == error_response(code)


def confirmation(batch):
    return dict(
        schema_version=1,
        device_id=str(batch.device_id),
        confirmed_record_ids=[str(record.record_id) for record in batch.records],
    )


def test_accepted_and_exact_count_boundaries():
    data = batch_data()
    record = data["records"][0]
    data["records"] = [record | {"record_id": str(uuid4())} for _ in range(200)]
    batch = validate_upload_body(body(data), content_encoding="identity")
    assert len(batch.records) == 200
    assert UploadBatch.model_validate_json(batch.model_dump_json()) == batch
    data["records"].append(record | {"record_id": str(uuid4())})
    rejection(data, "batch_too_large", 413)
    data["records"] = []
    rejection(data)


def test_exact_snapshot_boundary_including_all_sections():
    data = batch_data()
    record = data["records"][0]
    baseline = len(canonical_snapshot_bytes(Snapshot.model_validate(record)))
    record["metrics"]["energy_import_t1_kwh"] = "1"
    initial = len(canonical_snapshot_bytes(Snapshot.model_validate(record)))
    record["metrics"]["energy_import_t1_kwh"] = "1" * (16_384 - initial + 1)
    batch = validate_upload_body(body(data))
    assert len(canonical_snapshot_bytes(batch.records[0])) == 16_384
    assert baseline < 16_384
    record["metrics"]["energy_import_t1_kwh"] += "1"
    rejection(data, "batch_too_large", 413)
    # Field and quality bytes participate in the very same codec limit.
    record["metrics"]["energy_import_t1_kwh"] = None
    record["fields"] *= 200
    rejection(data, "batch_too_large", 413)
    record["fields"] = []
    record["quality"]["issues"] = [
        dict(code=f"9-9:{index}.0.0", reason="redacted") for index in range(1000)
    ]
    record["quality"]["issues"].sort(key=lambda item: item["code"])
    rejection(data, "batch_too_large", 413)


def test_exact_actual_body_byte_boundary_and_parse_order(monkeypatch):
    encoded = body(batch_data())
    at_limit = encoded + b" " * (MAX_BODY_BYTES - len(encoded))
    assert len(at_limit) == MAX_BODY_BYTES
    assert validate_upload_body(at_limit).records

    def must_not_parse(*args, **kwargs):
        pytest.fail("oversized body was parsed")

    monkeypatch.setattr("watt_profile.contracts.upload.json.loads", must_not_parse)
    with pytest.raises(UploadValidationError, match="batch_too_large") as error:
        validate_upload_body(at_limit + b" ")
    assert error.value.status_code == 413
    # A multibyte incoming string crosses the limit even with fewer characters.
    with pytest.raises(UploadValidationError, match="batch_too_large"):
        validate_upload_body(("é" * (MAX_BODY_BYTES // 2) + " ").encode())


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "identity, gzip", ""])
def test_reject_compression(encoding):
    with pytest.raises(UploadValidationError, match="invalid_payload"):
        validate_upload_body(body(batch_data()), content_encoding=encoding)


@pytest.mark.parametrize(
    "incoming",
    [b"not json", b"\xff", b"[]", b"null", b'{"schema_version":1,"schema_version":1}', "{}"],
)
def test_malformed_json_and_root(incoming):
    with pytest.raises(UploadValidationError, match="invalid_payload"):
        validate_upload_body(incoming)


@pytest.mark.parametrize(
    "section", [None, "session", "source", "record", "metrics", "quality", "field"]
)
def test_extra_keys_are_rejected_at_every_level(section):
    data = batch_data()
    record = data["records"][0]
    target = {
        None: data,
        "session": data["session"],
        "source": data["source"],
        "record": record,
        "metrics": record["metrics"],
        "quality": record["quality"],
        "field": record["fields"][0],
    }[section]
    target["token"] = "synthetic-private-value"
    rejection(data)


@pytest.mark.parametrize("section", [None, "record"])
@pytest.mark.parametrize("version", [0, 2, True, "1"])
def test_strict_and_unsupported_schema(section, version):
    data = batch_data()
    target = data if section is None else data["records"][0]
    target["schema_version"] = version
    rejection(data, "unsupported_schema" if type(version) is int else "invalid_payload")


@pytest.mark.parametrize(
    "section,key",
    [
        ("batch", "device_id"),
        ("batch", "site_id"),
        ("batch", "measurement_point_id"),
        ("session", "session_id"),
        ("session", "device_id"),
        ("session", "site_id"),
        ("source", "source_id"),
        ("source", "session_id"),
        ("source", "measurement_point_id"),
        ("record", "device_id"),
        ("record", "session_id"),
        ("record", "source_id"),
        ("record", "site_id"),
        ("record", "measurement_point_id"),
    ],
)
def test_every_ownership_binding(section, key):
    data = batch_data()
    target = (
        data if section == "batch" else data["records"][0] if section == "record" else data[section]
    )
    target[key] = (
        str(measurement_point_id(uuid4())) if key == "measurement_point_id" else str(uuid4())
    )
    rejection(data)


def test_mixed_groups_and_repeated_request_ids():
    data = batch_data()
    data["records"].append(dict(data["records"][0]))
    rejection(data)
    data["records"][1]["record_id"] = str(uuid4())
    data["records"][1]["source_id"] = str(uuid4())
    rejection(data)


@pytest.mark.parametrize("metric", [1, 1.02, True, "1e0", "NaN", "-1"])
def test_transport_decimal_policy(metric):
    data = batch_data()
    data["records"][0]["metrics"]["power_import_kw"] = metric
    rejection(data)


def test_reject_unsafe_values_despite_redaction_claims_and_safe_errors():
    data = batch_data()
    data["records"][0]["fields"][1]["raw_values"] = ["synthetic-private-value"]
    with pytest.raises(UploadValidationError) as error:
        validate_upload_body(body(data))
    assert "synthetic-private-value" not in str(error.value)
    assert "synthetic-private-value" not in error.value.response.model_dump_json()
    assert error.value.__cause__ is None
    data["records"][0]["fields"][1]["redacted"] = False
    rejection(data)
    data = batch_data()
    data["records"][0]["fields"][0]["unit"] = "V"
    rejection(data)


def test_replay_canonical_hash_and_immutable_content():
    data = batch_data()
    original = validate_upload_body(body(data))
    replay = validate_upload_body(json.dumps(data, indent=2, sort_keys=True).encode())
    assert payload_sha256(original.records[0]) == payload_sha256(replay.records[0])
    assert canonical_snapshot_bytes(original.records[0]) == canonical_snapshot_bytes(
        replay.records[0]
    )
    data["records"][0]["metrics"]["power_import_kw"] = "1.0200"
    assert payload_sha256(validate_upload_body(body(data)).records[0]) != payload_sha256(
        original.records[0]
    )
    data = batch_data()
    data["records"][0]["fields"].reverse()
    assert payload_sha256(validate_upload_body(body(data)).records[0]) != payload_sha256(
        original.records[0]
    )


def test_full_confirmation_set_allows_reordered_ids():
    data = batch_data()
    data["records"].append(data["records"][0] | {"record_id": str(uuid4())})
    batch = validate_upload_body(body(data))
    reply = confirmation(batch)
    reply["confirmed_record_ids"].reverse()
    validated = validate_confirmation_body(body(reply), batch)
    assert set(validated.confirmed_record_ids) == {record.record_id for record in batch.records}


@pytest.mark.parametrize(
    "patch",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"schema_version": "1"},
        {"device_id": str(uuid4())},
        {"confirmed_record_ids": []},
        {"confirmed_record_ids": [str(uuid4())]},
        {"confirmed_record_ids": ["malformed"]},
        {"confirmed_record_ids": ["00000000-0000-4000-8000-000000000006"] * 2},
        {"confirmed_record_ids": ["00000000-0000-4000-8000-000000000006", str(uuid4())]},
        {"extra": "synthetic-private-value"},
    ],
)
def test_invalid_confirmations(patch):
    batch = validate_upload_body(body(batch_data()))
    with pytest.raises(InvalidConfirmation, match="^invalid_confirmation$"):
        validate_confirmation_body(body(confirmation(batch) | patch), batch)


def test_missing_confirmation_ids_and_non_json_leave_batch_immutable():
    data = batch_data()
    data["records"].append(data["records"][0] | {"record_id": str(uuid4())})
    batch = validate_upload_body(body(data))
    before = batch.model_dump_json()
    reply = confirmation(batch)
    reply["confirmed_record_ids"].pop()
    for invalid in (body(reply), b"<html>upstream error</html>", b"null"):
        with pytest.raises(InvalidConfirmation):
            validate_confirmation_body(invalid, batch)
        assert batch.model_dump_json() == before


def test_all_error_contracts_are_fixed():
    for code, (_, message, retryable) in ERROR_CONTRACTS.items():
        response = error_response(code)
        assert response.error.message == message
        assert response.error.retryable is retryable
        assert UploadErrorResponse.model_validate_json(response.model_dump_json()) == response
        altered = response.model_dump(mode="json")
        altered["error"]["message"] = "synthetic-private-value"
        with pytest.raises(ValidationError):
            UploadErrorResponse.model_validate(altered)


def test_synthetic_examples():
    request = validate_upload_body((EXAMPLES / "request.json").read_bytes())
    replay = validate_upload_body((EXAMPLES / "replay.json").read_bytes())
    assert canonical_snapshot_bytes(request.records[0]) == canonical_snapshot_bytes(
        replay.records[0]
    )
    validate_confirmation_body((EXAMPLES / "confirmation.json").read_bytes(), request)
    reject = UploadErrorResponse.model_validate_json((EXAMPLES / "rejection.json").read_bytes())
    assert reject == error_response("site_not_authorized")
