# Watt Profile

> **English — source of truth** · [中文](README_zh.md)

Independent Python package for the Watt Profile P1 protocol and contracts.

## Development environment

Use Python 3.12 and create a unique isolated virtual environment outside the checkout. Install `requirements.lock` for the Logger runtime, or `requirements-dev.lock` to work on the project and run its checks:

```bash
M2_VENV="$(mktemp -d /tmp/watt-profile-m2-venv.XXXXXX)"
python3.12 -m venv "$M2_VENV"
"$M2_VENV/bin/python" -m pip install -r requirements-dev.lock
"$M2_VENV/bin/python" -m pip install --no-build-isolation --no-deps -e .
"$M2_VENV/bin/python" -m pytest tests/test_package.py -q
"$M2_VENV/bin/python" -m ruff check .
"$M2_VENV/bin/python" -m ruff format --check .
```

Regenerate both lock files with pip-tools. The first contains runtime dependencies; the second also includes development dependencies:

```bash
M2_PIP_CACHE="$(mktemp -d /tmp/watt-profile-pip-tools-cache.XXXXXX)"
PIP_TOOLS_CACHE_DIR="$M2_PIP_CACHE" "$M2_VENV/bin/python" -m piptools compile --allow-unsafe --resolver=backtracking --output-file requirements.lock pyproject.toml
PIP_TOOLS_CACHE_DIR="$M2_PIP_CACHE" "$M2_VENV/bin/python" -m piptools compile --extra dev --allow-unsafe --resolver=backtracking --output-file requirements-dev.lock pyproject.toml
```

Keep the environment for the current M2 work, then clean up exactly the directories created by this shell:

```bash
rm -rf -- "$M2_VENV" "$M2_PIP_CACHE"
```

## Logger containers

The Logger collects DSMR 5 locally and keeps snapshots pending; upload is not implemented in M2. See [M2 container operations and standalone setup](docs/milestone/m2.md#container-operations-m2-09) for exact development, test, ARM64 build, credential/device permission and cleanup commands.

The login page and dashboard default to dark mode. Use the header button to switch between light and dark; only the theme preference is saved in browser storage.

`docker-compose.dev.yml` runs synthetic bytes through the real parser with the development-only account `operator` / `testtesttesttest`, a private disposable project volume and loopback port (default 8080, override with `WATT_DEV_PORT`). Choose a unique `watt-m2-...` project and `WATT_DEV_IMAGE_TAG`; run Compose/browser tests explicitly. `up logger` starts only Logger dependencies and attaches no host devices.

`deploy/logger/compose.yml` is standalone serial deployment with required external operator password file and explicit device/GID mapping; copy [the sanitized example](deploy/logger/.env.example) to ignored `.env.local` and replace its placeholders. Application UID/GID is 10001, the data volume is 0700, the read-only password file must be 0600 and owned by UID 10001. A scoped root initializer prepares only the mounted volume. Normal `down` preserves data. Local container execution and ARM64 build are separate checks; neither claims physical-meter acceptance.

## Server containers

The Server implements administrator registration, authorized batch ingest, readings and CSV export; Logger upload remains M4 work. Standalone [Compose](deploy/server/compose.yml) contains only the application and PostgreSQL. It publishes one HTTP application port, default `127.0.0.1:18081` to container 8080; PostgreSQL has no host port. The owner's host/upstream Nginx manages HTTPS, certificates and reverse proxying independently. Revised external-HTTPS tests and owner acceptance remain pending in [M3](docs/milestone/m3.md).

Copy the [sanitized example](deploy/server/.env.example) to ignored `deploy/server/.env.local` and replace every placeholder. Set `WATT_SERVER_PUBLIC_ORIGIN` to the external browser origin (HTTPS behind Nginx), keep `WATT_SERVER_COOKIE_SECURE=true` for HTTPS, and select `WATT_SERVER_BIND_ADDRESS` / `WATT_SERVER_PUBLISHED_PORT` if the upstream needs a reachable interface or another port. Provide private DB/application credential directories (0700) and files (0600); application files belong to UID/GID 10001 and mount read-only. The DB directory contains `password`; the application directory contains `database-url` and `admin-password`, with matching DB credentials and no development defaults. See the [operating instructions](docs/milestone/m3.md#m3-11-deployment-operations) before starting:

```sh
docker compose --env-file deploy/server/.env.local -p watt-server -f deploy/server/compose.yml config --quiet
docker compose --env-file deploy/server/.env.local -p watt-server -f deploy/server/compose.yml up --build -d --wait --wait-timeout 120 server
docker compose --env-file deploy/server/.env.local -p watt-server -f deploy/server/compose.yml down
```

Development uses `admin` / `testtesttesttest` and loopback 18081 (`WATT_DEV_SERVER_PORT` override). Choose a new disposable `watt-m3-...` project and matching `WATT_DEV_IMAGE_TAG`; `docker compose -p PROJECT -f docker-compose.dev.yml up --build -d --wait --wait-timeout 120 server` starts only PostgreSQL/setup dependencies. Normal `down` preserves data and credentials; removal of volumes is only for disposable projects created for verification.
