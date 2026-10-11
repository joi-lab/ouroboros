"""The install's interface-language fact: one open tag, no enumeration of languages.

``OUROBOROS_UI_LANGUAGE`` holds a BCP-47 language tag. Three states matter and two of
them look alike, so they are kept apart on purpose: ``""`` is *not chosen* (the
interface renders its English source and a one-time migration may still seed a
value), ``"en"`` is *chosen English*, and any other tag names the language the
install translates itself into. The set is open: ``ru``, ``pt-BR``, ``zh-Hans``, the
ISO code of a constructed language (``qya`` for Quenya) or a private-use tag for an
invented one (``art-x-<slug>``, the BCP-47 form for artificial languages). Nothing
here lists languages; this leaf only decides whether a string is a well-formed tag
and spells it canonically, because the tag names the memory file on disk
(``state/i18n/<tag>.json``) and must therefore be a safe file name.

Pure functions, no I/O, no settings reads: ``i18n_memory`` owns the store and
``gateway/ui_i18n`` the endpoints.
"""

from __future__ import annotations

import re
from typing import Optional

LANGUAGE_NOT_CHOSEN = ""
SETTING_KEY = "OUROBOROS_UI_LANGUAGE"

# BCP-47 syntax, conservatively: a 2- or 3-letter ISO 639 primary subtag (the 5-8 letter
# registered forms exist in the grammar but name no real language, and admitting them
# would turn the word "Russian" into a tag), then up to eight alphanumeric subtags of
# 1-8 chars (script, region, variants, private use). The charset is letters, digits and
# hyphen only — the tag doubles as a file name.
_TAG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8}){0,8}$")
_MAX_TAG_CHARS = 48


def normalize_language_tag(value: object) -> Optional[str]:
    """Canonical spelling of a well-formed tag, or ``None`` when ``value`` is not one.

    ``""`` (not chosen) normalizes to ``""``. Case follows BCP-47 convention: the
    primary subtag lower (``ru``), a 4-letter script Title (``Hans``), a 2-letter or
    3-digit region upper (``BR``), everything else lower. ``x`` private-use
    subtags stay lower. The function never guesses: "Russian" or "русский" is not
    a tag and returns ``None``; turning a language *name* into a tag is a model's
    job (``ui_translation``), not this leaf's.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text == LANGUAGE_NOT_CHOSEN:
        return LANGUAGE_NOT_CHOSEN
    if len(text) > _MAX_TAG_CHARS or not _TAG_RE.match(text):
        return None
    parts = text.split("-")
    out = [parts[0].lower()]
    private = False
    for sub in parts[1:]:
        if private or sub.lower() == "x":
            private = True
            out.append(sub.lower())
        elif len(sub) == 4 and sub.isalpha():
            out.append(sub[0].upper() + sub[1:].lower())
        elif (len(sub) == 2 and sub.isalpha()) or (len(sub) == 3 and sub.isdigit()):
            out.append(sub.upper())
        else:
            out.append(sub.lower())
    return "-".join(out)


def is_english(tag: object) -> bool:
    """Not chosen, or chosen English in any region/script: the source renders as is."""
    normalized = normalize_language_tag(tag)
    if normalized is None:
        return True
    return normalized == LANGUAGE_NOT_CHOSEN or normalized.split("-")[0] == "en"


def language_file_name(tag: str) -> str:
    """The memory file stem for a canonical tag (the tag itself; validated by the caller)."""
    normalized = normalize_language_tag(tag)
    if not normalized:
        raise ValueError(f"not a language tag: {tag!r}")
    return normalized


def invented_language_tag(slug: object) -> str:
    """The private-use tag for an invented language: ``art-x-<slug>`` (BCP-47 ``art`` =
    artificial language). The slug keeps letters and digits only, at most 8 per subtag."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", str(slug or "")).strip("-").lower()
    subtags = [part[:8] for part in cleaned.split("-") if part][:6]
    if not subtags:
        subtags = ["lang"]
    return "art-x-" + "-".join(subtags)
