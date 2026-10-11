"""Validated deployment settings for the standalone Logger."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class SettingsError(ValueError):
    """A deployment setting is missing or invalid."""


@dataclass(frozen=True, slots=True)
class LoggerSettings:
    data_dir: Path
    host: str
    port: int
    input_mode: str
    operator_user: str
    operator_password_file: Path
    cookie_secure: bool

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> LoggerSettings:
        values = os.environ if environ is None else environ
        raw_data_dir = values.get("WATT_LOGGER_DATA_DIR", "/var/lib/watt-profile")
        if not raw_data_dir.strip() or "\x00" in raw_data_dir:
            raise SettingsError("invalid logger data directory")
        data_dir = Path(raw_data_dir)
        if not data_dir.is_absolute():
            raise SettingsError("logger data directory must be absolute")
        host = values.get("WATT_LOGGER_HOST", "0.0.0.0")
        raw_port = values.get("WATT_LOGGER_PORT", "8080")
        input_mode = values.get("WATT_LOGGER_INPUT", "serial")
        operator_user = values.get("WATT_LOGGER_OPERATOR_USER", "")
        password_path = values.get("WATT_LOGGER_OPERATOR_PASSWORD_FILE", "")
        raw_secure = values.get("WATT_LOGGER_COOKIE_SECURE", "false").lower()

        try:
            port = int(raw_port)
        except ValueError as exc:
            raise SettingsError("invalid logger port") from exc
        if not 1 <= port <= 65535:
            raise SettingsError("invalid logger port")
        if not host or "\x00" in host:
            raise SettingsError("invalid logger host")
        if input_mode not in {"serial", "synthetic"}:
            raise SettingsError("invalid logger input mode")
        if not 1 <= len(operator_user) <= 64 or "\x00" in operator_user:
            raise SettingsError("invalid operator username")
        if not password_path:
            raise SettingsError("operator password file is required")
        if raw_secure not in {"true", "false"}:
            raise SettingsError("invalid cookie secure setting")
        password_file = Path(password_path)
        try:
            password = password_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SettingsError("operator password file is unreadable") from exc
        if password.endswith("\n"):
            password = password[:-1]
        if not 16 <= len(password) <= 256 or "\x00" in password:
            raise SettingsError("invalid operator password")
        return cls(
            data_dir=data_dir,
            host=host,
            port=port,
            input_mode=input_mode,
            operator_user=operator_user,
            operator_password_file=password_file,
            cookie_secure=raw_secure == "true",
        )
