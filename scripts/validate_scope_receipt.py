#!/usr/bin/env python3
"""Validate a contributor scope-checklist receipt against the runtime contract.

CONTRIBUTING.md's main review path asks the separate review agent to output a
JSON array covering all eight Intent/Scope checklist items. This validator is
a thin CLI over the SAME contract the runtime scope review enforces
(`ouroboros.tools.scope_review_contract.normalize_scope_items`) — coverage,
duplicate/contradictory rows, severities, and justified PASS reasons — so a
receipt that passes here matches what the project's own reviewers must
produce. It validates SHAPE, not truth: the maintainer still reads the
evidence.

The array is accepted bare, inside a ```json fence, or embedded in the
reviewer's surrounding prose — extraction reuses the runtime's own
`extract_json_array`, so what the project's scope parser would read is what
gets validated.

Usage:
    python scripts/validate_scope_receipt.py path/to/receipt.json
    ... | python scripts/validate_scope_receipt.py -

Exit code 0 when the receipt is valid; 1 with the contract errors otherwise.
"""

from __future__ import annotations

import json
import pathlib
import sys


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] in {"-h", "--help"}:
        print(__doc__.strip())
        return 0
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 1
    try:
        raw = (sys.stdin.read() if argv[1] == "-"
               else pathlib.Path(argv[1]).read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"invalid: cannot read receipt ({exc})", file=sys.stderr)
        return 1

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from ouroboros.tools.scope_review_contract import normalize_scope_items
    from ouroboros.triad_review import extract_json_array

    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        items = extract_json_array(raw, validate_fn=lambda candidate: not normalize_scope_items(candidate)[1])
        if items is None:
            items = extract_json_array(raw)
        if items is None:
            print("invalid: no JSON array found in the receipt "
                  "(bare, fenced, or embedded)", file=sys.stderr)
            return 1

    normalized, error = normalize_scope_items(items)
    if error:
        print(f"invalid: {error}", file=sys.stderr)
        return 1
    fails = [item for item in normalized if item["verdict"] == "FAIL"]
    print(
        f"valid: {len(normalized)} rows cover all checklist items; "
        f"{len(fails)} FAIL row(s)"
        + (" — " + ", ".join(sorted({f['item'] for f in fails})) if fails else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
