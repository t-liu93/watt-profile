"""Run inside a native release image, using only its installed dependencies."""

import importlib
import os
import platform
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from uuid import uuid4


def check_logger_storage(root: Path) -> None:
    from watt_profile.contracts.domain import Metrics, Quality, Snapshot, canonical_snapshot_bytes
    from watt_profile.contracts.lifecycle import new_deployment
    from watt_profile.logger.bootstrap import installation_lock
    from watt_profile.logger.identity import load_or_initialize_installation, read_upload_token
    from watt_profile.logger.storage.database import create_database, migrate
    from watt_profile.logger.storage.repository import LoggerRepository, Site

    now = datetime.now(UTC)
    directory = root / "installation"
    with installation_lock(directory):
        engine = create_database(directory / "logger.sqlite3")
        try:
            migrate(engine)
            repo = LoggerRepository(engine)
            installation = load_or_initialize_installation(repo, directory, now)
            token = read_upload_token(directory)
            site = repo.save_site(Site(site_id=uuid4(), address="Synthetic image smoke"))
            repo.commit_deployment_change(
                None, new_deployment(repo.load_lifecycle(), site.site_id, now).lifecycle
            )
            state = repo.load_lifecycle()
            snapshot = Snapshot(
                record_id=uuid4(),
                device_id=installation.device_id,
                site_id=site.site_id,
                session_id=state.session.descriptor.session_id,
                source_id=state.source.descriptor.source_id,
                measurement_point_id=state.source.descriptor.measurement_point_id,
                received_at=now,
                sampled_at=now,
                meter_time_raw=None,
                meter_time_utc=None,
                metrics=Metrics(**dict.fromkeys(Metrics.model_fields)),
                fields=(),
                quality=Quality(frame_length=120, issues=()),
            )
            assert repo.insert_snapshot(snapshot)
        finally:
            engine.dispose()
        # Reopen the installed migration/database stack, preserving pending data and identity.
        engine = create_database(directory / "logger.sqlite3")
        try:
            migrate(engine)
            repo = LoggerRepository(engine)
            reopened = load_or_initialize_installation(repo, directory, now)
            assert reopened.device_id == installation.device_id
            assert repo.load_lifecycle() == state
            assert read_upload_token(directory) == token
            assert repo.get_status().pending_count == 1
            assert repo.select_pending_group().canonical_records == (
                canonical_snapshot_bytes(snapshot),
            )
        finally:
            engine.dispose()


def main() -> None:
    component, architecture = sys.argv[1:]
    assert component in {"logger", "server"}
    assert platform.machine() in {"amd64": {"x86_64"}, "arm64": {"aarch64"}}[architecture]
    assert os.getuid() == os.getgid() == 10001
    # Exercise native binary wheels on each architecture as well as packaged web assets.
    importlib.import_module("psycopg")
    importlib.import_module(f"watt_profile.{component}.app")
    package = files("watt_profile").joinpath(component)
    for name in ("templates/login.html", f"static/{component}.js"):
        assert package.joinpath(name).is_file(), name
    assert package.joinpath("storage/migrations/env.py").is_file()
    subprocess.run(
        [sys.executable, "-m", f"watt_profile.cli.{component}", "--help"],
        check=True,
        capture_output=True,
        timeout=10,
    )
    if component == "logger":
        with tempfile.TemporaryDirectory(prefix="watt-image-smoke-") as temporary:
            check_logger_storage(Path(temporary))
    print(f"Passed {component}/{architecture}: native imports, packaged assets, UID and CLI")


if __name__ == "__main__":
    main()
