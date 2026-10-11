"""Logger-only relational schema; lexical checks use connection-registered functions."""

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
)

from watt_profile.contracts.domain import Metrics

metadata = MetaData()


def column(name, kind="TEXT", *, nullable=False, default=None, primary=False):
    sql_type = Integer if kind in {"INT", "BOOL"} else Text
    checks = []
    function = {
        "U4": "wp_uuid4",
        "U5": "wp_uuid5",
        "UTC": "wp_utc",
        "HASH": "wp_hash",
        "DEC": "wp_decimal",
    }.get(kind)
    if function:
        checks.append(CheckConstraint(f"{name} IS NULL OR {function}({name}) = 1"))
    elif kind in {"TEXT", "INT", "BOOL"}:
        storage = "integer" if sql_type is Integer else "text"
        checks.append(CheckConstraint(f"{name} IS NULL OR typeof({name}) = '{storage}'"))
    if kind == "BOOL":
        checks.append(CheckConstraint(f"{name} IN (0,1)"))
    return Column(
        name, sql_type, *checks, nullable=nullable, server_default=default, primary_key=primary
    )


def fk(names, target, columns=None):
    return ForeignKeyConstraint(
        names,
        [f"{target}.{n}" for n in (columns or names)],
        onupdate="NO ACTION",
        ondelete="NO ACTION",
    )


devices = Table(
    "devices", metadata, column("device_id", "U4", primary=True), column("created_at", "UTC")
)
sites = Table(
    "sites",
    metadata,
    column("site_id", "U4", primary=True),
    column("address", default=""),
    column("description", default=""),
)
points = Table(
    "measurement_points",
    metadata,
    column("measurement_point_id", "U5", primary=True),
    column("site_id", "U4"),
    column("kind", default="grid-connection"),
    fk(["site_id"], "sites"),
    UniqueConstraint("measurement_point_id", "site_id"),
    UniqueConstraint("site_id", "kind"),
    CheckConstraint("kind='grid-connection'"),
)
sessions = Table(
    "sessions",
    metadata,
    column("session_id", "U4", primary=True),
    column("device_id", "U4"),
    column("site_id", "U4"),
    column("started_at", "UTC"),
    column("state", default="open"),
    column("ended_at", "UTC", nullable=True),
    column("collection_enabled", "BOOL", default="0"),
    fk(["device_id"], "devices"),
    fk(["site_id"], "sites"),
    UniqueConstraint("session_id", "device_id", "site_id"),
    UniqueConstraint("session_id", "site_id"),
    UniqueConstraint("session_id", "device_id"),
    CheckConstraint(
        "(state='open' AND ended_at IS NULL) OR "
        "(state='completed' AND ended_at IS NOT NULL "
        "AND collection_enabled=0)"
    ),
)
sources = Table(
    "sources",
    metadata,
    column("source_id", "U4", primary=True),
    column("session_id", "U4"),
    column("measurement_point_id", "U5"),
    column("site_id", "U4"),
    column("kind", default="p1_serial"),
    column("profile_id", default="dsmr5_nl"),
    column("profile_revision", "INT", default="1"),
    column("meter_fingerprint", "HASH", nullable=True),
    fk(["session_id"], "sessions"),
    fk(["measurement_point_id"], "measurement_points"),
    fk(["site_id"], "sites"),
    fk(["session_id", "site_id"], "sessions"),
    fk(["measurement_point_id", "site_id"], "measurement_points"),
    UniqueConstraint("source_id", "session_id", "measurement_point_id"),
    UniqueConstraint("session_id"),
    CheckConstraint("kind='p1_serial' AND profile_id='dsmr5_nl' AND profile_revision=1"),
)
snapshots = Table(
    "snapshots",
    metadata,
    *[
        column(n, "U5" if n == "measurement_point_id" else "U4")
        for n in [
            "device_id",
            "record_id",
            "site_id",
            "session_id",
            "source_id",
            "measurement_point_id",
        ]
    ],
    column("schema_version", "INT", default="1"),
    column("meter_time_raw", nullable=True),
    column("meter_time_utc", "UTC", nullable=True),
    column("received_at", "UTC"),
    column("sampled_at", "UTC"),
    *[column(n, "DEC", nullable=True) for n in Metrics.model_fields],
    column("fields"),
    column("quality"),
    column("canonical_snapshot"),
    column("payload_sha256", "HASH"),
    column("upload_state", default="pending"),
    column("uploaded_at", "UTC", nullable=True),
    PrimaryKeyConstraint("device_id", "record_id"),
    *[
        fk([n], t)
        for n, t in [
            ("device_id", "devices"),
            ("site_id", "sites"),
            ("session_id", "sessions"),
            ("source_id", "sources"),
            ("measurement_point_id", "measurement_points"),
        ]
    ],
    fk(["session_id", "device_id", "site_id"], "sessions"),
    fk(["source_id", "session_id", "measurement_point_id"], "sources"),
    fk(["measurement_point_id", "site_id"], "measurement_points"),
    CheckConstraint("schema_version=1"),
    CheckConstraint("meter_time_raw IS NULL OR wp_meter_time(meter_time_raw)=1"),
    CheckConstraint("wp_json_kind(fields,'array')=1"),
    CheckConstraint("wp_json_kind(quality,'object')=1"),
    CheckConstraint("wp_json_kind(canonical_snapshot,'object')=1"),
    CheckConstraint("length(CAST(canonical_snapshot AS BLOB)) <= 16384"),
    CheckConstraint(
        "(upload_state='pending' AND uploaded_at IS NULL) OR "
        "(upload_state='uploaded' AND uploaded_at IS NOT NULL)"
    ),
)
config = Table(
    "logger_config",
    metadata,
    column("singleton_id", "INT", default="1", primary=True),
    column("device_id", "U4"),
    column("active_session_id", "U4", nullable=True),
    column("server_url", nullable=True),
    column("serial_path", nullable=True),
    column("profile_id", default="dsmr5_nl"),
    column("profile_revision", "INT", default="1"),
    column("baudrate", "INT", default="115200"),
    column("bytesize", "INT", default="8"),
    column("parity", default="N"),
    column("stopbits", "DEC", default="1"),
    column("timeout", "DEC", default="1"),
    column("persisted_blocks", default="[]"),
    column("last_successful_upload_at", "UTC", nullable=True),
    fk(["device_id"], "devices"),
    fk(["active_session_id", "device_id"], "sessions", ["session_id", "device_id"]),
    CheckConstraint("singleton_id=1 AND profile_id='dsmr5_nl' AND profile_revision=1"),
    CheckConstraint("baudrate>0 AND bytesize IN (5,6,7,8) AND parity IN ('N','E','O','M','S')"),
    CheckConstraint("wp_serial_decimal(stopbits,0)=1 AND wp_serial_decimal(timeout,1)=1"),
    CheckConstraint("serial_path IS NULL OR length(serial_path)>0"),
    CheckConstraint("server_url IS NULL OR wp_origin(server_url)=1"),
    CheckConstraint("wp_blocks(persisted_blocks)=1"),
)
Index(
    "ix_snapshots_pending", snapshots.c.upload_state, snapshots.c.received_at, snapshots.c.record_id
)
Index("ix_snapshots_cleanup", snapshots.c.session_id, snapshots.c.upload_state)
Index("ix_sessions_device", sessions.c.device_id)
Index("ix_sessions_site", sessions.c.site_id)
Index("ix_sources_point_site", sources.c.measurement_point_id, sources.c.site_id)
