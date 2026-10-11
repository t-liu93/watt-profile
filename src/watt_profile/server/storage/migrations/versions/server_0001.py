"""Initial independent PostgreSQL Server schema."""

from alembic import op

from watt_profile.server.storage.schema import metadata

revision = "server_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(op.get_bind())


def downgrade():
    raise RuntimeError("server_downgrade_not_supported")
