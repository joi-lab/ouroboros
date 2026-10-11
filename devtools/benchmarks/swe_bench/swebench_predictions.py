#!/usr/bin/env python3
"""Generate SWE-bench predictions JSONL with Ouroboros.

This helper prepares the official prediction artifact only. Evaluation remains
the responsibility of ``swebench.harness.run_evaluation``.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from devtools.benchmarks.common.manifests import admit_benchmark_run, finalize_run_manifest
from devtools.benchmarks.common.result_index import task_result_row, write_result_index
from devtools.benchmarks.common.run_roots import (
    default_settings_path,
    assert_file_output_outside_repo,
    assert_outside_repo,
    repo_root_from_devtools,
    safe_benchmark_id,
)
from devtools.benchmarks.common.official_commands import swebench_eval_cmd
from devtools.benchmarks.swe_bench.presets import resolve_preset
from ouroboros.config import get_finalization_grace_sec


REPO_ROOT = repo_root_from_devtools()


def _records(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        item = json.loads(raw)
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _resolve_workspace(item: dict[str, Any], workspaces_root: str) -> str:
    workspace = str(item.get("workspace_root") or "").strip()
    if workspace or not workspaces_root:
        return workspace
    root = Path(workspaces_root).expanduser()
    instance_id = str(item.get("instance_id") or "")
    repo = str(item.get("repo") or "").strip()
    candidates = [root / instance_id]
    if repo:
        candidates.extend([root / repo.replace("/", "__"), root / repo.split("/")[-1]])
    for candidate in candidates:
        if candidate.is_dir():
            return str(candidate)
    return ""


def _git_stdout(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=10)


def _record_error(errors: list[dict[str, Any]], row: dict[str, Any], continue_on_error: bool) -> None:
    errors.append(row)
    if not continue_on_error:
        raise RuntimeError(str(row.get("error") or row))


def _record_instance_failure(
    failure_context: dict[str, Any],
    *,
    instance_id: str,
    status: str,
    reason_code: str,
    error: str,
    details: dict[str, Any] | None = None,
    error_payload: dict[str, Any] | None = None,
) -> None:
    ledger_rows = failure_context["ledger_rows"]
    errors = failure_context["errors"]
    continue_on_error = bool(failure_context.get("continue_on_error"))
    ledger_rows.append(
        task_result_row(
            benchmark="swe_bench",
            instance_id=instance_id,
            status=status,
            reason_code=reason_code,
            error=error,
            details=details,
        )
    )
    payload = {"instance_id": instance_id, "error": error, "reason_code": reason_code}
    if error_payload:
        payload.update(error_payload)
    _record_error(errors, payload, continue_on_error)


def _append_not_attempted_rows(
    ledger_rows: list[dict[str, Any]],
    input_rows: list[dict[str, Any]],
    *,
    benchmark: str,
) -> None:
    for item in input_rows[len(ledger_rows):]:
        ledger_rows.append(
            task_result_row(
                benchmark=benchmark,
                instance_id=str(item.get("instance_id") or ""),
                status="not_attempted",
                reason_code="aborted_after_prior_error",
                official_eval_status="not_run",
                error="not attempted because fail-fast stopped after an earlier error",
            )
        )


def _write_logs(logs_dir: str, instance_id: str, stdout: str, stderr: str, summary: dict[str, Any]) -> None:
    if not logs_dir:
        return
    log_dir = Path(logs_dir).expanduser() / safe_benchmark_id(instance_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "ouroboros.stdout").write_text(stdout, encoding="utf-8")
    (log_dir / "ouroboros.stderr").write_text(stderr, encoding="utf-8")
    (log_dir / "ouroboros-agent-result.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _build_ouroboros_cmd(args: argparse.Namespace, workspace: Path, result_json_path: Path, prompt: str) -> list[str]:
    cli_prefix = shlex.split(args.cli) if args.cli else [sys.executable, "-m", "ouroboros.cli"]
    # E2BIG hygiene (C5): the prompt travels as a FILE, never as an argv tail —
    # a benchmark prompt with a long issue body can exceed the kernel's per-arg
    # limit. Written beside the result json so it survives as a run artifact.
    prompt_path = result_json_path.parent / (result_json_path.stem + ".prompt.txt")
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    return [
        *cli_prefix,
        "run",
        "--workspace",
        str(workspace),
        "--memory-mode",
        "empty",
        "--timeout",
        str(int(args.timeout)),
        "--patch",
        "--result-json-out",
        str(result_json_path),
        "--prompt-file",
        str(prompt_path),
    ]


def _load_task_result(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _run_prediction_rows(
    args: argparse.Namespace,
    input_rows: list[dict[str, Any]],
    *,
    output_path: Path,
    logs_dir: str,
) -> tuple[list[dict[str, str]], list[dict[str, Any]], list[dict[str, Any]], Exception | None]:
    predictions: list[dict[str, str]] = []
    errors: list[dict[str, Any]] = []
    ledger_rows: list[dict[str, Any]] = []
    failure_context = {"ledger_rows": ledger_rows, "errors": errors, "continue_on_error": args.continue_on_error}
    first_error: Exception | None = None
    try:
        for item in input_rows:
            instance_id = str(item.get("instance_id") or "")
            try:
                safe_instance_id = safe_benchmark_id(instance_id)
            except ValueError as exc:
                _record_instance_failure(
                    failure_context,
                    instance_id=instance_id,
                    status="failed",
                    reason_code="invalid_instance_id",
                    error=str(exc),
                )
                continue
            workspace = _resolve_workspace(item, args.workspaces_root)
            prompt = str(item.get("problem_statement") or item.get("prompt") or "")
            if not instance_id or not workspace or not prompt:
                _record_instance_failure(
                    failure_context,
                    instance_id=instance_id,
                    status="failed",
                    reason_code="invalid_instance",
                    error="missing instance_id, workspace, or prompt",
                    error_payload={
                        "error": "each row must include instance_id, workspace_root or --workspaces-root, and problem_statement/prompt"
                    },
                )
                continue

            workspace_path = Path(workspace).expanduser().resolve(strict=False)
            if not workspace_path.is_dir():
                _record_instance_failure(
                    failure_context,
                    instance_id=instance_id,
                    status="failed",
                    reason_code="invalid_workspace",
                    error=f"workspace_root is not a directory: {workspace}",
                    error_payload={"error": f"workspace_root is not a directory for {instance_id}: {workspace}"},
                )
                continue
            head = _git_stdout(["git", "rev-parse", "HEAD"], workspace_path)
            if head.returncode != 0:
                _record_instance_failure(
                    failure_context,
                    instance_id=instance_id,
                    status="failed",
                    reason_code="not_git_checkout",
                    error=f"workspace_root is not a git checkout: {workspace_path}",
                    error_payload={"error": f"workspace_root is not a git checkout for {instance_id}: {workspace_path}"},
                )
                continue
            base_commit = str(item.get("base_commit") or "").strip()
            if base_commit and head.stdout.strip() != base_commit:
                _record_instance_failure(
                    failure_context,
                    instance_id=instance_id,
                    status="failed",
                    reason_code="wrong_base_commit",
                    error=f"workspace HEAD is {head.stdout.strip()}, expected {base_commit}",
                    details={"actual_head": head.stdout.strip(), "expected_base_commit": base_commit},
                    error_payload={
                        "error": f"workspace HEAD for {instance_id} is {head.stdout.strip()}, expected base_commit {base_commit}"
                    },
                )
                continue
            status = _git_stdout(["git", "status", "--porcelain=v1", "--untracked-files=all"], workspace_path)
            if status.returncode != 0 or status.stdout.strip():
                _record_instance_failure(
                    failure_context,
                    instance_id=instance_id,
                    status="failed",
                    reason_code="dirty_workspace",
                    error=f"workspace must be clean before SWE-bench run for {instance_id}",
                    details={"status_entries": len([line for line in status.stdout.splitlines() if line.strip()])},
                )
                continue

            if logs_dir:
                result_json_path = Path(logs_dir) / safe_instance_id / "task_result.json"
            else:
                result_json_path = Path(tempfile.gettempdir()) / f"ouroboros_swebench_{safe_instance_id}.task_result.json"
            result_json_path.parent.mkdir(parents=True, exist_ok=True)
            cmd = _build_ouroboros_cmd(args, workspace_path, result_json_path, prompt)
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=int(args.timeout) + get_finalization_grace_sec() + 60,
                )
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")
                stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")
                _write_logs(
                    logs_dir,
                    instance_id,
                    stdout,
                    stderr,
                    {
                        "instance_id": instance_id,
                        "returncode": 124,
                        "stdout_chars": len(stdout),
                        "stderr_chars": len(stderr),
                        "timeout_sec": int(args.timeout),
                        "failure_mode": "timeout",
                    },
                )
                _record_instance_failure(
                    failure_context,
                    instance_id=instance_id,
                    status="timeout",
                    reason_code="timeout",
                    error=f"ouroboros run timed out after {int(args.timeout)}s",
                    details={"timeout_sec": int(args.timeout), "stdout_chars": len(stdout), "stderr_chars": len(stderr)},
                    error_payload={"returncode": 124, "timeout": True},
                )
                continue

            task_result = _load_task_result(result_json_path)
            summary = {
                "instance_id": instance_id,
                "returncode": result.returncode,
                "stdout_chars": len(result.stdout or ""),
                "stderr_chars": len(result.stderr or ""),
                "patch_empty": not bool((result.stdout or "").strip()),
                "timeout_sec": int(args.timeout),
                "outcome_axes": task_result.get("outcome_axes"),
                "reason_code": task_result.get("reason_code"),
                "artifact_bundle": task_result.get("artifact_bundle"),
            }
            _write_logs(logs_dir, instance_id, result.stdout or "", result.stderr or "", summary)
            if result.returncode != 0:
                details = (result.stderr or result.stdout or "").strip()
                if len(details) > 4000:
                    details = details[:4000] + "\n...[truncated]"
                reason_code = str(task_result.get("reason_code") or "cli_failed")
                error = details or f"ouroboros run exited {result.returncode}"
                _record_instance_failure(
                    failure_context,
                    instance_id=instance_id,
                    status="failed",
                    reason_code=reason_code,
                    error=error,
                    details={
                        "returncode": result.returncode,
                        "outcome_axes": task_result.get("outcome_axes"),
                        "artifact_bundle": task_result.get("artifact_bundle"),
                    },
                    error_payload={
                        "returncode": result.returncode,
                        "outcome_axes": task_result.get("outcome_axes"),
                        "reason_code": task_result.get("reason_code"),
                        "artifact_bundle": task_result.get("artifact_bundle"),
                        "trace_refs": task_result.get("trace_refs"),
                    },
                )
                continue
            if not (result.stdout or "").strip():
                _record_instance_failure(
                    failure_context,
                    instance_id=instance_id,
                    status="empty_patch",
                    reason_code=str(task_result.get("reason_code") or "no_patch"),
                    error="ouroboros run produced no patch",
                    details={
                        "outcome_axes": task_result.get("outcome_axes"),
                        "artifact_bundle": task_result.get("artifact_bundle"),
                    },
                    error_payload={
                        "returncode": 0,
                        "outcome_axes": task_result.get("outcome_axes"),
                        "reason_code": task_result.get("reason_code") or "no_patch",
                        "artifact_bundle": task_result.get("artifact_bundle"),
                        "trace_refs": task_result.get("trace_refs"),
                    },
                )
                continue
            predictions.append(
                {
                    "instance_id": instance_id,
                    "model_name_or_path": args.model_name,
                    "model_patch": result.stdout,
                }
            )
            ledger_rows.append(
                task_result_row(
                    benchmark="swe_bench",
                    instance_id=instance_id,
                    status="completed",
                    reason_code="patch_generated",
                    # A patch produced under `budget_exhausted` used to be indistinguishable
                    # from a clean completion: the failure branches above already publish the
                    # runtime `reason_code`, the SUCCESS branch dropped it for a hard-coded
                    # adapter-stage literal. The patch is still real and still scored — this
                    # only says how the runtime stopped producing it.
                    runtime_result=task_result,
                    prediction_written=True,
                    official_eval_status="pending",
                    output_paths={"prediction_jsonl": str(output_path),
                                  "task_result": str(result_json_path)},
                    details={"patch_bytes": len(result.stdout.encode("utf-8", errors="replace"))},
                )
            )
    except Exception as exc:
        first_error = exc
    return predictions, errors, ledger_rows, first_error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="JSONL instances")
    parser.add_argument("--output", required=True, help="SWE-bench predictions JSONL")
    parser.add_argument("--model-name", default="ouroboros-cli")
    parser.add_argument("--cli", default="", help="optional Ouroboros CLI command prefix")
    parser.add_argument("--timeout", type=int, default=7200, help="per-instance Ouroboros timeout seconds")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--errors-output", default="")
    parser.add_argument("--ledger-output", default="", help="denominator-preserving JSONL result ledger")
    parser.add_argument("--manifest-output", default="", help="run manifest JSON")
    parser.add_argument("--logs-dir", default="")
    parser.add_argument("--workspaces-root", default="")
    parser.add_argument("--settings-path", default="")
    parser.add_argument("--isolated-data-root", default="", help="isolated Ouroboros data root used for this run")
    parser.add_argument("--print-eval-command", default="", help="optional preset/dataset for official eval command")
    parser.add_argument(
        "--allow-dirty-seed",
        action="store_true",
        help="record and proceed with an unclean/unidentifiable seed checkout instead of refusing",
    )
    args = parser.parse_args()
    settings_path = Path(args.settings_path).expanduser() if args.settings_path else default_settings_path()

    input_path = Path(args.input).expanduser()
    output_path = assert_file_output_outside_repo(Path(args.output), REPO_ROOT)
    errors_output_path = (
        assert_file_output_outside_repo(Path(args.errors_output), REPO_ROOT)
        if args.errors_output
        else Path(str(output_path) + ".errors.jsonl")
    )
    ledger_output_path = (
        assert_file_output_outside_repo(Path(args.ledger_output), REPO_ROOT)
        if args.ledger_output
        else Path(str(output_path) + ".ledger.jsonl")
    )
    manifest_output_path = (
        assert_file_output_outside_repo(Path(args.manifest_output), REPO_ROOT)
        if args.manifest_output
        else Path(str(output_path) + ".run_manifest.json")
    )
    logs_dir = str(assert_outside_repo(Path(args.logs_dir), REPO_ROOT)) if args.logs_dir else ""

    eval_command = (
        swebench_eval_cmd(resolve_preset(args.print_eval_command), output_path, "ouroboros", 1)
        if args.print_eval_command
        else []
    )
    # Manifest FIRST (seed-provenance gate before any paid agent run): `admit_benchmark_run`
    # persists the complete gate/refusal payload BEFORE enforcement raises, and the retained dict
    # is augmented with the outcome counts and finalized with a typed outcome on every exit path.
    manifest = admit_benchmark_run(
        manifest_output_path,
        benchmark="swe_bench",
        run_root=output_path.parent,
        repo_dir=REPO_ROOT,
        # The DECLARED input path, not the ids inside it: `_records` reads and JSON-parses
        # `--input`, and a missing or malformed instance file used to kill the process before
        # any manifest existed. Reading it is now the first thing the ADMITTED run does, and
        # `requested_task_ids` is amended below with what it actually contained.
        requested_task_ids=[],
        require_clean=not args.allow_dirty_seed,
        output_paths={
            "predictions": str(output_path),
            "errors": str(errors_output_path),
            "ledger": str(ledger_output_path),
        },
        dataset=resolve_preset(args.print_eval_command) if args.print_eval_command else "",
        official_command=eval_command,
        timeout_sec=int(args.timeout),
        isolated_data_root=str(args.isolated_data_root or ""),
        settings_path=settings_path,
        extra={"model_name_or_path": args.model_name, "input": str(input_path)},
    )

    with finalize_run_manifest(manifest_output_path, manifest, outcome="completed") as final:
        input_rows = _records(input_path)
        manifest["requested_task_ids"] = [str(item.get("instance_id") or "") for item in input_rows]
        manifest["requested_count"] = len(input_rows)
        predictions, errors, ledger_rows, first_error = _run_prediction_rows(
            args,
            input_rows,
            output_path=output_path,
            logs_dir=logs_dir,
        )

        if first_error is not None and not args.continue_on_error:
            _append_not_attempted_rows(ledger_rows, input_rows, benchmark="swe_bench")

        output_path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in predictions) + ("\n" if predictions else ""),
            encoding="utf-8",
        )
        write_result_index(ledger_output_path, ledger_rows)
        if errors:
            errors_output_path.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in errors) + "\n",
                encoding="utf-8",
            )
        manifest["extra"].update({"prediction_count": len(predictions), "error_count": len(errors)})
        if args.print_eval_command:
            print(" ".join(shlex.quote(part) for part in eval_command))
        if first_error is not None:
            # A named terminal outcome BEFORE the re-raise: the run stopped on an instance
            # error, which the shared seam then annotates with the typed exception.
            final.update({"outcome": "stopped_instance_error", "exit_code": 1})
            raise first_error
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
