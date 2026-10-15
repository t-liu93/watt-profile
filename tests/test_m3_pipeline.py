"""Real HTTP manual delivery from normal fake-serial Logger records to PostgreSQL."""

# Imported pytest fixture is resolved through the test argument.
# ruff: noqa: F811
import csv
import io
import json
import re
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from uuid import uuid4

import httpx
import uvicorn
from test_logger_runtime import Factory, FakeClock, start
from test_m2_pipeline import collect_window, stored_rows
from test_server_auth import server_settings  # noqa: F401

from watt_profile.contracts.domain import canonical_snapshot_bytes, payload_sha256
from watt_profile.contracts.upload import validate_confirmation_body
from watt_profile.logger.app import create_app as logger_app
from watt_profile.logger.deployment import DeploymentCommand
from watt_profile.logger.settings import LoggerSettings
from watt_profile.logger.storage.repository import LocalSettings
from watt_profile.logger.upload import TransportResult
from watt_profile.server.app import create_app as server_app
from watt_profile.server.export import CSV_COLUMNS
from watt_profile.server.storage.repository import ServerRepository


@contextmanager
def loopback(app_factory):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    app = app_factory(origin)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.01)
        with httpx.Client(base_url=origin, trust_env=False, timeout=10) as client:
            yield app, client, origin
    finally:
        server.should_exit = True
        thread.join(15)
        listener.close()
        assert not thread.is_alive()


def login(client, *, logger=False):
    csrf_name = "csrf_token" if logger else "csrf"
    page = client.get("/login")
    csrf = re.search(r'name="' + csrf_name + r'" value="([^"]+)"', page.text)[1]
    assert (
        client.post(
            "/login",
            data={
                "username": "operator" if logger else "admin",
                "password": "synthetic-password-long-enough",
                csrf_name: csrf,
            },
        ).status_code
        == 303
    )
    return {
        "X-CSRF-Token": re.search(r'name="csrf-token" content="([^"]+)"', client.get("/").text)[1]
    }


class ManualDeliveryTransport:
    """Keep the M3 manual-ingest fixture independent of automatic upload."""

    async def send(self, saved_origin, token, group):
        return TransportResult(0, error_code="upload_network_error")

    async def aclose(self):
        pass


def test_normal_logger_records_manual_http_ingest_query_csv_and_pending(
    server_engine, server_settings, tmp_path
):
    password = tmp_path / "logger-password"
    password.write_text("synthetic-password-long-enough")
    password.chmod(0o600)
    settings = LoggerSettings.from_env(
        {
            "WATT_LOGGER_DATA_DIR": str(tmp_path / "logger"),
            "WATT_LOGGER_OPERATOR_USER": "operator",
            "WATT_LOGGER_OPERATOR_PASSWORD_FILE": str(password),
        }
    )
    clock, factory = FakeClock(), Factory()
    with (
        loopback(lambda origin: server_app(replace(server_settings, public_origin=origin))) as (
            _,
            central,
            origin,
        ),
        loopback(
            lambda _: logger_app(
                settings,
                clock=clock,
                serial_factory=factory,
                upload_transport_factory=lambda _: ManualDeliveryTransport(),
            )
        ) as (
            local_app,
            local,
            _,
        ),
    ):
        headers, local_headers = login(central), login(local, logger=True)
        runtime, repo = local_app.state.runtime, local_app.state.repository
        # Manual delivery remains the subject of this fixture; M4 transport is injected.
        repo.save_settings(LocalSettings(server_url=origin, serial_path="/fake"))
        site = runtime.create_site(address="Synthetic pipeline\nA", description="Manual delivery")
        runtime.switch_site(DeploymentCommand(site_id=site.site_id, expected_session_id=None))
        port = start(runtime, factory)
        try:
            for _ in range(2):
                collect_window(runtime, repo, clock, port)
            runtime.pause(repo.load_lifecycle().session.descriptor.session_id)
            group = repo.select_pending_group()
            rows_before = stored_rows(repo.engine)
            assert len(group.batch.records) == repo.get_status().pending_count == 2
            assert repo.get_status().last_successful_upload_at is None
            summary_response = local.post(
                "/api/local/v1/registration-summary",
                json={"site_id": str(site.site_id)},
                headers=local_headers,
            )
            assert summary_response.status_code == 200
            summary = summary_response.json()["data"]
            upload_headers = {
                "Authorization": "Bearer " + summary["token"],
                "Content-Type": "application/json",
            }
            endpoint = "/api/v1/ingest/batches"
            assert (
                central.post(endpoint, content=group.body, headers=upload_headers).status_code
                == 401
            )
            preview = central.post(
                "/api/admin/v1/registrations/preview", json=summary, headers=headers
            )
            assert preview.status_code == 200 and summary["token"] not in preview.text
            registered = central.post("/api/admin/v1/registrations", json=summary, headers=headers)
            assert registered.status_code == 200
            credential = registered.json()["data"]["credential_id"]
            response = central.post(endpoint, content=group.body, headers=upload_headers)
            assert response.status_code == 200, response.text
            validate_confirmation_body(response.content, group.batch)
            assert central.post(endpoint, content=group.body, headers=upload_headers).content == (
                response.content
            )
            path = f"/api/admin/v1/sites/{site.site_id}/readings"
            filters = {
                "device_id": summary["device_id"],
                "session_id": str(group.batch.session.session_id),
            }
            page = central.get(path, params={**filters, "limit": 1}).json()["data"]
            assert page["total"] == 2 and len(page["items"]) == 1
            exported = central.get(path + ".csv", params=filters)
            assert exported.status_code == 200
            cells = list(csv.DictReader(io.StringIO(exported.text)))
            assert tuple(cells[0]) == CSV_COLUMNS and len(cells) == 2
            expected = {str(r.record_id): r for r in group.batch.records}
            assert {c["record_id"] for c in cells} == set(expected)
            for cell in cells:
                record = expected[cell["record_id"]]
                values = record.model_dump(mode="json")
                detail = central.get(path + f"/{record.device_id}/{record.record_id}")
                assert detail.json()["data"] == values
                for name, value in values["metrics"].items():
                    assert cell[name] == ("" if value is None else value)
                assert cell["voltage_l2_v"] == "" and cell["power_export_kw"] == "0.000"
                assert json.loads(cell["fields"]) == values["fields"]
                assert json.loads(cell["quality"]) == values["quality"]
                stored = ServerRepository(server_engine).get_snapshot(
                    record.device_id, record.record_id
                )
                assert stored.canonical == canonical_snapshot_bytes(record)
                assert stored.payload_sha256 == payload_sha256(record)
            assert summary["token"] not in exported.text
            assert (
                central.get(path, params={"session_id": str(uuid4())}).json()["data"]["total"] == 0
            )
            changed = group.batch.model_dump(mode="json")
            changed["records"][0]["metrics"]["power_export_kw"] = "0.0000"
            conflict = central.post(endpoint, json=changed, headers=upload_headers)
            assert (
                conflict.status_code == 409
                and conflict.json()["error"]["code"] == "record_conflict"
            )
            assert (
                central.delete(
                    f"/api/admin/v1/devices/{summary['device_id']}/sites/{site.site_id}",
                    headers=headers,
                ).status_code
                == 204
            )
            assert (
                central.post(endpoint, content=group.body, headers=upload_headers).status_code
                == 403
            )
            assert (
                central.post(
                    f"/api/admin/v1/devices/{summary['device_id']}/credentials/{credential}/disable",
                    json={},
                    headers=headers,
                ).status_code
                == 200
            )
            assert (
                central.post(endpoint, content=group.body, headers=upload_headers).status_code
                == 401
            )
            assert central.get(path + ".csv", params=filters).content == exported.content
            # Manual submission never acknowledges local records or mutates the local database.
            clock.advance(30)
            assert stored_rows(repo.engine) == rows_before
            assert repo.select_pending_group() == group
            assert repo.get_status().pending_count == 2
            assert repo.get_status().last_successful_upload_at is None
        finally:
            clock.advance(10)
