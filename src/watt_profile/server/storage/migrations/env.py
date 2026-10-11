"""Migrations use the caller's locked PostgreSQL transaction."""

from alembic import context

from watt_profile.server.storage.schema import metadata

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=metadata)
with context.begin_transaction():
    context.run_migrations()
