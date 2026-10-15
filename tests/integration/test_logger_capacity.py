"""Opt-in full four-week synthetic capacity and preservation fixture acceptance."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from watt_profile.contracts.domain import canonical_snapshot_bytes, decode_snapshot
from watt_profile.contracts.lifecycle import complete_session
from watt_profile.logger.capacity import build_capacity_fixture
from watt_profile.logger.identity import read_upload_token
from watt_profile.logger.storage.database import create_database
from watt_profile.logger.storage.repository import LoggerRepository

pytestmark = pytest.mark.skipif(
    os.environ.get("WATT_TEST_M4_CAPACITY") != "1"
    or not any("tests/integration" in arg for arg in sys.argv[1:]),
    reason="Set WATT_TEST_M4_CAPACITY=1 and explicitly select tests/integration",
)


def test_full_representative_capacity_and_private_cleanup(tmp_path: Path) -> None:
    output = tmp_path / "capacity"
    result = build_capacity_fixture(output)
    assert result["records_requested"] == 241_920
    assert result["records_pending_after_one_confirmation"] == 241_720
    assert result["representative_snapshot_canonical_bytes"] > 0
    assert result["total_canonical_bytes"] >= (
        result["representative_snapshot_canonical_bytes"] * 241_920
    )
    assert result["installation_metadata_bytes"] > 0
    assert result["required_bytes_with_metadata_and_64_mib_reserve"] >= 64 * 1024 * 1024
    assert result["near_limit_snapshot_bytes"] > 15_000
    near_sample = result["near_limit_sample"]
    assert near_sample["records"] == 500
    assert near_sample["canonical_snapshot_bytes"] == result["near_limit_snapshot_bytes"]
    assert near_sample["database_bytes"] >= near_sample["dbstat_bytes"]
    canonical_only_estimate = (
        result["database_bytes"]
        + (result["near_limit_snapshot_bytes"] - result["representative_snapshot_canonical_bytes"])
        * result["records_requested"]
    )
    assert result["near_limit_estimated_database_bytes"] > canonical_only_estimate
    assert not (output / "near-limit-sample").exists()
    assert result["allocation_fit"] == "pending_owner_allocation"
    data_dir = output / "installation"
    token_path = data_dir / "upload-token"
    assert output.stat().st_mode & 0o777 == 0o700
    assert data_dir.stat().st_mode & 0o777 == 0o700
    assert token_path.stat().st_mode & 0o777 == 0o600
    assert (data_dir / "logger.sqlite3").stat().st_mode & 0o777 == 0o600
    assert (output / "capacity.json").stat().st_mode & 0o777 == 0o600
    assert len(read_upload_token(data_dir)) == 43
    with pytest.raises(FileExistsError):
        build_capacity_fixture(output, 1)
    assert output.exists() and (output / "installation" / "logger.sqlite3").is_file()

    database = output / "installation" / "logger.sqlite3"
    engine = create_database(database)
    try:
        repository = LoggerRepository(engine)
        status = repository.get_status()
        assert status.pending_count == 241_720
        installation = repository.load_installation()
        session_id = installation.lifecycle.session.descriptor.session_id
        assert repository.cleanup_completed((session_id,)) == 0
        assert repository.get_status().pending_count == 241_720
        group = repository.select_pending_group()
        first, last = group.batch.records[0], group.batch.records[-1]
        assert first.device_id == installation.device_id
        assert first.site_id == group.batch.session.site_id
        assert first.measurement_point_id == group.batch.source.measurement_point_id
        assert last.received_at > first.received_at
        assert int(last.metrics.energy_import_t1_kwh) > int(first.metrics.energy_import_t1_kwh)
        assert len(first.fields) == 19
        assert all(value is not None for value in first.metrics.model_dump().values())
        assert first.quality.integrity == "valid" and first.quality.frame_length == 512
        assert first.fields[-1].redacted and first.fields[-1].raw_values == ("<redacted>",)
        assert all(
            canonical_snapshot_bytes(decode_snapshot(encoded)) == encoded
            for encoded in group.canonical_records
        )
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM snapshots")) == 241_920
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM snapshots WHERE upload_state='uploaded'")
                )
                == 200
            )
        repository.commit_deployment_change(
            session_id,
            complete_session(repository.load_lifecycle(), session_id, first.received_at).lifecycle,
        )
        assert repository.cleanup_completed((session_id,)) == 200
        assert repository.get_status().pending_count == 241_720
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM snapshots")) == 241_720
            preserved = connection.execute(
                text(
                    "SELECT canonical_snapshot FROM snapshots WHERE record_id=:record_id "
                    "AND upload_state='pending'"
                ),
                {"record_id": str(first.record_id)},
            ).scalar_one()
            assert preserved.encode("utf-8") == group.canonical_records[0]
    finally:
        engine.dispose()
    shutil.rmtree(output)
    assert not output.exists()
