"""Pure output-contract helpers for the commit scope reviewer.

This module owns no reviewer routing, retry, authority, or persistence.  It
validates the one-pass actor's JSON checklist response and projects FAIL rows
into the two finding buckets consumed by ``scope_review``.
"""

from __future__ import annotations

from typing import List


# The coupling questions in their checklist order (docs/CHECKLISTS.md "Coupling questions").
COUPLING_QUESTION_IDS = (
    "intent_alignment",
    "forgotten_touchpoints",
    "cross_surface_consistency",
    "regression_surface",
    "architecture_fit",
    "implicit_contracts",
)
SCOPE_REQUIRED_ITEMS = frozenset(COUPLING_QUESTION_IDS)
SCOPE_VALID_SEVERITIES = frozenset({"critical", "advisory"})
# Questions merged into a survivor. A row under a retired id answers an obligation
# recorded before the merge: it counts for its survivor and never duplicates or
# contradicts the survivor's own row, so it cannot void the seat's whole block.
_RETIRED_ITEM_SURVIVORS = {
    "prompt_doc_sync": "cross_surface_consistency",
    "cross_module_bugs": "implicit_contracts",
}


def normalize_scope_items(items: list) -> tuple[list[dict], str]:
    """Validate and normalize the scope-review checklist coverage contract."""
    if not isinstance(items, list):
        return [], "reviewer output is not a JSON array"

    normalized: list[dict] = []
    seen_pass: set[str] = set()
    seen_fail: set[str] = set()
    seen_items: set[str] = set()
    unexpected: list[str] = []
    invalid: list[str] = []

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            invalid.append(f"entry {index} is not an object")
            continue
        item_id = str(item.get("item", "") or "").strip()
        retired = item_id in _RETIRED_ITEM_SURVIVORS
        item_id = _RETIRED_ITEM_SURVIVORS.get(item_id, item_id)
        verdict = str(item.get("verdict", "") or "").strip().upper()
        if item_id not in SCOPE_REQUIRED_ITEMS:
            unexpected.append(item_id or f"<missing item at {index}>")
            continue
        if verdict not in {"PASS", "FAIL"}:
            invalid.append(f"{item_id}: invalid verdict {verdict!r}")
            continue
        severity = str(item.get("severity", "") or "").strip().lower()
        if verdict == "PASS" and not severity:
            # Severity only classifies FAIL blocking-ness. Reviewer models may
            # omit it on PASS, matching the triad parser's advisory default.
            severity = "advisory"
        if severity not in SCOPE_VALID_SEVERITIES:
            invalid.append(f"{item_id}: missing or invalid severity {severity!r}")
            continue
        reason = str(item.get("reason", "") or "").strip()
        if not reason:
            invalid.append(f"{item_id}: missing reason")
            continue
        if verdict == "PASS" and not retired:
            if item_id in seen_pass:
                invalid.append(f"{item_id}: duplicate PASS")
            seen_pass.add(item_id)
        elif not retired:
            seen_fail.add(item_id)
        seen_items.add(item_id)

        normalized_item = dict(item)
        normalized_item["item"] = item_id
        normalized_item["verdict"] = verdict
        normalized_item["severity"] = severity
        normalized_item["reason"] = reason
        normalized.append(normalized_item)

    pass_and_fail = sorted(seen_pass & seen_fail)
    if pass_and_fail:
        invalid.append("items with both PASS and FAIL: " + ", ".join(pass_and_fail))
    missing = sorted(SCOPE_REQUIRED_ITEMS - seen_items)
    errors: list[str] = []
    if missing:
        errors.append("missing required items: " + ", ".join(missing))
    if unexpected:
        errors.append("unexpected items: " + ", ".join(unexpected))
    if invalid:
        errors.append("invalid entries: " + "; ".join(invalid))
    return normalized, "; ".join(errors)


def classify_scope_findings(items: list) -> tuple[List[dict], List[dict]]:
    """Project normalized FAIL rows into critical and advisory findings."""
    critical_findings: List[dict] = []
    advisory_findings: List[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        verdict = str(item.get("verdict", "")).upper()
        severity = str(item.get("severity", "advisory")).lower()
        if verdict != "FAIL":
            continue
        finding = {
            "verdict": "FAIL",
            "severity": severity,
            "item": str(item.get("item", "scope_review")),
            "reason": str(item.get("reason", "")),
            "model": "scope_reviewer",
        }
        obligation_id = str(item.get("obligation_id", "") or "")
        if obligation_id:
            finding["obligation_id"] = obligation_id
        if severity == "critical":
            critical_findings.append(finding)
        else:
            advisory_findings.append(finding)
    return critical_findings, advisory_findings
