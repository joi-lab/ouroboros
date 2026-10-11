"""The actual model input names observation without changing source or media custody."""

from copy import deepcopy
from dataclasses import replace
import base64
import json

import pytest

from ouroboros.context import build_user_content
from ouroboros.presence_context import build_presence_context_section
from ouroboros.presence_runner import _build_task
from tests.test_attachment_staging import _PNG_BYTES
from tests.test_presence_runner import _admission, _event


def test_observed_message_keeps_source_and_attributes_the_actual_event(tmp_path):
    text = "<@person-8>, can you check this?\nThe latest numbers are in the document."
    event = replace(_event(), text=text)
    task = _build_task(_admission(), event, drive_root=tmp_path, staged_files=())
    original = deepcopy(task)
    content = json.loads(json.dumps(build_user_content(task), ensure_ascii=False, sort_keys=True))

    assert content.startswith("[Observed Presence event]")
    assert content.endswith(text) and content.count(text) == 1
    assert '"source_event_id": "telegram:bot-1:42"' in content
    assert '"platform_actor_id": "user-7"' in content
    assert '"conversation_id": "room-1"' in content
    assert "does not establish that its author addresses you" in content
    assert task == original and task["text"] == text
    assert task["metadata"]["presence"]["observed_text"] == text
    # The system already supplies the complete event/profile, not another copy of the body.
    section = build_presence_context_section(tmp_path, task["metadata"]["presence"])
    assert text not in section


@pytest.mark.parametrize("with_attachment", [False, True])
def test_non_text_event_does_not_attribute_host_context_to_its_actor(tmp_path, with_attachment):
    files = ()
    if with_attachment:
        file = tmp_path / "notes.txt"
        file.write_text("Received file content.", encoding="utf-8")
        files = (file,)
    message = {"message_id": "42"} if with_attachment else {"kind": "reaction_added", "reaction": "thumbsup"}
    event = replace(_event(), text="", message=message)
    task = _build_task(_admission(), event, drive_root=tmp_path, staged_files=files)
    original = deepcopy(task)
    content = build_user_content(task)

    assert content.startswith("[Observed Presence event]")
    assert "The event supplied no text" in content
    assert "placeholder or attachment declaration below is host context" in content
    assert content.endswith(task["text"])
    assert task["metadata"]["presence"]["observed_text"] == ""
    assert ("[ATTACHMENTS]" in content) == with_attachment
    if not with_attachment:
        assert task["text"] == "(empty presence event)"
    assert task == original


@pytest.mark.parametrize("marker", ["actor", "message"])
def test_proactive_cycle_is_initiating_context_not_a_correspondent_message(tmp_path, marker):
    event = replace(_event(), text="Consider sharing the result if it is useful.",
                    **{marker: {"kind": "proactive_initiation"}})
    task = _build_task(_admission(), event, drive_root=tmp_path, staged_files=())
    content = build_user_content(task)

    assert content.startswith("[Self-initiated Presence cycle]")
    assert "not a new message from a correspondent" in content
    assert "does not itself send anything" in content
    assert content.endswith(event.text) and content.count(event.text) == 1


@pytest.mark.parametrize("extra", [
    {"_is_direct_chat": True},
    {"_presence_origin": True, "source": "presence_promote"},
    {"metadata": {"source": "schedule_followup"}},
])
def test_owner_and_inherited_work_keep_their_own_unchanged_input(tmp_path, extra):
    source = _build_task(_admission(), _event(), drive_root=tmp_path, staged_files=())
    task = {"type": "task", "text": "Prepare the requested comparison.", **extra}
    task["metadata"] = {**task.get("metadata", {}), "presence": source["metadata"]["presence"]}
    original = deepcopy(task)

    assert build_user_content(task) == "Prepare the requested comparison."
    assert task == original


@pytest.mark.parametrize("media", ["legacy", "staged", "both"])
def test_presence_media_framing_preserves_caption_dedup_and_native_blocks(tmp_path, media):
    staged = ()
    if media in {"staged", "both"}:
        file = tmp_path / "image.png"
        file.write_bytes(_PNG_BYTES)
        staged = (file,)
    event = replace(_event(), text="Look at this picture.")
    task = _build_task(_admission(), event, drive_root=tmp_path, staged_files=staged)
    task["drive_root"] = str(tmp_path)
    if media in {"legacy", "both"}:
        task.update(image_base64=base64.b64encode(_PNG_BYTES).decode("ascii"),
                    image_mime="image/png", image_caption=task["text"])
    original = deepcopy(task)
    baseline = build_user_content({**task, "_presence_turn": False})
    content = json.loads(json.dumps(build_user_content(task), ensure_ascii=False, sort_keys=True))

    assert isinstance(content, list) and len(content) == len(baseline)
    assert content[0]["text"].startswith("[Observed Presence event]")
    assert content[0]["text"].endswith(baseline[0]["text"])
    assert content[0]["text"].count(event.text) == 1
    assert content[1:] == baseline[1:]
    assert any(block.get("type") == "image_url" for block in content)
    assert task == original


def test_unrecorded_source_is_disclosed_without_guessing_from_assembled_text():
    task = {"_presence_turn": True, "text": "An older assembled input.",
            "metadata": {"presence": {"event": {"source_event_id": "old-event"}}}}
    content = build_user_content(task)
    assert "Source text was not recorded separately" in content
    assert content.endswith(task["text"])


def test_runner_retains_exact_source_even_when_assembly_normalizes_whitespace(tmp_path):
    text = "  A received sentence.\n"
    task = _build_task(_admission(), replace(_event(), text=text), drive_root=tmp_path, staged_files=())
    assert task["metadata"]["presence"]["observed_text"] == text
    assert task["text"] == text.strip()  # existing presentation behavior, not a rewritten source
