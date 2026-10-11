"""The resident carrier of understanding: an authored summary, and a visible gap.

A note's authored summary is what stays in front of the model through the index, and an
unauthored common orientation is a VISIBLE GAP rather than silence. The local compactor
must keep both sections when it shrinks a context for a local model.
"""

from types import SimpleNamespace

import pytest

from ouroboros import context
from ouroboros.knowledge import resolve_knowledge_address, write_knowledge_note
from ouroboros.memory import Memory

GAP_CALL = "knowledge_write(topic='overview', scope='global', content=...)"


def _env(tmp_path):
    repo, drive = tmp_path / "repo", tmp_path / "data"
    repo.mkdir(parents=True, exist_ok=True)
    memory = Memory(drive_root=drive, repo_dir=repo)
    memory.ensure_files()
    return SimpleNamespace(repo_dir=repo, drive_root=drive, repo_path=lambda p: repo / p,
                           drive_path=lambda p: drive / p)


def _note(env, topic="people/alex", summary="Details belong to their source."):
    write_knowledge_note(resolve_knowledge_address(env.drive_root, topic),
                         f"---\ntype: understanding\ntitle: Alex\nsummary: {summary}\n---\nDetailed source.")


def test_missing_common_orientation_renders_a_visible_gap(tmp_path):
    env = _env(tmp_path)
    assert not resolve_knowledge_address(env.drive_root, "overview", "global").path.exists()
    sections = "\n".join(context.build_knowledge_sections(env))
    assert "## Shared understanding" in sections
    assert "Not authored yet." in sections and GAP_CALL in sections
    assert "loaded here in every context" in sections


def test_present_but_empty_orientation_renders_the_same_gap(tmp_path):
    env = _env(tmp_path)
    address = resolve_knowledge_address(env.drive_root, "overview", "global")
    write_knowledge_note(address, "---\ntype: overview\ntitle: Shared\n---\n   \n")
    assert address.path.exists()
    sections = "\n".join(context.build_knowledge_sections(env))
    assert "## Shared understanding" in sections and "Not authored yet." in sections


def test_the_gap_line_carries_no_timestamp_so_the_cached_block_stays_stable(tmp_path):
    """The section rides the semi-stable cached block; a clock in it would break caching."""
    env = _env(tmp_path)
    first = "\n".join(context.build_knowledge_sections(env))
    second = "\n".join(context.build_knowledge_sections(env))
    assert first == second
    gap = context._SHARED_UNDERSTANDING_GAP
    assert not any(ch.isdigit() for ch in gap)


def test_authored_orientation_replaces_the_gap_and_keeps_note_summaries(tmp_path):
    env = _env(tmp_path)
    _note(env)
    write_knowledge_note(resolve_knowledge_address(env.drive_root, "overview", "global"),
                         "Our shared understanding is current.")
    sections = "\n".join(context.build_knowledge_sections(env))
    assert "Our shared understanding is current." in sections
    assert "Not authored yet." not in sections
    assert "Details belong to their source." in sections


def test_note_summaries_are_resident_without_any_authored_orientation(tmp_path):
    env = _env(tmp_path)
    _note(env)
    sections = "\n".join(context.build_knowledge_sections(env))
    assert "Not authored yet." in sections
    assert "Details belong to their source." in sections


def test_note_summaries_are_resident_when_the_generated_index_is_absent(tmp_path):
    env = _env(tmp_path)
    _note(env)
    index = resolve_knowledge_address(env.drive_root, "overview", "global").shelf / "index-full.md"
    index.unlink()  # a note landed but its index rebuild did not
    sections = "\n".join(context.build_knowledge_sections(env))
    assert "Details belong to their source." in sections


@pytest.mark.parametrize("project_id", ["", "proj-1"])
def test_the_gap_travels_into_every_room(tmp_path, project_id):
    env = _env(tmp_path)
    sections = "\n".join(context.build_knowledge_sections(env, project_id=project_id))
    assert "## Shared understanding" in sections and "Not authored yet." in sections


def test_local_compactor_preserves_production_identity_and_shared_understanding():
    """Production headings carry a provenance suffix; exact-only matching dropped them."""
    from ouroboros.llm_local import _compact_local_text

    text = (
        "## Identity (from `memory/identity.md` — already loaded; do not re-read)\n\nIDENTITY BODY\n\n"
        "## Shared understanding\n\nSource: knowledge_read(topic='overview', scope='global').\n\nORIENTATION BODY\n\n"
        "## Knowledge base\n\n" + ("K" * 4000) + "\n"
    )
    compacted = _compact_local_text(text, "semi_stable")
    assert "IDENTITY BODY" in compacted
    assert "ORIENTATION BODY" in compacted
    assert "[Compacted for local-model context" in compacted
    assert "K" * 4000 not in compacted


def test_local_compactor_still_preserves_the_suffixed_memory_registry():
    from ouroboros.llm_local import _compact_local_text

    text = ("## Memory Registry (what I know / don't know)\n\nREGISTRY BODY\n\n"
            "## Recent tools\n\n" + ("T" * 4000) + "\n")
    compacted = _compact_local_text(text, "dynamic")
    assert "REGISTRY BODY" in compacted
    assert "T" * 4000 not in compacted
