"""Scheduler behavior against isolated durable Logger storage."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tests.test_logger_runtime import Factory, FakeClock, eventually, feed
from tests.test_logger_storage import record
from watt_profile.contracts.lifecycle import complete_session, new_deployment
from watt_profile.contracts.upload import UploadConfirmation
from watt_profile.logger.runtime import LoggerRuntime
from watt_profile.logger.storage.database import create_database, migrate
from watt_profile.logger.storage.repository import LocalSettings, LoggerRepository, Site
from watt_profile.logger.synthetic import synthetic_telegram
from watt_profile.logger.upload import TransportResult, UploadScheduler

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def repository(tmp_path):
    engine = create_database(tmp_path / "uploader.sqlite3")
    migrate(engine)
    repo = LoggerRepository(engine)
    repo.initialize_installation(uuid4(), NOW)
    site = repo.save_site(Site(site_id=uuid4()))
    repo.commit_deployment_change(
        None, new_deployment(repo.load_lifecycle(), site.site_id, NOW).lifecycle
    )
    yield repo
    engine.dispose()


class ConfirmingTransport:
    def __init__(self):
        self.groups = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, origin, token, group):
        self.groups.append(group)
        self.entered.set()
        await self.release.wait()
        confirmation = UploadConfirmation(
            schema_version=1,
            device_id=group.batch.device_id,
            confirmed_record_ids=tuple(item.record_id for item in group.batch.records),
        )
        return TransportResult(200, confirmation=confirmation)


class RetryTransport:
    def __init__(self, *, hold_first=False):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.hold_first = hold_first
        self.active = 0
        self.max_active = 0

    async def send(self, origin, token, group):
        self.calls.append(group)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            if len(self.calls) == 1:
                if self.hold_first:
                    await self.release.wait()
                    return TransportResult(401, error_code="invalid_credential")
                return TransportResult(0, error_code="upload_network_error")
            return TransportResult(
                200,
                confirmation=UploadConfirmation(
                    schema_version=1,
                    device_id=group.batch.device_id,
                    confirmed_record_ids=tuple(r.record_id for r in group.batch.records),
                ),
            )
        finally:
            self.active -= 1


class AuthenticationTransport:
    def __init__(self):
        self.calls = 0

    async def send(self, origin, token, group):
        self.calls += 1
        if self.calls == 1:
            return TransportResult(401, error_code="invalid_credential")
        return TransportResult(
            200,
            confirmation=UploadConfirmation(
                schema_version=1,
                device_id=group.batch.device_id,
                confirmed_record_ids=tuple(r.record_id for r in group.batch.records),
            ),
        )


class AlwaysConfirmTransport:
    async def send(self, origin, token, group):
        return TransportResult(200, confirmation=confirmed(group))


def confirmed(group):
    return UploadConfirmation(
        schema_version=1,
        device_id=group.batch.device_id,
        confirmed_record_ids=tuple(item.record_id for item in group.batch.records),
    )


def split_old_and_new_group(repository):
    old = record(repository, received_at="2026-01-02T00:00:00.000000Z")
    repository.insert_snapshot(old)
    state = repository.load_lifecycle()
    previous_session = state.session.descriptor.session_id
    ended = complete_session(state, previous_session, NOW).lifecycle
    repository.commit_deployment_change(previous_session, ended)
    site = repository.save_site(Site(site_id=uuid4()))
    newer = new_deployment(repository.load_lifecycle(), site.site_id, NOW).lifecycle
    repository.commit_deployment_change(previous_session, newer)
    new = record(repository, received_at="2026-01-03T00:00:00.000000Z")
    repository.insert_snapshot(new)
    return old, new


def test_startup_sends_and_confirms_durable_backlog_while_collection_can_advance(repository):
    async def scenario():
        repository.save_settings(LocalSettings(server_url="https://ingest.example"))
        snapshot = record(repository)
        repository.insert_snapshot(snapshot)
        transport = ConfirmingTransport()
        scheduler = UploadScheduler(repository, transport, "synthetic-token")
        worker = asyncio.create_task(scheduler.run())
        try:
            await asyncio.wait_for(transport.entered.wait(), 3)
            # A write can complete while the transport owns the only in-flight request.
            later = record(repository, received_at="2026-01-01T00:00:01.000000Z")
            repository.insert_snapshot(later)
            assert repository.get_status().pending_count == 2
            transport.release.set()
            for _ in range(100):
                if repository.get_status().pending_count == 0:
                    break
                await asyncio.sleep(0.01)
            assert repository.get_status().pending_count == 0
            assert (
                transport.groups[0].canonical_records[0]
                == json.dumps(
                    snapshot.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            )
            for _ in range(100):
                if scheduler.observation.state == "idle":
                    break
                await asyncio.sleep(0.01)
            assert scheduler.observation.state == "idle"
        finally:
            transport.release.set()
            scheduler.stop()
            await asyncio.wait_for(worker, 2)

    asyncio.run(scenario())


def test_serial_reader_commits_while_upload_transport_is_waiting(repository):
    async def scenario():
        repository.save_settings(LocalSettings(server_url="https://ingest.example"))
        repository.insert_snapshot(record(repository))
        transport = ConfirmingTransport()
        scheduler = UploadScheduler(repository, transport, "synthetic-token")
        clock, factory = FakeClock(), Factory()
        runtime = LoggerRuntime(
            repository,
            clock=clock,
            serial_factory=factory,
            input_mode="synthetic",
            health_guard=lambda: None,
        )
        worker = asyncio.create_task(scheduler.run())
        try:
            await asyncio.wait_for(transport.entered.wait(), 3)
            session_id = runtime.operations.state.session.descriptor.session_id
            runtime.start_collection(session_id)
            eventually(lambda: bool(factory.ports))
            port = factory.ports[-1]
            clock.advance(1)
            feed(runtime, port, synthetic_telegram(clock.utc_now()))
            clock.advance(8)
            feed(runtime, port, synthetic_telegram(clock.utc_now()))
            clock.advance(1)
            eventually(lambda: repository.get_status().pending_count == 2)
            assert runtime.get_status()["logger"].collection_state == "collecting"
            # The network request is still gated while the independent reader finalized a window.
            assert not transport.release.is_set()
            transport.release.set()
            for _ in range(100):
                if repository.get_status().pending_count == 0:
                    break
                await asyncio.sleep(0.01)
            assert repository.get_status().pending_count == 0
        finally:
            transport.release.set()
            scheduler.stop()
            await asyncio.wait_for(worker, 2)
            runtime.shutdown()

    asyncio.run(scenario())


def test_missing_origin_never_calls_transport_on_start_or_retry(repository):
    async def scenario():
        transport = ConfirmingTransport()
        scheduler = UploadScheduler(repository, transport, "synthetic-token")
        worker = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0)
        scheduler.wake()
        scheduler.retry()
        await asyncio.sleep(0)
        assert transport.groups == []
        scheduler.stop()
        await asyncio.wait_for(worker, 1)

    asyncio.run(scenario())


def test_retry_during_request_is_coalesced_and_applied_after_401(repository):
    async def scenario():
        repository.save_settings(LocalSettings(server_url="https://ingest.example"))
        repository.insert_snapshot(record(repository))
        transport = RetryTransport(hold_first=True)
        scheduler = UploadScheduler(repository, transport, "synthetic-token")
        worker = asyncio.create_task(scheduler.run())
        try:
            await asyncio.wait_for(transport.entered.wait(), 3)
            scheduler.retry()
            transport.release.set()
            for _ in range(200):
                if repository.get_status().pending_count == 0:
                    break
                await asyncio.sleep(0.01)
            assert repository.get_status().pending_count == 0
            assert len(transport.calls) == 2
            assert transport.max_active == 1
        finally:
            scheduler.stop()
            await asyncio.wait_for(worker, 2)

    asyncio.run(scenario())


def test_ambiguous_failure_reuses_selected_body(repository):
    async def scenario():
        repository.save_settings(LocalSettings(server_url="https://ingest.example"))
        repository.insert_snapshot(record(repository))
        transport = RetryTransport()

        async def immediate_wait(delay, wake, stop):
            await asyncio.sleep(0)
            return "timer"

        scheduler = UploadScheduler(repository, transport, "synthetic-token", wait=immediate_wait)
        worker = asyncio.create_task(scheduler.run())
        try:
            for _ in range(200):
                if repository.get_status().pending_count == 0:
                    break
                await asyncio.sleep(0.01)
            assert repository.get_status().pending_count == 0
            assert len(transport.calls) == 2
            assert transport.calls[0].body == transport.calls[1].body
            assert transport.max_active == 1
        finally:
            scheduler.stop()
            await asyncio.wait_for(worker, 2)

    asyncio.run(scenario())


def test_retry_after_uses_http_grammar_and_clamps_bounds(repository):
    from email.utils import format_datetime

    fixed_now = datetime(2026, 1, 1, tzinfo=UTC)
    scheduler = UploadScheduler(
        repository, ConfirmingTransport(), "synthetic-token", utcnow=lambda: fixed_now
    )
    assert scheduler._delay_from("0") == 1
    assert scheduler._delay_from("7") == 7
    assert scheduler._delay_from("999") == 60
    # Floats, exponent notation and signed values are invalid delay-seconds.
    assert scheduler._delay_from("1.5") == 1
    assert scheduler._delay_from("1e2") == 2
    assert scheduler._delay_from("-1") == 4
    assert scheduler._delay_from("0001") == 1
    assert scheduler._delay_from(format_datetime(fixed_now)) == 1


def test_same_effective_origin_wakeup_keeps_authentication_block_until_retry(repository):
    async def scenario():
        repository.save_settings(LocalSettings(server_url="https://ingest.example"))
        repository.insert_snapshot(record(repository))
        transport = AuthenticationTransport()
        scheduler = UploadScheduler(repository, transport, "synthetic-token")
        worker = asyncio.create_task(scheduler.run())
        try:
            for _ in range(100):
                if scheduler.observation.state == "blocked":
                    break
                await asyncio.sleep(0.01)
            assert scheduler.observation.global_error == "invalid_credential"
            scheduler.wake()
            await asyncio.sleep(0.05)
            assert transport.calls == 1
            scheduler.retry()
            for _ in range(100):
                if repository.get_status().pending_count == 0:
                    break
                await asyncio.sleep(0.01)
            assert repository.get_status().pending_count == 0
            assert transport.calls == 2
        finally:
            scheduler.stop()
            await asyncio.wait_for(worker, 2)

    asyncio.run(scenario())


def test_same_origin_wakeup_does_not_shorten_transient_backoff(repository):
    class Monotonic:
        now = 0.0

        def __call__(self):
            return self.now

    class FailThenConfirm:
        def __init__(self):
            self.calls = 0

        async def send(self, origin, token, group):
            self.calls += 1
            if self.calls == 1:
                return TransportResult(503, error_code="temporarily_unavailable")
            return TransportResult(200, confirmation=confirmed(group))

    async def scenario():
        repository.save_settings(LocalSettings(server_url="https://ingest.example"))
        repository.insert_snapshot(record(repository))
        clock = Monotonic()
        transport = FailThenConfirm()
        waits = []
        first_wait = asyncio.Event()

        async def controlled_wait(delay, wake, stop):
            waits.append(delay)
            first_wait.set()
            if clock.now < 1:
                await wake.wait()
                wake.clear()
            return "timer" if clock.now >= 1 else "wake"

        scheduler = UploadScheduler(
            repository,
            transport,
            "synthetic-token",
            monotonic=clock,
            wait=controlled_wait,
        )
        worker = asyncio.create_task(scheduler.run())
        try:
            await asyncio.wait_for(first_wait.wait(), 3)
            scheduler.wake()
            for _ in range(100):
                if len(waits) == 2:
                    break
                await asyncio.sleep(0.01)
            assert waits[:2] == [1.0, 1.0]
            assert transport.calls == 1
            clock.now = 1.0
            scheduler.wake()
            for _ in range(100):
                if repository.get_status().pending_count == 0:
                    break
                await asyncio.sleep(0.01)
            assert repository.get_status().pending_count == 0
            assert transport.calls == 2
        finally:
            scheduler.stop()
            await asyncio.wait_for(worker, 2)

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [403, 409, 422])
def test_rejected_old_group_does_not_starve_new_group(repository, status):
    class RejectThenConfirm:
        def __init__(self):
            self.calls = []

        async def send(self, origin, token, group):
            self.calls.append(group)
            if len(self.calls) == 1:
                return TransportResult(status, error_code="upload_http_rejected")
            return TransportResult(200, confirmation=confirmed(group))

    async def scenario():
        repository.save_settings(LocalSettings(server_url="https://ingest.example"))
        old, newer = split_old_and_new_group(repository)
        transport = RejectThenConfirm()
        scheduler = UploadScheduler(repository, transport, "synthetic-token")
        worker = asyncio.create_task(scheduler.run())
        try:
            for _ in range(100):
                if repository.get_status().pending_count == 1:
                    break
                await asyncio.sleep(0.01)
            assert repository.get_status().pending_count == 1
            assert transport.calls[0].batch.records == (old,)
            assert transport.calls[1].batch.records == (newer,)
            assert transport.calls[0].batch.session.session_id != (
                transport.calls[1].batch.session.session_id
            )
            assert len(scheduler.blocked_group_errors) == 1
        finally:
            scheduler.stop()
            await asyncio.wait_for(worker, 2)

    asyncio.run(scenario())


def test_413_halves_batch_limit_and_retains_one_record_limit(repository):
    class ShrinkTransport:
        def __init__(self):
            self.calls = []
            self.third = asyncio.Event()
            self.release = asyncio.Event()

        async def send(self, origin, token, group):
            self.calls.append(group)
            if len(self.calls) <= 7:
                return TransportResult(413)
            if len(self.calls) == 9:
                self.third.set()
                await self.release.wait()
            return TransportResult(200, confirmation=confirmed(group))

    async def scenario():
        repository.save_settings(LocalSettings(server_url="https://ingest.example"))
        snapshots = [record(repository) for _ in range(3)]
        for snapshot in snapshots:
            repository.insert_snapshot(snapshot)
        transport = ShrinkTransport()
        scheduler = UploadScheduler(repository, transport, "synthetic-token")
        worker = asyncio.create_task(scheduler.run())
        try:
            await asyncio.wait_for(transport.third.wait(), 3)
            key = scheduler._key(transport.calls[0])
            assert [len(group.batch.records) for group in transport.calls[:7]] == [3] * 7
            assert [len(group.batch.records) for group in transport.calls[7:]] == [1, 1]
            assert scheduler._limits[key] == 1
            assert repository.get_status().pending_count == 2
            transport.release.set()
            for _ in range(100):
                if repository.get_status().pending_count == 0:
                    break
                await asyncio.sleep(0.01)
            assert repository.get_status().pending_count == 0
        finally:
            transport.release.set()
            scheduler.stop()
            await asyncio.wait_for(worker, 2)

    asyncio.run(scenario())


def test_413_on_one_record_blocks_group_as_batch_too_large(repository):
    class TooLargeTransport:
        async def send(self, origin, token, group):
            return TransportResult(413)

    async def scenario():
        repository.save_settings(LocalSettings(server_url="https://ingest.example"))
        repository.insert_snapshot(record(repository))
        scheduler = UploadScheduler(repository, TooLargeTransport(), "synthetic-token")
        worker = asyncio.create_task(scheduler.run())
        try:
            for _ in range(100):
                if scheduler.observation.state == "blocked":
                    break
                await asyncio.sleep(0.01)
            assert list(scheduler.blocked_group_errors.values()) == ["batch_too_large"]
            assert repository.get_status().pending_count == 1
        finally:
            scheduler.stop()
            await asyncio.wait_for(worker, 2)

    asyncio.run(scenario())


def test_backoff_ladder_caps_and_successful_drain_is_immediate(repository):
    class LadderTransport:
        def __init__(self):
            self.calls = 0

        async def send(self, origin, token, group):
            self.calls += 1
            if self.calls <= 7:
                return TransportResult(503, error_code="temporarily_unavailable")
            return TransportResult(200, confirmation=confirmed(group))

    async def scenario():
        repository.save_settings(LocalSettings(server_url="https://ingest.example"))
        repository.insert_snapshot(record(repository))
        transport = LadderTransport()
        delays = []

        async def immediate_wait(delay, wake, stop):
            delays.append(delay)
            return "timer"

        scheduler = UploadScheduler(repository, transport, "synthetic-token", wait=immediate_wait)
        worker = asyncio.create_task(scheduler.run())
        try:
            for _ in range(100):
                if repository.get_status().pending_count == 0:
                    break
                await asyncio.sleep(0.01)
            assert repository.get_status().pending_count == 0
            assert delays[:7] == [1, 2, 4, 8, 16, 32, 60]
            assert transport.calls == 8
        finally:
            scheduler.stop()
            await asyncio.wait_for(worker, 2)

    asyncio.run(scenario())


def test_idle_scan_waits_ten_seconds(repository):
    async def scenario():
        repository.save_settings(LocalSettings(server_url="https://ingest.example"))
        delays = []

        async def stop_after_idle(delay, wake, stop):
            delays.append(delay)
            return "stop"

        scheduler = UploadScheduler(
            repository,
            ConfirmingTransport(),
            "synthetic-token",
            wait=stop_after_idle,
        )
        await scheduler.run()
        assert delays == [10.0]

    asyncio.run(scenario())


def test_confirmation_write_failure_preserves_pending_and_retries_same_batch(repository):
    original = repository.apply_confirmation
    attempts = []

    def fail_once(batch, confirmation, now):
        attempts.append(batch)
        if len(attempts) == 1:
            raise OSError("synthetic disk failure")
        return original(batch, confirmation, now)

    async def scenario():
        repository.save_settings(LocalSettings(server_url="https://ingest.example"))
        repository.insert_snapshot(record(repository))
        repository.apply_confirmation = fail_once

        class ConfirmEveryTime:
            def __init__(self):
                self.calls = []
                self.storage_states = []

            async def send(self, origin, token, group):
                self.calls.append(group)
                status = repository.get_status()
                self.storage_states.append((status.pending_count, status.last_successful_upload_at))
                return TransportResult(200, confirmation=confirmed(group))

        transport = ConfirmEveryTime()
        delays = []

        async def immediate_wait(delay, wake, stop):
            delays.append(delay)
            return "timer"

        scheduler = UploadScheduler(repository, transport, "synthetic-token", wait=immediate_wait)
        worker = asyncio.create_task(scheduler.run())
        try:
            for _ in range(100):
                if repository.get_status().pending_count == 0:
                    break
                await asyncio.sleep(0.01)
            status = repository.get_status()
            assert status.pending_count == 0
            assert status.last_successful_upload_at is not None
            assert len(attempts) == 2
            assert attempts[0] == attempts[1]
            assert transport.calls[0] == transport.calls[1]
            assert transport.storage_states == [(1, None), (1, None)]
            assert delays[0] == 1
        finally:
            scheduler.stop()
            await asyncio.wait_for(worker, 2)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [ValueError("descriptor_mismatch"), "sqlite_corruption"])
def test_confirmation_invariant_or_corruption_stops_with_fatal_callback(repository, failure):
    def fail_confirmation(*args):
        if failure == "sqlite_corruption":
            error = sqlite3.DatabaseError("synthetic corruption")
            error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
            raise error
        raise failure

    async def scenario():
        repository.save_settings(LocalSettings(server_url="https://ingest.example"))
        repository.insert_snapshot(record(repository))
        repository.apply_confirmation = fail_confirmation
        errors = []
        scheduler = UploadScheduler(
            repository,
            AlwaysConfirmTransport(),
            "synthetic-token",
            on_fatal=errors.append,
        )
        await scheduler.run()
        assert errors == ["storage_error"]
        assert scheduler.observation.state == "failed"
        assert repository.get_status().pending_count == 1

    asyncio.run(scenario())


def test_restart_reconstructs_delivery_without_process_local_block(repository):
    async def scenario():
        repository.save_settings(LocalSettings(server_url="https://ingest.example"))
        repository.insert_snapshot(record(repository))
        blocked = UploadScheduler(repository, AuthenticationTransport(), "synthetic-token")
        first_worker = asyncio.create_task(blocked.run())
        try:
            for _ in range(100):
                if blocked.observation.state == "blocked":
                    break
                await asyncio.sleep(0.01)
            assert blocked.observation.global_error == "invalid_credential"
        finally:
            blocked.stop()
            await asyncio.wait_for(first_worker, 2)

        transport = RetryTransport()
        restarted = UploadScheduler(repository, transport, "synthetic-token")
        second_worker = asyncio.create_task(restarted.run())
        try:
            for _ in range(200):
                if repository.get_status().pending_count == 0:
                    break
                await asyncio.sleep(0.01)
            assert repository.get_status().pending_count == 0
            assert len(transport.calls) == 2
        finally:
            restarted.stop()
            await asyncio.wait_for(second_worker, 2)

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_worker", [False, True])
def test_stop_or_cancel_cancels_transport_without_acknowledging(repository, cancel_worker):
    class WaitingTransport:
        def __init__(self):
            self.entered = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def send(self, origin, token, group):
            self.entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    async def scenario():
        repository.save_settings(LocalSettings(server_url="https://ingest.example"))
        repository.insert_snapshot(record(repository))
        transport = WaitingTransport()
        scheduler = UploadScheduler(repository, transport, "synthetic-token")
        worker = asyncio.create_task(scheduler.run())
        await asyncio.wait_for(transport.entered.wait(), 3)
        if cancel_worker:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
        else:
            scheduler.stop()
            await asyncio.wait_for(worker, 2)
        assert transport.cancelled.is_set()
        assert repository.get_status().pending_count == 1

    asyncio.run(scenario())


def test_cancel_waits_for_started_database_operation_before_return(repository):
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    original = repository.load_installation

    def delayed_load():
        entered.set()
        release.wait()
        try:
            return original()
        finally:
            completed.set()

    async def scenario():
        repository.load_installation = delayed_load
        scheduler = UploadScheduler(repository, ConfirmingTransport(), "synthetic-token")
        worker = asyncio.create_task(scheduler.run())
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            worker.cancel()
            await asyncio.sleep(0.05)
            assert not worker.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(worker, 2)
            assert completed.is_set()
        finally:
            release.set()
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


def test_invalid_confirmation_does_not_acknowledge_and_uses_backoff(repository):
    class InvalidThenConfirm:
        def __init__(self):
            self.calls = 0

        async def send(self, origin, token, group):
            self.calls += 1
            if self.calls == 1:
                return TransportResult(200, error_code="invalid_confirmation")
            return TransportResult(200, confirmation=confirmed(group))

    async def scenario():
        repository.save_settings(LocalSettings(server_url="https://ingest.example"))
        repository.insert_snapshot(record(repository))
        transport = InvalidThenConfirm()
        delays = []

        async def immediate_wait(delay, wake, stop):
            delays.append(delay)
            return "timer"

        scheduler = UploadScheduler(repository, transport, "synthetic-token", wait=immediate_wait)
        worker = asyncio.create_task(scheduler.run())
        try:
            for _ in range(100):
                if repository.get_status().pending_count == 0:
                    break
                await asyncio.sleep(0.01)
            assert repository.get_status().pending_count == 0
            assert transport.calls == 2
            assert delays[0] == 1
        finally:
            scheduler.stop()
            await asyncio.wait_for(worker, 2)

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [100, 600, 700])
def test_unrecognized_status_uses_protocol_error_and_bounded_backoff(repository, status):
    class UnknownStatusTransport:
        async def send(self, origin, token, group):
            return TransportResult(status, error_code="upload_http_rejected")

    async def scenario():
        repository.save_settings(LocalSettings(server_url="https://ingest.example"))
        repository.insert_snapshot(record(repository))
        scheduler = UploadScheduler(repository, UnknownStatusTransport(), "synthetic-token")
        seen = []

        async def stop_after_error(delay, wake, stop):
            seen.append((delay, scheduler.observation.global_error))
            return "stop"

        scheduler._wait = stop_after_error
        await scheduler.run()
        assert seen == [(1.0, "upload_protocol_error")]
        assert repository.get_status().pending_count == 1

    asyncio.run(scenario())


def test_transport_exception_has_fixed_network_error_and_backoff(repository):
    class BrokenTransport:
        async def send(self, origin, token, group):
            raise RuntimeError("synthetic private exception text")

    async def scenario():
        repository.save_settings(LocalSettings(server_url="https://ingest.example"))
        repository.insert_snapshot(record(repository))
        scheduler = UploadScheduler(repository, BrokenTransport(), "synthetic-token")
        seen = []

        async def stop_after_error(delay, wake, stop):
            seen.append((delay, scheduler.observation.global_error))
            return "stop"

        scheduler._wait = stop_after_error
        await scheduler.run()
        assert seen == [(1.0, "upload_network_error")]
        assert repository.get_status().pending_count == 1

    asyncio.run(scenario())
