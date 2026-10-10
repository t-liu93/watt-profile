from __future__ import annotations

import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pytest

from watt_profile.p1.parser import (
    MAX_FRAME_BYTES,
    P1ParseError,
    TelegramFramer,
    dsmr_crc16,
    parse_telegram,
)


def frame(lines: list[str], header: str = "/ABC5\\2M") -> bytes:
    body = (header + "\r\n" + "\r\n".join(lines) + "\r\n!").encode("ascii")
    return body + f"{dsmr_crc16(body):04X}\r\n".encode("ascii")


def by_code(telegram, code):
    return [item for item in telegram.fields if item.code == code]


def test_incremental_framing_noise_restart_and_reset():
    first = frame(["1-0:1.8.1(1.2*kWh)"])
    second = frame(["1-0:2.8.1(2.3*kWh)"])
    framer = TelegramFramer()
    assert framer.feed(b"noise" + first[:7]) == []
    assert framer.feed(first[7:] + b"/partial\r\n" + second) == [first, second]
    assert framer.discarded_frames == 1
    assert framer.feed(b"/half") == []
    framer.reset()
    assert framer.feed(first) == [first]


def test_incremental_large_chunk_never_keeps_oversized_frame():
    framer = TelegramFramer()
    valid = frame(["1-0:1.8.1(1*kWh)"])
    out = framer.feed(b"/" + b"x" * (MAX_FRAME_BYTES * 2) + valid)
    assert out == [valid]
    assert framer.discarded_frames == 1
    assert len(framer._buffer) <= MAX_FRAME_BYTES


def test_crc_status_and_safe_fixed_diagnostics():
    good = frame(["1-0:1.8.1(1*kWh)"])
    assert parse_telegram(good).integrity == "valid"
    broken = good.replace(b"1.8.1", b"1.8.2")
    assert parse_telegram(broken).integrity == "invalid"
    unverifiable = good.replace(b"!", b"!ZZZZ")
    parsed = parse_telegram(unverifiable)
    assert parsed.integrity == "unverifiable"
    assert "raw" not in parsed.integrity_reason
    with pytest.raises(P1ParseError, match="non_ascii") as error:
        parse_telegram(good.replace(b"1.8.1", b"1.8.\xff"))
    assert error.value.reason_code == "non_ascii"
    assert str(error.value) == "non_ascii"


def test_sanitization_closed_grammars_and_unknown_codes():
    telegram = parse_telegram(
        frame(
            [
                "1-3:0.2.8(50)",
                "0-0:1.0.0(260101120000W)",
                "0-0:96.14.0(2)",
                "1-0:1.8.1(000001.234*kWh)",
                "1-0:1.8.1(000002.345*kWh)",
                "0-0:96.1.1(SYNTHETIC-METER-123)",
                "0-0:96.13.0(SECRET MESSAGE)",
                "9-9:9.9.9(12345)",
                "1-0:1.7.0(3.2*W)",
                "1-0:32.7.0(230.1*V)",
                "1-0:99.97.0(1)(0-0:96.7.19)(230101000000W)(0000000001*s)",
                "0-1:24.1.0(3)",
                "0-1:24.2.1(260101000000W)(000012.34*kWh)",
            ]
        )
    )
    assert telegram.timestamp == "260101120000W"
    assert by_code(telegram, "1-3:0.2.8")[0].raw_values == ["50"]
    assert [f.value for f in by_code(telegram, "1-0:1.8.1")] == [Decimal("1.234"), Decimal("2.345")]
    assert by_code(telegram, "1-0:1.7.0")[0].redacted
    assert by_code(telegram, "1-0:1.7.0")[0].issues[0].reason == "invalid_unit"
    assert by_code(telegram, "1-0:32.7.0")[0].value == Decimal("230.1")
    for code in ("0-0:96.1.1", "0-0:96.13.0", "9-9:9.9.9"):
        assert by_code(telegram, code)[0].raw_values == ["<redacted>"]
    assert not hasattr(telegram, "frame")
    serialized = json.dumps(asdict(telegram), default=str)
    assert "SYNTHETIC-METER-123" not in serialized
    assert "SECRET MESSAGE" not in serialized
    assert "comparison" not in repr(telegram)
    assert by_code(telegram, "1-0:99.97.0")[0].raw_values == [
        "1",
        "0-0:96.7.19",
        "230101000000W",
        "0000000001*s",
    ]


@pytest.mark.parametrize(
    "line", ["not an OBIS line", "1-0:1.8.1(unterminated", "1-0:1.8.1(a)(b)c)"]
)
def test_malformed_obis_line_rejected_without_echoing(line):
    with pytest.raises(P1ParseError) as error:
        parse_telegram(frame([line]))
    assert str(error.value) == "malformed_frame"
    assert line not in str(error.value)


def test_malformed_safe_values_are_redacted_with_issue_codes():
    telegram = parse_telegram(
        frame(
            [
                "0-0:1.0.0(private timestamp)",
                "1-0:1.8.1(-1*kWh)",
                "1-0:1.8.2(1*Wh)",
                "0-0:96.7.21(10)",
                "1-0:99.97.0(1)(2)(3)",
                "0-4:24.1.0(9999)",
            ]
        )
    )
    assert telegram.timestamp is None
    for code, issue in (
        ("0-0:1.0.0", "invalid_value"),
        ("1-0:1.8.1", "invalid_value"),
        ("1-0:1.8.2", "invalid_unit"),
        ("1-0:99.97.0", "invalid_value"),
        ("0-4:24.1.0", "invalid_value"),
    ):
        field = by_code(telegram, code)[0]
        assert field.raw_values == ["<redacted>"]
        assert field.value is None and field.unit is None
        assert field.issues[0].reason == issue
    assert by_code(telegram, "0-0:96.7.21")[0].value == 10


def test_synthetic_privacy_fixture_is_crc_valid_and_redacted():
    fixture = Path(__file__).parent / "fixtures/p1/synthetic_valid.telegram"
    telegram = parse_telegram(fixture.read_bytes())
    assert telegram.integrity == "valid"
    assert by_code(telegram, "0-0:96.1.1")[0].raw_values == ["<redacted>"]
    assert "SYNTHETIC-METER-123" not in repr(telegram)


def test_standard_mbus_units_and_redacted_wrong_unit():
    telegram = parse_telegram(
        frame(
            [
                "0-1:24.2.1(260101000000W)(000012.34*m3)",
                "0-2:24.2.1(260101000000W)(000012.34*GJ)",
                "0-3:24.2.1(260101000000W)(000012.34*kWh)",
                "0-4:24.2.1(260101000000W)(000012.34*MWh)",
            ]
        )
    )
    for code in ("0-1:24.2.1", "0-2:24.2.1", "0-3:24.2.1"):
        assert by_code(telegram, code)[0].value == Decimal("12.34")
    rejected = by_code(telegram, "0-4:24.2.1")[0]
    assert rejected.redacted and rejected.raw_values == ["<redacted>"]
    assert rejected.issues[0].reason == "invalid_unit"


def test_power_failure_log_empty_and_grouped_entries():
    telegram = parse_telegram(
        frame(
            [
                "1-0:99.97.0(0)(0-0:96.7.19)",
                "1-0:99.97.0(2)(0-0:96.7.19)(260101120000W)(0000000240*s)"
                "(251231235959W)(0000000301*s)",
            ]
        )
    )
    logs = by_code(telegram, "1-0:99.97.0")
    assert logs[0].raw_values == ["0", "0-0:96.7.19"]
    assert not logs[0].redacted and logs[0].value is None
    assert logs[1].raw_values == [
        "2",
        "0-0:96.7.19",
        "260101120000W",
        "0000000240*s",
        "251231235959W",
        "0000000301*s",
    ]
    assert not logs[1].redacted and logs[1].value is None


@pytest.mark.parametrize(
    "groups",
    [
        "(2)(0-0:96.7.19)(260101120000W)(240*s)",
        "(1)(0-0:96.7.18)(260101120000W)(240*s)",
        "(1)(0-0:96.7.19)(bad timestamp)(240*s)",
        "(1)(0-0:96.7.19)(260101120000W)(240*min)",
        "(11)(0-0:96.7.19)",
        "(0)(0-0:96.7.19)(260101120000W)(240*s)",
    ],
)
def test_malformed_power_failure_log_is_redacted(groups):
    telegram = parse_telegram(frame([f"1-0:99.97.0{groups}"]))
    field = by_code(telegram, "1-0:99.97.0")[0]
    assert field.redacted and field.raw_values == ["<redacted>"]
    assert field.issues[0].reason == "invalid_value"


def test_failure_and_phase_counters_require_integer_grammar():
    telegram = parse_telegram(
        frame(
            [
                "0-0:96.7.21(10.5)",
                "0-0:96.7.9(11)",
                "1-0:32.32.0(3.0)",
                "1-0:52.36.0(4)",
            ]
        )
    )
    for code in ("0-0:96.7.21", "1-0:32.32.0"):
        field = by_code(telegram, code)[0]
        assert field.redacted and field.raw_values == ["<redacted>"]
        assert field.issues[0].reason == "invalid_value"
    assert by_code(telegram, "0-0:96.7.9")[0].value == 11
    assert by_code(telegram, "1-0:52.36.0")[0].value == 4
