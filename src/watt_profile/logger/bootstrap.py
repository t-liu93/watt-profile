"""Private filesystem helpers used during Logger startup."""

from __future__ import annotations

import fcntl
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class InstanceBusy(RuntimeError):
    """The Logger data directory is already held by another process."""


def set_private_umask() -> None:
    """Make newly created Logger files private by default for this process."""
    os.umask(0o077)


def prepare_private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


@contextmanager
def installation_lock(data_dir: Path, *, allow_incomplete_restore: bool = False) -> Iterator[int]:
    prepare_private_directory(data_dir)
    lock_path = data_dir / "logger.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstanceBusy("instance_busy") from exc
        marker = data_dir / ".restore-in-progress"
        if (marker.exists() or marker.is_symlink()) and not allow_incomplete_restore:
            raise RuntimeError("restore_incomplete")
        yield descriptor
    finally:
        os.close(descriptor)


def create_password_file(path: Path) -> str:
    """Create a fresh operator password file and return the one-time password."""
    password = secrets.token_urlsafe(24)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
            stream.write(password + "\n")
            stream.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return password
