# Ouroboros — common development commands
# Usage: make test, make lint, make health

.PHONY: test lint health clean

# Full local battery: node lane + every default-lane test in one xdist run
# (modes and focused runs: scripts/run_tests.py)
test:
	uv run --locked python scripts/run_tests.py

# Single-process verbose run (slow: the whole default suite in one process)
test-v:
	uv run --locked python -m pytest tests/ -v --tb=long

# Lint: deterministic F-rule gate (NameError class); matches the CI quick-test step
lint:
	uv run --locked python -m ruff check . --select F

# Run codebase health check (requires ouroboros importable)
health:
	uv run --locked python -c "from ouroboros.review import collect_sections, compute_complexity_metrics; \
		import pathlib, json; \
		sections, stats = collect_sections(pathlib.Path('.'), pathlib.Path('../data')); \
		m = compute_complexity_metrics(sections); \
		print(json.dumps({'repo': stats, **m}, indent=2, default=str))"

# Clean Python cache files
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
