"""Local manifest resolution requires a complete, bounded asset graph."""

import json

import pytest

from watt_profile.server.frontend import entry_assets, unavailable
from watt_profile.server.frontend_artifact import canonical, file_hashes, sha, verify


def test_entry_styles_include_transitive_imports(tmp_path):
    (tmp_path / ".vite").mkdir()
    manifest = {
        "index.html": {"file": "entry.js", "css": ["entry.css"], "imports": ["shared"]},
        "shared": {"file": "shared.js", "css": ["shared.css"]},
    }
    (tmp_path / ".vite/manifest.json").write_text(json.dumps(manifest))
    for name in ("entry.js", "entry.css", "shared.js", "shared.css"):
        (tmp_path / name).write_text("synthetic")
    payload = {"format": 1, "files": file_hashes(tmp_path), "locks": {}}
    (tmp_path / "artifact.json").write_bytes(
        canonical(dict(payload, digest=sha(canonical(payload))))
    )
    assert entry_assets(tmp_path) == ("entry.js", ["shared.css", "entry.css"], ["shared.js"])
    (tmp_path / "shared.css").unlink()
    with pytest.raises(ValueError, match="mismatch"):
        entry_assets(tmp_path)


def test_missing_assets_have_local_build_instructions():
    response = unavailable()
    assert response.status_code == 503
    assert b"npm run ui:prepare" in response.body and b"npm run build" in response.body


def test_artifact_rejects_untrusted_digest_and_symlinks(tmp_path):
    (tmp_path / ".vite").mkdir()
    (tmp_path / ".vite/manifest.json").write_text("{}")
    payload = {"format": 1, "files": file_hashes(tmp_path), "locks": {}}
    digest = sha(canonical(payload))
    (tmp_path / "artifact.json").write_bytes(canonical(dict(payload, digest=digest)))
    assert verify(tmp_path, digest)["digest"] == digest
    with pytest.raises(ValueError, match="Missing WATT_FRONTEND_DIGEST") as missing:
        verify(tmp_path, "")
    assert "--build-arg WATT_FRONTEND_DIGEST" in str(missing.value)
    assert "README.md" in str(missing.value)
    with pytest.raises(ValueError, match="mismatch"):
        verify(tmp_path, "0" * 64)
    (tmp_path / "escape").symlink_to(tmp_path.parent)
    with pytest.raises(ValueError, match="symlink"):
        verify(tmp_path, digest)


@pytest.mark.parametrize("mutation", ["missing", "extra", "changed", "metadata"])
def test_artifact_rejects_changed_exact_file_tree(tmp_path, mutation):
    (tmp_path / ".vite").mkdir()
    (tmp_path / ".vite/manifest.json").write_text("{}")
    (tmp_path / "entry.js").write_text("checked")
    payload = {"format": 1, "files": file_hashes(tmp_path), "locks": {"lock": "frozen"}}
    digest = sha(canonical(payload))
    metadata = dict(payload, digest=digest)
    if mutation == "missing":
        (tmp_path / "entry.js").unlink()
    elif mutation == "extra":
        (tmp_path / "unlisted.js").write_text("unlisted")
    elif mutation == "changed":
        (tmp_path / "entry.js").write_text("modified")
    else:
        metadata["locks"] = {"lock": "modified"}
    (tmp_path / "artifact.json").write_bytes(canonical(metadata))
    with pytest.raises(ValueError, match="mismatch"):
        verify(tmp_path, digest)
