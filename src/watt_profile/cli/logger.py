"""Standalone Logger application entry point."""

from __future__ import annotations

import argparse
import sys

from watt_profile.logger.bootstrap import InstanceBusy, installation_lock, set_private_umask
from watt_profile.logger.settings import LoggerSettings, SettingsError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the standalone Watt Profile Logger.")
    parser.parse_args(argv)
    try:
        settings = LoggerSettings.from_env()
        set_private_umask()
        with installation_lock(settings.data_dir):
            print("Logger application service is not available yet.", file=sys.stderr)
            return 2
    except (SettingsError, InstanceBusy) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
