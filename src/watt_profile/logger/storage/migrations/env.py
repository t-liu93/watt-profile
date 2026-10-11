"""Alembic runs only on the caller's already configured, locked connection."""

from alembic import context

from watt_profile.logger.storage.schema import metadata

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=metadata, transactional_ddl=True)
with context.begin_transaction():
    context.run_migrations()
