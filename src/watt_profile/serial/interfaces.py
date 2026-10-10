"""Read-only serial and clock contracts used by diagnostics and collection."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SerialConfig:
    """Immutable DSMR serial settings, with explicit diagnostic overrides."""

    path: str
    baudrate: int = 115200
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    timeout: float = 1


class ReadOnlySerial(Protocol):
    def read(self, size: int) -> bytes: ...

    def close(self) -> None: ...


class SerialFactory(Protocol):
    def __call__(self, config: SerialConfig) -> ReadOnlySerial: ...


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def utc_now(self) -> datetime: ...

    def wait(self, seconds: float) -> None: ...


class SystemClock:
    """System clock implementation; waits are interruptible by KeyboardInterrupt."""

    def monotonic(self) -> float:
        return time.monotonic()

    def utc_now(self) -> datetime:
        return datetime.now(UTC)

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)


class PySerialFactory:
    """Open one exclusive, read-only-at-the-call-site Linux serial handle."""

    def __call__(self, config: SerialConfig) -> ReadOnlySerial:
        import serial

        kwargs: dict[str, object] = {
            "port": config.path,
            "baudrate": config.baudrate,
            "bytesize": config.bytesize,
            "parity": config.parity,
            "stopbits": config.stopbits,
            "timeout": config.timeout,
        }
        if sys.platform.startswith("linux"):
            kwargs["exclusive"] = True
        return serial.Serial(**kwargs)  # type: ignore[return-value]
