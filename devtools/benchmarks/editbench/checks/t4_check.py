"""Behavior check for t4_move (run with PYTHONPATH=<candidate workspace>)."""
import ouroboros.git_shell_policy as gsp
import ouroboros.shell_parse as sp

assert not hasattr(sp, "collect_leading_env"), "collect_leading_env must be gone from shell_parse"
assigns, rest = gsp._collect_leading_env(["env", "A=1", "git", "status"])
assert assigns == {"A": "1"} and rest == ["git", "status"], (assigns, rest)
assigns, rest = gsp._collect_leading_env(["GIT_DIR=/x", "git", "log"])
assert assigns == {"GIT_DIR": "/x"} and rest == ["git", "log"], (assigns, rest)
print("OK")
