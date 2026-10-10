from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from watt_profile.p1.parser import FieldIssue, dsmr_crc16, parse_telegram
from watt_profile.p1.profile import (
    METRIC_CODES,
    MappedReading,
    map_telegram,
    map_telegram_with_rejection,
)

FIXTURES = Path(__file__).parent / "fixtures/p1/profile"
RECEIVED = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)


def make_frame(lines: list[str]) -> bytes:
    """Build a synthetic telegram and CRC its final content through the footer."""
    covered = ("/SYNTHETIC5\\2M\r\n" + "\r\n".join(lines) + "\r\n!").encode("ascii")
    return covered + f"{dsmr_crc16(covered):04X}\r\n".encode("ascii")


def issue_pairs(reading: MappedReading) -> list[tuple[str, str]]:
    return [(issue.code, issue.reason) for issue in reading.issues]


def test_synthetic_single_phase_fixture_maps_zero_and_omitted_phases():
    fixture = (FIXTURES / "single_phase.telegram").read_bytes()
    parsed = parse_telegram(fixture)
    reading, rejection = map_telegram_with_rejection(parsed, RECEIVED)

    assert rejection is None and isinstance(reading, MappedReading)
    assert parsed.integrity == "valid"
    assert len(METRIC_CODES) == len(reading.metrics) == 18
    assert reading.metrics["energy_import_t1_kwh"] == Decimal("1234.500")
    assert reading.metrics["energy_export_t1_kwh"] == Decimal("0.000")
    assert reading.metrics["power_import_kw"] == Decimal("0.000")
    assert reading.metrics["voltage_l1_v"] == Decimal("230.1")
    assert reading.metrics["current_l1_a"] == Decimal("5.2")
    assert reading.metrics["power_import_l1_kw"] == Decimal("1.200")
    for name in (
        "voltage_l2_v",
        "voltage_l3_v",
        "current_l2_a",
        "current_l3_a",
        "power_import_l2_kw",
        "power_import_l3_kw",
        "power_export_l2_kw",
        "power_export_l3_kw",
    ):
        assert reading.metrics[name] is None
        assert not any(
            code == METRIC_CODES[name][0] and reason == "missing"
            for code, reason in issue_pairs(reading)
        )
    assert reading.meter_time_raw == "260715120000S"
    assert reading.meter_time_utc == datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
    assert reading.received_at == RECEIVED
    assert "SYNTHETIC-METER-01" not in repr(reading)
    assert ("0-0:96.1.1", "redacted") in issue_pairs(reading)


def test_synthetic_three_phase_fixture_maps_distinct_phases_and_large_decimal():
    parsed = parse_telegram((FIXTURES / "three_phase.telegram").read_bytes())
    reading = map_telegram(parsed, RECEIVED)
    assert reading is not None
    assert reading.metrics["energy_import_t1_kwh"] == Decimal("99999999999999999999.123456")
    assert [reading.metrics[f"voltage_l{i}_v"] for i in (1, 2, 3)] == [
        Decimal("231.1"),
        Decimal("232.2"),
        Decimal("233.3"),
    ]
    assert [reading.metrics[f"current_l{i}_a"] for i in (1, 2, 3)] == [
        Decimal("1.1"),
        Decimal("2.2"),
        Decimal("3.3"),
    ]
    assert [reading.metrics[f"power_import_l{i}_kw"] for i in (1, 2, 3)] == [
        Decimal("0.100"),
        Decimal("0.200"),
        Decimal("0.300"),
    ]
    assert [reading.metrics[f"power_export_l{i}_kw"] for i in (1, 2, 3)] == [
        Decimal("0.400"),
        Decimal("0.500"),
        Decimal("0.600"),
    ]
    assert reading.metrics["power_import_kw"] == Decimal("5.000")
    assert reading.metrics["power_export_kw"] == Decimal("6.000")
    assert reading.metrics["energy_import_t2_kwh"] == Decimal("2.000")
    assert reading.metrics["energy_export_t1_kwh"] == Decimal("3.000")
    assert reading.metrics["energy_export_t2_kwh"] == Decimal("4.000")
    # The W suffix selects UTC+01:00 during the repeated autumn hour.
    assert reading.meter_time_utc == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)


def test_mapping_admission_and_fixed_safe_rejection_codes():
    valid = parse_telegram(make_frame(["1-3:0.2.8(50)", "1-0:1.8.1(1*kWh)"]))
    assert map_telegram_with_rejection(valid, RECEIVED)[1] is None
    broken = parse_telegram(
        make_frame(["1-3:0.2.8(50)", "1-0:1.8.1(1*kWh)"]).replace(b"1*kWh", b"2*kWh")
    )
    assert map_telegram_with_rejection(broken, RECEIVED) == (None, "invalid_crc")
    unverifiable_frame = make_frame(["1-3:0.2.8(50)", "1-0:1.8.1(1*kWh)"])
    unverifiable = parse_telegram(unverifiable_frame[:-6] + b"ZZZZ\r\n")
    assert map_telegram_with_rejection(unverifiable, RECEIVED)[1] == "unverifiable_crc"
    old_version = parse_telegram(make_frame(["1-3:0.2.8(40)", "1-0:1.8.1(1*kWh)"]))
    assert map_telegram_with_rejection(old_version, RECEIVED)[1] == "unsupported_profile_version"
    no_values = parse_telegram(make_frame(["1-3:0.2.8(50)", "1-0:1.8.1(-1*kWh)"]))
    assert map_telegram_with_rejection(no_values, RECEIVED)[1] == "no_electricity_metrics"


@pytest.mark.parametrize(
    "line,reason",
    [
        ("1-0:1.8.1(-1*kWh)", "invalid_value"),
        ("1-0:1.8.1(1*Wh)", "invalid_unit"),
    ],
)
def test_invalid_metric_is_null_and_safely_redacted(line, reason):
    parsed = parse_telegram(make_frame(["1-3:0.2.8(50)", line, "1-0:2.7.0(0*kW)"]))
    reading = map_telegram(parsed, RECEIVED)
    assert reading is not None
    assert reading.metrics["energy_import_t1_kwh"] is None
    assert reading.metrics["power_export_kw"] == Decimal(0)
    issue_code = "1-0:1.8.1"
    assert (issue_code, reason) in issue_pairs(reading)
    assert next(item for item in reading.fields if item.code == issue_code).raw_values == [
        "<redacted>"
    ]


def test_duplicate_metric_nulls_metric_and_deduplicates_sorted_issues():
    parsed = parse_telegram(
        make_frame(
            [
                "1-3:0.2.8(50)",
                "1-0:1.8.1(1*kWh)",
                "1-0:1.8.1(2*kWh)",
                "1-0:1.7.0(0*kW)",
                "1-0:1.7.0(0*kW)",
                "1-0:2.7.0(0*kW)",
            ]
        )
    )
    reading = map_telegram(parsed, RECEIVED)
    assert reading is not None
    assert reading.metrics["energy_import_t1_kwh"] is None
    assert reading.metrics["power_import_kw"] is None
    assert reading.metrics["power_export_kw"] == Decimal(0)
    assert issue_pairs(reading).count(("1-0:1.8.1", "duplicate")) == 1
    assert issue_pairs(reading) == sorted(issue_pairs(reading))
    assert all(item.redacted for item in reading.fields if item.code == "1-0:1.8.1")


def test_duplicate_only_electricity_metric_does_not_pass_admission():
    parsed = parse_telegram(
        make_frame(
            [
                "1-3:0.2.8(50)",
                "1-0:1.8.1(1*kWh)",
                "1-0:1.8.1(2*kWh)",
            ]
        )
    )
    assert map_telegram_with_rejection(parsed, RECEIVED) == (None, "no_electricity_metrics")


def test_missing_metrics_supplementary_fields_and_missing_identity_issue():
    parsed = parse_telegram(
        make_frame(
            [
                "1-3:0.2.8(50)",
                "1-0:1.8.1(0*kWh)",
                "0-0:96.7.21(3)",
                "9-9:9.9.9(8675309)",
            ]
        )
    )
    reading = map_telegram(parsed, RECEIVED)
    assert reading is not None
    assert reading.metrics["energy_import_t1_kwh"] == Decimal(0)
    assert reading.metrics["energy_import_t2_kwh"] is None
    assert ("1-0:1.8.2", "missing") in issue_pairs(reading)
    assert ("0-0:1.0.0", "missing") in issue_pairs(reading)
    assert ("0-0:96.1.1", "missing") in issue_pairs(reading)
    assert next(item for item in reading.fields if item.code == "9-9:9.9.9").raw_values == [
        "<redacted>"
    ]


def test_dst_transitions_suffix_mismatch_invalid_time_and_utc_input():
    summer = parse_telegram(
        make_frame(["1-3:0.2.8(50)", "1-0:1.8.1(1*kWh)", "0-0:1.0.0(260329033000S)"])
    )
    mapped = map_telegram(summer, RECEIVED)
    assert mapped is not None and mapped.meter_time_utc == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    assert ("0-0:1.0.0", "clock_offset_mismatch") not in issue_pairs(mapped)

    mismatch = parse_telegram(
        make_frame(["1-3:0.2.8(50)", "1-0:1.8.1(1*kWh)", "0-0:1.0.0(260715120000W)"])
    )
    mapped = map_telegram(mismatch, RECEIVED)
    assert mapped is not None and mapped.meter_time_utc == datetime(2026, 7, 15, 11, 0, tzinfo=UTC)
    assert ("0-0:1.0.0", "clock_offset_mismatch") in issue_pairs(mapped)

    impossible = parse_telegram(
        make_frame(["1-3:0.2.8(50)", "1-0:1.8.1(1*kWh)", "0-0:1.0.0(260230120000W)"])
    )
    mapped = map_telegram(impossible, RECEIVED)
    assert mapped is not None and mapped.meter_time_utc is None
    assert ("0-0:1.0.0", "invalid_timestamp") in issue_pairs(mapped)
    malformed = parse_telegram(
        make_frame(["1-3:0.2.8(50)", "1-0:1.8.1(1*kWh)", "0-0:1.0.0(private)"])
    )
    mapped = map_telegram(malformed, RECEIVED)
    assert mapped is not None and mapped.meter_time_raw is None
    assert ("0-0:1.0.0", "invalid_timestamp") in issue_pairs(mapped)
    with pytest.raises(ValueError, match="received_at_must_be_utc"):
        map_telegram(summer, datetime(2026, 7, 15, 12, 0))


def test_safe_issue_order_places_null_codes_first():
    from watt_profile.p1.profile import _sorted_issues

    assert _sorted_issues(
        [FieldIssue("b", "missing"), FieldIssue(None, "invalid_value"), FieldIssue("a", "missing")]
    ) == (
        FieldIssue(None, "invalid_value"),
        FieldIssue("a", "missing"),
        FieldIssue("b", "missing"),
    )
