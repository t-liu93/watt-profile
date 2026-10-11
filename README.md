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

`docker-compose.dev.yml` runs synthetic bytes through the real parser with the development-only account `operator` / `testtesttesttest`, a private disposable project volume and loopback port (default 8080, override with `WATT_DEV_PORT`). Choose a unique `watt-m2-...` project and `WATT_DEV_IMAGE_TAG`; run Compose/browser tests explicitly. It attaches no host devices or server services.

`deploy/logger/compose.yml` is standalone serial deployment with required external operator password file and explicit device/GID mapping; copy [the sanitized example](deploy/logger/.env.example) to ignored `.env.local` and replace its placeholders. Application UID/GID is 10001, the data volume is 0700, the read-only password file must be 0600 and owned by UID 10001. A scoped root initializer prepares only the mounted volume. Normal `down` preserves data. Local container execution and ARM64 build are separate checks; neither claims physical-meter acceptance.
