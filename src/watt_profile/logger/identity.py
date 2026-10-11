"""Installation credentials; callers hold the installation lock through startup."""

from __future__ import annotations

import os
import re
import secrets
import stat
import tempfile
from datetime import datetime
from pathlib import Path

from sqlalchemy.exc import MultipleResultsFound, NoResultFound

from watt_profile.contracts.domain import new_id
from watt_profile.logger.bootstrap import prepare_private_directory
from watt_profile.logger.storage.repository import Installation, LoggerRepository


class InstallationInconsistent(RuntimeError):
    def __init__(self):
        super().__init__("installation_inconsistent")


def read_upload_token(data_dir: Path) -> str:
    """Private credential access for registration/backup, never ordinary status."""
    return _read_upload_token(data_dir)


def _read_upload_token(data_dir: Path, *, durable: bool = False) -> str:
    descriptor = None
    try:
        descriptor = os.open(data_dir / "upload-token", os.O_RDONLY | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise InstallationInconsistent()
        content = os.read(descriptor, 44)
        if re.fullmatch(rb"[A-Za-z0-9_-]{43}", content) is None:
            raise InstallationInconsistent()
        if durable:
            os.fsync(descriptor)
        return content.decode("ascii")
    except (OSError, UnicodeError):
        raise InstallationInconsistent() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _sync_directory(data_dir: Path) -> None:
    directory = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _publish_token(data_dir: Path) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".upload-token-", dir=data_dir)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(secrets.token_urlsafe(32).encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, data_dir / "upload-token")
        _sync_directory(data_dir)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_or_initialize_installation(
    repository: LoggerRepository, data_dir: Path, now: datetime
) -> Installation:
    """Under installation_lock, publish the Token before committing the singleton.

    A published credential from an interrupted initial commit is reused. Hidden
    temporary files are never credentials. A committed identity is never replaced.
    """
    prepare_private_directory(data_dir)
    try:
        installation = repository.load_installation()
    except (ValueError, LookupError, NoResultFound, MultipleResultsFound):
        raise InstallationInconsistent() from None
    token_path = data_dir / "upload-token"
    if installation is not None:
        read_upload_token(data_dir)
        return installation
    if not token_path.exists() and not token_path.is_symlink():
        _publish_token(data_dir)
        read_upload_token(data_dir)
    else:
        # An interrupted rename may be visible without a completed durability barrier.
        _read_upload_token(data_dir, durable=True)
        _sync_directory(data_dir)
    return repository.initialize_installation(new_id(), now)
