"""The translation memory (ouroboros/i18n_memory.py) and the language tag leaf (ouroboros/ui_language.py).

Both directions for every guard: what is accepted AND what is refused, what a write
changes AND what it leaves alone.
"""
from __future__ import annotations

import json

import pytest

from ouroboros import i18n_memory as memory
from ouroboros.ui_language import (
    invented_language_tag,
    is_english,
    language_file_name,
    normalize_language_tag,
)

# Russian CLDR categories for 0..100 as Intl.PluralRules would write them (a subset is
# enough for the selection tests; the real map is produced by the browser at save time).
RU_SELECT = {"map": {str(n): ("one" if n % 10 == 1 and n % 100 != 11
                              else "few" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14
                              else "many") for n in range(0, 101)}, "period": 100}


def test_language_tags_are_canonical_open_and_never_guessed():
    assert normalize_language_tag("ru") == "ru"
    assert normalize_language_tag(" RU ") == "ru"
    assert normalize_language_tag("pt-br") == "pt-BR"
    assert normalize_language_tag("zh-hans") == "zh-Hans"
    assert normalize_language_tag("sr-cyrl-rs") == "sr-Cyrl-RS"
    assert normalize_language_tag("qya") == "qya"  # Quenya has an ISO 639-3 code
    assert normalize_language_tag("art-x-elvish") == "art-x-elvish"
    assert normalize_language_tag("") == ""  # not chosen
    # Names and descriptions are not tags: turning them into one is the generator's job.
    for bad in ("Russian", "русский", "ru_RU", "ru/RU", "../x", "a", "x" * 60, None, 5):
        assert normalize_language_tag(bad) is None, bad
    assert is_english("") and is_english("en") and is_english("en-GB") and is_english(None)
    assert not is_english("ru") and not is_english("art-x-elvish")
    assert language_file_name("PT-br") == "pt-BR"
    with pytest.raises(ValueError):
        language_file_name("Russian")
    assert invented_language_tag("Elvish Quenya!") == "art-x-elvish-quenya"
    assert invented_language_tag("") == "art-x-lang"


def test_memory_path_refuses_non_tags_and_stays_inside_state(tmp_path):
    assert memory.memory_path(tmp_path, "ru") == tmp_path / "state" / "i18n" / "ru.json"
    with pytest.raises(ValueError):
        memory.memory_path(tmp_path, "../../etc")
    with pytest.raises(ValueError):
        memory.memory_path(tmp_path, "Russian")


def test_validate_memory_accepts_schema_one_and_refuses_everything_else(tmp_path):
    doc = memory.new_memory("ru", profile={"label": "Русский"}, plural_select=RU_SELECT)
    doc["entries"]["Settings"] = {"text": "Настройки", "provenance": "generated"}
    doc["entries"]["{n} notes"] = {"forms": {"one": "{n} заметка", "few": "{n} заметки", "many": "{n} заметок"},
                                   "provenance": "imported"}
    doc["entries"]["code:task.headline.done"] = {"text": "Готово", "provenance": "owner"}
    normalized = memory.validate_memory(doc)
    assert normalized["language"] == "ru" and normalized["profile"]["label"] == "Русский"
    assert normalized["plural_select"]["map"]["1"] == "one"

    def refused(mutate):
        broken = json.loads(json.dumps(doc))
        mutate(broken)
        with pytest.raises(memory.MemoryFormatError):
            memory.validate_memory(broken)

    refused(lambda d: d.update(schema=2))
    refused(lambda d: d.update(language="Russian"))
    refused(lambda d: d["entries"].__setitem__("__proto__", {"text": "x", "provenance": "owner"}))
    refused(lambda d: d["entries"].__setitem__("Settings", {"text": "<b>Настройки</b>", "provenance": "owner"}))
    refused(lambda d: d["entries"].__setitem__("Settings", {"text": "{n} Настройки", "provenance": "owner"}))
    refused(lambda d: d["entries"].__setitem__("Settings", {"provenance": "owner"}))
    refused(lambda d: d["entries"].__setitem__("Settings", {"text": "a", "forms": {"other": "b"}, "provenance": "owner"}))
    refused(lambda d: d["entries"].__setitem__("Settings", {"text": "a", "provenance": "machine"}))
    refused(lambda d: d.update(plural_select={"map": {"101": "other"}}))
    refused(lambda d: d.update(profile={"direction": "sideways"}))
    # Tag placeholders fold open/close into one token; a value may reuse them.
    doc["entries"]["Open <1>Settings</1> to continue"] = {"text": "Откройте <1>Настройки</1>", "provenance": "generated"}
    memory.validate_memory(doc)
    refused(lambda d: d["entries"].__setitem__("Open <1>Settings</1> to continue",
                                               {"text": "Откройте <2>Настройки</2>", "provenance": "generated"}))


def test_update_memory_is_the_one_writer_with_revisions(tmp_path):
    with pytest.raises(FileNotFoundError):
        memory.update_memory(tmp_path, "ru", lambda d: d)
    created = memory.update_memory(tmp_path, "ru", lambda d: None, create=True, profile={"label": "Русский"})
    assert created["revision"] == 0 and memory.memory_path(tmp_path, "ru").exists()

    def add(doc):
        doc["entries"]["Settings"] = {"text": "Настройки", "provenance": "generated"}
        return doc

    first = memory.update_memory(tmp_path, "ru", add, expected_revision=0)
    assert first["revision"] == 1
    with pytest.raises(memory.RevisionConflict):
        memory.update_memory(tmp_path, "ru", add, expected_revision=0)
    unchanged = memory.update_memory(tmp_path, "ru", lambda d: None)
    assert unchanged["revision"] == 1  # a no-change decision rewrites nothing
    # A malformed file is refused on read and never overwritten as an empty dictionary.
    path = memory.memory_path(tmp_path, "ru")
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(memory.MemoryFormatError):
        memory.update_memory(tmp_path, "ru", add, create=True)
    assert path.read_text(encoding="utf-8") == "{not json"
    assert memory.cached_memory(tmp_path, "ru") is None  # the read seam swallows, logs, renders English


def test_precedence_owner_over_imported_over_generated(tmp_path):
    memory.update_memory(tmp_path, "ru", lambda d: None, create=True)

    def generate(doc):
        assert memory.apply_generated(doc, {"Settings": {"text": "Настройки"}, "Files": {"text": "Файлы"}},
                                      model="light", attempt_id="a1") == 2
        return doc

    doc = memory.update_memory(tmp_path, "ru", generate)
    assert doc["entries"]["Settings"]["model"] == "light" and doc["entries"]["Settings"]["attempt_id"] == "a1"

    pack = {"schema": 1, "language": "ru", "pack": "acme", "pack_version": "2.0",
            "entries": {"Settings": {"text": "Параметры"}, "Skills": {"text": "Навыки"}}}
    counts = {}
    doc = memory.update_memory(tmp_path, "ru", lambda d: (counts.update(memory.apply_import(d, pack)), d)[1])
    assert counts == {"added": 1, "replaced": 1, "shadowed": 0, "dropped": 0}
    assert doc["entries"]["Settings"] == {"text": "Параметры", "provenance": "imported", "pack": "acme", "pack_version": "2.0"}
    assert doc["entries"]["Files"]["provenance"] == "generated"  # untouched by the import

    def owner(doc):
        memory.set_owner_entry(doc, "Settings", {"text": "Настройки"})
        memory.set_owner_entry(doc, "Files", {"text": "Файлы!"})
        return doc

    doc = memory.update_memory(tmp_path, "ru", owner)
    assert doc["entries"]["Settings"]["provenance"] == "owner"
    assert doc["shadow"]["Settings"]["text"] == "Параметры"  # the import survives under the override
    # Generation never overwrites owner or imported entries.
    doc = memory.update_memory(tmp_path, "ru", lambda d: (memory.apply_generated(
        d, {"Settings": {"text": "X"}, "Skills": {"text": "Y"}, "New": {"text": "Новое"}}, model="light"), d)[1])
    assert doc["entries"]["Settings"]["text"] == "Настройки" and doc["entries"]["Skills"]["text"] == "Навыки"
    assert doc["entries"]["New"]["provenance"] == "generated"
    # Re-import replaces the imported layer only; the owner's override stands, its shadow follows the new import.
    pack2 = {"schema": 1, "language": "ru", "entries": {"Settings": {"text": "Опции"}}}
    doc = memory.update_memory(tmp_path, "ru", lambda d: (memory.apply_import(d, pack2), d)[1])
    assert doc["entries"]["Settings"]["provenance"] == "owner" and doc["shadow"]["Settings"]["text"] == "Опции"
    assert "Skills" not in doc["entries"]  # dropped: not in the new pack
    # Regenerate drops generated entries only.
    doc = memory.update_memory(tmp_path, "ru", lambda d: (memory.regenerate_reset(d), d)[1])
    assert set(doc["entries"]) == {"Settings", "Files"}
    # Removing the owner override restores the shadowed import.
    doc = memory.update_memory(tmp_path, "ru", lambda d: (memory.remove_owner_entry(d, "Settings"), d)[1])
    assert doc["entries"]["Settings"] == {"text": "Опции", "provenance": "imported"}
    assert "Settings" not in doc["shadow"]


def test_stats_count_provenance_and_stale_only_with_source_hashes(tmp_path):
    memory.update_memory(tmp_path, "ru", lambda d: None, create=True)
    doc = memory.update_memory(tmp_path, "ru", lambda d: (
        memory.apply_generated(d, {"code:task.headline.done": {"text": "Готово"}}, model="m",
                               source_hashes={"code:task.headline.done": memory.source_hash("Done")}),
        memory.set_owner_entry(d, "Settings", {"text": "Настройки"}), d)[2])
    assert memory.stats(doc)["stale"] is None  # unknown without the current English
    stale = memory.stats(doc, source_hashes={"code:task.headline.done": memory.source_hash("Done!")}, pending=3)
    assert stale == {"entries": 2, "generated": 1, "owner": 1, "imported": 0, "pending": 3, "stale": 1, "refused": 0}
    fresh = memory.stats(doc, source_hashes={"code:task.headline.done": memory.source_hash("Done")})
    assert fresh["stale"] == 0
    assert memory.stats(None)["entries"] == 0


def test_tr_and_fmt_read_the_memory_and_fall_back_to_english(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_UI_LANGUAGE", "ru")
    assert memory.tr("code:task.headline.done", default="Done", drive_root=tmp_path) == "Done"  # no memory yet
    memory.update_memory(tmp_path, "ru", lambda d: (memory.apply_generated(d, {
        "code:task.headline.done": {"text": "Готово"},
        "{n} notes": {"forms": {"one": "{n} заметка", "few": "{n} заметки", "many": "{n} заметок"}},
        "Running {name}": {"text": "Выполняется {name}"},
    }, model="m"), d)[1], create=True, plural_select=RU_SELECT)
    assert memory.tr("code:task.headline.done", default="Done", drive_root=tmp_path) == "Готово"
    assert memory.tr("code:unknown", default="Raw", drive_root=tmp_path) == "Raw"
    assert memory.fmt("{n} notes", {"n": 1}, drive_root=tmp_path) == "1 заметка"
    assert memory.fmt("{n} notes", {"n": 3}, drive_root=tmp_path) == "3 заметки"
    assert memory.fmt("{n} notes", {"n": 11}, drive_root=tmp_path) == "11 заметок"
    assert memory.fmt("{n} notes", {"n": 121}, drive_root=tmp_path) == "121 заметка"  # periodic map
    assert memory.fmt("{n} notes", {"n": 2.5}, drive_root=tmp_path) == "2.5 заметок"  # non-integer → other
    assert memory.fmt("Running {name}", {"name": "gpt-x"}, drive_root=tmp_path) == "Выполняется gpt-x"
    assert memory.fmt("Missing {name}", {"name": "x"}, drive_root=tmp_path) == "Missing x"  # English template
    # English (chosen or not) never touches the disk.
    monkeypatch.setenv("OUROBOROS_UI_LANGUAGE", "en")
    assert memory.tr("code:task.headline.done", default="Done", drive_root=tmp_path) == "Done"
    monkeypatch.setenv("OUROBOROS_UI_LANGUAGE", "")
    assert memory.fmt("{n} notes", {"n": 1}, drive_root=tmp_path) == "1 notes"
    # An explicit language wins over the setting.
    assert memory.tr("code:task.headline.done", lang="ru", default="Done", drive_root=tmp_path) == "Готово"
    assert memory.current_language() == ""


def test_misses_are_shape_filtered_bounded_and_deduplicated(tmp_path):
    assert memory.looks_volatile("2026-10-03T12:00:00Z") and memory.looks_volatile("/api/tasks/abc")
    assert memory.looks_volatile("3f2a9c8e7d6b5a4c3b2a") and memory.looks_volatile("42") and memory.looks_volatile("x")
    assert not memory.looks_volatile("Settings") and not memory.looks_volatile("3 notes")
    result = memory.record_missing(tmp_path, "ru", [
        {"key": "Settings", "context": {"page": "settings"}}, {"key": "Settings"},
        {"key": "/var/log"}, {"key": "code:task.headline.done"}, {"key": ""},
    ])
    assert result == {"accepted": 3, "dropped": 2, "pending": 2}
    assert memory.pending_count(tmp_path, "ru") == 2
    taken = memory.take_pending(tmp_path, "ru", 1)
    assert taken[0][0] == "Settings" and taken[0][1]["count"] == 2 and taken[0][1]["context"] == {"page": "settings"}
    assert memory.pending_count(tmp_path, "ru") == 1
    memory.requeue_pending(tmp_path, "ru", taken)
    assert memory.pending_count(tmp_path, "ru") == 2
    # The queue is bounded: the 2 001st distinct key is dropped, not stored.
    bulk = [{"key": f"String number {i}"} for i in range(memory.MAX_PENDING + 5)]
    bulk_result = memory.record_missing(tmp_path, "ru", bulk)
    assert bulk_result["pending"] == memory.MAX_PENDING and bulk_result["dropped"] >= 5
    languages = memory.list_languages(tmp_path)
    assert languages == [] or all(item["language"] != "ru.pending" for item in languages)


def test_placeholders_include_format_specs_and_inline_slots_must_balance():
    assert memory.placeholders("Spent ${spent_usd:.4f} of {total} <1>x</1>") == {"{spent_usd:.4f}", "{total}", "<1>"}
    assert memory.inline_slots_balanced("Type <1>pt-BR</1> here") is True
    assert memory.inline_slots_balanced("Type <1>pt-BR here") is False
    assert memory.inline_slots_balanced("<1>a</1> <1>b</1>") is False, "a slot used twice"
    doc = memory.new_memory("ru")
    doc["entries"]["Spent {amount:.2f}"] = {"text": "Потрачено {amount:.2f}", "provenance": "generated"}
    memory.validate_memory(doc)
    doc["entries"]["Spent {amount:.2f}"] = {"text": "Потрачено {amount:.4f}", "provenance": "generated"}
    with pytest.raises(memory.MemoryFormatError):
        memory.validate_memory(doc)
    doc["entries"]["Spent {amount:.2f}"] = {"text": "Потрачено", "provenance": "generated"}
    with pytest.raises(memory.MemoryFormatError, match="differ from the source"):
        memory.validate_memory(doc)


def test_refused_ledger_and_source_field_validate_and_count(tmp_path):
    doc = memory.new_memory("ru")
    doc["refused"] = {"Files": {"reason": "no valid answer", "at": "2026-10-03T00:00:00Z"}}
    doc["entries"]["code:task.progress.thinking"] = {"text": "Думаю", "provenance": "generated", "source": "Thinking"}
    normalized = memory.validate_memory(doc)
    assert normalized["refused"]["Files"]["reason"] == "no valid answer"
    assert normalized["entries"]["code:task.progress.thinking"]["source"] == "Thinking"
    assert memory.stats(normalized)["refused"] == 1
    doc["refused"] = ["Files"]
    with pytest.raises(memory.MemoryFormatError):
        memory.validate_memory(doc)
    assert memory.refuse_keys(normalized, ["Files", "Open"], reason="bound") == 1
    assert set(normalized["refused"]) == {"Files", "Open"}
    assert memory.regenerate_reset(normalized) == 1 and normalized["refused"] == {}


def test_a_help_paragraph_is_an_ordinary_key_and_source_layout_does_not_split_it(tmp_path):
    memory.update_memory(tmp_path, "ru", lambda doc: None, create=True)
    sentence = ("Interface language for this installation: the desktop window, browsers, the Telegram app and "
                "Ouroboros's own lines about tasks. Translations are generated by the light model and stored as install data.")
    laid_out = f"{sentence}\n           {sentence}\n           {sentence}"
    key = " ".join([sentence] * 3)
    assert 400 < len(key) < memory.MAX_KEY_CHARS
    assert not memory.looks_volatile(key) and memory.looks_volatile("x" * (memory.MAX_KEY_CHARS + 1))
    assert memory.key_text(laid_out) == key and memory.key_text(" a\u00a0b ") == "a\u00a0b", "ASCII whitespace only; typography stays"
    result = memory.record_missing(tmp_path, "ru", [{"key": laid_out, "context": {}}, {"key": "x" * 2001, "context": {}}])
    assert result == {"accepted": 1, "dropped": 1, "pending": 1}
    assert list(dict(memory.take_pending(tmp_path, "ru", 10))) == [key]


def test_markup_the_source_has_is_not_injected_markup():
    key = "MCP exposes tools as <1>mcp_<server>__<tool></1> after refresh."
    doc = memory.new_memory("ru")
    doc["entries"][key] = {"text": "MCP открывает инструменты как <1>mcp_<server>__<tool></1> после обновления.", "provenance": "generated"}
    assert memory.validate_memory(doc)["entries"][key]["text"].startswith("MCP открывает")
    doc["entries"][key] = {"text": "MCP <b>открывает</b> <1>mcp_<server>__<tool></1>.", "provenance": "generated"}
    with pytest.raises(memory.MemoryFormatError, match="carries markup") as refused:
        memory.validate_memory(doc)
    assert "`source`" not in str(refused.value), "a text key is its own source: nothing to add to the pack"
    doc["entries"] = {"code:tg.menu.hello": {"text": "Привет <server>", "provenance": "generated"}}
    with pytest.raises(memory.MemoryFormatError, match="carries markup .*states it as `source`"):
        memory.validate_memory(doc)  # a code entry without its English: the refusal says what the pack lacks


def test_brace_fields_the_source_lacks_are_refused_and_a_code_entry_is_judged_against_its_source(tmp_path):
    doc = memory.new_memory("ru")
    doc["entries"]["code:tg.menu.lang_changed"] = {"text": "Язык: {language} {language.foo}", "provenance": "generated",
                                                   "source": "Interface language changed to {language}"}
    with pytest.raises(memory.MemoryFormatError, match="field the source lacks"):
        memory.validate_memory(doc)
    doc["entries"]["code:tg.menu.lang_changed"] = {"text": "Язык: {language}", "provenance": "generated",
                                                   "source": "Interface language changed to {language}"}
    assert memory.validate_memory(doc)["entries"]["code:tg.menu.lang_changed"]["text"] == "Язык: {language}"
    doc["entries"]["code:tg.menu.lang_title"] = {"text": "Отправьте `/language <name>`", "provenance": "generated",
                                                 "source": "send `/language <name>`"}
    assert "<name>" in memory.validate_memory(doc)["entries"]["code:tg.menu.lang_title"]["text"]
    doc["entries"]["code:tg.menu.lang_title"] = {"text": "Отправьте `/language <name>`", "provenance": "imported"}
    with pytest.raises(memory.MemoryFormatError, match="carries markup"):
        memory.validate_memory(doc)  # no recorded source: nothing says the angle text is the source's own
    candidate = memory.validate_candidate("code:tg.menu.lang_title", {"text": "Отправьте `/language <name>`"}, source="send `/language <name>`")
    assert candidate["source"] == "send `/language <name>`"


def test_text_keys_are_read_as_the_reader_sees_them(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_UI_LANGUAGE", "ru")
    memory.update_memory(tmp_path, "ru", lambda doc: (memory.apply_generated(doc, {"Open the file now": {"text": "Откройте файл"}}, model="t"), doc)[1], create=True)
    assert memory.tr("Open the  file\n   now", drive_root=tmp_path) == "Откройте файл", "a relayed sentence with source-layout whitespace finds its entry"
    assert memory.fmt("Open   the file now", drive_root=tmp_path) == "Откройте файл"


def test_inline_slots_are_flat_and_may_be_reordered_but_never_nested_or_crossed():
    assert memory.inline_slots_balanced("Use <1>127.0.0.1</1> or <2>0.0.0.0</2>.")
    assert memory.inline_slots_balanced("<2>0.0.0.0</2> first, then <1>127.0.0.1</1>."), "sibling slots may change order"
    assert not memory.inline_slots_balanced("Use <1>127.0.0.1 <2>0.0.0.0</2></1>."), "nested: the renderer would drop the inner element"
    assert not memory.inline_slots_balanced("Use <1>127.0.0.1 <2>0.0.0.0</1></2>."), "crossing"
    assert not memory.inline_slots_balanced("<1>a</1> and <1>b</1>")
    key = "Use <1>127.0.0.1</1> or <2>0.0.0.0</2> for LAN."
    doc = memory.new_memory("de")
    doc["entries"][key] = {"text": "Nutze <1>127.0.0.1 <2>0.0.0.0</2></1> für LAN.", "provenance": "imported"}
    with pytest.raises(memory.MemoryFormatError, match="unbalanced inline slot"):
        memory.validate_memory(doc)
    doc["entries"][key] = {"text": "Für LAN <2>0.0.0.0</2>, sonst <1>127.0.0.1</1>.", "provenance": "imported"}
    assert memory.validate_memory(doc)["entries"][key]["text"].startswith("Für LAN")
