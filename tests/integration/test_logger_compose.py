"""Explicit acceptance attached only to a caller-created disposable Compose project."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import sync_playwright

pytestmark = pytest.mark.skipif(
    not any("tests/integration" in arg for arg in sys.argv[1:]),
    reason="Select tests/integration explicitly; never start or mutate services by default",
)
ROOT = Path(__file__).resolve().parents[2]
PREFIX = "/api/local/v1"


@pytest.fixture
def compose():
    project = os.environ.get("WATT_TEST_COMPOSE_PROJECT", "")
    if not re.fullmatch(r"watt-m2-[a-z0-9][a-z0-9_-]*", project):
        pytest.fail("Require explicit nonempty disposable WATT_TEST_COMPOSE_PROJECT=watt-m2-...")
    command = ["docker", "compose", "-p", project, "-f", str(ROOT / "docker-compose.dev.yml")]

    def run(*args):
        return subprocess.run(
            [*command, *args], cwd=ROOT, check=True, capture_output=True, text=True, timeout=120
        ).stdout

    configuration = json.loads(run("config", "--format", "json"))
    assert set(configuration["services"]) == {"logger", "logger-init"}
    logger = configuration["services"]["logger"]
    assert logger["environment"]["WATT_LOGGER_INPUT"] == "synthetic"
    assert logger["user"] == "10001:10001"
    assert not logger.get("devices") and not logger.get("privileged")
    assert logger.get("network_mode") != "host"
    assert all(not item.get("external") for item in configuration["volumes"].values())
    port = logger["ports"][0]
    assert port["host_ip"] == "127.0.0.1"
    for service in ("logger", "logger-init"):
        identifier = run("ps", "--all", "--quiet", service).strip()
        assert identifier, "Start the explicit development project with up --build -d --wait first"
        inspect = json.loads(
            subprocess.run(
                ["docker", "inspect", identifier],
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            ).stdout
        )[0]
        assert inspect["Config"]["Labels"]["com.docker.compose.project"] == project
    return run, f"http://127.0.0.1:{port['published']}"


def login(client):
    page = client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
    response = client.post(
        "/login",
        data={"username": "operator", "password": "testtesttesttest", "csrf_token": csrf},
    )
    assert response.status_code == 303
    page = client.get("/")
    return {"X-CSRF-Token": re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]}


def wait_for(check, seconds=35):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.2)
    pytest.fail("Timed out waiting for synthetic collection/readiness")


def test_build_readiness_browser_nonroot_and_recreation_persistence(compose):
    run, base = compose
    assert run("exec", "-T", "logger", "id", "-u").strip() == "10001"
    assert run("exec", "-T", "logger", "id", "-g").strip() == "10001"
    with httpx.Client(base_url=base, timeout=25, trust_env=False) as client:
        assert client.get("/health/ready").status_code == 200
        assert client.get(PREFIX + "/status").status_code == 401
        csrf = login(client)
        preview = client.post(PREFIX + "/preview", json={}, headers=csrf)
        assert preview.status_code == 200
        assert preview.json()["data"]["metrics"]["power_import_kw"] == "1.200"
        status = client.get(PREFIX + "/status").json()["data"]
        assert status["logger"]["session_id"] is None, "Use a fresh disposable project"
        site = client.post(
            PREFIX + "/sites", json={"address": "Synthetic container site"}, headers=csrf
        )
        assert site.status_code == 200
        response = client.post(
            PREFIX + "/deployment",
            json={
                "site_id": site.json()["data"]["site_id"],
                "expected_session_id": None,
                "confirm_switch": False,
            },
            headers=csrf,
        )
        assert response.status_code == 200
        owner = response.json()["data"]["logger"]
        sid = owner["session_id"]
        assert (
            client.post(PREFIX + f"/sessions/{sid}/start", json={}, headers=csrf).status_code == 200
        )
        before = wait_for(
            lambda: (
                state
                if (state := client.get(PREFIX + "/status").json()["data"])["logger"][
                    "pending_count"
                ]
                else None
            )
        )
        assert before["input_mode"] == "synthetic"
        assert before["upload_mode"] == "not_implemented"
        assert before["logger"]["last_successful_upload_at"] is None
    # Exercise installed templates/static with an actual browser in this container.
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(base + "/login")
            page.locator('[name="username"]').fill("operator")
            page.locator('[name="password"]').fill("testtesttesttest")
            page.locator('button[type="submit"]').click()
            page.wait_for_url(base + "/")
            assert page.locator('meta[name="csrf-token"]').get_attribute("content")
            assert page.locator('script[src="/static/logger.js"]').count() == 1
        finally:
            browser.close()
    # Compare durable identities and token digest; never emit credential bytes.
    probe = (
        "import hashlib,json,sqlite3; from pathlib import Path; "
        "p=Path('/var/lib/watt-profile'); c=sqlite3.connect(p/'logger.sqlite3'); "
        "print(json.dumps({'token':hashlib.sha256((p/'upload-token').read_bytes()).hexdigest(),"
        "'records':c.execute('select record_id,canonical_snapshot "
        "from snapshots order by record_id')"
        ".fetchall(),'mode':oct(p.stat().st_mode & 511),'uid':p.stat().st_uid}))"
    )
    persisted = json.loads(run("exec", "-T", "logger", "python", "-c", probe))
    assert persisted["mode"] == "0o700" and persisted["uid"] == 10001
    assert persisted["records"]
    old_container = run("ps", "--quiet", "logger").strip()
    run("up", "-d", "--no-deps", "--force-recreate", "--wait", "logger")
    assert run("ps", "--quiet", "logger").strip() != old_container
    after_disk = json.loads(run("exec", "-T", "logger", "python", "-c", probe))
    assert after_disk["token"] == persisted["token"]
    records = dict(after_disk["records"])
    for record, canonical in persisted["records"]:
        assert records[record] == canonical
    with httpx.Client(base_url=base, timeout=25, trust_env=False) as client:
        login(client)
        after = client.get(PREFIX + "/status").json()["data"]["logger"]
        for key in ("device_id", "site_id", "session_id", "source_id", "measurement_point_id"):
            assert after[key] == owner[key]
        assert after["pending_count"] >= before["logger"]["pending_count"]
        wait_for(lambda: client.get(PREFIX + "/status").json()["data"]["latest_live_reading"])
