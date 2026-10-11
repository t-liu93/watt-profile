"""Validated deployment settings for the Server."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path

from sqlalchemy.engine import make_url


class SettingsError(ValueError):
    """A required Server setting is missing or invalid."""


def _valid_public_origin(origin: str) -> bool:
    # Parse the original text strictly: URL parsers can silently remove controls
    # and accept invalid hostname characters, unlike browser Origin serialization.
    match = re.fullmatch(
        r"https?://(?P<host>\[[0-9a-fA-F:.]+\]|[a-zA-Z0-9.-]+)(?::(?P<port>[0-9]+))?",
        origin,
    )
    if match is None:
        return False
    raw_port = match.group("port")
    if raw_port is not None and (len(raw_port) > 5 or not 1 <= int(raw_port) <= 65535):
        return False
    host = match.group("host")
    if host.startswith("["):
        try:
            IPv6Address(host[1:-1])
        except ValueError:
            return False
        return True
    if re.fullmatch(r"[0-9.]+", host):
        try:
            IPv4Address(host)
        except ValueError:
            return False
        return True
    # DNS names (including localhost and ASCII IDNA names) use nonempty labels.
    name = host.removesuffix(".")
    return len(name) <= 253 and all(
        re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
        for label in name.split(".")
    )


def _private_text_file(raw_path: str, label: str, *, maximum: int = 16_384) -> tuple[Path, str]:
    if not raw_path or "\x00" in raw_path:
        raise SettingsError(f"{label} file is required")
    path = Path(raw_path)
    try:
        metadata = path.lstat()
        parent_mode = path.parent.stat().st_mode
        if not stat.S_ISREG(metadata.st_mode):
            raise SettingsError(f"{label} file must be a regular file")
        if metadata.st_mode & 0o077 or parent_mode & 0o077:
            raise SettingsError(f"{label} file and parent must be private")
        if metadata.st_size > maximum:
            raise SettingsError(f"{label} file is too large")
        value = path.read_text(encoding="utf-8")
    except SettingsError:
        raise
    except (OSError, UnicodeError) as exc:
        raise SettingsError(f"{label} file is unreadable") from exc
    if value.endswith("\n"):
        value = value[:-1]
    if "\x00" in value or "\r" in value or "\n" in value:
        raise SettingsError(f"invalid {label} file")
    return path, value


@dataclass(frozen=True, slots=True)
class ServerSettings:
    database_url_file: Path
    database_url: str = field(repr=False)
    admin_user: str
    admin_password_file: Path
    admin_password: str = field(repr=False)
    public_origin: str
    host: str
    port: int
    cookie_secure: bool

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> ServerSettings:
        values = os.environ if environ is None else environ
        database_file, database_url = _private_text_file(
            values.get("WATT_SERVER_DATABASE_URL_FILE", ""), "database URL"
        )
        if not database_url.startswith("postgresql+psycopg://"):
            raise SettingsError("database URL must use postgresql+psycopg")
        try:
            parsed_database_url = make_url(database_url)
        except Exception as exc:
            raise SettingsError("invalid database URL") from exc
        if not parsed_database_url.host or not parsed_database_url.database:
            raise SettingsError("invalid database URL")

        admin_user = values.get("WATT_SERVER_ADMIN_USER", "")
        if not 1 <= len(admin_user) <= 64 or "\x00" in admin_user:
            raise SettingsError("invalid administrator username")
        password_file, password = _private_text_file(
            values.get("WATT_SERVER_ADMIN_PASSWORD_FILE", ""),
            "administrator password",
            maximum=1_024,
        )
        if not 16 <= len(password) <= 256:
            raise SettingsError("invalid administrator password")

        origin = values.get("WATT_SERVER_PUBLIC_ORIGIN", "")
        if not _valid_public_origin(origin):
            raise SettingsError("invalid public origin")

        host = values.get("WATT_SERVER_HOST", "0.0.0.0")
        raw_port = values.get("WATT_SERVER_PORT", "8080")
        raw_secure = values.get("WATT_SERVER_COOKIE_SECURE", "true").lower()
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise SettingsError("invalid Server port") from exc
        if not host or "\x00" in host:
            raise SettingsError("invalid Server host")
        if not 1 <= port <= 65535:
            raise SettingsError("invalid Server port")
        if raw_secure not in {"true", "false"}:
            raise SettingsError("invalid cookie secure setting")
        return cls(
            database_url_file=database_file,
            database_url=database_url,
            admin_user=admin_user,
            admin_password_file=password_file,
            admin_password=password,
            public_origin=origin,
            host=host,
            port=port,
            cookie_secure=raw_secure == "true",
        )
