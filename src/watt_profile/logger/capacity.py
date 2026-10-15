"""Bounded synthetic four-week Logger capacity fixture construction."""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import insert, text

from watt_profile.contracts.domain import (
    MAX_SNAPSHOT_BYTES,
    Metrics,
    Quality,
    SanitizedField,
    SessionDescriptor,
    Snapshot,
    SourceDescriptor,
    canonical_snapshot_bytes,
)
from watt_profile.contracts.lifecycle import new_deployment
from watt_profile.logger.bootstrap import set_private_umask
from watt_profile.logger.identity import load_or_initialize_installation
from watt_profile.logger.storage import schema as db
from watt_profile.logger.storage.database import create_database, migrate
from watt_profile.logger.storage.repository import LoggerRepository, Site, _projection
from watt_profile.p1.profile import METRIC_CODES

_BATCH_SIZE = 500
_RESERVE_BYTES = 64 * 1024 * 1024


def _representative_snapshot(
    device_id: UUID,
    session: SessionDescriptor,
    source: SourceDescriptor,
    received: datetime,
    index: int,
) -> Snapshot:
    metrics = Metrics(
        energy_import_t1_kwh=str(index + 1),
        energy_import_t2_kwh=str(index // 2 + 2),
        energy_export_t1_kwh="0.125",
        energy_export_t2_kwh="0.25",
        power_import_kw="1.2300",
        power_export_kw="0",
        voltage_l1_v="230.1",
        voltage_l2_v="229.8",
        voltage_l3_v="230.4",
        current_l1_a="5.1",
        current_l2_a="4.9",
        current_l3_a="5.0",
        power_import_l1_kw="0.41",
        power_import_l2_kw="0.40",
        power_import_l3_kw="0.42",
        power_export_l1_kw="0",
        power_export_l2_kw="0",
        power_export_l3_kw="0",
    )
    fields = tuple(
        SanitizedField(
            code=code,
            raw_values=(f"{format(getattr(metrics, name), 'f')}*{unit}",),
            value=getattr(metrics, name),
            unit=unit,
            redacted=False,
        )
        for name, (code, unit) in METRIC_CODES.items()
    ) + (
        SanitizedField(
            code="0-0:96.1.1", raw_values=("<redacted>",), value=None, unit=None, redacted=True
        ),
    )
    return Snapshot(
        record_id=uuid4(),
        device_id=device_id,
        site_id=session.site_id,
        session_id=session.session_id,
        source_id=source.source_id,
        measurement_point_id=source.measurement_point_id,
        meter_time_raw=None,
        meter_time_utc=None,
        received_at=received,
        sampled_at=received + timedelta(seconds=9),
        metrics=metrics,
        fields=fields,
        quality=Quality(frame_length=512, issues=()),
    )


def _near_limit_size(
    device_id: UUID,
    session: SessionDescriptor,
    source: SourceDescriptor,
    now: datetime,
) -> int:
    sample = _representative_snapshot(device_id, session, source, now, 0)
    data = sample.model_dump(mode="json")
    metrics = dict(data["metrics"])
    low, high = 1, MAX_SNAPSHOT_BYTES
    best = len(canonical_snapshot_bytes(sample))
    while low <= high:
        digits = (low + high) // 2
        metrics["energy_import_t1_kwh"] = "1" + "0" * digits
        data["metrics"] = metrics
        try:
            candidate = Snapshot.model_validate(data)
            best = len(canonical_snapshot_bytes(candidate))
            low = digits + 1
        except ValueError:
            high = digits - 1
    return best


def _near_limit_snapshot(
    device_id: UUID,
    session: SessionDescriptor,
    source: SourceDescriptor,
    now: datetime,
) -> Snapshot:
    sample = _representative_snapshot(device_id, session, source, now, 0)
    data = sample.model_dump(mode="json")
    metrics = dict(data["metrics"])
    low, high = 1, MAX_SNAPSHOT_BYTES
    best = sample
    while low <= high:
        digits = (low + high) // 2
        metrics["energy_import_t1_kwh"] = "1" + "0" * digits
        data["metrics"] = metrics
        try:
            candidate = Snapshot.model_validate(data)
            canonical_snapshot_bytes(candidate)
            best = candidate
            low = digits + 1
        except ValueError:
            high = digits - 1
    return best


def _measure_near_limit_storage(
    output: Path,
    device_id: UUID,
    session: SessionDescriptor,
    source: SourceDescriptor,
    now: datetime,
    records: int = 500,
) -> dict[str, int]:
    """Measure valid near-limit rows including SQLite projections and indexes."""
    sample_dir = output / "near-limit-sample"
    sample_dir.mkdir(mode=0o700)
    database_path = sample_dir / "logger.sqlite3"
    engine = create_database(database_path)
    try:
        migrate(engine)
        repo = LoggerRepository(engine)
        load_or_initialize_installation(repo, sample_dir, now)
        sample_device_id = repo.load_installation().device_id
        sample_site = repo.save_site(Site(site_id=uuid4(), address="Synthetic near-limit sample"))
        repo.commit_deployment_change(
            None, new_deployment(repo.load_lifecycle(), sample_site.site_id, now).lifecycle
        )
        sample_state = repo.load_lifecycle()
        sample_session = sample_state.session.descriptor
        sample_source = sample_state.source.descriptor
        near = _near_limit_snapshot(sample_device_id, sample_session, sample_source, now)
        near.check_bindings(sample_session, sample_source)
        canonical_bytes = len(canonical_snapshot_bytes(near))
        for offset in range(0, records, _BATCH_SIZE):
            batch = []
            for index in range(offset, min(offset + _BATCH_SIZE, records)):
                row = near.model_copy(
                    update={
                        "record_id": uuid4(),
                        "received_at": now + timedelta(seconds=index * 10),
                    }
                )
                row.check_bindings(sample_session, sample_source)
                batch.append({**_projection(row), "upload_state": "pending", "uploaded_at": None})
            with engine.begin() as connection:
                connection.execute(insert(db.snapshots), batch)
        with engine.connect() as connection:
            page_size = int(connection.scalar(text("PRAGMA page_size")))
            page_count = int(connection.scalar(text("PRAGMA page_count")))
            dbstat = connection.execute(
                text("SELECT coalesce(sum(pgsize), 0) FROM dbstat")
            ).scalar_one()
        engine.dispose()
        database_bytes = database_path.stat().st_size
        return {
            "records": records,
            "canonical_snapshot_bytes": canonical_bytes,
            "database_bytes": database_bytes,
            "sqlite_page_bytes": page_size * page_count,
            "dbstat_bytes": int(dbstat),
        }
    finally:
        engine.dispose()
        shutil.rmtree(sample_dir)


def build_capacity_fixture(output: Path, records: int = 241_920) -> dict[str, object]:
    """Create a private synthetic installation and machine-readable measurements."""
    if not output.is_absolute():
        raise ValueError("output_must_be_absolute")
    if type(records) is not int or records < 1:
        raise ValueError("invalid_record_count")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    set_private_umask()
    data_dir = output / "installation"
    data_dir.mkdir(mode=0o700)
    database_path = data_dir / "logger.sqlite3"
    engine = create_database(database_path)
    started = time.perf_counter()
    try:
        migrate(engine)
        repo = LoggerRepository(engine)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        load_or_initialize_installation(repo, data_dir, now)
        device_id = repo.load_installation().device_id
        site = repo.save_site(Site(site_id=uuid4(), address="Synthetic capacity fixture"))
        repo.commit_deployment_change(
            None, new_deployment(repo.load_lifecycle(), site.site_id, now).lifecycle
        )
        state = repo.load_lifecycle()
        descriptor, source = state.session.descriptor, state.source.descriptor
        near_limit_bytes = _near_limit_size(device_id, descriptor, source, now)
        near_limit_storage = _measure_near_limit_storage(output, device_id, descriptor, source, now)
        representative_bytes = len(
            canonical_snapshot_bytes(
                _representative_snapshot(device_id, descriptor, source, now, 0)
            )
        )
        wal, shm = Path(str(database_path) + "-wal"), Path(str(database_path) + "-shm")
        wal_peak = shm_peak = 0
        for offset in range(0, records, _BATCH_SIZE):
            rows = []
            for index in range(offset, min(offset + _BATCH_SIZE, records)):
                received = now + timedelta(seconds=index * 10)
                snapshot = _representative_snapshot(device_id, descriptor, source, received, index)
                snapshot.check_bindings(descriptor, source)
                rows.append(
                    {**_projection(snapshot), "upload_state": "pending", "uploaded_at": None}
                )
            with engine.begin() as connection:
                connection.execute(insert(db.snapshots), rows)
            if wal.exists():
                wal_peak = max(wal_peak, wal.stat().st_size)
            if shm.exists():
                shm_peak = max(shm_peak, shm.stat().st_size)

        construction_seconds = time.perf_counter() - started
        selection_started = time.perf_counter()
        selected = repo.select_pending_group()
        selection_seconds = time.perf_counter() - selection_started
        status_started = time.perf_counter()
        repo.get_status(free_bytes=shutil.disk_usage(output).free)
        status_seconds = time.perf_counter() - status_started
        confirmation_seconds = None
        if selected is not None:
            from watt_profile.contracts.upload import UploadConfirmation

            confirm_started = time.perf_counter()
            repo.apply_confirmation(
                selected.batch,
                UploadConfirmation(
                    schema_version=1,
                    device_id=selected.batch.device_id,
                    confirmed_record_ids=tuple(item.record_id for item in selected.batch.records),
                ),
                now + timedelta(days=28),
            )
            confirmation_seconds = time.perf_counter() - confirm_started
        status_after_confirmation = repo.get_status(free_bytes=shutil.disk_usage(output).free)
        with engine.connect() as connection:
            page_count = connection.scalar(text("PRAGMA page_count"))
            page_size = connection.scalar(text("PRAGMA page_size"))
            total_canonical_bytes = connection.scalar(
                text("SELECT coalesce(sum(length(canonical_snapshot)), 0) FROM snapshots")
            )
            indexes = connection.execute(text("PRAGMA index_list('snapshots')")).all()
            try:
                dbstat = connection.execute(
                    text("SELECT name, sum(pgsize) FROM dbstat GROUP BY name ORDER BY name")
                ).all()
            except Exception:
                dbstat = []
        engine.dispose()
        database_bytes = database_path.stat().st_size
        observed_peak = database_bytes + wal_peak + shm_peak
        installation_metadata_bytes = sum(
            path.stat().st_size
            for path in data_dir.iterdir()
            if path.is_file() and path not in {database_path, wal, shm}
        )
        result: dict[str, object] = {
            "format_version": 1,
            "synthetic": True,
            "records_requested": records,
            "records_pending_after_one_confirmation": status_after_confirmation.pending_count,
            "representative_snapshot_canonical_bytes": representative_bytes,
            "total_canonical_bytes": total_canonical_bytes,
            "installation_metadata_bytes": installation_metadata_bytes,
            "database_bytes": database_bytes,
            "wal_peak_bytes": wal_peak,
            "wal_final_bytes": wal.stat().st_size if wal.exists() else 0,
            "shm_peak_bytes": shm_peak,
            "shm_final_bytes": shm.stat().st_size if shm.exists() else 0,
            "sqlite_page_size": page_size,
            "sqlite_page_count": page_count,
            "sqlite_page_bytes": page_size * page_count,
            "indexes": [
                {"seq": seq, "name": name, "unique": unique} for seq, name, unique, *_ in indexes
            ],
            "dbstat_pages_by_object": [{"name": name, "bytes": size} for name, size in dbstat],
            "free_bytes_after_fixture": shutil.disk_usage(output).free,
            "observed_peak_storage_bytes": observed_peak,
            "required_bytes_with_metadata_and_64_mib_reserve": (
                observed_peak + installation_metadata_bytes + _RESERVE_BYTES
            ),
            "timings_seconds": {
                "construction": construction_seconds,
                "selection": selection_seconds,
                "status": status_seconds,
                "confirmation": confirmation_seconds,
            },
            "near_limit_snapshot_bytes": near_limit_bytes,
            "near_limit_sample": near_limit_storage,
            "near_limit_estimated_database_bytes": round(
                database_bytes
                + max(
                    0,
                    near_limit_storage["database_bytes"] / near_limit_storage["records"]
                    - database_bytes / records,
                )
                * records
            ),
            "storage_allocation_bytes": None,
            "allocation_fit": "pending_owner_allocation",
        }
        (output / "capacity.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        os.chmod(output / "capacity.json", 0o600)
        return result
    except BaseException:
        engine.dispose()
        shutil.rmtree(output)
        raise
