"""Local manifest resolution requires a complete, bounded asset graph."""

import json

import pytest

from watt_profile.server.frontend import entry_assets, unavailable


def test_entry_styles_include_transitive_imports(tmp_path):
    (tmp_path / ".vite").mkdir()
    manifest = {
        "index.html": {"file": "entry.js", "css": ["entry.css"], "imports": ["shared"]},
        "shared": {"file": "shared.js", "css": ["shared.css"]},
    }
    (tmp_path / ".vite/manifest.json").write_text(json.dumps(manifest))
    for name in ("entry.js", "entry.css", "shared.js", "shared.css"):
        (tmp_path / name).write_text("synthetic")
    assert entry_assets(tmp_path) == ("entry.js", ["shared.css", "entry.css"], ["shared.js"])
    (tmp_path / "shared.css").unlink()
    with pytest.raises(ValueError, match="Invalid frontend asset"):
        entry_assets(tmp_path)


def test_missing_assets_have_local_build_instructions():
    response = unavailable()
    assert response.status_code == 503
    assert b"npm run ui:prepare" in response.body and b"npm run build" in response.body
