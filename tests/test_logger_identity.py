"""Durable installation/deployment acceptance with temporary credentials and fake drains."""

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import event, insert, select
from sqlalchemy.exc import IntegrityError

from watt_profile.contracts.domain import Snapshot
from watt_profile.logger.bootstrap import installation_lock
from watt_profile.logger.deployment import DeploymentCommand, DeploymentOperations, SettingsCommand
from watt_profile.logger.identity import (
    InstallationInconsistent,
    load_or_initialize_installation,
    read_upload_token,
)
from watt_profile.logger.storage import schema as db
from watt_profile.logger.storage.database import create_database, migrate
from watt_profile.logger.storage.repository import LocalSettings, LoggerRepository

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def installation(tmp_path):
    with installation_lock(tmp_path):
        engine = create_database(tmp_path / "logger.sqlite3")
        migrate(engine)
        repo = LoggerRepository(engine)
        load_or_initialize_installation(repo, tmp_path, NOW)
        try:
            yield repo, tmp_path
        finally:
            engine.dispose()


def test_identity_restart_and_privacy(installation):
    repo, directory = installation
    original = repo.load_installation()
    token = read_upload_token(directory)
    assert load_or_initialize_installation(repo, directory, NOW) == original
    assert read_upload_token(directory) == token
    assert len(token) == 43
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "upload-token").stat().st_mode & 0o777 == 0o600
    assert token not in original.model_dump_json()
    assert token not in repo.get_status().model_dump_json()


@pytest.mark.parametrize("failure", ["missing", "invalid", "mode", "symlink", "multiple_devices"])
def test_inconsistent_installation_refused(installation, failure):
    repo, directory = installation
    token = directory / "upload-token"
    original = repo.load_installation()
    if failure == "multiple_devices":
        with repo.engine.begin() as connection:
            connection.execute(
                insert(db.devices).values(
                    device_id=str(uuid4()), created_at="2026-01-01T00:00:00.000000Z"
                )
            )
    elif failure == "mode":
        token.chmod(0o644)
    else:
        token.unlink()
        if failure == "invalid":
            token.write_text("private-invalid-token")
            token.chmod(0o600)
        if failure == "symlink":
            target = directory / "other"
            target.write_text("a" * 43)
            target.chmod(0o600)
            token.symlink_to(target)
    with pytest.raises(InstallationInconsistent, match="^installation_inconsistent$") as error:
        load_or_initialize_installation(repo, directory, NOW)
    assert "private-invalid-token" not in str(error.value)
    if failure != "multiple_devices":
        assert repo.load_installation() == original


@pytest.mark.parametrize(
    "boundary", ["before_publish", "after_publish", "before_commit", "after_commit"]
)
def test_interrupted_initialization_recovers(tmp_path, monkeypatch, boundary):
    import watt_profile.logger.identity as identity

    with installation_lock(tmp_path):
        engine = create_database(tmp_path / "logger.sqlite3")
        migrate(engine)
        repo = LoggerRepository(engine)
        original_publish = identity.os.replace
        original_initialize = repo.initialize_installation
        original_fsync = identity.os.fsync

        def fail(*args, **kwargs):
            raise RuntimeError("synthetic_interruption")

        def after_publish(*args, **kwargs):
            original_publish(*args, **kwargs)
            fail()

        def after_commit(*args, **kwargs):
            original_initialize(*args, **kwargs)
            fail()

        def before_commit(connection):
            fail()

        if boundary == "before_publish":
            monkeypatch.setattr(identity.os, "fsync", fail)
        elif boundary == "after_publish":
            monkeypatch.setattr(identity.os, "replace", after_publish)
        elif boundary == "before_commit":
            event.listen(engine, "commit", before_commit)
        else:
            monkeypatch.setattr(repo, "initialize_installation", after_commit)
        try:
            with pytest.raises(RuntimeError, match="synthetic_interruption"):
                identity.load_or_initialize_installation(repo, tmp_path, NOW)
            monkeypatch.setattr(identity.os, "replace", original_publish)
            monkeypatch.setattr(identity.os, "fsync", original_fsync)
            monkeypatch.setattr(repo, "initialize_installation", original_initialize)
            if boundary == "before_commit":
                event.remove(engine, "commit", before_commit)
            published = (
                read_upload_token(tmp_path) if (tmp_path / "upload-token").exists() else None
            )
            committed = repo.load_installation()
            recovered = identity.load_or_initialize_installation(repo, tmp_path, NOW)
            if published:
                assert read_upload_token(tmp_path) == published
            if committed:
                assert recovered == committed
            assert not list(tmp_path.glob(".upload-token-*"))
        finally:
            engine.dispose()


@pytest.mark.parametrize("boundary", ["after_rename", "directory_fsync"])
@pytest.mark.parametrize("retry_failure", ["file", "directory"])
def test_published_token_recovery_syncs_before_commit(
    tmp_path, monkeypatch, boundary, retry_failure
):
    import watt_profile.logger.identity as identity

    with installation_lock(tmp_path):
        engine = create_database(tmp_path / "logger.sqlite3")
        migrate(engine)
        repo = LoggerRepository(engine)
        original_replace = identity.os.replace
        original_fsync = identity.os.fsync
        observed = []
        failing_sync = "directory" if boundary == "directory_fsync" else None

        def fsync(descriptor):
            kind = "directory" if stat.S_ISDIR(identity.os.fstat(descriptor).st_mode) else "file"
            observed.append(kind)
            if kind == failing_sync:
                raise OSError("synthetic_sync_failure")
            original_fsync(descriptor)

        def replace(*args):
            original_replace(*args)
            if boundary == "after_rename":
                raise RuntimeError("synthetic_rename_interruption")

        def before_commit(connection):
            if connection.execute(select(db.devices.c.device_id)).first() is None:
                return
            assert observed == ["file", "directory"]
            observed.append("commit")

        monkeypatch.setattr(identity.os, "fsync", fsync)
        monkeypatch.setattr(identity.os, "replace", replace)
        event.listen(engine, "commit", before_commit)
        try:
            with pytest.raises((OSError, RuntimeError), match="synthetic_"):
                load_or_initialize_installation(repo, tmp_path, NOW)
            assert repo.load_installation() is None
            token = read_upload_token(tmp_path)
            assert observed == (["file"] if boundary == "after_rename" else ["file", "directory"])
            monkeypatch.setattr(identity.os, "replace", original_replace)

            observed.clear()
            failing_sync = retry_failure
            with pytest.raises((OSError, InstallationInconsistent)):
                load_or_initialize_installation(repo, tmp_path, NOW)
            assert repo.load_installation() is None
            assert read_upload_token(tmp_path) == token
            assert observed == (["file"] if retry_failure == "file" else ["file", "directory"])

            observed.clear()
            failing_sync = None
            recovered = load_or_initialize_installation(repo, tmp_path, NOW)
            assert observed == ["file", "directory", "commit"]
            event.remove(engine, "commit", before_commit)
            assert repo.load_installation() == recovered
            assert read_upload_token(tmp_path) == token
            assert not list(tmp_path.glob(".upload-token-*"))
        finally:
            if event.contains(engine, "commit", before_commit):
                event.remove(engine, "commit", before_commit)
            engine.dispose()


def deploy(ops, site, **kwargs):
    selected = ops.state.session.descriptor.session_id if ops.state.session else None
    return ops.deploy(
        DeploymentCommand(site_id=site.site_id, expected_session_id=selected, **kwargs), NOW
    )


def test_sites_and_a_b_a_ownership(installation):
    repo, _ = installation
    ops = DeploymentOperations(repo, lambda state: NOW)
    a = ops.create_site(address="synthetic A")
    b = ops.create_site()
    assert ops.state.session is None
    assert ops.create_site(site_id=a.site_id, address="ignored") == a
    a1 = deploy(ops, a).lifecycle
    with pytest.raises(ValueError, match="same_site"):
        deploy(ops, a, confirm_switch=True)
    with pytest.raises(ValueError, match="switch_confirmation_required"):
        deploy(ops, b)
    b1 = deploy(ops, b, confirm_switch=True).lifecycle
    a2 = deploy(ops, a, confirm_switch=True).lifecycle
    assert len({s.session.descriptor.session_id for s in (a1, b1, a2)}) == 3
    assert len({s.source.descriptor.source_id for s in (a1, b1, a2)}) == 3
    assert a1.source.descriptor.measurement_point_id == a2.source.descriptor.measurement_point_id
    assert sum(s.state == "completed" for s in repo.list_sessions()) == 2
    with pytest.raises(ValueError, match="stale_session"):
        ops.complete(a1.session.descriptor.session_id, NOW)
    selected = a2.session.descriptor.session_id
    ops.complete(selected, NOW)
    assert ops.state.session.state == "completed"
    assert ops.state.session.descriptor.session_id == selected
    assert deploy(ops, a).lifecycle.session.descriptor.session_id != selected


def test_drain_commits_pause_before_atomic_switch_and_failed_switch(installation):
    repo, _ = installation
    observed = []

    def drain(state):
        persisted = repo.load_lifecycle()
        assert persisted.session.descriptor == state.session.descriptor
        assert not persisted.session.collection_enabled
        assert persisted.session.state == "open"
        # This independent write proves no transaction spans the callback.
        repo.update_blocks("storage_full", blocked=True)
        observed.append(state)
        return NOW + timedelta(seconds=10)

    ops = DeploymentOperations(repo, drain)
    a = ops.create_site()
    old = deploy(ops, a).lifecycle
    selected = old.session.descriptor.session_id
    assert ops.start(selected, input_mode="synthetic").result == "ok"
    ops.owner_started()
    with pytest.raises(IntegrityError):
        ops.deploy(
            DeploymentCommand(site_id=uuid4(), expected_session_id=selected, confirm_switch=True),
            NOW,
        )
    assert len(observed) == 1
    state = repo.load_lifecycle()
    assert state.session.descriptor == old.session.descriptor
    assert state.session.state == "open" and not state.session.collection_enabled
    assert state.persisted_blocks == ("storage_full",)
    assert len(repo.list_sessions()) == 1
    assert (
        DeploymentOperations(repo, drain).restore_intent().lifecycle.collection_state == "blocked"
    )


def test_pause_restart_settings_and_meter_blocks(installation, monkeypatch):
    repo, _ = installation
    drains = []
    ops = DeploymentOperations(repo, lambda state: drains.append(state) or NOW)
    a = ops.create_site()
    old = deploy(ops, a).lifecycle
    selected = old.session.descriptor.session_id
    with pytest.raises(ValueError, match="serial_path_required"):
        ops.start(selected)
    assert not repo.load_lifecycle().session.collection_enabled
    ops.start(selected, input_mode="synthetic")
    assert (
        DeploymentOperations(repo, lambda state: NOW).restore_intent().lifecycle.collection_state
        == "starting"
    )
    ops.owner_started()
    ops.save_settings(
        SettingsCommand(
            settings=LocalSettings(server_url="https://example.test"), expected_session_id=selected
        ),
        NOW,
    )
    assert not drains and ops.state.collection_state == "collecting"
    original_save = repo.save_settings

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic_save_failure")

    monkeypatch.setattr(repo, "save_settings", fail)
    with pytest.raises(RuntimeError, match="synthetic_save_failure"):
        ops.save_settings(
            SettingsCommand(
                settings=LocalSettings(serial_path="synthetic-port"), expected_session_id=selected
            ),
            NOW,
        )
    monkeypatch.setattr(repo, "save_settings", original_save)
    assert len(drains) == 1
    assert repo.load_installation().settings.serial_path is None
    assert not repo.load_lifecycle().session.collection_enabled
    assert ops.state.session.descriptor == old.session.descriptor
    assert ops.state.source.descriptor == old.source.descriptor
    repo.bind_meter(selected, None)
    assert repo.load_lifecycle().source.meter_fingerprint is None
    repo.bind_meter(selected, "a" * 64)
    repo.bind_meter(selected, "b" * 64)
    repo.update_blocks("storage_full", blocked=True)
    result = ops.confirm_meter_change(selected, NOW)
    assert result.lifecycle.session.descriptor.session_id != selected
    assert result.lifecycle.source.meter_fingerprint is None
    assert result.lifecycle.persisted_blocks == ("storage_full",)
    repo.bind_meter(result.lifecycle.session.descriptor.session_id, "a" * 64)
    repo.bind_meter(result.lifecycle.session.descriptor.session_id, "b" * 64)
    assert repo.update_blocks("storage_full", blocked=False).persisted_blocks == ("meter_changed",)


def test_successful_switch_uses_drain_time_and_preserves_new_blocks(installation):
    repo, _ = installation
    ended = NOW + timedelta(seconds=10)

    def drain(state):
        repo.update_blocks("storage_full", blocked=True)
        return ended

    ops = DeploymentOperations(repo, drain)
    a, b = ops.create_site(), ops.create_site()
    old = deploy(ops, a).lifecycle
    selected = old.session.descriptor.session_id
    ops.start(selected, input_mode="synthetic")
    ops.owner_started()
    switched = deploy(ops, b, confirm_switch=True).lifecycle
    assert switched.session.descriptor.started_at == ended
    assert switched.persisted_blocks == ("storage_full",)
    historical = next(s for s in repo.list_sessions() if s.descriptor.session_id == selected)
    assert historical.ended_at == ended and not historical.collection_enabled


def test_failed_drain_prevents_start_until_owner_release(installation):
    repo, _ = installation

    def drain(state):
        raise RuntimeError("synthetic_join_failure")

    ops = DeploymentOperations(repo, drain)
    selected = deploy(ops, ops.create_site()).lifecycle.session.descriptor.session_id
    ops.start(selected, input_mode="synthetic")
    ops.owner_started()
    with pytest.raises(RuntimeError, match="synthetic_join_failure"):
        ops.pause(selected, NOW)
    assert ops.state.collection_state == "stopping"
    assert ops.start(selected, input_mode="synthetic").result == "busy"
    assert not repo.load_lifecycle().session.collection_enabled
    ops.drain = lambda state: NOW + timedelta(seconds=10)
    assert ops.pause(selected, NOW).lifecycle.collection_state == "idle"


def test_serial_save_pause_and_stale_settings_transaction(installation):
    repo, _ = installation
    ops = DeploymentOperations(repo, lambda state: NOW)
    selected = deploy(ops, ops.create_site()).lifecycle.session.descriptor.session_id
    settings = LocalSettings(serial_path="synthetic-port")
    assert (
        ops.save_settings(SettingsCommand(settings=settings, expected_session_id=selected), NOW)
        == settings
    )
    ops.start(selected)
    ops.owner_started()
    changed = LocalSettings(serial_path="synthetic-other")
    ops.save_settings(SettingsCommand(settings=changed, expected_session_id=selected), NOW)
    assert ops.state.collection_state == "idle"
    assert not ops.state.session.collection_enabled
    assert ops.state.session.descriptor.session_id == selected
    with pytest.raises(ValueError, match="stale_session"):
        repo.save_settings(settings, expected_session_id=uuid4())
    assert repo.load_installation().settings == changed


def test_rejected_origin_only_edit_keeps_active_collection_and_all_settings(installation):
    repo, _ = installation
    drains = []
    ops = DeploymentOperations(repo, lambda state: drains.append(state) or NOW)
    selected = deploy(ops, ops.create_site()).lifecycle.session.descriptor.session_id
    ops.start(selected, input_mode="synthetic")
    ops.owner_started()
    current = LocalSettings(server_url="https://old.example")
    ops.save_settings(SettingsCommand(settings=current, expected_session_id=selected), NOW)

    state = repo.load_lifecycle()
    data = json.loads((Path(__file__).parent.parent / "examples/ingest/request.json").read_bytes())[
        "records"
    ][0]
    source = state.source.descriptor
    data.update(
        record_id=str(uuid4()),
        device_id=str(state.device_id),
        site_id=str(state.session.descriptor.site_id),
        session_id=str(selected),
        source_id=str(source.source_id),
        measurement_point_id=str(source.measurement_point_id),
        meter_time_utc=None,
    )
    repo.insert_snapshot(Snapshot.model_validate(data))
    changed = LocalSettings(server_url="https://new.example")
    with pytest.raises(ValueError, match="^pending_uploads_prevent_server_change$"):
        ops.save_settings(SettingsCommand(settings=changed, expected_session_id=selected), NOW)
    assert repo.load_installation().settings == current
    assert repo.load_lifecycle().session.collection_enabled
    assert ops.state.collection_state == "collecting"
    assert not drains
