"""Offline acceptance through real synthetic bytes, runtime and durable recovery."""

import socket
from decimal import Decimal

import pytest
from sqlalchemy import text
from test_logger_runtime import Factory, FakeClock, eventually, feed, session, start

from watt_profile.contracts.domain import canonical_snapshot_bytes, payload_sha256
from watt_profile.contracts.upload import UploadConfirmation
from watt_profile.logger.deployment import DeploymentCommand
from watt_profile.logger.identity import load_or_initialize_installation, read_upload_token
from watt_profile.logger.maintenance import backup, restore
from watt_profile.logger.runtime import LoggerRuntime
from watt_profile.logger.storage.database import create_database, migrate
from watt_profile.logger.storage.repository import LocalSettings, LoggerRepository
from watt_profile.logger.synthetic import synthetic_telegram


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    # Saving an origin must never cause delivery, even during collection/recovery.
    def forbid_network(*args, **kwargs):
        raise AssertionError("M2 attempted network delivery")

    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    monkeypatch.setattr(socket.socket, "connect_ex", forbid_network)
    directory = tmp_path / "installation"
    directory.mkdir(mode=0o700)
    engine = create_database(directory / "logger.sqlite3")
    migrate(engine)
    repo = LoggerRepository(engine)
    clock, factory = FakeClock(), Factory()
    load_or_initialize_installation(repo, directory, clock.utc_now())
    repo.save_settings(LocalSettings(server_url="https://upload.invalid"))
    runtimes = []

    def open_runtime(repository=repo):
        runtime = LoggerRuntime(
            repository,
            clock=clock,
            serial_factory=factory,
            input_mode="synthetic",
            health_guard=lambda: None,
        )
        runtimes.append(runtime)
        return runtime

    try:
        yield directory, engine, repo, clock, factory, open_runtime
    finally:
        for runtime in reversed(runtimes):
            if not runtime._closed:
                # All assertions finish windows normally; allow cleanup after a failure too.
                clock.advance(10)
                runtime.shutdown()
        engine.dispose()


def collect_window(runtime, repo, clock, port):
    before = repo.get_status().pending_count
    clock.advance(1)
    feed(runtime, port, synthetic_telegram(clock.utc_now()))
    clock.advance(8)
    received = clock.utc_now()
    feed(runtime, port, synthetic_telegram(received))
    clock.advance(1)
    eventually(lambda: repo.get_status().pending_count == before + 1)
    return received


def stored_rows(engine):
    with engine.connect() as connection:
        return (
            connection.execute(text("SELECT * FROM snapshots ORDER BY record_id")).mappings().all()
        )


@pytest.mark.parametrize("enabled", [False, True])
def test_real_synthetic_pipeline_restart_and_backup(pipeline, tmp_path, enabled):
    directory, engine, repo, clock, factory, open_runtime = pipeline
    runtime = open_runtime()
    site = runtime.create_site(address="Invented offline site")
    runtime.switch_site(DeploymentCommand(site_id=site.site_id, expected_session_id=None))
    owner = repo.load_lifecycle()
    port = start(runtime, factory)
    received = collect_window(runtime, repo, clock, port)
    group = repo.select_pending_group()
    snapshot = group.batch.records[0]
    assert snapshot.received_at == received
    assert snapshot.sampled_at == clock.utc_now()
    assert snapshot.metrics.energy_import_t1_kwh == Decimal(int(received.timestamp())) / 1000
    assert snapshot.metrics.energy_import_t1_kwh.as_tuple().exponent == -3
    assert snapshot.metrics.voltage_l2_v is None
    assert len(type(snapshot.metrics).model_fields) == 18
    assert group.canonical_records == (canonical_snapshot_bytes(snapshot),)
    assert payload_sha256(snapshot) == stored_rows(engine)[0]["payload_sha256"]
    assert b"WATT-SYNTHETIC-METER-01" not in group.body
    assert b'"<redacted>"' in group.body
    if not enabled:
        runtime.pause(session(runtime))
    token = read_upload_token(directory)
    runtime.shutdown()
    assert port.closed
    rows = stored_rows(engine)
    engine.dispose()
    migrate(engine)
    assert load_or_initialize_installation(repo, directory, clock.utc_now()).device_id == (
        owner.session.descriptor.device_id
    )
    restarted = open_runtime()
    restarted.restore_intent()
    assert repo.load_lifecycle().session.descriptor == owner.session.descriptor
    assert repo.load_lifecycle().source.descriptor == owner.source.descriptor
    assert read_upload_token(directory) == token
    assert repo.select_pending_group() == group
    assert stored_rows(engine) == rows
    assert restarted.get_status()["latest_live_reading"] is None
    if enabled:
        eventually(lambda: len(factory.ports) == 2)
        eventually(lambda: restarted.get_status()["logger"].collection_state == "collecting")
    else:
        assert len(factory.ports) == 1
        assert restarted.get_status()["logger"].collection_state == "idle"
    clock.advance(30)
    assert repo.select_pending_group() == group  # No stale candidate or catch-up copies.
    saved, target = tmp_path / "backup", tmp_path / "restored"
    before = repo.load_installation()
    backup(directory, saved)
    restore(saved, target)
    restored_engine = create_database(target / "logger.sqlite3")
    try:
        restored = LoggerRepository(restored_engine)
        assert restored.load_installation() == before
        assert restored.select_pending_group() == group
        assert stored_rows(restored_engine) == rows
        assert read_upload_token(target) == token
        assert len(factory.ports) == (2 if enabled else 1)  # Restore starts no reader.
    finally:
        restored_engine.dispose()
    assert repo.get_status().last_successful_upload_at is None
    assert all(row["upload_state"] == "pending" for row in rows)


def test_site_changes_confirmation_cleanup_and_backup_keep_pending_bytes(pipeline, tmp_path):
    directory, engine, repo, clock, factory, open_runtime = pipeline
    runtime = open_runtime()
    a = runtime.create_site(address="Invented A", description="Synthetic metadata")
    b = runtime.create_site(address="Invented B")
    runtime.switch_site(DeploymentCommand(site_id=a.site_id, expected_session_id=None))
    first = repo.load_lifecycle()
    collect_window(runtime, repo, clock, start(runtime, factory))
    a_group = repo.select_pending_group()
    runtime.switch_site(
        DeploymentCommand(
            site_id=b.site_id, expected_session_id=session(runtime), confirm_switch=True
        )
    )
    second = repo.load_lifecycle()
    assert second.session.descriptor.session_id != first.session.descriptor.session_id
    collect_window(runtime, repo, clock, start(runtime, factory))
    runtime.complete_session(session(runtime))
    runtime.create_site(site_id=a.site_id, address="Must not overwrite existing metadata")
    runtime.switch_site(DeploymentCommand(site_id=a.site_id, expected_session_id=session(runtime)))
    third = repo.load_lifecycle()
    assert third.source.descriptor.measurement_point_id == (
        first.source.descriptor.measurement_point_id
    )
    assert len({s.source.descriptor.source_id for s in (first, second, third)}) == 3
    assert repo.get_site(a.site_id) == a
    assert repo.select_pending_group() == a_group
    assert runtime.get_status()["active_session_latest_received_at"] is None
    rows = stored_rows(engine)
    ids = tuple(item.descriptor.session_id for item in repo.list_sessions())
    assert runtime.cleanup_completed(ids)["deleted_count"] == 0
    assert stored_rows(engine) == rows
    # A legitimate local confirmation tests the future uploader's storage primitive only.
    batch = a_group.batch
    repo.apply_confirmation(
        batch,
        UploadConfirmation(
            schema_version=1,
            device_id=batch.device_id,
            confirmed_record_ids=tuple(record.record_id for record in batch.records),
        ),
        clock.utc_now(),
    )
    pending = repo.select_pending_group()
    assert pending.batch.site_id == b.site_id
    assert runtime.cleanup_completed(ids)["deleted_count"] == 1
    assert repo.select_pending_group() == pending
    assert len(repo.list_sessions()) == 3
    backup(directory, tmp_path / "backup")
    restore(tmp_path / "backup", tmp_path / "restored")
    restored_engine = create_database(tmp_path / "restored/logger.sqlite3")
    try:
        restored = LoggerRepository(restored_engine)
        assert restored.load_installation() == repo.load_installation()
        assert restored.select_pending_group() == pending
        assert stored_rows(restored_engine) == stored_rows(engine)
        assert read_upload_token(tmp_path / "restored") == read_upload_token(directory)
    finally:
        restored_engine.dispose()
