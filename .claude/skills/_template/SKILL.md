---
name: "<skill-name>"
description: "One happy-path extension for Viktor-Friday."
---

# Skill Goal

Describe exactly one extension. Keep it narrow and deterministic.

## Rules

1. Do not patch unrelated files.
2. Use `manifest.yaml` as the source of truth.
3. Run tests listed in the manifest.
4. If conflicts appear, stop and open a PR for manual review.

## Apply

```bash
python scripts/vfriday_skill_apply.py .claude/skills/<skill-name>
```

