"""Portable, Node-free verification of a checked frontend file tree."""

import hashlib
import json
from pathlib import Path

METADATA = "artifact.json"
INSTRUCTIONS = (
    "See README.md Server containers for the pinned Node/npm build, "
    "WATT_FRONTEND_DIGEST export, and stage --expected commands."
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def file_hashes(directory):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"Missing or symlink frontend artifact. {INSTRUCTIONS}")
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError("Frontend symlink/path escape")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("Unsupported frontend file")
        name = path.relative_to(directory).as_posix()
        if name != METADATA:
            result[name] = sha(path.read_bytes())
    return result


def verify(directory, expected=None):
    if expected == "":
        raise ValueError(
            "Missing WATT_FRONTEND_DIGEST for image build. Export the checked artifact "
            "digest before Compose; for docker build also pass "
            "--build-arg WATT_FRONTEND_DIGEST. " + INSTRUCTIONS
        )
    directory = Path(directory)
    actual = file_hashes(directory)
    try:
        metadata = json.loads((directory / METADATA).read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"Missing or invalid frontend metadata. {INSTRUCTIONS}") from error
    payload = {key: metadata[key] for key in ("format", "files", "locks")}
    digest = sha(canonical(payload))
    if (
        metadata["format"] != 1
        or actual != metadata["files"]
        or not actual
        or ".vite/manifest.json" not in actual
        or digest != metadata["digest"]
        or (expected is not None and digest != expected)
    ):
        raise ValueError(f"Frontend artifact digest/file mismatch. {INSTRUCTIONS}")
    return metadata
