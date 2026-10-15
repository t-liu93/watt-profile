"""Private SQLite connections and packaged Alembic migration runner."""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import UUID

import idna
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event


def valid_origin(value: object) -> bool:
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return False
    try:
        url = urlsplit(value)
        return (
            url.scheme in {"http", "https"}
            and bool(url.hostname)
            and url.port != 0
            and url.username is None
            and url.password is None
            and not url.path
            and not url.query
            and not url.fragment
            and "?" not in value
            and "#" not in value
        )
    except ValueError:
        return False


def origin_identity(value: str | None) -> tuple[str, str, int | None] | None:
    """Return the comparison identity without changing the saved origin string."""
    if value is None:
        return None
    if not valid_origin(value):
        raise ValueError("invalid_local_settings")
    url = urlsplit(value)
    host = url.hostname
    try:
        host = ip_address(host).compressed
    except ValueError:
        # Mirror HTTPX's encode_host split: ASCII reg-names are lowercased and
        # quoted with HTTPX's safe set, while only non-ASCII hosts use IDNA.
        if host.isascii():
            host = quote(host.lower(), safe="!$&'()*+,;=\"`{}%|\\")
        else:
            try:
                host = idna.encode(host.lower()).decode("ascii")
            except (idna.IDNAError, UnicodeError):
                # M1 accepts these origins as settings even though HTTPX/IDNA
                # cannot route them. Keep their literal host spelling in a
                # namespace that no normalized transport host can occupy.
                host = "\x00unencodable:" + host
    port = url.port or (443 if url.scheme == "https" else 80)
    return url.scheme.lower(), host, port


def _uuid(value, version):
    try:
        parsed = UUID(value)
        return type(value) is str and str(parsed) == value and parsed.version == version
    except (ValueError, TypeError, AttributeError):
        return False


def _utc(value):
    try:
        return bool(
            re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:"
                r"[0-9]{2}\.[0-9]{6}Z",
                value,
            )
        ) and bool(datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ"))
    except (TypeError, ValueError):
        return False


def _match(pattern, value):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def _reject_json_constant(value):
    raise ValueError("invalid_json_constant")


def _json_kind(value, kind):
    try:
        parsed = json.loads(value, parse_constant=_reject_json_constant)
        return type(parsed) is {"array": list, "object": dict}[kind]
    except (ValueError, TypeError, KeyError):
        return False


def _blocks(value):
    try:
        blocks = json.loads(value)
        return (
            type(blocks) is list
            and len(blocks) <= 2
            and all(type(b) is str and b in {"meter_changed", "storage_full"} for b in blocks)
            and len(set(blocks)) == len(blocks)
        )
    except (ValueError, TypeError):
        return False


def _serial_decimal(value, timeout):
    if not _match(r"[0-9]+(?:\.[0-9]+)?", value):
        return False
    number = Decimal(value)
    return number > 0 if timeout else number in {Decimal(1), Decimal("1.5"), Decimal(2)}


def create_database(path: Path) -> Engine:
    """Open a file database; callers own directory preparation and installation locking."""
    engine = create_engine("sqlite:///" + str(path), connect_args={"timeout": 5})

    @event.listens_for(engine, "connect")
    def configure(connection, _record):
        functions = {
            "wp_uuid4": (1, lambda v: _uuid(v, 4)),
            "wp_uuid5": (1, lambda v: _uuid(v, 5)),
            "wp_utc": (1, _utc),
            "wp_hash": (1, lambda v: _match(r"[0-9a-f]{64}", v)),
            "wp_decimal": (1, lambda v: _match(r"[0-9]+(?:\.[0-9]+)?", v)),
            "wp_meter_time": (1, lambda v: _match(r"[0-9]{12}[SW]", v)),
            "wp_json_kind": (2, _json_kind),
            "wp_blocks": (1, _blocks),
            "wp_origin": (1, valid_origin),
            "wp_serial_decimal": (2, _serial_decimal),
        }
        for name, (arity, function) in functions.items():
            connection.create_function(name, arity, function, deterministic=True)
        cursor = connection.cursor()
        try:
            for pragma in [
                "foreign_keys=ON",
                "journal_mode=WAL",
                "synchronous=FULL",
                "busy_timeout=5000",
            ]:
                cursor.execute("PRAGMA " + pragma)
            for name, expected in [
                ("foreign_keys", 1),
                ("journal_mode", "wal"),
                ("synchronous", 2),
                ("busy_timeout", 5000),
            ]:
                if cursor.execute("PRAGMA " + name).fetchone()[0] != expected:
                    raise RuntimeError("sqlite_durability_unavailable")
        finally:
            cursor.close()

    return engine


def migrate(engine: Engine) -> None:
    configuration = Config()
    configuration.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    with engine.begin() as connection:
        # sqlite3's legacy mode otherwise autocommits DDL before its first DML.
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        configuration.attributes["connection"] = connection
        command.upgrade(configuration, "head")
