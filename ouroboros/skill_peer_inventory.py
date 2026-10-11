"""Fresh, non-executable conflict metadata over canonical skill locations.

Only the selected execution subject needs payload identity, review and grants.
Peers need manifest conflicts and enablement; hashing their payloads contributes
nothing to that decision. There is no stat/TTL cache and no partial LoadedSkill.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ouroboros.contracts.skill_manifest import SkillManifestError, parse_skill_manifest_text
from ouroboros.skill_loader import _ManifestUnreadable, _manifest_text_for_dir, _skill_location_inventory, load_enabled


@dataclass(frozen=True)
class SkillPeer:
    name: str
    location: str
    skill_dir: Path
    enabled: bool = False
    conflicts: tuple[str, ...] = ()
    identity_collision: bool = False
    manifest_error: str = ""


def discover_skill_peers(drive_root: Path, *, repo_path: str | None = None) -> tuple[SkillPeer, ...]:
    """Match discover_skills' peer semantics, without opening payload files.

    Collisions and malformed/unreadable manifests are disabled placeholders.
    A valid manifest retains its enablement and conflicts even if its payload
    would be unreadable: that is exactly how load_skill projects such a peer.
    Missing manifests are omitted by the ordinary location inventory.
    """
    inventory = _skill_location_inventory(drive_root, repo_path=repo_path)
    counts: dict[str, int] = {}
    for candidate in inventory:
        counts[candidate.name] = counts.get(candidate.name, 0) + 1
    peers = []
    for candidate in inventory:
        identity = dict(name=candidate.name, location=candidate.location, skill_dir=candidate.skill_dir)
        if counts[candidate.name] > 1:
            peers.append(SkillPeer(**identity, identity_collision=True))
            continue
        try:
            read = _manifest_text_for_dir(candidate.skill_dir)
            if read is None:
                continue
            manifest = parse_skill_manifest_text(read[0])
        except (_ManifestUnreadable, SkillManifestError) as exc:
            peers.append(SkillPeer(**identity, manifest_error=type(exc).__name__))
            continue
        peers.append(SkillPeer(**identity, enabled=load_enabled(drive_root, candidate.name),
                               conflicts=tuple(manifest.conflicts or ())))
    return tuple(peers)
