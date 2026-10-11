"""Single-owner serial collection with durable intent and bounded live state."""

from __future__ import annotations

import errno
import json
import re
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from pydantic import StrictStr, field_validator

from watt_profile.contracts import lifecycle as pure
from watt_profile.contracts.domain import (
    MAX_SNAPSHOT_BYTES,
    DomainModel,
    FieldIssue,
    Metrics,
    Quality,
    SanitizedField,
    UtcTime,
)
from watt_profile.logger.deployment import DeploymentCommand, DeploymentOperations, SettingsCommand
from watt_profile.logger.sampler import WindowSampler
from watt_profile.logger.storage.repository import LoggerRepository, Site
from watt_profile.p1.parser import P1ParseError, TelegramFramer, parse_telegram
from watt_profile.p1.profile import map_telegram_with_rejection
from watt_profile.serial.interfaces import (
    Clock,
    PySerialFactory,
    SerialConfig,
    SerialFactory,
    SystemClock,
)


class ReadingView(DomainModel):
    meter_time_raw: StrictStr | None
    meter_time_utc: UtcTime | None
    received_at: UtcTime
    metrics: Metrics
    fields: tuple[SanitizedField, ...]
    quality: Quality

    @field_validator("meter_time_raw")
    @classmethod
    def meter_time_grammar(cls, value):
        if value is not None and re.fullmatch(r"[0-9]{12}[SW]", value) is None:
            raise ValueError("unsafe_meter_time")
        return value

    @classmethod
    def from_reading(cls, reading):
        view = cls(
            meter_time_raw=reading.meter_time_raw,
            meter_time_utc=reading.meter_time_utc,
            received_at=reading.received_at,
            metrics=Metrics(**reading.metrics),
            fields=tuple(
                SanitizedField(
                    code=f.code,
                    raw_values=tuple(f.raw_values),
                    value=f.value,
                    unit=f.unit,
                    redacted=f.redacted,
                )
                for f in reading.fields
            ),
            quality=Quality(
                frame_length=reading.frame_length,
                issues=tuple(FieldIssue(code=i.code, reason=i.reason) for i in reading.issues),
            ),
        )
        # IDs all have the same canonical length. Check the exact eventual record
        # size without allocating a Snapshot or record ID before finalization.
        projection = view.model_dump(mode="json")
        projection.update(
            {
                name: "00000000-0000-4000-8000-000000000000"
                for name in (
                    "record_id",
                    "device_id",
                    "site_id",
                    "session_id",
                    "source_id",
                    "measurement_point_id",
                )
            }
        )
        projection.update(schema_version=1, sampled_at=projection["received_at"])
        if (
            len(
                json.dumps(
                    projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8")
            )
            > MAX_SNAPSHOT_BYTES
        ):
            raise ValueError("snapshot_too_large")
        return view


class LoggerRuntime:
    """Blocking domain commands are intended for worker threads, never the event loop.

    The reception lock orders complete-frame processing with window finalization.
    It is independent of the state lock, so a serial read cannot block status.
    """

    def __init__(
        self,
        repository: LoggerRepository,
        *,
        serial_factory: SerialFactory | None = None,
        clock: Clock | None = None,
        health_guard: Callable[[], None] | None = None,
        input_mode: str = "serial",
        join_timeout: float = 2.0,
    ):
        if input_mode not in {"serial", "synthetic"}:
            raise ValueError("invalid_input_mode")
        self.repository = repository
        self.factory = serial_factory or PySerialFactory()
        self.clock = clock or SystemClock()
        self.health_guard = health_guard or self._default_health
        self.input_mode = input_mode
        self.join_timeout = join_timeout
        self._commands = threading.Lock()
        self._state = threading.RLock()
        self._reception = threading.Lock()
        self._cancel = threading.Event()
        self._window_cancel = threading.Event()
        self._reader = self._window = self._port = None
        self._sampler = None
        self._anchor = 0.0
        self._latest = None
        self._sequence = 0
        self._preview_active = False
        self._preview_condition = threading.Condition(self._state)
        self._last_admitted = 0.0
        self.runtime_error = None
        self.rejected_frames = 0
        self._closed = False
        self._shutting_down = False
        self._disconnect_sequence = 0
        self.operations = DeploymentOperations(repository, self._drain)

    def _default_health(self):
        path = Path(self.repository.engine.url.database).parent
        if shutil.disk_usage(path).free < 64 * 1024 * 1024:
            raise OSError("storage_full")
        installation = self.repository.load_installation()
        self.repository.save_settings(installation.settings)

    def _wait(self, event, seconds):
        # Custom clocks may provide virtual, interruptible waits for deterministic tests.
        wait = getattr(self.clock, "wait_interruptible", None)
        if wait:
            wait(seconds, event)
        elif isinstance(self.clock, SystemClock):
            event.wait(seconds)
        else:
            remaining = seconds
            while remaining > 0 and not event.is_set():
                step = min(remaining, 0.05)
                self.clock.wait(step)
                remaining -= step

    def _change(self, **values):
        state = self.operations.state
        self.operations.state = pure.Lifecycle.model_validate(
            {n: getattr(state, n) for n in pure.Lifecycle.model_fields} | values
        )

    def _command(self, action):
        if not self._commands.acquire(blocking=False):
            raise ValueError("busy")
        try:
            with self._state:
                if self._closed or self._shutting_down:
                    raise ValueError("runtime_closed")
                if self._reader and self._reader.is_alive() and self._cancel.is_set():
                    raise ValueError("busy")
                if self._port is not None and self._cancel.is_set():
                    raise ValueError("serial_close_failed")
                return action()
        finally:
            self._commands.release()

    def restore_intent(self):
        def action():
            try:
                self.health_guard()
            except Exception:
                self._block("storage_full")
                return pure.Transition(lifecycle=self.operations.state, result="blocked")
            result = self.operations.restore_intent()
            if result.lifecycle.collection_state == "starting":
                self._launch(False)
            return result

        return self._command(action)

    def start_collection(self, session_id: UUID):
        def action():
            result = self.operations.start(session_id, input_mode=self.input_mode)
            if result.result == "ok" and not (self._reader and self._reader.is_alive()):
                try:
                    self.health_guard()
                except Exception:
                    self._block("storage_full")
                    return pure.Transition(lifecycle=self.operations.state, result="blocked")
                self._launch(False)
            return result

        return self._command(action)

    def stop_collection(self, session_id: UUID):
        return self._command(lambda: self.operations.pause(session_id, self.clock.utc_now()))

    pause = stop_collection

    def complete_session(self, session_id: UUID):
        return self._command(lambda: self.operations.complete(session_id, self.clock.utc_now()))

    def switch_site(self, command: DeploymentCommand):
        def action():
            previous = self.operations._selected()
            result = self.operations.deploy(command, self.clock.utc_now())
            if self.operations._selected() != previous:
                self._latest = None
            return result

        return self._command(action)

    def save_settings(self, command: SettingsCommand):
        return self._command(lambda: self.operations.save_settings(command, self.clock.utc_now()))

    def create_site(self, **values) -> Site:
        return self._command(lambda: self.operations.create_site(**values))

    def edit_site(self, site: Site):
        return self._command(lambda: self.operations.edit_site(site))

    def confirm_meter_change(self, session_id: UUID):
        def action():
            result = self.operations.confirm_meter_change(session_id, self.clock.utc_now())
            if result.result == "ok":
                self._latest = None
            return result

        return self._command(action)

    def _launch(self, temporary):
        if self._reader and self._reader.is_alive():
            raise ValueError("busy")
        if self._reader:
            self._release()
        self._cancel = threading.Event()
        self._window_cancel = threading.Event()
        self._anchor = self._last_admitted = self.clock.monotonic()
        if not temporary:
            state = self.operations.state
            self._sampler = WindowSampler(
                state.session.descriptor, state.source.descriptor, self._anchor
            )
            self._window = threading.Thread(target=self._windows, daemon=True, name="logger-window")
            self._window.start()
        self._reader = threading.Thread(
            target=self._read, args=(temporary,), daemon=True, name="logger-reader"
        )
        self._reader.start()

    def _serial_config(self):
        settings = self.repository.load_installation().settings
        if self.input_mode == "serial" and settings.serial_path is None:
            raise ValueError("serial_path_required")
        return SerialConfig(
            settings.serial_path or "synthetic",
            settings.baudrate,
            settings.bytesize,
            settings.parity,
            float(settings.stopbits),
            min(float(settings.timeout), 1.0),
        )

    @staticmethod
    def _serial_error(exc):
        if getattr(exc, "errno", None) in {errno.EBUSY, errno.EAGAIN}:
            return "serial_busy"
        if getattr(exc, "errno", None) in {errno.EACCES, errno.EPERM}:
            return "serial_permission"
        return "serial_unavailable"

    def _read(self, temporary):
        delays = (1, 2, 4, 8, 16, 32, 60)
        retry = 0
        framer = TelegramFramer()
        while not self._cancel.is_set():
            port = None
            try:
                port = self.factory(self._serial_config())
                with self._state:
                    self._port = port
                    if self._cancel.is_set():
                        break
                    if not temporary:
                        self.operations.owner_started()
                while not self._cancel.is_set():
                    with self._reception:
                        if self._receive(port, framer, temporary):
                            retry = 0
                    # Yield the reception lock to the boundary worker, even with fast fakes.
                    self._cancel.wait(0.001)
            except Exception as exc:
                with self._state:
                    if not self._cancel.is_set():
                        code = self._serial_error(exc)
                        self._disconnect_sequence += 1
                        self._preview_condition.notify_all()
                        if temporary:
                            self._change(latest_error=code)
                            self._cancel.set()
                            self._preview_condition.notify_all()
                        else:
                            self._change(collection_state="reconnecting", latest_error=code)
            finally:
                discarded_before = framer.discarded_frames
                framer.reset()
                with self._state:
                    self._report_discarded_frames(framer.discarded_frames - discarded_before)
                if port is not None:
                    try:
                        port.close()
                    except Exception:
                        with self._state:
                            self.runtime_error = "serial_close_failed"
                            self._cancel.set()
                with self._state:
                    if self.runtime_error != "serial_close_failed":
                        self._port = None
            if not temporary and not self._cancel.is_set():
                self._wait(self._cancel, delays[min(retry, len(delays) - 1)])
                retry += 1

    def _receive(self, port, framer, temporary):
        # Raw input belongs only to this read. Drop chunk/frame references before
        # binding or persistence, and return only an admission flag to the reader.
        chunk = port.read(4096)
        received = self.clock.monotonic()
        utc = self.clock.utc_now()
        with self._state:
            if self._cancel.is_set():
                return False
            discarded_before = framer.discarded_frames
            frames = framer.feed(chunk)
            del chunk
            self._report_discarded_frames(framer.discarded_frames - discarded_before)
            admitted = False
            while frames:
                frame = frames.pop(0)
                try:
                    telegram = parse_telegram(frame)
                    reading, _ = map_telegram_with_rejection(telegram, utc)
                    if reading is None:
                        raise ValueError("frame_invalid")
                    validated = ReadingView.from_reading(reading)
                    fingerprint = telegram.equipment_fingerprint
                    if not temporary:
                        session = self.operations.state.session.descriptor
                except (P1ParseError, ValueError):
                    self.rejected_frames += 1
                    self.runtime_error = "snapshot_invalid"
                    continue
                finally:
                    del frame
                del telegram
                if not temporary:
                    try:
                        bound = self.repository.bind_meter(session.session_id, fingerprint)
                    except Exception:
                        self.runtime_error = "storage_error"
                        self._block("storage_full")
                        break
                    if "meter_changed" in bound.persisted_blocks:
                        self._block("meter_changed")
                        break
                    self._change(source=bound.source)
                    completed = self._sampler.advance(received, self.clock.utc_now())
                    completed += self._sampler.offer(reading, received)
                    self._persist(completed)
                    if self._cancel.is_set():
                        break
                    self._change(latest_error=None)
                self._latest = validated
                self._sequence += 1
                self._last_admitted = received
                self.runtime_error = None
                admitted = True
                self._preview_condition.notify_all()
                if temporary:
                    self._cancel.set()
                    break
            return admitted

    def _report_discarded_frames(self, count):
        if count:
            self.rejected_frames += count
            # Do not obscure the fault that caused the reader to stop.
            if self.runtime_error not in {"storage_error", "serial_close_failed"}:
                self.runtime_error = "snapshot_invalid"

    def _block(self, reason):
        self._cancel.set()
        self._window_cancel.set()
        self._sampler = None
        self.operations.state = pure.block_collection(self.operations.state, reason).lifecycle
        try:
            persisted = self.repository.update_blocks(reason, blocked=True)
            self.operations.unsaved_blocks = tuple(
                block
                for block in self.operations.unsaved_blocks
                if block not in persisted.persisted_blocks
            )
            effective = self.operations._retain_unsaved_blocks(persisted)
            self._change(persisted_blocks=effective.persisted_blocks)
        except Exception:
            if reason not in self.operations.unsaved_blocks:
                self.operations.unsaved_blocks += (reason,)
            self.runtime_error = "storage_error"
        if self._port is not None:
            try:
                self._port.close()
            except Exception:
                self.runtime_error = "serial_close_failed"
        self._preview_condition.notify_all()

    def _persist(self, snapshots):
        for snapshot in snapshots:
            try:
                self.health_guard()
                self.repository.insert_snapshot(snapshot)
            except ValueError:
                self.rejected_frames += 1
                self.runtime_error = "snapshot_invalid"
            except Exception:
                self.runtime_error = "storage_error"
                self._block("storage_full")
                break

    def _windows(self):
        last_health = self._anchor
        while not self._window_cancel.is_set():
            self._wait(self._window_cancel, 0.05)
            with self._reception:
                with self._state:
                    if self._window_cancel.is_set():
                        break
                    now = self.clock.monotonic()
                    if now - last_health >= 10:
                        try:
                            self.health_guard()
                        except Exception:
                            self._block("storage_full")
                            break
                        last_health = now
                    if self._sampler:
                        try:
                            self._persist(self._sampler.advance(now, self.clock.utc_now()))
                        except ValueError:
                            self.rejected_frames += 1
                            self.runtime_error = "snapshot_invalid"
                    if (
                        now - self._last_admitted >= 15
                        and self.operations.state.collection_state == "collecting"
                    ):
                        self._change(latest_error="no_valid_reading")

    def _release(self):
        self._cancel.set()
        self._window_cancel.set()
        port = self._port
        self._state.release()
        try:
            if port is not None:
                try:
                    port.close()
                except Exception:
                    raise ValueError("serial_close_failed") from None
            for thread in (self._reader, self._window):
                if thread and thread is not threading.current_thread():
                    thread.join(self.join_timeout)
                    if thread.is_alive():
                        raise ValueError("serial_join_failed")
            if self._port is not None:
                raise ValueError("serial_close_failed")
        finally:
            self._state.acquire()

    def _drain(self, state):
        self._release()
        if self._sampler:
            boundary = self._sampler.next_boundary
            # A candidate may already have been finalized; no wait is needed then.
            if self._sampler.has_candidate:
                self._state.release()
                try:
                    self._wait(threading.Event(), max(0, boundary - self.clock.monotonic()))
                finally:
                    self._state.acquire()
                self._persist(self._sampler.advance(self.clock.monotonic(), self.clock.utc_now()))
            self._sampler = None
        if self.operations.state.persisted_blocks:
            raise ValueError("blocked")
        return self.clock.utc_now()

    def preview(self, *, cancellation: threading.Event | None = None):
        cancellation = cancellation or threading.Event()
        temporary = False
        with self._state:
            if self._closed or self._preview_active or self._commands.locked():
                raise ValueError("busy")
            if self.operations.state.persisted_blocks:
                raise ValueError("blocked")
            result = pure.preview(self.operations.state)
            if result.result not in {"ok", "subscribed"}:
                raise ValueError(result.result)
            self._preview_active = True
            sequence = self._sequence
            disconnected = self._disconnect_sequence
            temporary = result.result == "ok"
            if temporary:
                try:
                    self._serial_config()
                except Exception:
                    self._preview_active = False
                    raise
                self.operations.state = result.lifecycle
                self._launch(True)
            deadline = self.clock.monotonic() + 15
            try:
                while self._sequence == sequence:
                    if self._shutting_down:
                        raise ValueError("preview_cancelled")
                    if self._disconnect_sequence != disconnected:
                        raise ValueError(self.operations.state.latest_error or "serial_unavailable")
                    if cancellation.is_set():
                        raise ValueError("preview_cancelled")
                    if self.operations.state.persisted_blocks:
                        raise ValueError("blocked")
                    if self.clock.monotonic() >= deadline:
                        raise ValueError("preview_timeout")
                    if temporary and self._cancel.is_set():
                        raise ValueError(self.operations.state.latest_error or "serial_unavailable")
                    self._preview_condition.wait(0.05)
                return self._latest
            finally:
                try:
                    if temporary:
                        self._release()
                        self.operations.state = pure.finish_preview(self.operations.state).lifecycle
                finally:
                    self._preview_active = False
                    self._preview_condition.notify_all()

    def get_status(self):
        with self._state:
            state = self.operations.state
            latest = self._latest
            error = self.runtime_error
        free_bytes = shutil.disk_usage(Path(self.repository.engine.url.database).parent).free
        status = self.repository.get_status(free_bytes=free_bytes)
        status = status.model_copy(
            update={
                "collection_state": state.collection_state,
                "latest_collection_error": state.latest_error,
                "storage_blocked": "storage_full" in state.persisted_blocks,
            }
        )
        return {
            "logger": status,
            "input_mode": self.input_mode,
            "upload_mode": "not_implemented",
            "persisted_blocks": self.repository.load_lifecycle().persisted_blocks,
            "active_session_state": state.session.state if state.session else None,
            "latest_live_reading": latest,
            "active_session_latest_received_at": (
                self.repository.active_session_latest_received_at()
            ),
            "runtime_error": error,
        }

    def shutdown(self):
        def action():
            self._shutting_down = True
            self._preview_condition.notify_all()
            while self._preview_active:
                self._preview_condition.wait(0.05)
            state = self.operations.state
            if self._reader:
                if state.collection_state in {"starting", "collecting", "reconnecting"}:
                    self._change(collection_state="stopping", stop_action="pause")
                    self._drain(self.operations.state)
                else:
                    self._release()
            self._closed = True

        return self._command(action)
