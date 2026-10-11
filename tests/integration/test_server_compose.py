"""Opt-in disposable real container delivery acceptance; never ambient deployment URLs."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from test_server_ingest import batch

from watt_profile.server.registration import RegistrationSummary

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    not any("tests/integration" in arg for arg in sys.argv[1:]),
    reason="Select integration tests explicitly",
)


def shell(*args, timeout=120, env=None):
    return subprocess.run(
        args, cwd=ROOT, env=env, check=True, capture_output=True, text=True, timeout=timeout
    ).stdout


def test_disposable_server_build_readiness_and_persistence(tmp_path):
    project = os.environ.get("WATT_TEST_COMPOSE_PROJECT", "")
    tag = os.environ.get("WATT_DEV_IMAGE_TAG", "")
    assert os.environ.get("WATT_TEST_SERVER_COMPOSE") == "1", "Require explicit opt-in"
    assert re.fullmatch(r"watt-m3-[a-z0-9][a-z0-9_-]*", project)
    assert tag == project, "Require exact unique project/image tag"
    image = "watt-profile-server-dev:" + tag
    for kind in ("container", "network", "volume"):
        assert not shell(
            "docker", kind, "ls", "-q", "--filter", "label=com.docker.compose.project=" + project
        ).strip(), "Refuse pre-existing project resources"
    assert not shell("docker", "image", "ls", "-q", image).strip()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = dict(os.environ, WATT_DEV_SERVER_PORT=str(port))
    command = ["docker", "compose", "-p", project, "-f", str(ROOT / "docker-compose.dev.yml")]

    def run(*args, timeout=120):
        return shell(*command, *args, timeout=timeout, env=env)

    config = json.loads(run("config", "--format", "json"))
    services = config["services"]
    assert set(services["server"]["depends_on"]) == {"server-db"}
    assert set(services["server-db"]["depends_on"]) == {"server-init"}
    assert not services["server-db"].get("ports")
    assert services["server"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert all(not v.get("external") for v in config["volumes"].values())
    assert all(not s.get("devices") and not s.get("privileged") for s in services.values())
    try:
        run("up", "--build", "-d", "--wait", "--wait-timeout", "120", "server", timeout=600)
        assert not run("ps", "--all", "-q", "logger", "logger-init").strip()
        assert run("exec", "-T", "server", "id", "-u").strip() == "10001"
        assert run("exec", "-T", "server", "id", "-g").strip() == "10001"
        probe = """
import hashlib,json
from pathlib import Path
from importlib.resources import files
root=Path('/run/credentials')
assert root.stat().st_mode & 511 == 448
assert root.stat().st_uid == 10001
for p in root.iterdir():
    assert p.stat().st_mode & 511 == 384
    assert p.stat().st_uid == 10001
try:
    (root/'forbidden').write_text('x')
except OSError:
    pass
else:
    raise AssertionError('credentials must be read-only')
assert files('watt_profile.server').joinpath('templates/base.html').is_file()
assert files('watt_profile.server').joinpath('storage/migrations/versions/server_0001.py').is_file()
print(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}))
"""
        credentials = run("exec", "-T", "server", "python", "-c", probe)
        assert (
            run(
                "exec",
                "-T",
                "server-db",
                "psql",
                "-U",
                "watt",
                "-d",
                "watt",
                "-Atc",
                "SELECT version_num FROM alembic_version",
            ).strip()
            == "server_0001"
        )
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=20
        ) as client:
            assert client.get("/health/ready").status_code == 200
            assert (
                client.get("/health/ready", headers={"Host": "untrusted.invalid"}).status_code
                == 403
            )

            def login():
                page = client.get("/login")
                csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
                assert (
                    client.post(
                        "/login",
                        data={
                            "username": "admin",
                            "password": "testtesttesttest",
                            "csrf": csrf,
                        },
                    ).status_code
                    == 303
                )
                page = client.get("/")
                return {
                    "X-CSRF-Token": re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]
                }

            headers = login()
            summary = RegistrationSummary(
                site_id=uuid4(),
                device_id=uuid4(),
                address="Disposable compose",
                description="Synthetic only",
                token="a" * 43,
            )
            assert (
                client.post(
                    "/api/admin/v1/registrations",
                    json=summary.model_dump(mode="json"),
                    headers=headers,
                ).status_code
                == 200
            )
            payload = batch(summary)
            assert (
                client.post(
                    "/api/v1/ingest/batches",
                    json=payload.model_dump(mode="json"),
                    headers={"Authorization": "Bearer " + summary.token},
                ).status_code
                == 200
            )
            path = f"/api/admin/v1/sites/{summary.site_id}/readings"
            before = client.get(path).json()
            csv = client.get(path + ".csv").content
            run("up", "-d", "--force-recreate", "--wait", "--wait-timeout", "120", "server")
            assert client.get(path).status_code == 401
            login()
            assert client.get(path).json() == before
            assert client.get(path + ".csv").content == csv
            assert run("exec", "-T", "server", "python", "-c", probe) == credentials
            run("down")
            assert shell(
                "docker",
                "volume",
                "ls",
                "-q",
                "--filter",
                "label=com.docker.compose.project=" + project,
            ).strip()
            run("up", "-d", "--wait", "--wait-timeout", "120", "server")
            client.cookies.clear()
            login()
            assert client.get(path).json() == before
            assert client.get(path + ".csv").content == csv
            assert run("exec", "-T", "server", "python", "-c", probe) == credentials
    finally:
        run("down", "--volumes", "--remove-orphans")
        if shell("docker", "image", "ls", "-q", image).strip():
            shell("docker", "image", "rm", image)


def test_standalone_config_and_isolated_caddy_validation(tmp_path):
    project = "watt-m3-config-" + uuid4().hex
    for name in ("db", "server"):
        directory = tmp_path / name
        directory.mkdir(mode=0o700)
        names = ("password",) if name == "db" else ("database-url", "admin-password")
        for filename in names:
            target = directory / filename
            target.write_text("synthetic-placeholder\n")
            target.chmod(0o600)
    env = dict(
        os.environ,
        WATT_SERVER_DOMAIN="synthetic.example.invalid",
        WATT_SERVER_ADMIN_USER="synthetic",
        WATT_SERVER_DB_SECRET_DIR=str(tmp_path / "db"),
        WATT_SERVER_SECRET_DIR=str(tmp_path / "server"),
    )
    config = json.loads(
        shell(
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            str(ROOT / "deploy/server/compose.yml"),
            "config",
            "--format",
            "json",
            env=env,
        )
    )
    services = config["services"]
    assert not services["server"].get("ports") and not services["server-db"].get("ports")
    assert {p["published"] for p in services["caddy"]["ports"]} == {"80", "443"}
    assert all(not v.get("external") for v in config["volumes"].values())
    for name in ("server", "server-db"):
        mounts = [v for v in services[name]["volumes"] if v["type"] == "bind"]
        assert mounts and all(v["read_only"] for v in mounts)
        assert services[name]["stop_grace_period"] == "30s"
    output = shell(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "-e",
        "WATT_SERVER_DOMAIN=synthetic.example.invalid",
        "-v",
        str(ROOT / "deploy/server/Caddyfile") + ":/etc/caddy/Caddyfile:ro",
        "caddy:2.10.2-alpine",
        "caddy",
        "validate",
        "--config",
        "/etc/caddy/Caddyfile",
    )
    assert "Valid configuration" in output
