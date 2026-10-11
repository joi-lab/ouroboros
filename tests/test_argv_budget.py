"""C5 E2BIG hygiene: the byte-accurate argv/env budget helper and the CLI
prompt-file transport (positional XOR --prompt-file, stdin via '-')."""

from __future__ import annotations

import io

import pytest

from ouroboros.argv_budget import (
    PER_ARG_LIMIT_BYTES,
    argv_budget_excess,
    argv_env_bytes,
    encoded_arg_bytes,
    system_argv_limit_bytes,
)


class TestArgvBudget:
    def test_encoded_bytes_are_utf8_bytes_not_characters(self):
        # The prior art counted characters; a CJK/emoji payload under-counts 3-4x.
        assert encoded_arg_bytes("abc") == 4  # 3 bytes + NUL
        assert encoded_arg_bytes("яяя") == 7  # 2 bytes each + NUL
        assert argv_env_bytes(["я"], env={}) == 3

    def test_env_counts_toward_the_same_pool(self):
        empty = argv_env_bytes(["x"], env={})
        with_env = argv_env_bytes(["x"], env={"KEY": "value"})
        assert with_env == empty + len("KEY") + encoded_arg_bytes("value") + 1

    def test_single_giant_argument_is_named_before_the_total(self):
        excess = argv_budget_excess(["prog", "x" * (PER_ARG_LIMIT_BYTES + 1)], env={})
        assert "argv[1]" in excess and "MAX_ARG_STRLEN" in excess

    def test_total_budget_includes_env(self):
        excess = argv_budget_excess(
            ["prog", "arg"], env={"BIG": "v" * 60_000},
            limit_bytes=60_000, headroom_bytes=1024)
        assert "argv+env total" in excess

    def test_a_normal_command_fits(self):
        assert argv_budget_excess(["python", "-m", "ouroboros.cli", "run", "hello"],
                                  env={"PATH": "/usr/bin"}) == ""

    def test_system_limit_is_positive(self):
        assert system_argv_limit_bytes() >= 4096

    def test_multibyte_arg_is_judged_by_bytes(self):
        # A char-counted guard passes this; the kernel does not.
        payload = "я" * (PER_ARG_LIMIT_BYTES // 2 + 10)
        assert argv_budget_excess(["prog", payload], env={}) != ""

    def test_per_arg_boundary_counts_the_terminating_nul(self):
        # F9: `copy_strings` measures the string WITH its NUL, so the largest
        # string that still execs is limit-1 content bytes. `content > limit` let
        # the exact-boundary case through and E2BIG'd at exec.
        # An explicit generous total limit isolates the per-arg check: on Windows
        # the real total budget (32K chars) is smaller than one max POSIX arg, so
        # without it the TOTAL clause fires first and this boundary is untestable.
        roomy = PER_ARG_LIMIT_BYTES * 4
        assert argv_budget_excess(["prog", "x" * (PER_ARG_LIMIT_BYTES - 1)], env={},
                                  limit_bytes=roomy) == ""
        exact = argv_budget_excess(["prog", "x" * PER_ARG_LIMIT_BYTES], env={},
                                   limit_bytes=roomy)
        assert "argv[1]" in exact and "including its NUL" in exact

    def test_oversized_env_string_is_named(self):
        # F9: the kernel copies env strings through the SAME path, so one
        # oversized KEY=value fails the exec exactly like an oversized argument —
        # and was not checked at all.
        excess = argv_budget_excess(["prog"], env={"PROMPT": "x" * PER_ARG_LIMIT_BYTES})
        assert "PROMPT" in excess and "KEY=value" in excess
        # The KEY counts too: value just under the cap, key pushes it over.
        assert argv_budget_excess(
            ["prog"], env={"K" * 40: "x" * (PER_ARG_LIMIT_BYTES - 20)}) != ""


class TestCliPromptFile:
    def _args(self, argv):
        from ouroboros.cli import build_parser

        return build_parser().parse_args(argv)

    def test_prompt_file_reads_the_file(self, tmp_path):
        from ouroboros.cli import _resolve_prompt

        path = tmp_path / "prompt.txt"
        path.write_text("do the thing\n", encoding="utf-8")
        args = self._args(["run", "--detach", "--prompt-file", str(path)])
        assert _resolve_prompt(args) == "do the thing"

    def test_positional_and_file_are_mutually_exclusive(self, tmp_path):
        from ouroboros.cli import CLIError, _resolve_prompt

        path = tmp_path / "prompt.txt"
        path.write_text("x", encoding="utf-8")
        args = self._args(["run", "--prompt-file", str(path), "also", "positional"])
        with pytest.raises(CLIError, match="not both"):
            _resolve_prompt(args)

    def test_stdin_transport_via_dash(self, monkeypatch):
        from ouroboros.cli import _resolve_prompt

        monkeypatch.setattr("sys.stdin", io.StringIO("from stdin\n"))
        args = self._args(["run", "--prompt-file", "-"])
        assert _resolve_prompt(args) == "from stdin"

    def test_unreadable_file_is_a_typed_cli_error(self, tmp_path):
        from ouroboros.cli import CLIError, _resolve_prompt

        args = self._args(["run", "--prompt-file", str(tmp_path / "absent.txt")])
        with pytest.raises(CLIError, match="unreadable"):
            _resolve_prompt(args)

    def test_positional_prompt_still_works(self):
        from ouroboros.cli import _resolve_prompt

        args = self._args(["run", "hello", "world"])
        assert _resolve_prompt(args) == "hello world"


class TestSkillExecBudget:
    def test_oversized_skill_args_are_refused_typed(self):
        # The helper is the seam skill_exec asks; prove the refusal wording the
        # tool relays is produced for a payload that would E2BIG.
        excess = argv_budget_excess(
            ["python3", "script.py", "x" * (PER_ARG_LIMIT_BYTES * 2)], env={})
        assert "file" in excess and "stdin" in excess

    def test_skill_exec_really_refuses_before_spawn(self, tmp_path, monkeypatch):
        # F9: the gate must fire in the TOOL, not only in the helper — a spawn
        # attempt with an over-budget argv is an OSError from the kernel, not a
        # typed refusal the agent can act on.
        from ouroboros.tools import skill_exec as skill_exec_mod

        from tests.test_skill_exec import (
            _build_skill, _make_ctx, _mark_reviewed_and_enabled,
        )

        skills_root = tmp_path / "skills"
        skill_dir = _build_skill(skills_root, "hello")
        ctx = _make_ctx(tmp_path)
        _mark_reviewed_and_enabled(ctx.drive_root, skill_dir, "hello")
        monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
        monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
        result = skill_exec_mod._handle_skill_exec(
            ctx, skill="hello", script="scripts/hello.py",
            args=["x" * (PER_ARG_LIMIT_BYTES + 1)],
        )
        assert "SKILL_EXEC_ARGV_TOO_LARGE" in result, result
        assert "MAX_ARG_STRLEN" in result
