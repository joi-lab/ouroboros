# Harness Bench Fast

`harness_bench_fast` lives upstream at
`https://github.com/ai-forever/harness-bench-fast`. This directory contains the
Ouroboros-side adapter pieces we need to keep reproducible in this repo instead
of in each developer's private checkout.

The current public task set is larger than the historical sample60 run in
`bench_runs/01_harness_bench_fast_sample60_gpt55/`; compare only within the same
task list and runner version.

## Wrapper

`ouroboros_cli_wrapper.py` is a thin generic CLI adapter. It reads the task
prompt from `--prompt-file` or stdin, then calls:

```bash
ouroboros run --memory-mode empty --quiet <prompt>
```

Use a fresh/isolated `OUROBOROS_DATA_DIR` for benchmark runs unless you
explicitly want carryover memory. The wrapper does not inject benchmark answers,
grader hints, or task-specific shortcuts. The wrapper requires `--model` and writes
both its model-slot summary and canonical one-row `OUROBOROS_SUBAGENTS` value from
that exact CLI model, so ambient settings cannot mislabel the fixed-model run.

Example:

```bash
python devtools/benchmarks/harness_bench_fast/ouroboros_cli_wrapper.py \
  --prompt-file /path/to/task_prompt.txt \
  --ouroboros-bin /Users/anton/Ouroboros/.venv/bin/ouroboros \
  --model openai/gpt-5.5 \
  --repo-dir /Users/anton/Ouroboros/repo
```

## Known Result Pattern

The archived sample60 run was 59/60. The single miss was a formatting error in
YAML (`task_89_create_pre_commit`: the file content was semantically right but
indented two spaces too deeply). This is a definition-of-done/format-verification
class, not a missing capability.
