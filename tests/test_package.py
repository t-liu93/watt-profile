from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_installed_wheel_includes_logger_resources(tmp_path: Path) -> None:
    # setuptools writes build/ and egg-info beside the project being built.
    # Keep those side effects in the disposable test directory too.
    checkout = tmp_path / "checkout"
    shutil.copytree(
        Path(__file__).resolve().parents[1],
        checkout,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            "__pycache__",
            "*.egg-info",
            "build",
            "development-notes",
            ".pytest_cache",
            ".ruff_cache",
            ".env*",
            "*.secret",
            "*.sqlite3*",
        ),
    )
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_dir),
            str(checkout),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(wheel_dir.glob("watt_profile-*.whl"))
    install_venv = tmp_path / "installed-venv"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "venv",
            str(install_venv),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    installed_python = install_venv / "bin" / "python"
    subprocess.run(
        [
            str(installed_python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            str(wheel),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    script = """
from importlib.resources import files
root = files('watt_profile')
assert root.joinpath('logger/templates/login.html').is_file()
assert root.joinpath('logger/templates/index.html').is_file()
assert root.joinpath('logger/static/logger.css').is_file()
assert root.joinpath('logger/static/logger.js').is_file()
assert root.joinpath('logger/storage/migrations/__init__.py').is_file()
assert root.joinpath('server/__init__.py').is_file()
assert root.joinpath('server/settings.py').is_file()
assert root.joinpath('server/storage/migrations/__init__.py').is_file()
assert root.joinpath('server/templates/base.html').is_file()
assert root.joinpath('server/static/server.css').is_file()
assert root.joinpath('server/static/server.js').is_file()
assert root.joinpath('server/static/theme.js').is_file()
assert root.joinpath('server/static/login.js').is_file()
assert root.joinpath('server/templates/login.html').is_file()
import watt_profile.server
print(root)
"""
    result = subprocess.run(
        [str(installed_python), "-c", script],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert Path(result.stdout.strip()).is_relative_to(install_venv / "lib")
