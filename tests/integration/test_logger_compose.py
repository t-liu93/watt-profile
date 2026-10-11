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
from playwright.sync_api import expect, sync_playwright

pytestmark = pytest.mark.skipif(
    not any("tests/integration" in arg for arg in sys.argv[1:]),
    reason="Select tests/integration explicitly; never start or mutate services by default",
)
ROOT = Path(__file__).resolve().parents[2]
PREFIX = "/api/local/v1"
# A stop may drain a 10-second window; allow the 2-second UI poll plus scheduling margin.
DRAIN_TIMEOUT_MS = 15000


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


def test_complete_offline_compose_browser_walkthrough(compose):
    run, base = compose
    assert run("exec", "-T", "logger", "id", "-u").strip() == "10001"
    assert run("exec", "-T", "logger", "id", "-g").strip() == "10001"
    with httpx.Client(base_url=base, timeout=25, trust_env=False) as client:
        assert client.get("/health/ready").status_code == 200
        assert client.get(PREFIX + "/status").status_code == 401
    probe = (
        "import hashlib,json,sqlite3; from pathlib import Path; "
        "p=Path('/var/lib/watt-profile'); c=sqlite3.connect(p/'logger.sqlite3'); "
        "print(json.dumps({'token':hashlib.sha256((p/'upload-token').read_bytes()).hexdigest(),"
        "'records':c.execute('select record_id,canonical_snapshot,payload_sha256,upload_state "
        "from snapshots order by record_id').fetchall(),"
        "'mode':oct(p.stat().st_mode & 511),'uid':p.stat().st_uid}))"
    )

    def disk():
        return json.loads(run("exec", "-T", "logger", "python", "-c", probe))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            context = browser.new_context()
            context.route(
                "**/*",
                lambda route: (
                    route.continue_() if route.request.url.startswith(base + "/") else route.abort()
                ),
            )
            page = context.new_page()
            page.set_default_timeout(25000)

            def browser_login():
                page.goto(base + "/login")
                page.get_by_label("Username").fill("operator")
                page.get_by_label("Password").fill("testtesttesttest")
                page.get_by_role("button", name="Log in", exact=True).click()
                expect(page.get_by_role("heading", name="Watt Profile Logger")).to_be_visible()
                expect(page.locator("#synthetic")).to_be_visible()
                expect(
                    page.get_by_text(
                        "Upload not implemented in M2; records remain pending", exact=True
                    )
                ).to_be_visible()

            def section(name):
                page.get_by_role("button", name=name, exact=True).click()

            def status():
                response = page.request.get(base + PREFIX + "/status")
                assert response.status == 200
                return response.json()["data"]

            def command(name, expected="idle"):
                page.get_by_role("button", name=name, exact=True).click()
                timeout = DRAIN_TIMEOUT_MS if name == "Pause" else 5000
                expect(page.locator("#message")).to_have_text("Command completed.", timeout=timeout)
                expect(page.locator("#status")).to_contain_text(expected, timeout=timeout)

            def create_site(address):
                section("Site and settings")
                page.get_by_label("Address", exact=True).fill(address)
                page.get_by_role("button", name="Create / import site", exact=True).click()
                expect(page.locator("#site-select")).to_contain_text(address)
                return page.locator("#site-select").input_value()

            def deploy(confirm=False):
                page.get_by_role("button", name="Create paused deployment / switch site").click()
                if confirm:
                    expect(page.get_by_role("dialog")).to_be_visible()
                    page.get_by_role("button", name="Confirm", exact=True).click()
                expect(page.locator("#message")).to_have_text(
                    "Command completed.", timeout=DRAIN_TIMEOUT_MS if confirm else 5000
                )

            def reveal():
                section("Registration")
                page.get_by_role("button", name="Reveal registration information").click()
                expect(page.locator("#registration-text")).to_be_visible()
                value = page.locator("#registration-text").input_value()
                token = json.loads(value)["token"]
                assert token not in str(page.evaluate("Object.assign({}, localStorage)"))
                assert token not in page.url
                page.evaluate(
                    "Object.defineProperty(navigator, 'clipboard', "
                    "{value: undefined, configurable: true})"
                )
                page.get_by_role("button", name="Copy registration information").click()
                expect(page.locator("#copy-help")).to_contain_text("Clipboard unavailable")
                section("Collection")
                assert page.locator("#registration-text").input_value() == ""
                assert token not in json.dumps(status())
                assert token not in run("logs", "--no-color", "logger")
                return token

            def recreate():
                old = run("ps", "--quiet", "logger").strip()
                run("up", "-d", "--no-deps", "--force-recreate", "--wait", "logger")
                assert run("ps", "--quiet", "logger").strip() != old
                assert page.request.get(base + PREFIX + "/status").status == 401
                browser_login()

            browser_login()
            assert page.locator('script[src="/static/logger.js"]').count() == 1
            assert status()["logger"]["session_id"] is None, "Use a fresh disposable project"
            section("Reading preview")
            page.get_by_role("button", name="Read preview", exact=True).click()
            expect(page.locator("#reading-kind")).to_have_text("Read preview (not stored)")
            expect(page.locator("#metrics tr")).to_have_count(18)
            expect(page.locator("#metrics")).to_contain_text("kWh")
            expect(page.locator("#metrics")).to_contain_text("Unavailable")
            assert status()["logger"]["pending_count"] == 0
            site_a = create_site("Invented Compose A")
            deploy()
            owner_a = status()["logger"]
            section("Collection")
            command("Start / resume", "collecting")
            page.wait_for_timeout(25000)
            before = status()
            assert before["logger"]["pending_count"] >= 2
            assert before["active_session_latest_received_at"]
            assert before["upload_mode"] == "not_implemented"
            assert before["logger"]["last_successful_upload_at"] is None
            token = reveal()
            command("Pause")
            paused_count = status()["logger"]["pending_count"]
            page.wait_for_timeout(12000)
            assert status()["logger"]["pending_count"] == paused_count
            command("Start / resume", "collecting")
            assert status()["logger"]["session_id"] == owner_a["session_id"]
            assert status()["logger"]["source_id"] == owner_a["source_id"]
            committed = disk()
            assert committed["mode"] == "0o700" and committed["uid"] == 10001
            assert committed["records"]
            recreate()
            after = status()["logger"]
            for key in ("device_id", "site_id", "session_id", "source_id", "measurement_point_id"):
                assert after[key] == owner_a[key]
            wait_for(lambda: status()["latest_live_reading"])
            after_disk = disk()
            assert after_disk["token"] == committed["token"]
            records = {row[0]: row for row in after_disk["records"]}
            for row in committed["records"]:
                assert records[row[0]] == row
            assert reveal() == token
            command("Pause")
            paused_disk = disk()
            paused_count = status()["logger"]["pending_count"]
            recreate()
            assert status()["logger"]["collection_state"] == "idle"
            assert disk() == paused_disk
            page.wait_for_timeout(12000)
            assert status()["logger"]["pending_count"] == paused_count
            create_site("Invented Compose B")
            deploy(confirm=True)
            owner_b = status()["logger"]
            assert owner_b["session_id"] != owner_a["session_id"]
            assert owner_b["source_id"] != owner_a["source_id"]
            assert owner_b["collection_state"] == "idle"
            assert status()["active_session_latest_received_at"] is None
            assert status()["logger"]["pending_count"] == paused_count
            section("Collection")
            command("Start / resume", "collecting")
            wait_for(lambda: status()["logger"]["pending_count"] > paused_count)
            page.get_by_role("button", name="Complete session", exact=True).click()
            expect(page.get_by_role("dialog")).to_contain_text(owner_b["session_id"])
            page.get_by_role("button", name="Confirm", exact=True).click()
            expect(page.locator("#status")).to_contain_text("completed", timeout=DRAIN_TIMEOUT_MS)
            section("Site and settings")
            page.locator("#site-select").select_option(site_a)
            deploy()
            revisit = status()["logger"]
            assert revisit["site_id"] == site_a
            assert revisit["measurement_point_id"] == owner_a["measurement_point_id"]
            assert len({owner_a["session_id"], owner_b["session_id"], revisit["session_id"]}) == 3
            assert len({owner_a["source_id"], owner_b["source_id"], revisit["source_id"]}) == 3
            sites = page.request.get(base + PREFIX + "/sites").json()["data"]["items"]
            assert next(site for site in sites if site["site_id"] == site_a)["address"] == (
                "Invented Compose A"
            )
            section("Maintenance")
            expect(page.locator("#sessions tr")).to_have_count(3)
            assert all(not row.is_enabled() for row in page.locator("#sessions input").all())
            final_disk = disk()
            assert all(row[3] == "pending" for row in final_disk["records"])
            assert status()["logger"]["pending_count"] == len(final_disk["records"])
            # Online backup and offline restore use only this disposable container's /tmp.
            # No restored runtime is started, and private contents are never printed.
            run(
                "exec",
                "-T",
                "logger",
                "python",
                "-m",
                "watt_profile.cli.logger_admin",
                "backup",
                "--data-dir",
                "/var/lib/watt-profile",
                "--output",
                "/tmp/m2-backup",
            )
            run(
                "exec",
                "-T",
                "logger",
                "python",
                "-m",
                "watt_profile.cli.logger_admin",
                "restore",
                "--backup",
                "/tmp/m2-backup",
                "--data-dir",
                "/tmp/m2-restored",
            )
            verify = """
from pathlib import Path
from watt_profile.logger.identity import read_upload_token
from watt_profile.logger.storage.database import create_database
from watt_profile.logger.storage.repository import LoggerRepository
from sqlalchemy import text
original = Path('/var/lib/watt-profile')
restored = Path('/tmp/m2-restored')
a = create_database(original / 'logger.sqlite3')
b = create_database(restored / 'logger.sqlite3')
try:
    assert LoggerRepository(a).load_installation() == LoggerRepository(b).load_installation()
    assert read_upload_token(original) == read_upload_token(restored)
    with a.connect() as x, b.connect() as y:
        query = text('SELECT * FROM snapshots ORDER BY record_id')
        assert x.execute(query).all() == y.execute(query).all()
finally:
    a.dispose()
    b.dispose()
import shutil
shutil.rmtree('/tmp/m2-backup')
shutil.rmtree('/tmp/m2-restored')
"""
            run("exec", "-T", "logger", "python", "-c", verify)
            assert disk() == final_disk
            context.close()
        finally:
            browser.close()
