"""Translation memory: the install's own dictionary for one interface language.

The interface is authored in English (DEVELOPMENT "Naming and boundaries": product UI
strings are English *source* copy). Everything a non-English owner sees comes from a
generated, importable memory under the runtime data root — ``state/i18n/<tag>.json`` —
never from a locale file in the repository. One file per language, replaced atomically,
revision-counted, with one writer contract (``update_memory``: lock, read, mutate,
bump, write). The repository therefore enumerates no languages; a managed update never
touches the memory; a fork ships its dictionary as data.

Two kinds of keys live in ``entries``, because two kinds of owner-visible English exist:

* ``code:<table>.<key>`` — a host sentence minted by a closed code→sentence table
  (task headline words, cause sentences, question status, Telegram's own lines,
  notification titles). The code is stable across rewording, so an owner's or a
  fork's pin survives an upstream English edit; for a code the Python catalog owns,
  ``source_hash`` detects the edit and the status counts the entry as *stale* for
  review (a code only the browser renders has no hash here: its pin keeps rendering
  and is not counted).
* the rendered English string itself (optionally ``\\x1f<dom scope>``) — scattered
  interface chrome translated by the browser overlay (``web/modules/i18n.js``). The
  key IS the English, so a reword is a new key and the old entry is orphaned; that is
  the gettext trade-off; such entries are never reported stale (nothing can tell them apart
  from a string a page has simply not rendered yet).

Composed strings carry placeholders: ``{n}`` (a count), ``{name}`` (an opaque owner
span), ``<1>…</1>`` (an inline formatting element the browser maps onto the element's
own child). A value may use only placeholders its source has (``placeholders``), and
never markup. Plural forms are per CLDR category under ``forms``; which category a
number selects comes from ``plural_select`` — a map the browser writes at save time
from ``Intl.PluralRules`` (Python has no Intl and gains no Babel dependency for this),
with ``other`` as the honest fallback.

Provenance per entry: ``generated`` (the light model), ``owner`` (an edit the owner or
the mind made on request), ``imported`` (an enterprise or community file). Precedence
``owner > imported > generated``: generation never overwrites the other two, an import
replaces only ``imported`` entries and shadows itself under an owner override, and a
regenerate drops only ``generated`` entries. A malformed file is refused on read
(callers fall back to English and say so) and never overwritten as an empty dictionary.

``tr``/``fmt`` are the host's read seams (Telegram lines, host-composed text that
leaves the browser); the browser reads the same file through ``GET /api/ui/i18n``.
Misses the browser or the host cannot translate are queued in
``state/i18n/<tag>.pending.json`` for the generator (``ui_translation``), bounded.
"""

from __future__ import annotations

import hashlib
import json
import logging
import pathlib
import re
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ouroboros.ui_language import (
    LANGUAGE_NOT_CHOSEN,
    SETTING_KEY,
    is_english,
    normalize_language_tag,
)
from ouroboros.utils import atomic_write_json, read_json_dict, utc_now_iso

log = logging.getLogger(__name__)

SCHEMA = 1
MEMORY_DIR_REL = pathlib.Path("state") / "i18n"
PROVENANCES = ("generated", "owner", "imported")
_PRECEDENCE = {"owner": 3, "imported": 2, "generated": 1}
# Bounds are transport sanity, not product policy: a dictionary is a UI's worth of
# strings, a key is one rendered string, a value is its translation.
MAX_ENTRIES = 50_000
MAX_KEY_CHARS = 2_000
MAX_VALUE_CHARS = 4_000
MAX_PENDING = 2_000
MAX_LEXICON_CHARS = 60_000
PLURAL_SELECT_RANGE = 101  # select(n) for n in 0..100 as the browser writes it
CODE_PREFIX = "code:"
SCOPE_SEPARATOR = "\x1f"
_FORBIDDEN_KEYS = frozenset({"__proto__", "constructor", "prototype"})
# `{name}` and `{name:spec}` (the spec is part of the token: a changed spec is a changed
# placeholder), plus numbered inline slots `<1>`…`</1>`.
_PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*(?::[^{}]*)?\}|</?\d+>")
_SLOT_RE = re.compile(r"<(/?)(\d+)>")
_MARKUP_RE = re.compile(r"<(?!/?\d+>)[^>]*>")
_MEMORY_LOCK_TIMEOUT_SEC = 4.0

_cache_lock = threading.Lock()
_cache: Dict[str, Tuple[Tuple[float, int], Dict[str, Any]]] = {}


class MemoryFormatError(ValueError):
    """The file is not a schema-1 translation memory. Never coerced, never overwritten."""


class RevisionConflict(RuntimeError):
    """The writer's expected revision no longer matches the file: another writer landed first."""


# ---------------------------------------------------------------------------
# paths, hashing, placeholders
# ---------------------------------------------------------------------------


def memory_dir(drive_root: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(drive_root) / MEMORY_DIR_REL


def memory_path(drive_root: pathlib.Path, tag: str) -> pathlib.Path:
    normalized = normalize_language_tag(tag)
    if not normalized:
        raise ValueError(f"not a language tag: {tag!r}")
    return memory_dir(drive_root) / f"{normalized}.json"


def pending_path(drive_root: pathlib.Path, tag: str) -> pathlib.Path:
    return memory_path(drive_root, tag).with_suffix(".pending.json")


def source_hash(text: object) -> str:
    return "sha256:" + hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:24]


def placeholders(text: object) -> set:
    """``{n}``, ``{name}``, ``{name:spec}`` and inline tags as ``<1>`` (open and close fold to one token)."""
    out = set()
    for match in _PLACEHOLDER_RE.finditer(str(text or "")):
        token = match.group(0)
        if token.startswith("</"):
            token = "<" + token[2:]
        out.add(token)
    return out


def inline_slots_balanced(text: object) -> bool:
    """Numbered inline slots form the flat shape the renderer rebuilds: each ``<n>`` closes before
    another opens and each number is used once — ``<2>…</2> … <1>…</1>`` in any order, never
    nested or crossing (the overlay would drop the inner element)."""
    seen: set = set()
    current: Optional[str] = None
    for match in _SLOT_RE.finditer(str(text or "")):
        closing, number = bool(match.group(1)), match.group(2)
        if closing:
            if current != number:
                return False
            current = None
        elif current is not None or number in seen:
            return False
        else:
            seen.add(number)
            current = number
    return current is None


def is_code_key(key: object) -> bool:
    return str(key or "").startswith(CODE_PREFIX)


def split_scope(key: str) -> Tuple[str, str]:
    """``("Light", "[data-theme-control]")`` for a scoped chrome key, else ``(key, "")``."""
    text, sep, scope = str(key).partition(SCOPE_SEPARATOR)
    return (text, scope) if sep else (text, "")


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------


def new_memory(tag: str, *, profile: Optional[Dict[str, Any]] = None,
               plural_select: Optional[Dict[str, str]] = None,
               plural_categories: Optional[List[str]] = None) -> Dict[str, Any]:
    normalized = normalize_language_tag(tag)
    if not normalized:
        raise ValueError(f"not a language tag: {tag!r}")
    doc = {
        "schema": SCHEMA,
        "language": normalized,
        "revision": 0,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "profile": _normalize_profile(profile or {}, normalized),
        "plural_select": _normalize_plural_select(plural_select),
        "plural_categories": _normalize_plural_categories(plural_categories),
        "glossary_hash": "",
        "entries": {},
        "shadow": {},
    }
    return doc


def _normalize_profile(raw: Any, tag: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise MemoryFormatError("profile must be an object")
    label = str(raw.get("label") or "").strip()[:120]
    instruction = str(raw.get("instruction") or "").strip()[:4_000]
    direction = str(raw.get("direction") or "ltr").strip().lower()
    if direction not in ("ltr", "rtl"):
        raise MemoryFormatError("profile.direction must be ltr or rtl")
    lexicon = str(raw.get("lexicon") or "")
    if len(lexicon) > MAX_LEXICON_CHARS:
        raise MemoryFormatError("profile.lexicon is too long")
    return {"label": label or tag, "instruction": instruction, "direction": direction, "lexicon": lexicon}


def _normalize_plural_select(raw: Any) -> Optional[Dict[str, Any]]:
    """``{"map": {"0": "other", ...}, "period": 100|null}`` as the browser wrote it."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise MemoryFormatError("plural_select must be an object")
    mapping = raw.get("map") if "map" in raw else raw
    if not isinstance(mapping, dict):
        raise MemoryFormatError("plural_select.map must be an object")
    out: Dict[str, str] = {}
    for key, value in mapping.items():
        text = str(key)
        if not text.isdigit() or int(text) >= PLURAL_SELECT_RANGE:
            raise MemoryFormatError("plural_select keys are integers 0..100")
        if not isinstance(value, str) or not value or len(value) > 16:
            raise MemoryFormatError("plural_select values are CLDR category names")
        out[text] = value
    period = raw.get("period") if isinstance(raw, dict) else None
    if period is not None and (not isinstance(period, int) or isinstance(period, bool) or period <= 0):
        raise MemoryFormatError("plural_select.period must be a positive integer")
    return {"map": out, "period": period}


def _normalize_plural_categories(raw: Any) -> Optional[List[str]]:
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw) or len(raw) > 8:
        raise MemoryFormatError("plural_categories must be a short list of category names")
    return list(dict.fromkeys(raw))


def _validate_value(key: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    text = entry.get("text")
    forms = entry.get("forms")
    if text is None and forms is None:
        raise MemoryFormatError(f"entry {key!r} has neither text nor forms")
    if text is not None and forms is not None:
        raise MemoryFormatError(f"entry {key!r} has both text and forms")
    # The English the value translates: the key itself for rendered chrome, the recorded
    # ``source`` for a code entry (absent on an old import: nothing to compare with).
    if is_code_key(key):
        source_text = entry.get("source") if isinstance(entry.get("source"), str) else None
    else:
        source_text = split_scope(key)[0]
    allowed = placeholders(source_text) if source_text is not None else None
    out: Dict[str, Any] = {}
    if text is not None:
        out["text"] = _validate_text(key, text, allowed, source_text)
    else:
        if not isinstance(forms, dict) or not forms:
            raise MemoryFormatError(f"entry {key!r} forms must be a nonempty object")
        cleaned = {}
        for category, value in forms.items():
            if not isinstance(category, str) or not category or len(category) > 16:
                raise MemoryFormatError(f"entry {key!r} has a bad plural category")
            cleaned[category] = _validate_text(key, value, allowed, source_text)
        out["forms"] = cleaned
    return out


def validate_candidate(key: str, value: Dict[str, Any], *, source: Optional[str] = None) -> Dict[str, Any]:
    """The entry the generator is about to write, validated by the same rule the writer applies
    (so an answer the model gave is refused per key, never as a whole batch at write time).
    Raises ``MemoryFormatError``."""
    candidate = {**value, "provenance": "generated"}
    if is_code_key(key) and isinstance(source, str):
        candidate["source"] = source[:MAX_KEY_CHARS]
    return _validate_entry(key, candidate)


def markup_tokens(text: object) -> set:
    """The angle-bracket tokens of ``text`` other than numbered inline slots. A translation may
    carry exactly the ones its source has (``<code>mcp_<server>__<tool></code>`` is text, not
    markup) and no others."""
    return set(_MARKUP_RE.findall(str(text or "")))


_BRACE_RE = re.compile(r"\{[^{}]*\}")


def brace_tokens(text: object) -> set:
    """Every ``{...}`` field of ``text``: placeholders and anything else ``str.format`` would try
    to resolve (``{language.foo}``, ``{x[0]}``, ``{x!r}``). A translation may carry only the
    fields its source has, so a consumer's ``.format`` never meets a field it cannot fill."""
    return set(_BRACE_RE.findall(str(text or "")))


def _validate_text(key: str, value: Any, allowed: Optional[set], source: Optional[str]) -> str:
    """One rule for generated answers, imports and stored entries: ``source`` is the English the
    value translates (a text key IS its source; a code entry carries it as ``source``), and the
    value may use exactly the source's placeholders, only the source's angle-bracket tokens and
    no ``{...}`` field the source lacks. ``None`` source (an old import of a code key): the strict
    no-markup rule and no placeholder check."""
    if not isinstance(value, str):
        raise MemoryFormatError(f"entry {key!r} value must be a string")
    if len(value) > MAX_VALUE_CHARS:
        raise MemoryFormatError(f"entry {key!r} value is too long")
    if markup_tokens(value) - (markup_tokens(source) if source is not None else set()):
        hint = "" if source is not None else " (a code entry whose English has angle-bracket text states it as `source`)"
        raise MemoryFormatError(f"entry {key!r} value carries markup{hint}")
    if source is not None and brace_tokens(value) - brace_tokens(source):
        raise MemoryFormatError(f"entry {key!r} value carries a {{...}} field the source lacks")
    if not inline_slots_balanced(value):
        raise MemoryFormatError(f"entry {key!r} value has an unbalanced inline slot")
    if allowed is not None:
        # The same placeholders, no more and no fewer: a consumer formats with exactly the
        # source's parameters, and a dropped or invented one breaks it in every language.
        found = placeholders(value)
        if found != allowed:
            raise MemoryFormatError(f"entry {key!r} value placeholders {sorted(found)} differ from the source's {sorted(allowed)}")
    return value


def _validate_entry(key: Any, raw: Any) -> Dict[str, Any]:
    if not isinstance(key, str) or not key or len(key) > MAX_KEY_CHARS:
        raise MemoryFormatError("entry keys are nonempty strings")
    if key in _FORBIDDEN_KEYS or split_scope(key)[0] in _FORBIDDEN_KEYS:
        raise MemoryFormatError(f"entry key {key!r} is refused")
    if not isinstance(raw, dict):
        raise MemoryFormatError(f"entry {key!r} must be an object")
    provenance = str(raw.get("provenance") or "generated")
    if provenance not in PROVENANCES:
        raise MemoryFormatError(f"entry {key!r} has unknown provenance {provenance!r}")
    entry = _validate_value(key, raw)
    entry["provenance"] = provenance
    for field in ("source_hash", "model", "at", "attempt_id", "pack", "pack_version", "context"):
        value = raw.get(field)
        if value is not None:
            if not isinstance(value, str) or len(value) > 400:
                raise MemoryFormatError(f"entry {key!r}.{field} must be a short string")
            entry[field] = value
    # The English a code entry translated: the browser compares it with the current English
    # to catch a reword of a code it alone renders (no Python twin, so no source hash to check).
    source = raw.get("source")
    if source is not None:
        if not isinstance(source, str) or len(source) > MAX_KEY_CHARS:
            raise MemoryFormatError(f"entry {key!r}.source must be a string")
        entry["source"] = source
    return entry


def validate_memory(doc: Any) -> Dict[str, Any]:
    """The normalized document, or ``MemoryFormatError``. Unknown top-level fields are kept."""
    if not isinstance(doc, dict):
        raise MemoryFormatError("memory must be a JSON object")
    if doc.get("schema") != SCHEMA:
        raise MemoryFormatError(f"unsupported memory schema {doc.get('schema')!r}")
    tag = normalize_language_tag(doc.get("language"))
    if not tag:
        raise MemoryFormatError("memory.language must be a language tag")
    entries_raw = doc.get("entries", {})
    if not isinstance(entries_raw, dict):
        raise MemoryFormatError("memory.entries must be an object")
    if len(entries_raw) > MAX_ENTRIES:
        raise MemoryFormatError("memory has too many entries")
    entries = {key: _validate_entry(key, raw) for key, raw in entries_raw.items()}
    shadow_raw = doc.get("shadow", {})
    if not isinstance(shadow_raw, dict):
        raise MemoryFormatError("memory.shadow must be an object")
    shadow = {key: _validate_entry(key, raw) for key, raw in shadow_raw.items()}
    revision = doc.get("revision", 0)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise MemoryFormatError("memory.revision must be a non-negative integer")
    refused = doc.get("refused", {})
    if not isinstance(refused, dict) or not all(isinstance(k, str) and isinstance(v, dict) for k, v in refused.items()):
        raise MemoryFormatError("memory.refused must be an object of key → facts")
    out = dict(doc)
    out.update({
        "schema": SCHEMA, "language": tag, "revision": revision,
        "profile": _normalize_profile(doc.get("profile") or {}, tag),
        "plural_select": _normalize_plural_select(doc.get("plural_select")),
        "plural_categories": _normalize_plural_categories(doc.get("plural_categories")),
        "glossary_hash": str(doc.get("glossary_hash") or ""),
        "entries": entries, "shadow": shadow, "refused": refused,
    })
    return out


# ---------------------------------------------------------------------------
# reading and the one writer
# ---------------------------------------------------------------------------


def load_memory(drive_root: pathlib.Path, tag: str) -> Optional[Dict[str, Any]]:
    """The validated memory, ``None`` when absent, ``MemoryFormatError`` when malformed."""
    path = memory_path(drive_root, tag)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MemoryFormatError(f"{path.name}: unreadable JSON ({exc})") from exc
    return validate_memory(raw)


def _memory_lock(path: pathlib.Path):
    from contextlib import contextmanager

    from ouroboros.platform_layer import acquire_exclusive_file_lock, release_exclusive_file_lock

    @contextmanager
    def _held():
        lock_path = path.with_name(path.name + ".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = acquire_exclusive_file_lock(lock_path, timeout_sec=_MEMORY_LOCK_TIMEOUT_SEC)
        if fd is None:
            raise TimeoutError(f"could not lock the translation memory: {lock_path}")
        try:
            yield
        finally:
            release_exclusive_file_lock(lock_path, fd)

    return _held()


def update_memory(drive_root: pathlib.Path, tag: str,
                  mutate: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]], *,
                  expected_revision: Optional[int] = None, create: bool = False,
                  profile: Optional[Dict[str, Any]] = None,
                  plural_select: Optional[Dict[str, Any]] = None,
                  plural_categories: Optional[List[str]] = None) -> Dict[str, Any]:
    """Read, change and persist ONE memory file under its lock.

    ``mutate`` receives the validated document and returns the document to persist, or
    ``None`` to persist nothing. ``expected_revision`` refuses a write whose decision was
    taken on an older read (``RevisionConflict``). ``create`` makes an absent file; an
    existing malformed file is never replaced (the ``MemoryFormatError`` propagates)."""
    path = memory_path(drive_root, tag)
    with _memory_lock(path):
        doc = load_memory(drive_root, tag)
        created = doc is None
        if created:
            if not create:
                raise FileNotFoundError(str(path))
            doc = new_memory(tag, profile=profile, plural_select=plural_select,
                             plural_categories=plural_categories)
        if expected_revision is not None and doc["revision"] != expected_revision:
            raise RevisionConflict(f"memory revision is {doc['revision']}, expected {expected_revision}")
        proposed = mutate(doc)
        if proposed is None:
            if not created:
                return doc
            # A fresh header is a write even when the mutation had nothing to add.
            proposed = doc
        else:
            proposed = validate_memory(proposed)
            proposed["revision"] = doc["revision"] + 1
        proposed["updated_at"] = utc_now_iso()
        atomic_write_json(path, proposed, trailing_newline=True)
        _cache_forget(path)
        return proposed


def _cache_forget(path: pathlib.Path) -> None:
    with _cache_lock:
        _cache.pop(str(path), None)


def cached_memory(drive_root: pathlib.Path, tag: str) -> Optional[Dict[str, Any]]:
    """``load_memory`` through an mtime/size cache; a malformed file reads as ``None``
    here and is logged once per change, because a read seam must never raise into a
    rendering path (the gateway surfaces the error on the status)."""
    path = memory_path(drive_root, tag)
    try:
        stat = path.stat()
    except OSError:
        _cache_forget(path)
        return None
    stamp = (stat.st_mtime, stat.st_size)
    with _cache_lock:
        hit = _cache.get(str(path))
        if hit and hit[0] == stamp:
            return hit[1]
    try:
        doc = load_memory(drive_root, tag)
    except MemoryFormatError as exc:
        log.warning("translation memory %s is malformed and is ignored: %s", path.name, exc)
        doc = None
    with _cache_lock:
        _cache[str(path)] = (stamp, doc)
    return doc


# ---------------------------------------------------------------------------
# entry operations (pure on the document)
# ---------------------------------------------------------------------------


def apply_generated(doc: Dict[str, Any], items: Dict[str, Dict[str, Any]], *, model: str,
                    attempt_id: str = "", source_hashes: Optional[Dict[str, str]] = None,
                    sources: Optional[Dict[str, str]] = None, at: Optional[str] = None) -> int:
    """Write generated translations for keys that have no owner/imported entry. Returns the count.
    ``sources`` is the English each code key translated (kept on the entry for the browser's
    reword check); a key written here leaves the ``refused`` ledger."""
    applied = 0
    stamp = at or utc_now_iso()
    for key, value in items.items():
        current = doc["entries"].get(key)
        if current and current.get("provenance") in ("owner", "imported"):
            continue
        candidate = {**value, "provenance": "generated"}
        if sources and is_code_key(key) and isinstance(sources.get(key), str):
            candidate["source"] = sources[key][:MAX_KEY_CHARS]  # validated against the English it translates
        entry = _validate_entry(key, candidate)
        entry["model"] = str(model or "")[:400]
        entry["at"] = stamp
        if attempt_id:
            entry["attempt_id"] = str(attempt_id)[:400]
        if source_hashes and key in source_hashes:
            entry["source_hash"] = source_hashes[key]
        doc["entries"][key] = entry
        doc.setdefault("refused", {}).pop(key, None)
        applied += 1
    return applied


def refuse_keys(doc: Dict[str, Any], keys: Iterable[str], *, reason: str) -> int:
    """Remember keys the generator gave up on, so the queue stops taking them (a transport
    bound: nothing judges the text). Regenerate clears the ledger."""
    ledger = doc.setdefault("refused", {})
    stamp = utc_now_iso()
    count = 0
    for key in keys:
        if isinstance(key, str) and key and key not in ledger and len(ledger) < MAX_ENTRIES:
            ledger[key] = {"reason": str(reason or "")[:200], "at": stamp}
            count += 1
    return count


def apply_import(doc: Dict[str, Any], imported: Dict[str, Any]) -> Dict[str, int]:
    """Replace the ``imported`` layer. Owner entries stay and shadow the import; generated
    entries yield to the import; imported entries absent from the new file are dropped
    (an owner override that shadowed one keeps standing on its own)."""
    incoming_raw = imported.get("entries", imported) if isinstance(imported, dict) else None
    if not isinstance(incoming_raw, dict):
        raise MemoryFormatError("import must carry an entries object")
    if len(incoming_raw) > MAX_ENTRIES:
        raise MemoryFormatError("import has too many entries")
    pack = str(imported.get("pack") or "") if isinstance(imported, dict) else ""
    pack_version = str(imported.get("pack_version") or "") if isinstance(imported, dict) else ""
    incoming: Dict[str, Dict[str, Any]] = {}
    for key, raw in incoming_raw.items():
        entry = _validate_entry(key, {**raw, "provenance": "imported"} if isinstance(raw, dict) else raw)
        if pack:
            entry["pack"] = pack[:400]
        if pack_version:
            entry["pack_version"] = pack_version[:400]
        incoming[key] = entry
    replaced = shadowed = added = dropped = 0
    for key, current in list(doc["entries"].items()):
        if current.get("provenance") == "imported" and key not in incoming:
            del doc["entries"][key]
            dropped += 1
    for key in list(doc["shadow"]):
        if key not in incoming:
            del doc["shadow"][key]
    for key, entry in incoming.items():
        current = doc["entries"].get(key)
        if current and current.get("provenance") == "owner":
            doc["shadow"][key] = entry
            shadowed += 1
            continue
        if current:
            replaced += 1
        else:
            added += 1
        doc["entries"][key] = entry
    for key in ("profile", "plural_select", "plural_categories", "glossary_hash"):
        if isinstance(imported, dict) and imported.get(key) is not None and key in doc:
            doc[key] = imported[key]
    return {"added": added, "replaced": replaced, "shadowed": shadowed, "dropped": dropped}


def set_owner_entry(doc: Dict[str, Any], key: str, value: Dict[str, Any]) -> None:
    """An owner's wording for one key; a replaced import moves to ``shadow``."""
    current = doc["entries"].get(key)
    if current and current.get("provenance") == "imported":
        doc["shadow"][key] = current
    entry = _validate_entry(key, {**value, "provenance": "owner"})
    entry["at"] = utc_now_iso()
    doc["entries"][key] = entry


def remove_owner_entry(doc: Dict[str, Any], key: str) -> bool:
    """Drop an owner override; a shadowed import returns to effect."""
    current = doc["entries"].get(key)
    if not current or current.get("provenance") != "owner":
        return False
    shadowed = doc["shadow"].pop(key, None)
    if shadowed is not None:
        doc["entries"][key] = shadowed
    else:
        del doc["entries"][key]
    return True


def regenerate_reset(doc: Dict[str, Any]) -> int:
    """Drop generated entries and forget refused keys; owner and imported entries stay."""
    removed = [key for key, entry in doc["entries"].items() if entry.get("provenance") == "generated"]
    for key in removed:
        del doc["entries"][key]
    doc["refused"] = {}
    return len(removed)


def stats(doc: Optional[Dict[str, Any]], *, source_hashes: Optional[Dict[str, str]] = None,
          pending: int = 0) -> Dict[str, Any]:
    """Counts the status line shows. ``stale`` needs the current English of code/fmt keys
    (``source_hashes``); without them it is reported as unknown (``None``), never as zero."""
    if not doc:
        return {"entries": 0, "generated": 0, "owner": 0, "imported": 0, "stale": None if source_hashes is None else 0,
                "pending": pending, "refused": 0}
    counts = {"entries": len(doc["entries"]), "generated": 0, "owner": 0, "imported": 0, "pending": pending,
              "refused": len(doc.get("refused") or {})}
    stale = 0 if source_hashes is not None else None
    for key, entry in doc["entries"].items():
        counts[entry.get("provenance", "generated")] += 1
        if source_hashes is not None and key in source_hashes and entry.get("source_hash") \
                and entry["source_hash"] != source_hashes[key]:
            stale += 1
    counts["stale"] = stale
    return counts


# ---------------------------------------------------------------------------
# read seams: tr / fmt
# ---------------------------------------------------------------------------


def current_language(*, settings_reader: Optional[Callable[[], Dict[str, Any]]] = None) -> str:
    """The install's tag (``""`` = not chosen). Reads the runtime setting; a caller that
    already holds a settings document passes its reader."""
    if settings_reader is not None:
        try:
            value = (settings_reader() or {}).get(SETTING_KEY, LANGUAGE_NOT_CHOSEN)
        except Exception:
            value = LANGUAGE_NOT_CHOSEN
    else:
        from ouroboros.config import runtime_setting

        value = runtime_setting(SETTING_KEY, LANGUAGE_NOT_CHOSEN)
    return normalize_language_tag(value) or LANGUAGE_NOT_CHOSEN


def _drive_root(drive_root: Optional[pathlib.Path]) -> pathlib.Path:
    if drive_root is not None:
        return pathlib.Path(drive_root)
    from ouroboros.config import DATA_DIR

    return pathlib.Path(DATA_DIR)


def plural_category(doc: Optional[Dict[str, Any]], n: Any) -> str:
    """The CLDR category the memory's ``plural_select`` map gives ``n``; ``other`` when the
    map is absent, ``n`` is not an integer, or ``n`` is outside the recorded range and the
    language is not periodic."""
    if not doc or not isinstance(n, int) or isinstance(n, bool):
        return "other"
    select = doc.get("plural_select") or {}
    mapping = select.get("map") or {}
    value = abs(n)
    if str(value) in mapping:
        return mapping[str(value)]
    period = select.get("period")
    if period and str(value % period) in mapping:
        return mapping[str(value % period)]
    return "other"


def entry_text(entry: Optional[Dict[str, Any]], doc: Optional[Dict[str, Any]] = None,
               n: Any = None) -> Optional[str]:
    if not entry:
        return None
    if "text" in entry:
        return entry["text"]
    forms = entry.get("forms") or {}
    category = plural_category(doc, n)
    # ``other`` is the CLDR catch-all every language has; a memory written without it
    # (a hand-made import) falls back to ``many``, the form fractions and large counts
    # take in the Slavic languages, before the first form.
    return forms.get(category) or forms.get("other") or forms.get("many") or next(iter(forms.values()), None)


def tr(key: str, lang: Optional[str] = None, default: Optional[str] = None, *,
       drive_root: Optional[pathlib.Path] = None) -> Optional[str]:
    """The translation of ``key`` in the install language, else ``default``.

    English (chosen or not chosen) returns ``default`` without touching the disk. A
    missing or malformed memory also returns ``default``; the miss is the generator's
    business, not the renderer's."""
    tag = normalize_language_tag(lang) if lang is not None else current_language()
    if not tag or is_english(tag):
        return default
    doc = cached_memory(_drive_root(drive_root), tag)
    if not doc:
        return default
    lookup = key if is_code_key(key) else key_text(key)  # a text key is stored as the reader sees it
    text = entry_text(doc["entries"].get(lookup), doc)
    return text if text is not None else default


class _SafeParams(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def fmt(key: str, params: Optional[Dict[str, Any]] = None, lang: Optional[str] = None,
        default_template: Optional[str] = None, *, drive_root: Optional[pathlib.Path] = None) -> str:
    """Render a composed string: the translated template (plural form chosen by ``params['n']``)
    or ``default_template``, with ``{name}`` placeholders filled from ``params``."""
    params = dict(params or {})
    tag = normalize_language_tag(lang) if lang is not None else current_language()
    template = default_template if default_template is not None else key
    if tag and not is_english(tag):
        doc = cached_memory(_drive_root(drive_root), tag)
        if doc:
            lookup = key if is_code_key(key) else key_text(key)
            text = entry_text(doc["entries"].get(lookup), doc, params.get("n"))
            if text is not None:
                template = text
    try:
        return str(template).format_map(_SafeParams(params))
    except (ValueError, IndexError):
        return str(template)


# ---------------------------------------------------------------------------
# misses
# ---------------------------------------------------------------------------

_VOLATILE_RE = re.compile(
    r"^[\W\d_]*$"                               # digits and punctuation only
    r"|^(?:https?://|/|~/|[A-Za-z]:\\)"         # URLs and paths
    r"|^\d{4}-\d{2}-\d{2}[T ]"                  # timestamps
    r"|^[0-9a-f]{12,}$"                         # ids and hashes
)


_KEY_WHITESPACE_RE = re.compile(r"[ \t\n\r\f\v]+")


def key_text(text: object) -> str:
    """A text key as the reader sees it: runs of ASCII whitespace collapse to one space (the
    browser overlay applies the same rule in ``keyText``), so a help paragraph written over
    several indented source lines has one key everywhere."""
    return _KEY_WHITESPACE_RE.sub(" ", str(text or "")).strip()


def looks_volatile(text: object) -> bool:
    """Shape, not meaning: strings that can never translate (ids, paths, timestamps,
    bare numbers) are refused before the queue so they cannot buy a model call."""
    value = str(text or "").strip()
    if not value or len(value) > MAX_KEY_CHARS:
        return True
    if _VOLATILE_RE.search(value):
        return True
    letters = sum(1 for ch in value if ch.isalpha())
    return letters < 2


def record_missing(drive_root: pathlib.Path, tag: str, items: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """Queue keys the renderer could not translate. Bounded; duplicates count up."""
    path = pending_path(drive_root, tag)
    accepted = dropped = 0
    refused = (cached_memory(drive_root, tag) or {}).get("refused") or {}
    with _memory_lock(path):
        queue = read_json_dict(path) or {}
        entries = queue.get("items") if isinstance(queue.get("items"), dict) else {}
        for item in items:
            key = str((item or {}).get("key") or "") if isinstance(item, dict) else str(item or "")
            if not is_code_key(key):
                key = key_text(key)  # the browser's rule: one key per sentence, whatever the source layout
            if not key or len(key) > MAX_KEY_CHARS or key in _FORBIDDEN_KEYS or key in refused:
                dropped += 1
                continue
            source_text = split_scope(key)[0] if not is_code_key(key) else key
            if not is_code_key(key) and looks_volatile(source_text):
                dropped += 1
                continue
            existing = entries.get(key)
            if existing is None:
                if len(entries) >= MAX_PENDING:
                    dropped += 1
                    continue
                context = (item or {}).get("context") if isinstance(item, dict) else None
                entries[key] = {"context": context if isinstance(context, dict) else {},
                                "first_seen": utc_now_iso(), "count": 1}
            else:
                existing["count"] = int(existing.get("count") or 0) + 1
            accepted += 1
        atomic_write_json(path, {"schema": SCHEMA, "language": normalize_language_tag(tag),
                                 "items": entries, "updated_at": utc_now_iso()}, trailing_newline=True)
    return {"accepted": accepted, "dropped": dropped, "pending": len(entries)}


def pending_count(drive_root: pathlib.Path, tag: str) -> int:
    try:
        queue = read_json_dict(pending_path(drive_root, tag)) or {}
    except Exception:
        return 0
    items = queue.get("items")
    return len(items) if isinstance(items, dict) else 0


def take_pending(drive_root: pathlib.Path, tag: str, limit: int) -> List[Tuple[str, Dict[str, Any]]]:
    """Remove and return up to ``limit`` queued keys (oldest first) for one generation batch."""
    path = pending_path(drive_root, tag)
    taken: List[Tuple[str, Dict[str, Any]]] = []
    with _memory_lock(path):
        queue = read_json_dict(path) or {}
        entries = queue.get("items") if isinstance(queue.get("items"), dict) else {}
        for key in list(entries)[: max(0, int(limit))]:
            taken.append((key, entries.pop(key)))
        atomic_write_json(path, {"schema": SCHEMA, "language": normalize_language_tag(tag),
                                 "items": entries, "updated_at": utc_now_iso()}, trailing_newline=True)
    return taken


def requeue_pending(drive_root: pathlib.Path, tag: str, items: Iterable[Tuple[str, Dict[str, Any]]]) -> None:
    """Put keys back after a failed batch, keeping their first-seen facts."""
    path = pending_path(drive_root, tag)
    with _memory_lock(path):
        queue = read_json_dict(path) or {}
        entries = queue.get("items") if isinstance(queue.get("items"), dict) else {}
        for key, facts in items:
            if key not in entries and len(entries) < MAX_PENDING:
                entries[key] = facts
        atomic_write_json(path, {"schema": SCHEMA, "language": normalize_language_tag(tag),
                                 "items": entries, "updated_at": utc_now_iso()}, trailing_newline=True)


def list_languages(drive_root: pathlib.Path) -> List[Dict[str, Any]]:
    """Languages present on disk, with their label and counts, for the Settings select."""
    out: List[Dict[str, Any]] = []
    root = memory_dir(drive_root)
    if not root.exists():
        return out
    for path in sorted(root.glob("*.json")):
        if path.name.endswith(".pending.json"):
            continue
        tag = normalize_language_tag(path.stem)
        if not tag:
            continue
        doc = cached_memory(drive_root, tag)
        out.append({
            "language": tag,
            "label": (doc or {}).get("profile", {}).get("label") or tag,
            "entries": len((doc or {}).get("entries") or {}),
            "pending": pending_count(drive_root, tag),
            "malformed": doc is None,
        })
    return out


__all__ = [
    "SCHEMA", "MEMORY_DIR_REL", "CODE_PREFIX", "SCOPE_SEPARATOR", "MemoryFormatError", "RevisionConflict",
    "memory_dir", "memory_path", "pending_path", "source_hash", "placeholders", "is_code_key", "split_scope",
    "new_memory", "validate_memory", "load_memory", "cached_memory", "update_memory", "inline_slots_balanced",
    "apply_generated", "refuse_keys", "apply_import", "set_owner_entry", "remove_owner_entry", "regenerate_reset", "stats",
    "current_language", "plural_category", "entry_text", "tr", "fmt",
    "looks_volatile", "record_missing", "pending_count", "take_pending", "requeue_pending", "list_languages",
]
