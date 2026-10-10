import os
import subprocess
import sys
from pathlib import Path


def test_editable_package_imports_outside_checkout(tmp_path: Path) -> None:
    checkout = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import watt_profile; print(watt_profile.__file__)",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    imported_file = Path(result.stdout.strip()).resolve()
    assert imported_file.is_relative_to(checkout / "src" / "watt_profile")
