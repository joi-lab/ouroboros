"""Behavior check for t3_blocks (run with PYTHONPATH=<candidate workspace>)."""
import shell_parse as sp

# new strip_leading_env_assignments: validates the key shape
assert sp.strip_leading_env_assignments(["A=1", "B=2", "ls"]) == ["ls"]
assert sp.strip_leading_env_assignments(["A-B=1", "ls"]) == ["A-B=1", "ls"]
assert sp.strip_leading_env_assignments(["MY_VAR=x", "echo"]) == ["echo"]
# new shell_command_string: --command= support kept alongside -c
assert sp.shell_command_string(["bash", "--command=ls -la"]) == "ls -la"
assert sp.shell_command_string(["bash", "-c", "pwd"]) == "pwd"
assert sp.shell_command_string(["bash", "-lc", "pwd"]) == "pwd"
# new sudo_noninteractive_violation: doas flagged
assert sp.sudo_noninteractive_violation(["doas", "ls"]) is True
assert sp.sudo_noninteractive_violation(["sudoedit", "f"]) is True
assert sp.sudo_noninteractive_violation(["sudo", "-n", "ls"]) is False
assert sp.sudo_noninteractive_violation(["sudo", "ls"]) is True
assert sp.sudo_noninteractive_violation(["ls"]) is False
print("OK")
