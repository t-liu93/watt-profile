"""Release input boundaries; no network, registry, meter or deployment credentials."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_release_tag.py"
spec = importlib.util.spec_from_file_location("check_release_tag", SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v0.1.0", ("0.1.0", "0.1.0", True)),
        ("v1.2.3-rc.1", ("1.2.3-rc.1", "1.2.3-rc.1", False)),
        ("v1.2.3+build.01", ("1.2.3+build.01", "1.2.3_build.01", True)),
        ("v1.2.3-rc.1+build.2", ("1.2.3-rc.1+build.2", "1.2.3-rc.1_build.2", False)),
    ],
)
def test_release_classification(tag, expected):
    assert release.classify(tag) == expected


@pytest.mark.parametrize(
    "tag",
    [
        "1.2.3",
        "v1.2",
        "v01.2.3",
        "v1.02.3",
        "v1.2.03",
        "v1.2.3-01",
        "v1.2.3-rc..1",
        "v1.2.3+",
        "v1.2.3-",
        "v1.2.3\nstable=true",
        "v1.2.3;echo unsafe",
        "v1.2.3+" + "a" * 123,
    ],
)
def test_reject_invalid_or_unsafe_release_tag(tag):
    with pytest.raises(ValueError):
        release.classify(tag)


def test_docker_tag_length_boundary():
    assert len(release.classify("v1.2.3+" + "a" * 122)[1]) == 128


def test_output_appends_and_rejection_writes_nothing(tmp_path):
    output = tmp_path / "github-output"
    output.write_text("source_sha=existing\n")
    subprocess.run(
        [sys.executable, str(SCRIPT), "v1.2.3-rc.1", "--github-output", str(output)],
        check=True,
    )
    expected = "source_sha=existing\nversion=1.2.3-rc.1\nimage_tag=1.2.3-rc.1\nstable=false\n"
    assert output.read_text() == expected
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "v1.2.3\nstable=true", "--github-output", str(output)],
        capture_output=True,
    )
    assert result.returncode != 0
    assert output.read_text() == expected


@pytest.mark.parametrize("component", ["logger", "server"])
@pytest.mark.parametrize("published", [False, True])
def test_standalone_image_selection(tmp_path, component, published):
    root = SCRIPT.parents[1]
    env = {key: value for key, value in os.environ.items() if not key.startswith("WATT_")}
    env.update(
        WATT_LOGGER_OPERATOR_USER="synthetic",
        WATT_LOGGER_OPERATOR_PASSWORD_FILE=str(tmp_path / "password"),
        WATT_LOGGER_SERIAL_DEVICE="/dev/synthetic-never-opened",
        WATT_LOGGER_SERIAL_GID="1000",
        WATT_LOGGER_BIND_ADDRESS="127.0.0.1",
        WATT_LOGGER_PUBLISHED_PORT="18080",
        WATT_SERVER_ADMIN_USER="synthetic",
        WATT_SERVER_PUBLIC_ORIGIN="https://synthetic.example.invalid",
        WATT_SERVER_SECRET_DIR=str(tmp_path / "server"),
        WATT_SERVER_DB_SECRET_DIR=str(tmp_path / "db"),
    )
    expected = f"watt-profile-{component}:local"
    if published:
        expected = f"ghcr.io/synthetic/watt-profile-{component}:1.2.3"
        env[f"WATT_{component.upper()}_IMAGE"] = expected
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            "watt-ci-config",
            "-f",
            str(root / "deploy" / component / "compose.yml"),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    services = json.loads(result.stdout)["services"]
    assert services[component]["image"] == expected
    if component == "logger":
        assert services["logger-init"]["image"] == expected
