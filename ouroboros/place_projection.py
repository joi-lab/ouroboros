"""Owner-facing place views from current connector declarations and stored choices."""
from __future__ import annotations

from typing import Any
from ouroboros.places import PlaceError, connect_place, load_places, place_summary, note_identity, migration_problems


def place_declarations(root) -> list[dict]:
    from ouroboros.extension_registry_state import live_contribution_projection
    from ouroboros.skill_loader import discover_skills
    from ouroboros.extension_ui_validation import validate_contributions
    rows = [dict(row) for row in live_contribution_projection() if row.get("provides") == "place"]
    published = {row["skill"] for row in rows}
    for skill in discover_skills(root):
        if skill.name in published or skill.load_error or skill.identity_collision:
            continue
        clean, _warnings = validate_contributions(skill.manifest)
        rows.extend({**row, "skill": skill.name, "skill_label": skill.manifest.name}
                    for row in clean if row.get("provides") == "place")
    return rows


def connect_declared_place(root, connector: str, *, account_id: str = "", conversation_id: str = ""):
    declarations = [row for row in place_declarations(root) if row["skill"] == connector]
    if len(declarations) != 1:
        raise PlaceError("place_declaration_unavailable", "connector", "Enable a reviewed connector with one place declaration.")
    row = declarations[0]
    provider = str(row["destination"]).removeprefix("vendor:").lower()
    return connect_place(root, connector=connector, provider=provider, label=row["label"],
                         account_id=account_id, conversation_id=conversation_id, scope=row.get("scope", "account"),
                         contribution_id=row["id"], wakes=row.get("wakes", []))


def place_view(root, place, *, declarations: list[dict] | None = None, allowance: dict | None = None) -> dict[str, Any]:
    from ouroboros.place_arrivals import account_candidates
    rows = place_declarations(root) if declarations is None else declarations
    row = next((r for r in rows if r["skill"] == place.connector and (
        not place.contribution_id or r["id"] == place.contribution_id)), {})
    result = dict(place_summary(place))
    # Legacy notes predate ids; use stable content+ordinal ids without rewriting history.
    result["notes"] = [{**n, "note_id": note_identity(place.place_id, i, place.notes[i])}
                       for i, n in enumerate(result["notes"])]
    cause = "place:" + place.place_id
    spend = next((item.get("usd") for item in (allowance or {}).get("spend_by_cause", [])
                  if item.get("cause") == cause), None)
    activity = place_activity(root, place)
    result.update(connector_label=row.get("skill_label") or place.connector,
                  reply_destination=place.destination,
                  destination=row.get("destination") or "vendor:" + place.provider,
                  icon=row.get("icon") if isinstance(row.get("icon"), dict) else {"name": row["icon"]} if row.get("icon") else {}, wake_suggestions=list(row.get("wakes") or []),
                  unanswered=activity["unanswered"], last_activity_at=activity["last_activity_at"],
                  activity_coverage="captured_place_events", spent_24h_usd=spend)
    result["account_candidates"] = account_candidates(root, place.place_id)
    if not row:
        result["status"] = {"text": "Connector unavailable", "tone": "warn"}
    return result


def places_view(root) -> dict:
    declarations = place_declarations(root)
    places = load_places(root)
    from ouroboros.presence_admission import focus_allowance
    allowance = focus_allowance(root)
    return {"migration_problems": migration_problems(root), "places": [place_view(root, p, declarations=declarations, allowance=allowance) for p in places],
            "connectors": [{"connector": r["skill"], "connector_label": r.get("skill_label") or r["skill"],
                            "label": r["label"], "icon": r.get("icon") or {}, "destination": r["destination"],
                            "wakes": r.get("wakes") or [], "connected": any(p.connector == r["skill"] for p in places)}
                           for r in declarations]}


def place_activity(root, place) -> dict:
    """Indexed newest arrival and nonterminal focus count; no historical row walk."""
    from ouroboros.place_arrivals import activity
    return activity(root, place)
