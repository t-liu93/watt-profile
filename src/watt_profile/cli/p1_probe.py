"""Sanitized, read-only DSMR P1 diagnostic probe."""

from __future__ import annotations

import argparse
import errno
import math
import sys
from typing import TextIO

from watt_profile.p1.parser import P1ParseError, TelegramFramer, parse_telegram
from watt_profile.p1.profile import map_telegram_with_rejection
from watt_profile.serial.interfaces import (
    Clock,
    PySerialFactory,
    ReadOnlySerial,
    SerialConfig,
    SerialFactory,
    SystemClock,
)


def _duration(value: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("duration must be finite and positive") from exc
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("duration must be finite and positive")
    return result


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be positive") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only DSMR P1 diagnostic probe.")
    parser.add_argument("--device", required=True, help="Serial device path")
    parser.add_argument("--duration", type=_duration, default=15.0, help="Seconds (default: 15)")
    parser.add_argument("--baudrate", type=_positive_int, default=115200)
    parser.add_argument("--bytesize", type=int, choices=(5, 6, 7, 8), default=8)
    parser.add_argument("--parity", type=str.upper, choices=("N", "E", "O", "M", "S"), default="N")
    parser.add_argument("--stopbits", type=float, choices=(1, 1.5, 2), default=1)
    return parser


def _serial_error_code(exc: BaseException) -> str:
    number = getattr(exc, "errno", None)
    if number in (errno.EBUSY, errno.EAGAIN, errno.EWOULDBLOCK):
        return "serial_busy"
    if number in (errno.EACCES, errno.EPERM):
        return "serial_permission"
    if number in (errno.ENOENT, errno.ENODEV, errno.EIO):
        return "serial_unavailable"
    return "serial_error"


def run_probe(
    args: argparse.Namespace,
    *,
    serial_factory: SerialFactory | None = None,
    clock: Clock | None = None,
    output: TextIO | None = None,
    error_output: TextIO | None = None,
) -> int:
    factory = serial_factory or PySerialFactory()
    active_clock = clock or SystemClock()
    stdout = output or sys.stdout
    stderr = error_output or sys.stderr
    port: ReadOnlySerial | None = None
    serial_error = False
    frames = admitted = rejected = 0
    framer = TelegramFramer()
    try:
        port = factory(
            SerialConfig(
                path=args.device,
                baudrate=args.baudrate,
                bytesize=args.bytesize,
                parity=args.parity,
                stopbits=args.stopbits,
                timeout=1,
            )
        )
        deadline = active_clock.monotonic() + args.duration
        while active_clock.monotonic() < deadline:
            chunk = port.read(4096)
            for frame in framer.feed(chunk):
                frames += 1
                try:
                    telegram = parse_telegram(frame)
                    reading, _reason = map_telegram_with_rejection(telegram, active_clock.utc_now())
                except (P1ParseError, ValueError):
                    reading = None
                if reading is None:
                    rejected += 1
                    continue
                admitted += 1
                values = ", ".join(
                    f"{name}={value if value is not None else 'null'}"
                    for name, value in reading.metrics.items()
                )
                print(f"reading: {values}", file=stdout)
    except KeyboardInterrupt:
        print("error=interrupted", file=stderr)
        serial_error = True
    except Exception as exc:
        # Never include exception text: serial drivers may include the device path.
        print(f"error={_serial_error_code(exc)}", file=stderr)
        serial_error = True
    finally:
        if port is not None:
            try:
                port.close()
            except Exception as exc:
                print(f"error={_serial_error_code(exc)}", file=stderr)
                serial_error = True

    print(f"frames={frames} admitted={admitted} rejected={rejected}", file=stdout)
    if not serial_error and admitted:
        return 0
    if not serial_error:
        print("error=no_valid_reading", file=stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    return run_probe(build_parser().parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
