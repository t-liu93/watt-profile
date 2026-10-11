"""Real cursor, stable snapshot, complete CSV and cancellation resource checks."""

import csv
import io
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import insert, text, update
from test_server_queries import dataset as query_dataset  # noqa: F401

from watt_profile.server.export import CSV_COLUMNS, CSV_FILENAME, ExportManager, snapshot_csv
from watt_profile.server.queries import QueryService, QueryUnavailable
from watt_profile.server.storage import schema as db
from watt_profile.server.storage.repository import snapshot_values


@pytest.fixture
def dataset(request):
    return request.getfixturevalue("query_dataset")


def test_csv_matches_query_all_fields_precision_and_empty(dataset):
    engine, summary, _, _, _ = dataset
    manager = ExportManager(engine)
    filters = {"received_before": "2026-01-01T00:00:01Z"}
    with manager.open(summary.site_id, filters) as stream:
        output = b"".join(stream)
    expected = QueryService(engine).reading_page(summary.site_id, filters).items
    rows = list(csv.reader(io.StringIO(output.decode())))
    assert rows[0] == list(CSV_COLUMNS)
    assert len(rows) == len(expected) + 1
    assert output == (",".join(CSV_COLUMNS) + "\r\n").encode() + b"".join(
        snapshot_csv(r) for r in expected
    )
    assert not output.startswith(b"\xef\xbb\xbf") and b"\r\n" in output
    assert CSV_FILENAME == "watt-profile-readings.csv"
    for row, record in zip(rows[1:], expected, strict=True):
        cells = dict(zip(CSV_COLUMNS, row, strict=True))
        assert cells["power_import_kw"] == "0.000"
        assert cells["power_export_kw"] == "1.2300"
        assert cells["meter_time_raw"] == ""
        assert cells["meter_time_utc"] == ""
        assert cells["received_at"].endswith(".000000Z")
        assert json.loads(cells["fields"]) == record.model_dump(mode="json")["fields"]
        assert json.loads(cells["quality"]) == record.model_dump(mode="json")["quality"]
        for name in type(record.metrics).model_fields:
            value = record.model_dump(mode="json")["metrics"][name]
            assert cells[name] == ("" if value is None else value)
    assert (
        b"".join(manager.open(summary.site_id, {"device_id": str(uuid4())}))
        == output.split(b"\r\n")[0] + b"\r\n"
    )
    assert engine.pool.checkedout() == 0
    manager.close()


def test_process_slots_close_before_next_disconnect_shutdown_and_safe_failure(dataset):
    engine, summary, _, _, _ = dataset
    manager, other = ExportManager(engine), ExportManager(engine)
    first, second = manager.open(summary.site_id), other.open(summary.site_id)
    cursor = first._result._real_result.cursor
    assert cursor.name and not cursor.closed
    assert first._connection.scalar(text("SHOW transaction_isolation")) == "repeatable read"
    assert first._connection.scalar(text("SHOW transaction_read_only")) == "on"
    assert first._connection.scalar(text("SHOW statement_timeout")) == "30min"
    with pytest.raises(QueryUnavailable, match="^temporarily_unavailable$") as failure:
        manager.open(summary.site_id)
    assert failure.value.status_code == 503 and engine.pool.checkedout() == 2
    first.close()
    assert cursor.closed and engine.pool.checkedout() == 1
    third = manager.open(summary.site_id)
    next(third)
    manager.close()
    assert third.closed and engine.pool.checkedout() == 1
    second.close()
    other.close()
    assert engine.pool.checkedout() == 0


def test_bounded_cursor_stable_export_with_concurrent_insert(dataset, monkeypatch):
    engine, summary, _, records, _ = dataset
    extra = [records[0].model_copy(update={"record_id": uuid4()}) for _ in range(1001)]
    with engine.begin() as writer:
        writer.execute(insert(db.snapshots), [snapshot_values(r, datetime.now(UTC)) for r in extra])
    manager = ExportManager(engine)
    stream = manager.open(summary.site_id)
    calls = []
    original = stream._result.fetchmany

    def bounded(size):
        calls.append(size)
        rows = original(size)
        assert len(rows) <= 500
        return rows

    monkeypatch.setattr(stream._result, "fetchmany", bounded)
    inserted = records[0].model_copy(update={"record_id": uuid4()})
    with engine.begin() as writer:
        writer.execute(insert(db.snapshots).values(**snapshot_values(inserted, datetime.now(UTC))))
    count = sum(1 for _ in stream)
    assert count == 1006
    assert calls == [500] * 4
    assert stream.closed and engine.pool.checkedout() == 0
    assert QueryService(engine).reading_page(summary.site_id).total == 1006
    manager.close()


@pytest.mark.parametrize("kind", ["corruption", "fetch", "timeout", "open"])
def test_exceptions_release_cursor_connection_slot(dataset, monkeypatch, kind):
    engine, summary, _, _, _ = dataset
    manager = ExportManager(engine)
    if kind == "open":
        with pytest.raises(ValueError):
            manager.open(uuid4())
        assert engine.pool.checkedout() == 0
    else:
        if kind == "corruption":
            with engine.begin() as writer:
                writer.execute(update(db.snapshots).values(payload_sha256="f" * 64))
        stream = manager.open(summary.site_id)
        cursor = stream._result._real_result.cursor
        next(stream)
        if kind == "fetch":

            def fail(_size):
                raise RuntimeError("synthetic fetch failure")

            monkeypatch.setattr(stream._result, "fetchmany", fail)
        if kind == "timeout":
            stream._connection.execute(text("SET LOCAL statement_timeout = '1ms'"))

            def timeout(_size):
                return stream._connection.execute(text("SELECT pg_sleep(0.02)")).fetchall()

            monkeypatch.setattr(stream._result, "fetchmany", timeout)
        with pytest.raises(Exception):
            next(stream)
        assert stream.closed and cursor.closed and engine.pool.checkedout() == 0
    with manager.open(summary.site_id, {"device_id": str(uuid4())}) as replacement:
        assert len(list(replacement)) == 1
    manager.close()


@pytest.mark.parametrize("shutdown", [False, True])
def test_close_interrupts_real_blocked_fetch(dataset, monkeypatch, shutdown):
    from concurrent.futures import ThreadPoolExecutor
    from time import monotonic, sleep

    from sqlalchemy import case, func, select

    import watt_profile.server.export as export

    engine, summary, _, records, _ = dataset
    first = min(records, key=lambda r: (r.received_at, r.device_id, r.record_id))
    original = export.readings_statement

    def delayed(site_id, filters):
        statement = original(site_id, filters)
        return statement.add_columns(
            func.pg_sleep(case((db.snapshots.c.record_id == first.record_id, 0), else_=30))
        )

    monkeypatch.setattr(export, "readings_statement", delayed)
    manager = ExportManager(engine)
    stream = manager.open(summary.site_id)
    cursor = stream._result._real_result.cursor
    pid = stream._connection.scalar(text("SELECT pg_backend_pid()"))
    next(stream)  # Header; the first row is buffered, subsequent FETCH blocks.
    with ThreadPoolExecutor(max_workers=2) as executor:
        fetching = executor.submit(next, stream)
        deadline = monotonic() + 5
        with engine.connect() as observer:
            while monotonic() < deadline:
                state = observer.execute(
                    select(func.count())
                    .select_from(text("pg_stat_activity"))
                    .where(text("pid = :pid AND wait_event = 'PgSleep' AND query LIKE 'FETCH%'"))
                    .params(pid=pid)
                ).scalar()
                if state:
                    break
                sleep(0.01)
            else:
                pytest.fail("Expected real server cursor blocked in FETCH")
        closing = executor.submit(manager.close if shutdown else stream.close)
        closing.result(timeout=5)
        with pytest.raises(Exception):
            fetching.result(timeout=5)
    assert stream.closed and cursor.closed and engine.pool.checkedout() == 0
    manager.close()


def test_manager_shutdown_continues_after_one_close_error(dataset, monkeypatch):
    engine, summary, _, _, _ = dataset
    manager = ExportManager(engine)
    first, second = manager.open(summary.site_id), manager.open(summary.site_id)
    original = first._result.close

    def fail_after_close():
        original()
        raise RuntimeError("synthetic cursor close failure")

    monkeypatch.setattr(first._result, "close", fail_after_close)
    with pytest.raises(QueryUnavailable):
        manager.close()
    assert first.closed and second.closed and engine.pool.checkedout() == 0
    replacement = ExportManager(engine)
    with replacement.open(summary.site_id) as stream:
        assert next(stream)
    replacement.close()
