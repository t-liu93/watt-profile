"""Storage guards and private, verified offline recovery; no network or eviction."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

from watt_profile.logger.bootstrap import installation_lock
from watt_profile.logger.identity import read_upload_token
from watt_profile.logger.storage.database import create_database, migrate
from watt_profile.logger.storage.repository import LoggerRepository

MIN_FREE_BYTES = 64 * 1024 * 1024
REVISION = "logger_0001"


def is_database_corruption(error: Exception) -> bool:
    original = getattr(error, "orig", error)
    code = getattr(original, "sqlite_errorcode", 0)
    return (code & 255) in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}


class StorageHealthGuard:
    def __init__(self, repository: LoggerRepository, free_bytes: Callable[[], int] | None = None):
        self.repository = repository
        self.directory = Path(repository.engine.url.database).parent
        self.free_bytes = free_bytes or (lambda: shutil.disk_usage(self.directory).free)

    def check_capacity(self):
        if self.free_bytes() < MIN_FREE_BYTES:
            raise OSError("storage_full")

    def __call__(self):
        self.check_capacity()
        self.repository.check_safe_write()

    def resolve(self):
        self.check_capacity()
        return self.repository.check_safe_write(resolve_storage=True)


def _canonical_ddl(sql):
    """Compare every DDL token, allowing only CHECK expression ordering to vary.

    SQLite CHECKs are row predicates, including those emitted inline on columns.
    Quoted literals remain indivisible so parentheses inside strings cannot alter
    the grouping. Keep duplicate checks and all non-CHECK tokens intact.
    """
    if sql is None:
        return None
    tokens = re.findall(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`[^`]*`|\[[^]]*\]|\w+|[^\s]", sql)
    remaining, checks = [], []
    index = 0
    while index < len(tokens):
        if tokens[index].upper() != "CHECK" or tokens[index + 1 : index + 2] != ["("]:
            remaining.append(tokens[index])
            index += 1
            continue
        start = index
        index += 1
        depth = 0
        while index < len(tokens):
            depth += (tokens[index] == "(") - (tokens[index] == ")")
            index += 1
            if depth == 0:
                break
        if depth:
            raise ValueError("backup_invalid")
        checks.append(tuple(tokens[start:index]))
        remaining.append("CHECK")
    return tuple(remaining), tuple(sorted(checks))


def _schema(engine):
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).all()
        return [(kind, name, table, _canonical_ddl(sql)) for kind, name, table, sql in rows]


def _validate(directory: Path):
    read_upload_token(directory)
    engine = create_database(directory / "logger.sqlite3")
    try:
        with engine.connect() as connection:
            if connection.exec_driver_sql("PRAGMA integrity_check").all() != [("ok",)]:
                raise ValueError("backup_invalid")
            if connection.exec_driver_sql("PRAGMA foreign_key_check").all():
                raise ValueError("backup_invalid")
            if connection.exec_driver_sql("SELECT version_num FROM alembic_version").all() != [
                (REVISION,)
            ]:
                raise ValueError("backup_invalid")
        with tempfile.TemporaryDirectory(prefix="watt-schema-") as temporary:
            reference = create_database(Path(temporary) / "reference.sqlite3")
            try:
                migrate(reference)
                if _schema(engine) != _schema(reference):
                    raise ValueError("backup_invalid")
            finally:
                reference.dispose()
        installation = LoggerRepository(engine).validate_all()
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        return installation
    finally:
        engine.dispose()


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_private(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _sync(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def backup(data_dir: Path, output: Path):
    """Online backup with a separate configured connection and completion manifest last."""
    if not data_dir.is_absolute() or not output.is_absolute():
        raise ValueError("absolute_path_required")
    if not (data_dir / "logger.sqlite3").is_file():
        raise ValueError("installation_required")
    token = read_upload_token(data_dir)
    engine = create_database(data_dir / "logger.sqlite3")
    try:
        if LoggerRepository(engine).load_installation() is None:
            raise ValueError("installation_required")
        output.mkdir(mode=0o700)
        _write_private(output / "logger.sqlite3", b"")
        with engine.raw_connection() as source:
            destination = sqlite3.connect(output / "logger.sqlite3")
            try:
                source.driver_connection.backup(destination, pages=128)
            finally:
                destination.close()
        _write_private(output / "upload-token", token.encode("ascii"))
        installation = _validate(output)
        for name in ("logger.sqlite3", "upload-token"):
            with (output / name).open("rb") as stream:
                os.fsync(stream.fileno())
        manifest = {
            "format_version": 1,
            "device_id": str(installation.device_id),
            "migration_revision": REVISION,
            "sha256": {name: _digest(output / name) for name in ("logger.sqlite3", "upload-token")},
        }
        _write_private(output / "manifest.json", json.dumps(manifest, sort_keys=True).encode())
        _sync(output)
    finally:
        engine.dispose()


def _regular(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("backup_invalid")


def _verified_manifest(directory):
    _regular(directory / "manifest.json")
    manifest = json.loads((directory / "manifest.json").read_bytes())
    if (
        type(manifest) is not dict
        or set(manifest) != {"format_version", "device_id", "migration_revision", "sha256"}
        or type(manifest["format_version"]) is not int
        or manifest["format_version"] != 1
        or manifest["migration_revision"] != REVISION
        or type(manifest["sha256"]) is not dict
        or set(manifest["sha256"]) != {"logger.sqlite3", "upload-token"}
    ):
        raise ValueError("backup_invalid")
    for name in ("logger.sqlite3", "upload-token"):
        _regular(directory / name)
        if _digest(directory / name) != manifest["sha256"][name]:
            raise ValueError("backup_invalid")
    installation = _validate(directory)
    if str(installation.device_id) != manifest["device_id"]:
        raise ValueError("backup_invalid")
    return manifest


def restore(backup_dir: Path, data_dir: Path):
    """Publish under a durable marker; interrupted publication rejects startup.

    The verified staging copy survives publication until both files are durable.
    Repeating restore with the same backup safely resumes an interrupted publish.
    """
    if not backup_dir.is_absolute() or not data_dir.is_absolute():
        raise ValueError("absolute_path_required")
    with installation_lock(data_dir, allow_incomplete_restore=True):
        marker = data_dir / ".restore-in-progress"
        staged = None
        try:
            if marker.exists() or marker.is_symlink():
                _regular(marker)
                stage_name = marker.read_text()
                if re.fullmatch(r"\.restore-[A-Za-z0-9_-]+", stage_name) is None:
                    raise ValueError("restore_incomplete")
                staged = data_dir / stage_name
                if staged.is_symlink() or not staged.is_dir():
                    raise ValueError("restore_incomplete")
                allowed = {"logger.lock", marker.name, stage_name, "logger.sqlite3", "upload-token"}
                if any(p.name not in allowed for p in data_dir.iterdir()):
                    raise ValueError("restore_target_not_empty")
                manifest = _verified_manifest(staged)
                _regular(backup_dir / "manifest.json")
                if json.loads((backup_dir / "manifest.json").read_bytes()) != manifest:
                    raise ValueError("restore_backup_mismatch")
            else:
                if any(p.name != "logger.lock" for p in data_dir.iterdir()):
                    raise ValueError("restore_target_not_empty")
                staged = Path(tempfile.mkdtemp(prefix=".restore-", dir=data_dir))
                for name in ("manifest.json", "logger.sqlite3", "upload-token"):
                    _regular(backup_dir / name)
                    _write_private(staged / name, (backup_dir / name).read_bytes())
                _verified_manifest(staged)
                with (staged / "logger.sqlite3").open("rb") as stream:
                    os.fsync(stream.fileno())
                _sync(staged)
                _write_private(marker, staged.name.encode("ascii"))
                _sync(data_dir)
            # Preserve the staged originals for safe retries at every publication gap.
            for name in ("upload-token", "logger.sqlite3"):
                pending = staged / (name + ".publish")
                pending.unlink(missing_ok=True)
                _write_private(pending, (staged / name).read_bytes())
                os.replace(pending, data_dir / name)
            _sync(data_dir)
            marker.unlink()
            _sync(data_dir)
        finally:
            if staged is not None and not marker.exists() and not marker.is_symlink():
                shutil.rmtree(staged)
