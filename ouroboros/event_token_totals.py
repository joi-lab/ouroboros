"""Read-only token summaries for event consumers, never a money projection.

No runtime imports: offline artifact readers may load this module by path.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Any


TOKEN_COUNTERS = ("prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens")


def token_counter(row: Mapping[str, Any], name: str) -> tuple[str, int | None]:
    """One row's counter: ``("known", n)``, ``("legacy_zero", None)`` or ``("unknown", None)``.

    Missing and invalid values are unknown. A zero in an unstamped/v1 row is
    ambiguous: old writers could synthesize that zero.
    """
    nested = row.get("usage")
    usage = nested if isinstance(nested, Mapping) else {}
    # Canonical row-level counters win, including explicit null. A
    # nested review/extraction receipt must not fill a native unknown.
    value = row.get(name, usage.get(name))
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return "unknown", None
    if not isinstance(value, int) and (not value.is_integer()):
        return "unknown", None
    if value == 0 and row.get("record_contract_version") != 2:
        return "legacy_zero", None
    return "known", int(value)


def event_token_totals(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, int | None]]:
    """Keep reported lower bounds and per-counter gaps instead of adding unknown as zero.

    Unknown and ambiguous legacy zeros are counted per counter (``token_counter``).
    The caller owns event selection and deduplication; this is not a ledger.
    """
    result = {name: {"known_total": 0, "known_count": 0, "unknown_count": 0,
                     "legacy_zero_count": 0, "total": None} for name in TOKEN_COUNTERS}
    for row in rows:
        for name, summary in result.items():
            state, count = token_counter(row, name)
            summary[f"{state}_count"] += 1
            summary["known_total"] += count or 0
    for summary in result.values():
        if summary["known_count"] and not (summary["unknown_count"] or summary["legacy_zero_count"]):
            summary["total"] = summary["known_total"]
    return result
