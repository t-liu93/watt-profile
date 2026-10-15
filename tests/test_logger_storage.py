"""Offline Logger persistence acceptance using isolated databases and synthetic owners."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from watt_profile.contracts.domain import Snapshot, canonical_snapshot_bytes
from watt_profile.contracts.lifecycle import complete_session, new_deployment, switch_site
from watt_profile.contracts.upload import (
    InvalidConfirmation,
    UploadConfirmation,
    validate_upload_body,
)
from watt_profile.logger.bootstrap import installation_lock
from watt_profile.logger.storage import schema as db
from watt_profile.logger.storage.database import create_database, migrate, origin_identity
from watt_profile.logger.storage.repository import LocalSettings, LoggerRepository, Site

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def repository(tmp_path):
    engine = create_database(tmp_path / "test.sqlite3")
    migrate(engine)
    repo = LoggerRepository(engine)
    repo.initialize_installation(uuid4(), NOW)
    site = repo.save_site(Site(site_id=uuid4()))
    repo.commit_deployment_change(
        None, new_deployment(repo.load_lifecycle(), site.site_id, NOW).lifecycle
    )
    yield repo
    engine.dispose()


def record(repo, **changes):
    state = repo.load_lifecycle()
    data = json.loads((Path(__file__).parent.parent / "examples/ingest/request.json").read_bytes())[
        "records"
    ][0]
    descriptor, source = state.session.descriptor, state.source.descriptor
    data.update(
        record_id=str(uuid4()),
        device_id=str(state.device_id),
        site_id=str(descriptor.site_id),
        session_id=str(descriptor.session_id),
        source_id=str(source.source_id),
        measurement_point_id=str(source.measurement_point_id),
        **changes,
    )
    data["metrics"]["power_import_kw"] = "1.2300"
    data["metrics"]["power_export_kw"] = "0"
    data["meter_time_utc"] = None
    return Snapshot.model_validate(data)


def confirmation(batch):
    return UploadConfirmation(
        schema_version=1,
        device_id=batch.device_id,
        confirmed_record_ids=tuple(r.record_id for r in batch.records),
    )


def test_migrations_pragmas_and_logger_only_schema(tmp_path):
    engine = create_database(tmp_path / "new.sqlite3")
    try:
        migrate(engine)
        migrate(engine)
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version")) == "logger_0001"
            )
            for name, expected in [
                ("foreign_keys", 1),
                ("journal_mode", "wal"),
                ("synchronous", 2),
                ("busy_timeout", 5000),
            ]:
                assert connection.scalar(text("PRAGMA " + name)) == expected
            tables = set(
                connection.scalars(text("SELECT name FROM sqlite_master WHERE type='table'"))
            )
            assert tables == set(db.metadata.tables) | {"alembic_version"}
        assert LoggerRepository(engine).load_installation() is None
    finally:
        engine.dispose()


def test_canonical_roundtrip_restart_conflict_and_concurrent_duplicate(repository):
    repo = repository
    snapshot = record(repo)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(repo.insert_snapshot, [snapshot, snapshot])) == [False, True]
    selected = repo.select_pending_group()
    assert selected.canonical_records == (canonical_snapshot_bytes(snapshot),)
    assert validate_upload_body(selected.body) == selected.batch
    assert canonical_snapshot_bytes(selected.batch.records[0]) == canonical_snapshot_bytes(snapshot)
    assert selected.batch.records[0].metrics.power_import_kw.as_tuple().exponent == -4
    assert selected.batch.records[0].meter_time_utc is None
    changed = snapshot.model_dump(mode="json")
    changed["sampled_at"] = "2026-01-01T00:00:01.000000Z"
    with pytest.raises(ValueError, match="record_conflict"):
        repo.insert_snapshot(Snapshot.model_validate(changed))
    reopened = create_database(Path(repo.engine.url.database))
    try:
        assert LoggerRepository(reopened).select_pending_group() == selected
    finally:
        reopened.dispose()
    status = repo.get_status()
    assert status.pending_count == 1
    assert status.pending_bytes == len(canonical_snapshot_bytes(snapshot))


def test_pending_group_exclusion_preserves_oldest_group_bytes_and_limits(repository):
    repo = repository
    old_data = record(repo).model_dump(mode="json")
    old_data["record_id"] = "00000000-0000-4000-8000-000000000001"
    old = Snapshot.model_validate(old_data)
    repo.insert_snapshot(old)
    old_key = (
        old.device_id,
        old.site_id,
        old.session_id,
        old.source_id,
        old.measurement_point_id,
    )
    state = repo.load_lifecycle()
    closed = complete_session(state, state.session.descriptor.session_id, NOW).lifecycle
    old_session_id = state.session.descriptor.session_id
    repo.commit_deployment_change(old_session_id, closed)
    site = repo.save_site(Site(site_id=uuid4()))
    repo.commit_deployment_change(
        old_session_id,
        new_deployment(repo.load_lifecycle(), site.site_id, NOW + timedelta(seconds=1)).lifecycle,
    )
    new_data = record(repo).model_dump(mode="json")
    new_data["record_id"] = "00000000-0000-4000-8000-000000000002"
    new = Snapshot.model_validate(new_data)
    repo.insert_snapshot(new)
    selected = repo.select_pending_group(excluded_groups=(old_key,), limit=1)
    assert selected.batch.records == (new,)
    assert selected.canonical_records == (canonical_snapshot_bytes(new),)
    assert len(selected.body) <= 4_194_304
    assert repo.select_pending_group(limit=1).batch.records == (old,)


def test_pending_group_same_reception_tie_and_single_record_byte_bound(repository):
    repo = repository
    common = record(repo).model_dump(mode="json")
    common["received_at"] = "2026-01-01T00:00:00.000000Z"
    earlier, later = common.copy(), common.copy()
    earlier["record_id"] = "00000000-0000-4000-8000-000000000001"
    later["record_id"] = "00000000-0000-4000-8000-000000000002"
    first, second = Snapshot.model_validate(earlier), Snapshot.model_validate(later)
    repo.insert_snapshot(second)
    repo.insert_snapshot(first)
    selected = repo.select_pending_group(limit=1)
    assert selected.batch.records == (first,)
    with pytest.raises(ValueError, match="^batch_too_large$"):
        repo.select_pending_group(limit=1, byte_limit=len(selected.body) - 1)


def test_origin_change_gate_is_atomic_and_uses_effective_origin(repository):
    repo = repository
    repo.insert_snapshot(record(repo))
    first = LocalSettings(server_url="https://Example.test")
    repo.save_settings(first)
    equivalent = LocalSettings(server_url="https://example.test:443")
    repo.save_settings(equivalent)
    attempted = LocalSettings(server_url="http://other.test", serial_path="changed-port")
    with pytest.raises(ValueError, match="^pending_uploads_prevent_server_change$"):
        repo.save_settings(attempted)
    assert repo.load_installation().settings == equivalent
    with pytest.raises(ValueError, match="^pending_uploads_prevent_server_change$"):
        repo.save_settings(LocalSettings())
    assert repo.load_installation().settings == equivalent

    group = repo.select_pending_group()
    repo.apply_confirmation(group.batch, confirmation(group.batch), NOW)
    repo.save_settings(attempted)
    assert repo.load_installation().settings == attempted


def test_origin_gate_preserves_httpx_ascii_host_compatibility(repository):
    repo = repository
    first = LocalSettings(server_url="http://watt_server:8080")
    repo.save_settings(first)
    assert httpx.URL(first.server_url).raw_host == b"watt_server"

    # Same-origin edits, changes, and clearing remain valid with no backlog.
    repo.save_settings(first)
    changed = LocalSettings(server_url="http://other_server:8080")
    repo.save_settings(changed)
    repo.save_settings(LocalSettings())

    # A non-null ASCII origin still obeys the pending-record protection.
    repo.save_settings(first)
    repo.insert_snapshot(record(repo))
    with pytest.raises(ValueError, match="^pending_uploads_prevent_server_change$"):
        repo.save_settings(changed)
    assert repo.load_installation().settings == first


def test_origin_gate_matches_transport_idna_with_pending_records(repository):
    repo = repository
    assert (
        origin_identity("https://faß.de")[1].encode("ascii") == httpx.URL("https://faß.de").raw_host
    )
    assert (
        origin_identity("https://fass.de")[1].encode("ascii")
        == httpx.URL("https://fass.de").raw_host
    )
    assert origin_identity("https://faß.de") != origin_identity("https://fass.de")
    repo.insert_snapshot(record(repo))
    unicode_origin = LocalSettings(server_url="https://faß.de")
    repo.save_settings(unicode_origin)

    # These spellings reach the same IDNA 2008 destination, so editing between
    # them does not change the effective origin.
    punycode_origin = LocalSettings(server_url="https://xn--fa-hia.de")
    repo.save_settings(punycode_origin)
    assert repo.load_installation().settings == punycode_origin

    # Python's built-in IDNA 2003 codec conflates faß.de with fass.de. HTTPX
    # sends them to distinct hosts, so pending records must block this change.
    with pytest.raises(ValueError, match="^pending_uploads_prevent_server_change$"):
        repo.save_settings(LocalSettings(server_url="https://fass.de"))
    assert repo.load_installation().settings == punycode_origin


@pytest.mark.parametrize("host", ["☃.net", "💩.net", "a\u200d.net", "�.net"])
@pytest.mark.parametrize("has_pending", [False, True])
def test_unencodable_accepted_origins_remain_comparable(repository, host, has_pending):
    repo = repository
    saved = LocalSettings(server_url=f"https://{host}")
    repo.save_settings(saved)
    if has_pending:
        repo.insert_snapshot(record(repo))

    # Same literal host always permits ordinary settings edits.
    repo.save_settings(saved.model_copy(update={"serial_path": "edited-port"}))
    other = LocalSettings(server_url="https://☀.net")
    if has_pending:
        with pytest.raises(ValueError, match="^pending_uploads_prevent_server_change$"):
            repo.save_settings(other)
        with pytest.raises(ValueError, match="^pending_uploads_prevent_server_change$"):
            repo.save_settings(LocalSettings())
    else:
        repo.save_settings(other)
        repo.save_settings(LocalSettings())


def test_unencodable_origin_identity_cannot_collide_with_normalized_host():
    assert origin_identity("https://☃.net") != origin_identity("https://xn--n3h.net")


def test_pending_insert_racing_origin_change_is_serialized(repository):
    repo = repository
    repo.save_settings(LocalSettings(server_url="https://old.test"))
    snapshot = record(repo)
    barrier = threading.Barrier(2)

    def insert_pending():
        barrier.wait()
        return repo.insert_snapshot(snapshot)

    def change_origin():
        barrier.wait()
        try:
            repo.save_settings(LocalSettings(server_url="https://new.test"))
        except ValueError as exc:
            return exc.args[0]
        return "changed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        inserted = pool.submit(insert_pending)
        changed = pool.submit(change_origin)
        assert inserted.result()
        outcome = changed.result()
    saved = repo.load_installation().settings
    assert saved.server_url == ("https://new.test" if outcome == "changed" else "https://old.test")
    assert outcome in {"changed", "pending_uploads_prevent_server_change"}
    assert repo.get_status().pending_count == 1


@pytest.mark.parametrize(
    "column,value",
    [
        ("device_id", "00000000-0000-1000-8000-000000000000"),
        ("created_at", "2026-02-30T00:00:00.000000Z"),
        ("created_at", "2026-01-01T00:00:00Z"),
        ("device_id", "G" * 36),
    ],
)
def test_direct_device_constraints(repository, column, value):
    values = {"device_id": str(uuid4()), "created_at": "2026-01-01T00:00:00.000000Z"}
    values[column] = value
    with pytest.raises(IntegrityError), repository.engine.begin() as connection:
        connection.execute(insert(db.devices).values(**values))


@pytest.mark.parametrize(
    "column,value",
    [
        ("payload_sha256", "g" * 64),
        ("power_import_kw", "-1"),
        ("power_import_kw", "1e2"),
        ("power_import_kw", "NaN"),
        ("received_at", "2026-13-01T00:00:00.000000Z"),
        ("fields", "{}"),
        ("quality", "[]"),
        ("canonical_snapshot", "[]"),
        ("meter_time_raw", "unsafe"),
        ("schema_version", 2),
        ("upload_state", "uploaded"),
        ("canonical_snapshot", '{"x":"' + "x" * 16384 + '"}'),
    ],
)
def test_direct_snapshot_constraints(repository, column, value):
    repository.insert_snapshot(record(repository))
    with pytest.raises(IntegrityError), repository.engine.begin() as connection:
        connection.execute(update(db.snapshots).values(**{column: value}))


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("column", ["fields", "quality", "canonical_snapshot"])
def test_direct_snapshot_json_rejects_nonstandard_constants(repository, column, constant):
    repository.insert_snapshot(record(repository))
    value = "[" + constant + "]" if column == "fields" else '{"x":' + constant + "}"
    with repository.engine.connect() as connection:
        before = connection.execute(select(db.snapshots)).mappings().one()
    with pytest.raises(IntegrityError), repository.engine.begin() as connection:
        connection.execute(update(db.snapshots).values(**{column: value}))
    with repository.engine.connect() as connection:
        assert connection.execute(select(db.snapshots)).mappings().one() == before


@pytest.mark.parametrize(
    "column,value",
    [
        ("persisted_blocks", '["storage_full","storage_full"]'),
        ("persisted_blocks", '["other"]'),
        ("persisted_blocks", "{}"),
        ("stopbits", "NaN"),
        ("stopbits", "3"),
        ("timeout", "0"),
        ("timeout", "1e1"),
        ("server_url", "https://example.test/path"),
        ("serial_path", ""),
        ("bytesize", 9),
        ("profile_revision", 2),
        ("singleton_id", 2),
    ],
)
def test_direct_configuration_constraints(repository, column, value):
    with pytest.raises(IntegrityError), repository.engine.begin() as connection:
        connection.execute(update(db.config).values(**{column: value}))


def test_composite_ownership_and_insert_rollback(repository):
    repo = repository
    old = record(repo)
    site = repo.save_site(Site(site_id=uuid4()))
    state = repo.load_lifecycle()
    repo.commit_deployment_change(
        state.session.descriptor.session_id, switch_site(state, site.site_id, NOW).lifecycle
    )
    repo.insert_snapshot(old)
    with pytest.raises(IntegrityError), repo.engine.begin() as connection:
        connection.execute(update(db.snapshots).values(site_id=str(site.site_id)))
    invalid = old.model_dump(mode="json")
    invalid["source_id"] = str(repo.load_lifecycle().source.descriptor.source_id)
    invalid["record_id"] = str(uuid4())
    with pytest.raises(ValueError, match="descriptor_mismatch"):
        repo.insert_snapshot(Snapshot.model_validate(invalid))
    assert repo.get_status().pending_count == 1


def test_projection_corruption_refused_on_load(repository):
    repository.insert_snapshot(record(repository))
    with repository.engine.begin() as connection:
        connection.execute(update(db.snapshots).values(power_import_kw="1.23"))
    with pytest.raises(ValueError, match="snapshot_projection_mismatch"):
        repository.select_pending_group()


def test_confirmation_whole_batch_validation_rollback_and_replay(repository):
    repo = repository
    for _ in range(2):
        repo.insert_snapshot(record(repo))
    group = repo.select_pending_group()
    good = confirmation(group.batch)
    bad = good.model_dump(mode="json")
    bad["confirmed_record_ids"].pop()
    with pytest.raises(InvalidConfirmation):
        repo.apply_confirmation(group.batch, json.dumps(bad).encode(), NOW)
    assert repo.get_status().pending_count == 2
    changed = group.batch.model_dump(mode="json")
    changed["records"][1]["sampled_at"] = "2026-01-01T00:00:01.000000Z"
    from watt_profile.contracts.upload import UploadBatch

    with pytest.raises(InvalidConfirmation):
        repo.apply_confirmation(UploadBatch.model_validate(changed), good, NOW)
    assert repo.get_status().pending_count == 2
    assert repo.get_status().last_successful_upload_at is None
    assert repo.apply_confirmation(group.batch, good, NOW) == 2
    assert repo.apply_confirmation(group.batch, good, NOW + timedelta(days=1)) == 0
    assert repo.get_status().last_successful_upload_at == NOW


def test_homogeneous_bounded_selection_and_cleanup(repository):
    repo = repository
    for _ in range(3):
        repo.insert_snapshot(record(repo))
    old_state = repo.load_lifecycle()
    old_id = old_state.session.descriptor.session_id
    group = repo.select_pending_group(limit=2)
    repo.apply_confirmation(group.batch, confirmation(group.batch), NOW)
    assert repo.cleanup_completed((old_id,)) == 0
    closed = complete_session(old_state, old_id, NOW).lifecycle
    repo.commit_deployment_change(old_id, closed)
    assert repo.cleanup_completed((old_id,)) == 2
    assert repo.get_status().pending_count == 1
    assert len(repo.list_sessions()) == 1
    assert repo.get_status().last_successful_upload_at == NOW
    site = repo.save_site(Site(site_id=uuid4()))
    repo.commit_deployment_change(
        old_id, new_deployment(repo.load_lifecycle(), site.site_id, NOW).lifecycle
    )
    repo.insert_snapshot(record(repo, received_at="2026-12-01T00:00:00.000000Z"))
    assert repo.select_pending_group().batch.session.session_id == old_id
    with pytest.raises(ValueError, match="batch_too_large"):
        repo.select_pending_group(byte_limit=1)
    with pytest.raises(ValueError, match="invalid_batch_limit"):
        repo.select_pending_group(limit=201)


def test_atomic_stale_deployment_failed_switch_and_independent_blocks(repository):
    repo = repository
    before = repo.load_lifecycle()
    site = repo.save_site(Site(site_id=uuid4()))
    proposed = switch_site(before, site.site_id, NOW).lifecycle
    with pytest.raises(ValueError, match="stale_session"):
        repo.commit_deployment_change(uuid4(), proposed)
    assert repo.load_lifecycle() == before
    # A missing site fails after closing the old session; the transaction rolls it back.
    missing = switch_site(before, uuid4(), NOW).lifecycle
    with pytest.raises(IntegrityError):
        repo.commit_deployment_change(before.session.descriptor.session_id, missing)
    assert repo.load_lifecycle() == before
    repo.update_blocks("storage_full", blocked=True)
    repo.bind_meter(before.session.descriptor.session_id, "a" * 64)
    assert set(
        repo.bind_meter(before.session.descriptor.session_id, "b" * 64).persisted_blocks
    ) == {"storage_full", "meter_changed"}
    assert repo.update_blocks("storage_full", blocked=False).persisted_blocks == ("meter_changed",)
    repo.update_blocks("storage_full", blocked=True)
    assert repo.update_blocks("meter_changed", blocked=False).persisted_blocks == ("storage_full",)


def test_settings_revalidation_and_metadata_selection(repository):
    repo = repository
    site = repo.save_site(Site(site_id=uuid4(), address="synthetic"))
    assert repo.save_site(Site(site_id=site.site_id, address="different")) == site
    assert (
        repo.save_site(Site(site_id=site.site_id, address="edited"), edit=True).address == "edited"
    )
    saved = repo.save_settings(LocalSettings(server_url="https://example.test", timeout="1.2500"))
    assert repo.load_installation().settings == saved
    with pytest.raises(ValueError):
        repo.save_settings(LocalSettings(server_url="https://user@example.test"))


def test_migrate_cli_lock_and_no_installation(tmp_path):
    data_dir = tmp_path / "data"
    command = [
        sys.executable,
        "-m",
        "watt_profile.cli.logger_admin",
        "migrate",
        "--data-dir",
        str(data_dir),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (data_dir / "logger.sqlite3").stat().st_mode & 0o777 == 0o600
    with installation_lock(data_dir):
        result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 1 and result.stderr.strip() == "instance_busy"
    engine = create_database(data_dir / "logger.sqlite3")
    try:
        assert LoggerRepository(engine).load_installation() is None
        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(db.devices)) == 0
    finally:
        engine.dispose()


def test_failed_initial_migration_rolls_back_schema(tmp_path):
    engine = create_database(tmp_path / "failed.sqlite3")

    def fail_snapshot_creation(connection, cursor, statement, parameters, context, executemany):
        if "CREATE TABLE snapshots" in statement:
            raise RuntimeError("synthetic_migration_failure")

    event.listen(engine, "before_cursor_execute", fail_snapshot_creation)
    try:
        with pytest.raises(RuntimeError, match="synthetic_migration_failure"):
            migrate(engine)
        event.remove(engine, "before_cursor_execute", fail_snapshot_creation)
        with engine.connect() as connection:
            assert (
                list(connection.scalars(text("SELECT name FROM sqlite_master WHERE type='table'")))
                == []
            )
        migrate(engine)
    finally:
        engine.dispose()


def test_completion_retires_meter_block_preserving_storage_block(repository):
    repo = repository
    state = repo.update_blocks("meter_changed", blocked=True)
    state = repo.update_blocks("storage_full", blocked=True)
    selected = state.session.descriptor.session_id
    closed = complete_session(state, selected, NOW).lifecycle
    result = repo.commit_deployment_change(selected, closed)
    assert result.session.state == "completed"
    assert result.persisted_blocks == ("storage_full",)


def test_new_deployment_preserves_completed_session_history(repository):
    repo = repository
    state = repo.load_lifecycle()
    selected = state.session.descriptor.session_id
    ended_at = NOW + timedelta(hours=1)
    completed = repo.commit_deployment_change(
        selected, complete_session(state, selected, ended_at).lifecycle
    ).session
    assert completed.ended_at == ended_at
    started_at = NOW + timedelta(days=1)
    result = repo.commit_deployment_change(
        selected,
        new_deployment(repo.load_lifecycle(), completed.descriptor.site_id, started_at).lifecycle,
    )
    assert result.session.descriptor.started_at == started_at
    assert result.session.state == "open"
    historical = next(s for s in repo.list_sessions() if s.descriptor.session_id == selected)
    assert historical == completed
