"""Analysis HTTP contracts against isolated synthetic PostgreSQL data."""

# ruff: noqa: F811
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event, get_ident
from time import monotonic
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import insert, text, update
from test_server_analysis import analysis_data  # noqa: F401
from test_server_auth import client, login, server_settings  # noqa: F401

from watt_profile.server.analysis import AnalysisRead
from watt_profile.server.auth import COOKIE
from watt_profile.server.registration import SiteInput, SitePatch
from watt_profile.server.storage import schema as db
from watt_profile.server.storage.repository import snapshot_values

P = "/api/admin/v1"


@pytest.fixture
def data(client, analysis_data):
    login(client)
    _, summary, registration, add = analysis_data
    start = datetime(2026, 3, 10, tzinfo=UTC)
    descriptor, records = add(
        times=(start, start + timedelta(seconds=60), start + timedelta(seconds=120)),
        metrics=[
            {
                name: Decimal("1")
                for name in (
                    "energy_import_t1_kwh",
                    "energy_import_t2_kwh",
                    "energy_export_t1_kwh",
                    "energy_export_t2_kwh",
                )
            }
            for _ in range(3)
        ],
    )
    params = dict(
        session_id=str(descriptor.session_id),
        received_from=start.isoformat(),
        received_before=(start + timedelta(seconds=180)).isoformat(),
    )
    return summary, registration, add, descriptor, records, params


def url(summary, operation):
    return P + f"/sites/{summary.site_id}/analysis/{operation}"


def test_routes_context_decimals_and_independent_pages(client, data):
    summary, _, _, descriptor, records, params = data
    sessions = client.get(url(summary, "sessions"), params={"limit": 1}).json()["data"]
    assert sessions["total"] == 1 and sessions["default_session_id"] == str(descriptor.session_id)
    assert sessions["initial_range"]["received_before"] == "2026-03-10T00:02:00.000001Z"
    for operation in ("overview", "power", "typical-day", "phases", "quality"):
        response = client.get(url(summary, operation), params=params)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        result = response.json()["data"]
        assert result["context"]["reading_count"] == 3
        assert result["context"]["session_id"] == str(descriptor.session_id)
        assert result["context"]["timezone"] == "Europe/Amsterdam"
    overview = client.get(url(summary, "overview"), params=params).json()["data"]
    for key in ("energy", "power", "quality"):
        assert overview[key]["context"] == overview["context"]
    assert isinstance(overview["statistics"][0]["mean"], str)
    assert isinstance(overview["quality"]["coverage_percent"], str)
    assert overview["energy"]["import_energy"]["energy_kwh"] == "0"
    zoom = {
        **params,
        "zoom_from": records[1].received_at.isoformat(),
        "zoom_before": params["received_before"],
    }
    curve = client.get(url(summary, "power"), params=zoom).json()["data"]
    assert curve["context"]["reading_count"] == 3
    assert curve["curve_range"]["received_from"] == "2026-03-10T00:01:00.000000Z"
    phases = client.get(url(summary, "phases"), params={**zoom, "direction": "export"}).json()[
        "data"
    ]
    assert phases["direction"] == "export"
    assert phases["power"][0]["metric"] == "power_export_l1_kw"
    groups = client.get(url(summary, "typical-day"), params={**params, "group": "weekend"}).json()[
        "data"
    ]["groups"]
    assert len(groups) == 1 and groups[0]["group"] == "weekend"
    quality = client.get(
        url(summary, "quality"),
        params={**params, "gap_offset": 1, "gap_limit": 1, "problem_offset": 2, "problem_limit": 3},
    ).json()["data"]
    assert quality["gap_total"] == 3 and len(quality["gaps"]) == 1
    assert (
        quality["gap_offset"],
        quality["gap_limit"],
        quality["problem_offset"],
        quality["problem_limit"],
    ) == (1, 1, 2, 3)
    assert client.app.state.engine.pool.checkedout() == 0


def test_workspace_literal_search_names_uuid_and_pages(client, data):
    summary, registration, _, _, _, _ = data
    registration.edit_site(summary.site_id, SitePatch(display_name="A%_ Beacon"))
    other = uuid4()
    registration.create_site(SiteInput(site_id=other, address="other"))
    for query in ("%_", "beACON", "sYnTh", str(summary.site_id)):
        response = client.get(P + "/workspace/sites", params={"q": query})
        assert response.status_code == 200
        result = response.json()["data"]
        assert result["total"] == 1 and result["items"][0]["reading_count"] == 3
        assert result["items"][0]["latest_reading_received_at"] == "2026-03-10T00:02:00.000000Z"
    page = client.get(P + "/workspace/sites", params={"limit": 1, "offset": 1}).json()["data"]
    assert page["total"] == 2 and len(page["items"]) == 1
    assert client.get(P + "/workspace/sites", params={"limit": 101}).status_code == 422
    assert (
        client.get(P + "/workspace/sites", params={"q": "_", "site_id": str(other)}).status_code
        == 422
    )


def test_latest_independent_session_range_contact_and_integrity(client, data):
    summary, _, add, descriptor, records, params = data
    _, later = add(times=(datetime(2026, 3, 20, tzinfo=UTC),))
    response = client.get(url(summary, "latest"), params={"session_id": params["session_id"]})
    assert response.status_code == 200
    result = response.json()["data"]
    assert result["snapshot"] == records[-1].model_dump(mode="json")
    assert (
        result["site_latest_reading_received_at"] == later[0].model_dump(mode="json")["received_at"]
    )
    assert result["ingested_at"] == "2026-03-01T00:00:00.000000Z"
    assert result["snapshot"]["meter_time_utc"] != result["snapshot"]["received_at"]
    contact = datetime(2026, 4, 1, tzinfo=UTC)
    with client.app.state.engine.begin() as connection:
        connection.execute(
            update(db.devices)
            .where(db.devices.c.device_id == summary.device_id)
            .values(last_authenticated_contact_at=contact)
        )
    assert (
        client.get(url(summary, "latest"), params={"session_id": params["session_id"]}).json()[
            "data"
        ]["device_last_authenticated_contact_at"]
        == "2026-04-01T00:00:00.000000Z"
    )
    empty, _ = add()
    assert (
        client.get(url(summary, "latest"), params={"session_id": str(empty.session_id)}).json()[
            "data"
        ]["snapshot"]
        is None
    )
    with client.app.state.engine.begin() as connection:
        connection.execute(
            update(db.snapshots)
            .where(db.snapshots.c.record_id == records[-1].record_id)
            .values(payload_sha256="0" * 64)
        )
    failure = client.get(url(summary, "latest"), params={"session_id": str(descriptor.session_id)})
    assert failure.status_code == 503 and "projection" not in failure.text
    assert client.app.state.engine.pool.checkedout() == 0


def test_fixed_validation_ownership_and_auth(client, data):
    summary, registration, _, _, _, params = data
    other = uuid4()
    registration.create_site(SiteInput(site_id=other))
    assert client.get(P + f"/sites/{other}/analysis/overview", params=params).status_code == 404
    assert (
        client.get(
            P + f"/sites/{other}/analysis/latest", params={"session_id": params["session_id"]}
        ).status_code
        == 404
    )
    assert (
        client.get(
            url(summary, "overview"), params={**params, "session_id": str(uuid4())}
        ).status_code
        == 404
    )
    assert client.get(P + f"/sites/{uuid4()}/analysis/sessions").status_code == 404
    for query in (
        {},
        {**params, "session_id": "SECRET"},
        {**params, "received_from": "2026-01-01T00:00:00Z"},
        {**params, "received_before": params["received_from"]},
        {**params, "received_from": "2026-03-10T00:00:00"},
        {**params, "unknown": "SECRET"},
    ):
        response = client.get(url(summary, "overview"), params=query)
        assert response.status_code == 422 and "SECRET" not in response.text
        assert response.json()["error"]["code"] == "invalid_input"
    for operation, extra in (
        ("power", {"zoom_from": params["received_from"]}),
        ("power", {"zoom_from": "2026-03-09T00:00:00Z", "zoom_before": params["received_before"]}),
        ("phases", {"direction": "SECRET"}),
        ("typical-day", {"group": "SECRET"}),
        ("quality", {"gap_limit": "101"}),
        ("quality", {"problem_offset": "-1"}),
    ):
        assert client.get(url(summary, operation), params={**params, **extra}).status_code == 422
    assert (
        client.get(
            url(summary, "power"), params=[*params.items(), ("session_id", params["session_id"])]
        ).status_code
        == 422
    )
    assert client.get(url(summary, "latest")).status_code == 422
    assert (
        client.get(url(summary, "overview"), params=params, headers={"Host": "evil"}).status_code
        == 403
    )
    client.cookies.clear()
    for path in (
        url(summary, "overview"),
        url(summary, "latest"),
        url(summary, "sessions"),
        P + "/workspace/sites",
    ):
        response = client.get(
            path,
            params=params if path.endswith("overview") else None,
            headers={"Authorization": "Bearer " + "a" * 43},
        )
        assert response.status_code == 401


def test_overview_one_snapshot_and_entire_call_off_event_loop(client, data, monkeypatch):
    summary, _, _, _, records, params = data
    original = AnalysisRead.energy
    event_threads = []

    async def identify():
        event_threads.append(get_ident())

    client.portal.call(identify)

    def backfill(read):
        assert get_ident() != event_threads[0]
        assert read.connection.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert read.connection.scalar(text("SHOW transaction_read_only")) == "on"
        assert read.connection.scalar(text("SHOW statement_timeout")) == "10s"
        record = records[-1].model_copy(
            update={
                "record_id": uuid4(),
                "received_at": records[-1].received_at + timedelta(seconds=10),
            }
        )
        with client.app.state.engine.begin() as connection:
            connection.execute(
                insert(db.snapshots).values(**snapshot_values(record, datetime.now(UTC)))
            )
        return original(read)

    monkeypatch.setattr(AnalysisRead, "energy", backfill)
    response = client.get(url(summary, "overview"), params=params)
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["context"]["reading_count"] == result["quality"]["actual_count"] == 3
    assert sum(b["valid_count"] for b in result["power"]["import_power"]["buckets"]) == 3
    assert result["energy"]["import_energy"]["valid_endpoint_count"] == 3
    assert client.get(url(summary, "quality"), params=params).json()["data"]["actual_count"] == 4


def test_nonwaiting_busy_latest_csv_independent_and_worker_cleanup(client, data, monkeypatch):
    summary, _, _, _, _, params = data
    original = AnalysisRead.energy
    entered = [Event(), Event()]
    release = Event()
    count = iter(entered)

    def blocked(read):
        next(count).set()
        assert release.wait(5)
        return original(read)

    monkeypatch.setattr(AnalysisRead, "energy", blocked)
    with ThreadPoolExecutor(2) as executor:
        pending = [
            executor.submit(client.get, url(summary, "overview"), params=params) for _ in range(2)
        ]
        try:
            assert all(e.wait(5) for e in entered)
            begin = monotonic()
            assert client.get(url(summary, "power"), params=params).status_code == 503
            assert monotonic() - begin < 1
            assert (
                client.get(
                    url(summary, "latest"), params={"session_id": params["session_id"]}
                ).status_code
                == 200
            )
            assert client.get(P + f"/sites/{summary.site_id}/readings.csv").status_code == 200
        finally:
            release.set()
        assert all(p.result().status_code == 200 for p in pending)
    assert client.app.state.engine.pool.checkedout() == 0
    monkeypatch.setattr(AnalysisRead, "energy", original)
    assert client.get(url(summary, "overview"), params=params).status_code == 200


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_failure_cancellation_release_owned_resources(client, data, monkeypatch, cancel):
    summary, _, _, _, _, params = data
    entered = Event()
    original = AnalysisRead.energy

    def blocked(read):
        entered.set()
        # A shorter test-local timeout exercises the actual driver timeout path.
        read.connection.execute(text("SET LOCAL statement_timeout = '200ms'"))
        read.connection.execute(text("SELECT pg_sleep(3)"))
        return original(read)

    monkeypatch.setattr(AnalysisRead, "energy", blocked)
    if not cancel:
        response = client.get(url(summary, "overview"), params=params)
        assert response.status_code == 503 and "pg_sleep" not in response.text
    else:

        async def run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=client.app),
                base_url="http://testserver",
                cookies={COOKIE: client.cookies[COOKIE]},
            ) as http:
                task = asyncio.create_task(http.get(url(summary, "overview"), params=params))
                assert await asyncio.to_thread(entered.wait, 3)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                deadline = monotonic() + 3
                while client.app.state.engine.pool.checkedout() and monotonic() < deadline:
                    await asyncio.sleep(0.02)

        asyncio.run(run())
    assert client.app.state.engine.pool.checkedout() == 0
    api = client.app.state.analysis
    assert api.slots.acquire(blocking=False) and api.slots.acquire(blocking=False)
    assert not api.slots.acquire(blocking=False)
    api.slots.release()
    api.slots.release()
    monkeypatch.setattr(AnalysisRead, "energy", original)
    assert client.get(url(summary, "overview"), params=params).status_code == 200


def test_storage_failure_safe_and_recoverable(client, data, monkeypatch):
    summary, _, _, _, _, params = data
    original = client.app.state.engine.connect

    def failed():
        from sqlalchemy.exc import OperationalError

        raise OperationalError("SECRET", {}, Exception("SECRET"))

    monkeypatch.setattr(client.app.state.engine, "connect", failed)
    for operation, query in (
        ("overview", params),
        ("latest", {"session_id": params["session_id"]}),
        ("sessions", {}),
    ):
        response = client.get(url(summary, operation), params=query)
        assert response.status_code == 503 and "SECRET" not in response.text
    monkeypatch.setattr(client.app.state.engine, "connect", original)
    assert client.get(url(summary, "overview"), params=params).status_code == 200


def test_admission_precedes_dispatch_and_queued_cancellation_releases(client, data, monkeypatch):
    from watt_profile.server import app as app_module

    summary, _, _, _, _, params = data

    async def run():
        dispatched = asyncio.Event()
        calls = 0

        async def queued(call):
            nonlocal calls
            calls += 1
            if calls == 2:
                dispatched.set()
            await asyncio.Event().wait()

        with monkeypatch.context() as patch:
            patch.setattr(app_module, "run_in_threadpool", queued)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=client.app),
                base_url="http://testserver",
                cookies={COOKIE: client.cookies[COOKIE]},
            ) as http:
                tasks = [
                    asyncio.create_task(http.get(url(summary, "overview"), params=params))
                    for _ in range(2)
                ]
                try:
                    await asyncio.wait_for(dispatched.wait(), 3)
                    # Busy admission returns before touching the saturated dispatcher.
                    assert (await http.get(url(summary, "power"), params=params)).status_code == 503
                    assert calls == 2
                finally:
                    for task in tasks:
                        task.cancel()
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    assert all(isinstance(result, asyncio.CancelledError) for result in results)
        api = client.app.state.analysis
        assert api.slots.acquire(blocking=False) and api.slots.acquire(blocking=False)
        api.slots.release()
        api.slots.release()
        assert client.app.state.engine.pool.checkedout() == 0

    asyncio.run(run())


def test_cancelled_running_worker_keeps_admission_until_transaction_closes(
    client, data, monkeypatch
):
    summary, _, _, _, _, params = data
    entered, release = Event(), Event()
    original = AnalysisRead.energy

    def blocked(read):
        entered.set()
        assert release.wait(5)
        return original(read)

    monkeypatch.setattr(AnalysisRead, "energy", blocked)

    async def run():
        api = client.app.state.analysis
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app),
            base_url="http://testserver",
            cookies={COOKIE: client.cookies[COOKIE]},
        ) as http:
            task = asyncio.create_task(http.get(url(summary, "overview"), params=params))
            reserve = None
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert client.app.state.engine.pool.checkedout() == 1
                # Reserve the other slot without starting another worker.
                reserve = api.admit(summary.site_id, params["session_id"], params, "overview")
                assert (await http.get(url(summary, "power"), params=params)).status_code == 503
                assert (
                    await http.get(
                        url(summary, "latest"), params={"session_id": params["session_id"]}
                    )
                ).status_code == 200
            finally:
                if reserve is not None:
                    reserve.cancel_pending()
                release.set()
            deadline = monotonic() + 3
            while client.app.state.engine.pool.checkedout() and monotonic() < deadline:
                await asyncio.sleep(0.02)
        assert client.app.state.engine.pool.checkedout() == 0
        assert api.slots.acquire(blocking=False) and api.slots.acquire(blocking=False)
        api.slots.release()
        api.slots.release()

    asyncio.run(run())
