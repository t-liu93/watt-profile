from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from watt_profile.logger.bootstrap import (
    InstanceBusy,
    create_password_file,
    installation_lock,
    prepare_private_directory,
)
from watt_profile.logger.settings import LoggerSettings, SettingsError


def _settings_env(password_file: Path) -> dict[str, str]:
    return {
        "WATT_LOGGER_DATA_DIR": "/tmp/logger-data",
        "WATT_LOGGER_OPERATOR_USER": "operator",
        "WATT_LOGGER_OPERATOR_PASSWORD_FILE": str(password_file),
    }


def test_settings_validate_required_values_and_password(tmp_path: Path) -> None:
    password_file = tmp_path / "password"
    password_file.write_text("a-secure-password-123\n", encoding="utf-8")
    config = LoggerSettings.from_env(_settings_env(password_file))
    assert config.input_mode == "serial"
    assert config.port == 8080
    assert not config.cookie_secure

    with pytest.raises(SettingsError):
        LoggerSettings.from_env({**_settings_env(password_file), "WATT_LOGGER_INPUT": "other"})
    password_file.write_text("short\n", encoding="utf-8")
    with pytest.raises(SettingsError):
        LoggerSettings.from_env(_settings_env(password_file))


@pytest.mark.parametrize("data_dir", ["", "   ", "relative/path", "bad\x00path"])
def test_settings_reject_invalid_data_directory(tmp_path: Path, data_dir: str) -> None:
    password_file = tmp_path / "password"
    password_file.write_text("a-secure-password-123\n", encoding="utf-8")
    with pytest.raises(SettingsError, match="data directory"):
        LoggerSettings.from_env({**_settings_env(password_file), "WATT_LOGGER_DATA_DIR": data_dir})


def test_cli_rejects_empty_data_directory_without_touching_working_directory(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    work_dir.chmod(0o755)
    password_file = tmp_path / "password"
    password_file.write_text("a-secure-password-123\n", encoding="utf-8")
    before_entries = set(work_dir.iterdir())
    before_mode = work_dir.stat().st_mode & 0o777
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update(_settings_env(password_file))
    env["WATT_LOGGER_DATA_DIR"] = ""

    result = subprocess.run(
        [sys.executable, "-m", "watt_profile.cli.logger"],
        cwd=work_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "data directory" in result.stderr
    assert set(work_dir.iterdir()) == before_entries
    assert work_dir.stat().st_mode & 0o777 == before_mode


def test_password_file_is_private_and_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "operator-password"
    password = create_password_file(path)
    assert path.read_text(encoding="utf-8") == password + "\n"
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        create_password_file(path)
    assert path.read_text(encoding="utf-8") == password + "\n"


def test_private_directory_and_lock_exclusion(tmp_path: Path) -> None:
    data_dir = prepare_private_directory(tmp_path / "data")
    assert data_dir.stat().st_mode & 0o777 == 0o700
    with installation_lock(data_dir):
        with pytest.raises(InstanceBusy, match="instance_busy"):
            with installation_lock(data_dir):
                pass
    with installation_lock(data_dir):
        pass


def test_lock_released_after_holder_process_dies(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    script = (
        "import sys,time; from pathlib import Path; "
        "from watt_profile.logger.bootstrap import installation_lock; "
        "ctx=installation_lock(Path(sys.argv[1])); ctx.__enter__(); "
        "print('locked',flush=True); time.sleep(60)"
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(data_dir)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        child.kill()
        child.wait(timeout=5)
        with installation_lock(data_dir):
            pass
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_import_has_no_filesystem_effects(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import watt_profile.logger.settings, watt_profile.logger.bootstrap, "
            "watt_profile.cli.logger, watt_profile.cli.logger_admin; print('ok')",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "ok"
    assert list(tmp_path.iterdir()) == []
