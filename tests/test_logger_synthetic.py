"""Real synthetic bytes follow the production framer and profile seam."""

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta

from watt_profile.logger.app import create_app
from watt_profile.logger.settings import LoggerSettings
from watt_profile.logger.synthetic import SyntheticSerialFactory, synthetic_telegram
from watt_profile.p1.parser import TelegramFramer, parse_telegram
from watt_profile.p1.profile import map_telegram
from watt_profile.serial.interfaces import SerialConfig


def test_real_frames_timestamp_counter_crc_and_stable_fingerprint():
    now = datetime(2026, 10, 11, 12, tzinfo=UTC)
    readings = []
    for instant in (now, now + timedelta(seconds=1)):
        framer = TelegramFramer()
        frame = synthetic_telegram(instant)
        complete = []
        for offset in range(0, len(frame), 7):
            complete.extend(framer.feed(frame[offset : offset + 7]))
        assert complete == [frame]
        telegram = parse_telegram(complete[0])
        assert telegram.integrity == "valid"
        reading = map_telegram(telegram, instant)
        assert reading is not None
        assert reading.meter_time_utc == instant
        assert reading.metrics["voltage_l2_v"] is None
        readings.append((telegram, reading))
    assert readings[0][0].equipment_fingerprint == readings[1][0].equipment_fingerprint
    assert readings[0][0].equipment_fingerprint is not None
    assert (
        readings[1][1].metrics["energy_import_t1_kwh"]
        > readings[0][1].metrics["energy_import_t1_kwh"]
    )


def test_bounded_reads_once_per_second_close_interrupts_and_recreation():
    factory = SyntheticSerialFactory()
    port = factory(SerialConfig(path="synthetic"))
    framer = TelegramFramer()
    frames = []
    while not frames:
        chunk = port.read(4096)
        assert 0 < len(chunk) <= 64
        frames.extend(framer.feed(chunk))
    result = []
    waiter = threading.Thread(target=lambda: result.append(port.read(4096)))
    waiter.start()
    time.sleep(0.02)
    assert waiter.is_alive()  # The next frame is not emitted in a tight loop.
    port.close()
    waiter.join(0.5)
    assert not waiter.is_alive()
    assert result == [b""]
    assert port.read(1) == b""
    recreated = factory(SerialConfig(path="synthetic"))
    assert recreated.read(0) == b""
    assert recreated.read(3) == b"/WA"
    recreated.close()


def test_synthetic_app_preview_uses_real_source_without_injected_factory(tmp_path):
    password = tmp_path / "operator.secret"
    password.write_text("temporary-synthetic-password")
    settings = LoggerSettings.from_env(
        {
            "WATT_LOGGER_DATA_DIR": str(tmp_path / "data"),
            "WATT_LOGGER_OPERATOR_USER": "operator",
            "WATT_LOGGER_OPERATOR_PASSWORD_FILE": str(password),
            "WATT_LOGGER_INPUT": "synthetic",
        }
    )
    app = create_app(settings)
    assert not (tmp_path / "data").exists()

    async def run():
        async with app.router.lifespan_context(app):
            preview = await asyncio.to_thread(app.state.runtime.preview)
            assert preview.metrics.power_import_kw is not None
            assert app.state.runtime.get_status()["logger"].pending_count == 0

    asyncio.run(run())
