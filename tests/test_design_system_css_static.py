"""Design-system debt ratchets for first-party sheets (P10/P11)."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


def first_party_sheets():
    return sorted(path for path in WEB.rglob("*.css") if "vendor" not in path.relative_to(WEB).parts)


def without_comments(text):
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


# No exception: status/provenance/stat labels keep the renderer's sentence case.
UPPERCASE_ALLOWLIST = {}


def test_first_party_styles_do_not_uppercase_labels():
    violations = []
    for path in first_party_sheets():
        css = without_comments(path.read_text(encoding="utf-8"))
        if re.search(r"text-transform\s*:\s*uppercase\b", css, re.I):
            violations.append(str(path.relative_to(ROOT)))
    assert not violations, f"Sentence case is a design-system rule: {violations}"
    assert not UPPERCASE_ALLOWLIST


# Exact, shrink-only inventory at the L3 base. Counts matter: a new query at an
# existing width is new debt too. When removing a query, lower its count here in
# the same commit. L1 removes the shell entries during integration; content
# queries should become container queries, never another viewport breakpoint.
MAX_WIDTH_DEBT = {
    'web/model_roles.css': {'720px': 1},  # existing model editor stack
    'web/onboarding.css': {'1100px': 1, '900px': 1, '760px': 1, '720px': 1},  # wizard
    'web/room_work.css': {'900px': 1},  # existing Shade height recipe (L1)
    'web/settings.css': {'980px': 2, '760px': 1, '640px': 1, '600px': 1},  # existing form/MCP layout debt
    'web/style.css': {'980px': 2, '640px': 2, '620px': 2},  # legacy content only: the shell's queries are gone (L1)
    'web/ui.css': {'640px': 1},  # native field font-size/zoom recipe
}


def media_widths(css):
    from collections import Counter

    return Counter(
        re.sub(r'\s+', '', value).lower()
        for query in re.findall(r'@media\b([^{}]+)\{', without_comments(css), re.I)
        for value in re.findall(r'\(\s*max-width\s*:\s*([^)]+)\)', query, re.I)
    )


def test_viewport_width_queries_match_the_shrinking_debt_inventory():
    actual = {path.relative_to(ROOT).as_posix(): dict(counts) for path in first_party_sheets()
              if (counts := media_widths(path.read_text(encoding='utf-8')))}
    assert actual == MAX_WIDTH_DEBT, (
        'New raw viewport breakpoints are forbidden; components follow their container. '
        'When paying down existing queries, shrink MAX_WIDTH_DEBT in the same commit. '
        f'\nActual: {actual}\nAllowed debt: {MAX_WIDTH_DEBT}'
    )


def test_width_inventory_sees_multiline_compound_queries_and_repeated_debt():
    assert media_widths('''
        /* @media (max-width: 999px) { ignored } */
        @media (max-height: 820px),
            (max-width: 900px) { .card { max-width: 100px; } }
        @media (max-width: 900px) and (pointer: coarse) { .card { display: grid; } }
        @container card (max-width: 22rem) { .field { display: block; } }
    ''') == {'900px': 2}
