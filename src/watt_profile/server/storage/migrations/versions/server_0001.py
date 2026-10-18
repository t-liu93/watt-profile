"""Frozen initial Server schema. Never import live application schema here."""

from alembic import op
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "server_0001"
down_revision = None
branch_labels = None
depends_on = None

metadata = MetaData(
    naming_convention={
        "pk": "pk_%(table_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ix": "ix_%(table_name)s_%(column_0_name)s",
    }
)


def identity(name, version=4):
    return Column(name, UUID(as_uuid=True), nullable=False), CheckConstraint(
        f"{name}::text ~ '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-{version}[0-9a-f]{{3}}-"
        "[89ab][0-9a-f]{3}-[0-9a-f]{12}$'",
        name=f"ck_uuid_{name}",
    )


def time(name, nullable=False, default=None):
    return Column(
        name,
        DateTime(timezone=True),
        nullable=nullable,
        server_default=text(default) if default else None,
    )


def fk(columns, target, name=None):
    return ForeignKeyConstraint(
        columns, target, name=name, onupdate="NO ACTION", ondelete="NO ACTION"
    )


devices = Table(
    "devices",
    metadata,
    *identity("device_id"),
    time("created_at"),
    time("last_authenticated_contact_at", True, "NULL"),
    PrimaryKeyConstraint("device_id"),
)
sites = Table(
    "sites",
    metadata,
    *identity("site_id"),
    Column("address", Text, nullable=False, server_default=""),
    Column("description", Text, nullable=False, server_default=""),
    PrimaryKeyConstraint("site_id"),
)
points = Table(
    "measurement_points",
    metadata,
    *identity("measurement_point_id", 5),
    *identity("site_id"),
    Column("kind", Text, nullable=False, server_default="grid-connection"),
    CheckConstraint("kind = 'grid-connection'", name="ck_point_kind"),
    PrimaryKeyConstraint("measurement_point_id"),
    fk(["site_id"], ["sites.site_id"]),
    UniqueConstraint("measurement_point_id", "site_id", name="uq_points_id_site"),
    UniqueConstraint("site_id", "kind", name="uq_points_site_kind"),
)
sessions = Table(
    "sessions",
    metadata,
    *identity("session_id"),
    *identity("device_id"),
    *identity("site_id"),
    time("started_at"),
    PrimaryKeyConstraint("session_id"),
    fk(["device_id"], ["devices.device_id"]),
    fk(["site_id"], ["sites.site_id"]),
    UniqueConstraint("session_id", "device_id", "site_id", name="uq_sessions_id_device_site"),
    UniqueConstraint("session_id", "site_id", name="uq_sessions_id_site"),
    UniqueConstraint("session_id", "device_id", name="uq_sessions_id_device"),
)
sources = Table(
    "sources",
    metadata,
    *identity("source_id"),
    *identity("session_id"),
    *identity("measurement_point_id", 5),
    *identity("site_id"),
    Column("kind", Text, nullable=False, server_default="p1_serial"),
    Column("profile_id", Text, nullable=False, server_default="dsmr5_nl"),
    Column("profile_revision", Integer, nullable=False, server_default="1"),
    CheckConstraint("kind = 'p1_serial'", name="ck_source_kind"),
    CheckConstraint("profile_id = 'dsmr5_nl'", name="ck_source_profile"),
    CheckConstraint("profile_revision = 1", name="ck_source_revision"),
    PrimaryKeyConstraint("source_id"),
    fk(["session_id"], ["sessions.session_id"]),
    fk(["site_id"], ["sites.site_id"]),
    fk(["measurement_point_id"], ["measurement_points.measurement_point_id"]),
    fk(
        ["session_id", "site_id"],
        ["sessions.session_id", "sessions.site_id"],
        "fk_sources_session_site",
    ),
    fk(
        ["measurement_point_id", "site_id"],
        ["measurement_points.measurement_point_id", "measurement_points.site_id"],
        "fk_sources_point_site",
    ),
    UniqueConstraint(
        "source_id", "session_id", "measurement_point_id", name="uq_sources_id_session_point"
    ),
    UniqueConstraint("session_id", name="uq_sources_session"),
)

snapshot_columns = []
for name in (
    "device_id",
    "record_id",
    "site_id",
    "session_id",
    "source_id",
    "measurement_point_id",
):
    snapshot_columns.extend(identity(name, 5 if name == "measurement_point_id" else 4))
for name in (
    "energy_import_t1_kwh",
    "energy_import_t2_kwh",
    "energy_export_t1_kwh",
    "energy_export_t2_kwh",
    "power_import_kw",
    "power_export_kw",
    "voltage_l1_v",
    "voltage_l2_v",
    "voltage_l3_v",
    "current_l1_a",
    "current_l2_a",
    "current_l3_a",
    "power_import_l1_kw",
    "power_import_l2_kw",
    "power_import_l3_kw",
    "power_export_l1_kw",
    "power_export_l2_kw",
    "power_export_l3_kw",
):
    snapshot_columns.extend(
        (
            Column(name, Numeric, nullable=True),
            CheckConstraint(
                f"{name} >= 0 AND {name} NOT IN "
                "('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
                name=f"ck_snapshot_{name}",
            ),
        )
    )
snapshots = Table(
    "snapshots",
    metadata,
    *snapshot_columns,
    Column("schema_version", Integer, nullable=False, server_default="1"),
    Column("meter_time_raw", Text, nullable=True),
    time("meter_time_utc", True),
    time("received_at"),
    time("sampled_at"),
    Column("fields", JSONB, nullable=False),
    Column("quality", JSONB, nullable=False),
    Column("canonical_snapshot", Text, nullable=False),
    Column("payload_sha256", Text, nullable=False),
    time("ingested_at"),
    PrimaryKeyConstraint("device_id", "record_id"),
    CheckConstraint("schema_version = 1", name="ck_snapshot_schema"),
    CheckConstraint("meter_time_raw ~ '^[0-9]{12}[SW]$'", name="ck_snapshot_meter_time"),
    CheckConstraint("jsonb_typeof(fields) = 'array'", name="ck_snapshot_fields"),
    CheckConstraint("jsonb_typeof(quality) = 'object'", name="ck_snapshot_quality"),
    CheckConstraint(
        "jsonb_typeof(canonical_snapshot::jsonb) = 'object' "
        "AND octet_length(canonical_snapshot) <= 16384",
        name="ck_snapshot_canonical",
    ),
    CheckConstraint("payload_sha256 ~ '^[0-9a-f]{64}$'", name="ck_snapshot_hash"),
    *(
        fk([name], [target])
        for name, target in (
            ("device_id", "devices.device_id"),
            ("site_id", "sites.site_id"),
            ("session_id", "sessions.session_id"),
            ("source_id", "sources.source_id"),
            ("measurement_point_id", "measurement_points.measurement_point_id"),
        )
    ),
    fk(
        ["session_id", "device_id", "site_id"],
        ["sessions.session_id", "sessions.device_id", "sessions.site_id"],
        "fk_snapshots_session_owner",
    ),
    fk(
        ["source_id", "session_id", "measurement_point_id"],
        ["sources.source_id", "sources.session_id", "sources.measurement_point_id"],
        "fk_snapshots_source_owner",
    ),
    fk(
        ["measurement_point_id", "site_id"],
        ["measurement_points.measurement_point_id", "measurement_points.site_id"],
        "fk_snapshots_point_site",
    ),
)
credentials = Table(
    "device_credentials",
    metadata,
    *identity("credential_id"),
    *identity("device_id"),
    Column("token_sha256", Text, nullable=False),
    time("created_at"),
    time("disabled_at", True, "NULL"),
    PrimaryKeyConstraint("credential_id"),
    fk(["device_id"], ["devices.device_id"]),
    UniqueConstraint("token_sha256", name="uq_credentials_token_hash"),
    CheckConstraint("token_sha256 ~ '^[0-9a-f]{64}$'", name="ck_credentials_hash"),
)
authorizations = Table(
    "device_site_authorizations",
    metadata,
    *identity("device_id"),
    *identity("site_id"),
    PrimaryKeyConstraint("device_id", "site_id"),
    fk(["device_id"], ["devices.device_id"]),
    fk(["site_id"], ["sites.site_id"]),
)
for table, columns in (
    (sessions, ("device_id",)),
    (sessions, ("site_id",)),
    (sources, ("measurement_point_id", "site_id")),
    (credentials, ("device_id",)),
    (authorizations, ("site_id", "device_id")),
    (snapshots, ("site_id", "received_at")),
    (snapshots, ("site_id", "session_id", "received_at")),
    (snapshots, ("device_id", "received_at")),
    (snapshots, ("source_id", "received_at")),
):
    Index("ix_" + table.name + "_" + "_".join(columns), *(table.c[name] for name in columns))


def upgrade():
    metadata.create_all(op.get_bind())


def downgrade():
    raise RuntimeError("server_downgrade_not_supported")
