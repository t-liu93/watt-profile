"""Offline Logger administration commands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from watt_profile.logger.bootstrap import (
    InstanceBusy,
    create_password_file,
    installation_lock,
    set_private_umask,
)
from watt_profile.logger.maintenance import backup, restore
from watt_profile.logger.storage.database import create_database, migrate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage standalone Logger installation.")
    commands = parser.add_subparsers(dest="command", required=True)
    password = commands.add_parser("generate-password", help="Create an operator password file.")
    password.add_argument("--output", type=Path, required=True)
    migration = commands.add_parser("migrate", help="Upgrade the offline Logger database.")
    migration.add_argument("--data-dir", type=Path, required=True)
    backup_command = commands.add_parser("backup", help="Create a private online backup.")
    backup_command.add_argument("--data-dir", type=Path, required=True)
    backup_command.add_argument("--output", type=Path, required=True)
    restore_command = commands.add_parser("restore", help="Restore into an empty data directory.")
    restore_command.add_argument("--backup", type=Path, required=True)
    restore_command.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command in {"backup", "restore"}:
        try:
            set_private_umask()
            if args.command == "backup":
                backup(args.data_dir, args.output)
            else:
                restore(args.backup, args.data_dir)
        except InstanceBusy:
            print("instance_busy", file=sys.stderr)
            return 1
        except Exception:
            print(args.command + "_failed", file=sys.stderr)
            return 1
        print(args.command + "_complete")
        return 0
    if args.command == "migrate":
        if not args.data_dir.is_absolute():
            print("logger data directory must be absolute", file=sys.stderr)
            return 1
        try:
            set_private_umask()
            with installation_lock(args.data_dir):
                engine = create_database(args.data_dir / "logger.sqlite3")
                try:
                    migrate(engine)
                finally:
                    engine.dispose()
        except InstanceBusy:
            print("instance_busy", file=sys.stderr)
            return 1
        except Exception:
            print("migration_failed", file=sys.stderr)
            return 1
        print("logger_0001")
        return 0
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
