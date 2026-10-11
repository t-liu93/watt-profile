"""Initial Logger schema, independent of the future Server migration history."""

from alembic import op

from watt_profile.logger.storage.schema import metadata

revision = "logger_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(op.get_bind())


def downgrade():
    metadata.drop_all(op.get_bind())
