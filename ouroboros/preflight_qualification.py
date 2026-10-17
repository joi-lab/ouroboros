"""Full exact-commit test evidence for adoption and evolution publication.

Neither consumer runs tests. The full battery runs where a long wait belongs (a
reviewed commit with ``full_tests``, evolution's forced post-commit run); these
consumers accept only its evidence, for exactly the commit in question.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

_HOW = ("a run that also carried uncommitted or untracked files does not count; commit it with full_tests=true "
        "from a clean checkout so the post-commit run tests exactly this tree, then request the restart again")


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=60)


def _commit_tree(repo, commit) -> str:
    tree = _git(repo, "rev-parse", "--verify", f"{commit}^{{tree}}")
    return "" if tree.returncode else tree.stdout.strip()


def full_proof_error(ctx, repo, commit="") -> str:
    from ouroboros.commit_admission import preflight_test_proof_matches

    proof = getattr(ctx, "_preflight_test_proof", None)
    try:
        # A named commit needs a run of exactly its tree: green uncommitted or untracked
        # bytes on top of it are another body.
        if (getattr(ctx, "_preflight_tests_passed", False) and proof is not None
                and (not commit or (proof.head == commit and proof.tree == proof.index_tree == _commit_tree(repo, commit)))
                and preflight_test_proof_matches(ctx, repo)):
            return ""
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        pass
    return "a full green exact-tree test proof is required; selected, skipped or stale evidence cannot qualify"


def receipt_for_commit(data_dir, repo, commit) -> dict:
    """A coherent durable receipt of a complete full run of exactly ``commit``, or ``{}``."""
    from ouroboros.preflight_receipts import RECEIPT_SCHEMA, RECEIPTS_DIR, _record_problem

    tree = _commit_tree(repo, commit)
    if not tree:
        return {}
    # The run must have carried the Node lane exactly when this commit has web tests.
    node_ran = any(name.endswith(".test.js") for name in _git(repo, "ls-tree", "-r", "--name-only", commit, "--",
                                                               "web/tests").stdout.split())
    for path in sorted((Path(data_dir) / "state" / RECEIPTS_DIR).glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            identity = document["identity"]
            if (identity["schema"] == RECEIPT_SCHEMA and identity["head"] == commit
                    and identity["tree"] == identity["index_tree"] == tree
                    and not _record_problem(document, identity, node_ran)):
                return {"source": "receipt", "key": path.stem, "head": commit, "tree": tree,
                        "created_at": document["created_at"], "phase": document["phase"]}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return {}


def adoption_test_evidence(data_dir, repo, commit, ctx=None) -> dict:
    """The task's own full proof of ``commit`` or a durable receipt of it; never a new run."""
    if ctx is not None and not full_proof_error(ctx, repo, commit):
        proof = ctx._preflight_test_proof
        return {"tier": "full", "source": "proof", "head": proof.head, "tree": proof.tree}
    receipt = receipt_for_commit(data_dir, repo, commit)
    if receipt:
        return {"tier": "full", **receipt}
    raise ValueError(f"no full green test run of {str(commit)[:12]} is on record; {_HOW}")
