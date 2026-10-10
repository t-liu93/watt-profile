"""Pure DSMR 5 profile admission and electrical metric mapping."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from watt_profile.p1.parser import FieldIssue, P1Telegram, SanitizedField

PROFILE_ID = "dsmr5_nl"
PROFILE_REVISION = 1

METRIC_CODES: dict[str, tuple[str, str]] = {
    "energy_import_t1_kwh": ("1-0:1.8.1", "kWh"),
    "energy_import_t2_kwh": ("1-0:1.8.2", "kWh"),
    "energy_export_t1_kwh": ("1-0:2.8.1", "kWh"),
    "energy_export_t2_kwh": ("1-0:2.8.2", "kWh"),
    "power_import_kw": ("1-0:1.7.0", "kW"),
    "power_export_kw": ("1-0:2.7.0", "kW"),
    "voltage_l1_v": ("1-0:32.7.0", "V"),
    "voltage_l2_v": ("1-0:52.7.0", "V"),
    "voltage_l3_v": ("1-0:72.7.0", "V"),
    "current_l1_a": ("1-0:31.7.0", "A"),
    "current_l2_a": ("1-0:51.7.0", "A"),
    "current_l3_a": ("1-0:71.7.0", "A"),
    "power_import_l1_kw": ("1-0:21.7.0", "kW"),
    "power_import_l2_kw": ("1-0:41.7.0", "kW"),
    "power_import_l3_kw": ("1-0:61.7.0", "kW"),
    "power_export_l1_kw": ("1-0:22.7.0", "kW"),
    "power_export_l2_kw": ("1-0:42.7.0", "kW"),
    "power_export_l3_kw": ("1-0:62.7.0", "kW"),
}
_REQUIRED_METRICS = {
    "energy_import_t1_kwh",
    "energy_import_t2_kwh",
    "energy_export_t1_kwh",
    "energy_export_t2_kwh",
    "power_import_kw",
    "power_export_kw",
    "voltage_l1_v",
    "current_l1_a",
    "power_import_l1_kw",
    "power_export_l1_kw",
}
_TIMESTAMP_RE = re.compile(r"(?P<date>\d{12})(?P<season>[SW])\Z")
_AMSTERDAM = ZoneInfo("Europe/Amsterdam")


@dataclass(frozen=True)
class MappedReading:
    """Sanitized reading independent of Pydantic and persistence concerns."""

    profile_id: str
    profile_revision: int
    frame_length: int
    meter_time_raw: str | None
    meter_time_utc: datetime | None
    received_at: datetime
    metrics: dict[str, Decimal | None]
    fields: tuple[SanitizedField, ...]
    issues: tuple[FieldIssue, ...]


def map_telegram_with_rejection(
    telegram: P1Telegram, received_at: datetime
) -> tuple[MappedReading | None, str | None]:
    """Map an admitted telegram and return a fixed, content-free rejection code."""
    reason = _rejection_reason(telegram)
    if reason is not None:
        return None, reason
    received = _require_utc(received_at)
    fields = telegram.fields
    by_code: dict[str, list[SanitizedField]] = {}
    for item in fields:
        by_code.setdefault(item.code, []).append(item)

    issues: list[FieldIssue] = [
        FieldIssue(issue.code, "invalid_timestamp")
        if issue.code == "0-0:1.0.0" and issue.reason == "invalid_value"
        else issue
        for item in fields
        for issue in item.issues
    ]
    metrics: dict[str, Decimal | None] = {}
    normalized_fields = list(fields)
    for name, (code, expected_unit) in METRIC_CODES.items():
        occurrences = by_code.get(code, [])
        if not occurrences:
            metrics[name] = None
            if name in _REQUIRED_METRICS:
                issues.append(FieldIssue(code, "missing"))
            continue
        if len(occurrences) != 1:
            metrics[name] = None
            issues.append(FieldIssue(code, "duplicate"))
            normalized_fields = _redact_code(normalized_fields, code, "duplicate")
            continue
        item = occurrences[0]
        value = item.value
        if item.redacted or value is None:
            metrics[name] = None
            continue
        if value < 0:
            metrics[name] = None
            issues.append(FieldIssue(code, "invalid_value"))
            normalized_fields = _redact_code(normalized_fields, code, "invalid_value")
        elif item.unit != expected_unit:
            metrics[name] = None
            issues.append(FieldIssue(code, "invalid_unit"))
            normalized_fields = _redact_code(normalized_fields, code, "invalid_unit")
        else:
            metrics[name] = value

    if not any(value is not None for value in metrics.values()):
        return None, "no_electricity_metrics"

    # A main equipment identifier is used only for local source binding. Its
    # fingerprint stays out of the public representation and serialization.
    identity_fields = by_code.get("0-0:96.1.1", [])
    if not identity_fields:
        issues.append(FieldIssue("0-0:96.1.1", "missing"))

    timestamp_occurrences = by_code.get("0-0:1.0.0", [])
    meter_time_raw: str | None = None
    meter_time_utc: datetime | None = None
    if not timestamp_occurrences:
        issues.append(FieldIssue("0-0:1.0.0", "missing"))
    elif len(timestamp_occurrences) > 1:
        issues.append(FieldIssue("0-0:1.0.0", "duplicate"))
        normalized_fields = _redact_code(normalized_fields, "0-0:1.0.0", "duplicate")
    else:
        timestamp_field = timestamp_occurrences[0]
        if not timestamp_field.redacted and timestamp_field.raw_values:
            meter_time_raw = timestamp_field.raw_values[0]
            meter_time_utc, timestamp_issue = _parse_meter_time(meter_time_raw)
            if timestamp_issue:
                issues.append(FieldIssue("0-0:1.0.0", timestamp_issue))
                if timestamp_issue == "invalid_timestamp":
                    normalized_fields = _redact_code(
                        normalized_fields, "0-0:1.0.0", timestamp_issue
                    )
        elif timestamp_field.issues:
            # Parser issues already describe unsafe timestamp grammar.
            pass
        else:
            issues.append(FieldIssue("0-0:1.0.0", "invalid_timestamp"))

    return (
        MappedReading(
            profile_id=PROFILE_ID,
            profile_revision=PROFILE_REVISION,
            frame_length=telegram.frame_length,
            meter_time_raw=meter_time_raw,
            meter_time_utc=meter_time_utc,
            received_at=received,
            metrics=metrics,
            fields=tuple(normalized_fields),
            issues=_sorted_issues(issues),
        ),
        None,
    )


def map_telegram(telegram: P1Telegram, received_at: datetime) -> MappedReading | None:
    """Return a reading for admitted DSMR 5 telegrams, otherwise ``None``."""
    return map_telegram_with_rejection(telegram, received_at)[0]


def _rejection_reason(telegram: P1Telegram) -> str | None:
    if telegram.integrity == "invalid":
        return "invalid_crc"
    if telegram.integrity != "valid":
        return "unverifiable_crc"
    versions = [item for item in telegram.fields if item.code == "1-3:0.2.8"]
    if len(versions) != 1 or versions[0].redacted or versions[0].raw_values != ["50"]:
        return "unsupported_profile_version"
    return None


def _parse_meter_time(value: str) -> tuple[datetime | None, str | None]:
    match = _TIMESTAMP_RE.fullmatch(value)
    if match is None:
        return None, "invalid_timestamp"
    text = match.group("date")
    try:
        local_wall = datetime(
            2000 + int(text[0:2]),
            int(text[2:4]),
            int(text[4:6]),
            int(text[6:8]),
            int(text[8:10]),
            int(text[10:12]),
        )
    except ValueError:
        return None, "invalid_timestamp"
    offset_hours = 2 if match.group("season") == "S" else 1
    utc_value = local_wall.replace(tzinfo=UTC) - timedelta(hours=offset_hours)
    expected = utc_value.astimezone(_AMSTERDAM).utcoffset()
    actual = timedelta(hours=offset_hours)
    return utc_value, None if expected == actual else "clock_offset_mismatch"


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("received_at_must_be_utc")
    return value.astimezone(UTC)


def _redact_code(fields: list[SanitizedField], code: str, reason: str) -> list[SanitizedField]:
    result: list[SanitizedField] = []
    for item in fields:
        if item.code == code:
            issue = FieldIssue(code, reason)
            result.append(SanitizedField(code, ["<redacted>"], None, None, True, (issue,)))
        else:
            result.append(item)
    return result


def _sorted_issues(issues: list[FieldIssue]) -> tuple[FieldIssue, ...]:
    unique = {(item.code, item.reason): item for item in issues}
    return tuple(
        unique[key]
        for key in sorted(unique, key=lambda pair: (pair[0] is not None, pair[0] or "", pair[1]))
    )
