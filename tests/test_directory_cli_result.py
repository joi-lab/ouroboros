import json

import pytest

from ouroboros.cli import PatchCLIError, _patch_from_result


class FilesClient:
    def __init__(self, manifest):
        self.manifest = manifest

    def get_bytes(self, url):
        assert url == "/api/tasks/task-a/artifacts/workspace_patch.json"
        return json.dumps(self.manifest).encode()


@pytest.mark.parametrize("manifest", [
    {"capture_kind": "directory_direct", "registered_outputs": [{"name": "report.txt"}]},
    {"capture_kind": "engine_directory", "file_outputs": [{"name": "after-0"}]},
    {"file_outputs": [{"name": "files.zip"}]},
])
def test_strict_patch_names_complete_file_result(manifest):
    result = {"artifact_status": "ready", "artifacts": [
        {"kind": "workspace_patch_manifest", "name": "workspace_patch.json"}]}
    with pytest.raises(PatchCLIError, match="complete files.*workspace_patch.json"):
        _patch_from_result(FilesClient(manifest), "task-a", result, strict=True)
    assert _patch_from_result(FilesClient(manifest), "task-a", result, strict=False) == ""
