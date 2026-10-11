"""Catalog fallback is materialized before actual file installation."""
import hashlib
import io
import json

import pytest

from ouroboros.marketplace import ouroboroshub


@pytest.mark.parametrize("base_kind", ["absent", "null", "empty", "explicit"])
def test_catalog_raw_base_reaches_real_installer(tmp_path, monkeypatch, base_kind):
    catalog_url = "https://raw.githubusercontent.com/owner/hub/main/catalog.json"
    fallback = "https://raw.githubusercontent.com/owner/hub/main"
    explicit = "http://localhost:8769/custom-root"
    expected = explicit if base_kind == "explicit" else fallback
    content = b"---\nname: demo\nversion: 1\n---\n# Demo\n"
    catalog = {"schema_version": 1, "skills": [{
        "slug": "demo", "version": "1", "files": [{
            "path": "SKILL.md", "sha256": hashlib.sha256(content).hexdigest(), "size": len(content),
        }],
    }]}
    if base_kind != "absent":
        catalog["raw_base_url"] = {"null": None, "empty": "", "explicit": explicit}[base_kind]
    calls = []

    class Opener:
        def open(self, url, *, timeout):
            calls.append(url)
            if url == catalog_url:
                return io.BytesIO(json.dumps(catalog).encode())
            assert url == expected + "/skills/demo/SKILL.md"
            return io.BytesIO(content)

    monkeypatch.setattr(ouroboroshub, "_hub_opener", lambda: Opener())
    monkeypatch.setattr(ouroboroshub, "get_ouroboroshub_catalog_url", lambda: catalog_url)
    monkeypatch.setattr(ouroboroshub, "get_ouroboroshub_skills_dir", lambda: tmp_path / "data/skills/ouroboroshub")
    # Only the HTTP opener is fake: load_catalog, URL admission, download,
    # digest verification, staging and landing all execute the real code.
    loaded = ouroboroshub.load_catalog()
    result = ouroboroshub.install("demo", catalog=loaded)
    assert result.ok, result.error
    assert loaded["raw_base_url"] == expected
    assert (result.target_dir / "SKILL.md").read_bytes() == content
    assert calls == [catalog_url, expected + "/skills/demo/SKILL.md"]
