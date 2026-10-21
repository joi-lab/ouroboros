"""Manifest contributions staged with an extension and projected from its live bundle.

No PluginAPI surface: contributions are host-owned manifest data. UI assets are
captured while hashing the exact bytes, then served only from that publication.
"""
from __future__ import annotations

import copy
import hashlib
import logging
from urllib.parse import quote

from ouroboros.extension_ui_validation import validate_contributions
from ouroboros.skill_identity import payload_bucket, release_declaration, release_digest_from_file_digests
from ouroboros.skill_loader import _iter_payload_files

log = logging.getLogger(__name__)
HOST_ASSET_TYPES = {
    ".js": "text/javascript", ".css": "text/css", ".json": "application/json",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml", ".ico": "image/x-icon",
}


def _capture_host_sources(skill, revision: str) -> dict[str, bytes]:
    """The page assets of the reviewed release, read while hashing it: the bytes captured are the bytes whose
    release digest (skill_identity, the skill's ``content_hash``) equals ``revision``."""
    root = skill.skill_dir.resolve()
    bucket = payload_bucket(root)
    sources, digests = {}, []
    for path in _iter_payload_files(root, manifest_entry=skill.manifest.entry, manifest_scripts=skill.manifest.scripts):
        rel = path.relative_to(root).as_posix()
        capture = (rel.startswith("ui/") and path.suffix in HOST_ASSET_TYPES
                   and not any(part.startswith(".") for part in rel.split("/")))
        digest, content = hashlib.sha256(), bytearray()
        with path.open("rb") as stream:
            while chunk := stream.read(64 * 1024):
                digest.update(chunk)
                if capture:
                    content.extend(chunk)
        digests.append((rel, digest.digest()))
        if capture:
            sources[rel] = bytes(content)
    declaration = release_declaration(root, skill.manifest, bucket=bucket)
    if release_digest_from_file_digests(digests, declaration, bucket=bucket) != revision:
        raise ValueError("payload changed while capturing host modules; reload the reviewed revision")
    return sources


def stage_contributions(api, skill, revision: str) -> None:
    """Stage before the loader's atomic publish; invalid entries never abort register()."""
    entries, problems = validate_contributions(skill.manifest, skill_dir=skill.skill_dir)
    sources = {}
    if any(entry["render"]["kind"] == "host" for entry in entries):
        try:
            sources = _capture_host_sources(skill, revision)
        except (OSError, ValueError, RuntimeError) as exc:
            problems.append(f"contributes: {exc}")
        available = []
        for entry in entries:
            if entry["render"]["kind"] == "host" and entry["render"]["entry"] not in sources:
                problems.append(f"contributes.{entry['id']}: host entry is not in the captured reviewed payload")
            else:
                available.append(entry)
        entries = available
    api._staged.contributions = project_contributions(skill.name, entries, revision, skill.manifest.name)
    api._staged.contribution_problems = problems
    api._staged.host_sources = sources
    for problem in problems:
        log.warning("skill %s: %s", skill.name, problem)


def project_contributions(skill: str, entries: list[dict], revision: str, label: str = "") -> list[dict]:
    """Host-owned absolute paths; authors never choose a different skill's namespace."""
    prefix = f"/api/extensions/{quote(skill, safe='')}/"
    rows = []
    for entry in entries:
        row = copy.deepcopy(entry)
        row.update(key=f"{skill}:{entry['id']}", skill=skill, skill_label=label or skill, revision=revision)
        route = row.pop("route", "")
        if row.get("action", {}).get("verb") == "skill.request":
            route = row["action"].pop("route")
            row["action"]["url"] = prefix + quote(route, safe="/")
        if route:
            row["route_url"] = prefix + quote(route, safe="/")
        if "source" in row:
            row["source"]["url"] = prefix + quote(row["source"].pop("route"), safe="/")
        render = row["render"]
        render["url"] = (prefix + f"ui/{quote(revision, safe='')}/" + quote(render.pop("entry"), safe="/")
                         if render["kind"] == "host" else "")
        rows.append(row)
    return rows
