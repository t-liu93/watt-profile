"""Server process entrypoint skeleton."""

from __future__ import annotations

import argparse
import sys

from watt_profile.server.bootstrap import initialize_storage, set_private_umask
from watt_profile.server.settings import ServerSettings, SettingsError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Watt Profile Server.")
    parser.parse_args(argv)
    set_private_umask()
    try:
        settings = ServerSettings.from_env()
    except SettingsError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    from watt_profile.server.storage.database import MigrationError

    try:
        engine = initialize_storage(settings)
    except MigrationError:
        print("unable to initialize Server storage", file=sys.stderr)
        return 2
    engine.dispose()
    print("Server application is not implemented yet", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
