"""Development-only DSMR byte source using the production serial seam."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from watt_profile.p1.parser import dsmr_crc16
from watt_profile.serial.interfaces import SerialConfig


def synthetic_telegram(now: datetime) -> bytes:
    """Return one CRC-valid single-phase telegram with an immutable fake meter ID."""
    local = now.astimezone(ZoneInfo("Europe/Amsterdam"))
    stamp = local.strftime("%y%m%d%H%M%S") + ("S" if local.dst() else "W")
    counter = Decimal(int(now.timestamp())) / Decimal(1000)
    lines = [
        "/WATT5\\SYNTHETIC",
        "1-3:0.2.8(50)",
        f"0-0:1.0.0({stamp})",
        "0-0:96.1.1(WATT-SYNTHETIC-METER-01)",
        f"1-0:1.8.1({counter:.3f}*kWh)",
        "1-0:1.8.2(00002345.600*kWh)",
        "1-0:2.8.1(00000000.000*kWh)",
        "1-0:2.8.2(00000004.200*kWh)",
        "1-0:1.7.0(0001.200*kW)",
        "1-0:2.7.0(0000.000*kW)",
        "1-0:32.7.0(230.1*V)",
        "1-0:31.7.0(5.2*A)",
        "1-0:21.7.0(1.200*kW)",
        "1-0:22.7.0(0.000*kW)",
    ]
    body = ("\r\n".join(lines) + "\r\n!").encode("ascii")
    return body + f"{dsmr_crc16(body):04X}\r\n".encode("ascii")


class SyntheticSerial:
    """One bounded frame per second; close immediately cancels a pending read."""

    def __init__(self):
        self._closed = threading.Event()
        self._pending = b""
        self._next = time.monotonic()

    def read(self, size: int) -> bytes:
        if size <= 0 or self._closed.is_set():
            return b""
        if not self._pending:
            if self._closed.wait(max(0, self._next - time.monotonic())):
                return b""
            self._pending = synthetic_telegram(datetime.now(UTC))
            self._next = time.monotonic() + 1
        chunk, self._pending = self._pending[: min(size, 64)], self._pending[min(size, 64) :]
        return chunk

    def close(self) -> None:
        self._closed.set()
        self._pending = b""


class SyntheticSerialFactory:
    def __call__(self, config: SerialConfig) -> SyntheticSerial:
        return SyntheticSerial()
