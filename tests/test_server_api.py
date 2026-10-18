"""Complete Server HTTP contracts against a disposable PostgreSQL database."""

# Imported pytest fixtures are deliberately resolved through test function arguments.
# ruff: noqa: F811

import asyncio
import json
from threading import get_ident
from time import monotonic, sleep
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from test_server_auth import client, login, server_settings  # noqa: F401
from test_server_ingest import batch

from watt_profile.contracts.upload import MAX_BODY_BYTES
from watt_profile.server.app import create_app
from watt_profile.server.registration import RegistrationSummary

P = "/api/admin/v1"


def registration():
    return RegistrationSummary(
        site_id=uuid4(),
        device_id=uuid4(),
        address="synthetic",
        description="<script>private</script>",
        token="a" * 43,
    )


def test_complete_routes_safe_data_and_ingest(client):
    csrf = login(client)
    headers = {"X-CSRF-Token": csrf, "Origin": "http://TESTSERVER:80"}
    summary = registration()
    body = summary.model_dump(mode="json")
    site, device = str(summary.site_id), str(summary.device_id)
    preview = client.post(P + "/registrations/preview", json=body, headers=headers)
    assert preview.status_code == 200
    assert body["token"] not in preview.text
    assert client.get(P + "/sites").json()["data"]["total"] == 0
    committed = client.post(P + "/registrations", json=body, headers=headers)
    assert committed.status_code == 200
    credential = committed.json()["data"]["credential_id"]
    assert (
        client.post(P + "/registrations", json=body, headers=headers).json()["data"]["actions"][
            "site"
        ]
        == "reuse"
    )
    assert client.get(P + "/sites", params={"q": "SYNTH"}).json()["data"]["total"] == 1
    assert client.get(P + "/devices").json()["data"]["total"] == 1
    assert client.get(P + f"/sites/{site}").json()["data"]["reading_count"] == 0
    assert (
        client.get(P + f"/devices/{device}").json()["data"]["credentials"][0]["credential_id"]
        == credential
    )
    assert (
        client.patch(P + f"/sites/{site}", json={"address": "new"}, headers=headers).status_code
        == 200
    )
    other_site, other_device = str(uuid4()), str(uuid4())
    assert (
        client.post(P + "/sites", json={"site_id": other_site}, headers=headers).status_code == 200
    )
    assert (
        client.post(P + "/devices", json={"device_id": other_device}, headers=headers).status_code
        == 200
    )
    assert (
        client.post(
            P + f"/devices/{other_device}/credentials", json={"token": "b" * 43}, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.put(
            P + f"/devices/{device}/sites/{other_site}", json={}, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.delete(P + f"/devices/{device}/sites/{other_site}", headers=headers).status_code
        == 204
    )
    payload = batch(summary)
    upload_headers = {"Authorization": "Bearer " + body["token"]}
    ingest = client.post(
        "/api/v1/ingest/batches", json=payload.model_dump(mode="json"), headers=upload_headers
    )
    assert ingest.status_code == 200
    assert ingest.json() == {
        "schema_version": 1,
        "device_id": device,
        "confirmed_record_ids": [str(record.record_id) for record in payload.records],
    }
    assert (
        client.post(
            "/api/v1/ingest/batches", json=payload.model_dump(mode="json"), headers=upload_headers
        ).json()
        == ingest.json()
    )
    page = client.get(P + f"/sites/{site}/readings").json()["data"]
    assert page["items"] == [record.model_dump(mode="json") for record in payload.records]
    assert page["total"] == 1
    assert client.get(P + f"/sites/{site}/sessions").json()["data"]["total"] == 1
    record = payload.records[0]
    assert client.get(P + f"/sites/{site}/readings/{device}/{record.record_id}").json()[
        "data"
    ] == record.model_dump(mode="json")
    assert (
        client.get(P + f"/sites/{other_site}/readings/{device}/{record.record_id}").status_code
        == 404
    )
    exported = client.get(P + f"/sites/{site}/readings.csv")
    assert exported.status_code == 200 and len(exported.content.splitlines()) == 2
    assert (
        exported.headers["content-disposition"]
        == 'attachment; filename="watt-profile-readings.csv"'
    )
    assert not client.app.state.exports._streams
    assert (
        client.get(P + f"/sites/{site}/readings", params={"device_id": str(uuid4())}).json()[
            "data"
        ]["total"]
        == 0
    )
    assert client.get(P + f"/sites/{site}").json()["data"]["reading_count"] == 1
    changed = payload.model_dump(mode="json")
    changed["records"][0]["metrics"]["power_import_kw"] = "42.0"
    assert (
        client.post("/api/v1/ingest/batches", json=changed, headers=upload_headers).json()["error"][
            "code"
        ]
        == "record_conflict"
    )
    assert client.delete(P + f"/devices/{device}/sites/{site}", headers=headers).status_code == 204
    assert (
        client.post(
            "/api/v1/ingest/batches", json=payload.model_dump(mode="json"), headers=upload_headers
        ).json()["error"]["code"]
        == "site_not_authorized"
    )
    assert (
        client.post(
            P + f"/devices/{device}/credentials/{credential}/disable", json={}, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/ingest/batches", json=payload.model_dump(mode="json"), headers=upload_headers
        ).status_code
        == 401
    )
    assert client.get(P + f"/sites/{site}/readings").json()["data"]["total"] == 1


def test_fixed_errors_strict_json_queries_and_streamed_caps(client):
    csrf = login(client)
    headers = {"X-CSRF-Token": csrf, "Content-Type": "application/json"}
    summary = registration()
    body = summary.model_dump(mode="json")
    assert client.post(P + "/registrations", json=body, headers=headers).status_code == 200
    for payload in ({**body, "extra": "SECRET"}, {**body, "token": "SECRET"}, []):
        response = client.post(P + "/registrations/preview", json=payload, headers=headers)
        assert response.status_code == 422 and "SECRET" not in response.text
        assert set(response.json()) == {"error"}
    assert (
        client.post(
            P + "/registrations", content=json.dumps(body).encode("utf-16"), headers=headers
        ).status_code
        == 422
    )
    duplicate = json.dumps(body)[:-1] + ',"token":"SECRET"}'
    assert client.post(P + "/registrations", content=duplicate, headers=headers).status_code == 422
    assert (
        client.post(
            P + "/registrations", json={**body, "token": "b" * 43}, headers=headers
        ).status_code
        == 409
    )
    assert client.get(P + f"/sites/{uuid4()}").status_code == 404
    for query in ({"limit": "0"}, {"q": "x", "site_id": str(uuid4())}, {"extra": "SECRET"}):
        response = client.get(P + "/sites", params=query)
        assert response.status_code == 422 and "SECRET" not in response.text
    path = P + f"/sites/{summary.site_id}/readings"
    for query in (
        {"received_from": "2026-01-01T00:00:00"},
        {"device_id": "SECRET"},
        {"received_from": "2026-01-02T00:00:00Z", "received_before": "2026-01-01T00:00:00Z"},
    ):
        assert client.get(path, params=query).status_code == 422
    assert (
        client.patch(P + f"/sites/{summary.site_id}", json={}, headers=headers).status_code == 422
    )

    def chunks(size):
        for _ in range(size // 4096 + 1):
            yield b"x" * 4096

    assert client.post(P + "/sites", content=chunks(32768), headers=headers).status_code == 422
    response = client.post(
        "/api/v1/ingest/batches", content=chunks(MAX_BODY_BYTES), headers=headers
    )
    assert response.status_code == 413 and response.json()["error"]["code"] == "batch_too_large"
    upload_headers = {
        "Authorization": "Bearer " + body["token"],
        "Content-Type": "application/json",
    }
    for options in (
        {"content": b"SECRET"},
        {"json": {}, "headers": {**upload_headers, "Content-Encoding": "gzip"}},
    ):
        options.setdefault("headers", upload_headers)
        response = client.post("/api/v1/ingest/batches", **options)
        assert response.status_code == 422 and "SECRET" not in response.text
    assert (
        client.get(P + f"/devices/{summary.device_id}").json()["data"]["device"][
            "last_authenticated_contact_at"
        ]
        is not None
    )


def test_live_ready_storage_failure_and_threaded_work(client, monkeypatch):
    assert client.get("/health/live").json() is True
    assert client.get("/health/ready").json() is True
    csrf = login(client)
    event_thread = []
    original = client.app.state.registration.create_site

    async def capture():
        event_thread.append(get_ident())

    # TestClient's portal identifies the actual app event-loop thread.
    client.portal.call(capture)

    def threaded(value):
        assert get_ident() != event_thread[0]
        return original(value)

    monkeypatch.setattr(client.app.state.registration, "create_site", threaded)
    assert (
        client.post(
            P + "/sites", json={"site_id": str(uuid4())}, headers={"X-CSRF-Token": csrf}
        ).status_code
        == 200
    )

    def down(*args, **kwargs):
        raise OperationalError("SECRET SQL", {}, Exception("SECRET URL"))

    monkeypatch.setattr(client.app.state.engine, "connect", down)
    response = client.get("/health/ready")
    assert response.status_code == 503 and response.json() is False
    response = client.get(P + "/sites")
    assert response.status_code == 503 and "SECRET" not in response.text
    assert client.get("/health/live").json() is True
    client.app.state.ready = False
    assert client.get("/health/ready").status_code == 503


@pytest.mark.parametrize("blocked", [False, True])
def test_http_disconnect_closes_real_export(client, blocked):
    login(client)
    summary = registration()
    client.app.state.registration.commit(summary)
    messages = []
    owned = []
    original_open = client.app.state.exports.open

    def capture_open(*args):
        stream = original_open(*args)
        if blocked:
            original_result = stream._result

            class BlockingResult:
                def fetchmany(self, size):
                    # The fetch worker holds CSVStream's lock while the real
                    # PostgreSQL driver blocks; cancellation must interrupt it.
                    stream._connection.exec_driver_sql("SELECT pg_sleep(30)")
                    return original_result.fetchmany(size)

                def close(self):
                    original_result.close()

            stream._result = BlockingResult()
        owned.append(stream)
        return stream

    client.app.state.exports.open = capture_open

    async def run():
        started = asyncio.Event()

        async def receive():
            await started.wait()
            if blocked:

                def wait_for_database_block():
                    deadline = monotonic() + 5
                    with client.app.state.engine.connect() as connection:
                        while monotonic() < deadline:
                            wait_event = connection.scalar(
                                text("SELECT wait_event FROM pg_stat_activity WHERE pid = :pid"),
                                {"pid": owned[0]._driver.info.backend_pid},
                            )
                            if wait_event == "PgSleep":
                                return
                            sleep(0.01)
                    raise AssertionError("export worker did not reach the DB blocking query")

                await asyncio.to_thread(wait_for_database_block)
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body":
                started.set()
                if not blocked:
                    await asyncio.sleep(0.02)

        path = P + f"/sites/{summary.site_id}/readings.csv"
        await client.app(
            {
                "type": "http",
                "asgi": {"spec_version": "2.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "root_path": "",
                "server": ("testserver", 80),
                "client": ("127.0.0.1", 1234),
                "headers": [
                    (b"host", b"testserver"),
                    (
                        b"cookie",
                        f"watt_server_session={client.cookies['watt_server_session']}".encode(),
                    ),
                ],
            },
            receive,
            send,
        )

    async def bounded_run():
        await asyncio.wait_for(run(), timeout=8)

    asyncio.run(bounded_run())
    assert any(message["type"] == "http.response.body" for message in messages)
    assert len(owned) == 1 and owned[0].closed and not client.app.state.exports._streams
    assert client.app.state.engine.pool.checkedout() == 0
    for _ in range(2):
        client.app.state.exports.open(summary.site_id)
    assert client.get(P + f"/sites/{summary.site_id}/readings.csv").status_code == 503
    client.app.state.exports.close()
    assert client.app.state.engine.pool.checkedout() == 0


def test_readiness_before_startup_and_no_outbound(server_settings, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("automatic outbound HTTP")

    monkeypatch.setattr("httpx.HTTPTransport.handle_request", forbidden)
    monkeypatch.setattr("httpx.AsyncHTTPTransport.handle_async_request", forbidden)
    app = create_app(server_settings)

    # ASGI transport, not network, checks readiness before lifespan initialization.
    async def check():
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            assert (await client.get("/health/ready")).json() is False

    asyncio.run(check())
    with TestClient(app) as client:
        # TestClient uses an ASGI transport; default HTTP transports are forbidden.
        assert client.get("/health/live").json() is True
        assert client.get("/health/ready").json() is True
    assert app.state.engine.pool.checkedout() == 0


def test_cli_single_worker_without_forwarding_or_access_logs(server_settings, monkeypatch):
    from watt_profile.cli.server import main

    captured = {}
    monkeypatch.setattr("watt_profile.cli.server.ServerSettings.from_env", lambda: server_settings)
    monkeypatch.setattr("uvicorn.run", lambda app, **options: captured.update(options))
    assert main([]) == 0
    assert captured["workers"] == 1 and captured["reload"] is False
    assert captured["proxy_headers"] is False and captured["access_log"] is False


def test_display_name_management_preserves_registration_and_readings(client):
    csrf = login(client)
    headers = {"X-CSRF-Token": csrf, "Origin": "http://testserver"}
    summary = registration()
    body = summary.model_dump(mode="json")
    site, device = str(summary.site_id), str(summary.device_id)
    assert client.post(P + "/registrations", json=body, headers=headers).status_code == 200
    for path, value in ((f"/sites/{site}", "Home"), (f"/devices/{device}", "Logger")):
        response = client.patch(P + path, json={"display_name": value}, headers=headers)
        assert response.status_code == 200
        assert response.json()["data"]["display_name"] == value
        for invalid in (
            None,
            123,
            "x" * 101,
            "line\nline",
            "tab\t",
            "\x7f",
            "\u0085",
            "\u2028",
            "\u2029",
        ):
            assert (
                client.patch(P + path, json={"display_name": invalid}, headers=headers).status_code
                == 422
            )
    payload = batch(summary)
    upload_headers = {"Authorization": "Bearer " + body["token"]}
    assert (
        client.post(
            "/api/v1/ingest/batches", json=payload.model_dump(mode="json"), headers=upload_headers
        ).status_code
        == 200
    )
    csv_before = client.get(P + f"/sites/{site}/readings.csv").content
    assert client.post(P + "/registrations", json=body, headers=headers).status_code == 200
    assert client.get(P + f"/sites/{site}").json()["data"]["site"]["display_name"] == "Home"
    assert client.get(P + f"/devices/{device}").json()["data"]["device"]["display_name"] == "Logger"
    assert (
        client.patch(P + f"/devices/{device}", json={"address": "bad"}, headers=headers).status_code
        == 422
    )
    assert (
        client.patch(
            P + f"/devices/{uuid4()}", json={"display_name": "Missing"}, headers=headers
        ).status_code
        == 404
    )
    for path in (f"/sites/{site}", f"/devices/{device}"):
        assert (
            client.patch(P + path, json={"display_name": ""}, headers=headers).json()["data"][
                "display_name"
            ]
            == ""
        )
    assert client.get(P + f"/sites/{site}/readings.csv").content == csv_before
    assert (
        client.post(
            "/api/v1/ingest/batches", json=payload.model_dump(mode="json"), headers=upload_headers
        ).status_code
        == 200
    )
    created = client.post(
        P + "/sites", json={"site_id": str(uuid4()), "display_name": "New site"}, headers=headers
    )
    assert created.status_code == 200 and created.json()["data"]["display_name"] == "New site"
    assert (
        client.post(
            P + "/registrations/preview", json={**body, "display_name": "invalid"}, headers=headers
        ).status_code
        == 422
    )
