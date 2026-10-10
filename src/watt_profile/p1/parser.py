"""Bounded, privacy-preserving DSMR P1 framing and parsing."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

MAX_FRAME_BYTES = 16_384
_REDACTED = "<redacted>"
_OBIS_LINE = re.compile(r"(?P<code>\d+-\d+:\d+\.\d+\.\d+)(?P<values>(?:\([^()]*\))*)\Z")
_NUMERIC = re.compile(r"(?P<number>\d+(?:\.\d+)?)(?:\*(?P<unit>[A-Za-z0-9]+))?\Z")
_TIMESTAMP = re.compile(r"\d{12}[SW]\Z")
_VERSION = re.compile(r"\d{2}\Z")
_TARIFF = re.compile(r"[12]\Z")
_EQUIPMENT_CODE = re.compile(r"0-0:96\.1\.1\Z")
_SAFE_NUMERIC_UNITS = {
    "1-0:1.8.1": "kWh",
    "1-0:1.8.2": "kWh",
    "1-0:2.8.1": "kWh",
    "1-0:2.8.2": "kWh",
    "1-0:1.7.0": "kW",
    "1-0:2.7.0": "kW",
    **{f"1-0:{n}.7.0": "V" for n in (32, 52, 72)},
    **{f"1-0:{n}.7.0": "A" for n in (31, 51, 71)},
    **{f"1-0:{n}.7.0": "kW" for n in (21, 41, 61, 22, 42, 62)},
}
_SAFE_COUNTERS = {"0-0:96.7.21", "0-0:96.7.9"}
_SAFE_EVENT_CODES = {"1-0:99.97.0"}
_SAFE_PHASE_COUNTERS = {f"1-0:{n}.{suffix}.0" for n in (32, 52, 72) for suffix in ("32", "36")}


@dataclass(frozen=True)
class FieldIssue:
    code: str
    reason: str


@dataclass(frozen=True)
class SanitizedField:
    code: str
    raw_values: list[str]
    value: Decimal | None = None
    unit: str | None = None
    redacted: bool = False
    issues: tuple[FieldIssue, ...] = ()


@dataclass(frozen=True)
class P1Telegram:
    frame_length: int
    integrity: str
    integrity_reason: str
    timestamp: str | None
    equipment_fingerprint: str | None = field(default=None, repr=False)
    fields: tuple[SanitizedField, ...] = ()


class P1ParseError(ValueError):
    """A parser error with a stable code and no received content."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class TelegramFramer:
    """Incrementally extracts bounded frames, recovering at line-start headers."""

    def __init__(self, max_frame_bytes: int = MAX_FRAME_BYTES) -> None:
        if not 1 <= max_frame_bytes <= MAX_FRAME_BYTES:
            raise ValueError("invalid_max_frame_bytes")
        self.max_frame_bytes = max_frame_bytes
        self.discarded_frames = 0
        self._buffer = bytearray()

    def reset(self) -> None:
        if self._buffer:
            self.discarded_frames += 1
        self._buffer.clear()

    def feed(self, chunk: bytes) -> list[bytes]:
        frames: list[bytes] = []
        for byte in chunk:
            if not self._buffer:
                if byte == ord("/"):
                    self._buffer.append(byte)
                continue
            if byte == ord("/") and self._buffer.endswith(b"\n"):
                self.discarded_frames += 1
                self._buffer.clear()
                self._buffer.append(byte)
                continue
            if len(self._buffer) >= self.max_frame_bytes:
                self.discarded_frames += 1
                self._buffer.clear()
                if byte == ord("/"):
                    self._buffer.append(byte)
                continue
            self._buffer.append(byte)
            if byte == ord("\n") and _has_complete_footer(self._buffer):
                frames.append(bytes(self._buffer))
                self._buffer.clear()
        return frames


def _has_complete_footer(frame: bytearray) -> bool:
    bang = frame.rfind(b"!")
    return bang >= 0 and (bang == 0 or frame[bang - 1] == 10) and frame.endswith(b"\r\n")


def dsmr_crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def parse_telegram(frame: bytes) -> P1Telegram:
    if len(frame) > MAX_FRAME_BYTES:
        raise P1ParseError("frame_too_large")
    if not frame.startswith(b"/") or not frame.endswith(b"\r\n"):
        raise P1ParseError("malformed_frame")
    bang = frame.rfind(b"!")
    if bang < 0 or (bang and frame[bang - 1] != 10):
        raise P1ParseError("malformed_frame")
    footer = frame[bang + 1 : -2]
    if b"\r\n" in footer:
        raise P1ParseError("malformed_frame")
    if len(footer) != 4 or any(byte not in b"0123456789abcdefABCDEF" for byte in footer):
        status, reason = "unverifiable", "malformed_crc_footer"
    else:
        expected = int(footer, 16)
        actual = dsmr_crc16(frame[: bang + 1])
        if expected == actual:
            status, reason = "valid", "crc_verified"
        else:
            status, reason = "invalid", "crc_mismatch"
    # CRC is established before decoding or any value redaction.
    if any(byte > 0x7F for byte in frame):
        raise P1ParseError("non_ascii")
    lines = frame[:bang].decode("ascii").split("\r\n")
    fields: list[SanitizedField] = []
    fingerprint: str | None = None
    for line in lines[1:]:
        if not line:
            continue
        match = _OBIS_LINE.fullmatch(line)
        if match is None:
            raise P1ParseError("malformed_frame")
        code = match.group("code")
        values = re.findall(r"\(([^()]*)\)", match.group("values"))
        safe, numeric, unit, issue = _sanitize(code, values)
        if _EQUIPMENT_CODE.fullmatch(code) and values:
            fingerprint = hashlib.sha256(values[-1].encode("ascii")).hexdigest()
        fields.append(
            SanitizedField(
                code,
                safe,
                numeric,
                unit,
                issue is not None,
                (FieldIssue(code, issue),) if issue else (),
            )
        )
    timestamp = next(
        (item.raw_values[0] for item in fields if item.code == "0-0:1.0.0" and not item.redacted),
        None,
    )
    return P1Telegram(len(frame), status, reason, timestamp, fingerprint, tuple(fields))


def _sanitize(
    code: str, values: list[str]
) -> tuple[list[str], Decimal | None, str | None, str | None]:
    if code == "0-0:1.0.0":
        ok = len(values) == 1 and _TIMESTAMP.fullmatch(values[0])
        return (values if ok else [_REDACTED], None, None, None if ok else "invalid_value")
    if code == "1-3:0.2.8":
        ok = len(values) == 1 and _VERSION.fullmatch(values[0])
        return (values if ok else [_REDACTED], None, None, None if ok else "invalid_value")
    if code == "0-0:96.14.0":
        ok = len(values) == 1 and _TARIFF.fullmatch(values[0])
        return (values if ok else [_REDACTED], None, None, None if ok else "invalid_value")
    if _EQUIPMENT_CODE.fullmatch(code) or code == "0-0:96.13.0":
        return [_REDACTED], None, None, "redacted"
    expected = _SAFE_NUMERIC_UNITS.get(code)
    mbus_match = re.fullmatch(r"0-([1-4]):24\.(1\.0|2\.1)", code)
    if mbus_match and mbus_match.group(2) == "1.0":
        ok = len(values) == 1 and re.fullmatch(r"\d{1,3}", values[0]) is not None
        return (values if ok else [_REDACTED], None, None, None if ok else "invalid_value")
    if mbus_match:
        expected = "kWh"
    if expected or code in _SAFE_COUNTERS or code in _SAFE_PHASE_COUNTERS:
        value_index = 1 if mbus_match and mbus_match.group(2) == "2.1" else 0
        valid_count = 2 if value_index else 1
        if len(values) != valid_count:
            return [_REDACTED], None, None, "invalid_value"
        if value_index and _TIMESTAMP.fullmatch(values[0]) is None:
            return [_REDACTED], None, None, "invalid_value"
        match = _NUMERIC.fullmatch(values[value_index])
        if match is None:
            return [_REDACTED], None, None, "invalid_value"
        unit = match.group("unit")
        if mbus_match and unit not in {"m3", "GJ", "kWh"}:
            return [_REDACTED], None, None, "invalid_unit"
        if expected and not mbus_match and unit != expected:
            return [_REDACTED], None, None, "invalid_unit"
        if not expected and unit is not None:
            return [_REDACTED], None, None, "invalid_unit"
        if code in _SAFE_COUNTERS | _SAFE_PHASE_COUNTERS and "." in match.group("number"):
            return [_REDACTED], None, None, "invalid_value"
        try:
            value = Decimal(match.group("number"))
        except InvalidOperation:
            return [_REDACTED], None, None, "invalid_value"
        return values, value, unit, None
    if code in _SAFE_EVENT_CODES:
        ok = _valid_power_failure_log(values)
        return (values if ok else [_REDACTED], None, None, None if ok else "invalid_value")
    # Unknown and free-text values never escape.
    return [_REDACTED], None, None, "redacted"


def _valid_power_failure_log(values: list[str]) -> bool:
    """Validate z, capture object ID, then z timestamp/duration pairs."""
    if (
        len(values) < 2
        or re.fullmatch(r"(?:0|[1-9]|10)", values[0]) is None
        or values[1] != "0-0:96.7.19"
    ):
        return False
    count = int(values[0])
    if count > 10:
        return False
    if count == 0:
        return len(values) == 2
    if len(values) != 2 + count * 2:
        return False
    for index in range(2, len(values), 2):
        if _TIMESTAMP.fullmatch(values[index]) is None:
            return False
        duration = re.fullmatch(r"(?P<seconds>\d+)\*s", values[index + 1])
        if duration is None:
            return False
    return True
