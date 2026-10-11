"""Standalone Logger application entry point."""

from __future__ import annotations

import argparse
import sys

import uvicorn

from watt_profile.logger.app import create_app
from watt_profile.logger.bootstrap import InstanceBusy
from watt_profile.logger.settings import LoggerSettings, SettingsError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the standalone Watt Profile Logger.")
    parser.parse_args(argv)
    try:
        settings = LoggerSettings.from_env()
        uvicorn.run(
            create_app(settings),
            host=settings.host,
            port=settings.port,
            workers=1,
            reload=False,
            access_log=False,
            proxy_headers=False,
        )
        return 0
    except (SettingsError, InstanceBusy) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
