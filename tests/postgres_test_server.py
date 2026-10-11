"""Explicitly opted-in disposable PostgreSQL 17 test launcher.

Set WATT_TEST_POSTGRES=1 to start a fresh loopback-only container. The launcher
never reads deployment database variables or connects to a supplied database.
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


class PostgresOptInRequired(RuntimeError):
    """Raised when a database test was started without explicit opt-in."""


@dataclass(slots=True)
class PostgresTestServer:
    container_name: str
    directory: Path
    database_url_file: Path
    port: int
    volume_name: str

    @property
    def database_url(self) -> str:
        return self.database_url_file.read_text(encoding="utf-8").removesuffix("\n")


def _run(
    arguments: list[str], *, timeout: float = 30, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments, check=True, capture_output=True, text=True, timeout=timeout, env=env
    )


def start_postgres_test_server() -> PostgresTestServer:
    if os.environ.get("WATT_TEST_POSTGRES") != "1":
        raise PostgresOptInRequired("set WATT_TEST_POSTGRES=1 to start isolated PostgreSQL tests")
    # Ignore any ambient deployment URL by design. Refuse an explicit attempt to
    # smuggle one into this launcher so the test contract stays obvious.
    if "WATT_SERVER_DATABASE_URL" in os.environ:
        raise PostgresOptInRequired("database tests accept no ambient database URL")

    directory = Path(tempfile.mkdtemp(prefix="watt-m3-postgres-"))
    os.chmod(directory, 0o700)
    suffix = secrets.token_hex(8)
    name = f"watt-m3-test-{suffix}"
    container_created = False
    password = secrets.token_urlsafe(24)
    try:
        docker_env = {"PATH": os.environ.get("PATH", ""), "POSTGRES_PASSWORD": password}
        for key in (
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        ):
            if key in os.environ:
                docker_env[key] = os.environ[key]
        created = _run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                name,
                "--label",
                "watt-profile.m3-test=true",
                "--env",
                "POSTGRES_PASSWORD",
                "--env",
                "POSTGRES_USER=watt_test",
                "--env",
                "POSTGRES_DB=watt_test",
                "--publish",
                "127.0.0.1::5432",
                "postgres:17.6-alpine",
            ],
            env=docker_env,
        )
        del created
        container_created = True
        port_text = _run(["docker", "port", name, "5432/tcp"]).stdout.strip()
        host, raw_port = port_text.rsplit(":", 1)
        if host != "127.0.0.1":
            raise RuntimeError("PostgreSQL test port was not loopback-only")
        port = int(raw_port)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            state = _run(
                ["docker", "inspect", "--format", "{{.State.Running}}", name]
            ).stdout.strip()
            if state != "true":
                raise RuntimeError("PostgreSQL test container exited before becoming ready")
            ready = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", "watt_test", "-d", "watt_test"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("PostgreSQL test container did not become ready")

        # Verify the published endpoint binds only loopback before exposing its URL.
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
        url_path = directory / "database-url"
        url = f"postgresql+psycopg://watt_test:{password}@127.0.0.1:{port}/watt_test"
        fd = os.open(url_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(url + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        mounts = _run(
            [
                "docker",
                "inspect",
                "--format",
                '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}{{end}}{{end}}',
                name,
            ]
        ).stdout.strip()
        if not mounts:
            raise RuntimeError("PostgreSQL test container has no tracked data volume")
        return PostgresTestServer(name, directory, url_path, port, mounts)
    except Exception:
        if container_created:
            subprocess.run(
                ["docker", "rm", "--force", "--volumes", name],
                capture_output=True,
                text=True,
            )
        import shutil

        shutil.rmtree(directory, ignore_errors=True)
        raise


def stop_postgres_test_server(server: PostgresTestServer) -> None:
    """Remove this launcher's own container and private directory."""
    import shutil

    try:
        _run(["docker", "rm", "--force", "--volumes", server.container_name])
    finally:
        shutil.rmtree(server.directory, ignore_errors=True)
