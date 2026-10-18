#!/usr/bin/env python3
"""Build once, record exact bytes, and verify before staging for every consumer.

python scripts/frontend_artifact.py build
python scripts/frontend_artifact.py stage --expected <printed digest>
A supplied CI artifact is verified and staged without npm or registry access.
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from watt_profile.server.frontend_artifact import (  # noqa: E402
    METADATA,
    canonical,
    file_hashes,
    sha,
    verify,
)

FRONTEND = ROOT / "frontend"
STAGED = ROOT / "src/watt_profile/server/static/frontend"
LOCKS = ("package-lock.json", "ui-components.lock.json")


def locks():
    # Match ui.mjs fingerprint without Node when reusing an already checked build.
    inputs = [
        [name, sha((FRONTEND / name).read_bytes())]
        for name in (
            "components.json",
            "package.json",
            "package-lock.json",
            "tsconfig.json",
            "vite.config.ts",
            "scripts/ui.mjs",
        )
    ]
    fingerprint = sha(json.dumps(inputs, separators=(",", ":")).encode())
    component_lock = json.loads((FRONTEND / "ui-components.lock.json").read_text())
    if fingerprint != component_lock["configurationSHA256"]:
        raise ValueError(
            "Generation configuration fingerprint mismatch; explicitly update the lock"
        )
    return {name: sha((FRONTEND / name).read_bytes()) for name in LOCKS}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "verify", "stage"))
    parser.add_argument("--artifact", type=Path, default=FRONTEND / "dist")
    parser.add_argument("--expected")
    args = parser.parse_args()
    if args.command == "build":
        if args.artifact != FRONTEND / "dist":
            parser.error("build writes frontend/dist only")
        for tool, expected in (("node", "v24.22.0"), ("npm", "11.16.0")):
            actual = subprocess.check_output([tool, "--version"], text=True).strip()
            if actual != expected:
                raise ValueError(f"Expected {tool} {expected}, got {actual}")
        for command in (
            ["npm", "ci"],
            ["npm", "run", "ui:prepare"],
            ["npm", "run", "ui:verify"],
            ["npm", "run", "lint"],
            ["npm", "run", "typecheck"],
            ["npm", "test"],
            ["npm", "run", "build"],
        ):
            subprocess.run(command, cwd=FRONTEND, check=True)
        payload = {"format": 1, "files": file_hashes(args.artifact), "locks": locks()}
        metadata = dict(payload, digest=sha(canonical(payload)))
        (args.artifact / METADATA).write_bytes(canonical(metadata) + b"\n")
    elif not args.expected:
        parser.error("--expected is required when reusing a checked artifact")
    metadata = verify(args.artifact, args.expected)
    if metadata["locks"] != locks():
        raise ValueError("Frontend lock fingerprint mismatch; rebuild the artifact")
    if args.command == "stage":
        if args.artifact.resolve() == STAGED.resolve():
            parser.error("cannot stage artifact onto itself")
        if STAGED.is_symlink():
            raise ValueError("Staging symlink rejected")
        if STAGED.exists():
            shutil.rmtree(STAGED)
        shutil.copytree(args.artifact, STAGED)
        verify(STAGED, args.expected)
    print(metadata["digest"])


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit(
            f"{error}\nRun python scripts/frontend_artifact.py build and stage."
        ) from error
