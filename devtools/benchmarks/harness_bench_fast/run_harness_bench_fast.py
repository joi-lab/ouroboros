#!/usr/bin/env python3
"""Run harness-bench-fast through the Ouroboros CLI wrapper."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import subprocess
import sys
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from devtools.benchmarks.common.manifests import (
    admit_benchmark_run,
    finalize_run_manifest,
    write_json,
)
from devtools.benchmarks.common.model_slots import (
    fixed_model_actor_snapshot,
)
from devtools.benchmarks.common.result_index import task_result_row, write_result_index
from devtools.benchmarks.common.run_roots import assert_outside_repo, default_settings_path, repo_root_from_devtools, run_root


# Parameterized: env HARNESS_BENCH_ROOT, else a repo-relative sibling fallback (never a
# hardcoded contributor home path).
DEFAULT_BENCH_ROOT = os.environ.get("HARNESS_BENCH_ROOT") or str(
    pathlib.Path(__file__).resolve().parents[3] / "harness-bench-fast"
)
WRAPPER = pathlib.Path(__file__).with_name("ouroboros_cli_wrapper.py")


def _task_ids_from_file(path: pathlib.Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"task file not found: {path}")
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        tasks = data.get("tasks") if isinstance(data, dict) else data
        ids: list[str] = []
        if isinstance(tasks, list):
            for item in tasks:
                if isinstance(item, str):
                    ids.append(item)
                elif isinstance(item, dict) and item.get("id"):
                    ids.append(str(item["id"]))
        return ids
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _read_task_ids(bench_root: pathlib.Path, task_ids: list[str], task_file: str = "") -> list[str]:
    if task_file:
        from_file = _task_ids_from_file(pathlib.Path(task_file).expanduser())
        return [*from_file, *task_ids]
    if task_ids:
        return list(task_ids)
    try:
        out = subprocess.run(
            ["uv", "run", "python", "-m", "harness_bench", "list"],
            cwd=bench_root,
            text=True,
            capture_output=True,
            timeout=60,
        )
        ids: list[str] = []
        for line in out.stdout.splitlines():
            text = line.strip()
            if text.startswith("task_") and " " in text:
                ids.append(text.split()[0])
        return ids
    except Exception:
        return []


def _wrapper_command(
    *,
    repo_dir: pathlib.Path,
    data_dir: pathlib.Path,
    settings_path: pathlib.Path,
    model: str,
    log_root: pathlib.Path,
    timeout: int,
) -> str:
    parts = [
        sys.executable,
        str(WRAPPER),
        "--repo-dir",
        str(repo_dir),
        "--data-dir",
        str(data_dir),
        "--settings-path",
        str(settings_path),
        "--model",
        model,
        "--log-root",
        str(log_root),
        "--timeout",
        str(timeout),
        "--memory-mode",
        "empty",
        "--",
    ]
    return shlex.join(parts)


def _harness_command(
    *,
    bench_root: pathlib.Path,
    cli_command: str,
    task_ids: list[str],
    timeout: int,
    concurrency: int,
    attempts: int,
    json_output: pathlib.Path,
    allow_task_failures: bool,
) -> list[str]:
    cmd = [
        "uv",
        "run",
        "python",
        "-m",
        "harness_bench",
        "run-cli",
        "--cli-command",
        cli_command,
        "--timeout",
        str(timeout),
        "--concurrency",
        str(concurrency),
        "--attempts",
        str(attempts),
        "--json-output",
        str(json_output),
    ]
    for task_id in task_ids:
        cmd.extend(["--task", task_id])
    if allow_task_failures:
        cmd.append("--allow-task-failures")
    return cmd


def _write_ledger_from_results(
    *,
    result_json: pathlib.Path,
    ledger_output: pathlib.Path,
    requested_task_ids: list[str],
) -> None:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    if result_json.exists():
        try:
            data = json.loads(result_json.read_text(encoding="utf-8"))
        except Exception as exc:
            data = {"error": f"{type(exc).__name__}: {exc}"}
        raw_results = None
        if isinstance(data, dict):
            raw_results = data.get("results")
            if raw_results is None:
                raw_results = data.get("tasks")
        if isinstance(raw_results, list):
            for item in raw_results:
                if not isinstance(item, dict):
                    continue
                task_id = str(item.get("task_id") or item.get("id") or "").strip()
                if not task_id:
                    continue
                seen.add(task_id)
                passed = bool(item.get("passed"))
                rows.append(
                    task_result_row(
                        benchmark="harness_bench_fast",
                        instance_id=task_id,
                        status="passed" if passed else "failed",
                        reason_code="passed" if passed else "verifier_failed",
                        official_eval_status="completed",
                        output_paths={"results_json": str(result_json)},
                        error=str(item.get("error") or ""),
                        details=item,
                    )
                )
    for task_id in requested_task_ids:
        if task_id in seen:
            continue
        rows.append(
            task_result_row(
                benchmark="harness_bench_fast",
                instance_id=task_id,
                status="not_attempted",
                reason_code="missing_result",
                official_eval_status="not_run",
                output_paths={"results_json": str(result_json)},
            )
        )
    write_result_index(ledger_output, rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bench-root", default=DEFAULT_BENCH_ROOT)
    parser.add_argument("--repo-dir", default=str(repo_root_from_devtools()))
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--settings-path", default="")
    parser.add_argument("--model", default="openai/gpt-5.5")
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--task-file", default="", help="newline text file or JSON sample with task ids")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--run-root", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-allow-task-failures", action="store_true")
    parser.add_argument(
        "--allow-dirty-seed",
        action="store_true",
        help="record and proceed with an unclean/unidentifiable seed checkout instead of refusing",
    )
    args = parser.parse_args()

    repo_dir = pathlib.Path(args.repo_dir).expanduser().resolve(strict=False)
    bench_root = pathlib.Path(args.bench_root).expanduser().resolve(strict=False)
    data_dir = pathlib.Path(args.data_dir).expanduser().resolve(strict=False) if args.data_dir else repo_dir.parent / "data"
    settings_path = pathlib.Path(args.settings_path).expanduser().resolve(strict=False) if args.settings_path else default_settings_path()
    out_root = assert_outside_repo(
        pathlib.Path(args.run_root).expanduser() if args.run_root else run_root("harness_bench_fast", args.run_id),
        repo_dir,
    )
    # No pre-admission mkdir: the atomic manifest write creates the run root, and the console
    # log is opened only after admission.
    results_json = out_root / "results.json"
    ledger_output = out_root / "result_index.jsonl"
    manifest_output = out_root / "run_manifest.json"
    console_log = out_root / "console.log"
    wrapper_log_root = out_root / "per_task_logs"

    # `_read_task_ids` DISCOVERS ids by shelling out to `uv run python -m harness_bench list`
    # with a 60s timeout, so it must not run before the run is on disk: admission records the
    # ids the CLI actually DECLARED (pure argv), and discovery plus the derived commands are
    # attached to the retained manifest inside the finalization block below. The widened seam
    # meta-test caught this — the subprocess was hidden one level down inside a local helper.
    declared_task_ids = list(args.task)
    fixed_actor = fixed_model_actor_snapshot(args.model)
    # Built ONCE (the seed gate runs inside it) and PERSISTED BEFORE enforcement can raise, so
    # a refusal leaves a durable record of what was refused; then RETAINED and rewritten with the
    # final typed outcome on every exit path by the shared finalization seam below.
    manifest = admit_benchmark_run(
        manifest_output,
        benchmark="harness_bench_fast",
        run_root=out_root,
        repo_dir=repo_dir,
        requested_task_ids=declared_task_ids,
        require_clean=not args.allow_dirty_seed,
        argv=sys.argv,
        dataset=f"harness-bench-fast:{bench_root}",
        settings_path=settings_path,
        timeout_sec=args.timeout,
        output_paths={
            "results_json": str(results_json),
            "ledger": str(ledger_output),
            "manifest": str(manifest_output),
            "console_log": str(console_log),
            "per_task_logs": str(wrapper_log_root),
        },
        harness={
            "mode": "run-cli",
            "bench_root": str(bench_root),
            "concurrency": args.concurrency,
            "attempts": args.attempts,
            "memory_mode": "empty",
        },
        extra={"outcome": "started"},
    )
    # The measured model is the CLI's exact value, not an ambient/template slot.
    # Persist this before task discovery (which may invoke harness-bench itself).
    manifest["model_slots"] = fixed_actor["model_slots"]
    manifest["available_subagents"] = fixed_actor["available_subagents"]
    manifest["harness"]["fixed_model_actor"] = fixed_actor
    write_json(manifest_output, manifest)

    with finalize_run_manifest(manifest_output, manifest) as final:
        task_ids = _read_task_ids(bench_root, args.task, args.task_file)
        cli_command = _wrapper_command(
            repo_dir=repo_dir,
            data_dir=data_dir,
            settings_path=settings_path,
            model=args.model,
            log_root=wrapper_log_root,
            timeout=args.timeout,
        )
        cmd = _harness_command(
            bench_root=bench_root,
            cli_command=cli_command,
            task_ids=task_ids,
            timeout=args.timeout,
            concurrency=args.concurrency,
            attempts=args.attempts,
            json_output=results_json,
            allow_task_failures=not args.no_allow_task_failures,
        )
        # Late facts on the RETAINED manifest, which the finalization seam writes.
        manifest["requested_task_ids"] = [str(task_id) for task_id in task_ids]
        manifest["requested_count"] = len(task_ids)
        manifest["harness"] = {**(manifest.get("harness") or {}), "cli_command": cli_command}
        manifest["official_command"] = cmd
        print(shlex.join(cmd))
        if args.dry_run:
            _write_ledger_from_results(result_json=results_json, ledger_output=ledger_output, requested_task_ids=task_ids)
            final["outcome"] = "dry_run"
            return 0

        with console_log.open("w", encoding="utf-8") as fh:
            proc = subprocess.run(
                cmd,
                cwd=bench_root,
                text=True,
                stdout=fh,
                stderr=subprocess.STDOUT,
            )
        _write_ledger_from_results(result_json=results_json, ledger_output=ledger_output, requested_task_ids=task_ids)
        code = int(proc.returncode)
        final.update({"outcome": "completed" if code == 0 else "harness_nonzero_exit",
                      "exit_code": code})
        return code


if __name__ == "__main__":
    raise SystemExit(main())
