# Viktor-Friday Skills-First Blueprint

This document maps the "Karpathy-style" principles to concrete files in this repository.

## 1) Keep core small and readable

- Core runtime stays in `/vfriday/`.
- Extensions must not be merged into core directly unless they are bug/security fixes.
- Governance and orchestration remain in:
  - `/vfriday/app.py`
  - `/vfriday/pipeline.py`
  - `/vfriday/governance/*`

## 2) One skill = one happy path (no config zoo)

- Skill packages live in `/.claude/skills/<skill>/`.
- Each skill has:
  - `SKILL.md`
  - `manifest.yaml`
  - `add/` (new files)
  - `modify/` (full-file merge payloads when needed)
- Template:
  - `/.claude/skills/_template/`

## 3) Deterministic first, AI fallback second

- Deterministic apply pipeline:
  - `/vfriday/skills_engine/apply.py`
  - `/vfriday/skills_engine/merge.py`
  - `/vfriday/skills_engine/structured.py`
- Current policy: unresolved conflicts fail the run and require PR review.

## 4) Mandatory tests after apply/update/uninstall

- Per-skill test command is declared in `manifest.yaml:test`.
- Apply runner executes tests before recording success:
  - `/vfriday/skills_engine/apply.py`

## 5) Forkability first

- Skills mutate the local fork codebase deterministically.
- Applied state is explicit and replay-oriented:
  - `/.vfriday/state.yaml`
  - `/.vfriday/base/`
- Init and apply commands:
  - `/scripts/vfriday_skill_init.py`
  - `/scripts/vfriday_skill_apply.py`
  - `/scripts/vfriday_skill_state.py`

## 6) Secure-by-default boundaries

- Use container runtime for production deployment (`docker-compose.yml`).
- Keep sensitive operations behind confirm-gates and PR approvals.
- Skills should avoid touching secrets directly; only declare required env keys.

## 7) Transparent state, hashes, replay, audit

- State + applied hashes:
  - `/vfriday/skills_engine/state.py`
- Backup/restore safety:
  - `/vfriday/skills_engine/backup.py`
- Existing runtime audit trail remains in JSONL/SQLite from `vfriday/storage.py`.

## Recommended workflow

1. Initialize skills system:
```bash
python3 scripts/vfriday_skill_init.py
```
2. Apply one skill:
```bash
python3 scripts/vfriday_skill_apply.py .claude/skills/add-lean4-verifier
```
3. Inspect state:
```bash
python3 scripts/vfriday_skill_state.py
```
