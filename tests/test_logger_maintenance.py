"""Isolated synthetic pressure, cleanup and private recovery acceptance."""

import json
import os
import sqlite3
import stat
import subprocess
import sys

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import DatabaseError
from test_logger_runtime import (
    eventually,
    feed,
    session,
    start,
    threaded,
)
from test_logger_runtime import runtime as runtime
from test_logger_storage import NOW, confirmation, record
from test_logger_storage import repository as repository

from watt_profile.contracts.lifecycle import complete_session
from watt_profile.logger.bootstrap import installation_lock
from watt_profile.logger.deployment import DeploymentCommand, SettingsCommand
from watt_profile.logger.identity import load_or_initialize_installation, read_upload_token
from watt_profile.logger.maintenance import MIN_FREE_BYTES, StorageHealthGuard, backup, restore
from watt_profile.logger.storage import schema as db
from watt_profile.logger.storage.database import create_database, migrate
from watt_profile.logger.storage.repository import LoggerRepository


@pytest.mark.parametrize("capacity", [MIN_FREE_BYTES - 1, MIN_FREE_BYTES, MIN_FREE_BYTES + 1])
def test_health_threshold(repository, capacity):
    guard = StorageHealthGuard(repository, lambda: capacity)
    if capacity < MIN_FREE_BYTES:
        with pytest.raises(OSError, match="storage_full"):
            guard()
    else:
        guard()


def test_pressure_discards_candidate_and_joins(runtime):
    rt, repo, clock, factory = runtime
    port = start(rt, factory)
    feed(rt, port)
    rt.health_guard = StorageHealthGuard(repo, lambda: MIN_FREE_BYTES - 1)
    clock.advance(10)
    eventually(lambda: "storage_full" in repo.load_lifecycle().persisted_blocks)
    eventually(lambda: not rt._reader.is_alive() and not rt._window.is_alive())
    assert port.closed and rt._sampler is None
    assert repo.get_status().pending_count == 0
    rt.storage_guard.free_bytes = lambda: MIN_FREE_BYTES
    assert rt.resolve_storage_block().result == "ok"
    assert not repo.load_lifecycle().session.collection_enabled
    assert rt.operations.state.collection_state == "idle"
    assert len(factory.ports) == 1


def test_failed_write_and_failed_block_persistence(runtime, monkeypatch):
    rt, repo, clock, factory = runtime
    committed = record(repo)
    repo.insert_snapshot(committed)
    port = start(rt, factory)
    feed(rt, port)
    original_blocks = repo.update_blocks

    def fail(*args, **kwargs):
        raise OSError("injected failure")

    monkeypatch.setattr(repo, "insert_snapshot", fail)
    monkeypatch.setattr(repo, "update_blocks", fail)
    clock.advance(10)
    eventually(lambda: "storage_full" in rt.operations.unsaved_blocks)
    eventually(lambda: not rt._reader.is_alive() and not rt._window.is_alive())
    assert "storage_full" not in repo.load_lifecycle().persisted_blocks
    assert repo.select_pending_group().batch.records == (committed,)
    assert rt.get_status()["logger"].storage_blocked
    assert rt.get_status()["persisted_blocks"] == ()
    rt.storage_guard.free_bytes = lambda: MIN_FREE_BYTES
    monkeypatch.setattr(repo, "check_safe_write", fail)
    with pytest.raises(OSError, match="storage_full"):
        rt.resolve_storage_block()
    assert "storage_full" in rt.operations.unsaved_blocks
    monkeypatch.undo()
    assert original_blocks
    eventually(lambda: not rt._reader.is_alive())
    rt.resolve_storage_block()
    assert not rt.operations.unsaved_blocks


@pytest.mark.parametrize("storage_first", [True, False])
def test_resolution_orders(runtime, storage_first):
    rt, repo, _, _ = runtime
    repo.update_blocks("meter_changed", blocked=True)
    repo.update_blocks("storage_full", blocked=True)
    rt.operations.state = repo.load_lifecycle()
    rt.storage_guard.free_bytes = lambda: MIN_FREE_BYTES
    selected = session(rt)
    if storage_first:
        rt.resolve_storage_block()
        assert repo.load_lifecycle().persisted_blocks == ("meter_changed",)
        rt.confirm_meter_change(selected)
    else:
        rt.confirm_meter_change(selected)
        assert repo.load_lifecycle().persisted_blocks == ("storage_full",)
        rt.resolve_storage_block()
    assert not repo.load_lifecycle().persisted_blocks
    assert not repo.load_lifecycle().session.collection_enabled


def test_cleanup_completed_uploaded_only(repository):
    repo = repository
    uploaded, pending = record(repo), record(repo)
    repo.insert_snapshot(uploaded)
    batch = repo.select_pending_group().batch
    repo.apply_confirmation(batch, confirmation(batch), NOW)
    repo.insert_snapshot(pending)
    selected = repo.load_lifecycle().session.descriptor.session_id
    assert repo.cleanup_summary()[0]["eligible_uploaded_count"] == 0
    assert repo.cleanup_completed((selected,)) == 0
    repo.commit_deployment_change(
        selected, complete_session(repo.load_lifecycle(), selected, NOW).lifecycle
    )
    summary = repo.cleanup_summary()[0]
    assert summary["total_count"] == 2 and summary["pending_count"] == 1
    assert summary["eligible_uploaded_count"] == 1
    assert repo.cleanup_completed((selected,)) == 1
    assert repo.select_pending_group().batch.records == (pending,)
    assert len(repo.list_sessions()) == 1


@pytest.mark.parametrize("command", ["cleanup", "metadata", "settings"])
@pytest.mark.parametrize("failure", ["io", "busy", "corrupt"])
def test_command_storage_failure_stops_immediately(runtime, monkeypatch, command, failure):
    rt, repo, clock, factory = runtime
    committed = record(repo)
    repo.insert_snapshot(committed)
    site = rt.create_site()
    port = start(rt, factory)
    feed(rt, port)
    assert rt._sampler.has_candidate
    if failure == "io":
        error = OSError("synthetic private failure")
    else:
        error = sqlite3.OperationalError("synthetic private failure")
        error.sqlite_errorcode = (
            sqlite3.SQLITE_BUSY if failure == "busy" else sqlite3.SQLITE_CORRUPT
        )

    def fail(*args, **kwargs):
        raise error

    if command == "cleanup":
        monkeypatch.setattr(repo, "cleanup_completed", fail)

        def action():
            return rt.cleanup_completed((session(rt),))
    elif command == "metadata":
        monkeypatch.setattr(repo, "save_site", fail)

        def action():
            return rt.edit_site(site)
    else:
        settings = repo.load_installation().settings.model_copy(
            update={"server_url": "http://local"}
        )
        monkeypatch.setattr(repo, "save_settings", fail)

        def action():
            return rt.save_settings(
                SettingsCommand(settings=settings, expected_session_id=session(rt))
            )

    expected_error = RuntimeError if failure == "corrupt" else OSError
    expected_code = "storage_fatal" if failure == "corrupt" else "storage_full"
    with pytest.raises(expected_error, match="^" + expected_code + "$"):
        action()
    assert clock.monotonic() == 0
    assert port.closed and rt._sampler is None
    eventually(lambda: not rt._reader.is_alive() and not rt._window.is_alive())
    assert repo.select_pending_group().batch.records == (committed,)
    if failure == "corrupt":
        assert rt.runtime_error == "storage_fatal"
        assert not repo.load_lifecycle().persisted_blocks
    else:
        assert rt.runtime_error == "storage_error"
        assert repo.load_lifecycle().persisted_blocks == ("storage_full",)
    clock.advance(10)
    assert repo.select_pending_group().batch.records == (committed,)


def test_command_business_errors_preserve_collection(runtime):
    from uuid import uuid4

    rt, repo, _, factory = runtime
    port = start(rt, factory)
    feed(rt, port)
    with pytest.raises(ValueError, match="stale_session"):
        rt.pause(uuid4())
    assert not port.closed and rt._sampler.has_candidate
    assert not repo.load_lifecycle().persisted_blocks


def installation(directory):
    directory.mkdir(mode=0o700)
    engine = create_database(directory / "logger.sqlite3")
    migrate(engine)
    repo = LoggerRepository(engine)
    load_or_initialize_installation(repo, directory, NOW)
    return engine, repo


def test_online_wal_backup_restore_private_identity_bytes(tmp_path):
    from uuid import uuid4

    from watt_profile.contracts.lifecycle import new_deployment, start_collection
    from watt_profile.logger.storage.repository import Site

    source, saved, target = tmp_path / "source", tmp_path / "backup", tmp_path / "target"
    engine, repo = installation(source)
    try:
        site = repo.save_site(Site(site_id=uuid4(), address="synthetic site"))
        state = repo.commit_deployment_change(
            None, new_deployment(repo.load_lifecycle(), site.site_id, NOW).lifecycle
        )
        owner = state.session.descriptor.session_id
        repo.commit_deployment_change(owner, start_collection(state, owner).lifecycle)
        first = record(repo)
        repo.insert_snapshot(first)
        group = repo.select_pending_group()
        repo.apply_confirmation(group.batch, confirmation(group.batch), NOW)
        second = record(repo)
        repo.insert_snapshot(second)
        repo.update_blocks("meter_changed", blocked=True)
        repo.update_blocks("storage_full", blocked=True)
        # A live read transaction pins WAL while the backup uses its own connection.
        with engine.connect() as pinned:
            pinned.exec_driver_sql("BEGIN")
            pinned.exec_driver_sql("SELECT * FROM snapshots").all()
            repo.insert_snapshot(record(repo))
            assert (source / "logger.sqlite3-wal").stat().st_size > 0
            backup(source, saved)
        before = repo.load_installation()
        restore(saved, target)
        restored_engine = create_database(target / "logger.sqlite3")
        try:
            restored = LoggerRepository(restored_engine)
            assert restored.load_installation() == before
            assert restored.select_pending_group() == repo.select_pending_group()
            with engine.connect() as a, restored_engine.connect() as b:
                query = text("SELECT * FROM snapshots ORDER BY record_id")
                assert a.execute(query).all() == b.execute(query).all()
            assert read_upload_token(target) == read_upload_token(source)
        finally:
            restored_engine.dispose()
        assert stat.S_IMODE(saved.stat().st_mode) == 0o700
        for name in ("logger.sqlite3", "upload-token", "manifest.json"):
            assert stat.S_IMODE((saved / name).stat().st_mode) == 0o600
        with pytest.raises(FileExistsError):
            backup(source, saved)
        with pytest.raises(ValueError, match="restore_target_not_empty"):
            restore(saved, target)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "damage",
    [
        "manifest",
        "digest",
        "schema",
        "constraint",
        "index",
        "token",
        "invalid_token",
        "canonical",
        "revision",
    ],
)
def test_restore_rejects_invalid_without_installing(tmp_path, damage):
    import hashlib

    source, saved, target = tmp_path / "source", tmp_path / "backup", tmp_path / "target"
    engine, repo = installation(source)
    try:
        if damage == "canonical":
            from uuid import uuid4

            from watt_profile.contracts.lifecycle import new_deployment
            from watt_profile.logger.storage.repository import Site

            site = repo.save_site(Site(site_id=uuid4()))
            repo.commit_deployment_change(
                None, new_deployment(repo.load_lifecycle(), site.site_id, NOW).lifecycle
            )
            repo.insert_snapshot(record(repo))
        backup(source, saved)
    finally:
        engine.dispose()
    manifest = json.loads((saved / "manifest.json").read_bytes())
    if damage == "manifest":
        manifest["format_version"] = 2
    elif damage == "digest":
        (saved / "upload-token").write_bytes(b"x" * 43)
    elif damage == "token":
        (saved / "upload-token").unlink()
    elif damage == "invalid_token":
        (saved / "upload-token").write_bytes(b"!" * 43)
        manifest["sha256"]["upload-token"] = hashlib.sha256(b"!" * 43).hexdigest()
    else:
        bad_engine = create_database(saved / "logger.sqlite3")
        with bad_engine.begin() as connection:
            if damage == "schema":
                connection.exec_driver_sql("ALTER TABLE sites ADD COLUMN unexpected TEXT")
            elif damage == "constraint":
                connection.exec_driver_sql("PRAGMA writable_schema=ON")
                connection.exec_driver_sql(
                    "UPDATE sqlite_master SET sql=replace(sql, "
                    "'CHECK (collection_enabled IN (0,1))', "
                    "'CHECK (collection_enabled IN (0,1,2))') WHERE name='sessions'"
                )
                connection.exec_driver_sql("PRAGMA writable_schema=OFF")
            elif damage == "index":
                connection.exec_driver_sql("CREATE INDEX unexpected_index ON sites(address)")
            elif damage == "revision":
                connection.exec_driver_sql("UPDATE alembic_version SET version_num='logger_9999'")
            else:
                connection.execute(update(db.snapshots).values(payload_sha256="0" * 64))
        bad_engine.dispose()
        manifest["sha256"]["logger.sqlite3"] = hashlib.sha256(
            (saved / "logger.sqlite3").read_bytes()
        ).hexdigest()
    (saved / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(Exception):
        restore(saved, target)
    assert {p.name for p in target.iterdir()} == {"logger.lock"}


def test_cli_syntax_and_sanitized_errors(tmp_path):
    password = tmp_path / "password"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "watt_profile.cli.logger_admin",
            "generate-password",
            "--output",
            str(password),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and password.exists()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "watt_profile.cli.logger_admin",
            "backup",
            "--data-dir",
            str(tmp_path / "absent"),
            "--output",
            str(tmp_path / "backup"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1 and result.stderr.strip() == "backup_failed"
    assert not (tmp_path / "absent").exists()


def test_corruption_is_fatal_not_resolvable_pressure(runtime):
    rt, repo, _, _ = runtime
    error = sqlite3.DatabaseError("synthetic corruption")
    error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT

    def fail():
        raise error

    rt.health_guard = fail
    with pytest.raises(RuntimeError, match="storage_fatal"):
        rt.restore_intent()
    assert rt.runtime_error == "storage_fatal"
    assert not repo.load_lifecycle().persisted_blocks
    with pytest.raises(RuntimeError, match="storage_fatal"):
        rt.resolve_storage_block()


@pytest.mark.parametrize("error_code", [sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB])
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("phase", ["startup", "collection", "meter_change"])
def test_block_persistence_corruption_latches_fatal(
    runtime, monkeypatch, error_code, wrapped, phase
):
    rt, repo, clock, factory = runtime
    committed = record(repo)
    repo.insert_snapshot(committed)
    selected = session(rt)
    port = None
    if phase != "startup":
        port = start(rt, factory)
        feed(rt, port)
        assert rt._sampler.has_candidate
    if phase != "meter_change":
        repo.update_blocks("meter_changed", blocked=True)
        rt.operations.state = rt.operations.state.model_copy(
            update={"persisted_blocks": ("meter_changed",)}
        )
    before = repo.load_installation()
    error = sqlite3.DatabaseError("synthetic block-persistence corruption")
    error.sqlite_errorcode = error_code
    if wrapped:
        error = DatabaseError("synthetic update", {}, error)

    def fail(*args, **kwargs):
        raise error

    with monkeypatch.context() as patch:
        patch.setattr(repo, "update_blocks", fail)
        if phase == "meter_change":
            # Exercise the independent block path, without a health-guard failure.
            with rt._state:
                rt._block("meter_changed")
        else:
            rt.health_guard = StorageHealthGuard(repo, lambda: MIN_FREE_BYTES - 1)
            if phase == "startup":
                with pytest.raises(RuntimeError, match="^storage_fatal$"):
                    rt.restore_intent()
            else:
                clock.advance(10)
                eventually(lambda: rt._fatal_storage)
        if port is not None:
            eventually(lambda: not rt._reader.is_alive() and not rt._window.is_alive())
            assert port.closed
        else:
            assert not factory.ports

    assert rt._fatal_storage and rt.runtime_error == "storage_fatal"
    assert rt.get_status()["runtime_error"] == "storage_fatal"
    assert rt._sampler is None and rt._port is None
    assert rt.operations.state.collection_state == "idle"
    assert rt.operations.state.persisted_blocks == before.lifecycle.persisted_blocks
    assert not rt.operations.unsaved_blocks
    assert repo.load_installation() == before
    assert repo.select_pending_group().batch.records == (committed,)
    # Even after the injected corruption and capacity shortage disappear, the
    # fatal observation cannot be cleared or admit another serial owner.
    rt.health_guard = lambda: None
    rt.storage_guard.free_bytes = lambda: MIN_FREE_BYTES
    for action in (
        rt.resolve_storage_block,
        rt.restore_intent,
        lambda: rt.start_collection(selected),
        rt.create_site,
        rt.preview,
    ):
        with pytest.raises(RuntimeError, match="^storage_fatal$"):
            action()
    assert rt.runtime_error == "storage_fatal"
    assert len(factory.ports) == (0 if port is None else 1)
    assert repo.load_installation() == before
    rt.shutdown()
    assert rt._closed


@pytest.mark.parametrize("error_code", [sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB])
@pytest.mark.parametrize("blocks", [(), ("meter_changed",), ("meter_changed", "storage_full")])
def test_first_resolution_corruption_preserves_fatal_state_and_data(
    runtime, monkeypatch, error_code, blocks
):
    rt, repo, _, factory = runtime
    uploaded, pending = record(repo), record(repo)
    repo.insert_snapshot(uploaded)
    batch = repo.select_pending_group().batch
    repo.apply_confirmation(batch, confirmation(batch), NOW)
    repo.insert_snapshot(pending)
    for block in blocks:
        repo.update_blocks(block, blocked=True)
    rt.operations.state = repo.load_lifecycle()
    before = repo.load_installation()
    with repo.engine.connect() as connection:
        rows = connection.execute(text("SELECT * FROM snapshots ORDER BY record_id")).all()
    rt.storage_guard.free_bytes = lambda: MIN_FREE_BYTES
    error = sqlite3.DatabaseError("synthetic corruption")
    error.sqlite_errorcode = error_code

    def fail(*, resolve_storage=False):
        assert resolve_storage
        raise error

    with monkeypatch.context() as patch:
        patch.setattr(repo, "check_safe_write", fail)
        with pytest.raises(RuntimeError, match="^storage_fatal$"):
            rt.resolve_storage_block()

    assert rt._fatal_storage
    assert rt.runtime_error == "storage_fatal"
    assert rt.get_status()["runtime_error"] == "storage_fatal"
    assert rt.operations.state.collection_state == "idle"
    assert rt.operations.state.persisted_blocks == before.lifecycle.persisted_blocks
    assert not rt.operations.unsaved_blocks
    assert repo.load_installation() == before
    with repo.engine.connect() as connection:
        assert connection.execute(text("SELECT * FROM snapshots ORDER BY record_id")).all() == rows
    assert repo.select_pending_group().batch.records == (pending,)
    assert not factory.ports
    with pytest.raises(RuntimeError, match="^storage_fatal$"):
        rt.resolve_storage_block()
    assert rt.runtime_error == "storage_fatal"


def test_resolution_unrelated_error_is_not_storage_pressure(runtime, monkeypatch):
    rt, repo, _, _ = runtime
    before = repo.load_installation()
    rt.storage_guard.free_bytes = lambda: MIN_FREE_BYTES
    error = ValueError("synthetic validation failure")

    def fail(**kwargs):
        raise error

    with monkeypatch.context() as patch:
        patch.setattr(repo, "check_safe_write", fail)
        with pytest.raises(ValueError) as raised:
            rt.resolve_storage_block()
    assert raised.value is error
    assert rt.runtime_error is None and not rt._fatal_storage
    assert repo.load_installation() == before
    assert not rt.operations.unsaved_blocks


@pytest.mark.parametrize("failure", ["capacity", "write"])
def test_shutdown_storage_pressure_discards_tail_and_closes(runtime, monkeypatch, failure):
    rt, repo, clock, factory = runtime
    selected = session(rt)
    committed = record(repo)
    repo.insert_snapshot(committed)
    port = start(rt, factory)
    clock.advance(1)
    feed(rt, port)
    assert rt._sampler.has_candidate
    # An unresolved independent block must survive storage pressure as well.
    repo.update_blocks("meter_changed", blocked=True)
    if failure == "capacity":
        rt.health_guard = StorageHealthGuard(repo, lambda: MIN_FREE_BYTES - 1)
    else:

        def fail(snapshot):
            raise OSError("synthetic tail-write failure")

        monkeypatch.setattr(repo, "insert_snapshot", fail)

    thread, result = threaded(rt.shutdown)
    eventually(lambda: port.closed)
    assert rt.operations.state.collection_state == "stopping"
    clock.advance(9)
    thread.join(2)
    assert not thread.is_alive()
    assert result == [None]
    assert rt._closed and rt._sampler is None
    assert rt._port is None and port.closed
    assert not rt._reader.is_alive() and not rt._window.is_alive()
    assert rt.runtime_error == "storage_error"
    assert rt.operations.state.collection_state == "blocked"
    assert rt.operations.state.stop_action is None
    persisted = repo.load_lifecycle()
    assert set(persisted.persisted_blocks) == {"meter_changed", "storage_full"}
    assert persisted.session.collection_enabled
    assert persisted.session.descriptor.session_id == selected
    assert persisted.session.state == "open"
    assert repo.select_pending_group().batch.records == (committed,)


@pytest.mark.parametrize("action", ["pause", "switch", "shutdown"])
@pytest.mark.parametrize("error_code", [sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB])
def test_tail_corruption_releases_owners_and_finishes_shutdown(runtime, action, error_code):
    rt, repo, clock, factory = runtime
    selected = session(rt)
    target = rt.create_site()
    port = start(rt, factory)
    clock.advance(1)
    feed(rt, port)
    assert rt._sampler.has_candidate
    error = sqlite3.DatabaseError("synthetic corruption")
    error.sqlite_errorcode = error_code

    def fail():
        raise error

    rt.health_guard = fail

    def command():
        if action == "pause":
            return rt.pause(selected)
        if action == "switch":
            return rt.switch_site(
                DeploymentCommand(
                    site_id=target.site_id, expected_session_id=selected, confirm_switch=True
                )
            )
        return rt.shutdown()

    thread, result = threaded(command)
    eventually(lambda: port.closed)
    assert rt.operations.state.collection_state == "stopping"
    if action == "switch":
        assert rt.operations.state.next_site_id == target.site_id
    clock.advance(9)
    thread.join(2)
    assert not thread.is_alive()
    if action == "shutdown":
        assert result == [None]
    else:
        assert isinstance(result[0], RuntimeError)
        assert str(result[0]) == "storage_fatal"
    assert rt.runtime_error == "storage_fatal"
    assert rt._sampler is None
    assert rt.operations.state.collection_state == "idle"
    assert rt.operations.state.stop_action is None
    assert rt.operations.state.next_site_id is None
    assert not rt._reader.is_alive() and not rt._window.is_alive()
    assert rt._port is None and port.closed
    persisted = repo.load_lifecycle()
    assert not persisted.persisted_blocks
    assert persisted.session.collection_enabled == (action == "shutdown")
    assert persisted.session.descriptor.session_id == selected
    assert persisted.session.state == "open"
    assert repo.get_status().pending_count == 0
    if action != "shutdown":
        with pytest.raises(RuntimeError, match="storage_fatal"):
            rt.resolve_storage_block()
        rt.shutdown()
    assert rt._closed


def test_backup_restore_across_fresh_processes(tmp_path):
    source = tmp_path / "source"
    init = """
import sys
from datetime import UTC, datetime
from pathlib import Path
from watt_profile.logger.bootstrap import installation_lock
from watt_profile.logger.identity import load_or_initialize_installation
from watt_profile.logger.storage.database import create_database, migrate
from watt_profile.logger.storage.repository import LoggerRepository
path = Path(sys.argv[1])
with installation_lock(path):
    engine = create_database(path / "logger.sqlite3")
    migrate(engine)
    load_or_initialize_installation(
        LoggerRepository(engine), path, datetime(2026, 1, 1, tzinfo=UTC)
    )
    engine.dispose()
"""
    created = subprocess.run([sys.executable, "-c", init, str(source)], capture_output=True)
    assert created.returncode == 0
    original_token = read_upload_token(source)
    engine = create_database(source / "logger.sqlite3")
    try:
        original_identity = LoggerRepository(engine).load_installation()
    finally:
        engine.dispose()
    for seed in range(8):
        saved, target = tmp_path / f"backup-{seed}", tmp_path / f"target-{seed}"
        for command, args in (
            ("backup", ["--data-dir", str(source), "--output", str(saved)]),
            ("restore", ["--backup", str(saved), "--data-dir", str(target)]),
        ):
            result = subprocess.run(
                [sys.executable, "-m", "watt_profile.cli.logger_admin", command, *args],
                env=dict(os.environ, PYTHONHASHSEED=str(seed)),
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, result.stderr
        restored_engine = create_database(target / "logger.sqlite3")
        try:
            assert LoggerRepository(restored_engine).load_installation() == original_identity
            assert read_upload_token(target) == original_token
        finally:
            restored_engine.dispose()


@pytest.mark.parametrize("publish_file", ["upload-token", "logger.sqlite3"])
def test_interrupted_restore_rejects_startup_and_resumes(tmp_path, publish_file):
    from uuid import uuid4

    from watt_profile.contracts.lifecycle import new_deployment
    from watt_profile.logger.storage.repository import Site

    source, saved, target = tmp_path / "source", tmp_path / "backup", tmp_path / "target"
    engine, repo = installation(source)
    try:
        site = repo.save_site(Site(site_id=uuid4()))
        repo.commit_deployment_change(
            None, new_deployment(repo.load_lifecycle(), site.site_id, NOW).lifecycle
        )
        committed = record(repo)
        repo.insert_snapshot(committed)
        original_identity = repo.load_installation()
        original_token = read_upload_token(source)
        backup(source, saved)
    finally:
        engine.dispose()
    script = """
import os
import sys
from pathlib import Path
from watt_profile.logger.maintenance import restore
replace = os.replace

def interrupt(source, target):
    replace(source, target)
    if Path(target).name == sys.argv[3]:
        os._exit(91)

os.replace = interrupt
restore(Path(sys.argv[1]), Path(sys.argv[2]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(saved), str(target), publish_file], capture_output=True
    )
    assert result.returncode == 91
    before = {p.name for p in target.iterdir()}
    with pytest.raises(RuntimeError, match="restore_incomplete"):
        with installation_lock(target):
            startup_engine = create_database(target / "logger.sqlite3")
            try:
                migrate(startup_engine)
                load_or_initialize_installation(LoggerRepository(startup_engine), target, NOW)
            finally:
                startup_engine.dispose()
    assert {p.name for p in target.iterdir()} == before
    assert read_upload_token(target) == original_token
    restore(saved, target)
    with installation_lock(target):
        restored_engine = create_database(target / "logger.sqlite3")
        try:
            migrate(restored_engine)
            restored = LoggerRepository(restored_engine)
            assert load_or_initialize_installation(restored, target, NOW) == original_identity
            assert read_upload_token(target) == original_token
            assert restored.select_pending_group().batch.records == (committed,)
        finally:
            restored_engine.dispose()
    assert not (target / ".restore-in-progress").exists()
    assert not list(target.glob(".restore-*"))
