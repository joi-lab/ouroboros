"""v6.37.0 guard (C4.4): a project's chat must include its whole subagent TREE.
project_chat_for_task_tree resolves membership by lineage (own binding -> parent ->
root) so a subagent (never bound itself) routes to its root project's thread — the
cyber-racing 'subagents vanished from the project chat' gap. Exercised against the
real durable bindings store (the tree resolver reads it once for all three probes)."""


def test_project_chat_for_task_tree_inherits_from_root(tmp_path):
    from ouroboros import projects_registry as pr

    root_chat = int(pr.create_project(tmp_path, "rootproj")["chat_id"])
    pr.bind_task_to_project(tmp_path, "root", "rootproj", origin={"absent": "system"})

    # own binding wins
    assert pr.project_chat_for_task_tree(tmp_path, "root") == root_chat
    # a child with no own/parent binding inherits from its root
    assert pr.project_chat_for_task_tree(
        tmp_path, "child", parent_task_id="mid", root_task_id="root"
    ) == root_chat
    # a child inherits from a bound PARENT before the root
    mid_chat = int(pr.create_project(tmp_path, "midproj")["chat_id"])
    pr.bind_task_to_project(tmp_path, "mid", "midproj", origin={"absent": "system"})
    assert pr.project_chat_for_task_tree(
        tmp_path, "child", parent_task_id="mid", root_task_id="root"
    ) == mid_chat
    # nothing in the lineage is bound -> 0 (stays in main chat)
    assert pr.project_chat_for_task_tree(
        tmp_path, "x", parent_task_id="y", root_task_id="z"
    ) == 0
