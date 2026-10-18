"""Local Vite manifest integration; Node is only used to create the artifact."""

import json
from pathlib import Path

from fastapi.responses import HTMLResponse

from watt_profile.server.frontend_artifact import verify

# A staged package artifact takes precedence; the checkout path supports local builds.
PACKAGED = Path(__file__).parent / "static" / "frontend"
CHECKOUT = Path(__file__).resolve().parents[3] / "frontend" / "dist"


def asset_directory():
    return PACKAGED if PACKAGED.is_dir() else CHECKOUT


def entry_assets(directory):
    verify(directory)
    manifest = json.loads((directory / ".vite" / "manifest.json").read_text())
    entry = manifest["index.html"]
    styles, scripts, visited = [], [], set()

    def visit(key):
        if key in visited:
            return
        visited.add(key)
        item = manifest[key]
        for dependency in item.get("imports", []):
            visit(dependency)
        for name in [item["file"], *item.get("css", [])]:
            path = directory / name
            if (
                path.is_symlink()
                or not path.is_file()
                or not path.resolve().is_relative_to(directory.resolve())
            ):
                raise ValueError("Invalid frontend asset")
        styles.extend(item.get("css", []))
        if key != "index.html":
            scripts.append(item["file"])

    visit("index.html")
    return entry["file"], list(dict.fromkeys(styles)), scripts


def unavailable():
    return HTMLResponse(
        "Frontend assets are unavailable or mismatched. Run python scripts/frontend_artifact.py "
        "build, then stage with --expected DIGEST. In frontend/, this uses npm ci, "
        "npm run ui:prepare, and npm run build; then restart the Server. "
        "Packaged installations must include the built frontend artifact.",
        status_code=503,
    )
