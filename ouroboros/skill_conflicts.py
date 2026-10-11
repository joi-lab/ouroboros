"""Peer-conflict projection over installed skill identities.

Extracted from ``ouroboros/skill_loader.py`` at the module-size gate; every
historical import site keeps reaching these through that module's re-export.

Both helpers are duck-typed on ``name`` / ``enabled`` / ``conflicts`` so the
full ``LoadedSkill`` and the cheap non-executable ``SkillPeer`` descriptor
reach the SAME verdict. That equality is the whole point of separating a fresh
selected subject from cheap peer metadata (#1195 F4), and it is pinned by
``tests/test_skill_peer_inventory_differential.py``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

_MAX_CONFLICT_PROJECTION = 8


def enabled_skill_conflicts(skill: Any, skills: List[Any]) -> List[str]:
    """Return enabled installed peers conflicting with ``skill``.

    A declaration on either side is authoritative, so one-sided manifests are
    enforced symmetrically. Missing and disabled peers are deliberately inert.
    """
    declared = set(skill.conflicts or ())
    conflicts = {
        peer.name
        for peer in skills
        if peer.name != skill.name
        and peer.enabled
        and (peer.name in declared or skill.name in peer.conflicts)
    }
    return sorted(conflicts)


def skill_conflict_status(skill: Any, skills: List[Any]) -> Optional[Dict[str, Any]]:
    """Return a bounded API-safe projection of enabled peer conflicts."""
    names = enabled_skill_conflicts(skill, skills)
    if not names:
        return None
    visible = names[:_MAX_CONFLICT_PROJECTION]
    return {
        "code": "skill_conflict",
        "skills": visible,
        "omitted": len(names) - len(visible),
    }
