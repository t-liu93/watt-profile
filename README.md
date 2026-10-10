# Watt Profile

> **English — source of truth** · [中文](README_zh.md)

Independent Python package for the Watt Profile P1 protocol and contracts.

## Development environment

Use Python 3.12 and create a unique isolated virtual environment outside the checkout:

```bash
M1_VENV="$(mktemp -d /tmp/watt-profile-m1-venv.XXXXXX)"
python3.12 -m venv "$M1_VENV"
"$M1_VENV/bin/python" -m pip install -r requirements-dev.lock
"$M1_VENV/bin/python" -m pip install --no-build-isolation --no-deps -e .
"$M1_VENV/bin/python" -m pytest tests/test_package.py -q
"$M1_VENV/bin/python" -m ruff check .
"$M1_VENV/bin/python" -m ruff format --check .
```

Regenerate the locked development dependencies with pip-tools:

```bash
M1_PIP_CACHE="$(mktemp -d /tmp/watt-profile-pip-tools-cache.XXXXXX)"
PIP_TOOLS_CACHE_DIR="$M1_PIP_CACHE" "$M1_VENV/bin/python" -m piptools compile --extra dev --allow-unsafe --resolver=backtracking --output-file requirements-dev.lock pyproject.toml
```

Keep the environment through M1-08, then clean up exactly the directories created by this shell:

```bash
rm -rf -- "$M1_VENV" "$M1_PIP_CACHE"
```
