"""Server storage acceptance against a newly launched real PostgreSQL 17."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import event, func, insert, inspect, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from watt_profile.contracts.domain import (
    Metrics,
    SessionDescriptor,
    Snapshot,
    SourceDescriptor,
    canonical_snapshot_bytes,
    measurement_point_id,
)
from watt_profile.server.storage import schema as db
from watt_profile.server.storage.database import (
    HEAD,
    MIGRATION_KEY,
    MIGRATION_NAMESPACE,
    MigrationError,
    create_database,
    migrate,
)
from watt_profile.server.storage.repository import (
    ServerRepository,
    acquire_mutation_locks,
    snapshot_values,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def synthetic(server_engine):
    repo = ServerRepository(server_engine)
    descriptor = SessionDescriptor(
        session_id=uuid4(), device_id=uuid4(), site_id=uuid4(), started_at=NOW
    )
    source = SourceDescriptor(
        source_id=uuid4(),
        session_id=descriptor.session_id,
        measurement_point_id=measurement_point_id(descriptor.site_id),
    )
    with repo.transaction() as session:
        session.execute(insert(db.devices).values(device_id=descriptor.device_id, created_at=NOW))
        session.execute(insert(db.sites).values(site_id=descriptor.site_id))
        acquire_mutation_locks(
            session,
            device=[descriptor.device_id],
            site=[descriptor.site_id],
            session=[descriptor.session_id],
            source=[source.source_id],
        )
        repo.insert_descriptors(session, descriptor, source)
    data = json.loads((Path(__file__).parent.parent / "examples/ingest/request.json").read_text())[
        "records"
    ][0]
    data.update(
        record_id=str(uuid4()),
        device_id=str(descriptor.device_id),
        site_id=str(descriptor.site_id),
        session_id=str(descriptor.session_id),
        source_id=str(source.source_id),
        measurement_point_id=str(source.measurement_point_id),
    )
    data["meter_time_utc"] = None
    for i, name in enumerate(Metrics.model_fields):
        data["metrics"][name] = None if i % 3 == 0 else "12345678901234567890.123400"
    return repo, descriptor, source, Snapshot.model_validate(data)


def test_migration_fresh_noop_independent_chain(server_engine):
    migrate(server_engine)
    with server_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
        assert set(inspect(connection).get_table_names()) == set(db.metadata.tables) | {
            "alembic_version"
        }
        assert connection.scalar(text("SHOW TimeZone")) == "UTC"
        assert connection.scalar(text("SHOW lock_timeout")) == "5s"
        assert connection.scalar(text("SHOW statement_timeout")) == "30s"
        for name in Metrics.model_fields:
            row = connection.execute(
                text(
                    "SELECT numeric_precision, numeric_scale FROM information_schema.columns "
                    "WHERE table_name='snapshots' AND column_name=:name"
                ),
                {"name": name},
            ).one()
            assert row == (None, None)
        for table in db.metadata.tables.values():
            actual = inspect(connection).get_columns(table.name)
            assert {column["name"] for column in actual} == set(table.c.keys())
            assert {column["name"]: column["nullable"] for column in actual} == {
                column.name: column.nullable for column in table.c
            }
            assert all(item["name"] for item in inspect(connection).get_foreign_keys(table.name))
            assert all(
                item["name"] for item in inspect(connection).get_check_constraints(table.name)
            )
    assert server_engine.pool.size() == 5
    assert server_engine.pool._max_overflow == 5
    assert server_engine.pool.timeout() == 5
    configuration = Config()
    configuration.set_main_option("script_location", str(Path(db.__file__).parent / "migrations"))
    script = ScriptDirectory.from_config(configuration)
    assert script.get_current_head() == HEAD
    assert script.get_revision(HEAD).down_revision == "server_0001"
    assert script.get_revision("server_0001").down_revision is None


def test_unknown_revision_refused_without_reset(server_engine):
    with server_engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num='future_9999'"))
    try:
        with pytest.raises(MigrationError, match="unknown_database_revision"):
            migrate(server_engine)
        with server_engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version")) == "future_9999"
            )
            assert "snapshots" in inspect(connection).get_table_names()
    finally:
        with server_engine.begin() as connection:
            connection.execute(text("UPDATE alembic_version SET version_num=:head"), {"head": HEAD})


def test_concurrent_migration_lock_wait_and_release(server_engine):
    attempted = Event()

    @event.listens_for(server_engine, "before_cursor_execute")
    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if "pg_advisory_xact_lock" in statement:
            attempted.set()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with server_engine.begin() as connection:
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(:namespace, :key)"),
                    {"namespace": MIGRATION_NAMESPACE, "key": MIGRATION_KEY},
                )
                attempted.clear()
                future = executor.submit(migrate, server_engine)
                assert attempted.wait(2)
                assert not future.done()
                with server_engine.connect() as observer:
                    assert (
                        observer.scalar(
                            text(
                                "SELECT count(*) FROM pg_locks "
                                "WHERE locktype='advisory' AND NOT granted"
                            )
                        )
                        == 1
                    )
            future.result(timeout=5)
    finally:
        event.remove(server_engine, "before_cursor_execute", observe)
    migrate(server_engine)


def test_roundtrip_all_fields_decimal_scale_null_hash_and_duplicate(synthetic):
    repo, descriptor, source, snapshot = synthetic
    with repo.transaction() as session:
        assert repo.insert_snapshot(session, snapshot, NOW)
        assert not repo.insert_snapshot(session, snapshot, NOW)
        assert repo.load_descriptors(session, descriptor.session_id) == (descriptor, source)
        row = session.execute(select(db.snapshots)).mappings().one()
        assert row == snapshot_values(snapshot, NOW)
        assert isinstance(row["power_import_kw"], Decimal)
        assert row["fields"] == snapshot.model_dump(mode="json")["fields"]
    loaded = repo.get_snapshot(snapshot.device_id, snapshot.record_id)
    assert loaded.snapshot == snapshot
    assert loaded.canonical == canonical_snapshot_bytes(snapshot)
    assert b"12345678901234567890.123400" in loaded.canonical
    assert (
        loaded.snapshot.metrics.power_import_kw.as_tuple()
        == snapshot.metrics.power_import_kw.as_tuple()
    )
    assert loaded.ingested_at == NOW
    assert repo.get_snapshot(snapshot.device_id, uuid4()) is None


@pytest.mark.parametrize(
    "column,value",
    [
        ("power_import_kw", Decimal("-1")),
        ("power_import_kw", Decimal("NaN")),
        ("power_import_kw", Decimal("Infinity")),
        ("power_import_kw", Decimal("-Infinity")),
        ("schema_version", 2),
        ("payload_sha256", "G" * 64),
        ("payload_sha256", "a" * 63),
        ("meter_time_raw", "123456789012X"),
        ("fields", {}),
        ("quality", []),
        ("canonical_snapshot", "[]"),
        ("canonical_snapshot", '{"x":"' + "é" * 9000 + '"}'),
        ("record_id", UUID("12345678-1234-1234-8234-123456789abc")),
        ("record_id", UUID("12345678-1234-4234-7234-123456789abc")),
        ("received_at", None),
    ],
    ids=lambda value: str(value)[:50],
)
def test_direct_sql_snapshot_constraints(synthetic, column, value):
    repo, _, _, snapshot = synthetic
    values = snapshot_values(snapshot, NOW)
    values[column] = value
    with pytest.raises(DBAPIError), repo.transaction() as session:
        session.execute(insert(db.snapshots).values(**values))
    assert repo.get_snapshot(snapshot.device_id, snapshot.record_id) is None


@pytest.mark.parametrize(
    "column,value",
    [
        ("power_import_kw", Decimal("42")),
        ("fields", []),
        ("quality", {"integrity": "valid", "frame_length": 1, "issues": []}),
        ("payload_sha256", "0" * 64),
    ],
)
def test_load_rejects_projection_or_hash_corruption(synthetic, column, value):
    repo, _, _, snapshot = synthetic
    with repo.transaction() as session:
        repo.insert_snapshot(session, snapshot, NOW)
        session.execute(update(db.snapshots).values({column: value}))
    with pytest.raises(ValueError, match="snapshot_projection_mismatch"):
        repo.get_snapshot(snapshot.device_id, snapshot.record_id)


def test_load_rejects_noncanonical_text(synthetic):
    repo, _, _, snapshot = synthetic
    with repo.transaction() as session:
        repo.insert_snapshot(session, snapshot, NOW)
        session.execute(
            update(db.snapshots).values(
                canonical_snapshot=json.dumps(snapshot.model_dump(mode="json"))
            )
        )
    with pytest.raises(ValueError, match="snapshot_projection_mismatch"):
        repo.get_snapshot(snapshot.device_id, snapshot.record_id)


def test_rollback_and_immutable_conflicts(synthetic):
    repo, descriptor, source, snapshot = synthetic
    with pytest.raises(ValueError, match="record_conflict"), repo.transaction() as session:
        repo.insert_snapshot(session, snapshot, NOW)
        changed = Snapshot.model_validate(
            {**snapshot.model_dump(), "meter_time_raw": "000000000000S"}
        )
        repo.insert_snapshot(session, changed, NOW)
    assert repo.get_snapshot(snapshot.device_id, snapshot.record_id) is None
    with pytest.raises(ValueError, match="descriptor_conflict"), repo.transaction() as session:
        repo.insert_descriptors(
            session,
            descriptor.model_copy(update={"started_at": datetime(2027, 1, 1, tzinfo=UTC)}),
            source,
        )
    with repo.transaction() as session:
        assert repo.load_descriptors(session, descriptor.session_id) == (descriptor, source)


def test_composite_ownership_and_no_cascade(synthetic):
    repo, descriptor, source, snapshot = synthetic
    other_site, other_device = uuid4(), uuid4()
    other_point = measurement_point_id(other_site)
    with repo.transaction() as session:
        session.execute(insert(db.sites).values(site_id=other_site))
        session.execute(insert(db.devices).values(device_id=other_device, created_at=NOW))
        session.execute(
            insert(db.points).values(measurement_point_id=other_point, site_id=other_site)
        )
    for changes in (
        {"device_id": other_device},
        {"site_id": other_site},
        {"measurement_point_id": other_point},
    ):
        with pytest.raises(IntegrityError), repo.transaction() as session:
            session.execute(
                insert(db.snapshots).values(**{**snapshot_values(snapshot, NOW), **changes})
            )
    with pytest.raises(IntegrityError), repo.transaction() as session:
        session.execute(
            insert(db.sources).values(
                **{
                    **source.model_dump(),
                    "source_id": uuid4(),
                    "session_id": uuid4(),
                    "site_id": other_site,
                }
            )
        )
    with repo.transaction() as session:
        repo.insert_snapshot(session, snapshot, NOW)
    with pytest.raises(IntegrityError), repo.transaction() as session:
        session.execute(db.devices.delete().where(db.devices.c.device_id == descriptor.device_id))
    assert repo.get_snapshot(snapshot.device_id, snapshot.record_id)


def test_model_revalidation_on_insert(synthetic):
    repo, _, _, snapshot = synthetic
    invalid = snapshot.model_copy(
        update={"metrics": snapshot.metrics.model_copy(update={"power_import_kw": Decimal("NaN")})}
    )
    with pytest.raises(ValueError), repo.transaction() as session:
        repo.insert_snapshot(session, invalid, NOW)
    with repo.transaction() as session:
        assert session.scalar(select(func.count()).select_from(db.snapshots)) == 0


def test_migrate_cli_and_bootstrap(postgres_server, tmp_path):
    password = tmp_path / "password"
    password.write_text("synthetic-admin-password\n")
    password.chmod(0o600)
    environment = {
        **os.environ,
        "WATT_SERVER_DATABASE_URL_FILE": str(postgres_server.database_url_file),
        "WATT_SERVER_ADMIN_USER": "admin",
        "WATT_SERVER_ADMIN_PASSWORD_FILE": str(password),
        "WATT_SERVER_PUBLIC_ORIGIN": "https://example.test",
    }
    result = subprocess.run(
        [sys.executable, "-m", "watt_profile.cli.server_admin", "migrate"],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert not result.stdout and not result.stderr
    engine = create_database(postgres_server.database_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
    finally:
        engine.dispose()


def test_source_composite_ownership_existing_parents(synthetic):
    repo, descriptor, source, _ = synthetic
    other_site = uuid4()
    other_point = measurement_point_id(other_site)
    other_session = SessionDescriptor(
        session_id=uuid4(), device_id=descriptor.device_id, site_id=other_site, started_at=NOW
    )
    with repo.transaction() as session:
        session.execute(insert(db.sites).values(site_id=other_site))
        session.execute(
            insert(db.points).values(measurement_point_id=other_point, site_id=other_site)
        )
        session.execute(insert(db.sessions).values(**other_session.model_dump()))
    for values in (
        {
            **source.model_dump(),
            "source_id": uuid4(),
            "session_id": other_session.session_id,
            "site_id": other_site,
        },
        {
            **source.model_dump(),
            "source_id": uuid4(),
            "session_id": other_session.session_id,
            "measurement_point_id": other_point,
            "site_id": descriptor.site_id,
        },
    ):
        with pytest.raises(IntegrityError), repo.transaction() as session:
            session.execute(insert(db.sources).values(**values))


def test_credentials_constraints_and_grant_removal_preserves_history(synthetic):
    repo, descriptor, _, snapshot = synthetic
    credential_id = uuid4()
    with repo.transaction() as session:
        session.execute(
            insert(db.credentials).values(
                credential_id=credential_id,
                device_id=descriptor.device_id,
                token_sha256="a" * 64,
                created_at=NOW,
            )
        )
        session.execute(
            insert(db.authorizations).values(
                device_id=descriptor.device_id, site_id=descriptor.site_id
            )
        )
        repo.insert_snapshot(session, snapshot, NOW)
    for values in (
        {"credential_id": uuid4()},
        {"credential_id": uuid4(), "token_sha256": "x" * 64},
    ):
        with pytest.raises(IntegrityError), repo.transaction() as session:
            session.execute(
                insert(db.credentials).values(
                    **{
                        "device_id": descriptor.device_id,
                        "token_sha256": "a" * 64,
                        "created_at": NOW,
                        **values,
                    }
                )
            )
    with repo.transaction() as session:
        session.execute(update(db.credentials).values(disabled_at=NOW))
        session.execute(db.authorizations.delete())
    assert repo.get_snapshot(snapshot.device_id, snapshot.record_id)


def test_transaction_local_settings_reset_and_failure_rollback(server_engine):
    with server_engine.begin() as connection:
        connection.exec_driver_sql("SET LOCAL statement_timeout = '123ms'")
        connection.exec_driver_sql("SET LOCAL TIME ZONE 'Europe/Amsterdam'")
    with server_engine.begin() as connection:
        assert connection.scalar(text("SHOW statement_timeout")) == "30s"
        assert connection.scalar(text("SHOW TimeZone")) == "UTC"
    repo = ServerRepository(server_engine)
    with pytest.raises(RuntimeError), repo.transaction() as session:
        session.execute(insert(db.sites).values(site_id=uuid4()))
        raise RuntimeError("synthetic_abort")
    assert repo.list_sites() == ()


def test_migration_failure_rolls_back_without_reset(server_engine, monkeypatch):
    from watt_profile.server.storage import database

    def fail(configuration, _head):
        connection = configuration.attributes["connection"]
        connection.execute(text("CREATE TABLE synthetic_partial (id integer)"))
        raise RuntimeError("synthetic_migration_failure")

    monkeypatch.setattr(database.command, "upgrade", fail)
    with pytest.raises(MigrationError, match="database_migration_failed"):
        migrate(server_engine)
    with server_engine.connect() as connection:
        assert "synthetic_partial" not in inspect(connection).get_table_names()
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD


def test_original_revision_upgrade_equivalence_and_record_preservation(server_engine):
    from alembic import command

    def structure(connection):
        inspector = inspect(connection)
        return {
            name: {
                "columns": sorted(
                    (column["name"], str(column["type"]), column["nullable"], column["default"])
                    for column in inspector.get_columns(name)
                ),
                "indices": sorted(
                    (index["name"], tuple(index["column_names"]), index["unique"])
                    for index in inspector.get_indexes(name)
                ),
                "checks": sorted(
                    (item["name"], item["sqltext"])
                    for item in inspector.get_check_constraints(name)
                ),
                "foreign_keys": sorted(
                    (
                        item["name"],
                        tuple(item["constrained_columns"]),
                        item["referred_table"],
                        tuple(item["referred_columns"]),
                    )
                    for item in inspector.get_foreign_keys(name)
                ),
            }
            for name in db.metadata.tables
        }

    with server_engine.begin() as connection:
        fresh = structure(connection)
        db.metadata.drop_all(connection)
        connection.execute(text("DROP TABLE alembic_version"))
        configuration = Config()
        configuration.set_main_option(
            "script_location", str(Path(db.__file__).parent / "migrations")
        )
        configuration.attributes["connection"] = connection
        command.upgrade(configuration, "server_0001")
        for table in ("sites", "devices"):
            assert "display_name" not in {
                item["name"] for item in inspect(connection).get_columns(table)
            }

    descriptor = SessionDescriptor(
        session_id=uuid4(), device_id=uuid4(), site_id=uuid4(), started_at=NOW
    )
    source = SourceDescriptor(
        source_id=uuid4(),
        session_id=descriptor.session_id,
        measurement_point_id=measurement_point_id(descriptor.site_id),
    )
    snapshot = Snapshot(
        device_id=descriptor.device_id,
        record_id=uuid4(),
        site_id=descriptor.site_id,
        session_id=descriptor.session_id,
        source_id=source.source_id,
        measurement_point_id=source.measurement_point_id,
        received_at=NOW,
        sampled_at=NOW,
        meter_time_raw=None,
        meter_time_utc=None,
        metrics=Metrics(**{name: None for name in Metrics.model_fields}),
        fields=[],
        quality={"frame_length": 1, "issues": []},
    )
    repo = ServerRepository(server_engine)
    credential_id = uuid4()
    with repo.transaction() as session:
        session.execute(insert(db.devices).values(device_id=descriptor.device_id, created_at=NOW))
        session.execute(
            insert(db.sites).values(site_id=descriptor.site_id, address="Original address")
        )
        repo.insert_descriptors(session, descriptor, source)
        repo.insert_snapshot(session, snapshot, NOW)
        session.execute(
            insert(db.credentials).values(
                credential_id=credential_id,
                device_id=descriptor.device_id,
                token_sha256="a" * 64,
                created_at=NOW,
            )
        )
        before = dict(session.execute(select(db.snapshots)).mappings().one())
        credential_before = dict(session.execute(select(db.credentials)).mappings().one())
    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(lambda _: migrate(server_engine), range(3)))
    migrate(server_engine)
    with server_engine.connect() as connection:
        assert structure(connection) == fresh
        assert dict(connection.execute(select(db.snapshots)).mappings().one()) == before
        assert (
            dict(connection.execute(select(db.credentials)).mappings().one()) == credential_before
        )
        assert connection.scalar(select(db.sites.c.display_name)) == ""
        assert connection.scalar(select(db.devices.c.display_name)) == ""
    assert repo.get_snapshot(
        snapshot.device_id, snapshot.record_id
    ).canonical == canonical_snapshot_bytes(snapshot)
