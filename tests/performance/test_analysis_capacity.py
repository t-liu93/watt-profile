"""Explicit four-week acceptance; disposable synthetic PostgreSQL and Chromium only.

Run: WATT_TEST_POSTGRES=1 python -m pytest tests/performance -s
The explicit directory selector is required, even when database tests are enabled.
"""

# ruff: noqa: F811
import asyncio
import json
import os
import platform
import re
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import pytest
import uvicorn
from playwright.sync_api import sync_playwright
from sqlalchemy import event, func, insert, select, text
from test_server_analysis import analysis_data  # noqa: F401
from test_server_auth import server_settings  # noqa: F401
from test_server_ingest import batch

from watt_profile.server.analysis import AnalysisRead
from watt_profile.server.app import create_app
from watt_profile.server.auth import COOKIE
from watt_profile.server.storage import schema as db
from watt_profile.server.storage.repository import ServerRepository, load_snapshot, snapshot_values

pytestmark = pytest.mark.skipif(
    not any("tests/performance" in arg for arg in sys.argv[1:]),
    reason="Select tests/performance explicitly for four-week acceptance",
)
COUNT = 241_920


def bounded(value):
    if isinstance(value, dict):
        assert "canonical_snapshot" not in value and "snapshot" not in value
        for key, item in value.items():
            if key == "buckets":
                assert len(item) <= 2000
            if key == "slots":
                assert len(item) == 192  # two independent 96-slot metrics
            bounded(item)
    elif isinstance(value, list):
        for item in value:
            bounded(item)


@pytest.fixture
def capacity_data(analysis_data, server_engine):
    _, summary, _, _ = analysis_data
    start = datetime(2026, 1, 4, 23, tzinfo=UTC)  # exactly 28 Amsterdam dates
    template = batch(summary)
    descriptor = template.session.model_copy(update={"started_at": start})
    repository = ServerRepository(server_engine)
    with repository.transaction() as connection:
        repository.insert_descriptors(connection, descriptor, template.source)
    # Canonical bytes, hash and every projection come from the production validator;
    # bounded chunks never retain the month of Snapshots in Python.
    for offset in range(0, COUNT, 1000):
        values = []
        for i in range(offset, min(COUNT, offset + 1000)):
            at = start + timedelta(seconds=i * 10)
            metrics = template.records[0].metrics.model_dump()
            for name in metrics:
                if name.startswith("energy_"):
                    metrics[name] = Decimal(i) / 1000
                elif name.startswith("power_"):
                    metrics[name] = Decimal(i % 100) / 100
                elif name.startswith("voltage_"):
                    metrics[name] = Decimal("230.123")
                elif name.startswith("current_"):
                    metrics[name] = Decimal("2.345")
            record = template.records[0].model_copy(
                update={
                    "record_id": uuid4(),
                    "received_at": at,
                    "sampled_at": at,
                    "metrics": template.records[0].metrics.model_copy(update=metrics),
                }
            )
            values.append(snapshot_values(record, at))
        with server_engine.begin() as connection:
            connection.execute(insert(db.snapshots), values)
    with server_engine.begin() as connection:
        connection.execute(text("ANALYZE snapshots"))
        assert connection.scalar(select(func.count()).select_from(db.snapshots)) == COUNT
        for row in connection.execute(select(db.snapshots).limit(3)).mappings():
            load_snapshot(row)
    return (
        summary,
        descriptor,
        {
            "session_id": str(descriptor.session_id),
            "received_from": start.isoformat(),
            "received_before": (start + timedelta(weeks=4)).isoformat(),
        },
    )


@pytest.fixture
def live(server_engine, server_settings):
    from dataclasses import replace

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    app = create_app(replace(server_settings, public_origin=origin))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = monotonic() + 10
        while not server.started:
            assert thread.is_alive() and monotonic() < deadline
            sleep(0.01)
        with httpx.Client(base_url=origin, timeout=30) as client:
            csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/login").text)[1]
            response = client.post(
                "/login",
                data={
                    "username": "admin",
                    "password": "synthetic-password-long-enough",
                    "csrf": csrf,
                },
                follow_redirects=False,
            )
            assert response.status_code == 303
            yield client, app, origin
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
        assert not thread.is_alive()


def test_four_week_http_browser_and_resources(capacity_data, live, monkeypatch, tmp_path, request):
    client, app, origin = live
    summary, _, params = capacity_data
    base = f"/api/admin/v1/sites/{summary.site_id}/analysis/"
    evidence = {
        "machine": platform.uname()._asdict(),
        "cpus": os.cpu_count(),
        "cpu_model": next(
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ),
        "memory": Path("/proc/meminfo").read_text().splitlines()[0],
        "requests": {},
        "plans": [],
        "snapshots": COUNT,
    }
    target = Path(os.environ.get("WATT_PERFORMANCE_EVIDENCE", str(tmp_path / "evidence.json")))
    request.addfinalizer(
        lambda: target.write_text(json.dumps(evidence, indent=2, default=str) + "\n")
    )
    assert platform.machine() == "x86_64" and os.cpu_count() >= 2
    assert int(evidence["memory"].split()[1]) >= 4 * 1024 * 1024
    with app.state.engine.connect() as connection:
        evidence["postgres"] = connection.scalar(text("SELECT version()"))
        evidence["postgres_settings"] = dict(
            connection.execute(
                text(
                    "SELECT name, setting FROM pg_settings WHERE name IN "
                    "('shared_buffers', 'work_mem', 'max_parallel_workers_per_gather', 'jit')"
                )
            ).all()
        )
    statements = {}
    variants = {}
    evidence["query_seconds"] = {}

    def capture(conn, cursor, statement, parameters, context, executemany):
        context._capacity_start = monotonic()
        if "snapshots" in statement and statement.lstrip().upper().startswith(("SELECT", "WITH")):
            statements[statement] = parameters
            variants[(statement, repr(parameters))] = (statement, parameters)
            assert "canonical_snapshot" not in statement

    def completed(conn, cursor, statement, parameters, context, executemany):
        if statement in statements:
            evidence["query_seconds"].setdefault(statement, []).append(
                monotonic() - context._capacity_start
            )

    event.listen(app.state.engine, "before_cursor_execute", capture)
    event.listen(app.state.engine, "after_cursor_execute", completed)
    operations = [("overview", {}), ("power", {}), ("typical-day", {})]
    operations += [("typical-day", {"group": group}) for group in ("all", "weekday", "weekend")]
    operations += [("phases", {"direction": d}) for d in ("import", "export")]
    operations += [("quality", {})]
    try:
        for operation, extra in operations:
            query = params | extra
            warm = client.get(base + operation, params=query)
            assert warm.status_code == 200, warm.text
            measurements = []
            for _ in range(3):
                begin = monotonic()
                response = client.get(base + operation, params=query)
                elapsed = monotonic() - begin  # includes the complete body and serialization
                assert response.status_code == 200, response.text
                assert len(response.content) <= 2 * 1024 * 1024
                bounded(response.json()["data"])
                assert response.json()["data"]["context"]["reading_count"] == COUNT
                measurements.append({"seconds": elapsed, "bytes": len(response.content)})
            evidence["requests"][operation + json.dumps(extra, sort_keys=True)] = measurements
            print(operation, extra, measurements, flush=True)
        # Representative ten-minute zoom still retains global peak/gap semantics.
        zoom = params | {
            "zoom_from": params["received_from"],
            "zoom_before": "2026-01-04T23:10:00+00:00",
        }
        assert client.get(base + "power", params=zoom).status_code == 200
    finally:
        event.remove(app.state.engine, "before_cursor_execute", capture)
        event.remove(app.state.engine, "after_cursor_execute", completed)
    with app.state.engine.connect() as connection:
        connection.execute(text("SET LOCAL jit = off"))
        for statement, parameters in variants.values():
            plan = connection.exec_driver_sql(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, parameters
            ).scalar()
            evidence["plans"].append(
                {"sql": statement, "parameters": str(parameters), "plan": plan}
            )
        # A real optimizer-selected narrow raw range verifies the supporting index.
        narrow = connection.execute(
            text("""EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
            SELECT received_at, record_id FROM snapshots WHERE site_id=:site
            AND session_id=:session AND received_at >= :start AND received_at < :end
            ORDER BY received_at, device_id, record_id"""),
            {
                "site": summary.site_id,
                "session": params["session_id"],
                "start": params["received_from"],
                "end": zoom["zoom_before"],
            },
        ).scalar()
        assert "Index" in json.dumps(narrow)
        evidence["narrow_plan"] = narrow
    original = AnalysisRead.energy
    active = [threading.Event(), threading.Event()]
    admission = iter(active)
    release = threading.Event()

    def held(read):
        next(admission).set()
        assert release.wait(5)
        return original(read)

    with monkeypatch.context() as patch:
        patch.setattr(AnalysisRead, "energy", held)
        with ThreadPoolExecutor(2) as executor:
            begin = monotonic()
            futures = [
                executor.submit(client.get, base + "overview", params=params) for _ in range(2)
            ]
            try:
                assert all(entered.wait(5) for entered in active)
                rejected_at = monotonic()
                assert client.get(base + "power", params=params).status_code == 503
                evidence["busy_seconds"] = monotonic() - rejected_at
                assert evidence["busy_seconds"] < 1
            finally:
                release.set()
            assert all(f.result().status_code == 200 for f in futures)
            evidence["concurrent_seconds"] = monotonic() - begin
    assert app.state.engine.pool.checkedout() == 0
    # Real PostgreSQL statement timeout and cancellation, on this same full dataset.
    original = AnalysisRead.energy
    entered = threading.Event()

    def timeout(read):
        entered.set()
        read.connection.execute(text("SET LOCAL statement_timeout = '200ms'"))
        read.connection.execute(text("SELECT pg_sleep(3)"))
        return original(read)

    with monkeypatch.context() as patch:
        patch.setattr(AnalysisRead, "energy", timeout)
        assert client.get(base + "overview", params=params).status_code == 503

        async def cancel():
            entered.clear()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=origin,
                cookies={COOKIE: client.cookies[COOKIE]},
            ) as http:
                task = asyncio.create_task(http.get(base + "overview", params=params))
                assert await asyncio.to_thread(entered.wait, 3)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                deadline = monotonic() + 3
                while app.state.engine.pool.checkedout() and monotonic() < deadline:
                    await asyncio.sleep(0.02)

        asyncio.run(cancel())
    assert app.state.engine.pool.checkedout() == 0
    slots = app.state.analysis.slots
    assert slots.acquire(False) and slots.acquire(False)
    assert not slots.acquire(False)
    slots.release()
    slots.release()
    assert client.get(base + "overview", params=params).status_code == 200
    browser_measurements(client, origin, summary, params, evidence)
    slow = {
        key: runs
        for key, runs in evidence["requests"].items()
        if any(run["seconds"] >= 5 for run in runs)
    }
    assert not slow, slow
    print(
        json.dumps(
            {
                k: v
                for k, v in evidence.items()
                if k not in ("plans", "narrow_plan", "query_seconds")
            },
            indent=2,
        )
    )


def browser_measurements(client, origin, summary, params, evidence):
    """Instrument only served test JS; production assets/globals remain untouched."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            requests = []
            page.on("request", lambda request: requests.append(request.url))
            page.context.route(
                "**/*",
                lambda route: (
                    route.continue_()
                    if route.request.url.startswith(origin + "/")
                    else route.abort()
                ),
            )
            page.context.add_cookies(
                [{"name": COOKIE, "value": client.cookies[COOKIE], "url": origin}]
            )
            page.add_init_script("""(() => {
              window.__capacity = {bodies: [], paints: []};
              const original = window.fetch;
              window.fetch = async (...args) => {
                const response = await original(...args);
                const heavy = /analysis\\/(overview|power|typical-day|phases|quality)\\?/;
                if (heavy.test(response.url)) {
                  response.json = async () => {
                    const bytes = await response.arrayBuffer();
                    window.__capacity.bodies.push({url: response.url, at: performance.now()});
                    return JSON.parse(new TextDecoder().decode(bytes));
                  };
                }
                return response;
              };
            })();""")
            replacements = []

            def instrument(route):
                response = route.fetch()
                body = response.text()
                pattern = r"(\w+)\.setOption\((\w+)\),\1\.on\(`datazoom`"

                def replace(match):
                    chart, option = match.groups()
                    return (
                        f"{chart}.on(`finished`,()=>requestAnimationFrame(()=>"
                        "requestAnimationFrame(()=>window.__capacity.paints.push("
                        f"{{at:performance.now(),label:{chart}.getDom().getAttribute(`aria-label`)}}"
                        f")))),{chart}.setOption({option}),{chart}.on(`datazoom`"
                    )

                body, count = re.subn(pattern, replace, body)
                replacements.append(count)
                route.fulfill(response=response, body=body)

            page.route("**/assets/*.js", instrument)
            evidence["chromium"] = browser.version
            evidence["browser"] = {}
            for tab, expected in (("overview", 2), ("usage", 1), ("phases", 3)):
                page.goto(
                    origin
                    + f"/sites/{summary.site_id}?"
                    + urlencode(params | {"tab": tab, "range_mode": "custom"})
                )
                page.wait_for_function(
                    "n => new Set(window.__capacity.paints.map(p => p.label)).size >= n",
                    arg=expected,
                    timeout=30000,
                )
                timing = page.evaluate("window.__capacity")
                body_at = timing["bodies"][-1]["at"]
                elapsed = max(p["at"] for p in timing["paints"]) - body_at
                assert 0 <= elapsed < 2000, timing
                evidence["browser"][tab] = {"milliseconds": elapsed, **timing}
            evidence["browser_requests"] = requests
            assert not any("/readings" in url or ".csv" in url for url in requests)
            assert all(url.startswith(origin + "/") for url in requests)
            assert sum(replacements) >= 3  # each document receives an instrumented bundle
        finally:
            browser.close()
