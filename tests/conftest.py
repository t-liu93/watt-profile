"""Explicit opt-in isolated real PostgreSQL fixture for Server database tests."""

import subprocess

import pytest
from postgres_test_server import start_postgres_test_server, stop_postgres_test_server

from watt_profile.server.storage.database import create_database, migrate
from watt_profile.server.storage.schema import metadata


@pytest.fixture(scope="session")
def postgres_server():
    server = start_postgres_test_server()
    try:
        yield server
    finally:
        stop_postgres_test_server(server)
        assert not server.directory.exists()
        for arguments in (
            ["docker", "inspect", server.container_name],
            ["docker", "volume", "inspect", server.volume_name],
        ):
            removed = subprocess.run(arguments, capture_output=True, text=True, timeout=10)
            assert removed.returncode != 0


@pytest.fixture
def server_engine(postgres_server):
    engine = create_database(postgres_server.database_url)
    try:
        migrate(engine)
        try:
            yield engine
        finally:
            with engine.begin() as connection:
                for table in reversed(metadata.sorted_tables):
                    connection.execute(table.delete())
    finally:
        engine.dispose()
