"""Opt-in disposable real container delivery acceptance; never ambient deployment URLs."""

from __future__ import annotations

import json
import os
import re
import shutil
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


@pytest.mark.parametrize("standalone", [False, True], ids=["development", "standalone"])
def test_disposable_server_build_readiness_and_persistence(tmp_path, standalone):
    project = os.environ.get("WATT_TEST_COMPOSE_PROJECT", "")
    tag = os.environ.get("WATT_DEV_IMAGE_TAG", "")
    assert os.environ.get("WATT_TEST_SERVER_COMPOSE") == "1", "Require explicit opt-in"
    assert re.fullmatch(r"watt-m3-[a-z0-9][a-z0-9_-]*", project)
    assert tag == project, "Require exact unique project/image tag"
    project += "-standalone" if standalone else "-dev"
    tag = project
    image = ("watt-profile-server:" if standalone else "watt-profile-server-dev:") + tag
    for kind in ("container", "network", "volume"):
        assert not shell(
            "docker",
            kind,
            "ls",
            *(["--all"] if kind == "container" else []),
            "-q",
            "--filter",
            "label=com.docker.compose.project=" + project,
        ).strip(), "Refuse pre-existing project resources"
    assert not shell("docker", "image", "ls", "-q", image).strip()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    private = tmp_path / "private"
    if standalone:
        private.mkdir(mode=0o700)
    env = dict(
        os.environ,
        WATT_DEV_IMAGE_TAG=tag,
        WATT_DEV_SERVER_PORT=str(port),
        WATT_SERVER_IMAGE_TAG=tag,
        WATT_SERVER_PUBLIC_ORIGIN=f"http://127.0.0.1:{port}",
        WATT_SERVER_COOKIE_SECURE="false",
        WATT_SERVER_BIND_ADDRESS="127.0.0.1",
        WATT_SERVER_PUBLISHED_PORT=str(port),
        WATT_SERVER_ADMIN_USER="admin",
        WATT_SERVER_DB_SECRET_DIR=str(private / "db"),
        WATT_SERVER_SECRET_DIR=str(private / "server"),
    )
    compose_file = "deploy/server/compose.yml" if standalone else "docker-compose.dev.yml"
    command = ["docker", "compose", "-p", project, "-f", str(ROOT / compose_file)]
    secret_helper = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        "0:0",
        "--label",
        "com.docker.compose.project=" + project,
        "-v",
        str(private) + ":/private",
        image,
        "python",
        "-c",
    ]
    secrets_prepared = False

    def run(*args, timeout=120):
        return shell(*command, *args, timeout=timeout, env=env)

    config = json.loads(run("config", "--format", "json"))
    services = config["services"]
    assert set(services["server"]["depends_on"]) == {"server-db"}
    if standalone:
        assert set(services) == {"server", "server-db"}
    else:
        assert set(services["server-db"]["depends_on"]) == {"server-init"}
    assert not services["server-db"].get("ports")
    assert services["server"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert all(not v.get("external") for v in config["volumes"].values())
    assert all(not s.get("devices") and not s.get("privileged") for s in services.values())
    try:
        if standalone:
            run("build", "server", timeout=600)
            secrets_prepared = True
            shell(
                *secret_helper,
                """
import os,secrets
from pathlib import Path
os.umask(0o077)
password=secrets.token_urlsafe(32)
for name,values in {
    'db': {'password': password},
    'server': {'database-url': 'postgresql+psycopg://watt:'+password+'@server-db:5432/watt',
               'admin-password': 'testtesttesttest'},
}.items():
    root=Path('/private')/name
    root.mkdir(mode=0o700)
    for filename,value in values.items():
        target=root/filename
        with target.open('x') as stream:
            stream.write(value+'\\n')
        target.chmod(0o600)
        if name=='server':
            os.chown(target,10001,10001)
    if name=='server':
        os.chown(root,10001,10001)
""",
            )
        run("up", "--build", "-d", "--wait", "--wait-timeout", "120", "server", timeout=600)
        actual = run("ps", "--all", "--services").splitlines()
        assert set(actual) == (
            {"server", "server-db"} if standalone else {"server", "server-db", "server-init"}
        )
        assert run("exec", "-T", "server", "id", "-u").strip() == "10001"
        assert run("exec", "-T", "server", "id", "-g").strip() == "10001"
        probe = """
import hashlib,json
from pathlib import Path
from importlib.resources import files
root=Path('/run/server-credentials' if str(standalone) else '/run/credentials')
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
        probe = probe.replace("str(standalone)", str(standalone))
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
            fingerprint_probe = """
import hashlib,json,os
from pathlib import Path
from sqlalchemy import create_engine,select
from watt_profile.server.storage.schema import metadata
engine=create_engine(Path(os.environ['WATT_SERVER_DATABASE_URL_FILE']).read_text().strip())
with engine.connect() as connection:
    rows={table.name:sorted([json.dumps(dict(row),sort_keys=True,default=str)
          for row in connection.execute(select(table)).mappings()])
          for table in metadata.sorted_tables}
print(hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest())
engine.dispose()
"""
            fingerprint = run("exec", "-T", "server", "python", "-c", fingerprint_probe)
            run("up", "-d", "--force-recreate", "--wait", "--wait-timeout", "120", "server")
            assert client.get(path).status_code == 401
            login()
            assert client.get(path).json() == before
            assert client.get(path + ".csv").content == csv
            assert run("exec", "-T", "server", "python", "-c", probe) == credentials
            assert run("exec", "-T", "server", "python", "-c", fingerprint_probe) == fingerprint
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
            assert run("exec", "-T", "server", "python", "-c", fingerprint_probe) == fingerprint
    finally:
        run("down", "--volumes", "--remove-orphans")
        if secrets_prepared:
            shell(
                *secret_helper,
                """
import os
from pathlib import Path
for root in Path('/private').iterdir():
    for target in root.iterdir():
        os.chown(target, int(os.environ['OWNER_UID']), int(os.environ['OWNER_GID']))
    os.chown(root, int(os.environ['OWNER_UID']), int(os.environ['OWNER_GID']))
""".replace("os.environ['OWNER_UID']", str(os.getuid())).replace(
                    "os.environ['OWNER_GID']", str(os.getgid())
                ),
            )
            shutil.rmtree(private)
        if shell("docker", "image", "ls", "-q", image).strip():
            shell("docker", "image", "rm", image)
        for kind in ("container", "network", "volume"):
            assert not shell(
                "docker",
                kind,
                "ls",
                *(["--all"] if kind == "container" else []),
                "-q",
                "--filter",
                "label=com.docker.compose.project=" + project,
            ).strip()
        assert not shell("docker", "image", "ls", "-q", image).strip()


@pytest.mark.parametrize(
    ("bind_address", "published_port"), [("127.0.0.1", "18081"), ("192.0.2.10", "28081")]
)
def test_standalone_one_port_configuration(tmp_path, bind_address, published_port):
    project = "watt-m3-config-" + uuid4().hex
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("WATT_SERVER_", "WATT_DEV_SERVER_PORT"))
    }
    env.update(
        WATT_SERVER_PUBLIC_ORIGIN="https://synthetic.example.invalid:8443",
        WATT_SERVER_ADMIN_USER="synthetic",
        WATT_SERVER_DB_SECRET_DIR=str(tmp_path / "db"),
        WATT_SERVER_SECRET_DIR=str(tmp_path / "server"),
    )
    if published_port != "18081":
        env.update(WATT_SERVER_BIND_ADDRESS=bind_address, WATT_SERVER_PUBLISHED_PORT=published_port)
    command = ["docker", "compose", "-p", project, "-f", str(ROOT / "deploy/server/compose.yml")]
    config = json.loads(shell(*command, "config", "--format", "json", env=env))
    services = config["services"]
    assert set(services) == {"server", "server-db"}
    assert set(config["volumes"]) == {"db-data"}
    assert not services["server-db"].get("ports")
    assert services["server"]["ports"] == [
        {
            "mode": "ingress",
            "host_ip": bind_address,
            "target": 8080,
            "published": published_port,
            "protocol": "tcp",
        }
    ]
    assert services["server"]["user"] == "10001:10001"
    assert (
        services["server"]["environment"]["WATT_SERVER_PUBLIC_ORIGIN"]
        == env["WATT_SERVER_PUBLIC_ORIGIN"]
    )
    assert services["server"]["environment"]["WATT_SERVER_COOKIE_SECURE"] == "true"
    assert all(not v.get("external") for v in config["volumes"].values())
    assert all(not s.get("devices") and not s.get("privileged") for s in services.values())
    for name in ("server", "server-db"):
        mounts = [v for v in services[name]["volumes"] if v["type"] == "bind"]
        assert mounts and all(v["read_only"] for v in mounts)
        assert services[name]["stop_grace_period"] == "30s"
    assert not (ROOT / "deploy/server/Caddyfile").exists()
    serialized = json.dumps(config).lower()
    assert all(word not in serialized for word in ("caddy", "certificate", "watt_server_domain"))
    missing_origin = dict(env)
    del missing_origin["WATT_SERVER_PUBLIC_ORIGIN"]
    with pytest.raises(subprocess.CalledProcessError):
        shell(*command, "config", "--format", "json", env=missing_origin)
    dev = json.loads(
        shell(
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            str(ROOT / "docker-compose.dev.yml"),
            "config",
            "--format",
            "json",
            env=env,
        )
    )
    assert dev["services"]["server"]["ports"][0]["published"] == "18081"
    assert dev["services"]["server"]["environment"]["WATT_SERVER_PUBLIC_ORIGIN"] == (
        "http://127.0.0.1:18081"
    )
