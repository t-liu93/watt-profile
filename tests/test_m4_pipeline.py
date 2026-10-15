"""Real asynchronous Logger-to-Server delivery and query fidelity."""

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
from uuid import UUID, uuid4

import httpx
import pytest
import uvicorn
from sqlalchemy import event, text
from test_logger_runtime import Factory, FakeClock, start
from test_logger_storage import NOW, record
from test_m2_pipeline import collect_window
from test_server_auth import server_settings  # noqa: F401

from watt_profile.contracts.domain import canonical_snapshot_bytes, payload_sha256
from watt_profile.contracts.lifecycle import new_deployment, start_collection
from watt_profile.contracts.upload import UploadConfirmation, validate_confirmation_body
from watt_profile.logger.app import create_app as logger_app
from watt_profile.logger.deployment import DeploymentCommand
from watt_profile.logger.identity import load_or_initialize_installation, read_upload_token
from watt_profile.logger.maintenance import backup, restore
from watt_profile.logger.settings import LoggerSettings
from watt_profile.logger.storage.database import create_database, migrate
from watt_profile.logger.storage.repository import LocalSettings, LoggerRepository, Site
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


class DropNextCommittedResponse:
    """Test-owned ASGI fault: let Server commit, then cut one successful reply."""

    def __init__(self, app):
        self.app = app
        self.drop_next = False

    async def __call__(self, scope, receive, send):
        if not (
            self.drop_next
            and scope.get("type") == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/api/v1/ingest/batches"
        ):
            await self.app(scope, receive, send)
            return
        messages = []

        async def collect(message):
            messages.append(message)

        await self.app(scope, receive, collect)
        status = next(
            message["status"] for message in messages if message["type"] == "http.response.start"
        )
        if status == 200:
            self.drop_next = False
            raise OSError("injected post-commit response loss")
        for message in messages:
            await send(message)


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


def test_offline_registration_automatic_http_upload_query_csv_and_checkpoint(
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
        loopback(
            lambda origin: DropNextCommittedResponse(
                server_app(replace(server_settings, public_origin=origin))
            )
        ) as (
            fault_server,
            central,
            origin,
        ),
        loopback(lambda _: logger_app(settings, clock=clock, serial_factory=factory)) as (
            local_app,
            local,
            _,
        ),
    ):
        headers, local_headers = login(central), login(local, logger=True)
        runtime, repo = local_app.state.runtime, local_app.state.repository
        # Collect with no Server origin to prove the offline path retains records.
        repo.save_settings(LocalSettings(server_url=None, serial_path="/fake"))
        site = runtime.create_site(
            address="Synthetic pipeline\nA", description="Automatic delivery"
        )
        runtime.switch_site(DeploymentCommand(site_id=site.site_id, expected_session_id=None))
        port = start(runtime, factory)
        try:
            for _ in range(2):
                collect_window(runtime, repo, clock, port)
            runtime.pause(repo.load_lifecycle().session.descriptor.session_id)
            group = repo.select_pending_group()
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
            assert repo.get_status().pending_count == 2
            assert repo.get_status().last_successful_upload_at is None
            repo.save_settings(LocalSettings(server_url=origin, serial_path="/fake"))
            local_app.state.loop.call_soon_threadsafe(local_app.state.upload_scheduler.wake)
            deadline = time.monotonic() + 8
            while (
                local_app.state.upload_scheduler.observation.global_error != "invalid_credential"
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            assert local_app.state.upload_scheduler.observation.global_error == "invalid_credential"
            # Collection remains live while the remote credential is blocked.
            port = start(runtime, factory)
            collect_window(runtime, repo, clock, port)
            runtime.pause(repo.load_lifecycle().session.descriptor.session_id)
            group = repo.select_pending_group()
            assert len(group.batch.records) == repo.get_status().pending_count == 3
            preview = central.post(
                "/api/admin/v1/registrations/preview", json=summary, headers=headers
            )
            assert preview.status_code == 200 and summary["token"] not in preview.text
            registered = central.post("/api/admin/v1/registrations", json=summary, headers=headers)
            assert registered.status_code == 200
            credential = registered.json()["data"]["credential_id"]
            fault_server.drop_next = True
            retry = local.post("/api/local/v1/upload/retry", json={}, headers=local_headers)
            assert retry.status_code == 202
            deadline = time.monotonic() + 8
            server_repo = ServerRepository(server_engine)
            while (
                fault_server.drop_next
                or local_app.state.upload_scheduler.observation.state != "backoff"
            ) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert not fault_server.drop_next
            assert repo.get_status().pending_count == 3
            assert all(
                server_repo.get_snapshot(record.device_id, record.record_id) is not None
                for record in group.batch.records
            )
            # The first reply was cut after commit; explicit Retry safely resends that identity.
            retry = local.post("/api/local/v1/upload/retry", json={}, headers=local_headers)
            assert retry.status_code == 202
            deadline = time.monotonic() + 8
            while repo.get_status().pending_count and time.monotonic() < deadline:
                time.sleep(0.02)
            assert repo.get_status().pending_count == 0
            assert repo.get_status().last_successful_upload_at is not None
            response = central.post(endpoint, content=group.body, headers=upload_headers)
            assert response.status_code == 200, response.text
            validate_confirmation_body(response.content, group.batch)
            assert (
                central.post(endpoint, content=group.body, headers=upload_headers).content
                == response.content
            )
            path = f"/api/admin/v1/sites/{site.site_id}/readings"
            filters = {
                "device_id": summary["device_id"],
                "session_id": str(group.batch.session.session_id),
            }
            page = central.get(path, params={**filters, "limit": 1}).json()["data"]
            assert page["total"] == 3 and len(page["items"]) == 1
            exported = central.get(path + ".csv", params=filters)
            assert exported.status_code == 200
            cells = list(csv.DictReader(io.StringIO(exported.text)))
            assert tuple(cells[0]) == CSV_COLUMNS and len(cells) == 3
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
                stored = server_repo.get_snapshot(record.device_id, record.record_id)
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
            assert central.get(path + ".csv", params=filters).content == exported.content
            assert repo.get_status().pending_count == 0
            assert repo.get_status().last_successful_upload_at is not None
            expected_checkpoint = repo.get_status().last_successful_upload_at
            assert repo.select_pending_group() is None
        finally:
            clock.advance(10)

    # A Logger-only process recreation preserves identity, Token and the durable checkpoint.
    with loopback(lambda _: logger_app(settings, clock=clock, serial_factory=Factory())) as (
        restarted_app,
        _,
        _,
    ):
        restarted = restarted_app.state.repository
        assert restarted.load_installation().device_id == UUID(summary["device_id"])
        assert read_upload_token(settings.data_dir) == summary["token"]
        assert restarted.get_status().last_successful_upload_at == expected_checkpoint
        assert restarted.get_status().pending_count == 0


@pytest.mark.parametrize("saved_origin", [False, True], ids=["first-origin", "saved-origin"])
def test_existing_logger_installation_upgrade_confirmation_backup_restore(
    tmp_path, saved_origin, monkeypatch
):
    """M4 startup uploads an existing M2 backlog without replacing installation state."""
    data_dir = tmp_path / "old-installation"
    data_dir.mkdir(mode=0o700)
    engine = create_database(data_dir / "logger.sqlite3")
    password_file = tmp_path / "operator-password"
    password_file.write_text("synthetic-password-long-enough", encoding="utf-8")
    password_file.chmod(0o600)
    try:
        migrate(engine)  # Existing M2/M3 Logger schema; no uploader-owned migration.
        repo = LoggerRepository(engine)
        load_or_initialize_installation(repo, data_dir, NOW)
        saved_token = read_upload_token(data_dir)
        site = repo.save_site(Site(site_id=uuid4(), address="Synthetic prior installation"))
        repo.commit_deployment_change(
            None, new_deployment(repo.load_lifecycle(), site.site_id, NOW).lifecycle
        )
        lifecycle = repo.load_lifecycle()
        session_id = lifecycle.session.descriptor.session_id
        repo.commit_deployment_change(session_id, start_collection(lifecycle, session_id).lifecycle)
        if saved_origin:
            repo.save_settings(LocalSettings(server_url="https://upload.invalid"))
        snapshot = record(repo)
        repo.insert_snapshot(snapshot)
        group = repo.select_pending_group()
        before = repo.load_installation()
        engine.dispose()

        class ConfirmationTransport:
            def __init__(self):
                self.started = threading.Event()
                self.release = threading.Event()

            async def send(self, origin, token, pending):
                assert origin == "https://upload.invalid"
                assert token == saved_token
                assert pending.batch.records == group.batch.records
                self.started.set()
                await asyncio.to_thread(self.release.wait, 10)
                confirmation = UploadConfirmation(
                    schema_version=1,
                    device_id=pending.batch.device_id,
                    confirmed_record_ids=tuple(item.record_id for item in pending.batch.records),
                )
                return TransportResult(status_code=200, confirmation=confirmation)

            async def aclose(self):
                return None

        import asyncio

        transport = ConfirmationTransport()
        config = LoggerSettings(
            data_dir=data_dir,
            host="127.0.0.1",
            port=0,
            input_mode="synthetic",
            operator_user="operator",
            operator_password_file=password_file,
            cookie_secure=False,
        )
        saved_rows = None
        with loopback(
            lambda _: logger_app(config, upload_transport_factory=lambda _: transport)
        ) as (app, client, _):
            repository = app.state.repository
            if saved_origin:
                assert transport.started.wait(5)
            else:
                assert not transport.started.wait(0.1)
            current = repository.load_installation()
            assert current.device_id == before.device_id
            assert current.lifecycle.session.descriptor == before.lifecycle.session.descriptor
            assert current.lifecycle.session.collection_enabled
            assert read_upload_token(data_dir) == saved_token
            assert repository.select_pending_group() == group

            if not saved_origin:
                headers = login(client, logger=True)
                response = client.patch(
                    "/api/local/v1/config",
                    json={
                        "expected_session_id": str(session_id),
                        "server_url": "https://upload.invalid",
                    },
                    headers=headers,
                )
                assert response.status_code == 200
                assert transport.started.wait(5)

            # Enter the real SQLite confirmation UPDATE, pin an online backup during it,
            # then let both operations finish before restoring into a separate directory.
            update_active = threading.Event()
            continue_update = threading.Event()

            def pause_confirmation(connection, cursor, statement, parameters, context, many):
                if "UPDATE SNAPSHOTS" in statement.upper() and not update_active.is_set():
                    update_active.set()
                    assert continue_update.wait(10)

            event.listen(repository.engine, "before_cursor_execute", pause_confirmation)
            import watt_profile.logger.maintenance as maintenance

            original_online_copy = maintenance._online_copy
            backup_copy_finished = threading.Event()

            def observe_online_copy(source, destination):
                original_online_copy(source, destination)
                backup_copy_finished.set()

            monkeypatch.setattr(maintenance, "_online_copy", observe_online_copy)
            transport.release.set()
            assert update_active.wait(5)
            saved, target = tmp_path / "old-backup", tmp_path / "restored-installation"
            backup_started = threading.Event()
            backup_error = []

            def make_backup():
                backup_started.set()
                try:
                    backup(data_dir, saved)
                except BaseException as exc:
                    backup_error.append(exc)

            worker = threading.Thread(target=make_backup)
            worker.start()
            assert backup_started.wait(2) and backup_copy_finished.wait(5)
            continue_update.set()
            worker.join(10)
            assert not worker.is_alive() and not backup_error
            deadline = time.monotonic() + 5
            while repository.get_status().pending_count and time.monotonic() < deadline:
                time.sleep(0.01)
            assert repository.get_status().pending_count == 0
            assert repository.get_status().last_successful_upload_at is not None
            migrated = repository.load_installation()
            assert migrated.device_id == before.device_id
            assert (
                migrated.settings.model_copy(update={"server_url": before.settings.server_url})
                == before.settings
            )
            assert migrated.settings.server_url == "https://upload.invalid"
            assert migrated.lifecycle.session.descriptor == before.lifecycle.session.descriptor
            assert migrated.lifecycle.session.collection_enabled
            assert read_upload_token(data_dir) == saved_token
            with repository.engine.connect() as connection:
                saved_rows = connection.execute(
                    text(
                        "SELECT record_id, canonical_snapshot, payload_sha256, upload_state, "
                        "uploaded_at FROM snapshots"
                    )
                ).all()
            original_row = next(row for row in saved_rows if row[0] == str(snapshot.record_id))
            assert original_row[1] == group.canonical_records[0].decode("utf-8")
            assert original_row[3] == "uploaded"

            restore(saved, target)
            restored_engine = create_database(target / "logger.sqlite3")
            try:
                restored = LoggerRepository(restored_engine)
                restored_status = restored.get_status()
                assert restored.load_installation().device_id == before.device_id
                with restored_engine.connect() as connection:
                    rows = connection.execute(
                        text(
                            "SELECT record_id, canonical_snapshot, payload_sha256, upload_state, "
                            "uploaded_at FROM snapshots"
                        )
                    ).all()
                current_by_id = {row[0]: row for row in saved_rows}
                restored_by_id = {row[0]: row for row in rows}
                assert set(restored_by_id) <= set(current_by_id)
                confirmed_ids = {str(item.record_id) for item in group.batch.records}
                for record_id, restored_row in restored_by_id.items():
                    current_row = current_by_id[record_id]
                    assert restored_row[:3] == current_row[:3]
                    if record_id in confirmed_ids:
                        assert restored_row[3:] in {
                            ("pending", None),
                            ("uploaded", current_row[4]),
                        }
                    else:
                        assert restored_row[3:] == ("pending", None)
                restored_original = next(row for row in rows if row[0] == str(snapshot.record_id))
                if restored_original[3] == "pending":
                    assert restored_original[4] is None
                    assert restored_status.pending_count == sum(row[3] == "pending" for row in rows)
                    assert restored_status.last_successful_upload_at is None
                else:
                    assert restored_original[3] == "uploaded"
                    assert restored_original[4] == restored_status.last_successful_upload_at
                assert read_upload_token(target) == saved_token
            finally:
                restored_engine.dispose()
    finally:
        engine.dispose()
