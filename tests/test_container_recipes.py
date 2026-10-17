"""The container recipes are one carrier, and both image modes keep their contracts.

``docker build .`` still yields the immutable image (``python server.py``); the
``managed`` target hosts the common launcher over a released seed with the body and
runtime venv on volumes. ``packaging/docker/compose.yaml`` is the only executable
recipe: every profile restarts on failure only, reaps with an init, names its data and
work roots, and never mounts a volume at the repository bootstrap must create. The guides
point to it instead of repeating it. Real container lifecycle: tests/system_e2e
(``OUROBOROS_E2E_DEEP=container``).
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
yaml = pytest.importorskip("yaml")


def _stages() -> dict:
    stages, current = {}, None
    for line in (REPO / "Dockerfile").read_text(encoding="utf-8").splitlines():
        match = re.match(r"FROM\s+(\S+)(?:\s+AS\s+(\S+))?", line)
        if match:
            current = match.group(2) or f"<final:{match.group(1)}>"
            stages[current] = {"from": match.group(1), "lines": []}
        elif current:
            stages[current]["lines"].append(line)
    return stages


def _entrypoint(lines) -> list:
    return next((json.loads(line.split("ENTRYPOINT", 1)[1]) for line in lines if line.startswith("ENTRYPOINT")), None)


def test_the_default_target_is_still_the_immutable_image():
    stages = _stages()
    final_name = list(stages)[-1]
    assert final_name.startswith("<final:") and stages[final_name]["from"] == "runtime"
    assert stages[final_name]["lines"] == [] or all(not line.startswith(("ENTRYPOINT", "CMD", "COPY", "RUN"))
                                                     for line in stages[final_name]["lines"])
    assert _entrypoint(stages["runtime"]["lines"]) == ["python", "server.py"]


def test_the_managed_target_hosts_the_launcher_over_a_complete_released_seed():
    stages = _stages()
    managed = stages["managed"]
    assert managed["from"] == "runtime"
    text = "\n".join(managed["lines"])
    assert "COPY --from=ouroboros-seed repo.bundle repo_bundle_manifest.json /opt/ouroboros/seed/" in text
    assert "bundle verify /opt/ouroboros/seed/repo.bundle" in text, "a shallow seed fails the build"
    entry = _entrypoint(managed["lines"])
    assert entry[:3] == ["python", "/app/launcher.py", "--no-ui"]
    assert entry[entry.index("--seed-bundle") + 1] == "/opt/ouroboros/seed"
    assert entry[entry.index("--runtime-venv") + 1] == "/ouroboros/body/venv"
    assert '["--launch-intent", "automatic"]' in text
    layout = dict(re.findall(r"(OUROBOROS_[A-Z_]+|HOME)=(\S+)", text))
    assert layout["OUROBOROS_REPO_DIR"] == "/ouroboros/body/repo"
    assert layout["OUROBOROS_DATA_DIR"] == "/ouroboros/data"
    assert layout["OUROBOROS_PID_FILE"].startswith("/ouroboros/data/"), "a second container sharing data loses the lock"
    for key in ("OUROBOROS_SUBAGENT_PROJECTS_ROOT", "OUROBOROS_SUBAGENT_WORKTREE_ROOT", "OUROBOROS_DELIVERABLES_ROOT"):
        assert layout[key].startswith("/ouroboros/work/")
    assert layout["HOME"] == "/ouroboros/home"


def test_the_compose_carrier_is_the_one_executable_recipe():
    recipe = yaml.safe_load((REPO / "packaging" / "docker" / "compose.yaml").read_text(encoding="utf-8"))
    services = recipe["services"]
    assert set(services) == {"image", "managed", "managed-split"}
    for name, service in services.items():
        assert service["profiles"] == [name], "one profile per volume set"
        assert service["restart"] == "on-failure" and service["init"] is True
        assert service["ports"] == ["127.0.0.1:8765:8765"]
    image = services["image"]
    assert image["environment"]["OUROBOROS_LAUNCH_INTENT"] == "automatic"
    assert image["environment"]["OUROBOROS_DATA_DIR"] == "/data"
    for key in ("OUROBOROS_SUBAGENT_PROJECTS_ROOT", "OUROBOROS_SUBAGENT_WORKTREE_ROOT", "OUROBOROS_DELIVERABLES_ROOT"):
        assert image["environment"][key].startswith("/work/")
    assert services["managed"]["volumes"] == ["managed:/ouroboros"]
    split = [volume.split(":", 1)[1] for volume in services["managed-split"]["volumes"]]
    assert split == ["/ouroboros/body", "/ouroboros/data", "/ouroboros/work", "/ouroboros/home"]
    assert not any(target.startswith("/ouroboros/body/repo") for target in split)
    assert all(volume.split(":", 1)[0] in recipe["volumes"] for service in services.values()
               for volume in service["volumes"])


def test_the_guides_point_to_the_carrier_instead_of_repeating_it():
    deployment = (REPO / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "packaging/docker/compose.yaml" in deployment and "```yaml" not in deployment
    assert "packaging/docker/compose.yaml" in (REPO / "README.md").read_text(encoding="utf-8")
    assert "docker/compose.yaml" in (REPO / "packaging" / "systemd" / "README.md").read_text(encoding="utf-8")
