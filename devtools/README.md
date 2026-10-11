# Ouroboros Devtools

`devtools/` contains operator-side and benchmark support code that should be
versioned with Ouroboros without becoming part of the runtime core.

Rules:

- Generated logs, datasets, run outputs, Docker layers, and secrets do not live
  here.
- Choose a benchmark output root outside the source checkout and runtime data;
  use each runner's documented output option or `OUROBOROS_BENCH_RUNS_ROOT`.
- Runtime modules must not import `devtools`.
- This is not an immune-system bypass: touched files are reviewed normally.
- Promote code out of `devtools` only through a separate reviewed runtime plan.
