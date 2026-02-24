"""Tests for deterministic skills engine."""

from __future__ import annotations

from pathlib import Path

from vfriday.skills_engine.apply import apply_skill
from vfriday.skills_engine.manifest import read_manifest
from vfriday.skills_engine.state import init_skills_state, load_state


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_manifest_parse(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    _write(
        skill_dir / "manifest.yaml",
        "\n".join(
            [
                'skill: "add-demo"',
                'version: "0.1.0"',
                'description: "demo"',
                'core_version: "1.0.0"',
                "adds: [\"foo.txt\"]",
                "modifies: []",
                "structured:",
                "  env_additions: [\"DEMO_KEY\"]",
                "  python_dependencies: [\"demo-pkg>=1.0\"]",
                "depends: []",
                "conflicts: []",
                "post_apply: []",
                'test: "python3 -c \'print(123)\'"',
            ]
        ),
    )
    m = read_manifest(skill_dir)
    assert m.skill == "add-demo"
    assert m.structured.env_additions == ["DEMO_KEY"]
    assert m.structured.python_dependencies == ["demo-pkg>=1.0"]


def test_init_skills_state_creates_state_and_base(tmp_path: Path) -> None:
    _write(tmp_path / "VERSION", "6.2.0\n")
    _write(tmp_path / "README.md", "hello\n")
    state = init_skills_state(tmp_path)
    assert state["core_version"] == "6.2.0"
    assert (tmp_path / ".vfriday" / "state.yaml").exists()
    assert (tmp_path / ".vfriday" / "base" / "README.md").exists()


def test_apply_add_only_skill(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write(project / "VERSION", "6.2.0\n")
    _write(project / ".env.example", "OPENAI_API_KEY=\n")
    _write(project / "requirements.txt", "fastapi\n")
    init_skills_state(project)

    skill_dir = tmp_path / "skills" / "add-demo"
    _write(
        skill_dir / "manifest.yaml",
        "\n".join(
            [
                'skill: "add-demo"',
                'version: "0.1.0"',
                'description: "demo skill"',
                'core_version: "6.2.0"',
                "adds: [\"configs/demo.yaml\"]",
                "modifies: []",
                "structured:",
                "  env_additions: [\"VFRIDAY_DEMO_FLAG\"]",
                "  python_dependencies: [\"demo-lib>=0.1\"]",
                "depends: []",
                "conflicts: []",
                "post_apply: []",
                'test: "python3 -c \'print(1)\'"',
            ]
        ),
    )
    _write(skill_dir / "add" / "configs" / "demo.yaml", "enabled: true\n")

    result = apply_skill(project, skill_dir)
    assert result.success is True
    assert (project / "configs" / "demo.yaml").exists()
    env_text = (project / ".env.example").read_text(encoding="utf-8")
    req_text = (project / "requirements.txt").read_text(encoding="utf-8")
    assert "VFRIDAY_DEMO_FLAG=" in env_text
    assert "demo-lib>=0.1" in req_text

    state = load_state(project)
    applied = state.get("applied_skills") or []
    assert any(x.get("name") == "add-demo" for x in applied)
