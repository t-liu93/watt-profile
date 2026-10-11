"""Runtime acceptance uses only temporary SQLite and synthetic fake serial bytes."""

import errno
import queue
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from watt_profile.logger.deployment import DeploymentCommand, SettingsCommand
from watt_profile.logger.runtime import LoggerRuntime
from watt_profile.logger.storage.database import create_database, migrate
from watt_profile.logger.storage.repository import LocalSettings, LoggerRepository
from watt_profile.p1.parser import MAX_FRAME_BYTES

FRAME = Path("tests/fixtures/p1/profile/single_phase.telegram").read_bytes()
NOW = datetime(2026, 1, 1, tzinfo=UTC)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.condition = threading.Condition()

    def monotonic(self):
        return self.now

    def utc_now(self):
        return NOW + timedelta(seconds=self.now)

    def advance(self, seconds):
        with self.condition:
            self.now += seconds
            self.condition.notify_all()

    def wait_interruptible(self, seconds, event):
        deadline = self.now + seconds
        with self.condition:
            while self.now < deadline and not event.is_set():
                self.condition.wait(0.01)


class Port:
    def __init__(self):
        self.chunks = queue.Queue()
        self.closed = False

    def read(self, size):
        try:
            item = self.chunks.get(timeout=0.02)
        except queue.Empty:
            return b""
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True
        self.chunks.put(b"")


class Factory:
    def __init__(self):
        self.ports = []
        self.configs = []
        self.failures = []

    def __call__(self, config):
        assert all(p.closed for p in self.ports)
        self.configs.append(config)
        if self.failures:
            raise self.failures.pop(0)
        port = Port()
        self.ports.append(port)
        return port


def eventually(check):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.005)
    assert check()


@pytest.fixture
def runtime(tmp_path):
    engine = create_database(tmp_path / "logger.sqlite3")
    migrate(engine)
    repo = LoggerRepository(engine)
    from uuid import uuid4

    repo.initialize_installation(uuid4(), NOW)
    repo.save_settings(LocalSettings(serial_path="/fake"))
    clock, factory = FakeClock(), Factory()
    runtime = LoggerRuntime(repo, clock=clock, serial_factory=factory, health_guard=lambda: None)
    site = runtime.create_site()
    runtime.switch_site(DeploymentCommand(site_id=site.site_id, expected_session_id=None))
    yield runtime, repo, clock, factory
    if not runtime._closed:
        runtime._sampler = None
        runtime.shutdown()
    engine.dispose()


def session(runtime):
    return runtime.operations.state.session.descriptor.session_id


def start(runtime, factory):
    runtime.start_collection(session(runtime))
    eventually(lambda: factory.ports)
    eventually(lambda: runtime.get_status()["logger"].collection_state == "collecting")
    return factory.ports[-1]


def feed(runtime, port, frame=FRAME):
    before = runtime._sequence
    port.chunks.put(frame)
    eventually(lambda: runtime._sequence > before)


def threaded(action):
    result = []

    def run():
        try:
            result.append(action())
        except Exception as exc:
            result.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def test_collect_boundaries_preview_and_pause_tail(runtime):
    rt, repo, clock, factory = runtime
    port = start(rt, factory)
    assert factory.configs[0].timeout == 1
    assert factory.configs[0].baudrate == 115200
    thread, result = threaded(rt.preview)
    eventually(lambda: rt._preview_active)
    clock.advance(1)
    feed(rt, port)
    thread.join(1)
    assert not isinstance(result[0], Exception)
    assert "fingerprint" not in result[0].model_dump_json()
    assert len(factory.ports) == 1
    assert repo.get_status().pending_count == 0
    thread, result = threaded(lambda: rt.pause(session(rt)))
    eventually(lambda: port.closed)
    assert not repo.load_lifecycle().session.collection_enabled
    assert rt.get_status()["logger"].collection_state == "stopping"
    with pytest.raises(ValueError, match="busy"):
        rt.start_collection(session(rt))
    clock.advance(9)
    thread.join(2)
    assert not thread.is_alive()
    assert result[0].result == "ok"
    assert repo.get_status().pending_count == 1
    clock.advance(50)
    assert repo.get_status().pending_count == 1


@pytest.mark.parametrize("binding_delay", [3, 13])
def test_reader_finalization_uses_current_utc_after_binding_delay(
    runtime, monkeypatch, binding_delay
):
    rt, repo, clock, factory = runtime
    # Isolate reader-triggered finalization; the boundary worker normally competes
    # for the reception lock held throughout frame binding and admission.
    monkeypatch.setattr(rt, "_windows", lambda: None)
    port = start(rt, factory)
    clock.advance(1)
    feed(rt, port)
    bind_meter = repo.bind_meter

    def delayed_bind(*args):
        clock.advance(binding_delay)
        return bind_meter(*args)

    monkeypatch.setattr(repo, "bind_meter", delayed_bind)
    clock.advance(9)
    feed(rt, port)
    group = repo.select_pending_group(10, 64000)
    [first] = group.batch.records
    assert first.received_at == NOW + timedelta(seconds=1)
    assert first.sampled_at == NOW + timedelta(seconds=10 + binding_delay)
    assert rt.get_status()["latest_live_reading"].received_at == NOW + timedelta(seconds=10)
    # Even processing delayed past t=20 must retain the t=10 frame in [10, 20).
    assert rt._sampler.next_boundary == 20
    clock.advance(max(0, 20 - clock.monotonic()))
    with rt._reception, rt._state:
        rt._persist(rt._sampler.advance(clock.monotonic(), clock.utc_now()))
    group = repo.select_pending_group(10, 64000)
    first, second = group.batch.records
    assert second.received_at == NOW + timedelta(seconds=10)
    assert second.sampled_at == clock.utc_now()
    assert first.record_id != second.record_id


@pytest.mark.parametrize("outcome", ["accepted", "parse_rejected", "profile_rejected"])
def test_reader_releases_complete_raw_frames_before_next_blocking_read(
    runtime, monkeypatch, outcome
):
    rt, _, _, factory = runtime
    frame = FRAME
    if outcome == "parse_rejected":
        frame = FRAME.replace(b"S", b"\xff", 1)
    elif outcome == "profile_rejected":
        frame = FRAME.replace(b"1.8.1", b"1.8.9")
    waiting, release = threading.Event(), threading.Event()
    original_read = Port.read
    consumed = False

    def blocking_read(port, size):
        nonlocal consumed
        if consumed:
            waiting.set()
            assert release.wait(2)
            return b""
        chunk = original_read(port, size)
        if chunk:
            consumed = True
        return chunk

    monkeypatch.setattr(Port, "read", blocking_read)
    port = start(rt, factory)
    port.chunks.put(frame)
    try:
        assert waiting.wait(2)
        assert rt._sequence == (1 if outcome == "accepted" else 0)
        assert rt.rejected_frames == (0 if outcome == "accepted" else 1)

        def contains_raw(value):
            if isinstance(value, (bytes, bytearray)):
                return frame in value
            if isinstance(value, (list, tuple)):
                return any(contains_raw(item) for item in value)
            return False

        stack = sys._current_frames()[rt._reader.ident]
        runtime_scopes = 0
        while stack is not None:
            if stack.f_code.co_filename.endswith("logger/runtime.py"):
                runtime_scopes += 1
                assert not any(contains_raw(value) for value in stack.f_locals.values())
                framer = stack.f_locals.get("framer")
                if framer is not None:
                    assert not framer._buffer
            stack = stack.f_back
        assert runtime_scopes >= 2
    finally:
        release.set()


def test_idle_preview_requires_no_session_and_closes(runtime):
    rt, repo, clock, factory = runtime
    thread, result = threaded(rt.preview)
    eventually(lambda: factory.ports)
    factory.ports[-1].chunks.put(FRAME)
    thread.join(2)
    assert not isinstance(result[0], Exception)
    assert factory.ports[-1].closed
    assert repo.get_status().pending_count == 0
    assert repo.load_lifecycle().source.meter_fingerprint is None
    assert rt.get_status()["logger"].collection_state == "idle"


def test_preview_cancel_and_timeout_release_temporary_owner(runtime):
    rt, _, clock, factory = runtime
    cancelled = threading.Event()
    thread, result = threaded(lambda: rt.preview(cancellation=cancelled))
    eventually(lambda: factory.ports)
    cancelled.set()
    thread.join(2)
    assert str(result[0]) == "preview_cancelled"
    assert factory.ports[-1].closed
    thread, result = threaded(rt.preview)
    eventually(lambda: len(factory.ports) == 2)
    clock.advance(15)
    thread.join(2)
    assert str(result[0]) == "preview_timeout"
    assert factory.ports[-1].closed


def test_reconnect_backoff_cancellation_and_invalid_frames(runtime):
    rt, repo, clock, factory = runtime
    factory.failures = [OSError(errno.EACCES, "private-path")]
    rt.start_collection(session(rt))
    eventually(lambda: rt.get_status()["logger"].latest_collection_error == "serial_permission")
    assert len(factory.configs) == 1
    clock.advance(1)
    eventually(lambda: factory.ports)
    port = factory.ports[-1]
    port.chunks.put(FRAME.replace(b"1.8.1", b"1.8.9"))
    eventually(lambda: rt.rejected_frames == 1)
    feed(rt, port)
    port.chunks.put(OSError(errno.EIO, "private-path"))
    eventually(lambda: rt.get_status()["logger"].collection_state == "reconnecting")
    # Discard test candidate to isolate cancellable reconnect ownership.
    rt._sampler = None
    rt.pause(session(rt))
    assert port.closed
    assert repo.get_status().pending_count == 0


def test_silence_gaps_and_recovery(runtime):
    rt, repo, clock, factory = runtime
    port = start(rt, factory)
    clock.advance(15)
    eventually(lambda: rt.get_status()["logger"].latest_collection_error == "no_valid_reading")
    assert repo.get_status().pending_count == 0
    feed(rt, port)
    assert rt.get_status()["logger"].latest_collection_error is None
    clock.advance(5)
    eventually(lambda: repo.get_status().pending_count == 1)


def test_switch_drains_old_owner_and_clears_preview(runtime):
    rt, repo, clock, factory = runtime
    old = session(rt)
    site = rt.create_site()
    port = start(rt, factory)
    clock.advance(1)
    feed(rt, port)
    thread, result = threaded(
        lambda: rt.switch_site(
            DeploymentCommand(site_id=site.site_id, expected_session_id=old, confirm_switch=True)
        )
    )
    eventually(lambda: port.closed)
    assert session(rt) == old
    clock.advance(9)
    thread.join(2)
    assert result[0].result == "ok"
    assert session(rt) != old
    group = repo.select_pending_group(10, 64000)
    assert group.batch.session.session_id == old
    assert rt.get_status()["latest_live_reading"] is None
    with pytest.raises(ValueError, match="stale_session"):
        rt.start_collection(old)


def test_failed_settings_save_leaves_paused_old_configuration(runtime, monkeypatch):
    rt, repo, _, factory = runtime
    start(rt, factory)
    old = repo.load_installation().settings

    def fail(*args, **kwargs):
        raise OSError("private-data")

    monkeypatch.setattr(repo, "save_settings", fail)
    with pytest.raises(OSError):
        rt.save_settings(
            SettingsCommand(
                settings=LocalSettings(serial_path="/new"), expected_session_id=session(rt)
            )
        )
    assert repo.load_installation().settings == old
    assert not repo.load_lifecycle().session.collection_enabled
    assert factory.ports[-1].closed


def test_shutdown_keeps_intent_and_restart_has_fresh_anchor(runtime):
    rt, repo, clock, factory = runtime
    port = start(rt, factory)
    clock.advance(1)
    feed(rt, port)
    thread, result = threaded(rt.shutdown)
    eventually(lambda: port.closed)
    clock.advance(9)
    thread.join(2)
    assert result == [None]
    assert repo.load_lifecycle().session.collection_enabled
    restarted = LoggerRuntime(repo, serial_factory=factory, clock=clock, health_guard=lambda: None)
    restarted.restore_intent()
    eventually(lambda: len(factory.ports) == 2)
    assert restarted.get_status()["latest_live_reading"] is None
    clock.advance(10)
    assert repo.get_status().pending_count == 1
    restarted.shutdown()


def test_storage_guard_discard_and_durable_block(runtime):
    rt, repo, clock, factory = runtime
    port = start(rt, factory)
    clock.advance(1)
    feed(rt, port)

    def full():
        raise OSError("full")

    rt.health_guard = full
    clock.advance(9)
    eventually(lambda: "storage_full" in repo.load_lifecycle().persisted_blocks)
    eventually(lambda: port.closed)
    eventually(lambda: not rt._reader.is_alive() and not rt._window.is_alive())
    assert repo.get_status().pending_count == 0
    assert rt.start_collection(session(rt)).result == "blocked"
    with pytest.raises(ValueError, match="blocked"):
        rt.preview()


def test_meter_change_discards_candidate_and_confirmation_retains_storage(runtime):
    rt, repo, clock, factory = runtime
    port = start(rt, factory)
    clock.advance(1)
    feed(rt, port)
    from watt_profile.p1.parser import dsmr_crc16

    content = FRAME[: FRAME.index(b"!") + 1].replace(b"SYNTHETIC", b"DIFFERENT")
    # Fixtures use hexadecimal identifiers; change the identifier directly.
    marker = content.index(b"0-0:96.1.1(") + len(b"0-0:96.1.1(")
    content = content[:marker] + b"41" + content[marker + 2 :]
    port.chunks.put(content + f"{dsmr_crc16(content):04X}\r\n".encode())
    eventually(lambda: "meter_changed" in repo.load_lifecycle().persisted_blocks)
    eventually(lambda: port.closed)
    assert repo.get_status().pending_count == 0
    old = session(rt)
    repo.update_blocks("storage_full", blocked=True)
    rt.confirm_meter_change(old)
    assert session(rt) != old
    assert repo.load_lifecycle().persisted_blocks == ("storage_full",)


def test_preview_before_deployment_and_shutdown_cancels_wait(tmp_path):
    from uuid import uuid4

    engine = create_database(tmp_path / "preview.sqlite3")
    migrate(engine)
    repo = LoggerRepository(engine)
    repo.initialize_installation(uuid4(), NOW)
    repo.save_settings(LocalSettings(serial_path="/fake"))
    factory, clock = Factory(), FakeClock()
    rt = LoggerRuntime(repo, serial_factory=factory, clock=clock, health_guard=lambda: None)
    thread, result = threaded(rt.preview)
    eventually(lambda: factory.ports)
    rt.shutdown()
    thread.join(2)
    assert str(result[0]) == "preview_cancelled"
    assert factory.ports[-1].closed
    assert repo.load_lifecycle().session is None
    assert repo.get_status().pending_count == 0
    engine.dispose()


def test_reconnect_sequence_and_cancel_during_backoff(runtime):
    rt, repo, clock, factory = runtime
    factory.failures = [OSError(errno.EBUSY, "private")] * 9
    rt.start_collection(session(rt))
    eventually(lambda: len(factory.configs) == 1)
    for index, delay in enumerate((1, 2, 4, 8, 16, 32, 60, 60), start=2):
        # Wait until retry sleep has started, then advance virtual time.
        time.sleep(0.03)
        clock.advance(delay - 0.1)
        time.sleep(0.02)
        assert len(factory.configs) == index - 1
        clock.advance(0.1)
        eventually(lambda: len(factory.configs) == index)
    rt.pause(session(rt))
    assert not repo.load_lifecycle().session.collection_enabled
    assert len(factory.ports) == 0


def test_write_failure_blocks_without_claiming_durable_block(runtime, monkeypatch):
    rt, repo, clock, factory = runtime
    port = start(rt, factory)
    clock.advance(1)
    feed(rt, port)

    def failed(*args, **kwargs):
        raise OSError("private-data")

    with monkeypatch.context() as failures:
        failures.setattr(repo, "insert_snapshot", failed)
        failures.setattr(repo, "update_blocks", failed)
        clock.advance(9)
        eventually(lambda: rt.get_status()["logger"].storage_blocked)
        eventually(lambda: port.closed)
        eventually(lambda: not rt._reader.is_alive())
    assert repo.load_lifecycle().persisted_blocks == ()
    assert rt.get_status()["persisted_blocks"] == ()
    assert rt.get_status()["runtime_error"] == "storage_error"
    assert repo.get_status().pending_count == 0
    assert rt.start_collection(session(rt)).result == "blocked"
    assert rt.start_collection(session(rt)).result == "blocked"
    rt.save_settings(
        SettingsCommand(
            settings=LocalSettings(serial_path="/fake", server_url="https://example.com"),
            expected_session_id=session(rt),
        )
    )
    assert rt.get_status()["logger"].collection_state == "blocked"
    assert rt.get_status()["logger"].storage_blocked
    assert rt.get_status()["persisted_blocks"] == ()
    assert rt.restore_intent().result == "blocked"
    assert rt.start_collection(session(rt)).result == "blocked"
    with pytest.raises(ValueError, match="blocked"):
        rt.preview()
    assert len(factory.ports) == 1


def test_framer_overlong_frame_is_reported_and_valid_input_recovers(runtime):
    rt, repo, clock, factory = runtime
    port = start(rt, factory)
    overlong = b"/SYNTHETIC\r\n" + b"X" * MAX_FRAME_BYTES + b"\r\n!0000\r\n"
    for offset in range(0, len(overlong), 4096):
        port.chunks.put(overlong[offset : offset + 4096])
    eventually(lambda: rt.rejected_frames == 1)
    assert rt.get_status()["runtime_error"] == "snapshot_invalid"
    assert repo.get_status().pending_count == 0
    assert rt._reader.is_alive()
    clock.advance(1)
    feed(rt, port)
    clock.advance(9)
    eventually(lambda: repo.get_status().pending_count == 1)
    assert rt.rejected_frames == 1
    assert rt.get_status()["runtime_error"] is None


def test_framer_new_header_rejects_partial_frame_and_recovers(runtime):
    rt, repo, clock, factory = runtime
    port = start(rt, factory)
    port.chunks.put(b"/INCOMPLETE\r\n/RECOVERY\r\n")
    eventually(lambda: rt.rejected_frames == 1)
    assert rt.get_status()["runtime_error"] == "snapshot_invalid"
    assert rt._reader.is_alive()
    assert repo.get_status().pending_count == 0
    clock.advance(1)
    feed(rt, port)
    assert rt.rejected_frames == 2
    clock.advance(9)
    eventually(lambda: repo.get_status().pending_count == 1)
    assert rt.get_status()["runtime_error"] is None


def test_framer_disconnect_rejects_partial_frame_without_rejecting_empty_reads(runtime):
    rt, repo, clock, factory = runtime
    port = start(rt, factory)
    port.chunks.put(b"/INCOMPLETE\r\n")
    port.chunks.put(OSError(errno.EIO, "private-path"))
    eventually(lambda: port.closed)
    eventually(lambda: rt.rejected_frames == 1)
    assert rt.get_status()["runtime_error"] == "snapshot_invalid"
    time.sleep(0.03)
    clock.advance(1)
    eventually(lambda: len(factory.ports) == 2)
    port = factory.ports[-1]
    # Empty reads before and after a frame are ordinary timeouts, not rejections.
    port.chunks.put(b"")
    feed(rt, port)
    port.chunks.put(b"")
    clock.advance(9)
    eventually(lambda: repo.get_status().pending_count == 1)
    assert rt.rejected_frames == 1
    assert rt._reader.is_alive()
    assert rt.get_status()["runtime_error"] is None


def test_serial_close_failure_prevents_new_owner(runtime, monkeypatch):
    rt, _, _, factory = runtime
    port = start(rt, factory)
    real_close = port.close

    def failed():
        raise OSError("private-port")

    monkeypatch.setattr(port, "close", failed)
    with pytest.raises(ValueError, match="serial_close_failed"):
        rt.pause(session(rt))
    eventually(lambda: rt.runtime_error == "serial_close_failed")
    with pytest.raises(ValueError, match="serial_close_failed|busy"):
        rt.start_collection(session(rt))
    assert len(factory.ports) == 1
    # Explicitly release the fake after proving no replacement owner was started.
    monkeypatch.setattr(port, "close", real_close)
    real_close()
    rt._port = None
    rt.operations.state = rt.repository.load_lifecycle()


def test_serial_changes_pause_but_metadata_does_not(runtime):
    rt, repo, _, factory = runtime
    port = start(rt, factory)
    original = session(rt)
    rt.save_settings(
        SettingsCommand(
            settings=LocalSettings(serial_path="/fake", server_url="https://example.com"),
            expected_session_id=original,
        )
    )
    assert not port.closed
    rt.save_settings(
        SettingsCommand(
            settings=LocalSettings(serial_path="/new", baudrate=9600), expected_session_id=original
        )
    )
    assert port.closed
    assert session(rt) == original
    assert repo.load_installation().settings.baudrate == 9600
    start(rt, factory)
    assert factory.configs[-1].path == "/new"


def test_paused_restart_and_failed_health_never_open(runtime):
    rt, repo, clock, factory = runtime
    rt.restore_intent()
    assert not factory.ports
    start(rt, factory)
    rt.shutdown()

    def failed():
        raise OSError("full")

    restarted = LoggerRuntime(repo, clock=clock, serial_factory=factory, health_guard=failed)
    assert restarted.restore_intent().result == "blocked"
    assert len(factory.ports) == 1
    assert repo.load_lifecycle().persisted_blocks == ("storage_full",)
    restarted.shutdown()
