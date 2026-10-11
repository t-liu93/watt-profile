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
