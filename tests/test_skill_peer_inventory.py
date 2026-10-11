from __future__ import annotations

import json

from ouroboros import skill_loader
from ouroboros.skill_loader import save_enabled
from ouroboros.skill_peer_inventory import discover_skill_peers


def _manifest(name: str, *, conflicts=()) -> str:
    conflict_text = "[" + ", ".join(json.dumps(item) for item in conflicts) + "]"
    return (
        "---\n"
        f"name: {name}\n"
        "description: peer fixture\n"
        "version: 1.0.0\n"
        "type: instruction\n"
        f"conflicts: {conflict_text}\n"
        "---\n"
        "body\n"
    )


def test_peer_projection_preserves_conflicts_without_payload_hashing(tmp_path, monkeypatch):
    drive = tmp_path / "drive"
    skills = drive / "skills"
    first = skills / "alpha"
    second = skills / "beta"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "SKILL.md").write_text(_manifest("display-alpha", conflicts=("beta",)), encoding="utf-8")
    (first / "payload.bin").write_bytes(b"large payload")
    (second / "SKILL.md").write_text(_manifest("display-beta"), encoding="utf-8")
    save_enabled(drive, "alpha", True)
    save_enabled(drive, "beta", True)

    def fail_hash(*_args, **_kwargs):
        raise AssertionError("peer inventory must not hash payloads")

    monkeypatch.setattr(skill_loader, "compute_content_hash", fail_hash)
    peers = discover_skill_peers(drive, repo_path="")
    by_name = {peer.name: peer for peer in peers}
    assert by_name["alpha"].enabled is True
    assert by_name["alpha"].conflicts == ("beta",)
    assert by_name["beta"].enabled is True
    assert all(not peer.identity_collision for peer in peers)


def test_peer_projection_keeps_collision_and_malformed_as_disabled_placeholders(tmp_path):
    drive = tmp_path / "drive"
    skills = drive / "skills"
    (skills / "same").mkdir(parents=True)
    (skills / "same").joinpath("SKILL.md").write_text(_manifest("one"), encoding="utf-8")
    # A second canonical location is supplied by the user checkout.
    checkout = tmp_path / "checkout"
    (checkout / "same").mkdir(parents=True)
    (checkout / "same").joinpath("SKILL.md").write_text(_manifest("two"), encoding="utf-8")
    (skills / "broken").mkdir(parents=True)
    (skills / "broken" / "SKILL.md").write_text("---\nname: [\n---\n", encoding="utf-8")

    peers = discover_skill_peers(drive, repo_path=str(checkout))
    same = [peer for peer in peers if peer.name == "same"]
    assert len(same) == 2
    assert all(peer.identity_collision and not peer.enabled and not peer.conflicts for peer in same)
    broken = next(peer for peer in peers if peer.name == "broken")
    assert broken.manifest_error
    assert broken.enabled is False
    assert broken.conflicts == ()
