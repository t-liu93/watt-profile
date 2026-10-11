"""Bounded PostgreSQL pool and serialized independent Alembic upgrades."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, inspect, text

HEAD = "server_0001"
MIGRATION_NAMESPACE = 1465078855
MIGRATION_KEY = 1


class MigrationError(RuntimeError):
    """Database migration cannot safely proceed."""


def create_database(url: str) -> Engine:
    if not url.startswith("postgresql+psycopg://"):
        raise ValueError("postgresql_required")
    engine = create_engine(
        url,
        pool_size=5,
        max_overflow=5,
        pool_timeout=5,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"connect_timeout": 5},
    )

    @event.listens_for(engine, "begin")
    def configure(connection):
        connection.exec_driver_sql("SET LOCAL TIME ZONE 'UTC'")
        connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
        connection.exec_driver_sql("SET LOCAL statement_timeout = '30s'")

    return engine


def migrate(engine: Engine) -> None:
    configuration = Config()
    configuration.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    try:
        with engine.begin() as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:namespace, :key)"),
                {"namespace": MIGRATION_NAMESPACE, "key": MIGRATION_KEY},
            )
            if inspect(connection).has_table("alembic_version"):
                revisions = (
                    connection.execute(text("SELECT version_num FROM alembic_version"))
                    .scalars()
                    .all()
                )
                if revisions and revisions != [HEAD]:
                    raise MigrationError("unknown_database_revision")
            configuration.attributes["connection"] = connection
            command.upgrade(configuration, "head")
    except MigrationError:
        raise
    except Exception as exc:
        raise MigrationError("database_migration_failed") from exc
