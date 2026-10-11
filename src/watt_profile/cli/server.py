"""Single-worker Server process entrypoint."""

from __future__ import annotations

import argparse
import sys

from watt_profile.server.bootstrap import set_private_umask
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
    import uvicorn

    from watt_profile.server.app import create_app

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        workers=1,
        reload=False,
        proxy_headers=False,
        access_log=False,
        timeout_graceful_shutdown=30,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
