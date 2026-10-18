"""Server display names and analysis access paths."""

import sqlalchemy as sa
from alembic import op

revision = "server_0002"
down_revision = "server_0001"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("sites", "devices"):
        op.add_column(
            table, sa.Column("display_name", sa.Text(), nullable=False, server_default="")
        )
    op.create_index(
        "ix_snapshots_site_id_session_id_received_at_device_id_record_id",
        "snapshots",
        ["site_id", "session_id", "received_at", "device_id", "record_id"],
    )
    op.create_index(
        "ix_sessions_site_id_started_at_session_id",
        "sessions",
        ["site_id", "started_at", "session_id"],
    )


def downgrade():
    raise RuntimeError("server_downgrade_not_supported")
