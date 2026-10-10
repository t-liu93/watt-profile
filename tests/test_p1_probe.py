"""Offline-only tests for serial configuration and the diagnostic probe."""

from __future__ import annotations

import errno
import io
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from watt_profile.cli.p1_probe import build_parser, run_probe
from watt_profile.serial.interfaces import PySerialFactory, SerialConfig

FIXTURE = Path(__file__).parent / "fixtures/p1/profile/single_phase.telegram"


class FakeClock:
    def __init__(self):
        self.tick = 0.0

    def monotonic(self):
        self.tick += 0.5
        return self.tick

    def utc_now(self):
        return datetime(2026, 10, 10, tzinfo=UTC)

    def wait(self, seconds):
        self.tick += seconds


class FakePort:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = False
        self.read_calls = 0

    def read(self, size):
        self.read_calls += 1
        return next(self.chunks, b"")

    def close(self):
        self.closed = True


class Factory:
    def __init__(self, port=None, error=None):
        self.port = port
        self.error = error
        self.config = None

    def __call__(self, config: SerialConfig):
        self.config = config
        if self.error:
            raise self.error
        return self.port


def args(*overrides):
    return build_parser().parse_args(["--device", "/dev/fake", *overrides])


def test_serial_defaults_overrides_and_success_require_admitted_reading():
    frame = FIXTURE.read_bytes()
    port = FakePort([frame])
    factory = Factory(port)
    out, err = io.StringIO(), io.StringIO()
    assert (
        run_probe(args(), serial_factory=factory, clock=FakeClock(), output=out, error_output=err)
        == 0
    )
    assert factory.config == SerialConfig("/dev/fake", timeout=1)
    assert factory.config.baudrate == 115200
    assert factory.config.bytesize == 8 and factory.config.parity == "N"
    assert factory.config.stopbits == 1 and port.closed
    assert "power_import_l1_kw=" in out.getvalue()
    assert "frames=1 admitted=1 rejected=0" in out.getvalue()
    assert not err.getvalue()
    overridden = args("--baudrate", "9600", "--bytesize", "7", "--parity", "e", "--stopbits", "2")
    override_factory = Factory(FakePort([]))
    assert (
        run_probe(
            overridden,
            serial_factory=override_factory,
            clock=FakeClock(),
            output=io.StringIO(),
            error_output=io.StringIO(),
        )
        == 1
    )
    assert override_factory.config == SerialConfig(
        "/dev/fake", baudrate=9600, bytesize=7, parity="E", stopbits=2, timeout=1
    )


def test_linux_adapter_requests_pyserial_exclusive_open(monkeypatch):
    captured = {}
    handle = FakePort([])

    def open_serial(**kwargs):
        captured.update(kwargs)
        return handle

    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=open_serial))
    monkeypatch.setattr("watt_profile.serial.interfaces.sys.platform", "linux")
    assert PySerialFactory()(SerialConfig("/dev/fake")) is handle
    assert captured == {
        "port": "/dev/fake",
        "baudrate": 115200,
        "bytesize": 8,
        "parity": "N",
        "stopbits": 1,
        "timeout": 1,
        "exclusive": True,
    }


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux exclusive adapter")
@pytest.mark.parametrize("number", [errno.EAGAIN, errno.EWOULDBLOCK])
def test_linux_exclusive_lock_conflict_is_busy_and_closes_fake_descriptor(number):
    import serial.serialposix

    out, err = io.StringIO(), io.StringIO()
    descriptor = 314159
    device = "/dev/SYNTHETIC_EXCLUSIVE_CONFLICT"
    # Keep pyserial's real errno wrapping while replacing all reached OS operations.
    with (
        patch("serial.serialposix.os.open", return_value=descriptor) as fake_open,
        patch(
            "serial.serialposix.fcntl.flock",
            side_effect=BlockingIOError(number, "SYNTHETIC_LOCK_CONFLICT"),
        ) as fake_lock,
        patch("serial.serialposix.os.close") as fake_close,
    ):
        result = run_probe(
            build_parser().parse_args(["--device", device]),
            clock=FakeClock(),
            output=out,
            error_output=err,
        )

    assert result == 1
    assert fake_open.call_args.args[0] == device
    fake_lock.assert_called_once_with(
        descriptor, serial.serialposix.fcntl.LOCK_EX | serial.serialposix.fcntl.LOCK_NB
    )
    fake_close.assert_called_once_with(descriptor)
    assert out.getvalue() == "frames=0 admitted=0 rejected=0\n"
    assert err.getvalue() == "error=serial_busy\n"


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_duration_must_be_finite_and_positive(value):
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["--device", "/dev/fake", "--duration", value])
    assert error.value.code == 2


def test_rejected_frame_and_timeout_fail_and_close():
    rejected = b"/fake\r\n!0000\r\n"
    port = FakePort([rejected])
    out, err = io.StringIO(), io.StringIO()
    result = run_probe(
        args(), serial_factory=Factory(port), clock=FakeClock(), output=out, error_output=err
    )
    assert result == 1 and port.closed
    assert "frames=1 admitted=0 rejected=1" in out.getvalue()
    assert "error=no_valid_reading" in err.getvalue()


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (OSError(errno.EBUSY, "private path sentinel"), "serial_busy"),
        (OSError(errno.EACCES, "private path sentinel"), "serial_permission"),
        (OSError(errno.ENODEV, "private path sentinel"), "serial_unavailable"),
    ],
)
def test_open_errors_are_fixed_and_never_echo_exception(error, code):
    out, err = io.StringIO(), io.StringIO()
    assert (
        run_probe(
            args(),
            serial_factory=Factory(error=error),
            clock=FakeClock(),
            output=out,
            error_output=err,
        )
        == 1
    )
    assert f"error={code}" in err.getvalue()
    assert "private path sentinel" not in err.getvalue()


def test_read_interruption_and_close_failure_return_failure_and_close():
    class InterruptedPort(FakePort):
        def read(self, size):
            raise KeyboardInterrupt

    port = InterruptedPort([])
    out, err = io.StringIO(), io.StringIO()
    assert (
        run_probe(
            args(), serial_factory=Factory(port), clock=FakeClock(), output=out, error_output=err
        )
        == 1
    )
    assert port.closed and "error=interrupted" in err.getvalue()

    class ReadFailure(FakePort):
        def read(self, size):
            raise OSError(errno.EIO, "device unavailable at private path")

    port = ReadFailure([])
    out, err = io.StringIO(), io.StringIO()
    assert (
        run_probe(
            args(), serial_factory=Factory(port), clock=FakeClock(), output=out, error_output=err
        )
        == 1
    )
    assert port.closed and "error=serial_unavailable" in err.getvalue()
    assert "private path" not in err.getvalue()

    class BadClose(FakePort):
        def close(self):
            self.closed = True
            raise OSError("path sentinel")

    port = BadClose([FIXTURE.read_bytes()])
    out, err = io.StringIO(), io.StringIO()
    assert (
        run_probe(
            args(), serial_factory=Factory(port), clock=FakeClock(), output=out, error_output=err
        )
        == 1
    )
    assert port.closed and "error=serial_error" in err.getvalue()
    assert "path sentinel" not in err.getvalue()


def test_raw_output_is_not_a_cli_option():
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["--device", "/dev/fake", "--raw-output", "/tmp/raw"])
    assert error.value.code == 2
