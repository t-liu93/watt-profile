"""Exercise CI ownership and cleanup failures with fake commands, never real Docker."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mode", ["collision", "startup_failure", "success"])
def test_ci_preserves_existing_resources_and_cleans_owned_project(tmp_path, mode):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = bin_dir / "ci-python"
    python.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "if sys.argv[1:2] == ['-c']:\n"
        "    print('owned1234' if 'secrets' in sys.argv[2] else '54321')\n"
    )
    python.chmod(0o700)
    docker = bin_dir / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        """import json, os, sys
from pathlib import Path
args = sys.argv[1:]
root = Path(os.environ['FAKE_ROOT'])
with (root / 'commands.jsonl').open('a') as stream:
    stream.write(json.dumps(args) + '\\n')
mode = os.environ['FAKE_MODE']
if args[:2] == ['container', 'ls'] and mode == 'collision':
    print('existing-stopped-container')
elif args[:2] == ['image', 'inspect']:
    sys.exit(0 if (root / 'started').exists() else 1)
elif args[0] == 'compose' and 'up' in args:
    (root / 'started').touch()
    if mode == 'startup_failure':
        sys.exit(17)
"""
    )
    docker.chmod(0o700)
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        WATT_CI_PYTHON=str(python),
        TMPDIR=str(tmp_path),
        FAKE_ROOT=str(tmp_path),
        FAKE_MODE=mode,
    )
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/ci.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    commands = [json.loads(line) for line in (tmp_path / "commands.jsonl").read_text().splitlines()]
    assert not list(tmp_path.glob("watt-ci.*")), result.stderr
    assert ["container", "ls", "--all"] == commands[0][:3]
    if mode == "collision":
        assert result.returncode != 0
        assert "Refusing existing project" in result.stderr
        assert not any("down" in command or command[:2] == ["image", "rm"] for command in commands)
    else:
        assert result.returncode == (17 if mode == "startup_failure" else 0), result.stderr
        down = next(command for command in commands if "down" in command)
        assert down[2:4] == ["watt-m2-ci-owned1234", "-f"]
        assert "--volumes" in down
        assert ["image", "rm", "watt-profile-logger-dev:watt-m2-ci-owned1234"] in commands
        assert all("prune" not in command for command in commands)
