"""Server bootstrap helpers with no import-time side effects."""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path


def set_private_umask() -> None:
    os.umask(0o077)


def create_password_file(path: Path) -> str:
    """Exclusively create a private administrator password file."""
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
