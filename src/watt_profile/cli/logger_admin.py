"""Offline Logger administration commands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from watt_profile.logger.bootstrap import create_password_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage standalone Logger credentials.")
    commands = parser.add_subparsers(dest="command", required=True)
    password = commands.add_parser("generate-password", help="Create an operator password file.")
    password.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        value = create_password_file(args.path)
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
