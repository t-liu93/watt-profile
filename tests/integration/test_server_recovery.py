"""Real HTTP demo and custom-format backup into a newly created isolated database."""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import pytest
from test_server_compose import ROOT, shell

from watt_profile.cli.server_demo import DemoError, ingest, prepare
from watt_profile.server.storage.schema import metadata

pytestmark = pytest.mark.skipif(
    not any("tests/integration" in arg for arg in sys.argv[1:]),
    reason="Select integration tests explicitly",
)


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def login(client):
    page = client.get("/login")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    assert (
        client.post(
            "/login", data={"username": "admin", "password": "testtesttesttest", "csrf": csrf}
        ).status_code
        == 303
    )
    return {
        "X-CSRF-Token": re.search(r'name="csrf-token" content="([^"]+)"', client.get("/").text)[1]
    }


def test_private_demo_and_new_database_recovery(tmp_path):
    assert os.environ.get("WATT_TEST_SERVER_COMPOSE") == "1", "Explicit opt-in required"
    assert not os.environ.get("WATT_SERVER_DATABASE_URL_FILE") and not os.environ.get(
        "WATT_SERVER_DATABASE_URL"
    )
    project, tag = (
        os.environ.get("WATT_TEST_COMPOSE_PROJECT", ""),
        os.environ.get("WATT_DEV_IMAGE_TAG", ""),
    )
    assert re.fullmatch(r"watt-m3-[a-z0-9][a-z0-9_-]*", project) and tag == project
    image = "watt-profile-server-dev:" + tag
    for kind in ("container", "network", "volume"):
        assert not shell(
            "docker", kind, "ls", "-q", "--filter", "label=com.docker.compose.project=" + project
        ).strip()
    assert not shell("docker", "image", "ls", "-q", image).strip()
    port, restored_port = free_port(), free_port()
    env = dict(os.environ, WATT_DEV_SERVER_PORT=str(port))
    command = ["docker", "compose", "-p", project, "-f", str(ROOT / "docker-compose.dev.yml")]

    def run(*args, timeout=120):
        return shell(*command, *args, env=env, timeout=timeout)

    database, container = "restore_" + uuid4().hex, project + "-restored"
    database_created = container_created = False
    demo = tmp_path / "demo"
    prepare(demo)
    summaries = {
        label: json.loads((demo / f"registration-{label}.json").read_text()) for label in ("A", "B")
    }

    def sql(database_name, statement):
        return run(
            "exec",
            "-T",
            "server-db",
            "psql",
            "-U",
            "watt",
            "-d",
            database_name,
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            statement,
        ).strip()

    def state(database_name):
        return {
            table.name: sql(
                database_name,
                "SELECT coalesce(jsonb_agg(t ORDER BY t::text), '[]'::jsonb) "
                f"FROM (SELECT to_jsonb(x) AS t FROM {table.name} x) y",
            )
            for table in metadata.sorted_tables
        }

    try:
        run("up", "--build", "-d", "--wait", "--wait-timeout", "120", "server", timeout=600)
        origin = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=origin, trust_env=False, timeout=20) as client:
            headers = login(client)
            with pytest.raises(DemoError, match="401"):
                ingest(demo, origin)
            registration = client.post(
                "/api/admin/v1/registrations", json=summaries["A"], headers=headers
            )
            assert registration.status_code == 200
            credential_id = registration.json()["data"]["credential_id"]
            assert ingest(demo, origin, site="A") == 4
            with pytest.raises(DemoError, match="403"):
                ingest(demo, origin, site="B")
            assert (
                client.post(
                    "/api/admin/v1/registrations", json=summaries["B"], headers=headers
                ).status_code
                == 200
            )
            assert ingest(demo, origin) == 6
            before = {
                label: client.get(f"/api/admin/v1/sites/{summary['site_id']}/readings").json()
                for label, summary in summaries.items()
            }
            assert [before[label]["data"]["total"] for label in ("A", "B")] == [4, 2]
            assert ingest(demo, origin) == 6
            assert before == {
                label: client.get(f"/api/admin/v1/sites/{summary['site_id']}/readings").json()
                for label, summary in summaries.items()
            }
            # Runtime CLI uses the existing public-origin env for its Compose Host header.
            run(
                "exec",
                "-T",
                "server",
                "python",
                "-m",
                "watt_profile.cli.server_demo",
                "prepare",
                "--output",
                "/tmp/synthetic-demo",
            )
            run("cp", str(demo) + "/.", "server:/tmp/synthetic-demo")
            run(
                "exec",
                "-T",
                "--user",
                "0:0",
                "server",
                "python",
                "-c",
                "import os; from pathlib import Path; "
                "[os.chown(p,10001,10001) for p in Path('/tmp/synthetic-demo').iterdir()]",
            )
            run(
                "exec",
                "-T",
                "server",
                "python",
                "-m",
                "watt_profile.cli.server_demo",
                "ingest",
                "--directory",
                "/tmp/synthetic-demo",
                "--server-origin",
                "http://server:8080",
                "--allow-development-network",
            )
            assert client.get("/health/ready", headers={"Host": "server:8080"}).status_code == 403
            a = summaries["A"]
            assert (
                client.delete(
                    f"/api/admin/v1/devices/{a['device_id']}/sites/{a['site_id']}", headers=headers
                ).status_code
                == 204
            )
            with pytest.raises(DemoError, match="403"):
                ingest(demo, origin, site="A")
            assert ingest(demo, origin, site="B") == 2
            assert (
                client.post(
                    f"/api/admin/v1/devices/{a['device_id']}/credentials/{credential_id}/disable",
                    json={},
                    headers=headers,
                ).status_code
                == 200
            )
            with pytest.raises(DemoError, match="401"):
                ingest(demo, origin, site="B")
            # Keep an independently enabled credential in the backup.
            enabled_demo = tmp_path / "enabled-demo"
            prepare(enabled_demo)
            enabled = json.loads((enabled_demo / "registration-A.json").read_text())
            assert (
                client.post(
                    "/api/admin/v1/registrations", json=enabled, headers=headers
                ).status_code
                == 200
            )
            histories = {
                label: client.get(f"/api/admin/v1/sites/{s['site_id']}/readings").json()
                for label, s in summaries.items()
            }
            csv = {
                label: client.get(f"/api/admin/v1/sites/{s['site_id']}/readings.csv").content
                for label, s in summaries.items()
            }
            source_state = state("watt")
            backup_dir = tmp_path / "backup"
            backup_dir.mkdir(mode=0o700)
            backup = backup_dir / "server.dump"
            with backup.open("xb") as output:
                backup.chmod(0o600)
                subprocess.run(
                    [
                        *command,
                        "exec",
                        "-T",
                        "server-db",
                        "pg_dump",
                        "-U",
                        "watt",
                        "-d",
                        "watt",
                        "--format=custom",
                    ],
                    env=env,
                    cwd=ROOT,
                    stdout=output,
                    stderr=subprocess.PIPE,
                    check=True,
                    timeout=60,
                )
            with pytest.raises(FileExistsError):
                backup.open("xb")
            # createdb without --if-exists/cleanup refuses existing databases; never drop/reuse one.
            run("exec", "-T", "server-db", "createdb", "-U", "watt", database)
            database_created = True
            with pytest.raises(subprocess.CalledProcessError):
                run("exec", "-T", "server-db", "createdb", "-U", "watt", database)
            assert sql(database, "SELECT count(*) FROM pg_tables WHERE schemaname='public'") == "0"
            with backup.open("rb") as source:
                subprocess.run(
                    [
                        *command,
                        "exec",
                        "-T",
                        "server-db",
                        "pg_restore",
                        "-U",
                        "watt",
                        "-d",
                        database,
                        "--exit-on-error",
                        "--no-owner",
                        "--no-acl",
                    ],
                    env=env,
                    cwd=ROOT,
                    stdin=source,
                    capture_output=True,
                    check=True,
                    timeout=60,
                )
            assert state(database) == source_state
            config = tmp_path / "restored-config"
            config.mkdir(mode=0o700)
            # Obtain source URL privately; write independent config with the newly created DB only.
            url = (
                run(
                    "exec",
                    "-T",
                    "server",
                    "python",
                    "-c",
                    "from pathlib import Path; "
                    "print(Path('/run/credentials/database-url').read_text().strip())",
                )
                .strip()
                .rsplit("/", 1)[0]
                + "/"
                + database
            )
            for filename, value in (("database-url", url), ("admin-password", "testtesttesttest")):
                with (config / filename).open("x") as output:
                    (config / filename).chmod(0o600)
                    output.write(value + "\n")
            # Bind files private/read-only and owned by the app UID, independent of source volumes.
            os.chmod(config, 0o700)
            shell(
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "0:0",
                "-v",
                str(config) + ":/config",
                image,
                "python",
                "-c",
                "import os; from pathlib import Path; os.chown('/config',10001,10001); "
                "[os.chown(p,10001,10001) for p in Path('/config').iterdir()]",
            )
            shell(
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "--label",
                "com.docker.compose.project=" + project,
                "--network",
                project + "_default",
                "-p",
                f"127.0.0.1:{restored_port}:8080",
                "-v",
                str(config) + ":/run/credentials:ro",
                "-e",
                "WATT_SERVER_DATABASE_URL_FILE=/run/credentials/database-url",
                "-e",
                "WATT_SERVER_ADMIN_PASSWORD_FILE=/run/credentials/admin-password",
                "-e",
                "WATT_SERVER_ADMIN_USER=admin",
                "-e",
                f"WATT_SERVER_PUBLIC_ORIGIN=http://127.0.0.1:{restored_port}",
                "-e",
                "WATT_SERVER_COOKIE_SECURE=false",
                image,
            )
            container_created = True
            with httpx.Client(
                base_url=f"http://127.0.0.1:{restored_port}", trust_env=False, timeout=10
            ) as restored:
                for _ in range(120):
                    try:
                        if restored.get("/health/ready").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.25)
                else:
                    pytest.fail("restored server did not become ready")
                login(restored)
                for label, summary in summaries.items():
                    path = f"/api/admin/v1/sites/{summary['site_id']}/readings"
                    assert restored.get(path).json() == histories[label]
                    assert restored.get(path + ".csv").content == csv[label]
                with pytest.raises(DemoError, match="401"):
                    ingest(demo, f"http://127.0.0.1:{restored_port}")
                payload = json.loads((demo / "batch-1-A.json").read_text())
                response = restored.post(
                    "/api/v1/ingest/batches",
                    json=payload,
                    headers={"Authorization": "Bearer " + enabled["token"]},
                )
                assert (
                    response.status_code == 403
                    and response.json()["error"]["code"] == "device_mismatch"
                )
            assert state("watt") == source_state
    finally:
        if container_created:
            shell("docker", "rm", "-f", container)
        if database_created:
            run("exec", "-T", "server-db", "dropdb", "-U", "watt", database)
        # Return ownership so pytest can remove only this run's private temp files.
        config = tmp_path / "restored-config"
        if config.exists():
            shell(
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "0:0",
                "-v",
                str(config) + ":/config",
                image,
                "python",
                "-c",
                "import os; from pathlib import Path; "
                f"os.chown('/config',{os.getuid()},{os.getgid()}); "
                f"[os.chown(p,{os.getuid()},{os.getgid()}) for p in Path('/config').iterdir()]",
            )
        run("down", "--volumes", "--remove-orphans")
        if shell("docker", "image", "ls", "-q", image).strip():
            shell("docker", "image", "rm", image)
        for kind in ("container", "network", "volume"):
            assert not shell(
                "docker",
                kind,
                "ls",
                "-q",
                "--filter",
                "label=com.docker.compose.project=" + project,
            ).strip()
        assert not shell("docker", "image", "ls", "-q", image).strip()

        for name in ("demo", "enabled-demo", "backup", "restored-config"):
            target = tmp_path / name
            if target.exists():
                shutil.rmtree(target)
