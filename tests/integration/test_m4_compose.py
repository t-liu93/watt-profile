"""Opt-in, self-owned combined development Compose smoke and routing acceptance."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    not any("tests/integration" in arg for arg in sys.argv[1:]),
    reason="Select tests/integration explicitly; never start services by default",
)


def docker(*args: str, timeout: int = 120) -> str:
    return subprocess.run(
        ["docker", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    ).stdout


def free_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def login_local(port: int):
    client = httpx.Client(timeout=20, trust_env=False)
    origin = f"http://127.0.0.1:{port}"
    page = client.get(origin + "/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
    assert (
        client.post(
            origin + "/login",
            data={"username": "operator", "password": "testtesttesttest", "csrf_token": csrf},
        ).status_code
        == 303
    )
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', client.get(origin + "/").text)[1]
    return client, origin + "/api/local/v1", {"X-CSRF-Token": csrf}


def login_server(port: int):
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=20, trust_env=False)
    page = client.get("/login")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    assert (
        client.post(
            "/login", data={"username": "admin", "password": "testtesttesttest", "csrf": csrf}
        ).status_code
        == 303
    )
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', client.get("/").text)[1]
    return client, {"X-CSRF-Token": csrf}


def wait_for(predicate, timeout: float, message: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    pytest.fail(message)


def test_combined_compose_routing_ports_and_owned_cleanup(tmp_path):
    project = os.environ.get("WATT_TEST_COMPOSE_PROJECT", "")
    tag = os.environ.get("WATT_DEV_IMAGE_TAG", "")
    assert os.environ.get("WATT_TEST_M4_COMPOSE") == "1", "Require explicit M4 Compose opt-in"
    assert re.fullmatch(r"watt-m4-[a-z0-9][a-z0-9_-]*", project)
    assert tag == project, "Use an exact unique application image tag"
    app_images = [
        f"watt-profile-logger-dev:{tag}",
        f"watt-profile-server-dev:{tag}",
    ]

    # Refuse every labeled project resource and exact image tag before creating resources.
    for kind in ("container", "network", "volume"):
        args = ["container", "ls", "--all"] if kind == "container" else [kind, "ls"]
        assert not docker(
            *args, "-q", "--filter", f"label=com.docker.compose.project={project}"
        ).strip()
    for image in app_images:
        inspected = subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, text=True, timeout=20
        )
        assert inspected.returncode != 0, f"Refuse pre-existing exact image {image}"

    logger_port = int(os.environ.get("WATT_DEV_PORT", "8080"))
    server_port = int(os.environ.get("WATT_DEV_SERVER_PORT", "18081"))
    assert logger_port != server_port
    # Bind both ports together before any Compose command can create resources.
    sockets = [socket.socket(), socket.socket()]
    try:
        for sock, port in zip(sockets, (logger_port, server_port), strict=True):
            sock.bind(("127.0.0.1", port))
        config = json.loads(
            docker(
                "compose",
                "-p",
                project,
                "-f",
                "docker-compose.dev.yml",
                "config",
                "--format",
                "json",
            )
        )
        logger = config["services"]["logger"]
        assert logger["environment"]["WATT_LOGGER_DEV_SERVER_ORIGIN"] == (
            f"http://127.0.0.1:{server_port}"
        )
        assert (
            logger["environment"]["WATT_LOGGER_DEV_SERVER_CONNECT_ORIGIN"] == "http://server:8080"
        )
        assert set(logger["depends_on"]) == {"logger-init"}
        assert logger["user"] == "10001:10001" and not logger.get("devices")
        assert not logger.get("privileged") and logger.get("network_mode") != "host"
        assert config["services"]["server"]["user"] == "10001:10001"
        assert all(not volume.get("external") for volume in config["volumes"].values())
        defaults_env = dict(os.environ)
        defaults_env.pop("WATT_DEV_PORT", None)
        defaults_env.pop("WATT_DEV_SERVER_PORT", None)
        defaults = json.loads(
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "-p",
                    project,
                    "-f",
                    "docker-compose.dev.yml",
                    "config",
                    "--format",
                    "json",
                ],
                cwd=ROOT,
                env=defaults_env,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
        )
        assert defaults["services"]["logger"]["environment"]["WATT_LOGGER_DEV_SERVER_ORIGIN"] == (
            "http://127.0.0.1:18081"
        )
        assert defaults["services"]["logger"]["ports"][0]["published"] == "8080"
        assert defaults["services"]["server"]["ports"][0]["published"] == "18081"
    finally:
        for sock in sockets:
            sock.close()

    tmp_path.chmod(0o700)
    env = dict(
        os.environ,
        WATT_DEV_PORT=str(logger_port),
        WATT_DEV_SERVER_PORT=str(server_port),
        WATT_DEV_IMAGE_TAG=tag,
        TMPDIR=str(tmp_path),
    )
    command = ["docker", "compose", "-p", project, "-f", "docker-compose.dev.yml"]
    try:
        subprocess.run(
            [
                *command,
                "up",
                "--build",
                "-d",
                "--wait",
                "--wait-timeout",
                "180",
                "logger",
                "server",
            ],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=240,
        )
        with httpx.Client(timeout=8, trust_env=False) as client:
            logger_status = client.get(f"http://127.0.0.1:{logger_port}/api/local/v1/status")
            assert logger_status.status_code == 401
            assert client.get(f"http://127.0.0.1:{logger_port}/health/ready").status_code == 200
            assert client.get(f"http://127.0.0.1:{server_port}/health/ready").status_code == 200
        # Exercise actual Logger login, offline collection, manual registration and routed upload.
        with httpx.Client(timeout=20, trust_env=False) as local:
            page = local.get(f"http://127.0.0.1:{logger_port}/login")
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
            assert (
                local.post(
                    f"http://127.0.0.1:{logger_port}/login",
                    data={
                        "username": "operator",
                        "password": "testtesttesttest",
                        "csrf_token": csrf,
                    },
                ).status_code
                == 303
            )
            csrf = re.search(
                r'name="csrf-token" content="([^"]+)"',
                local.get(f"http://127.0.0.1:{logger_port}/").text,
            )[1]
            local_headers = {"X-CSRF-Token": csrf}
            prefix = f"http://127.0.0.1:{logger_port}/api/local/v1"
            site = local.post(
                prefix + "/sites",
                json={"address": "M4 synthetic site", "description": "Compose acceptance"},
                headers=local_headers,
            )
            assert site.status_code == 200, site.text
            site_id = site.json()["data"]["site_id"]
            deployed = local.post(
                prefix + "/deployment",
                json={"site_id": site_id, "expected_session_id": None},
                headers=local_headers,
            )
            assert deployed.status_code == 200, deployed.text
            session_id = deployed.json()["data"]["logger"]["session_id"]
            assert (
                local.post(
                    prefix + f"/sessions/{session_id}/start", json={}, headers=local_headers
                ).status_code
                == 200
            )

            def logger_status():
                response = local.get(prefix + "/status")
                assert response.status_code == 200
                return response.json()["data"]

            deadline = time.monotonic() + 50
            while logger_status()["logger"]["pending_count"] < 2 and time.monotonic() < deadline:
                time.sleep(0.25)
            offline = logger_status()
            assert offline["logger"]["pending_count"] >= 2
            assert offline["upload_mode"] == "unconfigured"
            paused = local.post(
                prefix + f"/sessions/{session_id}/pause", json={}, headers=local_headers
            )
            assert paused.status_code == 200, paused.text
            summary = local.post(
                prefix + "/registration-summary",
                json={"site_id": site_id},
                headers=local_headers,
            ).json()["data"]

        with httpx.Client(
            base_url=f"http://127.0.0.1:{server_port}", timeout=20, trust_env=False
        ) as central:
            login_page = central.get("/login")
            csrf = re.search(r'name="csrf" value="([^"]+)"', login_page.text)[1]
            assert (
                central.post(
                    "/login",
                    data={"username": "admin", "password": "testtesttesttest", "csrf": csrf},
                ).status_code
                == 303
            )
            csrf = re.search(r'name="csrf-token" content="([^"]+)"', central.get("/").text)[1]
            admin_headers = {"X-CSRF-Token": csrf}
            preview = central.post(
                "/api/admin/v1/registrations/preview", json=summary, headers=admin_headers
            )
            assert preview.status_code == 200, preview.text
            assert (
                central.post(
                    "/api/admin/v1/registrations", json=summary, headers=admin_headers
                ).status_code
                == 200
            )

        # The browser-facing origin is saved explicitly; Compose only routes the matching origin.
        with httpx.Client(timeout=20, trust_env=False) as local:
            page = local.get(f"http://127.0.0.1:{logger_port}/login")
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
            local.post(
                f"http://127.0.0.1:{logger_port}/login",
                data={"username": "operator", "password": "testtesttesttest", "csrf_token": csrf},
            )
            csrf = re.search(
                r'name="csrf-token" content="([^"]+)"',
                local.get(f"http://127.0.0.1:{logger_port}/").text,
            )[1]
            prefix = f"http://127.0.0.1:{logger_port}/api/local/v1"
            saved = local.patch(
                prefix + "/config",
                json={
                    "expected_session_id": session_id,
                    "server_url": f"http://127.0.0.1:{server_port}",
                },
                headers={"X-CSRF-Token": csrf},
            )
            assert saved.status_code == 200, saved.text
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                status = local.get(prefix + "/status").json()["data"]
                if status["logger"]["pending_count"] == 0:
                    break
                time.sleep(0.25)
            assert status["logger"]["pending_count"] == 0
            assert status["logger"]["last_successful_upload_at"]
        with httpx.Client(
            base_url=f"http://127.0.0.1:{server_port}", timeout=20, trust_env=False
        ) as central:
            login_page = central.get("/login")
            csrf = re.search(r'name="csrf" value="([^"]+)"', login_page.text)[1]
            central.post(
                "/login",
                data={"username": "admin", "password": "testtesttesttest", "csrf": csrf},
            )
            readings = f"/api/admin/v1/sites/{site_id}/readings"
            params = {"device_id": summary["device_id"], "session_id": session_id}
            result = central.get(readings, params={**params, "limit": 1})
            assert result.status_code == 200 and result.json()["data"]["total"] >= 2
            exported = central.get(readings + ".csv", params=params)
            assert exported.status_code == 200
            rows = list(csv.DictReader(exported.text.splitlines()))
            assert len(rows) == result.json()["data"]["total"]
            assert all(row["session_id"] == session_id for row in rows)

        # Freeze only this test project's Server. Logger must keep sampling and retain data.
        before_outage = status["logger"]["pending_count"]
        docker("compose", "-p", project, "-f", "docker-compose.dev.yml", "pause", "server")
        server_paused = True
        try:
            local, prefix, local_headers = login_local(logger_port)
            assert (
                local.post(
                    prefix + f"/sessions/{session_id}/start", json={}, headers=local_headers
                ).status_code
                == 200
            )
            outage_status = wait_for(
                lambda: (
                    value
                    if (value := local.get(prefix + "/status").json()["data"])["logger"][
                        "pending_count"
                    ]
                    > before_outage
                    and value["upload"]["state"] == "sending"
                    else None
                ),
                35,
                "Logger did not keep collecting and enter an in-flight send during Server outage",
            )
            assert outage_status["logger"]["pending_count"] > before_outage
            assert outage_status["active_session_latest_received_at"]
            old_container = docker(*command[1:], "ps", "-q", "logger").strip()
            expected_token_digest = hashlib.sha256(summary["token"].encode()).hexdigest()
            docker("kill", "--signal", "SIGKILL", old_container)
            subprocess.run(
                [
                    *command,
                    "up",
                    "-d",
                    "--no-deps",
                    "--force-recreate",
                    "--wait",
                    "logger",
                ],
                cwd=ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=90,
            )
            new_container = docker(*command[1:], "ps", "-q", "logger").strip()
            assert new_container and new_container != old_container
        finally:
            if server_paused:
                docker(
                    "compose", "-p", project, "-f", "docker-compose.dev.yml", "unpause", "server"
                )
                server_paused = False

        local, prefix, local_headers = login_local(logger_port)
        recovered = wait_for(
            lambda: (
                value
                if (value := local.get(prefix + "/status").json()["data"])["logger"][
                    "pending_count"
                ]
                == 0
                else None
            ),
            45,
            "Pending records did not drain after Logger recreation and Server recovery",
        )
        assert recovered["logger"]["device_id"] == summary["device_id"]
        assert (
            recovered["logger"]["last_successful_upload_at"]
            >= status["logger"]["last_successful_upload_at"]
        )

        # A→B→A uses fresh session/source identities and retains A's site-bound point.
        original_a = recovered["logger"]
        site_b = local.post(
            prefix + "/sites",
            json={"address": "M4 synthetic site B", "description": "Ownership separation"},
            headers=local_headers,
        ).json()["data"]["site_id"]
        switched_b = local.post(
            prefix + "/deployment",
            json={
                "site_id": site_b,
                "expected_session_id": session_id,
                "confirm_switch": True,
            },
            headers=local_headers,
        )
        assert switched_b.status_code == 200, switched_b.text
        owner_b = switched_b.json()["data"]["logger"]
        assert owner_b["session_id"] != original_a["session_id"]
        assert owner_b["source_id"] != original_a["source_id"]
        summary_b = local.post(
            prefix + "/registration-summary", json={"site_id": site_b}, headers=local_headers
        ).json()["data"]
        central, admin_headers = login_server(server_port)
        assert (
            central.post(
                "/api/admin/v1/registrations", json=summary_b, headers=admin_headers
            ).status_code
            == 200
        )

        # Revoke B's grant, collect, and verify cleanup preserves its blocked pending rows.
        device_id = summary["device_id"]
        revoked = central.delete(
            f"/api/admin/v1/devices/{device_id}/sites/{site_b}", headers=admin_headers
        )
        assert revoked.status_code == 204
        assert (
            local.post(
                prefix + f"/sessions/{owner_b['session_id']}/start", json={}, headers=local_headers
            ).status_code
            == 200
        )
        wait_for(
            lambda: (
                value
                if (value := local.get(prefix + "/status").json()["data"])["logger"][
                    "pending_count"
                ]
                > 0
                else None
            ),
            35,
            "Logger did not save B readings with the revoked grant",
        )
        paused_b = local.post(
            prefix + f"/sessions/{owner_b['session_id']}/pause", json={}, headers=local_headers
        )
        assert paused_b.status_code == 200, paused_b.text
        assert (
            local.post(
                prefix + f"/sessions/{owner_b['session_id']}/complete",
                json={},
                headers=local_headers,
            ).status_code
            == 200
        )
        wait_for(
            lambda: (
                item
                if (
                    item := next(
                        (
                            session
                            for session in local.get(prefix + "/sessions").json()["data"]["items"]
                            if session["session_id"] == owner_b["session_id"]
                        ),
                        None,
                    )
                )
                and item["upload_error"] == "site_not_authorized"
                else None
            ),
            15,
            "Revoked B grant did not become a visible group block",
        )
        cleanup = local.post(
            prefix + "/cleanup",
            json={"session_ids": [owner_b["session_id"]], "confirm": True},
            headers=local_headers,
        )
        assert cleanup.status_code == 200 and cleanup.json()["data"]["retained_pending_count"] > 0
        restored_grant = central.put(
            f"/api/admin/v1/devices/{device_id}/sites/{site_b}",
            json={},
            headers=admin_headers,
        )
        assert restored_grant.status_code == 200
        assert (
            local.post(prefix + "/upload/retry", json={}, headers=local_headers).status_code == 202
        )
        wait_for(
            lambda: local.get(prefix + "/status").json()["data"]["logger"]["pending_count"] == 0,
            45,
            "Retry did not deliver B after restoring its grant",
        )

        switched_a = local.post(
            prefix + "/deployment",
            json={
                "site_id": site_id,
                "expected_session_id": owner_b["session_id"],
                "confirm_switch": True,
            },
            headers=local_headers,
        )
        assert switched_a.status_code == 200, switched_a.text
        returned_a = switched_a.json()["data"]["logger"]
        assert returned_a["session_id"] not in {original_a["session_id"], owner_b["session_id"]}
        assert returned_a["source_id"] not in {original_a["source_id"], owner_b["source_id"]}
        assert returned_a["measurement_point_id"] == original_a["measurement_point_id"]
        assert (
            local.post(
                prefix + f"/sessions/{returned_a['session_id']}/start",
                json={},
                headers=local_headers,
            ).status_code
            == 200
        )
        wait_for(
            lambda: (
                value
                if (value := local.get(prefix + "/status").json()["data"])["logger"][
                    "pending_count"
                ]
                > 0
                else None
            ),
            35,
            "Returned A session did not save new readings",
        )
        assert (
            local.post(
                prefix + f"/sessions/{returned_a['session_id']}/pause",
                json={},
                headers=local_headers,
            ).status_code
            == 200
        )
        wait_for(
            lambda: local.get(prefix + "/status").json()["data"]["logger"]["pending_count"] == 0,
            45,
            "Returned A session did not upload through its retained grant",
        )

        # Completed uploaded snapshots may be removed locally; Server history remains intact.
        server_history_before_cleanup = central.get(readings, params={**params, "limit": 1})
        assert server_history_before_cleanup.status_code == 200
        cleaned_a = local.post(
            prefix + "/cleanup",
            json={"session_ids": [session_id], "confirm": True},
            headers=local_headers,
        )
        assert cleaned_a.status_code == 200, cleaned_a.text
        assert cleaned_a.json()["data"]["deleted_count"] >= 2
        assert cleaned_a.json()["data"]["retained_pending_count"] == 0
        server_history = central.get(readings, params={**params, "limit": 1})
        assert server_history.status_code == 200
        assert (
            server_history.json()["data"]["total"]
            == (server_history_before_cleanup.json()["data"]["total"])
        )

        # Verify durable Token and checkpoint after process replacement without printing secrets.
        probe = (
            "import hashlib; from pathlib import Path; "
            "from watt_profile.logger.identity import read_upload_token; "
            "print(hashlib.sha256(read_upload_token(Path('/var/lib/watt-profile')).encode()).hexdigest())"
        )
        token_digest = docker("exec", new_container, "python", "-c", probe).strip()
        assert token_digest == expected_token_digest
        assert returned_a["device_id"] == original_a["device_id"]
    finally:
        down = subprocess.run(
            [*command, "down", "--volumes", "--remove-orphans", "--timeout", "30"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert down.returncode == 0, down.stderr
        for kind in ("container", "network", "volume"):
            args = ["container", "ls", "--all"] if kind == "container" else [kind, "ls"]
            assert not docker(
                *args, "-q", "--filter", f"label=com.docker.compose.project={project}"
            ).strip()
        for image in app_images:
            removed = subprocess.run(
                ["docker", "image", "rm", image], capture_output=True, text=True, timeout=30
            )
            if removed.returncode == 0:
                continue
            assert (
                subprocess.run(
                    ["docker", "image", "inspect", image],
                    capture_output=True,
                    text=True,
                    timeout=20,
                ).returncode
                != 0
            )
