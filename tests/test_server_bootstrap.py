from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from postgres_test_server import (
    PostgresOptInRequired,
    start_postgres_test_server,
    stop_postgres_test_server,
)

from watt_profile.server.settings import ServerSettings, SettingsError


def _settings_files(tmp_path: Path) -> dict[str, str]:
    database = tmp_path / "database-url"
    database.write_text("postgresql+psycopg://u:p@127.0.0.1:5432/db\n", encoding="utf-8")
    os.chmod(database, 0o600)
    password = tmp_path / "admin-password"
    password.write_text("synthetic-admin-password\n", encoding="utf-8")
    os.chmod(password, 0o600)
    return {
        "WATT_SERVER_DATABASE_URL_FILE": str(database),
        "WATT_SERVER_ADMIN_USER": "admin",
        "WATT_SERVER_ADMIN_PASSWORD_FILE": str(password),
        "WATT_SERVER_PUBLIC_ORIGIN": "https://example.test",
    }


def test_settings_load_private_files_and_defaults(tmp_path: Path) -> None:
    settings = ServerSettings.from_env(_settings_files(tmp_path))
    assert settings.database_url.endswith("/db")
    assert settings.admin_password == "synthetic-admin-password"
    assert (settings.host, settings.port, settings.cookie_secure) == ("0.0.0.0", 8080, True)


@pytest.mark.parametrize(
    "origin",
    [
        "https://exa mple.test",
        "https://example.test\n",
        "https://example.test\\evil.test",
        "\thttps://example.test",
        "https://exam\tple.test",
        "https://example.test\r",
        "https://example.test\x00",
        "https://example.test\x7f",
        "https://example.test?",
        "https://example.test#",
        "https://example.test:",
        "https://example.test:0",
        "https://example.test:65536",
        "https://-example.test",
        "https://example..test",
        "https://example_.test",
        "https://256.0.0.1",
        "https://[::1]:bad",
        "https://[:::1]",
    ],
)
def test_settings_reject_invalid_public_origins(tmp_path: Path, origin: str) -> None:
    with pytest.raises(SettingsError, match="invalid public origin"):
        ServerSettings.from_env({**_settings_files(tmp_path), "WATT_SERVER_PUBLIC_ORIGIN": origin})


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.test",
        "https://sub-domain.example.test:443",
        "https://xn--bcher-kva.example",
        "http://localhost:8080",
        "http://127.0.0.1:1",
        "https://192.0.2.1:65535",
        "http://[::1]:8080",
        "https://[2001:db8::1]",
        "http://[::ffff:192.0.2.1]:8080",
    ],
)
def test_settings_accept_valid_public_origins(tmp_path: Path, origin: str) -> None:
    settings = ServerSettings.from_env(
        {**_settings_files(tmp_path), "WATT_SERVER_PUBLIC_ORIGIN": origin}
    )
    assert settings.public_origin == origin


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("WATT_SERVER_DATABASE_URL_FILE", ""),
        ("WATT_SERVER_ADMIN_USER", ""),
        ("WATT_SERVER_PUBLIC_ORIGIN", "https://example.test/path"),
        ("WATT_SERVER_PUBLIC_ORIGIN", "https://user@example.test"),
        ("WATT_SERVER_PORT", "0"),
        ("WATT_SERVER_COOKIE_SECURE", "yes"),
    ],
)
def test_settings_reject_invalid_values(tmp_path: Path, key: str, value: str) -> None:
    with pytest.raises(SettingsError):
        ServerSettings.from_env({**_settings_files(tmp_path), key: value})


def test_settings_refuse_non_private_file_and_parent(tmp_path: Path) -> None:
    env = _settings_files(tmp_path)
    password_path = Path(env["WATT_SERVER_ADMIN_PASSWORD_FILE"])
    os.chmod(password_path, 0o640)
    with pytest.raises(SettingsError, match="private"):
        ServerSettings.from_env(env)
    os.chmod(password_path, 0o600)
    os.chmod(tmp_path, 0o750)
    with pytest.raises(SettingsError, match="private"):
        ServerSettings.from_env(env)


def test_settings_reject_malformed_or_multiline_database_url(tmp_path: Path) -> None:
    env = _settings_files(tmp_path)
    database = Path(env["WATT_SERVER_DATABASE_URL_FILE"])
    database.write_text("postgresql+psycopg://\n", encoding="utf-8")
    os.chmod(database, 0o600)
    with pytest.raises(SettingsError, match="database URL"):
        ServerSettings.from_env(env)


def test_password_cli_exclusive_and_private(tmp_path: Path) -> None:
    output = tmp_path / "new-password"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "watt_profile.cli.server_admin",
            "generate-password",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    password = output.read_text(encoding="utf-8").removesuffix("\n")
    assert result.stdout.strip() == password
    assert len(password) >= 32
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    refused = subprocess.run(
        [
            sys.executable,
            "-m",
            "watt_profile.cli.server_admin",
            "generate-password",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert refused.returncode == 1
    assert output.read_text(encoding="utf-8").strip() == password


def test_database_launcher_requires_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WATT_TEST_POSTGRES", raising=False)
    monkeypatch.delenv("WATT_SERVER_DATABASE_URL", raising=False)
    with pytest.raises(PostgresOptInRequired):
        start_postgres_test_server()


@pytest.mark.skipif(
    os.environ.get("WATT_TEST_POSTGRES") != "1", reason="explicit PostgreSQL opt-in required"
)
def test_postgres_launcher_starts_and_cleans_only_its_resources() -> None:
    server = start_postgres_test_server()
    try:
        assert server.database_url.startswith("postgresql+psycopg://")
        assert stat.S_IMODE(server.database_url_file.stat().st_mode) == 0o600
        assert server.directory.is_dir()
        assert server.port > 0
    finally:
        stop_postgres_test_server(server)
    assert not server.directory.exists()
    volume = subprocess.run(
        ["docker", "volume", "inspect", server.volume_name], capture_output=True, text=True
    )
    assert volume.returncode != 0


def test_server_imports_have_no_runtime_effects(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop("WATT_SERVER_DATABASE_URL_FILE", None)
    env.pop("WATT_SERVER_ADMIN_PASSWORD_FILE", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import watt_profile.server.settings, watt_profile.server.bootstrap",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == ""
    assert list(tmp_path.iterdir()) == []
