"""Closable, bounded CSV streams with process-wide admission and owned connections."""

import csv
import io
import json
from collections.abc import Iterator
from threading import Event, RLock

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from watt_profile.contracts.domain import Metrics, Snapshot
from watt_profile.server.queries import (
    QueryUnavailable,
    parse_filters,
    query_id,
    readings_statement,
    require_site,
)
from watt_profile.server.storage.repository import load_snapshot

CSV_FILENAME = "watt-profile-readings.csv"
FETCH_ROWS = 500
CSV_COLUMNS = (
    "schema_version",
    "device_id",
    "record_id",
    "site_id",
    "session_id",
    "source_id",
    "measurement_point_id",
    "meter_time_raw",
    "meter_time_utc",
    "received_at",
    "sampled_at",
    *Metrics.model_fields,
    "fields",
    "quality",
)
# All managers share this admission set; constructing another manager cannot bypass the cap.
_ADMISSION_LOCK = RLock()
_ACTIVE: set["CSVStream"] = set()


def csv_line(values) -> bytes:
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\r\n").writerow(values)
    return output.getvalue().encode("utf-8")


def snapshot_csv(snapshot: Snapshot) -> bytes:
    values = snapshot.model_dump(mode="json")
    values.update(values.pop("metrics"))
    for name in ("fields", "quality"):
        values[name] = json.dumps(
            values[name], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    return csv_line(values[name] for name in CSV_COLUMNS)


class ExportManager:
    """Own streams for explicit disconnect and application-shutdown cleanup."""

    def __init__(self, engine: Engine):
        self.engine = engine
        self._streams: set[CSVStream] = set()
        self._closed = False

    def open(self, site_id, filters=None) -> "CSVStream":
        site_id, filters = query_id(site_id), parse_filters(filters)
        with _ADMISSION_LOCK:
            if self._closed or len(_ACTIVE) >= 2:
                raise QueryUnavailable()
            stream = CSVStream(self)
            _ACTIVE.add(stream)
            self._streams.add(stream)
        try:
            with stream._lock:
                if stream.closed:
                    raise QueryUnavailable()
                stream._open(site_id, filters)
        except BaseException:
            stream.close()
            raise
        return stream

    def close(self) -> None:
        with _ADMISSION_LOCK:
            self._closed = True
            streams = tuple(self._streams)
        failure = None
        for stream in streams:
            try:
                stream.close()
            except Exception as exc:
                failure = exc
        if failure is not None:
            raise QueryUnavailable() from None


class CSVStream(Iterator[bytes]):
    filename = CSV_FILENAME
    media_type = "text/csv; charset=utf-8"

    def __init__(self, manager):
        self._manager = manager
        self._lock = RLock()
        self._cancel_requested = Event()
        self._connection = None
        self._driver = None
        self._transaction = None
        self._result = None
        self._rows = iter(())
        self._header = True
        self.closed = False

    def _open(self, site_id, filters):
        try:
            self._connection = self._manager.engine.connect().execution_options(
                isolation_level="REPEATABLE READ"
            )
            self._driver = self._connection.connection.driver_connection
            if self._cancel_requested.is_set():
                raise QueryUnavailable()
            self._transaction = self._connection.begin()
            self._connection.execute(text("SET TRANSACTION READ ONLY"))
            self._connection.execute(text("SET LOCAL statement_timeout = '30min'"))
            require_site(self._connection, site_id)
            self._result = self._connection.execute(
                readings_statement(site_id, filters).execution_options(
                    stream_results=True, max_row_buffer=FETCH_ROWS
                )
            ).mappings()
        except SQLAlchemyError:
            raise QueryUnavailable() from None

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        with self._lock:
            if self.closed:
                raise StopIteration
            try:
                if self._header:
                    self._header = False
                    return csv_line(CSV_COLUMNS)
                row = next(self._rows, None)
                if row is None:
                    rows = self._result.fetchmany(FETCH_ROWS)
                    if not rows:
                        self.close()
                        raise StopIteration
                    self._rows = iter(rows)
                    row = next(self._rows)
                return snapshot_csv(load_snapshot(row).snapshot)
            except BaseException:
                self.close()
                raise

    def close(self) -> None:
        # psycopg cancellation is thread-safe and must run before waiting for a
        # worker that holds our lock while blocked in a server-side FETCH.
        self._cancel_requested.set()
        if not self._lock.acquire(blocking=False):
            driver = self._driver
            if driver is not None:
                try:
                    driver.cancel_safe(timeout=5)
                except Exception:
                    driver.close()
            self._lock.acquire()
        try:
            if self.closed:
                return
            self.closed = True
            try:
                if self._result is not None:
                    self._result.close()
            finally:
                try:
                    if self._transaction is not None:
                        self._transaction.rollback()
                finally:
                    try:
                        if self._connection is not None:
                            self._connection.close()
                    finally:
                        self._rows = iter(())
                        with _ADMISSION_LOCK:
                            _ACTIVE.discard(self)
                            self._manager._streams.discard(self)
        finally:
            self._lock.release()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
