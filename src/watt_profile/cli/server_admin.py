"""Server administration entrypoint skeleton."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from watt_profile.server.bootstrap import (
    create_password_file,
    initialize_storage,
    set_private_umask,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage Watt Profile Server.")
    commands = parser.add_subparsers(dest="command", required=True)
    password = commands.add_parser(
        "generate-password", help="Create an administrator password file."
    )
    password.add_argument("--output", type=Path, required=True)
    commands.add_parser("migrate", help="Upgrade the Server database.")
    args = parser.parse_args(argv)
    set_private_umask()
    if args.command == "generate-password":
        try:
            value = create_password_file(args.output)
        except FileExistsError:
            print("password file already exists", file=sys.stderr)
            return 1
        except OSError:
            print("unable to create password file", file=sys.stderr)
            return 1
        print(value)
        return 0
    from watt_profile.server.settings import ServerSettings, SettingsError
    from watt_profile.server.storage.database import MigrationError

    try:
        engine = initialize_storage(ServerSettings.from_env())
    except (SettingsError, MigrationError):
        print("unable to migrate Server database", file=sys.stderr)
        return 1
    engine.dispose()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
